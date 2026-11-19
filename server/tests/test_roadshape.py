"""Road revamp v2 phase P1b (roadshape.py, earthfast.deck_shape / deck_simple; BNE_ROADSHAPE=decks): a simple
structure's deck takes the road's smooth line (no corner where its approach arrives turned: KGBXY1DG's cause), its
ends follow the road it crosses (skew), run over the approach as a buried skirt, and bank with a banked road; a deck
of a structure with an END_ON edge (a ramp ending on it) is left as it was; off, nothing changes."""
import math
import types
import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import fast  # noqa: E402
import grading  # noqa: E402
import roadmesh  # noqa: E402
import roads  # noqa: E402
import roadshape  # noqa: E402
from test_roads import FRAME, features  # noqa: E402

TURN = math.radians(11.0)


def scene(ramp=False):
    """A 30 m bridge (its own feature, as Overture keeps most short bridges) whose west approach arrives turned 11
    degrees, its east approach straight on, a street crossing under it at 45 degrees; ramp: and a ramp ending on the
    deck's side (END_ON: not simple)."""
    lines = [[(-100.0 * math.cos(TURN), 100.0 * math.sin(TURN)), (0.0, 0.0)], [(0.0, 0.0), (30.0, 0.0)],
             [(30.0, 0.0), (130.0, 0.0)], [(15.0 - 40.0, -40.0), (15.0 + 40.0, 40.0)]]
    classes = ["primary", "primary", "primary", "residential"]
    levels = [(1, 1.0, 0.0, 1.0)]
    ids = ["west", "deck", "east", "street"]
    if ramp:
        lines.append([(12.0, 60.0), (12.0, 0.5)])
        classes.append("secondary")
        levels.append((4, 1.0, 0.3, 1.0))
        ids.append("ramp")
    return features(lines, classes, levels=levels, ids=ids)


def solve(f, on=True):
    flat = lambda la, lo: np.full(np.shape(la), 10.0)             # noqa: E731
    with mock.patch.object(roadshape, "DECKS", on), mock.patch.object(roadshape, "SKEW", on):
        bp = roads.BridgeProfiles(FRAME, lambda key: f, flat)
        got = bp.parts_shaped([f], (-50, -50, 80, 50))
    return {fid: (xy, z, rec) for fid, _spec, xy, z, _lon, rec in got}


def angle(u, v):
    return math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(u, v) / np.linalg.norm(u) / np.linalg.norm(v))))))


class ShapeTest(unittest.TestCase):
    def setUp(self):
        if not fast.AVAILABLE or getattr(fast._ef, "VERSION", 0) < roadshape.VERSION:
            self.skipTest("earthfast 505")

    def test_off_keeps_the_mapped_line(self):
        xy, _z, rec = solve(scene(), on=False)["deck"]
        self.assertIsNone(rec)
        self.assertLess(float(np.abs(xy[:, 1]).max()), 1e-3, "the mapped chord")

    def test_deck_leaves_along_its_approach(self):
        off = solve(scene(), on=False)
        got = solve(scene())
        xy, z, rec = got["deck"]
        self.assertIsNotNone(rec, "a plain bridge (its pieces end to end): simple")
        oxy = off["deck"][0]
        self.assertLess(float(np.hypot(*(xy[0] - oxy[0]))), 1e-6, "its ends stay")
        self.assertLess(float(np.hypot(*(xy[-1] - oxy[-1]))), 1e-6)
        # the whole structure, its pieces end to end: no corner anywhere along it (the mapped one turned 11 degrees)
        chain = sorted((v for v in got.values() if v[2] is not None), key=lambda v: float(v[0][0, 0]))
        line = np.concatenate([chain[0][0]] + [v[0][1:] for v in chain[1:]])
        d = np.diff(line, axis=0)
        d = d[np.hypot(*d.T) > 1e-6]
        turns = [angle(d[i], d[i + 1]) for i in range(len(d) - 1)]
        mapped = np.concatenate([off[f][0] for f in sorted(off, key=lambda f: float(off[f][0][0, 0]))])
        dm = np.diff(mapped, axis=0)
        dm = dm[np.hypot(*dm.T) > 1e-6]
        self.assertGreater(max(angle(dm[i], dm[i + 1]) for i in range(len(dm) - 1)), 10.0, "the mapped corner")
        self.assertLess(max(turns), 5.0, "rows by the chord rule along one smooth line")
        steps = np.hypot(*np.diff(xy, axis=0).T)
        self.assertLessEqual(float(steps.max()), roadshape.MAX_STEP_M + 1e-9)
        self.assertEqual(len(z), len(xy))
        self.assertTrue(np.isfinite(z).all())

    def test_ends_follow_the_crossed_street(self):
        got = solve(scene())
        street = np.array([1.0, 1.0]) / math.sqrt(2.0)
        skews = [rec["skew"][e] for _xy, _z, rec in got.values() if rec is not None for e in (0, 1)
                 if rec["skew"][e] is not None]
        self.assertEqual(len(skews), 2, "the structure's two free ends (its joints square)")
        for d in skews:
            self.assertLess(abs(float(d[0] * street[1] - d[1] * street[0])), 0.01, "along the street")
        ovs = [rec["ov"][e] for _xy, _z, rec in got.values() if rec is not None for e in (0, 1)
               if rec["ov"][e] is not None]
        self.assertEqual(len(ovs), 2, "a buried end over each approach")
        for ov in ovs:
            self.assertAlmostEqual(float(np.hypot(*np.diff(ov, axis=0).T).sum()), roadshape.OVERLAP_M, places=6)

    def test_shape_rides_on_its_published_line(self):
        # (one value in `done`: a later reader never pairs one solve's line with another's shape; the solve that
        # wins is chosen on the solve's own point count, not the shape's rows)
        f = scene()
        flat = lambda la, lo: np.full(np.shape(la), 10.0)         # noqa: E731
        with mock.patch.object(roadshape, "DECKS", True), mock.patch.object(roadshape, "SKEW", True):
            bp = roads.BridgeProfiles(FRAME, lambda key: f, flat)
            bp.parts_shaped([f], (-50, -50, 80, 50))
            for k, v in bp.done.items():
                rec = getattr(v[1], "shape_rec", None)
                if k[0] == "deck":
                    self.assertIsNotNone(rec)
                    self.assertIs(bp.shape_of(k), rec)
                    self.assertLess(bp._raw_len(k, v), len(v[1]), "its solve's points, not its rows")

    def test_end_on_structure_is_not_shaped(self):
        got = solve(scene(ramp=True))
        self.assertIsNone(got["deck"][2], "a ramp ending on it: END_ON, not simple")

    def test_simple_rule(self):
        self.assertEqual(roadshape.simple_members(
            [{"kind": 0}] * 4,
            {"root": [0, 0, 2, 2], "dup": [0, 1, 2, 3], "edges": [(0, 1, 4, True, 0.0), (2, 3, 2, True, 0.0)]}),
            [True, True, False, False])


class MeshTest(unittest.TestCase):
    def test_skewed_end_rows_never_fold(self):
        xy = np.c_[np.arange(0.0, 20.1, 2.0), np.zeros(11)]
        nrm = np.tile([0.0, 4.0], (11, 1))
        d = np.array([1.0, 1.0]) / math.sqrt(2.0)
        left, right = roadmesh.skew_ends(xy, nrm, d, d)
        for e in (0, -1):
            v = left[e] - right[e]
            self.assertLess(abs(float(v[0] * d[1] - v[1] * d[0])), 1e-9, "the end row along its line")
        for edge in (left, right):     # (each edge runs on along the deck: never back)
            self.assertTrue((np.diff(edge[:, 0]) >= -1e-9).all(), edge[:, 0])

    def test_reach_past_keeps_bank_and_drops_cut_ends(self):
        c = {"n": [[-2.0, 0.0, 5.0], [10.0, 0.0, 5.0], [20.0, 0.0, 5.0]], "w": 8.0, "bk": [0.01, 0.02, 0.03],
             "sk0": [0.6, 0.8], "ov0": [[-2.0, 0.0], [-12.0, 0.0]], "sk1": [0.6, 0.8]}
        out = roadmesh._reach_past(c, (0.0, -50.0, 50.0, 50.0))
        self.assertEqual(len(out["bk"]), len(out["n"]), "a slope per node (the skin keeps its tilt)")
        self.assertEqual(out["bk"][0], 0.01)
        self.assertNotIn("sk0", out, "a tile cut is no skewed end")
        self.assertNotIn("ov0", out)
        self.assertEqual(out["sk1"], [0.6, 0.8], "the deck's own end, inside the tile")

    def test_skewed_skirt_never_folds(self):
        d = np.array([0.5, math.sqrt(3.0) / 2])            # (60 degrees off square)
        xy = np.c_[np.arange(0.0, 10.1, 2.0), np.zeros(6)]
        nrm = np.tile([0.0, 4.45], (6, 1))
        el, er = roadmesh.skew_ends(xy, nrm, None, d)
        ov = np.c_[np.arange(10.0, 20.1, 2.0), np.zeros(6)]
        top = roadmesh.skirt(el, er, np.full(6, 20.0), 2.2, -1, ov)[0]
        Lt, Rt = top[0], top[1]
        for edge in (Lt, Rt):
            self.assertTrue((np.diff(edge[:, 0]) > 0).all(), edge[:, 0])

    def test_tilt_raises_the_left(self):
        left = np.array([[0.0, 2.0], [5.0, 2.0]])
        right = np.array([[0.0, -2.0], [5.0, -2.0]])
        z = np.array([1.0, 1.0])
        np.testing.assert_allclose(roadmesh.tilted(left, right, z, np.array([0.05, 0.05]), left), [1.1, 1.1])
        np.testing.assert_allclose(roadmesh.tilted(left, right, z, np.array([0.05, 0.05]), right), [0.9, 0.9])

    def test_skirt_is_buried(self):
        el = np.c_[np.arange(0.0, 10.1, 2.0), np.full(6, 4.45)]
        er = np.c_[np.arange(0.0, 10.1, 2.0), np.full(6, -4.45)]
        z = np.full(6, 20.0)
        ov = np.c_[np.arange(10.0, 20.1, 2.0), np.zeros(6)]
        faces = roadmesh.skirt(el, er, z, 2.2, -1, ov)
        self.assertEqual(len(faces), 5, "top, two sides, soffit, far end")
        top = faces[0][0]
        self.assertTrue((top[:, 2] <= 20.0 - roadshape.SKIRT_TOP_M + 1e-9).all(), "under the road")
        self.assertAlmostEqual(float(top[-1, 2]), 20.0 - roadshape.SKIRT_DIP_M, places=9)
        np.testing.assert_allclose(top[0, :2], el[-1], err_msg="its first row the slab's end row")
        soffit = faces[3][0]
        self.assertAlmostEqual(float(top[-1, 2] - soffit[-1, 2]), roadshape.SKIRT_LIP_M, places=9)


class GradingTest(unittest.TestCase):
    def test_bank_through_a_simple_deck(self):
        # a motorway's curve ending on a bridge: faded level there unless the deck banks with it
        t = np.linspace(0.0, math.pi / 3, 120)
        xy = np.c_[400.0 * np.sin(t), 400.0 * (1 - np.cos(t))]
        z = np.full(len(xy), 10.0)
        spec = types.SimpleNamespace(kind="paved", cls="motorway", width=10.0, median=None, link=False,
                                     oneway=True, mountain=False, street=None, urban=False)
        part, deck = xy[:80], xy[79:]           # (the deck: plan_roads' extension past the abutment end)
        faded = grading._plan(part, z[:80], spec, abut=(False, True))
        kept = grading._plan(part, z[:80], spec, abut=(False, True), bank_xy=np.r_[part, deck[1:]],
                             bank_ext=(0, len(deck) - 1), bank_abut=(False, False))
        self.assertEqual(float(faded.bank[-1]), 0.0)
        self.assertGreater(abs(float(kept.bank[-1])), 0.01)

    @staticmethod
    def long_structure(ramp, upper=False):
        """A motorway curving (r 400 m) onto a 30 m deck A, a 200 m deck B on from it, the road on east; ramp: a ramp
        ending on B's side 200 m from A's west end (END_ON: the structure is not simple). Grading's old read (the
        decks within 110 m of the end) saw A and B alone, simple, while the chain's group held the ramp. upper: a
        deck of level 2 ending 1.8 m from A's west end."""
        t = np.linspace(math.pi / 6, 0.0, 30)
        west = [(-400.0 * math.sin(a), 400.0 * (1 - math.cos(a))) for a in t]
        lines = [west, [(0.0, 0.0), (30.0, 0.0)], [(30.0, 0.0), (230.0, 0.0)], [(230.0, 0.0), (330.0, 0.0)]]
        classes = ["motorway"] * 4
        levels = [(1, 1.0, 0.0, 1.0), (2, 1.0, 0.0, 1.0)]
        ids = ["west", "A", "B", "east"]
        if ramp:
            lines.append([(200.0, 60.0), (200.0, 0.5)])
            classes.append("secondary")
            levels.append((4, 1.0, 0.3, 1.0))
            ids.append("ramp")
        if upper:
            lines.append([(1.5, 1.0), (1.5, 120.0)])
            classes.append("primary")
            levels.append((len(lines) - 1, 2.0, 0.0, 1.0))
            ids.append("upper")
        return features(lines, classes, levels=levels, ids=ids)

    def test_grading_and_deck_agree_at_the_abutment(self):
        # Codex high on P1b: grading decided "simple" from the decks within 110 m of the end, the deck from its whole
        # chain: a banked approach met a level deck at the abutment. Both now read roadshape.part_simple.
        if not fast.AVAILABLE or getattr(fast._ef, "VERSION", 0) < roadshape.VERSION:
            self.skipTest("earthfast 505")
        import threading
        for ramp in (True, False):
            f = self.long_structure(ramp)
            got = solve(f)
            g = object.__new__(grading._Planner)
            g.lock = threading.Lock()
            g.frame = FRAME
            g._roads_cell = lambda k, f=f: f
            with mock.patch.object(roadshape, "DECKS", True):
                keeps = g._deck_end_simple(np.array([0.3, 0.2]))     # (the west approach's end at A)
            rec = got["A"][2]
            self.assertEqual(keeps, rec is not None, f"ramp={ramp}: the road keeps its bank iff the deck banks")
            self.assertEqual(keeps, not ramp)
            if not ramp:
                self.assertIsNotNone(rec["bank"], "the deck banks with the curve")
                self.assertGreater(abs(float(rec["bank"][0])), 0.005)
                self.assertIsNotNone(got["B"][2], "one structure, one answer")
            else:
                self.assertIsNone(got["B"][2])

    def test_another_level_ending_at_the_abutment(self):
        # (Codex review of the fix: a road end near two deck ends, a stacked level's and its own deck's, took the
        # nearest one's answer; neither is simple now, so the plans and both decks agree whichever it takes)
        if not fast.AVAILABLE or getattr(fast._ef, "VERSION", 0) < roadshape.VERSION:
            self.skipTest("earthfast 505")
        import threading
        f = self.long_structure(False, upper=True)
        got = solve(f)
        g = object.__new__(grading._Planner)
        g.lock = threading.Lock()
        g.frame = FRAME
        g._roads_cell = lambda k: f
        with mock.patch.object(roadshape, "DECKS", True):
            keeps = g._deck_end_simple(np.array([0.3, 0.2]))
        self.assertFalse(keeps)
        self.assertIsNone(got["A"][2], "a deck of another level ends at its abutment: not simple")

    def test_end_simple_is_the_same_from_any_start(self):
        # (the reading starts at the caller's copy of the end; the answer is the map's)
        if not fast.AVAILABLE or getattr(fast._ef, "VERSION", 0) < roadshape.VERSION:
            self.skipTest("earthfast 505")
        f = self.long_structure(True)
        la, lo, _ = FRAME.enu_to_geodetic(np.array([0.0, 0.004, 250.0]), np.array([0.0, -0.003, 40.0]), 0.0)
        for e in (0, 1):
            outs = {roadshape.end_simple(lambda k: f, ("A", 0), e, (float(lo[i]), float(la[i]))) for i in range(3)}
            self.assertEqual(outs, {False})
        f = self.long_structure(False)
        self.assertTrue(roadshape.end_simple(lambda k: f, ("B", 0), 1, (float(lo[2]), float(la[2]))))

    def test_shape_maps_into_another_frame_twice(self):
        # (an adopted chain's shape, mapped again by a view: its line, skew, overlaps and bank go along)
        class Shift:
            def pts(self, p):
                q = np.array(p, np.float64, copy=True)
                q[:, 0] += 5.0
                return q
        rec = {"xy": np.array([[0.0, 0.0], [10.0, 0.0]]), "z": np.array([1.0, 2.0]), "tan": [0, None],
               "skew": [np.array([0.6, 0.8]), None], "ov": [np.array([[0.0, 0.0], [-10.0, 0.0]]), None],
               "bank": np.array([0.02, 0.03])}
        twice = roadshape.mapped(roadshape.mapped(rec, Shift()), Shift())
        np.testing.assert_allclose(twice["xy"][:, 0], [10.0, 20.0])
        np.testing.assert_allclose(twice["skew"][0], [0.6, 0.8])
        np.testing.assert_allclose(twice["ov"][0][:, 0], [10.0, 0.0])
        np.testing.assert_allclose(twice["bank"], [0.02, 0.03])


if __name__ == "__main__":
    unittest.main()
