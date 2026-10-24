"""staging: atomic writes into the game's user folder stage their temporary file outside it."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import staging  # noqa: E402


class StagingTest(unittest.TestCase):
    def test_outside_the_user_folder_same_disk(self):
        with tempfile.TemporaryDirectory() as root:
            user = os.path.join(root, "BeamNG.drive", "current")
            target = os.path.join(user, "temp", "beamngEarth", "s1", "far_1_2.cdae")
            os.makedirs(os.path.dirname(target))
            tmp = staging.tmp_for(target)
            self.assertEqual(os.path.dirname(tmp), os.path.join(root, "BeamNG.drive", staging.STAGING))
            self.assertFalse(tmp.startswith(user))
            with open(tmp, "wb") as f:
                f.write(b"x")
            os.replace(tmp, target)
            self.assertTrue(os.path.exists(target))
            self.assertNotEqual(staging.tmp_for(target), staging.tmp_for(target))   # one per write

    def test_session_folder_deleted_mid_run(self):
        """GitHub #27: the session folder was gone (temp cleared) and every terrain write after it failed."""
        with tempfile.TemporaryDirectory() as root:
            session = os.path.join(root, "BeamNG.drive", "current", "temp", "beamngEarth", "s1")
            target = os.path.join(session, "hm_11_0_0.png")
            os.makedirs(session)
            tmp = staging.tmp_for(target)
            with open(tmp, "wb") as f:
                f.write(b"x")
            os.rmdir(session)
            with self.assertLogs("earthd.staging", "WARNING"):
                staging.replace(tmp, target)
            with open(target, "rb") as f:
                self.assertEqual(f.read(), b"x")
            with self.assertRaises(FileNotFoundError):     # a missing temporary file is still an error
                staging.replace(tmp, target)

    def test_elsewhere_beside_the_file(self):
        self.assertEqual(staging.tmp_for("/home/x/.cache/beamng-earth/dem/1/2.webp"),
                         "/home/x/.cache/beamng-earth/dem/1/2.webp.tmp")

    def test_old_leftovers_swept(self):
        with tempfile.TemporaryDirectory() as root:
            d = os.path.join(root, "BeamNG.drive", staging.STAGING)
            os.makedirs(d)
            old, new = os.path.join(d, "a.tmp"), os.path.join(d, "b.tmp")
            for p in (old, new):
                open(p, "w").close()
            t = time.time() - staging.SWEEP_AGE_S - 10
            os.utime(old, (t, t))
            staging.tmp_for(os.path.join(root, "BeamNG.drive", "current", "temp", "x", "y.json"))
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(new))

    def test_cache_file_held_elsewhere(self):
        # Windows: a file open or mapped in another process cannot be replaced (PermissionError, GitHub #67)
        real, calls = os.replace, []

        def held(n):
            def fake(a, b):
                calls.append(a)
                if len(calls) <= n:
                    raise PermissionError(13, "Access is denied")
                real(a, b)
            return fake
        old_waits = staging.REPLACE_WAITS
        staging.REPLACE_WAITS = (0.0,) * len(old_waits)
        try:
            with tempfile.TemporaryDirectory() as d:
                path, tmp = os.path.join(d, "t.webp"), os.path.join(d, "t.webp.1.tmp")
                Path(tmp).write_bytes(b"new")
                staging.os.replace = held(2)                # freed after two tries: ours goes in
                self.assertTrue(staging.replace_cached(tmp, path))
                self.assertEqual(Path(path).read_bytes(), b"new")
                Path(tmp).write_bytes(b"newer")
                calls.clear()
                staging.os.replace = held(99)               # held throughout: the copy there is kept
                self.assertFalse(staging.replace_cached(tmp, path))
                self.assertEqual(Path(path).read_bytes(), b"new")
                self.assertFalse(os.path.exists(tmp))
                self.assertEqual(len(calls), len(staging.REPLACE_WAITS) + 1)
                os.remove(path)
                Path(tmp).write_bytes(b"x")
                with self.assertRaises(PermissionError):    # nothing there to keep: the error stands
                    staging.replace_cached(tmp, path)
        finally:
            staging.os.replace = real
            staging.REPLACE_WAITS = old_waits



class WarmTest(unittest.TestCase):
    """Windows: a file replaced into the game's folder is read once at once (staging.warm); cache files are not."""

    def test_game_folder_files_only(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            game = os.path.join(d, "BeamNG.drive", "current", "temp", "beamngEarth", "s")
            cache = os.path.join(d, "cache")
            os.makedirs(game)
            os.makedirs(cache)
            read = []
            with mock.patch.object(staging, "WARM_READ", True), \
                    mock.patch.object(staging, "warm", lambda p: read.append(p)):
                for folder in (game, cache):
                    path = os.path.join(folder, "t.cdae")
                    tmp = staging.tmp_for(path)
                    Path(tmp).write_bytes(b"x")
                    staging.replace(tmp, path)
            self.assertEqual(read, [os.path.join(game, "t.cdae")])
            staging.warm(os.path.join(game, "t.cdae"))          # (reads it; a missing file is no error)
            staging.warm(os.path.join(game, "missing.cdae"))


if __name__ == "__main__":
    unittest.main()
