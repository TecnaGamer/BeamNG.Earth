"""A divided street's median at a junction (the user's report 20 at Fells Point, 2026-11-12: "Median end needs work")."""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import buildings  # noqa: E402
import roads  # noqa: E402


def band(x, fid, w=2.0, y0=-80.0, y1=0.0):
    return {"pts": np.array([[x, y0, 0.0], [x, y1, 0.0]]), "median": True, "w": w, "side": 1, "fid": fid}


def corner(c, r, a0, a1):
    t = np.linspace(a0, a1, 9)
    return {"corner": {"arc": np.c_[c[0] + r * np.cos(t), c[1] + r * np.sin(t), np.zeros(9)]}}


class MedianNoseTest(unittest.TestCase):
    def test_returns_reaching_past_a_narrow_median_go(self):
        a, b = band(-2.0, "a"), band(2.0, "b")
        # two 6 m returns from the bands' ends crossing in front of a 4 m nose: a bow tie
        ca = corner((-2.0 + 6.0, 0.0), 6.0, math.pi, math.pi / 2)
        cb = corner((2.0 - 6.0, 0.0), 6.0, 0.0, math.pi / 2)
        got = roads._without_median_returns([a, b, ca, cb])
        self.assertEqual(len(got), 2)
        self.assertTrue(all("corner" not in w for w in got))

    def test_a_round_nose_stays(self):
        a, b = band(-2.0, "a"), band(2.0, "b")
        ca = corner((-2.0 + 2.5, 0.0), 2.5, math.pi, math.pi / 2)
        cb = corner((2.0 - 2.5, 0.0), 2.5, 0.0, math.pi / 2)
        self.assertEqual(len(roads._without_median_returns([a, b, ca, cb])), 4)

    def test_street_corners_stay(self):
        walks = [band(-2.0, "a"), band(2.0, "b"), corner((30.0, 30.0), 6.0, 0.0, math.pi / 2),
                 corner((33.0, 30.0), 6.0, math.pi / 2, math.pi)]
        self.assertEqual(len(roads._without_median_returns(walks)), 4)

    def test_both_halves_of_a_median_one_paving(self):
        walks = [band(-2.0, "zz", w=2.4), band(2.5, "aa", w=0.6), band(40.0, "mm"), {"pts": np.zeros((2, 3)), "fid": "s"}]
        keys = buildings._median_keys(walks)
        self.assertEqual(keys[0], "aa")
        self.assertEqual(keys[1], "aa")
        self.assertEqual(keys[2], "mm")
        self.assertNotIn(3, keys)


if __name__ == "__main__":
    unittest.main()
