"""globe.py: the 3D map's whole-earth sphere (its grid, uv, winding) and the small copy's scale."""
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geotiles  # noqa: E402
import globe  # noqa: E402


class SphereTest(unittest.TestCase):
    def test_grid_uv_and_outward_triangles(self):
        lat, lon, uv, tris = globe.sphere(8, 16)
        self.assertEqual(len(lat), 9 * 17)
        self.assertEqual(tris.shape, (2 * 8 * 16, 3))
        self.assertGreaterEqual(uv.min(), -1e-9)
        self.assertLessEqual(uv.max(), 1 + 1e-9)
        f = geotiles.WorldFrame(10.0, 20.0)
        e, n, u = f.geodetic_to_enu(lat, lon, np.zeros(len(lat)))
        p = np.stack([e, n, u], 1)
        c = np.asarray(f.ecef_to_enu(0.0, 0.0, 0.0), np.float64).reshape(3)
        a, b, d = p[tris[:, 0]], p[tris[:, 1]], p[tris[:, 2]]
        nrm = np.cross(b - a, d - a)
        area = np.linalg.norm(nrm, axis=1)
        live = area > 1e-3                          # (the poles' rows are degenerate)
        out = np.einsum("ij,ij->i", nrm[live], (a + b + d)[live] / 3 - c)
        self.assertTrue((out > 0).all(), "every triangle faces outward")

    def test_mini_copy_scale(self):
        with tempfile.TemporaryDirectory() as d:
            tex = os.path.join(d, "globe", f"world_eox2023_z{globe.WORLD_ZOOM}_v{globe.WORLD_VERSION}.dds")
            os.makedirs(os.path.dirname(tex))
            open(tex, "wb").write(b"DDS ")
            f = geotiles.WorldFrame(33.46, -112.26)
            res = globe.build(os.path.join(d, "out"), "/temp/x", f, 1, d, None)
            self.assertAlmostEqual(res["miniScale"] * 6368000, globe.MINI_R, delta=50)
            self.assertTrue(os.path.exists(os.path.join(d, "out", "globe_a1_mini.cdae")))
            self.assertEqual(res["shape"], "/temp/x/globe_a1.cdae")


if __name__ == "__main__":
    unittest.main()
