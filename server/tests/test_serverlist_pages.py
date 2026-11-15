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

    def test_advanced_page_escapes_report_strings(self):
        """A report's version or system is shown as text, never as HTML (a stored XSS Codex found once the page ran)."""
        page = (PAGES / "advanced.html").read_text(encoding="utf-8")
        code = re.findall(r"<script>(.*?)</script>", page, re.S)[0]
        harness = r"""
const data = { versions: { "<svg/onload=alert(1)>": 2, "1.9.1-beta": 5 }, systems: { "<img src=x onerror=alert(2)>": 1 },
  countries: { US: 3 }, beammp: 0, sources24h: { "a.example": { req: 1, mb: 0, e429: 0, e4xx: 0, e5xx: 0, err: 0, retry: 0 } },
  hourly: [{ t: 0, online: 1, sources: { "a.example": { req: 1 } } }] };
global.location = { pathname: "/players/advanced" };
const el = () => ({ innerHTML: "", textContent: "" });
for (const id of ["ver", "sys", "cty", "mp", "src", "hourly", "upd"]) global[id] = el();
global.fetch = async () => ({ json: async () => data });
global.setInterval = () => {};
""" + code + r"""
setTimeout(() => {
  const html = ver.innerHTML + sys.innerHTML;
  if (/<svg|<img/.test(html) || !/&lt;svg/.test(html) || !upd.textContent.startsWith("Last loaded")) {
    console.log("BAD", html); process.exit(1);
  }
}, 100);
"""
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(harness)
        try:
            r = subprocess.run(["node", f.name], capture_output=True, text=True, timeout=60)
        finally:
            os.unlink(f.name)
        self.assertEqual(r.returncode, 0, r.stdout[:600] + r.stderr[:400])


if __name__ == "__main__":
    unittest.main()
