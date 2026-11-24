"""BNE_ROAD_FOLLOW (roads keep the land's crests and dips): grading.profile_sigma with the switch on and off."""
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import grading  # noqa: E402
import roads  # noqa: E402


def spec(cls, urban=False, **kw):
    return roads.Spec(7.0, "paved", urban, cls, **kw)


class FollowTest(unittest.TestCase):
    def test_off_is_unchanged(self):
        with mock.patch.object(grading, "FOLLOW_ON", False):
            self.assertEqual(grading.profile_sigma(spec("tertiary")), 20.0)
            self.assertEqual(grading.profile_sigma(spec("primary")), grading.SIGMA_MAJOR_M)

    def test_on_shorter_for_minor_classes_majors_kept(self):
        with mock.patch.object(grading, "FOLLOW_ON", True):
            self.assertEqual(grading.profile_sigma(spec("tertiary")), 6.0)
            self.assertEqual(grading.profile_sigma(spec("primary")), 12.0)
            self.assertEqual(grading.profile_sigma(spec("tertiary", link=True)), grading.FOLLOW_LINK_SIGMA_M)
            self.assertEqual(grading.profile_sigma(spec("motorway")), grading.SIGMA_MAJOR_M)
            self.assertEqual(grading.profile_sigma(spec("trunk")), grading.SIGMA_MAJOR_M)
            self.assertEqual(grading.profile_sigma(roads.Spec(4.0, "gravel", False, "track")),
                             grading.DIRT_SIGMA_MINOR_M)      # dirt roads as before

    def test_on_keeps_a_100m_dip(self):
        s = np.arange(0.0, 600.0, grading.PROFILE_STEP)
        z = -2.0 * np.exp(-((s - 300.0) / 25.0) ** 2)         # a 2 m dip about 100 m across
        kept = {}
        for on in (False, True):
            with mock.patch.object(grading, "FOLLOW_ON", on):
                zs = grading.smooth_profile(z, grading.PROFILE_STEP, grading.profile_sigma(spec("tertiary")))
            kept[on] = -float(zs.min()) / 2.0
        self.assertLess(kept[False], 0.7)
        self.assertGreater(kept[True], 0.85)


if __name__ == "__main__":
    unittest.main()
