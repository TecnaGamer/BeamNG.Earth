"""A rail bridge over a road where the elevation shows no separation (built after the survey): the track is raised,
the road left at grade (railways.flat_rail_crossing, grading._lower_underpasses; Davis Road under the Armadale Line,
Kelmscott WA, 2026-10-29)."""
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy  # noqa: E402
import railways  # noqa: E402

FRAME = geodesy.EnuFrame(-32.1171858, 116.016649, 0.0)


def flat(la, lo):
    return np.full(np.shape(la), 25.0)


def cut(la, lo):
    """25 m, but a 6 m deep trench along the road (y = 0) where it passes under the bridge."""
    _e, n, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
    return 25.0 - 6.0 * (np.abs(np.asarray(n)) < 30.0)


class FlatRailCrossingTest(unittest.TestCase):
    bridge = np.c_[np.zeros(27), np.linspace(-26, 26, 27)]       # a 52 m deck north-south over x = 0
    road = np.c_[np.linspace(-200, 200, 101), np.zeros(101)]     # the road east-west, crossing at arc 200

    def test_flat_ground_is_no_separation(self):
        self.assertTrue(railways.flat_rail_crossing(FRAME, self.bridge, self.road, 200.0, flat))

    def test_a_real_cutting_keeps_the_dip(self):
        bridge = np.c_[np.zeros(27), np.linspace(-40, 40, 27)]   # (its ends on the banks)
        self.assertFalse(railways.flat_rail_crossing(FRAME, bridge, self.road, 200.0, cut))

    def test_off_or_no_ground_as_before(self):
        self.assertFalse(railways.flat_rail_crossing(FRAME, self.bridge, self.road, 200.0, None))
        with mock.patch.object(railways, "FLAT_SEP_ON", False):
            self.assertFalse(railways.flat_rail_crossing(FRAME, self.bridge, self.road, 200.0, flat))


if __name__ == "__main__":
    unittest.main()
