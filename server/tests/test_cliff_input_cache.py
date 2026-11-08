"""Cliff receiver caches must be bounded by memory, not just by tile count."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blockworker as bw

class CliffInputCacheTest(unittest.TestCase):
    def test_byte_eviction_preserves_current_record(self):
        a = {'cliff_strips': [{'faces': np.zeros(80, np.uint8)}]}
        with patch.object(bw, '_geo_inputs', {}), patch.object(bw, 'GEO_CLIFF_INPUT_BYTES', 100):
            bw._keep_skin_inputs('old', a)
            bw._keep_skin_inputs('new', a)
            self.assertEqual(list(bw._geo_inputs), ['new'])
            self.assertEqual(a['cliff_strips'][0]['faces'].size, 80)
            bw._clear_skin_inputs()
            self.assertFalse(bw._geo_inputs)

    def test_oversize_record_is_not_retained(self):
        with patch.object(bw, '_geo_inputs', {}), patch.object(bw, 'GEO_CLIFF_INPUT_BYTES', 10):
            bw._keep_skin_inputs('large', {'cliff_strips': [{'faces': np.zeros(11, np.uint8)}]})
            self.assertFalse(bw._geo_inputs)

    def test_replacement_and_road_count_limit(self):
        with patch.object(bw, '_geo_inputs', {}), patch.object(bw, 'GEO_INPUTS_KEPT', 2):
            for key in ['a', 'b', 'a', 'c']:
                bw._keep_skin_inputs(key, {'chunks': []})
            self.assertEqual(list(bw._geo_inputs), ['a', 'c'])

if __name__ == '__main__':
    unittest.main()
