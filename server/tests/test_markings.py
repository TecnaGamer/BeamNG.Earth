"""markings: country styles, real dashes and the lines lane by lane from OpenStreetMap's lane tags."""
import hashlib
import sys
import unittest
from unittest import mock
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
        self.assertEqual(markings.style("SE").centre_rural, (3.0, 9.0))


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

    def test_a_bus_lane_boundary_is_solid(self):
        plan = markings.lane_plan({"bus:lanes": "designated|no"}, False, 7.0, 2)
        self.assertEqual([h for _, _, h in plan], [True])
        same = markings.lane_plan({"bus:lanes": "designated|designated"}, False, 7.0, 2)
        self.assertEqual([h for _, _, h in same], [False], "two bus lanes share the ordinary broken line")
        psv = markings.lane_plan({"psv:lanes": "yes|no"}, False, 7.0, 2)
        self.assertEqual([h for _, _, h in psv], [True])
        two = markings.lane_plan({"lanes:forward": "2", "lanes:backward": "1",
                                  "bus:lanes:forward": "designated|no"}, True, 9.9, 3)
        self.assertEqual([(r, h) for _, r, h in two if r == "lane"], [("lane", True)])

    def test_extras_draws_the_plan(self):
        sp = roads.Spec(10.0, "paved", False, "primary", False)
        line = np.array([[0.0, 0, 0], [100, 0, 0]])
        lk = [(lk.get("kind"), round(float(ln[0][1]), 2)) for lk, w, ln in
              roads.extras(sp, line, -76.0, country="US", osm={"lanes:forward": "1", "lanes:backward": "1",
                                                                "overtaking": "no"}) if lk.get("kind") == "centre"]
        self.assertEqual(sorted(set(lk)), [("centre", -0.12), ("centre", 0.12)], "a double solid yellow line")


class BuswayStripeTest(unittest.TestCase):
    LINE = np.array([[0.0, 0, 0], [80.0, 0, 0]])

    def kinds(self, spec, country="NL", osm=None):
        return {lk.get("kind") for lk, _, _ in roads.extras(spec, self.LINE, 4.67, country=country, osm=osm)}

    def test_a_busway_is_edge_lined_where_a_style_lists_its_own_classes(self):
        us = markings.style("US")
        self.assertNotIn("busway", us.edge_classes)
        self.assertTrue(roads.paints_edge_line("busway", us))
        self.assertFalse(roads.paints_edge_line("tertiary", us))
        self.assertTrue(roads.paints_edge_line("busway", markings.style("NL")))

    def test_a_one_lane_busway_has_edge_lines_and_no_invented_dash(self):
        one = roads.Spec(7.0, "paved", False, "busway", True)
        for country in ("NL", "US"):
            kinds = self.kinds(one, country)
            self.assertIn("edge", kinds, country)
            self.assertNotIn("centre", kinds, country)
            self.assertNotIn("lane", kinds, country)
        town = self.kinds(roads.Spec(7.0, "paved", True, "busway", True))
        self.assertNotIn("lane", town, "the 7 m default is not a second lane")

    def test_a_two_way_busway_has_a_centre_line(self):
        kinds = self.kinds(roads.Spec(7.0, "paved", False, "busway", False))
        self.assertIn("centre", kinds)
        self.assertIn("edge", kinds)
        self.assertNotIn("lane", kinds)

    def test_mapped_lanes_are_dashed_and_a_bus_lane_on_a_road_is_solid(self):
        dashed = self.kinds(roads.Spec(7.0, "paved", False, "busway", True)._replace(lanes=2))
        self.assertIn("lane", dashed)
        self.assertNotIn("centre", dashed)
        sp = roads.Spec(10.0, "paved", False, "primary", True)._replace(lanes=2)
        drawn = [(lk.get("kind"), lk.get("dash", False)) for lk, _, _ in
                 roads.extras(sp, self.LINE, 4.67, country="NL", osm={"bus:lanes": "designated|no"})]
        self.assertIn(("lane", False), drawn, "the bus lane's boundary is solid")


class SwedenTest(unittest.TestCase):
    LINE = np.array([[0.0, 0, 0], [118, 0, 0]])     # (no dash ends right at its end)

    @staticmethod
    def lines(ex, kind):
        return [(round(float(w), 2), round(float(ln[0][1]), 2), round(float(np.hypot(*(ln[-1] - ln[0])[:2])), 1))
                for lk, w, ln in ex if lk.get("kind") == kind]

    def extras(self, width=9.0, cls="primary", urban=False, oneway=False, osm=None, kmh=None):
        sp = roads.Spec(width, "paved", urban, cls, oneway)._replace(kmh=kmh)
        return roads.extras(sp, self.LINE, 12.0, country="SE", osm=osm)

    def test_a_rural_road_has_broken_edge_lines(self):
        ex = self.extras()
        self.assertEqual({(w, n) for w, _, n in self.lines(ex, "edge")}, {(0.15, 1.0)}, "M2: 1 m dashes, 0.15 m")
        self.assertEqual(len(self.lines(ex, "edge")), 2 * 40, "1 + 2 m both sides")
        self.assertEqual({n for _, _, n in self.lines(ex, "centre")}, {3.0})
        self.assertEqual(len(self.lines(ex, "centre")), 10, "M1: 3 + 9 m")

    def test_a_town_road_is_1_to_1_and_narrower(self):
        got = self.lines(self.extras(urban=True), "centre")
        self.assertEqual({w for w, _, _ in got}, {0.10})
        self.assertEqual(len(got), 20)

    def test_a_motorway_edge_is_solid(self):
        self.assertEqual({(w, n) for w, _, n in self.lines(self.extras(14.0, "motorway", oneway=True), "edge")},
                         {(0.30, 118.0)})

    def test_no_overtaking_is_a_double_line_or_a_warning_line_on_a_narrow_road(self):
        osm = {"lanes:forward": "1", "lanes:backward": "1", "overtaking": "no"}
        self.assertEqual(sorted(self.lines(self.extras(osm=osm), "centre")), [(0.15, -0.15, 118.0), (0.15, 0.15, 118.0)])
        got = self.lines(self.extras(6.0, osm=osm), "centre")
        self.assertEqual(({n for _, _, n in got}, len(got)), ({9.0}, 10), "M3: 9 + 3 m")

    def test_a_2_plus_1_road_has_solid_lines_round_its_barrier(self):
        osm = {"lanes": "3", "lanes:forward": "2", "lanes:backward": "1"}
        ex = self.extras(11.0, osm=osm, kmh=100.0)
        centre = self.lines(ex, "centre")
        self.assertEqual({(w, n) for w, _, n in centre}, {(0.30, 118.0)}, "no centre line: the left edge lines")
        self.assertAlmostEqual(abs(centre[0][1] - centre[1][1]), 2 * markings.BARRIER_LINE_M, 1)
        self.assertEqual({(w, n) for w, _, n in self.lines(ex, "edge")}, {(0.20, 118.0)})
        self.assertEqual({n for _, _, n in self.lines(ex, "lane")}, {3.0})

    def test_a_2_plus_1_segments_lines_follow_each_way(self):
        class Feats:
            attrs = {}
            rules = {}
        sp = roads.Spec(11.0, "paved", False, "primary", False)._replace(kmh=100.0, lanes=3)
        a = {"lanes": "3", "lanes:forward": "2", "lanes:backward": "1"}
        b = {"lanes": "3", "lanes:forward": "1", "lanes:backward": "2"}
        parts = [(0.0, 0.5, "w1", a), (0.5, 1.0, "w2", b)]
        with mock.patch.object(roads, "lane_tag_parts", return_value=parts), \
                mock.patch.object(roads, "level_ranges", return_value=[]):
            ex = roads.way_lines(roads.extras(sp, self.LINE, 12.0, country="SE", osm=a), Feats, 0, sp, self.LINE, 12.0,
                                 False, "SE")
        mids = {round(float(ln[0][1]), 1) for lk, _, ln in ex if lk.get("kind") == "centre"}
        self.assertEqual(len(mids), 4, f"each way's two lines, on its own side of the middle: {mids}")


class AiLanesTest(unittest.TestCase):
    def test_the_ai_drives_the_painted_lanes(self):
        import navgraph
        for sp in (roads.Spec(12.8, "paved", True, "tertiary", False), roads.Spec(13.2, "paved", False, "primary", False),
                   roads.Spec(11.0, "paved", False, "motorway", True, 2.5, 1)):
            self.assertAlmostEqual(navgraph.driving_width(sp), roads.lane_layout(sp)[0])


class GiveWayLineTest(unittest.TestCase):
    """M14, TSFS 2010:171 4 kap. 7 §: triangles 0.70 m high on a 0.50 m base, 0.50 m apart."""

    def arm(self, width=8.0, oneway=False, wide=5.0):
        spec = roads.Spec(width, "paved", True, "residential", oneway)
        return ("a", spec, np.array([[0.0, 0.0, 3.0], [0.0, 60.0, 3.0]]), wide)    # the road runs north from the junction

    def test_triangles_across_the_lanes_coming_in(self):
        teeth, gone = markings.give_way_lines([self.arm()], [])
        self.assertEqual(gone, set())
        self.assertEqual(len(teeth), 4, "3.5 m of lane: four teeth at 1 m")
        pts = np.concatenate(teeth)
        self.assertTrue((pts[:, 0] < 0).all(), "traffic heading south keeps to the west half")
        self.assertAlmostEqual(pts[:, 1].min(), 6.0, 3, "1 m back from the crossing road's edge")
        self.assertAlmostEqual(pts[:, 1].max(), 6.7, 3, "0.70 m high")
        self.assertAlmostEqual(np.ptp(teeth[0][:2, 0]), 0.5, 6, "on a 0.50 m base")
        self.assertAlmostEqual(teeth[0][2, 1], 6.7, 3, "pointing at the drivers")
        self.assertTrue(np.allclose(np.diff(sorted(t[:, 0].mean() for t in teeth)), 1.0), "0.50 m apart")
        self.assertTrue((pts[:, 2] == 3.0).all())

    def test_left_hand_and_one_way(self):
        self.assertTrue((np.concatenate(markings.give_way_lines([self.arm()], [], True)[0])[:, 0] > 0).all())
        self.assertEqual(len(markings.give_way_lines([self.arm(oneway=True)], [])[0]), 7, "the whole width")

    def test_it_takes_the_place_of_a_stop_line_behind_a_crosswalk(self):
        at = lambda y, w, **kw: {"a": np.array([-4.0, y, 3.0]), "b": np.array([4.0, y, 3.0]), "w": w, **kw}
        teeth, gone = markings.give_way_lines([self.arm()], [at(9.0, 3.0), at(12.0, 0.45, stop=True)])
        self.assertEqual(gone, {1})
        self.assertGreaterEqual(np.concatenate(teeth)[:, 1].min(), 11.5)

    def test_a_stub_has_no_line(self):
        fid, spec, _, wide = self.arm()
        self.assertEqual(markings.give_way_lines([(fid, spec, np.array([[0.0, 0.0, 0.0], [0.0, 6.0, 0.0]]), wide)], [])[0], [])


class WaitLineTest(unittest.TestCase):
    """Germany's Wartelinie Z 341 where a 205 stands (VwV-StVO zu Zeichen 341 Rn. 1): a Breitstrich 0.50 m wide of
    0.50 m dashes 0.25 m apart (RMS 4.7.2), at the spot stop_lines stands a line."""

    def arm(self, width=8.0, oneway=False, wide=5.0, length=60.0):
        spec = roads.Spec(width, "paved", True, "residential", oneway)
        return ("a", spec, np.array([[0.0, 0.0, 3.0], [0.0, length, 3.0]]), wide)    # the road runs north from it

    def test_dashes_across_the_half_coming_in(self):
        dashes, cuts = markings.wait_lines([self.arm()], [])
        self.assertEqual(len(dashes), 5, "3.5 m of lane (0.2 m off the middle, 0.3 m off the edge): five dashes")
        for q in dashes:
            self.assertEqual(q.shape, (4, 3))
            self.assertAlmostEqual(np.ptp(q[:, 0]), 0.50, 6, msg="dashes 0.50 m long, across the lane")
            self.assertAlmostEqual(np.ptp(q[:, 1]), 0.50, 6, msg="a Breitstrich 0.50 m wide")
            self.assertTrue((q[:, 2] == 3.0).all())
        pts = np.concatenate(dashes)
        self.assertTrue((pts[:, 0] < 0).all(), "traffic heading south keeps to the west half")
        self.assertAlmostEqual(pts[:, 0].max(), -0.2, 6)
        self.assertAlmostEqual(pts[:, 0].min(), -3.7, 6)
        mids = sorted(float(q[:, 0].mean()) for q in dashes)
        self.assertTrue(np.allclose(np.diff(mids), 0.75), "0.25 m gaps")
        self.assertAlmostEqual(float(pts[:, 1].mean()), 6.0, 6, msg="1 m from the crossing road's edge (stop_lines')")
        self.assertEqual(list(cuts), ["a"])
        self.assertAlmostEqual(cuts["a"][0][2], 6.0, msg="the road's lines end at it")

    def test_whole_dashes_centred(self):
        dashes, _ = markings.wait_lines([self.arm(width=9.0)], [])
        pts = np.concatenate(dashes)
        self.assertEqual(len(dashes), 5, "4.0 m: five dashes (3.5 m), 0.25 m to spare each side")
        self.assertAlmostEqual(pts[:, 0].max(), -0.45, 6)
        self.assertAlmostEqual(pts[:, 0].min(), -3.95, 6)
        self.assertEqual(len(markings.wait_lines([self.arm(oneway=True)], [])[0]), 10, "a one-way: its whole width")

    def test_left_hand_mirror(self):
        right = np.concatenate(markings.wait_lines([self.arm()], [])[0])
        left = np.concatenate(markings.wait_lines([self.arm()], [], True)[0])
        self.assertTrue((left[:, 0] > 0).all(), "traffic heading south keeps to the east half")
        np.testing.assert_allclose(sorted(left[:, 0]), sorted(-right[:, 0]))

    def test_past_a_crossing(self):
        cw = {"a": np.array([-4.0, 9.0, 3.0]), "b": np.array([4.0, 9.0, 3.0]), "w": 3.0}
        dashes, cuts = markings.wait_lines([self.arm()], [cw])
        self.assertAlmostEqual(float(np.concatenate(dashes)[:, 1].mean()), 11.5, 6, msg="1 m past it (no car between)")
        self.assertAlmostEqual(cuts["a"][0][2], 11.5)

    def test_a_stub_gets_none(self):
        self.assertEqual(markings.wait_lines([self.arm(length=7.0)], []), ([], {}))


class StopLineWidthTest(unittest.TestCase):
    def test_germany_passes_its_haltlinie(self):
        spec = roads.Spec(8.0, "paved", True, "residential", False)
        arm = ("a", spec, np.array([[0.0, 0.0, 0.0], [0.0, 60.0, 0.0]]), 5.0, "stop")
        self.assertEqual(markings.stop_lines([arm], [])[0][0]["w"], markings.M13_W, "Sweden's M13 as it was")
        self.assertEqual(markings.stop_lines([arm], [], w=markings.Z294_W)[0][0]["w"], 0.50, "Z 294 (RMS 4.6)")


class M20Test(unittest.TestCase):
    def test_arrow_ends_at_its_tip_and_points_right_of_the_lane(self):
        polys = markings.m20((10.0, 20.0), (0.0, 1.0))       # heading north
        pts = np.concatenate(polys)
        self.assertAlmostEqual(pts[:, 1].max(), 20.0, 6, "its tip is the most forward")
        self.assertAlmostEqual(pts[:, 1].min(), 12.5, 6, "7.5 m long")
        tail = pts[pts[:, 1] < 13.0]
        tip = pts[np.argmax(pts[:, 1])]
        self.assertLess(tail[:, 0].mean(), tip[0], "the tail on the left: it turns right")
        self.assertAlmostEqual(np.ptp(tail[:, 0]), 0.30, 6, "0.30 m wide")


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


# fork: Australian line paint (barrier lines double, edge lines 0.15 m) must not touch any other country's.
class AustraliaPaintTest(unittest.TestCase):
    LINE = np.array([[0.0, 0, 0], [118, 0, 0]])
    S = roads.Spec
    CASES = {
        "rural2": (S(7.0, "paved", False, "primary", False), None),
        "rural4": (S(14.0, "paved", False, "primary", False)._replace(lanes=4), None),
        "urban2": (S(8.0, "paved", True, "secondary", False), None),
        "urban4": (S(14.0, "paved", True, "primary", False)._replace(lanes=4), None),
        "noovertake": (S(7.0, "paved", False, "primary", False),
                       {"lanes:forward": "1", "lanes:backward": "1", "overtaking": "no"}),
        "narrow_noovertake": (S(6.0, "paved", False, "primary", False),
                              {"lanes:forward": "1", "lanes:backward": "1", "overtaking": "no"}),
        "one_way_no": (S(7.0, "paved", False, "primary", False),
                       {"lanes:forward": "1", "lanes:backward": "1", "overtaking:forward": "no"}),
        "twltl": (S(15.0, "paved", True, "primary", False)._replace(lanes=5), {"lanes:both_ways": "1"}),
        "plan4": (S(14.0, "paved", False, "primary", False)._replace(lanes=4),
                  {"lanes:forward": "2", "lanes:backward": "2"}),
        "motorway": (S(14.0, "paved", False, "motorway", True)._replace(lanes=3), None),
        "tertiary": (S(6.5, "paved", False, "tertiary", False), None),
    }
    # digests of every country's rendered paint (both traffic sides) on the code before the Australian change
    UNCHANGED = {"US": "d7189f91f9d6", "CA": "b9fe9538afc1", "GB": "8a1142fca669", "IE": "8a1142fca669",
                 "DE": "00962800162e", "SE": "6f70f3fc55aa", "NZ": "e9b39ef79fc3", "FR": "4cfa53a33e2a",
                 "JP": "689676c68a9e", "NL": "01fa6692fbf4", "NO": "4a228b934bc4", "BR": "4a228b934bc4"}

    def digest(self, country):
        h = hashlib.sha1()
        for name, (sp, osm) in self.CASES.items():
            for lh in (False, True):
                ex = roads.extras(sp, self.LINE, 12.0, left_hand=lh, country=country, osm=osm)
                h.update(repr((name, lh, [(sorted(lk.items()), round(float(w), 4),
                                           [[round(float(v), 4) for v in p] for p in (ln[0], ln[-1])], len(ln))
                                          for lk, w, ln in ex])).encode())
        return h.hexdigest()[:12]

    def test_other_countries_paint_is_unchanged(self):
        for country, want in self.UNCHANGED.items():
            self.assertEqual(self.digest(country), want, f"{country}'s paint changed")

    def lines(self, country, kind, spec, osm=None):
        ex = roads.extras(spec, self.LINE, 150.0, country=country, osm=osm)
        return [(round(float(w), 2), round(float(ln[0][1]), 2), len(ln)) for lk, w, ln in ex
                if lk.get("kind") == kind and not lk.get("dash")]

    def test_a_no_overtaking_line_is_double_solid_white_in_australia(self):
        osm = {"lanes:forward": "1", "lanes:backward": "1", "overtaking": "no"}
        sp = self.S(7.0, "paved", False, "primary", False)
        ex = roads.extras(sp, self.LINE, 150.0, country="AU", osm=osm)
        centre = [(lk["m"], w, round(float(ln[0][1]), 2)) for lk, w, ln in ex if lk.get("kind") == "centre"]
        self.assertEqual(sorted(centre), [("line_white", 0.10, -0.10), ("line_white", 0.10, 0.10)],
                         "two 0.10 m lines 0.10 m apart (centre to centre 0.20 m)")
        self.assertEqual(self.lines("NZ", "centre", sp, osm), [(0.10, 0.0, 2)], "New Zealand keeps its single line")

    def test_an_undivided_four_lane_road_has_a_double_solid_line(self):
        sp = self.S(14.0, "paved", False, "primary", False)._replace(lanes=4)
        self.assertEqual(sorted(self.lines("AU", "centre", sp)), [(0.10, -0.10, 2), (0.10, 0.10, 2)])

    def test_a_two_lane_road_keeps_its_broken_centre_line_and_gets_0_15_edge_lines(self):
        sp = self.S(7.0, "paved", False, "primary", False)
        ex = roads.extras(sp, self.LINE, 150.0, country="AU")
        self.assertEqual({(w, lk.get("dash")) for lk, w, _ in ex if lk.get("kind") == "centre"}, {(0.10, True)})
        self.assertEqual({w for lk, w, _ in ex if lk.get("kind") == "edge"}, {0.15})
        self.assertEqual({w for lk, w, _ in roads.extras(sp, self.LINE, 150.0, country="NZ") if lk.get("kind") == "edge"},
                         {0.12})
