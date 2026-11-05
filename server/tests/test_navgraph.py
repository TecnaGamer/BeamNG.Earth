"""navgraph: the drivable roads as the stock map's graph: connectors shared at junctions, one-way direction,
lanes, speed limits, clipped to the window."""
import sys
import unittest
from unittest import mock
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import navgraph  # noqa: E402
import roads  # noqa: E402
from test_roads import FRAME, features, flat, rules  # noqa: E402


def graph(lines, classes, connectors=(), oneway=(), speeds=(), subclass=(), centre=(0.0, 0.0), window=1100.0):
    f = features(lines, classes)
    f.rules["connectors"] = rules(list(connectors))
    f.rules["oneway"] = rules(list(oneway), "float")
    f.rules["max_speed_kmh"] = rules(list(speeds), "float")
    f.rules["subclass"] = rules(list(subclass))
    return navgraph.build(FRAME, [f], flat, centre, window=window)


class GraphTest(unittest.TestCase):
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
        ramp = roads.Spec(roads.LINK_M[1], "paved", False, "motorway", True)
        self.assertEqual(navgraph._lanes(navgraph.driving_width(ramp), True), (1, 0), "a ramp: one lane (GitHub #107)")

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
        g = navgraph.build(FRAME, [f], flat, (0.0, 0.0))
        for e in g["edges"]:
            mid = (g["nodes"][e[0]][0] + g["nodes"][e[1]][0]) / 2
            self.assertFalse(130 < mid < 200, "an edge over the tunnel")
        self.assertTrue(g["edges"])
        # ... but drawn on the minimap (its "lines"): the stretch from 120 to 210 m covered, end to end
        xs = sorted(x for ln in g["lines"] for x in (ln[0], ln[2]))
        self.assertTrue(g["lines"] and xs[0] <= 130 and xs[-1] >= 200, g["lines"])
        self.assertTrue(all(len(ln) == 5 and abs(ln[1]) < 1 and abs(ln[3]) < 1 for ln in g["lines"]))

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
            self.assertEqual(lh[nb][7:11], rh[nb][7:11], f"{nb}: the head's transform")
        self.assertLess(lh["cS"][1], 0.0, "northbound stop point on the west (left) half")
        self.assertGreater(rh["cS"][1], 0.0, "and on the east half for right-hand traffic")


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

    def test_swedish_defaults_are_the_law(self):
        class F:
            rules = {}
        kmh = lambda spec: navgraph.road_mph(F, 0, spec, mph=False, country="SE") * 1.609344    # noqa: E731
        self.assertAlmostEqual(kmh(roads.Spec(11.0, "paved", False, "motorway", True)), 110.0)
        self.assertAlmostEqual(kmh(roads.Spec(9.0, "paved", False, "primary", False)), 70.0)
        self.assertAlmostEqual(kmh(roads.Spec(9.0, "paved", True, "primary", False)), 50.0)
        self.assertAlmostEqual(kmh(roads.Spec(6.0, "paved", True, "residential", False)), 50.0)
        self.assertLess(kmh(roads.Spec(4.0, "paved", True, "service", False)), 25.0)

    def test_driving_side_and_signals_by_country(self):
        self.assertEqual(navgraph.road_rules("US"), {"rightHandDrive": False, "turnOnRed": True, "redYellow": False,
                                                     "country": "US", "mph": True})
        self.assertFalse(navgraph.road_rules("CH")["mph"])
        self.assertTrue(navgraph.road_rules("GB")["mph"])
        self.assertTrue(navgraph.road_rules("gb")["rightHandDrive"], "the game's name: true = driving on the left")
        self.assertTrue(navgraph.road_rules("GB")["redYellow"])
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
