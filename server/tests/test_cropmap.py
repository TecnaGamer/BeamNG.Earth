"""Crop fields: the EU crop map reader (cropmap.py) and the photo painting built on it (surface.photo_crops,
cropfield.rs). Offline: the map is a stub, the photo a synthetic one."""
import math
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cropmap
import fast
import geodesy
import netfetch
import surface


class FakeCog:
    """A 3035 raster of 10 m pixels: the code is 216 (maize) east of x = SPLIT_X, else 211 (wheat)."""
    epsg = 3035
    width = height = 40000
    x0, y0, sx, sy = 4_200_000.0, 3_300_000.0, 10.0, 10.0
    SPLIT_X = float(cropmap.laea(50.0, 12.001)[0])

    def __init__(self):
        self.reads = 0

    def pixel_of(self, x, y):
        return (np.asarray(x) - self.x0) / self.sx, (self.y0 - np.asarray(y)) / self.sy

    def read_window(self, col0, row0, w, h):
        self.reads += 1
        cols = col0 + np.arange(w)
        a = np.where(self.x0 + cols * self.sx >= self.SPLIT_X, 216, 211)
        return np.broadcast_to(a, (h, w)).astype(np.int32)


class FoldTests(unittest.TestCase):
    def test_codes_fold_to_visual_groups(self):
        g = cropmap.fold(np.array([211, 213, 216, 231, 232, 222, 290, 100, 500, 0, -3, 99999]))
        self.assertEqual(list(g), [cropmap.CEREAL, cropmap.CEREAL, cropmap.MAIZE, cropmap.SUNFLOWER, cropmap.RAPE,
                                   cropmap.ROOT, cropmap.BARE, 0, cropmap.GRASSLAND, 0, 0, 0])

    def test_projection_origin_and_scale(self):
        x, y = cropmap.laea(52.0, 10.0)
        self.assertAlmostEqual(float(x), 4321000.0, places=3)
        self.assertAlmostEqual(float(y), 3210000.0, places=3)
        # a degree of latitude is ~111.2 km, so 0.001 deg north is ~111 m
        x2, y2 = cropmap.laea(52.001, 10.0)
        self.assertAlmostEqual(float(y2 - y), 111.2, delta=0.5)
        self.assertAlmostEqual(float(x2 - x), 0.0, delta=0.01)

    def test_coverage_boxes(self):
        self.assertEqual(cropmap.covers(np.array([50.28]), np.array([15.95])), ["eu27"])
        self.assertEqual(cropmap.covers(np.array([49.0]), np.array([28.7])), ["ua", "eu27"])
        self.assertEqual(cropmap.covers(np.array([40.0]), np.array([-100.0])), [])


class SampleTests(unittest.TestCase):
    def reader(self):
        cm = cropmap.CropMap("unused", None)
        cog = FakeCog()
        cm._cog = lambda name, kind: cog
        return cm, cog

    def test_sample_reads_the_window_once_and_folds(self):
        cm, cog = self.reader()
        la = np.full((3, 3), 50.0)
        lo = np.linspace(11.999, 12.003, 3)[None, :].repeat(3, 0)
        a = cm.sample(la, lo)
        b = cm.sample(la, lo)
        self.assertEqual(a.shape, (3, 3))
        self.assertEqual(sorted(set(a.ravel().tolist())), [cropmap.CEREAL, cropmap.MAIZE])     # the split is read
        self.assertEqual(cog.reads, len(cm._mem))       # a cached window is not read again
        self.assertTrue((a == b).all())

    def test_grass_is_hidden_from_the_crop_groups_and_asked_for_apart(self):
        # 500 (grass) folds to GRASSLAND, which the Rust field filter does not know (it counts ten groups): sample() masks it
        cm, cog = self.reader()
        cog.read_window = lambda c0, r0, w, h: np.full((h, w), 500, np.int32)
        la, lo = np.full((2, 2), 50.0), np.full((2, 2), 12.0)
        self.assertFalse(cm.sample(la, lo).any())
        self.assertTrue(cm.sample(la, lo, grass=True).all())

    def test_outside_every_file_is_none(self):
        cm, _ = self.reader()
        self.assertFalse(cm.sample(np.array([40.0]), np.array([-100.0])).any())

    def test_a_refused_request_reads_as_none(self):
        cm = cropmap.CropMap("unused", None)

        def boom(name, kind):
            raise netfetch.RateLimited("later", "u", "h")
        cm._cog = boom
        self.assertFalse(cm.sample(np.array([50.0]), np.array([12.0])).any())

    def test_a_broken_source_is_logged_once_and_leaves_none(self):
        cm = cropmap.CropMap("unused", None)
        n = [0]

        def boom(name, kind):
            n[0] += 1
            raise ValueError("bad tiff")
        cm._cog = boom
        with self.assertLogs("earthd.cropmap", "WARNING") as cap:
            cm.sample(np.array([50.0]), np.array([12.0]))
        self.assertEqual(len(cap.records), 1)
        cm._cog = lambda name, kind: None
        self.assertFalse(cm.sample(np.array([50.0]), np.array([12.0])).any())


class Stub:
    """The whole grid one group, or (box) only inside E [e0, e1] x N [n0, n1] of the frame: one bounded field."""

    def __init__(self, group, box=None, frame=None):
        self.group, self.box, self.frame = group, box, frame

    def crop_groups(self, la, lo):
        out = np.full(la.shape, self.group, np.uint8)
        if self.box is not None:
            e, n, _ = self.frame.geodetic_to_enu(la.ravel(), lo.ravel(), np.zeros(la.size))
            e, n = np.asarray(e).reshape(la.shape), np.asarray(n).reshape(la.shape)
            e0, e1, n0, n1 = self.box
            out[~((e >= e0) & (e <= e1) & (n >= n0) & (n <= n1))] = 0
        return out


def photo(rgb, size=512):
    return np.broadcast_to(np.array(rgb, np.uint8), (size, size, 3)).copy()


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= 148, "needs earthfast >= 148")
class PaintTests(unittest.TestCase):
    frame = geodesy.EnuFrame(50.28, 15.95)

    def paint(self, group, rgb, x0=-250.0, y0=-250.0, size=500.0, px=512):
        return surface.photo_crops(photo(rgb, px), self.frame, x0, y0, size, Stub(group))

    def test_no_field_leaves_the_photo(self):
        out, drawn = self.paint(0, (60, 90, 40))
        self.assertFalse(drawn)
        self.assertTrue((out == photo((60, 90, 40))).all())

    def test_input_is_not_changed_and_result_is_repeatable(self):
        src = photo((60, 90, 40))
        a, drawn = surface.photo_crops(src, self.frame, -250.0, -250.0, 500.0, Stub(cropmap.MAIZE))
        b, _ = surface.photo_crops(src, self.frame, -250.0, -250.0, 500.0, Stub(cropmap.MAIZE))
        self.assertTrue(drawn)
        self.assertTrue((src == photo((60, 90, 40))).all())
        self.assertTrue((a == b).all())
        self.assertFalse((a == src).all())

    def test_green_field_keeps_the_photos_colour(self):
        for g in (cropmap.CEREAL, cropmap.MAIZE, cropmap.SUNFLOWER, cropmap.RAPE, cropmap.ROOT):
            out, _ = self.paint(g, (50, 95, 40))
            inner = out[100:400, 100:400].reshape(-1, 3).mean(0)
            self.assertLess(float(np.abs(inner - np.array([50, 95, 40])).max()), 6.0, cropmap.GROUP_NAMES[g])
            self.assertGreater(float(out[100:400, 100:400].reshape(-1, 3).std(0).max()), 1.0)    # but not flat

    def test_bare_group_shows_furrows_and_soil_tint(self):
        out, _ = self.paint(cropmap.BARE, (120, 100, 80))
        inner = out[100:400, 100:400].astype(float)
        self.assertLess(inner[..., 2].mean(), 80.0)             # pulled toward the dark soil
        # furrows: a strong periodic component across the rows
        spec = np.abs(np.fft.rfft(inner[..., 1].mean(0) - inner[..., 1].mean()))
        self.assertGreater(spec[1:].max(), 3 * np.median(spec[1:]))

    def test_a_solid_field_is_sure_and_its_ragged_edge_is_not(self):
        groups = np.zeros((40, 40), np.uint8)
        groups[10:30, 10:30] = cropmap.MAIZE
        groups[8, 20] = cropmap.MAIZE                      # a lone cell stuck to the field's north edge
        groups[20, 31] = cropmap.CEREAL                    # ... and a speck of another crop just outside
        filt, _, _, sure = fast._ef.crop_grid(groups, 10.0, -200.0, 200.0)
        self.assertEqual(sure[20, 20], 1, "the middle of the field")
        self.assertEqual(sure[0, 0], 0, "no crop, no flag")
        self.assertEqual(filt[8, 20], 0)
        self.assertTrue((sure[filt == 0] == 0).all())

    def test_dirt_road_pixels_are_left_alone(self):
        h = w = 256
        rgb = photo((50, 95, 40), h)
        groups = np.full((60, 60), cropmap.MAIZE, np.uint8)
        filt, ang, ph, _ = fast._ef.crop_grid(groups, 10.0, -300.0, 300.0)
        dirt = np.zeros((h, w), np.uint8)
        dirt[:, 100:120] = 1
        out = rgb.copy()
        fast._ef.crop_paint(out, filt, ang, ph, [-128.0, 128.0, 1.0, -300.0, 300.0, 10.0], dirt)
        self.assertTrue((out[:, 100:120] == rgb[:, 100:120]).all())
        self.assertFalse((out[:, :100] == rgb[:, :100]).all())

    def test_neighbouring_squares_agree_where_they_overlap(self):
        # one photo, two overlapping squares of the same frame: the pattern at the same ground point must match
        size, px = 400.0, 800
        full = np.random.default_rng(3).integers(40, 110, (1000, 1000, 3)).astype(np.uint8)
        full = (full * 0 + np.array([55, 92, 40])).astype(np.uint8)         # a plain green photo
        a_src, b_src = full[:800, :800], full[:800, 200:1000]               # 0.5 m px: b starts 100 m east of a
        stub = Stub(cropmap.MAIZE, (-250.0, 150.0, -100.0, 100.0), self.frame)    # one field, whole in both windows
        a, _ = surface.photo_crops(a_src.copy(), self.frame, -300.0, -200.0, size, stub)
        b, _ = surface.photo_crops(b_src.copy(), self.frame, -200.0, -200.0, size, stub)
        ra, rb = a[:, 200:800].astype(float), b[:, :600].astype(float)
        self.assertLess(float(np.abs(ra - rb).mean()), 2.0)
        self.assertGreater(float(np.corrcoef(ra[..., 1].ravel(), rb[..., 1].ravel())[0, 1]), 0.9)


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= 148, "needs earthfast >= 148")
class LayerTests(unittest.TestCase):
    frame = geodesy.EnuFrame(50.28, 15.95)

    def grid(self, n=64, square=2.0):
        c = (np.arange(n) - n / 2 + 0.5) * square
        E, N = np.meshgrid(c, c[::-1])
        la, lo, _ = self.frame.enu_to_geodetic(E.ravel(), N.ravel(), np.zeros(E.size))
        return np.asarray(la).reshape(n, n), np.asarray(lo).reshape(n, n)

    def test_fields_become_their_layers_and_only_replace_open_ground(self):
        la, lo = self.grid(n=200, square=2.0)
        idx = np.full(la.shape, surface.GRASS, np.uint8)
        idx[:, :20] = surface.ASPHALT                      # a road along the west edge
        idx[:20, :] = surface.ROCK                         # rock along the north edge
        box = (-150.0, 150.0, -150.0, 150.0)
        for group, layer in ((cropmap.MAIZE, surface.CROP_MAIZE), (cropmap.CEREAL, surface.CROP_CEREAL),
                             (cropmap.SUNFLOWER, surface.CROP_SUNFLOWER), (cropmap.RAPE, surface.CROP_BROAD),
                             (cropmap.BARE, surface.DIRT)):
            out = surface.paint_crops(idx, self.frame, la, lo, Stub(group, box, self.frame))
            self.assertTrue((out[:20, :] == surface.ROCK).all(), cropmap.GROUP_NAMES[group])
            self.assertTrue((out[20:, :20] == surface.ASPHALT).all())
            inner = out[60:165, 60:165]
            self.assertGreater(float((inner == layer).mean()), 0.95, cropmap.GROUP_NAMES[group])
        self.assertTrue((idx[20:, 20:] == surface.GRASS).all())            # the input is not changed

    def test_grass_fields_become_pasture_on_farmland_only(self):
        la, lo = self.grid(n=100, square=2.0)

        class Grass:
            def crop_groups(self, la_, lo_):
                return np.zeros(la_.shape, np.uint8)

            def grass_fields(self, la_, lo_):
                return np.ones(la_.shape, bool)

        idx = np.full(la.shape, surface.DIRT, np.uint8)         # farmland by the land cover ...
        idx[:, :20] = surface.ASPHALT                           # ... a road
        idx[:20, :] = surface.GRASS                             # ... and wild grass, which stays what it was
        out = surface.paint_crops(idx, self.frame, la, lo, Grass())
        self.assertTrue((out[20:, 20:] == surface.PASTURE).all())
        self.assertTrue((out[20:, :20] == surface.ASPHALT).all())
        self.assertTrue((out[:20, 20:] == surface.GRASS).all())
        self.assertTrue((idx[20:, 20:] == surface.DIRT).all(), "the input is not changed")

    def test_nothing_to_paint_returns_the_same_array(self):
        la, lo = self.grid()
        idx = np.full(la.shape, surface.GRASS, np.uint8)
        self.assertIs(surface.paint_crops(idx, self.frame, la, lo, Stub(0)), idx)
        self.assertIs(surface.paint_crops(idx, self.frame, la, lo, object()), idx)

    def test_layer_and_photo_share_the_field_edge(self):
        # the ground layer's edge and the photo's painted edge fall within a few metres of each other
        box = (-100.0, 100.0, -100.0, 100.0)
        stub = Stub(cropmap.MAIZE, box, self.frame)
        la, lo = self.grid(n=250, square=2.0)
        lay = surface.paint_crops(np.full(la.shape, surface.GRASS, np.uint8), self.frame, la, lo, stub) == surface.CROP_MAIZE
        src = photo((60, 95, 40), 500)
        out, _ = surface.photo_crops(src, self.frame, -250.0, -250.0, 500.0, stub)
        painted = (out != src).any(axis=2)
        # compare along the row through the middle: first / last painted column vs layer column (1 px = 1 m / 2 m)
        pc = np.flatnonzero(painted[250])
        lc = np.flatnonzero(lay[125])
        self.assertLess(abs(pc[0] - 2 * lc[0]), 12)         # photo px are 1 m, layer samples 2 m
        self.assertLess(abs(pc[-1] - 2 * lc[-1]), 12)

    def test_no_crop_layer_beside_a_road(self):
        n = 40
        pre = np.full((n, n), surface.GRASS, np.uint8)
        with_crops = np.full((n, n), surface.CROP_MAIZE, np.uint8)
        idx = with_crops.copy()
        idx[:, 19:21] = surface.ASPHALT                      # a road two samples wide down the middle (square = 1 m)
        out = surface.crop_road_margin(idx, with_crops, pre, margin_m=3.0)
        self.assertTrue((out[:, 19:21] == surface.ASPHALT).all())
        near = np.r_[16:19, 21:24]
        self.assertTrue((out[:, near] == surface.GRASS).all())          # within 3 m: the ground as before the crops
        far = np.r_[0:14, 26:40]
        self.assertTrue((out[:, far] == surface.CROP_MAIZE).all())      # beyond it the field
        dirt_pre = np.full((n, n), surface.DIRT, np.uint8)       # cropland was dirt: its verge becomes grass
        out2 = surface.crop_road_margin(idx, with_crops, dirt_pre, margin_m=3.0)
        self.assertTrue((out2[:, np.r_[16:19, 21:24]] == surface.GRASS).all())
        self.assertIs(surface.crop_road_margin(with_crops, with_crops, pre), with_crops)       # no road: nothing to do
        self.assertIs(surface.crop_road_margin(idx, with_crops, None), idx)


if __name__ == "__main__":
    unittest.main()
