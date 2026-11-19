"""The sidewalks' lean collision mesh (sidewalks.Builder(collision=True), the tile's "walkcol")."""
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cdae  # noqa: E402
import sidewalks  # noqa: E402


def run(n=200, bend=0.0):
    s = np.linspace(0.0, 100.0, n)
    curb = np.stack([s, bend * np.sin(s / 15.0), 0.02 * s], 1)
    out = np.tile([0.0, -1.0], (n, 1))
    return curb, out


class NarrowBandTest(unittest.TestCase):
    def test_profile_never_folds(self):
        # GitHub #55 "the median eats tires": a band narrower than RAMP_RUN folded back over itself (its ramp end past
        # its outer edge), and the folded collision caught wheels
        for w in (0.3, 0.6, 1.2, 1.8, 2.5, 4.0):
            for hr in (sidewalks.CURB_H, 0.02):
                x = sidewalks.profile(sidewalks.CURB_H, w, hr)[:, 0]
                self.assertTrue((np.diff(x) > 0).all(), (w, hr, x.tolist()))
        np.testing.assert_allclose(sidewalks.profile(sidewalks.CURB_H, 2.5)[5], [sidewalks.RAMP_RUN,
                                   sidewalks.CURB_H + sidewalks.RAMP_RUN * sidewalks.CROSS_SLOPE], err_msg="wide: as before")


class SimplifyTest(unittest.TestCase):
    def test_straight_line_to_its_ends(self):
        pts = np.stack([np.linspace(0, 10, 50), np.linspace(0, 5, 50), np.zeros(50)], 1)
        self.assertEqual(sidewalks.simplify(pts).tolist(), [0, 49])

    def test_keeps_corners_and_within_tolerance(self):
        t = np.linspace(0, 2 * np.pi, 400)
        pts = np.stack([20 * np.cos(t), 20 * np.sin(t)], 1)
        keep = sidewalks.simplify(pts, 0.03)
        self.assertLess(len(keep), 120)
        self.assertEqual((keep[0], keep[-1]), (0, 399))
        # every dropped point within tol of the kept polyline
        k = pts[keep]
        for p in pts[::7]:
            a, b = k[:-1], k[1:]
            ab = b - a
            tt = np.clip(((p - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0, 1)
            d = np.hypot(*(a + tt[:, None] * ab - p).T).min()
            self.assertLessEqual(d, 0.03 + 1e-9)

    def test_short(self):
        self.assertEqual(sidewalks.simplify(np.zeros((2, 3))).tolist(), [0, 1])
        self.assertEqual(sidewalks.simplify(np.zeros((1, 3))).tolist(), [0])


class LeanMeshTest(unittest.TestCase):
    def build(self, **kw):
        b = sidewalks.Builder((0.0, 0.0, 0.0), collision=True)
        curb, out = run(**kw)
        b.add_run(curb, out, 2.5, ramps=[(40.0, 42.0)])
        b.add_corner(curb[-10:], np.tile([0.0, -1.0], (10, 1)), [[100, -2.5, 2.0], [102.5, -2.5, 2.0], [102.5, 0, 2.0]])
        return b

    def test_fewer_triangles_same_surface(self):
        b = self.build(bend=0.0)
        P, T, _, _ = b.arrays()
        CP, CT, _, _ = b.col.arrays()
        self.assertLess(len(CT), len(T) / 4)
        # the slab top at its outer edge: the same height in both meshes (the lean keeps profile points 6)
        self.assertAlmostEqual(P[:, 2].max(), CP[:, 2].max(), places=6)
        self.assertAlmostEqual(P[:, 2].min(), CP[:, 2].min(), places=6)

    def test_ramp_kept(self):
        b = self.build(bend=0.0)
        CP = b.col.arrays()[0]
        # a point at the lowered curb (RAMP_H above the road) survives the simplification
        s = CP[:, 0]
        road = 0.02 * s
        low = np.abs(CP[:, 2] - road - sidewalks.RAMP_H) < 1e-6
        self.assertTrue(low.any())

    def test_written_as_collision_shape(self):
        b = self.build(bend=3.0)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "walkcol.cdae")
            b.write_collision(p)
            shape = cdae.read(p)
        self.assertEqual(shape["names"], ["Colmesh", "detail2", "Collision-1"])   # (detail2: a null mesh, cdae.encode)

    def test_without_collision_no_lean_builder(self):
        self.assertIsNone(sidewalks.Builder((0.0, 0.0, 0.0)).col)


class NormalsTest(unittest.TestCase):
    """sidewalks._normals_2d (earthfast.sidewalk_normals, 508): the curb lines' mitred left normals."""

    def test_straight_bend_and_ends(self):
        n = sidewalks._normals_2d(np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]]))
        self.assertEqual(n.shape, (3, 2))
        self.assertTrue(n.flags.writeable and n.dtype == np.float64)
        np.testing.assert_array_equal(n[0], [0.0, 1.0])            # (the end segments' own normals)
        np.testing.assert_array_equal(n[2], [-1.0, 0.0])
        np.testing.assert_allclose(n[1], [-1.0, 1.0], atol=1e-12)  # (the 90 degree joint: mitred, sqrt 2 long)
        # a hairpin: the mitre is clamped at 2x, and a repeated point gives no NaN
        hp = sidewalks._normals_2d(np.array([[0.0, 0.0], [10.0, 0.0], [0.0, 0.1], [0.0, 0.1], [5.0, 5.0]]))
        self.assertTrue(np.isfinite(hp).all())
        self.assertLessEqual(float(np.hypot(*hp[1])), 2.0 + 1e-12)

    def test_closed_loop_mitred_across_seam(self):
        sq = np.array([[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0], [0.0, 0.0]])
        n = sidewalks._normals_2d(sq)
        np.testing.assert_array_equal(n[0], n[-1])
        np.testing.assert_allclose(n[0], [1.0, 1.0], atol=1e-12)
        open_ = sidewalks._normals_2d(sq + np.array([[0.0, 0.0]] * 4 + [[0.0, 1e-3]]))
        np.testing.assert_allclose(open_[0], [0.0, 1.0], atol=1e-12)  # (not closed: the one-sided end)

    def test_nan_sign_as_numpy(self):
        # a NaN point's NaN carried through as numpy carried it: the normal's x is -ty, the sign flipped after the
        # division (the compiler may otherwise negate first and keep the NaN's sign; try598's Python gave these bits)
        n = sidewalks._normals_2d(np.array([[np.nan, 0.0], [1.0, 0.0], [2.0, 1.0]]))
        self.assertEqual([hex(int(v)) for v in n.view(np.uint64)[0]], ["0xfff8000000000000", "0x7ff8000000000000"])
        with self.assertRaises(TypeError):
            sidewalks._normals_2d(np.zeros((3, 2), np.float32))

    def test_start_check_needs_508_in_every_mode(self):
        # (BNE_BATTER=vanilla set MIN_VERSION to 503 outright: a 505 binary started and failed at the first sidewalk)
        import subprocess
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for mode in ("off", "vanilla"):
            out = subprocess.run([sys.executable, "-c", "import fast; print(fast.MIN_VERSION)"], cwd=here,
                                 env=dict(os.environ, BNE_BATTER=mode), capture_output=True, text=True, check=True)
            self.assertGreaterEqual(int(out.stdout.strip()), 508, mode)

    def test_strided_and_short(self):
        xyz = np.array([[0.0, 0.0, 5.0], [3.0, 4.0, 6.0]])
        np.testing.assert_array_equal(sidewalks._normals_2d(xyz[:, :2]), [[-0.8, 0.6], [-0.8, 0.6]])
        for k in (0, 1):
            with self.assertRaises(IndexError):
                sidewalks._normals_2d(np.zeros((k, 2)))


if __name__ == "__main__":
    unittest.main()
