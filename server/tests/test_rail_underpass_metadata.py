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


class UnderpassMaskCostTest(unittest.TestCase):
    """_underpass_masks measures only a part's points inside the tunnel's padded box (a central London cell's rail beds
    measured every point of km-long lines against every tunnel in every node window: 41-45 CPU-s a cell, 2026-11-13),
    with the masks the whole-line measure gave."""

    @staticmethod
    def whole(rails, line, half, margin):
        """The masks' covered candidates as every point was measured (before), for the comparison."""
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

    def test_only_box_points_measured_same_masks(self):
        rng = np.random.default_rng(7)
        n = 4000                                    # (a 12 km line every STEP_M)
        s = np.arange(n) * railways.STEP_M
        long_line = np.c_[s - 6000.0, 30.0 * np.sin(s / 900.0)]
        rails = [("a", "rail", "ground", long_line),
                 ("b", "rail", "ground", long_line + [0.0, 4.5]),           # (its double track)
                 ("c", "rail", "ground", np.c_[np.full(300, 40.0), np.arange(300) * 3.0 - 450.0])]
        seen = []
        real = railways._underpass_distance

        def counting(xy, line):
            seen.append(len(xy))
            return real(xy, line)
        hits = 0
        for _ in range(20):
            c = rails[int(rng.integers(3))][3][int(rng.integers(300))]
            d = rng.normal(size=2)
            d /= np.hypot(*d)
            line = np.array([c - 25.0 * d, c + 25.0 * d])
            half, margin = float(rng.uniform(3, 10)), float(rng.uniform(2, 12))
            want = self.whole(rails, line, half, margin)
            seen.clear()
            with mock.patch.object(railways, "_underpass_distance", counting):
                got = railways._underpass_masks(rails, line, half, margin)
            self.assertLess(max(seen, default=0), 200, "only the points near the tunnel measured")
            for g, w in zip(got, want):
                self.assertFalse((g & ~w).any(), "nothing the whole measure left out")
            hits += sum(bool(g.any()) for g in got)
        self.assertGreater(hits, 10, "tunnels across the tracks found")


if __name__ == "__main__":
    unittest.main()
