"""roads._crossings_by: the crossings of each ground line with the parts, the same with and without the grid that leaves
out the pieces of a line no part can cross (roads._pieces_near_parts)."""
import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import fast  # noqa: E402
import roads  # noqa: E402


def every_piece(lines, ground_lines):
    """_crossings_by as it was: every piece of every ground line given to the kernel."""
    out = [[] for _ in ground_lines]
    pieces, owner = [], []
    for g, ln in enumerate(roads._lines64(ground_lines)):
        if len(ln) <= roads.CROSSINGS_CHUNK + 1:
            pieces.append(ln)
            owner.append(g)
            continue
        for a in range(0, len(ln) - 1, roads.CROSSINGS_CHUNK):
            pieces.append(ln[a:a + roads.CROSSINGS_CHUNK + 1])
            owner.append(g)
    for pi, c, arc, x, y in fast._ef.segment_crossings_lists(roads._lines64(lines), pieces):
        out[owner[c]].append((pi, arc, np.array([x, y])))
    return out


def walk(rng, n, step, origin, spread):
    pts = [origin + rng.uniform(-spread, spread, 2)]
    heading = rng.uniform(0, 2 * np.pi)
    for _ in range(n - 1):
        heading += rng.normal(0, 0.2)
        pts.append(pts[-1] + step * np.array([np.cos(heading), np.sin(heading)]))
    return np.array(pts)


class CrossingsByTest(unittest.TestCase):
    def test_the_same_crossings_in_the_same_order_as_every_piece_given_to_the_kernel(self):
        rng = np.random.default_rng(7)
        found = 0
        for trial in range(6):
            parts = [walk(rng, int(rng.integers(20, 400)), 4.0, np.zeros(2), 300.0) for _ in range(int(rng.integers(1, 6)))]
            lines = [walk(rng, int(rng.integers(2, 120)), 5.0, np.zeros(2), 600.0) for _ in range(60)]
            want = every_piece(parts, lines)
            got = roads._crossings_by(parts, lines)
            self.assertEqual(len(got), len(want))
            for g, w in zip(got, want):
                self.assertEqual(len(g), len(w))
                for (pi, arc, pt), (pj, arc2, pt2) in zip(g, w):
                    self.assertEqual(pi, pj)
                    self.assertEqual(arc, arc2)
                    self.assertTrue((pt == pt2).all())
            found += sum(len(w) for w in want)
        self.assertGreater(found, 20, "the trials cross something")

    def test_a_line_that_touches_a_part_at_a_cell_edge_is_found(self):
        part = np.array([[0.0, 0.0], [10.0, 0.0]])
        line = np.array([[10.0, -5.0], [10.0, 5.0]])      # (through the part's end point)
        got = roads._crossings_by([part], [line])
        self.assertEqual([len(g) for g in got], [1])

    def test_no_parts_and_no_lines(self):
        self.assertEqual(roads._crossings_by([], [np.zeros((2, 2))]), [[]])
        self.assertEqual(roads._crossings_by([np.zeros((2, 2))], []), [])


if __name__ == "__main__":
    unittest.main()
