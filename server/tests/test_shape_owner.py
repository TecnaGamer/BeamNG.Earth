"""One interchange, one owner (grading._Planner._interchange, plancache.PlanCache.shape_*): the interchange found whole
from whichever region's piece of it, its key and the cell that owns it the same from every cell, solved once by the
process that takes its lock while every other thread and process waits and reads, handed over when the owner is gone or
cancelled, and none of it with BNE_SHAPES off."""
import glob
import json
import multiprocessing
import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import gradeshapes as gs  # noqa: E402
import grading  # noqa: E402
import mapcells  # noqa: E402
import plancache  # noqa: E402
import pools  # noqa: E402
import revisit  # noqa: E402
import xproc  # noqa: E402
from ix import owner, s2_extent  # noqa: E402
from test_approaches import overpass, flat_elev  # noqa: E402
from test_roads import LAT, LON  # noqa: E402

REPLAY = os.path.join(HERE, "fixtures", "interchange_popoli_ss17_underpass.npz")
AT = (42.174704, 13.823807)


class FakeMD:
    def __init__(self, feats):
        self.feats = feats

    def cell(self, key, layers=None, kind=None):
        class C:
            pass
        c = C()
        c.layers = {"roads": self.feats}
        return c


def planner(cache_dir=None):
    """The overpass's planner (one interchange), its store sharing cache_dir with the other processes asking."""
    home = mapcells.cell_of(LAT, LON)
    store = grading.PlanStore(FakeMD(overpass()), flat_elev)
    if cache_dir is not None:
        store.shared = plancache.PlanCache(cache_dir, "w0s0")
    return store.planner_of_cell(home), home


def local(pl, home):
    """(net, interchanges, members, cells) of the region of cell home: its own find (ix.s2_extent.find), each interchange
    only the piece of it the region's box has."""
    lon0, lat0, lon1, lat1 = (float(v) for v in home.bounds())
    ce, cn, _ = pl.frame.geodetic_to_enu(np.array([lat0, lat0, lat1, lat1]), np.array([lon0, lon1, lon0, lon1]), 0.0)
    h_lo = np.array([np.min(ce), np.min(cn)]) - grading.SHAPES_PAD_M
    h_hi = np.array([np.max(ce), np.max(cn)]) + grading.SHAPES_PAD_M
    net, mine, loaded, members = s2_extent.find(pl, h_lo, h_hi)
    return net, mine, members, loaded


def whole(pl, net, ix):
    """(key, net, Interchange) of the whole interchange the region's piece ix is part of (owner.grow)."""
    cid = sorted({gs.canonical_id(net.m[i]) for i in ix.sysm["seeds"]})[0]
    ik, n2, i2, _others = owner.grow(pl, net, ix, cid, owner.probe(pl, net, ix.sysm, cid), adopt=False)
    return ik, n2, i2


def locks(cache_dir):
    return glob.glob(os.path.join(cache_dir, "**", "*.lock"), recursive=True)


def solutions(cache_dir):
    return glob.glob(os.path.join(cache_dir, "**", "shapes", "sol", "**", "*.npz"), recursive=True)


def heights(prof):
    return {fid: [(np.asarray(z).round(9).tolist(), m) for _xy, z, m in items] for fid, items in sorted(prof.items())}


def _child(cache_dir, out, solve_log, start, pause, die):
    """A process asking for the overpass's heights: every solve it makes is written to solve_log (its pid); die: it ends
    abruptly in its first solve (its lock held)."""
    real = gs.solve_shape

    def solve(*a, **k):
        with open(solve_log, "a") as fh:
            fh.write("%d\n" % os.getpid())
        time.sleep(pause)
        if die:
            os._exit(3)
        return real(*a, **k)
    gs.solve_shape = solve
    pl, home = planner(cache_dir)
    start.wait(60.0)
    got = pl._shape_region_calc(home, "r")
    with open(out, "w") as fh:
        json.dump(heights(got), fh)


class OwnerKeyTest(unittest.TestCase):
    """The interchange's key and its owner cell are the same from every cell that reaches it."""

    def test_the_key_and_the_owner_cell_agree_from_every_cell(self):
        pl, home = planner()
        net, mine, _members, _loaded = local(pl, home)
        self.assertGreaterEqual(len(mine), 1)
        got = []
        for ix in mine:
            ik, n2, i2 = whole(pl, net, ix)
            got.append((ik, str(owner.owner_cell(pl, n2, i2.sysm))))
        # (the same interchange asked for again from a region of another cell: the neighbours' boxes cut it differently)
        keys = {}
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                other = mapcells.CellKey(home.z, home.x + dx, home.y + dy)
                n3, mine3, _m, _l = local(pl, other)
                for ix in mine3:
                    ik, n4, i4 = whole(pl, n3, ix)
                    keys.setdefault(ik, set()).add(str(owner.owner_cell(pl, n4, i4.sysm)))
        for ik, cell in got:
            self.assertIn(ik, keys, "reached from the neighbouring cells too")
            self.assertEqual(keys[ik], {cell}, "one owner cell")

    @unittest.skipUnless(os.path.exists(REPLAY), "the Popoli replay")
    def test_a_replay_found_from_the_cells_round_it(self):
        import interchanges
        from interchange_audit_run import planner_of
        rep = interchanges.load("popoli_ss17_underpass")
        with unittest.mock.patch.object(revisit, "ON", False), unittest.mock.patch.object(grading, "SHAPES_ON", True):
            pl = planner_of(rep)
            centre = mapcells.cell_of(*AT)
            by_cell = {}
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    home = mapcells.CellKey(centre.z, centre.x + dx, centre.y + dy)
                    try:
                        net, mine, _m, _l = local(pl, home)
                    except Exception:       # (a cell the replay has no data for)
                        continue
                    for ix in mine:
                        ik, n2, i2 = whole(pl, net, ix)
                        by_cell.setdefault(ik, []).append((str(home), str(owner.owner_cell(pl, n2, i2.sysm)),
                                                           len(gs.system_ids(n2, i2.sysm))))
            self.assertTrue(by_cell)
            for ik, rows in by_cell.items():
                self.assertEqual(len({r[1] for r in rows}), 1, "one owner cell for %s" % ik[:8])
                self.assertEqual(len({r[2] for r in rows}), 1, "the same members from every cell")


class SolvedOnceTest(unittest.TestCase):
    """Processes asking for one interchange at once solve it once and get the very heights."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_processes_asking_at_once_solve_it_once(self):
        ctx = multiprocessing.get_context("spawn")
        start = ctx.Event()
        log = os.path.join(self.dir, "solves.txt")
        outs = [os.path.join(self.dir, "out%d.json" % n) for n in range(3)]
        cache = os.path.join(self.dir, "cache")
        procs = [ctx.Process(target=_child, args=(cache, o, log, start, 1.0, False)) for o in outs]
        for p in procs:
            p.start()
        time.sleep(0.5)
        start.set()
        for p in procs:
            p.join(240.0)
            self.assertEqual(p.exitcode, 0)
        pl, home = planner()
        systems = len(local(pl, home)[1])
        with open(log) as fh:
            solved = fh.read().split()
        self.assertEqual(len(solved), systems, "each interchange solved once, not once a process: %s" % solved)
        res = [json.load(open(o)) for o in outs]
        self.assertTrue(res[0])
        self.assertEqual(res[0], res[1])
        self.assertEqual(res[0], res[2])
        self.assertEqual(len(solutions(cache)), systems)
        self.assertEqual(locks(cache), [])

    def test_a_later_process_reads_what_was_stored(self):
        cache = os.path.join(self.dir, "cache")
        one, home = planner(cache)
        a = one._shape_region_calc(home, "a")
        two, _ = planner(cache)
        calls = []
        with unittest.mock.patch.object(gs, "solve_shape", lambda *x, **k: calls.append(1)):
            b = two._shape_region_calc(home, "b")
        self.assertEqual(calls, [], "read, not solved")
        self.assertEqual(heights(a), heights(b))

    def test_the_stored_heights_do_not_depend_on_the_readers_frame(self):
        """Stored as lat / lon and heights: a planner of another frame reads the same heights at the same places."""
        cache = os.path.join(self.dir, "cache")
        one, home = planner(cache)
        a = one._shape_region_calc(home, "a")
        shifted = grading.PlanStore(FakeMD(overpass()), flat_elev).planner_of_cell(home)
        shifted.shared = plancache.PlanCache(cache, "w0s0")
        with unittest.mock.patch.object(gs, "solve_shape", lambda *x, **k: self.fail("solved")):
            b = shifted._shape_region_calc(home, "b")
        self.assertEqual(heights(a), heights(b))
        for fid in a:
            for (xa, _za, _ma), (xb, _zb, _mb) in zip(a[fid], b[fid]):
                np.testing.assert_allclose(xa, xb, atol=1e-3)


class HandOverTest(unittest.TestCase):
    """The owner gone or cancelled: the next one takes the interchange over, never beside it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_owner_that_dies_hands_over(self):
        ctx = multiprocessing.get_context("spawn")
        start = ctx.Event()
        cache = os.path.join(self.dir, "cache")
        log = os.path.join(self.dir, "solves.txt")
        dead = ctx.Process(target=_child, args=(cache, os.path.join(self.dir, "dead.json"), log, start, 2.0, True))
        dead.start()
        start.set()
        end = time.time() + 120.0
        while not os.path.exists(log) and time.time() < end:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(log), "the first owner is solving")
        pl, home = planner(cache)
        out = []
        real = gs.solve_shape

        def solve(*a, **k):
            with open(log, "a") as fh:
                fh.write("%d\n" % os.getpid())
            return real(*a, **k)
        with unittest.mock.patch.object(gs, "solve_shape", solve):
            t = threading.Thread(target=lambda: out.append(pl._shape_region_calc(home, "r")))
            t.start()                           # (waits for the holder: its lock is there)
            t.join(180.0)
        dead.join(60.0)
        self.assertFalse(t.is_alive(), "the waiter took the lock over from the dead owner")
        self.assertEqual(dead.exitcode, 3)
        self.assertTrue(out and out[0])
        with open(log) as fh:
            self.assertEqual(len(fh.read().split()), 2, "the dead owner's solve, then the one that took over")
        # (the dead owner's claim on the extent stays as it was: nobody asked for it after the index was written)
        self.assertEqual([p for p in locks(cache) if p.endswith(".npz.lock")], [], "the solution's claim is given back")
        self.assertTrue(solutions(cache))

    def test_a_cancelled_owner_releases_the_lock_and_the_next_solves(self):
        cache = os.path.join(self.dir, "cache")
        one, home = planner(cache)
        real = gs.solve_shape
        calls = []

        def left(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise pools.Abandoned()
            return real(*a, **k)
        with unittest.mock.patch.object(gs, "solve_shape", left):
            with self.assertRaises(pools.Abandoned):
                one._shape_region_calc(home, "a")
            self.assertEqual(locks(cache), [], "the abandoned owner released its lock")
            self.assertEqual(solutions(cache), [])
            two, _ = planner(cache)
            got = two._shape_region_calc(home, "b")
        self.assertTrue(got)
        self.assertEqual(len(solutions(cache)), 1)
        self.assertGreaterEqual(len(calls), 2)

    def test_cancelling_keeps_the_stored_solution(self):
        cache = os.path.join(self.dir, "cache")
        one, home = planner(cache)
        one._shape_region_calc(home, "a")
        stored = solutions(cache)
        self.assertTrue(stored)
        two, _ = planner(cache)
        xproc.set_thread_cancel(lambda: True)
        try:
            with self.assertRaises(pools.Abandoned):
                two._shape_region_calc(home, "b")
        finally:
            xproc.set_thread_cancel(None)
        self.assertEqual(solutions(cache), stored)
        three, _ = planner(cache)
        with unittest.mock.patch.object(gs, "solve_shape", lambda *x, **k: self.fail("solved")):
            self.assertTrue(three._shape_region_calc(home, "c"))


class NoWaitBudgetTest(unittest.TestCase):
    """A waiter never solves beside a holder that lives: however long the holder takes."""

    def test_the_claim_waits_for_a_live_holder(self):
        with tempfile.TemporaryDirectory() as d:
            sh = plancache.PlanCache(d, "w0s0")
            got, mine = sh.shape_claim("sol", "ab" * 10, lambda: None)
            self.assertEqual((got, mine), (None, True))
            out = []
            with unittest.mock.patch.object(xproc, "POLL_S", 0.01):
                t = threading.Thread(target=lambda: out.append(sh.shape_claim("sol", "ab" * 10, lambda: None)),
                                     daemon=True)
                t.start()
                t.join(2.0)
                self.assertTrue(t.is_alive(), "waiting (no budget past which it solves too)")
                sh.shape_release("sol", "ab" * 10)
                t.join(10.0)
            self.assertEqual(out, [(None, True)], "handed over when the holder let go")
            sh.shape_release("sol", "ab" * 10)


class FlagOffTest(unittest.TestCase):
    """BNE_SHAPES off: nothing of the owner's machinery is asked for."""

    @unittest.mock.patch.object(grading, "SHAPES_ON", False)
    def test_nothing_is_asked_for_without_the_shapes(self):
        import test_roads
        f = test_roads.features([[(-300, 0), (300, 0)], [(0, -20), (0, 20)], [(0, -260), (0, -20)],
                                 [(0, 20), (0, 260)]], ["motorway", "tertiary", "tertiary", "tertiary"],
                                flags=[(1, "is_bridge")])
        boom = unittest.mock.Mock(side_effect=AssertionError("asked"))
        with unittest.mock.patch.object(owner, "interchange", boom), \
                unittest.mock.patch.object(grading._Planner, "_shape_region_calc", boom), \
                unittest.mock.patch.object(plancache.PlanCache, "shape_claim", boom):
            g = grading.Grader(test_roads.FRAME, FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
            la, lo, _ = test_roads.FRAME.enu_to_geodetic(np.array([0.0, 250.0]), np.array([0.0, 0.0]), 0.0)
            z = g.apply(la, lo, np.full(2, 10.0))
        self.assertEqual(len(z), 2)
        boom.assert_not_called()
        self.assertEqual(owner.FLIGHTS, {})
        self.assertNotIn(".sh", plancache.plans_format())


if __name__ == "__main__":
    unittest.main()
