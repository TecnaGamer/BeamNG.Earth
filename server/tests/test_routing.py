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

    def test_url(self):
        u = routing.url(39.28, -76.59, 38.9, -77.03)
        self.assertIn("-76.590000,39.280000;-77.030000,38.900000", u)
        self.assertTrue(u.startswith("https://routing.openstreetmap.de/"))
        self.assertNotIn("bearings", u)
        self.assertEqual(u.count("steps=true"), 1)    # (always: the GPS minimap's turn-by-turn, routing.turns)
        self.assertTrue(routing.url(39.28, -76.59, 38.9, -77.03, 370).endswith("&bearings=10,60;"))
        self.assertEqual(routing.url(39.28, -76.59, 38.9, -77.03, steps=True).count("steps=true"), 1)   # (not twice)
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


def step(kind, mod=None, lon=-76.6, lat=39.28, **kw):
    m = {"type": kind, "location": [lon, lat]}
    if mod:
        m["modifier"] = mod
    m.update(kw.pop("maneuver", {}))
    return dict(maneuver=m, **kw)


class TurnsTest(unittest.TestCase):
    """The GPS's turn-by-turn from OSRM's steps: only where the driver does something, with the card's text."""

    def test_texts(self):
        t = routing.turn_text
        self.assertEqual(t(step("turn", "left", name="Penn Avenue")), "Turn left onto Penn Avenue")
        self.assertEqual(t(step("turn", "slight right", name="Main St")), "Bear right onto Main St")
        self.assertEqual(t(step("turn", "sharp left")), "Turn sharp left")
        self.assertEqual(t(step("end of road", "right", name="", ref="US 30")), "Turn right onto US 30")
        self.assertEqual(t(step("turn", "left", name="Penn Ave", ref="PA 8")), "Turn left onto Penn Ave (PA 8)")
        self.assertEqual(t(step("off ramp", "slight right", exits="34", destinations="I 376: Oak Ridge, Fairview")),
                         "Take exit 34 toward Oak Ridge")
        self.assertEqual(t(step("off ramp", "slight right")), "Take the exit on the right")
        self.assertEqual(t(step("on ramp", "left", destinations="Harbor Tunnel")), "Take the ramp toward Harbor Tunnel")
        self.assertEqual(t(step("fork", "slight left", name="I-76")), "Keep left onto I-76")
        self.assertEqual(t(step("merge", "slight left", name="I-76")), "Merge onto I-76")
        self.assertEqual(t(step("roundabout", "right", name="Elm St", maneuver={"exit": 2})),
                         "At the roundabout, take the 2nd exit onto Elm St")
        self.assertEqual(t(step("roundabout", "right", maneuver={"exit": 5})), "At the roundabout, take the 5th exit")
        self.assertEqual(t(step("continue", "uturn")), "Make a U-turn")
        self.assertEqual(t(step("arrive")), "Arrive at your destination")

    def test_only_the_steps_that_need_doing(self):
        route = {"legs": [{"steps": [
            step("depart", lon=-76.0), step("new name", "straight", name="Broadway"),
            step("continue", "straight", name="Broadway"), step("turn", "right", name="Elm", lon=-76.1, lat=39.1),
            step("continue", "slight left", name="Oak", lon=-76.2), step("notification", "straight"),
            step("arrive", lon=-76.3, lat=39.3)]}]}
        for i, st in enumerate(route["legs"][0]["steps"]):
            st["distance"] = 100.0 * (i + 1)        # 100, 200, ... m from each maneuver to the next
        got = routing.turns(route)
        self.assertEqual([g["arrow"] for g in got], ["right", "slight-left", "arrive"])
        self.assertEqual([g["at"] for g in got], [600.0, 1000.0, 2100.0], "m along the route to each maneuver")
        self.assertEqual((got[0]["lat"], got[0]["lon"]), (39.1, -76.1))
        self.assertEqual(got[0]["text"], "Turn right onto Elm")

    def test_arrows(self):
        a = routing._arrow
        self.assertEqual([a("turn", "sharp right"), a("roundabout", "left"), a("arrive", None), a("fork", "weird")],
                         ["sharp-right", "roundabout", "arrive", "straight"])

    def test_parse_carries_the_turns(self):
        data = {"code": "Ok", "routes": [{"distance": 500.0, "duration": 60.0,
                                         "geometry": {"coordinates": [[-76.59, 39.28], [-76.60, 39.28]]},
                                         "legs": [{"steps": [step("turn", "left", name="A"), step("arrive")]}]}]}
        self.assertEqual([t["arrow"] for t in routing.parse(data)["turns"]], ["left", "arrive"])
        self.assertIn("steps=true", routing.url(39.28, -76.59, 38.9, -77.03))


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
