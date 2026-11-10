"""One verified OSM turbine: identity, dimensions, world placement and existing tile ownership."""
import copy
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
import revisit
import roads
import windturbines as wt

LAT, LON = 41.1397814, -104.9978882


def document():
    return json.loads(Path(wt.DATA_PATH).read_text(encoding="utf-8"))


def feature():
    return document()["elements"][0]


def owner(frame):
    x, y, _ = geotiles.geodetic_to_world(frame, LAT, LON)
    return math.floor(x / roads.TILE), math.floor(y / roads.TILE)


class TurbineSourceTest(unittest.TestCase):
    def test_verified_happy_jack_snapshot_and_exact_individual_position(self):
        doc = document()
        self.assertEqual(hashlib.sha256(Path(wt.DATA_PATH).read_bytes().replace(b"\r\n", b"\n")).hexdigest(), wt.DATA_SHA256)
        self.assertEqual(doc["phase"], 1)
        self.assertEqual(doc["source"]["farm_relation_id"], 3792596)
        self.assertEqual(len(doc["elements"]), 1)
        turbines = wt.selected_from_document(doc)
        self.assertEqual(len(turbines), 1)
        turbine = turbines[0]
        self.assertEqual(turbine.id, "osm:node:1175681635")
        self.assertEqual((turbine.lat, turbine.lon), (LAT, LON))
        self.assertNotEqual((turbine.lat, turbine.lon), (41.13972, -104.99778), "the farm reference is not the node")
        self.assertEqual(turbine.version, 4)
        self.assertEqual(turbine.tags["generator:source"], "wind")
        self.assertEqual(turbine.tags["generator:output:electricity"], "2.1 MW")
        self.assertIn("odbl", doc["source"]["license"])

    def test_wind_generator_detection_and_lifecycle_tags(self):
        node = feature()
        self.assertTrue(wt.is_wind_turbine(node))
        for remove in ("generator:source", "generator:method"):
            one_tag = copy.deepcopy(node)
            one_tag["tags"].pop(remove)
            self.assertTrue(wt.is_wind_turbine(one_tag))
        for kind in ("way", "relation"):
            self.assertFalse(wt.is_wind_turbine(dict(node, type=kind)))
        for tags in ({"power": "plant", "plant:source": "wind"},
                     {"power": "generator", "generator:source": "gas", "generator:method": "wind_turbine"},
                     {"power": "generator"}):
            self.assertFalse(wt.is_wind_turbine(dict(node, tags=tags)))
        for addition in ({"disused": "yes"}, {"construction": "yes"}, {"end_date": "2020"},
                         {"abandoned:power": "generator"}, {"generator:status": "decommissioned"}):
            self.assertFalse(wt.is_wind_turbine(dict(node, tags=dict(node["tags"], **addition))))

    def test_malformed_coordinates_do_not_generate(self):
        for change in ({"lat": float("nan")}, {"lon": float("inf")}, {"lat": 91}, {"id": "unknown"}):
            self.assertFalse(wt.is_wind_turbine(dict(feature(), **change)))
        self.assertFalse(wt.is_wind_turbine(None))
        self.assertFalse(wt.is_wind_turbine({}))

    def test_allowlist_excludes_other_turbines_and_deduplicates_the_selected_node(self):
        node = feature()
        later = dict(node, version=5)
        other = dict(node, id=1175681636, lon=LON + 0.002)
        farm = {"type": "relation", "id": 3792596, "tags": {"power": "plant", "plant:source": "wind"}}
        selected = wt.selected_from_document({"elements": [node, other, node, farm, later]})
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].id, wt.SELECTED_OSM_ID)
        self.assertEqual(selected[0].version, 5)

    def test_missing_or_modified_snapshot_skips_without_an_invented_position(self):
        self.addCleanup(wt._load.cache_clear)
        with tempfile.TemporaryDirectory() as tmp:
            for name, content in (("absent.json", None), ("changed.json", b'{"elements":[]}')):
                path = Path(tmp) / name
                if content is not None:
                    path.write_bytes(content)
                wt._load.cache_clear()
                with mock.patch.object(wt, "DATA_PATH", str(path)), self.assertLogs(wt.log, level="WARNING"):
                    self.assertEqual(wt._load(), ())

    def test_windows_crlf_checkout_preserves_the_verified_feature(self):
        self.addCleanup(wt._load.cache_clear)
        raw = Path(wt.DATA_PATH).read_bytes().replace(b"\r\n", b"\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            path.write_bytes(raw.replace(b"\n", b"\r\n"))
            wt._load.cache_clear()
            with mock.patch.object(wt, "DATA_PATH", str(path)):
                selected = wt._load()
            self.assertEqual(len(selected), 1)
            self.assertEqual((selected[0].id, selected[0].lat, selected[0].lon), (wt.SELECTED_OSM_ID, LAT, LON))


class TurbineDimensionsTest(unittest.TestCase):
    def test_actual_node_has_no_dimensions_and_uses_measured_model_defaults(self):
        tags = feature()["tags"]
        self.assertFalse({"height", "height:hub", "rotor:diameter"} & tags.keys())
        size = wt.dimensions(tags)
        self.assertEqual(size["basis"], "model_default")
        self.assertEqual(size["scale"], 1.0)
        self.assertAlmostEqual(size["hub_height_m"], 101.410286)
        self.assertAlmostEqual(size["rotor_diameter_m"], 69.158482)
        self.assertAlmostEqual(size["tip_height_m"], 135.989527)

    def test_optional_osm_dimensions_choose_a_documented_uniform_scale(self):
        for key, meters in (("height:hub", wt.MODEL_HUB_M), ("rotor:diameter", wt.MODEL_ROTOR_M),
                            ("height", wt.MODEL_TIP_M)):
            size = wt.dimensions({key: str(meters * 0.75) + " m"})
            self.assertEqual(size["basis"], "osm:" + key)
            self.assertAlmostEqual(size["scale"], 0.75)
        feet = wt.dimensions({"height:hub": str(wt.MODEL_HUB_M / 0.3048) + " ft"})
        self.assertAlmostEqual(feet["scale"], 1.0)
        priority = wt.dimensions({"height:hub": "80", "rotor:diameter": "90", "height": "125"})
        self.assertEqual(priority["basis"], "osm:height:hub")

    def test_missing_unknown_and_unreasonable_lengths_keep_the_model_default(self):
        for value in (None, "unknown", "nan", "inf", "-10", "0", "100000 m", "80;90", "80 bananas"):
            self.assertEqual(wt.dimensions({"height:hub": value})["scale"], 1.0)
        self.assertAlmostEqual(wt.length_m("100 feet"), 30.48)
        self.assertIsNone(wt.length_m("nan"))


class TurbinePlacementTest(unittest.TestCase):
    def setUp(self):
        # Keep the verified Phase 1 geographic cases independent of global-export updates.
        source = mock.patch.object(wt, "near", return_value=wt._load())
        source.start()
        self.addCleanup(source.stop)

    def test_exact_geographic_position_and_sampled_terrain_height(self):
        frame = geodesy.EnuFrame(LAT - 0.025, LON + 0.035)
        calls = []
        def ground(lat, lon):
            calls.append((lat.copy(), lon.copy()))
            return 1875.0 + (lat - LAT) * 5000.0
        rows = wt.tile_turbines(frame, *owner(frame), ground)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], wt.SELECTED_OSM_ID)
        self.assertEqual((row["lat"], row["lon"]), (LAT, LON))
        np.testing.assert_array_equal(calls[0], np.array([[LAT], [LON]]))
        expected = geotiles.geodetic_to_world(frame, LAT, LON, 1875.0)
        np.testing.assert_allclose([row[k] for k in ("x", "y", "z")], expected, atol=1e-8)
        lat, lon, height = geotiles.world_to_geodetic(frame, [[row["x"], row["y"], row["z"]]])
        self.assertAlmostEqual(lat[0], LAT, places=9)
        self.assertAlmostEqual(lon[0], LON, places=9)
        self.assertAlmostEqual(height[0], 1875.0, places=5)
        self.assertEqual(row["yaw"], 0.0, "stock heading; no terrain slope tilts the tower")

    def test_one_half_open_legacy_owner_at_a_shared_tile_corner(self):
        frame = geodesy.EnuFrame(LAT, LON)
        rows = [r for x in (-1, 0) for y in (-1, 0)
                for r in wt.tile_turbines(frame, x, y, lambda la, lo: np.full(la.shape, 1800.0))]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], wt.SELECTED_OSM_ID)

    def test_one_geotile_owner_and_exact_mapping_into_another_anchor(self):
        gx, gy = geotiles.tile_of(LAT, LON)
        rows = []
        for x in range(gx - 1, gx + 2):
            for y in range(gy - 1, gy + 2):
                tile = geotiles.geobox(x, y)
                for row in wt.tile_turbines(tile.frame, tile, None, lambda la, lo: 1820.0):
                    rows.append((tile, row))
        self.assertEqual(len(rows), 1)
        tile, row = rows[0]
        target = geodesy.EnuFrame(LAT + 0.07, LON - 0.04)
        actual = geotiles.place(tile.frame, target, (0.0, 0.0, row["z"]), [[row["x"], row["y"], row["z"]]])
        expected = geotiles.geodetic_to_world(target, LAT, LON, 1820.0)
        np.testing.assert_allclose(actual[0], expected, atol=0.001)

    def test_far_tiles_do_not_request_elevation(self):
        ground = mock.Mock(side_effect=AssertionError("an unowned turbine must not sample terrain"))
        self.assertEqual(wt.tile_turbines(geodesy.EnuFrame(LAT, LON), 20, 20, ground), [])
        ground.assert_not_called()

    def test_tile_reload_repeats_identity_scale_and_placement(self):
        frame = geodesy.EnuFrame(LAT - 0.003, LON + 0.008)
        args = (frame, *owner(frame), lambda la, lo: np.full(la.shape, 1812.345))
        first = wt.tile_turbines(*args)
        wt._load.cache_clear()
        self.assertEqual(first, wt.tile_turbines(*args))

    def test_unavailable_terrain_does_not_persist_a_guessed_height(self):
        frame = geodesy.EnuFrame(LAT, LON)
        before = revisit.faults()
        self.assertEqual(wt.tile_turbines(frame, *owner(frame), lambda la, lo: np.nan), [])
        self.assertGreater(revisit.faults(), before)
        later = wt.tile_turbines(frame, *owner(frame), lambda la, lo: 1810.0)
        self.assertEqual(len(later), 1)



class SmallTurbineTest(unittest.TestCase):
    """Small (rooftop, mast, structure-mounted) turbines are left out: the Eiffel Tower's two 3.2 m turbines, mapped
    at height 134, stood under it as toy-sized utility turbines (Discord 2026-11-06)."""

    def test_the_eiffel_towers_are_left_out(self):
        eiffel = {"height": "134.0", "rotor:diameter": "3.200000047683716"}
        self.assertTrue(wt.small(eiffel))
        self.assertTrue(wt.small({"height:hub": "9"}))
        self.assertFalse(wt.small({"rotor:diameter": "82", "height:hub": "80"}))
        self.assertFalse(wt.small({}), "unknown size: the stock turbine")
        t = wt.Turbine("osm:node:1", 48.8581, 2.2944, eiffel, 0)
        self.assertEqual(wt.rows_for_features(geodesy.EnuFrame(48.858, 2.294), [t], lambda la, lo: np.zeros(np.shape(la))), [])

if __name__ == "__main__":
    unittest.main()
