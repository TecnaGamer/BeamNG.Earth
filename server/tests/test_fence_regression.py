"""v5 classification, completeness and physical-replacement regressions."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import numpy as np
import shapely
from shapely.geometry import LineString,MultiLineString,box
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'server'),str(ROOT/'server/tests')]
import facilityperimeters as fp,perimeters as p,mapgeom
import test_facility_perimeters as base_tests


class RegressionTest(unittest.TestCase):
    def setUp(self):
        # Historical generic source-reconstruction regressions, independent of
        # the Warren-specific policy that supersedes this old captured layout.
        import authoritativeperimeters
        legacy=patch.object(authoritativeperimeters,'policy_for',return_value=None)
        legacy.start();self.addCleanup(legacy.stop)
        self.helper=base_tests.FacilityTest();self.helper.setUp()
        self.frame=self.helper.frame
        self.area=self.helper.area([[0,0],[100,0],[100,100],[0,100],[0,0]])

    def test_generic_internal_mapped_enclosure_is_not_promoted_to_security_perimeter(self):
        physical=self.helper.feature_evidence([[20,20],[80,20],[80,80],[20,80],[20,20]])
        audit={};rows=self.helper.rows([self.area],[physical],audit=audit)
        self.assertNotIn('physical:way/900',{r[8] for r in rows})
        decision=audit['facilities'][0]['source_decisions'][0]
        self.assertEqual(decision['classification'],'internal_nonperimeter')
        self.assertFalse(decision['retained'])
        self.assertTrue(decision['original_lonlat'])

    def test_separately_secured_compound_fence_is_preserved(self):
        physical=self.helper.feature_evidence([[20,20],[80,20],[80,80],[20,80],[20,20]])
        physical['properties']['boundary_class']='secured_compound'
        rows=self.helper.rows([self.area],[physical])
        self.assertIn('physical:way/900',{r[8] for r in rows})

    def test_declared_unfenced_facility_does_not_get_an_automatic_closed_loop(self):
        area=self.helper.area([[0,0],[100,0],[100,100],[0,100],[0,0]],tags={'landuse':'military','fenced':'no'})
        audit={};rows=self.helper.rows([area],audit=audit)
        self.assertEqual(rows,[])
        self.assertTrue(audit['facilities'][0]['declared_unfenced'])
        self.assertEqual(audit['facilities'][0]['completeness']['status'],'consistent')

    def test_unfenced_internal_subdivision_does_not_delete_facility_exterior(self):
        inner=self.helper.area([[20,20],[80,20],[80,80],[20,80],[20,20]],'w2@1','',{'landuse':'military','fenced':'no'})
        self.assertEqual(self.helper.rows([self.area]),self.helper.rows([self.area,inner]))

    def test_corroborated_named_facility_restores_exterior_without_internal_source_seam(self):
        admin=self.helper.area([[0,0],[200,0],[200,100],[0,100],[0,0]],'r2@1',tags={'landuse':'military','type':'boundary'})
        direct=self.helper.area([[0,0],[100,0],[100,40],[0,40],[0,0]])
        physical=self.helper.feature_evidence([[0,0],[200,0]],targets=['relation/2'])
        physical['properties']['outer_member_of']=['relation/2']
        audit={};rows=self.helper.rows([admin,direct],[physical],audit=audit)
        self.assertTrue(any(abs(r[1])<.01 and abs(r[4])<.01 and max(r[2],r[5])>70 for r in rows))
        self.assertFalse(any(abs(r[2]-40)<.01 and abs(r[5]-40)<.01 and 1<r[1]<99 for r in rows))
        facility=audit['facilities'][0]
        self.assertEqual(facility['completeness']['status'],'consistent')
        self.assertEqual(facility['corroborating_outer_members'],['way/900'])

    def test_physical_replacement_has_no_twelve_metre_endpoint_halo(self):
        physical=self.helper.feature_evidence([[20,2],[80,2]])
        rows=self.helper.rows([self.area],[physical])
        segments=shapely.union_all([LineString([r[1:3],r[4:6]]) for r in rows])
        for point in ((10,0),(19,0),(81,0),(90,0),(20,1),(80,1)):
            self.assertLess(segments.distance(shapely.Point(point)),.01)
        self.assertTrue(all(not (40<r[1]<60 and abs(r[2])<.01) for r in rows))

    def test_mapped_gate_remains_open_after_endpoint_restoration(self):
        physical=self.helper.feature_evidence([[20,2],[80,2]])
        gate=self.helper.base.feature([np.array([[50.,2.]])],tags={'barrier':'gate'},kind=mapgeom.POINT,record='w999@1')
        rows=p.rows(self.frame,lambda k:[self.area],lambda k:gate,(-10,-10,110,110),None,
            surface=lambda xy:np.full(len(xy),10.),extra_evidence=[physical])
        self.assertTrue(rows)
        self.assertTrue(all(LineString([r[1:3],r[4:6]]).distance(shapely.Point(50,2))>2.9 for r in rows))

    def test_anchored_physical_route_replaces_entire_corresponding_arc(self):
        physical=self.helper.feature_evidence([[20,2],[50,30],[80,2]])
        rows=self.helper.rows([self.area],[physical])
        inferred=shapely.union_all([LineString([r[1:3],r[4:6]]) for r in rows if r[8]=='way/1'])
        self.assertGreater(inferred.distance(shapely.Point(50,0)),19.)
        self.assertIn('physical:way/900',{r[8] for r in rows})

    def test_completeness_distinguishes_openings_errors_and_missing_data(self):
        expected=LineString([(0,0),(100,0)])
        selected=MultiLineString([[(0,0),(30,0)],[(70,0),(100,0)]])
        result=fp.validate('one',expected,selected,[box(40,-1,60,1)])
        self.assertEqual(result['status'],'implementation_gap')
        self.assertAlmostEqual(result['unexpected_missing_m'],20.,places=1)
        self.assertAlmostEqual(result['explained_opening_m'],20.)
        missing=fp.validate('one',expected,selected,missing_source='physical mapping unavailable')
        self.assertEqual(missing['status'],'missing_source')
        self.assertEqual(selected.length,60.) # validation never fills an opening

    def test_completeness_detects_duplicate_and_unexplained_internal_runs(self):
        line=LineString([(0,0),(100,0)])
        duplicate=MultiLineString([line,line])
        result=fp.validate('one',line,duplicate,internal=[dict(id='unknown yard',supported=False)])
        self.assertAlmostEqual(result['duplicate_m'],100.)
        self.assertEqual(result['unexplained_internal_ids'],['unknown yard'])

    def test_repeated_vertices_do_not_make_missing_external_sections(self):
        repeated=self.helper.area([[0,0],[40,0],[40,0],[100,0],[100,100],[0,100],[0,0]])
        audit={};self.helper.rows([repeated],audit=audit)
        self.assertEqual(audit['facilities'][0]['completeness']['status'],'consistent')

    def test_real_warren_west_is_restored_yards_removed_and_east_physical_retained(self):
        with np.load(ROOT/'probes/building_fence_military_road_perimeter_areas.npz') as arrays:
            feats=mapgeom.Features.from_arrays('perimeter_areas',arrays,'')
        areas=[];metadata={}
        for i in range(len(feats)):
            fid=p.osm_id(feats,i);category=p.classify(p.tags_of(feats,i),str(feats.attrs['class'][i]))
            if not category:continue
            rings=[]
            for part in feats.parts(i):
                geo=p.canonical_ring(part[0]);e,n=mapgeom.enu_xy(feats,self.frame,geo);rings.append((geo,np.c_[e,n]))
            areas.append((fid,category,rings));metadata[fid]=dict(tags=p.tags_of(feats,i),name=str(feats.attrs['name'][i]))
        selected,meta,audit,_=fp.select(self.frame,areas,metadata)
        self.assertNotIn('physical:way/468201016',meta)
        self.assertNotIn('physical:way/468201001',meta)
        # Exact endpoint chains may have a stable network ID; keep the actual
        # east-fence provenance and geometry, rather than requiring one way ID
        # to remain an independent duplicate render run.
        east=[record for record in meta.values() if record['confidence']=='explicit' and 'way/1299453233' in record['source_ids']]
        self.assertTrue(east)
        physical=next(x['geometry'] for x in fp.evidence() if x['properties']['id']=='way/1299453233')
        self.assertLess(physical.difference(shapely.union_all([r['allowed_lonlat'] for r in east]).buffer(2e-8)).length,2e-7)
        source=ET.parse(ROOT/'probes/warren_boundary_full.osm').getroot()
        nodes={node.attrib['id']:(float(node.attrib['lon']),float(node.attrib['lat'])) for node in source.findall('node')}
        western=next(way for way in source.findall('way') if way.attrib['id']=='462694592')
        geo=np.array([nodes[node.attrib['ref']] for node in western.findall('nd')])
        e,n=mapgeom.enu_xy(None,self.frame,geo);line=LineString(np.c_[e,n])
        expected=line.interpolate(.4,normalized=True)
        self.assertLess(meta['relation/252043']['allowed'].distance(expected),.01)
        facility=next(f for f in audit if 'relation/252043' in f['source_ids'])
        self.assertEqual(facility['completeness']['status'],'consistent',
            (facility['completeness'],facility['routing']['gaps']))
        self.assertLess(facility['completeness']['unexpected_missing_m'],.02)
        self.assertEqual(facility['completeness']['duplicate_m'],0.)


if __name__=='__main__':unittest.main()
