"""bridgerailings (BNE_BRIDGE_RAILINGS): which decks take the railing, and the railing's mesh."""
import os
import sys
import unittest
import unittest.mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bridgerailings as br  # noqa: E402
import roadmesh  # noqa: E402


def _run(deck, x0=0.0, x1=40.0, y=5.0):
    edge = np.column_stack((np.linspace(x0, x1, 9), np.full(9, y)))
    return {"deck": deck, "edge": edge, "out": np.tile([0.0, 1.0], (9, 1)), "z": np.full(9, 12.0),
            "v": np.linspace(0, 4, 9), "mid": edge, "ends": []}


def _de(_frame, p):
    return "DE" if p[0] < 3000 else "FR"


class Decks(unittest.TestCase):
    def test_off_by_default(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BNE_BRIDGE_RAILINGS", None)
            self.assertEqual(br.railing_decks(None, [_run(0)], country_fn=_de), {})
            self.assertEqual(br.key(), ())

    def test_country_and_mapped_rails(self):
        with unittest.mock.patch.dict(os.environ, {"BNE_BRIDGE_RAILINGS": "on"}):
            runs = [_run(0), _run(1, 2000.0, 2040.0), _run(-1, 4000.0, 4040.0)]
            # deck 1 has a mapped guard rail (the map's rail wins); deck -1 (a road-system face) lies in France
            self.assertEqual(br.railing_decks(None, runs, mapped={1}, country_fn=_de), {0: False})
            self.assertEqual(br.key(), (br.VERSION,))


class _Spec:
    def __init__(self, urban):
        self.urban, self.width = urban, 6.0


class Town(unittest.TestCase):
    def test_town_from_the_approach_not_the_street_under(self):
        edge = _run(0)["edge"]                  # along x at y 5, x 0..40
        approach = ("w1", _Spec(True), np.array([[-60.0, 0.0], [-2.0, 0.0]]))       # the same road, in town
        under = ("w2", _Spec(False), np.array([[20.0, -30.0], [20.0, 30.0]]))      # a rural road crossing under
        self.assertTrue(br.town_of(edge, [approach, under]))
        self.assertFalse(br.town_of(edge, [under]), "a crossing road is not the bridge's own")
        self.assertFalse(br.town_of(edge, []), "none near: rural, the guard rail stays")
        far = ("w3", _Spec(True), np.array([[-200.0, 0.0], [-150.0, 0.0]]))
        self.assertFalse(br.town_of(edge, [far]))

    def test_railing_decks_carry_town(self):
        with unittest.mock.patch.dict(os.environ, {"BNE_BRIDGE_RAILINGS": "on"}):
            approach = ("w1", _Spec(True), np.array([[-60.0, 0.0], [-2.0, 0.0]]))
            self.assertEqual(br.railing_decks(None, [_run(0)], country_fn=_de, roads=[approach]), {0: True})

    def test_town_edge_has_no_guard_rail(self):
        run = _run(0)
        (cp, _ct, _), (sp, _st, _) = br.mesh(run["edge"], run["out"], run["z"], town=True)
        road_y = 5.0 - br.SLAB_W_M
        self.assertAlmostEqual(cp[:, 1].max(), road_y + br.TOWN_CAP_W_M, places=6)
        self.assertAlmostEqual(sp[:, 1].min(), road_y + br.TOWN_RAILING_AT_M - br.HANDRAIL_W_M / 2, places=6)
        _cap, (rural, _rt, _ru) = br.mesh(run["edge"], run["out"], run["z"])
        self.assertLess(len(sp), len(rural))


class Mesh(unittest.TestCase):
    def _normals(self, P, T):
        n = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
        return n / np.maximum(np.linalg.norm(n, axis=1), 1e-12)[:, None]

    def test_sweep_box_is_closed_and_looks_out(self):
        line = np.array([[0.0, 0.0], [5.0, 0.0], [10.0, 1.0]])
        P, T, _uv = br.sweep(line, np.tile([0.0, 1.0], (3, 1)), np.zeros(3), *br._box(0.0, 1.0, 0.0, 2.0))
        # closed: every edge shared by exactly two triangles (positions welded)
        key = {tuple(np.round(p, 9)): n for n, p in reversed(list(enumerate(P)))}
        W = np.array([[key[tuple(np.round(P[v], 9))] for v in t] for t in T])
        edges = np.sort(np.concatenate([W[:, [0, 1]], W[:, [1, 2]], W[:, [2, 0]]]), axis=1)
        _u, counts = np.unique(edges, axis=0, return_counts=True)
        self.assertTrue((counts == 2).all())
        # every face looks away from the box's middle
        n = self._normals(P, T)
        cen = P[T].mean(axis=1)
        axis_pt = np.column_stack((cen[:, 0], np.interp(cen[:, 0], line[:, 0], line[:, 1]) + 0.5, np.full(len(cen), 1.0)))
        side = np.abs(n[:, 0]) < 0.9            # (not the end caps)
        self.assertTrue(((n[side] * (cen[side] - axis_pt[side]))[:, 1:].sum(axis=1) > 0).all())

    def test_edge_shape(self):
        run = _run(0)
        (cp, ct, _cu), (sp, st, _su) = br.mesh(run["edge"], run["out"], run["z"])
        road_y = 5.0 - br.SLAB_W_M
        # the cap: from the road's edge CAP_W_M out, CAP_H_M high, its outer face CORNICE_D_M down
        self.assertAlmostEqual(cp[:, 1].min(), road_y, places=6)
        self.assertAlmostEqual(cp[:, 1].max(), road_y + br.CAP_W_M, places=6)
        self.assertAlmostEqual(cp[:, 2].max(), 12.0 + br.CAP_H_M, places=6)
        self.assertAlmostEqual(cp[:, 2].min(), 12.0 - br.CORNICE_D_M, places=6)
        # the steel stands on the cap, wholly within it and within the run's length
        self.assertTrue(np.isfinite(sp).all() and st.max() < len(sp))
        self.assertAlmostEqual(sp[:, 2].min(), 12.0 + br.CAP_H_M, places=6)
        self.assertAlmostEqual(sp[:, 2].max(), 12.0 + br.CAP_H_M + br.RAILING_H_M, places=6)
        self.assertGreaterEqual(sp[:, 1].min(), road_y + br.RAIL_FACE_M - 1e-6)
        self.assertLessEqual(sp[:, 1].max(), road_y + br.CAP_W_M)
        self.assertGreaterEqual(sp[:, 0].min(), -1e-6)
        self.assertLessEqual(sp[:, 0].max(), 40.0 + 1e-6)
        # the guard rail's face toward the road, at the beam's height
        face = sp[np.isclose(sp[:, 1], road_y + br.RAIL_FACE_M)]
        self.assertTrue(len(face))
        self.assertAlmostEqual(face[:, 2].min(), 12.0 + br.CAP_H_M + (br.RAIL_LOW_M + br.RAIL_HIGH_M) / 2
                               - (br.RAIL_HIGH_M - br.RAIL_LOW_M) / 4, places=6)
        # the railing: posts every 2.5 m or less (17 over 40 m) and a handrail at its height
        posts = sp[np.isclose(sp[:, 1], road_y + br.RAILING_AT_M - br.POST_M / 2) & np.isclose(sp[:, 2], 12.0 + br.CAP_H_M)]
        self.assertEqual(len(np.unique(np.round(posts[:, 0], 3))), 2 * 17)

    def test_faces_look_out(self):
        run = _run(0)
        (cp, ct, _), (sp, st, _) = br.mesh(run["edge"], run["out"], run["z"])
        for P, T in ((cp, ct), (sp, st)):
            n = self._normals(P, T)
            top = np.isclose(P[T][:, :, 2], P[:, 2].max()).all(axis=1)
            self.assertTrue(top.any())
            self.assertTrue((n[top][:, 2] > 0.99).all(), "the highest faces look up")
        n = self._normals(cp, ct)
        inner = np.isclose(cp[ct][:, :, 1], 5.0 - br.SLAB_W_M).all(axis=1)     # the cap's curb looks at the road
        self.assertTrue(inner.any())
        self.assertTrue((n[inner][:, 1] < -0.99).all())


class Deck(unittest.TestCase):
    CHUNKS = [
        {"m": "road_asphalt_2lane", "w": 8.0, "tl": 11.0, "p": 20, "deck": True, "fixed": True, "fid": "railed",
         "n": [[0.0, 0.0, 20.0], [50.0, 0.0, 20.0]]},
        {"m": "road_asphalt_2lane", "w": 8.0, "tl": 11.0, "p": 30, "deck": True, "fixed": True, "fid": "plain",
         "n": [[100.0, 0.0, 30.0], [150.0, 0.0, 30.0]]},
    ]

    def test_curb_and_railing_in_the_deck_mesh(self):
        """Deck 0 in railing_decks: the cap instead of its parapet, the rail and railing on it; deck 1 unchanged."""
        P, T, UV, mats, M = roadmesh.deck(self.CHUNKS, (0.0, 0.0, 0.0), railing_decks={0: False})
        self.assertIn(br.STEEL, mats)
        side = mats.index(roadmesh.DECK_SIDE)
        concrete = P[np.unique(T[M == side])]
        railed = concrete[(concrete[:, 0] >= 0.0) & (concrete[:, 0] <= 50.0)]
        plain = concrete[(concrete[:, 0] >= 100.0) & (concrete[:, 0] <= 150.0)]
        self.assertAlmostEqual(float(railed[:, 2].max() - 20.0), br.CAP_H_M, places=6)
        self.assertAlmostEqual(float(plain[:, 2].max() - 30.0), roadmesh.PARAPET_H, places=6)
        steel = P[np.unique(T[M == mats.index(br.STEEL)])]
        self.assertTrue((steel[:, 0] <= 50.0 + 1e-6).all(), "no railing on the deck that is not listed")
        self.assertAlmostEqual(float(steel[:, 2].max() - 20.0), br.CAP_H_M + br.RAILING_H_M, places=6)
        # the cap, rail and railing on both free edges
        ys = np.sort(np.unique(np.round(steel[:, 1], 2)))
        self.assertTrue(ys.min() < 0 < ys.max())

    def test_nothing_listed_nothing_changed(self):
        a = roadmesh.deck(self.CHUNKS, (0.0, 0.0, 0.0))
        b = roadmesh.deck(self.CHUNKS, (0.0, 0.0, 0.0), railing_decks=())
        for x, y in zip(a, b):
            if isinstance(x, np.ndarray):
                self.assertTrue(np.array_equal(x, y))
            else:
                self.assertEqual(x, y)


if __name__ == "__main__":
    unittest.main()
