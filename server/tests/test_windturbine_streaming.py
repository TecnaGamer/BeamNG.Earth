"""Turbine records survive both existing road tile formats and participate in content identity."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blockworker
import geotiles
import roads


RECORD = {"id": "osm:node:1175681635", "x": 8.0, "y": 9.0, "z": 1800.0, "yaw": 30.0, "scale": 1.0,
          "lat": 41.1397814, "lon": -104.9978882, "dimensions": {"basis": "stock_model_default"}}


class WindTurbineStreamingTest(unittest.TestCase):
    def test_legacy_road_tile_preserves_identity_and_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            roads.write_tile(tmp, "/isolated", 7, 0, 0, [], turbines=[RECORD])
            data = json.loads((Path(tmp) / "roads_7_0_0.json").read_text())
            self.assertEqual(data["turbines"], [RECORD])
            roads.write_tile(tmp, "/isolated", 7, 0, 0, [], turbines=[])
            empty = json.loads((Path(tmp) / "roads_7_0_0.json").read_text())
            self.assertNotIn("turbines", empty)

    def test_geotile_preserves_record_and_hash_changes_when_turbine_removed(self):
        tile = geotiles.geobox(*geotiles.tile_of(RECORD["lat"], RECORD["lon"]))
        parts = SimpleNamespace(chunks=[], dmesh=None, bld=None, props=[], wires=[], park=[], tmesh=None,
                                fuel=[], rmesh=None, guardrail_rows=[], garage_sound=[], wells=[],
                                turbines=[RECORD], ctx=None, walks_raw=[])
        saved = blockworker._geo_inputs.get(tile.key)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                _, _, info = blockworker._write_geo_content(tmp, "/isolated", tile, parts)
                output = Path(tmp) / blockworker.geo_names(tile.key)["json"]
                data = json.loads(output.read_text())
                self.assertEqual(data["turbines"], [RECORD])
                self.assertEqual(data["key"], tile.key)
                parts.turbines = []
                _, _, empty_info = blockworker._write_geo_content(tmp, "/isolated", tile, parts)
                self.assertNotEqual(info["h"], empty_info["h"])
                self.assertNotIn("turbines", json.loads(output.read_text()))
        finally:
            if saved is None:
                blockworker._geo_inputs.pop(tile.key, None)
            else:
                blockworker._geo_inputs[tile.key] = saved


if __name__ == "__main__":
    unittest.main()
