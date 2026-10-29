"""Experimental terrain-edge support; synthetic geometry, no game-success claim."""
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_roads import FRAME
import grading
import heightmap
import roadmesh
import roads
import revisit
from test_roads import features


def legacy_shoulder(spec):
    if spec.kind not in roads.BUILT:
        return 0.0
    if spec.cls in roads.SIDEWALK_CLASSES or (spec.cls == 'trunk' and (not spec.oneway or roads._arterial(spec))):
        return spec.walk + grading.SIDEWALK_MARGIN_M
    return grading.SHOULDER_M


class TerrainEdgeSupportTest(unittest.TestCase):
    def specs(self):
        return [roads.Spec(8.6, 'paved', False, cls, True, walk=walk)
                for cls in ('primary', 'trunk', 'motorway', 'residential')
                for walk in (0., .5, 2.5, 4.)]

    def test_off_preserves_existing_verges_and_profiles_exactly(self):
        theta = np.linspace(-.4, .4, 121)
        xy = np.c_[300. * np.sin(theta), 300. * (1. - np.cos(theta))]
        z = 20. + .02 * xy[:, 0]
        for spec in self.specs():
            with self.subTest(cls=spec.cls, walk=spec.walk):
                with mock.patch.object(grading, 'TERRAIN_EDGE_SUPPORT', False):
                    self.assertEqual(grading.shoulder(spec), legacy_shoulder(spec))
                    actual = grading._plan(xy, z, spec)
                with mock.patch.object(grading, 'shoulder', side_effect=legacy_shoulder):
                    expected = grading._plan(xy, z, spec)
                self.assertEqual(actual.half, expected.half)
                self.assertEqual(actual.road_half, expected.road_half)
                np.testing.assert_array_equal(actual.xy, expected.xy)
                np.testing.assert_array_equal(actual.z, expected.z)
                if actual.bank is None:
                    self.assertIsNone(expected.bank)
                else:
                    np.testing.assert_array_equal(actual.bank, expected.bank)

    def test_enabled_support_is_derived_from_the_terrain_grid(self):
        with mock.patch.object(grading, 'TERRAIN_EDGE_SUPPORT', True):
            for spec in self.specs():
                self.assertEqual(grading.shoulder(spec), max(legacy_shoulder(spec), np.hypot(heightmap.SQUARE, heightmap.SQUARE)))
            with mock.patch.object(heightmap, 'SQUARE', 4.):
                self.assertAlmostEqual(grading.shoulder(self.specs()[0]), np.hypot(4., 4.))

    def test_unbuilt_roads_are_not_widened(self):
        with mock.patch.object(grading, 'TERRAIN_EDGE_SUPPORT', True):
            for kind in ('dirt', 'gravel'):
                self.assertEqual(grading.shoulder(roads.Spec(4., kind, False, 'track')), 0.)

    def test_on_and_off_runs_do_not_reuse_the_same_generated_cache_key(self):
        keys = []
        for value in ('off', 'on'):
            with mock.patch.dict(os.environ, {'BNE_TERRAIN_EDGE_SUPPORT': value}), mock.patch.object(revisit, '_code', []):
                keys.append(revisit.key('plans', 41.71311, -105.87816))
        self.assertNotEqual(*keys)

    def test_bridge_and_tunnel_spans_still_have_no_ground_corridor(self):
        f = features([[(0., 0.), (100., 0.)], [(0., 20.), (100., 20.)], [(0., 40.), (100., 40.)]],
                     ['motorway'] * 3, flags=[(0, 'is_bridge'), (1, 'is_tunnel')])
        with mock.patch.object(grading, 'TERRAIN_EDGE_SUPPORT', True):
            specs = roads.road_specs(f)
        self.assertIsNone(specs[0])
        self.assertIsNone(specs[1])
        self.assertIsNotNone(specs[2])

    def test_pavement_and_longitudinal_profile_do_not_expand_when_enabled(self):
        xy = np.c_[np.arange(0., 201., 2.), np.zeros(101)]
        z = 30. + .04 * xy[:, 0]
        spec = roads.Spec(11.8, 'paved', False, 'motorway', True)
        with mock.patch.object(grading, 'TERRAIN_EDGE_SUPPORT', False):
            old = grading._plan(xy, z, spec)
        with mock.patch.object(grading, 'TERRAIN_EDGE_SUPPORT', True):
            new = grading._plan(xy, z, spec)
        self.assertGreater(new.half, old.half)
        self.assertEqual(new.road_half, old.road_half)
        self.assertEqual(spec.width, 11.8)
        np.testing.assert_array_equal(new.z, old.z)
        np.testing.assert_array_equal(new.xy, old.xy)

    def test_both_banked_edges_at_tile_boundaries_clear_visible_terrain(self):
        theta = np.linspace(-.4, .4, 121)
        xy = np.c_[512.3 + 300. * np.sin(theta), .7 + 300. * (1. - np.cos(theta))]
        for cross_slope in (-.08, 0., .08):
            with self.subTest(cross_slope=cross_slope), mock.patch.object(grading, 'TERRAIN_EDGE_SUPPORT', True):
                spec = roads.Spec(8.6, 'paved', False, 'primary', True, walk=0., median=30.)
                plan = grading._plan(xy, np.full(len(xy), 20.), spec)
                plan.bank = np.full(len(plan.xy), cross_slope)
                def elevation(la, lo, lift):
                    x, y, _ = FRAME.geodetic_to_enu(la, lo, 0.)
                    return grading.grade_points(np.asarray(x), np.asarray(y), np.full(np.shape(x), 40.), [plan], lift)
                visual = lambda la, lo: elevation(la, lo, 0.)
                physics = lambda la, lo: elevation(la, lo, grading.ROAD_LIFT)
                chunk = {'m': 'road_asphalt_2lane', 'tl': 10., 'w': spec.width,
                         'n': np.c_[xy, np.zeros(len(xy))].tolist()}
                for box in ((470., -20., 512., 30.), (512., -20., 554., 30.)):
                    p, triangles, *_ = roadmesh.build(FRAME, [chunk], physics, (0., 0., 0.),
                                                       clip=box, follow=True, q=1/32)
                    samples = np.r_[p[np.unique(triangles)], p[triangles].mean(axis=1)]
                    ground = roadmesh.drawn_ground(FRAME, visual, samples[:, :2], q=1/32)
                    collision = roadmesh.drawn_ground(FRAME, physics, samples[:, :2], q=1/32)
                    self.assertGreater(np.min(samples[:, 2] - ground), .005)
                    self.assertLess(np.max(abs(samples[:, 2] - collision)), roadmesh.FOLLOW_TOL + 1e-6)

    def test_flat_road_transitions_back_to_hillside_without_a_vertical_step(self):
        xy = np.c_[np.arange(-100., 101., 2.), np.zeros(101)]
        with mock.patch.object(grading, 'TERRAIN_EDGE_SUPPORT', True):
            plan = grading._plan(xy, np.full(len(xy), 10.), roads.Spec(11.8, 'paved', False, 'motorway', True))
        y = np.arange(0., 40., .25)
        z = grading.grade_points(np.zeros(len(y)), y, np.full(len(y), 12.), [plan])
        self.assertAlmostEqual(z[0], 10.)
        self.assertAlmostEqual(z[-1], 12.)
        self.assertLess(np.max(abs(np.diff(z))), .2)


if __name__ == '__main__':
    unittest.main()
