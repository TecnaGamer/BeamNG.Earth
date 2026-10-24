"""KyzoFox 1.5.4: overpass approach ramps (grading.bridge_lifts / approach_add / Grader._lift_approaches): a
short bridge over an interstate on flat ground gets its approach roads graded up so its deck clears the lanes (trucks
hit the I-20 overpass near Temple, GA: ~2.5 m over the lanes, 2026-10-19)."""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import roads  # noqa: E402
from test_roads import features, FRAME  # noqa: E402
from test_grading import FakeMD  # noqa: E402

FLAT = 10.0


def flat_elev(la, lo):
    return np.full(np.shape(la), FLAT)


def overpass():
    """A secondary road east-west over x = -400..400, a bridge over x = -35..35, crossing a divided motorway (two
    carriageways north-south at x = -12 and x = +12)."""
    a, b = (400 - 35) / 800, (400 + 35) / 800
    return features([[(-400, 0), (400, 0)], [(-12, -500), (-12, 500)], [(12, 500), (12, -500)]],
                    ["secondary", "motorway", "motorway"], flags=[(0, "is_bridge", a, b)],
                    ids=["cross", "i20n", "i20s"])


class BridgeLiftsTest(unittest.TestCase):
    def test_both_abutments_raised_to_clearance(self):
        line = np.c_[np.linspace(-35, 35, 36), np.zeros(36)]
        ground = [np.array([[-12.0, -500], [-12, 500]]), np.array([[12.0, 500], [12, -500]])]
        out = grading.bridge_lifts(line, [line], ground, lambda xy: np.full(len(xy), FLAT))
        self.assertEqual(len(out), 2)
        for _xy, lift in out:
            self.assertGreaterEqual(lift, roads.DECK_ROAD_CLEAR)
            self.assertLessEqual(lift, roads.DECK_ROAD_CLEAR + grading.APPROACH_MARGIN_M + 1e-9)

    def test_nothing_under_nothing_raised(self):
        line = np.c_[np.linspace(-35, 35, 36), np.zeros(36)]
        self.assertEqual(grading.bridge_lifts(line, [line], [], lambda xy: np.full(len(xy), FLAT)), [])

    def test_ground_already_high_enough(self):
        # Lidar already contains embankments above the current structural clearance.
        line = np.c_[np.linspace(-35, 35, 36), np.zeros(36)]
        ground = [np.array([[-12.0, -500], [-12, 500]])]
        high = roads.DECK_ROAD_CLEAR + grading.APPROACH_MARGIN_M + 0.2
        z = lambda xy: np.where(np.abs(xy[:, 0]) > 30, FLAT + high, FLAT)     # noqa: E731
        self.assertEqual(grading.bridge_lifts(line, [line], ground, z), [])

    def test_junction_at_grade_and_chained_ends_left(self):
        line = np.c_[np.linspace(0, 70, 36), np.zeros(36)]
        at_end = [np.array([[3.0, -50], [3, 50]])]           # a cross street 3 m from the end: at grade
        self.assertEqual(grading.bridge_lifts(line, [line], at_end, lambda xy: np.full(len(xy), FLAT)), [])
        nxt = np.c_[np.linspace(70, 200, 20), np.zeros(20)]     # the deck goes on at x = 70: no abutment there
        under = [np.array([[60.0, -50], [60, 50]])]
        out = grading.bridge_lifts(line, [line, nxt], under, lambda xy: np.full(len(xy), FLAT))
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(float(out[0][0][0]), 0.0)

    def test_long_viaduct_not_raised(self):
        line = np.c_[np.linspace(-400, 400, 201), np.zeros(201)]
        ground = [np.array([[0.0, -50], [0, 50]])]               # a street under the middle of an 800 m viaduct
        self.assertEqual(grading.bridge_lifts(line, [line], ground, lambda xy: np.full(len(xy), FLAT)), [])


class ApproachAddTest(unittest.TestCase):
    def test_ramp_shape_along_the_road(self):
        xy = np.c_[np.arange(-35.0, -400.1, -4.0), np.zeros(92)]
        add = grading.approach_add(xy, False, 7.8, 5.0)
        self.assertIsNotNone(add)
        self.assertAlmostEqual(float(add[0]), 7.8, places=6)
        self.assertLess(float(add[-1]), 1e-6, "back on the ground at the ramp's foot")
        self.assertLessEqual(np.abs(np.diff(add) / 4.0).max(), grading.APPROACH_GRADE * 1.01)
        rev = grading.approach_add(xy[::-1], True, 7.8, 5.0)
        np.testing.assert_allclose(rev[::-1], add)

    def test_follows_the_road_not_the_crow(self):
        # a road that turns back past the abutment: 10 m away in a straight line, 300 m along the road
        a = np.c_[np.zeros(38), np.arange(0.0, 150.1, 4.0)]
        b = np.c_[np.full(38, 10.0), np.arange(150.0, -0.1, -4.0)]
        xy = np.r_[a, b]
        add = grading.approach_add(xy, False, 7.8, 4.0)
        self.assertLess(float(add[-1]), 1e-6, "its far end is not raised though it passes the abutment")

    def test_fill_kept_off_the_road_beside(self):
        # a ramp running 14 m beside a motorway: its fill slope may not reach it
        xy = np.c_[np.arange(0.0, 300.1, 4.0), np.full(76, 14.0)]
        mot = [(np.array([[-100.0, 0.0], [400.0, 0.0]]), 7.0)]
        add = grading.approach_add(xy, False, 7.8, 4.0, mot)
        self.assertLessEqual(float(add.max()), 1.0 + 1e-9, "14 m apart: 7 + 4 + 1 m of gap leave 2 m: a 1 m fill")
        xy2 = xy + np.array([0.0, 30.0])
        add2 = grading.approach_add(xy2, False, 7.8, 4.0, mot)
        cap = (44.0 - 4.0 - 7.0 - grading.APPROACH_GAP_M) / grading.SLOPE_RUN
        self.assertAlmostEqual(float(add2.max()), min(7.8, cap), places=6)

    def test_side_road_lowers_the_ramp_gently(self):
        # a side road joining 40 m from the abutment caps the ramp to nothing there: it falls to it at
        # APPROACH_CAP_GRADE, not in one step (a 3 m drop in 8 m north of the I-20 bridge near Temple, GA)
        xy = np.c_[np.arange(0.0, 200.1, 4.0), np.zeros(51)]
        side = [(np.array([[40.0, 5.0], [40.0, 200.0]]), 3.0)]
        add = grading.approach_add(xy, False, 3.7, 4.0, side)
        self.assertLessEqual(np.abs(np.diff(add) / 4.0).max(), grading.APPROACH_CAP_GRADE + 1e-9)
        self.assertLess(float(add[10]), 0.5, "low where the side road joins")
        self.assertGreater(float(add[0]), 3.0, "the abutment still raised")


class OverpassIntegrationTest(unittest.TestCase):
    def deck_over_lanes(self, on):
        old = grading.APPROACH_ON
        grading.APPROACH_ON = on
        try:
            f = overpass()
            g = grading.Grader(FRAME, FakeMD(f), flat_elev)

            def elev(la, lo):
                return g.apply(la, lo, flat_elev(la, lo), grading.ROAD_LIFT)
            bp = roads.BridgeProfiles(FRAME, lambda key: f, elev)
            parts = bp.parts([f], (-100, -100, 100, 100))
            self.assertEqual(len(parts), 1)
            _fid, _spec, xy, z, _lon = parts[0]
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(np.asarray(xy)[:, :2], axis=0).T))]
            at = [float(np.interp(x, np.asarray(xy)[:, 0], s)) for x in (-12.0, 12.0)]
            lanes = elev(*FRAME.enu_to_geodetic(np.array([-12.0, 12.0]), np.array([0.0, 0.0]), 0.0)[:2])
            # and the approach road 100 m out: on its ramp, not a wall
            ramp = elev(*FRAME.enu_to_geodetic(np.array([-135.0, -137.0]), np.array([0.0, 0.0]), 0.0)[:2])
            self.lanes = lanes - 10.0
            return np.interp(at, s, z) - lanes, abs(ramp[1] - ramp[0]) / 2.0
        finally:
            grading.APPROACH_ON = old

    def test_deck_clears_the_lanes(self):
        before, _ = self.deck_over_lanes(False)
        self.assertLess(before.max(), 4.0, "the bug: the deck low over the motorway")
        after, grade = self.deck_over_lanes(True)
        self.assertGreaterEqual(after.min(), roads.DECK_ROAD_CLEAR - 0.05, after)
        under = after.min() - 1.2 - 1.0     # (roadmesh DECK_DEPTH + GIRDER_D)
        self.assertGreater(under, 4.9, "16 ft headroom under the girders")
        self.assertLessEqual(grade, grading.APPROACH_GRADE * 1.05)
        self.assertLess(np.abs(self.lanes).max(), 0.2, "the motorway is not raised")

    def test_motorway_over_a_road_not_raised(self):
        # the user's crash (I-20 in Greene County, AL): the motorway was the bridge, over a local road, and both its
        # carriageways went up 7.8 m on 300 m fills, differently in different cells
        a, b = (400 - 35) / 800, (400 + 35) / 800
        f = features([[(-400, 0), (400, 0)], [(-12, -500), (-12, 500)]], ["motorway", "residential"],
                     flags=[(0, "is_bridge", a, b)], ids=["i20", "local"])
        g = grading.Grader(FRAME, FakeMD(f), flat_elev)
        la, lo, _ = FRAME.enu_to_geodetic(np.array([-100.0, -60.0, 100.0]), np.zeros(3), 0.0)
        z = g.apply(la, lo, flat_elev(la, lo))
        self.assertLess(np.abs(z - FLAT).max(), 0.05, "the motorway's approaches stay on the ground")


class TwinKeepTest(unittest.TestCase):
    def test_twins_kept_ground_only_where_both_run(self):
        # GitHub #84: one twin's portal 3 m deeper than the other's; the other's kept ground reached over its approach
        from roads import Spec
        sp = Spec(11.0, "paved", False, "trunk", True, 0.0, 0, None, None, False)
        xs = np.arange(0.0, 100.1, 1.0)            # (sections come resampled every few metres)
        a = ("a", sp, np.c_[xs, np.zeros_like(xs)], np.full(len(xs), 5.0), 2)
        b = ("b", sp, np.c_[103.0 - xs, np.full(len(xs), 9.5)], np.full(len(xs), 5.0), 2)
        out = grading._twin_trimmed([a, b])
        xa = np.asarray(out[0][2])
        self.assertGreaterEqual(float(xa[:, 0].min()), 3.0 - 1e-6, "a's keep starts at b's (deeper) portal")
        self.assertLessEqual(float(xa[:, 0].max()), 100.0 + 1e-6)
        xb = np.asarray(out[1][2])
        self.assertLessEqual(float(xb[:, 0].max()), 100.0 + 1e-6, "b's ends where a's does")
        self.assertEqual(grading._twin_trimmed([a]), [a])


class UnderwaterTunnelTest(unittest.TestCase):
    def test_no_plan_along_a_tunnel_under_water(self):
        # storm-ace's GitHub #85: the Maastunnel's sections were planned (at -0.4..-1.2 m over the river, whose DEM reads
        # about its surface) and the plan raised the carved bed (-6 m) into a dam across the river; roads.TunnelProfiles
        # leaves such tunnels out, now so does the grading (Rotterdam probe: no plan, the ground as it is)
        f = features([[(-300, 0), (300, 0)]], ["trunk"], flags=[(0, "is_tunnel", 0.0, 1.0)], ids=["maas"])

        def dem(la, lo):                    # the DEM: banks at 3 m, the river's surface at 0.5 m
            e, _, _ = FRAME.geodetic_to_enu(la, lo, 0.0)
            return np.where(np.abs(np.asarray(e)) < 200, 0.5, 3.0)
        la, lo, _ = FRAME.enu_to_geodetic(np.linspace(-280.0, 280.0, 57), np.zeros(57), 0.0)
        raw = dem(la, lo)
        g = grading.Grader(FRAME, FakeMD(f), dem)
        dry = g.apply(la, lo, raw)
        self.assertGreater(float(np.abs(dry - raw).max()), 0.3, "on land the tunnel's sections are planned")
        g2 = grading.Grader(FRAME, FakeMD(f), dem)
        g2.wet = lambda la, lo: np.where(np.asarray(dem(la, lo)) < 1.0, 6.0, 0.0)
        self.assertTrue(np.allclose(g2.apply(la, lo, raw), raw), "under water: none")


if __name__ == "__main__":
    unittest.main()
