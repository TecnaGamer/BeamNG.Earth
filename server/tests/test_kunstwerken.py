"""kunstwerken: a hand-built structure placed in the road tiles (its triangles per tile, its decks' planes)."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ahn  # noqa: E402
import geodesy  # noqa: E402
import kunstwerken  # noqa: E402


class RdTest(unittest.TestCase):
    def test_round_trip(self):
        la, lo = ahn.rd_to_wgs(*ahn.wgs_to_rd(51.9076, 4.4660))
        self.assertAlmostEqual(float(la), 51.9076, places=7)
        self.assertAlmostEqual(float(lo), 4.4660, places=7)


class KunstwerkTest(unittest.TestCase):
    def setUp(self):
        # a 20 x 10 m deck slab at 3 m NAP round RD (91600, 435900), its plane rising 1 % eastward
        self.k = kunstwerken.Kunstwerk({
            "name": "t", "origin_rd": [91600, 435900],
            "positions": [[-10, -5, 3], [10, -5, 3], [10, 5, 3], [-10, 5, 3]], "tris": [[0, 1, 2], [0, 2, 3]],
            "materials": ["bne_bridge_concrete"], "tri_material": [0, 0],
            "decks": [{"ring": [[-10, -5], [10, -5], [10, 5], [-10, 5]], "plane": [3.0, 0.01, 0.0]}]})
        la, lo = ahn.rd_to_wgs(91600.0, 435900.0)
        self.frame = geodesy.EnuFrame(float(la), float(lo))

    def test_tile_mesh_and_deck(self):
        m = self.k.tile_mesh(self.frame, (-50, -50, 50, 50), (0.0, 0.0, 0.0))
        self.assertIsNotNone(m)
        self.assertEqual(len(m[1]), 2)
        # (RD's grid is turned ~1.2 degrees from true north at Rotterdam: corners move ~0.1 m, the size stays)
        self.assertAlmostEqual(float(np.hypot(*(m[0][1, :2] - m[0][0, :2]))), 20.0, delta=0.01)
        np.testing.assert_allclose(m[0][:, 0].min(), -10, atol=0.3)
        self.assertIsNone(self.k.tile_mesh(self.frame, (100, 100, 200, 200), (0.0, 0.0, 0.0)))
        la, lo = ahn.rd_to_wgs(np.array([91605.0, 91700.0]), np.array([435900.0, 435900.0]))
        z = self.k.deck_top(la, lo)
        self.assertAlmostEqual(float(z[0]), 3.05, places=3)
        self.assertTrue(np.isnan(z[1]))


if __name__ == "__main__":
    unittest.main()
