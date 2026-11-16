"""Offline tests of the Norway-only bridge follow-up (no NVDB network needed)."""
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock

import numpy as np

MODULE = pathlib.Path(__file__).resolve().parents[1] / 'server' / 'nvdb_bridge_fixes_v059.py'
spec = importlib.util.spec_from_file_location('nvdb_bridge_fixes_v059', MODULE)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.lon = np.array([7.6700, 7.6701, 7.6702, 7.6703])
        self.lat = np.full(4, 62.89)

    def widths(self, sample):
        m = types.ModuleType('nvdb_width_profile_v03')
        m.at_points = lambda lon, lat, original: None if sample is None else np.array(sample)
        return mock.patch.dict(sys.modules, {'nvdb_width_profile_v03': m})

    def test_straight_bridge_measured(self):
        with self.widths([7.4, 7.4, 7.4, 7.4]):
            result = b.bridge_widths(self.lon, self.lat, 9.0)
        self.assertTrue(result._nvdb_matched)
        np.testing.assert_allclose(result, 7.4)

    def test_poor_coverage_keeps_original(self):
        with self.widths([7.4, 18.75, 18.75, 7.4]):
            self.assertIsNone(b.bridge_widths(self.lon, self.lat, 9.0))

    def test_partial_good_coverage_has_constant_width(self):
        with self.widths([7.4, 18.75, 7.5, 7.5]):
            result = b.bridge_widths(self.lon, self.lat, 9.0)
        np.testing.assert_allclose(result, 7.5)

    def test_conflicting_widths_rejected(self):
        with self.widths([7.2, 7.2, 10.8, 10.8]):
            self.assertIsNone(b.bridge_widths(self.lon, self.lat, 9.0))

    def test_fallback_native_tapers_when_missing(self):
        with self.widths(None):
            result = b.bridge_widths(self.lon, self.lat, 9.0,
                                     [(self.lon[0], self.lat[0], 10.5, 30.0)])
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result[0], 10.5)
        self.assertGreater(result[0], result[-1])
        self.assertFalse(getattr(result, '_nvdb_matched', False))

    def test_outside_norway_untouched(self):
        with self.widths([7.4] * 4):
            self.assertIsNone(b.bridge_widths(self.lon, np.full(4, 51.0), 9.0))

    def test_structural_margin_constant(self):
        p = np.column_stack((np.arange(4), np.zeros(4), np.ones(4)))
        out = b.structural_deck_widths([7.4] * 4, p, p)
        self.assertEqual(out, [8.3] * 4)

    def test_short_concrete_nub_removed_not_parapet(self):
        fr = types.SimpleNamespace(lon0=7.67, lat0=62.89)
        deck = [{'deck': True, 'n': [[0., 0., 1.], [15., 0., 1.]]}]
        nub = {'concrete': True, 'base': [[0., 1.], [10., 1.]]}
        parapet = {'concrete': True, 'parapet': True, 'base': [[0., 1.], [10., 1.]]}
        stone = {'concrete': False, 'base': [[0., 1.], [10., 1.]]}
        long = {'concrete': True, 'base': [[0., 1.], [30., 1.]]}
        out = b.clean_walls(fr, [nub, parapet, stone, long], deck)
        self.assertEqual(out, [parapet, stone, long])

    def test_grade_core_urban_unchanged(self):
        fr = types.SimpleNamespace(lon0=7.67, lat0=62.89)
        spec = types.SimpleNamespace(kind='paved', urban=True, median=False, mountain=False)
        self.assertEqual(b.wall_width_core(fr, [], spec, 12., 7., 1.), (12., 7.))


if __name__ == '__main__':
    unittest.main()
