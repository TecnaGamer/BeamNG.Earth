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


if __name__ == "__main__":
    unittest.main()


class RegionCacheTest(unittest.TestCase):
    def test_a_region_reads_back_as_written(self):
        import tempfile
        import plancache
        xy = np.array([[0.0, 0.0], [4.0, 0.0], [8.0, 0.0]])
        z = np.array([1.0, 1.5, 2.0])
        k1, k2 = ("g", 0.0, 0.0, 4.0, 0.0), ("b", 4.0, 0.0, 8.0, 0.0)
        got = {"parts": [(k1, xy[:2], False, "motorway", "w1"), (k2, xy[1:], True, "motorway", "w2"),
                         (("g", 9.0, 9.0, 9.5, 9.0), np.array([[9.0, 9.0], [9.5, 9.0]]), False, "trunk", None)],
               "prof": {k1: (xy, z), k2: (xy, z)}}
        with tempfile.TemporaryDirectory() as d:
            pc = plancache.PlanCache(d, "t")
            self.assertTrue(pc.region_store("f", (14, 1, 2), got))
            back = pc.region_load("f", (14, 1, 2))
        self.assertEqual([q[0] for q in back["parts"]], [q[0] for q in got["parts"]])
        self.assertEqual([q[2:5] for q in back["parts"]], [q[2:5] for q in got["parts"]])
        self.assertEqual([q[5] for q in back["parts"]], [0.0, 1.0, 0.0])
        self.assertEqual(set(back["prof"]), {k1, k2})
        np.testing.assert_array_equal(back["prof"][k2][1], z)
        np.testing.assert_array_equal(back["parts"][1][1], xy[1:])


class StackTest(unittest.TestCase):
    """OSM's layer orders an interchange's decks (the High Five, 2026-11-02): built from the ground up, each level over
    what is below it, the level-4 ramp over the level-1 one however the heights would have ordered them."""

    def _z(self, g, f, i, pts):
        import geodesy
        line = np.asarray(f.parts(i)[0][0], np.float64)
        la, lo = line[:, 1], line[:, 0]
        pl = g.store.planners_of(la[len(la) // 2:len(la) // 2 + 1], lo[len(lo) // 2:len(lo) // 2 + 1])[0][0]
        e, n, _ = pl.frame.geodetic_to_enu(la, lo, 0.0)
        xy = np.stack([np.asarray(e), np.asarray(n)], 1)
        pla, plo, _ = test_roads.FRAME.enu_to_geodetic(np.asarray(pts)[:, 0], np.asarray(pts)[:, 1], 0.0)
        pe, pn, _ = pl.frame.geodetic_to_enu(np.asarray(pla), np.asarray(plo), 0.0)
        return pl._cw_z_at(xy, np.stack([np.asarray(pe), np.asarray(pn)], 1), str(f.ids[i]))

    def test_levels_stack_from_the_ground_up(self):
        import grading
        d = 1 / np.sqrt(2)
        lines = [[(-700, 0), (700, 0)],                                      # 0 the motorway, on the ground
                 [(0, -700), (0, -70)], [(0, -70), (0, 70)], [(0, 70), (0, 700)],    # 1-3 a ramp, its bridge level 1
                 [(-700 * d, 40 - 700 * d), (-110 * d, 40 - 110 * d)],     # 4-6 a ramp, its bridge level 4
                 [(-110 * d, 40 - 110 * d), (110 * d, 40 + 110 * d)],
                 [(110 * d, 40 + 110 * d), (700 * d, 40 + 700 * d)]]
        cls = ["motorway"] * 7
        f = test_roads.features(lines, cls, flags=[(2, "is_bridge"), (5, "is_bridge")], levels=[(2, 1.0), (5, 4.0)])
        md = test_grading.FakeMD(f)
        g = grading.Grader(test_roads.FRAME, md, lambda la, lo: np.full(np.shape(la), 10.0))
        z_m = self._z(g, f, 0, [(-40.0, 0.0), (0.0, 0.0)])
        z_1 = self._z(g, f, 2, [(0.0, 0.0), (0.0, 40.0)])
        z_4 = self._z(g, f, 5, [(-40.0, 0.0), (0.0, 40.0)])
        self.assertIsNotNone(z_m)
        self.assertIsNotNone(z_1)
        self.assertIsNotNone(z_4)
        clear = grading.CW_HEADROOM_M
        self.assertGreater(z_1[0] - z_m[1], clear, "level 1 over the ground")
        self.assertGreater(z_4[1] - z_1[1], clear, "level 4 over level 1")
        self.assertGreater(z_4[0] - z_m[0], clear, "level 4 over the ground")
        self.assertLess(abs(z_m[0] - 10.0), 0.5, "the ground road left on the ground")
