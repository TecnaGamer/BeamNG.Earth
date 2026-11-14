"""A sharp corner's raised nose (junctions.find NOSE_*; the user, 2026-11-11: "Also check sharper corners"): the corner
slab goes on past where the two sidewalks' outer edges meet until they are NOSE_GAP_M apart."""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import junctions  # noqa: E402
import roads  # noqa: E402


def fork(deg):
    sp = roads.Spec(9.0, "paved", True, "residential", False, walk=2.0)
    a = np.array([[0.0, 0.0, 0.0], [120.0, 0.0, 0.0]])
    t = math.radians(deg)
    b = np.array([[0.0, 0.0, 0.0], [120.0 * math.cos(t), 120.0 * math.sin(t), 0.0]])
    c = np.array([[-120.0, 0.0, 0.0], [0.0, 0.0, 0.0]])
    return [("a", sp, a, 0.0), ("b", sp, b, 0.0), ("c", sp, c, 0.0)]


class NoseTest(unittest.TestCase):
    def test_sharp_fork_gets_a_nose(self):
        rows = fork(40.0)
        corners = junctions.find(rows, roads.has_sidewalks, None)[0]
        noses = [c for c in corners if len(c["outer"]) == 4]
        self.assertEqual(len(noses), 1, "the 40 degree corner between the fork's branches")
        o = np.asarray(noses[0]["outer"], np.float64)
        gap = float(np.hypot(*(o[1, :2] - o[2, :2])))
        self.assertAlmostEqual(gap, junctions.NOSE_GAP_M, delta=0.05)
        # (its far edge lies between the branches, past the corner, off both carriageways)
        mid = (o[1, :2] + o[2, :2]) / 2
        self.assertGreater(float(np.hypot(*mid)), 10.0)
        for _fid, sp, pts, _lon in rows:        # (off every carriageway: farther than half its width - 0.3 m)
            a, b = pts[0, :2], pts[1, :2]
            t = np.clip(np.dot(mid - a, b - a) / np.dot(b - a, b - a), 0.0, 1.0)
            self.assertGreater(float(np.hypot(*(mid - (a + t * (b - a))))), sp.width / 2 - 0.3)

    def test_wide_corners_unchanged(self):
        corners = junctions.find(fork(90.0), roads.has_sidewalks, None)[0]
        self.assertTrue(corners)
        self.assertTrue(all(len(c["outer"]) == 3 for c in corners))


if __name__ == "__main__":
    unittest.main()
