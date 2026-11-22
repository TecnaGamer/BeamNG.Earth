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
        self.assertFalse(any(json.loads(fast._ef.roadsys_audit(blob)).values()), "an empty scene: nothing counted")
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

    def test_not_on_the_settings_page(self):
        import experimental
        self.assertFalse(any("BNE_ROADSYS" in env for _sid, env, _n in experimental.FEATURES))

    def test_the_tile_path_only_reads_the_flag(self):
        src = (HERE / "blockworker.py").read_text(encoding="utf-8")
        i = src.index("roadsys.enqueue(")
        self.assertIn("if roadsys.SHADOW:", src[i - 200:i])


if __name__ == "__main__":
    unittest.main()
