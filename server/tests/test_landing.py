"""A bridge's loose end over water with no road going on (a ferry's linkspan, a pier) is held over the sea, not on the
sea bed (GitHub #1067: the Lodingen quay's linkspans fell 7 m in 20 m into the fjord)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy  # noqa: E402
import roads  # noqa: E402

FRAME = geodesy.EnuFrame(68.42, 15.99)


def bed(la, lo):
    """Land (3 m) west of x = 10 m, the sea bed (-6 m) east of it."""
    x, _, _ = FRAME.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), np.zeros(np.shape(la)))
    return np.where(np.asarray(x) < 10.0, 3.0, -6.0)


def profiles():
    bp = roads.BridgeProfiles(FRAME, lambda key: None, bed)
    bp.wet = lambda la, lo: np.where(np.asarray(bed(la, lo)) < 0.0, 6.0, 0.0)
    return bp


class LandingTest(unittest.TestCase):
    line = np.c_[np.linspace(0.0, 24.0, 13), np.zeros(13)]

    def sea(self, x):
        la, lo, _ = FRAME.enu_to_geodetic(np.array([x]), np.zeros(1), 0.0)
        return float(FRAME.geodetic_to_enu(la, lo, np.zeros(1))[2][0])

    def test_linkspan_end_held_over_the_sea(self):
        bp = profiles()
        road = [np.c_[np.linspace(-40.0, 0.0, 5), np.zeros(5)]]       # (the quay road onto its land end)
        g = bp._landing_ground([self.line], road)
        z = g(self.line[[0, -1]])
        self.assertAlmostEqual(float(z[0]), 3.0, delta=0.01)             # the land end: its ground
        self.assertAlmostEqual(float(z[1]), self.sea(24.0) + roads.LANDING_FREEBOARD_M, delta=0.01)
        self.assertAlmostEqual(float(g(self.line[[6]])[0]), -6.0, delta=0.01)   # (the rest: the ground as it is)

    def test_road_going_on_keeps_the_abutment(self):
        bp = profiles()
        road = [np.c_[np.linspace(-40.0, 0.0, 5), np.zeros(5)], np.c_[np.linspace(24.0, 60.0, 5), np.zeros(5)]]
        self.assertEqual(bp._landing_ground([self.line], road), bp._ground)

    def test_dry_ends_unchanged(self):
        bp = profiles()
        bp.wet = lambda la, lo: np.zeros(np.shape(la))
        self.assertEqual(bp._landing_ground([self.line], []), bp._ground)


if __name__ == "__main__":
    unittest.main()
