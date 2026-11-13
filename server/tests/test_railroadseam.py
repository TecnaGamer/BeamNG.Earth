"""A track beside a road has no step between them (railroadseam.py, plan 028)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import railroadseam as rs  # noqa: E402


def straight(y, n=51, x1=200.0):
    return np.c_[np.linspace(0.0, x1, n), np.full(n, y)]


class CoupleTest(unittest.TestCase):
    HALF = 3.0

    def road(self, z=100.0, half=8.6):
        xy = straight(0.0, 11)
        return [(xy, np.full(len(xy), z), half)]

    def step(self, xy, zb):
        """Height difference per metre of room between the road's band and the bed's, worst over the track."""
        worst = 0.0
        for p, z in zip(xy, zb):
            room = max(abs(p[1]) - 8.6 - self.HALF, 0.0)
            worst = max(worst, (abs(z - 100.0) - rs.SEAM_TOL_M) / max(room, 1e-9) if abs(z - 100.0) > rs.SEAM_TOL_M else 0.0)
        return worst

    def test_bed_under_the_road_beside_it_is_brought_up(self):
        # the Nemiia case: 11.2 m from the axis of a 7.6 m road, the bed 2.1 m under it: room 0 -> within the tolerance
        xy = straight(11.2)
        zb = np.full(len(xy), 97.9)
        got = rs.couple(xy, zb, self.HALF, self.road())
        self.assertLessEqual(float(np.abs(got - 100.0).max()), rs.SEAM_TOL_M + 1e-6)

    def test_room_allows_a_slope(self):
        # 20 m from the axis: 8.4 m of room carries 8.4 / SEAM_SLOPE of difference: a 2 m cut stays as it is
        xy = straight(20.0)
        zb = np.full(len(xy), 98.0)
        np.testing.assert_allclose(rs.couple(xy, zb, self.HALF, self.road()), zb)

    def test_far_track_untouched(self):
        xy = straight(90.0)
        zb = np.full(len(xy), 90.0)
        self.assertIs(rs.couple(xy, zb, self.HALF, self.road()), zb)

    def test_no_new_step_along_the_track(self):
        # the track runs beside the road for 80 m, then away from it: the move is run out at the track's grade
        x = np.linspace(0.0, 400.0, 101)
        xy = np.c_[x, np.where(x < 150.0, 11.2, 11.2 + (x - 150.0))]
        zb = np.full(len(x), 97.9)
        got = rs.couple(xy, zb, self.HALF, [(straight(0.0, 41, 400.0), np.full(41, 100.0), 8.6)])
        g = np.abs(np.diff(got)) / np.hypot(*np.diff(xy, axis=0).T)
        self.assertLess(float(g.max()), 0.0201)
        self.assertAlmostEqual(float(got[-1]), 97.9, places=6, msg="far from the road it is its own again")

    def test_a_crossing_track_takes_the_roads_height(self):
        x = np.linspace(-100.0, 100.0, 51)
        xy = np.c_[np.zeros(51), x]
        zb = np.full(51, 99.0)
        road = [(np.c_[np.linspace(-100.0, 100.0, 11), np.zeros(11)], np.full(11, 100.0), 8.6)]
        got = rs.couple(xy, zb, self.HALF, road)
        self.assertAlmostEqual(float(got[25]), 100.0, delta=rs.SEAM_TOL_M + 1e-6)

    def test_a_road_crossing_under_a_bridge_keeps_the_bed(self):
        # a track 6 m over a road it crosses at a right angle runs on a bridge, not beside the road: the road under
        # it must not take the approach fill down to the road (the deck then stands at the road, the embankment ends
        # mid-span and the two sides do not meet).
        x = np.linspace(-100.0, 100.0, 51)
        xy = np.c_[x, np.zeros(51)]
        zb = np.full(51, 106.0)
        road = [(np.c_[np.zeros(11), np.linspace(-100.0, 100.0, 11)], np.full(11, 100.0), 8.6)]
        got = rs.couple(xy, zb, self.HALF, road)
        np.testing.assert_allclose(got, zb)

    def test_a_move_run_out_along_the_track_keeps_to_a_crossing_roads_band(self):
        # GitHub #1169, Kennedybruecke in Vienna: an old tram track lying in a bus lane 5 m lower than its bed for 60 m,
        # then crossing a street at grade 40 m on. The bed is taken down to the bus lane there; run out along the track
        # at its grade, that move carried 4 m on to the crossing and the bed lay under the street's lanes (the grading
        # then pulled the street's edge down to it: a 2.65 m step). The vertex at the street keeps to its own band.
        x = np.linspace(0.0, 200.0, 101)
        xy = np.c_[x, np.zeros(101)]
        zb = np.full(101, 100.0)
        lane = (np.c_[np.linspace(0.0, 60.0, 7), np.zeros(7)], np.full(7, 95.0), 3.5)
        street = (np.c_[np.full(11, 100.0), np.linspace(-50.0, 50.0, 11)], np.full(11, 100.0), 3.55)
        got = rs.couple(xy, zb, self.HALF, [lane, street])
        self.assertLessEqual(abs(float(got[50]) - 100.0), rs.SEAM_TOL_M + 1e-6, "at the street: its height")
        self.assertLessEqual(abs(float(got[10]) - 95.0), rs.SEAM_TOL_M + 1e-6, "in the bus lane: the lane's height")

    def test_no_roads(self):
        zb = np.full(5, 3.0)
        self.assertIs(rs.couple(straight(5.0, 5), zb, self.HALF, []), zb)


if __name__ == "__main__":
    unittest.main()
