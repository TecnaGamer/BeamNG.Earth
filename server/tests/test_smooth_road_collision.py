"""Profile-to-collision checks, separate from street furniture.

Synthetic invariants, not a reproduction of the reported in-game bounce.
"""
import os
import tempfile
import unittest

import numpy as np

from test_roads import FRAME
import cdae
import grading
import heightmap
import roads
import terrainpatch


class SmoothRoadCollisionTest(unittest.TestCase):
    def plan(self, angle=0.0, hill=False):
        s = np.arange(-400., 402., grading.PROFILE_STEP)
        direction = np.array([np.cos(angle), np.sin(angle)])
        xy = s[:, None] * direction
        z = 50 + .04 * s
        if hill:
            z += 4 * np.cos(s / 150)
        noisy = z + .15 * np.sin(s * 2 * np.pi / 8)
        p = grading._plan(xy, noisy, roads.Spec(8., "paved", False, "secondary"))
        return p, direction

    def test_short_wavelength_noise_removed_real_hill_retained(self):
        p, _ = self.plan(hill=True)
        s = p.xy[:, 0]
        interior = abs(s) < 250
        expected = 50 + .04 * s + 4 * np.cos(s / 150)
        # (.08: a secondary's 25 m profile sigma, grading.CLASS_SIGMA_M, takes ~5 cm off this 4 m hill's crest)
        self.assertLess(np.max(abs(p.z[interior] - expected[interior])), .08)
        self.assertGreater(np.ptp((p.z - 50 - .04 * s)[interior]), 3.)

    def test_quantized_collision_tracks_keep_the_grade_at_all_angles(self):
        for angle in (0., .31, .79):
            with self.subTest(angle=angle):
                p, d = self.plan(angle)
                xs = ys = np.arange(-80., 82., heightmap.SQUARE)
                gx, gy = np.meshgrid(xs, ys)
                s = gx * d[0] + gy * d[1]
                base = 50 + .04 * s
                z = grading.grade_points(gx.ravel(), gy.ravel(), base.ravel(), [p], grading.ROAD_LIFT).reshape(gx.shape)
                # Inspect the actual quantized samples that TerrainBlock collision uses.
                with tempfile.TemporaryDirectory() as folder:
                    q, zb, mh, grid = heightmap.write_png(os.path.join(folder, "h.png"), z, q=1 / 256)
                    actual = heightmap.heights(grid, zb, mh)
                off = -gx * d[1] + gy * d[0]
                core = abs(off) < 2
                self.assertLess(np.max(abs(actual[core] - (base[core] + grading.ROAD_LIFT))), .006)

    def test_cell_trimming_and_query_batches_do_not_make_a_step(self):
        p, _ = self.plan(hill=True)
        pieces = grading.trim_plan(p, (-400, -20, 0, 20)) + grading.trim_plan(p, (0, -20, 400, 20))
        x = np.arange(-32., 34., 2.)
        base = 50 + .04 * x + 4 * np.cos(x / 150)
        full = grading.grade_points(x, x * 0, base, pieces)
        batches = np.concatenate([grading.grade_points(x[k], x[k] * 0, base[k], pieces)
                                  for k in np.array_split(np.arange(len(x)), 3)])
        np.testing.assert_array_equal(full, batches)
        self.assertLess(np.max(abs(np.diff(full, n=2))), .015)

    def test_collision_mesh_serialization_keeps_a_smooth_grade(self):
        p, _ = self.plan()

        def elev(la, lo):
            x, y, _ = FRAME.geodetic_to_enu(la, lo, 0.)
            # Geodetic height to ENU and back, as GradedDem / patch.build do.
            h = grading.grade_points(x, y, 50 + .04 * x, [p], grading.ROAD_LIFT)
            return FRAME.enu_to_geodetic(x, y, h)[2]

        with tempfile.TemporaryDirectory() as folder:
            parts, _ = terrainpatch.build(FRAME, elev, (-128, 0, 128, 128), os.path.join(folder, "patch"))
            edge = []
            for path, (cx, cy) in parts:
                shape = cdae.read(path)
                mesh = next(m for m in shape["meshes"] if m["type"] == 0)
                v = cdae.mesh_arrays(mesh)["verts"]
                world = v + np.array([cx, cy, 0.])
                # (colliders sit up to cdae.COLLIDER_UNALIGN_M off the lattice in x and y; heights untouched)
                tol = cdae.COLLIDER_UNALIGN_M + 1e-6
                track = world[abs(world[:, 1]) < tol]
                self.assertGreater(len(track), 2)
                self.assertLess(np.max(abs(track[:, 2] - (50 + .04 * track[:, 0] + grading.ROAD_LIFT))), .006)
                edge.extend(world[(abs(world[:, 0]) < tol) & (abs(world[:, 1]) < tol), 2])
            self.assertEqual(len(edge), 2)
            self.assertAlmostEqual(edge[0], edge[1], places=6)


if __name__ == "__main__":
    unittest.main()
