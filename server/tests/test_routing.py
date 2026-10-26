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

    def test_url(self):
        u = routing.url(39.28, -76.59, 38.9, -77.03)
        self.assertIn("-76.590000,39.280000;-77.030000,38.900000", u)
        self.assertTrue(u.startswith("https://routing.openstreetmap.de/"))
        self.assertNotIn("bearings", u)
        self.assertNotIn("steps", u)
        self.assertTrue(routing.url(39.28, -76.59, 38.9, -77.03, 370).endswith("&bearings=10,60;"))
        self.assertIn("&steps=true", routing.url(39.28, -76.59, 38.9, -77.03, steps=True))
        self.assertTrue(routing.url(39.28, -76.59, 38.9, -77.03, 370, True).endswith("&bearings=10,60;"))

    def test_parse_steps(self):
        def step(dist, name, kind, mod):
            return {"distance": dist, "duration": dist / 10.0, "name": name,
                    "maneuver": {"type": kind, "modifier": mod, "location": [-76.59, 39.28]}}
        data = {"code": "Ok", "routes": [{
            "distance": 300.0, "duration": 30.0,
            "geometry": {"coordinates": [[-76.59, 39.28], [-76.60, 39.28], [-76.60, 39.29]]},
            "legs": [{"steps": [step(0.0, "", "depart", ""), step(120.0, "Main Street", "turn", "left"),
                                step(180.0, "", "arrive", "")]}]}]}
        r = routing.parse(data)
        self.assertEqual([s["at"] for s in r["steps"]], [0.0, 0.0, 120.0], "cumulative metres along the route")
        self.assertEqual([s["dist"] for s in r["steps"]], [0.0, 120.0, 180.0])
        self.assertEqual(r["steps"][1], {"at": 0.0, "dist": 120.0, "dur": 12.0, "name": "Main Street",
                                         "type": "turn", "modifier": "left"})
        self.assertNotIn("steps", routing.parse({"code": "Ok", "routes": [{
            "distance": 1.0, "duration": 1.0, "geometry": {"coordinates": [[-76.59, 39.28], [-76.6, 39.28]]},
            "legs": [{"summary": "x"}]}]}), "a route without steps carries none")

    def test_alternatives_are_tagged_by_what_they_are(self):
        def route(dist, dur, ref, name="x"):
            return {"distance": dist, "duration": dur, "geometry": {"coordinates": [[-76.59, 39.28], [-76.6, 39.29]]},
                    "legs": [{"steps": [{"distance": dist, "ref": ref, "name": name}]}]}
        routes = [route(10000.0, 600.0, "A-66"),     # quickest and the one on numbered roads
                  route(9000.0, 700.0, "")]          # shortest, all lanes
        self.assertEqual(routing.main_share(routes[0]), 1.0)
        self.assertEqual(routing.main_share(routes[1]), 0.0)
        self.assertEqual([t["tag"] for t in routing.tag_alternatives(routes)], ["Fastest", "Shortest"])
        three = [route(10000.0, 600.0, ""), route(12000.0, 900.0, "A-5"), route(9000.0, 700.0, "")]
        self.assertEqual([t["tag"] for t in routing.tag_alternatives(three)], ["Fastest", "Main roads", "Shortest"])
        self.assertEqual([t["tag"] for t in routing.tag_alternatives([route(5000.0, 300.0, "A-66")])], ["Fastest"])
        self.assertEqual(routing.tag_alternatives([]), [])
        parsed = routing.parse({"code": "Ok", "routes": routes}, alternatives=3)
        self.assertEqual([r["tag"] for r in parsed["routes"]], ["Fastest", "Shortest"])
        self.assertEqual(parsed["dist"], 10000.0, "the top-level fields stay the first route")
        self.assertNotIn("routes", routing.parse({"code": "Ok", "routes": routes}), "asked for one: one")


if __name__ == "__main__":
    unittest.main()
