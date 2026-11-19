"""The vanilla-style cut and fill slopes (grading.BATTER_ON, earthfast batter.rs; road revamp v2 P1a): the profile
against the stock maps' numbers, a pure function of position (any batch, order, thread count), continuous where two
roads' corridors meet, today's heights on the sides that keep them, and off byte for byte today's."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fast  # noqa: E402
import grading  # noqa: E402
import plancache  # noqa: E402
from test_grade_binned import random_plans  # noqa: E402

fast.require(497, "batter")
EF = fast._ef


def road(y0=0.0, z=0.0, half=4.5, road_half=3.5, rank=110, x=(-200.0, 200.0), rid=1):
    xs = np.arange(x[0], x[1] + 0.01, grading.VERTEX_STEP)
    p = grading.Plan(np.column_stack((xs, np.full_like(xs, y0))), np.full_like(xs, z), half, road_half=road_half)
    p.rank, p.rid = rank, rid
    return p


def packed(plans, ground, rays=None):
    pack = grading.PlanPack(plans)
    pack.bt = grading.batter_params(pack, ground, rays)
    return pack


class Batter(unittest.TestCase):
    def setUp(self):
        self.was = grading.BATTER_ON
        grading.BATTER_ON = True

    def tearDown(self):
        grading.BATTER_ON = self.was

    def profile(self, pack, ground, ys, x=0.0):
        px = np.full_like(ys, x)
        return grading.grade_points(px, ys, ground(px, ys), pack)

    def test_side_slope_profile(self):
        # a road across a 0.3 side slope: a cut on +y, a fill on -y; the stock maps' strip, crest and 1:2 batter
        def ground(x, y):
            return 0.3 * np.asarray(y)
        pack = packed([road()], ground)
        m, r, fl = pack.bt
        self.assertTrue((fl == 0).all())
        np.testing.assert_allclose(m, 1 / grading.BATTER_RURAL)
        ys = np.arange(0.0, 45.0, 0.25)
        for sgn in (1.0, -1.0):
            g = self.profile(pack, ground, sgn * ys)
            off = ys - 3.5                                      # (past the lane edge)
            strip = np.abs(g[(off >= 0) & (off <= 2.0)]).max()
            self.assertLessEqual(strip, 0.3)
            slope = np.abs(np.diff(g)) / 0.25
            band = max(abs(g[i + 20] - g[i]) / 5.0 for i in range(len(g) - 20))
            self.assertGreater(band, 0.42)
            self.assertLess(band, 0.55)
            a = off[1:][slope >= 0.03][0]
            b = off[1:][slope >= 0.8 * band][0]
            self.assertTrue(3.0 <= b - a <= 6.5, (a, b))
            # out to the daylight line, then the natural ground
            nat = ground(0.0, sgn * ys)
            day = off[np.nonzero(np.abs(g - nat) > 0.01)[0][-1]]
            self.assertGreater(day, 12.0)
            self.assertLess(day, 30.0)
            far = off > day + grading.BATTER_SMOOTH_K
            np.testing.assert_array_equal(g[far], nat[far])

    def test_off_is_today(self):
        rng = np.random.default_rng(3)
        plans = random_plans(rng, 60, (0, 0, 400, 400))
        px, py = np.meshgrid(np.arange(0.0, 400.0, 1.7), np.arange(0.0, 400.0, 1.7))
        px, py = px.ravel(), py.ravel()
        base = 100.0 + 0.02 * px + rng.normal(0, 0.3, len(px))
        pack = grading.PlanPack(plans)
        today = grading.grade_points(px, py, base, pack, grading.ROAD_LIFT)
        pack.bt = grading.batter_params(pack, lambda x, y: 100.0 + 0.02 * np.asarray(x), None)
        grading.BATTER_ON = False
        np.testing.assert_array_equal(grading.grade_points(px, py, base, pack, grading.ROAD_LIFT), today)
        grading.BATTER_ON = True
        on = grading.grade_points(px, py, base, pack, grading.ROAD_LIFT)
        self.assertGreater(int((on != today).sum()), 1000)
        # (no params: today's)
        pack.bt = None
        np.testing.assert_array_equal(grading.grade_points(px, py, base, pack, grading.ROAD_LIFT), today)

    def test_pure_function_of_position(self):
        # every batch, its order and any thread count: the same bits at a point
        rng = np.random.default_rng(11)
        plans = random_plans(rng, 80, (0, 0, 500, 500))

        def ground(x, y):
            return 100.0 + 0.15 * np.asarray(x) + 3.0 * np.sin(np.asarray(y) / 37.0)
        pack = packed(plans, ground)
        px, py = rng.uniform(-20, 520, 40000), rng.uniform(-20, 520, 40000)
        base = ground(px, py) + rng.normal(0, 0.2, len(px))
        full = grading.grade_points(px, py, base, pack)
        perm = rng.permutation(len(px))
        np.testing.assert_array_equal(grading.grade_points(px[perm], py[perm], base[perm], pack), full[perm])
        for part in (np.arange(0, 1000), np.arange(39000, 40000), perm[:777]):
            np.testing.assert_array_equal(grading.grade_points(px[part], py[part], base[part], pack), full[part])
        try:
            for t in (1, 3, 8):
                EF.set_native_threads(t)
                np.testing.assert_array_equal(grading.grade_points(px, py, base, pack), full)
        finally:
            EF.set_native_threads(0)
        # the params of a segment: its own, whatever other plans share the set
        sub = packed(plans[:30], ground)
        n30 = int(sub.s0[-1])
        for k in range(3):
            np.testing.assert_array_equal(sub.bt[k], pack.bt[k][:n30])

    def test_two_roads_continuous(self):
        # two parallel roads 30 m apart, 6 m apart in height, on flat ground: their corridors meet (the ruled surface
        # between them), no step anywhere across
        def ground(x, y):
            return np.zeros(np.broadcast(np.asarray(x), np.asarray(y)).shape)
        pack = packed([road(0.0, 6.0, rid=1), road(30.0, 0.0, rid=2)], ground)
        ys = np.arange(-30.0, 60.0, 0.05)
        g = self.profile(pack, ground, ys)
        self.assertLess(np.abs(np.diff(g)).max(), 0.05 * 1.2)     # (at most a 1:1 slope sampled every 5 cm)
        self.assertAlmostEqual(float(g[np.argmin(np.abs(ys - 0.0))]), 6.0, places=6)
        self.assertAlmostEqual(float(g[np.argmin(np.abs(ys - 30.0))]), 0.0, places=6)

    def test_conflict_continuous(self):
        # two high roads whose lower bounds cross (a road at 10 m and a crossing one at 11 m) while a low road's upper
        # bound stays under both: the conflict between them changes binding road smoothly (Codex review of 861e550f)
        def ground(x, y):
            return np.zeros(np.broadcast(np.asarray(x), np.asarray(y)).shape)
        a = road(0.0, 10.0, rid=1)
        b = grading.Plan(np.column_stack((np.zeros(60), np.arange(0.0, 240.0, 4.0))), np.full(60, 11.0), 4.5,
                         road_half=3.5)
        b.rank, b.rid = 110, 2
        c = road(26.0, 0.0, rid=3)
        pack = packed([a, b, c], ground)
        # (no step: the largest change between samples shrinks with their spacing, a jump would not)
        def worst(x0, y0, x1, y1, step):
            n = int(np.hypot(x1 - x0, y1 - y0) / step)
            px, py = np.linspace(x0, x1, n), np.linspace(y0, y1, n)
            return float(np.abs(np.diff(grading.grade_points(px, py, ground(px, py), pack))).max())
        for line in ((6.0, 5.0, 6.0, 22.0), (12.0, 5.0, 12.0, 22.0), (5.0, 8.0, 30.0, 8.0), (5.0, 16.0, 30.0, 16.0)):
            coarse, fine = worst(*line, 0.01), worst(*line, 0.001)
            self.assertLess(fine, 0.2 * coarse + 1e-4, line)

    def test_old_sides_keep_today(self):
        # a plan of no feature (a tunnel's mouth: rank and rid < 0) and a sharp cutting's side: today's heights within
        # today's reach; a railway's bed (rank < 0, its feature's rid): the corridor
        def ground(x, y):
            return 0.4 * np.asarray(y)
        rail = road(rank=-1, rid=-1)
        bed = road(rank=-1, rid=7)
        fl = packed([bed], ground).bt[2]
        self.assertTrue((fl == 0).all())
        cutp = road(y0=200.0, z=80.0)
        n = len(cutp.xy) - 1
        cut = np.full((n, 6), np.nan)
        cut[:, 0], cut[:, 1], cut[:, 2] = 4.0, 84.0, 9.0         # (left side: a wall at 4 m, its top 4 m up)
        cutp.cut = cut
        for p in (rail, cutp):
            pack = packed([p], ground)
            ys = p.xy[0, 1] + np.arange(-14.0, 14.0, 0.5)
            today = grading.PlanPack([p])
            px = np.zeros_like(ys)
            g0 = grading.grade_points(px, ys, ground(px, ys), today)
            g1 = grading.grade_points(px, ys, ground(px, ys), pack)
            if p is rail:
                np.testing.assert_array_equal(g1, g0)
            else:                       # (the cutting's side: today's; the other: the corridor)
                left = ys - p.xy[0, 1] > 0
                np.testing.assert_array_equal(g1[left], g0[left])
                self.assertTrue((g1[~left] != g0[~left]).any())

    def test_buildings(self):
        # a building 12 m from the core's edge on one side: the urban batter, a reach that stops short of it
        def ground(x, y):
            return 0.1 * np.asarray(y)

        def rays(org, dirs, reach):
            return np.where(dirs[:, 1] > 0, 12.0, np.inf)
        pack = packed([road()], ground, rays)
        m, r, fl = pack.bt
        bs = int(np.where(np.isfinite(rays(None, np.array([[0.0, 1.0], [0.0, -1.0]]), 0)))[0][0])
        self.assertTrue((fl == 0).all())
        # (the open side: the urban batter; the building's: at least as steep, closed before the building)
        # (m, r: (segments, 4), each side at a, then at b)
        np.testing.assert_allclose(m[:, [1 - bs, 3 - bs]], 1 / grading.BATTER_URBAN)
        self.assertTrue((m[:, [bs, 2 + bs]] >= 1 / grading.BATTER_URBAN).all())
        self.assertTrue((r[:, [bs, 2 + bs]] <= 12.0 - grading.BATTER_BLD_GAP_M).all())

    def test_building_caps_the_dilated_reach(self):
        # one segment sees a building 10 m out; its neighbours, none, and a deep fill that needs a long reach: the
        # dilation and the end averaging never carry that segment's reach past its building (Codex review of ff9d2c55)
        def ground(x, y):
            return -0.6 * np.abs(np.asarray(y))
        p = road(z=0.0)
        mid = (len(p.xy) - 1) // 2

        def rays(org, dirs, reach):
            out = np.full(len(org), np.inf)
            out[2 * mid] = 10.0             # (that segment's left side)
            return out
        m, r, fl = packed([p], ground, rays).bt
        self.assertLessEqual(float(r[mid, 0]), 10.0 - grading.BATTER_BLD_GAP_M)
        self.assertLessEqual(float(r[mid, 2]), 10.0 - grading.BATTER_BLD_GAP_M)
        self.assertGreater(float(r[mid - 3, 0]), 15.0)

    def test_mixed_cap_continuous(self):
        # a raised road over flat ground ending in the open, its left side today's (a long sharp cutting), its right
        # the corridor: round its end cap the ground has no step, past today's fade too
        def ground(x, y):
            return np.zeros(np.broadcast(np.asarray(x), np.asarray(y)).shape)
        p = road(z=8.0, x=(-200.0, 0.0))
        n = len(p.xy) - 1
        cut = np.full((n, 6), np.nan)
        cut[:, 0], cut[:, 1], cut[:, 2] = 4.0, 12.0, 9.0
        p.cut = cut
        pack = packed([p], ground)
        self.assertTrue((pack.bt[2][:, 0] != 0).all() and (pack.bt[2][:, 1] == 0).all())
        for rad in (20.0, 26.0, 30.0):          # (past T_MAX + the old fade: 22 m + the core)
            a = np.linspace(-np.pi / 2, np.pi / 2, 4001)
            px, py = rad * np.cos(a), rad * np.sin(a)
            g = grading.grade_points(px, py, ground(px, py), pack)
            self.assertLess(float(np.abs(np.diff(g)).max()), 0.05, rad)

    def test_close_open(self):
        # a sharp cutting in short runs along a plan (3 segments on, 5 off): no run, the corridor all along; a long run
        # (its gaps of one segment closed): today's
        def ground(x, y):
            return 0.1 * np.asarray(y)
        p = road()
        n = len(p.xy) - 1
        cut = np.full((n, 6), np.nan)
        on = np.arange(n) % 8 < 3
        cut[on, 0], cut[on, 1], cut[on, 2] = 4.0, 3.0, 9.0
        p.cut = cut
        fl = packed([p], ground).bt[2]
        self.assertTrue((fl[:, 0] == 0).all())
        cut[:, :3] = np.nan
        cut[20:40, 0], cut[20:40, 1], cut[20:40, 2] = 4.0, 3.0, 9.0
        cut[30, :3] = np.nan
        fl = packed([p], ground).bt[2]
        self.assertTrue((fl[20:40, 0] != 0).all())
        self.assertTrue((fl[:18, 0] == 0).all() and (fl[42:, 0] == 0).all())

    def test_plans_format_tag(self):
        grading.BATTER_ON = False
        off = plancache.plans_format()
        grading.BATTER_ON = True
        on = plancache.plans_format()
        self.assertFalse(off.endswith(".bv1"))
        self.assertEqual(on, off + ".bv1")


if __name__ == "__main__":
    unittest.main()
