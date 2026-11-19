import os
import subprocess
import sys
import unittest

from tests.helpers import cleanup, load_repoforge


class Headless(unittest.TestCase):
    def test_failed_install_exits_non_zero(self):
        _, tmp = load_repoforge()  # a private copy next to its own config.json
        self.addCleanup(cleanup, tmp)
        env = dict(os.environ, USERPROFILE=str(tmp))  # the default apps folder lands in the temp folder
        p = subprocess.run([sys.executable, "-B", str(tmp / "repoforge.py"), "not a link"], cwd=tmp, env=env,
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertIn("doesn't look like a GitHub project link", p.stdout)
        self.assertNotEqual(p.returncode, 0)  # scripts that call it must see the failure


if __name__ == "__main__":
    unittest.main()
