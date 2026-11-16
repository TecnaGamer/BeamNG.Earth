"""The developer export builder preserves source identity/geometry and fails closed."""
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("build_windturbines", ROOT / "tools" / "build_windturbines.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
TAGS = {"power": "generator", "generator:source": "wind"}


def point(x, y):
    return struct.pack("<BIdd", 1, 1, x, y)


def polygon(rings):
    return struct.pack("<BII", 1, 3, len(rings)) + b"".join(
        struct.pack("<I", len(ring)) + b"".join(struct.pack("<dd", *xy) for xy in ring) for ring in rings)


class BuildWindTurbinesTest(unittest.TestCase):
    def test_exact_crs84_point_not_rounded_bbox_or_swapped_coordinates(self):
        element = builder.osm_element("node", 1175681635, TAGS, point(-104.9978882, 41.1397814))
        self.assertEqual((element["lat"], element["lon"]), (41.1397814, -104.9978882))
        arrays = builder.arrays_from_document({"elements": [element]}, min_count=1)
        self.assertEqual(arrays["lat"].dtype, np.dtype("float64"))
        self.assertEqual(arrays["osm_id"].tolist(), [1175681635])
        self.assertEqual(arrays["osm_type"].tolist(), [0])

    def test_multi_outer_holes_and_truncated_geometry_cannot_become_turbine_positions(self):
        ring = [(1, 1), (1.00001, 1), (1.00001, 1.00001), (1, 1)]
        poly = polygon([ring])
        multi = struct.pack("<BII", 1, 6, 1) + poly
        self.assertIsNotNone(builder.osm_element("way", 12, TAGS, multi))
        self.assertIsNone(builder.osm_element("way", 12, TAGS, polygon([ring, ring])))
        self.assertIsNone(builder.osm_element("way", 12, TAGS, struct.pack("<BII", 1, 6, 2) + poly + poly))
        self.assertIsNone(builder.osm_element("node", 12, TAGS, point(1, 2)[:-1]))
        self.assertIsNone(builder.osm_element("node", 12, TAGS, poly))

    def test_original_osm_active_rows_only_from_enriched_parquet(self):
        import duckdb
        with tempfile.TemporaryDirectory() as tmp, duckdb.connect() as database:
            source = Path(tmp) / "fixture.parquet"
            database.execute("CREATE TABLE source(osm_type VARCHAR, osm_id BIGINT, origin VARCHAR, lifecycle VARCHAR, tags MAP(VARCHAR,VARCHAR), geometry BLOB)")
            for fid, origin, lifecycle in [(1, "osm", "active"), (2, "uswtdb", "active"), (3, "osm", "construction")]:
                database.execute("INSERT INTO source VALUES ('node',?,?,?,?,?)",
                                 [fid, origin, lifecycle, TAGS, point(1, 2)])
            database.execute("COPY source TO ? (FORMAT PARQUET)", [str(source)])
            document, stats = builder.read_parquet(source, {"table:row_count": 3})
            self.assertEqual([e["id"] for e in document["elements"]], [1])
            self.assertEqual(stats["source_rows"], 3)
            with self.assertRaises(ValueError):
                builder.read_parquet(source, {"table:row_count": 4})

    def test_output_is_deterministic_identity_sorted_and_dimensions_are_optional_metres(self):
        a = {"type": "node", "id": 30, "lat": 2, "lon": 3, "tags": TAGS}
        b = {"type": "node", "id": 20, "lat": 1, "lon": 4, "tags": {**TAGS, "height:hub": "300 ft", "height": "bad"}}
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / "source.json", Path(tmp) / "wind.npz"
            source.write_text(json.dumps({"elements": [a, b, a]}))
            first = builder.build(source, output, min_count=1)
            with np.load(output, allow_pickle=False) as index:
                self.assertEqual(index["osm_id"].tolist(), [20, 30])
                self.assertAlmostEqual(float(index["hub_height"][0]), 91.44, places=4)
                self.assertTrue(np.isnan(index["height"]).all())
                self.assertTrue(np.isnan(index["rotor_diameter"]).all())
            source.write_text(json.dumps({"elements": [b, a]}))
            second = builder.build(source, output, min_count=1)
            self.assertEqual(first["index_sha256"], second["index_sha256"])
            self.assertEqual(second["counts"]["turbines"], 2)
            self.assertEqual(json.loads(output.with_suffix(".provenance.json").read_text())["license"], "ODbL-1.0")

    def test_incomplete_export_or_tiny_result_leaves_existing_artifact_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / "source.json", Path(tmp) / "wind.npz"
            output.write_bytes(b"preserve")
            source.write_text(json.dumps({"elements": [], "remark": "Query timed out"}))
            with self.assertRaises(ValueError):
                builder.build(source, output)
            self.assertEqual(output.read_bytes(), b"preserve")
            source.write_text(json.dumps({"elements": []}))
            with self.assertRaises(ValueError):
                builder.build(source, output)
            self.assertEqual(output.read_bytes(), b"preserve")
            self.assertFalse(output.with_suffix(".provenance.json").exists())

    def test_distinct_ids_at_identical_position_resolve_without_rounding_nearby_points(self):
        elements = [
            {"type": "node", "id": 31, "lat": 2.0, "lon": 3.0, "tags": TAGS},
            {"type": "node", "id": 30, "lat": 2.0, "lon": 3.0, "tags": TAGS},
            {"type": "node", "id": 32, "lat": 2.00000001, "lon": 3.0, "tags": TAGS},
        ]
        arrays = builder.arrays_from_document({"elements": elements}, min_count=1)
        self.assertEqual(arrays["osm_id"].tolist(), [30, 32])
        self.assertEqual(arrays["lat"].tolist(), [2.0, 2.00000001])


if __name__ == "__main__":
    unittest.main()
