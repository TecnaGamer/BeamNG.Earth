"""Shorelines for the shore sound (earthfast.shore_lines, seacarve.cell_shores; the mod lays "Waves" ribbons along
them): a lake's whole edge as one loop, a pond under min_px none, the sea always, only the edges of a cell's own
pixels (its neighbour has the rest)."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fast  # noqa: E402


def lines(kind, min_px, box):
    pts, off, wpx, kinds = fast._ef.shore_lines(np.ascontiguousarray(kind, np.uint8), min_px, 0.5, 2.0, *box)
    return [(pts[off[k]:off[k + 1]], int(kinds[k]), tuple(wpx[k])) for k in range(len(kinds))]


class ShoreLinesTest(unittest.TestCase):
    def test_lake_pond_and_sea(self):
        k = np.zeros((100, 100), np.uint8)
        k[20:60, 20:60] = 1             # a lake: 1600 px
        k[80:83, 80:83] = 1             # a pond: 9 px
        k[:, 95:] = 2                   # the sea along the east edge
        got = lines(k, 100, (0, 100, 0, 100))
        lake = [g for g in got if g[1] == 1]
        sea = [g for g in got if g[1] == 2]
        self.assertEqual(len(lake), 1)                          # (the pond left out)
        p = lake[0][0]
        self.assertTrue(np.allclose(p[0], p[-1]))               # a loop ...
        self.assertEqual({tuple(v) for v in p}, {(20, 20), (60, 20), (60, 60), (20, 60)})   # ... its corners
        self.assertEqual(k[lake[0][2]], 1)                      # (a pixel of its water)
        self.assertEqual(len(sea), 1)
        self.assertTrue(np.allclose(sea[0][0][:, 0], 95))       # the sea's edge, x = 95, top to bottom

    def test_only_own_pixels(self):
        k = np.zeros((40, 40), np.uint8)
        k[10:30, 10:30] = 1
        got = lines(k, 1, (0, 20, 0, 40))      # this cell's pixels: x 0..20
        xs = np.concatenate([g[0][:, 0] for g in got])
        self.assertLessEqual(xs.max(), 20.0)
        self.assertTrue(all(not np.allclose(g[0][0], g[0][-1]) for g in got))      # cut: open lines
        rest = lines(k, 1, (20, 40, 0, 40))
        both = sum(float(np.hypot(*np.diff(g[0], axis=0).T).sum()) for g in got + rest)
        self.assertAlmostEqual(both, 80.0)                      # the whole edge, once


if __name__ == "__main__":
    unittest.main()
