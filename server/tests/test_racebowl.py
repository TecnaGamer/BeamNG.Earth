"""Ring buildings (buildings.ring_pieces: a footprint's inner ring honoured) and the racing band rule
(racefurniture.race_stand: no building of any class over a raceway's carriageway + shoulder)."""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import buildings as B
import racefurniture as rf


def circle(r, n=48, cx=0.0, cy=0.0, wobble=0.0):
    t = np.linspace(0, 2 * math.pi, n, endpoint=False)
    rr = r * (1 + wobble * np.sin(5 * t))
    return np.c_[cx + rr * np.cos(t), cy + rr * np.sin(t)]


def oval(r=150.0, n=120):
    t = np.linspace(0, 2 * math.pi, n + 1)
    return np.c_[1.6 * r * np.cos(t), r * np.sin(t)]


class RingPieces(unittest.TestCase):
    def test_annulus_covered_hole_empty(self):
        outer, hole = B.clean_ring(circle(200, wobble=0.15)), circle(120)
        pcs = B.ring_pieces(outer, [hole])
        self.assertGreater(len(pcs), 1)
        want = B._area2(outer) / 2 - B._area2(B.clean_ring(hole)) / 2
        self.assertAlmostEqual(sum(B._area2(p) / 2 for p in pcs), want, delta=want * 0.01)
        for p in pcs:
            self.assertFalse(B._inside(p, (0.0, 0.0)))
            self.assertFalse(B._inside(p, (100.0, 0.0)))     # inside the hole
            self.assertGreater(B._area2(p), 0)

    def test_no_hole_is_outer(self):
        outer = B.clean_ring(circle(50))
        self.assertIs(B.ring_pieces(outer, [])[0], outer)
        self.assertEqual(len(B.ring_pieces(outer, [circle(1.0)])), 1)     # (a hole too small to count)

    def test_not_star_shaped_hole_is_outer(self):
        hole = np.array([[-50, -50], [50, -50], [50, 50], [40, 50], [40, -40], [-40, -40], [-40, 50], [-50, 50.]])
        outer = B.clean_ring(circle(200))
        self.assertEqual(len(B.ring_pieces(outer, [hole])), 1)


class RaceBand(unittest.TestCase):
    race = [oval()]
    halves = [6.0]

    def test_lid_over_oval_never_drawn(self):
        lid = circle(400)               # a bowl's roof over the whole track: never an ordinary building
        self.assertTrue(rf.on_band(lid, self.race, self.halves))
        self.assertIn(rf.race_stand(lid, "stadium", self.race, self.halves), ("stand", "drop"))
        self.assertIn(rf.race_stand(lid, "yes", self.race, self.halves), ("stand", "drop"))

    def test_any_class_on_band_drops(self):
        shed = np.array([[236, -5], [246, -5], [246, 5], [236, 5.]])     # across the track at (240, 0)
        self.assertEqual(rf.race_stand(shed, "house", self.race, self.halves), "drop")

    def test_far_building_untouched(self):
        far = np.array([[400, -5], [410, -5], [410, 5], [400, 5.]])
        self.assertIsNone(rf.race_stand(far, "house", self.race, self.halves))

    def test_bowl_pieces_become_stands_facing_track(self):
        # a bowl round the oval, its inner ring at the wall: every piece seats, each model's front to the track
        outer = B.clean_ring(np.c_[1.6 * 230 * np.cos(np.linspace(0, 6.28, 80, endpoint=False)),
                                   230 * np.sin(np.linspace(0, 6.28, 80, endpoint=False))])
        hole = np.c_[1.6 * 158 * np.cos(np.linspace(0, 6.28, 80, endpoint=False)),
                     158 * np.sin(np.linspace(0, 6.28, 80, endpoint=False))]
        pcs = B.ring_pieces(outer, [hole])
        self.assertGreater(len(pcs), 4)
        n = 0
        for p in pcs:
            self.assertEqual(rf.race_stand(p, "stadium", self.race, self.halves), "stand")
            for c, ax, s, front in rf.stand_pieces(p, self.race, self.halves):
                n += 1
                self.assertLess(np.dot(front, c), 0.0)          # facing in, to the track
                box = rf._box_pts(c, ax, front, rf.STAND_LEN * s, rf.STAND_DEPTH * s)
                self.assertGreaterEqual(float(rf.Segs(self.race, self.halves).edge(box).min()), rf.CLEAR_M - 1e-6)
        self.assertGreater(n, len(pcs))

    def test_big_unclassed_near_track_is_stand(self):
        # a 300 m x 40 m bleacher along the straight's outside, 5 m off the band: seating, not a block
        y0 = 150 + 6 + 5
        st = np.array([[-150, y0], [150, y0], [150, y0 + 40], [-150, y0 + 40.]])
        self.assertEqual(rf.race_stand(st, "", [np.array([[-300, 150.], [300, 150.]])], self.halves), "stand")
        small = np.array([[-20, y0], [20, y0], [20, y0 + 20], [-20, y0 + 20.]])
        self.assertIsNone(rf.race_stand(small, "", [np.array([[-300, 150.], [300, 150.]])], self.halves))

    def test_gather_stands_from_ring_building(self):
        tr = rf.Track()
        tr.race, tr.race_half = self.race, self.halves
        outer = np.c_[1.6 * 230 * np.cos(np.linspace(0, 6.28, 80)), 230 * np.sin(np.linspace(0, 6.28, 80))]
        hole = np.c_[1.6 * 158 * np.cos(np.linspace(0, 6.28, 80)), 158 * np.sin(np.linspace(0, 6.28, 80))]
        rf._stands(tr, [("bowl", "stadium", [outer, hole])])
        self.assertGreater(len(tr.stands), 4)
        self.assertTrue(all(fid.startswith("bowl#") for fid, _ in tr.stands))


if __name__ == "__main__":
    unittest.main()
