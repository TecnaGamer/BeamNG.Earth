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

    def test_surface_outline_is_the_start_of_each_fill(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import roadsurf3

        plan = road(z=6.)
        outline = np.array([[-80., .1], [-40., .1], [-20., 10.], [80., 10.],
                            [80., -4.], [-20., -4.], [-40., -.1], [-80., -.1]])
        surface = {"pieces": [({"segs": ["way", "through", "ramp"]}, None)],
                   "kept": [0], "outlines": [outline]}
        planner = SimpleNamespace(frame=None, _roads_cell=None, widths=None)
        with patch.object(roadsurf3, "tile_surface", return_value=surface):
            roadsurf3.grade_plans(planner, (-200., -30., 200., 30.), [("way", plan)],
                                  [SimpleNamespace(kind="paved", cls="motorway")], "US")
        ground = lambda x, y: np.zeros_like(np.asarray(y))  # noqa: E731
        pack = packed([plan], ground)
        ys = np.array([-21., -5., -4., 0., 10., 11., 27.])
        # The clipped tip is narrower than the old shoulder. A negative
        # banking width here would panic when the terrain is evaluated.
        np.testing.assert_allclose(self.profile(pack, ground, np.array([0.]), x=-60.), 6.)
        self.assertGreater(float(np.min(pack.rh)), 0.)
        zs = self.profile(pack, ground, ys)
        np.testing.assert_allclose(zs[2:5], 6., atol=1e-6)
        np.testing.assert_allclose(zs[[1, 5]], 6. - grading.BATTER_STRIP_LO, atol=1e-6)
        self.assertLess(float(max(zs[0], zs[-1])), .1)
        self.assertEqual(plan.half + plan.widen[0], 4.5)  # the core beyond the lane event

    def test_welded_taper_between_grading_sections(self):
        xy = np.array([[0., 0.], [40., 0.], [80., 0.]])
        plan = grading.Plan(xy, np.full(3, 6.), 3.)
        plan.rank, plan.rid = 110, 1
        plan.surface_width = True
        grading.set_widen(plan, np.array([0., 4., 0.]))

        def ground(x, y):
            return np.zeros_like(y)

        pack = packed([plan], ground)
        for station in (10., 20., 30., 39.999, 40., 40.001, 50., 60., 70.):
            half = 3. + min(station, 80. - station) * .1
            for side in (-1., 1.):
                z = self.profile(pack, ground, side * np.array([half, half + 1.]), station)
                np.testing.assert_allclose(z, [6., 6. - grading.BATTER_STRIP_LO], atol=1e-5, rtol=0)

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

    def test_no_wall_behind_an_open_end(self):
        # behind a plan's end (and behind the end of a run whose next segments keep today's grading: a bridge's
        # abutment metres) the corridors swept round the segments' end caps read ground their probes never saw: a 14 m
        # trench behind the Stelvio's deck end at Premadio, a 24 m fill past a plan's end, each closing at its reach as
        # a wall (revampfix). Loosened behind such an end, the ground there rises at about the batter + CAP_K
        def rise(x0):
            def f(x, y):        # (level beside the road, rising at 0.9 past x0)
                return 0.9 * np.maximum(np.asarray(x, float) - x0, 0.0) + 0.0 * np.asarray(y)
            return f

        def fall(x0):
            return lambda x, y: -rise(x0)(x, y)
        plain = road(z=0.0, x=(-200.0, 0.0))
        abut = road(z=0.0, x=(-200.0, 0.0))
        abut.abut = (False, True)       # (its last ABUT_NEAR_M: PlanPack's ab, today's on both sides)
        xs, ys = np.meshgrid(np.arange(-40.0, 60.0, 0.5), np.arange(-55.0, 55.0, 0.5))
        x, y = xs.ravel(), ys.ravel()
        for p, nat in ((plain, rise(0.0)), (plain, fall(0.0)), (abut, rise(-grading.ABUT_NEAR_M)),
                       (abut, fall(-grading.ABUT_NEAR_M))):
            pack = packed([p], nat)
            fl = pack.bt[2]
            if p is abut:
                self.assertTrue((fl[-1] != 0).all() and (fl[0] == 0).all())
            g = grading.grade_points(x, y, nat(x, y), pack).reshape(xs.shape)
            gy, gx = np.gradient(g, 0.5)
            s = np.hypot(gx, gy)
            # (the core and its blend left out, and today's grading round the segments that keep it: the batter alone)
            dx = np.maximum(xs - 0.0, 0.0)
            far = np.hypot(dx, ys) > p.half + grading.BATTER_CORE_BLEND_M + 1.0
            if p is abut:
                x_old = float(p.xy[np.flatnonzero(fl[:, 0] == 0)[-1] + 1, 0])
                dxo = np.maximum(x_old - xs, 0.0) + dx
                far &= np.hypot(dxo, ys) > p.half + grading.T_MAX + grading.BATTER_OLD_FADE_M
            # (the natural 0.9, the batter's 1:2 and CAP_K, the reach's close near the end: under 2.5; before, a wall of
            # 8.7 behind the plain end)
            self.assertLess(float(s[far].max()), 2.5, (p is abut, float(nat(10.0, 0.0))))
        # a bend on a side slope: the caps between its segments loosen nothing on their own end lines, so the ground
        # round the joint has no step (the corridors beside the two legs meet as before)
        xs_ = np.arange(-200.0, 0.01, grading.VERTEX_STEP)
        leg = np.column_stack((xs_, np.zeros_like(xs_)))
        ang = np.radians(20.0)
        leg2 = np.column_stack((np.arange(1, 50) * grading.VERTEX_STEP * np.cos(ang),
                                np.arange(1, 50) * grading.VERTEX_STEP * np.sin(ang)))
        line = np.concatenate([leg, leg2])
        bend = grading.Plan(line, np.zeros(len(line)), 4.5, road_half=3.5)
        bend.rank, bend.rid = 110, 1

        def side(x, y):
            return 0.35 * np.asarray(y, float) + 0.0 * np.asarray(x)
        pack = packed([bend], side)
        for rad in (12.0, 25.0, 38.0):
            a = np.linspace(-np.pi, np.pi, 8001)
            px, py = rad * np.cos(a), rad * np.sin(a)
            g = grading.grade_points(px, py, side(px, py), pack)
            self.assertLess(float(np.abs(np.diff(g)).max()), 0.05, rad)

    def test_open_end_not_closed_by_an_unrelated_plan(self):
        # an end cap's continuation is PlanPack's (nb: its own plan's next segment, the one plan it goes on into), not
        # the pack's next segment because its start is this end to the bit: an unrelated road starting at the end of
        # another and doubling back took away the relief behind that end in one plan order and not in the other
        # (Codex review of revampfix). Both orders: the same heights, and the relief behind the end
        def rise(x, y):
            return 0.9 * np.maximum(np.asarray(x, float), 0.0) + 0.0 * np.asarray(y)
        a = road(z=0.0, x=(-200.0, 0.0))
        k = np.arange(0, 40) * grading.VERTEX_STEP
        ang = np.radians(160.0)
        bline = np.column_stack((k * np.cos(ang), k * np.sin(ang)))
        bline[0] = a.xy[-1]
        b = grading.Plan(bline, np.zeros(len(bline)), 3.5, road_half=2.5)
        b.rank, b.rid = 60, 2
        xs, ys = np.meshgrid(np.arange(-20.0, 50.0, 0.5), np.arange(-45.0, 45.0, 0.5))
        x, y = xs.ravel(), ys.ravel()
        got = []
        for plans in ([a, b], [b, a]):
            pack = packed(plans, rise)
            self.assertTrue((pack.nb[:, 0][pack.s0[:-1]] == -1).all() and (pack.nb[:, 1][pack.s0[1:] - 1] == -1).all())
            got.append(grading.grade_points(x, y, rise(x, y), pack))
        np.testing.assert_array_equal(got[0], got[1])
        alone = grading.grade_points(x, y, rise(x, y), packed([a], rise))
        # (behind the end, on the far side from the other road: as with the end alone)
        far = (xs.ravel() > 8.0) & (ys.ravel() < -12.0)
        np.testing.assert_allclose(got[0][far], alone[far], atol=1e-9)
        # the next segment of its own plan, and a plan carrying straight on into another: theirs
        c = road(z=0.0, x=(0.0, 100.0), rid=1)
        pack = packed([a, c], rise)
        la, fc = int(pack.s0[1]) - 1, int(pack.s0[1])
        self.assertEqual(int(pack.nb[la, 1]), 2 * fc)
        self.assertEqual(int(pack.nb[fc, 0]), 2 * la + 1)
        self.assertEqual(int(pack.nb[la - 1, 1]), 2 * la)
        self.assertEqual(int(pack.nb[la, 0]), 2 * (la - 1) + 1)
        # ... but not another road's end there (another class: not one road planned in pieces, CONT_*), nor its own at
        # another level: both ends stay open (their relief kept; Codex review of revampfix2)
        for other in (road(z=0.0, x=(0.0, 100.0), rid=7, rank=60), road(z=3.0, x=(0.0, 100.0), rid=1)):
            pack = packed([a, other], rise)
            la, fc = int(pack.s0[1]) - 1, int(pack.s0[1])
            self.assertEqual((int(pack.nb[la, 1]), int(pack.nb[fc, 0])), (-1, -1))
        # ... and an ineligible end there hides no valid continuation: a's end goes on into c whatever the order
        c = road(z=0.0, x=(0.0, 100.0), rid=1)
        up = road(z=3.0, x=(0.0, 60.0), rid=1)
        other = road(z=0.0, x=(0.0, 80.0), rid=7, rank=60)
        for plans in ([a, c, up, other], [other, up, c, a], [up, a, other, c]):
            pack = packed(plans, rise)
            ia, ic = plans.index(a), plans.index(c)
            la, fc = int(pack.s0[ia + 1]) - 1, int(pack.s0[ic])
            self.assertEqual((int(pack.nb[la, 1]), int(pack.nb[fc, 0])), (2 * fc, 2 * la + 1), [p.rid for p in plans])
        # (the heights there are not compared: roads stacked on one footprint at two levels are graded in plan order
        # by today's grading, whatever the end caps)

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
        self.assertFalse(off.endswith(".bv3"))
        self.assertEqual(on, off + ".bv3")

    def test_bins_and_today_where_it_weighs(self):
        # batter.rs meets each bin's segments nearest first (stopping at today's weight 1) and grade_points makes
        # today's grading only where it weighs: the bins' size (which segments a point meets, in which order) changes
        # no height, over a ground with cuts, fills, roads over roads (conflicts), banks and sides that keep today's
        # grading, and the heights are batter_apply's over today's everywhere
        rng = np.random.default_rng(23)
        plans = random_plans(rng, 120, (0, 0, 400, 400))
        for i, p in enumerate(plans):
            p.z = np.asarray(p.z, np.float64) + (12.0 if i % 7 == 0 else 0.0)        # (some raised: fills, conflicts)
            if i % 5 == 0:
                p.bank = np.full(len(p.xy), 0.04)

        def ground(x, y):
            x, y = np.asarray(x), np.asarray(y)
            return 100.0 + 0.2 * x + 6.0 * np.sin(y / 23.0) + 2.0 * np.cos(x / 11.0)
        pack = packed(plans, ground)
        self.assertTrue((pack.bt[2] != 0).any() and (pack.bt[2] == 0).any())
        px, py = rng.uniform(-30, 430, 60000), rng.uniform(-30, 430, 60000)
        base = ground(px, py) + rng.normal(0, 0.5, len(px))
        base[::997] = np.nan                    # (a bin with a ground not read: nothing left out there)
        was = grading.BATTER_BIN_M
        try:
            got = []
            for bm in (8.0, 0.5, 3.0, 40.0):
                grading.BATTER_BIN_M = bm
                got.append(grading.grade_points(px, py, base, pack).view(np.uint64))
        finally:
            grading.BATTER_BIN_M = was
        for g in got[1:]:
            np.testing.assert_array_equal(g, got[0])
        # and the same as batter_apply (today's everywhere, then the corridors): today's is made only where it weighs
        f64 = np.float64
        prm = (grading.BATTER_L0_M, grading.BATTER_CREST_M, grading.BATTER_STRIP_LO, grading.BATTER_STRIP_UP,
               grading.BATTER_CLOSE_T, grading.BATTER_CLOSE_K, grading.BATTER_SMOOTH_K, grading.BATTER_CORE_BLEND_M,
               grading.T_MAX, grading.BATTER_OLD_FADE_M, 0.0, grading.BATTER_BIN_M)
        prm = tuple(float(v) for v in prm)

        def ref(qx, qy, qb):
            g0 = grading._grade_points_today(qx, qy, qb, pack, 0.0, qb.copy())
            return np.asarray(EF.batter_apply(qx, qy, qb, np.ascontiguousarray(g0, f64), pack.a, pack.b, pack.za,
                                              pack.zb, pack.h, pack.rh, pack.ba, pack.bb, *pack.bt, pack.nb, prm))
        np.testing.assert_array_equal(ref(px, py, base).view(np.uint64), got[0])
        # small calls (a few points round a road, a ground not read at some, the plans chosen by the call's box: the
        # points where today's weighs span less of it)
        for _ in range(300):
            c = rng.uniform(0, 400, 2)
            q = rng.integers(0, 2, 1)[0]
            k = int(rng.integers(2, 40))
            qx, qy = c[0] + rng.uniform(-30, 30, k), c[1] + rng.uniform(-30, 30, k)
            if q:
                qx[0], qy[0] = c[0] + 60.0, c[1] - 45.0        # (one far point widening the call's box)
            qb = ground(qx, qy) + rng.normal(0, 0.5, k)
            qb[rng.random(k) < 0.3] = np.nan
            np.testing.assert_array_equal(grading.grade_points(qx, qy, qb, pack).view(np.uint64),
                                          ref(qx, qy, qb).view(np.uint64))

    def test_memo_reads_each_segment_once(self):
        # the planner's memo of each segment's reads: the same params as reading them all, and a second plan set
        # sharing segments reads only its new ones
        rng = np.random.default_rng(5)
        plans = random_plans(rng, 40, (0, 0, 300, 300))
        asked = []

        def ground(x, y):
            asked.append(len(x))
            return 50.0 + 0.3 * np.asarray(x) - 0.1 * np.asarray(y)

        def rays(org, dirs, reach):
            return np.where(np.asarray(org)[:, 0] > 150.0, 25.0, np.inf)
        memo = grading.BatterMemo._View(grading.BatterMemo(), 7)      # (a store of its own, one planner's view)
        for sub in (plans[:25], plans[10:]):
            pack = grading.PlanPack(sub)
            want = grading.batter_params(pack, ground, rays)
            asked.clear()
            info = {}
            got = grading.batter_params(pack, ground, rays, memo, info)
            for a, b in zip(got, want):
                np.testing.assert_array_equal(a, b)
            if info.get("rows"):
                memo.add(*info["rows"])
        # (the second set: only the segments of plans[25:] read)
        first = grading.PlanPack(plans[10:25])
        k2 = 2 * len(grading.BATTER_OFFS)
        self.assertEqual(sum(asked), (len(grading.PlanPack(plans[10:]).h) - len(first.h)) * k2)
        hit, vals = memo.find(grading.BatterMemo.keys_of(first.a, first.b, first.h))
        self.assertTrue(hit.all())
        self.assertEqual(vals.shape, (len(first.h), k2 + 2))


class SlopeDressing(unittest.TestCase):
    """roads.slope_rocks / wall_skirts (the vanilla slopes' rocks and wall skirts, road revamp v2 P1a): the rocks a pure
    function of the road and the ground (the same whatever tile asks), only on the steep side, off the strip, within
    the cap; the skirts between the face and the carriageway, facing up."""

    def setUp(self):
        import types
        import geodesy
        import roads
        self.roads = roads
        self.frame = geodesy.EnuFrame(46.5, 10.45)
        self.spec = types.SimpleNamespace(kind="paved", width=7.0, mountain=False, cls="secondary", oneway=False,
                                          urban=False, walk=0.0, street=None, median=False)
        xs = np.arange(-400.0, 400.01, 25.0)
        # (a gentle bend: stations along the part, not along x)
        self.pts = np.column_stack((xs, 0.002 * xs ** 2 / 10.0, np.zeros(len(xs))))
        self.strip = 3.5 + grading.shoulder(self.spec) + grading.BATTER_L0_M

        def elev(la, lo):           # a 1:1 cut on the left (y over the strip + 3 m), flat on the right
            e, n, _ = self.frame.geodetic_to_enu(la, lo, 0.0)
            yc = np.asarray(n) - 0.002 * np.asarray(e) ** 2 / 10.0
            return np.maximum(yc - (self.strip + 3.0), 0.0) * 1.0
        self.elev = elev

    def rocks(self, box, rds=None):
        return self.roads.slope_rocks(self.frame, rds or [("r1", self.spec, self.pts, 0.0)], self.elev, box)

    def test_tile_independent_and_placed(self):
        whole = self.rocks((-512.0, -512.0, 512.0, 512.0))
        self.assertGreater(len(whole), 20)
        tiles = []
        for tx in range(-2, 2):
            for ty in range(-2, 2):
                tiles += self.rocks((tx * 256.0, ty * 256.0, (tx + 1) * 256.0, (ty + 1) * 256.0))
        self.assertEqual(sorted(map(tuple, tiles)), sorted(map(tuple, whole)))
        km = 0.8
        self.assertLessEqual(len(whole), 2 * km * 1000.0 / (self.roads.ROCK_STEP_M * self.roads.ROCK_BLOCK_N)
                             * self.roads.ROCK_PER_BLOCK + 2)
        for g, x, y, z, yaw, sc, h, rank in whole:
            self.assertEqual(g, "slope_rock")
            off = y - 0.002 * x ** 2 / 10.0
            self.assertGreater(off, self.strip - grading.BATTER_L0_M + 0.5 + self.roads.WALL_THICK_MAX_M
                               + self.roads.ROCK_WALL_GAP_M + self.roads.ROCK_R * h - 0.3)   # (left: the cut)
            gz = max(off - (self.strip + 3.0), 0.0)
            bottom = z - self.roads.ROCK_PIVOT * h
            self.assertLessEqual(bottom, gz - self.roads.ROCK_SINK[0] * h + 0.05)          # sunk 30 % or more
            self.assertGreaterEqual(bottom, gz - self.roads.ROCK_SINK_MAX * h - 0.05)
            self.assertTrue(self.roads.ROCK_H_M[0] - 1e-9 <= h <= self.roads.ROCK_H_M[1] + 1e-9)
        # (the same rows whatever other roads the tile holds, and in any order: a far road added first)
        far = ("r0", self.spec, self.pts + np.array([0.0, 900.0, 0.0]), 0.0)
        again = self.rocks((-512.0, -512.0, 512.0, 512.0), [far, ("r1", self.spec, self.pts, 0.0)])
        self.assertEqual(sorted(map(tuple, again)), sorted(map(tuple, whole)))

    def test_urban_and_flat_get_none(self):
        import types
        urb = types.SimpleNamespace(**{**vars(self.spec), "urban": True})
        self.assertEqual(self.rocks((-512.0, -512.0, 512.0, 512.0), [("u", urb, self.pts, 0.0)]), [])
        flat = self.roads.slope_rocks(self.frame, [("r1", self.spec, self.pts, 0.0)],
                                      lambda la, lo: np.zeros(len(np.atleast_1d(la))), (-512.0, -512.0, 512.0, 512.0))
        self.assertEqual(flat, [])

    def test_wall_skirt(self):
        import roadmesh
        k = 20
        xs = np.linspace(0.0, 38.0, k)
        face = 6.0
        w = {"base": np.column_stack((xs, np.full(k, face))), "toward": np.tile([0.0, -1.0], (k, 1)),
             "edge": np.column_stack((xs, np.full(k, 3.5 + self.roads.CLIFF_EDGE_M))), "top": np.full(k, 4.0),
             "foot": np.full(k, -1.0), "thick": np.full(k, 2.0), "verge": True}
        par = dict(w, parapet=True)
        self.roads.wall_skirts([w, par], lambda x, y: np.full(len(x), 0.5))
        self.assertNotIn("skirt", par)
        (run,) = w["skirt"]
        self.assertEqual(run.shape, (k, len(self.roads.SKIRT_ROWS), 3))
        width = face - 3.5 - self.roads.SKIRT_ROAD_GAP_M
        np.testing.assert_allclose(run[:, -1, 1], face - width)          # (out to the carriageway less its gap)
        np.testing.assert_allclose(run[:, 0, 2], 0.5 + self.roads.SKIRT_ROWS[0][2])
        P, T, UV, M = [], [], [], []
        roadmesh._retaining_walls([w], np.zeros(3), P, T, UV, M, 0)
        pts, tri = np.concatenate(P), T[-1]
        self.assertEqual(M[-1], 4)
        nz = np.cross(pts[tri[:, 1]] - pts[tri[:, 0]], pts[tri[:, 2]] - pts[tri[:, 0]])[:, 2]
        self.assertTrue((nz > 0).all())
        # (no skirt where the face stands just past the carriageway's edge: 0.1 m, its signed distance)
        near = dict(w, base=np.column_stack((xs, np.full(k, 3.6))))
        near.pop("skirt")
        self.roads.wall_skirts([near], lambda x, y: np.zeros(len(x)))
        self.assertNotIn("skirt", near)
        # (nor where its outer edge would lie on another road's core; its own road's is skipped)
        other = ("r2", self.spec, np.array([[0.0, 2.0, 0.0], [40.0, 2.0, 0.0]]), 0.0)
        own = ("r1", self.spec, np.array([[0.0, 0.0, 0.0], [40.0, 0.0, 0.0]]), 0.0)
        for rds, want in (([own], True), ([own, other], False)):
            ww = dict(w, fid="r1")
            ww.pop("skirt")
            self.roads.wall_skirts([ww], lambda x, y: np.zeros(len(x)), rds)
            self.assertEqual("skirt" in ww, want)


if __name__ == "__main__":
    unittest.main()
