"""Ground graded under roads (grading.py): the profile smoothing, flat across the core, untouched away from
roads, junctions blended, the plans made once per cell, and GradedDem as a drop-in DEM."""
import os
import sys
import unittest
import unittest.mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy  # noqa: E402
import grading  # noqa: E402
import mapcells  # noqa: E402
import roads  # noqa: E402
from test_roads import features, rules, LAT, LON, FRAME  # noqa: E402


class ProfileTest(unittest.TestCase):
    def test_smoothing_keeps_grades_removes_bumps(self):
        s = np.arange(0, 400, 2.0)
        ramp = 0.05 * s
        self.assertTrue(np.allclose(grading.smooth_profile(ramp, 2.0, 12.0)[20:-20], ramp[20:-20], atol=1e-6),
                        "a constant grade stays")
        bumpy = ramp + 0.3 * np.sin(s / 3.0)
        sm = grading.smooth_profile(bumpy, 2.0, 12.0)
        self.assertLess(np.abs(sm - ramp)[20:-20].max(), 0.02, "short bumps smoothed away")
        self.assertEqual(len(sm), len(s))

    def test_a_bridge_deck_in_the_data_is_no_hump(self):
        # GitHub #271: the elevation data had a bridge deck 8.5 m up in 3 samples (6 m) where it crosses over the road
        s = np.arange(0, 300, 2.0)
        ground = 50.0 + 0.01 * s
        spiked = ground.copy()
        spiked[74:77] += 8.5
        xy = np.stack([s, np.zeros(len(s))], axis=1)
        plan = grading._plan(xy, spiked, roads.Spec(7.0, "paved", False, "residential"))
        self.assertLess(float(np.max(plan.z - np.interp(plan.xy[:, 0], s, ground))), 0.2)
        steps = grading.median_profile(ground, 2.0, grading.MEDIAN_M)
        self.assertTrue(np.allclose(steps, ground), "a constant grade stays, ends included")

    def test_the_parts_of_a_road_meet_at_one_height(self):
        # a road on a 20 % grade split in two at a junction (GitHub #266, BR-381): each part's smoothed profile keeps
        # the grade to its end and ends at the ground's height there, so the two meet without a step (mirrored at the
        # ends, one ended 1.3 m under the ground and the next began 0.8 m over it)
        s = np.arange(0, 400, 2.0)
        ground = 0.20 * s + 0.3 * np.sin(s / 9.0)
        a, b = grading.smooth_profile(ground[:101], 2.0, 12.0), grading.smooth_profile(ground[100:], 2.0, 12.0)
        self.assertAlmostEqual(a[-1], ground[100], places=9)
        self.assertAlmostEqual(b[0], ground[100], places=9)
        ramp = 0.20 * s
        self.assertTrue(np.allclose(grading.smooth_profile(ramp, 2.0, 12.0), ramp, atol=1e-9), "the grade to the ends")
        short = 0.1 * np.arange(6) * 2.0        # shorter than the kernel: the line through its ends
        self.assertTrue(np.allclose(grading.smooth_profile(short, 2.0, 12.0), short, atol=1e-9))


class BankTest(unittest.TestCase):
    """A motorway's curve leans into its turn (grading.bank, grade_points): its inside lower, a straight level."""

    def arc(self, r, ang, step=2.0):
        th = np.arange(0.0, ang, step / r)
        return np.stack([r * np.sin(th), r - r * np.cos(th)], axis=1)       # a left turn from (0, 0) heading +x

    def test_left_curve_banked_inside_lower(self):
        xy = self.arc(400.0, 1.2)
        sp = roads.Spec(11.0, "paved", False, "motorway")
        b = grading.bank(xy, sp, ext=(1, 1))
        self.assertIsNotNone(b)
        mid = b[len(b) // 2]
        v, f = grading.BANK_DESIGN["motorway"]
        r_min = v * v / (127.0 * (grading.BANK_E_MAX + f))
        self.assertAlmostEqual(mid, -grading.BANK_E_MAX * min(1.0, r_min / 400.0), places=2)   # left (inside) lower
        plan = grading._plan(xy, np.full(len(xy), 50.0), sp)
        # graded across the carriageway at the middle: the outside (right) higher than the inside (left)
        i = len(xy) // 2
        p = xy[i]
        th = np.arctan2(xy[i + 1, 1] - xy[i - 1, 1], xy[i + 1, 0] - xy[i - 1, 0])
        n = np.array([-np.sin(th), np.cos(th)])          # left
        pts = np.array([p + n * 4.0, p - n * 4.0])
        z = grading.grade_points(pts[:, 0], pts[:, 1], np.full(2, 50.0), [plan])
        self.assertAlmostEqual(float(z[1] - z[0]), -mid * 8.0, places=1)

    def test_straight_and_city_streets_level(self):
        xy = np.stack([np.arange(0, 300, 2.0), np.zeros(150)], axis=1)
        self.assertIsNone(grading.bank(xy, roads.Spec(11.0, "paved", False, "motorway"), ext=(1, 1)))
        self.assertIsNone(grading.bank(self.arc(150.0, 1.0), roads.Spec(7.0, "paved", False, "residential")))

    def test_faded_to_level_at_a_bridge(self):
        xy = self.arc(300.0, 1.0)
        b = grading.bank(xy, roads.Spec(11.0, "paved", False, "motorway"), abut=(True, False), ext=(0, 1))
        self.assertEqual(float(b[0]), 0.0)
        self.assertLess(abs(float(b[len(b) // 2])), 0.09)
        self.assertGreater(abs(float(b[len(b) // 2])), 0.03)


class CrestTest(unittest.TestCase):
    def test_no_kinks_over_a_crest(self):
        # a road over a crest (the grade falls 4 % per 10 m): the graded centreline's slope changes smoothly, not
        # in a kink at every plan vertex (r/BeamNG 2026-10-17: 'bumpy roads, worst uphill and at speed'; with
        # 16 m vertices the slope jumped 6.4 % at each)
        xy = np.stack([np.arange(-150.0, 150.1, grading.PROFILE_STEP), np.zeros(151)], axis=1)
        z = 20.0 - 0.002 * xy[:, 0] ** 2
        plan = grading._plan(xy, z, roads.Spec(7.0, "paved", False, "residential"))
        px = np.arange(-60.0, 60.1, 0.5)
        out = grading.grade_points(px, np.zeros(len(px)), 20.0 - 0.002 * px ** 2, [plan])
        slope = np.diff(out[::4]) / 2.0                     # per 2 m (the terrain grid)
        self.assertLess(np.abs(np.diff(slope)).max(), 0.02)


def slope_ground(x, y):
    """A hillside: 10 % up to the north, plus small bumps."""
    return 0.10 * y + 0.2 * np.sin(x / 7.0) * np.cos(y / 5.0)


def plan_line(x0, x1, y, kind="paved", cls="residential", w=6.0):
    xy = np.stack([np.linspace(x0, x1, 200), np.full(200, y)], axis=1)
    z = grading.smooth_profile(slope_ground(xy[:, 0], xy[:, 1]), (x1 - x0) / 199, grading.SIGMA_M)
    keep = np.r_[np.arange(0, 199, 8), 199]
    spec = roads.Spec(w, kind, False, cls)
    width = roads.carriageway(cls, kind, True) if kind in roads.BUILT else w
    return grading.Plan(xy[keep], z[keep], width / 2 + grading.shoulder(spec)), spec


class GradePointsTest(unittest.TestCase):
    def grid(self):
        xs, ys = np.meshgrid(np.arange(-60.0, 60.1, 1.0), np.arange(-40.0, 40.1, 1.0))
        return xs.ravel(), ys.ravel(), slope_ground(xs.ravel(), ys.ravel())

    def test_flat_across_untouched_away(self):
        px, py, base = self.grid()
        p, _ = plan_line(-200, 200, 0.0)
        out = grading.grade_points(px, py, base, [p])
        core = np.abs(py) <= p.half - 0.01
        for x in (-30.0, 0.0, 25.0):
            col = core & (np.abs(px - x) < 0.5)
            self.assertLess(np.ptp(out[col]), 0.02, f"flat across the core at x={x}")
        far = np.abs(py) > p.half + grading.T_MAX + 0.5
        self.assertTrue(np.allclose(out[far], base[far]), "untouched beyond the transition")
        mid = (np.abs(py) > p.half) & (np.abs(py) < p.half + grading.T_MIN)
        self.assertTrue((np.abs(out[mid] - base[mid]) <= np.abs(out[core].mean() - base[mid]) + 1.0).all())

    def test_lift_under_roads_only(self):
        px, py, base = self.grid()
        p, _ = plan_line(-200, 200, 0.0)
        flat = grading.grade_points(px, py, base, [p])
        lifted = grading.grade_points(px, py, base, [p], lift=grading.ROAD_LIFT)
        core = np.abs(py) <= p.half - 0.01
        self.assertTrue(np.allclose(lifted[core] - flat[core], grading.ROAD_LIFT), "the physics ground under the road")
        far = np.abs(py) > p.half + grading.T_MAX + 0.5
        self.assertTrue(np.allclose(lifted[far], base[far]), "untouched away from roads")
        self.assertTrue(((lifted - flat) <= grading.ROAD_LIFT + 1e-9).all() and ((lifted - flat) >= -1e-9).all())

    def test_junction_blends_and_deterministic(self):
        px, py, base = self.grid()
        a, _ = plan_line(-200, 200, 0.0)
        b = grading.Plan(np.array([[10.0, -200], [10.0, 200]]), np.array([slope_ground(10.0, -200.0),
                                                                          slope_ground(10.0, 200.0)]), 4.0)
        out1 = grading.grade_points(px, py, base, [a, b])
        out2 = grading.grade_points(px, py, base, [a, b])
        self.assertTrue(np.array_equal(out1, out2))
        j = (np.abs(px - 10) < 0.5) & (np.abs(py) < 0.5)
        self.assertTrue(np.isfinite(out1[j]).all())


class AbutmentTest(unittest.TestCase):
    """GitHub #38 (the D1 near Presov): a motorway onto a bridge over a road in a cutting 8 m under it."""

    def test_fill_keeps_the_grade_down_the_embankment(self):
        d = np.arange(0.0, 100.0, grading.PROFILE_STEP)
        road = 372.8 + 0.01 * d                 # the road on its embankment, rising 1 % away from the bridge
        z = np.where(d < 10.0, 365.0 + (road - 365.0) * d / 10.0, road)     # its map line ends down the slope
        filled = grading.fill_abutment(z, False)
        self.assertTrue(np.allclose(filled, road, atol=0.05), "the grade kept to the end")
        back = grading.fill_abutment(z[::-1], True)
        self.assertTrue(np.allclose(back, filled[::-1]), "the same from the other end")
        steady = 380.0 - 0.06 * d               # a road falling 6 % onto a bridge all the way: left alone
        self.assertIs(grading.fill_abutment(steady, False), steady)

    def test_the_abutment_is_its_roads(self):
        # the approach (abut at its first vertex) ends 9.7 m from the centreline of a road cut 8 m under it
        xy = np.stack([np.arange(0.0, 80.1, 4.0), np.zeros(21)], axis=1)
        approach = grading.Plan(xy, np.full(21, 372.9), 6.9, abut=(True, False))
        ys = np.arange(-200.0, 200.1, 4.0)
        lower = grading.Plan(np.stack([np.full(len(ys), -9.7), ys], axis=1), np.full(len(ys), 364.6), 8.6)
        px, py = np.array([0.0, 2.0]), np.array([0.0, 0.0])
        base = np.array([369.9, 370.6])
        out = grading.grade_points(px, py, base, [approach, lower])
        self.assertTrue(np.allclose(out, 372.9, atol=0.25), out)     # (the segments along it still blend a little)
        plain = grading.Plan(xy, np.full(21, 372.9), 6.9)     # (not onto a bridge, nor SEP_ON: ...)
        with unittest.mock.patch.object(grading, "SEP_ON", False):
            # ... the lower road's verge still gives way to the approach's carriageway, 8 m over it (grade_points' rh)
            self.assertGreater(grading.grade_points(px, py, base, [plain, lower])[0], 372.5)


class SeparationTest(unittest.TestCase):
    """GitHub #92: a street in a cutting, a service road on the bank 7.5 m above it beside its core."""

    def setUp(self):
        xs = np.arange(-100.0, 100.1, 4.0)
        self.street = grading.Plan(np.stack([xs, np.zeros(len(xs))], axis=1), np.full(len(xs), 589.4), 9.1)
        self.bank = grading.Plan(np.stack([xs, np.full(len(xs), -15.0)], axis=1), np.full(len(xs), 596.9), 3.0)
        self.py = np.arange(-9.0, 9.1, 1.0)                         # across the street's core (the bank's 3 m off)
        self.px = np.zeros(len(self.py))
        self.base = np.where(self.py < -4.0, 594.0, 589.4)

    def test_the_street_stays_flat_across_its_core(self):
        out = grading.grade_points(self.px, self.py, self.base, [self.street, self.bank])
        self.assertLess(np.ptp(out), 0.3, out)
        with unittest.mock.patch.object(grading, "SEP_ON", False):
            # (the bank pulled its edge up before SEP; the carriageway rule keeps it flat too: the bank's verge
            # gives way to the street's carriageway, 7.5 m under it)
            old = grading.grade_points(self.px, self.py, self.base, [self.street, self.bank])
        self.assertLess(np.ptp(old), 0.3, old)


class CarriagewayFirstTest(unittest.TestCase):
    """A road in a cutting (carriageway 4 m each side) and a street 7 m above it, centres 10 m apart, its sidewalk verge
    3 m past its own carriageway (5.3 m): the verge reaches over the cutting's carriageway and gives way to it."""

    def test_verge_gives_way(self):
        xs = np.arange(-100.0, 100.1, 4.0)
        low = grading.Plan(np.stack([xs, np.zeros(len(xs))], axis=1), np.full(len(xs), 535.0), 7.0, road_half=4.0)
        high = grading.Plan(np.stack([xs, np.full(len(xs), 10.0)], axis=1), np.full(len(xs), 542.0), 8.3,
                            road_half=5.3)
        py = np.arange(-4.0, 4.01, 0.5)                              # across the low carriageway
        px = np.zeros(len(py))
        base = np.where(py > 1.0, 538.0, 535.0)
        out = grading.grade_points(px, py, base, [low, high])
        np.testing.assert_allclose(out, 535.0 + grading.ROAD_LIFT * 0, atol=0.3)
        up = grading.grade_points(np.zeros(5), np.arange(5.0, 7.1, 0.5), np.full(5, 542.0), [low, high])
        np.testing.assert_allclose(up, 542.0, atol=0.3)              # the street's own carriageway stays up


class SharpCutTest(unittest.TestCase):
    """_add_cuts: a street along y = 0 with a smoothed 4 m wall rising beside its left side (+y) gets a cutting there:
    the wall where the climb is half done, its top 4 m up; the open right side none."""

    def test_wall_on_one_side(self):
        import types
        xy = np.c_[np.arange(0.0, 81.0, 8.0), np.zeros(11)]
        p = types.SimpleNamespace(xy=xy, z=np.full(11, 10.0), half=5.0, road_half=4.0, cut=None)

        def elev(la, lo):
            _e, n, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), np.zeros(np.shape(la)))
            return 10.0 + 4.0 / (1.0 + np.exp(-(n - 8.0) / 0.8))
        grading._add_cuts(FRAME, [p], elev)
        self.assertIsNotNone(p.cut)
        self.assertEqual(p.cut.shape, (10, 6))
        wall, top, reach = p.cut[:, 0], p.cut[:, 1], p.cut[:, 2]
        self.assertTrue(np.isfinite(wall).all() and np.isfinite(top).all())
        np.testing.assert_allclose(wall, 8.0, atol=1.01)                # the half-way point of the climb
        np.testing.assert_allclose(top, 10.0 + 3.6, atol=0.4)          # 90 % of it
        self.assertTrue((reach > wall).all())
        self.assertTrue(np.isnan(p.cut[:, 3:]).all(), "the open side: no cutting")

    def test_gentle_slope_is_no_wall(self):
        import types
        xy = np.c_[np.arange(0.0, 81.0, 8.0), np.zeros(11)]
        p = types.SimpleNamespace(xy=xy, z=np.full(11, 10.0), half=5.0, road_half=4.0, cut=None)

        def elev(la, lo):
            _e, n, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), np.zeros(np.shape(la)))
            return 10.0 + 0.2 * np.abs(n)                                # 1 in 5: a bank, not a wall
        grading._add_cuts(FRAME, [p], elev)
        self.assertIsNone(p.cut)


class ShoulderTest(unittest.TestCase):
    def test_sidewalk_classes_keep_the_sidewalk_flat(self):
        res = roads.Spec(6.0, "paved", True, "residential")
        self.assertGreaterEqual(grading.shoulder(res), roads.SIDEWALK["w"], "the raised slab sits on flat ground")
        self.assertEqual(grading.shoulder(roads.Spec(6.0, "paved", False, "motorway")), grading.SHOULDER_M)
        self.assertEqual(grading.shoulder(roads.Spec(4.0, "dirt", False, "track")), 0.0)


class FakeMD:
    def __init__(self, feats):
        self.feats, self.calls = feats, 0

    def cell(self, key, layers=None, kind=None):
        self.calls += 1

        class C:
            pass
        c = C()
        c.layers = {"roads": self.feats}
        return c


class FailingMD:
    def __init__(self):
        self.calls = 0

    def cell(self, key, layers=None, kind=None):
        self.calls += 1
        raise OSError("offline")


class GraderTest(unittest.TestCase):
    def test_failed_fetch_not_retried_at_once(self):
        md = FailingMD()
        g = grading.Grader(FRAME, md, lambda la, lo: np.zeros(np.shape(la)))
        lat, lon = np.full((2, 2), LAT), np.full((2, 2), LON)
        base = np.ones((2, 2))
        self.assertTrue(np.array_equal(g.apply(lat, lon, base), base), "ungraded without road data")
        n = md.calls
        g.apply(lat, lon, base)
        self.assertEqual(md.calls, n, "a failed cell waits RETRY_S before it is fetched again")

    def test_plans_once_per_cell_and_dem_dropin(self):
        f = features([[(-300, 20), (300, 20)]], ["residential"])
        md = FakeMD(f)
        g = grading.Grader(FRAME, md, lambda la, lo: np.full(np.shape(la), 10.0))
        lat = np.full((3, 3), LAT)
        lon = np.full((3, 3), LON)
        g.apply(lat, lon, np.zeros((3, 3)))
        n = md.calls
        g.apply(lat, lon, np.zeros((3, 3)))
        self.assertEqual(md.calls, n, "cell plans cached")

        class Dem:
            def sample(self, la, lo, zoom, progress=None):
                return np.zeros(np.shape(la))
            other = 42
        gd = grading.GradedDem(Dem(), g)
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0, 0.0]), np.array([20.0, 200.0]), 0.0)
        z = gd.sample(la, lo, 15)
        self.assertAlmostEqual(float(z[0]), 10.0, delta=0.05, msg="on the road: its profile")
        self.assertAlmostEqual(float(z[1]), 0.0, delta=1e-9, msg="far from it: the DEM")
        self.assertEqual(gd.other, 42, "other attributes pass through")


    def test_points_far_apart_plan_only_their_cells(self):
        # two points 27 km apart (a straight salt-flats track's ends): the cells around each, not the ~240 of their
        # bbox (one road tile planned them all: 53 s, 2026-10-20); the same heights, in an order of their own
        f = features([[(-300, 20), (300, 20)]], ["residential"])
        md = FakeMD(f)
        g = grading.Grader(FRAME, md, lambda la, lo: np.full(np.shape(la), 10.0))
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0, 19000.0]), np.array([20.0, 19000.0]), 0.0)
        bbox = mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()))
        self.assertGreater(len(bbox), 100)
        out = g.apply(la, lo, np.zeros(2))
        # (each point's own cells and those whose plans reach it: a cell plans CLIP_MARGIN_M past its edges; and the
        # bridges of their neighbours, read for the underpasses: grading.UNDER_*)
        self.assertLessEqual(md.calls, 30, "the cells around the two points")
        self.assertAlmostEqual(float(out[0]), 10.0, delta=0.05)
        near = mapcells.cells_near_points(la, lo, 1e-4, 1e-4)
        self.assertTrue(set(near) <= set(bbox))
        self.assertEqual(near, sorted(near, key=lambda k: (k.y, k.x)), "row-major")
        self.assertEqual({mapcells.cell_of(float(a), float(b)) for a, b in zip(la, lo)} - set(near), set())

    def test_plan_order_independent_of_the_cells(self):
        f = features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]], ["residential", "secondary"])
        g = grading.Grader(FRAME, FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0, 5.0]), np.array([20.0, 25.0]), 0.0)
        a = g.plans(la, lo)
        la2, lo2, _ = FRAME.enu_to_geodetic(np.array([0.0, 5.0, 3000.0]), np.array([20.0, 25.0, -3000.0]), 0.0)
        b = g.plans(la2, lo2)
        self.assertEqual([id(p) for p in a], [id(p) for p in b])

    @unittest.mock.patch.object(grading, "SHARP_ON", False)        # (the profile's samples alone counted)
    def test_long_parts_planned_near_the_cell_only(self):
        # a 30 km straight track (one segment): its plan in a cell covers the cell and CLIP_MARGIN_M round it, not
        # its whole length (each cell sampled the ground to both ends: thousands of DEM tiles, 2026-10-20), with the
        # same heights in the cell; a short road is planned whole
        la, lo, _ = FRAME.enu_to_geodetic(np.array([-15000.0, 15000.0]), np.array([20.0, 20.0]), 0.0)
        f = features([[(-15000, 20), (15000, 20)], [(-300, 400), (300, 400)]], ["residential", "residential"])
        md = FakeMD(f)
        asked = []

        def elev(la_, lo_):
            asked.append(len(np.atleast_1d(la_)))
            e, _, _ = FRAME.geodetic_to_enu(la_, lo_, 0.0)
            return 10.0 + 0.001 * np.asarray(e)         # a gentle slope along the track
        g = grading.Grader(FRAME, md, elev)
        key = mapcells.cell_of(LAT, LON)
        plans = g._cell_plans(key)
        box = g._clip_box(key)
        xs = np.concatenate([p.xy[:, 0] for _f, p in plans])
        self.assertGreaterEqual(xs.min(), box[0] - 1e-6)
        self.assertLessEqual(xs.max(), box[2] + 1e-6)
        self.assertLess(sum(asked), 30000 / grading.PROFILE_STEP / 2, "not sampled along the whole track")
        short = [p for fid, p in plans if np.ptp(p.xy[:, 0]) < 700]
        self.assertEqual(len(short), 1, "the short road whole")
        y = float(plans[0][1].xy[0, 1])                 # on the track
        e = np.linspace(-600.0, 600.0, 13)
        lat, lon, _ = FRAME.enu_to_geodetic(e, np.full(len(e), y), 0.0)
        out = g.apply(lat, lon, np.zeros(len(e)))
        whole = grading.Grader(FRAME, FakeMD(f), elev)
        with unittest.mock.patch.object(grading, "LONG_PART_M", 1e9):
            want = whole.apply(lat, lon, np.zeros(len(e)))
        np.testing.assert_allclose(out, want, atol=1e-3)
        self.assertGreater(float(out.min()), 9.0, "graded to the track")

    def test_clip_polyline(self):
        xy = np.array([[-50.0, 0], [50, 0], [50, 50], [150, 50], [150, -150], [-150, -150]])
        runs = grading.clip_polyline(xy, (0, -10, 100, 100))
        self.assertEqual([r.tolist() for r in runs], [[[0.0, 0.0], [50.0, 0.0], [50.0, 50.0], [100.0, 50.0]]])
        self.assertEqual(grading.clip_polyline(np.array([[200.0, 0], [300, 0]]), (0, 0, 10, 10)), [])
        two = grading.clip_polyline(np.array([[-5.0, 5], [15, 5], [15, 7], [-5, 7]]), (0, 0, 10, 10))
        self.assertEqual(len(two), 2)


class ChainTest(unittest.TestCase):
    """A road split at a junction (GitHub #266, BR-381): its parts are smoothed as one road."""

    @staticmethod
    def hill(la, lo):           # a 20 % grade, with a 1.5 m bump in the data right at the junction
        e, n, _ = FRAME.geodetic_to_enu(la, lo, 0.0)
        e = np.asarray(e)
        return 100.0 + 0.20 * e + 0.4 * np.sin(e / 11.0) + 1.5 * np.exp(-(e / 6.0) ** 2)

    def plans(self, on):
        # A ends at the junction (0, 0) where B goes on straight and C branches off 31 degrees to the left
        f = features([[(-300, 0), (0, 0)], [(0, 0), (300, 0)], [(0, 0), (200, 120)]], ["primary"] * 3)
        with unittest.mock.patch.object(grading, "CHAIN_ON", on):
            g = grading.Grader(FRAME, FakeMD(f), self.hill)
            return {fid: p for fid, p in g._cell_plans(mapcells.cell_of(LAT, LON))}

    def test_the_parts_meet_at_one_height(self):
        p = self.plans(True)
        a, b = p["id000"], p["id001"]
        self.assertLess(abs(a.z[-1] - b.z[0]), 0.01)
        self.assertLess(a.z[-1], 101.0, "smoothed through the junction: the bump in the data mostly gone")
        alone = self.plans(False)
        self.assertGreater(alone["id000"].z[-1], 101.4, "smoothed alone: each part pinned to the bump's top")
        # the grade goes on through the junction (the plans' last / first 8 m)
        ga = (a.z[-1] - a.z[-3]) / np.hypot(*(a.xy[-1] - a.xy[-3]))
        gb = (b.z[2] - b.z[0]) / np.hypot(*(b.xy[2] - b.xy[0]))
        self.assertAlmostEqual(ga, gb, delta=0.03)

    def test_the_straightest_part_goes_on(self):
        import fast
        cand = [np.array([[0.0, 0.0], [300.0, 0.0]]), np.array([[200.0, 120.0], [0.0, 0.5]]),
                np.array([[0.0, 0.0], [0.0, 300.0]]), np.array([[0.0, 0.0], [250.0, 1.0]])]
        cxy = np.concatenate(cand)
        coff = np.array([0, 2, 4, 6, 8], np.int64)

        def ext(roads, cls=(1, 1, 1, 1), reach=50.0):
            pts, off = fast._ef.chain_extensions(
                cxy, coff, np.array(cls, np.int64), np.array(roads, np.int64), np.array([[0.0, 0.0]]),
                np.array([[1.0, 0.0]]), np.array([1], np.int64), np.array([7], np.int64), np.array([reach]),
                1.0, 35.0, 10.0)
            return np.asarray(pts[off[0]:off[1]])
        line = ext([1, 2, 3, 4])
        self.assertEqual(line[0].tolist(), [0.0, 0.0])
        self.assertAlmostEqual(float(np.hypot(*np.diff(line, axis=0).T).sum()), 50.0, places=6)
        self.assertLess(abs(line[-1, 1]), 1e-6, "the straight one, not the 31 degree branch")
        self.assertAlmostEqual(float(ext([7, 2, 3, 4])[-1, 1]), 0.2, places=4, msg="not its own road: the next straightest")
        self.assertGreater(float(ext([7, 2, 3, 7])[-1, 1]), 10.0, "then the branch, its end at the node, turned")
        self.assertEqual(len(ext([7, 7, 3, 7])), 0, "never a 90 degree turn")
        self.assertEqual(len(ext([1, 2, 3, 4], cls=(2, 2, 2, 2))), 0, "only roads smoothed alike")


class MajorRoadTest(unittest.TestCase):
    """Motorways and trunks are smoothed more (SIGMA_MAJOR_M); a road ending on one meets it at its height (pins)."""

    @staticmethod
    def bumpy(la, lo):          # a 4 % grade with a 3 m bump 60 m long (a forest edge in a surface model)
        e, n, _ = FRAME.geodetic_to_enu(la, lo, 0.0)
        e = np.asarray(e)
        return 100.0 + 0.04 * e + 3.0 * np.exp(-((e - 20.0) / 30.0) ** 2)

    def plans(self):
        # the motorway split at the slip road's junction (0, 0); the slip road leaves at 25 degrees
        f = features([[(-400, 0), (0, 0)], [(0, 0), (400, 0)], [(0, 0), (180, -84)]], ["motorway"] * 3)
        f.rules["subclass"] = rules([(2, "link")])
        g = grading.Grader(FRAME, FakeMD(f), self.bumpy)
        return {fid: p for fid, p in g._cell_plans(mapcells.cell_of(LAT, LON))}

    def test_major_roads_smoothed_more(self):
        self.assertEqual(grading.profile_sigma(roads.Spec(7.0, "paved", False, "motorway")), grading.SIGMA_MAJOR_M)
        self.assertEqual(grading.profile_sigma(roads.Spec(7.0, "paved", False, "motorway", link=True)), grading.SIGMA_M)
        p = self.plans()
        a = p["id000"]
        bump = np.interp(20.0, a.xy[:, 0], a.z) if a.xy[-1, 0] > 20 else np.interp(20.0, p["id001"].xy[:, 0],
                                                                                    p["id001"].z)
        self.assertLess(bump - (100.0 + 0.04 * 20.0), 1.6, "most of the bump smoothed out of the motorway")

    def test_a_slip_road_meets_the_motorway(self):
        p = self.plans()
        mway, link = p["id001"], p["id002"]
        self.assertLess(abs(mway.z[0] - p["id000"].z[-1]), 0.01, "the motorway's parts meet")
        self.assertLess(abs(link.z[0] - mway.z[0]), 0.01, "the slip road ends at the motorway's height")
        self.assertGreater(abs(link.z[0] - self.bumpy(np.array([LAT]), np.array([LON]))[0]), 0.5,
                           "not at the bump's height")


class OverTunnels(unittest.TestCase):
    """Grader._over_tunnels (earthfast.over_tunnel): over the tube within its half width, from TUNNEL_KEEP_IN_M inside
    its portals."""

    def test_mask(self):
        import threading
        import types
        xy = np.array([[0.0, 0.0], [100.0, 0.0], [200.0, 0.0]])
        g = types.SimpleNamespace(lock=threading.Lock(), covers={"a": [(xy, 6.0)]})
        k = grading.TUNNEL_KEEP_IN_M
        e = np.array([50.0, 150.0, 50.0, k / 2, 200.0 - k / 2, k + 1.0, -5.0])
        n = np.array([2.0, -5.5, 7.0, 0.0, 0.0, 0.0, 0.0])
        got = grading.Grader._over_tunnels(g, e, n)
        np.testing.assert_array_equal(got, [True, True, False, False, False, True, False])
        self.assertIsNone(grading.Grader._over_tunnels(g, np.array([50.0]), np.array([30.0])))


class WallHeightTest(unittest.TestCase):
    def test_city_concrete_walls_full_height(self):
        """A major road's cutting in town keeps a full-height concrete wall (McGraw-Graben); stone walls elsewhere
        stop at WALL_MAX_H_M with the slope above them (mountain passes)."""
        import roads
        city = roads.Spec(width=12.0, kind="paved", urban=True, cls="primary", oneway=False)
        rural = roads.Spec(width=7.0, kind="paved", urban=False, cls="primary", oneway=False)
        street = roads.Spec(width=7.0, kind="paved", urban=True, cls="residential", oneway=False)
        self.assertEqual(grading.wall_max_h(city), grading.WALL_MAX_H_CITY_M)
        self.assertEqual(grading.wall_max_h(rural), grading.WALL_MAX_H_M)
        self.assertEqual(grading.wall_max_h(street), grading.WALL_MAX_H_M)


class UnderpassTest(unittest.TestCase):
    def test_road_dips_under_a_motorway_overpass(self):
        """A local road crossing under a short motorway bridge whose terrain was interpolated at deck height (lidar
        with the bridge removed) dips to the deck minus UNDER_CLEAR_M; a road crossing nothing is left alone."""
        f = features([[(-200, 0), (200, 0)], [(0, -20), (0, 20)], [(-200, 300), (200, 300)]],
                     ["residential", "motorway", "residential"], flags=[(1, "is_bridge")])
        g = grading.Grader(FRAME, FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0, 195.0, 0.0]), np.array([0.0, 0.0, 300.0]), 0.0)
        out = g.apply(la, lo, np.full(3, 10.0))
        self.assertAlmostEqual(float(out[0]), 10.0 - min(grading.UNDER_CLEAR_M, grading.UNDER_MAX_DROP_M),
                               delta=0.3, msg="under the deck, within the maximum excavation depth")
        self.assertAlmostEqual(float(out[1]), 10.0, delta=0.1, msg="back up past the ramp")
        # the ramp: no steeper than UNDER_GRADE, and no kinks (the grade changes smoothly)
        xs = np.arange(0.0, 200.0, 2.0)
        la, lo, _ = FRAME.enu_to_geodetic(xs, np.zeros(len(xs)), 0.0)
        z = g.apply(la, lo, np.full(len(xs), 10.0))
        grade = np.diff(z) / 2.0
        self.assertLessEqual(float(np.abs(grade).max()), grading.UNDER_MAX_GRADE + 0.005)
        self.assertLess(float(np.abs(np.diff(grade)).max()), 0.01, "no kink")
        self.assertAlmostEqual(float(out[2]), 10.0, delta=0.1, msg="another road untouched")


class UnderDipTest(unittest.TestCase):
    def test_a_dip_ends_inside_its_road_part(self):
        d = np.arange(-80.0, 300.1, 1.0)
        free = grading.under_dip(d, 4.0)
        self.assertAlmostEqual(float(free[d == 0][0]), -4.0)
        self.assertLess(float(free[d == -80][0]), -0.5, "with room the ramp runs on past 80 m")
        fit = grading.under_dip(d, 4.0, (80.0, 300.0))
        self.assertAlmostEqual(float(fit[d == -80][0]), 0.0, places=9, msg="the part's start: back up")
        self.assertAlmostEqual(float(fit[d == 0][0]), -4.0)
        grade = np.abs(np.diff(fit[d <= 0]))
        self.assertLessEqual(float(grade.max()), grading.UNDER_MAX_GRADE + 1e-6)
        self.assertTrue(np.allclose(fit[d > 0], free[d > 0]), "the other side as before")
        self.assertIsNone(grading.under_dip(d, 4.0, (20.0, 300.0)), "no room even at UNDER_MAX_GRADE")


class UnderpassShortfallTest(unittest.TestCase):
    def test_road_under_gives_way_for_what_the_ramps_could_not_raise(self):
        """A local road's bridge over a motorway (the motorway never rises): with room for its ramps the overpass rises
        and the motorway stays; with frontage roads hugging both ends (the ramps held down) the motorway dips under it
        instead (BR-381 near Pouso Alegre: ~1 m of clearance, the AI car drove into the abutment)."""
        mway = [(-300, 0), (300, 0)]
        bridge = [(0, -20), (0, 20)]
        ramps = [[(0, -260), (0, -20)], [(0, 20), (0, 260)]]
        frontage = [[(-150, -26), (150, -26)], [(-150, 26), (150, 26)]]

        def motorway_z(lines, classes):
            f = features(lines, classes, flags=[(1, "is_bridge")])
            g = grading.Grader(FRAME, FakeMD(f), lambda la, lo: np.full(np.shape(la), 10.0))
            la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0, 250.0]), np.array([0.0, 0.0]), 0.0)
            return g.apply(la, lo, np.full(2, 10.0))
        free = motorway_z([mway, bridge] + ramps, ["motorway", "tertiary", "tertiary", "tertiary"])
        self.assertAlmostEqual(float(free[0]), 10.0, delta=0.3, msg="the overpass rises: the motorway stays")
        held = motorway_z([mway, bridge] + ramps + frontage, ["motorway", "tertiary", "tertiary", "tertiary",
                                                               "residential", "residential"])
        self.assertLess(float(held[0]), 10.0 - 0.6 * grading.UNDER_CLEAR_M, "ramps held down: the motorway dips")
        self.assertAlmostEqual(float(held[1]), 10.0, delta=0.1, msg="back up past its ramp")
        with unittest.mock.patch.object(grading, "UNDER_ANY_ON", False):
            off = motorway_z([mway, bridge] + ramps + frontage, ["motorway", "tertiary", "tertiary", "tertiary",
                                                                  "residential", "residential"])
        self.assertAlmostEqual(float(off[0]), 10.0, delta=0.3, msg="BNE_UNDERPASS_ANY=off: as before")


class RacewayKeepTest(unittest.TestCase):
    def test_track_surface_is_the_terrain(self):
        """A road crossing a racetrack at grade: on the track (its carriageway + margin) the terrain stays as it is
        (a banked oval's slope), off it the road is graded as usual."""
        f = features([[(-200, 0), (200, 0)], [(0, -100), (0, 100)]], ["raceway", "residential"])

        def sloped(la, lo):         # a banking: the ground rising to the north
            _e, n, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
            return 10.0 + 0.3 * np.asarray(n)
        g = grading.Grader(FRAME, FakeMD(f), sloped)
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0, 0.0]), np.array([3.0, 60.0]), 0.0)
        base = sloped(la, lo)
        out = g.apply(la, lo, base)
        self.assertAlmostEqual(float(out[0]), float(base[0]), places=6, msg="on the track: the terrain")



class NeverRaisedTest(unittest.TestCase):
    def test_links_are_raised_motorways_not(self):
        S = roads.Spec
        self.assertTrue(grading._never_raised(S(18.8, "paved", False, "motorway", True)))
        self.assertFalse(grading._never_raised(S(11.6, "paved", False, "motorway", True, link=True)))
        self.assertFalse(grading._never_raised(S(8.0, "paved", False, "secondary", False)))


if __name__ == "__main__":
    unittest.main()
