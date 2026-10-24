"""updates: the GitHub version check."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import netfetch  # noqa: E402
import updates  # noqa: E402


class FakeNet:
    def __init__(self, body=None, err=None):
        self.body, self.err, self.urls = body, err, []

    def get(self, url, kind="demand"):
        self.urls.append((url, kind))
        if self.err:
            raise self.err

        class R:
            pass
        r = R()
        r.body = self.body.encode()
        return r


class UpdatesTest(unittest.TestCase):
    def test_newer(self):
        self.assertTrue(updates.newer("1.4.1-preview", "1.4.0-preview"))
        self.assertTrue(updates.newer("1.4.0", "1.4.0-preview"), "a release is newer than its preview")
        self.assertFalse(updates.newer("1.4.0-preview", "1.4.0"))
        self.assertFalse(updates.newer("1.4.0-preview", "1.4.0-preview"))
        self.assertTrue(updates.newer("1.10.0", "1.9.3"), "numbers, not text")
        self.assertFalse(updates.newer("<html>404</html>", "1.0.0"), "not a version: no update")

    def test_check(self):
        net = FakeNet("1.5.0-preview\n")
        r = updates.check("1.4.0-preview", net)
        self.assertEqual(r["latest"], "1.5.0-preview")
        self.assertEqual(r["url"], updates.PAGE_URL)
        self.assertEqual(net.urls[0], (updates.VERSION_URL, "background"))
        self.assertIsNone(updates.check("1.5.0-preview", FakeNet("1.5.0-preview")), "up to date")
        self.assertIsNone(updates.check("1.4.0", FakeNet(err=netfetch.FetchError("offline"))), "offline: nothing")
        self.assertIsNone(updates.check(None, FakeNet("2.0.0")), "no local VERSION: nothing")


if __name__ == "__main__":
    unittest.main()


class OpenPageTest(unittest.TestCase):
    """The update notice's 'Open releases page' (the game's openWebBrowser does nothing on native Linux, 2026-10-18):
    the server opens this project's GitHub pages only."""

    def test_only_the_projects_pages(self):
        calls = []
        ok = updates.open_page(f"https://github.com/{updates.REPO}/releases", popen=lambda *a, **k: calls.append(a))
        if updates.sys.platform != "win32":
            self.assertTrue(ok)
            self.assertEqual(calls[0][0][1], f"https://github.com/{updates.REPO}/releases")
        for bad in ("https://evil.example/x", "https://github.com/someone/else", None, 5,
                    f"https://github.com/{updates.REPO}/releases\" && rm -rf ~"):
            self.assertFalse(updates.open_page(bad, popen=lambda *a, **k: calls.append(a)), bad)
