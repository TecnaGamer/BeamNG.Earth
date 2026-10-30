"""Ground stretches the lidar shows on a structure (roads.lidar_gap_candidates / _lidar_gaps, roadz.RoadZ._measure /
gap_answers, earthfast.lidar_gap_stretch): a ground piece going on from a deck's end is deck as far as its measured
surface stays well over the DTM; an at-grade ramp stays ground; without the lidar the rule stands, the same in every
process of a server run."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import mapcells  # noqa: E402
import revisit  # noqa: E402
import roads  # noqa: E402
import roadz  # noqa: E402
from test_roads import features, FRAME  # noqa: E402
from test_roadz import FakeMDem  # noqa: E402

ef = fast._ef
NATIVE = fast.AVAILABLE and getattr(ef, "VERSION", 0) >= roadz.GAP_NATIVE
DTM = 10.0


def ll(x, y):
    la, lo, _ = FRAME.enu_to_geodetic(np.array([float(x)]), np.array([float(y)]), 0.0)
    return float(lo[0]), float(la[0])


def cloud(surface, x0, x1, y, half=5.0, n=4000, seed=1):
    """Road surface points (class 1) z = surface(x) over x0..x1 along the line at y, and ground (class 2) at the DTM
    beside it."""
    rng = np.random.default_rng(seed)
    x = rng.uniform(x0, x1, n)
    yy = y + rng.uniform(-half, half, n)
    z = surface(x) + rng.normal(0, 0.03, n)
    gx = rng.uniform(x0, x1, n // 4)
    g = np.c_[gx, y + rng.uniform(-3 * half, 3 * half, n // 4), np.full(n // 4, DTM)]
    return np.vstack([np.c_[x, yy, z], g]), np.r_[np.full(n, 1), np.full(n // 4, 2)].astype(np.uint8)


def part(xs, y):
    xy = np.c_[np.asarray(xs, np.float64), np.full(len(xs), float(y))]
    la, lo, _ = FRAME.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
    return (np.c_[lo, la], 5.0), (xy, 5.0)


@unittest.skipUnless(NATIVE, "earthfast 107 (lidar_gap_stretch)")
class MeasureTest(unittest.TestCase):
    """A viaduct (x -100..0) 8 m up whose map deck ends at x = 0, the structure going on 40 m and then down at 6 %; and
    a ramp whose deck starts on the street (its ground piece at grade, as I-83's on-ramp from the street in Baltimore)."""

    def measure(self, surface_deck, surface_on):
        p1, c1 = cloud(surface_deck, -100, 0, 0.0)
        p2, c2 = cloud(surface_on, 0, 160, 0.0, n=6000, seed=2)
        pts = np.ascontiguousarray(np.vstack([p1, p2]))
        cls = np.ascontiguousarray(np.r_[c1, c2])
        rz = roadz.RoadZ(None, object(), FakeMDem(DTM))
        deck, deck_l = part(np.linspace(-100, 0, 26), 0.0)
        cand, cand_l = part(np.linspace(0, 150, 38), 0.0)
        return rz._measure(FRAME, [deck, cand], [deck_l, cand_l], 1, ["g1"], pts, cls)

    def test_elevated_stretch_is_deck(self):
        rows, gaps = self.measure(lambda x: np.full(len(x), DTM + 8.0),
                                  lambda x: DTM + 8.0 - 0.06 * np.clip(x - 40.0, 0.0, None))
        self.assertEqual(len(gaps), 1)
        fid, a, b = gaps[0]
        self.assertEqual(fid, "g1")
        e = FRAME.geodetic_to_enu(np.array([b[1]]), np.array([b[0]]), 0.0)[0][0]
        # (3 m over the DTM until x = 40 + 5 / 0.06 = 123)
        self.assertGreater(e, 112.0)
        self.assertLess(e, 126.0)
        self.assertEqual(len(rows), 2, "the stretch is measured as a deck too")

    def test_at_grade_ramp_stays_ground(self):
        rows, gaps = self.measure(lambda x: np.full(len(x), DTM + 0.2), lambda x: np.full(len(x), DTM + 0.1))
        self.assertEqual(gaps, [])

    def test_kernel(self):
        s = np.arange(0.0, 40.0, 4.0)
        z = np.r_[np.full(5, DTM + 7.0), np.full(5, DTM + 0.2)]
        self.assertEqual(ef.lidar_gap_stretch(s, z, np.full(10, DTM), DTM + 7.2, 3.0, 1.5, 1), 16.0)
        self.assertEqual(ef.lidar_gap_stretch(s, np.full(10, np.nan), np.full(10, DTM), np.nan, 3.0, 1.5, 1), 0.0)


class StubProvider:
    enabled, lidar = True, True

    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def gap_answers(self, keys):
        self.calls += 1
        return {k: self.answer for k in keys}


@unittest.skipUnless(NATIVE, "earthfast 107")
class RangesTest(unittest.TestCase):
    """A viaduct p0 (y -300..0) going on as a ground road p1 (y 0..200)."""

    def setUp(self):
        self.old = roadz.PROVIDER, roads.LIDAR_GAPS_ON

    def tearDown(self):
        roadz.PROVIDER, roads.LIDAR_GAPS_ON = self.old

    def feats(self):
        f = features([[(0, -300), (0, 0)], [(0, 0), (0, 200)]], ["motorway", "motorway"],
                     flags=[(0, "is_bridge", 0.0, 1.0)], ids=["p0", "p1"])
        f.__dict__["_bnge_cell"] = ("test", "roads", "{}")
        return f

    def test_candidate_from_the_deck_end(self):
        c = roads.lidar_gap_candidates(self.feats())
        self.assertEqual([x[0] for x in c], ["p1"])
        first = FRAME.geodetic_to_enu(c[0][1][:1, 1], c[0][1][:1, 0], 0.0)
        self.assertLess(abs(first[1][0]), 0.1, "measured from the deck's end")
        e = FRAME.geodetic_to_enu(c[0][1][-1:, 1], c[0][1][-1:, 0], 0.0)
        self.assertAlmostEqual(e[1][0], roads.LIDAR_GAP_REACH_M, delta=0.5)

    def test_answer_makes_deck(self):
        roadz.PROVIDER = StubProvider([("p1", ll(0, 0), ll(0, 80))])
        self.assertEqual(roads.level_ranges(self.feats(), 1)[0][2], "bridge")
        a, b, _ = roads.level_ranges(self.feats(), 1)[0]
        self.assertAlmostEqual(a, 0.0)
        self.assertAlmostEqual(b, 0.4, delta=0.01)

    def test_no_lidar_keeps_the_rule(self):
        roadz.PROVIDER = StubProvider(None)
        f = self.feats()
        f0 = revisit.faults()
        self.assertEqual(roads.level_ranges(f, 1), [])
        self.assertGreater(revisit.faults(), f0, "a build on the rule for want of the lidar is not kept")
        roadz.PROVIDER = StubProvider([])
        self.assertEqual(roads.level_ranges(self.feats(), 1), [])

    def test_off(self):
        roadz.PROVIDER = StubProvider([("p1", ll(0, 0), ll(0, 80))])
        roads.LIDAR_GAPS_ON = False
        self.assertEqual(roads.level_ranges(self.feats(), 1), [])


DECIDE = """
import json, os, sys
sys.path.insert(0, sys.argv[1])
import mapcells, roadz
rz = roadz.RoadZ(sys.argv[2], object(), None)
got = rz._decide(mapcells.CellKey(14, 1, 2), None if sys.argv[3] == "none" else [("p1", (1.0, 2.0), (3.0, 4.0))])
print(json.dumps(got))
"""


class SessionTest(unittest.TestCase):
    def test_first_decision_holds_across_processes(self):
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, BNGE_ROADZ_SESSION="t1")
            run = lambda arg: json.loads(subprocess.run(        # noqa: E731
                [sys.executable, "-c", DECIDE, here, tmp, arg], env=env, capture_output=True, text=True,
                check=True).stdout)
            self.assertIsNone(run("none"), "the lidar not there in time: the rule")
            self.assertIsNone(run("gaps"), "another process of the run: the same, though its lidar came")
            env["BNGE_ROADZ_SESSION"] = "t2"
            self.assertEqual(run("gaps"), [["p1", [1.0, 2.0], [3.0, 4.0]]], "the next run: the lidar's")


if __name__ == "__main__":
    unittest.main()
