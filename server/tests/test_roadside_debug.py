"""Roadside diagnostics expose consistent evidence without changing generation."""
import json
import importlib.util
import sys
from pathlib import Path
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import roadside_debug as debug
import roads
from test_roads import FRAME, features


class RoadsideDiagnosticTest(unittest.TestCase):
    def road(self):
        return features([[(-250., 0.), (250., 0.)]], ["tertiary"], ids=["synthetic-road"])

    def elev(self, lat, lon):
        east, _, _ = FRAME.geodetic_to_enu(np.asarray(lat), np.asarray(lon), 0.)
        return 10. + 5. * np.cos(np.asarray(east) * np.pi / 500.)

    def test_disabled_mode_does_not_sample(self):
        with mock.patch.object(debug, "ENABLED", False), mock.patch.object(debug, "explain") as explain:
            self.assertIsNone(debug.emit(self.road(), 0, roads.Spec(6.2, "paved", False, "tertiary"),
                                         FRAME, elev=lambda *_: self.fail("unexpected sampling")))
        explain.assert_not_called()

    def test_selected_identity_does_not_guess_relation_way(self):
        f = self.road()
        f.attrs["src_record"] = np.array(["r123@4"])
        with mock.patch.object(debug, "ENABLED", True), mock.patch.object(debug, "SELECTORS", frozenset(["123"])):
            self.assertFalse(debug.selected(f, 0))
        with mock.patch.object(debug, "ENABLED", True), mock.patch.object(debug, "SELECTORS", frozenset(["synthetic-road"])):
            self.assertTrue(debug.selected(f, 0))

    def test_profile_exposes_sparse_chord_error_and_height_conventions(self):
        result = debug.explain(self.road(), 0, roads.Spec(6.2, "paved", False, "tertiary"), FRAME,
                               elev=self.elev, raw_elev=lambda la, lo: self.elev(la, lo) - 1.)
        profile = result["profiles"][0]
        self.assertLess(min(profile["source_chord_minus_graded_m"]), -4.)
        self.assertTrue(np.allclose(np.asarray(profile["graded_h_m"]) - profile["raw_h_m"], 1.))
        self.assertIn("ENU", result["height_convention"])
        self.assertIsNone(profile["drawn_z_m"])
        self.assertIn("edge_left", profile["cross_sections"])
        self.assertIn("shoulder_right", profile["cross_sections"])
        json.dumps(result, allow_nan=False)

    def test_nearby_parapet_and_pole_described_without_false_ownership(self):
        wall = {"base": np.array([[0., -4.], [40., -4.]]), "top": np.array([14., 14.]),
                "foot": np.array([10., 10.]), "road_z": np.array([13.4, 13.4]), "parapet": True}
        props = [["pole", 10., 3., 10.05, 0., 1., 9.2, 0.], ["pylon", 800., 0., 30., 0., 1., 35.8, 0.]]
        result = debug.explain(self.road(), 0, roads.Spec(6.2, "paved", False, "tertiary"), FRAME,
            elev=self.elev, ground=lambda x, y: np.full(np.shape(x), 10.), walls=[wall], props=props)
        self.assertEqual(len(result["walls"]), 1)
        self.assertEqual(result["walls"][0]["geometry_type"], "procedural stone parapet")
        self.assertEqual(result["walls"][0]["height_m"], [4., 4.])
        self.assertIsNone(result["walls"][0]["feature_id"])
        self.assertEqual(len(result["poles"]), 1)
        self.assertAlmostEqual(result["poles"][0]["origin_to_ground_m"], .05)
        self.assertIn("not observed", result["collision_ground"])

    def test_failures_are_contained_and_selected_log_is_json(self):
        with mock.patch.object(debug, "ENABLED", True), mock.patch.object(debug, "SELECTORS", frozenset(["*"])):
            with self.assertLogs(debug.log, "INFO") as logs:
                record = debug.emit(self.road(), 0, roads.Spec(6.2, "paved", False, "tertiary"), FRAME,
                                    elev=self.elev, tile=[17, 1, 2], dem_source="synthetic test source")
            self.assertEqual(json.loads(logs.records[0].getMessage()), record)
            with mock.patch.object(debug, "explain", side_effect=ValueError("test failure")):
                with self.assertLogs(debug.log, "WARNING"):
                    self.assertIsNone(debug.emit(self.road(), 0, None, FRAME, elev=self.elev))

    def test_long_feature_samples_only_selected_range(self):
        f = features([[(-3000., 0.), (-250., 0.), (0., 0.), (250., 0.), (3000., 0.)]],
                     ["tertiary"], ids=["synthetic-long-road"])
        asked = []
        def elev(lat, lon):
            x, _, _ = FRAME.geodetic_to_enu(np.asarray(lat), np.asarray(lon), 0.)
            asked.extend(np.asarray(x).ravel().tolist())
            return np.full(np.shape(lat), 10.)
        result = debug.explain(f, 0, roads.Spec(6.2, "paved", False, "tertiary"), FRAME, elev=elev)
        # Bracketing source vertices can extend beyond the selected window, but
        # the entire 6 km feature must not be sampled as a dense profile.
        self.assertLess(result["profiles"][0]["selected_range"]["samples"], 310)
        self.assertLess(len(asked), 1700)

    def test_bridge_spans_are_not_reported_as_ground_road_profiles(self):
        f = features([[(-250., 0.), (250., 0.)]], ["tertiary"], ids=["synthetic-bridge-road"],
                     flags=[(0, "is_bridge", .4, .6)], levels=[(0, 1., .4, .6)])
        result = debug.explain(f, 0, roads.Spec(6.2, "paved", False, "tertiary"), FRAME, elev=self.elev)
        self.assertEqual(len(result["profiles"]), 2)
        self.assertTrue(all(abs(x) > 45. for profile in result["profiles"] for x in profile["east_m"]))
        self.assertIn("bridge/tunnel spans excluded", result["profile_scope"])
        self.assertTrue(result["off_ground_ranges"])

    def test_geotile_generation_hook_logs_serializable_tile_identity(self):
        import geotiles
        tile = geotiles.geobox(*geotiles.tile_of(FRAME.lat0, FRAME.lon0))
        with mock.patch.object(debug, "ENABLED", True), mock.patch.object(debug, "SELECTORS", frozenset(["synthetic-road"])):
            with self.assertLogs(debug.log, "INFO") as logs:
                roads.build_tile(tile.frame, tile, 0, [self.road()], self.elev,
                                 walks=[], walls=[], props=[], raw_elev=self.elev, dem_source="synthetic test DEM")
        records = [json.loads(item.getMessage()) for item in logs.records]
        self.assertTrue(records)
        self.assertEqual(records[0]["cache"]["tile"], list(tile.key) if isinstance(tile.key, tuple) else tile.key)

    def test_offline_profile_uses_captured_built_world_plans_and_ground_parts(self):
        path = Path(__file__).resolve().parents[2] / "tools/roadside_profile.py"
        module_spec = importlib.util.spec_from_file_location("roadside_profile_probe", path)
        probe = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(probe)
        record = probe.isolated_record()
        self.assertEqual(record["osm_way_id"], 15739626)
        self.assertEqual(len(record["cached_plan_provenance"]["plans"]), 4)
        self.assertEqual(len(record["profiles"]), 2)
        self.assertEqual(record["walls"], [])
        self.assertTrue(record["poles"])
        self.assertLess(max(abs(p["origin_to_ground_m"]) for p in record["poles"]), .02)


if __name__ == "__main__":
    unittest.main()
