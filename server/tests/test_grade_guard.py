"""The grade guard (grading.grade_guard, earthfast.grade_guard): the last pass over a cell's finished plans. A side road
that stands a wall under a raised road, DEM spikes on a motorway, the ground's own steep roads, solved dips and
bridge ends; the class ceilings and the window rule."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import roads  # noqa: E402


def spec(cls, link=False):
    return roads.Spec(8.0, "paved", False, cls, False)


def grade(plan):
    s = np.r_[0, np.cumsum(np.hypot(*np.diff(plan.xy, axis=0).T))]
    return float((np.abs(np.diff(plan.z)) / np.diff(s)).max())


class GradeGuardTest(unittest.TestCase):
    def test_a_side_road_climbs_to_a_raised_main_road_at_its_class_ceiling(self):
        """Zhmerynka (49.04314 N 28.04144 E, 2026-11-02): the main road lifted 10 m at the node, the side road left on
        the ground beside it: a wall. The side road rises to it at its class's grade ceiling (12 %), over its own length."""
        y = np.arange(-100, 101, 2.0)
        main = grading.Plan(np.stack([0 * y, y], 1), 10.0 + 0 * y, 5, road_half=4)
        x = np.arange(0, -301, -2.0)
        side = grading.Plan(np.stack([x, 0 * x], 1), 0.0 * x, 3, road_half=3)
        bases = [np.zeros_like(main.z), side.z.copy()]
        st = grading.grade_guard([main, side], [spec("primary"), spec("residential")], bases)
        self.assertGreater(side.z[0], 9.5, "the side road meets the main road")
        self.assertLessEqual(grade(side), 0.121)
        self.assertAlmostEqual(side.z[-1], 0.0, places=6, msg="its far end stays on the ground")
        np.testing.assert_allclose(main.z, 10.0, err_msg="the raised road keeps its height")
        self.assertLess(st[5], 0.05)

    def test_a_dem_spike_on_a_motorway_is_taken_out(self):
        x = np.arange(0, 300, 2.0)
        z = 50.0 + 0 * x
        z[58:61] += 3.0
        plan = grading.Plan(np.stack([x, 0 * x], 1), z.copy(), 5, road_half=4)
        grading.grade_guard([plan], [spec("motorway")], [z.copy()])
        self.assertLess(plan.z[59], 50.6)
        self.assertAlmostEqual(plan.z[10], 50.0, places=6)

    def test_the_grounds_own_steep_street_is_kept(self):
        x = np.arange(0, 200, 2.0)
        z = 0.20 * x
        plan = grading.Plan(np.stack([x, 0 * x], 1), z.copy(), 3, road_half=3)
        grading.grade_guard([plan], [spec("residential")], [z.copy()])
        np.testing.assert_allclose(plan.z, z)

    def test_a_solved_underpass_dip_keeps_its_depth(self):
        x = np.arange(0, 400, 2.0)
        base = 20.0 + 0 * x
        z = base.copy()
        z[60:140] -= 6.0 * np.sin(np.pi * np.arange(80) / 80.0) ** 2
        plan = grading.Plan(np.stack([x, 0 * x], 1), z.copy(), 5, road_half=4)
        grading.grade_guard([plan], [spec("primary")], [base])
        self.assertLess(plan.z[100], 14.1)

    def test_a_skipped_plan_is_left_as_it_is(self):
        x = np.arange(0, 300, 2.0)
        z = 50.0 + 0 * x
        z[58:61] += 3.0
        plan = grading.Plan(np.stack([x, 0 * x], 1), z.copy(), 5, road_half=4)
        grading.grade_guard([plan], [spec("motorway")], [z.copy()], skip=[0])
        np.testing.assert_allclose(plan.z, z)

    def test_a_node_of_equal_ranks_meets_at_one_height_and_eases_smoothly(self):
        """49.05477 N 28.04772 E (2026-11-06): parts of two trunks 3.85 m apart at one node, by the crossing solver. They
        meet between them, and each changes height smoothly: under its ceiling, no crease in the grade."""
        x = np.arange(0, 600, 2.0)
        a = grading.Plan(np.stack([x, 0 * x], 1), 302.0 + 0 * x, 5, road_half=4)
        b = grading.Plan(np.stack([-x, 0 * x], 1), 306.0 + 0 * x, 5, road_half=4)
        grading.grade_guard([a, b], [spec("trunk"), spec("trunk")], [304.0 + 0 * x, 304.0 + 0 * x])
        self.assertAlmostEqual(a.z[0], b.z[0], places=2)
        self.assertAlmostEqual(a.z[0], 304.0, places=1)
        for p in (a, b):
            self.assertLessEqual(grade(p), 0.06)
            bend = np.abs(p.z[20:] - 2 * p.z[10:-10] + p.z[:-20]) / 20.0
            self.assertLessEqual(float(bend.max()), 0.042)

    def test_the_datas_own_step_at_a_node_stays(self):
        """Perm, #414: 8 m apart in the natural heights: bad data, not a step the passes made."""
        x = np.arange(0, 200, 2.0)
        a = grading.Plan(np.stack([x, 0 * x], 1), 0.0 * x, 3, road_half=3)
        b = grading.Plan(np.stack([-x, 0 * x], 1), 8.0 + 0 * x, 3, road_half=3)
        grading.grade_guard([a, b], [spec("residential"), spec("residential")], [a.z.copy(), b.z.copy()])
        np.testing.assert_allclose(b.z[0] - a.z[0], 8.0)

    def test_the_plans_key_is_apart_with_the_guard_on(self):
        import plancache
        on, off = None, None
        old = grading.GUARD_ON
        try:
            grading.GUARD_ON = True
            on = plancache.plans_format()
            grading.GUARD_ON = False
            off = plancache.plans_format()
        finally:
            grading.GUARD_ON = old
        self.assertNotEqual(on, off)
        self.assertFalse(off.endswith(".gg1"))

    def test_limits_by_class(self):
        p = grading.Plan(np.zeros((2, 2)), np.zeros(2), 3)
        self.assertEqual(grading.guard_limits(spec("motorway"), p)[0], 0.06)
        self.assertAlmostEqual(grading.guard_limits(spec("motorway_link"), p)[0], 0.08)
        self.assertEqual(grading.guard_limits(spec("track"), p)[0], grading.GUARD_GRADE_OTHER)
        p.steep = True
        self.assertAlmostEqual(grading.guard_limits(spec("primary"), p)[0], 0.13)


if __name__ == "__main__":
    unittest.main()
