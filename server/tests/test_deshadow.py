"""server/deshadow.py (docs/research/2026-10-10-deshadow-prototype.md, integration plan): the
filters, the sun fit, the relight and its gates on synthetic scenes (a ridge with a known sun,
albedo texture, haze and JPEG), the region cache, the hook in imagery.Imagery and --deshadow off
being byte-identical to the imagery without it. No network.

Timing (per-tile cost) runs only with DESHADOW_TIMING=1 (CPU load from other work makes it flaky)."""
import glob
import hashlib
import io
import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deshadow as ds  # noqa: E402
import geodesy  # noqa: E402
import imagery  # noqa: E402

RES_F = 1.92                # fine pixel (z16 at ~36 N)
F = 4                       # fine px per grid cell (z14)
RES_G = RES_F * F
N_G = 512                   # synthetic region: 512 grid cells (3.9 km), 8 x 8 fine tiles
N_F = N_G * F
SUN = (100.0, 35.0)         # az, el of the synthetic capture
RHO = np.array([0.07, 0.072, 0.197], np.float32)
HAZE = np.array([0.010, 0.012, 0.022], np.float32)
EXPO = 2.4


def terrain(xe, yn, flat=False):
    """Heights (m) at east / north metres: a north-south ridge (~700 m) east of a valley, a few
    hills, a gentle tilt."""
    xe, yn = np.broadcast_arrays(np.asarray(xe, np.float64), np.asarray(yn, np.float64))
    if flat:
        return np.full(xe.shape, 1000.0)
    h = 1200.0 + 0.02 * xe + 700.0 * np.exp(-((xe - 2700.0) / 380.0) ** 2) * (1 + 0.25 * np.sin(yn / 700.0))
    rng = np.random.default_rng(7)
    for _ in range(7):
        cx, cy = rng.uniform(0, 3900), rng.uniform(-3900, 0)
        hh, sg = rng.uniform(60, 220), rng.uniform(120, 320)
        h = h + hh * np.exp(-((xe - cx) ** 2 + (yn - cy) ** 2) / (2 * sg * sg))
    return h


def dem_grid(res, n, pad, flat=False):
    """terrain() on a north-up grid of cell res with pad cells around the n x n core (row 0 north,
    the core's north-west corner at (0, 0) m)."""
    c = (np.arange(-pad, n + pad) + 0.5) * res
    return terrain(c[None, :], -c[:, None], flat).astype(np.float32)


def albedo(n, seed=1, dark=False):
    """Smooth multi-scale texture (linear RGB albedo), vegetation-like."""
    rng = np.random.default_rng(seed)
    f = sum(ds.box(rng.standard_normal((n, n)).astype(np.float32), s) * s for s in (1, 6, 24))
    f = (f - f.mean()) / f.std()
    base = np.array([0.05, 0.05, 0.05] if dark else [0.13, 0.14, 0.09], np.float32)
    a = base * np.exp(0.3 * f)[..., None]
    a[rng.random((n, n)) < 0.03] *= 0.06       # canopy gaps / tree shadows: the dark objects
    return (a * (1 + 0.04 * rng.standard_normal((n, n, 3)))).astype(np.float32)


def jpeg(rgb, q=85):
    b = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(b, "JPEG", quality=q)
    return np.asarray(Image.open(io.BytesIO(b.getvalue())).convert("RGB"))


class Scene:
    """Fine (z16-like) photo of the synthetic region under SUN, its true shadow and the expected
    relit image (the albedo under a flat-ground sun), plus the z14-like grid image and DEM."""

    def __init__(self, flat=False, cast=True, shift=0, dark=False, sun=SUN):
        az, el = sun
        self.sun = sun
        pf = int(math.ceil(ds.PAD_M / RES_F)) + 4
        hf = dem_grid(RES_F, N_F, pf, flat)
        ht = ds.horizon_tan(hf, RES_F, pf, N_F, az)
        lit = (ht < math.tan(math.radians(el))).astype(np.float32)
        if not cast:
            lit[:] = 1
        lam = ds.lambert(ds.normals(hf[pf:pf + N_F, pf:pf + N_F], RES_F), az, el)
        A = albedo(N_F, dark=dark)
        sz = math.sin(math.radians(el))
        lin = A * EXPO * (RHO + (lam * lit)[..., None]) + HAZE
        self.expected = A * EXPO * (RHO + sz) + HAZE
        if shift:           # the photo misregistered against the DEM by `shift` px east
            lin = np.roll(lin, shift, 1)
            lit = np.roll(lit, shift, 1)
            lam = np.roll(lam, shift, 1)
            self.expected = np.roll(self.expected, shift, 1)
        self.lit, self.lam = lit, lam
        self.rgb = jpeg(ds.to_srgb(lin))
        self.lin = ds.to_lin(self.rgb)
        self.grid_rgb = np.rint(ds.down(self.rgb.astype(np.float32), F)).astype(np.uint8)
        self.pad = ds.grid_pad(RES_G)
        self.hpad = dem_grid(RES_G, N_G, self.pad + ds.EXT, flat)

    def region(self, cast=True, fine=True):
        return ds.compute_region(self.grid_rgb, self.hpad, RES_G, self.pad, cast, self.fine if fine else None)

    def fine(self, P, grids):
        return [self.tile_input(grids, tx, ty) for tx, ty in ds.edge_tiles(grids, N_G)]

    def tile_input(self, grids, tx, ty):
        """A z16 tile with its margin (edge-replicated at the scene's border) and its terms."""
        m = ds.M
        if not hasattr(self, "_padded"):
            self._padded = np.pad(self.rgb, ((m, m), (m, m), (0, 0)), mode="edge")
        rgb_m = self._padded[ty * 256:(ty + 1) * 256 + 2 * m, tx * 256:(tx + 1) * 256 + 2 * m]
        return rgb_m, ds.sample_grids(grids, 0, 0, 16, tx, ty, n_core=N_G)

    def relight(self, P, grids):
        """The relit fine image (tile by tile, as the server does), the snapped masks and the paths."""
        out = self.rgb.copy()
        mask = np.ones((N_F, N_F), np.float32)
        paths = {}
        for ty in range(N_F // 256):
            for tx in range(N_F // 256):
                rgb_m, terms = self.tile_input(grids, tx, ty)
                o, path = ds.relight_tile(rgb_m, terms, P, fd=2)
                lit, _, _ = ds.tile_lit(rgb_m, terms, P, fd=2)
                sl = (slice(ty * 256, (ty + 1) * 256), slice(tx * 256, (tx + 1) * 256))
                mask[sl] = lit[ds.M:-ds.M, ds.M:-ds.M]
                paths[path] = paths.get(path, 0) + 1
                if o is not None:
                    out[sl] = o
        return out, mask, paths


_SCENES = {}


def scene(**kw):
    """Scenes are deterministic and slow-ish to make: one per configuration per run."""
    key = tuple(sorted(kw.items()))
    if key not in _SCENES:
        _SCENES[key] = Scene(**kw)
    return _SCENES[key]


def edge_distance_mask(mask, d):
    """Pixels at least d px away from the boundary of a boolean mask."""
    k = ds.box(mask.astype(np.float32), d)
    return (k < 1e-6) | (k > 1 - 1e-6)


def hard_terms(hpad, res, pe, n, sun=SUN):
    """lit (a hard shadow: a photo's penumbra is a pixel or two) and lam of a grid scene."""
    _, lam = ds.terrain_terms(hpad, res, pe, n, *sun)
    lit = (ds.horizon_tan(hpad, res, pe, n, sun[0]) < math.tan(math.radians(sun[1]))).astype(np.float32)
    return lit, lam


def grid_render(hpad, pe, n, sun=SUN, cast=True, shade=True, dark=False, seed=1):
    """A z14-like photo rendered on the grid itself (cheap scenes for the gates)."""
    lit, lam = hard_terms(hpad, RES_G, pe, n, sun)
    if not cast:
        lit[:] = 1
    A = albedo(n, seed, dark)
    light = (RHO + (lam * lit)[..., None]) if shade else (RHO + math.sin(math.radians(sun[1])))
    return jpeg(ds.to_srgb(A * EXPO * light + HAZE)), lit, lam


class FilterTest(unittest.TestCase):
    def test_box_matches_brute_force(self):
        rng = np.random.default_rng(3)
        a = rng.standard_normal((37, 29)).astype(np.float32) * 100
        for r in (1, 2, 5, 40):
            ref = np.array([[a[max(i - r, 0):i + r + 1, max(j - r, 0):j + r + 1].mean() for j in range(29)]
                            for i in range(37)])
            np.testing.assert_allclose(ds.box(a, r), ref, rtol=1e-5, atol=1e-3)
        self.assertIs(ds.box(a, 0), a)

    def test_guided_filter_keeps_an_aligned_edge_without_overshoot(self):
        """A mask edge that sits on the guide's edge stays sharp and inside [0, 1] (no ringing:
        a halo would come from values above 1 next to the edge)."""
        g = np.zeros((40, 40), np.float32)
        g[:, 20:] = 1.0
        p = g + np.random.default_rng(0).normal(0, 0.05, g.shape).astype(np.float32)
        q = ds.guided(g * 0.25, p, 2, 0.01)
        self.assertGreater(q.min(), -0.06)
        self.assertLess(q.max(), 1.06)
        self.assertLess(np.abs(q[:, :19]).mean(), 0.05)
        self.assertLess(np.abs(q[:, 21:] - 1).mean(), 0.05)

    def test_horizon_march_matches_brute_force(self):
        """The shifted-slice march equals per-point bilinear sampling along the ray."""
        rng = np.random.default_rng(5)
        n, pad, res = 24, 30, 10.0
        h = ds.box(rng.uniform(0, 400, (n + 2 * pad, n + 2 * pad)).astype(np.float32), 3)
        for az in (0.0, 37.0, 100.0, 233.0):
            got = ds.horizon_tan(h, res, pad, n, az, max_dist=250.0)
            s, c = math.sin(math.radians(az)), math.cos(math.radians(az))
            ref = np.full((n, n), -1e9)
            for i in range(n):
                for j in range(n):
                    d = res
                    while d <= 250.0:
                        y, x = pad + i - c * d / res, pad + j + s * d / res
                        y0, x0 = int(math.floor(y)), int(math.floor(x))
                        fy, fx = y - y0, x - x0
                        v = ((h[y0, x0] * (1 - fx) + h[y0, x0 + 1] * fx) * (1 - fy)
                             + (h[y0 + 1, x0] * (1 - fx) + h[y0 + 1, x0 + 1] * fx) * fy)
                        ref[i, j] = max(ref[i, j], (v - h[pad + i, pad + j]) / d)
                        d = max(d + res, d * 1.04)
            np.testing.assert_allclose(got, ref, rtol=1e-4, atol=1e-4)

    def test_cap_preserves_hue(self):
        """A gain over GMAX scales all three channels together: their ratios are kept."""
        lam = np.full((4, 4), 0.6, np.float32)
        direct = np.zeros((4, 4), np.float32)
        rho, sz = (0.02, 0.03, 0.08), 0.6
        g = ds.gain_field(lam, direct, rho, sz)
        unc = np.array([min(ds.LAM_CAP, max(1 / ds.LAM_CAP, (r + sz) / (r + 0.6))) * (r + 0.6) / r for r in rho])
        self.assertGreater(unc.max(), ds.GMAX)
        np.testing.assert_allclose(g[0, 0], unc * ds.GMAX / unc.max(), rtol=1e-5)
        self.assertAlmostEqual(float(g.max()), ds.GMAX, places=4)

    def test_tone_lift(self):
        """The de-shadowed texels' tone curve lifts black less and is the same curve otherwise."""
        full, half = imagery.tone_lut(1.0).astype(int), imagery.tone_lut(1.0, ds.FULL_TONE_LIFT).astype(int)
        self.assertTrue((imagery.tone_lut(1.0, 1.0) == imagery.tone_lut(1.0)).all())
        self.assertLess(half[0], full[0])
        self.assertTrue((half <= full).all() and (full - half).max() <= 8)
        self.assertEqual(half[255], full[255])


class SyntheticSceneTest(unittest.TestCase):
    """A ridge east of a valley under a known sun (az 100, el 35): its cast shadow, slope shading,
    an albedo texture with dark objects, haze and JPEG; fitted and relit as the server does."""

    @classmethod
    def setUpClass(cls):
        cls.S = scene()
        cls.P, cls.G = cls.S.region()
        cls.out, cls.mask, cls.paths = cls.S.relight(cls.P, cls.G)

    def test_sun_recovered(self):
        P = self.P
        self.assertTrue(P["ok"] and P["cast"], P)
        self.assertLessEqual(abs((P["az"] - SUN[0] + 180) % 360 - 180), 3.0)
        self.assertLessEqual(abs(P["el"] - SUN[1]), 1.5)
        self.assertGreaterEqual(P["r2"], ds.R2_MIN)
        self.assertEqual((P["rho_src"], P["haze_src"]), ("edge16", "z16"))

    def test_shadow_relit_to_flat_sun(self):
        """Inside the cast shadow (flat ground, 6 px from its edge) the relit image is the albedo
        under the flat-ground sun within 10 %; sunlit flat ground is left alone."""
        S, lit = self.S, self.S.lit
        flat = np.abs(S.lam - math.sin(math.radians(SUN[1]))) < 0.05
        inner = (lit < 0.5) & edge_distance_mask(lit < 0.5, 6) & flat
        oe = ds.luma(ds.to_lin(self.out)) / ds.luma(S.expected)
        self.assertGreater(inner.sum(), 50000)
        self.assertLess(np.median((ds.luma(S.lin) / ds.luma(S.expected))[inner]), 0.3)    # it was dark
        self.assertLess(abs(np.median(oe[inner]) - 1), 0.10)
        self.assertLess(abs(np.median(oe[(lit > 0.5) & flat]) - 1), 0.05)

    def test_no_halo(self):
        """No visibly sunlit pixel (lam * lit >= 0.2, 2 px from the shadow edge) is brightened
        above LAM_CAP: the shadow boost never leaks out of the shadow. (Grazing-lit pixels are as
        dark as shadow in the photo and may be relit like it: so does the ideal relight.)"""
        S, lit = self.S, self.S.lit
        vis = (lit > 0.5) & edge_distance_mask(lit > 0.5, 2) & (S.lam * lit >= 0.2)
        ratio = ds.luma(ds.to_lin(self.out)) / np.maximum(ds.luma(S.lin), 1e-3)
        self.assertGreater(vis.sum(), 10 ** 6)
        self.assertLessEqual(ratio[vis].max(), ds.LAM_CAP + 0.02)

    def test_mask_and_paths(self):
        tm, pm = self.S.lit < 0.5, self.mask < 0.5
        self.assertGreater((tm & pm).sum() / (tm | pm).sum(), 0.95)
        self.assertGreater(self.paths.get("edge", 0), 0)
        self.assertGreater(self.paths.get("fast", 0), 0)

    def test_misregistration_snaps_to_the_photo(self):
        """The photo 20 px (38 m) east of the DEM: the snapped mask follows the photo's shadow."""
        S = scene(shift=20)
        P, G = S.region()
        out, mask, _ = S.relight(P, G)
        tm, pm = S.lit < 0.5, mask < 0.5
        self.assertGreaterEqual((tm & pm).sum() / (tm | pm).sum(), 0.9)
        lit = S.lit
        flat = np.abs(S.lam - math.sin(math.radians(SUN[1]))) < 0.05
        inner = (lit < 0.5) & edge_distance_mask(lit < 0.5, 6) & flat
        self.assertLess(abs(np.median((ds.luma(ds.to_lin(out)) / ds.luma(S.expected))[inner]) - 1), 0.10)

    def test_lambert_mode(self):
        """Without the cast term only the slope shading changes, by at most LAM_CAP either way:
        the cast shadow stays dark."""
        P = dict(self.P, cast=False)
        S, lit = self.S, self.S.lit
        out, _, paths = S.relight(P, self.G)
        self.assertEqual(set(paths) - {"noop"}, {"lambert"})
        ratio = ds.luma(ds.to_lin(out)) / np.maximum(ds.luma(S.lin), 1e-3)
        bright = ds.luma(S.lin) > 0.02
        self.assertLessEqual(ratio[bright].max(), ds.LAM_CAP + 0.05)
        self.assertGreaterEqual(ratio[bright].min(), 1 / ds.LAM_CAP - 0.05)
        inner = (lit < 0.5) & edge_distance_mask(lit < 0.5, 6)
        self.assertLess(np.median((ds.luma(ds.to_lin(out)) / ds.luma(S.expected))[inner]), 0.4)


class GateTest(unittest.TestCase):
    """Regions and tiles the relight must leave exactly as they are."""

    @classmethod
    def setUpClass(cls):
        cls.pad = ds.grid_pad(RES_G)
        cls.pe = cls.pad + ds.EXT
        cls.hpad = dem_grid(RES_G, N_G, cls.pe)

    def test_flat_dem_unchanged(self):
        flat = dem_grid(RES_G, N_G, self.pe, flat=True)
        rgb, _, _ = grid_render(flat, self.pe, N_G)
        P, grids = ds.compute_region(rgb, flat, RES_G, self.pad)
        self.assertFalse(P["ok"])
        self.assertIsNone(grids)
        # and a flat, sunlit tile in a relit region is bitwise unchanged
        S = scene()
        P2, _ = S.region()
        terms = np.zeros((272, 272, 3), np.float32)
        terms[..., 0] = terms[..., 2] = 1
        terms[..., 1] = math.sin(math.radians(P2["el"]))
        self.assertTrue(ds.unchanged(terms, P2))
        self.assertEqual(ds.relight_tile(S.rgb[:272, :272], terms, P2, 2), (None, "noop"))

    def test_low_r2_region_unchanged(self):
        """A photo whose brightness does not follow the DEM's shading (other relief, clouds,
        a mosaic of flights): the fit is rejected and the region left alone."""
        rgb, _, _ = grid_render(self.hpad, self.pe, N_G, shade=False)
        P, grids = ds.compute_region(rgb, self.hpad, RES_G, self.pad)
        self.assertFalse(P["ok"], P)
        self.assertIsNone(grids)
        self.assertLess(P["r2"], ds.R2_MIN)

    def test_separation_gate(self):
        """The DEM predicts cast shadows the photo does not show (on a normal or a dark texture):
        the classes do not separate and the cast term is off (only the slope term stays)."""
        lit_t, lam_t = ds.terrain_terms(self.hpad, RES_G, self.pe, N_G, *SUN)
        for dark in (False, True):
            rgb, _, _ = grid_render(self.hpad, self.pe, N_G, cast=False, dark=dark)
            P = ds.region_params(rgb, lit_t, lam_t, SUN[1])
            self.assertFalse(P["cast"], P)
            self.assertLess(P["sep"], ds.SEP_MIN)
        rgb, _, _ = grid_render(self.hpad, self.pe, N_G)
        self.assertTrue(ds.region_params(rgb, lit_t, lam_t, SUN[1])["cast"])


# --- the Deshadow object and the imagery hook -------------------------------------------------
LAT0, LON0 = 36.5785, -118.2923
RK = tuple(int(v) // 256 for v in imagery.mercator_px(LAT0, LON0, ds.REGION_Z))
RES14 = ds.res_at(float(ds.lonlat_of_px(0, (RK[1] + 0.5) * 256, ds.REGION_Z)[1]), ds.GRID_Z)


def region_xy(xe, yn):
    """lat, lon of east / north metres from the test region's north-west corner."""
    lon, lat = ds.lonlat_of_px(RK[0] * ds.GRID_N + xe / RES14, RK[1] * ds.GRID_N - yn / RES14, ds.GRID_Z)
    return float(lat), float(lon)


class FakeDem:
    """terrain() on the test region's Web-Mercator metres. block: an Event sample() waits for."""

    def __init__(self, flat=False, fail=False, block=None):
        self.flat, self.fail, self.block, self.calls = flat, fail, block, 0

    def sample(self, lat, lon, zoom, progress=None):
        self.calls += 1
        if self.block is not None:
            self.block.wait(30)
        if self.fail:
            raise IOError("no DEM in this test")
        X, Y = imagery.mercator_px(lat, lon, ds.GRID_Z)
        return terrain((X - RK[0] * ds.GRID_N) * RES14, -(Y - RK[1] * ds.GRID_N) * RES14, self.flat)


_PHOTO = {}


def region_photo():
    """The test region's z14 photo under SUN (rendered on the grid), once per run."""
    if "rgb" not in _PHOTO:
        pe = ds.grid_pad(RES14) + ds.EXT
        idx = (np.arange(-pe, ds.GRID_N + pe) + 0.5) * RES14
        h = terrain(idx[None, :], -idx[:, None]).astype(np.float32)
        lit, lam = hard_terms(h, RES14, pe, ds.GRID_N)
        _PHOTO["rgb"] = jpeg(ds.to_srgb(albedo(ds.GRID_N) * EXPO * (RHO + (lam * lit)[..., None]) + HAZE))
    return _PHOTO["rgb"]


class RegionImagery(imagery.Imagery):
    """USGS-only imagery serving the region photo: z14 tiles, z15 / z16 upsampled from it; seeded
    noise elsewhere (and for the coverage probes). No disk, no network."""

    def __init__(self, cache_dir, **kw):
        super().__init__(cache_dir, ("usgs",), **kw)
        self.photo = region_photo()

    def _load(self, key):
        src, z, x, y = key
        k = 2 ** (z - ds.GRID_Z) if z >= ds.GRID_Z else 0
        if k and x >> (z - ds.REGION_Z) == RK[0] and y >> (z - ds.REGION_Z) == RK[1]:
            c = (x * 256 // k - RK[0] * ds.GRID_N, y * 256 // k - RK[1] * ds.GRID_N)
            a = self.photo[c[1]:c[1] + 256 // k, c[0]:c[0] + 256 // k]
            return imagery.as_tile(np.ascontiguousarray(a.repeat(k, 0).repeat(k, 1)))
        rng = np.random.default_rng([z, x, y])
        return imagery.as_tile(rng.integers(40, 200, (256, 256, 3), dtype=np.uint8))


def files_under(root):
    return sorted(os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(root) for f in fs)


class DeshadowObjectTest(unittest.TestCase):
    """Region values in the background, their disk cache, the hook in Imagery.render."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cache = os.path.join(cls.tmp.name, "computed")
        d = ds.Deshadow(cls.cache, FakeDem(), "full", wait_s=120)
        im = RegionImagery(os.path.join(cls.cache, "imagery"), deshadow=d, tone=0.0)     # the relight alone
        cls.frame = geodesy.EnuFrame(*region_xy(1800, -4000))       # in the ridge's cast shadow
        cls.square = (-250.0, -250.0, 500.0, 256)
        d.prepare(*region_xy(1800, -4000))
        cls.R = d.region(RK)
        cls.relit, cls.used = im.render(cls.frame, *cls.square)
        cls.stats = dict(d.stats)
        im.close()
        raw = RegionImagery(os.path.join(cls.tmp.name, "raw"), tone=0.0)
        cls.raw, _ = raw.render(cls.frame, *cls.square)
        raw.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def copy_cache(self):
        dst = tempfile.mkdtemp(dir=self.tmp.name)
        shutil.copytree(self.cache, dst, dirs_exist_ok=True)
        return dst

    def test_region_fitted_in_the_background(self):
        P = self.R.params
        self.assertTrue(P["ok"] and P["cast"], P)
        self.assertLessEqual(abs(P["az"] - SUN[0]), 3.0)
        self.assertLessEqual(abs(P["el"] - SUN[1]), 1.5)
        self.assertEqual(self.R.grids.shape, (ds.GRID_N + 2 * ds.EXT,) * 2 + (3,))

    def test_render_relit_and_raw_tiles_untouched(self):
        self.assertIn("deshadow", self.used)
        lr, lw = ds.luma(ds.to_lin(self.relit)), ds.luma(ds.to_lin(self.raw))
        dark, sunlit = lw < 0.04, lw > 0.12
        self.assertGreater(dark.mean(), 0.2)
        self.assertGreater(np.median(lr[dark] / lw[dark]), 4)             # the cast shadow is lifted (haze stays)
        self.assertLess(abs(np.median(lr[sunlit] / lw[sunlit]) - 1), 0.25)
        files = files_under(self.cache)
        ds_dir = os.path.join("imagery", f"usgs-ds{ds.VERSION}-full")
        fin = os.path.join("imagery", f"fin{imagery.FIN_VERSION}-")
        for f in files:     # only region values, relit tiles and the finished pyramid: the raw tile cache is never written
            self.assertTrue(f.startswith("deshadow" + os.sep) or f.startswith(ds_dir + os.sep) or f.startswith(fin), f)
        self.assertTrue(any(f.endswith(".png") for f in files))
        self.assertIn(os.path.join("deshadow", "usgs", "12", str(RK[0]), f"{RK[1]}.json"), files)
        self.assertIn(os.path.join("deshadow", "usgs", "12", str(RK[0]), f"{RK[1]}.npz"), files)

    def test_cache_round_trip(self):
        """A new server reads the region values and the relit tiles from disk: no DEM, same texture."""
        root = self.copy_cache()
        dem = FakeDem(fail=True)
        d = ds.Deshadow(root, dem, "full", wait_s=0)
        im = RegionImagery(os.path.join(root, "imagery"), deshadow=d, tone=0.0)
        R = d.region(RK)
        self.assertEqual(R.params, json.loads(json.dumps(self.R.params)))
        self.assertTrue((R.grids == self.R.grids).all())
        rgb, used = im.render(self.frame, *self.square)
        im.close()
        self.assertEqual(dem.calls, 0)
        self.assertTrue((rgb == self.relit).all())
        self.assertGreater(im.stats_snapshot().get("finished:disk", 0), 0)    # (the finished pyramid's relit tiles)
        self.assertNotIn("edge", d.stats)
        # without the pyramid: the relit tiles from the de-shadow cache, the same texture
        for f in glob.glob(os.path.join(root, "imagery", f"fin{imagery.FIN_VERSION}-*")):
            shutil.rmtree(f)
        d = ds.Deshadow(root, dem, "full", wait_s=0)
        im = RegionImagery(os.path.join(root, "imagery"), deshadow=d, tone=0.0)
        rgb, used = im.render(self.frame, *self.square)
        im.close()
        self.assertEqual(dem.calls, 0)
        self.assertTrue((rgb == self.relit).all())
        self.assertGreater(d.stats.get("disk", 0), 0)

    def test_version_bump_recomputes(self):
        root = self.copy_cache()
        jp = os.path.join(root, "deshadow", "usgs", "12", str(RK[0]), f"{RK[1]}.json")
        with open(jp) as f:
            P = json.load(f)
        P["version"] = ds.VERSION - 1
        with open(jp, "w") as f:
            json.dump(P, f)
        dem = FakeDem()
        d = ds.Deshadow(root, dem, "full", wait_s=120)
        im = RegionImagery(os.path.join(root, "imagery"), deshadow=d)
        d.prepare(*region_xy(1800, -4000))
        R = d.region(RK)
        im.close()
        self.assertGreater(dem.calls, 0)
        self.assertEqual(R.params["version"], ds.VERSION)
        with open(jp) as f:
            self.assertEqual(json.load(f)["version"], ds.VERSION)

    def test_relit_texels_use_the_reduced_shadow_lift(self):
        """With the tone curve on, relit texels go through tone_lut(tone, FULL_TONE_LIFT) ("full";
        "lambert" keeps the full lift since cast shadows stay), every other texel through the
        usual curve."""
        root = self.copy_cache()
        for mode, lift in (("full", ds.FULL_TONE_LIFT), ("lambert", 1.0)):
            d = ds.Deshadow(root, FakeDem(fail=True), mode, wait_s=0)
            im = RegionImagery(os.path.join(root, "imagery"), deshadow=d, tone=1.0)
            self.assertTrue((im.lut_ds == imagery.tone_lut(1.0, lift)).all())
            if mode == "full":
                rgb, used = im.render(self.frame, *self.square)
                # every texel relit here: the reduced lift's curve (applied per finished source tile, before the warp
                # and the tile's JPEG, so within a few levels of the curve of the tone-0 texture)
                near = np.abs(rgb.astype(int) - im.lut_ds[self.relit].astype(int))
                plain = np.abs(rgb.astype(int) - im.lut[self.relit].astype(int))
                self.assertEqual(used.get("deshadow"), rgb.shape[0] * rgb.shape[1])
                self.assertLess(near.mean(), 1.0)
                self.assertLess(near.mean(), 0.6 * plain.mean())
            im.close()

    def test_lambert_mode_reuses_the_region(self):
        """Region values are shared by the modes; lambert relights without the cast term and keeps
        its tiles apart."""
        root = self.copy_cache()
        d = ds.Deshadow(root, FakeDem(fail=True), "lambert", wait_s=0)
        im = RegionImagery(os.path.join(root, "imagery"), deshadow=d, tone=0.0)
        self.assertFalse(d.region(RK).params["cast"])
        rgb, used = im.render(self.frame, *self.square)
        im.close()
        self.assertNotIn("edge", d.stats)
        self.assertNotIn("fast", d.stats)
        self.assertTrue(os.path.isdir(os.path.join(root, "imagery", f"usgs-ds{ds.VERSION}-lambert")))
        lr, lw = ds.luma(ds.to_lin(rgb)), ds.luma(ds.to_lin(self.raw))
        dark = lw < 0.04
        self.assertLess(np.median(lr[dark] / lw[dark]), ds.LAM_CAP + 0.05)  # the cast shadow stays

    def test_no_wait(self):
        """Under no_wait() a render takes a region that is still being computed as pending at once (the
        block's base photo), though the anchor's deadline would have it wait 120 s."""
        gate = threading.Event()
        root = tempfile.mkdtemp(dir=self.tmp.name)
        d = ds.Deshadow(root, FakeDem(block=gate), "full", wait_s=120)
        im = RegionImagery(os.path.join(root, "imagery"), deshadow=d, tone=0.0)
        d.prepare(*region_xy(1800, -4000))
        pending = set()
        t0 = time.monotonic()
        with d.no_wait():
            rgb, used = im.render(self.frame, *self.square, pending)
        took = time.monotonic() - t0
        gate.set()
        im.close()
        self.assertLess(took, 10.0)
        self.assertNotIn("deshadow", used)
        self.assertTrue((rgb == self.raw).all())
        self.assertTrue(any(p[0] == imagery.DS_PENDING for p in pending), "the region is reported pending")
        self.assertFalse(getattr(d._tl, "nowait", False), "no_wait() is undone")

    def test_pending_and_gated_regions_render_raw(self):
        """Until a region is ready (and where it is gated off) the texture is byte-identical to
        the imagery without de-shadowing."""
        gate = threading.Event()
        root = tempfile.mkdtemp(dir=self.tmp.name)
        d = ds.Deshadow(root, FakeDem(block=gate), "full", wait_s=0)
        im = RegionImagery(os.path.join(root, "imagery"), deshadow=d, tone=0.0)
        d.prepare(*region_xy(1800, -4000))
        rgb, used = im.render(self.frame, *self.square)
        gate.set()
        im.close()
        self.assertNotIn("deshadow", used)
        self.assertGreater(d.stats.get("pending", 0), 0)
        self.assertTrue((rgb == self.raw).all())

        root = tempfile.mkdtemp(dir=self.tmp.name)
        d = ds.Deshadow(root, FakeDem(flat=True), "full", wait_s=120)
        im = RegionImagery(os.path.join(root, "imagery"), deshadow=d, tone=0.0)
        d.prepare(*region_xy(1800, -4000))
        rgb, used = im.render(self.frame, *self.square)
        im.close()
        self.assertFalse(d.region(RK).params["ok"])
        self.assertGreater(d.stats.get("off", 0), 0)
        self.assertTrue((rgb == self.raw).all())

    def test_pending_region_reported_and_listener(self):
        """render(pending=) names a region still to come as (DS_PENDING, rx, ry); its status is None
        until the worker is done, then the listener gets (rk, relights) and the next render is relit
        with nothing pending (earthd re-renders the shown tiles: far_retex)."""
        gate = threading.Event()
        root = self.copy_cache()
        for f in os.listdir(os.path.join(root, "deshadow", "usgs", "12", str(RK[0]))):
            os.remove(os.path.join(root, "deshadow", "usgs", "12", str(RK[0]), f))     # the region again
        shutil.rmtree(os.path.join(root, "imagery", f"usgs-ds{ds.VERSION}-full"))
        for f in glob.glob(os.path.join(root, "imagery", f"fin{imagery.FIN_VERSION}-*")):
            shutil.rmtree(f)        # (the finished pyramid's relit tiles too)
        d = ds.Deshadow(root, FakeDem(block=gate), "full", wait_s=0)
        heard, done = [], threading.Event()
        d.add_listener(lambda rk, changed: (heard.append((rk, changed, threading.current_thread().name)), done.set()))
        im = RegionImagery(os.path.join(root, "imagery"), deshadow=d, tone=0.0)
        d.prepare(*region_xy(1800, -4000))
        pending = set()
        rgb, used = im.render(self.frame, *self.square, pending=pending)
        self.assertEqual(pending, {(imagery.DS_PENDING,) + RK})
        self.assertTrue((rgb == self.raw).all())
        self.assertIsNone(d.status(RK))
        gate.set()
        self.assertTrue(done.wait(120))
        self.assertEqual(heard, [(RK, True, "deshadow")])
        self.assertIs(d.status(RK), True)
        pending = set()
        rgb, used = im.render(self.frame, *self.square, pending=pending)
        im.close()
        self.assertEqual(pending, set())
        self.assertTrue((rgb == self.relit).all())
        # without pending= (or beyond REQUEST_M) nothing is reported
        self.assertIsNone(d.pending_region((im.chain[0], 9, 0, 0), None))
        far = ds.Deshadow(root, FakeDem(fail=True), "full", wait_s=0)
        far._center = (0.0, 0.0)            # an anchor far away: the test region is beyond REQUEST_M
        key = (im.chain[0], 14, RK[0] * 4, RK[1] * 4)
        self.assertIsNone(far.pending_region(key, imagery.as_tile(np.zeros((256, 256, 3), np.uint8) + 7)))
        far.close()

    def test_gated_region_unchanged_status(self):
        root = tempfile.mkdtemp(dir=self.tmp.name)
        d = ds.Deshadow(root, FakeDem(flat=True), "full", wait_s=120)
        heard = []
        d.add_listener(lambda rk, changed: heard.append((rk, changed)))
        im = RegionImagery(os.path.join(root, "imagery"), deshadow=d, tone=0.0)
        self.addCleanup(im.close)
        d.prepare(*region_xy(1800, -4000))
        self.assertFalse(d.region(RK).params["ok"])     # waits for the worker
        for _ in range(200):
            if heard:
                break
            time.sleep(0.01)
        self.assertEqual(heard, [(RK, False)])          # gated off: tiles rendered raw stay right
        self.assertIs(d.status(RK), False)


# --- --deshadow off: the imagery as before ----------------------------------------------------
class SeededImagery(imagery.Imagery):
    """Seeded noise tiles from three sources, USGS only east of LON_SPLIT."""
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
    # sha256 of these renders by the imagery code without the de-shadow hook: the finished pyramid's (geotiles phase 3a,
    # 2026-10-29: the tone curve per source tile, kept as JPEGs; before it, commit 6465d26's a280901f / b9ef73bd)
    GOLDEN = {1.0: "f0b36dae73d66c9d15d056a6bfbf4b37aee3e0af42ccc53e1de4bdd208790b3c",
              0.0: "a8c2eedb16fa69f148c11b613099c8d7501e60a2a9cbef2fa1cc48f420a01e47"}

    def test_render_byte_identical_without_deshadow(self):
        for tone, digest in self.GOLDEN.items():
            self.assertEqual(golden(tone=tone), digest)
            self.assertEqual(golden(tone=tone, deshadow=None), digest)

    def test_server_default_is_off(self):
        import earthd
        with tempfile.TemporaryDirectory() as tmp:
            s = earthd.Server(tmp, os.path.join(tmp, "cache"))
            self.assertIsNone(s.deshadow)
            self.assertIsNone(s.imagery.deshadow)
            s.imagery.close()
            s2 = earthd.Server(tmp, os.path.join(tmp, "cache"), deshadow="full")
            self.assertIs(s2.imagery.deshadow, s2.deshadow)
            self.assertEqual(s2.deshadow.mode, "full")
            s2.imagery.close()
            s3 = earthd.Server(tmp, os.path.join(tmp, "cache"), imagery=("eox2023",), deshadow="full")
            self.assertIsNone(s3.deshadow)          # no USGS in the chain: nothing to de-shadow
            s3.imagery.close()
            with self.assertRaises(ValueError):
                ds.Deshadow(tmp, None, "off")

    def test_cli_flag_defaults_off(self):
        import earthd
        src = Path(earthd.__file__).read_text()
        self.assertIn('ap.add_argument("--deshadow", choices=deshadowmod.MODES, default="off"', src)
        self.assertEqual(ds.MODES[0], "off")


@unittest.skipUnless(os.environ.get("DESHADOW_TIMING") == "1", "timing: set DESHADOW_TIMING=1")
class TimingTest(unittest.TestCase):
    def test_per_tile_cost(self):
        """Region values precomputed: < 25 ms per shadow-edge tile, < 3 ms per unchanged tile
        (grid sampling included; neighbour assembly and the PNG write are not)."""
        S = scene()
        P, G = S.region()
        t = {}
        for rep in range(2):
            for ty in range(N_F // 256):
                for tx in range(N_F // 256):
                    rgb_m, _ = S.tile_input(G, tx, ty)
                    t0 = time.perf_counter()
                    terms = ds.sample_grids(G, 0, 0, 16, tx, ty, n_core=N_G)
                    _, path = ds.relight_tile(rgb_m, terms, P, 2)
                    if rep:
                        t.setdefault(path, []).append(time.perf_counter() - t0)
        flat = np.zeros((272, 272, 3), np.float32)
        flat[..., 0] = flat[..., 2] = 1
        flat[..., 1] = math.sin(math.radians(P["el"]))
        noop = []
        for _ in range(20):
            t0 = time.perf_counter()
            ds.sample_grids(G, 0, 0, 16, 0, 0, n_core=N_G)
            ds.unchanged(flat, P)
            noop.append(time.perf_counter() - t0)
        med = {k: round(1000 * float(np.median(v)), 1) for k, v in t.items()}
        print(f"\nper-tile ms (median): {med}, unchanged {1000 * float(np.median(noop)):.2f}")
        self.assertLess(med["edge"], 25.0)
        self.assertLess(1000 * float(np.median(noop)), 3.0)


if __name__ == "__main__":
    unittest.main()
