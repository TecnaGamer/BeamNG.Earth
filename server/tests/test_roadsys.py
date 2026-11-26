"""BNE_ROADSYS (the road surface + bridge rewrite; server/roadsys.py, native/roadsys): off unless set; no key switch at off
or shadow (M1: changes nothing the player sees), a key switch at heights (M2: today's decks and grading take its
profiles); the kernels' products functions of their declared inputs only: the same bytes whatever the input order, the
copies or the lane-section duplicates in the read; tiles sharing a side make the same seam; a piece's key moves with an
input inside its read list and only then; a profile square is the same whichever order its map comes in, round-trips
its bytes, and two squares agree inside their shared band."""
import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import fast  # noqa: E402
import revisit  # noqa: E402

STYLE = ("white", "", 0.12, 0.15, 0.15, 0.2, (3.0, 9.0), (3.0, 9.0), (3.0, 9.0), False)


def seg(fid, pts, cls=4, lanes=2, oneway=0, kmh=50.0, cons=(), bridge=None, cell="14/0/0", code="US"):
    """One segment row: (id, cell, texts, nums, ints, points, connectors [(id, vertex)], ranges [(a, b, kind, v)])."""
    rng = [(bridge[0], bridge[1], 0.0, 0.0), (bridge[0], bridge[1], 2.0, 1.0)] if bridge else []
    return (fid, cell, (code, "secondary", "", "", ""), (7.0, 7.0, kmh, 3.5),
            (lanes, 0, cls, 0, oneway, lanes, -1, -1, 0), np.asarray(pts, np.float64), list(cons), rng)


def table(rows):
    ids, cells, texts, nums, ints, pts, off = [], [], [], [], [], [], [0]
    cs, ci, cv, rs, rr = [], [], [], [], []
    for g, (fid, cell, t, n, i, p, cons, rng) in enumerate(rows):
        ids.append(fid)
        cells.append(cell)
        texts.append(t)
        nums.append(n)
        ints.append(i)
        pts.append(p)
        off.append(off[-1] + len(p))
        for c, v in cons:
            cs.append(g)
            ci.append(c)
            cv.append(v)
        for x in rng:
            rs.append(g)
            rr.append(x)
    return fast._ef.RoadsysSegments(ids, cells, texts, np.asarray(nums, np.float64), np.asarray(ints, np.int64),
                                    np.concatenate(pts), np.asarray(off, np.int64), np.asarray(cs, np.int64), ci,
                                    np.asarray(cv, np.int64), np.asarray(rs, np.int64),
                                    np.asarray(rr, np.float64).reshape(-1, 4), STYLE)


def network():
    """A crossroads of two streets, a third street ending on one of them and a motorway on a bridge over a street."""
    return [
        seg("a", [[-400, 0], [0, 0]], cons=[("n0", 1)]),
        seg("b", [[0, 0], [400, 0]], cons=[("n0", 0), ("n1", 1)]),
        seg("c", [[0, -400], [0, 0]], cons=[("n0", 1)]),
        seg("d", [[0, 0], [0, 400]], cons=[("n0", 0)]),
        seg("e", [[400, 0], [400, 300]], cons=[("n1", 0)]),
        seg("m", [[-300, -600], [-300, 600]], cls=1, lanes=2, oneway=1, kmh=100.0, bridge=(0.45, 0.55)),
    ]


def ground(e, n):
    return 10.0 + 0.01 * np.asarray(e) + 0.02 * np.asarray(n)


QUAD = [(-128.0, -128.0), (128.0, -128.0), (128.0, 128.0), (-128.0, 128.0)]


def build(rows, quad=QUAD, tid=(0, 0)):
    fast._ef.roadsys_clear()
    text, data = fast._ef.roadsys_build(table(rows), quad, tid, 1, ground)
    import json
    return json.loads(text), bytes(data)


def bridge_network(dx=0.0):
    """A street crossing and a motorway on a bridge over one of the streets (frame metres, offset by dx)."""
    return [
        seg("a|0", [[-600 + dx, 0], [dx, 0]], cons=[("n0", 1)]),
        seg("b|0", [[dx, 0], [600 + dx, 0]], cons=[("n0", 0)]),
        seg("c|0", [[dx, -600], [dx, 0]], cons=[("n0", 1)]),
        seg("m|0", [[-300 + dx, -900], [-300 + dx, 900]], cls=1, lanes=2, oneway=1, kmh=100.0, bridge=(0.48, 0.52)),
    ]


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= 602, "earthfast 602 (roadsys squares, M3) required")
class SquareTest(unittest.TestCase):
    def square(self, rows, sq=(-1, 0)):
        fast._ef.roadsys_clear()
        return fast._ef.roadsys_square(table(rows), sq, ground)

    def test_same_square_whatever_the_order(self):
        a = self.square(bridge_network())
        b = self.square(list(reversed(bridge_network())))
        self.assertEqual(a.info()[1], b.info()[1])
        self.assertEqual(bytes(a.to_bytes()), bytes(b.to_bytes()))

    def test_bytes_round_trip_and_are_checked(self):
        a = self.square(bridge_network())
        data = bytes(a.to_bytes())
        back = fast._ef.RoadsysSquare.from_bytes(data)
        self.assertEqual(bytes(back.to_bytes()), data)
        bad = bytearray(data)
        bad[len(bad) // 2] ^= 1
        with self.assertRaises(ValueError):
            fast._ef.RoadsysSquare.from_bytes(bytes(bad))

    def test_deck_heights_on_its_own_feature_only(self):
        a = self.square(bridge_network())
        pts = np.array([[-300.0, -20.0], [-300.0, 0.0], [-300.0, 20.0]])
        tans = np.array([[0.0, 1.0]] * 3)
        z = fast._ef.roadsys_blend([a], pts, tans, ["m"] * 3, True)
        self.assertTrue(np.isfinite(z).all())
        street = 10.0 + 0.01 * -300.0
        self.assertGreater(float(z[1]), street + 1.0, "the deck over the street")
        self.assertTrue(np.isnan(fast._ef.roadsys_blend([a], pts, tans, ["a"] * 3, True)).all(), "another feature: none")

    def test_neighbours_agree_in_their_band(self):
        rows = bridge_network(200.0)        # (the bridge 100 m west of the line between squares (-1, 0) and (0, 0))
        a, b = self.square(rows, (-1, 0)), self.square(rows, (0, 0))
        pts, tans = np.array([[-100.0, 0.0]]), np.array([[0.0, 1.0]])
        za = fast._ef.roadsys_blend([a], pts, tans, ["m"], True)[0]
        zb = fast._ef.roadsys_blend([b], pts, tans, ["m"], True)[0]
        self.assertLess(abs(za - zb), 0.01)
        self.assertLess(abs(fast._ef.roadsys_blend([a, b], pts, tans, ["m"], True)[0] - za), 0.01)

    def test_pins_are_ground_runs_of_the_component(self):
        pins = self.square(bridge_network()).pins()
        self.assertTrue(pins)
        for feat, pts, tans, w, share, half, hand, low in pins:
            self.assertEqual(len(pts), len(w))
            self.assertTrue(((w >= 0) & (w <= 1)).all())
            self.assertGreater(half, 1.0)
            self.assertEqual(len(hand), len(pts))
            self.assertTrue(((hand >= 0) & (hand <= 1) & (low >= 0) & (low <= 1)).all())

    def test_pins_own_the_ground_round_deck_ends_and_dig_under_decks(self):
        """(M3) hand: 1 at the motorway's deck ends, falling away from them; low: the street under the deck, round
        its crossing; the motorway's knots far from its deck hand nothing over."""
        pins = self.square(bridge_network()).pins()
        mw = [(pts, hand, w) for feat, pts, tans, w, share, half, hand, low in pins if feat == "m"]
        st = [(pts, low) for feat, pts, tans, w, share, half, hand, low in pins if feat in ("a", "b")]
        self.assertTrue(mw and st)
        ends = [-900 + 1800 * 0.48, -900 + 1800 * 0.52]
        for pts, hand, w in mw:
            d = np.min(np.abs(pts[:, 1][:, None] - np.array(ends)[None, :]), axis=1)
            self.assertTrue((hand[d < 1.0] > 0.99).all(), "full at the deck end")
            self.assertTrue((w[d < 1.0] > 0.99).all(), "never faded at the deck end")
            if (d > 250).any():
                self.assertTrue((hand[d > 250] < 0.01).all(), "the plans' own far away")
        near = [low[np.abs(pts[:, 0] + 300.0) < 2.0] for pts, low in st]
        self.assertTrue(any(len(v) and v.max() > 0.99 for v in near), "the street dug under the deck")

    def test_a_deck_depth_row_is_its_structures_depth(self):
        """(M3) a segment's depth row (kind 3): the drawn depth roadsys_deck_depth answers on its structure (the floors'
        and the deck builder's); none given: the full depth (2.2 m, roads.DECK_FULL_DEPTH)."""
        rows = bridge_network()
        a = self.square(rows)
        pts, tans = np.array([[-300.0, 0.0]]), np.array([[0.0, 1.0]])
        self.assertAlmostEqual(float(fast._ef.roadsys_deck_depth([a], pts, tans, ["m"])[0]), 2.2)
        fid, cell, t, n, i, p, cons, rng = rows[3]
        rows[3] = (fid, cell, t, n, i, p, cons, rng + [(0.0, 1.0, 3.0, 1.1)])
        b = self.square(rows)
        self.assertAlmostEqual(float(fast._ef.roadsys_deck_depth([b], pts, tans, ["m"])[0]), 1.1)
        self.assertTrue(np.isnan(fast._ef.roadsys_deck_depth([b], pts, tans, ["a"])[0]))
        # (a thinner deck clears the street with less height: never more)
        za = float(fast._ef.roadsys_blend([a], pts, tans, ["m"], True)[0])
        zb = float(fast._ef.roadsys_blend([b], pts, tans, ["m"], True)[0])
        self.assertLessEqual(zb, za + 1e-6)

    def test_end_lifts_raise_a_chain_onto_its_abutments_only_so_far(self):
        """(M3) roadsys_end_lifts: a free end under the plans' approach is lifted onto it (at most lift_max), never
        dropped; a joint takes the harmonic blend; the binding checks its shapes."""
        a = (np.array([[0.0, 0.0], [50.0, 0.0]]), np.array([10.0, 11.0]))
        b = (np.array([[50.0, 0.0], [100.0, 0.0]]), np.array([11.0, 10.0]))
        l = fast._ef.roadsys_end_lifts([a, b], [(10.6, float("nan")), (float("nan"), 9.0)], 1.0)
        self.assertAlmostEqual(float(l[0][0]), 0.6)
        self.assertAlmostEqual(float(l[1][1]), 0.0)
        self.assertAlmostEqual(float(l[0][1]), float(l[1][0]))
        l = fast._ef.roadsys_end_lifts([a, b], [(14.0, float("nan")), (float("nan"), 9.0)], 1.0)
        self.assertAlmostEqual(float(l[0][0]), 1.0)
        with self.assertRaises(ValueError):
            fast._ef.roadsys_end_lifts([a], [], 1.0)
        # dropped onto a lower approach, back on the profile by the part's hold (a floor it holds at 25 m)
        a3 = (np.array([[0.0, 0.0], [25.0, 0.0], [50.0, 0.0]]), np.array([10.0, 10.5, 11.0]))
        l = fast._ef.roadsys_end_lifts([a3], [(9.0, 11.0)], float("inf"), float("inf"), [[25.0]])
        self.assertAlmostEqual(float(l[0][0]), -1.0)
        self.assertAlmostEqual(float(l[0][1]), 0.0)
        self.assertAlmostEqual(float(l[0][2]), 0.0)

    def test_floor_points_are_the_decks_over_other_roads(self):
        """(M3) roadsys_floor_points: the motorway's floor over the street, on its own feature."""
        a = self.square(bridge_network())
        got = fast._ef.roadsys_floor_points([a], [-400.0, -100.0, -200.0, 100.0])
        self.assertTrue(any(f == "m" and abs(x + 300.0) < 1.0 and abs(y) < 1.0 for x, y, f in got), got)
        self.assertFalse(any(f != "m" for _x, _y, f in got))

    def test_chain_heights_through_a_store(self):
        """roadsys.chain_heights on a store standing in for roads.BridgeProfiles: the deck at the squares' heights,
        in the store's frame; a deck of no structure: None (today's)."""
        import roadsys
        import geotiles
        import mapcells
        lat0, lon0 = 40.0, -75.0
        fid, frame = roadsys.region(lat0, lon0)
        rows = bridge_network()

        class Feats:          # (one cell holding every row: the segments are given in the region frame)
            pass

        cc = roadsys._Cells(cell_fn=lambda k: None)
        tab = None

        def fake_table(key):        # (every cell holds a copy of every row: the copies are one segment)
            return dict(tab, cell=str(key))
        home = mapcells.cell_of(lat0, lon0)
        cc.table = fake_table
        # rows -> a table in lon / lat (the module's own layout), every row in the home cell
        ids, texts, nums, ints, lls, cons, rng = [], [], [], [], [], [], []
        for fid_, _cell, t, n, i, p, c, r in rows:
            la, lo, _ = frame.enu_to_geodetic(p[:, 0], p[:, 1], 0.0)
            ids.append(fid_); texts.append(t); nums.append(n); ints.append(i)
            lls.append(np.column_stack((np.asarray(lo), np.asarray(la)))); cons.append(list(c)); rng.append(list(r))
        tab = dict(ids=ids, texts=texts, nums=nums, ints=ints, ll=lls, cons=cons, rng=rng, cell=str(home))
        roadsys._cells_of[id(fake_table)] = (fake_table, cc)
        store = type("S", (), {})()
        store.frame = geotiles.WorldFrame(lat0, lon0)
        store.cell_fn = fake_table
        store.elev = lambda la, lo: np.full(np.shape(la), 5.0)

        def g_ll(la, lo):
            e, n, _ = frame.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
            return ground(e, n)
        la, lo, _ = frame.enu_to_geodetic(np.array([-300.0, -300.0, -300.0]), np.array([-30.0, 0.0, 30.0]), 0.0)
        e, n, _ = store.frame.geodetic_to_enu(la, lo, 0.0)
        deck = np.column_stack((e, n))
        roadsys._squares.clear()
        got = roadsys.chain_heights(store, [("m", 0)], {("m", 0): (None, deck, np.zeros(3), None)}, g_ll)
        self.assertIsNotNone(got)
        z = got["deck_z"][("m", 0)]
        self.assertEqual(len(z), 3)
        self.assertTrue(np.isfinite(z).all())
        e2, n2, _ = store.frame.geodetic_to_enu(la, lo, 0.0)
        self.assertIsNone(roadsys.chain_heights(store, [("a", 0)], {("a", 0): (None, deck + [400.0, 400.0], np.zeros(3), None)}, g_ll))
        roadsys._cells_of.pop(id(fake_table), None)
        roadsys._squares.clear()


    def test_same_chain_heights_whatever_the_order_chains_are_solved(self):
        """(M5 round 6) Two bridge chains solved one order in a fresh process state, then the other: each chain's deck
        heights and depths the same bytes, its digest the same (its squares, end lifts and floors its own inputs'
        alone, whichever chain solved the squares they share first)."""
        import roadsys
        import geotiles
        import mapcells
        lat0, lon0 = 40.0, -75.0
        _fid, frame = roadsys.region(lat0, lon0)
        rows = bridge_network() + [
            seg("n|0", [[300, -900], [300, 900]], cls=1, lanes=2, oneway=1, kmh=100.0, bridge=(0.48, 0.52))]
        ids, texts, nums, ints, lls, cons, rng = [], [], [], [], [], [], []
        for fid_, _cell, t, n, i, p, c, r in rows:
            la, lo, _ = frame.enu_to_geodetic(p[:, 0], p[:, 1], 0.0)
            ids.append(fid_); texts.append(t); nums.append(n); ints.append(i)
            lls.append(np.column_stack((np.asarray(lo), np.asarray(la)))); cons.append(list(c)); rng.append(list(r))
        tab = dict(ids=ids, texts=texts, nums=nums, ints=ints, ll=lls, cons=cons, rng=rng,
                   cell=str(mapcells.cell_of(lat0, lon0)))

        def fake_table(key):
            return dict(tab, cell=str(key))
        cc = roadsys._Cells(cell_fn=lambda k: None)
        cc.table = fake_table
        store = type("S", (), {})()
        store.frame = geotiles.WorldFrame(lat0, lon0)
        store.cell_fn = fake_table

        def elev(la, lo):           # (the abutments' ground: sloping, so the end lifts take a part)
            e, n, _ = frame.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
            return ground(e, n) - 1.5
        store.elev = elev

        def g_ll(la, lo):
            e, n, _ = frame.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
            return ground(e, n)

        def deck(x):
            la, lo, _ = frame.enu_to_geodetic(np.full(5, x), np.linspace(-36.0, 36.0, 5), 0.0)
            e, n, _ = store.frame.geodetic_to_enu(la, lo, 0.0)
            return np.column_stack((e, n))
        chains = {"m": ([("m", 0)], {("m", 0): (None, deck(-300.0), np.zeros(5), None)}),
                  "n": ([("n", 0)], {("n", 0): (None, deck(300.0), np.zeros(5), None)})}

        def solve(order):
            roadsys._squares.clear()
            roadsys._short_sq.clear()
            roadsys._cells_of[id(fake_table)] = (fake_table, cc)
            out = {}
            for c in order:
                got = roadsys.chain_heights(store, chains[c][0], chains[c][1], g_ll)
                self.assertIsNotNone(got, c)
                out[c] = (b"".join(str(k).encode() + np.ascontiguousarray(v, np.float64).tobytes()
                                   for k, v in sorted(got["deck_z"].items(), key=str))
                          + repr(sorted(got["depth"].items(), key=str)).encode(), got["digest"])
            return out
        try:
            a, b = solve(["m", "n"]), solve(["n", "m"])
        finally:
            roadsys._cells_of.pop(id(fake_table), None)
            roadsys._squares.clear()
            roadsys._short_sq.clear()
        self.assertEqual(a, b)


class ChainEndsTest(unittest.TestCase):
    def test_an_end_on_another_deck_of_the_chain_is_no_abutment(self):
        """(M5) roadsys._ends_on_decks: a viaduct's two parts 1.8 m apart (past endmatch's JOIN_M): each end's
        approach point lies on the other part, so neither is lifted or dropped to the ground under it; a real
        abutment's point (past the chain's outer end) is on no deck; a part never covers its own end."""
        import roadsys
        a = np.array([[0.0, 0.0], [50.0, 0.0]])
        b = np.array([[51.8, 0.0], [120.0, 0.0]])
        spec = type("Spec", (), {"width": 8.3})()
        decks = [("a", a, roadsys._half_width((spec, a))), ("b", b, roadsys._half_width((spec, b)))]
        pts = np.array([[-0.5, 0.0], [50.5, 0.0], [51.3, 0.0], [120.5, 0.0]])
        got = roadsys._ends_on_decks(pts, ["a", "a", "b", "b"], decks)
        self.assertEqual(got.tolist(), [False, True, True, False])
        self.assertEqual(roadsys._half_width((None, a)), roadsys.DECK_HALF_MIN_M)


class StructureDepthTest(unittest.TestCase):
    def test_some_bridges_are_thicker_than_others(self):
        """(M5) roadsys.structure_depth: a through truss's floor system whatever its span, a deck truss a tenth of its
        span or so, girders span / 22 within 0.6-4.0 m (a short slab thin, a long flyover deep)."""
        import bridgetypes as bt
        import roadsys
        self.assertEqual(roadsys.structure_depth(120.0, bt.NBI_THROUGH_TRUSS), roadsys.DEPTH_FLOOR_SYSTEM_M)
        self.assertAlmostEqual(roadsys.structure_depth(60.0, bt.NBI_DECK_TRUSS), 5.0)
        self.assertAlmostEqual(roadsys.structure_depth(8.0), 0.6)
        self.assertAlmostEqual(roadsys.structure_depth(44.0), 2.0)
        self.assertAlmostEqual(roadsys.structure_depth(200.0, bt.PLAIN), 4.0)
        self.assertLess(roadsys.structure_depth(15.0), roadsys.structure_depth(60.0))

    def test_railway_clearance_by_line_and_country(self):
        """(M5) roadsys.rail_clearance: a catenary's by country, a loading gauge's otherwise, light rail and metro
        their own; trams and lines not built hold no floor (RAIL_CLASSES)."""
        import roadsys
        self.assertEqual(roadsys.rail_clearance("US", "standard_gauge", False), 7.0)
        self.assertEqual(roadsys.rail_clearance("DE", "standard_gauge", True), 5.7)
        self.assertEqual(roadsys.rail_clearance("DE", "standard_gauge", False), 5.0)
        self.assertEqual(roadsys.rail_clearance("gb", "standard_gauge", False), 4.8)
        self.assertEqual(roadsys.rail_clearance("XX", "standard_gauge", True), 6.0)
        self.assertEqual(roadsys.rail_clearance("US", "light_rail", True), 5.0)
        self.assertNotIn("tram", roadsys.RAIL_CLASSES)
        self.assertNotIn("unknown", roadsys.RAIL_CLASSES)


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= 596, "earthfast 596 (roadsys) required")
class KernelTest(unittest.TestCase):
    def test_same_bytes_whatever_the_order(self):
        a, da = build(network())
        b, db = build(list(reversed(network())))
        self.assertGreater(a["tris"], 100)
        self.assertEqual(da, db)
        self.assertEqual(a["keys"], b["keys"])
        self.assertEqual(a["tile_hash"], b["tile_hash"])

    def test_store_reuse_gives_the_same_bytes(self):
        fast._ef.roadsys_clear()
        t1, d1 = fast._ef.roadsys_build(table(network()), QUAD, (0, 0), 1, ground)
        t2, d2 = fast._ef.roadsys_build(table(network()), QUAD, (0, 0), 1, ground)
        self.assertEqual(bytes(d1), bytes(d2))
        import json
        self.assertGreater(json.loads(t2)["profile"]["reused"], 0, "the second build reuses the solves")

    def test_one_copy_per_segment(self):
        rows = network()
        # a shorter copy of b in another cell: the longer one is read
        rows.append(seg("b", [[0, 0], [400, 0]], cons=[("n0", 0), ("n1", 1)], cell="14/0/1"))
        rows[1] = seg("b", [[0, 0], [200, 0], [400, 0]], cons=[("n0", 0), ("n1", 2)])
        rows2 = [r for r in rows if not (r[0] == "b" and r[1] == "14/0/1")]
        self.assertEqual(table(rows).counts()[:2], (6, 1))
        self.assertEqual(build(rows)[1], build(rows2)[1])

    def test_copies_are_one_segment_whatever_their_row_ids(self):
        """A row's id ends in its first point, which two cells' copies give differently: still one segment (the copy
        with the most points, all of its parts)."""
        rows = network()
        rows[1] = seg("b|0,0", [[0, 0], [200, 0], [400, 0]], cons=[("n0", 0), ("n1", 2)])
        other = rows + [seg("b|0.00002,0", [[0.02, 0], [400, 0]], cons=[("n0", 0), ("n1", 1)], cell="14/0/1")]
        self.assertEqual(table(other).counts()[:2], (6, 1))
        self.assertEqual(build(other)[1], build(rows)[1])

    def test_a_road_never_both_whole_and_as_its_sections(self):
        whole = network()
        sections = [r for r in whole if r[0] != "b"] + [
            seg("b~lanes000000000-500000000", [[0, 0], [200, 0]], cons=[("n0", 0), ("s", 1)], cell="14/0/1"),
            seg("b~lanes500000000-1000000000", [[200, 0], [400, 0]], cons=[("s", 0), ("n1", 1)], cell="14/0/1")]
        both = sections + [r for r in whole if r[0] == "b"]
        self.assertEqual(table(both).counts()[2], 1, "the whole road dropped for its covering sections")
        self.assertEqual(build(both)[1], build(sections)[1])
        partial = [r for r in both if r[0] != "b~lanes500000000-1000000000"]
        self.assertEqual(table(partial).counts()[3], 1, "a partial set of sections dropped for the whole road")
        self.assertEqual(build(partial)[1], build(whole)[1])

    def test_neighbours_share_their_seam(self):
        east = [(128.0, -128.0), (384.0, -128.0), (384.0, 128.0), (128.0, 128.0)]
        a, _ = build(network(), QUAD, (0, 0))
        b, _ = build(network(), east, (1, 0))
        self.assertEqual(a["seams"][1], b["seams"][3])
        self.assertNotEqual(a["seams"][1].split(":")[0], "0", "a road crosses the seam")

    def test_keys_follow_the_read_list(self):
        base, _ = build(network())
        far = network() + [seg("far", [[9000, 9000], [9400, 9000]])]
        self.assertEqual(build(far)[0]["keys"], base["keys"], "a segment outside every read list moves no key")
        near = network()
        near[4] = seg("e", [[400, 0], [400, 300]], lanes=4, cons=[("n1", 0)])
        moved = build(near)[0]["keys"]
        self.assertNotEqual(moved, base["keys"], "a lane count inside the read list moves the keys")

    def test_reads_cover_the_tile_and_its_strokes(self):
        boxes, fids, counts, _cpu = fast._ef.roadsys_reads(table(network()), [-128.0, -128.0, 128.0, 128.0])
        self.assertIn("a", fids)
        self.assertGreater(counts[0], 0)
        x0 = min(b[0] for b in boxes)
        # (a street with no fork and no lane change reads the arms at its nodes, 60 m: its lane program walks nothing;
        # a fork's walks would read 920 m)
        self.assertLessEqual(x0, -400 - 50, "the whole stroke and the arms at its nodes")
        self.assertGreater(x0, -400 - 900, "no lane-graph walk without a fork or a lane change")

    def test_the_read_list_is_all_a_build_needs(self):
        rows = network() + [seg("far", [[9000, 9000], [9400, 9000]], cons=[("x", 0)]),
                            seg("far2", [[9400, 9000], [9400, 9400]], cons=[("x", 0)])]
        boxes, _fids, _counts, _cpu = fast._ef.roadsys_reads(table(rows), [-128.0, -128.0, 128.0, 128.0])
        fast._ef.roadsys_clear()
        _t, whole = fast._ef.roadsys_build(table(rows), QUAD, (0, 0), 1, ground)
        fast._ef.roadsys_clear()
        _t, part = fast._ef.roadsys_build(table(rows), QUAD, (0, 0), 1, ground, boxes=boxes)
        self.assertEqual(bytes(whole), bytes(part))

    def test_audit_binding(self):
        import struct
        n = 4
        blob = b"RSAUDIT1" + np.array([-30, -30, 30, -30, 30, 30, -30, 30], "<f8").tobytes()
        blob += struct.pack("<Iqq", n, -2, -2) + np.zeros(n * n, "<f4").tobytes() + bytes(n * n)
        blob += struct.pack("<I", 0) + struct.pack("<I", 0)
        import json
        # (the auditor's measuring rules are stamped on every result, audit.rs AUDIT_VERSION: not a count)
        got = json.loads(fast._ef.roadsys_audit(blob))
        self.assertFalse(any(v for k, v in got.items() if k != "audit_version"), "an empty scene: nothing counted")
        with self.assertRaises(ValueError):
            fast._ef.roadsys_audit(b"nope")


class SwitchTest(unittest.TestCase):
    def test_off_unless_set(self):
        import roadsys
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BNE_ROADSYS", None)
            importlib.reload(roadsys)
            self.assertFalse(roadsys.SHADOW)
            self.assertFalse(roadsys.HEIGHTS)
            self.assertIsNone(roadsys.enqueue(0, 0, object(), object(), "/nonexistent"))
            self.assertIsNone(roadsys._q, "no thread when off")
        with mock.patch.dict(os.environ, {"BNE_ROADSYS": "shadow"}):
            importlib.reload(roadsys)
            self.assertTrue(roadsys.SHADOW)
            self.assertFalse(roadsys.HEIGHTS)
        with mock.patch.dict(os.environ, {"BNE_ROADSYS": "heights"}):
            importlib.reload(roadsys)
            self.assertTrue(roadsys.HEIGHTS)
            self.assertFalse(roadsys.SHADOW)
        importlib.reload(roadsys)

    def test_a_key_switch_only_at_heights(self):
        """off / shadow change nothing: no key holds them; heights changes the decks and the plans: the tiles', the
        plans' and the facts' keys hold it (switches_key, plans_switches_key) and the plans' format has its suffix."""
        self.assertNotIn("BNE_ROADSYS", revisit.NOT_SWITCHES)
        self.assertNotIn("BNE_ROADSYS", revisit.PLAN_NEUTRAL)
        revisit._switches.clear()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BNE_ROADSYS", None)
            revisit._switches.clear()
            base, base_p = revisit.switches_key(), revisit.plans_switches_key()
            self.assertFalse(revisit.keyed_env("BNE_ROADSYS"))
            for v in ("shadow", "off", " Shadow "):
                with mock.patch.dict(os.environ, {"BNE_ROADSYS": v}):
                    revisit._switches.clear()
                    self.assertFalse(revisit.keyed_env("BNE_ROADSYS"))
                    self.assertEqual((revisit.switches_key(), revisit.plans_switches_key()), (base, base_p))
            with mock.patch.dict(os.environ, {"BNE_ROADSYS": "heights"}):
                revisit._switches.clear()
                self.assertTrue(revisit.keyed_env("BNE_ROADSYS"))
                self.assertNotEqual(revisit.switches_key(), base)
                self.assertNotEqual(revisit.plans_switches_key(), base_p)
                self.assertIn("@v", revisit.keyed_value("BNE_ROADSYS", "heights"))
        revisit._switches.clear()

    def test_plans_format_suffix_at_heights(self):
        import plancache
        import roadsys
        with mock.patch.object(roadsys, "HEIGHTS", False):
            off = plancache.plans_format()
        with mock.patch.object(roadsys, "HEIGHTS", True):
            on = plancache.plans_format()
        self.assertNotEqual(off, on)
        self.assertTrue(on.startswith(off))

    def test_heights_joins_the_joint_profile_plumbing(self):
        """blockworker: the bridge stores on the plans' first stage, the plans' second stage from the squares' terrain
        targets, the profiles on the plans' own ground, only with heights (or P3b)."""
        src = (HERE / "blockworker.py").read_text(encoding="utf-8")
        self.assertIn("return roadshape.PROFILE_BUILT or roadsys.HEIGHTS", src)
        self.assertIn("if _joint_built():\n    gradingmod.PROFILE_PINS = _profile_pins", src)
        i = src.index("if roadsys.HEIGHTS and not roadshape.PROFILE_BUILT:")
        self.assertIn("roadsys.pins_near(", src[i:i + 400])

    def test_only_with_the_revamped_roads_setting(self):
        # (the user, 2026-11-24: "make sure the revamped roads toggle uses the updated rewritten roads": no page entry
        # of its own, only inside Revamped roads, at the M4 surface mode with the areas fit and cross-sections)
        import experimental
        holders = [sid for sid, env, _n in experimental.FEATURES if any(k.startswith("BNE_ROADSYS") for k in env)]
        self.assertEqual(holders, ["expRoadRevamp"])
        env = {sid: env for sid, env, _n in experimental.FEATURES}["expRoadRevamp"]
        self.assertEqual((env["BNE_ROADSYS"], env["BNE_ROADSYS_AREAS"], env["BNE_ROADSYS_XSEC"]), ("surface", "on", "on"))

    def test_the_tile_path_only_reads_the_flag(self):
        src = (HERE / "blockworker.py").read_text(encoding="utf-8")
        i = src.index("roadsys.enqueue(")
        self.assertIn("if roadsys.SHADOW:", src[i - 200:i])


def _ends(bridges, grounds, tunnels=(), s=1.0):
    return fast._ef.RoadsysEnds([(np.asarray(p, np.float64), hw) for p, hw in bridges],
                                [(np.asarray(p, np.float64), hw) for p, hw in grounds],
                                [np.asarray(p, np.float64) for p in tunnels], s)


def _deck(x0, x1, hw, z, step=2.0):
    """A flat deck top (material 1) from x0 to x1, |y - 0.2| <= hw, in rows of step m: (p, uv, t, m)."""
    P, T = [], []
    x = x0
    while x < x1 - 1e-9:
        xn = min(x + step, x1)
        b = len(P)
        P += [[x, 0.2 - hw, z], [xn, 0.2 - hw, z], [xn, 0.2 + hw, z], [x, 0.2 + hw, z]]
        T += [[b, b + 1, b + 2], [b, b + 2, b + 3]]
        x = xn
    P = np.asarray(P, np.float64)
    return P, np.zeros((len(P), 2)), np.asarray(T, np.int64), np.ones(len(T), np.int64)


@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= 603, "earthfast 603 (roadsys structures, M3) required")
class StructuresTest(unittest.TestCase):
    """BNE_ROADSYS=structures (M3): heights plus the deck ends' hand-over; keyed apart from heights; the ground's drop a
    function of the map's parts alone (any order, any copies, any batch); the deck's top meets the terrain's own nodes."""

    def test_structures_is_heights_and_more(self):
        import roadsys
        with mock.patch.dict(os.environ, {"BNE_ROADSYS": "structures"}):
            importlib.reload(roadsys)
            self.assertTrue(roadsys.HEIGHTS and roadsys.STRUCTURES)
            self.assertFalse(roadsys.SHADOW)
        with mock.patch.dict(os.environ, {"BNE_ROADSYS": "heights"}):
            importlib.reload(roadsys)
            self.assertTrue(roadsys.HEIGHTS)
            self.assertFalse(roadsys.STRUCTURES)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BNE_ROADSYS", None)
            importlib.reload(roadsys)
            self.assertFalse(roadsys.STRUCTURES)
            self.assertEqual(roadsys.handover_drop(None, None, np.zeros(3), np.zeros(3)).tolist(), [0.0] * 3)
        importlib.reload(roadsys)

    def test_keyed_apart_from_heights(self):
        h = revisit.keyed_value("BNE_ROADSYS", "heights")
        st = revisit.keyed_value("BNE_ROADSYS", "structures")
        self.assertNotEqual(h, st)
        self.assertIn(f"s{revisit.ROADSYS_STRUCTURES_VERSION}", st)
        self.assertIn(f"@v{revisit.SWITCH_VERSIONS['BNE_ROADSYS']}", st, "heights' meaning moves it too")
        keys = {}
        for v in ("heights", "structures"):
            with mock.patch.dict(os.environ, {"BNE_ROADSYS": v}):
                revisit._switches.clear()
                keys[v] = (revisit.switches_key(), revisit.plans_switches_key())
        revisit._switches.clear()
        self.assertNotEqual(keys["heights"][0], keys["structures"][0])
        self.assertNotEqual(keys["heights"][1], keys["structures"][1])

    def test_every_ground_evaluator_drops_alike(self):
        """roads.block_arch_raise (the blocks', the skins' and the content's drawn ground) is the one place the drop is
        applied; the exact deck reads its nodes through heightmap.surface and the same drop."""
        src = (HERE / "roads.py").read_text(encoding="utf-8")
        i = src.index("def block_arch_raise(")
        self.assertIn("roadsys.ground_cut(frame, cell_fn, la, lo, z)", src[i:i + 1500])
        bw = (HERE / "blockworker.py").read_text(encoding="utf-8")
        for caller in ("def gen(la, lo):", "def drawn(la, lo):"):
            j = bw.index(caller)
            self.assertIn("block_arch_raise(", bw[j:j + 400])
        j = bw.index("def _deck_handover(")
        body = bw[j:j + 6000]
        self.assertIn("heightmap.surface(fr, drawn, xs, ys, keep)", body)
        self.assertIn('roadsys.handover_drop(fr, cell_fn, keep["lat"], keep["lon"])', body)
        # (M5: under surface the lattice's lowering is the drop and the ceiling together, from the same ground before)
        self.assertIn('roadsys.ground_lowering(fr, cell_fn, keep["lat"], keep["lon"],', body)
        self.assertIn("np.rint(np.asarray(u, np.float64) * (1.0 / q)) * q", body)

    def test_ground_cut_is_the_drop_alone_under_structures(self):
        import roadsys
        try:
            with mock.patch.dict(os.environ, {"BNE_ROADSYS": "structures"}):
                importlib.reload(roadsys)
                self.assertFalse(roadsys.CEIL)
                with mock.patch.object(roadsys, "handover_drop", lambda f, c, la, lo: np.full(np.shape(la), 0.25)):
                    got = roadsys.ground_cut(None, None, np.zeros(3), np.zeros(3), np.array([1.0, 2.0, 3.0]))
                    self.assertEqual(got.tolist(), [0.75, 1.75, 2.75])
                    self.assertEqual(roadsys.ground_lowering(None, None, np.zeros(3), np.zeros(3), np.ones(3)).tolist(),
                                     [0.25] * 3)
        finally:
            importlib.reload(roadsys)
        z = np.array([1.0, 2.0])
        self.assertIs(roadsys.ground_cut(None, None, np.zeros(2), np.zeros(2), z), z)     # (off: untouched)

    def test_ends_are_the_maps_alone(self):
        deck = ([[0.3, 0.2], [60.0, 0.2]], 4.0)
        app = ([[-40.0, 0.2], [0.3, 0.2]], 4.0)
        far = ([[60.0, 0.2], [90.0, 0.2]], 4.0)     # (the far end goes on as another deck: no ground end)
        other = ([[4.0, -30.0], [4.0, 30.0]], 0.4)  # (a road crossing under it keeps its ground)
        section = ([[0.3, 0.2], [30.0, 0.2]], 4.0)  # (a copy of the deck from its end: the same end)
        x = np.linspace(-5, 30, 141)
        y = np.concatenate([np.zeros(141), np.full(141, 3.9), np.full(141, 4.6)])
        x = np.tile(x, 3)
        a = _ends([deck, far], [app, other])
        b = _ends([far, deck, section, deck], [other, app, app])
        self.assertEqual(a.count(), 1)
        da, db = a.drops(x, y), b.drops(x, y)
        self.assertEqual(da.tobytes(), db.tobytes())
        # one point at a time: the same values (no batch effects)
        one = np.array([a.drops(np.array([u]), np.array([v]))[0] for u, v in zip(x, y)])
        self.assertEqual(one.tobytes(), da.tobytes())
        self.assertTrue((da[(x < 1.5) & (y == 0)] == 0).all())
        self.assertTrue((da[(x > 2) & (x < 5) & (abs(x - 4) > 1.0) & (y == 0)] > 0).all())
        self.assertTrue((da[(abs(x - 4) < 0.8) & (y == 0)] == 0).all(), "the crossing road's surface keeps its ground")
        self.assertTrue((da[(x > 9) & (y == 0)] == 0).all(), "only the hand-over's metres")
        self.assertTrue((da[y == 4.6] == 0).all(), "beside the deck")

    def test_the_deck_meets_the_terrains_own_nodes(self):
        """The zone's free edges toward the approach lie on lattice edges at the terrain's quantised node heights (a 0 mm
        lip) though the deck stood 3 cm over its ground; its slab past the end cleared; the same bytes twice."""
        ends = _ends([([[0.3, 0.2], [60.0, 0.2]], 4.0)], [([[-40.0, 0.2], [0.3, 0.2]], 4.0)])
        i0, j0, n = -45, -20, 90
        xs, ys = np.meshgrid((i0 + np.arange(n)).astype(float), (j0 + np.arange(n)).astype(float))
        drop = ends.drops(xs.ravel(), ys.ravel()).reshape(n, n)
        q = 1.0 / 64
        z = np.rint((10.0 + 0.01 * xs) / q) * q - drop
        P, UV, T, M = _deck(0.3, 40.0, 4.45, 0.0)
        P[:, 2] = 10.03 + 0.01 * P[:, 0]
        P2, UV2, T2, M2 = _deck(-5.0, 0.3, 4.45, 0.0)
        P2[:, 2] = 10.02 + 0.01 * P2[:, 0]
        P = np.vstack([P, P2]); UV = np.vstack([UV, UV2]); T = np.vstack([T, T2 + len(P) - len(P2)]); M = np.concatenate([M, M2])
        out = fast._ef.roadsys_handover_zone(ends, P, UV, T, M, 1, i0, j0, 1.0, z, drop, 4.0)
        again = fast._ef.roadsys_handover_zone(ends, P, UV, T, M, 1, i0, j0, 1.0, z, drop, 4.0)
        for a, b in zip(out[:4], again[:4]):
            self.assertEqual(np.asarray(a).tobytes(), np.asarray(b).tobytes())
        p, t = np.asarray(out[0]), np.asarray(out[2])
        self.assertFalse(((p[:, 0] < 0) & (p[:, 2] < 11)).any(), "the slab past the end is cleared")
        edges = {}
        for tri in t:
            for k in range(3):
                a, b = tuple(np.round(p[tri[k]] * 1000).astype(int)), tuple(np.round(p[tri[(k + 1) % 3]] * 1000).astype(int))
                if a != b:
                    key = (min(a, b), max(a, b))
                    edges[key] = edges.get(key, 0) + 1
        free = [(np.array(a) / 1000, np.array(b) / 1000) for (a, b), c in edges.items() if c == 1]
        near = [(a, b) for a, b in free if max(a[0], b[0]) < 3.0 and abs((a[1] + b[1]) / 2 - 0.2) < 3.0]
        self.assertTrue(near)
        ter = lambda x: np.rint((10.0 + 0.01 * x) / q) * q   # noqa: E731
        for a, b in near:
            self.assertTrue(a[0] == round(a[0]) and b[0] == round(b[0]) or a[1] == round(a[1]) and b[1] == round(b[1]))
            self.assertAlmostEqual(a[2], ter(a[0]), places=3)
            self.assertAlmostEqual(b[2], ter(b[0]), places=3)

    def test_exact_always_for_a_handing_over_deck(self):
        import geotiles
        import roads
        tf = geotiles.frame(*geotiles.parse("g17_22540_52454")[1:])
        got = roads.geo_exact_auto(tf, tf, {"deck": {"pivot": 3.0, "relief": 0.0, "col": True, "ho": True},
                                            "bld": {"pivot": 3.0, "relief": 0.0}})
        self.assertEqual(got, {"deck": {"pivot": 3.0}})



@unittest.skipUnless(fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= 606, "earthfast 606 (roadsys surface, M4) required")
class SurfaceTest(unittest.TestCase):
    """BNE_ROADSYS=surface (M4): structures plus the decks drawn from the road system's own faces; keyed apart from
    structures; the decks take today's deck heights (flat across), parapets on the free edges only, the same bytes
    whatever the map's order; today's deck chunks the faces stand in for are drawn by neither mesh."""

    MOTORWAY = [(-300.0, -900.0), (-300.0, 900.0)]

    def lines(self, z_end=10.0):
        # (today's deck over the street: m|0 from 0.48 to 0.52 of its 1800 m, its middle 6 m up)
        y0, y1 = -900.0 + 0.48 * 1800.0, -900.0 + 0.52 * 1800.0
        g0, g1 = ground(-300.0, y0), ground(-300.0, y1)
        return [("m", np.array([[-300.0, y0, g0 + z_end - 10.0], [-300.0, 0.0, ground(-300.0, 0.0) + 6.0],
                                [-300.0, y1, g1 + z_end - 10.0]], np.float64))]

    def decks(self, rows, lines):
        quad = [(-428.0, -128.0), (-172.0, -128.0), (-172.0, 128.0), (-428.0, 128.0)]
        return fast._ef.roadsys_decks(table(rows), quad, ground, lines)

    def test_surface_is_structures_and_more(self):
        import roadsys
        with mock.patch.dict(os.environ, {"BNE_ROADSYS": "surface"}):
            importlib.reload(roadsys)
            self.assertTrue(roadsys.HEIGHTS and roadsys.STRUCTURES and roadsys.SURFACE)
        with mock.patch.dict(os.environ, {"BNE_ROADSYS": "structures"}):
            importlib.reload(roadsys)
            self.assertTrue(roadsys.STRUCTURES)
            self.assertFalse(roadsys.SURFACE)
        importlib.reload(roadsys)
        self.assertFalse(roadsys.SURFACE)

    def test_keyed_apart_from_structures(self):
        st = revisit.keyed_value("BNE_ROADSYS", "structures")
        su = revisit.keyed_value("BNE_ROADSYS", "surface")
        self.assertNotEqual(st, su)
        self.assertIn(f"u{revisit.ROADSYS_SURFACE_VERSION}", su)
        keys = {}
        for v in ("structures", "surface"):
            with mock.patch.dict(os.environ, {"BNE_ROADSYS": v}):
                revisit._switches.clear()
                keys[v] = revisit.switches_key()
        revisit._switches.clear()
        self.assertNotEqual(keys["structures"], keys["surface"])

    def test_decks_take_todays_heights_and_parapets_on_free_edges(self):
        V, T, K, F, D, feats, runs, MV, MT, MC, fallback, js = self.decks(bridge_network(), self.lines())
        self.assertEqual(list(fallback), [])
        self.assertEqual(list(feats), ["m"])
        V, T, K = np.asarray(V), np.asarray(T), np.asarray(K)
        top = T[K != 9]
        self.assertGreater(len(top), 10)
        mid = [V[i] for t in top for i in t if abs(V[i][1]) < 0.3]
        self.assertTrue(mid)
        for p in mid:           # (flat across: every vertex of the middle row at today's middle height)
            self.assertAlmostEqual(p[2], ground(-300.0, 0.0) + 6.0, delta=0.02)
        self.assertEqual(len(runs), 2, js)       # (one along each side; none across the deck's ends)
        for pts, nrm, closed, _f in runs:
            pts = np.asarray(pts)
            self.assertFalse(closed)
            self.assertGreater(float(np.ptp(pts[:, 1])), 60.0)
            self.assertLess(float(np.ptp(pts[:, 0])), 1.0)

    def test_no_deck_heights_is_todays_deck(self):
        out = self.decks(bridge_network(), [])
        self.assertEqual(list(out[10]), ["m"])
        self.assertEqual(len(out[1]), 0)

    def test_same_decks_whatever_the_order(self):
        a = self.decks(bridge_network(), self.lines())
        b = self.decks(list(reversed(bridge_network())), self.lines())
        self.assertEqual(np.asarray(a[0]).tobytes(), np.asarray(b[0]).tobytes())
        self.assertEqual(np.asarray(a[1]).tobytes(), np.asarray(b[1]).tobytes())

    def test_ground_end_meets_the_ground(self):
        V, T, K, F, D, feats, runs, MV, MT, MC, fallback, js = self.decks(bridge_network(), self.lines(z_end=10.4))
        V = np.asarray(V)
        y0 = -900.0 + 0.48 * 1800.0
        end = [p for p in V if abs(p[1] - y0) < 0.05 and abs(p[0] + 300.0) < 4.0]
        self.assertTrue(end)
        want = float(ground(-300.0, y0 - 0.5))
        for p in end:
            self.assertAlmostEqual(p[2], want, delta=0.02)

    def test_hidden_chunks_are_drawn_by_neither_mesh(self):
        import roadmesh
        c = {"m": "asphalt", "w": 7.0, "n": [[0.0, 0.0, 5.0], [20.0, 0.0, 5.0]], "deck": True, "fixed": True}
        self.assertIsNotNone(roadmesh.deck([dict(c)], (0.0, 0.0, 0.0)))
        self.assertIsNone(roadmesh.deck([dict(c, rs_hide=True)], (0.0, 0.0, 0.0)))
        self.assertEqual(roadmesh.parapet_runs([dict(c, rs_hide=True)]), [])
        self.assertEqual(roadmesh._shown([dict(c), dict(c, rs_hide=True)]), [dict(c)])


class CeilTest(unittest.TestCase):
    """M5 round 4 (BNE_ROADSYS=surface, earthfast 630): the ground under a deck at most at its soffit less 0.3 m
    (bridgerewrite/DESIGN.md G5), eased in from its free ends past the hand-over's own metres, a road under it and its
    lattice square kept; one value per point whatever the order of the decks; the drop as before where no deck is."""

    DECK = [[0.0, 0.0], [30.0, 0.0], [60.0, 0.0]]

    def ceil(self, decks, grounds=(), rails=()):
        e = _ends([(self.DECK, 5.0)], [([[-50.0, 0.0], [0.0, 0.0]], 5.0), ([[60.0, 0.0], [110.0, 0.0]], 5.0)] + list(grounds))
        return e, e.ceil([(np.asarray(p, np.float64), hw, d) for p, hw, d in decks],
                         [np.asarray(r, np.float64) for r in rails])

    def low(self, top=10.0, depth=1.2):
        return [[x, y, top] for x, y in self.DECK], 5.0, depth

    def test_under_a_low_deck_at_most_its_soffit_less_clearance(self):
        e, c = self.ceil([self.low()])
        x = np.array([30.0, 30.0, 3.0, 1.0, 7.0, 30.0, -3.0])
        y = np.array([0.0, 4.9, 0.0, 0.0, 0.0, 5.5, 0.0])
        got = c.ground(x, y, np.full(7, 10.0))
        want = [10.0 - 1.5, 10.0 - 1.5, 9.7, 10.0, 10.0 - 1.3, 10.0, 10.0]
        self.assertEqual(np.round(got, 9).tolist(), want)
        # (where no deck is: the drop alone, as before)
        np.testing.assert_array_equal(c.ground(x, y + 40.0, np.full(7, 10.0)), 10.0 - e.drops(x, y + 40.0))
        # (a deck high over its ground cuts nothing)
        _e, hi = self.ceil([self.low(top=30.0)])
        self.assertEqual(hi.ground(np.array([30.0]), np.array([0.0]), np.array([10.0])).tolist(), [10.0])

    def test_a_road_under_it_keeps_its_ground(self):
        _e, c = self.ceil([self.low()], grounds=[([[30.0, -40.0], [30.0, 40.0]], 3.0)])
        got = c.ground(np.array([30.0, 35.5, 36.5, 42.0]), np.array([1.0, 1.0, 1.0, 1.0]), np.full(4, 10.0))
        self.assertEqual(np.round(got, 9).tolist(), [10.0, 10.0, 9.5, 8.5])

    def test_same_ground_whatever_the_order_of_the_decks(self):
        a = self.low()
        b = ([[20.0, -20.0, 9.0], [20.0, 20.0, 9.0]], 4.0, 0.8)
        rv = ([list(p) for p in reversed(a[0])], a[1], a[2])
        _e, c1 = self.ceil([a, b])
        _e, c2 = self.ceil([b, rv, a])
        g = np.mgrid[-10:70:0.5, -25:25:0.5].reshape(2, -1)
        z = np.full(g.shape[1], 12.0)
        self.assertEqual(c1.ground(g[0], g[1], z).tobytes(), c2.ground(g[0], g[1], z).tobytes())
        self.assertGreater(len(c1.input()), 100)

    def test_ground_cut_reads_the_cells_ceilings(self):
        """roadsys.ground_cut under surface: each point by its own map cell's ceiling (ceil_of_cell), the ground
        before the drop and the ceiling in, the lowering what it took off."""
        import roadsys
        _e, c = self.ceil([self.low()])

        class F:
            lat0 = lon0 = 0.0

            def geodetic_to_enu(self, la, lo, h):
                return np.asarray(lo, np.float64) * 1e5, np.asarray(la, np.float64) * 1e5, np.zeros(np.shape(la))
        try:
            with mock.patch.dict(os.environ, {"BNE_ROADSYS": "surface"}):
                importlib.reload(roadsys)
                self.assertTrue(roadsys.CEIL)
                with mock.patch.object(roadsys, "ceil_of_cell", lambda f, cf, k, box=None: c):
                    la = np.array([0.0, 0.0, 4e-4])
                    lo = np.array([3e-4, -3e-5, 3e-4])
                    z = np.full(3, 10.0)
                    got = roadsys.ground_cut(F(), None, la, lo, z)
                    self.assertEqual(np.round(got, 9).tolist(), [8.5, 10.0, 10.0])
                    self.assertEqual(np.round(roadsys.ground_lowering(F(), None, la, lo, z), 9).tolist(), [1.5, 0.0, 0.0])
        finally:
            importlib.reload(roadsys)

    def test_a_short_ceiling_answers_a_while_and_is_never_kept(self):
        import mapcells
        import roadsys
        e, _c = self.ceil([self.low()])
        calls = []

        def inputs(frame, cell_fn, keys, reg, view):
            calls.append(1)
            revisit.fault()             # (a short read)
            return [], []

        class F:
            lat0 = lon0 = 0.0

            def geodetic_to_enu(self, la, lo, h):
                return np.asarray(lo, np.float64) * 1e5, np.asarray(la, np.float64) * 1e5, np.zeros(np.shape(la))
        key = mapcells.CellKey(mapcells.ZOOM, 1, 1)
        roadsys._ceil_memo.clear()
        with mock.patch.object(roadsys, "ends_of_cell", lambda f, cf, k: e), \
                mock.patch.object(roadsys, "_decks_view", lambda f: object()), \
                mock.patch.object(roadsys, "_ceil_inputs", inputs):
            for _ in range(3):
                with revisit.scope() as b:
                    roadsys.ceil_of_cell(F(), None, key)
                self.assertFalse(b.complete, "every use of a short ceiling is short")
            self.assertEqual(len(calls), 1)
            with mock.patch.object(roadsys, "CEIL_SHORT_TTL_S", -1.0):
                roadsys.ceil_of_cell(F(), None, key)
            self.assertEqual(len(calls), 2, "read again once its time is up")
        roadsys._ceil_memo.clear()

    def test_a_ceiling_reads_the_decks_near_the_points_asked(self):
        """(M5 r5) ground_cut asks each cell's ceiling for its points' box snapped out to CEIL_SNAP_M; the decks are
        read within the cell + 60 m and that box + ceil_reach (their chains alone solved: the whole cell's made the
        corpus' first visits x5-20 CPU), one ceiling per cell and box."""
        import mapcells
        import roadsys
        e, _c = self.ceil([self.low()])
        regs = []

        def inputs(frame, cell_fn, keys, reg, view):
            regs.append(reg)
            return [], []

        class F:
            lat0 = lon0 = 0.0

            def geodetic_to_enu(self, la, lo, h):
                return np.asarray(lo, np.float64) * 1e5, np.asarray(la, np.float64) * 1e5, np.zeros(np.shape(la))
        key = mapcells.CellKey(mapcells.ZOOM, 1 << (mapcells.ZOOM - 1), (1 << (mapcells.ZOOM - 1)) - 1)
        la0, lo0, la1, lo1 = roadsys._cell_bounds(key)
        roadsys._ceil_memo.clear()
        try:
            with mock.patch.dict(os.environ, {"BNE_ROADSYS": "surface"}):
                importlib.reload(roadsys)
                roadsys._ceil_memo.clear()
                with mock.patch.object(roadsys, "ends_of_cell", lambda f, cf, k: e), \
                        mock.patch.object(roadsys, "_decks_view", lambda f: object()), \
                        mock.patch.object(roadsys, "ceil_reach", lambda cf, keys: 50.0), \
                        mock.patch.object(roadsys, "_ceil_inputs", inputs):
                    la = np.array([(la0 + la1) / 2, (la0 + la1) / 2 + 1e-5])
                    lo = np.array([lo0 + 2e-5, lo0 + 3e-5])
                    for _ in range(2):
                        roadsys.ground_cut(F(), None, la, lo, np.full(2, 5.0))
                    self.assertEqual(len(regs), 1, "one ceiling per cell and box")
                    x, y = lo * 1e5, la * 1e5
                    box = roadsys._snap_box(x, y)
                    self.assertTrue(box[0] <= x.min() and box[2] >= x.max() and box[1] <= y.min() and box[3] >= y.max())
                    self.assertEqual([v % roadsys.CEIL_SNAP_M for v in box], [0.0] * 4)
                    reg = regs[0]
                    self.assertEqual(reg[0], max(lo0 * 1e5 - 60.0, box[0] - 50.0))     # (the cell's own edge + 60 m)
                    self.assertEqual(reg[2], box[2] + 50.0)
                    self.assertEqual(reg[1], max(la0 * 1e5 - 60.0, box[1] - 50.0))
                    self.assertEqual(reg[3], min(la1 * 1e5 + 60.0, box[3] + 50.0))
                    roadsys.ceil_of_cell(F(), None, key)                # (no box: the whole cell, as before)
                    self.assertEqual(regs[-1], (lo0 * 1e5 - 60.0, la0 * 1e5 - 60.0, lo1 * 1e5 + 60.0, la1 * 1e5 + 60.0))
        finally:
            importlib.reload(roadsys)
            roadsys._ceil_memo.clear()

    def test_a_ceilings_reach_is_the_widest_structure_rounds(self):
        """ceil_reach: CEIL_EASE_M + 3.74 x the widest bridge's half width round the cell + CEIL_XY_TOL_M, less the
        parts' NEAR_M (their boxes come that near the asked box)."""
        import roads
        import roadsys

        class Sp:
            def __init__(self, w):
                self.width = w

        class Fe(list):
            ids = ["w1", "w2"]
        specs = {"a": [None, Sp(12.0)], "b": [Sp(18.0), None], "c": [None, None]}
        cells = {k: Fe([1, 2]) for k in specs}
        for k, f in cells.items():
            f.k = k
        with mock.patch.object(roadsys, "XSEC", False), \
                mock.patch.object(roads, "bridge_specs", lambda f: specs[f.k]):
            got = roadsys.ceil_reach(lambda k: cells[k], ["a", "b", "c"])
        self.assertAlmostEqual(got, roadsys.CEIL_EASE_M + 3.74 * 9.0 + roadsys.CEIL_XY_TOL_M - roads.NEAR_M)

    def test_the_ceiling_takes_a_short_chain_as_published(self):
        """(M5 r5) the ceiling's read (ChainView.parts_published: parts_todo retry=False) takes a part published short
        as it is, its note making the reader short; it never asks the chain to be solved again (every ceiling box
        asked a short chain again each PROFILE_RETRY_S: railramp's corpus visit solved 11 chains more)."""
        import threading
        import roads
        st = roads.BridgeProfiles.__new__(roads.BridgeProfiles)
        st.lock = threading.Lock()
        st.done = {("w1", 0): ("spec", np.zeros((2, 2)), np.zeros(2), 0.0)}
        st.__dict__["short"] = {("w1", 0): (1, 0)}
        st.__dict__["profile_retry"] = {("w1", 0): ("c", 0.0)}
        asked = []
        st._retry_due = lambda k: asked.append(k)
        st._part_id = lambda k: k[0]
        with mock.patch.object(roads.BridgeProfiles, "CANON_LOAD", False):
            with revisit.scope() as b:
                got = st.parts_todo([(("w1", 0), None, 0)], part_ids=True, retry=False)
            self.assertEqual([g[0] for g in got], [("w1", 0)])
            self.assertFalse(b.complete, "its reader is short")
            self.assertEqual(asked, [])
            st.parts_todo([(("w1", 0), None, 0)], part_ids=True)
            self.assertEqual(asked, [("w1", 0)], "the other readers ask it again as before")

    def test_the_ceiling_keeps_every_drawn_railways_ground(self):
        """(M5 r5) _ceil_rails: the floors' railways and every line railways draws on the ground (its "ground"
        pieces: a short tunnel mapped under a road bridge is drawn there with its collider; 0bff's 19 lips)."""
        import railways
        import roads
        import roadsys

        class Fe(list):
            pass
        f = Fe([1, 2, 3])
        floor = np.array([[0.0, 0.0], [1.0, 0.0]])
        short = [np.array([[0.0, 1.0], [0.0, 2.0]])]
        with mock.patch.object(roadsys, "_rails", lambda feats: [("r0", floor, 5.0)]), \
                mock.patch.object(roads, "_rail_drawn", lambda feats, i: i != 1), \
                mock.patch.object(railways, "_pieces", lambda feats, i: [("ground", short), ("bridge", [floor + i])]):
            got = roadsys._ceil_rails(f)
        self.assertEqual([g.tolist() for g in got], [floor.tolist(), short[0].tolist(), short[0].tolist()])

    def test_keyed_and_wired(self):
        self.assertGreaterEqual(revisit.ROADSYS_SURFACE_VERSION, 8)
        self.assertGreaterEqual(fast.MIN_VERSION, 632)
        bw = (HERE / "blockworker.py").read_text(encoding="utf-8")
        self.assertIn("_roadsys.DECKS_FN = lambda frame: _bridges(None, frame, _elev_stage1)", bw)


class SkewTest(unittest.TestCase):
    """(627) The deck-end skew under BNE_ROADSYS=surface: the hand-over's skewed ends, the ground roads cut along them."""

    def test_the_hand_overs_ends_skew_by_the_rule(self):
        if getattr(fast._ef, "VERSION", 0) < 627:
            self.skipTest("earthfast < 627")
        b = (np.array([[0.3, 0.2], [40.3, 0.2]]), 4.0)
        gnd = (np.array([[-40.0, 0.2], [0.3, 0.2]]), 4.0)
        road = (np.array([[-4.7, -43.10127], [45.3, 43.50127]]), 3.5)
        e = fast._ef.RoadsysEnds([b], [gnd, road], [], 1.0, [], [None])
        sk = e.skews()
        self.assertEqual(len(sk), 1)
        x, y, dx, dy = sk[0]
        self.assertAlmostEqual(x, 0.3)
        self.assertAlmostEqual(dx, 0.5, places=4)
        self.assertAlmostEqual(dy, 0.8660254, places=4)
        self.assertEqual(fast._ef.RoadsysEnds([b], [gnd, road], [], 1.0).skews(), [])     # (no skew asked: square)
        # the inventory's record of a square structure: square
        self.assertEqual(fast._ef.RoadsysEnds([b], [gnd, road], [], 1.0, [], [(0.0, 40.0, 1.0, 0.0)]).skews(), [])

    def test_a_ground_road_ending_at_a_skewed_end_takes_its_line(self):
        import roads
        import roadmesh
        g = {"m": "asphalt", "w": 7.0, "n": [[-40.0, 0.0, 5.0], [0.0, 0.0, 5.0]]}
        far = {"m": "asphalt", "w": 7.0, "n": [[-40.0, 30.0, 5.0], [0.0, 30.0, 5.0]]}
        chunks = [dict(g), dict(far)]
        roads._rs_skew_ground_ends(chunks, [(0.0, 0.0, -0.5, -0.8660254037844386)])
        self.assertEqual(chunks[0].get("rs_sk1"), [0.5, 0.866025])       # (to the chunk's left there: +y)
        self.assertNotIn("rs_sk1", chunks[1])
        got = roadmesh.ribbon_pairs([chunks[0]])
        r = got[0][0] if isinstance(got[0], tuple) and len(got[0]) == 2 else got[0]
        le, ri = r[0], r[1]
        # the ribbon's end row along the line: left 3.5 m up at x = 3.5 cot 60, right at -3.5 cot 60
        self.assertAlmostEqual(float(le[-1][0]), 3.5 * 0.5 / 0.8660254037844386, places=3)
        self.assertAlmostEqual(float(ri[-1][0]), -3.5 * 0.5 / 0.8660254037844386, places=3)


if __name__ == "__main__":
    unittest.main()
