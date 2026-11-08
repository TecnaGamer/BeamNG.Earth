"""Nationwide route join, state-specific recognition and shared face layout."""
from contextlib import closing
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
from PIL import Image

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'server'))
sys.path.insert(0,str(ROOT/'tools'))
import highwayroutes
import routerelations
import routerepeaters
import routemarkers
import routefaces
import build_route_relation_index


class RecognitionTest(unittest.TestCase):
    def test_local_prefixes_require_their_state_context(self):
        for ref,state,net,number in [('K-6','KS','US:KS','6'),('M 185','MI','US:MI','185'),
                ('N-92','NE','US:NE','92'),('L-26A','NE','US:NE:LINK','26A'),
                ('S-26D','NE','US:NE:SPUR','26D'),('RR 1','TX','US:TX:RM','1')]:
            self.assertEqual(highwayroutes.recognize({'ref':ref},state=state),[highwayroutes.Route(net,number)])
            self.assertEqual(highwayroutes.recognize({'ref':ref},state='WY'),[])

    def test_letters_hawaii_and_combined_modifiers(self):
        self.assertEqual(highwayroutes.recognize({'ref':'SR A1A'},state='FL'),[highwayroutes.Route('US:FL','A1A')])
        self.assertEqual(highwayroutes.recognize({'ref':'MO JJ'},state='MO'),[highwayroutes.Route('US:MO:SUPPLEMENTAL','JJ')])
        route=highwayroutes.recognize({'ref':'I H-1'},state='HI')[0]
        self.assertEqual(highwayroutes.panel(route)['display_number'],'H-1')
        self.assertEqual(highwayroutes.recognize({'ref':'US 30 ALT TRUCK BUSINESS'})[0].modifier,'ALTERNATE TRUCK BUSINESS')
        self.assertEqual(highwayroutes.recognize({'ref':'I 35W'})[0].ref,'35W')
        self.assertEqual(highwayroutes.recognize({'ref':'LA 987-2'})[0].ref,'987-2')

    def test_cached_inventory_template_choices_follow_the_current_registry(self):
        panel=dict(kind='state',network='US:CO',number='14',template='wy')
        self.assertEqual(highwayroutes.current_panel(panel)['template'],'co')
        panel=dict(kind='interstate',number='80',modifier='BUSINESS LOOP',template='interstate')
        self.assertEqual(highwayroutes.current_panel(panel)['template'],'i_business_loop')
        panel=dict(kind='interstate',network='US:I:Business:Loop',number='80')
        self.assertEqual(highwayroutes.current_panel(panel)['template'],'i_business_loop')
        panel=dict(kind='interstate',network='US:I:Unknown',number='80')
        self.assertEqual(highwayroutes.current_panel(panel)['kind'],'state')

    def test_special_networks_never_collapse_to_state_primary(self):
        for network in ('US:TN:primary','US:TN:secondary','US:MT:secondary','US:VA:Secondary','US:TX:FM','US:TX:RM'):
            rel=dict(type='route',route='road',network=network,ref='125')
            self.assertEqual(highwayroutes.recognize({},[rel])[0].network,network.upper())
        self.assertEqual(highwayroutes.recognize({},[dict(network='US:CA:Los_Angeles',ref='2')]),[])


class RelationIndexTest(unittest.TestCase):
    def test_runtime_cell_joins_relation_only_roads(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=routerepeaters.Source(tmp,None)
            source.tags.version=lambda:('fixed',None)
            ways=[(123,[[-105.19,40.51],[-105.18,40.54]],dict(highway='primary'))]
            relation=dict(type='route',route='road',network='US:CO',ref='14')
            with mock.patch.object(routerepeaters,'read_routes',return_value=ways), \
                 mock.patch.object(source.relations,'query',return_value={123:[relation]}) as join:
                objects=source._cell(-1052,405)
            self.assertTrue(objects)
            self.assertTrue(all(o['roadside']['routes'][0]['network']=='US:CO' for o in objects))
            join.assert_called_once()

    def test_index_join_membership_roles_determinism_and_relation_only_way(self):
        data=dict(osm3s=dict(timestamp_osm_base='2026-11-08T10:34:50Z'),elements=[
            dict(type='relation',id=1,tags=dict(type='route',route='road',network='US:CO',ref='14'),
                 members=[dict(type='way',ref=123,role='forward')]),
            dict(type='relation',id=2,tags=dict(type='route',route='road',network='US:US',ref='287'),
                 members=[dict(type='way',ref=123,role='')]),
            dict(type='relation',id=3,tags=dict(type='route',route='road',network='US:WY',ref='210'))])
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);src=tmp/'source.json';src.write_text(json.dumps(data))
            archive=tmp/'route_relations.sqlite.gz'
            spec=build_route_relation_index.build(src,archive)
            expected=archive.read_bytes()
            build_route_relation_index.build(src,archive)
            self.assertEqual(archive.read_bytes(),expected)
            with mock.patch.object(routerelations,'ARCHIVE',archive),mock.patch.object(routerelations,'manifest',return_value=spec):
                source=routerelations.Source(tmp)
                relations=source.query([123,999])
                self.assertEqual({r['network'] for r in relations[123]},{'US:CO','US:US'})
                self.assertEqual(relations[123][0]['role'],'forward')
                with mock.patch.object(source,'_database',side_effect=AssertionError('cache miss')):
                    self.assertEqual(source.query([123]),relations)
                ways=[(123,[[-105.2,40.5],[-105.18,40.5]],dict(highway='primary'))]
                objects=routerepeaters.candidates(ways,relations)
                self.assertTrue(objects)
                self.assertTrue(any(r['network']=='US:CO' for o in objects for r in o['roadside']['routes']))

    def test_bundled_index_covers_all_states_without_runtime_network(self):
        spec=routerelations.manifest()
        states={network.split(':')[1] for network in spec['networks'] if network.startswith('US:')}
        expected={config['state'] for config in highwayroutes.registry()['networks'].values() if config.get('state')}
        self.assertTrue(expected<=states)
        self.assertGreater(spec['ways'],1000000)
        with tempfile.TemporaryDirectory() as tmp:
            source=routerelations.Source(tmp)
            database=source._database()
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(),spec['database_sha256'])
            with closing(sqlite3.connect(database)) as con:
                samples=con.execute('SELECT way_id FROM members LIMIT 20').fetchall()
                self.assertTrue(source.query([row[0] for row in samples]))

    def test_corrupt_bundle_falls_back_to_way_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad=Path(tmp)/'bad.gz';bad.write_bytes(gzip.compress(b'broken'))
            with mock.patch.object(routerelations,'ARCHIVE',bad):
                self.assertEqual(routerelations.Source(tmp).query([123]),{})


class LayoutTest(unittest.TestCase):
    def test_baked_business_legend_does_not_extend_the_post_above_the_assembly(self):
        route=dict(kind='interstate',number='80',modifier='BUSINESS LOOP',direction='EAST',template='interstate')
        self.assertLess(routemarkers.dimensions(dict(routes=[route]))[1],1.8)

    def test_wide_concurrent_panels_do_not_overlap(self):
        config=highwayroutes.registry()
        fake=dict(config['templates']);fake['testwide']=dict(aspect=1.5)
        routes=[dict(kind='state',template='testwide',number='7'),dict(kind='us',template='us',number='30')]
        with mock.patch.dict(config,templates=fake):
            layout,total=routemarkers.panel_layout(dict(routes=routes))
        self.assertGreaterEqual(layout[1][0]-layout[1][1]/2-(layout[0][0]+layout[0][1]/2),.149)
        self.assertAlmostEqual(total,2.65)


class LibraryTest(unittest.TestCase):
    def test_all_fifty_states_have_reviewed_main_designs_and_mapping(self):
        states='AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY'.split()
        registry=highwayroutes.registry()
        for state in states:
            net='US:TN:PRIMARY' if state=='TN' else 'US:'+state
            config=registry['networks'][net]
            self.assertTrue(config['verified'],state)
            template=registry['templates'][config['template']]
            self.assertTrue(template['verified'],state)
            self.assertTrue((routefaces.DATA/template['image']).is_file(),state)
            recognized=highwayroutes.recognize({},[dict(type='route',route='road',network=net,ref='42')])
            self.assertEqual(highwayroutes.panel(recognized[0])['state'],state)
        # A road class cannot choose which Tennessee design was actually posted.
        self.assertIsNone(registry['networks']['US:TN']['template'])

    def test_all_numberless_templates_have_safe_number_fields_and_licensed_sources(self):
        catalog=json.loads((routefaces.DATA/'artwork_sources.json').read_text(encoding='utf-8'))
        self.assertEqual(len(catalog['artwork']),88)
        for key,spec in catalog['artwork'].items():
            self.assertIn(spec['license'],('Public domain','CC BY-SA 4.0'))
            self.assertTrue(spec['description_url'].startswith('https://commons.wikimedia.org/wiki/File:'))
            self.assertEqual(len(spec['sha256']),64)
            image=np.asarray(Image.open(routefaces.DATA/'art'/f'{key}.png').convert('RGB'))
            x0,y0,x1,y1=spec['number_box'];box=image[y0:y1,x0:x1]
            colours,counts=np.unique(box.reshape(-1,3),axis=0,return_counts=True)
            background=colours[counts.argmax()]
            clear=(np.max(np.abs(box.astype(int)-background),axis=2)<25).mean()
            self.assertGreater(clear,.95,key) # no legend or state emblem under the number
            profile=routefaces.contour(key)
            if profile is not None:
                points,triangles=profile
                self.assertEqual(len(triangles),len(points)-2,key)
                self.assertTrue(np.isfinite(points).all())
        self.assertEqual(catalog['artwork']['mn_wide']['license'],'CC BY-SA 4.0')

    def test_each_state_renders_variable_length_refs_and_special_designs_remain_distinct(self):
        registry=highwayroutes.registry()
        states={config['state'] for config in registry['networks'].values() if config.get('state')}
        for state in states:
            net='US:TN:PRIMARY' if state=='TN' else 'US:'+state
            seen=set()
            for ref in ('7','42','210','1234'):
                raw=routefaces.raster(net,ref)
                self.assertEqual(len(raw),512*512*3)
                seen.add(hashlib.sha256(raw).digest())
            self.assertEqual(len(seen),4,state)
        self.assertNotEqual(routefaces.raster('US:TX:FM','1234'),routefaces.raster('US:TX:RM','1234'))
        self.assertNotEqual(routefaces.raster('US:TN:PRIMARY','125'),routefaces.raster('US:TN:SECONDARY','125'))
        self.assertNotEqual(routefaces.raster('US:MT','243'),routefaces.raster('US:MT:SECONDARY','243'))
        self.assertEqual(highwayroutes.template_for('US:MI','210'),'mi_wide')
        self.assertEqual(highwayroutes.template_for('US:I','196','BUSINESS SPUR'),'i_business_spur_wide')


if __name__=='__main__':unittest.main()
