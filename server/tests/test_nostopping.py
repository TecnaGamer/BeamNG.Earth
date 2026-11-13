"""nostopping: Australia's yellow no-stopping edge lines on the approaches to signalised junctions."""
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nostopping  # noqa: E402
import roads  # noqa: E402
from test_roads import FRAME, features, flat  # noqa: E402

URBAN = lambda la, lo: np.ones(np.shape(la), bool)      # noqa: E731
C = 200.0       # the junction's middle, inside tile (0, 0)
SIGNAL = np.array([[C, C]])


def cross(classes=("secondary", "secondary", "residential", "residential")):
    return features([[(-100, C), (C, C)], [(C, C), (500, C)], [(C, -100), (C, C)], [(C, C), (C, 500)]], list(classes))


def yellow(country, signals, left_hand=True, feats=None):
    chunks = roads.build_tile(FRAME, 0, 0, [feats or cross()], flat, URBAN, walks=[], props=[], country=country,
                              left_hand=left_hand, signals=signals)
    return [np.asarray(c["n"], float) for c in chunks if c["m"] == "line_yellow"]


class PieceTest(unittest.TestCase):
    LINE = np.c_[np.arange(0.0, 101.0, 10.0), np.zeros(11), np.arange(11) * 0.1]

    def test_the_last_stretch_in_the_lines_own_order(self):
        a = nostopping._piece(self.LINE, True, 30.0)
        self.assertEqual((a[0, 0], a[-1, 0]), (0.0, 30.0))
        b = nostopping._piece(self.LINE, False, 30.0)
        self.assertEqual((b[0, 0], b[-1, 0]), (70.0, 100.0))
        self.assertAlmostEqual(b[0, 2], 0.7, 6, "its height interpolated too")

    def test_a_short_road_gives_each_end_at_most_half(self):
        a, b = (nostopping._piece(self.LINE[:4], s, 30.0) for s in (True, False))
        self.assertEqual((a[-1, 0], b[0, 0]), (15.0, 15.0))


class ApproachTest(unittest.TestCase):
    def setUp(self):
        self.roads = [("a", roads.Spec(10.0, "paved", True, "secondary", False), np.array([[0.0, 0, 0], [100, 0, 0]]), 0.0),
                      ("b", roads.Spec(10.0, "paved", True, "secondary", False), np.array([[100.0, 0, 0], [200, 0, 0]]), 0.0),
                      ("c", roads.Spec(8.0, "paved", True, "residential", False), np.array([[100.0, -100, 0], [100, 0, 0]]), 0.0)]
        self.at = np.array([[100.0, 0.0]])

    def test_every_inbound_kerbed_arm_on_the_drivers_left(self):
        got = nostopping.approaches(self.roads, self.at, lambda s: True)
        self.assertEqual(sorted(got), ["a", "b", "c"])
        self.assertEqual([side for side, _ in got["a"]], [1], "traffic along the geometry: its left is +1")
        self.assertEqual([side for side, _ in got["b"]], [-1], "traffic against it")
        self.assertEqual([side for side, _ in got["c"]], [1])
        self.assertEqual(got["b"][0][1][0, 0], 100.0, "from the junction outwards")

    def test_none_without_a_mapped_signal_or_a_kerb(self):
        self.assertEqual(nostopping.approaches(self.roads, None, lambda s: True), {})
        self.assertEqual(nostopping.approaches(self.roads, np.zeros((0, 2)), lambda s: True), {})
        self.assertEqual(nostopping.approaches(self.roads, np.array([[900.0, 900.0]]), lambda s: True), {})
        self.assertEqual(nostopping.approaches(self.roads, self.at, lambda s: False), {}, "a road with its own edge line")

    def test_one_way_roads_only_where_they_come_in(self):
        rs = [(f, s._replace(oneway=True, dir=1) if f == "b" else s, p, lon) for f, s, p, lon in self.roads]
        got = nostopping.approaches(rs, self.at, lambda s: True)
        self.assertNotIn("b", got, "b leaves the junction")
        rs = [(f, s._replace(oneway=True, dir=1) if f == "a" else s, p, lon) for f, s, p, lon in self.roads]
        self.assertIn("a", nostopping.approaches(rs, self.at, lambda s: True))

    def test_not_motorways_links_or_roundabouts(self):
        for sp in (self.roads[0][1]._replace(cls="motorway"), self.roads[0][1]._replace(link=True),
                   self.roads[0][1]._replace(kind="gravel")):
            rs = [("a", sp, self.roads[0][2], 0.0)] + self.roads[1:]
            self.assertNotIn("a", nostopping.approaches(rs, self.at, lambda s: True), str(sp))
        self.assertNotIn("a", nostopping.approaches(self.roads, self.at, lambda s: True, ring={"a"}))


class TileTest(unittest.TestCase):
    def test_a_yellow_line_on_the_left_of_each_arm_in_australia(self):
        lines = yellow("AU", SIGNAL)
        self.assertEqual(len(lines), 4)
        # (traffic keeps left: the west arm's comes east, so its kerb is the road's north side; the carriageways are
        # 12.1 m (secondary) and 10.6 m (residential) wide, the line 0.65 m inside the channel's inner edge)
        want = [(-22.0, 5.4), (22.0, -5.4), (-5.1, -22.0), (5.1, 22.0)]
        mid = [l[:, :2].mean(0) - (200.3, 199.5) for l in lines]      # (the junction's middle as the frame has it)
        for wx, wy in want:
            self.assertTrue(any(abs(m[0] - wx) < 4.0 and abs(m[1] - wy) < 1.0 for m in mid), (wx, wy, mid))
        for l in lines:
            self.assertLess(float(np.hypot(*(l[:, :2].mean(0) - (200.3, 199.5)))), nostopping.LENGTH_M)

    def test_no_signal_no_line_and_none_outside_australia(self):
        self.assertEqual(yellow("AU", None), [])
        self.assertEqual(yellow("AU", np.zeros((0, 2))), [])
        for country in ("US", "GB", "NZ", None):
            with mock.patch.object(nostopping, "approaches", side_effect=AssertionError("called")):
                yellow(country, SIGNAL, left_hand=country in ("GB", "NZ"))

    def test_an_unsignalised_junction_has_none(self):
        self.assertEqual(yellow("AU", np.array([[900.0, 900.0]])), [])


if __name__ == "__main__":
    unittest.main()
