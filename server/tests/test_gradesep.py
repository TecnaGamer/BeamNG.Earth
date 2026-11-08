"""The grade separations the map has no bridge for (gradesep.py): a line whose ground stands above another's where they
cross, a road over a railway's short tunnel, a short stretch between two decks; nothing on flat ground or a hillside;
the spans in roads.level_ranges beside the map's own."""
import gc
import os
import sys
import unittest
import weakref
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import geodesy  # noqa: E402
import gradesep  # noqa: E402

gradesep.ON = gradesep.TUNNELS = True      # (only the tunnels by default: these test the whole rule)
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
        gradesep.ON = gradesep.TUNNELS = False
        try:
            self.assertEqual(roads.level_ranges(hooked(cross(), embankment), 0), [])
            f = hooked(cross(("motorway", "secondary"), flags=[(1, "is_tunnel", 0.45, 0.55)]),
                       lambda e, n: np.zeros_like(e))
            self.assertEqual(roads.level_ranges(f, 0), [])
        finally:
            gradesep.ON = gradesep.TUNNELS = True


class TunnelsOnlyTest(unittest.TestCase):
    """The default: only the lines over a short tunnel; a cell with no such tunnel reads no neighbour."""

    def setUp(self):
        gradesep.ON = False

    def tearDown(self):
        gradesep.ON = True

    def test_an_underpass_still_gets_its_bridge(self):
        f = hooked(cross(("motorway", "secondary"), flags=[(1, "is_tunnel", 0.45, 0.55)]), lambda e, n: np.zeros_like(e))
        self.assertEqual([k for _, _, k in roads.level_ranges(f, 0)], ["bridge"])
        self.assertEqual(roads.level_ranges(f, 1), [])

    def test_heights_alone_give_none_and_no_neighbour_is_read(self):
        md = FakeMd(embankment)
        asked = []
        md.cell = lambda key, layers=(), kind="demand": asked.append(key)
        f = cross()
        f.__dict__["_bnge_gradesep"] = gradesep.hook(md, KEY)
        self.assertEqual(roads.level_ranges(f, 0), [])
        self.assertEqual(asked, [])


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


class NeighboursMd:
    """MapData's cell() for neighbours_hook: each cell's roads layer a real Features, kept in `mem` (its memory cache)
    until a test evicts it; `missing` cells have none. Every cell() asked is in `asked`."""

    def __init__(self, missing=()):
        self.mem, self.asked, self.missing = {}, [], set(missing)

    def cell(self, key, layers=(), kind="demand"):
        self.asked.append(key)
        if key in self.missing:
            return SimpleNamespace(layers={"roads": None})
        f = self.mem.get(key)
        if f is None:
            f = self.mem[key] = features([[(0.0, 0.0), (50.0, 0.0)]], ["residential"])
        return SimpleNamespace(layers={"roads": f})


class NeighboursHookTest(unittest.TestCase):
    """The roads layers of the eight cells round a cell (neighbours_hook) are handed back while the map data keeps them,
    and never kept alive by the hook (GitHub #893: every layer held its neighbours, those theirs, so no roads layer a
    server process decoded was ever freed by the map data's memory cap)."""

    def test_a_neighbour_the_cache_let_go_of_is_freed(self):
        md = NeighboursMd()
        hook = gradesep.neighbours_hook(md, KEY)
        refs = [weakref.ref(f) for f in hook() if f is not None]
        self.assertEqual(len(refs), 8)
        md.mem.clear()
        gc.collect()
        self.assertEqual(sum(r() is not None for r in refs), 0)

    def test_live_neighbours_come_back_without_asking_again(self):
        md = NeighboursMd()
        hook = gradesep.neighbours_hook(md, KEY)
        first = hook()
        asked = len(md.asked)
        again = hook()
        self.assertEqual(len(md.asked), asked)
        self.assertTrue(all(a is b for a, b in zip(first, again)))

    def test_only_a_gone_one_is_asked_again(self):
        east, west = mapcells.CellKey(KEY.z, KEY.x + 1, KEY.y), mapcells.CellKey(KEY.z, KEY.x - 1, KEY.y)
        md = NeighboursMd(missing={west})
        hook = gradesep.neighbours_hook(md, KEY)
        self.assertEqual(sum(f is None for f in hook()), 1)          # (the missing one)
        del md.mem[east]
        gc.collect()
        md.asked.clear()
        again = hook()
        self.assertEqual(md.asked, [east])                           # (not the missing one, not the live ones)
        self.assertTrue(any(f is md.mem[east] for f in again))
        self.assertEqual(sum(f is None for f in again), 1)
        self.assertEqual({id(f) for f in again if f is not None}, {id(f) for f in md.mem.values()})


class NearMd(FakeMd):
    """FakeMd with roads layers in the eight cells round KEY: lines through the cell's own (one the same feature as
    the cell's own line id001, a copy as Overture has one in each cell it crosses) and far ones."""

    def __init__(self, ground, own):
        super().__init__(ground)
        self.own = own

    def cell(self, key, layers=(), kind="demand"):
        dx, dy = key.x - KEY.x, key.y - KEY.y
        lines = [[(-300.0 + 40 * dx, 30.0 * dy), (300.0, 30.0 * dy + 5.0)], [(5000.0, 5000.0), (5100.0, 5000.0)],
                 [(0.0, -200.0), (0.0, 200.0)], [(20.0 * dx, -80.0), (20.0 * dx, 80.0)]]
        ids = [f"n{dx}{dy}a", f"n{dx}{dy}b", "id001", f"n{dx}{dy}c"]
        order = np.argsort(ids)
        f = features([lines[i] for i in order], ["primary", "residential", "residential", "secondary"],
                     flags=[(int(np.flatnonzero(order == 3)[0]), "is_tunnel", 0.4, 0.6)], ids=[ids[i] for i in order])
        return SimpleNamespace(layers={"roads": f})


class ReferenceTest(unittest.TestCase):
    """_spans gives exactly the answers (and the probes' why) of the Python it was before its rows and lines moved to
    earthfast (tests/gradesep_reference.py), whole rule and tunnels only."""

    def check(self, f, md):
        import gradesep_reference
        for on in (True, False):
            gradesep.ON = on
            try:
                f.__dict__.pop("_gradesep_why", None)
                want = gradesep_reference.spans(md, KEY, f)
                why = f.__dict__.pop("_gradesep_why", None)
                got = gradesep._spans(md, KEY, f)
                self.assertEqual(repr(got), repr(want))
                self.assertEqual(repr(f.__dict__.get("_gradesep_why")), repr(why))
            finally:
                gradesep.ON = True

    def test_the_fixtures(self):
        flat0 = lambda e, n: np.zeros_like(e)    # noqa: E731
        for f, ground in [
                (cross(), embankment),
                (cross(("motorway", "secondary"), flags=[(1, "is_tunnel", 0.45, 0.55)]), flat0),
                (cross(("residential", "standard_gauge"), subtypes=["road", "rail"],
                       flags=[(1, "is_tunnel", 0.45, 0.55)]), flat0),
                (cross(flags=[(0, "is_bridge", 0.0, 0.4), (0, "is_bridge", 0.6, 1.0)]), flat0),
                (features([[(-200.0, 0.0), (200.0, 0.0)], [(0.0, -50.0), (0.0, 0.0)], [(0.0, 0.0), (0.0, 50.0)]],
                          ["motorway", "secondary", "secondary"], flags=[(1, "is_tunnel"), (2, "is_tunnel")]), flat0),
                (features([[(-200.0, 0.0), (3.0, 0.0)], [(3.0, 0.0), (200.0, 0.0)], [(0.0, -200.0), (0.0, 200.0)]],
                          ["residential"] * 3), embankment)]:
            with self.subTest(n=len(f)):
                self.check(f, FakeMd(ground))

    def test_neighbours(self):
        # the cell's own lines, its neighbours' over and under them (one its own line again), short tunnels in both
        f = cross(("motorway", "secondary"), flags=[(1, "is_tunnel", 0.45, 0.55)])
        self.check(f, NearMd(embankment, f))
        self.check(cross(), NearMd(lambda e, n: np.zeros_like(e), None))


if __name__ == "__main__":
    unittest.main()
