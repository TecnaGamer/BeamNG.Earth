"""Motorway "ski ramps" (the clearance study 2026-11-17, PR #1175's comment, GitHub #1176): each cause's switch, off
by default, and what it changes.

- BNE_FAST_DECK_ENDS: a motorway's branched deck chain is solved along a line a car drives, never a U through its two
  carriageways (roads.deck_curve fast, _turns_back).
- BNE_FAST_DECK_GROUND: a ramp beside a motorway's road is no twin of the motorway's abutment, which is never raised:
  its fill keeps clear of the motorway (grading._Planner._end_lift_calc).
"""
import os
import sys
import threading
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
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

    def test_one_line_unchanged(self):
        ln = line(0.0, 320.0)
        cr = [(0, 160.0, 10.0)]
        a = roads.deck_curve([ln], lambda xy: np.full(len(xy), 10.0), cr)
        b = roads.deck_curve([ln], lambda xy: np.full(len(xy), 10.0), cr, fast=True)
        self.assertTrue(np.array_equal(a[0], b[0]))


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


if __name__ == "__main__":
    unittest.main()
