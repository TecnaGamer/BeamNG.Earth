"""leaftype.py: the conifers' share of the forest at a point (Copernicus Land Cover 100 m's Forest Type, 2019)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import leaftype as lt  # noqa: E402


class LeafTypeTest(unittest.TestCase):
    def test_places(self):
        s = lt.conifer_share(np.array([61.0, 49.25, 49.2, -3.1]), np.array([10.0, 19.95, 22.5, -60.0]))
        self.assertGreater(s[0], 0.9, "Norway's spruces and pines")
        self.assertGreater(s[1], 0.9, "the Tatras' spruces")
        self.assertLess(s[2], 0.3, "the Bieszczady's beeches")
        self.assertLess(s[3], 0.05, "the Amazon")

    def test_no_forest_no_say(self):
        self.assertTrue(np.isnan(lt.conifer_share(np.array([0.0]), np.array([-30.0]))[0]), "the Atlantic")

    def test_national_shares(self):
        """The map's conifers against the national forest inventories (Germany's BWI 2012: 54 %; Finland: ~90 %)."""
        g = lt._load()

        def share(s, w, n, e):
            a = g[int((90 - n) / 0.05):int((90 - s) / 0.05), int((w + 180) / 0.05):int((e + 180) / 0.05)]
            a = a[..., 0]
            return float(a[a != lt.NONE].mean()) / 250.0
        self.assertAlmostEqual(share(47.5, 6.0, 55.0, 15.0), 0.54, delta=0.08)
        self.assertGreater(share(60.0, 21.0, 70.0, 31.0), 0.8)

    def test_smooth(self):
        """Bilinear between the pixels: no step at a pixel's edge."""
        lon = np.linspace(17.0, 18.0, 401)
        s = lt.conifer_share(np.full(lon.shape, 53.6), lon)
        self.assertLess(float(np.nanmax(np.abs(np.diff(s)))), 0.2)


if __name__ == "__main__":
    unittest.main()
