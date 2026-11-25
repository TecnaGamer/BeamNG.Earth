"""tunnelrec.py (tunnel plan T1): the record's sections as TunnelProfiles stages them, and its hole rule on a block."""
import os
import sys
import unittest
from collections import namedtuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numpy as np

import tunnelrec

Spec = namedtuple("Spec", "width cls")


class TunnelRecordTest(unittest.TestCase):
    def test_runs_share_their_end_samples(self):
        self.assertEqual(tunnelrec._runs([1, 1, 2, 2, 2, 0, 0, 2]), [(0, 1, 1), (1, 4, 2), (4, 6, 0), (6, 7, 2)])
        self.assertEqual(tunnelrec._runs([2, 2, 2]), [(0, 2, 2)])

    def test_holes_only_at_a_portal_and_never_under_deep_ground(self):
        # a 100 m tube along +x from its portal at x = 0 into a hill rising 1 m per metre; a 1 m lattice from -20 m
        n = 141
        xs = -20.0 + np.arange(n)
        u = np.tile(np.maximum(xs, 0.0), (n, 1))          # u[iy, ix]
        mask = np.zeros((n, n), bool)
        part = ("t.0#0", Spec(7.0, "primary"), np.array([[0.0, 0.0], [100.0, 0.0]]), np.array([0.0, 0.0]), 0.0)
        rest = tunnelrec.holes(mask, u, -20.0, -20.0, 1.0, [part], {}, {"t.0#0": (30.0, 0.0)})
        self.assertEqual(rest, [])
        iy, ix = np.nonzero(mask)
        self.assertTrue(len(ix) > 0)
        x, y = xs[ix], -20.0 + iy
        self.assertTrue((x >= -1.0).all() and (x <= 8.0).all(), sorted(set(x)))
        self.assertTrue((np.abs(y + 0.5) <= 6.0).all(), sorted(set(y)))
        # a part the record has no holes for: left to the legacy rule
        other = ("u.0#0",) + part[1:]
        self.assertEqual(tunnelrec.holes(np.zeros((n, n), bool), u, -20.0, -20.0, 1.0, [other], {}, {}), [other])


if __name__ == "__main__":
    unittest.main()
