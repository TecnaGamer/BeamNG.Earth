"""Facility interpretation, evidence priority and final transport regressions."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
import shapely
from shapely.geometry import Polygon,LineString,box
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'server'),str(ROOT/'server/tests')]
import facilityperimeters as fp,perimeters as p,fencecrossings as fc,mapgeom,geodesy,revisit,fences
import test_perimeters as base


class FacilityTest(unittest.TestCase):
    def setUp(self):
        self.base=base.GlobalPerimeterTest();self.base.setUp()
        self.frame=self.base.frame

    def geo(self,xy):
        xy=np.asarray(xy,float)
        la,lo,_=self.frame.enu_to_geodetic(xy[:,0],xy[:,1],np.zeros(len(xy)))
        return np.c_[lo,la]

    def area(self,xy,record='w1@1',name='Example installation',tags=None):
        feat=self.base.feature([np.asarray(xy,float)],record=record,tags=tags or {'landuse':'military'})
        feat.attrs['name']=np.array([name])
        return feat

    def feature_evidence(self,xy,identifier='way/900',role='physical_fence',targets=None):
        geometry=Polygon(self.geo(xy)) if role=='public_exclusion' else LineString(self.geo(xy))
        properties=dict(id=identifier,role=role,source='test geographic evidence',reason='synthetic verified evidence')
        if targets:properties['facility_sources']=targets
        return dict(geometry=geometry,properties=properties)

    def rows(self,areas,evidence=(),bounds=(-10,-10,220,120),**kwargs):
        return p.rows(self.frame,lambda key:areas,lambda key:None,bounds,None,
            surface=lambda xy:np.full(len(xy),10.),extra_evidence=evidence,**kwargs)

    def test_related_overlap_has_one_external_outline_no_internal_seam(self):
        one=self.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        two=self.area([[50,0],[150,0],[150,100],[50,100],[50,0]],record='w2@1')
        audit={};rows=self.rows([one,two],audit=audit)
        self.assertEqual(len(audit['facilities']),1)
        self.assertGreater(audit['facilities'][0]['internal_boundary_suppressed_m'],200)
        self.assertFalse(any(abs(r[1]-r[4])<.01 and 1<r[1]<149 for r in rows))

    def test_unnamed_nested_subdivision_has_no_inferred_internal_fence(self):
        outer=self.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        inner=self.area([[20,20],[80,20],[80,80],[20,80],[20,20]],'w2@1','')
        self.assertEqual(self.rows([outer]),self.rows([outer,inner]))

    def test_same_facility_near_identical_geometry_with_two_classifications_is_grouped(self):
        one=self.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        two=self.area([[0,0],[100,0],[100,100],[0,100],[0,0]],'w2@1',tags={'landuse':'industrial'})
        audit={};self.rows([one,two],audit=audit)
        self.assertEqual(len(audit['facilities']),1)
        self.assertEqual(audit['facilities'][0]['inferred_external_rings_after'],1)

    def test_same_installation_admin_does_not_extend_inference_over_public_land(self):
        admin=self.area([[0,0],[200,0],[200,100],[0,100],[0,0]],'r2@1',tags={'landuse':'military','type':'boundary'})
        direct=self.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        audit={};rows=self.rows([admin,direct],audit=audit)
        self.assertTrue(rows)
        self.assertFalse(any(r[1]>102 or r[4]>102 for r in rows))
        self.assertFalse(any(abs(r[1]-100)<.01 and abs(r[4]-100)<.01 for r in rows))
        self.assertGreater(audit['facilities'][0]['administrative_unsupported_m'],200)

    def test_unrelated_touching_facilities_remain_separate_with_shared_line_owner(self):
        one=self.area([[0,0],[100,0],[100,100],[0,100],[0,0]],name='One')
        two=self.area([[100,0],[200,0],[200,100],[100,100],[100,0]],'w2@1','Two')
        audit={};rows=self.rows([one,two],audit=audit)
        self.assertEqual(len(audit['facilities']),2)
        shared=[r for r in rows if abs(r[1]-100)<.01 and abs(r[4]-100)<.01]
        self.assertEqual(len({r[8] for r in shared}),1)

    def test_named_private_compound_and_distinct_heliport_are_preserved(self):
        outer=self.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        inner=self.area([[20,20],[80,20],[80,80],[20,80],[20,20]],'w2@1','Depot',{'landuse':'military','military':'depot','access':'private'})
        heli=self.area([[30,30],[40,30],[40,40],[30,40],[30,30]],'w3@1','Heliport',{'landuse':'military','aeroway':'aerodrome'})
        audit={};self.rows([outer,inner,heli],audit=audit)
        self.assertEqual(len(audit['facilities']),3)

    def test_explicit_partial_fence_wins_without_losing_other_sides(self):
        area=self.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        physical=self.feature_evidence([[0,2],[100,2]])
        rows=self.rows([area],[physical])
        self.assertTrue(any(r[8]=='physical:way/900' for r in rows))
        self.assertFalse(any(abs(r[2])<.1 and abs(r[5])<.1 for r in rows))
        self.assertTrue(any(abs(r[2]-100)<.1 and abs(r[5]-100)<.1 for r in rows))

    def test_legitimate_parallel_physical_fences_are_not_distance_filtered(self):
        area=self.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        evidence=[self.feature_evidence([[0,2],[100,2]]),self.feature_evidence([[0,3],[100,3]],'way/901')]
        rows=self.rows([area],evidence)
        self.assertTrue({'physical:way/900','physical:way/901'}.issubset({r[8] for r in rows}))

    def test_identical_physical_source_lines_have_one_topological_owner(self):
        area=self.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        evidence=[self.feature_evidence([[0,2],[100,2]]),self.feature_evidence([[0,2],[50,2],[100,2]],'way/901')]
        rows=self.rows([area],evidence)
        self.assertEqual(len({r[8] for r in rows if r[8].startswith('physical:')}),1)

    def test_physical_internal_fence_is_supported_even_when_landuse_seam_is_removed(self):
        outer=self.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        inner=self.area([[20,20],[80,20],[80,80],[20,80],[20,20]],'w2@1','')
        physical=self.feature_evidence([[20,20],[80,20],[80,80],[20,80],[20,20]])
        physical['properties']['boundary_class']='secured_compound'
        rows=self.rows([outer,inner],[physical])
        self.assertIn('physical:way/900',{r[8] for r in rows})

    def test_public_exclusion_cuts_only_verified_area(self):
        area=self.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        public=self.feature_evidence([[40,-10],[60,-10],[60,10],[40,10],[40,-10]],role='public_exclusion',targets=['way/1'])
        audit={};rows=self.rows([area],[public],audit=audit)
        self.assertFalse(any(min(r[1],r[4])<59.9 and max(r[1],r[4])>40.1 and abs(r[2])<.01 and abs(r[5])<.01 for r in rows))
        self.assertTrue(audit['facilities'][0]['public_exclusions'])

    def test_unverified_parking_is_not_assumed_public(self):
        unverified=self.base.feature(tags={'amenity':'parking'})
        self.assertEqual(fp.public_features([unverified]),[])
        unverified.attrs['tags']=np.array(['{"amenity":"parking","access":"yes"}'])
        self.assertEqual(len(fp.public_features([unverified])),1)

    def test_verified_geographic_override_and_content_cache_invalidation(self):
        area=self.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        override=self.feature_evidence([[0,10],[90,10]],'reviewed/one','perimeter_override',['way/1'])
        override['properties']['confidence']='verified'
        rows=self.rows([area],[override])
        self.assertTrue(rows and all(r[8]=='physical:reviewed/one' for r in rows))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'override.geojson'
            feature=dict(type='Feature',geometry=json.loads(shapely.to_geojson(override['geometry'])),properties=override['properties'])
            document=dict(type='FeatureCollection',features=[feature])
            path.write_text(json.dumps(document),encoding='utf-8')
            with patch.dict(os.environ,{'BNE_PERIMETER_OVERRIDES':str(path)}):
                with patch.object(revisit,'_code',[]):one=revisit.code_key()
                feature['properties']['reason']='review updated'
                path.write_text(json.dumps(document),encoding='utf-8')
                with patch.object(revisit,'_code',[]):two=revisit.code_key()
            self.assertNotEqual(one,two)

    def test_unverified_override_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'invalid.geojson'
            override=self.feature_evidence([[0,10],[90,10]],'reviewed/one','perimeter_override',['way/1'])
            path.write_text(json.dumps(dict(type='FeatureCollection',features=[dict(type='Feature',properties=override['properties'],geometry=json.loads(shapely.to_geojson(override['geometry'])))])),encoding='utf-8')
            with patch.dict(os.environ,{'BNE_PERIMETER_OVERRIDES':str(path)}):
                with self.assertRaises(ValueError):fp.evidence()

    def test_debug_geojson_contains_source_selection_and_final_transport_omissions(self):
        one=self.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        two=self.area([[50,0],[150,0],[150,100],[50,100],[50,0]],'w2@1')
        deck=dict(m='road_asphalt_2lane',w=8.,n=[[50,-20,11.],[50,120,11.]],fixed=True,deck=True,fid='real-deck')
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BNE_PERIMETER_DEBUG_DIR':directory}):
            self.rows([one,two],transport_geometry=[deck])
            files=list(Path(directory).glob('*.geojson'))
            self.assertEqual(len(files),1)
            features=json.loads(files[0].read_text())['features']
            self.assertIn('source_polygon',{f['properties']['layer'] for f in features})
            self.assertIn('suppressed_internal',{f['properties']['layer'] for f in features})
            self.assertTrue(any(f['properties'].get('reason')=='final_transport_envelope' for f in features))

    def test_stable_selection_and_module_ids_across_tile_load_order_and_origin(self):
        one=self.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        two=self.area([[50,0],[150,0],[150,100],[50,100],[50,0]],'w2@1')
        all_rows=self.rows([one,two])
        left=self.rows([two,one],bounds=(-10,-10,75,120));right=self.rows([one,two],bounds=(75,-10,220,120))
        self.assertEqual({r[7] for r in all_rows},{r[7] for r in left+right})
        self.assertFalse({r[7] for r in left}&{r[7] for r in right})
        self.frame=geodesy.EnuFrame(41.151,-104.851,0.)
        shifted=self.rows([two,one],bounds=(-1000,-1000,1000,1000))
        self.assertEqual({r[7] for r in all_rows},{r[7] for r in shifted})

    def test_multipolygon_and_holes_do_not_create_inferred_internal_rings(self):
        f=self.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        outer=f.parts(0)[0][0];hole=self.geo([[10,10],[20,10],[20,20],[10,20],[10,10]])
        second=self.geo([[120,0],[140,0],[140,20],[120,20],[120,0]])
        m=mapgeom.build(f.layer,f.ids,[(mapgeom.POLYGON,[[outer,hole],[second]])]);m.attrs=f.attrs
        audit={};self.rows([m],audit=audit)
        self.assertEqual(audit['selected_runs'],2)

    def test_final_bridge_volume_obstructing_fence_is_omitted_but_high_deck_is_kept(self):
        area=self.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        def deck(z):return dict(m='road_asphalt_2lane',w=8.,n=[[50,-20,z],[50,120,z]],fixed=True,deck=True,fid='real-deck')
        low_audit={};low=self.rows([area],transport_geometry=[deck(11.)],audit=low_audit)
        high=self.rows([area],transport_geometry=[deck(25.)])
        baseline=self.rows([area])
        self.assertEqual(len(high),len(baseline))
        self.assertLess(len(low),len(high))
        self.assertTrue(low_audit['final_transport_omissions'])
        self.assertTrue(all('real-deck' in e['transport_ids'] for e in low_audit['final_transport_omissions']))

    def test_final_generated_ground_carriageway_supplements_raw_transport(self):
        area=self.area([[0,0],[100,0],[100,100],[0,100],[0,0]])
        raw=mapgeom.empty('roads')
        actual=('moved-road',SimpleNamespace(width=12.),np.array([[50,-20,0],[50,120,0]]),0.)
        rows=self.rows([area],roads=[actual],transport_fn=lambda k:raw)
        self.assertFalse(any(min(r[1],r[4])<55 and max(r[1],r[4])>45 and abs(r[2])<.01 and abs(r[5])<.01 for r in rows))

    def test_selected_mapped_chainlink_is_not_redrawn_by_legacy_forest(self):
        fence=self.base.feature([np.array([[0.,0.],[40.,0.]])],tags={'barrier':'fence','fence_type':'chain_link'},kind=mapgeom.LINE,record='w900@1')
        before=fences.rows(self.frame,lambda k:fence,self.base.box,lambda la,lo:np.full(len(la),10.))
        after=fences.rows(self.frame,lambda k:fence,self.base.box,lambda la,lo:np.full(len(la),10.),exclude_ids={'way/900'})
        self.assertTrue(before);self.assertEqual(after,[])

    def test_actual_rail_mesh_is_guarded_only_where_active_source_exists(self):
        positions=np.array([[0.,0.,10.],[10.,0.,10.],[10.,4.,10.],[0.,4.,10.]])
        mesh=(positions,np.array([[0,1,2],[0,2,3]]),None,None,None)
        corridor=dict(a=np.array([0.,2.]),b=np.array([10.,2.]),radius=3.5)
        active=fc.SurfaceIndex([],meshes=[(mesh,(0,0,0),'rendered rail')],rail_corridors=[corridor])
        inactive=fc.SurfaceIndex([],meshes=[(mesh,(0,0,0),'rendered worn rail')],rail_corridors=[])
        self.assertTrue(active.conflicts(np.array([5.,-2.]),np.array([5.,6.]),10.,10.))
        self.assertFalse(inactive.conflicts(np.array([5.,-2.]),np.array([5.,6.]),10.,10.))

    def test_final_vertical_bridge_geometry_uses_its_actual_height_envelope(self):
        mesh=(np.array([[4,0,0],[4,5,0],[4,5,20],[4,0,20]]),np.array([[0,1,2],[0,2,3]]),None,None,None)
        index=fc.SurfaceIndex([],meshes=[(mesh,(0,0,0),'actual pier','bridge')],rail_corridors=[])
        self.assertTrue(index.conflicts(np.array([0.,2.]),np.array([8.,2.]),10.,10.))
        self.assertFalse(index.conflicts(np.array([0.,2.]),np.array([8.,2.]),30.,30.))

    def test_real_warren_overlap_reconciles_before_any_stock_sections(self):
        with np.load(ROOT/'probes/building_fence_military_road_perimeter_areas.npz') as arrays:
            feats=mapgeom.Features.from_arrays('perimeter_areas',arrays,'')
        areas=[];metadata={}
        for i in range(len(feats)):
            fid=p.osm_id(feats,i);category=p.classify(p.tags_of(feats,i),str(feats.attrs['class'][i]))
            if not fid or not category:continue
            rings=[]
            for part in feats.parts(i):
                geo=p.canonical_ring(part[0]);e,n=mapgeom.enu_xy(feats,self.frame,geo);rings.append((geo,np.c_[e,n]))
            areas.append((fid,category,rings));metadata[fid]=dict(tags=p.tags_of(feats,i),name=str(feats.attrs['name'][i]))
        selected,meta,audit,_=fp.select(self.frame,areas,metadata,extra_evidence=[])
        facility=next(f for f in audit if 'relation/252043' in f['source_ids'])
        self.assertEqual(facility['source_ids'],['relation/252043','way/841575049'])
        self.assertGreater(facility['overlap_measurements'][0]['overlap_fraction'],.999)
        self.assertGreater(facility['internal_boundary_suppressed_m'],1000.)
        # The heliport is a distinct role and not folded into that grouping.
        self.assertTrue(any(f['source_ids']==['way/468609144'] for f in audit))

    def test_complete_context_fetches_related_parcels_beyond_camera_cell(self):
        outer=self.area([[0,0],[5000,0],[5000,100],[0,100],[0,0]],'r2@1',tags={'landuse':'military','type':'boundary'})
        inner=self.area([[4000,0],[5000,0],[5000,100],[4000,100],[4000,0]])
        inner.ids=np.array(['second-source'])
        source_cell=mapgeom.empty('perimeter_areas');seen=[]
        def cell(key,layers=(),**kwargs):
            seen.append(key)
            content=outer if key==first else inner
            return SimpleNamespace(layers={layer:content if layer=='perimeter_areas' else mapgeom.empty(layer) for layer in layers})
        import mapcells
        first=mapcells.cell_of(self.frame.lat0,self.frame.lon0)
        md=SimpleNamespace(cell=cell,versions=lambda:{'test':1})
        # Hashable provider identity as real MapData is; SimpleNamespace is not.
        class Provider:
            versions=staticmethod(md.versions)
            cell=staticmethod(md.cell)
        result=fp.context(Provider(),self.frame,(-10,-10,10,10),p.LAYERS)
        self.assertGreater(len(set(seen)),1)
        self.assertEqual(set(result['perimeter_areas'].attrs['src_record']),{'r2@1','w1@1'})


if __name__=='__main__':unittest.main()
