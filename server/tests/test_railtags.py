"""railtags.py: the weekly rail shards joined to Overture's rail segments by OSM way id."""
import glob
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
            # (the shard's decoded form beside it, read by the next process instead of the shard: the same tags)
            side = glob.glob(os.path.join(d, "pagesdata", "rail", "*", f"{sx}_{sy}{railtags.IDX_SUFFIX}"))
            self.assertEqual(len(side), 1)
            os.remove(side[0][:-len(railtags.IDX_SUFFIX)] + ".json.gz")
            f.attrs.pop("osm_railway")
            railtags.RailTags(d, fetch).join(f, key)
            self.assertEqual(list(f.attrs["osm_railway"]), ["proposed", "rail", None])
            self.assertEqual(len(f._rail_nodes), 1)
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

    def test_the_same_as_before_on_odd_records(self):
        """The native way id search (fast.way_tag_rows / lane_parts) against the join as it was (lane_join_ref), on
        records the regex reads its own way: Unicode digits after ASCII ones, too many digits for int64, no "w", a
        bare "w", leading zeros, a feature's ways out of order in the rule table, rows of rail and of no feature,
        object columns, and a shard of plain-dict ways (ids that are not numbers) and none at all."""
        ar = chr(0x663)                 # (ARABIC-INDIC DIGIT THREE: a \\d of the regex)
        ok = ["w21@3", "w22@1", "w0021@2", "w21" + ar + "@1", "x21@1", "w", "", "w" + ar + "21", "w99@1", "w5@1", "w21"]
        for recs in (ok, ok + ["w" + "9" * 25 + "@1"]):     # (the second: the join stops, as it did, past int64)
            n = len(recs)
            lines = [[(0.0, 10.0 * i), (250.0, 10.0 * i)] for i in range(n)]
            subs = ["road"] * n
            subs[8] = "rail"
            rows = []
            for i in range(n):
                rows += [(i, recs[(i + 1) % n], [0.5, 1.0]), (i, recs[i], [0.0, 0.5])]
            rows += [(n + 3, "w21@1", None), (-1, "w21@1", None)]
            shard = {"ways": {"21": {"lanes:forward": "2", "lanes": "3"}, "22": {"turn:lanes": "left|through"},
                              "213": {"lane_markings": "no"}, "5": {}, "99": {"overtaking": "no"}}}
            for ways in (railtags.Ways.of(dict(shard["ways"])), {"w" + k: v for k, v in shard["ways"].items()},
                         shard["ways"], {}):
                for obj in (False, True):
                    f = features(lines, ["primary"] * n, subtypes=subs)
                    f.attrs["src_record"] = np.array(recs, object if obj else None)
                    f.rules["src_ways"] = mapgeom.rule_table(rows)
                    lt = railtags.LaneTags(None, None)
                    lt.of_cell = lambda key, w=ways: {"ways": w}
                    try:
                        lane_join_ref(lt, f, None)
                        err = None
                    except OverflowError as e:
                        err = e
                    want = {k: f.attrs.pop(k) for k in list(f.attrs) if k.startswith("osm_")}
                    if err is not None:
                        self.assertRaises(OverflowError, lt.join, f, None)
                        continue
                    lt.join(f, None)
                    got = {k: f.attrs[k] for k in f.attrs if k.startswith("osm_")}
                    self.assertTrue(lane_attrs_equal(want, got) and lane_attrs_equal(got, want), (type(ways), obj))
                    if recs is ok and isinstance(ways, railtags.Ways):
                        self.assertEqual(roads.lane_tag_parts(f, 3)[1][2], "21" + ar, "the regex's Unicode digits")
                        self.assertEqual(f.attrs["osm_lane_markings"][3], "no")


def lane_join_ref(lane_tags, feats, key):
    """LaneTags.join as it was before 2026-11-08 (a lookup per road and per way part): the oracle the new one must
    equal, column for column and part for part."""
    import re
    way_id = lambda record: (lambda m: m.group(1) if m else None)(re.match(r"^w(\d+)", record or ""))  # noqa: E731
    sub = feats.attrs.get("subtype")
    if sub is None or not len(feats):
        return
    road = np.flatnonzero(np.asarray(sub).astype(str) == "road")
    if not len(road):
        return
    sh = lane_tags.of_cell(key)
    if sh is None:
        return
    ways = sh.get("ways", {})
    rec = feats.attrs.get("src_record")
    cols = {c: np.full(len(feats), None, object) for c in railtags.LANE_COLUMNS}
    if rec is not None and ways:
        for i, t in zip(road, railtags._tags_of(ways, [way_id(str(rec[i])) or "" for i in road])):
            if t:
                for c in railtags.LANE_COLUMNS:
                    if c in t:
                        cols[c][i] = t[c]
    for c in railtags.LANE_COLUMNS:
        feats.attrs[railtags.lane_attr(c)] = cols[c]
    table = feats.rules.get("src_ways")
    parts = np.full(len(feats), None, object)
    if table is not None and len(table):
        for i in road:
            rows = table.for_feature(int(i))
            if rows:
                parts[i] = [(a, b, way_id(r), dict(ways.get(way_id(r)) or {})) for r, a, b in rows]
    feats.attrs[railtags.LANE_PARTS] = parts
    import roadlanes
    roadlanes.invalidate(feats)


def lane_attrs_equal(a, b):
    """Are two joins' lane attrs (dicts of the attr columns) the same, value for value and type for type?"""
    keys = [railtags.lane_attr(c) for c in railtags.LANE_COLUMNS] + [railtags.LANE_PARTS]
    for k in keys:
        x, y = a.get(k), b.get(k)
        if (x is None) != (y is None):
            return False
        if x is None:
            continue
        if x.dtype != y.dtype or x.shape != y.shape:
            return False
        for u, v in zip(x.tolist(), y.tolist()):
            if type(u) is not type(v) or u != v:
                return False
            if isinstance(u, list) and any(type(p) is not type(q) or len(p) != len(q)
                                            or any(type(e) is not type(g) for e, g in zip(p, q)) for p, q in zip(u, v)):
                return False
    return True


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
