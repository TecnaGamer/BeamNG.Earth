"""The phone's search and route (server/phoneapi.py): the saved places of the user folder, the merge with Photon,
the distances, and the /api/search and /api/route answers on the map's http endpoint (maptiles.respond delegates)."""
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

import asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import maptiles  # noqa: E402
import netfetch  # noqa: E402
import phoneapi  # noqa: E402

PLACES = {"version": 1, "nextId": 4, "places": [
    {"id": 1, "name": "Home", "lat": 39.2904, "lon": -76.6122},
    {"id": 2, "name": "Main Street Diner", "lat": 39.2950, "lon": -76.6000},
    {"id": 3, "name": "Broken", "lat": "x", "lon": None}]}     # (dropped: no numbers)

PHOTON = {"features": [
    {"geometry": {"coordinates": [-76.6100, 39.2910]}, "properties": {"name": "Main Street", "city": "Baltimore"}},
    {"geometry": {"coordinates": [-77.0365, 38.8977]}, "properties": {"name": "White House", "city": "Washington"}}]}

OSRM = {"code": "Ok", "routes": [{
    "distance": 500.0, "duration": 60.0,
    "geometry": {"coordinates": [[-76.6122, 39.2904], [-76.6000, 39.2950]]},
    "legs": [{"steps": [{"distance": 0.0, "duration": 0.0, "name": "", "maneuver": {"type": "depart"}},
                        {"distance": 500.0, "duration": 60.0, "name": "Main Street",
                         "maneuver": {"type": "turn", "modifier": "left"}}]}]}]}


class FakeNet:
    """netfetch's .get(url, kind) -> .body: the Photon answer, the OSRM answer, or a failure."""

    def __init__(self, photon=PHOTON, osrm=OSRM, fail=None):
        self.photon, self.osrm, self.fail, self.calls = photon, osrm, fail, []

    def get(self, url, kind="demand"):
        self.calls.append(url)
        if self.fail:
            raise netfetch.FetchError(self.fail)

        class Reply:
            body = json.dumps(self.osrm if "/route/v1/" in url else self.photon).encode()
        return Reply()


class PhoneApiTest(unittest.TestCase):
    def setUp(self):
        self.user_dir = tempfile.mkdtemp(prefix="bngphone")
        os.makedirs(os.path.join(self.user_dir, "settings"))
        with open(phoneapi.places_file(self.user_dir), "w", encoding="utf-8") as f:
            json.dump(PLACES, f)

    def tearDown(self):
        shutil.rmtree(self.user_dir, ignore_errors=True)

    def test_bookmarks(self):
        self.assertEqual([p["name"] for p in phoneapi.bookmarks(self.user_dir)], ["Home", "Main Street Diner"])
        self.assertEqual(phoneapi.bookmarks(tempfile.mkdtemp()), [], "no file: no places")
        empty = tempfile.mkdtemp()
        os.makedirs(os.path.join(empty, "settings"))
        with open(phoneapi.places_file(empty), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(phoneapi.bookmarks(empty), [], "an unreadable file is not fatal")
        shutil.rmtree(empty, ignore_errors=True)

    def test_matches(self):
        self.assertTrue(phoneapi.matches("main", "Main Street Diner"))
        self.assertTrue(phoneapi.matches("main din", "Main Street Diner"), "word prefixes, any order")
        self.assertFalse(phoneapi.matches("mains", "Main Street Diner"))
        self.assertFalse(phoneapi.matches("", "Main Street Diner"))

    def test_search_saved_first_then_photon(self):
        results, error = phoneapi.search("main", 39.29, -76.61, self.user_dir, FakeNet())
        self.assertIsNone(error)
        self.assertEqual([r["name"] for r in results][:2], ["Main Street Diner", "Main Street"], "saved places first")
        self.assertEqual(results[0]["source"], "saved")
        self.assertEqual(results[1]["source"], "osm")
        self.assertLess(results[0]["distance_km"], 2.0)
        self.assertEqual(results[0]["lat"], 39.295)
        self.assertEqual(set(results[0]), {"name", "lat", "lon", "distance_km", "detail", "source"})

    def test_search_drops_a_photon_hit_that_is_a_saved_place(self):
        photon = {"features": [{"geometry": {"coordinates": [-76.6122, 39.2904]},
                                "properties": {"name": "Home", "city": "Baltimore"}}]}
        results, _ = phoneapi.search("home", None, None, self.user_dir, FakeNet(photon=photon))
        self.assertEqual([r["name"] for r in results], ["Home"])
        self.assertEqual(results[0]["source"], "saved")

    def test_search_without_a_position_has_no_distance(self):
        results, _ = phoneapi.search("home", None, None, self.user_dir, FakeNet())
        self.assertIsNone(results[0]["distance_km"])

    def test_search_survives_a_dead_network(self):
        results, error = phoneapi.search("home", 39.29, -76.61, self.user_dir, FakeNet(fail="offline"))
        self.assertEqual([r["name"] for r in results], ["Home"], "the saved places still answer")
        self.assertIn("offline", error)

    def test_search_limit(self):
        results, _ = phoneapi.search("main", None, None, self.user_dir, FakeNet(), limit=1)
        self.assertEqual(len(results), 1)

    def test_search_sorts_photon_hits_by_distance(self):
        photon = {"features": [
            {"geometry": {"coordinates": [-77.0365, 38.8977]}, "properties": {"name": "Far Place", "city": "DC"}},
            {"geometry": {"coordinates": [-76.6101, 39.2910]}, "properties": {"name": "Near Place",
                                                                              "city": "Baltimore"}}]}
        results, _ = phoneapi.search("place", 39.29, -76.61, self.user_dir, FakeNet(photon=photon))
        self.assertEqual([r["name"] for r in results], ["Near Place", "Far Place"], "nearest suggestion first")

    def test_search_biases_photon_at_the_car(self):
        net = FakeNet()
        phoneapi.search("main", 39.29, -76.61, self.user_dir, net)
        last = net.calls[-1]
        self.assertIn("lat=39.2900", last)
        self.assertIn("lon=-76.6100", last)
        self.assertIn("zoom=12", last, "a bias area around the car")
        self.assertIn("location_bias_scale=0.40", last)
        net2 = FakeNet()
        phoneapi.search("main", None, None, self.user_dir, net2)
        self.assertNotIn("zoom=", net2.calls[-1], "no position: the plain query")

    def test_cached_search_asks_photon_once(self):
        clock = [100.0]
        net = FakeNet()
        first, _ = phoneapi.search_cached("diner", 39.29, -76.61, self.user_dir, net, clock=lambda: clock[0])
        calls = len(net.calls)
        clock[0] += 5.0
        again, _ = phoneapi.search_cached("diner", 39.29, -76.61, self.user_dir, net, clock=lambda: clock[0])
        self.assertEqual(len(net.calls), calls, "inside CACHE_S: no second request")
        self.assertEqual(first, again)
        clock[0] += phoneapi.CACHE_S + 1
        phoneapi.search_cached("diner", 39.29, -76.61, self.user_dir, net, clock=lambda: clock[0])
        self.assertEqual(len(net.calls), calls + 1, "a stale answer is asked again")

    def test_reverse_names_a_point(self):
        rev = {"features": [{"geometry": {"coordinates": [-76.61, 39.29]},
                             "properties": {"name": "Calle Mayor", "housenumber": "5", "city": "Cáceres"}}]}
        got = phoneapi.reverse(39.29, -76.61, FakeNet(photon=rev))
        self.assertEqual(got["name"], "Calle Mayor 5")
        self.assertTrue(got["detail"])

    def test_reverse_falls_back_to_the_coordinates(self):
        got = phoneapi.reverse(39.29412, -76.61522, FakeNet(fail="offline"))
        self.assertEqual(got["name"], "39.29412, -76.61522")
        self.assertEqual((got["lat"], got["lon"]), (39.29412, -76.61522))

    def test_distance(self):
        self.assertAlmostEqual(phoneapi.distance_km(39.2904, -76.6122, 39.2950, -76.6000), 1.16, places=1)


class PhoneHttpTest(unittest.TestCase):
    def setUp(self):
        self.user_dir = tempfile.mkdtemp(prefix="bngphone")
        os.makedirs(os.path.join(self.user_dir, "settings"))
        with open(phoneapi.places_file(self.user_dir), "w", encoding="utf-8") as f:
            json.dump(PLACES, f)
        self.api = phoneapi.PhoneApi(self.user_dir, FakeNet())
        self.tiles = maptiles.MapTiles(None, api=self.api)

    def tearDown(self):
        shutil.rmtree(self.user_dir, ignore_errors=True)

    def get(self, path):
        return self.tiles.respond("GET", path)

    def test_search(self):
        status, ctype, body = self.get("/api/search?q=main&lat=39.29&lon=-76.61")
        self.assertEqual((status, ctype), (200, "application/json"))
        got = json.loads(body)
        self.assertIsInstance(got, list, "a JSON array, as the phone expects")
        self.assertEqual(got[0]["name"], "Main Street Diner")
        self.assertEqual(round(got[0]["distance_km"], 1), 1.0)

    def test_search_needs_q(self):
        status, _, body = self.get("/api/search")
        self.assertEqual(status, 400)
        self.assertIn("q", json.loads(body)["error"])

    def test_search_lat_without_lon(self):
        self.assertEqual(self.get("/api/search?q=main&lat=39.29")[0], 400)

    def test_search_upstream_down(self):
        self.tiles.api = phoneapi.PhoneApi(self.user_dir, FakeNet(fail="offline"))
        status, _, body = self.get("/api/search?q=white+house")
        self.assertEqual(status, 503)
        self.assertIn("offline", json.loads(body)["error"])

    def test_route(self):
        status, ctype, body = self.get("/api/route?lat0=39.2904&lon0=-76.6122&lat1=39.2950&lon1=-76.6")
        self.assertEqual((status, ctype), (200, "application/json"))
        got = json.loads(body)
        self.assertEqual(got["dist"], 500.0)
        self.assertEqual(got["steps"][1]["name"], "Main Street")
        self.assertEqual(got["steps"][1]["type"], "turn")

    def test_route_needs_four_numbers(self):
        self.assertEqual(self.get("/api/route?lat0=39.29&lon0=-76.61")[0], 400)
        self.assertEqual(self.get("/api/route?lat0=39.29&lon0=x&lat1=1&lon1=2")[0], 400)

    def test_unknown_api_and_other_paths(self):
        self.assertEqual(self.get("/api/nope")[0], 404)
        self.assertIsNone(self.api.respond("GET", "/tiles/1/2/3.jpg"), "not an api path: the tiles answer")
        self.assertEqual(self.get("/health")[0], 200, "the map's own endpoints still answer")

    def test_post_is_refused(self):
        status, _, _ = self.tiles.respond("POST", "/api/search?q=main")
        self.assertEqual(status, 405)

    def test_reverse(self):
        rev = {"features": [{"geometry": {"coordinates": [-76.61, 39.29]},
                             "properties": {"name": "Main Street", "city": "Baltimore"}}]}
        self.tiles.api = phoneapi.PhoneApi(self.user_dir, FakeNet(photon=rev))
        status, ctype, body = self.get("/api/reverse?lat=39.29&lon=-76.61")
        self.assertEqual((status, ctype), (200, "application/json"))
        self.assertEqual(json.loads(body)["name"], "Main Street")
        self.assertEqual(self.get("/api/reverse?lat=39.29")[0], 400)


class SocketHandlersTest(unittest.TestCase):
    """The mod's side of the socket: Connection.phone_search and Connection.route with steps (earthd.py)."""

    class Conn:
        """Connection's route()/phone_search() with a fake server (route_download is upstream's corridor prefetch)."""

        def __init__(self, user_dir):
            self.s = types.SimpleNamespace(user_dir=user_dir, net=FakeNet(), ui_pool=None)
            self.sent = []
            self.downloads = []
            self.route_dl_version = 0

        def send(self, msg):
            self.sent.append(msg)

        def route_download(self, version, points):
            self.downloads.append((version, len(points)))

    def setUp(self):
        self.user_dir = tempfile.mkdtemp(prefix="bngphone")
        os.makedirs(os.path.join(self.user_dir, "settings"))
        with open(phoneapi.places_file(self.user_dir), "w", encoding="utf-8") as f:
            json.dump(PLACES, f)

    def tearDown(self):
        shutil.rmtree(self.user_dir, ignore_errors=True)

    def test_phone_search_message(self):
        import earthd
        c = self.Conn(self.user_dir)
        asyncio.run(earthd.Connection.phone_search(c, {"type": "phone_search", "id": 7, "q": "main",
                                                       "lat": 39.29, "lon": -76.61}))
        out = c.sent[0]
        self.assertEqual((out["type"], out["id"]), ("phone_search_result", 7))
        self.assertEqual([r["name"] for r in out["results"]][:2], ["Main Street Diner", "Main Street"])

    def test_reverse_message(self):
        import earthd
        c = self.Conn(self.user_dir)
        c.s.net = FakeNet(photon={"features": [{"geometry": {"coordinates": [-76.61, 39.29]},
                                                "properties": {"name": "Main Street"}}]})
        asyncio.run(earthd.Connection.phone_reverse(c, {"type": "phone_reverse", "id": 9, "lat": 39.29,
                                                        "lon": -76.61}))
        out = c.sent[0]
        self.assertEqual((out["type"], out["id"]), ("phone_reverse_result", 9))
        self.assertEqual(out["name"], "Main Street")

    def test_route_message_carries_the_steps(self):
        import earthd
        c = self.Conn(self.user_dir)
        asyncio.run(earthd.Connection.route(c, {"type": "route", "id": 3, "lat0": 39.2904, "lon0": -76.6122,
                                                "lat1": 39.2950, "lon1": -76.6, "steps": True}))
        out = c.sent[0]
        self.assertEqual(out["type"], "route_result")
        self.assertEqual(out["steps"][1]["name"], "Main Street")
        self.assertIn("&steps=true", c.s.net.calls[-1], "one OSRM request, with the steps")

    def test_route_without_steps_is_the_gps_route(self):
        import earthd
        c = self.Conn(self.user_dir)
        asyncio.run(earthd.Connection.route(c, {"type": "route", "id": 4, "lat0": 39.2904, "lon0": -76.6122,
                                                "lat1": 39.2950, "lon1": -76.6}))
        # (merged with the GPS minimap's turn-by-turn, 2026-10-27: every route carries OSRM's steps, asked for once)
        self.assertEqual(c.s.net.calls[-1].count("steps=true"), 1, "the map's GPS route asks for them once")

    def test_route_alternatives_are_tagged(self):
        import earthd
        two = json.loads(json.dumps(OSRM))
        second = json.loads(json.dumps(OSRM["routes"][0]))
        second["distance"] = 610.0
        second["duration"] = 90.0
        second["geometry"]["coordinates"] = [[-76.6122, 39.2904], [-76.5980, 39.2970]]
        two["routes"].append(second)
        c = self.Conn(self.user_dir)
        c.s.net = FakeNet(osrm=two)
        asyncio.run(earthd.Connection.route(c, {"type": "route", "id": 5, "lat0": 39.2904, "lon0": -76.6122,
                                                "lat1": 39.2950, "lon1": -76.6, "steps": True,
                                                "alternatives": 3}))
        out = c.sent[0]
        self.assertEqual(len(out["routes"]), 2, "both routes come back for the phone's chips")
        self.assertIn("&alternatives=3", c.s.net.calls[-1])
        self.assertEqual([r["tag"] for r in out["routes"]], ["Fastest", "Route 2"],
                         "the quicker one is named, the other is the second route")


if __name__ == "__main__":
    unittest.main()
