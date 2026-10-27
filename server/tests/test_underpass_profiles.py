"""Underpass troughs follow connected roads and preserve crossing allowances."""
import os
import sys
from unittest import TestCase

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading


def line(start, end, spacing=2.0):
    start, end = np.asarray(start, float), np.asarray(end, float)
    length = float(np.linalg.norm(end - start))
    return np.linspace(start, end, int(np.ceil(length / spacing)) + 1)


class UnderpassProfilesTest(TestCase):
    def test_two_crossings_keep_targets_measured_from_original_profile(self):
        xy = line((-600, 0), (600, 0))
        original = 100.0 + 0.01 * xy[:, 0]
        source_x, depths = (-24.0, 24.0), (4.0, 5.0)
        requirements = [(0, x + 600.0, depth) for x, depth in zip(source_x, depths)]
        before = list(requirements)
        offset, = grading.underpass_offsets([xy], requirements)
        lowered = original + offset
        for x, depth in zip(source_x, depths):
            target = float(np.interp(x, xy[:, 0], original)) - depth
            self.assertLessEqual(float(np.interp(x, xy[:, 0], lowered)), target + 1e-9)
        self.assertEqual(requirements, before)
        self.assertTrue(np.all(offset <= 0.0))
        self.assertLessEqual(float(-offset.min()), grading.UNDER_MAX_DROP_M)

    def test_overlapping_troughs_do_not_add_their_full_depths(self):
        xy = line((-600, 0), (600, 0))
        first, = grading.underpass_offsets([xy], [(0, 576.0, 4.0)])
        second, = grading.underpass_offsets([xy], [(0, 624.0, 5.0)])
        combined, = grading.underpass_offsets([xy], [(0, 576.0, 4.0), (0, 624.0, 5.0)])
        both = (first < -0.1) & (second < -0.1)
        self.assertTrue(both.any())
        self.assertTrue(np.all(combined <= np.minimum(first, second) + 1e-9))
        self.assertTrue(np.all(combined[both] > first[both] + second[both] + 1e-6))

    def test_joined_roads_share_lowering_across_junction_footprint(self):
        # A short crossing part continues into ramps; one ramp branches at its
        # start. The trough must extend through that node instead of ending there.
        lines = [line((0, 0), (150, 0)), line((-450, 0), (0, 0)),
                 line((0, 0), (0, 450)), line((150, 0), (600, 0))]
        offsets = grading.underpass_offsets(lines, [(0, 80.0, 4.0)])
        self.assertLess(offsets[0][0], -0.1)
        self.assertAlmostEqual(offsets[0][0], offsets[1][-1], places=9)
        self.assertAlmostEqual(offsets[0][0], offsets[2][0], places=9)
        self.assertAlmostEqual(offsets[0][-1], offsets[3][0], places=9)
        # Equal lowering across the shared footprint prevents terrain grading
        # from blending incident roads with opposing artificial ramp slopes.
        for i, distance in ((0, lines[0][:, 0]), (1, -lines[1][:, 0]),
                            (2, lines[2][:, 1])):
            np.testing.assert_allclose(offsets[i][distance <= 12.0], offsets[0][0])

    def test_nearby_disconnected_road_is_untouched(self):
        connected = line((-600, 0), (600, 0))
        nearby = connected + [0.0, 5.0]
        offsets = grading.underpass_offsets([connected, nearby], [(0, 600.0, 5.0)])
        self.assertLess(float(offsets[0].min()), -4.9)
        np.testing.assert_array_equal(offsets[1], np.zeros(len(nearby)))

    def test_true_loose_ends_stay_pinned(self):
        xy = line((0, 0), (100, 0))
        offset, = grading.underpass_offsets([xy], [(0, 50.0, 4.0)])
        np.testing.assert_array_equal(offset[[0, -1]], [0.0, 0.0])
        self.assertTrue(np.isfinite(offset).all())
        self.assertTrue(np.all(offset <= 0.0))
        self.assertLess(float(offset.min()), 0.0)

    def test_full_clearance_interval_and_ramp_grade_are_preserved(self):
        xy = line((-600, 0), (600, 0), spacing=1.0)
        offset, = grading.underpass_offsets([xy], [(0, 600.0, 5.0)])
        np.testing.assert_allclose(offset[np.abs(xy[:, 0]) <= grading.UNDER_FLAT_M], -5.0)
        slope = np.diff(offset) / np.diff(xy[:, 0])
        self.assertLessEqual(float(np.abs(slope).max()), grading.UNDER_GRADE)
        self.assertLess(float(np.abs(np.diff(slope)).max()), 0.002)

    def test_without_clearance_requirements_roads_are_unchanged(self):
        lines = [line((0, 0), (100, 0)), line((100, 0), (200, 100))]
        offsets = grading.underpass_offsets(lines, [])
        for xy, offset in zip(lines, offsets):
            np.testing.assert_array_equal(offset, np.zeros(len(xy)))
