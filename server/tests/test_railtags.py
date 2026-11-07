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

class SignNodesTest(unittest.TestCase):
    """The lane shards' German sign nodes (tools/build_lane_tags.py: the Ortstafeln) kept per cell by LaneTags.join
    for railtags.sign_nodes; the shard builder's records of them."""

    def joined(self, shard):
        f = features([[(0.0, 100.0), (250.0, 100.0)]], ["tertiary"], subtypes=["road"])
        f.attrs["src_record"] = np.array(["w21@1"])
        lo, la = f.lonlat[0]
        key = mapcells.cell_of(float(la), float(lo))
        sx, sy = key.x >> (key.z - 7), key.y >> (key.z - 7)
        fetch = _Fetcher({"lanes/manifest.json": json.dumps({"built": "2026-11-07T03:00:00Z"}).encode(),
                          f"lanes/{sx}/{sy}.json.gz": gzip.compress(json.dumps(shard(key)).encode())})
        with tempfile.TemporaryDirectory() as d:
            railtags.LaneTags(d, fetch).join(f, key)
        return f

    def test_sign_nodes_in_the_cell_only(self):
        def shard(key):
            lon0, lat0, lon1, lat1 = key.bounds()
            mid = ((lon0 + lon1) / 2, (lat0 + lat1) / 2)
            return {"ways": {}, "nodes": [[1, mid[0], mid[1], {"traffic_sign": "city_limit", "name": "Sarching"}],
                                          [2, lon1 + 0.05, lat1, {"traffic_sign": "city_limit", "name": "Far"}]]}
        got = railtags.sign_nodes(self.joined(shard))
        self.assertEqual([t["name"] for _lo, _la, t in got], ["Sarching"])

    def test_old_shard_without_nodes(self):
        self.assertEqual(railtags.sign_nodes(self.joined(lambda key: {"ways": {}})), [])

    def test_the_builders_records(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(
            __file__)))), "tools"))
        try:
            import build_lane_tags as b
        except ImportError as e:            # (pyosmium: the data repo's Action has it)
            self.skipTest(f"build_lane_tags: {e}")
        self.assertEqual(b.sign_records({"traffic_sign": "city_limit", "name": "Sarching"}),
                         [{"traffic_sign": "city_limit", "name": "Sarching"}])
        self.assertEqual(b.sign_records({"traffic_sign": "DE:310,DE:311", "direction": "forward", "name": "A / B"}),
                         [{"traffic_sign": "DE:310,DE:311", "name": "A / B", "direction": "forward"}])
        self.assertEqual(b.sign_records({"traffic_sign": "310"}), [{"traffic_sign": "DE:310"}])
        self.assertEqual(b.sign_records({"traffic_sign:backward": "city_limit"}),
                         [{"traffic_sign": "city_limit", "direction": "backward"}])
        self.assertEqual(b.sign_records({"traffic_sign": "DE:274[50]"}), [], "not a town sign")
        self.assertEqual(b.sign_records({"traffic_sign": "DE:3100"}), [], "not 310")
        self.assertTrue(b.in_box(12.2, 49.0))
        self.assertFalse(b.in_box(2.35, 48.85), "Paris: outside Germany's box")


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
