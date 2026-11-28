"""Live air traffic (airtraffic.py + earthfast adsb_compact / adsbdb_route): the coarse query point, the source order
and rests, the shared answer, the rows and the memory-only routes. No network: injected fetchers."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import airtraffic as at  # noqa: E402
import netfetch  # noqa: E402

NOW_MS = 1764200000000


def answer_body(rows, now=NOW_MS):
    return json.dumps({"ac": rows, "now": now, "msg": "No error"}).encode()


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class SnapTest(unittest.TestCase):
    def test_grid_and_radius(self):
        self.assertEqual(at.snap(33.6407, -84.4277), (33.75, -84.5))
        self.assertEqual(at.snap(-33.94, 151.17), (-34.0, 151.25))
        self.assertEqual(at.snap(10.0, 179.9), (10.0, -180.0))
        # the whole range round the player is inside the cell node's circle: + half the cell's diagonal (<= 10.6 NM)
        self.assertEqual(at.query_nm(60, 0.0), 71)
        self.assertEqual(at.query_nm(60, 60.0), 69)
        self.assertEqual(at.query_nm(500, 0.0), at.MAX_QUERY_NM)
        self.assertEqual(at.clamp_range("x"), at.RANGE_NM)
        self.assertEqual(at.clamp_range(1), at.RANGE_MIN_NM)
        self.assertEqual(at.clamp_range(1e9), at.RANGE_MAX_NM)

    def test_only_the_coarse_point_is_in_the_url(self):
        url = at.provider_url(at.PROVIDERS[0][2], *at.snap(33.640723, -84.427703), 71)
        self.assertEqual(url, "https://api.adsb.lol/v2/lat/33.75/lon/-84.50/dist/71")
        self.assertNotIn("33.64", url)


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.asked = []
        self.fail = {}
        self.clock, self.wall = Clock(), Clock(1764200000.0)

        def fetch(url):
            self.asked.append(url)
            for host, exc in self.fail.items():
                if host in url:
                    raise exc
            return answer_body([{"hex": "a1b2c3", "lat": 33.65, "lon": -84.43, "seen_pos": 1.0}])
        self.svc = at.Service(fetch=fetch, clock=self.clock, wall=self.wall)

    def test_adsb_lol_first_and_shared_per_slot(self):
        body, src, ms = self.svc.get(33.64, -84.43, 60)
        self.assertEqual(src, "adsb.lol")
        self.assertEqual(len(self.asked), 1)
        self.svc.get(33.70, -84.40, 60)         # the same cell, the same slot: no second ask
        self.assertEqual(len(self.asked), 1)
        self.wall.t += at.SLOT_S
        self.svc.get(33.70, -84.40, 60)
        self.assertEqual(len(self.asked), 2)

    def test_fallbacks_only_when_adsb_lol_is_limited_or_slow(self):
        self.fail["adsb.lol"] = netfetch.RateLimited("429", "u", "api.adsb.lol", retry_after=0.0, status=429)
        _b, src, _ms = self.svc.get(33.64, -84.43, 60)
        self.assertEqual(src, "airplanes.live")
        # adsb.lol rests: the next slot goes straight to the stand-in
        self.wall.t += at.SLOT_S
        self.asked.clear()
        _b, src, _ms = self.svc.get(33.64, -84.43, 60)
        self.assertEqual(src, "airplanes.live")
        self.assertEqual(len(self.asked), 1)
        # ... until its rest is over, then adsb.lol first again
        del self.fail["adsb.lol"]
        self.clock.t += at.REST_LIMITED_S + 1
        self.wall.t += at.SLOT_S
        _b, src, _ms = self.svc.get(33.64, -84.43, 60)
        self.assertEqual(src, "adsb.lol")

    def test_slow_source_then_adsb_fi(self):
        self.fail["adsb.lol"] = netfetch.FetchFailed("timed out", "u", "api.adsb.lol")
        self.fail["airplanes.live"] = netfetch.BudgetExceeded("day used", "u", "api.airplanes.live")
        _b, src, _ms = self.svc.get(33.64, -84.43, 60)
        self.assertEqual(src, "adsb.fi")
        self.assertEqual(self.svc.rest["adsb.lol"], self.clock.t + at.REST_SLOW_S)
        self.assertEqual(self.svc.rest["airplanes.live"], self.clock.t + at.REST_LIMITED_S)

    def test_403_and_503_retry_after_rest_the_source(self):
        self.fail["adsb.lol"] = netfetch.FetchFailed("HTTP 403", "u", "api.adsb.lol", status=403)
        self.fail["airplanes.live"] = netfetch.RateLimited("503", "u", "api.airplanes.live", retry_after=30.0,
                                                           status=503)
        _b, src, _ms = self.svc.get(33.64, -84.43, 60)
        self.assertEqual(src, "adsb.fi")
        self.assertEqual(self.svc.rest["adsb.lol"], self.clock.t + at.REST_LIMITED_S)
        self.assertEqual(self.svc.rest["airplanes.live"], self.clock.t + at.REST_LIMITED_S)

    def test_our_own_limiter_does_not_reach_for_the_stand_ins(self):
        self.fail["adsb.lol"] = netfetch.RateLimited("rate limited for 9 s", "u", "api.adsb.lol", retry_after=9.0)
        with self.assertRaises(IOError):
            self.svc.get(33.64, -84.43, 60)
        self.assertEqual(len(self.asked), 1)
        self.assertNotIn("adsb.lol", self.svc.rest)

    def test_every_source_down(self):
        for h in ("adsb.lol", "airplanes.live", "adsb.fi"):
            self.fail[h] = netfetch.FetchFailed("down", "u", h)
        with self.assertRaises(IOError) as cm:
            self.svc.get(33.64, -84.43, 60)
        self.assertIn("adsb.fi", str(cm.exception))


class AnswerTest(unittest.TestCase):
    def setUp(self):
        self.wall = Clock(NOW_MS / 1000.0 + 5.0)
        rows = [{"hex": "a1b2c3", "flight": "DAL1234 ", "t": "A321", "lat": 33.65, "lon": -84.43, "alt_baro": 3000,
                 "gs": 160, "track": 90, "seen_pos": 2.0},
                {"hex": "d4e5f6", "flight": "N123AB", "lat": 33.66, "lon": -84.44, "alt_baro": "ground", "gs": 0},
                {"hex": "ffffff", "lat": 36.0, "lon": -84.4, "seen_pos": 1.0}]       # 260 km off: past 60 NM
        self.svc = at.Service(fetch=lambda url: answer_body(rows), clock=Clock(), wall=self.wall)
        self.route_asks = []

        def route_fetch(url):
            self.route_asks.append(url)
            return json.dumps({"response": {"flightroute": {"origin": {"iata_code": "ATL"},
                                                            "destination": {"iata_code": "JFK",
                                                                            "municipality": "New York"}}}}).encode()
        self.routes = at.Routes(fetch=route_fetch, clock=Clock(), start=False)

    def test_rows_are_the_players_range_with_times(self):
        out = at.answer(self.svc, None, {"type": "air_traffic", "id": 3, "lat": 33.64, "lon": -84.43, "rangeNm": 60})
        self.assertTrue(out["ok"])
        self.assertEqual(out["type"], "air_traffic_result")
        self.assertEqual(out["id"], 3)
        self.assertEqual(out["src"], "adsb.lol")
        self.assertIn("adsb.lol", out["credit"])
        self.assertEqual([r["h"] for r in out["ac"]], ["a1b2c3", "d4e5f6"])
        self.assertEqual(out["ac"][0]["ts"], NOW_MS - 2000)
        self.assertEqual(out["ac"][0]["f"], "DAL1234")
        self.assertTrue(out["ac"][1]["g"])
        self.assertEqual(out["srvNow"], self.wall.t * 1000.0)
        self.assertNotIn("o", out["ac"][0])          # routes off: none asked, none attached

    def test_routes_only_with_their_switch_and_in_memory(self):
        msg = {"id": 1, "lat": 33.64, "lon": -84.43, "routes": True}
        at.answer(self.svc, self.routes, msg)
        self.assertEqual(list(self.routes.queue), ["DAL1234"])      # (N123AB is a registration, not an airline)
        self.assertTrue(self.routes.step())
        self.assertEqual(self.route_asks, ["https://api.adsbdb.com/v0/callsign/DAL1234"])
        out = at.answer(self.svc, self.routes, msg)
        self.assertEqual((out["ac"][0]["o"], out["ac"][0]["d"], out["ac"][0]["dn"]), ("ATL", "JFK", "New York"))
        self.assertIn(at.ROUTE_CREDIT, out["credit"])
        at.answer(self.svc, self.routes, msg)
        self.assertFalse(self.routes.step())                         # known: not asked again
        self.assertEqual(len(self.route_asks), 1)

    def test_routes_off_clears_the_queue_and_stale_wants_are_dropped(self):
        msg = {"id": 1, "lat": 33.64, "lon": -84.43, "routes": True}
        at.answer(self.svc, self.routes, msg)
        self.assertEqual(list(self.routes.queue), ["DAL1234"])
        at.answer(self.svc, self.routes, dict(msg, routes=False))      # routes turned off: nothing left queued
        self.assertFalse(self.routes.step())
        self.assertEqual(self.route_asks, [])
        at.answer(self.svc, self.routes, msg)
        self.routes.clock.t += at.WANT_FRESH_S + 1                       # the mod stopped asking
        self.assertFalse(self.routes.step())
        self.assertEqual(self.route_asks, [])

    def test_route_lookups_follow_the_cell_not_the_player(self):
        rows = [{"hex": "%06x" % i, "flight": cs, "lat": 33.75 + d, "lon": -84.5, "seen_pos": 1.0}
                for i, (cs, d) in enumerate((("UAL9", 0.6), ("AAL1", 0.01), ("DAL5", 0.3)))]
        svc = at.Service(fetch=lambda url: answer_body(rows), clock=Clock(), wall=self.wall)
        # the player at the cell's edge, nearest to UAL9: still looked up in callsign order
        at.answer(svc, self.routes, {"lat": 33.86, "lon": -84.5, "rangeNm": 60, "routes": True})
        self.assertEqual(list(self.routes.queue), ["AAL1", "DAL5", "UAL9"])

    def test_bad_requests(self):
        for msg in ({}, {"lat": "x", "lon": 1}, {"lat": 95, "lon": 0}, {"lat": float("nan"), "lon": 0}):
            out = at.answer(self.svc, None, msg)
            self.assertFalse(out["ok"])
            self.assertIn("bad request", out["error"])

    def test_a_bad_answer_is_an_error_not_a_crash(self):
        svc = at.Service(fetch=lambda url: b"<html>busy</html>", clock=Clock(), wall=self.wall)
        out = at.answer(svc, None, {"lat": 33.64, "lon": -84.43})
        self.assertFalse(out["ok"])
        self.assertIn("JSON", out["error"])


class PolicyTest(unittest.TestCase):
    def test_strict_policies_for_every_source(self):
        for _name, host, _url, credit in at.PROVIDERS:
            p = netfetch.POLICIES[host]
            self.assertLessEqual(p.rate, 1 / 12)
            self.assertEqual(p.burst, 1.0)
            self.assertIsNotNone(p.daily_hard)
            self.assertLessEqual(p.daily_hard, 8000)
            self.assertTrue(p.optional)
            self.assertEqual(p.cache_ttl, 0)
            self.assertTrue(credit)
        self.assertIn("https://adsb.fi", at.CREDITS["adsb.fi"])
        p = netfetch.POLICIES["api.adsbdb.com"]
        self.assertLessEqual(p.rate, 0.5)
        self.assertEqual(p.cache_ttl, 0)


if __name__ == "__main__":
    unittest.main()
