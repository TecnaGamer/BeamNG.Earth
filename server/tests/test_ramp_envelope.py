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


class DippedSlipRoadTest(unittest.TestCase):
    """GitHub #1329, Sao Paulo's 23 de Maio: a trunk and the slip road leaving it at a gore, both dipped under a deck
    after the first envelope. The second envelope's floor (the plans before any ramp, taken before the dips) stood the
    slip road back at its undipped 7 m, a 7 m step at the gore; its floor no higher than the plans now keeps the dip."""

    def setUp(self):
        self.trunk = types.SimpleNamespace(cls="trunk", link=False, subclass=None)
        self.slip = types.SimpleNamespace(cls="trunk", link=True, subclass="link")

    def run_envelope(self, floor_now):
        xs = np.arange(0.0, 100.0 + 1e-9, 10.0)
        trunk = plan(np.c_[xs, np.zeros(len(xs))], np.zeros(len(xs)), 8.0)          # dipped: 0 m (was 7)
        slip = plan(np.c_[xs[:6] + 100.0, np.linspace(0.0, 15.0, 6)], np.zeros(6), 6.0)   # dipped too, from the gore
        # (and a street raised 3 m over the trunk's other end: a broken node at a trunk, so the envelope's ceiling and
        # floor are laid)
        street = plan([(0.0, 0.0), (0.0, -30.0), (0.0, -60.0)], [3.0, 3.0, 3.0])
        before = [np.full(len(xs), 7.0), np.full(6, 7.0), np.full(3, 3.0)]   # (the floor: 7 m up before the dips)
        plans = [("trunk", trunk), ("slip", slip), ("street", street)]
        floor = grading._floor_now(before, plans) if floor_now else before
        grading.ramp_envelope(plans, [self.trunk, self.slip, STREET], [1, 2], (), floor)
        return np.asarray(plans[1][1].z, np.float64)

    def test_the_slip_road_keeps_its_dip(self):
        z = self.run_envelope(True)
        self.assertLess(float(z[0]), 0.05, "at the trunk's height at the gore")

    def test_the_old_floor_stood_it_back_up(self):
        self.assertGreater(float(self.run_envelope(False)[0]), 6.0)

    def test_floor_now_is_the_lower_of_the_two(self):
        plans = [("a", plan([(0.0, 0.0), (10.0, 0.0)], [1.0, 5.0])), ("b", plan([(0.0, 0.0), (10.0, 0.0)], [2.0, 2.0]))]
        got = grading._floor_now([np.array([3.0, 3.0]), np.array([1.0, 2.0, 3.0])], plans)
        np.testing.assert_array_equal(got[0], [1.0, 3.0])
        np.testing.assert_array_equal(got[1], [1.0, 2.0, 3.0], "another length: as it was")


if __name__ == "__main__":
    unittest.main()
