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


if __name__ == "__main__":
    unittest.main()
