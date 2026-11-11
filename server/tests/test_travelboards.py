"""Real welcome assets, mapped recreation entrances and streaming placement."""
from pathlib import Path
import sys,json,tempfile,hashlib
from types import SimpleNamespace
from unittest import mock
import unittest
from concurrent.futures import CancelledError
import numpy as np
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import travelboards as signs
import signstates,signroads,mapgeom,mapcells,overture,mapdata
from test_roads import features,rules,FRAME,flat,LON,LAT,M_LON,M_LAT

def ll(x,y=0):return np.array([LON+x/M_LON,LAT+y/M_LAT])
def states(lat,lon):return np.where(np.asarray(lon)<LON,'CO','WY')
def rows(split=False,oneway=False,cls='primary'):
    pieces=[(-400.,0.),(0.,400.)] if split else [(-400.,400.)]
    f=features([[(a,0),(b,0)] for a,b in pieces],[cls]*len(pieces));f.attrs['src_record']=np.array([f'w{10+i}@1' for i in range(len(pieces))])
    if oneway:f.rules['oneway']=rules([(i,1.) for i in range(len(pieces))],'float')
    specs=signroads.cell_specs(f)
    return [dict(fid=signroads.feature_id(f,i),wid=10+i,spec=specs[i],path=f.parts(i)[0][0],routes=[('us:wy','210',0.,1.)]) for i in range(len(f))]

def welcome():
    with mock.patch.object(signstates,'states',side_effect=states),mock.patch.object(signs,'boundary_near',return_value=True):
        return signs.candidates(rows())

def area(private=False):
    ring=np.array([ll(0,-100),ll(500,-100),ll(500,100),ll(0,100),ll(0,-100)])
    f=mapgeom.build('recreation_areas',np.array(['park']),[(mapgeom.POLYGON,[[ring]])])
    f.attrs.update(name=np.array(['Test State Park']),tags=np.array([json.dumps(dict(leisure='park',access='private' if private else 'yes'))]),**{'class':np.array(['park'])})
    return f,dict(id='park',name='Test State Park',category='STATE PARK',parts=f.parts(0),lo=ring.min(0),hi=ring.max(0))

class TravelBoardsTest(unittest.TestCase):
    def test_mapped_i25_colorado_and_wyoming_exact_positions_and_facing(self):
        import geodesy
        fixture=json.loads((Path(__file__).parent/'fixtures/i25_welcome_nodes.json').read_text())
        frame=geodesy.EnuFrame(41.00211,-104.90629,0.)
        rows=[];pavements=[]
        for road in fixture['roads']:
            sp=signroads.Spec(14.,'motorway',True,1,False,0.,())
            fid=f"osm:w{road['wid']}:{road['fid']}";path=np.asarray(road['path'])
            rows.append(dict(fid=fid,wid=road['wid'],spec=sp,path=path,routes=[('us:i','25',0.,1.)]))
            e,n,_=frame.geodetic_to_enu(path[:,1],path[:,0],0.)
            pavements.append((fid,sp,np.c_[e,n,np.zeros(len(e))],14.))
        objects=signs.mapped_welcomes(fixture['nodes'],rows)
        self.assertEqual({o['travelboard']['state'] for o in objects},{'CO','WY'})
        context=SimpleNamespace(pavements=lambda *a:pavements)
        for obj in objects:
            node=next(n for n in fixture['nodes'] if n['id']==obj['travelboard']['osm_node_id'])
            self.assertEqual((obj['lat'],obj['lon']),(node['lat'],node['lon']))
            placed=signs.place(frame,obj,context,(-1024,-1024,1024,1024),flat)
            self.assertIsNotNone(placed)
            lat,lon,_=frame.enu_to_geodetic(*placed['centre'][:2],0.)
            self.assertAlmostEqual(float(lat),node['lat'],places=8)
            self.assertAlmostEqual(float(lon),node['lon'],places=8)
            self.assertGreater(placed['travel'][1] if obj['travelboard']['state']=='WY' else -placed['travel'][1],.98)
            # Wyoming's mapped sign stands in Colorado: its inscription wins.
            if obj['travelboard']['state']=='WY':self.assertEqual(signstates.states(lat,lon).item(),'CO')
            self.assertIsNone(signs.place(frame,obj,context,(-1024,-1024,1024,1024),flat,
                                         obstacles=SimpleNamespace(clear=lambda *a:False)))
            x,y=placed['centre'][:2];self.assertIsNone(signs.place(frame,obj,context,(x+1,y+1,x+257,y+257),flat))

    def test_mapped_local_welcomes_and_unverified_designs_do_not_become_state_signs(self):
        node=dict(id=1,lon=float(ll(-100,-18)[0]),lat=float(ll(-100,-18)[1]))
        for text in ('Welcome to Colorado Springs','Welcome to Weld County Colorado','Welcome to Atlantis','Welcome'):
            with self.subTest(text=text):
                self.assertEqual(signs.mapped_welcomes([dict(node,tags=dict(board_type='welcome_sign',inscription=text))],rows()),[])
        self.assertEqual(signs.mapped_welcomes([dict(node,tags=dict(inscription='Welcome to Wyoming'))],rows()),[])

    def test_source_keeps_ground_sections_of_a_road_with_a_bridge(self):
        f=features([[(-400.,0.),(400.,0.)]],['primary'],flags=[(0,'is_bridge',.8,.9)])
        f.attrs['src_record']=np.array(['w10@1'])
        f.attrs['routes_txt']=np.array(['us:wy|210|0|1'])
        node=dict(id=55,lon=float(ll(-100,-18)[0]),lat=float(ll(-100,-18)[1]),
                  tags=dict(board_type='welcome_sign',inscription='Welcome to Wyoming'))
        md=SimpleNamespace(cell=lambda key,layers,kind:SimpleNamespace(layers={'roads':f,'road_routes':f}))
        source=signs.Source(md);source.mapped=SimpleNamespace(cell=lambda key:[node])
        with mock.patch.object(signs,'boundary_near',return_value=True),mock.patch.object(signs.signstates,'_polygons',
             return_value=signstates._Polygons([('WY',ll(-1000,-1000),ll(1000,1000),[])])):
            objects=source.query(LON-.004,LAT-.001,LON+.004,LAT+.001)
        self.assertEqual(len(objects),1)
        self.assertTrue(objects[0]['travelboard']['mapped'])
        self.assertEqual(objects[0]['travelboard']['osm_node_id'],55)

    def test_recreation_exit243_photo_board_faces_northbound_off_the_road(self):
        import geodesy
        fixture=json.loads((Path(__file__).parent/'fixtures/i25_exit243_recreation.json').read_text())
        definition=signs.guide_registry()[0];frame=geodesy.EnuFrame(definition['lat'],definition['lon'],0.)
        rows=[];pavements=[]
        for road in fixture['roads']:
            path=np.asarray(road['path']);fid=f"osm:w{road['wid']}:fixture"
            sp=signroads.Spec(18.,'motorway',True,1,False,0.,())
            rows.append(dict(fid=fid,wid=road['wid'],spec=sp,path=path))
            e,n,_=frame.geodetic_to_enu(path[:,1],path[:,0],0.)
            pavements.append((fid,sp,np.c_[e,n,np.zeros(len(e))],18.))
        objects=signs.recreation_guides(rows)
        self.assertEqual(len(objects),1)
        obj=objects[0];self.assertEqual(obj['travelboard']['main_id'],647810729)
        self.assertEqual(obj['travelboard']['exit'],'243')
        self.assertEqual(obj['travelboard']['lines'],['Rocky Mtn',"Nat'l Park"])
        self.assertEqual(obj['travelboard']['instruction'],'NEXT RIGHT')
        self.assertEqual(signs.recreation_guides(rows[1:]),[], 'no southbound substitute')
        context=SimpleNamespace(pavements=lambda *a:pavements)
        placed=signs.place(frame,obj,context,(-512,-512,512,512),flat)
        self.assertIsNotNone(placed);self.assertGreater(placed['travel'][1],.99)
        self.assertTrue(signs.exitsigns._clear(np.asarray(placed['centre'][:2]),placed['width']/2+.6,
                                             signs.exitsigns._outlines(pavements)))
        with tempfile.TemporaryDirectory() as tmp:
            faces={};mesh=signs.render([placed],(0,0,0),tmp,'/cache',faces)
            self.assertNotIn('bne_recreation_brown_opaque',faces)
            brown=mesh[3].index('bne_recreation_brown_opaque')
            self.assertTrue((mesh[4]==brown).any())
            self.assertTrue((mesh[4]==mesh[3].index('bne_guide_letters')).any())
        self.assertIsNone(signs.place(frame,obj,context,(512,512,768,768),flat))
        # A photograph at this station never becomes an arbitrary repeating sign.
        far=[dict(r,path=r['path']+[0,1]) for r in rows]
        self.assertEqual(signs.recreation_guides(far),[])

    def test_vedauwoo_forest_entrance_style_and_local_access_placement(self):
        import geodesy,forestboards
        definition=next(g for g in signs.guide_registry() if g.get('kind')=='forest_entrance')
        frame=geodesy.EnuFrame(definition['lat'],definition['lon'],0.)
        # Captured from mapped OSM access way 15674422, not the nearby motorway.
        path=np.array([[-105.3818331,41.1546464],[-105.3817192,41.1548926],
                       [-105.3816071,41.1550764],[-105.3815473,41.1551645],[-105.381243,41.1554944]])
        sp=signroads.Spec(6.,'unclassified',False,0,False,0.,())
        row=dict(fid='osm:w15674422:vedauwoo',wid=15674422,spec=sp,path=path)
        objects=signs.recreation_guides([row],[definition]);self.assertEqual(len(objects),1)
        self.assertEqual(objects[0]['travelboard']['location_status'],'approximate_access_road')
        self.assertEqual(signs.recreation_guides([dict(row,spec=sp._replace(cls='motorway',oneway=True,dir=1))],[definition]),[])
        self.assertEqual(signs.recreation_guides([dict(row,spec=sp._replace(oneway=True,dir=-1))],[definition]),[])
        e,n,_=frame.geodetic_to_enu(path[:,1],path[:,0],0.);line=np.c_[e,n,np.zeros(len(e))]
        pavement=[(row['fid'],sp,line,6.)]
        placed=signs.place(frame,objects[0],SimpleNamespace(pavements=lambda *a:pavement),(-256,-256,256,256),flat)
        self.assertIsNotNone(placed);self.assertLess(placed['bottom'],1.3)
        with tempfile.TemporaryDirectory() as tmp:
            faces={};mesh=signs.render([placed],(0,0,0),tmp,'/cache',faces)
            self.assertEqual(faces,{},'solid static panels need no generated picture material')
            for material in ('bne_forest_cream','bne_forest_timber','bne_forest_shield','bne_forest_letters_cream'):
                self.assertIn(material,mesh[3])
            for name in ('bne_forest_cream','bne_recreation_brown_opaque','bne_forest_shield'):
                triangles=mesh[0][mesh[1][mesh[4]==mesh[3].index(name)]]
                normal=np.cross(triangles[:,1]-triangles[:,0],triangles[:,2]-triangles[:,0])
                self.assertTrue(np.all(normal@np.r_[-np.array(placed['travel']),0.]>=-1e-8))
        outline=forestboards.outline(4.8,0,2.6)
        self.assertLess(np.ptp(outline[outline[:,1]<.2,0]),np.ptp(outline[outline[:,1]>2.4,0]))
        root=Path(__file__).resolve().parents[2]/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/roadside'
        materials=json.loads((root/'exit.materials.json').read_text())
        for name in ('bne_forest_cream','bne_forest_timber','bne_forest_shield'):
            self.assertFalse(materials[name]['translucent'])
        self.assertTrue((root/'usfs-shield.png').is_file());self.assertIn('Public-domain',(root/'USFS-NOTICE.md').read_text())

    def test_current_assets_are_state_specific_and_pending_states_get_no_generic(self):
        self.assertEqual(len(signs.registry()['states']),51)
        enabled=[code for code in signs.registry()['states'] if signs.current(code)]
        self.assertEqual(len(enabled),49)
        self.assertEqual(signs.registry()['states']['HI']['status'],'not_applicable_no_land_border')
        self.assertEqual(signs.current('WY')['design'],'2024 scenic postcard')
        self.assertIn('Colorful',signs.current('CO')['design'])
        self.assertIsNone(signs.current('ZZ'))
        self.assertEqual(signs.current('OH')['status'],'newest_available')
        with mock.patch.object(signs,'registry',return_value=dict(signs.registry(),policy='current_verified_only')):
            self.assertIsNone(signs.current('OH'))
        self.assertNotEqual(signs.current('CO')['image'],signs.current('WY')['image'])

    def test_real_state_crossings_and_connected_border_segments(self):
        for split in (False,True):
            with mock.patch.object(signstates,'states',side_effect=states),mock.patch.object(signs,'boundary_near',return_value=True):
                objects=signs.candidates(rows(split=split))
            self.assertEqual(len(objects),2)
            self.assertEqual({o['travelboard']['state'] for o in objects},{'CO','WY'})
            for o in objects:self.assertAlmostEqual(abs((o['lon']-LON)*M_LON),90.,delta=.1)

    def test_no_unknown_or_international_or_wrong_way_welcomes(self):
        with mock.patch.object(signstates,'states',side_effect=states),mock.patch.object(signs,'boundary_near',return_value=True):
            self.assertEqual(len(signs.candidates(rows(oneway=True))),1)
        with mock.patch.object(signstates,'states',side_effect=lambda lat,lon:np.where(np.asarray(lon)<LON,'','WY')),mock.patch.object(signs,'boundary_near',return_value=True):
            self.assertEqual(signs.candidates(rows()),[])
        with mock.patch.object(signstates,'states',side_effect=lambda lat,lon:np.where(np.asarray(lon)<LON,'ZZ','XX')),mock.patch.object(signs,'boundary_near',return_value=True):
            self.assertEqual(signs.candidates(rows()),[])

    def test_actual_area_entry_not_centroid_and_no_motorway_park_signs(self):
        _,park=area()
        result=signs.candidates(rows(cls='tertiary'),[park],welcomes=False)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['travelboard']['name'],'Test State Park')
        self.assertAlmostEqual((result[0]['lon']-LON)*M_LON,-65.,delta=.1)
        self.assertEqual(signs.candidates(rows(cls='motorway'),[park],welcomes=False),[])
        park['lo']=ll(0,100);park['hi']=ll(500,200)
        self.assertEqual(signs.candidates(rows(cls='tertiary'),[park],welcomes=False),[])

    def test_holes_and_unclipped_optional_geometry(self):
        outer=np.array([ll(-100,-100),ll(100,-100),ll(100,100),ll(-100,100)])
        hole=np.array([ll(-20,-20),ll(20,-20),ll(20,20),ll(-20,20)])
        self.assertFalse(signs.within(ll(0),[[outer,hole]]));self.assertTrue(signs.within(ll(50),[[outer,hole]]))
        self.assertFalse(overture.LAYERS['recreation_areas']['clip'])
        self.assertIn('recreation_areas',mapdata.OPTIONAL_LAYERS);self.assertNotIn('recreation_areas',mapdata.LAYERS)

    def test_projective_texture_mapping_matches_all_reference_corners(self):
        for state in signs.registry()['states']:
            if not signs.current(state):continue
            corners=signs.current(state)['uv_corners'];transform=signs.homography(corners)
            for xy,expected in zip(((0,0),(1,0),(1,1),(0,1)),corners):np.testing.assert_allclose(transform(*xy),expected,atol=1e-12)

    def test_enabled_artwork_integrity_and_cutout_area(self):
        for state in signs.registry()['states']:
            info=signs.current(state)
            if not info:continue
            with self.subTest(state=state):
                self.assertEqual(hashlib.sha256((signs.DATA/info['image']).read_bytes()).hexdigest(),info['source_sha256'])
                self.assertGreater(info['width_m'],0);self.assertGreater(info['height_m'],0)
                corners=np.asarray(info['uv_corners'])
                self.assertTrue(np.isfinite(corners).all())
                if not info.get('outline'):continue
                contour,triangles=signs.welcome_outline(tuple(map(tuple,info['outline'])))
                a,b,c=contour[triangles].transpose(1,0,2)
                ab,ac=b-a,c-a
                triangle_area=np.abs(ab[:,0]*ac[:,1]-ab[:,1]*ac[:,0]).sum()/2
                polygon_area=abs(np.sum(contour[:,0]*np.roll(contour[:,1],-1)-contour[:,1]*np.roll(contour[:,0],-1)))/2
                self.assertAlmostEqual(triangle_area,polygon_area)

    def test_face_cache_and_checksum_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            first=signs.face('CO',tmp,'/cache');self.assertTrue((Path(tmp)/Path(first[1]).name).is_file())
            with mock.patch.object(signs.imagery,'write_dds') as write:
                self.assertEqual(signs.face('CO',tmp,'/cache'),first);write.assert_not_called()
            self.assertIsNone(signs.face('ZZ',tmp,'/cache'))
            with mock.patch.object(signs,'current',return_value=dict(signs.current('CO'),source_sha256='broken')):
                with self.assertRaises(ValueError):signs.face('CO',tmp,'/cache')

    def test_terrain_facing_owner_and_obstacles(self):
        obj=next(o for o in welcome() if o['travelboard']['state']=='WY')
        sp=signroads.Spec(8.,'primary',False,0,False,0.,())
        pavement=[('osm:w10:id000',sp,np.array([[-400.,0.,0.],[400.,0.,0.]]),8.)]
        context=SimpleNamespace(pavements=lambda *a:pavement)
        with mock.patch.object(signstates,'states',side_effect=states):
            placed=signs.place(FRAME,obj,context,(0.,-512.,512.,0.),flat)
            self.assertIsNotNone(placed)
            self.assertIsNone(signs.place(FRAME,obj,context,(512.,-512.,1024.,0.),flat))
            self.assertIsNone(signs.place(FRAME,obj,context,(-512.,-512.,512.,512.),flat,obstacles=SimpleNamespace(clear=lambda *a:False)))
            self.assertIsNone(signs.place(FRAME,obj,context,(-512.,-512.,512.,512.),lambda lat,lon:np.full(np.shape(lat),np.nan)))
        self.assertGreater(placed['travel'][0],.99);self.assertLess(placed['centre'][1],-7.)
        with tempfile.TemporaryDirectory() as tmp:
            faces={};mesh=signs.render([placed],(0,0,0),tmp,'/cache',faces)
            self.assertEqual(len(faces),1)
            mat=mesh[3].index(next(iter(faces)));p=mesh[0][mesh[1][mesh[4]==mat]]
            self.assertTrue(np.all(np.cross(p[:,1]-p[:,0],p[:,2]-p[:,0])[:,0]<0))
            uv=mesh[2][mesh[1][mesh[4]==mat]];self.assertTrue(np.all((uv>=0)&(uv<=1)))

    def test_normal_streaming_source_and_private_area_filter(self):
        f=features([[(-400.,0.),(400.,0.)]],['tertiary']);f.attrs['src_record']=np.array(['w10@1'])
        park,_=area()
        cell=mock.Mock(side_effect=lambda key,layers,kind:SimpleNamespace(layers={'roads':f} if layers==('roads',) else {'recreation_areas':park,'road_routes':mapgeom.empty('road_routes'),'town_areas':mapgeom.empty('town_areas')}))
        source=signs.Source(SimpleNamespace(cell=cell))
        bounds=(LON-.004,LAT-.001,LON+.004,LAT+.001)
        objects=source.query(*bounds)
        self.assertEqual(len(objects),1)
        self.assertEqual(objects[0]['travelboard']['kind'],'forest_entrance')
        self.assertEqual(objects[0]['travelboard']['category'],'STATE PARK')
        self.assertIsNone(objects[0]['travelboard']['emblem'])
        calls=cell.call_count
        self.assertEqual(source.query(*bounds),objects)
        self.assertEqual(cell.call_count,calls)
        self.assertEqual({o['id'] for o in objects},{o['id'] for o in source.query(LON-.004,LAT-.001,LON,LAT+.001)+source.query(LON,LAT-.001,LON+.004,LAT+.001)})
        private,_=area(private=True)
        source=signs.Source(SimpleNamespace(cell=lambda key,layers,kind:SimpleNamespace(layers={'roads':f} if layers==('roads',) else {'recreation_areas':private})))
        self.assertEqual(source.query(*bounds),[])

    def test_existing_sign_stream_registers_welcome_material_without_direction_option(self):
        import highwaysigns,signstream,signplacement,roads
        from test_highway_signs import fixture
        with tempfile.TemporaryDirectory() as tmp:
            md=fixture(tmp)
            path=[]
            for x in (90.,210.):
                lat,lon,_=FRAME.enu_to_geodetic(x,100.,0.);path.append([float(lon),float(lat)])
            lat,lon,_=FRAME.enu_to_geodetic(150.,100.,0.)
            obj=dict(id=-999,lat=float(lat),lon=float(lon),ways=[],tags={},travelboard=dict(kind='welcome',state='WY',feature_id='osm:w10:0',main_id=10,axis=[1.,0.],approach=path))
            tx,ty=int(150//roads.TILE),int(100//roads.TILE)
            with mock.patch.object(highwaysigns,'tile_objects',return_value=[obj]),mock.patch.object(highwaysigns,'guide_layout',return_value=([],[])),mock.patch.object(signplacement,'for_tile',return_value=signplacement.Context()),mock.patch.object(signstream,'_speeds',return_value=None),mock.patch.object(signstates,'states',side_effect=lambda lat,lon:np.full(np.shape(lat),'WY')):
                result=signstream.build_tile(md,FRAME,7,tx,ty,flat,tmp,'/cache',dirs=False)
            manifest=json.loads((Path(tmp)/Path(result[0]).name).read_text())
            self.assertIn('exits',manifest)
            self.assertTrue(any(name.startswith('bne_welcome_') for name in manifest['dirs_faces']))

    def test_cancellation_and_missing_roads_are_not_frozen(self):
        cell=mock.Mock(side_effect=CancelledError());source=signs.Source(SimpleNamespace(cell=cell))
        with self.assertRaises(CancelledError):source._cell(mapcells.cell_of(LAT,LON))
        self.assertFalse(source.cache)
        cell.side_effect=None;cell.return_value=SimpleNamespace(layers={})
        self.assertEqual(source._cell(mapcells.cell_of(LAT,LON)),[]);self.assertFalse(source.cache)

    def test_recreation_mesh_uses_explicitly_opaque_static_material(self):
        sign=dict(extra=dict(kind='recreation',name='Test State Park',category='STATE PARK'),centre=[0,0,0],travel=[0,1],width=4.,height=2.,bottom=2.2,posts=[-1.2,1.2],bases=[0,0])
        with tempfile.TemporaryDirectory() as tmp:
            faces={};mesh=signs.render([sign],(0,0,0),tmp,'/cache',faces)
            self.assertIn('bne_recreation_brown_opaque',mesh[3]);self.assertEqual(faces,{})
            path=Path(__file__).resolve().parents[2]/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/roadside/exit.materials.json'
            material=json.loads(path.read_text())['bne_recreation_brown_opaque']
            self.assertFalse(material['translucent']);self.assertFalse(material['alphaTest'])
            self.assertEqual(material['Stages'][0]['baseColorFactor'][3],1.)

if __name__=='__main__':unittest.main()
