"""roads.sub_polyline / sub_polylines (earthfast 504): the part of a lon/lat polyline between two fractions of its
length (lon scaled by the cosine of its mean latitude), one ring or many rings x many ranges in one call."""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import roads  # noqa: E402


def _same(a, b):
    a, b = np.asarray(a), np.asarray(b)
    return a.dtype == b.dtype and a.shape == b.shape and a.tobytes() == b.tobytes()


class SubPolyline(unittest.TestCase):
    def test_cut_between_and_at_vertices(self):
        ring = np.array([[0.0, 0.0], [0.0, 1.0], [0.0, 2.0], [0.0, 4.0]])
        got = roads.sub_polyline(ring, 0.25, 0.75)
        self.assertEqual(got.tolist(), [[0.0, 1.0], [0.0, 2.0], [0.0, 3.0]])
        # (a vertex at a range's end is that end, not an inner point too)
        self.assertEqual(roads.sub_polyline(ring, 0.5, 1.0).tolist(), [[0.0, 2.0], [0.0, 4.0]])
        self.assertEqual(roads.sub_polyline(ring, 0.0, 1.0).tolist(), ring.tolist())

    def test_lon_scaled_by_cos_of_mean_lat(self):
        # (at 60 degrees a degree of lon is half one of lat: the half way point lies at the corner)
        ring = np.array([[0.0, 59.0], [2.0, 59.0], [2.0, 61.0]])
        c = math.cos(math.radians(np.mean(ring[:, 1])))
        f = 2 * c / (2 * c + 2)
        got = roads.sub_polyline(ring, f, 1.0)
        self.assertAlmostEqual(got[0, 0], 2.0, places=9)
        self.assertAlmostEqual(got[0, 1], 59.0, places=9)

    def test_past_the_ends_and_zero_length(self):
        ring = np.array([[10.0, 50.0], [10.001, 50.0], [10.002, 50.0]])
        # (past the ends: np.interp's first / last point, then every vertex between)
        got = roads.sub_polyline(ring, -0.5, 1.5)
        self.assertEqual(got.tolist(), [ring[0].tolist()] + ring.tolist() + [ring[-1].tolist()])
        got = roads.sub_polyline(ring, 0.25, 0.25)
        self.assertEqual(got.shape, (2, 2))
        self.assertTrue(_same(got[0], got[1]))

    def test_no_length_short_and_nan(self):
        ring = np.array([[1.0, 2.0], [1.0, 2.0], [1.0, 2.0]])
        self.assertEqual(roads.sub_polyline(ring, 0.2, 0.6).tolist(), [[1.0, 2.0]])
        self.assertEqual(roads.sub_polyline(ring[:1], 0.2, 0.6).tolist(), [[1.0, 2.0]])
        self.assertEqual(roads.sub_polyline(ring[:0], 0.2, 0.6).shape, (0, 2))
        nan = np.array([[0.0, 0.0], [np.nan, 1.0], [0.0, 2.0]])
        self.assertTrue(np.isnan(roads.sub_polyline(nan, 0.1, 0.9)).all())

    def test_columns_views_and_float32(self):
        ring = np.array([[0.0, 0.0, 5.0], [0.0, 1.0, 7.0], [0.0, 2.0, 9.0]])
        got = roads.sub_polyline(ring, 0.25, 0.75)
        self.assertEqual(got.tolist(), [[0.0, 0.5, 6.0], [0.0, 1.0, 7.0], [0.0, 1.5, 8.0]])
        wide = np.zeros((6, 5))
        wide[::2, 1:4] = ring
        self.assertTrue(_same(roads.sub_polyline(wide[::2, 1:4], 0.25, 0.75), got))
        f32 = ring[:, :2].astype(np.float32)
        self.assertTrue(_same(roads.sub_polyline(f32, 0.25, 0.75), roads.sub_polyline(ring[:, :2], 0.25, 0.75)))

    def test_many_rings_and_ranges(self):
        rng = np.random.default_rng(3)
        rings = [np.array([16.37, 48.21]) + np.cumsum(rng.normal(0, 1e-4, (k, 2)), 0) for k in (2, 5, 9, 1)]
        rings.append(np.array([[1.0, 2.0], [1.0, 2.0]]))
        pieces = [(0.1, 0.4), (0.0, 1.0), (0.3, 0.3), (-0.2, 1.7), (0.5, 0.99)]
        got = roads.sub_polylines(rings, pieces)
        rows = roads.sub_polylines(rings, pieces, rows_only=True)
        cut = roads.sub_polylines(rings, pieces, whole=False)
        self.assertEqual(len(got), len(rings) * len(pieces))
        for j, r in enumerate(rings):
            for k, (a, b) in enumerate(pieces):
                g, n = got[j * len(pieces) + k], rows[j * len(pieces) + k]
                one = roads.sub_polyline(r, a, b)
                self.assertTrue(_same(cut[j * len(pieces) + k], one))
                if (a, b) == (0.0, 1.0):
                    self.assertIs(g, r)                    # (the ring itself, uncut)
                    self.assertEqual(n, len(r))
                else:
                    self.assertTrue(_same(g, one))
                    self.assertEqual(n, len(one))
        self.assertEqual(roads.sub_polylines([], pieces), [])
        self.assertEqual(roads.sub_polylines(rings, []), [])

    def test_shortcut_keeps_a_list_ring_as_given(self):
        lst = [[0.0, 0.0], [1e-3, 2e-3], [3e-3, 2e-3]]
        got = roads.sub_polylines([lst], [(0.0, 1.0), (0.2, 0.7)])
        self.assertIs(got[0], lst)
        self.assertTrue(_same(got[1], roads.sub_polyline(np.array(lst), 0.2, 0.7)))


if __name__ == "__main__":
    unittest.main()
