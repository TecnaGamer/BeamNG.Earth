"""The trees' leaf cycle (leafcycle.py, phenology.rs): anchor_ready's "leaf" field at places and its switch."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import leafcycle  # noqa: E402


class TestLeafCycle(unittest.TestCase):
    def test_off_by_default(self):
        if not os.environ.get("BNE_LEAF_CYCLE"):
            self.assertFalse(leafcycle.ON)

    def test_baltimore(self):
        f = leafcycle.anchor_leaf(39.29, 50.0, 14)       # Cfa
        self.assertEqual(f["v"], leafcycle.VERSION)
        self.assertEqual(f["share"], 1.0)
        self.assertEqual(len(f["cal"]), 4)
        for row in f["cal"]:
            self.assertEqual(len(row), 6)
            self.assertEqual(row, sorted(row))
        beech = f["cal"][1]
        self.assertTrue(95 <= beech[0] <= 110, beech)          # leaf-out mid April
        self.assertTrue(310 <= beech[5] <= 330, beech)         # bare late November
        self.assertEqual(f["spread"], [6.0, 9.0])

    def test_places(self):
        self.assertEqual(leafcycle.anchor_leaf(33.5, 340.0, 4)["share"], 0.25)   # Phoenix: hot desert
        self.assertEqual(leafcycle.anchor_leaf(1.3, 10.0, 1)["share"], 0.0)      # Singapore
        de = leafcycle.anchor_leaf(50.5, 300.0, 15)["cal"][1]                    # Germany, maritime: PEP725 116.6
        self.assertLess(abs(de[0] - 116.6), 6)
        hi = leafcycle.anchor_leaf(50.5, 1300.0, 15)["cal"][1]                   # 1000 m higher: later, earlier
        self.assertGreater(hi[0], de[0] + 20)
        self.assertLess(hi[5], de[5] - 10)
        south = leafcycle.anchor_leaf(-39.29, 50.0, 14)["cal"]                   # northern-equivalent days
        self.assertEqual(south, leafcycle.anchor_leaf(39.29, 50.0, 14)["cal"])
        self.assertEqual(leafcycle.anchor_leaf(45.0, None, 26)["elev"], 0.0)     # no ground height: sea level


if __name__ == "__main__":
    unittest.main()
