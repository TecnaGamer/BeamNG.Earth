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
        self.assertTrue(set(np.unique(UV[:, 0])) <= {0.0, 1.0}, "u across the width")

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

    def test_drawn_ground_is_the_road_surfaces_ground(self):
        # the sidewalks' curbs on the ground the road surfaces lie on (the user, 2026-10-21): drawn_ground at every
        # point follow_ground (the kernel the road mesh uses) cuts the triangles into is the height it gives them,
        # on the cells' edges, diagonals and corners too
        rng = np.random.default_rng(7)
        P = np.r_[rng.uniform(-30, 30, (80, 2)), rng.integers(-15, 16, (20, 2)) * 2.0]
        T = np.array([rng.choice(len(P), 3, replace=False) for _ in range(60)], np.int64)
        grid, i0, j0 = roadmesh._height_grid(FRAME, self.bumpy(), P, 1 / 32)
        fP, _, fT, _ = roadmesh.follow_ground(P, np.zeros((len(P), 2)), T, grid, i0, j0)
        self.assertGreater(len(fP), len(P), "cut along the ground's triangles")
        z = roadmesh.drawn_ground(FRAME, self.bumpy(), fP[:, :2], 1 / 32)
        self.assertTrue(np.isfinite(z).all())
        np.testing.assert_allclose(z, fP[:, 2], rtol=0, atol=1e-6)

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


    def test_no_climbing_a_step(self):
        """A strip beside a cutting's wall: in the lattice cells holding the step it stays at the road's level instead
        of climbing the lattice's teeth (FOLLOW_STEP_M / FOLLOW_MAX_UP), elsewhere it follows the ground."""
        grid = np.zeros((10, 10))
        grid[:, 5:] = 4.0                                   # a 4 m step between x = 8 and x = 10
        grid[:, :5] += np.arange(5) * 0.1                   # a gentle slope before it (followed)
        P = np.array([[1.0, 1.0], [9.5, 1.0], [9.5, 15.0], [1.0, 15.0]])
        T = np.array([[0, 1, 2], [0, 2, 3]])
        UV = np.zeros((4, 2))
        Q, _, U, _ = roadmesh.follow_ground(P, UV, T, grid, 0, 0)
        step = Q[:, 0] > 8.0
        self.assertTrue(step.any())
        self.assertLessEqual(Q[step, 2].max(), 0.4 + roadmesh.FOLLOW_MAX_UP + 1e-9, "held down at the step")
        flat = Q[:, 0] < 7.9
        self.assertGreater(Q[flat, 2].max(), 0.25, "the slope before it followed")


class TrimOverlapsTest(unittest.TestCase):
    """A ramp leaving its mainline lower, their decks overlapping in plan: each ends halfway between their centre
    lines (US-101's exit 1E over the Los Angeles River: the mainline's side stood in the ramp's lanes)."""

    def ribbon(self, y, half, z, fid):
        x = np.arange(0.0, 50.0, 5.0)
        mid = np.c_[x, np.full(len(x), y)]
        left, right = mid + [0, half], mid - [0, half]
        return ((left, right, x / 10, "m", "base", np.full(len(x), z)), {"fid": fid})

    def test_halfway_between_decks_at_another_height(self):
        main = self.ribbon(10.0, 9.4, 95.0, "main")
        ramp = self.ribbon(0.0, 5.8, 92.6, "ramp")
        (m, _), (r, _) = roadmesh.trim_overlaps([main, ramp])
        np.testing.assert_allclose(m[1][:, 1], 5.0 + roadmesh.TRIM_GAP_M)     # the mainline's right edge
        np.testing.assert_allclose(r[0][:, 1], 5.0 - roadmesh.TRIM_GAP_M)     # the ramp's left edge
        np.testing.assert_allclose(m[0][:, 1], 19.4)                            # the far edges as they were
        np.testing.assert_allclose(r[1][:, 1], -5.8)

    def test_one_surface_or_stacked_kept(self):
        for dz in (0.1, 6.0):            # (one surface at a split; an upper roadway over a lower one)
            main = self.ribbon(10.0, 9.4, 95.0, "main")
            ramp = self.ribbon(0.0, 5.8, 95.0 - dz, "ramp")
            (m, _), (r, _) = roadmesh.trim_overlaps([main, ramp])
            np.testing.assert_allclose(m[1], main[0][1])
            np.testing.assert_allclose(r[0], ramp[0][0])


if __name__ == "__main__":
    unittest.main()


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
        self.assertEqual(mats, [roadmesh.DECK_SIDE, roadmesh.DECK_TOP], "an opaque slab, a plain asphalt top")
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

    def test_no_parapets_across_a_median(self):
        """A divided road's two decks a narrow median apart (3 m): no parapets facing each other across it (two walls and
        a dark gap stood in the median), the slabs meet in its middle; the outer edges keep theirs."""
        P = roadmesh.deck([self.deck(0.0, "a"), self.deck(11.0, "b")], (0.0, 0.0, 0.0))[0]
        ys = np.unique(np.round(self.parapet_tops(P)[:, 1], 2))
        self.assertTrue(ys.min() < -4.0 and ys.max() > 15.0, "the outer edges have one")
        self.assertFalse(((ys > 0.5) & (ys < 10.5)).any(), "none in the median: %s" % ys)
        top = P[np.abs(P[:, 2] - 20.0) < 1e-6]
        self.assertTrue(((top[:, 1] > 5.3) & (top[:, 1] < 5.7)).any(), "the slabs reach the median's middle")

    def test_parapets_beside_a_deck_well_apart(self):
        P = roadmesh.deck([self.deck(0.0, "a"), self.deck(20.0, "b")], (0.0, 0.0, 0.0))[0]
        ys = np.unique(np.round(self.parapet_tops(P)[:, 1], 2))
        self.assertTrue(((ys > 4.0) & (ys < 5.0)).any() and ((ys > 15.0) & (ys < 16.0)).any(),
                        "12 m apart: each keeps its own: %s" % ys)

    def test_parapet_where_a_deck_runs_under(self):
        P = roadmesh.deck([self.deck(0.0, "a"), self.deck(8.0, "b", z=12.0)], (0.0, 0.0, 0.0))[0]
        ys = np.unique(np.round(self.parapet_tops(P)[:, 1], 2))
        self.assertTrue(((ys > 3.0) & (ys < 5.0)).any(), "over a lower deck the edge is free")

    def test_no_parapet_across_a_road_at_grade(self):
        """GitHub #96: a ramp's deck at grade over a road stood its parapets across the road's lanes; over a road well
        under it the parapets stay."""
        road = lambda z: {"m": "road_asphalt_2lane", "w": 10.0, "tl": 11.0, "p": 10,
                          "n": [[25.0, -30.0, z], [25.0, 30.0, z]]}
        def tops_over_road(z_road):
            P = roadmesh.deck([self.deck(0.0, "a"), road(z_road)], (0.0, 0.0, 0.0))[0]
            t = self.parapet_tops(P)
            return ((t[:, 0] > 21.0) & (t[:, 0] < 29.0)).sum()
        self.assertEqual(tops_over_road(19.5), 0, "at grade: no wall across the road")
        self.assertGreater(tops_over_road(12.0), 0, "a road 8 m below: the parapets stay")
        # the roads painted into the ground (no ground chunks drawn): the carriageway comes as ground= (GitHub #258)
        P = roadmesh.deck([self.deck(0.0, "a")], (0.0, 0.0, 0.0), ground=[road(19.5)])[0]
        t = self.parapet_tops(P)
        self.assertEqual(((t[:, 0] > 21.0) & (t[:, 0] < 29.0)).sum(), 0, "ground=: no wall across the road either")

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

    def test_ramp_of_a_higher_level_starts_at_the_fork(self):
        # I-83, Baltimore (2026-10-21): a ramp (level 2) leaving the mainline (level 1) overlaps it from the fork on; it
        # was lifted to clear it there and started 2.2 m over the fork, a ledge across the lanes
        import roads
        flat = lambda q: np.full(len(q), 2.0)      # noqa: E731
        main_in = np.c_[np.zeros(51), np.arange(-200.0, 0.1, 4.0)]
        main_on = np.c_[np.zeros(51), np.arange(0.0, 200.1, 4.0)]
        t = np.arange(0.0, 200.1, 4.0)
        ramp = np.c_[0.0004 * t ** 2, t]            # diverging slowly: 4 m apart after 100 m
        zs = roads.deck_profile([main_in, main_on, ramp], flat, [(0, 100.0, 2.0)], levels=[1.0, 1.0, 2.0],
                                widths=[11.0, 11.0, 6.0])
        self.assertLess(abs(zs[2][0] - zs[0][-1]), 0.3, (zs[0][-1], zs[1][0], zs[2][0]))
        self.assertLess(abs(zs[1][0] - zs[0][-1]), 0.3)

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
