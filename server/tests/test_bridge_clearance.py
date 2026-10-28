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
    def test_fixed_deck_near_lattice_knots_keep_full_width(self):
        pts = [[0, 0, 10], [1.9999999999999998, 0, 11], [4, 0, 10]]
        chunk = dict(roads.DECK, w=12.0, deck=True, fixed=True, n=pts)
        left, right, _, _, _, z = roadmesh.ribbons([chunk])[0]
        np.testing.assert_allclose(np.linalg.norm(left - right, axis=1), 12.0)
        self.assertGreater(float(np.diff(left[:, 0]).min()), 1e-9)
        self.assertAlmostEqual(float(z.max()), 11.0)

    def test_clearance_floor_does_not_restore_a_sharp_crest(self):
        xy = line(800.0)
        z, = roads.deck_profile([xy], flat, [(0, 401.0, 10.0)])
        slopes = np.diff(z) / np.diff(xy[:, 0])
        self.assertLessEqual(float(np.abs(np.diff(slopes)).max()), 4.0 / 300.0 + 1e-6)
        self.assertEqual(roads._clearance_deficits([xy], [z], [(0, 401.0, 10.0)]), [])
        np.testing.assert_allclose(z[[0, -1]], 10.0)

    def test_fixed_deck_mesh_keeps_profile_knots(self):
        # Neither the crest nor the clearance corridor edge falls on the regular
        # ribbon lattice. Rendering and collision must retain both elevations.
        pts = np.array([[0, 0, 10], [9.3, 0, 18.5], [14.7, 0, 18.5], [25, 0, 10]])
        chunk = dict(roads.DECK, w=12.0, deck=True, fixed=True, n=pts.tolist())
        left, right, _, _, _, heights = roadmesh.ribbons([chunk])[0]
        vertices, *_ = roadmesh.deck([chunk], (0, 0, 0))
        for p in pts:
            at = np.flatnonzero(np.isclose(left[:, 0], p[0]))
            self.assertEqual(len(at), 1)
            self.assertAlmostEqual(heights[at[0]], p[2])
            self.assertTrue(np.any(np.isclose(vertices[:, 0], p[0]) & np.isclose(vertices[:, 2], p[2])))
        np.testing.assert_allclose(left[:, 0], right[:, 0])

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


class MergeJointTest(TestCase):
    def _y(self):
        # a deck over a road at x = 60 (two 60 m parts) and a short ramp part forking off at a 20 degree angle from
        # the joint at x = 90 to its own abutment 8 m away
        a = np.c_[np.arange(0.0, 90.1, 3.0), np.zeros(31)]
        b = np.c_[np.arange(90.0, 180.1, 3.0), np.zeros(31)]
        t = np.linspace(0.0, 8.0, 5)
        c = np.c_[90.0 + t * np.cos(0.35), t * np.sin(0.35)]
        return [a, b, c]

    def test_parts_meet_at_their_joint(self):
        lines = self._y()
        zs = roads.deck_profile(lines, flat, [(0, 60.0, 10.0)], levels=[1.0, 1.0, 1.0], widths=[8.5, 8.5, 6.0])
        joint = [float(zs[0][-1]), float(zs[1][0]), float(zs[2][0])]
        self.assertLess(max(joint) - min(joint), 0.05, joint)

    def test_merge_never_lifts_a_deck_steeply_off_its_abutment(self):
        lines = self._y()
        zs = roads.deck_profile(lines, flat, [(0, 60.0, 10.0)], levels=[1.0, 1.0, 1.0], widths=[8.5, 8.5, 6.0])
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(lines[0], axis=0).T))]
        first = (zs[0][1] - zs[0][0]) / (s[1] - s[0])
        self.assertLess(float(first), 0.25)


class TwinLiftTest(TestCase):
    def test_a_lift_to_a_twin_fades_no_steeper_than_a_deck_climbs(self):
        # two ramps of one level side by side from a joint, parting after 120 m (the High Five, Dallas: one lifted 11 m
        # to its twin fell back over ~60 m, 35 %, once they were 30 m apart)
        x = np.arange(0.0, 400.1, 4.0)
        low = np.c_[x, np.zeros(len(x))]
        bend = np.clip(x - 120.0, 0.0, None)
        high = np.c_[x, bend * np.tan(np.radians(12.0))]
        zl = 20.0 - 0.02 * x
        zh = np.full(len(x), 25.0)
        out = roads._merge_overlaps([low, high], [zl, zh], [8.0, 8.0], [4.0, 4.0])
        z = out[0]
        self.assertGreater(float(z[20]), 24.0)                  # beside its twin: lifted to it (18.4 m alone)
        grade = np.abs(np.diff(z)) / 4.0
        self.assertLess(float(grade.max()), 0.10)       # (the blend to the far abutment's cap adds ~3 %)


    def test_a_lift_never_reaches_past_a_joint_steeper_than_its_chain_allows(self):
        # a ramp coming down beside a higher deck of its level to a joint, then a 48 m part to the ground (Spaghetti
        # Junction: the joint was lifted 21 m and the short part dropped 40 %)
        x = np.arange(-200.0, 0.1, 4.0)
        ramp = np.c_[x, np.zeros(len(x))]
        twin = np.c_[x, np.full(len(x), 12.0)]
        t = np.arange(0.0, 48.1, 4.0)
        tail = np.c_[t, np.zeros(len(t))]
        zs = [110.0 + (98.0 - 110.0) * (x + 200.0) / 200.0, np.full(len(x), 120.0), 98.0 + (96.6 - 98.0) * t / 48.0]
        out = roads._merge_overlaps([ramp, twin, tail], zs, [8.0, 8.0, 8.0], [3.0, 3.0, 1.0])
        grade = np.abs(np.diff(out[2])) / 4.0
        self.assertLess(float(grade.max()), roads.DECK_MAX_GRADE + 0.01)
        self.assertAlmostEqual(float(out[0][-1]), float(out[2][0]), delta=0.5)     # (the parts still meet)


class SemiApproachIntegrationTest(TestCase):
    def test_approach_and_generated_deck_clear_six_metres(self):
        fixture = approaches.OverpassIntegrationTest("test_deck_clears_the_lanes")
        separation, grade = fixture.deck_over_lanes(True)
        usable = separation - roadmesh.DECK_DEPTH - roadmesh.GIRDER_D
        self.assertGreaterEqual(float(usable.min()), 6.0)
        self.assertLessEqual(grade, roads.DECK_MAX_GRADE)
