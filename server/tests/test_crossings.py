"""crossings.py (BNE_INTERCHANGE): a single-span bridge and the roads under it solved together: the cheaper of raising
the bridge and lowering the roads, from the ground they can be trusted for (Wezep's A28 over Zuiderzeestraatweg:
lidar's fill under the deck, the street either side, 2026-11-01)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import crossings as xs  # noqa: E402


class SolveTest(unittest.TestCase):
    def test_enough_already(self):
        lift, drops = xs.solve([10.0], [2.0], 7.0, 4.0, [3.0])
        self.assertEqual(lift, 0.0)
        self.assertEqual(drops.tolist(), [0.0])

    def test_cheaper_road_dips(self):
        lift, drops = xs.solve([5.0], [2.0], 7.0, 4.0, [2.0])       # 4 m short; lowering costs less
        self.assertEqual(lift, 0.0)
        self.assertAlmostEqual(drops[0], 4.0)

    def test_cheaper_bridge_rises(self):
        lift, drops = xs.solve([5.0], [2.0], 7.0, 2.0, [6.0])
        self.assertAlmostEqual(lift, 4.0)
        self.assertAlmostEqual(drops[0], 0.0)

    def test_one_lift_serves_several_roads(self):
        # three cheap roads under one bridge: lowering all three (3 x 2) costs more than one lift (5)
        lift, drops = xs.solve([5.0, 5.0, 5.0], [2.0, 2.0, 2.0], 7.0, 5.0, [2.0, 2.0, 2.0])
        self.assertAlmostEqual(lift, 4.0)
        self.assertTrue(np.allclose(drops, 0.0))

    def test_caps_share(self):
        # the lift is capped: the road takes the rest
        lift, drops = xs.solve([5.0], [2.0], 7.0, 1.0, [9.0], max_lift=2.5)
        self.assertAlmostEqual(lift, 2.5)
        self.assertAlmostEqual(drops[0], 1.5)

    def test_beyond_both_caps_low_bridge(self):
        lift, drops = xs.solve([0.0], [0.0], 7.0, 1.0, [9.0], max_lift=2.0, max_drop=1.0)
        self.assertAlmostEqual(lift, 2.0)
        self.assertAlmostEqual(drops[0], 1.0)

    def test_tied_low_road_sends_motorway_up(self):
        # a secondary street with junctions near, on low ground, under a motorway: the motorway rises
        w_down = xs.down_cost("secondary", ties=2, low=True)
        lift, drops = xs.solve([6.6], [3.5], 7.7, xs.up_cost("motorway"), [w_down])
        self.assertAlmostEqual(lift, 4.6)
        self.assertAlmostEqual(drops[0], 0.0)

    def test_service_road_under_motorway_dips(self):
        lift, drops = xs.solve([6.6], [3.5], 7.7, xs.up_cost("motorway"), [xs.down_cost("service")])
        self.assertEqual(lift, 0.0)
        self.assertAlmostEqual(drops[0], 4.6)


class TrustedTest(unittest.TestCase):
    @staticmethod
    def fill(p):
        """3.5 m street, lidar's fill to 6.3 m within 35 m of the deck (at x = 0)."""
        p = np.asarray(p, np.float64).reshape(-1, 2)
        return 3.5 + 2.8 * np.clip(1.0 - np.abs(p[:, 0]) / 35.0, 0.0, 1.0)

    def test_under_skips_the_fill(self):
        t = xs.trusted_under(np.zeros(2), np.array([1.0, 0.0]), 9.0, self.fill)
        self.assertAlmostEqual(t, 3.5, places=1)

    def test_over_reads_past_the_abutment(self):
        # an embankment at 6.6 m past the end; the end itself on its slope at 5.4 m
        z = lambda p: np.where(np.asarray(p).reshape(-1, 2)[:, 0] > 4.0, 6.6, 5.4)
        self.assertAlmostEqual(xs.trusted_over(np.zeros(2), np.array([1.0, 0.0]), z), 6.6)


class TiesTest(unittest.TestCase):
    def test_continuation_is_no_junction(self):
        own = np.array([[-50.0, 0.0], [0.0, 0.0]])
        nxt = np.array([[0.0, 0.0], [50.0, 0.0]])
        self.assertEqual(xs.ties(np.array([-10.0, 0.0]), [own, nxt], own), 0)

    def test_side_roads(self):
        own = np.array([[-80.0, 0.0], [80.0, 0.0]])
        side = np.array([[30.0, 0.0], [30.0, 60.0]])            # a T on its middle
        cross = [np.array([[-40.0, -50.0], [-40.0, 0.0]]), np.array([[-40.0, 0.0], [-40.0, 50.0]])]
        self.assertEqual(xs.ties(np.zeros(2), [own, side] + cross, own), 2)

    def test_far_junctions_dont_count(self):
        own = np.array([[-300.0, 0.0], [300.0, 0.0]])
        side = np.array([[250.0, 0.0], [250.0, 60.0]])
        self.assertEqual(xs.ties(np.zeros(2), [own, side], own), 0)


if __name__ == "__main__":
    unittest.main()
