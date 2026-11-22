"""BNE_ROADSYS (the road surface + bridge rewrite, M1 shadow; server/roadsys.py, native/roadsys): off unless set, never a
key switch while shadow changes nothing the player sees, and the kernels' product a function of its declared inputs
only: the same bytes whatever the input order, the copies or the lane-section duplicates in the read; tiles sharing a
side make the same seam; a piece's key moves with an input inside its read list and only then."""
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
            self.assertIsNone(roadsys.enqueue(0, 0, object(), object(), "/nonexistent"))
            self.assertIsNone(roadsys._q, "no thread when off")
        with mock.patch.dict(os.environ, {"BNE_ROADSYS": "shadow"}):
            importlib.reload(roadsys)
            self.assertTrue(roadsys.SHADOW)
        importlib.reload(roadsys)

    def test_not_a_key_switch(self):
        self.assertIn("BNE_ROADSYS", revisit.NOT_SWITCHES)
        self.assertFalse(revisit.keyed_env("BNE_ROADSYS"))
        revisit._switches.clear()
        base = revisit.switches_key()
        with mock.patch.dict(os.environ, {"BNE_ROADSYS": "shadow"}):
            revisit._switches.clear()
            self.assertEqual(revisit.switches_key(), base)
        revisit._switches.clear()

    def test_not_on_the_settings_page(self):
        import experimental
        self.assertFalse(any("BNE_ROADSYS" in env for _sid, env, _n in experimental.FEATURES))

    def test_the_tile_path_only_reads_the_flag(self):
        src = (HERE / "blockworker.py").read_text(encoding="utf-8")
        i = src.index("roadsys.enqueue(")
        self.assertIn("if roadsys.SHADOW:", src[i - 200:i])


if __name__ == "__main__":
    unittest.main()
