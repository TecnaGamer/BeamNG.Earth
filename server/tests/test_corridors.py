"""Roads side by side agree in height (grading.share_corridors): a fork's branches, a slip road beside the road it
joins, a road dipped under a deck and the ramp beside it (GitHub #1043, #1045)."""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grading  # noqa: E402
import roads  # noqa: E402


def branch(deg, grade, n=60, step=2.0, half=8.0, rh=7.0, z0=100.0, rank=118):
    a = math.radians(deg)
    s = np.arange(n) * step
    p = grading.Plan(np.stack([s * math.cos(a), s * math.sin(a)], 1), z0 + grade * s, half, road_half=rh)
    p.rank = rank       # (grade_rank: a paved motorway's; plan_roads sets it, -1 is a road dipped under a deck)
    return p


LINK = roads.Spec(10.0, "paved", False, "motorway", link=True)
MAIN = roads.Spec(14.0, "paved", False, "motorway")
SECONDARY = roads.Spec(7.0, "paved", False, "secondary")


class ShareCorridorsTest(unittest.TestCase):
    def test_fork_branches_meet_where_their_pavements_overlap(self):
        up, down = branch(5.0, 0.03), branch(-5.0, -0.03)
        plans = [("up", up), ("down", down)]
        self.assertEqual(grading.share_corridors(plans, [LINK, LINK]), 1)
        self.assertIs(plans[0][1], up, "the higher branch keeps its profile")
        z = plans[1][1].z
        i = 20      # 40 m on: 7 m apart, the 14 m carriageways overlap
        self.assertLess(abs(float(up.z[i] - z[i])), 0.8, "the lower branch rose to the other")
        self.assertGreater(abs(float(up.z[i] - down.z[i])), 2.0)
        self.assertAlmostEqual(float(z[0]), float(down.z[0]), delta=0.05, msg="at the fork they were together already")
        self.assertLess(float(np.abs(np.diff(z, 2)).max()), 0.05, "eased, no kink")

    def test_a_slip_road_follows_the_through_road(self):
        main, ramp = branch(0.0, 0.0, half=10.0, rh=9.0), branch(4.0, 0.04)
        plans = [("main", main), ("ramp", ramp)]
        grading.share_corridors(plans, [MAIN, LINK])
        self.assertIs(plans[0][1], main, "the motorway holds")
        self.assertLess(float(plans[1][1].z[15]), float(ramp.z[15]) - 0.5, "the ramp comes down to it")
        plans = [("sec", branch(0.0, 0.0, rh=3.5, rank=112)), ("ramp", branch(4.0, 0.04))]
        grading.share_corridors(plans, [SECONDARY, LINK])
        self.assertTrue(np.array_equal(plans[0][1].z, branch(0.0, 0.0, rh=3.5).z), "a through road holds over a link")

    def test_a_dip_is_carried_onto_the_road_beside(self):
        main = branch(0.0, 0.0, n=80, half=10.0, rh=9.0)
        s = np.arange(80) * 2.0
        main.z = main.z - 1.6 * np.exp(-0.5 * ((s - 80.0) / 20.0) ** 2)
        main.rank = grading.GRADE_RANK_NONE     # (dipped under a deck)
        beside = grading.Plan(np.stack([s, np.full(80, 15.0)], 1), np.full(80, 100.0), 8.0, road_half=7.0)
        beside.rank = 118
        plans = [("main", main), ("beside", beside)]
        grading.share_corridors(plans, [MAIN, LINK])
        self.assertIs(plans[0][1], main, "the dipped road holds")
        self.assertLess(float(plans[1][1].z[40]), 99.3, "the ramp beside dips with it")

    def test_apart_or_crossing_roads_are_left_alone(self):
        a, b = branch(0.0, 0.03), branch(0.0, -0.03)
        b.xy = b.xy + np.array([0.0, 30.0])         # 30 m apart: room for a bank
        c = branch(90.0, 0.05)                       # crossing
        plans = [("a", a), ("b", b), ("c", c)]
        self.assertEqual(grading.share_corridors(plans, [LINK, LINK, LINK]), 0)

    def test_two_levels_are_left_alone(self):
        up, down = branch(5.0, 0.0, z0=106.0), branch(-5.0, 0.0)
        plans = [("up", up), ("down", down)]
        self.assertEqual(grading.share_corridors(plans, [LINK, LINK]), 0)


if __name__ == "__main__":
    unittest.main()
