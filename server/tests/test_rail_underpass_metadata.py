"""Source road tunnels still reserve railway clearance after gradesep makes them ground."""
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import railways
import roads
from test_roads import FRAME, features


class UnderpassMetadataTest(unittest.TestCase):
    def setUp(self):
        railways._GROUND_MEMO.clear()

    def tearDown(self):
        railways._GROUND_MEMO.clear()

    def cell(self, road=((120, 90), (120, 110)), tunnel=True):
        return features([[(-200, 100), (450, 100)], list(road)],
                        ["standard_gauge", "secondary"], subtypes=["rail", "road"],
                        flags=([(1, "is_tunnel")] if tunnel else []), widths=[(1, 8.6)])

    def effective(self, f, rail_bridge=False, road_ranges=()):
        # The effective result saved by gradesep/cellfacts, without mutating original map flags.
        ranges = {0: [(0.0, 1.0, "bridge")] if rail_bridge else [], 1: list(road_ranges)}
        # (roads' batched readers too: bridge_specs / tunnel_specs read the layer's ranges at once, earthfast.RoadRules)
        return mock.patch.multiple(roads,
                                   raw_level_ranges=lambda feats, i: list(ranges[i]),
                                   level_ranges=lambda feats, i: list(ranges[i]),
                                   _raw_all=lambda feats: [list(ranges[i]) for i in range(len(feats))],
                                   _level_lists=lambda feats, idx: {i: list(ranges[i]) for i in idx})

    def test_converted_tunnel_keeps_promoted_deck_clear(self):
        f = self.cell()
        with self.effective(f, rail_bridge=True):
            self.assertIsNone(roads.tunnel_specs(f)[1])
            tunnels = railways.road_underpasses(FRAME, [f, f], roads.tile_bbox(0, 0))
            self.assertEqual(len(tunnels), 1)
            self.assertAlmostEqual(tunnels[0][1], 5.3, places=5)
            parts = railways.parts(FRAME, [f], roads.tile_bbox(0, 0))
            prof = railways.profile(parts, lambda xy: np.full(len(xy), 10.0), tunnels)
            for part, (bed, _) in zip(parts, prof):
                distance, _ = railways._underpass_distance(part[3], tunnels[0][0])
                over = distance < tunnels[0][1]
                self.assertTrue(over.any())
                self.assertGreaterEqual(float((bed[over] + railways.BALLAST_H - railways.DECK_D).min()),
                                        10.0 + railways.UNDERPASS_CLEAR_M - 1e-6)
            self.assertEqual(roads.raw_level_ranges(f, 1), [])
            self.assertEqual(roads.tunnel_parts(f, 1), [])

    def test_inferred_span_keeps_source_crossing_checks(self):
        cases = [(self.cell(), True), (self.cell(tunnel=False), False),
                 (self.cell(road=((110, 90), (130, 90))), False),
                 (self.cell(road=((120, 80), (120, 94))), False),
                 (self.cell(road=((120, 0), (120, 200))), False)]
        for f, expected in cases:
            with self.subTest(expected=expected, coords=f.lonlat.tolist()), self.effective(f):
                parts = railways.parts(FRAME, [f], roads.tile_bbox(0, 0))
                self.assertEqual(any(p[2] == "bridge" for p in parts), expected)

    def test_retained_tunnel_uses_existing_portal_geometry_once(self):
        f = self.cell()
        with self.effective(f, road_ranges=[(0.0, 1.0, "tunnel")]):
            original = roads.tunnel_parts(f, 1)
            with mock.patch.object(roads, "_parts_by_level_new", wraps=roads._parts_by_level_new) as source:
                got = railways.road_underpasses(FRAME, [f, f], roads.tile_bbox(0, 0))
            self.assertEqual(len(got), 1)
            self.assertFalse(any(call.kwargs.get("rngs") for call in source.call_args_list))
            e, n = railways.mapgeom.enu_xy(f, FRAME, original[0])
            np.testing.assert_allclose(got[0][0], np.c_[e, n])

    def test_one_removed_tunnel_with_another_retained_on_same_feature(self):
        f = features([[(-200, 100), (450, 100)], [(120, 60), (120, 160)]],
                     ["standard_gauge", "secondary"], subtypes=["rail", "road"],
                     flags=[(1, "is_tunnel", 0.3, 0.5), (1, "is_tunnel", 0.7, 0.9)])
        with self.effective(f, road_ranges=[(0.7, 0.9, "tunnel")]):
            self.assertIsNotNone(roads.tunnel_specs(f)[1])
            tunnels = railways.road_underpasses(FRAME, [f], roads.tile_bbox(0, 0))
            self.assertEqual(len(tunnels), 2)
            parts = railways.parts(FRAME, [f], roads.tile_bbox(0, 0))
            self.assertTrue(any(p[2] == "bridge" for p in parts))


class UnderpassMaskTest(unittest.TestCase):
    """_underpass_masks (earthfast.rail_underpass_hits: only a part's points inside the tunnel's padded box measured; a
    central London cell's rail beds measured every point of km-long lines against every tunnel in every node window:
    41-45 CPU-s a cell, 2026-11-13) masks no point the whole-line measure leaves out, and a crossing on one track does
    not mask the double track beside it unless their runs join."""

    @staticmethod
    def whole(rails, line, half, margin):
        """The covered candidates as every point of every part measured gives them, for the comparison."""
        pad = half + margin
        direction = line[-1] - line[0]
        out = []
        for *_, xy in rails:
            t = np.gradient(xy, axis=0)
            cross = np.abs(t[:, 0] * direction[1] - t[:, 1] * direction[0])
            cross /= np.maximum(np.sqrt((t * t).sum(1)) * np.sqrt((direction * direction).sum()), 1e-9)
            d, _ = railways._underpass_distance(xy, line)
            out.append((d <= pad) & (cross > railways.UNDERPASS_MIN_SIN))
        return out

    def test_masks_within_whole_measure(self):
        rng = np.random.default_rng(7)
        n = 4000                                    # (a 12 km line every STEP_M)
        s = np.arange(n) * railways.STEP_M
        long_line = np.c_[s - 6000.0, 30.0 * np.sin(s / 900.0)]
        rails = [("a", "rail", "ground", long_line),
                 ("b", "rail", "ground", long_line + [0.0, 4.5]),           # (its double track)
                 ("c", "rail", "ground", np.c_[np.full(300, 40.0), np.arange(300) * 3.0 - 450.0])]
        hits = 0
        for _ in range(20):
            c = rails[int(rng.integers(3))][3][int(rng.integers(300))]
            d = rng.normal(size=2)
            d /= np.hypot(*d)
            line = np.array([c - 25.0 * d, c + 25.0 * d])
            half, margin = float(rng.uniform(3, 10)), float(rng.uniform(2, 12))
            want = self.whole(rails, line, half, margin)
            got = railways._underpass_masks(rails, line, half, margin)
            for g, w in zip(got, want):
                self.assertFalse((g & ~w).any(), "nothing the whole measure left out")
            hits += sum(bool(g.any()) for g in got)
        self.assertGreater(hits, 10, "tunnels across the tracks found")

    def test_crossing_track_only(self):
        """A tunnel under one track whose end stops short of the track beside it: only the crossed track's points."""
        xs = np.arange(0.0, 200.0, railways.STEP_M)
        rails = [("a", "rail", "ground", np.c_[xs, np.zeros(len(xs))]),
                 ("b", "rail", "ground", np.c_[xs, np.full(len(xs), 4.5)])]
        line = np.array([[100.0, -20.0], [100.0, 2.0]])
        a, b = railways._underpass_masks(rails, line, 3.0, 2.0)
        self.assertTrue(a.any())
        self.assertFalse(b.any())
        got = railways._underpass_hits(rails, [(line, 3.0)], 2.0)[0]
        self.assertEqual(list(got), [0])
        np.testing.assert_array_equal(got[0][0], np.flatnonzero(a))
        _d, t = railways._underpass_distance(rails[0][3][a], line)
        np.testing.assert_array_equal(got[0][1], t)
        at = int(np.flatnonzero(rails[0][3][:, 0] == 100.0)[0])            # (on the line, 20 m along its 22)
        self.assertAlmostEqual(float(got[0][1][list(got[0][0]).index(at)]), 20.0 / 22.0, places=12)
        d, t = railways._underpass_distance(np.array([[100.0, 5.0], [103.0, -20.0]]), line)
        np.testing.assert_allclose(d, [3.0, 3.0])
        np.testing.assert_allclose(t, [1.0, 0.0])

    def test_span_clip(self):
        """The track over a tunnel made a bridge piece, the ground pieces cut to the window, a stub left out."""
        xs = np.arange(0.0, 400.0, railways.STEP_M)
        rails = [("a", "rail", "ground", np.c_[xs, np.zeros(len(xs))]),
                 ("b", "rail", "bridge", np.c_[xs, np.full(len(xs), 50.0)])]
        line = np.array([[100.0, -20.0], [100.0, 20.0]])
        got = railways._span_clip(rails, [(line, 3.0)], (0.0, -10.0, 300.0, 60.0))
        self.assertEqual([k for _f, _c, k, _xy in got], ["ground", "bridge", "ground", "bridge"])
        g0, br, g1, deck = (p[3] for p in got)
        self.assertEqual(g0[-1].tolist(), br[0].tolist())                   # (shared end points)
        self.assertEqual(br[-1].tolist(), g1[0].tolist())
        self.assertLess(br[0, 0], 100.0 - 3.0)
        self.assertGreater(br[-1, 0], 100.0 + 3.0)
        self.assertEqual(g1[-1, 0], 302.0)                                  # (one point past the window)
        self.assertIs(deck, rails[1][3])                                    # (a deck stays whole)
        self.assertIs(railways._span_clip(rails, []), rails)
        gen = railways._span_clip(rails, (t for t in [(line, 3.0)]), (0.0, -10.0, 300.0, 60.0))
        self.assertEqual([(k, p.tolist()) for _f, _c, k, p in gen], [(k, p.tolist()) for _f, _c, k, p in got])
        whole = railways._span_clip(rails, [], (-1e9, -1e9, 1e9, 1e9))
        self.assertTrue(all(a[3] is b[3] for a, b in zip(whole, rails)))
        self.assertEqual(railways._span_clip([("s", "rail", "ground", np.array([[0.0, 0.0], [1e4, 0.0]]))], [],
                                             (100.0, -1.0, 200.0, 1.0)), [])


if __name__ == "__main__":
    unittest.main()
