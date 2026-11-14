"""grade_points by fine bins (earthfast grade_points_binned, grading.GRADE_BINNED_ON) against the bucket walk it
replaces (PlanPack.near + earthfast grade_points): the same heights to the last bit, on random plan sets with every
feature the pairs read (cuttings, banks, abutments, pass roads, links, ranks, widened cores) and on dense, scattered,
sparse and odd batches."""
import os
import sys
import unittest
import unittest.mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402


def random_plans(rng, n, box, step=grading.VERTEX_STEP):
    """n random plans over box (x0, y0, x1, y1): wandering lines a few vertices to a few hundred m long."""
    x0, y0, x1, y1 = box
    plans = []
    for k in range(n):
        m = int(rng.integers(2, 60))
        heading = rng.uniform(0, 2 * np.pi)
        turn = rng.normal(0, 0.08, m - 1).cumsum() + heading
        seg = step * rng.uniform(0.3, 1.5, m - 1)
        start = np.array([rng.uniform(x0, x1), rng.uniform(y0, y1)])
        xy = np.vstack([start, start + np.cumsum(np.c_[np.cos(turn) * seg, np.sin(turn) * seg], axis=0)])
        if rng.random() < 0.1:
            xy[1] = xy[0] + 1e-3            # (a near-zero segment)
        z = 100.0 + rng.normal(0, 3) + np.cumsum(rng.normal(0, 0.3, m))
        half = float(rng.uniform(2.0, 9.0))
        road_half = half * float(rng.uniform(0.5, 1.0))
        cut = None
        if rng.random() < 0.3:
            cut = np.full((m - 1, 6), np.nan)
            on = rng.random(m - 1) < 0.6
            for side in (0, 3):
                wall = half * rng.uniform(0.6, 1.1, m - 1)
                top = z[:-1] + rng.uniform(-4, 8, m - 1)
                reach = wall + rng.uniform(0.5, 25, m - 1)       # (past T_MAX now and then: the far reach)
                cut[on, side] = wall[on]
                cut[on, side + 1] = np.where(rng.random(m - 1) < 0.2, np.nan, top)[on]
                cut[on, side + 2] = reach[on]
        abut = (bool(rng.random() < 0.15), bool(rng.random() < 0.15))
        p = grading.Plan(xy, z, half, abut, cut, road_half)
        p.rid = int(rng.integers(0, max(2, n // 2))) if rng.random() < 0.9 else -1
        p.rank = int(rng.choice([-1, 5, 20, 103, 105, 108, 112, 114, 116]))
        if rng.random() < 0.3:
            p.bank = rng.uniform(-0.08, 0.08, m)
        p.steep = bool(rng.random() < 0.15)
        if rng.random() < 0.15:
            grading.set_widen(p, np.clip(rng.normal(0.5, 1.0, m), 0.0, 3.0))
        plans.append(p)
    # some plans continuing others end to end (links, welded rids)
    for k in range(0, n - 1, 5):
        a, b = plans[k], plans[k + 1]
        xy = np.array(b.xy, np.float64)
        xy += a.xy[-1] - xy[0]
        q = grading.Plan(xy, np.array(b.z) - b.z[0] + a.z[-1], b.half, b.abut, b.cut, b.road_half)
        q.rid, q.rank, q.bank, q.steep = b.rid, a.rank, b.bank, b.steep
        plans[k + 1] = q
    return plans


def both(px, py, base, plans, lift=0.0):
    """grade_points by fine bins and by the bucket walk."""
    with unittest.mock.patch.object(grading, "GRADE_BINNED_ON", True):
        a = grading.grade_points(px, py, base, plans, lift)
    with unittest.mock.patch.object(grading, "GRADE_BINNED_ON", False):
        b = grading.grade_points(px, py, base, plans, lift)
    return a, b


class GradeBinnedTest(unittest.TestCase):
    def assertSame(self, a, b):
        self.assertEqual(a.shape, b.shape)
        np.testing.assert_array_equal(a, b)
        np.testing.assert_array_equal(np.signbit(a), np.signbit(b))

    def test_dense_grids(self):
        for seed in range(6):
            rng = np.random.default_rng(seed)
            ox, oy = rng.uniform(-30000, 30000, 2)
            box = (ox, oy, ox + 300.0, oy + 300.0)
            plans = random_plans(rng, 40, (ox - 40, oy - 40, ox + 340, oy + 340))
            xs = ox + np.arange(0.0, 300.0, 1.0)
            ys = oy + np.arange(0.0, 300.0, 1.0)
            px, py = np.meshgrid(xs, ys)
            px, py = px.ravel(), py.ravel()
            base = 100.0 + 0.02 * (px - ox) + rng.normal(0, 0.5, len(px))
            for lift in (0.0, grading.ROAD_LIFT):
                a, b = both(px, py, base, plans, lift)
                self.assertSame(a, b)
            self.assertGreater(int((a != base).sum()), 1000, "the plans graded the ground")
            del box

    def test_scattered_and_odd_points(self):
        rng = np.random.default_rng(42)
        plans = random_plans(rng, 50, (0, 0, 400, 400))
        n = 20000
        px = rng.uniform(-50, 450, n)
        py = rng.uniform(-50, 450, n)
        base = 100.0 + rng.normal(0, 4, n)
        px[:5] = np.nan                     # (no coordinate: the base back)
        py[5:10] = np.nan
        base[10:20] = np.nan                # (no ground under it)
        base[20:25] = -0.0
        a, b = both(px, py, base, plans)
        self.assertSame(a, b)

    def test_sparse_batches(self):
        # far apart (the fine bins grow), single points, a line of points, points past every plan
        rng = np.random.default_rng(7)
        plans = random_plans(rng, 60, (0, 0, 3000, 3000))
        for px, py in (
                (rng.uniform(0, 3000, 50), rng.uniform(0, 3000, 50)),
                (np.array([1500.0]), np.array([1500.0])),
                (np.linspace(0, 3000, 4000), np.linspace(100, 2900, 4000)),
                (np.array([-9000.0, -9100.0]), np.array([0.0, 5.0])),
                (np.full(30, 812.25), np.full(30, 1100.5))):
            base = 100.0 + rng.normal(0, 2, len(px))
            a, b = both(px, py, base, plans)
            self.assertSame(a, b)

    def test_nothing_near(self):
        rng = np.random.default_rng(3)
        plans = random_plans(rng, 10, (0, 0, 100, 100))
        px = np.array([5000.0, 5001.0])
        py = np.array([5000.0, 5000.0])
        base = np.array([1.0, 2.0])
        a, b = both(px, py, base, plans)
        self.assertSame(a, b)
        self.assertSame(a, base)
        a, b = both(np.zeros(0), np.zeros(0), np.zeros(0), plans)
        self.assertEqual(len(a), 0)

    def test_bin_sizes(self):
        rng = np.random.default_rng(11)
        plans = random_plans(rng, 30, (0, 0, 200, 200))
        px, py = np.meshgrid(np.arange(0.0, 200.0, 0.7), np.arange(0.0, 200.0, 0.9))
        px, py = px.ravel(), py.ravel()
        base = 98.0 + rng.normal(0, 1, len(px))
        with unittest.mock.patch.object(grading, "GRADE_BINNED_ON", False):
            ref = grading.grade_points(px, py, base, plans)
        for bm in (0.5, 2.0, 4.0, 8.0, 33.0, 300.0):
            with unittest.mock.patch.object(grading, "GRADE_BIN_M", bm):
                got = grading.grade_points(px, py, base, plans)
            self.assertSame(got, ref)


if __name__ == "__main__":
    unittest.main()
