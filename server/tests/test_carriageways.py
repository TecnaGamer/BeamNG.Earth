"""carriageways.py: a carriageway's profile, the shortest road between the points that matter within its limits."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import carriageways as cw  # noqa: E402


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.s = np.arange(200) * cw.STEP_M
        self.t = 20 + 3 * np.sin(self.s / 60.0)            # a coarse DEM's bumps
        self.w = np.full(len(self.s), 0.2)
        self.w[0] = self.w[-1] = 5.0

    def test_length_bias_straightens(self):
        flat = cw.solve([dict(t=self.t, w=self.w, grade=0.05, K=30, length=0.0)])[0]
        straight = cw.solve([dict(t=self.t, w=self.w, grade=0.05, K=30, length=2000.0)])[0]
        self.assertLess(np.ptp(np.diff(straight)), np.ptp(np.diff(flat)), "fewer rises and falls")
        self.assertAlmostEqual(straight[0], self.t[0], delta=0.5, msg="its ends on their ground")

    def test_limits_hold(self):
        z = cw.solve([dict(t=self.t * 2, w=self.w, grade=0.05, K=30)])[0]
        self.assertLessEqual(np.abs(np.diff(z)).max() / cw.STEP_M, 0.05 * 1.05)
        self.assertLessEqual(np.abs(np.diff(z, 2)).max(), cw.STEP_M ** 2 / 3000.0 * 1.15)

    def test_clearance_between_chains(self):
        a = dict(t=np.full(60, 20.0), w=np.ones(60), grade=0.05, K=30)
        b = dict(t=np.full(60, 20.0), w=np.ones(60), grade=0.07, K=12)
        za, zb = cw.solve([a, b], [(1, 30, 0, 30, "ge", 7.0)])
        self.assertGreaterEqual(zb[30] - za[30], 7.0 - 0.05)

    def test_chains_split_at_branch_points(self):
        P = [("a", np.array([[0, 0], [100, 0.]])), ("b", np.array([[100, 0], [160, 0.]])),
             ("c", np.array([[300, 0], [160, 0.]])), ("d", np.array([[300, 0], [400, 50.]])),
             ("e", np.array([[300, 0], [400, 0.]]))]
        self.assertEqual(cw.chains(P), [[(0, False), (1, False), (2, True)], [(3, False)], [(4, False)]])


if __name__ == "__main__":
    unittest.main()
