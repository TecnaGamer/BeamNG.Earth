"""AU payphones and post boxes from OSM (streetamenities.py, in gasstations.tile_sites' records)."""
import json
import os
import re
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import geodesy  # noqa: E402
import gasstations as gs  # noqa: E402
import mapgeom as mg  # noqa: E402
import osmfuel  # noqa: E402
import streetamenities as sa  # noqa: E402
from test_gasstations import FakeMD, flat, rect  # noqa: E402

SYD = geodesy.EnuFrame(-33.8688, 151.2093)
LON = geodesy.EnuFrame(51.5072, -0.1276)
ROAD = [(np.array([(-300.0, 0.0), (300.0, 0.0)]), 5.0)]       # 10 m wide along y = 0


def row(frame, oid, x, y, amenity="telephone"):
    la, lo, _ = frame.enu_to_geodetic(x, y, 0.0)
    return {"id": oid, "amenity": amenity, "name": "", "brand": "", "bbox": (float(lo), float(la), float(lo), float(la))}


def pt(oid, x, y, kind="telephone"):
    return {"id": f"{kind}:{oid}", "kind": kind, "x": x, "y": y, "name": "", "region": "AU"}


def feats(frame, layer, rows, kind):
    geoms = []
    for _, xy, _ in rows:
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        geoms.append((kind, [[np.stack([lo, la], 1)]]))
    f = mg.build(layer, np.array([r[0] for r in rows]), geoms)
    for k in {k for r in rows for k in r[2]}:
        f.attrs[k] = np.array([r[2].get(k, "") for r in rows], object)
    return f


class DataTest(unittest.TestCase):
    def test_amenities_and_version(self):
        import mapdata
        self.assertEqual(osmfuel.AMENITIES, ("fuel", "charging_station"), "the shared rows are upstream's")
        self.assertEqual(osmfuel.STREET_AMENITIES, ("telephone", "post_box"))
        self.assertEqual(mapdata.OSM_POI_VERSION, 2, "no global cell refetches")

    def test_street_rows_only_for_australian_cells(self):
        import shutil
        import tempfile
        import mapcells
        import mapdata

        class Fake:
            def __init__(self):
                self.calls = []

            def cell(self, bounds, kind="demand", amenities=osmfuel.AMENITIES):
                self.calls.append(amenities)
                return [{"id": 1, "amenity": "telephone", "brand": "", "name": "", "building": "", "bbox": (0.0, 0.0, 0.0, 0.0)}]

        d = tempfile.mkdtemp()
        try:
            md = mapdata.MapData(d, fetcher=object())
            md.osmfuel = Fake()
            lon_key = next(iter(mapcells.cells_for_bbox(51.5, -0.12, 51.501, -0.119)))
            syd_key = next(iter(mapcells.cells_for_bbox(-33.87, 151.21, -33.869, 151.211)))
            self.assertEqual(md.osm_street_cached(lon_key), [], "London: none, never 'not fetched'")
            self.assertEqual(md.osm_street_fetch(lon_key), [])
            self.assertEqual(md.osmfuel.calls, [], "no request for a cell outside Australia")
            self.assertIsNone(md.osm_street_cached(syd_key), "Sydney: not fetched yet")
            self.assertEqual(len(md.osm_street_fetch(syd_key)), 1)
            self.assertEqual(md.osmfuel.calls, [osmfuel.STREET_AMENITIES])
            self.assertEqual(len(md.osm_street_cached(syd_key)), 1)
            self.assertNotIn("osmstreet_cells", md.osm_fuel_path(syd_key), "the fuel rows' file is its own")
            self.assertIsNone(md.osm_fuel_cached(syd_key), "the shared rows are not touched")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_fuel_and_chargers_do_not_see_phones(self):
        rows = [row(SYD, 1, 10, 10, "telephone"), row(SYD, 2, 20, 10, "post_box"), row(SYD, 3, 30, 10, "fuel"),
                row(SYD, 4, 40, 10, "charging_station")]
        self.assertEqual([p["id"] for p in gs._osm_points(SYD, [rows])], [3])
        self.assertEqual([p["id"] for p in gs._osm_points(SYD, [rows], amenity="charging_station")], [4])
        self.assertEqual([p["kind"] for p in sa.points(SYD, [rows])], ["telephone", "post_box"])


class GatingTest(unittest.TestCase):
    def test_sydney_gets_a_booth_london_nothing(self):
        self.assertEqual(len(sa.points(SYD, [[row(SYD, 1, 10, 10)]])), 1)
        self.assertEqual(sa.points(LON, [[row(LON, 1, 10, 10)]]), [])
        self.assertEqual(sa.shapes_at(51.5, -0.12), [])
        self.assertEqual(len(sa.shapes_at(-33.87, 151.2)), 2)

    def test_duplicates_merged_and_deterministic(self):
        rows = [row(SYD, 5, 10, 10), row(SYD, 3, 11.5, 10), row(SYD, 9, 30, 10), row(SYD, 4, 10.5, 10, "post_box")]
        a = sa.points(SYD, [rows])
        self.assertEqual([p["id"] for p in a], ["telephone:3", "telephone:9", "post_box:4"], "lowest id kept, kinds apart")
        self.assertEqual(a, sa.points(SYD, [list(reversed(rows))]))


class PlaceTest(unittest.TestCase):
    def test_on_the_road_moves_to_the_verge_facing_it(self):
        x, y, yaw = sa.place(pt(1, 50.0, 1.0), ROAD, [])
        self.assertAlmostEqual(y, 5.0 + sa.KERB_OFF_M, 3)
        self.assertAlmostEqual(x, 50.0, 3)
        self.assertAlmostEqual((yaw - sa.FRONT_DYAW + 180) % 360 - 180, -90.0, 3)     # front toward the road at -y

    def test_on_the_footpath_stays_and_faces_the_road(self):
        x, y, yaw = sa.place(pt(1, 50.0, -7.0), ROAD, [])
        self.assertEqual((x, y), (50.0, -7.0))
        self.assertAlmostEqual((yaw - sa.FRONT_DYAW) % 360, 90.0, 3)                  # front toward +y

    def test_inside_a_building_is_left_out(self):
        self.assertIsNone(sa.place(pt(1, 50.0, 20.0), ROAD, [rect("b", 50.0, 20.0, 20.0, 20.0)]))
        self.assertIsNotNone(sa.place(pt(1, 50.0, 20.0), ROAD, [rect("b", 80.0, 20.0, 20.0, 20.0)]))


class TileTest(unittest.TestCase):
    def md(self, frame, osm):
        rds = feats(frame, "roads", [("r-1", [(-300.0, 100.0), (600.0, 100.0)], {"class": "secondary"})], mg.LINE)
        blds = feats(frame, "buildings", [("b-1", [(40.0, 150.0), (60.0, 150.0), (60.0, 170.0), (40.0, 170.0), (40.0, 150.0)],
                                           {"class": "retail"})], mg.POLYGON)
        md = FakeMD({"roads": rds, "buildings": blds, "places": None})
        md.osm_fuel_cached = lambda key: []
        md.osm_street_cached = lambda key: osm      # (their own rows: MapData.osm_street_cached)
        return md

    def test_tile_records(self):
        osm = [row(SYD, 1, 100.0, 101.0), row(SYD, 2, 150.0, 130.0, "post_box"), row(SYD, 3, 50.0, 160.0),   # (in a building)
               row(SYD, 4, 300.0, 130.0)]                                                                    # (the next tile)
        recs, skip = gs.tile_sites(SYD, 0, 0, self.md(SYD, osm), flat)
        self.assertEqual(sorted(r["id"] for r in recs), ["amen:post_box:2", "amen:telephone:1"])
        self.assertEqual(skip, frozenset())
        ph = {r["id"]: r for r in recs}["amen:telephone:1"]
        self.assertTrue(ph["props"][0]["shape"].endswith("bne_au_payphone.dae") and ph["areas"] == [])
        self.assertGreater(ph["props"][0]["pos"][1], 100.0 + 4.0, "off the carriageway (moved from y = 101)")
        json.dumps(recs)
        self.assertEqual([r["id"] for r in gs.tile_sites(SYD, 1, 0, self.md(SYD, osm), flat)[0]], ["amen:telephone:4"])

    def test_london_tile_has_none(self):
        recs, _ = gs.tile_sites(LON, 0, 0, self.md(LON, [row(LON, 1, 100.0, 101.0)]), flat)
        self.assertEqual(recs, [])


class ModFilesTest(unittest.TestCase):
    def test_models_and_materials_exist(self):
        root = Path(__file__).resolve().parents[2] / "mod" / "beamng_earth"
        shapes = root / "levels" / "beamng_earth" / "art" / "shapes" / "beamngEarth"
        mats = json.loads((shapes / "au_street_amenities.materials.json").read_text(encoding="utf-8"))
        for model in sa.MODELS["AU"].values():
            dae = (shapes / Path(model).name).read_text(encoding="utf-8")
            for used in set(re.findall(r'symbol="([^"]+)-mat"', dae)):
                self.assertIn(used, mats, used)
        for name in ("bne_au_amen_orange", "bne_au_amen_blue", "bne_au_amen_glass", "bne_au_amen_red", "bne_au_amen_white"):
            self.assertIn(name, mats)
        lua = (root / "lua" / "ge" / "extensions" / "beamngEarth" / "gasstations.lua").read_text(encoding="utf-8")
        self.assertIn("au_street_amenities.materials.json", lua)


if __name__ == "__main__":
    unittest.main()
