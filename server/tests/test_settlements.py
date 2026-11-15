"""The bundled offline populated-place index used by adaptive traffic."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import settlements  # noqa: E402


class SettlementIndexTest(unittest.TestCase):
    def test_bundled_catalogue_and_melbourne_lookup(self):
        index = settlements.load_index()
        self.assertIsNotNone(index)
        self.assertGreater(len(index), 6000)
        result = index.nearest(-37.818, 144.973)
        self.assertEqual(result["name"], "Melbourne")
        self.assertEqual(result["sizeClass"], "metropolitan")
        self.assertLess(result["distanceM"], 1000)
        self.assertAlmostEqual(result["lat"], -37.818, places=3)
        self.assertAlmostEqual(result["lon"], 144.973, places=3)

    def test_lookup_handles_missing_data(self):
        self.assertIsNone(settlements.SettlementIndex([]).nearest(0, 0))

    def test_invalid_coordinates_return_none(self):
        index = settlements.SettlementIndex([("Test", 1, 1, 7, 0)])
        self.assertIsNone(index.nearest(float("nan"), 1))
        self.assertIsNone(index.nearest(1, "not-a-number"))


if __name__ == "__main__":
    unittest.main()
