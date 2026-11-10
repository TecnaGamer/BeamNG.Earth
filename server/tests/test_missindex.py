"""Remembered 404s in a column's miss index (missindex.py, GitHub #1081): record / read / expire / clear, damaged
and cut-short lines, several processes appending to one column, old <y>.404 marker files honoured and migrated (by
hand and by the cache's start-up walk), the cache's bookkeeping (never evicted, cleared), the map's cache view counting
index lines as misses, and the cache's fast clear (moved aside, deleted in the background). No network."""
import multiprocessing
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cache as cachemod  # noqa: E402
import missindex  # noqa: E402
import netfetch  # noqa: E402

T0 = 1_760_000_000.0


def col(root, *parts):
    return os.path.join(root, *parts)


def _append_many(d, y0, n, go=None):
    if go is not None:
        go.wait(30)
    for y in range(y0, y0 + n):
        missindex.record(missindex.marker(d, y), t=T0 + y)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        missindex.reset_memo()
        self.addCleanup(missindex.reset_memo)


class IndexTest(Base):
    def test_record_read_clear(self):
        d = col(self.root, "usgs", "16", "100")
        m = missindex.marker(d, 7)
        self.assertIsNone(missindex.when(m))
        missindex.record(m, t=T0)
        self.assertEqual(missindex.when(m), T0)
        self.assertTrue(missindex.known(m))
        self.assertFalse(os.path.exists(m), "no marker file")
        self.assertEqual(os.listdir(d), [missindex.NAME])
        missindex.record([missindex.marker(d, 8), missindex.marker(d, 9)], t=T0 + 5)
        self.assertEqual(dict(missindex.column(d)), {7: T0, 8: T0 + 5, 9: T0 + 5})
        missindex.record(m, t=T0 + 100)                     # a 404 again (after its TTL): the newer time
        self.assertEqual(missindex.when(m), T0 + 100)
        self.assertTrue(missindex.clear(m))                 # data arrived
        self.assertIsNone(missindex.when(m))
        self.assertFalse(missindex.clear(m), "nothing left to clear: no line written")
        self.assertEqual(missindex.when(missindex.marker(d, 8)), T0 + 5)
        missindex.record(m, t=T0 + 200)                     # gone again
        self.assertEqual(missindex.when(m), T0 + 200)
        with open(os.path.join(d, missindex.NAME), "rb") as f:
            self.assertEqual(f.read().count(b"\n"), 6, "append only")

    def test_ttl_through_netfetch(self):
        d = col(self.root, "usgs", "16", "100")
        m = missindex.marker(d, 7)
        ttl = 30 * 86400
        missindex.record(m, t=T0)
        self.assertTrue(netfetch.miss_alive(m, ttl, now_wall=T0 + ttl - 1))
        self.assertFalse(netfetch.miss_alive(m, ttl, now_wall=T0 + ttl + 1), "expired: asked again")
        self.assertTrue(netfetch.miss_alive(m, None, now_wall=T0 + 1e9), "no TTL: forever")
        self.assertFalse(netfetch.miss_alive(missindex.marker(d, 6), ttl, now_wall=T0))

    def test_damaged_and_cut_short_lines(self):
        self.assertEqual(missindex.parse(b"1 100\n2 200\n3 3"), {1: 100.0, 2: 200.0}, "a cut-short last line")
        self.assertEqual(missindex.parse(b"1 100\nx y\n2\n-4 5\n5 nan\n6 1e99\n7 70 7\n\n8 80\n"), {1: 100.0, 8: 80.0})
        self.assertEqual(missindex.parse(b"1 100\n1 0\n2 5\n"), {2: 5.0}, "a tombstone clears")
        self.assertEqual(missindex.parse(b""), {})
        d = col(self.root, "usgs", "16", "1")
        os.makedirs(d)
        with open(os.path.join(d, missindex.NAME), "wb") as f:
            f.write(b"1 100\n12")                         # a writer killed mid-line
        missindex.record(missindex.marker(d, 34), t=T0)
        self.assertEqual(dict(missindex.column(d)), {1: 100.0, 34: T0}, "the next writer ends the cut line first")

    def test_another_process_lines_are_seen(self):
        d = col(self.root, "usgs", "16", "1")
        missindex.record(missindex.marker(d, 1), t=T0)
        self.assertEqual(set(missindex.column(d)), {1})
        with open(os.path.join(d, missindex.NAME), "ab") as f:    # (another process's append)
            f.write(b"2 1760000001\n")
        self.assertEqual(set(missindex.column(d)), {1, 2}, "the memo follows the file's size / mtime")
        os.remove(os.path.join(d, missindex.NAME))                 # cleared by "Clear cache"
        self.assertEqual(dict(missindex.column(d)), {})
        self.assertIsNone(missindex.when(missindex.marker(d, 1)))

    def test_two_processes_appending(self):
        d = col(self.root, "usgs", "16", "1")
        ctx = multiprocessing.get_context("spawn")
        go = ctx.Event()
        ps = [ctx.Process(target=_append_many, args=(d, i * 1000, 600, go)) for i in range(3)]
        for p in ps:
            p.start()
        threads = [threading.Thread(target=_append_many, args=(d, 5000 + i * 1000, 300, go)) for i in range(2)]
        for t in threads:
            t.start()
        import time
        time.sleep(1.0)                 # (the processes started: all append at once)
        go.set()
        for p in ps:
            p.join(60)
            self.assertEqual(p.exitcode, 0)
        for t in threads:
            t.join()
        got = missindex.column(d)
        want = {y for i in range(3) for y in range(i * 1000, i * 1000 + 600)}
        want |= {y for i in range(2) for y in range(5000 + i * 1000, 5000 + i * 1000 + 300)}
        self.assertEqual(set(got), want, "no process's line lost")
        self.assertTrue(all(got[y] == T0 + y for y in want))

    def test_not_a_tile_marker(self):
        """rangeread's whole-file marker "<dir>/404" stays a file of its own (its mtime)."""
        p = os.path.join(self.root, "404")
        self.assertIsNone(missindex.when(p))
        open(p, "wb").close()
        os.utime(p, (T0, T0))
        self.assertEqual(missindex.when(p), T0)
        with self.assertRaises(ValueError):
            missindex.record(p)


class LegacyTest(Base):
    def old(self, d, y, t=T0):
        os.makedirs(d, exist_ok=True)
        p = missindex.marker(d, y)
        open(p, "wb").close()
        os.utime(p, (t, t))
        return p

    def test_old_markers_honoured_and_cleared(self):
        d = col(self.root, "usgs", "16", "1")
        p = self.old(d, 3)
        self.assertEqual(missindex.when(p), T0)
        self.assertTrue(netfetch.miss_alive(p, 100.0, now_wall=T0 + 50))
        self.assertTrue(missindex.clear(p))
        self.assertFalse(os.path.exists(p), "data arrived: the old marker goes")
        self.assertIsNone(missindex.when(p))

    def test_migrate(self):
        d = col(self.root, "usgs", "16", "1")
        olds = [self.old(d, y, T0 + y) for y in (1, 2, 3)]
        missindex.record(missindex.marker(d, 2), t=T0 + 500)       # already in the index: its line stays
        self.assertEqual(missindex.migrate(d), 3)
        self.assertFalse(any(os.path.exists(p) for p in olds))
        self.assertEqual(dict(missindex.column(d)), {1: T0 + 1, 2: T0 + 500, 3: T0 + 3})
        self.assertEqual(missindex.migrate(d), 0, "nothing left")

    def test_flagged_source_skips_old_markers(self):
        src = col(self.root, "usgs")
        d = col(src, "16", "1")
        p = self.old(d, 3)
        self.assertEqual(missindex.flag_sources(self.root, skip={src}), 0)
        self.assertEqual(missindex.flag_sources(self.root), 1)
        self.assertEqual(missindex.flag_sources(self.root), 0, "once")
        missindex.reset_memo()
        self.assertIsNone(missindex.when(p), "flagged: an old marker file is not looked at")

    def test_cache_walk_migrates(self):
        root = self.root
        olds = [self.old(col(root, "imagery", "usgs", "16", str(x)), y, T0 + y) for x in (1, 2) for y in (5, 6)]
        self.old(col(root, "dem", "mapterhorn", "15", "9"), 4)
        jpg = col(root, "imagery", "eox2023", "14", "1", "1.jpg")
        os.makedirs(os.path.dirname(jpg))
        with open(jpg, "wb") as f:
            f.write(b"x" * 100)
        c = cachemod.Cache(root, start=False)
        c.scan()
        self.assertEqual(c.usage()[1], 6)
        self.assertEqual(c.migrate_misses(), 5)
        self.assertFalse(any(os.path.exists(p) for p in olds))
        for x in (1, 2):
            self.assertEqual(dict(missindex.column(col(root, "imagery", "usgs", "16", str(x)))), {5: T0 + 5, 6: T0 + 6})
        self.assertEqual(sorted(k for k in c.keys()), [
            "dem/mapterhorn/15/9/_misses", "imagery/eox2023/14/1/1.jpg",
            "imagery/usgs/16/1/_misses", "imagery/usgs/16/2/_misses"])
        for src in ("imagery/usgs", "imagery/eox2023", "dem/mapterhorn"):
            self.assertTrue(os.path.exists(col(root, *src.split("/"), missindex.MIGRATED)), src)
        missindex.reset_memo()
        self.assertEqual(missindex.when(missindex.marker(col(root, "imagery", "usgs", "16", "1"), 5)), T0 + 5)
        # a cache left alone (another layout) is not touched
        with open(os.path.join(root, cachemod.VERSION_FILE), "w") as f:
            f.write("99\n")
        p = self.old(col(root, "imagery", "usgs", "16", "3"), 1)
        c2 = cachemod.Cache(root, start=False)
        c2.scan()
        self.assertEqual(c2.migrate_misses(), 0)
        self.assertTrue(os.path.exists(p))


class CacheBookkeepingTest(Base):
    def test_miss_index_never_evicted_but_cleared(self):
        now = [T0]
        c = cachemod.Cache(self.root, cap_gb=0.000001, now=lambda: now[0], start=False)
        c.scan()
        d = col(self.root, "imagery", "usgs", "16", "1")
        missindex.record([missindex.marker(d, y) for y in range(20)], disk=c, t=T0)
        key = "imagery/usgs/16/1/_misses"
        self.assertGreater(c.sizes([key])[0][1], 0)
        now[0] += cachemod.PROTECT_S + 10
        c.evict(target=0)
        self.assertTrue(os.path.exists(os.path.join(d, missindex.NAME)), "never evicted")
        now[0] += cachemod.CLEAR_KEEP_S + 10
        n, _ = c.clear()
        self.assertEqual(n, 1)
        self.assertFalse(os.path.exists(os.path.join(d, missindex.NAME)), "Clear cache removes it")
        self.assertFalse(missindex.known(missindex.marker(d, 3)))

    def test_fast_clear(self):
        root = self.root
        for rel in ("imagery/usgs/16/1/2.jpg", "dem/mapterhorn/12/1/2.webp", "gravity/egm.npy", "netstats/x.json",
                    "mapdata-mirror/v2/a.bin", "logs/server.log"):
            p = col(root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").close()
        moved = cachemod.clear_all(root)
        self.assertEqual(sorted(moved), ["dem", "imagery", "mapdata-mirror"])
        for rel in ("imagery", "dem", "mapdata-mirror"):
            self.assertFalse(os.path.exists(col(root, rel)), rel)
        for rel in ("gravity/egm.npy", "netstats/x.json", "logs/server.log"):
            self.assertTrue(os.path.exists(col(root, *rel.split("/"))), rel)
        self.assertTrue(os.listdir(col(root, cachemod.TRASH)))
        c = cachemod.Cache(root, start=False)
        c.scan()
        self.assertEqual(c.keys(), ["gravity/egm.npy"], "the trash is not counted")
        self.assertEqual(cachemod.sweep_trash(root), 1)
        self.assertFalse(os.path.exists(col(root, cachemod.TRASH)))
        self.assertEqual(cachemod.clear_all(root), [], "nothing left to clear")
        os.makedirs(col(root, "imagery"))
        with open(os.path.join(root, cachemod.VERSION_FILE), "w") as f:
            f.write("99\n")
        self.assertEqual(cachemod.clear_all(root), [], "another layout: left alone")


class CoverMapTest(Base):
    def test_index_lines_count_as_misses(self):
        import covermap

        class FakeCache:
            def __init__(self, keys):
                self._lock = threading.Lock()
                self._entries = dict.fromkeys(keys)
                self._ready_ev = threading.Event()
                self._ready_ev.set()
                self.on_change = None

        import revisit
        revisit.COMPACTED.set()
        d = col(self.root, "dem", "mapterhorn", "16", "5")
        missindex.record([missindex.marker(d, 6), missindex.marker(d, 7)], t=T0)
        old = col(self.root, "dem", "mapterhorn", "16", "5", "8.404")
        open(old, "wb").close()
        keys = ["dem/mapterhorn/16/5/_misses", "dem/mapterhorn/16/5/8.404", "dem/mapterhorn/16/5/9.webp"]
        cm = covermap.CoverMap(FakeCache(keys), self.root)
        self.assertTrue(cm.build(wait=1.0))
        self.addCleanup(cm.close)
        self.assertEqual(cm.idx.count(covermap.DEM), (1, 3))
        missindex.record(missindex.marker(d, 10), t=T0)          # this process: the listener
        missindex.record(missindex.marker(d, 6), t=T0 + 1)       # (again: counted once)
        cm._drain()
        self.assertEqual(cm.idx.count(covermap.DEM), (1, 4))
        missindex.clear(missindex.marker(d, 7))
        cm._drain()
        self.assertEqual(cm.idx.count(covermap.DEM), (1, 3))
        # the old marker migrated: forgotten as a file, kept as a miss
        cm._changed("dem/mapterhorn/16/5/8.404", False)
        missindex.migrate(d)
        cm._drain()
        self.assertEqual(cm.idx.count(covermap.DEM), (1, 3))
        cm._changed("dem/mapterhorn/16/5/_misses", False)       # the index file cleared
        cm._drain()
        self.assertEqual(cm.idx.count(covermap.DEM), (1, 0))


if __name__ == "__main__":
    unittest.main()
