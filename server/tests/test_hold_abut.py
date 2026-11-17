"""grade_points' hard hold (a road whose rank is the point's highest holds its carriageway whole) stops at the
carriageway of an approach of its rank leading onto a bridge: the Stonehouse Bridge in Plymouth ends 7 m from the
gyratory it runs into, its approach planned 1.7 m over the gyratory there, and since the gyratory's two pieces count
as one road in the blend (PR #1107) the gyratory held its own height over the deck's end: a 1.6 m step (GitHub #1374)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402


def plan(xy, z, half, road_half, rid, abut=(False, False), rank=114):
    p = grading.Plan(np.asarray(xy, np.float64), np.asarray(z, np.float64), half, abut=abut, road_half=road_half)
    p.rid, p.rank = rid, rank
    return p


def gyratory_and_approach(approach_rank=114):
    xs = np.arange(-100.0, 0.0 + 1e-9, 4.0)
    a = plan(np.c_[xs, np.zeros(len(xs))], np.full(len(xs), 5.0), 11.0, 10.0, 1)
    b = plan(np.c_[-xs[::-1], np.zeros(len(xs))], np.full(len(xs), 5.0), 11.0, 10.0, 2)
    ys = np.array([0.0, 4.0, 8.0])
    # (the approach from the gyratory's node to the deck's end 8 m off, 1.7 m up, its end on the bridge)
    c = plan(np.c_[np.zeros(3), ys], [5.0, 5.85, 6.7], 5.0, 4.3, 3, abut=(False, True), rank=approach_rank)
    return [a, b, c]


class HoldOverApproachTest(unittest.TestCase):
    def z_at(self, plans, x, y):
        return float(grading.grade_points(np.array([x]), np.array([y]), np.array([5.0]), plans)[0])

    def test_the_deck_end_is_not_cut_off_at_the_gyratory_height(self):
        z = self.z_at(gyratory_and_approach(), 0.0, 7.5)
        self.assertGreater(z, 5.5, "the approach's own height at the deck end, not the gyratory's 5 m")

    def test_the_gyratory_holds_away_from_the_approach(self):
        self.assertAlmostEqual(self.z_at(gyratory_and_approach(), -40.0, 6.0), 5.0, delta=0.05)

    def test_a_lower_ranked_approach_does_not_lift_the_hold(self):
        z = self.z_at(gyratory_and_approach(approach_rank=106), 0.0, 7.5)
        self.assertAlmostEqual(z, 5.0, delta=0.05)


if __name__ == "__main__":
    unittest.main()
