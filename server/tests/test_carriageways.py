"""carriageways.py: a carriageway's profile, the shortest road between the points that matter within its limits."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import carriageways as cw  # noqa: E402
import test_roads  # noqa: E402
import test_grading  # noqa: E402


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


class SoftLawTest(unittest.TestCase):
    """The kernel's soft laws (earthfast 643): a comfortable grade and curve paid for past them, and soft clearances: a
    problem whose laws contradict is answered once, with the least excess, instead of an infeasible certificate."""

    def setUp(self):
        self.t = np.r_[np.zeros(30), np.full(30, 6.0)]          # (a 6 m step in the ground)
        self.w = np.ones(60)

    def test_a_soft_grade_spreads_a_climb_the_hard_one_cannot_make(self):
        hard, info = {}, {}
        lo = np.full(60, -np.inf)
        lo[-1] = 6.0
        hi = np.full(60, np.inf)
        hi[0] = 0.0
        cw.solve([dict(t=self.t, w=self.w, lo=lo, hi=hi, grade=0.01, K=30)], diagnostics=hard)
        self.assertFalse(hard["converged"])
        z = cw.solve([dict(t=self.t, w=self.w, lo=lo, hi=hi, grade=0.5, K=30, soft_grade=0.01, w_grade=1e4)],
                     diagnostics=info)[0]
        self.assertTrue(info["converged"] and info["feasible"], info)
        self.assertGreater(info["soft_grade_excess"], 0.0)
        self.assertGreater(info["slack_vars"], 0)
        self.assertAlmostEqual(float(z[-1]), 6.0, delta=1e-3)

    def test_a_soft_clearance_gives_way_to_a_join(self):
        a = dict(t=np.zeros(20), w=np.ones(20), grade=0.05, K=30)
        b = dict(t=np.zeros(20), w=np.ones(20), grade=0.05, K=30)
        cps = [(0, 0, 1, 0, "eq", 0.0), (0, 1, 1, 1, "ge", 7.0)]
        info = {}
        za, zb = cw.solve([a, b], cps, diagnostics=info, soft=[0.0, 1e5])
        self.assertTrue(info["converged"], info)
        self.assertAlmostEqual(float(za[0] - zb[0]), 0.0, delta=1e-6)
        self.assertGreater(info["soft_clearance_shortfall_m"], 6.0)

    def test_a_soft_floor_gives_way_to_a_join(self):
        # (a deck's middle asked 5 m over its ground, its ends joined to a road held on its ground (hard), its grade 10 %:
        # 0.4 m within reach over 4 m. Hard: no answer; soft: as high as the grade lets it)
        road = dict(t=np.zeros(1), w=np.ones(1), lo=np.zeros(1), hi=np.zeros(1), grade=0.1, K=0.01)
        lo = np.array([-np.inf, 5.0, -np.inf])
        deck = dict(t=np.zeros(3), w=np.ones(3), lo=lo, grade=0.1, K=0.01, step=4.0)
        cps = [(1, 0, 0, 0, "eq", 0.0), (1, 2, 0, 0, "eq", 0.0)]
        hard = {}
        cw.solve([road, deck], cps, diagnostics=hard)
        self.assertFalse(hard["converged"])
        info = {}
        _r, z = cw.solve([road, dict(deck, w_bound=1e4)], cps, diagnostics=info)
        self.assertTrue(info["converged"] and info["feasible"], info)
        self.assertAlmostEqual(float(z[1]), 0.4, delta=1e-3)
        self.assertAlmostEqual(info["soft_bound_shortfall_m"], 4.6, delta=1e-3)

    def test_no_soft_law_is_the_hard_answer(self):
        a = dict(t=self.t, w=self.w, grade=0.2, K=30)
        self.assertTrue(np.array_equal(cw.solve([a])[0], cw.solve([dict(a, w_grade=5.0)])[0]))


if __name__ == "__main__":
    unittest.main()


class BlockCacheTest(unittest.TestCase):
    def test_a_3d_block_reads_back_as_written(self):
        import tempfile
        import plancache
        from grade3d.region import Block
        a = np.array([[0.0, 0.0], [4.0, 0.0]])
        b = np.array([[4.0, 0.0], [8.0, 0.0]])
        blk = Block((np.array([0.0, 0.0]), np.array([1500.0, 1500.0])),
                    {("w1", "set"): (a, b, np.array([1.0, 1.5]), np.array([1.5, 2.0])),
                     ("w2", "deck"): (a[:1], b[:1], np.array([7.0]), np.array([7.5]))},
                    3.5, {"status": "Solved", "rows": 12}, [("w1", np.array([1.0, 2.0]), np.array([3.0, 4.0]))])
        with tempfile.TemporaryDirectory() as d:
            pc = plancache.PlanCache(d, "t")
            self.assertTrue(pc.block_store("3_4", -1, 2, blk))
            back = pc.block_load("3_4", -1, 2)
            self.assertIsNone(pc.block_load("3_4", 0, 2))
        self.assertEqual(set(back.prof), set(blk.prof))
        for k in blk.prof:
            for x, y in zip(back.prof[k], blk.prof[k]):
                np.testing.assert_array_equal(x, y)
        np.testing.assert_array_equal(back.core[1], blk.core[1])
        self.assertEqual(back.diagnostics, blk.diagnostics)
        self.assertEqual(back.seconds, 3.5)
        (f, p, q), = back.decks
        self.assertEqual(f, "w1")
        np.testing.assert_array_equal(np.r_[p, q], [1.0, 2.0, 3.0, 4.0])


