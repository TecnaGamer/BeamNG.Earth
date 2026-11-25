"""Fill walls: a wall over the edge of the earthwork the grading made beside a road, receding from the road, as steep as the
terrain draws, covering the lattice's jagged band.

The terrain is a lattice of squares GRID_M apart, planar across each (roadmesh.GRID_M): a slope breaks cleanly only where
it is planar over a square, and a break in it (a slope's top, its foot) is drawn in teeth in the squares it crosses, as
far either side as roadmesh.jag_band: GRID_M along an axis of the lattice, GRID_DIAG_M at 45 degrees, the most. The
grading (gradebins pair1: the weights fill_walls put in Plan.cut) leaves the road's own level a strip (grading.FILL_FLAT_M)
past the core's edge, then steps down to the natural ground, and roads.fill_walls stands the wall's body over the whole
band, from the core's edge to the teeth past the step."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grading  # noqa: E402
import roadmesh  # noqa: E402
import roads  # noqa: E402
from test_roads import FRAME, LON, features  # noqa: E402

ANGLES_DEG = (0.0, 10.0, 22.5, 45.0, 80.0, 120.0, 135.0)    # (a road's angle to the lattice: any; the lattice's squares are cut
                                                            # along the 45 degree line, so teeth reach furthest at 135)
G = roadmesh.GRID_M
TOL_M = 0.005                                           # a tooth: the lattice's ground off the true ground this much
Z0 = 10.0                                               # the road's height


def lattice_height(nodes, x0, y0, px, py):
    """The height at points (px, py) of the lattice nodes[iy, ix] (GRID_M apart from (x0, y0)), each square cut from its
    south-west corner to its north-east one (the game's terrain, roadmesh.GRID_M)."""
    fx, fy = (np.asarray(px) - x0) / G, (np.asarray(py) - y0) / G
    ix, iy = np.floor(fx).astype(int), np.floor(fy).astype(int)
    u, v = fx - ix, fy - iy
    z00, z10, z01, z11 = nodes[iy, ix], nodes[iy, ix + 1], nodes[iy + 1, ix], nodes[iy + 1, ix + 1]
    return np.where(v <= u, z00 + (z10 - z00) * u + (z11 - z10) * v, z00 + (z11 - z01) * u + (z01 - z00) * v)


def smoothstep(u):
    u = np.clip(u, 0.0, 1.0)
    return u * u * (3.0 - 2.0 * u)


class GridBandTest(unittest.TestCase):
    def test_a_break_shows_in_teeth_within_jag_band_of_it_whatever_its_angle_and_place(self):
        """The ground a lattice draws of a step along a line differs from it only in the squares the line crosses, all within
        roadmesh.jag_band of the line, the most at 45 degrees to the lattice's axes (roadmesh.GRID_DIAG_M): a strip and a
        wall as wide as the band cover them whatever the road's angle."""
        axis = np.arange(-20.0, 20.01, G)
        X, Y = np.meshgrid(axis, axis)
        fine = np.arange(-10.0, 10.0, 0.03)
        FX, FY = np.meshgrid(fine, fine)
        reach = {}
        for deg in ANGLES_DEG:
            t = np.array([np.cos(np.radians(deg)), np.sin(np.radians(deg))])
            n = np.array([-t[1], t[0]])
            worst = 0.0
            for c in np.linspace(-1.0, 1.0, 33):        # (the line's place against the lattice: all of them)
                step = lambda px, py: (n[0] * px + n[1] * py > c).astype(float)    # noqa: E731
                z = lattice_height(step(X, Y), axis[0], axis[0], FX, FY)
                off = (n[0] * FX + n[1] * FY - c)[np.abs(z - step(FX, FY)) > TOL_M]
                self.assertTrue(len(off))
                worst = max(worst, float(np.abs(off).max()))
                self.assertLessEqual(float(np.abs(off).max()), float(roadmesh.jag_band(*t)) + 1e-9, (deg, c))
            reach[deg] = worst
        # (and the band is the bound, not slack: a break along an axis of the lattice reaches nearly a square, across the
        # squares' diagonal nearly the diagonal)
        self.assertGreater(reach[0.0], 0.9 * G)
        self.assertGreater(reach[135.0], 0.95 * roadmesh.GRID_DIAG_M)
        self.assertLessEqual(max(reach.values()), roadmesh.GRID_DIAG_M + 1e-9)

    def test_the_band_is_a_square_along_an_axis_and_the_diagonal_at_45_degrees(self):
        self.assertAlmostEqual(float(roadmesh.jag_band(1.0, 0.0)), G)
        self.assertAlmostEqual(float(roadmesh.jag_band(np.sqrt(0.5), np.sqrt(0.5))), roadmesh.GRID_DIAG_M)
        self.assertEqual(grading.FILL_FLAT_M, roadmesh.GRID_DIAG_M)
        self.assertEqual(roads.FILL_WALL_BAND_M, 2.0 * roadmesh.GRID_DIAG_M)


class BlendTeethTest(unittest.TestCase):
    """grading.blend_teeth_m: the most the lattice errs on the blend of a fill (a smoothstep of its height over
    grading.SLOPE_RUN x it, T_MIN to T_MAX), by its curvature."""

    def _error(self, dz, deg):
        """The largest error (m) of the lattice's drawing of the blend over a fill dz m high, its edge at deg to the lattice
        (the blend's start at every place against the lattice's squares)."""
        run = float(np.clip(grading.SLOPE_RUN * dz, grading.T_MIN, grading.T_MAX))
        t = np.array([np.cos(np.radians(deg)), np.sin(np.radians(deg))])
        n = np.array([-t[1], t[0]])
        axis = np.arange(-30.0, 30.01, G)
        X, Y = np.meshgrid(axis, axis)
        fine = np.arange(-6.0, 6.0, 0.05)
        FX, FY = np.meshgrid(fine, fine)
        worst = 0.0
        for c in np.linspace(0.0, 1.0, 9):
            def ground(px, py):
                return -dz * smoothstep((n[0] * px + n[1] * py - c) / run)
            err = np.abs(lattice_height(ground(X, Y), axis[0], axis[0], FX, FY) - ground(FX, FY)).max()
            worst = max(worst, float(err))
        return worst

    def test_the_bound_holds_at_every_angle_and_is_reached_at_the_worst(self):
        for dz in (2.0, 3.0, 5.0, 8.0):
            bound = float(grading.blend_teeth_m(dz))
            errs = {deg: self._error(dz, deg) for deg in (0.0, 22.5, 45.0, 135.0)}
            for deg, e in errs.items():
                self.assertLessEqual(e, bound + 1e-6, (dz, deg))
            self.assertGreater(max(errs.values()), 0.5 * bound, dz)     # (not slack)

    def test_a_wall_where_the_blend_is_over_the_step_the_user_set_and_the_fill_is_a_wall_high(self):
        """Over the cuttings' gate (grading.WALL_CUT_M), at most a wall's height, and the blend's teeth over
        grading.FILL_TEETH_M."""
        dz = np.array([0.5, 1.9, 2.5, 3.0, 8.0, 12.0])
        for max_h in (4.0, 10.0):
            got = grading.fill_wall_at(dz, max_h)
            want = (dz > grading.WALL_CUT_M) & (dz <= max_h) & (grading.blend_teeth_m(dz) > grading.FILL_TEETH_M)
            self.assertTrue(np.array_equal(got, want))
        self.assertFalse(grading.fill_wall_at(1.5, 10.0))
        self.assertFalse(grading.fill_wall_at(11.0, 10.0))


def road_plan(deg, filled=True, z0=Z0, length=90.0, step=4.0):
    """A primary road's grading plan through the origin along deg to the lattice's axes (its fill walls' weights on
    every segment when filled), with its carriageway and core half widths."""
    spec = roads.road_specs(features([[(0.0, 0.0), (200.0, 0.0)]], ["primary"]), None)[0]
    rh, h = grading.plan_halves(spec)
    t = np.array([np.cos(np.radians(deg)), np.sin(np.radians(deg))])
    s = np.arange(-length, length + 0.01, step)
    xy = s[:, None] * t[None, :]
    cut = None
    if filled:
        cut = np.full((len(s) - 1, grading.CUT_COLS), np.nan)
        cut[:, 6:8] = 1.0
    plan = grading.Plan(xy, np.full(len(s), z0), h, cut=cut, road_half=rh)
    return spec, rh, h, t, np.array([-t[1], t[0]]), s, xy, plan


class FillRuleTest(unittest.TestCase):
    """The kernel's fill rule (gradebins pair1) on a straight road at any angle to the lattice: its level for FILL_FLAT_M past
    the core's edge, then the natural ground, the same all along the road."""

    def _graded(self, plan, natural):
        def graded(px, py):
            return grading.grade_points(px, py, np.broadcast_to(np.asarray(natural, np.float64), np.shape(px)).copy(), [plan])
        return graded

    def test_the_road_keeps_its_level_a_strip_past_the_core_then_the_ground_steps_to_the_natural_one(self):
        for deg in ANGLES_DEG:
            spec, rh, h, t, n, s, xy, plan = road_plan(deg)
            graded = self._graded(plan, Z0 - 3.0)
            d = np.arange(0.0, h + 8.0, 0.01)
            for side in (1.0, -1.0):
                x = side * d * n[0]
                y = side * d * n[1]
                z = graded(x, y)
                foot = h + grading.FILL_FLAT_M
                self.assertTrue(np.allclose(z[d < foot - 0.01], Z0), (deg, side, "the road's level out to the step"))
                self.assertTrue(np.allclose(z[d > foot + 0.01], Z0 - 3.0), (deg, side, "the natural ground past it"))

    def test_the_ground_where_it_lies_over_the_road_is_the_blends_as_before(self):
        """On the uphill side (natural over the road) and where a segment has no weight: as without fill walls, bit for bit."""
        spec, rh, h, t, n, s, xy, filled = road_plan(22.5)
        plain = road_plan(22.5, filled=False)[-1]
        d = np.arange(0.0, 30.0, 0.37)
        px, py = np.r_[d * n[0], -d * n[0]], np.r_[d * n[1], -d * n[1]]
        up = np.full(len(px), Z0 + 3.0)
        self.assertTrue(np.array_equal(grading.grade_points(px, py, up, [filled]), grading.grade_points(px, py, up, [plain])))
        none = grading.Plan(xy, np.full(len(s), Z0), h, cut=np.full((len(s) - 1, grading.CUT_COLS), np.nan), road_half=rh)
        down = np.full(len(px), Z0 - 3.0)
        self.assertTrue(np.array_equal(grading.grade_points(px, py, down, [none]), grading.grade_points(px, py, down, [plain])))
        self.assertFalse(np.array_equal(grading.grade_points(px, py, down, [filled]), grading.grade_points(px, py, down, [plain])))

    def test_a_cuttings_wall_or_fall_on_the_side_keeps_it(self):
        """Where the plan has a cutting's wall or a fall on a side, no fill wall there (the kernel leaves the side to it)."""
        spec, rh, h, t, n, s, xy, plan = road_plan(22.5)
        fall = np.array(plan.cut)
        fall[:, 0] = h + 0.5           # a fall on the left side, its wall's offset
        walled = grading.Plan(xy, np.array(plan.z), h, cut=fall, road_half=rh)
        d = np.arange(h, h + 8.0, 0.25)
        px, py = d * n[0], d * n[1]
        z = grading.grade_points(px, py, np.full(len(d), Z0 - 3.0), [walled])
        want = grading.grade_points(px, py, np.full(len(d), Z0 - 3.0), [grading.Plan(
            xy, np.array(plan.z), h, cut=np.where(np.arange(grading.CUT_COLS) < 6, fall, np.nan), road_half=rh)])
        self.assertTrue(np.array_equal(z, want))
        self.assertGreater(float(np.ptp(z)), 0.0)

    def test_the_rule_is_the_same_along_the_road_whatever_the_segment(self):
        """The ground at one offset from the road is the same at every station along it: 4 m segments, any angle (no wall
        every segment and a slope between)."""
        for deg in (0.0, 22.5, 45.0, 135.0):
            spec, rh, h, t, n, s, xy, plan = road_plan(deg, length=120.0)
            graded = self._graded(plan, Z0 - 3.0)
            d = np.arange(0.0, h + 8.0, 0.13)
            ref = None
            for st in np.arange(-40.0, 40.01, 0.7):
                z = graded(st * t[0] + d * n[0], st * t[1] + d * n[1])
                ref = z if ref is None else ref
                self.assertLess(float(np.abs(z - ref).max()), 1e-9, (deg, st))

    def test_the_lattice_draws_the_step_in_teeth_within_the_band_the_wall_covers(self):
        """The lattice's ground differs from the graded ground only from the core's edge out to GRID_DIAG_M past the strip:
        the carriageway and its shoulder keep a clean edge, and the wall's body (roads.FILL_WALL_BAND_M from the core's edge)
        covers the rest."""
        for deg in ANGLES_DEG:
            spec, rh, h, t, n, s, xy, plan = road_plan(deg)
            graded = self._graded(plan, Z0 - 3.0)
            axis = np.arange(-110.0, 110.01, G)
            X, Y = np.meshgrid(axis, axis)
            nodes = graded(X.ravel(), Y.ravel()).reshape(X.shape)
            for side in (1.0, -1.0):
                got = []
                d = np.arange(0.0, h + 10.0, 0.02)
                for st in np.linspace(-20.0, 20.0, 9):
                    px = st * t[0] + side * d * n[0]
                    py = st * t[1] + side * d * n[1]
                    drawn = lattice_height(nodes, axis[0], axis[0], px, py)
                    got.append(d[np.abs(graded(px, py) - drawn) > TOL_M])
                teeth = np.concatenate(got)
                self.assertTrue(len(teeth), "the lattice draws the step in teeth")
                self.assertGreaterEqual(float(teeth.min()), h - 0.03, (deg, side, "none in the core: it recedes"))
                self.assertLessEqual(float(teeth.max()), h + roads.FILL_WALL_BAND_M + 0.03, (deg, side, "none past the body"))


class FillWallSelectionTest(unittest.TestCase):
    """grading.fill_walls: which segments of a plan get the weights, from the natural ground (ground) at the strip's end
    beside each vertex."""

    def _plan(self, n=12):
        spec = roads.road_specs(features([[(0.0, 0.0), (200.0, 0.0)]], ["primary"]), None)[0]
        rh, h = grading.plan_halves(spec)
        xy = np.c_[np.arange(n) * 4.0, np.zeros(n)]
        return spec, grading.Plan(xy, np.full(n, Z0), h, road_half=rh)

    def _run(self, rise, spec=None, plan=None, left=True, right=True, slope=0.0):
        """fill_walls on a road along x (north the left): the natural ground rise (per vertex) under the road beside it, on the
        sides asked, falling at slope m per m past the strip's end."""
        spec0, plan0 = self._plan(len(rise))
        spec, plan = spec or spec0, plan or plan0
        rise = np.asarray(rise, np.float64)
        h = plan.half

        def ground(xy):
            xy = np.asarray(xy, np.float64)
            at = np.clip(np.round(xy[:, 0] / 4.0).astype(int), 0, len(rise) - 1)
            past = np.abs(xy[:, 1]) - h - grading.FILL_FLAT_M
            on = np.where(xy[:, 1] > 0, left, right)
            return np.where(on, Z0 - rise[at] - slope * past, Z0)
        return grading.fill_walls([("r", plan)], [spec], ground), plan

    def _window(self):
        """A height inside the wall window for the grid in use."""
        inside = [dz for dz in np.arange(2.05, 9.9, 0.05) if grading.fill_wall_at(dz, 10.0)]
        return float(inside[0])

    def test_a_run_of_vertices_over_the_natural_ground_by_a_wall_window_gets_the_weights_on_that_side(self):
        lo = self._window()
        changed, plan = self._run([0.0, 0.0] + [lo] * 6 + [0.0] * 4, right=False)
        self.assertEqual(changed, 1)
        cut = np.asarray(plan.cut)
        self.assertEqual(cut.shape, (11, grading.CUT_COLS))
        self.assertTrue(np.array_equal(np.flatnonzero(np.isfinite(cut[:, 6])), np.arange(2, 7)))   # (between two filled vertices)
        self.assertFalse(np.isfinite(cut[:, 7]).any(), "no weight on the side the road does not stand over")
        self.assertTrue(np.all(cut[2:7, 6] == 1.0))
        self.assertFalse(np.isfinite(cut[:, :6]).any())

    def test_a_road_on_its_ground_a_fill_of_a_gentle_height_and_a_natural_slope_have_none(self):
        changed, plan = self._run([0.0] * 12)
        self.assertEqual(changed, 0)
        self.assertIsNone(plan.cut)
        changed, plan = self._run([1.5] * 12)           # (under the cuttings' gate: no wall's height)
        self.assertEqual(changed, 0)
        changed, plan = self._run([11.0] * 12)          # (over a wall's: the slope is the embankment's)
        self.assertEqual(changed, 0)
        lo = self._window()
        changed, plan = self._run([lo] * 12, slope=1.0 / grading.SLOPE_RUN + 0.05)    # (as steep as the batter: the road's slope)
        self.assertEqual(changed, 0)
        changed, plan = self._run([lo] * 12, slope=1.0 / grading.SLOPE_RUN - 0.05)
        self.assertEqual(changed, 1)

    def test_a_fill_of_a_few_vertices_is_no_embankment_and_a_gap_of_one_is_closed(self):
        lo = self._window()
        changed, _ = self._run([0.0] * 4 + [lo] * 2 + [0.0] * 6)
        self.assertEqual(changed, 0)
        changed, plan = self._run([lo] * 4 + [0.0] + [lo] * 4 + [0.0] * 3)
        self.assertEqual(changed, 1)
        w = np.isfinite(np.asarray(plan.cut)[:, 6])
        self.assertTrue(w[:8].all() and not w[9:].any())

    def test_a_cuttings_wall_or_fall_on_the_side_stays_and_a_tunnels_plans_have_none(self):
        lo = self._window()
        spec, plan = self._plan(12)
        cut = np.full((11, 6), np.nan)
        cut[3, 0:3] = (5.0, 12.0, 6.0)
        cut[5, 0:3] = (5.0, np.nan, np.nan)             # (a fall)
        plan.cut = cut
        changed, plan = self._run([lo] * 12, spec, plan)
        self.assertEqual(changed, 1)
        got = np.asarray(plan.cut)
        self.assertEqual(got.shape, (11, grading.CUT_COLS))
        self.assertTrue(np.array_equal(got[3, 0:3], [5.0, 12.0, 6.0]))
        self.assertFalse(np.isfinite(got[[3, 5], 6]).any(), "no weight on a side with a wall or a fall of its own")
        self.assertTrue(np.isfinite(got[[2, 4, 6], 6]).all() and np.isfinite(got[:, 7]).all())
        spec, plan = self._plan(12)
        self.assertEqual(grading.fill_walls([("r" + grading.TUNNEL_TAG, plan)], [spec],
                                            lambda xy: np.full(len(xy), Z0 - lo)), 0)

    def test_the_pack_has_the_weights_after_the_cuts(self):
        lo = self._window()
        changed, plan = self._run([lo] * 12)
        pack = grading.PlanPack([plan])
        self.assertEqual(pack.cut.shape[1], grading.CUT_COLS)
        self.assertTrue(np.all(pack.cut[:, 6:8] == 1.0))
        self.assertFalse(np.isfinite(pack.cut[:, :6]).any())


class FillWallBuilderTest(unittest.TestCase):
    """roads.fill_walls on the ground the kernel graded, drawn on the lattice, at any angle to it."""

    def _case(self, deg, natural, filled=True, pts_n=None):
        spec, rh, h, t, n, s, xy, plan = road_plan(deg, filled=filled)

        def elev(la, lo):               # (the grader's own ground, as the tile build asks it: the graded function itself)
            e, nn, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), np.zeros(np.shape(la)))
            return grading.grade_points(e, nn, np.broadcast_to(np.asarray(natural(e, nn), np.float64), np.shape(e)).copy(), [plan])
        pts = np.c_[xy, np.full(len(s), Z0)]
        ws = roads.fill_walls(FRAME, [("r", spec, pts, LON)], elev, (-128.0, -128.0, 128.0, 128.0))
        return spec, rh, h, t, n, ws

    def test_a_wall_over_the_band_on_the_filled_sides_at_any_angle(self):
        for deg in ANGLES_DEG:
            with self.subTest(deg=deg):
                spec, rh, h, t, n, ws = self._case(deg, lambda x, y: Z0 - 3.0)
                self.assertEqual(len(ws), 2, "a wall each side")
                for side in (1.0, -1.0):
                    w = [w for w in ws if np.sign(float(np.median(np.asarray(w["base"]) @ n))) == side][0]
                    base, thick = np.asarray(w["base"]), np.asarray(w["thick"])
                    out = np.abs(base @ n)
                    self.assertTrue(np.allclose(out, h + roads.FILL_WALL_BAND_M, atol=0.02))
                    self.assertTrue(np.all(out - thick > rh), "the body stands off the carriageway")
                    self.assertTrue(np.allclose(out - thick, h + roads.FILL_WALL_INSET_M, atol=0.02))
                    # the face looks away from the road, its top flush with the road, its foot under the natural ground
                    self.assertTrue(np.all(np.asarray(w["toward"]) @ n * side > 0.99))
                    self.assertTrue(np.allclose(w["top"], Z0 + roads.FILL_WALL_TOP_M, atol=0.02))
                    self.assertTrue(np.all(np.asarray(w["foot"]) < Z0 - 3.0))
                    self.assertTrue(w["fill"])

    def test_no_wall_on_a_side_where_the_ground_is_over_the_road_or_beside_a_natural_cliff(self):
        spec, rh, h, t, n, ws = self._case(22.5, lambda x, y: Z0 - 3.0, filled=False)
        self.assertFalse(ws, "no strip: no fill wall")
        spec, rh, h, t, n, ws = self._case(22.5, lambda x, y: Z0 + 3.0)
        self.assertFalse(ws, "the ground over the road: a cutting's, not a fill's")
        # a cliff: the natural ground goes on falling steeply past the band
        spec, rh, h, t, n, ws = self._case(22.5, lambda x, y: Z0 - 3.0 - 1.0 * np.maximum(np.abs(x * -np.sin(np.radians(22.5)) + y * np.cos(np.radians(22.5))) - h - 4.0, 0.0))
        self.assertFalse(ws, "a natural cliff")

    def test_the_wall_is_solid_flush_with_the_road(self):
        spec, rh, h, t, n, ws = self._case(22.5, lambda x, y: Z0 - 3.0)
        P, T, UV, M = [], [], [], []
        nv = roadmesh._retaining_walls(ws[:1], np.zeros(3), P, T, UV, M, 0)
        pts = np.concatenate(P)
        self.assertGreater(nv, 0)
        self.assertAlmostEqual(float(pts[:, 2].max()), Z0 + roads.FILL_WALL_TOP_M, places=3)      # (the top is the road's level)
        self.assertLess(float(pts[:, 2].min()), Z0 - 3.0)                                          # (down to under the natural ground)


if __name__ == "__main__":
    unittest.main()
