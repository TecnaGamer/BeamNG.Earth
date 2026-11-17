"""Seams where two surfaces meet (plan 028, 2026-11-08): a carriageway road's abutment end is back at its deck's height
whatever the passes after the carriageway solve did to it, and the roads ending at the same abutment come down from it."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import roads  # noqa: E402


def spec(cls):
    return roads.Spec(*([None] * len(roads.Spec._fields)))._replace(
        width=9.6, kind="paved", urban=False, cls=cls, surf="asphalt", sides=0, walk=False, lanes=2, oneway=False,
        gap=0.0, median=0.0, link=False, dir=0, kmh=50)


class PinCwEndsTest(unittest.TestCase):
    DECK = 83.33

    def planner(self, profile_ok=True):
        g = object.__new__(grading._Planner)

        def cw(line, pts, fid=None):
            pts = np.asarray(pts, np.float64).reshape(-1, 2)
            return np.full(len(pts), self.DECK) if profile_ok else None
        g._cw_z_at = cw
        return g

    def plans(self):
        s = np.arange(0.0, 121.0, 4.0)
        trunk = grading.Plan(np.c_[s, np.zeros(len(s))], np.minimum(81.65 + 0.4 * s / 4, 83.34), 7.8, abut=(True, False))
        trunk.rid = 1
        street = grading.Plan(np.c_[np.zeros(14), np.linspace(60.0, 0.0, 14)] + [0.0, 0.0],
                              np.full(14, 81.65), 8.3, abut=(False, True))
        street.rid = 2
        return [("t", trunk), ("s", street)], [spec("trunk"), spec("residential")]

    def test_end_back_at_the_deck(self):
        plans, specs = self.plans()
        self.assertTrue(self.planner()._pin_cw_ends(plans, specs))
        z = np.asarray(plans[0][1].z)
        self.assertAlmostEqual(float(z[0]), self.DECK, places=6)
        self.assertLess(float(np.abs(np.diff(z)).max()), 0.1 * 4, "no step along the road")
        orig = self.plans()[0][0][1].z
        self.assertTrue(np.allclose(z[-10:], orig[-10:]), "the road far from the end is as it was")

    def test_street_at_the_abutment_comes_down_from_it(self):
        plans, specs = self.plans()
        self.planner()._pin_cw_ends(plans, specs)
        z = np.asarray(plans[1][1].z)
        self.assertAlmostEqual(float(z[-1]), self.DECK, places=6)       # (its last vertex is the abutment's)
        grade = np.abs(np.diff(z)).max() / (60.0 / 13)
        self.assertLessEqual(grade, 0.25)
        self.assertAlmostEqual(float(z[0]), 81.65, places=6)

    def test_nothing_to_do_when_it_already_meets(self):
        plans, specs = self.plans()
        plans[0][1].z[:] = self.DECK
        plans[1][1].z[:] = self.DECK
        self.assertFalse(self.planner()._pin_cw_ends(plans, specs))

    def test_no_profile_no_change(self):
        plans, specs = self.plans()
        self.assertFalse(self.planner(profile_ok=False)._pin_cw_ends(plans, specs))

    def test_off_switch(self):
        self.assertTrue(grading.SEAMS_ON)


class GradeMatchTest(unittest.TestCase):
    """_grade_match: a single span is not tilted more than SEAM_DECK_GRADE by the ground at its ends; the rest of the
    difference goes half to each approach, the lower end up, the higher down."""

    def planner(self, z0, z1, length=17.0, cls="secondary", chain=False):
        import mapcells
        g = object.__new__(grading._Planner)
        import threading
        g.lock = threading.Lock()
        import geodesy
        g.frame = geodesy.EnuFrame(50.5, 16.2)
        bridge = np.array([[0.0, 0.0], [length, 0.0]])
        g._cell_enu = lambda k, ground=True: {"ends": bridge, "end_of": [0, 0], "bridges": [bridge],
                                                "bridge_cls": [cls]}
        la, lo, _ = g.frame.enu_to_geodetic(bridge[:, 0], bridge[:, 1], 0.0)
        zs = {0: z0, 1: z1}

        def elev(a, b):
            a = np.asarray(a, np.float64)
            return np.where(np.abs(a - la[0]) < np.abs(a - la[1]), z0, z1) if abs(la[0] - la[1]) > 0 else np.full(a.shape, z0)
        g.elev = elev
        return g

    def test_steep_difference_is_shared(self):
        g = self.planner(398.1, 401.3)
        a = g._grade_match(np.array([0.0, 0.0]))
        b = g._grade_match(np.array([17.0, 0.0]))
        if a is None or b is None:
            self.skipTest("the stub's elevation does not tell the ends apart")
        self.assertGreater(a, 0.5)
        self.assertLess(b, -0.5)
        self.assertAlmostEqual(a, -b, places=6)

    def test_gentle_difference_is_left(self):
        g = self.planner(398.1, 398.4)
        self.assertIsNone(g._grade_match(np.array([0.0, 0.0])))

    def test_motorway_and_link_are_not_matched(self):
        for cls in ("motorway", "trunk", "primary_link"):
            self.assertIsNone(self.planner(398.1, 401.3, cls=cls)._grade_match(np.array([0.0, 0.0])), cls)


class HoldMouthTest(unittest.TestCase):
    """A street off a deck's abutment keeps the abutment's height over the junction's wedge, then goes back at 8 %."""

    def setUp(self):
        self.a = np.arange(0.0, 60.1, 3.0)                  # distance from the abutment
        self.z = 100.0 - 0.09 * np.minimum(self.a, 12.0) - 0.0 * self.a   # the street's own profile: falls 9 %, then level
        self.z = np.where(self.a > 12.0, 100.0 - 0.09 * 12.0, self.z)

    def test_held_then_back_at_grade(self):
        z = np.array(self.z)
        out = grading._Planner._hold_mouth(z, self.a)
        held = self.a <= grading.SEAM_MOUTH_M
        self.assertTrue(np.allclose(out[held], z[0]))
        g = np.abs(np.diff(out[~held | (self.a == self.a[held][-1])])) / 3.0
        self.assertLessEqual(g.max(), grading.SEAM_MOUTH_GRADE + 1e-9)
        self.assertAlmostEqual(out[-1], z[-1], places=6)    # (back on its own profile)

    def test_unreachable_profile_is_left(self):
        z = np.array(self.a * -0.5 + 100.0)                 # falls 50 %: the 8 % return never meets it
        out = grading._Planner._hold_mouth(z, self.a)
        self.assertTrue(np.array_equal(out, z))

    def test_short_street_is_left(self):
        a = np.array([0.0, 3.0])
        z = np.array([100.0, 99.0])
        self.assertTrue(np.array_equal(grading._Planner._hold_mouth(z, a), z))


class GradeMatchBankEndTest(unittest.TestCase):
    """_grade_match reads each end as its approach is laid (fill_abutment): a stream bridge whose map line ends down the
    bank on one side is not tilted by that end's low ground (Alsfeld, B 62 over a stream: 2.6 m over a 16 m deck)."""

    def planner(self, ground):
        import threading
        import geodesy
        g = object.__new__(grading._Planner)
        g.lock = threading.Lock()
        g.frame = geodesy.EnuFrame(50.7563, 9.2870)
        bridge = np.array([[0.0, 0.0], [16.0, 0.0]])
        g._cell_enu = lambda k, ground=True: {"ends": bridge, "end_of": [0, 0], "bridges": [bridge],
                                                "bridge_cls": ["primary"]}

        def elev(la, lo):
            x, _y, _ = g.frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), 0.0)
            return ground(np.asarray(x, np.float64))
        g.elev = elev
        return g

    @staticmethod
    def stream(x):
        # the road's flat 243.25 m, a stream's 3.5 m valley from x = -6 to 9 (its bed at x = 3): the bridge's west end
        # (x = 0) on the bank 3 m down, its east end (x = 16) on the flat
        return np.where((x > -6.0) & (x < 9.0), 243.25 - 3.5 * np.cos(np.clip((x - 3.0) / 6.0, -1, 1) * np.pi / 2), 243.25)

    def test_end_down_the_bank_is_not_matched(self):
        g = self.planner(self.stream)
        self.assertLess(float(self.stream(np.array([0.0]))[0]), 241.0)     # (the end's own ground: down the bank)
        self.assertIsNone(g._grade_match(np.array([0.0, 0.0])))
        self.assertIsNone(g._grade_match(np.array([16.0, 0.0])))

    def test_off_switch_brings_the_old_match_back(self):
        from unittest import mock
        with mock.patch.object(grading, "SEAM_FILLED_ON", False):
            g = self.planner(self.stream)
            a = g._grade_match(np.array([0.0, 0.0]))
            b = g._grade_match(np.array([16.0, 0.0]))
        self.assertGreater(a, 0.5)
        self.assertLess(b, -0.5)

    def test_real_step_between_the_ends_is_still_shared(self):
        # the ground really differs on either side (flat at 398.1 m to the west, 401.3 m to the east): matched as before
        g = self.planner(lambda x: np.where(x < 8.0, 398.1, 401.3))
        a = g._grade_match(np.array([0.0, 0.0]))
        b = g._grade_match(np.array([16.0, 0.0]))
        self.assertGreater(a, 0.5)
        self.assertLess(b, -0.5)
        self.assertAlmostEqual(a, -b, places=6)

    def test_an_end_on_a_hills_brow_is_not_filled_above_its_ground(self):
        # a track up a hill: the ground falls ever more steeply west of the bridge's lower end (x = 0) and rises past
        # its upper one; fill_abutment's line through the far slope would read the lower end 4 m over the higher one
        # (Riva del Garda's Pontera hill, test_drivability): matched on the ground at the ends, as with the switch off
        def hill(x):
            return np.where(x < 0.0, 195.0 + 0.2 * x - 0.008 * x * x, 195.0 + 0.2 * x)
        from unittest import mock
        g = self.planner(hill)
        line = np.array([[0.0, 0.0], [16.0, 0.0]])
        self.assertGreater(g._end_filled(line, False), hill(np.array([16.0]))[0] + 1.0)     # (the bare fill: over the top)
        self.assertAlmostEqual(g._end_filled(line, False, past=True), 195.0, places=3)
        a = g._grade_match(np.array([0.0, 0.0]))
        b = g._grade_match(np.array([16.0, 0.0]))
        with mock.patch.object(grading, "SEAM_FILLED_ON", False):
            g = self.planner(hill)
            self.assertEqual((a, b), (g._grade_match(np.array([0.0, 0.0])), g._grade_match(np.array([16.0, 0.0]))))
        self.assertGreater(a, 0.5)                                          # (the lower end's approach raised, as before)


if __name__ == "__main__":
    unittest.main()
