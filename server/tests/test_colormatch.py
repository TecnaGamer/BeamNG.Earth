"""server/colormatch.py (docs/research/2026-10-10-imagery-colormatch.md §7 tests 1-9): the cut blur,
seam detection, the sub-pixel line, the feather end to end on a synthetic world (textured ground x
a flight-seam gain step, Sentinel-2 without the step), per-tile application against the whole
region, the no-seam identity, --colormatch off byte-identical, the disk cache, determinism across
region orders, the order of operations with --deshadow (§6), the per-tile cost and the server's
"rendered raw, textured again once the region is ready" bookkeeping. No network.

The synthetic world's regions are z14 tiles (ColorMatch(region_z=14), 1.95 km) so a detection
takes seconds; the feather field at the production scale (z12 regions) is built from seams laid
out directly (FieldScaleTest)."""
import asyncio
import hashlib
import math
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import colormatch as cm  # noqa: E402
import fast  # noqa: E402
import deshadow as ds  # noqa: E402
import earthd  # noqa: E402
import farfield as ff  # noqa: E402
import geodesy  # noqa: E402
import imagery  # noqa: E402

RZ = 14                                 # test regions: z14 tiles
LAT0, LON0 = 37.31, -103.87             # an arbitrary place for the synthetic world
GAIN = np.array([1.40, 1.32, 1.22], np.float32)     # the flight on the +n side of the seam
THETA = math.radians(20.0)              # seam direction (image x right, y down)
NX, NY = -math.sin(THETA), math.cos(THETA)          # its normal
TINT = np.array([1.15, 1.0, 0.85], np.float32)      # S2's own palette (never imported)
U = imagery.SOURCES["usgs"]


# --- the synthetic world --------------------------------------------------------------------
_M32 = np.uint64(0xFFFFFFFF)


def _hash(ix, iy, seed):
    """Deterministic [0, 1) per integer lattice point."""
    h = (ix.astype(np.uint64) * np.uint64(0x9E3779B1) + iy.astype(np.uint64) * np.uint64(0x85EBCA77)
         + np.uint64(seed * 0x27D4EB2F)) & _M32
    for sh, mul in ((15, 0x2C1B3C6D), (12, 0x297A2D39)):
        h ^= h >> np.uint64(sh)
        h = (h * np.uint64(mul)) & _M32
    h ^= h >> np.uint64(15)
    return (h & np.uint64(0xFFFFFF)).astype(np.float32) / np.float32(16777216.0)


def _vnoise(gx, gy, cell, seed):
    """Value noise (smoothstep-bilinear) at pixel centres gx [w], gy [h]: [h, w] in [0, 1)."""
    fx, fy = gx / cell, gy / cell
    ix, iy = np.floor(fx).astype(np.int64), np.floor(fy).astype(np.int64)
    tx, ty = fx - ix, fy - iy
    tx, ty = (tx * tx * (3 - 2 * tx))[None, :], (ty * ty * (3 - 2 * ty))[:, None]
    a, b = _hash(ix[None, :], iy[:, None], seed), _hash(ix[None, :] + 1, iy[:, None], seed)
    c, d = _hash(ix[None, :], iy[:, None] + 1, seed), _hash(ix[None, :] + 1, iy[:, None] + 1, seed)
    return ((a * (1 - tx) + b * tx) * (1 - ty) + (c * (1 - tx) + d * tx) * ty).astype(np.float32)


def albedo(gx0, gy0, n, h=None):
    """Linear RGB ground (n wide, h (default n) high, z16 px from global z16 pixel (gx0, gy0)):
    fields and scrub at several scales, per-pixel noise."""
    gx = gx0 + np.arange(n) + 0.5
    gy = gy0 + np.arange(n if h is None else h) + 0.5
    f = (0.9 * (_vnoise(gx, gy, 96, 1) - 0.5) + 0.6 * (_vnoise(gx, gy, 24, 2) - 0.5)
         + 0.4 * (_vnoise(gx, gy, 6, 3) - 0.5))
    ix, iy = np.floor(gx).astype(np.int64), np.floor(gy).astype(np.int64)
    f += 0.3 * (_hash(ix[None, :], iy[:, None], 9) - 0.5)
    ch = np.stack([0.25 * (_vnoise(gx, gy, 48, 4 + c) - 0.5) for c in range(3)], -1)
    return (np.array([0.16, 0.11, 0.07], np.float32) * np.exp(f[..., None] + ch)).astype(np.float32)


class World:
    """USGS with a flight seam (a straight line through P with a 2 px blend, GAIN on its +n side)
    over the z14 tiles [X0, X0 + 2) x [Y0, Y0 + 2) only; Sentinel-2 = the same ground on the z14
    grid in its own palette, with texture of its own and no seam."""

    def __init__(self, seam=True):
        x, y = imagery.mercator_px(LAT0, LON0, 14)
        self.X0, self.Y0 = int(x // 256) - 1, int(y // 256) - 1
        self.P = ((self.X0 + 1) * 1024.0, (self.Y0 + 1) * 1024.0)       # z16 px: the box centre
        self.seam = seam
        self._blocks = {}
        self._lock = threading.Lock()

    def covered(self, z, x, y):
        k = 2 ** (z - 14)
        return self.X0 <= x // k < self.X0 + 2 and self.Y0 <= y // k < self.Y0 + 2

    def d(self, gx, gy):
        """Signed distance (z16 px) of pixel centres from the seam line."""
        return (gx - self.P[0]) * NX + (gy - self.P[1]) * NY

    def block(self, x14, y14):
        """(ground, USGS) linear z16 px of a z14 tile, 1024²."""
        with self._lock:
            b = self._blocks.get((x14, y14))
        if b is None:
            A = albedo(x14 * 1024, y14 * 1024, 1024)
            Uu = A
            if self.seam:
                gx = x14 * 1024 + np.arange(1024) + 0.5
                gy = y14 * 1024 + np.arange(1024) + 0.5
                a = np.clip(0.5 + self.d(gx[None, :], gy[:, None]) / 2, 0, 1)[..., None]
                Uu = A * (1 + a * (GAIN - 1))
            b = (A, Uu.astype(np.float32))
            with self._lock:
                self._blocks[(x14, y14)] = b
        return b

    def usgs(self, z, x, y):
        k = 2 ** (z - 14)
        A, Uu = self.block(x // k, y // k)
        f = 4 // k
        c, r = (x % k) * 256 * f, (y % k) * 256 * f
        a = Uu[r:r + 256 * f, c:c + 256 * f]
        return ds.to_srgb(ds.down(a, f) if f > 1 else a)

    def s2(self, x, y):
        A, _ = self.block(x, y)
        gx = x * 256 + np.arange(256) + 0.5
        gy = y * 256 + np.arange(256) + 0.5
        own = np.exp(0.2 * (_vnoise(gx, gy, 4, 11) - 0.5))[..., None]
        return ds.to_srgb(ds.down(A, 4) * TINT * own)


def noise_tile(*seed):
    return np.random.default_rng(list(seed)).integers(30, 200, (256, 256, 3), dtype=np.uint8)


class WorldImagery(imagery.Imagery):
    """The world's tiles (USGS z14..16 inside its box, EOX 2023 z14 there), seeded noise for the
    rest (so the coverage probes count it as covered). No disk, no network."""

    def __init__(self, cache_dir, world, **kw):
        super().__init__(cache_dir, ("usgs", "eox2023", "eox2016"), **kw)
        self.world = world
        self.loads = {}

    def _load(self, key):
        src, z, x, y = key
        with self._lock:
            self.loads[(src.name, z)] = self.loads.get((src.name, z), 0) + 1
        if src.name == "usgs":
            if z < 14:
                return imagery.as_tile(noise_tile(z, x, y))
            return imagery.as_tile(self.world.usgs(z, x, y)) if self.world.covered(z, x, y) else None
        if src.name == "eox2023" and z == 14 and self.world.covered(z, x, y):
            return imagery.as_tile(self.world.s2(x, y))
        return imagery.as_tile(noise_tile(z, x, y, len(src.name)))


def regions(world):
    return [(world.X0 + i, world.Y0 + j) for j in range(2) for i in range(2)]


def compute(cache, world, order, **kw):
    """A ColorMatch on cache with the world's fields computed in `order` (region keys)."""
    c = cm.ColorMatch(cache, "s2", region_z=RZ)
    im = WorldImagery(os.path.join(cache, "imagery"), world, colormatch=c, **kw)
    for rk in order:
        c.wait(rk, 600)
    return c, im


def z16_tiles(world):
    return [(U, 16, x, y) for y in range(world.Y0 * 4, world.Y0 * 4 + 8) for x in range(world.X0 * 4, world.X0 * 4 + 8)]


def corrected(c, im, keys):
    """{key: uint8 tile after the correction} through ColorMatch.apply (raw where unchanged)."""
    got = im.tiles(keys)
    out = {}
    for k in keys:
        r, _ = c.apply(k, got[k].rgb, im)
        out[k] = got[k].rgb if r is None else r
    return out


def mosaic(tiles, world):
    out = np.zeros((2048, 2048, 3), np.uint8)
    for (_, _, x, y), a in tiles.items():
        r, c = (y - world.Y0 * 4) * 256, (x - world.X0 * 4) * 256
        out[r:r + 256, c:c + 256] = a
    return out


def whole_region(F, img, rk, world):
    """(region rk of the z16 mosaic img (the world's 2048² box) corrected at once by its Field F,
    pixels with neighbours on every side): the reference of the per-tile path."""
    M = cm.MARGIN
    x0, y0 = (rk[0] - world.X0) * 1024, (rk[1] - world.Y0) * 1024
    canvas = np.pad(img, ((M, M), (M, M), (0, 0)), mode="edge")[y0:y0 + 1024 + 2 * M, x0:x0 + 1024 + 2 * M]
    core = canvas[M:-M, M:-M]
    g = F.gain[cm.PADC - 1:cm.PADC + 64 + 1, cm.PADC - 1:cm.PADC + 64 + 1]
    G = cm.upsample(g, 16).copy()
    sc = F.sid[F.pb:F.pb + 256, F.pb:F.pb + 256]
    by, bx = np.nonzero(sc >= 0)
    strip = F._band(G, (by, bx, sc[by, bx]), 4.0, 0, 0, 16)
    x = ds.to_lin(core) * G
    if strip is not None:
        F._inpaint(x, core, strip, 4.0, lambda: canvas)
    ref = cm._LUT16[np.minimum(x * 65535 + 0.5, 65535).astype(np.uint16)]
    same = (G == 1).all(-1)
    if strip is not None:
        same[strip[0], strip[1]] = False
    ref[same] = core[same]
    inside = np.zeros((2048, 2048), bool)          # the box's outer edge: no neighbours to sample
    inside[M:-M, M:-M] = True
    return ref, inside[y0:y0 + 1024, x0:x0 + 1024]


def lonlat(world, gx, gy):
    lon, lat = ds.lonlat_of_px(gx, gy, 16)
    return float(lat), float(lon)


_RUN = {}


def world_run():
    """The seam world computed once per test run (regions in sorted order): (tmp dir, cache, world,
    ColorMatch, imagery, raw z16 tiles, corrected z16 tiles, seconds)."""
    if "run" not in _RUN:
        tmp = tempfile.TemporaryDirectory()
        world = World()
        cache = os.path.join(tmp.name, "a")
        t0 = time.perf_counter()
        c, im = compute(cache, world, regions(world), tone=0.0)
        dt = time.perf_counter() - t0
        keys = z16_tiles(world)
        raw = {k: t.rgb for k, t in im.tiles(keys).items()}
        _RUN["run"] = (tmp, cache, world, c, im, raw, corrected(c, im, keys), dt)
    return _RUN["run"]


def tearDownModule():
    if "run" in _RUN:
        _RUN["run"][4].close()
        _RUN.pop("run")[0].cleanup()


# --- 1. the cut blur -------------------------------------------------------------------------
class DtBlurTest(unittest.TestCase):
    def test_constants_kept_and_cuts_block(self):
        X = np.full((64, 80, 2), 0.37, np.float32)
        cut = np.zeros((64, 80), bool)
        cut[:, 40] = True
        for c in (None, cut):
            np.testing.assert_allclose(cm.dt_blur(X, 7.0, c), 0.37, rtol=1e-5)
        imp = np.zeros((64, 80, 1), np.float32)
        imp[32, 30] = 1.0
        B = cm.dt_blur(imp, 12.0, cut)
        self.assertTrue((B[:, 41:] == 0).all())         # exactly nothing beyond the cut
        self.assertTrue((B[:, 40] == 0).all())          # nor on the cut cells themselves
        self.assertGreater(B[32, 39], 0)
        self.assertAlmostEqual(float(B.sum()), float(cm.dt_blur(imp, 12.0, cut).sum()))
        free = cm.dt_blur(imp, 12.0)
        self.assertGreater(free[32, 50, 0], 0)          # without the cut it spreads across

    def test_wide_blur_is_smooth_and_symmetric(self):
        imp = np.zeros((101, 101, 1), np.float32)
        imp[50, 50] = 1.0
        B = cm.dt_blur(imp, 10.0)[..., 0]
        inner = B[20:-20, 20:-20]                       # the recursive filter's ends are not symmetric
        np.testing.assert_allclose(inner, inner[::-1, ::-1], atol=1e-6)
        np.testing.assert_allclose(inner, inner.T, atol=1e-6)
        self.assertGreater(B[50, 60], 0.3 * B[50, 50])  # about sigma = 10 wide
        self.assertLess(B[50, 80], 0.05 * B[50, 50])


# --- 2. seam detection -----------------------------------------------------------------------
DET_M = 2 * 7.6                             # detection grid (15 m)
DN = 176                                    # detection window (2.7 km)


def det_params():
    return dict(seg=max(4, int(round(cm.SEG_M / DET_M))), depth=max(2, int(round(cm.DEPTH_M / DET_M))),
                min_len=int(round(cm.MIN_LEN_M / DET_M)))


def det_texture(seed, n=DN):
    """Log linear ground on the detection grid: correlated texture, ~0.3 log std."""
    rng = np.random.default_rng(seed)
    f = sum(ds.box(rng.standard_normal((n, n)).astype(np.float32), s) * s for s in (1, 3, 9))
    f = 0.3 * (f - f.mean()) / f.std()
    tint = np.log(np.array([0.16, 0.11, 0.07], np.float32))
    return f[..., None] + tint + 0.04 * rng.standard_normal((n, n, 3)).astype(np.float32)


class DetectTest(unittest.TestCase):
    D = np.log(GAIN)                        # the step vector of the synthetic flight seam

    def detect(self, Lu, Ls):
        return cm.detect_seams(Lu, Ls, np.ones(Lu.shape[:2], np.float32), **det_params())

    def test_step_found_at_every_angle(self):
        T = det_texture(1)
        S = T + 0.08 * np.random.default_rng(2).standard_normal(T.shape).astype(np.float32)   # S2's own noise
        yy, xx = np.mgrid[0:DN, 0:DN] + 0.5
        for deg in range(0, 180, 15):
            a = math.radians(deg)
            nx, ny = -math.sin(a), math.cos(a)      # the normal of a line at deg
            d = (xx - DN / 2) * nx + (yy - DN / 2) * ny
            Lu = T + (d > 0)[..., None] * self.D
            seam, ang, D, score, stats = self.detect(Lu, S)
            on = seam & (np.abs(d) < 1.5) & (np.hypot(xx - DN / 2, yy - DN / 2) < DN / 4)
            self.assertGreater(on.sum(), DN / 4, deg)
            found = ang[on]
            err = np.degrees(np.abs((found - a + np.pi / 2) % np.pi - np.pi / 2))
            self.assertLess(float(np.median(err)), 5.0, deg)
            step = float(np.median(np.linalg.norm(D[on], axis=-1)))   # the measured step
            self.assertLess(abs(step - np.linalg.norm(self.D)), 0.08 * np.linalg.norm(self.D), (deg, step))
            sc = float(np.median(score[on]))        # |step| x the S2 gate (S2's texture across the line)
            self.assertTrue(0.6 * step < sc <= step + 1e-6, (deg, sc, step))
            self.assertFalse((seam & (np.abs(d) > 4)).any(), deg)     # nothing off the line

    @unittest.skipUnless(fast.gpu(), "no GPU here (earthfast.gpu_name)")
    def test_gpu_matches_cpu(self):
        """The GPU's angle loop (earthfast.seam_detect_gpu, float32) finds the same seam as the CPU path (float64
        sums), with the same angles and steps to float32 precision."""
        T = det_texture(1)
        S = T + 0.08 * np.random.default_rng(2).standard_normal(T.shape).astype(np.float32)
        yy, xx = np.mgrid[0:DN, 0:DN] + 0.5
        for deg in (0, 35, 90, 140):
            a = math.radians(deg)
            d = (xx - DN / 2) * -math.sin(a) + (yy - DN / 2) * math.cos(a)
            Lu = T + (d > 0)[..., None] * self.D
            seam_g, ang_g, D_g, score_g, _ = self.detect(Lu, S)
            with mock.patch.object(cm, "_seams_gpu", lambda *a: None):
                seam_c, ang_c, D_c, score_c, _ = self.detect(Lu, S)
            np.testing.assert_array_equal(seam_g, seam_c, deg)
            # (float32 vs float64 sums: a median or the agreement test flips on a few borderline cells)
            self.assertLess(float((np.abs(score_g - score_c) > 1e-3).mean()), 1e-3, deg)
            np.testing.assert_allclose(ang_g[seam_c], ang_c[seam_c], atol=1e-6, err_msg=str(deg))
            np.testing.assert_allclose(D_g[seam_c], D_c[seam_c], atol=1e-4, err_msg=str(deg))

    def test_noise_natural_edges_and_patches_rejected(self):
        T = det_texture(3)
        S = T + 0.08 * np.random.default_rng(4).standard_normal(T.shape).astype(np.float32)
        self.assertFalse(self.detect(T, S)[0].any())                   # pure texture
        yy, xx = np.mgrid[0:DN, 0:DN] + 0.5
        edge = (yy > DN / 2)[..., None] * np.array([0.5, 0.45, 0.3], np.float32)
        self.assertFalse(self.detect(T + edge, S + 0.8 * edge)[0].any())    # a natural edge, in both images
        side = int(round(600 / DET_M))
        patch = ((np.abs(xx - DN / 2) < side / 2) & (np.abs(yy - DN / 2) < side / 2))[..., None] * self.D
        self.assertFalse(self.detect(T + patch, S)[0].any())          # 600 m crop change in USGS only

    def test_step_in_s2_only_rejected(self):
        """A straight step in S2 only (an EOX compositing seam, or the edge of the per-tile 2016
        fallback) is not a survey seam: correcting it would import S2's step into USGS (§5). The
        USGS / S2 ratio has the same step as for a flight seam and the S2 gate lets |D_s2| ~ |D|
        through, so detect_region drops the components whose step USGS does not carry (before:
        detected along the whole line). Below about twice this texture's band-step noise (0.15 log
        per 240 m segment) the two cannot be told apart; in the real Monument Valley window a 20 %
        step in S2 only was detected before and is rejected now, the AZ/UT seam kept."""
        T = det_texture(1)
        S = T + 0.08 * np.random.default_rng(2).standard_normal(T.shape).astype(np.float32)
        n = 2 * DN                                  # detect_region's cells: half the detection grid
        up = lambda a: np.exp(np.repeat(np.repeat(a, 2, 0), 2, 1)).astype(np.float32)   # noqa: E731
        yy, xx = np.mgrid[0:DN, 0:DN] + 0.5

        def seams(Lu, Ls):
            return cm.detect_region(up(Lu), up(Ls), np.ones((n, n), bool), DET_M / 2, (0, n, 0, n))
        for mag in (0.3, 0.6, 1.0):
            for deg in (0, 35, 90):
                a = math.radians(deg)
                d = (xx - DN / 2) * -math.sin(a) + (yy - DN / 2) * math.cos(a)
                step = (d > 0)[..., None] * np.array([mag, 0.9 * mag, 0.8 * mag], np.float32)
                self.assertEqual(len(seams(T, S + step)["y"]), 0, (mag, deg))
                self.assertEqual(len(seams(T, S - step)["y"]), 0, (mag, deg))
                s = seams(T + step, S)              # the same step in USGS: kept
                self.assertGreater(len(s["y"]), n / 2, (mag, deg))
                self.assertTrue(all(st["share"] >= cm.MIN_SHARE for st in s["stats"]), s["stats"])


# --- 3. the sub-pixel line -------------------------------------------------------------------
class SeamLineTest(unittest.TestCase):
    def check(self, deg, y0):
        """A 2 px blended step along a line at deg through (x 256, y0) of a 512² z16 image."""
        a = math.radians(deg)
        nx, ny = -math.sin(a), math.cos(a)
        img = albedo(0, 0, 512)
        yy, xx = np.mgrid[0:512, 0:512].astype(np.float64)
        d = (yy - y0) * ny + (xx - 256) * nx                        # pixel index coordinates
        lin = img * (1 + np.clip(0.5 + d / 2, 0, 1)[..., None] * (GAIN - 1))
        lg = np.log(ds.to_lin(ds.to_srgb(lin)) + cm.EPS)

        def fine(py, px):
            ok = (py >= 0) & (py <= 511) & (px >= 0) & (px <= 511)
            v = cm.bilinear(lg, py, px)
            v[~ok] = np.nan
            return v
        cy, cx = np.mgrid[0:128, 0:128]
        dc = ((cy * 4 + 1.5) - y0) * ny + ((cx * 4 + 1.5) - 256) * nx
        sel = (np.abs(dc) <= 4) & (np.abs(cx - 64) < 40) & (np.abs(cy - 64) < 40)
        ys, xs = np.nonzero(sel)
        ang = np.full(len(ys), a, np.float32)
        Dv = np.tile(np.log(GAIN), (len(ys), 1)).astype(np.float32)
        off = cm.seam_lines(ys, xs, ang, Dv, fine)
        qy = ys * 4 + 1.5 + off * 4 * ny
        qx = xs * 4 + 1.5 + off * 4 * nx
        err = (qy - y0) * ny + (qx - 256) * nx                     # line point's distance from the truth
        inner = (np.abs(cx[sel] - 64) < 28) & (np.abs(cy[sel] - 64) < 28)
        self.assertLess(float(np.abs(err[inner]).max()), 0.3, (deg, y0))      # 0.2 measured

    def test_fractional_rows_and_slants(self):
        for deg, y0 in ((0, 255.3), (0, 256.75), (17, 254.4), (-35, 257.1), (60, 255.6), (90, 256.0)):
            self.check(deg, y0)


# --- 4.-6., 8. the ColorMatch on the synthetic world ------------------------------------------
class WorldTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.cache, cls.world, cls.c, cls.im, cls.raw, cls.out, cls.seconds = world_run()
        w = cls.world
        gx = w.X0 * 1024 + np.arange(2048) + 0.5
        gy = w.Y0 * 1024 + np.arange(2048) + 0.5
        cls.d = w.d(gx[None, :], gy[:, None])
        cls.A = np.concatenate([np.concatenate([w.block(w.X0 + i, w.Y0 + j)[0] for i in range(2)], 1)
                                for j in range(2)], 0)
        cls.before, cls.after = mosaic(cls.raw, w), mosaic(cls.out, w)

    def test_regions_have_the_seam(self):
        for rk in regions(self.world):
            F = self.c.field(rk, enqueue=False)
            self.assertIsInstance(F, cm.Field, rk)
            self.assertTrue(F.has_band, rk)
        print(f"\n4 synthetic regions (z14) in {self.seconds:.1f} s; {self.c.summary()}")

    def band_step(self, img, lo, hi):
        """Step across the seam (+n side / -n side) of img / ground in the bands lo < |d| < hi px,
        away from the box's edges and the line's ends (per channel, fraction)."""
        R = ds.to_lin(img) / np.maximum(self.A, 1e-4)
        yy, xx = np.mgrid[0:2048, 0:2048]
        inner = (np.abs(xx - 1024) < 700) & (np.abs(yy - 1024) < 700)
        a = inner & (self.d > lo) & (self.d < hi)
        b = inner & (self.d < -lo) & (self.d > -hi)
        return R[a].mean(0) / R[b].mean(0) - 1

    def test_feather_removes_the_step(self):
        """§7 test 4: a +40 % flight seam over textured ground, S2 without it: residual step < 2 %
        in 20 m bands (and 200 m), texture kept (log-HF std within 1 %), S2's palette not imported."""
        before = self.band_step(self.before, 2, 12)
        np.testing.assert_allclose(before, GAIN - 1, atol=0.01)
        after = self.band_step(self.after, 2, 12)
        self.assertLess(float(np.abs(after).max()), 0.02, after)
        self.assertLess(float(np.abs(self.band_step(self.after, 5, 105)).max()), 0.02)

        def hf(img):
            g = np.log(ds.luma(ds.to_lin(img)) + 0.004)
            return (g - ds.box(g, 4))[300:1750, 300:1750]
        far = np.abs(self.d)[300:1750, 300:1750] > 4
        self.assertLess(abs(hf(self.after)[far].std() / hf(self.before)[far].std() - 1), 0.01)
        # each side moves half way: the corrected ground is the geometric mean of the two flights
        R = ds.to_lin(self.after) / np.maximum(self.A, 1e-4)
        near = (np.abs(self.d) > 3) & (np.abs(self.d) < 60) & (np.abs(np.arange(2048)[None, :] - 1024) < 500)
        np.testing.assert_allclose(np.median(R[near], 0), np.sqrt(GAIN), rtol=0.03)

    def test_blend_row_inpainted(self):
        """The mosaic's 2 px blend row is gone: the luminance profile across the line is monotone
        within the noise (no dip or bump on the line itself)."""
        L = np.log(ds.luma(ds.to_lin(self.after)) / ds.luma(self.A))
        inner = np.abs(np.arange(2048)[None, :] - 1024) < 600
        prof = [float(np.median(L[(np.abs(self.d - t) < 0.5) & inner])) for t in np.arange(-6, 7)]
        self.assertLess(max(prof) - min(prof), 0.02, prof)

    def test_gain_one_far_from_the_seam(self):
        far = np.abs(self.d) * imagery.ground_res(LAT0, 16) > cm.fade_cut(self.c.cell_m(regions(self.world)[0]), 256) + 100
        self.assertGreater(far.sum(), 1000)
        self.assertTrue((self.after[far] == self.before[far]).all())

    def test_tiles_match_the_whole_region(self):
        """§7 test 5: per-tile results (each with its neighbours as margin) == the whole region
        corrected at once, within 1 level."""
        w = self.world
        for rk in regions(w):
            ref, inside = whole_region(self.c.field(rk, enqueue=False), self.before, rk, w)
            x0, y0 = (rk[0] - w.X0) * 1024, (rk[1] - w.Y0) * 1024
            got = self.after[y0:y0 + 1024, x0:x0 + 1024]
            diff = np.abs(ref.astype(int) - got.astype(int)).max(-1)
            self.assertLessEqual(int(diff[inside].max()), 1, rk)

    def test_upsample_is_bilinear(self):
        rng = np.random.default_rng(5)
        g = rng.random((10, 10, 3)).astype(np.float32)
        for f in (2, 4, 16):
            o = cm.upsample(g, f)
            c = (np.arange(8 * f) + 0.5) / f + 0.5             # sample coordinates in g
            ref = cm.bilinear(g, c[:, None] + 0 * c[None, :], 0 * c[:, None] + c[None, :])
            np.testing.assert_allclose(o, ref, atol=1e-5)

    def test_raw_tiles_untouched(self):
        for k, t in self.im.tiles(list(self.raw)).items():
            self.assertTrue((t.rgb == self.raw[k]).all())
        files = [os.path.relpath(os.path.join(d, f), self.cache) for d, _, fs in os.walk(self.cache) for f in fs]
        fin = os.path.join("imagery", f"fin{imagery.FIN_VERSION}-")
        files = [f for f in files if not f.startswith(fin)]         # (the finished pyramid: the corrected tiles)
        for f in files:
            self.assertTrue(f.startswith(os.path.join("colormatch", f"v{cm.VERSION}", "usgs", str(RZ)) + os.sep), f)
        base = os.path.join("colormatch", f"v{cm.VERSION}", "usgs", str(RZ))
        with_seams = 0
        for rk in regions(self.world):          # the line crosses two of them, the fade reaches all four
            self.assertIn(os.path.join(base, str(rk[0]), f"{rk[1]}.npz"), files)
            with_seams += os.path.join(base, str(rk[0]), f"{rk[1]}.seams.npz") in files
        self.assertEqual(with_seams, 2)
        self.assertTrue(all(f.endswith((".npz", ".none")) for f in files))

    def test_other_sources_and_coarser_zooms_untouched(self):
        n = np.zeros((256, 256, 3), np.uint8) + 90
        e = (imagery.SOURCES["eox2023"], 16, self.world.X0 * 4, self.world.Y0 * 4)
        self.assertEqual(self.c.apply(e, n, self.im), (None, True))
        self.assertEqual(self.c.apply((U, 13, self.world.X0 // 2, self.world.Y0 // 2), n, self.im), (None, True))

    def test_render_and_pending(self):
        """Imagery.render: the corrected texture; with the region not ready, the raw texture and the
        region reported in pending."""
        w = self.world
        frame = geodesy.EnuFrame(*lonlat(w, *w.P))
        sq = imagery.gutter_square(-128.0, -128.0, 256.0, 256)
        got, used = self.im.render(frame, *sq, 256, pending=(p := set()))
        self.assertEqual(p, set())
        # the corrected share (data credits: its S2 references are named for it)
        self.assertEqual(used.get("colormatch"), used["usgs"])
        raw_im = WorldImagery(os.path.join(self.tmp.name, "raw"), w, tone=0.0)
        raw, _ = raw_im.render(frame, *sq, 256)
        raw_im.close()
        self.assertGreater(np.abs(got.astype(int) - raw.astype(int)).mean(), 3)
        gate = threading.Event()

        class Held(cm.ColorMatch):
            def _compute_field(self, rk):
                gate.wait(60)
                if self._closed:
                    raise RuntimeError("closed")
                return super()._compute_field(rk)
        root = tempfile.mkdtemp(dir=self.tmp.name)
        c = Held(root, "s2", region_z=RZ)
        im = WorldImagery(os.path.join(root, "imagery"), w, colormatch=c, tone=0.0)
        c.prepare(*lonlat(w, *w.P))
        rgb, held = im.render(frame, *sq, 256, pending=(p := set()))
        im.close()
        gate.set()
        self.assertNotIn("colormatch", held)                   # left raw: nothing corrected
        self.assertTrue((rgb == raw).all())
        self.assertTrue(p and p <= set(regions(w)), p)
        self.assertGreater(c.stats.get("pending", 0), 0)


class NoSeamTest(unittest.TestCase):
    def test_identity(self):
        """§7 test 6: no seam within reach: the field is EMPTY (a marker on disk), apply returns
        the input unchanged and the texture is byte-identical to the one without colour matching."""
        with tempfile.TemporaryDirectory() as tmp:
            w = World(seam=False)
            c, im = compute(os.path.join(tmp, "c"), w, regions(w)[:1], tone=1.0)
            rk = regions(w)[0]
            self.assertIs(c.field(rk, enqueue=False), cm.EMPTY)
            self.assertTrue(os.path.exists(os.path.join(c.dir, str(rk[0]), f"{rk[1]}.none")))
            key = (U, 16, rk[0] * 4 + 1, rk[1] * 4 + 2)
            t = im.tiles([key])[key]
            t0 = time.perf_counter()
            self.assertEqual(c.apply(key, t.rgb, im), (None, True))
            self.assertLess(time.perf_counter() - t0, 0.005)
            frame = geodesy.EnuFrame(*lonlat(w, *w.P))
            got, used = im.render(frame, *imagery.gutter_square(-300.0, -300.0, 600.0, 256), 256)
            im.close()
            plain = WorldImagery(os.path.join(tmp, "p"), w, tone=1.0)
            want, plain_used = plain.render(frame, *imagery.gutter_square(-300.0, -300.0, 600.0, 256), 256)
            plain.close()
            self.assertTrue((got == want).all())
            self.assertEqual(used, plain_used)                  # nothing corrected: the same counts
            F = cm.Field(256, np.zeros((68, 68, 3), np.float32))
            self.assertIsNone(F.apply(t.rgb, 16, key[2], key[3], RZ))


class FallbackImagery(WorldImagery):
    """S2 2023 missing on the world's east column of z14 tiles: the per-tile fallback takes S2 2016
    there, in a palette of its own (a straight step in S2 only, along the tile edge)."""
    TINT16 = np.array([0.8, 0.9, 1.35], np.float32)

    def _load(self, key):
        src, z, x, y = key
        if z == 14 and src.name in cm.REFS and self.world.covered(z, x, y) and x == self.world.X0 + 1:
            with self._lock:
                self.loads[(src.name, z)] = self.loads.get((src.name, z), 0) + 1
            if src.name == "eox2023":
                return None
            return imagery.as_tile(ds.to_srgb(np.clip(ds.to_lin(self.world.s2(x, y)) * self.TINT16, 0, 1)))
        return super()._load(key)


class S2OnlyStepTest(unittest.TestCase):
    def test_fallback_edge_not_imported(self):
        """The edge between S2 2023 and the 2016 fallback is not a survey seam: no field, USGS
        unchanged (§5: a step in S2 only is never imported into USGS)."""
        with tempfile.TemporaryDirectory() as tmp:
            w = World(seam=False)
            c = cm.ColorMatch(os.path.join(tmp, "c"), "s2", region_z=RZ)
            im = FallbackImagery(os.path.join(tmp, "c", "imagery"), w, colormatch=c, tone=0.0)
            self.addCleanup(im.close)
            for rk in regions(w)[:2]:               # either side of the edge
                self.assertIs(c.wait(rk, 600), cm.EMPTY, rk)
            self.assertGreater(im.loads.get(("eox2016", 14), 0), 0)
            for rk in regions(w):
                s, _ = c._seams_of(rk)
                self.assertEqual(len(s["y"]), 0, rk)


class KindImagery(WorldImagery):
    """Records the netfetch kind each load ran as; `defer`: background loads fail as a dropped
    (deferred) request would, like a host whose bucket is kept for demand."""
    defer = False

    def _load(self, key):
        kind = getattr(self._local, "kind", "demand")
        with self._lock:
            self.kinds = getattr(self, "kinds", set()) | {kind}
        if kind != "demand" and (self.defer or getattr(self, "defer_n", 0) > 0):
            if not self.defer:
                with self._lock:
                    self.defer_n -= 1
            raise imagery.ImageryError("deferred (test)", deferred=True)
        return super()._load(key)


class NetfetchKindTest(unittest.TestCase):
    def test_region_fetches_are_background(self):
        """A region's detection window (USGS and the S2 references) is fetched as netfetch kind
        "background", like the de-shadow fits: never the demand budget of the textures."""
        with tempfile.TemporaryDirectory() as tmp:
            w = World(seam=False)
            c = cm.ColorMatch(os.path.join(tmp, "c"), "s2", region_z=RZ)
            im = KindImagery(os.path.join(tmp, "c", "imagery"), w, colormatch=c, tone=0.0)
            self.addCleanup(im.close)
            self.assertIs(c.wait(regions(w)[0], 600), cm.EMPTY)
            self.assertEqual(im.kinds, {"background"})

    def test_deferred_region_is_quiet_and_retried(self):
        """A region whose background fetches the limiter dropped is not ready (its tiles render raw),
        logs no warning (a cold start is no fault) and is queued again after RETRY_S."""
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cm, "PATIENT_S", 0.3):
            w = World(seam=False)
            c = cm.ColorMatch(os.path.join(tmp, "c"), "s2", region_z=RZ)
            im = KindImagery(os.path.join(tmp, "c", "imagery"), w, colormatch=c, tone=0.0)
            self.addCleanup(im.close)
            im.defer = True
            rk = regions(w)[0]
            with self.assertNoLogs("earthd.colormatch", "WARNING"):
                self.assertIsNone(c.wait(rk, 600))
            self.assertIsNone(c.status(rk))
            self.assertIn(rk, c._failed)
            im.defer = False
            c._failed[rk] -= cm.RETRY_S             # the retry time has passed
            self.assertIs(c.wait(rk, 600), cm.EMPTY)

    def test_a_few_dropped_fetches_are_waited_out(self):
        """The limiter drops a few background fetches (bucket low): the region thread waits and goes
        on, and the region is ready on its first attempt instead of failing."""
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cm, "PATIENT_STEP_S", 0.01):
            w = World(seam=False)
            c = cm.ColorMatch(os.path.join(tmp, "c"), "s2", region_z=RZ)
            im = KindImagery(os.path.join(tmp, "c", "imagery"), w, colormatch=c, tone=0.0)
            self.addCleanup(im.close)
            im.defer_n = 5
            rk = regions(w)[0]
            self.assertIs(c.wait(rk, 600), cm.EMPTY)
            self.assertNotIn(rk, c._failed)
            self.assertEqual(im.defer_n, 0)

    def test_dropped_region_is_retried_without_a_new_request(self):
        """A parked player asks for nothing new: a region dropped at the cold start must still be
        computed once RETRY_S has passed (in-game 2026-10-11 it never was)."""
        import time as _time
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cm, "RETRY_S", 0.4), \
                mock.patch.object(cm, "PATIENT_S", 0.3):
            w = World(seam=False)
            c = cm.ColorMatch(os.path.join(tmp, "c"), "s2", region_z=RZ)
            im = KindImagery(os.path.join(tmp, "c", "imagery"), w, colormatch=c, tone=0.0)
            self.addCleanup(im.close)
            im.defer = True
            rk = regions(w)[0]
            self.assertIsNone(c.wait(rk, 600))
            self.assertIn(rk, c._failed)
            im.defer = False
            deadline = _time.monotonic() + 60
            while _time.monotonic() < deadline and not c._peek(rk):
                _time.sleep(0.05)
            self.assertTrue(c._peek(rk), "the worker retried the region by itself")
            self.assertNotIn(rk, c._failed)


class LoopSideTest(unittest.TestCase):
    def test_no_field_decoded_on_the_event_loop(self):
        """prepare (anchor), request (prefetch) and status (a finished tile) run on earthd's event
        loop: they never decode a field (stat calls only); field() (tile workers) decodes it without
        holding the lock the loop takes."""
        with tempfile.TemporaryDirectory() as tmp:
            c = cm.ColorMatch(tmp, "s2", region_z=RZ)
            c.close()                               # no worker: nothing is computed
            w = World()
            rk, rk2 = regions(w)[:2]
            s = line_seams(256, 12.0, np.log(GAIN), (256 + 100.3, 256 + 75.0), 7.6)
            c._store(rk, "field", cm.build_field(s, (0, 0), (192, 192), (64, 64), 256, 7.6,
                                                 cm.fade_cut(7.6, 256)), {})
            c._store(rk2, "field", cm.EMPTY, {})
            calls = []
            load = c._load

            def spy(k, kind):
                calls.append((k, kind, c._lock._is_owned()))
                return load(k, kind)
            c._load = spy
            c.prepare(*lonlat(w, *w.P))
            c.request((U, 16, rk[0] * 4, rk[1] * 4))
            self.assertIs(c.status(rk), True)
            self.assertIs(c.status(rk2), False)
            self.assertEqual(calls, [])
            self.assertFalse({rk, rk2} & set(c._events))      # on disk: not queued
            self.assertIsInstance(c.field(rk), cm.Field)
            self.assertIs(c.field(rk2), cm.EMPTY)
            self.assertEqual(calls, [(rk, "field", False), (rk2, "field", False)])
            self.assertIs(c.status(rk), True)                  # from memory now
            self.assertEqual(len(calls), 2)


class CacheTest(unittest.TestCase):
    """§7 test 8: a new ColorMatch reads the fields from disk (no imagery for the analysis, same
    bytes); another VERSION, reference chain or region zoom is recomputed."""

    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.cache, cls.world, cls.c, cls.im, cls.raw, cls.out, _ = world_run()

    def copy(self):
        dst = tempfile.mkdtemp(dir=self.tmp.name)
        shutil.copytree(self.cache, dst, dirs_exist_ok=True)
        return dst

    def test_round_trip(self):
        root = self.copy()
        c = cm.ColorMatch(root, "s2", region_z=RZ)
        im = WorldImagery(os.path.join(root, "imagery"), self.world, colormatch=c, tone=0.0)
        keys = z16_tiles(self.world)
        out = corrected(c, im, keys)
        im.close()
        self.assertEqual(im.loads.get(("usgs", 14), 0), 0)        # no analysis
        self.assertEqual(im.loads.get(("eox2023", 14), 0), 0)
        for k in keys:
            self.assertTrue((out[k] == self.out[k]).all(), k)
        self.assertGreater(c.stats.get("corrected", 0), 0)

    def test_stale_entries_recomputed(self):
        rk = regions(self.world)[0]
        root = self.copy()
        old = cm.VERSION
        try:
            cm.VERSION = old + 1                    # a new algorithm: another directory
            c = cm.ColorMatch(root, "s2", region_z=RZ)
            self.assertIn(f"v{old + 1}", c.dir)
            self.assertIsNone(c.field(rk, enqueue=False))
            c.close()
        finally:
            cm.VERSION = old
        c = cm.ColorMatch(root, "s2", region_z=RZ)
        c.meta = dict(c.meta, refs="eox2024")       # another reference chain under the same path
        self.assertIsNone(c.field(rk, enqueue=False))
        self.assertIsNone(c._load(rk, "seams"))
        c.close()
        c = cm.ColorMatch(root, "s2", region_z=RZ)  # the right meta: read back
        self.assertIsInstance(c.field(rk, enqueue=False), cm.Field)
        c.close()
        # a stale field is computed again (from the cached seams: no detection)
        npz = os.path.join(root, "colormatch", f"v{cm.VERSION}", "usgs", str(RZ), str(rk[0]), f"{rk[1]}.npz")
        with np.load(npz) as f:
            arrays = {k: f[k] for k in f.files}
        arrays["meta"] = np.array(str(arrays["meta"]).replace('"region_z": 14', '"region_z": 13'))
        np.savez_compressed(npz, **arrays)
        c = cm.ColorMatch(root, "s2", region_z=RZ)
        im = WorldImagery(os.path.join(root, "imagery"), self.world, colormatch=c)
        F = c.wait(rk, 120)
        im.close()
        self.assertIsInstance(F, cm.Field)
        self.assertEqual(im.loads.get(("usgs", 14), 0), 0)
        with np.load(npz) as f:
            self.assertIn('"region_z": 14', str(f["meta"]))
        np.testing.assert_array_equal(F.C, self.c.field(rk, enqueue=False).C)

    def test_modes(self):
        with self.assertRaises(ValueError):
            cm.ColorMatch(self.tmp.name, "off")
        with self.assertRaises(ValueError):
            cm.ColorMatch(self.tmp.name, "abs")
        self.assertEqual(cm.MODES, ("off", "s2"))


class DeterminismTest(unittest.TestCase):
    """The same place renders the same bytes whatever order its regions were computed in."""

    def test_region_order(self):
        tmp, cache, world, c, im, raw, out, _ = world_run()
        root = tempfile.mkdtemp(dir=tmp.name)
        order = regions(world)[::-1]
        c2, im2 = compute(root, world, order[:1], tone=0.0)
        # the rest as an anchor between them would queue them (nearest first), and one by request
        c2.prepare(*lonlat(world, *world.P))
        for rk in order[1:]:
            c2.wait(rk, 600)
        for rk in regions(world):
            F1, F2 = c.field(rk, enqueue=False), c2.field(rk, enqueue=False)
            for name in ("C", "sid", "line", "cA", "cB"):
                np.testing.assert_array_equal(getattr(F1, name), getattr(F2, name), f"{rk} {name}")
        out2 = corrected(c2, im2, list(out))
        for k in out:
            self.assertTrue((out2[k] == out[k]).all(), k)
        frame = geodesy.EnuFrame(*lonlat(world, world.P[0] + 300, world.P[1] - 200))
        sq = imagery.gutter_square(-500.0, -500.0, 1000.0, 512)
        r1, _ = im.render(frame, *sq, 512)
        r2, _ = im2.render(frame, *sq, 512)
        im2.close()
        self.assertEqual(hashlib.sha256(r1.tobytes()).hexdigest(), hashlib.sha256(r2.tobytes()).hexdigest())


# --- production scale: the field from laid-out seams ----------------------------------------
def line_seams(n_cells, angle_deg, step, through, cell_m):
    """Seam cells (global cells, sorted) of a straight seam through the 3 x 3 regions around
    region (1, 1) of a grid with n_cells per region: 2 cells wide as the detector gives them,
    the line's sub-cell offset exact."""
    a = math.radians(angle_deg)
    nx, ny = -math.sin(a), math.cos(a)
    c = np.arange(3 * n_cells, dtype=np.float32) + 0.5
    d = (c[:, None] - through[0]) * ny + (c[None, :] - through[1]) * nx
    ys, xs = np.nonzero(np.abs(d) <= 1.0)
    k = len(ys)
    return dict(y=ys.astype(np.int64), x=xs.astype(np.int64), ang=np.full(k, a, np.float32),
                D=np.tile(np.float32(step), (k, 1)), step=np.tile(np.float32(step), (k, 1)),
                ok=np.ones(k, bool), off=(-d[ys, xs]).astype(np.float32))


class FieldScaleTest(unittest.TestCase):
    """The feather field of a z12 region (1024 cells of 7.6 m) built from a laid-out seam."""
    N = 1024
    CELL = 7.6
    STEP = np.log(GAIN)

    @classmethod
    def setUpClass(cls):
        n = cls.N
        cls.through = (n + 50.3, n + 300.0)         # the line passes 50 cells into the region
        s = line_seams(n, 12.0, cls.STEP, cls.through, cls.CELL)
        cut = cm.fade_cut(cls.CELL, n)
        t0 = time.perf_counter()
        cls.F = cm.build_field(s, (0, 0), (3 * n // cm.COARSE,) * 2, (n // cm.COARSE,) * 2, n, cls.CELL, cut)
        cls.build_s = time.perf_counter() - t0
        cls.cut = cut
        a = math.radians(12.0)
        cls.n = (-math.sin(a), math.cos(a))
        # the region's z16 image: ground x the flight step across the line (in row blocks)
        c = (np.arange(4 * n, dtype=np.float32) + 0.5) / 4 + n
        cls.d16 = 4 * ((c[:, None] - cls.through[0]) * cls.n[1] + (c[None, :] - cls.through[1]) * cls.n[0])
        cls.img = np.empty((4 * n, 4 * n, 3), np.float32)
        cls.raw = np.empty((4 * n, 4 * n, 3), np.uint8)
        for r in range(0, 4 * n, 512):
            cls.img[r:r + 512] = albedo(0, r, 4 * n, 512)
            a = np.clip(0.5 + cls.d16[r:r + 512] / 2, 0, 1)[..., None]
            cls.raw[r:r + 512] = ds.to_srgb(cls.img[r:r + 512] * (1 + a * (GAIN - 1)))

    def apply16(self, timing=None):
        out = np.empty_like(self.raw)
        M = cm.MARGIN
        pad = np.pad(self.raw, ((M, M), (M, M), (0, 0)), mode="edge")
        for ty in range(16):
            for tx in range(16):
                t = self.raw[ty * 256:(ty + 1) * 256, tx * 256:(tx + 1) * 256]
                mg = (lambda ty=ty, tx=tx: pad[ty * 256:ty * 256 + 256 + 2 * M, tx * 256:tx * 256 + 256 + 2 * M])
                best = math.inf
                for _ in range(3 if timing is not None else 1):
                    t0 = time.perf_counter()
                    r = self.F.apply(t, 16, tx, ty, 12, mg)
                    best = min(best, time.perf_counter() - t0)
                if timing is not None:
                    has_band = (self.F.sid[self.F.pb + ty * 64:self.F.pb + ty * 64 + 64,
                                           self.F.pb + tx * 64:self.F.pb + tx * 64 + 64] >= 0).any()
                    timing.setdefault("band" if has_band else ("same" if r is None else "smooth"), []).append(best)
                out[ty * 256:(ty + 1) * 256, tx * 256:(tx + 1) * 256] = t if r is None else r
        return out

    def test_fade_and_clamp(self):
        """§7 test 4 at the production scale: gain exactly 1 beyond 4 sigma_f, the half step next to
        the line, the clamp."""
        F = self.F
        self.assertEqual(self.cut, cm.FADE_CUTS * cm.SIGMA_F_M)
        hc = F.C.shape[0]
        sy, sx = np.mgrid[0:hc, 0:hc]
        cy = (sy - cm.PADC + 0.5) * cm.COARSE + self.N            # global cells of the samples
        cx = (sx - cm.PADC + 0.5) * cm.COARSE + self.N
        dist = np.abs((cy - self.through[0]) * self.n[1] + (cx - self.through[1]) * self.n[0]) * self.CELL
        beyond = dist > 4 * cm.SIGMA_F_M + 2 * cm.COARSE * self.CELL    # distance measured on the 30 m grid
        self.assertTrue((F.C[beyond] == 0).all())
        self.assertGreater(beyond.sum(), 1000)
        near = (dist > 60) & (dist < 120)
        np.testing.assert_allclose(np.abs(F.C[near]).mean(0), self.STEP / 2, rtol=0.1)
        self.assertLessEqual(float(np.abs(F.C).max()), cm.CLAMP + 1e-3)
        s = line_seams(256, 12.0, np.full(3, 2.5), (256 + 100.3, 256 + 75.0), self.CELL)
        G = cm.build_field(s, (0, 0), (192, 192), (64, 64), 256, self.CELL, cm.fade_cut(self.CELL, 256))
        self.assertLessEqual(float(np.abs(G.C).max()), cm.CLAMP + 1e-3)       # float16 storage
        self.assertGreater(float(np.abs(G.C).max()), cm.CLAMP - 0.01)
        self.assertLess(self.build_s, 30)

    def test_removed_at_z16_and_coarser_zooms_follow(self):
        out = self.apply16()
        R = ds.to_lin(out) / np.maximum(self.img, 1e-4)
        a = (self.d16 > 2) & (self.d16 < 12)
        b = (self.d16 < -2) & (self.d16 > -12)
        self.assertLess(float(np.abs(R[a].mean(0) / R[b].mean(0) - 1).max()), 0.02)
        lin16 = ds.to_lin(out)
        for z in (15, 14, 13, 12):
            f = 2 ** (16 - z)
            src = ds.to_srgb(ds.down(ds.to_lin(self.raw), f))
            n = 4096 // f // 256
            got = np.empty_like(src)
            for ty in range(n):
                for tx in range(n):
                    t = src[ty * 256:(ty + 1) * 256, tx * 256:(tx + 1) * 256]
                    r = self.F.apply(t, z, tx, ty, 12, None)
                    got[ty * 256:(ty + 1) * 256, tx * 256:(tx + 1) * 256] = t if r is None else r
            want = ds.to_srgb(ds.down(lin16, f))
            diff = np.abs(got.astype(int) - want.astype(int))
            off_line = ds.down((np.abs(self.d16) > 8).astype(np.float32), f) == 1
            self.assertLess(float(diff[off_line].mean()), 0.6, z)
            self.assertLessEqual(int(np.percentile(diff[off_line], 99)), 3, z)

    def test_per_tile_cost(self):
        """§7 test 9 (loose): < 10 ms per 256 px tile (best of three), unchanged tiles ~0."""
        t = {}
        self.apply16(t)
        ms = {k: 1000 * float(np.max(v)) for k, v in t.items()}
        med = {k: round(1000 * float(np.median(v)), 2) for k, v in t.items()}
        print(f"\nper-tile ms at z16 (median): {med}, max {ms}")
        self.assertLess(ms["band"], 10.0)
        self.assertLess(ms.get("smooth", 0), 10.0)
        self.assertLess(ms.get("same", 0), 0.5)


# --- 7. --colormatch off ----------------------------------------------------------------------
class SeededImagery(imagery.Imagery):
    """test_deshadow.SeededImagery: seeded noise tiles from three sources, USGS only east of
    LON_SPLIT."""
    LON_SPLIT = -118.30

    def _load(self, key):
        src, z, x, y = key
        if src.name == "usgs" and (x + 0.5) / 2 ** z * 360 - 180 < self.LON_SPLIT:
            return None
        rng = np.random.default_rng([z, x, y, len(src.name)])
        return imagery.as_tile(rng.integers(0, 256, (256, 256, 3), dtype=np.uint8))


def golden(**kw):
    h = hashlib.sha256()
    with tempfile.TemporaryDirectory() as d:
        im = SeededImagery(d, **kw)
        f = geodesy.EnuFrame(36.5785, -118.2923)
        for (x0, y0, size, px) in [(0, 0, 64, 256), (-12000, 300, 64, 256), (-2000, -3000, 512, 256),
                                   (-9000, 5000, 4096, 512), (-40000, -20000, 32768, 256)]:
            rgb, used = im.render(f, *imagery.gutter_square(x0, y0, size, px), px)
            h.update(rgb.tobytes())
            h.update(repr(sorted(used.items())).encode())
        im.close()
    return h.hexdigest()


class FlagOffTest(unittest.TestCase):
    # sha256 of these renders by the imagery code without the de-shadow and colour-match hooks (test_deshadow.FlagOffTest
    # pins the same digests: the finished pyramid's since 2026-10-29)
    GOLDEN = {1.0: "f0b36dae73d66c9d15d056a6bfbf4b37aee3e0af42ccc53e1de4bdd208790b3c",
              0.0: "a8c2eedb16fa69f148c11b613099c8d7501e60a2a9cbef2fa1cc48f420a01e47"}

    def test_render_byte_identical_without_colormatch(self):
        for tone, digest in self.GOLDEN.items():
            self.assertEqual(golden(tone=tone, colormatch=None), digest)

    def test_server_default_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = earthd.Server(tmp, os.path.join(tmp, "cache"))
            self.assertIsNone(s.colormatch)
            self.assertIsNone(s.imagery.colormatch)
            s.imagery.close()
            s2 = earthd.Server(tmp, os.path.join(tmp, "cache"), colormatch="s2")
            self.assertIs(s2.imagery.colormatch, s2.colormatch)
            self.assertEqual(s2.colormatch.mode, "s2")
            self.assertIn("colormatch s2", s2.imagery.summary())
            self.assertEqual(s2.imagery.attribution(), imagery.ATTRIBUTION)
            s2.imagery.close()
            s3 = earthd.Server(tmp, os.path.join(tmp, "cache"), imagery=("eox2023",), colormatch="s2")
            self.assertIsNone(s3.colormatch)        # no USGS in the chain: nothing to match
            s3.imagery.close()
            s4 = earthd.Server(tmp, os.path.join(tmp, "cache"), imagery=("usgs",), colormatch="s2")
            self.assertEqual(s4.imagery.attribution(), "Imagery: " + "; ".join(     # the reference is credited
                imagery.SOURCES[n].attribution for n in ("usgs", "eox2023", "eox2016")))
            s4.imagery.close()
            s5 = earthd.Server(tmp, os.path.join(tmp, "cache"), imagery=("usgs",))
            self.assertEqual(s5.imagery.attribution(), "Imagery: " + imagery.SOURCES["usgs"].attribution)
            s5.imagery.close()

    def test_cli_flag_defaults(self):
        """The command line turns s2 on when the imagery chain has USGS (in-game checks 2026-10-11);
        an explicit flag wins; no USGS / no imagery: off. Server() itself keeps off."""
        self.assertEqual(cm.MODES[0], "off")
        chain = imagery.parse_chain("usgs,eox2023,eox2016")
        self.assertEqual(earthd.resolve_colormatch(None, chain), "s2")
        self.assertEqual(earthd.resolve_colormatch("off", chain), "off")
        self.assertEqual(earthd.resolve_colormatch(None, imagery.parse_chain("eox2023")), "off")
        self.assertEqual(earthd.resolve_colormatch(None, imagery.parse_chain("off")), "off")
        import inspect
        self.assertEqual(inspect.signature(earthd.Server).parameters["colormatch"].default, "off")


# --- §6: the order with --deshadow ---------------------------------------------------------------
RELIGHT = np.array([1.25, 1.2, 1.1], np.float32)


class FakeDeshadow:
    """deshadow.Deshadow's interface for Imagery: every USGS z14+ tile 'relit' by RELIGHT."""
    tone_lift = 0.5
    mode = "fake"

    def bind(self, im):
        pass

    def relight(self, key, tile, im):
        src, z, x, y = key
        if src.name != "usgs" or z < 14:
            return None
        return ds.to_srgb(np.clip(ds.to_lin(tile.rgb) * RELIGHT, 0, 1))

    def summary(self):
        return "fake deshadow"

    def close(self):
        pass


class DeshadowOrderTest(unittest.TestCase):
    """raw -> de-shadow -> colour gain -> tone curve; the seams come from the raw tiles."""

    def test_order(self):
        tmp, cache, world, c, im, raw, out, _ = world_run()
        root = tempfile.mkdtemp(dir=tmp.name)
        shutil.copytree(cache, root, dirs_exist_ok=True)
        c2 = cm.ColorMatch(root, "s2", region_z=RZ)
        im2 = WorldImagery(os.path.join(root, "imagery"), world, colormatch=c2, deshadow=FakeDeshadow(), tone=1.0)
        rk = regions(world)[0]
        key = (U, 16, rk[0] * 4 + 2, rk[1] * 4 + 3)
        f = im2.finished([key])[key]            # the finished source tile (the pyramid's)
        got = f.data if isinstance(f.data, np.ndarray) else imagery.fast._ef.jpeg_rgb(f.data)
        relit = FakeDeshadow().relight(key, im2.tiles([key])[key], im2)
        want, _ = c2.apply(key, relit, im2)
        self.assertIsNotNone(want)
        lut_ds = imagery.tone_lut(1.0, FakeDeshadow.tone_lift)
        d = np.abs(got.astype(int) - lut_ds[want].astype(int))     # the gain on the relit tile, then its curve
        self.assertLess(d.mean(), 1.0)                              # (as its JPEG: imagery.FIN_QUALITY)
        self.assertTrue(f.relit and f.cm)                           # ... marked relit (its texels count as such)
        self.assertTrue((im2.tiles([key])[key].rgb == raw[key]).all())     # the raw tile stays
        # the tone curve comes last: the relit texels' curve over the corrected values
        frame = geodesy.EnuFrame(*lonlat(world, *world.P))
        sq = imagery.gutter_square(-100.0, -100.0, 200.0, 256)
        t1, used = im2.render(frame, *sq, 256)
        im2.close()
        im3 = WorldImagery(os.path.join(root, "imagery3"), world, colormatch=c2, deshadow=FakeDeshadow(), tone=0.0)
        t0, _ = im3.render(frame, *sq, 256)
        im3.close()
        self.assertEqual(used.get("deshadow"), 256 * 256)
        d = np.abs(t1.astype(int) - lut_ds[t0].astype(int))       # (per source tile before the warp: within a level)
        self.assertLess(d.mean(), 1.0)
        self.assertLessEqual(int(np.percentile(d, 99)), 4)
        # the analysis reads the raw tiles: the seams detected with de-shadowing on are the same
        c4 = cm.ColorMatch(tempfile.mkdtemp(dir=tmp.name), "s2", region_z=RZ)
        im4 = WorldImagery(os.path.join(root, "imagery4"), world, colormatch=c4, deshadow=FakeDeshadow())
        s4 = c4._detect(rk)
        im4.close()
        s1, _ = c._seams_of(rk)
        for k in ("y", "x", "ang", "step", "off"):
            np.testing.assert_array_equal(s4[k], s1[k], k)

    def test_inpaint_margin_relit(self):
        """With --deshadow the strip's samples beyond a tile edge come from the neighbours as the
        mosaic shows them (relit), like those inside: per-tile == the relit region corrected at
        once (before: raw neighbour samples, the strip ~20 % off for a few px at each tile edge)."""
        tmp, cache, world, c, im, raw, out, _ = world_run()
        root = tempfile.mkdtemp(dir=tmp.name)
        shutil.copytree(cache, root, dirs_exist_ok=True)
        c2 = cm.ColorMatch(root, "s2", region_z=RZ)
        im2 = WorldImagery(os.path.join(root, "imagery"), world, colormatch=c2, deshadow=FakeDeshadow())
        self.addCleanup(im2.close)
        keys = z16_tiles(world)
        got = {}
        for k, t in im2.tiles(keys).items():
            relit = im2.deshadow.relight(k, t, im2)
            out, _ = c2.apply(k, relit, im2)
            got[k] = relit if out is None else out
        after = mosaic(got, world)
        relit = ds.to_srgb(np.clip(ds.to_lin(mosaic(raw, world)) * RELIGHT, 0, 1))
        for rk in regions(world):
            ref, inside = whole_region(c2.field(rk, enqueue=False), relit, rk, world)
            x0, y0 = (rk[0] - world.X0) * 1024, (rk[1] - world.Y0) * 1024
            diff = np.abs(ref.astype(int) - after[y0:y0 + 1024, x0:x0 + 1024].astype(int)).max(-1)
            self.assertLessEqual(int(diff[inside].max()), 1, rk)


# --- earthd: textures rendered raw are rendered again once their region is ready ------------
class FakeDem:
    def sample(self, lat, lon, zoom, progress=None):
        if progress:
            progress(1, 1)
        return 1500 + 0 * np.asarray(lat, np.float64) * np.asarray(lon, np.float64)

    def stats_snapshot(self):
        return {}


class ServerRetextureTest(unittest.TestCase):
    def test_stale_texture_rendered_again(self):
        tmp, cache, world, c, im, raw, out, _ = world_run()
        root = tempfile.mkdtemp(dir=tmp.name)
        shutil.copytree(cache, os.path.join(root, "cache"), dirs_exist_ok=True)     # seams known
        for d, _, fs in os.walk(os.path.join(root, "cache")):
            for f in fs:
                if f.endswith(".npz") and not f.endswith(".seams.npz"):
                    os.remove(os.path.join(d, f))           # the fields are computed again
        gate = threading.Event()

        class Held(cm.ColorMatch):
            def _compute_field(self, rk):
                gate.wait(60)
                return super()._compute_field(rk)
        s = earthd.Server(root, os.path.join(root, "cache"), imagery=None, near_visual="mesh")
        self.addCleanup(s.tile_pool.shutdown)
        self.addCleanup(s.hm_pool.shutdown)
        s.dem = FakeDem()
        s.colormatch = Held(os.path.join(root, "cache"), "s2", region_z=RZ)
        s.colormatch.add_listener(s._cm_listener)
        s.imagery = WorldImagery(os.path.join(root, "cache", "imagery"), world, colormatch=s.colormatch)
        self.addCleanup(s.imagery.close)
        res = imagery.ground_res(LAT0, 16)
        a = s.new_anchor(*lonlat(world, world.P[0] - 128 / res, world.P[1] + 128 / res))    # P mid-tile
        tile = ff.tile_of("0_0_0")
        ck = (a.id, tile.key)

        async def run():
            first = await s.tile(a, tile)
            self.assertTrue(first.cm_pending, first)
            self.assertFalse(first.stale)
            gate.set()
            for _ in range(600):                    # the listener runs on the loop
                await asyncio.sleep(0.05)
                if s.tiles[ck].stale and not s.cm_waiting:
                    break
            self.assertTrue(s.tiles[ck].stale)
            second = await s.tile(a, tile)
            third = await s.tile(a, tile)           # not stale any more: the same info
            return first, second, third
        first, second, third = asyncio.run(run())
        self.assertEqual(first.texture, f"far_{a.id}_0_0_0.dds")
        self.assertEqual(second.texture, f"far_{a.id}_0_0_0_r1.dds")
        self.assertEqual((second.rev, second.stale, second.cm_pending), (1, False, frozenset()))
        self.assertIs(third, second)
        self.assertIs(second.pos, first.pos)       # the shape stays
        self.assertEqual(second.material, first.material)
        p0, p1 = (os.path.join(s.out_dir, n) for n in (first.texture, second.texture))
        self.assertTrue(os.path.exists(p0) and os.path.exists(p1))
        with open(p0, "rb") as f0, open(p1, "rb") as f1:
            self.assertNotEqual(f0.read(), f1.read())
        x0, y0, _, _ = tile.bbox
        px = s.tex_size(tile.level)
        rgb, _ = s.imagery.render(a.frame, *imagery.gutter_square(x0, y0, tile.size, px), px)
        with open(p1, "rb") as f1:
            self.assertEqual(f1.read(), imagery.encode_dds(rgb))
        s._evict_tile(ck)                           # a later full build takes the next name
        self.assertEqual(s.tex_rev[ck], 2)
        self.assertEqual(s.texture_name(a.id, tile.key, s.tex_rev[ck]), f"far_{a.id}_0_0_0_r2.dds")

    def test_evicted_while_pending_takes_the_next_name(self):
        """A tile evicted while one of its regions is still pending is not marked stale (it is gone
        when the region finishes), but its next build may render the corrected texture: it must not
        reuse the raw texture's file name (the engine caches textures by path)."""
        with tempfile.TemporaryDirectory() as tmp:
            s = earthd.Server(tmp, os.path.join(tmp, "cache"), imagery=None)
            self.addCleanup(s.tile_pool.shutdown)
            self.addCleanup(s.hm_pool.shutdown)
            ck = (0, "0_0_0")
            s.tiles[ck] = earthd.TileInfo([0, 0, 0], 0.0, None, 0.0, None, 0, 0.0, "m", "far_0_0_0_0.dds",
                                          cm_pending=frozenset({(1, 2)}))
            s._evict_tile(ck)
            self.assertEqual(s.tex_rev.get(ck), 1)
            ck2 = (0, "0_0_1")
            s.tiles[ck2] = earthd.TileInfo([0, 0, 0], 0.0, None, 0.0, None, 0, 0.0, "m", "far_0_0_0_1.dds")
            s._evict_tile(ck2)                      # nothing pending, never re-rendered: the name stays
            self.assertNotIn(ck2, s.tex_rev)


if __name__ == "__main__":
    unittest.main()
