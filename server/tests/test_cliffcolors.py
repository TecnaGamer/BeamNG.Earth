"""Native satellite colour selection and sparse geographic interpolation."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cliffcolors
import fast


class CliffColorTests(unittest.TestCase):
    def test_disjoint_queries_allocate_only_needed_nodes(self):
        probes, indices, weights = fast._ef.cliff_photo_queries(
            np.array([36., -20.]), np.array([-114., 130.]), 64., 64.)
        self.assertEqual(probes.shape, (8, 81, 2))
        self.assertEqual(indices.shape, (2, 4))
        np.testing.assert_allclose(weights.sum(axis=1), 1.)

    def test_query_order_does_not_change_samples(self):
        lat = np.array([36., 36.0003, 36.0006])
        lon = np.array([-114., -114.0003, -114.0006])
        def photo(_, la, lo):
            shade = ((np.floor(la*100000) + np.floor(lo*100000)).astype(int) % 3) * 8
            return np.stack((190+shade, 175+shade, 150+shade), axis=-1).astype(float)
        with patch.object(cliffcolors, '_samples', side_effect=photo):
            forward = cliffcolors.sample('', lat, lon)
            reverse = cliffcolors.sample('', lat[::-1], lon[::-1])
        np.testing.assert_array_equal(forward, reverse[::-1])

    def test_missing_and_green_photos_use_neutral_stone(self):
        for color in ([0., 0., 0.], [30., 120., 20.]):
            with patch.object(cliffcolors, '_samples', side_effect=lambda _, la, lo:
                              np.broadcast_to(color, (*la.shape, 3))):
                result = cliffcolors.sample('', np.array([36.]), np.array([-114.]))
            np.testing.assert_array_equal(result, [cliffcolors.NEUTRAL_STONE])

    def test_pale_and_olive_canopy_cannot_enter_regional_fallback(self):
        # Placerville cached imagery produced a pale green mineral tint because
        # the regional fallback admitted green up to 4% brighter than red.
        for color in ([171., 176., 166.], [110., 108., 70.]):
            with self.subTest(color=color), patch.object(cliffcolors, '_samples',
                    side_effect=lambda _, la, lo: np.broadcast_to(color, (*la.shape, 3))):
                result = cliffcolors.sample('', np.array([38.7311]), np.array([-120.7934]))
            np.testing.assert_array_equal(result, [cliffcolors.NEUTRAL_STONE])

    def test_rejected_local_patch_uses_surrounding_stone_without_a_colour_step(self):
        # A shadowed/vegetated patch larger than the local sampling footprint.
        lat = np.full(401, 38.86)
        lon = -105.06 + np.linspace(-200., 200., 401)/111320.
        def photo(_, la, lo):
            local = (abs(la-38.86)*111320. < 130) & (abs(lo+105.06)*111320. < 130)
            return np.where(local[..., None], [30., 120., 20.], [190., 175., 150.])
        with patch.object(cliffcolors, '_samples', side_effect=photo):
            whole = cliffcolors.sample('', lat, lon)
            left = cliffcolors.sample('', lat[:201], lon[:201])
            right = cliffcolors.sample('', lat[200:], lon[200:])
        np.testing.assert_allclose(whole, np.broadcast_to([190., 175., 150.], whole.shape), atol=1e-10)
        np.testing.assert_array_equal(left, whole[:201])
        np.testing.assert_array_equal(right, whole[200:])
        self.assertLess(np.abs(np.diff(whole, axis=0)).max(), 1.)

    def test_dry_tan_regional_colour_is_not_diluted_with_gray(self):
        tan = [174., 155., 113.5]
        with patch.object(cliffcolors, '_samples', side_effect=lambda _, la, lo:
                          np.broadcast_to(tan, (*la.shape, 3))):
            result = cliffcolors.sample('', np.array([38.86]), np.array([-105.06]))
        np.testing.assert_allclose(result, [tan], atol=1e-10)

    def test_absent_regional_evidence_fades_without_switching_to_zero(self):
        lat = np.full(401, 38.86)
        lon = -105.06 + np.linspace(-1000., 1000., 401)/111320.
        def photo(_, la, lo):
            return np.where((lo < -105.06)[..., None], [190., 175., 150.], [0., 0., 0.])
        with patch.object(cliffcolors, '_samples', side_effect=photo):
            result = cliffcolors.sample('', lat, lon)
        self.assertGreater(result.min(), 140.)
        self.assertLess(np.abs(np.diff(result, axis=0)).max(), 2.)

    def test_empty_queries_need_no_image_io(self):
        with patch.object(cliffcolors, '_samples', side_effect=AssertionError('unexpected image read')):
            self.assertEqual(cliffcolors.sample('', np.array([]), np.array([])).shape, (0, 3))

    def test_invalid_inputs_fail_before_native_indexing(self):
        for lat, lon, grid in [([np.nan], [0.], 64.), ([91.], [0.], 64.), ([0.], [0.], 0.)]:
            with self.assertRaises(ValueError):
                fast._ef.cliff_photo_queries(np.array(lat), np.array(lon), grid, 64.)
        with self.assertRaises(ValueError):
            fast._ef.cliff_photo_tints(np.zeros((1,81,3)), np.ones((1,4),np.int64), np.ones((1,4)))


if __name__ == '__main__':
    unittest.main()
