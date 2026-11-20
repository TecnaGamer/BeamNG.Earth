"""bdtopo: France's measured buildings (IGN BD TOPO, ported from PR #456) fill what the map leaves out of the
footprint their point stands in; the map's own values stay, nothing is asked for outside France, the downloads run in
the background and a tile never waits past its bound; plan() draws a measured roof rise."""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bdtopo  # noqa: E402
import buildings as bld  # noqa: E402
import mapcells  # noqa: E402
import mapgeom  # noqa: E402
import revisit  # noqa: E402

AIX = (43.5297, 5.4474)


def square(lon, lat, w=0.0001, d=0.0001):
    return [[lon, lat], [lon + w, lat], [lon + w, lat + d], [lon, lat + d], [lon, lat]]


def feature(ring, **props):
    p = {"hauteur": None, "altitude_minimale_toit": None, "altitude_maximale_toit": None, "nombre_d_etages": None,
         "nombre_de_logements": None, "materiaux_des_murs": None, "materiaux_de_la_toiture": None,
         "usage_1": "Indifférencié", "nature": "Indifférenciée", "construction_legere": False}
    p.update(props)
    geom = {"type": "MultiPolygon", "coordinates": [[[[x, y, 100.0] for x, y in ring]]]}
    return {"type": "Feature", "geometry": geom, "properties": p}


def layer(rings, **cols):
    """A buildings Features of rings (lon/lat lists) with attrs cols (lists)."""
    co = np.cumsum([0] + [len(r) for r in rings])
    n = len(rings)
    attrs = {"height": np.array(cols.pop("height", [np.nan] * n), np.float32),
             "num_floors": np.array(cols.pop("num_floors", [np.nan] * n), np.float32),
             "roof_height": np.full(n, np.nan, np.float32)}
    for k in ("class", "roof_shape", "roof_material", "facade_material", "height_src"):
        attrs[k] = mapgeom.text_column(cols.pop(k, [""] * n))
    return mapgeom.Features(layer="buildings", ids=mapgeom.text_column([f"b{i}" for i in range(n)]),
                            kind=np.full(n, mapgeom.POLYGON, np.uint8), part_off=np.arange(n + 1, dtype=np.int64),
                            ring_off=np.arange(n + 1, dtype=np.int64), coord_off=co.astype(np.int64),
                            lonlat=np.array([p for r in rings for p in r], np.float64), attrs=attrs)


class CoversTest(unittest.TestCase):
    def test_france_and_not_its_neighbours(self):
        for la, lo in (AIX, (48.8566, 2.3522), (45.764, 4.8357), (48.5734, 7.7521), (41.9192, 8.7386),
                       (48.39, -4.486), (-20.88, 55.45), (16.24, -61.53)):
            self.assertTrue(bdtopo.covers(mapcells.cell_of(la, lo).bounds()), (la, lo))
        for la, lo in ((41.3874, 2.1686), (50.85, 4.35), (45.07, 7.69), (48.137, 11.575), (47.5596, 7.5886),
                       (51.5, -0.12), (40.0, -3.7), (39.29, -76.61)):
            self.assertFalse(bdtopo.covers(mapcells.cell_of(la, lo).bounds()), (la, lo))


class ParseTest(unittest.TestCase):
    def test_records_and_what_they_give(self):
        lon, lat = 5.4474, 43.5297
        feats = [
            # an Aix town house: 14.2 m to the eaves, 2 m roof rise, 4 storeys, stone walls, tiles
            feature(square(lon, lat), hauteur=14.2, altitude_minimale_toit=209.0, altitude_maximale_toit=211.0,
                    nombre_d_etages=4, nombre_de_logements=6, materiaux_des_murs="10", materiaux_de_la_toiture="10",
                    usage_1="Résidentiel"),
            # a house: one dwelling, two storeys, a flat roof (rise 0.3 m), brick and concrete
            feature(square(lon + 0.001, lat), hauteur=6.0, altitude_minimale_toit=105.0,
                    altitude_maximale_toit=105.3, nombre_d_etages=2, nombre_de_logements=1,
                    materiaux_des_murs="40", materiaux_de_la_toiture="40", usage_1="Résidentiel"),
            # a Paris block: no rise known, zinc roof
            feature(square(lon + 0.002, lat), hauteur=18.0, materiaux_de_la_toiture="30",
                    usage_1="Commercial et services"),
            # a church; a tiny annex
            feature(square(lon + 0.003, lat, 0.0003, 0.0006), hauteur=20.0, nature="Eglise", usage_1="Religieux"),
            feature(square(lon + 0.004, lat, 0.00005, 0.00005), hauteur=2.5, usage_1="Annexe"),
            # broken: too few points
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[lon, lat], [lon, lat]]]},
             "properties": {}},
        ]
        r = bdtopo.parse(feats)
        self.assertEqual(len(r["lon"]), 5)
        self.assertTrue(np.allclose(r["lon"][0], lon + 0.00005) and np.allclose(r["lat"][0], lat + 0.00005))
        self.assertAlmostEqual(float(r["area"][0]), 0.0001 * 111320 * np.cos(np.radians(lat)) * 0.0001 * 111320,
                               delta=2.0)
        a = bdtopo.interpret(r)
        self.assertAlmostEqual(a["height"][0], 16.2, places=4)         # eaves + rise
        self.assertAlmostEqual(a["height"][1], 6.0, places=4)          # flat: the eaves'
        self.assertTrue(a["eave_only"][2] and not a["eave_only"][0])
        self.assertEqual(list(a["cls"]), ["", "house", "commercial", "church", "shed"])
        self.assertEqual(list(a["facade_material"][:2]), ["stone", "brick"])
        self.assertEqual(list(a["roof_material"][:3]), ["tile", "concrete", "zinc"])
        self.assertEqual(int(a["num_floors"][0]), 4)

    def test_concrete_walls_of_housing_are_rendered(self):
        r = bdtopo.parse([feature(square(5.0, 43.0), hauteur=9.0, materiaux_des_murs="30", usage_1="Résidentiel"),
                          feature(square(5.01, 43.0), hauteur=9.0, materiaux_des_murs="30", usage_1="Industriel")])
        self.assertEqual(list(bdtopo.interpret(r)["facade_material"]), ["plaster", "concrete"])


class JoinTest(unittest.TestCase):
    def setUp(self):
        lon, lat = 5.4474, 43.5297
        self.lon, self.lat = lon, lat
        self.recs = bdtopo.Records(bdtopo.parse([
            feature(square(lon, lat), hauteur=14.2, altitude_minimale_toit=209.0, altitude_maximale_toit=211.0,
                    nombre_d_etages=4, materiaux_des_murs="10", materiaux_de_la_toiture="10",
                    usage_1="Commercial et services"),
            # two cadastre buildings under one map outline: the larger one's record wins
            feature(square(lon + 0.001, lat, 0.00004), hauteur=5.0, nombre_d_etages=1),
            feature(square(lon + 0.00104, lat, 0.00006), hauteur=11.0, nombre_d_etages=3),
        ]))

    def test_fills_what_the_map_leaves_out(self):
        f = layer([square(self.lon, self.lat), square(self.lon + 0.001, self.lat), square(self.lon + 0.01, self.lat)])
        a = bdtopo.join(f, self.recs)
        self.assertAlmostEqual(float(a["height"][0]), 16.2, places=3)
        self.assertEqual(str(a["height_src"][0]), "ign_bdtopo")
        self.assertEqual(int(a["num_floors"][0]), 4)
        self.assertAlmostEqual(float(a["roof_rise"][0]), 2.0, places=3)
        self.assertAlmostEqual(float(a["roof_height"][0]), 2.0, places=3)
        self.assertEqual((str(a["facade_material"][0]), str(a["roof_material"][0]), str(a["class"][0])),
                         ("stone", "tile", "commercial"))
        self.assertAlmostEqual(float(a["height"][1]), 11.0, places=3)      # (the larger record)
        self.assertEqual(str(a["height_src"][1]), "ign_bdtopo_eave")
        self.assertTrue(np.isnan(a["height"][2]) and str(a["height_src"][2]) == "")   # (no record: as it was)
        self.assertTrue(np.isnan(a["roof_rise"][2]))

    def test_the_maps_own_values_stay_ml_heights_give_way(self):
        f = layer([square(self.lon, self.lat), square(self.lon, self.lat)], height=[12.0, 30.0], num_floors=[3, np.nan],
                  height_src=["OpenStreetMap", "Microsoft ML Buildings"], facade_material=["brick", ""],
                  roof_material=["slate", ""], **{"class": ["apartments", "yes"]})
        a = bdtopo.join(f, self.recs)
        self.assertEqual(float(a["height"][0]), 12.0)
        self.assertEqual(int(a["num_floors"][0]), 3)
        self.assertEqual((str(a["facade_material"][0]), str(a["roof_material"][0]), str(a["class"][0])),
                         ("brick", "slate", "apartments"))
        self.assertAlmostEqual(float(a["height"][1]), 16.2, places=3)      # (the ML height: measured instead)
        self.assertEqual(str(a["class"][1]), "commercial")                  # (a generic class)

    def test_no_match_no_new_attrs(self):
        self.assertIsNone(bdtopo.join(layer([square(6.0, 44.0)]), self.recs))
        self.assertIsNone(bdtopo.join(layer([square(self.lon, self.lat)]), bdtopo.EMPTY))


class UnmatchedTest(unittest.TestCase):
    def setUp(self):
        self.lon, self.lat = 5.4474, 43.5297
        self.recs = bdtopo.Records(bdtopo.parse([
            # 0: matches the first map footprint
            feature(square(self.lon, self.lat), cleabs="BAT_MATCHED", hauteur=14.2,
                    altitude_minimale_toit=209.0, altitude_maximale_toit=211.0, nombre_d_etages=4,
                    materiaux_des_murs="10", materiaux_de_la_toiture="10", usage_1="Commercial et services"),
            # 1 & 2: two cadastre buildings both inside the second map footprint (neither is unmatched)
            feature(square(self.lon + 0.001, self.lat, 0.00004), cleabs="BAT_CAD_A", hauteur=5.0, nombre_d_etages=1),
            feature(square(self.lon + 0.00104, self.lat, 0.00006), cleabs="BAT_CAD_B", hauteur=11.0, nombre_d_etages=3),
            # 3 & 4: two unmatched rural buildings (given out of order to verify deterministic ~ign:<id> sort)
            feature(square(self.lon + 0.003, self.lat), cleabs="BAT_RURAL_2", hauteur=7.5,
                    altitude_minimale_toit=100.0, altitude_maximale_toit=102.0, nombre_d_etages=2,
                    nombre_de_logements=1, materiaux_des_murs="10", materiaux_de_la_toiture="10",
                    usage_1="Résidentiel"),
            feature(square(self.lon + 0.002, self.lat), cleabs="BAT_RURAL_1", hauteur=6.0,
                    nombre_d_etages=1, usage_1="Agricole", materiaux_des_murs="60", materiaux_de_la_toiture="10"),
            # 5: outside the cell bounds -> not owned by this cell
            feature(square(self.lon + 0.05, self.lat), cleabs="BAT_OUTSIDE", hauteur=8.0, nombre_d_etages=2),
        ]))

    def test_appends_unmatched_sorted_without_suppressing_overture(self):
        # Map has 3 footprints: 0 matches BAT_MATCHED, 1 covers BAT_CAD_A & BAT_CAD_B, 2 has no BD TOPO match at all
        f = layer([square(self.lon, self.lat), square(self.lon + 0.001, self.lat), square(self.lon + 0.005, self.lat)],
                  height=[np.nan, np.nan, 9.5], height_src=["", "", "OpenStreetMap"],
                  **{"class": ["", "", "house"]})
        bounds = (self.lon - 0.001, self.lat - 0.001, self.lon + 0.01, self.lat + 0.01)
        bdtopo.merge(f, self.recs, bounds=bounds)
        self.assertEqual(list(f.ids), ["b0", "b1", "b2", "~ign:BAT_RURAL_1", "~ign:BAT_RURAL_2"])
        # Existing unmatched Overture building b2 is preserved intact (never suppressed to 0.01m)
        self.assertAlmostEqual(float(f.attrs["height"][2]), 9.5, places=3)
        self.assertEqual(str(f.attrs["class"][2]), "house")
        self.assertTrue(bool(f.own[2]))
        # Appended BD TOPO buildings carry their measured attributes and IGN_BDTOPO dataset
        self.assertAlmostEqual(float(f.attrs["height"][3]), 6.0, places=3)
        self.assertEqual(str(f.attrs["height_src"][3]), "ign_bdtopo_eave")
        self.assertEqual(str(f.attrs["class"][3]), "farm_auxiliary")
        self.assertEqual(str(f.attrs["facade_material"][3]), "wood")
        self.assertEqual(str(f.attrs["src_dataset"][3]), "IGN_BDTOPO")
        self.assertAlmostEqual(float(f.attrs["height"][4]), 9.5, places=3)
        self.assertEqual(str(f.attrs["height_src"][4]), "ign_bdtopo")
        self.assertEqual(str(f.attrs["class"][4]), "house")
        self.assertEqual(str(f.attrs["facade_material"][4]), "stone")
        self.assertEqual(str(f.attrs["roof_material"][4]), "tile")
        self.assertTrue(bool(f.own[3]) and bool(f.own[4]))

    def test_empty_map_layer_receives_unmatched_buildings(self):
        import geodesy
        import mapdata
        key = mapcells.cell_of(*AIX)
        with tempfile.TemporaryDirectory() as d:
            fx = FakeFetcher([
                feature(square(AIX[1], AIX[0]), cleabs="BAT_B", hauteur=8.0, nombre_d_etages=2),
                feature(square(AIX[1] + 0.0005, AIX[0]), cleabs="BAT_A", hauteur=5.0, nombre_d_etages=1),
            ])
            md = mapdata.MapData.__new__(mapdata.MapData)
            md._lock = threading.Lock()
            md.bdtopo = bdtopo.Source(d, fx)
            self.assertTrue(md.bdtopo.wait([key]))
            empty = mapgeom.empty("buildings")
            empty.localize(geodesy.EnuFrame(*key.center()))
            out = md._bdtopo(key, "buildings", empty)
            self.assertEqual(list(out.ids), ["~ign:BAT_A", "~ign:BAT_B"])
            self.assertEqual(len(out.en), len(out.lonlat))

    def test_bdtopo_cache_is_managed(self):
        import cache
        self.assertIn("bdtopo", cache.MANAGED)


class PlanTest(unittest.TestCase):
    RING = np.array([[0, 0], [10, 0], [10, 8], [0, 8]], np.float64)

    def at(self, **kw):
        a = {"class": "", "height": None, "num_floors": None, "roof_shape": "", "roof_height": None,
             "height_src": None, "seed_id": "x"}
        a.update(kw)
        return a

    def test_a_measured_rise_pitches_an_attached_house(self):
        base = bld.plan(self.RING, self.at(height=16.2, num_floors=4), 2, *AIX)
        s = bld.plan(self.RING, self.at(height=16.2, num_floors=4, roof_rise=2.0, roof_height=2.0,
                                        height_src="ign_bdtopo"), 2, *AIX)
        self.assertIn(s.roof, ("gabled", "hipped"))
        self.assertAlmostEqual(s.height, 14.2, places=3)        # (walls to the eaves)
        flat = bld.plan(self.RING, self.at(height=6.0, num_floors=2, roof_rise=0.3, height_src="ign_bdtopo"), 0, *AIX)
        self.assertEqual(flat.roof, "flat")
        self.assertIsNotNone(base)

    def test_eave_heights_are_not_cut_by_a_predicted_roof(self):
        s = bld.plan(self.RING, self.at(height=9.0, height_src="ign_bdtopo_eave",
                                        **{"class": "house"}), 0, *AIX)
        if s.roof != "flat":
            self.assertAlmostEqual(s.height, 9.0, places=3)

    def test_no_rise_no_change(self):
        """Outside France (no roof_rise attribute) plan is what it was."""
        a = self.at(height=9.0, num_floors=3)
        s1 = bld.plan(self.RING, dict(a), 0, 39.29, -76.61)
        s2 = bld.plan(self.RING, dict(a, roof_rise=None), 0, 39.29, -76.61)
        self.assertEqual((s1.roof, s1.height, s1.floors, s1.roof_mat), (s2.roof, s2.height, s2.floors, s2.roof_mat))


class Res:
    def __init__(self, body):
        self.body = body
        self.headers = {}


class FakeFetcher:
    def __init__(self, features, delay=0.0, fail=False):
        self.features, self.delay, self.fail, self.calls = features, delay, fail, 0

    def get(self, url, kind="demand", lane=None, headers=None):
        import json
        self.calls += 1
        time.sleep(self.delay)
        if self.fail:
            raise IOError("offline")
        return Res(json.dumps({"features": self.features}).encode())


class SourceTest(unittest.TestCase):
    def test_outside_france_nothing_is_asked(self):
        with tempfile.TemporaryDirectory() as d:
            fx = FakeFetcher([])
            s = bdtopo.Source(d, fx)
            self.assertTrue(s.wait([mapcells.cell_of(39.29, -76.61)]))
            self.assertEqual(fx.calls, 0)

    def test_downloaded_once_kept_on_disk_and_joined(self):
        key = mapcells.cell_of(*AIX)
        with tempfile.TemporaryDirectory() as d:
            fx = FakeFetcher([feature(square(AIX[1], AIX[0]), hauteur=12.0)])
            s = bdtopo.Source(d, fx)
            self.assertTrue(s.wait([key]))
            self.assertEqual(fx.calls, 1)
            self.assertTrue(os.path.exists(s.path(key)))
            s2 = bdtopo.Source(d, FakeFetcher([], fail=True))
            self.assertEqual(s2.cached(key).n, 1)
            self.assertTrue(bdtopo.credited_near(d, AIX[0], AIX[1], 1500.0))
            self.assertFalse(bdtopo.credited_near(d, 39.29, -76.61, 1500.0))

    def test_a_slow_answer_is_waited_for_at_most_the_bound(self):
        key = mapcells.cell_of(*AIX)
        with tempfile.TemporaryDirectory() as d:
            s = bdtopo.Source(d, FakeFetcher([], delay=1.5))
            old = bdtopo.WAIT_S
            bdtopo.WAIT_S = 0.2
            try:
                t0 = time.monotonic()
                self.assertFalse(s.wait([key]))
                self.assertLess(time.monotonic() - t0, 1.0)
            finally:
                bdtopo.WAIT_S = old
            for t in threading.enumerate():
                if t.name.startswith("bdtopo"):
                    t.join(5.0)
            self.assertIsNotNone(s.cached(key))     # (there for the next build)

    def test_a_failed_download_is_not_kept(self):
        key = mapcells.cell_of(*AIX)
        with tempfile.TemporaryDirectory() as d:
            s = bdtopo.Source(d, FakeFetcher([], fail=True))
            self.assertFalse(s.wait([key]))
            self.assertFalse(os.path.exists(s.path(key)))

    def test_a_tile_without_its_records_is_a_revisit_fault(self):
        import mapdata
        key = mapcells.cell_of(*AIX)
        with tempfile.TemporaryDirectory() as d:
            md = mapdata.MapData.__new__(mapdata.MapData)
            md.bdtopo = bdtopo.Source(d, FakeFetcher([], fail=True))
            before = revisit.thread_faults()
            self.assertFalse(md.bdtopo_wait([key]))
            self.assertEqual(revisit.thread_faults(), before + 1)


if __name__ == "__main__":
    unittest.main()
