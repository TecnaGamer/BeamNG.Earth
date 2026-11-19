"""Road revamp v2 P3a (BNE_ROADSHAPE=profile, shadow only): earthfast.profile_members (the stretches from ground
anchor to ground anchor) and earthfast.profile_solve (one joint profile through the abutments), and rideprobe's pure
ride metric."""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools", "probes"))
import fast  # noqa: E402
import rideprobe  # noqa: E402
import roadshape  # noqa: E402

EF = fast._ef if getattr(fast._ef, "VERSION", 0) >= 516 else None
MPRM = [1.0, 10.0, math.cos(math.radians(40.0)), 2.0, 20000.0]


def sprm():
    r = roadshape
    return [r.PROF_LAMBDA, r.PROF_W_G, r.PROF_D0_M, r.PROF_D1_M, r.PROF_EPS_W, r.PROF_MU, r.PROF_SPLIT_GAP_M,
            r.PROF_SHEET_DZ_M, math.cos(math.radians(r.PROF_SHEET_DEG)), r.PROF_SHEET_REACH_M, r.PROF_AT_GRADE_M,
            r.PROF_DECK_GROUND_M, r.PROF_MAX_GRADE, r.PROF_GRADE_ADD, r.PROF_MARGIN, float(r.PROF_ROUNDS), 0.001, 1e-6] + (
                [1.0, 1.0, 16.0, 1.0] if getattr(EF, "VERSION", 0) >= 536 else [])


def flat(lines):
    off = [0]
    for ln in lines:
        off.append(off[-1] + len(ln))
    return np.ascontiguousarray(np.concatenate([np.asarray(ln, np.float64) for ln in lines]), np.float64), off


def knots(x0, x1, step=2.0):
    n = max(1, int(math.ceil(abs(x1 - x0) / step)))
    x = x0 + (x1 - x0) * np.arange(n + 1) / n
    x[-1] = x1
    return np.stack([x, np.zeros(n + 1)], axis=1)


@unittest.skipIf(EF is None, "earthfast 516 needed")
class Members(unittest.TestCase):
    def test_walk_to_anchor_straight_on(self):
        decks = [[[0.0, 0.0], [100.0, 0.0]]]
        ground = [[[100.0, 0.0], [150.0, 0.0]], [[150.0, 0.0], [400.0, 0.0]], [[150.0, 0.0], [150.0, 100.0]],
                  [[-300.0, 0.0], [0.0, 0.0]]]
        dxy, doff = flat(decks)
        gxy, goff = flat(ground)
        axy, aoff, meta, joins, ends = EF.profile_members(dxy, doff, [5.0], [200.0], gxy, goff, MPRM)
        axy = np.asarray(axy).reshape(-1, 2)
        meta = np.asarray(meta).reshape(-1, 4)
        self.assertEqual(len(meta), 2)
        self.assertEqual(list(meta[:, 3]), [0, 0])      # (both reach their anchor)
        far = axy[aoff[2] - 1]
        self.assertAlmostEqual(far[0], 300.0, places=6)     # (straight on at the junction, not the side road)
        self.assertEqual(joins, [])

    def test_window_independent(self):
        # extra far lines and another order of the near ones: the same members
        decks = [[[0.0, 0.0], [100.0, 0.0]]]
        near = [[[100.0, 0.0], [400.0, 0.0]], [[-300.0, 0.0], [0.0, 0.0]]]
        far = [[[5000.0, 0.0], [5100.0, 0.0]]]
        dxy, doff = flat(decks)
        a = EF.profile_members(dxy, doff, [5.0], [200.0], *flat(near), MPRM)
        b = EF.profile_members(dxy, doff, [5.0], [200.0], *flat(near[::-1] + far), MPRM)
        self.assertEqual(a[0], b[0])
        self.assertEqual(a[2], b[2])

    def test_gap_between_decks_once(self):
        decks = [[[0.0, 0.0], [100.0, 0.0]], [[150.0, 0.0], [250.0, 0.0]]]
        ground = [[[100.0, 0.0], [150.0, 0.0]]]
        dxy, doff = flat(decks)
        _axy, _aoff, meta, _joins, _ends = EF.profile_members(dxy, doff, [5.0, 5.0], [200.0, 200.0], *flat(ground), MPRM)
        meta = np.asarray(meta).reshape(-1, 4)
        self.assertEqual(len(meta), 1)                  # (found from both sides, kept once)
        self.assertEqual(int(meta[0][3]), 2)
        self.assertEqual(int(meta[0][2]), 1 * 2 + 0)


@unittest.skipIf(EF is None, "earthfast 516 needed")
class Solve(unittest.TestCase):
    def setup_ramp(self, deck_z):
        a0, dk, a1 = knots(0.0, -200.0), knots(0.0, 100.0), knots(100.0, 300.0)
        xy, off = flat([dk, a0, a1])
        z = np.concatenate([deck_z(dk[:, 0]), np.zeros(len(a0)), np.full(len(a1), float(deck_z(np.array([100.0]))[0]))])
        g = np.full(len(z), np.nan)
        joins = [1, 0, 0, 0, 1, 2, 0, 0, 1, 1]
        pins = [1, 1, 2, 2, 1, 2]
        return xy, off, z, g, joins, pins, len(dk)

    def test_kink_smoothed_anchors_kept(self):
        xy, off, z, g, joins, pins, nd = self.setup_ramp(lambda x: 0.04 * x)
        zs, rep, _t = EF.profile_solve(xy, off, z, g, [1, 0, 0], [5.0, 0.0, 0.0], [1.0, 0.0, 0.0], [5.0, 0.0, 0.0],
                                   [3, 1, 1], joins, pins, [], sprm())
        self.assertEqual(int(rep[0]), 0)
        zs = np.asarray(zs)
        self.assertAlmostEqual(zs[off[2] - 1], 0.0, delta=0.002)       # (the anchor kept)
        today = rideprobe.ride_g(np.r_[z[off[1]:off[2]][::-1], z[1:nd]], 2.0)
        solved = rideprobe.ride_g(np.r_[zs[off[1]:off[2]][::-1], zs[1:nd]], 2.0)
        self.assertLess(np.abs(solved).max(), 0.5 * np.abs(today).max())
        again, _, _t = EF.profile_solve(xy, off, z, g, [1, 0, 0], [5.0, 0.0, 0.0], [1.0, 0.0, 0.0], [5.0, 0.0, 0.0],
                                    [3, 1, 1], joins, pins, [], sprm())
        self.assertEqual(list(zs), list(again))

    def test_floor_held(self):
        xy, off, z, g, joins, pins, nd = self.setup_ramp(lambda x: 6.0 * (1.0 - ((x - 50.0) / 50.0) ** 2))
        zs, rep, _t = EF.profile_solve(xy, off, z, g, [1, 0, 0], [5.0, 0.0, 0.0], [1.0, 0.0, 0.0], [5.0, 0.0, 0.0],
                                   [3, 1, 1], joins, pins, [0.0, 50.0, 0.0, 5.5], sprm())
        self.assertEqual(int(rep[0]), 0)
        self.assertGreaterEqual(zs[25], 5.5 - 0.02)

    def test_sheet_union_held(self):
        # a ramp leaving a through deck: today 2 m above it inside the union (a hump); held to it up to the split
        th = knots(0.0, 200.0)
        rx = np.arange(0.0, 101.0, 2.0)
        ramp = np.stack([60.0 + rx, 0.08 * rx], axis=1)       # (its edges part after ~ (5 + 4 + 1.2) / 0.08 m)
        xy, off = flat([th, ramp])
        z = np.r_[np.full(len(th), 10.0), 10.0 + 2.0 * np.sin(np.pi * np.clip(rx / 60.0, 0, 1))]
        g = np.full(len(z), np.nan)
        zs, rep, _t = EF.profile_solve(xy, off, z, g, [1, 1], [5.0, 4.0], [1.0, 1.0], [5.0, 5.0], [3, 3], [], [], [],
                                   sprm())
        self.assertEqual(int(rep[0]), 0)
        self.assertGreater(rep[4], 10)                  # (sheet pairs)
        zs = np.asarray(zs)
        self.assertLess(abs(zs[off[1] + 10] - zs[40]), 0.03)    # (20 m along the ramp: level with the through deck)

    @unittest.skipUnless(EF is not None and EF.VERSION >= 536, "earthfast 536")
    def test_twin_parting_uphill_held_while_beside(self):
        # P3e: a ramp beside a through deck (gap 1 m) parting slowly, climbing away today (3 m over it 60 m on, still
        # within 2.5 m): held level with it while beside (a run seeded within TWIN_DZ_M), free once parted
        th = knots(0.0, 200.0)
        rx = np.arange(0.0, 161.0, 2.0)
        ramp = np.stack([rx, 10.0 + 0.02 * rx], axis=1)        # (gap 1 + 0.02 x: 2.5 m at x = 75, 3.5 m at 125)
        xy, off = flat([th, ramp])
        z = np.r_[np.full(len(th), 10.0), 10.0 + 0.05 * rx]
        g = np.full(len(z), np.nan)
        zs, rep, _t = EF.profile_solve(xy, off, z, g, [1, 1], [5.0, 4.0], [1.0, 1.0], [5.0, 5.0], [3, 3], [], [], [],
                                       sprm())
        self.assertEqual(int(rep[0]), 0)
        self.assertGreater(rep[13], 20)                 # (twin pairs)
        zs = np.asarray(zs)
        for x in (20.0, 40.0, 70.0):                    # (gap <= 2.5 m: one height)
            k = off[1] + int(x / 2.0)
            self.assertLess(abs(zs[k] - zs[int(x / 2.0)]), 0.02, x)
        self.assertGreater(zs[off[2] - 1] - zs[80], 0.5)    # (parted, it climbs)

    def test_bad_shapes(self):
        with self.assertRaises(ValueError):
            EF.profile_solve(np.zeros((2, 2)), [0, 2], np.zeros(2), np.zeros(2), [1], [1.0], [1.0], [1.0], [0], [], [],
                             [], [1.0])


class Ride(unittest.TestCase):
    def test_ride_g(self):
        # a 4 % kink: a 10 m half chord at 70 mph sees v^2 * 0.04 / 10 / g
        x = np.arange(0.0, 101.0, 1.0)
        z = np.where(x > 50, 0.04 * (x - 50), 0.0)
        a = rideprobe.ride_g(z, 1.0)
        v = rideprobe.RIDE_V
        self.assertAlmostEqual(float(np.max(np.abs(a))), v * v * 0.04 / 10.0 / 9.81, places=6)
        self.assertEqual(float(a[0]), 0.0)


class RideRecord(unittest.TestCase):
    def rec(self, dz):
        ax = [[-float(x), 0.0] for x in range(0, 101, 2)]
        dx = [[float(x), 0.0] for x in range(0, 101, 2)]
        mem = lambda kind, xy, z, **k: dict({"kind": kind, "xy": xy, "z_today": z, "z_solved": z, "ll": [[0, 0], [0, 1]],
                                             "cls": "motorway", "link": False, "deck": None, "end": None, "reach": None},
                                            **k)      # noqa: E731
        return {"status": "solved", "joins": [[1, 0, 0, 0, 1]],
                "members": [mem("deck", dx, [dz] * len(dx)), mem("approach", ax, [0.0] * len(ax), deck=0, end=0, reach=0)]}

    def test_flat_no_step(self):
        self.assertEqual(rideprobe.abutment_rides(self.rec(0.0))[0][3], 0.0)

    def test_step_kept(self):
        # (a 0.2 m step at the abutment: the 20 m chord sees v^2 0.2 / 10^2 / g either side of it)
        r = rideprobe.abutment_rides(self.rec(0.2))[0]
        v = rideprobe.RIDE_V
        self.assertAlmostEqual(r[3], round(v * v * 0.2 / 100.0 / 9.81, 3), places=2)


if __name__ == "__main__":
    unittest.main()


def _ribbon(x0, y0, ang, n, hw, z=0.0, step=1.0):
    d = np.array([math.cos(ang), math.sin(ang)])
    nrm = np.array([-d[1], d[0]])
    mid = np.array([[x0, y0]]) + step * np.arange(n)[:, None] * d[None]
    return mid + hw * nrm, mid - hw * nrm, np.full(n, z)


def _cop(ribs, bank=None, ends=None, tol=0.03):
    L = np.ascontiguousarray(np.concatenate([r[0] for r in ribs]))
    R = np.ascontiguousarray(np.concatenate([r[1] for r in ribs]))
    Z = np.ascontiguousarray(np.concatenate([r[2] for r in ribs]))
    B = np.zeros(len(Z)) if bank is None else np.ascontiguousarray(np.concatenate(bank))
    off = np.r_[0, np.cumsum([len(r[2]) for r in ribs])].astype(np.int64)
    n = len(ribs)
    e = np.ones(2 * n, bool) if ends is None else np.asarray(ends, bool)
    tl, tr = EF.deck_trim_coplanar(L, R, Z, B, off, np.arange(n, dtype=np.int64), np.full(n, -1, np.int64),
                                   np.zeros(n, bool), e, np.arange(n, dtype=np.int64),
                                   np.array([tol, math.cos(math.radians(30.0)), 0.0, 0.3]))
    out = []
    for k in range(n):
        a, b = off[k], off[k + 1]
        u = (L[a:b] - R[a:b]) / np.hypot(*(L[a:b] - R[a:b]).T)[:, None]
        out.append((L[a:b] - u * tl[a:b, None], R[a:b] + u * tr[a:b, None], tl[a:b], tr[a:b]))
    return out


def _dist_line(p, a, ang):
    return abs((p[:, 0] - a[0]) * math.sin(ang) - (p[:, 1] - a[1]) * math.cos(ang))


@unittest.skipUnless(EF is not None and getattr(EF, "VERSION", 0) >= 536, "earthfast 536")
class CoplanarTrim(unittest.TestCase):
    """P3e: roadmesh.trim_overlaps' coplanar cut (earthfast deck_trim_coplanar): one seam, surfaces met, part ends."""

    def test_angled_decks_one_seam(self):
        # two flat 10 m decks 20 degrees apart, overlapping: each cut edge on the line equally far from both centre
        # lines (each at half the distance along its own normal: ~0.23 m apart, Codex P3d)
        ang = math.radians(20.0)
        a = _ribbon(-30.0, 0.0, 0.0, 61, 5.0)
        b0 = np.array([-30.0, 8.0 - 30.0 * math.tan(ang)])
        b = _ribbon(b0[0], b0[1], ang, 61, 5.0)
        (la, _ra, tla, _), (_lb, rb, _, trb) = _cop([a, b])
        cut_a = la[tla > 0]
        cut_b = rb[trb > 0]
        self.assertGreater(len(cut_a), 3)
        self.assertGreater(len(cut_b), 3)
        for pts in (cut_a, cut_b):      # (on the bisector: as far from one centre line as from the other)
            np.testing.assert_allclose(_dist_line(pts, (-30.0, 0.0), 0.0), _dist_line(pts, b0, ang), atol=1e-6)

    def test_step_or_bank_not_cut(self):
        a = _ribbon(0.0, 0.0, 0.0, 41, 5.0)
        b = _ribbon(0.0, 8.0, 0.0, 41, 5.0)
        self.assertGreater(_cop([a, b])[0][2].max(), 0.9)               # (one surface: cut halfway)
        b_up = (b[0], b[1], np.full(41, 0.25))
        got = _cop([a, b_up])
        # (0.25 m apart: no step made; the upper whole, the lower cut back under the upper's near edge only)
        self.assertEqual(got[1][3].max(), 0.0)
        np.testing.assert_allclose(got[0][2][1:-1], 2.0, atol=1e-9)
        bank = [np.zeros(41), np.full(41, 0.02)]                        # (b banked: its edge 8 cm under at the seam)
        got = _cop([a, b], bank=bank)
        self.assertEqual(got[0][2].max(), 0.0)
        np.testing.assert_allclose(got[1][3][1:-1], 2.0, atol=1e-9)

    def test_part_end_not_chunk_end(self):
        # b's ribbon ends at x = 20; past a real end nothing is cut, past a chunk end (its part goes on) the cut goes on
        a = _ribbon(0.0, 0.0, 0.0, 41, 5.0)
        b = _ribbon(0.0, 8.0, 0.0, 21, 5.0)
        real = _cop([a, b], ends=[True, True, True, True])[0][2]
        chunk = _cop([a, b], ends=[True, True, True, False])[0][2]
        self.assertEqual(real[25], 0.0)
        self.assertGreater(chunk[25], 0.0)
        self.assertGreater(real[20], 0.9)       # (a row exactly at the end's station: beside it, cut)

    def test_angled_decks_on_one_grade(self):
        # two decks 25 degrees apart lying on one 5 % plane: one surface at the seam, both cut (the other's height read
        # at the seam point's own foot, not the row centre's: a false 6 cm step, Codex P3e)
        ang = math.radians(25.0)
        a = _ribbon(-30.0, 0.0, 0.0, 61, 5.0)
        b0 = np.array([-30.0, 8.0 - 30.0 * math.tan(ang)])
        b = _ribbon(b0[0], b0[1], ang, 61, 5.0)
        a = (a[0], a[1], 0.05 * ((a[0] + a[1]) / 2)[:, 0])
        b = (b[0], b[1], 0.05 * ((b[0] + b[1]) / 2)[:, 0])
        (_la, _ra, tla, _), (_lb, _rb, _, trb) = _cop([a, b])
        self.assertGreater(int((tla > 0).sum()), 3)
        self.assertGreater(int((trb > 0).sum()), 3)
