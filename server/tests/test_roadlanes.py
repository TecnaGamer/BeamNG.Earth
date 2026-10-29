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
import rampoverrides
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
        box = dict(bx0=f.lonlat[0, 0], by0=f.lonlat[0, 1], bx1=f.lonlat[-1, 0], by1=f.lonlat[-1, 1])
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
        self.assertEqual((sp.width, sp.lanes), (14., 3))
        self.assertIsNone(roads.road_specs(highway(cls="residential", direction=0))[0].lanes)
        ring = highway()
        ring.attrs["osm_junction"] = np.array(["roundabout"])
        sp = roads.road_specs(ring)[0]
        self.assertIsNone(sp.lanes)
        self.assertEqual(sp.width, roads.ring_width(None))

    def test_exit76_correction_is_confined_to_the_source_way_and_bounds(self):
        f = highway(link=True)
        west, south, east, north = rampoverrides.KY_I65_EXIT76_BOUNDS
        f.lonlat[:] = [[west + .0001, south + .0001], [east - .0001, north - .0001]]
        wid = rampoverrides.KY_I65_EXIT76_WAY
        f.attrs["src_record"] = np.array([f"w{wid}@9"])
        osmtags.join(f, {wid: {"lanes": 3}})
        self.assertEqual(f.attrs["osm_lanes"][0], 3, "downloaded tag kept")
        self.assertEqual(roads.road_specs(f)[0].lanes, 1)
        f.lonlat[:, 0] += .01
        roadlanes.invalidate(f)
        self.assertEqual(roads.road_specs(f)[0].lanes, 3)


if __name__ == "__main__":
    unittest.main()
