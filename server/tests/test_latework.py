"""The lidar off planning's critical path and what was planned on the rule planned again (2026-11-13): a planning
process takes a cell's lidar only when it is on disk (the rule at once otherwise, asked of the server's prefetch), the
server's prefetch reads the cells nearest the player first and never stops, the late work builds short road content
again once its lidar is in, and its complete plans replace the run's short ones where they are read from disk; nothing
made short is ever kept or read as complete."""
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
import blockworker  # noqa: E402
import grading  # noqa: E402
import latework  # noqa: E402
import mapcells  # noqa: E402
import plancache  # noqa: E402
import revisit  # noqa: E402
import roadz  # noqa: E402
import xproc  # noqa: E402
from test_grading import FakeMD  # noqa: E402
from test_roads import features, FRAME  # noqa: E402

CELL = mapcells.CellKey(14, 1, 2)


def provider(tmp):
    rz = roadz.RoadZ(tmp, object())
    rz.enabled = rz.lidar = True
    return rz


class PrefetchedGapsTest(unittest.TestCase):
    """gap_answers in a server run with the prefetch: the lidar on disk is taken, none is waited for."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = {roadz.PREFETCH_ENV: "1", roadz.SESSION_ENV: f"s{time.monotonic_ns()}"}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(roadz, "sources_at", lambda la, lo, lidar=None: ["lidar"])
        p.start()
        self.addCleanup(p.stop)

    def test_not_on_disk_the_rule_at_once_and_asked_for(self):
        rz = provider(self.tmp)
        read = []
        rz.lines = lambda src, key: read.append(key) or time.sleep(2)       # (a slow read: never waited for)
        t0, l0 = time.monotonic(), revisit.thread_lidar_faults()
        got = rz.gap_answers([tuple(CELL)])
        self.assertLess(time.monotonic() - t0, 0.5, "waited for the lidar")
        self.assertEqual(list(got.values()), [None], "the rule meanwhile")
        self.assertGreater(revisit.thread_lidar_faults(), l0, "made on the rule: never kept")
        self.assertEqual(read, [], "read in the planning process")
        self.assertFalse([t for t in threading.enumerate() if t.name == "roadz-gaps"], "a read started")
        want = os.listdir(os.path.join(rz._session_dir(), roadz.WANT_DIR))
        self.assertEqual(want, [f"lidar_{CELL.z}_{CELL.x}_{CELL.y}"], "asked of the server's prefetch")

    def test_on_disk_taken(self):
        rz = provider(self.tmp)
        ln = roadz.Lines.build([], corrected=True)
        ln.gaps = [("f1", (1.0, 2.0), (3.0, 4.0))]
        p = rz._path("lidar", CELL)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").close()               # (the cell's file: the read itself is the stub's)
        rz.lines = lambda src, key: ln
        l0 = revisit.thread_lidar_faults()
        got = rz.gap_answers([tuple(CELL)])
        self.assertEqual(got[CELL], [("f1", (1.0, 2.0), (3.0, 4.0))])
        self.assertEqual(revisit.thread_lidar_faults(), l0, "the lidar's answer: kept")

    def test_pre_cache_still_waits(self):
        with roadz.precache_gaps_wait(0.5):
            self.assertFalse(roadz.prefetched(), "a pre-cache's (and the late work's) builds wait for the lidar")
        self.assertTrue(roadz.prefetched())

    def test_without_the_server_waits_as_before(self):
        with mock.patch.dict(os.environ, {roadz.PREFETCH_ENV: ""}):
            self.assertFalse(roadz.prefetched(), "a probe or tool outside a server run")


class PrefetchQueueTest(unittest.TestCase):
    """RoadZ.prefetch: nearest the player first, the cells planning asked for too, a failed read tried again."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.dict(os.environ, {roadz.SESSION_ENV: f"q{time.monotonic_ns()}"})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(roadz, "sources_at", lambda la, lo, lidar=None: ["lidar"])
        p.start()
        self.addCleanup(p.stop)

    def wait_idle(self, rz, n, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            st = rz._pf()
            with st["cv"]:
                if len(st["done"]) + len(st["failed"]) >= n and not st["busy"]:
                    return
            time.sleep(0.02)
        self.fail("the prefetch did not finish")

    def test_nearest_first_and_wants(self):
        rz = provider(self.tmp)
        order, gate = [], threading.Event()

        def lines(src, key):
            gate.wait(5)
            order.append((key.x, key.y))
            self.assertTrue(roadz._tl.prefetch, "a prefetch read: never stopped by a rule decision")
            return roadz.Lines.build([])
        rz.lines = lines
        keys = [mapcells.CellKey(14, x, 0) for x in (5, 1, 3)]
        with mock.patch.object(roadz, "PREFETCH_CELLS", 1):
            rz.prefetch(keys, where=lambda k: float(k.x))
            rz._want("lidar", mapcells.CellKey(14, 0, 0))     # (a planning process asked: x 0, the nearest)
            time.sleep(roadz.PREFETCH_POLL_S * 1.5)
            gate.set()
            self.wait_idle(rz, 4)
        # (the first pick may come before the want is seen: the rest nearest first)
        self.assertEqual(sorted(order), [(0, 0), (1, 0), (3, 0), (5, 0)])
        self.assertEqual(order[1:], sorted(order[1:]), "nearest first")
        self.assertEqual(rz.prefetch_state(keys[0]), "done")
        self.assertIsNone(rz.prefetch_state(mapcells.CellKey(14, 9, 9)))

    def test_far_cells_dropped_failed_retried(self):
        rz = provider(self.tmp)
        rz.lines = lambda src, key: None        # (a failed read)
        with mock.patch.object(roadz, "PREFETCH_RETRY_S", 0.0):
            rz.prefetch([mapcells.CellKey(14, 1, 0), mapcells.CellKey(14, 2, 0)],
                        where=lambda k: 1e9 if k.x == 2 else 10.0)
            self.wait_idle(rz, 1)
        self.assertEqual(rz.prefetch_state(mapcells.CellKey(14, 1, 0)), "failed", "its builds do without it")
        self.assertIsNone(rz.prefetch_state(mapcells.CellKey(14, 2, 0)), "the player left it: dropped")

    def test_read_not_stopped_for_the_prefetch(self):
        """_read: a cell this run goes without (_dropped) stops a planning read, not the prefetch's."""
        rz = provider(self.tmp)
        rz._dropped.add(str(CELL))
        boxes = [(0.0, 0.0, 10.0, 10.0)]
        with mock.patch.object(roadz.fast, "_ef", mock.Mock(las_decode=lambda body, bb: (np.zeros((0, 3)),
                                                                                         np.zeros(0, np.uint8)))), \
                mock.patch.object(rz, "_node_bytes", lambda name, k: b""), mock.patch.object(roadz, "PART_CLAIMS", False):
            with self.assertRaises(roadz.LidarDropped):
                rz._read("ds", ["0-0-0-0"], boxes, CELL)
            roadz._tl.prefetch = True
            try:
                xyz, _cls = rz._read("ds", ["0-0-0-0"], boxes, CELL)
            finally:
                roadz._tl.prefetch = False
        self.assertEqual(len(xyz), 0)


class PlanUpgradeTest(unittest.TestCase):
    """A cell planned short in this run, then complete by the late pool (its own run): read complete from disk."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        p = mock.patch.dict(os.environ, {plancache.RUN_ENV: "run1", xproc.RUN_ENV: "run1"})
        p.start()
        self.addCleanup(p.stop)
        self.f = features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]], ["residential", "secondary"])
        self.la, self.lo, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.0)

    def grader(self, z, rule=False):
        def elev(la, lo):
            if rule:
                revisit.fault(lidar=True)   # (planned on the lidar rule)
            return z + 0.0 * np.asarray(la)
        g = grading.Grader(FRAME, FakeMD(self.f), elev)
        g.shared = plancache.for_grader(self.dir, g, None, None)
        return g

    def keys(self, g):
        return sorted({k for pl, idx in g.store.planners_of(np.asarray(self.la), np.asarray(self.lo))
                       for k in pl._keys(np.asarray(self.la)[idx], np.asarray(self.lo)[idx])})

    def test_short_stays_short_without_a_complete_copy(self):
        g = self.grader(10.0, rule=True)
        g.apply(self.la, self.lo, np.zeros(50))
        b = self.grader(99.0)                           # (another process of the run)
        for k in self.keys(b):
            self.assertIsNotNone(b.shared.load(k))
            self.assertTrue(b.shared.is_short(k), "the run's short plans: never read as complete")
            self.assertFalse(b.shared.known_complete(k))
        with revisit.scope() as s:
            b.apply(self.la, self.lo, np.zeros(50))
        self.assertFalse(s.complete, "built on short plans: not kept")
        self.assertFalse(any(revisit.has(self.dir, "plans", b.shared._revisit_key(k)) for k in self.keys(b)),
                         "short plans kept")

    def test_late_plans_read_complete(self):
        short = self.grader(10.0, rule=True)
        z_short = short.apply(self.la, self.lo, np.zeros(50))
        with mock.patch.dict(os.environ):
            blockworker.late_env()                      # (the late pool's process)
            self.assertEqual(os.environ[plancache.RUN_ENV], "run1" + blockworker.LATE_RUN)
            self.assertEqual(os.environ[xproc.RUN_ENV], "run1" + blockworker.LATE_RUN)
            late = self.grader(12.0)                    # (its lidar in: other heights, complete)
            z_late = late.apply(self.la, self.lo, np.zeros(50))
            keys = self.keys(late)
            self.assertTrue(all(revisit.has(self.dir, "plans", late.shared._revisit_key(k)) for k in keys), "kept")
        self.assertFalse(np.array_equal(z_short, z_late))
        b = self.grader(99.0)                           # (another process of the first run, reading from disk)
        for k in keys:
            self.assertTrue(os.path.exists(b.shared._path(k) + ".short"), "the run's own file stays short")
        with revisit.scope() as s:
            z_b = b.apply(self.la, self.lo, np.zeros(50))
        np.testing.assert_array_equal(z_b, z_late, "the complete copy read")
        self.assertTrue(s.complete, "built on complete plans: kept")
        for k in keys:
            self.assertIsNotNone(b.shared.load(k))      # (as is_short's callers: in the thread that read them)
            self.assertFalse(b.shared.is_short(k))
            self.assertTrue(b.shared.known_complete(k))

    def test_another_threads_short_read_stays_short(self):
        short = self.grader(10.0, rule=True)
        short.apply(self.la, self.lo, np.zeros(50))
        b = self.grader(99.0)
        k = self.keys(b)[0]
        self.assertIsNotNone(b.shared.load(k))          # (this thread: the short file, before the copy came)
        with mock.patch.dict(os.environ):
            blockworker.late_env()
            self.grader(12.0).apply(self.la, self.lo, np.zeros(50))
        t = threading.Thread(target=b.shared.load, args=(k,))     # (another thread reads the complete copy)
        t.start()
        t.join()
        self.assertTrue(b.shared.is_short(k), "this thread read the short plans: still short")


class LateWorkTest(unittest.TestCase):
    """latework.LateWork: built again once the lidar is in, nearest first (a cell's plans before a geotile), kept or
    tried again, never forever; the run's short plan cells found by its scan."""

    def run_until(self, lw, cond, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not cond():
            time.sleep(0.02)
        lw.stop = True
        with lw.cv:
            lw.cv.notify_all()

    def test_waits_for_the_lidar_then_keeps(self):
        state = {"in": False}
        built = []

        class Prov:
            def lidar_wanted(self, c):
                return True

            def lidar_on_disk(self, c):
                return state["in"]

            def prefetch_state(self, c):
                return "busy"

            def prefetch(self, keys):
                pass
        lw = latework.LateWork(lambda k: built.append(k) or True, lambda k: k in built, lambda k: [CELL], Prov())
        with mock.patch.object(latework, "LATE_WAIT_S", 0.05), mock.patch.object(latework, "LATE_POLL_S", 0.05):
            lw.note(1, 1)
            time.sleep(0.3)
            self.assertEqual(built, [], "built before its lidar was in")
            state["in"] = True
            self.run_until(lw, lambda: built)
        self.assertEqual(built, [("g", 1, 1)])
        self.assertEqual(lw.stats["kept"], 1)

    def test_nearest_first_short_retried_then_left(self):
        built = []
        lw = latework.LateWork(lambda k: built.append(k) or False, lambda k: False, lambda k: [], None)
        lw.where = lambda k: float(k[1]) if k[0] == "g" else float(k[2])
        with mock.patch.object(latework, "LATE_RETRY_S", 0.3), mock.patch.object(latework, "LATE_POLL_S", 0.05), \
                mock.patch.object(latework, "LATE_FAR_M", 50.0), mock.patch.object(latework, "CELL_FIRST_M", 15.0):
            with lw.cv:                 # (all queued before the first pick)
                for gx in (30, 10, 20, 99):
                    lw.note(gx, 0)
                lw.note_item(("c", 14, 22, 0))      # (22 m, less CELL_FIRST_M: 7 m, before the 10 m geotile)
            self.run_until(lw, lambda: lw.stats["given_up"] == 4, 10.0)
        self.assertEqual(built[:4], [("c", 14, 22, 0), ("g", 10, 0), ("g", 20, 0), ("g", 30, 0)], "nearest first")
        self.assertNotIn(("g", 99, 0), built, "the far one dropped")
        self.assertEqual(len(built), 4 * latework.LATE_TRIES, "tried LATE_TRIES times, then left to the next visit")
        self.assertEqual(lw.stats["dropped"], 1)

    def test_kept_already_not_built(self):
        built = []
        lw = latework.LateWork(lambda k: built.append(k) or True, lambda k: True, lambda k: [], None)
        with mock.patch.object(latework, "LATE_POLL_S", 0.05):
            lw.note(3, 3)
            self.run_until(lw, lambda: lw.stats["already"])
        self.assertEqual(built, [])

    def test_scan_notes_short_cells_once(self):
        built, scans = [], []

        def scan():
            scans.append(1)
            return [("c", 14, 1, 2)]
        lw = latework.LateWork(lambda k: built.append(k) or True, lambda k: False, lambda k: [], None, scan=scan)
        with mock.patch.object(latework, "LATE_POLL_S", 0.02), mock.patch.object(latework, "LATE_SCAN_S", 0.05):
            lw.start()
            self.run_until(lw, lambda: len(scans) >= 4)
        self.assertEqual(built, [("c", 14, 1, 2)], "planned again once, kept")


class LateIdleTest(unittest.TestCase):
    def test_waits_for_room(self):
        built, room = [], {"ok": False}
        lw = latework.LateWork(lambda k: built.append(k) or True, lambda k: False, lambda k: [], None,
                               idle=lambda: room["ok"])
        with mock.patch.object(latework, "LATE_POLL_S", 0.02):
            lw.note(1, 1)
            time.sleep(0.2)
            self.assertEqual(built, [], "built while the live work had no room")
            room["ok"] = True
            LateWorkTest.run_until(self, lw, lambda: built)
        self.assertEqual(built, [("g", 1, 1)])


class ServerGlueTest(unittest.TestCase):
    """earthd.Server's late glue: the run's short plan cells found, an item's cells round it."""

    def test_scan_and_cells(self):
        import types
        import earthd
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {plancache.RUN_ENV: "r1"}):
            sig = os.path.join(d, "plans", "r1", plancache.FRAMES, "w1s1")
            os.makedirs(sig)
            for name in ("14_1_2.npz", "14_1_2.npz.short", "14_3_4.npz", "14_3_4.npz.complete"):
                open(os.path.join(sig, name), "wb").close()
            self.assertEqual(earthd.Server.late_scan(types.SimpleNamespace(cache_dir=d)), [("c", 14, 1, 2)])
        self.assertIn(CELL, earthd.Server.late_cells(("c", *CELL)))
        la, lo = CELL.center()
        import geotiles
        gx, gy = geotiles.tile_of(la, lo)[:2]
        self.assertIn(CELL, earthd.Server.late_cells(("g", gx, gy)))


if __name__ == "__main__":
    unittest.main()
