"""The Koppen-Geiger class at a point (climate.koppen_at, Beck et al. 2023 at 0.1 degrees): known places' classes, a
coast's from the land beside it, the open sea none."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np  # noqa: E402
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

    def test_australia(self):       # fork
        self.assertEqual(climate.koppen_at(-27.47, 153.03), 14)     # Brisbane: Cfa
        self.assertEqual(climate.koppen_at(-23.44, 144.25), 6)      # Longreach: BSh
        self.assertEqual(climate.koppen_at(-16.92, 145.77), 2)      # Cairns: Am
        lat = np.array([-27.47, -23.44, -16.92, -37.82, 51.5, -5.0, -27.47])
        lon = np.array([153.03, 144.25, 145.77, 144.96, -0.12, 130.0, 100.0])
        self.assertEqual(climate.in_australia(lat, lon).tolist(), [True, True, True, True, False, False, False])
        self.assertEqual(climate.dry_australia(lat, lon).tolist(), [True, True, False, False, False, False, False])
        self.assertEqual(climate.koppen_grid(lat[:2], lon[:2]).tolist(), [14, 6])
        self.assertEqual(climate.koppen_grid(np.array([30.0]), np.array([-40.0])).tolist(), [0])   # the sea
        self.assertTrue(bool(climate.dry_australia(-27.47, 153.03)))                                # scalars too

    def test_au_state(self):        # fork: the state outlines, a coast's nearest state, nothing past 5 km
        for (lat, lon), state in {(-27.47, 153.03): "QLD", (-28.18, 153.54): "NSW", (-28.168, 153.536): "QLD",
                                  (-9.44, 147.18): None, (-42.88, 147.33): "TAS", (-33.87, 151.21): "NSW",
                                  (-27.47, 153.5): "QLD", (-27.47, 156.5): None}.items():
            self.assertEqual(climate.au_state(lat, lon), state, (lat, lon))
        got = climate.in_australia(np.array([-27.47, -9.44]), np.array([153.03, 147.18]))
        self.assertEqual(got.tolist(), [True, False])               # arrays too


if __name__ == "__main__":
    unittest.main()
