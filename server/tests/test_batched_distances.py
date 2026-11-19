"""Distances taken in one call for many items (2026-11-19): roads._cornered (the road pairs meeting at a curb return),
roadmesh._end_rows / _ground_runs (build's ground ribbon ends against the deck ends), each the numbers of a call per
item."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dnp  # noqa: E402
import roadmesh  # noqa: E402
import roads  # noqa: E402


def _same(a, b):
    a, b = np.asarray(a), np.asarray(b)
    return a.dtype == b.dtype and a.shape == b.shape and a.tobytes() == b.tobytes()


def _road(fid, pts):
    p = np.asarray(pts, np.float64)
    return (fid, None, np.c_[p, np.zeros(len(p))], 0.0)


class Cornered(unittest.TestCase):
    def test_within_a_metre_of_another_road(self):
        rs = [_road("a", [[0.0, 0.0], [10.0, 0.0]]), _road("b", [[0.6, 0.6], [0.6, 10.0]]),
              _road("c", [[1.0, 0.0], [1.0, -9.0]]), _road("d", [[0.0, 0.999], [-5.0, 5.0]])]
        cuts = {("a", 1): [(np.array([0.0, 0.0]), None, 1.0)]}
        # (its own road is never a pair; 1 m away exactly is not within; d's first point 0.999 m away is)
        self.assertEqual(roads._cornered(cuts, rs), {("a", "b"), ("a", "d")})

    def test_every_cut_and_road(self):
        rs = [_road("a", [[0.0, 0.0], [5.0, 0.0]]), _road("b", [[100.0, 0.0], [100.0, 5.0]]),
              _road("c", [[0.0, 0.5], [0.0, 5.0]])]
        cuts = {("b", -1): [(np.array([100.0, 0.2]), None, 1.0), (np.array([0.0, 0.0]), None, 1.0)],
                ("a", 1): [(np.array([100.0, 0.0]), None, 1.0)]}
        self.assertEqual(roads._cornered(cuts, rs), {("b", "a"), ("b", "c"), ("a", "b")})

    def test_not_finite_as_a_call_per_road(self):
        # (a NaN point: that road's nearest distance is NaN, never within)
        rs = [_road("a", [[0.0, 0.0], [5.0, 0.0]]), _road("b", [[0.5, 0.0], [np.nan, 1.0]]),
              _road("c", [[0.0, 0.5], [0.0, 3.0]])]
        cuts = {("a", 1): [(np.array([0.0, 0.0]), None, 1.0)]}
        self.assertEqual(roads._cornered(cuts, rs), {("a", "c")})
        self.assertEqual(roads._cornered({}, rs), set())


class GroundEnds(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(5)
        self.rs = []
        for k in (2, 5, 3):
            mid = np.cumsum(rng.normal(0, 4, (k, 2)), 0)
            half = rng.normal(0, 3, (k, 2))
            self.rs.append((mid + half, mid - half, None, None, "base", None))
        self.ab = rng.normal(0, 10, (4, 2))

    def test_rows_are_the_ribbons_own(self):
        rows = list(roadmesh._end_rows(self.rs, self.ab))
        runs = list(roadmesh._ground_runs(self.rs, self.ab))
        ab = self.ab
        self.assertEqual(len(rows), len(self.rs))
        for g, (left, right, *_r) in enumerate(self.rs):
            d0, d1, w0, w1 = zip(*rows)
            m0, m1 = (left[0] + right[0]) * 0.5, (left[-1] + right[-1]) * 0.5
            self.assertTrue(_same(d0[g], dnp.hypot(ab[:, 0] - m0[0], ab[:, 1] - m0[1])))
            self.assertTrue(_same(d1[g], dnp.hypot(ab[:, 0] - m1[0], ab[:, 1] - m1[1])))
            self.assertEqual(w0[g], float(dnp.hypot(*(left[0] - right[0]))))
            self.assertEqual(w1[g], float(dnp.hypot(*(left[-1] - right[-1]))))
            mid = (left + right) * 0.5
            s, a, b = runs[g]
            self.assertTrue(_same(s, np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(mid, axis=0).T))]))
            self.assertEqual(a, dnp.hypot(ab[:, 0] - mid[0, 0], ab[:, 1] - mid[0, 1]).min())
            self.assertEqual(b, dnp.hypot(ab[:, 0] - mid[-1, 0], ab[:, 1] - mid[-1, 1]).min())

    def test_chunked(self):
        # (a bounded buffer: a ribbon or a few a call, the same rows)
        import unittest.mock as um
        whole = [tuple(np.asarray(x).tobytes() for x in r) for r in roadmesh._end_rows(self.rs, self.ab)]
        runs = list(roadmesh._ground_runs(self.rs, self.ab))
        with um.patch.object(roadmesh, "_TO_POINTS_MAX", 5):
            self.assertEqual([tuple(np.asarray(x).tobytes() for x in r) for r in roadmesh._end_rows(self.rs, self.ab)],
                             whole)
            got = list(roadmesh._ground_runs(self.rs, self.ab))
        self.assertTrue(all(_same(a[0], b[0]) and a[1] == b[1] and a[2] == b[2] for a, b in zip(got, runs)))

    def test_none_unless_float64_pairs(self):
        rs = [(self.rs[0][0].astype(np.float32),) + self.rs[0][1:]] + self.rs[1:]
        self.assertIsNone(roadmesh._end_rows(rs, self.ab))
        self.assertIsNone(roadmesh._ground_runs(rs, self.ab))
        self.assertIsNone(roadmesh._end_rows([], self.ab))


if __name__ == "__main__":
    unittest.main()
