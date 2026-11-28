"""windsocks: a windsock beside each paved runway end, on the left of a pilot at that end looking down the runway and off
the pavement (a short runway's at its midpoint); a mapped windsock stands instead of the generated ones; the sock
streams downwind of the given wind, else along its runway; none for a closed, water or unpaved runway nor without an
airport; each in one tile; the shape's file agrees with the row (height, +Y the sock's axis, a light mesh)."""
import json
import math
import os
import re
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import airportfixtures as af  # noqa: E402
import airports  # noqa: E402
import geodesy  # noqa: E402
import mapgeom  # noqa: E402
import windsocks as ws  # noqa: E402

FRAME = geodesy.EnuFrame(40.0, -76.0)       # the runway's low end is its origin, the runway runs east
LENGTH, WIDTH = 2000.0, 45.72
BOX = (-500.0, -700.0, 2600.0, 700.0)
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
SHAPES = os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth")


def runways(*specs):
    """airports.Runways of (lat, lon, heading, length_m, kw) specs at one airport."""
    ap = af.airport(1, "KTST", 40.0, -76.0, 100)
    rows = [af.runway(i + 1, ap, lat, lon, hdg, length, **kw) for i, (lat, lon, hdg, length, kw) in enumerate(specs)]
    return airports.AirportDB.from_csv(af.to_csv([ap], af.AIRPORT_COLS), af.to_csv(rows, af.RUNWAY_COLS)).runways


def runway(length=LENGTH, **kw):
    return runways((40.0, -76.0, 90.0, length, kw))


def en(lat, lon):
    e, n, _ = FRAME.geodetic_to_enu(np.asarray(lat, np.float64), np.asarray(lon, np.float64), 0.0)
    return float(e), float(n)


def spot_en(sp):
    return en(sp[0], sp[1])


def lonlat(pts):
    la, lo, _ = FRAME.enu_to_geodetic(np.array([p[0] for p in pts], np.float64), np.array([p[1] for p in pts], np.float64),
                                      0.0)
    return np.stack([lo, la], 1)


def feats(*rows):
    """Infrastructure Features of (point (east, north) in FRAME, class, tags dict) rows."""
    ids = np.array([f"f{i:03d}" for i in range(len(rows))])
    f = mapgeom.build("infrastructure", ids, [(mapgeom.POINT, [[lonlat([r[0]])]]) for r in rows])
    f.attrs = {"class": mapgeom.text_column([r[1] for r in rows]), "subtype": mapgeom.text_column(["airport"] * len(rows)),
               "tags": mapgeom.text_column([json.dumps(r[2]) for r in rows]),
               "surface": mapgeom.text_column([None] * len(rows))}
    return f


def points_to(yaw):
    """(east, north) a row's yaw turns the shape's +Y to (the server's yaw: counter-clockwise from +x)."""
    a = math.radians(yaw + 90.0)
    return math.cos(a), math.sin(a)


def ground(lat, lon):
    return np.full(np.shape(lat), 12.0)


def rows(rws, f=None, box=BOX, wind_deg=None, roads_fn=None):
    return ws.tile_rows(FRAME, box, lambda k: f, lambda k: rws, ground, wind_deg=wind_deg, keys=[0], roads_fn=roads_fn)


class Spots(unittest.TestCase):
    def test_one_per_end_on_the_left_off_the_pavement(self):
        got = ws.spots(runway())
        self.assertEqual(len(got), 2)
        (e0, n0), (e1, n1) = sorted(spot_en(sp) for sp in got)
        out = WIDTH / 2.0 + ws.OUT_BEYOND_M
        self.assertAlmostEqual(e0, ws.IN_M, delta=1.0)               # low end: 300 m in, looking east its left is north
        self.assertAlmostEqual(n0, out, delta=1.0)
        self.assertAlmostEqual(e1, LENGTH - ws.IN_M, delta=1.0)      # high end: looking west its left is south
        self.assertAlmostEqual(n1, -out, delta=1.0)
        self.assertTrue(all(abs(n) > WIDTH / 2.0 + 30.0 for n in (n0, n1)))

    def test_a_short_runway_has_one_at_its_middle(self):
        got = ws.spots(runway(length=700.0))
        self.assertEqual(len(got), 1)
        e, n = spot_en(got[0])
        self.assertAlmostEqual(e, 350.0, delta=1.0)
        self.assertGreater(n, 0.0)
        self.assertEqual(len(ws.spots(runway(length=ws.SHORT_M + 1.0))), 2)

    def test_none_for_closed_water_and_unpaved_runways_nor_without_any(self):
        self.assertEqual(ws.spots(runway(closed=1)), [])
        self.assertEqual(ws.spots(runway(surface="WATER")), [])
        self.assertEqual(ws.spots(runway(surface="GRS", lighted=0)), [])
        self.assertEqual(len(ws.spots(runway(surface="CON"))), 2)
        self.assertEqual(ws.spots([]), [])
        self.assertEqual(ws.spots(None), [])

    def test_not_on_another_runways_pavement(self):
        lat, lon = FRAME.enu_to_geodetic(ws.IN_M, -600.0, 0.0)[:2]             # a runway north from (300, -600) over it
        cross = {"le": "18", "he": "36"}
        both = runways((40.0, -76.0, 90.0, LENGTH, {}), (float(lat), float(lon), 0.0, 1500.0, cross))
        spot = (ws.IN_M, WIDTH / 2.0 + ws.OUT_BEYOND_M)                        # the first runway's low-end sock
        self.assertTrue(any(math.dist(spot_en(sp), spot) < 5.0 for sp in ws.spots(runway())))
        self.assertFalse(any(math.dist(spot_en(sp), spot) < 5.0 for sp in ws.spots(both)))
        self.assertGreater(len(ws.spots(both)), 0)                             # (the crossing runway's own stand)

    def test_close_spots_are_one(self):
        a = runway()
        b = runways((40.00005, -76.0, 90.0, LENGTH, {}))                # a runway 5 m off: its spots are the same places
        b = [airports.Runway(**{**{f: getattr(b[0], f) for f in airports.Runway.__slots__}, "id": 99})]
        self.assertEqual(len(ws.spots(a + b)), 2)

    def test_memoised(self):
        a = runway()
        self.assertIs(ws._clear_spots(a), ws._clear_spots(list(a)))


def line_feats(layer, cls, *lines, **tags):
    """Features of (east, north) polylines in FRAME: an airport's infrastructure ways (class cls) or a roads layer's."""
    ids = np.array([f"l{i:03d}" for i in range(len(lines))])
    f = mapgeom.build(layer, ids, [(mapgeom.LINE, [[lonlat(ln)]]) for ln in lines])
    f.attrs = {"class": mapgeom.text_column([cls] * len(lines)),
               "subtype": mapgeom.text_column(["airport" if layer == "infrastructure" else "road"] * len(lines)),
               "tags": mapgeom.text_column([json.dumps(tags)] * len(lines)),
               "surface": mapgeom.text_column([None] * len(lines))}
    return f


class Clear(unittest.TestCase):
    """The sock steps off pavement and roads, or is left out."""
    FIRST = (ws.IN_M, WIDTH / 2.0 + ws.OUT_BEYOND_M)            # the low end's own place
    ACROSS = [(-500.0, FIRST[1]), (2600.0, FIRST[1])]           # a road along the runway through it

    def low(self, got):
        return min((spot_en(sp) for sp in got), key=lambda p: p[0])

    def test_a_road_through_the_first_spot_moves_the_sock(self):
        rd = [lonlat(self.ACROSS)]
        e, n = self.low(ws.spots(runway(), roads=rd))
        self.assertGreater(abs(n - self.FIRST[1]), ws.ROAD_CLEAR_M)                 # off the road
        self.assertLess(math.dist((e, n), self.FIRST), 101.0)                       # and still near the runway end
        self.assertGreater(n, WIDTH / 2.0 + ws.OUT_BEYOND_M - 1.0)                  # (on the left, no nearer the runway)
        self.assertEqual(len(ws.spots(runway(), roads=rd)), 2)

    def test_a_road_elsewhere_changes_nothing(self):
        far = [lonlat([(-500.0, 400.0), (2600.0, 400.0)])]
        self.assertEqual(ws.spots(runway(), roads=far), ws.spots(runway()))
        self.assertEqual(ws.spots(runway(), roads=[]), ws.spots(runway()))

    def test_a_pad_over_the_first_spot_moves_the_sock(self):
        n0 = self.FIRST[1]
        ll = lonlat([(200.0, n0 - 30.0), (400.0, n0 - 30.0), (400.0, n0 + 40.0), (200.0, n0 + 40.0), (200.0, n0 - 30.0)])
        pad = (ll[:, 1], ll[:, 0])                                              # (aeroways.pads' (lat, lon) outline)
        e, n = self.low(ws.spots(runway(), pads=[pad]))
        self.assertFalse(200.0 - ws.PAVED_CLEAR_M < e < 400.0 + ws.PAVED_CLEAR_M and
                         n0 - 30.0 - ws.PAVED_CLEAR_M < n < n0 + 40.0 + ws.PAVED_CLEAR_M)

    def test_no_clear_spot_no_sock(self):
        walls = [lonlat([(x, -400.0), (x, 400.0)]) for x in range(-100, 2700, 20)]       # roads every 20 m, all round
        got = ws.spots(runway(), roads=walls)
        self.assertEqual(got, [])

    def test_only_the_blocked_end_moves(self):
        got = sorted(spot_en(sp) for sp in ws.spots(runway(), roads=[lonlat(self.ACROSS)]))
        self.assertAlmostEqual(got[1][0], LENGTH - ws.IN_M, delta=1.0)               # the high end's own place
        self.assertAlmostEqual(got[1][1], -(WIDTH / 2.0 + ws.OUT_BEYOND_M), delta=1.0)

    def test_tile_rows_read_the_roads_and_the_taxiways(self):
        n0 = self.FIRST[1]
        roads = line_feats("roads", "service", self.ACROSS)
        got = sorted(rows(runway(), roads_fn=lambda k: roads), key=lambda r: r[1])
        self.assertEqual(len(got), 2)
        self.assertGreater(abs(got[0][2] - n0), ws.ROAD_CLEAR_M)
        foot = line_feats("roads", "footway", self.ACROSS)                           # (a footpath is no reason to move)
        self.assertAlmostEqual(sorted(rows(runway(), roads_fn=lambda k: foot), key=lambda r: r[1])[0][2], n0, delta=1.0)
        taxi = line_feats("infrastructure", "taxiway", [(-500.0, n0), (2600.0, n0)], width="23")
        moved = sorted(rows(runway(), f=taxi), key=lambda r: r[1])
        self.assertEqual(len(moved), 2)
        self.assertGreater(abs(moved[0][2] - n0), 23.0 / 2.0 + ws.PAVED_CLEAR_M - 0.5)
        self.assertAlmostEqual(sorted(rows(runway()), key=lambda r: r[1])[0][2], n0, delta=1.0)

    def test_the_candidates_stay_off_the_runway_ends(self):
        for ladder in ws._candidates(runway()[0]):
            for sp in ladder:
                self.assertTrue(0.1 * LENGTH - 1.0 <= spot_en(sp)[0] <= 0.9 * LENGTH + 1.0)


class Mapped(unittest.TestCase):
    def test_the_infrastructure_windsocks(self):
        f = feats(((600.0, 150.0), "windsock", {}), ((900.0, 150.0), "gate", {"aeroway": "windsock"}),
                  ((100.0, 100.0), "gate", {"aeroway": "gate"}), ((50.0, 0.0), "windsock_mast", {}))
        got = ws.mapped(f)
        self.assertEqual(len(got), 2)
        self.assertEqual(sorted(round(v) for v in (en(*p)[0] for p in got)), [600, 900])
        self.assertIs(ws.mapped(f), ws.mapped(f))
        self.assertEqual(ws.mapped(None), [])

    def test_a_mapped_windsock_stands_instead(self):
        got = rows(runway(), feats(((700.0, 200.0), "windsock", {})))
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0][1], 700.0, delta=0.5)
        self.assertAlmostEqual(got[0][2], 200.0, delta=0.5)
        self.assertEqual(got[0][0], ws.GROUP)

    def test_a_far_mapped_windsock_leaves_the_runways_own(self):
        far = feats(((700.0, 5000.0), "windsock", {}))
        self.assertEqual(len(ws.spots(runway(), ws.mapped(far))), 2)

    def test_mapped_points_along_the_nearest_runway_or_the_wind(self):
        f = feats(((700.0, 200.0), "windsock", {}))
        self.assertAlmostEqual(points_to(rows(runway(), f)[0][4])[0], 1.0, places=3)       # (east: along the runway)
        e, n = points_to(rows(runway(), f, wind_deg=0.0)[0][4])
        self.assertAlmostEqual(n, -1.0, places=3)                                       # a north wind: the sock points south


class TileRows(unittest.TestCase):
    def test_rows_on_the_ground_in_the_group(self):
        got = rows(runway())
        self.assertEqual(len(got), 2)
        for r in got:
            self.assertEqual(len(r), 8)
            self.assertEqual(r[0], ws.GROUP)
            self.assertAlmostEqual(r[3], 12.0, delta=1.0)          # (the frame's up falls with the curvature)
            self.assertEqual((r[5], r[6], r[7]), (1.0, ws.WINDSOCK_H, 0.0))

    def test_the_sock_points_along_its_runway_the_ends_opposite_ways(self):
        low, high = sorted(rows(runway()), key=lambda r: r[1])
        self.assertAlmostEqual(points_to(low[4])[0], 1.0, places=3)         # the low end's: east, down the runway
        self.assertAlmostEqual(points_to(high[4])[0], -1.0, places=3)

    def test_yaw_follows_the_wind(self):
        for wind in (0.0, 45.0, 90.0, 180.0, 270.0, 359.0):
            toward = math.radians(wind + 180.0)                             # the compass bearing it blows towards
            for r in rows(runway(), wind_deg=wind):
                e, n = points_to(r[4])
                self.assertAlmostEqual(e, math.sin(toward), places=2)
                self.assertAlmostEqual(n, math.cos(toward), places=2)
        a, b = rows(runway(), wind_deg=30.0), rows(runway(), wind_deg=30.0 + 360.0)
        self.assertEqual([r[4] for r in a], [r[4] for r in b])
        self.assertEqual([r[4] for r in rows(runway(), wind_deg=float("nan"))], [r[4] for r in rows(runway())])

    def test_each_windsock_in_one_tile(self):
        whole = rows(runway())
        a, b = rows(runway(), box=(-500.0, -700.0, 1000.0, 700.0)), rows(runway(), box=(1000.0, -700.0, 2600.0, 700.0))
        self.assertEqual(len(a) + len(b), len(whole))
        self.assertTrue(all(r[1] < 1000.0 for r in a) and all(r[1] >= 1000.0 for r in b))
        self.assertEqual(rows(runway(), box=(5000.0, 5000.0, 5500.0, 5500.0)), [])

    def test_none_without_an_airport_or_for_a_closed_or_water_runway(self):
        self.assertEqual(ws.tile_rows(FRAME, BOX, lambda k: None, lambda k: [], ground, keys=[0]), [])
        self.assertEqual(ws.tile_rows(FRAME, BOX, lambda k: None, lambda k: None, ground, keys=[0]), [])
        self.assertEqual(rows(runway(closed=1)), [])
        self.assertEqual(rows(runway(surface="WATER")), [])


class Wind(unittest.TestCase):
    class Service:
        """weather.Service's cache, as far as wind_from reads it."""
        def __init__(self, msg):
            import threading
            self.lock, self.cache, self.clock = threading.Lock(), {}, lambda: 1000.0
            self.msg = msg

        def key(self, lat, lon, now):
            return (round(lat, 1), round(lon, 1), int(now // 900))

        def put(self, lat, lon):
            self.cache[self.key(lat, lon, self.clock())] = self.msg
            return self

    def test_the_cached_direction_or_none(self):
        svc = self.Service({"wind": {"ms": 6.0, "fromDeg": 450.0}}).put(40.0, -76.0)
        self.assertEqual(ws.wind_from(svc, 40.02, -76.01), 90.0)
        self.assertIsNone(ws.wind_from(svc, 10.0, 10.0))                     # nothing cached there
        self.assertIsNone(ws.wind_from(None, 40.0, -76.0))
        calm = self.Service({"wind": {"ms": 0.2, "fromDeg": 90.0}}).put(40.0, -76.0)
        self.assertIsNone(ws.wind_from(calm, 40.0, -76.0))
        self.assertIsNone(ws.wind_from(self.Service({"type": "weather"}).put(40.0, -76.0), 40.0, -76.0))


class Shape(unittest.TestCase):
    """The generated shape (tools/build_windsock.py) is the one the rows are made for."""

    def positions(self):
        with open(os.path.join(SHAPES, "bne_windsock.dae"), encoding="utf-8") as f:
            text = f.read()
        pos = [np.array(m.split(), np.float64).reshape(-1, 3) for m in re.findall(r'-pos-a"[^>]*>([^<]*)<', text)]
        return text, pos

    def test_a_light_mesh_as_tall_as_the_row_says_its_sock_along_plus_y(self):
        text, pos = self.positions()
        v = np.concatenate(pos)
        tris = sum(int(c) for c in re.findall(r'<triangles material="[^"]*" count="(\d+)"', text))
        self.assertLess(tris, 600)
        self.assertAlmostEqual(float(v[:, 2].max()), ws.WINDSOCK_H, delta=0.01)
        self.assertAlmostEqual(float(v[:, 2].min()), 0.0, delta=0.01)       # the origin is on the ground
        self.assertAlmostEqual(float(v[:, 1].max()), 3.6, delta=0.05)       # the sock's 3.6 m along +y
        self.assertGreater(float(v[:, 1].min()), -0.2)                      # (nothing the other way)
        self.assertAlmostEqual(float(2 * v[:, 0].max()), 0.94, delta=0.05)  # 0.9 m across the mouth (and the ring bar)

    def test_the_materials_are_there(self):
        text, _ = self.positions()
        with open(os.path.join(SHAPES, "windsock.materials.json"), encoding="utf-8") as f:
            mats = json.load(f)
        names = set(re.findall(r'target="#([^"]+)"', text))
        self.assertEqual(names, {"bne_windsock_pole", "bne_windsock_orange", "bne_windsock_white"})
        self.assertEqual(set(mats), names)


if __name__ == "__main__":
    unittest.main()
