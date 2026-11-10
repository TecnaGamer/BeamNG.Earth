"""updates: the GitHub version check."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import netfetch  # noqa: E402
import updates  # noqa: E402


def published(version="1.8.1", **changes):
    release = {"tag_name": "v" + version, "html_url": f"{updates.PAGE_URL}/tag/v{version}",
               "draft": False, "prerelease": False}
    release.update(changes)
    return json.dumps(release)


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
        self.assertIsNone(updates.check("1.8.1", FakeNet(published())), "main channel checks its published release")

    def test_check(self):
        net = FakeNet(published())
        r = updates.check("1.7.4-beta", net)
        self.assertEqual(r["latest"], "1.8.1")
        self.assertEqual(r["url"], updates.PAGE_URL + "/tag/v1.8.1")
        self.assertEqual(net.urls[0], (updates.LATEST_API, "background"))
        self.assertIsNone(updates.check("1.8.1", FakeNet(published())), "up to date")
        self.assertIsNone(updates.check("1.4.0", FakeNet(err=netfetch.FetchError("offline"))), "offline: nothing")
        self.assertIsNone(updates.check(None, FakeNet("2.0.0")), "no local VERSION: nothing")

    def test_unpublished_main_version_is_not_an_update(self):
        self.assertIsNone(updates.check("1.7.4-beta", FakeNet("1.8.1\n")))
        self.assertIsNone(updates.check("1.7.4-beta", FakeNet(published("1.7.3"))))

    def test_ineligible_or_malformed_published_release_is_ignored(self):
        for body in (published(draft=True), published(prerelease=True), published("1.9.0-dev.1"),
                     published(html_url="https://example.com/release"), published(tag_name="invalid"),
                     "[]", "null", "{}", "not JSON"):
            with self.subTest(body=body):
                self.assertIsNone(updates.check("1.7.4-beta", FakeNet(body)))


if __name__ == "__main__":
    unittest.main()


class OpenPageTest(unittest.TestCase):
    """The update notice's 'Open releases page' (the game's openWebBrowser does nothing on native Linux, 2026-10-18):
    the server opens this project's GitHub pages only."""

    def test_only_the_projects_pages(self):
        calls = []
        with mock.patch.object(updates.os, "startfile", create=True) as startfile:
            ok = updates.open_page(updates.PAGE_URL, popen=lambda *a, **k: calls.append(a))
            if updates.sys.platform == "win32":
                self.assertTrue(ok)
                startfile.assert_called_once_with(updates.PAGE_URL)
        if updates.sys.platform != "win32":
            self.assertTrue(ok)
            self.assertEqual(calls[0][0][1], updates.PAGE_URL)
        for bad in ("https://evil.example/x", "https://github.com/someone/else", None, 5,
                    updates.PAGE_URL + '\" && rm -rf ~', "https://github.com/redthepro/Beam-Earth/releases"):
            self.assertFalse(updates.open_page(bad, popen=lambda *a, **k: calls.append(a)), bad)

    def test_the_about_page_links(self):
        """The settings page's About buttons and contributors (the game's openWebBrowser refuses other sites): the
        Discord, Ko-fi, the project and a user's profile; nothing after a profile, nothing else on those sites."""
        for ok in ("https://discord.gg/tgf", "https://ko-fi.com/tecnagamer", "https://github.com/TecnaGamer/BeamNG.Earth",
                   "https://github.com/brechtknecht", "https://github.com/wyo-backroads"):
            self.assertTrue(updates.openable(ok), ok)
        for bad in ("https://discord.gg/other", "https://ko-fi.com/someoneelse", "https://github.com/someone/repo",
                    "https://github.com/a-", "https://github.com/x?y=1", "http://github.com/brechtknecht",
                    "https://github.com.evil.example/x", "https://discord.gg/tgf/../x", "https://github.com/" + "a" * 40):
            self.assertFalse(updates.openable(bad), bad)

    def test_downloads_stay_on_upstream(self):
        import selfupdate
        self.assertEqual(updates.REPO, "TecnaGamer/BeamNG.Earth")
        self.assertEqual(selfupdate.API_URL, updates.LATEST_API)
        self.assertEqual(selfupdate.ASSET_PREFIX, "https://github.com/TecnaGamer/BeamNG.Earth/releases/download/")
