from pathlib import Path
import json
import sys
import unittest
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy
import nlsignalcontext as context


class NativeContext(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(52., 5., 0.)
        self.road = dict(kind='road', id='12345678901', geometry=[[4.999, 52.], [5.001, 52.]],
                         tags={'highway': 'primary', 'oneway': 'yes', 'turn:lanes': 'left|through|right'})
        self.query = np.array([[0., 1., 1., 0., 3.]])

    def test_real_geodesy_native_match_preserves_source_and_three_arrows(self):
        urban = dict(kind='urban_section', id='123', geometry=self.road['geometry'])
        got = context.match([self.road, urban], self.frame, self.query)[0]
        self.assertEqual(got['zone'], 'bibeko')
        self.assertEqual(got['zone_wvk_id'], '123')
        self.assertEqual(got['turns_way_id'], '12345678901')
        self.assertEqual(got['turns'], ['left', 'through', 'right'])
        self.assertLess(got['road_distance_m'], 2.)
        json.dumps(got, allow_nan=False)  # the server must send ordinary JSON, not NumPy scalar types

    def test_no_wkd_evidence_remains_unknown_unless_coverage_is_explicit(self):
        self.assertEqual(context.match([], self.frame, self.query)[0]['zone'], 'unknown')
        self.assertEqual(context.match([], self.frame, self.query, complete_wkd=True)[0]['zone'], 'bubeko')

    def test_opposing_oneway_and_wrong_lane_count_cannot_supply_arrows(self):
        q = self.query.copy();q[:, 2] = -1
        self.assertNotIn('road_id', context.match([self.road], self.frame, q)[0])
        q = self.query.copy();q[:, 4] = 2
        self.assertNotIn('turns', context.match([self.road], self.frame, q)[0])

    def test_ambiguous_same_distance_arrows_are_not_guessed(self):
        other = dict(self.road, id='2', tags=dict(self.road['tags'], **{'turn:lanes': 'through|through|right'}))
        self.assertNotIn('turns', context.match([self.road, other], self.frame, self.query)[0])

    def test_corrupt_source_and_array_shape_fail(self):
        with self.assertRaises(ValueError):
            context.match([dict(self.road, geometry=[[float('nan'), 52.]])], self.frame, self.query)
        with self.assertRaises(ValueError):
            context.match([], self.frame, np.zeros((2, 3)))


if __name__ == '__main__':
    unittest.main()
