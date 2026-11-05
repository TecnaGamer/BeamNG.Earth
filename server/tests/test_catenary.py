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
