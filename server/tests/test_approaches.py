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
import roadmesh  # noqa: E402
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
        # (lidar with the real embankments: the ends already 8 m up)
        line = np.c_[np.linspace(-35, 35, 36), np.zeros(36)]
        ground = [np.array([[-12.0, -500], [-12, 500]])]
        z = lambda xy: np.where(np.abs(xy[:, 0]) > 30, FLAT + 8.0, FLAT)     # noqa: E731
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
        self.assertLessEqual(float(add.max()), 2.0 + 1e-9, "14 m apart: 7 + 4 + 1 m of gap leave 2 m: a 2 m fill (1:1)")
        xy2 = xy + np.array([0.0, 30.0])
        add2 = grading.approach_add(xy2, False, 7.8, 4.0, mot)
        cap = (44.0 - 4.0 - 7.0 - grading.APPROACH_GAP_M) / grading.APPROACH_CAP_RUN
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
        # without ramps only the hard floor (KyzoFox 1.5.5) holds the deck up: 5 m under the girders, short of the aim
        # (the lanes 23 m from the abutments: the floor's climb, DECK_HARD_MAX_GRADE, reaches this far)
        self.assertGreaterEqual(before.min(), min(roads.DECK_HARD_CLEAR, roads.DECK_HARD_MAX_GRADE * 23.0) - 0.1,
                                "the hard floor alone")
        self.assertLess(before.max(), roads.DECK_ROAD_CLEAR - 0.5, "the ramps are what reach the aim")
        after, grade = self.deck_over_lanes(True)
        self.assertGreaterEqual(after.min(), roads.DECK_ROAD_CLEAR - 0.05, after)
        under = after.min() - roadmesh.DECK_DEPTH - roadmesh.GIRDER_D
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


class KyzoFox155Test(unittest.TestCase):
    """KyzoFox 1.5.5: the overpasses over I-20 in Louisiana and Mississippi trucks still hit (2026-10-21)."""

    def test_twin_bridge_approach_does_not_cap(self):
        # a divided road on twin bridges 12 m apart (Thomas Road, West Monroe, LA): each carriageway's ramp was capped
        # by the other carriageway, "a road beside it", to 1.8 m of 5.7
        a, b = (400 - 35) / 800, (400 + 35) / 800
        f = features([[(-400, -6), (400, -6)], [(-400, 6), (400, 6)], [(-12, -500), (-12, 500)],
                      [(12, 500), (12, -500)]], ["secondary", "secondary", "motorway", "motorway"],
                     flags=[(0, "is_bridge", a, b), (1, "is_bridge", a, b)], ids=["wb", "eb", "i20n", "i20s"])
        g = grading.Grader(FRAME, FakeMD(f), flat_elev)
        la, lo, _ = FRAME.enu_to_geodetic(np.array([-36.0, 36.0]), np.array([-6.0, 6.0]), 0.0)
        z = g.apply(la, lo, flat_elev(la, lo))
        self.assertTrue((z - FLAT > roads.DECK_ROAD_CLEAR - 1.0).all(), z)

    def test_ramp_curving_over_the_road_under_the_bridge_leaves_it(self):
        # a ramp leaving the bridge that curves back over the frontage road passing under the bridge (West Monroe,
        # LA): its fill was built across that road, a ribbon of ground over its lanes
        xy = np.r_[np.c_[np.arange(-35.0, -80.1, -4.0), np.zeros(12)],
                   np.c_[np.full(20, -80.0), np.arange(4.0, 80.1, 4.0)],
                   np.c_[np.arange(-76.0, 0.1, 4.0), np.full(20, 80.0)]]
        frontage = (np.array([[-20.0, -300.0], [-20.0, 300.0]]), 4.0, grading.APPROACH_UNDER_FREE_M,
                    np.full(2, FLAT))       # (under the bridge, 15 m from its abutment)
        add = grading.approach_add(xy, False, 7.8, 4.0, [frontage], np.full(len(xy), FLAT))
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
        over = np.abs(xy[:, 0] + 20.0) < 6.0
        self.assertLess(float(add[over & (s > 100)].max()), 0.05, "nothing raised over the frontage road")
        self.assertGreater(float(add[0]), 7.0, "the abutment still raised (the abutment stands between)")

    def test_cap_over_the_other_roads_own_height(self):
        # roads meeting the ramp as high as the abutment (Nutland Road, Monroe, LA): no cap; lower ones still cap
        xy = np.c_[np.arange(0.0, 200.1, 4.0), np.zeros(51)]
        side = np.array([[0.0, 8.0], [200.0, 8.0]])
        z = np.full(len(xy), FLAT)
        high = grading.approach_add(xy, False, 4.0, 4.0, [(side, 3.0, 0.0, np.full(2, FLAT + 4.0))], z)
        low = grading.approach_add(xy, False, 4.0, 4.0, [(side, 3.0, 0.0, np.full(2, FLAT))], z)
        self.assertGreater(float(high[0]), 3.9)
        self.assertTrue(low is None or float(low.max()) < 0.5)

    def test_lift_measured_on_the_planned_road(self):
        # the ground's dip at a bridge end (the bridge cut out of the lidar) pulled the planned road 4 m under the
        # ground there; measured on the ground, the ramp fell that short
        line = np.c_[np.linspace(-35, 35, 36), np.zeros(36)]
        ground = [np.array([[-12.0, -500], [-12, 500]])]
        need = grading.bridge_needs(line, [line], ground, lambda xy: np.full(len(xy), FLAT))
        self.assertEqual(len(need), 2)
        top = FLAT + roads.DECK_ROAD_CLEAR + grading.APPROACH_MARGIN_M
        for _e, h in need:          # (falling DECK_GRADE per m past APPROACH_FLAT_M from the crossing)
            self.assertLessEqual(h, top + 1e-9)
            self.assertGreaterEqual(h, top - roads.DECK_GRADE * 30.0)

    def test_steep_deck_only_over_a_major_road(self):
        ln = np.c_[np.arange(0.0, 81.0, 4.0), np.zeros(21)]
        g = lambda xy: np.where(xy[:, 0] < 1, 24.0, np.where(xy[:, 0] > 79, 27.0, 21.5))   # noqa: E731
        cr = [(0, 32.0, 21.5)]
        (z0,) = roads.deck_profile([ln], g, cr)
        (z1,) = roads.deck_profile([ln], g, cr, steep=[0])
        at = lambda z: float(np.interp(32.0, ln[:, 0], z))      # noqa: E731
        # a street: the hard floor and no higher; over a motorway at least that, up to the steep grade's reach
        self.assertAlmostEqual(at(z0), 21.5 + roads.DECK_HARD_CLEAR, delta=0.15)
        self.assertGreaterEqual(at(z1), at(z0) - 0.01)
        self.assertGreaterEqual(at(z1), min(24.0 + roads.DECK_STEEP_GRADE * 32, 21.5 + roads.DECK_ROAD_CLEAR) - 0.15)
        # with the abutments lower, the steep grade alone falls short and the hard floor holds a street's deck too
        g2 = lambda xy: np.where(xy[:, 0] < 1, 21.5, np.where(xy[:, 0] > 79, 21.5, 21.5))   # noqa: E731
        (z2,) = roads.deck_profile([ln], g2, cr)
        self.assertGreaterEqual(at(z2), 21.5 + roads.DECK_HARD_CLEAR - 0.01)

    def test_no_deck_under_the_hard_floor(self):
        # whatever holds the abutments down (here: none raised, the ground flat), every road under a deck has 5 m
        # (16.4 ft) under its girders (the user's crash into an overpass, 2026-10-21)
        for span, at_ in ((24.0, 12.0), (60.0, 30.0), (60.0, 14.0), (200.0, 40.0)):
            ln = np.c_[np.linspace(0.0, span, int(span // 4) + 1), np.zeros(int(span // 4) + 1)]
            for fast, gr in ((False, roads.DECK_HARD_MAX_GRADE), (True, roads.DECK_HARD_FAST_GRADE)):
                (z,) = roads.deck_profile([ln], lambda xy: np.full(len(xy), FLAT), [(0, at_, FLAT)], fast=[fast])
                top = float(np.interp(at_, ln[:, 0], z)) - FLAT
                # as high as the floor, or as its climb reaches from the nearer abutment (no wall; a fast road's deck
                # gentler still: past that the road under is lowered, Grader._lower_underpasses)
                want = min(roads.DECK_HARD_CLEAR, gr * min(at_, span - at_))
                self.assertGreaterEqual(top, want - gr * 2.0 - 0.05, (span, at_, fast, top))     # (4 m vertices)
                self.assertLessEqual(np.abs(np.diff(z) / np.diff(ln[:, 0])).max(), max(gr, roads.DECK_MAX_GRADE) + 0.01)

    def test_road_under_a_short_motorway_bridge_dips(self):
        # I-20 crossing a local road on 18 m bridges whose ends sit on the ground (Ruston, LA): the deck lay 1.3 m over
        # the road. The road is lowered under it instead, the interstate's ends kept, a side road left alone
        a, b = (300 - 9) / 600, (300 + 9) / 600
        f = features([[(-300, 0), (300, 0)], [(0, -300), (0, 0)], [(0, 0), (0, 300)], [(0, 120), (300, 120)]],
                     ["motorway", "tertiary", "tertiary", "residential"], flags=[(0, "is_bridge", a, b)],
                     ids=["i20", "south", "north", "side"])
        g = grading.Grader(FRAME, FakeMD(f), flat_elev)
        e = np.array([0.0, 0.0, 0.0, -14.0, 14.0, 0.0, 80.0])
        n = np.array([0.0, -10.0, 10.0, 0.0, 0.0, 250.0, 120.0])
        la, lo, _ = FRAME.enu_to_geodetic(e, n, 0.0)
        z = g.apply(la, lo, flat_elev(la, lo))
        self.assertTrue((z[:3] < FLAT - 4.5).all(), z)              # under the bridge, both pieces
        self.assertTrue((np.abs(z[3:5] - FLAT) < 0.3).all(), z)     # the interstate at its bridge's ends
        self.assertTrue((np.abs(z[5:] - FLAT) < 0.3).all(), z)      # far along, and the side road


if __name__ == "__main__":
    unittest.main()
