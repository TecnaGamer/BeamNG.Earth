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
from test_roads import FRAME  # noqa: E402


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
        # a 60 m bridge over a road at its middle: capped at DECK_MAX_GRADE from each abutment, not DECK_ROAD_CLEAR high
        ln = np.c_[np.arange(0.0, 61.0, 4.0), np.zeros(16)]
        (z,) = roads.deck_profile([ln], flat, [(0, 30.0, 10.0)])
        s = ln[:, 0]
        self.assertLessEqual(np.abs(np.diff(z) / np.diff(s)).max(), roads.DECK_MAX_GRADE + 0.02)
        self.assertLess(z.max(), 10.0 + roads.DECK_MAX_GRADE * 30 + 0.1)
        # a ramp starting in the intersection it climbs from (Queens Plaza, report #55): flush with the cross street
        (z,) = roads.deck_profile([ln], flat, [(0, 6.0, 10.0), (0, 30.0, 10.0)])
        self.assertLess(float(np.interp(6.0, s, z)), 10.0 + roads.DECK_MAX_GRADE * 2.5, "no hump across the cross street")
        self.assertGreater(z.max(), 10.0 + 0.06 * 18 - 0.3, "the crossing farther on still lifts the deck")
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
