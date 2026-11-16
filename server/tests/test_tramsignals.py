"""Tram signals (tramsignals.py and the kernel's tram::signals): a signal before the stop line where a tram track crosses a
road at a signalled junction, its yaw, its junction's cycle, the tile's own rows only (plan 046 stage H)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
import roads  # noqa: E402
import tramsignals  # noqa: E402
from test_roads import FRAME, features  # noqa: E402

BOX = roads.tile_bbox(0, 0)
Z = 10.0


def ground(xy):
    return np.full(len(np.atleast_2d(xy)), Z)


def scene(extra_tram=None):
    """A tram along y = 128 and a secondary road across it along x = 150, 200 m of each."""
    lines = [[(-100.0, 128.0), (400.0, 128.0)], [(150.0, 20.0), (150.0, 240.0)]]
    classes = ["tram", "secondary"]
    if extra_tram is not None:
        lines.append([(-100.0, extra_tram), (400.0, extra_tram)])
        classes.append("tram")
    return features(lines, classes, ids=[f"w{i}" for i in range(len(lines))],
                    subtypes=["rail", "road"] + (["rail"] if extra_tram is not None else []))


def rows_of(f, signals, **kw):
    return tramsignals.tile_rows(FRAME, BOX, [f], ground, signals, kw.get("country", "DE"), kw.get("left_hand", False))


class TramSignalRowsTest(unittest.TestCase):
    def test_a_signal_per_direction_before_the_stop_line(self):
        half = roads.road_specs(scene())[1].width / 2
        rows = rows_of(scene(), np.array([[160.0, 140.0]]))
        self.assertEqual(len(rows), 2)
        west = min(rows, key=lambda r: r[0])
        east = max(rows, key=lambda r: r[0])
        # the road's edge is half a width from x = 150; the stop line a crosswalk and the margin before it
        self.assertAlmostEqual(west[0], 150.0 - half - tramsignals_BEFORE(), delta=0.4)
        self.assertAlmostEqual(east[0], 150.0 + half + tramsignals_BEFORE(), delta=0.4)
        # heading east the right is south (smaller y); heading west, north
        self.assertLess(west[1], 128.0 - 1.5)
        self.assertGreater(east[1], 128.0 + 1.5)
        self.assertAlmostEqual(west[3], 270.0, delta=1.0)        # the model's +x is the right of the tram's travel
        self.assertAlmostEqual(east[3], 90.0, delta=1.0)
        self.assertEqual(west[2], Z)
        self.assertEqual(west[4], 1)                              # Germany: the BOStrab head
        self.assertEqual(west[5:], east[5:], "both signals of a junction share its cycle")

    def test_a_signalled_junction_only(self):
        self.assertEqual(rows_of(scene(), None), [])
        self.assertEqual(rows_of(scene(), np.zeros((0, 2))), [])
        self.assertEqual(rows_of(scene(), np.array([[150.0, 128.0 + 60.0]])), [])

    def test_the_style_follows_the_country_and_the_side_the_traffic(self):
        for country, style in (("CZ", 0), ("UA", 0), ("DE", 1), ("FI", 1)):
            rows = rows_of(scene(), np.array([[160.0, 140.0]]), country=country)
            self.assertTrue(rows and all(r[4] == style for r in rows), country)
        left = rows_of(scene(), np.array([[160.0, 140.0]]), left_hand=True)
        west = min(left, key=lambda r: r[0])
        self.assertGreater(west[1], 128.0 + 1.5, "left-hand traffic: the post on the tram's left")

    def test_a_pair_of_tracks_keeps_only_the_outer_signals(self):
        rows = rows_of(scene(extra_tram=131.0), np.array([[160.0, 140.0]]))
        self.assertEqual(len(rows), 2, rows)
        self.assertTrue(any(r[0] < 150.0 and r[1] < 128.0 for r in rows))
        self.assertTrue(any(r[0] > 150.0 and r[1] > 131.0 for r in rows))

    def test_the_cycle_is_one_per_junction_and_does_not_move_with_the_tile(self):
        a = rows_of(scene(), np.array([[160.0, 140.0]]))
        b = rows_of(scene(), np.array([[160.0, 140.0], [200.0, 128.0 + 40.0]]))      # a far node: the junction's key moves
        self.assertEqual(a, rows_of(scene(), np.array([[160.0, 140.0]])))
        cyc = a[0][5:]
        self.assertTrue(60 <= cyc[0] <= 90 and 0 <= cyc[1] < cyc[0] and 12 <= cyc[2] <= 18, cyc)
        self.assertEqual(len(b), 2)

    def test_the_flag_switches_it_off(self):
        old = tramsignals.ENABLED
        tramsignals.ENABLED = False
        try:
            self.assertEqual(rows_of(scene(), np.array([[160.0, 140.0]])), [])
        finally:
            tramsignals.ENABLED = old

    def test_only_the_tiles_own_posts(self):
        # the tile (1, 0) does not own the crossing at x = 150 (its box starts further east)
        box = roads.tile_bbox(5, 0)
        self.assertEqual(tramsignals.tile_rows(FRAME, box, [scene()], ground, np.array([[160.0, 140.0]]), "DE"), [])

    def test_no_tram_no_rows(self):
        f = features([[(150.0, 20.0), (150.0, 240.0)]], ["secondary"], ids=["w0"], subtypes=["road"])
        self.assertEqual(rows_of(f, np.array([[160.0, 140.0]])), [])


def tramsignals_BEFORE():
    return 3.5


class BranchAndRailSignalTest(unittest.TestCase):
    def test_a_switch_ahead_gives_the_signal_its_diagonal_bar(self):
        # a second way starts on the tram's line 25 m east of the road and leaves it to the left (north)
        f = features([[(-100.0, 128.0), (400.0, 128.0)], [(150.0, 20.0), (150.0, 240.0)], [(175.0, 128.0), (230.0, 160.0)]],
                     ["tram", "secondary", "tram"], ids=["w0", "w1", "w2"], subtypes=["rail", "road", "rail"])
        rows = rows_of(f, np.array([[160.0, 140.0]]))
        east = [r for r in rows if r[0] < 150.0 and r[1] < 128.0]
        west = [r for r in rows if r[0] > 150.0]
        self.assertEqual(len(east), 1, rows)
        self.assertEqual(east[0][8], 1, "a track leaves to the left of the tram heading east")
        self.assertTrue(all(r[8] == 0 for r in west), "the branch is behind the tram heading west")
        self.assertEqual(len(rows[0]), 9)

    def test_no_switch_no_branch(self):
        rows = rows_of(scene(), np.array([[160.0, 140.0]]))
        self.assertTrue(rows and all(r[8] == 0 for r in rows))

    def test_railway_signal_nodes_are_read_from_the_rail_tag_shards_when_they_have_them(self):
        f = scene()
        lat, lon, _ = FRAME.enu_to_geodetic(np.array([140.0, 90.0]), np.array([128.0, 30.0]), 0.0)
        f.__dict__["_rail_nodes"] = [(float(lon[0]), float(lat[0]), {"railway": "signal"}),
                                     (float(lon[1]), float(lat[1]), {"railway": "level_crossing"})]
        pts = tramsignals.rail_signal_points(FRAME, [f])
        self.assertEqual(pts.shape, (1, 2))
        np.testing.assert_allclose(pts[0], [140.0, 128.0], atol=0.2)
        self.assertEqual(tramsignals.rail_signal_points(FRAME, [scene()]).shape, (0, 2))
        # a tram line with such a node and no mapped traffic signals gets signals by rule B
        got = tramsignals.tile_rows(FRAME, BOX, [f], ground, None, "DE")
        self.assertEqual(len(got), 2, got)


class SignalTexturesTest(unittest.TestCase):
    """The lamp sheet and its palette (bne_ts_lamps.color.dds, bne_ts_palette.color.dds): the palette's pure red, green and blue
    bands select instanceColor / 1 / 2 and must survive the block compression; the lamp sheet's three bands are lit."""

    DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "mod", "beamng_earth", "levels", "beamng_earth",
                       "art", "shapes", "beamngEarth")

    def _open(self, name):
        from PIL import Image
        try:
            return np.array(Image.open(os.path.join(self.DIR, name)).convert("RGB"))
        except Exception as e:      # (a Pillow without DDS support)
            self.skipTest("cannot read %s: %s" % (name, e))

    def test_both_are_block_compressed_dds_of_the_size_the_models_assume(self):
        from test_mod_textures import dds_levels
        for name in ("bne_ts_palette.color.dds", "bne_ts_lamps.color.dds"):
            with open(os.path.join(self.DIR, name), "rb") as fh:
                levels = dds_levels(fh.read())
            self.assertEqual(levels[0][:2], (512, 512), name)

    def test_the_palette_keeps_its_pure_bands_through_the_compression(self):
        a = self._open("bne_ts_palette.color.dds").astype(int)
        for k, (lo, hi) in enumerate(((0, 171), (171, 341), (341, 512))):
            band = a[lo + 3:hi - 3]
            self.assertTrue((band[:, :, k] >= 245).all(), "band %d: its own channel is full" % k)
            self.assertTrue((np.delete(band, k, axis=2) <= 10).all(), "band %d: the other two are empty" % k)

    def test_the_palette_bands_stay_pure_in_the_small_mips_where_the_models_lamps_are_seen(self):
        a = self._open("bne_ts_palette.color.dds").astype(float)
        for level in (1, 2, 3, 4, 5):       # (a lamp 0.27 m wide is 20-100 pixels away at 30 m: UV span 1/4 band: mip 3-5)
            n = 512 >> level
            small = a.reshape(n, 512 // n, n, 512 // n, 3).mean(axis=(1, 3))
            for k, tc in enumerate(self.bands()):
                row = int((1.0 - tc) * n)
                self.assertGreaterEqual(small[row, n // 2, k], 235, (level, k))
                self.assertLessEqual(np.delete(small[row, n // 2], k).max(), 12, (level, k))

    def bands(self):
        import build_tram_signals as b
        return b.BAND_T

    def test_the_models_lamps_look_up_the_bands_they_light(self):
        import build_tram_signals as b
        a = self._open("bne_ts_palette.color.dds").astype(int)
        for k, tc in enumerate(b.BAND_T):
            for v in (tc - b.BAND_H / 2, tc, tc + b.BAND_H / 2):         # the lamp's picture's V range
                row = min(511, int((1.0 - v) * 512))
                self.assertGreaterEqual(a[row, 256, k], 245, (k, v))
                self.assertLessEqual(np.delete(a[row, 256], k).max(), 10, (k, v))

    def test_the_lamp_sheet_is_lit_in_all_three_bands(self):
        a = self._open("bne_ts_lamps.color.dds")
        for lo, hi in ((0, 171), (171, 341), (341, 512)):
            band = a[lo + 20:hi - 20].reshape(-1, 3)
            self.assertGreater(band.mean(), 215)
            self.assertGreater(band.min(), 120, "no black gaps: an unlit lamp is the instance colour's black")


if __name__ == "__main__":
    unittest.main()
