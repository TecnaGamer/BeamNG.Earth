"""High-voltage lines and substations (earthfast.power_line_layout, earthfast.substation_yard; powerlines.py): a tower
at every vertex outside substations, none inside; a line end where a line reaches a substation, its conductors on the
strain insulators of the kit's gantries there, lines ending close by sharing it; towers shared where lines meet; the
kit at a line end facing the line, every busbar over its column of supports (West Coast USA's yard); the rest of a
yard filled with the largest kit that fits, inside it and clear of the anchored kits; a small yard the smaller kit;
the fence along the whole edge."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fast  # noqa: E402
import powerlines  # noqa: E402

BOX = [-1e4, -1e4, 1e4, 1e4]


def layout(lines, stop=None):
    pts = np.ascontiguousarray(np.concatenate(lines), np.float64)
    off = np.cumsum([0] + [len(r) for r in lines]).astype(np.int64)
    stop = np.zeros(len(pts), bool) if stop is None else np.asarray(stop, bool)
    return fast._ef.power_line_layout(pts, off, stop, powerlines.ARMS, powerlines.SAG, powerlines.SEG_M,
                                      powerlines.MIN_SPAN_M, powerlines.MAX_SPAN_M, powerlines.DEDUPE_M,
                                      powerlines.END_MERGE_M, powerlines.GANTRY_FRONT, BOX)


def yard(rings, anchors=np.zeros((0, 3))):
    pts = np.ascontiguousarray(np.concatenate(rings), np.float64)
    ro = np.cumsum([0] + [len(r) for r in rings]).astype(np.int64)
    groups = sorted(powerlines.PARTS)
    kits = np.array([k[0] for k in powerlines.KITS], np.float64)
    parts = np.array([[k, groups.index(g), x, y] for k, (_, ps) in enumerate(powerlines.KITS) for g, x, y in ps])
    got, fence = fast._ef.substation_yard(pts, ro, kits, parts, powerlines.YARD_GAP,
                                          np.asarray(anchors, np.float64).reshape(-1, 3), 0,
                                          list(powerlines.ANCHOR_AT), powerlines.YARD_MAX_KITS, powerlines.FENCE_LEN,
                                          BOX)
    return [(groups[int(g)], x, y, yaw) for g, x, y, yaw in got], fence


def square(x0, y0, w, h):
    return np.array([[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h]], np.float64)


class LineTest(unittest.TestCase):
    def test_towers_and_conductors(self):
        towers, gantries, wp, wo = layout([np.array([[0, 0, 0], [300, 0, 0], [600, 0, 0]], np.float64)])
        self.assertEqual(len(towers), 3)
        self.assertEqual(len(gantries), 0)
        self.assertEqual(len(wo) - 1, 2 * len(powerlines.ARMS))           # two spans, six conductors each
        self.assertTrue(np.allclose(towers[:, 3], 0.0))                   # turned along the line
        w = wp[wo[0]:wo[1]]
        self.assertAlmostEqual(w[0][2], powerlines.ARMS[0][1])           # from the insulator ...
        self.assertLess(w[len(w) // 2][2], w[0][2] - 5.0)                # ... sagging between towers

    def test_into_a_substation(self):
        # the line's last two vertices in a substation: no towers there, a line end at the first, no span on
        line = np.array([[0, 0, 0], [200, 0, 0], [260, 0, 0], [290, 0, 0]], np.float64)
        towers, ends, wp, wo = layout([line], [False, False, True, True])
        self.assertEqual(sorted(towers[:, 0].tolist()), [0.0, 200.0])
        self.assertEqual(len(ends), 1)
        self.assertTrue(np.allclose(ends[0], [260.0, 0.0, 0.0, 0.0]))     # heading into the substation: +x
        last = np.array([wp[b - 1] for b in wo[1:]])
        last = last[last[:, 0] > 250]
        self.assertEqual(len(last), len(powerlines.ARMS))                # every conductor ends on the gantries ...
        self.assertTrue(np.allclose(last[:, 2], powerlines.GANTRY_H))
        self.assertTrue(np.allclose(last[:, 0], 260.0 - powerlines.GANTRY_FRONT))   # ... in front, toward the line
        # each side of the line to its own gantry's three insulators
        lat = np.sort(last[:, 1])
        g = powerlines.GANTRY_LAT
        self.assertTrue(np.allclose(lat, [-g - 1.75, -g, -g + 1.75, g - 1.75, g, g + 1.75]))
        # a line starting in the substation: the same sides in the world
        towers, ends, wp, wo = layout([line[::-1].copy()], [True, True, False, False])
        self.assertTrue(np.allclose(ends[0][:2], [260.0, 0.0]))
        first = np.array([wp[a] for a in wo[:-1]])
        first = first[first[:, 0] > 250]
        self.assertTrue(np.allclose(np.sort(first[:, 1]), lat))

    def test_ends_merge(self):
        # two lines ending 12 m apart: one end, both lines' conductors on it
        a = np.array([[0, 0, 0], [200, 0, 0], [260, 0, 0]], np.float64)
        b = np.array([[0, 12, 0], [200, 12, 0], [260, 12, 0]], np.float64)
        _, ends, wp, wo = layout([a, b], [False, False, True, False, False, True])
        self.assertEqual(len(ends), 1)
        last = np.array([wp[q - 1] for q in wo[1:]])
        self.assertTrue(np.all(np.abs(last[last[:, 0] > 250][:, 1]) < powerlines.GANTRY_LAT + 2.0))

    def test_shared_tower(self):
        a = np.array([[0, 0, 0], [300, 0, 0]], np.float64)
        b = np.array([[300, 2, 0], [300, 300, 0]], np.float64)
        towers, _, _, _ = layout([a, b])
        self.assertEqual(len(towers), 3)                                  # the lines' meeting: one tower


class YardTest(unittest.TestCase):
    def test_big_yard(self):
        ring = square(0, 0, 120, 60)
        got, fence = yard([ring])
        self.assertIn("sub_breaker", [g for g, *_ in got])
        kits = sum(g == "sub_breaker" for g, *_ in got)
        self.assertGreaterEqual(kits, 4)
        for g, x, y, yaw in got:
            self.assertTrue(0 < x < 120 and 0 < y < 60, (g, x, y))
        self.assertTrue(all(abs(yaw % 180) < 1e-6 or abs(yaw % 180 - 180) < 1e-6 for *_, yaw in got))
        # the fence: the whole edge (360 m of 2.04 m panels), on it
        self.assertGreaterEqual(len(fence), int(360 / powerlines.FENCE_LEN))
        on = [min(abs(x), abs(x - 120), abs(y), abs(y - 60)) < 1e-6 for x, y, _ in fence]
        self.assertTrue(all(on))

    def test_small_yard(self):
        got, _ = yard([square(0, 0, 26, 24)])
        self.assertEqual(sorted(g for g, *_ in got), ["sub_mast", "sub_transformer"])

    def test_line_end_kit(self):
        # a line coming in from the west (heading +x) to (20, 50) in a 120 x 100 yard: the big kit there, its gantries
        # 5.8 m either side of the end, facing the line (their +y west: yaw 90 degrees, x north), every busbar (29.5 m
        # along the gantry's -y: east, into the yard) over its column of six supports; the rest filled clear of it
        got, _ = yard([square(0, 0, 120, 100)], anchors=[[20.0, 50.0, 0.0]])
        gantries = sorted((round(y, 2), round(x, 2), yaw) for g, x, y, yaw in got if g == "gantry")
        self.assertGreaterEqual(len(gantries), 2)
        g = powerlines.GANTRY_LAT
        near = [q for q in gantries if abs(q[1] - 20.0) < 1.0 and abs(q[0] - 50.0) < g + 0.5]
        self.assertEqual([round(q[0] - 50.0, 1) for q in near], [round(-g + 0.045, 1), round(g + 0.045, 1)])
        self.assertTrue(all(abs(q[2] - 90.0) < 1e-6 for q in near))
        supports = [(x, y) for g_, x, y, _ in got if g_ == "sub_support"]
        for y, x, _ in near:
            under = [s for s in supports if abs(s[1] - y) < 1.2 and x - 4.0 < s[0] < x + 29.5]
            self.assertEqual(len(under), 6, (x, y))
        # the other kits clear of it: no part of another kit within its footprint
        breakers = [(x, y) for g_, x, y, _ in got if g_ == "sub_breaker"]
        self.assertGreater(len(breakers), 1)
        self.assertEqual(sum(1 for x, y in breakers if abs(x - 20.0) < 25 and abs(y - 50.0) < 13), 1)

    def test_turned_yard(self):
        # a yard along a diagonal: the kits turned with it
        c, s = np.cos(np.radians(30)), np.sin(np.radians(30))
        ring = square(-60, -30, 120, 60) @ np.array([[c, s], [-s, c]])
        got, _ = yard([ring])
        yaws = {round(yaw % 180, 3) for *_, yaw in got}
        self.assertEqual(yaws, {30.0})


if __name__ == "__main__":
    unittest.main()
