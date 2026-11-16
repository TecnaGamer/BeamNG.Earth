"""grading.ramp_envelope: the roads round a raised one come up to it at RAMP_GRADE; here, its nodes on another road's
middle (a side road joining it), the part that asks every node which roads it lies on."""
import os
import sys
import types
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402

STREET = types.SimpleNamespace(cls="residential")


def plan(xy, z, half=3.0):
    return grading.Plan(np.asarray(xy, np.float64), np.asarray(z, np.float64), half)


def along_x(y, z=0.0, x0=0.0, x1=100.0):
    xs = np.arange(x0, x1 + 1e-9, 10.0)
    return plan(np.c_[xs, np.full(len(xs), y)], np.full(len(xs), z))


class SideRoadOnAMiddleTest(unittest.TestCase):
    def setUp(self):
        # a street along x on the ground; a raised side road (3 m) ending on its middle at x = 50; a street whose box
        # the node is in but that passes 20 m from it; a street far away
        side = plan([(50.0, 0.5), (50.0, 30.0), (50.0, 60.0)], [3.0, 3.0, 3.0])
        diagonal = plan([(20.0, 30.0), (50.0, 20.0), (80.0, -30.0)], [0.0, 0.0, 0.0])
        self.plans = [("main", along_x(0.0)), ("side", side), ("diagonal", diagonal), ("far", along_x(500.0))]
        self.specs = [STREET] * 4
        grading.ramp_envelope(self.plans, self.specs, [1])
        self.z = {fid: np.asarray(p.z, np.float64) for fid, p in self.plans}

    def test_the_road_it_joins_comes_up_to_it_and_ramps_away(self):
        x = np.arange(0.0, 100.0 + 1e-9, 10.0)
        np.testing.assert_allclose(self.z["main"], np.maximum(0.0, 3.0 - grading.RAMP_GRADE * np.abs(x - 50.0)),
                                   atol=1e-9)

    def test_the_raised_road_stays(self):
        np.testing.assert_allclose(self.z["side"], [3.0, 3.0, 3.0])

    def test_a_road_only_near_it_and_a_road_far_away_stay(self):
        np.testing.assert_array_equal(self.z["diagonal"], [0.0, 0.0, 0.0])
        np.testing.assert_array_equal(self.z["far"], np.zeros(11))

    def test_a_road_of_two_points_has_no_middle_to_join(self):
        short = plan([(0.0, 0.0), (100.0, 0.0)], [0.0, 0.0])
        plans = [("short", short), ("side", plan([(50.0, 0.5), (50.0, 30.0), (50.0, 60.0)], [3.0, 3.0, 3.0]))]
        grading.ramp_envelope(plans, [STREET, STREET], [1])
        np.testing.assert_array_equal(np.asarray(plans[0][1].z), [0.0, 0.0])

    def test_no_roads_but_the_raised_one(self):
        plans = [("side", plan([(50.0, 0.5), (50.0, 30.0)], [3.0, 3.0]))]
        grading.ramp_envelope(plans, [STREET], [0])
        np.testing.assert_array_equal(np.asarray(plans[0][1].z), [3.0, 3.0])


if __name__ == "__main__":
    unittest.main()
