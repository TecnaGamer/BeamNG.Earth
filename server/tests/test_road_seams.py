"""Joined motorway profiles must not acquire a wheel-track bump at every map segment.

A 128 m partition repeats every 4.1 s at 70 mph. Constant curvature/grade lets
us distinguish an artificial segment-end transition from a real hill or turn.
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


class MotorwaySeamTest(unittest.TestCase):
    radius = 400.
    seams = (256., 384.)

    def xy(self, s):
        th = np.asarray(s) / self.radius
        return np.stack([self.radius * np.sin(th), self.radius * (1 - np.cos(th))], axis=-1)

    def ll(self, s):
        xy = self.xy(s)
        la, lo, _ = FRAME.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.)
        return np.c_[lo, la]

    def raw(self, la, lo):
        x, y, _ = FRAME.geodetic_to_enu(la, lo, 0.)
        s = self.radius * np.arctan2(x, self.radius - y)
        return 50. + .02 * s

    def plans(self, abut=None):
        spec = roads.Spec(8., "paved", False, "motorway")
        ranges = ((128., 256.), (256., 384.), (384., 512.))
        parts = [(self.ll(np.arange(a, b + 1, 2.)), spec) for a, b in ranges]
        # Same outward continuations produced by Grader._extensions, sampled well
        # beyond the curvature stencil/runoff and longitudinal Gaussian support.
        ext = [(self.xy(np.arange(a, a - 125, -2.)), self.xy(np.arange(b, b + 125, 2.)))
               for a, b in ranges]
        split = grading.plan_roads(FRAME, parts, self.raw, ext=ext, abut=abut, cuts=False)
        whole = grading.plan_roads(FRAME, [(self.ll(np.arange(128., 513., 2.)), spec)], self.raw,
                                   ext=[(ext[0][0], ext[-1][1])], cuts=False)
        return split, whole

    def wheel(self, s, side):
        s = np.asarray(s)
        normal = np.stack([-np.sin(s / self.radius), np.cos(s / self.radius)], axis=-1)
        return self.xy(s) + side * 2. * normal

    def sample(self, plans, xy):
        la, lo, _ = FRAME.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.)
        return grading.grade_points(xy[:, 0], xy[:, 1], self.raw(la, lo), plans, grading.ROAD_LIFT)

    def test_neighbor_positions_heights_slopes_and_bank(self):
        parts, whole = self.plans()
        for a, b, seam in zip(parts[:-1], parts[1:], self.seams):
            np.testing.assert_allclose(a.xy[-1], b.xy[0], atol=1e-7)
            self.assertAlmostEqual(a.z[-1], b.z[0], delta=1e-4)
            ga = (a.z[-1] - a.z[-2]) / np.linalg.norm(a.xy[-1] - a.xy[-2])
            gb = (b.z[1] - b.z[0]) / np.linalg.norm(b.xy[1] - b.xy[0])
            self.assertAlmostEqual(ga, gb, delta=1e-4)
            expected = np.interp(seam, np.arange(128., 513., 4.), whole[0].bank)
            self.assertAlmostEqual(a.bank[-1], expected, delta=1e-5)
            self.assertAlmostEqual(b.bank[0], expected, delta=1e-5)

    def test_both_wheel_tracks_have_no_periodic_segment_bumps(self):
        parts, whole = self.plans()
        s = np.arange(224., 417., 1.)
        for side in (-1, 1):
            xy = self.wheel(s, side)
            actual, reference = self.sample(parts, xy), self.sample(whole, xy)
            self.assertLess(np.max(abs(actual - reference)), .005)
            self.assertLess(np.max(abs(np.diff(actual - reference, n=2))), .002)

    def test_quantized_collision_geometry_at_segment_seams(self):
        parts, whole = self.plans()
        for seam in self.seams:
            center = self.xy(seam)
            xs = np.arange(np.floor(center[0] / 2) * 2 - 24, center[0] + 26, 2.)
            ys = np.arange(np.floor(center[1] / 2) * 2 - 24, center[1] + 26, 2.)
            gx, gy = np.meshgrid(xs, ys)
            xy = np.c_[gx.ravel(), gy.ravel()]
            r = np.hypot(xy[:, 0], self.radius - xy[:, 1])
            carriageway = abs(r - self.radius) < 3.
            decoded = []
            with tempfile.TemporaryDirectory() as folder:
                for k, plans in enumerate((parts, whole)):
                    z = self.sample(plans, xy).reshape(gx.shape)
                    _, zb, mh, grid = heightmap.write_png(os.path.join(folder, f"h{k}.png"), z, q=1 / 256)
                    z = heightmap.heights(grid, zb, mh)
                    # Collision triangles, not only profile arrays. Serialize the mesh
                    # and inspect the positions consumed by the game collision shape.
                    pos, tris = terrainpatch.mesh(xs, ys, z, (0., 0.))
                    path = os.path.join(folder, f"col{k}.cdae")
                    cdae.write(path, pos, tris, collision_only=True, material=terrainpatch.MATERIAL)
                    shape = cdae.read(path)
                    mesh = next(m for m in shape["meshes"] if m["type"] == 0)
                    decoded.append(cdae.mesh_arrays(mesh)["verts"])
                    normals = np.cross(pos[tris[:, 1]] - pos[tris[:, 0]], pos[tris[:, 2]] - pos[tris[:, 0]])
                    self.assertTrue((normals[:, 2] > 0).all())
            np.testing.assert_array_equal(decoded[0][:, :2], decoded[1][:, :2])
            self.assertLess(np.max(abs(decoded[0][carriageway, 2] - decoded[1][carriageway, 2])), .009)

    def test_bridge_abutments_still_fade_to_level(self):
        parts, _ = self.plans(abut=[(False, True), (True, False), (False, False)])
        self.assertAlmostEqual(parts[0].bank[-1], 0., places=9)
        self.assertAlmostEqual(parts[1].bank[0], 0., places=9)

    def test_real_chain_lookup_keeps_banking_through_the_join(self):
        from test_roads import features, LAT, LON
        from test_grading import FakeMD
        import mapcells
        ranges = ((128., 256.), (256., 384.), (384., 512.))
        f = features([self.xy(np.arange(a, b + 1, 2.)) for a, b in ranges], ["motorway"] * 3)
        f.lonlat = np.concatenate([self.ll(np.arange(a, b + 1, 2.)) for a, b in ranges])
        specs = roads.road_specs(f)
        parts = [(roads.ground_parts(f, i)[0], specs[i]) for i in range(3)]
        g = grading._Planner(FRAME, FakeMD(f), self.raw)      # (a planner in FRAME: its chain lookup)
        g._chain_cands(mapcells.cell_of(LAT, LON), f, specs)
        ext = g._extensions(parts, [str(fid) for fid in f.ids])
        self.assertIsNotNone(ext[0][1])
        self.assertIsNotNone(ext[1][0])
        plans = grading.plan_roads(FRAME, parts, self.raw, ext=ext, cuts=False)
        self.assertAlmostEqual(plans[0].bank[-1], -grading.BANK_E_MAX, delta=1e-5)
        self.assertAlmostEqual(plans[1].bank[0], -grading.BANK_E_MAX, delta=1e-5)

    def test_terrain_follow_overlap_keeps_collision_heights(self):
        parts, _ = self.plans()

        def elev(la, lo):
            x, y, _ = FRAME.geodetic_to_enu(la, lo, 0.)
            return grading.grade_points(x.ravel(), y.ravel(), self.raw(la, lo).ravel(),
                                        parts, grading.ROAD_LIFT).reshape(np.shape(la))

        # Default small-block follow distance: 128 m. Test independent builds,
        # then a shared ChunkCache, including decoded uint16 collision heights.
        with tempfile.TemporaryDirectory() as folder:
            grids = []
            cache = heightmap.ChunkCache()
            for i, (cx, chunks) in enumerate(((256., None), (384., None), (256., cache), (384., cache))):
                keep = {}
                info = heightmap.generate(FRAME, elev, os.path.join(folder, f"block{i}.png"),
                                          cx=cx, cy=128., size=512, q=1 / 256,
                                          chunks=chunks, keep=keep)
                grids.append(heightmap.heights(keep["qgrid"], info["zBase"], info["maxHeight"]))
            np.testing.assert_array_equal(grids[0][:, 64:], grids[1][:, :-64])
            np.testing.assert_array_equal(grids[0], grids[2])
            np.testing.assert_array_equal(grids[1], grids[3])


if __name__ == "__main__":
    unittest.main()
