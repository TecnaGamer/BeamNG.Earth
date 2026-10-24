"""Railway tracks (railways.py): the rail lines taken, the level crossings, the bed's heights and the mesh."""
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import railways  # noqa: E402
import roads  # noqa: E402
from test_roads import FRAME, features  # noqa: E402


def flat(xy):
    return np.full(len(xy), 10.0)


class RailwayTest(unittest.TestCase):
    def test_track_mesh_along_a_line(self):
        f = features([[(-50.0, 100.0), (300.0, 100.0)]], ["standard_gauge"], subtypes=["rail"])
        mesh = railways.tile_mesh(FRAME, 0, 0, [f], flat, "fi")
        self.assertIsNotNone(mesh)
        P, T, UV, mats, M = mesh
        self.assertEqual(mats, [railways.BALLAST, railways.CONCRETE, railways.STEEL, railways.SLEEPER])
        self.assertLess(len(P), railways.VERTS_MAX)
        self.assertEqual(int(T.max()) + 1, len(P))
        steel = P[np.unique(T[M == 2])]
        self.assertAlmostEqual(float(steel[:, 2].max()),
                               10.0 + railways.BALLAST_H - railways.SLEEPER_SINK + railways.SLEEPER_H + railways.RAIL_H, 3)
        heads = np.unique(np.round(steel[:, 1] + 0.0, 2))      # (the tile's origin at y 0)
        gap = heads.max() - heads.min() - 2 * railways.RAIL_HEAD    # between the rails' inner faces
        self.assertAlmostEqual(gap, railways.FI_GAUGE_M, delta=0.011, msg="Finnish broad gauge")
        n_sleepers = (M == 3).sum() // 10
        self.assertGreater(n_sleepers, 256 / railways.SLEEPER_EVERY * 0.95)
        self.assertGreaterEqual(P[:, 0].min(), -roads.OVERLAP - railways.STEP_M - 3.0)

    def test_roads_and_trams_are_not_tracks(self):
        f = features([[(0.0, 50.0), (200.0, 50.0)], [(0.0, 80.0), (200.0, 80.0)]], ["residential", "tram"],
                     subtypes=["road", "rail"])
        self.assertIsNone(railways.tile_mesh(FRAME, 0, 0, [f], flat))

    def test_level_crossing_cuts_the_track(self):
        f = features([[(0.0, 100.0), (250.0, 100.0)], [(120.0, 0.0), (120.0, 250.0)]], ["standard_gauge", "secondary"],
                     subtypes=["rail", "road"])
        P, T, UV, mats, M = railways.tile_mesh(FRAME, 0, 0, [f], flat)
        x = P[np.unique(T)][:, 0]
        hw = roads.road_specs(f)[1].width / 2
        self.assertFalse(((x > 120 - hw) & (x < 120 + hw)).any(), "nothing on the road")

    def test_short_tunnel_under_a_road_is_track_and_abandoned_lines_are_not(self):
        """The line under a road bridge tagged is_tunnel for 12 m (OSM's habit) is track; a long tunnel is left out;
        an abandoned line beside it is not drawn (Joensuu, 2026-10-25)."""
        f = features([[(0.0, 100.0), (250.0, 100.0)], [(0.0, 110.0), (250.0, 110.0)], [(0.0, 150.0), (250.0, 150.0)]],
                     ["standard_gauge", "unknown", "standard_gauge"], subtypes=["rail"] * 3,
                     flags=[(0, "is_tunnel", 0.46, 0.51), (1, "is_abandoned"), (2, "is_tunnel", 0.2, 0.8)])
        rails = railways.parts(FRAME, [f], roads.tile_bbox(0, 0))
        got = [(fid, kind, float(np.ptp(xy[:, 0]))) for fid, _c, kind, xy in rails]
        self.assertEqual(sorted(g[0] for g in got), ["id000", "id002", "id002"], "no abandoned line")
        self.assertAlmostEqual([g[2] for g in got if g[0] == "id000"][0], 250.0, 0, "track through the short tunnel")
        self.assertLess(max(g[2] for g in got if g[0] == "id002"), 60.0, "a long tunnel left out")

    def test_short_piece_on_its_own_is_left_out(self):
        f = features([[(0.0, 100.0), (120.0, 100.0)], [(120.0, 100.0), (250.0, 100.0)], [(50.0, 130.0), (62.0, 130.0)]],
                     ["standard_gauge"] * 3, subtypes=["rail"] * 3)
        got = sorted(fid for fid, *_ in railways.parts(FRAME, [f], roads.tile_bbox(0, 0)))
        self.assertEqual(got, ["id000", "id001"], "the 12 m stub goes, two short joined pieces of a line stay")

    def test_road_high_over_the_track_does_not_cut_it(self):
        f = features([[(0.0, 100.0), (250.0, 100.0)], [(120.0, 0.0), (120.0, 250.0)]], ["standard_gauge", "secondary"],
                     subtypes=["rail", "road"])

        def cutting(xy):            # the track 7 m down a cutting, the road on its own level over it
            return np.where(np.abs(xy[:, 1] - 100.0) < 12.0, 10.0, 17.0)
        P, T, UV, mats, M = railways.tile_mesh(FRAME, 0, 0, [f], cutting)
        x = P[np.unique(T[M == 2])][:, 0]
        self.assertTrue(((x > 115) & (x < 125)).any(), "the rails go on under the road")

    def test_bed_smooths_bumps_but_follows_hills(self):
        s = np.arange(0.0, 400.0, railways.STEP_M)
        z = 0.05 * s + 0.4 * np.sin(s / 3.0)
        zb = railways.bed_heights(z)
        mid = slice(20, -20)
        self.assertLess(np.abs(zb - 0.05 * s)[20:-20].max(), 0.1, "the bumps gone")
        self.assertLess(np.abs(zb - 0.05 * s).max(), 0.3, "the grade kept to the ends")

    def _bed(self, f, ground):
        rails = railways.parts(FRAME, [f], roads.tile_bbox(0, 0))
        prof = railways.profile(rails, ground)
        return rails, prof

    def _line(self, ground):
        f = features([[(-400.0, 100.0), (100.0, 100.0)], [(100.0, 100.0), (150.0, 100.0)],
                      [(150.0, 100.0), (640.0, 100.0)]], ["standard_gauge"] * 3, subtypes=["rail"] * 3,
                     flags=[(1, "is_bridge")])
        rails, prof = self._bed(f, ground)
        pts = np.concatenate([xy for *_, xy in rails])
        z = np.concatenate([zb for zb, _ in prof])
        o = np.argsort(pts[:, 0])
        return f, pts[o, 0], z[o]

    def test_bridge_over_a_dip_keeps_the_line_level(self):
        """A short bridge over a stream or an underpass road (the ground 3 m down under it): the deck straight at the
        line's height, no ramps (a 6 m clearance stood an 8 m culvert's track 6.8 m up on 4.5 m ramps, 2026-10-25)."""
        def dip(xy):
            return np.where((xy[:, 0] > 112) & (xy[:, 0] < 138), 7.0, 10.0)
        f, x, z = self._line(dip)
        self.assertLess(np.abs(z - 10.0).max(), 0.05, "on the ground's level everywhere, the bridge too")
        P, T, UV, mats, M = railways.tile_mesh(FRAME, 0, 0, [f], dip)
        piers = P[np.unique(T[M == 1])]
        self.assertTrue(((piers[:, 2] < 7.0) & (piers[:, 0] > 100) & (piers[:, 0] < 150)).any(),
                        "a pier down to the stream bed")

    def test_piers_stand_beside_a_road_under_the_bridge_on_the_ground(self):
        """A rail bridge over a road in a 5 m dip: no pier in the road's lanes, every pier's foot under the ground at
        all its corners (one stood in the lanes, one hung in the air, 2026-10-25)."""
        f = features([[(-300.0, 100.0), (60.0, 100.0)], [(60.0, 100.0), (190.0, 100.0)],
                      [(190.0, 100.0), (500.0, 100.0)], [(125.0, -100.0), (125.0, 300.0)]],
                     ["standard_gauge"] * 3 + ["secondary"], subtypes=["rail"] * 3 + ["road"], flags=[(1, "is_bridge")])

        def dip(xy):                # the ground falling 5 m to the road over 40 m either side of it
            return 10.0 - 5.0 * np.clip(1.0 - np.abs(xy[:, 0] - 125.0) / 40.0, 0.0, 1.0)
        P, T, UV, mats, M = railways.tile_mesh(FRAME, 0, 0, [f], dip)
        con = P[np.unique(T[M == 1])]
        low = con[con[:, 2] < 8.5]          # the piers' feet (the deck's underside: 10 + BALLAST_H - DECK_D = 8.9)
        self.assertTrue(len(low), "piers there")
        hw = roads.road_specs(f)[3].width / 2
        self.assertFalse((np.abs(low[:, 0] - 125.0) < hw + 0.5).any(), "none in the road")
        self.assertTrue((low[:, 2] <= dip(low[:, :2]) - railways.PIER_FOOT_M + 1e-6).all(), "all in the ground")

    def test_ground_through_a_deck_raises_it_on_gentle_ramps(self):
        """Ground under a bridge's middle higher than its ends (bad data, or a ridge): the deck over it, the line
        climbing to it at GRADE at most and back down to the ground past the ramps."""
        def hump(xy):
            return np.where((xy[:, 0] > 120) & (xy[:, 0] < 130), 11.0, 10.0)
        f, x, z = self._line(hump)
        deck = (x > 120) & (x < 130)
        self.assertGreaterEqual(z[deck].min(), 11.0 - 1e-6)
        dz = np.abs(np.diff(z)) / np.maximum(np.diff(x), 1e-9)
        self.assertLessEqual(dz[np.diff(x) > 0.5].max(), railways.GRADE + 1e-6)
        self.assertAlmostEqual(float(z[x < -300].max()), 10.0, 1, "back on the ground past the ramp")

    def test_bridge_over_a_valley_runs_straight(self):
        """A bridge over a 30 m deep valley between its banks: straight between them, piers down to the floor."""
        f = features([[(-200.0, 100.0), (60.0, 100.0)], [(60.0, 100.0), (200.0, 100.0)],
                      [(200.0, 100.0), (450.0, 100.0)]], ["standard_gauge"] * 3, subtypes=["rail"] * 3,
                     flags=[(1, "is_bridge")])

        def valley(xy):
            x = xy[:, 0]
            return np.where((x > 70) & (x < 190), -20.0, 10.0 + 0.01 * x)
        rails, prof = self._bed(f, valley)
        (zb, zg), = [p for p, (_, _, k, _) in zip(prof, rails) if k == "bridge"]
        line = np.linspace(zb[0], zb[-1], len(zb))
        self.assertLess(np.abs(zb - line).max(), 0.3)
        self.assertAlmostEqual(zb[0], 10.6, 0)
        self.assertAlmostEqual(zb[-1], 12.0, 0)
        P, T, UV, mats, M = railways.tile_mesh(FRAME, 0, 0, [f], valley)
        self.assertLess(P[np.unique(T[M == 1])][:, 2].min(), -20.0)

    def test_track_follows_a_hill(self):
        f = features([[(-100.0, 100.0), (400.0, 100.0)]], ["standard_gauge"], subtypes=["rail"])

        def hill(xy):
            return 10.0 + 0.015 * xy[:, 0]
        rails, prof = self._bed(f, hill)
        zb, zg = prof[0]
        self.assertLess(np.abs(zb - zg)[10:-10].max(), 0.05)

    def test_ground_graded_to_the_bed(self):
        """A line through a 6 m ridge (a rock cutting the DEM half shows): the grader lays the ground to the track's
        bed, the walls back from it, so the track is not buried (2026-10-25: half a track under the rock)."""
        import grading
        from test_grading import FakeMD
        f = features([[(-400.0, 0.0), (400.0, 0.0)]], ["standard_gauge"], subtypes=["rail"])

        def ridge(la, lo):
            e, _n, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
            return 10.0 + 6.0 * np.exp(-(np.asarray(e) / 40.0) ** 2)
        g = grading.Grader(FRAME, FakeMD(f), ridge)
        e = np.linspace(-60.0, 60.0, 25)
        for off in (0.0, 1.5, -1.5):
            la, lo, _ = FRAME.enu_to_geodetic(e, np.full(len(e), off), 0.0)
            raw = ridge(la, lo)
            got = g.apply(la, lo, raw)
            rails = railways.parts(FRAME, [f], (-60, -10, 60, 10))
            (zb, _), = railways.profile(rails, lambda xy: ridge(*FRAME.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)[:2]))
            want = np.interp(e, rails[0][3][:, 0], zb)
            self.assertLess(np.abs(got - want).max(), 0.1, f"the ground at {off} m off the line is at its bed")
            self.assertLess(abs(float(got[12]) - float(raw[12])), 0.3, "over the ridge as it is (the real elevation)")
        self.assertLess(railways.FOOT_MAX_M, 10.0)

    def test_written_with_the_tile(self):
        f = features([[(-50.0, 100.0), (300.0, 100.0)]], ["standard_gauge"], subtypes=["rail"])
        mesh = railways.tile_mesh(FRAME, 0, 0, [f], flat)
        with tempfile.TemporaryDirectory() as d:
            roads.write_tile(d, "/vfs", 1, 0, 0, [], rail_mesh=mesh)
            self.assertTrue(os.path.exists(os.path.join(d, "rail_1_0_0.cdae")))
            self.assertIn("rail", roads.shape_files(1, 0, 0))


if __name__ == "__main__":
    unittest.main()
