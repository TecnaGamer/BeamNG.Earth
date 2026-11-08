import sys
from pathlib import Path
import unittest
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'tools'))
from bake_limestone_pbr import channels
from bake_cliff_roughness import limestone_cavity_roughness


class LimestonePbrTest(unittest.TestCase):
    def test_flat_stone_does_not_acquire_false_relief_or_occlusion(self):
        normal, ao = channels(np.full((256,256), .5, np.float32))
        np.testing.assert_allclose(normal, np.broadcast_to([0,0,1], normal.shape), atol=1e-6)
        np.testing.assert_allclose(ao, 1, atol=1e-6)

    def test_joints_have_relief_and_cavity_shading(self):
        y,x = np.mgrid[:256,:256]
        lum = (.55+.15*np.cos(x*np.pi/16)+.1*np.cos(y*np.pi/24)).astype(np.float32)
        normal, ao = channels(lum)
        np.testing.assert_allclose(np.linalg.norm(normal,axis=-1),1,atol=1e-6)
        self.assertGreater(normal[...,:2].std(),.02)
        self.assertLess(ao.min(),.8)
        self.assertGreater(ao.max(),.98)

    def test_dark_or_occluded_patches_cannot_keep_polished_highlights(self):
        result = limestone_cavity_roughness(np.full(3,.6), np.array([.2,.8,.8]), np.array([1,1,.5]))
        np.testing.assert_allclose(result,[.96,.6,.96])


if __name__ == '__main__':unittest.main()
