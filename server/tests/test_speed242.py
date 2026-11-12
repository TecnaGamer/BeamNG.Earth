"""The world-gen speed kernels of earthfast 242 by their behaviour: hash01 (buildings._hash01), chain_graph
(roads._chain_graph), geo_world_to_latlon (geotiles.WorldFrame), skeleton_roof_uv / Mesh.tris (buildings._skel_roof),
GeoBox.owns of one point, the skyline's small-building filter (buildings._small_rows), railroadseam.couple's lowered
roads and roads.is_placed's row filter."""
import hashlib
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import buildings  # noqa: E402
import geotiles  # noqa: E402
import railroadseam  # noqa: E402
import roads  # noqa: E402


class Hash01Test(unittest.TestCase):
    def test_blake2b_of_the_tuples_repr(self):
        # (the definition: BLAKE2b-64 of repr(vals), little-endian, / 2^64; quotes, escapes, non-ASCII, floats Python's
        # own repr picks among equally short digits, big ints, None and bools)
        for vals in [("08b2a100d2c4cfff0200a4a4c8d16a3d", "plan"), (0.25, "look"), ("it's",), ('say "hi"', 3),
                     ("tab\there\\", None, True), ("café", "東京"), (2208031614071659.2, "x"), (1 << 70, -0.0),
                     (np.float64(2.5), np.str_("np"))]:
            want = int.from_bytes(hashlib.blake2b(repr(vals).encode(), digest_size=8).digest(), "little") / 2.0 ** 64
            self.assertEqual(buildings._hash01(*vals), want, vals)

    def test_range_and_spread(self):
        got = [buildings._hash01(f"id{i}", "look") for i in range(2000)]
        self.assertTrue(all(0.0 <= v < 1.0 for v in got))
        self.assertGreater(len(set(got)), 1990)
        self.assertAlmostEqual(float(np.mean(got)), 0.5, delta=0.03)


class ChainGraphTest(unittest.TestCase):
    def test_ends_within_half_a_metre_are_one_node(self):
        a = np.array([[0.0, 0.0], [10.0, 0.0]])
        b = np.array([[10.3, 0.0], [20.0, 0.0], [20.0, 5.0]])      # (0.3 m from a's end: joined)
        c = np.array([[20.0, 5.6], [30.0, 5.6]])                   # (0.6 m from b's end: a node of its own)
        nodes, edges, adj = roads._chain_graph([a, b, c])
        self.assertEqual([(e[0], e[1]) for e in edges], [(0, 1), (1, 2), (3, 4)])
        self.assertEqual(len(nodes), 5)
        np.testing.assert_array_equal(nodes[1], [10.0, 0.0])       # (named by the first point seen)
        np.testing.assert_allclose(edges[1][2], [0.0, 9.7, 14.7])
        self.assertEqual(sorted(j for j, _s in adj[1]), [0, 2])
        self.assertAlmostEqual(float(dict(adj[2])[1]), 14.7)

    def test_a_point_joins_the_nearest_node(self):
        lines = [np.array([[0.0, 0.0], [5.0, 0.0]]), np.array([[0.8, 0.0], [5.0, 3.0]]),
                 np.array([[0.45, 0.0], [9.0, 9.0]])]                # (0.45 from node 0, 0.35 from node 2's point)
        _nodes, edges, _adj = roads._chain_graph(lines)
        self.assertEqual(edges[2][0], edges[1][0])

    def test_one_point_line(self):
        nodes, edges, _adj = roads._chain_graph([np.array([[1.0, 2.0]])])
        self.assertEqual((edges[0][0], edges[0][1]), (0, 0))
        np.testing.assert_array_equal(edges[0][2], [0.0])
        self.assertEqual(len(nodes), 1)


class WorldFrameTest(unittest.TestCase):
    def test_lat_lon_as_the_full_inverse(self):
        f = geotiles.WorldFrame(39.2834, -76.5934)
        e = np.linspace(-15000.0, 15000.0, 41)
        n = np.linspace(9000.0, -12000.0, 41)
        la, lo, h = f.enu_to_geodetic(e, n, 0.0)
        p = np.zeros((len(e), 3))
        p[:, 0], p[:, 1] = e, n
        la2, lo2, _ = geotiles.world_to_geodetic(f, p)
        np.testing.assert_array_equal(la, la2)
        np.testing.assert_array_equal(lo, lo2)
        np.testing.assert_array_equal(h, 0.0)
        a, b, c = f.enu_to_geodetic(120.0, -40.0, 0.0)          # (a scalar in, scalars out: the array's numbers)
        la3, lo3, _ = f.enu_to_geodetic(np.array([120.0]), np.array([-40.0]), 0.0)
        self.assertEqual((float(a), float(b), float(c)), (float(la3[0]), float(lo3[0]), 0.0))
        self.assertEqual(np.ndim(a), 0)


class OwnsTest(unittest.TestCase):
    def test_one_point_as_the_array(self):
        t = geotiles.GeoBox(37500, 50100, 17)
        x0, y0, x1, y1 = t.box
        rng = np.random.default_rng(3)
        xs = np.r_[rng.uniform(x0 - 1, x1 + 1, 300), x0, x1, x0 + 0.001, x1 - 0.001, np.nan]
        ys = np.r_[rng.uniform(y0 - 1, y1 + 1, 300), y0, y1, y1 - 0.001, y0 + 0.001, 0.0]
        arr = t.owns(xs, ys)
        self.assertEqual([t.owns(float(x), float(y)) for x, y in zip(xs, ys)], arr.tolist())
        self.assertIsInstance(t.owns(float(xs[0]), float(ys[0])), bool)


class SkelRoofTest(unittest.TestCase):
    def test_blocks_as_triangles(self):
        m1, m2 = buildings.Mesh(), buildings.Mesh()
        mat = (3, (10, 20, 30, 255))
        P = np.arange(18, dtype=np.float64).reshape(6, 3) * 0.5
        UV = np.arange(12, dtype=np.float64).reshape(6, 2) * 0.25
        m1.tri((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0), (1, 0), (0, 1), mat)
        m2.tri((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0), (1, 0), (0, 1), mat)
        for k in range(2):
            m1.tri(*(tuple(P[3 * k + i]) for i in range(3)), *(tuple(UV[3 * k + i]) for i in range(3)), mat)
        m2.tris(P, UV, mat)
        for a, b in zip(m1.arrays(), m2.arrays()):
            np.testing.assert_array_equal(a, b)

    def test_uv_along_the_slope(self):
        import fast
        tri = np.array([[0.0, 0.0, 3.0], [4.0, 0.0, 3.0], [0.0, 2.0, 4.0]])
        uv = fast._ef.skeleton_roof_uv(tri, np.array([[0.0, 0.5]]), 2.0)
        f = (1.0 + 0.25) ** 0.5
        np.testing.assert_allclose(uv, [[0.0, 0.0], [-2.0, 0.0], [0.0, f]])      # (u along the level line, v up it)
        flat = fast._ef.skeleton_roof_uv(tri, np.array([[0.0, 0.0]]), 2.0)
        np.testing.assert_allclose(flat, [[0.0, 0.0], [2.0, 0.0], [0.0, 1.0]])


class SmallRowsTest(unittest.TestCase):
    def test_only_the_surely_small(self):
        import mapgeom
        import geodesy
        frame = geodesy.EnuFrame(45.0, 7.0)
        rings, heights = [], []
        for k, (side, h) in enumerate([(3.0, 4.0), (3.0, 40.0), (10.0, 4.0), (4.0, np.nan), (5.9, np.nan)]):
            x0 = 100.0 * k
            r = np.array([[x0, 0.0], [x0 + side, 0.0], [x0 + side, side], [x0, side], [x0, 0.0]])
            la, lo, _ = frame.enu_to_geodetic(r[:, 0], r[:, 1], 0.0)
            rings.append(np.c_[lo, la])
            heights.append(h)
        n = len(rings)
        f = mapgeom.Features("buildings", np.array([f"b{i}" for i in range(n)]), np.full(n, 3, np.uint8),
                             np.arange(n + 1, dtype=np.int64), np.arange(n + 1, dtype=np.int64),
                             np.r_[0, np.cumsum([len(r) for r in rings])].astype(np.int64), np.vstack(rings))
        a = {"height": np.array(heights), "num_floors": np.array([np.nan, np.nan, np.nan, 1.0, np.nan])}
        got = buildings._small_rows(f, frame, a, (30.0, 40.0)).tolist()
        # 9 m2 and 4 m: small; 40 m tall: kept; 100 m2: kept; 16 m2 one floor (3.2 m): small; 35 m2: not surely small
        # (SMALL_MARGIN: under half the 40 m2), the loop's own test decides it
        self.assertEqual(got, [True, False, False, True, False])

    def test_not_plain_floats_none(self):
        import mapgeom
        f = mapgeom.Features("buildings", np.array(["a"]), np.full(1, 3, np.uint8), np.arange(2, dtype=np.int64),
                             np.arange(2, dtype=np.int64), np.array([0, 5], np.int64),
                             np.array([[7.0, 45.0], [7.00001, 45.0], [7.00001, 45.00001], [7.0, 45.00001], [7.0, 45.0]]))
        import geodesy
        got = buildings._small_rows(f, geodesy.EnuFrame(45.0, 7.0), {"height": np.array([None], dtype=object)},
                                    (30.0, 40.0))
        self.assertEqual(got.tolist(), [False])


class SeamLoweredTest(unittest.TestCase):
    def test_lowered_roads_as_lowered_copies(self):
        xy = np.c_[np.linspace(0.0, 200.0, 51), np.full(51, 11.2)]
        zb = np.full(51, 97.9)
        road = np.c_[np.linspace(0.0, 200.0, 11), np.zeros(11)]
        roads_ = [(road, np.full(11, 100.0), 8.6), (np.zeros((1, 2)), np.zeros(1), 1.0)]
        lowered = railroadseam.couple(xy, zb, 3.0, roads_, (0,), 0.55)
        copy = railroadseam.couple(xy, zb, 3.0, [(road, np.full(11, 100.0) - 0.55, 8.6)])
        np.testing.assert_array_equal(lowered, copy)
        np.testing.assert_array_equal(railroadseam.couple(xy, zb, 3.0, roads_),
                                      railroadseam.couple(xy, zb, 3.0, roads_[:1]))


class IsPlacedTest(unittest.TestCase):
    def test_only_rows_with_a_placement(self):
        class F:
            pass
        f = F()
        f.__dict__["_placement_rows"] = frozenset({1})
        spec = roads.Spec(*([None] * len(roads.Spec._fields)))
        called = []
        orig = roads.lane_tag_parts
        try:
            roads.lane_tag_parts = lambda feats, i: called.append(i) or []
            self.assertFalse(roads.is_placed(f, 0, spec))
            self.assertFalse(roads.is_placed(f, 1, spec))
        finally:
            roads.lane_tag_parts = orig
        self.assertEqual(called, [1])


if __name__ == "__main__":
    unittest.main()
