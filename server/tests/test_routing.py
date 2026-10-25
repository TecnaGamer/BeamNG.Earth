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
        self.assertTrue(routing.url(39.28, -76.59, 38.9, -77.03, 370).endswith("&bearings=10,60;"))


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
