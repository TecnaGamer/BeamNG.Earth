"""Minimum usable headroom: profiles, actual slab/girder mesh, approaches and diagnostics."""
import os
import sys
from unittest import TestCase, mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import roads
import roadmesh
import test_approaches as approaches


def flat(xy):
    return np.full(len(xy), 10.0)


def line(length=400.0):
    x = np.arange(0.0, length + 1.0, 4.0)
    return np.c_[x, np.zeros(len(x))]


class MinimumBridgeClearanceTest(TestCase):
    def test_default_counts_slab_and_girders_below_six_metres(self):
        self.assertEqual(roads.MIN_BRIDGE_HEADROOM_M, 6.0)
        self.assertAlmostEqual(roads.DECK_ROAD_CLEAR,
                               6.0 + roads.BRIDGE_HEADROOM_MARGIN_M + roadmesh.DECK_DEPTH + roadmesh.GIRDER_D)

    def test_smoothing_cannot_lower_a_feasible_crossing_corridor(self):
        xy = line()
        z, = roads.deck_profile([xy], flat, [(0, 201.0, 10.0)])
        # A crossing, and the edges of its full-height corridor, between samples.
        at = np.linspace(181.0, 221.0, 161)
        usable = np.interp(at, xy[:, 0], z) - 10.0 - roadmesh.DECK_DEPTH - roadmesh.GIRDER_D
        self.assertGreaterEqual(float(usable.min()), roads.MIN_BRIDGE_HEADROOM_M)
        self.assertEqual(roads._clearance_deficits([xy], [z], [(0, 201.0, 10.0)]), [])
        self.assertAlmostEqual(z[0], 10.0)
        self.assertAlmostEqual(z[-1], 10.0)

    def test_actual_collision_vertices_clear_tall_vehicle_envelope(self):
        xy = line()
        z, = roads.deck_profile([xy], flat, [(0, 201.0, 10.0)])
        chunk = dict(roads.DECK, w=12.0, deck=True, fixed=True, n=np.c_[xy, z].tolist())
        vertices, triangles, *_ = roadmesh.deck([chunk], (0.0, 0.0, 0.0))
        corridor = vertices[(vertices[:, 0] >= 180.0) & (vertices[:, 0] <= 224.0)]
        self.assertGreater(len(corridor), 0)
        self.assertGreaterEqual(float(corridor[:, 2].min()) - 10.0, 6.0)
        self.assertTrue(np.isfinite(vertices).all())
        self.assertTrue(((triangles >= 0) & (triangles < len(vertices))).all())

    def test_overlapping_ramp_cannot_reduce_solved_floor(self):
        xy = line()
        z, = roads.deck_profile([xy], flat, [(0, 201.0, 10.0)])
        merged = roads._merge_overlaps([xy, xy + [0.0, 2.0]], [z, z - 2.0], [12.0, 12.0], [1.0, 1.0])
        self.assertTrue((merged[0] >= z - 1e-9).all())

    def test_stacked_crossing_decks_keep_usable_headroom(self):
        a = line(800.0) - [400.0, 0.0]
        b = np.c_[np.zeros(len(a)), a[:, 0]]
        low, high = roads.deck_profile([a, b], flat, levels=[1.0, 2.0], widths=[12.0, 12.0])
        usable = high[len(high) // 2] - low[len(low) // 2] - roadmesh.DECK_DEPTH - roadmesh.GIRDER_D
        self.assertGreaterEqual(usable, 6.0)

    def test_impossible_short_span_is_reported_without_a_vertical_wall(self):
        xy = line(60.0)
        z, = roads.deck_profile([xy], flat, [(0, 30.0, 10.0)])
        deficits = roads._clearance_deficits([xy], [z], [(0, 30.0, 10.0)])
        self.assertEqual(len(deficits), 1)
        self.assertLess(deficits[0][1], 6.0)
        self.assertLessEqual(float(np.abs(np.diff(z) / 4.0).max()), roads.DECK_MAX_GRADE + 0.02)
        self.assertAlmostEqual(z[0], 10.0)
        self.assertAlmostEqual(z[-1], 10.0)

    def test_true_abutment_junction_is_not_forced_into_an_overpass(self):
        xy = line(60.0)
        z, = roads.deck_profile([xy], flat, [(0, 6.0, 10.0)])
        self.assertLess(float(np.interp(6.0, xy[:, 0], z)), 10.0 + roads.AT_GRADE_M)
        self.assertEqual(roads._clearance_deficits([xy], [z], [(0, 6.0, 10.0)]), [])

    def test_explicit_water_requirement_is_not_replaced_by_vehicle_policy(self):
        xy = line(800.0)
        z, = roads.deck_profile([xy], flat, [(0, 400.0, 10.0, 20.0)])
        self.assertGreaterEqual(float(np.interp(400.0, xy[:, 0], z)) - 10.0, 20.0)

    def test_bridge_profiles_warn_when_constraints_prevent_the_target(self):
        xy = line(60.0)
        lower = np.array([[30.0, -50.0], [30.0, 50.0]])
        bp = object.__new__(roads.BridgeProfiles)
        bp._ground = flat
        bp.frame = None
        bp._water = lambda lines, nav: []
        key = ("short", 0)
        pool = {key: (mock.Mock(width=12.0), xy, 0.0, 1.0, len(xy))}
        with mock.patch("bridgetypes.nav_clearance", return_value=0.0), \
                self.assertLogs("earthd.roads", level="WARNING") as messages:
            bp._solve([key], pool, {("lower", "g"): [lower]})
        self.assertIn("below 6.0 m target", " ".join(messages.output))


class SemiApproachIntegrationTest(TestCase):
    def test_approach_and_generated_deck_clear_six_metres(self):
        fixture = approaches.OverpassIntegrationTest("test_deck_clears_the_lanes")
        separation, grade = fixture.deck_over_lanes(True)
        usable = separation - roadmesh.DECK_DEPTH - roadmesh.GIRDER_D
        self.assertGreaterEqual(float(usable.min()), 6.0)
        self.assertLessEqual(grade, roads.DECK_MAX_GRADE)
