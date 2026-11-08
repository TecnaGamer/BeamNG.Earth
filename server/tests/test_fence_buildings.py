"""Final mesh, geometric clearance, local resolution, and source-relationship regressions."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import numpy as np
from shapely.geometry import Polygon,LineString,box

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'server'))
sys.path.insert(0,str(ROOT/'server/tests'))
import fencebuildings as fb
import facilityperimeters as fp
import buildings,perimeters as p,mapgeom,geodesy
import test_perimeters as base_tests


def record(shape,identifier='synthetic GIS building'):
    return dict(geometry=shape,source=dict(id=identifier,dataset='synthetic non-OSM GIS'),zmin=0.,zmax=10.,
                transform=dict(position=[0,0,0],rotation='baked',scale=[1,1,1]),original_xy=list(shape.exterior.coords))


class BuildingFenceTest(unittest.TestCase):
    def test_cold_custom_reads_processed_mesh_without_baking_assets(self):
        vertices=np.array([[10.,20.,0.],[14.,20.,0.],[14.,22.,5.],[10.,22.,5.]])
        triangles=np.array([[0,1,2],[0,2,3]])
        with patch.object(buildings,'_model_mesh',return_value=(vertices,None,triangles,None,None,None)), \
             patch('threedmr.write_model',side_effect=AssertionError('must not bake')):
            records,radii=fb.cold_custom([dict(id=7,xy=(100.,200.))],None)
        self.assertEqual(records[0]['geometry'].bounds,(98.,199.,102.,201.))
        self.assertEqual(radii[0],(7,100.,200.,15.))

    def setUp(self):
        self.base=base_tests.GlobalPerimeterTest();self.base.setUp()

    def test_swept_mesh_detects_corner_with_centre_outside(self):
        index=fb.Index([record(box(3.9,-.1,6.,2.))])
        a,b=np.array([0.,0.]),np.array([4.,0.])
        self.assertFalse(box(3.9,-.1,6.,2.).contains(__import__('shapely').geometry.Point((a+b)/2)))
        self.assertTrue(index.conflicts(a,b))

    def test_adjacent_clearance_preserves_original(self):
        index=fb.Index([record(box(0,2,4,5))])
        self.assertEqual(index.resolve(np.array([-5.,0.]),np.array([10.,0.])),([],[]))

    def test_short_safe_detour_and_large_building_termination(self):
        index=fb.Index([record(box(4,-1,6,1))])
        cuts,detours=index.resolve(np.array([0.,0.]),np.array([10.,0.]))
        self.assertTrue(cuts and detours)
        self.assertTrue(all(index.clear(route) for _,_,route in detours))
        large=fb.Index([record(box(4,-20,30,20))])
        cuts,detours=large.resolve(np.array([0.,0.]),np.array([40.,0.]))
        self.assertTrue(cuts);self.assertFalse(detours)
        self.assertTrue(large.events[-1]['omitted'])

    def test_detour_cannot_enter_road_rail_runway_or_gate(self):
        shape=box(4,-1,6,1)
        a,b=np.array([0.,0.]),np.array([10.,0.])
        tracks=[dict(a=np.array([0.,y]),b=np.array([10.,y]),radius=1.,grade='ground',kind='railway') for y in (-2.,2.)]
        index=fb.Index([record(shape)])
        self.assertFalse(index.resolve(a,b,transport=tracks)[1])
        self.assertFalse(index.resolve(a,b,protected=[box(2,-5,8,5)])[1])
        self.assertFalse(index.resolve(a,b,blockers=[(np.array([5.,-2.]),np.array([5.,-2.]),4.)])[1])

    def test_exact_projection_preserves_irregular_mesh_not_bbox(self):
        vertices=np.array([[0,0,0],[4,0,0],[4,1,3],[1,1,3],[1,4,3],[0,4,0]],float)
        triangles=np.array([[0,1,2],[0,2,3],[0,3,5],[3,4,5]])
        mesh=fb.from_mesh(vertices,triangles,origin=(10,20),source=dict(id='GIS fixture',dataset='synthetic GIS'))
        self.assertLess(mesh['geometry'].area,16.)
        self.assertFalse(fb.Index([mesh]).conflicts(np.array([12.,22.]),np.array([13.,23.])))

    def test_multipolygon_building_keeps_clear_space_between_components(self):
        from shapely.geometry import MultiPolygon
        obstacle=dict(geometry=MultiPolygon([box(4,-1,6,1),box(14,-1,16,1)]),
                      source=dict(id='synthetic multipart GIS fixture',dataset='synthetic GIS'),
                      transform=dict(position=[0,0,0]),zmin=0.,zmax=10.)
        index=fb.Index([obstacle])
        self.assertFalse(index.conflicts(np.array([8.,0.]),np.array([12.,0.])))
        cuts,detours=index.resolve(np.array([0.,0.]),np.array([20.,0.]))
        self.assertEqual(len(cuts),2)
        self.assertEqual(len(detours),2)

    def test_transformed_mesh_is_used_instead_of_original_footprint(self):
        verts=np.array([[0,0,0],[2,0,0],[2,1,3],[0,1,3]],float)
        angle=.7;rot=np.array([[np.cos(angle),-np.sin(angle)],[np.sin(angle),np.cos(angle)]])
        verts[:,:2]=verts[:,:2]@rot.T*2+[15.,20.]
        final=fb.from_mesh(verts,[[0,1,2],[0,2,3]],source=dict(id='scaled rotated fixture',dataset='synthetic GIS'),
                           original=[[0,0],[2,0],[2,1],[0,1]])
        self.assertGreater(final['geometry'].bounds[0],10)
        self.assertTrue(fb.Index([final]).conflicts(np.array([15.,20.]),np.array([18.,22.])))

    def test_final_fence_pitch_includes_top_and_footing_projection(self):
        a,b=np.array([0.,0.]),np.array([4.,0.])
        index=fb.Index([record(box(-1.8,-.1,-.8,1.))])
        self.assertFalse(index.conflicts(a,b))
        self.assertTrue(index.conflicts(a,b,0.,2.))

    def test_footprints_follow_client_building_part_limit(self):
        import roads
        ring=buildings.clean_ring([[0,0],[20,0],[20,10],[0,10]])
        spec=buildings.plan(ring,{'height':8.,'seed_id':'synthetic capped mesh fixture'})
        records=[]
        meshes=buildings.build_parts([(spec,10.,10.)]*(roads.BLD_PARTS_MAX+1),max_verts=1,fence_geometry=records)
        self.assertEqual(len(meshes),roads.BLD_PARTS_MAX+1) # generation output itself is unchanged
        self.assertEqual(len(records),roads.BLD_PARTS_MAX)

    def test_build_parts_exports_same_final_vertices(self):
        ring=buildings.clean_ring([[0,0],[20,0],[20,12],[0,12]])
        spec=buildings.plan(ring,{'height':8.,'roof_shape':'gabled','seed_id':'synthetic fixture'})
        spec.source_meta=dict(id='test mesh',dataset='synthetic GIS',record='')
        records=[]
        meshes=buildings.build_parts([(spec,10.,10.)],fence_geometry=records,origin=(100,200))
        baseline=buildings.build_parts([(spec,10.,10.)])
        np.testing.assert_array_equal(meshes[0][0],baseline[0][0])
        np.testing.assert_array_equal(meshes[0][2],baseline[0][2])
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['source']['dataset'],'synthetic GIS')
        verts=meshes[0][0]
        self.assertGreaterEqual(records[0]['geometry'].bounds[0],float(verts[:,0].min())+100-.01)
        self.assertLessEqual(records[0]['geometry'].bounds[2],float(verts[:,0].max())+100+.01)
        deferred=[]
        with patch.object(fb,'from_mesh',wraps=fb.from_mesh) as project:
            buildings.build_parts([(spec,10.,10.)],fence_geometry=deferred,fence_geometry_deferred=True)
            project.assert_not_called()
            self.assertTrue(fb.materialize(deferred))
            project.assert_called_once()

    def test_building_corrections_retain_tile_ownership_and_terrain(self):
        obstacles=[record(box(15,-2,25,2))]
        def rows(bounds):
            return p.rows(self.base.frame,lambda k:[self.base.feature()],lambda k:None,bounds,None,
                          surface=lambda xy:20.+.02*xy[:,0],building_fn=lambda:obstacles)
        full=rows((-100,-100,200,200));left=rows((-100,-100,20,200));right=rows((20,-100,200,200))
        self.assertEqual(sorted(full),sorted(left+right))
        for row in full:
            self.assertFalse(fb.Index(obstacles).conflicts(np.array(row[1:3]),np.array(row[4:6])))
            self.assertAlmostEqual(row[3],20.+.02*row[1])

    def test_legitimate_named_parallel_internal_boundaries_remain(self):
        def area(fid,offset,name,tags):
            xy=np.array([[offset,offset],[100-offset,offset],[100-offset,100-offset],[offset,100-offset],[offset,offset]])
            la,lo,_=self.base.frame.enu_to_geodetic(xy[:,0],xy[:,1],np.zeros(len(xy)))
            return fid,'military',[(np.c_[lo,la],xy)],dict(name=name,tags=tags)
        one=area('one',0,'installation',{'landuse':'military'})
        two=area('two',1,'restricted internal area',{'landuse':'military','access':'private'})
        _,_,audit,_=fp.select(self.base.frame,[one[:3],two[:3]],{'one':one[3],'two':two[3]},extra_evidence=[])
        self.assertEqual(len(audit),2)

    def test_real_military_road_source_relationship(self):
        frame=geodesy.EnuFrame(41.13458,-104.88113,0.)
        with np.load(ROOT/'probes/building_fence_military_road_perimeter_areas.npz') as data:
            f=mapgeom.Features.from_arrays('perimeter_areas',data,'')
        areas=[];meta={}
        for i in range(len(f)):
            fid=p.osm_id(f,i);kind=p.classify(p.tags_of(f,i),str(f.attrs['class'][i]))
            if not kind: continue
            rings=[]
            for part in f.parts(i):
                geo=p.canonical_ring(part[0]);e,n=mapgeom.enu_xy(f,frame,geo);rings.append((geo,np.c_[e,n]))
            areas.append((fid,kind,rings));meta[fid]=dict(tags=p.tags_of(f,i),name=str(f.attrs['name'][i]))
        _,_,audit,_=fp.select(frame,areas,meta,extra_evidence=[])
        match=next(d for d in audit if 'relation/252043' in d['source_ids'])
        self.assertIn('way/841575049',match['source_ids'])
        self.assertGreater(match['internal_boundary_suppressed_m'],1000.)

    def test_published_final_bounds_find_moved_neighbor_even_when_raw_bbox_misses(self):
        from types import SimpleNamespace
        raw=self.base.feature([np.array([[5,5],[10,5],[10,10],[5,10],[5,5]])])
        class Source:
            def versions(self):return {}
            def cell(self,key,layers=(),**kwargs):return SimpleNamespace(layers={'buildings':raw})
        md=Source();frame=self.base.frame
        actual=[record(box(280,20,300,40),'moved final geometry fixture')]
        signature=fb._signature(None)
        fb.publish(md,frame,0,0,actual,signature)
        fb.publish_bounds(md,frame,0,0,actual)
        with patch.object(buildings,'road_tile_items',side_effect=AssertionError('published final geometry should be reused')):
            found=fb.nearby(frame,1,0,md,None,[])
        self.assertTrue(any(item['source']['id']=='moved final geometry fixture' for item in found))

    def test_dry_creek_exact_owner_mesh_and_cold_neighbor(self):
        from types import SimpleNamespace
        import roads
        class Snapshot:
            def __init__(self):
                self.layers={}
                for layer in ('buildings','building_parts','roads','infrastructure','water')+p.LAYERS:
                    with np.load(ROOT/'probes'/('building_fence_dry_creek_'+layer+'.npz')) as arrays:
                        self.layers[layer]=mapgeom.Features.from_arrays(layer,arrays,'')
            def cell(self,key,layers=(),**kwargs):
                return SimpleNamespace(layers={layer:self.layers.get(layer) for layer in layers})
            def versions(self):return {'snapshot':'2026-10-21.0'}
        md=Snapshot();frame=geodesy.EnuFrame(41.15960,-104.80802,0.)
        ground=lambda la,lo:np.full(np.shape(la),10.)
        items,custom=buildings.road_tile_items(frame,0,-3,md,ground)
        deferred=[]
        buildings.road_tile(frame,0,-3,md,ground,items=items,custom=custom,
                           fence_geometry=deferred,fence_geometry_deferred=True)
        self.assertTrue(deferred and 'deferred_mesh' in deferred[0])
        records=fb.nearby(frame,0,-3,md,ground,deferred)
        ids={record['source'].get('record') for record in records}
        self.assertIn('w233169879@2',ids)
        self.assertIn('w233169882@3',ids) # adjacent owner tile, not the tile rendering this fence
        index=fb.Index(records)
        kwargs=dict(frame=frame,area_fn=lambda k:[md.layers[x] for x in p.LAYERS],
            infra_fn=lambda k:md.layers['infrastructure'],box=roads.tile_bbox(0,-3),elev=ground,
            transport_fn=lambda k:md.layers['roads'],surface=lambda xy:np.full(len(xy),10.))
        before=p.rows(**kwargs)
        after=p.rows(**kwargs,building_fn=lambda:records)
        self.assertTrue(any(index.conflicts(np.array(r[1:3]),np.array(r[4:6])) for r in before))
        self.assertFalse(any(index.conflicts(np.array(r[1:3]),np.array(r[4:6])) for r in after))

    def test_real_geotile_frames_have_unique_complete_ownership(self):
        import geotiles
        gx,gy=geotiles.tile_of(41.15,-104.85)
        one,two=geotiles.GeoBox(gx,gy),geotiles.GeoBox(gx+1,gy)
        south,west,north,east=geotiles.info(gx,gy)[2:]
        width=east-west;height=north-south
        geo=np.array([[west+.1*width,south+.25*height],[east+.9*width,south+.25*height],
                      [east+.9*width,north-.25*height],[west+.1*width,north-.25*height],[west+.1*width,south+.25*height]])
        feature=mapgeom.build('perimeter_areas',np.array(['synthetic tile fixture']),[(mapgeom.POLYGON,[[geo]])])
        feature.attrs={'src_record':np.array(['w1@1']),'tags':np.array(['{"landuse":"industrial"}'])}
        def rows(frame,bounds):
            return p.rows(frame,lambda k:[feature],None,bounds,None,surface=lambda xy:np.zeros(len(xy)))
        a,b=rows(one.frame,one.box),rows(two.frame,two.box)
        all_rows=rows(one.frame,(-2000,-2000,2000,2000))
        self.assertFalse({r[7] for r in a}&{r[7] for r in b})
        self.assertEqual({r[7] for r in a+b},{r[7] for r in all_rows})


if __name__=='__main__':unittest.main()
