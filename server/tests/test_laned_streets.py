"""Town streets of a mapped lane count between buildings: parking lanes and sidewalks out to the walls (the user's
reports 18 and 19 at Fells Point, 2026-11-12: Eastern Avenue, two lanes, parking both sides, 21 m wall to wall)."""
import os
import sys
import types
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import roads  # noqa: E402


class _Feats:
    """One straight feature, the fields _laned_walled reads."""
    def __init__(self):
        self.lonlat = np.array([[-76.593, 39.2856], [-76.591, 39.2857]])
        self.coord_off = np.array([0, 2])
        self.ring_off = np.array([0, 1])
        self.part_off = np.array([0, 1])
        self.ids = np.array(["east"])


class _Widths:
    def __init__(self, m, near):
        self.m, self.near = m, near

    def measure(self, fid, lonlat):
        return self.m

    def cap(self, fid, lonlat):
        return (self.near, None)


def spec(cls="primary", width=6.6, lanes=2, oneway=False, urban=True):
    return roads.Spec(width=width, kind="paved", urban=urban, cls=cls, oneway=oneway, lanes=lanes)


class LanedWalledTest(unittest.TestCase):
    def test_two_lane_primary_between_walls_gets_parking_and_walks(self):
        w = _Widths((12.6, 4.2), 19.1)
        got = roads._laned_walled(_Feats(), 0, spec(), w.measure)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got.width, 6.6 + 2 * roads.PARKING_M, places=6)
        self.assertGreater(got.walk, 3.0)
        self.assertAlmostEqual(roads.laned_parking(got), 2 * roads.PARKING_M, places=6)
        self.assertAlmostEqual(roads.assumed_parking(got), 2 * roads.PARKING_M, places=6)

    def test_no_room_or_no_walls_no_change(self):
        self.assertIsNone(roads._laned_walled(_Feats(), 0, spec(), _Widths(None, None).measure))
        tight = roads._laned_walled(_Feats(), 0, spec(), _Widths((7.0, 1.9), 11.0).measure)
        self.assertTrue(tight is None or abs(tight.width - 6.6) < 1e-9, "no parking lane fits: the lanes alone")

    def test_wide_arterials_and_rural_roads_left(self):
        self.assertIsNone(roads._laned_walled(_Feats(), 0, spec(lanes=6, width=19.8), _Widths((24.0, 4.0), 32).measure))
        self.assertIsNone(roads._laned_walled(_Feats(), 0, spec(urban=False), _Widths((12.6, 4.2), 19.1).measure))

    def test_a_primary_of_its_lanes_alone_parks_nothing(self):
        self.assertEqual(roads.laned_parking(spec()), 0.0)
        self.assertEqual(roads.assumed_parking(spec()), 0.0)


if __name__ == "__main__":
    unittest.main()
