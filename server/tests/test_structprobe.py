"""structprobe.py's metrics on small synthetic cases (the road revamp's structures track, step S0): two overlapping
decks (overlap and hole), a parapet across a carriageway, a square deck end over a 50 degree crossing, twins of one
level and an overpass (the grouping prediction)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import structprobe as sp  # noqa: E402


def ribbon(a, b, w, z):
    """A straight deck from a to b (2,), width w, height z: (left, right, z) rows every 2 m."""
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    L = float(np.sqrt(((b - a) * (b - a)).sum()))
    n = max(2, int(round(L / 2.0)) + 1)
    t = np.linspace(0.0, 1.0, n)[:, None]
    mid = a + t * (b - a)
    u = (b - a) / L
    left = mid + np.array([-u[1], u[0]]) * (w / 2)
    right = mid - np.array([-u[1], u[0]]) * (w / 2)
    return left, right, np.full(n, float(z))


def top_tris(left, right, z):
    """The drawn top of a ribbon as deck() draws it: two triangles per row pair."""
    L = np.c_[left, z]
    R = np.c_[right, z]
    return np.concatenate([np.stack([L[:-1], R[:-1], R[1:]], 1), np.stack([L[:-1], R[1:], L[1:]], 1)])


def member(key, a, b, w, z, cls="primary"):
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    n = 21
    t = np.linspace(0.0, 1.0, n)[:, None]
    return {"key": key, "fid": key, "cls": cls, "xy": a + t * (b - a), "z": np.full(n, float(z)), "half": w / 2,
            "walk": False}


class Raster(unittest.TestCase):
    def test_square(self):
        # a 10 m x 4 m rectangle: 40 x 16 raster points, each 0.0625 m^2
        tri = np.array([[[0, 0, 1], [10, 0, 1], [10, 4, 1]], [[0, 0, 1], [10, 4, 1], [0, 4, 1]]], np.float64)
        k, z, _ = sp.raster_tris(tri)
        self.assertEqual(len(k), 640)
        self.assertEqual(len(np.unique(k)), 640)       # (no point on the shared diagonal counted twice)
        self.assertTrue(np.allclose(z, 1.0))

    def test_shared_edge_on_raster_points(self):
        # a square whose corners and diagonal lie on the raster's points: each point inside once (the top-left rule),
        # no false overlap between the two triangles of one slab
        x0, y0 = 0.5 * sp.RASTER_M + sp._JX, 0.5 * sp.RASTER_M + sp._JY
        s = 8 * sp.RASTER_M
        a, b, c, d = (x0, y0), (x0 + s, y0), (x0 + s, y0 + s), (x0, y0 + s)
        for tris in ([[a, b, c], [a, c, d]], [[a, c, b], [a, d, c]]):        # (both windings)
            tri = np.array([[p + (2.0,) for p in t] for t in tris], np.float64)
            k, _z, _ = sp.raster_tris(tri)
            self.assertEqual(len(k), len(np.unique(k)))
            self.assertEqual(len(k), 64)                # (8 x 8: two of the four sides own their points)
            self.assertEqual(len(sp.overlap_keys(k, _z)), 0)


class OverlapHole(unittest.TestCase):
    def test_two_overlapping_decks(self):
        # deck A 40 m along x, 10 m wide (y -5..5); deck B beside it 5 m over (y 0..10), 10 cm higher: one level,
        # 40 m x 5 m drawn twice
        a = ribbon((0, 0), (40, 0), 10.0, 10.0)
        b = ribbon((0, 5), (40, 5), 10.0, 10.1)
        tops = np.concatenate([top_tris(*a), top_tris(*b)])
        ideal = sp.ideal_tris([a, b])
        res = sp.overlap_hole(tops, ideal, R=1000.0)
        self.assertAlmostEqual(res["overlap_m2"], 200.0, delta=1.0)
        self.assertEqual(res["hole_m2"], 0.0)
        # stacked 6 m apart: two levels, no overlap
        c = ribbon((0, 5), (40, 5), 10.0, 16.0)
        res = sp.overlap_hole(np.concatenate([top_tris(*a), top_tris(*c)]), sp.ideal_tris([a, c]), R=1000.0)
        self.assertEqual(res["overlap_m2"], 0.0)

    def test_hole(self):
        # A's top drawn only to x = 30: the ideal footprint to 39 (its free end's last metre left out), less what B
        # covers at its height (y 0..5): 9 m x 5 m
        a = ribbon((0, 0), (40, 0), 10.0, 10.0)
        a_drawn = ribbon((0, 0), (30, 0), 10.0, 10.0)
        b = ribbon((0, 5), (40, 5), 10.0, 10.1)
        tops = np.concatenate([top_tris(*a_drawn), top_tris(*b)])
        res = sp.overlap_hole(tops, sp.ideal_tris([a, b]), R=1000.0)
        self.assertAlmostEqual(res["hole_m2"], 45.0, delta=1.0)
        # B 2 m higher does not cover A's hole: 9 m x 10 m
        b2 = ribbon((0, 5), (40, 5), 10.0, 12.0)
        res = sp.overlap_hole(np.concatenate([top_tris(*a_drawn), top_tris(*b2)]), sp.ideal_tris([a, b2]), R=1000.0)
        self.assertAlmostEqual(res["hole_m2"], 90.0, delta=1.0)

    def test_joint_end_not_excluded(self):
        # two decks end to end at x = 20: the joint is no free end, a gap drawn there is a hole
        a = ribbon((0, 0), (20, 0), 10.0, 5.0)
        b = ribbon((20, 0), (40, 0), 10.0, 5.0)
        a_short = ribbon((0, 0), (19, 0), 10.0, 5.0)
        res = sp.overlap_hole(np.concatenate([top_tris(*a_short), top_tris(*b)]), sp.ideal_tris([a, b]), R=1000.0)
        self.assertAlmostEqual(res["hole_m2"], 10.0, delta=0.5)


class Parapet(unittest.TestCase):
    def test_parapet_across_carriageway(self):
        road = (np.array([[-20.0, 50.0], [20.0, 50.0]]), np.zeros(2), 5.0, ("ground", "road"))
        across = (np.array([[0.0, 40.0], [0.0, 60.0]]), np.full(2, 0.5))          # at its height: 9.5 m in it
        over = (np.array([[5.0, 40.0], [5.0, 60.0]]), np.full(2, 6.0))            # an overpass's: not in it
        beside = (np.array([[-20.0, 55.5], [20.0, 55.5]]), np.full(2, 0.3))      # along its edge, outside
        res = sp.parapet_in_carriageway([across, over, beside], [road], R=1000.0)
        self.assertAlmostEqual(res["m"], 9.5, delta=0.3)
        self.assertEqual(res["ground_m"], res["m"])
        self.assertEqual(len(res["worst"]), 1)


class Skew(unittest.TestCase):
    def test_square_end_over_50_degree_crossing(self):
        # a deck along y ending at (0, 30), its end square (the end line along x); a road crossing under it 10 m from
        # the end at 50 degrees to the deck: the end line is 40 degrees off the road
        m = member("deck", (0.0, -30.0), (0.0, 30.0), 10.0, 6.0)
        a50 = np.radians(50.0)
        d = np.array([float(np.sin(a50)), float(np.cos(a50))])   # det-ok: a test's input
        road = np.array([[0.0, 20.0] - 40 * d, [0.0, 20.0] + 40 * d])
        res = sp.skew_errors([(0, 1, np.array([1.0, 0.0])), (0, 0, np.array([1.0, 0.0]))], [m],
                             [(road, "road", "r1")], R=1000.0)
        self.assertEqual(res["ends"], 1)                # (the start is 50 m from the crossing: not its end)
        self.assertAlmostEqual(res["max"], 40.0, delta=0.05)
        self.assertAlmostEqual(res["worst"][0][1], 50.0, delta=0.05)
        self.assertEqual(res["n_over"], 1)
        # the end line laid along the road: no error
        res = sp.skew_errors([(0, 1, d)], [m], [(road, "road", "r1")], R=1000.0)
        self.assertAlmostEqual(res["max"], 0.0, delta=0.05)
        # its own road going on from the end is not crossed
        res = sp.skew_errors([(0, 1, np.array([1.0, 0.0]))], [m], [(road, "road", "deck")], R=1000.0)
        self.assertEqual(res["ends"], 0)


class Groups(unittest.TestCase):
    def test_twins_overlap_and_overpass(self):
        ms = [member("a", (0, 0), (100, 0), 10.0, 8.0),           # twins of one level, 2 m apart, 3 cm off
              member("b", (0, 12), (100, 12), 10.0, 8.03),
              member("c", (50, -50), (50, 50), 8.0, 15.0),        # an overpass over both: never one structure
              member("d", (100, 0), (160, 10), 10.0, 8.1),        # going on from a (JOIN)
              member("e", (10, -7), (60, -7), 10.0, 8.2)]         # overlapping a by 3 m along 50 m
        res = sp.twin_dz(ms, R=1000.0)
        self.assertEqual(res["pairs"], 1)
        self.assertAlmostEqual(res["max"], 0.03, delta=1e-6)
        g = sp.predict_groups([dict(m) for m in ms], R=1000.0)
        self.assertEqual(g["n"], 1)
        self.assertEqual(sorted(g["list"][0]["members"]), ["a", "b", "d", "e"])
        kinds = {e[0] for e in g["list"][0]["edges"]}
        self.assertEqual(kinds, {"BESIDE", "JOIN", "OVERLAP_ALONG"})
        self.assertGreaterEqual(g["height_rejected"]["OVERLAP_CROSS"], 1)
        self.assertEqual(g["single"], 1)                # (the overpass)

    def test_outline_holds_both(self):
        # two decks 4 m apart edge to edge (more than BESIDE's 2.5 m): one only when one bridge outline holds both
        ms = [member("n", (0, 0), (60, 0), 8.0, 4.0), member("s", (0, 12), (60, 12), 8.0, 4.01)]
        both = [np.array([[-5, -6], [65, -6], [65, 18], [-5, 18]], np.float64)]
        one = [np.array([[-5, -6], [65, -6], [65, 6], [-5, 6]], np.float64)]
        self.assertEqual(sp.predict_groups([dict(m) for m in ms], both, R=1000.0)["edges"]["BESIDE_OUTLINE"], 1)
        g = sp.predict_groups([dict(m) for m in ms], one, R=1000.0)
        self.assertEqual(g["n"], 0)
        self.assertEqual(g["height_rejected"]["BESIDE_OUTLINE"], 0)

    def test_outline_order(self):
        # the decks apart in height over their first 30 m, together over the last 30: an outline round each half;
        # the pair is one structure whichever outline comes first
        n = member("n", (0, 0), (60, 0), 8.0, 4.0)
        s_ = member("s", (0, 12), (60, 12), 8.0, 4.0)
        s_["z"] = np.where(s_["xy"][:, 0] < 30.0, 6.0, 4.01)
        first = np.array([[-5, -6], [29, -6], [29, 18], [-5, 18]], np.float64)
        last = np.array([[31, -6], [65, -6], [65, 18], [31, 18]], np.float64)
        for outlines in ([first, last], [last, first]):
            g = sp.predict_groups([dict(n), dict(s_)], outlines, R=1000.0)
            self.assertEqual(g["edges"]["BESIDE_OUTLINE"], 1)

    def test_short_twins(self):
        # two carriageways on one 17 m deck (E6 Vist), 1 m apart: BESIDE though they never run 20 m side by side
        ms = [member("n", (0, 0), (17, 0), 8.0, 4.0), member("s", (0, 9), (17, 9), 8.0, 4.01)]
        g = sp.predict_groups([dict(m) for m in ms], R=1000.0)
        self.assertEqual(g["n"], 1)
        self.assertEqual({e[0] for e in g["list"][0]["edges"]}, {"BESIDE"})
        self.assertEqual(sp.twin_dz(ms, R=1000.0)["pairs"], 1)
        # a 6 m piece beside a long deck joins it too (0.8 x 6 m beside it is enough)
        ms = [member("n", (0, 0), (100, 0), 8.0, 4.0), member("s", (40, 9), (46, 9), 8.0, 4.01)]
        self.assertEqual(sp.predict_groups([dict(m) for m in ms], R=1000.0)["edges"]["BESIDE"], 1)


class Seams(unittest.TestCase):
    def test_step(self):
        a = ribbon((0, 0), (20, 0), 10.0, 5.0)
        b = ribbon((20, 0), (40, 0), 10.4, 5.0)        # 0.2 m wider each side: a 0.2 m step in its outline
        res = sp.seam_steps([a + ("a",), b + ("b",)], R=1000.0)
        self.assertEqual(res["joints"], 1)
        self.assertEqual(res["n_over"], 1)
        self.assertAlmostEqual(res["side_max"], 0.2, delta=1e-6)


if __name__ == "__main__":
    unittest.main()
