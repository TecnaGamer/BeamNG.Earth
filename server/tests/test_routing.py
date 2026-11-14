"""routing: the GPS's whole-route planning (OSRM response handling, simplification)."""
import sys
import unittest
import numpy as np
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

    def test_url_through_waypoints(self):
        # GitHub #935: the waypoints as OSRM's intermediate coordinates, in order; a bearing only for the start
        u = routing.url(39.28, -76.59, 38.9, -77.03, 370, via=[(39.1, -76.8), (39.0, -76.9)])
        self.assertIn("/driving/-76.590000,39.280000;-76.800000,39.100000;-76.900000,39.000000;-77.030000,38.900000?",
                      u)
        self.assertTrue(u.endswith("&bearings=10,60;;;"))
        too_many = [(39.0, -76.0)] * (routing.MAX_VIA + 5)
        self.assertEqual(routing.url(39.28, -76.59, 38.9, -77.03, via=too_many).count(";"), routing.MAX_VIA + 1)

    def test_via_points_keeps_the_valid_ones(self):
        self.assertEqual(routing.via_points([[1, 2], ["x", 3], [91, 0], [3.5, -4]]), [(1.0, 2.0), (3.5, -4.0)])
        self.assertEqual(routing.via_points(None), [])
        self.assertEqual(len(routing.via_points([[1, 1]] * 50)), routing.MAX_VIA)

    def test_parse_legs(self):
        data = {"code": "Ok", "routes": [{"distance": 3000, "duration": 200,
                                          "geometry": {"coordinates": [[-76.0, 39.0], [-76.01, 39.01], [-76.02, 39.0]]},
                                          "legs": [{"distance": 1000, "duration": 70}, {"distance": 2000, "duration": 130}]}]}
        self.assertEqual(routing.parse(data)["legs"], [[1000.0, 70.0], [2000.0, 130.0]])
        data["routes"][0]["legs"] = [{"distance": 3000, "duration": 200}]
        self.assertNotIn("legs", routing.parse(data))

    def test_url(self):
        u = routing.url(39.28, -76.59, 38.9, -77.03)
        self.assertIn("-76.590000,39.280000;-77.030000,38.900000", u)
        self.assertTrue(u.startswith("https://routing.openstreetmap.de/"))
        self.assertNotIn("bearings", u)
        self.assertTrue(routing.url(39.28, -76.59, 38.9, -77.03, 370).endswith("&bearings=10,60;"))


if __name__ == "__main__":
    unittest.main()


class RouteProfileSmoothTest(unittest.TestCase):
    def test_spikes_of_a_sample_or_two_go_the_climb_stays(self):
        # (the ground under a bridge, an embankment's bump: the map's chart drew them as peaks, the user 2026-10-27)
        import earthd
        climb = np.linspace(10.0, 40.0, 40)
        z = climb.copy()
        z[10] += 15.0           # a bump one sample long
        z[25:27] -= 12.0        # a valley under a bridge, two samples
        got = earthd.route_profile_smooth(z)
        self.assertLess(float(np.abs(got - climb)[2:-2].max()), 2.0)     # (of 12-15 m spikes)
        self.assertEqual(len(got), len(z))
        self.assertTrue(np.allclose(earthd.route_profile_smooth([5.0, 6.0]), [5.0, 6.0]))
