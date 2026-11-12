"""routing: the GPS's whole-route planning (OSRM response handling, simplification)."""
import sys
import copy
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

    def test_resample_crosses_dateline_near_the_route_in_both_directions(self):
        line = [(-16.5, 179.9), (-16.5, -179.9)]
        expected = 111320.0 * 0.2 * np.cos(np.deg2rad(16.5))
        for points in (line, line[::-1]):
            with self.subTest(points=points):
                got = routing.resample(points, max_points=5)
                self.assertAlmostEqual(got[-1][2], expected, places=5)
                self.assertEqual(got[0][:2], points[0])
                self.assertAlmostEqual(got[-1][1], points[-1][1], places=6)
                self.assertTrue(all(179.89 <= abs(lo) <= 180.0 for _, lo, _ in got))
                self.assertTrue(all(a[2] < b[2] for a, b in zip(got, got[1:])))

    def test_simplify_keeps_a_dateline_bend_and_drops_straight_vertices(self):
        # The middle of the straight eastbound run is not a corner at the longitude wrap.
        line = [(0.0, 179.9), (0.0, 180.0), (0.0, -179.9), (0.1, -179.9)]
        for points in (line, line[::-1]):
            with self.subTest(points=points):
                self.assertEqual(routing.simplify(points), [points[0], line[2], points[-1]])

    def test_resample_crosses_dateline_more_than_once(self):
        line = [(0.0, 179.9), (0.0, -179.9), (0.0, 179.9)]
        got = routing.resample(line, max_points=9)
        self.assertAlmostEqual(got[-1][2], 111320.0 * 0.4, places=5)
        self.assertTrue(all(abs(lo) >= 179.89 for _, lo, _ in got))

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
        self.assertIn("steps=true", u)
        self.assertTrue(routing.url(39.28, -76.59, 38.9, -77.03, 370).endswith("&bearings=10,60;"))

    def test_steps_keep_maneuvers_lanes_and_offsets_through_waypoints(self):
        def step(kind, distance, lon, **maneuver):
            return {"name": "Laugavegur", "distance": distance, "duration": distance / 10,
                    "maneuver": {"type": kind, "location": [lon, 64.1], **maneuver}}
        depart = step("depart", 900, -21.9, bearing_after=90)
        turn = step("roundabout", 100, -21.89, modifier="right", exit=3, bearing_before=90, bearing_after=180)
        turn.update(ref="1", driving_side="right", intersections=[
            {"lanes": [{"indications": ["straight", "right"], "valid": True},
                       {"indications": ["left"], "valid": False}]},
            {"lanes": [{"indications": ["left"], "valid": True}]}])
        data = {"code": "Ok", "routes": [{"distance": 1500, "duration": 150,
            "geometry": {"coordinates": [[-21.9, 64.1], [-21.88, 64.1]]}, "legs": [
                {"distance": 1000, "duration": 100, "steps": [depart, turn, step("arrive", 0, -21.885)]},
                {"distance": 500, "duration": 50, "steps": [step("depart", 500, -21.885),
                                                             step("arrive", 0, -21.88)]}]}]}
        original = copy.deepcopy(data)
        got = routing.parse(data)
        self.assertEqual(data, original, "parsing does not mutate the provider's response")
        self.assertEqual([s["type"] for s in got["steps"]], ["depart", "roundabout", "arrive", "depart", "arrive"])
        self.assertEqual([s["leg"] for s in got["steps"]], [1, 1, 1, 2, 2])
        self.assertEqual([s["offset"] for s in got["steps"]], [0, 900, 1000, 1000, 1500])
        s = got["steps"][1]
        self.assertEqual((s["name"], s["ref"], s["modifier"], s["exit"]), ("Laugavegur", "1", "right", 3))
        self.assertEqual((s["distance"], s["duration"], s["bearingBefore"], s["bearingAfter"]), (100, 10, 90, 180))
        self.assertEqual(s["location"], {"lat": 64.1, "lon": -21.89})
        self.assertEqual(s["lanes"], turn["intersections"][0]["lanes"], "only the maneuver's lanes")
        self.assertEqual(got["legs"], [[1000.0, 100.0], [500.0, 50.0]])

    def test_missing_or_bad_steps_do_not_lose_a_route(self):
        data = {"code": "Ok", "routes": [{"distance": 1000, "duration": 60,
            "geometry": {"coordinates": [[7, 45], [7.01, 45]]}, "legs": [{"distance": 1000}]}]}
        self.assertNotIn("steps", routing.parse(data), "older/step-less responses stay usable")
        data["routes"][0]["legs"][0]["steps"] = [None, {},
            {"distance": 10, "maneuver": {"type": "turn", "location": [7]}},
            {"distance": 20, "maneuver": {"type": "turn", "location": [7, float('nan')]}},
            {"distance": 30, "duration": 2, "maneuver": {"type": "future maneuver", "location": [7, 45]}}]
        got = routing.parse(data)
        self.assertEqual(len(got["steps"]), 1)
        self.assertEqual(got["steps"][0]["type"], "future maneuver")
        self.assertEqual(got["steps"][0]["offset"], 30)
        self.assertEqual(len(got["points"]), 2)


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
