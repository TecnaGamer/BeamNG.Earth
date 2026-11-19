"""Road revamp v2 P3b (BNE_ROADSHAPE=profile, built): earthfast.profile_solve and earthfast.profile_pin_plans give the
same BYTES on every platform (the design's section 1.3 "Numbers": a Linux / Windows / ARM byte test before the switch
joins the toggle; Windows CI runs this file). Fixed inputs (a ramped deck with kinked abutments, a sheet union, floors
and a grade-limited stretch; a pinned approach under plans along, across and beside it) to fixed output digests:
sha256 of the float64 outputs' bytes. A digest change is a real change of the solved heights: check it is meant, then
update it here (BNGE_UPDATE_GOLDEN=1 prints the new digests)."""
import hashlib
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fast  # noqa: E402
import roadshape  # noqa: E402

EF = fast._ef if getattr(fast._ef, "VERSION", 0) >= 523 else None


def sprm():
    r = roadshape
    return [r.PROF_LAMBDA, r.PROF_W_G, r.PROF_D0_M, r.PROF_D1_M, r.PROF_EPS_W, r.PROF_MU, r.PROF_SPLIT_GAP_M,
            r.PROF_SHEET_DZ_M, math.cos(math.radians(r.PROF_SHEET_DEG)), r.PROF_SHEET_REACH_M, r.PROF_AT_GRADE_M,
            r.PROF_DECK_GROUND_M, r.PROF_MAX_GRADE, r.PROF_GRADE_ADD, r.PROF_MARGIN, float(r.PROF_ROUNDS),
            r.PROF_ACTIVE_TOL, r.PROF_RELEASE_TOL]


def flat(lines):
    off = [0]
    for ln in lines:
        off.append(off[-1] + len(ln))
    return np.ascontiguousarray(np.concatenate([np.asarray(ln, np.float64) for ln in lines]), np.float64), off


def knots(x0, x1, y=0.0, step=2.0):
    n = max(1, int(math.ceil(abs(x1 - x0) / step)))
    x = x0 + (x1 - x0) * np.arange(n + 1) / n
    x[-1] = x1
    return np.stack([x, np.full(n + 1, y)], axis=1)


def digest(*arrays):
    h = hashlib.sha256()
    for a in arrays:
        h.update(np.ascontiguousarray(np.asarray(a, np.float64)).tobytes())
    return h.hexdigest()[:24]


def ramp_case():
    """A deck rising on a curve (sin) between two approaches, its far end over a crossing floor; 3 members."""
    a0, dk, a1 = knots(0.0, -200.0), knots(0.0, 120.0), knots(120.0, 320.0)
    xy, off = flat([dk, a0, a1])
    zd = 6.0 * np.sin(np.pi * dk[:, 0] / 240.0) + 0.013 * dk[:, 0]
    z = np.concatenate([zd, 0.05 * np.sin(a0[:, 0] / 17.0), np.full(len(a1), zd[-1]) + 0.02 * (a1[:, 0] - 120.0)])
    g = np.concatenate([zd - 5.0, np.full(len(a0) + len(a1), np.nan)])
    joins = [1, 0, 0, 0, 1, 2, 0, 0, 1, 1]
    pins = [1, 1, 2, 2, 1, 2]
    floors = [0.0, 60.0, 0.0, 6.4]
    return (xy, off, z, g, [1, 0, 0], [5.0, 0.0, 0.0], [1.0, 0.0, 0.0], [5.0, 0.0, 0.0], [3, 1, 1], joins, pins,
            floors)


def sheet_case():
    """A ramp leaving a through deck 2 m over it in their union (P3a's), and an approach on the through deck."""
    th = knots(0.0, 200.0)
    rx = np.arange(0.0, 101.0, 2.0)
    ramp = np.stack([60.0 + rx, 0.08 * rx], axis=1)
    ap = knots(0.0, -150.0)
    xy, off = flat([th, ramp, ap])
    z = np.r_[np.full(len(th), 10.0) + 0.01 * th[:, 0], 10.6 + 2.0 * np.sin(np.pi * np.clip(rx / 60.0, 0, 1)),
              np.linspace(10.0, 4.0, len(ap))]
    g = np.full(len(z), np.nan)
    joins = [2, 0, 0, 0, 1]
    pins = [2, 1, 2]
    return xy, off, z, g, [1, 1, 0], [5.0, 4.0, 0.0], [1.0, 1.0, 0.0], [5.0, 5.0, 0.0], [3, 3, 1], joins, pins, []


@unittest.skipIf(EF is None, "earthfast 523 needed")
class Bytes(unittest.TestCase):
    GOLDEN = {
        "ramp": "b30e05b8e080457f7d9389b6",
        "sheet": "a0998d9624505490057d0d5b",
        "pins": "657e51a47f1fc45fa4fe592c",
    }

    def check(self, name, got):
        if os.environ.get("BNGE_UPDATE_GOLDEN"):
            print(f"{name}: {got}")
            return
        self.assertEqual(got, self.GOLDEN[name], f"{name}'s bytes changed")

    def test_ramp(self):
        zs, rep, tried = EF.profile_solve(*ramp_case(), sprm())
        self.assertEqual(int(rep[0]), 0)
        self.check("ramp", digest(zs, rep[:8], tried))

    def test_sheet(self):
        zs, rep, tried = EF.profile_solve(*sheet_case(), sprm())
        self.assertEqual(int(rep[0]), 0)
        self.check("sheet", digest(zs, rep[:8], tried))

    def test_pins(self):
        import grading
        q = knots(0.0, 200.0, 0.0, 2.0)
        q2 = knots(0.0, 200.0, 7.0, 2.0)        # (a twin beside it)
        dz = np.round(1.7 * np.cos(q[:, 0] / 70.0) * np.clip((200.0 - q[:, 0]) / 50.0, 0, 1), 4)
        h = np.round(100.0 + 0.03 * q[:, 0], 4)
        gx, gy = np.meshgrid(np.linspace(-30.0, 230.0, 53), np.linspace(-30.0, 40.0, 29))
        pts = np.ascontiguousarray(np.stack([gx.ravel(), gy.ravel()], axis=1))
        z = 100.0 + 0.03 * pts[:, 0] + np.where(pts[:, 1] > 25.0, 6.0, 0.0)     # (a road 6 m up past y = 25)
        qxy, qoff = flat([q, q2])
        prm = [grading.PIN_TAPER_M, grading.PIN_GATE_M[0], grading.PIN_GATE_M[1]]
        w = np.clip((200.0 - np.r_[q[:, 0], q2[:, 0]]) / 50.0, 0.0, 1.0)
        got = np.asarray(EF.profile_pin_field(pts, z, qxy, qoff, np.r_[dz, 0.5 * dz], np.r_[h, h], w,
                                              1.0 - 0.9 * np.r_[q[:, 0], q2[:, 0]] / 200.0, [5.0, 5.0], prm),
                         np.float64)
        on = np.flatnonzero((np.abs(pts[:, 1]) < 1e-9) & (pts[:, 0] >= 0.0) & (pts[:, 0] <= 200.0))
        self.assertTrue(np.any(got[on] != 0.0))
        self.assertFalse(np.any(got[pts[:, 1] > 25.0] != 0.0))     # (the road at another level: its own)
        self.check("pins", digest(got))

    def test_again_same(self):
        a = EF.profile_solve(*ramp_case(), sprm())
        b = EF.profile_solve(*ramp_case(), sprm())
        self.assertEqual(digest(a[0]), digest(b[0]))


if __name__ == "__main__":
    unittest.main()
