"""Entrance-only, source-stable warning signs matching the user's pole layouts."""
from pathlib import Path
import json
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import exitsigns
import mergesigns
import signcontext
import signstream
import geodesy
import mapgeom
import roads
from test_roads import flat,FRAME,LAT,LON,features,rules


def interchange(entrance=False, reverse=False):
    ramp = [(300, 100), (200, 80), (150, 20), (-20, 10)] if entrance else [
        (300, 100), (400, 80), (450, 20), (650, 10)]
    direction = -1. if entrance else 1.
    f = features([[(-600, 100), (200, 100)], [(200, 100), (300, 100)], [(300, 100), (850, 100)],
                  ramp[::-1] if reverse else ramp], ['motorway']*4, ids=['upstream', 'before', 'after', 'ramp'])
    f.rules['oneway'] = rules([(0, 1.), (1, 1.), (2, 1.), (3, -direction if reverse else direction)], 'float')
    f.rules['subclass'] = rules([(3, 'link')])
    return f

def relocate(f, lat, lon):
    f.lonlat[:, 0] = lon+(f.lonlat[:, 0]-LON)*np.cos(np.radians(LAT))/np.cos(np.radians(lat))
    f.lonlat[:, 1] += lat-LAT
    return geodesy.EnuFrame(lat, lon, 0.)

def subset(feats, indices):
    out = mapgeom.build('roads', feats.ids[indices], [(int(feats.kind[i]), feats.parts(i)) for i in indices])
    out.attrs = {k: v[indices].copy() for k, v in feats.attrs.items()}
    for name, table in feats.rules.items():
        rows = np.concatenate([table.rows(i) for i in indices])
        owner = np.concatenate([np.full(len(table.rows(i)), j, np.int32) for j, i in enumerate(indices)])
        out.rules[name] = mapgeom.RuleTable(owner, table.value[rows].copy(),
                                           table.start[rows].copy(), table.end[rows].copy())
    return out

def fixture(entry=True,reverse=False):
    f=interchange(entry,reverse)
    frame=relocate(f,42.,-85.)
    return frame,f


class PlacementTest(unittest.TestCase):
    def test_entrance_is_upstream_between_pavements_facing_freeway(self):
        frame,f=fixture()
        plans=mergesigns.plans(frame,[f])
        self.assertEqual(len(plans),1)
        p=plans[0]
        self.assertGreater(p['travel'][0],.99)
        self.assertEqual(p['side'],1)
        self.assertEqual(p['state'],'MI')
        self.assertLess(p['xy'][0],280.)
        self.assertGreater(p['xy'][1],20.)
        self.assertLess(p['xy'][1],100.)
        rows=mergesigns._rows(frame,f,None,None,False)
        self.assertTrue(exitsigns._clear(p['xy'],mergesigns.SPAN/2+mergesigns.CLEARANCE,
                                        [exitsigns._outline(r[2],r[3]) for r in rows]))

    def test_exit_gets_no_merge_warning(self):
        frame,f=fixture(False)
        self.assertEqual(mergesigns.plans(frame,[f]),[])

    def test_reversed_osm_vertex_order_preserves_location_and_facing(self):
        frame,a=fixture();_,b=fixture(reverse=True)
        x,y=mergesigns.plans(frame,[a])[0],mergesigns.plans(frame,[b])[0]
        np.testing.assert_allclose(x['xy'],y['xy'],atol=.03)
        np.testing.assert_allclose(x['travel'],y['travel'],atol=1e-6)

    def test_left_entrance_mirrors_symbol(self):
        frame,f=fixture()
        # Mirror the complete source relative to the mainline (local y~100).
        la,lo,_=frame.enu_to_geodetic(0.,100.,0.)
        f.lonlat[:,1]=2*la-f.lonlat[:,1]
        p=mergesigns.plans(frame,[f])[0]
        self.assertEqual(p['side'],-1)
        self.assertGreater(p['travel'][0],.99)

    def test_duplicate_cells_and_neighbor_context_have_one_owner(self):
        frame,f=fixture();a=subset(f,[0,1,3]);b=subset(f,[1,2,3])
        context=signcontext.Context(lambda key:f)
        first=mergesigns.plans(frame,[a],context=context)[0]
        for cells in ([b],[a,b],[b,a,a]):
            got=mergesigns.plans(frame,cells,context=context)
            self.assertEqual(len(got),1)
            np.testing.assert_array_equal(first['xy'],got[0]['xy'])
        xy=first['xy']
        left=mergesigns.place(frame,[a],(xy[0]-100,xy[1]-100,xy[0],xy[1]+100),flat,context=context)
        right=mergesigns.place(frame,[b],(xy[0],xy[1]-100,xy[0]+100,xy[1]+100),flat,context=context)
        self.assertEqual(len(left)+len(right),1)

    def test_no_fallback_onto_pavement_or_a_bridge(self):
        frame,f=fixture()
        with patch.object(exitsigns,'_clear',return_value=False):
            self.assertEqual(mergesigns._place_source(frame,f,None,None,False),[])
        p=mergesigns.plans(frame,[f])[0]['xy']
        bridge=dict(deck=True,w=8.,n=[[p[0]-10,p[1],15],[p[0]+10,p[1],15]])
        self.assertEqual(mergesigns.place(frame,[f],(0,0,256,256),flat,[bridge]),[])

    def test_post_bases_follow_sloped_ground(self):
        frame,f=fixture()
        def slope(la,lo):
            x,y,_=frame.geodetic_to_enu(la,lo,0.)
            return 100.+y*.1
        sign=mergesigns.place(frame,[f],(0,0,256,256),slope)[0]
        self.assertGreater(abs(sign['bases'][0]-sign['bases'][1]),.09)
        self.assertGreaterEqual(sign['bottom'],max(sign['bases'])+mergesigns.BOTTOM)


class ModelTest(unittest.TestCase):
    def test_requested_post_styles_and_fallback(self):
        self.assertEqual(mergesigns.post_offsets('MI'),(-.51,.51))
        for state in ('IN','OH','KY','TN','AZ','OK','KS'):
            self.assertEqual(mergesigns.post_offsets(state),(-.38,.38))
        for state in ('CA','WI','NY','','XX'):
            self.assertEqual(mergesigns.post_offsets(state),(0.,))

    def test_no_overlapping_face_layers_and_correct_colored_area(self):
        for side in (1,-1):
            for state in ('MI','IN','CA'):
                sign=dict(centre=[0.,0.,0.],travel=np.array([0.,1.]),side=side,
                          bottom=1.8,offsets=mergesigns.post_offsets(state),
                          bases=[0.]*len(mergesigns.post_offsets(state)))
                p,t,uv,mats,mi=mergesigns.build([sign],(0,0,0))
                self.assertTrue(np.isfinite(p).all())
                self.assertTrue(np.isfinite(uv).all())
                faces=p[t[mi!=2]]
                np.testing.assert_allclose(faces[:,:,1],-.01,atol=1e-12)
                area=np.linalg.norm(np.cross(faces[:,1]-faces[:,0],faces[:,2]-faces[:,0]),axis=1).sum()/2
                outline=mergesigns._diamond(mergesigns.SIDE)
                expected=abs(np.sum(outline[:,0]*np.roll(outline[:,1],-1)-outline[:,1]*np.roll(outline[:,0],-1)))/2
                self.assertAlmostEqual(area,expected,places=10)
                self.assertGreater(np.count_nonzero(mi==1),15)
                self.assertEqual(mats,mergesigns.MATERIALS)

    def test_serialization_keeps_signs_separate_and_noncolliding(self):
        frame,f=fixture();mesh=mergesigns.build_tile(frame,0,0,[f],flat)
        self.assertIsNotNone(mesh)
        with tempfile.TemporaryDirectory() as out:
            signstream.write_tile(out,'/temp/test',1,0,0,(None,None,mesh))
            data=json.loads(Path(out,'signs_1_0_0.json').read_text())
            self.assertIn('merges',data)
            self.assertIn('merges',signstream.shape_files(1,0,0))
            self.assertNotIn('merges',data.get('colliders',{}))


class PipelineTest(unittest.TestCase):
    def test_worker_and_fallback_match_and_road_geometry_is_unchanged(self):
        import blockworker,earthd
        from test_highway_signs import server
        from test_blockworker import FakeDem,FakeMapData
        from test_roads import LAT,LON
        f=interchange(True)
        md=FakeMapData(f,roads.cells_of_tile(FRAME,0,0)[0])
        with tempfile.TemporaryDirectory() as root:
            import highwaysigns
            md.raw_dir,md.fetcher=root,SimpleNamespace(offline=True)
            md.highway_signs=highwaysigns.Source(md)
            directories=[Path(root,name) for name in ('server','worker','unsigned')]
            for path in directories:path.mkdir()
            earthd.Server.build_signs_tile(server(md,str(directories[0])),1,0,0)
            blockworker._s=SimpleNamespace(md=md,dem=FakeDem(),widths=None,surface=None,airports=None,sea=None,
                                          lock=threading.Lock(),bridges={},anchor=lambda *args:(None,None))
            blockworker.signs_tile(1,LAT,LON,15,0,0,str(directories[1]),'/temp/test')
            for p in directories[0].iterdir():
                self.assertEqual(p.read_bytes(),(directories[1]/p.name).read_bytes(),p.name)
            data=json.loads((directories[0]/'signs_1_0_0.json').read_text())
            self.assertIn('merges',data)
            with patch.object(mergesigns,'build_tile',return_value=None):
                earthd.Server.build_signs_tile(server(md,str(directories[2])),1,0,0)
            unsigned=json.loads((directories[2]/'signs_1_0_0.json').read_text())
            self.assertEqual({k:v for k,v in data.items() if k!='merges'},unsigned)
            for p in directories[2].glob('*.cdae'):
                self.assertEqual(p.read_bytes(),(directories[0]/p.name).read_bytes(),p.name)


if __name__=='__main__':unittest.main()
