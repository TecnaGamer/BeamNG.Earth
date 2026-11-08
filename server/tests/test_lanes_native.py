"""The Rust ports of the lane evidence and lane flow (earthfast generated_lanes / lane_flow_cells) against the original
Python (lanes_reference.py), output for output (offline)."""
import copy
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import lanes_reference as ref
import mapcells
import roadlanes
import roads
from test_roads import features, rules


def fresh(f):
    """A copy of f without memos (both sides compute from scratch)."""
    g = copy.copy(f)
    g.__dict__ = {k: v for k, v in f.__dict__.items() if not k.startswith("_")}
    g.attrs = dict(f.attrs)
    return g


def same(a, b):
    return len(a) == len(b) and all(type(x) is type(y) and x == y for x, y in zip(a, b))


def obj(values):
    a = np.empty(len(values), object)
    a[:] = values
    return a


class GeneratedCountsTest(unittest.TestCase):
    def check(self, f):
        old, new = ref.generated_counts(fresh(f)), roadlanes.generated_counts(fresh(f))
        self.assertTrue(same(old, new), (old, new))
        return new

    def test_tag_values_parse_as_python_does(self):
        vals = ["3", "3;2", " 4 ", None, 1.5, "nan", 13, "2_0", "inf", "0", "١", True, np.float32(2.0), "1e0"]
        n = len(vals)
        f = features([[(10 * i, 0), (10 * i + 8, 0)] for i in range(n)], ["primary"] * n)
        f.attrs["osm_lane_total"] = obj(vals)
        self.check(f)
        f = features([[(10 * i, 0), (10 * i + 8, 0)] for i in range(n)], ["primary"] * n)
        f.attrs["osm_lanes_forward"] = obj(vals)
        f.attrs["osm_lanes_backward"] = obj(vals[::-1])
        f.attrs["osm_lanes_both_ways"] = obj(vals[3:] + vals[:3])
        f.attrs["lane_count"] = np.array([np.nan, 2.0, 2.5, 0.0, 12.0, 13.0, np.inf] * 2)
        self.check(f)
        f.rules["oneway"] = rules([(i, (-1) ** i) for i in range(0, n, 3)] + [(1, 0.0)], "float")
        self.check(f)

    def test_highway_gap_between_agreeing_ends(self):
        lines = [[(0, 0), (100, 0)], [(100, 0), (200, 0.3)], [(200, 0.3), (300, 0)], [(300, 0), (400, 0)]]
        for counts, expect in (((3, None, None, 3), [3, 3, 3, 3]), ((3, None, None, 2), [3, None, None, 2]),
                               ((None, None, None, 3), [None] * 3 + [3])):
            for direction in (1, -1, 0):
                with self.subTest(counts=counts, direction=direction):
                    f = features(lines, ["motorway"] * 4)
                    f.attrs["lane_count"] = np.array([np.nan if c is None else float(c) for c in counts])
                    if direction:
                        f.rules["oneway"] = rules([(i, direction) for i in range(4)], "float")
                    got = self.check(f)
                    if direction:
                        self.assertEqual(got, expect)
                    # connectors that disagree at a joint keep the gap open; agreeing ones close it
                    f.rules["connectors"] = rules([(0, "a", 1.0, 1.0), (1, "b", 0.0, 0.0), (1, "c", 1.0, 1.0),
                                                   (2, "c", 0.0, 0.0), (2, "d", 1.0, 1.0), (3, "d", 0.0, 0.0)])
                    self.check(f)
                    f.rules["connectors"] = rules([(0, "a", 1.0, 1.0), (1, "a", 0.0, 0.0)])
                    self.check(f)

    def test_ramps_rings_and_other_classes_stay_out(self):
        lines = [[(0, 0), (100, 0)], [(100, 0), (200, 0)], [(200, 0), (300, 0)]]
        for cls, sub, ring in (("trunk", "link", None), ("motorway", "", "roundabout"), ("primary", "", None),
                               ("trunk", "", "circular")):
            f = features(lines, ["trunk", cls, "trunk"])
            f.attrs["lane_count"] = np.array([2.0, np.nan, 2.0])
            f.attrs["subclass"] = np.array(["", sub, ""])
            f.attrs["osm_junction"] = obj([None, ring, None])
            f.rules["oneway"] = rules([(i, 1) for i in range(3)], "float")
            self.check(f)


class LaneFlowTest(unittest.TestCase):
    def merge(self):
        """A 2-lane one-way primary and a ramp joining it at (100, 0); the road going on from there has no count.
        The road going on is also in a neighbouring cell's layer (the same id) with one more road."""
        a = features([[(0, 0), (100, 0)], [(50, -40), (100, 0)], [(100, 0), (200, 0)]], ["primary"] * 3,
                     ids=["r0", "r1", "r2"])
        a.attrs["lane_count"] = np.array([2.0, np.nan, np.nan])
        a.rules["oneway"] = rules([(0, 1), (1, 1), (2, 1)], "float")
        a.rules["subclass"] = rules([(1, "link")])
        b = features([[(100, 0), (200, 0)], [(200, 0), (300, 0)]], ["primary"] * 2, ids=["r2", "r3"])
        b.attrs["lane_count"] = np.array([np.nan, np.nan])
        b.rules["oneway"] = rules([(0, 1), (1, 1)], "float")
        return a, b

    def old(self, layers):
        rows = []
        for f in layers:
            rows.append((ref.lane_flow_rows(fresh(f), ref.generated_counts(fresh(f))),
                         np.asarray([str(x) for x in f.ids.tolist()], dtype=str), f.lonlat))
        return ref.lane_flow_local(len(layers[0]), rows)

    def test_merge_adds_the_ramps_lane(self):
        a, b = self.merge()
        new = roads._lane_flow_local(a)
        self.assertEqual(new, [None, None, 3])
        self.assertEqual(self.old([a]), new)
        a2, b2 = fresh(a), fresh(b)
        a2.__dict__["_bnge_neighbours"] = lambda: [b2]
        self.assertEqual(roads._lane_flow_local(a2), self.old([a, b]))
        b3, a3 = fresh(b), fresh(a)
        b3.__dict__["_bnge_neighbours"] = lambda: [a3]
        self.assertEqual(roads._lane_flow_local(b3), self.old([b, a]))

    def test_rows_match(self):
        a, _b = self.merge()
        _ids, starts, ends, at = ref.lane_flow_rows(fresh(a), ref.generated_counts(fresh(a)))
        pts, npts, at2 = roads._lane_flow_rows(fresh(a))
        np.testing.assert_array_equal(at, at2)
        np.testing.assert_array_equal(npts, ends - starts)
        np.testing.assert_array_equal(pts[:, :2], a.lonlat[starts])
        np.testing.assert_array_equal(pts[:, 2:], a.lonlat[ends - 1])


class HomeCellsTest(unittest.TestCase):
    def test_cells_of_all_features_match_cell_of(self):
        key = mapcells.cell_of(40.75, -73.97)
        lon0, lat0, lon1, lat1 = key.bounds()
        pts = [(lon0, lat0), (lon0, lat1), (lon1, lat1), ((lon0 + lon1) / 2, (lat0 + lat1) / 2),
               (np.nextafter(lon0, -180), lat0), (lon1, np.nextafter(lat1, 90)), (lon0 + 1e-12, lat1 - 1e-12)]
        f = features([[(0, 0), (1, 0)]] * len(pts), ["primary"] * len(pts))
        f.lonlat = np.array([p for p in pts for _ in range(2)], np.float64)
        self.assertEqual(roads._home_cells(fresh(f)), [ref.home_cell(f, i) for i in range(len(f))])


if __name__ == "__main__":
    unittest.main()
