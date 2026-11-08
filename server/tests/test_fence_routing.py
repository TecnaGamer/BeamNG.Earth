"""Whole-run routing invariants: no game, DEM downloads, or active caches."""
import json,os,sys,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import shapely
from shapely.geometry import LineString,box,Point
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'server'),str(ROOT/'server/tests')]
import fencerouting as fr,facilityperimeters as fp,revisit
import test_facility_perimeters as fixtures

class RoutingTest(unittest.TestCase):
    def route(self,reverse=False,angle=0):
        from shapely.affinity import rotate
        interior=rotate(box(-200,-200,200,0),angle,origin=(0,0))
        boundary=rotate(LineString([[-100,0],[100,0]]),angle,origin=(0,0))
        road=rotate(LineString([[-120,0],[120,0]]),angle,origin=(0,0))
        physical=rotate(LineString([[-100,4],[-70,4]]),angle,origin=(0,0))
        if reverse:
            boundary=LineString(list(boundary.coords)[::-1]);road=LineString(list(road.coords)[::-1])
            interior=shapely.geometry.Polygon(list(interior.exterior.coords)[::-1])
        return fr.optimize(interior,boundary,[('mapped',physical)],[dict(id='road',radius=2.,geometry=road)])

    def test_near_duplicate_source_stations_do_not_invent_missing_source(self):
        boundary=LineString([(0,0),(32,0),(64,0),(100,0)])
        physical=LineString([(32+1e-10,0),(64,0)])
        runs,audit=fr.optimize(box(0,0,100,100),boundary,[('mapped',physical)])
        self.assertFalse(audit['gaps'])
        self.assertEqual(audit['source_coverage'][0]['unresolved_m'],0.)
        self.assertAlmostEqual(audit['source_coverage'][0]['covered_m'],boundary.length)
        self.assertTrue(any(r['confidence']=='physical' for r in runs))

    def test_whole_run_uses_physical_side_anchor(self):
        runs,audit=self.route()
        self.assertEqual(audit['road_preferences'],{'road':-1})
        self.assertFalse(audit['gaps'])
        self.assertTrue(all(run['geometry'].bounds[1]>=2.64 for run in runs))
        self.assertTrue(any(r['confidence']=='physical' for r in runs))
        self.assertAlmostEqual(sum(r['geometry'].length for r in runs),201.35,places=5)

    def test_reversed_ways_and_polygon_winding_are_identical(self):
        first,a=self.route();second,b=self.route(reverse=True)
        self.assertEqual([(r['source'],r['geometry'].wkb) for r in first],[(r['source'],r['geometry'].wkb) for r in second])
        self.assertEqual(a['road_preferences'],b['road_preferences'])

    def test_rotations_keep_facility_relative_side(self):
        from shapely.affinity import rotate
        for angle in (90,180,270,37):
            with self.subTest(angle=angle):
                runs,audit=self.route(angle=angle)
                self.assertFalse(audit['gaps'])
                self.assertTrue(all(rotate(r['geometry'],-angle,origin=(0,0)).bounds[1]>=2.64 for r in runs))

    def test_ambiguous_costs_do_not_reset_established_physical_side(self):
        with patch.dict(os.environ,{'BNE_FENCE_ROUTING_WEIGHTS':'{"road_side":0}'}):
            runs,audit=self.route()
        self.assertFalse(audit['gaps'])
        self.assertTrue(all(r['geometry'].bounds[1]>=2.64 for r in runs))

    def test_road_endpoint_does_not_extend_an_infinite_corridor(self):
        roads=fr.Roads([dict(id='short',radius=2.,geometry=LineString([[0,0],[20,0]]))],box(-10,-50,50,0),30.)
        self.assertIsNone(roads.near(np.array([35.,.1]),np.array([1.,0.])))

    def test_self_crossing_physical_candidate_is_rejected(self):
        physical=LineString([[0,0],[70,30],[30,30],[100,0],[30,0],[70,40]])
        _,audit=fr.optimize(box(0,0,100,100),LineString([[0,0],[100,0]]),[('crossed',physical)])
        self.assertFalse(any(s['source']=='crossed' for s in audit['selected']))

    def test_unresolvable_obstacle_is_reported_not_bridged(self):
        runs,audit=fr.optimize(box(-100,-100,100,0),LineString([[0,0],[100,0]]),[],obstacles=[box(30,-10,70,10)])
        self.assertTrue(audit['gaps'])
        visible=shapely.union_all([r['geometry'] for r in runs if r.get('render',True)])
        self.assertFalse(visible.intersects(box(31,-9,69,9)))
        self.assertTrue(any(r.get('render') is False for r in runs))

    def test_source_disconnected_runs_do_not_get_a_chord(self):
        boundary=shapely.MultiLineString([[[0,0],[20,0]],[[80,0],[100,0]]])
        runs,_=fr.optimize(box(0,-100,100,0),boundary,[])
        self.assertAlmostEqual(sum(r['geometry'].length for r in runs),40.)

    def test_settings_reject_unknown_nonfinite_negative_weights(self):
        for setting in ('{"unknown":1}','{"source":-1}','{"source":NaN}'):
            with self.subTest(setting=setting),patch.dict(os.environ,{'BNE_FENCE_ROUTING_WEIGHTS':setting}):
                with self.assertRaises(ValueError):fr.settings()
        with patch.dict(os.environ,{'BNE_FENCE_ROUTE_ROAD_CLEARANCE_M':'nan'}):
            with self.assertRaises(ValueError):fr.settings()

    def test_exact_endpoint_chains_recover_fragmented_exterior(self):
        h=fixtures.FacilityTest();h.setUp()
        area=h.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        one=h.feature_evidence([[0,0],[30,20]],identifier='way/901')
        two=h.feature_evidence([[30,20],[100,0]],identifier='way/902')
        audit={};rows=h.rows([area],[one,two],audit=audit)
        self.assertTrue(any(r[8].startswith('physical:network/') for r in rows))
        self.assertTrue(any(d['osm_id'].startswith('network/') for d in audit['facilities'][0]['source_decisions']))

    def test_close_endpoints_are_not_connected(self):
        h=fixtures.FacilityTest();h.setUp()
        evidence=[h.feature_evidence([[0,0],[30,20]],identifier='way/901'),h.feature_evidence([[30.1,20],[100,0]],identifier='way/902')]
        self.assertFalse(fp.physical_chains({e['properties']['id']:e for e in evidence}))

    def test_terrain_failure_does_not_become_successful_empty_cache(self):
        h=fixtures.FacilityTest();h.setUp();audit={};before=revisit.thread_faults()
        area=h.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        import perimeters as p
        rows=p.rows(h.frame,lambda key:[area],lambda key:None,(-10,-10,110,110),None,
                    surface=lambda xy:np.full(len(xy),np.nan),extra_evidence=[],audit=audit)
        self.assertEqual(rows,[])
        self.assertGreater(revisit.thread_faults(),before)
        self.assertTrue(audit['placement_failures'])

    def test_named_enclosure_membership_preserves_physical_school_fence(self):
        h=fixtures.FacilityTest();h.setUp()
        area=h.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        fence=h.feature_evidence([[20,20],[80,20],[80,80],[20,80]])
        fence['properties']['enclosure_of']=[dict(id='relation/5',tags=dict(amenity='school'))]
        rows=h.rows([area],[fence])
        self.assertTrue(any(r[8]=='physical:way/900' for r in rows))

    def test_authoritative_outer_source_is_not_erased_by_union_distance(self):
        h=fixtures.FacilityTest();h.setUp()
        area=h.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        fence=h.feature_evidence([[20,20],[80,20]])
        fence['properties']['outer_member_of']=['way/1']
        rows=h.rows([area],[fence])
        self.assertTrue(any(r[8]=='physical:way/900' for r in rows))

    def test_debug_export_has_interior_candidates_and_selected_reasons(self):
        import tempfile
        h=fixtures.FacilityTest();h.setUp()
        area=h.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BNE_PERIMETER_DEBUG_DIR':directory}):
            h.rows([area])
            data=json.loads(next(Path(directory).glob('*.geojson')).read_text())
        layers={f['properties']['layer'] for f in data['features']}
        self.assertTrue({'facility_interior','route_candidate','selected_route','generated'}.issubset(layers))
        self.assertTrue(all(f['properties'].get('reason') for f in data['features'] if f['properties']['layer']=='selected_route'))

    def test_new_router_participates_in_revisit_source_key(self):
        self.assertIn('fencerouting.py',revisit.source_closure(str(ROOT/'server')))



class CapturedWarrenRoutingTest(unittest.TestCase):
    """Pinned geographic inputs: five photo points plus entrance/school regressions."""
    @classmethod
    def setUpClass(cls):
        # Historical reconstructed-mode fixture: exercise the shared global
        # optimizer without the new Warren-only authoritative selection rule.
        import authoritativeperimeters
        legacy=patch.object(authoritativeperimeters,'policy_for',return_value=None)
        legacy.start();cls.addClassCleanup(legacy.stop)
        sys.path.insert(0,str(ROOT/'probes'))
        import facility_perimeter_review as review,perimeters as p,geodesy
        cls.data,_=review.snapshot();cls.results={}
        cases=[('photo1',41.17608,-104.88371),('photo2',41.17514,-104.88102),
               ('photo3',41.18959,-104.88097),('photo4',41.18958,-104.87817),
               ('north',41.20116,-104.86686),('entrance',41.1466,-104.8404),
               ('school',41.1355,-104.854),('happy_jack',41.139,-104.846),
               ('military',41.13458,-104.88113),('carey',41.14897,-104.82727)]
        for name,lat,lon in cases:
            frame=geodesy.EnuFrame(lat,lon,0);audit={}
            rows=p.rows(frame,lambda key:[cls.data[layer] for layer in p.LAYERS],lambda key:cls.data['infrastructure'],
                ((-1200,-1200,1200,1200) if name=='carey' else (-400,-350,400,350)),None,surface=lambda xy:np.full(len(xy),10.),
                transport_fn=lambda key:cls.data['roads'],routing_context=dict(roads=[cls.data['roads']]),audit=audit)
            cls.results[name]=(frame,rows,audit)

    def test_all_photo_locations_have_source_supported_tile_rows(self):
        for name in ('photo1','photo2','photo3','photo4','north'):
            with self.subTest(location=name):
                _,rows,audit=self.results[name]
                self.assertTrue(rows)
                self.assertLess(audit['selection_context_length_removed_m'],.02)
                self.assertEqual(len({r[7] for r in rows}),len(rows))

    def test_north_photo_stays_geographically_north_of_patrol_road(self):
        import geodesy
        frame,rows,audit=self.results['north']
        facility=next(f for f in audit['facilities'] if 'relation/252043' in f['source_ids'])
        local=geodesy.EnuFrame(*facility['validation_frame'],0)
        records=[]
        for road in facility['routing']['roads']:
            if road['id']!='way/1418376925':continue
            xy=np.asarray(shapely.geometry.shape(road['geometry_xy']).coords)
            la,lo,_=local.enu_to_geodetic(xy[:,0],xy[:,1],0);e,n,_=frame.geodetic_to_enu(la,lo,0)
            records.append(dict(id=road['id'],radius=road['radius'],geometry=LineString(np.c_[e,n])))
        index=fr.Roads(records,box(-1,-1,1,1),32);clearances=[]
        for row in rows:
            midpoint=np.mean(np.array([row[1:3],row[4:6]]),axis=0)
            if abs(midpoint[0])>120:continue
            near=index.near(midpoint,fr.unit(row[1:3],row[4:6]))
            if near:clearances.append(near[5]-near[2]['radius'])
        self.assertGreater(len(clearances),40)
        self.assertGreater(min(clearances),.5)

    def test_school_fences_have_real_relation_membership(self):
        _,rows,audit=self.results['school']
        for fid in ('way/468200975','way/468200977','way/468200978'):
            self.assertIn('physical:'+fid,{r[8] for r in rows})
        evidence={x['properties']['id']:x for x in fp.evidence()}
        self.assertEqual(evidence['way/468200975']['properties']['enclosure_of'][0]['id'],'relation/6905052')

    def test_entrance_retains_outer_connected_physical_recess(self):
        _,rows,audit=self.results['entrance']
        self.assertTrue(any(r[8].startswith('physical:network/') for r in rows))
        sources={x for run in audit['runs'] for x in run['source_ids']}
        self.assertIn('way/462694620',sources)
        self.assertIn('way/462694617',sources)

    def test_other_regression_areas_keep_unique_outputs(self):
        for name in ('happy_jack','military','carey'):
            with self.subTest(location=name):
                _,rows,_=self.results[name]
                self.assertTrue(rows)
                self.assertEqual(len({r[7] for r in rows}),len(rows))

if __name__=='__main__':unittest.main()
