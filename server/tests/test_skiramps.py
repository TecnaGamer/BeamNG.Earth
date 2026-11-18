"""Motorway "ski ramps" (the clearance study 2026-11-17, PR #1175's comment, GitHub #1176): each cause's switch, off
by default, and what it changes.

- BNE_FAST_DECK_ENDS: a motorway's branched deck chain is solved along a line a car drives, never a U through its two
  carriageways (roads.deck_curve fast, _turns_back).
- BNE_FAST_DECK_GROUND: a ramp beside a motorway's road is no twin of the motorway's abutment, which is never raised:
  its fill keeps clear of the motorway (grading._Planner._end_lift_calc).
- BNE_FAST_GAP: a motorway's profile by a deck end reads its own line across the elevation's opening (grading.gap_fill).
- BNE_UNDER_SPEED: a fast road's dip under a deck ramps on its design speed's curve (grading.under_speed_grade).
- BNE_LEVEL_SPACING (R1f): one clearance target per crossing for the approach fills, the dips and the deck solve.
"""
import os
import sys
import threading
import time
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

    def test_turns_back(self):
        lines, _g, _c = self.chain()
        self.assertTrue(roads._turns_back(lines, [(0, False), (1, False)]))
        self.assertFalse(roads._turns_back(lines, [(0, False), (2, False)]))
        self.assertFalse(roads._turns_back(lines, [(2, True), (0, True)]))

    def test_right_angle_joints(self):
        # (a joint at 90 degrees is no turn back, nor one a hair under it; past it, either way round, it is; the sign
        # read with dnp.dot, the same on every CPU)
        a = line(0.0, 50.0)
        for ang, back in ((90.0, False), (89.9, False), (90.1, True), (135.0, True), (45.0, False)):
            t = np.radians(ang)                     # (the turn at the joint)
            b = np.c_[50.0 + np.linspace(0.0, 40.0, 11) * np.cos(t), np.linspace(0.0, 40.0, 11) * np.sin(t)]
            for path, lines in (([(0, False), (1, False)], [a, b]), ([(1, True), (0, True)], [a, b]),
                                ([(0, False), (1, True)], [a, b[::-1]])):
                self.assertEqual(roads._turns_back(lines, path), back, (ang, path))

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


class GapFill(unittest.TestCase):
    """The A20 near Rostock: the lidar's opening under a track bridge 10 m deep, reaching 8-20 m past the mapped deck
    end; a 3 % motorway on either side."""

    def profile(self, hole_m, depth=10.0):
        zb = 50.0 - 0.03 * np.arange(0.0, grading.FAST_GAP_FAR_M + 1e-6, grading.PROFILE_STEP)
        d = np.arange(0.0, grading.FAST_GAP_FAR_M + 1e-6, grading.PROFILE_STEP)
        hole = d < hole_m
        zb = np.where(hole, zb - depth * (1.0 - d / hole_m), zb)
        return d, zb

    def test_fills_the_opening_to_the_line(self):
        for hole_m in (8.0, 20.0):
            d, zb = self.profile(hole_m)
            out = grading.gap_fill(zb.copy(), d, zb)
            self.assertLess(float(np.max(np.abs(out - (50.0 - 0.03 * d)))), 0.05, hole_m)

    def test_road_without_opening_unchanged(self):
        d = np.arange(0.0, 60.0 + 1e-6, 2.0)
        zb = 50.0 - 0.03 * d
        self.assertIs(grading.gap_fill(zb, d, zb), zb)

    def test_capped(self):
        d, zb = self.profile(8.0, depth=40.0)
        out = grading.gap_fill(zb.copy(), d, zb)
        self.assertLessEqual(float(np.max(out - zb)), grading.FAST_GAP_MAX_M + 1e-9)


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


class RawLinesShort(unittest.TestCase):
    """Codex round 5: a cell layer whose level ranges were made short (or taken from a short shared entry) keeps them,
    and a read of it again says nothing; _raw_lines says it short every time and keeps nothing built on it, and keeps
    the cell once a layer read complete comes. With FAST_GAP's range bookkeeping (roads.RANGE_OWNERS), as it runs."""

    def setUp(self):
        p = mock.patch.object(roads, "RANGE_OWNERS", True)
        p.start()
        self.addCleanup(p.stop)

    def planner(self, layer):
        import geodesy
        g = object.__new__(grading._Planner)
        g.lock = threading.Lock()
        g.frame = geodesy.EnuFrame(54.037418, 12.606645)
        g.widths = None
        g.md = mock.Mock()
        g.md.cell = lambda k, **kw: mock.Mock(layers={"roads": layer})
        return g

    def layer(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import interchanges
        site = interchanges.load("a20_921", "interchange")
        return site, sorted(site.cells)[0]

    def test_short_layer_never_kept(self):
        import mapcells
        site, key = self.layer()
        f = site.cells[key]
        k = mapcells.CellKey(*(int(v) for v in key.split("/")))
        g = self.planner(f)
        with mock.patch.object(roads, "LIDAR_GAPS_ON", False):
            roads.ground_parts(f, 0)                    # (its ranges made on this layer)
            f.__dict__["_ranges_short_until"] = 1e18    # (as roads._cell_ranges marks a short one)
            for attempt in range(2):                    # (the faulted read, then a retry on the same layer)
                with revisit.scope() as b:
                    g._raw_lines(k)
                self.assertTrue(b.n, f"attempt {attempt}: said short")
                self.assertNotIn(str(k), g.__dict__.get("_raw_lines_memo", {}), "nothing kept")
            fresh = site.cells[key].__class__.__new__(site.cells[key].__class__)
            fresh.__dict__.update({kk: v for kk, v in f.__dict__.items() if kk != "_ranges_short_until"})
            g.md.cell = lambda k_, **kw: mock.Mock(layers={"roads": fresh})
            with revisit.scope() as b:                  # (the dependency back: a layer read complete)
                got = g._raw_lines(k)
            self.assertFalse(b.n)
            self.assertIn(str(k), g._raw_lines_memo)
            with revisit.scope() as b:
                self.assertIs(g._raw_lines(k), got)
            self.assertFalse(b.n, "kept clean, read clean")

    def test_short_home_copy(self):
        # (Codex review of round 5: the requested layer unmarked, the home copy its features take their ranges from
        # marked short: still short, never kept)
        import mapcells
        site, key = self.layer()
        f = site.cells[key]
        f.__dict__.pop("_ranges_short_until", None)
        k = mapcells.CellKey(*(int(v) for v in key.split("/")))
        g = self.planner(f)
        home = f.__class__.__new__(f.__class__)
        home.__dict__.update({kk: v for kk, v in f.__dict__.items() if not kk.startswith("_")})
        home.__dict__["_ranges_short_until"] = 1e18
        with mock.patch.object(roads, "LIDAR_GAPS_ON", False), \
                mock.patch.object(roads, "home_copy", lambda feats, i: (home, i) if feats is f else None):
            for attempt in range(2):
                with revisit.scope() as b:
                    g._raw_lines(k)
                self.assertTrue(b.n, f"attempt {attempt}: said short")
                self.assertNotIn(str(k), g.__dict__.get("_raw_lines_memo", {}))

    def test_home_copy_replaced(self):
        # (Codex review of round 5: the requesting layer kept, its short home copy evicted and reloaded clean between
        # reads: the second read's pieces come from the clean copy's ranges, not the short one's left in any memory)
        import mapcells
        site, key = self.layer()
        f = site.cells[key]
        f.__dict__.pop("_ranges_short_until", None)
        k = mapcells.CellKey(*(int(v) for v in key.split("/")))
        g = self.planner(f)

        def copy():
            h = f.__class__.__new__(f.__class__)
            h.__dict__.update({kk: v for kk, v in f.__dict__.items() if not kk.startswith("_")})
            return h
        short_home, clean_home = copy(), copy()
        short_home.__dict__["_level_ranges"] = {i: [(0.2, 0.8, "bridge")] for i in range(len(f))}
        short_home.__dict__["_ranges_state"] = "done"
        short_home.__dict__["_ranges_short_until"] = 1e18
        home = [short_home]
        with mock.patch.object(roads, "LIDAR_GAPS_ON", False), \
                mock.patch.object(roads, "home_copy", lambda feats, i: (home[0], i) if feats is f else None):
            with revisit.scope() as b:
                first = g._raw_lines(k)
            self.assertTrue(b.n)
            self.assertNotIn(str(k), g.__dict__.get("_raw_lines_memo", {}))
            home[0] = clean_home
            with revisit.scope() as b:
                second = g._raw_lines(k)
            self.assertFalse(b.n)
            self.assertIn(str(k), g._raw_lines_memo)
            want = sum(len(roads._parts_by_level_new(f, i, "bridge", rngs=roads.level_ranges(clean_home, i, _home=False)))
                       for i, sp in enumerate(roads.bridge_specs(f)) if sp is not None)
        self.assertEqual(len(second["bridges"]), want, "the clean copy's pieces")
        length = lambda r: round(sum(float(np.hypot(*np.diff(b[0], axis=0).T).sum()) for b in r["bridges"]), 3)  # noqa: E731
        self.assertNotEqual(length(first), length(second), "not the short copy's")

    def test_ranges_still_being_made(self):
        # (Codex review of round 5: another thread is still making the layer's ranges, its entries visible as they
        # come; then it finishes short, marked before done; then a complete layer. Neither the cell's lines nor a
        # landing built on them is kept until the complete one)
        import mapcells
        site, key = self.layer()
        f = site.cells[key]
        f.__dict__.pop("_ranges_short_until", None)
        k = mapcells.CellKey(*(int(v) for v in key.split("/")))
        g = self.planner(f)
        with mock.patch.object(roads, "LIDAR_GAPS_ON", False):
            roads.ground_parts(f, 0)                    # (the entries there, as a build half way leaves them)
            la, lo, _ = g.frame.enu_to_geodetic(np.array([5.0]), np.array([-25.0]), 0.0)
            for state, mark in (("busy", None), ("done", 1e18)):
                f.__dict__["_ranges_state"] = state
                if mark is not None:
                    f.__dict__["_ranges_short_until"] = mark
                with revisit.scope() as b:
                    g._raw_lines(k)
                    g._fast_landings(la, lo)
                self.assertTrue(b.n, state)
                self.assertNotIn(str(k), g.__dict__.get("_raw_lines_memo", {}), state)
                self.assertFalse(g.__dict__.get("_landing_memo"), state)
            f.__dict__.pop("_ranges_short_until", None)
            with revisit.scope() as b:
                g._raw_lines(k)
                g._fast_landings(la, lo)
            self.assertFalse(b.n)
            self.assertIn(str(k), g._raw_lines_memo)
            self.assertTrue(g._landing_memo, "the landings kept once complete")

    def test_no_row_written_into_a_busy_layer(self):
        # (Codex review of round 5: another thread loading the layer's clean ranges, the requested rows not in yet: no
        # row is made here into its memory (the load's setdefault would keep it), the read is short and unkept)
        import mapcells
        site, key = self.layer()
        f = site.cells[key].__class__.__new__(site.cells[key].__class__)
        f.__dict__.update({kk: v for kk, v in site.cells[key].__dict__.items() if not kk.startswith("_")})
        k = mapcells.CellKey(*(int(v) for v in key.split("/")))
        g = self.planner(f)
        f.__dict__["_ranges_state"] = "busy"
        f.__dict__["_level_ranges"] = {}
        with mock.patch.object(roads, "LIDAR_GAPS_ON", False):
            with revisit.scope() as b:
                g._raw_lines(k)
        self.assertTrue(b.n)
        self.assertEqual(f.__dict__["_level_ranges"], {}, "nothing written into the layer being loaded")
        self.assertNotIn(str(k), g.__dict__.get("_raw_lines_memo", {}))

    def test_bridge_restored_with_the_clean_copy(self):
        # (Codex review of round 5: the short home copy had no bridge, the layer's kept bridge_specs say none; the clean
        # copy restores it: the retry emits and keeps its deck, its eligibility from the ranges read, not those specs)
        import mapcells
        site, key = self.layer()
        f = site.cells[key]
        f.__dict__.pop("_ranges_short_until", None)
        k = mapcells.CellKey(*(int(v) for v in key.split("/")))
        g = self.planner(f)

        def copy():
            h = f.__class__.__new__(f.__class__)
            h.__dict__.update({kk: v for kk, v in f.__dict__.items() if not kk.startswith("_")})
            return h
        short_home, clean_home = copy(), copy()
        short_home.__dict__["_level_ranges"] = {i: [] for i in range(len(f))}
        short_home.__dict__["_ranges_state"] = "done"
        short_home.__dict__["_ranges_short_until"] = 1e18
        home = [short_home]
        with mock.patch.object(roads, "LIDAR_GAPS_ON", False), \
                mock.patch.object(roads, "home_copy", lambda feats, i: (home[0], i) if feats is f else None), \
                mock.patch.object(roads, "bridge_specs", lambda feats: [None] * len(feats)):
            with revisit.scope() as b:
                first = g._raw_lines(k)
            self.assertTrue(b.n)
            home[0] = clean_home
            with revisit.scope() as b:
                second = g._raw_lines(k)
            self.assertFalse(b.n)
        self.assertEqual(len(first["bridges"]), 0)
        self.assertGreater(len(second["bridges"]), 0, "the restored deck")
        self.assertIs(g._raw_lines_memo[str(k)], second)

    def test_competing_build_claims_first(self):
        # (Codex review of round 5: the layer unstarted when looked at, another thread claiming its build just before
        # ours (its _cell_ranges returns at once, the build theirs, busy): nothing read, no row written, short, unkept)
        import mapcells
        site, key = self.layer()
        f = site.cells[key].__class__.__new__(site.cells[key].__class__)
        f.__dict__.update({kk: v for kk, v in site.cells[key].__dict__.items() if not kk.startswith("_")})
        k = mapcells.CellKey(*(int(v) for v in key.split("/")))
        g = self.planner(f)

        def claimed(lay, load_only=False):
            lay.__dict__["_ranges_state"] = "busy"      # (the other thread's claim, its rows not in yet)
            lay.__dict__.setdefault("_level_ranges", {})
        with mock.patch.object(roads, "LIDAR_GAPS_ON", False), mock.patch.object(roads, "_cell_ranges", claimed):
            with revisit.scope() as b:
                g._raw_lines(k)
        self.assertTrue(b.n)
        self.assertEqual(f.__dict__["_level_ranges"], {})
        self.assertNotIn(str(k), g.__dict__.get("_raw_lines_memo", {}))

    def fresh(self):
        site, key = self.layer()
        f = site.cells[key].__class__.__new__(site.cells[key].__class__)
        f.__dict__.update({kk: v for kk, v in site.cells[key].__dict__.items() if not kk.startswith("_")})
        return f

    def test_busy_reader_fallback_stays_its_own(self):
        # (Codex review of round 5: an ordinary reader while another thread makes the layer's ranges makes its missing
        # row itself (its faults its own): kept out of the memo, so the build cannot publish it done; the owner's go in)
        f = self.fresh()
        f.__dict__["_ranges_state"] = "busy"
        f.__dict__["_ranges_owner"] = -1                    # (another thread's build)
        f.__dict__["_level_ranges"] = {}
        with mock.patch.object(roads, "LIDAR_GAPS_ON", False):
            got = roads.level_ranges(f, 0, _home=False)
            self.assertIsInstance(got, list)
            self.assertEqual(f.__dict__["_level_ranges"], {}, "a busy reader's row stays its own")
            self.assertEqual(roads._level_memo(f, [0, 1]), {0: got, 1: roads.level_ranges(f, 1, _home=False)})
            self.assertEqual(f.__dict__["_level_ranges"], {})
            f.__dict__["_ranges_owner"] = threading.get_ident()      # (this thread's build: its rows go in)
            roads._level_many_owned(f, [0])
            self.assertEqual(f.__dict__["_level_ranges"], {0: got})

    def test_load_only_then_full_build(self):
        # (Codex review of 48db74bc: a reader computing a row while a load-only call holds the layer (then loadable),
        # a full build claiming it meanwhile: the reader's row never goes in; the build makes and publishes its own)
        f = self.fresh()
        f.__dict__["_ranges_state"] = "loadable"
        f.__dict__["_ranges_owner"] = -1                    # (the load-only call's thread)
        f.__dict__["_level_ranges"] = {}
        real = roads._level_steps
        claimed = []

        def steps(feats, idx, *a):
            rows = real(feats, idx, *a)
            if feats is f and not claimed:          # (the full build claims the layer during the reader's making)
                claimed.append(True)
                with roads._FACTS_LOCK:
                    f.__dict__["_ranges_state"] = "busy"
                    f.__dict__["_ranges_owner"] = -2
            return [[(0.0, 0.1, "bridge")] + list(r) for r in rows]     # (the reader's row, told apart)
        with mock.patch.object(roads, "LIDAR_GAPS_ON", False), mock.patch.object(roads, "_level_steps", steps):
            mine = roads._level_many_owned(f, [0])
        self.assertTrue(claimed)
        self.assertEqual(f.__dict__["_level_ranges"], {}, "the reader's row never published")
        self.assertEqual(mine[0][0], (0.0, 0.1, "bridge"))

    def test_row_published_during_the_reading(self):
        # (Codex review of 48db74bc: the build publishes the row while a reader makes its own: the reader hands back
        # the published one, and keeps nothing)
        f = self.fresh()
        f.__dict__["_ranges_state"] = "busy"
        f.__dict__["_ranges_owner"] = -1
        f.__dict__["_level_ranges"] = {}
        row = [(0.25, 0.5, "bridge")]
        real = roads._level_steps

        def steps(feats, idx, *a):
            out = real(feats, idx, *a)
            f.__dict__["_level_ranges"][0] = row     # (published by the build meanwhile)
            return out
        with mock.patch.object(roads, "LIDAR_GAPS_ON", False), mock.patch.object(roads, "_level_steps", steps):
            got = roads._level_many_owned(f, [0, 1])
        self.assertIs(got[0], row)
        self.assertIn(1, got)
        self.assertEqual(f.__dict__["_level_ranges"], {0: row})

    def test_switch_off_is_the_old_path(self):
        # (Codex review of 48db74bc: with FAST_GAP off, the old code: a reader's row goes into the memo whoever builds,
        # no owner and no short mark is written)
        f = self.fresh()
        with mock.patch.object(roads, "RANGE_OWNERS", False), mock.patch.object(roads, "LIDAR_GAPS_ON", False):
            f.__dict__["_ranges_state"] = "busy"
            f.__dict__["_level_ranges"] = {}
            got = roads.level_ranges(f, 0, _home=False)
            self.assertEqual(f.__dict__["_level_ranges"], {0: got})
            g = self.fresh()
            roads._cell_ranges(g)
            self.assertNotIn("_ranges_owner", g.__dict__)
            self.assertNotIn("_ranges_short_until", g.__dict__)

    def test_fully_bridged_road_kept(self):
        # (Codex review of round 5: a road with no ground spec, bridged its whole length by its tags, still gives its
        # decks, its being one from its bridge spec; and no ground pieces)
        import mapcells
        site, key = self.layer()
        f = site.cells[key]
        f.__dict__.pop("_ranges_short_until", None)
        k = mapcells.CellKey(*(int(v) for v in key.split("/")))
        with mock.patch.object(roads, "LIDAR_GAPS_ON", False):
            bs = roads.bridge_specs(f)
            rs = roads.road_specs(f, None, None)
            decks = [i for i in range(len(f)) if bs[i] is not None and rs[i] is not None and roads.bridge_parts(f, i)]
            self.assertTrue(decks)
            gone = list(rs)
            for i in decks:
                gone[i] = None
            whole = self.planner(f)._raw_lines(k)
            with mock.patch.object(roads, "road_specs", lambda feats, *a, **kw: list(gone)):
                part = self.planner(f)._raw_lines(k)
        self.assertEqual(len(part["bridges"]), len(whole["bridges"]))
        self.assertLess(len(part["ground"]), len(whole["ground"]))

    def test_row_published_meanwhile(self):
        # (Codex review of round 5: a reader missing a row the build publishes before it looks again gets that row,
        # never a KeyError)
        f = self.fresh()
        f.__dict__["_ranges_state"] = "busy"
        f.__dict__["_ranges_owner"] = -1
        row = [(0.25, 0.5, "bridge")]
        f.__dict__["_level_ranges"] = {0: row}          # (published between the reader's miss and its look)
        self.assertEqual(roads._level_many_owned(f, [0]), {0: row})
        with mock.patch.object(roads, "LIDAR_GAPS_ON", False):
            got = roads._level_many_owned(f, [0, 1])
        self.assertIs(got[0], row)
        self.assertIn(1, got)

    def test_short_until_decoded_again(self):
        # (Codex review of 48db74bc: a short layer's ranges are never made again in place, its short inputs (grade
        # separation hooks, raw and rule ranges, lidar answers) kept on it: past its time it stays short and unkept;
        # the map data's next decode (a new layer) reads complete and is kept)
        import mapcells
        site, key = self.layer()
        k = mapcells.CellKey(*(int(v) for v in key.split("/")))
        f = self.fresh()
        g = self.planner(f)
        f.__dict__["_level_ranges"] = {i: [] for i in range(len(f))}      # (short: no decks)
        f.__dict__["_ranges_state"] = "done"
        f.__dict__["_ranges_short_until"] = time.monotonic() - 1.0     # (its time past)
        with mock.patch.object(roads, "LIDAR_GAPS_ON", False):
            with revisit.scope() as b:
                first = g._raw_lines(k)
            self.assertTrue(b.n)
            self.assertEqual(f.__dict__["_level_ranges"], {i: [] for i in range(len(f))}, "not made again in place")
            new = self.fresh()
            g.md.cell = lambda k_, **kw: mock.Mock(layers={"roads": new})
            with revisit.scope() as b:
                got = g._raw_lines(k)
        self.assertFalse(b.n)
        self.assertEqual(len(first["bridges"]), 0)
        self.assertGreater(len(got["bridges"]), 0)
        self.assertIs(g._raw_lines_memo[str(k)], got)

    def test_cell_ranges_mark_a_short_entry(self):
        import time
        site, key = self.layer()
        f = site.cells[key].__class__.__new__(site.cells[key].__class__)
        f.__dict__.update({kk: v for kk, v in site.cells[key].__dict__.items() if not kk.startswith("_")})
        f.__dict__["_bnge_cell"] = ("skiramps-test", "roads", "x")
        mk = roads._ranges_mem_key(f.__dict__)
        n = len(site.cells[key])
        with roads._FACTS_LOCK:
            roads._FACTS_MEM[mk] = {"raw": [[]] * n, "lvl": [[]] * n, "rule": False,
                                    "until": time.monotonic() + 100.0}
        try:
            with revisit.scope() as b:
                roads._cell_ranges(f)
            self.assertTrue(b.n)
            self.assertIsNotNone(f.__dict__.get("_ranges_short_until"))
        finally:
            with roads._FACTS_LOCK:
                roads._FACTS_MEM.pop(mk, None)


class PieceIds(unittest.TestCase):
    """Codex review of round 5: a ground piece's id is the same in every clean map cell's copy of its feature, however
    far its deck reaches in each (roads' part keys), tells the two banks of a bridge apart, and never matches a piece
    made from ranges without that bridge (one unsplit piece over both banks)."""

    def test_ids(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import interchanges
        site = interchanges.load("a20_921", "interchange")
        f = site.cells[sorted(site.cells)[0]]
        with mock.patch.object(roads, "LIDAR_GAPS_ON", False):
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
        with mock.patch.object(roads, "LIDAR_GAPS_ON", False):
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

    def test_build_order(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import drivability as dv
        with mock.patch.object(grading, "FAST_GAP", True), mock.patch.object(roads, "RANGE_OWNERS", True):
            base = dv.measure("a20_921")
            for at in ((0.0, 600.0), (600.0, 0.0), (0.0, -600.0)):
                self.assertEqual(dv.measure("a20_921", approach=at), base, at)
        lift = max((p.get("lift_g", 0.0) for k, p in base["parts"].items() if k.startswith("bridge:")), default=0.0)
        self.assertLess(lift, 0.5)


class UnderSpeed(unittest.TestCase):
    """A lone 1 m dip on 6 % cosine ramps bends at 2 g^2 / d: 0.95 g at 130 km/h. A motorway's: within its radius."""

    def test_grade(self):
        sg = grading.SIGMA_MAJOR_M
        g = grading.under_speed_grade("motorway", sg, 1.0)
        self.assertAlmostEqual(g, (1.0 / (2.0 * grading.UNDER_SPEED_R["motorway"])) ** 0.5)
        self.assertLessEqual(2.0 * g * g / 1.0, 1.0 / grading.UNDER_SPEED_R["motorway"] + 1e-12)
        self.assertEqual(grading.under_speed_grade("motorway", grading.SIGMA_M, 1.0), 0.0, "a link: as before")
        self.assertEqual(grading.under_speed_grade("residential", 14.0, 1.0), 0.0)
        self.assertEqual(grading.under_speed_grade("motorway", sg, 8.0), grading.UNDER_GRADE, "never past max_drop's")

    def test_offsets_longer_gentler(self):
        ln = [line(0.0, 800.0)]
        q = [(0, ln[0])]
        old = grading.underpass_offsets(ln, [(0, 400.0, 1.0)], q)[0]
        g = grading.under_speed_grade("motorway", grading.SIGMA_MAJOR_M, 1.0)
        new = grading.underpass_offsets(ln, [(0, 400.0, 1.0, g)], q)[0]
        self.assertAlmostEqual(float(old.min()), -1.0, places=6)
        self.assertAlmostEqual(float(new.min()), -1.0, places=6)
        self.assertGreater(int((new < -1e-6).sum()), int((old < -1e-6).sum()), "a longer dip")
        v = 36.0
        bend = lambda z: float(np.max(np.abs(np.diff(z, 2)))) / 16.0          # noqa: E731 (4 m samples)
        self.assertLess(v * v * bend(new) / 9.81, 0.15)
        self.assertGreater(v * v * bend(old) / 9.81, 0.5)
        same = grading.underpass_offsets(ln, [(0, 400.0, 1.0, 0.0)], q)[0]
        self.assertTrue(np.array_equal(same, old), "0: the kernel's own grade")

    def test_stops_before_a_junction(self):
        # (Codex round 2: a trunk's 1 m dip, its ramp 52 -> 208 m long, ran on through a slip road joining 75 m on and
        # its 20 m junction blend made a crest on the slip road. Now it ends before the junction: no leg bends more)
        trunk_a, trunk_b = line(0.0, 475.0), line(475.0, 900.0)
        slip = line(475.0, 700.0, 0.0, 120.0)
        lns = [trunk_a, trunk_b, slip]
        q = [(i, ln) for i, ln in enumerate(lns)]
        g = grading.under_speed_grade("trunk", grading.SIGMA_MAJOR_M, 1.0)
        old = grading.underpass_offsets(lns, [(0, 400.0, 1.0)], q)
        new = grading.underpass_offsets(lns, [(0, 400.0, 1.0, g)], q)
        self.assertAlmostEqual(float(new[0].min()), -1.0, places=6)
        for o, n in zip(old, new):
            ds = 4.0
            bend = lambda z: float(np.max(np.abs(np.diff(z, 2)))) / (ds * ds) if len(z) > 2 else 0.0  # noqa: E731
            self.assertLessEqual(bend(n), bend(o) + 1e-9)
        self.assertTrue(np.allclose(new[2], old[2]), "the slip road: nothing more than before")
        self.assertAlmostEqual(float(new[0][-1]), float(old[0][-1]), places=9, msg="the junction: as before")


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
