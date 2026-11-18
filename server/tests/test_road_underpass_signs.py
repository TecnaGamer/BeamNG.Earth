"""A road with a height limit (a maxheight sign: the roads layer's max_height_m rule) under a road bridge gets that limit
under the girders, not the 6 m guess (roads.road_sign_at / signed_road_clear): the deck's profile, its abutments' lifts
and the road's dip under it (after PR #922)."""
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import roads  # noqa: E402

FLAT = 10.0


def flat(xy):
    return np.full(len(xy), FLAT)


def line(length=400.0):
    x = np.arange(0.0, length + 1.0, 4.0)
    return np.c_[x, np.zeros(len(x))]


def limit(x, h):
    """A height limit's polyline across x (a road under running north-south there)."""
    return (np.array([[x, -10.0], [x, 10.0]]), h)


class SignAtTest(unittest.TestCase):
    def test_the_limit_near_the_crossing(self):
        lims = [limit(200.0, 4.2), limit(0.0, 3.0)]
        self.assertAlmostEqual(roads.road_sign_at(lims, (201.0, 0.0)), 4.2)
        self.assertIsNone(roads.road_sign_at(lims, (100.0, 0.0)))
        self.assertIsNone(roads.road_sign_at([], (201.0, 0.0)))

    def test_the_lowest_of_two(self):
        self.assertAlmostEqual(roads.road_sign_at([limit(200.0, 4.5), limit(201.0, 3.9)], (200.5, 0.0)), 3.9)

    def test_implausible_limits_left_out(self):
        self.assertIsNone(roads.road_sign_at([limit(200.0, 1.2)], (200.0, 0.0)))
        self.assertIsNone(roads.road_sign_at([limit(200.0, 12.0)], (200.0, 0.0)))

    def test_switched_off(self):
        with mock.patch.object(roads, "ROAD_SIGNED_ON", False):
            self.assertIsNone(roads.road_sign_at([limit(200.0, 4.2)], (200.0, 0.0)))

    def test_the_clearance_is_a_road_s(self):
        cl = roads.signed_road_clear(4.2, 1.5)
        self.assertIsInstance(cl, roads.RoadClear)
        self.assertAlmostEqual(cl, 4.2 + roads.SIGN_MARGIN_M + 1.5)
        self.assertTrue(roads._road_crossing((0, 1.0, 2.0, cl)))
        self.assertTrue(roads._road_crossing((0, 1.0, 2.0)))
        self.assertFalse(roads._road_crossing((0, 1.0, 2.0, 20.0)))     # (water, a railway: their own)


class DeckProfileTest(unittest.TestCase):
    def test_a_low_sign_keeps_the_deck_lower(self):
        xy = line()
        plain, = roads.deck_profile([xy], flat, [(0, 201.0, FLAT)])
        cl = roads.signed_road_clear(4.2)
        signed, = roads.deck_profile([xy], flat, [(0, 201.0, FLAT, cl)])
        top_plain = float(np.interp(201.0, xy[:, 0], plain))
        top_signed = float(np.interp(201.0, xy[:, 0], signed))
        self.assertLess(top_signed, top_plain - 1.0)
        # (still the sign, its margin and the deck's depth over the road, across the road's corridor)
        at = np.linspace(201.0 - roads.DECK_CLEAR_REACH_M, 201.0 + roads.DECK_CLEAR_REACH_M, 81)
        self.assertGreaterEqual(float(np.interp(at, xy[:, 0], signed).min()) - FLAT, float(cl) - 0.05)

    def test_a_tall_sign_raises_it(self):
        xy = line()
        plain, = roads.deck_profile([xy], flat, [(0, 201.0, FLAT)])
        tall, = roads.deck_profile([xy], flat, [(0, 201.0, FLAT, roads.signed_road_clear(7.5))])
        self.assertGreater(float(np.interp(201.0, xy[:, 0], tall)), float(np.interp(201.0, xy[:, 0], plain)) + 1.0)

    def test_a_signed_road_at_an_abutment_is_still_a_junction_at_grade(self):
        xy = line(60.0)
        z, = roads.deck_profile([xy], flat, [(0, 6.0, FLAT, roads.signed_road_clear(4.2))])
        self.assertLess(float(np.interp(6.0, xy[:, 0], z)), FLAT + roads.AT_GRADE_M)


class BridgeLiftsTest(unittest.TestCase):
    def test_a_low_sign_lifts_the_abutments_less(self):
        deck = np.c_[np.linspace(-35, 35, 36), np.zeros(36)]
        ground = [np.array([[-12.0, -500], [-12, 500]]), np.array([[12.0, 500], [12, -500]])]
        z = lambda xy: np.full(len(xy), FLAT)
        plain = dict((tuple(e), lf) for e, lf in grading.bridge_lifts(deck, [deck], ground, z))
        lims = [(np.array([[-12.0, -10.0], [-12.0, 10.0]]), 4.2), (np.array([[12.0, -10.0], [12.0, 10.0]]), 4.2)]
        signed = dict((tuple(e), lf) for e, lf in grading.bridge_lifts(deck, [deck], ground, z, limits=lims))
        self.assertEqual(set(plain), set(signed))
        depth = roads.deck_depth(roads.geom_span([deck]))
        for k in plain:
            self.assertAlmostEqual(plain[k] - signed[k], roads.deck_road_clear(depth) - roads.signed_road_clear(4.2, depth),
                                   delta=1e-6)

    def test_no_limits_as_before(self):
        deck = np.c_[np.linspace(-35, 35, 36), np.zeros(36)]
        ground = [np.array([[-12.0, -500], [-12, 500]])]
        z = lambda xy: np.full(len(xy), FLAT)
        self.assertEqual([lf for _e, lf in grading.bridge_lifts(deck, [deck], ground, z)],
                         [lf for _e, lf in grading.bridge_lifts(deck, [deck], ground, z, limits=[])])


class CorridorUnderDeckTest(unittest.TestCase):
    def test_a_raise_under_a_deck_is_found(self):
        xy = np.c_[np.linspace(-50, 50, 26), np.zeros(26)]
        deck = [(np.array([[0.0, -40.0], [0.0, 40.0]]), 6.0)]
        dz = np.where(np.abs(xy[:, 0]) < 10, 3.0, 0.0)
        self.assertTrue(grading._raised_under(xy, dz, deck))
        self.assertFalse(grading._raised_under(xy, dz * 0.2, deck))     # (a small raise: the corridors as before)
        far = np.where(np.abs(xy[:, 0] - 40) < 5, 3.0, 0.0)             # (raised beside, not under it)
        self.assertFalse(grading._raised_under(xy, far, deck))



class PiersBesideLimitsTest(unittest.TestCase):
    def test_a_chain_with_height_limits_keeps_its_piers(self):
        # the chain's ground holds its cells' height limits too (("lim", cell): (xy, m) pairs, no lines): its piers
        # read only the roads and railways (they raised on the limits, and the Bowker Overpass lost every pier)
        import types
        bp = object.__new__(roads.BridgeProfiles)
        bp._ground, bp.wet = flat, None
        xy = line(200.0)
        part = (types.SimpleNamespace(width=10.0), xy, np.full(len(xy), FLAT + 10.0), None)
        road = np.array([[100.0, -50.0], [100.0, 50.0]])
        plain = bp._precommit("k", part, {("r1", "g"): [road]})
        self.assertGreaterEqual(len(plain), 3)
        signed = bp._precommit("k", part, {("r1", "g"): [road], ("lim", "c"): [limit(100.0, 3.5)]})
        self.assertEqual(signed, plain)


class BridgeOwnLimitTest(unittest.TestCase):
    def test_a_limit_on_the_road_s_own_bridge_is_left_out(self):
        # a truss or covered bridge's own maxheight is not the clearance of a road under it (PR #1267 review)
        rows = [(3.0, 0.4, 0.6), (4.0, 0.0, 0.1)]                  # (on the bridge 0.3-0.7; on the ground)

        class Rule:
            feature = np.array([0, 0])

            def __len__(self):
                return len(rows)

            def for_feature(self, i):
                return rows

        class Feats:
            rules = {"max_height_m": Rule()}

            def __len__(self):
                return 1

            def parts(self, i):
                return [[np.array([[0.0, 0.0], [0.001, 0.0]])]]
        f = Feats()
        with mock.patch.object(roads, "tag_level_ranges", lambda feats, i: [(0.3, 0.7, "bridge")]):
            self.assertEqual(sorted(h for _r, h in roads.height_limit_lines(f)), [3.0, 4.0])
            self.assertEqual([h for _r, h in roads.height_limit_lines(f, on_bridges=False)], [4.0])



class CellLimitsShortReadTest(unittest.TestCase):
    """A cell whose height limits were read short (a revisit fault: a fallback read) is not kept by the limits memo:
    the next read asks again (and is short again, or complete once the data is there), so a later scope never takes
    the short limits without the fault (Codex review of PR #1267)."""

    def test_a_faulted_read_is_read_again(self):
        import revisit
        calls, fail = [0], [True]

        def read(key):
            calls[0] += 1
            if fail[0]:
                revisit.fault()
                return []
            return [(np.zeros((2, 2)), 4.0)]
        bp = roads.BridgeProfiles(None, None, None)
        with mock.patch.object(roads.BridgeProfiles, "_read_limits", lambda self, key: read(key)):
            with revisit.scope() as b1:
                self.assertEqual(bp._cell_limits("k"), [])
            self.assertFalse(b1.complete, "the short read's fault reaches the caller's scope")
            fail[0] = False
            with revisit.scope() as b2:
                got = bp._cell_limits("k")
            self.assertTrue(b2.complete)
            self.assertEqual([h for _xy, h in got], [4.0], "read again once the data is there")
            with revisit.scope() as b3:
                bp._cell_limits("k")
            self.assertEqual(calls[0], 2, "a complete read is kept")


if __name__ == "__main__":
    unittest.main()
