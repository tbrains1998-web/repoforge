import re
import unittest

from tests.helpers import cleanup, load_repoforge


class Helpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rf, cls.tmp = load_repoforge(config={"apps_dir": "unused"})

    @classmethod
    def tearDownClass(cls):
        cleanup(cls.tmp)

    def test_parse_url(self):
        p = self.rf.parse_url
        self.assertEqual(p("https://github.com/owner/repo"), ("owner", "repo", None))
        self.assertEqual(p("github.com/owner/repo.git"), ("owner", "repo", None))
        self.assertEqual(p("https://github.com/owner/repo/releases/tag/v1.2"), ("owner", "repo", "v1.2"))
        self.assertEqual(p("https://github.com/owner/repo/blob/main/README.md"), ("owner", "repo", None))
        self.assertEqual(p("owner/repo"), ("owner", "repo", None))
        self.assertIsNone(p("https://example.com/owner/repo"))

    def test_safe_name(self):
        self.assertEqual(self.rf.safe_name('My: "App"?', "x"), "My App")
        self.assertEqual(self.rf.safe_name("CON", "fallback"), "fallback")
        self.assertEqual(self.rf.safe_name("", None), "app")

    def test_uninstall_cmds(self):
        self.assertEqual(self.rf.uninstall_cmds(" a "), ["a"])
        self.assertEqual(self.rf.uninstall_cmds(["a", "", None, "b"]), ["a", "b"])
        self.assertEqual(self.rf.uninstall_cmds(""), [])

    def test_prompt_has_no_hardware_of_one_pc(self):
        self.assertIsNone(re.search(r"\b(RTX|GTX|CUDA|Radeon)\b", self.rf.PROMPT))


if __name__ == "__main__":
    unittest.main()
