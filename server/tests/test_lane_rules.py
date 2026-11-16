"""Germany's road rules and junction signs in the lane shards (tools/build_lane_tags.py: rule_tags, sign_records,
lane_way, the first pass over objects, and the shards build_rail_tags.write makes of them); pyosmium is stubbed, the
planet is not read."""
import gzip
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.modules.setdefault("osmium", types.ModuleType("osmium"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
sys.path.append(str(Path(__file__).resolve().parents[1]))     # (after tools: the server's germany only)

import build_lane_tags as lt  # noqa: E402
import build_rail_tags as rail  # noqa: E402
import germany  # noqa: E402  (its BOX: the tool's GERMANY_BOX)


class RuleTagsTest(unittest.TestCase):
    def test_priority_road(self):
        self.assertEqual(lt.rule_tags({"priority_road": "designated"}), {"priority_road": "designated"})
        self.assertEqual(lt.rule_tags({"priority_road": "end"}), {"priority_road": "end"})
        self.assertEqual(lt.rule_tags({"priority_road": "yes_unposted"}), {"priority_road": "yes_unposted"})
        self.assertEqual(lt.rule_tags({"priority_road": "maybe"}), {})
        self.assertEqual(lt.rule_tags({"traffic_sign": "DE:306"}), {"priority_road": "designated"})

    def test_zones(self):
        for t in ({"zone:maxspeed": "DE:30"}, {"maxspeed:type": "DE:zone30"}, {"maxspeed:type": "DE:zone:30"},
                  {"source:maxspeed": "DE:zone30"}, {"source:maxspeed": "DE:zone:30"}):
            self.assertEqual(lt.rule_tags(t), {"zone:maxspeed": "DE:30"}, t)
        self.assertEqual(lt.rule_tags({"zone:maxspeed": "DE:20"}), {"zone:maxspeed": "DE:20"})
        self.assertEqual(lt.rule_tags({"maxspeed:type": "DE:zone10"}), {"zone:maxspeed": "DE:10"})

    def test_not_a_german_zone(self):
        for t in ({"zone:maxspeed": "AT:30"}, {"maxspeed:type": "FR:zone30"}, {"maxspeed": "30"},
                  {"source:maxspeed": "DE:urban"}, {"source:maxspeed": "sign"}):
            self.assertEqual(lt.rule_tags(t), {}, t)


class SignNodeTest(unittest.TestCase):
    def test_codes_kept_in_order(self):
        self.assertEqual(lt.sign_node({"traffic_sign": "DE:205,1002-12", "direction": "forward"}),
                         {"traffic_sign": "DE:205,1002-12", "direction": "forward"})
        self.assertEqual(lt.sign_node({"traffic_sign": "DE:206;DE:1022-10"}), {"traffic_sign": "DE:206"})
        self.assertEqual(lt.sign_node({"traffic_sign": "DE:205;DE:1002-12", "traffic_sign:direction": "backward"}),
                         {"traffic_sign": "DE:205,DE:1002-12", "traffic_sign:direction": "backward"})

    def test_other_signs_dropped(self):
        self.assertIsNone(lt.sign_node({"traffic_sign": "DE:244.1"}))
        self.assertIsNone(lt.sign_node({"highway": "street_lamp"}))
        for code in ("DE:274", "DE:274.10", "DE:2050", "DE:3060", "AT:205", "DE:1002-12"):
            self.assertIsNone(lt.sign_node({"traffic_sign": code}), code)

    def test_variants_kept_as_mapped(self):
        """A code's variant after its number (a zone's limit) matches on the number and is kept whole."""
        for code in ("DE:274.1:30", "DE:274.1[30]", "DE:274.1-20", "DE:274.2-20", "DE:274.1[20]"):
            self.assertEqual(lt.sign_node({"traffic_sign": code}), {"traffic_sign": code}, code)

    def test_codes_without_the_country(self):
        for code in ("205", "206", "301", "306", "307", "274.1", "274.2"):
            self.assertEqual(lt.sign_node({"traffic_sign": code}), {"traffic_sign": "DE:" + code}, code)
        self.assertEqual(lt.sign_node({"traffic_sign": "274.1-20"}), {"traffic_sign": "DE:274.1-20"})
        self.assertEqual(lt.sign_node({"traffic_sign": "DE:306,205"}), {"traffic_sign": "DE:306,DE:205"})

    def test_forward_and_backward_keys(self):
        self.assertEqual(lt.sign_records({"traffic_sign:forward": "DE:306"}),
                         [{"traffic_sign": "DE:306", "direction": "forward"}])
        self.assertEqual(lt.sign_records({"traffic_sign:backward": "DE:205,1002-12"}),
                         [{"traffic_sign": "DE:205,1002-12", "direction": "backward"}])
        self.assertEqual(lt.sign_records({"traffic_sign:backward": "DE:205", "direction": "NE",
                                          "traffic_sign:direction": "forward"}),
                         [{"traffic_sign": "DE:205", "direction": "NE"}], "a mapped direction wins")
        self.assertEqual(lt.sign_records({"traffic_sign:forward": "DE:306", "traffic_sign:backward": "DE:307"}),
                         [{"traffic_sign": "DE:306", "direction": "forward"},
                          {"traffic_sign": "DE:307", "direction": "backward"}], "one record a way")
        self.assertEqual(lt.sign_records({"traffic_sign": "DE:205", "direction": "backward"}),
                         [{"traffic_sign": "DE:205", "direction": "backward"}])
        self.assertEqual(lt.sign_records({"traffic_sign:forward": "DE:244.1"}), [])

    def test_filters_read_every_key_used(self):
        """The first pass's filters: a way every key lane_way and rule_tags read (dev's LANE_KEYS and junction
        among them), a node the three sign keys."""
        for k in lt.RULE_KEYS + lt.LANE_KEYS + ("junction",):
            self.assertIn(k, lt.WAY_KEYS)
        self.assertEqual(len(lt.WAY_KEYS), len(set(lt.WAY_KEYS)))
        self.assertEqual(lt.NODE_KEYS, ("traffic_sign", "traffic_sign:forward", "traffic_sign:backward"))


class LaneWayTest(unittest.TestCase):
    def test_lane_way_is_its_tags(self):
        t = {"highway": "primary", "turn:lanes": "left|through", "lanes": "2", "name": "X"}
        self.assertEqual(lt.lane_way(t), {"turn:lanes": "left|through", "highway": "primary", "lanes": "2"})

    def test_rule_keys_never_in_a_lane_way(self):
        t = {"highway": "primary", "turn:lanes": "left|through", "lanes": "2", "priority_road": "designated",
             "zone:maxspeed": "DE:30", "source:maxspeed": "DE:zone30", "traffic_sign": "DE:306"}
        self.assertEqual(lt.lane_way(t), {"turn:lanes": "left|through", "highway": "primary", "lanes": "2"})

    def test_not_lane_ways(self):
        self.assertIsNone(lt.lane_way({"highway": "secondary", "lanes": "2", "oneway": "yes",
                                       "priority_road": "designated"}))
        self.assertIsNone(lt.lane_way({"highway": "residential", "junction": "yes"}))
        self.assertIsNone(lt.lane_way({"railway": "rail", "lanes:forward": "1"}))

    def test_ring_kept(self):
        self.assertEqual(lt.lane_way({"highway": "tertiary", "junction": "roundabout"}),
                         {"highway": "tertiary", "junction": "roundabout"})


def _way(oid, tags, refs):
    return SimpleNamespace(id=oid, tags=tags, nodes=[SimpleNamespace(ref=r) for r in refs], is_node=lambda: False)


def _node(oid, tags, lon, lat):
    return SimpleNamespace(id=oid, tags=tags, location=SimpleNamespace(lon=lon, lat=lat, valid=lambda: True),
                           is_node=lambda: True)


def _upstream_first_pass(objects):
    """tools/build_lane_tags.py's first pass on dev 741354ad (before the road rules), over the objects its filter
    let through (ways with a LANE_KEYS or junction key): what today's shards' "ways" are made of."""
    ways = []
    for o in objects:
        if o.is_node() or not any(k in o.tags for k in lt.LANE_KEYS + ("junction",)):
            continue
        tags = o.tags
        if "highway" not in tags or len(o.nodes) < 2:
            continue
        if tags.get("junction") not in lt.RINGS and not any(k in tags for k in lt.LANE_KEYS):
            continue
        refs = [n.ref for n in o.nodes]
        ways.append((o.id, {t: tags[t] for t in lt.TAGS if t in tags},
                     np.array([refs[0], refs[len(refs) // 2], refs[-1]], np.int64)))
    return ways


# node places: 1-6 in Munich (z7 68/44), 7-9 in Berlin (68/41), 10-11 in Hamburg (67/41)
NID = np.arange(1, 12, dtype=np.int64)
NLON = np.array([11.50, 11.52, 11.54, 11.56, 11.58, 11.60, 13.40, 13.41, 13.42, 10.00, 10.01])
NLAT = np.array([48.10, 48.11, 48.12, 48.13, 48.14, 48.15, 52.50, 52.51, 52.52, 53.55, 53.56])
OBJECTS = [
    _node(201, {"traffic_sign": "DE:205", "direction": "forward"}, 11.51, 48.105),
    _node(202, {"traffic_sign:forward": "DE:306", "traffic_sign:backward": "DE:307"}, 11.53, 48.115),
    _node(203, {"traffic_sign": "DE:244.1"}, 11.55, 48.125),
    _way(101, {"highway": "primary", "turn:lanes": "left|through", "lanes": "2", "oneway": "yes", "name": "A",
               "priority_road": "designated", "source:maxspeed": "DE:zone30", "traffic_sign": "DE:306"}, [1, 2, 3]),
    _way(102, {"highway": "secondary", "lanes": "2", "oneway": "yes", "zone:maxspeed": "DE:30"}, [4, 5, 6]),
    _way(103, {"highway": "tertiary", "junction": "roundabout"}, [7, 8]),
    _way(104, {"highway": "residential", "source:maxspeed": "DE:urban"}, [8, 9]),
    _way(105, {"highway": "primary", "priority_road": "designated"}, [3]),           # (one node: no way)
    _way(106, {"highway": "primary", "lanes:forward": "2", "priority_road": "end"}, [7, 9]),
    _way(107, {"highway": "secondary", "priority_road": "yes_unposted"}, [10, 11]),  # (a shard with rules only)
]


def _shards(out):
    got = {}
    for x in sorted(os.listdir(out)):
        if x.isdigit():
            for f in sorted(os.listdir(os.path.join(out, x))):
                with gzip.open(os.path.join(out, x, f), "rb") as fh:
                    got[(int(x), int(f.split(".")[0]))] = fh.read()
    return got


class FirstPassTest(unittest.TestCase):
    def test_ways_are_todays_and_rules_apart(self):
        ways, rules, nodes = lt._first_pass(OBJECTS)
        up = _upstream_first_pass(OBJECTS)
        self.assertEqual([(i, t) for i, t, _r in ways], [(i, t) for i, t, _r in up], "the ways: today's, exactly")
        self.assertEqual([r.tolist() for _i, _t, r in ways], [r.tolist() for _i, _t, r in up])
        self.assertEqual([(i, t) for i, t, _r in rules],
                         [(101, {"priority_road": "designated", "zone:maxspeed": "DE:30"}),
                          (102, {"zone:maxspeed": "DE:30"}), (106, {"priority_road": "end"}),
                          (107, {"priority_road": "yes_unposted"})])
        self.assertEqual([(i, t) for i, _lo, _la, t in nodes],
                         [(201, {"traffic_sign": "DE:205", "direction": "forward"}),
                          (202, {"traffic_sign": "DE:306", "direction": "forward"}),
                          (202, {"traffic_sign": "DE:307", "direction": "backward"})])


class ShardsTest(unittest.TestCase):
    """The lane shards build_rail_tags.write makes of the first pass: "ways" as today's tool writes them, the road
    rules apart in "rules"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ways, rules, nodes = lt._first_pass(OBJECTS)
        self.new = os.path.join(self.tmp.name, "new")
        self.old = os.path.join(self.tmp.name, "old")
        rail.write(self.new, ways, nodes, NID, NLON, NLAT, rules=rules)
        rail.write(self.old, _upstream_first_pass(OBJECTS), [], NID, NLON, NLAT)     # (today's tool: no nodes)

    def test_ways_byte_identical_to_todays(self):
        old, new = _shards(self.old), _shards(self.new)
        self.assertLessEqual(set(old), set(new))
        for k, body in old.items():
            o, n = json.loads(body), json.loads(new[k])
            self.assertEqual(json.dumps(n["ways"], separators=(",", ":")), json.dumps(o["ways"], separators=(",", ":")),
                             k)
        for k in set(new) - set(old):
            self.assertEqual(json.loads(new[k])["ways"], {}, "a shard of rules only has no ways")

    def test_rule_only_way_not_in_ways(self):
        munich = json.loads(_shards(self.new)[(68, 44)])
        self.assertEqual(munich["ways"], {"101": {"turn:lanes": "left|through", "highway": "primary", "lanes": "2",
                                                  "oneway": "yes"}})
        self.assertNotIn("102", munich["ways"])
        self.assertEqual(munich["rules"], {"priority_road": {"designated": [101]},
                                           "zone:maxspeed": {"DE:30": [101, 1]}})
        self.assertEqual(list(munich), ["ways", "nodes", "rules"])
        self.assertEqual([n[0] for n in munich["nodes"]], [201, 202, 202])

    def test_rules_where_the_way_is(self):
        new = {k: json.loads(v) for k, v in _shards(self.new).items()}
        self.assertEqual(new[(68, 41)]["rules"], {"priority_road": {"end": [106]}})
        self.assertEqual(set(new[(68, 41)]["ways"]), {"103", "106"})
        self.assertEqual(new[(67, 41)], {"ways": {}, "nodes": [], "rules": {"priority_road": {"yes_unposted": [107]}}})

    def test_no_rules_key_where_none(self):
        ways, _r, _n = lt._first_pass(OBJECTS[5:6])                     # (the ring only)
        out = os.path.join(self.tmp.name, "ring")
        rail.write(out, ways, [], NID, NLON, NLAT, rules=[])
        self.assertEqual(list(json.loads(_shards(out)[(68, 41)])), ["ways", "nodes"])
        with open(os.path.join(out, "manifest.json")) as fh:
            self.assertEqual(json.load(fh)["rules"], 0)


class GermanyBoxTest(unittest.TestCase):
    """Rules and sign nodes only around Germany (GERMANY_BOX); delta-coded rule ids."""
    STOCKHOLM = (18.07, 59.33)
    MUNICH = (11.58, 48.14)

    def _build(self, objects, places):
        """(shards decoded, manifest) of objects with the node places {ref: (lon, lat)}, as main() does."""
        ways, rules, nodes = lt._first_pass(objects)
        refs = sorted(places)
        nid = np.array(refs, np.int64)
        nlon = np.array([places[r][0] for r in refs])
        nlat = np.array([places[r][1] for r in refs])
        rules = lt.rules_in_box(rules, nid, nlon, nlat)
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        rail.write(out, ways, nodes, nid, nlon, nlat, rules=rules)
        with open(os.path.join(out, "manifest.json")) as fh:
            manifest = json.load(fh)
        return {k: json.loads(v) for k, v in _shards(out).items()}, manifest

    def test_box(self):
        self.assertEqual(lt.GERMANY_BOX, (5.81, 47.22, 15.10, 55.11))
        self.assertEqual(lt.GERMANY_BOX, germany.BOX, "the server's box (germany.BOX) and the tool's are one")
        self.assertTrue(lt.in_box(11.58, 48.14))
        self.assertFalse(lt.in_box(18.07, 59.33))

    def test_rule_way_outside_dropped_inside_kept(self):
        way = lambda i, refs: _way(i, {"highway": "primary", "priority_road": "designated"}, refs)
        places = {1: self.STOCKHOLM, 2: (18.08, 59.34), 3: (18.09, 59.35), 4: self.MUNICH, 5: (18.0, 59.0)}
        got, manifest = self._build([way(1, [1, 2, 3]), way(2, [5, 5, 4])], places)
        self.assertEqual(manifest["rules"], 1)
        self.assertEqual(sorted(k for k, v in got.items() if "rules" in v), [(68, 44), (70, 37)],
                         "way 2: in every shard its nodes fall in, as today; way 1 nowhere")
        for k in ((68, 44), (70, 37)):
            self.assertEqual(got[k]["rules"], {"priority_road": {"designated": [2]}})
        self.assertEqual(len(got), 2, "no shard for the way outside")

    def test_way_without_places_dropped(self):
        got, manifest = self._build([_way(1, {"highway": "primary", "priority_road": "end"}, [8, 9])],
                                    {1: self.MUNICH})
        self.assertEqual((got, manifest["rules"]), ({}, 0))

    def test_sign_node_outside_not_written(self):
        got, manifest = self._build([_node(1, {"traffic_sign": "DE:205"}, *self.STOCKHOLM),
                                     _node(2, {"traffic_sign": "DE:206"}, *self.MUNICH)], {})
        self.assertEqual(manifest["nodes"], 1)
        self.assertEqual([n[0] for v in got.values() for n in v["nodes"]], [2])

    def test_rule_ids_delta_coded(self):
        objs = [_way(i, {"highway": "primary", "priority_road": "designated"}, [4, 4]) for i in (230, 100, 105)]
        got, _m = self._build(objs, {4: self.MUNICH})
        self.assertEqual(got[(68, 44)]["rules"], {"priority_road": {"designated": [100, 5, 125]}})

    def test_no_node_places_no_rules(self):
        way = _way(1, {"highway": "primary", "priority_road": "end"}, [1, 2])
        self.assertEqual(lt.rules_in_box(lt._first_pass([way])[1], np.zeros(0, np.int64), np.zeros(0), np.zeros(0)),
                         [])

    def test_main_applies_the_box(self):
        """main() writes only the rules rules_in_box keeps (the first pass and the second pass stubbed)."""
        objs = [_way(1, {"highway": "primary", "priority_road": "end"}, [1, 2]),
                _way(2, {"highway": "primary", "priority_road": "end"}, [3, 4])]
        places = {1: self.STOCKHOLM, 2: (18.08, 59.34), 3: self.MUNICH, 4: (11.59, 48.15)}
        refs = sorted(places)
        written = {}
        saved = (lt.first_pass, lt.rail.second_pass, lt.rail.write, sys.argv)
        lt.first_pass = lambda _src: lt._first_pass(objs)
        lt.rail.second_pass = lambda _src, _ids: (np.array(refs, np.int64), np.array([places[r][0] for r in refs]),
                                                  np.array([places[r][1] for r in refs]))
        lt.rail.write = lambda _out, _w, _n, _i, _lo, _la, rules=None: written.update(rules=rules)
        sys.argv = ["build_lane_tags.py", "planet.pbf", "out"]
        try:
            lt.main()
        finally:
            lt.first_pass, lt.rail.second_pass, lt.rail.write, sys.argv = saved
        self.assertEqual([r[0] for r in written["rules"]], [2])

    def test_all_outside_same_shards_as_without(self):
        objs = [_way(1, {"highway": "primary", "turn:lanes": "left|through", "priority_road": "designated"}, [1, 2]),
                _way(2, {"highway": "primary", "zone:maxspeed": "DE:30"}, [1, 2]),
                _node(3, {"traffic_sign": "DE:205"}, *self.STOCKHOLM)]
        places = {1: self.STOCKHOLM, 2: (18.08, 59.34)}
        got, manifest = self._build(objs, places)
        want, wmanifest = self._build([_way(1, {"highway": "primary", "turn:lanes": "left|through"}, [1, 2])],
                                      places)
        self.assertEqual(got, want)
        self.assertEqual((manifest["rules"], manifest["nodes"]), (0, 0))
        self.assertEqual(manifest["shards"], wmanifest["shards"])


class RailShardsUnchangedTest(unittest.TestCase):
    """build_rail_tags.write without rules (the rail shards): byte for byte what dev 741354ad's wrote for the same
    input (the shards decompressed: gzip's header holds the time)."""

    def test_rail_shards(self):
        nid = np.array([1, 2, 3, 4, 5], np.int64)
        nlon = np.array([11.50, 11.52, 11.54, 13.40, 13.41])
        nlat = np.array([48.10, 48.11, 48.12, 52.50, 52.51])
        ways = [(7, {"railway": "rail", "gauge": "1435", "electrified": "contact_line"}, np.array([1, 2, 3], np.int64)),
                (8, {"railway": "tram"}, np.array([4, 5], np.int64)),
                (9, {"railway": "rail"}, np.array([99], np.int64))]
        nodes = [(20, 11.5000001, 48.1000001, {"railway": "level_crossing", "crossing:barrier": "full"}),
                 (21, 13.4, 52.5, {"power": "catenary_mast"})]
        with tempfile.TemporaryDirectory() as out:
            rail.write(out, ways, nodes, nid, nlon, nlat)
            got = _shards(out)
            with open(os.path.join(out, "manifest.json")) as fh:
                manifest = json.load(fh)
        self.assertEqual(got, {
            (68, 41): b'{"ways":{"8":{"railway":"tram"}},"nodes":[[21,13.4,52.5,{"power":"catenary_mast"}]]}',
            (68, 44): b'{"ways":{"7":{"railway":"rail","gauge":"1435","electrified":"contact_line"}},"nodes":[[20,'
                      b'11.5000001,48.1000001,{"railway":"level_crossing","crossing:barrier":"full"}]]}'})
        self.assertEqual(list(manifest), ["format", "zoom", "built", "ways", "nodes", "shards", "license"])
        self.assertEqual((manifest["ways"], manifest["nodes"], manifest["shards"]), (3, 2, 2))


if __name__ == "__main__":
    unittest.main()
