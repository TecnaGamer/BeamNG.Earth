"""Road surfaces and markings as a mesh (roadmesh.py): ribbons laid on the ground, facing up, lifted by
layer, one material per chunk material, written as a cdae."""
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import cdae  # noqa: E402
import roadmesh  # noqa: E402
import math  # noqa: E402
from test_roads import FRAME, LAT, LON, M_LAT, M_LON  # noqa: E402


def chunk(m, w, pts, tl=10.0):
    return {"m": m, "w": w, "tl": tl, "p": 20, "n": [list(p) for p in pts]}


def slope(la, lo):
    """A 5 % slope east (via the frame): heights from the points' ENU x."""
    e, _, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
    return 10.0 + 0.05 * e


class RoadMeshTest(unittest.TestCase):
    def test_ribbons_drape_face_up_and_lift(self):
        chunks = [chunk("road_asphalt_2lane", 6.0, [(0, 0, 0), (40, 0, 0), (80, 20, 0)]),
                  chunk("line_yellow", 0.12, [(0, 0.12, 0), (40, 0.12, 0)])]
        P, T, UV, mats, M = roadmesh.build(FRAME, chunks, slope, (0.0, 0.0, 0.0))
        self.assertEqual(mats, ["road_asphalt_2lane", "line_yellow"])
        n = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
        self.assertTrue((n[:, 2] > 0).all(), "counter-clockwise seen from above")
        base = P[np.unique(T[M == 0])]
        want = 10.0 + 0.05 * base[:, 0] + roadmesh.LIFT["base"]
        self.assertLess(np.abs(base[:, 2] - want).max(), 0.01, "on the ground, lifted (frame curvature ~mm)")
        mark = P[np.unique(T[M == 1])]
        self.assertGreater((mark[:, 2] - (10.0 + 0.05 * mark[:, 0])).min(), roadmesh.LIFT["base"] + 0.005,
                           "markings above the asphalt")
        self.assertLessEqual(np.diff(np.unique(np.round(base[:, 0], 3))).max(), roadmesh.DRAPE_STEP + 1e-6)
        self.assertTrue(((UV[:, 0] >= 0.0) & (UV[:, 0] <= 1.0)).all(), "u stays across the original width")
        self.assertEqual((UV[:, 0].min(), UV[:, 0].max()), (0.0, 1.0))

    def test_wide_asphalt_follows_the_graded_hump_between_its_edges(self):
        def hump(la, lo):
            e, n, _ = FRAME.geodetic_to_enu(la, lo, 0.0)
            return 10.0 + 0.6 * np.exp(-(n * n) / 9.0) * np.sin(np.pi * e / 60.0) ** 2

        ch = chunk("road_asphalt_2lane", 13.0, [(0.0, 0.0, 0.0), (60.0, 0.0, 0.0)])
        # The old 13 m cross-section only sampled both edges; its asphalt chord missed the
        # graded hump and the ground poked over it by 55 cm. Check both the terrain-triangle
        # path used by the server and the cross-width fallback against the emitted planes.
        for follow in (False, True):
            with self.subTest(follow=follow):
                P, T, _uv, _mats, _materials = roadmesh.build(FRAME, [ch], hump, (0.0, 0.0, 0.0),
                                                           lift=0.05, follow=follow)
                for x in np.arange(20.0, 40.1, 2.0):
                    for y in (-2.0, -1.0, 0.0, 1.0, 2.0):
                        q = np.array([x, y])
                        a, b, c = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
                        ab, ac, ap = b - a, c - a, q - a[:, :2]
                        den = ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0]
                        u = (ap[:, 0] * ac[:, 1] - ap[:, 1] * ac[:, 0]) / den
                        v = (ab[:, 0] * ap[:, 1] - ab[:, 1] * ap[:, 0]) / den
                        hit = (u >= -1e-8) & (v >= -1e-8) & (u + v <= 1.0 + 1e-8)
                        self.assertTrue(hit.any())
                        asphalt = np.max((a[:, 2] + u * ab[:, 2] + v * ac[:, 2])[hit])
                        ground = 10.0 + 0.6 * np.exp(-(y * y) / 9.0) * np.sin(np.pi * x / 60.0) ** 2
                        self.assertGreaterEqual(asphalt, ground - 0.01, "the drawn asphalt covers the graded road")

    def test_writes_one_primitive_per_material(self):
        chunks = [chunk("road_asphalt_2lane", 6.0, [(0, 0, 0), (30, 0, 0)]),
                  chunk("gutter1", 0.5, [(0, 3, 0), (30, 3, 0)]),
                  chunk("road_asphalt_2lane", 6.0, [(10, -20, 0), (10, 20, 0)])]
        mesh = roadmesh.build(FRAME, chunks, lambda la, lo: np.zeros(np.shape(la)), (0.0, 0.0, 0.0))
        with tempfile.TemporaryDirectory() as d:
            roadmesh.write(os.path.join(d, "r.cdae"), mesh)
            shape = cdae.read(os.path.join(d, "r.cdae"))
        self.assertEqual([m["name"] for m in shape["materials"]], ["road_asphalt_2lane", "gutter1"])
        self.assertEqual(roadmesh.layer_of({"m": "line_white"}), "mark")
        self.assertIsNone(roadmesh.build(FRAME, [], lambda la, lo: la, (0, 0, 0)))


    def test_clipped_to_the_tile(self):
        """Two tiles' ribbons each reach roads.OVERLAP past their edge; overlapping at one height they flickered along
        every seam (the user, 2026-10-20): each is cut exactly at its tile's edge, the two together cover the road
        once."""
        flat = lambda la, lo: np.zeros(np.shape(la))     # noqa: E731
        area = 0.0
        for box in ((0.0, 0.0, 256.0, 256.0), (256.0, 0.0, 512.0, 256.0)):
            chunks = [chunk("road_asphalt_2lane", 6.0, [(box[0] - 2, 98, 0), (box[2] + 2, 130, 0)])]  # oblique
            P, T, UV, mats, M = roadmesh.build(FRAME, chunks, flat, (box[0], box[1], 0.0), clip=box)
            xy = P[:, :2] + box[:2]
            used = xy[np.unique(T)]
            self.assertTrue((used[:, 0] >= box[0] - 1e-6).all() and (used[:, 0] <= box[2] + 1e-6).all())
            self.assertTrue(np.isfinite(UV).all())
            a = xy[T]
            e1, e2 = a[:, 1] - a[:, 0], a[:, 2] - a[:, 0]
            area += float(np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]).sum() / 2)
        L = np.hypot(512.0 - 0.0 + 4.0, 32.0) * 512.0 / 516.0       # the road's length over both tiles
        self.assertAlmostEqual(area, 6.0 * L, delta=6.0 * L * 0.01)

    def test_slanted_road_covered_at_the_seam(self):
        """A road crossing the seam at 50 degrees (the E6 in Norway): no triangle of it left uncovered between the
        two tiles' square ribbon ends."""
        flat = lambda la, lo: np.zeros(np.shape(la))     # noqa: E731
        k = 1.2                                          # y = k x
        area = 0.0
        for box in ((0.0, -1000.0, 256.0, 1000.0), (256.0, -1000.0, 512.0, 1000.0)):
            a, b = box[0] - 2.0, box[2] + 2.0           # cut roads.OVERLAP past the tile, as roads.build_tile does
            chunks = [chunk("road_asphalt_2lane", 11.0, [(a, k * a, 0), (b, k * b, 0)])]
            P, T, UV, mats, M = roadmesh.build(FRAME, chunks, flat, (box[0], 0.0, 0.0), clip=box)
            xy = P[:, :2] + [box[0], 0.0]
            t = xy[T]
            e1, e2 = t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]
            area += float(np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]).sum() / 2)
        L = 512.0 * np.hypot(1.0, k)
        self.assertAlmostEqual(area, 11.0 * L, delta=1.0)


class FollowTest(unittest.TestCase):
    """The road surface on the terrain block's own triangles (the user, 2026-10-20: the tyres sank into a road
    drawn 5 cm over the ground)."""

    def bumpy(self):
        def elev(la, lo):
            e = (np.asarray(lo) - LON) * M_LON
            n = (np.asarray(la) - LAT) * M_LAT
            return 10.0 + 0.8 * np.sin(e / 3.1) * np.cos(n / 2.3) + 0.05 * e
        return elev

    def test_kernel_and_python_agree(self):
        import fast
        if not (fast.AVAILABLE and hasattr(fast._ef, "follow_tris")):
            self.skipTest("no kernel")
        rng = np.random.default_rng(5)
        P = rng.uniform(-9, 9, (60, 2))
        T = np.array([rng.choice(60, 3, replace=False) for _ in range(40)], np.int64)
        UV = rng.uniform(0, 1, (60, 2))
        gx, gy = np.meshgrid(np.arange(-7, 8) * 2.0, np.arange(-7, 8) * 2.0)
        grid = np.round((np.sin(gx / 3) * np.cos(gy / 2) + 0.03 * gx) * 32) / 32
        a = fast._ef.follow_tris(P, UV, T, 2.0, grid, -7, -7, 0.005)
        b = roadmesh._follow_py(P, UV, T, 2.0, grid, -7, -7, 0.005)
        for x, y in zip(a, b):
            self.assertTrue(np.allclose(np.asarray(x), y, equal_nan=True))

    def test_with_filled_polygons(self):
        """A tile with corner asphalt (a filled polygon) follows the ground too: the polygon's points once took the
        name of the height quantum and every such road tile failed (a Steam Deck in Baltimore, 2026-10-20)."""
        chunks = [chunk("road_asphalt_2lane", 9.0, [(3.3, 1.7, 0), (61.9, 40.2, 0)]),
                  {"m": "road_asphalt_2lane", "tl": 10.0, "poly": [(0.0, 0.0, 0), (8.0, 0.0, 0), (8.0, 6.0, 0)], "n": []}]
        P, T, UV, mats, M = roadmesh.build(FRAME, chunks, self.bumpy(), (0.0, 0.0, 0.0), 0.015, follow=True,
                                           q=1 / 32)
        self.assertGreater(len(T), 0)

    def test_on_the_ground_everywhere(self):
        elev = self.bumpy()
        chunks = [chunk("road_asphalt_2lane", 9.0, [(3.3, 1.7, 0), (61.9, 40.2, 0), (90.0, 20.0, 0)])]
        P, T, UV, mats, M = roadmesh.build(FRAME, chunks, elev, (0.0, 0.0, 0.0), 0.015, follow=True, q=1 / 32)
        # the engine's terrain: samples every 2 m (rounded to 1/32 m), each square cut south-west to north-east
        def ground(x, y):
            la, lo, _ = FRAME.enu_to_geodetic(np.array([x]), np.array([y]), 0.0)
            i, j = math.floor(x / 2), math.floor(y / 2)
            def s(a, b):
                la_, lo_, _ = FRAME.enu_to_geodetic(np.array([2.0 * a]), np.array([2.0 * b]), 0.0)
                return round(float(FRAME.geodetic_to_enu(la_, lo_, elev(la_, lo_))[2][0]) * 32) / 32
            u, v = x / 2 - i, y / 2 - j
            if v > u:
                return s(i, j) + v * (s(i, j + 1) - s(i, j)) + u * (s(i + 1, j + 1) - s(i, j + 1))
            return s(i, j) + u * (s(i + 1, j) - s(i, j)) + v * (s(i + 1, j + 1) - s(i + 1, j))
        rng = np.random.default_rng(2)
        worst = 0.0
        for t in T[rng.choice(len(T), 300)]:
            w = rng.dirichlet((1, 1, 1))
            p = (P[t] * w[:, None]).sum(0)
            worst = max(worst, abs(p[2] - 0.015 - ground(p[0], p[1])))
        self.assertLess(worst, roadmesh.FOLLOW_TOL + 1e-6, "the surface is the drawn ground + the lift")
        a = P[T][:, :, :2]
        e1, e2 = a[:, 1] - a[:, 0], a[:, 2] - a[:, 0]
        self.assertTrue((e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0] > 0).all(), "facing up")


if __name__ == "__main__":
    unittest.main()


class TunnelCapControlTest(unittest.TestCase):
    def test_low_arch_caps_stop_before_crossing_roads_or_can_be_disabled(self):
        ch = {"m": "road_asphalt_2lane", "w": 7.0, "fixed": True, "tunnel": True,
              "n": [[0.0, 0.0, 10.0], [40.0, 0.0, 10.0]], "tunnel_h": 4.0,
              "portal": [True, False]}
        for enabled, depth, want in ((True, None, roadmesh.LOW_CAP_M), (True, 3.0, 3.0),
                                     (True, 0.5, None), (False, 3.0, None)):
            with self.subTest(enabled=enabled, depth=depth):
                P, T, _uv, mats, M = roadmesh.tunnel([{**ch, "arch_cap": enabled, "cap_m": [depth, None]}],
                                                    (0.0, 0.0, 0.0))
                if want is None:
                    self.assertNotIn(roadmesh.TUNNEL_SOIL, mats, "no cap can block the crossing road")
                else:
                    soil = P[np.unique(T[M == mats.index(roadmesh.TUNNEL_SOIL)])]
                    self.assertAlmostEqual(soil[:, 0].min(), 0.0)
                    self.assertAlmostEqual(soil[:, 0].max(), want, msg="the cap respects the road clearance")


class BridgeTest(unittest.TestCase):
    def test_deck_heights_straight_between_abutments(self):
        import roads
        parts = [np.array([[0.0, 0], [60, 0], [100, 0]]), np.array([[100.0, 0], [200, 0]])]
        z = roads.deck_heights(parts, lambda xy: 10.0 + 0.1 * xy[:, 0])     # abutments at x 0 and 200
        self.assertTrue(np.allclose(z[0], [10.0, 16.0, 20.0]), z[0])
        self.assertTrue(np.allclose(z[1], [20.0, 30.0]), z[1])
        loop = [np.array([[0.0, 0], [10, 0]]), np.array([[10.0, 0], [0, 0]])]
        self.assertTrue(np.isnan(roads.deck_heights(loop, lambda xy: xy[:, 0])[0]).all(), "no abutment known")

    def test_deck_slab_collides_and_closes(self):
        deck = {"m": "road_asphalt_2lane", "w": 8.0, "tl": 11.0, "p": 20, "deck": True, "fixed": True,
                "n": [[0, 0, 20.0], [50, 0, 22.0]]}
        P, T, UV, mats, M = roadmesh.deck([deck], (0.0, 0.0, 0.0))
        self.assertEqual(mats, [roadmesh.DECK_SIDE], "an opaque slab")
        n = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
        n /= np.linalg.norm(n, axis=1)[:, None]
        flat = np.abs(n[:, 2]) > 0.5
        c = P[T].mean(1)
        high = c[:, 2] > 20.0 + 0.04 * c[:, 0] - roadmesh.DECK_DEPTH / 2   # the top rises 20 -> 22 m
        self.assertTrue((n[flat & high, 2] > 0).all() and (flat & high).any(), "the top faces up")
        self.assertTrue((n[flat & ~high, 2] < 0).all() and (flat & ~high).any(), "the underside faces down")
        self.assertAlmostEqual(P[:, 2].max(), 22.0 + roadmesh.PARAPET_H, msg="a parapet on each edge")
        self.assertAlmostEqual(P[:, 2].min(), 20.0 - roadmesh.DECK_DEPTH - roadmesh.GIRDER_D, msg="girders under it")
        Pr, Tr, _, mr, _ = roadmesh.build(FRAME, [deck], lambda la, lo: la, (0, 0, 0))
        self.assertEqual(mr, ["road_asphalt_2lane"], "the asphalt is in the road mesh ...")
        self.assertAlmostEqual(Pr[:, 2].max(), 22.0 + roadmesh.DECK_SKIN, msg="... just over the slab")


class BridgeLookTest(unittest.TestCase):
    """Girders, parapets and piers (2026-10-17, the user: "Bridges need a lot of work in general")."""

    def deck(self, y, fid, z=20.0):
        return {"m": "road_asphalt_2lane", "w": 8.0, "tl": 11.0, "p": 20, "deck": True, "fixed": True, "fid": fid,
                "n": [[0, y, z], [50, y, z]]}

    def parapet_tops(self, P):
        return P[np.abs(P[:, 2] - (20.0 + roadmesh.PARAPET_H)) < 1e-6]

    def test_no_parapet_between_decks_side_by_side(self):
        P = roadmesh.deck([self.deck(0.0, "a"), self.deck(8.0, "b")], (0.0, 0.0, 0.0))[0]
        ys = np.unique(np.round(self.parapet_tops(P)[:, 1], 2))
        self.assertTrue(ys.min() < -4.0 and ys.max() > 12.0, "the outer edges have one")
        self.assertFalse(((ys > 0.5) & (ys < 7.5)).any(), "none between them: %s" % ys)

    def test_parapet_where_a_deck_runs_under(self):
        P = roadmesh.deck([self.deck(0.0, "a"), self.deck(8.0, "b", z=12.0)], (0.0, 0.0, 0.0))[0]
        ys = np.unique(np.round(self.parapet_tops(P)[:, 1], 2))
        self.assertTrue(((ys > 3.0) & (ys < 5.0)).any(), "over a lower deck the edge is free")

    def test_tunnel_floor_is_a_slab(self):
        d = self.deck(0.0, "a")
        d["tunnel"] = True
        P = roadmesh.deck([d], (0.0, 0.0, 0.0))[0]
        self.assertAlmostEqual(P[:, 2].max(), 20.0)
        self.assertAlmostEqual(P[:, 2].min(), 20.0 - roadmesh.DECK_DEPTH)

    def test_piers_reach_the_ground(self):
        p = (25.0, 0.0, 1.0, 0.0, 17.8, 3.0, 8.0)
        P, T, UV, mats, M = roadmesh.deck([], (0.0, 0.0, 0.0), [p])
        self.assertAlmostEqual(P[:, 2].max(), 17.8)
        self.assertAlmostEqual(P[:, 2].min(), 3.0)
        c = P[T].mean(1)
        n = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
        out = c - np.array([25.0, 0.0, 10.0])
        self.assertTrue(((n * out).sum(1) > 0).all(), "faces outward")
        wide = roadmesh.deck([], (0.0, 0.0, 0.0), [(25.0, 0.0, 1.0, 0.0, 17.8, 3.0, 24.0)])[0]
        self.assertGreater(len(wide), len(P), "a wide deck: a bent of columns")


class PierSitesTest(unittest.TestCase):
    def test_road_clearance_without_native_module(self):
        import roads
        from unittest.mock import patch
        xy = np.c_[np.arange(0.0, 240.1, 4.0), np.zeros(61)]
        z = np.full(len(xy), 20.0)
        road = [np.array([[120.0, -50.0], [120.0, 50.0]])]
        with patch("fast.AVAILABLE", False):
            ps = roads.pier_sites(xy, z, 10.0, lambda q: np.zeros(len(q)), road)
        self.assertGreater(len(ps), 0, "fallback places safe piers instead of failing the entire bridge")
        self.assertTrue(all(abs(p[0] - 120.0) > roads.PIER_ROAD_M + roadmesh.PIER_T / 2 for p in ps))

    def test_clearance_uses_the_crossing_motorway_width(self):
        import roads
        from unittest.mock import patch
        xy = np.c_[np.arange(0.0, 240.1, 4.0), np.zeros(61)]
        z = np.full(len(xy), 20.0)
        road = [np.array([[120.0, -50.0], [120.0, 50.0]])]
        for native in (False, True):
            import fast
            if native and not fast.AVAILABLE:
                continue
            with self.subTest(native=native), patch("fast.AVAILABLE", native):
                ps = roads.pier_sites(xy, z, 10.0, lambda q: np.zeros(len(q)), road, road_widths=[40.0])
                self.assertGreater(len(ps), 0)
                self.assertTrue(all(abs(p[0] - 120.0) > 21.0 + roadmesh.PIER_T / 2 for p in ps),
                                "the full roadway and pier thickness stay clear")

    def test_spacing_roads_and_low_decks(self):
        import roads
        xy = np.c_[np.arange(0.0, 200.1, 4.0), np.zeros(51)]
        z = np.full(51, 20.0)
        flat = lambda q: np.zeros(len(q))          # noqa: E731
        ps = roads.pier_sites(xy, z, 10.0, flat)
        self.assertGreaterEqual(len(ps), 3)
        xs = sorted(p[0] for p in ps)
        self.assertTrue(all(b - a > 20.0 for a, b in zip(xs, xs[1:])))
        self.assertAlmostEqual(ps[0][4], 20.0 - roadmesh.DECK_DEPTH - roadmesh.GIRDER_D)
        # a road under the deck at x = 100: no pier on it
        road = [np.array([[100.0, -50.0], [100.0, 50.0]])]
        ps2 = roads.pier_sites(xy, z, 10.0, flat, road)
        self.assertTrue(all(abs(p[0] - 100.0) > roads.PIER_ROAD_M for p in ps2))
        # a deck lying on the ground: none
        self.assertEqual(roads.pier_sites(xy, np.full(51, 1.0), 10.0, flat), [])
        # a short bridge: none
        self.assertEqual(roads.pier_sites(xy[:6], z[:6], 10.0, flat), [])

    def test_pier_cannot_intrude_into_a_deck_in_the_neighboring_tile(self):
        # The pier centre is in this tile; the lower deck is just over its boundary and supplied
        # as context rather than emitted in this tile. Its cap/wall still must not block that road.
        lower = {"m": "road_asphalt_2lane", "w": 8.0, "tl": 11.0, "fixed": True, "deck": True,
                 "fid": "neighbor", "n": [[4.0, -40.0, 10.0], [4.0, 40.0, 10.0]]}
        pier = (0.0, 0.0, 0.0, 1.0, 18.0, 0.0, 10.0)
        self.assertIsNotNone(roadmesh.deck([], (0.0, 0.0, 0.0), [pier]))
        self.assertIsNone(roadmesh.deck([], (0.0, 0.0, 0.0), [pier], context=[lower]))


class DeckRulesTest(unittest.TestCase):
    def test_upper_deck_over_the_lower_one(self):
        # two roadways on one line, levels 2 and 3 (the Queensboro Bridge): the upper clears the lower
        import roads
        xy = np.c_[np.arange(0.0, 600.1, 4.0), np.zeros(151)]
        flat = lambda q: np.full(len(q), 2.0)      # noqa: E731
        zs = roads.deck_profile([xy, xy + [0.0, 3.0]], flat, levels=[2.0, 3.0], widths=[10.0, 10.0])
        mid = slice(40, 110)
        self.assertTrue((zs[1][mid] - zs[0][mid] >= roads.DECK_ROAD_CLEAR - 0.6).all(),
                        float((zs[1][mid] - zs[0][mid]).min()))

    def test_no_step_onto_a_steep_abutment(self):
        # Lombard Street, Baltimore (2026-10-20): the map's bridge line ends down its east embankment, whose top stands
        # over the deck's climb cap from that end; the deck met it in a 0.86 m step in 4 m and cars stopped dead there
        import roads
        s = np.arange(0.0, 164.1, 4.0)
        xy = np.c_[s, np.zeros_like(s)]
        g = np.interp(s, [0, 20, 24, 128, 133, 137, 164], [20.4, 22.0, 18.4, 18.4, 25.0, 24.7, 22.2])
        zs = roads.deck_profile([xy], lambda q: np.interp(q[:, 0], s, g))
        z = zs[0]
        self.assertTrue((z >= g - 1e-6).all(), "never under the ground")
        top = int(np.argmax(z))
        self.assertAlmostEqual(s[top], 132.0, delta=4.0)
        self.assertTrue((np.diff(z[top - 8:top + 1]) >= -1e-6).all(), f"no dip before the top: {z[top - 8:top + 1]}")
        self.assertLess(abs(np.diff(z[top - 3:top + 4], 2)).max(), 0.4, "rounded over the embankment's top (1.06 before)")

    def test_water_clearance(self):
        import roads
        self.assertEqual(roads.water_clear(10.0), roads.WATER_CLEAR_MIN)
        self.assertAlmostEqual(roads.water_clear(250.0), 37.5)
        self.assertEqual(roads.water_clear(5000.0), roads.WATER_CAUSEWAY_CLEAR, "a causeway")


class BridgeProfilesTest(unittest.TestCase):
    def test_chain_across_cells_and_clearance(self):
        import mapcells
        import roads
        from test_roads import features
        # a bridge in two parts (two features) over a street at x = 0; the ground is flat at 10 m
        f = features([[(-300, 0), (0, 0)], [(0, 0), (300, 0)], [(0, -200), (0, 200)]],
                     ["primary", "primary", "residential"],
                     levels=[(0, 1.0, 0.5, 1.0), (1, 1.0, 0.0, 0.5)], ids=["a", "b", "street"])
        calls = []

        def cell_fn(key):
            calls.append(str(key))
            return f
        flat = lambda la, lo: np.full(np.shape(la), 10.0)
        bp = roads.BridgeProfiles(FRAME, cell_fn, flat)
        got1 = {fid: (xy, z) for fid, spec, xy, z, lon in bp.parts([f], (-256, -128, 0, 128))}
        self.assertEqual(set(got1), {"a", "b"}, "both halves of the chain")
        za = got1["a"][1]
        self.assertAlmostEqual(za[0], 10.0, delta=0.2, msg="an abutment on the ground")
        k = int(np.argmin(np.abs(got1["a"][0][:, 0] - 0.0)))
        self.assertGreaterEqual(za[k], 10.0 + roads.DECK_ROAD_CLEAR - 0.6, "headroom over the street")
        bp2 = roads.BridgeProfiles(FRAME, cell_fn, flat)        # another tile asking first: same deck
        got2 = {fid: (xy, z) for fid, spec, xy, z, lon in bp2.parts([f], (0, -128, 256, 128))}
        self.assertTrue(np.allclose(got2["a"][1], got1["a"][1]) and np.allclose(got2["b"][1], got1["b"][1]))
        ea, eb = got1["a"], got1["b"]
        self.assertAlmostEqual(float(ea[1][-1]), float(eb[1][0]), places=6, msg="the halves meet")

    def test_crossing_decks_stack_by_level(self):
        import roads
        from test_roads import features
        # two bridges crossing at (0, 0): level 1 east-west, level 2 north-south; ground flat at 10 m
        f = features([[(-300, 0), (300, 0)], [(0, -300), (0, 300)]], ["primary", "motorway"],
                     levels=[(0, 1.0, 0.2, 0.8), (1, 2.0, 0.2, 0.8)], ids=["low", "high"])
        flat = lambda la, lo: np.full(np.shape(la), 10.0)
        bp = roads.BridgeProfiles(FRAME, lambda key: f, flat)
        got = {fid: (xy, z) for fid, spec, xy, z, lon in bp.parts([f], (-128, -128, 128, 128))}
        lo_xy, lo_z = got["low"]
        hi_xy, hi_z = got["high"]
        zl = lo_z[int(np.argmin(np.abs(lo_xy[:, 0])))]
        zh = hi_z[int(np.argmin(np.abs(hi_xy[:, 1])))]
        self.assertGreaterEqual(zh - zl, roads.DECK_ROAD_CLEAR - 0.6, (zl, zh))

    def test_short_bridge_no_wall_and_splits_merge(self):
        import roads
        flat = lambda xy: np.full(len(xy), 10.0)
        # a 60 m bridge over a road at its middle: capped at 6 % from each abutment, not 6.5 m high
        ln = np.c_[np.arange(0.0, 61.0, 4.0), np.zeros(16)]
        (z,) = roads.deck_profile([ln], flat, [(0, 30.0, 10.0)])
        s = ln[:, 0]
        self.assertLessEqual(np.abs(np.diff(z) / np.diff(s)).max(), roads.DECK_MAX_GRADE + 0.02)
        self.assertLess(z.max(), 10.0 + 0.06 * 30 + 0.1)
        # a ramp starting in the intersection it climbs from (Queens Plaza, report #55): flush with the cross street
        (z,) = roads.deck_profile([ln], flat, [(0, 6.0, 10.0), (0, 30.0, 10.0)])
        self.assertLess(float(np.interp(6.0, s, z)), 10.0 + 0.15, "no hump across the cross street")
        self.assertGreater(z.max(), 10.0 + 0.06 * 18 - 0.3, "the crossing farther on still lifts the deck")
        # abutments 3.5 m apart in height over 44 m (8 %), the roads meeting them listed as crossings at its ends: both
        # ends keep their ground, no step onto the deck (the I-20 bridge near Temple, GA, its approaches raised)
        ln3 = np.c_[np.arange(0.0, 44.1, 4.0), np.zeros(12)]
        slope = lambda xy: 10.0 + 3.5 * (np.asarray(xy)[:, 0] > 22.0)
        (z3,) = roads.deck_profile([ln3], slope, [(0, 0.0, 10.0), (0, 20.0, 5.0), (0, 44.0, 13.5)])
        self.assertAlmostEqual(float(z3[0]), 10.0, places=6)
        self.assertAlmostEqual(float(z3[-1]), 13.5, places=6)
        # no sag between two crossings 120 m apart (the Queensboro Bridge over Roosevelt Island)
        ln2 = np.c_[np.arange(0.0, 401.0, 4.0), np.zeros(101)]
        (z2,) = roads.deck_profile([ln2], flat, [(0, 140.0, 10.0, 20.0), (0, 260.0, 10.0, 20.0)])
        mid = float(np.interp(200.0, ln2[:, 0], z2))
        self.assertGreater(mid, min(float(np.interp(140.0, ln2[:, 0], z2)), float(np.interp(260.0, ln2[:, 0], z2))) - 0.5)
        # a mainline at 18 m with a ramp leaving it at a shallow angle: heights match where they overlap
        main = np.c_[np.arange(0.0, 201.0, 4.0), np.zeros(51)]
        ramp = np.c_[np.arange(100.0, 301.0, 4.0), (np.arange(100.0, 301.0, 4.0) - 100.0) * 0.12]
        zs = [np.full(len(main), 18.0), np.linspace(18.0, 10.0, len(ramp))]
        out = roads._merge_overlaps([main, ramp], zs, [12.0, 6.0], [1.0, 1.0])
        k = 5                                   # ramp point 20 m from the split, 2.4 m off the mainline
        j = int(np.argmin(np.abs(main[:, 0] - ramp[k, 0])))
        self.assertLess(abs(out[1][k] - out[0][j]), abs(zs[1][k] - zs[0][j]) / 2 + 1e-9)
