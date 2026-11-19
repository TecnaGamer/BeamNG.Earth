"""Level spacing from real clearance and deck depth (roads.LEVEL_SPACING, BNE_LEVEL_SPACING; the road revamp's R1,
2026-11-16, decision D10): a deck's road stands over the road under it by that road's clearance (motorway, trunk,
primary 4.9 m, other roads 4.5, service 4.0, paths 2.5, rail 6.0: the clearance study 2026-11-17) + the deck's depth (0.045 of its span, 0.7-4.0 m) + 0.1 m, not the
flat 6.15 m + 0.9-2.2 m; off, as before."""
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import roads  # noqa: E402


def flat(xy):
    return np.full(len(xy), 10.0)


def cross():
    x = np.arange(0.0, 801.0, 4.0) - 400.0
    return np.c_[x, np.zeros(len(x))], np.c_[np.zeros(len(x)), x]


class LevelSpacingTest(unittest.TestCase):
    def test_off_as_before(self):
        self.assertFalse(roads.LEVEL_SPACING)
        self.assertAlmostEqual(roads.deck_road_clear(), roads.DECK_ROAD_CLEAR)
        self.assertAlmostEqual(roads.deck_road_clear(1.5, ["motorway"]), 6.15 + 1.5)
        self.assertAlmostEqual(roads.deck_depth(32.0), 32.0 / roads.DECK_SPAN_RATIO)

    def test_clearance_by_the_road_under(self):
        with mock.patch.object(roads, "LEVEL_SPACING", True):
            self.assertAlmostEqual(roads.deck_road_clear(1.5, ["motorway"]), 4.9 + 1.5 + 0.1)
            self.assertAlmostEqual(roads.deck_road_clear(1.5, ["residential"]), 4.5 + 1.5 + 0.1)
            self.assertAlmostEqual(roads.deck_road_clear(1.5, ["service", "footway"]), 4.0 + 1.5 + 0.1)
            self.assertAlmostEqual(roads.deck_road_clear(1.5, ["footway"]), 2.5 + 1.5 + 0.1)
            self.assertAlmostEqual(roads.deck_road_clear(1.5, ["rail"]), 6.0 + 1.5 + 0.1)
            self.assertAlmostEqual(roads.deck_road_clear(1.5), 4.9 + 1.5 + 0.1, msg="unknown: the most demanding")
            self.assertAlmostEqual(roads.deck_depth(10.0), 0.7)
            self.assertAlmostEqual(roads.deck_depth(40.0), 1.8)
            self.assertAlmostEqual(roads.deck_depth(200.0), 4.0)

    def test_a_stack_is_lower(self):
        a, b = cross()
        gaps = {}
        for on in (False, True):
            with mock.patch.object(roads, "LEVEL_SPACING", on):
                clear = roads.deck_road_clear(roads.deck_depth(32.0), ["motorway"])
                low, high = roads.deck_profile([a, b], flat, levels=[1.0, 2.0], widths=[12.0, 12.0], clear=clear)
                gaps[on] = float(high[len(high) // 2] - low[len(low) // 2])
                self.assertGreaterEqual(gaps[on], clear - 0.05)
        self.assertLess(gaps[True], gaps[False] - 0.8, "about a metre lower a level")

    def test_crossing_ground_index(self):
        a, _b = cross()
        far = np.array([[-50.0, 300.0], [50.0, 300.0]])
        under = np.array([[100.0, -50.0], [100.0, 50.0]])
        self.assertEqual(sorted(set(roads.fast_crossing_ground([a], [far, under]))), [1])


if __name__ == "__main__":
    unittest.main()
