"""statcache.py: the launch's cached file digests (the up-to-date checks do not re-read whole trees),
and install.py's tree_hash / modtextures.plan reading their files only when they changed."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import statcache  # noqa: E402
import install  # noqa: E402


class FakeDataDir:
    """apppaths in place, its data folder a temporary one."""

    def __init__(self, dd):
        self.dd = dd

    def data_dir(self):
        return self.dd


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dd = os.path.join(self.tmp.name, "data")
        patch = mock.patch.object(statcache, "apppaths", FakeDataDir(self.dd))
        patch.start()
        self.addCleanup(patch.stop)
        self.file = os.path.join(self.tmp.name, "a.bin")
        with open(self.file, "wb") as f:
            f.write(b"hello earth")

    def fresh(self):
        return statcache.Cache()

    def advance_clock(self):
        """The file's mtime moved on: what the stat of a real edit shows (a rewrite inside one clock
        tick, the stat's one blind spot, is not this)."""
        st = os.stat(self.file)
        later = st.st_mtime_ns + statcache_ns_step()
        os.utime(self.file, ns=(later, later))


def statcache_ns_step():
    return 1_000_000_000           # (one second past the last write: every file system sees it)

    def test_digest_matches_the_file_and_is_not_read_again(self):
        with self.fresh() as cache:
            d1 = cache.digest(self.file)
        self.assertEqual(d1, hashlib.sha256(b"hello earth").digest())
        # the cache holds it now; a second pass reads no file (an open of one raises)
        with self.fresh() as cache2, mock.patch("builtins.open", side_effect=OSError("read!")):
            self.assertEqual(cache2.digest(self.file), d1)
            self.assertFalse(cache2.dirty)

    def test_a_changed_file_is_read_and_the_cache_updated(self):
        with self.fresh() as cache:
            d1 = cache.digest(self.file)
        with open(self.file, "wb") as f:
            f.write(b"goodbye earth")
        self.advance_clock()
        with self.fresh() as cache2:
            d2 = cache2.digest(self.file)
        self.assertNotEqual(d1, d2)
        self.assertEqual(d2, hashlib.sha256(b"goodbye earth").digest())
        self.assertTrue(cache2.dirty)

    def test_cache_is_shared_through_the_data_folder(self):
        with self.fresh() as cache:
            cache.digest(self.file)
        real_open = open

        def guarded(path, *a, **k):      # (every file but the cache's own is unreadable)
            if str(path) == self.file:
                raise OSError("read!")
            return real_open(path, *a, **k)

        with mock.patch("builtins.open", guarded), self.fresh() as cache2:
            self.assertEqual(cache2.digest(self.file), hashlib.sha256(b"hello earth").digest())

    def test_gone_files_drop_out(self):
        other = os.path.join(self.tmp.name, "b.bin")
        Path(other).write_text("b")
        with self.fresh() as cache:
            cache.digest(self.file)
        os.remove(self.file)
        with self.fresh() as cache2:
            cache2.digest(other)          # (a miss: the pruned cache is written back)
        with open(os.path.join(self.dd, statcache.FILE), encoding="utf-8") as f:
            self.assertNotIn(self.file, json.load(f))

    def test_no_data_folder_still_hashes(self):
        """No data folder (its path a file): every digest read, nothing saved, no error."""
        blocker = os.path.join(self.tmp.name, "blocker")
        Path(blocker).write_text("x")
        with mock.patch.object(statcache, "apppaths", FakeDataDir(blocker)):
            with statcache.Cache() as cache:
                self.assertEqual(cache.digest(self.file), hashlib.sha256(b"hello earth").digest())


class TreeHashTest(unittest.TestCase):
    def advance_clock(self):
        """one.txt's mtime moved on (what a real edit's stat shows)."""
        full = os.path.join(self.tree, "one.txt")
        st = os.stat(full)
        later = st.st_mtime_ns + 1_000_000_000
        os.utime(full, ns=(later, later))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dd = os.path.join(self.tmp.name, "data")
        self.tree = os.path.join(self.tmp.name, "tree")
        os.makedirs(os.path.join(self.tree, "sub"))
        for name, data in (("one.txt", b"one"), (os.path.join("sub", "two.txt"), b"two")):
            with open(os.path.join(self.tree, name), "wb") as f:
                f.write(data)

    def digest_of(self, path):
        h = hashlib.sha256()
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, path).replace(os.sep, "/")
                h.update(rel.encode())
                with open(full, "rb") as f:
                    h.update(hashlib.sha256(f.read()).digest())
        return h.hexdigest()

    def test_the_digest_is_the_files_names_and_hashes(self):
        self.assertEqual(install.tree_hash(self.tree), self.digest_of(self.tree))

    def test_a_second_pass_reads_no_file(self):
        expected = self.digest_of(self.tree)
        self.assertEqual(install.tree_hash(self.tree), expected)
        real_open = open

        def guarded(path, *a, **k):
            if str(path).replace(os.sep, "/").startswith(self.tree.replace(os.sep, "/")):
                raise OSError("read!")
            return real_open(path, *a, **k)

        with mock.patch("builtins.open", guarded):
            self.assertEqual(install.tree_hash(self.tree), expected)

    def test_a_change_is_seen_again(self):
        first = install.tree_hash(self.tree)
        with open(os.path.join(self.tree, "one.txt"), "wb") as f:
            f.write(b"ONE")
        self.advance_clock()          # (a rewrite inside one clock tick with the same size is the
                                      # one change the stat cannot see; the mod's files change far apart)
        self.assertNotEqual(install.tree_hash(self.tree), first)

    def test_skip_leaves_a_file_out(self):
        with_skip = install.tree_hash(self.tree, skip=("one.txt",))
        self.assertNotEqual(with_skip, install.tree_hash(self.tree))
        h = hashlib.sha256()
        rel = os.path.relpath(os.path.join(self.tree, "sub", "two.txt"), self.tree).replace(os.sep, "/")
        h.update(rel.encode())
        with open(os.path.join(self.tree, "sub", "two.txt"), "rb") as f:
            h.update(hashlib.sha256(f.read()).digest())
        self.assertEqual(with_skip, h.hexdigest())


if __name__ == "__main__":
    unittest.main()
