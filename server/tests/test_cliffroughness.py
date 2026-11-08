"""Cliff roughness follows relief, stays dry, and is bound on every rock stage."""
import hashlib
import json
from pathlib import Path
import sys
import unittest

import numpy as np
from PIL import Image
from scipy.ndimage import label
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'tools'))
import bake_cliff_roughness as bake


class CurvatureRoughnessTest(unittest.TestCase):
    def test_convex_ridges_smoother_than_concave_joints_in_both_directions(self):
        y,x = np.mgrid[:256,:256]*2*np.pi/256
        height = np.cos(x)+np.cos(y)
        n = np.stack((np.sin(x), -np.sin(y), np.ones_like(x)), -1)
        n /= np.linalg.norm(n, axis=-1, keepdims=True)
        for profile in bake.PROFILES:
            r,e = bake.roughness(n, profile)
            self.assertLess(float(r[height>1].mean())+.06, float(r[height<-1].mean()))
            self.assertGreaterEqual(float(r.min()), .679)
            self.assertLessEqual(float(r.max()), .971)
            np.testing.assert_allclose(bake.roughness(np.roll(n, 43, axis=1),profile)[0],
                                       np.roll(r,43,axis=1),atol=1e-6)

    def test_flat_sloping_surface_does_not_gain_arbitrary_noise(self):
        n = np.tile([.3,-.2,.9],(64,64,1)).astype(np.float32)
        r,e = bake.roughness(n)
        np.testing.assert_allclose(r,.86,atol=1e-6)
        self.assertFalse(e.any())

    def test_polished_patches_are_coherent_moderate_and_keep_recesses_matte(self):
        base = np.full((1024,1024), .86, dtype=np.float32)
        exposure = np.full_like(base, .2)
        exposure[:,512:] = -.5
        result, mask = bake.polished_patches(base, exposure, 42)
        np.testing.assert_array_equal(result, bake.polished_patches(base, exposure, 42)[0])
        self.assertGreater(float(np.mean(mask > .1)), .01)
        self.assertLess(float(np.mean(mask > .1)), .12)
        self.assertGreaterEqual(float((result/base).min()), .659)
        self.assertLessEqual(float((result/base).max()), 1)
        np.testing.assert_array_equal(result[:,512:], base[:,512:])
        # Island interiors vary substantially and are rougher than the old uniform polish.
        ratios = (result/base)[mask > .99]
        self.assertGreater(ratios.size, 100)
        self.assertGreater(float(ratios.mean()), .75)
        self.assertLess(float(ratios.mean()), .90)
        self.assertGreater(float(np.percentile(ratios,90)-np.percentile(ratios,10)), .10)
        # Fine grain may interrupt highlights sharply, but never by the full polish contrast.
        contrast = .86 * bake.PATCH_REDUCTION
        self.assertLess(float(np.abs(np.diff(result[:,:510], axis=1)).max()) / contrast, .75)

    def test_fine_grain_breaks_up_the_interior_without_glossier_specks(self):
        reduction = bake.patch_reduction((512,512), 42)
        self.assertGreaterEqual(float(reduction.min()), 0)
        self.assertLessEqual(float(reduction.max()), .340001)
        # Compare fine variation inside solid 16x16 areas, not patch boundaries.
        blocks = reduction.reshape(32,16,32,16).transpose(0,2,1,3)
        spread = np.std(blocks, axis=(2,3))
        means = np.mean(blocks, axis=(2,3))
        self.assertGreater(float(np.median(spread)), .035)
        self.assertGreater(float(np.std(means)), .02)
        self.assertGreater(float(np.percentile(spread,90)-np.percentile(spread,10)), .015)
        self.assertGreater(float(np.mean(np.abs(np.diff(reduction,axis=1)))), .02)
        seam = float(np.mean(np.abs(reduction[:,0]-reduction[:,-1])))
        self.assertLess(seam, float(np.mean(np.abs(np.diff(reduction,axis=1))))*1.5)

    def test_smaller_patches_add_coverage_without_removing_existing_patches(self):
        base = np.full((512,512), .86, dtype=np.float32)
        exposure = np.full_like(base, .2)
        original_noise = bake.periodic_perlin
        def without_small(shape, cell_texels, seed):
            if cell_texels == bake.SMALL_PATCH_CELL_TEXELS:
                return np.zeros(shape, np.float32)
            return original_noise(shape, cell_texels, seed)
        with patch.object(bake, 'periodic_perlin', side_effect=without_small):
            previous, previous_mask = bake.polished_patches(base, exposure, 42)
        result, mask = bake.polished_patches(base, exposure, 42)
        self.assertTrue(np.all(mask >= previous_mask))
        self.assertTrue(np.all(result <= previous))
        added = (mask > .1) & (previous_mask == 0)
        self.assertGreater(float(added.mean()), .01)
        self.assertLess(float(added.mean()), .08)

    def test_smaller_regions_break_up_previous_large_highlights(self):
        base = np.full((1024,1024), .86, dtype=np.float32)
        exposure = np.full_like(base, .2)
        _, current = bake.polished_patches(base, exposure, 42)
        with patch.object(bake, 'PATCH_CELL_TEXELS', 64), patch.object(bake, 'SMALL_PATCH_CELL_TEXELS', 24):
            _, previous = bake.polished_patches(base, exposure, 42)
        def region_areas(mask):
            regions, _ = label(mask > .1)
            return np.bincount(regions.ravel())[1:]
        smaller, larger = region_areas(current), region_areas(previous)
        self.assertGreater(smaller.size, larger.size * 2)
        self.assertLess(np.percentile(smaller,95), np.percentile(larger,95) * .5)
        self.assertAlmostEqual(float((current > .1).mean()), float((previous > .1).mean()), delta=.015)

    def test_perlin_noise_wraps_without_a_boundary_jump(self):
        noise = bake.periodic_perlin((256,512), 32, 17)
        interior = float(np.abs(np.diff(noise, axis=1)).max())
        self.assertLessEqual(float(np.abs(noise[:,0]-noise[:,-1]).max()), interior*1.1)
        self.assertAlmostEqual(float(noise.mean()), 0, delta=.015)

    def test_all_packaged_rock_stages_use_aligned_nonuniform_maps(self):
        audit=json.loads(bake.AUDIT_PATH.read_text())
        records={r['file']:r for r in audit['files']}
        checked=set()
        for file in ('cliff.materials.json','cliff_contact.materials.json'):
            for name,mat in json.loads((bake.ASSETS/file).read_text()).items():
                stage=mat['Stages'][0]
                path=bake.MOD/stage['roughnessMap'].lstrip('/')
                source,uv=bake.source_for(name,stage)
                self.assertEqual(stage['roughnessMapUseUV'],uv,name)
                self.assertEqual(stage['roughnessFactor'],1,name)
                self.assertEqual(stage['metallicFactor'],0,name)
                record=records[path.name]
                self.assertEqual(record['source'],source.name)
                if path.name in checked:continue
                checked.add(path.name)
                self.assertEqual(record['source_sha256'],hashlib.sha256(source.read_bytes()).hexdigest())
                self.assertEqual(record['sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
                with Image.open(path) as im:
                    self.assertEqual(im.mode,'RGB')
                    a=np.asarray(im)[...,0]/255.
                self.assertGreaterEqual(float(a.min()), .375 if record['profile'] != 'far' else .745)
                if record['profile'] == 'far':
                    self.assertEqual(record['patch_coverage'], 0)
                else:
                    self.assertGreater(record['patch_coverage'], .01)
                    self.assertLess(record['patch_coverage'], .15)
                self.assertLessEqual(float(a.max()),.975)
                self.assertGreater(float(np.percentile(a,95)-np.percentile(a,5)),.045)
                self.assertGreater(record['concave_mean']-record['convex_mean'],.04)
        self.assertEqual(checked,set(records))


if __name__=='__main__':unittest.main()
