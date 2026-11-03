"""Side roads land flat across the road they join (grading.snap_junctions, then junction_pads), equal-ranked T junctions
land, and roads side by side on coarse elevation are coupled (grading.couple_neighbours, NEIGHBOUR_*)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import roads  # noqa: E402

PRIMARY = roads.Spec(10.0, "paved", True, "primary", False)
RESIDENTIAL = roads.Spec(6.0, "paved", True, "residential", False)
SERVICE = roads.Spec(5.0, "paved", True, "service", False)


def plan(xy, z, half, road_half, rid):
    p = grading.Plan(np.asarray(xy, float), np.asarray(z, float), half, road_half=road_half)
    p.rid = rid
    return p


def line(x0, y0, x1, y1, step=4.0):
    n = int(round(np.hypot(x1 - x0, y1 - y0) / step)) + 1
    return np.c_[np.linspace(x0, x1, n), np.linspace(y0, y1, n)]


class LandingTest(unittest.TestCase):
    """The landing, as the grader makes it since 1.7.3: earthfast.snap_junctions (as it was), then junction_pads."""

    @staticmethod
    def junctions(plans, specs):
        grading.snap_junctions(plans, specs)
        grading.junction_pads(plans, specs)

    def t_junction(self, side_spec=RESIDENTIAL, main_spec=PRIMARY):
        """A through road split at (0, 0) (flat at 10 m) and a side road climbing north from it at 7 %."""
        west, east = line(-200, 0, 0, 0), line(0, 0, 200, 0)
        stem = line(0, 0, 0, 120)
        a = plan(west, np.full(len(west), 10.0), 8.0, 5.0, 1)
        b = plan(east, np.full(len(east), 10.0), 8.0, 5.0, 2)
        c = plan(stem, 10.0 + 0.07 * stem[:, 1], 6.0, 3.0, 3)
        self.junctions([a, b, c], [main_spec, main_spec, side_spec])
        return a, b, c

    def test_the_side_road_lies_flat_across_the_main_road(self):
        a, b, c = self.t_junction()
        d = c.xy[:, 1]
        held = d <= a.half + grading.PAD_LANDING_M
        np.testing.assert_allclose(c.z[held], 10.0, atol=0.01, err_msg="on the main road's surface over its core")
        self.assertAlmostEqual(c.z[-1], 10.0 + 0.07 * 120, places=6, msg="its own profile past the ease")
        self.assertTrue(np.all(np.diff(c.z) >= -1e-6), "climbing from there, no dip")
        np.testing.assert_allclose(a.z, 10.0, atol=1e-6)
        np.testing.assert_allclose(b.z, 10.0, atol=1e-6)

    def test_equal_ranks_the_stem_of_a_t_lands(self):
        a, b, c = self.t_junction(RESIDENTIAL, RESIDENTIAL)
        self.assertAlmostEqual(c.z[0], 10.0, places=6)
        self.assertLess(abs(c.z[1] - 10.0), 0.03, "landed, as on a bigger road")
        np.testing.assert_allclose(a.z, 10.0, atol=0.01, err_msg="the through road keeps its profile")
        np.testing.assert_allclose(b.z, 10.0, atol=0.01)

    def test_equal_crossroads_meet_on_one_plane(self):
        """A crossroads of two equal streets on a slope: both keep their own heights at the node, the flat one its
        profile, and the climbing one is eased (it meets the plane both lie on there)."""
        parts = [line(-200, 0, 0, 0), line(0, 0, 200, 0), line(0, 0, 0, 120), line(0, -120, 0, 0)]
        ps = [plan(parts[0], np.full(len(parts[0]), 10.0), 6, 3, 1), plan(parts[1], np.full(len(parts[1]), 10.0), 6, 3, 2),
              plan(parts[2], 10.0 + 0.07 * parts[2][:, 1], 6, 3, 3), plan(parts[3], 10.0 + 0.07 * parts[3][:, 1], 6, 3, 4)]
        self.junctions(ps, [RESIDENTIAL] * 4)
        np.testing.assert_allclose(ps[0].z, 10.0, atol=0.01)
        np.testing.assert_allclose(ps[1].z, 10.0, atol=0.01)
        self.assertAlmostEqual(ps[2].z[0], 10.0, places=2)
        self.assertAlmostEqual(ps[3].z[-1], 10.0, places=2)
        self.assertAlmostEqual(ps[2].z[-1], 10.0 + 0.07 * 120, places=6, msg="its own profile further on")

    def test_a_short_connector_keeps_half_its_length_to_climb(self):
        a = plan(line(-100, 0, 100, 0), np.full(51, 10.0), 8.0, 5.0, 1)
        conn = line(0, 0, 0, 18, 2.0)
        c = plan(conn, 10.0 + 0.08 * conn[:, 1], 3.0, 2.5, 2)
        self.junctions([a, c], [PRIMARY, SERVICE])
        flat = np.flatnonzero(np.abs(c.z - 10.0) < 1e-9)
        self.assertLessEqual(c.xy[flat.max(), 1], 0.25 * 18 + 1e-6)

    def test_off_as_before(self):
        from unittest import mock
        with mock.patch.object(grading, "PAD_ON", False):
            a, b, c = self.t_junction()
        self.assertAlmostEqual(c.z[0], 10.0, places=6)
        self.assertGreater(c.z[1], 10.05, "snap_junctions alone: climbing from the main road's centre")


class NeighbourTest(unittest.TestCase):
    @staticmethod
    def trend(slope):
        return lambda xy: 100.0 + slope * np.asarray(xy)[:, 1]

    def pair(self, gap, z_front, slope=0.0, front_spec=SERVICE, abut=False):
        main = plan(line(-200, 0, 200, 0), np.full(101, 100.0), 8.0, 5.0, 1)
        front = plan(line(-200, gap, 200, gap), np.full(101, z_front), 3.5, 2.5, 2)
        if abut:
            front.abut = (True, False)
        grading.couple_neighbours([main, front], [PRIMARY, front_spec], self.trend(slope))
        return main, front

    def test_a_frontage_road_comes_to_its_primary_plus_the_real_fall(self):
        main, front = self.pair(18.0, 101.8, slope=0.03)
        np.testing.assert_allclose(main.z, 100.0, err_msg="the higher rank stays")
        mid = front.z[30:71]
        np.testing.assert_allclose(mid, 100.0 + 0.03 * 18.0, atol=0.02, err_msg="the primary + the hillside's fall")

    def test_far_roads_crossing_roads_and_real_separations_stay(self):
        _m, far = self.pair(60.0, 101.8)
        np.testing.assert_allclose(far.z, 101.8)
        _m, sep = self.pair(18.0, 100.0 + grading.NEIGHBOUR_MAX_M + 1.0)
        np.testing.assert_allclose(sep.z, 100.0 + grading.NEIGHBOUR_MAX_M + 1.0, err_msg="a real separation")
        _m, onto_bridge = self.pair(18.0, 101.8, abut=True)
        np.testing.assert_allclose(onto_bridge.z, 101.8, err_msg="a bridge approach: as it was")
        main = plan(line(-200, 0, 200, 0), np.full(101, 100.0), 8.0, 5.0, 1)
        cross = plan(line(30, -100, 30, 100), np.full(51, 102.0), 3.5, 2.5, 2)
        grading.couple_neighbours([main, cross], [PRIMARY, SERVICE], self.trend(0.0))
        np.testing.assert_allclose(cross.z, 102.0, err_msg="crossing, not alongside: the junction code's")

    def test_equal_ranks_meet_halfway(self):
        a = plan(line(-200, 0, 200, 0), np.full(101, 100.0), 6.0, 3.0, 1)
        b = plan(line(-200, 16, 200, 16), np.full(101, 101.0), 6.0, 3.0, 2)
        grading.couple_neighbours([a, b], [RESIDENTIAL, RESIDENTIAL], self.trend(0.0))
        self.assertLess(float(np.median(b.z) - np.median(a.z)), 0.75)
        self.assertGreater(float(np.median(b.z)), 100.2)

    def test_roads_meeting_at_a_node_are_left_to_the_junctions(self):
        mway = plan(line(-200, 0, 0, 0), np.full(51, 100.0), 9.0, 6.0, 1)
        link = plan(line(0, 0, 180, -60), np.full(48, 101.0), 4.0, 3.0, 2)
        grading.couple_neighbours([mway, link], [roads.Spec(12.0, "paved", False, "motorway", True),
                                                 roads.Spec(5.0, "paved", False, "motorway", True, link=True)],
                                  self.trend(0.0))
        np.testing.assert_allclose(link.z, 101.0)


if __name__ == "__main__":
    unittest.main()
