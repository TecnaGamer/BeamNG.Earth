"""Offline regressions against captured Overture/OSM and WorldCover source data.

The sources and exact cache provenance are in fixtures/i25_decorations/manifest.json.
No server, active cache, network access or actual terrain is required.
"""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import furniture
import geodesy
import mapcells
import mapdata
import mapgeom
import maprasters
import roads
import surface


FIXTURE = Path(__file__).parent / "fixtures" / "i25_decorations"


class CapturedContext:
    def __init__(self):
        self.manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
        self.rasters = {}
        with np.load(FIXTURE / "worldcover.npz", allow_pickle=False) as arrays:
            for cell in self.manifest["worldcover_cells"]:
                key = mapcells.parse(cell)
                prefix = f"{key.x}_{key.y}."
                meta = json.loads(str(arrays[prefix + "meta"]))
                self.rasters[key] = maprasters.Raster(arrays[prefix + "data"].copy(), **meta)

    def roads(self, x, y):
        with np.load(FIXTURE / f"roads_{x}_{y}.npz", allow_pickle=False) as arrays:
            return mapgeom.Features.from_arrays("roads", dict(arrays), "")

    @staticmethod
    def urban_locations(lat, lon):
        return surface.warp(np.asarray(lat), np.asarray(lon))

    def urban(self, lat, lon):
        # The production Surface applies its organic-border warp before sampling.
        lat, lon = self.urban_locations(lat, lon)
        out = np.zeros(lat.shape, bool)
        for key, raster in self.rasters.items():
            lo0, la0, lo1, la1 = key.bounds()
            inside = (lat >= la0) & (lat < la1) & (lon >= lo0) & (lon < lo1)
            if inside.any():
                out[inside] = raster.sample(lon[inside], lat[inside]) == roads.URBAN_CLASS
        return out


class I25CapturedDecorationsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = CapturedContext()

    def selected(self, x, y, name):
        features = self.context.roads(x, y)
        fid = self.context.manifest["selected_features"][name]
        index = features.index(fid)
        self.assertGreaterEqual(index, 0)
        return features, index

    def test_exact_frontage_source_tags_are_not_urban_or_sidewalk_tags(self):
        osm = json.loads((FIXTURE / "osm_6169603.json").read_text(encoding="utf-8-sig"))
        self.assertEqual((osm["id"], osm["version"]), (6169603, 8))
        self.assertEqual(osm["tags"]["highway"], "tertiary")
        self.assertEqual(osm["tags"]["name"], "Interstate 25 Frontage Road")
        self.assertNotIn("sidewalk", osm["tags"])
        self.assertNotIn("lit", osm["tags"])
        self.assertNotIn("urban", osm["tags"])

    def test_frontage_pavement_stops_voting_as_development(self):
        features, i = self.selected(3414, 6152, "frontage")
        votes = roads.urban_context(features, self.context.urban)[i]
        self.assertGreaterEqual(votes["raw_beside_share"], roads.URBAN_BESIDE_SHARE)
        self.assertLess(votes["beside_share"], roads.URBAN_BESIDE_SHARE)
        spec = roads.road_specs(features, self.context.urban)[i]
        self.assertFalse(spec.urban)
        self.assertFalse(roads.has_sidewalks(spec))

    def test_i25_carriageways_north_of_carr_remain_rural(self):
        for name in ("mainline_north_carr", "opposite_north_carr"):
            with self.subTest(name=name):
                features, i = self.selected(3415, 6150, name)
                spec = roads.road_specs(features, self.context.urban)[i]
                self.assertEqual(spec.cls, "motorway")
                self.assertFalse(spec.link)
                self.assertFalse(spec.urban)
                self.assertFalse(furniture.highway_lit("na:sparse", spec.link, spec.urban))

    def test_border_mainline_remains_rural(self):
        features, i = self.selected(3417, 6142, "border_mainline")
        spec = roads.road_specs(features, self.context.urban)[i]
        self.assertFalse(spec.urban)
        self.assertFalse(spec.link)
        self.assertFalse(furniture.highway_lit("na:sparse", spec.link, spec.urban))

    def test_reported_northbound_corridor_false_positive_is_corrected(self):
        for name in ("mainline_rural_north", "mainline_wyoming"):
            with self.subTest(name=name):
                features, i = self.selected(3417, 6142, name)
                ll = features.parts(i)[0][0]
                left, right = roads._beside(ll, [len(ll)], 30.0)
                centre = self.context.urban(ll[:, 1], ll[:, 0])
                a = self.context.urban(left[:, 1], left[:, 0])
                b = self.context.urban(right[:, 1], right[:, 0])
                # Reproduce the original erroneous positive using its exact source-vertex votes.
                self.assertGreaterEqual(float(centre.mean()), roads.URBAN_SHARE)
                self.assertGreaterEqual(float((a | b).mean()), roads.URBAN_BESIDE_SHARE)
                self.assertGreaterEqual(float((a & b).mean()), roads.URBAN_BOTH_SHARE)
                spec = roads.road_specs(features, self.context.urban)[i]
                self.assertFalse(spec.urban)
                self.assertFalse(furniture.highway_lit("na:sparse", spec.link, spec.urban))

    def test_developed_cheyenne_sidewalk_preserved(self):
        features, i = self.selected(3421, 6134, "cheyenne_sidewalk")
        self.assertEqual(str(features.attrs["osm_sidewalk"][i]), "both")
        spec = roads.road_specs(features, self.context.urban)[i]
        self.assertTrue(spec.urban)
        self.assertTrue(roads.has_sidewalks(spec))

    def test_same_actual_frontage_across_source_cells_stays_rural(self):
        copies = [self.context.roads(3414, y) for y in (6152, 6153)]
        fid = self.context.manifest["selected_features"]["frontage"]
        lines = []
        for features in copies:
            i = features.index(fid)
            self.assertGreaterEqual(i, 0)
            spec = roads.road_specs(features, self.context.urban)[i]
            self.assertFalse(spec.urban)
            self.assertFalse(roads.has_sidewalks(spec))
            lines.append(features.parts(i)[0][0])
        np.testing.assert_array_equal(*lines)

    def test_production_geographic_pavement_hook_uses_complete_captured_sources(self):
        context = self.context
        requested, missing, loaded = set(), set(), {}

        class CapturedMapData:
            def cell(self, key, layers, kind):
                self_key = str(key)
                requested.add(self_key)
                if self_key not in loaded:
                    try:
                        loaded[self_key] = context.roads(key.x, key.y)
                    except FileNotFoundError:
                        missing.add(self_key)
                        raise
                return SimpleNamespace(layers={"roads": loaded[self_key]})

        hook = mapdata.pavement_hook(CapturedMapData())
        frontage_votes = []
        for x, y, name, expected_urban in (
                (3414, 6152, "frontage", False),
                (3414, 6153, "frontage", False),
                (3417, 6142, "mainline_rural_north", False),
                (3417, 6142, "border_mainline", False),
                (3421, 6134, "cheyenne_sidewalk", True)):
            with self.subTest(cell=(x, y), feature=name):
                features, i = self.selected(x, y, name)
                features.__dict__["_bnge_pavement"] = hook
                ll = features.parts(i)[0][0]
                votes = roads._urban_votes(features, context.urban,
                                            [(i, roads._urban_samples(ll))])[i]
                cls = str(features.attrs["class"][i])
                urban = (votes["centre_share"] >= roads.URBAN_SHARE
                         and votes["beside_share"] >= roads.URBAN_BESIDE_SHARE
                         and (cls not in ("motorway", "trunk")
                              or votes["both_share"] >= roads.URBAN_BOTH_SHARE))
                self.assertEqual(urban, expected_urban)
                if name == "frontage":
                    frontage_votes.append(votes)
        self.assertEqual(frontage_votes[0], frontage_votes[1])
        self.assertTrue(requested)
        self.assertFalse(missing, "A conservative missing-source fallback must not make this test pass")
        self.assertTrue(requested <= set(context.manifest["canonical_probe_source_cells"]))

    def test_cheyenne_rendered_walks_and_lamps_survive(self):
        features, i = self.selected(3421, 6134, "cheyenne_sidewalk")
        ll = features.parts(i)[0][0]
        lon, lat = ll[len(ll) // 2]
        frame = geodesy.EnuFrame(float(lat), float(lon), 0.0)
        walks, props = [], []
        for tx in (-1, 0):
            for ty in (-1, 0):
                roads.build_tile(frame, tx, ty, [features],
                                 lambda lat, lon: np.zeros(np.shape(lat)),
                                 self.context.urban, walks=walks, props=props,
                                 lamp_region="na:sparse")
        self.assertTrue(walks)
        self.assertTrue([p for p in props if p[0] in {furniture.G_LAMP, furniture.G_POLELAMP}])

    def test_frontage_tile_output_has_no_inferred_walks_or_streetscape(self):
        features, _ = self.selected(3414, 6152, "frontage")
        frame = geodesy.EnuFrame(40.83387, -104.96816, 0.0)
        for tx in (-1, 0):
            for ty in (-1, 0):
                with self.subTest(tile=(tx, ty)):
                    walks, props = [], []
                    roads.build_tile(frame, tx, ty, [features],
                                     lambda lat, lon: np.zeros(np.shape(lat)),
                                     self.context.urban, walks=walks, props=props,
                                     lamp_region="na:sparse")
                    self.assertFalse(walks)
                    prohibited = {furniture.G_LAMP, furniture.G_POLELAMP, furniture.G_STREET_TREE,
                                  furniture.G_BENCH, furniture.G_BIN, furniture.G_HYDRANT}
                    self.assertFalse([p for p in props if p[0] in prohibited])


if __name__ == "__main__":
    unittest.main()
