"""routing: the GPS's whole-route planning (OSRM response handling, simplification)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routing  # noqa: E402


class RoutingTest(unittest.TestCase):
    def test_simplify_keeps_corners(self):
        line = [(39.0, -76.0 + i * 1e-5) for i in range(50)] + [(39.0 + i * 1e-5, -76.0 + 49e-5) for i in range(1, 50)]
        s = routing.simplify(line)
        self.assertEqual(len(s), 3, "a straight run, a corner, a straight run")
        self.assertEqual(s[0], line[0])
        self.assertEqual(s[-1], line[-1])

    def test_parse(self):
        data = {"code": "Ok", "routes": [{"distance": 1234.5, "duration": 99.0,
                                         "geometry": {"coordinates": [[-76.59, 39.28], [-76.60, 39.28], [-76.60, 39.29]]}}]}
        r = routing.parse(data)
        self.assertEqual(r["points"][0], [39.28, -76.59])
        self.assertEqual((r["dist"], r["dur"]), (1234.5, 99.0))
        with self.assertRaises(ValueError):
            routing.parse({"code": "NoRoute", "routes": []})

    def test_resample_even_steps(self):
        line = [(39.0, -76.0), (39.0, -75.9)]              # ~8.6 km east, one straight segment
        r = routing.resample(line, max_points=20)
        self.assertEqual(r[0][2], 0.0)
        self.assertAlmostEqual(r[-1][0], line[-1][0], places=6)
        self.assertAlmostEqual(r[-1][1], line[-1][1], places=6)
        for a, b in zip(r, r[1:]):
            self.assertGreater(b[2], a[2])

    def test_resample_short_line_one_point(self):
        self.assertEqual(routing.resample([(39.0, -76.0)]), [(39.0, -76.0, 0.0)])
        self.assertEqual(routing.resample([]), [])

    def test_url(self):
        u = routing.url(39.28, -76.59, 38.9, -77.03)
        self.assertIn("-76.590000,39.280000;-77.030000,38.900000", u)
        self.assertTrue(u.startswith("https://routing.openstreetmap.de/"))
        self.assertNotIn("bearings", u)
        self.assertTrue(routing.url(39.28, -76.59, 38.9, -77.03, 370).endswith("&bearings=10,60;"))


if __name__ == "__main__":
    unittest.main()
