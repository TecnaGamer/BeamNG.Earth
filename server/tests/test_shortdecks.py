"""Short bridges are not humped (2026-10-27, I-275 by the Sunshine Skyway): a short overpass follows the straight line
between its abutments when that clears what is under it; a deck's slab and girders scale with its span; a short
water span too low for its clearance gets its approach fill raised instead of a hump; the approach meets the deck."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import roadmesh  # noqa: E402
import roads  # noqa: E402
from test_approaches import overpass, flat_elev, FLAT  # noqa: E402
from test_roads import FRAME  # noqa: E402
from test_grading import FakeMD  # noqa: E402


class ChordTest(unittest.TestCase):
    def overpass_profile(self, z0=7.57, zL=7.39, road=-0.3):
        # the I-275 overpass's numbers: 24 m, its abutments on 6 m shelves of approach ground, the road under dipped
        s = np.arange(0.0, 24.01, 1.0)
        g = np.where(s < 6, z0, np.where(s > 19, zL, road))
        req = g + roads.DECK_CLEAR
        req[:] = np.maximum(req, road + roads.deck_road_clear(roads.deck_depth(24.0)) + 0.06)
        return s, g, roads.curve_profile(s, g, req, z0, zL, 0.004, -0.001)

    def test_short_overpass_not_crowned(self):
        s, g, z = self.overpass_profile()
        chord = 7.57 + (7.39 - 7.57) * s / s[-1]
        self.assertLess(float(np.max(np.abs(z - chord))), 0.1, "no crown over the chord")
        self.assertTrue((z >= g - 1e-6).all(), "never under the ground")

    def test_too_low_still_climbs(self):
        # the chord cannot clear a road 2 m higher: the curve profile is still used, and holds the clearance
        s, g, z = self.overpass_profile(road=1.7)
        self.assertGreater(float(z.max()), 7.57 + 0.15, "it climbs over the chord")


class DepthTest(unittest.TestCase):
    def test_depth_scales_with_span(self):
        self.assertAlmostEqual(roads.deck_depth(10.0), roads.DECK_MIN_DEPTH)
        self.assertAlmostEqual(roads.deck_depth(24.0), 24.0 / roads.DECK_SPAN_RATIO)
        self.assertAlmostEqual(roads.deck_depth(400.0), roadmesh.DECK_DEPTH + roadmesh.GIRDER_D)
        slab, gird = roads.deck_split(1.1)
        self.assertAlmostEqual(slab + gird, 1.1)
        self.assertAlmostEqual(slab / gird, roadmesh.DECK_DEPTH / roadmesh.GIRDER_D)

    def test_geom_span(self):
        self.assertAlmostEqual(roads.geom_span([np.c_[np.linspace(0, 24, 7), np.zeros(7)]]), 24.0)
        self.assertAlmostEqual(roads.geom_span([np.c_[np.linspace(0, 105, 30), np.zeros(30)]]), 35.0)

    def test_short_water_span_mesh_is_thin(self):
        # a 15 m channel span's deck: its underside (girders' bottom) DECK_MIN_DEPTH under the road, not 2.2 m
        line = [[0.0, 0.0, 5.0], [15.0, 0.0, 5.0]]
        ch = {"m": "road_asphalt_2lane", "w": 10.0, "tl": 11.0, "p": 20, "n": line, "fixed": True, "deck": True,
              "fid": "a", "dd": roads.deck_depth(15.0)}
        got = roadmesh.deck([ch], (0.0, 0.0, 0.0))
        pos = np.asarray(got[0])
        self.assertAlmostEqual(float(pos[:, 2].min()), 5.0 - roads.DECK_MIN_DEPTH, places=3)
        ch.pop("dd")
        pos = np.asarray(roadmesh.deck([ch], (0.0, 0.0, 0.0))[0])
        self.assertAlmostEqual(float(pos[:, 2].min()), 5.0 - roadmesh.DECK_DEPTH - roadmesh.GIRDER_D, places=3)

    def test_chunks_carry_depth(self):
        pts = np.array([[0.0, 0.0, 5.0], [20.0, 0.0, 5.0]])
        spec = roads.Spec(*([None] * len(roads.Spec._fields)))._replace(
            width=10.0, kind="paved", urban=False, cls="primary", surf="asphalt", sides=0, walk=False, lanes=2,
            oneway=False, gap=0.0, median=0.0, link=False, dir=0, kmh=50)
        out = roads._deck_chunks([("a", spec, pts, -82.6)], (-50, -50, 50, 50), None, {"a": 1.0})
        decks = [c for c in out if c.get("deck") and not c.get("ctx")]
        self.assertTrue(decks)
        self.assertTrue(all(c["dd"] == 1.0 for c in decks))


class WaterFillTest(unittest.TestCase):
    def grader(self, need):
        g = object.__new__(grading._Planner)
        g.wet = lambda la, lo: np.ones(np.shape(la))
        g.frame = FRAME

        class Chains:
            def _depth(self, lines):
                return 0.9

            def _water(self, lines, nav, depth):
                # one requirement every 16 m from 20 m to 85 m along the first line
                return [(0, a, 0.0, need) for a in np.arange(20.0, 86.0, 16.0)]
        g._chains_for = lambda: Chains()
        return g

    def test_short_water_span_raises_its_approach(self):
        line = np.c_[np.linspace(0, 105, 30), np.zeros(30)]
        g = self.grader(4.7)
        lift = g._water_lift(line, [], line[0], lambda xy: np.full(len(xy), 4.04))
        self.assertAlmostEqual(lift, 4.7 - 4.04, places=6)
        self.assertEqual(g._water_lift(line, [], line[0], lambda xy: np.full(len(xy), 5.0)), 0.0)

    def test_far_requirement_reached_on_grade(self):
        # a requirement far out from the abutment is reached by the deck's own grade: no fill
        line = np.c_[np.linspace(0, 400, 101), np.zeros(101)]
        g = self.grader(4.7)
        g._chains_for = lambda: type("C", (), {"_depth": lambda s, l: 0.9,
                                               "_water": lambda s, l, n, d, c=0.0: [(0, 200.0, 0.0, 4.7)]})()
        self.assertEqual(g._water_lift(line, [], line[0], lambda xy: np.full(len(xy), 4.0)), 0.0)


class FlushTest(unittest.TestCase):
    def test_approach_meets_deck_across_its_width(self):
        # the graded approach road at each abutment of a raised overpass: within 0.1 m of the deck's end height
        # from one edge of the road to the other
        old = grading.APPROACH_ON
        grading.APPROACH_ON = True
        try:
            f = overpass()
            g = grading.Grader(FRAME, FakeMD(f), flat_elev)

            def elev(la, lo):
                return g.apply(la, lo, flat_elev(la, lo), grading.ROAD_LIFT)
            bp = roads.BridgeProfiles(FRAME, lambda key: f, elev)
            (_fid, spec, xy, z, _lon), = bp.parts([f], (-100, -100, 100, 100))
            xy = np.asarray(xy)[:, :2]
            for e, i in ((0, 1), (-1, -2)):
                t = xy[e] - xy[i]
                t /= np.hypot(*t)
                n = np.array([-t[1], t[0]])
                q = xy[e] + t * 0.5
                pts = np.array([q + n * k * spec.width / 2 for k in (-1.0, -0.5, 0.0, 0.5, 1.0)])
                la, lo, _ = FRAME.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
                road = FRAME.geodetic_to_enu(la, lo, np.asarray(elev(la, lo)))[2]
                self.assertLess(float(np.max(np.abs(road - z[e]))), 0.1, (road, z[e]))
        finally:
            grading.APPROACH_ON = old


if __name__ == "__main__":
    unittest.main()
