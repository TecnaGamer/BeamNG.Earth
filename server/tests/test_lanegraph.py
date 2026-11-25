"""Road surface v3 phase S1 (lanegraph.cell_lanes, earthfast.lane_graph; BNE_ROADSURF_V3=shadow, shadow only): the lane
graph from the map alone. The gore comes from the ramp's geometry (where its line is half the main's lanes plus half
its own lane off), not from the mapped split node; the taper and parallel lane from the TaperRule table or the lane
evidence; the boundaries are ordered on every row and the widths have no steps; the records are the same bytes in any
input order and with roads beyond the reach added (window independence)."""
import json
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import fast  # noqa: E402
import lanegraph  # noqa: E402

ANG = math.radians(3.0)
W_US = 3.65


def seg(fid, pts, cls=1, link=False, d=1, lanes=3, kmh=100.0, turn="", lfw=-1, lbw=-1, turn_bw="", place="",
        region=1):
    return dict(fid=fid, pts=np.asarray(pts, np.float64), cls=cls, link=link, dir=d, lanes=lanes, lfw=lfw, lbw=lbw,
                kmh=kmh, lane_w=3.3, turn=turn, turn_bw=turn_bw, place=place, region=region)


def ramp_pts(reverse=False, n=31):
    t = np.linspace(0.0, 300.0, n)
    p = np.c_[t, -t * math.tan(ANG)]
    return p[::-1] if reverse else p


def scene(trunk_lanes=3, merge=False, region=1):
    """A motorway along +x through the node (0, 0): pieces from x = -600 and -200 (the second with trunk_lanes), the
    through on to x = 500, a one-lane ramp leaving to the right at 3 degrees. merge: every direction reversed."""
    d = -1 if merge else 1
    segs = [seg("a0", [(-600.0, 0.0), (-200.0, 0.0)], d=d, region=region),
            seg("a1", [(-200.0, 0.0), (0.0, 0.0)], lanes=trunk_lanes, d=d, region=region),
            seg("b", [(0.0, 0.0), (250.0, 0.0), (500.0, 0.0)], d=d, region=region),
            seg("c", ramp_pts(), link=True, d=d, lanes=1, kmh=60.0, region=region)]
    cons = [("n_a0", "a0", 0), ("n_mid", "a0", 1), ("n_mid", "a1", 0), ("n0", "a1", 1), ("n0", "b", 0),
            ("n_b", "b", 2), ("n0", "c", 0), ("n_c", "c", len(segs[-1]["pts"]) - 1)]
    return segs, cons


def run(segs, cons, home=("n0",), order=None, seg_home=(), divided_crossings=False):
    segs = sorted(segs, key=lambda s: s["fid"])
    idx = {s["fid"]: k for k, s in enumerate(segs)}
    off = [0]
    for s in segs:
        off.append(off[-1] + len(s["pts"]))
    cons = list(cons) if order is None else [cons[i] for i in order]
    col = lambda k: [s[k] for s in segs]   # noqa: E731
    fast.require(lanegraph.LANES_VERSION, "lane_graph")
    got = fast._ef.lane_graph(np.ascontiguousarray(np.concatenate([s["pts"] for s in segs])), off, col("fid"),
                              col("cls"), col("link"), col("dir"), col("lanes"), col("lfw"), col("lbw"), col("kmh"),
                              col("lane_w"), col("turn"), col("turn_bw"), col("place"), col("region"),
                              [s["fid"] in seg_home for s in segs], [c[0] for c in cons], [idx[c[1]] for c in cons],
                              [c[2] for c in cons], [c[0] in home for c in cons], lanegraph.LANE_PARAMS,
                              np.ascontiguousarray(lanegraph.TAPER_TABLE.reshape(-1)), divided_crossings=divided_crossings)
    return got, json.loads(got)


def st(ev, k):
    return ev["s"][k] / 1000.0


def check_rows(test, ev):
    r = np.asarray(ev["rows"], np.float64)
    t = r[:, 5:]
    test.assertTrue(np.all(t[:, :-1] >= t[:, 1:]), "boundaries out of order")
    w = t[:, :-1] - t[:, 1:]
    # (a step would be a whole lane; the steepest quintic here, a 4 m gore over 50 m, moves 0.3 m per 2 m row)
    test.assertLess(float(np.abs(np.diff(w, axis=0)).max()), 0.5 * 1000.0, "a lane width steps between rows")


def gore_s(lanes_main=3, lanes_branch=1, w=W_US):
    return (lanes_main * w / 2.0 + lanes_branch * w / 2.0) / math.tan(ANG)


class LaneGraphTest(unittest.TestCase):
    def test_slip_lane_at_divided_crossing(self):
        segs, cons = scene()
        for road in segs:
            road["cls"] = 2  # divided trunk, all arms one-way
        segs[2]["pts"] = np.array([[0., 0.], [35., 0.], [250., 0.]])
        segs[3]["pts"][:, 1] = -.45 * segs[3]["pts"][:, 0]
        segs.append(seg("cross", [(35., -40.), (35., 0.), (35., 40.)], cls=3))
        cons.extend([("crossing", "b", 1), ("crossing", "cross", 1)])
        def event():
            return run(segs, cons, divided_crossings=True)[1]["events"][0]
        self.assertEqual(event()["flags"], ["at_grade"])
        segs[-1]["pts"] = np.array([[0., -2.], [35., 0.], [70., 2.]])
        self.assertIn("rows", event())  # four nearly parallel arms
        segs[-1]["pts"] = np.array([[35., -40.], [35., 0.], [35., 40.]])
        segs[1]["cls"] = segs[2]["cls"] = 1
        self.assertIn("rows", event())  # a true motorway exit
        # A crossing at the end of the designed gore is still an at-grade conflict
        # when the connector node is farther away than the short-arm test.
        segs[1]["cls"] = segs[2]["cls"] = 2
        segs[2]["pts"][1, 0] = 90
        segs[-1]["pts"][:, 0] = 90
        self.assertEqual(event()["flags"], ["at_grade"])

    def test_gore_from_geometry_design(self):
        _, out = run(*scene())
        (ev,) = out["events"]
        self.assertEqual((ev["kind"], ev["side"], ev["evidence"], ev["type"]), ("diverge", "right", "design", "parallel"))
        # the ramp's line 3 x 3.65 / 2 + 3.65 / 2 = 7.3 m off: 139 m after the node, not at it
        self.assertAlmostEqual(st(ev, "gore"), gore_s(), delta=1.5)
        tab = dict(zip(lanegraph.TAPER_FIELDS, lanegraph.TAPER_TABLE[1]))
        self.assertAlmostEqual(st(ev, "nose") - st(ev, "gore"), tab["gore_1"], delta=0.01)
        self.assertAlmostEqual(st(ev, "par1"), st(ev, "gore"), delta=0.01)
        self.assertAlmostEqual(st(ev, "par1") - st(ev, "par0"), tab["dec_par"], delta=0.01)
        self.assertAlmostEqual(st(ev, "taper1") - st(ev, "taper0"), max(tab["exit_k"] * W_US, tab["exit_min"]), delta=0.01)
        self.assertEqual(ev["strips"], ["through"] * 3 + ["gore", "branch"])
        check_rows(self, ev)

    def test_lane_drop_from_the_lanes(self):
        _, out = run(*scene(trunk_lanes=4))
        (ev,) = out["events"]
        self.assertEqual((ev["evidence"], ev["type"]), ("lanes", "parallel"))
        # the lane begins where the trunk has 4 lanes (x = -200), its taper a fifth before that joint
        tab = dict(zip(lanegraph.TAPER_FIELDS, lanegraph.TAPER_TABLE[1]))
        taper = max(tab["exit_k"] * W_US, tab["exit_min"])
        self.assertAlmostEqual(st(ev, "taper0"), -200.0 - tab["add_place"] * taper, delta=0.01)
        self.assertAlmostEqual(st(ev, "par1"), st(ev, "gore"), delta=0.01)
        check_rows(self, ev)

    def test_merge(self):
        _, out = run(*scene(merge=True))
        (ev,) = out["events"]
        # (every direction reversed: westbound, the ramp to the south joins on the driver's left)
        self.assertEqual((ev["kind"], ev["side"]), ("merge", "left"))
        self.assertAlmostEqual(st(ev, "gore"), -gore_s(), delta=1.5)
        self.assertLess(st(ev, "nose"), st(ev, "gore"))
        self.assertAlmostEqual(st(ev, "par0"), st(ev, "gore"), delta=0.01)
        self.assertGreater(st(ev, "taper1"), st(ev, "taper0"))
        check_rows(self, ev)

    def test_region_rules(self):
        _, us = run(*scene(region=1))
        _, de = run(*scene(region=2))
        a, b = us["events"][0], de["events"][0]
        self.assertAlmostEqual(st(b, "nose") - st(b, "gore"), 50.0, delta=0.01)
        self.assertAlmostEqual(st(b, "par1") - st(b, "par0"), 250.0, delta=0.01)
        # Germany's 3.75 m lanes put the gore further out
        self.assertGreater(st(b, "gore"), st(a, "gore"))

    def test_lane_add_without_ramp(self):
        segs = [seg("a", [(-600.0, 0.0), (0.0, 0.0)], lanes=3), seg("b", [(0.0, 0.0), (600.0, 0.0)], lanes=4)]
        cons = [("na", "a", 0), ("n0", "a", 1), ("n0", "b", 0), ("nb", "b", 1)]
        _, out = run(segs, cons)
        (ev,) = out["events"]
        self.assertEqual((ev["kind"], ev["side"], ev["evidence"], ev["type"]), ("add", "right", "outer", "drop"))
        # MUTCD L = W S at 100 km/h (62.1 mph): 227 m, a fifth of it before the joint
        L = W_US * 100.0 / 1.609344
        self.assertAlmostEqual(st(ev, "taper1") - st(ev, "taper0"), L, delta=0.01)
        self.assertAlmostEqual(st(ev, "taper0"), -0.2 * L, delta=0.01)
        check_rows(self, ev)
        # both ways centred on one unbroken line: the map says the through lanes move half a lane left, blended over
        # SHIFT_M about the joint (the anchor follows the data)
        r = np.asarray(ev["rows"], np.float64)
        self.assertAlmostEqual((r[-1, 5] - r[0, 5]) / 1000.0, W_US / 2.0, delta=0.002)
        shift = np.flatnonzero(np.diff(r[:, 5]) != 0)
        self.assertLessEqual(r[shift[-1] + 1, 0] - r[shift[0], 0], lanegraph.SHIFT_M * 1000.0 + 2000.0)
        # placement=left_of:1 on both: the left edge (the anchor) stays put on every row, the lane added on the right
        segs[0]["place"] = segs[1]["place"] = "left_of:1"
        _, out = run(segs, cons)
        (ev,) = out["events"]
        self.assertEqual((ev["side"], ev["evidence"]), ("right", "placement"))
        r = np.asarray(ev["rows"])
        self.assertEqual(len(set(r[:, 5].tolist())), 1)

    def test_turn_bay_two_way(self):
        segs = [seg("a", [(-300.0, 0.0), (0.0, 0.0)], cls=3, d=0, lanes=2, lfw=1, lbw=1, kmh=50.0),
                seg("b", [(0.0, 0.0), (40.0, 0.0)], cls=3, d=0, lanes=3, lfw=2, lbw=1, kmh=50.0, turn="left|")]
        cons = [("na", "a", 0), ("n0", "a", 1), ("n0", "b", 0), ("nb", "b", 1)]
        _, out = run(segs, cons)
        (ev,) = out["events"]
        self.assertEqual((ev["kind"], ev["side"], ev["evidence"]), ("add", "left", "bay"))
        self.assertEqual(ev["strips"], ["opposite", "added", "through"])
        check_rows(self, ev)
        # centred on the mapped line: the edges move out by half the bay each
        r = np.asarray(ev["rows"], np.float64)
        np.testing.assert_allclose(r[:, 5], -r[:, -1], atol=1)

    def test_order_free_and_window_independent(self):
        segs, cons = scene(trunk_lanes=4)
        base, _ = run(segs, cons)
        rng = np.random.default_rng(538)
        for _ in range(3):
            got, _ = run(list(reversed(segs)), cons, order=rng.permutation(len(cons)))
            self.assertEqual(got, base)
        # roads beyond the reach (another carriageway 3 km away) change nothing of the home node's record
        far = [seg("z0", [(3000.0, 0.0), (3600.0, 0.0)]), seg("z1", [(3600.0, 0.0), (4200.0, 0.0)], lanes=4)]
        far_cons = [("nz0", "z0", 0), ("nz1", "z0", 1), ("nz1", "z1", 0), ("nz2", "z1", 1)]
        got, _ = run(segs + far, cons + far_cons)
        self.assertEqual(got, base)

    def test_stroke_links(self):
        segs, cons = scene(trunk_lanes=4)
        _, out = run(segs, cons, home=("n0", "n_mid"), seg_home=("a1",))
        links = {tuple(x[:3]): x[3] for x in out["links"]}
        self.assertEqual(links[("a0", "a1", "continue")], 0)
        self.assertEqual(links[("a1", "b", "through")], 0)
        self.assertEqual(links[("a1", "c", "branch")], -3)      # (the trunk's lane 3 is the ramp's lane 0)
        self.assertEqual([r["fid"] for r in out["segs"]], ["a1"])
        self.assertEqual(out["segs"][0]["lanes"][0], 4)

    def test_left_add_link(self):
        # placement keeps the right edge (right_of:last): the lane is added on the left, so the left lanes' numbers move
        segs = [seg("a", [(-600.0, 0.0), (0.0, 0.0)], lanes=2, place="right_of:2"),
                seg("b", [(0.0, 0.0), (600.0, 0.0)], lanes=3, place="right_of:3")]
        cons = [("na", "a", 0), ("n0", "a", 1), ("n0", "b", 0), ("nb", "b", 1)]
        _, out = run(segs, cons)
        self.assertEqual(out["events"][0]["side"], "left")
        self.assertEqual(out["links"], [["a", "b", "continue", 1]])
        segs[0]["place"], segs[1]["place"] = "left_of:1", "left_of:1"
        _, out = run(segs, cons)
        self.assertEqual(out["links"], [["a", "b", "continue", 0]])

    def test_not_home(self):
        _, out = run(*scene(), home=())
        self.assertEqual(out["events"], [])


if __name__ == "__main__":
    unittest.main()
