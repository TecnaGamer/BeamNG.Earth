"""treegenus.py: Europe's tree genera (ForestPaths' European Tree Genus Map 2020) and their part in the leaf type."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import leaftype as lt  # noqa: E402
import treegenus as tg  # noqa: E402


class TreeGenusTest(unittest.TestCase):
    def test_places(self):
        s = tg.shares(np.array([53.665, 49.15, 62.0, 47.9]), np.array([17.895, 22.48, 26.0, 8.2]))
        self.assertGreater(s[0, tg.PINUS], 0.7, "Bory Tucholskie's pines")
        self.assertGreater(s[1, tg.FAGUS], 0.4, "the Bieszczady's beeches")
        self.assertGreater(s[2, tg.PINUS] + s[2, tg.PICEA], 0.6, "Finland")
        self.assertGreater(s[3, tg.PICEA], 0.3, "the Black Forest's spruces")
        np.testing.assert_allclose(s.sum(axis=1), 1.0, atol=1e-9)

    def test_outside_europe(self):
        s = tg.shares(np.array([40.0, -3.0]), np.array([-100.0, -60.0]))
        self.assertTrue(np.isnan(s).all())

    def test_mixed_into_the_leaf_type(self):
        """(39) half the genus map's, half the two leaf type maps' mean; England's woods no longer three-fifths
        conifers (the inventory: a third)."""
        la, lo = np.array([50.87, 51.8]), np.array([-1.6, -2.55])
        m, g = lt._bilinear(la, lo, 0), tg.conifer_share(la, lo)
        np.testing.assert_allclose(lt.conifer_share(la, lo), 0.5 * m + 0.5 * g, atol=1e-9)
        self.assertLess(float(np.mean(lt.conifer_share(la, lo))), float(np.mean(m)))
