"""Built environment scores use existing polygons and POIs, independently of road policy."""
import os
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import development  # noqa: E402
import geodesy  # noqa: E402
import mapcells  # noqa: E402
import mapgeom  # noqa: E402


FRAME = geodesy.EnuFrame(41.14, -104.82)
BOX = (-256., -256., 256., 256.)


def ring(x0, y0, x1, y1):
    return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]], float)


def geographic(xy, frame=FRAME):
    xy = np.asarray(xy, float)
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.)
    return np.column_stack([lon, lat])


def polygons(layer, features, attrs=None, ids=None):
    """Features are polygon parts, each containing an exterior and optional holes."""
    geoms = [(mapgeom.POLYGON, [[geographic(r) for r in part] for part in parts]) for parts in features]
    f = mapgeom.build(layer, np.array(ids or [f"p{i}" for i in range(len(geoms))]), geoms)
    f.attrs = {key: np.asarray(values) for key, values in (attrs or {}).items()}
    return f


def buildings(rects, cls="", subtype=""):
    return polygons("buildings", [[[r]] for r in rects],
                    {"class": [cls] * len(rects), "subtype": [subtype] * len(rects)})


def places(xy, category="restaurant"):
    f = mapgeom.build("places", np.array([f"poi{i}" for i in range(len(xy))]),
                      [(mapgeom.POINT, [[p[None]]]) for p in geographic(xy)])
    f.attrs = {"basic_category": np.full(len(xy), category), "taxonomy": np.full(len(xy), ""),
               "operating_status": np.full(len(xy), "open"), "confidence": np.ones(len(xy))}
    return f


def streetfront(cls="", subtype=""):
    return buildings([ring(-120, 12, 120, 46), ring(-120, -46, 120, -12)], cls, subtype)


def sample(layers, xy=(0., 0.), radius=100., box=BOX, frame=FRAME):
    return development.Context(frame, box, layers, radius=radius).sample(np.array(xy), np.array([0., 1.]), 4.)


class CoverageTest(unittest.TestCase):
    def test_coverage_is_union_not_sum_of_repeated_or_overlapping_buildings(self):
        first = buildings([ring(-24, 12, 24, 44)])
        baseline = sample({"buildings": [first]})
        repeated = sample({"buildings": [first, first]})
        overlap = sample({"buildings": [buildings([ring(-24, 12, 24, 44), ring(-12, 16, 12, 36)])]})
        self.assertGreater(baseline.coverage, .025)
        self.assertAlmostEqual(baseline.coverage, repeated.coverage, places=8)
        self.assertAlmostEqual(baseline.coverage, overlap.coverage, places=8)

    def test_holes_and_multipart_footprints_preserve_topology(self):
        outer, hole = ring(-60, -60, 60, 60), ring(-40, -40, 40, 40)
        solid = sample({"buildings": [polygons("buildings", [[[outer]]])]})
        courtyard = sample({"buildings": [polygons("buildings", [[[outer, hole]]])]})
        self.assertGreater(solid.coverage - courtyard.coverage, .15)
        left, right = ring(-50, 12, -10, 42), ring(10, 12, 50, 42)
        multi = sample({"buildings": [polygons("buildings", [[[left], [right]]])]})
        split = sample({"buildings": [buildings([left, right])]})
        self.assertAlmostEqual(multi.coverage, split.coverage, places=8)

    def test_footprint_outside_radius_is_not_counted_by_its_centroid_area(self):
        inside = sample({"buildings": [buildings([ring(-15, 12, 15, 42)])]}, radius=80.)
        outside = sample({"buildings": [buildings([ring(130, 12, 160, 42)])]}, radius=80.)
        self.assertGreater(inside.coverage, .02)
        self.assertEqual(outside.coverage, 0.)

    def test_configurable_radius_changes_local_coverage(self):
        f = buildings([ring(-15, 12, 15, 42)])
        near = sample({"buildings": [f]}, radius=60.)
        wide = sample({"buildings": [f]}, radius=140.)
        self.assertGreater(near.coverage, 2 * wide.coverage)

    def test_city_sized_mapping_error_does_not_establish_development(self):
        f = buildings([ring(-1500, -1500, 1500, 1500)], "retail", "commercial")
        got = sample({"buildings": [f]})
        self.assertEqual(got.coverage, 0.)
        self.assertEqual(got.tier, 0)


class PatternTest(unittest.TestCase):
    def test_residential_polygon_and_isolated_house_remain_sparse(self):
        land = polygons("land_use", [[[ring(-500, -500, 500, 500)]]], {"class": ["residential"]})
        got = sample({"buildings": [buildings([ring(55, 35, 65, 45)], "house")], "land_use": [land]})
        self.assertEqual(got.tier, 0)
        self.assertEqual(got.limited, 0.)
        self.assertEqual(got.urban, 0.)

    def test_residential_and_commercial_frontages_have_different_tiers(self):
        residential = sample({"buildings": [streetfront("apartments", "residential")]})
        commercial = sample({"buildings": [streetfront("", "commercial")]})
        self.assertEqual(residential.tier, 1)
        self.assertGreater(residential.limited, 0.)
        self.assertEqual(residential.urban, 0.)
        self.assertEqual(commercial.tier, 2, "commercial subtype counts when class is absent")
        self.assertGreater(commercial.urban, residential.urban)

    def test_commercial_land_use_can_supply_missing_building_classification(self):
        land = polygons("land_use", [[[ring(-150, -100, 150, 100)]]], {"class": ["retail"]})
        got = sample({"buildings": [streetfront()], "land_use": [land]})
        self.assertEqual(got.tier, 2)
        self.assertGreater(got.commercial, 0.)

    def test_industrial_land_without_pedestrian_cluster_stays_limited(self):
        land = polygons("land_use", [[[ring(-200, -200, 200, 200)]]], {"class": ["industrial"]})
        got = sample({"buildings": [streetfront("commercial", "commercial")], "land_use": [land]})
        self.assertEqual(got.tier, 1)
        self.assertEqual(got.urban, 0.)

    def test_raw_land_use_source_tags_are_supported_without_reclassifying_roads(self):
        for tags in ('{"landuse": "retail"}', '[{"key": "landuse", "value": "retail"}]'):
            with self.subTest(tags=tags):
                land = polygons("land_use", [[[ring(-150, -100, 150, 100)]]], {"tags": [tags]})
                got = sample({"buildings": [streetfront()], "land_use": [land]})
                self.assertEqual(got.tier, 2)
        malformed = polygons("land_use", [[[ring(-150, -100, 150, 100)]]], {"tags": ["invalid json"]})
        self.assertEqual(sample({"buildings": [streetfront()], "land_use": [malformed]}).tier, 1)

    def test_underground_footprints_are_not_surface_development(self):
        f = streetfront("retail", "commercial")
        f.attrs["is_underground"] = np.ones(len(f), bool)
        got = sample({"buildings": [f]})
        self.assertEqual(got.coverage, 0.)
        self.assertEqual(got.tier, 0)

    def test_pois_alone_do_not_decorate_an_unbuilt_field(self):
        xy = [(x, y) for x in (-30., -10., 10., 30.) for y in (-20., 20.)]
        got = sample({"places": [places(xy)]})
        self.assertEqual(got.tier, 0)
        self.assertEqual(got.limited, 0.)
        self.assertEqual(got.urban, 0.)

    def test_pedestrian_pois_support_otherwise_unclassified_streetfront(self):
        xy = [(x, y) for x in (-30., -10., 10., 30.) for y in (-20., 20.)]
        got = sample({"buildings": [streetfront()], "places": [places(xy)]})
        self.assertEqual(got.tier, 2)
        self.assertGreater(got.poi, 0.)

    def test_cached_overture_shop_and_office_categories_are_pedestrian_evidence(self):
        for category in ("fashion_and_apparel_store", "food_and_beverage_store", "bank_or_credit_union",
                         "corporate_or_business_office"):
            with self.subTest(category=category):
                got = sample({"buildings": [streetfront()],
                              "places": [places([(-20., 20.), (20., 20.), (-20., -20.), (20., -20.)], category)]})
                self.assertGreater(got.poi, 3.)
                self.assertEqual(got.tier, 2)

    def test_closed_or_low_confidence_pois_do_not_make_a_residential_area_commercial(self):
        f = places([(x, y) for x in (-30., -10., 10., 30.) for y in (-20., 20.)])
        f.attrs["operating_status"] = np.array(["permanently_closed"] * 4 + ["open"] * 4)
        f.attrs["confidence"] = np.array([1.] * 4 + [.1] * 4)
        got = sample({"buildings": [streetfront("apartments", "residential")], "places": [f]})
        self.assertEqual(got.poi, 0.)
        self.assertEqual(got.tier, 1)
        self.assertEqual(got.urban, 0.)

    def test_large_agricultural_footprints_do_not_imply_occupied_streetfront(self):
        got = sample({"buildings": [streetfront("barn", "agricultural")]})
        self.assertGreater(got.coverage, .2)
        self.assertEqual(got.tier, 0)
        self.assertEqual(got.limited, 0.)

    def test_buildings_set_back_from_road_have_less_frontage(self):
        close = sample({"buildings": [buildings([ring(-60, 12, 60, 30)])]})
        setback = sample({"buildings": [buildings([ring(-60, 70, 60, 88)])]})
        self.assertGreater(close.frontage, setback.frontage)

    def test_context_weights_change_gradually_across_developed_edge(self):
        f = buildings([ring(-160, 12, 0, 46), ring(-160, -46, 0, -12)], "retail", "commercial")
        context = development.Context(FRAME, BOX, {"buildings": [f]}, radius=100.)
        got = [context.sample(np.array([x, 0.]), np.array([0., 1.]), 4.) for x in np.arange(-50., 151., 5.)]
        self.assertGreater(got[0].limited, got[-1].limited)
        self.assertLess(np.max(np.abs(np.diff([s.limited for s in got]))), .25)
        self.assertLess(np.max(np.abs(np.diff([s.urban for s in got]))), .25)


class ContextBoundaryTest(unittest.TestCase):
    def test_adjacent_tile_contexts_agree_at_same_point(self):
        layers = {"buildings": [streetfront("retail", "commercial")]}
        a = sample(layers, xy=(0., 0.), box=(-256., -128., 0., 128.))
        b = sample(layers, xy=(0., 0.), box=(0., -128., 256., 128.))
        self.assertEqual(a.tier, b.tier)
        self.assertAlmostEqual(a.coverage, b.coverage, places=6)
        self.assertAlmostEqual(a.frontage, b.frontage, places=6)
        self.assertAlmostEqual(a.score, b.score, places=6)

    def test_local_frame_change_keeps_decoration_context(self):
        layers = {"buildings": [streetfront("retail", "commercial")]}
        a = sample(layers)
        second = geodesy.EnuFrame(41.1403, -104.8203)
        x, y, _ = second.geodetic_to_enu(FRAME.lat0, FRAME.lon0, 0.)
        b = sample(layers, xy=(x, y), box=(x - 256, y - 256, x + 256, y + 256), frame=second)
        self.assertEqual(a.tier, b.tier)
        self.assertAlmostEqual(a.coverage, b.coverage, delta=.035)
        self.assertAlmostEqual(a.score, b.score, delta=.06)


class CachedMap:
    def __init__(self, root):
        self.root = root
        self._mem_lock = threading.Lock()
        self._mem = {}

    def path(self, key, layer):
        return os.path.join(self.root, str(key.x) + "_" + str(key.y) + "_" + layer + ".npz")

    def cell(self, *args, **kwargs):
        raise AssertionError("decoration scoring must never fetch cells")

    def warm(self, *args, **kwargs):
        raise AssertionError("decoration scoring must never queue downloads")


class CapturedWyomingTest(unittest.TestCase):
    """Replay original downloaded geometry and attributes, with no network or cache mutation."""
    def test_city_and_rural_streets_use_their_local_built_environment(self):
        path = Path(__file__).parent / "fixtures" / "sidewalk_development.npz"
        with path.with_suffix(".json").open(encoding="utf-8") as f:
            manifest = json.load(f)
        with np.load(path, allow_pickle=False) as archive:
            arrays = {name: archive[name] for name in archive.files}
        for case in manifest["cases"]:
            with self.subTest(location=case["name"]):
                frame = geodesy.EnuFrame(case["latitude"], case["longitude"])
                layers = {layer: [mapgeom.Features.from_arrays(layer, arrays, prefix) for prefix in prefixes]
                          for layer, prefixes in case["layers"].items()}
                context = development.Context(frame, tuple(case["box"]), layers)
                normal = np.array(case["outward"])
                half = case["road_half_width"]
                got = [context.sample(np.array(case["query_xy"]), side * normal, half) for side in (1, -1)]
                pattern = case["expected_pattern"]
                if pattern == "rural":
                    self.assertEqual([s.tier for s in got], [0, 0])
                    self.assertTrue(all(s.limited == 0. and s.urban == 0. for s in got))
                elif pattern == "limited":
                    self.assertTrue(any(s.tier == 1 and s.limited > 0. for s in got))
                    self.assertTrue(all(s.tier < 2 for s in got))
                else:
                    self.assertTrue(any(s.tier == 2 and s.urban > 0. for s in got))


if __name__ == "__main__":
    unittest.main()
