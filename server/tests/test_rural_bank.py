"""BNE_RURAL_BANK (country roads take the real road's cross slope): grading.bench_lean / rural_lean / bank_design /
bank and plan_roads with the switch on and off."""
import math
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import geodesy  # noqa: E402
import grading  # noqa: E402
import roads  # noqa: E402


def spec(cls, urban=False, **kw):
    return roads.Spec(7.0, "paved", urban, cls, **kw)


def bend(radius, angle_deg=90.0, lead=150.0, step=2.0):
    """A straight lead, a circular bend of radius (m) through angle_deg to the left, a straight lead: (n, 2)."""
    pts = [(x, 0.0) for x in np.arange(-lead, 0.0, step)]
    a = math.radians(angle_deg)
    for t in np.arange(0.0, a, step / radius):
        pts.append((radius * math.sin(t), radius * (1.0 - math.cos(t))))
    end = np.array([radius * math.sin(a), radius * (1.0 - math.cos(a))])
    d = np.array([math.cos(a), math.sin(a)])
    for s in np.arange(step, lead, step):
        pts.append(tuple(end + d * s))
    return np.asarray(pts, np.float64)


class RuralBankTest(unittest.TestCase):
    def test_off_no_bank_below_trunk(self):
        with mock.patch.object(grading, "RURAL_BANK_ON", False):
            self.assertIsNone(grading.bank_design(spec("tertiary")))
            self.assertIsNone(grading.bank(bend(60.0), spec("tertiary"), ext=(1, 1)))
            self.assertIsNotNone(grading.bank_design(spec("motorway")))

    def test_motorway_design_unchanged(self):
        for on in (False, True):
            with mock.patch.object(grading, "RURAL_BANK_ON", on):
                e, r_min, st, ro, fa, sg = grading.bank_design(spec("motorway"))
                self.assertEqual((e, st, ro, fa, sg), (grading.BANK_E_MAX, grading.BANK_STENCIL_M,
                                                       grading.BANK_RUNOFF_M, grading.BANK_FADE_M,
                                                       grading.BANK_SIGMA_M))
                self.assertAlmostEqual(r_min, 110.0 ** 2 / (127.0 * 0.18))

    def test_on_tight_country_bend_leans_in(self):
        with mock.patch.object(grading, "RURAL_BANK_ON", True):
            b = grading.bank(bend(40.0), spec("tertiary"), ext=(1, 1))
        self.assertIsNotNone(b)
        self.assertLess(float(b.min()), -0.05)                # (a left-hand bend: grading's sign, the I-80 test's)
        self.assertLessEqual(float(b.max()), 1e-9)            # one way only
        self.assertLessEqual(float(np.abs(b).max()), grading.RURAL_BANK_E_MAX + 1e-9)
        self.assertLess(abs(float(b[5])), 0.01)               # level on the straight lead

    def test_gentle_bend_leans_less(self):
        with mock.patch.object(grading, "RURAL_BANK_ON", True):
            tight = grading.bank(bend(40.0), spec("tertiary"), ext=(1, 1))
            wide = grading.bank(bend(400.0, 40.0), spec("tertiary"), ext=(1, 1))
        self.assertLess(float(np.abs(wide).max()) if wide is not None else 0.0, float(np.abs(tight).max()) / 2)

    def test_speed_limit_sets_design_speed(self):
        with mock.patch.object(grading, "RURAL_BANK_ON", True):
            slow = grading.bank_design(spec("tertiary", kmh=40.0))[1]
            fast_ = grading.bank_design(spec("tertiary", kmh=90.0))[1]
        self.assertLess(slow, fast_)

    def test_on_not_town_link_or_mountain(self):
        with mock.patch.object(grading, "RURAL_BANK_ON", True):
            self.assertIsNone(grading.bank_design(spec("tertiary", urban=True)))
            self.assertIsNone(grading.bank_design(spec("tertiary", link=True)))
            self.assertIsNone(grading.bank_design(spec("tertiary", mountain=True)))
            self.assertIsNone(grading.bank_design(spec("service")))


def cross(x, tilt, hill, at=0.0, half=2.5):
    """The elevation x m to the left of a line whose real road lies `at` m to its left: a carriageway half m either side
    of its middle rising `tilt` m per m to the left, the hillside past both edges rising `hill` m per m to the left."""
    d = np.asarray(x, np.float64) - at
    return np.where(np.abs(d) <= half, tilt * d, tilt * half * np.sign(d) + hill * (d - half * np.sign(d)))


class BenchLeanTest(unittest.TestCase):
    """grading.bench_lean: the real road's cross slope in the elevation across the map line (RURAL_BENCH_*)."""

    def lean(self, tilt, hill, at=0.0, noise=0.0, **kw):
        z = cross(grading.bench_offsets(), tilt, hill, at, **kw)[None, :].repeat(4, axis=0)
        if noise:
            z = z + np.random.default_rng(7).normal(0.0, noise, z.shape)
        return grading.bench_lean(z)

    def test_bench_under_the_line_in_a_hillside(self):
        np.testing.assert_allclose(self.lean(0.02, 0.30), 0.02, atol=1e-9)
        np.testing.assert_allclose(self.lean(-0.04, 0.30), -0.04, atol=1e-9)    # (adverse: out of the hillside)

    def test_bench_beside_the_line(self):
        # the map line 2 m off the real road: a reading across the line itself spans the cut bank
        for at in (-2.0, 2.0):
            np.testing.assert_allclose(self.lean(-0.015, 0.30, at), -0.015, atol=1e-9)
            x = np.array([-2.35, 2.35])
            z = cross(x, -0.015, 0.30, at)
            self.assertGreater((z[1] - z[0]) / 4.7, 0.05)

    def test_level_road_reads_level(self):
        np.testing.assert_allclose(self.lean(0.0, 0.45, 1.0), 0.0, atol=1e-9)

    def test_banked_road_on_level_land(self):
        # (the strips half on the level land beside it are the more level ones: bent, they lose to the carriageway)
        np.testing.assert_allclose(self.lean(0.06, 0.0), 0.06, atol=1e-9)
        np.testing.assert_allclose(self.lean(-0.06, 0.0, at=1.0, half=3.5), -0.06, atol=1e-9)

    def test_open_slope_is_no_road(self):
        self.assertTrue(np.isnan(self.lean(0.06, 0.06)).all())          # (leans as the land round it: no strip shows)
        self.assertTrue(np.isnan(self.lean(0.2, 0.2)).all())            # (steeper than any carriageway)
        np.testing.assert_allclose(self.lean(0.02, 0.02), 0.02, atol=1e-9)      # (near level: believed)

    def test_road_past_the_reach_is_not_guessed(self):
        # the real road 6 m off the line: the strips in reach lie on the 8 % fill
        self.assertTrue(np.isnan(self.lean(0.0, 0.08, 6.0, half=1.5)).all())

    def test_rough_ground_is_no_road(self):
        self.assertTrue(np.isnan(self.lean(0.0, 0.0, noise=0.10)).all())

    def test_lidar_noise_is_a_road(self):
        got = self.lean(0.03, 0.30, noise=0.005)
        self.assertTrue(np.isfinite(got).all())
        np.testing.assert_allclose(got, 0.03, atol=0.012)

    def test_unread_elevation(self):
        z = np.full((2, len(grading.bench_offsets())), np.nan)
        self.assertTrue(np.isnan(grading.bench_lean(z)).all())

    def test_any_step_across(self):
        for step in (0.5, 0.75, 1.0):
            z = cross(grading.bench_offsets(step), -0.02, 0.30, 2.0)[None, :]
            np.testing.assert_allclose(grading.bench_lean(z, step), -0.02, atol=1e-9)


class RuralLeanTest(unittest.TestCase):
    """grading.rural_lean: the strips' slopes along the road (median, Gaussian, fades)."""

    def test_steady_lean_kept(self):
        np.testing.assert_allclose(grading.rural_lean(np.full(100, 0.03), 2.0, (False, False)), 0.03, atol=1e-9)

    def test_no_strip_is_level(self):
        np.testing.assert_allclose(grading.rural_lean(np.full(100, np.nan), 2.0, (False, False)), 0.0, atol=1e-12)

    def test_lone_reading_dropped(self):
        lean = np.zeros(100)
        lean[50] = 0.08                                                 # (a driveway's apron)
        self.assertLess(float(np.abs(grading.rural_lean(lean, 2.0, (False, False))).max()), 1e-9)

    def test_held_within_the_most(self):
        lt = grading.rural_lean(np.full(100, 0.2), 2.0, (False, False))
        self.assertLessEqual(float(lt.max()), grading.RURAL_LEAN_MAX + 1e-9)

    def test_fades_level_at_a_flagged_end(self):
        lt = grading.rural_lean(np.full(100, 0.03), 2.0, (True, False))
        self.assertEqual(float(lt[0]), 0.0)
        self.assertAlmostEqual(float(lt[-1]), 0.03)
        self.assertAlmostEqual(float(lt[int(grading.RURAL_BANK_FADE_M / 2.0) + 1]), 0.03)

    def test_road_going_on_smoothed_in_then_dropped(self):
        lean = np.r_[np.full(20, 0.06), np.full(100, 0.0), np.full(10, 0.06)]
        lt = grading.rural_lean(lean, 2.0, (False, False), (20, 10))
        self.assertEqual(len(lt), 100)
        self.assertAlmostEqual(float(lt[0]), 0.03, places=2)            # (half way between the two at the join)
        self.assertLess(abs(float(lt[50])), 1e-3)


class TerrainLeanTest(unittest.TestCase):
    """plan_roads: a country road takes the real road's cross slope where the elevation shows it (benches), the design
    rule's share where it shows no roads."""
    FRAME = geodesy.EnuFrame(39.66, -79.92)
    EAST = np.c_[np.arange(-300.0, 300.0, 2.0), np.zeros(300)]         # due east: its left is north

    def plan(self, elev, cls="tertiary", xy=None, benches=True, on=True):
        xy = bend(40.0) if xy is None else xy
        la, lo, _ = self.FRAME.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        with mock.patch.object(grading, "RURAL_BANK_ON", on):
            return grading.plan_roads(self.FRAME, [(np.c_[lo, la], spec(cls))], elev, cuts=False, benches=benches)[0]

    def land(self, tilt, hill, at=0.0):
        """cross() of the frame's north: the real road of a line due east."""
        def elev(la, lo):
            _e, n, _u = self.FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
            return 100.0 + cross(np.asarray(n), tilt, hill, at)
        return elev

    def mid(self, p):
        return p.bank[len(p.bank) // 3: 2 * len(p.bank) // 3]

    def test_takes_the_real_roads_lean(self):
        p = self.plan(self.land(0.025, 0.30), xy=self.EAST)
        np.testing.assert_allclose(self.mid(p), 0.025, atol=1e-3)

    def test_takes_it_from_a_road_beside_the_line(self):
        p = self.plan(self.land(-0.02, 0.30, at=-2.0), xy=self.EAST)
        np.testing.assert_allclose(self.mid(p), -0.02, atol=1e-3)

    def test_level_real_road_in_a_hillside_stays_level(self):
        self.assertIsNone(self.plan(self.land(0.0, 0.30, at=2.0), xy=self.EAST).bank)

    def test_open_hillside_does_not_lean_the_road(self):
        self.assertIsNone(self.plan(self.land(0.06, 0.06), xy=self.EAST).bank)

    def test_measured_level_bend_gets_no_made_up_lean(self):
        self.assertIsNone(self.plan(self.land(0.0, 0.0)).bank)

    def test_bend_where_no_road_shows_is_level_not_ruled(self):
        # (lidar with no strip anywhere: a slope of 20 % or more across the road all round the bend. Level; the design
        # rule stands in only where the elevation shows no roads at all, benches False)
        def steep(la, lo):
            e, n, _u = self.FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
            return 100.0 + 0.2 * np.asarray(n) - 0.2 * np.asarray(e)
        self.assertIsNone(self.plan(steep).bank)
        self.assertIsNotNone(self.plan(steep, benches=False).bank)

    def test_no_roads_in_the_elevation_share_of_the_rule(self):
        p = self.plan(self.land(0.0, 0.0), benches=False)
        want = grading.RURAL_CURVE_SHARE["tertiary"] * grading.RURAL_BANK_E_MAX
        self.assertAlmostEqual(float(-p.bank.min()), want, places=2)
        self.assertLessEqual(float(p.bank.max()), 1e-9)                 # (a left-hand bend: in, one way only)
        self.assertIsNone(self.plan(self.land(0.0, 0.0), cls="residential", benches=False).bank)

    def test_read_at_every_other_sample_or_at_each(self):
        got = []
        for every in (1, grading.RURAL_BENCH_EVERY):
            with mock.patch.object(grading, "RURAL_BENCH_EVERY", every):
                got.append(self.mid(self.plan(self.land(0.025, 0.30, at=1.0), xy=self.EAST)))
        np.testing.assert_allclose(got[0], 0.025, atol=1e-3)
        np.testing.assert_allclose(got[1], got[0], atol=1e-6)

    def test_fades_level_at_a_loose_end(self):
        p = self.plan(self.land(0.03, 0.30), xy=self.EAST)
        self.assertEqual(float(p.bank[0]), 0.0)
        self.assertEqual(float(p.bank[-1]), 0.0)

    def test_off_plans_level_across(self):
        self.assertIsNone(self.plan(self.land(0.03, 0.30), xy=self.EAST, on=False).bank)
        self.assertIsNone(self.plan(self.land(0.0, 0.0), benches=False, on=False).bank)


if __name__ == "__main__":
    unittest.main()
