"""Oil wells for the pump jacks (oilwells.near / tile_wells): the wells in a box but the gas wells and pump=no ones;
a road tile's rows in the anchor frame on the ground, yaw hashed from the position (the same every time)."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy  # noqa: E402
import oilwells  # noqa: E402
import roads  # noqa: E402


class OilWellsTest(unittest.TestCase):
    def setUp(self):
        self.saved = oilwells._data
        lat = np.array([31.9000, 31.9001, 31.9002, 31.9003, 35.0], np.float64)
        lon = np.array([-102.1, -102.1, -102.1, -102.1, -102.1], np.float64)
        flags = np.array([0, oilwells.GAS, oilwells.NO_PUMP, 0, 0], np.uint8)
        order = np.argsort(lat)
        oilwells._data = ((lat[order] * 1e6).round().astype(np.int32), (lon[order] * 1e6).round().astype(np.int32),
                          flags[order])

    def tearDown(self):
        oilwells._data = self.saved

    def test_near_leaves_out_gas_and_no_pump(self):
        la, lo = oilwells.near(31.89, -102.2, 31.91, -102.0)
        self.assertEqual(sorted(np.round(la, 4)), [31.9, 31.9003])

    def test_tile_rows(self):
        f = geodesy.EnuFrame(31.9, -102.1)
        e, n, _ = f.geodetic_to_enu(np.array([31.9]), np.array([-102.1]), np.zeros(1))
        tx, ty = int(np.floor(e[0] / roads.TILE)), int(np.floor(n[0] / roads.TILE))
        rows = oilwells.tile_wells(f, tx, ty, lambda la, lo: np.full(np.shape(la), 850.0))
        self.assertTrue(rows)
        x, y, z, yaw = rows[0]
        self.assertAlmostEqual(z, float(f.geodetic_to_enu(np.array([31.9]), np.array([-102.1]),
                                                           np.array([850.0]))[2][0]), delta=0.05)
        self.assertTrue(0 <= yaw < 360)
        self.assertEqual(rows, oilwells.tile_wells(f, tx, ty, lambda la, lo: np.full(np.shape(la), 850.0)))


if __name__ == "__main__":
    unittest.main()
