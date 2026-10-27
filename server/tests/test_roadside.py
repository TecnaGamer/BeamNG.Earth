"""OSM import, stable stations and actual generated roadside meshes."""
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'server'))
import geodesy
import highwaysigns
import osmsigns
import roadside
import roadsideindex as idx
import signroads

FRAME = geodesy.EnuFrame(42.8,-84.3)


def ll(x,y=0):
    lat,lon,_ = FRAME.enu_to_geodetic(x,y,0.)
    return [float(lon),float(lat)]


def corridor(length=14000, split=1000, **tags):
    coords = {i+1:ll(x) for i,x in enumerate(range(0,length+1,split))}
    base = dict(highway='motorway',oneway='yes',ref='I 69',maxspeed='70 mph',**{'maxspeed:hgv':'65 mph'})
    base.update(tags)
    ways = [(100+i,[i,i+1],dict(base)) for i in range(1,len(coords))]
    return ways,coords


def objects(ways,coords,nodes=(),relations=None):
    out = idx.index_records(ways,coords,nodes,relations)
    for obj in out:
        obj['roadside'] = obj['ways'][0]
    return out


def station(obj):
    return FRAME.geodetic_to_enu(obj['lat'],obj['lon'],0.)[0]


class RoadsideIndexTest(unittest.TestCase):
    def test_units_unknowns_and_directional_truck_limits(self):
        self.assertEqual(idx.speed('70 mph'),70)
        self.assertEqual(idx.speed('100'),62)
        self.assertEqual(idx.speed('80 km/h'),50)
        for invalid in ('signals','none','70 @ wet','70;50','0','-30','999 mph'):
            self.assertIsNone(idx.speed(invalid))
        self.assertEqual(idx.limits({'maxspeed':'70 mph','maxspeed:backward':'55 mph','maxspeed:hgv:backward':'45 mph'},-1),(55,45))

    def test_repeaters_join_ways_and_keep_mileage_spacing(self):
        ways,coords = corridor()
        out = objects(ways,coords)
        for kind,spacing in (('speed',idx.SPEED_SPACING),('shield',idx.SHIELD_SPACING),('adopt',idx.ADOPT_SPACING)):
            s = sorted(station(o) for o in out if o['roadside']['type']==kind)
            self.assertGreater(len(s),2)
            np.testing.assert_allclose(np.diff(s),spacing,rtol=.003)
        self.assertEqual(out,objects(ways[::-1],coords))
        speeds = [o['roadside'] for o in out if o['roadside']['type']=='speed']
        self.assertTrue(all((o['speed'],o['truck'])==(70,65) for o in speeds))

    def test_segmentation_does_not_restart_stations(self):
        ways,coords = corridor()
        merged = [(99,list(coords),ways[0][2])]
        a = sorted((o['roadside']['type'],round(station(o),1)) for o in objects(ways,coords))
        b = sorted((o['roadside']['type'],round(station(o),1)) for o in objects(merged,coords))
        self.assertEqual(a,b)

    def test_reverse_and_two_way_get_correct_travel(self):
        ways,coords = corridor(oneway='-1')
        out = objects(ways,coords)
        self.assertTrue(all(o['roadside']['axis'][0]<-.99 for o in out))
        ways,coords = corridor(highway='primary',oneway='no',ref='US 127')
        out = objects(ways,coords)
        for kind in ('speed','shield','adopt'):
            axes = {round(o['roadside']['axis'][0]) for o in out if o['roadside']['type']==kind}
            self.assertEqual(axes,{-1,1})

    def test_mapped_node_suppresses_nearby_repeater_and_inherits_truck(self):
        ways,coords = corridor()
        p = ll(410,-8)
        out = objects(ways,coords,[(800,*p,{'traffic_sign':'US:R2-1[70]','direction':'forward'})])
        speeds = [o for o in out if o['roadside']['type']=='speed']
        self.assertEqual(sum(abs(station(o)-410)<300 for o in speeds),1)
        mapped = next(o['roadside'] for o in speeds if o['roadside']['source']=='osm:node')
        self.assertEqual(mapped['truck'],65)

    def test_node_on_way_honors_backward_only(self):
        ways,coords = corridor(highway='primary',oneway='no',ref='US 127')
        out = objects(ways,coords,[(4,*coords[4],{'maxspeed':'45 mph','direction':'backward'})])
        mapped = [o for o in out if o['roadside'].get('source')=='osm:node']
        self.assertEqual(len(mapped),1)
        self.assertLess(mapped[0]['roadside']['axis'][0],0)

    def test_changed_limit_and_bridge_do_not_reset_phase(self):
        ways,coords = corridor()
        ways[2][2]['maxspeed']='55 mph'
        ways[3][2]['bridge']='yes'
        out = objects(ways,coords)
        changed = [o for o in out if o['roadside'].get('source')=='osm:change']
        self.assertTrue(any(o['roadside']['speed']==55 for o in changed))
        self.assertFalse(any(3000<station(o)<4000 for o in out))
        self.assertTrue(any(o['roadside']['type']=='speed' and 6800<station(o)<6900 for o in out))

    def test_missing_speed_uses_game_default_but_variable_speed_does_not(self):
        ways,coords = corridor(maxspeed='')
        out = objects(ways,coords)
        self.assertTrue(all(o['roadside']['speed']==65 for o in out if o['roadside']['type']=='speed'))
        ways,coords = corridor(maxspeed='signals')
        self.assertFalse(any(o['roadside']['type']=='speed' for o in objects(ways,coords)))

    def test_michigan_only_and_no_us_panels_outside_us(self):
        ways,coords = corridor()
        with mock.patch.object(idx.signstates,'states',side_effect=lambda lat,lon:np.full(np.shape(lat),'KY')):
            out = objects(ways,coords)
        self.assertTrue(out)
        self.assertFalse(any(o['roadside']['type']=='adopt' for o in out))
        with mock.patch.object(idx.signstates,'states',side_effect=lambda lat,lon:np.full(np.shape(lat),'')):
            self.assertEqual(objects(ways,coords),[])

    def test_route_relation_ref_and_direction_override_local_heading(self):
        ways,coords = corridor(ref='')
        rel = {wid:[dict(network='US:I',ref='69',direction='east',role='forward')] for wid,_,_ in ways}
        out = objects(ways,coords,relations=rel)
        shields = [o['roadside']['routes'] for o in out if o['roadside']['type']=='shield']
        self.assertTrue(shields)
        self.assertTrue(all(s==[dict(kind='interstate',number='69',direction='EAST')] for s in shields))
        self.assertEqual(idx.route_refs({'ref':'I-65; US 31; M 43; I 65 Business'},()),[('I','65'),('US','31')])

    def test_lamp_tags_and_unsupported_mounts(self):
        nodes = [(1,*ll(0),dict(highway='street_lamp',lamp_mount='high_mast')),
                 (2,*ll(10),{'man_made':'mast','tower:type':'lighting'}),
                 (3,*ll(20),dict(highway='street_lamp',lamp_mount='wall'))]
        out = objects([],{},nodes)
        self.assertEqual(len(out),2)
        self.assertTrue(all(o['roadside']['type']=='lamp' for o in out))


class RoadsidePlacementTest(unittest.TestCase):
    def setUp(self):
        self.flat = lambda lat,lon:np.zeros_like(lat)
        self.rows = [('osm:w101:0',signroads.Spec(14.,'motorway',True,1),np.array([[-1000.,0,0],[2000.,0,0]]),14.)]
        self.box = (-1000,-1000,2000,1000)
        ways,coords = corridor(length=3000)
        self.obj = next(o for o in objects(ways,coords) if o['roadside']['type']=='speed')

    def place(self,obj=None,rows=None,box=None,chunks=()):
        return roadside.place(FRAME,obj or self.obj,self.rows if rows is None else rows,box or self.box,self.flat,chunks)

    def test_off_pavement_facing_incoming_traffic_and_single_tile_owner(self):
        sign = self.place()
        self.assertIsNotNone(sign)
        self.assertLess(sign['centre'][1],-8.5)
        self.assertGreater(sign['travel'][0],.99)
        x = sign['centre'][0]
        self.assertIsNone(self.place(box=(-1000,-1000,x,1000)))
        self.assertIsNotNone(self.place(box=(x,-1000,2000,1000)))

    def test_opposite_source_way_and_bridge_do_not_receive_sign(self):
        rows = [('osm:w999:0',*self.rows[0][1:])]
        self.assertIsNone(self.place(rows=rows))
        self.assertIsNone(self.place(chunks=[dict(deck=True,n=self.rows[0][2],w=14)]))

    def test_no_support_on_adjacent_pavement(self):
        rows = self.rows+[('neighbor',signroads.Spec(30,'primary',False,1),np.array([[-1000.,-23,0],[2000.,-23,0]]),30)]
        self.assertIsNone(self.place(rows=rows))

    def test_mapped_high_mast_height_count_and_pavement_rejection(self):
        def obj(y):
            p=ll(400,y)
            return dict(id=20,lon=p[0],lat=p[1],roadside=dict(type='lamp',tags={'lamp_mount':'high_mast','height':'120 ft','light:count':'6'}))
        high = self.place(obj(-30))
        self.assertAlmostEqual(high['height'],36.576)
        self.assertEqual(high['count'],6)
        self.assertTrue(high['high'])
        self.assertIsNone(self.place(obj(0)))

    def test_all_meshes_use_bundled_materials_and_finite_geometry(self):
        samples=[self.place()]
        for kind,extra in (('shield',dict(routes=[dict(kind='interstate',number='69',direction='NORTH'),dict(kind='us',number='127',direction='NORTH')])),('adopt',{})):
            samples.append(dict(type=kind,centre=[0,0,0],travel=[1,0],bottom=1.75,width=1.65,height=2.35,posts=[-.5,.5],bases=[0,0],**extra))
        samples.append(dict(type='lamp',centre=[0,0,0],travel=[1,0],height=35,high=True,count=8))
        mats=json.loads((ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/roadside/exit.materials.json').read_text())
        for sample in samples:
            p,t,uv,m,mi=roadside.build([sample],(0,0,0))
            self.assertTrue(np.isfinite(p).all())
            self.assertTrue(np.isfinite(uv).all())
            self.assertLess(t.max(),len(p))
            self.assertLess(mi.max(),len(m))
            self.assertTrue(set(m)<=set(mats))
            self.assertEqual(len(mi),len(t))


class RoadsideImportTest(unittest.TestCase):
    def test_existing_sign_tile_stream_includes_new_panels(self):
        from test_highway_signs import fixture, FRAME as frame
        with tempfile.TemporaryDirectory() as tmp:
            md=fixture(tmp)
            self.addCleanup(md.highway_signs.close)
            def point(x,y):
                lat,lon,_=frame.enu_to_geodetic(x,y,0.)
                return [float(lon),float(lat)]
            p=point(150,100)
            extra=dict(type='speed',point=p,approach=[point(100,100),point(200,100)],
                       main_id=10,speed=70,truck=65,state='MI')
            obj=dict(id=-999,lon=p[0],lat=p[1],roadside=extra)
            flat=lambda lat,lon:np.zeros_like(lat)
            old,miles=highwaysigns.build_tile(md,frame,0,0,flat)
            md.highway_signs.objects.query_roadside=lambda *args:[obj]
            new,new_miles=highwaysigns.build_tile(md,frame,0,0,flat)
            self.assertIsNotNone(new)
            self.assertGreater(len(new[1]),len(old[1]))
            np.testing.assert_equal(miles[0],new_miles[0])
            self.assertIn('bne_guide_letters_black',new[3])

    def test_pbf_roundtrip_and_legacy_index_upgrade(self):
        import osmium
        with tempfile.TemporaryDirectory() as tmp:
            pbf=Path(tmp)/'tiny.osm.pbf'
            ways,coords=corridor(length=3000)
            with osmium.SimpleWriter(str(pbf)) as writer:
                for nid,p in coords.items():
                    writer.add_node(osmium.osm.mutable.Node(id=nid,location=tuple(p)))
                writer.add_node(osmium.osm.mutable.Node(id=90,location=tuple(ll(400,-30)),tags={'highway':'street_lamp','lamp_mount':'high_mast'}))
                for wid,refs,tags in ways:
                    writer.add_way(osmium.osm.mutable.Way(id=wid,nodes=refs,tags=tags))
            target=Path(tmp)/('us_michigan'+osmsigns.INDEX_SUFFIX)
            count=osmsigns.import_pbf(pbf,target)
            with closing(sqlite3.connect(target)) as con:
                rows=con.execute("SELECT ways FROM objects WHERE kind='roadside'").fetchall()
                kinds=Counter(json.loads(r[0])[0]['type'] for r in rows)
            self.assertGreater(count,0)
            self.assertEqual(kinds,dict(lamp=1,speed=1,shield=1,adopt=1))
            region=SimpleNamespace(root=tmp,background=True,close=lambda:None)
            source=osmsigns.ObjectSource(region)
            source._region=lambda lat,lon:'us_michigan'
            found=source.query_roadside(FRAME,(-200,-200,3200,200))
            self.assertEqual(len(found),4)
            self.assertTrue(all('roadside' in o and 'exit' not in o for o in found))
            old=Path(tmp)/'us_michigan.signs.v2.sqlite'
            target.rename(old)
            self.assertEqual(source._readable('us_michigan','roadside')[0],str(old))
            self.assertFalse(source._current('us_michigan'))


if __name__=='__main__':
    unittest.main()
