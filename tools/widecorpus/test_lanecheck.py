"""lanecheck.py's between-lanes gauges and its axis."""
import math
import unittest

import numpy as np

import lanecheck


def line(id_, xy, level="tunnel", width=4.0):
    return dict(id=id_, level=level, width=width, lanes=1, paved=True, xy=[list(map(float, p)) for p in xy])


class PairLanes(unittest.TestCase):
    def test_two_carriageways_of_one_bore(self):
        # the Milchbucktunnel's two one-way roads, 6.9 m apart (the second drawn the other way)
        a = line("north", [[0.0, 0.0], [100.0, 0.0]])
        b = line("south", [[100.0, 6.9], [0.0, 6.9]])
        got = lanecheck.pair_lanes([a, b])
        self.assertTrue(got)
        for g in got:
            self.assertTrue(g["pair"])
            self.assertEqual(g["level"], "tunnel")
            self.assertAlmostEqual(g["width"], 7.9, places=6)
            self.assertTrue(np.allclose(np.asarray(g["xy"])[:, 1], 3.45))
        covered = sum(np.ptp(np.asarray(g["xy"])[:, 0]) for g in got)
        self.assertGreater(covered, 90.0, "the whole stretch, in pieces")

    def test_no_gauge_for_ground_roads_far_roads_crossings_or_one_road(self):
        a = line("a", [[0.0, 0.0], [100.0, 0.0]])
        self.assertEqual(lanecheck.pair_lanes([a, line("g", [[0.0, 6.9], [100.0, 6.9]], level="ground")]), [])
        self.assertEqual(lanecheck.pair_lanes([a, line("far", [[0.0, 30.0], [100.0, 30.0]])]), [])
        self.assertEqual(lanecheck.pair_lanes([a, line("x", [[50.0, -50.0], [50.0, 50.0]])]), [])
        self.assertEqual(lanecheck.pair_lanes([a, line("a", [[0.0, 6.9], [100.0, 6.9]])]), [])


class AxisTest(unittest.TestCase):
    def test_along_and_across(self):
        ax = lanecheck.Axis([[47.0, 8.0], [47.0, 8.01]])        # ~760 m east
        k = 111320.0 * math.cos(math.radians(47.0))
        s, off = ax.place(np.array([47.0 + 5 / 111320.0]), np.array([8.0 + 100 / k]))
        self.assertAlmostEqual(float(s[0]), 100.0, delta=0.5)
        self.assertAlmostEqual(float(off[0]), 5.0, delta=0.05)  # (north of an eastward axis: left, positive)


if __name__ == "__main__":
    unittest.main()
