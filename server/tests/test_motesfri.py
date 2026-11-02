"""motesfri.py: the cable barrier along Sweden's 2+1 roads: which ways get one (never a junction's turn pocket), where it
stands, how it crosses over where the 2-lane side changes, its openings, the same across tile seams."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import guardrails  # noqa: E402
import mapgeom  # noqa: E402
import medians  # noqa: E402
import motesfri  # noqa: E402
import railtags  # noqa: E402
import roads  # noqa: E402
from test_roads import features  # noqa: E402

BOX = (-128.0, -128.0, 128.0, 128.0)
TWO = {"lanes:forward": "2", "lanes:backward": "1"}
ONE = {"lanes:forward": "1", "lanes:backward": "2"}


def spec(lanes=3, cls="primary", kmh=90.0, oneway=False, width=13.0, link=False):
    return roads.Spec(width, "paved", False, cls, oneway, 2.5, 0, kmh, None, None, lanes, None, link)


def line(y0, y1, x=0.0, n=30):
    y = np.linspace(y0, y1, n)
    return np.c_[np.full(n, x), y]


def way(fid, xy, tags=TWO, sp=None, key=None):
    return motesfri.Way(fid, sp if sp is not None else spec(), tags, xy, key or fid)


def shift_of(m):
    """x of each module's middle by its y."""
    mid = (m[:, 2:4] + m[:, 4:6]) / 2
    order = np.argsort(mid[:, 1])
    return mid[order, 1], mid[order, 0]


class WhichWaysTest(unittest.TestCase):
    def test_section(self):
        self.assertTrue(motesfri.is_section(spec(), TWO))
        self.assertTrue(motesfri.is_section(spec(), ONE))

    def test_not_a_section(self):
        self.assertFalse(motesfri.is_section(spec(), {"lanes:forward": "2", "lanes:backward": "2"}))
        self.assertFalse(motesfri.is_section(spec(lanes=2), TWO))
        self.assertFalse(motesfri.is_section(spec(kmh=70.0), TWO))              # below 80
        self.assertFalse(motesfri.is_section(spec(kmh=None), TWO))              # unknown limit: no guess
        self.assertFalse(motesfri.is_section(spec(cls="secondary"), TWO))
        self.assertFalse(motesfri.is_section(spec(oneway=True), TWO))
        self.assertFalse(motesfri.is_section(spec(link=True), TWO))

    def test_a_turn_pocket_is_no_section(self):
        self.assertFalse(motesfri.is_section(spec(), {**TWO, "turn:lanes:forward": "left|through"}))
        self.assertTrue(motesfri.is_section(spec(), {**TWO, "turn:lanes:forward": "merge_to_right|"}))    # a lane drop
        self.assertFalse(motesfri.is_section(spec(), {**TWO, "turn:lanes:forward": "through|right"}))
        self.assertTrue(motesfri.is_section(spec(), {**TWO, "turn:lanes:forward": "through|through"}))

    def test_short_way_is_a_pocket_but_cut_ways_count_together(self):
        self.assertEqual(len(motesfri.modules([way("a", line(-60, 60))], BOX)), 0)
        cut = [way("a", line(-120, 0), key="w1"), way("b", line(0, 120), key="w1")]
        self.assertGreater(len(motesfri.modules(cut, BOX)), 70)

    def test_only_sweden(self):
        near = [("a", spec(), np.zeros((2, 3)), 0.0)]
        self.assertTrue(motesfri.wanted("se", near))
        self.assertFalse(motesfri.wanted("NO", near))
        self.assertFalse(motesfri.wanted(None, near))
        self.assertFalse(motesfri.wanted("SE", [("a", spec(kmh=50.0), np.zeros((2, 3)), 0.0)]))


class SegmentOfSeveralWaysTest(unittest.TestCase):
    """Overture merges the ways between two junctions: a 2+1 road's sections alternate along one segment (väg 41)."""

    def feats(self, ways, flags=()):
        f = features([[(0.0, 0.0), (0.0, 1800.0)]], ["primary"], subtypes=["road"])
        f.attrs[railtags.LANE_PARTS] = np.array([None], object)
        f.attrs[railtags.LANE_PARTS][0] = ways
        f.rules["flags"] = mapgeom.rule_table(list(flags))
        return f

    def test_cut_where_the_way_changes(self):
        got = motesfri._ground_stretches(self.feats([(0.0, 0.5, "1", TWO), (0.5, 1.0, "2", ONE)]), 0)
        self.assertEqual([(w, t) for _r, w, t in got], [("1", TWO), ("2", ONE)])
        self.assertTrue(np.allclose(got[0][0][-1], got[1][0][0]))
        lengths = [np.hypot(*(r[-1] - r[0])) for r, _w, _t in got]
        self.assertAlmostEqual(lengths[0], lengths[1], places=6)

    def test_a_bridge_cuts_too(self):
        f = self.feats([(0.0, 0.5, "1", TWO), (0.5, 1.0, "2", ONE)], [(0, "is_bridge", [0.4, 0.6])])
        got = motesfri._ground_stretches(f, 0)
        self.assertEqual([w for _r, w, _t in got], ["1", "2"])
        whole = np.hypot(*(f.lonlat[-1] - f.lonlat[0]))
        for r, _w, _t in got:
            self.assertAlmostEqual(np.hypot(*(r[-1] - r[0])) / whole, 0.4, places=3)

    def test_the_ways_own_lane_count(self):
        self.assertTrue(motesfri.is_section(spec(lanes=2), {**TWO, "lanes": "3"}))
        self.assertFalse(motesfri.is_section(spec(lanes=3), {**TWO, "lanes": "4"}))
        plain = way("a", line(0, 900), {"lanes": "2", "overtaking": "no"}, spec(lanes=3))
        self.assertEqual(motesfri.centre_offset(plain), 0.0, "a 2-lane way of a segment that is mostly 3")


class WhereTest(unittest.TestCase):
    def test_on_the_line_between_the_directions(self):
        m = motesfri.modules([way("a", line(-400, 400))], BOX)
        self.assertTrue((m[:, 0] == medians.CABLE).all())
        self.assertGreater(len(m), 80)
        drive, _ = roads.lane_layout(spec())
        # drawn north, right-hand traffic: forward's two lanes on the right, so the line is a lane left of the middle...
        np.testing.assert_allclose(m[:, 2], -drive / 6, atol=1e-6)
        # ... and with the two lanes backward, a lane right of it
        m = motesfri.modules([way("a", line(-400, 400), ONE)], BOX)
        np.testing.assert_allclose(m[:, 2], drive / 6, atol=1e-6)

    def test_left_hand_traffic_mirrors(self):
        a = motesfri.modules([way("a", line(-400, 400))], BOX)
        b = motesfri.modules([way("a", line(-400, 400))], BOX, left_hand=True)
        np.testing.assert_allclose(a[:, 2], -b[:, 2], atol=1e-6)

    def test_no_gaps_between_modules(self):
        m = motesfri.modules([way("a", line(-400, 400))], BOX)
        ys = np.sort(np.r_[m[:, 3], m[:, 5]])
        self.assertLessEqual(np.diff(ys).max(), medians.MODULE_M + 1e-6)

    def test_rows_are_the_cable_group(self):
        m = motesfri.modules([way("a", line(-400, 400))], BOX)
        import fast
        z = np.zeros(len(m))
        rows = fast._ef.median_rows(np.ascontiguousarray(m), z, z)
        self.assertEqual(set(rows[:, 0].astype(int).tolist()), {guardrails.G_CABLE})

    def test_seams(self):
        ways = [way("a", line(-1000, 1000)), way("b", line(1000, 2000), ONE)]
        whole = motesfri.modules(ways, (-256.0, -1000.0, 256.0, 2000.0))
        parts = [motesfri.modules(ways, (-256.0, y, 256.0, y + 256.0)) for y in np.arange(-1000.0, 2000.0, 256.0)]
        tiled = np.concatenate(parts)
        self.assertEqual(len(whole), len(tiled))
        np.testing.assert_allclose(np.sort(whole[:, 3]), np.sort(tiled[:, 3]), atol=1e-9)


class ShiftTest(unittest.TestCase):
    def check(self, ways):
        m = motesfri.modules(ways, (-256.0, -1000.0, 256.0, 1000.0))
        y, x = shift_of(m)
        slope = np.abs(np.diff(x) / np.diff(y))
        self.assertLessEqual(slope.max(), 1.0 / motesfri.SHIFT_RATIO + 1e-3)    # VGU's 1:20
        self.assertLess(np.abs(np.diff(x)).max(), 0.2)                          # no step
        return y, x

    def test_direct_switch_stays_within_1_to_20(self):
        y, x = self.check([way("a", line(-900, 0)), way("b", line(0, 900), ONE)])
        drive, _ = roads.lane_layout(spec())
        self.assertAlmostEqual(float(x[0]), -drive / 6, places=6)
        self.assertAlmostEqual(float(x[-1]), drive / 6, places=6)
        self.assertLess(abs(float(np.interp(0.0, y, x))), 0.05)                 # meets in the middle

    def test_way_drawn_the_other_way_round(self):
        back = line(0, 900)[::-1]               # drawn south: its lanes:forward (2) now run south
        self.check([way("a", line(-900, 0)), way("b", back, TWO)])

    def test_connector_goes_through_the_middle(self):
        ways = [way("a", line(-900, -150)), way("c", line(-150, 150), {}, spec(lanes=2)),
                way("b", line(150, 900), ONE)]
        y, x = self.check(ways)
        self.assertLess(abs(float(np.interp(0.0, y, x))), 1e-6)

    def test_plain_road_past_a_section_gets_none(self):
        ways = [way("a", line(-900, -150)), way("c", line(-150, 400), {}, spec(lanes=2))]
        m = motesfri.modules(ways, (-256.0, -1000.0, 256.0, 1000.0))
        self.assertLess(((m[:, 3] + m[:, 5]) / 2).max(), -149.0)

    def test_junction_area_is_chained(self):
        pocket = {"lanes:forward": "2", "lanes:backward": "2", "turn:lanes:forward": "left|through"}
        ways = [way("a", line(-900, -150)), way("p", line(-150, -50), pocket, spec(lanes=4)),
                way("q", line(-50, 60), {**ONE, "turn:lanes:backward": "left|through"}),
                way("c", line(60, 150), {}, spec(lanes=2)), way("b", line(150, 900), ONE)]
        m = motesfri.modules(ways, (-256.0, -1000.0, 256.0, 1000.0))
        y = np.sort((m[:, 3] + m[:, 5]) / 2)
        self.assertLessEqual(np.diff(y).max(), medians.MODULE_M + 0.1)          # continuous through all four

    def test_long_gap_is_no_connector(self):
        half = motesfri.CONNECTOR_MAX_M / 2 + 50.0
        ways = [way("a", line(-half - 900, -half)), way("c", line(-half, half), {}, spec(lanes=2)),
                way("b", line(half, half + 900), ONE)]
        self.assertEqual(len(motesfri.modules(ways, (-256.0, -256.0, 256.0, 256.0))), 0)

    def test_chain_does_not_turn_into_a_side_road(self):
        ways = [way("a", line(-900, -150)), way("c", line(-150, 0), {}, spec(lanes=2)),
                way("b", np.array([[0.0, 0.0], [900.0, 0.0]]), ONE)]       # 90 degrees at the joint
        self.assertEqual(len(motesfri.modules(ways, (-64.0, -120.0, 64.0, -10.0))), 0)

    def test_slower_road_breaks_the_chain(self):
        ways = [way("a", line(-900, -150)), way("c", line(-150, 150), {}, spec(lanes=2, kmh=50.0)),
                way("b", line(150, 900), ONE)]
        self.assertEqual(len(motesfri.modules(ways, (-256.0, -100.0, 256.0, 100.0))), 0)


class OpeningTest(unittest.TestCase):
    def test_crossing_road_opens_it(self):
        side = way("x", np.array([[-300.0, 0.0], [300.0, 0.0]]), {}, spec(lanes=2, cls="tertiary", width=6.0))
        m = motesfri.modules([way("a", line(-400, 400)), side], BOX)
        mid = (m[:, 3] + m[:, 5]) / 2
        gap = 3.0 + medians.OPENING_M
        self.assertGreater(np.abs(mid).min(), gap)
        self.assertLess(np.abs(mid).min(), gap + medians.MODULE_M)
        self.assertGreater(len(m), 70)

    def test_a_track_does_not(self):
        track = way("x", np.array([[0.0, 0.0], [300.0, 0.0]]), {}, spec(lanes=None, cls="service", width=4.0))
        self.assertEqual(len(motesfri.modules([way("a", line(-400, 400)), track], BOX)),
                         len(motesfri.modules([way("a", line(-400, 400))], BOX)))

    def test_a_minor_road_at_a_section_does_not(self):
        side = way("x", np.array([[0.0, 0.0], [300.0, 0.0]]), {}, spec(lanes=2, cls="unclassified", width=5.5))
        self.assertEqual(len(motesfri.modules([way("a", line(-400, 400)), side], BOX)),
                         len(motesfri.modules([way("a", line(-400, 400))], BOX)))

    def test_a_minor_road_in_a_junction_area_does(self):
        area = way("area", line(-90, 90), {"lanes": "2"}, spec(lanes=2), key="w2")
        side = way("x", np.array([[0.0, 0.0], [300.0, 0.0]]), {}, spec(lanes=2, cls="unclassified", width=5.5))
        road = [way("a", line(-400, -90)), area, way("b", line(90, 400))]
        mid = (lambda m: (m[:, 3] + m[:, 5]) / 2)(motesfri.modules(road + [side], BOX))
        self.assertLess(np.abs((lambda m: (m[:, 3] + m[:, 5]) / 2)(motesfri.modules(road, BOX))).min(), 3.0)
        self.assertGreater(np.abs(mid).min(), 2.75 + medians.OPENING_M)

    def test_stub_between_two_openings_goes(self):
        sides = [way(f"x{i}", np.array([[-300.0, y], [300.0, y]]), {}, spec(lanes=2, cls="tertiary", width=6.0))
                 for i, y in enumerate((-20.0, 20.0))]
        m = motesfri.modules([way("a", line(-400, 400))] + sides, BOX)
        self.assertGreater(np.abs((m[:, 3] + m[:, 5]) / 2).min(), 20.0)

    def test_mapped_barrier_stands_for_it(self):
        ours = motesfri.modules([way("a", line(-400, 400))], BOX)
        rows = []
        for y in np.arange(-126.5, 0.0, 3.0):           # a mapped cable barrier down the road's centre, south half
            rows.append([guardrails.G_CABLE, 0.0, y, 0.0, 0, 0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0])
        m = motesfri.modules([way("a", line(-400, 400))], BOX, mapped_rows=rows)
        self.assertLess(len(m), len(ours) * 0.6)
        self.assertGreater(((m[:, 3] + m[:, 5]) / 2).min(), -2.0)

    def test_side_rail_is_not_a_centre_barrier(self):
        ours = motesfri.modules([way("a", line(-400, 400))], BOX)
        rows = [[guardrails.G_CABLE, 6.0, y, 0.0, 0, 0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]
                for y in np.arange(-126.5, 126.0, 3.0)]
        self.assertEqual(len(motesfri.modules([way("a", line(-400, 400))], BOX, mapped_rows=rows)), len(ours))


class LaneDropTest(unittest.TestCase):
    """M20: VGU 2024 12.2.1.5.3, table 12.14, before the place the overtaking lane ends."""
    PLAIN = {"lanes:forward": "1", "lanes:backward": "1"}

    def test_five_arrows_back_from_the_end_at_90(self):
        got = motesfri.lane_drops([way("s", line(-400, 0)), way("c", line(0, 400), self.PLAIN, spec(lanes=2))])
        self.assertEqual([round(-float(p[1]), 1) for p, _ in got], [2.5, 27.5, 77.5, 152.5, 252.5])
        off, travel = motesfri._overtaking(way("s", line(-400, 0)))
        self.assertEqual(travel, 1)
        for p, t in got:
            self.assertAlmostEqual(float(p[0]), -off, 6, "in the lane that ends")
            self.assertAlmostEqual(float(t[1]), 1.0, 6)

    def test_four_arrows_at_80(self):
        got = motesfri.lane_drops([way("s", line(-400, 0), sp=spec(kmh=80.0)), way("c", line(0, 400), self.PLAIN, spec(lanes=2, kmh=80.0))])
        self.assertEqual([round(-float(p[1]), 1) for p, _ in got], [2.5, 22.5, 62.5, 122.5])

    def test_the_lanes_may_run_the_other_way_along_the_geometry(self):
        got = motesfri.lane_drops([way("s", line(0, 400), ONE), way("c", line(-400, 0), self.PLAIN, spec(lanes=2))])
        self.assertEqual([round(float(p[1]), 1) for p, _ in got], [2.5, 27.5, 77.5, 152.5, 252.5])
        self.assertTrue(all(float(t[1]) < -0.99 for _, t in got), "heading south")

    def test_no_drop_where_the_two_lanes_go_on_and_arrows_past_a_joint(self):
        ways = [way("a", line(-500, -100), key="k"), way("b", line(-100, 0), key="k"), way("c", line(0, 400), self.PLAIN, spec(lanes=2))]
        self.assertEqual([round(-float(p[1]), 1) for p, _ in motesfri.lane_drops(ways)], [2.5, 27.5, 77.5, 152.5, 252.5])

    def test_nothing_read_past_it_no_guess(self):
        self.assertEqual(motesfri.lane_drops([way("s", line(-400, 0))]), [])

    def test_a_box_keeps_its_own_arrows(self):
        ways = [way("s", line(-400, 0)), way("c", line(0, 400), self.PLAIN, spec(lanes=2))]
        self.assertEqual(len(motesfri.drop_marks(ways, (-128.0, -128.0, 128.0, 128.0))), 3 * 5, "three arrows of five pieces")
        self.assertEqual(motesfri.drop_marks(ways, (-128.0, 128.0, 128.0, 384.0)), [])


if __name__ == "__main__":
    unittest.main()
