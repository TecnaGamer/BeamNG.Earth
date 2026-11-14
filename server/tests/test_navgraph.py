"""navgraph: the drivable roads as the stock map's graph: connectors shared at junctions, one-way direction,
lanes, speed limits, clipped to the window."""
import os
os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                                                       "regional"))     # (PR #505's data: the tests' slice)
import sys
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import borderfixtures  # noqa: E402
import geodesy  # noqa: E402
import navgraph  # noqa: E402
import roads  # noqa: E402
from test_roads import FRAME, features, flat, rules  # noqa: E402


def setUpModule():
    borderfixtures.require()        # (no border data: the German graph tests would pass on today's graph)


def graph(lines, classes, connectors=(), oneway=(), speeds=(), subclass=(), centre=(0.0, 0.0), window=1100.0):
    f = features(lines, classes)
    f.rules["connectors"] = rules(list(connectors))
    f.rules["oneway"] = rules(list(oneway), "float")
    f.rules["max_speed_kmh"] = rules(list(speeds), "float")
    f.rules["subclass"] = rules(list(subclass))
    return navgraph.build(FRAME, [f], flat, centre, window=window)


class ShortIdsTest(unittest.TestCase):
    """navgraph.short_ids: one short stable name per node in the nodes, edges and signals (the game decodes the file at
    every rebuild; the long names were most of it), the same name in every graph."""

    def test_names_short_stable_and_consistent(self):
        a, b = "s3d0fc61b-e9c6-487c-9938-418f4c654cb6_6831d", "s3d0fc61b-e9c6-487c-9938-418f4c654cb6_7555d"
        g = {"nodes": {a: [0, 0, 0, 3], b: [10, 0, 0, 3]}, "edges": [[a, b, 1, 1, 0, 13.4, 1.0, 3.0]],
             "signals": [{"j": b, "kind": "stop", "app": [[a, 1.0, 2.0, 0.0, 1.0, 0.0, 0]]}], "lines": [[0, 0, 1, 1, 1]],
             "rules": {"rightHandDrive": True}}
        s = navgraph.short_ids(g)
        na, nb = navgraph.short_ids({"nodes": {a: [0, 0, 0, 1]}})["nodes"].popitem()[0], None
        self.assertEqual(set(s["nodes"]), {s["edges"][0][0], s["edges"][0][1]})
        self.assertEqual(s["edges"][0][0], na, "the same node: the same short name in another graph")
        self.assertEqual(s["signals"][0]["j"], s["edges"][0][1])
        self.assertEqual(s["signals"][0]["app"][0][0], s["edges"][0][0])
        self.assertEqual(s["edges"][0][2:], g["edges"][0][2:])
        self.assertEqual((s["lines"], s["rules"]), (g["lines"], g["rules"]))
        self.assertTrue(all(len(k) == 13 for k in s["nodes"]))
        self.assertIn(a, g["nodes"], "the input is not changed")


class GraphTest(unittest.TestCase):
    def test_rumble_heuristic_is_country_and_road_type_scoped(self):
        motorway = SimpleNamespace(kind="paved", cls="motorway", urban=True, link=False)
        rural_primary = SimpleNamespace(kind="paved", cls="primary", urban=False, link=False)
        urban_primary = SimpleNamespace(kind="paved", cls="primary", urban=True, link=False)
        link = SimpleNamespace(kind="paved", cls="motorway", urban=False, link=True)
        gravel = SimpleNamespace(kind="unpaved", cls="primary", urban=False, link=False)
        self.assertTrue(navgraph.rumble_eligible("AU", motorway))
        self.assertTrue(navgraph.rumble_eligible("NZ", rural_primary))
        self.assertTrue(navgraph.rumble_eligible("US", rural_primary))
        self.assertFalse(navgraph.rumble_eligible("AU", urban_primary))
        self.assertFalse(navgraph.rumble_eligible("GB", rural_primary))
        self.assertFalse(navgraph.rumble_eligible("AU", link))
        self.assertFalse(navgraph.rumble_eligible("AU", gravel))

    def test_rumble_flag_is_serialized_for_rural_australian_highway(self):
        f = features([[(0, 0), (500, 0)]], ["primary"])
        f.rules["connectors"] = rules([(0, "A", 0.0, 0.0), (0, "B", 1.0, 1.0)])
        rural = navgraph.build(FRAME, [f], flat, (0.0, 0.0), mph=False, country="AU",
                               urban_fn=lambda lat, lon: np.zeros_like(lat, dtype=bool))
        self.assertTrue(rural["edges"])
        self.assertTrue(all(len(e) > 11 and e[11] is True and e[10] is False for e in rural["edges"]),
                        "rural eligible edges carry the optional rumble flag (after the rough-road flag)")
        urban = navgraph.build(FRAME, [f], flat, (0.0, 0.0), mph=False, country="AU",
                               urban_fn=lambda lat, lon: np.ones_like(lat, dtype=bool))
        self.assertTrue(all(len(e) <= 11 or e[11] is not True for e in urban["edges"]),
                        "urban non-motorway edges do not get rumble metadata")

    def test_graph_has_a_traffic_profile(self):
        g = graph([[(0, 0), (300, 0)]], ["primary"], connectors=[(0, "A", 0.0, 0.0), (0, "B", 1.0, 1.0)])
        p = g["trafficProfile"]
        self.assertEqual(p["sampleRadiusM"], 750)
        self.assertAlmostEqual(p["roadDensityKmKm2"], 0.3 / (np.pi * 0.75 ** 2), places=2)
        self.assertEqual(p["roadClass"], "primary")
        self.assertIn("target", p)
        self.assertTrue(all(len(e) == 8 for e in g["edges"]), "the edges as before")

    def test_rough_osm_smoothness_marks_road_without_changing_speed_targets(self):
        """Roughness is metadata for the traffic driver profile, not a fabricated speed limit."""
        f = features([[(0, 0), (500, 0)]], ["primary"])
        f.rules["connectors"] = rules([(0, "A", 0.0, 0.0), (0, "B", 1.0, 1.0)])
        f.rules["max_speed_kmh"] = rules([(0, 110.0)], "float")
        f.attrs["osm_smoothness"] = np.array(["bad"], dtype=object)
        g = navgraph.build(FRAME, [f], flat, (0.0, 0.0), mph=False)
        self.assertTrue(g["edges"])
        self.assertTrue(all(abs(e[5] - 110.0 / 3.6) < 0.1 for e in g["edges"]), g["edges"])
        self.assertTrue(all(len(e) > 10 and e[10] is True for e in g["edges"]), "rough flag follows optional edge metadata")
        f.attrs["osm_smoothness"] = np.array(["good"], dtype=object)
        smooth = navgraph.build(FRAME, [f], flat, (0.0, 0.0), mph=False)
        self.assertTrue(all(len(e) <= 10 or e[10] is not True for e in smooth["edges"]),
                        "good/unknown smoothness does not mark the road rough")

    def test_priority_stops_in_the_graph(self):
        """furniture.PRIORITY_STOPS: a residential crossing a primary stops (a stop junction for the AI), its approaches
        below the primary's in drivability, the primary's a little slower than with it off."""
        lines = [[(-100, 0), (0, 0)], [(0, 0), (100, 0)], [(0, -100), (0, 0)], [(0, 0), (0, 100)]]
        con = [(0, "W", 0.0, 0.0), (0, "J", 1.0, 1.0), (1, "J", 0.0, 0.0), (1, "E", 1.0, 1.0),
               (2, "S", 0.0, 0.0), (2, "J", 1.0, 1.0), (3, "J", 0.0, 0.0), (3, "N", 1.0, 1.0)]
        cls = ["primary", "primary", "residential", "residential"]
        import furniture
        off = graph(lines, cls, connectors=con)
        with mock.patch.object(furniture, "PRIORITY_STOPS", True):
            on = graph(lines, cls, connectors=con)
        stops = [s for s in on["signals"] if s["j"] == "cJ"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["kind"], "stop")
        self.assertEqual(len(stops[0]["app"]), 2, "the two residential approaches")

        res_on = [e for e in on["edges"] if "cJ" in e[:2] and e[6] < 0.8]
        self.assertTrue(res_on and all(e[6] == furniture.PRIORITY_DRIVABILITY[4] for e in res_on))
        prim_on = sorted(e[5] for e in on["edges"] if "cJ" in e[:2] and e[6] > 0.9)
        prim_off = sorted(e[5] for e in off["edges"] if "cJ" in e[:2] and e[6] > 0.9)
        self.assertEqual(len(prim_on), len(prim_off))
        self.assertTrue(all(a < b for a, b in zip(prim_on, prim_off)), (prim_on, prim_off))

    def test_junction_shares_the_connector(self):
        g = graph([[(0, 0), (100, 0)], [(100, 0), (100, 100)]], ["residential", "residential"],
                  connectors=[(0, "A", 0.0, 0.0), (0, "J", 1.0, 1.0), (1, "J", 0.0, 0.0), (1, "B", 1.0, 1.0)])
        self.assertIn("cJ", g["nodes"])
        links = [e for e in g["edges"] if "cJ" in e[:2]]
        self.assertEqual(len(links), 2, "both roads end at the junction node")
        x, y, z, r = g["nodes"]["cJ"]
        self.assertAlmostEqual(x, 100.0, delta=0.5)
        self.assertAlmostEqual(y, 0.0, delta=0.5)
        self.assertEqual(z, 10.0)
        for e in g["edges"]:
            self.assertEqual((e[3], e[4]), (1, 1), "a two-way residential street: one lane each way")
            self.assertAlmostEqual(e[5], 25 * navgraph.MPH, places=2)

    def test_offset_connector_at_the_widest_road(self):
        """A ramp's line ends 3.3 m off the motorway's joint at their shared connector (I-83 in Baltimore): the node
        stands on the motorway's line, whichever road comes first."""
        for order in (0, 1):
            lines = [[(0, -200), (0, 0)], [(0, 0), (0, 200)], [(60, -150), (3.3, 0)]]
            classes = ["primary", "primary", "residential"]
            con = [(0, "J", 1.0, 1.0), (1, "J", 0.0, 0.0), (2, "J", 1.0, 1.0)]
            if order:
                lines, classes = lines[::-1], classes[::-1]
                con = [(2 - f, c, a, b) for f, c, a, b in con]
            x, y, _z, _r = graph(lines, classes, connectors=con)["nodes"]["cJ"]
            self.assertAlmostEqual(x, 0.0, delta=0.05, msg=order)
            self.assertAlmostEqual(y, 0.0, delta=0.05)

    def test_a_median_crossover_is_kept_from_traffic(self):
        """I-94 near Huntley MT (2026-11-05): a service road across the median between the two carriageways' connectors
        took traffic across into the ditch: it has a track's drivability (below the stock traffic's minimum). A rest
        area's service road between two ramps, and one from the motorway to a frontage road, stay service roads."""
        lines = [[(0, 0), (500, 0)], [(500, 30), (0, 30)],                  # the two carriageways
                 [(250, 0), (250, 30)],                                     # the crossover
                 [(400, -10), (400, -60)],                                  # a ramp off the eastbound
                 [(400, -60), (300, -60)],                                  # the rest area's road, ramp to ramp
                 [(300, -60), (300, -10)],                                  # the ramp back
                 [(100, 0), (100, -80)]]                                    # a service road to a frontage road
        classes = ["motorway", "motorway", "service", "motorway", "service", "motorway", "service"]
        con = [(0, "W", 0.0, 0.0), (0, "X1", 0.5, 0.5), (0, "E", 1.0, 1.0),
               (1, "E2", 0.0, 0.0), (1, "X2", 0.5, 0.5), (1, "W2", 1.0, 1.0),
               (2, "X1", 0.0, 0.0), (2, "X2", 1.0, 1.0),
               (3, "R1", 0.0, 0.0), (3, "R2", 1.0, 1.0), (4, "R2", 0.0, 0.0), (4, "R3", 1.0, 1.0),
               (5, "R3", 0.0, 0.0), (5, "R4", 1.0, 1.0),
               (6, "F0", 0.0, 0.0), (6, "F1", 1.0, 1.0)]
        g = graph(lines, classes, connectors=con, subclass=[(3, "link"), (5, "link")], oneway=[(0, 1.0), (1, 1.0)])
        drive = {frozenset(e[:2]): e[6] for e in g["edges"]}
        self.assertEqual(drive[frozenset(("cX1", "cX2"))], navgraph.DRIVE["track"][0])
        rest = [e[6] for e in g["edges"] if any(n.startswith("s") for n in e[:2]) and {"cR2", "cR3"} & set(e[:2])]
        self.assertTrue(rest and all(d == navgraph.DRIVE["service"][0] for d in rest), "a rest area's road")
        self.assertIn(navgraph.DRIVE["service"][0], [d for k, d in drive.items() if "cF1" in k], "to a frontage road")

    def test_long_edges_split_and_oneway_against_the_geometry(self):
        g = graph([[(0, 0), (300, 0)]], ["secondary"], connectors=[(0, "A", 0.0, 0.0), (0, "B", 1.0, 1.0)],
                  oneway=[(0, -1.0)], speeds=[(0, 72.0)])
        self.assertGreaterEqual(len(g["edges"]), 4, "300 m in edges of at most MAX_EDGE_M")
        for e in g["edges"]:
            self.assertEqual(e[2], 1)
            self.assertLess(g["nodes"][e[1]][0], g["nodes"][e[0]][0], "one-way -1: drives against the geometry")
            self.assertEqual((e[3], e[4]), (e[3], 0))
            self.assertAlmostEqual(e[5], 20.0, places=2)
        self.assertTrue(any(e[1] == "cA" for e in g["edges"]))

    def test_not_drivable_and_window(self):
        g = graph([[(0, 0), (50, 0)], [(0, 10), (50, 10)], [(0, 20), (50, 20)], [(5000, 0), (5100, 0)]],
                  ["footway", "service", "residential", "residential"], subclass=[(1, "parking_aisle")])
        ys = {round(n[1]) for n in g["nodes"].values()}
        self.assertEqual(ys, {20}, "only the residential street in the window")

    def test_even_lanes_and_no_parking_lanes(self):
        import roads
        for cls in ("residential", "tertiary", "secondary", "primary"):
            sp = roads.Spec(roads.carriageway(cls, "paved", True), "paved", True, cls, False)
            w = navgraph.driving_width(sp)
            self.assertLessEqual(w, sp.width)
            lo, li = navgraph._lanes(w, False)
            self.assertEqual(lo, li, cls)

    def test_mountain_road_inside_its_edges(self):
        """A switchback pass road (roads._mountain_roads): the AI's half width MOUNTAIN_EDGE_M inside its edge lines
        (a wall and a parapet right at the edge), wider round its widened hairpins; an ordinary rural secondary at its
        lanes' edge."""
        from test_roads import _zigzag
        g = graph([_zigzag(4)], ["secondary"])
        radii = {round(e[7], 2) for e in g["edges"]}
        sp = roads.road_specs(__import__("test_roads").features([_zigzag(4)], ["secondary"]))[0]
        self.assertTrue(sp.mountain)
        leg = round(navgraph.driving_width(sp) / 2 - navgraph.MOUNTAIN_EDGE_M, 2)
        self.assertEqual(min(radii), leg, "the legs: inside the edge lines")
        self.assertGreater(max(radii), leg + 1.0, "round the hairpins: wider (roads.HAIRPIN_WIDEN_M)")
        self.assertLessEqual(max(radii), leg + roads.HAIRPIN_WIDEN_M / 2 + 0.01)
        flat_ = graph([[(0, 0), (300, 0)]], ["secondary"])
        sp2 = roads.road_specs(__import__("test_roads").features([[(0, 0), (300, 0)]], ["secondary"]))[0]
        self.assertEqual({round(e[7], 2) for e in flat_["edges"]}, {round(navgraph.driving_width(sp2) / 2, 2)})

    def test_motorway_defaults_two_lanes(self):
        import roads
        for urban, n in ((False, 2), (True, 2)):
            sp = roads.Spec(roads.carriageway("motorway", "paved", urban, None, True), "paved", urban, "motorway", True)
            self.assertEqual(navgraph._lanes(navgraph.driving_width(sp), True), (n, 0), urban)
        ramp = roads.Spec(roads.LINK_M[1], "paved", False, "motorway", True, link=True, lanes=1)
        self.assertAlmostEqual(navgraph.driving_width(ramp), 4.8)
        self.assertEqual(navgraph._lanes(navgraph.driving_width(ramp), True, ramp.lanes), (1, 0), "a ramp: one lane (GitHub #107)")

    def test_nodes_on_the_curve_of_the_earth(self):
        """10 km from the anchor the ground drawn lies ~7.8 m under the flat plane (the anchor frame's up): the nodes
        too, not at the raw height (the AI's nodes and traffic lights stood over the ground, PR #112's finding)."""
        g = graph([[(0, 10000), (200, 10000)]], ["primary"], centre=(100.0, 10000.0))
        zs = np.array([n[2] for n in g["nodes"].values()])
        self.assertTrue(len(zs))
        drop = 10000.0 ** 2 / (2 * 6371000.0)
        np.testing.assert_allclose(zs, 10.0 - drop, atol=0.15)

    def test_tracks_for_the_follow_ai_not_for_traffic(self):
        """A track is in the graph (GitHub #756: an AI following the player down one drove into the trees) below the
        stock traffic's spawn floor (0.5) and its cars' minimum (0.25), at a slow speed."""
        f = features([[(0, 0), (300, 0)]], ["track"])
        f.rules["connectors"] = rules([(0, "A", 0.0, 0.0), (0, "B", 1.0, 1.0)])
        g = navgraph.build(FRAME, [f], flat, (0.0, 0.0))
        self.assertTrue(g["edges"])
        self.assertTrue(all(e[6] < 0.25 and e[5] <= 15 * navgraph.MPH + 0.01 for e in g["edges"]), g["edges"][:2])

    def test_tunnel_stretch_left_out(self):
        f = features([[(0, 0), (300, 0)]], ["primary"], levels=[(0, -1.0, 0.4, 0.7)])
        f.rules["connectors"] = rules([(0, "A", 0.0, 0.0), (0, "B", 1.0, 1.0)])
        f.rules["max_speed_kmh"] = rules([(0, 70.0)], "float")
        g = navgraph.build(FRAME, [f], flat, (0.0, 0.0))
        for e in g["edges"]:
            mid = (g["nodes"][e[0]][0] + g["nodes"][e[1]][0]) / 2
            self.assertFalse(130 < mid < 200, "an edge over the tunnel")
        self.assertTrue(g["edges"])
        # ... but drawn on the minimap (its "lines"): the stretch from 120 to 210 m covered, end to end
        xs = sorted(x for ln in g["lines"] for x in (ln[0], ln[2]))
        self.assertTrue(g["lines"] and xs[0] <= 130 and xs[-1] >= 200, g["lines"])
        self.assertTrue(all(len(ln) == 6 and abs(ln[1]) < 1 and abs(ln[3]) < 1 for ln in g["lines"]))
        self.assertTrue(all(abs(ln[5] - 70.0 / 3.6) < 0.01 for ln in g["lines"]),
                        "the separate tunnel minimap lines retain the mapped legal limit in m/s")

    def test_a_built_tunnel_is_the_ais_at_its_floor(self):
        """A tunnel the road tiles build (roads.TunnelProfiles' parts: tunnels=) is driven through, its nodes at the
        tunnel's floor; one not built (none given) stays the minimap's line only."""
        f = features([[(0, 0), (300, 0)]], ["primary"], levels=[(0, -1.0, 0.4, 0.7)])
        f.rules["connectors"] = rules([(0, "A", 0.0, 0.0), (0, "B", 1.0, 1.0)])
        fid = str(f.ids[0])
        floor = (np.array([[110.0, 0.0], [220.0, 0.0]]), np.array([3.0, 3.0]))
        g = navgraph.build(FRAME, [f], flat, (0.0, 0.0), tunnels={fid: [floor]})
        if not navgraph.AI_TUNNELS:     # (off until the portals fit their roads: the minimap's line only)
            self.assertTrue(g["lines"])
            with mock.patch.object(navgraph, "AI_TUNNELS", True):
                g = navgraph.build(FRAME, [f], flat, (0.0, 0.0), tunnels={fid: [floor]})
        inside = [n for n in g["nodes"].values() if 130 < n[0] < 200]
        self.assertTrue(inside, "nodes in the tunnel")
        self.assertTrue(all(abs(n[2] - 3.0) < 0.05 for n in inside), [n[2] for n in inside])
        mids = [(g["nodes"][e[0]][0] + g["nodes"][e[1]][0]) / 2 for e in g["edges"]]
        self.assertTrue(any(130 < m < 200 for m in mids), "an edge through it")
        self.assertFalse(g["lines"], "nothing left for the minimap alone")

    def test_open_underpass_uses_the_rendered_road_with_tube_ai_off(self):
        # (an open section: with tunnels under flat ground buried by default, PLANS_FORMAT 57, this flat fixture is
        # only open without the burial)
        self.enterContext(mock.patch.object(roads, "BURY_ON", False))
        f = features([[(-150, 0), (150, 0)]], ["motorway"], flags=[(0, "is_tunnel")])
        f.rules["oneway"] = rules([(0, 1.0)], "float")
        profiles = roads.TunnelProfiles(FRAME, lambda key: f, flat)
        box = (-256, -128, 256, 128)
        chunks = roads._tunnel_chunks(profiles, [f], box)
        self.assertTrue(chunks and not any(c.get("tunnel") for c in chunks), "the renderer builds an open road")
        with mock.patch.object(navgraph, "AI_TUNNELS", False):
            closed, opened = navgraph.tunnel_floors(profiles, [f], box)
            g = navgraph.build(FRAME, [f], flat, (0, 0), tunnels=closed, open_tunnels=opened)
        self.assertTrue(opened)
        self.assertFalse(closed)
        self.assertFalse(g["lines"], "the rendered ground road is connected in the AI graph")
        self.assertTrue(g["edges"])
        self.assertTrue(all(e[2] == 1 and g["nodes"][e[0]][0] < g["nodes"][e[1]][0] for e in g["edges"]),
                        "one-way direction survives")
        plain = features([[(-150, 0), (150, 0)]], ["motorway"])
        plain.rules["oneway"] = f.rules["oneway"]
        expected = navgraph.build(FRAME, [plain], flat, (0, 0))
        self.assertEqual([e[2:] for e in g["edges"]], [e[2:] for e in expected["edges"]],
                         "open roads keep ordinary lanes, speeds and width, without portal narrowing")

    def test_missing_open_floor_elevation_stays_out_of_the_ai_graph(self):
        f = features([[(-150, 0), (150, 0)]], ["motorway"], flags=[(0, "is_tunnel")])

        def missing(lat, lon):
            return np.full(np.broadcast(lat, lon).shape, np.nan)

        profiles = roads.TunnelProfiles(FRAME, lambda key: f, missing)
        box = (-256, -128, 256, 128)
        self.assertFalse(roads._tunnel_chunks(profiles, [f], box), "the renderer skips the invalid floor")
        with mock.patch.object(navgraph, "AI_TUNNELS", False):
            closed, opened = navgraph.tunnel_floors(profiles, [f], box)
            g = navgraph.build(FRAME, [f], flat, (0, 0), tunnels=closed, open_tunnels=opened)
        self.assertFalse(opened, "an unbuilt open floor cannot supply AI heights")
        self.assertFalse(g["edges"], "no AI road crosses the unbuilt section")
        self.assertTrue(all(np.isfinite(n).all() for n in g["nodes"].values()))

    def test_tunnel_floors_skip_invalid_sections_without_losing_valid_ones(self):
        xy = np.array([[0., 0.], [100., 0.]])
        heights = ([3., 4.], [3., np.nan], [np.inf, 4.], [3., -np.inf])
        parts = [(f"road.0#{i}", None, xy, z, 0.) for i, z in enumerate(heights)]
        profiles = SimpleNamespace(parts=lambda *_: parts, open_parts=lambda *_: parts)
        with mock.patch.object(navgraph, "AI_TUNNELS", True):
            closed, opened = navgraph.tunnel_floors(profiles, [], (0, 0, 100, 100))
        for floors in (closed, opened):
            self.assertEqual(set(floors), {"road"})
            self.assertEqual(len(floors["road"]), 1, "skip invalid parts, keeping their valid sibling")
            np.testing.assert_array_equal(floors["road"][0][1], [3., 4.])

    def test_open_profiles_do_not_enable_closed_or_missing_tunnels(self):
        f = features([[(0, 0), (300, 0)]], ["primary"], levels=[(0, -1.0, 0.4, 0.7)])
        fid = str(f.ids[0])
        floor = (np.array([[110.0, 0.0], [220.0, 0.0]]), np.array([3.0, 3.0]))
        with mock.patch.object(navgraph, "AI_TUNNELS", False):
            g = navgraph.build(FRAME, [f], flat, (0, 0), tunnels={fid: [floor]}, open_tunnels={})
        self.assertTrue(g["lines"], "built closed tubes still require their separate opt-in")
        self.assertFalse(any(130 < n[0] < 200 for n in g["nodes"].values()))

    def test_open_floor_coverage_cannot_join_separate_open_sections(self):
        f = features([[(0, 0), (300, 0)]], ["primary"], levels=[(0, -1.0, 0.2, 0.8)])
        fid = str(f.ids[0])
        opened = {fid: [(np.array([[60., 0.], [140., 0.]]), np.array([3., 3.])),
                        (np.array([[150., 0.], [240., 0.]]), np.array([3., 3.]))]}
        with mock.patch.object(navgraph, "AI_TUNNELS", False):
            g = navgraph.build(FRAME, [f], flat, (0, 0), open_tunnels=opened)
        self.assertTrue(any(min(ln[0], ln[2]) < 140 and max(ln[0], ln[2]) > 150 for ln in g["lines"]),
                        "two open sections cannot bridge an unbuilt closed section between them")
        self.assertFalse(navgraph._open_floor(opened[fid], []))

    def test_open_floor_covers_only_the_tunnel_part_of_a_boundary_edge(self):
        f = features([[(0, 0), (300, 0)]], ["primary"], levels=[(0, -1.0, 0.4, 0.7)])
        fid = str(f.ids[0])
        opened = {fid: [(np.array([[120., 0.], [210., 0.]]), np.array([3., 3.]))]}
        with mock.patch.object(navgraph, "AI_TUNNELS", False):
            g = navgraph.build(FRAME, [f], flat, (0, 0), open_tunnels=opened)
        self.assertFalse(g["lines"], "a partly ordinary-road edge needs coverage only over its mapped tunnel range")
        inside = [n for n in g["nodes"].values() if 130 < n[0] < 200]
        self.assertTrue(inside)
        self.assertTrue(all(abs(n[2] - 3.) < .05 for n in inside), "the graph uses the rendered open floor")

    def test_the_approach_narrows_to_the_tube_at_the_portal(self):
        """A road wider than its tunnel's tube narrows to the tube's AI half width at the portal over PORTAL_LEAD_M
        (Fort Pitt: the AI spread its lanes over the wide approach into the pier): the portal node's radius is the
        tube's, the edges near it carry both ends' radii (edge[7] at edge[0], edge[8] at edge[1])."""
        L = navgraph.PORTAL_LEAD_M
        xs = [-200.0, -100.0, -L, -L / 2, 0.0, 50.0, 100.0]       # the portal at 0, the tube from 0 to 100
        nodes = {f"n{k}": [x, 0.0, 0.0, 6.0] for k, x in enumerate(xs)}
        edges = [[f"n{k}", f"n{k + 1}", 0, 2, 2, 25.0, 1.0, 6.0] for k in range(4)]
        tube = [[f"n{k}", f"n{k + 1}", 0, 2, 2, 25.0, 1.0, 4.0] for k in (4, 5)]
        side = ["n9", "n0", 0, 1, 1, 10.0, 1.0, 3.0]         # a narrower road: never widened
        nodes["n9"] = [-200.0, 50.0, 0.0, 3.0]
        navgraph.portal_taper(nodes, edges + tube + [side], {id(e) for e in tube}, {"n5"})

        def r(e):
            return e[7], (e[8] if len(e) > 8 else e[7])
        self.assertEqual(r(edges[0]), (6.0, 6.0), "farther than PORTAL_LEAD_M: as it was")
        self.assertEqual(r(edges[1]), (6.0, 6.0))
        self.assertEqual(r(edges[2]), (6.0, 5.0), "a straight taper")
        self.assertEqual(r(edges[3]), (5.0, 4.0), "the tube's at the portal")
        self.assertEqual(nodes["n4"][3], 4.0, "the portal node's own radius")
        self.assertEqual(nodes["n2"][3], 6.0)
        self.assertEqual([r(e) for e in tube], [(4.0, 4.0)] * 2, "the tube's own edges as they were")
        self.assertEqual(r(side), (3.0, 3.0))
        # a road narrower than the tube: nothing changes
        n2 = {k: list(v) for k, v in nodes.items()}
        e2 = [[f"n{k}", f"n{k + 1}", 0, 1, 1, 25.0, 1.0, 3.0] for k in range(4)]
        navgraph.portal_taper(n2, e2 + tube, {id(e) for e in tube}, {"n5"})
        self.assertTrue(all(len(e) == 8 for e in e2))

    def test_nodes_on_a_bridge_take_its_deck(self):
        f = features([[(0, 0), (200, 0)]], ["primary"])
        f.rules["connectors"] = rules([(0, "A", 0.0, 0.0), (0, "B", 1.0, 1.0)])
        decks = {"id000": [(np.array([(50.0, 0.0), (150.0, 0.0)]), np.array([10.0, 14.0]))]}
        g = navgraph.build(FRAME, [f], flat, (0.0, 0.0), decks=decks)
        on = [n for n in g["nodes"].values() if 55 < n[0] < 145]
        off = [n for n in g["nodes"].values() if n[0] < 40 or n[0] > 160]
        self.assertTrue(on and off)
        for n in on:
            self.assertAlmostEqual(n[2], 10.0 + 4.0 * (n[0] - 50.0) / 100.0, delta=0.05)
        self.assertTrue(all(n[2] == 10.0 for n in off), "the ground (flat 10 m) off the bridge")



class SignalsTest(unittest.TestCase):
    def test_no_stop_on_a_link_inside_one_intersection(self):
        import roads
        sp = roads.Spec(8.0, "paved", True, "residential", False)
        nodes = {"cJ": [0.0, 0.0, 0.0, 4], "cK": [0.0, 8.0, 0.0, 4], "s1": [0.0, -40.0, 0.0, 2],
                 "cW": [-40.0, 0.0, 0.0, 2], "cE": [40.0, 0.0, 0.0, 2]}
        arms = {"cJ": [("n", sp, np.array([0.0, 1.0]), 0.0, 1), ("s", sp, np.array([0.0, -1.0]), 0.0, 0),
                       ("w", sp, np.array([-1.0, 0.0]), 0.0, 1), ("e", sp, np.array([1.0, 0.0]), 0.0, 0)]}
        approaches = {("cJ", "n", 1): ("cK", True), ("cJ", "s", 0): ("s1", True), ("cJ", "w", 1): ("cW", True),
                      ("cJ", "e", 0): ("cE", True)}
        import furniture
        orig = furniture.junction_controls
        furniture.junction_controls = lambda a, k, link_ok=None, ring=None, signals=None, country=None, priority=None: [(x, furniture.G_STOP) for x in a]     # an all-way stop
        try:
            out = navgraph._signals(arms, approaches, nodes)
        finally:
            furniture.junction_controls = orig
        got = {ap[0] for ap in out[0]["app"]}
        self.assertEqual(got, {"s1", "cW", "cE"}, "the 8 m link from the next connector gets no stop")


    def test_left_hand_signal_heads_stand_as_the_props(self):
        """The switchable heads stand where furniture.junction_props stands the static booms, turned the same way: in
        left-hand traffic too (GitHub #595: they faced away from the drivers), the stop points on the left half."""
        import furniture
        import roads
        sp = roads.Spec(10.0, "paved", True, "tertiary", False)
        nodes = {"cJ": [0.0, 0.0, 0.0, 4], "cN": [0.0, 40.0, 0.0, 2], "cS": [0.0, -40.0, 0.0, 2],
                 "cW": [-40.0, 0.0, 0.0, 2], "cE": [40.0, 0.0, 0.0, 2]}
        arms = {"cJ": [("n", sp, np.array([0.0, 1.0]), 0.0, 1), ("s", sp, np.array([0.0, -1.0]), 0.0, 0),
                       ("w", sp, np.array([-1.0, 0.0]), 0.0, 1), ("e", sp, np.array([1.0, 0.0]), 0.0, 0)]}
        approaches = {("cJ", "n", 1): ("cN", True), ("cJ", "s", 0): ("cS", True), ("cJ", "w", 1): ("cW", True),
                      ("cJ", "e", 0): ("cE", True)}
        orig = furniture.junction_controls
        furniture.junction_controls = lambda a, k, link_ok=None, ring=None, signals=None, **_kw: [(x, furniture.G_SIGNAL)
                                                                                          for x in a]
        try:
            rh = {ap[0]: ap for ap in navgraph._signals(arms, approaches, nodes)[0]["app"]}
            lh = {ap[0]: ap for ap in navgraph._signals(arms, approaches, nodes, left_hand=True)[0]["app"]}
        finally:
            furniture.junction_controls = orig
        self.assertEqual(set(rh), {"cN", "cS", "cW", "cE"})
        for nb in rh:
            self.assertEqual(len(rh[nb]), 11, "right-hand layout unchanged")
            self.assertEqual(lh[nb][11], 1, "left-hand shape marker")
            self.assertEqual(lh[nb][10], rh[nb][10], f"{nb}: both heads face incoming drivers")
        self.assertLess(lh["cS"][7], 0.0, "northbound head on the left kerb")
        self.assertGreater(rh["cS"][7], 0.0, "right-hand head on the right kerb")
        self.assertLess(lh["cS"][1], 0.0, "northbound stop point on the west (left) half")
        self.assertGreater(rh["cS"][1], 0.0, "and on the east half for right-hand traffic")


DE_FRAME = geodesy.EnuFrame(48.561, 11.262, 0.0)        # Schrobenhausen (test_designs' JunctionGlueTest.FRAME)
NO_SIGNALS = np.zeros((0, 2))       # the map's traffic signals: none mapped (None: guessed where 3 major roads meet)
ARMS = {"w": [(-100, 0), (0, 0)], "e": [(0, 0), (100, 0)], "n": [(0, 0), (0, 100)], "s": [(0, 0), (0, -100)]}


def town(la, lo):
    """An urban_fn: in town everywhere."""
    return np.ones(np.shape(la), bool)


def in_germany(f):
    """test_roads' features (round Baltimore) moved to the same east / north metres round Schrobenhausen."""
    from test_roads import LAT, LON, M_LAT, M_LON
    lat, lon, _ = DE_FRAME.enu_to_geodetic((f.lonlat[:, 0] - LON) * M_LON, (f.lonlat[:, 1] - LAT) * M_LAT, 0.0)
    f.lonlat = np.column_stack([np.asarray(lon, np.float64), np.asarray(lat, np.float64)])
    return f


def junction(classes):
    """(lines, classes, connectors) of a junction J at (0, 0): classes {arm: class}, the arms w, e, n, s (ARMS: 100 m
    each, to the connectors W, E, N, S), ids id000 ... in the dict's order."""
    lines, con = [], []
    for i, (d, _c) in enumerate(classes.items()):
        lines.append(ARMS[d])
        at = 1.0 if d == "w" else 0.0
        con += [(i, "J", at, at), (i, d.upper(), 1.0 - at, 1.0 - at)]
    return lines, list(classes.values()), con


def de_graph(lines, classes, connectors, oneway=(), signs=(), **kw):
    """The road graph of these roads round Schrobenhausen (graph()'s, in Germany): signs, the lane shards' sign nodes
    in the cell [(x, y, traffic_sign)] (railtags.sign_nodes'); kw: navgraph.build's (de_on, priority, urban_fn ...),
    no traffic signal mapped unless given."""
    import railtags
    f = in_germany(features(lines, classes))
    f.rules["connectors"] = rules(list(connectors))
    f.rules["oneway"] = rules(list(oneway), "float")
    f.rules["max_speed_kmh"] = rules([], "float")
    f.rules["subclass"] = rules([])
    nodes = []
    for x, y, code in signs:
        lat, lon, _ = DE_FRAME.enu_to_geodetic(np.array(float(x)), np.array(float(y)), 0.0)
        nodes.append((float(lon), float(lat), {"traffic_sign": code}))
    f.__dict__["_sign_nodes"] = nodes
    assert railtags.sign_nodes(f) == nodes
    kw.setdefault("signals", NO_SIGNALS)
    return navgraph.build(DE_FRAME, [f], flat, (0.0, 0.0), **kw)


def stops(g):
    """[(junction, kind, sorted approach nodes)] of the graph's controlled junctions."""
    return [(s["j"], s["kind"], sorted(a[0] for a in s["app"])) for s in g["signals"]]


def into(g, j="cJ"):
    """{the other node: drivability} of the edges at node j (its approaches)."""
    return {(e[1] if e[0] == j else e[0]): e[6] for e in g["edges"] if j in e[:2]}


FIXTURES = {"stretch T": dict(w="secondary", e="secondary", s="residential"),
            "residential T": dict(w="residential", e="residential", s="residential"),
            "residential cross": dict(w="residential", e="residential", n="residential", s="residential"),
            "secondary T": dict(w="secondary", e="secondary", s="secondary"),
            "mixed cross": dict(w="secondary", e="secondary", n="secondary", s="residential")}
# today's graph of FIXTURES in town (urban_fn town), no signal mapped (and "lit T": the secondary T with one at J), the
# German junction signs off: (stops(), into()) recorded from navgraph.build at 41c0dea6 (literals, no git show)
TODAY = {
    "stretch T": ([("cJ", "stop", ["sid002_333d"])], {"sid000_500d": 1.0, "sid001_500d": 1.0, "sid002_333d": 0.8}),
    "residential T": ([("cJ", "stop", ["sid002_333d"])],
                      {"sid000_667d": 0.8, "sid001_333d": 0.8, "sid002_333d": 0.8}),
    "residential cross": ([("cJ", "stop", ["sid000_667d", "sid001_333d", "sid002_333d", "sid003_333d"])],
                          {"sid000_667d": 0.8, "sid001_333d": 0.8, "sid002_333d": 0.8, "sid003_333d": 0.8}),
    "secondary T": ([], {"sid000_500d": 1.0, "sid001_500d": 1.0, "sid002_500d": 1.0}),
    "mixed cross": ([("cJ", "stop", ["sid003_333d"])],
                    {"sid000_500d": 1.0, "sid001_500d": 1.0, "sid002_500d": 1.0, "sid003_333d": 0.8}),
    "lit T": ([("cJ", "signal", ["sid000_500d", "sid001_500d", "sid002_500d"])],
              {"sid000_500d": 1.0, "sid001_500d": 1.0, "sid002_500d": 1.0}),
}


class GermanJunctionTest(unittest.TestCase):
    """The traffic AI at Germany's junctions (de_on, the junction point inside Germany): designs.controls' decision,
    the road tiles' own (the same arms, context and arm lines): a stop controller on the 206 and right-before-left arms
    (the spec's B4: an invisible stop for the AI at right before left), a drivability step on the 205 arms' and the
    exits' (StVO § 10) approaches below the arms they give way to (the stock AI gives way on an incoming edge of lower
    drivability: the roundabouts' method), signals and roundabouts as today, nothing at a node not decided."""

    def build(self, name, **kw):
        lines, classes, con = junction(FIXTURES[name])
        return de_graph(lines, classes, con, **kw)

    def test_de_give_way_lowers_the_approach(self):
        """A T on a stretch (secondary, 1.0) with a residential side road (0.8, 205): already lower, unchanged; three
        secondaries (the straightest pair through, S 205): S's approach 0.98, the pair's 1.0; no stop on either."""
        g = self.build("stretch T", de_on=True)
        self.assertEqual(into(g), {"sid000_500d": 1.0, "sid001_500d": 1.0, "sid002_333d": 0.8})
        self.assertEqual(g["signals"], [])
        g = self.build("secondary T", de_on=True)
        self.assertEqual(into(g), {"sid000_500d": 1.0, "sid001_500d": 1.0, "sid002_500d": 0.98})
        self.assertEqual(g["signals"], [])
        self.assertEqual(self.build("stretch T", de_on=True, urban_fn=town)["signals"], [],
                         "in town too: no stop on a 205 (today's stem stop is gone)")

    def test_de_stop_only_on_mapped(self):
        """A mapped stop on S (Overture's highway=stop 10 m out, or the lane shards' DE:206 node there) of the stretch
        T: one stop junction, one approach (S); E and W (306) none. A mixed cross (N secondary 205, S's stop): the stop
        on S only, N's approach stepped."""
        for kw in (dict(priority=np.array([[0.0, -10.0, 1.0]])), dict(signs=[(0.0, -10.0, "DE:206")])):
            g = self.build("stretch T", de_on=True, **kw)
            self.assertEqual(stops(g), [("cJ", "stop", ["sid002_333d"])], kw)
            self.assertEqual(len(g["signals"][0]["app"][0]), 11, "the app's layout")
            g = self.build("mixed cross", de_on=True, **kw)
            self.assertEqual(stops(g), [("cJ", "stop", ["sid003_333d"])], kw)
            self.assertEqual(into(g), {"sid000_500d": 1.0, "sid001_500d": 1.0, "sid002_500d": 0.98, "sid003_333d": 0.8})
        g = self.build("mixed cross", de_on=True)
        self.assertEqual(g["signals"], [], "without it: N and S both give way")

    def test_de_rvl_all_arms_stop(self):
        """A residential crossroads (right before left): a stop on all four approaches, out of town too (today's graph
        has none there); in a residential T on its three."""
        for urban in (None, town):
            g = self.build("residential cross", de_on=True, urban_fn=urban)
            self.assertEqual(stops(g), [("cJ", "stop", ["sid000_667d", "sid001_333d", "sid002_333d", "sid003_333d"])])
            self.assertTrue(all(a[7:] == [None] * 4 for a in g["signals"][0]["app"]), "no head: no stop sign stands")
            self.assertEqual(set(into(g).values()), {0.8}, "no step")
        self.assertEqual(stops(self.build("residential T", de_on=True)),
                         [("cJ", "stop", ["sid000_667d", "sid001_333d", "sid002_333d"])])

    def test_de_short_approach(self):
        """A short approach: a 205 arm (S of three secondaries) 10 m from its approach node (the next connector K): its
        edge stepped all the same, no stop, nothing raised; a right-before-left arm that short: no stop (today's skip),
        the other three stop."""
        lines = [ARMS["w"], ARMS["e"], [(0, 0), (0, -10)], [(0, -10), (0, -100)]]
        con = [(0, "W", 0.0, 0.0), (0, "J", 1.0, 1.0), (1, "J", 0.0, 0.0), (1, "E", 1.0, 1.0),
               (2, "J", 0.0, 0.0), (2, "K", 1.0, 1.0), (3, "K", 0.0, 0.0), (3, "S", 1.0, 1.0)]
        g = de_graph(lines, ["secondary"] * 4, con, de_on=True)
        self.assertEqual(into(g)["cK"], 0.98)
        self.assertEqual(g["signals"], [])
        lines = [ARMS["w"], ARMS["e"], ARMS["n"], [(0, 0), (0, -10)], [(0, -10), (0, -100)]]
        con = [(0, "W", 0.0, 0.0), (0, "J", 1.0, 1.0), (1, "J", 0.0, 0.0), (1, "E", 1.0, 1.0), (2, "J", 0.0, 0.0),
               (2, "N", 1.0, 1.0), (3, "J", 0.0, 0.0), (3, "K", 1.0, 1.0), (4, "K", 0.0, 0.0), (4, "S", 1.0, 1.0)]
        g = de_graph(lines, ["residential"] * 5, con, de_on=True)
        self.assertEqual(stops(g), [("cJ", "stop", ["sid000_667d", "sid001_333d", "sid002_333d"])])

    def test_de_off_or_outside_is_todays(self):
        """The setting off: today's graph (recorded), Germany's border data untouched; on, with Germany patched away
        (outside Germany): the same graph."""
        import countrybounds
        import germany

        def untouched(*a, **k):
            raise AssertionError("Germany's border data asked with the setting off")

        def nowhere(lon, lat):
            return np.zeros(np.size(lon), bool)
        cases = dict((name, (name, {})) for name in FIXTURES)
        cases["lit T"] = ("secondary T", dict(signals=np.array([[5.0, 5.0]])))
        cases["mixed cross"] = ("mixed cross", dict(priority=np.array([[0.0, -10.0, 1.0]])))
        for case, (name, kw) in cases.items():
            with mock.patch.object(germany, "contains", untouched), \
                    mock.patch.object(countrybounds, "bundled", untouched):
                off = self.build(name, urban_fn=town, **kw)
                self.assertEqual(self.build(name, urban_fn=town, de_on=False, **kw), off, case)
            self.assertEqual((stops(off), into(off)), TODAY[case], case)
            with mock.patch.object(germany, "contains", nowhere):
                self.assertEqual(self.build(name, urban_fn=town, de_on=True, **kw), off, case)

    def test_de_the_junction_point_decides(self):
        """A window across the border (Germany patched to the half-plane west of x = cut): the residential crossroads J
        at (0, 0) is decided where its point is in Germany (a stop on all four), as today where it is not (here: none,
        out of town), whatever else of the window is (designs.decide's rule)."""
        import germany
        for cut, want in ((-300.0, []), (50.0, [("cJ", "stop", ["sid000_667d", "sid001_333d", "sid002_333d",
                                                                "sid003_333d"])])):
            lon_cut = float(DE_FRAME.enu_to_geodetic(np.array(cut), np.array(0.0), 0.0)[1])
            west = lambda lon, lat: np.atleast_1d(np.asarray(lon, float) <= lon_cut)     # noqa: E731
            with mock.patch.object(germany, "contains", west):
                self.assertEqual(stops(self.build("residential cross", de_on=True)), want, cut)

    def test_de_signals_and_roundabouts_as_today(self):
        """A signalised junction in Germany (a signal mapped at J) and a roundabout's entry (the ring's own
        drivability method, no stop): today's graph."""
        lit = dict(signals=np.array([[5.0, 5.0]]), urban_fn=town)
        self.assertEqual(self.build("secondary T", de_on=True, **lit), self.build("secondary T", **lit))
        from test_european_traffic import roundabout
        f = in_germany(roundabout())
        on, off = (navgraph.build(DE_FRAME, [f], flat, (0.0, 0.0), signals=NO_SIGNALS, de_on=v) for v in (True, False))
        self.assertEqual(on, off)
        self.assertEqual(on["signals"], [])

    def test_de_exits_give_way(self):
        """StVO § 10: an arm that is no street (here of class unknown, 0.6) at a living street junction (0.5,
        right before left among them) gives way: its approach a step below theirs, no stop on it."""
        lines, classes, con = junction(dict(w="living_street", e="living_street", n="living_street", s="unknown"))
        g = de_graph(lines, classes, con, de_on=True)
        ways = into(g)
        exit_ = next(k for k in ways if k.startswith("sid003_"))
        self.assertEqual(ways[exit_], 0.48)
        self.assertEqual({v for k, v in ways.items() if k != exit_}, {0.5})
        self.assertEqual(len(g["signals"]), 1)
        self.assertEqual(len(g["signals"][0]["app"]), 3)
        self.assertNotIn(exit_, [a[0] for a in g["signals"][0]["app"]])

    # A side road that goes on through the next junction as its priority road (one graph edge, 30 m): J a primary
    # W - E with a secondary J - K south; K: J - K going on as K - S, K - Q east (205); Q: K - Q going on as Q - R,
    # Q - T south (205)
    CHAIN = ([ARMS["w"], ARMS["e"], [(0, 0), (0, -30)], [(0, -30), (0, -130)], [(0, -30), (30, -30)],
              [(30, -30), (130, -30)], [(30, -30), (30, -60)], [(30, -60), (30, -160)]],
             ["primary", "primary"] + ["secondary"] * 6,
             [(0, "W", 0.0, 0.0), (0, "J", 1.0, 1.0), (1, "J", 0.0, 0.0), (1, "E", 1.0, 1.0), (2, "J", 0.0, 0.0),
              (2, "K", 1.0, 1.0), (3, "K", 0.0, 0.0), (3, "S", 1.0, 1.0), (4, "K", 0.0, 0.0), (4, "Q", 1.0, 1.0),
              (5, "Q", 0.0, 0.0), (5, "R", 1.0, 1.0), (6, "Q", 0.0, 0.0), (6, "T", 1.0, 1.0), (7, "T", 0.0, 0.0),
              (7, "U", 1.0, 1.0)])
    # ... and a cycle of them: a triangle of secondaries (30 m sides) A (0, 0), B (30, 0), C (15, 26), each corner's
    # road out (100 m) straight on from one side: A's from C - A, B's from A - B, C's from B - C; so A - B is A's 205
    # and B's priority road, B - C B's 205 and C's, C - A C's 205 and A's
    CYCLE = ([[(0, 0), (30, 0)], [(30, 0), (15, 25.98)], [(15, 25.98), (0, 0)], [(0, 0), (-50, -86.6)],
              [(30, 0), (130, 0)], [(15, 25.98), (-35, 112.58)]], ["secondary"] * 6,
             [(0, "A", 0.0, 0.0), (0, "B", 1.0, 1.0), (1, "B", 0.0, 0.0), (1, "C", 1.0, 1.0), (2, "C", 0.0, 0.0),
              (2, "A", 1.0, 1.0), (3, "A", 0.0, 0.0), (3, "XA", 1.0, 1.0), (4, "B", 0.0, 0.0), (4, "XB", 1.0, 1.0),
              (5, "C", 0.0, 0.0), (5, "XC", 1.0, 1.0)])

    def test_de_give_way_through_a_shared_approach(self):
        """J - K is J's 205 (0.98 below J's primary) and K's priority road; K - Q, K's 205,
        a step below that (0.96): strictly below the road from J, the stock AI's give-way there."""
        lines, classes, con = self.CHAIN
        g = de_graph(lines[:5], classes[:5], con[:10], de_on=True)
        self.assertEqual(into(g, "cJ")["cK"], 0.98)
        k = into(g, "cK")
        self.assertEqual((k["cJ"], k[next(n for n in k if n.startswith("sid003_"))]), (0.98, 1.0))
        self.assertEqual(k["cQ"], 0.96)

    def test_de_give_way_along_a_chain(self):
        """Three junctions in a chain (J, K, Q): each 205 a step below the one before, 0.98, 0.96, 0.94; the priority
        roads through as they were."""
        g = de_graph(*self.CHAIN, de_on=True)
        self.assertEqual((into(g, "cJ")["cK"], into(g, "cK")["cQ"], into(g, "cQ")["cT"]), (0.98, 0.96, 0.94))
        q = into(g, "cQ")
        self.assertEqual(q[next(n for n in q if n.startswith("sid005_"))], 1.0)
        self.assertEqual({v for n, v in into(g, "cJ").items() if n != "cK"}, {1.0})

    def test_de_give_way_cycle_stops_at_the_cap(self):
        """A cycle of such junctions would step down for ever: after the four passes (the cap, DE_YIELD_PASSES) its
        three sides stand at 1.0 - 4 x 0.02 (each one's 205 tied with its priority road again: the cap's cost); the
        roads out as they were."""
        g = de_graph(*self.CYCLE, de_on=True, urban_fn=town)
        self.assertEqual(g["signals"], [])
        self.assertEqual((into(g, "cA")["cB"], into(g, "cB")["cC"], into(g, "cC")["cA"]), (0.92, 0.92, 0.92))
        for j, out in (("cA", "sid003_"), ("cB", "sid004_"), ("cC", "sid005_")):
            got = into(g, j)
            self.assertEqual(got[next(n for n in got if n.startswith(out))], 1.0, j)

    def test_de_give_way_in_any_order(self):
        """The steps do not depend on the order the junctions are taken in (each pass from the values at its start,
        min only): the chain's and the cycle's give-way pass rerun on their graphs' edges, the junctions shuffled."""
        import copy
        import random
        real, seen = navgraph._give_way, []

        def record(edges, approaches, yields):
            seen.append((copy.deepcopy(edges), approaches, list(yields)))
            return real(edges, approaches, yields)
        for name, roads_ in (("chain", self.CHAIN), ("cycle", self.CYCLE)):
            seen.clear()
            with mock.patch.object(navgraph, "_give_way", record):
                de_graph(*roads_, de_on=True)
            edges, approaches, yields = seen[0]
            self.assertGreaterEqual(len(yields), 3, name)
            want = copy.deepcopy(edges)
            real(want, approaches, yields)
            self.assertNotEqual(want, edges, name)
            for seed in range(8):
                order = list(yields)
                random.Random(seed).shuffle(order)
                got = copy.deepcopy(edges)
                real(got, approaches, order)
                self.assertEqual(got, want, (name, seed))

    def test_de_town_before_the_drawing(self):
        """The kernel's town is the land cover's before the drawing's overrides (the graph's context, as the road
        tiles'): an unclassified crossroads in town, one arm a mapped roundabout's way (drawn rural: roads.road_specs),
        is right before left (in town), a stop on all four; by the arms' drawn specs it would be a rural 301 / 205."""
        lines, classes, con = junction(dict(w="unclassified", e="unclassified", n="unclassified", s="unclassified"))
        import railtags
        f = in_germany(features(lines, classes))
        f.attrs["osm_junction"] = np.array([None, "roundabout", None, None], object)
        f.rules["connectors"] = rules(con)
        for k in ("oneway", "max_speed_kmh"):
            f.rules[k] = rules([], "float")
        f.rules["subclass"] = rules([])
        self.assertFalse(roads.road_specs(f, town)[1].urban, "(drawn rural)")
        self.assertEqual(railtags.sign_nodes(f), [])
        g = navgraph.build(DE_FRAME, [f], flat, (0.0, 0.0), town, signals=NO_SIGNALS, de_on=True)
        self.assertEqual([(s["j"], len(s["app"])) for s in g["signals"]], [("cJ", 4)])

    def test_de_not_decided_is_nothing(self):
        """A carriageway split (a two-way residential street parting into its two one-way carriageways) in town:
        not decided, no German change and no US stop either (the road tiles sign nothing there)."""
        lines = [ARMS["w"], [(0, 0), (100, 15)], [(100, -15), (0, 0)]]
        con = [(0, "W", 0.0, 0.0), (0, "J", 1.0, 1.0), (1, "J", 0.0, 0.0), (1, "O", 1.0, 1.0),
               (2, "I", 0.0, 0.0), (2, "J", 1.0, 1.0)]
        args = (lines, ["residential"] * 3, con, [(1, 1.0), (2, 1.0)])
        self.assertTrue(de_graph(*args, urban_fn=town)["signals"], "(today: the US stem stop)")
        g = de_graph(*args, urban_fn=town, de_on=True)
        self.assertEqual(g["signals"], [])
        self.assertEqual(into(g), into(de_graph(*args, urban_fn=town)))

    def test_de_one_decision_with_the_road_tiles(self):
        """Junctions A (0, 0) and B (30, 0), all residential, joined by a road bowing out 12 m (42.8 m long) with a
        mapped give-way 20.4 m along it from A (22.4 m from B: test_designs' bowed road): the road tiles' decision
        (designs.decide: measured along the road) is A's 205, B's right before left; so here: A's approach on it a step
        below A's 301 pair, B a stop on all three (along each end's first direction both would claim it)."""
        lines = [[(0, 0), (8, 12), (22, 12), (30, 0)], [(0, 0), (-80, 0)], [(0, 0), (0, -80)], [(30, 0), (110, 0)],
                 [(30, 0), (30, -80)]]
        con = [(0, "A", 0.0, 0.0), (0, "B", 1.0, 1.0), (1, "A", 0.0, 0.0), (1, "W", 1.0, 1.0), (2, "A", 0.0, 0.0),
               (2, "S", 1.0, 1.0), (3, "B", 0.0, 0.0), (3, "E", 1.0, 1.0), (4, "B", 0.0, 0.0), (4, "T", 1.0, 1.0)]
        g = de_graph(lines, ["residential"] * 5, con, de_on=True, priority=np.array([[14.0, 12.5, 0.0]]))
        self.assertEqual([(s["j"], s["kind"], len(s["app"])) for s in g["signals"]], [("cB", "stop", 3)])
        a = into(g, "cA")
        self.assertEqual(a[next(k for k in a if k.startswith("sid000_"))], 0.78)
        self.assertEqual({v for k, v in a.items() if not k.startswith("sid000_")}, {0.8})
        self.assertEqual(set(into(g, "cB").values()), {0.8})

    def test_de_one_decision_cross_check(self):
        """The road tiles and the road graph make one decision of each German junction: designs.controls recorded (its
        real answer) while roads.build_tile (de_on) and navgraph.build (de_on) take the same roads: per junction point
        the same [(arm, what, plate)], on the bowed road between two junctions with its mapped give-way, a stretch T
        with a mapped 205 (the lane shards' node) on its stem, or on its east arm (the stretch's: W and S then the pair,
        301, whichever side missed the node would differ) and a residential crossroads."""
        import designs
        import railtags
        bowed = ([[(0, 0), (8, 12), (22, 12), (30, 0)], [(0, 0), (-80, 0)], [(0, 0), (0, -80)], [(30, 0), (110, 0)],
                  [(30, 0), (30, -80)]], ["residential"] * 5,
                 [(0, "A", 0.0, 0.0), (0, "B", 1.0, 1.0), (1, "A", 0.0, 0.0), (1, "W", 1.0, 1.0), (2, "A", 0.0, 0.0),
                  (2, "S", 1.0, 1.0), (3, "B", 0.0, 0.0), (3, "E", 1.0, 1.0), (4, "B", 0.0, 0.0), (4, "T", 1.0, 1.0)])
        cases = {"bowed road": bowed + (np.array([[14.0, 12.5, 0.0]]), ()),
                 "stretch T": junction(FIXTURES["stretch T"]) + (None, [(0.0, -10.0, "DE:205")]),
                 "stretch T, 205 east": junction(FIXTURES["stretch T"]) + (None, [(10.0, 0.0, "DE:205")]),
                 "residential cross": junction(FIXTURES["residential cross"]) + (None, ())}
        at = np.array([100.0, 100.0])           # (every junction inside road tile (0, 0))
        real = designs.controls
        for name, (lines, classes, con, priority, signs) in cases.items():
            f = in_germany(features([[tuple(np.add(p, at)) for p in ln] for ln in lines], classes))
            f.rules["connectors"] = rules(list(con))
            for k in ("oneway", "max_speed_kmh"):
                f.rules[k] = rules([], "float")
            f.rules["subclass"] = rules([])
            nodes = []
            for x, y, code in signs:
                lat, lon, _ = DE_FRAME.enu_to_geodetic(np.array(x + at[0]), np.array(y + at[1]), 0.0)
                nodes.append((float(lon), float(lat), {"traffic_sign": code}))
            f.__dict__["_sign_nodes"] = nodes
            self.assertEqual(railtags.sign_nodes(f), nodes)
            rows = None if priority is None else priority + [at[0], at[1], 0.0]
            got = {}
            for side in ("tile", "graph"):
                seen = got.setdefault(side, {})

                def record(arms, *a, **k):
                    out = real(arms, *a, **k)
                    seen[tuple(np.round(np.asarray(arms[0][3][:2], np.float64), 1))] = sorted(
                        (str(x[0]), w, int(p)) for x, w, p in out)
                    return out
                with mock.patch.object(designs, "controls", side_effect=record):
                    if side == "tile":
                        nodes_in = None if rows is None else {"crossings": [], "de_crossings": [], "priority": rows,
                                                              "stop_lines": np.zeros((0, 2))}
                        roads.build_tile(DE_FRAME, 0, 0, [f], flat, town, walks=[], props=[], signals=NO_SIGNALS,
                                         country="DE", nodes=nodes_in, de_on=True)
                    else:
                        navgraph.build(DE_FRAME, [f], flat, tuple(at), town, signals=NO_SIGNALS, priority=rows,
                                       de_on=True)
            self.assertTrue(got["tile"], name)
            self.assertEqual(got["graph"], got["tile"], name)
            self.assertTrue(all(isinstance(w, int) and w for ctl in got["tile"].values() for _, w, _ in ctl), name)


class RulesTest(unittest.TestCase):
    def test_default_speeds_by_country(self):
        class F:            # a feature without a mapped limit
            rules = {}
        mw = roads.Spec(11.0, "paved", False, "motorway", True)
        self.assertAlmostEqual(navgraph.road_mph(F, 0, mw), 65.0)
        self.assertAlmostEqual(navgraph.road_mph(F, 0, mw, mph=False) * 1.609344, 120.0)
        town = roads.Spec(9.0, "paved", True, "primary", False)
        self.assertAlmostEqual(navgraph.road_mph(F, 0, town, mph=False), 35.0, msg="in town: the urban cap")
        res = roads.Spec(6.0, "paved", True, "residential", False)
        self.assertAlmostEqual(navgraph.road_mph(F, 0, res, mph=False), 25.0)

    def test_us_motorways_default_to_their_states_limit(self):
        # Tann2019 2026-11-05: Montana's I-94 near Huntley (no mapped limit) is 80, the traffic drove the flat 65
        def at(lat, lon):
            class F:
                rules = {}
                attrs = {"cx": np.array([lon]), "cy": np.array([lat])}
            return F
        rural = roads.Spec(11.0, "paved", False, "motorway", True)
        urban = rural._replace(urban=True)
        ramp = rural._replace(link=True)
        huntley, billings = at(45.87295, -108.33252), at(45.7833, -108.5007)
        self.assertEqual(navgraph.road_mph(huntley, 0, rural, country="US"), 80.0)
        self.assertEqual(navgraph.road_mph(billings, 0, urban, country="US"), 65.0, "Montana's urban interstates")
        self.assertEqual(navgraph.road_mph(huntley, 0, urban, country="US"), 80.0,
                         "built up beside it past a small town (Huntley): still the rural limit, no city near")
        self.assertEqual(navgraph.road_mph(huntley, 0, ramp, country="US"), 65.0, "a ramp: the class default")
        self.assertEqual(navgraph.road_mph(at(39.2834, -76.5934), 0, rural, country="US"), 70.0, "Maryland")
        self.assertEqual(navgraph.road_mph(at(31.0, -100.0), 0, rural, country="US"), 75.0, "Texas")
        self.assertEqual(navgraph.road_mph(huntley, 0, rural), 65.0, "no country: as before")
        self.assertEqual(navgraph.road_mph(at(49.5, -97.0), 0, rural, country="US"), 65.0, "outside the states: 65")
        self.assertEqual(len(navgraph.US_MOTORWAY_MPH), 51)

    def test_us_rural_highways_default_to_55(self):
        """GitHub #888: a Texas farm-to-market road with no mapped limit was driven at town speeds; outside towns the
        fast classes take the usual rural limit, in town (and anywhere outside the US) as before."""
        class F:
            rules = {}
        rural = roads.Spec(7.0, "paved", False, "secondary", False)
        self.assertEqual(navgraph.road_mph(F, 0, rural, country="US"), 55.0)
        self.assertEqual(navgraph.road_mph(F, 0, rural._replace(cls="tertiary"), country="US"), 45.0)
        self.assertEqual(navgraph.road_mph(F, 0, rural._replace(cls="trunk"), country="US"), 65.0)
        self.assertEqual(navgraph.road_mph(F, 0, rural._replace(urban=True), country="US"), 35.0, "in town: the cap")
        self.assertEqual(navgraph.road_mph(F, 0, rural._replace(link=True), country="US"), 40.0, "a slip road")
        self.assertEqual(navgraph.road_mph(F, 0, rural._replace(cls="unclassified"), country="US"), 30.0)
        self.assertEqual(navgraph.road_mph(F, 0, rural), 40.0, "no country: as before")

        class Mapped:
            class _T:
                start, end, value = [0.0], [1.0], [72.42]

                def rows(self, i):
                    return [0]
            rules = {"max_speed_kmh": _T()}
        self.assertAlmostEqual(navgraph.road_mph(Mapped, 0, rural, country="US"), 45.0, places=1, msg="mapped wins")

    def test_swedish_defaults_are_the_law(self):
        class F:
            rules = {}
        kmh = lambda spec: navgraph.road_mph(F, 0, spec, mph=False, country="SE") * 1.609344    # noqa: E731
        self.assertAlmostEqual(kmh(roads.Spec(11.0, "paved", False, "motorway", True)), 110.0)
        self.assertAlmostEqual(kmh(roads.Spec(9.0, "paved", False, "primary", False)), 70.0)
        self.assertAlmostEqual(kmh(roads.Spec(9.0, "paved", True, "primary", False)), 50.0)
        self.assertAlmostEqual(kmh(roads.Spec(6.0, "paved", True, "residential", False)), 50.0)
        self.assertLess(kmh(roads.Spec(4.0, "paved", True, "service", False)), 25.0)

    def test_german_zone_roads_drive_the_zones_limit(self):
        """A road of a German zone without a mapped limit drives the zone's (its 274.1's) with the German junction
        signs on; off, or with a mapped limit, as before."""
        class F:
            rules = {}
            attrs = {"osm_zone_maxspeed": np.array(["DE:20", "DE:30", None, "DE:50"], object)}
        res = roads.Spec(6.0, "paved", True, "residential", False)
        kmh = lambda i, on: navgraph.road_mph(F, i, res, mph=False, country="DE", de_on=on) * 1.609344   # noqa: E731
        self.assertAlmostEqual(kmh(0, True), 20.0)
        self.assertAlmostEqual(kmh(1, True), 30.0)
        self.assertAlmostEqual(kmh(0, False), 25.0 * 1.609344, msg="off: the class default")
        self.assertAlmostEqual(kmh(2, True), 25.0 * 1.609344, msg="no zone")
        self.assertAlmostEqual(kmh(3, True), 25.0 * 1.609344, msg="not a zone of the family")
        class Mapped(F):
            rules = {"max_speed_kmh": rules([(0, 10.0)], "float")}
        self.assertAlmostEqual(navgraph.road_mph(Mapped, 0, res, mph=False, de_on=True) * 1.609344, 10.0,
                               msg="a mapped limit first")

    def test_refresh_drops_a_build_of_the_old_setting(self):
        """Tracker.refresh (the German junction signs switched): the next pose builds again where the player stands,
        and a build running at the switch lands unsent."""
        import asyncio
        import threading
        sent, builds = [], []
        conn = mock.Mock(send=sent.append)
        gate = threading.Event()

        def build(aid, x, y, serial):
            builds.append(serial)
            return f"g{serial}", 1, 1

        def slow(aid, x, y, serial):
            gate.wait(5)
            return build(aid, x, y, serial)

        async def run():
            loop = asyncio.get_running_loop()
            t = navgraph.Tracker()
            t.on_pose(conn, 1, 0.0, 0.0, loop, build)
            await asyncio.sleep(0.05)
            self.assertEqual([m["serial"] for m in sent], [1])
            t.on_pose(conn, 1, 0.0, 0.0, loop, build)
            self.assertEqual(builds, [1], "not moved: no build")
            t.refresh()
            t.on_pose(conn, 1, 0.0, 0.0, loop, build)
            await asyncio.sleep(0.05)
            self.assertEqual([m["serial"] for m in sent], [1, 2], "built again in place")
            t.on_pose(conn, 1, 9999.0, 0.0, loop, slow)     # (moved away: a build, still running at the switch)
            await asyncio.sleep(0.02)
            t.refresh()
            gate.set()
            await asyncio.sleep(0.05)
            self.assertEqual([m["serial"] for m in sent], [1, 2], "the old setting's build is not sent")
            t.on_pose(conn, 1, 9999.0, 0.0, loop, build)
            await asyncio.sleep(0.05)
            self.assertEqual([m["serial"] for m in sent], [1, 2, 4])
            t.close()
        asyncio.run(run())

    def test_driving_side_and_signals_by_country(self):
        self.assertEqual(navgraph.road_rules("US"), {"rightHandDrive": False, "turnOnRed": True, "redYellow": False,
                                                     "country": "US", "mph": True})
        self.assertFalse(navgraph.road_rules("CH")["mph"])
        self.assertTrue(navgraph.road_rules("GB")["mph"])
        self.assertTrue(navgraph.road_rules("gb")["rightHandDrive"], "the game's name: true = driving on the left")
        self.assertTrue(navgraph.road_rules("GB")["redYellow"])
        self.assertFalse(navgraph.road_rules("NL")["redYellow"], "Dutch signals: red straight to green")
        self.assertFalse(navgraph.road_rules("DE")["rightHandDrive"])
        self.assertTrue(navgraph.road_rules("JP")["rightHandDrive"])
        self.assertFalse(navgraph.road_rules(None)["rightHandDrive"], "unknown: US rules")
        for cc in ("BS", "BZ", "AG", "LC", "PW", "MH"):         # mph outside the US and the UK
            self.assertTrue(navgraph.road_rules(cc)["mph"], cc)
        self.assertTrue(navgraph.road_rules("BS")["rightHandDrive"], "the Bahamas: mph, on the left")
        self.assertFalse(navgraph.road_rules("BZ")["rightHandDrive"], "Belize: mph, on the right")
        self.assertTrue(navgraph.road_rules("NF")["rightHandDrive"], "Norfolk Island drives on the left")
        self.assertFalse(navgraph.road_rules("FR")["mph"])


if __name__ == "__main__":
    unittest.main()


class PostedLimitTest(unittest.TestCase):
    """GitHub #1061: an edge's posted limit (e[9]) where its AI target (e[5]) is not it, or not mapped."""

    def test_posted_field(self):
        import navgraph as ng
        self.assertIsNone(ng.posted_field(24.6, 24.6, True))          # (the target is the limit: nothing)
        self.assertIsNone(ng.posted_field(24.6, 24.6, False))
        self.assertEqual(ng.posted_field(24.6, 18.0, True), 24.6)     # (a bend: the limit)
        self.assertEqual(ng.posted_field(24.6, 18.0, False), -24.6)   # (the class's default: negative)
        self.assertIsNone(ng.posted_field(None, 10.0, True))
