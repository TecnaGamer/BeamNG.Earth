"""grading.portal_lip (a road's last metres into a tunnel's portal held to its own line) and railways._clear_bridge_ends
(a rail bridge ending over the road it spans carried on until its embankment clears that road)."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import railways  # noqa: E402


class PortalLipTest(unittest.TestCase):
    STEP = grading.PROFILE_STEP

    def road(self, n=40, grade=-0.02, z0=272.0):
        return z0 + grade * np.arange(n) * self.STEP

    def test_face_over_the_mouth_held_to_the_road(self):
        z = self.road()
        z[0], z[1] = 280.3, 275.4         # (the cutting's face at the mouth, I-75 at the Cobb Cloverleaf)
        got = grading.portal_lip(z, at_end=False, step=self.STEP)
        self.assertLess(got[0], 272.0 + grading.PORTAL_LIP_TOL_M + 0.05)
        self.assertLess(got[1], 272.0 + grading.PORTAL_LIP_TOL_M + 0.05)
        np.testing.assert_array_equal(got[10:], z[10:])     # (past PORTAL_LIP_M: as read)

    def test_end_of_the_part(self):
        z = self.road()
        z[-1] = z[-2] + 6.0
        got = grading.portal_lip(z, at_end=True, step=self.STEP)
        self.assertLess(got[-1] - z[-2], 1.0)

    def test_a_road_on_its_own_line_unchanged(self):
        z = self.road(grade=0.05)
        np.testing.assert_array_equal(grading.portal_lip(z, False, self.STEP), z)
        np.testing.assert_array_equal(grading.portal_lip(z, True, self.STEP), z)

    def test_a_small_rise_unchanged(self):
        z = self.road()
        z[0] += grading.PORTAL_LIP_MIN_M * 0.8
        np.testing.assert_array_equal(grading.portal_lip(z, False, self.STEP), z)

    def test_never_raised(self):
        z = self.road()
        z[0] -= 3.0                       # (a dip at the mouth is left alone)
        got = grading.portal_lip(z, False, self.STEP)
        self.assertTrue((got <= z + 1e-12).all())

    def test_short_part_unchanged(self):
        z = np.array([280.0, 272.0, 271.9])
        np.testing.assert_array_equal(grading.portal_lip(z, False, self.STEP), z)


def _line(a, b, step=railways.STEP_M):
    a, b = np.asarray(a, float), np.asarray(b, float)
    n = int(round(np.hypot(*(b - a)) / step))
    return a + (b - a) * np.linspace(0, 1, n + 1)[:, None]


class ClearBridgeEndsTest(unittest.TestCase):
    """A road along y = 0 (half width 7 m); a track crossing it diagonally, its bridge ending at the road's edge."""

    def run_rule(self, rails, roads_=None):
        roads_ = [(np.array([[-200.0, 0.0], [200.0, 0.0]]), 7.0 + railways.END_CLEAR_SIDE_M)] if roads_ is None \
            else roads_
        with mock.patch.object(railways, "_spanned_roads", lambda frame, cells, boxes: roads_):
            return railways._clear_bridge_ends(rails, SimpleNamespace(), [])

    def test_bridge_carried_past_the_road(self):
        bridge = _line((-40.0, -40.0), (6.0, 6.0))        # (ends 6 m off the road's line: inside its lanes)
        ground = _line((6.0, 6.0), (60.0, 60.0))
        got = self.run_rule([("a", "standard_gauge", "bridge", bridge), ("b", "standard_gauge", "ground", ground)])
        kinds = [(f, k, len(xy)) for f, _c, k, xy in got]
        self.assertEqual([k for _f, k, _n in kinds], ["bridge", "bridge", "ground"])
        added = got[1][3]
        np.testing.assert_allclose(added[0], ground[0])   # (from the bridge's end ...)
        clear = 7.0 + railways.END_CLEAR_SIDE_M + railways.FOOT_MAX_M + railways.STEP_M
        self.assertGreater(abs(added[-1, 1]), clear - railways.STEP_M)   # (... until the embankment clears the road)
        np.testing.assert_allclose(got[2][3][0], added[-1])
        self.assertEqual(len(added) + len(got[2][3]) - 1, len(ground))     # (the lattice kept)

    def test_bridge_ending_clear_of_the_road_unchanged(self):
        bridge = _line((-40.0, -40.0), (30.0, 30.0))
        ground = _line((30.0, 30.0), (60.0, 60.0))
        rails = [("a", "standard_gauge", "bridge", bridge), ("a", "standard_gauge", "ground", ground)]
        self.assertEqual(self.run_rule(rails), rails)

    def test_road_the_bridge_does_not_cross_unchanged(self):
        bridge = _line((-40.0, -60.0), (-10.0, -10.0))    # (ends short of the road: a road beside, not under it)
        ground = _line((-10.0, -10.0), (-10.0, 60.0))
        rails = [("a", "standard_gauge", "bridge", bridge), ("a", "standard_gauge", "ground", ground)]
        self.assertEqual(self.run_rule(rails), rails)

    def test_at_most_end_clear_max(self):
        # (a bridge crossing to the road's centre line, its ground part then running along it)
        bridge = np.vstack([_line((0.0, -40.0), (0.0, 0.0))[:-1], _line((0.0, 0.0), (0.0, 0.5))])
        ground = _line((0.0, 0.5), (200.0, 0.5))
        got = self.run_rule([("a", "standard_gauge", "bridge", bridge), ("a", "standard_gauge", "ground", ground)])
        added = got[1][3]
        self.assertLessEqual(railways._arc(added)[-1], railways.END_CLEAR_MAX_M + railways.STEP_M + 1e-6)

    def test_off_switch(self):
        self.assertFalse(railways.END_CLEAR_ON)        # (off by default until test_drivability holds with it)


if __name__ == "__main__":
    unittest.main()
