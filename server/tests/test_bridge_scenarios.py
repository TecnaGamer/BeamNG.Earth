"""Synthetic bridge scenarios (the user, 2026-10-31: "bridge widths dont always match the road widths"; a friend's
"test city" idea): small made-up road layouts on flat ground through the real spec and deck solvers, each checked for
what a driver sees: a deck as wide as the road it carries, decks of a divided road side by side without overlapping,
decks at the height of the road they continue. A bug found in a real place gets a scenario here (bug -> test -> fix).
.claude/devtools/bridgewidth.py measures the widths over a real area."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import roads  # noqa: E402
from test_approaches import flat_elev, FLAT  # noqa: E402
from test_roads import features, rules, FRAME  # noqa: E402

UNDER = [[(-200, 0), (200, 0)]]         # the street the bridges cross
BRIDGE = (0.45, 0.55)                   # each carried road's bridged range (of its length): over the street


def scenario(lines, classes, names=None, oneway=(), links=(), bridged=None):
    """Features of lines (+ the street under them) with the given classes, names, one-way and link roads; bridged:
    which lines carry a bridge over the street (default all)."""
    lines = list(lines) + UNDER
    classes = list(classes) + ["residential"]
    bridged = range(len(lines) - 1) if bridged is None else bridged
    flags = [(i, "is_bridge", *BRIDGE) for i in bridged]
    f = features(lines, classes, flags=flags, ids=[f"r{i}" for i in range(len(lines))])
    f.attrs["name"] = np.array(list(names or [""] * (len(lines) - 1)) + ["Under St"])
    f.rules["oneway"] = rules([(i, 1.0) for i in oneway], "float")
    f.rules["subclass"] = rules([(i, "link") for i in links])
    return f


def decks(f):
    """{fid: (spec, xy (k, 2), z (k,))} of the solved bridge parts."""
    bp = roads.BridgeProfiles(FRAME, lambda key: f, flat_elev)
    return {fid: (spec, np.asarray(xy, np.float64)[:, :2], np.asarray(z, np.float64))
            for fid, spec, xy, z, _lon in bp.parts([f], (-400, -400, 400, 400))}


class DeckWidthScenarios(unittest.TestCase):
    def assert_deck_as_wide_as_its_road(self, f):
        ground, bridge = roads.road_specs(f), roads.bridge_specs(f)
        n = 0
        for i, b in enumerate(bridge):
            if b is None:
                continue
            n += 1
            self.assertAlmostEqual(b.width, ground[i].width, delta=0.05, msg=f"r{i}: deck {b.width} m, road {ground[i].width} m")
        self.assertGreater(n, 0)
        return ground, bridge

    def test_two_way_road(self):
        self.assert_deck_as_wide_as_its_road(scenario([[(0, -300), (0, 300)]], ["primary"]))

    def test_one_way_ramp(self):
        self.assert_deck_as_wide_as_its_road(scenario([[(0, -300), (40, 300)]], ["motorway"], oneway=[0], links=[0]))

    def test_divided_motorway_mapped_one_way(self):
        f = scenario([[(-9, -300), (-9, 300)], [(9, 300), (9, -300)]], ["motorway"] * 2, names=["I-95", "I-95"],
                     oneway=[0, 1])
        self.assert_deck_as_wide_as_its_road(f)

    def test_divided_road_the_map_does_not_mark_one_way(self):
        """Two same-named carriageways side by side (dual_carriageways): the decks were the class's two-way width, 18.8 m
        decks on 12 m motorway carriageways in Baltimore (bridgewidth.py, 2026-10-31)."""
        f = scenario([[(-8, -300), (-8, 300)], [(8, 300), (8, -300)]], ["primary"] * 2, names=["Main", "Main"])
        ground, bridge = self.assert_deck_as_wide_as_its_road(f)
        self.assertTrue(ground[0].median and bridge[0].median, "(both read as one carriageway of a divided road)")
        self.assertTrue(bridge[0].oneway and bridge[1].oneway)
        gap = 16.0
        self.assertLessEqual((bridge[0].width + bridge[1].width) / 2, gap - roads.DUAL_MIN_MEDIAN_M + 1e-6,
                             "the two decks do not overlap")

    def test_close_carriageways_never_overlap(self):
        """Carriageways 7 m apart: each deck capped so the two keep a barrier's width between them (dual_cap)."""
        f = scenario([[(-3.5, -300), (-3.5, 300)], [(3.5, 300), (3.5, -300)]], ["motorway"] * 2,
                     names=["I-83", "I-83"], oneway=[0, 1])
        _, bridge = self.assert_deck_as_wide_as_its_road(f)
        self.assertLessEqual((bridge[0].width + bridge[1].width) / 2, 7.0 - roads.DUAL_MIN_MEDIAN_M + 1e-6)


class DeckHeightScenarios(unittest.TestCase):
    def test_divided_overpass_decks_side_by_side_at_one_height(self):
        f = scenario([[(-8, -300), (-8, 300)], [(8, 300), (8, -300)]], ["primary"] * 2, names=["Main", "Main"])
        d = decks(f)
        self.assertEqual(set(d), {"r0", "r1"})
        # (the headroom over the street comes with the grading's approach ramps, tested in test_approaches / test_grading:
        # on flat ground without a grader the decks rise only what their own span allows)
        tops = [float(z.max()) for _s, _xy, z in d.values()]
        self.assertGreater(min(tops), FLAT, "raised over the street")
        self.assertLess(abs(tops[0] - tops[1]), 0.5, "a divided road's decks at one height")
        for fid, (spec, xy, z) in d.items():
            self.assertAlmostEqual(z[0], FLAT, delta=0.6, msg=f"{fid} starts on its approach")
            self.assertAlmostEqual(z[-1], FLAT, delta=0.6, msg=f"{fid} ends on its approach")



def chain(widths, classes=None, bridged=(), extra=()):
    """Roads end to end along x (each 200 m, from x = -200 on) with these widths (the map's width rule), + extra lines."""
    lines = [[(-200 + 200 * k, 0), (200 * k, 0)] for k in range(len(widths))] + [ln for ln, _ in extra]
    classes = list(classes or ["primary"] * len(widths)) + [c for _, c in extra]
    f = features(lines, classes, widths=[(k, float(w)) for k, w in enumerate(widths)],
                 flags=[(k, "is_bridge", 0.0, 1.0) for k in bridged], ids=[f"r{k}" for k in range(len(lines))])
    f.attrs["name"] = np.array([""] * len(lines))
    return f


def width_at(f, i, x, y=0.0):
    sp = roads.road_specs(f)[i]
    la, lo, _ = FRAME.enu_to_geodetic(np.array([float(x)]), np.array([float(y)]), 0.0)
    ws = roads.taper_widths(sp.width, roads.road_tapers(f)[i], lo, la)
    return sp.width if ws is None else float(ws[0])


class LaneTaperScenarios(unittest.TestCase):
    """Where the map's lane count changes the drawn width runs over a taper instead of stepping (the user, 2026-10-31:
    "the bridge geometry as lanes merge and split is rough")."""

    def test_two_ground_roads_meet_half_way(self):
        f = chain([10.0, 16.0])
        self.assertAlmostEqual(width_at(f, 0, 0.0), 13.0, delta=0.05, msg="the narrow one widens to half way")
        self.assertAlmostEqual(width_at(f, 1, 0.0), 13.0, delta=0.05, msg="the wide one narrows to half way")
        span = float(np.clip(6.0 * roads.TAPER_M_PER_M, *roads.TAPER_M))
        self.assertAlmostEqual(width_at(f, 0, -span - 1), 10.0, delta=0.01, msg="its own width past the taper")
        self.assertAlmostEqual(width_at(f, 1, span + 1), 16.0, delta=0.01)
        ws = [width_at(f, 0, x) for x in np.linspace(-span, 0.0, 30)]
        self.assertTrue(np.all(np.diff(ws) >= -1e-9), "widening steadily")
        self.assertLess(float(np.abs(np.diff(ws)).max()), 6.0 / 2 / 30 * 2.0, "no step (smoothstep: at most 1.5x the mean rate)")

    def test_ground_road_meets_a_wider_deck_at_its_width(self):
        """The deck keeps its width (its parapets and collision): the road onto it takes all of the change."""
        f = chain([10.0, 16.0], bridged=[1])
        self.assertAlmostEqual(width_at(f, 0, 0.0), roads.bridge_specs(f)[1].width, delta=0.05)
        self.assertEqual(roads.road_tapers(f)[1], (), "no taper on the deck")

    def test_small_change_and_side_roads_keep_their_width(self):
        self.assertEqual(list(roads.road_tapers(chain([10.0, 10.2]))), [(), ()], "under TAPER_MIN_DW: as before")
        f = chain([10.0], extra=[([(0.0, 0.0), (0.0, 200.0)], "primary")])      # a side road from its end, at 90 deg
        self.assertEqual(roads.road_tapers(f)[0], (), "a junction's arm is not a continuation")
        f = chain([10.0, 16.0], extra=[([(0.0, 0.0), (0.0, 200.0)], "residential")])
        self.assertEqual(list(roads.road_tapers(f))[:2], [(), ()], "across a junction: as real streets, no taper")
        f = chain([10.0, 16.0], classes=["primary", "secondary"])
        self.assertEqual(roads.road_tapers(f)[0], (), "another class: a junction, not the same road")

    def test_edge_lines_follow_the_taper(self):
        f = chain([10.0, 16.0], classes=["trunk", "trunk"])
        sp = roads.road_specs(f)[0]
        x = np.linspace(-150.0, 0.0, 76)
        pts = np.c_[x, np.zeros_like(x), np.zeros_like(x)]
        la, lo, _ = FRAME.enu_to_geodetic(x, np.zeros_like(x), 0.0)
        ws = roads.taper_widths(sp.width, roads.road_tapers(f)[0], lo, la)
        self.assertIsNotNone(ws)
        edges = [ln for look, _w, ln in roads.extras(sp, pts, -76.6, ws=ws) if look.get("kind") == "edge"]
        self.assertEqual(len(edges), 2)
        for ln in edges:
            off = np.abs(ln[:, 1])
            self.assertAlmostEqual(float(off[0]), 10.0 / 2 - roads.EDGE_LINE["inset"], delta=0.05)
            self.assertAlmostEqual(float(off[-1]), 13.0 / 2 - roads.EDGE_LINE["inset"], delta=0.05)
        plain = [ln for look, _w, ln in roads.extras(sp, pts, -76.6) if look.get("kind") == "edge"]
        self.assertAlmostEqual(float(np.abs(plain[0][-1, 1])), 10.0 / 2 - roads.EDGE_LINE["inset"], delta=0.05,
                               msg="(without ws: as before)")


def urban(la, lo):
    return np.ones(np.shape(la), bool)


class DeckSidewalkScenarios(unittest.TestCase):
    """A city street's bridge carries its sidewalks (the user, 2026-10-31, East Pratt Street over the Jones Falls): the
    deck wider by them, a concrete strip each side of the carriageway."""

    def test_city_street_bridge_carries_its_sidewalks(self):
        f = features([[(-200, 0), (200, 0)]], ["residential"], flags=[(0, "is_bridge", 0.4, 0.6)], ids=["r0"])
        walk = roads.deck_walks(f, 0, roads.road_specs(f, urban))
        self.assertIsNotNone(walk)
        wm, sides = walk
        self.assertEqual(set(sides), {1, -1})
        spec = roads.bridge_specs(f)[0]
        pts = np.c_[np.linspace(-40, 40, 9), np.zeros(9), np.full(9, 15.0)]
        chunks = roads._deck_chunks([("r0", spec, pts, -76.6)], (-100, -100, 100, 100), walks={"r0": walk})
        deck = [c for c in chunks if c.get("deck") and not c.get("ctx")]
        self.assertTrue(deck)
        self.assertAlmostEqual(deck[0]["w"], spec.width + 2 * wm, delta=0.01, msg="the slab wider by both sidewalks")
        strips = [c for c in chunks if c["m"] == roads.DECK_WALK["m"]]
        self.assertEqual(len(strips), 2)
        for c in strips:
            y = abs(np.asarray(c["n"])[0][1])
            self.assertAlmostEqual(y, spec.width / 2 + wm / 2, delta=0.05, msg="beside the carriageway")
            self.assertAlmostEqual(np.asarray(c["n"])[0][2], 15.0 + roads.DECK_WALK_LIFT_M, delta=1e-3)

    def test_motorway_and_rural_bridges_have_none(self):
        f = features([[(-200, 0), (200, 0)]], ["motorway"], flags=[(0, "is_bridge", 0.4, 0.6)], ids=["r0"])
        self.assertIsNone(roads.deck_walks(f, 0, roads.road_specs(f, urban)))
        f = features([[(-200, 0), (200, 0)]], ["residential"], flags=[(0, "is_bridge", 0.4, 0.6)], ids=["r0"])
        self.assertIsNone(roads.deck_walks(f, 0, roads.road_specs(f)), "rural: no sidewalks on the ground either")

    def test_a_road_all_bridge_takes_the_sidewalks_of_the_street_going_on(self):
        f = features([[(-200, 0), (0, 0)], [(0, 0), (60, 0)]], ["residential"] * 2, flags=[(1, "is_bridge", 0.0, 1.0)],
                     ids=["r0", "r1"])
        specs = roads.road_specs(f, urban)
        self.assertIsNone(specs[1], "(all bridge: no ground spec)")
        walk = roads.deck_walks(f, 1, specs)
        self.assertIsNotNone(walk)
        self.assertEqual(set(walk[1]), {1, -1})

    def test_one_side_offsets_the_deck(self):
        spec = roads.Spec(width=10.0, kind="paved", urban=False, cls="trunk", oneway=True)
        self.assertEqual(roads._deck_frame(spec, (2.0, (-1,))), (12.0, -1.0))
        self.assertEqual(roads._deck_frame(spec, None), (10.0, 0.0))


class LaneFlowScenarios(unittest.TestCase):
    """One-way pieces the map gives no lane count take theirs from what flows in or out (I-95 by the Middle Branch: a
    4-lane deck took a ramp and went on at the 3-lane default width, 11.8 m after 18.8 m)."""

    def flow(self, lines, lanes, links=()):
        f = features(lines, ["motorway"] * len(lines), ids=[f"r{k}" for k in range(len(lines))])
        f.rules["oneway"] = rules([(k, 1.0) for k in range(len(lines))], "float")
        f.rules["subclass"] = rules([(k, "link") for k in links])
        f.attrs["osm_lanes"] = np.array([np.nan if v is None else float(v) for v in lanes])
        return roads.lane_flow(f), roads.road_specs(f)

    def test_merge_adds_the_ramp(self):
        got, specs = self.flow([[(-300, 0), (0, 0)], [(-200, -150), (0, 0)], [(0, 0), (300, 0)]], [4, None, None], links=[1])
        self.assertEqual(got[2], 5, "4 lanes + the ramp's one")
        self.assertGreater(specs[2].width, specs[0].width, "wider than before the merge, not the class default")

    def test_split_and_chain(self):
        got, _ = self.flow([[(-300, 0), (0, 0)], [(0, 0), (150, 0)], [(150, 0), (300, 0)], [(150, 0), (300, -150)]],
                           [5, None, None, None], links=[3])
        self.assertEqual(got[1], 5, "a road going on keeps its lanes")
        self.assertEqual(got[2], 4, "past the exit: what arrives less the ramp")
        self.assertIsNone(got[3], "(a ramp: its own one-lane width)")

    def test_no_counts_at_all_a_ramp_still_adds_a_lane(self):
        got, specs = self.flow([[(-300, 0), (0, 0)], [(-200, -150), (0, 0)], [(0, 0), (300, 0)]], [None, None, None],
                               links=[1])
        dflt = roads.lane_layout(specs[0])[1]
        self.assertEqual(got[2], dflt + 1, "its class's default lanes + the ramp's")
        self.assertGreater(specs[2].width, specs[0].width)

    def test_map_counts_and_two_way_roads_stand(self):
        got, _ = self.flow([[(-300, 0), (0, 0)], [(0, 0), (300, 0)]], [4, 3])
        self.assertEqual(list(got), [None, None], "the map's own counts are not replaced")


class LaneShiftScenarios(unittest.TestCase):
    """At a merge the wider piece's lines start shifted toward the ramp, going on from the narrower piece's (I-95 by the
    Middle Branch: every line jumped about 1.8 m at the joint)."""

    def lane_offsets(self, f, i, x):
        sp = roads.road_specs(f)[i]
        xs = np.linspace(x, x + 40.0, 21) if x >= 0 else np.linspace(x, 0.0, 21)
        pts = np.c_[xs, np.zeros_like(xs), np.zeros_like(xs)]
        la, lo, _ = FRAME.enu_to_geodetic(xs, np.zeros_like(xs), 0.0)
        sh = roads.shift_at(roads.lane_shifts(f)[i], lo, la)
        lines = [ln for look, _w, ln in roads.extras(sp, pts, -76.6, shift=sh) if look.get("kind") in ("lane", "edge")]
        at = 0 if x >= 0 else -1                    # (the point at the joint)
        return sorted(round(float(ln[at if len(ln) else 0, 1]), 1) for ln in lines
                      if np.hypot(*(ln[at, :2] - [0.0, 0.0])) < 15.0)

    def test_merge_lines_go_on(self):
        f = features([[(-300, 0), (0, 0)], [(-200, 150), (0, 0)], [(0, 0), (300, 0)]], ["motorway"] * 3,
                     ids=["main", "ramp", "on"])
        f.rules["oneway"] = rules([(k, 1.0) for k in range(3)], "float")
        f.rules["subclass"] = rules([(1, "link")])
        f.attrs["osm_lanes"] = np.array([4.0, np.nan, np.nan])
        self.assertEqual(roads.lane_flow(f)[2], 5)
        shifts = roads.lane_shifts(f)[2]
        self.assertEqual(len(shifts), 1)
        self.assertGreater(shifts[0][2], 0.0, "toward the ramp: it comes in from the left (north)")
        before = self.lane_offsets(f, 0, -40.0)
        after = self.lane_offsets(f, 2, 0.0)
        for y in before:
            self.assertTrue(any(abs(y - y2) < 0.15 for y2 in after), f"the line at {y} m goes on: {after}")

    def test_two_ramps_merging_lie_side_by_side(self):
        # two ramps joining into a new road, none going on (I-95 by the Middle Branch: both centred on its line, its
        # corners 2.6 m past either): each across the new road, the outer edges flush with its, easing away
        f = features([[(-300, 60), (-150, 6), (0, 0)], [(-300, -60), (-150, -6), (0, 0)], [(0, 0), (300, 0)]],
                     ["motorway"] * 3, ids=["left", "right", "on"])
        f.rules["oneway"] = rules([(k, 1.0) for k in range(3)], "float")
        f.rules["subclass"] = rules([(0, "link"), (1, "link")])
        specs = roads.road_specs(f)
        wl, wr, wt = specs[0].width, specs[1].width, specs[2].width
        sh = roads.lane_shifts(f)
        self.assertEqual(sh[2], (), "the new road keeps its line")
        (lo_l, la_l, s_l, span_l), = sh[0]
        (lo_r, la_r, s_r, span_r), = sh[1]
        self.assertAlmostEqual(s_l, wt / 2 - wl / 2, delta=0.01, msg="the left ramp's left edge on the new road's")
        self.assertAlmostEqual(s_r, -(wt / 2 - wr / 2), delta=0.01, msg="the right ramp's right edge on the new road's")
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0, -40.0, -400.0]), np.zeros(3), 0.0)
        got = roads.shift_at(sh[0], lo, la)
        self.assertAlmostEqual(float(got[0]), s_l, delta=0.01)
        self.assertLess(abs(float(got[2])), 1e-9, "eased away far up the ramp")
        self.assertLessEqual(span_l, roads.SHIFT_M[1])

    def test_roads_meeting_at_an_angle_keep_their_lines(self):
        # a one-way street joining another at a right angle: a junction, not a merge
        f = features([[(-300, 0), (0, 0)], [(0, -300), (0, 0)], [(0, 0), (300, 0)]], ["primary"] * 3,
                     ids=["a", "b", "on"])
        f.rules["oneway"] = rules([(k, 1.0) for k in range(3)], "float")
        self.assertEqual(roads.lane_shifts(f), [(), (), ()])


class GoreNoseScenarios(unittest.TestCase):
    """Where a ramp's deck parts from its road's, their joined slab narrows to a point and the parapets start there
    (an I-95 exit by the Middle Branch: the slab ended square 6 m across, both walls starting in a step)."""

    def test_diverging_gap_ends_in_a_nose(self):
        import roadmesh
        g = np.r_[np.linspace(0.5, 5.9, 28), np.zeros(6)]          # the gap widening, then past MEDIAN_JOIN_M
        ext, cov = roadmesh._noses(g)
        start = roadmesh.MEDIAN_JOIN_M - roadmesh.MEDIAN_NOSE_M
        self.assertTrue(cov[(g > 0) & (g <= start)].all(), "a narrow median: joined, no parapet, as before")
        self.assertFalse(cov[(g > start)].any(), "the nose: parapets")
        self.assertTrue(np.all(np.diff(ext[(g > start)]) <= 1e-9), "the slab narrowing toward the nose's tip")
        self.assertLess(ext[27], 0.2, "to (almost) nothing where the decks part")

    def test_steady_median_stays_joined(self):
        import roadmesh
        g = np.r_[np.full(30, 4.5), np.zeros(4)]                   # a divided road's decks 4.5 m apart: no widening
        ext, cov = roadmesh._noses(g)
        self.assertTrue(cov[:30].all())
        np.testing.assert_allclose(ext[:30], 4.5 / 2 + 0.05)


class RampTieScenarios(unittest.TestCase):
    """A ramp leaving its road's deck stays at that deck's height while their slabs are joined, then eases into its own
    profile (an I-95 exit by the Middle Branch: a lip down the joined slab a car hung on)."""

    def test_ramp_held_level_then_eased(self):
        xs = np.arange(0.0, 801.0, 5.0)                    # (its abutment far off: the ease fits before it)
        main = np.c_[xs, np.zeros_like(xs)]
        ramp = np.c_[xs, -0.08 * xs]                       # parting at ~4.6 degrees from the fork at x = 0
        main_z = np.full(len(xs), 12.0)
        ramp_z = 12.0 - 0.05 * xs                          # its own solve: down at 5 % from the fork
        mspec = roads.Spec(width=15.0, kind="paved", urban=False, cls="motorway", oneway=True)
        rspec = roads.Spec(width=8.0, kind="paved", urban=False, cls="motorway", oneway=True, link=True)
        mz, rz = roads._tie_beside([main, ramp], [main_z, ramp_z], [mspec, rspec])
        np.testing.assert_allclose(mz, main_z, err_msg="the main deck as solved")
        gap = 0.08 * xs - (15.0 + 8.0) / 2
        joined = gap < roads.TIE_JOIN_M
        np.testing.assert_allclose(rz[joined], 12.0, atol=1e-6, err_msg="level with the main deck while joined")
        sep = int(np.flatnonzero(~joined)[0])
        self.assertTrue(np.all(np.diff(rz[sep:]) <= 1e-9), "then down, no step")
        g = np.abs(np.diff(rz)) / np.hypot(*np.diff(ramp, axis=0).T)
        self.assertLessEqual(float(g.max()), 0.05 + roads.TIE_EASE_GRADE + 1e-6)
        far = xs > xs[sep] + max(roads.TIE_EASE_MIN_M, (12.0 - ramp_z[sep]) / roads.TIE_EASE_GRADE) + 1
        np.testing.assert_allclose(rz[far], ramp_z[far], err_msg="its own profile past the easing")

    def test_abutment_beside_the_road_keeps_its_height(self):
        # a ramp running beside its road the whole way from the fork to its own abutment, climbing from the ground
        # there (MLK Boulevard by I-395, Baltimore: held at the road's height to its start, 4.9 m over the ground road)
        xs = np.arange(0.0, 191.0, 5.0)
        main = np.c_[xs, np.zeros_like(xs)]
        ramp = np.c_[xs, np.full(len(xs), -13.0)]          # beside it, 13 m apart: slabs joined (gap 1 m)
        main_z = np.full(len(xs), 15.7)
        ramp_z = np.interp(xs, [0.0, 110.0, 190.0], [15.7, 15.7, 11.6])     # fork at x = 0, abutment at x = 190
        mspec = roads.Spec(width=16.0, kind="paved", urban=False, cls="motorway", oneway=True)
        rspec = roads.Spec(width=8.0, kind="paved", urban=False, cls="motorway", oneway=True, link=True)
        _, rz = roads._tie_beside([main, ramp], [main_z, ramp_z], [mspec, rspec])
        self.assertAlmostEqual(float(rz[-1]), 11.6, places=6, msg="its abutment at its own (the ground's) height")
        g = np.abs(np.diff(rz)) / 5.0
        self.assertLessEqual(float(g.max()), 0.0513 + roads.TIE_EASE_GRADE * 2, "no step on the way down")

    def test_joint_beside_the_road_keeps_its_height(self):
        # the ramp's far end a joint with the next part (which goes on at its own height): never pulled to the road's
        # (by I-395 in Baltimore: held to its joint, a 6.9 m step to the part going on from it)
        xs = np.arange(0.0, 191.0, 5.0)
        main = np.c_[xs, np.zeros_like(xs)]
        ramp = np.c_[xs, np.full(len(xs), -13.0)]
        nxt = np.c_[np.arange(190.0, 300.0, 5.0), np.full(22, -13.0)]
        main_z = np.full(len(xs), 15.7)
        ramp_z = np.interp(xs, [0.0, 110.0, 190.0], [15.7, 15.7, 11.6])
        mspec = roads.Spec(width=16.0, kind="paved", urban=False, cls="motorway", oneway=True)
        rspec = roads.Spec(width=8.0, kind="paved", urban=False, cls="motorway", oneway=True, link=True)
        _, rz, _ = roads._tie_beside([main, ramp, nxt], [main_z, ramp_z, np.full(22, 11.6)], [mspec, rspec, rspec])
        self.assertAlmostEqual(float(rz[-1]), 11.6, places=6, msg="its joint at its own height")

    def test_measured_ramp_kept_a_level_apart(self):
        # a ramp whose heights the lidar measured: held at its road's only within TIE_MEASURED_M
        xs = np.arange(0.0, 301.0, 5.0)
        main = np.c_[xs, np.zeros_like(xs)]
        ramp = np.c_[xs, -0.08 * xs]
        main_z = np.full(len(xs), 12.0)
        ramp_z = np.full(len(xs), 10.0)                      # measured 2 m under it the whole way
        mspec = roads.Spec(width=15.0, kind="paved", urban=False, cls="motorway", oneway=True)
        rspec = roads.Spec(width=8.0, kind="paved", urban=False, cls="motorway", oneway=True, link=True)
        _, rz = roads._tie_beside([main, ramp], [main_z, ramp_z], [mspec, rspec], [False, True])
        np.testing.assert_allclose(rz, ramp_z, err_msg="the lidar's heights stand")
        _, rz = roads._tie_beside([main, ramp], [main_z, ramp_z + 1.6], [mspec, rspec], [False, True])
        self.assertAlmostEqual(float(rz[0]), 12.0, places=6, msg="a small gap at the fork still closed")


class EndJoinScenarios(unittest.TestCase):
    """Ends within 0.5 m of a point already in a node join it (four ramps' ends at a Springfield junction, each within
    0.5 m of a neighbour, made two nodes: one ramp an abutment 9 m under the others)."""

    def test_chain_of_near_ends_is_one_node(self):
        nk = roads._EndJoin()
        pts = [(-0.36, 0.181), (-0.035, -0.022), (-0.035, -0.022), (-0.188, -0.297)]
        self.assertEqual(len({nk(np.array(p)) for p in pts}), 1)

    def test_nodes_numbered_as_made(self):
        nk = roads._EndJoin()
        self.assertEqual([nk(np.array(p)) for p in [(0, 0), (5, 0), (0.2, 0), (10, 0)]], [0, 1, 0, 2])


class ForkPadScenarios(unittest.TestCase):
    """A ramp's fork from its motorway gets a pad only to the decks' edges, its railing where theirs stop (the full pad
    was a box wider than both decks, its railings standing out: I-95 by the Middle Branch); a street junction on a
    bridge keeps its wide pad (test_deckjoins)."""

    def test_fork_pad_to_the_decks_edges(self):
        main_in = np.c_[np.linspace(-120.0, 0.0, 61), np.zeros(61)]
        main_out = np.c_[np.linspace(0.0, 120.0, 61), np.zeros(61)]
        ramp = np.c_[np.linspace(0.0, 120.0, 61), -0.12 * np.linspace(0.0, 120.0, 61)]
        z = np.full(61, 20.0)
        wide = roads.Spec(width=18.8, kind="paved", urban=False, cls="motorway", oneway=True)
        link = roads.Spec(width=8.0, kind="paved", urban=False, cls="motorway", oneway=True, link=True)
        decks = [("a", wide, main_in, z), ("b", wide, main_out, z), ("r", link, ramp, z)]
        pads = roads.deck_pads(decks, [], (-50, -50, 50, 50), lambda xy: np.zeros(len(xy)))
        self.assertEqual(len(pads), 1, "the joint paved (its railing where the parts' own stop)")
        ring = np.asarray(pads[0][0], np.float64)
        # no wider than the decks: within the carriageways' half widths + the fork margin of some part's line
        lines = [(main_in, 9.4), (main_out, 9.4), (ramp, 4.0)]
        for q in ring[:, :2]:
            d = min(float(np.min(np.hypot(*(ln - q).T))) - h for ln, h in lines)
            self.assertLess(d, roads.PAD_FORK_MARGIN_M + 0.6, f"pad point {q} past the decks' edges")
        self.assertTrue(pads[0][2], "a railing round its rim over the drop")


class MedianScenarios(unittest.TestCase):
    """Opposite traffic either side of a median is never one open slab: a median barrier when close, an open gap with
    each deck's own parapet when wider (I-95 by the Middle Branch); a ramp beside its road still joins."""

    def test_close_opposite_decks_get_a_median_barrier(self):
        import roadmesh
        g = np.full(20, 1.5)
        opp = np.ones(20, bool)
        ext, cov = roadmesh._opposite(g, opp, *roadmesh._noses(g))
        self.assertFalse(cov.any(), "a parapet (at the middle)")
        np.testing.assert_allclose(ext, 1.5 / 2 + 0.05, err_msg="the slab to the gap's middle")

    def test_wide_opposite_decks_stay_apart(self):
        import roadmesh
        g = np.full(20, 4.5)
        ext, cov = roadmesh._opposite(g, np.ones(20, bool), *roadmesh._noses(g))
        self.assertFalse(cov.any())
        np.testing.assert_allclose(ext, 0.0, err_msg="no fill: each deck at its own edge, the gap open")

    def test_same_direction_still_joins(self):
        import roadmesh
        g = np.full(20, 4.5)
        ext, cov = roadmesh._opposite(g, np.zeros(20, bool), *roadmesh._noses(g))
        self.assertTrue(cov.all())

    def test_travel_vectors_oppose(self):
        import roadmesh
        x = np.linspace(0, 50, 11)
        left, right = np.c_[x, np.full(11, 5.0)], np.c_[x, np.zeros(11)]
        a, b = roadmesh._travel_vec(left, right, 1), roadmesh._travel_vec(left, right, -1)
        np.testing.assert_allclose(a[0], [1.0, 0.0], atol=1e-9)
        self.assertTrue(((a * b).sum(1) < -0.5).all())


class DeckTaperScenarios(unittest.TestCase):
    """Two decks end to end at other widths both run to half way (a 24.0 m deck going on as a 20.4 m one stepped
    1.8 m); the deck's ribbon follows its per-point widths ("ws")."""

    def test_decks_meet_half_way(self):
        f = chain([24.0, 20.0], classes=["motorway", "motorway"], bridged=[0, 1])
        f.rules["oneway"] = rules([(0, 1.0), (1, 1.0)], "float")
        tp = roads.deck_tapers(f)
        self.assertTrue(tp[0] and tp[1], "both decks taper")
        bs = roads.bridge_specs(f)
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0]), np.array([0.0]), 0.0)
        for i in (0, 1):
            w = roads.taper_widths(bs[i].width, tp[i], lo, la)
            self.assertAlmostEqual(float(w[0]), (bs[0].width + bs[1].width) / 2, delta=0.05)

    def test_ribbon_follows_its_widths(self):
        import roadmesh
        pts = np.c_[np.linspace(0, 40, 11), np.zeros(11), np.full(11, 10.0)]
        c = {"m": "x", "w": 20.0, "tl": 10.0, "p": 20, "deck": True, "fixed": True, "n": pts.tolist(),
             "ws": list(np.linspace(20.0, 24.0, 11))}
        (left, right, v, m, layer, z), = roadmesh.ribbons([c])
        w = np.hypot(*(left - right).T)
        self.assertAlmostEqual(float(w[0]), 20.0, delta=0.05)
        self.assertAlmostEqual(float(w[-1]), 24.0, delta=0.05)
        b = {"m": "x", "w": 3.0, "tl": 10.0, "p": 20, "n": pts.tolist()}       # (and a plain chunk beside it)
        self.assertEqual(len(roadmesh.ribbons([c, b])), 2)

if __name__ == "__main__":
    unittest.main()
