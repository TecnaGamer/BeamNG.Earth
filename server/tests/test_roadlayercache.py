"""roadlayercache: a decoded roads layer kept on disk comes back as it was (arrays, attribute order, the joined tags'
object columns with their tuples, the layer's kept state), only under its key and while the neighbour cells it read
are unchanged; a layer holding something that cannot be given back exactly is not kept."""
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import mapgeom  # noqa: E402
import roadlayercache as rc  # noqa: E402


def layer():
    f = mapgeom.Features("roads", np.array(["a", "b", "c"]), np.array([1, 1, 1], np.uint8),
                         np.array([0, 1, 2, 3], np.int64), np.array([0, 1, 2, 3], np.int64),
                         np.array([0, 2, 4, 7], np.int64), np.arange(14, dtype=np.float64).reshape(7, 2) / 3.0,
                         en=np.arange(14, dtype=np.float64).reshape(7, 2) * 1.5, own=np.array([True, False, True]))
    f.attrs["subtype"] = np.array(["road", "rail", "road"])
    f.attrs["class"] = np.array(["primary", "", "service"])
    tags = np.full(3, None, object)
    tags[1] = "1435"
    f.attrs["osm_gauge"] = tags
    parts = np.empty(3, object)
    parts[0] = [(0.0, 0.5, "w1", {"lanes": "2"}), (0.5, 1.0, None, {})]
    parts[2] = []
    f.attrs["osm_lane_parts"] = parts
    f.rules["width"] = mapgeom.RuleTable(np.array([0], np.int32), np.array([7.5]), np.array([0.0], np.float32),
                                         np.array([1.0], np.float32))
    f.rules["surface"] = mapgeom.RuleTable(np.array([2], np.int32), np.array(["gravel"]), np.array([0.25], np.float32),
                                           np.array([0.75], np.float32))
    f.__dict__["_rails_built"] = "2026-11-01"
    f.__dict__["_rail_nodes"] = [(5.0, 47.0, {"railway": "level_crossing"})]
    f.__dict__["_road_revision"] = 1
    f.__dict__["_xy_in"] = {"memo": 1}          # (a memo: made again, not kept)
    return f


class RoadLayerCacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.d = os.path.join(self.dir, "5.dec")

    def test_comes_back_as_it_was(self):
        f = layer()
        self.assertGreater(rc.store(self.d, "k1", f, [("14/1/2", "10 20")]), 0)
        g = rc.load(self.d, "k1", lambda cell: "10 20")
        self.assertIsNotNone(g)
        for name in ("ids", "kind", "part_off", "ring_off", "coord_off", "lonlat", "en", "own"):
            a, b = getattr(f, name), getattr(g, name)
            self.assertEqual((a.dtype, a.shape, a.tobytes()), (b.dtype, b.shape, b.tobytes()), name)
            self.assertTrue(b.flags.writeable, name)
        self.assertIsNone(g.clipped)
        self.assertEqual(list(f.attrs), list(g.attrs))
        self.assertEqual(list(f.rules), list(g.rules))
        self.assertEqual(g.attrs["osm_gauge"].dtype, object)
        self.assertEqual(g.attrs["osm_gauge"].tolist(), [None, "1435", None])
        self.assertEqual(g.attrs["osm_lane_parts"].tolist(), f.attrs["osm_lane_parts"].tolist())
        self.assertIs(type(g.attrs["osm_lane_parts"][0][0]), tuple)
        for n in f.rules:
            for col in ("feature", "value", "start", "end"):
                a, b = getattr(f.rules[n], col), getattr(g.rules[n], col)
                self.assertEqual((a.dtype, a.tobytes()), (b.dtype, b.tobytes()), (n, col))
        self.assertEqual(g.__dict__["_rail_nodes"], f.__dict__["_rail_nodes"])
        self.assertIs(type(g.__dict__["_rail_nodes"][0]), tuple)
        self.assertEqual(g.__dict__["_rails_built"], "2026-11-01")
        self.assertEqual(g.__dict__["_road_revision"], 1)
        self.assertNotIn("_xy_in", g.__dict__)

    def test_only_under_its_key_and_its_neighbours_stamps(self):
        rc.store(self.d, "k1", layer(), [("14/1/2", "10 20")])
        self.assertIsNone(rc.load(self.d, "k2", lambda cell: "10 20"))
        self.assertIsNone(rc.load(self.d, "k1", lambda cell: "11 20"))        # (the neighbour's file changed)
        self.assertIsNone(rc.load(self.d, "k1", lambda cell: None))           # (... or is gone)
        self.assertIsNotNone(rc.load(self.d, "k1", lambda cell: "10 20"))
        self.assertTrue(rc.has(self.d, "k1"))
        self.assertFalse(rc.has(self.d, "k2"))
        self.assertFalse(rc.has(self.d + "x", "k1"))

    def test_a_damaged_file_is_no_layer(self):
        rc.store(self.d, "k1", layer(), [])
        with open(os.path.join(self.d, rc.BLOB), "r+b") as fh:
            fh.truncate(10)
        self.assertIsNone(rc.load(self.d, "k1", lambda cell: None))

    def test_what_cannot_be_given_back_is_not_kept(self):
        f = layer()
        f.__dict__["_something_new"] = 1                  # (state this file does not know)
        self.assertEqual(rc.store(self.d, "k1", f, []), 0)
        f = layer()
        f.attrs["osm_gauge"][0] = object()                # (a value of a kind it cannot write)
        self.assertEqual(rc.store(self.d, "k1", f, []), 0)
        self.assertFalse(os.path.exists(self.d))

    def test_numpy_values_and_odd_keys_keep_their_types(self):
        f = layer()
        joints = {(np.float64(5.25), np.float64(47.5)): [(np.array([0.6, 0.8]), np.float64(1.75), True, "motorway", 1)],
                  ("a", 1): []}
        f.__dict__["_lane_section_joints"] = joints
        f.attrs["osm_gauge"][0] = np.int64(3)
        self.assertGreater(rc.store(self.d, "k1", f, []), 0)
        g = rc.load(self.d, "k1", lambda cell: None)
        got = g.__dict__["_lane_section_joints"]
        self.assertEqual(list(got), list(joints))
        k = next(iter(got))
        self.assertIs(type(k[0]), np.float64)
        row = got[k][0]
        self.assertEqual((row[0].dtype, row[0].tobytes()), (np.dtype(np.float64), np.array([0.6, 0.8]).tobytes()))
        self.assertIs(type(row[1]), np.float64)
        self.assertEqual(row[1:], (np.float64(1.75), True, "motorway", 1))
        self.assertIs(type(g.attrs["osm_gauge"][0]), np.int64)
        self.assertEqual(got[("a", 1)], [])


if __name__ == "__main__":
    unittest.main()
