import json
import os
import unittest
from pathlib import Path

from tests.helpers import REPO, cleanup, load_repoforge


class FirstRun(unittest.TestCase):
    def setUp(self):
        self.rf, self.tmp = load_repoforge(config=None)

    def tearDown(self):
        cleanup(self.tmp)

    def test_writes_config_with_plain_default(self):
        cfg = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["apps_dir"], r"%USERPROFILE%\RepoForgeApps")

    def test_default_apps_dir_is_under_user_profile(self):
        expected = Path(os.path.expandvars(r"%USERPROFILE%\RepoForgeApps"))
        self.assertEqual(self.rf.APPS, expected)
        self.assertEqual(self.rf.REGISTRY, expected / "repoforge-registry.json")

    def test_example_config_matches_defaults(self):
        example = json.loads((REPO / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(example, self.rf.DEFAULTS)

    def test_readme_lists_every_setting(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        for k in self.rf.DEFAULTS:
            self.assertIn(f"| `{k}` |", readme, k)

    def test_first_run_has_no_config_error(self):
        self.assertEqual(self.rf.CONFIG_ERROR, "")


class ConfiguredAppsDir(unittest.TestCase):
    def setUp(self):
        os.environ["RF_TEST_BASE"] = "placeholder"
        self.rf, self.tmp = load_repoforge(config={"apps_dir": r"%RF_TEST_BASE%\apps"})

    def tearDown(self):
        os.environ.pop("RF_TEST_BASE", None)
        cleanup(self.tmp)

    def test_env_vars_in_apps_dir_are_expanded(self):
        self.assertEqual(self.rf.APPS, Path(r"placeholder\apps"))


class DamagedConfig(unittest.TestCase):
    """A config.json the user made a mistake in is never replaced; RepoForge reports it instead."""

    def check(self, text):
        rf, tmp = load_repoforge(config=text)
        try:
            self.assertEqual((tmp / "config.json").read_text(encoding="utf-8"), text)
            self.assertIn("config.json", rf.CONFIG_ERROR)
        finally:
            cleanup(tmp)

    def test_typo_is_kept_and_reported(self):
        self.check('{"model": "sonnet",}')

    def test_not_an_object_is_kept_and_reported(self):
        self.check("[1]")

    def test_wrong_setting_type_is_reported(self):
        self.check('{"apps_dir": 5}')


if __name__ == "__main__":
    unittest.main()
