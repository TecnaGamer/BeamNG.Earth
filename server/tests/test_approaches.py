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
        clear = roads.deck_road_clear(roads.deck_depth(roads.geom_span([line])))     # (its span's deck depth)
        for _xy, lift in out:
            self.assertGreaterEqual(lift, clear)
            self.assertLessEqual(lift, clear + grading.APPROACH_MARGIN_M + 1e-9)

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

    def test_junction_at_grade_near_the_abutment(self):
        # (a road meeting a deck 10 m from its free end where the deck cannot get roads.AT_GRADE_M over it is the
        # junction the deck profile treats at grade: no approach fill on its account. A track sharing a bridge's
        # mapped node came under an end skewed onto that junction and the fill rose 8 m over its own ground)
        line = np.c_[np.linspace(0, 70, 36), np.zeros(36)]
        near = [np.array([[10.0, -50], [10, 50]])]
        flat = lambda xy: np.full(len(xy), FLAT)        # noqa: E731
        self.assertEqual(grading.bridge_lifts(line, [line], near, flat), [])
        # (the same crossing, low enough under the deck for the deck to clear it, is still a road under it)
        low = lambda xy: np.where(np.abs(xy[:, 0] - 10.0) < 4.0, FLAT - 5.0, FLAT)     # noqa: E731
        out = grading.bridge_lifts(line, [line], near, low)
        self.assertEqual(len(out), 2)

    def test_short_part_of_a_chain_raises_its_free_end(self):
        # (GitHub #306, BR-343 at Teresina: the deck carried 5 m onto the road joined at its end; that short part no
        # road crosses under must still raise its free end for the carriageways under the rest of the chain, or the
        # whole deck sat ~4.5 m low)
        deck = np.c_[np.linspace(0, 34, 18), np.zeros(18)]
        stub = np.c_[np.linspace(-5, 0, 3), np.zeros(3)]            # joined at x = 0, free at x = -5
        under = [np.array([[8.0, -50], [8, 50]]), np.array([[14.0, 50], [14, -50]])]
        out = grading.bridge_lifts(stub, [deck, stub], under, lambda xy: np.full(len(xy), FLAT), chain=[deck])
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(float(out[0][0][0]), -5.0)
        self.assertGreaterEqual(out[0][1], roads.deck_road_clear(roads.deck_depth(roads.geom_span([stub, deck]))))
        # alone (no chain), a short part is no abutment of a span
        self.assertEqual(grading.bridge_lifts(stub, [stub], under, lambda xy: np.full(len(xy), FLAT)), [])

    def test_railway_by_the_end_still_counts(self):
        # (a track 3 m from an abutment is still crossed over: a road deck never meets a railway at grade, GitHub #324)
        line = np.c_[np.linspace(0, 70, 36), np.zeros(36)]
        track = [np.array([[3.0, -50], [3, 50]])]
        out = grading.bridge_lifts(line, [line], [], lambda xy: np.full(len(xy), FLAT), rail_lines=track)
        self.assertEqual(len(out), 2)
        near = [lift for end, lift in out if end[0] < 1][0]
        clear = roads.deck_road_clear(roads.deck_depth(roads.geom_span([line])))     # (its depth from its span)
        self.assertAlmostEqual(near, clear + grading.APPROACH_MARGIN_M, places=6)

    def test_deck_in_the_elevation_read_beside_it(self):
        # (GitHub #1077, the A-334 at Albox: the elevation keeps the overpass's deck, a band at the abutments' height
        # across the motorway's cut. Read on the crossing, the motorway stood 7 m over its cut and the abutments were
        # raised 8 m; read beside the deck, as the motorway's own plan reads it, the deck clears the cut as it is)
        line = np.c_[np.linspace(-35, 35, 36), np.zeros(36)]
        ground = [np.array([[-12.0, -500], [-12, 500]]), np.array([[12.0, 500], [12, -500]])]
        cut = FLAT - 8.0

        def z(xy):          # (a cut 30 m wide under the deck, the deck 8 m wide at the ground's height)
            out = np.where(np.abs(xy[:, 0]) < 30.0, cut, FLAT)
            return np.where((np.abs(xy[:, 1]) <= 4.0) & (np.abs(xy[:, 0]) < 30.0), FLAT, out)
        on_deck = grading.bridge_lifts(line, [line], ground, z)
        self.assertEqual(len(on_deck), 2)                   # (the old reading: the cut taken for the deck's height)
        self.assertGreater(min(lift for _e, lift in on_deck), 5.0)
        self.assertEqual(grading.bridge_lifts(line, [line], ground, z, half=4.0), [])
        # (a real road under at the ground's height is still cleared: nothing beside the deck to read lower)
        flat = lambda xy: np.full(len(xy), FLAT)        # noqa: E731
        self.assertEqual(len(grading.bridge_lifts(line, [line], ground, flat, half=4.0)), 2)

    def test_ground_under_deck_never_higher(self):
        deck = np.c_[np.linspace(-35, 35, 36), np.zeros(36)]
        road = np.array([[0.0, -100], [0, 100]])
        hump = lambda xy: np.where(np.abs(xy[:, 1]) < 30.0, FLAT - 3.0, FLAT)       # noqa: E731 (a dip under it)
        got = grading.ground_under_deck(deck, 4.0, [(np.array([0.0, 0.0]), road)], hump)
        self.assertAlmostEqual(float(got[0]), FLAT - 3.0)
        band = lambda xy: np.where(np.abs(xy[:, 1]) <= 4.0, FLAT + 6.0, FLAT)       # noqa: E731 (the deck)
        got = grading.ground_under_deck(deck, 4.0, [(np.array([0.0, 0.0]), road)], band)
        self.assertAlmostEqual(float(got[0]), FLAT)

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
        depth = roads.deck_depth(roads.geom_span([np.c_[np.linspace(-35, 35, 36), np.zeros(36)]]))
        self.assertGreaterEqual(after.min(), roads.deck_road_clear(depth) - 0.05, after)
        under = after.min() - depth     # (the deck's slab and girders, from its span)
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


class JunctionRaiseTest(unittest.TestCase):
    """GitHub #1043 / #1045: an overpass over a freeway mapped right up to a junction at each end (an 8 m stub, then a
    cross street): the junction's roads capped the stub's ramp to nothing and the deck stood 1-2 m over the interstate.
    The junction rises with the approach and the roads leaving it ramp down (grading JUNCTION_RAISE_ON)."""

    def deck_over_lanes(self, on):
        old = grading.JUNCTION_RAISE_ON
        grading.JUNCTION_RAISE_ON = on
        try:
            a, b = (43 - 35) / 86, (43 + 35) / 86
            f = features([[(-43, 0), (43, 0)], [(-400, 0), (-43, 0)], [(43, 0), (400, 0)],
                          [(-12, -500), (-12, 500)], [(12, 500), (12, -500)],
                          [(43, -300), (43, 300)], [(-43, -300), (-43, 300)]],
                         ["secondary", "secondary", "secondary", "motorway", "motorway", "tertiary", "tertiary"],
                         flags=[(0, "is_bridge", a, b)], ids=["deck", "west", "east", "fwn", "fws", "xe", "xw"])
            g = grading.Grader(FRAME, FakeMD(f), flat_elev)

            def elev(la, lo):
                return g.apply(la, lo, flat_elev(la, lo), grading.ROAD_LIFT)
            bp = roads.BridgeProfiles(FRAME, lambda key: f, elev)
            parts = bp.parts([f], (-100, -100, 100, 100))
            _fid, _spec, xy, z, _lon = parts[0]
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(np.asarray(xy)[:, :2], axis=0).T))]
            at = [float(np.interp(x, np.asarray(xy)[:, 0], s)) for x in (-12.0, 12.0)]
            lanes = elev(*FRAME.enu_to_geodetic(np.array([-12.0, 12.0]), np.array([30.0, -30.0]), 0.0)[:2])
            return np.interp(at, s, z) - lanes, lanes - FLAT
        finally:
            grading.JUNCTION_RAISE_ON = old

    def test_the_junction_rises_and_the_deck_clears_the_freeway(self):
        before, _ = self.deck_over_lanes(False)
        after, lanes = self.deck_over_lanes(True)
        depth = roads.deck_depth(roads.geom_span([np.c_[np.linspace(-35, 35, 36), np.zeros(36)]]))
        # (the real cases' slip lanes and twin carriageway capped the stub's ramp; this plain junction already
        # clears without the raise: the raise must keep it so, and never lower it)
        self.assertGreaterEqual(after.min(), roads.deck_road_clear(depth) - 0.3, (before, after))
        self.assertGreaterEqual(after.min(), before.min() - 0.05)
        self.assertLess(np.abs(lanes).max(), 0.2, "the freeway is not raised")


class JunctionRaiseFalseLiftTest(unittest.TestCase):
    """GitHub #1077 (Andalusia, the A-334 and A-7): the junction raise (#1043) went off where no lift was needed. The
    elevation model held the decks over the motorway in its cut (a roundabout's two decks 7 m over the A-334: 8.5 m
    asked, 1.5 m clears it), and a motorway's own deck asked its abutment's lift (never applied: the road under dips)
    and raised the links at the gore past it. The roundabouts, their links and the roads round them stood 5-8 m (one
    track 21 m) over the ground for 400 m, the slip roads climbing onto them from the motorway."""

    def graded(self, elev_fn, f, pts, **knobs):
        old = {k: getattr(grading, k) for k in knobs}
        for k, v in knobs.items():
            setattr(grading, k, v)
        try:
            g = grading.Grader(FRAME, FakeMD(f), elev_fn)
            la, lo, _ = FRAME.enu_to_geodetic(np.asarray(pts, float)[:, 0], np.asarray(pts, float)[:, 1], 0.0)
            base = elev_fn(la, lo)
            return np.asarray(g.apply(la, lo, base, grading.ROAD_LIFT)) - base
        finally:
            for k, v in old.items():
                setattr(grading, k, v)

    def test_a_deck_the_elevation_holds_over_a_cut_raises_no_junction(self):
        # (JunctionRaiseTest's overpass and junctions, the motorway in a 6.5 m cut; the elevation holds the deck: the
        # ground at the overpass's level across the cut under it)
        def cut_elev(la, lo):
            e, n, _ = FRAME.geodetic_to_enu(np.asarray(la, float), np.asarray(lo, float), 0.0)
            e, n = np.asarray(e), np.asarray(n)
            z = np.full(np.shape(e), FLAT)
            z = np.where(np.minimum(np.abs(e + 12), np.abs(e - 12)) < 10, FLAT - 6.5, z)
            return np.where((np.abs(e) < 24) & (np.abs(n) < 8), FLAT + 0.5, z)
        a, b = (43 - 35) / 86, (43 + 35) / 86
        f = features([[(-43, 0), (43, 0)], [(-400, 0), (-43, 0)], [(43, 0), (400, 0)],
                      [(-12, -500), (-12, 500)], [(12, 500), (12, -500)],
                      [(43, -300), (43, 300)], [(-43, -300), (-43, 300)]],
                     ["secondary", "secondary", "secondary", "motorway", "motorway", "tertiary", "tertiary"],
                     flags=[(0, "is_bridge", a, b)], ids=["deck", "west", "east", "fwn", "fws", "xe", "xw"])
        # (the junction's roads: the cross streets 20-60 m off it, the road on past it)
        pts = [(x, y) for x in (-43.0, 43.0) for y in (-60.0, -40.0, -20.0, 20.0, 40.0, 60.0)] + \
              [(x, 0.0) for x in (-100.0, -70.0, 70.0, 100.0)]
        before = self.graded(cut_elev, f, pts, JR_TRUSTED=False, LIFT_UNDER_BESIDE=False)
        after = self.graded(cut_elev, f, pts)
        self.assertGreater(before.max(), 2.0, "the case raised its junction before the fix")
        self.assertLess(after.max(), 0.5, (before.round(1), after.round(1)))
        # (either alone: the junction raise asking the road under's own ground, or the abutment's lift reading it)
        for knobs in ({"JR_TRUSTED": False}, {"LIFT_UNDER_BESIDE": False}):
            self.assertLess(self.graded(cut_elev, f, pts, **knobs).max(), 0.5, knobs)

    def test_a_freeways_own_deck_is_no_raised_junctions(self):
        # (a motorway on the ground over a street on its own deck, 20 m on to the gore where a link leaves: its
        # abutment asks a lift that is never applied, and the gore is no junction to raise)
        f = features([[(0, -500), (0, -20)], [(0, -20), (0, 20)], [(0, 20), (0, 40)], [(0, 40), (0, 500)],
                      [(0, 40), (150, 150)], [(-300, 0), (300, 0)]],
                     ["motorway"] * 5 + ["secondary"],
                     flags=[(1, "is_bridge", 0.0, 1.0), (4, "is_link", 0.0, 1.0)],
                     ids=["mws", "deck", "stub", "mwn", "link", "street"])
        got = {}
        for own in (False, True):
            old = grading.JR_OWN_CLASS
            grading.JR_OWN_CLASS = own
            try:
                g = grading.Grader(FRAME, FakeMD(f), flat_elev)
                la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0, 0.0]), np.array([20.0, 40.0]), 0.0)
                g.plans(la[:1], lo[:1])
                pl = g.store.planners_of(la[:1], lo[:1])[0][0]
                e = np.stack([np.asarray(v) for v in pl.frame.geodetic_to_enu(la, lo, 0.0)[:2]], 1)
                lift = pl._end_lift(e[0])
                got[own] = (lift is not None and float(lift[0]) >= grading.JUNCTION_MIN_LIFT, pl._jr_at(e[0]),
                            pl._junction_lift(e[1], e[1] + (150.0, 110.0)))
            finally:
                grading.JR_OWN_CLASS = old
        self.assertEqual(got[False][:2], (True, True), "the case: the deck's end asks a big lift, read as a raise's")
        self.assertIsNotNone(got[False][2], "... and the gore's link would rise from it")
        self.assertEqual(got[True][1:], (False, None), got)


class EmbankmentApproachTest(unittest.TestCase):
    """Kurna - Tuhala over Tallinna ringtee (59.3364 N 24.8239 E, GitHub #684): the road reaches the bridge on
    embankments 6 m over the ring road, but lidar ground has the bridge taken out and its map line ends down their
    slopes. The approach was filled up the slope to the road (fill_abutment), then raised again by the whole lift the
    deck needed over the ground at the bridge's end point: the deck end 6 m over the embankment."""

    @staticmethod
    def embankment(la, lo):
        x = np.abs(np.asarray(FRAME.geodetic_to_enu(la, lo, 0.0)[0], np.float64))
        return np.clip(FLAT + 6.0 * (x - 35.0) / 10.0, FLAT, FLAT + 6.0)  # 16 m out from 45 m, down to 10 at the ends

    def test_deck_end_raised_only_by_what_it_lacks(self):
        f = overpass()
        g = grading.Grader(FRAME, FakeMD(f), self.embankment)

        def elev(la, lo):
            return g.apply(la, lo, self.embankment(la, lo), grading.ROAD_LIFT)
        parts = roads.BridgeProfiles(FRAME, lambda key: f, elev).parts([f], (-100, -100, 100, 100))
        self.assertEqual(len(parts), 1)
        _fid, _spec, xy, z, _lon = parts[0]
        z = np.asarray(z, np.float64)
        depth = roads.deck_depth(roads.geom_span([np.c_[np.linspace(-35, 35, 36), np.zeros(36)]]))
        need = FLAT + roads.deck_road_clear(depth) + grading.APPROACH_MARGIN_M     # (7.7 m: 2 m over the embankment)
        self.assertAlmostEqual(z[0], need, delta=0.5, msg="up to the clearance, not the lift again over the fill")
        self.assertAlmostEqual(z[-1], need, delta=0.5)
        lanes = elev(*FRAME.enu_to_geodetic(np.array([-12.0, 12.0]), np.array([0.0, 0.0]), 0.0)[:2])
        over = np.interp([-12.0, 12.0], np.asarray(xy)[:, 0], z) - lanes
        self.assertGreaterEqual(over.min(), roads.deck_road_clear(depth) - 0.05, "still clears the motorway")


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


class EndsNearTest(unittest.TestCase):
    """_Planner._ends_near (planends.rs): the bridge ends of a point's own cells, rounded to the mm, each once in (x, y)
    order, in the distance band, the approach's own other end left out; None where a cell of its own is unreadable."""

    def planner(self, ends_of, bad=()):
        import threading
        import geodesy
        import mapcells
        g = object.__new__(grading._Planner)
        g.lock = threading.Lock()
        g.frame = geodesy.EnuFrame(40.754, -73.984)

        def cell_enu(k, ground=True):
            if (k.x, k.y) in bad:
                raise OSError("unreadable")
            return {"ends": ends_of.get((k.x, k.y), np.zeros((0, 2)))}
        g._cell_enu = cell_enu
        self.home = mapcells.cell_of(40.754, -73.984)
        return g

    def test_band_round_unique_order(self):
        self.planner({})
        k = (self.home.x, self.home.y)
        ends = np.array([[30.0004, 5.0], [30.0, 5.0], [-20.0, 1.0], [1.0, 1.0], [150.0, 0.0], [-20.0, -3.0]])
        g = self.planner({k: ends})
        got = g._ends_near([np.array([0.0, 0.0])], 100.0, 2.5, 100.0)[0]
        # (30.0004 rounds to 30.0: one; 1, 1 too near; 150 too far; sorted by x then y)
        self.assertEqual(got.tolist(), [[-20.0, -3.0], [-20.0, 1.0], [30.0, 5.0]])
        got = g._ends_near([np.array([0.0, 0.0])], 100.0, 2.5, 100.0, [np.array([-21.0, 1.5])])[0]
        self.assertEqual(got.tolist(), [[-20.0, -3.0], [30.0, 5.0]], "the other end's abutment left out")

    def test_unreadable_cell(self):
        self.planner({})
        g = self.planner({}, bad={(self.home.x, self.home.y)})
        self.assertIsNone(g._ends_near([np.array([0.0, 0.0])], 100.0, 2.5, 100.0)[0])


if __name__ == "__main__":
    unittest.main()
