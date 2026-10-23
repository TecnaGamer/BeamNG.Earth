"""Smooth map curves and connected ramp paint, including reversed sources and tile cuts."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import roads
import roadmesh
import navgraph
from test_roads import FRAME, features, rules, flat
from test_ramp_junctions import exit_ramp
from test_auxiliary_markings import interchange


def layout(f):
    rs = []
    specs = roads.road_specs(f)
    links = set()
    for i, sp in enumerate(specs):
        if roads.is_link(f, i):
            links.add(str(f.ids[i]))
        if sp is None:
            continue
        for ring in roads.ground_parts(f, i):
            e, n, _ = FRAME.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.)
            rs.append((str(f.ids[i]), sp, np.c_[e, n, np.zeros(len(e))], float(ring[:, 0].mean())))
    continuous, merges = roads._road_connections(rs, links)
    return roads._ramp_markings(rs, continuous, merges, roads._join_sections(FRAME, [f]), False)[:3], rs


def entrance_ramp(reverse=False):
    ramp = [(270, 100), (215, 80), (175, 45), (40, 35)]
    f = features([[(10, 100), (270, 100)], [(270, 100), (450, 100)],
                  ramp[::-1] if reverse else ramp], ['motorway']*3, ids=['before', 'after', 'ramp'])
    f.rules['oneway'] = rules([(0, 1.), (1, 1.), (2, 1. if reverse else -1.)], 'float')
    f.rules['subclass'] = rules([(2, 'link')])
    return f


class RoadCurvesTest(unittest.TestCase):
    def test_highway_fairing_straightens_small_jogs_without_moving_junctions(self):
        source = np.array([(0, 0), (90, 0), (135, 2.8), (175, -1.8),
                           (220, 2), (270, 0), (420, 0)], float)
        fair = roads._fair_highway_shape(source, [0, len(source)-1])
        self.assertLess(np.ptp(fair[:, 1]), .4*np.ptp(source[:, 1]))
        self.assertLessEqual(np.linalg.norm(fair-source, axis=1).max(), 3.+1e-9)
        np.testing.assert_array_equal(fair[[0, -1]], source[[0, -1]])
        reverse = roads._fair_highway_shape(source[::-1], [0, len(source)-1])
        np.testing.assert_allclose(fair, reverse[::-1], atol=1e-10)
        locked = roads._fair_highway_shape(source, [0, 3, len(source)-1])
        np.testing.assert_array_equal(locked[[0, 3, -1]], source[[0, 3, -1]])

    def test_fairing_is_shared_by_highway_skins_and_preserves_bridge_rule_cuts(self):
        source = [(10, 100), (100, 100), (145, 102.8), (185, 98.2),
                  (230, 102), (280, 100), (430, 100)]
        f = features([source], ['motorway'])
        f.rules['flags'] = rules([(0, 'is_bridge', .32, .57)])
        full = roads.geometry_parts(f, 0)[0][0]
        x, y, _ = FRAME.geodetic_to_enu(full[:, 1], full[:, 0], 0.)
        self.assertGreater(len(full), len(f.parts(0)[0][0]), 'ground sections still receive fitted curves')
        bridge = roads.bridge_parts(f, 0)[0]
        a, b, _ = roads.level_ranges(f, 0)[0]
        np.testing.assert_array_equal(bridge, roads.sub_polyline(f.parts(0)[0][0], a, b))
        ground = roads.ground_parts(f, 0)
        np.testing.assert_allclose(ground[0][-1], bridge[0], atol=1e-12)
        np.testing.assert_allclose(ground[-1][0], bridge[-1], atol=1e-12)

    def test_highway_bends_are_tangent_continuous_and_stay_close_to_the_map(self):
        xy = np.array([(0, 0), (120, 0), (250, 35), (400, 35), (520, 0)], float)
        smooth, _ = roads._smooth_road(xy)
        self.assertGreater(len(smooth), 200)
        np.testing.assert_allclose(smooth[[0, -1]], xy[[0, -1]])
        self.assertLess(roads._seg_dist(smooth, xy[:-1], xy[1:]).min(1).max(), 6.)
        delta = np.diff(smooth, axis=0)
        heading = np.unwrap(np.arctan2(delta[:, 1], delta[:, 0]))
        self.assertLess(np.max(np.abs(np.diff(heading))), np.radians(1.5))
        reverse, _ = roads._smooth_road(xy[::-1])
        np.testing.assert_allclose(smooth, reverse[::-1], atol=1e-9)

    def test_a_single_shallow_bend_has_no_artificial_reverse_turns(self):
        for xy in ([(0, 0), (240, 0), (370, 18), (520, 60)],
                   [(0, 0), (80, 0), (360, 35), (540, 100)],
                   [(0, 0), (150, 0), (300, 30)]):
            for reflect in (1, -1):
                source = np.asarray(xy, float)*[1, reflect]
                curve, param = roads._smooth_road(source)
                d = np.diff(curve, axis=0)
                angle = np.unwrap(np.arctan2(d[:, 1], d[:, 0]))*reflect
                self.assertGreaterEqual(np.diff(angle).min(), -1e-8,
                                        'a broad one-direction bend must not wiggle left then right')
                self.assertLess(abs(np.diff(angle)).max(), np.radians(1.))
                self.assertTrue((np.diff(param) > 0).all())

    def test_a_curved_t_junction_keeps_its_connector_exact_without_locking_other_bends(self):
        f = features([[(10, 80), (130, 80), (260, 110), (400, 170)],
                      [(130, 80), (130, 0)]], ['motorway', 'primary'])
        ll = roads.geometry_parts(f, 0)[0][0]
        e, n, _ = FRAME.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
        raw = f.parts(0)[0][0]
        x, y, _ = FRAME.geodetic_to_enu(raw[:, 1], raw[:, 0], 0.)
        self.assertLess(np.linalg.norm(np.c_[e, n]-[x[1], y[1]], axis=1).min(), .00001)
        self.assertGreater(np.linalg.norm(np.c_[e, n]-[x[2], y[2]], axis=1).min(), .5,
                           'ordinary shape vertices are still rounded on a road with a junction')

    def test_nearby_curve_samples_in_one_hash_bucket_are_not_snapped_together(self):
        p = np.array([[10., 80., 0.], [11.1, 80.1, 0.], [11.12, 80.102, 0.], [13., 80.35, 0.]])
        own = roads.offset_polyline(p, 1.)[:, :2]-p[:, :2]
        sections = {}
        for point, normal in zip(p, own):
            key = tuple(round(v*2) for v in point[:2])
            sections.setdefault(key, []).append((point[:2], normal, False))
        fitted = roads._with_cross(p, sections)
        np.testing.assert_allclose(fitted[:, :3], p, atol=1e-12)
        np.testing.assert_allclose(fitted[:, 3:5], own, atol=1e-12)

    def test_long_segments_do_not_swing_far_off_the_mapped_road(self):
        xy = np.array([(0, 0), (500, 0), (500, 500)], float)
        for cap in (4., 12.):
            curve, _ = roads._smooth_road(xy, bend_m=cap)
            distance = roads._seg_dist(curve, xy[:-1], xy[1:]).min(1)
            self.assertLess(distance.max(), cap*.5)
            self.assertTrue(np.isfinite(curve).all())

    def test_general_road_geometry_and_bridge_cuts_use_the_same_curve(self):
        f = features([[(10, 80), (130, 80), (250, 110), (400, 110)]], ['motorway'])
        f.rules['flags'] = rules([(0, 'is_bridge', .2, .8)])
        f.rules['oneway'] = rules([(0, 1.)], 'float')
        full = roads.geometry_parts(f, 0)[0][0]
        ground, bridge = roads.ground_parts(f, 0), roads.bridge_parts(f, 0)[0]
        np.testing.assert_allclose(ground[0][-1], bridge[0], atol=1e-12)
        np.testing.assert_allclose(ground[-1][0], bridge[-1], atol=1e-12)
        a, b, _ = roads.level_ranges(f, 0)[0]
        np.testing.assert_array_equal(bridge, roads.sub_polyline(f.parts(0)[0][0], a, b))

    def test_curves_join_across_reversed_feature_endpoints(self):
        f = features([[(10, 80), (130, 80)], [(260, 115), (130, 80)]], ['motorway']*2)
        lines = []
        for i in (0, 1):
            ll = roads.geometry_parts(f, i)[0][0]
            e, n, _ = FRAME.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
            lines.append(np.c_[e, n])
        u = lines[0][-1]-lines[0][-2]
        v = lines[1][-1]-lines[1][-2]
        self.assertGreater(abs(u @ v)/np.linalg.norm(u)/np.linalg.norm(v), .9999)
        np.testing.assert_allclose(lines[0][-1], lines[1][-1], atol=1e-7)

    def test_a_bend_across_map_features_is_one_curve_without_an_s_twist(self):
        for reverse in (False, True):
            second = [(130, 80), (260, 115), (430, 190)]
            f = features([[(10, 80), (130, 80)], second[::-1] if reverse else second], ['motorway']*2)
            pieces = []
            for i in (0, 1):
                ll = roads.geometry_parts(f, i)[0][0]
                x, y, _ = FRAME.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
                pieces.append(np.c_[x, y])
            if reverse:
                pieces[1] = pieces[1][::-1]
            np.testing.assert_allclose(pieces[0][-1], pieces[1][0], atol=1e-8)
            d = np.diff(np.vstack([pieces[0], pieces[1][1:]]), axis=0)
            angle = np.unwrap(np.arctan2(d[:, 1], d[:, 0]))
            self.assertGreaterEqual(np.diff(angle).min(), -1e-7)
            self.assertLess(abs(np.diff(angle)).max(), np.radians(.5))

    def test_ramp_split_paint_and_added_pavement_do_not_catch_up_in_a_sharp_jog(self):
        for entrance in (False, True):
            for reverse in (False, True):
                _, (edges, _, _, bands) = roads.cached_ramp_layout(interchange(entrance, reverse))
                for runs in edges.values():
                    white = [(look, p) for look, p in runs if look['m'] == 'line_white']
                    # The longer run is the connected approach to the painted gore.
                    node = bands[0]['guide'][-1, :2]
                    p = min((p for _, p in white), key=lambda p: np.linalg.norm(p[:, :2]-node, axis=1).min())
                    delta = np.diff(p[:, :2], axis=0)
                    angle = np.unwrap(np.arctan2(delta[:, 1], delta[:, 0]))
                    self.assertLess(abs(np.diff(angle)).max(), np.radians(1.5))
                p = bands[0]['pts']
                delta = np.diff(p[:, :2], axis=0)
                angle = np.unwrap(np.arctan2(delta[:, 1], delta[:, 0]))
                self.assertLess(abs(np.diff(angle)).max(), np.radians(1.5),
                                'the supporting pavement follows the same gentle ramp transition')

    def test_closed_curves_have_a_smooth_join_and_duplicate_nodes_are_safe(self):
        t = np.linspace(0., 2*np.pi, 17)
        xy = np.c_[30*np.cos(t), 30*np.sin(t)]
        xy = np.insert(xy, 5, xy[5], axis=0)
        curve, param = roads._smooth_road(xy)
        self.assertTrue(np.isfinite(curve).all())
        self.assertTrue((np.diff(param) > 0).all())
        u, v = curve[1]-curve[0], curve[-1]-curve[-2]
        self.assertGreater(u @ v/np.linalg.norm(u)/np.linalg.norm(v), np.cos(np.radians(5.)))
        self.assertLess(abs(u[0])/np.linalg.norm(u), .045)
        self.assertLess(abs(v[0])/np.linalg.norm(v), .045)

    def test_short_closed_rings_are_finite_and_keep_explicit_junctions(self):
        square = np.array([(0., 0.), (60., 0.), (60., 60.), (0., 60.), (0., 0.)])
        curve, param = roads._smooth_road(square, fixed=(.25,))
        self.assertTrue(np.isfinite(curve).all())
        self.assertTrue((np.diff(param) > 0.).all())
        np.testing.assert_allclose(curve[0], curve[-1], atol=1e-9)
        self.assertLess(np.linalg.norm(curve-square[1], axis=1).min(), 1e-9)

    def test_ai_connector_remains_at_its_mapped_junction_on_a_fitted_curve(self):
        f = features([[(10, 80), (130, 80), (260, 115), (500, 115)]], ['motorway'])
        ring = f.parts(0)[0][0]
        seg = np.hypot(np.diff(ring[:, 0])*np.cos(np.radians(ring[:, 1].mean())), np.diff(ring[:, 1]))
        at = seg[0]/seg.sum()
        f.rules['connectors'] = rules([(0, 'junction', at, at)])
        graph = navgraph.build(FRAME, [f], flat, (250., 100.))
        e, n, _ = FRAME.geodetic_to_enu(ring[1, 1], ring[1, 0], 0.)
        np.testing.assert_allclose(graph['nodes']['cjunction'][:2], [e, n], atol=.02)

    def test_exit_and_entrance_have_connected_solid_edges_and_usable_lane_guidance(self):
        for factory in (exit_ramp, entrance_ramp):
            for reverse in (False, True):
                with self.subTest(factory=factory.__name__, reverse=reverse):
                    f = factory(reverse)
                    (overrides, zones, exclusions), rs = layout(f)
                    self.assertEqual(set(overrides), {('ramp', -1), ('ramp', 1)})
                    mains = [(roads._with_cross(p, None), roads.driving_width(sp)/2)
                             for mid, sp, p, _ in rs if mid != 'ramp']
                    start_offsets = []
                    for key, runs in overrides.items():
                        white = [(look, line) for look, line in runs if look['m'] == 'line_white']
                        self.assertTrue(white)
                        ends = np.array([p for _, line in white for p in (line[0], line[-1])])
                        q, half = roads._nearest_road(ends, mains)
                        boundary = q[:, :2]-q[:, 3:5]*half[:, None]
                        distance = np.linalg.norm(ends[:, :2]-boundary, axis=1)
                        near = ends[distance.argmin()]
                        start_offsets.append(float(distance.min()))
                        paint = np.vstack([p for _, p in white])
                        q, half = roads._nearest_road(paint, mains)
                        outside = ((paint[:, :2]-q[:, :2])*-q[:, 3:5]).sum(1)-half
                        self.assertGreaterEqual(outside.min(), -.004,
                                                'gore paint never intrudes into the through travel lanes')
                    self.assertLess(min(start_offsets), .15, 'inner solid starts at the gore')
                    self.assertGreater(max(start_offsets), 3., 'outer solid continues the extra lane edge')
                    self.assertIn('before', exclusions['ramp'])
                    guide = next(z['line'] for runs in zones.values() for z in runs if z['mode'] == 'dotted')
                    length = np.linalg.norm(np.diff(guide, axis=0), axis=1).sum()
                    self.assertGreater(length, 8.)
                    self.assertGreater(length, 70., 'dotted divider spans the usable auxiliary lane')
                    self.assertLess(length, 320., 'solid gore begins before the ramp separates')

    def test_both_ramp_edges_survive_actual_tile_clipping_without_blank_sections(self):
        f = exit_ramp()
        (overrides, _, _), _ = layout(f)
        chunks = []
        for tx in (0, 1):
            chunks.extend(roads.build_tile(FRAME, tx, 0, [f], flat))
        paint = [np.asarray(c['n']) for c in chunks if c['m'] == 'line_white' and len(c['n']) >= 2]
        a, b = np.vstack([p[:-1, :2] for p in paint]), np.vstack([p[1:, :2] for p in paint])
        for runs in overrides.values():
            for look, line in runs:
                if look['m'] == 'line_white':
                    self.assertLess(roads._seg_dist(line[:, :2], a, b).min(1).max(), .04)

    def test_curved_highway_has_smooth_connected_ramp_edges(self):
        f = features([[(10, 100), (150, 100), (270, 120)],
                      [(270, 120), (400, 150), (650, 150)],
                      [(270, 120), (360, 100), (420, 40), (580, 20)]],
                     ['motorway']*3, ids=['before', 'after', 'ramp'])
        f.rules['oneway'] = rules([(0, 1.), (1, 1.), (2, 1.)], 'float')
        f.rules['subclass'] = rules([(2, 'link')])
        (overrides, zones, _), rs = layout(f)
        self.assertEqual(len(overrides), 2)
        self.assertTrue(zones)
        mains = [(roads._with_cross(p, None), roads.driving_width(sp)/2)
                 for mid, sp, p, _ in rs if mid != 'ramp']
        for runs in overrides.values():
            white = next(line for look, line in runs if look['m'] == 'line_white')
            q, half = roads._nearest_road(white, mains)
            outside = ((white[:, :2]-q[:, :2])*-q[:, 3:5]).sum(1)-half
            self.assertGreaterEqual(outside.min(), -.01)
            self.assertTrue(abs(outside[0]) < .15 or outside[0] > 3., 'solid begins at gore or the full extra lane edge')
            d = np.diff(white[:, :2], axis=0)
            heading = np.unwrap(np.arctan2(d[:, 1], d[:, 0]))
            self.assertLess(np.max(np.abs(np.diff(heading))), np.radians(15.))

    def test_bridge_split_does_not_duplicate_ramp_paint_on_other_ground_parts(self):
        f = exit_ramp()
        f.rules['flags'] = rules([(2, 'is_bridge', .45, .55)])
        (overrides, _, _), rs = layout(f)
        self.assertTrue(overrides)
        parts = [p for fid, _, p, _ in rs if fid == 'ramp']
        self.assertEqual(len(parts), 2)
        first_key = (tuple(parts[0][0, :2]), tuple(parts[0][-1, :2]))
        for runs in overrides.values():
            self.assertTrue(all(look['ramp_part'] == first_key for look, _ in runs))
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat)
        painted = [(c['m'], c['w'], tuple(tuple(p) for p in c['n']))
                   for c in chunks if c['m'].startswith('line_') and len(c['n']) > 3]
        self.assertEqual(len(painted), len(set(painted)), 'a bridge-split ramp head is painted only once')

    def test_dash_uv_phase_is_continuous_across_tile_and_mesh_splits(self):
        line = np.array([[10., 100., 0.], [710., 100., 0.]])
        left = roads._chunk(roads.LANE_LINE, .12, np.array([[250., 100., 0.], [256., 100., 0.]]), line)
        right = roads._chunk(roads.LANE_LINE, .12, np.array([[256., 100., 0.], [270., 100., 0.]]), line)
        lv = roadmesh.ribbons([left])[0][2][-1]
        rv = roadmesh.ribbons([right])[0][2][0]
        self.assertAlmostEqual(lv, rv, places=10)
        self.assertAlmostEqual(rv*roads.LANE_LINE['tl'], 256.)


if __name__ == '__main__':
    unittest.main()
