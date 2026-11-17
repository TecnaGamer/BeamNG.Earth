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
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import revisit  # noqa: E402
import roads  # noqa: E402


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
        new, old = self.keys({"BNE_FAST_GAP": "on"})
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


class LevelSpacingTarget(unittest.TestCase):
    """R1f: an approach fill asks the clearance of the roads under the chain, as the deck solve does."""

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
