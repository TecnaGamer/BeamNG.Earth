"""Numerical checks for registration, runoff and stable scan assembly."""
import unittest
import numpy as np

from assemble_cliff_scans import (CHANNELS, detail_from_normals, frame, geometric_normal,
                                  make_canvas, minimum_path, normal_from_detail, patch_from, paste, unit)
from cliff_weathering import runoff, shape_and_erode, weather_maps
from bake_scan_cliff_material import flat_normal, residual_normal, runtime_frame, source_detail


class ScanAssemblyTests(unittest.TestCase):
    def test_flat_bake_keeps_broad_relief_even_when_low_reference_contains_it(self):
        yy, xx = np.mgrid[:65,:129].astype(np.float32)
        height = 1.4*np.sin(xx*2*np.pi/129)+.8*np.cos(yy*4*np.pi/65)
        detail = np.zeros((*height.shape,3),np.float32)
        detail[...,2] = 1
        flat = flat_normal(height,detail,strip_rows=13)
        residual = residual_normal(height,detail,height,(129,65))
        np.testing.assert_allclose(residual,detail,atol=1e-6)
        self.assertGreater(float(np.abs(flat[...,:2]).max()),.08)
        reference = runtime_frame(np.zeros_like(height))
        reconstructed = sum(axis*flat[...,i:i+1] for i,axis in enumerate(reference))
        np.testing.assert_allclose(reconstructed,runtime_frame(height)[2],atol=1e-6)

    def test_flat_bake_strip_seams_preserve_source_detail_and_unit_normals(self):
        rng = np.random.default_rng(42)
        height = rng.normal(size=(39,47)).astype(np.float32)*.06
        full = rng.integers(80,220,size=(39,47,3),dtype=np.uint8)
        detail = source_detail(height,full,47/91.44,strip_rows=7)
        expected = detail_from_normals(unit(full.astype(np.float32)/127.5-1),geometric_normal(height,47/91.44))
        np.testing.assert_allclose(detail,expected,atol=1e-6)
        flat = flat_normal(height,detail,strip_rows=7)
        legacy_flat = residual_normal(height,detail,np.zeros((4,4),np.float32),(47,39))
        np.testing.assert_allclose(flat,legacy_flat,atol=1e-6)
        np.testing.assert_allclose(np.linalg.norm(flat,axis=-1),1,atol=1e-6)

    def test_runtime_bake_does_not_double_existing_mesh_slopes(self):
        yy, xx = np.mgrid[:64,:128].astype(np.float32)
        height = .6*np.sin(xx*2*np.pi/128)+.4*np.cos(yy*2*np.pi/64)
        detail = np.zeros((*height.shape,3),np.float32)
        detail[...,2] = 1
        residual = residual_normal(height, detail, height, (128,64))
        np.testing.assert_allclose(residual, detail, atol=1e-6)

    def test_runtime_bake_encodes_missing_high_frequency_relief(self):
        yy, xx = np.mgrid[:64,:128].astype(np.float32)
        high = .05*np.sin(xx*.5)+.08*np.cos(yy*.6)
        detail = np.zeros((*high.shape,3),np.float32)
        detail[...,2] = 1
        residual = residual_normal(high, detail, np.zeros((8,16),np.float32), (128,64))
        low_frame = runtime_frame(np.zeros_like(high))
        reconstructed = sum(axis*residual[...,i:i+1] for i,axis in enumerate(low_frame))
        np.testing.assert_allclose(reconstructed, runtime_frame(high)[2], atol=1e-6)
        self.assertGreater(float(np.abs(residual[...,:2]).max()), .01)

    def test_normal_transfer_round_trip(self):
        rng = np.random.default_rng(12)
        geometry = unit(rng.normal(size=(20, 30, 3)).astype(np.float32))
        shading = unit(geometry + rng.normal(size=geometry.shape)*.1)
        np.testing.assert_allclose(normal_from_detail(detail_from_normals(shading, geometry), geometry),
                                   shading, atol=1e-5)

    def test_rotated_mirrored_scan_normal_agrees_with_height(self):
        yy, xx = np.mgrid[:120, :180].astype(np.float32)
        height = xx*.012-yy*.021
        normal = geometric_normal(height, 1)
        source = dict(height=height, detail=normal, color=np.repeat(height[..., None], 3, axis=-1),
                      ao=np.ones_like(height), roughness=np.ones_like(height))
        patch, _ = patch_from(source, 70, 60, np.random.default_rng(1), True, scale=1.3, angle=19)
        actual = geometric_normal(patch['height'], 1)[3:-3, 3:-3]
        # Bilinear remapping uses subpixel tables; allow their derivative error.
        np.testing.assert_allclose(patch['detail'][3:-3, 3:-3], actual, atol=.001)
        np.testing.assert_allclose(patch['color'][..., 0]*1.3, patch['height'], atol=1e-5)

    def test_seam_avoids_high_error_column(self):
        costs = np.ones((20, 15), np.float32)
        costs[:, 8] = 0
        self.assertTrue(np.all(minimum_path(costs) == 8))

    def test_repeated_joins_do_not_accumulate_offsets(self):
        canvas = make_canvas(40, 200)
        yy, xx = np.mgrid[:30, :40].astype(np.float32)
        patch = dict(height=yy*.1, color=np.ones((30,40,3), np.float32)*.3,
                     detail=np.broadcast_to([0.,0.,1.], (30,40,3)).copy(),
                     roughness=np.ones_like(yy)*.8, ao=np.ones_like(yy))
        for y in range(0, 200, 20):
            paste(canvas, patch, 0, y, 0, 10 if y else 0, 2, 1)
        self.assertTrue(canvas['occupied'].all())
        self.assertLess(float(canvas['height'].max()), 3.6)

    def test_runoff_is_translation_invariant_and_bounded(self):
        yy, xx = np.mgrid[:64, :64].astype(np.float32)
        base = .6*np.cos(xx*.15)+.1*np.sin(yy*.1)
        rain = np.ones_like(base)
        a, cut, deposit, water = runoff(base, .2, rain, iterations=4)
        b, *_ = runoff(base+10, .2, rain, iterations=4)
        np.testing.assert_allclose(a+10, b, atol=1e-5)
        np.testing.assert_allclose(a, base-cut+deposit, atol=1e-6)
        self.assertGreater(float(cut.max()), .03)
        self.assertLessEqual(float(cut.max()), .650001)
        self.assertTrue(np.isfinite(water).all())
        self.assertGreater(float(water[-1].mean()), float(water[0].mean()))

    def test_weathering_keeps_channels_finite_and_is_deterministic(self):
        def example():
            canvas = make_canvas(96, 96)
            canvas['occupied'][:] = True
            canvas['color'][:] = .3
            canvas['detail'][..., 2] = 1
            canvas['ao'][:] = 1
            canvas['roughness'][:] = .8
            return canvas
        a, b = example(), example()
        shape_and_erode(a, 96/91.44)
        shape_and_erode(b, 96/91.44)
        for key in CHANNELS:
            self.assertTrue(np.isfinite(a[key]).all(), key)
            np.testing.assert_array_equal(a[key], b[key])
        self.assertGreater(float(a['height'].std()), .5)
        self.assertLess(float(a['height'].max()), 10)


if __name__ == '__main__':
    unittest.main()
