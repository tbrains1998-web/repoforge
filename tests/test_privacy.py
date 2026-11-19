"""General checks that no file git would commit holds local paths, contact details, keys or work notes."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Words are put together at run time so this file doesn't match its own checks.
J = "".join
PATTERNS = {
    "absolute Windows path": re.compile(r"(?<![\w%])[A-Za-z]:\\\\?(?!n\b)[\w$ .-]+"),
    "user folder": re.compile(J([r"[\\/]", "Us", r"ers[\\/](?!<|\{|%|\*)\w+"]), re.I),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "key or token": re.compile(J([r"\b(sk-[A-Za-z0-9_-]{16,}|gh", r"[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|",
                                  r"xox[abp]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})"])),
    "IPv4 address": re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b"),
    "work notes": re.compile(J([r"\b(PL", "AN\\.md|re", "con\\b|fix ro", "und|part [A-H]\\b|(?:B|RF)[1-9]\\d?\\b(?=[:)\\s,]))"]),
                             re.I),
}
OK_EMAIL = re.compile(r"(noreply|example\.(com|org|net))", re.I)
OK_IP = {"127.0.0.1", "0.0.0.0"}


def repo_files(root: Path = REPO) -> list[Path]:
    """Every file git would commit: tracked ones plus new ones .gitignore doesn't keep out (so the settings and
    logs made by using the app in this folder are skipped). Without a .git folder a throwaway one is used."""
    with tempfile.TemporaryDirectory() as tmp:
        git_dir = root / ".git"
        if not git_dir.exists():
            git_dir = Path(tmp)
            subprocess.run(["git", "init", "-q", "--bare", str(git_dir)], check=True, capture_output=True)
        out = subprocess.run(["git", f"--git-dir={git_dir}", f"--work-tree={root}", "ls-files", "-z", "--cached",
                              "--others", "--exclude-standard"], check=True, capture_output=True, cwd=root).stdout
    files = [root / f for f in out.decode("utf-8").split("\0") if f]
    return [f for f in files if f.is_file()]


def text_of(f: Path) -> str:
    """The file as text; for a binary file (image, icon...) the readable text runs in it, where metadata lives."""
    data = f.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        runs = [r.decode("ascii") for r in re.findall(rb"[\x20-\x7e]{4,}", data)]
        runs += [r.decode("utf-16-le") for r in re.findall(rb"(?:[\x20-\x7e]\x00){4,}", data)]
        return "\n".join(runs)


def problems_in(files) -> list[str]:
    problems = []
    for f in files:
        for n, line in enumerate(text_of(f).splitlines(), 1):
            for what, rx in PATTERNS.items():
                for m in rx.finditer(line):
                    hit = m.group(0)
                    if what == "email" and OK_EMAIL.search(hit):
                        continue
                    if what == "IPv4 address" and (hit in OK_IP or hit.startswith(("192.0.2.", "198.51.100.",
                                                                                        "203.0.113."))
                                                   or any(int(x) > 255 for x in m.groups())):
                        continue
                    problems.append(f"{f.name}:{n}: {what}: {hit}")
    return problems


class Privacy(unittest.TestCase):
    def test_no_private_patterns(self):
        files = repo_files()
        self.assertIn(REPO / "repoforge.py", files)
        problems = problems_in(files)
        self.assertEqual(problems, [], "\n".join(problems))

    def test_no_backup_or_local_files(self):
        bad = [str(p.relative_to(REPO)) for p in repo_files()
               if ".bak" in p.name.lower() or p.name == "config.json" or p.suffix.lower() in {".log", ".exe"}]
        self.assertEqual(bad, [])


class Checker(unittest.TestCase):
    """The checks above on a made-up folder: they skip what .gitignore keeps out and still see new files."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rf-privacy-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        shutil.copy2(REPO / ".gitignore", self.root / ".gitignore")
        (self.root / "logs").mkdir()

    def test_ignored_files_are_skipped(self):
        for name in ("config.json", "logs/owner_repo_1.log", "new-file.md"):
            (self.root / name).write_text("x", encoding="utf-8")
        names = {f.relative_to(self.root).as_posix() for f in repo_files(self.root)}
        self.assertEqual(names, {".gitignore", "new-file.md"})

    def test_text_in_a_binary_file_is_checked(self):
        path = J(["C:", "\\", "Us", "ers\\someone\\pic.png"])
        (self.root / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff" + path.encode() + b"\x00\xfe"
                                            + path.encode("utf-16-le") + b"\xff")
        self.assertEqual(len([p for p in problems_in(repo_files(self.root)) if "user folder" in p]), 2)


if __name__ == "__main__":
    unittest.main()
