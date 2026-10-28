"""The Koppen-Geiger class at a point (climate.koppen_at, Beck et al. 2023 at 0.1 degrees): known places' classes, a
coast's from the land beside it, the open sea none."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import climate  # noqa: E402


class ClimateTest(unittest.TestCase):
    def test_places(self):
        self.assertEqual(climate.koppen_at(39.29, -76.61), 14)      # Baltimore: Cfa
        self.assertEqual(climate.koppen_at(51.5, -0.12), 15)        # London: Cfb
        self.assertEqual(climate.koppen_at(41.9, 12.5), 8)          # Rome: Csa
        self.assertEqual(climate.koppen_at(33.45, -112.07), 4)      # Phoenix: BWh
        self.assertEqual(climate.koppen_at(-3.1, -60.0), 1)         # Manaus: Af

    def test_coast_and_sea(self):
        self.assertEqual(climate.koppen_at(38.3365, -75.0855), 14)  # Ocean City's beach: the land beside it
        self.assertEqual(climate.koppen_at(30.0, -40.0), 0)         # the mid-Atlantic


if __name__ == "__main__":
    unittest.main()
