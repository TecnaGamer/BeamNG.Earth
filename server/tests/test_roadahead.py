"""The road ahead (roadahead.RoadNet.follow): snapped to the road the player drives along, walked ahead along the same
road through its joins, stopping at a fork; the prefetch chain along it (earthd.predict_route_chain)."""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import roadahead  # noqa: E402


def arc(x0, y0, r, a0, a1, n=40):
    a = np.linspace(a0, a1, n)
    return np.stack([x0 + r * np.cos(a), y0 + r * np.sin(a)], 1)


class FollowTest(unittest.TestCase):
    def test_straight_through_joins(self):
        """A motorway in three pieces, a side road off it at a join: the motorway is followed, not the side road."""
        net = roadahead.RoadNet([
            ([(0, 0), (1000, 0)], "motorway", "A13", False),
            ([(1000, 0), (2000, 0)], "motorway", "A13", False),
            ([(1000, 0), (1800, 300)], "motorway", "", True),        # an exit ramp, bending only 20 degrees
            ([(2000, 0), (3000, 0)], "motorway", "A13", False),
        ])
        xy = net.follow(100, 5, 80, 0, 2500)
        self.assertIsNotNone(xy)
        self.assertGreaterEqual(xy[-1][0], 2590)
        self.assertTrue(np.all(np.abs(xy[:, 1]) < 10), "stays on the motorway")

    def test_backwards_lines_and_bends(self):
        """Lines drawn against the direction of travel are walked backwards; a curving road is followed round."""
        bend = arc(1000, 1000, 1000, -math.pi / 2, 0)     # (1000, 0) round to (2000, 1000)
        net = roadahead.RoadNet([
            ([(1000, 0), (0, 0)], "primary", "B1", False),           # drawn westwards
            (bend[::-1], "primary", "B1", False),                     # drawn from the far end
            ([(2000, 1000), (2000, 3000)], "primary", "B1", False),
        ])
        xy = net.follow(200, 0, 60, 0, 4000)
        self.assertIsNotNone(xy)
        self.assertGreater(xy[-1][1], 1500, "round the bend, north")
        self.assertAlmostEqual(xy[-1][0], 2000, delta=1)

    def test_fork_stops(self):
        """Two alike continuations bending alike: a fork, the road ahead ends there."""
        net = roadahead.RoadNet([
            ([(0, 0), (1000, 0)], "primary", "", False),
            ([(1000, 0), (2000, 100)], "primary", "", False),
            ([(1000, 0), (2000, -100)], "primary", "", False),
        ])
        xy = net.follow(100, 0, 60, 0, 3000)
        self.assertAlmostEqual(xy[-1][0], 1000, delta=1)

    def test_off_road(self):
        """Far from any road, or across it: None."""
        net = roadahead.RoadNet([([(0, 0), (1000, 0)], "primary", "", False)])
        self.assertIsNone(net.follow(100, 100, 60, 0, 1000), "100 m off")
        self.assertIsNone(net.follow(100, 0, 0, 60, 1000), "across it")
        self.assertIsNone(net.follow(100, 0, 0, 0, 1000), "standing")
        self.assertIsNone(roadahead.RoadNet().follow(0, 0, 60, 0, 1000), "no roads")

    def test_chain_along_the_road(self):
        """The prefetch chain along the road round a bend, more blocks than the straight chain's."""
        import earthd
        bend = arc(1500, 1500, 1500, -math.pi / 2, 0)
        net = roadahead.RoadNet([
            ([(-500, 0), (1500, 0)], "motorway", "A1", False),
            (bend, "motorway", "A1", False),
            ([(3000, 1500), (3000, 6000)], "motorway", "A1", False),
        ])
        v = 83.0
        xy = net.follow(0, 0, v, 0, earthd.ROAD_AHEAD_MAX_M)
        rule = earthd.FOLLOW_RULES[1024.0]
        chain = earthd.predict_route_chain(xy, 0.0, 0.0, v, 0.0, 0.0, 0.0, rule, earthd.ROAD_CHAIN + 1, None,
                                           earthd.ROAD_AHEAD_S, earthd.ROAD_AHEAD_MAX_M)
        self.assertGreaterEqual(len(chain), 6, chain)
        self.assertGreater(max(c[1] for c in chain), 300, "into the bend")
        self.assertEqual(len(set(chain)), len(chain))


    def test_line_in_two_cells_once(self):
        """A line in two cells' layers (one id) is one line: its copy is no fork."""
        a = ([(0, 0), (1000, 0)], "primary", "B2", False, "a")
        b = ([(1000, 0), (2000, 0)], "primary", "B2", False, "b")
        net = roadahead.RoadNet([a, b, a, b])
        self.assertEqual(len(net.lines), 2)
        self.assertAlmostEqual(net.follow(100, 0, 60, 0, 3000)[-1][0], 2000, delta=1)

    def test_road_and_its_lane_sections_once(self):
        """A road whole in one cell's layer and a lane section of it in the next's: one road, no fork at its start; its
        sections alone where they cover it."""
        before = ([(0, 0), (1000, 0)], "motorway", "", False, "a")
        whole = ([(1000, 0), (3000, 0)], "motorway", "", False, "b")
        first = ([(1000, 2), (1200, 2)], "motorway", "", False, "b~lanes000000000-100000000")
        rest = ([(1200, 2), (3000, 0)], "motorway", "", False, "b~lanes100000000-1000000000")
        net = roadahead.RoadNet([before, first, whole])
        self.assertEqual(len(net.lines), 2, "the part of it alone is left out")
        self.assertAlmostEqual(net.follow(100, 0, 85, 0, 5000)[-1][0], 3000, delta=1)
        net = roadahead.RoadNet([before, whole, first, rest])
        self.assertEqual(len(net.lines), 3, "its sections, all there, stand for it")
        self.assertAlmostEqual(net.follow(100, 0, 85, 0, 5000)[-1][0], 3000, delta=1)
        net = roadahead.RoadNet([before, first])
        self.assertEqual(len(net.lines), 2, "no road whole: its sections stay")


if __name__ == "__main__":
    unittest.main()
