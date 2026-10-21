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

    def test_back_z_conforms_to_falling_ground_and_keeps_skirt(self):
        curb = np.array([[0.0, 0.0, 10.0], [20.0, 0.0, 10.0], [40.0, 0.0, 10.0]])
        out = np.tile([0.0, -1.0], (3, 1))
        back_z = np.array([9.2, 8.8, 8.5])          # hillside falls 0.8..1.5 m below the curb line
        b = sidewalks.Builder((0.0, 0.0, 0.0), collision=True)
        b.add_run(curb, out, 2.5, caps=(True, True), back_z=back_z)
        P, T, _, _ = b.arrays()
        CP, CT, _, _ = b.col.arrays()
        # curb top at 10.0 + CURB_H; outer back foundation drops to min(back_z) - SINK - SKIRT
        self.assertAlmostEqual(P[:, 2].max(), 10.0 + sidewalks.CURB_H, places=5)
        self.assertAlmostEqual(CP[:, 2].max(), 10.0 + sidewalks.CURB_H, places=5)
        want_min = 8.5 - sidewalks.SINK - sidewalks.SKIRT
        self.assertAlmostEqual(P[:, 2].min(), want_min, places=5)
        self.assertAlmostEqual(CP[:, 2].min(), want_min, places=5)


if __name__ == "__main__":
    unittest.main()
