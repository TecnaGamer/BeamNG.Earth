"""earthd.Connection.route asks again after the routing host's 429 while the destination is still the latest one
(GitHub #1027: a destination changed twice in 2 s got no route until it was set again), and a failed re-plan of the
saved route keeps its points (a rate-limited off-route re-plan wiped them while the map kept drawing the route, so
"Pre-cache route" said "no route: plan one with the GPS first")."""
import asyncio
import concurrent.futures
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import earthd  # noqa: E402
import netfetch  # noqa: E402


def connection():
    sent = []
    c = types.SimpleNamespace(route_dl_version=0, route_points=[], route_dest=None, s=types.SimpleNamespace(
        ui_pool=concurrent.futures.ThreadPoolExecutor(1), net=None, dem=None), sent=sent, downloads=[])
    c.send = sent.append
    c.route_download = lambda version, points, mode: c.downloads.append(points)
    return c, sent


class RouteRetryTest(unittest.TestCase):
    def setUp(self):
        self.plan, self.wait = earthd.routingmod.plan, earthd.ROUTE_429_WAIT_S
        earthd.ROUTE_429_WAIT_S = 0.01

    def tearDown(self):
        earthd.routingmod.plan, earthd.ROUTE_429_WAIT_S = self.plan, self.wait

    def answers(self, *got):
        calls = []

        def plan(*a):
            calls.append(a)
            r = got[min(len(calls), len(got)) - 1]
            if isinstance(r, Exception):
                raise r
            return r
        earthd.routingmod.plan = plan
        return calls

    MSG = {"id": 2, "lat0": 63.77, "lon0": -18.09, "lat1": 64.05, "lon1": -16.18}

    def test_a_429_is_asked_again(self):
        calls = self.answers(netfetch.RateLimited("HTTP 429"), {"points": [[63.77, -18.09], [64.05, -16.18]],
                                                                  "dist": 130100.0, "dur": 5000.0})
        c, sent = connection()
        asyncio.run(earthd.Connection.route(c, dict(self.MSG)))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(sent[0]["points"]), 2)
        self.assertNotIn("error", sent[0])

    def test_other_errors_are_not(self):
        calls = self.answers(netfetch.FetchFailed("HTTP 500"))
        c, sent = connection()
        asyncio.run(earthd.Connection.route(c, dict(self.MSG)))
        self.assertEqual(len(calls), 1)
        self.assertIn("error", sent[0])

    def test_maneuvers_reach_the_client_unchanged(self):
        steps = [{"type": "roundabout", "exit": 3, "name": "Main Road", "leg": 1, "offset": 400,
                  "location": {"lat": 64.05, "lon": -16.18}}]
        self.answers({"points": [[63.77, -18.09], [64.05, -16.18]], "dist": 130100, "dur": 5000, "steps": steps})
        c, sent = connection()
        asyncio.run(earthd.Connection.route(c, dict(self.MSG)))
        self.assertEqual(sent[0]["steps"], steps)

    def test_it_gives_up_after_its_tries(self):
        calls = self.answers(netfetch.RateLimited("HTTP 429"))
        c, sent = connection()
        asyncio.run(earthd.Connection.route(c, dict(self.MSG)))
        self.assertEqual(len(calls), earthd.ROUTE_429_TRIES)
        self.assertEqual(sent[0]["points"], [])
        self.assertIn("RateLimited", sent[0]["error"])

    PLANNED = {"points": [[63.77, -18.09], [63.9, -17.1], [64.05, -16.18]], "dist": 130100.0, "dur": 5000.0}

    def test_a_failed_replan_of_the_same_route_keeps_its_points(self):
        self.answers(self.PLANNED, netfetch.RateLimited("HTTP 429"))
        c, sent = connection()
        asyncio.run(earthd.Connection.route(c, dict(self.MSG)))
        again = dict(self.MSG, id=3, lat0=63.81, lon0=-17.95, lat1=self.MSG["lat1"] + 1e-7)  # off route, re-planned
        asyncio.run(earthd.Connection.route(c, again))
        self.assertIn("error", sent[1])
        self.assertEqual(c.route_points, self.PLANNED["points"], "the route the map still draws")
        lats, lons = earthd.Connection._area_route(c, {"route": "current"})
        self.assertEqual(len(lats), 3, "pre-cache still sees it")
        self.assertEqual(c.downloads[1], self.PLANNED["points"], "its corridor prefetch goes on")

    def test_a_failed_plan_of_a_new_destination_drops_the_route(self):
        self.answers(self.PLANNED, netfetch.FetchFailed("HTTP 500"))
        c, sent = connection()
        asyncio.run(earthd.Connection.route(c, dict(self.MSG)))
        asyncio.run(earthd.Connection.route(c, dict(self.MSG, id=3, lat1=63.3, lon1=-19.0)))
        self.assertIn("error", sent[1])
        self.assertEqual(c.route_points, [], "the mod draws no route to the new destination")
        self.assertEqual(c.route_dest, None)
        self.assertEqual(c.downloads[1], [])

    def test_a_newer_destination_answers_instead(self):
        c, sent = connection()

        def plan(*a):
            c.route_dl_version += 1             # (another route() came while this one waited)
            raise netfetch.RateLimited("HTTP 429")
        earthd.routingmod.plan = plan
        asyncio.run(earthd.Connection.route(c, dict(self.MSG)))
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
