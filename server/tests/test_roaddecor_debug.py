"""Opt-in diagnostics describe real identity and rules without changing generation."""
import json
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import furniture
import mapgeom
import roaddecor_debug as debug
import roads


def feature(record=""):
    # This synthetic feature intentionally has no real-world OSM id.
    return mapgeom.Features("roads", np.array(["synthetic-road"]), np.array([mapgeom.LINE]),
                            np.array([0, 1]), np.array([0, 1]), np.array([0, 2]),
                            np.array([[-105., 41.], [-105., 41.001]]),
                            attrs={"src_record": np.array([record]), "class": np.array(["residential"])})


class DiagnosticTest(unittest.TestCase):
    def test_default_disabled_does_not_sample_or_format(self):
        with mock.patch.object(debug, "ENABLED", False), mock.patch.object(debug, "explain") as explain:
            self.assertIsNone(debug.emit(feature(), 0, roads.Spec(7., "paved", False, "residential")))
            explain.assert_not_called()

    def test_selection_requires_matching_identity(self):
        with mock.patch.object(debug, "ENABLED", True), mock.patch.object(debug, "SELECTORS", frozenset(["123"])):
            self.assertFalse(debug.selected(feature(), 0))
            self.assertTrue(debug.selected(feature("w123@4"), 0))
            self.assertFalse(debug.selected(feature("w456@4"), 0))
        with mock.patch.object(debug, "ENABLED", True), mock.patch.object(debug, "SELECTORS", frozenset(["synthetic-road"])):
            self.assertTrue(debug.selected(feature(), 0))

    def test_rural_explicit_sidewalk_does_not_imply_decorative_trees(self):
        f = feature()
        f.attrs["osm_sidewalk"] = np.array(["both"])
        spec = roads.Spec(7., "paved", False, "residential", sides=(1, -1))
        result = debug.explain(f, 0, spec)
        self.assertTrue(result["eligibility"]["sidewalk"]["eligible"])
        self.assertEqual(result["eligibility"]["sidewalk"]["policy_source"], "mapped_way_tag")
        self.assertFalse(result["eligibility"]["decorative_tree"]["eligible"])
        self.assertFalse(result["eligibility"]["streetlight"]["eligible"])
        self.assertIsNone(result["osm_way_id"])
        self.assertEqual(result["cache"]["status"], "generated")
        self.assertIn("not used", result["context"]["administrative_boundaries"])
        json.dumps(result, allow_nan=False)

    def test_explicit_unlit_is_explained_independently_of_urban_context(self):
        f = feature()
        f.attrs["osm_lit"] = np.array(["no"])
        result = debug.explain(f, 0, roads.Spec(7., "paved", True, "residential"))
        self.assertFalse(result["eligibility"]["streetlight"]["eligible"])
        self.assertEqual(result["eligibility"]["streetlight"]["object_source"], "procedural")

    def test_explicit_lit_preserves_rural_lighting_policy(self):
        f = feature()
        f.attrs["osm_lit"] = np.array(["yes"])
        result = debug.explain(f, 0, roads.Spec(7., "paved", False, "residential"))
        light = result["eligibility"]["streetlight"]
        self.assertTrue(light["eligible"])
        self.assertEqual(light["rule"], "osm.lit.yes")
        self.assertEqual(light["policy_source"], "mapped_way_tag")
        self.assertEqual(light["object_source"], "procedural")

    def test_urban_street_retains_sidewalk_and_light_candidates(self):
        result = debug.explain(feature(), 0, roads.Spec(7., "paved", True, "residential"))
        self.assertTrue(result["eligibility"]["sidewalk"]["eligible"])
        self.assertTrue(result["eligibility"]["streetlight"]["eligible"])
        self.assertEqual(result["placement"]["status"], "per-object attribution not supplied")

    def test_missing_numeric_tags_are_valid_json_nulls(self):
        f = feature()
        f.attrs["osm_width"] = np.array([np.nan])
        result = debug.explain(f, 0, roads.Spec(np.float64(7.), "paved", False, "residential"))
        self.assertIsNone(result["tags"]["width"])
        json.dumps(result, allow_nan=False)

    def test_selected_log_is_json_and_diagnostic_failure_is_contained(self):
        with mock.patch.object(debug, "ENABLED", True), mock.patch.object(debug, "SELECTORS", frozenset(["*"])):
            with self.assertLogs(debug.log, "INFO") as logs:
                result = debug.emit(feature(), 0, roads.Spec(7., "paved", False, "residential"), tile=[1, 2])
            self.assertEqual(json.loads(logs.records[0].getMessage()), result)
            with mock.patch.object(debug, "explain", side_effect=ValueError("bad evidence")):
                with self.assertLogs(debug.log, "WARNING"):
                    self.assertIsNone(debug.emit(feature(), 0, None))


class OfflineProbeTest(unittest.TestCase):
    def test_probe_reads_source_files_without_rewriting_them(self):
        path = Path(__file__).resolve().parents[2] / "tools" / "road_decorations.py"
        spec = importlib.util.spec_from_file_location("road_decorations_probe", path)
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        with tempfile.TemporaryDirectory() as directory:
            road_path = Path(directory) / "roads.npz"
            cover_path = Path(directory) / "cover.npz"
            np.savez_compressed(road_path, **feature().to_arrays(""))
            meta = {"crs": "EPSG:4326", "x0": -105.01, "y0": 41.01,
                    "dx": .001, "dy": .001, "nodata": 0, "files": ["synthetic cover"]}
            np.savez_compressed(cover_path, data=np.full((20, 20), 50, np.uint8), meta=np.array(json.dumps(meta)))
            before = {p.name: p.read_bytes() for p in Path(directory).iterdir()}
            with mock.patch.object(debug, "ENABLED"), mock.patch.object(debug, "SELECTORS"):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    result = probe.main(["--roads", str(road_path), "--landcover", str(cover_path),
                                         "--id", "synthetic-road", "--region", "na:sparse"])
            self.assertEqual(result, 0)
            record = json.loads(output.getvalue())
            self.assertEqual(record["cache"]["status"], "offline_recomputed_from_source_cache")
            self.assertTrue(record["source_cache"]["coverage_complete"])
            self.assertEqual(before, {p.name: p.read_bytes() for p in Path(directory).iterdir()})


if __name__ == "__main__":
    unittest.main()
