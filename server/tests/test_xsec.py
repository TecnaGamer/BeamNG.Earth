"""The road system's cross-sections (BNE_ROADSYS_XSEC; native/roadsys xsec.rs): the lane shards' "xsec" table
(tools/build_lane_tags.py --xsec, build_rail_tags.write), its reader (railtags.XsecTable, CIP slices) and the texts the
kernels read (roadsys._xsec_text)."""
import gzip
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.modules.setdefault("osmium", types.ModuleType("osmium"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
sys.path.append(str(Path(__file__).resolve().parents[1]))

import build_lane_tags as lt  # noqa: E402
import build_rail_tags as rail  # noqa: E402
import railtags  # noqa: E402
import roadsys  # noqa: E402


def _way(oid, tags, refs):
    return SimpleNamespace(id=oid, tags=tags, nodes=[SimpleNamespace(ref=r) for r in refs], is_node=lambda: False)


NID = np.arange(1, 7, dtype=np.int64)
NLON = np.array([13.40, 13.41, 13.42, 13.43, 13.44, 13.45])
NLAT = np.array([52.50, 52.51, 52.52, 52.50, 52.51, 52.52])
OBJECTS = [
    _way(11, {"highway": "residential", "parking:both": "lane", "parking:both:orientation": "parallel",
              "cycleway:both": "no", "sidewalk:both": "separate"}, [1, 2]),
    _way(12, {"highway": "primary", "turn:lanes": "left|through", "lanes": "2", "cycleway:right": "lane",
              "cycleway:right:width": "1.6"}, [2, 3]),
    _way(13, {"highway": "footway", "sidewalk:both": "no"}, [3, 4]),            # (no drivable highway: not kept)
    _way(14, {"highway": "tertiary", "parking:left": "lane", "width:carriageway": "9.5",
              "note": "not a cross-section key"}, [4, 5]),
    _way(15, {"highway": "residential", "parking:both": "lane", "parking:both:orientation": "parallel",
              "cycleway:both": "no", "sidewalk:both": "separate"}, [5, 6]),
]


def _shards(out):
    got = {}
    for x in sorted(os.listdir(out)):
        if x.isdigit():
            for f in sorted(os.listdir(os.path.join(out, x))):
                with gzip.open(os.path.join(out, x, f), "rb") as fh:
                    got[(int(x), int(f.split(".")[0]))] = json.loads(fh.read())
    return got


class BuilderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, xsec):
        rows = lt.XsecRows() if xsec else None
        ways, rules, nodes = lt._first_pass(OBJECTS, rows)
        out = os.path.join(self.tmp.name, "on" if xsec else "off")
        rail.write(out, ways, nodes, NID, NLON, NLAT, rules=rules, xsec=rows)
        return _shards(out), rows

    def test_only_drivable_ways_with_street_tags_their_keys_only(self):
        self.assertIsNone(lt.xsec_way({"highway": "footway", "sidewalk:both": "no"}))
        self.assertIsNone(lt.xsec_way({"highway": "residential", "name": "A"}))
        self.assertEqual(lt.xsec_way(OBJECTS[3].tags), {"parking:left": "lane", "width:carriageway": "9.5"})

    def test_ways_unchanged_and_the_table_apart(self):
        off, _ = self.build(False)
        on, rows = self.build(True)
        self.assertEqual(len(rows), 4)
        self.assertEqual(set(off), set(on))
        for k in off:
            self.assertEqual(json.dumps(on[k]["ways"]), json.dumps(off[k]["ways"]))
            self.assertNotIn("xsec", off[k])
        sh = on[(68, 41)]
        t = railtags.XsecTable(sh["xsec"])
        got = t.get_many([11, 12, 13, 14, 15, 99, None])
        self.assertEqual(got[0], {"parking:both": "lane", "parking:both:orientation": "parallel", "cycleway:both": "no",
                                  "sidewalk:both": "separate"})
        self.assertEqual(got[1], {"cycleway:right": "lane", "cycleway:right:width": "1.6"})
        self.assertIsNone(got[2])
        self.assertEqual(got[3], {"parking:left": "lane", "width:carriageway": "9.5"})
        self.assertEqual(got[4], got[0])
        self.assertEqual(got[5:], [None, None])
        # (one row per distinct tag set; ids delta-coded)
        self.assertEqual(len(sh["xsec"]["sets"]), 3)
        self.assertEqual(sh["xsec"]["ids"], [11, 1, 2, 1])

    def test_the_same_shards_whatever_the_order(self):
        a, _ = self.build(True)
        OBJECTS.reverse()
        try:
            b, _ = self.build(True)
        finally:
            OBJECTS.reverse()
        self.assertEqual(json.dumps(a[(68, 41)]["xsec"]), json.dumps(b[(68, 41)]["xsec"]))


class ReaderTest(unittest.TestCase):
    def test_a_bad_table_reads_as_empty(self):
        t = railtags.XsecTable({"keys": ["a"], "sets": [["x"]], "ids": [5, 1], "set": [0, 3]})
        self.assertEqual(t.get_many([5, 6]), [None, None])

    def test_cip_slices_keep_the_rows_of_their_ways(self):
        doc = {"ways": {}, "nodes": [], "xsec": rail._xsec_table([(11, {"parking:both": "lane"}), (12, {"cycleway:right": "lane"}),
                                                                 (14, {"parking:left": "no"})])}
        key = SimpleNamespace(bounds=lambda: (13.0, 52.0, 14.0, 53.0))
        sl = railtags.cell_slice(doc, key, ["12", "14", "99"])
        back = railtags.shard_of_slice(json.loads(json.dumps(sl)))
        t = railtags.xsec_table(back, None)
        self.assertEqual(t.get_many([11, 12, 14]), [None, {"cycleway:right": "lane"}, {"parking:left": "no"}])
        self.assertNotIn("xsec", railtags.cell_slice({"ways": {}, "nodes": []}, key, ["12"]))


class TextTest(unittest.TestCase):
    def test_sorted_pairs_with_osm_width_and_sidewalk(self):
        feats = SimpleNamespace(attrs={railtags.XSEC_ATTR: np.array([{"parking:right": "lane", "cycleway:left": "lane"}, None], object),
                                       "osm_width": np.array([9.5, 7.0]), "osm_sidewalk": np.array(["both", None], object)})
        self.assertEqual(roadsys._xsec_text(feats, 0),
                         "cycleway:left=lane\x1fparking:right=lane\x1fsidewalk=both\x1fwidth=9.5")
        self.assertEqual(roadsys._xsec_text(feats, 1), "")
        # (a separator inside a value never splits a pair)
        feats.attrs[railtags.XSEC_ATTR][1] = {"parking:both": "lane\x1fx"}
        self.assertEqual(roadsys._xsec_text(feats, 1), "parking:both=lane x\x1fwidth=7.0")


if __name__ == "__main__":
    unittest.main()
