"""Line endings follow .gitattributes, also in a clone made with Git for Windows' core.autocrlf=true."""
import unittest

from tests.test_privacy import REPO, repo_files

CRLF = {".bat", ".cmd", ".ps1", ".vbs"}


class LineEndings(unittest.TestCase):
    def test_files_match_gitattributes(self):
        self.assertTrue((REPO / ".gitattributes").is_file())
        bad = []
        for f in repo_files():
            data = f.read_bytes()
            if f.suffix == ".ico":
                continue
            if f.suffix.lower() in CRLF:
                if data.replace(b"\r\n", b"").count(b"\n"):
                    bad.append(f"{f.name}: needs CRLF")
            elif b"\r" in data:
                bad.append(f"{f.name}: needs LF")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
