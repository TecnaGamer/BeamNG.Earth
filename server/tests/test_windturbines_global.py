"""Worldwide mapped identities, individual footprints, spatial queries and farm placement."""
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy
import geotiles
import roads
import windturbines as wt

TAGS = {"power": "generator", "generator:source": "wind", "generator:method": "wind_turbine"}


def node(fid, lat=41.0, lon=-105.0, **tags):
    return {"type": "node", "id": fid, "lat": lat, "lon": lon, "tags": dict(TAGS, **tags), "version": 1}


def way(fid, lat=41.0, lon=-105.0, delta=0.00002):
    return {"type": "way", "id": fid, "version": 1, "tags": dict(TAGS), "geometry": [
        {"lat": lat + a * delta, "lon": lon + b * delta} for a, b in
        ((-1, -1), (-1, 1), (1, 1), (1, -1), (-1, -1))]}


class GlobalDetectionTest(unittest.TestCase):
    def test_worldwide_nodes_preserve_individual_ids_and_exact_positions(self):
        points = [node(1, 41.1397814, -104.9978882), node(2, -34.6, 138.5), node(3, 55.7, 8.2),
                  node(4, 23.6, 120.5), node(5, -8.9, -36.0)]
        found = wt.features_from_document({"elements": points})
        self.assertEqual({t.id for t in found}, {"osm:node:" + str(i) for i in range(1, 6)})
        self.assertEqual({(t.lat, t.lon) for t in found}, {(n["lat"], n["lon"]) for n in points})

    def test_latest_inactive_version_supersedes_active_and_farm_areas_are_excluded(self):
        active = node(1)
        inactive = dict(active, version=2, tags=dict(TAGS, **{"disused:power": "generator"}))
        farm = {"type": "relation", "id": 2, "tags": {"power": "plant", "plant:source": "wind"}}
        large = way(3, delta=0.01)
        tagged_site = way(4)
        tagged_site["tags"]["site"] = "wind_farm"
        self.assertEqual(wt.features_from_document({"elements": [active, inactive, farm, large, tagged_site]}), ())

    def test_small_closed_explicit_footprint_uses_mapped_area_centroid(self):
        footprint = way(99)
        found = wt.features_from_document({"elements": [footprint]})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].id, "osm:way:99")
        self.assertAlmostEqual(found[0].lat, 41.0, places=9)
        self.assertAlmostEqual(found[0].lon, -105.0, places=9)
        self.assertEqual(found[0].tags, TAGS)

    def test_nodes_win_duplicate_way_footprints_without_merging_distinct_nodes(self):
        footprint = way(7)
        footprint["nodes"] = [10, 11, 12, 10]
        doc = {"elements": [node(10), node(20, lon=-105.000001), footprint, way(8)]}
        found = wt.features_from_document(doc)
        self.assertEqual({t.id for t in found}, {"osm:node:10", "osm:node:20"})

    def test_exact_coincident_node_ids_deduplicate_without_rounding_nearby_positions(self):
        points = [node(3), node(2), node(1, lon=-105.00000001)]
        forward = wt.features_from_document({"elements": points})
        reverse = wt.features_from_document({"elements": points[::-1]})
        self.assertEqual([t.id for t in forward], ["osm:node:1", "osm:node:2"])
        self.assertEqual(forward, reverse)

    def test_open_degenerate_and_geometry_free_ways_are_omitted(self):
        open_way, flat, no_geom = way(1), way(2), way(3)
        open_way["geometry"].pop()
        flat["geometry"] = [{"lat": 41, "lon": -105 + i * 0.00001} for i in (0, 1, 2, 0)]
        no_geom.pop("geometry")
        no_geom["center"] = {"lat": 41, "lon": -105}
        self.assertEqual(wt.features_from_document({"elements": [open_way, flat, no_geom]}), ())

    def test_self_crossing_individual_footprint_is_rejected(self):
        crossing = way(12)
        crossing["geometry"] = [{"lat": 41 + a * 0.00002, "lon": -105 + b * 0.00002}
                                for a, b in ((-1, -1), (1, 1), (-1, 1), (1, -0.5), (-1, -1))]
        self.assertFalse(wt.is_wind_turbine(crossing))

    def test_dateline_footprint_centroid_and_node_dedup(self):
        footprint = way(9, lat=0, lon=180)
        for p in footprint["geometry"]:
            p["lon"] = (p["lon"] + 180) % 360 - 180
        found = wt.features_from_document({"elements": [footprint]})
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(abs(found[0].lon), 180, places=8)
        found = wt.features_from_document({"elements": [footprint, node(1, 0, -179.999999)]})
        self.assertEqual([t.id for t in found], ["osm:node:1"])

    def test_dateline_way_contains_offcentre_node_on_other_longitude_branch(self):
        footprint = way(9, lat=0, lon=180, delta=0.0002)
        for p in footprint["geometry"]:
            p["lon"] = (p["lon"] + 180) % 360 - 180
        found = wt.features_from_document({"elements": [footprint, node(1, 0, 179.9999)]})
        self.assertEqual([t.id for t in found], ["osm:node:1"])


class GlobalIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "wind.npz"
        self.addCleanup(wt._index.cache_clear)

    def index(self, lat, lon, **changes):
        n = len(lat)
        arrays = {"lat": np.asarray(lat, np.float64), "lon": np.asarray(lon, np.float64),
                  "osm_id": np.arange(1, n + 1, dtype=np.int64), "osm_type": np.zeros(n, np.uint8),
                  "height": np.full(n, np.nan, np.float32), "hub_height": np.full(n, np.nan, np.float32),
                  "rotor_diameter": np.full(n, np.nan, np.float32)}
        arrays.update(changes)
        np.savez_compressed(self.path, **arrays)
        wt._index.cache_clear()
        for attr, value in (("INDEX_PATH", str(self.path)), ("INDEX_SHA256", hashlib.sha256(self.path.read_bytes()).hexdigest())):
            patch = mock.patch.object(wt, attr, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_index_query_preserves_type_id_and_dimension_basis(self):
        self.index([-35, 41, 56], [139, -105, 8], osm_type=np.array([0, 1, 0], np.uint8),
                   hub_height=np.array([np.nan, 80, np.nan], np.float32))
        result = wt.near(40, -106, 42, -104)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0].id, result[0].lat, result[0].lon), ("osm:way:2", 41, -105))
        self.assertEqual(wt.dimensions(result[0].tags)["basis"], "osm:height:hub")
        self.assertAlmostEqual(wt.dimensions(result[0].tags)["hub_height_m"], 80)
        self.assertFalse(wt._index()["lat"].flags.writeable)

    def test_wrapped_query_selects_both_sides_of_dateline(self):
        self.index([0, 0, 0], [-179.99, 0, 179.99])
        self.assertEqual({t.id for t in wt.near(-1, 179.9, 1, -179.9)}, {"osm:node:1", "osm:node:3"})

    def test_exact_180_longitude_alias_is_visible_in_owning_cell(self):
        self.index([0, 0], [-180, 180])
        self.assertEqual(len(wt.near(-1, -180, 1, -179.9)), 2)

    def test_corrupt_and_unordered_indexes_use_verified_fallback(self):
        self.index([42, 41], [-105, -105])
        with self.assertLogs(wt.log, level="WARNING"):
            self.assertEqual([t.id for t in wt.near(41, -106, 42, -104)], [wt.SELECTED_OSM_ID])
        wt._index.cache_clear()
        self.path.write_bytes(b"not an npz")
        with self.assertLogs(wt.log, level="WARNING"):
            self.assertIsNone(wt._index())

    def test_empty_window_does_not_materialize_global_population(self):
        self.index(np.linspace(-80, 80, 200000), np.zeros(200000))
        self.assertEqual(wt.near(40, 120, 41, 121), ())
        found = wt.near(40, -1, 40.001, 1)
        self.assertLess(len(found), 3)


class FarmPlacementTest(unittest.TestCase):
    def test_legacy_prefilter_uses_exact_world_inverse_at_distant_tile_edge(self):
        frame = geodesy.EnuFrame(41, -105)
        la, lo, _ = geotiles.world_to_geodetic(frame, [[15872.001, 16127.999, 0]])
        feature = wt.Turbine("osm:node:1", float(la[0]), float(lo[0]), {}, 0)
        def source(s, w, n, e):
            return (feature,) if s <= feature.lat <= n and w <= feature.lon <= e else ()
        with mock.patch.object(wt, "near", side_effect=source):
            rows = wt.tile_turbines(frame, 62, 62, lambda la, lo: 1800)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["x"], 15872.001, places=6)
        self.assertAlmostEqual(rows[0]["y"], 16127.999, places=6)

    def test_small_mapped_turbine_uses_its_dimension_instead_of_utility_default(self):
        size = wt.dimensions({"height:hub": "3 m", "rotor:diameter": "2 m"})
        self.assertEqual(size["basis"], "osm:height:hub")
        self.assertAlmostEqual(size["hub_height_m"], 3)
        self.assertLess(size["scale"], 0.1)

    def test_300_turbines_sample_ground_once_preserve_ids_and_stay_upright(self):
        features = wt.features_from_document({"elements": [node(i + 1, 41 + i // 20 * 0.002,
                     -105 + i % 20 * 0.002, **{"height:hub": "80 m"}) for i in range(300)]})
        frame = geodesy.EnuFrame(41, -105)
        ground = mock.Mock(side_effect=lambda lat, lon: 1800 + (lat - 41) * 10000)
        rows = wt.rows_for_features(frame, features, ground, collide=False)
        self.assertEqual(len(rows), 300)
        self.assertEqual(len({r["id"] for r in rows}), 300)
        ground.assert_called_once()
        self.assertTrue(all(r["yaw"] == 0 and r["collide"] is False for r in rows))
        for row in rows:
            self.assertAlmostEqual(row["dimensions"]["hub_height_m"], 80)
            expect = geotiles.geodetic_to_world(frame, row["lat"], row["lon"], 1800 + (row["lat"] - 41) * 10000)
            np.testing.assert_allclose([row[k] for k in ("x", "y", "z")], expect, atol=1e-7)
        self.assertEqual(rows, wt.rows_for_features(frame, features, ground, collide=False))

    def test_partial_height_failure_keeps_valid_records_and_empty_does_not_sample(self):
        features = wt.features_from_document({"elements": [node(1), node(2, lat=41.01)]})
        rows = wt.rows_for_features(geodesy.EnuFrame(41, -105), features, lambda la, lo: [1800, np.nan])
        self.assertEqual([r["id"] for r in rows], ["osm:node:1"])
        self.assertTrue(rows[0]["collide"])
        missing = mock.Mock(side_effect=AssertionError("no empty sampling"))
        self.assertEqual(wt.rows_for_features(None, [], missing), [])
        missing.assert_not_called()

    def test_global_tile_boundary_ownership_and_repeatable_georebase(self):
        lat, lon = 41.1397814, -104.9978882
        features = wt.features_from_document({"elements": [node(i + 1, lat + (i // 10) * 0.0004,
                                                           lon + (i % 10) * 0.0004) for i in range(100)]})
        frame = geodesy.EnuFrame(lat, lon)
        x, y, _ = geotiles.geodetic_to_world(frame, [t.lat for t in features], [t.lon for t in features])
        seen = []
        with mock.patch.object(wt, "near", return_value=features):
            for tx in range(math.floor(min(x) / roads.TILE) - 1, math.floor(max(x) / roads.TILE) + 2):
                for ty in range(math.floor(min(y) / roads.TILE) - 1, math.floor(max(y) / roads.TILE) + 2):
                    seen.extend(wt.tile_turbines(frame, tx, ty, lambda la, lo: 1810))
        self.assertEqual(len(seen), 100)
        self.assertEqual(len({r["id"] for r in seen}), 100)
        target = geodesy.EnuFrame(lat + 0.03, lon - 0.02)
        for row in seen:
            actual = geotiles.place(frame, target, [0, 0, row["z"]], [[row[k] for k in ("x", "y", "z")]])[0]
            expected = geotiles.geodetic_to_world(target, row["lat"], row["lon"], 1810)
            np.testing.assert_allclose(actual, expected, atol=0.001)


class BundledGlobalIndexTest(unittest.TestCase):
    def tearDown(self):
        wt._index.cache_clear()

    def test_shipped_index_has_unique_global_identities_and_matching_provenance(self):
        wt._index.cache_clear()
        metadata = json.loads(Path(wt.INDEX_PATH).with_suffix(".provenance.json").read_text(encoding="utf-8"))
        arrays = wt._index()
        self.assertIsNotNone(arrays)
        self.assertEqual(metadata["index_sha256"], wt.INDEX_SHA256)
        self.assertEqual(len(arrays["lat"]), metadata["counts"]["turbines"])
        self.assertGreater(len(arrays["lat"]), 400000)
        self.assertGreater(int((arrays["osm_type"] == 1).sum()), 1000)
        identities = arrays["osm_id"] * 2 + arrays["osm_type"]
        self.assertEqual(len(np.unique(identities)), len(identities))
        self.assertEqual(metadata["license"], "ODbL-1.0")
        # Wide windows demonstrate coverage in both hemispheres without assuming every farm is mapped.
        for bounds in ((20, -130, 60, -60), (-55, -85, 10, -30), (35, -15, 70, 40),
                       (-35, -20, 35, 50), (5, 60, 60, 150), (-45, 110, -10, 155)):
            self.assertGreater(len(wt.near(*bounds)), 0, bounds)

    def test_all_14_happy_jack_nodes_match_the_verified_relation_coordinates(self):
        # OSM relation3792596/full.json, retrieved2026-11-01, ODbL / OSM contributors.
        expected = {
            1175681635: (41.1397814, -104.9978882), 1175681655: (41.1354639, -104.9945141),
            1175681668: (41.1390659, -105.0087641), 1175681673: (41.1409328, -104.9837572),
            1175681696: (41.1449692, -105.0080693), 1175681704: (41.1378817, -104.9960538),
            1175681710: (41.1456158, -104.9952184), 1175681725: (41.1458474, -104.9825096),
            1175681743: (41.1325031, -104.9944158), 1175681749: (41.1432938, -104.9843651),
            1175681765: (41.1354396, -104.9848449), 1175681793: (41.1333675, -104.9828615),
            1175681796: (41.1429762, -104.996021), 1175681800: (41.1413259, -105.0089223),
        }
        wt._index.cache_clear()
        found = wt.near(41.13, -105.01, 41.15, -104.98)
        self.assertEqual({t.id: (t.lat, t.lon) for t in found},
                         {"osm:node:" + str(fid): coords for fid, coords in expected.items()})


if __name__ == "__main__":
    unittest.main()
