"""A side road joining a wide road keeps that road's height across its carriageway and shoulder."""
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import roads  # noqa: E402

TRUNK = roads.Spec(13.2, "paved", False, "trunk", False)
TRACK = roads.Spec(3.5, "paved", False, "track", False)


def main_road(z=lambda x: 10.0 + 0 * x, step=4.0):
    x = np.arange(-200, 201, step)
    return grading.Plan(np.stack([x, 0 * x], 1), z(x), 8, road_half=6.6)           # a trunk, 13.2 m wide


def side_road(y, z=11.8, x=50.0, abut=(False, False)):
    y = np.asarray(y, np.float64)
    return grading.Plan(np.stack([0 * y + x, y], 1), z + 0 * y, 2, abut=abut, road_half=1.75)   # the field above it


class JunctionCorridorTest(unittest.TestCase):
    Y = [0.7, 4.14, 7.44, 20.0, 40.0, 80.0]          # a track's vertices: two inside the main road's corridor

    def test_the_side_road_holds_the_main_height_across_the_main_road(self):
        for reverse in (False, True):
            main, side = main_road(), side_road(self.Y)
            if reverse:
                side = grading.Plan(side.xy[::-1].copy(), side.z[::-1].copy(), 2, road_half=1.75)
            grading.snap_junctions([main, side], [TRUNK, TRACK])
            d = np.abs(side.xy[:, 1])
            inside = d <= 6.6 + 1.0                                   # carriageway and shoulder
            self.assertTrue(inside.sum() >= 2)
            np.testing.assert_allclose(side.z[inside], 10.0, atol=0.01, err_msg=f"reverse={reverse}")
            self.assertAlmostEqual(float(side.z[d.argmax()]), 11.8, places=2, msg="its own height again far out")
            np.testing.assert_allclose(main.z, 10.0)                  # the main road keeps its profile

    def test_back_to_its_own_profile_without_a_dip_or_a_steep_ramp(self):
        main, side = main_road(), side_road(np.arange(0.0, 120.0, 2.0), 15.0)        # 5 m above the main road
        grading.snap_junctions([main, side], [TRUNK, TRACK])
        dz = np.diff(side.z) / 2.0
        self.assertTrue(np.all(dz >= -1e-9), "rising from the main road to the field")
        self.assertLessEqual(float(dz.max()), grading.JUNCTION_HOLD_GRADE + 1e-9)

    def test_a_second_call_changes_nothing(self):
        main, side = main_road(), side_road(self.Y)
        grading.snap_junctions([main, side], [TRUNK, TRACK])
        first = side.z.copy()
        grading.snap_junctions([main, side], [TRUNK, TRACK])
        np.testing.assert_allclose(side.z, first, atol=1e-9)

    def test_a_link_leaving_at_a_shallow_angle_follows_the_main_road_up_a_grade(self):
        """Held at the main road's height at the nearest point of it, not at its height at the end: a link at 5 degrees
        stays beside the road for 100 m (6 m off on a 6 % grade when held flat)."""
        grade, a = 0.06, np.radians(5.0)
        main = main_road(lambda x: 10.0 + grade * x)
        s = np.arange(0.0, 200.0, 2.0)
        xy = np.stack([s * np.cos(a), -s * np.sin(a)], 1)
        link = grading.Plan(xy, 10.0 + grade * xy[:, 0], 4, road_half=3.5)
        grading.snap_junctions([main, link], [TRUNK, roads.Spec(7.0, "paved", False, "trunk_link", False)])
        np.testing.assert_allclose(link.z, 10.0 + grade * xy[:, 0], atol=0.02)

    def test_an_abutment_a_ramp_and_a_short_road_are_left_to_the_snap(self):
        """Where the snap leaves an end (a bridge abutment, a gap over JUNCTION_MAX_M: a ramp to an overpass) or the road
        is too short for a corridor, the result is the snap's own."""
        y = [0.0, 2.0, 4.0, 6.0, 8.0, 10.0]
        cases = {"abutment": dict(z=11.8, abut=(True, False), y=y), "overpass ramp (8 m up)": dict(z=18.0, y=y),
                 "short road": dict(z=11.8, y=[0.0, 3.0, 6.0, 9.0])}
        for name, kw in cases.items():
            main, side = main_road(), side_road(kw["y"], kw["z"], abut=kw.get("abut", (False, False)))
            grading.snap_junctions([main, side], [TRUNK, TRACK])
            main0, side0 = main_road(), side_road(kw["y"], kw["z"], abut=kw.get("abut", (False, False)))
            with mock.patch.object(grading, "_hold_junction_corridors", return_value=False):
                grading.snap_junctions([main0, side0], [TRUNK, TRACK])
            np.testing.assert_allclose(side.z, side0.z, atol=1e-9, err_msg=name)


class TrimmedBankTest(unittest.TestCase):
    """A side road trimmed at its mouth (merge_plan_set) has its bank and widen cut with its line: left as long as the
    untrimmed line, every later plan in the pack read shifted bank values (2026-11-11)."""

    def plans(self, reverse=False):
        main = main_road()
        y = np.array([0.0, 3.0, 5.0, 8.0, 20.0, 40.0])
        side = side_road(y, 10.0)
        side.bank = np.array([0.01, 0.02, 0.03, 0.04, 0.05, 0.06])
        grading.set_widen(side, np.array([0.5, 0.4, 0.3, 0.2, 0.1, 0.0]))
        if reverse:
            q = grading.Plan(side.xy[::-1].copy(), side.z[::-1].copy(), 2, road_half=1.75)
            q.bank = side.bank[::-1].copy()
            grading.set_widen(q, side.widen[::-1].copy())
            side = q
        far = side_road(np.arange(0.0, 30.0, 5.0) + 100.0, 12.0, x=150.0)
        far.bank = np.full(len(far.xy), 0.5)
        main.rank, side.rank, far.rank = grading.grade_rank(TRUNK), grading.grade_rank(TRACK), grading.grade_rank(TRACK)
        main.rid, side.rid, far.rid = 1, 2, 3
        return [main, side, far]

    def test_bank_and_widen_follow_the_trimmed_line(self):
        for reverse in (False, True):
            ps = self.plans(reverse)
            n0 = len(ps[1].xy)
            pack = grading.merge_plan_set(ps)
            side = ps[1]
            self.assertLess(len(side.xy), n0, "the side road's mouth trimmed")
            self.assertEqual(len(side.bank), len(side.xy))
            self.assertEqual(len(side.widen), len(side.xy))
            ys = side.xy[:, 1]
            # (the bank / widen at each vertex where the untrimmed line had it there, interpolated at the new end)
            np.testing.assert_allclose(side.bank, np.interp(ys, [0, 3, 5, 8, 20, 40], [.01, .02, .03, .04, .05, .06]),
                                       atol=1e-12, err_msg=f"reverse={reverse}")
            np.testing.assert_allclose(side.widen, np.interp(ys, [0, 3, 5, 8, 20, 40], [.5, .4, .3, .2, .1, 0]),
                                       atol=1e-12)
            pad = side.half + float(np.max(side.widen)) + grading.T_MAX
            self.assertAlmostEqual(side.bbox[1], float(ys.min()) - pad)
            # the plan after it reads its own bank in the pack
            far = pack.rid == 3
            self.assertTrue(far.any())
            np.testing.assert_allclose(pack.ba[far], 0.5)
            np.testing.assert_allclose(pack.bb[far], 0.5)


if __name__ == "__main__":
    unittest.main()

class WeldContinuationsTest(unittest.TestCase):
    """Pieces of one road meet at one height (grading.weld_continuations): no dip across a junction."""

    def test_the_lower_piece_rises_to_its_neighbour_at_the_joint(self):
        a = grading.Plan(np.array([[-20.0, 0.0], [0.0, 0.0]]), np.full(2, 100.0), 4.0, road_half=3.5)
        b = grading.Plan(np.array([[0.0, 0.0], [20.0, 0.0]]), np.full(2, 100.2), 4.0, road_half=3.5)
        ps = [("a", a), ("b", b)]
        grading.weld_continuations(ps, [roads.Spec(6.0, "paved", False, "residential"),
                                        roads.Spec(6.0, "paved", False, "residential")])
        a, b = [p for _f, p in ps]
        self.assertAlmostEqual(float(a.z[-1]), 100.2, places=6)
        self.assertAlmostEqual(float(b.z[0]), 100.2, places=6)
        self.assertAlmostEqual(float(a.z[0]), 100.0, places=6, msg="the far end keeps its profile")
        self.assertAlmostEqual(float(b.z[-1]), 100.2, places=6)

    def test_different_classes_are_not_welded(self):
        a = grading.Plan(np.array([[-20.0, 0.0], [0.0, 0.0]]), np.full(2, 100.0), 4.0, road_half=3.5)
        b = grading.Plan(np.array([[0.0, 0.0], [20.0, 0.0]]), np.full(2, 100.2), 4.0, road_half=3.5)
        ps = [("a", a), ("b", b)]
        grading.weld_continuations(ps, [roads.Spec(10.0, "paved", False, "primary"),
                                        roads.Spec(6.0, "paved", False, "residential")])
        self.assertAlmostEqual(float(ps[0][1].z[-1]), 100.0, places=6)
        self.assertAlmostEqual(float(ps[1][1].z[0]), 100.2, places=6)

    def test_a_plan_set_is_welded_in_place(self):
        a = grading.Plan(np.array([[-20.0, 0.0], [0.0, 0.0]]), np.full(2, 100.0), 4.0, road_half=3.5)
        b = grading.Plan(np.array([[0.0, 0.0], [20.0, 0.0]]), np.full(2, 100.2), 4.0, road_half=3.5)
        a.rank = b.rank = 114
        plans = [a, b]
        grading.weld_continuations(plans)
        self.assertAlmostEqual(float(plans[0].z[-1]), 100.2, places=6)
        self.assertAlmostEqual(float(plans[1].z[0]), 100.2, places=6)
