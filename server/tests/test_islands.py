"""islands: traffic islands inside small road loops (roundabouts, turnaround loops)."""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import islands  # noqa: E402
import roads  # noqa: E402


def arc(r, a0, a1, n=12, cx=0.0, cy=0.0):
    t = np.linspace(a0, a1, n)
    return np.stack([cx + r * np.cos(t), cy + r * np.sin(t), np.zeros(n)], 1)


class IslandTest(unittest.TestCase):
    def test_splitter_marker_faces_each_incoming_lane(self):
        ow = roads.Spec(3., "paved", True, "tertiary", True, 2.5, 1)
        road = [
            ("out", ow, np.array([[0., 0., 0.], [-3.2, 5., 0.], [-3.2, 27., 0.], [0., 32., 0.]])[::-1], 0.),
            ("in", ow, np.array([[0., 0., 0.], [3.2, 5., 0.], [3.2, 27., 0.], [0., 32., 0.]]), 0.),
        ]
        found = islands.find(road)
        self.assertEqual(len(found), 1)
        placed = islands.splitter_heads(found, [], road, (-8., -5., 8., 35.))
        self.assertEqual(len(placed), 2)
        self.assertGreater(min(d[1] for p, d in placed if p[1] < 16.), .9)
        self.assertLess(max(d[1] for p, d in placed if p[1] > 16.), -.9)
        self.assertEqual(islands.splitter_heads(found, [], road, (-8., 13., 8., 19.)), [])
        service = [(fid, spec._replace(cls="service"), line, lon) for fid, spec, line, lon in road]
        self.assertEqual(islands.splitter_heads(found, [], service, (-8., -5., 8., 35.)), [])

    def test_french_road_tile_serializes_splitter_markers(self):
        import json
        import os
        import tempfile
        from unittest.mock import patch
        from test_roads import FRAME, features, flat, rules

        west = [(60., 50.), (56.8, 55.), (56.8, 77.), (60., 82.)][::-1]
        east = [(60., 50.), (63.2, 55.), (63.2, 77.), (60., 82.)]
        feats = features([west, east], ["tertiary"] * 2, widths=[(0, 3.), (1, 3.)])
        feats.rules["oneway"] = rules([(0, 1.), (1, 1.)], "float")
        with patch.dict(os.environ, {"BNE_EU_ROAD_DETAILS": "on"}), patch("europewalks.country_for", return_value="FR"):
            walks, signs = [], []
            chunks = roads.build_tile(FRAME, 0, 0, [feats], flat, walks=walks, props=[], signs=signs)
            self.assertEqual(len(signs), 1)
            self.assertEqual(len(signs[0][1]), 2)
            with tempfile.TemporaryDirectory() as tmp:
                path, _ = roads.write_tile(tmp, "/probe", 1, 0, 0, chunks, None, signs=signs)
                data = json.loads((Path(tmp) / Path(path).name).read_text())
                self.assertEqual({row["kind"] for row in data["regionalSigns"]}, {"island_keep_right"})
                self.assertGreater((Path(tmp) / Path(data["signs"]["shape"]).name).stat().st_size, 0)

    def test_roundabout_ring(self):
        ow = roads.Spec(6.0, "paved", True, "residential", True)
        two = roads.Spec(9.0, "paved", True, "residential", False)
        rs = [("r1", ow, arc(16, 0, math.pi), 0.0), ("r2", ow, arc(16, math.pi, 2 * math.pi), 0.0),
              ("app", two, np.array([(16.0, 0.0, 0.0), (80.0, 0.0, 0.0)]), 0.0)]
        f = islands.find(rs)
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["inner"], {"r1": 1, "r2": 1}, "counter-clockwise ring: the inside is its left")
        ring = f[0]["ring"]
        r = np.hypot(ring[:, 0], ring[:, 1])
        self.assertTrue(np.all(np.abs(r - 13.0) < 0.8), "the curb line is the ring inset by its half width")
        self.assertTrue(islands.in_island(f, (0.0, 0.0)) and not islands.in_island(f, (40.0, 0.0)))

    def test_a_road_through_the_loop_is_no_island(self):
        ow = roads.Spec(6.0, "paved", True, "residential", True)
        rs = [("r1", ow, arc(16, 0, math.pi), 0.0), ("r2", ow, arc(16, math.pi, 2 * math.pi), 0.0)]
        self.assertEqual(len(islands.find(rs)), 1)
        # a turn lane across it, joined to neither half at the loop's corners (Timisoara 2026-10-15)
        lane = ("lane", ow, np.array([(-30.0, 3.0, 0.0), (30.0, 3.0, 0.0)]), 0.0)
        self.assertEqual(islands.find(rs + [lane]), [])

    def test_turnaround_loop_with_a_two_way_link(self):
        ow = roads.Spec(5.8, "paved", True, "residential", True)
        two = roads.Spec(10.6, "paved", True, "residential", False)
        loop = arc(12, math.pi, 2 * math.pi, cy=0.0)                 # south half, west to east, one-way
        link = np.array([(12.0, 0.0, 0.0), (-12.0, 0.0, 0.0)])       # north side, two-way
        f = islands.find([("loop", ow, loop, 0.0), ("link", two, link, 0.0)])
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["inner"]["loop"], 1, "the loop runs anticlockwise: its left faces the island")
        self.assertEqual(f[0]["inner"]["link"], 1, "the link runs east to west: its left (south) faces it")

    def test_loop_round_a_building_is_a_block(self):
        ow = roads.Spec(6.0, "paved", True, "residential", True)
        rs = [("r1", ow, arc(16, 0, math.pi), 0.0), ("r2", ow, arc(16, math.pi, 2 * math.pi), 0.0)]
        self.assertEqual(islands.find(rs, structures=[(2.0, 1.0)]), [], "a building inside: not an island")

    def test_city_block_keeps_its_sidewalks(self):
        two = roads.Spec(9.0, "paved", True, "residential", False)
        sq = [((0, 0), (50, 0)), ((50, 0), (50, 50)), ((50, 50), (0, 50)), ((0, 50), (0, 0))]
        rs = [(f"s{i}", two, np.array([(a[0], a[1], 0.0), (b[0], b[1], 0.0)]), 0.0) for i, (a, b) in enumerate(sq)]
        self.assertEqual(islands.find(rs), [], "two-way streets round a block: not an island")

    def test_large_roundabout_gets_an_island(self):
        # diameter ~44 m: above the old 1200 m2 cap, so inner sidewalks used to stay as four arcs at the entries
        # (Chavagnes-en-Paillers 2026-10-28)
        ow = roads.Spec(5.5, "paved", True, "tertiary", True, 2.5, 1)
        two = roads.Spec(9.0, "paved", True, "tertiary", False)
        R = 22.0
        rs = [(f"r{k}", ow, arc(R, k * math.pi / 2, (k + 1) * math.pi / 2, n=12), 0.0) for k in range(4)]
        for i, ang in enumerate((0, math.pi / 2, math.pi, 3 * math.pi / 2)):
            c, s = math.cos(ang), math.sin(ang)
            rs.append((f"a{i}", two, np.array([(R * c, R * s, 0.0), ((R + 60) * c, (R + 60) * s, 0.0)]), 0.0))
        f = islands.find(rs)
        self.assertEqual(len(f), 1)
        self.assertIsNotNone(f[0]["ring"])
        self.assertEqual(set(f[0]["inner"]), {f"r{k}" for k in range(4)})
        r = np.hypot(f[0]["ring"][:, 0], f[0]["ring"][:, 1])
        self.assertTrue(np.all(np.abs(r - (R - 2.75)) < 1.0), "curb inset by the ring's half width")

    def test_untagged_round_ring_gets_an_island(self):
        # Overture often leaves oneway / junction=roundabout off the ring (Chavagnes: rnd 0.99, ow 0): still an island,
        # and entry teardrops that reuse ring edges are dropped
        two = roads.Spec(8.0, "paved", True, "primary", False)
        ow = roads.Spec(6.3, "paved", True, "tertiary", True, 2.5, 1)
        R = 16.0
        rs = [(f"r{k}", two, arc(R, k * math.pi / 2, (k + 1) * math.pi / 2, n=12), 0.0) for k in range(4)]
        for i, ang in enumerate((0.25, math.pi / 2 + 0.25, math.pi + 0.25, 3 * math.pi / 2 + 0.25)):
            # a short one-way chord across each entry that with two ring arcs makes a teardrop (~oneway loop)
            c0, s0 = math.cos(ang - 0.3), math.sin(ang - 0.3)
            c1, s1 = math.cos(ang + 0.3), math.sin(ang + 0.3)
            rs.append((f"a{i}", ow, np.array([(R * c0, R * s0, 0.0), ((R + 25) * math.cos(ang), (R + 25) * math.sin(ang), 0.0),
                                              (R * c1, R * s1, 0.0)]), 0.0))
        f = islands.find(rs)
        self.assertEqual(len(f), 1, "the roundabout only; not the entry teardrops")
        self.assertTrue(f[0]["round"])
        self.assertEqual(set(f[0]["inner"]), {f"r{k}" for k in range(4)})
        self.assertIsNotNone(f[0]["ring"])

    def test_ring_approaches_are_the_roads_that_meet_it(self):
        ow = roads.Spec(5.5, "paved", True, "tertiary", True, 2.5, 1)
        two = roads.Spec(9.0, "paved", True, "tertiary", False)
        R = 16.0
        rs = [(f"r{k}", ow, arc(R, k * math.pi / 2, (k + 1) * math.pi / 2, n=12), 0.0) for k in range(4)]
        for i, ang in enumerate((0, math.pi / 2, math.pi, 3 * math.pi / 2)):
            c, s = math.cos(ang), math.sin(ang)
            rs.append((f"a{i}", two, np.array([(R * c, R * s, 0.0), ((R + 40) * c, (R + 40) * s, 0.0)]), 0.0))
        ring = islands.ring_fids(rs)
        self.assertEqual(ring, {f"r{k}" for k in range(4)})
        self.assertEqual(islands.ring_approaches(rs, ring), {f"a{i}" for i in range(4)})

    def test_large_oneway_block_is_no_island(self):
        # a 60 x 60 m one-way block (~3600 m2): under the raised area cap, still rejected by roundness
        ow = roads.Spec(6.0, "paved", True, "residential", True, 2.5, 1)
        sq = [((0, 0), (60, 0)), ((60, 0), (60, 60)), ((60, 60), (0, 60)), ((0, 60), (0, 0))]
        rs = [(f"s{k}", ow, np.array([(*a, 0.0), (*b, 0.0)]), 0.0) for k, (a, b) in enumerate(sq)]
        self.assertEqual(islands.find(rs), [], "a square one-way block: not round enough for an island")

    def test_large_roundabout_tile_has_one_island_curb_not_inner_arcs(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_roads import FRAME, flat, features, rules
        R, cx, cy = 22.0, 128.0, 128.0
        t = np.linspace(0, 2 * math.pi, 48, endpoint=False)
        ring = [(cx + R * np.cos(a), cy + R * np.sin(a)) for a in t] + [(cx + R, cy)]
        arms = [[(cx + R * math.cos(a), cy + R * math.sin(a)),
                 (cx + (R + 80) * math.cos(a), cy + (R + 80) * math.sin(a))]
                for a in (0, math.pi / 2, math.pi, 3 * math.pi / 2)]
        f = features([ring] + arms, ["tertiary"] * 5)
        f.rules["oneway"] = rules([(0, 1.0)], "float")
        walks = []
        roads.build_tile(FRAME, 0, 0, [f], flat, lambda la, lo: np.ones(np.shape(la), bool), walks=walks)
        # (a roundabout's island is layered, plan 038: the paved apron outside, the planter's curb inside it)
        layers = [w for w in walks if w.get("fid") == "island"]
        self.assertEqual(sorted(("disc" in w, w.get("mat", 0) == 7) for w in layers), [(False, False), (False, True), (True, False)],
                         "an apron, the planting's curb band and its fan")
        islands_w = [w for w in layers if w.get("mat", 0) == 7]
        self.assertEqual(len(islands_w), 1, "one continuous island curb owned by the centre's tile")
        L = float(np.hypot(*np.diff(islands_w[0]["pts"][:, :2], axis=0).T).sum())
        self.assertGreater(L, 2 * math.pi * (R - 4) * 0.85, "the curb goes most of the way round")
        for w in walks:
            if "corner" in w or w.get("fid") in ("island", "ringcurb"):
                continue
            mid = w["pts"][len(w["pts"]) // 2, :2]
            self.assertGreater(float(np.hypot(mid[0] - cx, mid[1] - cy)), R - 1.0,
                               "no sidewalk facing the island")

    def test_roundabout_on_a_tile_corner_curb_shared_by_its_tiles(self):
        # a roundabout on the corner of four tiles: each tile its part of the curb (clipped to its box; a geotile's
        # skin cuts them at its quad), together all the way round with no gap, and the same vertices either side of a
        # seam (the whole ring simplified before it is clipped). (PR #468 gave the whole curb to the tile with the
        # island's centre: with the geotiles' skins clipping at the quad the other three quarters were lost.)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_roads import FRAME, flat, features, rules
        R, cx, cy = 22.0, 3.0, -2.0
        t = np.linspace(0, 2 * math.pi, 48, endpoint=False)
        ring = [(cx + R * np.cos(a), cy + R * np.sin(a)) for a in t] + [(cx + R, cy)]
        arms = [[(cx + R * math.cos(a), cy + R * math.sin(a)),
                 (cx + (R + 80) * math.cos(a), cy + (R + 80) * math.sin(a))]
                for a in (0.3, math.pi / 2 + 0.3, math.pi + 0.3, 3 * math.pi / 2 + 0.3)]
        f = features([ring] + arms, ["tertiary"] * 5)
        f.rules["oneway"] = rules([(0, 1.0)], "float")
        total, pts = 0.0, []
        for tx, ty in ((0, 0), (-1, 0), (0, -1), (-1, -1)):
            walks = []
            roads.build_tile(FRAME, tx, ty, [f], flat, lambda la, lo: np.ones(np.shape(la), bool), walks=walks)
            for w in walks:
                if w.get("fid") == "island" and w.get("mat") == 7:
                    total += float(np.hypot(*np.diff(w["pts"][:, :2], axis=0).T).sum())
                    pts.append(w["pts"][:, :2])
        self.assertGreaterEqual(len(pts), 4, "a piece in each tile")
        r = np.hypot(*(np.concatenate(pts) - (cx, cy)).T)
        self.assertLess(float(r.max() - r.min()), 0.3, "one curb ring")
        L1 = 2 * math.pi * float(r.mean())
        self.assertAlmostEqual(total, L1, delta=0.02 * L1, msg="all the way round")
        ends = np.array([p[k] for p in pts for k in (0, -1)])
        for e in ends:              # every cut end meets another piece's
            d = np.hypot(*(ends - e).T)
            self.assertLess(np.sort(d)[1], 1e-6)


class RingTest(unittest.TestCase):
    def test_rings(self):
        two = roads.Spec(7.0, "paved", True, "tertiary", False)
        ow = roads.Spec(7.0, "paved", True, "primary", True, 2.5, 1)
        circle = [(f"c{k}", two, arc(14, k * math.pi / 3, (k + 1) * math.pi / 3, 4), 0.0) for k in range(6)]
        self.assertEqual(islands.ring_fids(circle), {f"c{k}" for k in range(6)}, "a round loop: a ring, oneway or not")
        sq = [((0, 0), (60, 0)), ((60, 0), (60, 60)), ((60, 60), (0, 60)), ((0, 60), (0, 0))]
        block = [(f"s{k}", ow, np.array([(*a, 0.0), (*b, 0.0)]), 0.0) for k, (a, b) in enumerate(sq)]
        self.assertEqual(islands.ring_fids(block), set(), "a block of one-way streets driven round: no ring")
        back = roads.Spec(7.0, "paved", True, "primary", True, 2.5, -1)
        pair = [("n", ow, np.array([(0.0, 0.0, 0.0), (0.0, 200.0, 0.0)]), 0.0),
                ("s", back, np.array([(12.0, 0.0, 0.0), (12.0, 200.0, 0.0)]), 0.0),
                ("j0", two, np.array([(0.0, 0.0, 0.0), (12.0, 0.0, 0.0)]), 0.0),
                ("j1", two, np.array([(0.0, 200.0, 0.0), (12.0, 200.0, 0.0)]), 0.0)]
        self.assertEqual(islands.ring_fids(pair), set(), "a divided road's carriageways: no ring")



class NearEdgesTest(unittest.TestCase):
    """_near_edges: the edges whose box meets the loop's, in index order (those far off cannot cross it)."""

    def test_box_filter(self):
        edges = [(f"e{k}", None, np.array(p, float), 0, 1, 1.0) for k, p in enumerate((
            [[1, 1, 0], [9, 9, 0]],                 # inside
            [[-5, 5, 0], [15, 5, 0]],               # through
            [[100, 100, 0], [110, 100, 0]],         # far
            [[10, -3, 0], [10, 0, 0]]))]            # touching the box's corner
        poly = np.array([[0, 0], [10, 0], [10, 10], [0, 10]], float)
        self.assertEqual(islands._near_edges(islands._edge_boxes(edges), poly), [0, 1, 3])
        self.assertEqual(list(islands._near_edges(islands._edge_boxes([]), poly)), [])


if __name__ == "__main__":
    unittest.main()
