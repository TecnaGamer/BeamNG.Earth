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

    def test_off(self):
        xy, z = track()
        road = (np.array([[0.0, -100.0], [0.0, 100.0]]), np.full(2, 10.0), 4.0)
        old = grading.RAIL_YIELD_ON
        grading.RAIL_YIELD_ON = False
        try:
            (pxy, _pz), = grading.rail_yield_pieces(xy, z, [road], railways.GRADE_HALF_M)
        finally:
            grading.RAIL_YIELD_ON = old
        self.assertIs(pxy, pxy)
        self.assertEqual(len(pxy), len(xy))


if __name__ == "__main__":
    unittest.main()
