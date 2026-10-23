"""Long usable ramp lanes and regional travel-lane gaps, independent of ramp guidance."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import geodesy
import roads
import roadmesh
import highway_markings as markings
import grading
import surface
from test_roads import features, rules, FRAME, LAT, LON, flat
from test_grading import FakeMD


def interchange(entrance=False, reverse=False):
    ramp = [(300, 100), (200, 80), (150, 20), (-20, 10)] if entrance else [
        (300, 100), (400, 80), (450, 20), (650, 10)]
    direction = -1. if entrance else 1.
    f = features([[(-600, 100), (200, 100)], [(200, 100), (300, 100)], [(300, 100), (850, 100)],
                  ramp[::-1] if reverse else ramp], ['motorway']*4, ids=['upstream', 'before', 'after', 'ramp'])
    f.rules['oneway'] = rules([(0, 1.), (1, 1.), (2, 1.), (3, -direction if reverse else direction)], 'float')
    f.rules['subclass'] = rules([(3, 'link')])
    return f


def relocate(f, lat, lon):
    f.lonlat[:, 0] = lon+(f.lonlat[:, 0]-LON)*np.cos(np.radians(LAT))/np.cos(np.radians(lat))
    f.lonlat[:, 1] += lat-LAT
    return geodesy.EnuFrame(lat, lon, 0.)


class AuxiliaryLaneTest(unittest.TestCase):
    def test_exit_and_entrance_have_200_m_full_width_plus_70_m_taper(self):
        for entrance in (False, True):
            for reverse in (False, True):
                f = interchange(entrance, reverse)
                source, (_, zones, _, bands) = roads.cached_ramp_layout(f)
                self.assertEqual(len(bands), 1)
                band = bands[0]
                guide = band['guide']
                arc = np.r_[0., np.cumsum(np.linalg.norm(np.diff(guide[:, :2], axis=0), axis=1))]
                self.assertAlmostEqual(arc[-1], 270., delta=.04)
                self.assertAlmostEqual(arc[-1]-arc[band['taper']], 200., delta=2.)
                self.assertAlmostEqual(band['widths'][0], 3.2)
                self.assertAlmostEqual(max(band['widths']), 6.8)
                dots = next(z['line'] for runs in zones.values() for z in runs if z['mode'] == 'dotted')
                self.assertGreater(np.linalg.norm(np.diff(dots, axis=0), axis=1).sum(), 270.)
                np.testing.assert_allclose(dots[0], guide[0, :2], atol=1e-9,
                                           err_msg='dots start at the taper, filling its former blank divider')
                # Every dotted segment lies on the through/auxiliary divider, not on the taper's outer edge.
                self.assertLess(roads._seg_dist(dots[:30], guide[:-1, :2], guide[1:, :2]).min(1).max(), .03)
                la, lo, _ = source.enu_to_geodetic(guide[0, 0], guide[0, 1], guide[0, 2])
                x, _, _ = FRAME.geodetic_to_enu(la, lo, 0.)
                self.assertAlmostEqual(x, 570. if entrance else 30., delta=.7)

    def test_extension_is_graded_and_painted_as_asphalt(self):
        f = interchange()
        parts = roads.auxiliary_parts(f, 3)
        self.assertEqual(len(parts), 1)
        g = grading.Grader(FRAME, FakeMD(f), flat)
        plans = g._cell_plans(__import__('mapcells').cell_of(LAT, LON))
        self.assertTrue(any(':aux' in fid for fid, _ in plans))
        x, y = np.meshgrid(np.arange(100., 181., 1.), np.arange(85., 106., 1.))
        la, lo, _ = FRAME.enu_to_geodetic(x, y, 0.)
        material = surface.paint_roads(np.full(x.shape, surface.GRASS, np.uint8), la, lo, [f], 1.)
        self.assertEqual(material[7, 40], surface.ASPHALT, 'new outer shoulder has asphalt ground material')
        z = g.apply(la, lo, np.zeros(x.shape))
        self.assertGreater(z[7, 40], 9.9, 'physics ground supports the widened lane')

    def test_layout_and_dash_phase_stay_consistent_between_tiles(self):
        f = interchange()
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat)+roads.build_tile(FRAME, 1, 0, [f], flat)
        guidance = [c for c in chunks if c.get('ramp_guidance')]
        self.assertTrue(guidance)
        self.assertTrue(all(c.get('dash') == 1.8 and c.get('gap') == 5.5 for c in guidance))
        # At x=256 both overlapping tiles retain the same UV coordinate on the dotted divider.
        values = []
        for c in guidance:
            p = np.asarray(c['n'])
            arc = np.r_[0., np.cumsum(np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1))]
            if p[:, 0].min() <= 256. <= p[:, 0].max():
                order = p[:, 0].argsort()
                at = np.interp(256., p[order, 0], arc[order])
                values.append((c['uv_start']+at)/c['tl'])
        self.assertGreaterEqual(len(values), 2)
        self.assertLess(max(values)-min(values), .002)

    def test_nearby_interchange_limits_lane_length(self):
        node = np.array([300., 0., 0.])
        line = np.c_[np.arange(0., 601., 2.), np.zeros(301), np.zeros(301), np.zeros(301), np.ones(301)]
        band = roads._auxiliary_lane(node, np.array([1., 0.]), [(line, 3.6)], -1., 3.6,
                                     [np.array([180., 0., 0.])])
        guide = band['guide']
        self.assertAlmostEqual(np.linalg.norm(np.diff(guide[:, :2], axis=0), axis=1).sum(), 100.)

    def test_complete_approach_wins_over_partial_neighbour_cell_in_either_order(self):
        full = interchange()
        partial = features([[(200, 100), (300, 100)], [(300, 100), (850, 100)],
                            [(300, 100), (400, 80), (450, 20), (650, 10)]],
                           ['motorway']*3, ids=['before', 'after', 'ramp'])
        partial.rules['oneway'] = rules([(0, 1.), (1, 1.), (2, 1.)], 'float')
        partial.rules['subclass'] = rules([(2, 'link')])
        a = roads.projected_ramp_layout(FRAME, [partial, full])[3]
        b = roads.projected_ramp_layout(FRAME, [full, partial])[3]
        self.assertEqual(len(a), 1)
        np.testing.assert_allclose(a[0]['pts'], b[0]['pts'], atol=1e-8)
        self.assertAlmostEqual(a[0]['guide'][0, 0], 30., delta=.7)

    def test_ramp_dots_share_one_phase_across_curved_source_feature_boundaries(self):
        x = np.linspace(0., 200., 101)
        line = np.c_[x, .001*x*x, np.zeros(len(x))]
        guide = line[:, :2]
        zone = [{'line': guide, 'mode': 'dotted'}]
        look = {**roads.EDGE_LINE, 'edge_side': -1}
        left = roads._merge_edge_runs(line[:51], look, zone)[0]
        right = roads._merge_edge_runs(line[50:], look, zone)[0]
        a = roads._chunk(left[0], .25, left[1], line[:51])
        b = roads._chunk(right[0], .25, right[1], line[50:])
        length = np.linalg.norm(np.diff(np.asarray(a['n'])[:, :2], axis=0), axis=1).sum()
        self.assertAlmostEqual(a['uv_start']+length, b['uv_start'], delta=.005)
        self.assertEqual(a['tl'], 7.3)
        self.assertEqual(b['tl'], 7.3)

    def test_exit_and_entrance_use_the_same_pattern_in_both_source_orders(self):
        for entrance in (False, True):
            for reverse in (False, True):
                f = interchange(entrance, reverse)
                chunks = [c for tx in (0, 1, 2) for c in roads.build_tile(FRAME, tx, 0, [f], flat)]
                dots = [c for c in chunks if c.get('ramp_guidance')]
                self.assertTrue(dots)
                for c in dots:
                    self.assertEqual(c['w'], .25)
                    self.assertEqual(c['tl'], 7.3)
                    self.assertEqual((c['dash'], c['gap']), (1.8, 5.5))

    def test_exit_stripes_have_exact_length_and_gap_even_when_a_chunk_splits_a_stripe(self):
        reference = np.array([[0., 0., 0.], [99., 0., 0.]])
        zone = [{'line': reference[:, :2], 'mode': 'dotted', 'is_exit': True}]
        look = {**roads.EDGE_LINE, 'edge_side': -1}
        chunks = []
        for a, b in ((0., 44.5), (44.5, 99.)):
            line = np.array([[a, 0., 0.], [b, 0., 0.]])
            style, paint = roads._merge_edge_runs(line, look, zone)[0]
            chunks.append(roads._chunk(style, style['w'], paint, reference))
        spans = []
        for left, right, _, material, _, _ in roadmesh.ribbons(chunks):
            self.assertEqual(material, 'line_white')
            np.testing.assert_allclose(np.linalg.norm(left-right, axis=1), .25, atol=1e-9)
            spans.append((left[0, 0], left[-1, 0]))
        merged = []
        for a, b in sorted(spans):
            if merged and abs(a-merged[-1][1]) < 1e-8:
                merged[-1] = (merged[-1][0], b)
            else:
                merged.append((a, b))
        self.assertTrue(all(abs(b-a-1.8) < 1e-8 for a, b in merged))
        self.assertTrue(all(abs(c-b-5.5) < 1e-8 for (_, b), (c, _) in zip(merged[:-1], merged[1:])))

    def test_adjacent_exit_and_entrance_guides_keep_their_own_phase_references(self):
        line = np.array([[0., 0., 0.], [100., 0., 0.]])
        zones = [{'line': np.array([[0., 0.], [50., 0.]]), 'mode': 'dotted', 'is_exit': True},
                 {'line': np.array([[50., 0.], [100., 0.]]), 'mode': 'dotted', 'is_exit': False}]
        runs = roads._merge_edge_runs(line, {**roads.EDGE_LINE, 'edge_side': -1}, zones)
        self.assertEqual(len(runs), 2)
        self.assertEqual((runs[0][0]['dash'], runs[0][0]['gap']), (1.8, 5.5))
        self.assertEqual((runs[1][0]['dash'], runs[1][0]['gap']), (1.8, 5.5))
        self.assertIs(runs[0][0]['uv_reference'], zones[0]['line'])
        self.assertIs(runs[1][0]['uv_reference'], zones[1]['line'])
        np.testing.assert_allclose(runs[0][1][-1], runs[1][1][0])

    def test_reversed_guidance_chunks_render_identical_stripes_without_phase_breaks(self):
        for backwards in (False, True):
            reference = np.array([[0., 0., 0.], [99., 0., 0.]])
            if backwards:
                reference = reference[::-1]
            zones = [{'line': reference[:, :2], 'mode': 'dotted'}]
            look = {**roads.EDGE_LINE, 'edge_side': -1}
            baseline = None
            for split in (False, True):
                chunks = []
                ranges = [(0., 42.5), (42.5, 44.5), (44.5, 99.)] if split else [(0., 99.)]
                for index, (a, b) in enumerate(ranges):
                    line = np.array([[a, 0., 0., 0., 1.], [b, 0., 0., 0., 1.]])
                    if index % 2 == 0:
                        line = line[::-1].copy()
                        line[:, 3:5] *= -1
                    style, paint = roads._merge_edge_runs(line, look, zones)[0]
                    chunks.append(roads._chunk(style, style['w'], paint, reference))
                spans = []
                for left, right, *_ in roadmesh.ribbons(chunks):
                    np.testing.assert_allclose(np.linalg.norm(left-right, axis=1), .25, atol=1e-9)
                    spans.append((left[:, 0].min(), left[:, 0].max()))
                merged = []
                for a, b in sorted(spans):
                    if merged and a <= merged[-1][1]+1e-8:
                        merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
                    else:
                        merged.append((a, b))
                self.assertTrue(all(abs(b-a-1.8) < 1e-8 for a, b in merged))
                self.assertTrue(all(abs(c-b-5.5) < 1e-8 for (_, b), (c, _) in zip(merged[:-1], merged[1:])))
                if baseline is None:
                    baseline = merged
                else:
                    np.testing.assert_allclose(merged, baseline, atol=1e-8)

    def test_rendered_dots_cover_the_full_taper_on_exit_and_entrance(self):
        for entrance in (False, True):
            for reverse in (False, True):
                f = interchange(entrance, reverse)
                _, _, _, bands = roads.projected_ramp_layout(FRAME, [f])
                band = bands[0]
                guide = band['guide'][:, :2]
                direction = guide[band['taper']]-guide[0]
                taper = np.linalg.norm(direction)
                direction /= taper
                chunks = [c for tx in (0, 1, 2) for c in roads.build_tile(FRAME, tx, 0, [f], flat)
                          if c.get('ramp_guidance')]
                spans = []
                for left, right, *_ in roadmesh.ribbons(chunks):
                    at = (((left+right)/2)-guide[0]) @ direction
                    a, b = max(0., at.min()), min(taper, at.max())
                    if b-a > 1e-4:
                        spans.append((a, b))
                merged = []
                for a, b in sorted(spans):
                    if merged and a <= merged[-1][1]+.01:
                        merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
                    else:
                        merged.append((a, b))
                self.assertTrue(merged, 'the taper contains painted guidance')
                self.assertLessEqual(merged[0][0], 5.51)
                self.assertLessEqual(taper-merged[-1][1], 5.51)
                self.assertTrue(all(c-b <= 5.51 for (_, b), (c, _) in zip(merged[:-1], merged[1:])),
                                'the taper has only normal dotted gaps, without a long unpainted section')


class RegionalMarkingsTest(unittest.TestCase):
    def test_five_states_and_neighbours_use_polygons_not_bounding_boxes(self):
        inside = [(44.95, -93.09), (38.58, -121.49), (43.62, -116.21), (42.73, -84.55), (43.07, -89.40)]
        outside = [(46.877, -96.789), (39.53, -119.81), (47.66, -117.43),
                   (41.676, -86.252), (42.416, -90.429), (42.315, -83.036)]
        self.assertTrue(markings.selected(*np.array(inside).T).all())
        self.assertFalse(markings.selected(*np.array(outside).T).any())

    def test_only_highway_travel_lanes_get_12_m_gaps_in_selected_states(self):
        for lat, lon in ((44.95, -93.09), (38.58, -121.49), (43.62, -116.21), (42.73, -84.55), (43.07, -89.40)):
            f = interchange()
            frame = relocate(f, lat, lon)
            chunks = roads.build_tile(frame, 0, 0, [f], flat)
            lanes = [c for c in chunks if c.get('highway_lane')]
            dots = [c for c in chunks if c.get('ramp_guidance')]
            self.assertTrue(lanes)
            self.assertTrue(dots)
            dash = 4.5
            self.assertTrue(all(c.get('dash') == dash and c.get('gap') == 12. for c in lanes))
            self.assertTrue(all(c.get('dash') == 1.8 and c.get('gap') == 5.5 for c in dots))
            self.assertTrue(all(c['w'] == .25 for c in dots), 'ramp dots keep their separate width')
            width = .45
            self.assertTrue(all(c['w'] == width for c in lanes))
        f = interchange()
        self.assertTrue(all('gap' not in c for c in roads.build_tile(FRAME, 0, 0, [f], flat)
                            if not c.get('ramp_guidance')))

    def test_crossing_state_border_splits_pattern_without_a_geometry_gap(self):
        frame = geodesy.EnuFrame(41.7606, -85., 0.)
        c = {'n': [[0., -400., 0.], [0., 400., 0.]], 'm': roads.LANE_LINE['m'],
             'tl': 12., 'w': .12, 'p': 6, 'highway_lane': True, 'uv_start': 100.}
        pieces = markings.apply([c], frame)
        self.assertEqual(len(pieces), 2)
        self.assertNotIn('gap', pieces[0])
        self.assertEqual(pieces[1]['gap'], 12.)
        self.assertEqual([c['w'] for c in pieces], [.12, .45])
        np.testing.assert_allclose(pieces[0]['n'][-1], pieces[1]['n'][0])

    def test_physical_strokes_keep_regional_length_and_12_m_blank_gaps_across_mesh_chunks(self):
        for dash in (3., 4.5):
            base = {'m': roads.LANE_LINE['m'], 'w': .12, 'tl': 12., 'p': 6, 'dash': dash, 'gap': 12.}
            chunks = [{**base, 'n': [[0., 0., 0.], [47., 0., 0.]], 'uv_start': 0.},
                      {**base, 'n': [[47., 0., 0.], [99., 0., 0.]], 'uv_start': 47.}]
            spans = sorted((float(l[0, 0]), float(l[-1, 0])) for l, _, _, m, _, _ in roadmesh.ribbons(chunks))
            merged = []
            for a, b in spans:
                if merged and abs(a-merged[-1][1]) < 1e-8:
                    merged[-1] = (merged[-1][0], b)
                else:
                    merged.append((a, b))
            self.assertTrue(all(abs(b-a-dash) < 1e-8 for a, b in merged))
            self.assertTrue(all(abs(c-b-12.) < 1e-8 for (_, b), (c, _) in zip(merged[:-1], merged[1:])))

    def test_michigan_stripe_length_applies_to_both_peninsulas_and_not_neighbours(self):
        lat = [42.73, 46.54, 43.07, 44.95, 43.62, 38.58, 41.67]
        lon = [-84.55, -87.4, -89.4, -93.09, -116.21, -121.49, -86.25]
        np.testing.assert_array_equal(markings.dash_length(lat, lon), [4.5, 4.5, 4.5, 4.5, 4.5, 4.5, 0.])

    def test_michigan_width_reaches_the_actual_stroke_geometry(self):
        chunk = {'n': [[0., 0., 0.], [99., 0., 0.]], 'm': roads.LANE_LINE['m'],
                 'tl': 12., 'w': .12, 'p': 6, 'highway_lane': True}
        for lat, lon, width in ((42.73, -84.55, .45), (46.54, -87.4, .45), (43.07, -89.4, .45)):
            pieces = markings.apply([chunk], geodesy.EnuFrame(lat, lon, 0.))
            for left, right, _, _, _, _ in roadmesh.ribbons(pieces):
                np.testing.assert_allclose(np.linalg.norm(left-right, axis=1), width, atol=1e-9)

    def test_michigan_wisconsin_border_retains_the_same_requested_pattern(self):
        frame = geodesy.EnuFrame(45.999026, -88.671267, 0.)
        chunk = {'n': [[-200., 0., 0.], [200., 0., 0.]], 'm': roads.LANE_LINE['m'],
                 'tl': 12., 'w': .12, 'p': 6, 'highway_lane': True, 'uv_start': 100.}
        pieces = markings.apply([chunk], frame)
        self.assertEqual(len(pieces), 1)
        self.assertEqual((pieces[0]['dash'], pieces[0]['gap'], pieces[0]['w']), (4.5, 12., .45))
        np.testing.assert_allclose(pieces[0]['n'], chunk['n'])
        self.assertEqual(pieces[0]['uv_start'], 100.)

    def test_bridge_uses_regional_road_style_and_multilane_links_keep_their_separate_style(self):
        f = features([[(10, 100), (200, 100)]], ['motorway'], flags=[(0, 'is_bridge')])
        f.rules['oneway'] = rules([(0, 1.)], 'float')
        frame = relocate(f, 42.73, -84.55)
        chunks = roads.build_tile(frame, 0, 0, [f], flat)
        paint = [c for c in chunks if c.get('fixed') and c['m'].startswith('line_')]
        self.assertTrue(paint)
        lanes = [c for c in paint if c.get('highway_lane')]
        self.assertTrue(lanes)
        self.assertTrue(all((c['dash'], c['gap'], c['w']) == (4.5, 12., .45) for c in lanes))
        self.assertTrue(any(c['m'] == 'line_yellow' and c['w'] == .45 for c in paint))
        f = features([[(10, 100), (200, 100)]], ['motorway'], widths=[(0, 11.9)])
        f.rules['oneway'] = rules([(0, 1.)], 'float');f.rules['subclass'] = rules([(0, 'link')])
        frame = relocate(f, 42.73, -84.55)
        self.assertFalse(any(c.get('highway_lane') or 'gap' in c for c in roads.build_tile(frame, 0, 0, [f], flat)))


if __name__ == '__main__':
    unittest.main()
