"""Mapped lane totals and measured paint geometry, including clipped/fixed roads."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import navgraph
import roadmarkings
import roadmesh
import roads
import signstates
from test_roads import features, rules
from test_osmtags import tagged


LINE = np.array([[0., 0., 10.], [160., 0., 10.]])


class LaneGeometryTest(unittest.TestCase):
    def test_asymmetric_shoulders_follow_travel_direction(self):
        for link, left, right, lane in ((False, 1.5, 4.5, 4.), (True, .875, 2.625, 4.8)):
            for direction in (1, -1):
                width = roads.carriageway('motorway', 'paved', False, oneway=True, lanes=2, link=link)
                sp = roads.Spec(width, 'paved', False, 'motorway', True, dir=direction, lanes=2, link=link)
                ex = roads.extras(sp, LINE, -85.)
                edges = {lk['edge_side']: p[0, 1] for lk, _, p in ex if 'edge_side' in lk}
                self.assertAlmostEqual(width/2-direction*edges[direction], left)
                self.assertAlmostEqual(width/2+direction*edges[-direction], right)
                divider = next(p[0, 1] for lk, _, p in ex if 'dash' in lk)
                self.assertAlmostEqual(abs(edges[1]-divider), lane)
                self.assertAlmostEqual(abs(edges[-1]-divider), lane)
                self.assertAlmostEqual(divider, roads.driving_offset(sp))
        width = roads.carriageway('primary', 'paved', False, oneway=False, lanes=2, link=True)
        two_way = roads.Spec(width, 'paved', False, 'primary', lanes=2, link=True)
        self.assertEqual(roads.driving_offset(two_way), 0.)
        self.assertEqual(roads.edge_margin(two_way), 1.75)

    def test_wider_ramps_keep_all_lanes_between_the_shoulders(self):
        import grading
        for cls in ('motorway', 'trunk', 'primary'):
            for count in (None, 1, 2, 3):
                for level in ('ground', 'bridge', 'tunnel'):
                    for urban in (False, True):
                        with self.subTest(cls=cls, count=count, level=level, urban=urban):
                            tags = {} if urban else {'width': 6.0}
                            if count is not None:
                                tags['lanes'] = count
                            f = tagged([[(0, 0), (160, 0)]], [cls], ['w1@1'], {1: tags})
                            f.attrs['subclass'] = np.array(['link'])
                            f.rules['oneway'] = rules([(0, 1)], 'float')
                            if level != 'ground':
                                f.rules['flags'] = rules([(0, 'is_' + level)])
                            if level == 'ground':
                                sp = roads.road_specs(f, urban_fn=lambda lat, lon: np.full(np.shape(lat), urban))[0]
                                self.assertEqual(sp.urban, urban)
                            else:
                                sp = {'bridge': roads.bridge_specs, 'tunnel': roads.tunnel_specs}[level](f)[0]
                            sp = sp._replace(urban=urban, state='MI')
                            n = count or 1
                            self.assertEqual(sp.width, n * 4.8 + 3.5)
                            self.assertAlmostEqual(navgraph.driving_width(sp), n * 4.8)
                            ex = roads.extras(sp, LINE, -84.97)
                            edges = [p[0, 1] for lk, _, p in ex if lk['p'] == roads.EDGE_LINE['p']]
                            self.assertAlmostEqual(max(edges) - min(edges), n * 4.8)
                            self.assertAlmostEqual(sp.width / 2 - max(edges), .875)
                            self.assertAlmostEqual(sp.width / 2 + min(edges), 2.625)
                            self.assertEqual(sum('dash' in lk for lk, _, _ in ex), n - 1)
                            self.assertFalse(roads.has_sidewalks(sp))
                            plan = grading._plan(LINE[:, :2], LINE[:, 2], sp)
                            self.assertAlmostEqual(plan.road_half * 2, sp.width)

    def test_motorway_lane_width_and_shoulders_in_town_and_country(self):
        for urban in (False, True):
            for count in (None, 1, 2, 4):
                width = roads.carriageway('motorway', 'paved', urban, 8.0, True, count)
                sp = roads.Spec(width, 'paved', urban, 'motorway', True, lanes=count)
                n = count or 2
                self.assertEqual(width, n * 4.0 + 6.)
                self.assertEqual(roads.edge_margin(sp), 3.)
                self.assertEqual(navgraph.driving_width(sp), n * 4.0)
                ex = roads.extras(sp, LINE, -84.97)
                edges = [p[0, 1] for lk, _, p in ex if lk['p'] == roads.EDGE_LINE['p']]
                self.assertAlmostEqual(max(edges) - min(edges), n * 4.0)
                if count is None:
                    continue  # the deliberately constrained legacy spec below has a known lane count
                constrained = sp._replace(width=count * 4.0 + 1.0)
                self.assertEqual(roads.edge_margin(constrained), .5)
                self.assertEqual(navgraph.driving_width(constrained), count * 4.0)

    def test_mapped_counts_on_every_road_level(self):
        for cls in ('motorway', 'trunk', 'secondary', 'residential'):
            for count in (1, 2, 3, 4, 5, 14):
                for level in ('ground', 'bridge', 'tunnel'):
                    with self.subTest(cls=cls, count=count, level=level):
                        f = tagged([[(0, 0), (160, 0)]], [cls], ['w1@1'], {1: {'lanes': count}})
                        f.rules['oneway'] = rules([(0, -1)], 'float')
                        if level != 'ground':
                            f.rules['flags'] = rules([(0, 'is_' + level)])
                        sp = {'ground': roads.road_specs, 'bridge': roads.bridge_specs,
                              'tunnel': roads.tunnel_specs}[level](f)[0]
                        self.assertEqual((sp.lanes, sp.dir), (count, -1))
                        self.assertGreaterEqual(navgraph.driving_width(sp), count * roads.LANE_W.get(cls, 3.0) - .001)
                        dividers = [lk for lk, _, _ in roads.extras(sp, LINE, -76.) if 'dash' in lk]
                        self.assertEqual(len(dividers), count - 1)

    def test_odd_two_way_counts_are_not_rounded_down(self):
        for count in (3, 5, 7):
            f = tagged([[(0, 0), (160, 0)]], ['secondary'], ['w1@1'], {1: {'lanes': count}})
            sp = roads.road_specs(f)[0]
            ex = roads.extras(sp, LINE, -76.)
            self.assertEqual(sum('dash' in lk for lk, _, _ in ex), count - 2)
            centers = [ln[0, 1] for lk, _, ln in ex if lk['m'] == 'line_yellow']
            self.assertEqual(len(centers), 2)
            self.assertAlmostEqual(np.mean(centers), 3.5 / 2)
            self.assertEqual(sum(navgraph._lanes(navgraph.driving_width(sp), False, sp.lanes)), count)

    def test_link_attribute_and_minimum_deck_width(self):
        f = tagged([[(0, 0), (160, 0)]], ['motorway'], ['w1@1'], {1: {'lanes': 2, 'width': 10.5}})
        f.attrs['subclass'] = np.array(['link'])
        f.rules['oneway'] = rules([(0, 1)], 'float')
        f.rules['flags'] = rules([(0, 'is_bridge')])
        sp = roads.bridge_specs(f)[0]
        self.assertTrue(sp.link)
        self.assertEqual(sp.width, 13.1, 'two 4.8 m ramp lanes plus 0.875 m left / 2.625 m right shoulders')
        look = next(lk for lk, _, _ in roads.extras(sp, LINE, -76.) if 'dash' in lk)
        self.assertEqual(look['dash'], (1.2, 2.8))


class PaintTest(unittest.TestCase):
    def test_lower_class_ramps_and_divided_roads_keep_the_driver_left_yellow(self):
        for cls in ('motorway', 'trunk', 'primary', 'secondary', 'tertiary'):
            for direction in (-1, 1):
                for kind in ('ramp', 'divided'):
                    sp = roads.Spec(14., 'paved', False, cls, True, dir=direction, lanes=2,
                                    link=kind == 'ramp', median=30. if kind == 'divided' else None)
                    edges = [row for row in roads.extras(sp, LINE, -85.) if 'edge_side' in row[0]]
                    self.assertEqual(len(edges), 2)
                    for look, _, p in edges:
                        self.assertEqual(look['m'], 'line_yellow' if p[0, 1]*direction > 0 else 'line_white',
                                         (cls, kind, direction))
                    # Two-way roads still have white outer edges and a separate centre line.
                    edges = [lk for lk, _, _ in roads.extras(sp._replace(oneway=False, dir=0), LINE, -85.)
                             if 'edge_side' in lk]
                    self.assertTrue(all(lk['m'] == 'line_white' for lk in edges))

    def spec(self, state='', link=False, direction=1):
        return roads.Spec(11.6, 'paved', False, 'motorway', True, dir=direction,
                          lanes=2, state=state, link=link)

    def test_state_boundaries_and_fallback(self):
        self.assertEqual(list(signstates.states([42.73, 44.98, 43.07, 38.58, 51.5, 35.68],
                                                [-84.55, -93.27, -89.4, -121.49, -.12, 139.69])),
                         ['MI', 'MN', 'WI', 'CA', '', ''])
        f = features([[(0, 0), (100, 0)]], ['motorway'])
        f.lonlat[:] = [[-84.55, 42.73], [-84.549, 42.73]]
        self.assertEqual(roads.road_specs(f)[0].state, 'MI')

    def test_dimensions_measured_from_output(self):
        expected = [('', False, 3., 8., .35), ('', True, 1.2, 2.8, .35),
                    ('MI', False, 4.5, 11.5, .4), ('MI', True, 1.5, 6., .35),
                    ('MN', False, 3.1, 12.25, .3), ('KY', False, 3.5, 8.9, .6),
                    ('WI', False, 5.15, 10.25, .31), ('WI', True, 1.25, 2.9, .3),
                    ('CA', False, 4.6, 10.5, .35), ('CA', True, 2.3, 4.9, .25)]
        for state, link, length, gap, width in expected:
            with self.subTest(state=state, link=link):
                look, w, line = next(row for row in roads.extras(self.spec(state, link), LINE, -90.)
                                     if 'dash' in row[0])
                chunks = roadmarkings.paint([roads._chunk(look, w, line)])
                self.assertAlmostEqual(chunks[0]['n'][-1][0] - chunks[0]['n'][0][0], length)
                self.assertAlmostEqual(chunks[1]['n'][0][0] - chunks[0]['n'][-1][0], gap)
                self.assertEqual(chunks[0]['w'], width)
                ribbon = roadmesh.ribbons(chunks[:1])[0]
                np.testing.assert_allclose(np.linalg.norm(ribbon[0] - ribbon[1], axis=1), width)

    def test_solid_widths_and_driver_left_color(self):
        for state, width in [('', .35), ('MI', .4), ('MN', .45), ('WI', .32), ('CA', .25), ('KY', .6)]:
            for direction in (1, -1):
                for lon in (-90., 12., 139.):
                    edges = [row for row in roads.extras(self.spec(state, direction=direction), LINE, lon)
                             if row[0]['p'] == roads.EDGE_LINE['p']]
                    self.assertEqual(len(edges), 2)
                    for look, w, p in edges:
                        self.assertEqual(w, width)
                        self.assertEqual(look['m'], 'line_yellow' if p[0, 1] * direction > 0 else 'line_white')

    def test_minnesota_ramps_have_no_dashes(self):
        ex = roads.extras(self.spec('MN', True), LINE, -93.)
        self.assertFalse(any('dash' in lk for lk, _, _ in ex))
        self.assertEqual(len(ex), 2)

    def test_kentucky_ramps_have_no_dashes_but_highways_keep_dividers(self):
        from types import SimpleNamespace
        f = features([[(0, 0), (100, 0)]], ['motorway'])
        f.lonlat[:] = [[-84.5037, 38.0406], [-84.5027, 38.0406]]
        f.attrs['subclass'] = np.array(['link'])
        sp = roads.road_specs(f)[0]
        self.assertEqual(sp.state, 'KY')
        self.assertTrue(sp.link)
        self.assertIsNone(roadmarkings.profile(sp.state).ramp)
        self.assertEqual(list(signstates.states([38.2527, 38.0406, 36.9685, 39.1031, 36.1627],
                                                [-85.7585, -84.5037, -86.4808, -84.5120, -86.7816])),
                         ['KY', 'KY', 'KY', 'OH', 'TN'])
        for direction in (-1, 1):
            sp = self.spec('KY', True, direction)
            self.assertFalse(any('dash' in lk for lk, _, _ in roads.extras(sp, LINE, -85.)))
            source = [('ramp', sp, LINE, -85.)]
            tunnel = SimpleNamespace(parts=lambda cells, area: [('ramp', sp, LINE[:, :2], LINE[:, 2], -85.)])
            for chunks in (roads._deck_chunks(source, (0., -20., 160., 20.)),
                           roads._tunnel_chunks(tunnel, [], (0., -20., 160., 20.))):
                self.assertFalse(any(c['p'] == 6 for c in chunks))
                self.assertTrue(any(c['m'] == 'line_white' for c in chunks))
        self.assertTrue(any('dash' in lk for lk, _, _ in roads.extras(self.spec('KY'), LINE, -85.)))

    def test_phase_survives_tile_cuts_and_fixed_heights(self):
        look, w, source = next(row for row in roads.extras(self.spec(), LINE, -90.) if 'dash' in row[0])
        chunks = []
        for a, b in [(0., 12.), (12., 24.), (24., 40.)]:
            piece = np.array([[a, 0., 10.], [b, 0., 10.]])
            chunk = roads._chunk(look, w, piece)
            chunk['fixed'] = True
            chunks.extend(roadmarkings.paint([chunk]))
        spans = [(c['n'][0][0], c['n'][-1][0]) for c in chunks]
        np.testing.assert_allclose(spans, [(0., 3.), (11., 12.), (12., 14.), (22., 24.), (24., 25.), (33., 36.)])
        self.assertTrue(all(c['fixed'] and all(p[2] == 10. for p in c['n']) for c in chunks))
        self.assertEqual(roadmarkings.paint(chunks), chunks)

    def test_bridge_output_contains_short_ramp_dashes(self):
        chunks = roads._deck_chunks([('bridge', self.spec(link=True), LINE, -90.)], (0., -20., 40., 20.))
        dashes = [c for c in chunks if c['m'] == 'line_white' and c['p'] == 6]
        self.assertEqual(len(dashes), 10)
        self.assertTrue(all(c['fixed'] for c in dashes))
        self.assertAlmostEqual(dashes[0]['n'][-1][0] - dashes[0]['n'][0][0], 1.2)

    def test_two_way_bridge_and_tunnel_follow_left_hand_lane_split(self):
        from types import SimpleNamespace
        sp = roads.Spec(13.0, 'paved', False, 'secondary', lanes=3)
        tunnels = SimpleNamespace(parts=lambda cells, box: [('t', sp, LINE[:, :2], LINE[:, 2], -90.)])
        box = (0., -20., 40., 20.)
        for chunks in (roads._deck_chunks([('b', sp, LINE, -90.)], box, left_hand=True),
                       roads._tunnel_chunks(tunnels, [], box, left_hand=True)):
            centers = [c['n'][0][1] for c in chunks if c['m'] == 'line_yellow']
            self.assertEqual(len(centers), 2)
            self.assertAlmostEqual(np.mean(centers), -1.75, places=3)


if __name__ == '__main__':
    unittest.main()
