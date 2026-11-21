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

    def test_default_counts_slab_and_girders_below_the_law_headroom(self):
        # (the law: 4 m under a bridge over a road with no height limit, a track's 6 m: a wagon and its catenary)
        self.assertEqual(roads.MIN_BRIDGE_HEADROOM_M, 4.0)
        self.assertAlmostEqual(roads.DECK_ROAD_CLEAR, roads.MIN_BRIDGE_HEADROOM_M + roads.BRIDGE_HEADROOM_MARGIN_M
                               + roadmesh.DECK_DEPTH + roadmesh.GIRDER_D)
        self.assertEqual(roads.RAIL_HEADROOM_M, 6.0)
        self.assertAlmostEqual(roads.deck_rail_clear(), roads.RAIL_HEADROOM_M + roads.BRIDGE_HEADROOM_MARGIN_M
                               + roadmesh.DECK_DEPTH + roadmesh.GIRDER_D)
        self.assertGreater(roads.deck_rail_clear(1.0), roads.deck_road_clear(1.0), "a track's is the higher")

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
        self.assertGreaterEqual(float(corridor[:, 2].min()) - 10.0, roads.MIN_BRIDGE_HEADROOM_M)
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
        self.assertGreaterEqual(usable, roads.MIN_BRIDGE_HEADROOM_M)

    def test_impossible_short_span_is_reported_without_a_vertical_wall(self):
        xy = line(60.0)
        z, = roads.deck_profile([xy], flat, [(0, 30.0, 10.0)])
        deficits = roads._clearance_deficits([xy], [z], [(0, 30.0, 10.0)])
        self.assertEqual(len(deficits), 1)
        self.assertLess(deficits[0][1], roads.MIN_BRIDGE_HEADROOM_M)
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

    def test_bridge_profiles_log_when_constraints_prevent_the_target(self):
        xy = line(60.0)
        lower = np.array([[30.0, -50.0], [30.0, 50.0]])
        bp = object.__new__(roads.BridgeProfiles)
        bp._ground = flat
        bp.frame = None
        bp._water = lambda lines, nav, depth=None, channel=0.0: []
        key = ("short", 0)
        pool = {key: (mock.Mock(width=12.0), xy, 0.0, 1.0, len(xy))}
        with mock.patch("bridgetypes.nav_clearance", return_value=0.0), \
                self.assertLogs("earthd.roads", level="DEBUG") as messages:     # (debug: GitHub #391)
            bp._solve([key], pool, {("lower", "g"): [lower]})
        self.assertIn(f"below {roads.MIN_BRIDGE_HEADROOM_M:.1f} m target", " ".join(messages.output))


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


class JointWeldTest(TestCase):
    """A way that changes level (the K5 line at Daikoku Junction, level 2 then 3): the part of one level is lifted to the
    deck of its level beside it and the other part is not, so the joint they share opened a step of 0.9-2.1 m."""

    def parts(self, partner_z):
        xa = np.arange(-60.0, 0.1, 4.0)
        xb = np.arange(0.0, 60.1, 4.0)
        a = np.c_[xa, np.zeros(len(xa))]
        b = np.c_[xb, np.zeros(len(xb))]
        c = np.c_[xb, np.full(len(xb), 7.0)]                    # a deck of the second part's level beside it
        zs = [np.full(len(xa), 10.0), np.full(len(xb), 10.0), np.full(len(xb), partner_z)]
        return [a, b, c], zs, [8.0, 8.0, 8.0], [2.0, 3.0, 3.0]

    def test_parts_sharing_a_joint_meet_at_one_height(self):
        for partner_z in (11.0, 13.0):
            lines, zs, widths, levels = self.parts(partner_z)
            out = roads._merge_overlaps(lines, zs, widths, levels)
            self.assertLess(abs(float(out[0][-1] - out[1][0])), 0.05, f"partner at {partner_z}")
            self.assertGreaterEqual(float(out[1].min()), 10.0 - 1e-6)
            self.assertGreaterEqual(float(out[0].min()), 10.0 - 1e-6, "never lowered")
            old = roads.MERGE_WELD_ON
            roads.MERGE_WELD_ON = False
            try:
                before = roads._merge_overlaps(lines, zs, widths, levels)
            finally:
                roads.MERGE_WELD_ON = old
            grade = np.abs(np.diff(out[0])) / 4.0
            self.assertLess(float(grade.max()), float((np.abs(np.diff(before[0])) / 4.0).max()) + 0.035,
                            "the raise is run out along the part")

    def test_off(self):
        old = roads.MERGE_WELD_ON
        try:
            roads.MERGE_WELD_ON = False
            lines, zs, widths, levels = self.parts(11.0)
            out = roads._merge_overlaps(lines, zs, widths, levels)
        finally:
            roads.MERGE_WELD_ON = old
        self.assertGreater(abs(float(out[0][-1] - out[1][0])), 0.1, "(the step it is for)")

    def test_an_abutment_keeps_its_height(self):
        # a joint a ground road goes on from is an abutment: held at the ground, never welded to the other part's end
        lines, zs, widths, levels = self.parts(13.0)
        pins = [np.array([0.0, 0.0])]
        out = roads._merge_overlaps(lines, zs, widths, levels, pins=pins)
        self.assertAlmostEqual(float(out[0][-1]), 10.0, places=6)
        self.assertAlmostEqual(float(out[1][0]), 10.0, places=6)


class SemiApproachIntegrationTest(TestCase):
    def test_approach_and_generated_deck_clear_the_law_headroom(self):
        fixture = approaches.OverpassIntegrationTest("test_deck_clears_the_lanes")
        separation, approach, deck = fixture.deck_over_lanes(True)
        # (the deck's own depth: roads.deck_depth of the 70 m bridge's span)
        usable = separation - roads.deck_depth(roads.geom_span([np.c_[np.linspace(-35, 35, 36), np.zeros(36)]]))
        self.assertGreaterEqual(float(usable.min()), roads.MIN_BRIDGE_HEADROOM_M)
        # (two roads, two laws: the approach road's ramp within its own road's grade, the generated deck within the
        # deck law. They were one number, the approach read 100 m out against the deck's: the legacy ramp's 0.05 passed
        # it and the shapes' ground there 0.0)
        self.assertLessEqual(approach, approaches.approach_law() * 1.05, "the approach is a ramp, not a wall")
        self.assertLessEqual(deck, roads.DECK_MAX_GRADE, "the generated deck climbs no steeper than the deck law")
