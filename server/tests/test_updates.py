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

    def get(self, url, kind="demand", headers=None):
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

    def test_development_builds(self):
        # the dev branch's pre-releases (v<VERSION>.dev.<n>): after their beta, before the next, in number order
        self.assertTrue(updates.newer("1.7.2-beta.dev.1", "1.7.2-beta"))
        self.assertTrue(updates.newer("1.7.2-beta.dev.10", "1.7.2-beta.dev.9"), "numbers, not text")
        self.assertTrue(updates.newer("1.7.3-beta", "1.7.2-beta.dev.99"))
        self.assertFalse(updates.newer("1.7.2-beta", "1.7.2-beta.dev.1"))
        self.assertTrue(updates.is_dev("1.7.2-beta.dev.3"))
        self.assertFalse(updates.is_dev("1.7.2-beta"))

    def test_development_build_checks_the_prereleases(self):
        import json
        page = f"https://github.com/{updates.REPO}/releases/tag/"
        rels = [{"tag_name": "v1.7.2-beta.dev.12", "html_url": page + "v1.7.2-beta.dev.12", "prerelease": True},
                {"tag_name": "v1.7.3-beta.dev.2", "html_url": "https://evil.example/x", "prerelease": True},
                {"tag_name": "v1.7.2-beta.dev.11", "html_url": page + "v1.7.2-beta.dev.11", "prerelease": True},
                {"tag_name": "v1.8.0-beta", "html_url": page + "v1.8.0-beta", "draft": True},
                {"tag_name": "v1.7.2-beta", "html_url": page + "v1.7.2-beta"}]
        updates._releases[:] = [0.0, None]
        net = FakeNet(json.dumps(rels))
        r = updates.check("1.7.2-beta.dev.11", net)
        self.assertEqual((r["latest"], r["url"], r["dev"]), ("1.7.2-beta.dev.12", page + "v1.7.2-beta.dev.12", True),
                         "the newest of this project's pages, no draft")
        self.assertEqual(net.urls[0][0], updates.RELEASES_API)
        self.assertIsNone(updates.check("1.7.2-beta.dev.12", net), "up to date (the list kept: no second request)")
        self.assertEqual(len(net.urls), 1)
        updates._releases[:] = [0.0, None]
        self.assertIsNone(updates.check("1.7.2-beta", FakeNet("1.7.2-beta")), "a beta asks VERSION on main as before")

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
        ok = updates.open_page("https://github.com/TecnaGamer/BeamNG.Earth/releases", popen=lambda *a, **k: calls.append(a))
        if updates.sys.platform != "win32":
            self.assertTrue(ok)
            self.assertEqual(calls[0][0][1], "https://github.com/TecnaGamer/BeamNG.Earth/releases")
        for bad in ("https://evil.example/x", "https://github.com/someone/else", None, 5,
                    "https://github.com/TecnaGamer/BeamNG.Earth/releases\" && rm -rf ~"):
            self.assertFalse(updates.open_page(bad, popen=lambda *a, **k: calls.append(a)), bad)
