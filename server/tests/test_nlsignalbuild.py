from pathlib import Path
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import geodesy
import nlsignalassembly as assembly
import nlsignalbuild as builder


def graph():
    return dict(nodes=dict(j=[0.,0.,0.,3.],w=[-60.,0.,0.,3.],e=[60.,0.,0.,3.],n=[0.,60.,0.,3.]),
                edges=[['w','j',1,2,0,22.,1.,3.3],['j','e',1,2,0,22.,1.,3.3],['j','n',0,1,1,13.,1.,3.3]],signals=[])


def surface():
    parts=[]
    for points in ([[-60.,-3.3,0.],[60.,-3.3,0.],[60.,3.3,0.],[-60.,3.3,0.]],
                   [[-3.3,-60.,0.],[3.3,-60.,0.],[3.3,60.,0.],[-3.3,60.,0.]]):
        parts.append(dict(kind='road',positions=np.array(points),triangles=np.array([[0,1,2],[0,2,3]])))
    return assembly.make_scene(parts,np.zeros((3,3),np.uint16),0.,100.,(50.,50.),[[-100.,-100.,100.,100.]],square=100.)


class LiveNativeBuild(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.vfs='/temp/native-test'
        self.frame=geodesy.EnuFrame(51.92,4.46,0.)
        self.source=SimpleNamespace(snapshot=lambda bounds:dict(built='known',complete=True,records=[
            dict(kind='signal',id='123',geometry=[[4.46,51.92]],tags={})]))
        self.catalog=json.loads((Path(__file__).resolve().parents[1]/'data/groenpol_components.json').read_text())
        self.g=graph()

    def build(self,previous=None):
        (self.root/'graph.json').write_text(json.dumps(self.g))
        with patch.object(builder.nlsignalinputs,'session_scene',return_value=surface()):
            return builder.build(str(self.root),self.vfs,1,self.frame,{},(0.,0.),[],
                                 self.vfs+'/graph.json',self.source,self.catalog,previous=previous)

    def test_real_native_pipeline_emits_complete_source_backed_mounts(self):
        result=self.build()
        self.assertEqual(len(result['owners']),2)
        self.assertTrue(result['parts'])
        self.assertEqual(len({p['key'] for p in result['parts']}),len(result['parts']))
        heads=[p for p in result['parts'] if p['role']=='head' and not p['repeat']]
        self.assertEqual(len(heads),3)
        self.assertTrue(all('300' in p['variant'] and 'forest' in p for p in heads))
        written=json.loads((self.root/Path(result['graphPath']).name).read_text())
        self.assertTrue(written['nlNative']);self.assertEqual(len(written['signals']),1)
        for app in written['signals'][0]['app']:
            self.assertLess(app[1]*app[4]+app[2]*app[5],-5.,'inferred stop is before its primary heads')

    def test_pending_surface_reuses_previous_complete_junction(self):
        previous=self.build()['groups']
        with patch.object(assembly,'junction',return_value=dict(ready=False,retryable=True,reason='surface_tiles_pending')):
            result=self.build(previous)
        self.assertEqual(result['groups'],previous)
        self.assertTrue(result['statuses'][0]['pending'])

    def test_rejected_native_mount_preserves_stock_controller_geometry(self):
        original=dict(j='j',kind='signal',app=[['w',-12.,0.,0.,1.,0.,1,2.,3.,4.,90.]])
        self.g['signals']=[original]
        with patch.object(assembly,'junction',return_value=dict(ready=False,retryable=False,reason='lane_surface_unavailable')):
            result=self.build()
        written=json.loads((self.root/Path(result['graphPath']).name).read_text())
        self.assertEqual(written['signals'],[original])
        self.assertEqual(json.loads(result['signalSignature']),[original])
        self.assertEqual(result['owners'],[])

    def test_absent_public_data_does_not_guess_or_read_surfaces(self):
        self.source.snapshot=lambda bounds:dict(built=None,complete=False,records=[])
        (self.root/'graph.json').write_text(json.dumps(self.g))
        with patch.object(builder.nlsignalinputs,'session_scene',side_effect=AssertionError('no data, no scene work')):
            result=builder.build(str(self.root),self.vfs,1,self.frame,{},(0.,0.),[],
                                 self.vfs+'/graph.json',self.source,self.catalog)
        self.assertTrue(result['pending'])


if __name__=='__main__':unittest.main()
