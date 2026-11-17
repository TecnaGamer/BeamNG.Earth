"""The road laws on every road (roadlaws.apply): a sudden change of grade becomes a curve the road's speed allows, a
side road's end stays on the road it joins, the ground's own hills stay, what the passes solved is held."""
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
        self.assertGreater(before[0], 3.0 / rad_j)
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


if __name__ == "__main__":
    unittest.main()
