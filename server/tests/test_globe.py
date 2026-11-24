"""globe.py: the 3D map's whole-earth sphere (its grid, uv, winding) and the small copy's scale."""
import os
import sys
import tempfile
import types
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
            tex = os.path.join(d, "globe", f"world_s2wc2021-eox2016_z{globe.WORLD_ZOOM}_v{globe.WORLD_VERSION}.dds")
            os.makedirs(os.path.dirname(tex))
            open(tex, "wb").write(b"DDS ")
            f = geotiles.WorldFrame(33.46, -112.26)
            res = globe.build(os.path.join(d, "out"), "/temp/x", f, 1, d, None)
            self.assertAlmostEqual(res["miniScale"] * 6368000, globe.MINI_R, delta=50)
            self.assertTrue(os.path.exists(os.path.join(d, "out", "globe_a1_mini.cdae")))
            self.assertEqual(res["shape"], "/temp/x/globe_a1.cdae")



class _Resp:
    def __init__(self, body):
        self.body = body


class WorldTextureTest(unittest.TestCase):
    """The world texture (2026-11-23): the land from the Sentinel-2 packs (bngs2), EOX's 2016 mosaic (CC BY 4.0) at
    sea and past them; never EOX 2023 (CC BY-NC-SA)."""

    def fetcher(self, missing_pack=None):
        import io as _io
        import netfetch
        import tilepack
        from PIL import Image
        urls = []

        def jpeg(rgb):
            b = _io.BytesIO()
            Image.fromarray(rgb).save(b, "JPEG", quality=95)
            return b.getvalue()

        def get(url, kind="demand"):
            urls.append(url)
            if "tiles.maps.eox.at" in url:
                return _Resp(jpeg(np.full((256, 256, 3), (24, 44, 78), np.uint8)))
            z, px, py = (int(v) for v in url.rsplit(".", 1)[0].split("/")[-3:])
            if (px, py) == missing_pack:
                raise netfetch.NotFound("no land")
            land = np.full((256, 256, 3), (90, 140, 60), np.uint8)
            land[128:] = 0                  # (its south half: open sea, "no data")
            return _Resp(tilepack.build(z, px, py, 3, {0: jpeg(land)}))
        return types.SimpleNamespace(get=get), urls

    def test_packs_over_eox2016(self):
        f, urls = self.fetcher(missing_pack=(1, 1))
        img, has, whole = globe.s2_world(f)
        self.assertTrue(whole, "a pack that is not there is an answer (no land)")
        self.assertEqual(img.shape, (globe.WORLD_PX, globe.WORLD_PX, 3))
        self.assertTrue(has[:100, :256].all() and not has[160:256, :256].any() and not has[300:2048].any())
        self.assertTrue(has[2048:2148, :256].all() and has[:100, 2048:2304].all(), "every pack's first tile")
        self.assertFalse(has[2048:2148, 2048:2304].any(), "the pack that is not there")
        with tempfile.TemporaryDirectory() as d:
            f, urls = self.fetcher()
            path = globe.world_texture(d, f, s2=True)
            self.assertTrue(path.endswith(f"world_s2wc2021-eox2016_z{globe.WORLD_ZOOM}_v{globe.WORLD_VERSION}.dds"))
            self.assertEqual(len([u for u in urls if u.endswith(".bngp")]), 4)
            self.assertEqual(len({u for u in urls if "eox.at" in u}), 256)
            path2 = globe.world_texture(d, self.fetcher()[0], s2=False)
            self.assertTrue(path2.endswith(f"world_eox2016_z{globe.WORLD_ZOOM}_v{globe.WORLD_VERSION}.dds"))
        self.assertTrue(all("2023" not in u for u in urls))
        self.assertIn("/s2cloudless_3857/", globe.EOX_URL)


if __name__ == "__main__":
    unittest.main()
