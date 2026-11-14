"""sesigns: Sweden's signs at the places a road's shape asks for them, and nowhere else (not in any other country)."""
import math
import os
import re
import sys
import unittest
import unittest.mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import secrossings  # noqa: E402
import geodesy  # noqa: E402
import guardrails  # noqa: E402
import motesfri  # noqa: E402
import roads  # noqa: E402
import sesigns  # noqa: E402

TREES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "mod", "beamng_earth", "lua", "ge",
                     "extensions", "beamngEarth", "trees.lua")
SPLIT = {"lanes": "3", "lanes:forward": "2", "lanes:backward": "1"}
PLAIN = {"lanes:forward": "1", "lanes:backward": "1"}


def spec(width=11.0, cls="trunk", oneway=False, kmh=100.0, lanes=3, link=False, dir_=1, urban=False):
    return roads.Spec(width, "paved", urban, cls, oneway, 2.5, dir_, kmh, None, None, lanes, None, link)


def way(fid, xy, tags=None, sp=None, key=None):
    return motesfri.Way(fid, sp or spec(), tags or {}, np.asarray(xy, np.float64), key or fid)


def north(a, b, x=0.0):
    return np.c_[np.full(9, x), np.linspace(a, b, 9)]


def pair(fid, xy, lanes, cls="trunk", link=False, kmh=100.0):
    return way(fid, xy, {}, spec(9.0 if lanes == 2 else 4.5, cls, True, kmh, lanes, link))


def of(signs, group):
    return [s for s in signs if s.group == group]


class CatalogTest(unittest.TestCase):
    def test_every_sign_has_a_group_and_a_model_of_the_same_height(self):
        with open(TREES, encoding="utf-8") as f:
            lua = f.read()
        block = lua[lua.index("local SE_SIGNS"):lua.index("for g, h in pairs(SE_SIGNS)")]
        got = {k: float(v) for k, v in re.findall(r"(se_\w+) = ([\d.]+)", block)}
        self.assertEqual(got, sesigns.HEIGHTS)
        for g in sesigns.HEIGHTS:
            self.assertIn(g, guardrails.GROUPS)
            self.assertTrue(os.path.exists(os.path.join(os.path.dirname(TREES), "..", "..", "..", "..", "levels",
                                                        "beamng_earth", "art", "shapes", "beamngEarth", f"bne_{g}.dae")), g)


class LaneEndTest(unittest.TestCase):
    def ways(self):
        return [way("s", north(-1000, 0), SPLIT, spec(lanes=3)), way("c", north(0, 400), PLAIN, spec(lanes=2))]

    def test_f17_before_the_arrows_and_400_m_before_them_on_both_sides_facing_the_lanes(self):
        got = sesigns.all_signs(self.ways())
        near, far = of(got, "se_f17"), of(got, "se_f17_400")
        self.assertEqual((len(near), len(far)), (2, 2))
        end = 100.0                                         # (the line stops 2/3 of 150 m before the road's end)
        arrows = end + motesfri.M20_FIRST_M + sum(motesfri.M20_GAPS_90)     # (100 km/h)
        self.assertAlmostEqual(float(near[0].p[1]), -(arrows + sesigns.ARROWS_LEAD_M), 3)
        self.assertAlmostEqual(float(far[0].p[1]), -(arrows + 400.0), 3)
        for s in near + far:
            self.assertGreater(float(s.facing[1]), 0.99, "the two lanes' traffic, going north")
        self.assertEqual(sorted(round(float(s.p[0]), 2) for s in near)[1], 11.0 / 2 + sesigns.SIDE_M, "right of it")
        self.assertLess(min(float(s.p[0]) for s in near), 0.0 + 1e-9 + 3.0, "the other in the barrier's line")

    def test_a_shorter_stretch_gets_a_nearer_plate(self):
        got = sesigns.all_signs([way("s", north(-700, 0), SPLIT, spec(lanes=3)), way("c", north(0, 400), PLAIN, spec(lanes=2))])
        self.assertEqual(of(got, "se_f17_400"), [])
        self.assertEqual(len(of(got, "se_f17_300")), 2)

    def test_a_junctions_short_lane_gets_none(self):
        got = sesigns.all_signs([pair("a", north(-120, 0), 2), pair("b", north(0, 400), 1)])
        self.assertEqual([s for s in got if s.group.startswith("se_f17")], [])

    def merging(self, lanes_before=None, ramp=False):
        """A 120 m two-lane carriageway going north, tagged as merging away, into one lane; before it lanes_before lanes
        of carriageway for 400 m (None: nothing), or ramp: one lane with a link road joining where the two lanes begin."""
        two = way("a", north(-120, 0), {"lanes": "2", "turn:lanes": "|merge_to_left"}, spec(9.0, "trunk", True, 100.0, 2))
        out = [two, pair("b", north(0, 400), 1)]
        if lanes_before:
            out.append(way("w", north(-520, -120), {}, spec(4.5 * lanes_before, "trunk", True, 100.0, lanes_before)))
        if ramp:
            out += [pair("m", north(-520, -120), 1),
                    pair("r", np.c_[np.linspace(30, 0, 9), np.linspace(-320, -120, 9)], 1, link=True)]
        return sesigns.all_signs(out)

    def test_a_short_lane_the_map_merges_away_gets_f17(self):
        got = self.merging()
        self.assertEqual(len(of(got, "se_f17")), 2)
        self.assertEqual([s for s in got if s.group.startswith("se_f17_")], [], "no room for a plate")
        y = float(of(got, "se_f17")[0].p[1])
        self.assertTrue(-120.0 < y <= -(64.0 + sesigns.NEAR_M), f"no room before the arrows: as far back as fits ({y})")

    def test_into_a_carriageway_with_no_lane_count_past_a_merge(self):
        got = sesigns.all_signs([way("a", north(-120, 0), {"lanes": "2", "turn:lanes": "merge_to_right|"},
                                     spec(9.0, "trunk", True, 100.0, 2)),
                                 way("b", north(0, 400), {}, spec(4.5, "trunk", True, 100.0, None))])
        self.assertEqual(len(of(got, "se_f17")), 2)

    def test_but_not_an_on_ramps_acceleration_lane(self):
        self.assertEqual([s for s in self.merging(ramp=True) if s.group.startswith("se_f17")], [])

    def test_two_lanes_go_on_back_beside_an_exits_lane(self):
        got = self.merging(lanes_before=3)
        self.assertEqual(len(of(got, "se_f17")), 2)
        self.assertEqual(len(of(got, "se_f17_200")), 2, "the plate on the three-lane way")

    def test_the_plate_goes_back_over_a_bridge(self):
        two = lambda fid, a, b: way(fid, north(a, b), {"lanes": "2"}, spec(9.0, "trunk", True, 100.0, 2), "k")
        got = sesigns.all_signs([two("k:0", -900, -330), two("k:1", -310, 0), pair("b", north(0, 400), 1)])
        far = of(got, "se_f17_400")
        self.assertEqual(len(far), 2, "past the 20 m the ground ways leave out")
        self.assertFalse(any(-330.0 < float(s.p[1]) < -310.0 for s in got), "none on the bridge")

    def test_a_carriageway_gets_them_too(self):
        got = sesigns.all_signs([pair("a", north(-800, 0), 2), pair("b", north(0, 400), 1)])
        self.assertEqual(len(of(got, "se_f17_400")), 2)
        left = min(float(s.p[0]) for s in of(got, "se_f17"))
        self.assertAlmostEqual(left, -(9.0 / 2 + sesigns.MEDIAN_M), 3)

    def test_two_lanes_going_on_get_none(self):
        got = sesigns.all_signs([way("a", north(-1000, 0), SPLIT, spec(lanes=3), "k"), way("b", north(0, 400), SPLIT, spec(lanes=3), "k")])
        self.assertEqual([s for s in got if s.group.startswith("se_f17")], [])


class SeparationTest(unittest.TestCase):
    def test_f18_where_a_plain_road_becomes_a_section(self):
        got = sesigns.all_signs([way("c", north(-400, 0), PLAIN, spec(lanes=2)), way("s", north(0, 1000), SPLIT, spec(lanes=3))])
        f18 = of(got, "se_f18_12")
        self.assertEqual(len(f18), 1, "two lanes this way")
        self.assertAlmostEqual(float(f18[0].p[1]), -sesigns.NEAR_M, 3)
        self.assertGreater(float(f18[0].facing[1]), 0.99)

    def test_one_lane_into_it(self):
        got = sesigns.all_signs([way("c", north(-400, 0), PLAIN, spec(lanes=2)),
                                 way("s", north(0, 1000), {"lanes": "3", "lanes:forward": "1", "lanes:backward": "2"}, spec(lanes=3))])
        self.assertEqual(len(of(got, "se_f18_21")), 1)

    def test_f18_where_a_road_forks_into_a_pair(self):
        got = sesigns.all_signs([way("c", north(-400, 0), PLAIN, spec(lanes=2)),
                                 pair("up", np.c_[np.linspace(0, 8, 9), np.linspace(0, 800, 9)], 1),
                                 way("dn", np.c_[np.linspace(-8, 0, 9), np.linspace(800, 0, 9)], {},
                                     spec(4.5, "trunk", True, 100.0, 1))])
        self.assertEqual(len(of(got, "se_f18_11")), 1)


class SpanishTest(unittest.TestCase):
    def main(self, cls="trunk", kmh=100.0, urban=False):
        return way("m", north(-1000, 1000), PLAIN, spec(11.0, cls, False, kmh, 2, urban=urban))

    def loop(self):
        """A one-way link off the right of a road going north, back round to cross it."""
        xy = np.c_[40.0 - 40.0 * np.cos(np.linspace(0.0, 1.5 * math.pi, 25)),
                   40.0 * np.sin(np.linspace(0.0, 1.5 * math.pi, 25))]
        return np.vstack([xy, [0.0, -40.0]])

    def test_a_link_coming_back_is_one(self):
        ways = [self.main(), way("l", self.loop(), {}, spec(6.0, "trunk", True, 50.0, 1, True))]
        found = sesigns.spanish(ways)
        self.assertEqual(len(found), 1)
        got = sesigns.all_signs(ways)
        self.assertEqual(len(of(got, "se_f2")), 1)
        f2 = of(got, "se_f2")[0]
        self.assertGreater(float(f2.facing[1]), 0.99, "traffic going north: the link leaves on its right")
        self.assertAlmostEqual(float(f2.p[1]), -sesigns.F2_M, 1)
        self.assertGreater(float(f2.p[0]), 0.0)
        self.assertEqual(len(of(got, "se_c1")), 2, "both sides of where it meets the two-way road")

    def test_a_plain_slip_road_is_not(self):
        xy = np.c_[np.linspace(0, 60, 9), np.linspace(0, 40, 9)]
        self.assertEqual(sesigns.spanish([self.main(), way("l", xy, {}, spec(6.0, "trunk", True, 50.0, 1, True))]), [])

    def test_a_service_road_is_not(self):
        self.assertEqual(sesigns.spanish([self.main(), way("l", self.loop(), {}, spec(6.0, "service", True, 30.0, 1))]), [])

    def test_a_town_street_is_not(self):
        ways = [self.main(kmh=50.0, urban=True), way("l", self.loop(), {}, spec(6.0, "trunk", True, 50.0, 1, True))]
        self.assertEqual(sesigns.spanish(ways), [])


class RampTest(unittest.TestCase):
    def test_exit_ramp_gets_c1_and_e2_at_its_end(self):
        ramp = np.c_[np.linspace(0, 200, 9), np.linspace(0, 200, 9)]
        ways = [pair("m", north(-800, 800), 2, "motorway"), pair("r", ramp, 1, "motorway", True, 70.0),
                way("t", np.c_[np.full(9, 200.0), np.linspace(-200, 600, 9)], PLAIN, spec(8.0, "secondary", False, 70.0, 2))]
        got = sesigns.all_signs(ways)
        c1 = of(got, "se_c1")
        self.assertEqual(len(c1), 2)
        self.assertLess(float(c1[0].facing[0]), -0.5, "facing whoever would drive up it")
        self.assertEqual(len(of(got, "se_e2")), 1)
        self.assertLess(float(np.hypot(*(of(got, "se_e2")[0].p - [200, 200]))), sesigns.RAMP_E_M + 5.0)

    def test_on_ramp_gets_e1_on_both_sides(self):
        ramp = np.c_[np.linspace(200, 0, 9), np.linspace(-200, 0, 9)]
        ways = [pair("m", north(-800, 800), 2, "motorway"), pair("r", ramp, 1, "motorway", True, 70.0),
                way("t", np.c_[np.full(9, 200.0), np.linspace(-600, 200, 9)], PLAIN, spec(8.0, "secondary", False, 70.0, 2))]
        self.assertEqual(len(of(sesigns.all_signs(ways), "se_e1")), 2)

    def test_a_ramp_between_motorways_has_no_e_sign(self):
        ramp = np.c_[np.linspace(0, 400, 9), np.linspace(0, 0, 9)]
        ways = [pair("m", north(-800, 800), 2, "motorway"), pair("r", ramp, 1, "motorway", True, 70.0),
                pair("n", north(-800, 800, 400.0), 2, "motorway")]
        got = sesigns.all_signs(ways)
        self.assertEqual(of(got, "se_e1") + of(got, "se_e2"), [])

    def test_a_motorway_begins_and_ends(self):
        ways = [way("a", north(-800, 0), PLAIN, spec(10.0, "trunk", False, 90.0, 2)), pair("m", north(0, 800), 2, "motorway"),
                way("b", north(800, 1600), PLAIN, spec(10.0, "trunk", False, 90.0, 2))]
        got = sesigns.all_signs(ways)
        self.assertEqual(len(of(got, "se_e1")), 2)
        self.assertEqual(len(of(got, "se_e2")), 2)

    def test_motorway_broken_by_a_bridge_is_not_an_end(self):
        got = sesigns.all_signs([pair("a", north(-800, 0), 2, "motorway"), pair("b", north(30, 800), 2, "motorway")])
        self.assertEqual(of(got, "se_e1") + of(got, "se_e2"), [])


class ZebraTest(unittest.TestCase):
    NEAR = [("s", spec(8.0, "residential", False, 50.0, 2, urban=True), north(-100, 100))]

    def test_two_diagonal_signs_each_on_its_right(self):
        got = sesigns.zebra_signs(self.NEAR, [(0.0, 0.0)])
        self.assertEqual(len(got), 2)
        ne, sw = sorted(got, key=lambda s: float(s.p[1]))[::-1]
        self.assertGreater(float(ne.p[1]), 0)
        self.assertLess(float(ne.p[0]), 0, "going south its right is west")
        self.assertGreater(float(sw.p[0]), 0)

    def test_none_on_a_fast_road(self):
        self.assertEqual(sesigns.zebra_signs([("s", spec(8.0, "secondary", False, 70.0, 2), north(-100, 100))], [(0.0, 0.0)]), [])

    def test_two_nodes_one_crossing(self):
        self.assertEqual(len(sesigns.zebra_signs(self.NEAR, [(0.0, 0.0), (0.0, 2.0)])), 2)

    def test_a_node_off_the_road(self):
        self.assertEqual(sesigns.zebra_signs(self.NEAR, [(9.0, 0.0)]), [])


class OnlySwedenTest(unittest.TestCase):
    """The whole of the check: nothing of this may reach another country's roads."""
    WAYS = [way("s", north(-1000, 0), SPLIT, spec(lanes=3)), way("c", north(0, 400), PLAIN, spec(lanes=2))]
    NEAR = [("s", spec(8.0, "residential", False, 50.0, 2, urban=True), north(-100, 100))]
    NODES = {"zebras": [(0.0, 0.0)]}

    def rows(self, country):
        frame = geodesy.EnuFrame(57.6, 12.9, 0.0)
        return sesigns.tile_rows(frame, self.WAYS, self.NEAR, self.NODES, (-2000.0, -2000.0, 2000.0, 2000.0),
                                 lambda la, lo: np.zeros(np.shape(la)), country)

    def test_sweden_has_them_in_their_group(self):
        rows = self.rows("SE")
        self.assertGreater(len(rows), 4)
        for r in rows:
            self.assertEqual(len(r), 17)
            self.assertTrue(guardrails.GROUPS[r[0]].startswith("se_"))

    def test_any_case(self):
        self.assertTrue(self.rows("se"))

    def test_no_other_country(self):
        for c in ("NO", "FI", "DK", "DE", "US", "GB", "", None):
            self.assertEqual(self.rows(c), [], c)
            self.assertFalse(sesigns.wanted(c, [("a", spec(), north(0, 9))]), c)

    def test_blockworker_stops_it_before_it_reads_anything(self):
        import blockworker
        def boom(_):
            raise AssertionError("read a cell")
        self.assertEqual(blockworker._sign_rows(None, boom, self.NEAR, self.NODES, (0, 0, 1, 1), None, None, None, False,
                                                "NO", 0, 0), [])

    def test_the_motesfri_hook_is_shut_to_them_too(self):
        import blockworker
        chunks = [{"m": "line_white", "n": [[0.0, 0.0, 0.0], [0.0, 9.0, 0.0]], "w": 0.1}]
        before = [dict(c) for c in chunks]
        near = [("a", spec(), north(0, 9))]
        def boom(_):
            raise AssertionError("read a cell")
        self.assertEqual(blockworker._motesfri_rows(None, boom, near, (0, 0, 1, 1), None, None, None, False, "NO", [], 0, 0,
                                                    chunks), [])
        self.assertEqual(chunks, before, "no line bent, no arrow added")


class HookTest(unittest.TestCase):
    """The hooks log and swallow what goes wrong, so a slip in them would only show as signs and barriers gone."""

    def test_the_hooks_run_through_without_a_word(self):
        import blockworker
        frame = geodesy.EnuFrame(57.6, 12.9, 0.0)
        near = [("a", spec(), north(0, 9))]
        box = roads.tile_bbox(0, 0)
        flat = lambda la, lo: np.zeros(np.shape(la))        # noqa: E731
        calls = []
        with self.assertNoLogs("earthd.blockworker", level="WARNING"):
            wide = {}
            blockworker._motesfri_rows(frame, lambda k: calls.append(k), near, box, flat, None, None, False, "SE", [], 0, 0,
                                       [], wide)
            read = len(calls)
            self.assertGreater(read, 0)
            self.assertEqual(blockworker._sign_rows(frame, lambda k: calls.append(k), near, {"zebras": []}, box, flat, None,
                                                    None, False, "SE", 0, 0, wide), [])
        self.assertEqual(len(calls), read, "the wider cells are read once")


class NodesTest(unittest.TestCase):
    def test_a_painted_crossing_counts_but_not_one_with_signals_or_no_paint(self):
        self.assertTrue(secrossings.paint({"crossing": "uncontrolled"}) & secrossings.M15)
        self.assertTrue(secrossings.paint({}) & secrossings.M15)
        self.assertFalse(secrossings.paint({"crossing": "unmarked"}) & secrossings.M15)


if __name__ == "__main__":
    unittest.main()


class MooseTest(unittest.TestCase):
    def roads(self, **kw):
        sp = spec(8.0, kw.get("cls", "secondary"), kw.get("oneway", False), kw.get("kmh", 80.0), 2,
                  urban=kw.get("urban", False))
        return [("r", sp, np.c_[np.zeros(9), np.linspace(0, kw.get("length", 3000.0), 9), np.zeros(9)], 13.0)]

    def test_a_chosen_road_gets_one_each_way_facing_its_traffic(self):
        with unittest.mock.patch.object(sesigns, "MOOSE_SPACING_M", 1.0):
            got = sesigns.moose_signs(self.roads())
        self.assertEqual(len(got), 2)
        north_, south_ = sorted(got, key=lambda s: float(s.facing[1]), reverse=True)
        self.assertAlmostEqual(float(north_.p[1]), sesigns.MOOSE_FROM_M, 3)
        self.assertAlmostEqual(float(north_.p[0]), 4.0 + sesigns.MOOSE_SIDE_M, 3, "on the northbound traffic's right")
        self.assertAlmostEqual(float(south_.p[1]), 3000.0 - sesigns.MOOSE_FROM_M, 3)
        self.assertLess(float(south_.p[0]), 0.0)

    def test_the_choice_is_the_roads_own(self):
        self.assertEqual(sesigns._chance("r"), sesigns._chance("r"))
        picked = sum(sesigns._chance(f"road{k}") < 0.2 for k in range(2000))
        self.assertTrue(300 < picked < 500, picked)

    def test_not_in_town_nor_on_fast_short_or_one_way_roads(self):
        with unittest.mock.patch.object(sesigns, "MOOSE_SPACING_M", 1.0):
            for kw in ({"urban": True}, {"cls": "motorway"}, {"kmh": 110.0}, {"kmh": 50.0}, {"length": 900.0},
                       {"oneway": True}, {"cls": "residential"}):
                self.assertEqual(sesigns.moose_signs(self.roads(**kw)), [], kw)
