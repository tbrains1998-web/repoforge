import contextlib
import io
import json
import types
import unittest
from unittest import mock

from tests.helpers import cleanup, load_repoforge, use_temp_apps

HALF_WRITTEN = '{"half": {"key": '


class Registry(unittest.TestCase):
    def setUp(self):
        self.rf, self.tmp = load_repoforge(config={"apps_dir": "unused"})
        use_temp_apps(self.rf, self.tmp)
        quiet = contextlib.redirect_stderr(io.StringIO())  # the damaged-file warning is expected here
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def tearDown(self):
        cleanup(self.tmp)

    def test_round_trip(self):
        self.rf.save_registry({"a/b": {"key": "a/b"}})
        self.assertEqual(self.rf.load_registry(), {"a/b": {"key": "a/b"}})
        self.assertEqual([p.name for p in self.rf.APPS.iterdir()], ["repoforge-registry.json"])  # no temp file left

    def test_missing_file_is_empty(self):
        self.assertEqual(self.rf.load_registry(), {})

    def test_failed_write_keeps_old_file(self):
        self.rf.save_registry({"old": {}})
        with mock.patch.object(self.rf.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.rf.save_registry({"new": {}})
        self.assertEqual(json.loads(self.rf.REGISTRY.read_text(encoding="utf-8")), {"old": {}})

    def test_broken_file_is_kept_not_wiped(self):
        self.rf.APPS.mkdir(parents=True)
        self.rf.REGISTRY.write_text(HALF_WRITTEN, encoding="utf-8")
        self.assertEqual(self.rf.load_registry(), {})
        kept = list(self.rf.APPS.glob("*.broken*"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(encoding="utf-8"), HALF_WRITTEN)
        self.assertTrue(self.rf.REGISTRY_WARNINGS)
        self.assertIn(str(kept[0]), self.rf.REGISTRY_WARNINGS[0])
        self.rf.save_registry({"new": {}})
        self.assertTrue(kept[0].exists())

    def test_not_a_dict_counts_as_broken(self):
        self.rf.APPS.mkdir(parents=True)
        self.rf.REGISTRY.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(self.rf.load_registry(), {})
        self.assertEqual(len(list(self.rf.APPS.glob("*.broken*"))), 1)


    def test_not_utf8_counts_as_broken(self):
        self.rf.APPS.mkdir(parents=True)
        self.rf.REGISTRY.write_bytes(b'{"a": "\xff\xfe"}')
        self.assertEqual(self.rf.load_registry(), {})
        self.assertEqual(len(list(self.rf.APPS.glob("*.broken*"))), 1)
        self.assertTrue(self.rf.REGISTRY_WARNINGS)

    def test_bom_is_fine(self):
        self.rf.APPS.mkdir(parents=True)
        self.rf.REGISTRY.write_text('{"a/b": {"key": "a/b"}}', encoding="utf-8-sig")
        self.assertEqual(self.rf.load_registry(), {"a/b": {"key": "a/b"}})
        self.assertEqual(list(self.rf.APPS.glob("*.broken*")), [])

    def test_file_gone_after_locked_read_is_empty(self):
        with mock.patch.object(self.rf.time, "sleep"), \
                mock.patch("pathlib.Path.read_bytes", side_effect=[PermissionError("locked"), FileNotFoundError()]):
            self.assertEqual(self.rf.load_registry(), {})

    def test_other_reader_moved_broken_file_first(self):
        self.rf.APPS.mkdir(parents=True)
        self.rf.REGISTRY.write_text(HALF_WRITTEN, encoding="utf-8")
        real_replace = self.rf.os.replace
        calls = []

        def other_reader_wins(src, dst):
            if not calls:  # the other window moves it aside just before this one tries
                real_replace(src, str(src) + ".broken-other")
                calls.append(dst)
                raise FileNotFoundError(src)
            return real_replace(src, dst)

        with mock.patch.object(self.rf.os, "replace", side_effect=other_reader_wins):
            self.assertEqual(self.rf.load_registry(), {})

    def test_still_locked_raises(self):
        self.rf.APPS.mkdir(parents=True)
        self.rf.REGISTRY.write_text("{}", encoding="utf-8")
        with mock.patch.object(self.rf.time, "sleep"), \
                mock.patch("pathlib.Path.read_bytes", side_effect=PermissionError("locked")):
            with self.assertRaises(OSError):
                self.rf.load_registry()


class WindowReadsRegistry(unittest.TestCase):
    """The window's buttons read the app list too: a read error must show a message, not do nothing."""

    def setUp(self):
        self.rf, self.tmp = load_repoforge(config={"apps_dir": "unused"})
        use_temp_apps(self.rf, self.tmp)
        self.win = types.SimpleNamespace(log=mock.Mock(), say=mock.Mock(), check_and_update=mock.Mock(),
                                         tree=mock.Mock(selection=mock.Mock(return_value=("a/b",))))
        self.win._registry = types.MethodType(self.rf.App._registry, self.win)

    def tearDown(self):
        cleanup(self.tmp)

    def test_selected_reports_read_error(self):
        with mock.patch.object(self.rf, "load_registry", side_effect=RuntimeError("damaged")):
            self.assertIsNone(self.rf.App.selected(self.win))
        self.win.say.assert_called()
        self.assertIn("damaged", self.win.log.call_args[0][0])

    def test_update_all_reports_read_error(self):
        with mock.patch.object(self.rf, "load_registry", side_effect=OSError("locked")):
            self.rf.App.update_all(self.win)
        self.win.check_and_update.assert_not_called()
        self.win.say.assert_called()

    def test_selected_shows_new_damage_warning_now(self):
        def damaged():
            self.rf.REGISTRY_WARNINGS.append("warning: damaged")
            return {}
        with mock.patch.object(self.rf, "load_registry", side_effect=damaged):
            self.assertIsNone(self.rf.App.selected(self.win))
        self.win.log.assert_called_with("warning: damaged")
        self.assertEqual(self.rf.REGISTRY_WARNINGS, [])

    def test_update_all_uses_the_list(self):
        self.rf.save_registry({"a/b": {}, "c/d": {}})
        self.rf.App.update_all(self.win)
        self.win.check_and_update.assert_called_once_with(["a/b", "c/d"])


if __name__ == "__main__":
    unittest.main()
