"""buslines: OpenStreetMap's bus routes as lines with their stops in order (the user, 2026-11-17: "yes start the bus
lines")."""
import os
import sys
import unittest
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import buslines  # noqa: E402


def node(i, lat, lon, **tags):
    return {"type": "node", "id": i, "lat": lat, "lon": lon, "tags": tags}


def rel(i, members, **tags):
    tags = dict({"type": "route", "route": "bus"}, **tags)
    return {"type": "relation", "id": i, "tags": tags,
            "members": [{"type": t, "ref": r, "role": role} for t, r, role in members]}


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


class ParseTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
