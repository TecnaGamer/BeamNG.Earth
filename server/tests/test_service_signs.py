"""Directed access, conservative POI matching, canonical ownership and shared mesh."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import servicesigns
import servicesigns_index as index
from test_exit_signs import FRAME, fixture, ll
import signroads


def osm():
    points = {1: ll(-1900, 0), 2: ll(-1000, 0), 3: ll(0, 0),
              4: ll(100, -100), 5: ll(200, -200), 6: ll(300, -200),
              7: ll(310, -200), 8: ll(210, -210), 9: ll(500, -200)}
    elements = [dict(type='node', id=n, lon=p[0], lat=p[1], tags={}) for n, p in points.items()]
    def way(wid, refs, **tags): return dict(type='way', id=wid, nodes=refs, tags=tags)
    elements += [way(10, [1, 2, 3], highway='motorway', ref='I 80', oneway='yes'),
                 way(20, [3, 4, 5], highway='motorway_link', oneway='yes', destination='Town'),
                 way(30, [5, 6, 9], highway='residential', oneway='yes'),
                 way(31, [5, 8], highway='service', access='private')]
    elements[2]['tags'] = {'highway': 'motorway_junction', 'ref': '36'}
    elements[5]['tags'] = {'amenity': 'restaurant', 'name': 'Real Cafe'}
    elements[6]['tags'] = {'amenity': 'fuel', 'name': 'Disconnected Gas'}
    elements[7]['tags'] = {'tourism': 'hotel', 'name': 'Private Hotel'}
    return dict(elements=elements)


def records(data):
    with patch.object(index.signstates, 'states', return_value=np.array('WY')):
        return index.build(data)['objects']


class ServiceSignsTest(unittest.TestCase):
    def test_directed_network_excludes_near_disconnected_and_private_pois(self):
        result = records(osm())
        self.assertEqual(len(result), 1)
        services = result[0]['exit']['services']
        self.assertEqual([s['name'] for s in services], ['Real Cafe'])
        self.assertGreater(services[0]['distance_m'], 350)
        self.assertEqual(result[0]['exit']['label']['ref'], '36')
        self.assertEqual(services[0]['provenance'], 'inferred')

    def test_wrong_way_road_and_entry_ramp_do_not_qualify(self):
        data = osm()
        data['elements'][-2]['tags']['oneway'] = '-1'
        self.assertEqual(records(data), [])
        data = osm()
        data['elements'][-3]['tags']['oneway'] = '-1'
        self.assertEqual(records(data), [])

    def test_restrictions_and_barriers_fail_closed(self):
        data = osm()
        data['elements'].append(dict(type='relation', id=1, tags={'type': 'restriction'},
                                    members=[dict(type='node', ref=5, role='via')]))
        self.assertEqual(records(data), [])
        data = osm()
        data['elements'][4]['tags']['barrier'] = 'gate'
        self.assertEqual(records(data), [])

    def test_no_service_and_network_distance_limit(self):
        data = osm()
        data['elements'][5]['tags'] = {}
        self.assertEqual(records(data), [])
        data = osm()
        data['elements'][5]['lon'], data['elements'][5]['lat'] = ll(8000, -200)
        self.assertEqual(records(data), [])

    def test_area_access_requires_actual_connected_vertex(self):
        data = osm()
        ring = [ll(280, -220), ll(320, -220), ll(320, -180), ll(280, -180)]
        data['elements'][5]['tags'] = {}
        data['elements'].append(dict(type='way', id=100, nodes=[101,102,103,104,101],
                                    geometry=[dict(lon=p[0], lat=p[1]) for p in ring+[ring[0]]],
                                    tags={'amenity': 'fuel', 'name': 'Area Fuel'}))
        self.assertEqual(records(data)[0]['exit']['services'][0]['name'], 'Area Fuel')

    def test_roundabout_oneway_and_motor_vehicle_access(self):
        self.assertFalse(index.allowed({'motor_vehicle': 'private', 'access': 'yes'}))
        self.assertTrue(index.allowed({'motorcar': 'yes', 'access': 'private'}))
        self.assertFalse(index.allowed({'access:conditional': 'no @ (night)'}))
        data = osm()
        data['elements'][-2]['tags'].update(oneway='-1', junction='roundabout')
        self.assertEqual(records(data), [])

    def test_deterministic_build(self):
        data = osm()
        a = records(data)
        data['elements'].reverse()
        self.assertEqual(a, records(data))

    def test_military_area_is_not_public_service_access(self):
        data = osm()
        ring = [ll(280,-220),ll(320,-220),ll(320,-180),ll(280,-180)]
        data['elements'].append(dict(type='way', id=120, nodes=[111,112,113,114,111],
                                    geometry=[dict(lon=p[0],lat=p[1]) for p in ring+[ring[0]]],
                                    tags={'landuse':'military'}))
        self.assertEqual(records(data), [])

    def test_verified_evidence_cannot_add_an_unreachable_business(self):
        result = dict(objects=records(osm()), diagnostics={})
        evidence = dict(ramp_id=20,business_id='osm:n6',source='https://example.gov/inventory',
                        checked_by='test auditor',checked_at='2026-11-08')
        index.apply_verifications(result,[evidence])
        self.assertEqual(result['objects'][0]['exit']['services'][0]['provenance'], 'verified')
        evidence['business_id'] = 'osm:n7'
        with self.assertRaises(ValueError): index.apply_verifications(result,[evidence])

    def test_multipolygon_holes_do_not_supply_business_access(self):
        ring = [ll(280,-220),ll(320,-220),ll(320,-180),ll(280,-180)]
        data = osm()
        data['elements'][5]['tags'] = {}
        outer = [dict(lon=p[0],lat=p[1]) for p in ring+[ring[0]]]
        relation = dict(type='relation',id=130,tags={'type':'multipolygon','amenity':'fuel','name':'Area Fuel'},
                        members=[dict(type='way',ref=131,role='outer',geometry=outer)])
        data['elements'].append(relation)
        self.assertEqual(records(data)[0]['exit']['services'][0]['name'], 'Area Fuel')
        relation['members'].append(dict(type='way',ref=132,role='inner',geometry=outer))
        self.assertEqual(records(data), [])

    def test_layout_limits_real_names_and_verified_priority(self):
        _, obj = fixture()
        extra = obj['exit']
        extra['services'] = [dict(id=str(i), category='FOOD', name='Actual Name '+str(i),
                                  distance_m=i, provenance='verified' if i == 7 else 'inferred') for i in range(8)]
        boards = servicesigns.boards(extra, index.policy('WY'))
        self.assertEqual(boards[0]['lines'], ['FOOD'])
        self.assertEqual(len(boards[1]['businesses']), 6)
        self.assertEqual(boards[1]['businesses'][0]['id'], '7')

    def test_source_revision_spatial_query_and_bad_index(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'index.json'
            source = servicesigns.Source(path)
            self.assertIsNone(source.version())
            data = dict(format=1, objects=records(osm()))
            path.write_text(json.dumps(data))
            version = source.version()
            self.assertTrue(version)
            self.assertEqual(len(source.query(-180, -85, 180, 85)), 1)
            self.assertEqual(source.query(0, 0, 1, 1), [])
            path.write_text('bad')
            with self.assertLogs('earthd.servicesigns', level='WARNING'):
                source.version()
            self.assertEqual(source.query(-180, -85, 180, 85), [])

    def test_pipeline_batches_services_with_existing_exit_mesh(self):
        from test_highway_signs import fixture as pipeline_fixture
        from test_roads import FRAME as pipeline_frame
        import highwaysigns
        import plazas
        import serviceobstacles as signplacement
        import roads
        import signstream
        with tempfile.TemporaryDirectory() as folder:
            md = pipeline_fixture(folder)
            original = md.highway_signs.tags.objects[0]
            extra = copy.deepcopy(original['exit'])
            extra.update(service_state='WY', services=[dict(id='f', name='Real Cafe', category='FOOD', distance_m=300)])
            path = Path(folder)/'services.json'
            path.write_text(json.dumps(dict(format=1, objects=[dict(id='service:20', lon=original['lon'],
                                       lat=original['lat'], tags={}, exit=extra)])))
            md.highway_signs.services = servicesigns.Source(path)
            elev = lambda la,lo: np.zeros_like(np.asarray(la,dtype=float))
            # Use the real road context, guide layout, merge and streaming writer;
            # only building availability is stubbed in this isolated fixture.
            rules = dict(index.policy('WY'), advance_m=1750)
            with patch.object(signplacement, 'for_tile', return_value=signplacement.Context()), \
                    patch.object(servicesigns, 'policy', return_value=rules):
                tx,ty = -7,0
                box = roads.tile_bbox(tx,ty)
                mesh, _ = highwaysigns.build_tile(md,pipeline_frame,tx,ty,elev)
                self.assertIsNotNone(mesh)
                self.assertIn('bne_exit_blue', mesh[3])
                self.assertIn(4, mesh[4])
                output = signstream.write_tile(folder,'/temp/service-test',1,tx,ty,(mesh,None,None,None,None))
                self.assertEqual(output[1],1)
                data = json.loads((Path(folder)/f'signs_1_{tx}_{ty}.json').read_text())
                self.assertIn('exits',data)
                self.assertTrue((Path(folder)/f'exits_1_{tx}_{ty}.cdae').is_file())
                with patch.object(servicesigns, 'build_tile', side_effect=ValueError('isolated service failure')):
                    with self.assertLogs('earthd.highwaysigns', level='WARNING'):
                        fallback, _ = highwaysigns.build_tile(md,pipeline_frame,tx,ty,elev)
                # A broken optional service template must retain the green signs.
                self.assertIsNotNone(fallback)
                self.assertGreater(len(mesh[0]),len(fallback[0]))

    def test_placement_ownership_clearance_mesh_and_orientation(self):
        rows, obj = fixture()
        extra = copy.deepcopy(obj['exit'])
        extra.update(service_state='WY', services=[dict(id='food', name='Real Restaurant', category='FOOD', distance_m=200)])
        service = dict(id='service:201', service_exit=extra)
        pavements = []
        for fid, sp, line, _ in rows:
            sp = signroads.spec(sp, link=fid == 'ramp')
            p, width = signroads.pavement(sp, line, FRAME, False)
            pavements.append((fid, sp, p, width))
        class Context:
            def pavements(self, *args): return pavements
        elev = lambda la, lo: np.zeros_like(np.asarray(la, dtype=float))
        args = FRAME, [service, service], Context()
        whole = servicesigns.place(*args, (-1800,-300,256,100), elev)
        self.assertEqual(len(whole), 2)
        split = [s for a,b in [(-1800,-1200),(-1200,256)]
                 for s in servicesigns.place(*args, (a,-300,b,100), elev)]
        self.assertEqual([s['id'] for s in whole], [s['id'] for s in split])
        self.assertTrue(all(s['travel'][0] > .99 and s['centre'][1] < -10 for s in whole))
        self.assertEqual(servicesigns.place(*args, (-1800,-300,256,100), elev, existing=whole), [])
        class Blocked:
            def clear(self, *args): return False
        self.assertEqual(servicesigns.place(*args, (-1800,-300,256,100), elev,
                                           obstacle_factory=lambda p: Blocked()), [])
        mesh = servicesigns.build(whole, (0,0,0))
        self.assertTrue(np.isfinite(mesh[0]).all())
        self.assertEqual(len(mesh[0]), len(mesh[2]))
        self.assertIn('bne_exit_blue', mesh[3])
        self.assertLess(mesh[1].max(), len(mesh[0]))
        self.assertLess(mesh[4].max(), len(mesh[3]))

        # A mapped guardrail/fence crossing either candidate excludes that sign.
        for s in whole:
            x,y = s['centre'][:2]
            extra.setdefault('service_barriers', []).append([ll(x-10,y),ll(x+10,y)])
        self.assertEqual(servicesigns.place(*args, (-1800,-300,256,100), elev), [])


if __name__ == '__main__': unittest.main()
