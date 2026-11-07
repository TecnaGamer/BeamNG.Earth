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

    # fork: evergreen regions (season.evergreen): the tropics, Koppen A, Australia
    def test_evergreen_regions_have_no_season(self):
        none = {"fall": 0.0, "winter": 0.0, "spring": 0.0}
        brisbane, sydney, hobart, perth = (-27.47, 153.03), (-33.87, 151.2), (-42.88, 147.33), (-31.95, 115.86)
        miami = (25.77, -80.19)                 # Koppen Am, north of 23
        for name, (lat, lon) in (("Brisbane", brisbane), ("Sydney", sydney), ("Hobart", hobart), ("Perth", perth),
                                 ("Miami", miami), ("equator", (0.0, 30.0))):
            self.assertTrue(season.evergreen(lat, lon), name)
            for month in range(1, 13):
                self.assertEqual(season.season_amounts(datetime.date(2026, month, 15), lat, lon), none, (name, month))
            self.assertEqual(season.amounts_for("auto", lat, datetime.date(2026, 5, 10), lon), none, name)
        # without a longitude only the latitude rule applies (callers that know none)
        self.assertGreater(season.season_amounts(datetime.date(2026, 5, 10), -27.47)["fall"], 0.0)
        self.assertTrue(season.evergreen(10.0))
        # an explicit --season fall is still full anywhere
        self.assertEqual(season.amounts_for("fall", -27.47, lon=153.03)["fall"], 1.0)

    def test_deciduous_regions_keep_their_seasons(self):
        may = datetime.date(2026, 5, 10)
        self.assertFalse(season.evergreen(39.2, -76.8))          # Maryland
        self.assertGreater(season.season_amounts(datetime.date(2026, 10, 14), 39.2, -76.8)["fall"], 0.3)
        self.assertGreater(season.season_amounts(datetime.date(2026, 10, 14), 51.5, -0.1)["fall"], 0.3, "London")
        # southern hemisphere: autumn is March - May (Santiago, Auckland, Durban peak in May), October is spring
        for lat, lon in ((-33.45, -70.67), (-36.85, 174.76), (-29.85, 31.0)):
            self.assertFalse(season.evergreen(lat, lon))
            self.assertGreater(season.season_amounts(may, lat, lon)["fall"], 0.5, (lat, lon))
            self.assertEqual(season.season_amounts(datetime.date(2026, 10, 14), lat, lon)["fall"], 0.0, (lat, lon))
            self.assertEqual(season.season_amounts(datetime.date(2026, 11, 14), lat, lon)["fall"], 0.0, (lat, lon))
        self.assertGreater(season.season_amounts(datetime.date(2026, 7, 15), -33.45, -70.67)["winter"], 0.9)


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


class GrassMonthsTest(unittest.TestCase):
    """fork: the grass is brown most of the year and green in its palette's wet months."""

    def test_tropics_wet_summer_south_wet_winter(self):
        north = season.grass_dry_months(-23.31, 150.60)           # Mt Archer, Rockhampton: woodland, December to March
        self.assertEqual(len(north), 12)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in north))
        self.assertEqual(north[0], season.GRASS_DRY_SHARES[1], "January: green")
        self.assertEqual(north[6], season.GRASS_DRY_SHARES[0], "July: brown")
        self.assertTrue(north[3] > north[0] and north[10] > north[0], "April and November: changing")
        self.assertGreater(sum(north) / 12, 0.5, "dry most of the year")
        south = season.grass_dry_months(-37.85, 145.35)           # the Dandenongs: green June to October
        self.assertEqual(south[7], season.GRASS_DRY_SHARES[1], "August: green")
        self.assertEqual(south[0], season.GRASS_DRY_SHARES[0], "January: brown")

    def test_no_palette_no_months(self):
        for lat, lon in ((35.68, 139.69), (-23.38, 150.51), (51.5, -0.12)):      # Tokyo, Rockhampton's cleared city, London
            self.assertIsNone(season.grass_dry_months(lat, lon))


class ColourTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
