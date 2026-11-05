"""Level crossings as one place (railxing.py): a kit per approach before the outermost track, the gate rule, the
country's distances."""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import railways  # noqa: E402
import railxing  # noqa: E402
import roads  # noqa: E402
from test_roads import FRAME, features  # noqa: E402


def flat(xy):
    return np.full(len(xy), 10.0)


def crossing(rail_ys, road_cls="secondary", **kw):
    """A north-south road of road_cls at x = 120 crossed by east-west tracks at the given ys."""
    lines = [[(0.0, y), (250.0, y)] for y in rail_ys] + [[(120.0, 0.0), (120.0, 250.0)]]
    f = features(lines, ["standard_gauge"] * len(rail_ys) + [road_cls], subtypes=["rail"] * len(rail_ys) + ["road"])
    return f, railxing.crossing_rows  # (the rows come through railways.crossing_props)


class RailXingTest(unittest.TestCase):
    def setUp(self):
        self._on = railxing.ENABLED
        railxing.ENABLED = True

    def tearDown(self):
        railxing.ENABLED = self._on

    def rows(self, f, **kw):
        return railways.crossing_props(FRAME, 0, 0, [f], flat, **kw)

    def test_four_tracks_are_one_crossing_with_a_kit_before_the_outer_ones(self):
        f, _ = crossing([100.0, 105.0, 110.0, 115.0])
        rows = self.rows(f)
        self.assertEqual(len(rows), 2, rows)            # not one per track: two approaches
        ys = sorted(r[2] for r in rows)
        self.assertLess(ys[0], 100.0 - 4.0, "before the southern track")
        self.assertGreater(ys[1], 115.0 + 4.0, "past the northern one")
        for r in rows:
            self.assertFalse(100.0 - 3.0 < r[2] < 115.0 + 3.0, "never between the tracks")

    def test_far_apart_tracks_are_two_crossings(self):
        f, _ = crossing([60.0, 160.0])
        self.assertEqual(len(self.rows(f)), 4)

    def test_distance_from_the_rail_by_country(self):
        f, _ = crossing([100.0])
        us = self.rows(f, country="US")
        su = self.rows(f, country="UA")
        d = lambda rs: min(abs(r[2] - 100.0) for r in rs)   # noqa: E731
        self.assertGreater(d(su), d(us), "ex-USSR gates stand further from the rail than the MUTCD's")
        self.assertGreaterEqual(d(su), railxing.BEFORE_M["su"]["gate"] - 0.6)    # (the test frame's metres are ~0.3 % off)
        self.assertTrue(all(r[0].startswith("rrgate") for r in su))

    def test_style_groups(self):
        f, _ = crossing([100.0])
        self.assertTrue(all(r[0] == "rrgate" for r in self.rows(f, country="US")))
        self.assertTrue(all(r[0] == "rrgate_eu" for r in self.rows(f, country="DE")))

    def test_lateral_off_the_carriageway(self):
        f, _ = crossing([100.0])
        sp = roads.road_specs(f)[1]
        for r in self.rows(f, country="DE"):
            self.assertAlmostEqual(abs(r[1] - 120.0), sp.width / 2 + railxing.LATERAL_M["eu"], delta=0.5)

    def test_gate_rule(self):
        flash = ("flash", False)
        self.assertEqual(railxing.decide("residential", 1, False, flash), "flash")      # a small crossing: lamps
        self.assertEqual(railxing.decide("residential", 2, False, flash), "gate")       # two tracks
        self.assertEqual(railxing.decide("tertiary", 1, True, flash), "gate")           # a main line over a tertiary
        self.assertEqual(railxing.decide("tertiary", 1, False, flash), "flash")
        self.assertEqual(railxing.decide("tertiary", 4, True, ("flash", True)), "flash", "barrier=no said so")
        self.assertEqual(railxing.decide("residential", 1, True, ("gate", True)), "gate")
        self.assertEqual(railxing.decide("secondary", 1, False), "gate", "the road class's kit")
        self.assertEqual(railxing.decide("residential", 1, False), "cross")
        self.assertEqual(railxing.decide("residential", 1, False, flash, fra="cross"), "cross", "the inventory first")

    def test_osm_devices(self):
        self.assertEqual(railxing.osm_devices({"crossing:light": "yes"}), ("flash", False))
        self.assertEqual(railxing.osm_devices({"crossing:light": "yes", "crossing:barrier": "no"}), ("flash", True))
        self.assertEqual(railxing.osm_devices({"crossing:barrier": "full"}), ("gate", True))
        self.assertEqual(railxing.osm_devices({"crossing:barrier": "no", "crossing:light": "no"}), ("cross", True))
        self.assertEqual(railxing.osm_devices({}), (None, False))

    def test_main_line_with_lights_gets_gates(self):
        """Kalynivka: OSM says lights and no word on a barrier; a main line over a secondary road."""
        f, _ = crossing([100.0, 106.0], road_cls="residential")
        f.attrs["osm_usage"] = np.array(["main", "main", ""], object)
        f.__dict__["_rail_nodes"] = [(FRAME_LON(120.0), FRAME_LAT(103.0), {"railway": "level_crossing",
                                                                           "crossing:light": "yes"})]
        rows = self.rows(f, country="UA")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r[0].startswith("rrgate") for r in rows), rows)

    def test_deck_across_the_road_with_rails_on_it(self):
        """A main line over a primary street: a rubber deck and two rails across the carriageway, not the tram decal; a
        tram keeps the street decal."""
        import rails
        cells = [features([[(-50.0, 128.0), (300.0, 128.0)], [(100.0, -50.0), (100.0, 300.0)]],
                          ["primary", "standard_gauge"], subtypes=["road", "rail"])]
        half = roads.road_specs(cells[0])[0].width / 2
        ch = rails.tile_chunks(FRAME, 0, 0, cells)
        deck = [c for c in ch if c["m"] == rails.DECK["m"]]
        self.assertEqual(len(deck), 1)
        y = np.asarray(deck[0]["n"])[:, 1]
        self.assertLess(abs(y.min() - (128 - half)), rails.MIN_RUN_M + 1.0)
        self.assertLess(abs(y.max() - (128 + half)), rails.MIN_RUN_M + 1.0)
        self.assertEqual(deck[0]["w"], rails.DECK_W)
        self.assertFalse([c for c in ch if c["m"] == rails.STREET_TRACK["m"]], "no tram decal on a railway")
        ext = rails.DECK_EXT + 1.0
        on = [c for c in ch if c["m"] == rails.RAIL["m"] and np.asarray(c["n"])[:, 1].min() > 128 - half - ext
              and np.asarray(c["n"])[:, 1].max() < 128 + half + ext]
        self.assertEqual(len(on), 2, "both rails across the deck")
        for c in on:
            y = np.asarray(c["n"])[:, 1]
            self.assertGreater(y.max() - y.min(), 2 * half + 2.0, "the rails run on past the road's edge")
            self.assertEqual(c["w"], rails.DECK_RAIL_W)
        names = [c["m"] for c in ch]
        self.assertLess(names.index(rails.DECK["m"]), names.index(rails.RAIL["m"]), "the deck drawn first")
        railxing.ENABLED = False
        ch = rails.tile_chunks(FRAME, 0, 0, cells)
        self.assertTrue([c for c in ch if c["m"] == rails.STREET_TRACK["m"]], "off: the decal as before")

    def test_crushed_stone_ballast_on_a_track_in_use(self):
        f = features([[(0.0, 100.0), (250.0, 100.0)]], ["standard_gauge"], subtypes=["rail"])
        P, T, UV, mats, M = railways.tile_mesh(FRAME, 0, 0, [f], flat)
        self.assertIn(railways.BALLAST_STONE, mats)
        self.assertFalse((M == mats.index(railways.BALLAST)).any(), "no stock gravel triangles")
        top = np.unique(T[M == mats.index(railways.BALLAST_STONE)])
        u, v = UV[top, 0], UV[top, 1]
        self.assertAlmostEqual(float(u.max() - u.min()), 2 * railways.BALLAST_TOP_M / railways.BALLAST_TILE_M, delta=1.0)
        self.assertGreater(float(v.max() - v.min()), 200.0 / railways.BALLAST_TILE_M * 0.8, "tiled every 0.8 m")
        railxing.ENABLED = False
        _P, _T, _UV, mats, _M = railways.tile_mesh(FRAME, 0, 0, [f], flat)
        self.assertIn(railways.BALLAST, mats, "off: the stock gravel as before")

    def test_parallel_tracks_share_a_profile(self):
        """Two tracks 5 m apart over a ground with a bump under one: their beds agree; a track 60 m off keeps its own."""
        def ground(xy):
            xy = np.asarray(xy, np.float64)
            return 10.0 + 1.5 * np.exp(-((xy[:, 0] - 120.0) ** 2 / 200.0 + (xy[:, 1] - 100.0) ** 2 / 4.0))
        f = features([[(0.0, 100.0), (250.0, 100.0)], [(0.0, 105.0), (250.0, 105.0)], [(0.0, 190.0), (250.0, 190.0)]],
                     ["standard_gauge"] * 3, subtypes=["rail"] * 3)
        rails = railways.parts(FRAME, [f], roads.tile_bbox(0, 0), clip=True)
        gap = lambda prof: np.abs(prof[0][0] - prof[1][0]).max()    # noqa: E731
        railxing.ENABLED = False
        before = railways.profile(rails, ground)
        railxing.ENABLED = True
        after = railways.profile(rails, ground)
        self.assertGreater(gap(before), 0.2, "the bump alone under track one")
        self.assertLess(gap(after), gap(before) * 0.5)
        np.testing.assert_allclose(after[2][0], before[2][0], atol=1e-9)    # (the far track: untouched)

    def test_corridor_ground_is_deterministic_and_symmetric(self):
        a = np.c_[np.arange(0.0, 100.0, 2.0), np.zeros(50)]
        b = np.c_[np.arange(0.0, 100.0, 2.0), np.full(50, 5.0)]
        za, zb = np.linspace(10, 12, 50), np.linspace(10.4, 12.4, 50)
        ra, rb = railxing.corridor_ground([a, b], [za, zb])
        np.testing.assert_allclose(ra, rb)
        np.testing.assert_allclose(ra, (za + zb) / 2)
        far = np.c_[np.arange(0.0, 100.0, 2.0), np.full(50, 80.0)]
        r = railxing.corridor_ground([a, far], [za, zb])
        np.testing.assert_allclose(r[0], za)
        np.testing.assert_allclose(r[1], zb)

    def test_buildings_keep_off_the_railway_bed(self):
        import buildings
        f = features([[(-100.0, 100.0), (400.0, 100.0)], [(-100.0, 140.0), (400.0, 140.0)]],
                     ["standard_gauge", "tram"], subtypes=["rail", "rail"])
        x0, y0 = roads.tile_bbox(0, 0)[:2]
        rl = buildings.rail_lines(FRAME, [f], x0, y0)
        self.assertEqual(len(rl), 1, "the railway, not the tram")
        hut = np.array([[50.0, 101.5], [58.0, 101.5], [58.0, 106.0], [50.0, 106.0]]) - [x0, y0]
        far = np.array([[50.0, 112.0], [58.0, 112.0], [58.0, 118.0], [50.0, 118.0]]) - [x0, y0]
        astride = np.array([[50.0, 95.0], [58.0, 95.0], [58.0, 105.0], [50.0, 105.0]]) - [x0, y0]
        self.assertTrue(buildings.on_rail_bed(hut, rl), "its wall within the bed's reach")
        self.assertTrue(buildings.on_rail_bed(astride, rl), "across the track")
        self.assertFalse(buildings.on_rail_bed(far, rl))
        railxing.ENABLED = False
        self.assertEqual(buildings.rail_lines(FRAME, [f], x0, y0), [], "off: nothing")

    def test_the_grader_plans_the_bed_flush_with_the_road(self):
        """The bed the terrain is graded to is lowered to the road's level at a level crossing, as the mesh lays it (else
        the ballast and sleepers lay under the ground): lower than the smoothed ground by the rail's height there."""
        from test_roads import flat as dem
        f = features([[(0.0, 100.0), (250.0, 100.0)], [(120.0, 0.0), (120.0, 250.0)]], ["standard_gauge", "secondary"],
                     subtypes=["rail", "road"])
        box = roads.tile_bbox(0, 0)
        railxing.ENABLED = False
        before = railways.grade_plans(FRAME, [f], box, dem)
        railxing.ENABLED = True
        after = railways.grade_plans(FRAME, [f], box, dem)
        zb0, zb1 = before[0][2], after[0][2]
        xy = before[0][1]
        at = np.argmin(np.abs(xy[:, 0] - 120.0))
        self.assertAlmostEqual(float(zb0[at]), 10.0, delta=0.05)
        self.assertLess(float(zb1[at]), 10.0 - 0.3, "lowered to the road's level at the crossing")
        far = np.argmin(np.abs(xy[:, 0] - 20.0))
        self.assertAlmostEqual(float(zb1[far]), float(zb0[far]), delta=0.05)

    def test_abandoned_none_and_disused_cross(self):
        f, _ = crossing([100.0])
        f.rules["flags"] = _flags([(0, "is_abandoned")])
        self.assertEqual(self.rows(f), [])
        f.rules["flags"] = _flags([(0, "is_disused")])
        self.assertTrue(all(r[0].startswith("rrcross") for r in self.rows(f)))

    def test_clusters_cut_at_the_span(self):
        line = np.array([[0.0, 0.0], [0.0, 400.0]])
        hits = [(i, (0.0, 10.0 * i)) for i in range(1, 12)]
        cs = railxing.clusters(line, hits)
        self.assertGreater(len(cs), 1)
        for c in cs:
            self.assertLessEqual(c[-1][0] - c[0][0], railxing.MAX_SPAN_M + 1e-6)

    def test_style_of(self):
        self.assertEqual(railxing.style_of("UA"), "su")
        self.assertEqual(railxing.style_of("us"), "us")
        self.assertEqual(railxing.style_of("DE"), "eu")
        self.assertEqual(railxing.style_of(None, -100.0), "us")
        self.assertEqual(railxing.style_of(None, 20.0), "eu")


def FRAME_LON(e):
    from test_roads import LON, M_LON
    return LON + e / M_LON


def FRAME_LAT(n):
    from test_roads import LAT, M_LAT
    return LAT + n / M_LAT


def _flags(entries):
    from test_roads import rules
    return rules(entries)


if __name__ == "__main__":
    unittest.main()
