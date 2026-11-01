"""Grass palettes: image evidence, blended selection and geographic continuity."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import grass  # noqa: E402


class ColourTest(unittest.TestCase):
    def test_brown_tan_gold_and_dusty_yellow_are_dry(self):
        c = [(132, 107, 73), (177, 159, 118), (170, 155, 94), (159, 147, 98)]
        w = grass.color_weights(c)
        self.assertTrue((w[:, grass.DRY] > 0.85).all(), w)

    def test_pale_muted_and_yellow_green_are_transitional(self):
        c = [(145, 167, 114), (116, 136, 85), (142, 150, 95)]
        w = grass.color_weights(c)
        self.assertTrue((w[:, grass.LIGHT] > w[:, grass.DRY]).all(), w)
        self.assertTrue((w[:, grass.LIGHT] > w[:, grass.LUSH]).all(), w)

    def test_dark_rich_green_is_lush(self):
        w = grass.color_weights([(42, 75, 38), (55, 80, 52), (46, 95, 41)])
        self.assertTrue((w[:, grass.LUSH] > 0.7).all(), w)

    def test_neutral_white_and_shadow_take_contextual_prior(self):
        prior = np.array([0.8, 0.2, 0.0])
        c = [(90, 90, 90), (250, 250, 250), (5, 6, 4), (0, 0, 0)]
        np.testing.assert_allclose(grass.color_weights(c, prior), np.tile(prior, (4, 1)), atol=1e-6)
        np.testing.assert_allclose(grass.color_weights(c), np.tile([0, 0, 1], (4, 1)), atol=1e-6)

    def test_weights_are_normalized_and_continuous(self):
        dry, green = np.array([150, 130, 85]), np.array([105, 146, 88])
        c = np.linspace(dry, green, 101)
        w = grass.color_weights(c)
        np.testing.assert_allclose(w.sum(-1), 1, atol=1e-6)
        self.assertTrue((w >= 0).all())
        self.assertLess(np.abs(np.diff(w, axis=0)).max(), 0.06)
        self.assertGreater(w[0, grass.DRY], w[-1, grass.DRY])


class SamplingTest(unittest.TestCase):
    def coordinates(self):
        lat, lon = np.meshgrid(41.0 + np.arange(150) * 2 / 111320,
                              -104.9 + np.arange(160) * 2 / 84000, indexing="ij")
        return lat, lon

    def test_blended_distribution_changes_type_and_not_quantity(self):
        lat, lon = self.coordinates()
        w = np.broadcast_to([0.6, 0.3, 0.1], lat.shape + (3,))
        a = grass.choose(w, lat, lon)
        shares = np.bincount(a.ravel(), minlength=3) / a.size
        np.testing.assert_allclose(shares, [0.6, 0.3, 0.1], atol=0.015)
        self.assertEqual(a.size, lat.size)
        self.assertTrue((a < 3).all(), "classification never removes a grass sample")

    def test_reload_split_tiles_and_reordered_samples_agree(self):
        lat, lon = self.coordinates()
        w = np.broadcast_to([0.3, 0.4, 0.3], lat.shape + (3,))
        whole = grass.choose(w, lat, lon)
        again = grass.choose(w.copy(), lat.copy(), lon.copy())
        np.testing.assert_array_equal(whole, again)
        split = np.concatenate([grass.choose(w[:, :80], lat[:, :80], lon[:, :80]),
                                grass.choose(w[:, 80:], lat[:, 80:], lon[:, 80:])], axis=1)
        np.testing.assert_array_equal(whole, split)
        reverse = grass.choose(w.reshape(-1, 3)[::-1], lat.ravel()[::-1], lon.ravel()[::-1])
        np.testing.assert_array_equal(whole.ravel(), reverse[::-1])

    def test_excluded_surfaces_never_get_a_category(self):
        # Asphalt, concrete, buildings, bare rock, water and explicitly bare
        # land all remain excluded even when their imagery is brown or green.
        w = grass.color_weights(np.tile([132, 107, 73], (7, 1)))
        eligible = np.array([False] * 6 + [True])
        out = grass.choose(w, np.full(7, 41.1), np.arange(7) * 0.001 - 105, eligible)
        np.testing.assert_array_equal(out[:6], np.full(6, grass.EXCLUDED))
        self.assertEqual(out[-1], grass.DRY)

    def test_float_frame_noise_keeps_same_world_choice(self):
        lat, lon = self.coordinates()
        np.testing.assert_array_equal(grass.world_random(lat, lon),
                                      grass.world_random(lat + 1e-12, lon - 1e-12))


class SmoothingTest(unittest.TestCase):
    def test_halos_make_adjacent_tile_boundaries_agree(self):
        rng = np.random.default_rng(125)
        image = rng.integers(0, 255, (60, 96, 3), dtype=np.uint8)
        radius, edge = 6, 48
        whole = grass.smooth_colors(image, radius)
        left = grass.smooth_colors(image[:, :edge + radius], radius)[:, :edge]
        right = grass.smooth_colors(image[:, edge - radius:], radius)[:, radius:]
        np.testing.assert_array_equal(np.concatenate((left, right), axis=1), whole)
        # The colour evidence and therefore the selected palette at shared world
        # positions agree too; no tile ID participates in either operation.
        np.testing.assert_array_equal(grass.color_weights(right), grass.color_weights(whole[:, edge:]))

    def test_average_removes_individual_pixel_noise(self):
        image = np.full((32, 32, 3), [135, 120, 80], np.uint8)
        image[16, 16] = [20, 150, 30]
        smoothed = grass.smooth_colors(image, 5)
        self.assertGreater(grass.color_weights(smoothed[16, 16])[grass.DRY], 0.9)
        np.testing.assert_array_equal(grass.smooth_colors(image, 0), image)


if __name__ == "__main__":
    unittest.main()
