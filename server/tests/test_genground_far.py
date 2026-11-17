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
        self.assertEqual(gg.far_texel(2048.0, 512), 4.0, "L3 (4 m)")
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
        canopy = gg.canopy_grid

        def tall(mx, my, rasters):
            out = canopy(mx, my, rasters)
            out[:] = 20
            return out
        with mock.patch.object(gg.fast._ef, "gen_ground", spy), mock.patch.object(gg, "canopy_grid", tall):
            gg.render_far(_Im(), frame, 0.0, 0.0, 512.0, 256, None, None, veg=True)
            gg.render_far(_Im(), frame, 2048.0, 0.0, 2048.0, 512, None, None, veg=True)
            gg.render_far(_Im(), frame, 4096.0, 0.0, 512.0, 256, None, None, veg=None)
        self.assertEqual(seen["floor"][0], (255, 2), "2 m squares: as the block (the trees drawn to 700 m)")
        self.assertEqual(seen["floor"][1], (255, 2), "4 m: the same (a level's own weight: a seam between levels)")
        self.assertEqual(seen["floor"][-1], (None, 0), "no trees drawn: their own colour, as the block")

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
