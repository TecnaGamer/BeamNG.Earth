"""terrainpatch.py: the ground's collision patch under a collision island off the terrain block (BeamMP support)."""
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy  # noqa: E402
import terrainpatch  # noqa: E402


class TerrainPatchTest(unittest.TestCase):
    def test_snap_box(self):
        self.assertEqual(terrainpatch.snap_box(10, -70, 650, 570), (0.0, -128.0, 768.0, 640.0))
        x0, y0, x1, y1 = terrainpatch.snap_box(0, 0, 4000, 100)
        self.assertLessEqual(x1 - x0, terrainpatch.MAX_SIDE_M + 2 * terrainpatch.SNAP_M, "clipped, then snapped out")
        self.assertTrue(all(v % terrainpatch.SNAP_M == 0 for v in (x0, y0, x1, y1)))

    def test_mesh_grid_and_winding(self):
        xs, ys = terrainpatch.grid((0.0, 0.0, 16.0, 8.0), 4.0)
        self.assertEqual((len(xs), len(ys)), (5, 3))
        z = np.add.outer(ys, xs) * 0.1
        pos, tris = terrainpatch.mesh(xs, ys, z, (8.0, 4.0))
        self.assertEqual(pos.shape, (15, 3))
        self.assertEqual(len(tris), 2 * 4 * 2)
        self.assertEqual(tuple(pos[0]), (-8.0, -4.0, 0.0), "relative to the origin")
        n = np.cross(pos[tris[:, 1]] - pos[tris[:, 0]], pos[tris[:, 2]] - pos[tris[:, 0]])
        self.assertTrue((n[:, 2] > 0).all(), "every triangle faces up")

    def test_build_writes_small_pieces(self):
        """A patch is PIECE_M pieces of 512 triangles, each its own collision shape placed at its centre (the Windows
        game builds one big collision mesh in time growing with the square of its triangles), named by its cell: a
        second patch over the same cells reuses their files."""
        frame = geodesy.EnuFrame(39.28, -76.59)
        with tempfile.TemporaryDirectory() as d:
            prefix = os.path.join(d, "patch_1")
            z = lambda la, lo: np.full(np.shape(la), 12.5)
            parts, n = terrainpatch.build(frame, z, (0, -128, 384, 128), prefix)
            self.assertEqual(len(parts), 3 * 2)
            self.assertEqual(n, 6 * 512, "the whole box's triangles")
            self.assertEqual([c for _, c in parts][:4], [(64.0, -64.0), (192.0, -64.0), (320.0, -64.0), (64.0, 64.0)])
            self.assertEqual(parts[0][0], prefix + "_0_m1.cdae")
            for p, _ in parts:
                self.assertGreater(os.path.getsize(p), 0)
            t0 = os.path.getmtime(parts[0][0])
            parts2, _ = terrainpatch.build(frame, z, (-128, -128, 128, 128), prefix)
            self.assertIn(parts[0][0], [p for p, _ in parts2], "the shared cell, same file")
            self.assertEqual(os.path.getmtime(parts[0][0]), t0, "not written again")
            pos, tris = terrainpatch.mesh(*terrainpatch.grid((0, 0, 128, 128)), np.zeros((17, 17)), (64, 64))
            self.assertEqual(len(tris), 512)
            self.assertAlmostEqual(float(np.abs(pos[:, :2]).max()), 64.0, 5, "a piece's vertices round its own centre")


if __name__ == "__main__":
    unittest.main()
