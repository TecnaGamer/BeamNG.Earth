"""cdae.unalign: colliders off the axis-aligned lattice (native unalign.rs; BeamNG's Windows collision build degenerates
on a regular axis-aligned grid: an 8k-triangle grid 145-171 ms a reloadCollision, the same jittered 4 ms).

Bounded and horizontal only, coincident vertices together (closed meshes and seams stay exact), no triangle turned
over, the surface's height moved by at most the amplitude times its slope (no step a wheel can feel), the same bits on
every platform (a golden digest: Linux and Windows CI run this), and only colliders touched by encode."""
import hashlib
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cdae  # noqa: E402
import dae  # noqa: E402
import fast  # noqa: E402

AMP = cdae.COLLIDER_UNALIGN_M


def grid(n=33, step=2.0, slope=0.0):
    xs = np.arange(n) * step
    X, Y = np.meshgrid(xs, xs)
    P = np.stack([X.ravel() + 100.0, Y.ravel() - 50.0, 12.0 + slope * X.ravel()], 1)    # (a tile's local frame)
    i = np.arange(n - 1)
    I, J = np.meshgrid(i, i)
    a = (J * n + I).ravel()
    T = np.concatenate([np.stack([a, a + 1, a + n + 1], 1), np.stack([a, a + n + 1, a + n], 1)])
    return P, T


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= cdae.UNALIGN_NATIVE, "needs native 595")
class Unalign(unittest.TestCase):
    def test_bounded_horizontal_coincident(self):
        P, _ = grid()
        Q = np.asarray(cdae.unalign(P), np.float64)
        d = Q - P
        self.assertLessEqual(np.abs(d[:, :2]).max(), AMP + 1e-5)       # (+ the float32 rounding)
        self.assertGreater(np.abs(d[:, :2]).max(), AMP / 4)
        np.testing.assert_array_equal(Q[:, 2], P[:, 2].astype(np.float32))
        # the same place twice: the same move (a closed mesh's duplicated vertices, a seam's two sides)
        twice = np.asarray(cdae.unalign(np.concatenate([P, P[::-1]])), np.float64)
        np.testing.assert_array_equal(twice[:len(P)], twice[len(P):][::-1])
        # off the lattice: the columns and rows of a grid no longer share coordinates
        self.assertGreater(len(np.unique(Q[:, 0])), 0.85 * len(P))
        self.assertGreater(len(np.unique(Q[:, 1])), 0.85 * len(P))

    def test_no_flip_no_step(self):
        P, T = grid(n=17, step=1.0, slope=0.5)          # a 50 % slope, 1 m squares (the station zones' finest)
        Q = np.asarray(cdae.unalign(P), np.float64)

        def area(V):
            a, b, c = V[T[:, 0], :2], V[T[:, 1], :2], V[T[:, 2], :2]
            return (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
        self.assertTrue((np.sign(area(P)) == np.sign(area(Q))).all())
        # the moved surface's height at each original vertex: within AMP x slope (x2 for both axes) of the plane's
        rng = np.random.default_rng(3)
        pts = P[:, :2][rng.choice(len(P), 200, replace=False)]
        a, b, c = Q[T[:, 0]], Q[T[:, 1]], Q[T[:, 2]]
        worst = 0.0
        for x, y in pts:
            v0, v1 = b[:, :2] - a[:, :2], c[:, :2] - a[:, :2]
            w = np.array([x, y]) - a[:, :2]
            den = v0[:, 0] * v1[:, 1] - v0[:, 1] * v1[:, 0]
            s = (w[:, 0] * v1[:, 1] - w[:, 1] * v1[:, 0]) / den
            t = (v0[:, 0] * w[:, 1] - v0[:, 1] * w[:, 0]) / den
            k = np.where((s >= -1e-9) & (t >= -1e-9) & (s + t <= 1 + 1e-9))[0]
            if not len(k):
                continue          # (the mesh's own edge moved inwards past the point)
            k = k[0]
            z = a[k, 2] + s[k] * (b[k, 2] - a[k, 2]) + t[k] * (c[k, 2] - a[k, 2])
            worst = max(worst, abs(z - (12.0 + 0.5 * (x - 100.0))))
        self.assertLess(worst, 2 * AMP * 0.5 + 1e-4)

    def test_same_bits_everywhere(self):
        P, _ = grid(n=9, step=2.0, slope=0.1)
        Q = np.ascontiguousarray(cdae.unalign(P), np.float32)
        # (Linux, 2026-11-22; Windows CI must give the same: integer hashing, one f64 add rounded to f32)
        self.assertEqual(hashlib.sha256(Q.tobytes()).hexdigest()[:16], GOLDEN)

    def test_only_colliders(self):
        pos, uv, tri = dae.grid(4, 2.0, lambda x, y: 0.1 * x)
        drawn = cdae.mesh_arrays(cdae.decode(cdae.encode(pos, tri, uvs=uv, material="m"))["meshes"][0])["verts"]
        col = cdae.decode(cdae.encode(pos, tri, uvs=uv, material="m", collision_only=True))
        cverts = [cdae.mesh_arrays(m)["verts"] for m in col["meshes"] if "verts" in m and m["verts"][0]]
        ref = np.asarray(pos, np.float64).reshape(-1, 3)
        self.assertAlmostEqual(float(np.abs(np.sort(drawn[:, 0]) - np.sort(ref[:, 0])).max()), 0.0, places=5)
        moved = np.abs(np.sort(cverts[-1][:, 0]) - np.sort(np.asarray(cdae.unalign(ref))[:, 0])).max()
        self.assertAlmostEqual(float(moved), 0.0, places=5)


GOLDEN = "f4060499263aa0fc"

if __name__ == "__main__":
    unittest.main()
