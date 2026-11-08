"""Confirmed OSM relation outer ways: direct alignment, exclusivity and freshness."""
from pathlib import Path
import json,sys,tempfile,unittest,copy
from unittest.mock import patch
import numpy as np
import shapely
from shapely.geometry import LineString,box
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'server'),str(ROOT/'server/tests'),str(ROOT/'tools')]
import authoritativeperimeters as ap,facilityperimeters as fp,perimeters as p,mapgeom,fences,fencerouting as fr
import test_facility_perimeters as fixtures

class ExplicitTest(unittest.TestCase):
 def setUp(self):
  self.h=fixtures.FacilityTest();self.h.setUp();self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.path=Path(self.temp.name)/'source.json';self.patch=patch.object(ap,'DATA',self.path);self.patch.start();self.addCleanup(self.patch.stop)
  self.geo=self.h.geo([[0,0],[50,0],[100,0],[100,100],[0,100],[0,0]])
  self.doc=dict(elements=[dict(type='relation',id=252043,version=17,changeset=191629546,tags=dict(type='boundary',boundary='border_zone',name=ap.BASE_NAME),members=[dict(type='way',ref=10,role='outer'),dict(type='way',ref=11,role='outer')]),
   dict(type='way',id=10,version=2,nodes=[1,2,3],geometry=[dict(lon=x,lat=y) for x,y in self.geo[:3]],tags={}),
   dict(type='way',id=11,version=4,nodes=[1,5,4,3],geometry=[dict(lon=x,lat=y) for x,y in self.geo[[0,4,3,2]]],tags={})])
  self.write(self.doc)
  self.area=self.h.area([[-20,-20],[120,-20],[120,120],[-20,120],[-20,-20]],record='r252043@16',name=ap.BASE_NAME)
 def write(self,doc):
  temporary=self.path.with_suffix('.tmp');temporary.write_text(json.dumps(doc),encoding='utf-8');temporary.replace(self.path)
 def test_current_selector_is_confirmed_relation_not_assumed_fence_tag(self):
  records,roots,issues=ap.extract(self.doc)
  self.assertEqual({r['id'] for r in records},{'way/10','way/11'});self.assertEqual(roots[0]['version'],17);self.assertFalse(issues)
  wrong=copy.deepcopy(self.doc);wrong['elements'][0]['tags']['boundary']='administrative'
  self.assertEqual(ap.extract(wrong)[0],[])
 def test_connected_reversed_member_ways_preserve_every_original_vertex(self):
  records,_,_=ap.extract(self.doc);paths,report=ap.chains(records)
  self.assertEqual(len(paths),1);self.assertEqual(report['endpoints'],[])
  self.assertEqual(set(paths[0]['coordinates']),{tuple(v) for v in self.geo})
  self.assertEqual(paths[0]['coordinates'][0],paths[0]['coordinates'][-1])
 def test_duplicate_members_and_geometry_have_one_owner(self):
  doc=copy.deepcopy(self.doc);duplicate=copy.deepcopy(doc['elements'][1]);duplicate['id']=12;doc['elements'].append(duplicate)
  doc['elements'][0]['members'].append(dict(type='way',ref=12,role='outer'))
  records,_,_=ap.extract(doc);paths,report=ap.chains(records)
  self.assertEqual(report['source_segments'],5);self.assertEqual(len(paths),1)
  self.assertIn('way/12',paths[0]['source_ids'])
 def test_inner_enclosure_members_are_not_main_outer_linework(self):
  doc=copy.deepcopy(self.doc);doc['elements'][0]['members'][1]['role']='inner'
  records,_,_=ap.extract(doc);self.assertEqual([r['id'] for r in records],['way/10'])
 def test_gap_remains_open_without_closing_chord(self):
  doc=copy.deepcopy(self.doc);doc['elements'][2]['nodes'][0]=99;doc['elements'][2]['geometry'][0]['lat']+=.00001
  paths,report=ap.chains(ap.extract(doc)[0])
  self.assertTrue(report['endpoints'])
  self.assertEqual(sum(len(x['coordinates'])-1 for x in paths),5)
 def test_main_ignores_property_union_road_constraint_and_optimizer(self):
  road=self.h.feature_evidence([[-20,80],[120,80]],identifier='reviewed/old',role='boundary_constraint',targets=['relation/252043'])
  road['properties'].update(keep_side='north',offset_m=6.,confidence='verified')
  audit={}
  with patch.object(fr,'optimize',side_effect=AssertionError('automatic optimizer must not run for this main perimeter')):
   rows=self.h.rows([self.area],[road],audit=audit)
  self.assertTrue(rows);self.assertTrue(all(r[8].startswith('physical:authoritative:') for r in rows))
  for row in rows:self.assertLess(LineString([row[1:3],row[4:6]]).difference(box(0,0,100,100).boundary.buffer(.002)).length,.01)
  self.assertEqual(audit['facilities'][0]['boundary_constraints'],[])
 def test_missing_current_relation_withholds_main_without_polygon_fallback(self):
  self.write(dict(elements=[]));audit={}
  with patch.object(fr,'optimize',side_effect=AssertionError('no fallback')):
   rows=self.h.rows([self.area],[],audit=audit)
  self.assertEqual(rows,[])
  self.assertEqual(audit['placement_report']['geographic_status'],'awaiting_authoritative_source')
  self.assertFalse(audit['placement_report']['authoritative_sources'][0]['fallback'])
 def test_independent_internal_enclosure_is_preserved(self):
  fence=self.h.feature_evidence([[20,20],[40,20],[40,40],[20,40],[20,20]])
  fence['properties']['boundary_class']='secured_compound'
  rows=self.h.rows([self.area],[fence]);self.assertIn('physical:way/900',{r[8] for r in rows})
 def test_connected_ways_have_global_phase_and_unique_tile_ownership(self):
  whole=self.h.rows([self.area],[],bounds=(-10,-10,110,110));left=self.h.rows([self.area],[],bounds=(-10,-10,50,110));right=self.h.rows([self.area],[],bounds=(50,-10,110,110))
  self.assertEqual(sorted(whole),sorted(left+right));self.assertEqual(len(whole),len({r[7] for r in whole}))
 def test_standalone_relation_does_not_require_military_or_barrier_feature(self):
  rows=self.h.rows([],[],bounds=(-10,-10,110,110))
  self.assertGreater(len(rows),90)
 def test_ordinary_duplicate_is_suppressed_but_parallel_and_crossing_fences_remain(self):
  frame=self.h.frame;exclusive=[LineString(self.geo[:3])]
  def rows(xy):
   feature=self.h.base.feature([np.asarray(xy,float)],tags=dict(barrier='fence',fence_type='chain_link'),record='w999@1',kind=mapgeom.LINE)
   return fences.rows(frame,lambda key:feature,(-10,-30,110,30),lambda la,lo:np.full(len(la),10.),exclude_geometry=exclusive)
  self.assertEqual(rows([[0,0],[100,0]]),[])
  self.assertTrue(rows([[0,.4],[100,.4]]));self.assertTrue(rows([[50,-20],[50,20]]))
 def test_snapshot_refresh_invalidates_only_its_geographic_cache_scope(self):
  b=LineString(self.geo).bounds;self.doc['scope_bounds']=list(b);self.write(self.doc)
  one=ap.cache_token(b);foreign=ap.cache_token((-.2,51.4,.2,51.6))
  changed=copy.deepcopy(self.doc);changed['elements'][0]['version']=18;self.write(changed)
  self.assertNotEqual(one,ap.cache_token(b));self.assertEqual(foreign,ap.cache_token((-.2,51.4,.2,51.6)))
 def test_refresh_validates_before_atomic_replacement(self):
  import refresh_warren_boundary as refresh
  fixture=(ROOT/'probes/warren_authoritative_relation_252043.osm').read_bytes()
  result=refresh.refresh(self.path,fetch=lambda url:fixture)
  self.assertEqual(result['source'],'relation/252043');self.assertFalse(result['restart_performed'])
  before=self.path.read_bytes()
  with self.assertRaises(ValueError):refresh.refresh(self.path,fetch=lambda url:b'<osm/>')
  self.assertEqual(before,self.path.read_bytes())
 def test_captured_version17_has_two_chains_and_no_new_military_relation_members(self):
  actual=ap.xml_document((ROOT/'probes/warren_authoritative_relation_252043.osm').read_bytes())
  records,roots,issues=ap.extract(actual);paths,topology=ap.chains(records)
  self.assertEqual(roots[0]['id'],'relation/252043');self.assertEqual(len(records),11);self.assertFalse(issues)
  self.assertEqual(topology['paths'],2);self.assertEqual(len(topology['endpoints']),4)
  self.assertNotIn('way/1576457907',{r['id'] for r in records})

if __name__=='__main__':unittest.main()
