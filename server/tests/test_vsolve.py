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
        fast.require(156, "carriageway_profiles")
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

    def test_a_steep_street_keeps_its_grade(self):
        z0 = 0.2 * S                     # (20 % on its ground, over its class's 12 %)
        p = plan(np.c_[S, np.zeros_like(S)], z0.copy())
        zs, info = vsolve.solve([p], [spec("residential")], [z0], [0.12], [0])
        self.assertTrue(info["feasible"] and not info["loosened"], info)
        self.assertLess(float(np.abs(zs[0] - z0).max()), 0.05)


class JoinTest(unittest.TestCase):

    def test_levels_apart_meet_at_no_node(self):
        a = np.c_[S, np.zeros_like(S)]
        b = np.c_[np.full(len(S), 100.0), S]
        self.assertEqual(len(vsolve.joins([a, b], [np.zeros(len(S)), np.zeros(len(S))])), 1)
        self.assertEqual(vsolve.joins([a, b], [np.zeros(len(S)), np.full(len(S), 8.0)]), [])


if __name__ == "__main__":
    unittest.main()
