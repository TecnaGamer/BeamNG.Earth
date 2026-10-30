"""markings: country styles, real dashes and the lines lane by lane from OpenStreetMap's lane tags."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import markings  # noqa: E402
import roads  # noqa: E402


class StyleTest(unittest.TestCase):
    def test_countries(self):
        self.assertEqual(markings.style("US").centre, "line_yellow")
        self.assertEqual(markings.style("NO").centre, "line_yellow", "Norway paints its centre lines yellow")
        self.assertEqual(markings.style("DE").centre_rural, (6.0, 12.0))
        self.assertEqual(markings.style("GB").lane_urban, (1.0, 5.0))
        self.assertEqual(markings.style(None, -100.0).centre, "line_yellow", "unknown: by longitude")
        self.assertEqual(markings.style(None, 10.0).centre, "line_white")


class DashTest(unittest.TestCase):
    def test_dashes_follow_the_line_and_its_phase(self):
        pts = np.array([[0.0, 0, 0], [10, 0, 1], [10, 20, 2]])
        d = markings.dashes(pts, (3.0, 9.0))
        self.assertEqual(len(d), 3)
        np.testing.assert_allclose(d[0][[0, -1]], [[0, 0, 0], [3, 0, 0.3]])
        np.testing.assert_allclose(d[1][[0, -1], :2], [[10, 2], [10, 5]], err_msg="round the corner")
        moved = markings.dashes(pts, (3.0, 9.0), phase=11.0)
        np.testing.assert_allclose(moved[0][0, :2], [1, 0], err_msg="the cycle 11 m on: 1 m into the next dash")


class LanePlanTest(unittest.TestCase):
    def test_none_without_tags(self):
        self.assertIsNone(markings.lane_plan({}, True, 7.0, 2))
        self.assertIsNone(markings.lane_plan(None, True, 7.0, 2))

    def test_uneven_directions_move_the_centre_line(self):
        plan = markings.lane_plan({"lanes:forward": "2", "lanes:backward": "1"}, True, 9.9, 3)
        self.assertEqual([r for _, r, _ in plan], ["centre", "lane"])
        self.assertAlmostEqual(plan[0][0], 1.65, "one lane left of it, two right (right-hand traffic)")
        lh = markings.lane_plan({"lanes:forward": "2", "lanes:backward": "1"}, True, 9.9, 3, left_hand=True)
        self.assertAlmostEqual(lh[-1][0], -1.65, msg="driving on the left: mirrored")

    def test_two_way_turn_lane_and_passing(self):
        plan = markings.lane_plan({"lanes:both_ways": "1"}, True, 15.0, 5)
        self.assertEqual([(round(o, 2), r, h) for o, r, h in plan],
                         [(4.5, "lane", False), (1.5, "twltl", 1), (-1.5, "twltl", -1), (-4.5, "lane", False)])
        self.assertEqual(markings.lane_plan({"overtaking": "no"}, True, 7.0, 2)[0][2], "double")
        self.assertEqual(markings.lane_plan({"overtaking:forward": "no"}, True, 7.0, 2)[0][2], "solid_right")

    def test_change_lanes_makes_lines_solid(self):
        plan = markings.lane_plan({"change:lanes": "no|yes|yes"}, False, 10.5, 3)
        self.assertEqual([h for _, _, h in plan], [True, False])

    def test_extras_draws_the_plan(self):
        sp = roads.Spec(10.0, "paved", False, "primary", False)
        line = np.array([[0.0, 0, 0], [100, 0, 0]])
        lk = [(lk.get("kind"), round(float(ln[0][1]), 2)) for lk, w, ln in
              roads.extras(sp, line, -76.0, country="US", osm={"lanes:forward": "1", "lanes:backward": "1",
                                                                "overtaking": "no"}) if lk.get("kind") == "centre"]
        self.assertEqual(sorted(set(lk)), [("centre", -0.12), ("centre", 0.12)], "a double solid yellow line")


class AiLanesTest(unittest.TestCase):
    def test_the_ai_drives_the_painted_lanes(self):
        import navgraph
        for sp in (roads.Spec(12.8, "paved", True, "tertiary", False), roads.Spec(13.2, "paved", False, "primary", False),
                   roads.Spec(11.0, "paved", False, "motorway", True, 2.5, 1)):
            self.assertAlmostEqual(navgraph.driving_width(sp), roads.lane_layout(sp)[0])


if __name__ == "__main__":
    unittest.main()


class GoreTest(unittest.TestCase):
    def test_an_exit_gets_chevrons_pointing_back_at_traffic(self):
        main = roads.Spec(11.0, "paved", False, "motorway", True, 2.5, 1)
        ramp = roads.Spec(6.0, "paved", False, "motorway", True, 2.5, 1, link=True)
        mpts = np.array([[0.0, 0, 0], [400, 0, 0]])
        # the ramp leaves the carriageway's centre line at x = 100 and drifts right (south) by 1 m every 10 m
        rx = np.arange(100.0, 401.0, 10.0)
        rpts = np.c_[rx, -(rx - 100) / 10.0, np.zeros(len(rx))]
        g = markings.gores([("m", main, mpts, 0.0), ("r", ramp, rpts, 0.0)])
        stripes = [l for k, _, l in g if k == "stripe"]
        self.assertTrue(any(k == "channel" for k, _, _ in g))
        self.assertGreater(len(stripes), 4)
        a, apex = stripes[0]
        self.assertLess(apex[0], a[0], "the point toward the traffic coming from the split")
        for l in stripes:
            self.assertTrue(np.all(l[:, 1] <= -main.width / 2 + 1e-6), "in the wedge, off the carriageway")


class ArrowTest(unittest.TestCase):
    def test_turn_lanes_get_two_arrows_each_before_the_end(self):
        pts = np.array([[0.0, 0, 0], [100, 0, 0]])
        a = markings.arrows({"turn:lanes": "left|through|through;right"}, pts, False, 9.9, 3)
        self.assertEqual(len(a), 6)
        self.assertEqual(sorted({k for _, _, k in a}), ["left", "through"])
        lefts = sorted(round(float(c[0]), 1) for c, _, k in a if k == "left")
        self.assertEqual(lefts, [55.0, 88.0], "12 m and 45 m before the end")
        self.assertTrue(all(abs(float(c[1]) - 3.3) < 1e-6 for c, _, k in a if k == "left"), "the leftmost lane")
        self.assertEqual(markings.arrows({"turn:lanes": "through|through"}, pts, False, 6.6, 2), [], "no turn lane")

    def test_backward_lanes_point_back(self):
        pts = np.array([[0.0, 0, 0], [100, 0, 0]])
        a = markings.arrows({"turn:lanes:backward": "left|through", "lanes:forward": "1"}, pts, True, 9.9, 3)
        self.assertTrue(a and all(t[0] < 0 for _, t, _ in a), "travelling against the way")
        self.assertTrue(all(float(c[1]) > -1e-6 for c, _, _ in a), "on the way's left (driving on the right)")
        lt = [float(c[1]) for c, _, k in a if k == "left"]
        self.assertTrue(lt and all(abs(y) < 1e-6 for y in lt), "the left-turn lane next to the centre line")

    def test_quad_uv_is_one_cell(self):
        corners, uv = markings.arrow_quad(np.zeros(3), np.array([1.0, 0.0]), "right")
        self.assertAlmostEqual(float(corners[0][0]), 2.0)
        # the sheet's top row (the shape's V runs up from the bottom: 0.75 .. 1), the tip (corners 0, 1) at its top; the
        # bottom row is the game 0.40 sheet's "EXIT" (it showed upside down where straight arrows belong, 2026-10-29)
        self.assertEqual(uv.min(0).tolist(), [0.25, 0.75])
        self.assertEqual(uv.max(0).tolist(), [0.5, 1.0])
        self.assertEqual(uv[0][1], 1.0)
