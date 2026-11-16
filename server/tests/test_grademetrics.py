"""grademetrics: the graded roads' numbers (docs/VERTICAL_SOLVER_RFC.md step 1), on plans made by hand."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import grademetrics as gm  # noqa: E402
import grading  # noqa: E402
import roads  # noqa: E402


def plan(xy, z, half=4.0, rid=0):
    p = grading.Plan(np.asarray(xy, np.float64), np.asarray(z, np.float64), half)
    p.rid = rid
    return p


S = np.arange(0.0, 101.0, 2.0)


class JunctionTest(unittest.TestCase):

    def test_a_side_road_onto_a_main_roads_middle(self):
        main = plan(np.c_[S, np.zeros_like(S)], np.full(len(S), 10.0), rid=1)
        side = plan(np.c_[np.full(len(S), 50.0), S], 10.4 + 0.01 * S, rid=2)     # (its end 0.4 m over the main road)
        rows = gm.junction_steps([("main", main), ("side", side)])
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["dz"], 0.4, places=9)
        self.assertEqual({rows[0]["a"], rows[0]["b"]}, {"main", "side"})

    def test_ends_meeting_ends_once_and_crossings_are_no_nodes(self):
        a = plan(np.c_[S, np.zeros_like(S)], np.full(len(S), 5.0), rid=1)
        b = plan(np.c_[S + 100.0, np.zeros_like(S)], np.full(len(S), 5.1), rid=1)    # (the same road's next piece)
        c = plan(np.c_[np.full(len(S), 30.0), S - 50.0], np.full(len(S), 9.0), rid=3)   # (crosses a's middle: no end there)
        rows = gm.junction_steps([("a", a), ("b", b), ("c", c)])
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["dz"], 0.1, places=9)

    def test_ends_on_the_cells_edge_are_seams_not_nodes(self):
        a = plan(np.c_[S, np.zeros_like(S)], np.full(len(S), 5.0), rid=1)
        b = plan(np.c_[np.full(len(S), 100.0), S - 50.0], np.full(len(S), 7.0), rid=2)  # (along the box's east edge)
        self.assertEqual(len(gm.junction_steps([("a", a), ("b", b)])), 1)
        self.assertEqual(gm.junction_steps([("a", a), ("b", b)], box=(-500.0, -500.0, 100.0, 500.0)), [])


class SeamTest(unittest.TestCase):

    def test_the_same_road_across_two_cells(self):
        west = plan(np.c_[S, np.zeros_like(S)], np.full(len(S), 5.0), rid=7)
        east = plan(np.c_[S + 100.0, np.zeros_like(S)], np.full(len(S), 5.25), rid=7)
        other = plan(np.c_[S + 100.0, np.zeros_like(S) + 0.5], np.full(len(S), 9.0), rid=8)   # (another road there)
        rows = gm.seam_steps({"w": ([("r", west)], (-100.0, -100.0, 100.0, 100.0)),
                              "e": ([("r", east), ("o", other)], (100.0, -100.0, 300.0, 100.0))})
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["dz"], 0.25, places=9)


class ProfileTest(unittest.TestCase):

    def test_a_ramp_the_plans_made_is_steep_a_steep_street_is_not(self):
        flat = np.zeros_like(S)
        ramp = np.clip((S - 40.0) * 0.15, 0.0, 3.0)        # (15 % on flat ground)
        made = plan(np.c_[S, flat], ramp, rid=1)
        hill = plan(np.c_[S, flat + 50.0], 0.15 * S, rid=2)  # (15 % on a 15 % hill)

        def ground(xy):
            return np.where(xy[:, 1] > 25.0, 0.15 * xy[:, 0], 0.0)
        out = {p["fid"]: p for p in gm.profiles([("made", made), ("hill", hill)], {"made": 0.09, "hill": 0.09}, ground)}
        self.assertGreater(out["made"]["over"], 0)
        self.assertGreater(out["made"]["made_steep"], 0)
        self.assertGreater(out["hill"]["over"], 0)
        self.assertEqual(out["hill"]["made_steep"], 0)
        self.assertAlmostEqual(out["made"]["grade"], 0.15, places=6)
        self.assertGreater(out["made"]["bump"], 0.25)      # (the kinks: 15 % gained in one 2 m step, x 2 m)
        self.assertAlmostEqual(out["hill"]["bump"], 0.0, places=9)
        self.assertAlmostEqual(float(out["hill"]["dev_abs"].max()), 0.0, places=9)
        self.assertAlmostEqual(float(out["made"]["dev"].max()), 3.0, places=9)

    def test_roads_without_a_ceiling_are_not_measured(self):
        p = plan(np.c_[S, np.zeros_like(S)], np.zeros_like(S))
        self.assertEqual(gm.profiles([("rail:1", p)], {}, lambda xy: np.zeros(len(xy))), [])


class ClearanceTest(unittest.TestCase):

    def test_a_road_too_close_under_a_deck(self):
        need = float(roads.DECK_ROAD_CLEAR)
        road = plan(np.c_[S, np.zeros_like(S)], np.zeros_like(S))
        deck_xy = np.c_[np.full(21, 50.0), np.linspace(-20.0, 20.0, 21)]
        low = [("deck", deck_xy, np.full(21, need - 1.5), 6.0)]
        high = [("deck", deck_xy, np.full(21, need + 0.5), 6.0)]
        own = [("deck", deck_xy, np.full(21, 0.2), 6.0)]          # (level with the road: its own bridge, not over it)
        rows = gm.clearance([("road", road)], low, {"road"})
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["dz"], 1.5, places=6)
        self.assertAlmostEqual(rows[0]["from_end"], 20.0, places=6)    # (under the deck's middle)
        self.assertEqual(gm.summary([], [], [], rows)["clearance"]["through_deck"], 0)
        through = gm.clearance([("road", road)], [("deck", deck_xy, np.full(21, 1.2), 6.0)], {"road"})
        self.assertEqual(gm.summary([], [], [], through)["clearance"]["through_deck"], 1)
        self.assertEqual(gm.clearance([("road", road)], high, {"road"}), [])
        self.assertEqual(gm.clearance([("road", road)], own, {"road"}), [])
        self.assertEqual(gm.clearance([("road", road)], low, set()), [])


class SummaryTest(unittest.TestCase):

    def test_counts(self):
        rows = [{"x": 0.0, "y": 0.0, "dz": d, "a": "a", "b": "b"} for d in (0.05, 0.2, 0.35, 1.0)]
        s = gm.summary(rows, [], [], rows[:1])
        self.assertEqual((s["junction"]["n"], s["junction"]["bad"], s["junction"]["over_10cm"]), (4, 2, 3))
        self.assertAlmostEqual(s["junction"]["max"], 1.0)
        self.assertEqual(s["junction"]["worst"][0]["dz"], 1.0)
        self.assertEqual(s["clearance"]["n"], 1)
        self.assertEqual(s["roads"], 0)


if __name__ == "__main__":
    unittest.main()
