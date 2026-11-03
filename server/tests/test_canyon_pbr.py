"""Near canyon channels align to the accepted diffuse without amplifying bands."""
import json
import sys
from pathlib import Path
import unittest
import numpy as np
from PIL import Image
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools'))
import bake_canyon_near_pbr as pbr

class CanyonPbrTest(unittest.TestCase):
    def test_flat_surface_stays_flat_and_nonmetallic(self):
        n,r,a=pbr.channels(np.full((256,256),.5,np.float32))
        np.testing.assert_allclose(n,np.broadcast_to([0,0,1],n.shape),atol=1e-6)
        np.testing.assert_allclose(r,.89,atol=1e-6)
        np.testing.assert_allclose(a,1,atol=1e-6)

    def test_details_drive_aligned_relief_and_subtle_cavities(self):
        y,x=np.mgrid[:256,:256]
        lum=(.5+.1*np.cos(x*np.pi/8)+.04*np.sin(y*np.pi/5)).astype(np.float32)
        n,r,a=pbr.channels(lum)
        np.testing.assert_allclose(np.linalg.norm(n,axis=2),1,atol=1e-6)
        self.assertGreater(n[:,:,0].std(),.001)
        self.assertGreater(n[:,:,1].std(),.001)
        # +X derivative of a peak becomes a matching positive X normal downhill.
        self.assertGreater(n[0,4,0],0)
        self.assertGreater(r.std(),.001)
        self.assertGreaterEqual(a.min(),.879)
        self.assertGreater(a.max()-a.min(),.001)

    def test_material_maps_are_4k_and_use_diffuse_uvs(self):
        mats=json.loads((pbr.OUT.parent/'cliff.materials.json').read_text())
        stage=mats['bne_cliff_canyon_sat']['Stages'][0]
        for key in ('normalMap','roughnessMap','ambientOcclusionMap'):
            self.assertEqual(stage[key+'UseUV'],0)
            with Image.open(pbr.OUT/Path(stage[key]).name) as im:
                self.assertEqual(im.size,(4096,4096))
        self.assertEqual(stage['metallicFactor'],0)
        self.assertNotEqual(stage['normalMap'],mats['bne_far_cliff_canyon_blend']['Stages'][0]['normalMap'])

if __name__=='__main__':unittest.main()
