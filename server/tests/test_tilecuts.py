"""The road tiles' packed kernels (earthfast 240): build_tile's cut lists as masks of the tile's roads (_RoadCuts),
retaining_walls' deck floor, offset_polyline, railways' ground memo, the crossings of lines given as lists."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import railways
import roads


class _Spec:
    def __init__(self, width):
        self.width = width


def _tile(lines):
    return [(f"r{k}", _Spec(w), np.c_[np.asarray(p, np.float64), np.zeros(len(p))], None)
            for k, (p, w) in enumerate(lines)]


class RoadCutsTest(unittest.TestCase):
    def setUp(self):
        self.rl = _tile([([(0, -50), (0, 50)], 8.0), ([(-50, 0), (50, 0)], 6.0), ([(30, 30)], 4.0)])
        self.tr = roads._TileRoads(self.rl)

    def test_list_view(self):
        """The cuts read as the list of (line, half width) they stand for: the kept roads in order, then the extra."""
        extra = [(np.array([[5.0, 5.0], [6.0, 6.0]]), 1.0)]
        rc = roads._RoadCuts(self.tr, np.array([False, True, True]), self.tr.half + 0.5, extra)
        items = list(rc)
        self.assertEqual(len(rc), 3)
        self.assertEqual([h for _l, h in items], [3.5, 2.5, 1.0])
        self.assertIs(items[2][0], extra[0][0])
        self.assertTrue(rc)
        self.assertFalse(roads._RoadCuts(self.tr, np.zeros(3, bool), self.tr.half))
        self.assertEqual(len(rc + [(np.zeros((2, 2)), 1.0)]), 4)

    def test_cut_outside_as_the_list(self):
        """cut_outside cuts a line at the kept roads only, the same pieces as with the list of tuples."""
        pts = np.array([[-40.0, 10.0, 0.0], [40.0, -10.0, 0.0]])
        rc = roads._RoadCuts(self.tr, np.array([True, False, True]), self.tr.half + roads.CUT_MARGIN)
        a = roads.cut_outside(pts, rc, 0.5)
        b = roads.cut_outside(pts, list(rc), 0.5)
        self.assertEqual(len(a), 2)                     # (cut where it crosses the north-south road only)
        self.assertEqual(len(a), len(b))
        for x, y in zip(a, b):
            np.testing.assert_array_equal(x, y)
        self.assertEqual(roads.cut_outside(pts, roads._RoadCuts(self.tr, np.zeros(3, bool), self.tr.half), 0.5)[0]
                         .tolist(), pts.tolist())       # (no cut: the line as it is)


class DeckFloorTest(unittest.TestCase):
    def test_under_a_deck(self):
        """A point under a deck over the road reads the deck's underside; beside it or under a low deck: +inf."""
        decks = [{"n": [[0.0, -20.0, 10.0], [0.0, 20.0, 10.0]], "w": 10.0},
                 {"n": [[50.0, -20.0, 1.0], [50.0, 20.0, 1.0]], "w": 10.0}]
        f = roads._deck_floor(np.array([[2.0, 0.0], [30.0, 0.0], [50.0, 0.0]]), np.zeros(3), decks)
        self.assertAlmostEqual(f[0], 10.0 - roads.WALL_DECK_UNDER_M)
        self.assertTrue(np.isinf(f[1]) and np.isinf(f[2]))
        self.assertTrue(np.isinf(roads._deck_floor(np.zeros((1, 2)), np.zeros(1), [])).all())


class OffsetPolylineTest(unittest.TestCase):
    def test_left_and_mitre(self):
        """Moved to the left, z kept; a right-angle corner's mitre sqrt(2) out; per-point offsets."""
        p = np.array([[0.0, 0.0, 5.0], [10.0, 0.0, 6.0], [10.0, 10.0, 7.0]])
        o = roads.offset_polyline(p, 1.0)
        np.testing.assert_allclose(o[0], [0.0, 1.0, 5.0])
        np.testing.assert_allclose(o[1], [9.0, 1.0, 6.0])
        np.testing.assert_allclose(o[2], [9.0, 10.0, 7.0])
        o2 = roads.offset_polyline(p, np.array([0.0, 2.0, 1.0]))
        np.testing.assert_allclose(o2[1], [8.0, 2.0, 6.0])
        np.testing.assert_array_equal(roads.offset_polyline(p[:1], 3.0), p[:1])


class PointMemoTest(unittest.TestCase):
    def test_as_a_dict(self):
        m = railways._point_memo()
        k = np.array([[1, 2], [3, 4], [1, 2]], np.int64)
        self.assertTrue(np.isnan(m.get(k)).all())
        m.put(k, np.array([1.0, 2.0, 3.0]))             # (a later one of the same key wins)
        np.testing.assert_array_equal(m.get(k), [3.0, 2.0, 3.0])
        self.assertEqual(len(m), 2)
        m.clear()
        self.assertEqual(len(m), 0)


class CrossingsTest(unittest.TestCase):
    def test_lists(self):
        """Crossings of lines given as lists or float32 arrays, per ground line."""
        deck = [[[-10.0, 0.0], [10.0, 0.0]]]
        ground = [np.array([[0.0, -5.0], [0.0, 5.0]], np.float32), [[20.0, -5.0], [20.0, 5.0]]]
        by = roads._crossings_by(deck, ground)
        self.assertEqual(len(by), 2)
        self.assertEqual(len(by[0]), 1)
        self.assertEqual(by[1], [])
        pi, arc, xy = by[0][0]
        self.assertEqual(pi, 0)
        self.assertAlmostEqual(arc, 10.0)
        np.testing.assert_allclose(xy, [0.0, 0.0])
        self.assertEqual(len(roads._crossings(deck, ground)), 1)
        self.assertEqual(roads._crossings([], ground), [])


if __name__ == "__main__":
    unittest.main()
