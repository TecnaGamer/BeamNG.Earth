"""plancache: a cell's road plans shared between the server's processes: the same heights from the cache, one
process plans a cell while the others wait (not behind a less favoured one), stale locks, old runs pruned."""
import os
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace
import time
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import mapcells  # noqa: E402
import plancache  # noqa: E402
import xproc  # noqa: E402
from test_grading import FakeMD  # noqa: E402
from test_roads import features, FRAME  # noqa: E402

KEY = (14, 4000, 6000)


class PlanCacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        os.environ[plancache.RUN_ENV] = "test"
        self.addCleanup(os.environ.pop, plancache.RUN_ENV, None)

    def grader(self, md, shared=True):
        g = grading.Grader(FRAME, md, lambda la, lo: 10.0 + 0.01 * np.asarray(la) * 0)
        if shared:
            g.shared = plancache.for_grader(self.dir, g, None, None)
        return g

    def test_delegated_cell_is_read_not_planned_here(self):
        """grading.PlanStore.delegate (the server's own process): the cell another process planned is read from the
        plan cache; when the delegate could not, it is planned here as before."""
        f = features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]], ["residential", "secondary"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        a = self.grader(FakeMD(f))                  # (the other process)
        b = self.grader(FakeMD(f))
        asked = []

        def elsewhere(key):
            asked.append(key)
            a.store.planner_of_cell(key)._cell_plans(key)
            return True
        b.store.delegate = elsewhere
        za = a.apply(la, lo, np.full(50, 10.0))
        shutil.rmtree(os.path.join(self.dir, "plans"), True)     # (b asks first: the delegate plans them, in a)
        a = self.grader(FakeMD(f))
        made = []
        orig = grading._Planner._make_cell
        with mock.patch.object(grading._Planner, "_make_cell", lambda self, key: made.append(key) or orig(self, key)):
            zb = b.apply(la, lo, np.full(50, 10.0))
        self.assertEqual(made, [])                  # (nothing planned in b's process: read)
        np.testing.assert_array_equal(za, zb)
        c = self.grader(FakeMD(f))
        c.store.delegate = lambda key: (_ for _ in ()).throw(RuntimeError("no worker"))
        shutil.rmtree(os.path.join(self.dir, "plans"), True)
        zc = c.apply(la, lo, np.full(50, 10.0))     # (planned here after all)
        np.testing.assert_array_equal(za, zc)

    def test_other_process_reads_the_same_heights(self):
        f = features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]], ["residential", "secondary"])
        a = self.grader(FakeMD(f))
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        za = a.apply(la, lo, np.zeros(50))
        md = FakeMD(f)
        b = self.grader(md)                     # (another process: its own Grader over the same cache)
        zb = b.apply(la, lo, np.zeros(50))
        self.assertEqual(md.calls, 0, "no road data fetched: the plans came from the cache")
        self.assertGreater(b.shared.stats["read"], 0)
        self.assertTrue(np.array_equal(za, zb))
        self.assertTrue(np.array_equal(self.grader(FakeMD(f), shared=False).apply(la, lo, np.zeros(50)), za),
                        "the same as without the cache")

    def test_complete_plans_kept_for_the_next_run(self):
        f = features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]], ["residential", "secondary"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        za = self.grader(FakeMD(f)).apply(la, lo, np.zeros(50))
        os.environ[plancache.RUN_ENV] = "next"          # (a later server run: its own run folder, empty)
        md = FakeMD(f)
        b = self.grader(md)
        zb = b.apply(la, lo, np.zeros(50))
        self.assertEqual(md.calls, 0, "planned in the last run: read from the revisit cache")
        self.assertTrue(np.array_equal(za, zb))

    def test_has(self):
        """PlanCache.has: a cell's plans on disk, this run's or (a later run) the revisit cache's; nothing read."""
        f = features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]], ["residential", "secondary"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        a = self.grader(FakeMD(f))
        keys = sorted({k for pl, idx in a.store.planners_of(np.asarray(la), np.asarray(lo))
                       for k in pl._keys(np.asarray(la)[idx], np.asarray(lo)[idx])})
        self.assertFalse(any(a.shared.has(k) for k in keys), "nothing planned yet")
        a.apply(la, lo, np.zeros(50))
        self.assertTrue(all(a.shared.has(k) for k in keys), "this run's plans")
        os.environ[plancache.RUN_ENV] = "next"          # (a later server run: its own run folder, empty)
        b = self.grader(FakeMD(f))
        self.assertTrue(all(b.shared.has(k) for k in keys), "the revisit cache's copy")
        self.assertFalse(b.shared.has((14, 1, 1)), "another cell")

    def test_incomplete_plans_not_kept(self):
        import revisit
        f = features([[(-300, 20), (300, 20)]], ["residential"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)

        def faulty(la_, lo_):           # (an elevation download failed while planning)
            revisit.fault()
            return 10.0 + 0.0 * np.asarray(la_)
        g = grading.Grader(FRAME, FakeMD(f), faulty)
        g.shared = plancache.for_grader(self.dir, g, None, None)
        g.apply(la, lo, np.zeros(50))
        os.environ[plancache.RUN_ENV] = "next"
        md = FakeMD(f)
        self.grader(md).apply(la, lo, np.zeros(50))
        self.assertGreater(md.calls, 0, "planned again: the last run's plans were not complete")

    def test_no_run_no_cache(self):
        os.environ.pop(plancache.RUN_ENV, None)
        self.assertIsNone(plancache.for_grader(self.dir, self.grader(FakeMD(None), False), None, None))

    def test_waits_for_an_equal_planner(self):
        c = plancache.PlanCache(self.dir, "s")
        state, mine = c.claim(KEY)
        self.assertEqual((state, mine), (None, True))
        other = plancache.PlanCache(self.dir, "s")
        got = []
        t = threading.Thread(target=lambda: got.append(other.claim(KEY)))
        t.start()
        time.sleep(0.3)
        self.assertEqual(got, [], "waiting")
        c.store(KEY, [], [], [], [])
        c.release(KEY)
        t.join(5)
        self.assertEqual(got[0][1], False)
        self.assertEqual(got[0][0], ([], [], [], [], []))     # (the corrected decks last: grading.DECK_GROUND_*)
        self.assertEqual(other.stats["waited"], 1)

    def test_long_wait_logged_with_its_holder(self):
        """A wait for another process's plans past LONG_WAIT_S is logged with the cell and that process (a road tile
        "may be stuck": whose plans it waited for)."""
        c = plancache.PlanCache(self.dir, "s")
        c.claim(KEY)
        other = plancache.PlanCache(self.dir, "s")
        got = []
        with mock.patch.object(plancache, "LONG_WAIT_S", 0.2), self.assertLogs("earthd.plancache", "WARNING") as cm:
            t = threading.Thread(target=lambda: got.append(other.claim(KEY)))
            t.start()
            time.sleep(0.4)
            c.store(KEY, [], [], [], [])
            c.release(KEY)
            t.join(5)
        self.assertIn(str(os.getpid()), cm.output[0])
        self.assertIn("they came", cm.output[0])

    def test_does_not_wait_behind_a_less_favoured_planner(self):
        c = plancache.PlanCache(self.dir, "s")
        with mock.patch.object(xproc, "PRIORITY", 5):
            self.assertEqual(c.claim(KEY), (None, True))
        t0 = time.monotonic()
        self.assertEqual(c.claim(KEY), (None, False), "plans it itself")
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_stale_lock_taken_over(self):
        c = plancache.PlanCache(self.dir, "s")
        self.assertEqual(c.claim(KEY), (None, True))
        lock = c._path(KEY) + ".lock"
        old = time.time() - xproc.STALE_S - 5
        os.utime(lock, (old, old))
        self.assertEqual(c.claim(KEY), (None, True))

    def test_failed_fetch_not_stored(self):
        class Failing:
            def cell(self, key, layers=None, kind=None):
                raise OSError("offline")
        g = self.grader(Failing())
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0]), np.array([0.0]), 0.0)
        g.apply(la, lo, np.zeros(1))
        self.assertEqual(g.shared.stats["written"], 0)
        locks = [n for _r, _d, fs in os.walk(self.dir) for n in fs if n.endswith(".lock")]
        self.assertEqual(locks, [], "its lock released")

    def test_prune_old_runs(self):
        root = os.path.join(self.dir, "plans")
        for run in ("old", "new"):
            os.makedirs(os.path.join(root, run, "anchor", "sig"))
        old = time.time() - plancache.KEEP_S - 60
        for d in ("old", "old/anchor", "old/anchor/sig", "new", "new/anchor"):
            os.utime(os.path.join(root, d), (old, old))
        self.assertEqual(plancache.prune(root), 1)
        self.assertEqual(os.listdir(root), ["new"], "a run still writing (its newest folder) is kept")


class RoadStageTest(unittest.TestCase):
    """A cell's road stage shared by the run's processes (plancache's stage_*, earthfast's stageshare; grading
    _Planner._road_stage): made by one, read by the others, the same plans and heights as each making its own."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        os.environ[plancache.RUN_ENV] = "test"
        self.addCleanup(os.environ.pop, plancache.RUN_ENV, None)

    def other_pid(self):
        """Another live process's id (a child that sleeps; os.getppid is not there on every platform's test host)."""
        import subprocess
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        return p.pid

    @staticmethod
    def uptime_ms():
        """The machine's uptime clock, as the claims' (stageshare.rs uptime_ms)."""
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_uint64
            return int(ctypes.windll.kernel32.GetTickCount64())
        try:
            with open("/proc/uptime") as fh:
                return int(float(fh.read().split()[0]) * 1000)
        except OSError:
            return int(time.time() * 1000)

    def grader(self, md, elev=None):
        g = grading.Grader(FRAME, md, elev or (lambda la, lo: 10.0 + 0.01 * np.asarray(la) * 0))
        g.shared = plancache.for_grader(self.dir, g, None, None)
        return g

    def cells(self, g, la, lo):
        return sorted({k for pl, idx in g.store.planners_of(np.asarray(la), np.asarray(lo))
                       for k in pl._keys(np.asarray(la)[idx], np.asarray(lo)[idx])})

    def test_round_trip_is_exact(self):
        f = features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]], ["residential", "secondary"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        a = self.grader(FakeMD(f))
        key = self.cells(a, la, lo)[0]
        pl = a.store.planner_of_cell(key)
        feats, plans, keeps = pl._road_stage(key)
        self.assertGreater(len(plans), 0)
        self.assertGreater(a.shared.stats.get("stage_written", 0), 0, "kept for the run's other processes")
        got = a.shared.stage_load(key)
        self.assertIsNotNone(got)
        rp, rk, short, decks = got
        self.assertFalse(short)
        self.assertEqual(decks, grading.deck_patches_id(pl._cell_deck_patches(key)))
        self.assertEqual([fid for fid, _p in rp], [fid for fid, _p in plans])
        for (_f, x), (_g, y) in zip(plans, rp):
            for name in ("xy", "z", "cut", "bank", "widen"):
                u, v = getattr(x, name), getattr(y, name)
                self.assertEqual(u is None, v is None, name)
                if u is not None:
                    self.assertEqual((np.asarray(u).dtype, np.asarray(u).shape), (v.dtype, v.shape), name)
                    self.assertEqual(np.ascontiguousarray(u).tobytes(), v.tobytes(), name)
                    self.assertTrue(v.flags.writeable, "arrays of the reader's own")
            self.assertEqual((x.half, x.abut, x.road_half, x.rid, x.rank, x.max_wall, x.steep, x.bbox),
                             (y.half, y.abut, y.road_half, y.rid, y.rank, y.max_wall, y.steep, y.bbox))
        self.assertEqual(len(rk), len(keeps))

    def test_other_process_reads_the_stage_and_plans_the_same(self):
        f = features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]], ["residential", "secondary"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        a = self.grader(FakeMD(f))
        for key in self.cells(a, la, lo):                   # (one process made the cells' road stages, not their plans)
            a.store.planner_of_cell(key)._road_stage(key)
            self.assertFalse(a.shared.has(key))
        b = self.grader(FakeMD(f))                          # (another process plans the cell: it takes the stage)
        with mock.patch.object(grading._Planner, "_make_cell_roads", side_effect=AssertionError("made again")):
            zb = b.apply(la, lo, np.zeros(50))
        self.assertGreater(b.shared.stats.get("stage_read", 0), 0)
        os.environ[plancache.RUN_ENV] = "alone"             # (a run where nothing was shared)
        with mock.patch.object(plancache, "STAGE_ON", False):
            c = self.grader(FakeMD(f))
            zc = c.apply(la, lo, np.zeros(50))
            self.assertEqual(c.shared.stats.get("stage_written", 0), 0)
        self.assertEqual(zb.tobytes(), zc.tobytes(), "the same heights as planned alone")

    def test_cells_planned_side_by_side_claim_no_stage(self):
        """A process planning several cells at once (a block's: one interpreter lock for all) takes no stage claim and
        keeps no stage for the others (they make their own meanwhile, as before), but reads theirs."""
        f = features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]], ["residential", "secondary"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        a = self.grader(FakeMD(f))
        key = self.cells(a, la, lo)[0]
        with grading._STAGE_CROWD_LOCK:
            grading._STAGE_CROWD[0] += 2
        try:
            seen = []
            real = a.shared.stage_claim
            a.shared.stage_claim = lambda k, **kw: (seen.append(kw.get("hold")), real(k, **kw))[1]
            a.store.planner_of_cell(key)._road_stage(key)
            self.assertEqual(seen, [False])
            self.assertEqual(a.shared.stats.get("stage_written", 0), 0)
            self.assertIsNone(a.shared.stage_load(key))
            b = self.grader(FakeMD(f))
            self.assertEqual(b.shared.stage_claim(key), (None, True), "free for a process that makes it alone")
            p = grading.Plan(np.array([[0.0, 0.0], [10.0, 0.0]]), np.array([1.0, 2.0]), 3.0)
            b.shared.stage_store(key, [("a", p)], [], False)
            b.shared.stage_release(key)
            got, mine = a.shared.stage_claim(key, hold=False)
            self.assertEqual((got[0][0][0], mine), ("a", False), "another process's stage read")
        finally:
            with grading._STAGE_CROWD_LOCK:
                grading._STAGE_CROWD[0] -= 2
        self.assertEqual(grading._STAGE_CROWD[0], 0)

    def test_short_stage_read_short(self):
        import revisit
        f = features([[(-300, 20), (300, 20)]], ["residential"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)

        def faulty(la_, lo_):           # (an elevation download failed while the stage was made)
            revisit.fault()
            return 10.0 + 0.0 * np.asarray(la_)
        a = self.grader(FakeMD(f), faulty)
        key = self.cells(a, la, lo)[0]
        a.store.planner_of_cell(key)._road_stage(key)
        self.assertTrue(a.shared.stage_load(key)[2], "made short of something")
        b = self.grader(FakeMD(f))
        b.apply(la, lo, np.zeros(50))
        self.assertTrue(b.shared.is_short(key), "plans made from a short stage are short: not kept for later visits")

    def test_one_maker_the_others_wait(self):
        c = plancache.PlanCache(self.dir, "s")
        self.assertEqual(c.stage_claim(KEY), (None, True))
        other = plancache.PlanCache(self.dir, "s")
        with mock.patch.object(os, "getpid", return_value=self.other_pid()):    # (another live process asks)
            t0 = time.monotonic()
            self.assertEqual(other.stage_claim(KEY, wait_s=0.3), (None, False), "made there after the wait")
            self.assertGreaterEqual(time.monotonic() - t0, 0.3)
            got = []
            t = threading.Thread(target=lambda: got.append(other.stage_claim(KEY, wait_s=20.0)))
            t.start()
            time.sleep(0.2)
            self.assertEqual(got, [], "waiting")
            p = grading.Plan(np.array([[0.0, 0.0], [10.0, 0.0]]), np.array([1.0, 2.0]), 3.0)
            self.assertTrue(c.stage_store(KEY, [("a", p)], [], False))
            c.stage_release(KEY)
            t.join(5.0)
        self.assertEqual(len(got), 1)
        (plans, keeps, short, _decks), mine = got[0]
        self.assertFalse(mine)
        self.assertEqual((plans[0][0], plans[0][1].z.tolist(), keeps, short), ("a", [1.0, 2.0], [], False))

    def test_dead_or_less_favoured_holder_not_waited_for(self):
        c = plancache.PlanCache(self.dir, "s")
        path = c._stage_path(KEY)
        os.makedirs(os.path.dirname(path))
        with open(path + ".claim", "w") as fh:
            fh.write(f"1 0 4000000000 1 5 {self.uptime_ms()}")      # (its process is gone)
        self.assertEqual(c.stage_claim(KEY), (None, True), "taken over")
        c.stage_release(KEY)
        self.assertFalse(os.path.exists(path + ".claim"))
        with open(path + ".claim", "w") as fh:          # (a road tile's process, below this one)
            fh.write(f"1 3 {self.other_pid()} 0 7 {self.uptime_ms()}")
        t0 = time.monotonic()
        self.assertEqual(c.stage_claim(KEY), (None, False), "made here without the claim")
        self.assertLess(time.monotonic() - t0, 1.0)
        with xproc.favoured():                          # (itself a road tile's process on a first block's cell)
            with mock.patch.object(xproc, "PRIORITY", 3):
                self.assertEqual(xproc._rank("demand"), (1, 0))
        with mock.patch.object(xproc, "PRIORITY", 3):
            self.assertEqual(xproc._rank("demand"), (1, 3))

    def test_damaged_stage_made_again(self):
        f = features([[(-300, 20), (300, 20)]], ["residential"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        a = self.grader(FakeMD(f))
        key = self.cells(a, la, lo)[0]
        a.store.planner_of_cell(key)._road_stage(key)
        path = a.shared._stage_path(key)
        with open(path, "r+b") as fh:
            fh.seek(os.path.getsize(path) // 2)
            fh.write(b"\xff\xfe")
        self.assertIsNone(a.shared.stage_load(key))
        md = FakeMD(f)
        b = self.grader(md)
        made = b.store.planner_of_cell(key)._road_stage(key)
        self.assertGreater(md.calls, 0, "made here")
        self.assertGreater(b.shared.stats.get("stage_damaged", 0), 0)
        self.assertGreater(b.shared.stats.get("stage_written", 0), 0, "and written again, under its claim")
        self.assertFalse(os.path.exists(path + ".claim"))
        again = b.shared.stage_load(key)
        self.assertIsNotNone(again, "the run's other processes read it again")
        self.assertEqual([fid for fid, _p in again[0]], [fid for fid, _p in made[1]])
        with mock.patch.object(grading._Planner, "_make_cell_roads", side_effect=AssertionError("made again")):
            c = self.grader(FakeMD(f))
            self.assertIsNotNone(c.store.planner_of_cell(key)._road_stage(key))
        os.environ[plancache.RUN_ENV] = "alone"
        with mock.patch.object(plancache, "STAGE_ON", False):
            alone = self.grader(FakeMD(f)).apply(la, lo, np.zeros(50))
        os.environ[plancache.RUN_ENV] = "test"
        self.assertEqual(c.apply(la, lo, np.zeros(50)).tobytes(), alone.tobytes())

    def test_a_damaged_stage_is_repaired_by_one_process(self):
        """Readers of a damaged stage: one takes the claim to write it again, the others wait for it; a claim held
        for the repair by another process is not taken twice."""
        c = plancache.PlanCache(self.dir, "s")
        self.assertEqual(c.stage_claim(KEY), (None, True))
        p = grading.Plan(np.array([[0.0, 0.0], [10.0, 0.0]]), np.array([1.0, 2.0]), 3.0)
        self.assertTrue(c.stage_store(KEY, [("a", p)], [], False))
        c.stage_release(KEY)
        path = c._stage_path(KEY)
        with open(path, "r+b") as fh:
            fh.truncate(os.path.getsize(path) - 5)
        a, b = plancache.PlanCache(self.dir, "s"), plancache.PlanCache(self.dir, "s")
        self.assertEqual(a.stage_claim(KEY), (None, True), "the damaged file is its to write again")
        with mock.patch.object(os, "getpid", return_value=self.other_pid()):
            self.assertEqual(b.stage_claim(KEY, wait_s=0.3), (None, False), "another process waits, then makes its own")
            self.assertFalse(b.stage_store(KEY, [("b", p)], [], False), "without the claim it keeps nothing")
            got = []
            t = threading.Thread(target=lambda: got.append(b.stage_claim(KEY, wait_s=20.0)))
            t.start()
            time.sleep(0.2)
            self.assertEqual(got, [])
            self.assertTrue(a.stage_store(KEY, [("a", p)], [], False))
            a.stage_release(KEY)
            t.join(5.0)
        self.assertEqual((got[0][0][0][0][0], got[0][1]), ("a", False), "read once it is written again")

    def test_only_the_claims_owner_keeps_and_releases(self):
        """A claim taken over (its holder past the lease): the first owner's late result and release leave its
        successor's claim and result alone."""
        a, b = plancache.PlanCache(self.dir, "s"), plancache.PlanCache(self.dir, "s")
        self.assertEqual(a.stage_claim(KEY), (None, True))
        path = a._stage_path(KEY)
        with open(path + ".claim") as fh:
            f = fh.read().split()
        f[5] = "0"                                      # (taken long ago on the uptime clock)
        with open(path + ".claim", "w") as fh:
            fh.write(" ".join(f))
        self.assertEqual(b.stage_claim(KEY), (None, True), "past its lease: taken over")
        p = grading.Plan(np.array([[0.0, 0.0], [10.0, 0.0]]), np.array([1.0, 2.0]), 3.0)
        self.assertFalse(a.stage_store(KEY, [("old", p)], [], False))
        a.stage_release(KEY)
        self.assertTrue(os.path.exists(path + ".claim"), "the successor's claim is still there")
        self.assertIsNone(b.stage_load(KEY))
        self.assertTrue(b.stage_store(KEY, [("new", p)], [], False))
        b.stage_release(KEY)
        self.assertFalse(os.path.exists(path + ".claim"))
        self.assertEqual(b.stage_load(KEY)[0][0][0], "new")

    def test_fault_in_the_deck_patches_makes_the_stage_short(self):
        """The cell's corrected decks are part of its road stage: made short of something (an elevation read that
        failed while they were prepared), the stage kept for the run is short, and a process that adopts it keeps
        no plans for later visits."""
        import revisit
        f = features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]], ["residential", "secondary"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        a = self.grader(FakeMD(f))
        keys = self.cells(a, la, lo)

        def faulty(planner, key):       # (a side probe's elevation failed: fallback corrections)
            revisit.fault(here=True)
            return []
        with mock.patch.object(grading._Planner, "_cell_deck_patches", faulty):
            for key in keys:
                a.store.planner_of_cell(key)._road_stage(key)
        for key in keys:
            self.assertTrue(a.shared.stage_load(key)[2], "kept as short")
        b = self.grader(FakeMD(f))      # (its own reads all succeed)
        with mock.patch.object(grading._Planner, "_make_cell_roads", side_effect=AssertionError("made again")):
            b.apply(la, lo, np.zeros(50))
        for key in keys:
            self.assertTrue(b.shared.is_short(key), "plans from a short stage are short")
            self.assertFalse(revisit.has(self.dir, "plans", b.shared._revisit_key(key)), "and not kept for later visits")

    def test_deck_patches_say_they_are_short_at_every_read(self):
        """A deck's correction made while something it read failed is remembered as short: every later read of it
        (another cell's stage, the same cell's again) is short too, and an error while they are prepared is one."""
        import revisit
        f = features([[(-300, 20), (300, 20)]], ["residential"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        g = self.grader(FakeMD(f))
        pl = g.store.planner_of_cell(self.cells(g, la, lo)[0])
        decks = {("d", 1): (np.array([[0.0, 0.0], [30.0, 0.0]]), 4.0)}

        def make(todo, decks_, now, fail):
            if fail:
                revisit.fault()
            return {k: (None, True, ()) for k in todo}, ((1, 0) if fail else (0, 0))
        with mock.patch.object(pl, "_deck_patches_make", lambda t, d, n: make(t, d, n, True)):
            with revisit.scope() as b1:
                pl._deck_patches(decks)
            with revisit.scope() as b2:         # (read again: from the short ones' memo, said again)
                pl._deck_patches(decks)
        self.assertTrue(b1.n and b2.n)
        other = {("d", 2): (np.array([[0.0, 9.0], [30.0, 9.0]]), 4.0)}
        with mock.patch.object(pl, "_deck_patches_make", lambda t, d, n: make(t, d, n, False)):
            with revisit.scope() as b3:
                pl._deck_patches(other)
                pl._deck_patches(other)
        self.assertEqual(b3.n, 0, "a complete one is not")
        if grading.DECK_GROUND_ON:
            with mock.patch.object(pl, "_decks_in", side_effect=RuntimeError("no data")):
                with revisit.scope() as b4:
                    self.assertEqual(pl._cell_deck_patches(self.cells(g, la, lo)[0]), [])
            self.assertTrue(b4.n, "prepared with an error: short")

    def deck_fixture(self):
        """(grader, cells, la, lo, state): a place whose cells have one deck each to correct; state["fail"]: the
        elevation read of a correction's making fails (a fault), state["made"]: how many were made."""
        import revisit
        f = features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]], ["residential", "secondary"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        g = self.grader(FakeMD(f))
        state = {"fail": True, "made": 0, "fail_keys": None, "stores": []}

        def decks_in(planner, keys):
            out = {}
            for k in keys:
                e, n, _ = planner.frame.geodetic_to_enu(*k.center(), 0.0)
                for j in (0, 1):
                    out[("deck", str(k), j)] = (np.array([[float(e) - 20, float(n) + 9 * j], [float(e) + 20, float(n) + 9 * j]]), 4.0)
            return out, False

        def make(planner, todo, decks, now):
            state["made"] += len(todo)
            state["stores"].append(planner.store)
            fail = state["fail"] and (state["fail_keys"] is None or any(k in state["fail_keys"] for k in todo))
            if fail:
                revisit.fault()
            return {k: (None, True, ()) for k in todo}, ((1, 0) if fail else (0, 0))
        for name, fn in (("_decks_in", decks_in), ("_deck_patches_make", make)):
            pch = mock.patch.object(grading._Planner, name, fn)
            pch.start()
            self.addCleanup(pch.stop)
        pch = mock.patch.object(grading, "DECK_GROUND_ON", True)
        pch.start()
        self.addCleanup(pch.stop)
        return g, self.cells(g, la, lo), la, lo, state

    def late_state(self, md):
        """A process of the late pool (blockworker._State, _LATE): what late_plan and the late geotile builds run in."""
        import blockworker

        class Dem:
            def sample(self, lat, lon, zoom, progress=None):
                return np.full(np.shape(lat), 10.0)
        st = blockworker._State.__new__(blockworker._State)
        st.lock = threading.Lock()
        st.cache_dir, st.md, st.widths, st.sea, st.dem, st.grading = self.dir, md, None, None, Dem(), True
        st.graders, st.chunks = {}, {}
        old, late = getattr(blockworker, "_s", None), blockworker._LATE[0]
        blockworker._s, blockworker._LATE[0] = st, True
        self.addCleanup(setattr, blockworker, "_s", old)
        self.addCleanup(blockworker._LATE.__setitem__, 0, late)
        self.addCleanup(blockworker._geo_graders.clear)
        return st

    def test_a_transient_deck_fault_recovers_in_the_same_worker(self):
        """A deck correction made while its elevation read failed: the cell's plans are short and not kept. Once the
        inputs are there a late retry in the same late process makes complete plans that are kept, the same as
        planned with nothing failing: its planning state was dropped whole after the tries that read something
        short, and is kept and used again while nothing is."""
        import revisit
        import blockworker
        g, keys, la, lo, state = self.deck_fixture()
        z1 = g.apply(la, lo, np.zeros(50))          # (the session's process: short, not kept)
        for key in keys:
            self.assertTrue(g.shared.is_short(key))
            self.assertFalse(revisit.has(self.dir, "plans", g.shared._revisit_key(key)))
        st = self.late_state(g.md)
        for n, key in enumerate(keys):              # (late tries while the elevation still fails: short, not kept)
            self.assertIs(blockworker.late_plan(*key), False)
            self.assertNotIn("plan_store", st.__dict__, "a state that read something short is dropped whole")
            self.assertEqual((st.late_resets, st.graders, blockworker._geo_graders), (n + 1, {}, {}))
        tried = st.late_resets
        state["fail"] = False                       # (the elevation is there now)
        for key in keys:
            self.assertIs(blockworker.late_plan(*key), True, "kept after the late retry")
        self.assertEqual(st.late_resets, tried, "nothing short: the state is kept and used again")
        self.assertTrue(st.plan_store.shared.fresh)
        self.assertFalse(g.shared.fresh)
        self.assertIs(blockworker.late_plan(*keys[0]), True, "kept before: answered at once")
        os.environ[plancache.RUN_ENV] = "next"      # (a later run reads the kept plans)
        z2 = self.grader(FakeMD(features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]],
                                         ["residential", "secondary"]))).apply(la, lo, np.zeros(50))
        z3 = grading.Grader(FRAME, FakeMD(features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]],
                                                   ["residential", "secondary"])),
                            lambda la_, lo_: 10.0 + 0.0 * np.asarray(la_)).apply(la, lo, np.zeros(50))
        self.assertEqual(z2.tobytes(), z3.tobytes(), "the same as planned with nothing failing")
        self.assertEqual(z1.tobytes(), z3.tobytes())

    def test_a_late_retry_reads_nothing_of_an_earlier_short_try(self):
        """After a late try that read something short, the next try (a cell's plans, a geotile's build) asks nothing
        of the planners that try used (any of them asked would fail here) and reads none of the run's short plan
        files: it plans on a new store, from the disk."""
        import revisit
        import blockworker
        g, keys, la, lo, state = self.deck_fixture()
        st = self.late_state(g.md)
        self.assertIs(blockworker.late_plan(*keys[0]), False)       # (a first try, the elevation failing)
        old = [s for s in state["stores"] if s is not g.store]
        self.assertTrue(old)
        self.assertTrue(any(pl.short or pl.stage_short or pl.__dict__.get("_dg_faulted")
                            for s_ in old for pl in s_.planners.values()), "it left short things in its planners")

        def poisoned(*a, **k):
            raise AssertionError("a planner of the try that read something short was asked")
        for s_ in old:
            for pl in s_.planners.values():
                for name in ("_cell_plans", "_plan_cell", "_road_stage", "_cell_road_plans", "_deck_patches",
                             "_cell_deck_patches", "_deck_pack_cells", "_plan_set", "_make_cell", "apply", "_cell_enu"):
                    pch = mock.patch.object(pl, name, poisoned)
                    pch.start()
                    self.addCleanup(pch.stop)
        state["fail"] = False
        loads = []
        real_load = plancache.PlanCache.load

        def load(cache, key):
            got = real_load(cache, key)
            if cache.fresh and got is not None:
                loads.append(cache.is_short(key))
            return got
        with mock.patch.object(plancache.PlanCache, "load", load):
            for key in keys:
                self.assertIs(blockworker.late_plan(*key), True)
            # (a late geotile's build: its frame's grader, the process's store)
            seen = []

            def content():
                gr = blockworker._frame_grader(FRAME)
                seen.append(gr.store)
                return gr.apply(la, lo, np.zeros(50))
            z = blockworker.late_job(lambda: True, content)
        self.assertFalse(any(loads), "no short plans of the run read")
        self.assertTrue(all(s_ not in old for s_ in seen + [st.plan_store]))
        self.assertEqual(z.tobytes(), grading.Grader(
            FRAME, FakeMD(features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]], ["residential", "secondary"])),
            lambda la_, lo_: 10.0 + 0.0 * np.asarray(la_)).apply(la, lo, np.zeros(50)).tobytes())

    def test_a_late_geotile_build_that_read_something_short_leaves_no_state(self):
        """blockworker.prebuild_geo in a late process is a job of its own planning state: a build that read
        something short, or whose content was not kept, drops the store, the chain stores and the frames' graders
        (their mapped copies); a clean, kept one leaves them for the next."""
        import revisit
        import blockworker
        f = features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]], ["residential", "secondary"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        st = self.late_state(FakeMD(f))
        kept = {"v": False}
        calls = []

        def geo_content(gx, gy, zoom, out_dir, vfs_dir, *a, **k):
            gr = blockworker._frame_grader(FRAME)
            st.__dict__.setdefault("chain_stores", {})["tunnels"] = object()
            gr.apply(la, lo, np.zeros(50))
            calls.append(gr.store)
            if not kept["v"]:
                revisit.fault()

        real_has = revisit.has
        with mock.patch.object(blockworker, "_geo_content", geo_content), \
                mock.patch.object(blockworker, "_geo_content_key", lambda *a, **k: "k"), \
                mock.patch.object(revisit, "has", lambda d, kind, k: kept["v"] if kind == "roadsg" else real_has(d, kind, k)):
            scratch = os.path.join(self.dir, "scratch")
            self.assertIsNone(blockworker.prebuild_geo(1, 2, 16, scratch, False, False, True, False))
            self.assertEqual(len(calls), 1)
            self.assertNotIn("plan_store", st.__dict__)
            self.assertEqual((st.chain_stores, st.graders, blockworker._geo_graders, st.late_resets), ({}, {}, {}, 1))
            self.assertIsNone(blockworker.prebuild_geo(1, 2, 16, scratch, False, False, True, False))
            self.assertIsNot(calls[0], calls[1], "the next try plans on a new store")
            self.assertEqual(st.late_resets, 2)
            blockworker._LATE[0] = False            # (another pool's process: its state is its own for good)
            self.assertIsNone(blockworker.prebuild_geo(1, 2, 16, scratch, False, False, True, False))
            self.assertIn("plan_store", st.__dict__)
            self.assertEqual(st.late_resets, 2)
            blockworker._LATE[0] = True
        self.assertEqual(blockworker._LATE_JOB[0], 0)

    def test_a_mapped_copy_of_short_plans_is_read_short(self):
        """A grader's mapped copy of a cell's plans made short of something says so at every read, as the plans."""
        import revisit
        g, keys, la, lo, state = self.deck_fixture()
        with revisit.scope() as b1:
            first = g._cell_plans(keys[0])
        with revisit.scope() as b2:
            self.assertIs(g._cell_plans(keys[0]), first, "the mapped copy")
        self.assertTrue(b1.n and b2.n)
        state["fail"] = False
        os.environ[plancache.RUN_ENV] = "clean"
        c = self.grader(g.md)
        c._cell_plans(keys[0])
        with revisit.scope() as b3:
            c._cell_plans(keys[0])
        self.assertEqual(b3.n, 0)

    def test_corrections_from_kept_geometry_on_the_lidar_rule_stay_short(self):
        """A cell's geometry made on the lidar rule is kept with that mark and says so at every read. A batch of deck
        corrections that made it, the single corrections made again after a failed elevation read (they read the kept
        geometry: no new fault of theirs) and a later making from the kept copy alone are all short of the lidar:
        none is ever kept as complete."""
        import revisit
        f = features([[(-300, 20), (300, 20)]], ["residential"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        state = {"elev_fail": True, "calcs": 0}

        def elev(la_, lo_):
            if state["elev_fail"]:
                revisit.fault()
            return 10.0 + 0.0 * np.asarray(la_)
        g = grading.Grader(FRAME, FakeMD(f), elev)
        g.shared = plancache.for_grader(self.dir, g, None, None)
        key = self.cells(g, la, lo)[0]
        pl = g.store.planner_of_cell(key)

        def calc(planner, k, ground):               # (the cell's parts read on the lidar rule)
            state["calcs"] += 1
            revisit.fault(lidar=True)
            return {"cell": k}

        def rows(planner, c):
            e, n, _ = planner.frame.geodetic_to_enu(*c["cell"].center(), 0.0)
            return [(("deck", str(c["cell"]), j),
                     np.array([[float(e) - 20, float(n) + 30 * j], [float(e) + 20, float(n) + 30 * j]]), 4.0) for j in (0, 1)]
        with mock.patch.object(grading._Planner, "_cell_enu_calc", calc), \
                mock.patch.object(grading._Planner, "_deck_rows", rows), \
                mock.patch.object(grading, "ENU_AHEAD_ON", False):
            with revisit.scope() as b0:
                decks = {k: v for k, v in pl._decks_in([key])[0].items() if k[1] == str(key)}
            self.assertEqual(len(decks), 2)
            self.assertTrue(b0.lidar, "made on the rule")
            with revisit.scope() as b0b:
                pl._cell_enu(key, ground=False)
            self.assertTrue(b0b.lidar, "and the kept copy says so again")
            made = state["calcs"]
            # (the batch: its elevation read fails, so each correction is made again alone, from the kept geometry)
            with revisit.scope() as b1:
                pl._deck_patches(decks)
            self.assertTrue(b1.n and b1.lidar)
            self.assertEqual(pl.__dict__["_dg_stats"]["alone"], 2)
            self.assertEqual(set(pl.__dict__["_dg_faulted"]), set(decks))
            self.assertFalse(set(decks) & set(pl.__dict__["_dg_memo"]), "never kept as complete")
            # (made again with the elevation there: the kept geometry is still the rule's)
            state["elev_fail"] = False
            with mock.patch.object(grading, "DECK_GROUND_RETRY_S", 0.0), revisit.scope() as b2:
                pl._deck_patches(decks)
            self.assertEqual(b2.n, 0)
            self.assertTrue(b2.lidar)
            self.assertEqual({k: v[2] for k, v in pl.__dict__["_dg_faulted"].items()}, {k: True for k in decks},
                             "short of the lidar alone now")
            self.assertFalse(set(decks) & set(pl.__dict__["_dg_memo"]))
            self.assertEqual(state["calcs"], made, "the geometry itself was read from memory throughout")

    def test_a_faulted_deck_correction_is_made_again_after_a_moment(self):
        """Without any late retry: a faulted correction is used for DECK_GROUND_RETRY_S at most, then made again; a
        complete one is kept and never made again."""
        import revisit
        g, keys, la, lo, state = self.deck_fixture()
        pl = g.store.planner_of_cell(keys[0])
        decks = pl._decks_in([keys[0]])[0]
        with revisit.scope() as b1:
            pl._deck_patches(decks)
        made = state["made"]
        with revisit.scope() as b2:                 # (at once: the faulted ones, said again, not made again)
            pl._deck_patches(decks)
        self.assertTrue(b1.n and b2.n)
        self.assertEqual(state["made"], made)
        state["fail"] = False
        with mock.patch.object(grading, "DECK_GROUND_RETRY_S", 0.0), revisit.scope() as b3:
            pl._deck_patches(decks)
        self.assertEqual(b3.n, 0, "made again with its inputs there: complete")
        self.assertGreater(state["made"], made)
        made = state["made"]
        with revisit.scope() as b4:
            pl._deck_patches(decks)
        self.assertEqual((b4.n, state["made"]), (0, made), "kept")
        self.assertFalse(pl.__dict__["_dg_faulted"])

    def test_one_failed_read_does_not_mark_the_whole_batch(self):
        import revisit
        g, keys, la, lo, state = self.deck_fixture()
        pl = g.store.planner_of_cell(keys[0])
        decks = pl._decks_in([keys[0]])[0]
        bad = sorted(decks)[0]
        state["fail_keys"] = {bad}
        with revisit.scope() as b1:
            pl._deck_patches(decks)
        self.assertTrue(b1.n)
        self.assertEqual(set(pl.__dict__["_dg_faulted"]), {bad}, "only the correction whose own read failed")
        self.assertEqual(set(pl.__dict__["_dg_memo"]), set(decks) - {bad})
        with revisit.scope() as b2:
            pl._deck_patches({k: v for k, v in decks.items() if k != bad})
        self.assertEqual(b2.n, 0, "the others are complete")

    def enu_fixture(self):
        """(planner, cell, state): a cell with two decks in its kept geometry; state["elev_fail"]: the elevation read
        of their corrections fails."""
        import revisit
        f = features([[(-300, 20), (300, 20)]], ["residential"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        state = {"elev_fail": True}

        def elev(la_, lo_):
            if state["elev_fail"]:
                revisit.fault()
            return 10.0 + 0.0 * np.asarray(la_)
        g = grading.Grader(FRAME, FakeMD(f), elev)
        key = self.cells(g, la, lo)[0]

        def calc(planner, k, ground):
            return {"cell": k}

        def rows(planner, c):
            e, n, _ = planner.frame.geodetic_to_enu(*c["cell"].center(), 0.0)
            return [(("deck", str(c["cell"]), j),
                     np.array([[float(e) - 20, float(n) + 30 * j], [float(e) + 20, float(n) + 30 * j]]), 4.0) for j in (0, 1)]
        for target, name, fn in ((grading._Planner, "_cell_enu_calc", calc), (grading._Planner, "_deck_rows", rows),
                                 (grading, "ENU_AHEAD_ON", False), (grading, "DECK_GROUND_ON", True)):
            pch = mock.patch.object(target, name, fn)
            pch.start()
            self.addCleanup(pch.stop)
        return g.store.planner_of_cell(key), key, state

    def test_a_short_near_pack_is_short_for_every_reader(self):
        """The pack of the corrected decks round some points (_near_pack), made of corrections whose elevation read
        failed: every planning scope that reads the kept pack is short, not only the one that made it; made again with
        the elevation there, it is complete."""
        import revisit
        pl, key, state = self.enu_fixture()
        lat, lon = (np.array([v]) for v in key.center())
        with revisit.scope() as b1:
            p1 = pl._near_pack(lat, lon)
        with revisit.scope() as b2:                 # (another cell's planning, a moment later)
            p2 = pl._near_pack(lat, lon)
        self.assertTrue(b1.n and b2.n, "both readers of the short pack are short")
        self.assertIs(p1, p2, "the kept one")
        state["elev_fail"] = False
        with revisit.scope() as b3:                 # (still the kept short pack until its corrections are made again)
            pl._near_pack(lat, lon)
        self.assertTrue(b3.n)
        with mock.patch.object(grading, "DECK_GROUND_RETRY_S", 0.0), revisit.scope() as b4:
            pl._near_pack(lat, lon)
        with revisit.scope() as b5:
            pl._near_pack(lat, lon)
        self.assertEqual((b4.n, b5.n), (0, 0), "made again from complete corrections: complete, and kept so")

    def test_decks_of_a_cell_that_failed_a_moment_ago_are_short(self):
        """A cell whose road data failed is not asked again for RETRY_S: the decks of a cell set with it in, and a
        cell's deck corrections found through it, are short of it in every scope, with or without any deck found."""
        import revisit
        pl, key, state = self.enu_fixture()
        state["elev_fail"] = False
        other = mapcells.CellKey(key.z, key.x + 1, key.y)
        with pl.lock:
            pl.failed[other] = time.monotonic()     # (its read failed in an earlier job)
        with revisit.scope() as b1:
            decks, short = pl._decks_in([key, other])
        self.assertTrue(short and b1.n and len(decks) == 2)
        with revisit.scope() as b2:
            self.assertEqual(pl._decks_in([other]), ({}, True))
        self.assertTrue(b2.n, "also with no deck found at all")
        with revisit.scope() as b3:                 # (the cell's corrections: other is among the cells round it)
            pl._cell_deck_patches(key)
        self.assertTrue(b3.n)
        with pl.lock:
            pl.failed.clear()
        with revisit.scope() as b4:
            pl._decks_in([key])
        self.assertEqual(b4.n, 0)

    def test_kept_answers_made_short_say_so_again(self):
        """The planner's kept answers (_marked: chain candidates, pin heights, junction and abutment lifts, crossing
        solves, grade matches, carriageway cells and regions, the decks near a cell) made while a read failed are short
        for every later reader; one made complete is not."""
        import revisit
        f = features([[(-300, 20), (300, 20)]], ["residential"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        g = self.grader(FakeMD(f))
        pl = g.store.planner_of_cell(self.cells(g, la, lo)[0])
        fail = {"v": True, "calls": 0}

        def calc(planner, xy):
            fail["calls"] += 1
            if fail["v"]:
                revisit.fault(lidar=True)
            return None
        with mock.patch.object(grading._Planner, "_end_lift_calc", calc), \
                mock.patch.object(grading._Planner, "_grade_match_calc", calc):
            for ask in (pl._end_lift, pl._grade_match):
                fail["v"] = True
                with revisit.scope() as b1:
                    ask(np.array([5.0, 7.0]))
                with revisit.scope() as b2:
                    ask(np.array([5.0, 7.0]))
                self.assertTrue(b1.lidar and b2.lidar)
                fail["v"] = False
                with revisit.scope() as b3:
                    ask(np.array([50.0, 70.0]))
                    ask(np.array([50.0, 70.0]))
                self.assertEqual((b3.n, b3.lidar), (0, 0))
        self.assertEqual(fail["calls"], 4, "each made once")
        for name in ("_chain_cands", "_stronger_z", "_junction_lift", "_cw_cell_parts", "_cw_region", "_end_lift",
                     "_decks_near", "_xsolve", "_grade_match"):
            self.assertEqual(getattr(grading._Planner, name).__name__, name)
            self.assertTrue(getattr(getattr(grading._Planner, name), "marked", None), name + " keeps its marks")

    def test_both_pin_ends_kept_by_one_making_carry_its_marks(self):
        """A part planned whole keeps the pin heights of both its ends (_strong_keep): made while a read failed, the
        other end, read later in a fresh scope, is short too; and so is each one of those planned at once
        (_stronger_zs)."""
        import revisit
        f = features([[(-60, 20), (60, 20)], [(0, -60), (0, 60)]], ["residential", "secondary"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-50, 50, 20), np.linspace(-30, 30, 20), 0.0)
        g = self.grader(FakeMD(f))
        key = self.cells(g, la, lo)[0]
        pl = g.store.planner_of_cell(key)
        cands = pl._chain_cands(key)
        self.assertTrue(cands)
        real = grading.plan_roads
        state = {"fail": True}

        def plan_roads(*a, **k):
            if state["fail"]:
                revisit.fault()
            return real(*a, **k)
        with mock.patch.object(grading, "plan_roads", plan_roads):
            c = cands[0]
            k0, k1 = (grading._Planner._strong_key(c, last, None) for last in (False, True))
            with revisit.scope() as b1:
                pl._stronger_z(c, False)
            self.assertTrue(b1.n)
            self.assertIn(k1, pl.__dict__["_strong_memo"], "a short part: both ends kept by the one making")
            state["fail"] = False
            with revisit.scope() as b2:
                pl._stronger_z(c, True)             # (the other end: from memory, in a fresh scope)
            self.assertTrue(b2.n, "short of what its making read")
            with revisit.scope() as b3:
                pl._stronger_z(c, False)
            self.assertTrue(b3.n)
            # (several planned at once: every end they keep)
            state["fail"] = True
            pl2 = self.grader(FakeMD(f)).store.planner_of_cell(key)
            asks = [(c2, last) for c2 in pl2._chain_cands(key) for last in (False,)]
            with revisit.scope() as b4:
                pl2._stronger_zs(asks, key)
            self.assertTrue(b4.n)
            state["fail"] = False
            for c2, _last in asks:
                for last in (False, True):
                    kk = grading._Planner._strong_key(c2, last, key)
                    if kk in pl2.__dict__["_strong_memo"]:
                        with revisit.scope() as b5:
                            pl2._stronger_z(c2, last, key)
                        self.assertTrue(b5.n, kk)
            # (made with nothing failing: none)
            pl3 = self.grader(FakeMD(f)).store.planner_of_cell(key)
            with revisit.scope() as b6:
                pl3._stronger_zs([(c3, False) for c3 in pl3._chain_cands(key)], key)
                pl3._stronger_z(pl3._chain_cands(key)[0], True, key)
            self.assertEqual(b6.n, 0)

    def test_a_fallback_after_an_error_is_short_when_it_is_the_datas(self):
        """A handler that goes on with a fallback (grading._caught): any error makes what is kept and built on it
        short, with no fault raised before it (a read's, or a ValueError / IndexError / KeyError of malformed data
        deep in a kernel); only the few errors of the computation itself (grading._COMPUTED) on complete inputs do
        not (logged with the cell), and in a making that had read something short they do too; pools.Abandoned goes
        on up."""
        import revisit
        import pools
        f = features([[(-300, 20), (300, 20)]], ["residential"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        g = self.grader(FakeMD(f))
        pl = g.store.planner_of_cell(self.cells(g, la, lo)[0])
        err = {"e": None, "fault": False}

        def calc(planner, xy):
            if err["fault"]:
                revisit.fault(lidar=True)
            raise err["e"]
        with mock.patch.object(grading._Planner, "_end_lift_calc", calc), \
                mock.patch.object(grading._Planner, "_grade_match_calc", calc), \
                mock.patch.object(grading._Planner, "_xsolve_calc", lambda planner, bridge: calc(planner, bridge)):
            asks = (lambda v: pl._end_lift(np.array([v, 7.0])), lambda v: pl._grade_match(np.array([v, 7.0])),
                    lambda v: pl._xsolve(np.array([[v, 0.0], [v + 20.0, 0.0]])))
            for n, ask in enumerate(asks):
                for m, e in enumerate((OSError("read failed"), TimeoutError("slow"), LookupError("a cell unread"),
                                       ValueError("operands could not be broadcast"), IndexError("index 9 is out of bounds"),
                                       KeyError("bridges"))):
                    err["e"], err["fault"] = e, False
                    v = 100.0 * n + 10.0 * m
                    with revisit.scope() as b1:
                        self.assertIsNone(ask(v))
                    with revisit.scope() as b2:         # (the kept fallback, read again)
                        self.assertIsNone(ask(v))
                    self.assertTrue(b1.n and b2.n, (n, type(e).__name__))
                err["e"] = ZeroDivisionError(f"a part of no length {n}")    # (the computation's own, complete inputs)
                with self.assertLogs("earthd.grading", "WARNING") as logs, revisit.scope() as b3:
                    self.assertIsNone(ask(100.0 * n + 85.0))
                    self.assertIsNone(ask(100.0 * n + 85.0))
                self.assertEqual((b3.n, b3.lidar), (0, 0))
                self.assertTrue(any("complete inputs" in ln for ln in logs.output))
                err["fault"] = True                             # (the same error after a short read: short)
                with revisit.scope() as b4:
                    ask(100.0 * n + 90.0)
                with revisit.scope() as b5:
                    ask(100.0 * n + 90.0)
                self.assertTrue(b4.n and b4.lidar and b5.n and b5.lidar)
                err["e"], err["fault"] = pools.Abandoned(), False
                with self.assertRaises(pools.Abandoned):
                    ask(100.0 * n + 95.0)
        self.assertEqual(grading.log.name, "earthd.grading")
        self.assertEqual(set(grading._COMPUTED), {ZeroDivisionError, FloatingPointError, OverflowError})

    def test_the_marks_said_are_those_of_the_entry_returned(self):
        """A reader takes a kept answer made short of something; before it has said so, the entry is put out and a
        complete one put in its place (the memory emptied at its size, the cell made again): the reader is still short
        of the one it was given, and the next reader, given the complete one, is not."""
        import revisit
        f = features([[(-300, 20), (300, 20)]], ["residential"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        g = self.grader(FakeMD(f))
        pl = g.store.planner_of_cell(self.cells(g, la, lo)[0])
        state = {"fail": True}

        def calc(planner, xy):
            if state["fail"]:
                revisit.fault()
            return ("lift", state["fail"])
        xy = np.array([5.0, 7.0])
        k = (10, 14)
        with mock.patch.object(grading._Planner, "_end_lift_calc", calc):
            with revisit.scope() as b0:
                first = pl._end_lift(xy)
            self.assertTrue(b0.n)
            swapped = []

            class Seen(list):           # (the wrapper takes its slot back when the method has returned: there, with
                def pop(self_, *a):     # the entry in hand and its marks not said yet, another thread's swap)
                    if not swapped:
                        swapped.append(k)
                        with pl.lock:
                            pl.end_cache[k] = ("lift", False)
                            pl.__dict__["_marks"].pop(("end_cache", k), None)
                    return list.pop(self_, *a)
            grading._MAKING.seen = Seen()
            self.addCleanup(grading._MAKING.__dict__.pop, "seen", None)
            with revisit.scope() as b1:
                got = pl._end_lift(xy)
            self.assertEqual(swapped, [k])
            self.assertIs(got, first, "the short entry was the one returned")
            self.assertTrue(b1.n, "and its marks the ones said")
            with revisit.scope() as b2:
                self.assertEqual(pl._end_lift(xy), ("lift", False))
            self.assertEqual(b2.n, 0, "the complete entry put in its place says nothing")
        # (every kept answer's marks are read where its entry is taken out)
        import inspect
        for name in ("_chain_cands", "_stronger_z", "_junction_lift", "_cw_cell_parts", "_cw_region", "_end_lift",
                     "_decks_near", "_xsolve", "_grade_match"):
            fn = getattr(grading._Planner, name)
            memo = fn.marked[0]
            src = inspect.getsource(fn.__closure__[[c for c in fn.__code__.co_freevars].index("fn")].cell_contents)
            self.assertIn(f'_marks_seen("{memo}"', src, name)

    def test_malformed_survey_arrays_are_refused_or_short(self):
        """A survey's arrays that do not fit each other are refused where they come in (roadz.Lines: a data error,
        read again from the source); should such lines reach the kernels all the same, the ValueError / IndexError
        they raise there makes the fallback short."""
        import revisit
        import roadz
        ll = np.array([[0.0, 0.0], [0.001, 0.0], [0.002, 0.0], [0.003, 0.0]])
        z = np.zeros(4)
        ok = roadz.Lines(ll, z, [0, 2, 4], [0, 0], ["a", "b"], ["", ""])
        self.assertEqual(len(ok), 2)
        for off, zz, level in (([0, 2, 9], z, [0, 0]),         # (a range past the vertices)
                               ([0, 3, 2, 4], z, [0, 0, 0]),   # (ranges out of order: a negative count)
                               ([1, 2, 4], z, [0, 0]),         # (not from the first vertex)
                               ([0, 2, 4], z[:3], [0, 0]),     # (a height missing)
                               ([0, 2, 4], z, [0])):           # (a level missing)
            with self.assertRaises(roadz.SurveyDataError):
                roadz.Lines(ll, zz, off, level, [], [])
        self.assertTrue(issubclass(roadz.SurveyDataError, ValueError), "as the callers of Lines.load catch it")
        path = os.path.join(self.dir, "lines.npz")
        np.savez(path, lonlat=ll, z=z, off=np.array([0, 3, 2, 4]), level=np.zeros(3, np.int8), ref=np.zeros(0, "U1"),
                 name=np.zeros(0, "U1"), corrected=np.array(False))
        with self.assertRaises(roadz.SurveyDataError):
            roadz.Lines.load(path)
        # (such lines in the kernel, as before the check: its error, no fault of its own, in a fallback handler)
        owner = next(c for c in vars(roadz).values() if isinstance(c, type) and "_segments" in vars(c))
        errors = []
        for off in ([0, 3, 2, 4], [0, 2, 9]):
            bad = roadz.Lines.__new__(roadz.Lines)
            bad.lonlat, bad.z, bad.off = ll, z, np.asarray(off, np.int64)
            stub = SimpleNamespace(_lock=threading.Lock(), _enu={})
            try:
                segs = owner._segments(stub, FRAME, "src", str(off), bad)
                errors.append(None if len(segs[2]) == len(segs[0]) else "lengths")
            except (ValueError, IndexError) as e:
                errors.append(e)
        self.assertTrue(any(isinstance(e, (ValueError, IndexError)) for e in errors), errors)
        f = features([[(-300, 20), (300, 20)]], ["residential"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        g = self.grader(FakeMD(f))
        pl = g.store.planner_of_cell(self.cells(g, la, lo)[0])
        bad = roadz.Lines.__new__(roadz.Lines)
        bad.lonlat, bad.z, bad.off = ll, z, np.asarray([0, 3, 2, 4], np.int64)

        def calc(planner, xy):          # (a measured height asked of the malformed survey)
            owner._segments(SimpleNamespace(_lock=threading.Lock(), _enu={}), FRAME, "src", "k", bad)
        with mock.patch.object(grading._Planner, "_end_lift_calc", calc):
            with revisit.scope() as b1:
                self.assertIsNone(pl._end_lift(np.array([1.0, 2.0])))
            with revisit.scope() as b2:
                self.assertIsNone(pl._end_lift(np.array([1.0, 2.0])))
        self.assertTrue(b1.n and b2.n, "short, and short again from memory")

    def test_adopted_stage_made_under_other_decks_is_not_kept(self):
        """A stage adopted from another process was made under that process's corrected decks: when they are not the
        ones this process keeps with the cell's plans, the plans are not kept for later visits."""
        import revisit
        f = features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]], ["residential", "secondary"])
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)
        a = self.grader(FakeMD(f))
        keys = self.cells(a, la, lo)
        other = [(("d", 1), np.array([[0.0, 0.0], [30.0, 0.0]]), 4.0, np.zeros((2, 6)))]
        with mock.patch.object(grading, "deck_patches_id", lambda dg: "other decks"):
            for key in keys:
                a.store.planner_of_cell(key)._road_stage(key)
        self.assertNotEqual(grading.deck_patches_id(other), grading.deck_patches_id([]))
        b = self.grader(FakeMD(f))
        b.apply(la, lo, np.zeros(50))
        for key in keys:
            self.assertFalse(revisit.has(self.dir, "plans", b.shared._revisit_key(key)))
        os.environ[plancache.RUN_ENV] = "same"          # (the same decks on both sides: kept)
        c = self.grader(FakeMD(f))
        for key in keys:
            c.store.planner_of_cell(key)._road_stage(key)
        d = self.grader(FakeMD(f))
        d.apply(la, lo, np.zeros(50))
        self.assertTrue(all(revisit.has(self.dir, "plans", d.shared._revisit_key(key)) for key in keys))


class LockTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.lock = os.path.join(self.dir, "x.lock")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_release_tries_again_while_the_file_is_open_elsewhere(self):
        # Windows: a lock another process has open cannot be removed (WinError 32); a release that gave up at once left
        # the lock behind and every process after it waited (GitHub #435)
        self.assertTrue(xproc.take(self.lock))
        real, calls = os.remove, []

        def busy(path):
            calls.append(path)
            if len(calls) < 3:
                raise PermissionError(32, "in use")
            real(path)
        with mock.patch.object(xproc.os, "remove", busy):
            xproc.release(self.lock)
        self.assertEqual(len(calls), 3)
        self.assertFalse(os.path.exists(self.lock))

    def test_a_dead_holders_lock_is_taken_over(self):
        # the game closed mid-download (the server's processes end with it): its locks were waited out for STALE_S
        import subprocess
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        with open(self.lock, "w") as f:
            f.write(f"{xproc.KIND_RANK['demand']} 0 {p.pid}")
        t0 = time.monotonic()
        got, mine = xproc.once(self.lock, lambda: None, 5.0, "demand")
        self.assertEqual((got, mine), (None, True), "taken over")
        self.assertLess(time.monotonic() - t0, 1.0)
        xproc.release(self.lock)

    def test_a_holder_that_dies_while_waited_for_is_noticed(self):
        # a holder alive when first looked at that dies later (a road tile's worker killed, a process ended mid-build):
        # its lock was waited out to wait_s (BUILD_WAIT_S, 180 s), its pid never looked at again
        import subprocess
        import threading
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.6)"])
        reaper = threading.Thread(target=p.wait)        # (reaped at once, as the server's pools do: no zombie)
        reaper.start()
        with open(self.lock, "w") as f:
            f.write(f"{xproc.KIND_RANK['demand']} 0 {p.pid}")
        t0 = time.monotonic()
        try:
            got, mine = xproc.once(self.lock, lambda: None, 20.0, "demand")
        finally:
            reaper.join()
        self.assertEqual((got, mine), (None, True), "taken over")
        self.assertLess(time.monotonic() - t0, 0.6 + xproc.ALIVE_RECHECK_S + 1.0)
        xproc.release(self.lock)

    def test_alive(self):
        self.assertTrue(xproc.alive(os.getpid()))
        self.assertTrue(xproc.alive(None))

    def test_waiter_reads_the_holders_urgency_once(self):
        self.assertTrue(xproc.take(self.lock, "demand"))
        with mock.patch.object(xproc, "_holder", wraps=xproc._holder) as lu:
            got, mine = xproc.once(self.lock, lambda: None, 0.3, "demand")
        self.assertEqual((got, mine), (None, False), "waited out wait_s")
        self.assertEqual(lu.call_count, 1)
        xproc.release(self.lock)


if __name__ == "__main__":
    unittest.main()
