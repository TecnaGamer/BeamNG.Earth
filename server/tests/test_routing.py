"""routing: the GPS's whole-route planning (OSRM response handling, simplification)."""
import json
import sys
import unittest
import numpy as np
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import netfetch  # noqa: E402
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


class _FakeNet:
    """answers: bytes (a 200's body) or an exception to raise, in order."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def get(self, url, *, kind="demand", **kw):
        self.calls += 1
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return SimpleNamespace(body=a)


OSRM_OK = json.dumps({"code": "Ok", "routes": [{"distance": 10.0, "duration": 1.0, "geometry": {
    "coordinates": [[-76.59, 39.28], [-76.60, 39.29]]}}]}).encode()


class RateLimitRetryTest(unittest.TestCase):
    """GitHub #1027: a 429 from OSRM dropped the whole GPS route."""

    def _rl(self, retry_after=0.0):
        return netfetch.RateLimited("routing.openstreetmap.de: HTTP 429", "u", "routing.openstreetmap.de",
                                    retry_after=retry_after, status=429)

    def test_429_is_retried_once_after_a_short_wait(self):
        net = _FakeNet(self._rl(), OSRM_OK)
        with mock.patch("routing.time") as t:
            r = routing.plan(39.28, -76.59, 39.29, -76.60, net=net)
        self.assertEqual(len(r["points"]), 2)
        self.assertEqual(net.calls, 2)
        t.sleep.assert_called_once_with(routing.RETRY_429_S)

    def test_retry_after_is_honoured_when_short(self):
        net = _FakeNet(self._rl(retry_after=3.0), OSRM_OK)
        with mock.patch("routing.time") as t:
            routing.plan(39.28, -76.59, 39.29, -76.60, net=net)
        t.sleep.assert_called_once_with(3.0)

    def test_long_retry_after_is_not_waited_for(self):
        net = _FakeNet(self._rl(retry_after=60.0), OSRM_OK)
        with mock.patch("routing.time") as t:
            with self.assertRaises(netfetch.RateLimited):
                routing.plan(39.28, -76.59, 39.29, -76.60, net=net)
        self.assertEqual(net.calls, 1)
        t.sleep.assert_not_called()

    def test_superseded_route_is_not_retried(self):
        net = _FakeNet(self._rl(), OSRM_OK)
        with mock.patch("routing.time") as t:
            with self.assertRaises(netfetch.RateLimited):
                routing.plan(39.28, -76.59, 39.29, -76.60, net=net, still_wanted=lambda: False)
        self.assertEqual(net.calls, 1)
        t.sleep.assert_not_called()

    def test_superseded_during_the_wait_is_not_retried(self):
        net = _FakeNet(self._rl(), OSRM_OK)
        wanted = iter([True, False])
        with mock.patch("routing.time") as t:
            with self.assertRaises(netfetch.RateLimited):
                routing.plan(39.28, -76.59, 39.29, -76.60, net=net, still_wanted=lambda: next(wanted))
        self.assertEqual(net.calls, 1)
        t.sleep.assert_called_once()

    def test_second_429_gives_up(self):
        net = _FakeNet(self._rl(), self._rl())
        with mock.patch("routing.time"):
            with self.assertRaises(netfetch.RateLimited):
                routing.plan(39.28, -76.59, 39.29, -76.60, net=net)
        self.assertEqual(net.calls, 2)


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
