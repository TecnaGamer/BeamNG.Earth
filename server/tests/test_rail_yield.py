"""A rail bed's grading plan gives way to the roads it crosses at grade (grading.rail_yield_pieces): its bed is flushed
railways.RAIL_TOP_M under the road's surface there, and its plan pulled the road's own ground down toward it, a dip of
0.3-0.7 m across the road at every level crossing (Fells Point's street-running tracks, bumpscan 2026-11-12)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import railways  # noqa: E402


def track(x0=-100.0, x1=100.0, z=9.45):
    xy = np.c_[np.linspace(x0, x1, 51), np.zeros(51)]
    return xy, np.full(51, z)


def full_scan(xy, z, pts, starts, reach, dz):
    """earthfast.near_crossers as every point against every segment of every road (the kernel before its grid)."""
    import dnp
    out = np.zeros(len(xy), bool)
    with np.errstate(all="ignore"):
        for c in range(len(starts) - 1):
            d = pts[starts[c]:starts[c + 1]]
            if len(d) < 2:
                continue
            a, b = d[:-1, :2], d[1:, :2] - d[:-1, :2]
            l2 = np.maximum(b[:, 0] * b[:, 0] + b[:, 1] * b[:, 1], 1e-12)
            for i, (px, py) in enumerate(xy):
                t = np.clip(((px - a[:, 0]) * b[:, 0] + (py - a[:, 1]) * b[:, 1]) / l2, 0.0, 1.0)
                dd = dnp.hypot(px - (a[:, 0] + t * b[:, 0]), py - (a[:, 1] + t * b[:, 1]))
                best, bj = np.inf, None
                for n, v in enumerate(dd.tolist()):
                    if v < best or bj is None:
                        best, bj = v, n
                if best < reach[c] and abs(z[i] - d[bj, 2]) < dz:
                    out[i] = True
    return out


class NearCrossersTest(unittest.TestCase):
    """The kernel's grid of near segments answers as the full scan, ties, NaN and overflowing distances included."""

    def ask(self, xy, z, pts, starts, reach, dz):
        import fast
        got = np.asarray(fast._ef.near_crossers(np.ascontiguousarray(xy, np.float64), np.asarray(z, np.float64),
                                                np.ascontiguousarray(pts, np.float64), np.asarray(starts, np.int64),
                                                np.asarray(reach, np.float64), float(dz)), bool)
        np.testing.assert_array_equal(got, full_scan(xy, z, pts, starts, reach, dz))
        return got

    def test_overflowing_first_segment(self):
        """A first segment whose distance overflows to NaN holds the nearest for good (Codex's review, 2026-11-15)."""
        pts = np.array([[1e200, 1e200, 0], [2e200, 2e200, 0], [100, 100, 0], [1, 0, 0], [0, 0, 0]], np.float64)
        got = self.ask(np.zeros((1, 2)), np.zeros(1), pts, [0, 5], [2.0], 1.0)
        self.assertFalse(got[0])

    def test_long_segments_over_sparse_points(self):
        """Two points 14 km apart and a road of 1000 segments from one to the other and back (Codex's review,
        2026-11-15: each segment's box over the whole 513 x 513 grid, 263 million entries and 2 GB): answered without
        the grid, as the full scan, at once."""
        import time
        xy = np.array([[0.0, 0.0], [10000.0, 10000.0]])
        line = np.array([[0.0, 0.0] if n % 2 == 0 else [10000.0, 10000.0] for n in range(1001)])
        pts = np.c_[line, np.zeros(len(line))]
        t0 = time.perf_counter()
        got = self.ask(xy, np.zeros(2), pts, [0, len(pts)], [5.0], 1.0)
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertTrue(got.all())
        # (and many long roads, each under the budget, over it together)
        roads_ = [np.c_[np.array([[0.0, 0.0], [10000.0, 10000.0], [0.0, 10000.0]]) + c, np.zeros(3)]
                  for c in range(200)]
        self.ask(xy, np.zeros(2), np.concatenate(roads_), np.arange(0, 601, 3), np.full(200, 5.0), 1.0)

    def test_random(self):
        rng = np.random.default_rng(7)
        for _ in range(300):
            k = int(rng.integers(1, 40))
            big = float(rng.choice([1.0, 9.9e6, 1.01e7, 1e154, 1e200, 1.7e308]))
            xy = np.round(rng.normal(size=(k, 2)) * 30.0)
            if rng.random() < 0.2:
                xy[int(rng.integers(0, k))] *= big
            if rng.random() < 0.05:
                xy[int(rng.integers(0, k)), 0] = np.nan
            lines, reach = [], []
            for _c in range(int(rng.integers(1, 5))):
                n = int(rng.integers(0, 8))
                ln = np.round(rng.normal(size=(n, 2)) * 30.0)
                if n and rng.random() < 0.3:
                    ln[int(rng.integers(0, n))] = rng.choice([-1.0, 1.0], size=2) * big
                if n > 2 and rng.random() < 0.2:
                    ln[1] = ln[0]       # (a zero-length segment)
                lines.append(np.c_[ln, rng.normal(size=n)])
                reach.append(float(rng.choice([0.0, 5.0, 40.0, big, np.inf])))
            starts = np.r_[0, np.cumsum([len(ln) for ln in lines])]
            self.ask(xy, rng.normal(size=k), np.concatenate(lines), starts, reach, 1.0)


class RailYieldTest(unittest.TestCase):
    def test_cut_out_of_a_crossing_road(self):
        xy, z = track()
        road = (np.array([[0.0, -100.0], [0.0, 100.0]]), np.full(2, 10.0), 4.0)    # at grade: bed 0.55 m under it
        pieces = grading.rail_yield_pieces(xy, z, [road], railways.GRADE_HALF_M)
        self.assertEqual(len(pieces), 2)
        gap = 4.0 + railways.GRADE_HALF_M + grading.RAIL_YIELD_PAD_M
        for pxy, pz in pieces:
            self.assertGreaterEqual(float(np.abs(pxy[:, 0]).min()), gap - grading.RAIL_YIELD_STEP_M - 1e-6)
            np.testing.assert_allclose(pz, 9.45)
        self.assertAlmostEqual(float(pieces[0][0][0, 0]), -100.0)
        self.assertAlmostEqual(float(pieces[1][0][-1, 0]), 100.0)

    def test_road_over_or_under_kept(self):
        xy, z = track()
        for dz in (6.0, -6.0):      # a bridge over the track or an underpass: no level crossing
            road = (np.array([[0.0, -100.0], [0.0, 100.0]]), np.full(2, 10.0 + dz), 4.0)
            (pxy, pz), = grading.rail_yield_pieces(xy, z, [road], railways.GRADE_HALF_M)
            self.assertEqual(len(pxy), len(xy))

    def test_street_running_track_gone(self):
        xy, z = track(-50.0, 50.0)
        street = (np.array([[-200.0, 1.0], [200.0, 1.0]]), np.full(2, 10.0), 4.0)    # the track in its carriageway
        self.assertEqual(grading.rail_yield_pieces(xy, z, [street], railways.GRADE_HALF_M), [])

    def test_track_beside_a_road_kept(self):
        """A track in a motorway's median (Perth's Kwinana Freeway), 10 m from a carriageway's centre line: kept."""
        xy, z = track()
        road = (np.array([[-200.0, 10.0], [200.0, 10.0]]), np.full(2, 10.0), 7.0)
        (pxy, _pz), = grading.rail_yield_pieces(xy, z, [road], railways.GRADE_HALF_M)
        self.assertEqual(len(pxy), len(xy))

    def test_long_line_through_a_street_grid(self):
        """A 3 km line through a grid of streets every 100 m (a rail yard's cell): cut at each street it crosses at
        grade (the road's nearest segment by the grid of near segments, earthfast near_crossers), the ones beside it,
        over it or past its end kept off it."""
        xy, z = np.c_[np.linspace(0.0, 3000.0, 601), np.zeros(601)], np.full(601, 9.45)
        roads_ = [(np.array([[x, -400.0], [x, -50.0], [x + 0.5, 50.0], [x, 400.0]]), np.full(4, 10.0), 4.0)
                  for x in np.arange(50.0, 3000.0, 100.0)]
        roads_.append((np.array([[1400.0, -400.0], [1400.0, 400.0]]), np.full(2, 16.0), 4.0))     # a bridge
        roads_.append((np.array([[-500.0, 30.0], [3500.0, 30.0]]), np.full(2, 10.0), 4.0))        # beside it
        roads_.append((np.array([[3100.0, -400.0], [3100.0, 400.0]]), np.full(2, 10.0), 4.0))     # past its end
        pieces = grading.rail_yield_pieces(xy, z, roads_, railways.GRADE_HALF_M)
        self.assertEqual(len(pieces), 31)
        gap = 4.0 + railways.GRADE_HALF_M + grading.RAIL_YIELD_PAD_M
        for x in np.arange(50.0, 3000.0, 100.0):
            for pxy, _pz in pieces:
                self.assertGreaterEqual(float(np.abs(pxy[:, 0] - x).min()), gap - grading.RAIL_YIELD_STEP_M - 1e-6)
        self.assertAlmostEqual(float(pieces[0][0][0, 0]), 0.0)
        self.assertAlmostEqual(float(pieces[-1][0][-1, 0]), 3000.0)

    def test_off(self):
        xy, z = track()
        road = (np.array([[0.0, -100.0], [0.0, 100.0]]), np.full(2, 10.0), 4.0)
        old = grading.RAIL_YIELD_ON
        grading.RAIL_YIELD_ON = False
        try:
            (pxy, pz), = grading.rail_yield_pieces(xy, z, [road], railways.GRADE_HALF_M)
        finally:
            grading.RAIL_YIELD_ON = old
        np.testing.assert_array_equal(pxy, xy)
        np.testing.assert_array_equal(pz, z)


if __name__ == "__main__":
    unittest.main()
