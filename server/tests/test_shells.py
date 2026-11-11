"""shells.close: every written mesh a closed solid (the user, 2026-11-07)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import shells  # noqa: E402


def box_without(skip):
    """A unit box's 8 corners and its faces but those in skip (outward facing)."""
    P = np.array([[x, y, z] for z in (0, 1) for y in (0, 1) for x in (0, 1)], float)
    faces = {"bottom": [[0, 2, 3], [0, 3, 1]], "top": [[4, 5, 7], [4, 7, 6]], "s": [[0, 1, 5], [0, 5, 4]],
             "n": [[2, 6, 7], [2, 7, 3]], "w": [[0, 4, 6], [0, 6, 2]], "e": [[1, 3, 7], [1, 7, 5]]}
    return P, np.array([t for k, v in faces.items() if k not in skip for t in v])


def outward_volume(P, T):
    a, b, c = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


class Close(unittest.TestCase):
    def test_closed_box_unchanged(self):
        P, T = box_without(())
        self.assertEqual(shells.loops(P, T), [])
        P2, T2, _, _ = shells.close(P, T)
        self.assertEqual(len(T2), len(T))

    def test_open_bottom_gets_a_sunk_bottom(self):
        P, T = box_without(("bottom",))
        P2, T2, _, (uv,) = shells.close(P, T, per_vertex=(np.zeros((len(P), 2)),))
        self.assertEqual(shells.open_loops(P2, T2), [])
        self.assertAlmostEqual(P2[:, 2].min(), -shells.SINK_M)
        self.assertGreater(outward_volume(P2, T2), 1.0)      # outward facing, the sunk 0.1 m added
        self.assertEqual(len(uv), len(P2))

    def test_open_end_capped_in_its_plane(self):
        P, T = box_without(("e",))
        P2, T2, _, _ = shells.close(P, T)
        self.assertEqual(shells.open_loops(P2, T2), [])
        self.assertAlmostEqual(outward_volume(P2, T2), 1.0)

    def test_ribbon_becomes_a_slab(self):
        x = np.linspace(0, 20, 11)
        P = np.r_[np.c_[x, np.zeros(11), 0.02 * x], np.c_[x, np.full(11, 4.0), 0.02 * x]]
        T = np.array([t for i in range(10) for t in ([i, i + 11, i + 12], [i, i + 12, i + 1])])
        TM = np.zeros(len(T), int)
        P2, T2, TM2, _ = shells.close(P, T, TM)
        self.assertEqual(shells.open_loops(P2, T2), [])
        self.assertEqual(len(TM2), len(T2))
        self.assertGreater(outward_volume(P2, T2), 0)

    def test_long_ribbon_bottom_is_coarse(self):
        x = np.linspace(0, 100, 51)
        P = np.r_[np.c_[x, np.zeros(51), np.zeros(51)], np.c_[x, np.full(51, 4.0), np.zeros(51)]]
        T = np.array([t for i in range(50) for t in ([i, i + 51, i + 52], [i, i + 52, i + 1])])
        P2, T2, _, _ = shells.close(P, T)
        self.assertEqual(shells.open_loops(P2, T2), [])
        self.assertLess(len(T2) - len(T), 110)      # a skirt per edge (104) and a bottom of a few triangles

    def test_overlay_left_single_faced(self):
        P, T = box_without(())
        Pm = np.array([[0.2, 0.2, 1.01], [0.8, 0.2, 1.01], [0.8, 0.8, 1.01]])     # a marking on the top
        P2 = np.r_[P, Pm]
        T2 = np.r_[T, [[8, 9, 10]]]
        mask = np.r_[np.zeros(len(T), bool), True]
        P3, T3, _, _ = shells.close(P2, T2, open_tris=mask)
        self.assertEqual(len(T3), len(T2))

    def test_tunnel_mouths_left_open(self):
        P, T = box_without(("e", "w"))
        P2, T2, _, _ = shells.close(P, T, mouths=False)
        self.assertEqual(len(shells.open_loops(P2, T2)), 2)


if __name__ == "__main__":
    unittest.main()
