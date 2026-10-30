"""The download cache's size cap (server/cache.py; docs/ROADMAP.md "Caching" 1, 3, 5): LRU order, the
cap and the low-water mark, the protection rules (this session's last hour, gravity/, netstats/, empty
markers), index persistence and damage, the VERSION file, per-source lifetimes, concurrent use from
worker threads, clear, the background thread's pacing, and the hooks in dem.py / imagery.py /
deshadow.py / colormatch.py and earthd's 'cache' messages. No network: fake clock, stub fetcher."""
import asyncio
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cache as cachemod  # noqa: E402
import dem as demmod  # noqa: E402
import earthd  # noqa: E402
import imagery as imagerymod  # noqa: E402
import netfetch  # noqa: E402

DAY = 86400.0
T0 = 1_760_000_000.0        # the fake wall clock's start (2025-10)
MB = 1_000_000


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def put(root, rel, size, mtime=None):
    """A file of `size` bytes at root/rel (mtime: its download time)."""
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"x" * size)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def exists(root, rel):
    return os.path.exists(os.path.join(root, *rel.split("/")))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.clock = Clock()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, cap_gb=None, scan=True):
        c = cachemod.Cache(self.root, cap_gb, now=self.clock, start=False)
        if scan:
            c.scan()
        return c


class LruTest(Base):
    def test_least_recently_used_go_first_down_to_low_water(self):
        """Ten 1 MB files downloaded a day apart (mtime = last use for files the index does not know);
        a cap of 8 MB evicts down to 7.2 MB: the three oldest go, in that order."""
        for i in range(10):
            put(self.root, f"imagery/usgs/16/1/{i}.jpg", MB, T0 - (10 - i) * DAY)
        self.clock.t += 7200            # this session started now; nothing is used yet
        c = self.make(cap_gb=0.008)
        self.assertEqual(c.usage(), (10 * MB, 10))
        n, b = c.evict()
        self.assertEqual((n, b), (3, 3 * MB))
        self.assertEqual([exists(self.root, f"imagery/usgs/16/1/{i}.jpg") for i in range(10)],
                         [False] * 3 + [True] * 7)
        self.assertEqual(c.usage(), (7 * MB, 7))
        self.assertEqual(c.evict(), (0, 0), "under the cap: nothing")

    def test_a_hit_moves_a_file_to_the_back(self):
        """An old file used (read) a while ago in this session outlives newer files that were not."""
        for i in range(4):
            put(self.root, f"dem/mapterhorn/12/1/{i}.webp", MB, T0 - (4 - i) * DAY)
        c = self.make(cap_gb=0.0025)
        self.assertIsNotNone(c.read(os.path.join(self.root, "dem/mapterhorn/12/1/0.webp")))
        self.clock.t += 2 * cachemod.PROTECT_S          # no longer protected, but still the newest use
        c.evict()
        self.assertEqual([exists(self.root, f"dem/mapterhorn/12/1/{i}.webp") for i in range(4)],
                         [True, False, False, True])

    def test_unlimited_and_raised_cap(self):
        for i in range(5):
            put(self.root, f"imagery/eox2023/14/1/{i}.jpg", MB, T0 - DAY)
        c = self.make(cap_gb=0)
        self.assertEqual(c.evict(), (0, 0))
        self.assertTrue(c.set_cap(0.002))
        self.assertFalse(c.set_cap(0.002), "the same cap: no change")
        self.assertEqual(c.cap, 2 * MB)
        self.assertEqual(c.evict()[0], 4)               # 1 MB left (<= 1.8 MB)

    def test_cap_bytes(self):
        self.assertEqual(cachemod.cap_bytes(20), 20_000_000_000)
        self.assertEqual(cachemod.cap_bytes("0"), 0)
        for bad in (-1, float("nan"), float("inf"), 1e300, "x"):
            with self.assertRaises(ValueError):
                cachemod.cap_bytes(bad)


class ProtectionTest(Base):
    def test_rules(self):
        """Never evicted: gravity/, this session's files used within the hour, empty markers; netstats/
        and unknown directories are not even tracked."""
        old = T0 - 30 * DAY
        put(self.root, "gravity/egm2008_rowmajor.npy", 5 * MB, old)
        put(self.root, "netstats/2025-09-01.json", 1000, old)
        put(self.root, "notes/readme.txt", 1000, old)
        put(self.root, "imagery/usgs/16/1/1.404", 0, old)
        put(self.root, "dem/aws-terrarium/13/1/1.png", MB, old)
        put(self.root, "imagery/usgs/16/1/2.jpg", MB, old)
        c = self.make(cap_gb=0.000001)
        self.assertEqual(c.usage(), (7 * MB, 4))        # gravity counted; netstats, notes untracked
        c.used(os.path.join(self.root, "imagery/usgs/16/1/2.jpg"))      # in use now
        c.evict()
        self.assertFalse(exists(self.root, "dem/aws-terrarium/13/1/1.png"))
        for rel in ("gravity/egm2008_rowmajor.npy", "netstats/2025-09-01.json", "notes/readme.txt",
                    "imagery/usgs/16/1/1.404", "imagery/usgs/16/1/2.jpg"):
            self.assertTrue(exists(self.root, rel), rel)
        self.clock.t += cachemod.PROTECT_S + 1          # an hour later: no longer protected
        c.evict()
        self.assertFalse(exists(self.root, "imagery/usgs/16/1/2.jpg"))
        self.assertTrue(exists(self.root, "gravity/egm2008_rowmajor.npy"))

    def test_a_use_during_eviction_wins(self):
        """The protection is checked again under the lock for every file: a producer's used() after the
        pass sorted its candidates keeps the file."""
        for i in range(3):
            put(self.root, f"imagery/usgs/16/1/{i}.jpg", MB, T0 - DAY)
        c = self.make(cap_gb=0.0001)
        real = c._remove

        def remove(key, floor, empty_too):
            if key.endswith("0.jpg"):
                c.used(os.path.join(self.root, key))    # read by a tile worker just now
            return real(key, floor, empty_too)
        c._remove = remove
        c.evict()
        self.assertEqual([exists(self.root, f"imagery/usgs/16/1/{i}.jpg") for i in range(3)], [True, False, False])

    def test_previous_sessions_uses_are_not_protected(self):
        """Last uses from the index (an earlier server) do not protect: only this process's."""
        put(self.root, "imagery/usgs/16/1/0.jpg", MB, T0 - DAY)
        c = self.make()
        c.used(os.path.join(self.root, "imagery/usgs/16/1/0.jpg"))
        c.save()
        self.clock.t += 600                             # a restart ten minutes later
        c2 = self.make(cap_gb=0.0001)
        self.assertEqual(c2.evict(), (1, MB))
        # times from a clock that ran ahead (index or mtime) do not protect either
        put(self.root, "imagery/usgs/16/1/1.jpg", MB, T0 + DAY)
        Path(self.root, cachemod.INDEX_FILE).write_text(
            f"{cachemod.INDEX_HEADER}\n{int(T0 + DAY)}\t{MB}\timagery/usgs/16/1/1.jpg\n")
        put(self.root, "imagery/usgs/16/1/2.jpg", MB, T0 + DAY)
        self.assertEqual(self.make(cap_gb=0.0001).evict(), (2, 2 * MB))


class IndexTest(Base):
    def test_last_uses_and_cap_persist(self):
        for i in range(3):
            put(self.root, f"imagery/usgs/16/1/{i}.jpg", MB, T0 - 10 * DAY)
        c = self.make(cap_gb=50)
        self.clock.t -= DAY                             # (a use recorded "yesterday")
        c.used(os.path.join(self.root, "imagery/usgs/16/1/0.jpg"))
        self.clock.t += DAY
        c.set_cap(5)
        self.assertTrue(c.save())
        self.assertFalse(c.save(), "nothing changed since")
        text = Path(self.root, cachemod.INDEX_FILE).read_text()
        self.assertTrue(text.startswith(cachemod.INDEX_HEADER + "\tcap=5000000000\n"), text[:80])
        self.clock.t += 2 * cachemod.PROTECT_S
        c2 = self.make()
        self.assertEqual(c2.cap, 5_000_000_000, "the mod's cap survives a restart")
        self.assertEqual(c2._entries["imagery/usgs/16/1/0.jpg"][1], T0 - DAY)
        self.assertEqual(c2._entries["imagery/usgs/16/1/1.jpg"][1], T0 - 10 * DAY)
        self.assertEqual(self.make(cap_gb=7).cap, 7_000_000_000, "--cache-cap overrides the saved cap")
        c2.set_cap(0.0015)
        c2.evict()
        self.assertTrue(exists(self.root, "imagery/usgs/16/1/0.jpg"), "the used file outlives the others")

    def test_vanished_files_and_newer_downloads(self):
        """Index entries without a file are dropped; a file downloaded again after its last use (by a run
        without the index) takes its mtime."""
        p = put(self.root, "dem/mapterhorn/12/1/1.webp", MB, T0 - 5 * DAY)
        put(self.root, "dem/mapterhorn/12/1/2.webp", MB, T0 - 5 * DAY)
        c = self.make()
        c.save()
        os.remove(p)
        put(self.root, "dem/mapterhorn/12/1/2.webp", 2 * MB, T0 - DAY)
        c2 = self.make()
        self.assertNotIn("dem/mapterhorn/12/1/1.webp", c2._entries)
        self.assertEqual(c2._entries["dem/mapterhorn/12/1/2.webp"], [2 * MB, T0 - DAY])
        self.assertEqual(c2.usage(), (2 * MB, 1))

    def test_damaged_index(self):
        put(self.root, "imagery/usgs/16/1/0.jpg", MB, T0 - 3 * DAY)
        put(self.root, "imagery/usgs/16/1/1.jpg", MB, T0 - 3 * DAY)
        idx = Path(self.root, cachemod.INDEX_FILE)
        idx.write_bytes(b"\x00\xffgarbage\nmore\tgarbage\n")
        with self.assertLogs("earthd.cache", "WARNING"):
            c = self.make()
        self.assertEqual(c._entries["imagery/usgs/16/1/0.jpg"][1], T0 - 3 * DAY, "the mtime instead")
        self.assertEqual(c.cap, cachemod.cap_bytes(cachemod.DEFAULT_CAP_GB))
        # a good header with damaged lines: those are skipped, the rest is read; a line naming a path
        # outside the managed directories (or with '..') never becomes an entry
        idx.write_text(f"{cachemod.INDEX_HEADER}\tcap=oops\n"
                       f"{int(T0 - DAY)}\t{MB}\timagery/usgs/16/1/0.jpg\n"
                       "not a line\n"
                       f"{int(T0)}\t-5\timagery/usgs/16/1/1.jpg\n"
                       f"{int(T0)}\t5\tdem/../../outside.txt\n"
                       f"{int(T0)}\t5\tnetstats/x.json\n"
                       f"{int(T0 - DAY)}\t{MB}\timagery/usgs/16/1/1.jp")       # cut short (no file)
        with self.assertLogs("earthd.cache", "WARNING") as logs:
            c = self.make()
        self.assertIn("4 damaged lines", "\n".join(logs.output))
        self.assertEqual(c._entries["imagery/usgs/16/1/0.jpg"][1], T0 - DAY)
        self.assertEqual(c._entries["imagery/usgs/16/1/1.jpg"][1], T0 - 3 * DAY)
        self.assertEqual(sorted(c._entries), ["imagery/usgs/16/1/0.jpg", "imagery/usgs/16/1/1.jpg"])
        self.assertEqual(c.cap, cachemod.cap_bytes(cachemod.DEFAULT_CAP_GB))
        self.assertTrue(c.save())
        self.assertTrue(Path(self.root, cachemod.INDEX_FILE).read_text().startswith(cachemod.INDEX_HEADER))

    def test_non_finite_numbers_are_damage(self):
        """'nan' / '-inf' times and a cap too large for a float are damaged values, not data: a nan last
        use made every later save() raise (int(nan)), so the index was never written again and a clear
        (which saves) never answered the page; an overflowing cap stopped the server at start."""
        for i in range(3):
            put(self.root, f"imagery/usgs/16/1/{i}.jpg", MB, T0 - 3 * DAY)
        Path(self.root, cachemod.INDEX_FILE).write_text(
            f"{cachemod.INDEX_HEADER}\tcap={'9' * 400}\n"
            f"nan\t{MB}\timagery/usgs/16/1/0.jpg\n"
            f"-inf\t{MB}\timagery/usgs/16/1/1.jpg\n"
            f"{int(T0 - DAY)}\t{MB}\timagery/usgs/16/1/2.jpg\n")
        with self.assertLogs("earthd.cache", "WARNING") as logs:
            c = self.make()
        self.assertIn("2 damaged lines", "\n".join(logs.output))
        self.assertEqual(c.cap, cachemod.cap_bytes(cachemod.DEFAULT_CAP_GB))
        self.assertEqual([c._entries[f"imagery/usgs/16/1/{i}.jpg"][1] for i in range(3)],
                         [T0 - 3 * DAY, T0 - 3 * DAY, T0 - DAY], "the mtimes instead")
        self.assertTrue(c.save())
        self.assertEqual(c.clear(), (3, 3 * MB))


class VersionTest(Base):
    def test_version_written_for_a_cache_without_one(self):
        put(self.root, "imagery/usgs/16/1/0.jpg", MB, T0 - DAY)
        c = self.make()
        self.assertTrue(c.managed)
        self.assertEqual(Path(self.root, "VERSION").read_text().strip(), str(cachemod.LAYOUT_VERSION))
        sub = os.path.join(self.root, "fresh")
        cachemod.Cache(sub, now=self.clock, start=False)
        self.assertTrue(os.path.isfile(os.path.join(sub, "VERSION")), "a new root is created with its VERSION")

    def test_empty_version_is_written_again(self):
        """An empty VERSION (a write cut short, or a second server reading it while the first one writes)
        is no newer layout: it is written again (atomically) and the cache is managed."""
        Path(self.root, "VERSION").write_text("")
        put(self.root, "imagery/usgs/16/1/0.jpg", MB, T0 - DAY)
        c = self.make(cap_gb=0.0001)
        self.assertTrue(c.managed)
        self.assertEqual(Path(self.root, "VERSION").read_text().strip(), str(cachemod.LAYOUT_VERSION))
        self.assertEqual(c.evict(), (1, MB))
        self.assertEqual([n for n in os.listdir(self.root) if n.endswith(".tmp")], [], "no temporary file left")

    def test_unknown_version_is_left_alone(self):
        for text in ("2\n", "banana"):
            with self.subTest(text=text):
                Path(self.root, "VERSION").write_text(text)
                put(self.root, "imagery/usgs/16/1/0.jpg", MB, T0 - 30 * DAY)
                with self.assertLogs("earthd.cache", "WARNING"):
                    c = self.make(cap_gb=0.0001)
                self.assertFalse(c.managed)
                self.assertEqual(c.usage(), (MB, 1), "still counted for the page")
                self.assertEqual(c.evict(), (0, 0))
                self.assertEqual(c.clear(), (0, 0))
                c.used(os.path.join(self.root, "imagery/usgs/16/1/0.jpg"))
                self.assertFalse(c.save())
                self.assertFalse(os.path.exists(os.path.join(self.root, cachemod.INDEX_FILE)))
                self.assertTrue(exists(self.root, "imagery/usgs/16/1/0.jpg"))
                self.assertEqual(Path(self.root, "VERSION").read_text(), text, "VERSION untouched")
                self.assertFalse(c.message()["managed"])


class LifetimeTest(Base):
    def test_files_past_their_source_lifetime_go_at_the_walk(self):
        put(self.root, "imagery/eox2023/14/1/0.jpg", MB, T0 - 40 * DAY)
        put(self.root, "imagery/eox2023/14/1/1.jpg", MB, T0 - 10 * DAY)
        put(self.root, "imagery/usgs/16/1/0.jpg", MB, T0 - 400 * DAY)
        self.assertEqual(cachemod.LIFETIMES, {}, "no source limits caching time today")
        with mock.patch.dict(cachemod.LIFETIMES, {"imagery/eox2023/": 30 * DAY}):
            c = self.make()
        self.assertFalse(exists(self.root, "imagery/eox2023/14/1/0.jpg"))
        self.assertTrue(exists(self.root, "imagery/eox2023/14/1/1.jpg"))
        self.assertTrue(exists(self.root, "imagery/usgs/16/1/0.jpg"))
        self.assertEqual((c.usage(), c.stats["expired"]), ((2 * MB, 2), 1))


class ClearTest(Base):
    def test_clear(self):
        old = T0 - 3 * DAY
        put(self.root, "gravity/egm2008.tar.bz2", 3 * MB, old)
        put(self.root, "netstats/2025-10-01.json", 100, old)
        put(self.root, "imagery/usgs/16/1/0.jpg", MB, old)
        put(self.root, "imagery/usgs/16/1/1.404", 0, old)
        put(self.root, "dem/mapterhorn/12/1/0.webp", MB, old)
        put(self.root, "colormatch/v1/usgs/12/1/0.npz", MB, old)
        put(self.root, "deshadow/usgs/12/1/0.json", 100, old)
        keep = put(self.root, "imagery/usgs/16/1/9.jpg", MB, old)
        c = self.make()
        c.used(keep)                                    # on screen now
        n, b = c.clear()
        self.assertEqual((n, b), (5, 3 * MB + 100))
        for rel in ("imagery/usgs/16/1/0.jpg", "imagery/usgs/16/1/1.404", "dem/mapterhorn/12/1/0.webp",
                    "colormatch/v1/usgs/12/1/0.npz", "deshadow/usgs/12/1/0.json"):
            self.assertFalse(exists(self.root, rel), rel)
        for rel in ("gravity/egm2008.tar.bz2", "netstats/2025-10-01.json", "imagery/usgs/16/1/9.jpg"):
            self.assertTrue(exists(self.root, rel), rel)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "imagery/usgs/16/1")), "directories stay")
        self.assertEqual(c.usage(), (4 * MB, 2))
        m = c.message(cleared=b)
        self.assertEqual(m["clearedBytes"], 3 * MB + 100)
        self.assertTrue(os.path.exists(os.path.join(self.root, cachemod.INDEX_FILE)), "saved after a clear")

    def test_mapdata_is_managed(self):
        """mapdata/ (mapdata.py: layer files, range blocks) is tracked, evicted LRU and cleared like the
        other producers; its 404 markers stay through eviction, its files being written are not tracked."""
        old = T0 - 3 * DAY
        put(self.root, "mapdata/v1/overture/2026-09-23.1/roads/14/1/0.npz", MB, old - DAY)
        put(self.root, "mapdata/raw/overture/2026-09-23.1/part-0/0.blk", MB, old)
        put(self.root, "mapdata/raw/overture/2026-09-23.1/part-1/404", 0, old - 2 * DAY)
        put(self.root, "mapdata/v1/overture/2026-09-23.1/roads/14/1/1.npz.1.2.tmp.npz", MB, T0)
        put(self.root, "mapdata/raw/worldcover/v200/x/0.blk.1.2.tmp", MB, T0)
        put(self.root, "imagery/usgs/16/1/0.jpg", MB, T0 - DAY)
        self.clock.t += 7200
        c = self.make(cap_gb=0.0025)
        self.assertEqual(c.usage(), (3 * MB, 4), "the two mapdata files, its 404 marker, the imagery tile")
        n, b = c.evict()
        self.assertEqual((n, b), (1, MB), "the least recently used goes first, the empty marker never")
        self.assertFalse(exists(self.root, "mapdata/v1/overture/2026-09-23.1/roads/14/1/0.npz"))
        self.assertTrue(exists(self.root, "mapdata/raw/overture/2026-09-23.1/part-1/404"))
        self.assertTrue(exists(self.root, "mapdata/v1/overture/2026-09-23.1/roads/14/1/1.npz.1.2.tmp.npz"),
                        "a file being written is not evicted")
        c.clear()
        self.assertFalse(exists(self.root, "mapdata/raw/overture/2026-09-23.1/part-0/0.blk"), "cleared")
        self.assertFalse(exists(self.root, "mapdata/raw/overture/2026-09-23.1/part-1/404"), "markers too")

    def test_clear_keeps_everything_this_session_used(self):
        """Clear keeps the current session's files (ROADMAP "Caching": "everything except gravity/ and the
        current session's files"), also those used more than an hour ago, which eviction may take: a
        tile read at the start of a long drive may still be in the server's memory caches."""
        old = T0 - 3 * DAY
        early = put(self.root, "imagery/usgs/16/1/0.jpg", MB, old)
        put(self.root, "imagery/usgs/16/1/1.jpg", MB, old)
        c = self.make()
        c.used(early)                                   # at the start of the session
        self.clock.t += 2 * cachemod.PROTECT_S
        self.assertEqual(c.clear(), (1, MB))
        self.assertTrue(exists(self.root, "imagery/usgs/16/1/0.jpg"), "used this session: kept")
        self.assertFalse(exists(self.root, "imagery/usgs/16/1/1.jpg"))
        c.set_cap(0.0001)
        self.assertEqual(c.evict(), (1, MB), "eviction protects the last hour only")

    def test_clear_waits_for_the_walk(self):
        put(self.root, "imagery/usgs/16/1/0.jpg", MB, T0 - DAY)
        c = self.make(scan=False)
        self.assertFalse(c.ready)
        self.assertEqual(c.clear(wait=0.01), (0, 0))
        self.assertEqual(c.evict(), (0, 0))
        threading.Timer(0.05, c.scan).start()
        self.assertEqual(c.clear(wait=10), (1, MB))


class ConcurrencyTest(Base):
    def test_workers_and_eviction_at_once(self):
        """Eight producer threads write and read 200 files each while eviction passes and saves run: the
        byte count always matches the entries, every entry's file exists, and nothing a thread used in
        this session is gone; old files read while the passes delete them come back whole or as None
        (downloaded again), never as an error."""
        for i in range(400):
            put(self.root, f"dem/mapterhorn/12/9/{i}.webp", 700, T0 - DAY)
        c = self.make(cap_gb=0.05)
        errors = []
        old_reads = [0, 0]
        stop = threading.Event()

        def worker(w):
            try:
                for i in range(200):
                    p = os.path.join(self.root, "imagery", "usgs", "16", str(w), f"{i}.jpg")
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    data = os.urandom(1000 + i)
                    with open(p, "wb") as f:
                        f.write(data)
                    c.wrote(p, len(data))
                    got = c.read(os.path.join(self.root, "imagery", "usgs", "16", str(w), f"{i // 2}.jpg"))
                    if got is None:
                        errors.append(("missing", w, i))
                    got = c.read(os.path.join(self.root, "dem", "mapterhorn", "12", "9", f"{(w * 50 + i) % 400}.webp"))
                    if got is not None and len(got) != 700:
                        errors.append(("torn", w, i))
                    old_reads[got is None] += 1
            except Exception as e:      # pragma: no cover - reported below
                errors.append(e)

        def evictor():
            while not stop.is_set():
                c.evict(target=0)
                c.save()
        ts = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
        ev = threading.Thread(target=evictor)
        ev.start()
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        stop.set()
        ev.join()
        self.assertEqual(errors, [])
        with c._lock:
            self.assertEqual(c._used, sum(e[0] for e in c._entries.values()))
            self.assertEqual(sum(k.startswith("imagery/") for k in c._entries), 1600, "used this session: kept")
            self.assertTrue(all(exists(self.root, k) for k in c._entries))
        self.assertEqual(sum(old_reads), 1600)
        self.clock.t += cachemod.PROTECT_S + 1
        c.evict(target=0)
        self.assertEqual(c.usage(), (0, 0))

    def test_uses_during_the_walk(self):
        """Files used or written while the start-up walk runs keep their size and last use."""
        for i in range(50):
            put(self.root, f"dem/mapterhorn/12/1/{i}.webp", 1000, T0 - DAY)
        c = self.make(scan=False)
        c.used(os.path.join(self.root, "dem/mapterhorn/12/1/0.webp"))           # size not known yet
        new = put(self.root, "dem/mapterhorn/12/2/0.webp", 500)
        c.wrote(new, 500)
        c.scan()
        self.assertEqual(c._entries["dem/mapterhorn/12/1/0.webp"], [1000, T0])
        self.assertEqual(c._entries["dem/mapterhorn/12/2/0.webp"][0], 500)
        self.assertEqual(c.usage(), (50 * 1000 + 500, 51))

    def test_the_message_never_waits_for_the_lock(self):
        """earthd builds the 'cache' message on the event loop (after hello, every 10 s while the page is
        open): it must not wait for the lock, which an eviction pass's sort or an index save over
        ~700k files holds for a few tenths of a second."""
        put(self.root, "dem/mapterhorn/12/1/0.webp", 1000, T0 - DAY)
        c = self.make()
        got = []
        with c._lock:                   # a pass in progress
            t = threading.Thread(target=lambda: got.append(c.message()), daemon=True)
            t.start()
            t.join(2.0)
            self.assertFalse(t.is_alive(), "message() waited for the lock")
        self.assertEqual((got[0]["usedBytes"], got[0]["files"]), (1000, 1))


class ThreadTest(Base):
    def test_tick_paces_eviction_and_saves(self):
        for i in range(10):
            put(self.root, f"imagery/usgs/16/1/{i}.jpg", MB, T0 - DAY)
        c = self.make(cap_gb=0.005)
        c.tick()
        self.assertEqual(c.usage()[1], 4, "over the cap: evicted at once after the walk")
        for i in range(10, 14):
            c.wrote(put(self.root, f"imagery/usgs/16/1/{i}.jpg", MB), MB)
            c._entries[f"imagery/usgs/16/1/{i}.jpg"][1] = T0 - DAY         # (as if from an earlier session)
        self.clock.t += cachemod.EVICT_S - 1
        c.tick()
        self.assertEqual(c.usage()[1], 8, "the next pass waits EVICT_S")
        self.assertFalse(os.path.exists(os.path.join(self.root, cachemod.INDEX_FILE)), "SAVE_S not yet")
        self.clock.t += 1
        c.tick()
        self.assertEqual(c.usage()[1], 4)
        self.clock.t += cachemod.SAVE_S
        c.tick()
        self.assertTrue(os.path.exists(os.path.join(self.root, cachemod.INDEX_FILE)), "SAVE_S passed: saved")

    def test_background_thread(self):
        put(self.root, "imagery/usgs/16/1/0.jpg", MB, T0 - DAY)
        c = cachemod.Cache(self.root, 0.0001, now=self.clock)
        self.assertTrue(c._ready_ev.wait(10))
        for _ in range(200):                            # the first pass follows the walk at once
            if c.usage()[1] == 0:
                break
            threading.Event().wait(0.01)
        self.assertEqual(c.usage(), (0, 0))
        c.close()
        self.assertFalse(c._thread.is_alive())


# --- the producers -------------------------------------------------------------------------------
class StubFetcher:
    """netfetch.Fetcher's surface for Dem / Imagery: 200 with `body` unless the URL is in `missing`."""

    def __init__(self, body, missing=()):
        self.body, self.missing, self.calls = body, set(missing), []

    def register_chain(self, name, urls):
        pass

    def miss_alive(self, path, url):
        return os.path.exists(path)

    def get(self, url, kind="demand", headers=None):
        self.calls.append(url)
        if url in self.missing:
            raise netfetch.NotFound("404", url=url)
        return types.SimpleNamespace(body=self.body, elapsed=0.01)


class ProducerTest(Base):
    def test_dem_hits_writes_markers_and_eviction(self):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (512, 512), (128, 0, 0)).save(buf, "WEBP", lossless=True)
        aws = demmod.AWS["url"].format(z=13, x=0, y=0)
        f = StubFetcher(buf.getvalue(), missing={aws})
        c = self.make()
        d = demmod.Dem(os.path.join(self.root, "dem"), fetch_workers=1, fetcher=f)
        self.assertIs(d.disk, cachemod.NONE, "no bookkeeping by default")
        d.disk = c
        d._download(demmod.MAPTERHORN, 12, 1, 2)
        self.assertEqual(c._entries["dem/mapterhorn/12/1/2.webp"][0], len(buf.getvalue()))
        self.assertIsNone(d._download(demmod.AWS, 13, 0, 0))
        self.assertEqual(c._entries["dem/aws-terrarium/13/0/0.404"][0], 0, "the marker: tracked, empty")
        self.clock.t += 100
        d._download(demmod.MAPTERHORN, 12, 1, 2)                # a disk hit: last use now
        self.assertEqual((len(f.calls), c._entries["dem/mapterhorn/12/1/2.webp"][1]), (2, T0 + 100))
        self.clock.t += cachemod.PROTECT_S + 1
        c.evict(target=0)
        self.assertFalse(exists(self.root, "dem/mapterhorn/12/1/2.webp"))
        d._download(demmod.MAPTERHORN, 12, 1, 2)                # evicted: downloaded again
        self.assertEqual(len(f.calls), 3)
        # an undecodable tile leaves the cache and the bookkeeping
        with self.assertRaises(netfetch.BadResponse):
            d._decode(demmod.MAPTERHORN, 12, 1, 2, b"junk")
        self.assertNotIn("dem/mapterhorn/12/1/2.webp", c._entries)
        d.pool.shutdown()

    def test_imagery_hits_and_writes(self):
        f = StubFetcher(b"\xff\xd8 not decoded here")
        c = self.make()
        im = imagerymod.Imagery(os.path.join(self.root, "imagery"), ("usgs",), fetch_workers=1, fetcher=f)
        im.disk = c
        src = imagerymod.SOURCES["usgs"]
        im._download(src, 16, 1, 2)
        self.assertIn("imagery/usgs/16/1/2.jpg", c._entries)
        self.clock.t += 50
        im._download(src, 16, 1, 2)
        self.assertEqual((len(f.calls), c._entries["imagery/usgs/16/1/2.jpg"][1]), (1, T0 + 50))
        im._discard(src, 16, 1, 2)
        self.assertNotIn("imagery/usgs/16/1/2.jpg", c._entries)
        im.close()

    def test_region_caches(self):
        """The de-shadow and colour-seam region files are tracked through their own write / read points
        (_atomic_write + disk.wrote / disk.used), and a de-shadow region whose grids were evicted is
        computed again instead of staying "pending"."""
        import colormatch as colormatchmod
        import deshadow as deshadowmod
        c = self.make()
        cm = colormatchmod.ColorMatch(self.root)
        cm.disk = c
        try:
            cm._store((1, 2), "field", colormatchmod.EMPTY, {})        # the "no seams" marker (JSON)
            self.assertIn("colormatch/v1/usgs/12/1/2.none", c._entries)
            self.clock.t += 10
            self.assertIsNotNone(cm._load((1, 2), "field"))
            self.assertEqual(c._entries["colormatch/v1/usgs/12/1/2.none"][1], T0 + 10)
        finally:
            cm.close()
        ds = deshadowmod.Deshadow(self.root, dem=None, mode="full")
        ds.disk = c
        try:
            jp, npz = ds._paths((5, 6))
            deshadowmod._atomic_write(npz, lambda fh: np.savez_compressed(fh, grids=np.zeros((2, 2, 2), np.uint8)))
            deshadowmod._atomic_write(jp, lambda fh: fh.write(
                b'{"version": %d, "ok": true, "cast": false}' % deshadowmod.VERSION))
            with ds._lock:
                P = ds._load_params((5, 6))
            self.assertTrue(P["ok"])
            self.assertIn("deshadow/usgs/12/5/6.json", c._entries)
            r = ds.region((5, 6), wait=False)
            self.assertIsNotNone(r)
            self.assertIn("deshadow/usgs/12/5/6.npz", c._entries)
            with ds._lock:
                ds._grids.clear()
            os.remove(npz)                              # evicted
            self.assertIsNone(ds.region((5, 6), wait=False))
            self.assertNotIn((5, 6), ds._params, "dropped: queued again on the next request")
        finally:
            ds.close()


# --- earthd ------------------------------------------------------------------------------------------
class _Writer:
    def __init__(self):
        import json
        self.json, self.msgs = json, []

    def write(self, data):
        self.msgs.append(self.json.loads(data[4:].decode()))

    def is_closing(self):
        return False

    def get_extra_info(self, name):
        return ("127.0.0.1", 0)


class ServerCacheTest(Base):
    def run_conn(self, server, script):
        async def go():
            w = _Writer()
            c = earthd.Connection(server, None, w)
            await script(c, w)
            return w.msgs
        return asyncio.run(go())

    def test_messages(self):
        put(self.root, "cache/imagery/usgs/16/1/0.jpg", MB, T0 - DAY)
        put(self.root, "cache/gravity/egm2008_rowmajor.npy", 2 * MB, T0 - DAY)
        disk = cachemod.Cache(os.path.join(self.root, "cache"), 20, now=self.clock, start=False)
        disk.scan()
        s = earthd.Server(self.root, os.path.join(self.root, "cache"), imagery=("usgs",), cache=disk)
        self.assertIs(s.dem.disk, disk)
        self.assertIs(s.imagery.disk, disk)
        hello = {"type": "hello", "protocol": earthd.PROTOCOL, "format": 4, "modVersion": "0.4.0"}

        async def script(c, w):
            c.dispatch(hello)
            c.dispatch({"type": "cache", "capGb": 5})
            c.dispatch({"type": "cache"})
            c.dispatch({"type": "cache", "clear": True})
            for _ in range(200):
                if any("clearedBytes" in m for m in w.msgs):
                    break
                await asyncio.sleep(0.01)
        msgs = self.run_conn(s, script)
        cache_msgs = [m for m in msgs if m["type"] == "cache"]
        self.assertEqual([m["type"] for m in msgs[:2]], ["hello", "cache"], "the usage follows hello")
        self.assertEqual(cache_msgs[0], {"type": "cache", "v": 1, "usedBytes": 3 * MB, "capBytes": 20_000_000_000,
                                         "files": 2, "ready": True, "managed": True})
        self.assertEqual(cache_msgs[1]["capBytes"], 5_000_000_000)
        self.assertEqual(len(cache_msgs), 4)
        self.assertEqual((cache_msgs[3]["clearedBytes"], cache_msgs[3]["usedBytes"]), (MB, 2 * MB))
        self.assertFalse(exists(self.root, "cache/imagery/usgs/16/1/0.jpg"))
        s.imagery.close()

    def test_bad_cap_and_default_server(self):
        disk = cachemod.Cache(os.path.join(self.root, "c1"), 20, now=self.clock, start=False)
        s = earthd.Server(self.root, os.path.join(self.root, "c1"), imagery=None, cache=disk)
        for bad in ({"capGb": "5"}, {"capGb": True}, {"capGb": -1}):
            with self.assertRaises(ValueError):
                earthd.Connection.on_cache(types.SimpleNamespace(s=s, send=lambda m: None), bad)
        self.assertEqual(disk.cap, 20_000_000_000)
        # Server()'s default: no Cache, no 'cache' message, a mod's 'cache' is ignored
        s0 = earthd.Server(self.root, os.path.join(self.root, "c2"), imagery=None)
        self.assertIsNone(s0.cache)
        self.assertIs(s0.dem.disk, cachemod.NONE)

        async def script(c, w):
            c.dispatch({"type": "hello", "protocol": earthd.PROTOCOL, "format": 4, "modVersion": "0.4.0"})
            c.dispatch({"type": "cache", "capGb": 5, "clear": True})
        msgs = self.run_conn(s0, script)
        self.assertEqual([m["type"] for m in msgs], ["hello"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "c2", cachemod.VERSION_FILE)),
                         "Server() alone writes nothing into the cache root")

    @unittest.skip("shared world building, commented out 2026-10-27 (off since 2026-10-23; the user: not coming back for a while): earthd.share_port")
    def test_share_port(self):
        """The BeamMP group service's port: this machine's BNGE_SHARE_PORT, else the BeamMP server's, else 47621."""
        self.assertEqual(earthd.share_port(None, ""), earthd.peerworkmod.SHARE_PORT)
        self.assertEqual(earthd.share_port(50000, ""), 50000)
        self.assertEqual(earthd.share_port(50000, "51000"), 51000)
        self.assertEqual(earthd.share_port(80, "x"), earthd.peerworkmod.SHARE_PORT, "unusable ones skipped")

    def test_cli(self):
        self.assertEqual(earthd.parse_cache_cap("12.5"), 12.5)
        self.assertEqual(earthd.parse_cache_cap("0"), 0.0)
        import argparse
        for bad in ("-1", "nan", "lots"):
            with self.assertRaises(argparse.ArgumentTypeError):
                earthd.parse_cache_cap(bad)



class PruneDirTest(unittest.TestCase):
    def test_oldest_go_first_recent_stay(self):
        import tempfile, shutil
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        now = 1_000_000.0
        for i in range(10):
            p = os.path.join(root, f"d{i % 3}", f"{i}.raw.npy")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(b"x" * 100)
            os.utime(p, (now - 10000 + i * 100, now - 10000 + i * 100))
        keep = os.path.join(root, "other.npz")
        with open(keep, "wb") as f:
            f.write(b"x" * 5000)
        os.utime(keep, (0, 0))
        n, left = cachemod.prune_dir(root, 500, keep_s=600, match=lambda n: n.endswith(".raw.npy"), now=now)
        self.assertEqual((n, left), (6, 400), "down to 90 % of the cap, the oldest first")
        self.assertTrue(os.path.exists(keep), "files not matched stay")
        self.assertEqual(sorted(os.listdir(os.path.join(root, "d0"))), ["6.raw.npy", "9.raw.npy"])
        # a recent file stays even over the cap
        n, left = cachemod.prune_dir(root, 50, keep_s=10 ** 6, match=lambda n: n.endswith(".raw.npy"), now=now)
        self.assertEqual(n, 0)

    def test_touch_once(self):
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        self.addCleanup(os.remove, path)
        os.utime(path, (1, 1))
        cachemod.touch_once(path)
        self.assertGreater(os.path.getmtime(path), 1000)
        os.utime(path, (1, 1))
        cachemod.touch_once(path)
        self.assertEqual(os.path.getmtime(path), 1, "once per process")


class ChangeHookTest(Base):
    """on_change (the map's cache view, covermap.py): called for keys new to the index and keys gone from it, never for
    a repeat hit; the view's index follows the cache through writes, evictions, forgets and clears."""

    def test_hook_only_on_new_and_removed_keys(self):
        put(self.root, "dem/mapterhorn/12/1/1.webp", MB, T0 - 3 * DAY)
        c = self.make(cap_gb=0)
        seen = []
        c.on_change = lambda key, added: seen.append((key, added))
        c.used(os.path.join(self.root, "dem/mapterhorn/12/1/1.webp"))          # a repeat hit: nothing
        c.read(os.path.join(self.root, "dem/mapterhorn/12/1/1.webp"))
        self.assertEqual(seen, [])
        p = put(self.root, "imagery/usgs/16/2/3.jpg", 1000)
        c.wrote(p)
        c.wrote(p)                                                             # replaced: no second call
        self.assertEqual(seen, [("imagery/usgs/16/2/3.jpg", True)])
        c.forget(p)
        self.assertEqual(seen[-1], ("imagery/usgs/16/2/3.jpg", False))
        c.forget(p)
        self.assertEqual(len(seen), 2, "forgetting an unknown key: nothing")

    def test_cover_index_stays_in_sync(self):
        import covermap
        import revisit
        revisit.COMPACTED.set()
        for i in range(6):
            put(self.root, f"imagery/usgs/16/100/{i}.jpg", MB, T0 - (10 - i) * DAY)
        c = self.make(cap_gb=0.0045)
        cm = covermap.CoverMap(c, self.root)
        self.assertTrue(cm.build(wait=1.0))
        self.assertEqual(cm.idx.count(covermap.IMG), (6, 0))
        p = put(self.root, "imagery/usgs/16/100/9.jpg", 1000)
        c.wrote(p)
        miss = put(self.root, "imagery/usgs/16/100/8.404", 0)
        c.wrote(miss, 0)
        c.evict()                       # 2 of the old MB files go (down to 90 % of 4.5 MB)
        cm._drain()
        self.assertEqual(cm.idx.count(covermap.IMG), (5, 1))
        c.clear()
        cm._drain()
        self.assertEqual(cm.idx.count(covermap.IMG), (1, 1), "the older files go, this session's two stay")


class PinTest(Base):
    """Saved areas (areas.py): pinned keys are skipped by evict() and clear(); pinnedBytes in the 'cache' message."""

    def test_pinned_files_survive_eviction_and_clear(self):
        for i in range(6):
            put(self.root, f"imagery/usgs/16/1/{i}.jpg", MB, T0 - (10 - i) * DAY)
        self.clock.t += 7200
        c = self.make(cap_gb=0.002)
        c.pinned = lambda keys: [k.endswith(("/0.jpg", "/1.jpg")) for k in keys]
        c.evict()
        self.assertEqual([exists(self.root, f"imagery/usgs/16/1/{i}.jpg") for i in range(6)],
                         [True, True, False, False, False, False])
        put(self.root, "imagery/usgs/16/1/7.jpg", MB, T0 - DAY)
        c.scan()
        c.clear()
        self.assertTrue(exists(self.root, "imagery/usgs/16/1/0.jpg"))
        self.assertFalse(exists(self.root, "imagery/usgs/16/1/7.jpg"))
        self.assertNotIn("pinnedBytes", c.message())
        c.pinned_bytes = 2 * MB
        self.assertEqual(c.message()["pinnedBytes"], 2 * MB)

    def test_a_failing_pin_check_evicts_as_before(self):
        for i in range(3):
            put(self.root, f"imagery/usgs/16/1/{i}.jpg", MB, T0 - (10 - i) * DAY)
        self.clock.t += 7200
        c = self.make(cap_gb=0.002)

        def boom(keys):
            raise RuntimeError("x")
        c.pinned = boom
        self.assertEqual(c.evict()[0], 2)


class PruneVersionsTest(unittest.TestCase):
    def test_only_older_version_folders_go(self):
        import shutil
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        for d in ("v1", "v17", "v18", "notes", "v2x"):
            os.makedirs(os.path.join(root, d, "rel", "4704"))
            with open(os.path.join(root, d, "rel", "4704", "6244.npz"), "wb") as f:
                f.write(b"x" * 10)
        n, freed = cachemod.prune_versions(root, "v18")
        self.assertEqual((n, freed), (2, 20))
        self.assertEqual(sorted(os.listdir(root)), ["notes", "v18", "v2x"])
        self.assertEqual(cachemod.prune_versions(os.path.join(root, "missing"), "v1"), (0, 0))


class HeldReadTest(unittest.TestCase):
    """A file another process is replacing on Windows (PermissionError for a moment): read again, then a miss."""

    def test_held_then_free_and_held_for_good(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.jpg")
            Path(path).write_bytes(b"jpg")
            real = open
            calls = []

            def held(n):
                def fake(p, *a, **k):
                    calls.append(p)
                    if p == path and len(calls) <= n:
                        raise PermissionError(13, "Permission denied")
                    return real(p, *a, **k)
                return fake
            with mock.patch.object(cachemod, "READ_WAITS", (0, 0, 0)), \
                    mock.patch("builtins.open", held(2)):
                self.assertEqual(cachemod.NONE.read(path), b"jpg")
            calls.clear()
            with mock.patch.object(cachemod, "READ_WAITS", (0, 0, 0)), \
                    mock.patch("builtins.open", held(99)):
                self.assertIsNone(cachemod.NONE.read(path))
            self.assertEqual(len(calls), 4)


if __name__ == "__main__":
    unittest.main()


class LinkedCacheTest(unittest.TestCase):
    """A scratch cache of links into a real one must never delete the real one's files (2026-10-29: ~7 GB evicted
    through the links of a benchmark's scratch cache)."""

    def test_a_cache_whose_folders_are_links_is_left_alone(self):
        with tempfile.TemporaryDirectory() as real, tempfile.TemporaryDirectory() as scratch:
            d = os.path.join(real, "dem", "z")
            os.makedirs(d)
            f = os.path.join(d, "1.webp")
            with open(f, "wb") as fh:
                fh.write(b"x" * 4096)
            os.symlink(os.path.join(real, "dem"), os.path.join(scratch, "dem"))
            c = cachemod.Cache(scratch, cap_gb=0.000001, start=False)
            self.assertFalse(c.managed, "linked folders: left alone")
            c.scan()
            c.evict(0)
            c.clear(wait=0.1)
            self.assertTrue(os.path.exists(f), "the real file stays")

    def test_remove_never_follows_a_link_out_of_the_cache(self):
        with tempfile.TemporaryDirectory() as real, tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "dem", "z"))
            target = os.path.join(real, "1.webp")
            with open(target, "wb") as fh:
                fh.write(b"x" * 10)
            os.symlink(target, os.path.join(root, "dem", "z", "1.webp"))
            self.assertFalse(cachemod._inside(os.path.join(root, "dem", "z", "1.webp"), root))
            self.assertTrue(cachemod._inside(os.path.join(root, "dem", "z", "2.webp"), root))
