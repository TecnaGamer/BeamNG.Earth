"""What a rail bed lattice node costs: the same beds as when its passes were written as loops over every point and every
piece (railways._envelope, roads._pieces_near_parts' boxes, gradelaw.road_grade's table), to the last bit."""
import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import gradelaw  # noqa: E402
import railways  # noqa: E402
import roads  # noqa: E402


def envelope_loop(s, req, grade=railways.GRADE):
    """_envelope as it was written: a forward and a backward pass over every point, in Python floats."""
    out = np.array(req, np.float64).tolist()
    rise = (grade * np.diff(np.asarray(s)[:len(out)])).tolist()
    for k in range(1, len(rise) + 1):
        v = out[k - 1] - rise[k - 1]
        if v > out[k]:
            out[k] = v
    for k in range(len(rise) - 1, -1, -1):
        v = out[k + 1] - rise[k]
        if v > out[k]:
            out[k] = v
    return np.array(out, np.float64)


def same_bits(a, b):
    return a.shape == b.shape and a.tobytes() == b.tobytes()


class EnvelopeTest(unittest.TestCase):
    def test_every_kind_of_line_comes_out_bit_for_bit_as_the_loops_gave_it(self):
        rng = np.random.default_rng(11)
        moved = 0
        for trial in range(1500):
            n = int(rng.integers(1, 400))
            s = np.cumsum(rng.uniform(0.5, 6.0, n))
            kind = trial % 6
            if kind == 0:
                req = rng.normal(0, 3, n)                           # (steep everywhere: runs of every length)
            elif kind == 1:
                req = np.cumsum(rng.normal(0, 0.05, n))             # (within the grade: nothing to do)
            elif kind == 2:
                req = np.cumsum(rng.normal(0, 0.4, n))
            elif kind == 3:
                req = np.cumsum(rng.normal(0, 0.05, n))
                req[rng.integers(0, n, 3)] += 20.0                  # (spikes: the line comes up to them both ways)
            elif kind == 4:
                req = np.cumsum(rng.normal(0, 0.05, n))
                req[rng.integers(0, n, 3)] = np.nan
                req[rng.integers(0, n)] = np.inf
            else:
                req = np.full(n, 5.0)
            for arc in (s, s[:max(1, n - 2)]):                      # (an arc shorter than the line, as the caller may give)
                want, got = envelope_loop(arc, req), railways._envelope(arc, req)
                self.assertTrue(same_bits(want, got), (trial, kind))
                moved += int((want != np.asarray(req, np.float64)).sum() > 0)
        self.assertGreater(moved, 500, "the trials move lines")

    def test_the_line_given_is_not_changed(self):
        s, req = np.arange(10.0) * 5.0, np.array([0, 0, 9, 0, 0, 0, 0, 0, 0, 0], np.float64)
        keep = req.copy()
        got = railways._envelope(s, req)
        self.assertTrue((req == keep).all())
        self.assertEqual(float(got[2]), 9.0)
        self.assertAlmostEqual(float(got[1]), 9.0 - railways.GRADE * 5.0)

    def test_empty_and_single_point_lines(self):
        for n in (0, 1):
            got = railways._envelope(np.arange(float(n)), np.zeros(n))
            self.assertEqual(len(got), n)


def boxes_loop(pieces):
    return np.array([[q[:, 0].min(), q[:, 1].min(), q[:, 0].max(), q[:, 1].max()] for q in pieces], np.float64)


class PieceBoxesTest(unittest.TestCase):
    def test_the_boxes_of_many_pieces_in_one_pass_are_the_boxes_one_by_one(self):
        rng = np.random.default_rng(5)
        for trial in range(40):
            pieces = [rng.normal(0, 300, (int(rng.integers(1, 30)), int(rng.integers(2, 4)))) for _ in range(int(rng.integers(1, 200)))]
            self.assertTrue(same_bits(roads._piece_boxes(pieces), boxes_loop(pieces)))


class StructureRunTest(unittest.TestCase):
    def test_the_nodes_made_once_give_the_runs_made_every_time(self):
        def seg(x0, x1):
            return np.c_[np.linspace(x0, x1, 11), np.zeros(11)]
        # a ground track, a bridge, a tunnel and a bridge end to end, a ground track
        rails = [("g0", "rail", "ground", seg(0, 100)), ("b1", "rail", "bridge", seg(100, 200)),
                 ("t2", "rail", "tunnel", seg(200, 300)), ("b3", "rail", "bridge", seg(300, 400)),
                 ("g4", "rail", "ground", seg(400, 500))]
        _nodes, edges, _adj = roads._chain_graph([r[3] for r in rails])
        ends = {edges[0][1]: [0.0], edges[4][0]: [0.0]}
        at = railways._structure_at(rails, edges)
        for n in (1, 2, 3):
            for sc in (0.0, 30.0, 70.0):
                self.assertEqual(railways._structure_run(rails, edges, ends, n, sc),
                                 railways._structure_run(rails, edges, ends, n, sc, at))
        run = railways._structure_run(rails, edges, ends, 2, 50.0, at)
        self.assertEqual(run, (150.0, 150.0))      # (to the ground ends: the bridge, tunnel and bridge are one structure)


class RoadGradeTest(unittest.TestCase):
    def test_the_memo_gives_what_the_law_gives(self):
        for cls in (None, "motorway", "motorway_link", "trunk", "primary", "residential", "service", "path", "footway", "track"):
            for kmh in (None, 0, 20, 33.5, 50, 50.0, 90, 130):
                self.assertEqual(gradelaw.road_grade(cls, kmh), gradelaw.road_grade.__wrapped__(cls, kmh))
                self.assertEqual(gradelaw.road_grade(cls, kmh), gradelaw.road_grade(cls, kmh))


if __name__ == "__main__":
    unittest.main()
