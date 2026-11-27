"""Motorway "ski ramps" (the clearance study 2026-11-17, PR #1175's comment, GitHub #1176): each cause's switch, off
by default, and what it changes.

- BNE_FAST_DECK_ENDS: a motorway's branched deck chain is solved along a line a car drives, never a U through its two
  carriageways (roads.deck_curve fast).
- BNE_FAST_DECK_GROUND: a ramp beside a motorway's road is no twin of the motorway's abutment, which is never raised:
  its fill keeps clear of the motorway (grading._Planner._end_lift_calc).
- BNE_FAST_GAP: a motorway's profile by a deck end reads its own line across the elevation's opening (grading.gap_fill).
- BNE_UNDER_SPEED: a fast road's dip under a deck ramps on its design speed's curve (grading.under_speed_grade).
- BNE_LEVEL_SPACING (R1f): one clearance target per crossing for the approach fills, the dips and the deck solve.
"""
import os
import sys
import threading
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import revisit  # noqa: E402
import roads  # noqa: E402


OUT = np.array([-1.0, 0.0])         # (the test decks' direction out into the road)


def line(x0, x1, y0=0.0, y1=None, step=4.0):
    y1 = y0 if y1 is None else y1
    n = max(2, int(round(abs(x1 - x0) / step)) + 1)
    return np.c_[np.linspace(x0, x1, n), np.linspace(y0, y1, n)]


def max_grade(ln, z):
    ds = np.hypot(*np.diff(np.asarray(ln, np.float64)[:, :2], axis=0).T)
    return float(np.max(np.abs(np.diff(np.asarray(z, np.float64)) / np.maximum(ds, 1e-9))))


class FastDeckEnds(unittest.TestCase):
    """Kreuz Weinsberg's A81 viaduct (GitHub #1176): two carriageway decks from the south abutment (10 m) meet at a
    joint 200 m north, a 45 m span goes on to the north abutment 4 m lower. The abutments' own roads count as
    crossings at grade, the highest requirement on the south parts: their U was the trunk, held at 10 m, and the
    north span fell 4 m in its 45 m as a branch."""

    def chain(self):
        a = line(0.0, 200.0)                                     # one carriageway, south to the joint
        b = np.r_[line(200.0, 190.0, 0.0, 3.0)[:-1], line(190.0, 0.0, 3.0)]     # its twin, back south
        c = line(200.0, 245.0)                                   # the span on north
        lines = [a, b, c]

        def ground(xy):
            x = np.asarray(xy, np.float64)[:, 0]
            return np.where(x <= 2.0, 10.0, np.where(x >= 243.0, 6.0, 0.0))
        crossings = [(0, 0.0, 10.0), (1, float(np.hypot(*np.diff(b, axis=0).T).sum()), 10.0), (2, 45.0, 6.0),
                     (0, 120.0, 0.0)]
        return lines, ground, crossings

    def test_trunk_goes_on_through(self):
        lines, ground, crossings = self.chain()
        old = roads.deck_curve(lines, ground, crossings, clear=8.35)
        new = roads.deck_curve(lines, ground, crossings, clear=8.35, fast=True)
        self.assertGreater(max_grade(lines[2], old[2]), 0.08, "the U's branch: the north span falls 4 m in 45 m")
        self.assertLess(max_grade(lines[2], new[2]), 0.04, "a trunk through the joint: one gentle line")
        for z in new:
            self.assertTrue(np.isfinite(z).all())
        # the requirement over the road under it is held either way
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(lines[0], axis=0).T))]
        self.assertGreaterEqual(float(np.interp(120.0, s, new[0])), 8.35 - 0.05)

    def test_right_angle_joints(self):
        # Two long arms and one short continuation. Either direction through
        # the long arms wins on length until the angle exceeds 90 degrees.
        a = line(0.0, 50.0)
        c = line(50.0, 55.0)
        for ang, back in ((90.0, False), (89.9, False), (90.1, True), (135.0, True), (45.0, False)):
            t = np.radians(ang)
            b = np.c_[50.0 + np.linspace(0.0, 40.0, 11) * np.cos(t), np.linspace(0.0, 40.0, 11) * np.sin(t)]
            for lines, priority in (([a, b, c], 0), ([b[::-1], a[::-1], c], 1), ([a, b[::-1], c], 0)):
                nodes, edges, _ = roads._curve_graph(lines, None)
                path = roads._deck_paths(lines, nodes, edges, {priority: 100.}, True)[0]
                self.assertEqual({k for k, _ in path} == {0, 1}, not back, (ang, path))

    def test_equal_distance_paths_keep_heap_and_edge_order(self):
        # Equal-length routes through a diamond. The smaller node reaches
        # the far joint first, even when its outgoing edge comes later.
        edges = [(0, 1), (1, 3), (1, 2), (3, 4), (2, 4), (4, 5)]
        edges = [(a, b, np.array([0., 1.])) for a, b in edges]
        self.assertEqual(roads._deck_paths([], range(6), edges, {}, False)[0],
                         [(0, False), (2, False), (4, False), (5, False)])

    def test_closed_branch_uses_fallback_before_sampling_terrain(self):
        # A loop attached at just one joint cannot be fitted as an open branch.
        lines = [line(0, 50), line(50, 100), line(50, 75, 0, 25),
                 line(75, 25, 25), line(25, 50, 25, 0)]
        ground = mock.Mock(side_effect=AssertionError("unusable profile sampled terrain"))
        self.assertIsNone(roads.deck_curve(lines, ground, fast=True))
        ground.assert_not_called()

    def test_one_line_unchanged(self):
        ln = line(0.0, 320.0)
        cr = [(0, 160.0, 10.0)]
        a = roads.deck_curve([ln], lambda xy: np.full(len(xy), 10.0), cr)
        b = roads.deck_curve([ln], lambda xy: np.full(len(xy), 10.0), cr, fast=True)
        self.assertTrue(np.array_equal(a[0], b[0]))


class SwitchVersions(unittest.TestCase):
    """BNE_LEVEL_SPACING changed its meaning (the clearance study's class table): set, it is keyed with its version, so
    plans made under its old meaning miss; unset, every key is as before."""

    def keys(self, env):
        import hashlib
        with mock.patch.dict(os.environ, env, clear=True):
            revisit._switches.clear()
            try:
                new = revisit.plans_switches_key()
            finally:
                revisit._switches.clear()
            old = hashlib.sha1()
            for k in sorted(os.environ):
                if revisit.keyed_env(k) and k not in revisit.PLAN_NEUTRAL:
                    old.update(f"{k}={os.environ[k]}".encode())
        return new, old.hexdigest()[:16]

    def test_set_misses_unset_same(self):
        new, old = self.keys({"BNE_LEVEL_SPACING": "on"})
        self.assertNotEqual(new, old, "the old meaning's plans miss")
        for k in ("BNE_FAST_DECK_GROUND", "BNE_UNDER_SPEED", "BNE_FAST_GAP"):
            new, old = self.keys({k: "on"})
            self.assertNotEqual(new, old, k + ": its reviewed meaning")
        # (and the earlier versions' keys miss too: FAST_GAP and LEVEL_SPACING at 3, Codex round 4; FAST_GAP at 4;
        # LEVEL_SPACING at 4, the chain's one target before each crossing's own)
        for k, was in (("BNE_FAST_GAP", 2), ("BNE_FAST_GAP", 3), ("BNE_FAST_GAP", 4), ("BNE_LEVEL_SPACING", 2),
                        ("BNE_LEVEL_SPACING", 3), ("BNE_LEVEL_SPACING", 4)):
            with mock.patch.dict(revisit.SWITCH_VERSIONS, {k: was}):
                prev, _old = self.keys({k: "on"})
            new, _old = self.keys({k: "on"})
            self.assertNotEqual(new, prev, k + " at its earlier version: its plans miss")
        new, old = self.keys({"BNE_FAST_DECK_ENDS": "on"})
        self.assertEqual(new, old, "a switch with no version: as before")
        new, old = self.keys({})
        self.assertEqual(new, old, "the default namespace")


class FastDeckGround(unittest.TestCase):
    """The 417's exit ramp at Carling Avenue (Ottawa): the ramp's abutment beside the motorway's, whose clearance asks
    a lift that is never made (a motorway is never raised): counted as a twin rising with the ramp, the motorway's
    road left the ramp's caps and its fill lifted the ground under the motorway's deck start."""

    def planner(self, never_raised):
        import geodesy
        g = object.__new__(grading._Planner)
        g.lock = threading.Lock()
        g.frame = geodesy.EnuFrame(45.385, -75.738)
        beside = np.array([[0.0, 11.0], [-80.0, 11.0]])
        other_end = np.array([0.5, 11.0])
        lifts = {(0.0, 0.0): (5.0, [(beside, 6.0)], [(np.array([[0.0, 0.0], [-80.0, 0.0]]), 4.0)], False),
                 (0.5, 11.0): (4.6, [], [], False)}
        g._end_lift_raw = lambda xy: lifts[(float(xy[0]), float(xy[1]))]
        g._ends_near = lambda pts, *a, **k: [np.array([other_end])]
        g.elev = lambda la, lo: np.zeros(len(np.atleast_1d(la)))
        g._jr_at = lambda *a, **k: False
        g._end_never_raised = lambda xy: never_raised
        return g, beside

    def kept(self, on, never_raised):
        g, beside = self.planner(never_raised)
        with mock.patch.object(grading, "FAST_DECK_GROUND", on):
            got = g._end_lift_calc(np.array([0.0, 0.0]))
        return any(ln is beside for ln, _h in got[1])

    def test_motorway_beside_caps_the_ramp(self):
        self.assertFalse(self.kept(False, True), "off: a twin, left out of the caps (as before)")
        self.assertTrue(self.kept(True, True), "on: the motorway never rises, it caps the ramp's fill")
        self.assertFalse(self.kept(True, False), "a road whose abutment does rise is still a twin")


class GapLanding(unittest.TestCase):
    """Codex rounds 2 and 3: one line per landing, along the road a car drives off the deck on. The A7 at Thulba ran
    into its hole through a 4 m lane-section stub between the road and the deck. The stub and the road ending short of
    it (straight, or bent 35 degrees at their joint), planned together or apart, either way round, with or without the
    other as an extension, meet at one height and grade; the other carriageway 3 m beside, its own line 2 m higher, is
    never filled from this landing nor this road from its."""

    def setUp(self):
        import geodesy
        self.frame = geodesy.EnuFrame(50.198, 9.939)
        self.spec = roads.Spec(11.5, "paved", False, "motorway")

    def approach(self, bend):
        t = np.radians(bend)
        far = np.array([-4.0, 0.0]) + 196.0 * np.array([-np.cos(t), np.sin(t)])
        return np.array([[0.0, 0.0], [-4.0, 0.0], far])

    def raw_for(self, ap, twin):
        def raw(la, lo):
            e, n, _ = self.frame.geodetic_to_enu(la, lo, 0.0)
            pts = np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], axis=1)
            s_, off = grading._on_line(pts, ap)
            road = 50.0 + 0.037 * s_                       # 3.7 % down to the deck along the approach
            road = road - 4.0 * np.clip(1.0 - s_ / 14.0, 0.0, 1.0) * (off < 1.5)      # its opening
            st, ot = grading._on_line(pts, twin)
            twin_z = 52.0 + 0.037 * st - 4.0 * np.clip(1.0 - st / 14.0, 0.0, 1.0)
            return np.where(ot < off, twin_z, road)
        return raw

    def lonlat(self, xy):
        la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return np.c_[lo, la]

    def dense(self, a, b, step=2.0):
        n = max(2, int(np.ceil(np.hypot(*(b - a)) / step)) + 1)
        return np.c_[np.linspace(a[0], b[0], n), np.linspace(a[1], b[1], n)]

    def check(self, bend, shift=0.0):
        ap = self.approach(bend)
        twin = ap + np.array([0.0, 3.0])
        raw = self.raw_for(ap, twin)
        road_map, stub_map = ap[[2, 1]], ap[[1, 0]]                       # (the map's own lines)
        lands = [(ap, "motorway", [road_map, stub_map], [twin[[2, 1]], twin[[1, 0]]], frozenset({"road", "stub"}), OUT),
                 (twin, "motorway", [twin[[2, 1]], twin[[1, 0]]], [road_map, stub_map], frozenset({"troad", "tstub"}), OUT)]
        # the road, ending 4 m short of the deck, as one cell's lane placement puts it: shift m sideways (a lane
        # section's placed copy); the stub on to the deck as mapped
        road_xy = self.dense(ap[2], ap[1]) + np.array([0.0, -shift])
        stub_xy = self.dense(ap[1], ap[0])
        road, stub = (self.lonlat(road_xy), self.spec), (self.lonlat(stub_xy), self.spec)
        kw = dict(cuts=False, landings=lands)
        runs = {"together": grading.plan_roads(self.frame, [road, stub], raw, **kw),
                "apart": [grading.plan_roads(self.frame, [road], raw, **kw)[0],
                          grading.plan_roads(self.frame, [stub], raw, **kw)[0]],
                "apart, reversed": grading.plan_roads(self.frame, [stub], raw, **kw)[::-1] or None}
        runs["apart, reversed"] = [grading.plan_roads(self.frame, [road], raw, **kw)[0], runs["apart, reversed"][0]]
        runs["extensions"] = [grading.plan_roads(self.frame, [road], raw, ext=[(None, stub_xy)], **kw)[0],
                              grading.plan_roads(self.frame, [stub], raw, ext=[(road_xy[::-1][:60], None)], **kw)[0]]
        joint = 50.0 + 0.037 * 4.0
        for name, (r, s_) in runs.items():
            self.assertAlmostEqual(float(r.z[-1]), joint, delta=0.03, msg=(bend, name, "road at the joint"))
            self.assertAlmostEqual(float(s_.z[0]), joint, delta=0.02, msg=(bend, name, "stub at the joint"))
            self.assertAlmostEqual(float(s_.z[-1]), 50.0, delta=0.02, msg=(bend, name, "at the deck"))
            g_r = (float(r.z[-1]) - float(r.z[-2])) / float(np.hypot(*np.diff(r.xy[-2:, :2], axis=0)[0]))
            g_s = (float(s_.z[1]) - float(s_.z[0])) / float(np.hypot(*np.diff(s_.xy[:2, :2], axis=0)[0]))
            self.assertAlmostEqual(g_r, -0.037, delta=0.006, msg=(bend, name, "road's grade at the joint"))
            self.assertAlmostEqual(g_s, -0.037, delta=0.006, msg=(bend, name, "stub's grade at the joint"))
        # the twin's carriageway (3 m beside, 2 m higher) planned with both landings: its own line, not this one's
        tw = grading.plan_roads(self.frame, [(self.lonlat(self.dense(twin[1], twin[0])), self.spec)], raw, **kw)[0]
        self.assertAlmostEqual(float(tw.z[-1]), 52.0, delta=0.02)

    def test_straight(self):
        self.check(0.0)

    def test_bent(self):
        self.check(35.0)

    def test_placed_copy(self):
        # (Codex round 4: a lane section's placed copy 1 m off the mapped line, its neighbour as mapped, in two cells)
        self.check(0.0, shift=1.0)
        self.check(35.0, shift=1.0)

    def test_placed_by_its_road(self):
        # (Codex round 5: a four-lane motorway's lanes placed left_of: its plan 4 m off the mapped line, the other
        # carriageway's mapped line 6 m off on the same side, one terrain for both. Taken by its road, the placed copy
        # and the stub as mapped meet at one height; the other carriageway's road (another feature) takes nothing from
        # this landing, nor does the placed copy when its road is not known (4 m off, nearer the other's line))
        ap = self.approach(0.0)
        twin = ap + np.array([0.0, -6.0])

        def raw(la, lo):
            e, n, _ = self.frame.geodetic_to_enu(la, lo, 0.0)
            pts = np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], axis=1)
            s_, _off = grading._on_line(pts, ap)
            return 50.0 + 0.037 * s_ - 4.0 * np.clip(1.0 - s_ / 14.0, 0.0, 1.0)
        road_map, stub_map = ap[[2, 1]], ap[[1, 0]]
        lands = [(ap, "motorway", [road_map, stub_map], [twin[[2, 1]], twin[[1, 0]]], frozenset({"road", "stub"}), OUT)]
        road_xy = self.dense(ap[2], ap[1]) + np.array([0.0, -4.0])
        stub_xy = self.dense(ap[1], ap[0])
        kw = dict(cuts=False, landings=lands)
        joint = 50.0 + 0.037 * 4.0
        r = grading.plan_roads(self.frame, [(self.lonlat(road_xy), self.spec)], raw, part_ids=["road"],
                               ext_ids=[(None, "stub")], ext=[(None, stub_xy)], **kw)[0]
        s_ = grading.plan_roads(self.frame, [(self.lonlat(stub_xy), self.spec)], raw, part_ids=["stub"], **kw)[0]
        self.assertAlmostEqual(float(r.z[-1]), joint, delta=0.03)
        self.assertAlmostEqual(float(s_.z[0]), joint, delta=0.02)
        bare = grading.plan_roads(self.frame, [(self.lonlat(road_xy), self.spec)], raw, **kw)[0]
        self.assertLess(float(bare.z[-1]), joint - 0.5)
        tw = grading.plan_roads(self.frame, [(self.lonlat(self.dense(twin[1], twin[0])), self.spec)], raw,
                                part_ids=["tstub"], **kw)[0]
        self.assertLess(float(tw.z[-1]), 50.0 - 1.0, "the other carriageway: nothing from this landing")

    def test_not_across_the_bridge(self):
        # (Codex review of round 5: one feature with a ground piece on each bank of an 8 m bridge: the far bank's
        # samples, of the same road, take nothing from this bank's landing, whose line stands 3 m higher)
        ap = self.approach(0.0)

        def raw(la, lo):
            e, n, _ = self.frame.geodetic_to_enu(la, lo, 0.0)
            x = np.asarray(e, np.float64)
            return np.where(x < 0.0, 53.0 - 0.037 * x, 50.0 + 0.037 * (x - 8.0))   # (this bank higher)
        lands = [(ap, "motorway", [ap[[2, 1]], ap[[1, 0]]], [], frozenset({"road"}), OUT)]
        far_xy = self.dense(np.array([8.0, 0.0]), np.array([200.0, 0.0]))
        far = grading.plan_roads(self.frame, [(self.lonlat(far_xy), self.spec)], raw, cuts=False, landings=lands,
                                 part_ids=["road"])[0]
        ref = grading.plan_roads(self.frame, [(self.lonlat(far_xy), self.spec)], raw, cuts=False)[0]
        self.assertTrue(np.allclose(far.z, ref.z))

    def test_angled_placed_far_bank_piece(self):
        # (Codex review of round 5: the same feature's far-bank piece leaves the deck at 45 degrees, placed 8 m aside so
        # its first samples come round in front of this bank's deck end, 6 m from the approach: told apart by its own
        # ground piece (road#1, the landing's road#0), it takes nothing; as the landing's own piece it would)
        ap = self.approach(0.0)

        def raw(la, lo):
            e, n, _ = self.frame.geodetic_to_enu(la, lo, 0.0)
            x = np.asarray(e, np.float64)
            return np.where(x < 0.0, 53.0 - 0.037 * x, 50.0 + 0.037 * x)        # (this bank higher)
        lands = [(ap, "motorway", [ap[[2, 1]], ap[[1, 0]]], [], frozenset({"road#0"}), OUT)]
        s2 = np.sqrt(0.5)
        far_xy = self.dense(np.array([-0.66, 5.66]), np.array([-0.66 + 150.0 * s2, 5.66 + 150.0 * s2]))

        def plan(pid):
            return grading.plan_roads(self.frame, [(self.lonlat(far_xy), self.spec)], raw, cuts=False,
                                      landings=lands if pid else None, part_ids=[pid] if pid else None)[0]
        ref = plan(None)
        self.assertTrue(np.allclose(plan("road#1").z, ref.z))
        self.assertFalse(np.allclose(plan("road#0").z, ref.z), "(the guard alone would not tell them apart)")

    def test_stale_piece_ordinal(self):
        # (Codex review of round 5: the plan's pieces from a layer's kept ground_parts, made from a short home copy that
        # had no bridge: one unsplit piece, road#0; the landing from the clean copy, its approach the far bank's road#1.
        # Another piece of the landing's own feature goes by where it lies: on the approach, it takes the line)
        ap = self.approach(0.0)
        lands = [(ap, "motorway", [ap[[2, 1]], ap[[1, 0]]], [], frozenset({"road#1"}), OUT)]
        raw = self.raw_for(ap, ap + np.array([0.0, 50.0]))
        road_xy = self.dense(np.array([-200.0, 0.0]), np.array([-1.0, 0.0]))

        def plan(pid):
            return grading.plan_roads(self.frame, [(self.lonlat(road_xy), self.spec)], raw, cuts=False,
                                      landings=lands, part_ids=[pid])[0]
        ref = grading.plan_roads(self.frame, [(self.lonlat(road_xy), self.spec)], raw, cuts=False)[0]
        stale = plan("road#0")
        self.assertGreater(float(stale.z[-1]) - float(ref.z[-1]), 1.0, "filled")
        self.assertTrue(np.allclose(stale.z, plan("road#1").z))
        self.assertTrue(np.allclose(plan("other#0").z, ref.z), "(another feature: never)")

    def test_stale_unsplit_piece_takes_nothing(self):
        # (Codex review of round 5: the plan's one unsplit piece (from a short home copy without the bridge) is not the
        # clean landing's near-bank piece (PieceIds): its far-bank samples, lane-placed round in front of this bank's
        # deck end, go by where they lie (5.7 m off the approach) and take nothing)
        ap = self.approach(0.0)

        def raw(la, lo):
            e, n, _ = self.frame.geodetic_to_enu(la, lo, 0.0)
            x = np.asarray(e, np.float64)
            return np.where(x < 0.0, 53.0 - 0.037 * x, 50.0 + 0.037 * x)        # (this bank higher)
        lands = [(ap, "motorway", [ap[[2, 1]], ap[[1, 0]]], [], frozenset({"road#7/2"}), OUT)]
        s2 = np.sqrt(0.5)
        far_xy = self.dense(np.array([-0.66, 5.66]), np.array([-0.66 + 150.0 * s2, 5.66 + 150.0 * s2]))
        ref = grading.plan_roads(self.frame, [(self.lonlat(far_xy), self.spec)], raw, cuts=False)[0]
        got = grading.plan_roads(self.frame, [(self.lonlat(far_xy), self.spec)], raw, cuts=False, landings=lands,
                                 part_ids=["road#7/1"])[0]
        self.assertTrue(np.allclose(got.z, ref.z))

    def test_own_piece_behind_the_deck_end(self):
        # (Codex review of round 5: the landing's own piece, placed so a sample lies 2 m behind the deck end, within 3 m
        # of the approach: not raised (its distance, reach and bank tested together, never by the 3 m rule alone))
        ap = self.approach(0.0)
        line = (60.0, 0.0, 100.0)
        pts = np.array([[2.0, 0.5], [-10.0, 0.5]])
        z = grading.gap_apply(np.array([50.0, 50.0]), pts, ap, line, [ap], [], ["road#a", "road#a"],
                              frozenset({"road#a"}), OUT)
        self.assertEqual(float(z[0]), 50.0, "behind the deck end")
        self.assertEqual(float(z[1]), 60.0)

    def test_no_known_piece_by_where_it_lies(self):
        # (Codex review of round 5: a main part with no piece id (its pieces not from the ranges the ids are made of)
        # goes by where it lies, as an extension's sample with none does, never as another feature's)
        ap = self.approach(0.0)
        raw = self.raw_for(ap, ap + np.array([0.0, 50.0]))
        lands = [(ap, "motorway", [ap[[2, 1]], ap[[1, 0]]], [], frozenset({"road#1/2"}), OUT)]
        road_xy = self.dense(np.array([-200.0, 0.0]), np.array([-1.0, 0.0]))
        road = [(self.lonlat(road_xy), self.spec)]
        none = grading.plan_roads(self.frame, road, raw, cuts=False, landings=lands, part_ids=[None])[0]
        bare = grading.plan_roads(self.frame, road, raw, cuts=False, landings=lands)[0]
        ref = grading.plan_roads(self.frame, road, raw, cuts=False)[0]
        self.assertTrue(np.allclose(none.z, bare.z))
        self.assertGreater(float(none.z[-1]) - float(ref.z[-1]), 1.0, "filled")

    def test_stale_pieces_keep_their_feature(self):
        # (Codex review of round 5: kept pieces whose key is not known keep their feature: a foreign road's, lane-placed
        # onto the approach, takes nothing; the landing's own feature's goes by where it lies)
        ap = self.approach(0.0)
        raw = self.raw_for(ap, ap + np.array([0.0, 50.0]))
        lands = [(ap, "motorway", [ap[[2, 1]], ap[[1, 0]]], [], frozenset({"road#1/2"}), OUT)]
        road = [(self.lonlat(self.dense(np.array([-200.0, 0.0]), np.array([-1.0, 0.0]))), self.spec)]
        ref = grading.plan_roads(self.frame, road, raw, cuts=False)[0]
        foreign = grading.plan_roads(self.frame, road, raw, cuts=False, landings=lands, part_ids=["trunk#?"])[0]
        own = grading.plan_roads(self.frame, road, raw, cuts=False, landings=lands, part_ids=["road#?"])[0]
        self.assertTrue(np.allclose(foreign.z, ref.z))
        self.assertGreater(float(own.z[-1]) - float(ref.z[-1]), 1.0)

    def test_diagonal_connector_placed_copy(self):
        # (Codex review of round 5: the first ground piece starts 3 m on and 2 m aside of the deck end, a diagonal
        # connector; the road placed 8 m to the other side: its first samples are ahead of the deck end along the deck,
        # though behind the connector's own direction, and take the line)
        ap = np.array([[0.0, 0.0], [-3.0, 2.0], [-200.0, 2.0]])

        def raw(la, lo):
            e, n, _ = self.frame.geodetic_to_enu(la, lo, 0.0)
            x = -np.asarray(e, np.float64)
            return 50.0 + 0.037 * x - 4.0 * np.clip(1.0 - x / 14.0, 0.0, 1.0)
        lands = [(ap, "motorway", [ap[[2, 1]]], [], frozenset({"road"}), OUT)]
        road_xy = self.dense(np.array([-200.0, -6.0]), np.array([-3.0, -6.0]))
        r = grading.plan_roads(self.frame, [(self.lonlat(road_xy), self.spec)], raw, cuts=False, landings=lands,
                               part_ids=["road"])[0]
        self.assertGreater(float(r.z[-1]), 49.8, "filled (unfilled it stands ~47 m, down the opening)")

    def test_off_the_line_untouched(self):
        # (a road crossing the approach, not on its line, keeps its own ground)
        ap = self.approach(0.0)
        raw = self.raw_for(ap, ap + np.array([0.0, 300.0]))
        la, lo, _ = self.frame.enu_to_geodetic(np.full(21, -6.0), np.linspace(-20.0, 20.0, 21), 0.0)
        cross = grading.plan_roads(self.frame, [(np.c_[lo, la], self.spec)], raw, cuts=False,
                                   landings=[(ap, "motorway", [ap], [], frozenset(), OUT)])[0]
        ref = grading.plan_roads(self.frame, [(np.c_[lo, la], self.spec)], raw, cuts=False)[0]
        self.assertTrue(np.allclose(cross.z, ref.z))


class PieceIds(unittest.TestCase):
    """Codex review of round 5: a ground piece's id is the same in every clean map cell's copy of its feature, however
    far its deck reaches in each (roads' part keys), tells the two banks of a bridge apart, and never matches a piece
    made from ranges without that bridge (one unsplit piece over both banks)."""

    def test_ids(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import interchanges
        site = interchanges.load("a20_921", "interchange")
        f = site.cells[sorted(site.cells)[0]]
        found = 0
        for i in range(len(f)):
            rngs = roads.level_ranges(f, i)
            br = [(a, b, k) for a, b, k in rngs if k == "bridge" and 0.05 < a and b < 0.95]
            if not br:
                continue
            ps = roads._parts_by_level_new(f, i, "ground", rngs=rngs)
            clean = grading._piece_ids(f, i, ps, rngs)
            if len(ps) < 2 or None in clean:
                continue
            found += 1
            self.assertEqual(len(set(clean)), len(clean), "the banks told apart")
            a, b, k = br[0]                 # (another copy's deck reaching 2 % further each way)
            longer = [(max(0.0, a - 0.02), min(1.0, b + 0.02), k) if (a, b, k) == br[0] else r for r in rngs]
            ps1 = roads._parts_by_level_new(f, i, "ground", rngs=longer)
            self.assertEqual(grading._piece_ids(f, i, ps1, longer), clean)
            bare = [r for r in rngs if r[2] != "bridge"]
            ps0 = roads._parts_by_level_new(f, i, "ground", rngs=bare)
            self.assertFalse(set(grading._piece_ids(f, i, ps0, bare)) & set(clean), "the unsplit piece")
            self.assertEqual(grading._piece_ids(f, i, ps0, rngs), [f"{f.ids[i]}#?"] * len(ps0),
                             "kept pieces from other ranges: their feature only")
        self.assertGreater(found, 0)

    def test_kept_stale_pieces_are_short(self):
        # (Codex review of round 5: a layer's kept ground pieces from other ranges than its own now (a short home copy
        # since recovered a bridge): the plan's build is said short, every time (nothing on them kept); its own: not)
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import interchanges
        site = interchanges.load("a20_921", "interchange")
        f = site.cells[sorted(site.cells)[0]]
        i = next(i for i in range(len(f)) if any(k == "bridge" for _a, _b, k in roads.level_ranges(f, i))
                 and len(roads.ground_parts(f, i)) >= 2)
        with revisit.scope() as b:
            grading._piece_ids(f, i, roads.ground_parts(f, i))
        self.assertFalse(b.n)
        stale = roads._parts_by_level_new(f, i, "ground", rngs=[])     # (one unsplit piece)
        for _attempt in range(2):
            with revisit.scope() as b:
                ids = grading._piece_ids(f, i, stale)
            self.assertTrue(b.n)
            self.assertEqual(ids, [f"{f.ids[i]}#?"] * len(stale))


class ExtensionOwner(unittest.TestCase):
    """Codex review of round 5: an extension's road is the one the native selector chose. Two candidates share their
    first 2 m past the node, then one turns 30 degrees: the straight one goes on (over CHAIN_LOOK_M), and it, not the
    first listed, owns the extension's samples."""

    def test_selector_choice(self):
        import geodesy
        g = object.__new__(grading._Planner)
        g.lock = threading.Lock()
        g.frame = geodesy.EnuFrame(50.0, 10.0)
        spec = roads.Spec(11.5, "paved", False, "motorway")
        sg = grading.profile_sigma(spec)
        t = np.radians(30.0)
        turn = np.array([[0.0, 0.0], [2.0, 0.0], [2.0 + 60.0 * np.cos(t), 60.0 * np.sin(t)]])
        straight = np.array([[0.0, 0.0], [2.0, 0.0], [62.0, 0.0]])
        cands = [(turn, sg, grading.road_id("turn"), None, spec, "turn", "turn#0"),
                 (straight, sg, grading.road_id("straight"), None, spec, "straight", "straight#0")]
        g._chain_cands = lambda key, feats=None, specs=None: cands
        xs = np.linspace(-100.0, 0.0, 26)
        la, lo, _ = g.frame.enu_to_geodetic(xs, np.zeros(len(xs)), 0.0)
        ext, ids = g._extensions([(np.c_[lo, la], spec)], ["road"], with_ids=True)
        self.assertIsNotNone(ext[0][1])
        self.assertEqual(ids[0][1], "straight#0")
        self.assertGreater(float(ext[0][1][-1][0]), 30.0, "along the straight one")


class GapLandingCells(unittest.TestCase):
    """The A20 near Rostock (drivability a20_921) with BNE_FAST_GAP through the real cell planning: the same roads and
    decks whichever side the cells are planned from first (drivability.measure's approach), and the 5.35 g deck gone."""

    def setUp(self):
        self._store = grading._ACTIVE_STORE

    def tearDown(self):
        grading._ACTIVE_STORE = self._store     # (the Grader made here: no later test's tracks read from its solve)

    def test_build_order(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import drivability as dv
        with mock.patch.object(grading, "FAST_GAP", True):
            base = dv.measure("a20_921")
            for at in ((0.0, 600.0), (600.0, 0.0), (0.0, -600.0)):
                self.assertEqual(dv.measure("a20_921", approach=at), base, at)
        lift = max((p.get("lift_g", 0.0) for k, p in base["parts"].items() if k.startswith("bridge:")), default=0.0)
        self.assertLess(lift, 0.5)


class PinBatch(unittest.TestCase):
    """M5 round 7 (the map's own ranges): a cell's pin heights (_stronger_zs) planned in one batch
    are each the pin's own: the same bytes as planned alone (_stronger_z), and as planned again in another batch after
    the kept ones are evicted (_strong_memo cleared, as at 4096 entries or a road stage made again). Each map cell has
    its own copy of a deck (~1e-5 m from the others'; here each fixture cell's bridges moved by a few 1e-5 m): the
    batch's landings are the union round all its parts, and the least copy of each deck end was taken, so a part took
    another cell's copy than its own: Castlegar's 0a58d21e 5e-6 m apart, batch against alone. Now each end is at its
    0.1 m key whichever copy (_landings_in)."""

    SITE = "castlegar_939"

    def setUp(self):
        self._store = grading._ACTIVE_STORE

    def tearDown(self):
        grading._ACTIVE_STORE = self._store     # (the Grader made here: no later test's tracks read from its solve)

    def grader(self):
        from types import SimpleNamespace
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import drivability as dv
        import interchanges
        site = interchanges.load(self.SITE, "interchange", keep_m=dv.KEEP_M_SITE.get(self.SITE, dv.KEEP_M))
        md = SimpleNamespace(cell=lambda key, **kw: SimpleNamespace(layers={"roads": site.cells.get(str(key))}),
                             dem=SimpleNamespace(sample=site.sample))
        dv._hook_cells(site, md, self.SITE)
        store = grading.PlanStore(md, site.sample)
        store.frame_of = lambda region: site.frame
        return site, grading.Grader(site.frame, md, site.sample, store=store)

    def test_batch_alone_and_evicted(self):
        import zlib
        import roadsurf
        raw = grading._Planner._raw_lines

        def copies(self_, k):               # (each cell's own copy of its decks)
            out = raw(self_, k)
            h = zlib.crc32(str(k).encode()) % 97
            off = np.array([1e-5 * (h % 7 + 1), -1e-5 * (h % 5 + 1)])
            memo = self_.__dict__.setdefault("_test_copies", {})
            if str(k) not in memo:
                memo[str(k)] = {"bridges": [(np.asarray(b, np.float64) + off,) + tuple(r) for b, *r in out["bridges"]],
                                "ground": out["ground"]}
            return memo[str(k)]
        with mock.patch.object(grading, "FAST_GAP", True), \
                mock.patch.object(grading._Planner, "_raw_lines", copies), \
                mock.patch.object(revisit, "ON", False), mock.patch.object(grading, "PLAN_THREADS", 1), \
                mock.patch.object(roadsurf, "ON", False):
            site, g = self.grader()
            import mapcells
            key = mapcells.CellKey(14, 2837, 5605)
            pl = g.store.planner_of_cell(key)
            asks = [(c, last) for c in pl._chain_cands(key) for last in (False, True)]
            self.assertGreater(len(asks), 20)
            batch = pl._stronger_zs(asks, key)
            self.assertTrue(any(z is not None for z in batch))
            self.assertTrue(any(pl._landings_of([(pl._strong_part(c, last)[0], c[4])]) for c, last in asks),
                            "a fast road's landings in play")
            # (evicted: planned again in other batches, the second half first, then the first half)
            pl.__dict__["_strong_memo"].clear()
            half = len(asks) // 2
            again = pl._stronger_zs(asks[half:], key)
            again = pl._stronger_zs(asks[:half], key) + again
            # (alone: each on its own, from a fresh grader, nothing kept between)
            _s, g2 = self.grader()
            pl2 = g2.store.planner_of_cell(key)
            alone = []
            for c, last in [(c, last) for c in pl2._chain_cands(key) for last in (False, True)]:
                pl2.__dict__.pop("_strong_memo", None)
                alone.append(pl2._stronger_z(c, last, key))
        for n, (c, last) in enumerate(asks):
            self.assertEqual(repr(batch[n]), repr(alone[n]), (str(key), c[5], last, "batch against alone"))
            self.assertEqual(repr(again[n]), repr(batch[n]), (str(key), c[5], last, "evicted against fresh"))
        self.assertGreaterEqual(revisit.ROADSYS_SURFACE_VERSION, 9, "the surface plans keyed apart from round 6's")


class LevelSpacingTarget(unittest.TestCase):
    """R1f: each crossing asks its own road's clearance (roads.crossing_clear), the same in the deck solve, the approach
    fills and the dip under it; a deck over a track no longer climbs for a motorway under another of its parts."""

    def solve(self, classes, levels=None, lines=None):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_roads import features
        from test_roadmesh import FRAME
        lines = lines or [[(-400, 0), (400, 0)], [(-200, -60), (-200, 60)], [(200, -60), (200, 60)]]
        f = features(lines, classes, levels=levels or [(0, 1.0, 0.0, 1.0)], ids=[f"p{i}" for i in range(len(lines))])
        flat = lambda la, lo: np.full(np.shape(la), 10.0)  # noqa: E731
        with mock.patch.object(roads, "LEVEL_SPACING", True):
            bp = roads.BridgeProfiles(FRAME, lambda key: f, flat)
            got = {fid: (xy, z) for fid, _sp, xy, z, _lon in bp.parts([f], (-500, -500, 500, 500))}
            return got, bp

    def test_deck_solve_per_crossing(self):
        # (a long deck over a track at x = -200 and a motorway at x = +200, 400 m apart: each crossing at its own road's
        # clearance; the chain's one target stood the deck 0.9 m higher over the track)
        got, bp = self.solve(["primary", "track", "motorway"])
        xy, z = got["p0"]
        d = bp.depth_of("p0")
        with mock.patch.object(roads, "LEVEL_SPACING", True):
            want_t, want_m = float(roads.crossing_clear(d, "track")), float(roads.crossing_clear(d, "motorway"))
        over_t = float(np.interp(-200.0, xy[:, 0], z)) - 10.0
        over_m = float(np.interp(200.0, xy[:, 0], z)) - 10.0
        self.assertGreaterEqual(over_m, want_m - 0.05)
        self.assertGreaterEqual(over_t, want_t - 0.05)
        self.assertLess(over_t, want_m - 0.5, "the track's own target, not the motorway's")
        got2, _bp = self.solve(["primary", "motorway", "motorway"])
        self.assertAlmostEqual(float(np.interp(200.0, got2["p0"][0][:, 0], got2["p0"][1])) - 10.0, over_m, places=3,
                               msg="the motorway's crossing as with a motorway under the other end too")

    def test_stack_lower_part_its_own_class(self):
        # (a stack: the upper deck over the lower part asks the lower part's own road's clearance)
        gaps = {}
        for low in ("track", "motorway"):
            got, bp = self.solve([low, "primary"], levels=[(0, 1.0, 0.2, 0.8), (1, 2.0, 0.2, 0.8)],
                                 lines=[[(-300, 0), (300, 0)], [(0, -300), (0, 300)]])
            lo_xy, lo_z = got["p0"]
            hi_xy, hi_z = got["p1"]
            gaps[low] = (float(hi_z[int(np.argmin(np.abs(hi_xy[:, 1])))] - lo_z[int(np.argmin(np.abs(lo_xy[:, 0])))]),
                         bp.depth_of("p1"))
        for low, (gap, d) in gaps.items():
            with mock.patch.object(roads, "LEVEL_SPACING", True):
                self.assertGreaterEqual(gap, float(roads.crossing_clear(d, low)) - 0.6, (low, gap))
        self.assertLess(gaps["track"][0], gaps["motorway"][0] - 0.5, gaps)

    def test_approach_fill_per_crossing(self):
        # (a two-span chain, a track under the first span 20 m from the abutment, a motorway under the second 60 m off:
        # each crossing's own clearance, falling at DECK_GRADE from it, the higher of the two asked)
        span1, span2 = line(0.0, 40.0), line(40.0, 80.0)
        track = np.array([[20.0, -30.0], [20.0, 30.0]])
        mway = np.array([[60.0, -30.0], [60.0, 30.0]])
        z_at = lambda xy: np.zeros(len(np.asarray(xy).reshape(-1, 2)))  # noqa: E731
        with mock.patch.object(roads, "LEVEL_SPACING", True):
            depth = roads.deck_depth(roads.geom_span([span1, span2]))

            def want(cls, at):
                return float(roads.crossing_clear(depth, cls)) + grading.APPROACH_MARGIN_M \
                    - roads.DECK_GRADE * max(at - grading.APPROACH_FLAT_M, 0.0)

            for classes in (["track", "motorway"], ["track", "track"], ["motorway", "track"]):
                got = dict((tuple(e), lf) for e, lf in grading.bridge_lifts(span1, [span1, span2], [track, mway], z_at,
                                                                             chain=[span2], classes=classes))
                self.assertAlmostEqual(got[(0.0, 0.0)], max(want(classes[0], 20.0), want(classes[1], 60.0)), places=6,
                                       msg=classes)

    def test_dip_target_is_the_crossing_s(self):
        # (grading's dip and the deck solve ask one number at a crossing: crossing_clear of its road's class, whatever
        # else the chain crosses)
        with mock.patch.object(roads, "LEVEL_SPACING", True):
            for cls in ("motorway", "residential", "track", "footway", None):
                self.assertIsInstance(roads.crossing_clear(1.2, cls), roads.RoadClear)
                self.assertAlmostEqual(float(roads.crossing_clear(1.2, cls)),
                                       roads.deck_road_clear(1.2, None if cls is None else [cls]))
            self.assertAlmostEqual(float(roads.crossing_clear(1.2, None)), roads.LOWER_CLEAR_UNKNOWN_M + 1.3)

    def test_railway_under_the_next_span(self):
        # (Codex round 3: a railway under the chain's next part counted as a road, its 6.0 m given as a road's)
        span1, span2 = line(0.0, 40.0), line(40.0, 80.0)
        rail = np.array([[60.0, -30.0], [60.0, 30.0]])
        z_at = lambda xy: np.zeros(len(np.asarray(xy).reshape(-1, 2)))  # noqa: E731
        with mock.patch.object(roads, "LEVEL_SPACING", True):
            got = dict((tuple(e), lf) for e, lf in grading.bridge_lifts(span1, [span1, span2], [], z_at, chain=[span2],
                                                                         rail_lines=[rail], classes=[]))
            depth = roads.deck_depth(roads.geom_span([span1, span2]))
            want = roads.deck_road_clear(depth, ["rail"]) + grading.APPROACH_MARGIN_M \
                - roads.DECK_GRADE * (60.0 - grading.APPROACH_FLAT_M)
        self.assertAlmostEqual(got[(0.0, 0.0)], want, places=6)

    def test_signed_road_by_the_railway_under_the_next_span(self):
        # (Codex round 4: a road with a 4.2 m sign passing by the second span's railway: the railway keeps its 6.0 m)
        span1, span2 = line(0.0, 40.0), line(40.0, 80.0)
        rail = np.array([[60.0, -30.0], [60.0, 30.0]])
        sign = [(np.array([[61.0, -30.0], [61.0, 30.0]]), 4.2)]
        z_at = lambda xy: np.zeros(len(np.asarray(xy).reshape(-1, 2)))  # noqa: E731
        with mock.patch.object(roads, "LEVEL_SPACING", True):
            plain = dict((tuple(e), lf) for e, lf in grading.bridge_lifts(span1, [span1, span2], [], z_at,
                                                                           chain=[span2], rail_lines=[rail],
                                                                           classes=[]))
            signed = dict((tuple(e), lf) for e, lf in grading.bridge_lifts(span1, [span1, span2], [], z_at,
                                                                            chain=[span2], rail_lines=[rail],
                                                                            classes=[], limits=sign))
        self.assertAlmostEqual(signed[(0.0, 0.0)], plain[(0.0, 0.0)], places=9)

    def test_track_under_asks_less(self):
        deck = line(0.0, 80.0)
        road = np.array([[40.0, -30.0], [40.0, 30.0]])
        z_at = lambda xy: np.zeros(len(np.asarray(xy).reshape(-1, 2)))  # noqa: E731
        with mock.patch.object(roads, "LEVEL_SPACING", True):
            unknown = dict((tuple(e), lf) for e, lf in grading.bridge_lifts(deck, [deck], [road], z_at))
            track = dict((tuple(e), lf) for e, lf in grading.bridge_lifts(deck, [deck], [road], z_at,
                                                                         classes=["track"]))
            mw = dict((tuple(e), lf) for e, lf in grading.bridge_lifts(deck, [deck], [road], z_at,
                                                                      classes=["motorway"]))
        for k in unknown:
            self.assertAlmostEqual(unknown[k] - track[k], roads.LOWER_CLEAR_UNKNOWN_M - roads.LOWER_CLEAR_M["track"])
            self.assertAlmostEqual(mw[k], unknown[k])


if __name__ == "__main__":
    unittest.main()
