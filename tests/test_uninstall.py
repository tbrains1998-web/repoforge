import threading
import unittest
from unittest import mock

from tests.helpers import cleanup, load_repoforge, use_temp_apps


class Uninstall(unittest.TestCase):
    def setUp(self):
        self.rf, self.tmp = load_repoforge(config={"apps_dir": "unused"})
        use_temp_apps(self.rf, self.tmp)
        self.root = self.rf.APPS / "owner" / "tool"
        (self.root / "app").mkdir(parents=True)

    def tearDown(self):
        cleanup(self.tmp)

    def _register(self, ep_path, uninstall=""):
        self.rf.save_registry({"owner/tool": {"key": "owner/tool", "owner": "owner", "repo": "tool",
                                              "dir": str(self.root), "uninstall": uninstall,
                                              "entrypoints": [{"path": str(ep_path), "kind": "gui"}],
                                              "shortcuts": [], "shims": []}})

    def test_normal_app_is_removed(self):
        exe = self.root / "app" / "tool.exe"
        exe.write_text("x")
        self._register(exe)
        self.assertEqual(self.rf.uninstall("owner/tool"), "Removed owner/tool.")
        self.assertFalse(self.root.exists())
        self.assertEqual(self.rf.load_registry(), {})

    def test_installer_route_without_uninstaller_is_honest(self):
        outside = self.tmp / "ProgramFiles" / "Tool" / "tool.exe"
        outside.parent.mkdir(parents=True)
        outside.write_text("x")
        self._register(outside)
        msg = self.rf.uninstall("owner/tool")
        self.assertFalse(msg.startswith("Removed owner/tool."), msg)
        self.assertIn(str(outside), msg)
        self.assertIn("Settings", msg)
        self.assertFalse(self.root.exists())
        self.assertTrue(outside.exists())  # RepoForge never deletes what the creator's installer put elsewhere

    def test_installer_route_program_already_gone(self):
        self._register(self.tmp / "ProgramFiles" / "Gone" / "tool.exe")
        self.assertEqual(self.rf.uninstall("owner/tool"), "Removed owner/tool.")

    def _outside_exe(self):
        outside = self.tmp / "ProgramFiles" / "Tool" / "tool.exe"
        outside.parent.mkdir(parents=True)
        outside.write_text("x")
        return outside

    def _assert_honest(self, msg, outside):
        self.assertFalse(msg.startswith("Removed owner/tool."), msg)
        self.assertIn(str(outside), msg)
        self.assertIn("Settings", msg)
        self.assertTrue(outside.exists())

    def test_saved_uninstaller_file_missing_is_honest(self):
        outside = self._outside_exe()
        self._register(outside, uninstall=f'"{self.tmp / "ProgramFiles" / "Tool" / "unins000.exe"}" /S')
        with mock.patch.object(self.rf, "run_uninstaller", return_value=False):
            self._assert_honest(self.rf.uninstall("owner/tool"), outside)

    def test_uninstaller_ok_but_program_stays_is_honest(self):
        outside = self._outside_exe()
        self._register(outside, uninstall="unins000.exe /S")
        with mock.patch.object(self.rf, "run_uninstaller", return_value=True), \
                mock.patch.object(self.rf, "UNINSTALL_SETTLE_S", 0.3):
            self._assert_honest(self.rf.uninstall("owner/tool"), outside)
        self.assertEqual(self.rf.load_registry(), {})

    def test_uninstaller_that_finishes_a_moment_later(self):
        outside = self._outside_exe()
        self._register(outside, uninstall="unins000.exe /S")
        later = threading.Timer(0.3, outside.unlink)  # like an uninstaller that copied itself to temp and returned
        with mock.patch.object(self.rf, "run_uninstaller", side_effect=lambda cmd: later.start() or True), \
                mock.patch.object(self.rf, "UNINSTALL_SETTLE_S", 10):
            self.assertEqual(self.rf.uninstall("owner/tool"), "Removed owner/tool.")
        later.join()


if __name__ == "__main__":
    unittest.main()
