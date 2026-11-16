"""The tram overhead line's kernel (native/earthfast/src/tram) against the Python it replaced (tests/tramcat_ref.py):
the same poles and masts on synthetic scenes (a street, an own way, a wall, bridges, curves, two tracks), to the
centimetre (plan 046, stage 1: the port changed no behaviour). Stage G changed the wires on purpose (they follow the
axis between the old chords' ends, parts are joined, the span wires have insulators and springs), so the wires are held
to what stayed: every old chord's ends and every span wire's hanger point are on the new wires, no new contact wire
leaves the old chord by more than a pull-off, the droppers are the same."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fast  # noqa: E402
import roads  # noqa: E402
import tramcat  # noqa: E402
import tramcat_ref as ref  # noqa: E402
from test_roads import FRAME, features  # noqa: E402

BOX = roads.tile_bbox(0, 0)


def ground(xy):
    xy = np.atleast_2d(xy)
    return 10.0 + 0.01 * xy[:, 0] + 0.5 * np.sin(xy[:, 1] / 20.0)


def scene(lines, classes, tags, ids=None, subtypes=None):
    f = features(lines, classes, ids=ids, subtypes=subtypes)
    f.attrs["osm_electrified"] = np.array(list(tags), object)
    return f


def both(f, rings=(), decks=(), box=BOX, country="UA"):
    parts, poles = ref.plan(FRAME, [f], box, rings, decks)
    r_rows, r_wires = ref.rows_and_wires(parts, poles, ground, lambda x, y: roads.box_owns(box, x, y),
                                         ref.cat.style_of(country))
    rows, rxy, wires, wxy, kpoles, _gaps = tramcat.overhead(FRAME, [f], box, ground, rings, country, decks)
    own_r = roads.box_owns(box, rxy[:, 0], rxy[:, 1]) if len(rows) else []
    own_w = roads.box_owns(box, wxy[:, 0], wxy[:, 1]) if len(wires) else []
    rows = [r for r, o in zip(rows, own_r) if o]
    wires = [w for w, o in zip(wires, own_w) if o]
    return (r_rows, r_wires, poles), (rows, wires, kpoles)


def _dist_seg(p, w):
    a, b = w[:-1], w[1:]
    d = b - a
    t = np.clip(((p - a) * d).sum(1) / np.maximum((d * d).sum(1), 1e-12), 0.0, 1.0)
    return float(np.sqrt(((a + t[:, None] * d - p) ** 2).sum(1)).min())


def compare(tc, a, b):
    (r_rows, r_wires, r_poles), (rows, wires, poles) = a, b
    # (the models are the tram's own now, the lamp brackets and wall rosettes are new rows: the poles and masts stand where
    # the reference's did, in the same order)
    structure = [r for r in rows if r[0].startswith("tram_") and r[0] not in ("tram_lamp_g", "tram_lamp_k", "tram_lamp_t", "tram_rosette")]
    tc.assertEqual(len(r_rows), len(structure))
    for x, y in zip(r_rows, structure):
        np.testing.assert_allclose([float(v) for v in x[1:4]], [float(v) for v in y[1:4]], atol=0.006)
        if x[0] == "catmast":
            tc.assertAlmostEqual(x[4], y[4], delta=0.06)
    C = tramcat.consts()
    new_c = [w for w, r in wires if r == C["CONTACT_R"]]
    pts_c = np.concatenate([w for w in new_c]) if new_c else np.zeros((0, 3))
    missing = 0
    for w, r in r_wires:
        if r == ref.cat.CONTACT_R:         # (the old chord: its two ends are points of the new wires, a vertex is near it)
            for e in (w[0], w[-1]):
                tc.assertLess(float(np.sqrt(((pts_c[:, :2] - e[:2]) ** 2).sum(1)).min()), 3.0)    # (in plan: an own way's wire is now 5.6 m up, not the railway's 6.18; a node within 3 m of the one before is left out)
        elif r == C["SPAN_R"]:             # (its hanger point is on a new span wire, but for one within 3 m of a part's end)
            if not any(rr == r and np.sqrt(((ww[:, :2] - w[1][:2]) ** 2).sum(1)).min() < 0.01 for ww, rr in wires):
                missing += 1
    tc.assertLessEqual(missing, 2 * len(set(p["q"][0] // 1000 for p in r_poles)) + 2)
    for w in new_c:                       # (no vertex leaves the old chords by more than the axis' own bend)
        for p in w:
            tc.assertLess(min(_dist_seg(p, ww) for ww, rr in r_wires if rr == ref.cat.CONTACT_R) if r_wires else 0.0, 6.0)
    tc.assertEqual(len(r_poles), len(poles))
    for p, (x, y, wall, z) in zip(r_poles, poles):
        np.testing.assert_allclose(p["q"], [x, y], atol=1e-6)
        tc.assertEqual(bool(p["wall"]), wall)
        tc.assertEqual("z" in p, z is not None)
        if z is not None:
            tc.assertAlmostEqual(p["z"], z, places=6)


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= tramcat.KERNEL, "the kernel is older")
class GoldenTest(unittest.TestCase):
    def check(self, f, **kw):
        a, b = both(f, **kw)
        self.assertTrue(a[0] or a[1], "the scene has something to compare")
        compare(self, a, b)

    def test_street_tram_between_pavements(self):
        self.check(scene([[(-150.0, 128.0), (450.0, 128.0)], [(-150.0, 128.0), (450.0, 128.0)]], ["primary", "tram"],
                         ["", "contact_line"], ids=["road0", "tram0"], subtypes=["road", "rail"]))

    def test_own_way_tram_gets_masts(self):
        self.check(scene([[(-150.0, 128.0), (450.0, 128.0)], [(-150.0, 40.0), (450.0, 40.0)]], ["primary", "tram"],
                         ["", "contact_line"], ids=["road0", "tram0"], subtypes=["road", "rail"]))

    def test_wall_instead_of_pole(self):
        f = scene([[(-150.0, 128.0), (450.0, 128.0)], [(-150.0, 128.0), (450.0, 128.0)]], ["primary", "tram"],
                  ["", "contact_line"], ids=["road0", "tram0"], subtypes=["road", "rail"])
        wall = np.array([[-200.0, 133.5], [500.0, 133.5], [500.0, 160.0], [-200.0, 160.0]])
        self.check(f, rings=[wall])

    def test_curved_street_tram_and_a_second_track(self):
        t = np.linspace(0.0, 1.0, 40)
        curve = [(-100.0 + 500.0 * u, 110.0 + 60.0 * np.sin(u * 3.0)) for u in t]
        side = [(x, y + 5.0) for x, y in curve]
        road = [(x, y + 2.0) for x, y in curve]
        self.check(scene([road, curve, side], ["secondary", "tram", "tram"], ["", "contact_line", "contact_line"],
                         ids=["r0", "t0", "t1"], subtypes=["road", "rail", "rail"]))

    def test_tram_across_the_tile_border(self):
        box = roads.tile_bbox(1, 0)
        f = scene([[(-150.0, 128.0), (2500.0, 128.0)], [(-150.0, 128.0), (2500.0, 128.0)]], ["primary", "tram"],
                  ["", "contact_line"], ids=["road0", "tram0"], subtypes=["road", "rail"])
        self.check(f, box=box)

    def test_eu_style_groups(self):
        a, b = both(scene([[(-150.0, 128.0), (450.0, 128.0)], [(-150.0, 128.0), (450.0, 128.0)]], ["primary", "tram"],
                          ["", "contact_line"], ids=["road0", "tram0"], subtypes=["road", "rail"]), country="DE")
        compare(self, a, b)
        self.assertTrue(any(r[0] == "tram_tube_span" for r in b[0]))

    def test_bridge_on_a_road_deck(self):
        f = scene([[(-150.0, 128.0), (450.0, 128.0)], [(-150.0, 128.0), (450.0, 128.0)]], ["primary", "tram"],
                  ["", "contact_line"], ids=["road0", "tram0"], subtypes=["road", "rail"])
        saved = roads.level_ranges
        roads.level_ranges = lambda feats, i, **kw: [(0.25, 0.75, "bridge")] if i == 1 else []
        self.addCleanup(setattr, roads, "level_ranges", saved)
        xs = np.arange(-150.0, 451.0, 5.0)
        deck = [(np.c_[xs, np.full(len(xs), 128.0 - 3.0)], np.full(len(xs), 24.0) + 0.01 * xs, 12.0)]
        self.check(f, decks=deck)

    def test_nothing_without_a_contact_line(self):
        f = scene([[(-150.0, 128.0), (450.0, 128.0)], [(-150.0, 128.0), (450.0, 128.0)]], ["primary", "tram"],
                  ["", "no"], ids=["road0", "tram0"], subtypes=["road", "rail"])
        a, b = both(f)
        self.assertEqual((a[0], a[1], b[0], b[1]), ([], [], [], []))


if __name__ == "__main__":
    unittest.main()
