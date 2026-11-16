"""vsolve: the one solve of a cell's ground roads (docs/VERTICAL_SOLVER_RFC.md step 2), on plans made by hand."""
import os
import sys
import unittest
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
        fast.require(306, "carriageway_profiles' soft couplings")
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
