import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy as g  # noqa: E402


class GeodesyTest(unittest.TestCase):
    def test_wgs84_segment_lengths_for_linear_references(self):
        ring = np.array([[0., 0.], [1., 0.], [1., 1.]])
        lengths = g.segment_lengths(ring)
        self.assertAlmostEqual(lengths[0], g.A * np.pi / 180., delta=1e-5)
        self.assertAlmostEqual(lengths[1], 110574.3885578, delta=1e-5)
        np.testing.assert_allclose(g.segment_lengths(ring[::-1]), lengths[::-1], atol=1e-5)
        np.testing.assert_allclose(g.segment_lengths([[179., 0.], [-179., 0.]]),
                                   [2 * g.A * np.pi / 180.], atol=1e-5)
        np.testing.assert_array_equal(g.segment_lengths([[10., 48.], [10., 48.]]), [0.])
        self.assertEqual(len(g.segment_lengths([[0., 0.]])), 0)

    def test_known_ecef(self):
        cases = [
            ((0, 0, 0), (g.A, 0, 0)),
            ((0, 90, 0), (0, g.A, 0)),
            ((0, 180, 100), (-(g.A + 100), 0, 0)),
            ((90, 0, 0), (0, 0, g.B)),
            ((-90, 0, 10), (0, 0, -(g.B + 10))),
            ((45, 0, 0), (4517590.878849, 0, 4487348.408866)),   # textbook WGS84 value
        ]
        for (lat, lon, h), want in cases:
            got = g.geodetic_to_ecef(lat, lon, h)
            np.testing.assert_allclose(got, want, atol=1e-5, err_msg=str((lat, lon, h)))

    def test_round_trip(self):
        rng = np.random.default_rng(1)
        lat = rng.uniform(-90, 90, 20000)
        lon = rng.uniform(-180, 180, 20000)
        h = rng.uniform(-500, 9000, 20000)
        lat = np.concatenate([lat, [90, -90, 0, 89.9999999, 36.5785]])
        lon = np.concatenate([lon, [0, 0, -180, 12, -118.2923]])
        h = np.concatenate([h, [0, 5, 0, 1, 4400]])
        la, lo, hh = g.ecef_to_geodetic(*g.geodetic_to_ecef(lat, lon, h))
        np.testing.assert_allclose(la, lat, atol=1e-9)
        dlon = (lo - lon + 180) % 360 - 180
        np.testing.assert_allclose(dlon[np.abs(lat) < 89.99], 0, atol=1e-9)
        np.testing.assert_allclose(hh, h, atol=1e-6)

    def test_enu_round_trip_and_axes(self):
        f = g.EnuFrame(36.5785, -118.2923)
        e, n, u = np.array([0, 1000, -3000.5]), np.array([0, -2000, 40000]), np.array([0, 50, -7])
        back = f.ecef_to_enu(*f.enu_to_ecef(e, n, u))
        np.testing.assert_allclose(back, (e, n, u), atol=1e-8)
        np.testing.assert_allclose(f.geodetic_to_enu(36.5785, -118.2923, 0), (0, 0, 0), atol=1e-8)
        # A point slightly north has +n, slightly east has +e, higher has +u.
        self.assertGreater(f.geodetic_to_enu(36.6, -118.2923)[1], 2000)
        self.assertGreater(f.geodetic_to_enu(36.5785, -118.27)[0], 1500)
        np.testing.assert_allclose(f.geodetic_to_enu(36.5785, -118.2923, 10)[2], 10, atol=1e-8)

    def test_curvature_drop(self):
        # A point on the ellipsoid 10 km east of the anchor sits ~d^2/2R = 7.85 m lower.
        for lat0 in (0.0, 36.5785, 60.0):
            f = g.EnuFrame(lat0, -118.2923)
            la, lo, _ = f.enu_to_geodetic(10000.0, 0.0, 0.0)
            e, n, u = f.geodetic_to_enu(la, lo, 0.0)
            self.assertAlmostEqual(e, 10000.0, delta=0.05)
            # East-west radius of curvature N(lat0): 6378 km at the equator, 6394 km at 60 deg.
            nr = g.A / np.sqrt(1 - g.E2 * np.sin(np.radians(lat0)) ** 2)
            self.assertAlmostEqual(u, -1e8 / (2 * nr), delta=0.002, msg=f"lat0={lat0}")
            self.assertAlmostEqual(u, -7.85, delta=0.05)


if __name__ == "__main__":
    unittest.main()
