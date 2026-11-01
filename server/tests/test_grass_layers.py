"""Generated terrain files, masks and shared imagery neighbourhoods for grass palettes."""
import os
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy  # noqa: E402
import grass_imagery as gi  # noqa: E402
import grass_layers as gl  # noqa: E402
import heightmap  # noqa: E402
import surface as sf  # noqa: E402

DRY = (160, 130, 80)
LIGHT = (125, 151, 80)
LUSH = (42, 78, 36)
VEGETATED = (sf.GRASS, sf.GRASS_DRY, sf.GRASS_LIGHT, sf.DUSTY, sf.DUSTY_LIGHT, sf.DUSTY_LUSH)


def solid(color, px=32):
    return np.broadcast_to(np.array(color, np.uint8), (px, px, 3)).copy()


class ContinuousImagery:
    """A reproducible continuous geographic image, sampled only through the public render API."""
    def __init__(self):
        self.calls = []

    def render(self, frame, x0, y0, size, px, pending):
        self.calls.append((x0, y0, size, px, pending))
        axis = (np.arange(px) + 0.5) * size / px
        x, y = np.meshgrid(x0 + axis, y0 + size - axis)
        rgb = np.stack((135 + 35 * np.sin(x / 22), 135 + 28 * np.cos(y / 30),
                        68 + 16 * np.sin((x + y) / 19)), axis=-1)
        return np.clip(np.round(rgb), 0, 255).astype(np.uint8), {"fake": px * px}


class GrassLayersTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.frame = geodesy.EnuFrame(41.14, -104.82)

    def block(self, idx, stem="b"):
        qgrid = np.arange(idx.size, dtype=np.uint16).reshape(idx.shape)
        sf.write_ter(self.root, stem + ".ter", qgrid, idx)
        masks = sf.write_masks(idx[::-1], self.root, stem)
        return masks, gl.read_ter(os.path.join(self.root, stem + ".ter"))

    def apply(self, masks, colors, rgb=None, stem="b", cx=0.0, cy=0.0, size=64.0):
        used = gl.apply(self.root, stem + ".ter", masks, solid(LUSH) if rgb is None else rgb,
                        colors, self.frame, cx, cy, size)
        qgrid, idx, listed = gl.read_ter(os.path.join(self.root, stem + ".ter"))
        self.assertEqual(used, listed)
        return qgrid, idx, listed

    def assert_masks(self, masks, idx):
        for material, name in enumerate(masks):
            with Image.open(os.path.join(self.root, name)) as image:
                actual = np.asarray(image)
            np.testing.assert_array_equal(actual[1:-1, 1:-1],
                                          np.where(idx[::-1, :][1:-1, 1:-1] == material, 255, 0),
                                          err_msg="mask differs from .ter for " + sf.MATERIALS[material][0])

    def test_uniform_environments_change_types_and_preserve_quantity(self):
        for color, grass_layer, shrub_layer in ((DRY, sf.GRASS_DRY, sf.DUSTY),
                                                (LIGHT, sf.GRASS_LIGHT, sf.DUSTY_LIGHT),
                                                (LUSH, sf.GRASS, sf.DUSTY_LUSH)):
            with self.subTest(color=color):
                original = np.full((32, 32), sf.GRASS, np.uint8)
                original[:, 16:] = sf.DUSTY
                masks, (heights, before, _) = self.block(original)
                after_heights, after, used = self.apply(masks, solid(color))
                np.testing.assert_array_equal(heights, after_heights)
                self.assertTrue((after[1:-1, 1:16] == grass_layer).all())
                self.assertTrue((after[1:-1, 16:-1] == shrub_layer).all())
                self.assertEqual(int(np.isin(before, VEGETATED).sum()),
                                 int(np.isin(after, VEGETATED).sum()), "palette choices do not thin grass")
                self.assertEqual(used, sorted({grass_layer, shrub_layer}), "no unused palette reservations")
                self.assert_masks(masks, after)

    def test_nonvegetated_surface_indices_and_holes_survive_palette_changes(self):
        original = np.full((32, 32), sf.GRASS, np.uint8)
        excluded = (sf.ASPHALT, sf.ROCK, sf.MUD, sf.DIRT, sf.LOOSE, sf.COBBLE, heightmap.TER_HOLE)
        for row, material in enumerate(excluded, start=4):
            original[row, 3:29] = material
        masks, (_, before, _) = self.block(original)
        _, after, used = self.apply(masks, solid(DRY))
        keep = ~np.isin(before, VEGETATED)
        np.testing.assert_array_equal(after[keep], before[keep])
        np.testing.assert_array_equal(after == heightmap.TER_HOLE, before == heightmap.TER_HOLE)
        self.assertNotIn(sf.GRASS, used)
        self.assertNotIn(sf.GRASS_LIGHT, used)
        self.assertNotIn(sf.DUSTY_LIGHT, used)
        self.assert_masks(masks, after)

    def test_green_cropland_stays_bare_beside_green_grassland(self):
        # Irrigated crops can look as green as a meadow in the aerial photo.
        # WorldCover eligibility must win over both green palette choices.
        classes = np.full((32, 32), 40, np.uint8)       # WorldCover cropland
        classes[:, 16:] = 30                          # adjacent grassland
        original = sf.classify(classes, np.zeros(classes.shape), 2.0)
        self.assertTrue((original[:, :16] == sf.DIRT).all())
        self.assertTrue((original[:, 16:] == sf.GRASS).all())
        for color, expected in ((LIGHT, sf.GRASS_LIGHT), (LUSH, sf.GRASS)):
            with self.subTest(color=color):
                masks, _ = self.block(original)
                _, after, used = self.apply(masks, solid(color), rgb=solid(color))
                self.assertTrue((after[1:-1, 1:16] == sf.DIRT).all(),
                                "green imagery must not promote cropland into grass")
                self.assertFalse(np.isin(after[1:-1, 1:16], VEGETATED).any())
                self.assertTrue((after[1:-1, 16:-1] == expected).all(),
                                "the same imagery selects green grass on eligible grassland")
                self.assertEqual(used, sorted((sf.DIRT, expected)))
                for material in VEGETATED:
                    with Image.open(os.path.join(self.root, masks[material])) as image:
                        self.assertFalse(np.asarray(image)[:, :16].any(),
                                         "cropland must stay empty in every grass palette mask")
                self.assert_masks(masks, after)

    def test_reapplying_a_palette_is_stable_and_new_imagery_replaces_old_types(self):
        masks, (_, before, _) = self.block(np.full((32, 32), sf.GRASS, np.uint8))
        _, dry, dry_used = self.apply(masks, solid(DRY))
        with open(os.path.join(self.root, "b.ter"), "rb") as stream:
            first = stream.read()
        _, again, again_used = self.apply(masks, solid(DRY))
        with open(os.path.join(self.root, "b.ter"), "rb") as stream:
            self.assertEqual(first, stream.read(), "same imagery produces byte-identical terrain")
        np.testing.assert_array_equal(dry, again)
        self.assertEqual(dry_used, again_used)
        _, light, used = self.apply(masks, solid(LIGHT))
        self.assertTrue((light[1:-1, 1:-1] == sf.GRASS_LIGHT).all())
        self.assertEqual(used, [sf.GRASS_LIGHT])
        self.assertEqual(int(np.isin(before, VEGETATED).sum()), int(np.isin(light, VEGETATED).sum()))
        self.assert_masks(masks, light)
        with Image.open(os.path.join(self.root, masks[sf.GRASS_DRY])) as image:
            self.assertFalse(np.asarray(image).any(), "old palette masks are emptied")

    def test_original_lawn_and_snow_photo_vetoes_still_apply(self):
        original = np.full((32, 32), sf.LAWN, np.uint8)
        original[:, 16:] = sf.SNOW
        rgb = solid(LUSH)
        rgb[:, 8:16] = (180, 180, 180)       # paved lawn candidate
        rgb[:, 16:24] = (235, 238, 242)      # white snow candidate
        rgb[:, 24:] = DRY                  # snow map's summer scree
        masks, _ = self.block(original)
        _, after, used = self.apply(masks, solid(DRY), rgb=rgb)
        for c0, c1, material in ((1, 8, sf.LAWN), (8, 16, sf.DIRT),
                                 (16, 24, sf.SNOW), (24, 31, sf.LOOSE)):
            self.assertTrue((after[1:-1, c0:c1] == material).all())
        self.assertEqual(used, sorted((sf.LAWN, sf.DIRT, sf.SNOW, sf.LOOSE)))
        self.assert_masks(masks, after)

    def test_missing_analysis_retains_original_eligibility(self):
        original = np.full((32, 32), sf.DUSTY, np.uint8)
        original[:, 16:] = sf.ASPHALT
        masks, (_, before, _) = self.block(original)
        _, after, used = self.apply(masks, None)
        np.testing.assert_array_equal(after, before)
        self.assertEqual(used, sorted((sf.DUSTY, sf.ASPHALT)))

    def test_overlapping_blocks_have_identical_palette_choices(self):
        image = ContinuousImagery()
        original = np.full((64, 64), sf.GRASS, np.uint8)
        masks_a, _ = self.block(original, "a")
        masks_b, _ = self.block(original, "b")
        _, _, colors_a = gi.render_base(image, self.frame, -65, -65, 128, 128, set())
        _, _, colors_b = gi.render_base(image, self.frame, -33, -65, 128, 128, set())
        # A 32 m move is 16 terrain samples and 32 analysis texels. The halo is
        # identical geographic evidence even where either original photo ends.
        np.testing.assert_array_equal(colors_a[:, 32:], colors_b[:, :96])
        _, a, _ = self.apply(masks_a, colors_a, stem="a", size=128)
        _, b, _ = self.apply(masks_b, colors_b, stem="b", cx=32, size=128)
        np.testing.assert_array_equal(a[1:-1, 17:-1], b[1:-1, 1:47])


class GrassImageryTest(unittest.TestCase):
    def test_one_halo_render_preserves_ground_photo_footprint_and_pending_set(self):
        image = ContinuousImagery()
        frame, pending = object(), {"existing"}
        rgb, used, colors = gi.render_base(image, frame, 10, 20, 128, 64, pending)
        self.assertEqual(len(image.calls), 1, "one shared imagery render, never a request per grass item")
        x0, y0, size, px, seen_pending = image.calls[0]
        self.assertIs(seen_pending, pending)
        self.assertLessEqual(x0, 10 - 12)
        self.assertLessEqual(y0, 20 - 12)
        self.assertEqual(size / px, 128 / 64)
        self.assertEqual(px % (gi.imagerymod.MESH_CELLS * gi.imagerymod.MATCH_DOWN), 0,
                         "the native warp and lazy color reference need complete mesh cells")
        direct, _ = image.render(frame, 10, 20, 128, 64, pending)
        np.testing.assert_array_equal(rgb, direct)
        self.assertEqual(rgb.shape, (64, 64, 3))
        self.assertEqual(colors.shape, (64, 64, 3))
        self.assertEqual(used, {"fake": 64 * 64})

    def test_analysis_size_is_bounded_for_large_base_photos(self):
        rgb, _, colors = gi.render_base(ContinuousImagery(), object(), -513, -513, 1024, 1024, set())
        self.assertEqual(rgb.shape, (1024, 1024, 3))
        self.assertEqual(colors.shape, (256, 256, 3))
        self.assertEqual(colors.dtype, np.float32)

    def test_downsampled_halos_agree_after_a_normal_block_move(self):
        image = ContinuousImagery()
        _, _, a = gi.render_base(image, object(), -513, -513, 1024, 1024, set())
        _, _, b = gi.render_base(image, object(), -385, -513, 1024, 1024, set())
        # The production 128 m movement is 32 of the 4 m analysis cells.
        np.testing.assert_array_equal(a[:, 32:], b[:, :224])

    def test_sample_coordinates_match_terrain_nodes_and_north_south_orientation(self):
        image = ContinuousImagery()
        rgb, _ = image.render(object(), -33, -33, 64, 64, set())
        actual = gl.sample_colors(rgb, 32)
        # With the existing 1 m base shift, each 2 m terrain sample lies
        # between four 1 m texel centres, including the south-first row flip.
        expected = (rgb[::-1, :].astype(np.float32).reshape(32, 2, 32, 2, 3).mean(axis=(1, 3)))
        np.testing.assert_array_equal(actual, expected)


if __name__ == "__main__":
    unittest.main()
