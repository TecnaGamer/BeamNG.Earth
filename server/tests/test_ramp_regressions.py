"""Width collapse at ramp nodes, altitude-dependent paint, curve kinks and bridge paint."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import geodesy
import roads
import roadmesh
from test_roads import FRAME, LAT, LON, features, flat, rules
from test_auxiliary_markings import interchange, relocate


class RampRegressionsTest(unittest.TestCase):
    def test_roundoff_at_rule_cuts_cannot_collapse_the_endpoint_width(self):
        f = features([[(10, 100), (120, 100), (230, 130)]], ['motorway'])
        ring = roads.geometry_parts(f, 0)[0][0]
        param = f.__dict__['_geometry_params'][0, 0, 0]
        # An ordinary floating-point normalisation can leave an endpoint just inside
        # its [0, 1] rule range. It must not be appended again as a zero-length edge.
        param[0], param[-1] = np.nextafter(0., 1.), np.nextafter(1., 0.)
        ground = roads.ground_parts(f, 0)[0]
        np.testing.assert_allclose(ground[[0, -1]], ring[[0, -1]], atol=1e-12)
        p = np.column_stack(FRAME.geodetic_to_enu(ground[:, 1], ground[:, 0], 0.))
        self.assertGreater(np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1).min(), 1e-4)
        cross = roads._with_cross(p, roads._join_sections(FRAME, [f]))[:, 3:5]
        self.assertTrue((np.linalg.norm(cross, axis=1) > .99).all(), 'full width through both endpoints')

    def test_distant_ramp_paint_and_pavement_do_not_move_with_elevation(self):
        f = interchange()
        frame = geodesy.EnuFrame(LAT-.1, LON, 0.)
        node = f.parts(3)[0][0][0]
        x, y, _ = frame.geodetic_to_enu(node[1], node[0], 0.)
        tx, ty = int(x//roads.TILE), int(y//roads.TILE)
        low = roads.build_tile(frame, tx, ty, [f], lambda la, lo: np.zeros(np.shape(la)))
        high = roads.build_tile(frame, tx, ty, [f], lambda la, lo: np.full(np.shape(la), 800.))
        def xy(chunks):
            return [(c['m'], c['w'], [p[:2] for p in c['n']]) for c in chunks]
        self.assertEqual(xy(low), xy(high), 'altitude cannot disable ramp paint overrides or join normals')
        self.assertTrue(any(c['m'] == 'line_white' and c['w'] == .45 for c in high))
        self.assertTrue(any(c.get('ramp_guidance') for c in high))
        edges = roads.projected_ramp_layout(frame, [f])[0]
        white = [np.asarray(c['n'])[:, :2] for c in high if c['m'] == 'line_white' and c['w'] == .45]
        starts, ends = np.vstack([p[:-1] for p in white]), np.vstack([p[1:] for p in white])
        checked = 0
        for runs in edges.values():
            for look, line in runs:
                if look['m'] != 'line_white':
                    continue
                for piece in roads.clip_polyline(line, *roads.tile_bbox(tx, ty)):
                    self.assertLess(roads._seg_dist(piece[:, :2], starts, ends).min(1).max(), .04,
                                    'both fitted solid ramp edges must actually reach the mesh')
                    checked += 1
        self.assertGreaterEqual(checked, 2)

    def test_endpoint_tangent_is_spread_over_the_approach(self):
        source = np.array([(0., 0.), (2., 0.), (30., .5), (120., 10.), (280., 24.)])
        direction = np.array([1., .03]); direction /= np.linalg.norm(direction)
        for reverse in (False, True):
            p = source[::-1] if reverse else source
            ends = (None, -direction) if reverse else (direction, None)
            curve, param = roads._smooth_road(p, end_dirs=ends)
            d = np.diff(curve, axis=0)
            heading = np.unwrap(np.arctan2(d[:, 1], d[:, 0]))
            self.assertLess(abs(np.diff(heading)).max(), np.radians(.3))
            self.assertTrue((np.diff(param) > 0.).all())
            np.testing.assert_allclose(curve[[0, -1]], p[[0, -1]], atol=1e-10)

    def test_mesh_resampling_retains_fitted_curve_vertices(self):
        p = np.array([[0., 0., 10.], [.7, .4, 10.], [4., 1., 10.]])
        xy, _, _ = roadmesh._resample(p, 2.)
        self.assertLess(np.linalg.norm(xy-p[1, :2], axis=1).min(), 1e-10)
        self.assertLessEqual(np.linalg.norm(np.diff(xy, axis=0), axis=1).max(), 2.+1e-10)


class BridgePaintRegressionsTest(unittest.TestCase):
    def test_bridge_matches_lane_count_colour_width_and_dash_phase_on_both_directions(self):
        for count in (2, 3, 4):
            for reverse in (False, True):
                with self.subTest(count=count, reverse=reverse):
                    lines = [[(10, 100), (90, 100)], [(90, 100), (170, 100)], [(170, 100), (250, 100)]]
                    if reverse:
                        lines = [p[::-1] for p in lines]
                    f = features(lines, ['motorway']*3, flags=[(1, 'is_bridge')])
                    f.rules['oneway'] = rules([(i, -1. if reverse else 1.) for i in range(3)], 'float')
                    for name, n in [('lanes_forward', 0 if reverse else count),
                                    ('lanes_backward', count if reverse else 0)]:
                        f.rules[name] = rules([(i, n) for i in range(3)], 'float')
                    frame = relocate(f, 42.73, -84.55)
                    chunks = roads.build_tile(frame, 0, 0, [f], flat)
                    paint = [c for c in chunks if c.get('fixed') and c['m'].startswith('line_')]
                    lanes = [c for c in paint if c.get('highway_lane')]
                    self.assertEqual(len(lanes), count-1)
                    self.assertTrue(all((c['dash'], c['gap'], c['w']) == (4.5, 12., .45) for c in lanes))
                    yellow = [c for c in paint if c['m'] == 'line_yellow']
                    white = [c for c in paint if c['m'] == 'line_white']
                    self.assertEqual((len(yellow), len(white)), (1, 1))
                    self.assertTrue(all(c['w'] == .45 for c in yellow+white))
                    self.assertGreater(np.mean(np.asarray(yellow[0]['n'])[:, 1]),
                                       np.mean(np.asarray(white[0]['n'])[:, 1]), 'yellow stays on the traffic-left edge')
                    for bridge in lanes+yellow+white:
                        for end in (0, -1):
                            q = np.asarray(bridge['n'])[end, :2]
                            connected = [c for c in chunks if not c.get('fixed') and c['m'] == bridge['m']]
                            distance = min(roads._seg_dist(q[None], np.asarray(c['n'])[:-1, :2],
                                                          np.asarray(c['n'])[1:, :2]).min() for c in connected)
                            self.assertLess(distance, .01, 'paint meets the approach at the deck abutment')
                    for bridge in lanes:
                        for c in chunks:
                            if c.get('fixed') or not c.get('highway_lane'):
                                continue
                            a, b = np.asarray(bridge['n']), np.asarray(c['n'])
                            if abs(a[0, 1]-b[0, 1]) < .01:
                                self.assertAlmostEqual(bridge['uv_start']-a[0, 0]*( -1 if reverse else 1),
                                                       c['uv_start']-b[0, 0]*( -1 if reverse else 1), delta=.01)


if __name__ == '__main__':
    unittest.main()
