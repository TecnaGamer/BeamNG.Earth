"""The generated ground's forest floor fades out with distance on the near block too (bc7.dds fade,
terraincache.base_entry, earthd.Server.write_base / write_quick_base, blockworker.base_photo): each mip of the block's
packed base texture blends its floor and canopy versions by that mip's texel on the ground, as the far tiles' do
(genground.write_far_dds), so from high above the block no longer shows as a lighter square in large woods. The GPU
picks mips by footprint: the block and the far tiles meet at the same weight."""
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bc7  # noqa: E402
import genground as gg  # noqa: E402
import imagery  # noqa: E402
import terraincache  # noqa: E402

FLOOR = (150, 140, 95)
CANOPY = (40, 60, 35)


def bc7_levels(data):
    """[(w, h, blocks)] of a bc7.dds file's mips."""
    import struct
    h, w = struct.unpack_from("<2I", data, 12)
    n = struct.unpack_from("<I", data, 28)[0]
    off, out = 148, []
    for k in range(n):
        lw, lh = max(1, w >> k), max(1, h >> k)
        size = max(1, (lw + 3) // 4) * max(1, (lh + 3) // 4) * 16
        out.append((lw, lh, data[off:off + size]))
        off += size
    assert off == len(data)
    return out


def bc1_levels(data):
    info = imagery.read_dds_info(data)
    off, out = len(data) - sum(info[4]), []
    for k, size in enumerate(info[4]):
        out.append((max(1, info[0] >> k), max(1, info[1] >> k), data[off:off + size]))
        off += size
    return out


def rgba(rgb, ao=251):           # (earthd.BASE_AO)
    rgb = np.asarray(rgb, np.uint8)
    return np.concatenate([rgb, np.full(rgb.shape[:2] + (1,), ao, np.uint8)], 2)


def _grass(real):
    """genground.texel_fields with the land cover grass everywhere (no map data here: the canopy model's woods over
    open ground, where the game places no tree its unplaced gaps)."""
    def fields(*a, **k):
        f, complete = real(*a, **k)
        f = dict(f)
        f["base"] = np.full_like(np.asarray(f["base"]), gg.GRASS)
        return f, complete
    return fields


class Bc7FadeTest(unittest.TestCase):
    """bc7.dds fade: the far tiles' curve on the block's BC7 layout, its alpha (AO) as before."""

    def setUp(self):
        bc7._GPU_FAILED, self._gpu = True, bc7._GPU_FAILED        # (the CPU's encoder: the same blocks everywhere)
        self.addCleanup(setattr, bc7, "_GPU_FAILED", self._gpu)

    def test_identity_when_floor_is_canopy(self):
        rng = np.random.default_rng(7)
        img = rgba(rng.integers(0, 256, (64, 64, 3), np.uint8))
        plain = bc7.dds(img)
        self.assertEqual(bc7.dds(img, (img[:, :, :3], 0.5)), plain, "byte for byte the plain file")
        self.assertEqual(bc7.dds(img, (img, 0.25)), plain)

    def test_mips_floor_then_canopy_alpha_kept(self):
        n = 64
        floor = rgba(np.full((n, n, 3), FLOOR, np.uint8))
        canopy = np.full((n, n, 3), CANOPY, np.uint8)
        plain, cplain = bc7.dds(floor), bc7.dds(rgba(canopy))
        got = bc7.dds(floor, (canopy, 0.5))
        self.assertEqual(got[:148], plain[:148], "the same header (DX10, BC7_UNORM_SRGB, the mip count)")
        lv, f, c = bc7_levels(got), bc7_levels(plain), bc7_levels(cplain)
        self.assertEqual(len(lv), len(f))
        self.assertEqual(lv[0], f[0], "0.5 m: the floor")
        self.assertEqual(lv[1], f[1], "1 m: still the floor (FADE_NEAR_M)")
        self.assertEqual(lv[2:], c[2:], "2 m and coarser: the canopy's colour (FADE_FAR_M)")
        for (w, h, blocks), (_, _, fb) in zip(lv, f):
            np.testing.assert_array_equal(bc7.decode(blocks, w, h)[:, :, 3], bc7.decode(fb, w, h)[:, :, 3],
                                          "the AO alpha in every mip as the plain file's")

    def test_between_the_ends_and_painted_texels(self):
        n = 64
        floor = np.full((n, n, 3), FLOOR, np.uint8)
        floor[:, :16] = (90, 90, 90)                    # (a road painted after the render: in both versions)
        canopy = np.full((n, n, 3), CANOPY, np.uint8)
        canopy[:, :16] = (90, 90, 90)
        t = 0.7071067811865476                          # (mip 1: 1.414 m, half way in log2)
        lv = bc7_levels(bc7.dds(rgba(floor), (canopy, t)))
        self.assertEqual(gg.fade_weights(t, 3)[0], 1.0)
        self.assertAlmostEqual(gg.fade_weights(t, 3)[1], 0.5, places=6)
        m1 = bc7.decode(lv[1][2], lv[1][0], lv[1][1])[:, :, :3].astype(int)
        half = (np.array(FLOOR) + np.array(CANOPY)) / 2
        self.assertLess(np.abs(m1[4:, 12:] - half).max(), 4, "the woods half way")
        self.assertLess(np.abs(m1[:, :6] - 90).max(), 3, "the road as painted")

    def test_base_entry_writes_it(self):
        n = 64
        floor = np.full((n, n, 3), FLOOR, np.uint8)
        canopy = np.full((n, n, 3), CANOPY, np.uint8)
        with tempfile.TemporaryDirectory() as d:
            p0 = terraincache.base_entry(d, "/a/b.jpg", "/a/ao.png", floor, 251)
            plain = open(p0, "rb").read()
            p1 = terraincache.base_entry(d, "/a/b.jpg", "/a/ao.png", floor, 251, (canopy, 0.5))
            self.assertEqual(p1, p0, "the game's key unchanged")
            self.assertEqual(open(p1, "rb").read(), bc7.dds(rgba(floor), (canopy, 0.5)))
            self.assertNotEqual(open(p1, "rb").read(), plain)
            terraincache.base_entry(d, "/a/b.jpg", "/a/ao.png", floor, 251, (floor, 0.5))
            self.assertEqual(open(p0, "rb").read(), plain, "canopy == floor: the plain file")


class BlockMeetsFarTest(unittest.TestCase):
    """At the block's edge the GPU draws the block's mip and an L-1 far tile's mip of the same footprint: the same
    weight, the same colour, from any height (both chains' nodes, and the trilinear blend between them)."""

    def setUp(self):
        bc7._GPU_FAILED, self._gpu = True, bc7._GPU_FAILED
        self.addCleanup(setattr, bc7, "_GPU_FAILED", self._gpu)
        p = mock.patch.object(gg, "ON", True)
        p.start()
        self.addCleanup(p.stop)

    def test_weights_at_equal_footprint(self):
        import earthd
        block_t = 1024.0 / earthd.BASE_PX                     # (detail mode: the 1 km block at the photo's size)
        far_t = 0.5                                           # (an L-1 tile's texel, its gutter aside)
        bw = gg.fade_weights(block_t, 12)
        fw = gg.fade_weights(far_t, 12)
        for k, w in enumerate(bw):
            k2 = int(round(np.log2(block_t * (1 << k) / far_t)))
            if 0 <= k2 < len(fw):
                self.assertEqual(w, fw[k2], f"mip {k}: {block_t * (1 << k)} m")
        for px in (1024, 2048, 4096):         # (every ground photo size: its mips on the same 2^k grid of metres)
            t = 1024.0 / px
            ws = gg.fade_weights(t, 14)
            for k, w in enumerate(ws):
                ground = t * (1 << k)
                self.assertEqual(w, 1.0 if ground <= gg.FADE_NEAR_M else 0.0 if ground >= gg.FADE_FAR_M else w)
        # with its gutter an L-1 tile's texel is a little over 0.5 m (128 m over 256 - 2g texels: 0.533 m, +6.7 %): the
        # trilinear blends between the two chains' nodes then differ by at most that shift in log2 (~0.09 of the
        # floor-to-canopy step, at footprints near FADE_NEAR_M / FADE_FAR_M); the all-floor block differed by up to 1
        g = imagery.gutter(256)
        gt = 128.0 / (256 - 2 * g)
        bound = np.log2(gt / 0.5) / np.log2(gg.FADE_FAR_M / gg.FADE_NEAR_M) + 1e-9
        worst = old = 0.0
        for foot in np.exp2(np.linspace(-1.5, 2.5, 81)):
            def tri(t, ws):
                x = np.log2(foot / t)
                if x <= 0:
                    return ws[0]
                k = int(np.floor(x))
                return ws[k] + (ws[k + 1] - ws[k]) * (x - k)
            a = tri(block_t, gg.fade_weights(block_t, 16))
            b = tri(gt, gg.fade_weights(gt, 16))
            self.assertLessEqual(abs(a - b), bound, f"footprint {foot:.3f} m")
            worst, old = max(worst, abs(a - b)), max(old, abs(1.0 - b))
        self.assertLess(worst, 0.1)
        self.assertEqual(old, 1.0, "(before: the block all floor, the tiles all canopy from 2 m)")

    def test_colours_at_equal_texel(self):
        """The block's BC7 mip and a far tile's BC1 mip at the same ground texel decode to the same blend."""
        import tempfile as tf
        n = 64
        floor = np.full((n, n, 3), FLOOR, np.uint8)
        canopy = np.full((n, n, 3), CANOPY, np.uint8)
        blk = bc7_levels(bc7.dds(rgba(floor), (canopy, 0.5)))
        gg._fade_tl.last = (floor, canopy, 0.5)
        with tf.TemporaryDirectory() as d:
            path = os.path.join(d, "t.dds")
            gg.write_far_dds(path, floor, True)
            far = bc1_levels(open(path, "rb").read())
        from test_imagery import bc1_decode
        for k in range(5):
            a = bc7.decode(blk[k][2], blk[k][0], blk[k][1])[:, :, :3].astype(float).mean((0, 1))
            b = bc1_decode(far[k][2], far[k][0], far[k][1]).mean((0, 1))
            self.assertLess(np.abs(a - b).max(), 4, f"mip {k} ({0.5 * (1 << k)} m): {a} vs {b}")
        # the block of another photo size meets the tile at its own mip of that texel
        blk4 = bc7_levels(bc7.dds(rgba(np.full((128, 128, 3), FLOOR, np.uint8)),
                                  (np.full((128, 128, 3), CANOPY, np.uint8), 0.25)))
        for k in range(4):
            a = bc7.decode(blk4[k + 1][2], blk4[k + 1][0], blk4[k + 1][1])[:, :, :3].astype(float).mean((0, 1))
            b = bc1_decode(far[k][2], far[k][0], far[k][1]).mean((0, 1))
            self.assertLess(np.abs(a - b).max(), 4, f"{0.5 * (1 << k)} m")


class KeepPaintedTest(unittest.TestCase):
    def test_keep_painted(self):
        before = np.full((8, 8, 3), FLOOR, np.uint8)
        canopy = np.full((8, 8, 3), CANOPY, np.uint8)
        after = before.copy()
        after[2:4] = (90, 90, 90)
        got = gg.keep_painted(before, after, canopy)
        self.assertTrue((got[2:4] == 90).all(), "painted: as painted")
        self.assertTrue((got[:2] == CANOPY).all() and (got[4:] == CANOPY).all(), "the rest: the canopy's")
        self.assertIsNone(gg.keep_painted(before, after, None))
        self.assertIsNone(gg.canopy_if_differs(before, [before.copy()]), "no difference: nothing to fade")
        self.assertIsNone(gg.canopy_if_differs(before, []))
        self.assertIs(gg.canopy_if_differs(before, [canopy]).dtype, np.dtype(np.uint8))


class GenerateCanopyTest(unittest.TestCase):
    """render's canopy version for the block: its trees placed (mode 2) or not built yet (mode 1)."""

    def setUp(self):
        p = mock.patch.object(gg, "ON", True)
        p.start()
        self.addCleanup(p.stop)

    def run_mode(self, mode):
        import geodesy
        from test_genground_far import _Im
        frame = geodesy.EnuFrame(40.877, -77.566)
        real = gg.canopy_grid

        def tall(mx, my, rasters):
            out = real(mx, my, rasters)
            out[:] = 20
            return out

        def floor(frame, x0, y0, size, px, veg, missing):
            if mode == 1:
                return None, 1
            f = np.zeros((px, px), np.uint8)
            f[:, : px // 2] = 255               # (the game's trees on the west half)
            return f, 2
        cout = []
        with mock.patch.object(gg, "canopy_grid", tall), mock.patch.object(gg, "tree_floor", floor), \
                mock.patch.object(gg, "texel_fields", _grass(gg.texel_fields)):
            rgb, _ = gg.render(_Im(), frame, -64.5, -64.5, 128.0, 128, None, None, None, set(), True,
                               relight=False, canopy_out=cout)
        return rgb, cout

    def test_placed_trees(self):
        rgb, cout = self.run_mode(2)
        self.assertEqual(len(cout), 1)
        c = cout[0]
        self.assertEqual(c.shape, rgb.shape)
        d = (c != rgb).any(axis=2)
        self.assertTrue(d[:, :60].mean() > 0.9, "the floor under the placed trees: the canopy's colour there")
        self.assertGreater(float(np.abs(rgb[:, :60].astype(int) - c[:, :60]).mean()), 5, "the floor's colour")
        # the east half: the canopy model's woods where the game placed no tree (unplaced gaps): their gap colour (the
        # forest floor here) in the ground, the photo's own in the canopy version, as the far squares' woods
        import geodesy
        from test_genground_far import _Im
        photo = _Im().render(geodesy.EnuFrame(40.877, -77.566), -64.5, -64.5, 128.0, 128)[0].astype(float)
        east = (slice(None), slice(70, None))
        self.assertTrue(d[east].mean() > 0.9, "the gaps differ too")
        near_c = np.abs(c[east].astype(float).mean((0, 1)) - photo[east].mean((0, 1))).max()
        near_g = np.abs(rgb[east].astype(float).mean((0, 1)) - photo[east].mean((0, 1))).max()
        self.assertLess(near_c, near_g, "the canopy version's gaps nearer the photo's colour than the gap colour")

    def test_trees_not_built(self):
        rgb, cout = self.run_mode(1)
        self.assertEqual(len(cout), 1)
        self.assertGreater(float((cout[0] != rgb).any(axis=2).mean()), 0.9, "all of FLOOR_MIX: faded too")

    def test_no_trees(self):
        import geodesy
        from test_genground_far import _Im
        cout = []
        rgb, _ = gg.render(_Im(), geodesy.EnuFrame(40.877, -77.566), -64.5, -64.5, 128.0, 128, None, None, None,
                           set(), None, relight=False, canopy_out=cout)
        self.assertIsNone(gg.canopy_if_differs(rgb, cout), "no trees drawn: no canopy version, the plain encoding")


class PairTest(unittest.TestCase):
    """earthfast.gen_ground_pair: the ground and its canopy version in one pass, byte for byte the two gen_ground calls
    (genground.generate's fallback without it)."""

    def inputs(self, mode, px=192):
        import geodesy
        from test_genground_far import _Im
        frame = geodesy.EnuFrame(40.877, -77.566)
        real = gg.canopy_grid

        def tall(mx, my, rasters):
            out = real(mx, my, rasters)
            out[:] = 20
            out[:, px * 2 // 3:] = 0                        # (open ground east: region borders by the woods)
            return out

        def floor(frame, x0, y0, size, px, veg, missing):
            if mode != 2:
                return None, mode
            f = np.zeros((px, px), np.uint8)
            f[:, : px // 2] = np.linspace(0, 255, px // 2).astype(np.uint8)    # (every floor weight)
            return f, 2
        x0 = y0 = -64.5
        size = 128.0 * px / 128
        rgb = _Im().render(frame, x0, y0, size, px)[0]
        with mock.patch.object(gg, "canopy_grid", tall), mock.patch.object(gg, "tree_floor", floor), \
                mock.patch.object(gg, "texel_fields", _grass(gg.texel_fields)):
            kind, region, canopy, _c, poly, dirs, phases, fl, md = gg.inputs(frame, x0, y0, size, px, None, None,
                                                                              None, True if mode else None)
        t = size / px
        gc, gr = int(np.floor(x0 / t + 0.5)), int(np.floor(-(y0 + size) / t + 0.5))
        return (rgb, kind, region, canopy, poly, dirs, phases, float(x0 + t / 2), float(y0 + size - t / 2), float(t),
                gr, gc), fl, md

    def test_pair_is_the_two_calls(self):
        import fast
        if getattr(fast._ef, "VERSION", 0) < gg.PAIR_NEED:
            self.skipTest("earthfast without gen_ground_pair")
        for mode in (2, 1, 0):
            args, fl, md = self.inputs(mode)
            self.assertEqual(md, mode)
            out, can = fast._ef.gen_ground_pair(*args, fl, md, False)
            np.testing.assert_array_equal(out, fast._ef.gen_ground(*args, fl, md), f"mode {mode}: the ground")
            if mode == 2:
                want = fast._ef.gen_ground(*args, np.zeros_like(fl), 2)
            else:
                want = fast._ef.gen_ground(*args, None, 0)
            np.testing.assert_array_equal(can, want, f"mode {mode}: the canopy version")
            if mode:
                self.assertTrue((can != out).any(), f"mode {mode}: the woods differ")
            else:
                np.testing.assert_array_equal(can, out, "no trees drawn: the ground itself")
            # gaps (the block's): the ground the same; the canopy version differs at the unplaced gaps alone (and the
            # region borders' 3 x 3 antialias next to them)
            out2, can2 = fast._ef.gen_ground_pair(*args, fl, md, True)
            np.testing.assert_array_equal(out2, out, f"mode {mode}: the ground with gaps")
            kind, region = args[1], args[2]
            gap = (((region & 0x40000000) != 0) & ((region & gg.LOST) == 0)
                   & ~np.isin(kind, [gg.TREE, gg.WATER, gg.PAVED, gg.ROOF, gg.NONE]))
            d = (can2 != can).any(axis=2)
            if mode != 2:
                self.assertFalse(d.any(), f"mode {mode}: no placed trees, no gaps")
                continue
            self.assertTrue(gap.any() and d.any())
            grown = gap.copy()
            grown[1:] |= gap[:-1]
            grown[:-1] |= gap[1:]
            g2 = grown.copy()
            g2[:, 1:] |= grown[:, :-1]
            g2[:, :-1] |= grown[:, 1:]
            self.assertFalse((d & ~g2).any(), "only the gaps (grown by the antialias's texel)")

    def test_generate_without_the_pair(self):
        """A module without gen_ground_pair: the same ground, no canopy version (the plain encoding)."""
        import fast
        import geodesy
        from test_genground_far import _Im
        if getattr(fast._ef, "VERSION", 0) < gg.PAIR_NEED:
            self.skipTest("earthfast without gen_ground_pair")
        frame = geodesy.EnuFrame(40.877, -77.566)
        rgb = _Im().render(frame, -64.5, -64.5, 128.0, 128)[0]
        real = gg.canopy_grid

        def tall(mx, my, rasters):
            out = real(mx, my, rasters)
            out[:] = 20
            return out

        def floor(frame, x0, y0, size, px, veg, missing):
            f = np.zeros((px, px), np.uint8)
            f[:, : px // 2] = 255
            return f, 2
        got = []
        with mock.patch.object(gg, "canopy_grid", tall), mock.patch.object(gg, "tree_floor", floor):
            for need in (gg.PAIR_NEED, 10 ** 9):
                cout = []
                with mock.patch.object(gg, "PAIR_NEED", need):
                    out, _ = gg.generate(rgb, frame, -64.5, -64.5, 128.0, 128, None, None, veg=True, relight=False,
                                         canopy_out=cout)
                got.append((out, cout))
        np.testing.assert_array_equal(got[0][0], got[1][0])
        self.assertEqual(len(got[0][1]), 1)
        self.assertEqual(got[1][1], [], "no canopy version")


class WriteBaseFadeTest(unittest.TestCase):
    """earthd.Server.write_base: the block's packed copy faded (texel 1 km / its photo size), the photo file the floor
    version as before; the stand-ins pasted from it faded too; switch off: byte for byte as before."""

    def harness(self):
        import earthd
        from test_server import LAT, LON, ServerHarness

        class Harness(ServerHarness):
            near_visual = "detail"
            with_imagery = True
            cells = 64
            block = 1024

            def runTest(self):
                pass
        h = Harness()
        h.setUp()
        self.addCleanup(h.tearDown)
        h.hello()
        aid = h.prepare_anchor(1, LAT, LON)["anchorId"]
        s = h.server
        s.base_px = 256                     # (a small photo: quick)
        return earthd, s, s.anchors[aid]

    def fake_render(self, im, frame, x0, y0, size, px, *a, canopy_out=None, **k):
        rgb = np.full((px, px, 3), FLOOR, np.uint8)
        if canopy_out is not None:
            c = rgb.copy()
            c[:, : px // 2] = CANOPY             # (woods on the west half)
            canopy_out.append(c)
        return rgb, {"s2": px * px}

    def test_write_base_and_stand_in(self):
        earthd, s, anchor = self.harness()
        bc7._GPU_FAILED, gpu = True, bc7._GPU_FAILED
        self.addCleanup(setattr, bc7, "_GPU_FAILED", gpu)
        seen = []
        real = terraincache.base_entry

        def spy(*a):
            seen.append(a)
            return real(*a)
        with mock.patch.object(earthd.genground, "render", self.fake_render), \
                mock.patch.object(earthd.terraincache, "base_entry", spy), \
                mock.patch.object(earthd.genground, "ON", True), \
                mock.patch.object(earthd.rockfacemod, "rock", lambda rgb, *a, **k: rgb):
            out = []
            name = s.write_base(anchor, 0.0, 0.0, rev=1, canopy_out=out)
            self.assertIsNotNone(name)
            args = seen[-1]
            self.assertEqual(len(args), 6, "with the fade")
            canopy, t = args[5]
            self.assertEqual(t, 1024.0 / s.base_px)
            rgb = args[3]
            self.assertIs(out[0], canopy)
            self.assertEqual(s.recent_full[-1][4] is canopy, True)
            d = (canopy != rgb).any(axis=2)
            self.assertTrue(d[:, : s.base_px // 2 - 2].mean() > 0.5, "the woods: the canopy version differs")
            self.assertFalse(d[:, s.base_px // 2 + 2:].any(), "elsewhere alike")
            pack = s._base_pack_path(name)
            self.assertEqual(open(pack, "rb").read(), bc7.dds(rgba(rgb), (canopy, t)))
            lv = bc7_levels(open(pack, "rb").read())
            w4 = 1024.0 / s.base_px * 4             # (mip 2: 16 m at a 256 px photo; the canopy's colour)
            self.assertEqual(gg.fade_weights(w4 / 4, 3)[2], 0.0)
            m2 = bc7.decode(lv[2][2], lv[2][0], lv[2][1])[:, :, :3].astype(int)
            self.assertLess(np.abs(m2[:, :20] - np.array(CANOPY)).max(), 6, "coarse mips: the canopy's colour")
            # the next block's stand-in pasted from it: faded too
            seen.clear()
            with mock.patch.object(s.imagery, "render", lambda *a, **k: (np.full((64, 64, 3), FLOOR, np.uint8),
                                                                           {"s2": 1 << 20})):
                s.write_quick_base(anchor, 128.0, 0.0)
            qa = seen[-1]
            self.assertEqual(len(qa), 6, "the stand-in with the fade")
            qc = qa[5][0]
            self.assertTrue((qc != qa[3]).any(axis=2).any(), "its overlap's woods in the canopy version")
        # switch off: no fade anywhere, the packed copy byte for byte the plain one
        seen.clear()
        with mock.patch.object(earthd.terraincache, "base_entry", spy), \
                mock.patch.object(earthd.genground, "ON", False):
            name = s.write_base(anchor, 0.0, 0.0, rev=2)
            self.assertEqual(len(seen[-1]), 5)
            self.assertEqual(open(s._base_pack_path(name), "rb").read(), bc7.dds(rgba(seen[-1][3])))
            self.assertIsNone(s.recent_full[-1][4])
            seen.clear()
            s.write_quick_base(anchor, 256.0, 0.0)
            self.assertTrue(all(len(a) == 5 for a in seen), "a stand-in off: plain")

    def test_key(self):
        earthd, s, anchor = self.harness()
        import revisit
        with mock.patch.object(revisit, "ON", True):
            with mock.patch.object(earthd.genground, "ON", False):
                off = s.base_revisit_key(anchor, 0.0, 0.0)
                with mock.patch.object(earthd.genground, "FADE_FAR_M", 4.0):
                    self.assertEqual(s.base_revisit_key(anchor, 0.0, 0.0), off, "off: the fade not in it")
            with mock.patch.object(earthd.genground, "ON", True):
                on = s.base_revisit_key(anchor, 0.0, 0.0)
                with mock.patch.object(earthd.genground, "FADE_FAR_M", 4.0):
                    self.assertNotEqual(s.base_revisit_key(anchor, 0.0, 0.0), on, "the fade's parameters")
                with mock.patch.object(earthd.genground, "BLOCK_FADE_VERSION", 2):
                    self.assertNotEqual(s.base_revisit_key(anchor, 0.0, 0.0), on, "its version")


class RestoredBlockTest(unittest.TestCase):
    """A block's photo kept in the revisit cache comes back faded: its packed copy is the faded one, and its canopy
    version (kept beside it) feeds the stand-ins pasted from it."""

    def setUp(self):
        import revisit
        import revisitpack
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(revisitpack.reset)
        self.on = revisit.ON
        revisit.ON = True
        self.addCleanup(setattr, revisit, "ON", self.on)
        p = mock.patch.object(gg, "ON", True)
        p.start()
        self.addCleanup(p.stop)
        bc7._GPU_FAILED, self._gpu = True, bc7._GPU_FAILED
        self.addCleanup(setattr, bc7, "_GPU_FAILED", self._gpu)

    def test_restored_faded(self):
        import revisit
        from test_base_photo_revisit import fake_server
        srv = fake_server(self.tmp.name)
        a1 = SimpleNamespace(id=1, lat=40.877, lon=-77.566, zooms=[17])
        rgb = np.full((64, 64, 3), FLOOR, np.uint8)
        rgb[:, 40:] = (90, 90, 90)
        canopy = rgb.copy()
        canopy[:, :32] = CANOPY
        name = srv.base_name((1, 0.0, 0.0))
        with open(os.path.join(srv.out_dir, name), "wb") as f:
            f.write(terraincache.stored_photo(rgb))
        terraincache.base_entry(srv.user_dir, f"{srv.vfs_dir}/{name}", "/ao.png", rgb, 251, (canopy, 8.0))
        os.makedirs(terraincache.cache_dir(srv.user_dir), exist_ok=True)
        os.replace(os.path.join(terraincache.cache_dir(srv.user_dir),
                                terraincache.base_key(f"{srv.vfs_dir}/{name}", "/ao.png") + ".dds"),
                   srv._base_pack_path(name))
        faded = open(srv._base_pack_path(name), "rb").read()
        bk = srv.base_revisit_key(a1, 0.0, 0.0)
        srv._base_kept(a1, 0.0, 0.0, ())
        srv._base_keep(a1, 0.0, 0.0, bk, name, (revisit.Build(),), (rgb, canopy))
        srv2 = fake_server(os.path.join(self.tmp.name, "2"))
        srv2.cache_dir = srv.cache_dir
        photo = []
        got = srv2._base_revisited(SimpleNamespace(id=3, lat=40.877, lon=-77.566, zooms=[17]), 0.0, 0.0, bk, photo)
        self.assertIsNotNone(got)
        self.assertEqual(open(srv2._base_pack_path(got), "rb").read(), faded, "the faded packed copy")
        c2 = srv2.recent_full[-1][4]
        self.assertIsNotNone(c2, "its canopy version for the stand-ins")
        self.assertLess(np.abs(c2[:, :30].astype(int) - np.array(CANOPY)).max(), 8)
        np.testing.assert_array_equal(c2[:, 34:], photo[0][:, 34:], "elsewhere the photo's own decoded texels")
        # kept without one (no woods): restored without one
        srv3 = fake_server(os.path.join(self.tmp.name, "3"))
        a4 = SimpleNamespace(id=4, lat=40.9, lon=-77.566, zooms=[17])
        name3 = srv3.base_name((4, 0.0, 0.0))
        with open(os.path.join(srv3.out_dir, name3), "wb") as f:
            f.write(terraincache.stored_photo(rgb))
        os.makedirs(terraincache.cache_dir(srv3.user_dir), exist_ok=True)
        with open(srv3._base_pack_path(name3), "wb") as f:
            f.write(b"DDS packed")
        bk3 = srv3.base_revisit_key(a4, 0.0, 0.0)
        srv3._base_kept(a4, 0.0, 0.0, ())
        srv3._base_keep(a4, 0.0, 0.0, bk3, name3, (revisit.Build(),))
        srv3.recent_full.clear()
        self.assertIsNotNone(srv3._base_revisited(a4, 0.0, 0.0, bk3, []))
        self.assertIsNone(srv3.recent_full[-1][4])


class BlockworkerTest(unittest.TestCase):
    def test_base_entry_passes_the_fade(self):
        import blockworker
        with mock.patch.object(blockworker.terraincache, "base_entry", lambda *a: a):
            self.assertEqual(len(blockworker.base_entry("u", "b", "a", 1, 2)), 5)
            self.assertEqual(len(blockworker.base_entry("u", "b", "a", 1, 2, ("c", 0.5))), 6)


if __name__ == "__main__":
    unittest.main()
