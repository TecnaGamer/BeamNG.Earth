"""The per-sample kernels on threads (earthfast par.rs, 287): the same bits on one thread and on several, for the plan
apply (grade_points_binned's bins), the mip levels (float and integer), the CPU BC1 / BC7 encoders and the far
textures' DDS payload, and (651) the terrain block's elementwise kernels; and the integer mips / ISPC BC1 against the
float mips / our BC1 (the far textures' look)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fast  # noqa: E402
import grading  # noqa: E402
import imagery  # noqa: E402
from test_grade_binned import random_plans  # noqa: E402

fast.require(287, "native threads")
EF = fast._ef
THREADS = (1, 2, 3, 8)


def on_threads(fn):
    """[fn() on each of THREADS' caps] (the cap put back after)."""
    out = []
    try:
        for t in THREADS:
            EF.set_native_threads(t)
            out.append(fn())
    finally:
        EF.set_native_threads(0)
    return out


def photo_like(rng, n):
    """An n x n RGB image with smooth areas, edges, noise and black / white texels (what the encoders meet)."""
    y, x = np.mgrid[0:n, 0:n] / n
    img = np.stack([128 + 100 * np.sin(7 * x + 3 * y), 128 + 90 * np.cos(5 * y), 60 + 150 * x * y], -1)
    img += rng.normal(0, 12, img.shape)
    img[(x * 13 % 1) < 0.08] = 0.0           # (dark lines beside bright ground: the sharpen's hardest case)
    img[n // 3:n // 3 + 9, :] = 255.0
    return np.ascontiguousarray(np.clip(img, 0, 255).astype(np.uint8))


def psnr(a, b):
    m = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return 99.0 if m == 0 else 10 * np.log10(255.0 ** 2 / m)


def bc1_decode(blob, h, w):
    """RGB of BC1 blocks, and the count of 3-colour blocks' transparent texels (none wanted: opaque ground)."""
    nby, nbx = (h + 3) // 4, (w + 3) // 4
    b = np.frombuffer(blob, np.uint8).reshape(nby, nbx, 8).astype(np.uint32)
    c0 = b[..., 0] | (b[..., 1] << 8)
    c1 = b[..., 2] | (b[..., 3] << 8)
    idx = b[..., 4] | (b[..., 5] << 8) | (b[..., 6] << 16) | (b[..., 7] << 24)

    def rgb(c):
        r, g, bl = (c >> 11) & 31, (c >> 5) & 63, c & 31
        return np.stack([(r << 3) | (r >> 2), (g << 2) | (g >> 4), (bl << 3) | (bl >> 2)], -1).astype(np.int32)
    a, bb = rgb(c0), rgb(c1)
    four = (c0 > c1)[..., None]
    pal = np.stack([a, bb, np.where(four, (2 * a + bb) // 3, (a + bb) // 2), np.where(four, (a + 2 * bb) // 3, 0)], -2)
    out = np.zeros((nby * 4, nbx * 4, 3), np.int32)
    holes = 0
    for k in range(16):
        s = (idx >> (2 * k)) & 3
        holes += int(((c0 <= c1) & (s == 3)).sum())
        out[k // 4::4, k % 4::4] = np.take_along_axis(pal, s[..., None, None].repeat(3, -1), -2)[..., 0, :]
    return out[:h, :w], holes


class NativeThreadsTest(unittest.TestCase):
    def test_cap(self):
        try:
            self.assertEqual(EF.set_native_threads(1), 1)
            self.assertLessEqual(EF.set_native_threads(3), 3)
            self.assertGreaterEqual(EF.native_threads(), 1)
        finally:
            EF.set_native_threads(0)
        self.assertLessEqual(EF.native_threads(), 8)

    def test_grade_points_binned(self):
        # a block-sized batch (the bins split over the threads: over 16 k points each) with NaN points and grounds
        rng = np.random.default_rng(5)
        plans = random_plans(rng, 120, (-40, -40, 640, 640))
        px, py = np.meshgrid(np.arange(0.0, 600.0, 0.75), np.arange(0.0, 600.0, 0.75))
        px, py = px.ravel().copy(), py.ravel().copy()
        base = 100.0 + 0.01 * px + rng.normal(0, 0.4, len(px))
        px[:7] = np.nan
        base[100:130] = np.nan
        for avx2 in (True, False):
            if not avx2:
                os.environ["BNE_GRADE_NO_AVX2"] = "1"
            try:
                got = on_threads(lambda: grading.grade_points(px, py, base, plans, grading.ROAD_LIFT))
            finally:
                os.environ.pop("BNE_GRADE_NO_AVX2", None)
            for g in got[1:]:
                np.testing.assert_array_equal(g, got[0])
                np.testing.assert_array_equal(np.signbit(g), np.signbit(got[0]))
            self.assertGreater(int((got[0] != base).sum()), 10000, "the plans graded the ground")

    def test_terrain_block_kernels(self):
        # 651: the terrain block's elementwise kernels split over the threads: the same bits on 1, 2, 3 and 8 (arrays
        # over par.rs's 32 k items a thread, with NaN and edge values)
        import heightmap
        import geodesy
        import geotiles
        import seacarve
        import surface
        fast.require(651, "threaded terrain kernels")
        rng = np.random.default_rng(11)
        n = 300_000
        x = rng.normal(0, 3, n)
        x[:5] = (np.nan, np.inf, -np.inf, 0.0, -0.0)
        y = rng.normal(0, 3, n)
        for op in (0, 5, 9, 12, 15):            # sin, atan, asinh, log, cbrt
            got = on_threads(lambda: EF.dm_vec(op, x).tobytes())
            self.assertTrue(all(g == got[0] for g in got[1:]), op)
        for op in (32, 33, 34):                 # atan2, pow, hypot
            got = on_threads(lambda: EF.dm_vec(op, x, y).tobytes())
            self.assertTrue(all(g == got[0] for g in got[1:]), op)
        lat = 40.7 + rng.random(n) * 0.05
        lon = -74.0 + rng.random(n) * 0.05
        got = on_threads(lambda: [a.tobytes() for a in geotiles.tile_of(lat, lon, 16)])
        self.assertTrue(all(g == got[0] for g in got[1:]))

        def tiles(keys):        # (heights from each tile's key: the same tiles whatever is asked first)
            return [np.random.default_rng(abs(hash(tuple(k))) % 2 ** 32).random((512, 512)).astype(np.float32) * 50
                    for k in keys]
        got = on_threads(lambda: EF.dem_sample(lat, lon, 15, 512, 84.9, 1024, 16, 4, 2, tiles,
                                               lambda la, lo: None).tobytes())
        self.assertTrue(all(g == got[0] for g in got[1:]))

        # the block lattice: coordinates, heights back, the chunk grid (all chunks new and some from the cache)
        frame = geodesy.EnuFrame(40.72, -73.99)
        size, cx, cy = 1024, 0.0, 0.0
        xs, ys = heightmap.sample_coords(cx, cy, size)

        def elev(la, lo):
            return 10.0 + 30.0 * np.sin((np.asarray(la) - 40.72) * 900.0) + (np.asarray(lo) + 73.99) * 100.0
        got = on_threads(lambda: [a.tobytes() for a in EF.block_coordinates(xs, ys, frame.origin, frame.rot)])
        self.assertTrue(all(g == got[0] for g in got[1:]))
        la, lo = EF.block_coordinates(xs, ys, frame.origin, frame.rot)
        got = on_threads(lambda: EF.block_up(la, lo, elev(la, lo), frame.origin, frame.rot).tobytes())
        self.assertTrue(all(g == got[0] for g in got[1:]))

        def chunked():
            cache, keep = heightmap.ChunkCache(), {}
            u, _ = heightmap.grid_u_chunked(frame, elev, cx, cy, size, cache, keep)
            u2, share = heightmap.grid_u_chunked(frame, elev, cx + heightmap.CHUNK * heightmap.SQUARE, cy, size,
                                                 cache, {})
            self.assertGreater(share, 0.0)
            self.assertLess(share, 1.0)
            return [u.tobytes(), u2.tobytes(), keep["lat"].tobytes(), keep["lon"].tobytes()]
        got = on_threads(chunked)
        self.assertTrue(all(g == got[0] for g in got[1:]))

        # the land cover's classes on a block (the slope's neighbours across the threads' parts, lawns, sand)
        m = 600
        classes = rng.choice(np.array([10, 20, 30, 40, 50, 60, 80, 90], np.uint8), (m, m))
        classes[:, :200] = 80
        u = np.cumsum(rng.normal(0, 0.6, (m, m)), axis=1)
        lat2, lon2 = np.meshgrid(np.linspace(40.70, 40.71, m), np.linspace(-74.0, -73.99, m), indexing="ij")
        got = on_threads(lambda: surface.classify(classes, u, 1.0, lat2, lon2).tobytes())
        self.assertTrue(all(g == got[0] for g in got[1:]))

        # the sea's cell lookup (the floor's bilinear and the near test), a cell's points among many others'
        rn, b, c = 1026, seacarve.RAW_BLOCK_PX, seacarve.CELL_PX
        jj, ii = np.mgrid[0:rn, 0:rn].astype(np.float32)
        r = seacarve._in_blocks(((ii * 0.25 + jj * 0.5).astype(np.float32), np.full((rn, rn), 30.0, np.float32),
                                 np.zeros((rn, rn), bool)))
        kx, ky = 70, 90
        mx = kx * c + rng.random(n) * (rn + 40) - 20
        my = ky * c + rng.random(n) * (rn + 40) - 20
        cxs = np.where(rng.random(n) < 0.7, kx, kx + 1).astype(np.int64)
        cys = np.full(n, ky, np.int64)

        def lookup():
            d, lv = np.full(n, -1, np.float32), np.full(n, -1, np.float32)
            ok = seacarve._lookup(r, (mx, my, cxs, cys), kx, ky, (d, lv), False)
            return ok, d.tobytes(), lv.tobytes()
        got = on_threads(lookup)
        self.assertTrue(got[0][0])
        self.assertTrue(all(g == got[0] for g in got[1:]))
        sea = seacarve._in_blocks(((ii * 0.25).astype(np.float32),
                                   np.where(ii > 1000, np.float32(seacarve.SEA_E), np.float32(30.0)).astype(np.float32),
                                   np.zeros((rn, rn), bool)))
        got = on_threads(lambda: seacarve._lookup(sea, (mx, my, cxs, cys), kx, ky,
                                                   (np.zeros(n, np.float32), np.zeros(n, np.float32)), True))
        self.assertEqual(got, [False] * len(THREADS), "a point by the sea's level found on any number of threads")

    def test_mips_and_encoders(self):
        img = photo_like(np.random.default_rng(1), 1024)
        lut, sh = imagery._TO_LIN, float(imagery.MIP_SHARPEN)
        for name, fn in (
                ("mip_levels", lambda: [a.tobytes() for a in EF.mip_levels(img, lut, sh)]),
                ("mip_levels_fast", lambda: [a.tobytes() for a in EF.mip_levels_fast(img, lut, sh)]),
                ("bc1_encode_fast", lambda: EF.bc1_encode_fast(img)),
                ("bc7_encode", lambda: EF.bc7_encode(img)),
                ("dds_levels", lambda: EF.dds_levels(img, lut, sh, False)[:2])):
            got = on_threads(fn)
            for g in got[1:]:
                self.assertEqual(g, got[0], name)

    def test_integer_mips_look(self):
        # within one sRGB step of the float mips on every texel, nearly all the same
        lut, sh = imagery._TO_LIN, float(imagery.MIP_SHARPEN)
        for n, seed in ((512, 2), (64, 3), (4, 4)):
            img = photo_like(np.random.default_rng(seed), n)
            ref = EF.mip_levels(img, lut, sh)
            got = EF.mip_levels_fast(img, lut, sh)
            self.assertEqual([a.shape for a in ref], [a.shape for a in got])
            for a, b in zip(ref, got):
                d = np.abs(a.astype(int) - b.astype(int))
                self.assertLessEqual(int(d.max()), 1)
                self.assertLess(float((d > 0).mean()), 0.01)

    def test_bc1_look(self):
        # ISPC's BC1 at our encoder's quality, opaque (4-colour or 3-colour without its transparent index)
        img = photo_like(np.random.default_rng(9), 256)
        flat = np.full((16, 16, 3), (90, 140, 60), np.uint8)
        odd = np.ascontiguousarray(img[:37, :23])
        for im in (img, flat, odd):
            h, w = im.shape[:2]
            ref, _ = bc1_decode(EF.bc1_encode(im), h, w)
            new, holes = bc1_decode(EF.bc1_encode_fast(im), h, w)
            self.assertEqual(holes, 0)
            self.assertGreater(psnr(im, new), psnr(im, ref) - 0.3)
        if EF.bc1_ispc_linked():
            self.assertNotEqual(EF.bc1_encode_fast(img), EF.bc1_encode(img), "ISPC's kernels, not bc1_block")

    def test_encode_dds_levels(self):
        img = photo_like(np.random.default_rng(6), 128)
        data = imagery.encode_dds(img)
        self.assertEqual(len(data), imagery.DDS_HEADER + sum(imagery.dxt1_size(128 >> k, 128 >> k) for k in range(8)))
        # each level decodes to the integer mips' picture (BC1's error only: about our own encoder's)
        off = imagery.DDS_HEADER
        levels = [img] + list(EF.mip_levels_fast(img, imagery._TO_LIN, float(imagery.MIP_SHARPEN)))
        for lv in levels[:5]:
            h, w = lv.shape[:2]
            n = imagery.dxt1_size(w, h)
            dec, holes = bc1_decode(data[off:off + n], h, w)
            off += n
            self.assertEqual(holes, 0)
            ref, _ = bc1_decode(EF.bc1_encode(lv), h, w)
            self.assertGreater(psnr(lv, dec), psnr(lv, ref) - 0.5)


if __name__ == "__main__":
    unittest.main()
