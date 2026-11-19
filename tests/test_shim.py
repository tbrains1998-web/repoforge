import ctypes
import os
import subprocess
import unittest

from tests.helpers import cleanup, load_repoforge, use_temp_apps


def oem_letter():
    """A non-ASCII letter this PC's console code page can hold, or None (e.g. é on 850, Ж on 866, 日 on 932)."""
    for c in "éöЖα日":
        try:
            c.encode("oem")
            return c
        except UnicodeEncodeError:
            pass
    return None


class Shim(unittest.TestCase):
    """The .cmd command RepoForge makes for a CLI app must work when the path has letters like ö or é in it
    (a Windows user name often does). cmd.exe reads .cmd files in the console code page, not UTF-8."""

    def setUp(self):
        self.rf, self.tmp = load_repoforge(config={"apps_dir": "unused"})
        # Letters from several scripts: no single code page has them all, so this only works because the apps
        # folder's path is never written into the shim.
        use_temp_apps(self.rf, self.tmp / "Jösé Müller Жан 日本")

    def tearDown(self):
        cleanup(self.tmp)

    def run_cmd(self, line, cwd=None, path_first=None):
        cp = ctypes.windll.kernel32.GetOEMCP()  # a normal new console starts in this code page
        env = dict(os.environ)
        if path_first:
            env["PATH"] = f"{path_first};{env.get('PATH', '')}"
        p = subprocess.run(f'cmd /d /s /c "chcp {cp} >nul & {line}"', capture_output=True, text=True, env=env,
                           cwd=cwd, creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        return p.stdout + p.stderr, p.returncode

    def run_shim(self, shim):
        return self.run_cmd(f'"{shim}" hi')[0]

    def make_target(self, folder, body="@echo ran %*\r\n"):
        folder.mkdir(parents=True)
        target = folder / "tool.cmd"
        target.write_text(body, encoding="ascii")
        return target

    def test_target_inside_apps_folder(self):
        target = self.make_target(self.rf.APPS / "owner" / "tool" / "app" / "bin")
        shim = self.rf.write_shim(target, "tool")
        self.assertEqual(shim, self.rf.BIN / "tool.cmd")
        shim.read_bytes().decode("ascii")  # plain ASCII, so it reads the same in every code page
        self.assertIn("ran hi", self.run_shim(shim))

    def test_quoted_name_found_on_path(self):
        # cmd.exe bug: for "tool" (quoted, no .cmd) found through PATH, %~dp0 is the current folder instead.
        target = self.make_target(self.rf.APPS / "owner" / "tool" / "app", "@echo ran %*\r\n@exit /b 3\r\n")
        self.rf.write_shim(target, "tool")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        for line in ('"tool" hi', "tool hi", '"tool.cmd" hi'):
            out, code = self.run_cmd(line, cwd=elsewhere, path_first=self.rf.BIN)
            self.assertIn("ran hi", out, line)
            self.assertEqual(code, 3, line)  # the app's exit code comes through

    def test_dotted_name_found_on_path(self):
        # %~n0 drops what follows the last dot, so the fallback must use the whole typed name.
        target = self.make_target(self.rf.APPS / "owner" / "my.tool" / "app", "@echo ran %*\r\n@exit /b 3\r\n")
        self.rf.write_shim(target, "my.tool")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        for line in ('"my.tool" hi', "my.tool hi", '"my.tool.cmd" hi', "my.tool.cmd hi", '"MY.TOOL" hi'):
            out, code = self.run_cmd(line, cwd=elsewhere, path_first=self.rf.BIN)
            self.assertIn("ran hi", out, line)
            self.assertEqual(code, 3, line)

    def test_target_outside_apps_folder(self):
        c = oem_letter()
        if c is None:
            self.skipTest("this PC's console code page has no non-ASCII letters")
        target = self.make_target(self.tmp / f"Pr{c}gram Files" / "Tool")
        self.assertIn("ran hi", self.run_shim(self.rf.write_shim(target, "tool")))


if __name__ == "__main__":
    unittest.main()
