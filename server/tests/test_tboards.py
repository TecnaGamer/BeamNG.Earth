"""tboards (fork): Australia's T-junction chevron boards: where they stand."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import guardrails  # noqa: E402
import roads  # noqa: E402
import tboards  # noqa: E402


class Frame:
    enu_to_geodetic = staticmethod(lambda x, y, z: (y, x, z))
    geodetic_to_enu = staticmethod(lambda la, lo, h: (lo, la, h))


BOX = (-5000.0, -5000.0, 5000.0, 5000.0)
SPEC = roads.Spec(9.0, "paved", False, "secondary", False)._replace(kmh=100.0, sides=())


def line(a, b, step=10.0):
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    n = max(int(np.hypot(*(b - a)) / step), 1)
    t = np.linspace(0.0, 1.0, n + 1)[:, None]
    xy = a + t * (b - a)
    return np.c_[xy, np.zeros(len(xy))]


def rows(near, country="AU", box=BOX):
    got = tboards.tile_rows(Frame, near, box, lambda la, lo: la * 0 + 5.0, country)
    return np.array(got).reshape(-1, 17)


def t_junction(angle=90.0, spec_a=SPEC, spec_b=SPEC):
    """B along X through the origin (-300..300); A comes in from the south at the angle to B, ending on B at the origin."""
    a = np.radians(angle)
    start = -200.0 * np.array([np.cos(a), np.sin(a)])
    return [("b", spec_b, line((-300.0, 0.0), (300.0, 0.0)), 12.0), ("a", spec_a, line(start, (0.0, 0.0)), 12.0)]


class TBoardTest(unittest.TestCase):
    def test_a_t_has_one_board_on_the_far_side_facing_the_driver(self):
        r = rows(t_junction())
        self.assertEqual(len(r), 1)
        self.assertEqual(guardrails.GROUPS[int(r[0, 0])], "au_tboard")
        self.assertAlmostEqual(r[0, 1], 0.0, places=2)
        self.assertAlmostEqual(r[0, 2], 4.5 + tboards.FAR_M, places=2)      # (B's half width + 3 m past A's end)
        self.assertAlmostEqual(r[0, 6], tboards.HEIGHT_M)
        # A runs north (+Y) to the T: the board faces back down it, the traffic's direction in the row's local Y
        self.assertEqual(np.round(r[0, 11:13], 3).tolist(), [0.0, 1.0])
        self.assertEqual(np.round(r[0, 8:10], 3).tolist(), [1.0, 0.0])

    def test_the_board_is_on_the_far_side_whichever_way_a_runs(self):
        flipped = [(f, s, p * np.array([1.0, -1.0, 1.0]), w) for f, s, p, w in t_junction()]
        r = rows(flipped)
        self.assertEqual(len(r), 1)
        self.assertAlmostEqual(r[0, 2], -(4.5 + tboards.FAR_M), places=2)

    def test_a_road_digitised_the_other_way_round_still_gets_one(self):
        near = t_junction()
        near[1] = (near[1][0], near[1][1], near[1][2][::-1], 12.0)
        self.assertEqual(len(rows(near)), 1)

    def test_a_cross_road_has_none(self):
        near = t_junction() + [("c", SPEC, line((0.0, 0.0), (0.0, 200.0)), 12.0)]
        self.assertEqual(len(rows(near)), 0, "a road leaves the far side")
        near = t_junction() + [("c", SPEC, line((0.0, -4.0), (0.0, 200.0)), 12.0)]
        self.assertEqual(len(rows(near)), 0, "(and one running through, too)")

    def test_a_town_street_has_none(self):
        self.assertEqual(len(rows(t_junction(spec_a=SPEC._replace(urban=True)))), 0)
        self.assertEqual(len(rows(t_junction(spec_b=SPEC._replace(urban=True)))), 0)

    def test_other_countries_have_none(self):
        self.assertEqual(len(rows(t_junction(), country="SE")), 0)
        self.assertEqual(len(rows(t_junction(), country=None)), 0)

    def test_a_45_degree_join_has_none_and_a_70_degree_one_has_one(self):
        self.assertEqual(len(rows(t_junction(45.0))), 0)
        self.assertEqual(len(rows(t_junction(70.0))), 1)

    def test_a_motorway_or_a_residential_road_has_none(self):
        self.assertEqual(len(rows(t_junction(spec_a=SPEC._replace(cls="motorway")))), 0)
        self.assertEqual(len(rows(t_junction(spec_b=SPEC._replace(cls="residential")))), 0)

    def test_where_the_through_road_is_two_features_it_is_still_a_t(self):
        a = t_junction()[1]
        near = [("b1", SPEC, line((-300.0, 0.0), (0.0, 0.0)), 12.0), ("b2", SPEC, line((0.0, 0.0), (300.0, 0.0)), 12.0), a]
        self.assertEqual(len(rows(near)), 1)
        near = [near[0], ("b2", SPEC, line((0.0, 0.0), (300.0, 300.0)), 12.0), a]
        self.assertEqual(len(rows(near)), 0, "three roads meeting at a bend: not a through road")

    def test_an_end_to_end_join_has_none(self):
        near = [("b", SPEC, line((-300.0, 0.0), (0.0, 0.0)), 12.0), ("a", SPEC, line((0.0, 0.0), (0.0, -200.0)), 12.0)]
        self.assertEqual(len(rows(near)), 0)

    def test_a_one_way_road_leaving_the_t_has_none(self):
        self.assertEqual(len(rows(t_junction(spec_a=SPEC._replace(oneway=True, dir=1)))), 1)
        self.assertEqual(len(rows(t_junction(spec_a=SPEC._replace(oneway=True, dir=-1)))), 0)

    def test_the_tile_that_owns_the_point_keeps_it(self):
        self.assertEqual(len(rows(t_junction(), box=(-100.0, 0.0, 100.0, 100.0))), 1)
        self.assertEqual(len(rows(t_junction(), box=(-100.0, -100.0, 100.0, 0.0))), 0)

    def test_a_board_on_another_road_is_skipped(self):
        near = t_junction() + [("d", SPEC, line((-300.0, 8.0), (300.0, 8.0)), 12.0)]      # (a road just past B)
        self.assertEqual(len(rows(near)), 0)


if __name__ == "__main__":
    unittest.main()
