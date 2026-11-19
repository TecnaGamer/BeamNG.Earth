"""Road revamp v2 phase P2a (roadshape.cell_forks, earthfast.fork_records; BNE_ROADSHAPE=sheet, shadow only): the fork
records of a diverge / merge from the map alone: which arm is the branch, its side, the painted and physical noses
from the mapped ramp, the shared length's start from the lanes (a lane drop), the turn lanes or the design length, the
widths without steps, and the same record whatever order the inputs come in. A one-way street's right-angle T is not
a fork."""
import json
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import fast  # noqa: E402
import roadshape  # noqa: E402

ANG = math.radians(3.0)


def seg(fid, pts, cls=1, link=False, d=1, pave=14.1, drive=11.1, lanes=3, kmh=100.0, turn=""):
    return dict(fid=fid, pts=np.asarray(pts, np.float64), cls=cls, link=link, dir=d, pave=pave, drive=drive,
                lanes=lanes, kmh=kmh, level=0, turn=turn)


def ramp_pts(reverse=False, n=31):
    t = np.linspace(0.0, 300.0, n)
    p = np.c_[t, -t * math.tan(ANG)]
    return p[::-1] if reverse else p


def scene(trunk_lanes=3, turn="", merge=False, tee=False):
    """A motorway along +x through the node (0, 0): the trunk from x = -200 (another piece before it from -600, with
    3 lanes), the through on to x = 500, a one-lane ramp leaving to the right at 3 degrees (tee: a one-way street at
    90 degrees instead). merge: every direction reversed (the ramp joins)."""
    d = -1 if merge else 1
    segs = [seg("a0", [(-600.0, 0.0), (-200.0, 0.0)], d=d), seg("a1", [(-200.0, 0.0), (0.0, 0.0)], lanes=trunk_lanes,
                                                               pave=17.8 if trunk_lanes == 4 else 14.1,
                                                               drive=14.8 if trunk_lanes == 4 else 11.1, d=d, turn=turn),
            seg("b", [(0.0, 0.0), (250.0, 0.0), (500.0, 0.0)], d=d)]
    if tee:
        segs.append(seg("c", [(0.0, 0.0), (0.0, -300.0)], cls=4, d=d, lanes=1, pave=6.7, drive=3.7))
    else:
        segs.append(seg("c", ramp_pts(), link=True, d=d, lanes=1, pave=6.7, drive=3.7, kmh=60.0))
    cons = [("n_a0", "a0", 0), ("n_mid", "a0", 1), ("n_mid", "a1", 0), ("n0", "a1", 1), ("n0", "b", 0),
            ("n_b", "b", 2), ("n0", "c", 0), ("n_c", "c", len(segs[-1]["pts"]) - 1)]
    return segs, cons


def run(segs, cons, home=("n0",), order=None):
    segs = sorted(segs, key=lambda s: s["fid"])
    idx = {s["fid"]: k for k, s in enumerate(segs)}
    off = [0]
    for s in segs:
        off.append(off[-1] + len(s["pts"]))
    cons = list(cons) if order is None else [cons[i] for i in order]
    col = lambda k: [s[k] for s in segs]   # noqa: E731
    fast.require(roadshape.FORK_VERSION, "fork_records")
    got = fast._ef.fork_records(np.ascontiguousarray(np.concatenate([s["pts"] for s in segs])), off, col("fid"),
                                col("cls"), col("link"), col("dir"), col("pave"), col("drive"), col("lanes"), col("kmh"),
                                col("level"), col("turn"), [c[0] for c in cons], [idx[c[1]] for c in cons],
                                [c[2] for c in cons], [c[0] in home for c in cons], roadshape.FORK_PARAMS)
    return got, json.loads(got)


def widths(rec):
    r = np.asarray(rec["rows"], np.float64) / 1000.0
    return r[:, 0], r[:, 9] - r[:, 5]


class ForkTest(unittest.TestCase):
    def test_diverge_design(self):
        _, recs = run(*scene())
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual((r["kind"], r["side"], r["trunk"], r["through"], r["branch"]), ("diverge", "right", "a1", "b", "c"))
        self.assertEqual(r["evidence"], "design")
        # painted nose: the ramp's centre half the lanes' widths off ((3 + 1) x 3.65 / 2 = 7.3 m); physical: NOSE_GAP_M
        # (7 m) further (P2a2: fitted on the aerial)
        tan = math.tan(ANG)
        w2 = (3 + 1) * roadshape.NOSE_LANE_M / 2.0
        self.assertAlmostEqual(r["s"]["painted"] / 1000.0, w2 / tan * math.cos(ANG), delta=1.5)
        self.assertAlmostEqual(r["s"]["physical"] / 1000.0, (w2 + roadshape.NOSE_GAP_M) / tan * math.cos(ANG), delta=1.5)
        # design: deceleration 1.5 V - 40 = 110 m and a 90 m taper before the painted nose
        self.assertAlmostEqual((r["s"]["painted"] - r["s"]["taper"]) / 1000.0, 200.0, delta=0.01)
        s, w = widths(r)
        self.assertLess(np.abs(np.diff(w)).max(), 0.6)          # no steps (2 m rows)
        self.assertAlmostEqual(w[0], 14.1, delta=0.01)          # the trunk's mapped pavement at the record's start
        k = int(np.argmin(np.abs(s - r["s"]["physical"] / 1000.0)))
        self.assertAlmostEqual(w[k], 14.1 + 1.2 + 6.7, delta=0.01)   # through + gap + branch at the physical nose
        self.assertTrue(r["branch_line"])

    def test_lane_drop(self):
        _, recs = run(*scene(trunk_lanes=4))
        r = recs[0]
        self.assertEqual(r["evidence"], "lanes")
        self.assertEqual(r["s"]["taper"], -200000)              # the connector where the trunk got its fourth lane
        s, w = widths(r)
        self.assertLess(np.abs(np.diff(w)).max(), 0.6)
        self.assertAlmostEqual(w[0], 14.1, delta=0.01)          # a0's 3-lane pavement before the seam

    def test_lane_from_the_start(self):
        # the trunk has the exit lane all along its 400 m reach (a0 four lanes too): the width blends from the trunk's
        # mapped pavement to the model's after the record's start, never a step there
        segs, cons = scene(trunk_lanes=4)
        segs[0].update(lanes=4, pave=19.0)
        _, recs = run(segs, cons)
        r = recs[0]
        self.assertEqual(r["evidence"], "lanes")
        self.assertIn("lane_beyond_reach", r["flags"])
        s, w = widths(r)
        self.assertAlmostEqual(w[0], 19.0, delta=0.01)
        self.assertLess(np.abs(np.diff(w)).max(), 0.6)

    def test_short_trunk_rows_are_the_pieces(self):
        # the trunk is 50 m from a dead end: the seam runs after the taper start, the lane's own taper after it, and the
        # rows' widths are the serialized pieces' (Codex review of P2a)
        segs, cons = scene()
        segs = [s for s in segs if s["fid"] != "a0"]
        segs[0]["pts"] = np.array([(-50.0, 0.0), (0.0, 0.0)])
        segs[0]["pave"] = 12.0
        cons = [c for c in cons if c[1] != "a0"]
        _, recs = run(segs, cons)
        r = recs[0]
        self.assertIn("short_trunk", r["flags"])

        def q(u):
            u = min(max(u, 0.0), 1.0)
            return u ** 3 * (10 - 15 * u + 6 * u * u)

        def pc(p, s):
            _n, s0, s1, v0, v1 = p
            return v0 if s < s0 else v1 if s >= s1 or s1 <= s0 else v0 + (v1 - v0) * q((s - s0) / (s1 - s0))
        ws, wb, gp = r["pieces"]
        dM, hM, dB, hB, _ = r["w"]
        for row in r["rows"]:
            s = row[0]
            w = pc(ws, s) if s < ws[2] else hM + dM + pc(wb, s) + pc(gp, s) + hB
            self.assertLess(abs(w - (row[9] - row[5])), 2)        # mm
            if s >= ws[2]:      # (in the seam a mapped trunk narrower than the through is the map's own)
                self.assertGreaterEqual(row[9], row[8])           # the near pavement edge never inside the lane

    def test_turn_lanes(self):
        _, recs = run(*scene(turn="none|none|slight_right"))
        r = recs[0]
        self.assertEqual(r["evidence"], "turn_lanes")
        self.assertEqual(r["s"]["taper"], -200000)              # the way with the exit lane starts there

    def test_merge(self):
        _, recs = run(*scene(merge=True))
        r = recs[0]
        # (traffic now runs west: the ramp south of the road joins from the driver's left)
        self.assertEqual((r["kind"], r["side"], r["branch"]), ("merge", "left", "c"))
        self.assertEqual(r["evidence"], "design")
        # acceleration 5 V - 250 = 250 m and the taper: clamped at the trunk's 400 m reach
        self.assertGreater(r["s"]["painted"] - r["s"]["taper"], 300000)

    def test_nose_held(self):
        # (P2a2) a mapped ramp that bulges out early (15 m off at 60 m), back in (8 m at 120 m), then away: the physical
        # nose where the offset stays past 14.3 m (about 155 m), never at the bulge; the painted nose stays at the start
        segs, cons = scene()
        segs[-1] = seg("c", [(0.0, 0.0), (60.0, -15.0), (120.0, -8.0), (300.0, -40.0)], link=True, lanes=1, pave=6.7,
                       drive=3.7, kmh=60.0)
        cons[-1] = ("n_c", "c", 3)
        r = run(segs, cons)[1][0]
        self.assertAlmostEqual(r["s"]["physical"] / 1000.0, 120.0 + 6.3 / 32.0 * 180.0, delta=1.5)
        self.assertLess(r["s"]["painted"] / 1000.0, 40.0)

    def test_nose_held_past_a_wide_bulge(self):
        # (Codex P2a2) a bulge past the scan's stop offset (25 m off at 40 m), back in to 6 m at 80 m, then away: both
        # noses after the dip (painted ~85 m, physical ~114 m)
        segs, cons = scene()
        segs[-1] = seg("c", [(0.0, 0.0), (40.0, -25.0), (80.0, -6.0), (300.0, -60.0)], link=True, lanes=1, pave=6.7,
                       drive=3.7, kmh=60.0)
        cons[-1] = ("n_c", "c", 3)
        r = run(segs, cons)[1][0]
        k = 54.0 / 220.0
        self.assertAlmostEqual(r["s"]["physical"] / 1000.0, 80.0 + 8.3 / k, delta=2.0)
        self.assertAlmostEqual(r["s"]["painted"] / 1000.0, 80.0 + 1.3 / k, delta=2.0)

    def _crossing_scene(self, cls):
        # the through arm ends 40 m on at a two-way road of the fork classes (a signal's crossing)
        segs, cons = scene()
        for s in segs:
            if not s["link"]:
                s["cls"] = cls
        segs[2] = seg("b", [(0.0, 0.0), (40.0, 0.0)], cls=cls)
        segs.append(seg("x", [(40.0, -100.0), (40.0, 0.0), (40.0, 100.0)], cls=4, d=0, lanes=2, pave=7.4, drive=7.4))
        cons = [c for c in cons if c[1] != "b"] + [("n0", "b", 0), ("n_x", "b", 1), ("n_x", "x", 1)]
        return run(segs, cons)[1]

    def test_at_grade_slip_lane(self):
        # (P2a2) a primary road's slip lane by a junction with crossing traffic: a junction zone's, no record
        recs = self._crossing_scene(3)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["flags"], ["at_grade"])
        self.assertNotIn("rows", recs[0])
        # a motorway's own exit is a freeway gore wherever its arms end
        recs = self._crossing_scene(1)
        self.assertNotIn("at_grade", recs[0]["flags"])
        # a service road's access (two-way, no fork class) is no crossing
        segs, cons = scene()
        for s in segs:
            if not s["link"]:
                s["cls"] = 3
        segs.append(seg("x", [(30.0, 0.0), (30.0, 60.0)], cls=0, d=0, lanes=1, pave=4.0, drive=4.0))
        segs[2] = seg("b", [(0.0, 0.0), (30.0, 0.0), (500.0, 0.0)], cls=3)
        cons = cons + [("n_x", "b", 1), ("n_x", "x", 0)]
        self.assertNotIn("at_grade", run(segs, cons)[1][0]["flags"])

    def test_four_arms(self):
        # (Codex P2a2) four one-way arms 40 m on: a same-direction lane merge and split is no crossing, a one-way cross
        # street is
        def four(across):
            segs, cons = scene()
            for s in segs:
                if not s["link"]:
                    s["cls"] = 3
            segs[2] = seg("b", [(0.0, 0.0), (40.0, 0.0)], cls=3)
            if across:
                e, f = [(40.0, -100.0), (40.0, 0.0)], [(40.0, 0.0), (40.0, 100.0)]
            else:
                e, f = [(-60.0, 8.0), (40.0, 0.0)], [(40.0, 0.0), (500.0, 8.0)]
            segs += [seg("d", [(40.0, 0.0), (500.0, 0.0)], cls=3), seg("e", e, cls=0, lanes=1),
                     seg("f", f, cls=0, lanes=1)]
            cons = [c for c in cons if c[1] != "b"] + [("n0", "b", 0), ("n_x", "b", 1), ("n_x", "d", 0),
                                                       ("n_x", "e", 1), ("n_x", "f", 0)]
            return run(segs, cons)[1][0]["flags"]
        self.assertNotIn("at_grade", four(False))
        self.assertEqual(four(True), ["at_grade"])

    def test_tee_is_not_a_fork(self):
        _, recs = run(*scene(tee=True))
        self.assertEqual(recs, [])

    def test_not_home(self):
        _, recs = run(*scene(), home=())
        self.assertEqual(recs, [])

    def test_order_free(self):
        segs, cons = scene(trunk_lanes=4)
        a, _ = run(segs, cons)
        rng = np.random.default_rng(7)
        for _ in range(5):
            b, _ = run(list(reversed(segs)), cons, order=list(rng.permutation(len(cons))))
            self.assertEqual(a, b)

    def test_quintic_pieces(self):
        _, recs = run(*scene())
        r = recs[0]
        names = [p[0] for p in r["pieces"]]
        self.assertEqual(names, ["w_seam", "wb", "gp"])
        gp = r["pieces"][2]
        self.assertEqual((gp[1], gp[2]), (r["s"]["painted"], r["s"]["physical"]))


if __name__ == "__main__":
    unittest.main()
