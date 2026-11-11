"""Railway catenary (catenary.py): masts along electrified lines, the wires between them."""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catenary  # noqa: E402
import roads  # noqa: E402
from test_roads import FRAME, features  # noqa: E402


def flat(xy):
    return np.full(len(np.atleast_2d(xy)), 10.0)


def electrified(lines, classes=None, tags=None, **kw):
    n = len(lines)
    f = features(lines, classes or ["standard_gauge"] * n, subtypes=["rail"] * n, **kw)
    f.attrs["osm_electrified"] = np.array(tags or ["contact_line"] * n, object)
    return f


BOX = roads.tile_bbox(0, 0)


def masts(f):
    return catenary.masts_of(FRAME, [f], BOX, flat)


class CatenaryTest(unittest.TestCase):
    def test_masts_along_a_straight_line_at_the_span(self):
        f = electrified([[(-300.0, 100.0), (550.0, 100.0)]])
        ms = masts(f)
        self.assertGreater(len(ms), 8)
        s = np.array(sorted(m["s"] for m in ms))
        self.assertTrue(np.all(np.abs(np.diff(s) - catenary.SPAN_M) < catenary.SPAN_M * 0.55), "about a span apart")
        for m in ms:
            self.assertAlmostEqual(float(np.hypot(*(m["q"] - m["axis"]))), catenary.MAST_OFF_M, delta=0.05)
            np.testing.assert_allclose(m["dir"], (m["axis"] - m["q"]) / catenary.MAST_OFF_M, atol=0.05)

    def test_not_electrified_none(self):
        f = electrified([[(-300.0, 100.0), (550.0, 100.0)]], tags=["no"])
        self.assertEqual(masts(f), [])
        f = electrified([[(-300.0, 100.0), (550.0, 100.0)]], tags=[None])
        self.assertEqual(masts(f), [])

    def test_the_same_masts_in_every_tile(self):
        f = electrified([[(-300.0, 100.0), (900.0, 100.0)]])
        a = catenary.masts_of(FRAME, [f], roads.tile_bbox(0, 0), flat)
        b = catenary.masts_of(FRAME, [f], roads.tile_bbox(1, 0), flat)
        pa = {round(float(m["q"][0]), 1) for m in a}
        pb = {round(float(m["q"][0]), 1) for m in b}
        common = {x for x in pa if 200.0 < x < 480.0}
        self.assertTrue(common and common <= pb, (sorted(pa), sorted(pb)))

    def test_double_track_masts_on_the_outer_sides(self):
        f = electrified([[(-200.0, 100.0), (500.0, 100.0)], [(-200.0, 105.0), (500.0, 105.0)]])
        ms = masts(f)
        self.assertTrue(ms)
        for m in ms:
            self.assertTrue(m["q"][1] < 100.0 - 3.0 or m["q"][1] > 105.0 + 3.0, "never between the tracks")

    def test_masts_keep_off_a_road(self):
        f = electrified([[(-300.0, 100.0), (550.0, 100.0)]])
        lines = [[(-300.0, 100.0), (550.0, 100.0)], [(120.0, -50.0), (120.0, 300.0)]]
        f = features(lines, ["standard_gauge", "secondary"], subtypes=["rail", "road"])
        f.attrs["osm_electrified"] = np.array(["contact_line", None], object)
        half = roads.road_specs(f)[1].width / 2
        for m in masts(f):
            self.assertGreater(abs(float(m["q"][0]) - 120.0), half + catenary.ROAD_CLEAR_M - 0.5)

    def test_masts_keep_off_buildings(self):
        f = electrified([[(-300.0, 100.0), (550.0, 100.0)]])
        free = masts(f)
        m0 = [m for m in free if 0.0 < m["q"][0] < 256.0][2]
        q = m0["q"]
        hut = np.array([[q[0] - 3.0, q[1] - 2.0], [q[0] + 3.0, q[1] - 2.0], [q[0] + 3.0, q[1] + 2.0], [q[0] - 3.0, q[1] + 2.0]])
        kept = catenary.masts_of(FRAME, [f], BOX, flat, [hut])
        for m in kept:
            self.assertFalse(catenary.near_ring(m["q"], [hut]), "no mast in the hut's wall")
        self.assertTrue(catenary.near_ring(q, [hut]) and catenary.near_ring(np.array([q[0] + 4.0, q[1]]), [hut]))
        self.assertFalse(catenary.near_ring(np.array([q[0] + 6.0, q[1]]), [hut]))

    def test_style_by_country(self):
        self.assertEqual(catenary.style_of("UA"), "su")
        self.assertEqual(catenary.style_of("pl"), "su")
        self.assertEqual(catenary.style_of("CZ"), "su")
        self.assertEqual(catenary.style_of("DE"), "eu")
        self.assertEqual(catenary.style_of("FR"), "eu")
        self.assertEqual(catenary.style_of(None), "eu")
        f = electrified([[(-300.0, 100.0), (550.0, 100.0)]])
        ms = masts(f)
        ow = lambda x, y: BOX[0] <= x < BOX[2] and BOX[1] <= y < BOX[3]    # noqa: E731
        self.assertTrue(all(r[0] == "catmast" for r in catenary.rows_and_wires(ms, ow, "su")[0]))
        self.assertTrue(all(r[0] == "catmast_eu" for r in catenary.rows_and_wires(ms, ow, "eu")[0]))

    def test_third_rail_has_no_catenary(self):
        f = electrified([[(-300.0, 100.0), (550.0, 100.0)]], tags=["rail"])
        self.assertEqual(masts(f), [])

    def test_three_tracks_get_a_portal_not_cantilevers(self):
        f = electrified([[(-300.0, y), (700.0, y)] for y in (100.0, 106.0, 112.0)])
        ms = masts(f)
        portals = [m for m in ms if m.get("portal")]
        self.assertTrue(portals)
        self.assertFalse([m for m in ms if not m.get("portal") and not m.get("virtual")], "no cantilever between tracks")
        for pt in portals:
            (a, _za), (b, _zb) = pt["posts"]
            ys = sorted([a[1], b[1]])
            self.assertLess(ys[0], 100.0 - 2.5, "posts outside the outer tracks")
            self.assertGreater(ys[1], 112.0 + 2.5)
            self.assertAlmostEqual(abs(a[0] - b[0]), 0.0, delta=0.5, msg="across the tracks")
            self.assertEqual(len(pt["tracks"]), 3)
        virtual = [m for m in ms if m.get("virtual")]
        self.assertEqual(len(virtual), 3 * len(portals))
        ow = lambda x, y: True    # noqa: E731
        rows, wires = catenary.rows_and_wires(ms, ow, "su")
        self.assertEqual(sorted(r[0] for r in rows), ["catpost"] * (2 * len(portals)))
        chords = [w for w, r in wires if r == catenary.CHORD_R]
        self.assertEqual(len(chords), 2 * len(portals))
        self.assertFalse([w for w, r in wires if r == catenary.DIAG_R and abs(w[0, 0] - w[-1, 0]) + abs(w[0, 1] - w[-1, 1]) > 1e-6],
                         "no diagonals: the girder's bars between the chords are verticals")
        for w, r in wires:
            if r == catenary.CHORD_R:
                self.assertIn(round(float(w[0, 2]), 1), (10.0 + catenary.PORTAL_Z, 10.0 + catenary.PORTAL_Z + catenary.PORTAL_H))
        contact = [w for w, r in wires if r == catenary.CONTACT_R]
        self.assertTrue(contact, "the contact wires run on between the portals")

    def test_two_tracks_stay_cantilevers_and_a_wide_yard_gets_none(self):
        f = electrified([[(-300.0, 100.0), (700.0, 100.0)], [(-300.0, 106.0), (700.0, 106.0)]])
        self.assertFalse([m for m in masts(f) if m.get("portal")])
        wide = electrified([[(-300.0, y), (700.0, y)] for y in (60.0, 80.0, 100.0, 120.0, 140.0)])
        self.assertFalse([m for m in masts(wide) if m.get("portal")], "wider than PORTAL_MAX_W_M")

    def test_pieces_of_one_feature_are_not_joined(self):
        """Masts of two pieces of a feature (their arc lengths each from 0) are never one span: no diagonal wire."""
        def mast(part, s, x, y):
            q = np.array([x, y])
            return {"fid": "same", "part": part, "s": s, "q": q, "axis": q + [0.0, 3.45], "z": 10.0, "dir": np.array([0.0, 1.0]),
                    "u": np.array([1.0, 0.0])}
        ms = [mast(0, 0.0, 0.0, 100.0), mast(0, 60.0, 60.0, 100.0), mast(1, 30.0, 30.0, 200.0), mast(1, 90.0, 90.0, 200.0)]
        rows, wires = catenary.rows_and_wires(ms, lambda x, y: True)
        for w, r in wires:
            if r == catenary.CONTACT_R:
                self.assertLess(abs(float(w[0, 1]) - float(w[-1, 1])), 1.0, "each span stays on its own piece")
        self.assertEqual(len([w for w, r in wires if r == catenary.CONTACT_R]), 2)

    def test_curve_shortens_the_span(self):
        r = 400.0
        th = np.linspace(0.0, math.pi / 3, 60)
        arc = [(r * math.sin(t), 100.0 + r * (1 - math.cos(t))) for t in th]
        straight = [(x, 100.0) for x in np.arange(0.0, 400.0, 20.0)]
        xy = np.array(arc)
        a = _stn(xy)
        b = _stn(np.array(straight))
        self.assertLess(np.diff(a).mean(), np.diff(b).mean())
        self.assertGreaterEqual(np.diff(a).min(), catenary.SPAN_MIN_M - 1e-6)

    def test_rows_and_wires(self):
        f = electrified([[(-300.0, 100.0), (550.0, 100.0)]])
        ms = masts(f)
        rows, wires = catenary.rows_and_wires(ms, lambda x, y: BOX[0] <= x < BOX[2] and BOX[1] <= y < BOX[3])
        self.assertTrue(rows)
        self.assertTrue(all(r[0] == catenary.GROUP and BOX[0] <= r[1] < BOX[2] for r in rows), "the tile's masts only")
        contact = [w for w, r in wires if r == catenary.CONTACT_R]
        self.assertTrue(contact)
        for line in contact:
            self.assertAlmostEqual(float(line[0, 2]), 10.0 + catenary.Z_CONTACT, delta=0.6)
            self.assertAlmostEqual(abs(float(line[0, 1]) - 100.0), 0.0, delta=0.5, msg="over the axis")
        mess = [w for w, r in wires if r == catenary.MESSENGER_R]
        self.assertTrue(all(w[:, 2].min() < w[0, 2] - 0.5 for w in mess), "the messenger sags")
        drops = [w for w, r in wires if r == catenary.DROPPER_R]
        self.assertTrue(drops and all(len(d) == 2 for d in drops))
        # (a wire of the next tile is drawn by it: no span twice)
        rows2, wires2 = catenary.rows_and_wires(ms, lambda x, y: roads.tile_bbox(1, 0)[0] <= x < roads.tile_bbox(1, 0)[2])
        keys = {tuple(np.round(w[0], 1)) for w, r in wires if r == catenary.CONTACT_R}
        keys2 = {tuple(np.round(w[0], 1)) for w, r in wires2 if r == catenary.CONTACT_R}
        self.assertFalse(keys & keys2)

    def _contact_ends(self, wires):
        return [(tuple(np.round(w[0, :2], 1)), tuple(np.round(w[-1, :2], 1))) for w, r in wires if r == catenary.CONTACT_R]

    def _chain(self, wires):
        """The contact wires as one chain: the sorted ends joined; returns (number of wires, the gaps between consecutive
        wires' ends along x, 0 where they join)."""
        ws = sorted((w for w, r in wires if r == catenary.CONTACT_R), key=lambda w: min(w[0, 0], w[-1, 0]))
        gaps = [float(np.hypot(*(a[-1, :2] - b[0, :2]))) for a, b in zip(ws[:-1], ws[1:])]
        return len(ws), gaps

    def test_wire_runs_on_through_the_joint_of_two_ways(self):
        f = electrified([[(-300.0, 100.0), (120.0, 100.0)], [(120.0, 100.0), (550.0, 100.0)]])
        ms = masts(f)
        rows, wires = catenary.rows_and_wires(ms, lambda x, y: True)
        n, gaps = self._chain(wires)
        self.assertGreater(n, 8)
        self.assertLess(max(gaps), 0.05, "every wire starts where the one before it ends, over the joint too")
        joint = [w for w, r in wires if r == catenary.CONTACT_R and min(w[0, 0], w[-1, 0]) < 120.0 < max(w[0, 0], w[-1, 0])]
        self.assertTrue(joint, "a span crosses the joint")

    def test_a_branch_wire_starts_on_the_main_track(self):
        f = electrified([[(-300.0, 100.0), (550.0, 100.0)], [(150.0, 100.0), (300.0, 160.0)]])
        ms = masts(f)
        ends = [m for m in ms if "link" in m and m["link"][1].get("end")]
        self.assertTrue(ends, "the branch's end on the main track gets an anchor")
        e = ends[0]["link"][1]
        self.assertAlmostEqual(float(e["q"][1]), 100.0, delta=0.5, msg="on the main track's axis")
        rows, wires = catenary.rows_and_wires(ms, lambda x, y: True)
        starts = [w[0, :2] for w, r in wires if r == catenary.CONTACT_R] + [w[-1, :2] for w, r in wires if r == catenary.CONTACT_R]
        self.assertTrue(any(float(np.hypot(*(p - e["q"]))) < 0.05 for p in starts), "a contact wire ends at that anchor")
        mess = [w for w, r in wires if r == catenary.MESSENGER_R and min(float(np.hypot(*(w[0, :2] - e["q"]))), float(np.hypot(*(w[-1, :2] - e["q"])))) < 0.05]
        self.assertTrue(mess)
        for w in mess:
            k = 0 if float(np.hypot(*(w[0, :2] - e["q"]))) < 0.05 else -1
            self.assertAlmostEqual(float(w[k, 2]), 10.0 + catenary.Z_CONTACT, delta=0.01, msg="the messenger comes down to the contact wire")

    def test_droppers_every_few_metres_and_the_gap_between_wires(self):
        f = electrified([[(-300.0, 100.0), (550.0, 100.0)]])
        rows, wires = catenary.rows_and_wires(masts(f), lambda x, y: True)
        drops = [w for w, r in wires if r == catenary.DROPPER_R]
        xs = np.sort(np.array([float(d[0, 0]) for d in drops]))
        near = np.diff(xs)[np.diff(xs) > 0.5]
        self.assertTrue(len(near) and near.max() <= catenary.DROPPER_EVERY_M * 1.5 + 1.0, near.max())
        for d in drops:
            self.assertGreater(float(d[1, 2] - d[0, 2]), catenary.DROPPER_MIN_GAP_M)
        gaps = [float(d[1, 2] - d[0, 2]) for d in drops]
        self.assertAlmostEqual(min(gaps), 0.5, delta=0.1, msg="half a metre between the messenger and the contact wire at mid span")

    def test_portal_holders_per_track(self):
        f = electrified([[(-300.0, y), (700.0, y)] for y in (100.0, 106.0, 112.0)])
        ms = masts(f)
        pt = next(m for m in ms if m.get("portal"))
        wires = catenary.portal_wires(pt)
        ins = [w for w, r in wires if r == catenary.INS_R]
        self.assertEqual(len(ins), 2 * 3, "two rod insulators on each of the three tracks")
        for w, r in wires:
            pass
        hang = [w for w, r in wires if r == catenary.HANGER_R]
        self.assertTrue(hang)
        tops = sorted(round(float(w[0, 2]), 2) for w in ins)
        self.assertTrue(all(abs(a[1, 2] - b[0, 2]) < 1e-6 for a in ins for b in ins
                            if np.allclose(a[1, :2], b[0, :2]) and a is not b and abs(a[1, 2] - b[0, 2]) < 0.2),
                        "the two insulators of a holder touch")
        clamps = [w for w, r in wires if r == catenary.CLAMP_R]
        self.assertEqual(len(clamps), 3)

    def test_wire_materials_by_class(self):
        import furniture
        f = electrified([[(-300.0, y), (700.0, y)] for y in (100.0, 106.0, 112.0)])
        ms = masts(f)
        rows, wires = catenary.rows_and_wires(ms, lambda x, y: True)
        wires = list(wires) + [(np.array([[0.0, 0.0, 8.0], [30.0, 0.0, 8.0]]), furniture.WIRE_R)]
        wm = furniture.wire_mesh(wires, (0.0, 0.0, 0.0), catenary.wire_material)
        P, T, UV, names, TM = wm
        self.assertEqual(set(names), {catenary.MAT_COPPER, catenary.MAT_BRONZE, catenary.MAT_STEEL, catenary.MAT_INSULATOR,
                                      "bne_wire"})
        self.assertEqual(len(TM), len(T))
        self.assertEqual(furniture.wire_mesh(wires, (0, 0, 0))[0].shape, P.shape, "the plain call is as it was")
        self.assertEqual(len(UV), len(P))
        P2, T2, UV2, n2, TM2 = furniture.wire_mesh(wires, (0, 0, 0), catenary.wire_material, catenary.wire_solid)
        self.assertEqual(len(P2), len(P), "a bar costs the vertices two ribbons did")
        self.assertEqual(len(UV2), len(P2))
        steel = n2.index(catenary.MAT_STEEL)
        bars = T2[TM2 == steel]
        self.assertTrue(len(bars) and len(bars) % 8 == 0, "eight triangles a bar segment")
        self.assertGreater(float(UV2[np.unique(bars)][:, 1].max()), 1.0, "UVs run along the bar")
        out = 0
        for a, b, c in bars:      # (counter-clockwise seen from outside: each face's normal points away from its bar's axis)
            n = np.cross(P2[b] - P2[a], P2[c] - P2[a])
            cen = (P2[a] + P2[b] + P2[c]) / 3.0
            axis = (P2[a] + P2[b] + P2[c] + P2[a] + P2[b] + P2[c]) / 6.0     # (a face's own centre: tests the sign below)
            lo = min(a, b, c) // 8 * 8
            mid = P2[lo:lo + 8].mean(axis=0)
            out += float(n @ (cen - mid)) > 0
        self.assertEqual(out, len(bars), "bars face outwards")

    def test_a_vertical_wire_has_width(self):
        import furniture
        line = np.array([[10.0, 5.0, 6.3], [10.0, 5.0, 7.5]])
        P, T, UV = furniture.wire_mesh([(line, 0.01)], (0.0, 0.0, 0.0))
        self.assertGreater(float(np.ptp(P[:, 0]) + np.ptp(P[:, 1])), 0.03, "both crossed ribbons are as wide as the wire")
        self.assertAlmostEqual(float(np.ptp(P[:, 0])), 0.02, places=6)
        self.assertAlmostEqual(float(np.ptp(P[:, 1])), 0.02, places=6)

    def test_overlapping_portals_are_merged(self):
        a = {"portal": True, "posts": [(np.array([0.0, 0.0]), 0.0), (np.array([0.0, 20.0]), 0.0)]}
        near = {"portal": True, "posts": [(np.array([6.0, 1.0]), 0.0), (np.array([6.0, 21.0]), 0.0)]}
        far = {"portal": True, "posts": [(np.array([40.0, 0.0]), 0.0), (np.array([40.0, 20.0]), 0.0)]}
        self.assertTrue(catenary._overlaps(near, [a]))
        self.assertFalse(catenary._overlaps(far, [a]))

    def test_mapped_masts_are_used(self):
        f = electrified([[(-300.0, 100.0), (550.0, 100.0)]])
        from test_roads import LAT, LON, M_LAT, M_LON
        nodes = [(LON + x / M_LON, LAT + 96.5 / M_LAT, {"power": "catenary_mast"}) for x in range(-200, 500, 50)]
        f.__dict__["_rail_nodes"] = nodes
        ms = masts(f)
        xs = np.array(sorted(float(m["q"][0]) for m in ms))
        self.assertTrue(np.all(np.abs(np.diff(xs) - 50.0) < 8.0), np.diff(xs))
        self.assertTrue(all(m["q"][1] < 100.0 for m in ms), "on the mapped side")


def _stn(xy):
    s, arc = catenary.stations(xy, "k")
    return np.array(s)


if __name__ == "__main__":
    unittest.main()
