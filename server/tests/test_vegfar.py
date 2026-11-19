"""The far forest ring (vegfar.py): squares of the canopy's overview, the clumps' kinds, a geotile's rows."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geotiles  # noqa: E402
import maprasters  # noqa: E402
import vegetation as veg  # noqa: E402
import vegfar  # noqa: E402


class SquaresTest(unittest.TestCase):
    def test_forest_squares_only(self):
        h = np.zeros((6, 6), np.uint8)
        h[:3, :3] = 20                      # a full square of forest
        h[3:, 3:] = 255                     # nodata
        h[:3, 3:] = np.array([[20, 0, 0], [0, 0, 0], [0, 0, 0]])     # 1 of 9: not forest
        sq = vegfar.squares(h, 3)
        self.assertEqual(sq.shape[0], 1)
        self.assertEqual((sq[0, 0], sq[0, 1]), (0, 0))
        self.assertAlmostEqual(sq[0, 2], 1.0)
        self.assertAlmostEqual(sq[0, 3], 20.0)


class KindsTest(unittest.TestCase):
    def test_by_place(self):
        lat = np.array([-25.0, -2.9, 62.0])
        lon = np.array([150.0, -60.0, 26.0])
        k = vegfar.kinds(lat, lon, np.array([0.999, 0.999, 0.999]))         # (the broadleaf draw everywhere)
        self.assertEqual(k[0], "eucalyptus_clump")
        self.assertEqual(k[1], "deciduous_clump")
        self.assertEqual(k[2], "birch_clump")
        self.assertEqual(vegfar.kinds(lat[2:], lon[2:], np.array([0.0]))[0], "conifer_clump")
        for g in ("conifer_clump", "deciduous_clump", "birch_clump", "mediterranean_clump", "eucalyptus_clump"):
            self.assertIn(g, veg.GROUPS)


class FakeRasters:
    """canopy_coarse: one Raster of 19.1 m pixels (the CHM's overview) all `h` m over the box asked for."""

    def __init__(self, h):
        self.h = h

    def canopy_coarse(self, mx0, my0, mx1, my1):
        px = 2 * maprasters.MERC_HALF / (1 << 21)
        x0 = np.floor(mx0 / px) * px
        y0 = np.ceil(my1 / px) * px
        w, hh = int(np.ceil((mx1 - x0) / px)), int(np.ceil((y0 - my0) / px))
        return maprasters.Raster(np.full((hh, w), self.h, np.uint8), "EPSG:3857", x0, y0, px, px, 255, [])


class RowsTest(unittest.TestCase):
    def test_a_forested_tile(self):
        lat, lon = 61.0, 25.0
        gx, gy = geotiles.tile_of(lat, lon, vegfar.Z)
        rows, pivot = vegfar.far_rows(gx, gy, FakeRasters(18), lambda la, lo: np.full(np.shape(la), 120.0))
        _, _, s, w, n, e = geotiles.info(gx, gy, vegfar.Z)
        km2 = (n - s) * 111.32 * (e - w) * 111.32 * np.cos(np.radians(lat))
        per_km2 = len(rows) / km2
        expect = 1e6 / vegfar.SPACING_M ** 2
        self.assertGreater(per_km2, 0.6 * expect)
        self.assertLess(per_km2, 1.6 * expect)
        names = {veg.GROUPS[r[0]] for r in rows}
        self.assertTrue(names <= {"conifer_clump", "birch_clump", "deciduous_clump"}, names)
        self.assertTrue(all(0 <= r[7] < vegfar.RANK_MAX for r in rows))
        self.assertTrue(all(abs(r[6] - 18.0) < 1e-6 and r[5] == 1.0 for r in rows))
        half = 0.6 * max((n - s) * 111320.0, (e - w) * 111320.0 * np.cos(np.radians(lat)))
        self.assertTrue(all(abs(r[1]) < half and abs(r[2]) < half for r in rows))     # (in the tile's own frame)
        again, _ = vegfar.far_rows(gx, gy, FakeRasters(18), lambda la, lo: np.full(np.shape(la), 120.0))
        self.assertEqual(rows, again)                                                 # (deterministic)

    def test_no_forest_no_rows(self):
        gx, gy = geotiles.tile_of(34.85, -116.9, vegfar.Z)
        self.assertEqual(vegfar.far_rows(gx, gy, FakeRasters(0), lambda la, lo: np.zeros(np.shape(la)))[0], [])

        class NoFile:
            def canopy_coarse(self, *a):
                return None
        self.assertEqual(vegfar.far_rows(gx, gy, NoFile(), lambda la, lo: np.zeros(np.shape(la))), ([], 0.0))

    def test_reach(self):
        self.assertGreater(vegfar.send_m(4800.0), 4800.0)       # past the skyline
        self.assertEqual(vegfar.ring_inner_m(1.0), veg.SEND_M)  # from the tree tiles' reach


if __name__ == "__main__":
    unittest.main()
