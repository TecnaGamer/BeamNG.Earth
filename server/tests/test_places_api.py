"""places.py and earthd's 'places': the modding API's places round a point (api.places / api.buildings in the mod).
The request's checks and caps; categories exact, by prefix and by the map's groups; nearest first, named only, a place
in two cells once; paging by cursor that neither repeats nor skips when more cells come; footprints' outer rings,
thinned and capped. earthd answers from the cells on disk (no fetch) at once, once more after the missing cells came
in the background ("complete", "pending"), never touches the map labels' counter, and says why when it cannot."""
import asyncio
import sys
import types
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import places  # noqa: E402

LAT, LON = 39.2847, -76.5925
M_LAT = 1.0 / 111320.0          # degrees of latitude in a metre


def place_cell(rows):
    """A places layer of rows (id, name, category, metres north of (LAT, LON), confidence, status)."""
    n = len(rows)
    return types.SimpleNamespace(
        ids=np.array([r[0] for r in rows]),
        attrs={"name": np.array([r[1] for r in rows]), "basic_category": np.array([r[2] for r in rows]),
               "taxonomy": np.array([""] * n), "confidence": np.array([r[4] for r in rows], np.float64),
               "operating_status": np.array([r[5] for r in rows]),
               "cx": np.full(n, LON), "cy": np.array([LAT + r[3] * M_LAT for r in rows])})


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
    """mapdata.MapData's has / cell over places cells: on disk or not; a fetch (kind "background") puts it there."""

    def __init__(self, layers, missing=()):
        self.layers, self.missing, self.kinds = layers, set(missing), []

    def has(self, key, layer):
        return str(key) not in self.missing

    def cell(self, key, layers, kind="demand"):
        self.kinds.append(kind)
        self.missing.discard(str(key))
        return types.SimpleNamespace(layers={lr: self.layers.get(lr, {}).get(str(key)) for lr in layers})


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
