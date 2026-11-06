"""railtags.py: the weekly rail shards joined to Overture's rail segments by OSM way id."""
import gzip
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mapcells  # noqa: E402
import mapgeom  # noqa: E402
import netfetch  # noqa: E402
import railtags  # noqa: E402
import roads  # noqa: E402
from test_roads import features  # noqa: E402


class _Fetcher:
    def __init__(self, files):
        self.files, self.asked = files, []

    def get(self, url, kind="demand", **_kw):
        self.asked.append(url)
        for k, v in self.files.items():
            if url.endswith(k):
                return SimpleNamespace(body=v)
        raise netfetch.NotFound(url)


class RailTagsTest(unittest.TestCase):
    def test_join_by_way_id_and_nodes(self):
        f = features([[(0.0, 100.0), (250.0, 100.0)], [(0.0, 120.0), (250.0, 120.0)], [(0.0, 0.0), (5.0, 5.0)]],
                     ["unknown", "standard_gauge", "residential"], subtypes=["rail", "rail", "road"])
        f.attrs["src_record"] = np.array(["w11@3", "w12@1", "w11@3"])
        lo, la = f.lonlat[0]
        key = mapcells.cell_of(float(la), float(lo))
        sx, sy = key.x >> (key.z - 7), key.y >> (key.z - 7)
        shard = {"ways": {"11": {"railway": "proposed"}, "12": {"railway": "rail", "gauge": "1520"}},
                 "nodes": [[5, float(lo), float(la), {"railway": "level_crossing", "crossing:barrier": "full"}],
                           [6, float(lo) + 3.0, float(la), {"railway": "level_crossing"}]]}
        fetch = _Fetcher({"manifest.json": json.dumps({"built": "2026-10-30T03:00:00Z"}).encode(),
                          f"/{sx}/{sy}.json.gz": gzip.compress(json.dumps(shard).encode())})
        with tempfile.TemporaryDirectory() as d:
            rt = railtags.RailTags(d, fetch)
            rt.join(f, key)
            self.assertEqual(list(f.attrs["osm_railway"]), ["proposed", "rail", None], "a road keeps none")
            self.assertEqual(f.attrs["osm_gauge"][1], "1520")
            self.assertEqual(len(f._rail_nodes), 1, "the cell's nodes only")
            n = len(fetch.asked)
            railtags.RailTags(d, fetch).join(f, key)        # (from disk: the manifest's day and the shard)
            self.assertEqual(len(fetch.asked), n)

    def test_no_shard_no_tags(self):
        f = features([[(0.0, 100.0), (250.0, 100.0)]], ["standard_gauge"], subtypes=["rail"])
        f.attrs["src_record"] = np.array(["w11@3"])
        lo, la = f.lonlat[0]
        key = mapcells.cell_of(float(la), float(lo))
        fetch = _Fetcher({"manifest.json": json.dumps({"built": "x"}).encode()})
        with tempfile.TemporaryDirectory() as d:
            railtags.RailTags(d, fetch).join(f, key)
        self.assertEqual(list(f.attrs["osm_railway"]), [None])

    def test_unreachable_joins_nothing(self):
        f = features([[(0.0, 100.0), (250.0, 100.0)]], ["standard_gauge"], subtypes=["rail"])
        lo, la = f.lonlat[0]
        with tempfile.TemporaryDirectory() as d:
            railtags.RailTags(d, _Fetcher({})).join(f, mapcells.cell_of(float(la), float(lo)))
        self.assertNotIn("osm_railway", f.attrs)



class KnownPinnedTest(unittest.TestCase):
    """Shards.known(): the pinned manifest's build before this run asked for it (the tiles' cache keys the same from
    a run's start: None until the manifest came before, 2026-11-04)."""

    def test_pinned_build_known_at_once(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            s = railtags.Shards(d, None, "rail")
            self.assertIsNone(s.known())
            s2 = railtags.Shards(d, None, "rail")
            os.makedirs(s2.dir)
            with open(os.path.join(s2.dir, "manifest.json"), "w") as fh:
                json.dump({"built": "2026-11-01T00:00Z"}, fh)
            on = railtags.ON
            railtags.ON = True
            try:
                self.assertEqual(s2.known(), "2026-11-01T00:00Z")
                s2.built = "2026-11-08T00:00Z"           # (the run's own, once asked)
                self.assertEqual(s2.known(), "2026-11-08T00:00Z")
            finally:
                railtags.ON = on

if __name__ == "__main__":
    unittest.main()


class LaneTagsTest(unittest.TestCase):
    def test_road_segments_get_their_ways_lane_tags(self):
        f = features([[(0.0, 100.0), (250.0, 100.0)], [(0.0, 0.0), (5.0, 5.0)], [(0.0, 120.0), (250.0, 120.0)]],
                     ["primary", "residential", "standard_gauge"], subtypes=["road", "road", "rail"])
        f.attrs["src_record"] = np.array(["w21@3", "w22@1", "w21@3"])
        lo, la = f.lonlat[0]
        key = mapcells.cell_of(float(la), float(lo))
        sx, sy = key.x >> (key.z - 7), key.y >> (key.z - 7)
        shard = {"ways": {"21": {"turn:lanes:forward": "left|through|through;right", "lanes:forward": "3",
                                 "overtaking": "no"}}}
        fetch = _Fetcher({"lanes/manifest.json": json.dumps({"built": "2026-10-30T03:00:00Z"}).encode(),
                          f"lanes/{sx}/{sy}.json.gz": gzip.compress(json.dumps(shard).encode())})
        with tempfile.TemporaryDirectory() as d:
            railtags.LaneTags(d, fetch).join(f, key)
        self.assertEqual(list(f.attrs["osm_turn_lanes_forward"]), ["left|through|through;right", None, None],
                         "the road with the way's tags; a rail segment of the same way none")
        self.assertEqual(f.attrs["osm_overtaking"][0], "no")
        self.assertIsNone(f.attrs["osm_change_lanes"][0])

    def test_a_segment_of_several_ways_keeps_each_ways_tags(self):
        f = features([[(0.0, 100.0), (900.0, 100.0)], [(0.0, 0.0), (5.0, 5.0)]], ["primary", "residential"],
                     subtypes=["road", "road"])
        f.attrs["src_record"] = np.array(["w31@1", "w33@1"])
        f.rules["src_ways"] = mapgeom.rule_table([(0, "w30@1", [0.0, 0.25]), (0, "w31@1", [0.25, 0.75]),
                                                  (0, "w99@1", [0.75, 1.0])])
        lo, la = f.lonlat[0]
        key = mapcells.cell_of(float(la), float(lo))
        sx, sy = key.x >> (key.z - 7), key.y >> (key.z - 7)
        shard = {"ways": {"30": {"lanes:forward": "1", "lanes:backward": "2", "lanes": "3"},
                          "31": {"lanes:forward": "2", "lanes:backward": "1", "lanes": "3"}}}
        fetch = _Fetcher({"lanes/manifest.json": json.dumps({"built": "2026-10-30T03:00:00Z"}).encode(),
                          f"lanes/{sx}/{sy}.json.gz": gzip.compress(json.dumps(shard).encode())})
        with tempfile.TemporaryDirectory() as d:
            railtags.LaneTags(d, fetch).join(f, key)
        self.assertEqual(f.attrs["osm_lanes_forward"][0], "2", "the columns: the way covering most of it")
        got = roads.lane_tag_parts(f, 0)
        self.assertEqual([(a, b, w) for a, b, w, _t in got], [(0.0, 0.25, "30"), (0.25, 0.75, "31"), (0.75, 1.0, "99")])
        self.assertEqual(got[0][3]["lanes:backward"], "2")
        self.assertEqual(got[1][3]["lanes:forward"], "2")
        self.assertIsNone(got[2][3], "a way without lane tags, not its neighbour's")
        self.assertEqual(roads.lane_tag_parts(f, 1), [(0.0, 1.0, "33", None)], "one way: the whole segment")


class RuleTagsTest(unittest.TestCase):
    """The German road rules (priority_road, zone:maxspeed: the shard's "rules", apart from its "ways") and sign
    nodes of a lane shard."""

    def _join(self, shard, src_ways=None, lt_out=None):
        f = features([[(0.0, 100.0), (250.0, 100.0)], [(0.0, 0.0), (5.0, 5.0)]], ["primary", "residential"],
                     subtypes=["road", "road"])
        f.attrs["src_record"] = np.array(["w21@3", "w22@1"])
        if src_ways:
            f.rules["src_ways"] = mapgeom.rule_table(src_ways)
        lo, la = (float(v) for v in f.lonlat[0])
        key = mapcells.cell_of(la, lo)
        sx, sy = key.x >> (key.z - 7), key.y >> (key.z - 7)
        if callable(shard):
            shard = shard(lo, la)
        files = {"lanes/manifest.json": json.dumps({"built": "2026-10-30T03:00:00Z"}).encode()}
        if shard is not None:               # (None: no shard there, a 404)
            files[f"lanes/{sx}/{sy}.json.gz"] = gzip.compress(json.dumps(shard).encode())
        with tempfile.TemporaryDirectory() as d:
            lt = railtags.LaneTags(d, _Fetcher(files))
            lt.join(f, key)
            if lt_out is not None:
                lt_out.append(lt.shard(sx, sy))
        return f

    def test_rule_tags_join(self):
        f = self._join({"ways": {}, "rules": {"priority_road": {"designated": [21]},
                                              "zone:maxspeed": {"DE:30": [21], "DE:20": [22]}}})
        self.assertEqual(list(f.attrs["osm_priority_road"]), ["designated", None])
        self.assertEqual(list(f.attrs["osm_zone_maxspeed"]), ["DE:30", "DE:20"])

    def test_rules_compact_on_load(self):
        got = []
        self._join({"ways": {}, "rules": {"priority_road": {"designated": [21, 9], "end": [25]},
                                          "zone:maxspeed": {"DE:30": [21, "x", None]}, "junk": [1]}}, lt_out=got)
        rules = got[0]["rules"]
        self.assertIsInstance(rules, railtags.Rules)
        ids, idx, values = rules.keys["priority_road"]
        self.assertEqual((ids.dtype, ids.tolist(), [values[i] for i in idx]),
                         (np.dtype(np.int64), [21, 25, 30], ["designated", "end", "designated"]))
        self.assertEqual(rules.keys["zone:maxspeed"][0].tolist(), [],
                         "a list with an entry that is not a number dropped whole")
        self.assertNotIn("junk", rules.keys)
        self.assertEqual(rules.get_many("priority_road", ["30", "31", None, "25"]), ["designated", None, None, "end"])
        self.assertEqual(rules.get_many("zone:maxspeed", []), [])

    def test_rules_delta_coded(self):
        r = railtags.Rules({"priority_road": {"designated": [100, 5, 125]}})
        self.assertEqual(r.get_many("priority_road", ["100", "105", "230", "101"]),
                         ["designated", "designated", "designated", None])
        r = railtags.Rules({"priority_road": {"designated": [100, "x", 5], "end": [], "yes_unposted": 7,
                                              "x": [True, 3]}})
        self.assertEqual(r.get_many("priority_road", ["100", "105", "5", "3"]), [None] * 4)
        self.assertEqual(r.keys["priority_road"][0].dtype, np.dtype(np.int64))

    def test_sign_nodes_in_the_cell_only(self):
        f = self._join(lambda lo, la: {"ways": {}, "nodes": [[1, lo, la, {"traffic_sign": "DE:205"}],
                                                              [2, lo + 0.1, la + 0.1, {"traffic_sign": "DE:206"}]]})
        got = railtags.sign_nodes(f)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][2], {"traffic_sign": "DE:205"})

    def test_join_without_a_cell(self):
        """join(feats, None) (test_roadlanes' mocked shard): the lane tags, and no sign nodes (none to bound)."""
        f = features([[(0.0, 100.0), (250.0, 100.0)]], ["primary"], subtypes=["road"])
        f.attrs["src_record"] = np.array(["w21@3"])
        lt = railtags.LaneTags.__new__(railtags.LaneTags)
        lt.of_cell = lambda _key: {"ways": {"21": {"lanes:forward": "2"}}, "rules": {"priority_road": {"end": [21]}},
                                   "nodes": [[1, 0.0, 0.0, {"traffic_sign": "DE:205"}]]}
        lt.join(f, None)
        self.assertEqual(f.attrs["osm_lanes_forward"][0], "2")
        self.assertEqual(f.attrs["osm_priority_road"][0], "end")
        self.assertEqual(railtags.sign_nodes(f), [])

    def test_old_shard_without_nodes_or_rules(self):
        f = self._join({"ways": {"21": {"lanes:forward": "2"}}})
        self.assertEqual(list(f.attrs["osm_lanes_forward"]), ["2", None], "the lane tags still joined")
        self.assertEqual(roads.lane_tags(f, 0), {"lanes:forward": "2"})
        self.assertEqual(list(f.attrs["osm_priority_road"]), [None, None])
        self.assertEqual(list(f.attrs["osm_zone_maxspeed"]), [None, None])
        self.assertEqual(railtags.sign_nodes(f), [])

    def test_rule_only_way_stays_out_of_lane_tags(self):
        f = self._join({"ways": {}, "rules": {"priority_road": {"designated": [21]}}}, [(0, "w21@3", [0.0, 1.0])])
        self.assertIsNone(roads.lane_tags(f, 0))
        self.assertEqual([t for _a, _b, _w, t in roads.lane_tag_parts(f, 0)], [None])
        self.assertEqual(f.attrs["osm_priority_road"][0], "designated")

    def test_lane_parts_without_rule_keys(self):
        f = self._join({"ways": {"21": {"lanes:forward": "2"}},
                        "rules": {"priority_road": {"designated": [21]}, "zone:maxspeed": {"DE:30": [21]}}},
                       [(0, "w21@3", [0.0, 1.0])])
        self.assertEqual(f.attrs[railtags.LANE_PARTS][0], [(0.0, 1.0, "21", {"lanes:forward": "2"})])
        self.assertEqual(roads.lane_tags(f, 0), {"lanes:forward": "2"})
        self.assertEqual(f.attrs["osm_priority_road"][0], "designated")
        self.assertEqual(f.attrs["osm_zone_maxspeed"][0], "DE:30")

    def test_ways_reader_same_with_and_without_rules(self):
        """What a reader of the shard's "ways" alone gets (the lane columns, LANE_PARTS: what today's server reads
        and its markings take) is the same with the new "rules" and "nodes" as without; a shard of rules only is
        read like no shard at all."""
        ways = {"21": {"lanes:forward": "1", "lanes:backward": "1", "highway": "primary"}}
        rules = {"priority_road": {"designated": [21, 78]}, "zone:maxspeed": {"DE:30": [22]}}
        src = [(0, "w21@3", [0.0, 0.5]), (0, "w99@1", [0.5, 1.0]), (1, "w22@1", [0.0, 1.0])]

        def seen(shard):
            f = self._join(shard, src)
            return ({c: list(f.attrs[railtags.lane_attr(c)]) for c in railtags.LANE_COLUMNS},
                    list(f.attrs[railtags.LANE_PARTS]), [roads.lane_tags(f, i) for i in range(2)],
                    [roads.lane_tag_parts(f, i) for i in range(2)])

        old = seen({"ways": ways, "nodes": []})
        self.assertEqual(seen({"ways": ways, "nodes": [[1, 0.0, 0.0, {"traffic_sign": "DE:205"}]], "rules": rules}),
                         old)
        self.assertEqual(old[3][0][1], (0.5, 1.0, "99", None), "the rule-only way: no lane tags")
        self.assertEqual(seen({"ways": {}, "nodes": [], "rules": rules}), seen(None))


class WaysTest(unittest.TestCase):
    """A shard's ways kept compact (railtags.Ways): the same lookups as the dict, the distinct tag sets once."""

    def test_same_as_the_dict(self):
        ways = {"21": {"lanes": "3", "highway": "primary"}, "7": {"highway": "primary", "lanes": "3"},
                "900000000001": {"lane_markings": "no"}, "5": {}}
        w = railtags.Ways.of(ways)
        self.assertIsInstance(w, railtags.Ways)
        for k, v in ways.items():
            self.assertEqual(w.get(k), v)
        self.assertEqual(len(w.tags), 3, "equal tag sets kept once")
        self.assertIsNone(w.get("8"))
        self.assertIsNone(w.get(""))
        self.assertIsNone(w.get(None))
        self.assertEqual(w.get("8", "x"), "x")
        self.assertEqual(w.get_many(["21", "8", "", "900000000001"]), [ways["21"], None, None, ways["900000000001"]])
        self.assertEqual(len(w), 4)

    def test_not_plain_numbers_stay_a_dict(self):
        ways = {"w21": {"lanes": "2"}}
        self.assertIs(railtags.Ways.of(ways), ways)
        self.assertEqual(railtags._tags_of(ways, ["w21", ""]), [{"lanes": "2"}, None])
        empty = railtags.Ways.of({})
        self.assertFalse(empty)
        self.assertEqual(empty.get_many(["1"]), [None])
