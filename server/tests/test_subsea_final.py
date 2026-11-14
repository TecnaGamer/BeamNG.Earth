"""subsea: an underwater tunnel network that cannot be solved is "awaiting" (None: the caller faults, nothing built on
it is kept) only while the map may still hold more of it: a map cell at one of its ends not read. Once every cell at
its ends is read (and the resolve's limit of cells not reached) the map has no continuation, and the answer is final:
left out as before, WITHOUT a fault. Waiting for good made Times Square's and Lower Manhattan's map cells short on
every visit (a 26 m and a 59 m covered ramp onto Hudson piers, one end over the water), so no terrain block or road
tile round them was ever kept (2026-11-13)."""
import os
import sys
import threading
import types
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy  # noqa: E402
import mapcells  # noqa: E402
import revisit  # noqa: E402
import roads  # noqa: E402
import subsea  # noqa: E402

LAT, LON = 40.7627, -74.0013


def _owner(read, wet_from=20.0):
    """A tunnel store's stand-in: flat ground, water (2 m deep) east of x = wet_from; read: the cells its resolve
    read (None: no resolve)."""
    frame = geodesy.EnuFrame(LAT, LON)

    def water(la, lo):
        x = np.asarray(frame.geodetic_to_enu(np.asarray(la), np.asarray(lo), np.zeros(np.size(la)))[0])
        return np.where(x > wet_from, 2.0, 0.0), np.zeros(np.size(la))
    o = types.SimpleNamespace(frame=frame, water=water, raw=None, _ground=lambda q: np.full(len(q), 3.0),
                              _tls=threading.local())
    if read is not None:
        o._tls.read = frozenset(read)
    return o


def _pool(x1=30.0):
    """One part, 30 m, from dry land (x = 0) out over the water."""
    spec = types.SimpleNamespace(width=7.0)
    return {("f", 1): (spec, np.array([[0.0, 0.0], [x1, 0.0]]), None, 0, 2)}


class SubseaFinalTest(unittest.TestCase):
    def setUp(self):
        self.cell = str(mapcells.cell_of(LAT, LON))

    def solve(self, owner, pool=None):
        pool = pool or _pool()
        with revisit.scope() as b:
            got = subsea.profiles(owner, sorted(pool), pool)
        return got, b

    def test_awaiting_while_an_end_cell_is_not_read(self):
        for read in (None, set(), {"14/1/1"}):
            got, b = self.solve(_owner(read))
            self.assertIsNone(got, read)

    def test_awaiting_when_the_resolve_reached_its_limit(self):
        read = {self.cell} | {f"14/0/{i}" for i in range(roads.CHAIN_CELLS - 1)}
        self.assertEqual(len(read), roads.CHAIN_CELLS)
        got, _ = self.solve(_owner(read))
        self.assertIsNone(got)

    def test_final_once_every_end_cell_is_read(self):
        got, b = self.solve(_owner({self.cell}))
        self.assertIs(got, subsea.LEFT_OUT)
        self.assertEqual(got, ({}, {}))
        self.assertTrue(b.complete, "no fault from subsea itself")

    def test_the_caller_faults_only_while_awaiting(self):
        # (roads.TunnelProfiles._solve: `answer is None and water` is the fault; LEFT_OUT is an answer)
        import inspect
        src = inspect.getsource(roads.TunnelProfiles._solve)
        self.assertIn("if answer is not None:", src)
        self.assertIn("revisit.fault()", src)
        self.assertIn("self._tls.read = frozenset(loaded)", inspect.getsource(roads.TunnelProfiles._resolve))

    def test_a_loop_is_final_too(self):
        spec = types.SimpleNamespace(width=7.0)
        ring = np.array([[0.0, 0.0], [30.0, 0.0], [30.0, 30.0], [0.0, 0.0]])
        pool = {("f", 1): (spec, ring, None, 0, 4)}
        got, _ = self.solve(_owner(None), pool)
        self.assertIsNone(got)
        got, b = self.solve(_owner({self.cell}), pool)
        self.assertIs(got, subsea.LEFT_OUT)
        self.assertTrue(b.complete)

    def test_a_dry_network_is_solved_as_before(self):
        got, _ = self.solve(_owner({self.cell}, wet_from=1e9))
        self.assertEqual(got[0], {})
        self.assertEqual(got[1], {("f", 1): False})

    def test_awaiting_while_the_solve_read_something_short(self):
        # (a map cell whose read failed is among the resolve's cells all the same: not final)
        with revisit.scope() as b:
            revisit.fault()
            got = subsea.profiles(_owner({self.cell}), sorted(_pool()), _pool())
        self.assertIsNone(got)
        self.assertFalse(b.complete)


def _bore_owner(read, ground, wet):
    """Flat water 2 m deep where wet(x), the ground ground(x) (m, owner frame x)."""
    o = _owner(read)
    frame = o.frame

    def water(la, lo):
        x = np.asarray(frame.geodetic_to_enu(np.asarray(la), np.asarray(lo), np.zeros(np.size(la)))[0])
        return np.where(wet(x), 2.0, 0.0), np.zeros(np.size(la))
    o.water = water
    o._ground = lambda q: ground(np.asarray(q, np.float64)[:, 0])
    return o


def _bore(x1):
    spec = types.SimpleNamespace(width=7.0)
    return {("f", 1): (spec, np.array([[0.0, 0.0], [x1, 0.0]]), None, 0, 2)}


class SubseaInfeasibleTest(unittest.TestCase):
    """A network with both portals mapped whose bounds cannot be met (the solver proves it) is the same answer on every
    build of the same inputs: final (LEFT_OUT, no fault) once its map cells are read, as the stub and loop rules. The
    Fort McHenry Tunnel (I-95, Baltimore) made its map cell short on every build until 2026-11-13: one bore's west
    portal on the DEM's wall of its approach cut, the ground 28 m in lower than the grade could reach."""

    def setUp(self):
        self.cell = str(mapcells.cell_of(LAT, LON))
        # 60 m, portals on the ground at 3 m, water over its middle 20 m: 8 m of cover there is out of reach at 8 %
        self.pool = _bore(60.0)
        self.ground = lambda x: np.full(len(x), 3.0)
        self.wet = lambda x: (x > 20.0) & (x < 40.0)

    def solve(self, owner, pool=None):
        pool = pool or self.pool
        with revisit.scope() as b:
            got = subsea.profiles(owner, sorted(pool), pool)
        return got, b

    def test_final_once_every_end_cell_is_read(self):
        got, b = self.solve(_bore_owner({self.cell}, self.ground, self.wet))
        self.assertIs(got, subsea.LEFT_OUT)
        self.assertTrue(b.complete, "no fault from subsea itself")
        again, _ = self.solve(_bore_owner({self.cell}, self.ground, self.wet))
        self.assertIs(again, subsea.LEFT_OUT, "deterministic")

    def test_awaiting_while_an_end_cell_is_not_read_or_the_limit_reached(self):
        for read in (None, set(), {"14/1/1"}, {self.cell} | {f"14/0/{i}" for i in range(roads.CHAIN_CELLS - 1)}):
            got, _ = self.solve(_bore_owner(read, self.ground, self.wet))
            self.assertIsNone(got, read)

    def test_awaiting_while_an_input_was_short(self):
        with revisit.scope() as b:
            revisit.fault()
            got = subsea.profiles(_bore_owner({self.cell}, self.ground, self.wet), sorted(self.pool), self.pool)
        self.assertIsNone(got)
        self.assertFalse(b.complete)

    def stubbed(self, status, owner, pool=None, infeasible=True, timed_out=False):
        """The answer with the solver saying status (and nothing solved)."""
        real = subsea.fast._ef

        class Stub:
            def __getattr__(self, n):
                return getattr(real, n)

            def carriageway_profiles(self, target, *a):
                return np.zeros(len(target)), dict(converged=False, feasible=False, infeasible=infeasible,
                                                   timed_out=timed_out, status=status, grade_excess=0.0,
                                                   curve_excess=0.0, lower_shortfall_m=0.0, upper_excess_m=0.0)
        subsea.fast._ef = Stub()
        try:
            return self.solve(owner, pool)
        finally:
            subsea.fast._ef = real

    def test_awaiting_on_a_time_limit(self):
        got, _ = self.stubbed("MaxTime", _bore_owner({self.cell}, self.ground, self.wet), infeasible=False,
                              timed_out=True)
        self.assertIsNone(got)

    def test_awaiting_on_approximate_or_dual_statuses(self):
        # (reduced tolerances or a dual certificate prove nothing about the bounds: a numerically troubled solve
        # must not become an omission kept for good, even on a network the bounds show infeasible)
        for status in ("AlmostPrimalInfeasible", "DualInfeasible", "AlmostDualInfeasible", "NumericalError",
                       "InsufficientProgress", "MaxIterations"):
            got, b = self.stubbed(status, _bore_owner({self.cell}, self.ground, self.wet))
            self.assertIsNone(got, status)

    def test_strict_primal_infeasible_needs_the_bounds_proof(self):
        # a network the bounds do not show infeasible (1 km, water over its middle 400 m, 8 m of cover in reach): a
        # solver's "PrimalInfeasible" alone is not final
        pool = _bore(1000.0)
        wet = lambda x: (x > 300.0) & (x < 700.0)
        got, _ = self.stubbed("PrimalInfeasible", _bore_owner({self.cell}, self.ground, wet), pool)
        self.assertIsNone(got)
        # the provably infeasible one is
        got, b = self.stubbed("PrimalInfeasible", _bore_owner({self.cell}, self.ground, self.wet))
        self.assertIs(got, subsea.LEFT_OUT)
        self.assertTrue(b.complete)

    def test_inconsistent_bounds_are_final(self):
        got, b = self.stubbed("InconsistentBounds", _bore_owner({self.cell}, self.ground, self.wet))
        self.assertIs(got, subsea.LEFT_OUT)
        self.assertTrue(b.complete)

    def test_the_proof(self):
        s = np.arange(0.0, 101.0, 4.0)
        lo = np.full(len(s), -np.inf)
        lo[0] = 0.0
        up = np.full(len(s), np.inf)
        up[0] = 0.0
        self.assertFalse(subsea._proven([lo], [up], [s]))
        up[5] = -subsea.GRADE * 20.0 + 0.001        # (just in reach)
        self.assertFalse(subsea._proven([lo], [up], [s]))
        up[5] = -subsea.GRADE * 20.0 - 0.05         # 5 cm out of reach
        self.assertTrue(subsea._proven([lo], [up], [s]))
        up[5] = np.inf
        lo[-1] = up[-1] = subsea.GRADE * 100.0 + 0.05   # two portals further apart in height than the grade allows
        self.assertTrue(subsea._proven([lo], [up], [s]))

    def _reach_case(self):
        # 1 km; water from 300 to 700 m; the east portal's point 7 m over the ground just inside it (a cut's wall):
        # the floor cannot leave the portal steeply enough to stay under that ground
        pool = _bore(1000.0)
        ground = lambda x: np.where(x > 999.0, 10.0, 3.0)
        wet = lambda x: (x > 300.0) & (x < 700.0)
        return _bore_owner({self.cell}, ground, wet), pool

    def test_a_dry_ground_under_a_portals_reach_is_final_by_default(self):
        # (BNE_SUBSEA_RELAX off, the default: infeasible as it is, left out as today, no fault)
        old, subsea.RELAX = subsea.RELAX, False
        try:
            got, b = self.solve(*self._reach_case())
        finally:
            subsea.RELAX = old
        self.assertIs(got, subsea.LEFT_OUT)
        self.assertTrue(b.complete)

    def test_a_dry_ground_under_a_portals_reach_is_solved_with_relax(self):
        # (BNE_SUBSEA_RELAX=on: that ground does not hold the floor within the portal's grade reach)
        old, subsea.RELAX = subsea.RELAX, True
        try:
            got, b = self.solve(*self._reach_case())
        finally:
            subsea.RELAX = old
        self.assertIsNot(got, subsea.LEFT_OUT)
        self.assertIsNotNone(got)
        floors, membership = got
        self.assertEqual(membership, {("f", 1): True})
        z = floors[("f", 1)]
        self.assertAlmostEqual(float(z[0]), 3.0, places=2)
        self.assertAlmostEqual(float(z[-1]), 10.0, places=2)
        self.assertTrue(b.complete)


class CellEntriesShortTest(unittest.TestCase):
    """A map cell whose read fails is not kept by the tunnel store's cell memo (roads.BridgeProfiles._cell_entries):
    each chain solve that reads it reads it again and is short again, and none of them calls its network final on
    the empty copy (review 2026-11-13: one chain's failed read left an empty entry another chain took without the
    fault, then passed subsea._final with the topology incomplete)."""

    def setUp(self):
        self.frame = geodesy.EnuFrame(LAT, LON)
        self.key = mapcells.cell_of(LAT, LON)
        self.calls = 0
        self.fail = True

        def cell_fn(key):
            self.calls += 1
            if self.fail:
                revisit.fault()         # (as MapData's cell read does for a layer that could not be had)
            return None
        self.tp = roads.TunnelProfiles(self.frame, cell_fn, None)
        self.tp._tls.read = frozenset({str(self.key)})

    def chain_solve(self):
        """One chain solve's read of the cell, and whether its network could then be called final."""
        with revisit.scope() as b:
            self.tp._load(self.key, {}, {})
            final = subsea._final(self.tp, [[0.0, 0.0]], "test")
        return b, final

    def test_a_failed_read_is_read_again_and_stays_short(self):
        b1, final1 = self.chain_solve()
        b2, final2 = self.chain_solve()
        self.assertFalse(b1.complete)
        self.assertFalse(b2.complete, "the second chain solve read the cell short too")
        self.assertFalse(final1)
        self.assertFalse(final2, "the second chain solve must stay non-final")
        self.assertGreaterEqual(self.calls, 2)

    def test_a_complete_read_is_kept(self):
        self.fail = False
        b1, final1 = self.chain_solve()
        n = self.calls
        b2, final2 = self.chain_solve()
        self.assertTrue(b1.complete and b2.complete)
        self.assertTrue(final1 and final2)
        self.assertEqual(self.calls, n, "read once")

    def test_a_failed_read_then_a_complete_one(self):
        self.chain_solve()
        self.fail = False
        b, final = self.chain_solve()
        self.assertTrue(b.complete)
        self.assertTrue(final)


class CellEntriesWorkerFaultTest(unittest.TestCase):
    """A cell read whose layer is loaded by a pool worker (MapData: runtimeprofiler.ThreadPoolExecutor, revisit.task /
    bind) that faults there and hands back fallback data (a PMTiles failure, then the parquet fallback): the fault
    reaches the reader's scopes through revisit.take, not this thread's counters, and the read is still short: not kept
    by the cell memo, read again by the next chain solve, never final (review, 2026-11-13)."""

    def setUp(self):
        import runtimeprofiler
        self.pool = runtimeprofiler.ThreadPoolExecutor(max_workers=2)
        self.addCleanup(self.pool.shutdown)
        self.frame = geodesy.EnuFrame(LAT, LON)
        self.key = mapcells.cell_of(LAT, LON)
        self.calls = 0
        self.shared = None

    def owner(self, cell_fn):
        tp = roads.TunnelProfiles(self.frame, cell_fn, None)
        tp._tls.read = frozenset({str(self.key)})
        return tp

    def faulting_layer(self):
        revisit.fault()                 # (the first source failed; the fallback's data comes back all the same)
        return None

    def chain_solve(self, tp):
        with revisit.scope() as b:
            tp._load(self.key, {}, {})
            final = subsea._final(tp, [[0.0, 0.0]], "test")
        return b, final

    def check(self, cell_fn):
        tp = self.owner(cell_fn)
        b1, final1 = self.chain_solve(tp)
        b2, final2 = self.chain_solve(tp)
        self.assertFalse(b1.complete)
        self.assertFalse(b2.complete, "the second chain solve read the cell short too")
        self.assertFalse(final1)
        self.assertFalse(final2, "the second chain solve must stay non-final")
        self.assertGreaterEqual(self.calls, 2, "the read is tried again")

    def test_a_worker_submitted_by_the_read(self):
        def cell_fn(key):
            self.calls += 1
            return self.pool.submit(self.faulting_layer).result()
        self.check(cell_fn)

    def test_a_shared_load_another_caller_started(self):
        # (MapData shares a layer's load already under way: submitted outside this read's scopes, its fault handed
        # over only by take() to whoever takes the result)
        def cell_fn(key):
            self.calls += 1
            if self.shared is None:
                self.shared = self.pool.submit(self.faulting_layer)
            return self.shared.result()
        self.shared = self.pool.submit(self.faulting_layer)     # (no scope: a background prefetch's)
        self.shared.result()
        self.check(cell_fn)


class CellEntriesSpecFaultTest(unittest.TestCase):
    """A cell read complete whose specs are made short (germanroads.adjust_specs, in road_specs / specs_of, faults and
    returns fallback specs when its axes are not there): the entries and the half widths made from it are not kept by
    the memos either; the next chain solve makes them again and stays non-final (review, 2026-11-13)."""

    class Feats:
        ids = ["w1"]
        attrs = {}

        def __len__(self):
            return 1

    def setUp(self):
        self.frame = geodesy.EnuFrame(LAT, LON)
        self.key = mapcells.cell_of(LAT, LON)
        self.reads = 0
        self.specs = 0
        self.fail = True

        def cell_fn(key):
            self.reads += 1
            return self.Feats()             # (read complete)

        def road_specs(feats):
            self.specs += 1
            if self.fail:
                revisit.fault()             # (a survey's lookup that fell back)
            return [None] * len(feats)
        for name, fn in (("road_specs", road_specs), ("_rail_drawn", lambda feats, i: False)):
            old = getattr(roads, name)
            setattr(roads, name, fn)
            self.addCleanup(setattr, roads, name, old)
        self.tp = roads.TunnelProfiles(self.frame, cell_fn, None)
        self.tp.specs_of = lambda feats: [None] * len(feats)
        self.tp._tls.read = frozenset({str(self.key)})

    def chain_solve(self):
        with revisit.scope() as b:
            self.tp._load(self.key, {}, {})
            final = subsea._final(self.tp, [[0.0, 0.0]], "test")
        return b, final

    def test_short_specs_are_made_again_and_stay_non_final(self):
        b1, final1 = self.chain_solve()
        n = self.specs
        b2, final2 = self.chain_solve()
        self.assertFalse(b1.complete or b2.complete)
        self.assertFalse(final1)
        self.assertFalse(final2, "the second chain solve must stay non-final")
        self.assertGreater(self.specs, n, "the specs are made again")
        self.assertNotIn(str(self.key), self.tp.__dict__.get("hw_done", ()))
        self.assertNotIn(str(self.key), self.tp.__dict__.get("cell_entries", {}))

    def test_complete_specs_are_kept(self):
        self.fail = False
        b1, final1 = self.chain_solve()
        n = self.reads, self.specs
        b2, final2 = self.chain_solve()
        self.assertTrue(b1.complete and b2.complete and final1 and final2)
        self.assertEqual((self.reads, self.specs), n, "made once")


if __name__ == "__main__":
    unittest.main()
