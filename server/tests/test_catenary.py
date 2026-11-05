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
        bars = [w for w, r in wires if r in (catenary.CHORD_R, catenary.DIAG_R)]
        self.assertEqual(len(bars), 3 * len(portals))
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
