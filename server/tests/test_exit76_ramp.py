"""Replay the reported I-65 Exit 76 ramp without changing other highway lanes."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import numpy as np
from test_roads import rules, flat
import geodesy, mapgeom, navgraph, osmtags, roadlanes, roads, surface


DATA = json.loads((Path(__file__).parent / 'fixtures/ky_i65_exit76.json').read_text())['roads']


def fixture(name='southbound_exit', line=None):
    row = DATA[name]
    line = np.array(row['line'] if line is None else line, dtype=float)
    f = mapgeom.build('roads', np.array([row['id']]), [(mapgeom.LINE, [[line]])])
    f.attrs = {k: np.array([np.nan if v is None else v]) for k, v in row['attrs'].items()}
    f.rules = {k: mapgeom.RuleTable(np.zeros(len(entries), np.int32),
                                   np.array([r[0] for r in entries]),
                                   np.array([r[1] for r in entries], np.float32),
                                   np.array([r[2] for r in entries], np.float32))
               for k, entries in row['rules'].items()}
    return f


class Exit76RampTest(unittest.TestCase):
    def test_cached_three_lane_tag_becomes_one_lane_width_and_ai(self):
        f = fixture()
        f._cached_lane_counts = {0: 3}
        sp = roads.road_specs(f)[0]
        self.assertEqual((sp.lanes, sp.link, sp.oneway), (1, True, True))
        self.assertAlmostEqual(sp.width, 8.3)
        self.assertAlmostEqual(navgraph.driving_width(sp), 4.8)
        self.assertEqual(navgraph._lanes(navgraph.driving_width(sp), sp.oneway, sp.lanes), (1, 0))
        self.assertEqual(f.attrs['osm_lanes'][0], 3)
        self.assertEqual(f.attrs['lane_count'][0], 3)
        self.assertEqual(roadlanes.generated_counts(f), [1])
        frame = geodesy.EnuFrame(37.460176, -85.883144)
        graph = navgraph.build(frame, [f], flat, (0., 0.))
        self.assertTrue(graph['edges'])
        self.assertTrue(all((e[3], e[4]) == (1, 0) for e in graph['edges']))

    def test_actual_neighbouring_mainlines_and_northbound_ramp_are_unchanged(self):
        for name, lanes, width in (('southbound_mainline', 3, 18.),
                                   ('northbound_mainline', 3, 18.), ('northbound_exit', 1, 8.3)):
            with self.subTest(name=name):
                sp = roads.road_specs(fixture(name))[0]
                self.assertEqual(sp.lanes, lanes)
                self.assertAlmostEqual(sp.width, width)

    def test_source_splits_revisions_and_reversed_geometry(self):
        line = np.array(DATA['southbound_exit']['line'])
        for part in (line, line[:11], line[10:][::-1]):
            f = fixture(line=part)
            f.ids = np.array(['split-feature'])
            f.attrs['src_record'] = np.array(['w16257189@5'])
            f.attrs.pop('lane_osm_way')
            f.attrs.pop('subclass')  # The equivalent rule also identifies a link.
            f.rules['oneway'] = rules([(0, -1)], 'float')
            self.assertEqual(roads.road_specs(f)[0].lanes, 1)
        f = fixture()
        f.attrs['src_record'] = np.array(['w999@1'])
        self.assertEqual(roads.road_specs(f)[0].lanes, 1, 'geometry-enriched source ID also matches')

    def test_correction_requires_identity_location_and_a_oneway_motorway_link(self):
        for mode in ('other_way', 'similar_way_id', 'elsewhere', 'part_outside', 'mainline', 'twoway', 'other_class'):
            with self.subTest(mode=mode):
                f = fixture()
                if mode in ('other_way', 'similar_way_id'):
                    f.attrs['src_record'] = np.array(['w999@4' if mode == 'other_way' else 'w162571890@4'])
                    f.attrs['lane_osm_way'] = np.array([999])
                elif mode == 'elsewhere':
                    f.lonlat += [1., 0.]
                elif mode == 'part_outside':
                    f.lonlat[-1] += [1., 0.]
                elif mode == 'mainline':
                    f.attrs['subclass'] = np.array([''])
                    f.rules.pop('subclass')
                elif mode == 'twoway':
                    f.rules.pop('oneway')
                else:
                    f.attrs['class'] = np.array(['primary'])
                self.assertEqual(roads.road_specs(f)[0].lanes, 3)

    def test_osm_refresh_keeps_the_local_correction(self):
        f = fixture()
        self.assertEqual(roads.road_specs(f)[0].lanes, 1)
        osmtags.join(f, {16257189: {'lanes': 3}})
        self.assertEqual(roads.road_specs(f)[0].lanes, 1)
        self.assertEqual(f.attrs['osm_lanes'][0], 3)

    def test_pavement_mesh_markings_and_terrain_use_the_narrow_ramp(self):
        f = fixture()
        # Mid-ramp, away from the shared mainline gore and KY 224 junction.
        ll = f.lonlat[11]
        frame = geodesy.EnuFrame(ll[1], ll[0])
        line = np.c_[f.xy_in(frame), np.full(len(f.lonlat), 10.)]
        sp = roads.road_specs(f)[0]
        marks = roads.extras(sp, line, float(ll[0]))
        self.assertFalse(any('dash' in style for style, _, _ in marks))
        self.assertEqual({style['m'] for style, _, _ in marks}, {'line_white', 'line_yellow'})
        with patch.object(roads, 'ROADS_IN_GROUND', False):
            chunks = roads.build_tile(frame, -1, -1, [f], flat)
        pavement = [c for c in chunks if c['m'] == roads.LOOK['paved']['m']]
        self.assertTrue(pavement)
        self.assertTrue(all(abs(c['w'] - 8.3) < 1e-6 for c in pavement))
        # A sample six metres off the centre was inside the old 17.9 m ramp;
        # it must now be unpaved verge in the physical terrain as well as outside
        # the painted pavement and mesh.
        tangent = line[12, :2] - line[10, :2]
        tangent /= np.linalg.norm(tangent)
        normal = np.array([-tangent[1], tangent[0]])
        outside = line[11, :2] + 6 * normal
        e, n = np.meshgrid(np.arange(-12., 12., .25), np.arange(-12., 12., .25))
        lat, lon, _ = frame.enu_to_geodetic(e, n, 0.)
        material = surface.paint_roads(np.full(e.shape, surface.GRASS, np.uint8), lat, lon, [f], .25)
        col, row = np.rint((outside + 12.) / .25).astype(int)
        self.assertEqual(material[48, 48], surface.ASPHALT)
        self.assertEqual(material[row, col], surface.VERGE)
        photo = surface.photo_roads(np.full((96, 96, 3), 200, np.uint8), frame, -12., -12., 24., [f])
        self.assertTrue(np.all(photo[48, 48] < 130))
        # Photo row zero is north; terrain row zero is south.
        np.testing.assert_array_equal(photo[96 - row, col], [200, 200, 200])


if __name__ == '__main__':
    unittest.main()
