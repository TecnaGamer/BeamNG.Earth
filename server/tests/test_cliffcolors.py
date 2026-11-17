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

    def test_missing_and_green_photos_do_not_tint_rock(self):
        for color in ([0., 0., 0.], [30., 120., 20.]):
            with patch.object(cliffcolors, '_samples', side_effect=lambda _, la, lo:
                              np.broadcast_to(color, (*la.shape, 3))):
                result = cliffcolors.sample('', np.array([36.]), np.array([-114.]))
            np.testing.assert_array_equal(result, [[0., 0., 0.]])

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
