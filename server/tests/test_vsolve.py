"""vsolve: the one solve of a cell's ground roads (docs/VERTICAL_SOLVER_RFC.md step 2), on plans made by hand."""
import os
import sys
import unittest
import unittest.mock
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import grading  # noqa: E402
import vsolve  # noqa: E402

S = np.arange(0.0, 201.0, 2.0)


def plan(xy, z, abut=(False, False), half=4.0):
    return grading.Plan(np.asarray(xy, np.float64), np.asarray(z, np.float64), half, abut)


def spec(cls):
    return SimpleNamespace(cls=cls)


def have_kernel():
    try:
        fast.require(471, "carriageway_profiles' soft couplings")
        return True
    except Exception:
        return False


@unittest.skipUnless(have_kernel(), "earthfast's carriageway_profiles")
class SolveTest(unittest.TestCase):

    def test_a_side_road_meets_the_main_road_and_eases_in(self):
        main = plan(np.c_[S, np.zeros_like(S)], np.full(len(S), 10.0))
        side_xy = np.c_[np.full(len(S), 100.0), S]
        side = plan(side_xy, np.full(len(S), 11.0))          # (its profile 1 m over the main road at the node)
        z0 = [main.z.copy(), side.z.copy()]
        ranks = [grading.road_rank(SimpleNamespace(cls="primary")), grading.road_rank(SimpleNamespace(cls="residential"))]
        zs, info = vsolve.solve([main, side], [spec("primary"), spec("residential")], z0, [0.09, 0.12], ranks)
        self.assertTrue(info["converged"] and info["feasible"], info)
        self.assertEqual(info["joins"], 1)
        self.assertAlmostEqual(float(zs[1][0]), float(zs[0][50]), places=3)     # (one height at the node)
        self.assertLess(abs(float(zs[0][50]) - 10.0), 0.15)                     # (the main road keeps its profile)
        self.assertLess(abs(float(zs[1][-1]) - 11.0), 0.05)                     # (the side road's far end its own)
        g = np.abs(np.diff(zs[1])) / 2.0
        self.assertLessEqual(float(g.max()), 0.12 + 1e-6)

    def test_a_side_road_meets_a_steep_road_between_its_samples(self):
        main = plan(np.c_[S, np.zeros_like(S)], 0.15 * S)                # (15 %, samples every 2 m)
        side = plan(np.c_[np.full(len(S), 101.0), S + 0.5], np.full(len(S), 15.0))     # (lands at x 101: between two)
        z0 = [main.z.copy(), side.z.copy()]
        zs, info = vsolve.solve([main, side], [spec("primary"), spec("residential")], z0, [0.2, 0.2], [8, 2])
        self.assertTrue(info["feasible"], info)
        at = float(np.interp(101.0, S, zs[0]))
        self.assertLess(abs(float(zs[1][0]) - at), 0.01)      # (the nearest sample alone: up to 15 cm off)

    def test_three_ends_at_one_node_on_a_steep_road_stay_feasible(self):
        main = plan(np.c_[S, np.zeros_like(S)], 0.15 * S)
        north = plan(np.c_[np.full(len(S), 101.0), S + 0.3], np.full(len(S), 15.2))
        south = plan(np.c_[np.full(len(S), 101.4), -S - 0.3], np.full(len(S), 15.0))  # (its end 0.72 m from north's)
        z0 = [main.z.copy(), north.z.copy(), south.z.copy()]
        zs, info = vsolve.solve([main, north, south], [spec("primary"), spec("residential"), spec("residential")], z0,
                                [0.2, 0.2, 0.2], [8, 2, 2])
        self.assertTrue(info["feasible"] and not info["fallback"], info)
        self.assertEqual(len(vsolve.nodes([np.asarray(p.xy) for p in (main, north, south)], z0)), 1)
        self.assertAlmostEqual(float(zs[1][0]), float(zs[2][0]), places=4)
        self.assertLess(abs(float(zs[1][0]) - float(np.interp(101.2, S, zs[0]))), 0.04)

    def test_a_deck_end_is_held_and_the_road_ramps_down_within_its_ceiling(self):
        z = np.zeros(len(S))
        z[0] = 5.0                       # (the passes left the deck's height at its abutment)
        p = plan(np.c_[S, np.zeros_like(S)], z, abut=(True, False))
        zs, info = vsolve.solve([p], [spec("secondary")], [np.zeros(len(S))], [0.09], [0])
        self.assertTrue(info["feasible"], info)
        self.assertAlmostEqual(float(zs[0][0]), 5.0, places=4)
        self.assertLessEqual(float((np.abs(np.diff(zs[0])) / 2.0).max()), 0.09 + 1e-6)
        self.assertLess(abs(float(zs[0][-1])), 0.1)          # (200 m on: its profile, the spread's tail)

    def test_margin_pins_and_dips(self):
        p = plan(np.c_[S, np.zeros_like(S)], np.zeros(len(S)))
        z0 = np.zeros(len(S))
        z0[45:56] = 0.0
        dips = [np.full(len(S), np.nan)]
        dips[0][48:53] = -2.0            # (under a deck the passes dipped it 2 m)
        zs, info = vsolve.solve([p], [spec("residential")], [z0], [0.12], [0], pins=[(0, -1, 0.3)], dips=dips)
        self.assertTrue(info["feasible"], info)
        self.assertLessEqual(float(zs[0][48:53].max()), -2.0 + 1e-4)
        self.assertAlmostEqual(float(zs[0][-1]), 0.3, places=4)

    def test_a_ceiling_that_cannot_hold_breaks_where_it_must_and_nowhere_else(self):
        # (a deck end held 6 m over a road whose other end is held at 0 m 40 m on: 15 %, over its class's 9 %; hard,
        # the whole solve was infeasible; soft, the grade is broken there and the rest of the road keeps its own)
        z = np.zeros(len(S))
        z[0] = 6.0
        p = plan(np.c_[S, np.zeros_like(S)], z, abut=(True, False))
        zs, info = vsolve.solve([p], [spec("secondary")], [np.zeros(len(S))], [0.09], [0], pins=[(0, 20, 0.0)])
        self.assertTrue(info["feasible"] and not info["fallback"], info)
        self.assertGreater(info["grade_broken"], 0, info)
        self.assertAlmostEqual(float(zs[0][0]), 6.0, places=4)
        self.assertAlmostEqual(float(zs[0][20]), 0.0, places=4)
        g = np.abs(np.diff(zs[0])) / 2.0
        # (the least excess, 6 m - 9 % of 40 m: no segment between the two under its ceiling, which would put more
        # over it elsewhere; how it is spread over them, the smoothing's)
        self.assertGreater(float(g[:20].min()), 0.09 - 2e-3)
        self.assertLess(float(g[:20].max()), 0.3)
        self.assertLessEqual(float(g[20:].max()), 0.09 + 1e-3)   # (past the pin, where it can hold: held)

    def test_a_road_under_a_deck_is_not_raised_to_meet_a_junction(self):
        # (the side road runs under a deck from 10 m to 40 m along it, capped at its height there; its end meets the
        # main road 1 m higher: the main road comes down to it, the side road does not rise into the deck)
        main = plan(np.c_[S, np.zeros_like(S)], np.full(len(S), 11.0))
        side = plan(np.c_[np.full(len(S), 100.0), S], np.full(len(S), 10.0))
        z0 = [main.z.copy(), side.z.copy()]
        caps = [None, np.where((S >= 2) & (S <= 40), 10.0, np.nan)]
        zs, info = vsolve.solve([main, side], [spec("primary"), spec("residential")], z0, [0.09, 0.12], [8, 2],
                                caps=caps)
        self.assertTrue(info["feasible"] and not info["fallback"], info)
        self.assertEqual(info["clear_broken"], 0, info)
        self.assertLessEqual(float(zs[1][1:21].max()), 10.0 + 1e-3)
        self.assertAlmostEqual(float(zs[1][0]), float(zs[0][50]), places=3)
        free, _ = vsolve.solve([main, side], [spec("primary"), spec("residential")], z0, [0.09, 0.12], [8, 2])
        self.assertGreater(float(free[1][1:21].max()), 10.05)     # (without the cap: raised under the deck)

    def test_a_cap_that_cannot_hold_is_broken_and_said_where(self):
        p = plan(np.c_[S, np.zeros_like(S)], np.full(len(S), 5.0), abut=(True, False))     # (its deck end at 5 m)
        caps = [np.where(S <= 6, 4.0, np.nan)]          # (capped at 4 m from its end: the deck end wins)
        zs, info = vsolve.solve([p], [spec("residential")], [np.full(len(S), 5.0)], [0.12], [0], caps=caps)
        self.assertTrue(info["feasible"] and not info["fallback"], info)
        self.assertGreater(info["clear_broken"], 0, info)
        self.assertEqual(info["clear_worst"]["plan"], 0)
        self.assertAlmostEqual(float(zs[0][0]), 5.0, places=4)
        self.assertLessEqual(float(zs[0][3]), 4.0 + 1e-3)       # (where it can hold: held)

    def test_an_approach_the_rule_raised_stays_raised(self):
        # (the approach rule raised the road's last 40 m onto its deck by up to 3 m: floors there; without them the
        # solve, whose target is the profile, left it on its ground)
        p = plan(np.c_[S, np.zeros_like(S)], np.zeros(len(S)))
        lift = np.where(S >= 160, (S - 160) / 40 * 3.0, np.nan)
        lift[lift < 0.3] = np.nan
        zs, info = vsolve.solve([p], [spec("residential")], [np.zeros(len(S))], [0.12], [0], floors=[lift])
        self.assertTrue(info["feasible"] and not info["clear_broken"], info)
        ok = np.isfinite(lift)
        self.assertGreaterEqual(float((zs[0][ok] - lift[ok]).min()), -1e-3)

    def test_a_steep_street_keeps_its_grade(self):
        z0 = 0.2 * S                     # (20 % on its ground, over its class's 12 %)
        p = plan(np.c_[S, np.zeros_like(S)], z0.copy())
        zs, info = vsolve.solve([p], [spec("residential")], [z0], [0.12], [0])
        self.assertTrue(info["feasible"] and not info["grade_broken"], info)
        self.assertLess(float(np.abs(zs[0] - z0).max()), 0.05)


class JoinTest(unittest.TestCase):

    def test_levels_apart_meet_at_no_node(self):
        a = np.c_[S, np.zeros_like(S)]
        b = np.c_[np.full(len(S), 100.0), S]
        self.assertEqual(len(vsolve.joins([a, b], [np.zeros(len(S)), np.zeros(len(S))])), 1)
        self.assertEqual(vsolve.joins([a, b], [np.zeros(len(S)), np.full(len(S), 8.0)]), [])


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(have_kernel(), "earthfast's carriageway_profiles")
class OnTest(unittest.TestCase):
    """BNE_VSOLVE=on (the RFC's step 3): the solve's heights in the plans, under their own cache key."""

    def setUp(self):
        p = unittest.mock.patch.multiple(vsolve, ON=True, SHADOW=True)
        p.start()
        self.addCleanup(p.stop)

    def test_its_plans_are_kept_apart(self):
        import plancache
        with unittest.mock.patch.object(vsolve, "ON", False):
            off = plancache.plans_format()
        self.assertEqual(plancache.plans_format(), off + ".vs1")

    def test_a_dip_comes_back_to_its_ground_without_ringing(self):
        """A road under a motorway bridge dips, and is back on its ground past the ramp, no higher: bending and the
        hold alone rang 0.16 m over it there, and 0.31 m at its end."""
        import test_grading as tg
        f = tg.features([[(-200, 0), (200, 0)], [(0, -20), (0, 20)]], ["residential", "motorway"],
                        flags=[(1, "is_bridge")])
        g = grading.Grader(tg.FRAME, tg.FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
        xs = np.arange(-200.0, 201.0, 5.0)
        la, lo, _ = tg.FRAME.enu_to_geodetic(xs, np.zeros(len(xs)), 0.0)
        z = g.apply(la, lo, np.full(len(xs), 10.0))
        self.assertLess(float(z[len(xs) // 2]), 10.0 - 0.6 * grading.UNDER_CLEAR_M, "under the deck")
        self.assertLessEqual(float(z.max()), 10.0 + 0.02, "no higher than its ground")
        self.assertAlmostEqual(float(z[1]), 10.0, delta=0.1, msg="back up past the ramp")


def have_pairs():
    try:
        fast.require(472, "plan_corridor_pairs")
        return True
    except Exception:
        return False


@unittest.skipUnless(have_kernel() and have_pairs(), "earthfast's plan_corridor_pairs")
class CorridorTest(unittest.TestCase):
    """Roads side by side at one height (share_corridors' rule as the solve's rows, BNE_VSOLVE_CORRIDOR=on): a slip
    road 1 m beside a primary whose middle the underpass rule dipped 1.5 m follows it down, to what the gap holds as a
    bank."""

    def setUp(self):
        patch = unittest.mock.patch.object(vsolve, "CORRIDOR_ROWS_ON", True)
        patch.start()
        self.addCleanup(patch.stop)

    def roads(self, link=True):
        a = plan(np.c_[S, np.zeros_like(S)], np.zeros(len(S)))
        b = plan(np.c_[S, np.full(len(S), 8.0)], np.zeros(len(S)))
        for p in (a, b):
            p.road_half = 3.5                   # (8 m apart: a 1 m gap, 0.4 m of bank)
        specs = [SimpleNamespace(cls="primary", kind="road"),
                 SimpleNamespace(cls="primary_link" if link else "primary", kind="road")]
        dips = [np.where((S >= 80) & (S <= 120), -1.5, np.nan), None]
        return [a, b], specs, dips

    def solve(self, plans, specs, dips, corridors):
        z0 = [p.z.copy() for p in plans]
        ranks = [grading.road_rank(sp) for sp in specs]
        return vsolve.solve(plans, specs, z0, [0.09, 0.09], ranks, dips=dips, corridors=corridors)

    def test_the_slip_road_follows_the_dip(self):
        plans, specs, dips = self.roads()
        pairs = grading.corridor_pairs([("a", plans[0]), ("b", plans[1])], specs)
        self.assertIsNotNone(pairs)
        self.assertGreater(len(pairs[0]), 0)
        zs, info = self.solve(plans, specs, dips, pairs)
        self.assertTrue(info["feasible"] and not info["fallback"], info)
        self.assertLessEqual(float(zs[0][40:61].max()), -1.5 + 1e-4)          # (the dip: hard)
        self.assertGreater(info["corridor_rows"], 0, info)
        diff = np.abs(zs[1] - zs[0])
        self.assertLess(float(diff[45:56].max()), 0.4 + 0.05, info)            # (the bank the gap holds)
        self.assertLess(float(zs[1][50]), -1.0)

    def test_without_rows_it_stays_on_its_profile(self):
        plans, specs, dips = self.roads()
        zs, info = self.solve(plans, specs, dips, None)
        self.assertEqual(info["corridor_rows"], 0)
        self.assertGreater(float(np.abs(zs[1] - zs[0])[50]), 1.4)

    def test_two_through_roads_keep_their_own(self):
        plans, specs, dips = self.roads(link=False)
        pairs = grading.corridor_pairs([("a", plans[0]), ("b", plans[1])], specs)
        self.assertTrue(pairs is None or len(pairs[0]) == 0)


@unittest.skipUnless(have_kernel(), "earthfast's carriageway_profiles")
class SoloTest(unittest.TestCase):
    """BNE_VSOLVE=solo: the solve without the passes it replaces, and the passes again where it finds no heights."""

    @staticmethod
    def heights(**modes):
        import test_grading as tg
        f = tg.features([[(-200, 0), (200, 0)], [(0, -20), (0, 20)], [(-60, 0), (-60, 80)]],
                        ["residential", "motorway", "residential"], flags=[(1, "is_bridge")])
        with unittest.mock.patch.multiple(vsolve, **modes):
            g = grading.Grader(tg.FRAME, tg.FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
            xs = np.arange(-200.0, 201.0, 5.0)
            la, lo, _ = tg.FRAME.enu_to_geodetic(xs, np.zeros(len(xs)), 0.0)
            return g.apply(la, lo, np.full(len(xs), 10.0))

    def test_without_a_solve_the_plans_are_the_passes(self):
        passes = self.heights(ON=False, SHADOW=False, SOLO=False)
        with unittest.mock.patch.object(vsolve, "solve", side_effect=RuntimeError("no solve")), \
                self.assertLogs("earthd.grading", "WARNING") as cm:
            solo = self.heights(ON=True, SHADOW=True, SOLO=True)
        self.assertTrue(any("no solve in solo mode" in m for m in cm.output))
        np.testing.assert_array_equal(solo, passes)

    def test_with_one_the_dip_is_the_solve_s(self):
        solo = self.heights(ON=True, SHADOW=True, SOLO=True)
        self.assertLess(float(solo[40]), 10.0 - 0.6 * grading.UNDER_CLEAR_M, "under the deck")


class CapsTest(unittest.TestCase):
    """The caps under decks (grading's _vsolve_caps), on map data made by hand."""

    def _caps(self, decks):
        import test_grading as tg
        f = tg.features([[(-200, 0), (200, 0)]] + decks, ["residential"] + ["motorway"] * len(decks),
                        flags=[(1 + k, "is_bridge") for k in range(len(decks))])
        g = grading.Grader(tg.FRAME, tg.FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
        la, lo, _ = tg.FRAME.enu_to_geodetic(np.array([0.0]), np.array([0.0]), 0.0)
        g.apply(la, lo, np.full(1, 10.0))
        planner = next(iter(g.store.planners.values()))
        key = next(iter(planner.cells))
        xs = np.arange(-50.0, 51.0, 1.0)
        la, lo, _ = tg.FRAME.enu_to_geodetic(xs, np.zeros_like(xs), 0.0)
        e, n, _ = planner.frame.geodetic_to_enu(la, lo, 0.0)         # (the planner's own frame)
        road = plan(np.c_[e, n], np.full(len(xs), 10.0))
        got = planner._vsolve_caps(key, [road], [np.full(len(xs), 9.0)])[0]
        return xs, got

    def test_a_road_under_a_joint_of_two_decks_is_capped(self):
        """A viaduct mapped in two parts joined 2 m from the road under it: a joint is no abutment, so the road is
        capped there (left out, the solve raised a junction under the Shuto's viaduct 2.7 m)."""
        xs, got = self._caps([[(5, -20), (5, 2)], [(5, 2), (5, 20)]])
        self.assertIsNotNone(got)
        self.assertTrue(np.isfinite(got[np.abs(xs - 5.0) < 1.0]).all())

    def test_a_road_by_an_abutment_is_not(self):
        """Within APPROACH_MIN_CROSS_M of a deck's real end, a junction at grade: no cap."""
        xs, got = self._caps([[(5, 2), (5, 40)]])
        self.assertTrue(got is None or not np.isfinite(got[np.abs(xs - 5.0) < 1.0]).any())
