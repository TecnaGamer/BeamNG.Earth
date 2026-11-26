"""The generated ground's stage 5 (earthfast 314, ggshade.rs): the capture sun's shadows out of the Sentinel-2 colour.
The buildings' and canopy's cast shadows (a sweep over their heights, the receiver at its own height), the terrain's
(a bounded march on a DEM grid) and slope shading; a matte from the geometry and the image (no dark patch: nothing
changes); the relight; the unrecoverable texels lost (filled by gen_ground from the kind's lit texels); every block
alike (a texel's result from the data within the halo); the sun fit per data cell."""
import math
import os
import sys
import types
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import genground as gg  # noqa: E402
import geodesy  # noqa: E402

fast.require(gg.NEED, "generated ground")
EF = fast._ef


def sun(az_deg, el_deg, terrain=0.0, buildings=1.0, k=(0.4, 0.4, 0.4)):
    a, e = math.radians(az_deg), math.radians(el_deg)
    return [math.sin(a), math.cos(a), math.tan(e), math.sin(e), math.cos(e), terrain, buildings, *k]


def scene(n=200, h=20.0, dark=60, lit=150, k=0.4):
    """A 10 x 10 m building of height h in a lawn of grey lit, its shadow under a sun from the east at 45 degrees
    (h m to the west) darkened to dark."""
    rgb = np.full((n, n, 3), lit, np.uint8)
    kind = np.full((n, n), gg.GRASS, np.uint8)
    occ = np.zeros((n, n), np.float32)
    c = n // 2
    occ[c - 5:c + 5, c - 5:c + 5] = h
    kind[c - 5:c + 5, c - 5:c + 5] = gg.ROOF
    rgb[c - 5:c + 5, c - 5:c + 5] = (200, 80, 70)
    if dark is not None:
        rgb[c - 5:c + 5, c - 5 - int(h) + 2:c - 5] = dark
    return rgb, kind, occ


def relight(rgb, kind, occ, suns, cell=None, dem=None, r0=0, c0=0, step=16, steps=96, t=1.0, gr=0, gc=0):
    if occ.shape == kind.shape:         # (no margin given: none there)
        m = gg.shade_margin(t)
        occ = np.pad(occ, m)
    out = np.array(rgb, copy=True)
    cell = np.zeros(kind.shape, np.uint8) if cell is None else cell
    lost = EF.gg_relight(out, kind, occ, cell, np.asarray(suns, np.float32), gg.RELIT, t, dem, r0, c0, step, steps,
                         gg.SHADE_REACH_M, gr, gc)
    return out, lost


class RelightTest(unittest.TestCase):
    def test_building_shadow_relit_roof_and_sunny_side_untouched(self):
        rgb, kind, occ = scene()
        out, lost = relight(rgb, kind, occ, [sun(90, 45)])
        c = 100
        sh = out[c - 4:c + 4, c - 20:c - 7].astype(float)
        self.assertAlmostEqual(sh.mean(), 150, delta=15, msg="the shadow lit like the lawn round it")
        np.testing.assert_array_equal(out[:, c + 6:], rgb[:, c + 6:], "the sunny side's texels, their very bytes")
        np.testing.assert_array_equal(out[c - 5:c + 5, c - 5:c + 5], rgb[c - 5:c + 5, c - 5:c + 5], "the roof")
        self.assertFalse(lost.any())

    def test_no_dark_patch_in_the_image_no_change(self):
        rgb, kind, occ = scene(dark=None)
        out, lost = relight(rgb, kind, occ, [sun(90, 45)])
        np.testing.assert_array_equal(out, rgb, "the geometry alone never darkens or lights anything")
        self.assertFalse(lost.any())

    def test_no_sun_no_change(self):
        rgb, kind, occ = scene()
        out, _ = relight(rgb, kind, occ, [sun(90, 45)], cell=np.full(kind.shape, 255, np.uint8))
        np.testing.assert_array_equal(out, rgb)

    def test_clipped_umbra_lost_and_filled_by_gen_ground(self):
        rgb, kind, occ = scene(dark=4)
        out, lost = relight(rgb, kind, occ, [sun(90, 45)])
        c = 100
        self.assertTrue(lost[c - 3:c + 3, c - 18:c - 8].all(), "too dark to recover: lost, not boosted")
        region = np.where(lost > 0, kind.astype(np.uint32) | np.uint32(gg.LOST), kind.astype(np.uint32))
        canopy = np.full(kind.shape, 255, np.uint8)
        g = EF.gen_ground(out, kind, region, canopy, np.zeros(kind.shape, np.uint32), np.zeros((0, 2)), np.zeros(0),
                          0.0, 0.0, 1.0, 0, 0, None, 0).astype(float)
        ref = EF.gen_ground(np.full_like(rgb, 150), kind, kind.astype(np.uint32), canopy,
                            np.zeros(kind.shape, np.uint32), np.zeros((0, 2)), np.zeros(0), 0.0, 0.0, 1.0, 0, 0,
                            None, 0).astype(float)
        self.assertAlmostEqual(g[c - 3:c + 3, c - 18:c - 8].mean(), ref[c - 3:c + 3, c - 18:c - 8].mean(), delta=12,
                               msg="the lawn's lit colour round it")

    def test_every_block_alike(self):
        # two overlapping windows of a city (their occluders shade_margin further out) agree on their common texels
        # at least HALO_M inside both
        rng = np.random.default_rng(5)
        n = 900
        occ = np.zeros((n, n), np.float32)
        kind = np.full((n, n), gg.GRASS, np.uint8)
        for _ in range(140):
            r, c = rng.integers(0, n - 30, 2)
            occ[r:r + 20, c:c + 20] = rng.integers(5, 300)
            kind[r:r + 20, c:c + 20] = gg.ROOF
        rgb = rng.integers(40, 200, (n, n, 3)).astype(np.uint8)
        rgb[occ == 0] //= 2
        s = [sun(130, 35)]
        m = gg.shade_margin(1.0)
        a0, b0, w = 150, 250, 500

        def win(o):
            return relight(rgb[o:o + w, o:o + w], kind[o:o + w, o:o + w], occ[o - m:o + w + m, o - m:o + w + m], s,
                           gr=o, gc=o)
        a, la = win(a0)
        b, lb = win(b0)
        self.assertTrue((a != rgb[a0:a0 + w, a0:a0 + w]).any(), "(something relit)")
        h, d = 128, b0 - a0
        np.testing.assert_array_equal(a[d + h:w - h, d + h:w - h], b[h:w - d - h, h:w - d - h])
        np.testing.assert_array_equal(la[d + h:w - h, d + h:w - h], lb[h:w - d - h, h:w - d - h])

    def test_terrain_cast_shadow_and_alignment(self):
        # a ridge 200 m high east of a valley floor, the sun low in the east: the floor next to it in its shadow,
        # relit where the image is dark there; two windows on the same global node grid agree
        n, step = 400, 16
        rows = cols = (n + 2 * 512) // step + 1
        r0 = c0 = -512
        jj = c0 + step * np.arange(cols)
        dem = np.tile(np.where(jj > 300, 200.0, 0.0).astype(np.float32), (rows, 1))
        rgb = np.full((n, n, 3), 140, np.uint8)
        rgb[:, 200:290] = 50                                # (the ridge's shadow: 200 m west of it at 45 deg)
        kind = np.full((n, n), gg.GRASS, np.uint8)
        occ = np.zeros((n, n), np.float32)
        s = [sun(90, 45, terrain=2.0, buildings=0.0)]
        out, _ = relight(rgb, kind, occ, s, dem=np.ascontiguousarray(dem), r0=r0, c0=c0, step=step, steps=32)
        self.assertGreater(out[:, 220:280].mean(), 110, "the ridge's shadow relit")
        self.assertLess(abs(out[:, 40:120].astype(float).mean() - 140), 3, "the open floor as it was")


class FitTest(unittest.TestCase):
    def test_candidates(self):
        cands, meta = gg.sun_candidates(40.758)
        self.assertGreater(len(cands), 30)
        self.assertTrue(all(100 <= d <= 260 for _h, d in meta), "(the northern summer half: the composite's days)")
        el = np.degrees(np.arctan(cands[:, 2]))
        self.assertTrue((el >= gg.EL_MIN_DEG - 0.01).all())
        np.testing.assert_allclose(np.hypot(cands[:, 0], cands[:, 1]), 1.0, atol=1e-5)
        i = min(range(len(meta)), key=lambda j: abs(meta[j][1] - 169) + 10 * abs(meta[j][0] - 10.5))
        az = math.degrees(math.atan2(cands[i, 0], cands[i, 1])) % 360
        self.assertAlmostEqual(el[i], 63, delta=4, msg="Manhattan, late June, 10:30: high in the south-east")
        self.assertAlmostEqual(az, 122, delta=8)
        again, _ = gg.sun_candidates(40.758)
        np.testing.assert_array_equal(cands, again)

    def test_fit_finds_the_sun_of_the_shadows(self):
        n = 160
        occ = np.zeros((n, n), np.float32)
        rgb = np.full((n, n, 3), 150, np.uint8)
        for r in range(10, n - 20, 25):
            for c in range(20, n - 20, 25):
                occ[r:r + 5, c:c + 5] = 24.0                # (24 m: 6 texels of shadow at 45 degrees, 4 m texels)
                rgb[r:r + 5, c - 6:c] = 50                  # (to the west: a sun from the east)
        cands = np.asarray([sun(270, 45)[:5], sun(90, 45)[:5], sun(180, 45)[:5]], np.float32)
        scores, _ = EF.gg_sun_fit(None, 32.0, 0, None, occ, 4.0, rgb, cands, gg.SHADE_REACH_M, 0, False)
        self.assertEqual(int(np.argmax(scores[:, 1])), 1)
        self.assertGreater(scores[1, 1], 0.8)
        _, sums = EF.gg_sun_fit(None, 32.0, 0, None, occ, 4.0, rgb, cands[1:2], gg.SHADE_REACH_M, 0, True)
        self.assertGreater(sums[0][3], 0)
        k = sums[0][0] * sums[0][7] / (sums[0][4] * sums[0][3])
        self.assertAlmostEqual(k, 50 / 150, delta=0.05, msg="the shadow / lit ratio across the edges")


class PendingFitTest(unittest.TestCase):
    def setUp(self):
        gg._retries.clear()
        gg._fits.clear()
        self.addCleanup(gg._retries.clear)
        self.addCleanup(gg._fits.clear)

    def test_fit_with_a_tile_the_block_already_waits_for(self):
        # (Codex review of a8da6b7b, high) the block's photo is rendered first: a tile missing for both is already in
        # the caller's pending set, so the fit's render adds nothing new; it must still count as partial and be made
        # again once the tile is here
        from unittest import mock
        TILE = ("lazy", 14, 1, 2)
        state = {"missing": True}

        class Im:
            def render(self, frame, x0, y0, size, px, pending=None):
                if state["missing"] and pending is not None:
                    pending.add(TILE)
                return np.full((px, px, 3), 120, np.uint8), {"eox2016": px * px}

        key = gg.mapcells.cell_of(40.758, -73.985)
        now = [1000.0]
        with mock.patch.object(gg, "_now", lambda: now[0]):
            pending, missing = {TILE}, []
            _row, info = gg.cell_sun(key, Im(), None, None, pending, missing)
            self.assertTrue(info["partial"], "the tile still to come: a partial fit")
            self.assertIn((key, gg.SHADE_LAYER), missing)
            self.assertIn(TILE, pending)
            state["missing"] = False
            now[0] += gg.FIT_PARTIAL_S + 1
            missing = []
            _row, info = gg.cell_sun(key, Im(), None, None, set(), missing)
            self.assertFalse(info["partial"], "made again once it is here: final")
            self.assertEqual(missing, [])


class ReadyTest(unittest.TestCase):
    def setUp(self):
        gg._retries.clear()
        self.addCleanup(gg._retries.clear)

    def test_data_ready_waits_for_the_dem(self):
        frame = geodesy.EnuFrame(46.53, 10.45)
        md = types.SimpleNamespace(has=lambda k, layer: True)
        asked = []
        dem = types.SimpleNamespace(has=lambda z, x, y: False, prefetch=lambda keys: asked.extend(keys),
                                    sample=None)
        self.assertFalse(gg.data_ready(md, frame, -500.0, -500.0, 1000.0, None, None, dem))
        have = types.SimpleNamespace(has=lambda z, x, y: True, prefetch=None, sample=None)
        self.assertTrue(gg.data_ready(md, frame, -500.0, -500.0, 1000.0, None, None, have))
        self.assertTrue(gg.data_ready(md, frame, -500.0, -500.0, 1000.0, None, None, None))


if __name__ == "__main__":
    unittest.main()
