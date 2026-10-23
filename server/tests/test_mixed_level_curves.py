"""A short bridge/tunnel on a long feature must not leave kilometres of angular ground road."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import roadcontext
import roadmesh
import roads
from test_roads import FRAME, features, rules, flat


SOURCE = [(10, 70), (150, 70), (280, 105), (430, 105), (560, 140), (800, 140), (990, 140)]


def fixture(flags=((0, 'is_bridge', .8, .9),), source=SOURCE):
    f = features([source], ['motorway'], flags=list(flags))
    f.rules['oneway'] = rules([(0, 1.)], 'float')
    f.rules['lanes_forward'] = rules([(0, 2)], 'float')
    f.rules['lanes_backward'] = rules([(0, 0)], 'float')
    return f


def xy(ll):
    return np.column_stack(FRAME.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.))[:, :2]


def max_turn(points):
    d = np.diff(points[:, :2], axis=0)
    angles = np.unwrap(np.arctan2(d[:, 1], d[:, 0]))
    return np.degrees(abs(np.diff(angles))).max(initial=0.)


class MixedLevelCurvesTest(unittest.TestCase):
    def test_short_bridge_does_not_disable_ground_curve_smoothing(self):
        f = fixture()
        raw = xy(f.parts(0)[0][0])
        ground = roads.ground_parts(f, 0)
        self.assertGreater(max_turn(raw), 10., 'fixture contains visible raw polyline knees')
        self.assertGreater(len(ground[0]), 150)
        self.assertLess(max_turn(xy(ground[0])), .7)
        # Fairing permits 3 m, followed by corner fitting within half its 12 m budget.
        self.assertLess(roads._seg_dist(xy(ground[0]), raw[:-1], raw[1:]).min(1).max(), 9.)

    def test_bridge_and_tunnel_coordinates_and_cuts_are_unchanged_after_ground_fitting(self):
        f = fixture(flags=((0, 'is_bridge', .35, .4), (0, 'is_tunnel', .7, .76)))
        raw = f.parts(0)[0][0].copy()
        ranges = roads.level_ranges(f, 0)
        # Fit first: cached ground parameters must never be used against raw bridge/tunnel vertices.
        ground = roads.ground_parts(f, 0)
        bridge = roads.bridge_parts(f, 0)
        tunnel = roads.tunnel_parts(f, 0)
        self.assertEqual((len(ground), len(bridge), len(tunnel)), (3, 1, 1))
        for a, b, kind in ranges:
            actual = (bridge if kind == 'bridge' else tunnel)[0]
            np.testing.assert_array_equal(actual, roads.sub_polyline(raw, a, b))
        for k, span in enumerate((bridge[0], tunnel[0])):
            np.testing.assert_allclose(ground[k][-1], span[0], rtol=0., atol=1e-12)
            np.testing.assert_allclose(ground[k+1][0], span[-1], rtol=0., atol=1e-12)
        param = f._geometry_params[0, 0, 0]
        self.assertTrue((np.diff(param) > 0.).all())
        self.assertEqual(len(param), len(roads.geometry_parts(f, 0)[0][0]))
        np.testing.assert_array_equal(f.parts(0)[0][0], raw)

    def test_ground_approaches_match_the_fixed_span_direction(self):
        f = fixture(flags=((0, 'is_bridge', .31, .49),))
        ground = [xy(r) for r in roads.ground_parts(f, 0)]
        bridge = xy(roads.bridge_parts(f, 0)[0])
        for a, b in ((np.diff(ground[0], axis=0)[-1], np.diff(bridge, axis=0)[0]),
                     (np.diff(bridge, axis=0)[-1], np.diff(ground[1], axis=0)[0])):
            cosine = a@b/np.linalg.norm(a)/np.linalg.norm(b)
            self.assertGreater(cosine, np.cos(np.radians(.5)))

    def test_reversing_a_mixed_feature_preserves_its_physical_curves(self):
        a = fixture(flags=((0, 'is_bridge', .73, .79),))
        b = fixture(flags=((0, 'is_bridge', .21, .27),), source=SOURCE[::-1])
        pa, pb = roads.geometry_parts(a, 0)[0][0], roads.geometry_parts(b, 0)[0][0]
        ta, tb = a._geometry_params[0, 0, 0], b._geometry_params[0, 0, 0]
        at = np.linspace(0., 1., 1001)
        qa = xy(np.c_[np.interp(at, ta, pa[:, 0]), np.interp(at, ta, pa[:, 1])])
        qb = xy(np.c_[np.interp(at, 1.-tb[::-1], pb[::-1, 0]), np.interp(at, 1.-tb[::-1], pb[::-1, 1])])
        np.testing.assert_allclose(qa, qb, atol=.005)

    def test_real_junction_stays_exact_without_adding_a_false_knee(self):
        source = [(0, 70), (90, 70), (130, 70), (140, 70), (185, 70),
                  (230, 75), (350, 90), (520, 90), (700, 90)]
        f = features([source, [(140, 70), (140, -20)]], ['motorway', 'primary'],
                     flags=[(0, 'is_bridge', .9, .97)])
        fitted = xy(roads.ground_parts(f, 0)[0])
        junction = xy(f.parts(1)[0][0])[0]
        self.assertLess(np.linalg.norm(fitted-junction, axis=1).min(), 1e-5)
        near = fitted[abs(fitted[:, 0]-junction[0]) < 15.]
        self.assertLess(max_turn(near), .02, 'fairing cannot bend a straight highway at its pinned junction')

    def test_ground_curve_and_paint_remain_connected_in_neighboring_tile_meshes(self):
        f = fixture(source=[(10, 70), (150, 70), (330, 115), (530, 115), (850, 130)])
        f._road_context = roadcontext.Context(lambda key: f)
        seams = []
        for tx in (0, 1):
            chunks = roads.build_tile(FRAME, tx, 0, [f], flat)
            origin = np.array([tx*roads.TILE, 0., 0.])
            mesh = roadmesh.build(FRAME, chunks, flat, origin, clip=roads.tile_bbox(tx, 0), follow=True)
            p, t, _, materials, mi = mesh
            p = p+origin
            ids = np.unique(t[mi == materials.index('line_yellow')])
            cut = np.unique(np.round(p[ids][abs(p[ids, 0]-256.) < 1e-7, 1], 7))
            self.assertGreaterEqual(len(cut), 2)  # triangle diagonals may add interior boundary vertices
            self.assertGreater(cut[-1]-cut[0], .4)
            seams.append(cut[[0, -1]])
        np.testing.assert_allclose(seams[0], seams[1], atol=.005)

    def test_fully_off_ground_geometry_remains_the_source_polyline(self):
        for kind in ('is_bridge', 'is_tunnel'):
            f = fixture(flags=((0, kind, 0., 1.),))
            np.testing.assert_array_equal(roads.geometry_parts(f, 0)[0][0], f.parts(0)[0][0])

    def test_mesh_simplification_keeps_the_fitted_highway_heading(self):
        at = np.linspace(0., .25, 201)
        source = np.c_[1000.*np.sin(at), 1000.*(1.-np.cos(at)), np.zeros(len(at))]
        old = roads.simplify(source, roads.CURVE_SIMPLIFY_M)
        fitted = roads.simplify(source, roads.CURVE_SIMPLIFY_M, heading=roads.CURVE_HEADING_DEG)
        self.assertGreater(max_turn(old), .25)
        self.assertLess(max_turn(fitted), .21)
        self.assertLess(len(fitted), len(source), 'still reduce mesh data on gentle bends')
        np.testing.assert_array_equal(fitted[[0, -1]], source[[0, -1]])


if __name__ == '__main__':
    unittest.main()
