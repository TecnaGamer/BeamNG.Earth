"""Junction pads (grading.junction_pads, PAD_*): every road at a junction lies on one plane over its pad, weighted by
rank and by whether a branch faces it; the strongest road keeps its profile."""
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import roads  # noqa: E402

PRIMARY = roads.Spec(14.0, "paved", True, "primary", False)
RESIDENTIAL = roads.Spec(7.0, "paved", True, "residential", False)
SERVICE = roads.Spec(5.0, "paved", True, "service", False)
E, N, W, S = (1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0)


def line(x0, y0, x1, y1, step=4.0):
    n = int(round(np.hypot(x1 - x0, y1 - y0) / step)) + 1
    return np.c_[np.linspace(x0, x1, n), np.linspace(y0, y1, n)]


def plan(xy, z, spec, rid):
    p = grading.Plan(np.asarray(xy, float), np.asarray(z, float), spec.width / 2 + 1.0, road_half=spec.width / 2)
    p.rid = rid
    return p


def hill(xy):                   # 7 % up to the north, a gentle crest along x
    return 50.0 + 0.07 * xy[:, 1] - 0.0004 * xy[:, 0] ** 2


def graded(plans, pts):
    pts = np.asarray(pts, float)
    return grading.grade_points(pts[:, 0], pts[:, 1], hill(pts), plans)


class AuthorityTest(unittest.TestCase):
    def w(self, spec):
        return grading.pad_class_w(spec)

    def test_a_stem_gives_way_to_the_road_going_through(self):
        a = grading.pad_authority([self.w(PRIMARY)] * 2 + [self.w(RESIDENTIAL)], [E, W, N])
        np.testing.assert_allclose(a[:2], 1.0)
        self.assertLess(a[2], 0.05)

    def test_facing_counts_fully_only_for_its_own_class(self):
        both = grading.pad_authority([self.w(PRIMARY)] * 2 + [self.w(RESIDENTIAL)] * 2, [E, W, N, S])
        drive = grading.pad_authority([self.w(PRIMARY)] * 2 + [self.w(RESIDENTIAL), self.w(SERVICE)], [E, W, N, S])
        self.assertGreater(both[2], drive[2], "a street facing a driveway is less of a through road")

    def test_equal_crossroads_all_one(self):
        np.testing.assert_allclose(grading.pad_authority([self.w(RESIDENTIAL)] * 4, [E, W, N, S]), 1.0)

    def test_the_plane_follows_the_strongest(self):
        a = grading.pad_authority([self.w(PRIMARY)] * 2 + [self.w(RESIDENTIAL)] * 2, [E, W, N, S])
        z0, g = grading.pad_plane([10.0] * 4, [0.0, 0.0, 0.07, 0.07], [E, W, N, S], a ** grading.PAD_FIT_POW)
        self.assertLess(abs(g[1]), 0.01, "next to no tilt across the primary")
        z0, g = grading.pad_plane([10.0] * 4, [0.0, 0.0, 0.07, -0.07], [E, W, N, S], np.ones(4))
        self.assertGreater(g[1], 0.02, "equal roads: the plane leans with the climbing one")


class PadTest(unittest.TestCase):
    def crossing(self, one_part):
        """A primary along the contour, a residential climbing across it (one part, or split at the node)."""
        w_, e_ = line(-200, 0, 0, 0, 2.0), line(0, 0, 200, 0, 2.0)
        ps = [plan(w_, hill(w_), PRIMARY, 1), plan(e_, hill(e_), PRIMARY, 2)]
        sp = [PRIMARY, PRIMARY]
        if one_part:
            ns = line(0, -120, 0, 120, 2.0)
            ps.append(plan(ns, hill(ns) + 0.4, RESIDENTIAL, 3))     # (its own smoothing: 0.4 m off at the node)
            sp.append(RESIDENTIAL)
        else:
            for k, (y0, y1) in enumerate(((0, 120), (0, -120))):
                ln = line(0, y0, 0, y1, 2.0)
                ps.append(plan(ln, hill(ln) + 0.4, RESIDENTIAL, 3 + k))
                sp.append(RESIDENTIAL)
        return ps, sp

    def lanes(self, plans, off):
        xs = np.arange(-40.0, 40.0, 1.0)
        return graded(plans, np.c_[xs, np.full(len(xs), off)])

    def test_a_road_crossing_in_one_part_no_longer_tilts_the_main_road(self):
        for one in (True, False):
            ps, sp = self.crossing(one)
            before = [p.z.copy() for p in ps[:2]]
            grading.snap_junctions(ps, sp)
            grading.junction_pads(ps, sp)
            for p, z in zip(ps[:2], before):
                self.assertLess(np.abs(p.z - z).max(), 0.02, "the primary keeps its profile")
            north, south = self.lanes(ps, 6.0), self.lanes(ps, -6.0)
            xs = np.arange(-40.0, 40.0, 1.0)
            want = 50.0 - 0.0004 * xs ** 2
            self.assertLess(np.abs(north - want).max(), 0.08, f"one part={one}: no hump in the uphill lane")
            self.assertLess(np.abs(south - want).max(), 0.08, f"one part={one}: no dip in the downhill lane")

    def test_off_as_before(self):
        ps, sp = self.crossing(True)
        z = [p.z.copy() for p in ps]
        with mock.patch.object(grading, "PAD_ON", False):
            grading.junction_pads(ps, sp)
        for p, zz in zip(ps, z):
            np.testing.assert_array_equal(p.z, zz)

    def test_a_road_going_on_into_its_next_part_is_no_pad(self):
        a, b = line(-100, 0, 0, 0), line(0, 0, 100, 10)
        ps = [plan(a, hill(a), RESIDENTIAL, 1), plan(b, hill(b), RESIDENTIAL, 2)]
        z = [p.z.copy() for p in ps]
        grading.junction_pads(ps, [RESIDENTIAL] * 2)
        for p, zz in zip(ps, z):
            np.testing.assert_array_equal(p.z, zz)


class SteepTrackTest(unittest.TestCase):
    """A track leaving a residential street straight up a 30 % bank (-32.0951, 116.03866, the user 2026-10-29: a 2.4 m
    trench for 30 m): it lands on the street, then climbs from its edge on a short ramp, not a long sunk ease."""

    def test_no_trench_up_a_bank(self):
        TRACK = roads.Spec(3.0, "dirt", False, "track", False)

        def bank(xy):
            return 250.0 + 0.30 * np.maximum(xy[:, 1] - 4.0, 0.0)       # the street's verge, then the bank
        w_, e_ = line(-150, 0, 0, 0, 2.0), line(0, 0, 150, 0, 2.0)
        up = line(0, 0, 0, 80, 2.0)
        ps = [plan(w_, bank(w_), RESIDENTIAL, 1), plan(e_, bank(e_), RESIDENTIAL, 2), plan(up, bank(up), TRACK, 3)]
        sp = [RESIDENTIAL, RESIDENTIAL, TRACK]
        grading.snap_junctions(ps, sp)
        grading.junction_pads(ps, sp)
        t = ps[2]
        sink = bank(t.xy) - t.z
        self.assertLess(sink[t.xy[:, 1] > 8.0].max(), 1.0, "no deep trench up the bank")
        self.assertLess(sink[t.xy[:, 1] > 25.0].max(), 0.05, "back on its own profile within ~25 m")
        self.assertLess(abs(t.z[0] - ps[0].z[-1]), 0.01, "it still meets the street")


if __name__ == "__main__":
    unittest.main()
