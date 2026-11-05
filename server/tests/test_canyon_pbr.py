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
        np.testing.assert_allclose(r,.86,atol=1e-6)
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
        self.assertGreaterEqual(a.min(),.759)
        self.assertGreater(a.max()-a.min(),.001)

    def test_pigment_brightness_does_not_dominate_relief(self):
        y,x=np.mgrid[:512,:512]
        structure=np.cos(x*np.pi/32)*.12+np.sin(y*np.pi/24)*.05
        dark=pbr.channels((.3*(1+structure)).astype(np.float32))[0]
        pale=pbr.channels((.6*(1+structure)).astype(np.float32))[0]
        ratio=pale[:,:,:2].std()/dark[:,:,:2].std()
        self.assertGreater(ratio,.8)
        self.assertLess(ratio,1.4, 'same relief on pale rock must not double in strength')

    def test_face_filter_keeps_sharp_fractures_without_amplifying_height(self):
        face=np.full((32,256),-.15,np.float32)
        face[:,128:]=.15
        result=pbr.preserve_face_edges(face,1)
        rounded=pbr.blur(face,4)
        # Preserve the step rather than spreading its normal response over a
        # broad rounded shoulder. No overshoot or change in plateau height.
        edge=result[16,128]-result[16,127]
        self.assertGreater(edge,4*(rounded[16,128]-rounded[16,127]))
        self.assertLessEqual(result.max(),.150001)
        self.assertGreaterEqual(result.min(),-.150001)
        self.assertTrue(np.isfinite(result).all())

    def test_main_normal_prefers_broad_forms_over_fine_grain(self):
        x=np.arange(4096,dtype=np.float32)[None,:]
        response=[]
        for period in (8,128):
            lum=np.repeat(.5+.1*np.sin(x*2*np.pi/period),32,axis=0)
            normal=pbr.channels(lum)[0]
            response.append(normal[:,:,0].std())
        self.assertLess(response[0],response[1]*.25)

    def test_stock_detail_pair_is_local_pinned_and_valid(self):
        import hashlib
        mats=json.loads((pbr.OUT.parent/'cliff.materials.json').read_text())
        stage=mats['bne_cliff_canyon_sat']['Stages'][0]
        self.assertEqual(stage['normalDetailMapUseUV'],0)
        self.assertNotIn('detailNormalMapUseUV',stage)
        self.assertEqual(stage['detailMapUseUV'],0)
        self.assertAlmostEqual(stage['detailScale'][1]/stage['detailScale'][0],2)
        manifest=json.loads((pbr.OUT/'canyon_detail_dirt_rocky_v1.sources.json').read_text())
        for key in ('detailMap','detailNormalMap'):
            self.assertTrue(stage[key].startswith('/levels/beamng_earth/'))
            file=pbr.OUT/Path(stage[key]).name
            blob=file.read_bytes()
            record=next(r for r in manifest['files'] if r['file']==file.name)
            self.assertEqual(hashlib.sha256(blob).hexdigest(),record['sha256'])
            if key=='detailNormalMap':
                import struct
                self.assertEqual(blob[:4], b'DDS ')
                height,width=struct.unpack_from('<II',blob,12)
                self.assertEqual((width,height),(1024,1024))
                self.assertGreater(struct.unpack_from('<I',blob,28)[0],1)
            else:
                with Image.open(file) as im:
                    self.assertEqual(im.mode,'RGB')
                    self.assertEqual(im.size,(1024,1024))
                    data=np.asarray(im,dtype=np.float32)/255
                np.testing.assert_array_equal(data[:,:,0],data[:,:,1])
                np.testing.assert_array_equal(data[:,:,1],data[:,:,2])
                self.assertAlmostEqual(float(data.mean()),.5,places=3)
                self.assertGreater(data.std(),.05)
                self.assertLess(data.std(),.08)

    def test_detail_pbr_preserves_ao_and_roughness_baselines(self):
        from bake_canyon_detail import detail_pbr
        ao,rough=detail_pbr(np.array([0,.5,1]),np.array([.2,.5,.8]))
        self.assertTrue(np.all(2*ao-1<=0), 'AO detail must never erase main-map cavities')
        self.assertEqual(2*ao[-1]-1,0)
        self.assertAlmostEqual(float(rough.mean()),.5)
        self.assertTrue(np.all(np.diff(rough)>0))
        mats=json.loads((pbr.OUT.parent/'cliff.materials.json').read_text())
        for name in ('bne_cliff_canyon_sat','bne_cliff_canyon_sat_terrain'):
            stage=mats[name]['Stages'][0]
            for key in ('ambientOcclusionDetailMap','roughnessDetailMap'):
                with Image.open(pbr.OUT/Path(stage[key]).name) as im:
                    self.assertEqual(im.size,(1024,1024))
                    values=np.asarray(im,dtype=np.float32)/255
                if key=='ambientOcclusionDetailMap':
                    self.assertLessEqual(values.max(),.5)
                else:
                    self.assertAlmostEqual(float(values.mean()),.5,delta=1/510)  # Half an 8-bit quantization step.
            self.assertEqual(stage['detailAoMapStrength'],pbr.DETAIL_AO_STRENGTH)
            self.assertEqual(stage['detailRoughnessMapStrength'],pbr.DETAIL_ROUGHNESS_STRENGTH)

    def test_deep_recesses_have_flatter_floors_without_terracing_faces(self):
        profile=np.linspace(-.4,.4,801,dtype=np.float32)
        shaped=pbr.flatten_deep_recesses(profile)
        np.testing.assert_allclose(shaped[profile>=-.2],profile[profile>=-.2])
        slope=np.diff(shaped)/np.diff(profile)
        np.testing.assert_allclose(slope[profile[:-1]<-.21],.15,atol=3e-5)
        self.assertTrue(np.all(slope>0), 'no inverted slopes or quantized terraces')
        self.assertLessEqual(np.abs(shaped).max(),np.abs(profile).max())

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
