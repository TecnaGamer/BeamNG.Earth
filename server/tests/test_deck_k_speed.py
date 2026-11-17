"""Deck vertical limits by speed (roads.DECK_K_SPEED, BNE_DECK_K_SPEED; the road revamp's R1e, 2026-11-17, plan §2.6):
a deck's crest curve as long as its road's design speed asks (AASHTO crest K x 100), then half, before today's
curves, each only when it holds what today's best holds and stands at most DECK_K_OVER_M higher; the ground
envelope's curve_deck likewise (R, R/2, 300 m). Off, as before."""
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import roads  # noqa: E402


def flat(xy):
    return np.full(len(xy), 10.0)


def spec(cls, kmh=None, urban=False, link=False):
    return roads.Spec(width=7.0, kind="road", urban=urban, cls=cls, kmh=kmh, link=link)


def bend(z, step):
    """Largest change of grade per metre (1 / the sharpest vertical radius)."""
    gr = np.diff(z) / step
    return float(np.max(np.abs(np.diff(gr)))) / step


def overpass(L=900.0, step=4.0):
    x = np.arange(0.0, L + 1e-9, step)
    return np.c_[x, np.zeros(len(x))]


class DeckKSpeedTest(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(roads.DECK_K_SPEED)

    def test_crest_radii_by_design_speed(self):
        got = roads.deck_crest_radii([spec("motorway"), spec("residential", urban=True), spec("primary", kmh=80.0),
                                      spec("motorway", link=True)])
        self.assertAlmostEqual(got[0] / 9390.0, 1.0, 1, "120 km/h: ~9.4 km")
        self.assertAlmostEqual(got[1] / 150.0, 1.0, 1, "an urban street, 30 km/h: ~150 m")
        self.assertAlmostEqual(got[2] / 2530.0, 1.0, 1, "its speed limit: 80 km/h")
        self.assertLess(got[3], got[0], "a link: 0.7 of its road's speed")

    def test_off_the_radius_changes_nothing(self):
        ln = overpass()
        a = roads.deck_profile([ln], flat, [(0, 450.0, 10.0)])
        b = roads.deck_profile([ln], flat, [(0, 450.0, 10.0)], crest=[9390.0])
        np.testing.assert_array_equal(a[0], b[0])

    def test_a_long_motorway_deck_takes_a_longer_crest(self):
        ln = overpass()
        off, = roads.deck_profile([ln], flat, [(0, 450.0, 10.0)], crest=[9390.0])
        with mock.patch.object(roads, "DECK_K_SPEED", True):
            on, = roads.deck_profile([ln], flat, [(0, 450.0, 10.0)], crest=[9390.0])
            slow, = roads.deck_profile([ln], flat, [(0, 450.0, 10.0)], crest=[150.0])
        need = 10.0 + roads.DECK_ROAD_CLEAR
        mid = np.abs(ln[:, 0] - 450.0) <= roads.DECK_ROAD_HALF
        self.assertGreaterEqual(float(on[mid].min()), need - 0.06, "the clearance held")
        self.assertLessEqual(float(on.max()), float(off.max()) + roads.DECK_K_OVER_M + 1e-9, "no higher than allowed")
        self.assertLess(bend(on, 4.0), 0.75 * bend(off, 4.0), "a gentler crest")
        np.testing.assert_array_equal(slow, off, "a 30 km/h road's radius is under today's: as before")

    def test_a_short_deck_keeps_todays_profile(self):
        """Too short for the long curve to clear: today's result."""
        ln = overpass(L=200.0)
        off, = roads.deck_profile([ln], flat, [(0, 100.0, 10.0)], crest=[9390.0])
        with mock.patch.object(roads, "DECK_K_SPEED", True):
            on, = roads.deck_profile([ln], flat, [(0, 100.0, 10.0)], crest=[9390.0])
        mid = np.abs(ln[:, 0] - 100.0) <= roads.DECK_ROAD_HALF
        self.assertGreaterEqual(float(on[mid].min()), float(off[mid].min()) - 0.05)

    def test_curve_deck_radii(self):
        import fast
        s = np.arange(0.0, 401.0, 4.0)
        z = np.where((s >= 160) & (s <= 240), 6.0, 0.0)
        floor = np.where((s >= 180) & (s <= 220), 6.0, 0.0)
        cap = np.full(len(s), 50.0)
        got, k = fast._ef.curve_deck_radii(s, z, floor, cap, np.array([1e6, 100.0]))
        self.assertEqual(k, 1, "the long radius cannot rise 6 m in 180 m: the next")
        self.assertGreater(float(got[50]), 5.99)
        for zz in (z, got):                     # (one that does not meet in 2000 rounds, one that does)
            old = fast._ef.curve_deck(s, zz, floor, cap)
            np.testing.assert_array_equal(fast._ef.curve_deck_radii(s, zz, floor, cap, np.array([300.0]))[0], old,
                                          "300 m: the old kernel's result")
        _z, k = fast._ef.curve_deck_radii(s, z, floor, cap, np.array([1e9]))
        self.assertEqual(k, -1)
        with self.assertRaises(ValueError):
            fast._ef.curve_deck_radii(s, z, floor[:-1], cap, np.array([300.0]))
        with self.assertRaises(ValueError):
            fast._ef.curve_deck_radii(s, z, floor, cap, np.array([np.nan]))
        with self.assertRaises(ValueError):
            fast._ef.deck_crest_radii(np.zeros((2, 3)))


if __name__ == "__main__":
    unittest.main()
