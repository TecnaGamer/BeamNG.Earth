"""Glue tests (the checks themselves are native/roadscan's cargo tests): python3 -m unittest discover -s tools/roadscan"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import roadscan  # noqa: E402


class Glue(unittest.TestCase):
    def test_polylines(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.json"
            p.write_text(json.dumps([[39.0, -76.0], [39.001, -76.0]]))
            self.assertEqual(roadscan.read_polyline(p), [[39.0, -76.0], [39.001, -76.0]])
            p.write_text(json.dumps({"type": "Feature", "geometry": {"type": "MultiLineString",
                                                                     "coordinates": [[[-76.0, 39.0]], [[-76.0, 39.001]]]}}))
            self.assertEqual(roadscan.read_polyline(p), [[39.0, -76.0], [39.001, -76.0]])
            p.write_text("# a comment\n39.0,-76.0\n39.001,-76.0\n")
            self.assertEqual(roadscan.read_polyline(p), [[39.0, -76.0], [39.001, -76.0]])

    def test_names(self):
        with self.assertRaises(SystemExit):
            roadscan.corridor_dir("../x")


if __name__ == "__main__":
    unittest.main()
