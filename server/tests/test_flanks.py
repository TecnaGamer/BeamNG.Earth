"""The ground under a raised road (grading.embankment_flanks): a road lifted far over its natural profile stands on an
embankment whose sides fall at 1:2 down to the natural ground (not a cliff at the plan's 12 m transition), and a road
crossing beside it on the ground keeps its own ground."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import grading  # noqa: E402


def road(xy, z, half=5.0, rank=7):
    p = grading.Plan(np.asarray(xy, np.float64), np.asarray(z, np.float64), half, (False, False), None, half - 1.0)
    p.rid = grading.road_id("r%d" % int(xy[0][0] * 7 + xy[0][1]))
    p.rank = rank
    return p


class FlankTest(unittest.TestCase):
    def setUp(self):
        x = np.arange(-200.0, 201.0, 4.0)
        self.x = x
        self.base = np.full(len(x), 100.0)
        self.raised = road(np.c_[x, 0 * x], 124.0 + 0 * x)        # (24 m over its natural profile)

    def ground(self, plans, px, py):
        return grading.grade_points(np.asarray(px, float), np.asarray(py, float), np.full(len(px), 100.0), plans)

    def test_a_24_m_fill_gets_its_slope_both_sides(self):
        fl = grading.embankment_flanks([("r", self.raised)], [SimpleNamespace(cls="primary")], [self.base])
        self.assertEqual(len(fl), 2)
        y = np.array([8.0, 15.0, 25.0, 35.0, 45.0, 55.0])
        for side in (1.0, -1.0):
            g = self.ground([self.raised] + [q for _f, q in fl], np.zeros(len(y)), side * y)
            want = np.maximum(124.0 - (y - self.raised.half) / grading.SLOPE_RUN, 100.0)
            np.testing.assert_allclose(g, want, atol=1.5, err_msg="1:2 from the core's edge down to the ground")
        alone = self.ground([self.raised], np.zeros(len(y)), y)
        self.assertLess(float(alone[3]), 105.0, "without the flank: a cliff at its 12 m transition")

    def test_a_low_road_beside_keeps_its_ground(self):
        fl = grading.embankment_flanks([("r", self.raised)], [SimpleNamespace(cls="primary")], [self.base])
        y = np.arange(-100.0, 101.0, 4.0)
        cross = road(np.c_[0 * y + 60.0, y], 100.0 + 0 * y, half=4.0, rank=7)     # (a street 60 m along, at grade)
        g = self.ground([self.raised, cross] + [q for _f, q in fl], np.array([60.0, 60.0]), np.array([20.0, 30.0]))
        self.assertLess(float(np.abs(g - 100.0).max()), 0.5, "the street crossing the flank stays at its level")

    def test_never_under_the_natural_ground(self):
        z = np.where(self.x < 0, 124.0, 110.0)         # (a 24 m fill, then a 10 m one: the narrower flank's vertices)
        step = road(np.c_[self.x, 0 * self.x], z)
        fl = grading.embankment_flanks([("r", step)], [SimpleNamespace(cls="primary")], [self.base])
        gx, gy = np.meshgrid(np.arange(-60.0, 61.0, 2.0), np.arange(-70.0, 71.0, 2.0))
        g = self.ground([step] + [q for _f, q in fl], gx.ravel(), gy.ravel())
        self.assertGreater(float(g.min()), 99.5, "a flank never digs under the natural ground")

    def test_stops_at_another_roads_core(self):
        beside = road(np.c_[self.x, 0 * self.x + 30.0], 100.0 + 0 * self.x, half=6.0, rank=-1)   # (a motorway beside)
        fl = grading.embankment_flanks([("r", self.raised), ("m", beside)], [SimpleNamespace(cls="primary")] * 2,
                                       [self.base, self.base])
        g = self.ground([self.raised, beside] + [q for _f, q in fl], np.array([0.0, 50.0]), np.array([30.0, 30.0]))
        self.assertLess(float(np.abs(g - 100.0).max()), 0.5, "the road beside keeps its own ground")

    def test_no_flank_for_a_small_fill(self):
        low = road(np.c_[self.x, 0 * self.x], 104.0 + 0 * self.x)
        self.assertEqual(grading.embankment_flanks([("r", low)], [SimpleNamespace(cls="primary")], [self.base]), [])


if __name__ == "__main__":
    unittest.main()
