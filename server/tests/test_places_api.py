"""places.py and earthd's 'places': the modding API's places round a point (api.places / api.buildings in the mod).
The request's checks and caps; categories exact, by prefix and by the map's groups; nearest first, named only, a place
in two cells once; paging by cursor that neither repeats nor skips when more cells come; footprints' outer rings,
thinned and capped. earthd answers from the cells on disk (no fetch) at once, once more after the missing cells came
in the background ("complete", "pending"), never touches the map labels' counter, and says why when it cannot.
A cachedOnly request reads only the cells on disk, at a radius up to RADIUS_WIDE_MAX_M: it must name categories past
RADIUS_MAX_M, never fetches and never follows up, and says "truncated" where its reach was cut back to
CACHED_CELLS_MAX cells."""
import asyncio
import math
import sys
import types
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mapcells  # noqa: E402
import places  # noqa: E402

LAT, LON = 39.2847, -76.5925
M_LAT = 1.0 / 111320.0          # degrees of latitude in a metre


def place_cell(rows, lat=LAT, lon=LON):
    """A places layer of rows (id, name, category, metres north of (lat, lon), confidence, status)."""
    n = len(rows)
    return types.SimpleNamespace(
        ids=np.array([r[0] for r in rows]),
        attrs={"name": np.array([r[1] for r in rows]), "basic_category": np.array([r[2] for r in rows]),
               "taxonomy": np.array([""] * n), "confidence": np.array([r[4] for r in rows], np.float64),
               "operating_status": np.array([r[5] for r in rows]),
               "cx": np.full(n, lon), "cy": np.array([lat + r[3] * M_LAT for r in rows])})


def building_cell(rings, heights=None):
    """A buildings layer: one polygon a ring each ([(lon, lat), ...], closed), centres at their first vertex."""
    coords, coord_off = [], [0]
    for ring in rings:
        coords.extend(ring)
        coord_off.append(len(coords))
    n = len(rings)
    return types.SimpleNamespace(
        ids=np.array([f"b{i}" for i in range(n)]), lonlat=np.array(coords, np.float64),
        part_off=np.arange(n + 1), ring_off=np.arange(n + 1), coord_off=np.array(coord_off),
        attrs={"cx": np.array([r[0][0] for r in rings]), "cy": np.array([r[0][1] for r in rings]),
               "height": np.array(heights if heights is not None else [np.nan] * n, np.float32),
               "num_floors": np.full(n, np.nan, np.float32), "name": np.array([""] * n),
               "class": np.array(["residential"] * n)})


class RequestTest(unittest.TestCase):
    def test_caps_and_defaults(self):
        req, why = places.request({"lat": LAT, "lon": LON, "radius": 1e6, "limit": 1e6})
        self.assertIsNone(why)
        self.assertEqual(req["radius"], places.RADIUS_MAX_M)
        self.assertEqual(req["limit"], places.LIMIT_MAX)
        self.assertFalse(req["buildings"])
        req, _ = places.request({"lat": LAT, "lon": LON})
        self.assertEqual((req["radius"], req["limit"], req["categories"]),
                         (places.RADIUS_DEFAULT_M, places.LIMIT_DEFAULT, []))
        self.assertEqual(places.request({"lat": LAT, "lon": LON, "categories": {}})[0]["categories"], [])

    def test_cached_only_wide_radius(self):
        plain = places.request({"lat": LAT, "lon": LON, "radius": 25000})[0]
        self.assertEqual(plain["radius"], places.RADIUS_MAX_M)          # (as today: clamped, not refused)
        self.assertFalse(plain["cached_only"])
        self.assertFalse(plain["truncated"])
        req, why = places.request({"lat": LAT, "lon": LON, "radius": 25000, "cachedOnly": True})
        self.assertIsNone(req)                                          # (a wide search is for sparse kinds)
        self.assertIn("categories", why)
        req, why = places.request({"lat": LAT, "lon": LON, "radius": 25000, "cachedOnly": True,
                                   "categories": ["car_dealer"]})
        self.assertIsNone(why)
        self.assertEqual((req["radius"], req["cached_only"], req["truncated"]), (25000.0, True, False))
        wide = places.request({"lat": LAT, "lon": LON, "radius": 1e6, "cachedOnly": True,
                               "categories": ["car_dealer"]})[0]
        self.assertEqual(wide["radius"], places.RADIUS_WIDE_MAX_M)
        # cachedOnly within RADIUS_MAX_M needs no categories (the cells it reads are the ordinary ones)
        self.assertIsNone(places.request({"lat": LAT, "lon": LON, "cachedOnly": True})[1])

    def test_cached_reach_cut_back_to_the_cell_cap(self):
        # a z14 cell shrinks with cos(lat): 25 km fits round Baltimore, not round Tromso (69.65 N)
        self.assertEqual(places.cached_reach(LAT, LON, places.RADIUS_WIDE_MAX_M), (places.RADIUS_WIDE_MAX_M, False))
        for lat in (0.0, LAT, 48.14, 61.8, 62.31, 69.6492, 78.2232, 85.0, mapcells.MAX_LAT, 90.0):
            reach, cut = places.cached_reach(lat, 0.0, places.RADIUS_WIDE_MAX_M)
            self.assertLessEqual(len(places.near_cells(lat, 0.0, reach)), places.CACHED_CELLS_MAX, lat)
            self.assertGreater(reach, places.RADIUS_MAX_M, lat)         # (never shorter than an ordinary request)
        req, _ = places.request({"lat": 69.6492, "lon": 18.9553, "radius": 25000, "cachedOnly": True,
                                 "categories": ["car_dealer"]})
        self.assertTrue(req["truncated"])
        self.assertLess(req["radius"], places.RADIUS_WIDE_MAX_M)

    def test_the_reach_read_never_passes_the_cell_cap_at_any_latitude(self):
        """Swept over the globe: the box cached_reach hands near_cells is never over CACHED_CELLS_MAX, so
        mapcells.cells_for_bbox is never asked for more than it will count out (it raises past its own 4096, which
        an add-on would hear as a bare "ValueError")."""
        worst = 0
        for i in range(-180, 181):                      # (half a degree a step, plus the awkward ones below)
            for lat in (i / 2.0,):
                for lon in (0.0, 179.999, -180.0):
                    reach, _ = places.cached_reach(lat, lon, places.RADIUS_WIDE_MAX_M)
                    worst = max(worst, len(places.near_cells(lat, lon, reach)))
        for lat in (mapcells.MAX_LAT, -mapcells.MAX_LAT, 85.262, 62.317, 61.8, 21.7):
            reach, _ = places.cached_reach(lat, 0.0, places.RADIUS_WIDE_MAX_M)
            worst = max(worst, len(places.near_cells(lat, 0.0, reach)))
        self.assertLessEqual(worst, places.CACHED_CELLS_MAX, worst)
        self.assertGreater(worst, 1900)                 # (the cap is really approached, not merely never reached)

    def test_a_radius_whose_box_fits_is_never_cut(self):
        """The box is counted, not estimated: round 61.8-62.3 N an estimate shortened the reach by metres over a cell
        set the full radius would have read exactly, and said truncated (and complete False) over it."""
        for lat in (61.6159, 61.8, 62.0, 62.31, 60.0, LAT):
            reach, cut = places.cached_reach(lat, 0.0, places.RADIUS_WIDE_MAX_M)
            if not cut:
                self.assertEqual(reach, places.RADIUS_WIDE_MAX_M, lat)  # (read whole, not shaved)
                continue
            # cut: the cells inside the radius asked for really are more than the cap, so some were left unread
            dla = places.RADIUS_WIDE_MAX_M / 111320.0
            dlo = dla / max(0.05, math.cos(math.radians(lat)))
            asked = mapcells.cells_for_bbox(lat - dla, -dlo, lat + dla, dlo, max_cells=1 << 20)
            got = places.near_cells(lat, 0.0, reach)
            self.assertGreater(len(asked), places.CACHED_CELLS_MAX, lat)
            self.assertLess(len(got), len(asked), lat)
            self.assertTrue(set(got) <= set(asked), lat)                 # (concentric: a sub-box, not a shifted one)
        req, why = places.request({"lat": 61.8, "lon": 0.0, "radius": 25000, "cachedOnly": True,
                                   "categories": ["car_dealer"]})
        self.assertIsNone(why)
        self.assertFalse(req["truncated"])
        self.assertEqual(req["radius"], 25000.0)

    # the three below, and the two paging cases in EarthdPlacesTest marked the same way, came from an independent
    # review of this change rather than from writing it: the gate's exact boundary, a flag that is not a bool, a
    # categories list that is only blanks, and a cell landing on disk between two pages
    def test_the_categories_gate_sits_exactly_at_radius_max(self):     # (from the review)
        at = places.request({"lat": LAT, "lon": LON, "radius": places.RADIUS_MAX_M, "cachedOnly": True})
        self.assertIsNone(at[1])                                        # (exactly 3000 m needs no categories)
        over = places.request({"lat": LAT, "lon": LON, "radius": places.RADIUS_MAX_M + 0.001, "cachedOnly": True})
        self.assertIsNone(over[0])
        self.assertIn("categories", over[1])

    def test_a_non_boolean_cached_only_is_not_cached_only(self):        # (from the review)
        for v in ("yes", 1, [1], {}):
            req, why = places.request({"lat": LAT, "lon": LON, "radius": 25000, "cachedOnly": v})
            self.assertIsNone(why, v)
            self.assertFalse(req["cached_only"], v)
            self.assertEqual(req["radius"], places.RADIUS_MAX_M, v)     # (clamped as today, not widened)

    def test_a_blank_category_list_does_not_open_the_gate(self):        # (from the review)
        req, why = places.request({"lat": LAT, "lon": LON, "radius": 25000, "cachedOnly": True,
                                   "categories": ["  ", ""]})
        self.assertIsNone(req)
        self.assertIn("categories", why)

    def test_refused(self):
        for msg in ({"lat": 91, "lon": 0}, {"lat": "x", "lon": 0}, {"lon": 0}, {"lat": float("nan"), "lon": 0},
                    {"lat": 0, "lon": 0, "categories": "food"}, {"lat": 0, "lon": 0, "categories": [1]},
                    {"lat": 0, "lon": 0, "categories": ["a"] * (places.CATEGORIES_MAX + 1)},
                    {"lat": 0, "lon": 0, "cursor": "nonsense"}, {"lat": 0, "lon": 0, "radius": "far"}):
            req, why = places.request(msg)
            self.assertIsNone(req, msg)
            self.assertTrue(why, msg)


class MatchTest(unittest.TestCase):
    def test_exact_prefix_group(self):
        m = places.matcher(["gas_station", "car_*", "group:food"])
        self.assertTrue(m("gas_station", ""))
        self.assertTrue(m("convenience_store", "gas_station"))      # (the taxonomy's value counts too)
        self.assertTrue(m("car_dealer", ""))
        self.assertTrue(m("pizza_restaurant", ""))                  # (the map's food group)
        self.assertFalse(m("carpet_store", ""))
        self.assertFalse(m("legal_service", ""))
        self.assertTrue(places.matcher([])("legal_service", ""))    # (none asked: every kind, not only the map's)


class RowsTest(unittest.TestCase):
    def test_nearest_first_named_once(self):
        a = place_cell([("p3", "Far Diner", "diner", 900, 0.9, "open"), ("p1", "Near Bank", "bank", 100, 0.6, ""),
                        ("p2", "", "park", 50, 0.9, "open"), ("p4", "Out Of Reach", "bank", 5000, 0.9, "open")])
        b = place_cell([("p1", "Near Bank", "bank", 100, 0.6, "")])        # (the same place in the next cell)
        keyed = places.place_rows([a, None, b], LAT, LON, 1000)
        rows = [r for _, r in keyed]
        self.assertEqual([r["id"] for r in rows], ["p1", "p3"])
        self.assertEqual(rows[0]["category"], "bank")                      # (every kind: a bank is not on the map)
        self.assertEqual(rows[0]["open"], "")
        self.assertAlmostEqual(rows[0]["confidence"], 0.6)
        self.assertEqual(set(rows[0]), {"id", "name", "category", "lat", "lon", "confidence", "open"})
        self.assertEqual([r["id"] for _, r in places.place_rows([a], LAT, LON, 1000, min_conf=0.75)], ["p3"])

    def test_pages_neither_repeat_nor_skip(self):
        cell = place_cell([(f"p{i:02d}", f"Place {i}", "bank", 10 * i + 1, 0.9, "open") for i in range(25)])
        keyed = places.place_rows([cell], LAT, LON, 3000)
        got, cursor = [], None
        while True:
            rows, cursor = places.page(keyed, cursor, 10)
            got += [r["id"] for r in rows]
            if cursor is None:
                break
        self.assertEqual(got, [f"p{i:02d}" for i in range(25)])
        # more cells came between pages: the second page goes on after the first page's last row
        first, cursor = places.page(keyed, None, 10)
        more = place_cell([("q1", "Nearer Than All", "bank", 0.5, 0.9, "open"),
                           ("q2", "In Page Two", "bank", 151.5, 0.9, "open")])
        keyed2 = places.place_rows([cell, more], LAT, LON, 3000)
        second, _ = places.page(keyed2, cursor, 10)
        self.assertEqual(second[0]["id"], "p10")
        self.assertIn("q2", [r["id"] for r in second])
        self.assertNotIn("q1", [r["id"] for r in second])


class BuildingsTest(unittest.TestCase):
    def square(self, north_m, side_m=10.0, n=4):
        la0, lo0 = LAT + north_m * M_LAT, LON
        ring = [(lo0 + side_m * M_LAT * np.cos(2 * np.pi * k / n), la0 + side_m * M_LAT * np.sin(2 * np.pi * k / n))
                for k in range(n)]
        return ring + [ring[0]]

    def test_outlines_nearest_first_within_reach(self):
        f = building_cell([self.square(300), self.square(20), self.square(places.BUILDINGS_RADIUS_MAX_M + 200)],
                          heights=[12.0, np.nan, 30.0])
        rows = places.building_rows([f], LAT, LON, 3000)
        self.assertEqual([r["id"] for r in rows], ["b1", "b0"])            # (the far one past the buildings' reach)
        self.assertIsNone(rows[0]["height"])
        self.assertEqual(rows[1]["height"], 12.0)
        self.assertEqual(len(rows[1]["outline"]), 5)
        la, lo = rows[1]["outline"][0]
        self.assertAlmostEqual(la, LAT + 300 * M_LAT, places=5)            # ([lat, lon], not GeoJSON's lon first)
        self.assertAlmostEqual(lo, LON + 10 * M_LAT, places=5)

    def test_long_rings_thinned_and_count_capped(self):
        f = building_cell([self.square(5, n=1000)] + [self.square(10 + i) for i in range(places.BUILDINGS_MAX + 20)])
        rows = places.building_rows([f], LAT, LON, 500)
        self.assertEqual(len(rows), places.BUILDINGS_MAX)
        self.assertEqual(len(rows[0]["outline"]), places.OUTLINE_MAX_POINTS)
        self.assertEqual(rows[0]["outline"][0], rows[0]["outline"][-1])     # (still closed)


class FakeMapData:
    """mapdata.MapData's has / cell over places cells: on disk or not; a fetch (kind "background") puts it there.
    "fetched" is the keys a cell() asked for that were not on disk: what a cachedOnly request must never do."""

    def __init__(self, layers, missing=()):
        self.layers, self.missing, self.kinds, self.fetched = layers, set(missing), [], []

    def has(self, key, layer):
        return str(key) not in self.missing

    def cell(self, key, layers, kind="demand"):
        self.kinds.append(kind)
        if str(key) in self.missing:
            self.fetched.append(str(key))
        self.missing.discard(str(key))
        return types.SimpleNamespace(layers={lr: self.layers.get(lr, {}).get(str(key)) for lr in layers})

    def layer_unkept(self, key, layer, kind="background"):
        """MapData.layer_unkept: one layer read and not kept (a cachedOnly request's places)."""
        self.kinds.append("unkept")
        if str(key) in self.missing:
            self.fetched.append(str(key))
        return self.layers.get(layer, {}).get(str(key))


class EarthdPlacesTest(unittest.TestCase):
    def run_places(self, msgs, md, busy=0):
        import earthd
        sent = []

        async def run():
            from concurrent.futures import ThreadPoolExecutor
            pool = ThreadPoolExecutor(1)
            s = types.SimpleNamespace(mapdata=md, api_pool=pool, place_pool=None)
            conn = types.SimpleNamespace(s=s, send=sent.append, _map_labels_seq=7, _places_followups=busy)
            conn._place_rows = lambda *a: earthd.Connection._place_rows(conn, *a)
            old = earthd.WHERE_PLACE_RETRY_S
            earthd.WHERE_PLACE_RETRY_S = 0.0
            try:
                await asyncio.gather(*(earthd.Connection.places(conn, m) for m in msgs))
            finally:
                earthd.WHERE_PLACE_RETRY_S = old
                pool.shutdown()
            return conn
        conn = asyncio.run(run())
        return sent, conn

    def keys(self, radius=1000):
        return [str(k) for k in places.near_cells(LAT, LON, radius)]

    def test_complete_at_once_from_disk(self):
        keys = self.keys()
        md = FakeMapData({"places": {keys[0]: place_cell([("p1", "Joe's", "restaurant", 100, 0.9, "open")])}})
        sent, conn = self.run_places([{"id": 1, "lat": LAT, "lon": LON, "radius": 1000}], md)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["type"], "places_result")
        self.assertTrue(sent[0]["complete"])
        self.assertFalse(sent[0]["pending"])
        self.assertEqual([r["name"] for r in sent[0]["places"]], ["Joe's"])
        self.assertNotIn("buildings", sent[0])
        self.assertNotIn("truncated", sent[0])               # (nothing new on the wire without cachedOnly)
        self.assertEqual(set(md.kinds), {"background"})
        self.assertEqual(conn._map_labels_seq, 7)                    # (the map's labels' answers not cancelled)

    def test_missing_cells_second_answer(self):
        keys = self.keys()
        md = FakeMapData({"places": {keys[0]: place_cell([("p1", "Joe's", "restaurant", 100, 0.9, "open")]),
                                     keys[-1]: place_cell([("p2", "Later Cafe", "cafe", 200, 0.9, "open")])}},
                         missing=[keys[-1]])
        sent, conn = self.run_places([{"id": 2, "lat": LAT, "lon": LON, "radius": 1000}], md)
        self.assertEqual([(m["complete"], m["pending"]) for m in sent], [(False, True), (True, False)])
        self.assertEqual([r["id"] for r in sent[0]["places"]], ["p1"])     # (only what was on disk: no download)
        self.assertEqual([r["id"] for r in sent[1]["places"]], ["p1", "p2"])
        self.assertEqual(conn._places_followups, 0)
        self.assertEqual(conn._map_labels_seq, 7)

    def test_buildings_and_paging(self):
        keys = self.keys()
        rows = [(f"p{i}", f"Shop {i}", "bank", 10 * i + 5, 0.9, "open") for i in range(5)]
        ring = [(LON, LAT), (LON + 1e-4, LAT), (LON + 1e-4, LAT + 1e-4), (LON, LAT)]
        md = FakeMapData({"places": {keys[0]: place_cell(rows)}, "buildings": {keys[0]: building_cell([ring])}})
        sent, _ = self.run_places([{"id": 3, "lat": LAT, "lon": LON, "limit": 2, "buildings": True}], md)
        self.assertEqual([r["id"] for r in sent[0]["places"]], ["p0", "p1"])
        self.assertEqual(len(sent[0]["buildings"]), 1)
        sent2, _ = self.run_places([{"id": 4, "lat": LAT, "lon": LON, "limit": 2, "cursor": sent[0]["next"]}], md)
        self.assertEqual([r["id"] for r in sent2[0]["places"]], ["p2", "p3"])

    def test_building_with_no_rings_is_skipped(self):
        keys = self.keys()
        ring = [(LON, LAT), (LON + 1e-4, LAT), (LON + 1e-4, LAT + 1e-4), (LON, LAT)]
        cell = building_cell([ring, ring])
        cell.ring_off = np.array([0, 0, 1])          # (b0's part carries no ring, as a damaged cell writes it)
        md = FakeMapData({"places": {keys[0]: place_cell([("p0", "Shop", "bank", 5, 0.9, "open")])},
                          "buildings": {keys[0]: cell}})
        sent, _ = self.run_places([{"id": 31, "lat": LAT, "lon": LON, "buildings": True}], md)
        self.assertEqual([b["id"] for b in sent[0]["buildings"]], ["b1"])

    def test_cached_only_answers_once_and_never_fetches(self):
        keys = self.keys()
        md = FakeMapData({"places": {keys[0]: place_cell([("p1", "Joe's", "car_dealer", 100, 0.9, "open")])}},
                         missing=keys[1:])
        sent, conn = self.run_places([{"id": 20, "lat": LAT, "lon": LON, "radius": 1000, "cachedOnly": True}], md)
        self.assertEqual([(m["complete"], m["pending"]) for m in sent], [(False, False)])   # (final, incomplete)
        self.assertEqual([r["id"] for r in sent[0]["places"]], ["p1"])
        self.assertEqual(md.fetched, [])                               # (nothing downloaded for it)
        self.assertNotIn("truncated", sent[0])
        self.assertEqual(conn._places_followups, 0)                    # (no follow-up was ever scheduled)
        self.assertEqual(conn._map_labels_seq, 7)
        self.assertEqual(set(md.kinds), {"unkept"})     # (its places read one at a time, none kept: layer_unkept)

    def test_cached_only_never_fetches_even_when_asked_to(self):
        import earthd
        keys = self.keys()
        md = FakeMapData({"places": {keys[0]: place_cell([("p1", "Joe's", "car_dealer", 100, 0.9, "open")])}},
                         missing=keys[1:])
        req, why = places.request({"lat": LAT, "lon": LON, "radius": 1000, "cachedOnly": True})
        self.assertIsNone(why)
        conn = types.SimpleNamespace(s=types.SimpleNamespace(mapdata=md))
        keyed, blds, complete = earthd.Connection._place_rows(conn, req, True)    # (the fetch branch, refused)
        self.assertEqual(md.fetched, [])
        self.assertFalse(complete)
        self.assertEqual([r["id"] for _, r in keyed], ["p1"])
        self.assertIsNone(blds)

    def test_cached_only_wide_truncates_and_pages(self):
        lat, lon = 69.6492, 18.9553                                    # Tromso: a small z14 cell, so the cap bites
        keys = [str(k) for k in places.near_cells(lat, lon, places.cached_reach(lat, lon, 25000.0)[0])]
        rows = [(f"p{i}", f"Dealer {i}", "car_dealer", 1000 * i + 500, 0.9, "open") for i in range(5)]
        md = FakeMapData({"places": {keys[0]: place_cell(rows, lat, lon)}}, missing=keys[1:])
        ask = {"lat": lat, "lon": lon, "radius": 25000, "cachedOnly": True, "categories": ["car_dealer"], "limit": 2}
        sent, _ = self.run_places([dict(ask, id=21)], md)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]["truncated"])
        self.assertFalse(sent[0]["complete"])                          # (the cells past the cap were not read)
        self.assertFalse(sent[0]["pending"])
        self.assertEqual([r["id"] for r in sent[0]["places"]], ["p0", "p1"])
        self.assertEqual(md.fetched, [])
        # the cut is the same on every page, so the cursor goes on where the first page stopped
        sent2, _ = self.run_places([dict(ask, id=22, cursor=sent[0]["next"])], md)
        self.assertEqual([r["id"] for r in sent2[0]["places"]], ["p2", "p3"])
        self.assertTrue(sent2[0]["truncated"])

    def test_cached_only_keeps_the_buildings_reach(self):
        wide, close = self.keys(places.RADIUS_WIDE_MAX_M), self.keys(places.BUILDINGS_RADIUS_MAX_M)
        outside = next(k for k in wide if k not in close)

        def ring(north_m):
            la = LAT + north_m * M_LAT
            return [(LON, la), (LON + 1e-4, la), (LON + 1e-4, la + 1e-4), (LON, la)]
        md = FakeMapData({"places": {wide[0]: place_cell([("p0", "Dealer", "car_dealer", 20, 0.9, "open")])},
                          "buildings": {close[0]: building_cell([ring(places.BUILDINGS_RADIUS_MAX_M + 2000), ring(0)]),
                                        outside: building_cell([ring(0)])}})
        sent, _ = self.run_places([{"id": 23, "lat": LAT, "lon": LON, "radius": 25000, "cachedOnly": True,
                                    "categories": ["car_dealer"], "buildings": True}], md)
        self.assertEqual([r["id"] for r in sent[0]["places"]], ["p0"])           # (the wide reach, for the places)
        # b0 is past BUILDINGS_RADIUS_MAX_M, and the cell outside it was never read: the footprints keep their 500 m
        self.assertEqual([b["id"] for b in sent[0]["buildings"]], ["b1"])

    def test_a_nearer_place_arriving_between_pages_is_behind_the_cursor(self):      # (from the review)
        keys = self.keys(3000.0)
        md = FakeMapData({"places": {keys[0]: place_cell(
            [(f"p{i}", f"D{i}", "car_dealer", 500 * (i + 1), 0.9, "open") for i in range(6)])}}, missing=keys[1:])
        ask = {"lat": LAT, "lon": LON, "radius": 3000, "cachedOnly": True, "categories": ["car_dealer"], "limit": 2}
        p1, _ = self.run_places([dict(ask, id=24)], md)
        self.assertEqual([r["id"] for r in p1[0]["places"]], ["p0", "p1"])
        # a cell lands on disk between the pages, with one place nearer than the cursor and one past it
        md.layers["places"][keys[1]] = place_cell([("n0", "New near", "car_dealer", 100, 0.9, "open"),
                                                   ("n9", "New far", "car_dealer", 2600, 0.9, "open")])
        md.missing.discard(keys[1])
        seen, cursor, guard = [], p1[0]["next"], 0
        while cursor is not None and guard < 10:
            guard += 1
            pn, _ = self.run_places([dict(ask, id=24 + guard, cursor=cursor)], md)
            seen += [r["id"] for r in pn[0]["places"]]
            cursor = pn[0].get("next")
        self.assertEqual(seen, ["p2", "p3", "p4", "n9", "p5"])          # (n9 at 2600 m sorts in before p5 at 3000)
        self.assertNotIn("n0", seen)                                    # (nearer than the cursor: behind it)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(md.fetched, [])

    def test_the_wide_cut_is_the_same_on_every_page_while_cells_arrive(self):       # (from the review)
        lat, lon = 69.6492, 18.9553
        reach = places.cached_reach(lat, lon, 25000.0)[0]
        keys = [str(k) for k in places.near_cells(lat, lon, reach)]
        md = FakeMapData({"places": {keys[0]: place_cell(
            [(f"p{i}", f"D{i}", "car_dealer", 1000 * i + 500, 0.9, "open") for i in range(4)], lat, lon)}},
            missing=keys[1:])
        ask = {"lat": lat, "lon": lon, "radius": 25000, "cachedOnly": True, "categories": ["car_dealer"], "limit": 2}
        p1, _ = self.run_places([dict(ask, id=30)], md)
        self.assertTrue(p1[0]["truncated"])
        # a cell arrives holding a place past the cut reach: the cut does not move, so it is on no later page
        md.layers["places"][keys[1]] = place_cell([("beyond", "Beyond", "car_dealer", int(reach) + 500, 0.9, "open")],
                                                  lat, lon)
        md.missing.discard(keys[1])
        p2, _ = self.run_places([dict(ask, id=31, cursor=p1[0]["next"])], md)
        self.assertTrue(p2[0]["truncated"])
        self.assertEqual([r["id"] for r in p2[0]["places"]], ["p2", "p3"])
        self.assertNotIn("beyond", [r["id"] for r in p2[0]["places"]])
        self.assertEqual(md.fetched, [])

    def test_says_why(self):
        sent, _ = self.run_places([{"id": 5, "lat": 95, "lon": 0}], FakeMapData({}))
        self.assertEqual(sent, [{"type": "places_result", "id": 5, "places": [], "complete": False, "pending": False,
                                 "error": "bad coordinates"}])
        sent, _ = self.run_places([{"id": 6, "lat": LAT, "lon": LON}], None)
        self.assertFalse(sent[0]["complete"])
        self.assertIn("no map data", sent[0]["error"])

    def test_follow_ups_capped(self):
        import earthd
        md = FakeMapData({}, missing=self.keys())
        sent, conn = self.run_places([{"id": 10, "lat": LAT, "lon": LON}], md, busy=earthd.PLACES_FOLLOWUPS)
        self.assertEqual([(m["complete"], m["pending"]) for m in sent], [(False, False)])   # (the last answer)
        self.assertEqual(md.kinds, [])                                     # (nothing fetched for it)
        self.assertEqual(conn._places_followups, earthd.PLACES_FOLLOWUPS)

if __name__ == "__main__":
    unittest.main()
