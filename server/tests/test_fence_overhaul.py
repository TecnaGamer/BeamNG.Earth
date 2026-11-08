"""Pass contracts, scoped road constraints and ten photographed regression sites."""
import sys,unittest,json
from pathlib import Path
from dataclasses import FrozenInstanceError
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
import shapely
from shapely.geometry import LineString,box,shape,Point
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'server'),str(ROOT/'server/tests'),str(ROOT/'probes')]
import perimeters as p,facilityperimeters as fp,fencenetwork as fn,geodesy
import test_facility_perimeters as fixtures

PHOTOS=[(41.13223,-104.85832),(41.13364,-104.85491),(41.16833,-104.88051),(41.17039,-104.88052),(41.17607,-104.88373),(41.18283,-104.88588),(41.18485,-104.88585),(41.18695,-104.88585),(41.18897,-104.88582),(41.17660,-104.88433)]

class ContractTest(unittest.TestCase):
    def setUp(self):
        self.h=fixtures.FacilityTest();self.h.setUp()
        self.area=self.h.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
    def constraint(self,xy=((-20,40),(120,40)),side='north'):
        return self.h.feature_evidence(xy,identifier='reviewed/road',role='boundary_constraint',targets=['way/1'])|{'properties':dict(id='reviewed/road',role='boundary_constraint',facility_sources=['way/1'],source='fixture reviewed road',reason='reviewed main boundary',confidence='verified',keep_side=side,offset_m=6.)}
    def test_two_pass_reports_keep_manifest_when_terrain_fails(self):
        audit={};rows=p.rows(self.h.frame,lambda key:[self.area],lambda key:None,(-10,-10,110,110),None,
            surface=lambda xy:np.full(len(xy),np.nan),extra_evidence=[],audit=audit)
        self.assertEqual(rows,[])
        self.assertGreater(audit['geographic_network']['approved_paths'],0)
        self.assertEqual(audit['placement_report']['placement_status'],'terrain_failure')
        self.assertTrue(audit['geographic_network']['digest'])
    def test_production_missing_source_raises_for_existing_tracker_retry(self):
        missing=SimpleNamespace(cell=lambda *a,**k:SimpleNamespace(layers={}))
        with self.assertRaises(p.PerimeterPending):p.tile_rows(self.h.frame,missing,None,(-10,-10,110,110),None,[],strict=True)
    def test_curved_road_constraint_follows_source_not_latitude(self):
        line=LineString([[-20,20],[40,60],[120,40]])
        record=dict(geometry=line,properties=dict(id='curve',keep_side='north',offset_m=6.,source='fixture',reason='reviewed'))
        interior,mask,_=fp.constrain_interior(box(0,0,100,100),[record],lambda x:x)
        self.assertFalse(interior.covers(Point(40,50)))
        self.assertTrue(interior.covers(Point(0,50)))
        self.assertGreater(interior.bounds[1],20.)
    def test_reversing_road_keeps_identical_constraint(self):
        line=LineString([[-20,20],[40,60],[120,40]])
        def apply(g):return fp.constrain_interior(box(0,0,100,100),[dict(geometry=g,properties=dict(id='curve',keep_side='north',offset_m=6.,source='fixture',reason='reviewed'))],lambda x:x)[0]
        self.assertLess(apply(line).symmetric_difference(apply(LineString(list(line.coords)[::-1]))).area,1e-7)
    def test_scoped_constraint_retains_independent_southern_enclosure(self):
        fence=self.h.feature_evidence([[20,10],[80,10],[80,25],[20,25],[20,10]])
        fence['properties']['boundary_class']='secured_compound'
        rows=self.h.rows([self.area],[self.constraint(),fence])
        main=[r for r in rows if r[8]=='way/1'];compound=[r for r in rows if r[8]=='physical:way/900']
        self.assertTrue(main and compound)
        self.assertTrue(all(min(r[2],r[5])>=45.99 for r in main))
        self.assertTrue(any(r[2]<25.1 for r in compound))
    def test_constraint_does_not_apply_to_other_facility_identity(self):
        constraint=self.constraint();constraint['properties']['facility_sources']=['way/999']
        baseline=self.h.rows([self.area],[])
        self.assertEqual(baseline,self.h.rows([self.area],[constraint]))
    def test_missing_physical_edge_recovers_supported_full_boundary(self):
        fence=self.h.feature_evidence([[0,0],[100,0],[100,100],[0,100]])
        audit={};rows=self.h.rows([self.area],[fence],audit=audit)
        self.assertTrue(any(abs(r[1])<.01 and abs(r[4])<.01 and r[8]=='way/1' for r in rows))
        self.assertTrue(any(n['recovery'] for n in audit['geographic_network']['nodes']))
    def test_disconnected_physical_sections_use_source_arcs_not_chords(self):
        evidence=[self.h.feature_evidence([[0,0],[40,0]],identifier='way/901'),self.h.feature_evidence([[100,60],[100,100]],identifier='way/902')]
        audit={};rows=self.h.rows([self.area],evidence,audit=audit)
        self.assertTrue(rows)
        for row in rows:
            a,b=np.asarray(row[1:3]),np.asarray(row[4:6]);self.assertTrue(abs(a[0]-b[0])<.01 or abs(a[1]-b[1])<.01)
    def test_geographic_digest_independent_of_terrain_and_tile_window(self):
        one={};two={};self.h.rows([self.area],audit=one)
        p.rows(self.h.frame,lambda key:[self.area],lambda key:None,(0,0,45,100),None,surface=lambda xy:np.full(len(xy),40.),extra_evidence=[],audit=two)
        self.assertEqual(one['geographic_network']['digest'],two['geographic_network']['digest'])
    def test_foreign_facilities_keep_same_general_pipeline(self):
        for lat,lon in ((51.5,-.1),(-33.9,151.2),(35.7,139.7)):
            self.h.frame=self.h.base.frame=geodesy.EnuFrame(lat,lon,0)
            area=self.h.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
            rows=self.h.rows([area],[]);self.assertGreater(len(rows),90)
    def test_concurrent_tiles_share_one_source_reconstruction(self):
        import concurrent.futures,threading,time
        area=self.h.area([[0,0],[100,0],[100,100],[0,100],[0,0]],record='w718262@1')
        original=fp._resolve_selection;started=threading.Event()
        def slow(*args,**kwargs):
            started.set();time.sleep(.08);return original(*args,**kwargs)
        with patch.object(fp,'_resolve_selection',side_effect=slow) as solve,concurrent.futures.ThreadPoolExecutor(2) as pool:
            one=pool.submit(self.h.rows,[area],[]);self.assertTrue(started.wait(2.))
            two=pool.submit(self.h.rows,[area],[])
            self.assertEqual(one.result(timeout=5.),two.result(timeout=5.))
            self.assertEqual(solve.call_count,1)
    def test_wire_format_distinguishes_valid_empty_tile_from_missing_output(self):
        import tempfile,roads
        report=dict(network_digest='approved-route',placement_status='no_sections_in_tile',version=7)
        with tempfile.TemporaryDirectory() as directory:
            path,_=roads.write_tile(directory,'/x',1,0,0,[],props=[],perimeter_status=report)
            data=json.loads((Path(directory)/Path(path).name).read_text())
        self.assertEqual(data['perimeters'],[])
        self.assertEqual(data['perimeter_status'],report)
    def test_old_unanchored_census_main_line_is_review_only_after_constraint(self):
        fence=self.h.feature_evidence([[20,65],[80,65]])
        fence['properties'].update(outer_member_of=['way/1'],tags=dict(boundary='census'))
        audit={};rows=self.h.rows([self.area],[self.constraint(),fence],audit=audit)
        self.assertFalse(any(r[8]=='physical:way/900' for r in rows))
        proposals=audit['geographic_network']['proposals']
        self.assertTrue(any(x['source']=='way/900' and x['confidence']=='medium' and not x['render'] for x in proposals))

class PhotoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The captured v7 automatic-mode contract remains a regression fixture
        # for global facilities; current Warren source selection has dedicated tests.
        import authoritativeperimeters
        legacy=patch.object(authoritativeperimeters,'policy_for',return_value=None)
        legacy.start();cls.addClassCleanup(legacy.stop)
        import facility_perimeter_review as review
        cls.data,_=review.snapshot();cls.results=[]
        for lat,lon in PHOTOS:
            frame=geodesy.EnuFrame(lat,lon,0);audit={}
            rows=p.rows(frame,lambda key:[cls.data[layer] for layer in p.LAYERS],lambda key:cls.data['infrastructure'],(-180,-180,180,180),None,
                surface=lambda xy:np.full(len(xy),10.),transport_fn=lambda key:cls.data['roads'],routing_context=dict(roads=[cls.data['roads']]),audit=audit)
            cls.results.append((frame,rows,audit))
    def test_ten_locations_report_both_passes_and_stable_unique_panels(self):
        for photo,(frame,rows,audit) in enumerate(self.results,1):
            with self.subTest(photo=photo):
                self.assertEqual(audit['geographic_network']['pass_number'],1)
                self.assertEqual(audit['placement_report']['pass_number'],2)
                self.assertEqual(len(rows),len({r[7] for r in rows}))
                if photo>2:self.assertGreater(len(rows),20)
    def test_happy_jack_main_network_has_no_southern_geometry(self):
        frame,_,audit=self.results[0]
        facility=next(f for f in audit['facilities'] if 'relation/252043' in f['source_ids'])
        self.assertTrue(facility['boundary_constraints'])
        constraint=next(x for x in fp.evidence() if x['properties']['role']=='boundary_constraint')
        local=geodesy.EnuFrame(*facility['validation_frame'],0)
        def metric(c):
            e,n,_=local.geodetic_to_enu(c[:,1],c[:,0],0);return np.c_[e,n]
        _,keep,_=fp.constrain_interior(shapely.transform(shape(facility['union_lonlat']),metric),[constraint],lambda g:shapely.transform(g,metric))
        for run in audit['runs']:
            if run['facility_id']!=facility['facility_id'] or run['boundary_class']=='secured_compound':continue
            selected=shapely.transform(shape(run['selected_geometry']),metric)
            self.assertLess(selected.difference(keep.buffer(.005)).length,.01)

if __name__=='__main__':unittest.main()
