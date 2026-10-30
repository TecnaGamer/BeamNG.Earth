"""plancache: a cell's road plans shared between the server's processes: the same heights from the cache, one
process plans a cell while the others wait (not behind a less favoured one), stale locks, old runs pruned."""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
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
        self.assertEqual(got[0][0], ([], [], [], []))
        self.assertEqual(other.stats["waited"], 1)

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


if __name__ == "__main__":
    unittest.main()
