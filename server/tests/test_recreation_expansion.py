"""Nationwide recreation classification, park exit sources and protected-area caching."""
import copy
import json
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import numpy as np

from test_roads import FRAME, LAT, LON, M_LAT, M_LON, flat
import mapcells
import mapgeom
import osmexitdest
import osmparks
import rangeread
import recreationrules
import signroads
import travelboards


def ll(x,y=0.):return np.array([LON+x/M_LON,LAT+y/M_LAT])


class RecreationRulesTest(unittest.TestCase):
    def test_detailed_road_distances_are_built_once_for_boundary_sampling(self):
        path=np.array([ll(x) for x in np.linspace(-2000.,2000.,2001)])
        original=travelboards.at
        with patch.object(travelboards,'at',wraps=original) as distances:
            crossings=travelboards.transitions(path,lambda points:points[:,0]>=LON)
        self.assertEqual(len(crossings),1)
        self.assertAlmostEqual(crossings[0][0],2000.,delta=.01)
        self.assertLessEqual(distances.call_count,1)

    def test_prepared_forest_preserves_holes_and_disjoint_parts(self):
        outer=np.array([ll(-100,-100),ll(100,-100),ll(100,100),ll(-100,100)])
        hole=np.array([ll(-20,-20),ll(20,-20),ll(20,20),ll(-20,20)])
        island=outer+[500/M_LON,0]
        parts=[[outer,hole],[island]]
        prepared=travelboards.AreaIndex(parts)
        for point in (ll(0),ll(50),ll(500),ll(900),ll(50,200)):
            self.assertEqual(prepared.contains(point),travelboards.within(point,parts))
        self.assertFalse(prepared.crosses_box(ll(40,40),ll(60,60)))
        self.assertTrue(prepared.crosses_box(ll(-30,-30),ll(30,30)))

    def test_interior_roads_do_not_scan_large_forest_membership(self):
        angle=np.linspace(0,2*np.pi,20001)
        ring=np.array([LON,LAT])+np.c_[np.cos(angle)*10000/M_LON,np.sin(angle)*10000/M_LAT]
        area=dict(id='large-forest',name='Test National Forest',parts=[[ring]],lo=ring.min(0),hi=ring.max(0),
                  entrance=recreationrules.entrance('Test National Forest',{},'WY'))
        row=dict(fid='interior-road',wid=123,spec=signroads.Spec(8.,'secondary',False,0,False),path=np.array([ll(-1000),ll(1000)]))
        with patch.object(travelboards.AreaIndex,'contains_many',side_effect=AssertionError('interior road scanned')):
            self.assertEqual(travelboards.candidates([row],[area],welcomes=False),[])

    def test_board_tile_query_does_not_use_exit_junction_halo(self):
        import highwaysigns
        source=Mock()
        highwaysigns.tile_objects(source,FRAME,0,0)
        args=source.objects.call_args
        road_box=args.args[1];board_box=args.kwargs['board_box']
        self.assertLess(board_box[2]-board_box[0],road_box[2]-road_box[0])
        self.assertEqual(board_box,(-100.,-100.,356.,356.))

    def test_posted_destinations_not_park_named_towns_or_roads(self):
        for name in ('Custer State Park','Yellowstone National Park',"Rocky Mtn Nat'l Park",'Thunder Basin National Grassland'):
            self.assertTrue(recreationrules.destination(name),name)
        for name in ('Park City','National Park','State Park Road','Yellowstone National Park Road','Forest Park'):
            self.assertFalse(recreationrules.destination(name),name)

    def test_agencies_do_not_become_forest_service_by_default(self):
        cases=[('Medicine Bow-Routt National Forest',{},'usfs','NATIONAL FOREST'),
               ('Thunder Basin National Grassland',{},'usfs','NATIONAL GRASSLAND'),
               ('Yellowstone National Park',{},'nps','NATIONAL PARK'),
               ('Curt Gowdy State Park',{},None,'STATE PARK'),
               ('Vedauwoo',{'operator':'United States Forest Service'},'usfs','RECREATION AREA')]
        for name,tags,emblem,category in cases:
            with self.subTest(name=name):
                value=recreationrules.entrance(name,tags,'WY')
                self.assertEqual((value['emblem'],value['category']),(emblem,category))
        self.assertIsNone(recreationrules.entrance('Town Park',{},'WY'))
        self.assertIsNone(recreationrules.entrance('Foreign National Park',{},''))

    def test_all_state_names_and_welcome_wording_are_recognized(self):
        for code,info in travelboards.registry()['states'].items():
            for text in (f"Welcome to {info['name']}",f"{info['name']} Welcomes You",f"Welcome to | {info['name']} | a motto"):
                self.assertEqual(travelboards.welcome_state({'inscription':text}),code)
        self.assertIsNone(travelboards.welcome_state({'inscription':'Welcome to Colorado Springs'}))
        self.assertIsNone(travelboards.welcome_state({'inscription':'Welcome to Weld County | Colorado'}))
        self.assertEqual(travelboards.welcome_state({'inscription':'Welcome to the Free State of Florida'}),'FL')
        self.assertEqual(travelboards.welcome_state({'inscription':'The People of Iowa Welcome You'}),'IA')
        self.assertEqual(travelboards.welcome_state({'name':'North Dakota'}),'ND')

    def test_brown_exit_separate_from_city_routes_and_respects_exit_side(self):
        path=np.array([ll(-1800),ll(0)])
        obj=dict(id=17,lat=LAT,lon=LON,exit=dict(label=dict(ref='7',names=['Town','Custer State Park'],routes=['US 16']),
                 approach=path.tolist(),main_id=10,main_ids=[10],side=-1))
        before=copy.deepcopy(obj);boards=travelboards.park_exit_boards([obj])
        self.assertEqual(obj,before,'existing posted city/route information preserved')
        self.assertEqual(len(boards),1);extra=boards[0]['travelboard']
        self.assertEqual(extra['name'],'Custer State Park');self.assertEqual(extra['instruction'],'NEXT LEFT')
        sp=signroads.Spec(14.,'motorway',True,1,False)
        pavement=[('osm:w10:main',sp,np.array([[-1800.,0.,0.],[300.,0.,0.]]),14.)]
        placed=travelboards.place(FRAME,boards[0],SimpleNamespace(pavements=lambda *a:pavement),(-2000,-512,512,512),flat)
        self.assertIsNotNone(placed);self.assertGreater(placed['centre'][1],10.)
        self.assertEqual(travelboards.park_exit_boards([obj,*boards]),[],'no duplicate of an existing brown board')

    def test_split_access_roads_find_real_boundary_and_use_outside_road(self):
        sp=signroads.Spec(6.,'secondary',False,0,False)
        rows=[dict(fid=str(i),wid=i,spec=sp,path=np.array([ll(a),ll(b)])) for i,(a,b) in enumerate(((-400,0),(0,400)))]
        polygon=np.array([ll(0,-100),ll(500,-100),ll(500,100),ll(0,100),ll(0,-100)])
        style=recreationrules.entrance('Yellowstone National Park',{},'WY')
        area=dict(id='nps',name='Yellowstone National Park',category='NATIONAL PARK',entrance=style,
                  parts=[[polygon]],lo=polygon.min(0),hi=polygon.max(0))
        objects=travelboards.candidates(rows,[area],welcomes=False)
        self.assertEqual(len(objects),1);self.assertEqual(objects[0]['travelboard']['kind'],'forest_entrance')
        self.assertEqual(objects[0]['travelboard']['feature_id'],'0')
        self.assertAlmostEqual((objects[0]['lon']-LON)*M_LON,-65.,delta=.1)


class ExitSourceTest(unittest.TestCase):
    def test_runtime_sources_work_without_a_weekly_sign_index(self):
        sp=signroads.Spec(14.,'motorway',True,1,False)
        mains=[dict(fid='osm:w10:main',wid=10,spec=sp,path=np.array([ll(-1800),ll(400)]))]
        links=[dict(fid='osm:w20:ramp',wid=20,spec=sp._replace(link=True),path=np.array([ll(0),ll(100,-60)]))]
        ways={20:dict(nodes=[3,4],tags=dict(highway='motorway_link',oneway='yes',destination='Custer State Park'))}
        records=osmexitdest.records(links,mains,ways,{3:dict(tags={'ref':'7'})})
        self.assertEqual(len(travelboards.park_exit_boards(records)),1)
        self.assertEqual(osmexitdest.records(links,mains,ways,{}),[],'no invented exit number')
        ways[20]['tags']['destination']='Park City'
        self.assertEqual(osmexitdest.records(links,mains,ways,{3:dict(tags={'ref':'7'})}),[])

    def test_tag_reads_are_batched_cached_and_available_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            get=Mock(return_value=SimpleNamespace(body=json.dumps({'elements':[
                dict(type='way',id=1,nodes=[3,4],tags={'destination':'Custer State Park'}),
                dict(type='way',id=2,nodes=[5,6],tags={})]}).encode()))
            md=SimpleNamespace(cache_dir=tmp,fetcher=SimpleNamespace(get=get));source=osmexitdest.Tags(md)
            expected=source.read('way',[2,1]);self.assertEqual(len(expected),2)
            self.assertEqual(source.read('way',[1,2]),expected);get.assert_called_once()
            for path in (Path(tmp)/'osm-exit-destinations-v1/way').glob('*.json'):os.utime(path,(0,0))
            get.side_effect=OSError('offline')
            self.assertEqual(osmexitdest.Tags(md).read('way',[1,2]),expected)


class ProtectedAreasTest(unittest.TestCase):
    def test_whole_polygons_agencies_and_disk_snapshot(self):
        import duckdb
        with tempfile.TemporaryDirectory() as tmp:
            p=osmparks.Parks(SimpleNamespace(cache_dir=tmp,raw_dir=tmp,fetcher=None))
            ring=np.array([[-105.5,41.],[-105.4,41.],[-105.4,41.1],[-105.5,41.1],[-105.5,41.]])
            wkb=struct.pack('<BIII',1,3,1,len(ring))+ring.astype('<f8').tobytes()
            path=Path(tmp)/'parks.parquet';con=duckdb.connect()
            try:
                con.execute("""COPY (SELECT 'relation' AS type, 5::BIGINT AS id, ['Medicine Bow National Forest'] AS name,
                  'United States Forest Service' AS operator, 'National Forest' AS protection_title, '' AS access,
                  'protected_area' AS boundary, '' AS leisure,
                  {'xmin':-105.5,'ymin':41.,'xmax':-105.4,'ymax':41.1} AS bbox,
                  unhex(?) AS geometry) TO '"""+path.as_posix()+"' (FORMAT parquet)",[wkb.hex()])
            finally:con.close()
            remote=rangeread.MemoryRange(path.read_bytes());remote.cache_dir=tmp
            p.version=lambda kind:('fixture',remote)
            key=mapcells.cell_of(41.05,-105.45);f=p.cell(key)
            self.assertEqual(len(f),1);np.testing.assert_array_equal(f.parts(0)[0][0],ring)
            self.assertIn('Forest Service',json.loads(f.attrs['tags'][0])['operator'])
            again=osmparks.Parks(SimpleNamespace(cache_dir=tmp,raw_dir=tmp,fetcher=None))
            again.version=Mock(side_effect=OSError('offline'))
            self.assertEqual(len(again.cell(key)),1);again.version.assert_not_called()
            for snapshot in (Path(tmp)/'protected-areas-v1').glob('*.npz'):os.utime(snapshot,(0,0))
            self.assertEqual(len(again.cell(key)),1)
