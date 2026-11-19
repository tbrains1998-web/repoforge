"""RepoForge - paste a GitHub link, it builds the project from the creator's source and installs it.

Deterministic parts (URL parsing, cloning, shortcuts, PATH shims, uninstall, updates) live here.
The build itself is handed to the Claude Code CLI (your Claude subscription), which reads the
repo's own build docs / CI, installs missing toolchains with winget, builds, and reports back
through a small JSON result file.
"""
import ctypes
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import winreg
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
LOGS = HERE / "logs"
DEFAULTS = {"apps_dir": r"%USERPROFILE%\RepoForgeApps", "model": "sonnet", "desktop_shortcuts": True,
            "launch_after_install": True, "max_nudges": 3}
NO_WINDOW = subprocess.CREATE_NO_WINDOW
CONFIG_ERROR = ""  # set when config.json can't be used; the program then stops with this message


def load_config() -> dict:
    """The settings. A missing config.json is made with the defaults. A damaged one is never overwritten (the user
    would lose their apps folder setting): CONFIG_ERROR says what is wrong and the defaults are used meanwhile."""
    global CONFIG_ERROR
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        try:
            CONFIG.write_text(json.dumps(DEFAULTS, indent=2), encoding="utf-8")
        except OSError:
            pass  # e.g. a read-only folder: the defaults still work
        return dict(DEFAULTS)
    except (OSError, ValueError) as e:
        CONFIG_ERROR = f"Couldn't read {CONFIG}: {e}"
        return dict(DEFAULTS)
    if not isinstance(cfg, dict):
        CONFIG_ERROR = f"{CONFIG} must hold one {{...}} block of settings, like config.example.json."
        return dict(DEFAULTS)
    wrong = [k for k, v in DEFAULTS.items() if k in cfg and type(cfg[k]) is not type(v)]
    if wrong:
        CONFIG_ERROR = (f"In {CONFIG} these settings have the wrong kind of value: {', '.join(wrong)} "
                        "(see config.example.json).")
        return dict(DEFAULTS)
    return dict(DEFAULTS, **cfg)


CFG = load_config()
APPS = Path(os.path.expandvars(CFG["apps_dir"]))  # e.g. %USERPROFILE%\RepoForgeApps
BIN = APPS / "bin"
REGISTRY = APPS / "repoforge-registry.json"
START_MENU = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "RepoForge"


# --------------------------------------------------------------------------- helpers
URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?"
    r"(?:/(?:tree|releases/tag|commit)/([^?#]+?))?/?(?:[?#].*)?$", re.I)
SHORT_RE = re.compile(r"^([\w.-]+)/([\w.-]+)$")


def parse_url(text: str):
    """-> (owner, repo, ref or None), or None if it isn't a GitHub project link."""
    text = text.strip().strip('"').strip()
    m = URL_RE.match(text)
    if m:
        return m.group(1), m.group(2), m.group(3)
    # Deeper links (e.g. .../blob/main/README.md) still identify the project.
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/", text, re.I)
    if m:
        return m.group(1), m.group(2), None
    m = SHORT_RE.match(text)
    return (m.group(1), m.group(2), None) if m else None


def gh_api(path: str):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "RepoForge"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    req = urllib.request.Request("https://api.github.com" + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def latest_ref(owner: str, repo: str, info: dict) -> tuple[str, str]:
    """-> (ref, kind): newest release tag if the creator publishes releases, else the default branch."""
    rel = gh_api(f"/repos/{owner}/{repo}/releases/latest")
    if rel and rel.get("tag_name"):
        return rel["tag_name"], "release"
    return info["default_branch"], "branch"


def remote_head(owner: str, repo: str, ref: str) -> str | None:
    c = gh_api(f"/repos/{owner}/{repo}/commits/{ref}")
    return c["sha"] if c else None


def rmtree(p: Path):
    def onexc(func, path, _exc):
        os.chmod(path, stat.S_IWRITE)  # git marks pack files read-only
        func(path)
    for attempt in range(6):
        if not p.exists():
            return
        try:
            shutil.rmtree(p, onexc=onexc)
            return
        except PermissionError:
            if attempt == 5:
                raise RuntimeError(f"Files in {p} are still in use by another program. "
                                   "Close the app (or wait a minute) and try again.") from None
            time.sleep(5)  # build helpers / antivirus usually let go within seconds


def pid_alive(pid: int) -> bool:
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return code.value == 259  # STILL_ACTIVE


def take_lock(root: Path) -> Path | None:
    """One install per project at a time (across RepoForge windows too). -> lock path, or None if busy."""
    lock = root / "repoforge.lock"
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                if pid_alive(int(lock.read_text())):
                    return None
            except (OSError, ValueError):
                pass
            lock.unlink(missing_ok=True)  # left behind by a crashed run
    return None


def _same_dir(a: Path, b: Path) -> bool:
    """True when a and b are the same folder (Windows ignores upper/lower case in names)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


GIT = ["git"]  # the git command (a list, so it can be a full path plus arguments)
_RUN_JOB = threading.local()  # set by Job._git: run() then starts the command in that job's kill group


class Cancelled(Exception):
    """Cancel was pressed (or the window closed) while a command ran."""


def run(cmd, cwd=None, log=None) -> str:
    job = getattr(_RUN_JOB, "job", None)
    if job is None:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", creationflags=NO_WINDOW)
        stdout, stderr, code = p.stdout, p.stderr, p.returncode
    else:
        # In the job's kill group, so cancel() stops it with every helper it started (e.g. git-remote-https).
        p = job._spawn(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                       text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        if p is None:
            raise Cancelled()
        stdout, stderr = p.communicate()
        code = p.returncode
        if job.cancelled:
            raise Cancelled()
    stdout, stderr = stdout or "", stderr or ""
    out = (stdout + stderr).strip()
    if log and out:
        log(out)
    if code:
        raise RuntimeError(f"{cmd[0]} failed ({code}): {out[-500:]}")
    return stdout.strip()


def find_claude_exe() -> str | None:
    """Path of the Claude Code CLI, or None. Never raises."""
    try:
        # Standalone install (newer desktop app versions no longer ship the CLI): wins when present.
        for cand in (Path.home() / ".local" / "bin" / "claude.exe",
                     Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin" / "claude.exe"):
            if cand.is_file():
                return str(cand)
        which = shutil.which("claude")
        if which and Path(which).is_file():
            return which
        # Older layout: the CLI bundled with the desktop app (a packaged MSIX app: outside the package its files
        # live under Packages\Claude_*). App updates changed the layout (claude-code\<ver>\claude.exe, later
        # claude-code\<ver>\<hash>\claude.exe), so search below each claude-code folder and sort by <ver>.
        roots = [Path(os.environ.get("APPDATA", "")) / "Claude" / "claude-code"]
        roots += list((Path(os.environ.get("LOCALAPPDATA", "")) / "Packages").glob(
            "Claude_*/LocalCache/Roaming/Claude/claude-code"))
        found = []
        for root in roots:
            for exe in root.glob("**/claude.exe"):
                ver = exe.relative_to(root).parts[0]
                if re.fullmatch(r"\d+(\.\d+)*", ver):
                    found.append((tuple(int(x) for x in ver.split(".")), exe.stat().st_mtime, exe))
        return str(max(found)[2]) if found else None
    except Exception:
        return None


def desktop_dir() -> Path:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            return Path(os.path.expandvars(winreg.QueryValueEx(k, "Desktop")[0]))
    except OSError:
        return Path(os.environ["USERPROFILE"]) / "Desktop"


def make_shortcut(lnk: Path, target: str, workdir: str, description: str = ""):
    q = lambda s: str(s).replace("'", "''")
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(lnk)}');"
          f"$s.TargetPath='{q(target)}';$s.WorkingDirectory='{q(workdir)}';"
          f"$s.Description='{q(description)}';$s.Save()")
    lnk.parent.mkdir(parents=True, exist_ok=True)
    run(["powershell", "-NoProfile", "-Command", ps])


BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(text, fallback) -> str:
    """A name Windows accepts as a file name (for a shortcut or command), else the cleaned fallback, else "app"."""
    for cand in (text, fallback):
        if not isinstance(cand, str):
            continue
        name = BAD_NAME_CHARS.sub("", cand).strip().rstrip(". ")[:100].rstrip(". ")
        if name and name.split(".")[0].strip().upper() not in RESERVED_NAMES:
            return name
    return "app"


def entrypoint_problem(eps, app: Path) -> str | None:
    """None when every entry point is a dict whose "path" is an existing file (a relative path counts from app/
    and is made absolute in place). A full path outside app/ is fine: as a last resort the build may run the
    creator's own installer, which installs somewhere else. Otherwise the reason in plain words."""
    if not isinstance(eps, list) or not eps:
        return "The build didn't say which program to start."
    for ep in eps:
        p = ep.get("path") if isinstance(ep, dict) else None
        if not isinstance(p, str) or not p.strip():
            return "The build finished, but it didn't say where the program to start is."
        t = Path(p.strip())
        if not t.is_absolute():
            t = app / t
        try:
            found = t.is_file()
            real = t.resolve()
            # Anything else in RepoForge's own folder for this app (src/, app.old ...) is replaced or deleted on
            # the next update, so the program must be in app/ - or (installer route) somewhere else entirely.
            misplaced = not real.is_relative_to(app.resolve()) and real.is_relative_to(app.parent.resolve())
        except (OSError, ValueError, RuntimeError):
            found = misplaced = False
        if not found:
            return f"The build finished, but the program to start wasn't found ({p})."
        if misplaced:
            return f"The build finished, but the program to start was left outside the install folder ({p})."
        if t != Path(p):
            ep["path"] = str(t)
    return None


# A Windows "job object" holds the build agent and everything it starts, so Cancel can stop the whole group at
# once - also helpers whose parent already exited, which taskkill /T can't find any more.
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # own copy, so these argtypes don't change pid_alive
_k32.CreateJobObjectW.restype = ctypes.c_void_p
_k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
_k32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
_k32.TerminateJobObject.argtypes = (ctypes.c_void_p, ctypes.c_uint)
_k32.CloseHandle.argtypes = (ctypes.c_void_p,)
_k32.SetInformationJobObject.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
_ntdll = ctypes.WinDLL("ntdll")
_ntdll.NtResumeProcess.argtypes = (ctypes.c_void_p,)
_ntdll.NtResumeProcess.restype = ctypes.c_long
CREATE_SUSPENDED = 0x4  # started frozen, put in the kill group, then let go: nothing it starts can slip out


class _JobBasicLimits(ctypes.Structure):  # JOBOBJECT_BASIC_LIMIT_INFORMATION
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32)]


class _JobAccounting(ctypes.Structure):  # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
    _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", ctypes.c_uint32), ("TotalProcesses", ctypes.c_uint32),
                ("ActiveProcesses", ctypes.c_uint32), ("TotalTerminatedProcesses", ctypes.c_uint32)]


_k32.QueryInformationJobObject.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
                                           ctypes.c_void_p)


class _JobExtLimits(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    _fields_ = [("BasicLimitInformation", _JobBasicLimits), ("IoInfo", ctypes.c_ulonglong * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def set_kill_on_close(group, on: bool) -> bool:
    """Turn JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE on/off: when on, the whole group dies once its last handle closes -
    also when RepoForge itself dies or is killed. Never raises."""
    try:
        info = _JobExtLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000 if on else 0
        return bool(_k32.SetInformationJobObject(group, 9, ctypes.byref(info), ctypes.sizeof(info)))  # 9 = extended
    except Exception:  # noqa: BLE001
        return False


def new_kill_group(proc):
    """-> a job object handle holding proc (its later children join it too), or None. Never raises.
    The group dies with RepoForge (kill on close); Job.run turns that off before a normal end closes the handle."""
    try:
        h = getattr(proc, "_handle", None)  # Popen's own process handle (no PID-reuse race)
        if h is None:
            return None
        group = _k32.CreateJobObjectW(None, None)
        if not group:
            return None
        set_kill_on_close(group, True)  # best effort: without it, Cancel still stops the group
        if _k32.AssignProcessToJobObject(group, int(h)):
            return group
        _k32.CloseHandle(group)
    except Exception:  # noqa: BLE001
        pass
    return None


def ensure_bin_on_path():
    BIN.mkdir(parents=True, exist_ok=True)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_WRITE) as k:
        try:
            cur, typ = winreg.QueryValueEx(k, "Path")
        except OSError:
            cur, typ = "", winreg.REG_EXPAND_SZ
        parts = [p for p in cur.split(";") if p]
        if any(os.path.normcase(p.rstrip("\\")) == os.path.normcase(str(BIN)) for p in parts):
            return
        winreg.SetValueEx(k, "Path", 0, typ if typ in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else winreg.REG_EXPAND_SZ,
                          ";".join(parts + [str(BIN)]))
    # Tell Explorer so newly opened terminals see it.
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)


REGISTRY_WARNINGS = []  # messages about a broken registry file, shown by the window (App.refresh)


def _registry_bytes() -> bytes | None:
    try:
        return REGISTRY.read_bytes()
    except FileNotFoundError:
        return None


def load_registry(_again: bool = True) -> dict:
    try:
        data = _registry_bytes()
    except OSError:
        time.sleep(0.5)  # e.g. another RepoForge window is just replacing it
        data = _registry_bytes()  # still failing: raise, never act as if no apps are installed
    if data is None:
        return {}
    try:
        reg = json.loads(data.decode("utf-8-sig"))  # -sig: some editors save a BOM, the content is still fine
        if isinstance(reg, dict):
            return reg
    except ValueError:  # bad JSON, or not UTF-8 text at all (UnicodeDecodeError)
        pass
    # Unreadable: keep it aside instead of letting the next save overwrite the whole list.
    kept = REGISTRY.with_name(f"{REGISTRY.name}.broken-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}")
    try:
        os.replace(REGISTRY, kept)
    except FileNotFoundError:
        if _again:  # another window moved it aside (or saved a new list) just now: read what is there now
            return load_registry(_again=False)
        raise RuntimeError(f"The app list {REGISTRY} keeps changing while it is read - try again.") from None
    except OSError:
        raise RuntimeError(f"The app list {REGISTRY} is damaged and couldn't be moved aside.") from None
    msg = (f"warning: the app list {REGISTRY} was damaged. It was kept as {kept} and RepoForge starts with an "
           "empty list; apps installed before still have their folders under the apps folder.")
    REGISTRY_WARNINGS.append(msg)
    if sys.stderr:
        print(msg, file=sys.stderr)
    return {}


def save_registry(reg: dict):
    """Write the whole file to a temp file first, then swap it in: a crash or full disk never leaves half a file."""
    APPS.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_name(f"{REGISTRY.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(reg, indent=2))
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, REGISTRY)
                break
            except PermissionError:  # a reader in another window has it open for a moment
                if attempt == 4:
                    raise
                time.sleep(0.2)
    finally:
        tmp.unlink(missing_ok=True)


def launch(path: str, kind: str):
    p = Path(path)
    if kind == "cli":
        # A CLI has no window of its own: open a terminal where the command is ready to use.
        subprocess.Popen(["cmd", "/k", f'cd /d "{p.parent}" && echo Ready: "{p.name}" --help'],
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        os.startfile(str(p), cwd=str(p.parent))


# --------------------------------------------------------------------------- build agent
PROMPT = """You are RepoForge's build agent, running unattended on the user's Windows PC.
Nobody is watching: never ask questions, make every decision yourself, and keep going until it works or is truly impossible.
IMPORTANT: this is a one-shot session that ends the moment you stop, killing anything still running. Run every
command (winget installs, builds, tests) in the FOREGROUND and wait for it; never use background tasks, never
"wait for a notification". Long builds are fine - commands may run up to an hour.

Project: https://github.com/{owner}/{repo}
Description: {desc}
Source is already cloned at: {src}   (ref {ref}, commit {sha})
Install folder (empty, yours): {app}
Result file you MUST write as your very last step: {result}

GOAL: build this project from its original source code and install the working program into the install folder,
so the user can just run it.

1. Learn the official build: README / BUILDING / docs, and .github/workflows (Windows jobs are the best guide).
2. Install any missing toolchain yourself with winget. Always pass: --silent --accept-package-agreements --accept-source-agreements
   Examples: Rustlang.Rustup, GoLang.Go, OpenJS.NodeJS.LTS, Microsoft.DotNet.SDK.8 (or the version the project targets),
   Kitware.CMake, Ninja-build.Ninja, astral-sh.uv, Python.Python.3.12,
   Microsoft.VisualStudio.2022.BuildTools with --override "--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended".
   After a winget install your shell does not have the new PATH: re-read PATH from the registry
   (HKCU\\Environment + HKLM\\...\\Session Manager\\Environment) or call the tool by its full path.
3. Build an optimized release build the way the project intends (respect lockfiles).
4. Put a self-contained runnable copy in the install folder: the exe plus the DLLs/assets it needs.
   Python projects: create a venv inside the install folder with uv and install the project into it.
   Node/Electron apps: package them, or install their dependencies inside the install folder.
   If the program needs a launcher, write a .cmd in the install folder (for GUI apps use pythonw / start "" so no console stays open).
   In a .cmd, give paths relative to the .cmd file (%~dp0): cmd.exe misreads letters like é in a full path.
5. Smoke-test: run CLIs with --help or --version; start GUI apps for a few seconds, then kill them. Leave nothing running.
6. Only if building from source is genuinely impossible on Windows after real effort (macOS/Linux-only, closed SDK...),
   fall back to the creator's own Windows release asset from https://github.com/{owner}/{repo}/releases (never any other site).
   Prefer a portable zip extracted into the install folder; only if there is nothing but an installer, run it silently
   and record how to uninstall it.
7. Do not touch anything except the source folder, the install folder and toolchain installs. No system setting changes,
   no git commits or pushes, no desktop shortcuts (RepoForge makes those).

Write {result} as JSON:
{{"status": "ok" or "failed",
  "method": "source" or "release",
  "name": "human-friendly app name",
  "version": "version string",
  "kind": "gui" or "cli" or "other",
  "entrypoints": [{{"name": "display name", "path": "absolute path to .exe/.cmd/.bat", "kind": "gui" or "cli", "command": "short command name (cli only)"}}],
  "uninstall": "command that uninstalls it if an installer put files outside the install folder, else empty",
  "notes": "one or two plain sentences for a non-technical user: how to use it and anything they must know"}}
RepoForge itself puts CLI commands on the PATH and creates Start menu / desktop shortcuts for GUI apps,
so the notes should not tell the user to do either.
If it failed, use status "failed" and explain why in "notes" in plain words.
"""


class Job:
    """One install/update run. Talks to the UI only through `post`."""

    def __init__(self, url_text: str, post):
        self.url_text = url_text
        self.post = post
        self.proc = None
        self.cancelled = False
        self.logfile = None
        self.lock = None
        self._kill_lock = threading.Lock()  # cancel() vs. starting the build agent
        self._kill_group = None

    def log(self, text: str):
        self.post(("log", text))
        if self.logfile:
            self.logfile.write(text + "\n")
            self.logfile.flush()

    def status(self, text: str):
        self.post(("status", text))
        self.log(f"== {text}")

    def cancel(self):
        with self._kill_lock:  # after this, _spawn starts nothing new
            self.cancelled = True
            proc = self.proc
            if self._kill_group:
                _k32.TerminateJobObject(self._kill_group, 1)  # the whole group, orphans included
        if proc and proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=NO_WINDOW)

    def _stop_group(self, wait_s: float = 5.0):
        """A failed build: stop everything it left running (e.g. a test run of the program that keeps files in
        app/ open), without counting as a Cancel, and wait up to wait_s for it to end. Never raises."""
        try:
            with self._kill_lock:
                group = self._kill_group  # closed only by run(), on this same thread, after _run
                if group:
                    _k32.TerminateJobObject(group, 1)
            if not group:
                return
            end = time.monotonic() + wait_s
            while time.monotonic() < end:
                info = _JobAccounting()
                if (not _k32.QueryInformationJobObject(group, 1, ctypes.byref(info), ctypes.sizeof(info), None)
                        or info.ActiveProcesses == 0):
                    return
                time.sleep(0.1)
        except Exception:  # noqa: BLE001 - _restore_old still retries files that are in use
            pass

    def _git(self, *args, cwd=None) -> str:
        """Run git in this job's kill group, so Cancel (or closing the window) stops it at once, with all the
        helpers it started. Raises Cancelled when that happened."""
        _RUN_JOB.job = self
        try:
            return run([*GIT, *args], cwd=cwd)
        finally:
            _RUN_JOB.job = None

    def _spawn(self, cmd, **kw):
        """Start git or the build agent in this job's kill group -> the process, or None when already cancelled."""
        with self._kill_lock:
            if self.cancelled:
                return None
            # Started frozen, so it can't start a helper (e.g. git.exe's wrapper starting the real git) before
            # it is in the group; it is let go right after.
            kw["creationflags"] = (kw.get("creationflags") or 0) | CREATE_SUSPENDED
            self.proc = subprocess.Popen(cmd, **kw)
            h = getattr(self.proc, "_handle", None)
            if not self._kill_group:
                self._kill_group = new_kill_group(self.proc)
            elif h is not None:  # a follow-up run of the agent joins the same group
                _k32.AssignProcessToJobObject(self._kill_group, int(h))  # if refused, taskkill /T still covers it
            if h is not None and _ntdll.NtResumeProcess(int(h)) != 0:
                try:  # it can't be let go: don't leave a frozen process behind
                    self.proc.kill()
                except OSError:
                    pass
                raise OSError(f"couldn't start {Path(str(cmd[0])).name}")
            return self.proc

    def run(self):
        ended_normally = False  # set only once _run returned (not on an error or Ctrl+C)
        result_ok = False  # only a good install may leave something running
        try:
            result = self._run()
            ended_normally = True
            result_ok = bool(result and result[0])
            self.post(("done", *result))
        except Exception as e:  # noqa: BLE001 - anything that goes wrong is reported to the user
            try:
                self.log(f"ERROR: {e}")
            except Exception:  # noqa: BLE001 - e.g. the disk is full: the message below must still reach the window
                pass
            self.post(("done", False, f"Failed: {e}", None))
        finally:
            with self._kill_lock:
                if self._kill_group:
                    if ended_normally and result_ok and not self.cancelled:
                        # A good install: closing the handle must not kill what the build left running on purpose.
                        set_kill_on_close(self._kill_group, False)
                    else:  # a failed build, an error, Ctrl+C or Cancel: nothing of the build may keep running
                        _k32.TerminateJobObject(self._kill_group, 1)
                    _k32.CloseHandle(self._kill_group)
                    self._kill_group = None
            if self.lock:
                self.lock.unlink(missing_ok=True)
            if self.logfile:
                self.logfile.close()
        return result_ok

    def _restore_old(self, root: Path):
        """Throw away the new attempt in root/app and put root/app.old back. Raises if it could not."""
        app, old, marker = root / "app", root / "app.old", root / "update-in-progress"
        failed = None
        if app.exists():
            # First move the failed attempt aside (one quick rename), so app.old is back right away even when
            # deleting the attempt is slow or the program is closed meanwhile. The copy is deleted afterwards.
            stamp = time.strftime('%Y%m%d-%H%M%S')
            failed, n = root / f"app.failed-{stamp}", 1
            while failed.exists():
                failed, n = root / f"app.failed-{stamp}-{n}", n + 1
            try:
                app.rename(failed)
            except OSError:
                failed = None
                rmtree(app)  # it can't be moved (a file in it is open): delete it instead; raises if it can't
        if old.exists():
            old.rename(app)
        marker.unlink(missing_ok=True)
        if failed:
            try:
                shutil.rmtree(failed, ignore_errors=True)
            except Exception:  # noqa: BLE001 - best effort; _heal clears app.failed-* next time
                pass
            if failed.exists():
                self.log(f"Some files of the failed attempt were in use - moved them to {failed}.")

    def _heal(self, root: Path, key: str) -> str | None:
        """Fix what an earlier run left behind in root. Call only while holding root's lock.
        -> None when fine, or the message to show when the previous install couldn't be put back."""
        app, old, marker = root / "app", root / "app.old", root / "update-in-progress"
        # The marker exists only while app.old holds the last good install and app/ is being rebuilt.
        # Without it, a full app/ is the good install and app.old is a leftover a good run couldn't delete.
        app_full = app.is_dir() and any(app.iterdir())
        if old.exists() and (marker.exists() or not app_full):
            self.log("Found the previous install left in app.old by an earlier run that didn't finish - "
                     "putting it back.")
            try:
                self._restore_old(root)
            except Exception as e:  # noqa: BLE001
                return (f"Couldn't put the previous install of {key} back from {old}: {e} "
                        "The previous install is still safe there. Close anything using it and try again.")
        elif old.exists():
            self.log("Found an old copy left in app.old by an earlier good update - clearing it "
                     "(the installed app is not touched).")
            try:
                old.rename(root / f"app.failed-{time.strftime('%Y%m%d-%H%M%S')}-old")
            except OSError:
                pass  # the delete right before the update moves app/ tries again
        for leftover in root.glob("app.failed-*"):  # failed attempts moved aside earlier; best effort
            shutil.rmtree(leftover, ignore_errors=True)
        return None

    def _run(self):
        parsed = parse_url(self.url_text)
        if not parsed:
            return False, "That doesn't look like a GitHub project link.", None
        owner, repo, ref = parsed

        # Put back a previous install left in app.old by a run that didn't finish - BEFORE anything that
        # can fail (GitHub lookup, download, CLI check), so a failing step never keeps the app missing.
        # The link's own spelling finds the folder (Windows folder names ignore upper/lower case).
        early_root = APPS / owner / repo
        if all(n.strip(".") for n in (owner, repo)) and early_root.is_dir():
            self.lock = take_lock(early_root)
            if not self.lock:
                return False, f"{owner}/{repo} is already being installed - wait for that one to finish.", None
            problem = self._heal(early_root, f"{owner}/{repo}")
            if problem:
                return False, problem, None

        self.status(f"Looking up {owner}/{repo} on GitHub...")
        info = gh_api(f"/repos/{owner}/{repo}")
        if not info:
            return False, f"GitHub has no public project called {owner}/{repo}.", None
        owner, repo = info["owner"]["login"], info["name"]  # canonical spelling
        key = f"{owner}/{repo}"
        ref_kind = "pinned"
        if not ref:
            ref, ref_kind = latest_ref(owner, repo, info)

        LOGS.mkdir(exist_ok=True)
        self.logfile = open(LOGS / f"{owner}_{repo}_{time.strftime('%Y%m%d-%H%M%S')}.log", "w", encoding="utf-8")
        self.log(f"{key} - {info.get('description') or ''}")
        self.log(f"Version: {ref} ({ref_kind})")

        root = APPS / owner / repo
        src, app, old = root / "src", root / "app", root / "app.old"
        marker = root / "update-in-progress"
        result_file = root / "repoforge-result.json"
        root.mkdir(parents=True, exist_ok=True)
        if self.lock and not _same_dir(self.lock.parent, root):
            # The link used an old project name: GitHub's real name is a different folder.
            self.lock.unlink(missing_ok=True)
            self.lock = None
        if not self.lock:
            self.lock = take_lock(root)
            if not self.lock:
                return False, f"{key} is already being installed - wait for that one to finish.", None
            problem = self._heal(root, key)
            if problem:
                return False, problem, None

        # Fresh shallow clone of exactly that version.
        self.status(f"Downloading source ({ref})...")
        rmtree(src)
        url = f"https://github.com/{owner}/{repo}.git"
        try:
            if re.fullmatch(r"[0-9a-f]{7,40}", ref):
                self._git("init", "-q", str(src))
                self._git("remote", "add", "origin", url, cwd=src)
                self._git("fetch", "-q", "--depth", "1", "origin", ref, cwd=src)
                self._git("checkout", "-q", "FETCH_HEAD", cwd=src)
                self._git("submodule", "update", "-q", "--init", "--recursive", "--depth", "1", cwd=src)
            else:
                self._git("clone", "-q", "--depth", "1", "--branch", ref, "--recurse-submodules",
                          "--shallow-submodules", url, str(src))
            sha = self._git("rev-parse", "HEAD", cwd=src)
        except Cancelled:
            return False, "Cancelled.", None
        self.log(f"Commit: {sha}")
        if self.cancelled:
            return False, "Cancelled.", None

        # Find the CLI before touching the installed copy, so nothing is moved if it is missing.
        exe = find_claude_exe()
        if not exe:
            return False, ("Claude Code CLI not found. RepoForge needs the standalone Claude Code CLI "
                           "(claude.exe). It looked in %USERPROFILE%\\.local\\bin, on the PATH, and in "
                           "the old Claude desktop app folders. Install Claude Code, then try again."), None

        def try_restore_old() -> bool:
            """restore_old that never raises: on failure the old copy stays safe in app.old."""
            try:
                self._restore_old(root)
                return True
            except Exception as e:  # noqa: BLE001
                self.log(f"Couldn't put the previous install back yet ({e}). It is still safe in {old}, "
                         "and RepoForge will put it back on the next try.")
                return False

        # Keep the previous install until the new one is proven to work.
        # (Clears a stale app.old the heal step couldn't move, right before app/ is moved there.)
        try:
            rmtree(old)
        except (RuntimeError, OSError) as e:
            return False, (f"Couldn't clear the old copy in {old}: {e} The installed app was not touched."), None
        if app.exists():
            try:
                marker.write_text("RepoForge is updating this app. app.old holds the last good install.")
                app.rename(old)
            except OSError:
                marker.unlink(missing_ok=True)
                return False, "The installed version is still running - close it and try again.", None
        try:
            app.mkdir(parents=True)
            result_file.unlink(missing_ok=True)

            prompt = PROMPT.format(owner=owner, repo=repo, desc=info.get("description") or "(none)", src=src,
                                   ref=ref, sha=sha, app=app, result=result_file)
            self.status("Building from source (this can take a while)...")
            base = [exe, "--output-format", "stream-json", "--verbose",
                    "--model", CFG["model"], "--permission-mode", "bypassPermissions",
                    "--add-dir", str(root), "--strict-mcp-config",
                    # A -p session ends the moment the agent stops talking, killing anything it left
                    # running in the background - so builds must run in the foreground.
                    "--disallowedTools", "Monitor,ScheduleWakeup,CronCreate,Agent"]
            env = dict(os.environ, CLAUDE_CODE_DISABLE_BACKGROUND_TASKS="1",
                       BASH_DEFAULT_TIMEOUT_MS="3600000", BASH_MAX_TIMEOUT_MS="3600000",
                       # MSBuild/Roslyn helper processes otherwise linger ~15 min and lock the source folder.
                       MSBUILDDISABLENODEREUSE="1", UseSharedCompilation="false")

            res = None
            self.session = None
            message = prompt
            for attempt in range(1 + CFG["max_nudges"]):
                cmd = base + (["--resume", self.session] if self.session else []) + ["-p", message]
                if not self._spawn(cmd, cwd=src, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, env=env,
                                   text=True, encoding="utf-8", errors="replace"):
                    break  # cancelled before the agent started
                for line in self.proc.stdout:
                    self._agent_event(line)
                self.proc.wait()
                try:
                    res = json.loads(result_file.read_text(encoding="utf-8-sig"))  # PowerShell 5 writes a BOM
                except (OSError, ValueError):
                    res = None
                if self.cancelled or res or not self.session:
                    break
                # Stopped without writing the result file: send it back to finish the job.
                self.status(f"Build agent stopped early - telling it to keep going ({attempt + 1})...")
                message = (f"You stopped before finishing, and your session ends whenever you stop, which kills "
                           f"any background process. Check what state the build is in, run anything still needed "
                           f"in the FOREGROUND (never in the background, never wait for notifications), finish the "
                           f"install, and write {result_file}.")
            if not isinstance(res, dict):
                res = None
            ep_problem = None
            if res and not self.cancelled and res.get("status") == "ok":
                ep_problem = entrypoint_problem(res.get("entrypoints"), app)
                if ep_problem:
                    self.log(f"The build said ok, but: {ep_problem}")
            build_ok = bool(res) and not self.cancelled and res.get("status") == "ok" and not ep_problem
        except Exception:
            # Anything that broke mid-build: put the working install back, then let the user see the
            # original error (a failed restore is only logged, so it can't hide the real cause).
            # First stop the build agent and everything it started, so nothing keeps writing into the folder
            # while (and after) the previous install is put back.
            try:
                self.cancel()
                if self.proc:
                    self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001 - _restore_old still retries files that are in use
                pass
            try:
                self.log("Something went wrong during the build - putting the previous install back.")
            except Exception:  # noqa: BLE001 - e.g. the disk is full
                pass
            try_restore_old()
            raise
        if not build_ok:
            why = ("Cancelled." if self.cancelled else ep_problem or (res or {}).get("notes")
                   or "The build didn't finish.")
            msg = f"Couldn't install {key}: {why}"
            self._stop_group()  # nothing the failed build left running may block putting the old version back
            if not try_restore_old():
                msg += (" The previous version couldn't be put back yet because files were in use. "
                        f"It is safe in {old} and will be put back on the next try.")
            return False, msg, None

        marker.unlink(missing_ok=True)  # the new build is proven good: app.old is no longer needed
        try:
            rmtree(old)
        except (RuntimeError, OSError):
            # Best effort only: the new install is good, so a locked old file must not fail the job.
            try:
                old.rename(root / f"app.failed-{time.strftime('%Y%m%d-%H%M%S')}-old")
            except OSError:
                self.log(f"Couldn't delete the previous version in {old} because some of its files are in "
                         "use. The new version is installed fine; RepoForge clears the old copy next time.")
        # Keep src so failures/updates can be inspected; it is replaced on every update anyway.
        self.status("Adding shortcuts...")
        reg = load_registry()
        prev = reg.get(key, {})
        try:
            remove_integration(prev)
        except Exception as e:  # noqa: BLE001 - an old shortcut that can't be removed must not lose the install
            self.log(f"warning: couldn't remove an old shortcut or command: {e}")
        entry = {"key": key, "owner": owner, "repo": repo, "ref": ref, "ref_kind": ref_kind,
                 "default_branch": info["default_branch"], "commit": sha, "dir": str(root),
                 "installed_at": time.strftime("%Y-%m-%d %H:%M"), **res, "shortcuts": [], "shims": []}
        # Keep the uninstall command of an earlier installer-route install: what that installer put on this PC
        # is still there, even when this build went into app/ or used a different installer.
        cmds = uninstall_cmds(res.get("uninstall"))
        cmds += [c for c in uninstall_cmds(prev.get("uninstall")) if c not in cmds]
        entry["uninstall"] = cmds[0] if len(cmds) == 1 else (cmds or "")
        # Each shortcut / command on its own: one that can't be made is only a warning, and the
        # registry is always saved, so the good build isn't rebuilt by the next "Update all".
        some_failed = False
        for ep in res["entrypoints"]:
            target = Path(ep["path"])
            if not target.exists():
                self.log(f"warning: entry point missing: {target}")
                continue
            if ep.get("kind", res.get("kind")) == "cli":
                try:
                    ensure_bin_on_path()
                    cmd_name = ep.get("command") if isinstance(ep.get("command"), str) else ""
                    name = safe_name(re.sub(r"[^\w.-]", "", cmd_name or target.stem), repo)
                    shim = write_shim(target, name)
                    entry["shims"].append(str(shim))
                    self.log(f"Command available in new terminals: {name}")
                except Exception as e:  # noqa: BLE001
                    some_failed = True
                    self.log(f"warning: couldn't make the command for {target.name}: {e}")
            else:
                label = safe_name(ep.get("name") or res.get("name"), repo)
                lnks = [START_MENU / f"{label}.lnk"]
                if CFG["desktop_shortcuts"]:
                    lnks.append(desktop_dir() / f"{label}.lnk")
                for lnk in lnks:
                    try:
                        make_shortcut(lnk, str(target), str(target.parent), f"{key} (RepoForge)")
                        entry["shortcuts"].append(str(lnk))
                    except Exception as e:  # noqa: BLE001
                        some_failed = True
                        self.log(f"warning: couldn't make the shortcut {lnk}: {e}")
        reg[key] = entry
        save_registry(reg)
        how = "built from source" if res.get("method") == "source" else "official release (source build wasn't possible)"
        if some_failed:
            how += " (some shortcuts couldn't be made - see the log)"
        return True, f"Installed {res.get('name') or repo} {res.get('version', '')} - {how}. {res.get('notes', '')}", entry

    def _agent_event(self, line: str):
        try:
            d = json.loads(line)
        except ValueError:
            if line.strip():
                self.log(line.rstrip())
            return
        if d.get("type") == "assistant":
            for b in d.get("message", {}).get("content", []):
                if b.get("type") == "text" and b["text"].strip():
                    self.log(b["text"].strip())
                elif b.get("type") == "tool_use":
                    inp = b.get("input", {})
                    detail = inp.get("command") or inp.get("file_path") or inp.get("url") or inp.get("pattern") or ""
                    self.log(f"  > {b['name']}: {str(detail).splitlines()[0][:200] if detail else ''}")
                    if b["name"] in ("Bash", "PowerShell") and "winget install" in str(detail):
                        self.post(("status", "Installing build tools (click Yes if Windows asks)..."))
        elif d.get("type") == "system" and d.get("session_id"):
            self.session = d["session_id"]
        elif d.get("type") == "result":
            self.session = d.get("session_id") or self.session
            if d.get("is_error"):
                self.log(f"Claude: {d.get('result')}")
            self.log(f"(agent finished in {d.get('duration_ms', 0) // 60000} min)")


def write_shim(target: Path, name: str) -> Path:
    """bin/<name>.cmd that runs target. cmd.exe reads a .cmd file in the console's code page (OEM), not UTF-8, so a
    path with letters like é in it (often the user name) must be written that way. A target in the apps folder is
    found from the .cmd file's own folder (%~dp0), so the apps folder's path isn't in the file at all."""
    shim = BIN / f"{name}.cmd"
    try:
        rel = "..\\" + str(Path(target).relative_to(APPS))
        # cmd.exe bug: for "name" typed in quotes and found through PATH, %~dp0 is the current folder.
        # Then look the shim up on PATH by the whole typed name (%~nx0, which keeps dots), with and without .cmd,
        # and take the folder that really holds the target.
        body = ('@echo off\r\nsetlocal\r\nset "here=%~dp0"\r\n'
                f'if not exist "%here%{rel}" for %%I in ("%~nx0.cmd" "%~nx0") do '
                f'if not "%%~dp$PATH:I"=="" if exist "%%~dp$PATH:I{rel}" set "here=%%~dp$PATH:I"\r\n'
                f'endlocal & "%here%{rel}" %*\r\n')
    except ValueError:
        body = f'@echo off\r\n"{target}" %*\r\n'  # e.g. put elsewhere by the creator's installer
    BIN.mkdir(parents=True, exist_ok=True)
    # shortcut: a letter the OEM code page lacks raises here (the caller logs it); use a short 8.3 path if that bites
    shim.write_bytes(body.encode("oem"))
    return shim


def remove_integration(entry: dict):
    for p in entry.get("shortcuts", []) + entry.get("shims", []):
        Path(p).unlink(missing_ok=True)


BUSY_MSG = "This app is busy (being installed or repaired) - try again in a moment."


UNINSTALLER_WAIT_S = 300  # a hidden console uninstaller waiting for a key press would otherwise wait forever
# Exit codes that mean "done": 0; 3010 / 1641 = done, Windows wants a restart; 1605 = (msiexec) not installed any more.
UNINSTALL_DONE_CODES = (0, 3010, 1641, 1605)
UNINSTALL_SETTLE_S = 20  # how long the program's files may take to go away after its uninstaller returned


def uninstall_cmds(value) -> list[str]:
    """The creator's uninstall command(s) saved for an app: one text, or a list after an update added one."""
    vals = value if isinstance(value, list) else [value]
    return [v.strip() for v in vals if isinstance(v, str) and v.strip()]


def run_uninstaller(cmd: str) -> bool:
    """Run the creator's uninstaller -> True only when it finished fine. Never raises. When it takes too long
    (e.g. a hidden console waiting for a key press), it is stopped with everything it started."""
    try:
        p = subprocess.Popen(cmd, shell=True, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except OSError:
        return False
    try:
        return p.wait(timeout=UNINSTALLER_WAIT_S) in UNINSTALL_DONE_CODES
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True,
                           creationflags=NO_WINDOW)
            p.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        return False
    except Exception:  # noqa: BLE001
        return False


def uninstaller_gone(cmd: str, entry: dict) -> bool:
    """After the uninstaller failed: True when the program is clearly gone from this PC already - the file the
    command starts no longer exists (e.g. removed in Windows Settings, or it deleted itself), or every entry point
    outside RepoForge's folder is gone. Never raises."""
    try:
        m = (re.match(r'\s*"([^"]+)"', cmd)
             or re.match(r'(?i)\s*((?:[a-z]:|\\\\|%)[^"]*?\.(?:exe|cmd|bat|com))(?=\s|$)', cmd))
        if m and re.search(r"[\\/]", m.group(1)) and not Path(os.path.expandvars(m.group(1))).exists():
            return True
        outside = entrypoints_outside(entry)
        return bool(outside) and not any(p.exists() for p in outside)
    except Exception:  # noqa: BLE001
        return False


def entrypoints_outside(entry: dict) -> list[Path]:
    """Entry points outside RepoForge's own folder for the app: put there by the creator's installer."""
    root = Path(entry["dir"]).resolve()
    return [Path(ep["path"]) for ep in entry.get("entrypoints") or []
            if isinstance(ep, dict) and isinstance(ep.get("path"), str) and Path(ep["path"]).is_absolute()
            and not Path(ep["path"]).resolve().is_relative_to(root)]


def uninstall(key: str) -> str:
    """Remove an installed app (slow: call it from a worker thread). -> the message to show.
    Raises RuntimeError with a plain message when not everything could be removed; the registry entry is then
    kept (with only what is still left), so Uninstall can simply be clicked again."""
    entry = load_registry().get(key)
    if not entry:
        return "Not installed."
    root = Path(entry["dir"])
    lock = None
    if root.is_dir():
        lock = take_lock(root)  # not while an install, update or repair works in this folder
        if not lock:
            raise RuntimeError(BUSY_MSG)
    left = []  # shortcuts / commands that couldn't be removed
    files_left = False
    still = []  # uninstall commands that didn't finish: kept, so Uninstall can be clicked again
    gave_up = False  # an uninstaller failed on the last click too: not tried again, so the app can always be removed
    failed_before = bool(entry.get("uninstall_failed"))
    ran_ok = False  # an uninstaller said it finished
    try:
        # Shortcuts and commands first, so they are gone even when some files in the folder are still in use.
        for p in entry.get("shortcuts", []) + entry.get("shims", []):
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                left.append(p)
        for cmd in uninstall_cmds(entry.get("uninstall")):  # the creator's own uninstaller (installer route)
            if run_uninstaller(cmd):
                ran_ok = True
                continue
            if uninstaller_gone(cmd, entry):
                continue  # done: a second Uninstall click doesn't run it again
            if failed_before:
                gave_up = True
            else:
                still.append(cmd)
        if lock:
            try:
                children = list(root.iterdir())
            except OSError:
                children, files_left = [], True
            for child in children:  # each on its own: delete as much as possible even when one part is in use
                if child.name == lock.name:
                    continue  # our own lock goes last
                try:
                    if child.is_dir():
                        rmtree(child)
                    else:
                        try:
                            child.unlink()
                        except PermissionError:
                            os.chmod(child, stat.S_IWRITE)
                            child.unlink()
                except (OSError, RuntimeError):
                    files_left = True
            if not files_left:
                lock.unlink(missing_ok=True)
                lock = None
                try:
                    root.rmdir()
                except OSError:
                    files_left = root.exists()
    finally:
        if lock:
            lock.unlink(missing_ok=True)
    reg = load_registry()  # read again: another RepoForge window may have changed it meanwhile
    gave_up_msg = ("The app's own uninstaller failed again, so RepoForge stopped trying it. If the program still "
                   "shows up in Windows Settings > Apps, remove it there.") if gave_up else ""
    if files_left or left or still:
        if key in reg:
            reg[key] = dict(reg[key], shortcuts=[p for p in entry.get("shortcuts", []) if p in left],
                            shims=[p for p in entry.get("shims", []) if p in left],
                            uninstall=still[0] if len(still) == 1 else (still or ""), uninstall_failed=bool(still))
            save_registry(reg)
        msgs = []
        if files_left:
            msgs.append(f"Couldn't delete everything in {root} because some files are in use - "
                        "close the app and click Uninstall again.")
        if still:
            msgs.append("The app's own uninstaller didn't finish, so the program may still be on this PC - "
                        "click Uninstall again to try once more.")
        if left and not msgs:
            msgs.append("Couldn't remove some of its shortcuts or commands because they are in use - "
                        "click Uninstall again in a moment.")
        if gave_up_msg:
            msgs.append(gave_up_msg)
        raise RuntimeError(" ".join(msgs))
    reg.pop(key, None)
    save_registry(reg)
    owner_dir = root.parent
    try:
        if owner_dir.exists() and not any(owner_dir.iterdir()):
            owner_dir.rmdir()
    except OSError:
        pass  # an empty owner folder left behind is harmless
    # Installer route: what the creator's installer put elsewhere may still be there - with no uninstall command
    # saved, when that command's file was already gone, or when an uninstaller said "done" and then didn't remove it.
    try:
        outside = entrypoints_outside(entry)
    except Exception:  # noqa: BLE001 - a broken entry: nothing more to say
        outside = []
    still_there = [p for p in outside if p.exists()]
    if still_there and ran_ok:
        # Many uninstallers copy themselves to a temp folder, return at once and remove the files a moment later.
        end = time.monotonic() + UNINSTALL_SETTLE_S
        while still_there and time.monotonic() < end:
            time.sleep(0.2)
            still_there = [p for p in still_there if p.exists()]
    if still_there:
        return (f"Removed RepoForge's copy of {key}, but the program its own installer put on this PC is still "
                f"there ({still_there[0]}). Remove it in Windows Settings > Apps."
                + (f" {gave_up_msg}" if gave_up_msg else ""))
    return f"Removed {key}." + (f" {gave_up_msg}" if gave_up_msg else "")


def update_available(entry: dict) -> str | None:
    """-> the newer ref to install, or None if up to date."""
    info = gh_api(f"/repos/{entry['owner']}/{entry['repo']}")
    ref, kind = latest_ref(entry["owner"], entry["repo"], info)
    if kind == "release":
        return ref if ref != entry["ref"] else None
    head = remote_head(entry["owner"], entry["repo"], ref)
    return ref if head and head != entry["commit"] else None


def needs_heal(root: Path) -> bool:
    """True when an earlier run left root half-updated, so app.old must be put back: app.old plus the marker,
    or app.old with app/ missing or empty (the same rule Job._heal uses)."""
    app = root / "app"
    try:
        if not (root / "app.old").exists():
            return False  # nothing to put back (a lone marker means app/ is the good install)
        return (root / "update-in-progress").exists() or not (app.is_dir() and any(app.iterdir()))
    except OSError:
        return False


_HEAL_PASS = threading.Lock()  # one repair pass at a time in this window (they share the folder locks)


def heal_registered(reg: dict, log) -> int:
    """Put back any installed app an earlier run left half-updated. Needs no internet, so it also runs
    when the GitHub checks fail. Each app is healed only while holding its lock; problems are logged.
    -> how many apps still need repairing (an app busy in another RepoForge window is not counted)."""
    unrepaired = 0
    for key, e in reg.items():
        try:
            root = Path(e["dir"])
            if not root.is_dir() or not needs_heal(root):
                continue
        except Exception:  # noqa: BLE001 - a broken registry entry is just skipped
            continue
        lock = None
        try:
            lock = take_lock(root)
            if not lock:
                log(f"{key}: left half-updated, but it is being installed right now - not touching it.")
                continue
            log(f"{key}: an earlier update didn't finish - repairing it.")
            problem = Job(f"https://github.com/{key}", lambda m: log(m[1]) if m[0] == "log" else None)._heal(root, key)
            still_broken = bool(problem) or needs_heal(root)
            log(problem or (f"{key}: couldn't repair it yet - RepoForge tries again next time." if still_broken
                            else f"{key}: the installed version is back."))
        except Exception as ex:  # noqa: BLE001
            still_broken = True
            log(f"{key}: couldn't repair it ({ex}).")
        finally:
            if lock:
                try:
                    lock.unlink(missing_ok=True)
                except OSError:
                    pass  # a lock with a dead owner is cleared by the next run
        unrepaired += still_broken
    return unrepaired


# --------------------------------------------------------------------------- UI
BG, PANEL, FG, DIM, ACCENT = "#16181d", "#1f232b", "#e6e6e6", "#8a8f98", "#3b82f6"


class App:
    checking = False  # the update checks (and repairs) are running in check_thread
    check_thread = None
    check_cancelled = False  # Cancel was pressed during the checks: start no updates
    uninstalling = False  # an Uninstall is deleting files in uninstall_thread
    uninstall_thread = None
    heals = 0  # repair passes running right now (at start and before update checks); closing waits for them
    _heals_lock = threading.Lock()

    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on scaled displays
        except OSError:
            pass
        self.root = tk.Tk()
        self.root.title("RepoForge")
        k = self.root.winfo_fpixels("1i") / 96  # display scale factor
        w, h = int(900 * k), int(620 * k)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        h = min(h, int(sh * 0.9))
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - int(20 * k))}")
        self.root.configure(bg=BG)
        try:
            self.root.iconbitmap(str(HERE / "repoforge.ico"))
        except tk.TclError:
            pass
        self.q = queue.Queue()
        self.job = None
        self.job_thread = None
        self.closing = False  # the window was closed while a build ran: stopping it, then closing
        self.close_deadline = 0.0
        self.after_note = ""  # shown after the last update of an "Update all" (e.g. checks that failed)

        st = ttk.Style()
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL)
        st.configure("TButton", background=PANEL, foreground=FG, padding=6, borderwidth=0)
        st.map("TButton", background=[("active", "#2b303a"), ("disabled", BG)], foreground=[("disabled", DIM)])
        st.configure("Go.TButton", background=ACCENT, foreground="white", font=("Segoe UI", 11, "bold"))
        st.map("Go.TButton", background=[("active", "#2563eb")])
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=26, borderwidth=0)
        st.configure("Treeview.Heading", background=BG, foreground=DIM, borderwidth=0)
        st.map("Treeview", background=[("selected", "#2b3a55")])
        st.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL, borderwidth=0)

        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=16, pady=(16, 6))
        self.url = tk.StringVar()
        self.entry = tk.Entry(top, textvariable=self.url, font=("Segoe UI", 13), bg=PANEL, fg=FG,
                              insertbackground=FG, relief="flat")
        self.entry.pack(side="left", fill="x", expand=True, ipady=8, padx=(0, 8))
        self.go = ttk.Button(top, text="Install", style="Go.TButton", command=self.start)
        self.go.pack(side="left")
        self.entry.bind("<Return>", lambda e: self.start())
        self.entry.bind("<<Paste>>", lambda e: self.root.after(50, self.auto_start))

        self.status = tk.Label(self.root, text="Paste a GitHub project link - it installs by itself.",
                               bg=BG, fg=DIM, anchor="w", font=("Segoe UI", 10))
        self.status.pack(fill="x", padx=16)
        self.bar = ttk.Progressbar(self.root, mode="determinate")
        self.bar.pack(fill="x", padx=16, pady=(4, 8))

        self.logbox = tk.Text(self.root, height=10, bg=PANEL, fg="#c9d1d9", relief="flat", wrap="word",
                              font=("Cascadia Mono", 9), state="disabled")
        self.logbox.pack(fill="both", expand=True, padx=16)

        tk.Label(self.root, text="Installed", bg=BG, fg=DIM, anchor="w",
                 font=("Segoe UI", 10, "bold")).pack(fill="x", padx=16, pady=(12, 2))
        self.tree = ttk.Treeview(self.root, columns=("ver", "how", "when"), height=6)
        self.tree.heading("#0", text="App")
        self.tree.heading("ver", text="Version")
        self.tree.heading("how", text="From")
        self.tree.heading("when", text="Installed")
        self.tree.column("#0", width=380)
        for c, w in (("ver", 140), ("how", 110), ("when", 140)):
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="x", padx=16)
        self.tree.bind("<Double-1>", lambda e: self.launch_sel())

        btns = tk.Frame(self.root, bg=BG)
        btns.pack(fill="x", padx=16, pady=(6, 16))
        self.btns = []
        for text, fn in (("Open", self.launch_sel), ("Update", self.update_sel), ("Update all", self.update_all),
                         ("Folder", self.folder_sel), ("Uninstall", self.uninstall_sel)):
            b = ttk.Button(btns, text=text, command=fn)
            b.pack(side="left", padx=(0, 6))
            self.btns.append(b)
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="right")

        self.pending = []  # queued update keys for "Update all"
        self.refresh()
        # Put back anything an earlier run left half-updated (e.g. the window was closed mid-update).
        # Installs and update checks started meanwhile wait for it (_HEAL_PASS), so no two touch one folder.
        if self._any_half_updated():
            self.say("Putting back an app whose last update didn't finish...")
            # The thread gets only the queue, not self: a worker thread must never drop the last
            # reference to the Tk window (Tcl aborts when Tk objects are freed from the wrong thread).
            threading.Thread(target=App._startup_heal, args=(self.q,), daemon=True).start()
        self.root.bind("<FocusIn>", lambda e: self.prefill() if e.widget is self.root else None)
        self.root.after(200, self.prefill)
        self.root.after(100, self.pump)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # -- helpers
    def say(self, text: str, color=DIM):
        self.status.config(text=text, fg=color)

    def log(self, text: str):
        self.logbox.config(state="normal")
        self.logbox.insert("end", text + "\n")
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def prefill(self):
        """If a GitHub link is on the clipboard, put it in the box, ready for Enter."""
        if self.job or self.checking or self.closing or self.uninstalling or self.url.get().strip():
            return
        try:
            clip = self.root.clipboard_get().strip()
        except tk.TclError:
            return
        if len(clip) < 300 and parse_url(clip) and "github.com" in clip.lower():
            self.url.set(clip)
            self.say("Link from your clipboard is ready - press Enter to install.", FG)

    def auto_start(self):
        if not self.job and not self.closing and not self.uninstalling and parse_url(self.url.get()):
            self.start()

    def _registry(self) -> dict | None:
        """The app list, or None when it can't be read (the reason is then in the log and the status line)."""
        try:
            reg = load_registry()
        except (OSError, RuntimeError, ValueError) as ex:
            reg = None
            self.log(f"Couldn't read the list of installed apps ({ex})")
            self.say("Couldn't read the list of installed apps - see the log.", "#f87171")
        while REGISTRY_WARNINGS:
            self.log(REGISTRY_WARNINGS.pop(0))
            self.say("The list of installed apps was damaged - see the log.", FG)
        return reg

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        reg = self._registry() or {}
        for key, e in sorted(reg.items(), key=lambda kv: str(kv[1].get("name") or kv[0]).lower()):
            self.tree.insert("", "end", iid=key, text=f"{e.get('name') or e['repo']}   ({key})",
                             values=(e.get("version") or e["ref"], e.get("method", ""), e.get("installed_at", "")))

    def selected(self):
        sel = self.tree.selection()
        reg = self._registry() if sel else None
        return reg.get(sel[0]) if reg else None

    def set_busy(self, busy: bool):
        self.go.config(state="disabled" if busy else "normal")
        self.entry.config(state="disabled" if busy else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")
        for b in self.btns:
            b.config(state="disabled" if busy else "normal")
        if busy:
            self.bar.config(mode="indeterminate")
            self.bar.start(12)
        else:
            self.bar.stop()
            self.bar.config(mode="determinate", value=0)

    # -- actions
    def _clear_log(self):
        self.logbox.config(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.config(state="disabled")

    def start(self, text: str | None = None):
        if self.job or self.checking or self.closing or self.uninstalling:  # closing: nothing new may start
            return
        from_box = text is None  # Install / Enter / paste. Update starts pass the link and keep their log lines.
        text = text or self.url.get()
        if not parse_url(text):
            self.say("That doesn't look like a GitHub project link.", "#f87171")
            return
        if from_box:
            self._clear_log()
        self.set_busy(True)
        self.job = Job(text, self.q.put)
        job = self.job

        def run():
            with _HEAL_PASS:  # a repair pass from the start may still be busy with these folders
                pass
            job.run()
        self.job_thread = threading.Thread(target=run, daemon=True)
        self.job_thread.start()

    def cancel(self):
        if self.job:
            self.pending.clear()
            self.job.cancel()
            self.say("Cancelling...")
        elif self.checking:  # no Job yet: stop the checks, so no update starts when they end
            self.check_cancelled = True
            self.pending.clear()
            self.say("Cancelling...")

    CLOSE_WAIT_S = 60  # after this the window closes anyway; the next run puts the old version back

    def on_close(self):
        """Window X: close at once when idle; while a build runs, ask, stop it, let it put the old version back."""
        if self.closing:
            return
        building = bool(self.job) and self._alive(self.job_thread)
        # Update checks themselves are only GitHub lookups (their result is thrown away while closing),
        # so the window waits only for a build, an uninstall or a repair that is moving folders right now.
        if not building and not self.heals and not self._alive(self.uninstall_thread):
            self.root.destroy()
            return
        if building and not messagebox.askyesno("RepoForge", "A build is still running. Stop it and close? "
                                                "If this is an update, the version you had in RepoForge's folder "
                                                "is put back. Changes the app's own installer already made "
                                                "can't be undone.", parent=self.root):
            return
        self.closing = True  # from here on a finished job starts no queued update
        self.pending.clear()
        # Read again: while the question was open the job may have finished and the next update started.
        job, thread = self.job, self.job_thread
        if not self._alive(thread) and not self.heals and not self._alive(self.uninstall_thread):
            self.root.destroy()
            return
        self.set_busy(True)  # no new install, update or uninstall can be clicked while closing
        self.cancel_btn.config(state="disabled")
        if self._alive(self.uninstall_thread):  # an uninstall can't be stopped halfway: it finishes first
            self.say("Removing... the window closes when it's done.", FG)
        else:
            self.say("Stopping...", FG)
        self.close_deadline = time.monotonic() + self.CLOSE_WAIT_S
        self.root.after(200, self._close_when_stopped)
        if job and self._alive(thread):
            try:
                job.cancel()
            except OSError:
                pass  # e.g. taskkill couldn't start; the window still closes, the next run puts the old version back

    @staticmethod
    def _alive(thread) -> bool:
        return bool(thread and thread.is_alive())

    def _close_when_stopped(self):
        uninstalling = self._alive(self.uninstall_thread)
        busy = self._alive(self.job_thread) or self.heals > 0 or uninstalling
        # An uninstall is never cut off (the registry must be saved); it ends by itself, as the creator's
        # uninstaller is stopped after UNINSTALLER_WAIT_S.
        if not busy or (time.monotonic() >= self.close_deadline and not uninstalling):
            if self.job and self._alive(self.job_thread):
                try:  # backstop: stop the build tree too (e.g. a step that ignored the first Cancel)
                    self.job.cancel()
                except Exception:  # noqa: BLE001 - the window closes anyway
                    pass
            self.root.destroy()
            return
        self.root.after(200, self._close_when_stopped)

    def launch_sel(self):
        # Not while an install, update, repair, uninstall or closing is busy: app/ may hold a half-built
        # attempt then, and a program started now would lock the folder that is being put back.
        if self.job or self.checking or self.heals or self.closing or self.uninstalling:
            return  # the status line already says what is busy
        e = self.selected()
        if e:
            try:
                ep = e["entrypoints"][0]
                launch(ep["path"], ep.get("kind", e.get("kind")))
            except (OSError, LookupError, TypeError) as ex:
                self.say(f"Couldn't start it: {ex}", "#f87171")

    def folder_sel(self):
        e = self.selected()
        if e:
            app = Path(e["dir"]) / "app"
            target = app
            try:  # installer route: the program lives outside app/, so open where it really is
                p = Path(e["entrypoints"][0]["path"])
                if p.is_absolute() and not p.resolve().is_relative_to(app.resolve()):
                    target = p.parent
            except (OSError, LookupError, TypeError, ValueError):
                pass
            try:
                os.startfile(str(target))
            except OSError:
                self.say(f"Couldn't open the folder {target} - it may have been moved or deleted.", "#f87171")

    def uninstall_sel(self):
        if self.job or self.checking or self.closing or self.uninstalling:
            return
        e = self.selected()
        if not e or not messagebox.askyesno("Uninstall", f"Remove {e.get('name') or e['key']}?"):
            return
        if self.job or self.checking or self.closing or self.uninstalling:  # something started while asking
            return
        self.uninstalling = True
        self.set_busy(True)
        self.cancel_btn.config(state="disabled")  # a delete can't be cancelled halfway
        self.say(f"Removing {e.get('name') or e['key']}...")
        # Deleting can take a while (files in use are retried), so it runs in a worker thread.
        # The thread gets only the queue, not self (see _startup_heal).
        self.uninstall_thread = threading.Thread(target=App._uninstall_worker, args=(self.q, e["key"]), daemon=True)
        self.uninstall_thread.start()

    @staticmethod
    def _uninstall_worker(q, key):
        try:
            q.put(("uninstalled", True, uninstall(key)))
        except (OSError, RuntimeError) as ex:
            q.put(("uninstalled", False, str(ex) if isinstance(ex, RuntimeError) else
                   f"Couldn't remove everything (is it still running?): {ex}"))
        except Exception as ex:  # noqa: BLE001 - the window must never stay stuck on "Removing..."
            q.put(("uninstalled", False, f"Couldn't uninstall it: {ex}"))

    def update_sel(self):
        e = self.selected()
        if e:
            self.check_and_update([e["key"]])

    def update_all(self):
        reg = self._registry()
        if reg is not None:
            self.check_and_update(list(reg))

    def check_and_update(self, keys):
        """Repair + update checks run in a worker thread (they can take a while); the result comes back
        through self.q as ("checked", todo, failed, current, unrepaired) and pump hands it to _checked."""
        if self.job or self.checking or self.closing or self.uninstalling:
            return
        self.checking = True
        self.check_cancelled = False
        self._clear_log()  # cleared here, not when the updates start, so the repair/check lines stay visible
        self.set_busy(True)
        self.say("Checking for updates...")
        self.check_thread = threading.Thread(target=self._check_worker, args=(list(keys),), daemon=True)
        self.check_thread.start()

    def _check_worker(self, keys):
        log = lambda text: self.q.put(("log", text))
        todo, failed, current, unrepaired = [], 0, 0, 0
        try:
            reg = load_registry()
            # First put back anything an earlier run left half-updated - this needs no internet.
            unrepaired = self._heal_all(reg, log)
            for k in keys:
                if self.check_cancelled:
                    break
                try:
                    if update_available(reg[k]):
                        todo.append(k)
                    else:
                        current += 1
                except Exception as ex:  # noqa: BLE001
                    failed += 1
                    log(f"{k}: couldn't check ({ex})")
        except Exception as ex:  # noqa: BLE001 - e.g. the registry file can't be read
            log(f"Couldn't check for updates ({ex})")
            failed = max(len(keys) - len(todo) - current, failed)
        finally:
            self.q.put(("checked", todo, failed, current, unrepaired))

    @staticmethod
    def _heal_all(reg, log, done=None) -> int:
        """heal_registered, one pass at a time (_HEAL_PASS) and counted in self.heals so closing the window
        waits for it. done(unrepaired) runs before the next pass or install may start."""
        with _HEAL_PASS:
            with App._heals_lock:
                App.heals += 1
            unrepaired = 1
            try:
                unrepaired = heal_registered(reg, log)
                return unrepaired
            finally:
                with App._heals_lock:
                    App.heals -= 1
                if done:
                    done(unrepaired)

    @staticmethod
    def _any_half_updated() -> bool:
        try:
            for e in load_registry().values():
                try:
                    root = Path(e["dir"])
                    if root.is_dir() and needs_heal(root):
                        return True
                except Exception:  # noqa: BLE001 - a broken registry entry is just skipped
                    continue
        except Exception:  # noqa: BLE001 - e.g. the registry isn't a dict
            pass
        return False

    @staticmethod
    def _startup_heal(q):
        log = lambda text: q.put(("log", text))
        try:
            # "healed" is queued while the pass still holds _HEAL_PASS, so it always comes before
            # anything a check or install that waited for this pass reports.
            App._heal_all(load_registry(), log, done=lambda n: q.put(("healed", n)))
        except Exception as ex:  # noqa: BLE001 - e.g. the registry can't be read; the update checks try again
            log(f"Couldn't repair an unfinished update ({ex})")

    def _healed(self, unrepaired):
        n = f"{unrepaired} app" if unrepaired == 1 else f"{unrepaired} apps"
        note = (f"Couldn't repair {n} that an earlier update left unfinished - see the log. "
                "RepoForge tries again next time.") if unrepaired else ""
        if self.job or self.checking or self.uninstalling:  # an install, check or uninstall owns the status line
            if note:
                self.log(note)
            return
        if note:
            self.say(note, "#fbbf24")
        else:
            self.say("Checked an update that didn't finish last time - see the log.", FG)
        self.prefill()

    def _checked(self, todo, failed, current, unrepaired):
        self.checking = False
        apps = lambda n: f"{n} app" if n == 1 else f"{n} apps"
        notes = []
        if unrepaired:
            notes.append(f"Couldn't repair {apps(unrepaired)} that an earlier update left unfinished - see the log. "
                         "RepoForge tries again next time.")
        if self.check_cancelled:  # Cancel was pressed during the checks
            self.check_cancelled = False
            self.pending.clear()
            self.set_busy(False)
            self.say(" ".join(["Cancelled - no updates were started."] + notes), "#fbbf24" if notes else FG)
            return
        if failed:
            notes.append(f"Couldn't check {apps(failed)} for updates (no internet or GitHub limit). Try again later.")
        note = " ".join(notes)
        if not todo:
            self.set_busy(False)
            if not note:
                self.say("Everything is up to date.", FG)
            elif current:
                self.say(f"{apps(current)} up to date. {note}", "#fbbf24")
            else:
                self.say(note, "#fbbf24")
            return
        if note:
            self.log(note)
        self.after_note = note
        self.pending = todo[1:]
        self.start(f"https://github.com/{todo[0]}")

    def pump(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "log":
                    self.log(msg[1])
                elif msg[0] == "checked":
                    if self.closing:  # the window is closing: start no updates
                        self.checking = False
                        continue
                    self._checked(*msg[1:])
                elif msg[0] == "uninstalled":
                    _, ok, text = msg
                    self.uninstalling = False
                    self.log(text)
                    if self.closing:
                        continue
                    self.set_busy(False)
                    self.say(text, "#4ade80" if ok else "#f87171")
                    self.refresh()
                elif msg[0] == "healed":  # the repair pass at start has ended
                    if not self.closing:
                        self._healed(msg[1])
                elif msg[0] == "status":
                    if not self.closing:  # keep "Stopping..." on screen
                        self.say(msg[1], FG)
                elif msg[0] == "done":
                    _, ok, text, entry = msg
                    self.job = None
                    self.log(text)
                    if self.closing:  # the window closes as soon as the job thread has ended
                        continue
                    self.set_busy(False)
                    if self.after_note and not self.pending:
                        text, self.after_note = f"{text} {self.after_note}", ""
                    self.say(text, "#4ade80" if ok else "#f87171")
                    self.refresh()
                    if ok:
                        self.url.set("")
                        self.tree.selection_set(entry["key"])
                        guis = [ep for ep in entry["entrypoints"] if ep.get("kind", entry.get("kind")) != "cli"]
                        if guis and CFG["launch_after_install"] and not self.pending:
                            try:
                                launch(guis[0]["path"], "gui")
                            except OSError as ex:  # e.g. the admin (UAC) prompt was cancelled
                                self.log(f"Installed, but couldn't start it ({ex}).")
                    if self.pending:
                        self.start(f"https://github.com/{self.pending.pop(0)}")
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self.pump)  # always keep listening, even if one message went wrong


if __name__ == "__main__":
    if CONFIG_ERROR:  # don't install anything into the wrong (default) apps folder
        msg = f"{CONFIG_ERROR}\n\nFix the file (or delete it to start over with the defaults), then start RepoForge again."
        if sys.stderr:
            print(msg, file=sys.stderr)
        else:  # pythonw: no console, so show it in a window
            tk.Tk().withdraw()
            messagebox.showerror("RepoForge", msg)
        sys.exit(1)
    if len(sys.argv) > 1 and sys.argv[1] != "--gui":
        # Headless: python repoforge.py <github url>
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        j = Job(" ".join(sys.argv[1:]), lambda m: print(m[1], flush=True) if m[0] == "log" else
                print(m[2]) if m[0] == "done" else None)
        sys.exit(0 if j.run() else 1)  # so a script that calls it can tell a failure from a good install
    else:
        App().root.mainloop()
