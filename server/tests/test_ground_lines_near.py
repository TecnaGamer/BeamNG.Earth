"""roads._ground_lines_near: the ground roads' lines with an end near a tunnel line (tunnel_side_ground's), their ends
tested all at once."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import roads  # noqa: E402

M = roads.CHAIN_END_M
XY = np.array([[0.0, 0.0], [100.0, 0.0]])      # the tunnel line: its box is x 0..100, y 0


def one_by_one(xy, ground):
    """What the rule is, line by line (as it was written before)."""
    lines = [np.asarray(ln, np.float64).reshape(-1, 2) for gk, lns in sorted((ground or {}).items()) if gk[1] == "g"
             for ln in lns if len(ln) >= 2]
    lo, hi = xy.min(axis=0) - M, xy.max(axis=0) + M
    return [ln for ln in lines if ((ln[[0, -1]] >= lo) & (ln[[0, -1]] <= hi)).all(axis=1).any()]


class GroundLinesNearTest(unittest.TestCase):
    def kept(self, ground, xy=XY):
        return [ln.tolist() for ln in roads._ground_lines_near(xy, ground)]

    def test_a_line_ending_on_the_tunnel_is_kept(self):
        ends_on_it = [[50.0, 40.0], [50.0, 0.0]]
        self.assertEqual(self.kept({("a", "g"): [ends_on_it]}), [ends_on_it])

    def test_a_line_crossing_it_with_both_ends_away_is_not(self):
        self.assertEqual(self.kept({("a", "g"): [[[50.0, -40.0], [50.0, 0.0], [50.0, 40.0]]]}), [])

    def test_an_end_just_inside_the_margin_and_one_just_outside(self):
        inside = [[100.0 + M - 0.01, 0.0], [300.0, 0.0]]
        outside = [[100.0 + M + 0.01, 0.0], [300.0, 0.0]]
        self.assertEqual(self.kept({("a", "g"): [inside, outside]}), [inside])

    def test_only_the_ground_keys_in_their_order_and_no_single_points(self):
        a, b = [[10.0, 0.0], [10.0, 5.0]], [[20.0, 0.0], [20.0, 5.0]]
        ground = {("z", "g"): [b], ("a", "g"): [a, [[30.0, 0.0]]], ("a", "t"): [[[40.0, 0.0], [40.0, 5.0]]]}
        self.assertEqual(self.kept(ground), [a, b])

    def test_no_ground(self):
        self.assertEqual(self.kept(None), [])
        self.assertEqual(self.kept({}), [])
        self.assertEqual(self.kept({("a", "t"): [[[0.0, 0.0], [1.0, 1.0]]]}), [])

    def test_the_same_lines_as_asked_one_by_one(self):
        rng = np.random.default_rng(20261115)
        for _ in range(40):
            xy = rng.uniform(-200, 200, (int(rng.integers(2, 8)), 2))
            ground = {(f"f{k:03d}", "g" if k % 4 else "t"):
                      [rng.uniform(-400, 400, (int(rng.integers(1, 6)), 2)) for _ in range(int(rng.integers(0, 4)))]
                      for k in range(60)}
            got, want = roads._ground_lines_near(xy, ground), one_by_one(xy, ground)
            self.assertEqual(len(got), len(want))
            for a, b in zip(got, want):
                np.testing.assert_array_equal(a, b)


if __name__ == "__main__":
    unittest.main()
