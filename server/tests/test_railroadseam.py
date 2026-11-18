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


class NaturalSlopeTest(unittest.TestCase):
    """grading.seam_on_ground (BNE_RAIL_SEAM_NATURAL): the ground's own slope between a road and the track asks nothing of
    the bed (Schleiden's Oleftalbahn below a road up the hillside, the user's reports r61 / r62; Vist's E6 ramps, r80);
    what grading made of a road still moves it, and the Nemiia case (a road on level ground over a bed below it) too."""

    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
        from test_roads import FRAME
        self.frame = FRAME

    def elev(self, slope):
        """The DEM: a hillside rising slope (m per m) north of y = 10 (flat south of it), in lat / lon."""
        frame = self.frame

        def f(la, lo):
            _e, n, _u = frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), 0.0)
            return 100.0 + slope * np.maximum(np.asarray(n, np.float64) - 10.0, 0.0)
        return f

    def run_seam(self, elev, road_y, road_z, bed_z=100.0, half=3.0):
        import grading
        import railways
        xy = straight(0.0, 101, 200.0)
        zb = np.full(len(xy), bed_z)
        road = straight(road_y, 21, 200.0)
        beside = [(road, np.full(len(road), road_z), half)]
        on = grading.seam_on_ground(self.frame, elev, xy, zb, beside, grading.seam_pack(self.frame, elev, beside))
        off = rs.couple(xy, zb, railways.GRADE_HALF_M, beside, railways.seam_lowered(xy, zb, beside),
                        railways.SEAM_LOWER_M)
        return np.asarray(on), np.asarray(off)

    def test_a_road_up_the_hillside_moves_nothing(self):
        elev = self.elev(1.0)                   # (a 1:1 hillside: the road 20 m north, 10 m up on it)
        on, off = self.run_seam(elev, 20.0, 110.0)
        self.assertGreater(float(off.max()), 105.0, "couple alone stands the bed up toward the road")
        np.testing.assert_allclose(on, 100.0, atol=1e-9)

    def test_a_road_filled_over_its_ground_still_moves_the_bed(self):
        elev = self.elev(0.0)                   # (level ground: the road's 2.1 m is grading's own)
        on, off = self.run_seam(elev, 11.2, 102.1)
        np.testing.assert_allclose(on, off)
        self.assertGreater(float(on.min()), 100.2)

    def test_part_of_the_difference_natural(self):
        import railways
        elev = self.elev(1.0)                   # (the road 4 m over its own ground on the hillside, 20 m north)
        on, off = self.run_seam(elev, 20.0, 114.0)
        room = 20.0 - 3.0 - railways.GRADE_HALF_M
        # its ground's own 10 m less the room's allowance comes off: the bed moved as for a road that much lower
        _on2, off2 = self.run_seam(self.elev(0.0), 20.0, 114.0 - (10.0 - (room / rs.SEAM_SLOPE + rs.SEAM_TOL_M)))
        np.testing.assert_allclose(on, off2, atol=1e-9)
        self.assertLess(float(on.max()), float(off.max()))

    def test_a_road_crossing_over_the_track_stays_a_grade_separation(self):
        """Codex on ee2591a3: the natural difference taken off a road's height before couple's own test of a grade
        separation (a road across the bed more than SEAM_CROSS_M from it is not beside it) pulled a road crossing over
        the track on its embankment (the DEM holding the embankment) to the bed's level: the bed then went to it. The
        separation is told by the road's own height: nothing moves."""
        import grading
        import railways
        frame = self.frame

        def elev(la, lo):           # (the road's embankment in the DEM: 7 m over the ground round it)
            e, _n, _u = frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), 0.0)
            return np.where(np.abs(np.asarray(e, np.float64) - 100.0) < 6.0, 107.0, 100.0)
        xy = straight(0.0, 101, 200.0)
        zb = np.full(len(xy), 103.0)                    # (the bed on a fill of its own, 5 m under the road)
        road = np.c_[np.full(21, 100.0), np.linspace(-30.0, 30.0, 21)]
        beside = [(road, np.full(21, 108.0), 4.0)]
        on = grading.seam_on_ground(frame, elev, xy, zb, beside, grading.seam_pack(frame, elev, beside))
        np.testing.assert_allclose(on, zb)
        off = rs.couple(xy, zb, railways.GRADE_HALF_M, beside, railways.seam_lowered(xy, zb, beside),
                        railways.SEAM_LOWER_M)
        np.testing.assert_allclose(off, zb)
        # (the road's height with the natural difference taken off before the test: at grade, and the bed pulled down)
        eff = [(road, np.full(21, 108.0 - (7.0 - rs.SEAM_TOL_M)), 4.0)]
        moved = rs.couple(xy, zb, railways.GRADE_HALF_M, eff, (), 0.0)
        self.assertLess(float(np.min(moved)), 102.0)

    def test_a_road_crossing_at_grade_and_again_raised_keeps_its_separation(self):
        """Codex on b2a9b4b3: a road crossing the track at grade is lowered (railways.seam_lowered: the flush crossing)
        all along, and its second crossing 2.3 m over the bed on an approach read 1.75 m: at grade, the bed pulled up
        to it. The separation test reads the road's own heights."""
        import grading
        import railways
        frame = self.frame
        elev = self.elev(0.0)
        xy = straight(0.0, 101, 200.0)
        zb = np.full(len(xy), 100.0)
        road = np.array([[50.0, -30.0], [50.0, 30.0], [150.0, 30.0], [150.0, -30.0]])
        rz = np.array([100.55, 100.55, 102.3, 102.3])          # (at grade at x 50, 2.3 m up at x 150)
        beside = [(road, rz, 4.0)]
        self.assertEqual(railways.seam_lowered(xy, zb, beside), (0,))
        on = np.asarray(grading.seam_on_ground(frame, elev, xy, zb, beside, grading.seam_pack(frame, elev, beside)))
        at = np.abs(xy[:, 0] - 150.0) < 6.0
        np.testing.assert_allclose(on[at], 100.0, atol=1e-9)
        old = rs.couple(xy, zb, railways.GRADE_HALF_M, beside, (0,), railways.SEAM_LOWER_M)   # (the plain path)
        self.assertGreater(float(np.max(old[at])), 101.0, "couple's own lowered test takes it as at grade")


if __name__ == "__main__":
    unittest.main()
