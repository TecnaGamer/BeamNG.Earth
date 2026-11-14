"""A rail fill's foot stops at a road or another track, not a slope over them (railways._foot_limits)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import railways  # noqa: E402


class FootLimitsTest(unittest.TestCase):
    def setUp(self):
        self.xy = np.array([[0.0, 0.0], [10.0, 0.0], [20.0, 0.0]])
        self.side = np.array([[0.0, 1.0]] * 3)        # the left side of a track running east
        self.foot = np.full(3, 5.0)

    def test_a_road_beside_the_fill_stops_its_foot(self):
        road = (np.array([[0.0, 6.0], [20.0, 6.0]]), 2.0, 0.0)
        got = railways._foot_limits(self.xy, self.side, self.foot, [], [road])
        # the road's edge is at 4 m (6 - half 2), its clearance 0.5: the foot ends at 3.5
        np.testing.assert_allclose(got, np.full(3, 3.5), atol=1e-9)

    def test_another_track_beside_the_fill_stops_its_foot(self):
        other = [("a-line", np.array([[0.0, 5.0], [20.0, 5.0]]))]
        got = railways._foot_limits(self.xy, self.side, self.foot, other, [])
        # the other deck's edge (half 2.4) and 1 m clear leave 1.6; the ballast top (1.8) is the wall's face
        np.testing.assert_allclose(got, np.full(3, railways.BALLAST_TOP_M + 0.01), atol=1e-9)

    def test_a_road_on_the_other_side_does_not_clip(self):
        road = (np.array([[0.0, -6.0], [20.0, -6.0]]), 2.0, 0.0)
        got = railways._foot_limits(self.xy, self.side, self.foot, [], [road])
        np.testing.assert_allclose(got, self.foot)

    def test_a_far_road_or_track_does_not_clip(self):
        road = (np.array([[0.0, 40.0], [20.0, 40.0]]), 2.0, 0.0)
        other = [("far", np.array([[0.0, -40.0], [20.0, -40.0]]))]
        got = railways._foot_limits(self.xy, self.side, self.foot, other, [road])
        np.testing.assert_allclose(got, self.foot)


if __name__ == "__main__":
    unittest.main()
