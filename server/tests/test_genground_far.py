"""The generated ground's far rings (genground.render_far): the far tiles up to FAR_MAX_T m texels are cut from the
generated ground of FAR_SQ_M squares of the anchor grid (the block's colours at the seam, the user's near / far border
at Patapsco); coarser ones and the switch off: the plain photo (None)."""
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import genground as gg  # noqa: E402
import geodesy  # noqa: E402


class _Im:
    """A photo that is a function of the place (so every square agrees where they meet)."""

    def render(self, frame, x0, y0, size, px, pending=None):
        t = size / px
        k = (np.arange(px) + 0.5) * t
        x, y = np.meshgrid(x0 + k, y0 + size - k)
        r = 90 + 40 * np.sin(x / 37.0) + 20 * np.cos(y / 53.0)     # det-ok: a test's photo
        g = 110 + 30 * np.sin((x + y) / 71.0)                       # det-ok: a test's photo
        return np.stack([r, g, np.full_like(r, 70)], 2).clip(0, 255).astype(np.uint8), {"eox2023": px * px}


class FarTest(unittest.TestCase):
    def setUp(self):
        gg._far_sq.clear()
        self.addCleanup(gg._far_sq.clear)
        p = mock.patch.object(gg, "ON", True)
        p.start()
        self.addCleanup(p.stop)

    def test_cut_from_the_squares(self):
        frame = geodesy.EnuFrame(40.877, -77.566)
        im = _Im()
        sq, _ = gg.render(im, frame, 0.0, 0.0, gg.FAR_SQ_M, 512, None, None, relight=False)
        out, used = gg.render_far(im, frame, 0.0, 0.0, 512.0, 256, None, None)
        np.testing.assert_array_equal(out, sq[256:, :256], "the square's own texels (2 m)")
        self.assertEqual(sum(used.values()), 256 * 256)
        # a tile across four squares: the squares' texels, continuous across their edges
        a, _ = gg.render_far(im, frame, 768.0, 768.0, 512.0, 256, None, None)
        sq11, _ = gg.render(im, frame, 1024.0, 1024.0, gg.FAR_SQ_M, 512, None, None, relight=False)
        np.testing.assert_array_equal(a[:128, 128:], sq11[384:, :128])

    def test_texel_sizes_and_off(self):
        self.assertEqual(gg.far_texel(256.0, 256), 2.0, "L0 (1 m): from the 2 m squares")
        self.assertIsNone(gg.far_strength(2), "L2 on: the plain photo (the fade ends at L1)")
        self.assertEqual(gg.far_strength(-1), 1.0)
        self.assertIsNone(gg.far_texel(4096.0, 512), "L4 (8 m): the plain photo")
        frame = geodesy.EnuFrame(40.877, -77.566)
        self.assertIsNone(gg.render_far(_Im(), frame, 0.0, 0.0, 4096.0, 512, None, None))
        with mock.patch.object(gg, "ON", False):
            self.assertIsNone(gg.render_far(_Im(), frame, 0.0, 0.0, 512.0, 256, None, None), "off: unchanged")
            self.assertIsNone(gg.far_key(None, True))
        self.assertEqual(gg.far_key(None, True)[0], "genfar")

    def test_far_floor_by_the_drawn_share(self):
        # (the trees as the mod draws them at the distance: no vegetation read; the floor's weight by FAR_SHARE)
        frame = geodesy.EnuFrame(40.877, -77.566)
        seen = {}
        real = gg.fast._ef.gen_ground

        def spy(*a):
            seen.setdefault("floor", []).append((None if a[12] is None else int(np.max(a[12])), a[13]))
            return real(*a)
        real_pair = getattr(gg.fast._ef, "gen_ground_pair", None)

        def pair_spy(*a):           # (the ground and its canopy version in one pass: as the two calls it replaces)
            seen.setdefault("floor", []).append((None if a[12] is None else int(np.max(a[12])), a[13]))
            seen["floor"].append((0, 2) if a[13] == 2 else (None, 0))
            return real_pair(*a)
        canopy = gg.canopy_grid

        def tall(mx, my, rasters):
            out = canopy(mx, my, rasters)
            out[:] = 20
            return out
        import contextlib
        pair = mock.patch.object(gg.fast._ef, "gen_ground_pair", pair_spy) if real_pair is not None else \
            contextlib.nullcontext()
        with mock.patch.object(gg.fast._ef, "gen_ground", spy), mock.patch.object(gg, "canopy_grid", tall), pair:
            gg.render_far(_Im(), frame, 0.0, 0.0, 128.0, 256, None, None, veg=True, level=-1)
            gg.render_far(_Im(), frame, 2048.0, 0.0, 512.0, 256, None, None, veg=True, level=1)
            gg.render_far(_Im(), frame, 4096.0, 0.0, 512.0, 256, None, None, veg=None, level=0)
        self.assertEqual(seen["floor"][0], (255, 2), "L-1, next to the block: its full floor")
        self.assertEqual(seen["floor"][1], (0, 2), "... and its canopy version (the coarser mips', write_far_dds)")
        self.assertEqual(seen["floor"][2], (255, 2), "L1: the same floor (the fade is in the mips)")
        self.assertEqual(seen["floor"][-1], (None, 0), "no trees drawn: their own colour, as the block")
        self.assertIsNone(gg.render_far(_Im(), frame, 0.0, 0.0, 1024.0, 512, None, None, veg=True, level=2),
                          "L2: the plain photo")

    def test_far_squares_never_ask_for_trees(self):
        frame = geodesy.EnuFrame(40.877, -77.566)
        asked = []

        class Veg:
            def ready(self, key):
                return False

            def build_soon(self, key):
                asked.append(key)
        gg._retries.clear()
        self.addCleanup(gg._retries.clear)
        pending = set()
        gg.render_far(_Im(), frame, 0.0, 0.0, 512.0, 256, None, None, pending=pending, veg=Veg())
        self.assertEqual(asked, [], "a far square takes the warm-up's cells, never queues its own")
        self.assertNotIn(gg.GEN_PENDING, pending, "no GEN_PENDING on a far tile")


class MipFadeTest(unittest.TestCase):
    """The forest floor's fade with distance in the far textures' mips (genground.write_far_dds): each mip blends the
    floor and canopy versions by its texel's size on the ground; the GPU's mip choice by footprint makes it continuous
    across every tile and ring."""

    def setUp(self):
        p = mock.patch.object(gg, "ON", True)
        p.start()
        self.addCleanup(p.stop)

    def levels(self, data, w):
        import imagery
        info = imagery.read_dds_info(data)
        off = len(data) - sum(info[4])
        out = []
        for size in info[4]:
            out.append(data[off:off + size])
            off += size
        return out

    def chain(self, rgb):
        import fast
        import imagery
        lv = [rgb] + list(fast._ef.mip_levels_fast(rgb, imagery._TO_LIN, float(imagery.MIP_SHARPEN)))
        return [bytes(fast._ef.bc1_encode_fast(np.ascontiguousarray(m))) for m in lv]

    def test_weights_by_the_ground_texel(self):
        self.assertEqual(gg.fade_weights(0.5, 4), [1.0, 1.0, 0.0, 0.0], "0.5, 1 m: floor; 2, 4 m: canopy")
        mid = gg.fade_weights(1.4142135623730951, 1)[0]
        self.assertAlmostEqual(mid, 0.5, places=6)
        # a tile one level coarser (twice the texel) has, at each mip, the weight of the finer one's next mip: the
        # same footprint, the same weight, so neighbouring tiles of two levels agree where they meet
        for t in (0.25, 0.5, 0.75, 1.0, 1.5):
            self.assertEqual(gg.fade_weights(2 * t, 5), gg.fade_weights(t, 6)[1:])

    def test_mips_floor_then_canopy_and_identity(self):
        import imagery
        n = 64
        floor = np.full((n, n, 3), (150, 140, 95), np.uint8)
        canopy = np.full((n, n, 3), (40, 60, 35), np.uint8)
        plain = imagery.encode_dds(floor, True)
        self.assertEqual(imagery.encode_dds(floor, True, fade=(floor, 0.5)), plain,
                         "canopy == floor: byte for byte the plain encoding (the container as BNE_FAR_SRGB writes it)")
        data = imagery.encode_dds(floor, True, fade=(canopy, 0.5))
        self.assertEqual(data[:len(plain) - sum(imagery.read_dds_info(plain)[4])],
                         plain[:len(plain) - sum(imagery.read_dds_info(plain)[4])], "the same header")
        got, f, c = self.levels(data, n), self.chain(floor), self.chain(canopy)
        self.assertEqual(got[0], f[0])
        self.assertEqual(got[1], f[1], "1 m: still the floor")
        self.assertEqual(got[2:], c[2:], "2 m and coarser: the canopy's colour (as the plain photo's levels)")

    def test_painted_texels_kept_and_switch_off(self):
        import imagery
        n = 64
        floor = np.full((n, n, 3), (150, 140, 95), np.uint8)
        canopy = np.full((n, n, 3), (40, 60, 35), np.uint8)
        painted = floor.copy()
        painted[:, :16] = (90, 90, 90)              # (a road painted over the tile after its render)
        gg._fade_tl.last = (floor, canopy, 2.0)
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.dds")
            gg.write_far_dds(path, painted, True)
            data = open(path, "rb").read()
            road_canopy = canopy.copy()
            road_canopy[:, :16] = (90, 90, 90)
            self.assertEqual(data, imagery.encode_dds(painted, True, fade=(road_canopy, 2.0)),
                             "the road in every mip, the woods faded")
            self.assertIsNone(gg._fade_tl.last, "(used once)")
            gg.write_far_dds(path, painted, True)
            self.assertEqual(open(path, "rb").read(), imagery.encode_dds(painted, True), "no render_far: plain")
            gg._fade_tl.last = (floor, canopy, 2.0)
            with mock.patch.object(gg, "ON", False):
                gg.write_far_dds(path, painted, True)
            self.assertEqual(open(path, "rb").read(), imagery.encode_dds(painted, True), "off: unchanged")

    def test_render_far_gives_the_canopy_version(self):
        frame = geodesy.EnuFrame(40.877, -77.566)

        class Dark(_Im):
            def render(self, frame, x0, y0, size, px, pending=None):
                return np.full((px, px, 3), (40, 60, 35), np.uint8), {"eox2023": px * px}
        canopy = gg.canopy_grid

        def tall(mx, my, rasters):
            out = canopy(mx, my, rasters)
            out[:] = 20
            return out
        gg._far_sq.clear()
        self.addCleanup(gg._far_sq.clear)
        with mock.patch.object(gg, "canopy_grid", tall):
            gg.clear_fade()
            rgb, _ = gg.render_far(Dark(), frame, 0.0, 0.0, 512.0, 256, None, None, veg=True, level=0)
            fl, cn, t = gg._fade_tl.last
            gg.clear_fade()
            gg.render_far(Dark(), frame, 0.0, 0.0, 512.0, 256, None, None, veg=None, level=0)
            self.assertIsNone(gg._fade_tl.last, "no trees drawn: nothing to fade")
        self.assertIs(fl, rgb)
        self.assertEqual(t, 2.0)
        self.assertGreater(float(rgb.mean()), float(cn.mean()) + 10, "the floor lighter than the canopy version")


class CodexReviewTest(unittest.TestCase):
    """Codex review of 899a9f75..a3c7895f."""

    def setUp(self):
        gg._far_sq.clear()
        self.addCleanup(gg._far_sq.clear)
        p = mock.patch.object(gg, "ON", True)
        p.start()
        self.addCleanup(p.stop)

    def test_raw_and_finished_squares_never_shared(self):
        # [high] a raw (driving, quick) and a finished-only imagery made the same square key: raw colours stored as the
        # finished tile's
        import types

        class Im(_Im):
            def __init__(self, fin, sigs, value):
                self.chain = [types.SimpleNamespace(name="eox2023")]
                self.tone, self.leaf_on, self.season_mode = "auto", True, "off"
                self.finished_only, self._fin_sigs, self.value = fin, sigs, value

            def render(self, frame, x0, y0, size, px, pending=None):
                return np.full((px, px, 3), self.value, np.uint8), {"eox2023": px * px}
        frame = geodesy.EnuFrame(40.877, -77.566)
        raw, _ = gg.render_far(Im(False, {}, 60), frame, 0.0, 0.0, 512.0, 256, None, None)
        fin, _ = gg.render_far(Im(True, {"eox2023": "abc"}, 160), frame, 0.0, 0.0, 512.0, 256, None, None)
        other, _ = gg.render_far(Im(True, {"eox2023": "def"}, 200), frame, 0.0, 0.0, 512.0, 256, None, None)
        self.assertNotEqual(int(raw.mean()), int(fin.mean()), "the finished imagery's own square")
        self.assertNotEqual(int(fin.mean()), int(other.mean()), "other finished settings: their own square")

    def test_main_process_texture_key_has_the_trees(self):
        # [high] the main process's far texture key (Server.tex_key) left out whether the trees are drawn, which
        # render_far's pixels depend on
        import types
        import earthd
        im = types.SimpleNamespace(chain=[types.SimpleNamespace(name="eox2023")], leaf_on=True, season_mode="off")
        md = types.SimpleNamespace(versions=lambda buildings=False: {"overture": "x"})

        def server(veg):
            return types.SimpleNamespace(imagery=im, tone="auto", tex_modes=(), road_masks=False, mapdata=md,
                                         road_widths=None, surface=None, layout=types.SimpleNamespace(cells=64),
                                         veg=veg, road_mask_px=lambda lvl: 0)
        anchor = types.SimpleNamespace(lat=40.877, lon=-77.566, zooms={k: 16 - k for k in range(9)})
        near = types.SimpleNamespace(key=(1, 0, 0), level=1, size=512.0, bbox=(0.0, 0.0, 512.0, 512.0))
        a = earthd.Server.tex_key(server(None), anchor, near, 256)
        b = earthd.Server.tex_key(server(object()), anchor, near, 256)
        self.assertNotEqual(a, b, "trees drawn or not: another texture")
        far = types.SimpleNamespace(key=(5, 0, 0), level=5, size=8192.0, bbox=(0.0, 0.0, 8192.0, 8192.0))
        self.assertEqual(earthd.Server.tex_key(server(None), anchor, far, 512),
                         earthd.Server.tex_key(server(object()), anchor, far, 512), "(past render_far's levels: as before)")
        with mock.patch.object(gg, "ON", False):
            self.assertEqual(earthd.Server.tex_key(server(None), anchor, near, 256),
                             earthd.Server.tex_key(server(object()), anchor, near, 256), "(off: as before)")


if __name__ == "__main__":
    unittest.main()
