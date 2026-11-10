"""selfupdate.py: the in-game "Update now" (GitHub #140): only this project's release assets with a digest, the file
checked against it, staged beside the running copy and handed over to at the next start."""
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import selfupdate  # noqa: E402

DATA = b"new BeamNG.Earth build" * 1000
DIGEST = hashlib.sha256(DATA).hexdigest()
URL = selfupdate.ASSET_PREFIX + "v9.9.9-beta/BeamNG.Earth-9.9.9-beta.exe"


def release(digest=DIGEST, url=URL, name="BeamNG.Earth-9.9.9-beta.exe"):
    return {"tag_name": "v9.9.9-beta", "assets": [
        {"name": "BeamNG.Earth-9.9.9-beta-x86_64.AppImage", "size": 5,
         "browser_download_url": selfupdate.ASSET_PREFIX + "v9.9.9-beta/BeamNG.Earth-9.9.9-beta-x86_64.AppImage",
         "digest": "sha256:" + "0" * 64},
        {"name": name, "size": len(DATA), "browser_download_url": url, "digest": "sha256:" + digest}]}


class FakeNet:
    @contextlib.contextmanager
    def session(self, url, kind="demand"):
        class S:
            headers = {"User-Agent": "test"}
            status, nbytes = 200, 0
        yield S()


class FakeResp(io.BytesIO):
    def __init__(self, body, url):
        super().__init__(body)
        self.status, self._url = 200, url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener(rel, body=DATA):
    def urlopen(req, timeout=None, context=None):
        u = req.full_url
        if u == selfupdate.API_URL:
            return FakeResp(json.dumps(rel).encode(), u)
        return FakeResp(body, "https://release-assets.githubusercontent.com/x")
    return urlopen


class PickTest(unittest.TestCase):
    def test_platform_asset_with_digest(self):
        v, name, url, size, digest = selfupdate.pick(release(), ".exe")
        self.assertEqual((v, name, digest), ("9.9.9-beta", "BeamNG.Earth-9.9.9-beta.exe", DIGEST))

    def test_foreign_or_unchecked_assets_refused(self):
        self.assertIsNone(selfupdate.pick(release(url="https://example.com/x.exe"), ".exe"))
        self.assertIsNone(selfupdate.pick(release(digest=""), ".exe"))
        self.assertIsNone(selfupdate.pick(release(name="../evil.exe"), ".exe"))

    def test_packaged_path(self):
        self.assertEqual(selfupdate.packaged_path({"APPIMAGE": "/a/B.AppImage"}, False), "/a/B.AppImage")
        self.assertIsNone(selfupdate.packaged_path({}, False))
        self.assertEqual(selfupdate.packaged_path({}, True, "C:/x/B.exe"), "C:/x/B.exe")


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.me = os.path.join(self.dir, "BeamNG.Earth-1.5.5-beta.exe")
        open(self.me, "wb").close()

    def test_downloads_checks_and_hands_over(self):
        with mock.patch("urllib.request.urlopen", opener(release())):
            version, path = selfupdate.download(FakeNet(), "1.5.5-beta", self.me)
        self.assertEqual(version, "9.9.9-beta")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), DATA)
        self.assertEqual(selfupdate.staged("1.5.5-beta", self.me), path, "the old copy hands over")
        self.assertIsNone(selfupdate.staged("9.9.9-beta", path), "the new copy runs itself")
        self.assertEqual(selfupdate.staged("1.5.5-beta", self.me), path, "... and the old one still hands over")

    def test_bad_checksum_deleted(self):
        with mock.patch("urllib.request.urlopen", opener(release(), body=b"tampered")):
            with self.assertRaises(selfupdate.UpdateError):
                selfupdate.download(FakeNet(), "1.5.5-beta", self.me)
        self.assertEqual(sorted(os.listdir(self.dir)), ["BeamNG.Earth-1.5.5-beta.exe"], "nothing left behind")
        self.assertIsNone(selfupdate.staged("1.5.5-beta", self.me))

    def test_up_to_date_and_source(self):
        with mock.patch("urllib.request.urlopen", opener(release())):
            with self.assertRaises(selfupdate.UpdateError):
                selfupdate.download(FakeNet(), "9.9.9-beta", self.me)
        with mock.patch.object(selfupdate, "packaged_path", return_value=None):
            with self.assertRaises(selfupdate.UpdateError):
                selfupdate.download(FakeNet(), "1.5.5-beta")

    def test_blocked_version_remembered(self):
        """GitHub #823: a downloaded copy Windows would not start (Smart App Control) is dropped and its version kept,
        so the update check does not offer it again; a plain drop remembers nothing."""
        with mock.patch("urllib.request.urlopen", opener(release())):
            selfupdate.download(FakeNet(), "1.5.5-beta", self.me)
        self.assertIsNone(selfupdate.blocked(self.me))
        selfupdate.drop_staged(self.me, blocked_by="smart_app_control")
        self.assertIsNone(selfupdate.staged("1.5.5-beta", self.me), "dropped")
        self.assertEqual(selfupdate.blocked(self.me), {"version": "9.9.9-beta", "by": "smart_app_control"})

    def test_changed_file_not_run(self):
        with mock.patch("urllib.request.urlopen", opener(release())):
            _v, path = selfupdate.download(FakeNet(), "1.5.5-beta", self.me)
        with open(path, "ab") as f:
            f.write(b"x")
        self.assertIsNone(selfupdate.staged("1.5.5-beta", self.me))


if __name__ == "__main__":
    unittest.main()
