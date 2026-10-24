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

    def test_bed_smooths_bumps_but_follows_hills(self):
        s = np.arange(0.0, 400.0, railways.STEP_M)
        z = 0.05 * s + 0.4 * np.sin(s / 3.0)
        zb = railways.bed_heights(z)
        mid = slice(20, -20)
        self.assertLess(np.abs(zb[mid] - 0.05 * s[mid]).max(), 0.25)
        self.assertTrue((zb >= z - 0.15 - 1e-9).all() or np.abs(zb - z).max() < 0.6)

    def _bed(self, f, ground):
        rails = railways.parts(FRAME, [f], roads.tile_bbox(0, 0))
        prof = railways.profile(rails, ground)
        return rails, prof

    def test_bridge_over_flat_ground_rises_on_ramps(self):
        """A bridge segment between two ground segments on the flat (it crosses a road): its deck BRIDGE_CLEAR_M up,
        the track either side climbing to it at GRADE at most, across the segments' joins."""
        f = features([[(-400.0, 100.0), (100.0, 100.0)], [(100.0, 100.0), (140.0, 100.0)],
                      [(140.0, 100.0), (640.0, 100.0)]], ["standard_gauge"] * 3, subtypes=["rail"] * 3,
                     flags=[(1, "is_bridge")])
        rails, prof = self._bed(f, flat)
        kinds = [k for _, _, k, _ in rails]
        self.assertEqual(sorted(kinds), ["bridge", "ground", "ground"])
        pts = np.concatenate([xy for *_, xy in rails])
        z = np.concatenate([zb for zb, _ in prof])
        o = np.argsort(pts[:, 0])
        x, z = pts[o, 0], z[o]
        deck = (x > 100) & (x < 140)
        self.assertGreaterEqual(z[deck].min(), 10.0 + railways.BRIDGE_CLEAR_M - 1e-6)
        dz = np.abs(np.diff(z)) / np.maximum(np.diff(x), 1e-9)
        self.assertLessEqual(dz[np.diff(x) > 0.5].max(), railways.GRADE + 1e-6)
        self.assertAlmostEqual(float(z[x < -300].max()), 10.0, 1, "back on the ground past the ramp")
        P, T, UV, mats, M = railways.tile_mesh(FRAME, 0, 0, [f], flat)
        piers = P[np.unique(T[M == 1])]
        self.assertTrue(((piers[:, 2] < 10.0 - 1.0) & (piers[:, 0] > 100) & (piers[:, 0] < 140)).any(),
                        "a pier into the ground under the deck")

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

    def test_written_with_the_tile(self):
        f = features([[(-50.0, 100.0), (300.0, 100.0)]], ["standard_gauge"], subtypes=["rail"])
        mesh = railways.tile_mesh(FRAME, 0, 0, [f], flat)
        with tempfile.TemporaryDirectory() as d:
            roads.write_tile(d, "/vfs", 1, 0, 0, [], rail_mesh=mesh)
            self.assertTrue(os.path.exists(os.path.join(d, "rail_1_0_0.cdae")))
            self.assertIn("rail", roads.shape_files(1, 0, 0))


if __name__ == "__main__":
    unittest.main()
