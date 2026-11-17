"""A long feature's level ranges from its home copy, laid on each copy's own extent (roads.home_ranges / map_ranges):
mapdata stitches a long line's copy per cell, cut to other lengths, and the home copy's fractions taken as they were
put a copy's bridges out of place (the Main-Weser-Bahn's bridge over Friedrich-Ebert-Strasse in Treysa lay 100 m up the
line in the cell whose copy ran 2.9 km further: its ballast across the road under the deck, 2026-11-16)."""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import roads  # noqa: E402

LAT0, LON0 = 50.9118, 9.1905
M_LAT = 1.0 / 111320.0
M_LON = M_LAT / math.cos(math.radians(LAT0))


def line(length_m, start_m=0.0, step_m=50.0):
    """A lon/lat line running north-north-east from (LON0, LAT0), from start_m to length_m metres along it."""
    s = np.r_[np.arange(start_m, length_m, step_m), length_m]
    return np.c_[LON0 + 0.4 * s * M_LON, LAT0 + 0.92 * s * M_LAT]


def off_m(ring_a, fa, ring_b, fb):
    """m between the point at fraction fa of ring_a and the one at fraction fb of ring_b."""
    a, b = roads._ring_point(ring_a, fa), roads._ring_point(ring_b, fb)
    return float(math.hypot((a[0] - b[0]) / M_LON, (a[1] - b[1]) / M_LAT))


class MapRangesTest(unittest.TestCase):
    def setUp(self):
        self.home = line(10920.0)
        self.bridge = (380.0, 441.0)
        L = 10920.0
        self.rngs = [(self.bridge[0] / L, self.bridge[1] / L, "bridge"), (9000.0 / L, 9050.0 / L, "bridge")]

    def test_longer_copy_keeps_the_bridge_on_its_place(self):
        mine = line(13845.0)
        got = roads.map_ranges(self.home, mine, self.rngs)
        self.assertEqual(len(got), 2)
        for k in (0, 1):
            for e in (0, 1):
                self.assertLess(off_m(self.home, self.rngs[k][e], mine, got[k][e]), 0.5)
        # (taken as they were: 100 m up the line, the crossing under the bridge on the ground)
        self.assertGreater(off_m(self.home, self.rngs[0][0], mine, self.rngs[0][0]), 90.0)

    def test_shorter_copy_drops_and_cuts_what_lies_past_it(self):
        mine = line(9020.0)
        got = roads.map_ranges(self.home, mine, self.rngs)
        self.assertEqual(len(got), 2)
        self.assertLess(off_m(self.home, self.rngs[0][0], mine, got[0][0]), 0.5)
        self.assertEqual(got[1][1], 1.0)            # (the second bridge runs on past this copy's end)
        self.assertLess(off_m(self.home, self.rngs[1][0], mine, got[1][0]), 0.5)
        self.assertEqual(roads.map_ranges(self.home, line(8000.0), self.rngs)[1:], [])

    def test_copy_starting_later_on_the_line(self):
        mine = line(13845.0, start_m=200.0)
        got = roads.map_ranges(self.home, mine, self.rngs)
        self.assertLess(off_m(self.home, self.rngs[0][0], mine, got[0][0]), 0.5)
        self.assertLess(off_m(self.home, self.rngs[0][1], mine, got[0][1]), 0.5)

    def test_same_copy_is_the_same_list(self):
        self.assertIs(roads.map_ranges(self.home, self.home.copy(), self.rngs), self.rngs)

    def test_unrelated_or_reversed_line_is_left(self):
        other = self.home + [0.01, 0.0]             # (700 m east: not the same line)
        self.assertIs(roads.map_ranges(self.home, other, self.rngs), self.rngs)
        self.assertIs(roads.map_ranges(self.home, self.home[::-1].copy(), self.rngs), self.rngs)

    def test_off_switch(self):
        self.assertTrue(roads.HOME_MAP_ON)


if __name__ == "__main__":
    unittest.main()
