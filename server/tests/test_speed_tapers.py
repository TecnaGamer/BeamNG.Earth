"""Speed tapers (roads.SPEED_TAPERS, BNE_SPEED_TAPERS; the road revamp's R1, 2026-11-16, plan §2.3): a width change
runs over the length the standards ask at the road's design speed (MUTCD's W·V·min(V/155, 0.62), at least the quintic
shape's 0.5 m/s² lateral acceleration length, 20-400 m), measured along the road, eased by the quintic smootherstep;
off, the 12:1 tapers of at most 90 m as before."""
import math
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import roadlanes  # noqa: E402
import roads  # noqa: E402
from test_bridge_scenarios import chain  # noqa: E402
from test_roads import FRAME, features  # noqa: E402


def std_len(w, v):
    """The plan's formula, written out here (the kernel's own: tapers::speed_taper_m)."""
    vv = v / 3.6
    return float(np.clip(max(w * v * min(v / 155.0, 0.62), math.sqrt(5.77 * w * vv * vv / 0.5)), 20.0, 400.0))


def fresh(f):
    roadlanes.invalidate(f)
    f.__dict__.pop("_road_tapers", None)
    f.__dict__.pop("_deck_tapers", None)
    return f


def width_at(f, i, x, y=0.0):
    sp = roads.road_specs(f)[i]
    la, lo, _ = FRAME.enu_to_geodetic(np.array([float(x)]), np.array([float(y)]), 0.0)
    ws = roads.taper_widths(sp.width, roads.road_tapers(f)[i], lo, la)
    return sp.width if ws is None else float(ws[0])


class SpeedTaperLengthTest(unittest.TestCase):
    def test_the_plans_table(self):
        # (plan §2.3: a 3.6 m lane over 54 m at 30 km/h ... 268 m at 120 km/h)
        for v, want in ((30, 54), (50, 90), (70, 125), (90, 188), (100, 223), (120, 268)):
            self.assertAlmostEqual(std_len(3.6, v), want, delta=1.0, msg=f"{v} km/h")

    def test_off_by_default_as_before(self):
        self.assertFalse(roads.SPEED_TAPERS)
        f = fresh(chain([10.0, 16.0], classes=["trunk", "trunk"]))
        t = roads.road_tapers(f)[0]
        self.assertEqual(len(t), 1)
        self.assertEqual(len(t[0]), 4)
        self.assertAlmostEqual(t[0][3], float(np.clip(6.0 * roads.TAPER_M_PER_M, *roads.TAPER_M)), places=6)

    def test_a_trunk_road_tapers_at_its_speed(self):
        f = chain([10.0, 16.0], classes=["trunk", "trunk"])
        f.attrs["class"] = np.array(["trunk", "trunk"])
        with mock.patch.object(roads, "SPEED_TAPERS", True):
            fresh(f)
            sp = roads.road_specs(f)[0]
            v = 70.0 if sp.urban else 100.0
            t = roads.road_tapers(f)[0]
            self.assertEqual(len(t), 1)
            want = min(std_len(3.0, v), roads.TAPER_SHARE * 200.0)      # (each side: half of the 6 m change)
            self.assertAlmostEqual(t[0][3], want, delta=0.01)
            self.assertGreater(t[0][3], 43.0, "longer than the 12:1 taper")
            self.assertAlmostEqual(width_at(f, 0, 0.0), 13.0, delta=0.05, msg="half way at the joint")
            self.assertAlmostEqual(width_at(f, 0, -t[0][3] - 1.0), 10.0, delta=0.01, msg="its own width past it")
            # the quintic: at u = 0.5 half of the change, and flat at both ends
            self.assertAlmostEqual(width_at(f, 0, -0.5 * t[0][3]), 11.5, delta=0.05)
            ws = [width_at(f, 0, x) for x in np.linspace(-t[0][3], 0.0, 41)]
            self.assertTrue(np.all(np.diff(ws) >= -1e-9), "widening steadily")
        fresh(f)

    def test_distance_along_the_road_not_straight(self):
        # a road curving over a quarter circle (r 60 m) into a wider one going on straight: a point on the curve is
        # farther from the joint along the road than in a straight line, and its width follows the distance along it
        r = 60.0
        arc = [(r * math.cos(a), r * math.sin(a)) for a in np.linspace(math.pi / 2, 0.0, 91)]
        lines = [[(-200.0, r)] + arc, [(r, 0.0), (r, -300.0)]]
        f = features(lines, ["trunk", "trunk"], widths=[(0, 10.0), (1, 16.0)], ids=["a", "b"])
        f.attrs["name"] = np.array(["", ""])
        with mock.patch.object(roads, "SPEED_TAPERS", True):
            fresh(f)
            t = roads.road_tapers(f)[0]
            self.assertEqual(len(t), 1)
            span = t[0][3]

            def quintic_width(d):
                u = min(d / span, 1.0)
                return 10.0 + 3.0 * (1.0 - (10 * u ** 3 - 15 * u ** 4 + 6 * u ** 5))

            a = 0.45 * math.pi
            px, py = r * math.cos(a), r * math.sin(a)
            s_arc, chord = r * a, 2.0 * r * math.sin(a / 2.0)
            self.assertGreater(abs(quintic_width(s_arc) - quintic_width(chord)), 0.05, "the test tells them apart")
            got = width_at(f, 0, px, py)
            self.assertAlmostEqual(got, quintic_width(s_arc), delta=0.02, msg="by its distance along the road")
        fresh(f)

if __name__ == "__main__":
    unittest.main()
