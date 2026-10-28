"""Railway tracks (railways.py): the rail lines taken, the level crossings, the bed's heights and the mesh."""
import math
import os
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import railways  # noqa: E402
import roads  # noqa: E402
from test_roads import FRAME, features  # noqa: E402


def flat(xy):
    return np.full(len(xy), 10.0)


class RailwayTest(unittest.TestCase):
    def test_distant_bridge_of_a_nearby_feature_is_not_profiled(self):
        # One feature crosses the tile, but its last 100 m is a bridge 5 km away.
        f = features([[(-100.0, 100.0), (5000.0, 100.0), (5100.0, 100.0)]],
                     ["standard_gauge"], subtypes=["rail"],
                     flags=[(0, "is_bridge", 5100.0 / 5200.0, 1.0)])
        calls = []

        def ground(xy):
            calls.append(len(xy))
            self.assertLess(float(xy[:, 0].max()), 1000.0,
                            "a distant bridge must not trigger elevation work")
            return flat(xy)

        self.assertIsNotNone(railways.tile_mesh(FRAME, 0, 0, [f], ground))
        self.assertTrue(calls, "the nearby ground track is still built")

    def test_bridge_bbox_overlap_without_track_nearby_needs_no_work(self):
        # The bbox covers the tile, but all three sides of this U are far away.
        f = features([[(-1000.0, -1000.0), (-1000.0, 1000.0),
                       (1000.0, 1000.0), (1000.0, -1000.0)]],
                     ["standard_gauge"], subtypes=["rail"], flags=[(0, "is_bridge")])
        ground = mock.Mock(side_effect=AssertionError("no railway here to sample"))
        with mock.patch.object(railways, "_resample", wraps=railways._resample) as resample:
            self.assertIsNone(railways.tile_mesh(FRAME, 0, 0, [f], ground))
        ground.assert_not_called()
        resample.assert_not_called()

    def test_crossing_bridge_keeps_both_ends_outside_search_window(self):
        f = features([[(-500.0, 100.0), (800.0, 100.0)]],
                     ["standard_gauge"], subtypes=["rail"], flags=[(0, "is_bridge")])
        parts = railways.parts(FRAME, [f], roads.tile_bbox(0, 0), clip=True)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0][2], "bridge")
        np.testing.assert_allclose(parts[0][3][[0, -1]], f.xy_in(FRAME)[[0, -1]], atol=1e-6, rtol=0)
        self.assertIsNotNone(railways.tile_mesh(FRAME, 0, 0, [f], flat))

    def test_bridge_in_context_outside_drawn_tile_is_kept(self):
        f = features([[(300.0, 100.0), (420.0, 100.0)]],
                     ["standard_gauge"], subtypes=["rail"], flags=[(0, "is_bridge")])
        parts = railways.parts(FRAME, [f], roads.tile_bbox(0, 0), clip=True)
        self.assertEqual(len(parts), 1, "the existing reach supplies bridge and approach context")
        np.testing.assert_allclose(parts[0][3][[0, -1]], f.xy_in(FRAME)[[0, -1]], atol=1e-6, rtol=0)

    def test_bridge_profile_agrees_across_adjacent_tiles(self):
        f = features([[(-400.0, 100.0), (100.0, 100.0)],
                      [(100.0, 100.0), (450.0, 100.0)],
                      [(450.0, 100.0), (900.0, 100.0)]],
                     ["standard_gauge"] * 3, subtypes=["rail"] * 3, flags=[(1, "is_bridge")])
        profiles = []
        for tx in (0, 1):
            parts = railways.parts(FRAME, [f], roads.tile_bbox(tx, 0), clip=True)
            bridge = next(i for i, part in enumerate(parts) if part[2] == "bridge")
            bed, _ = railways.profile(parts, flat)[bridge]
            profiles.append((parts[bridge][3], bed))
        np.testing.assert_allclose(profiles[0][0], profiles[1][0], atol=1e-6, rtol=0)
        np.testing.assert_allclose(profiles[0][1], profiles[1][1], atol=1e-6, rtol=0)

    def test_empty_tile_does_not_sample_ground(self):
        ground = mock.Mock(side_effect=AssertionError("empty tile"))
        self.assertIsNone(railways.tile_mesh(FRAME, 0, 0, [], ground))
        ground.assert_not_called()

    def test_track_mesh_along_a_line(self):
        f = features([[(-50.0, 100.0), (300.0, 100.0)]], ["standard_gauge"], subtypes=["rail"])
        mesh = railways.tile_mesh(FRAME, 0, 0, [f], flat, "fi")
        self.assertIsNotNone(mesh)
        P, T, UV, mats, M = mesh
        self.assertEqual(mats, [railways.BALLAST, railways.CONCRETE, railways.STEEL, railways.SLEEPER])
        self.assertLess(len(P), railways.VERTS_MAX)
        self.assertEqual(int(T.max()) + 1, len(P))
        steel = P[np.unique(T[M == 2])]
        self.assertAlmostEqual(float(steel[:, 2].max()),
                               10.0 + railways.BALLAST_H - railways.SLEEPER_SINK + railways.SLEEPER_H + railways.RAIL_H, 3)
        heads = np.unique(np.round(steel[:, 1] + 0.0, 2))      # (the tile's origin at y 0)
        gap = heads.max() - heads.min() - 2 * railways.RAIL_HEAD    # between the rails' inner faces
        self.assertAlmostEqual(gap, railways.FI_GAUGE_M, delta=0.011, msg="Finnish broad gauge")
        n_sleepers = (M == 3).sum() // 10
        self.assertGreater(n_sleepers, 256 / railways.SLEEPER_EVERY * 0.95)
        self.assertGreaterEqual(P[:, 0].min(), -roads.OVERLAP - railways.STEP_M - 3.0)

    def test_roads_and_trams_are_not_tracks(self):
        f = features([[(0.0, 50.0), (200.0, 50.0)], [(0.0, 80.0), (200.0, 80.0)]], ["residential", "tram"],
                     subtypes=["road", "rail"])
        self.assertIsNone(railways.tile_mesh(FRAME, 0, 0, [f], flat))

    def test_level_crossing_cuts_the_track(self):
        f = features([[(0.0, 100.0), (250.0, 100.0)], [(120.0, 0.0), (120.0, 250.0)]], ["standard_gauge", "secondary"],
                     subtypes=["rail", "road"])
        P, T, UV, mats, M = railways.tile_mesh(FRAME, 0, 0, [f], flat)
        x = P[np.unique(T)][:, 0]
        hw = roads.road_specs(f)[1].width / 2
        self.assertFalse(((x > 120 - hw) & (x < 120 + hw)).any(), "nothing on the road")

    def test_short_tunnel_under_a_road_is_track_and_abandoned_lines_are_worn(self):
        """The line under a road bridge tagged is_tunnel for 12 m (OSM's habit) is track; a long tunnel is left out;
        an abandoned line beside it is drawn worn (Joensuu, 2026-10-25; the user, 2026-10-27: abandoned lines shown,
        grown over)."""
        f = features([[(0.0, 100.0), (250.0, 100.0)], [(0.0, 110.0), (250.0, 110.0)], [(0.0, 150.0), (250.0, 150.0)]],
                     ["standard_gauge", "unknown", "standard_gauge"], subtypes=["rail"] * 3,
                     flags=[(0, "is_tunnel", 0.46, 0.51), (1, "is_abandoned"), (2, "is_tunnel", 0.2, 0.8)])
        worn = {}
        rails = railways.parts(FRAME, [f], roads.tile_bbox(0, 0), worn=worn)
        got = [(fid, kind, float(np.ptp(xy[:, 0]))) for fid, _c, kind, xy in rails]
        self.assertEqual(sorted(g[0] for g in got), ["id000", "id001", "id002", "id002"])
        self.assertEqual(worn, {"id001": 2}, "the abandoned line worn")
        self.assertEqual([c for fid, c, *_ in rails if fid == "id001"], ["standard_gauge"])
        self.assertAlmostEqual([g[2] for g in got if g[0] == "id000"][0], 250.0, 0, "track through the short tunnel")
        self.assertLess(max(g[2] for g in got if g[0] == "id002"), 60.0, "a long tunnel left out")

    def test_level_crossing_rails_meet_the_road_on_gentle_ramps(self):
        """The rails' top is flush with the road where the track crosses it (they stood 0.6 m over it at its edge), and
        the track ramps back to its own height at 1.7 %."""
        s = np.arange(0.0, 200.0, 2.0)
        zb = np.full(len(s), 10.0)
        blk = (s >= 96) & (s <= 104)
        z = railways._flush(s, zb, blk, np.full(int(blk.sum()), 10.0))
        top = z + railways.RAIL_TOP_M
        np.testing.assert_allclose(top[blk], 10.0 + railways.FLUSH_LIFT_M)
        self.assertLessEqual(float(np.abs(np.diff(z) / np.diff(s)).max()), railways.FLUSH_GRADE + 1e-9)
        self.assertEqual(float(z[0]), 10.0, "its own height away from the road")
        P, T, UV, mats, M = railways.tile_mesh(FRAME, 0, 0, [features([[(0.0, 100.0), (250.0, 100.0)],
                                                                       [(120.0, 0.0), (120.0, 250.0)]],
                                                                      ["standard_gauge", "secondary"],
                                                                      subtypes=["rail", "road"])], flat)
        rails = P[np.unique(T[M == mats.index(railways.STEEL)])]
        edge = rails[np.abs(rails[:, 0] - 120.0) < 8.0]
        self.assertTrue(len(edge))
        self.assertLess(float(edge[:, 2].max()), flat(edge[:1, :2])[0] + railways.FLUSH_LIFT_M + 0.08, "flush")

    def test_level_crossing_kits_face_each_approach(self):
        """A secondary road's crossing gets a gate at each approach, on the kerb side of the traffic before the track,
        facing it; a residential street's a cross; an abandoned line's none."""
        f = features([[(0.0, 100.0), (250.0, 100.0)], [(120.0, 0.0), (120.0, 250.0)], [(40.0, 0.0), (40.0, 250.0)],
                      [(0.0, 180.0), (250.0, 180.0)]],
                     ["standard_gauge", "secondary", "residential", "unknown"], subtypes=["rail", "road", "road", "rail"],
                     flags=[(3, "is_abandoned")])
        rows = railways.crossing_props(FRAME, 0, 0, [f], lambda xy: np.full(len(xy), 10.0))
        gates = [r for r in rows if r[0].startswith("rrgate")]
        crosses = [r for r in rows if r[0].startswith("rrcross")]
        self.assertEqual(len(gates), 2, rows)
        self.assertEqual(len(crosses), 2, rows)
        for g, x, y, z, yaw, *_ in gates:
            self.assertGreater(abs(y - 100.0), 3.0, "before the track")
            self.assertLess(abs(y - 100.0), 8.0)
            # (the kerb side: right of the traffic heading for the track; its local +x points there)
            travel = np.array([0.0, 1.0 if y < 100.0 else -1.0])
            right = np.array([travel[1], -travel[0]])
            self.assertGreater((x - 120.0) * right[0], 0.0, "on the right of the traffic")
            ux = np.array([math.cos(math.radians(yaw)), math.sin(math.radians(yaw))])
            np.testing.assert_allclose(ux, right, atol=1e-6)
            self.assertEqual(z, 10.0)
        left = railways.crossing_props(FRAME, 0, 0, [f], lambda xy: np.full(len(xy), 10.0), left_hand=True)
        self.assertTrue(all(r[0].endswith("_l") for r in left if r[0].startswith("rrgate")))

    def test_light_rail_crossings_only_with_data(self):
        f = features([[(0.0, 100.0), (250.0, 100.0)], [(120.0, 0.0), (120.0, 250.0)]], ["light_rail", "secondary"],
                     subtypes=["rail", "road"])
        ground = lambda xy: np.zeros(len(xy))     # noqa: E731
        self.assertEqual(railways.crossing_props(FRAME, 0, 0, [f], ground), [], "a street's light rail: none")
        rows = railways.crossing_props(FRAME, 0, 0, [f], ground, kits=lambda p, c: "gate")
        self.assertEqual(len(rows), 2, "the inventory says gates")

    def test_osm_railway_tags_decide_when_known(self):
        """The weekly rail file's OSM values (railtags.py): proposed out whatever Overture's class, disused worn, a
        gauge in mm; a crossing node's devices."""
        f = features([[(0.0, 100.0), (250.0, 100.0)], [(0.0, 120.0), (250.0, 120.0)], [(0.0, 140.0), (250.0, 140.0)]],
                     ["standard_gauge", "standard_gauge", "unknown"], subtypes=["rail"] * 3)
        f.attrs["osm_railway"] = np.array(["proposed", "disused", "rail"], object)
        f.attrs["osm_gauge"] = np.array([None, None, "1520"], object)
        self.assertIsNone(railways.drawn(f, 0))
        self.assertEqual(railways.drawn(f, 1), ("standard_gauge", 1))
        self.assertEqual(railways.drawn(f, 2), ("standard_gauge", 0))
        self.assertAlmostEqual(railways.osm_gauge(f, 2), 1.52)
        self.assertEqual(railways.osm_kit_of({"railway": "level_crossing", "crossing:barrier": "half"}), "gate")
        self.assertEqual(railways.osm_kit_of({"railway": "level_crossing", "crossing:light": "yes"}), "flash")
        self.assertEqual(railways.osm_kit_of({"crossing:barrier": "no", "crossing:light": "no"}), "cross")
        self.assertIsNone(railways.osm_kit_of({"railway": "level_crossing"}), "no word: the road class's")

    def test_proposed_and_unbuilt_lines_are_not_drawn(self):
        """Overture's "unknown" rail with no abandoned or disused flag is OpenStreetMap's railway=proposed or
        construction (Link Union Station across US-101, 2026-10-27); a line under construction neither."""
        f = features([[(0.0, 100.0), (250.0, 100.0)], [(0.0, 120.0), (250.0, 120.0)], [(0.0, 140.0), (250.0, 140.0)],
                      [(0.0, 160.0), (250.0, 160.0)]],
                     ["unknown", "standard_gauge", "unknown", "standard_gauge"], subtypes=["rail"] * 4,
                     flags=[(1, "is_under_construction"), (2, "is_disused"), (3, "is_disused")])
        worn = {}
        got = sorted({fid for fid, *_ in railways.parts(FRAME, [f], roads.tile_bbox(0, 0), worn=worn)})
        self.assertEqual(got, ["id002", "id003"])
        self.assertEqual(worn, {"id002": 1, "id003": 1})

    def test_abandoned_line_loses_some_sleepers_the_same_in_every_tile(self):
        xy = np.c_[np.arange(0.0, 60.0, 2.0), np.zeros(30)]
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
        u = np.tile([0.0, 1.0], (30, 1))
        top = np.zeros(30)
        full = railways._sleepers(s, xy, u, top, 0, 29)[0]
        some = railways._sleepers(s, xy, u, top, 0, 29, railways.SLEEPERS_GONE[2])[0]
        part = railways._sleepers(s, xy, u, top, 10, 29, railways.SLEEPERS_GONE[2])[0]
        self.assertTrue(0.55 * len(full) < len(some) < 0.85 * len(full), (len(some), len(full)))
        np.testing.assert_allclose(some[some[:, 0] >= s[10]], part, err_msg="a tile's run keeps the same ones")

    def test_short_piece_on_its_own_is_left_out(self):
        f = features([[(0.0, 100.0), (120.0, 100.0)], [(120.0, 100.0), (250.0, 100.0)], [(50.0, 130.0), (62.0, 130.0)]],
                     ["standard_gauge"] * 3, subtypes=["rail"] * 3)
        got = sorted(fid for fid, *_ in railways.parts(FRAME, [f], roads.tile_bbox(0, 0)))
        self.assertEqual(got, ["id000", "id001"], "the 12 m stub goes, two short joined pieces of a line stay")

    def test_road_high_over_the_track_does_not_cut_it(self):
        f = features([[(0.0, 100.0), (250.0, 100.0)], [(120.0, 0.0), (120.0, 250.0)]], ["standard_gauge", "secondary"],
                     subtypes=["rail", "road"])

        def cutting(xy):            # the track 7 m down a cutting, the road on its own level over it
            return np.where(np.abs(xy[:, 1] - 100.0) < 12.0, 10.0, 17.0)
        P, T, UV, mats, M = railways.tile_mesh(FRAME, 0, 0, [f], cutting)
        x = P[np.unique(T[M == 2])][:, 0]
        self.assertTrue(((x > 115) & (x < 125)).any(), "the rails go on under the road")

    def test_bed_smooths_bumps_but_follows_hills(self):
        s = np.arange(0.0, 400.0, railways.STEP_M)
        z = 0.05 * s + 0.4 * np.sin(s / 3.0)
        zb = railways.bed_heights(z)
        mid = slice(20, -20)
        self.assertLess(np.abs(zb - 0.05 * s)[20:-20].max(), 0.1, "the bumps gone")
        self.assertLess(np.abs(zb - 0.05 * s).max(), 0.3, "the grade kept to the ends")

    def _bed(self, f, ground):
        rails = railways.parts(FRAME, [f], roads.tile_bbox(0, 0))
        prof = railways.profile(rails, ground)
        return rails, prof

    def _line(self, ground):
        f = features([[(-400.0, 100.0), (100.0, 100.0)], [(100.0, 100.0), (150.0, 100.0)],
                      [(150.0, 100.0), (640.0, 100.0)]], ["standard_gauge"] * 3, subtypes=["rail"] * 3,
                     flags=[(1, "is_bridge")])
        rails, prof = self._bed(f, ground)
        pts = np.concatenate([xy for *_, xy in rails])
        z = np.concatenate([zb for zb, _ in prof])
        o = np.argsort(pts[:, 0])
        return f, pts[o, 0], z[o]

    def test_bridge_over_a_dip_keeps_the_line_level(self):
        """A short bridge over a stream or an underpass road (the ground 3 m down under it): the deck straight at the
        line's height, no ramps (a 6 m clearance stood an 8 m culvert's track 6.8 m up on 4.5 m ramps, 2026-10-25)."""
        def dip(xy):
            return np.where((xy[:, 0] > 112) & (xy[:, 0] < 138), 7.0, 10.0)
        f, x, z = self._line(dip)
        self.assertLess(np.abs(z - 10.0).max(), 0.05, "on the ground's level everywhere, the bridge too")
        P, T, UV, mats, M = railways.tile_mesh(FRAME, 0, 0, [f], dip)
        piers = P[np.unique(T[M == 1])]
        self.assertTrue(((piers[:, 2] < 7.0) & (piers[:, 0] > 100) & (piers[:, 0] < 150)).any(),
                        "a pier down to the stream bed")

    def test_piers_stand_beside_a_road_under_the_bridge_on_the_ground(self):
        """A rail bridge over a road in a 5 m dip: no pier in the road's lanes, every pier's foot under the ground at
        all its corners (one stood in the lanes, one hung in the air, 2026-10-25)."""
        f = features([[(-300.0, 100.0), (60.0, 100.0)], [(60.0, 100.0), (190.0, 100.0)],
                      [(190.0, 100.0), (500.0, 100.0)], [(125.0, -100.0), (125.0, 300.0)]],
                     ["standard_gauge"] * 3 + ["secondary"], subtypes=["rail"] * 3 + ["road"], flags=[(1, "is_bridge")])

        def dip(xy):                # the ground falling 5 m to the road over 40 m either side of it
            return 10.0 - 5.0 * np.clip(1.0 - np.abs(xy[:, 0] - 125.0) / 40.0, 0.0, 1.0)
        P, T, UV, mats, M = railways.tile_mesh(FRAME, 0, 0, [f], dip)
        con = P[np.unique(T[M == 1])]
        low = con[con[:, 2] < 8.5]          # the piers' feet (the deck's underside: 10 + BALLAST_H - DECK_D = 8.9)
        self.assertTrue(len(low), "piers there")
        hw = roads.road_specs(f)[3].width / 2
        self.assertFalse((np.abs(low[:, 0] - 125.0) < hw + 0.5).any(), "none in the road")
        self.assertTrue((low[:, 2] <= dip(low[:, :2]) - railways.PIER_FOOT_M + 1e-6).all(), "all in the ground")

    def test_ground_through_a_deck_raises_it_on_gentle_ramps(self):
        """Ground under a bridge's middle higher than its ends (bad data, or a ridge): the deck over it, the line
        climbing to it at GRADE at most and back down to the ground past the ramps."""
        def hump(xy):
            return np.where((xy[:, 0] > 120) & (xy[:, 0] < 130), 11.0, 10.0)
        f, x, z = self._line(hump)
        deck = (x > 120) & (x < 130)
        self.assertGreaterEqual(z[deck].min(), 11.0 - 1e-6)
        dz = np.abs(np.diff(z)) / np.maximum(np.diff(x), 1e-9)
        self.assertLessEqual(dz[np.diff(x) > 0.5].max(), railways.GRADE + 1e-6)
        self.assertAlmostEqual(float(z[x < -300].max()), 10.0, 1, "back on the ground past the ramp")

    def test_bridge_over_a_valley_runs_straight(self):
        """A bridge over a 30 m deep valley between its banks: straight between them, piers down to the floor."""
        f = features([[(-200.0, 100.0), (60.0, 100.0)], [(60.0, 100.0), (200.0, 100.0)],
                      [(200.0, 100.0), (450.0, 100.0)]], ["standard_gauge"] * 3, subtypes=["rail"] * 3,
                     flags=[(1, "is_bridge")])

        def valley(xy):
            x = xy[:, 0]
            return np.where((x > 70) & (x < 190), -20.0, 10.0 + 0.01 * x)
        rails, prof = self._bed(f, valley)
        (zb, zg), = [p for p, (_, _, k, _) in zip(prof, rails) if k == "bridge"]
        line = np.linspace(zb[0], zb[-1], len(zb))
        self.assertLess(np.abs(zb - line).max(), 0.3)
        self.assertAlmostEqual(zb[0], 10.6, 0)
        self.assertAlmostEqual(zb[-1], 12.0, 0)
        P, T, UV, mats, M = railways.tile_mesh(FRAME, 0, 0, [f], valley)
        self.assertLess(P[np.unique(T[M == 1])][:, 2].min(), -20.0)

    def test_track_follows_a_hill(self):
        f = features([[(-100.0, 100.0), (400.0, 100.0)]], ["standard_gauge"], subtypes=["rail"])

        def hill(xy):
            return 10.0 + 0.015 * xy[:, 0]
        rails, prof = self._bed(f, hill)
        zb, zg = prof[0]
        self.assertLess(np.abs(zb - zg)[10:-10].max(), 0.05)

    def test_ground_graded_to_the_bed(self):
        """A line through a 6 m ridge (a rock cutting the DEM half shows): the grader lays the ground to the track's
        bed, the walls back from it, so the track is not buried (2026-10-25: half a track under the rock)."""
        import grading
        from test_grading import FakeMD
        f = features([[(-400.0, 0.0), (400.0, 0.0)]], ["standard_gauge"], subtypes=["rail"])

        def ridge(la, lo):
            e, _n, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
            return 10.0 + 6.0 * np.exp(-(np.asarray(e) / 40.0) ** 2)
        g = grading.Grader(FRAME, FakeMD(f), ridge)
        e = np.linspace(-60.0, 60.0, 25)
        for off in (0.0, 1.5, -1.5):
            la, lo, _ = FRAME.enu_to_geodetic(e, np.full(len(e), off), 0.0)
            raw = ridge(la, lo)
            got = g.apply(la, lo, raw)
            rails = railways.parts(FRAME, [f], (-60, -10, 60, 10))
            (zb, _), = railways.profile(rails, lambda xy: ridge(*FRAME.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)[:2]))
            want = np.interp(e, rails[0][3][:, 0], zb)
            self.assertLess(np.abs(got - want).max(), 0.1, f"the ground at {off} m off the line is at its bed")
            self.assertLess(abs(float(got[12]) - float(raw[12])), 0.3, "over the ridge as it is (the real elevation)")
        self.assertLess(railways.FOOT_MAX_M, 10.0)

    def test_written_with_the_tile(self):
        f = features([[(-50.0, 100.0), (300.0, 100.0)]], ["standard_gauge"], subtypes=["rail"])
        mesh = railways.tile_mesh(FRAME, 0, 0, [f], flat)
        with tempfile.TemporaryDirectory() as d:
            roads.write_tile(d, "/vfs", 1, 0, 0, [], rail_mesh=mesh)
            self.assertTrue(os.path.exists(os.path.join(d, "rail_1_0_0.cdae")))
            self.assertIn("rail", roads.shape_files(1, 0, 0))



class RoadUnderpassTest(unittest.TestCase):
    """A tunnel flag on the lower road must also keep the railway above it open."""

    def setUp(self):
        railways._GROUND_MEMO.clear()

    def tearDown(self):
        railways._GROUND_MEMO.clear()

    def features(self, split=False, tunnel=True, parallel=False):
        track = [[(-200, 100), (120, 100)], [(120, 100), (450, 100)]] if split else [[(-200, 100), (450, 100)]]
        road = [(110, 90), (130, 90)] if parallel else [(120, 90), (120, 110)]
        return features(track + [road], ["standard_gauge"] * len(track) + ["secondary"],
                        subtypes=["rail"] * len(track) + ["road"],
                        flags=[(len(track), "is_tunnel")] if tunnel else [])

    def test_span_replaces_embankment_and_has_clearance(self):
        f = self.features()
        box = roads.tile_bbox(0, 0)
        tunnels = railways.road_underpasses(FRAME, [f], box)
        parts = railways.parts(FRAME, [f], box)
        self.assertTrue(any(p[2] == "bridge" for p in parts))
        prof = railways.profile(parts, flat, tunnels)
        for (_, _, kind, xy), (z, _) in zip(parts, prof):
            self.assertLessEqual(float((np.abs(np.diff(z)) / np.diff(railways._arc(xy))).max()),
                                 railways.GRADE + 1e-6)
            if kind == "bridge":
                self.assertGreaterEqual(float(z.min() + railways.BALLAST_H - railways.DECK_D),
                                        10 + railways.UNDERPASS_CLEAR_M - 0.5)
        P, T, _uv, _mats, _M = railways.tile_mesh(FRAME, 0, 0, [f], flat)
        # Check all vertices belonging to triangles over the actual lower lane,
        # including ballast skirts, the concrete deck, and support piers.
        tri = P[T]
        x = float(tunnels[0][0][:, 0].mean())
        over = (tri[:, :, 0].min(1) < x + 3) & (tri[:, :, 0].max(1) > x - 3)
        self.assertTrue(over.any())
        self.assertGreater(float(tri[over, :, 2].min()), 10 + railways.UNDERPASS_CLEAR_M - 0.1)

    def test_no_bridge_for_level_crossing_or_parallel_track(self):
        for f in (self.features(tunnel=False), self.features(parallel=True)):
            self.assertTrue(all(p[2] == "ground" for p in railways.parts(FRAME, [f], roads.tile_bbox(0, 0))))

    def test_tunnel_ending_beside_track_does_not_create_a_bridge(self):
        f = features([[(-200, 100), (450, 100)], [(120, 80), (120, 94)]],
                     ["standard_gauge", "secondary"], subtypes=["rail", "road"], flags=[(1, "is_tunnel")])
        self.assertTrue(all(p[2] == "ground" for p in railways.parts(FRAME, [f], roads.tile_bbox(0, 0))))

    def test_feature_split_and_adjacent_tiles_keep_the_same_span(self):
        f = self.features(split=True)
        a = railways.parts(FRAME, [f], (0, 0, 256, 256))
        b = railways.parts(FRAME, [f, f], (256, 0, 512, 256))
        spans_a = [(p[0], p[3]) for p in a if p[2] == "bridge"]
        spans_b = [(p[0], p[3]) for p in b if p[2] == "bridge"]
        self.assertEqual(len(spans_a), 2)
        self.assertEqual(len(spans_a), len(spans_b))
        for (fa, xa), (fb, xb) in zip(spans_a, spans_b):
            self.assertEqual(fa, fb)
            np.testing.assert_array_equal(xa, xb)
        np.testing.assert_allclose(spans_a[0][1][-1], spans_a[1][1][0], atol=1e-8)

    def test_grading_keeps_lower_road_at_its_floor(self):
        from types import SimpleNamespace
        import grading
        f = self.features()
        md = SimpleNamespace(cell=lambda *a, **kw: SimpleNamespace(layers={"roads": f}))
        def embankment(la, lo):
            e, _, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
            return np.where(np.abs(e - 120.0) < 7.0, 10.0, 15.0)
        g = grading.Grader(FRAME, md, embankment)
        ring = f.parts(len(f) - 1)[0][0]
        e, n, _ = FRAME.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
        xy = np.c_[e, n]
        line = railways._resample(xy, 1.0)
        la, lo, _ = FRAME.enu_to_geodetic(line[:, 0], line[:, 1], 0.0)
        z = g.apply(la, lo, np.full(len(la), 10.0), grading.ROAD_LIFT)
        self.assertLess(float(np.abs(z - (10 + grading.ROAD_LIFT)).max()), 0.2,
                        "rail approach grading must not refill the lower road")



class UnderpassNeighbourTest(unittest.TestCase):
    """Only the track actually over the road gets a span and its clearance."""

    def test_non_crossing_neighbour_stays_on_ground(self):
        for y in (114.0, 122.0):
            with self.subTest(neighbour_y=y):
                f = features([[(-200, 100), (450, 100)], [(-200, y), (450, y)],
                              [(120, 90), (120, 110)]],
                             ["standard_gauge", "standard_gauge", "secondary"],
                             subtypes=["rail", "rail", "road"], flags=[(2, "is_tunnel")])
                box = roads.tile_bbox(0, 0)
                parts = railways.parts(FRAME, [f], box)
                neighbour = str(f.ids[1])
                selected = [p for p in parts if p[0] == neighbour]
                self.assertTrue(selected)
                self.assertTrue(all(p[2] == "ground" for p in selected))
                profiles = railways.profile(parts, flat, railways.road_underpasses(FRAME, [f], box))
                for part, (bed, _) in zip(parts, profiles):
                    if part[0] == neighbour:
                        np.testing.assert_allclose(bed, 10.0, atol=1e-6)

    def test_existing_neighbour_bridge_keeps_its_height(self):
        f = features([[(-200, 100), (450, 100)], [(-200, 114), (450, 114)],
                      [(120, 90), (120, 110)]],
                     ["standard_gauge", "standard_gauge", "secondary"],
                     subtypes=["rail", "rail", "road"], flags=[(1, "is_bridge"), (2, "is_tunnel")])
        box = roads.tile_bbox(0, 0)
        parts = railways.parts(FRAME, [f], box)
        profiles = railways.profile(parts, flat, railways.road_underpasses(FRAME, [f], box))
        neighbour = [(part, bed) for part, (bed, _) in zip(parts, profiles) if part[0] == str(f.ids[1])]
        self.assertTrue(neighbour)
        for part, bed in neighbour:
            self.assertEqual(part[2], "bridge")
            np.testing.assert_allclose(bed, 10.0, atol=1e-6,
                                       err_msg="a tunnel under a different track must not raise this bridge")

    def test_track_connected_far_away_does_not_get_another_span(self):
        # Both parallel legs belong to one feature, joined 330 m from the crossing.
        f = features([[(-200, 100), (450, 100), (450, 122), (-200, 122)],
                      [(120, 90), (120, 110)]], ["standard_gauge", "secondary"],
                     subtypes=["rail", "road"], flags=[(1, "is_tunnel")])
        spans = [p[3] for p in railways.parts(FRAME, [f], roads.tile_bbox(0, 0)) if p[2] == "bridge"]
        self.assertTrue(spans)
        self.assertTrue(all(float(xy[:, 1].max()) < 101 for xy in spans),
                        "the returning leg beside the tunnel is not another crossing")

    def test_span_continues_across_a_feature_split_beside_crossing(self):
        for split in (110, 114, 120, 126, 130):
            with self.subTest(split=split):
                f = features([[(-200, 100), (split, 100)], [(split, 100), (450, 100)],
                              [(120, 90), (120, 110)]],
                             ["standard_gauge", "standard_gauge", "secondary"],
                             subtypes=["rail", "rail", "road"], flags=[(2, "is_tunnel")])
                a = [p for p in railways.parts(FRAME, [f], roads.tile_bbox(0, 0)) if p[2] == "bridge"]
                b = [p for p in railways.parts(FRAME, [f, f], roads.tile_bbox(1, 0)) if p[2] == "bridge"]
                self.assertEqual({p[0] for p in a}, {str(f.ids[0]), str(f.ids[1])})
                self.assertEqual(len(a), len(b))
                for pa, pb in zip(a, b):
                    self.assertEqual(pa[:3], pb[:3])
                    np.testing.assert_array_equal(pa[3], pb[3])
                np.testing.assert_allclose(a[0][3][-1], a[1][3][0], atol=1e-6)

    def test_two_tracks_really_crossing_the_tunnel_both_get_clearance(self):
        f = features([[(-200, 100), (450, 100)], [(-200, 108), (450, 108)],
                      [(120, 90), (120, 118)]],
                     ["standard_gauge", "standard_gauge", "secondary"],
                     subtypes=["rail", "rail", "road"], flags=[(2, "is_tunnel")])
        box = roads.tile_bbox(0, 0)
        parts = railways.parts(FRAME, [f], box)
        profiles = railways.profile(parts, flat, railways.road_underpasses(FRAME, [f], box))
        raised = {part[0] for part, (bed, _) in zip(parts, profiles)
                  if part[2] == "bridge" and float(bed.max()) > 14.0}
        self.assertEqual(raised, {str(f.ids[0]), str(f.ids[1])})


if __name__ == "__main__":
    unittest.main()
