"""Real reference numbering, source calibration, shoulder ownership and generated panels."""
import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import exitsigns
import geodesy
import milemarkers
import osmsigns as osmobjects
import signroads as roadcontext
import signroads as roadlanes
import roads
import signstream

FRAME = geodesy.EnuFrame(42.82,-84.27)


def ll(x,y):
    lat,lon,_ = FRAME.enu_to_geodetic(x,y,0.)
    return [float(lon),float(lat)]


def marker(x=120.,y=0.,direction='E',mile=116,source='osm',state='MI'):
    return milemarkers._object(state,69,mile,direction,ll(x,y),milemarkers.AXES[direction],source)


def pavement(direction=1,y=0.,start=-500.,end=1200.):
    spec = roads.Spec(7.4,'paved',False,'motorway',True,2.5,direction,110.,
                      lanes=(2,0,0) if direction > 0 else (0,2,0))
    line = np.array([[start,y,0.],[end,y,0.]])
    line,width = roadlanes.pavement(spec,line,FRAME)
    return ('main'+str(direction),roadlanes.spec(spec),line,width)


def flat(lat,lon):
    return np.zeros_like(np.asarray(lat,float))


class ReferenceTest(unittest.TestCase):
    def test_displayed_mileage_and_posted_direction_come_from_real_references(self):
        refs,_ = milemarkers.references()
        point, = [o for o in refs if o['mile_marker']['state']=='MI' and
                  o['mile_marker']['route']==69 and o['mile_marker']['mile']==116 and
                  o['mile_marker']['direction']=='W']
        self.assertAlmostEqual(point['lat'],42.9149444,places=6)
        self.assertAlmostEqual(point['lon'],-84.0395286,places=6)
        self.assertLess(point['mile_marker']['axis'][0],-.7)
        ky = {o['mile_marker']['mile'] for o in refs if o['mile_marker']['state']=='KY'
              and o['mile_marker']['route']==65 and o['mile_marker']['direction']=='N'}
        self.assertTrue(set(range(1,137)) <= ky)

    def test_units_are_not_silently_confused(self):
        self.assertEqual(milemarkers.miles('116 mi'),116.)
        self.assertAlmostEqual(milemarkers.miles('1.609344'),1.)
        self.assertAlmostEqual(milemarkers.miles('1.609344 km'),1.)
        for bad in ('116 miles;117 miles','unknown','-1 mi','nan','116ft'):
            self.assertIsNone(milemarkers.miles(bad))
        self.assertEqual(milemarkers.interstate('I 69 West'),69)
        self.assertIsNone(milemarkers.interstate('US 69'))
        self.assertEqual(milemarkers.cardinal('I 69 WB'),'W')

    def test_lookup_has_stable_numbers_across_anchor_changes(self):
        refs,_ = milemarkers.references()
        obj = next(o for o in refs if o['mile_marker']['state']=='MI' and
                   o['mile_marker']['route']==69 and o['mile_marker']['mile']==116)
        found = []
        for shift in (0.,.02):
            frame = geodesy.EnuFrame(obj['lat']+shift,obj['lon']+shift)
            x,y,_ = frame.geodetic_to_enu(obj['lat'],obj['lon'],0.)
            result = milemarkers.query(frame,(x-100.,y-100.,x+100.,y+100.))
            found.append({o['id'] for o in result})
        self.assertEqual(found[0],found[1])
        self.assertIn(obj['id'],found[0])

    def test_missing_optional_snapshot_keeps_roads_available(self):
        milemarkers.references.cache_clear()
        try:
            with mock.patch.object(milemarkers,'DATA',Path('absent-mile-reference.json')):
                with self.assertLogs('earthd.milemarkers',level='WARNING'):
                    self.assertEqual(milemarkers.query(FRAME,(0.,0.,256.,256.)),[])
        finally:
            milemarkers.references.cache_clear()


class CalibrationTest(unittest.TestCase):
    def fixture(self,reverse=False):
        coords = {i:ll((i-1)*milemarkers.MILE_M,0.) for i in range(1,6)}
        tags = dict(highway='motorway',oneway='-1' if reverse else 'yes',ref='I 69')
        ways = [(10,[3,2,1] if reverse else [1,2,3],tags),
                (11,[5,4,3] if reverse else [3,4,5],tags)]
        marks = {100:(*coords[1],dict(highway='milestone',distance='10 mi')),
                 101:(*coords[5],dict(highway='milestone',distance='14 mi'))}
        routes = [(10,69,'E','MI'),(11,69,'E','MI')]
        return ways,coords,marks,routes

    def test_whole_miles_follow_connected_ways_and_do_not_restart_at_way_boundaries(self):
        ways,coords,marks,routes = self.fixture()
        out = milemarkers.osm_records(ways,coords,marks,routes)
        self.assertEqual(sorted(o['mile_marker']['mile'] for o in out),list(range(10,15)))
        for obj in out:
            self.assertEqual(obj['mile_marker']['direction'],'E')
            p = exitsigns._xy(FRAME,[[obj['lon'],obj['lat']]])[0]
            self.assertAlmostEqual(p[0],(obj['mile_marker']['mile']-10)*milemarkers.MILE_M,delta=.2)
        again = milemarkers.osm_records(*self.fixture(reverse=True))
        self.assertEqual(out,again)

    def test_single_anchor_does_not_invent_the_rest_of_a_route(self):
        ways,coords,marks,routes = self.fixture()
        out = milemarkers.osm_records(ways,coords,{100:marks[100]},routes)
        self.assertEqual([o['mile_marker']['mile'] for o in out],[10])

    def test_state_reset_and_inconsistent_distance_do_not_interpolate(self):
        ways,coords,marks,routes = self.fixture()
        marks[101] = (*coords[5],dict(highway='milestone',distance='1 mi'))
        out = milemarkers.osm_records(ways,coords,marks,routes)
        self.assertEqual(sorted(o['mile_marker']['mile'] for o in out),[1,10])

    def test_unknown_fork_does_not_choose_a_branch(self):
        ways,coords,marks,routes = self.fixture()
        coords[6] = ll(milemarkers.MILE_M*4.,milemarkers.MILE_M)
        ways.append((12,[3,6],dict(highway='motorway',oneway='yes',ref='I 69')))
        routes.append((12,69,'E','MI'))
        out = milemarkers.osm_records(ways,coords,marks,routes)
        self.assertEqual(sorted(o['mile_marker']['mile'] for o in out),[10,14])

    def test_non_interstate_milestones_do_not_get_an_interstate_shield(self):
        ways,coords,marks,_ = self.fixture()
        for _,_,tags in ways:
            tags['ref'] = 'US 69'
        self.assertEqual(milemarkers.osm_records(ways,coords,marks),[])

    def test_binary_pbf_import_preserves_mileposts_and_exit_index(self):
        import osmium
        ways,coords,marks,_ = self.fixture()
        xml = ['<osm version="0.6">']
        for nid,(lon,lat) in coords.items():
            tags = '<tag k="highway" v="milestone"/><tag k="distance" v="'+('10 mi' if nid==1 else '14 mi')+'"/>' if nid in (1,5) else ''
            xml.append(f'<node id="{nid}" version="1" lon="{lon}" lat="{lat}">{tags}</node>')
        for wid,nodes,tags in ways:
            xml.append(f'<way id="{wid}" version="1">'+''.join(f'<nd ref="{n}"/>' for n in nodes)+
                       ''.join(f'<tag k="{k}" v="{v}"/>' for k,v in tags.items())+'</way>')
        xml.append('<relation id="20" version="1"><member type="way" ref="10" role=""/><member type="way" ref="11" role=""/>'+
                   '<tag k="type" v="route"/><tag k="route" v="road"/><tag k="network" v="US:I"/><tag k="ref" v="69"/><tag k="direction" v="east"/><tag k="is_in:state" v="MI"/></relation></osm>')
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'roads.osm'
            source.write_text(''.join(xml),encoding='utf-8')
            pbf,db = Path(tmp)/'roads.osm.pbf',Path(tmp)/'objects.sqlite'
            with osmium.SimpleWriter(str(pbf)) as writer:
                for obj in osmium.FileProcessor(str(source)):
                    writer.add(obj)
            osmobjects.import_pbf(pbf,db)
            with closing(sqlite3.connect(db)) as con:
                got = [json.loads(row[0])[0] for row in con.execute("SELECT ways FROM objects WHERE kind='mile_marker'")]
            self.assertEqual(sorted(row['mile'] for row in got),list(range(10,15)))
            self.assertTrue(all(row['direction']=='E' for row in got))


class PlacementTest(unittest.TestCase):
    def test_marker_clears_the_full_shoulder_and_faces_its_own_carriageway(self):
        ribbons = [pavement(1,0.),pavement(-1,35.)]
        objects = [marker(),marker(y=35.,direction='W')]
        signs = milemarkers.place(FRAME,objects,ribbons,(-500.,-100.,1000.,100.),flat)
        self.assertEqual(len(signs),2)
        east = next(s for s in signs if s['direction']=='EAST')
        west = next(s for s in signs if s['direction']=='WEST')
        self.assertLess(east['centre'][1],-ribbons[0][3]/2.)
        self.assertGreater(west['centre'][1],35.+ribbons[1][3]/2.)
        self.assertGreater(east['travel'][0],.99)
        self.assertLess(west['travel'][0],-.99)

    def test_one_marker_across_adjacent_tiles_even_with_duplicate_sources(self):
        obj = marker(x=256.)
        copy_osm = copy.deepcopy(obj)
        copy_osm['id'] -= 100
        copy_osm['mile_marker']['state'] = ''
        copy_dot = copy.deepcopy(obj)
        copy_dot['mile_marker']['source'] = 'dot:mi'
        signs = [s for tx in (0,1) for ty in (-1,0)
                 for s in milemarkers.place(FRAME,[copy_osm,copy_dot,copy_osm],
                                            [pavement()],roads.tile_bbox(tx,ty),flat)]
        self.assertEqual(len(signs),1)
        self.assertEqual(signs[0]['source'],'dot:mi')

    def test_ramps_and_unmatched_carriageways_cannot_claim_marker(self):
        fid,spec,line,width = pavement()
        ribbons = [(fid,spec._replace(link=True),line,width)]
        self.assertEqual(milemarkers.place(FRAME,[marker()],ribbons,(-500.,-100.,1000.,100.),flat),[])
        self.assertEqual(milemarkers.place(FRAME,[marker(direction='W')],[pavement()],(-500.,-100.,1000.,100.),flat),[])

    def test_source_neighbourhood_has_one_owner_across_tile_boundaries(self):
        from test_roads import features,rules,FRAME as frame
        feats = features([[(-300.,0.),(700.,0.)]],['motorway'])
        feats.rules['oneway'] = rules([(0,1.)],'float')
        feats.attrs['osm_lanes'] = np.array([2.])
        ring = feats.parts(0)[0][0]
        point = ring[0]*(1.-.42)+ring[-1]*.42
        obj = milemarkers._object('MI',69,116,'E',point,[1.,0.],'dot:mi')
        context = roadcontext.Context(lambda key:feats)
        pavements = milemarkers.source_pavements(frame,obj,context)
        signs = [s for tx in (0,1) for ty in (-1,0)
                 for s in milemarkers.place(frame,[obj,obj],pavements,roads.tile_bbox(tx,ty),flat)]
        self.assertEqual(len(signs),1)
        self.assertEqual(signs[0]['mile'],116)
        self.assertIs(milemarkers.source_pavements(frame,obj,context),pavements)

    def test_post_clears_current_osm_lane_width_and_all_paved_roads(self):
        from test_roads import features,rules,FRAME as frame
        feats = features([[(-300.,100.),(700.,100.)]],['motorway'])
        feats.rules['oneway'] = rules([(0,1.)],'float')
        feats.attrs['osm_lanes'] = np.array([4.])
        ring = feats.parts(0)[0][0]
        obj = milemarkers._object('MI',69,116,'E',ring[0]*.58+ring[-1]*.42,[1.,0.],'dot:mi')
        context = roadcontext.Context(lambda key:feats)
        ribbons = milemarkers.source_pavements(frame,obj,context)
        self.assertAlmostEqual(ribbons[0][3],roads.road_specs(feats)[0].width)
        signs = milemarkers.place(frame,[obj],ribbons,(-500.,-100.,1000.,300.),flat)
        self.assertEqual(len(signs),1)
        self.assertTrue(exitsigns._clear(np.asarray(signs[0]['centre'][:2]),milemarkers.WIDTH/2.+.35,
                                        [exitsigns._outline(p,w) for _,_,p,w in ribbons]))


class MeshTest(unittest.TestCase):
    def sign(self):
        return dict(centre=[0.,0.,0.],travel=[0.,1.],bottom=1.15,width=milemarkers.WIDTH,
                    height=milemarkers.HEIGHT,direction='WEST',route=69,mile=116)

    def test_panel_layout_and_front_facing_shield(self):
        sign = self.sign()
        mesh = milemarkers.Mesh((0.,0.,0.))
        with mock.patch.object(mesh,'text',wraps=mesh.text) as text:
            mesh.panel(sign)
        calls = [c.args for c in text.call_args_list]
        self.assertEqual([c[0] for c in calls],['WEST','INTERSTATE','69','MILE','116'])
        self.assertTrue(all(a[2] > b[2] for a,b in zip(calls[:-1],calls[1:])))
        positions,triangles,uv,materials,indices = mesh.result()
        self.assertTrue(np.isfinite(positions).all())
        self.assertEqual(uv.shape,(len(positions),2))
        face = np.array([0.,-1.,0.])
        for name in ('bne_mile_red','bne_mile_blue'):
            t = triangles[indices==materials.index(name)]
            normals = np.cross(positions[t[:,1]]-positions[t[:,0]],positions[t[:,2]]-positions[t[:,0]])
            self.assertTrue(np.all(normals@face > 0.))

    def test_shape_file_is_announced_for_the_render_only_client_object(self):
        mesh = milemarkers.build([self.sign()],(0.,0.,0.))
        with tempfile.TemporaryDirectory() as tmp:
            path,_ = signstream.write_tile(tmp,'/session',2,0,0,(None,mesh,None))
            data = json.loads((Path(tmp)/Path(path).name).read_text(encoding='utf-8'))
            self.assertIn('mileposts',data)
            self.assertTrue((Path(tmp)/'mileposts_2_0_0.cdae').is_file())
            self.assertEqual(signstream.shape_files(2,0,0)['mileposts'],'mileposts_2_0_0.cdae')


if __name__ == '__main__':
    unittest.main()
