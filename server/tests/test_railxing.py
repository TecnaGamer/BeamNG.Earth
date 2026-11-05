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
