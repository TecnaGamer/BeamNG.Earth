"""Highway lane defaults and the evidence that may override them (offline)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import navgraph
import markings
import osmtags
import railtags
import roadlanes
import roads
from test_roads import FRAME, features, flat, rules


def highway(lines=None, cls="motorway", direction=1, link=False, flags=()):
    lines = lines or [[(0, 0), (100, 0)]]
    f = features(lines, [cls] * len(lines), flags=flags)
    f.attrs["src_record"] = np.array([f"w{i + 1}@1" for i in range(len(lines))])
    if direction:
        f.rules["oneway"] = rules([(i, direction) for i in range(len(lines))], "float")
    if link:
        f.rules["subclass"] = rules([(i, "link") for i in range(len(lines))])
    return f


def chain(counts=(3, None, 3), reverse=False):
    lines = [[(0, 0), (100, 0)], [(100, 0), (200, 0)], [(200, 0), (300, 0)]]
    if reverse:
        lines[1].reverse()
    f = highway(lines, flags=[(1, "is_bridge")])
    f.rules["oneway"] = rules([(i, -1 if reverse and i == 1 else 1) for i in range(3)], "float")
    return osmtags.join(f, {i + 1: {"lanes": n} for i, n in enumerate(counts) if n is not None})


class HighwayLanesTest(unittest.TestCase):
    def test_unmapped_width_guess_does_not_add_an_unpainted_lane(self):
        f = features([[(0, 0), (100, 0)]], ["primary"], widths=[(0, 12.0)])
        sp = roads.road_specs(f)[0]
        self.assertIsNone(sp.lanes)
        self.assertEqual(roads.lane_layout(sp)[1], 3)
        g = navgraph.build(FRAME, [f], flat, (0., 0.))
        self.assertTrue(all((e[3], e[4]) == (1, 1) for e in g["edges"]))

    def test_directional_tags_and_odd_totals_agree_in_paint_and_traffic(self):
        for total, tags, expected in (
                (3, {}, (2, 1, 0)),
                (5, {"lanes:both_ways": "1"}, (2, 2, 1)),
                (5, {"lanes:forward": "3", "lanes:backward": "1", "lanes:both_ways": "1"}, (3, 1, 1)),
                (3, {"lanes:forward": "1"}, (1, 2, 0)),
                (3, {"lanes:forward": "4", "lanes:backward": "4"}, (2, 1, 0)),
                (3, {"lanes:forward": "2;3", "lanes:backward": "1.5"}, (2, 1, 0))):
            with self.subTest(total=total, tags=tags):
                f = highway(cls="trunk", direction=0)
                osmtags.join(f, {1: {"lanes": total}})
                for k, v in tags.items():
                    f.attrs[railtags.lane_attr(k)] = np.array([v])
                self.assertEqual(roadlanes.directions(total, tags), expected)
                g = navgraph.build(FRAME, [f], flat, (0., 0.))
                self.assertTrue(all((e[3], e[4]) == expected[:2] for e in g["edges"]))
                sp = roads.road_specs(f)[0]
                drive, _ = roads.lane_layout(sp)
                plan = markings.lane_plan(tags or {"lanes": total}, True, drive, total)
                self.assertEqual(len([r for _, r, _ in plan if r == "lane"]), sum(expected[:2]) - 2)
                if total == 3 and not tags:
                    paint = roads.extras(sp, np.array([[0., 0., 0.], [100., 0., 0.]]), -85., country="US")
                    self.assertEqual(len({round(float(p[0, 1]), 4) for lk, _, p in paint
                                          if lk.get("kind") == "lane"}), 1)

    def test_urban_and_rural_default_two_but_oneway_ramps_one(self):
        for cls in ("motorway", "trunk"):
            for urban in (False, True):
                for direction in (-1, 0, 1):
                    for link in (False, True):
                        with self.subTest(cls=cls, urban=urban, direction=direction, link=link):
                            f = highway(cls=cls, direction=direction, link=link)
                            sp = roads.road_specs(f, lambda la, lo: np.full(np.shape(la), urban))[0]
                            n = 1 if link and direction else 2
                            self.assertEqual(sp.lanes, n)
                            self.assertEqual(roads.lane_layout(sp)[1], n)
                            g = navgraph.build(FRAME, [f], flat, (0., 0.))
                            self.assertTrue(g["edges"])
                            self.assertTrue(all((e[3], e[4]) == ((n, 0) if direction else (1, 1))
                                                for e in g["edges"]))

    def test_mapped_counts_win_on_ground_bridges_tunnels_and_reversed_ramps(self):
        for kind in (None, "is_bridge", "is_tunnel"):
            for link in (False, True):
                for n in (1, 2, 3, 4, 5, 12):
                    with self.subTest(kind=kind, link=link, lanes=n):
                        f = highway(direction=-1, link=link, flags=[(0, kind)] if kind else [])
                        osmtags.join(f, {1: {"lanes": n}})
                        make = roads.bridge_specs if kind == "is_bridge" else (
                            roads.tunnel_specs if kind == "is_tunnel" else roads.road_specs)
                        sp = make(f)[0]
                        self.assertEqual((sp.lanes, sp.dir, sp.link), (n, -1, link))
                        self.assertEqual(roads.lane_layout(sp)[1], n)
                        self.assertEqual(navgraph._lanes(navgraph.driving_width(sp), True, sp.lanes), (n, 0))
                        self.assertAlmostEqual(roads._structure_width(f, 0), sp.width)

    def test_bad_counts_are_missing_not_truncated(self):
        for bad in (None, "", "2;3", "2.5", 2.5, "3 lanes", "3 @ (Mo-Fr)", 0, -1, 40, np.nan, np.inf):
            with self.subTest(value=bad):
                f = highway()
                osmtags.join(f, {1: {"lanes": bad}})
                self.assertTrue(np.isnan(f.attrs["osm_lanes"][0]))
                self.assertEqual(roads.road_specs(f)[0].lanes, 2)

    def test_bridge_gap_needs_agreement_at_both_ends(self):
        for reverse in (False, True):
            f = chain(reverse=reverse)
            self.assertTrue(np.isnan(f.attrs["osm_lanes"][1]))
            self.assertEqual(roads.bridge_specs(f)[1].lanes, 3)
        for counts in ((3, None, 4), (3, None, None)):
            self.assertEqual(roads.bridge_specs(chain(counts))[1].lanes, 2)
        self.assertEqual(roads.bridge_specs(chain((3, 2, 3)))[1].lanes, 2)

    def test_no_gap_inference_from_ramp_opposite_direction_fork_or_wrong_connector(self):
        for mode in ("ramp", "opposing", "crossing", "connector", "fork"):
            f = chain()
            if mode == "ramp":
                f.rules["subclass"] = rules([(0, "link")])
            elif mode == "opposing":
                f.rules["oneway"] = rules([(0, -1), (1, 1), (2, 1)], "float")
            elif mode == "crossing":
                f.lonlat[:2] = highway([[(100, -100), (100, 0)]]).lonlat
            elif mode == "connector":
                f.rules["connectors"] = rules([(0, "A", 1., 1.), (1, "B", 0., 0.)])
            else:
                f = highway([[(0, 0), (100, 0)], [(100, 0), (200, 0)],
                             [(200, 0), (300, 0)], [(200, 0), (300, 1)]], flags=[(1, "is_bridge")])
                osmtags.join(f, {1: {"lanes": 3}, 3: {"lanes": 3}, 4: {"lanes": 3}})
            self.assertEqual(roads.bridge_specs(f)[1].lanes, 2, mode)

    def test_highway_name_alone_does_not_supply_lanes(self):
        f = highway()
        f.attrs["name"] = np.array(["Interstate 65"])
        # (Python floats, as osm_tags makes them: osm_join refuses numpy scalars, whose errors follow np.errstate)
        box = dict(bx0=float(f.lonlat[0, 0]), by0=float(f.lonlat[0, 1]), bx1=float(f.lonlat[-1, 0]),
                   by1=float(f.lonlat[-1, 1]))
        osmtags.join(f, {99: dict(box, name="Interstate 65", lanes=4, osm_oneway="yes")})
        self.assertTrue(np.isnan(f.attrs["osm_lanes"][0]))
        self.assertEqual(roads.road_specs(f)[0].lanes, 2)

    def test_current_geometry_recovers_changed_source_ids_in_both_orders(self):
        for reverse in (False, True):
            f = highway([[(20, 0), (80, 0)]], direction=-1 if reverse else 1)
            if reverse:
                f.lonlat[:] = f.lonlat[::-1]
            osmtags.join(f, {99: {"lanes": 4, "_coords": highway().lonlat,
                                 "_highway": "motorway", "osm_oneway": "yes"}})
            self.assertEqual(roads.road_specs(f)[0].lanes, 4)
            self.assertEqual(f.attrs["lane_source"][0], "osm-geometry")
            self.assertEqual(f.attrs["lane_osm_way"][0], 99)

    def test_geometry_read_only_when_a_highways_own_way_is_gone(self):
        """osmtags.join's geometry callback (the highways' geometry column, ~33 MB of a cold LA visit) is asked only
        when a motorway's own way id is not among the tags; then it gives the count as the tags' _coords did."""
        asked = []

        def geometry(need):
            asked.append(need)
            return {99: highway().lonlat}
        f = highway([[(20, 0), (80, 0)]])
        osmtags.join(f, {99: {"lanes": 4, "lane_hw": "motorway", "osm_oneway": "yes"}}, geometry)
        self.assertEqual((len(asked), roads.road_specs(f)[0].lanes), (1, 4))
        g = highway([[(20, 0), (80, 0)]])
        own = osmtags.way_id(str(g.attrs["src_record"][0])) if "src_record" in g.attrs else None
        if own is not None:
            asked.clear()
            osmtags.join(g, {own: {"lanes": 3, "lane_hw": "motorway", "osm_oneway": "yes"}}, geometry)
            self.assertEqual((len(asked), roads.road_specs(g)[0].lanes), (0, 3), "its own way: not asked")

    def test_geometry_rejects_ambiguous_or_unrelated_ways(self):
        for mode in ("opposing", "two_way", "far", "ambiguous", "ramp", "class"):
            f = highway()
            geom = highway([[(0, 10), (100, 10)]]).lonlat if mode == "far" else f.lonlat.copy()
            tag = {"lanes": 4, "_coords": geom, "_highway": "motorway", "osm_oneway": "yes"}
            if mode in ("opposing", "two_way"):
                tag["osm_oneway"] = "-1" if mode == "opposing" else "no"
            if mode in ("ramp", "class"):
                tag["_highway"] = "motorway_link" if mode == "ramp" else "trunk"
            tags = {99: tag}
            if mode == "ambiguous":
                tags[98] = dict(tag, lanes=3)
            osmtags.join(f, tags)
            self.assertEqual(roads.road_specs(f)[0].lanes, 2, mode)
            self.assertEqual(f.attrs["lane_source"][0], "", mode)

    def test_enrichment_refresh_discards_ground_and_structure_specs(self):
        f = chain()
        self.assertEqual(roads.road_specs(f)[0].lanes, 3)
        self.assertEqual(roads.bridge_specs(f)[1].lanes, 3)
        osmtags.join(f, {1: {"lanes": 4}, 3: {"lanes": 4}})
        self.assertEqual(roads.road_specs(f)[0].lanes, 4)
        self.assertEqual(roads.bridge_specs(f)[1].lanes, 4)

    def test_lane_shard_total_and_directional_counts_reach_specs(self):
        f = highway(cls="trunk", direction=0)
        self.assertEqual(roads.road_specs(f)[0].lanes, 2)
        lt = railtags.LaneTags.__new__(railtags.LaneTags)
        with mock.patch.object(lt, "of_cell", return_value={"ways": {"1": {
                "lanes:forward": "2", "lanes:backward": "1", "lanes:both_ways": "1"}}}):
            lt.join(f, None)
        self.assertEqual(roads.road_specs(f)[0].lanes, 4)
        with mock.patch.object(lt, "of_cell", return_value={"ways": {"1": {"lanes": "5"}}}):
            lt.join(f, None)
        self.assertEqual(roads.road_specs(f)[0].lanes, 5)

    def test_mapped_width_and_nonhighway_defaults_stay_independent(self):
        f = highway()
        osmtags.join(f, {1: {"lanes": 3, "width": 14.0}})
        sp = roads.road_specs(f)[0]
        self.assertEqual((sp.width, sp.lanes), (18., 3))  # preserve three travel lanes and both highway shoulders
        self.assertIsNone(roads.road_specs(highway(cls="residential", direction=0))[0].lanes)
        ring = highway()
        ring.attrs["osm_junction"] = np.array(["roundabout"])
        sp = roads.road_specs(ring)[0]
        self.assertIsNone(sp.lanes)
        self.assertEqual(sp.width, roads.ring_width(None))


class RampLaneRuleTest(unittest.TestCase):
    """A motorway link mapped with more lanes than its diverge gives takes what it gives (roads._lane_overrides,
    tapers::ramp_lane_rule; in place of rampoverrides' I-65 Exit 76 correction: the same shape at Upton, a lanes=3
    exit from a 3-lane mainline going on with 3)."""

    @staticmethod
    def diverge(main_in=3, main_out=3, link=3, link_tags=None):
        f = highway([[(0, 0), (100, 0)], [(100, 0), (200, 0)], [(100, 0), (180, -30)]])
        f.rules["subclass"] = rules([(2, "link")])
        tags = {1: {"lanes": main_in}, 2: {"lanes": main_out}, 3: dict({"lanes": link}, **(link_tags or {}))}
        return osmtags.join(f, tags)

    def test_an_exit_mapped_wider_than_its_diverge(self):
        f = self.diverge()
        self.assertEqual(f.attrs["osm_lanes"][2], 3, "the downloaded tag kept")
        specs = roads.road_specs(f)
        self.assertEqual([specs[0].lanes, specs[1].lanes, specs[2].lanes], [3, 3, 1])

    def test_kept_where_the_map_says_more(self):
        self.assertEqual(roads.road_specs(self.diverge(link_tags={"width": 12.0}))[2].lanes, 3, "a mapped width")
        f = self.diverge()
        f.attrs["osm_turn_lanes"] = np.array([None, None, "left|through|right"], dtype=object)
        roadlanes.invalidate(f)
        self.assertEqual(roads.road_specs(f)[2].lanes, 3, "turn:lanes")
        self.assertEqual(roads.road_specs(self.diverge(main_in=5))[2].lanes, 3, "a diverge that gives two lanes")
        self.assertEqual(roads.road_specs(self.diverge(link=2))[2].lanes, 2, "under three lanes: not checked")

    def test_a_ramp_splitting_from_a_ramp_is_not_checked(self):
        # (a ramp fanning out to its terminal: 1 lane splits into a 1-lane branch and the 3-lane approach to a signal)
        f = highway([[(0, 0), (100, 0)], [(100, 0), (200, 0)], [(100, 0), (180, -30)]])
        f.rules["subclass"] = rules([(0, "link"), (1, "link"), (2, "link")])
        osmtags.join(f, {1: {"lanes": 1}, 2: {"lanes": 1}, 3: {"lanes": 3}})
        self.assertEqual(roads.road_specs(f)[2].lanes, 3)

    def test_switch(self):
        with mock.patch.object(roads, "RAMP_LANE_RULE", False):
            self.assertEqual(roads.road_specs(self.diverge())[2].lanes, 3)

    def test_switch_off_in_the_lane_flow_too(self):
        # (Codex review of the R1 branch: off, the lane flow solve still took the rule's count, so a road the link
        # feeds took 1 lane instead of its 3; off must be the map's counts everywhere)
        def fed():
            f = highway([[(0, 0), (100, 0)], [(100, 0), (200, 0)], [(100, 0), (180, -30)], [(180, -30), (300, -60)]])
            f.attrs["class"] = np.array(["motorway", "motorway", "motorway", "primary"])
            f.rules["subclass"] = rules([(2, "link")])
            return osmtags.join(f, {1: {"lanes": 3}, 2: {"lanes": 3}, 3: {"lanes": 3}})
        self.assertEqual(roads.lane_flow(fed())[3], 1, "on: the road it feeds takes the corrected count")
        with mock.patch.object(roads, "RAMP_LANE_RULE", False):
            f = fed()
            self.assertEqual(roads.lane_flow(f)[3], 3)
            self.assertEqual([sp.lanes for sp in roads.road_specs(f)], [3, 3, 3, 3])


if __name__ == "__main__":
    unittest.main()
