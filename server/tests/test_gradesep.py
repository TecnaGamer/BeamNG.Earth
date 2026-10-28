"""The grade separations the map has no bridge for (gradesep.py): a line whose ground stands above another's where they
cross, a road over a railway's short tunnel, a short stretch between two decks; nothing on flat ground or a hillside;
the spans in roads.level_ranges beside the map's own."""
import os
import sys
import unittest
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import geodesy  # noqa: E402
import gradesep  # noqa: E402

gradesep.ON = True      # (off by default: these test the rule itself)
import mapcells  # noqa: E402
import roads  # noqa: E402
from test_roads import LAT, LON, features  # noqa: E402

KEY = mapcells.cell_of(LAT, LON)
FRAME = geodesy.EnuFrame(*KEY.center(), 0.0)


class FakeMd:
    """MapData with no neighbours and a DEM of ground(e, n) in the cell's frame."""

    def __init__(self, ground):
        self.ground = ground
        self.dem = SimpleNamespace(sample=self.sample)

    def sample(self, lat, lon, zoom):
        e, n, _ = FRAME.geodetic_to_enu(np.asarray(lat), np.asarray(lon), 0.0)
        return self.ground(np.asarray(e), np.asarray(n))

    def cell(self, key, layers=(), kind="demand"):
        return SimpleNamespace(layers={"roads": None})


def hooked(f, ground):
    f.__dict__["_bnge_gradesep"] = gradesep.hook(FakeMd(ground), KEY)
    return f


def cross(classes=("residential", "residential"), **kw):
    """An east-west line and a north-south one, 400 m each, crossing at their middles (no node there)."""
    return features([[(-200.0, 0.0), (200.0, 0.0)], [(0.0, -200.0), (0.0, 200.0)]], list(classes), **kw)


def embankment(e, n):
    """7 m of fill along the east-west line (within 6 m of it), 0 elsewhere."""
    _, n0, _ = FRAME.geodetic_to_enu(np.array([LAT]), np.array([LON]), 0.0)
    return np.where(np.abs(n - n0[0]) < 6.0, 7.0, 0.0)


class HeightsTest(unittest.TestCase):
    def test_the_higher_line_gets_a_bridge_over_the_other(self):
        f = hooked(cross(), embankment)
        got = roads.level_ranges(f, 0)
        self.assertEqual(len(got), 1)
        a, b, k = got[0]
        self.assertEqual(k, "bridge")
        self.assertLess(a, 0.5)
        self.assertGreater(b, 0.5)
        # the lower road's half width (3 m) + CLEAR_M either side, over 400 m (skew_decks may lengthen it a little)
        self.assertAlmostEqual(b - a, 2 * (3.0 + gradesep.CLEAR_M) / 400.0, delta=0.02)
        self.assertEqual(roads.level_ranges(f, 1), [])

    def test_flat_ground_and_a_hillside_give_none(self):
        for ground in (lambda e, n: np.zeros_like(e), lambda e, n: 0.3 * n, lambda e, n: 0.3 * e):
            f = hooked(cross(), ground)
            self.assertEqual(roads.level_ranges(f, 0), [])
            self.assertEqual(roads.level_ranges(f, 1), [])

    def test_a_junction_is_none(self):
        # the north-south road ends at the crossing (a T junction): no bridge, whatever the ground
        f = hooked(features([[(-200.0, 0.0), (200.0, 0.0)], [(0.0, 0.0), (0.0, 200.0)]],
                            ["residential", "residential"]), embankment)
        self.assertEqual(roads.level_ranges(f, 0), [])

    def test_the_map_s_own_ranges_win(self):
        # a tunnel range of the map's over the crossing: left as it is
        f = hooked(cross(flags=[(0, "is_tunnel", 0.4, 0.6)]), embankment)
        self.assertEqual([k for _, _, k in roads.level_ranges(f, 0)], ["tunnel"])

    def test_off(self):
        gradesep.ON = False
        try:
            self.assertEqual(roads.level_ranges(hooked(cross(), embankment), 0), [])
        finally:
            gradesep.ON = True


class ShortTunnelTest(unittest.TestCase):
    def test_a_road_over_a_railway_s_short_tunnel(self):
        # the railway's 40 m "tunnel" under the road (OSM's way of saying the road bridges it)
        f = hooked(cross(("residential", "standard_gauge"), subtypes=["road", "rail"],
                         flags=[(1, "is_tunnel", 0.45, 0.55)]), lambda e, n: np.zeros_like(e))
        got = roads.level_ranges(f, 0)
        self.assertEqual([k for _, _, k in got], ["bridge"])
        self.assertLess(got[0][0], 0.5)
        self.assertGreater(got[0][1], 0.5)

    def test_a_level_crossing_is_none(self):
        f = hooked(cross(("residential", "standard_gauge"), subtypes=["road", "rail"]), lambda e, n: np.zeros_like(e))
        self.assertEqual(roads.level_ranges(f, 0), [])
        self.assertEqual(roads.level_ranges(f, 1), [])


class UnderpassTest(unittest.TestCase):
    def test_a_road_over_a_short_road_tunnel_bridges_it(self):
        # OSM's underpass: the lower road's 40 m "tunnel" under a road on the ground (the A7 at Brackel, #243)
        f = hooked(cross(("motorway", "secondary"), flags=[(1, "is_tunnel", 0.45, 0.55)]), lambda e, n: np.zeros_like(e))
        self.assertEqual([k for _, _, k in roads.level_ranges(f, 0)], ["bridge"])
        self.assertEqual(roads.level_ranges(f, 1), [])          # (its tunnel: ground under the bridge)

    def test_a_long_tunnel_in_short_pieces_stays_a_tunnel(self):
        # two 50 m tunnel pieces joined end to end (Overture splits a tunnel at its nodes): 100 m, no underpass
        f = hooked(features([[(-200.0, 0.0), (200.0, 0.0)], [(0.0, -50.0), (0.0, 0.0)], [(0.0, 0.0), (0.0, 50.0)]],
                            ["motorway", "secondary", "secondary"], flags=[(1, "is_tunnel"), (2, "is_tunnel")]),
                   lambda e, n: np.zeros_like(e))
        self.assertEqual(roads.level_ranges(f, 0), [])
        self.assertEqual([k for _, _, k in roads.level_ranges(f, 1)], ["tunnel"])

    def test_a_short_tunnel_through_a_hill_stays_a_tunnel(self):
        # 20 m of ground over the road crossing it above the portals' approaches
        _, n0, _ = FRAME.geodetic_to_enu(np.array([LAT]), np.array([LON]), 0.0)
        hill = lambda e, n: np.where(np.abs(n - n0[0]) < 15.0, 20.0, 0.0)
        f = hooked(cross(("residential", "secondary"), flags=[(1, "is_tunnel", 0.45, 0.55)]), hill)
        self.assertEqual([k for _, _, k in roads.level_ranges(f, 1)], ["tunnel"])
        self.assertEqual(roads.level_ranges(f, 0), [])


class GapTest(unittest.TestCase):
    def test_a_short_stretch_between_decks_over_a_road_is_a_bridge(self):
        # decks over 0-160 m and 240-400 m, the 80 m between them on the ground over the other road
        f = hooked(cross(flags=[(0, "is_bridge", 0.0, 0.4), (0, "is_bridge", 0.6, 1.0)]), lambda e, n: np.zeros_like(e))
        self.assertEqual(roads.level_ranges(f, 0), [(0.0, 1.0, "bridge")])

    def test_a_long_one_is_left(self):
        f = hooked(cross(flags=[(0, "is_bridge", 0.0, 0.1), (0, "is_bridge", 0.9, 1.0)]), lambda e, n: np.zeros_like(e))
        self.assertEqual(len(roads.level_ranges(f, 0)), 2)


    def test_a_stretch_shorter_than_its_decks_is_a_bridge(self):
        # an interchange ramp: 160 m decks and 80 m between them, crossing nothing (the line alone)
        f = hooked(features([[(-200.0, 0.0), (200.0, 0.0)]], ["motorway"],
                            flags=[(0, "is_bridge", 0.0, 0.4), (0, "is_bridge", 0.6, 1.0)]), lambda e, n: np.zeros_like(e))
        self.assertEqual(roads.level_ranges(f, 0), [(0.0, 1.0, "bridge")])

    def test_a_freeway_between_short_street_bridges_stays_on_the_ground(self):
        # 30 m decks over the cross streets, 120 m of freeway between them
        f = hooked(features([[(-90.0, 0.0), (90.0, 0.0)]], ["motorway"],
                            flags=[(0, "is_bridge", 0.0, 1 / 6), (0, "is_bridge", 5 / 6, 1.0)]),
                   lambda e, n: np.zeros_like(e))
        self.assertEqual(len(roads.level_ranges(f, 0)), 2)


class JoinedTest(unittest.TestCase):
    def test_a_span_past_a_line_s_end_goes_on_along_the_next(self):
        # the upper road is two segments joined 3 m past the crossing: the span covers both sides
        f = hooked(features([[(-200.0, 0.0), (3.0, 0.0)], [(3.0, 0.0), (200.0, 0.0)], [(0.0, -200.0), (0.0, 200.0)]],
                            ["residential"] * 3), embankment)
        a = roads.level_ranges(f, 0)
        b = roads.level_ranges(f, 1)
        self.assertEqual(len(a), 1)
        self.assertAlmostEqual(a[0][1], 1.0)
        self.assertEqual(len(b), 1)
        self.assertAlmostEqual(b[0][0], 0.0)
        self.assertGreater(b[0][1] * 197.0, 3.0)


if __name__ == "__main__":
    unittest.main()
