"""Road revamp v2 phase P2b (roadshape.deck_sheets, earthfast.deck_sheets; BNE_ROADSHAPE=sheet): an exit leaving a
carriageway on a bridge is ONE deck with it up to where their edges part (SPLIT_GAP_M), found from the deck geometry:
the through deck's slab reaches over the ramp to its far edge (one parapet on the union's outer edge), the ramp's own
slab starts on the split line (skewed), its road there drawn on the union at the through road's heights (pinned, then
blended back), no slab tops overlapping, no parapet in a lane, no step at the shared edge. The same records whatever
order the parts come in; a two-way road, a crossing and another level are no fork."""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import fast  # noqa: E402
import roadmesh  # noqa: E402
import roads  # noqa: E402
import roadshape  # noqa: E402
import structprobe  # noqa: E402

HAVE = fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= roadshape.SHEET_VERSION
MAIN = roads.Spec(11.0, "paved", False, "motorway", True, dir=1)
RAMP = roads.Spec(5.0, "paved", False, "motorway", True, dir=1, link=True)
TWO = roads.Spec(11.0, "paved", False, "primary", False)


def line(a, b, n, z0, z1):
    t = np.linspace(0.0, 1.0, n)[:, None]
    xy = np.asarray(a, np.float64) + (np.asarray(b, np.float64) - np.asarray(a, np.float64)) * t
    return np.c_[xy, z0 + (z1 - z0) * t[:, 0]]


def scene(merge=False, ramp_rise=1.5, pieces=False):
    """A motorway deck along +x: the trunk from x = -100 to the node (0, 0), the through on to x = 300 (12 m up),
    a one-lane ramp leaving to the left at 1:8 to (240, 30), ramp_rise m above the through's line at its far end
    (the two decks' heights disagree). merge: every line reversed (the ramp joins)."""
    parts = [("a-trunk", MAIN, line((-100, 0), (0, 0), 11, 10.0, 10.0)),
             ("b-through", MAIN, line((0, 0), (300, 0), 31, 10.0, 12.0)),
             ("c-ramp", RAMP, line((0, 0), (240, 30), 25, 10.0, 11.6 + ramp_rise))]
    if pieces:              # (each deck in two parts end to end, the map's segments split at connectors)
        th, ra = parts[1][2], parts[2][2]
        parts = [parts[0], ("b-through", MAIN, th[:6]), ("b-through2", MAIN, th[5:]),
                 ("c-ramp", RAMP, ra[:5]), ("c-ramp2", RAMP, ra[4:])]
    if merge:
        parts = [(f, s._replace(dir=1), p[::-1].copy()) for f, s, p in parts]
    return [(f, s, p, -76.6) for f, s, p in parts]


def drawn(parts, sheet, chain=False):
    old, prm = roadshape.SHEET, roadshape.SHEET_PARAMS.copy()
    roadshape.SHEET = sheet
    roadshape.SHEET_PARAMS[-1] = 1.0 if chain else 0.0     # (SHEET_CHAIN: the arms through their pieces)
    try:
        allc = roads._deck_chunks(parts, (-60.0, -60.0, 360.0, 90.0))
        chunks = [c for c in allc if not c.get("ctx")]
        ctx = [c for c in allc if c.get("ctx")]
        mesh = roadmesh.deck(chunks, (0.0, 0.0, 0.0), context=ctx)
        runs = roadmesh.parapet_runs(chunks, ctx)
    finally:
        roadshape.SHEET, roadshape.SHEET_PARAMS = old, prm
    pp, tt, _uu, mats, M = mesh
    tops = np.asarray(pp, np.float64)[tt[np.asarray(M) == mats.index(roadmesh.DECK_TOP)]]
    return chunks, tops, runs


@unittest.skipUnless(HAVE, "earthfast.deck_sheets missing")
class SheetRecords(unittest.TestCase):
    def recs(self, parts):
        return roadshape.deck_sheets([(f, s, p, float(s.width) / 2) for f, s, p, _lon in parts])

    def test_diverge(self):
        recs, dz = self.recs(scene())
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual((r["t"], r["b"], r["side"]), (1, 2, 1))
        self.assertEqual([(c[0], c[2]) for c in r["cover"]], [(1, 0), (2, 1)])
        # the facing edges part by 1.2 m where the offset is 5.5 + 2.5 + 1.2 = 9.2 m: x = 73.6 m along the through
        self.assertAlmostEqual(r["s_t"], 73.6, delta=0.6)
        self.assertTrue(r["parted"])
        # the ramp pinned to the through deck's heights in the union, its far end its own
        self.assertAlmostEqual(dz[2][0], 0.0, places=9)
        self.assertNotEqual(dz[2][3], 0.0)
        self.assertEqual(dz[2][-1], 0.0)
        self.assertFalse(np.any(dz[0]) or np.any(dz[1]))

    def test_merge_and_order(self):
        recs, _dz = self.recs(scene(merge=True))
        self.assertEqual(len(recs), 1)
        self.assertEqual((recs[0]["t"], recs[0]["b"]), (1, 2))
        self.assertTrue(all(c[1] for c in recs[0]["cover"]))         # (both walked against their point order)
        a, _ = self.recs(scene())
        b, _ = self.recs(scene()[::-1])
        self.assertEqual(a[0]["s_b"], b[0]["s_b"])
        self.assertEqual((b[0]["t"], b[0]["b"]), (1, 0))

    def test_ends_a_hair_apart(self):
        """The ramp's start 0.2 um from the through deck's (two nodes a rounding apart): one record, its pin once."""
        p = scene()
        ramp = p[2][2].copy()
        ramp[0, 0] += 2e-7
        src = [p[0], p[1], (p[2][0], p[2][1], ramp, p[2][3])]
        recs, dzs = roads._sheet_records(src)
        self.assertEqual(len(recs), 1)
        z_t = 10.0 + 2.0 * ramp[3, 0] / 300.0
        self.assertAlmostEqual(float(ramp[3, 2] + dzs[2][3]), z_t, places=6)

    def test_no_forks(self):
        p = scene()
        self.assertEqual(self.recs([p[0], (p[1][0], TWO, p[1][2], 0), p[2]])[0], [])     # (two-way through)
        hi = (p[2][0], p[2][1], p[2][2] + np.array([0.0, 0.0, 6.0]), 0)
        self.assertEqual(self.recs([p[0], p[1], hi])[0], [])                             # (another level)
        cross = ("c-ramp", RAMP, line((0, 0), (0, 200), 21, 10.0, 10.0), 0)
        self.assertEqual(self.recs([p[0], p[1], cross])[0], [])


@unittest.skipUnless(HAVE, "earthfast.deck_sheets missing")
class SheetDecks(unittest.TestCase):
    def test_one_deck(self):
        for merge, pieces in ((False, False), (True, False), (False, True), (True, True)):
            parts = scene(merge=merge, pieces=pieces)
            ch, tops, runs = drawn(parts, True, chain=pieces)
            # the ramp's road inside the union draws no slab of its own; its slab starts skewed on the split line
            ramp = [c for c in ch if str(c.get("fid")).startswith("c-ramp") and c.get("deck")]
            if pieces:
                # (a node's record reads the parts within SHEET_NODE_R_M of it alone: a ramp in pieces whose first
                # piece never parts from the deck is no fork; chains stay off until they can be window-independent)
                self.assertFalse(any(c.get("noslab") or c.get("sx") for c in ch))
                continue
            self.assertTrue(any(c.get("noslab") for c in ramp))
            self.assertTrue(any(c.get("sk0") or c.get("sk1") for c in ramp if not c.get("noslab")))
            self.assertTrue(any(c.get("sx") for c in ch if str(c.get("fid")).startswith("b-through")))
            # (the fork's node on the branch's union chunks: roads.deck_pads leaves its pad out)
            self.assertTrue(all(c.get("sxn")[:2] == [0.0, 0.0] for c in ramp if c.get("noslab")))
            # pinned: the ramp's road in the union at the through deck's heights (10 + 2 x / 300)
            for c in ramp:
                if c.get("noslab"):
                    n = np.asarray(c["n"], np.float64)
                    zt = 10.0 + 2.0 * n[:, 0] / 300.0
                    self.assertLess(float(np.abs(n[:, 2] - zt).max()), 0.02)
            # one slab over the union (the node to the split line at x 73.6): no tops overlapping, no step in them,
            # no hole; no parapet in a lane anywhere (past the split: the median fill to the gore nose, as before)
            tk, tz, _ = structprobe.raster_tris(tops)
            ov = structprobe.key_xy(structprobe.overlap_keys(tk, tz))
            self.assertEqual(int((ov[:, 0] < 73.0).sum()), 0, (merge, ov[:5]))
            st = structprobe.top_steps(tops, 1e4)
            self.assertEqual(int((structprobe.key_xy(st["keys"])[:, 0] < 73.0).sum()), 0, (merge, st["worst"]))
            u = structprobe.key_xy(np.unique(tk))
            for x, y in ((20.0, 6.0), (40.0, 7.5), (60.0, 10.0), (72.0, 11.5), (72.0, 7.0), (73.4, 11.9), (73.8, 11.9)):
                self.assertTrue(bool(((np.abs(u[:, 0] - x) < 0.2) & (np.abs(u[:, 1] - y) < 0.2)).any()), (x, y))
            cw = [(np.asarray(p[2], np.float64)[:, :2], np.asarray(p[2], np.float64)[:, 2], float(p[1].width) / 2,
                   ("deck", p[0])) for p in parts]
            got = structprobe.parapet_in_carriageway([(r["edge"], r["z"]) for r in runs], cw, 1e4)
            self.assertLess(got["m"], 0.5, (merge, got))

    def test_two_windows(self):
        """One fork seen by two tiles with different part lists (the forks' own window round each, the parts each
        draws, a duplicate copy, an unrelated far deck): the same record, the same pinned ramp, the same union."""
        p = scene()
        far = ("z-far", MAIN, line((0, 500), (300, 500), 31, 30.0, 30.0), -76.6)
        dup = (p[1][0], p[1][1], p[1][2].copy(), p[1][3])
        old = roadshape.SHEET
        roadshape.SHEET = True
        try:
            got = []
            for drawn_, wide, box in (([p[1], p[2]], p + [far, dup], (0.0, -60.0, 120.0, 90.0)),
                                      ([p[0], p[1], p[2]], [p[2], p[0], p[1]], (60.0, -60.0, 360.0, 90.0))):
                recs, decks = roads._sheet_plan(drawn_, None, None, wide)
                ramp = next(d for d in decks if d[0] == "c-ramp")[2]
                thr = next(d for d in decks if d[0] == "b-through")[2]
                allc = roads._deck_chunks(drawn_, box, sheet_wide=wide)
                sx = sorted(str(c.get("sx")) for c in allc if c.get("fid") == "b-through" and c.get("sx"))
                got.append((len(recs), ramp.tolist(), thr.tolist(), sx[:1]))
        finally:
            roadshape.SHEET = old
        self.assertEqual(got[0][0], 1)
        self.assertEqual(got[0], got[1])

    def test_before(self):
        """The same scene without the switch: the two slabs overlap at the fork (what P2b removes)."""
        _ch, tops, _runs = drawn(scene(), False)
        tk, tz, _ = structprobe.raster_tris(tops)
        self.assertGreater(len(structprobe.overlap_keys(tk, tz)) * structprobe.RASTER_M ** 2, 20.0)


if __name__ == "__main__":
    unittest.main()
