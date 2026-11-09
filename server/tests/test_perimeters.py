"""Global facility perimeter geometry and stream regressions, no geographic allowlist."""
import json
import math
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'server'))
import geodesy
import mapgeom
import perimeters as p
import overture


class GlobalPerimeterTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(41.15,-104.85,0.)
        self.box = (-100.,-100.,200.,200.)

    def feature(self, rings=None, tags=None, record='w1@1', kind=mapgeom.POLYGON, fid='fixture'):
        if rings is None:
            rings = [np.array([[0.,0.],[40.,0.],[40.,40.],[0.,40.],[0.,0.]])]
        geo = []
        for ring in rings:
            ring = np.asarray(ring,float)
            la,lo,_ = self.frame.enu_to_geodetic(ring[:,0],ring[:,1],np.zeros(len(ring)))
            geo.append(np.column_stack((lo,la)))
        feats = mapgeom.build('perimeter_areas',np.array([fid]),[(kind,[geo])])
        feats.attrs = {'tags':np.array([json.dumps(tags if tags is not None else {'landuse':'industrial'})]),
                       'src_record':np.array([record]),'src_dataset':np.array(['OpenStreetMap']), 'class':np.array([''])}
        feats.clipped = np.array([False])
        return feats

    def rows(self,features=None,box=None,roads=(),infra=None,elev=None):
        if features is None: features = [self.feature()]
        return p.rows(self.frame,lambda k:features,lambda k:infra,box or self.box,
                      elev or (lambda la,lo:np.full(len(la),10.)),roads)

    def test_all_area_tags_global_default_on(self):
        for tags in ({'landuse':'industrial'},{'landuse':'military'},{'military':'base'},
                     {'aeroway':'aerodrome'},{'landuse':'quarry'},{'industrial':'mine'}):
            with self.subTest(tags=tags): self.assertGreater(len(self.rows([self.feature(tags=tags)])),30)
        with patch.dict(os.environ,{'BNE_PERIMETERS':'0'}): self.assertEqual(self.rows(),[])

    def test_points_subfeatures_and_non_osm_rejected(self):
        for tags in ({'aeroway':'runway'},{'aeroway':'taxiway'},{'aeroway':'apron'},
                     {'military':'helipad'},{'building':'industrial','landuse':'industrial'},
                     {'industrial':'mine','disused':'yes'}):
            self.assertEqual(self.rows([self.feature(tags=tags)]),[])
        self.assertEqual(self.rows([self.feature(kind=mapgeom.POINT)]),[])
        self.assertEqual(self.rows([self.feature(record='invented')]),[])
        self.assertIsNone(p.classify({'aeroway':'runway'},'airport'))
        self.assertIsNone(p.classify({'military':'helipad'},'military'))
        self.assertIsNone(p.classify({'industrial':'mine','disused':'yes'},'mine'))

    def test_complete_layers_do_not_clip_or_change_existing_landuse(self):
        self.assertTrue(overture.LAYERS['land_use']['clip'])
        for layer in p.LAYERS:
            self.assertFalse(overture.LAYERS[layer]['clip'])
            self.assertIn('src_record',overture.LAYERS[layer]['text'])
        feat = self.feature(); feat.clipped[:] = True
        self.assertEqual(self.rows([feat]),[])

    def test_holes_remain_open(self):
        outer = np.array([[0,0],[40,0],[40,40],[0,40],[0,0]])
        hole = np.array([[10,10],[20,10],[20,20],[10,20],[10,10]])
        self.assertEqual(self.rows([self.feature([outer,hole])]), self.rows([self.feature([outer])]))

    def test_multiple_outer_parts(self):
        feat = self.feature()
        ring = feat.parts(0)[0][0]
        second = ring+np.array([.001,0.])
        feat2 = mapgeom.build(feat.layer,feat.ids,[(mapgeom.POLYGON,[[ring],[second]])])
        feat2.attrs=feat.attrs
        self.assertGreater(len(self.rows([feat2])),len(self.rows([feat])))

    def test_spacing_closure_orientation_and_corners(self):
        rows = self.rows()
        lengths = [math.dist(r[1:3],r[4:6]) for r in rows]
        self.assertTrue(all(3.8<v<4.2 for v in lengths))
        endpoints = [tuple(round(v,4) for v in xyz) for r in rows for xyz in (r[1:3],r[4:6])]
        from collections import Counter
        self.assertTrue(all(n==2 for n in Counter(endpoints).values()))
        self.assertTrue(all(abs(r[1]-r[4])<.001 or abs(r[2]-r[5])<.001 for r in rows))

    def test_canonical_geometry_reversal_and_start(self):
        ring = np.array([[0,0],[40,0],[40,40],[0,40],[0,0]])
        a = self.rows([self.feature([ring])])
        b = self.rows([self.feature([ring[::-1]])])
        self.assertEqual(a,b)
        np.testing.assert_equal(p.canonical_ring(ring),p.canonical_ring(np.vstack((ring[2:-1],ring[:3]))))

    def test_long_edge_work_is_local_and_residual_height_not_scaled(self):
        got = list(p.sections(np.array([-100000.,0.]),np.array([100000.,0.]),box=(-10,-10,10,10)))
        self.assertLess(len(got),7)
        short = list(p.sections(np.array([0.,0.]),np.array([.5,0.])))
        self.assertAlmostEqual(np.linalg.norm(short[0][1]-short[0][0]),.5)

    def test_road_opening_parallel_clearance_and_gate(self):
        road = (np.array([[20.,-20.],[20.,60.]]),3.)
        rows = self.rows(roads=[road])
        self.assertTrue(rows)
        self.assertTrue(all(not (min(r[1],r[4])<23.62-1e-3 and max(r[1],r[4])>16.38+1e-3 and abs(r[2]-r[5])<.001) for r in rows))
        gate = self.feature([np.array([[0.,20.]])],tags={'barrier':'gate'},kind=mapgeom.POINT)
        self.assertLess(sum(math.dist(r[1:3],r[4:6]) for r in self.rows(infra=gate)),
                        sum(math.dist(r[1:3],r[4:6]) for r in self.rows()))

    def test_real_road_tuple(self):
        from types import SimpleNamespace
        road = ('road',SimpleNamespace(width=6.),np.array([[20.,-20.,0.],[20.,60.,0.]]),0.)
        self.assertTrue(self.rows(roads=[road]))

    def test_overlap_and_cell_duplicates(self):
        f = self.feature()
        self.assertEqual(self.rows([f,f]),self.rows([f]))
        same = self.feature(record='r2@3',fid='second')
        self.assertEqual(len(self.rows([f,same])),len(self.rows([f])))
        ring=np.array([[0,0],[20,0],[40,0],[40,40],[0,40],[0,0]])
        other=self.feature([ring],record='r2@3',fid='second')
        self.assertEqual(len(self.rows([f,other])),len(self.rows([f])))

    def test_tile_boundaries_and_reload_stable(self):
        rows=self.rows()
        left=self.rows(box=(-100,-100,20,200))
        right=self.rows(box=(20,-100,200,200))
        self.assertEqual(sorted(left+right),sorted(rows))
        self.assertEqual(rows,self.rows())
        self.assertEqual(self.rows(box=(1000,1000,1200,1200)),[])

    def test_ids_and_module_phase_survive_anchor_change(self):
        feature=self.feature()
        original={r[7] for r in self.rows([feature])}
        self.frame=geodesy.EnuFrame(41.151,-104.851,0.)
        shifted={r[7] for r in self.rows([feature],box=(-1000,-1000,1000,1000))}
        self.assertEqual(original,shifted)

    def test_local_road_cuts_do_not_shift_distant_panels(self):
        a,b=np.array([0.,0.]),np.array([100.,0.])
        left=list(p.sections(a,b,box=(0,-1,20,1)))
        cut=list(p.sections(a,b,cuts=[(80,90)],box=(0,-1,20,1)))
        np.testing.assert_allclose(left,cut)

    def test_final_terrain_slope_and_bad_samples(self):
        def elevation(la,lo):
            e,n,_=self.frame.geodetic_to_enu(la,lo,np.zeros(len(la)))
            return 10.+np.asarray(e)*.05
        rows=self.rows(elev=elevation)
        self.assertTrue(any(abs(r[3]-r[6])>.1 for r in rows))
        self.assertEqual(self.rows(elev=lambda la,lo:np.full(len(la),np.nan)),[])

    def test_curved_terrain_adapts_instead_of_floating(self):
        def hill(la,lo):
            e,n,_=self.frame.geodetic_to_enu(la,lo,np.zeros(len(la)))
            return 10.+.04*np.asarray(e)**2
        self.assertGreater(len(self.rows(elev=hill)),len(self.rows()))

    def test_raw_closed_way_and_reversed_relation_assembly(self):
        import osmarea,io
        self.assertEqual(len(osmarea.outer_rings(ROOT/'probes/warren_full.osm','way','841575049')[0])-1,28)
        xml='<osm><node id="1" lat="0" lon="0"/><node id="2" lat="0" lon="1"/><node id="3" lat="1" lon="0"/>'
        xml+='<way id="a"><nd ref="1"/><nd ref="2"/></way><way id="b"><nd ref="1"/><nd ref="3"/><nd ref="2"/></way>'
        xml+='<relation id="r"><member type="way" ref="a" role="outer"/><member type="way" ref="b" role="outer"/><member type="way" ref="missing" role="inner"/></relation></osm>'
        self.assertEqual(len(osmarea.outer_rings(io.StringIO(xml),'relation','r')[0]),4)
        with self.assertRaises(ValueError):
            osmarea.outer_rings(io.StringIO(xml.replace('<nd ref="3"/><nd ref="2"/>','<nd ref="3"/>')),'relation','r')

    def test_real_cheyenne_osm_relations(self):
        import osmarea
        for filename,identifier,tags in [('warren_boundary_full.osm','252043',{'landuse':'military'}),
                ('airport_full.osm','6830118',{'aeroway':'aerodrome'}),
                ('thornton_quarry_full.osm','20071516',{'landuse':'quarry'}),
                ('thornton_industrial_full.osm','5525237',{'landuse':'industrial'})]:
            rings=osmarea.outer_rings(ROOT/'probes'/filename,'relation',identifier)
            feat=mapgeom.build('perimeter_areas',np.array([identifier]),[(mapgeom.POLYGON,[[np.array(r)] for r in rings])])
            feat.attrs={'src_record':np.array(['r'+identifier+'@0']),'tags':np.array([json.dumps(tags)])}
            self.assertEqual(p.osm_id(feat,0),'relation/'+identifier)
            self.assertTrue(all(p.closed_ring(part[0]) for part in feat.parts(0)))
            previous=self.frame
            self.frame=geodesy.EnuFrame(rings[0][0][1],rings[0][0][0],0.)
            try: self.assertTrue(self.rows([feat],box=(-60,-60,60,60)))
            finally: self.frame=previous

    def test_geoparquet_ingestion_complete_geometry_and_cache_version(self):
        import tempfile
        import mapdata
        import mapfixtures as mf
        features=mf.features()
        # Synthetic source, explicit tag must pass even when normalized class differs.
        features[('base','land_use')][0]['source_tags']={'landuse':'industrial'}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(mf,'features',return_value=features):
                files,_=mf.release(directory,extra_far_file=False)
            md=mapdata.MapData(directory,mf.fetcher(mf.Transport(files)),mf.RELEASE)
            try:
                cell=md.cell(mf.CELL,layers=p.LAYERS,kind='demand')
                feat=cell.layers['perimeter_areas']
                self.assertEqual(len(feat),1)
                self.assertFalse(feat.clipped.any())
                lon0,lat0,lon1,lat1=mf.CELL.bounds()
                self.assertLess(feat.lonlat[:,0].min(),lon0)
                self.assertGreater(feat.lonlat[:,0].max(),lon1)
                self.assertIn('perimeter_areas.2',md.path(mf.CELL,'perimeter_areas'))
                # Source snapshots and normal land-use cache keep their existing versions.
                self.assertNotEqual(md.path(mf.CELL,'land_use'),md.path(mf.CELL,'perimeter_areas'))
            finally: md.close()

    def test_source_failure_preserves_roads_and_marks_tile_incomplete(self):
        import revisit
        from types import SimpleNamespace
        md=SimpleNamespace(cell=lambda *a,**kw:SimpleNamespace(layers={}))
        before=revisit.thread_faults()
        self.assertEqual(p.tile_rows(self.frame,md,None,self.box,None,()),[])
        self.assertGreater(revisit.thread_faults(),before)

    def test_stale_revisit_cache_invalidated_by_code_and_switch(self):
        import revisit
        self.assertIn('perimeters.py',revisit.source_closure(str(ROOT/'server')))
        self.assertIn('fencecrossings.py',revisit.source_closure(str(ROOT/'server')))
        self.assertIn('fencebuildings.py',revisit.source_closure(str(ROOT/'server')))
        self.assertIn('facilityperimeters.py',revisit.source_closure(str(ROOT/'server')))
        with patch.object(revisit,'_code',[]),patch.dict(os.environ,{'BNE_PERIMETERS':'0'}):
            disabled=revisit.key('roadsg','test-cell')
        with patch.object(revisit,'_code',[]),patch.dict(os.environ,{'BNE_PERIMETERS':'1'}):
            enabled=revisit.key('roadsg','test-cell')
        self.assertNotEqual(disabled,enabled)

    def test_serialized_endpoint_rows_reach_dedicated_client_field(self):
        import tempfile,roads
        perimeter_rows=self.rows()
        props=perimeter_rows+[['chainlink',1.,2.,3.,0.,1.,1.76,0.]]
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BNE_PERIMETER_DIAGNOSTICS':'1'}):
            with self.assertLogs('earthd.roads',level='INFO') as logs:
                path,_=roads.write_tile(directory,'/x',1,0,0,[],props=props)
            data=json.loads((Path(directory)/Path(path).name).read_text())
            self.assertEqual(data['perimeters'],[r[1:] for r in perimeter_rows])
            self.assertEqual(data['props']['groups'],['chainlink'])
            self.assertEqual(data['perimeters'][0][7],'way/1')
            self.assertTrue(any('serialized=' in message for message in logs.output))


if __name__ == '__main__': unittest.main()
