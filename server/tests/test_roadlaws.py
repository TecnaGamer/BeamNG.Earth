"""The road laws on every road (roadlaws.apply): a sudden change of grade becomes a curve the road's speed allows, a
side road's end stays on the road it joins, the ground's own hills stay, what the passes solved is held."""
import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import grading  # noqa: E402
import roadlaws  # noqa: E402


def plan(xy, z, half=3.0, abut=(False, False)):
    xy = np.asarray(xy, np.float64)
    return grading.Plan(xy, np.asarray(z, np.float64), half, abut, None, half)


def spec(cls, kmh=None):
    return SimpleNamespace(cls=cls, kmh=kmh)


def bends(p, beyond=0.0):
    """The largest change of grade per metre along plan p (1 / its tightest vertical curve), from beyond m on."""
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(np.asarray(p.xy)[:, :2], axis=0).T))]
    g = np.diff(p.z) / np.diff(s)
    b = np.abs(np.diff(g)) / ((s[2:] - s[:-2]) / 2)
    return float(np.max(b[s[1:-1] >= beyond]))


def sag(p, chord=12.0):
    """m the ground stands over the straight line between any two points of p chord m apart (a truck's length)."""
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(np.asarray(p.xy)[:, :2], axis=0).T))]
    fine = np.arange(0.0, s[-1], 0.5)
    z = np.interp(fine, s, p.z)
    n = int(round(chord / 0.5))
    worst = 0.0
    for a in range(len(fine) - n):
        line = np.linspace(z[a], z[a + n], n + 1)
        worst = max(worst, float((z[a:a + n + 1] - line).max()))
    return worst


class SagTest(unittest.TestCase):
    def test_a_side_road_flat_at_its_junction_then_up_a_bank_is_eased(self):
        # (the main road along y = 0 at 100 m; the side road leaves it north, held flat 8 m then 20 % up its bank)
        x = np.arange(-200.0, 201.0, 4.0)
        main = plan(np.c_[x, 0 * x], 100.0 + 0 * x, half=4.0)
        y = np.arange(0.0, 121.0, 4.0)
        zs = 100.0 + np.where(y < 8.0, 0.0, 0.2 * (y - 8.0))
        zs = np.minimum(zs, 100.0 + 0.2 * 60.0)                     # (and flat again on top)
        side = plan(np.c_[0 * y, y], zs)
        before = bends(side), sag(side)
        n = roadlaws.apply([main, side], [spec("primary"), spec("unclassified")])
        self.assertGreaterEqual(n, 1)
        _g, rad = roadlaws.laws(spec("unclassified"))
        rad_j = roadlaws.laws(spec("road", roadlaws.JUNCTION_KMH))[1]
        self.assertGreater(before[0], 2.5 / rad_j)
        self.assertGreater(before[1], 0.5, "(the case: a 12 m truck bottoms out)")
        self.assertLess(bends(side), 1.3 / rad_j, "a junction's curve near it")
        self.assertLess(bends(side, beyond=roadlaws.APPROACH_M + 8.0), 1.3 / rad, "its own speed's past the approach")
        self.assertLess(sag(side), 0.5, "a 12 m truck clears it")
        self.assertAlmostEqual(float(side.z[0]), float(np.interp(0.0, x, main.z)), delta=0.05)
        self.assertLess(abs(float(side.z[1] - side.z[0])) / 4.0, roadlaws.LANDING_TOL + 0.005, "lands on the flat main")

    def test_a_steep_road_keeps_its_hill(self):
        s = np.arange(0.0, 401.0, 4.0)
        z = 100.0 + 0.18 * s                                        # (a real 18 % road, steeper than its law)
        p = plan(np.c_[s, 0 * s], z)
        roadlaws.apply([p], [spec("residential")])
        self.assertLess(float(np.abs(p.z - z).max()), 0.05)


class HoldTest(unittest.TestCase):
    def test_held_points_and_a_road_under_a_deck(self):
        s = np.arange(0.0, 201.0, 4.0)
        z = 100.0 + np.where(s < 100.0, 0.0, 2.0)                   # (a 2 m step at 100 m)
        z0 = z.copy()
        p = plan(np.c_[s, 0 * s], z)
        held = np.abs(s - 40.0) <= 4.0
        under = np.abs(s - 104.0) <= 8.0                            # (under a deck past the step: only down)
        roadlaws.apply([p], [spec("tertiary")], held=lambda i: held, under=lambda i: under)
        self.assertTrue(np.allclose(p.z[held], z0[held]))
        self.assertTrue((p.z[under] <= z0[under] + 1e-9).all())
        self.assertLess(float(np.abs(np.diff(p.z)).max()), 1.0)    # (the step eased, not kept)

    def test_abutment_end_held(self):
        s = np.arange(0.0, 101.0, 4.0)
        z = 100.0 + np.where(s < 8.0, 3.0, 0.0)
        p = plan(np.c_[s, 0 * s], z, abut=(True, False))
        roadlaws.apply([p], [spec("secondary")])
        self.assertAlmostEqual(float(p.z[0]), 103.0, delta=1e-6)


class JointTest(unittest.TestCase):
    def test_two_pieces_of_one_road_have_no_kink_at_their_joint(self):
        s1 = np.arange(0.0, 101.0, 4.0)
        s2 = np.arange(100.0, 201.0, 4.0)
        a = plan(np.c_[s1, 0 * s1], 100.0 + 0 * s1)
        b = plan(np.c_[s2, 0 * s2], 100.0 + 0.12 * (s2 - 100.0))   # (flat, then 12 % from the joint on)
        a.rid = b.rid = 7
        roadlaws.apply([a, b], [spec("tertiary"), spec("tertiary")])
        _g, rad = roadlaws.laws(spec("tertiary"))
        z = np.r_[a.z, b.z[1:]]
        s = np.r_[s1, s2[1:]]
        g = np.diff(z) / np.diff(s)
        self.assertLess(float(np.max(np.abs(np.diff(g)) / 4.0)), 1.3 / rad)
        self.assertAlmostEqual(float(a.z[-1]), float(b.z[0]), delta=0.01)

    def test_two_pieces_the_passes_left_apart_meet_at_their_joint(self):
        # (one road in two pieces, the second planned 2 m over the first's end at their joint (a pass's piece beside one
        # it left): both ends take the solved height there, not the first's change carried onto the second's own step)
        s1 = np.arange(0.0, 101.0, 4.0)
        s2 = np.arange(100.0, 201.0, 4.0)
        a = plan(np.c_[s1, 0 * s1], 100.0 + 0 * s1)
        b = plan(np.c_[s2, 0 * s2], 102.0 + 0 * s2)
        a.rid = b.rid = 7
        roadlaws.apply([a, b], [spec("tertiary"), spec("tertiary")])
        self.assertAlmostEqual(float(a.z[-1]), float(b.z[0]), delta=0.01)
        g = np.diff(np.r_[a.z, b.z[1:]]) / 4.0
        self.assertLess(float(np.abs(g).max()), roadlaws.laws(spec("tertiary"))[0] + 1e-3)


class LandingTest(unittest.TestCase):
    """A side road leaves the road it joins at that road's grade as solved, not as planned: a step in the planned road
    (a held shape's piece beside one at the ground, a pass's dip) is no grade a side road is held to."""

    @staticmethod
    def grade_at(p, x0):
        """The grade of plan p (along x) over its segment from x0 on."""
        x = np.asarray(p.xy)[:, 0]
        k = int(np.argmin(np.abs(x - x0)))
        return float((p.z[k + 1] - p.z[k]) / (x[k + 1] - x[k]))

    def test_a_side_road_leaving_a_step_in_the_road_it_joins(self):
        # (the main road along y = 0, planned with a 3 m step at x = 0 (a 37 % grade over its samples there); a
        # residential side road leaves it at x = 0, 30 degrees off its line: held to 0.87 x 37 % = 32 %, over its 10 %
        # law, its network had no answer and was left as it was)
        x = np.arange(-200.0, 201.0, 4.0)
        main = plan(np.c_[x, 0 * x], 100.0 + np.where(x < 0.0, 0.0, 3.0), half=4.0)
        u = np.array([math.cos(math.radians(30.0)), math.sin(math.radians(30.0))])
        t = np.arange(0.0, 81.0, 4.0)
        side = plan(t[:, None] * u[None, :], 103.0 + 0 * t)
        info = {}
        roadlaws.apply([main, side], [spec("primary"), spec("residential")], diagnostics=info)
        self.assertEqual(info["infeasible"], 0, info.get("failed"))
        self.assertAlmostEqual(float(side.z[0]), float(np.interp(0.0, x, main.z)), delta=0.02)
        g_side = float((side.z[1] - side.z[0]) / 4.0)
        g_main = self.grade_at(main, 0.0)
        self.assertLessEqual(abs(g_main), roadlaws.laws(spec("primary"))[0] + 1e-3, "the step a curve under its law")
        self.assertLess(abs(g_side - u[0] * g_main), roadlaws.LANDING_TOL + 0.005, "leaves at the solved grade its way")

    def test_a_short_road_between_two_roads_lands_on_both(self):
        # (two parallel roads 12 m apart, each planned with a step where a short link joins them obliquely: both ends of
        # the link landed on grades that, held as planned, no 20 km/h curve joins in 14 m)
        x = np.arange(-120.0, 121.0, 4.0)
        a = plan(np.c_[x, 0 * x], 100.0 + np.where(x < 0.0, 0.0, 1.5), half=3.0)
        b = plan(np.c_[x, 0 * x + 12.0], 100.0 + np.where(x < 8.0, 1.0, -0.5), half=3.0)
        link = plan(np.array([[0.0, 0.0], [8 / 3, 4.0], [16 / 3, 8.0], [8.0, 12.0]]), np.array([100.7, 100.6, 100.5, 100.3]))
        info = {}
        roadlaws.apply([a, b, link], [spec("tertiary"), spec("tertiary"), spec("residential")], diagnostics=info)
        self.assertEqual(info["infeasible"], 0, info.get("failed"))
        self.assertAlmostEqual(float(link.z[0]), float(np.interp(0.0, x, a.z)), delta=0.02)
        self.assertAlmostEqual(float(link.z[-1]), float(np.interp(8.0, x, b.z)), delta=0.02)


class ThreeSampleCouplingTest(unittest.TestCase):
    def test_a_height_between_two_samples_of_another_chain(self):
        import carriageways
        ch = lambda t: dict(t=np.asarray(t, np.float64), w=np.ones(len(t)), grade=1.0, K=0.01, lam=0.0, step=4.0,
                            length=0.0)
        # (z_a[0] - (1 - 0.25) z_b[1] - 0.25 z_b[2] = 0, and the same pulled apart by a band of +-0.1)
        for kind in ("eq", "band"):
            cps = [(0, 0, 1, 1, "eq", 0.0, 1, 2, 0.25)] if kind == "eq" else                 [(0, 0, 1, 1, "ge", -0.1, 1, 2, 0.25), (0, 0, 1, 1, "le", 0.1, 1, 2, 0.25)]
            info = {}
            z = carriageways.solve([ch([0.0, 0.0]), ch([10.0, 10.0, 14.0, 14.0])], cps, diagnostics=info)
            self.assertTrue(info["feasible"] and info["converged"], info)
            gap = float(z[0][0] - 0.75 * z[1][1] - 0.25 * z[1][2])
            if kind == "eq":
                self.assertAlmostEqual(gap, 0.0, delta=1e-5)
            else:
                self.assertAlmostEqual(gap, -0.1, delta=1e-4)     # (pulled down to it from 11 m: its lower side)


if __name__ == "__main__":
    unittest.main()
