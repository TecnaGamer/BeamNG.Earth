"""The GA-400 / I-285 rebuild (Atlanta, 2020-2023, over Georgia's 2018 lidar): its decks stood 1.4-4.6 m over the roads
under them, one on I-285 itself (the user, 2026-11-21). The rules that held them low:
 - grading.bridge_lifts took another cell's copy of a deck for a part joined at both its ends: no abutment raised
   (BRIDGE_COPY_ON; 21249a5a over Perimeter Center, 1.4 m where the inventory gives 6.8);
 - roads.deck_profile asked an upper deck's clearance over a lower one before the overlap merge raised the lower
   (STACK_RECHECK), and none between two crossing decks of the same layer (SAME_LEVEL_APART_M; e33ee9a3 over 3e166703,
   4.1 m where the inventory gives 6.5)."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grading  # noqa: E402
import roads  # noqa: E402

DECK = np.c_[np.linspace(0.0, 116.0, 30), np.zeros(30)]
ROAD = np.array([[60.0, -40.0], [60.0, 40.0]])          # (a street crossing under its middle)


def flat(xy):
    return np.zeros(len(np.asarray(xy).reshape(-1, 2)))


class BridgeCopyTest(unittest.TestCase):
    def test_a_copy_of_the_deck_is_no_joint(self):
        copy = DECK + 1e-5
        got = grading.bridge_lifts(DECK, [DECK, copy], [ROAD], flat)
        self.assertEqual(len(got), 2)                   # (both abutments raised, as with the deck alone)
        alone = grading.bridge_lifts(DECK, [DECK], [ROAD], flat)
        np.testing.assert_allclose(sorted(l for _e, l in got), sorted(l for _e, l in alone))

    def test_a_reversed_copy_too(self):
        got = grading.bridge_lifts(DECK, [DECK, DECK[::-1] + 1e-5], [ROAD], flat)
        self.assertEqual(len(got), 2)

    def test_a_real_next_part_is_still_a_joint(self):
        nxt = np.c_[np.linspace(116.0, 200.0, 10), np.zeros(10)]
        got = grading.bridge_lifts(DECK, [DECK, nxt], [ROAD], flat)
        ends = [tuple(np.round(e, 1)) for e, _l in got]
        self.assertNotIn((116.0, 0.0), ends)            # (its far end goes on onto the next part)

    def test_off_as_before(self):
        old = grading.BRIDGE_COPY_ON
        grading.BRIDGE_COPY_ON = False
        try:
            self.assertEqual(grading.bridge_lifts(DECK, [DECK, DECK + 1e-5], [ROAD], flat), [])
        finally:
            grading.BRIDGE_COPY_ON = old


class CrossAngleTest(unittest.TestCase):
    def test_across_and_beside(self):
        deck = np.array([[0.0, 0.0], [100.0, 0.0]])
        self.assertAlmostEqual(grading._cross_angle(deck, 50.0, np.array([[50.0, -20.0], [50.0, 20.0]]),
                                                    np.array([50.0, 0.0])), 90.0, places=6)
        beside = np.array([[0.0, -2.0], [100.0, 8.5]])      # (6 degrees: the other carriageway, I-75 at Forest Park)
        self.assertLess(grading._cross_angle(deck, 50.0, beside, np.array([19.0, 0.0])), grading.BUILT_CROSS_DEG)


def grade10(m, step=4.0):
    """The steepest grade over 12 m (3 vertices): what a car feels, not one 4 m segment."""
    return float(np.abs(m[3:] - m[:-3]).max() / (3 * step))


class FreewayGradeCapTest(unittest.TestCase):
    S = np.arange(0.0, 200.0, 4.0)
    XY = np.c_[S, 0 * S]

    def test_a_stale_hill_is_cut_to_the_cap(self):
        z = np.where((self.S > 20) & (self.S < 60), 320 + np.minimum(self.S - 20, 60 - self.S) * 0.25, 320.0)
        m = grading.freeway_grade_cap(self.XY, z, 0.07)
        self.assertLessEqual(grade10(m), 0.09)                  # (25 % before; its corners rounded into curves)
        self.assertEqual((m[0], m[-1]), (z[0], z[-1]))
        self.assertTrue((m <= z + 1e-9).all())

    def test_a_dip_is_not_filled_by_default(self):
        z = np.where((self.S > 80) & (self.S < 120), 314.0, 320.0)        # (a freeway dipped under a deck)
        m = grading.freeway_grade_cap(self.XY, z, 0.07)
        self.assertTrue(m is None or (m <= z + 1e-9).all())

    def test_a_hollow_away_from_decks_is_filled(self):
        z = np.where((self.S > 80) & (self.S < 120), 314.0, 320.0)
        m = grading.freeway_grade_cap(self.XY, z, 0.07, np.ones(len(z), bool))
        self.assertLessEqual(grade10(m), 0.075)
        self.assertGreater(float(m[25]), 314.0)

    def test_a_gentle_road_is_left(self):
        self.assertIsNone(grading.freeway_grade_cap(self.XY, 320 + 0.05 * self.S, 0.07))


class BuiltRampTest(unittest.TestCase):
    def test_a_carriageway_alongside_is_no_cap(self):
        ramp = np.c_[np.linspace(0, 200, 40), np.zeros(40)]
        beside = np.c_[np.linspace(-50, 250, 30), np.full(30, 9.0)]          # (edge to edge, parallel)
        across = np.array([[100.0, -40.0], [100.0, 40.0]])
        got = grading.built_others(ramp, False, 6.0, [(beside, 6.0), (across, 4.0)])
        self.assertEqual(len(got), 1)
        self.assertIs(got[0][0], across)

    def test_room_for_a_ramp(self):
        self.assertFalse(grading._room_for(np.array([[0.0, 0.0], [23.0, 0.0]]), 4.9))
        self.assertTrue(grading._room_for(np.array([[0.0, 0.0], [227.0, 0.0]]), 3.4))


class MeasuredSpanTest(unittest.TestCase):
    """Hammond Drive over GA-400 (33.9190 N 84.3577 W): a 114 m deck measured climbing 5.1 m end to end on its ground;
    the grade match levelled it, a 1.6-2.4 m step onto each end (the user, 2026-11-21)."""
    LINE = np.c_[np.linspace(0.0, 114.0, 30), np.zeros(30)]

    def spans(self, measured, built=False):
        from unittest import mock
        import roadz

        class Prov:
            enabled = True

            def observed(self, frame, xy, half, level, name=""):
                return np.asarray(measured, np.float64)
        me = type("P", (), {"frame": None})()
        with mock.patch.object(roadz, "PROVIDER", Prov()), mock.patch.object(roads, "built_after_survey",
                                                                               lambda f, xy: built):
            return grading._Planner._measured_spans(me, self.LINE, np.array([304.56, 309.86]))

    def test_measured_end_to_end_on_its_ground(self):
        self.assertTrue(self.spans(np.linspace(304.83, 309.89, 30)))

    def test_measured_off_its_ground_or_built_after_or_unmeasured(self):
        self.assertFalse(self.spans(np.linspace(306.5, 307.4, 30)))
        self.assertFalse(self.spans(np.linspace(304.83, 309.89, 30), built=True))
        m = np.linspace(304.83, 309.89, 30)
        m[0] = np.nan
        self.assertFalse(self.spans(m))


class StackShortTest(unittest.TestCase):
    ARCS = [np.array([0.0, 100.0]), np.array([0.0, 100.0])]
    OVER = [(0, 1, 50.0, 50.0)]

    def test_across_levels(self):
        zs = [np.array([300.0, 300.0]), np.array([306.0, 306.0])]
        self.assertTrue(roads._stack_short(self.OVER, [1.0, 2.0], self.ARCS, zs, 7.6, 0.3))
        zs[1] = zs[1] + 2.0
        self.assertFalse(roads._stack_short(self.OVER, [1.0, 2.0], self.ARCS, zs, 7.6, 0.3))

    def test_one_layer_far_apart_is_one_over_the_other(self):
        zs = [np.array([300.0, 300.0]), np.array([306.0, 306.0])]
        self.assertTrue(roads._stack_short(self.OVER, [2.0, 2.0], self.ARCS, zs, 7.6, 0.3))
        extra = roads._stack_extra(self.OVER, [], [2.0, 2.0], self.ARCS, zs)
        self.assertEqual([(e[0], e[2]) for e in extra], [(1, 300.0)])   # (the higher over the lower's height)

    def test_one_layer_close_is_a_junction(self):
        zs = [np.array([300.0, 300.0]), np.array([301.0, 301.0])]
        self.assertFalse(roads._stack_short(self.OVER, [2.0, 2.0], self.ARCS, zs, 7.6, 0.3))
        self.assertEqual(roads._stack_extra(self.OVER, [], [2.0, 2.0], self.ARCS, zs), [])


if __name__ == "__main__":
    unittest.main()
