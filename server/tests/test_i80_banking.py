"""Offline replay of the actual I-80 curve at 41.41135, -105.83960.

The input geometry is sparse Overture/OSM, not an idealized circular arc.
Elevation comes from a captured raw DEM raster, before road grading.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cdae
import geodesy
import grading
import heightmap
import roadmesh
import roads
import terrainpatch


class WyomingBankingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with np.load(Path(__file__).parent / "fixtures/i80_wyoming_banking.npz", allow_pickle=False) as archive:
            cls.data = {k: archive[k] for k in archive.files}
        cls.frame = geodesy.EnuFrame(*cls.data["origin"], 0.)
        cls.parts = []
        for k in range(2):
            ll = cls.data[f"road{k}"]
            x, y, _ = cls.frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
            xy = grading.clip_polyline(np.c_[x, y], (-900., -900., 900., 900.))[0]
            la, lo, _ = cls.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.)
            sp = roads.Spec(**json.loads(str(cls.data[f"spec{k}"])))
            cls.parts.append((np.c_[lo, la], sp))
        cls.plans = grading.plan_roads(cls.frame, cls.parts, cls.raw, cuts=False)
        with mock.patch.object(grading, "BANK_ON", False):
            cls.level = grading.plan_roads(cls.frame, cls.parts, cls.raw, cuts=False)

    @classmethod
    def raw(cls, la, lo):
        x, y, _ = cls.frame.geodetic_to_enu(la, lo, 0.)
        x, y = np.asarray(x), np.asarray(y)
        fx = (x - cls.data["dem_x"][0]) / 8.
        fy = (y - cls.data["dem_y"][0]) / 8.
        if np.any((fx < 0) | (fy < 0) | (fx >= 300) | (fy >= 300)):
            raise AssertionError("query outside captured DEM")
        ix, iy = np.floor(fx).astype(int), np.floor(fy).astype(int)
        u, v = fx - ix, fy - iy
        z = cls.data["dem_height"]
        return ((1 - u) * (1 - v) * z[iy, ix] + u * (1 - v) * z[iy, ix + 1]
                + (1 - u) * v * z[iy + 1, ix] + u * v * z[iy + 1, ix + 1])

    @classmethod
    def elev(cls, la, lo):
        x, y, _ = cls.frame.geodetic_to_enu(la, lo, 0.)
        return grading.grade_points(np.asarray(x).ravel(), np.asarray(y).ravel(), cls.raw(la, lo).ravel(),
                                    cls.plans, grading.ROAD_LIFT).reshape(np.shape(la))

    def test_real_curve_bank_angles_and_rate(self):
        for k, p in enumerate(self.plans):
            ds = np.linalg.norm(np.diff(p.xy, axis=0), axis=1)
            near = np.linalg.norm(p.xy, axis=1) < 650.
            rate = np.abs(np.diff(p.bank) / ds)[near[:-1] & near[1:]]
            self.assertLess(float(rate.max()), .0008, "cross-slope change per metre")
            b = p.bank[near]
            self.assertLessEqual(float(abs(np.arctan(b)).max()), np.arctan(.08) + 1e-9)
            # The two opposite directions of this left/right curve must lean
            # toward the same physical side; tiny map kinks must not reverse it.
            signed = b * (-1 if k == 0 else 1)
            self.assertGreaterEqual(float(signed.min()), -1e-8)
            curve = near & (p.xy[:, 0] > 0) & (p.xy[:, 0] < 350)
            self.assertGreater(float(np.median(abs(p.bank[curve]))), .015)
            self.assertLess(float(abs(p.bank[curve]).max()), .05)

    def test_lane_edge_vertical_rate_and_natural_grades(self):
        for p, flat in zip(self.plans, self.level):
            np.testing.assert_array_equal(p.xy, flat.xy)
            np.testing.assert_array_equal(p.z, flat.z)  # hills and centerline smoothing preserved
            self.assertGreater(np.ptp(p.z), 2.)
            ds = np.linalg.norm(np.diff(p.xy, axis=0), axis=1)
            near = np.linalg.norm(p.xy, axis=1) < 650.
            edge_rate = abs(np.diff(p.bank * p.road_half) / ds)
            self.assertLess(float(edge_rate[near[:-1] & near[1:]].max()), .0035)

    def test_adjacent_real_curve_segments_share_bank_and_edge_heights(self):
        for p, (_, sp) in zip(self.plans, self.parts):
            i = int(np.argmin(np.linalg.norm(p.xy, axis=1)))
            pieces = (p.xy[:i + 1], p.xy[i:])
            parts = []
            for xy in pieces:
                la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.)
                parts.append((np.c_[lo, la], sp))
            ext = [(None, pieces[1][:51]), (pieces[0][-51:][::-1], None)]
            a, b = grading.plan_roads(self.frame, parts, self.raw, ext=ext, cuts=False)
            np.testing.assert_allclose(a.xy[-1], b.xy[0], atol=1e-7)
            self.assertAlmostEqual(a.z[-1], b.z[0], delta=.001)
            self.assertAlmostEqual(a.bank[-1], b.bank[0], delta=.0002)
            for side in (-1, 1):
                za = a.z[-1] + side * a.road_half * a.bank[-1]
                zb = b.z[0] + side * b.road_half * b.bank[0]
                self.assertAlmostEqual(za, zb, delta=.003)

    def test_rendered_mesh_matches_collision_ground_at_tile_seam(self):
        # The road ribbon is render-only; use the same graded/quantized ground
        # as the real TerrainBlock. Two independently built tiles meet at x=0.
        edges = []
        for x0, x1 in ((-256., 0.), (0., 256.)):
            chunks = [{"m": "road_asphalt_2lane", "w": sp.width, "tl": 10., "p": 20,
                       "n": np.c_[p.xy, p.z].tolist()} for (_, sp), p in zip(self.parts, self.plans)]
            mesh = roadmesh.build(self.frame, chunks, self.elev, (x0, -512., 0.),
                                  clip=(x0, -512., x1, 256.), follow=True, q=1 / 256)
            pos = mesh[0] + np.array([x0, -512., 0.])
            pos = pos[np.unique(mesh[1])]  # follow_ground retains unused pre-drape vertices
            ground = roadmesh.drawn_ground(self.frame, self.elev, pos[:, :2], q=1 / 256)
            self.assertLess(float(abs(pos[:, 2] - ground).max()), roadmesh.FOLLOW_TOL + .001)
            edge = pos[abs(pos[:, 0]) < 1e-5]
            edges.append({round(float(v[1]), 3): float(v[2]) for v in edge})
        shared = edges[0].keys() & edges[1].keys()
        self.assertGreater(len(shared), 2)
        self.assertLess(max(abs(edges[0][y] - edges[1][y]) for y in shared), .001)

    def test_collision_mesh_banking_has_no_sharp_wheel_track_pulses(self):
        # Serialized collision-only triangles from the same quantized heights
        # as the terrain, then inspect the cross-slope contribution near the lane.
        for p, flat in zip(self.plans, self.level):
            xy, s = grading.resample(p.xy, 2.)
            tangent = np.gradient(xy, axis=0)
            tangent /= np.linalg.norm(tangent, axis=1)[:, None]
            normal = np.c_[-tangent[:, 1], tangent[:, 0]]
            near = np.linalg.norm(xy, axis=1) < 600.
            for side in (-1, 1):
                wheel = xy + side * 2. * normal
                la, lo, _ = self.frame.enu_to_geodetic(wheel[:, 0], wheel[:, 1], 0.)
                base = self.raw(la, lo)
                z = grading.grade_points(wheel[:, 0], wheel[:, 1], base, [p], grading.ROAD_LIFT)
                ref = grading.grade_points(wheel[:, 0], wheel[:, 1], base, [flat], grading.ROAD_LIFT)
                # Quantization is included; compare adjacent physical samples.
                bank_height = (heightmap.quantise(z, 1 / 256) - heightmap.quantise(ref, 1 / 256)) / 256
                rate = abs(np.diff(bank_height) / np.diff(s))
                self.assertLess(float(rate[near[:-1] & near[1:]].max()), .006)
        xs = np.arange(-128., 130., 2.)
        ys = np.arange(-128., 66., 2.)
        gx, gy = np.meshgrid(xs, ys)
        la, lo, _ = self.frame.enu_to_geodetic(gx, gy, 0.)
        z = self.frame.geodetic_to_enu(la, lo, self.elev(la, lo))[2]
        z = heightmap.quantise(z, 1 / 256) / 256
        pos, tris = terrainpatch.mesh(xs, ys, z, (0., 0.))
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "curve.cdae")
            cdae.write(path, pos, tris, collision_only=True, material=terrainpatch.MATERIAL, close=False)   # (terrain)
            shape = cdae.read(path)
            m = next(m for m in shape["meshes"] if m["type"] == 0)
            actual = cdae.mesh_arrays(m)["verts"]  # exporter reorders vertices by triangles
            actual = actual[np.lexsort(actual.T[::-1])]
            expected = pos[np.lexsort(pos.T[::-1])]
            np.testing.assert_allclose(actual, expected, atol=.0002)
        normals = np.cross(pos[tris[:, 1]] - pos[tris[:, 0]], pos[tris[:, 2]] - pos[tris[:, 0]])
        self.assertTrue((normals[:, 2] > 0).all())

    def test_bridge_banking_fade_is_gradual_but_still_level_at_abutment(self):
        s = np.arange(0., 1002., 2.)
        xy = np.c_[400 * np.sin(s / 400), 400 * (1 - np.cos(s / 400))]
        b = grading.bank(xy, roads.Spec(8., "paved", False, "motorway"), abut=(True, True))
        self.assertAlmostEqual(b[0], 0., places=9)
        self.assertAlmostEqual(b[-1], 0., places=9)
        self.assertGreater(float(abs(b).max()), .07)  # realistic bank retained
        self.assertLess(float(abs(np.diff(b) / 2.).max()), .00125)


if __name__ == "__main__":
    unittest.main()
