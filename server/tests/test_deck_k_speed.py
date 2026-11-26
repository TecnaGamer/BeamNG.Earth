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
    def test_ground_reuse_preserves_batches_and_retries_short_reads(self):
        import revisit
        calls = []

        def read(xy):
            calls.append(xy.copy())
            if len(calls) == 1:
                revisit.fault(here=True)
            return xy[:, 0] + len(calls)

        sample = roads._profile_ground_cache(read)
        points = np.array([[1., 0.], [3., 0.]])
        np.testing.assert_array_equal(sample(points), [2., 4.])
        sample(points)[:] = 0
        np.testing.assert_array_equal(sample(points.copy()), [3., 5.])
        np.testing.assert_array_equal(sample(points[::-1]), [6., 4.])
        self.assertEqual(len(calls), 3)

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
        """curve_profile's own rung: the design speed's crest rounds a 900 m motorway overpass over its length, its
        steepest grade gentler and its peak no higher."""
        ln = overpass()
        s = ln[:, 0]
        g = np.full(len(s), 10.0)
        req = np.where(np.abs(s - 450.0) <= roads.DECK_ROAD_HALF, 10.0 + roads.DECK_ROAD_CLEAR, -np.inf)
        off = roads.curve_profile(s, g, req, 10.0, 10.0, crest_r=9390.0)
        with mock.patch.object(roads, "DECK_K_SPEED", True):
            on = roads.curve_profile(s, g, req, 10.0, 10.0, crest_r=9390.0)
            slow = roads.curve_profile(s, g, req, 10.0, 10.0, crest_r=150.0)
        mid = np.abs(s - 450.0) <= roads.DECK_ROAD_HALF
        self.assertGreaterEqual(float(on[mid].min()), 10.0 + roads.DECK_ROAD_CLEAR - 0.06, "the clearance held")
        self.assertLessEqual(float(on.max()), float(off.max()) + roads.DECK_K_OVER_M + 1e-9, "no higher peak")
        self.assertLess(bend(on, 4.0), 0.75 * bend(off, 4.0), "a gentler crest")
        self.assertLess(float(np.max(np.abs(np.diff(on)))), float(np.max(np.abs(np.diff(off)))), "gentler at most")
        np.testing.assert_array_equal(slow, off, "a 30 km/h road's radius is under today's: as before")

    def test_the_chain_guard_keeps_todays_deck_when_any_point_rises(self):
        """deck_profile's guard is point by point (Codex review of 16fa5b80): the longer crest stands up to ~6 m over
        today's deck on its approaches (it climbs from further out, at 2.8 % for today's 5 %), so the chain keeps
        today's profile."""
        ln = overpass()
        off, = roads.deck_profile([ln], flat, [(0, 450.0, 10.0)], crest=[9390.0])
        with mock.patch.object(roads, "DECK_K_SPEED", True):
            on, = roads.deck_profile([ln], flat, [(0, 450.0, 10.0)], crest=[9390.0])
        np.testing.assert_array_equal(on, off)

    def test_a_short_deck_keeps_todays_profile(self):
        """Too short for the long curve to clear: today's result."""
        ln = overpass(L=200.0)
        off, = roads.deck_profile([ln], flat, [(0, 100.0, 10.0)], crest=[9390.0])
        with mock.patch.object(roads, "DECK_K_SPEED", True):
            on, = roads.deck_profile([ln], flat, [(0, 100.0, 10.0)], crest=[9390.0])
        mid = np.abs(ln[:, 0] - 100.0) <= roads.DECK_ROAD_HALF
        self.assertGreaterEqual(float(on[mid].min()), float(off[mid].min()) - 0.05)

    def test_the_chain_is_never_steeper_or_higher(self):
        """deck_profile's guard: the chain with the longer crests only when no part is DECK_K_GRADE steeper nor
        DECK_K_OVER_M higher than without them."""
        ln = overpass(L=100.0)
        base = [np.linspace(0.0, 4.0, len(ln))]                     # 4 %
        self.assertTrue(roads._no_worse([ln], base, [base[0] + 0.2]))
        self.assertFalse(roads._no_worse([ln], base, [base[0] + 0.3]), "higher")
        self.assertFalse(roads._no_worse([ln], base, [np.linspace(0.0, 4.4, len(ln))]), "steeper")
        self.assertFalse(roads._no_worse([ln], base, [np.r_[base[0][:-1], np.nan]]), "NaN")
        # (Codex review of 16fa5b80: equal maxima, shifted: 4 m higher at the third point, 4 % on a level segment)
        line4 = np.c_[np.arange(4) * 100.0, np.zeros(4)]
        self.assertFalse(roads._no_worse([line4], [np.array([0.0, 4.0, 0.0, 0.0])], [np.array([0.0, 4.0, 4.0, 0.0])]))
        self.assertTrue(roads._no_worse([line4], [np.array([0.0, 4.0, 4.0, 0.0])], [np.array([0.0, 4.0, 4.1, 0.0])]),
                        "0.1 m higher, 0.1 % steeper: within the margins")
        short = np.c_[np.arange(4) * 10.0, np.zeros(4)]
        self.assertFalse(roads._no_worse([short], [np.array([0.0, 0.4, 0.4, 0.4])], [np.array([0.0, 0.4, 0.4, 0.6])]),
                         "within the height margin, but 2 % on a level segment (the steepest still 4 %)")

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
