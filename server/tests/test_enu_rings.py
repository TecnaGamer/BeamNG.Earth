"""mapgeom.enu_rings / enu_bounds (earthfast.enu_rings / enu_bounds): many rings in one call, bit for bit what
enu_xy gives ring by ring (its xy_in slices of the layer's rings and its conversion of other rings), NumPy's min / max."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import geodesy  # noqa: E402
import mapgeom  # noqa: E402


def _feats(rng, n=120):
    """n features of one part, each of one or two rings."""
    lonlat, ring_off, coord_off = [], [0], [0]
    for _ in range(n):
        nr = int(rng.integers(1, 3))
        for _ in range(nr):
            k = int(rng.integers(2, 12))
            lonlat.append(np.array([16.37, 48.21]) + rng.normal(0, 0.01, (k, 2)))
            coord_off.append(coord_off[-1] + k)
        ring_off.append(ring_off[-1] + nr)
    return mapgeom.Features(layer="r", ids=np.array([f"{i:04d}" for i in range(n)]), kind=np.full(n, 2, np.uint8),
                            part_off=np.arange(n + 1), ring_off=np.array(ring_off), coord_off=np.array(coord_off),
                            lonlat=np.vstack(lonlat))


def _rings(f, rng):
    """The layer's own rings (views of lonlat), pieces cut out of them (copies), a (k, 3) ring, column views."""
    out = []
    for i in range(len(f)):
        for ring in f.parts(i)[0]:
            out.append(ring)                                        # a view: xy_in's slice
            if len(ring) > 3:
                out.append(ring[1:-1].copy())                       # a copy: converted
                out.append(np.c_[ring, rng.normal(0, 5, len(ring))])    # a z column
                out.append(np.asfortranarray(ring))                 # strided
    return out


def _same(a, b):
    a, b = np.asarray(a), np.asarray(b)
    return a.dtype == b.dtype and a.shape == b.shape and a.tobytes() == b.tobytes()


class EnuRingsTest(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(11)
        self.f = _feats(rng)
        self.rings = _rings(self.f, rng)
        self.frames = [geodesy.EnuFrame(48.21, 16.37, 0.0), geodesy.EnuFrame(48.2, 16.4, 31.5)]

    def test_rings_as_enu_xy(self):
        for fr in self.frames:      # (the second frame: xy_in by the kept ECEF's rotation)
            got = mapgeom.enu_rings(self.f, fr, self.rings)
            self.assertEqual(len(got), len(self.rings))
            for r, xy in zip(self.rings, got):
                self.assertTrue(_same(xy, np.column_stack(mapgeom.enu_xy(self.f, fr, r))))

    def test_bounds_as_numpy_min_max(self):
        for fr in self.frames:
            bb = mapgeom.enu_bounds(self.f, fr, self.rings)
            self.assertEqual(bb.shape, (len(self.rings), 4))
            for r, b in zip(self.rings, bb):
                e, n = mapgeom.enu_xy(self.f, fr, r)
                self.assertTrue(np.array_equal(b, [e.min(), e.max(), n.min(), n.max()]))   # (as numbers)

    def test_nan_and_empty(self):
        fr = self.frames[0]
        r = np.array([[16.37, 48.21], [np.nan, 48.22], [16.38, 48.2]])
        b = mapgeom.enu_bounds(self.f, fr, [r])[0]
        e, n = mapgeom.enu_xy(self.f, fr, r)
        self.assertTrue(np.isnan(e.max()) and np.isnan(b[0]) and np.isnan(b[1]))
        self.assertTrue(np.isnan(b[2]) == np.isnan(n.min()) and np.isnan(b[3]) == np.isnan(n.max()))
        empty = np.zeros((0, 2))
        self.assertTrue(_same(mapgeom.enu_rings(self.f, fr, [empty])[0], np.column_stack(mapgeom.enu_xy(self.f, fr,
                                                                                                         empty))))
        with self.assertRaises(ValueError):         # (as min / max of no values)
            mapgeom.enu_bounds(self.f, fr, [empty])
        self.assertEqual(mapgeom.enu_rings(self.f, fr, []), [])
        self.assertEqual(mapgeom.enu_bounds(self.f, fr, []).shape, (0, 4))

    def test_long_batches_in_chunks(self):
        # (more points than one chunk of the kernels' copy (65536), and one ring longer than a chunk on its own)
        rng = np.random.default_rng(5)
        rings = [np.array([16.37, 48.21]) + rng.normal(0, 0.01, (int(k), 2)) for k in rng.integers(2, 4000, 60)]
        rings.insert(17, np.array([16.37, 48.21]) + rng.normal(0, 0.01, (70000, 2)))
        fr = self.frames[0]
        got = mapgeom.enu_rings(self.f, fr, rings)
        bb = mapgeom.enu_bounds(self.f, fr, rings)
        self.assertEqual(len(got), len(rings))
        for r, xy, b in zip(rings, got, bb):
            e, n = mapgeom.enu_xy(self.f, fr, r)
            self.assertTrue(_same(xy, np.column_stack((e, n))))
            self.assertTrue(np.array_equal(b, [e.min(), e.max(), n.min(), n.max()]))

    def test_other_frames_and_types_ring_by_ring(self):
        class Half(geodesy.EnuFrame):           # (a frame converting its own way: enu_xy's, ring by ring)
            def geodetic_to_enu(self, lat, lon, h=0.0):
                e, n, u = super().geodetic_to_enu(lat, lon, h)
                return e * 0.5, n * 0.5, u
        fr = Half(48.21, 16.37, 0.0)
        got = mapgeom.enu_rings(self.f, fr, self.rings[:20])
        for r, xy in zip(self.rings[:20], got):
            self.assertTrue(_same(xy, np.column_stack(mapgeom.enu_xy(self.f, fr, r))))
        f32 = [r.astype(np.float32) for r in self.rings[:10]]
        fr = self.frames[0]
        for r, xy in zip(f32, mapgeom.enu_rings(self.f, fr, f32)):
            self.assertTrue(_same(xy, np.column_stack(mapgeom.enu_xy(self.f, fr, r))))
        bb = mapgeom.enu_bounds(self.f, fr, f32)
        for r, b in zip(f32, bb):
            e, n = mapgeom.enu_xy(self.f, fr, r)
            self.assertTrue(np.array_equal(b, [e.min(), e.max(), n.min(), n.max()]))
        # (a one-shot iterable with a ring taken ring by ring: every ring still converted)
        mixed = [self.rings[0], f32[1], self.rings[2]]
        got = mapgeom.enu_rings(self.f, fr, iter(mixed))
        self.assertEqual(len(got), 3)
        for r, xy in zip(mixed, got):
            self.assertTrue(_same(xy, np.column_stack(mapgeom.enu_xy(self.f, fr, r))))
        self.assertEqual(mapgeom.enu_bounds(self.f, fr, iter(mixed)).shape, (3, 4))


if __name__ == "__main__":
    unittest.main()
