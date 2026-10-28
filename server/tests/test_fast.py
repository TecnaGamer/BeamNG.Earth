"""fast: the Rust kernels (earthfast) against independent references (Pillow, the JSON encoder, plain sums), and
fast.require. The Python twins they replaced are gone (the user, 2026-10-21: "we dont need 2 versions of the same
thing"); each module's own tests check its results."""
import os
import sys
import types
import unittest
from unittest import mock
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fast  # noqa: E402


class RequireTest(unittest.TestCase):
    def test_require(self):
        saved = fast._ef
        try:
            fast._ef = None
            with self.assertRaises(fast.NativeMissing):
                fast.require(0, "x")
            fast._ef = types.SimpleNamespace(VERSION=3)
            fast.require(3, "x")
            with self.assertRaises(fast.NativeMissing):
                fast.require(4, "x")
        finally:
            fast._ef = saved


@unittest.skipUnless(fast.AVAILABLE, "no earthfast binary for this platform (tools/build_native.py)")
class FastTest(unittest.TestCase):
    def test_srgb_monotonic(self):
        # the Rust sRGB step (mip_levels) reads bytes from thresholds: exact only if the formula never decreases
        self.assertTrue(fast._ef.srgb_monotonic())

    def test_any_within(self):
        rng = np.random.default_rng(14)
        for trial in range(80):
            road = np.cumsum(rng.normal(0, 12, (int(rng.integers(2, 7)), 2)), 0) + rng.normal(0, 20, 2)
            pts = rng.normal(0, 30, (int(rng.integers(1, 20)), 2))
            h = float(rng.uniform(1, 10))
            a, b = road[:-1], road[1:]
            ab = b - a
            t = np.clip(((pts[:, None] - a[None]) * ab[None]).sum(2) / np.maximum((ab * ab).sum(1), 1e-12), 0, 1)
            d = np.hypot(*(pts[:, None] - (a[None] + t[..., None] * ab[None])).transpose(2, 0, 1))
            self.assertEqual(fast._ef.any_within(pts, road, h), bool((d < h).any()), trial)

    def test_veg_rows_json_matches_python(self):
        import json
        import tempfile
        import vegetation as veg
        rng = np.random.default_rng(67)
        n = 3000
        cols = [rng.integers(0, 40, n), rng.uniform(-200, 200, n), rng.uniform(-200, 200, n), rng.uniform(-50, 3000, n),
                rng.uniform(-180, 180, n), rng.uniform(0.5, 2, n), rng.uniform(1, 40, n), rng.uniform(0, 1, n)]
        cols[1][:300] = rng.integers(-100, 100, 300) + 0.005         # ties
        cols[7][:300] = rng.uniform(0, 0.0004, 300)                   # tiny ranks
        cols[2][:50] = -0.001                                         # -0.0
        rows = [[int(g), round(float(a), 2), round(float(b), 2), round(float(c), 2), round(float(y), 1),
                 round(float(s), 3), round(float(h), 1), round(float(r), 4)] for g, a, b, c, y, s, h, r in zip(*cols)]
        js = veg.JsonRows(fast._ef.veg_rows_json(*(np.ascontiguousarray(c, np.int64 if i == 0 else np.float64)
                                                   for i, c in enumerate(cols))))
        js.n = n
        with tempfile.TemporaryDirectory() as tmp:
            a = veg.write_tile(tmp, "/v", 1, 0, 0, rows)
            with open(os.path.join(tmp, "veg_1_0_0.json")) as fh:
                want = fh.read()
            b = veg.write_tile(tmp, "/v", 1, 0, 0, js)
            with open(os.path.join(tmp, "veg_1_0_0.json")) as fh:
                got = fh.read()
        self.assertEqual(a, b)
        # (the JSON rows carry the tile's ambience too: none here)
        self.assertEqual(got, want.replace('"items":', '"audio":[],"biome":[0,0],"items":', 1))
        self.assertEqual(len(json.loads(got)["items"]), n)
        # flat (hello.vegFlat): the Rust text flattened as text is the same file as the Python rows' flattened list, the
        # same numbers as the rows, in their order
        with tempfile.TemporaryDirectory() as tmp:
            fa = veg.write_tile(tmp, "/v", 1, 0, 0, rows, flat=True)
            with open(os.path.join(tmp, "veg_1_0_0.flat.json")) as fh:
                flat_want = fh.read()
            fb = veg.write_tile(tmp, "/v", 1, 0, 0, js, flat=True)
            with open(os.path.join(tmp, "veg_1_0_0.flat.json")) as fh:
                flat_got = fh.read()
        self.assertEqual(fa, ("/v/veg_1_0_0.flat.json", n))
        self.assertEqual(fb, fa)
        # (the same tile but for its sound, which only tile_items' JsonRows carry: audio and biome, ambience.lua)
        self.assertEqual({k: v for k, v in json.loads(flat_got).items() if k not in ("audio", "biome")},
                         json.loads(flat_want))
        doc = json.loads(flat_got)
        self.assertEqual((doc["groups"], doc["stride"]), (list(veg.GROUPS), veg.FLAT_STRIDE))
        self.assertEqual(doc["flat"], [v for r in json.loads(got)["items"] for v in r])


    def test_mesh_warp_matches_pillow(self):
        from PIL import Image
        rng = np.random.default_rng(47)
        for t in range(12):
            H, W = int(rng.integers(256, 700)), int(rng.integers(256, 700))
            img = rng.integers(0, 256, (H, W, 4), dtype=np.uint8)
            if t % 2:
                img[..., 3] = rng.choice(np.array([0, 254, 255], np.uint8), (H, W))
            px = int(rng.choice([64, 128, 256])); cells = min(16, px); cw = px // cells
            k = np.arange(cells + 1) / cells
            X, Y = np.meshgrid(k, k)
            qx = X * W * rng.uniform(0.3, 1.2) + rng.uniform(-50, 200) + 20 * np.sin(Y * 3)
            qy = Y * H * rng.uniform(0.3, 1.2) + rng.uniform(-50, 200) + 15 * np.cos(X * 2)
            mesh = [((c * cw, r * cw, (c + 1) * cw, (r + 1) * cw),
                     (qx[r, c], qy[r, c], qx[r + 1, c], qy[r + 1, c], qx[r + 1, c + 1], qy[r + 1, c + 1],
                      qx[r, c + 1], qy[r, c + 1])) for r in range(cells) for c in range(cells)]
            want = np.asarray(Image.fromarray(img, "RGBA").transform((px, px), Image.MESH, mesh,
                                                                     resample=Image.BILINEAR))
            got = fast._ef.mesh_warp(img, np.ascontiguousarray(qx), np.ascontiguousarray(qy), cells, cw, px)
            np.testing.assert_array_equal(got, want, err_msg=str(t))


    def test_points_and_distances(self):
        sq = np.array([[0, 0], [10, 0], [10, 10], [0, 10]], float)
        pts = np.array([[5, 5], [15, 5], [-1, -1], [9.9, 0.1]], float)
        self.assertEqual(list(fast._ef.points_inside(sq, pts)), [True, False, False, True])
        d = fast._ef.polyline_dist(np.array([[0, 0], [10, 0]], float), np.array([[5, 3], [12, 0], [-3, 4]], float))
        self.assertTrue(np.allclose(d, [3, 2, 5]))




class GpuDefaultTest(unittest.TestCase):
    def test_windows_off_unless_asked(self):
        # Windows players saw the game freeze in new places with 1.6.0's GPU kernels: the CPU path unless BNGE_GPU=1
        import fast
        with mock.patch.object(fast.sys, "platform", "win32"), mock.patch.object(fast, "_GPU", None), \
                mock.patch.dict(fast.os.environ, {}, clear=False):
            fast.os.environ.pop("BNGE_GPU", None)
            self.assertIsNone(fast.gpu())


if __name__ == "__main__":
    unittest.main()
