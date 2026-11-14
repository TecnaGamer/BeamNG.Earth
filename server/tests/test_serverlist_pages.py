"""The collector's public pages (native/serverlist/src/*.html, served with include_str!): every inline script parses.
A double quote inside a double-quoted string in advanced.html's table header (the own-rate-limit columns' titles) made
its whole script a syntax error: the page showed its headings and nothing else, not even "Could not load" (2026-11-14).
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PAGES = Path(__file__).resolve().parents[2] / "native" / "serverlist" / "src"


@unittest.skipUnless(shutil.which("node"), "needs node to parse the pages' scripts")
class PageScriptsTest(unittest.TestCase):
    def test_inline_scripts_parse(self):
        pages = sorted(PAGES.glob("*.html"))
        self.assertTrue(pages)
        for page in pages:
            for i, code in enumerate(re.findall(r"<script>(.*?)</script>", page.read_text(encoding="utf-8"), re.S)):
                with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
                    f.write(code)
                try:
                    r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True, timeout=60)
                finally:
                    os.unlink(f.name)
                self.assertEqual(r.returncode, 0, f"{page.name} script {i}: {r.stderr[:400]}")


if __name__ == "__main__":
    unittest.main()
