"""airfieldsigns: a red hold sign each side of a taxiway where it meets a runway, with the runway's designators in the
order the pilot sees them; location and direction signs only at junctions of ways that both have a ref; signs stand off the
pavement; Features of anywhere else give nothing."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import airfieldsigns as afs  # noqa: E402
import test_taxiwaymarks as ttm  # noqa: E402

FRAME = ttm.FRAME


def runway_ref(ref, *pts):
    return (ttm.line(*pts), "runway", "airport", {"width": "45", "ref": ref}, None)


def metres(sign):
    e, n, _ = FRAME.geodetic_to_enu(sign.lat, sign.lon, 0.0)
    return np.array([float(e), float(n)])


def way_dist(p, a, b):
    a, b = np.array(a, float), np.array(b, float)
    t = np.clip((p - a) @ (b - a) / ((b - a) @ (b - a)), 0, 1)
    return float(np.hypot(*(a + t * (b - a) - p)))


class HoldSigns(unittest.TestCase):
    def setUp(self):
        # a runway along north (x = 0), 01R at the south end; a taxiway runs east-west into it
        self.feats = ttm.feats(runway_ref("01R/19L", (0, -1000), (0, 1000)),
                               ttm.taxiway((300, 0), (0, 0), tags={"ref": "A"}))

    def test_a_red_sign_each_side_with_the_designators(self):
        got = [s for s in afs.signs([self.feats]) if s.kind == "hold"]
        self.assertEqual(len(got), 2)
        for s in got:
            self.assertEqual(s.kind, "hold")
        sides = sorted(metres(s)[1] for s in got)
        self.assertLess(sides[0], 0.0)
        self.assertGreater(sides[1], 0.0)
        # the pilot comes in from the east heading west: his left is south, where 01R is: it is first
        self.assertEqual({s.text for s in got}, {"01R-19L"})

    def test_signs_stand_off_the_pavement(self):
        for s in afs.signs([self.feats]):
            p = metres(s)
            self.assertGreater(abs(p[1]) - s.w / 2.0, 7.5 + 4.0)

    def test_no_name_no_sign(self):
        f = ttm.feats(ttm.runway((0, -1000), (0, 1000)), ttm.taxiway((300, 0), (0, 0), tags={"ref": "A"}))
        self.assertEqual([s for s in afs.signs([f]) if s.kind == "hold"], [])

    def test_nothing_without_airport_features(self):
        self.assertEqual(afs.signs([None]), [])
        f = ttm.feats((ttm.line((0, 0), (100, 0)), "residential", "road", {}, None))
        self.assertEqual(afs.signs([f]), [])


class JunctionSigns(unittest.TestCase):
    def junction(self, ref_a, ref_b):
        a = ttm.taxiway((-200, 0), (0, 0), tags={"ref": ref_a} if ref_a else {})
        a2 = ttm.taxiway((0, 0), (200, 0), tags={"ref": ref_a} if ref_a else {})
        b = ttm.taxiway((0, 0), (0, 200), tags={"ref": ref_b} if ref_b else {})
        return ttm.feats(a, a2, b)

    def test_location_and_direction_where_both_have_refs(self):
        got = afs.signs([self.junction("A", "B3")])
        kinds = {s.kind for s in got}
        self.assertEqual(kinds, {"location", "direction"})
        self.assertTrue(any(s.kind == "location" and s.text == "A" for s in got))
        self.assertTrue(any(s.kind == "direction" and "B3" in s.text for s in got))
        self.assertTrue(any(s.kind == "location" and s.text == "B3" for s in got))

    def test_left_turn_arrow_and_left_side(self):
        got = afs.signs([self.junction("A", "B3")])
        # a pilot heading east on A (from x < 0) has B3 on his left (north): a left arrow, the board north of the way
        east = [s for s in got if s.kind == "direction" and "B3" in s.text and metres(s)[0] < 0 and metres(s)[1] > 0]
        self.assertTrue(east)
        self.assertTrue(all("\u2190" in s.text or "\u2196" in s.text for s in east))

    def test_nothing_where_a_way_has_no_ref(self):
        self.assertEqual(afs.signs([self.junction("A", None)]), [])
        self.assertEqual(afs.signs([self.junction(None, "B3")]), [])
        self.assertEqual(afs.signs([self.junction("A", "South Taxilane")]), [])

    def test_the_same_taxiway_going_on_is_no_turn(self):
        a = ttm.taxiway((-200, 0), (0, 0), tags={"ref": "A"})
        a2 = ttm.taxiway((0, 0), (200, 0), tags={"ref": "A"})
        self.assertEqual(afs.signs([ttm.feats(a, a2)]), [])

    def test_off_the_pavement(self):
        for s in afs.signs([self.junction("A", "B3")]):
            p = metres(s)
            self.assertGreater(way_dist(p, (-200, 0), (200, 0)), 7.5 + 3.0)
            self.assertGreater(way_dist(p, (0, 0), (0, 200)), 7.5 + 3.0)


class StandSigns(unittest.TestCase):
    """A stand's ref gives a black-on-yellow sign ahead of the stop bar, to the nose's left, facing the aircraft."""

    def setUp(self):
        self.feats = ttm.feats(ttm.taxiway((-120, 0), (100, 0), cls="taxilane"))
        self.craft = ttm.craft("narrow", 0.0, 60.0, 0.0, 1.0)               # nose north, 60 + L/2 north of the origin

    def test_a_ref_gives_a_sign_beside_the_nose_facing_the_aircraft(self):
        got = [s for s in afs.signs([self.feats], stands=[self.craft._replace(ref="A12")]) if s.kind == "stand"]
        self.assertEqual([s.text for s in got], ["A12"])
        p = metres(got[0])
        nose = 60.0 + ttm.pa.SPECS["narrow"][0] / 2.0
        self.assertAlmostEqual(p[0], -afs.STAND_LEFT_M, 2)                  # (north-facing nose: its left is west)
        self.assertAlmostEqual(p[1], nose + afs.STAND_AHEAD_M, 2)
        self.assertAlmostEqual(got[0].face, 180.0, 1)                       # (looking south, at the aircraft)
        self.assertEqual(afs.STYLES["stand"][:2], (afs.YELLOW, afs.BLACK))

    def test_a_stand_board_is_double_sided_and_the_others_single(self):
        sg = [s for s in afs.signs([self.feats], stands=[self.craft._replace(ref="A12")]) if s.kind == "stand"][0]
        rows = afs._board_rows(sg, 10.0, 20.0, 5.0, 0.7, 0.4, 3, 2.1)
        self.assertEqual(len(rows), 2)                                      # (a board each way, back to back)
        d = [np.array(r[6:8]) for r in rows]
        self.assertLess(float(d[0] @ d[1]), -0.999)                         # (turned round: the text on both faces)
        out = [np.array(r[2:4]) - np.array([10.0, 20.0]) for r in rows]
        face = np.array([np.sin(np.radians(sg.face)), np.cos(np.radians(sg.face))])
        for dv, o in zip(d, out):                                           # (each board's face looks away from the other: -dv is its normal)
            self.assertAlmostEqual(float(o @ -dv), afs.BOARD_GAP_M, 6)
        self.assertGreater(float(np.hypot(*(out[0] - out[1]))), 0.0)
        for kind in ("hold", "location", "direction"):
            one = afs._board_rows(afs.Sign(kind, "X", 0.0, 0.0, 90.0, 1.0, 1.0), 1.0, 2.0, 0.0, 1.0, 1.0, 1, 1.0)
            self.assertEqual(len(one), 1)
            self.assertEqual(one[0][2:4], [1.0, 2.0])
        self.assertTrue(any(float(-dv @ face) > 0.999 for dv in d))         # (one of them faces the approaching aircraft)

    def test_no_ref_no_sign(self):
        self.assertEqual(afs.signs([self.feats], stands=[self.craft]), [])
        self.assertEqual(afs.signs([self.feats], stands=[self.craft._replace(ref="TOOLONGREF")]), [])
        self.assertEqual(afs.signs([self.feats], stands=[self.craft._replace(ref="7", mode="ga")]), [])


class Designators(unittest.TestCase):
    def test_designators(self):
        self.assertEqual(afs.designators("01R/19L"), ["01R", "19L"])
        self.assertEqual(afs.designators("9/27"), ["09", "27"])
        self.assertEqual(afs.designators("09/20"), [])
        self.assertEqual(afs.designators("ILS"), [])
        self.assertEqual(afs.designators(None), [])


if __name__ == "__main__":
    unittest.main()
