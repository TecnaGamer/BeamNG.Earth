"""The far field's potentially visible set (farpvs.py, earthfast farpvs.rs; the retro study's item C): far tiles hidden
behind terrain from everywhere the player can be soon get a small texture (Server.tile_tex_px), and get their full one
back (stale, far_retex) as soon as a set no longer hides them (Connection.pvs_reveal)."""
import asyncio
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import earthd  # noqa: E402
import farfield as ff  # noqa: E402
import farpvs as pv  # noqa: E402
import geodesy  # noqa: E402

LAT, LON = 39.64, -106.37
SPECS = ((12000.0, 100.0, 5.0), (None, 400.0, 20.0))     # (small rings: the tests' synthetic ground)


class RidgeDem:
    """Sea-level ground with a 1500 m ridge running north-south 6-7 km east of the frame's origin, and a 600 m plateau
    from 2 km south of the origin on (its north edge: a step)."""

    def __init__(self, frame, plateau=False):
        self.frame, self.plateau = frame, plateau

    def sample(self, lat, lon, zoom, progress=None):
        e, n, _ = self.frame.geodetic_to_enu(np.asarray(lat, np.float64), np.asarray(lon, np.float64), 0.0)
        z = np.where((e > 6000.0) & (e < 7000.0), 1500.0, 0.0)
        if self.plateau:
            z = np.maximum(z, np.where(n < -2000.0, 600.0, 0.0))
        return z


def make_set(dem, half=1500.0, eye=30.0, px=0.0, py=0.0, specs=SPECS, radius=40000.0):
    frame = dem.frame
    rings = pv.Rings(frame, dem, radius, specs=specs)
    field = pv.Field(1, frame, rings)
    old = pv.EYE_CLASSES
    pv.EYE_CLASSES = (eye,)
    try:
        g = rings.ground_hi(px, py)
        return pv.Set(field, 0.0, 0.0, half, 0, px, py, g), field
    finally:
        pv.EYE_CLASSES = old


class PvsTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(LAT, LON)

    def test_ridge_hides_the_far_side(self):
        s, field = make_set(RidgeDem(self.frame))
        self.assertGreater(s.viewpoints, 9)
        behind = (16000.0, -2000.0, 20000.0, 2000.0)      # an L4-size tile 9 km past the ridge
        front = (-20000.0, -2000.0, -16000.0, 2000.0)     # the same the other way
        ridge = (6000.0, -2000.0, 10000.0, 2000.0)        # on it
        self.assertTrue(s.hidden("a", behind, 4))
        self.assertFalse(s.hidden("b", front, 4))
        self.assertFalse(s.hidden("c", ridge, 4))
        self.assertFalse(s.hidden("d", behind, 0), "levels under MIN_LEVEL: always full")
        self.assertFalse(s.hidden("e", (1000.0, 0.0, 1256.0, 256.0), 4), "at the region: always full")
        self.assertFalse(s.hidden("f", (30000.0, 0.0, 70000.0, 40000.0), 6), "past the rings: visible")

    def test_high_eye_sees_over_it(self):
        s, _ = make_set(RidgeDem(self.frame), eye=3000.0)
        self.assertFalse(s.hidden("a", (16000.0, -2000.0, 20000.0, 2000.0), 4))

    def test_flat_ground_hides_nothing_near(self):
        class Flat(RidgeDem):
            def sample(self, lat, lon, zoom, progress=None):
                return np.zeros(np.shape(lat))
        s, _ = make_set(Flat(self.frame))
        for x in (-20000.0, 8000.0, 16000.0):
            self.assertFalse(s.hidden(str(x), (x, -2000.0, x + 4000.0, 2000.0), 4))

    def test_valid_at(self):
        s, _ = make_set(RidgeDem(self.frame))
        self.assertTrue(s.valid_at(1, 200.0, -300.0, 35.0))
        self.assertFalse(s.valid_at(2, 200.0, -300.0, 35.0), "another anchor")
        self.assertFalse(s.valid_at(1, 1200.0, 0.0, 35.0), "past the square less its margin")
        self.assertFalse(s.valid_at(1, 0.0, 0.0, 80.0), "the camera above the eye height it was made for")

    def test_unreachable_viewpoints_left_out(self):
        """A plateau 600 m above the player (2 km south) is no viewpoint of a car on the plain: what only the plateau
        sees stays hidden; a player on the plateau has its own."""
        dem = RidgeDem(self.frame, plateau=True)
        s, _ = make_set(dem, half=3000.0)
        pv_grade = pv.GRADE
        try:
            pv.GRADE = 99.0
            s_all, _ = make_set(dem, half=3000.0)
        finally:
            pv.GRADE = pv_grade
        self.assertLess(s.viewpoints, s_all.viewpoints)

    def test_region_follows_speed(self):
        cx, cy, half = pv.region(120.0, -80.0, 0.0, 0.0)
        self.assertEqual((cx, cy, half), (0.0, 0.0, pv.HALF_MIN))
        cx, cy, half = pv.region(0.0, 0.0, 100.0, 0.0)      # 224 mph east
        self.assertGreater(half, pv.HALF_MIN)
        self.assertGreater(cx, 0.0)
        self.assertLessEqual(cx, (half - pv.MARGIN_M) / 2 + pv.VP_STEP / 2)
        self.assertEqual(pv.eye_class_for(2.0), 0)
        self.assertIsNone(pv.eye_class_for(1000.0))

    def test_viewpoints_cached(self):
        dem = RidgeDem(self.frame)
        s, field = make_set(dem)
        n = field.computed
        old = pv.EYE_CLASSES
        pv.EYE_CLASSES = (30.0,)
        try:
            pv.Set(field, 500.0, 0.0, 1500.0, 0, 500.0, 0.0, field.rings.ground_hi(500.0, 0.0))
        finally:
            pv.EYE_CLASSES = old
        self.assertLessEqual(field.computed - n, 7, "one new column of viewpoints, the rest kept")


class SlopeDem(RidgeDem):
    """Sea-level ground, rising eastwards at 20 % from 15 to 20 km east of the origin (facing it), a 1000 m plateau
    past that."""

    def sample(self, lat, lon, zoom, progress=None):
        e, n, _ = self.frame.geodetic_to_enu(np.asarray(lat, np.float64), np.asarray(lon, np.float64), 0.0)
        return np.clip((e - 15000.0) * 0.2, 0.0, 1000.0)


class ScreenSizeTest(unittest.TestCase):
    """Visible far tiles' textures by what the screen resolves of them (farpvs.Set.tex_px, SLOPE_SIZING)."""

    def setUp(self):
        self.frame = geodesy.EnuFrame(LAT, LON)
        self.dem = SlopeDem(self.frame)

    def px(self, s, bbox, level):
        return s.tex_px(f"{bbox}{level}", bbox, level, bbox[2] - bbox[0], 512)

    def test_flat_far_ground_coarser_facing_slope_full(self):
        s, _ = make_set(self.dem, eye=8.0)
        flat = (-20000.0, -2000.0, -16000.0, 2000.0)     # L4, 14.5 km from the square, flat
        slope = (16000.0, -2000.0, 20000.0, 2000.0)      # the same distance, a 20 % slope facing the eye
        self.assertLess(self.px(s, flat, 4), 512)
        self.assertGreaterEqual(self.px(s, flat, 4), pv.SLOPE_PX_MIN)
        self.assertEqual(self.px(s, slope, 4), 512)
        self.assertEqual(self.px(s, (2500.0, 0.0, 3524.0, 1024.0), 2), 512, "near the square: its own")
        self.assertEqual(self.px(s, (-20000.0, -2000.0, -16000.0, 2000.0), 1), 512, "under SLOPE_MIN_LEVEL: its own")

    def test_higher_eye_and_nearer_need_more(self):
        low, _ = make_set(self.dem, eye=8.0)
        high, _ = make_set(self.dem, eye=280.0)
        mid = (-10048.0, 0.0, -8000.0, 2048.0)            # L3, 6.5 km from the square
        self.assertGreater(self.px(high, mid, 3), self.px(low, mid, 3))
        flat = (-20000.0, -2000.0, -16000.0, 2000.0)
        near = (-8000.0, -2000.0, -4000.0, 2000.0)
        self.assertGreaterEqual(self.px(low, near, 4), self.px(low, flat, 4))

    def test_never_coarser_than_the_screen(self):
        """The texel chosen is within the need the module's rule computes (by brute force over the footprint)."""
        s, _ = make_set(self.dem, eye=8.0)
        for bbox in [(-20000.0, -2000.0, -16000.0, 2000.0), (-40000.0, 0.0, -32000.0, 8000.0)]:
            size = bbox[2] - bbox[0]
            px = self.px(s, bbox, 5)
            d = max(bbox[0] - (s.cx + s.half), (s.cx - s.half) - bbox[2], 0.0)
            sin_a = (s.eye_top - 0.0) / d + d / (2 * pv.R_OCC) + pv.SLOPE_FLOOR     # (flat: the floor's slope)
            need = d / (s.ppr * pv.OVERSAMPLE) / min(1.0, pv.ANISO * sin_a)
            self.assertLessEqual(size / px, need + 1e-9)


class _Writer:
    def write(self, data):
        pass

    def close(self):
        pass

    def get_extra_info(self, name):
        return ("127.0.0.1", 0)


class ServerPvsTest(unittest.TestCase):
    """tile_tex_px / tex_short / pvs_reveal with a set in effect."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = earthd.Server(self.tmp.name, os.path.join(self.tmp.name, "cache"), imagery=None, cells=64, block=1024,
                               near_visual="mesh", horizon=False)
        self.addCleanup(self.s.hm_pool.shutdown)
        self.addCleanup(self.s.tile_pool.shutdown)

    def test_hidden_tile_small_then_sharpened(self):
        old = pv.SLOPE_SIZING
        pv.SLOPE_SIZING = False             # (the hidden rule alone: test_screen_sizing has the other)
        self.addCleanup(setattr, pv, "SLOPE_SIZING", old)

        async def go():
            c = earthd.Connection(self.s, None, _Writer())
            a = self.s.new_anchor(LAT, LON)
            self.s.anchors[a.id] = a
            c.active = a.id
            dem = RidgeDem(a.frame)
            s, field = make_set(dem)
            s.aid = field.aid = a.id
            self.s.pvs_on = True
            self.s.pvs_fields[a.id] = field
            behind = ff.Tile(4, 4, -1)        # 16-20 km east (L4: 4096 m)
            front = ff.Tile(4, -5, -1)
            c.desired = {behind.key: behind, front.key: front}
            self.s.connections.add(c)
            c.pvs = s
            full = self.s.tex_size(4)
            self.assertEqual(self.s.tile_tex_px(a, behind), min(full, pv.TEX_PX))
            self.assertEqual(self.s.tile_tex_px(a, front), full)
            # shown small; the set dropped (the player flew up): stale, asked to re-render
            ck = c.ck(behind.key)
            self.s.tiles[ck] = earthd.TileInfo(pos=[0, 0, 0], err=0.0, sink=None, skirt=2.0, grid=None, bytes=1,
                                               seconds=0.0, material="m", texture="t", tex_px=pv.TEX_PX, tile=behind)
            c.shown = {ck: behind.bbox}
            self.assertFalse(c.tex_short(self.s.tiles[ck], behind), "hidden: small is enough")
            self.s.retex_rate = 0.0       # (no pump in the test: the request is what counts)
            c._pvs_drop()
            self.assertIsNone(c.pvs)
            self.assertEqual(self.s.tile_tex_px(a, behind), full)
            self.assertTrue(self.s.tiles[ck].stale)
            self.assertEqual(self.s.pvs_stats["revealed"], 1)
            # a second connection that wants the tile without a set: not hidden for it, full size
            c.pvs = s
            c2 = earthd.Connection(self.s, None, _Writer())
            c2.active, c2.desired = a.id, {behind.key: behind}
            self.s.connections.add(c2)
            self.assertEqual(self.s.tile_tex_px(a, behind), full)
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
