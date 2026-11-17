"""buslines: OpenStreetMap's bus routes as lines with their stops in order (the user, 2026-11-17: "yes start the bus
lines")."""
import os
import json
import sys
import tempfile
import time
from pathlib import Path
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import buslines  # noqa: E402
import netfetch  # noqa: E402


def node(i, lat, lon, **tags):
    return {"type": "node", "id": i, "lat": lat, "lon": lon, "tags": tags}


def rel(i, members, **tags):
    tags = dict({"type": "route", "route": "bus"}, **tags)
    return {"type": "relation", "id": i, "tags": tags,
            "members": [{"type": t, "ref": r, "role": role} for t, r, role in members]}


class OverpassOnly:
    """These test the Overpass path: Göteborg's coordinates are in Västtrafik's area (gtfsbus), kept out of it."""
    def setUp(self):
        super().setUp()
        p = mock.patch.object(buslines.gtfsbus, "covers", return_value=False)
        p.start()
        self.addCleanup(p.stop)


class StopsTest(unittest.TestCase):
    def setUp(self):
        self.nodes = {1: node(1, 57.700, 11.970, name="Vasaplatsen", public_transport="stop_position"),
                      2: node(2, 57.7001, 11.9702, name="Vasaplatsen", public_transport="platform"),
                      3: node(3, 57.702, 11.970, name="Kapellplatsen"),
                      4: node(4, 57.704, 11.970, name="Chalmers", public_transport="platform"),
                      5: node(5, 57.706, 11.970, name="Wavrinskys Plats")}
        self.ways = {9: {"type": "way", "id": 9, "center": {"lat": 57.708, "lon": 11.971},
                         "tags": {"name": "Fyrktorget", "public_transport": "platform"}}}

    def test_a_stop_and_its_platform_are_one_stop_at_the_road(self):
        r = rel(10, [("node", 1, "stop"), ("node", 2, "platform"), ("way", 100, ""), ("node", 3, "stop_entry_only"),
                     ("node", 4, "platform"), ("node", 5, "stop"), ("way", 9, "platform_exit_only")])
        st = buslines.stops(r, self.nodes, self.ways)
        self.assertEqual([s["name"] for s in st], ["Vasaplatsen", "Kapellplatsen", "Chalmers", "Wavrinskys Plats",
                                                   "Fyrktorget"])
        self.assertTrue(st[0]["on_road"])
        self.assertEqual((st[0]["lat"], st[0]["lon"]), (57.700, 11.970))
        self.assertFalse(st[2]["on_road"])                          # a lone platform: the stop is there
        self.assertEqual(st[4]["lat"], 57.708)                      # a platform area: its centre

    def test_missing_members_and_repeats_are_skipped(self):
        r = rel(10, [("node", 1, "stop"), ("node", 77, "stop"), ("node", 1, "stop"), ("node", 3, "stop")])
        self.assertEqual([s["name"] for s in buslines.stops(r, self.nodes, self.ways)], ["Vasaplatsen", "Kapellplatsen"])


class PlatformLetterTest(unittest.TestCase):
    """Västtrafik's platforms are named by their letter (name=A, ref=A beside "Fässbergsskolan"; line 25 near Mölndal,
    2026-11-17): they were stops of their own, A and A8 a few metres after the stop they belong to."""
    def test_a_lettered_platform_is_the_stop_before_it(self):
        nodes = {1: node(1, 57.6500, 12.0150, name="Fässbergsskolan", public_transport="stop_position", ref="A"),
                 2: node(2, 57.6600, 12.0150, name="Storgatan", public_transport="stop_position", ref="B"),
                 3: node(3, 57.6700, 12.0150, name="Mölndals Innerstad", public_transport="stop_position", ref="A8")}
        ways = {8: {"type": "way", "id": 8, "center": {"lat": 57.65003, "lon": 12.01505},
                    "tags": {"name": "A", "public_transport": "platform", "ref": "A"}},
                9: {"type": "way", "id": 9, "center": {"lat": 57.67004, "lon": 12.01506},
                    "tags": {"name": "A8", "public_transport": "platform", "ref": "A8"}}}
        r = rel(10, [("node", 1, "stop"), ("way", 8, "platform"), ("node", 2, "stop"), ("node", 3, "stop"),
                     ("way", 9, "platform")])
        st = buslines.stops(r, nodes, ways)
        self.assertEqual([s["name"] for s in st], ["Fässbergsskolan", "Storgatan", "Mölndals Innerstad"])
        self.assertTrue(all(s["on_road"] for s in st))

    def test_a_platform_beside_the_stop_joins_it_whatever_its_name(self):
        nodes = {1: node(1, 57.6500, 12.0150, name="Mölndals bro", public_transport="stop_position"),
                 2: node(2, 57.65020, 12.0151, name="Mölndals bro läge C", public_transport="platform")}
        st = buslines.stops(rel(10, [("node", 1, "stop"), ("node", 2, "platform")]), nodes, {})
        self.assertEqual([s["name"] for s in st], ["Mölndals bro"])

    def test_a_lone_lettered_platform_takes_the_name_of_the_stop_beside_it(self):
        nodes = {1: node(1, 57.6500, 12.0150, name="GoCo", highway="bus_stop"),
                 2: node(2, 57.65010, 12.0152, name="B", public_transport="platform", ref="B"),
                 3: node(3, 57.6600, 12.0150, name="Krokslätt", public_transport="platform")}
        st = buslines.stops(rel(10, [("node", 2, "platform"), ("node", 3, "platform")]), nodes, {})
        self.assertEqual([s["name"] for s in st], ["GoCo", "Krokslätt"])
        self.assertFalse(st[0]["on_road"])


class ParseTest(OverpassOnly, unittest.TestCase):
    def setUp(self):
        super().setUp()
        buslines._cache.clear()

    def data(self):
        els = [node(1, 57.700, 11.970, name="A"), node(2, 57.710, 11.970, name="B"), node(3, 57.720, 11.970, name="C")]
        els += [rel(10, [("node", 1, "stop"), ("node", 2, "stop")], ref="158", colour="red", to="B"),
                rel(11, [("node", 2, "stop"), ("node", 3, "stop")], ref="16", colour="#0C81C5"),
                rel(12, [("node", 2, "stop"), ("node", 3, "stop")], ref="16", colour="#0C81C5"),    # mapped twice
                rel(13, [("node", 1, "stop")], ref="1"),                                            # one stop
                rel(14, [("node", 1, "stop"), ("node", 2, "stop")], ref="X1", colour="sparkly")]
        return {"elements": els}

    def test_lines(self):
        lines = buslines.parse(self.data(), 57.710, 11.970)
        self.assertEqual([l["ref"] for l in lines], ["16", "158", "X1"])
        l16 = lines[0]
        self.assertEqual((l16["colour"], l16["from"], l16["to"], l16["near_i"], l16["near_m"]),
                         ("#0c81c5", "B", "C", 0, 0.0))
        self.assertEqual(lines[1]["colour"], "#ff0000")
        self.assertEqual(lines[2]["colour"], buslines.DEFAULT_COLOUR)

    def test_colours(self):
        self.assertEqual(buslines.colour("#abc"), "#aabbcc")
        self.assertEqual(buslines.colour("FFFF00"), "#ffff00")
        self.assertIsNone(buslines.colour(""))

    def test_the_request_is_bounded_and_snapped(self):
        a, b = buslines.overpass_url(57.7061, 11.9702), buslines.overpass_url(57.7062, 11.9703)
        self.assertEqual(a, b)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(a).query)["data"][0]
        self.assertIn("[route=bus]", q)
        self.assertNotIn("out geom", q)
        self.assertIn("[timeout:25]", q)
        self.assertIn("[maxsize:16777216]", q)
        self.assertIn("[highway=bus_stop]", q)
        self.assertIn("rel(bn.near)", q)
        self.assertIn("rel(bw.near)", q)
        self.assertNotRegex(q, r"rel\(-?\d")  # no scan of whole routes' road members in the bbox
        self.assertEqual(q.count("[route=bus]"), 2)

    def test_near_asks_once_per_place(self):
        calls = []

        class Net:
            def get(self, url, kind):
                calls.append(url)
                return type("R", (), {"body": b'{"elements": []}'})()
        buslines._cache.clear()
        buslines.near(57.7, 11.97, Net())
        buslines.near(57.7, 11.97, Net())
        self.assertEqual(len(calls), 1)

    def test_incomplete_answers_are_not_cached_even_with_elements(self):
        buslines._cache.clear()
        partial = {"remark": "runtime error: Query timed out", **self.data()}
        net = mock.Mock()
        net.get.return_value.body = json.dumps(partial).encode()
        with self.assertRaisesRegex(ValueError, "Query timed out"):
            buslines.near(57.7, 11.97, net)
        net.get.return_value.body = json.dumps(self.data()).encode()
        self.assertEqual(len(buslines.near(57.7, 11.97, net)), 3)
        self.assertEqual(len(buslines.near(57.7, 11.97, net)), 3)
        self.assertEqual(net.get.call_count, 2, "retry after failure; cache only the complete answer")

    def test_unavailable_primary_uses_one_fallback_and_caches_it(self):
        for error in (netfetch.HostUnavailable("circuit open"),
                      netfetch.FetchFailed("connection timed out"),
                      netfetch.FetchFailed("HTTP 503", status=503)):
            with self.subTest(error=str(error)):
                buslines._cache.clear()
                net = mock.Mock()
                net.get.side_effect = [error, mock.Mock(body=json.dumps(self.data()).encode())]
                self.assertEqual(len(buslines.near(57.7, 11.97, net)), 3)
                self.assertEqual(len(buslines.near(57.7, 11.97, net)), 3)
                self.assertEqual(net.get.call_count, 2)
                urls = [call.args[0] for call in net.get.call_args_list]
                self.assertTrue(urls[0].startswith(buslines.OVERPASS_URL + "?"))
                self.assertTrue(urls[1].startswith(buslines.OVERPASS_FALLBACK_URL + "?"))
                self.assertEqual(urllib.parse.urlsplit(urls[0]).query, urllib.parse.urlsplit(urls[1]).query)

    def test_refusals_do_not_contact_a_fallback(self):
        for error in (netfetch.Offline("offline"), netfetch.RateLimited("HTTP 429", status=429),
                      netfetch.PolicyDenied("denied"), netfetch.BudgetExceeded("budget"),
                      netfetch.FetchFailed("HTTP 400", status=400),
                      netfetch.FetchFailed("HTTP 406", status=406)):
            with self.subTest(error=type(error).__name__):
                buslines._cache.clear()
                net = mock.Mock()
                net.get.side_effect = error
                with self.assertRaises(type(error)):
                    buslines.near(57.7, 11.97, net)
                self.assertEqual(net.get.call_count, 1)

    def test_both_endpoints_fail_with_readable_reasons_and_no_cached_empty_result(self):
        buslines._cache.clear()
        def fail(url, **kwargs):
            raise netfetch.FetchFailed(f"{url}: TimeoutError: connection timed out", url=url)
        net = mock.Mock()
        net.get.side_effect = fail
        with self.assertRaisesRegex(ValueError, "connection timed out") as raised:
            buslines.near(57.7, 11.97, net)
        message = str(raised.exception)
        self.assertIn("overpass-api.de", message)
        self.assertIn("overpass.private.coffee", message)
        self.assertNotIn("?data=", message)
        self.assertFalse(buslines._cache)
        self.assertEqual(net.get.call_count, 2)

    def test_both_fail_says_so_plainly(self):
        buslines._cache.clear()
        net = mock.Mock()
        net.get.side_effect = netfetch.FetchFailed("x: HTTP 504", url="x", status=504)
        with self.assertRaises(buslines.Busy) as raised:
            buslines.near(57.7, 11.97, net)
        self.assertTrue(buslines.error_text(raised.exception).startswith("OpenStreetMap's bus-line servers"))

    def test_both_fail_uses_a_saved_lookup_nearby(self):
        """Overpass overloaded everywhere (2026-11-17): the lines of an earlier lookup with a stop near the player."""
        buslines._cache.clear()
        stops_ = [node(1, 57.7000, 11.970, name="Vasaplatsen", public_transport="stop_position"),
                  node(2, 57.7040, 11.970, name="Kapellplatsen", public_transport="stop_position"),
                  node(3, 57.7300, 11.970, name="Far away", public_transport="stop_position")]
        good = {"elements": stops_ + [rel(10, [("node", 1, "stop"), ("node", 2, "stop")], ref="1", to="Kapellplatsen"),
                                      rel(11, [("node", 3, "stop"), ("node", 2, "stop")], ref="2", to="Far")]}
        with tempfile.TemporaryDirectory() as tmp:
            buslines._write_cache(buslines._cache_path(tmp, buslines.overpass_url(57.70, 11.97)), good)
            net = mock.Mock()
            net.get.side_effect = netfetch.FetchFailed("x: HTTP 504", url="x", status=504)
            got = buslines.near(57.7012, 11.9745, net, cache_dir=tmp)       # (another snapped place: not its key)
            self.assertEqual(sorted(l["ref"] for l in got), ["1", "2"])
            self.assertTrue(all(l["saved"] for l in got))
            with self.assertRaises(buslines.Busy):
                buslines.near(57.80, 11.97, net, cache_dir=tmp)              # (none of its stops near)

    def test_error_text_keeps_reason_after_a_long_query_url(self):
        url = buslines.overpass_url(57.7, 11.97)
        error = netfetch.FetchFailed(f"{url}: HTTP 503", url=url, status=503)
        self.assertEqual(buslines.error_text(error), "FetchFailed: overpass-api.de: HTTP 503")

    def test_fallback_has_explicit_conservative_policy(self):
        self.assertEqual(netfetch.POLICIES["overpass.private.coffee"], netfetch.POLICIES["overpass-api.de"])

    def test_success_survives_a_restart_and_works_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            net = mock.Mock()
            net.get.return_value.body = json.dumps(self.data()).encode()
            expected = buslines.near(57.7, 11.97, net, directory)
            self.assertEqual(len(list(Path(directory).rglob("*.json"))), 1)
            buslines._cache.clear()  # a new server session
            offline = mock.Mock()
            offline.get.side_effect = netfetch.Offline("offline")
            self.assertEqual(buslines.near(57.7, 11.97, offline, directory), expected)
            offline.get.assert_not_called()
            # A cache entry for one place must not hide a different place's network failure.
            with self.assertRaises(netfetch.Offline):
                buslines.near(58, 12, offline, directory)

    def test_expired_and_corrupt_disk_entries_are_refetched(self):
        for content, expired in (("broken json", False), ('{"remark":"timeout","elements":[]}', False),
                                 (json.dumps(self.data()), True)):
            with self.subTest(content=content[:20], expired=expired), tempfile.TemporaryDirectory() as directory:
                buslines._cache.clear()
                path = buslines._cache_path(directory, buslines.overpass_url(57.7, 11.97))
                path.parent.mkdir(parents=True)
                path.write_text(content)
                if expired:
                    old = time.time() - buslines.CACHE_TTL_S - 1
                    os.utime(path, (old, old))
                net = mock.Mock()
                net.get.return_value.body = json.dumps(self.data()).encode()
                self.assertEqual(len(buslines.near(57.7, 11.97, net, directory)), 3)
                self.assertEqual(net.get.call_count, 1)

    def test_cache_write_failure_does_not_discard_good_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            # A regular file where the cache directory should be: the game must still get its routes.
            (Path(directory) / "buslines").write_text("unwritable")
            net = mock.Mock()
            net.get.return_value.body = json.dumps(self.data()).encode()
            self.assertEqual(len(buslines.near(57.7, 11.97, net, directory)), 3)

    def test_failed_lookup_is_not_written_to_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            net = mock.Mock()
            net.get.side_effect = netfetch.FetchFailed("HTTP 502", status=502)
            with self.assertRaises(ValueError):
                buslines.near(57.7, 11.97, net, directory)
            self.assertEqual(list(Path(directory).rglob("*.json")), [])


class BusLinesProtocolTest(OverpassOnly, unittest.IsolatedAsyncioTestCase):
    async def test_server_uses_disk_cache_across_sessions(self):
        import earthd
        from concurrent.futures import ThreadPoolExecutor
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(1) as pool:
            buslines._cache.clear()
            net = mock.Mock()
            net.get.return_value.body = json.dumps(ParseTest().data()).encode()
            server = SimpleNamespace(net=net, cache_dir=directory, search_pool=pool)
            sent = []
            connection = SimpleNamespace(s=server, send=sent.append)
            await earthd.Connection.bus_lines(connection, {"id": 1, "lat": 57.7, "lon": 11.97})
            self.assertEqual(sent[0]["type"], "bus_lines_result")
            self.assertEqual(len(sent[0]["lines"]), 3)
            self.assertNotIn("error", sent[0])
            buslines._cache.clear()
            net.get.side_effect = netfetch.Offline("offline")
            await earthd.Connection.bus_lines(connection, {"id": 2, "lat": 57.7, "lon": 11.97})
            self.assertEqual(sent[1]["id"], 2)
            self.assertEqual(sent[1]["lines"], sent[0]["lines"])
            self.assertNotIn("error", sent[1])
            self.assertEqual(net.get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
