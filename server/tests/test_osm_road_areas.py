"""OSM's mapped road areas (area:highway=*: tools/build_osm_road_areas.py -> PMTiles parts; server/osmareas.py +
earthfast roadareas.rs read a tile; native/roadsys areas.rs audits and fits): the builder's ring assembly and drops,
its blob read back natively, a polygon on a tile edge in both tiles, a staged build read through BNE_OSMAREAS_DIR, the
switch off, and the road system's products with areas (the audit in the record, the fit keyed and only where on).

No network: the builder's passes are fed in memory (the osmium passes run only where pyosmium is installed)."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve()
sys.path[:0] = [str(HERE.parents[1]), str(HERE.parents[2] / "tools")]
import build_osm_road_areas as ba  # noqa: E402
import fast  # noqa: E402
import osmareas  # noqa: E402

try:
    import osmium  # noqa: F401
except ImportError:
    osmium = None

LAT0, LON0 = 51.10714, 17.03269          # (Wroclaw: the densest area:highway mapping, 2026-11-23)
E7 = 10_000_000


def square(lon, lat, d):
    """A closed square ring of node refs' places (x, y in 1e-7 degrees), side d degrees."""
    x0, y0, s = int(lon * E7), int(lat * E7), int(d * E7)
    return np.array([[x0, y0], [x0 + s, y0], [x0 + s, y0 + s], [x0, y0 + s], [x0, y0]], np.int64)


class Rings(unittest.TestCase):
    def test_member_ways_join_in_member_order_either_way_round(self):
        # three ways of one outer ring, the second reversed; a fourth closed on its own
        got = ba.join_rings([[1, 2, 3], [5, 4, 3], [5, 6, 1], [9, 8, 7, 9]])
        self.assertEqual(got, [[1, 2, 3, 4, 5, 6, 1], [9, 8, 7, 9]])

    def test_a_ring_that_does_not_close_drops_the_relation(self):
        self.assertIsNone(ba.join_rings([[1, 2, 3], [3, 4]]))

    def test_polygons_and_their_drops(self):
        nid = np.arange(1, 20, dtype=np.int64)
        nx = 170_000_000 + nid * 1000
        ny = 510_000_000 + (nid % 4) * 1000
        ways = {10: ({"area:highway": "residential"}, np.array([1, 2, 3, 4, 1], np.int64)),
                11: ({"area:highway": "footway"}, np.array([1, 2, 3], np.int64)),          # open: dropped
                12: ({"area:highway": "service"}, np.array([1, 2, 99, 4, 1], np.int64))}  # a node missing
        rels = {7: ({"area:highway": "primary", "type": "multipolygon"}, [(20, "outer"), (21, "outer"), (22, "inner"),
                                                                          (30, "street")])}
        member = {20: np.array([5, 6, 7], np.int64), 21: np.array([7, 8, 5], np.int64),
                  22: np.array([9, 10, 11, 9], np.int64)}
        from collections import Counter
        c = Counter()
        polys = ba.polygons(ways, rels, member, nid, nx, ny, c)
        self.assertEqual([p[0] for p in polys], [15, 20])          # relation 7 (2 x 7 + 1), way 10
        self.assertEqual((c["open_way"], c["missing_node"]), (1, 1))
        rel = polys[0]
        self.assertEqual(rel[2], [30])                              # the street member
        self.assertEqual([r[0] for r in rel[3]], [0, 1])


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VER" + "SION", 0) >= osmareas.NATIVE, "native module")
class Tiles(unittest.TestCase):
    def poly(self, key, value, lon, lat, d, **tags):
        return (key, {"area:highway": value, **tags}, [], [(0, square(lon, lat, d))])

    def test_round_trip(self):
        polys = [self.poly(4, "secondary", LON0, LAT0, 0.0002, name="Ulica"),
                 (11, {"area:highway": "primary", "layer": "1", "bridge": "yes"}, [123, 456],
                  [(0, square(LON0, LAT0, 0.0003)), (1, square(LON0 + 0.0001, LAT0 + 0.0001, 0.00005))])]
        keys = np.array([p[0] for p in polys], np.uint64)
        for blob in (ba.encode_tile(keys, polys), ba.ots.deflate(ba.encode_tile(keys, polys))):
            t = fast._ef.oah_decode(blob)
            k, values, names, layer, flags, lo, links, rp, rr, po, pts = t
            self.assertEqual(k.tolist(), [4, 11])
            self.assertEqual(values, ["secondary", "primary"])
            self.assertEqual(names, ["Ulica", None])
            self.assertEqual((layer.tolist(), flags.tolist()), ([0, 1], [0, 1]))
            self.assertEqual((lo.tolist(), links.tolist()), ([0, 0, 2], [123, 456]))
            self.assertEqual((rp.tolist(), rr.tolist(), po.tolist()), ([0, 1, 1], [0, 0, 1], [0, 5, 10, 15]))
            np.testing.assert_array_equal(pts[5:10], square(LON0, LAT0, 0.0003) / 1e7)

    def test_damaged_tiles_raise(self):
        polys = [self.poly(4, "secondary", LON0, LAT0, 0.0002)]
        b = ba.encode_tile(np.array([4], np.uint64), polys)
        for bad in (b[:-1], b"OAH1", b"", b"OAH1\x00\xff\xff\xff\xff\x0f"):
            with self.assertRaises(ValueError):
                fast._ef.oah_decode(bad)

    def test_a_polygon_on_a_tile_edge_is_in_both(self):
        import mapcells
        k = mapcells.cell_of(LAT0, LON0)
        edge = (k.x + 1) / (1 << k.z) * 360.0 - 180.0
        got = ba.cut([self.poly(2, "residential", edge - 0.0001, LAT0, 0.0002)])
        self.assertEqual(sorted(got), [(k.x, k.y), (k.x + 1, k.y)])
        for keys, recs in got.values():
            self.assertEqual(keys.tolist(), [2])


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VER" + "SION", 0) >= osmareas.NATIVE, "native module")
class Staged(unittest.TestCase):
    """A staged build on disk (BNE_OSMAREAS_DIR) read by osmareas as the server would."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="osmareas-test-")
        polys = [(2 * 10, {"area:highway": "secondary"}, [], [(0, square(LON0, LAT0, 0.0004))]),
                 (2 * 11, {"area:highway": "traffic_island"}, [], [(0, square(LON0 + 0.0001, LAT0 + 0.0001, 0.00005))])]
        tiles = ba.cut(polys)
        cls.manifest = ba.by.write_build(tiles, ba.encode_tile, os.path.join(cls.tmp, "build"), z=14,
                                         part_bytes=ba.PART_BYTES, bbox=None, prefix="osmareas", title="test",
                                         fmt="oah1", fmt_num=1, source="test", source_etag=None, log=lambda s: None,
                                         t0=0.0)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.saved = (osmareas.ON, osmareas.FIT, osmareas.DIR)
        osmareas._SHARDS.clear()
        osmareas._MEM.clear()

    def tearDown(self):
        osmareas.ON, osmareas.FIT, osmareas.DIR = self.saved
        osmareas._SHARDS.clear()
        osmareas._MEM.clear()

    class Md:
        def __init__(self, d):
            self.cache_dir, self.fetcher = d, None

    def test_read_back_in_a_frame(self):
        import geodesy
        import mapcells
        osmareas.ON, osmareas.DIR = True, os.path.join(self.tmp, "build")
        md = self.Md(os.path.join(self.tmp, "cache"))
        frame = geodesy.EnuFrame(LAT0, LON0)
        cells = mapcells.cells_for_bbox(LAT0 - 1e-4, LON0 - 1e-4, LAT0 + 1e-4, LON0 + 1e-4)
        polys = osmareas.polygons_for(md, cells, frame)
        self.assertEqual(sorted(polys), [20, 22])
        v, layer, flags, rings = polys[20]
        self.assertEqual(v, "secondary")
        xy = rings[0][1]
        self.assertLess(np.abs(xy[0]).max(), 0.5)            # the square's corner is the frame's origin
        self.assertGreater(xy[:, 0].max(), 20.0)
        self.assertEqual(osmareas.identity(md), self.manifest["built"] + "/" + self.manifest["key"])
        a = osmareas.areas_for(md, cells, frame)
        self.assertEqual(a.count(), 2)

    def test_off(self):
        osmareas.ON, osmareas.DIR = False, os.path.join(self.tmp, "build")
        md = self.Md(os.path.join(self.tmp, "cache"))
        self.assertEqual(osmareas.identity(md), "off")
        self.assertIsNone(osmareas.areas_for(md, [], None))


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VER" + "SION", 0) >= osmareas.NATIVE, "native module")
class Product(unittest.TestCase):
    """RoadsysSegments with areas: the record's audit, the fit only with fit=True, the keys moving with it."""

    def segs(self, areas=None, fit=False):
        xy = np.array([[-300.0, 0.0], [0.0, 0.0], [0.0, 0.0], [300.0, 0.0]])
        style = ("white", "", 0.12, 0.15, 0.15, 0.2, (3.0, 9.0), (3.0, 9.0), (3.0, 9.0), False)
        return fast._ef.RoadsysSegments(
            ["a|0", "b|0"], ["c", "c"], [("DE", "secondary", "", "", ""), ("DE", "secondary", "", "", "")],
            np.array([[9.0, 7.0, 50.0, 3.5]] * 2), np.array([[2, 1, 4, 0, 0, 2, -1, -1, 0]] * 2, np.int64), xy,
            np.array([0, 2, 4], np.int64), np.array([0, 1], np.int64), ["n0", "n0"], np.array([1, 0], np.int64),
            np.zeros(0, np.int64), np.zeros((0, 4)), style, areas=areas, fit=fit)

    def areas(self):
        ring = np.array([[-300.0, -4.0], [300.0, -4.0], [300.0, 6.0], [-300.0, 6.0], [-300.0, -4.0]])
        return fast._ef.RoadsysAreas(np.array([2], np.uint64), ["secondary"], np.zeros(1, np.int64),
                                     np.zeros(1, np.int64), np.zeros(1, np.int64), np.zeros(1, np.int64),
                                     np.array([0, 5], np.int64), ring, 99)

    def build(self, segs):
        fast._ef.roadsys_clear()
        q = [(-128.0, -128.0), (128.0, -128.0), (128.0, 128.0), (-128.0, 128.0)]
        return fast._ef.roadsys_build(segs, q, (0, 0), 1, lambda e, n: np.full(len(e), 10.0))

    def test_audit_fit_and_keys(self):
        j0, b0 = self.build(self.segs())
        ja, ba_ = self.build(self.segs(self.areas()))
        jf, bf = self.build(self.segs(self.areas(), fit=True))
        r0, ra, rf = json.loads(j0), json.loads(ja), json.loads(jf)
        self.assertNotIn("areas", r0)
        self.assertEqual(bytes(b0), bytes(ba_))                 # (the audit alone changes nothing built)
        self.assertEqual(r0["keys"], ra["keys"])
        self.assertFalse(ra["areas_on"])
        self.assertTrue(rf["areas_on"])
        self.assertNotEqual(bytes(b0), bytes(bf))
        self.assertNotEqual(r0["keys"], rf["keys"])
        self.assertGreater(rf["areas_fit"]["fitted"], 0)
        # the fit brings the surface onto the mapped edges
        self.assertLess(rf["areas"]["edge"]["all"]["p95"], ra["areas"]["edge"]["all"]["p95"])
        self.assertGreater(rf["areas"]["iou"], ra["areas"]["iou"])

    def test_a_staged_audit_input_runs(self):
        quad = [[0.0, -50.0], [100.0, -50.0], [100.0, 50.0], [0.0, 50.0]]
        sys.path.insert(0, str(HERE.parents[2] / "tools" / "widecorpus"))
        import checks
        t = np.array([[[0.0, -6.0, 0], [100.0, -6.0, 0], [100.0, 6.0, 0]], [[0.0, -6.0, 0], [100.0, 6.0, 0], [0.0, 6.0, 0]]])
        polys = {2: ("secondary", 0, 0, [(0, np.array([[0.0, -6.0], [100.0, -6.0], [100.0, 6.0], [0.0, 6.0], [0.0, -6.0]]))])}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.bin")
            checks.areas_input(path, quad, [(False, t)], [dict(xy=[[-10.0, 0.0], [110.0, 0.0]], width=12.0)], polys)
            with open(path, "rb") as f:
                got = json.loads(fast._ef.roadsys_areas_audit(f.read()))
        self.assertGreater(got["iou"], 0.99)
        self.assertLessEqual(got["edge"]["secondary"]["p95"], 0.3)


@unittest.skipUnless(osmium is not None, "pyosmium")
class Osmium(unittest.TestCase):
    def test_an_extract_end_to_end(self):
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6">
 <node id="1" lat="51.1" lon="17.0"/><node id="2" lat="51.1" lon="17.001"/><node id="3" lat="51.1005" lon="17.001"/>
 <node id="4" lat="51.1005" lon="17.0"/>
 <way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/><tag k="area:highway" v="residential"/></way>
</osm>"""
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "x.osm")
            with open(src, "w") as f:
                f.write(xml)
            m = ba.build(src, os.path.join(d, "out"), log=lambda s: None)
            self.assertEqual(m["counts"]["polygons"], 1)


if __name__ == "__main__":
    unittest.main()
