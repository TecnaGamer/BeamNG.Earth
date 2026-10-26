"""Overhead control-city labels, pavement clearance, streaming and cache upgrades."""
from pathlib import Path
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import exitsigns
import highwaysigns
import osmregions
import osmsigns
import signroads
import throughsigns
from test_exit_signs import FRAME, fixture, ll


def flat(lat, lon):
    return np.zeros_like(np.asarray(lat, float))


def tags(**extra):
    return dict(highway='motorway', oneway='yes', ref='I 65', destination='Louisville', **extra)


def pavement(wid=100, y=0., direction=1, width=14.):
    return (f'osm:w{wid}:0', signroads.Spec(width, 'motorway', True, direction, False),
            np.array([[-2000., y, 0.], [6200., y, 0.]]), width)


def through_object(x=100.):
    records = throughsigns.index_records([(100, [1, 2], tags(direction='north'))],
                                       {1: ll(x-100., 0.), 2: ll(x+100., 0.)})
    obj = records[0]
    obj['through'] = obj.pop('ways')[0]
    return obj


class SourceTest(unittest.TestCase):
    def test_control_city_uses_posted_tags_and_route_direction_without_city_guesses(self):
        self.assertEqual(throughsigns.label(tags(direction='north')),
                         dict(ref='', names=['Louisville'], routes=['I 65'], direction='NORTH'))
        self.assertIsNone(throughsigns.label(dict(highway='motorway', ref='I 65', name='Louisville Highway')))
        self.assertEqual(throughsigns.label(tags())['direction'], '', 'road bearing is not a posted direction')
        self.assertEqual(throughsigns.label(tags(), [dict(role='north', direction='NORTH')])['direction'], 'NORTH')
        self.assertEqual(throughsigns.label(tags(), [dict(role='backward', direction='NORTH')])['direction'], '')

    def test_lane_specific_and_reverse_direction_labels_do_not_leak_between_lanes(self):
        self.assertIsNone(throughsigns.label(tags(**{'destination:lanes': 'Louisville|Bowling Green'})))
        common = throughsigns.label(tags(**{'destination:lanes': 'Louisville|'}))
        self.assertEqual(common['names'], ['Louisville'])
        source = tags(**{'destination:forward': 'Nashville', 'destination:backward': 'Louisville'})
        source['oneway'] = '-1'
        self.assertEqual(throughsigns.label(source)['names'], ['Louisville'])
        source['oneway'] = 'no'
        self.assertIsNone(throughsigns.label(source), 'gantries require a separated carriageway')

    def test_contiguous_tagged_ways_form_one_corridor_with_bounded_context(self):
        coords = {n: ll(x, 0.) for n, x in enumerate((0., 100., 3000., 6200.), 1)}
        ways = [(100, [1, 2], tags()), (101, [2, 3], tags()), (102, [3, 4], tags())]
        records = throughsigns.index_records(ways, coords)
        self.assertEqual(len(records), 2, 'way boundaries must not create duplicate assemblies')
        self.assertEqual(records, throughsigns.index_records(ways[::-1], coords))
        self.assertEqual(len({o['id'] for o in records}), 2)
        for obj in records:
            local = exitsigns._xy(FRAME, obj['ways'][0]['approach'])
            self.assertLess(exitsigns._station(local, 1e12)[2], 161.)
        forbidden = [(100, [1, 2, 3, 4], tags(bridge='yes'))]
        self.assertEqual(throughsigns.index_records(forbidden, coords), [])

    def test_reverse_oneway_records_retain_travel_order(self):
        source = tags()
        source['oneway'] = '-1'
        obj, = throughsigns.index_records([(100, [1, 2], source)], {1: ll(0., 0.), 2: ll(300., 0.)})
        path = exitsigns._xy(FRAME, obj['ways'][0]['approach'])
        self.assertLess(path[-1, 0], path[0, 0])

    def test_exit_annotation_uses_the_through_way_not_the_exit_destination(self):
        coords = {1: ll(-1900., 0.), 2: ll(0., 0.), 3: ll(100., -100.)}
        main = [(100, [1, 2], tags())]
        links = [(200, [2, 3], dict(highway='motorway_link', oneway='yes', destination='Bowling Green'))]
        objects = exitsigns.index_records(main, links, coords, {2: dict(ref='22')})
        throughsigns.annotate_exits(objects, {100: [dict(role='north',direction='NORTH')]})
        extra = objects[0]['ways'][0]
        self.assertEqual(extra['label']['names'], ['Bowling Green'])
        self.assertEqual(extra['through_label']['names'], ['Louisville'])
        self.assertEqual(extra['through_label']['direction'], 'NORTH')
        self.assertEqual(throughsigns.index_records(main, coords, objects), [],
                         'a paired gantry replaces a nearby standalone panel on the same way')


class PlacementTest(unittest.TestCase):
    def test_unrelated_tiles_do_not_sample_elevation_for_a_distant_gantry(self):
        rows, obj = fixture()
        obj['exit']['main_tags'] = tags()
        ribbons = [(fid, signroads.spec(sp, fid == 'ramp'), line, sp.width) for fid, sp, line, _ in rows]
        sample = mock.Mock(side_effect=AssertionError('unnecessary elevation request'))
        self.assertEqual(throughsigns.place(FRAME, [obj], ribbons, (0.,0.,256.,256.), sample), [])
        sample.assert_not_called()

    def test_metric_left_exit_uses_a_500_m_station_and_left_panel(self):
        rows, obj = fixture()
        obj['exit'].update(main_tags=tags(), side=-1)
        ribbons = [(fid, signroads.spec(sp, fid == 'ramp'), line, sp.width) for fid, sp, line, _ in rows]
        sign, = throughsigns.place(FRAME, [obj], ribbons, (-1900.,-100.,256.,100.), flat, mph=False)
        self.assertEqual(sign['distance'], '500 m')
        self.assertAlmostEqual(sign['centre'][0], -500., places=3)
        self.assertLess(sign['panels'][1]['offset'], sign['panels'][0]['offset'])

    def test_gantry_spans_its_own_carriageway_with_clear_columns_and_height(self):
        obj = through_object()
        pavements = [pavement(), pavement(101, 17., -1), pavement(102, -18., 1)]
        sign, = throughsigns.place(FRAME, [obj, obj], pavements, (-300., -100., 300., 100.), flat)
        self.assertGreater(sign['travel'][0], .99)
        self.assertAlmostEqual(sign['centre'][1], 0., places=3)
        self.assertGreaterEqual(sign['bottom']-sign['centre'][2], 6.)
        right = np.array([sign['travel'][1], -sign['travel'][0]])
        polygons = [exitsigns._outline(row[2], row[3]) for row in pavements]
        for pole in sign['poles']:
            point = np.array(sign['centre'][:2])+right*pole
            self.assertTrue(exitsigns._clear(point, .65, polygons))
        self.assertEqual(throughsigns.place(FRAME, [obj], [pavement(101)],
                                          (-300., -100., 300., 100.), flat), [])

    def test_one_owner_across_adjacent_tiles_and_no_bridge_fallback(self):
        obj = through_object(256.)
        boxes = [(0., -100., 256., 100.), (256., -100., 512., 100.)]
        signs = [s for box in boxes for s in throughsigns.place(FRAME, [obj, obj], [pavement()], box, flat)]
        self.assertEqual(len(signs), 1)
        deck = dict(deck=True, n=[[-2000., 0., 8.], [2000., 0., 8.]], w=14.)
        self.assertEqual(throughsigns.place(FRAME, [obj], [pavement()],
                                          (-300., -100., 600., 100.), flat, [deck]), [])

    def test_paired_exit_gantry_keeps_distant_and_local_destinations_separate(self):
        rows, obj = fixture()
        obj['exit'].update(main_tags=tags(direction='north'),
                           label=dict(ref='22', names=['Bowling Green'], routes=['US 231']))
        ribbons = [(fid, signroads.spec(sp, fid == 'ramp'), line, sp.width) for fid, sp, line, _ in rows]
        sign, = throughsigns.place(FRAME, [obj], ribbons, (-1900., -100., 256., 100.), flat)
        self.assertEqual([p['label']['names'] for p in sign['panels']], [['Louisville'], ['Bowling Green']])
        self.assertEqual(sign['distance'], '1/2 MILE')
        self.assertLess(sign['panels'][0]['offset'], sign['panels'][1]['offset'])
        self.assertAlmostEqual(sign['centre'][0], -804.672, places=3)
        mesh = throughsigns.build([], [sign], (-1024., -256., 0.))
        p, t, uv, materials, mi = mesh
        self.assertTrue(np.isfinite(p).all())
        self.assertEqual(uv.shape, (len(p), 2))
        self.assertLess(t.max(), len(p))
        self.assertLess(mi.max(), len(materials))
        self.assertIn('bne_guide_letters', materials)
        # All guide lettering faces the approaching driver, including shields.
        tris = t[np.isin(mi, [materials.index('bne_guide_letters'), materials.index('bne_guide_letters_black')])]
        normals = np.cross(p[tris[:, 1]]-p[tris[:, 0]], p[tris[:, 2]]-p[tris[:, 0]])
        self.assertTrue(np.all(normals@np.array([-1., 0., 0.]) > 0.))

    def test_replacement_is_decided_before_tile_ownership(self):
        rows, obj = fixture()
        obj['exit']['main_tags'] = tags()
        ribbons = [(fid, signroads.spec(sp, fid == 'ramp'), line, sp.width) for fid, sp, line, _ in rows]
        source = SimpleNamespace(context=lambda *args: None, warned=-float('inf'))
        md = SimpleNamespace(highway_signs=source)
        # Road centre and shoulder are on opposite sides of y=0, hence different tiles.
        for ty in (-1, 0):
            with mock.patch.object(osmsigns, 'tile_exit_objects', return_value=[obj]), \
                 mock.patch.object(exitsigns, 'source_pavements', return_value=(ribbons, {})), \
                 mock.patch.object(throughsigns, 'build', return_value=None) as build:
                highwaysigns.build_tile(md, FRAME, -4, ty, flat)
                roadside, overheads, origin = build.call_args.args
                self.assertFalse(any(s['distance'] == '1/2 MILE' for s in roadside))
                self.assertEqual(len(overheads), int(ty == 0))


class CacheTest(unittest.TestCase):
    def test_old_index_stays_readable_during_online_upgrade_and_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            regions = osmregions.RegionSource(tmp, SimpleNamespace(offline=False))
            Path(regions.root).mkdir()
            source = osmsigns.ObjectSource(regions)
            old = Path(regions.root, 'test.signs.v1.sqlite')
            old.write_bytes(b'old signs')
            self.assertEqual(source._readable('test', True)[0], str(old))
            self.assertFalse(source._current('test'))
            regions.background = False
            self.assertTrue(source._current('test'))
            Path(source.path('test')).write_bytes(b'new signs')
            regions.background = True
            self.assertTrue(source._current('test'))
            self.assertEqual(old.read_bytes(), b'old signs')

    def test_binary_import_and_spatial_query_preserve_destinations_and_relation_direction(self):
        import osmium
        coords = {1: ll(0., 0.), 2: ll(400., 0.)}
        xml = ['<osm version="0.6">']
        for nid, (lon, lat) in coords.items():
            xml.append(f'<node id="{nid}" version="1" lon="{lon}" lat="{lat}"/>')
        xml.append('<way id="100" version="1"><nd ref="1"/><nd ref="2"/>'+''.join(
            f'<tag k="{k}" v="{v}"/>' for k, v in tags().items())+'</way>')
        xml.append('<relation id="200" version="1"><member type="way" ref="100" role="north"/>'
                   '<tag k="type" v="route"/><tag k="route" v="road"/><tag k="network" v="US:I"/>'
                   '<tag k="ref" v="65"/></relation></osm>')
        with tempfile.TemporaryDirectory() as tmp:
            regions = osmregions.RegionSource(tmp, SimpleNamespace(offline=True))
            Path(regions.root).mkdir()
            source = osmsigns.ObjectSource(regions)
            original, pbf = Path(tmp, 'input.osm'), Path(tmp, 'input.osm.pbf')
            original.write_text(''.join(xml), encoding='utf-8')
            with osmium.SimpleWriter(str(pbf)) as writer:
                for obj in osmium.FileProcessor(str(original)):
                    writer.add(obj)
            self.assertEqual(osmsigns.import_pbf(pbf, source.path('test')), 1)
            source._region = lambda *args: 'test'
            objects = source.query_through(FRAME, (-100., -100., 500., 100.))
            self.assertEqual(len(objects), 1)
            self.assertEqual(objects[0]['through']['label']['direction'], 'NORTH')
            self.assertEqual(objects[0]['through']['label']['names'], ['Louisville'])
            self.assertEqual(source.query_exits(FRAME, (-100., -100., 500., 100.)), [])
            self.assertEqual(source.query_through(FRAME, (1000., -100., 1500., 100.)), [])


class PipelineTest(unittest.TestCase):
    def test_overhead_mesh_is_identical_in_server_and_worker_and_has_no_collision(self):
        import blockworker
        import earthd
        import roads
        import threading
        from test_highway_signs import fixture as pipeline_fixture, server
        from test_blockworker import FakeDem
        from test_roads import FRAME as frame, LAT, LON
        with tempfile.TemporaryDirectory() as tmp:
            md = pipeline_fixture(tmp)
            path = md.feats.parts(0)[0][0]
            lat, lon, _ = frame.enu_to_geodetic(150., 100., 0.)
            obj = dict(id=-12345, through=dict(label=throughsigns.label(tags(direction='north')),
                main_ids=[10], approach=path.tolist(), point=[float(lon),float(lat)]))
            md.highway_signs.objects.query_exits = lambda *args: []
            md.highway_signs.objects.query_mile_markers = lambda *args: []
            md.highway_signs.objects.query_through = lambda *args: [obj]
            dirs = [Path(tmp, name) for name in ('server', 'worker')]
            for directory in dirs:
                directory.mkdir()
            expected = earthd.Server.build_signs_tile(server(md, str(dirs[0])), 1, 0, 0)
            blockworker._s = SimpleNamespace(md=md, dem=FakeDem(), widths=None, surface=None,
                airports=None, sea=None, lock=threading.Lock(), bridges={}, anchor=lambda *a: (None, None))
            result = blockworker.signs_tile(1, LAT, LON, 15, 0, 0, str(dirs[1]), '/temp/test')
            self.assertEqual(expected, result)
            self.assertEqual(sorted(p.name for p in dirs[0].iterdir()), sorted(p.name for p in dirs[1].iterdir()))
            for path in dirs[0].iterdir():
                self.assertEqual(path.read_bytes(), (dirs[1]/path.name).read_bytes(), path.name)
            data = json.loads(next(dirs[0].glob('*.json')).read_text())
            self.assertIn('exits', data)
            self.assertNotIn('exits', data.get('colliders', {}))
            md.highway_signs.close()


if __name__ == '__main__':
    unittest.main()
