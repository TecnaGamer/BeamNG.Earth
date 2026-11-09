"""Rijksdriehoek: the Amersfoort origin, a round trip, and a tile of points at once."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import rd  # noqa: E402


class ConvertTest(unittest.TestCase):
    def test_amersfoort_is_the_origin(self):
        x, y = rd.wgs_to_rd(52.15517440, 5.38720621)
        self.assertAlmostEqual(x, 155000.0, delta=0.01)
        self.assertAlmostEqual(y, 463000.0, delta=0.01)
        lat, lon = rd.rd_to_wgs(155000.0, 463000.0)
        self.assertAlmostEqual(lat, 52.15517440, delta=1e-7)
        self.assertAlmostEqual(lon, 5.38720621, delta=1e-7)

    def test_a_dutch_point_comes_back_within_a_centimetre(self):
        for lat, lon in ((52.354, 4.896), (52.378, 4.900), (51.922, 4.479), (53.200, 5.800)):
            x, y = rd.wgs_to_rd(lat, lon)
            back_lat, back_lon = rd.rd_to_wgs(x, y)
            # a centimetre, in degrees, at this latitude
            self.assertAlmostEqual(back_lat, lat, delta=1e-7)
            self.assertAlmostEqual(back_lon, lon, delta=1e-7)
            east, north = rd.wgs_to_rd(back_lat, back_lon)
            self.assertAlmostEqual(east, x, delta=0.01)
            self.assertAlmostEqual(north, y, delta=0.01)

    def test_a_tile_of_points_matches_the_points(self):
        lat = np.array([52.354, 52.378, 51.922])
        lon = np.array([4.896, 4.900, 4.479])
        x, y = rd.wgs_to_rd(lat, lon)
        self.assertEqual(x.shape, (3,))
        for i in range(3):
            one = rd.wgs_to_rd(float(lat[i]), float(lon[i]))
            self.assertAlmostEqual(float(x[i]), one[0], delta=1e-6)
            self.assertAlmostEqual(float(y[i]), one[1], delta=1e-6)
        back_lat, back_lon = rd.rd_to_wgs(x, y)
        np.testing.assert_allclose(back_lat, lat, atol=1e-7)
        np.testing.assert_allclose(back_lon, lon, atol=1e-7)

    def test_bounds_hold_the_centre(self):
        x0, y0, x1, y1 = rd.bounds(52.35, 4.89, 52.36, 4.90)
        x, y = rd.wgs_to_rd(52.354, 4.896)
        self.assertLess(x0, x)
        self.assertLess(x, x1)
        self.assertLess(y0, y)
        self.assertLess(y, y1)
