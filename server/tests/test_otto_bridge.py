"""Offline replay of I-80 / Otto Road at 41.10022, -105.06194.

The fixture contains real Overture geometry and captured DEM point samples,
not solved heights. Grading, bridge solving, chunking and meshes run afresh.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import geodesy
import grading
import mapcells
import mapgeom
import revisit
import roadmesh
import roads

LAT, LON = 41.10022, -105.06194
FIDS = ("a4ce4242-6c0c-4f09-af3f-4ef16198fae5", "c055323a-f109-486c-9a9d-ad07f216b366")


class OttoBridgeTest(TestCase):
    @classmethod
    def setUpClass(cls):
        patch = mock.patch.object(revisit, "ON", False)
        patch.start()
        cls.addClassCleanup(patch.stop)
        with np.load(Path(__file__).parent / "fixtures" / "otto_wyoming.npz", allow_pickle=False) as archive:
            cls.data = {k: archive[k] for k in archive.files}
        cls.frame = geodesy.EnuFrame(LAT, LON)
        cells = {str(key): mapgeom.Features.from_arrays("roads", cls.data, f"cell{i}.")
                 for i, key in enumerate(cls.data["cells"])}
        origin = cls.data["sample_origin"]
        samples = {tuple(k.astype(np.int64) + origin): float(z)
                   for k, z in zip(cls.data["sample_keys"], cls.data["sample_z"])}

        def sample(la, lo):
            a, b = np.broadcast_arrays(la, lo)
            keys = np.c_[np.rint(a.ravel() * 1e9).astype(np.int64),
                         np.rint(b.ravel() * 1e9).astype(np.int64),
                         np.full(a.size, 16, dtype=np.int64)]
            # Missing queries fail explicitly; no synthetic terrain or network.
            return np.array([samples[tuple(k)] for k in keys]).reshape(a.shape)

        def cell(key, **kwargs):
            return SimpleNamespace(layers={"roads": cells[str(key)]})

        cls.grader = grading.Grader(cls.frame, SimpleNamespace(cell=cell), sample)

        def elev(la, lo):
            return cls.grader.apply(la, lo, sample(la, lo), grading.ROAD_LIFT)

        cls.elev = staticmethod(elev)
        cls.bridges = roads.BridgeProfiles(cls.frame, lambda k: cells[str(k)], elev)
        cls.parts = cls.bridges.parts([cells[str(mapcells.cell_of(LAT, LON))]], (-300, -300, 300, 300))

    def test_real_spans_meet_approaches_without_clearance_crests(self):
        self.assertEqual(tuple(p[0] for p in self.parts), FIDS)
        for i, (fid, spec, xy, z, lon) in enumerate(self.parts):
            s = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))]
            self.assertGreater(s[-1], 30.0)
            self.assertLess(s[-1], 36.0)
            chord = z[0] + (z[-1] - z[0]) * s / s[-1]
            self.assertLess(float(np.max(z - chord)), 0.10, fid)
            slopes = np.diff(z) / np.diff(s)
            self.assertLess(float(np.abs(slopes).max()), 0.02, fid)
            self.assertLess(float(np.abs(np.diff(slopes)).max()), 0.014, fid)
            approach = self.bridges._ground(self.data[f"approach{i}.xy"])
            # Fixed before/after reference: the motorway approaches are not lifted.
            np.testing.assert_allclose(approach, self.data[f"approach{i}.z"], atol=0.003, rtol=0)
            np.testing.assert_allclose(z[[0, -1]], approach[[0, 5]], atol=1e-6, rtol=0)
            entering = (approach[0] - approach[1]) / 4.0
            leaving = (approach[6] - approach[5]) / 4.0
            self.assertLess(abs(slopes[0] - entering), 0.015)
            self.assertLess(abs(slopes[-1] - leaving), 0.015)

    def test_girders_clear_the_full_width_of_otto_road(self):
        points = self.data["footprint_xy"]
        ground = self.bridges._ground(points)
        for fid, spec, xy, z, lon in self.parts:
            ab = np.diff(xy, axis=0)
            t = np.clip(((points[:, None] - xy[None, :-1]) * ab[None]).sum(2)
                        / (ab * ab).sum(1), 0, 1)
            projection = xy[None, :-1] + t[:, :, None] * ab[None]
            distance = np.linalg.norm(points[:, None] - projection, axis=2)
            j = np.argmin(distance, axis=1)
            under = distance[np.arange(len(points)), j] <= spec.width / 2
            top = z[j] + t[np.arange(len(points)), j] * (z[j + 1] - z[j])
            usable = top - ground - roadmesh.DECK_DEPTH - roadmesh.GIRDER_D
            self.assertGreater(int(under.sum()), 20)
            self.assertGreaterEqual(float(usable[under].min()), roads.MIN_BRIDGE_HEADROOM_M, fid)

    def test_connected_lower_road_and_junctions_stay_drivable(self):
        xy = self.data["lower_xy"]
        z = self.bridges._ground(xy)
        s = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))]
        slopes = np.diff(z) / np.diff(s)
        self.assertLess(float(np.abs(slopes).max()), grading.UNDER_MAX_GRADE)
        self.assertLess(float(np.abs(np.diff(slopes)).max()), 0.035)
        # Every road incident at either real Otto Road ramp junction agrees.
        plans = self.grader.cells[mapcells.cell_of(LAT, LON)]
        for point in (xy[0], xy[-1]):
            ends = [float(p.z[at]) for _fid, p in plans for at in (0, -1)
                    if np.linalg.norm(p.xy[at] - point) < 0.01]
            self.assertGreaterEqual(len(ends), 3)
            # Original south-junction profiles differ by 2.01 cm. Common
            # lowering must not amplify that existing sampling discrepancy.
            self.assertLess(max(ends) - min(ends), 0.025)

    def test_surface_barriers_and_collision_share_the_solved_profile(self):
        for fid, spec, xy, z, lon in self.parts:
            chunks = roads._deck_chunks([(fid, spec, np.c_[xy, z], lon)], (-100, -100, 100, 150))
            chunks = [c for c in chunks if c.get("deck") and not c.get("ctx")]
            for c in chunks:
                left, right, _, _, _, h = roadmesh.ribbons([c])[0]
                np.testing.assert_allclose(np.linalg.norm(left - right, axis=1), spec.width, atol=0.003)
                surface, *_ = roadmesh.build(self.frame, [c], None, (0, 0, 0))
                collision, *_ = roadmesh.deck([c], (0, 0, 0))
                k = len(h)
                np.testing.assert_allclose(surface[:k, 2], h + roadmesh.DECK_SKIN)
                for edge in (left, right):
                    for pt, height in zip(edge, h):
                        at = np.linalg.norm(collision[:, :2] - pt, axis=1) < 1e-6
                        self.assertTrue(np.any(np.isclose(collision[at, 2], height, rtol=0, atol=1e-6)))
                        self.assertTrue(np.any(np.isclose(collision[at, 2], height + roadmesh.PARAPET_H,
                                                         rtol=0, atol=1e-6)))
