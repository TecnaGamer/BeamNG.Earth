"""shops: real repair shops and property near the player, with property prices."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mapgeom as mg  # noqa: E402
import shops  # noqa: E402
from test_gasstations import FakeMD, feats  # noqa: E402
from test_jobfind import LAT0, LON0, place, spot  # noqa: E402


class ShopsTest(unittest.TestCase):
    def setUp(self):
        self.md = FakeMD({"places": feats("places", [
            place("r1", 300.0, 0.0, name="Joe's Auto"), place("r2", 900.0, 0.0, cat="auto_body_shop", name="Body Pro"),
            place("s1", 500.0, 0.0, cat="self_storage", name="Public Storage"),
            place("p1", 700.0, 0.0, cat="parking_garage", name="Main St Garage"),
            place("c1", 100.0, 0.0, cat="cafe", name="Cafe"),
            place("far", 9000.0, 0.0, name="Too Far Auto")], mg.POINT)})

    def test_repair_shops(self):
        got = shops.near(self.md, LAT0, LON0, "repair", spot_fn=spot)
        self.assertEqual([g["name"] for g in got], ["Joe's Auto", "Body Pro"])
        self.assertLess(got[0]["dist"], got[1]["dist"])
        self.assertNotIn("buy", got[0])

    def test_property_with_prices(self):
        got = shops.near(self.md, LAT0, LON0, "property", spot_fn=spot)
        self.assertEqual([g["name"] for g in got], ["Public Storage", "Main St Garage"])
        for g in got:
            self.assertTrue(18000 <= g["buy"] <= 60000 and 400 <= g["rent"] <= 1200)
        self.assertEqual(shops.prices("s1"), shops.prices("s1"))


if __name__ == "__main__":
    unittest.main()
