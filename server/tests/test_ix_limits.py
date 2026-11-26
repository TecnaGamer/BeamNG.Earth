"""The wall-clock limit on each step of an interchange's pipeline (ix/limits.py): enforced through the cancel mechanism
(xproc.check_cancel), never by killing a thread; a step over its limit stops, says 'failed: time limit N s', leaves an
ixcheck finding, and the interchange is not stored as solved: its claim is given back, nothing takes its place."""
import logging
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import gradeshapes as gs  # noqa: E402
import xproc  # noqa: E402
from ix import cache, limits, owner  # noqa: E402
from test_shape_owner import locks, planner, solutions  # noqa: E402

ENV = "BNE_IX_STEP_LIMITS"


class Env(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ixlimits")
        self.addCleanup(shutil.rmtree, self.dir, True)
        old = os.environ.pop(ENV, None)
        self.addCleanup(lambda: os.environ.__setitem__(ENV, old) if old is not None else os.environ.pop(ENV, None))
        owner.LIMITED.clear()
        self.addCleanup(owner.LIMITED.clear)
        for table in (owner.PROCESS, owner.INDEX, owner.FLIGHTS, owner.EXTENTS):
            table.clear()


class TableTest(Env):
    def test_the_defaults_make_slowness_fail_not_fit_the_worst_case_seen(self):
        self.assertEqual(limits.table(), {"paths": 30.0, "extent": 60.0, "order": 10.0, "terrain": 30.0, "datum": 10.0,
                                          "heights": 120.0})
        # (Shanghai's extent took 1206 s and its heights 5043 s: they fail, and the restructure is the fix)
        self.assertLess(limits.table()["extent"], 1206)
        self.assertLess(limits.table()["heights"], 5043)

    def test_the_environment_overrides_the_steps_it_names_and_ignores_what_it_cannot_read(self):
        os.environ[ENV] = "heights=12000, extent=2400,bogus=5,order=x,datum=-1,terrain"
        got = limits.table()
        self.assertEqual((got["heights"], got["extent"]), (12000.0, 2400.0))
        for step in ("paths", "order", "terrain", "datum"):
            self.assertEqual(got[step], limits.DEFAULTS[step])


class ScopeTest(Env):
    def test_a_step_that_asks_the_cancel_mechanism_stops_at_its_limit_and_the_deadline_is_gone_after(self):
        os.environ[ENV] = "order=0.2"
        t = time.time()
        with self.assertRaises(xproc.TimeLimit) as ctx:
            with limits.scope("order"):
                while True:
                    xproc.check_cancel()
                    time.sleep(0.01)
        self.assertLess(time.time() - t, 5)
        self.assertEqual((ctx.exception.what, ctx.exception.secs), ("order", 0.2))
        self.assertIsNone(xproc.deadline_left())
        xproc.check_cancel()

    def test_a_step_that_never_asks_is_stopped_when_it_ends_late_and_a_quick_one_is_not(self):
        os.environ[ENV] = "datum=0.1"
        with limits.scope("datum"):
            pass
        with self.assertRaises(xproc.TimeLimit):
            with limits.scope("datum"):
                time.sleep(0.3)

    def test_what_the_growth_spent_counts_and_zero_is_no_limit(self):
        os.environ[ENV] = "extent=1,paths=0"
        with self.assertRaises(xproc.TimeLimit):
            with limits.scope("extent", 1.5):
                pass
        with limits.scope("paths", 10 ** 6):
            time.sleep(0.01)

    def test_a_thread_with_no_deadline_never_meets_one(self):
        xproc.check_cancel()
        self.assertIsNone(xproc.deadline_left())

    def test_the_kernels_budget_is_capped_by_what_is_left_of_the_step(self):
        import carriageways
        import fast
        seen = []
        real = fast._ef.carriageway_profiles

        def spy(*a, **k):
            seen.append(a[9])
            return real(*a, **k)
        chains = [dict(t=[0.0, 0.0, 0.0], w=[1.0, 1.0, 1.0], grade=0.1, K=100.0)]
        with unittest.mock.patch.object(fast._ef, "carriageway_profiles", spy):
            carriageways.solve(chains)
            old = xproc.set_deadline("heights", 2.0)
            try:
                carriageways.solve(chains)
            finally:
                xproc.restore_deadline(old)
        self.assertEqual(seen[0], carriageways.BUDGET_S)
        self.assertLessEqual(seen[1], 2.0)


class StepTest(Env):
    """The overpass: one interchange, one region."""

    def region(self, cache_dir=None, hk="r"):
        pl, home = planner(cache_dir)
        return pl, home, pl._shape_region_calc(home, hk)

    def run_logged(self, pl, home, hk="r"):
        handler = logging.Handler()
        lines = []
        handler.emit = lambda rec: lines.append((rec.levelno, rec.getMessage()))
        logger = logging.getLogger("earthd.grading")
        logger.addHandler(handler)
        old = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            got = pl._shape_region_calc(home, hk)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old)
        return got, lines

    def test_normal_steps_are_unaffected(self):
        cache_dir = os.path.join(self.dir, "c")
        pl, home = planner(cache_dir)
        got, lines = self.run_logged(pl, home)
        self.assertTrue(got)
        self.assertEqual(len(solutions(cache_dir)), 1)
        steps = [m for _lv, m in lines if m.startswith("ix step:")]
        self.assertEqual([m.split()[2] for m in steps], list(limits.DEFAULTS))
        self.assertFalse([m for m in steps if "failed" in m])
        self.assertEqual(owner.LIMITED, {})

    def slow_heights(self):
        def slow(*a, **k):
            while True:         # (a solve that goes on: it asks the cancel mechanism as the relaxation rounds do)
                xproc.check_cancel()
                time.sleep(0.01)
        return unittest.mock.patch.object(gs, "solve_shape", slow)

    def test_a_step_over_its_limit_fails_with_its_line_and_the_claim_is_released_and_nothing_is_stored(self):
        os.environ[ENV] = "heights=0.3"
        cache_dir = os.path.join(self.dir, "c")
        pl, home = planner(cache_dir)
        with self.slow_heights():
            got, lines = self.run_logged(pl, home)
        self.assertEqual(got, {}, "no answer, nothing in its place")
        step = [m for _lv, m in lines if m.startswith("ix step: heights")]
        self.assertEqual(len(step), 1)
        self.assertRegex(step[0], r"ix step: heights key - pieces \d+ crossings \d+ [0-9.]+ s failed: time limit 0\.3 s @ ")
        self.assertTrue([m for lv, m in lines if lv >= logging.ERROR and "infeasible system" in m and "time limit 0.3 s" in m],
                        "the eval's failure line")
        self.assertEqual(solutions(cache_dir), [], "not stored as solved")
        self.assertEqual(locks(cache_dir), [], "its claim is released")
        self.assertEqual(owner.FLIGHTS, {})
        self.assertIsNone(owner.have(pl, next(iter(owner.LIMITED))), "not kept either")
        (ik,) = owner.LIMITED
        self.assertTrue(limits.is_limit(owner.LIMITED[ik]["failed"]))

    def test_it_is_not_made_again_for_every_region_of_the_process_and_another_process_may_try(self):
        os.environ[ENV] = "heights=0.3"
        cache_dir = os.path.join(self.dir, "c")
        pl, home = planner(cache_dir)
        with self.slow_heights():
            pl._shape_region_calc(home, "a")
        calls = []
        real = gs.solve_shape
        with unittest.mock.patch.object(gs, "solve_shape", lambda *a, **k: calls.append(1) or real(*a, **k)):
            self.assertEqual(pl._shape_region_calc(home, "b"), {})
            self.assertEqual(calls, [])
            owner.LIMITED.clear()       # (another process: it has not met the limit)
            os.environ.pop(ENV)
            two, _ = planner(cache_dir)
            got = two._shape_region_calc(home, "c")
        self.assertTrue(got)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(solutions(cache_dir)), 1)

    def test_a_growth_over_its_limit_stops_and_gives_back_its_claim(self):
        os.environ[ENV] = "extent=0.3"
        cache_dir = os.path.join(self.dir, "c")
        pl, home = planner(cache_dir)

        real = gs.find_interchanges
        calls = []

        def growth_slow(members, make, **k):
            if k.get("scan") is None:       # (the region's own find, no scan: as it is; the growth's rounds are the slow ones)
                return real(members, make, **k)
            calls.append(1)
            while True:
                xproc.check_cancel()
                time.sleep(0.01)
        with unittest.mock.patch.object(gs, "find_interchanges", growth_slow):
            got, lines = self.run_logged(pl, home)
        self.assertTrue(calls)
        self.assertEqual(got, {})
        self.assertTrue([m for _lv, m in lines if m.startswith("ix step: extent key - failed: time limit 0.3 s")])
        self.assertEqual(locks(cache_dir), [])
        self.assertEqual(solutions(cache_dir), [])
        self.assertEqual(owner.FLIGHTS, {})
        self.assertTrue(owner.LIMITED and all(k[0] == "road" for k in owner.LIMITED), "the growth is not made again")

    def test_a_growth_over_its_limit_carries_the_roads_it_had_found_and_each_of_them_is_limited_too(self):
        def over(planner, net, ix, cid, pt, adopt, hint, seen):
            seen |= {"a", "b"}
            raise xproc.TimeLimit("extent", 1.0)
        with unittest.mock.patch.object(owner, "_grow", over):
            with self.assertRaises(xproc.TimeLimit) as ctx:
                owner.grow(None, None, None, "a", None)
        self.assertEqual(ctx.exception.roads, frozenset({"a", "b"}))

        class Net:
            m = [type("M", (), {"key": ("a",), "kind": "road", "feature": "a"})()]
        sysm = dict(seeds=[0], crossings=[], ends=[])
        ix = type("IX", (), {"sysm": sysm})()

        def again(planner, net, ix, hk, clock):
            raise ctx.exception
        with unittest.mock.patch.object(owner, "_interchange", again),                 unittest.mock.patch.object(owner._gsh, "canonical_id", lambda m: "a"),                 unittest.mock.patch.object(owner, "rank_of", lambda *a: 0):
            owner.interchange(None, Net, ix, "r", {})
        self.assertEqual(set(owner.LIMITED), {("road", "a"), ("road", "b")})
        self.assertIs(owner.LIMITED[("road", "b")], owner.LIMITED[("road", "a")])

    def test_the_step_leaves_a_record_and_ixcheck_reports_a_finding(self):
        from ixcheck import load, run
        os.environ[ENV] = "heights=0.3"
        root = os.path.join(self.dir, "ix")
        with unittest.mock.patch.object(cache, "ROOT", root):
            pl, home = planner()
            with self.slow_heights():
                pl._shape_region_calc(home, "r")
        scanned = load.scan(root)
        self.assertFalse(scanned["errors"])
        rows = run.check_all(scanned)
        found = [f for _a, fs in rows for f in fs if f.code == "time_limit"]
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].step, found[0].level), (6, "fail"))
        self.assertIn("heights", found[0].text)
        _lines, fails, counts = run.report(rows)
        self.assertEqual(counts["6"]["time_limit"]["fail"], 1)
        self.assertTrue([f for f in fails if "time_limit" in f])


if __name__ == "__main__":
    unittest.main()
