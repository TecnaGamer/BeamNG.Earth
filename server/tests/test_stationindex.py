"""The station finder's index (the user, 2026-11-09: "making a new layer on the map that you can enable, one for gas
stations and one for ev's to make them easier to find"): the stations of a map cell from the lists the world is built
from, cached on disk; the map's cached cells, Nearest's rings outward, and the listings found empty left out."""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import mapcells  # noqa: E402
import mapgeom as mg  # noqa: E402
import stationindex as si  # noqa: E402
from test_gasstations import FRAME, feats  # noqa: E402

LAT, LON = 40.44, -80.00
A = mapcells.cell_of(LAT, LON)


def xy(lat, lon):
    x, y, _ = FRAME.geodetic_to_enu(lat, lon, 0.0)
    return float(x), float(y)


def place(pid, lat, lon, cat="gas_station", name="Shell", **a):
    return (pid, xy(lat, lon), {"operating_status": "open", "basic_category": cat, "name": name, **a})


def osm(oid, lat, lon, amenity="fuel", name=""):
    return {"id": oid, "amenity": amenity, "name": name, "brand": "", "bbox": (lon, lat, lon, lat)}


def east(key, k=1):
    return mapcells.CellKey(key.z, key.x + k, key.y)


def centre(key):
    b = key.bounds()                    # (lon0, lat0, lon1, lat1)
    return (b[1] + b[3]) / 2, (b[0] + b[2]) / 2


class FakeCell:
    def __init__(self, layers):
        self.layers = layers


class CellMD:
    """Map data by cell: places rows per cell, cached (has) or not; OSM rows per cell."""

    def __init__(self, places=None, cached=None, osm_rows=None, release="2026-10-22.0"):
        self.release = release
        self.places = places or {}
        self.cached = set(self.places) if cached is None else set(cached)
        self.osm = osm_rows or {}
        self.fetched = []

    def has(self, key, layer):
        return layer == "places" and key in self.cached

    def cell(self, key, layers=None, kind="demand"):
        self.fetched.append((key, kind))
        self.cached.add(key)
        rows = self.places.get(key) or []
        return FakeCell({"places": feats("places", rows, mg.POINT) if rows else None})

    def osm_fuel_cached(self, key):
        return self.osm.get(key, [])            # (None: not fetched yet, as mapdata.MapData.osm_fuel_cached)


class IndexCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bnge-si-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def index(self, md):
        return si.StationIndex(self.root, md)


class CellItemsTest(IndexCase):
    def test_cell_items_fuel_and_ev(self):
        la, lo = centre(A)
        md = CellMD({A: [place("pla-shell", la, lo),
                         place("pla-ev", la + 0.003, lo, cat="ev_charging_station", name="ChargePoint")]})
        items = sorted(si.cell_items(md, A), key=lambda i: i["kind"])
        self.assertEqual([i["kind"] for i in items], ["ev", "fuel"])
        self.assertAlmostEqual(items[1]["lat"], la, delta=1e-5)
        self.assertAlmostEqual(items[1]["lon"], lo, delta=1e-5)
        self.assertEqual(items[0]["name"], "ChargePoint")

    def test_cell_items_osm_only(self):
        la, lo = centre(A)
        md = CellMD({A: []}, osm_rows={A: [osm(1, la, lo), osm(2, la + 0.003, lo, "charging_station")]})
        self.assertEqual(sorted(i["kind"] for i in si.cell_items(md, A)), ["ev", "fuel"])


class QueryTest(IndexCase):
    def test_query_map_cached_only(self):
        la, lo = centre(A)
        lb, ob = centre(east(A))
        md = CellMD({A: [place("a", la, lo)], east(A): [place("b", lb, ob)]}, cached=[A])
        items, partial = self.index(md).query(la, lo, 5.0, ["fuel", "ev"], "map")
        self.assertFalse(partial)
        self.assertEqual(len(items), 1)
        self.assertEqual(md.fetched and {k for k, _ in md.fetched} - {A}, set(), "no uncached cell fetched")

    def test_query_map_partial(self):
        la, lo = centre(A)
        keys = [mapcells.CellKey(A.z, A.x + i, A.y + j) for i in range(-2, 3) for j in range(-2, 2)]
        md = CellMD({k: [] for k in keys})
        idx = self.index(md)
        _, partial = idx.query(la, lo, 5.0, ["fuel"], "map")
        self.assertTrue(partial)
        self.assertEqual(len(self.files()), si.MAP_BUILD_MAX)
        _, partial = idx.query(la, lo, 5.0, ["fuel"], "map")
        self.assertFalse(partial)

    def test_query_near_rings(self):
        la, lo = centre(A)
        lb, ob = centre(east(A, 2))
        md = CellMD({A: [], east(A, 2): [place("far", lb, ob)]}, cached=[A])
        idx = self.index(md)
        items, partial, calls = [], True, 0
        for calls in range(1, 20):              # (NEAR_FETCH_MAX cells a call: 8 + 16 cells to two rings out)
            items, partial = idx.query(la, lo, 25.0, ["fuel"], "near")
            if not partial:
                break
        self.assertFalse(partial)
        self.assertEqual(len(items), 1)
        self.assertIn((east(A, 2), "demand"), md.fetched)
        self.assertGreater(calls, 1, "a call downloads at most NEAR_FETCH_MAX cells")
        self.assertTrue(2.0 < items[0]["km"] < 5.0, items[0]["km"])

    def test_query_near_nothing(self):
        la, lo = centre(A)
        md = CellMD({}, cached=[])
        idx = self.index(md)
        items, partial = [], True
        for _ in range(200):
            items, partial = idx.query(la, lo, 3.0, ["fuel"], "near")
            if not partial:
                break
        self.assertFalse(partial)
        self.assertEqual(items, [])

    def test_map_answers_more_than_nearest(self):
        # the user, 2026-11-10: "does it only show a certain amount of symbols?" (a zoomed-out view of Berlin from the
        # shards: 50 icons round the centre, none towards the edges). The map's cap is MAP_MAX_ITEMS, Nearest's MAX_ITEMS.
        la, lo = centre(A)
        rows = [place(f"p{i:02d}", la + (i // 8) * 0.0015 - 0.005, lo + (i % 8) * 0.002 - 0.008) for i in range(60)]
        md = CellMD({A: rows})
        items, _ = self.index(md).query(la, lo, 5.0, ["fuel"], "map")
        self.assertEqual(len(items), 60)
        self.assertEqual([i["km"] for i in items], sorted(i["km"] for i in items))
        old = si.MAP_MAX_ITEMS
        si.MAP_MAX_ITEMS = 40
        try:
            items, _ = self.index(md).query(la, lo, 5.0, ["fuel"], "map")
        finally:
            si.MAP_MAX_ITEMS = old
        self.assertEqual(len(items), 40)
        self.assertGreater(si.MAP_MAX_ITEMS, si.MAX_ITEMS)

    def test_query_sorted_capped_dedup(self):
        la, lo = centre(A)
        rows = [place(f"p{i:02d}", la + (i // 8) * 0.0015 - 0.005, lo + (i % 8) * 0.002 - 0.008) for i in range(60)]
        md = CellMD({A: rows})
        items, _ = self.index(md).query(la, lo, 5.0, ["fuel"], "near")
        self.assertEqual(len(items), si.MAX_ITEMS)
        self.assertEqual([i["km"] for i in items], sorted(i["km"] for i in items))
        # one station seen from two cells' indexes (its pins either side of the edge): one item
        b = A.bounds()
        md = CellMD({A: [place("w", la, b[2] - 0.0001)], east(A): [place("e", la, b[2] + 0.0001)]})
        items, _ = si.StationIndex(tempfile.mkdtemp(dir=self.root), md).query(la, lo, 5.0, ["fuel"], "map")
        self.assertEqual(len(items), 1)

    def test_kinds_filter(self):
        la, lo = centre(A)
        md = CellMD({A: [place("g", la, lo), place("e", la + 0.003, lo, cat="ev_charging_station", name="EVgo")]})
        items, _ = self.index(md).query(la, lo, 5.0, ["ev"], "map")
        self.assertEqual([i["kind"] for i in items], ["ev"])

    def test_r_km_clamped(self):
        la, lo = centre(A)
        far = east(A, 20)                       # ~37 km east
        lf, of = centre(far)
        md = CellMD({A: [], far: [place("x", lf, of)]})
        items, _ = self.index(md).query(la, lo, 1000.0, ["fuel"], "map")
        self.assertEqual(items, [])

    def files(self):
        out = []
        for d, _, fs in os.walk(self.root):
            out += [os.path.join(d, f) for f in fs if f.endswith(".json")]
        return out


class MarkTest(IndexCase):
    def test_mark_hides_and_restores(self):
        la, lo = centre(A)
        md = CellMD({A: [place("a", la, lo)]})
        idx = self.index(md)
        self.assertEqual(len(idx.query(la, lo, 5.0, ["fuel"], "map")[0]), 1)
        idx.mark([("fuel", la, lo, False)])
        self.assertEqual(idx.query(la, lo, 5.0, ["fuel"], "map")[0], [])
        idx.mark([("fuel", la + 1e-5, lo, True)])           # (the tile built again: laid out this time)
        self.assertEqual(len(idx.query(la, lo, 5.0, ["fuel"], "map")[0]), 1)

    def test_rebuilt_tile_restores(self):
        # a road tile built again (another anchor, a later rule) that lays the station out: the latest report wins
        la, lo = centre(A)
        md = CellMD({A: [place("a", la, lo)]})
        idx = self.index(md)
        idx.mark([("fuel", la, lo, False)])
        idx.mark([("fuel", la, lo, True)])
        self.assertEqual(len(idx.query(la, lo, 5.0, ["fuel"], "map")[0]), 1)
        idx.mark([("fuel", la, lo, False)])
        self.assertEqual(idx.query(la, lo, 5.0, ["fuel"], "map")[0], [])

    def test_mark_kind_apart(self):
        la, lo = centre(A)
        md = CellMD({A: [place("a", la, lo)]})
        idx = self.index(md)
        idx.mark([("ev", la, lo, False)])                   # (a charger there found empty: the pumps stay)
        self.assertEqual(len(idx.query(la, lo, 5.0, ["fuel"], "map")[0]), 1)

    def test_mark_before_index(self):
        la, lo = centre(A)
        md = CellMD({A: [place("a", la, lo)]})
        idx = self.index(md)
        idx.mark([("fuel", la, lo, False)])                 # (the tile was built before the map asked)
        self.assertEqual(idx.query(la, lo, 5.0, ["fuel"], "map")[0], [])

    def test_concurrent_marks_kept(self):
        # (road tiles report from several worker threads: two reports for one cell, read-modify-write each, lost one)
        import time
        la, lo = centre(A)
        md = CellMD({A: [place("a", la, lo), place("b", la + 0.003, lo)]})
        idx = self.index(md)
        save = idx._save

        def slow(key, e):
            time.sleep(0.05)
            save(key, e)
        idx._save = slow
        ts = [threading.Thread(target=idx.mark, args=([("fuel", la + d, lo, False)],)) for d in (0.0, 0.003)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(idx.query(la, lo, 5.0, ["fuel"], "map")[0], [], "both reports kept")

    def test_marks_kept_on_disk(self):
        la, lo = centre(A)
        md = CellMD({A: [place("a", la, lo)]})
        self.index(md).mark([("fuel", la, lo, False)])
        self.assertEqual(self.index(md).query(la, lo, 5.0, ["fuel"], "map")[0], [])

    def test_osm_fetched_later_refreshes(self):
        # (Codex on #1056) a cell indexed while its OpenStreetMap stations were still being fetched: built again once
        # they are there, not kept as a complete Overture-only list
        la, lo = centre(A)
        md = CellMD({A: [place("a", la, lo)]}, osm_rows={A: None})
        idx = self.index(md)
        self.assertEqual(len(idx.query(la, lo, 5.0, ["fuel", "ev"], "map")[0]), 1)
        md.osm[A] = [osm(9, la + 0.004, lo, "charging_station", "Ionna")]
        items = idx.query(la, lo, 5.0, ["fuel", "ev"], "map")[0]
        self.assertEqual(sorted(i["kind"] for i in items), ["ev", "fuel"])

    def test_new_overture_release_rebuilds(self):
        # (Codex on #1056) the project moving to a newer Overture release: the cells' lists built again from it
        la, lo = centre(A)
        md = CellMD({A: [place("a", la, lo)]})
        self.assertEqual(len(self.index(md).query(la, lo, 5.0, ["fuel"], "map")[0]), 1)
        md2 = CellMD({A: [place("a", la, lo), place("b", la + 0.004, lo)]}, release="2026-11-19.0")
        self.assertEqual(len(self.index(md2).query(la, lo, 5.0, ["fuel"], "map")[0]), 2)

    def test_stale_version_ignored(self):
        la, lo = centre(A)
        old = os.path.join(self.root, "v0-0", f"{A.z}_{A.x}_{A.y}.json")
        os.makedirs(os.path.dirname(old))
        with open(old, "w") as f:
            json.dump({"items": [], "marks": [["fuel", la, lo, False]]}, f)
        md = CellMD({A: [place("a", la, lo)]})
        self.assertEqual(len(self.index(md).query(la, lo, 5.0, ["fuel"], "map")[0]), 1)


class FakeShards:
    """railtags.StationTags' answers: stations in a box, or None (unreachable)."""

    def __init__(self, stations, down=False):
        self.stations, self.down, self.asked = stations, down, 0

    def stations_in(self, lat0, lon0, lat1, lon1):
        self.asked += 1
        if self.down:
            return None
        return [dict(s) for s in self.stations if lat0 <= s["lat"] <= lat1 and lon0 <= s["lon"] <= lon1]


def shard_station(sid, lat, lon, kind="fuel", name="Sheetz", cap=None):
    return {"id": sid, "kind": kind, "lat": lat, "lon": lon, "name": name, "brand": "", "cap": cap}


class ShardTest(IndexCase):
    """The weekly station shards where no map data is downloaded (Bauer33333's find on #716; the user, 2026-11-09:
    the finder's map filled everywhere): the map lists them, Nearest reads them instead of downloading cells."""

    def test_map_lists_a_shard_station_in_an_uncached_cell(self):
        la, lo = centre(east(A, 3))
        md = CellMD({}, cached=[])
        md.station_tags = FakeShards([shard_station("n1", la, lo)])
        items, partial = self.index(md).query(*centre(A), 15.0, ["fuel"], "map")
        self.assertEqual([i["name"] for i in items], ["Sheetz"])
        self.assertFalse(partial)
        self.assertEqual(md.fetched, [], "no map data downloaded")

    def test_places_on_disk_replace_the_shard_list(self):
        la, lo = centre(A)
        md = CellMD({A: [place("a", la, lo, name="Sheetz")]}, cached=[])
        md.station_tags = FakeShards([shard_station("n1", la + 0.00005, lo)])
        idx = self.index(md)
        self.assertEqual(len(idx.query(la, lo, 5.0, ["fuel"], "map")[0]), 1)
        md.cached.add(A)                                    # (the player drove there: its places are on disk)
        items = idx.query(la, lo, 5.0, ["fuel"], "map")[0]
        self.assertEqual(len(items), 1, "one station, not the shard's and the places' both")
        self.assertIn((A, "demand"), md.fetched, "built from the places once they are there")

    def test_near_reads_shards_not_cells(self):
        la, lo = centre(east(A, 8))                          # (~15 km east)
        md = CellMD({}, cached=[])
        md.station_tags = FakeShards([shard_station("n1", la, lo, kind="ev", name="Ionna", cap=6)])
        items, partial = [], True
        for _ in range(20):
            items, partial = self.index(md).query(*centre(A), 25.0, ["ev"], "near")
            if not partial:
                break
        self.assertFalse(partial)
        self.assertEqual([i["name"] for i in items], ["Ionna"])
        self.assertEqual(md.fetched, [], "no map data downloaded")

    def test_unreachable_shards_download_as_before(self):
        la, lo = centre(east(A, 2))
        md = CellMD({A: [], east(A, 2): [place("far", la, lo)]}, cached=[A])
        md.station_tags = FakeShards([], down=True)
        idx = self.index(md)
        items, partial = [], True
        for _ in range(20):
            items, partial = idx.query(*centre(A), 25.0, ["fuel"], "near")
            if not partial:
                break
        self.assertEqual(len(items), 1)
        self.assertIn((east(A, 2), "demand"), md.fetched)

    def test_a_shard_station_reported_empty_is_hidden(self):
        la, lo = centre(east(A, 3))
        md = CellMD({}, cached=[])
        md.station_tags = FakeShards([shard_station("n1", la, lo)])
        idx = self.index(md)
        self.assertEqual(len(idx.query(*centre(A), 15.0, ["fuel"], "map")[0]), 1)
        idx.mark([("fuel", la, lo, False)])
        self.assertEqual(idx.query(*centre(A), 15.0, ["fuel"], "map")[0], [])
        # (after a restart: the report kept, the list read from the shards again, not from the cell's file)
        md.station_tags = FakeShards([shard_station("n1", la, lo), shard_station("n2", la + 0.004, lo)])
        self.assertEqual([i["name"] for i in self.index(md).query(*centre(A), 15.0, ["fuel"], "map")[0]], ["Sheetz"])


import threading  # noqa: E402

import test_server  # noqa: E402


class FakeIndex:
    def __init__(self):
        self.calls = []

    def query(self, lat, lon, r_km, kinds, mode):
        self.calls.append((lat, lon, r_km, tuple(kinds), mode, threading.current_thread().name))
        return [{"lat": lat, "lon": lon, "name": "Sheetz", "brand": "Sheetz", "kind": "fuel", "km": 0.4}], mode == "near"


class StationsMessageTest(test_server.ServerHarness):
    """stations through a session: stations_result with its id, the index's items and partial, the query off the
    event loop; without an index an empty answer that says so."""

    def test_stations_message(self):
        self.hello()
        self.server.stations = None
        self.c.send({"type": "stations", "id": 1, "lat": 40.44, "lon": -80.0, "r_km": 5.0, "kinds": ["fuel"],
                     "mode": "map"})
        m, _ = self.c.until(lambda m: m["type"] == "stations_result", timeout=10)
        self.assertEqual((m["id"], m["items"], m["partial"], m.get("error")), (1, [], False, "no stations"))
        fake = self.server.stations = FakeIndex()
        self.server.gas_stations = True
        self.c.send({"type": "stations", "id": 2, "lat": 40.44, "lon": -80.0, "r_km": 25.0, "kinds": ["fuel", "ev"],
                     "mode": "near"})
        m, _ = self.c.until(lambda m: m["type"] == "stations_result" and m["id"] == 2, timeout=10)
        self.assertEqual(m["items"][0]["name"], "Sheetz")
        self.assertTrue(m["partial"])
        lat, lon, r_km, kinds, mode, thread = fake.calls[0]
        self.assertEqual((lat, lon, r_km, kinds, mode), (40.44, -80.0, 25.0, ("fuel", "ev"), "near"))
        self.assertNotEqual(thread, threading.main_thread().name, "the query ran off the event loop")


if __name__ == "__main__":
    unittest.main()
