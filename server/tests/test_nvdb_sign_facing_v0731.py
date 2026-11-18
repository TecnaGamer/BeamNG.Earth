"""Offline tests for the missing v0.7.3 facing module."""
import math
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent if HERE.name == 'tests' else HERE))
from norway_signs_v073.facing import facing


class FacingTests(unittest.TestCase):
    def test_parallel_away_and_toward(self):
        self.assertEqual(facing((5, 0), (0, 0), 'parallelt fra veg', 'arrow'), (1, 0))
        self.assertEqual(facing((5, 0), (0, 0), 'parallelt mot veg', 'arrow'), (-1, 0))

    def test_traffic_face(self):
        self.assertEqual(facing((4, 0), (0, 0), 'trafikk i metreringsretning', 'direction'), (0, -1))
        self.assertEqual(facing((4, 0), (0, 0), 'trafikk mot metreringsretning', 'direction'), (0, -1))

    def test_double_sided_route_only(self):
        self.assertEqual(facing((4, 0), (0, 0), 'tosidig', 'route'), (0, -1))
        self.assertIsNone(facing((4, 0), (0, 0), 'tosidig', 'arrow'))

    def test_reject_unusable(self):
        self.assertIsNone(facing((0, 0), (0, 0), 'parallelt fra veg', 'route'))
        self.assertIsNone(facing((1, 0), (0, 0), 'parallelt fra veg', 'route'))
        self.assertIsNone(facing((26, 0), (0, 0), 'parallelt fra veg', 'route'))
        self.assertIsNone(facing((math.nan, 0), (0, 0), 'parallelt fra veg', 'route'))
        self.assertIsNone(facing((4, 0), (0, 0), 'unknown', 'route'))

    def test_normalized(self):
        x, y = facing((5, 9), (2, 5), 'parallelt fra veg', 'direction')
        self.assertAlmostEqual(math.hypot(x, y), 1.0)

if __name__ == '__main__':
    unittest.main()
