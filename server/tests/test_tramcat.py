"""Tram overhead line (tramcat.py): poles on the pavements or building walls, the span wire, the contact wire."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catenary  # noqa: E402
import roads  # noqa: E402
import fast  # noqa: E402
import tramcat  # noqa: E402
import railways  # noqa: E402
from test_roads import FRAME, features  # noqa: E402

BOX = roads.tile_bbox(0, 0)
Z = 10.0
C = tramcat.consts() if fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= tramcat.KERNEL else {}


def flat(xy):
    return np.full(len(np.atleast_2d(xy)), Z)


def scene(tram_y, tags=("", "contact_line")):
    """A primary street along y = 128 and a tram along y = tram_y."""
    f = features([[(-150.0, 128.0), (450.0, 128.0)], [(-150.0, tram_y), (450.0, tram_y)]], ["primary", "tram"],
                 ids=["road0", "tram0"], subtypes=["road", "rail"])
    f.attrs["osm_electrified"] = np.array(list(tags), object)
    return f


def axis_y(f):
    """The tram's axis y in the test frame (the frame is not exactly metric: 128 comes out ~0.3 m off)."""
    parts = tramcat.tram_parts(FRAME, [f], BOX)
    return float(np.mean([xy[:, 1].mean() for _f, xy, _k in parts]))


def overhead(f, rings=(), decks=()):
    """([], poles [{"q", "wall", "z"?}], rows, wires) of the kernel's tram overhead line on the tile (the tile's own items)."""
    rows, rxy, wires, wxy, poles, _gaps = tramcat.overhead(FRAME, [f], BOX, flat, rings, "UA", decks)
    own_r = roads.box_owns(BOX, rxy[:, 0], rxy[:, 1]) if len(rows) else []
    own_w = roads.box_owns(BOX, wxy[:, 0], wxy[:, 1]) if len(wires) else []
    pl = [dict({"q": np.array([x, y]), "wall": wall}, **({} if z is None else {"z": z})) for x, y, wall, z in poles]
    return [], pl, [r for r, o in zip(rows, own_r) if o], [w for w, o in zip(wires, own_w) if o]


def run(f, rings=()):
    return overhead(f, rings)


class StreetTramTest(unittest.TestCase):
    def setUp(self):
        self.f = scene(128.0)
        self.half = roads.road_specs(self.f)[0].width / 2
        self.y = axis_y(self.f)

    def test_poles_stand_on_both_pavements_clear_of_the_carriageway(self):
        _p, _pl, rows, _w = run(self.f)
        ys = np.array([r[2] for r in rows if r[0] == "tram_concrete_span"])
        self.assertGreater(len(ys), 6)
        self.assertTrue(np.all(np.abs(ys - self.y) > self.half + C['SIDEWALK_OFF_M'] - 0.3), "off the carriageway")
        self.assertTrue((ys < self.y).any() and (ys > self.y).any(), "both sides")
        self.assertTrue(all(r[5] == 1.0 and r[6] == C['MODEL_H'] for r in rows if r[0].startswith('tram_') and r[0] != 'tram_rosette'))
        self.assertEqual(len([r for r in rows if r[0] in ('tram_lamp_g', 'tram_lamp_k', 'tram_lamp_t')]), len(ys), 'a lamp bracket on each pole')

    def test_contact_wire_over_the_axis_and_a_span_wire_between_the_poles(self):
        _p, _pl, _r, wires = run(self.f)
        contact = [w for w, r in wires if r == C['CONTACT_R']]
        self.assertTrue(contact)
        for w in contact:
            np.testing.assert_allclose(w[:, 2], Z + C['CONTACT_Z'], atol=0.01)
            np.testing.assert_allclose(w[:, 1], self.y, atol=0.05)
        spans = [w for w, r in wires if r == C['SPAN_R']]
        self.assertTrue(spans)
        ins = [w for w, r in wires if r == C['INS_R']]
        self.assertTrue(ins and all(abs(w[0, 2] - (Z + C['SPAN_Z'])) < 0.01 for w in ins if abs(w[0, 1] - self.y) > 3.0),
                        "an insulator at each pole's fixing")
        self.assertEqual(len([1 for w, r in wires if r == C['SPRING_R']]), len(spans), "a tensioner spring a span")
        for w in spans:
            self.assertEqual(len(w), 3)
            self.assertAlmostEqual(w[1, 2], Z + C['CONTACT_Z'] + C['HANGER_M'], places=2)
            self.assertGreater(w[0, 1], self.y + self.half)       # (the left of the track first)
            self.assertLess(w[2, 1], self.y - self.half)

    def test_a_building_where_the_pole_would_stand_takes_the_fixing_on_its_wall(self):
        wall = np.array([[-200.0, self.y + self.half + 0.5], [500.0, self.y + self.half + 0.5],
                         [500.0, 160.0], [-200.0, 160.0]])
        _p, poles, rows, wires = run(self.f, rings=[wall])
        self.assertTrue(any(pl["wall"] for pl in poles))
        self.assertFalse(any(r[2] > self.y for r in rows if r[0] != "tram_rosette"), "no pole on the building's side")
        ros = [r for r in rows if r[0] == "tram_rosette"]
        self.assertTrue(ros, "a rosette on its wall")
        for r in ros:
            self.assertAlmostEqual(r[2], self.y + self.half + 0.5, delta=0.01)     # (on the wall's face)
            self.assertAlmostEqual(r[3], Z + C["WALL_Z"], places=2)
        walls = [w for w, r in wires if r == C['INS_R'] and abs(w[0, 2] - (Z + C['WALL_Z'])) < 0.01]
        self.assertTrue(walls, "a span wire fixed at the wall's height")

    def test_deterministic(self):
        a = run(self.f)
        b = run(self.f)
        self.assertEqual(a[2], b[2])
        self.assertEqual(len(a[3]), len(b[3]))


class OwnWayTramTest(unittest.TestCase):
    def test_a_tram_on_its_own_way_gets_the_cantilever_mast_beside_it(self):
        f = scene(40.0)
        y = axis_y(f)
        _p, _pl, rows, wires = run(f)
        masts = [r for r in rows if r[0] == "tram_concrete_arm"]
        self.assertGreater(len(masts), 4)
        for r in masts:
            self.assertAlmostEqual(abs(r[2] - y), catenary.MAST_OFF_M, delta=0.05)
        self.assertFalse([r for r in rows if r[0] == "tram_concrete_span"])
        contact = [w for w, rad in wires if rad == C['CONTACT_R']]
        self.assertTrue(contact)
        np.testing.assert_allclose(contact[0][:, 2], Z + C['Z_ARM'], atol=0.3)


class YardTest(unittest.TestCase):
    """A fan of six tracks of their own (a depot's yard): poles stand outside the track field, as a portal, not one a track
    every 34 m (plan 046, the Vinnytsia depot)."""

    def setUp(self):
        ys = [20.0 + 4.2 * k for k in range(6)]
        self.f = features([[(-150.0, y), (450.0, y)] for y in ys], ["tram"] * 6, ids=[f"y{k}" for k in range(6)],
                          subtypes=["rail"] * 6)
        self.f.attrs["osm_electrified"] = np.array(["contact_line"] * 6, object)
        self.axes = [np.array([[-150.0, y], [450.0, y]]) for y in ys]

    def test_no_pole_within_the_sleepers_of_any_track_and_a_portal_holds_the_wires(self):
        rows, _rxy, wires, _wxy, poles, gaps = tramcat.overhead(FRAME, [self.f], BOX, flat, (), "UA", ())
        ys = sorted({round(float(np.mean(xy[:, 1])), 1) for _f, xy, _k in tramcat.tram_parts(FRAME, [self.f], BOX)})
        self.assertEqual(len(ys), 6)
        struct = [r for r in rows if r[0].startswith("tram_") and not r[0].startswith("tram_lamp")]
        self.assertTrue(struct)
        for r in struct:
            self.assertTrue(all(abs(r[2] - y) >= 1.5 for y in ys), f"{r[0]} at y {r[2]} stands on the tracks {ys}")
        # a portal: poles outside the whole fan (or in its wide gaps), far fewer than a mast per track and station
        self.assertLess(len(struct), 70)     # (3 a portal: 18 portals; a mast a track and station would be over 100)
        self.assertEqual(gaps, [], "no stretch of wire without support for more than 40 m")
        spans = [w for w, r in wires if r == C["SPAN_R"] and len(w) >= 3]
        self.assertTrue(spans, "cross-spans")
        self.assertTrue(any(len(w) >= 5 for w in spans), "a cross-span carries the hangers of several tracks")


class FlagsTest(unittest.TestCase):
    def test_a_tram_whose_osm_line_is_not_a_contact_line_has_no_overhead(self):
        parts, poles, rows, wires = run(scene(128.0, tags=("", "no")))
        self.assertEqual((parts, rows, wires), ([], [], []))
        _p, _pl, rows, _w = run(scene(128.0, tags=("", "rail")))
        self.assertEqual(rows, [])

    def test_no_value_means_a_contact_line(self):
        _p, _pl, rows, _w = run(scene(128.0, tags=("", "")))
        self.assertTrue(rows)


class BridgeTest(unittest.TestCase):
    """A tram bridge on a road deck: poles at the deck's edges, at its height; a bridge of its own: bare."""

    def setUp(self):
        self.f = scene(128.0)
        self.f.attrs["osm_electrified"] = np.array(["", "contact_line"], object)
        self.y = axis_y(self.f)
        # the tram's middle 200 m is a bridge; a road deck 12 m wide under it, 14 m higher than the flat ground
        self._saved = roads.level_ranges
        roads.level_ranges = lambda feats, i, **kw: [(0.25, 0.75, "bridge")] if i == 1 else []
        self.addCleanup(setattr, roads, "level_ranges", self._saved)
        xs = np.arange(-150.0, 451.0, 5.0)
        self.deck = [(np.c_[xs, np.full(len(xs), self.y)], np.full(len(xs), Z + 14.0), 12.0)]

    def run_plan(self, decks):
        return overhead(self.f, (), decks)

    def test_poles_stand_behind_the_parapets_at_the_decks_height(self):
        _p, poles, rows, _w = self.run_plan(self.deck)
        on = [pl for pl in poles if "z" in pl]
        self.assertGreater(len(on), 2)
        for pl in on:
            self.assertAlmostEqual(abs(pl["q"][1] - self.y), 6.0 + C['BRIDGE_OUT_M'], delta=0.1)
            self.assertAlmostEqual(pl["z"], Z + 14.0, places=2)
        zs = {round(r[3], 2) for r in rows if r[0] == "tram_concrete_span" and abs(r[2] - self.y) < 7.5 and abs(r[2] - self.y) > 5.0}
        self.assertIn(round(Z + 14.0, 2), zs, "a pole row on the deck")

    def test_poles_are_measured_from_the_decks_centre_line_not_the_trams(self):
        # the deck's centre line 3 m to the right of the tram (it lies to the left of the middle): its edges stay put
        xs = np.arange(-150.0, 451.0, 5.0)
        off = [(np.c_[xs, np.full(len(xs), self.y - 3.0)], np.full(len(xs), Z + 14.0), 12.0)]
        _p, poles, _r, _w = self.run_plan(off)
        on = [pl for pl in poles if "z" in pl]
        self.assertTrue(on)
        for pl in on:
            self.assertAlmostEqual(abs(pl["q"][1] - (self.y - 3.0)), 6.0 + C['BRIDGE_OUT_M'], delta=0.1)
        self.assertTrue(any(pl["q"][1] > self.y + 2.0 for pl in on) and any(pl["q"][1] < self.y - 8.0 for pl in on))

    def test_contact_wire_over_the_bridge_is_at_the_decks_height(self):
        _p, _pl, _r, wires = self.run_plan(self.deck)
        hi = [w for w, r in wires if r == C['CONTACT_R'] and w[:, 2].max() > Z + 14.0]
        self.assertTrue(hi)
        for w in hi:
            self.assertAlmostEqual(float(w[:, 2].max()), Z + 14.0 + C['CONTACT_Z'], delta=0.05)

    def test_a_bridge_with_no_road_deck_has_no_overhead_line(self):
        _p, poles, _r, wires = self.run_plan(())
        self.assertFalse([pl for pl in poles if "z" in pl])
        self.assertFalse([w for w, r in wires if r == C['CONTACT_R'] and w[:, 0].min() > 40.0 and w[:, 0].max() < 340.0],
                         "nothing over the bridge")


if __name__ == "__main__":
    unittest.main()
