"""Glue tests (the checks themselves are native/roadscan's cargo tests): python3 -m unittest discover -s tools/roadscan"""
import json
from pathlib import Path
import struct
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

    def test_tiles_along(self):
        line = [[39.28, -76.60], [39.28, -76.59]]             # ~0.9 km east
        got = roadscan.tiles_along([line], 40.0)
        again = roadscan.tiles_along([line], 40.0)
        self.assertEqual(got, again)
        order = sorted(got.items(), key=lambda kv: kv[1][2])
        xs = [v[0] for _k, v in order]
        self.assertEqual(xs, sorted(xs))                       # (east: tile x grows along the line)
        self.assertGreaterEqual(len(got), 3)

    def test_route_bin(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.bin"
            roadscan.write_route_bin(p, [[1.0, 2.0], [3.0, 4.0]])
            b = p.read_bytes()
            self.assertEqual(b[:8], b"RSROUTE1")
            self.assertEqual(struct.unpack("<I4d", b[8:]), (2, 1.0, 2.0, 3.0, 4.0))

    def test_names(self):
        with self.assertRaises(SystemExit):
            roadscan.corridor_dir("../x")


if __name__ == "__main__":
    unittest.main()
