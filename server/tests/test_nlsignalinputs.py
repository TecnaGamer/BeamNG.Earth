from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import json
import pickle
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import heightmap
import nlsignalinputs as inputs


class ExactSurfaceInputs(unittest.TestCase):
    def test_session_reads_drawn_shapes_and_rejects_replaced_content(self):
        import cdae,geotiles,geodesy,blockworker
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);vfs='/temp/exact';aid=1
            frame=geodesy.EnuFrame(51.92,4.46,0.)
            gx,gy=geotiles.tile_of(frame.lat0,frame.lon0);key=geotiles.key(gx,gy)
            skin_names=blockworker.geo_names(key,aid)
            content_names=blockworker.geo_names(key)
            q=np.zeros((3,3),np.uint16)
            heightmap.write_ter(str(root/'g.ter'),q,np.zeros((3,3),np.uint8),['test'])
            p=np.array([[-4.,-4.,0.],[4.,-4.,0.],[4.,4.,0.],[-4.,4.,0.]],np.float32)
            cdae.write(str(root/skin_names['road']),p,np.array([[0,1,2],[0,2,3]]),close=False)
            (root/skin_names['json']).write_text(json.dumps(dict(anchorId=aid,key=key,
                road=dict(shape=vfs+'/'+skin_names['road'],pos=[1,2,3]))))
            (root/content_names['json']).write_text(json.dumps(dict(h='same')))
            (root/content_names['inputs']).write_bytes(pickle.dumps(dict(h='same',ground_cover=[])))
            args=(root,vfs,aid,frame,dict(ter='g.ter',zBase=0.,maxHeight=100.),(0.,0.),[(gx,gy,dict(h='same'),{})])
            with patch.object(inputs,'make_scene',return_value='scene')as make:
                self.assertEqual(inputs.session_scene(*args),'scene')
                self.assertEqual(make.call_args.args[0][0]['kind'],'road')
                self.assertEqual(make.call_args.args[0][0]['origin'],[1,2,3])
                np.testing.assert_allclose(make.call_args.kwargs['coverage_quads'][0],
                    np.asarray(geotiles.quad(frame,gx,gy))[:,:2].reshape(8))
            (root/content_names['json']).write_text(json.dumps(dict(h='changed')))
            with self.assertRaises(inputs.SurfacePending):inputs.session_scene(*args)

    def test_painted_carriageway_is_present_without_visible_road_triangles(self):
        import geodesy
        frame=geodesy.EnuFrame(51.92,4.46,0.)
        chunk=dict(m='asphalt',w=6.5,tl=10.,p=0,n=[[0.,-20.,50.],[0.,20.,50.]])
        part=inputs.ground_footprints([chunk],frame,frame)
        self.assertEqual(part['kind'],'ground_road')
        self.assertGreater(len(part['triangles']),0)
        self.assertAlmostEqual(float(part['positions'][:,0].max()),3.25,places=4)
        # The source profile is deliberately at another height: the committed
        # terrain, not this envelope's placeholder Z, owns the painted road.
        from nlsignalassembly import make_scene,plans
        scene=make_scene([part],np.zeros((41,41),np.uint16),2.,100.,(0.,0.),
                         [[-30.,-30.,30.,30.]],square=1.)
        request=dict(stop=[0.,0.,2.],travel=[0.,1.],lanes=2,opposite=0,radius=3.25,
                     overhead=True,before=3.,after=0.)
        result=plans(scene,[request])[0]
        self.assertTrue(result['ready'],result)

    def test_missing_painted_road_generation_stays_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            import blockworker
            with self.assertRaises(inputs.SurfacePending):
                blockworker._skin_inputs('missing',folder,
                    lambda: (_ for _ in ()).throw(inputs.SurfacePending('pending')))

    def test_terrain_uses_quantised_samples_and_actual_holes(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'ground.ter'
            q=np.arange(25,dtype=np.uint16).reshape(5,5)
            mask=np.zeros((5,5),np.uint8);mask[2,3]=heightmap.TER_HOLE
            heightmap.write_ter(str(p),q,mask,['test'])
            got,holes=inputs.terrain(p)
            np.testing.assert_array_equal(got,q)
            self.assertTrue(holes[2,3]);self.assertFalse(holes[2,2])
            self.assertTrue(holes[0,:].all())

    def test_short_or_oversized_terrain_never_reaches_native(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'ground.ter'
            for raw in (b'',struct.pack('<BI',heightmap.TER_VERSION,5000),
                        struct.pack('<BI',heightmap.TER_VERSION,5)+b'\0'*8):
                p.write_bytes(raw)
                with self.assertRaises(ValueError):inputs.terrain(p)

    def test_session_paths_cannot_read_a_different_session(self):
        with tempfile.TemporaryDirectory() as folder:
            for path in ('/other/file.cdae','/temp/test/../private','C:/outside.cdae'):
                with self.assertRaises(ValueError):inputs.owned(folder,'/temp/test',path)
            self.assertEqual(inputs.owned(folder,'/temp/test','/temp/test/roads.cdae'),Path(folder).resolve()/'roads.cdae')


if __name__=='__main__':unittest.main()
