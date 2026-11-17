"""railxing.corridor_ground (earthfast 372: a grid over the tracks' points) against the numpy version it replaced, bit for
bit: random line sets with exact distance ties, duplicate points, one-point lines, parallel and antiparallel tracks at
exactly PARALLEL_M, NaN / inf / huge coordinates (the kernel's full path) and NaN heights.

The numpy version is kept here only as the reference (the server has no Python twin: fast.require). Real windows'
lines are compared too when given: BNGE_RAILXING_CAPTURE, .npz files (comma-separated) of xy_<n> / z_<n> arrays, the
inputs of railways.profile's corridor_ground calls recorded while planning cells from a scratch cache (Fells Point's
rail yards, 2026-11-17)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dnp  # noqa: E402
import railxing  # noqa: E402
from railxing import CORRIDOR_MIN_STEP, PARALLEL_COS, PARALLEL_M  # noqa: E402


def _ref_tangents(xy):
    d = np.gradient(xy, axis=0)
    n = dnp.hypot(d[:, 0], d[:, 1])
    return d / np.maximum(n, CORRIDOR_MIN_STEP)[:, None]


def ref_corridor_ground(lines, zs):
    """railxing.corridor_ground as numpy had it (before earthfast 372), verbatim."""
    out = [np.asarray(z, np.float64).copy() for z in zs]
    pts = [np.asarray(l, np.float64)[:, :2] for l in lines]
    tan = [_ref_tangents(p) if len(p) >= 2 else np.zeros_like(p) for p in pts]
    box = [(p[:, 0].min() - PARALLEL_M, p[:, 1].min() - PARALLEL_M, p[:, 0].max() + PARALLEL_M,
            p[:, 1].max() + PARALLEL_M) for p in pts]
    for i, pi in enumerate(pts):
        acc, cnt = np.asarray(zs[i], np.float64).copy(), np.ones(len(pi))
        for j, pj in enumerate(pts):
            if i == j or box[i][0] > box[j][2] or box[j][0] > box[i][2] or box[i][1] > box[j][3] or \
                    box[j][1] > box[i][3]:
                continue
            d = dnp.hypot(pi[:, None, 0] - pj[None, :, 0], pi[:, None, 1] - pj[None, :, 1])
            k = np.argmin(d, axis=1)
            near = d[np.arange(len(pi)), k]
            cos = np.abs((tan[i] * tan[j][k]).sum(1))
            ok = (near <= PARALLEL_M) & (cos >= PARALLEL_COS)
            acc[ok] += np.asarray(zs[j], np.float64)[k[ok]]
            cnt[ok] += 1.0
        out[i] = acc / cnt
    return out


def bits(a):
    return np.ascontiguousarray(np.asarray(a, np.float64)).view(np.uint64)


class CorridorParityTest(unittest.TestCase):
    def same(self, lines, zs, what=""):
        with np.errstate(all="ignore"):
            want = ref_corridor_ground(lines, zs)
        got = railxing.corridor_ground(lines, zs)
        self.assertEqual(len(got), len(want))
        for n, (g, w) in enumerate(zip(got, want)):
            self.assertEqual(g.dtype, np.float64)
            self.assertEqual(g.shape, w.shape)
            if not np.array_equal(bits(g), bits(w)):
                bad = np.flatnonzero(bits(g) != bits(w))
                self.fail(f"{what} line {n}: {len(bad)} points differ, first {bad[0]}: {g[bad[0]]!r} vs {w[bad[0]]!r}")
        return got

    def random_set(self, rng, n_lines, spread, step, quant):
        lines, zs = [], []
        for _ in range(n_lines):
            kind = rng.integers(0, 6)
            n = 1 if kind == 0 else int(rng.integers(2, 60))
            x0, y0 = rng.uniform(0, spread, 2)
            a = rng.uniform(0, 2 * np.pi) if kind == 1 else rng.choice([0.0, np.pi, np.pi / 2, 0.05, np.pi + 0.05])
            s = np.cumsum(np.r_[0.0, rng.choice([0.0, step, step, step, step * 0.5], n - 1)])  # (duplicate points)
            bend = rng.uniform(-0.02, 0.02) * s * s / max(s[-1], 1.0) if kind == 2 else 0.0
            xy = np.c_[x0 + s * np.cos(a) - bend * np.sin(a), y0 + s * np.sin(a) + bend * np.cos(a)]
            if quant:
                xy = np.round(xy / quant) * quant              # (a lattice: exact distance ties)
            if kind == 3:
                xy = np.c_[xy, rng.uniform(0, 5, len(xy))]     # (a third column, ignored)
            lines.append(xy)
            zs.append(np.round(rng.uniform(0, 20, n), 3))
        return lines, zs

    def test_random_sets(self):
        rng = np.random.default_rng(372)
        for case in range(300):
            lines, zs = self.random_set(rng, int(rng.integers(1, 14)), float(rng.choice([30.0, 80.0, 300.0])),
                                        float(rng.choice([1.0, 2.0, 2.5])), float(rng.choice([0.0, 0.5, 1.0, 2.0])))
            self.same(lines, zs, f"case {case}")

    def test_ties_and_exact_reach(self):
        x = np.arange(0.0, 60.0, 2.0)
        base = np.c_[x, np.zeros_like(x)]
        z0 = np.linspace(3.0, 4.0, len(x))
        sets = [
            [base, np.c_[x, np.full_like(x, PARALLEL_M)]],                          # (exactly 14 m: in)
            [base, np.c_[x[::-1], np.full_like(x, PARALLEL_M)]],                    # (antiparallel at 14 m)
            [base, np.c_[x, np.full_like(x, np.nextafter(PARALLEL_M, 99.0))]],      # (an ulp past: out)
            [base, np.c_[x + 1.0, np.full_like(x, 3.0)]],                           # (two nearest at equal distance)
            [base, np.c_[np.repeat(x, 2), np.full(2 * len(x), 4.0)]],               # (every point twice)
            [base, np.c_[x, np.full_like(x, -PARALLEL_M)], np.c_[x, np.full_like(x, PARALLEL_M)], base.copy()],
            [base, np.array([[10.0, 5.0]]), np.array([[10.0, 5.0], [10.0, 5.0]])],  # (one point; a still line)
            [np.c_[x * 1e-7, np.zeros_like(x)], np.c_[x * 1e-7, np.full_like(x, 1e-7)]],   # (steps under min_step)
            [base + 1e14, np.c_[x, np.full_like(x, 5.0)] + 1e14],                   # (far from the origin: grid)
            [base + 2e15, np.c_[x, np.full_like(x, 5.0)] + 2e15],                   # (past the grid's range: full)
            [base - 37.25, np.c_[x - 37.25, np.full_like(x, -40.0)]],               # (negative cells)
        ]
        for n, lines in enumerate(sets):
            zs = [z0[:len(ln)] if len(ln) <= len(z0) else np.resize(z0, len(ln)) + k for k, ln in enumerate(lines)]
            self.same(lines, zs, f"set {n}")

    def test_non_finite(self):
        x = np.arange(0.0, 40.0, 2.0)
        a, b = np.c_[x, np.zeros_like(x)], np.c_[x, np.full_like(x, 5.0)]
        za, zb = np.linspace(1.0, 2.0, len(x)), np.linspace(2.0, 3.0, len(x))
        for bad in (np.nan, np.inf, -np.inf):
            for where in ((0, 0), (5, 1), (len(x) - 1, 0)):
                b2 = b.copy()
                b2[where] = bad
                self.same([a, b2], [za, zb], f"{bad} at {where}")
                self.same([b2, a, b], [zb, za, zb], f"{bad} at {where}, three lines")
        zn = za.copy()
        zn[3] = np.nan
        self.same([a, b], [zn, zb], "a NaN height")
        self.same([a, b], [za, np.where(np.arange(len(x)) == 4, np.inf, zb)], "an inf height")

    def test_empty_and_one(self):
        self.assertEqual(railxing.corridor_ground([], []), [])
        self.same([np.array([[1.0, 2.0], [3.0, 4.0]])], [np.array([5.0, 6.0])], "one line")
        with self.assertRaises(ValueError):
            railxing.corridor_ground([np.zeros((0, 2)), np.zeros((2, 2))], [np.zeros(0), np.zeros(2)])

    def test_dense_yard(self):
        """Forty parallel tracks 4.5 m apart with crossovers (the case that never finished in numpy at its real size)."""
        rng = np.random.default_rng(7)
        lines, zs = [], []
        for t in range(40):
            x = np.arange(0.0, 300.0, 2.0) + rng.uniform(0, 2)
            lines.append(np.c_[x, np.full_like(x, 4.5 * t) + rng.normal(0, 0.05, len(x))])
            zs.append(rng.normal(10.0, 0.5, len(x)))
        for t in range(10):
            s = np.arange(0.0, 60.0, 2.0)
            x0, y0 = rng.uniform(0, 240), rng.uniform(0, 160)
            lines.append(np.c_[x0 + s * 0.97, y0 + s * 0.24])
            zs.append(rng.normal(10.0, 0.5, len(s)))
        self.same(lines, zs, "yard")

    def test_capture(self):
        paths = [p for p in os.environ.get("BNGE_RAILXING_CAPTURE", "").split(",") if p]
        if not paths:
            self.skipTest("BNGE_RAILXING_CAPTURE not set")
        for path in paths:
            with np.load(path) as f:
                n = len([k for k in f.files if k.startswith("xy_")])
                lines = [f[f"xy_{i}"] for i in range(n)]
                zs = [f[f"z_{i}"] for i in range(n)]
            self.same(lines, zs, os.path.basename(path))


if __name__ == "__main__":
    unittest.main()
