"""season: leaf-off imagery to the summer reference's colour, and the fall grade by date and latitude."""
import datetime
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import season  # noqa: E402


class AmountsTest(unittest.TestCase):
    def test_fall_by_date_and_latitude(self):
        md = season.season_amounts(datetime.date(2026, 10, 14), 39.2)["fall"]
        self.assertGreater(md, 0.3)
        self.assertLess(md, 0.7, "mid October in Maryland: turning, not peak")
        self.assertEqual(season.season_amounts(datetime.date(2026, 7, 1), 39.2)["fall"], 0.0)
        self.assertGreater(season.season_amounts(datetime.date(2026, 10, 14), 46.0)["fall"], md, "further north: further")
        self.assertEqual(season.season_amounts(datetime.date(2026, 10, 14), 10.0)["fall"], 0.0, "tropics")
        self.assertEqual(season.season_amounts(datetime.date(2026, 10, 14), -39.2)["fall"], 0.0, "southern spring")
        self.assertEqual(season.amounts_for("off", 39.2)["fall"], 0.0)
        self.assertEqual(season.amounts_for("fall", 39.2)["fall"], 1.0)
        jan = season.season_amounts(datetime.date(2026, 1, 15), 39.2)
        self.assertEqual((jan["winter"], jan["fall"], jan["spring"]), (1.0, 0.0, 0.0))
        self.assertGreater(season.season_amounts(datetime.date(2026, 4, 22), 39.2)["spring"], 0.8)
        self.assertEqual(season.season_amounts(datetime.date(2026, 7, 1), 39.2)["winter"], 0.0)


def _hsv(rgb):
    """(hue deg, saturation, value) of uint8 rgb (..., 3): the tests' measure of the grade."""
    a = rgb.astype(np.float32) / 255.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx, mn = a.max(-1), a.min(-1)
    d = mx - mn
    h = np.zeros_like(mx)
    m = d > 1e-6
    rr, gg, bb = (mx == r) & m, (mx == g) & m & (mx != r), (mx == b) & m & (mx != r) & (mx != g)
    h[rr] = (60.0 * ((g - b)[rr] / d[rr])) % 360.0
    h[gg] = 60.0 * ((b - r)[gg] / d[gg]) + 120.0
    h[bb] = 60.0 * ((r - g)[bb] / d[bb]) + 240.0
    s = np.where(mx > 0, d / np.maximum(mx, 1e-6), 0.0)
    return h, s, mx


class ColourTest(unittest.TestCase):
    def test_auto_is_graded_and_keyed_by_its_week(self):
        # (far tiles and block photos were keyed by "auto" alone: October's fall colours served in late November)
        d = datetime.date(2026, 11, 26)          # a Thursday
        start = season.slot_date(d)
        self.assertEqual(start, datetime.date(2026, 11, 23), "the Monday the week's slot starts on")
        self.assertEqual(season.slot_date(start + datetime.timedelta(days=6)), start)
        self.assertEqual(season.amounts_for("auto", 45.0, d), season.season_amounts(start, 45.0),
                         "graded with the slot's date: the key says it all")
        k = season.season_key("auto", 45.0, d)
        self.assertEqual(k, ("auto", "2026-11-23"))
        self.assertEqual(season.season_key("auto", 45.0, start + datetime.timedelta(days=6)), k, "same week, same key")
        self.assertNotEqual(season.season_key("auto", 45.0, d + datetime.timedelta(days=7)), k, "next week: another")
        self.assertEqual(season.season_key("auto", 0.0, d), ("auto", "none"), "the tropics: no season, one key")
        self.assertEqual(season.season_key("off", 45.0, d), "off")
        self.assertEqual(season.season_key("fall", 45.0, d), "fall")

    def test_leaf_on_greens_vegetation_only(self):
        rgb = np.zeros((64, 64, 3), np.uint8)
        rgb[:, :32] = (120, 105, 80)            # dormant grass (left), a grey roof (right)
        rgb[:, 32:] = (128, 128, 128)
        ref = np.zeros((8, 8, 3), np.uint8)
        ref[:, :4] = (70, 110, 60)              # summer: green left, the same roof right
        ref[:, 4:] = (128, 128, 128)
        out = season.leaf_on(rgb, ref).astype(int)
        self.assertGreater(out[32, 4, 1] - out[32, 4, 0], 10, "the grass turned green")
        self.assertLess(np.abs(out[32, 60] - 128).max(), 3, "the roof kept its colour")

    def test_fall_turns_dark_canopy_not_lawn(self):
        rng = np.random.default_rng(2)
        canopy = np.clip(np.array([50, 85, 45]) + rng.normal(0, 18, (64, 64, 1)), 0, 255)
        lawn = np.tile(np.array([110, 150, 90], float), (64, 64, 1))
        rgb = np.concatenate([canopy, lawn], 1).astype(np.uint8)
        ys, xs = np.mgrid[0:64, 0:128].astype(np.float32) * 1.0
        out = season.grade(rgb, xs, -ys, {"fall": 1.0})
        h0, _, _ = _hsv(rgb)
        h1, _, _ = _hsv(out)
        turned = (h0 - h1)[:, :64]
        self.assertGreater(float(np.mean(turned > 20)), 0.2, "a good share of the canopy turned yellow / red")
        self.assertLess(float(np.max((h0 - h1)[:, 70:])), 25, "the lawn only yellowed a little")
        self.assertTrue(np.array_equal(season.grade(rgb, xs, -ys, {"fall": 0.0}), rgb), "summer: unchanged")
        win = season.grade(rgb, xs, -ys, season.amounts_for("winter", 39.2))
        _, s0, _ = _hsv(rgb)
        _, s1, _ = _hsv(win)
        self.assertLess(float(s1[:, :64].mean()), 0.7 * float(s0[:, :64].mean()), "winter: the canopy greys")
        # world-anchored: the same place graded alone gives the same texels
        part = season.grade(rgb[:, :64], xs[:, :64], -ys[:, :64], {"fall": 1.0})
        self.assertLess(np.abs(part[:, 10:54].astype(int) - out[:, 10:54].astype(int)).max(), 2)




class FarTexKeyTest(unittest.TestCase):
    def test_fartex_key_holds_the_week(self):
        """Server.tex_key ("fartex", the main process's far textures and the bulk restore's): the season's week
        (season_key, not "auto" alone: October's grade was served in late November; found by the winter W2 agent)."""
        import datetime
        from types import SimpleNamespace
        import earthd
        import season
        im = SimpleNamespace(chain=[SimpleNamespace(name="eox2016")], leaf_on=True, season_mode="auto")
        srv = SimpleNamespace(imagery=im, tone="auto", tex_modes=(), road_masks=False, mapdata=None, road_widths=None,
                              surface=None, layout=SimpleNamespace(cells=64), road_mask_px=lambda lvl: 0)
        anchor = SimpleNamespace(lat=40.877, lon=-77.566, zooms={k: 16 - k for k in range(9)})
        tile = SimpleNamespace(key=(5, 0, 0), level=5, size=8192.0, bbox=(0.0, 0.0, 8192.0, 8192.0))
        real = season.utc_today
        try:
            season.utc_today = lambda: datetime.date(2026, 10, 20)
            october = earthd.Server.tex_key(srv, anchor, tile, 256)
            season.utc_today = lambda: datetime.date(2026, 11, 24)
            november = earthd.Server.tex_key(srv, anchor, tile, 256)
            self.assertNotEqual(october, november)
            self.assertEqual(earthd.Server.tex_key(srv, anchor, tile, 256), november)
        finally:
            season.utc_today = real

if __name__ == "__main__":
    unittest.main()
