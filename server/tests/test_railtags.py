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



class OutageTest(unittest.TestCase):
    """GitHub down (the night of 1.9.0: 504s for hours): the data on disk stays in use, a stale manifest is refreshed in
    the background, concurrent callers make one request, and a failed shard is not asked again by every tile."""

    class Down:
        def __init__(self, delay=0.0):
            self.calls, self.delay = [], delay

        def get(self, url, kind="demand", **_kw):
            import time
            self.calls.append(url)
            time.sleep(self.delay)
            raise netfetch.FetchFailed(url + ": HTTP 504", url)

    def test_a_stale_pin_stays_in_use_and_is_refreshed_in_the_background(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            down = self.Down(delay=0.3)
            s = railtags.Shards(d, down, "rail")
            os.makedirs(s.dir)
            pin = os.path.join(s.dir, "manifest.json")
            with open(pin, "w") as fh:
                json.dump({"built": "old"}, fh)
            old = time.time() - railtags.MANIFEST_TTL_S - 60
            os.utime(pin, (old, old))
            t = time.time()
            self.assertEqual(s.version(), "old")
            self.assertLess(time.time() - t, 0.2, "no wait for the network")
            for _ in range(50):
                if s.fetching is None and down.calls:
                    break
                time.sleep(0.05)
            self.assertEqual(len(down.calls), 1, "one background refresh")
            self.assertEqual(s.version(), "old", "kept when the refresh failed")
            self.assertEqual(len(down.calls), 1, "not asked again at once")

    def test_without_a_pin_concurrent_callers_make_one_request(self):
        import threading
        with tempfile.TemporaryDirectory() as d:
            down = self.Down(delay=0.3)
            s = railtags.Shards(d, down, "rail")
            got = []
            ts = [threading.Thread(target=lambda: got.append(s.version())) for _ in range(6)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(got, [None] * 6)
            self.assertEqual(len(down.calls), 1)

    def test_a_failed_shard_is_not_asked_by_every_tile(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []

            class Half:
                def get(self, url, kind="demand", **_kw):
                    calls.append(url)
                    if url.endswith("manifest.json"):
                        return type("R", (), {"body": json.dumps({"built": "b"}).encode()})()
                    raise netfetch.FetchFailed(url + ": HTTP 504", url)
            s = railtags.Shards(d, Half(), "rail")
            self.assertIsNone(s.shard(10, 20))
            self.assertIsNone(s.shard(10, 20))
            self.assertEqual(sum(u.endswith("/10/20.json.gz") for u in calls), 1)


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

def _shard_xy(lat, lon):
    key = mapcells.cell_of(lat, lon)
    return key.x >> (key.z - 7), key.y >> (key.z - 7)


class StationTagsTest(unittest.TestCase):
    """The weekly station shards (tools/build_station_tags.py, dataset "stations"; Bauer33333's find on #716): fuel and
    charging stations with capacity, and the charge points, in a lat/lon box."""

    def fetcher(self, shards, manifest=True):
        files = {f"/{x}/{y}.json.gz": gzip.compress(json.dumps(s).encode()) for (x, y), s in shards.items()}
        if manifest:
            files["stations/manifest.json"] = json.dumps({"built": "2026-11-13T03:00:00Z", "format": 1}).encode()
        return _Fetcher(files)

    def test_stations_and_points_in_a_box(self):
        xy = _shard_xy(34.849, -117.085)
        sh = {"s": [["n2", "ev", -117.0854, 34.8491, "Tesla Supercharger", "Tesla", 8],
                    ["n1", "fuel", -117.085, 34.849, "Arco", "", None],
                    ["n9", "fuel", -117.5, 34.9, "Far", "", None]],
              "p": [[-117.0853, 34.8492], [-117.6, 34.95]]}
        with tempfile.TemporaryDirectory() as d:
            st = railtags.StationTags(d, self.fetcher({xy: sh}))
            got = st.stations_in(34.84, -117.09, 34.86, -117.08)
            self.assertEqual(sorted(r["id"] for r in got), ["n1", "n2"])
            ev = [r for r in got if r["id"] == "n2"][0]
            self.assertEqual((ev["kind"], ev["name"], ev["brand"], ev["cap"]), ("ev", "Tesla Supercharger", "Tesla", 8))
            self.assertAlmostEqual(ev["lat"], 34.8491)
            self.assertEqual(st.points_in(34.84, -117.09, 34.86, -117.08), [(34.8492, -117.0853)])

    def test_box_over_two_shards(self):
        a, b = (34.0, -117.0), (34.0, -114.0)           # (~280 km apart: two z7 shards)
        xa, xb = _shard_xy(*a), _shard_xy(*b)
        self.assertNotEqual(xa, xb)
        with tempfile.TemporaryDirectory() as d:
            st = railtags.StationTags(d, self.fetcher({xa: {"s": [["n1", "fuel", a[1], a[0], "", "", None]], "p": []},
                                                       xb: {"s": [["n2", "fuel", b[1], b[0], "", "", None]], "p": []}}))
            self.assertEqual(sorted(r["id"] for r in st.stations_in(33.9, -117.1, 34.1, -113.9)), ["n1", "n2"])

    def test_no_shard_there_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            st = railtags.StationTags(d, self.fetcher({}))
            self.assertEqual(st.stations_in(0.0, 0.0, 0.1, 0.1), [])
            self.assertEqual(st.points_in(0.0, 0.0, 0.1, 0.1), [])

    def test_unreachable_is_none(self):
        class Down:
            def get(self, url, kind="demand", **_kw):
                raise OSError("offline")
        with tempfile.TemporaryDirectory() as d:
            st = railtags.StationTags(d, Down())
            self.assertIsNone(st.stations_in(34.84, -117.09, 34.86, -117.08))

    def test_off_reads_nothing(self):
        f = self.fetcher({})
        old = railtags.STATIONS_ON
        railtags.STATIONS_ON = False
        try:
            with tempfile.TemporaryDirectory() as d:
                self.assertIsNone(railtags.StationTags(d, f).stations_in(34.84, -117.09, 34.86, -117.08))
        finally:
            railtags.STATIONS_ON = old
        self.assertEqual(f.asked, [], "off: nothing fetched")

    def test_local_folder(self):
        xy = _shard_xy(52.519, 13.414)
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, str(xy[0])))
            with gzip.open(os.path.join(d, str(xy[0]), f"{xy[1]}.json.gz"), "wt", encoding="utf-8") as fh:
                json.dump({"s": [["n3", "ev", 13.414, 52.519, "", "", 2]], "p": [[13.4141, 52.5191]]}, fh)
            with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as fh:
                json.dump({"built": "local test"}, fh)
            st = railtags.StationTags(None, None, local=d)
            self.assertEqual([r["cap"] for r in st.stations_in(52.51, 13.40, 52.53, 13.42)], [2])

    def test_shed_lets_go_of_the_shards(self):
        # Codex on #1111 (2026-11-10): the budget's shedder must let go of the decoded shards, and the per-shard cell
        # index (by_cell) holds each shard, so it goes too
        xy = _shard_xy(34.849, -117.085)
        sh = {"s": [["n1", "fuel", -117.085, 34.849, "Arco", "", None]], "p": [[-117.0853, 34.8492]]}
        with tempfile.TemporaryDirectory() as d:
            st = railtags.StationTags(d, self.fetcher({xy: sh}))
            self.assertEqual(len(st.stations_in(34.84, -117.09, 34.86, -117.08)), 1)
            self.assertTrue(st.shards and st.by_cell)
            st.clear_memory()
            self.assertEqual((st.shards, st.by_cell), ({}, {}))
            self.assertEqual(len(st.stations_in(34.84, -117.09, 34.86, -117.08)), 1, "read again when asked")


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


class RuleTagsTest(unittest.TestCase):
    """The German road rules (priority_road, zone:maxspeed: the shard's "rules", apart from its "ways") and sign
    nodes of a lane shard."""

    def _join(self, shard, src_ways=None, lt_out=None, from_side=False):
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
            if from_side:       # (the next process: the shard's decoded form beside it, the shard itself gone)
                side = glob.glob(os.path.join(d, "pagesdata", "lanes", "*", f"{sx}_{sy}{railtags.IDX_SUFFIX}"))
                self.assertEqual(len(side), 1)
                os.remove(side[0][:-len(railtags.IDX_SUFFIX)] + ".json.gz")
                for c in railtags.RULE_COLUMNS:
                    f.attrs.pop(railtags.lane_attr(c))
                lt = railtags.LaneTags(d, _Fetcher({"lanes/manifest.json": files["lanes/manifest.json"]}))
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

    def test_rules_from_the_side_file(self):
        got = []
        f = self._join({"ways": {}, "rules": {"priority_road": {"designated": [21]}, "zone:maxspeed": {"DE:20": [22]}}},
                       lt_out=got, from_side=True)
        self.assertIsInstance(got[0]["rules"], railtags.Rules, "made Rules as from the shard")
        self.assertEqual(list(f.attrs["osm_priority_road"]), ["designated", None])
        self.assertEqual(list(f.attrs["osm_zone_maxspeed"]), [None, "DE:20"])

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
