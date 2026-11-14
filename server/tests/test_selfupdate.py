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
        self.assertEqual(selfupdate._downloaded(self.dir), ["BeamNG.Earth-9.9.9-beta.exe"],
                         "noted as the updater's own download (prune_old may delete it later, GitHub #900)")
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


class PruneTest(unittest.TestCase):
    """GitHub #900: the old copies beside the running one go, except the ones a shortcut or a way back needs."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def files(self, *names, fetched=True):
        """The files, noted as downloaded by the updater (fetched) unless told otherwise."""
        for n in names:
            Path(self.dir, n).write_bytes(b"x")
            if fetched:
                selfupdate.note_downloaded(os.path.join(self.dir, n))

    def left(self):
        """The files in the folder, less the updater's notes."""
        return sorted(n for n in os.listdir(self.dir) if n not in (selfupdate.KEEP, selfupdate.DOWNLOADED))

    def test_keeps_the_previous_and_the_oldest_while_no_shortcut_is_known(self):
        self.files("BeamNG.Earth-1.8.0-beta.exe", "BeamNG.Earth-1.8.1-beta.exe", "BeamNG.Earth-1.8.1-beta.dev.3.exe",
                   "BeamNG.Earth-1.8.2-beta.exe", "BeamNG.Earth-1.9.0-beta.exe", "BeamNG.Earth-1.8.0-beta.zip",
                   "notes.txt", "BeamNG.Earth-1.8.0-beta-x86_64.AppImage")
        me = os.path.join(self.dir, "BeamNG.Earth-1.8.2-beta.exe")
        gone = selfupdate.prune_old("1.8.2-beta", me)
        self.assertEqual(sorted(gone), ["BeamNG.Earth-1.8.1-beta.exe"], "(beta.dev.3 is after beta: the previous)")
        self.assertEqual(self.left(), ["BeamNG.Earth-1.8.0-beta-x86_64.AppImage", "BeamNG.Earth-1.8.0-beta.exe",
                                       "BeamNG.Earth-1.8.0-beta.zip", "BeamNG.Earth-1.8.1-beta.dev.3.exe",
                                       "BeamNG.Earth-1.8.2-beta.exe", "BeamNG.Earth-1.9.0-beta.exe", "notes.txt"],
                         "this copy, a newer one, other kinds of file and the AppImage are left alone")

    def test_the_hand_over_source_is_kept_and_the_oldest_then_goes(self):
        self.files("BeamNG.Earth-1.7.0.exe", "BeamNG.Earth-1.7.4.exe", "BeamNG.Earth-1.8.0-beta.exe",
                   "BeamNG.Earth-1.8.1-beta.exe", "BeamNG.Earth-1.8.2-beta.exe")
        me = os.path.join(self.dir, "BeamNG.Earth-1.8.2-beta.exe")
        selfupdate.note_started_from(os.path.join(self.dir, "BeamNG.Earth-1.7.4.exe"), me)
        selfupdate.note_started_from(os.path.join(self.dir, "BeamNG.Earth-1.7.4.exe"), me)
        selfupdate.note_started_from(os.path.join(tempfile.gettempdir(), "BeamNG.Earth-1.0.0.exe"), me)
        self.assertEqual(json.loads(Path(self.dir, selfupdate.KEEP).read_text())["started_from"],
                         ["BeamNG.Earth-1.7.4.exe"], "noted once, only from this folder")
        gone = selfupdate.prune_old("1.8.2-beta", me)
        self.assertEqual(sorted(gone), ["BeamNG.Earth-1.7.0.exe", "BeamNG.Earth-1.8.0-beta.exe"])
        self.assertIn("BeamNG.Earth-1.7.4.exe", self.left(), "the shortcut's copy kept")
        self.assertIn("BeamNG.Earth-1.8.1-beta.exe", self.left(), "the previous version kept")

    def test_nothing_for_a_source_copy_or_a_single_old_copy(self):
        self.files("BeamNG.Earth-1.8.1-beta.exe", "BeamNG.Earth-1.8.2-beta.exe")
        me = os.path.join(self.dir, "BeamNG.Earth-1.8.2-beta.exe")
        self.assertEqual(selfupdate.prune_old("1.8.2-beta", me), [])
        self.assertEqual(selfupdate.prune_old("1.8.2-beta", os.path.join(self.dir, "start.py")), [])
        self.assertEqual(selfupdate.prune_old("not a version", me), [])
        self.assertEqual(len(self.left()), 2)

    def test_only_the_updaters_own_downloads_go(self):
        """A copy the player downloaded by hand (a shortcut may point at it before any hand-over noted it), and every
        copy from before the downloaded list existed, stays, even on the first run after an update."""
        self.files("BeamNG.Earth-1.7.0.exe", "BeamNG.Earth-1.7.4.exe", fetched=False)
        self.files("BeamNG.Earth-1.8.0-beta.exe", "BeamNG.Earth-1.8.1-beta.exe", "BeamNG.Earth-1.8.2-beta.exe")
        me = os.path.join(self.dir, "BeamNG.Earth-1.8.2-beta.exe")
        gone = selfupdate.prune_old("1.8.2-beta", me)
        self.assertEqual(gone, [], "of the two fetched older copies, the previous and the oldest are both kept")
        self.files("BeamNG.Earth-1.7.9-beta.exe")
        gone = selfupdate.prune_old("1.8.2-beta", me)
        self.assertEqual(gone, ["BeamNG.Earth-1.8.0-beta.exe"])
        self.assertIn("BeamNG.Earth-1.7.0.exe", self.left(), "a hand-downloaded copy is never touched")
        self.assertIn("BeamNG.Earth-1.7.4.exe", self.left())

    def test_nothing_without_the_downloaded_list(self):
        self.files("BeamNG.Earth-1.8.0.exe", "BeamNG.Earth-1.8.1.exe", "BeamNG.Earth-1.8.2.exe",
                   "BeamNG.Earth-1.8.3.exe", fetched=False)
        me = os.path.join(self.dir, "BeamNG.Earth-1.8.3.exe")
        self.assertEqual(selfupdate.prune_old("1.8.3", me), [])
        self.assertEqual(len(self.left()), 4)

    def test_a_locked_copy_is_not_reported(self):
        self.files("BeamNG.Earth-1.8.0.exe", "BeamNG.Earth-1.8.1.exe", "BeamNG.Earth-1.8.2.exe", "BeamNG.Earth-1.8.3.exe")
        me = os.path.join(self.dir, "BeamNG.Earth-1.8.3.exe")
        self.assertEqual(selfupdate.prune_old("1.8.3", me, remove=lambda p: None), [])


if __name__ == "__main__":
    unittest.main()
