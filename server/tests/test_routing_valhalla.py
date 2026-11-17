"""PR #1263's "Avoid highways": Valhalla's answer decoded (shape, summary, maneuvers as the GPS's turn-by-turn steps)
and routing.plan's order (Valhalla, again without the heading on "no route", OSRM; the service down: OSRM at once)."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import netfetch  # noqa: E402
import routing  # noqa: E402


def encode(pts, precision=6):
    """Google's polyline encoding (the decoder's inverse, for the tests)."""
    out, plat, plon = [], 0, 0
    for la, lo in pts:
        ila, ilo = round(la * 10 ** precision), round(lo * 10 ** precision)
        for d in (ila - plat, ilo - plon):
            v = ~(d << 1) if d < 0 else d << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = ila, ilo
    return "".join(out)


PTS = [(42.3601, -71.0589), (42.3611, -71.0570), (42.3650, -71.0500)]
TRIP = {"trip": {"status": 0, "summary": {"length": 0.9, "time": 80.0}, "legs": [{
    "shape": encode(PTS), "maneuvers": [
        {"type": 1, "street_names": ["Main St"], "length": 0.2, "time": 20, "begin_shape_index": 0},
        {"type": 10, "street_names": ["Elm St"], "length": 0.7, "time": 60, "begin_shape_index": 1},
        {"type": 4, "length": 0.0, "time": 0, "begin_shape_index": 2}]}]}}


class Resp:
    def __init__(self, data):
        self.body = json.dumps(data).encode()


class FakeNet:
    def __init__(self, answers):
        self.answers, self.urls = list(answers), []

    def get(self, url, kind=None, **_kw):
        self.urls.append(url)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return Resp(a)


OSRM = {"code": "Ok", "routes": [{"distance": 1200.0, "duration": 90.0,
                                  "geometry": {"coordinates": [[-71.0589, 42.3601], [-71.0500, 42.3650]]}}]}


class ValhallaTest(unittest.TestCase):
    def test_decode_polyline(self):
        got = routing.decode_polyline(encode(PTS))
        for (a, b), (c, d) in zip(got, PTS):
            self.assertAlmostEqual(a, c, places=6)
            self.assertAlmostEqual(b, d, places=6)
        with self.assertRaises(ValueError):
            routing.decode_polyline(encode(PTS)[:-1] + "\x7f"[:0] + "_")

    def test_parse_valhalla_with_steps(self):
        r = routing.parse_valhalla(TRIP)
        steps = r["steps"]
        self.assertEqual([s["type"] for s in steps], ["depart", "turn", "arrive"])
        self.assertEqual(steps[1]["modifier"], "right")
        self.assertEqual(steps[1]["name"], "Elm St")
        self.assertEqual(steps[1]["offset"], 200.0)
        self.assertEqual(steps[1]["location"], {"lat": 42.3611, "lon": -71.057})
        with self.assertRaises(ValueError):
            routing.parse_valhalla({"trip": {"status": 442, "legs": []}})
        with self.assertRaises(ValueError):
            routing.parse_valhalla({"error_code": 442, "error": "No path could be found for input"})
        for bad in ({"trip": {"status": 0, "legs": [{}], "summary": {}}}, {"trip": {"status": 0, "legs": [{"shape": None}],
                    "summary": {}}}, {"trip": {"status": 0, "legs": [{"shape": "~" * 50}], "summary": {}}}, {"x": 1}):
            with self.assertRaises(routing.ValhallaUnavailable, msg=bad):
                routing.parse_valhalla(bad)

    def test_decoder_bounds(self):
        for bad in ("~" * 40, "\x01\x02", "?" * (routing.SHAPE_MAX_CHARS + 1), encode([(95.0, 0.0), (0.0, 0.0)])):
            with self.assertRaises(ValueError):
                routing.decode_polyline(bad)

    def test_plan_order(self):
        net = FakeNet([TRIP])
        self.assertIs(routing.plan(42.36, -71.06, 42.365, -71.05, net, 90, avoid_highways=True)["avoided"], True)
        net = FakeNet([{"trip": {"status": 442}}, {"trip": {"status": 442}}, OSRM])
        r = routing.plan(42.36, -71.06, 42.365, -71.05, net, 90, avoid_highways=True)
        self.assertIs(r["avoided"], False)
        self.assertEqual(len(net.urls), 3, "no route: again without the heading, then OSRM")
        net = FakeNet([netfetch.FetchError("down"), OSRM])
        r = routing.plan(42.36, -71.06, 42.365, -71.05, net, 90, avoid_highways=True)
        self.assertIsNone(r["avoided"], "the service down: no 'no route' note")
        self.assertEqual(len(net.urls), 2, "the service down: OSRM at once")
        net = FakeNet([{"trip": {"status": 0, "legs": [{}], "summary": {}}}, OSRM])
        r = routing.plan(42.36, -71.06, 42.365, -71.05, net, 90, avoid_highways=True)
        self.assertIsNone(r["avoided"], "a malformed answer: the normal route, no 'no route' note")
        self.assertEqual(len(net.urls), 2)
        for bad_trip in (dict(TRIP["trip"], summary={"length": 0.9, "time": {"x": 1}}),
                         dict(TRIP["trip"], legs=[dict(TRIP["trip"]["legs"][0], maneuvers=[{"type": [1],
                                                  "length": 0.1, "time": 1, "begin_shape_index": 0}])])):
            net = FakeNet([{"trip": bad_trip}, OSRM])
            r = routing.plan(42.36, -71.06, 42.365, -71.05, net, 90, avoid_highways=True)
            self.assertIsNone(r["avoided"])
            self.assertEqual(len(net.urls), 2, "malformed field types: OSRM")
        net = FakeNet([OSRM])
        self.assertNotIn("avoided", routing.plan(42.36, -71.06, 42.365, -71.05, net, 90))


if __name__ == "__main__":
    unittest.main()
