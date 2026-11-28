"""airportsmooth: the ground round an airport smoothed, the DEM kept bit for bit past the zone."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import airportsmooth as asm          # noqa: E402
import runwaygeom                   # noqa: E402


class _Rw:
    def __init__(self):
        self.id = 1


class SmoothTest(unittest.TestCase):
    def setUp(self):
        g = runwaygeom.Geometry("x", -27.0, 153.0, 1.0, 0.0, 2000.0, 45.0, 7.5, 140.0, 60.0, 0.0, None, True)
        self.g = g
        self._geometry = runwaygeom.geometry
        runwaygeom.geometry = lambda rw: g
        self.addCleanup(lambda: setattr(runwaygeom, "geometry", self._geometry))
        # rolling ground: 3 m humps every 80 m on a gentle 0.5 % slope
        self.dem = lambda la, lo: (np.asarray(la) + 27.0) * 111000.0 * 0.005 + 3.0 * np.sin(np.asarray(lo) * 111000.0 / 80.0)
        self.s = asm.Surface([_Rw()], self.dem)

    def test_humps_smoothed_on_the_apron_side(self):
        s = np.linspace(200.0, 1800.0, 200)
        la, lo = self.g.latlon(s, np.full_like(s, 300.0))       # 300 m off the centreline: the aprons
        z = self.dem(la, lo)
        out = self.s.apply(la, lo, z)
        left = np.ptp(out - np.polyval(np.polyfit(s, out, 1), s))
        was = np.ptp(z - np.polyval(np.polyfit(s, z, 1), s))
        self.assertGreater(was, 5.0)
        self.assertLess(left, 0.15 * was, "the humps cut to under 15 %")

    def test_far_ground_bit_for_bit(self):
        la, lo = self.g.latlon(np.array([1000.0]), np.array([3000.0]))
        z = self.dem(la, lo)
        self.assertTrue(np.array_equal(self.s.apply(la, lo, z), z))

    def test_deterministic(self):
        s2 = asm.Surface([_Rw()], self.dem)
        self.assertTrue(np.array_equal(self.s.z, s2.z))


if __name__ == "__main__":
    unittest.main()
