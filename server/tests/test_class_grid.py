"""Exact-lattice class reuse; missing data, source lifetimes, concurrent plans and material/terrain parity."""
import gc
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import weakref
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fast
import geodesy
import heightmap
import mapcells
import maprasters
import surface

FRAME = (49.0, 7.0, 0.0, 1.0)
WARPS = list(surface.WARP_OCTAVES)


def values(n, ix, iy):
    y, x = np.mgrid[:n, :n]
    return (((x + ix) * 17 + (y + iy) * 31) % 101).astype(np.uint8)


class ClassGridTest(unittest.TestCase):
    def setUp(self):
        fast.require(171, "class grid tests")

    def cache(self, size=64*4096):
        return fast._ef.ClassGridCache(size)

    def lookup(self, cache, n=128, ix=-128, iy=-128, sources=None, frame=FRAME):
        return cache.lookup(frame, WARPS, ix, iy, n, [] if sources is None else sources)

    def test_seeded_moves_negative_indices_and_eviction(self):
        rng = np.random.default_rng(731)
        cache = self.cache()
        source = object()
        for _ in range(70):
            n = int(rng.choice([64, 128, 256]))
            ix, iy = (rng.integers(-5, 6, 2)*64).tolist()
            expected = values(n, ix, iy)
            plan, missing = self.lookup(cache, n, ix, iy, [("cell", source)])
            got = cache.finish(plan, np.ascontiguousarray(expected.ravel()[missing]), True)
            np.testing.assert_array_equal(got, expected)
            self.assertLessEqual(cache.bytes, 64*4096)
        self.assertGreater(cache.hits, 0)
        tiny = self.cache(4096)
        for ix in (0, 64, 128, 0):
            p, m = self.lookup(tiny, 64, ix, 0)
            tiny.finish(p, values(64, ix, 0).ravel()[m], True)
            self.assertEqual(tiny.bytes, 4096)

    def test_sources_replaced_and_frame_changed(self):
        cache = self.cache()
        a, b = object(), object()
        for source, val, frame in [(a, 1, FRAME), (a, 1, FRAME), (b, 2, FRAME), (b, 3, (50.,7.,0.,1.))]:
            p, m = self.lookup(cache, 64, sources=[("cell", source)], frame=frame)
            got = cache.finish(p, np.full(len(m), val, np.uint8), True)
            self.assertTrue((got == val).all())

    def test_incomplete_and_unaligned_results_not_retained(self):
        cache = self.cache()
        for complete, ix in [(False, 0), (True, 1)]:
            p, m = self.lookup(cache, 64, ix, 0)
            self.assertEqual(len(m), 4096)
            got = cache.finish(p, np.full(4096, 30, np.uint8), complete)
            self.assertTrue((got == 30).all())
            self.assertEqual(cache.bytes, 0)

    def test_independent_plans_and_stale_generation(self):
        cache = self.cache()
        p1, m1 = self.lookup(cache, 64)
        p2, m2 = self.lookup(cache, 64)
        for p, m in [(p2, m2), (p1, m1)]:
            self.assertTrue((cache.finish(p, np.full(len(m), 40, np.uint8), True) == 40).all())
        p3, m3 = self.lookup(cache, 64, 0, 0)
        cache.clear()
        self.assertTrue((cache.finish(p3, np.full(len(m3), 50, np.uint8), True) == 50).all())
        self.assertEqual(cache.bytes, 0)

    def test_invalid_plan_values_and_retry(self):
        cache = self.cache()
        p, m = self.lookup(cache, 64)
        with self.assertRaises(ValueError):
            self.cache().finish(p, np.zeros(len(m), np.uint8), True)
        with self.assertRaises(ValueError):
            cache.finish(p, np.zeros(len(m)-1, np.uint8), True)
        with self.assertRaises(ValueError):
            cache.finish(p, np.zeros(2*len(m), np.uint8)[::2], True)
        cache.finish(p, np.zeros(len(m), np.uint8), True)
        with self.assertRaises(ValueError):
            cache.finish(p, np.zeros(len(m), np.uint8), True)
        for n in (0, 2049):
            with self.assertRaises(ValueError):
                self.lookup(cache, n)

    def test_source_tokens_do_not_retain_raster_data(self):
        class Source: pass
        cache = self.cache()
        source = Source()
        ref = weakref.ref(source)
        p, m = self.lookup(cache, 64, sources=[("cell", ref)])
        cache.finish(p, np.full(len(m), 30, np.uint8), True)
        del p, source
        gc.collect()
        self.assertIsNone(ref())
        self.assertEqual(cache.bytes, 4096)
        replacement = Source()
        p, m = self.lookup(cache, 64, sources=[("cell", weakref.ref(replacement))])
        self.assertEqual(len(m), 4096, "a new raster cannot reuse the retained dead identity token")

    def test_returned_arrays_and_input_values_cannot_mutate_the_cache(self):
        cache = self.cache()
        p, m = self.lookup(cache, 64)
        v = np.full(len(m), 30, np.uint8)
        out = cache.finish(p, v, True)
        out.fill(80)
        v.fill(10)
        p, m = self.lookup(cache, 64)
        self.assertEqual(len(m), 0)
        self.assertTrue((cache.finish(p, np.empty(0,np.uint8), True) == 30).all())

    def test_conservative_warp_bounds_and_polar_bypass(self):
        for la in (-84., -49., 0., 49., 84.):
            lat = la + np.linspace(-0.001, 0.001, 128).reshape(16,8)
            lon = np.full_like(lat, 7.)
            s, w, n, e = fast._ef.class_grid_bounds(lat, lon, WARPS)
            a, b = surface.warp(lat, lon)
            self.assertTrue((a >= s).all() and (a <= n).all() and (b >= w).all() and (b <= e).all())
        for lat, lon in [(90., 0.), (-90., 0.), (0., 180.), (85.05, 0.)]:
            self.assertIsNone(fast._ef.class_grid_bounds(np.full((2,2),lat), np.full((2,2),lon), WARPS))
        with self.assertRaises(ValueError):
            fast._ef.class_grid_bounds(np.full((2,2), np.nan), np.zeros((2,2)), WARPS)


class StableMapData:
    def __init__(self, keys):
        self.layers = {}
        for key in keys:
            w, s, e, n = key.bounds()
            y, x = np.mgrid[:64, :64]
            data = np.take(np.array([10,30,50,70,80], np.uint8), (x+3*y) % 5)
            self.layers[key] = maprasters.Raster(data, "EPSG:4326", w, n, (e-w)/64, (n-s)/64)
    def has(self, key, layer): return key in self.layers
    def cell(self, key, layers, kind="background"):
        return SimpleNamespace(layers={"landcover": self.layers[key]})
    def prefetch(self, *args): raise AssertionError("offline fixture must never fetch")


class BlockConsumerTest(unittest.TestCase):
    def setUp(self):
        fast.require(171, "block class tests")
        self.frame = geodesy.EnuFrame(49., 7.)
        self.n = 256
        la, lo, _ = self.frame.enu_to_geodetic(np.array([-1024.,1024.]), np.array([-1024.,1024.]), 0.)
        keys = mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()))
        self.md = StableMapData(keys)
        self.s = surface.Surface(self.md)
        self.addCleanup(self.s.close)

    def grid(self, cx, cy):
        n = self.n
        x = cx - n/2 + np.arange(n)
        y = cy - n/2 + np.arange(n)
        la, lo, _ = self.frame.enu_to_geodetic(x[None,:], y[:,None], 0.)
        return la, lo

    def test_raw_material_and_ter_bytes_equal_across_moves(self):
        for cx, cy in [(0,0),(128,0),(128,128),(0,0),(-128,0)]:
            la, lo = self.grid(cx, cy)
            original, complete = self.s.classes(la, lo)
            reused, ready = self.s.block_classes(self.frame, cx, cy, la, lo)
            self.assertTrue(complete and ready)
            np.testing.assert_array_equal(original, reused)
            y,x = np.mgrid[:self.n,:self.n]
            u = np.sin(x/50.)*30 + np.cos(y/70.)*40
            a = surface.classify(original, u, 1., la, lo)
            b = surface.classify(reused, u, 1., la, lo)
            np.testing.assert_array_equal(a, b)
            with tempfile.TemporaryDirectory(prefix="bnge-class-parity-") as d:
                q = np.clip((u+100)*64,0,65535).astype(np.uint16)
                paths = [Path(d,'old.ter'), Path(d,'cached.ter')]
                for path, idx in zip(paths,(a,b)):
                    heightmap.write_ter(str(path), q, idx, [m[0] for m in surface.MATERIALS])
                self.assertEqual(paths[0].read_bytes(), paths[1].read_bytes())
        self.assertGreater(self.s._class_grid.hits, 0)

    def test_decoded_raster_replacement_invalidates_cached_classes(self):
        la, lo = self.grid(0,0)
        before, _ = self.s.block_classes(self.frame,0,0,la,lo)
        for key, old in list(self.md.layers.items()):
            self.md.layers[key] = maprasters.Raster(np.full_like(old.data,40), old.crs, old.x0,old.y0,old.dx,old.dy)
        after, ready = self.s.block_classes(self.frame,0,0,la,lo)
        self.assertTrue(ready and (after == 40).all())
        self.assertFalse(np.array_equal(before, after))

    def test_missing_cells_not_retained_and_no_unused_halo_fetch(self):
        la, lo = self.grid(0,0)
        saved = self.md.layers
        self.md.layers = {}
        queued = []
        self.s._queue = lambda key, layers=("landcover",): (queued.append(key), self.s._pending.add(key))
        missing, ready = self.s.block_classes(self.frame,0,0,la,lo)
        self.assertFalse(ready)
        self.assertTrue((missing == 0).all())
        self.assertFalse(hasattr(self.s,"_class_grid"))
        expected_keys = mapcells.cells_for_points(*surface.warp(la,lo))
        self.assertEqual(set(queued), set(expected_keys))
        self.md.layers = saved
        self.s._pending.clear()
        got, ready = self.s.block_classes(self.frame,0,0,la,lo)
        reference, _ = self.s.classes(la,lo)
        self.assertTrue(ready)
        np.testing.assert_array_equal(got, reference)


if __name__ == "__main__":
    unittest.main()
