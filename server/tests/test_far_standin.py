"""A block's stand-in photo from the far tiles' textures on disk (earthfast.far_standin, earthd.Server.write_quick_base;
2026-11-22): a block that came before its photo showed the level's flat brown base, or the far tiles at their own height
over the drivable block (the user, 2026-11-21). The stand-in now draws the far field's own textures onto the block."""
import os
import sys
import tempfile
import threading
import types
import unittest
from collections import OrderedDict, deque

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import earthd  # noqa: E402
import farfield as ff  # noqa: E402
import fast  # noqa: E402
import geodesy  # noqa: E402
import imagery  # noqa: E402
import terraincache  # noqa: E402

LAYOUT = ff.Layout(64, 1024, True)


def world_picture(px, fx0, fy0, fs, blue):
    """A far texture of footprint [fx0, fx0 + fs]² (its gutter too) whose red / green are the texel's world x / y
    (mod 256) and blue a tag: what a texel of the stand-in shows says where it was sampled."""
    g = imagery.gutter(px)
    t = fs / (px - 2 * g)
    xs = fx0 - g * t + (np.arange(px) + 0.5) * t
    ys = fy0 + fs + g * t - (np.arange(px) + 0.5) * t
    X, Y = np.meshgrid(xs, ys)
    return np.stack([X % 256, Y % 256, np.full_like(X, blue)], -1).clip(0, 255).astype(np.uint8)


class KernelTest(unittest.TestCase):
    def setUp(self):
        fast.require(597, "far tile stand-in")
        self.dir = tempfile.mkdtemp()

    def tex(self, name, px, fx0, fy0, fs, blue, srgb=False):
        p = os.path.join(self.dir, name)
        imagery.write_dds(p, world_picture(px, fx0, fy0, fs, blue), srgb=srgb)
        return (p, float(fx0), float(fy0), float(fs))

    def test_finer_tiles_over_coarser_at_their_place(self):
        coarse = self.tex("c.dds", 256, 0, 0, 256, 40)
        fine = [self.tex(f"f{i}{j}.dds", 256, 128 * i, 128 * j, 128, 80 + 40 * (2 * j + i), srgb=bool(i))
                for i, j in ((0, 0), (1, 0), (0, 1))]
        rgb, n = fast._ef.far_standin([coarse] + fine, 0.0, 0.0, 256.0, 256)
        self.assertEqual(n, 256 * 256)
        c = np.arange(256) + 0.5
        X, Y = np.meshgrid(c, 256 - c)
        err = np.abs(rgb[..., :2].astype(int) - np.stack([X, Y], -1) % 256)[3:-3, 3:-3]
        self.assertLessEqual(err.max(), 8, "every texel sampled where it lies (BC1's error)")
        # rows from the north: the NE quarter has the coarse tile alone
        quads = {"SW": rgb[200, 50, 2], "SE": rgb[200, 200, 2], "NW": rgb[50, 50, 2], "NE": rgb[50, 200, 2]}
        for k, want in {"SW": 80, "SE": 120, "NW": 160, "NE": 40}.items():
            self.assertLessEqual(abs(int(quads[k]) - want), 8, (k, quads))

    def test_coverage_holes_and_under(self):
        t = self.tex("a.dds", 128, 0, 0, 128, 100)
        rgb, n = fast._ef.far_standin([t], -128.0, -128.0, 512.0, 128)
        self.assertEqual(n, 32 * 32, "a quarter of the square's side")
        self.assertTrue((rgb[..., 2].astype(int) - 100 <= 8).all() and (rgb[..., 2] > 0).all(),
                        "holes filled from round them")
        under = np.full((128, 128, 3), 7, np.uint8)
        rgb2, n2 = fast._ef.far_standin([t], -128.0, -128.0, 512.0, 128, under)
        self.assertEqual(n2, n)
        self.assertTrue((rgb2[0, 0] == 7).all() and (rgb2[127, 127] == 7).all(), "under kept where no tile is")
        self.assertTrue(np.array_equal(rgb2[64:96, 32:64], rgb[64:96, 32:64]), "the tile's texels the same")

    def test_missing_or_foreign_files_are_skipped(self):
        bad = os.path.join(self.dir, "x.dds")
        with open(bad, "wb") as f:
            f.write(b"DDS " + b"\0" * 200)
        tiles = [(os.path.join(self.dir, "gone.dds"), 0.0, 0.0, 128.0), (bad, 0.0, 0.0, 128.0)]
        rgb, n = fast._ef.far_standin(tiles, 0.0, 0.0, 128.0, 64)
        self.assertEqual(n, 0)
        self.assertFalse(rgb.any())

    def test_deterministic(self):
        tiles = [self.tex(f"d{i}.dds", 256, 128 * i, 0, 128, 50 + i) for i in range(2)]
        a = fast._ef.far_standin(tiles, 3.0, -50.0, 300.0, 512)
        b = fast._ef.far_standin(tiles, 3.0, -50.0, 300.0, 512)
        self.assertEqual(a[1], b[1])
        self.assertTrue(np.array_equal(a[0], b[0]))


def server(out, tiles, render=None):
    """A stand-in server for write_quick_base: its far tiles' cache (ck -> TileInfo-like), no recent photos."""
    def no_render(*a, **k):
        raise AssertionError("the imagery pass ran")
    s = types.SimpleNamespace(
        layout=LAYOUT, base_px=256, base_lock=threading.Lock(), recent_full=deque(), out_dir=out, user_dir=out,
        vfs_dir="/temp/beamngEarth/s1", block_pools=[], deshadow=None, tiles=tiles, near_visual="detail",
        imagery=types.SimpleNamespace(render=render or no_render))
    s.hm_stem = lambda key: earthd.Server.hm_stem(s, key)
    s.quick_base_name = lambda key: earthd.Server.quick_base_name(s, key)
    s.far_standin_tiles = lambda *a: earthd.Server.far_standin_tiles(s, *a)
    s.write_quick_base = lambda *a, **k: earthd.Server.write_quick_base(s, *a, **k)
    return s


class WriteQuickBaseTest(unittest.TestCase):
    def setUp(self):
        fast.require(597, "far tile stand-in")
        self.out = tempfile.mkdtemp()
        self.anchor = types.SimpleNamespace(id=1, frame=geodesy.EnuFrame(39.28, -76.6))

    def far_tiles(self, level, keys, aid=1):
        tiles = OrderedDict()
        for i, j in keys:
            t = ff.Tile(level, i, j)
            name = f"far_{aid}_{t.key}.dds"
            imagery.write_dds(os.path.join(self.out, name), world_picture(64, *t.bbox[:2], t.size, 90))
            tiles[(aid, t.key)] = earthd.TileInfo(pos=list(t.bbox[:2]), err=0.0, sink=None, skirt=0.0, grid=None,
                                                  bytes=0, seconds=0.0, texture=name, tile=t)
        return tiles

    def test_far_textures_alone_when_they_cover_the_block(self):
        size = LAYOUT.block * earthd.heightmap.SQUARE
        n = int(round(size / ff.level_size(-1))) + 1     # (the photo square is BASE_SHIFT_M off the block's)
        tiles = self.far_tiles(-1, [(i, j) for i in range(-n // 2 - 1, n // 2 + 1) for j in range(-n // 2 - 1, n // 2 + 1)])
        tiles.update(self.far_tiles(-1, [(0, 0)], aid=2))                   # (another anchor's: not used)
        s = server(self.out, tiles)
        name = earthd.Server.write_quick_base(s, self.anchor, 0.0, 0.0, far_only=True)
        self.assertEqual(name, "hm_1_0_0_base_q" + terraincache.PHOTO_EXT)
        self.assertTrue(os.path.exists(os.path.join(self.out, name)))
        self.assertTrue(os.listdir(terraincache.cache_dir(self.out)), "packed for the game (no runtime cook)")
        self.assertIsNotNone(earthd.Server._far_quick(s, self.anchor, 0.0, 0.0))

    def test_far_only_declines_a_bare_block(self):
        s = server(self.out, self.far_tiles(-1, [(0, 0)]))
        self.assertIsNone(earthd.Server.write_quick_base(s, self.anchor, 0.0, 0.0, far_only=True))
        self.assertIsNone(earthd.Server.write_quick_base(server(self.out, OrderedDict()), self.anchor, 0.0, 0.0,
                                                         far_only=True))

    def test_imagery_holes_filled_from_the_far_textures(self):
        """The imagery pass with texels no source has (its brown fill): the far textures' then, never None while they
        cover some of it."""
        def render(frame, x0, y0, size, px, pending):
            return np.zeros((px, px, 3), np.uint8), {"usgs": px * px - 1, "none": 1}
        s = server(self.out, self.far_tiles(-1, [(0, 0), (-1, 0)]), render)
        self.assertIsNotNone(earthd.Server.write_quick_base(s, self.anchor, 0.0, 0.0))
        self.assertIsNone(earthd.Server.write_quick_base(server(self.out, OrderedDict(), render), self.anchor, 0.0, 0.0),
                          "nothing to draw it from: none, as before")

    def test_far_texels_over_the_imagery_pass(self):
        calls = []

        def render(frame, x0, y0, size, px, pending):
            calls.append(px)
            return np.full((px, px, 3), 3, np.uint8), {"usgs": px * px}
        s = server(self.out, self.far_tiles(-1, [(0, 0)]), render)
        name = earthd.Server.write_quick_base(s, self.anchor, 0.0, 0.0)
        self.assertEqual(calls, [earthd.QUICK_BASE_PX])
        rgb = np.asarray(earthd.Image.open(os.path.join(self.out, name)))
        self.assertGreater(int(rgb[..., 2].max()), 60, "the far tile's texels")
        self.assertLess(int(np.median(rgb[..., 2])), 20, "the imagery pass elsewhere")


if __name__ == "__main__":
    unittest.main()
