"""The roadnet kernels read Python values as the Python did when a value's type overrides its conversion (Codex on
bc34313f): a float subclass whose __float__ gives another value or raises, a str subclass whose __str__ gives another
text, a class value that only hashes and compares like "motorway"; and a column read where the Python read it, row by
row (a list subclass's __getitem__ asked alike, an element's __float__ changing a later row seen alike). Against the
references kept in the parity tests (the Python before each kernel)."""
import os
import random
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from test_roads import features, rules  # noqa: E402
import fast  # noqa: E402
import lanesections  # noqa: E402
import mapgeom  # noqa: E402
import roadlanes  # noqa: E402
import roads  # noqa: E402
import test_roadnet_lanesplit as tl  # noqa: E402
import test_roadnet_widthlanes as tw  # noqa: E402

LOG = []


class OverFloat(float):
    """A float whose float() is another value (5.0 for 3.0: the stored value read directly was 3.0)."""

    def __float__(self):
        LOG.append(("float", float.__repr__(self)))
        return float.__float__(self) + 2.0


class RaisingFloat(float):
    def __float__(self):
        raise ArithmeticError("no float")


class ValueErrorFloat(float):
    def __float__(self):
        raise ValueError("not a number")


class OtherStr(str):
    """A str whose str() is another class (str.__str__ overridden)."""

    def __str__(self):
        return {"trunk": "residential", "residential": "trunk", "road": "rail"}.get(str.__str__(self), "primary")


class LikeMotorway:
    """Hashes and compares as "motorway" (Python's `in` of a set finds it; it is no str)."""

    def __hash__(self):
        return hash("motorway")

    def __eq__(self, other):
        return other == "motorway"


class Recording(list):
    """A column whose rows are read through its own __getitem__ (recorded)."""

    def __getitem__(self, i):
        LOG.append(("get", i))
        return list.__getitem__(self, i)


class Mutating(float):
    """A count whose float() rewrites a later row of its column."""

    def __new__(cls, v, col, row, new):
        x = float.__new__(cls, v)
        x.col, x.row, x.new = col, row, new
        return x

    def __float__(self):
        self.col[self.row] = self.new
        return float.__float__(self)


def outcome(fn, *args):
    del LOG[:]
    try:
        got = ("ok", fn(*args))
    except Exception as e:      # noqa: BLE001 (the class is the point)
        got = ("raised", type(e))
    return got, list(LOG)


def same(a, b):
    if isinstance(a, tuple):
        return isinstance(b, tuple) and len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, np.ndarray):
        return isinstance(b, np.ndarray) and a.dtype == b.dtype and a.tobytes() == b.tobytes()
    return a == b


class OverrideTest(unittest.TestCase):
    def compare(self, ref, new, f, what):
        f.__dict__.pop("_lane_evidence", None)
        f.__dict__.pop("_ref_lane_evidence", None)
        f.__dict__.pop("_width_rejected", None)
        f.__dict__.pop("_ref_width_rejected", None)
        want = outcome(ref, f)
        got = outcome(new, f)
        self.assertEqual(got[0][0], want[0][0], (what, got, want))
        if want[0][0] == "ok":
            self.assertTrue(same(got[0][1], want[0][1]), (what, got[0][1], want[0][1]))
        else:
            self.assertIs(got[0][1], want[0][1], what)
        self.assertEqual(got[1], want[1], what)
        return want[0]

    def evidence(self, values, wrap=None):
        f = features([[(0, 10 * i), (50, 10 * i)] for i in range(len(values))], ["motorway"] * len(values))
        a = np.empty(len(values), object)
        a[:] = values
        f.attrs["lane_count"] = a if wrap is None else wrap(values)
        return f

    def test_lane_evidence_float_overrides(self):
        gen = lambda feats: [None] * len(feats)     # noqa: E731
        with mock.patch.object(roadlanes, "generated_counts", gen):
            got = self.compare(tw.ref_lane_evidence, roads._lane_evidence,
                               self.evidence([OverFloat(3.0), OverFloat(2.0), 4.0]), "override")
            self.assertEqual(got[1].tolist(), [5, 4, 4])            # (float() of them, not the stored 3 and 2)
            self.assertEqual(self.compare(tw.ref_lane_evidence, roads._lane_evidence,
                                          self.evidence([2.0, RaisingFloat(3.0)]), "raising"),
                             ("raised", ArithmeticError))
            got = self.compare(tw.ref_lane_evidence, roads._lane_evidence,
                               self.evidence([ValueErrorFloat(3.0), 2.0]), "not a number")
            self.assertEqual(got[1].tolist(), [-1, 2])

    def test_lane_evidence_reads_rows_in_place(self):
        gen = lambda feats: [None] * len(feats)     # noqa: E731
        with mock.patch.object(roadlanes, "generated_counts", gen):
            f = self.evidence(["2", "3", "4"], wrap=Recording)
            self.compare(tw.ref_lane_evidence, roads._lane_evidence, f, "recording")
            for kind in (list, np.array):
                vals = ["2", "3", "4", "5"]
                f = self.evidence(vals)
                col = kind(vals, object) if kind is np.array else list(vals)
                col[0] = Mutating(2.0, col, 2, "9")
                f.attrs["lane_count"] = col
                got = self.compare(tw.ref_lane_evidence, roads._lane_evidence, f, "mutating")
                # (the row rewritten by the first row's float() is read as rewritten: 9)
                self.assertEqual(got[1].tolist(), [2, 3, 9, 5])

    def test_direction_columns_are_three(self):
        for k in (0, 2, 4):
            with self.assertRaises(ValueError):
                fast._ef.lane_evidence(1, [None] * 4, [None], [None] * k, None)

    def test_width_overrides(self):
        rng = random.Random(1)
        rejected = 0
        for k in range(300):
            f = tw.width_layer(rng)
            n = len(f)
            c = np.empty(n, object)
            c[:] = [rng.choice([OtherStr("trunk"), OtherStr("residential"), "trunk", OtherStr("x")]) for _ in range(n)]
            f.attrs["class"] = c
            if rng.random() < 0.5:
                s = np.empty(n, object)
                s[:] = [rng.choice(["road", OtherStr("road")]) for _ in range(n)]
                f.attrs["subtype"] = s
            entries = [(i, rng.choice([OverFloat(28.5), 30.5, OverFloat(10.0)])) for i in range(n)]
            vals = np.empty(n, object)
            vals[:] = [e[1] for e in entries]
            f.rules["width"] = mapgeom.RuleTable(np.arange(n, dtype=np.int32), vals, np.zeros(n, np.float32),
                                                 np.ones(n, np.float32))
            ow = np.empty(n, object)
            ow[:] = [rng.choice([None, OverFloat(28.0), "30.5", ValueErrorFloat(40.0)]) for _ in range(n)]
            f.attrs["osm_width"] = ow
            got = self.compare(tw.ref_width_rejected, roads.width_rejected, f, k)
            if got[0] == "ok":
                rejected += int(got[1][0].sum() + got[1][1].sum())
        self.assertGreater(rejected, 50)
        f = tw.width_layer(random.Random(2))
        n = len(f)
        vals = np.empty(n, object)
        vals[:] = [RaisingFloat(30.0)] * n
        f.attrs["class"] = np.array(["trunk"] * n)
        f.attrs.pop("subtype", None)
        f.rules["width"] = mapgeom.RuleTable(np.arange(n, dtype=np.int32), vals, np.zeros(n, np.float32),
                                             np.ones(n, np.float32))
        self.assertEqual(self.compare(tw.ref_width_rejected, roads.width_rejected, f, "raising"),
                         ("raised", ArithmeticError))

    def test_osm_values_overrides(self):
        f = features([[(0, 10 * i), (50, 10 * i)] for i in range(5)], ["primary"] * 5)
        ow = np.empty(5, object)
        ow[:] = [OverFloat(10.0), ValueErrorFloat(3.0), 12.0, OtherStr("7"), None]
        f.attrs["osm_width"] = ow
        want = [roads._osm(f, "width", i, 2.0, 40.0) for i in range(5)]
        want = np.array([np.nan if v is None else v for v in want], np.float64)
        got = roads._osm_values(f, "width", 2.0, 40.0)
        self.assertEqual(got.tobytes(), want.tobytes())
        self.assertEqual(got[0], 12.0)
        ow[1] = RaisingFloat(3.0)
        with self.assertRaises(ArithmeticError):
            roads._osm(f, "width", 1, 2.0, 40.0)
        with self.assertRaises(ArithmeticError):
            roads._osm_values(f, "width", 2.0, 40.0)

    def test_highway_class_values(self):
        """lanesections' highway test (_col in roadlanes.HIGHWAYS) of class values that are no plain str."""
        rng = random.Random(3)
        mouth = lanesections._structure_mouth
        lanesections._structure_mouth = tl._structure_mouth
        try:
            for k in range(150):
                f = tl.random_layer(rng)
                n = len(f)
                c = np.empty(n, object)
                c[:] = [rng.choice([LikeMotorway(), OtherStr("motorway"), np.str_("trunk"), "motorway"])
                        if str(v) in ("motorway", "trunk") else str(v) for v in f.attrs["class"].tolist()]
                f.attrs["class"] = c
                want = outcome(tl.split, f)
                got = outcome(lanesections.split, f)
                self.assertEqual(got[0][0], want[0][0], (k, got, want))
                if want[0][0] == "ok":
                    tl.same_layer(got[0][1], want[0][1], f"case {k}")
                else:
                    self.assertIs(got[0][1], want[0][1], k)
            f = tl.random_layer(random.Random(4))
            c = np.empty(len(f), object)
            c[:] = [[1]] * len(f)              # (unhashable: Python's `in` of a set raised TypeError)
            f.attrs["class"] = c
            self.assertEqual(outcome(lanesections.split, f)[0][0], outcome(tl.split, f)[0][0])
        finally:
            lanesections._structure_mouth = mouth


class Flip(list):
    """Generated counts whose row reads alternate: a second read of a row gives another count, or raises."""

    def __init__(self, v, mode):
        list.__init__(self, v)
        self.mode, self.seen = mode, {}

    def __getitem__(self, i):
        k = self.seen[i] = self.seen.get(i, 0) + 1
        LOG.append(("gen", i, k))
        if k % 2 == 0:
            if self.mode == "raise":
                raise LookupError("second read")
            return 7
        return list.__getitem__(self, i)


class LazyRows:
    """A subtype column whose tolist() gives an iterable that yields `good` rows, then raises (or rewrites its rows
    through a row's __str__)."""

    def __init__(self, rows, good):
        self.rows, self.good = rows, good

    def tolist(self):
        rows, good = self.rows, self.good

        class It:
            def __iter__(self):
                for k, r in enumerate(rows):
                    if k >= good:
                        raise LookupError("past the rows numpy asked for")
                    LOG.append(("sub", k))
                    yield r
        return It()


class Rewriting(str):
    """A subtype whose str() rewrites the next row of its list (the list's later rows read after it)."""

    def __new__(cls, v, rows, k):
        x = str.__new__(cls, v)
        x.rows, x.k = rows, k
        return x

    def __str__(self):
        if self.k + 1 < len(self.rows):
            self.rows[self.k + 1] = "rail"
        return str.__str__(self)


class Kernel:
    """A road rules stand-in whose width_rejects answers with masks of other lengths."""

    def __init__(self, real, mode):
        self.real, self.mode = real, mode

    def width_rejects(self, cls, oneway, link, lanes, f, v, x):
        got = np.asarray(self.real.width_rejects(cls, oneway, link, lanes, f, v, x), bool)
        if np.all(np.asarray(lanes) == 1):      # (the first pass as it is: the second pass's masks altered)
            return got
        if self.mode == "long":
            return np.r_[got, True]
        if self.mode == "one":
            return got[:1] if len(got) else np.ones(1, bool)
        return got


class RoundTwoTest(unittest.TestCase):
    """Codex round 2 on ec3397ee."""

    def test_generated_counts_read_twice(self):
        for mode in ("value", "raise"):
            vals = [None, 3, 2, None]
            f = features([[(0, 10 * i), (50, 10 * i)] for i in range(4)], ["motorway"] * 4)
            f.attrs.pop("osm_lanes", None)
            for side, fn in (("ref", tw.ref_lane_evidence), ("new", roads._lane_evidence)):
                gen = Flip(vals, mode)
                with mock.patch.object(roadlanes, "generated_counts", lambda feats: gen):
                    f.__dict__.pop("_lane_evidence", None)
                    f.__dict__.pop("_ref_lane_evidence", None)
                    if side == "ref":
                        want = outcome(fn, f)
                    else:
                        got = outcome(fn, f)
            self.assertEqual(got[0][0], want[0][0], mode)
            self.assertEqual(got[1], want[1], mode)
            if want[0][0] == "ok":
                self.assertEqual(got[0][1].tolist(), want[0][1].tolist())
                self.assertEqual(want[0][1].tolist(), [-1, 7, 7, -1])
            else:
                self.assertIs(got[0][1], want[0][1])

    def width_case(self, n, sub):
        f = features([[(0, 10 * i), (50, 10 * i)] for i in range(n)], ["trunk"] * n)
        f.attrs["subtype"] = sub
        f.rules["width"] = rules([(i, 30.5) for i in range(n)], "float")
        return f

    def compare(self, f, patches=()):
        out = {}
        for side, fn in (("ref", tw.ref_width_rejected), ("new", roads.width_rejected)):
            f.__dict__.pop("_width_rejected", None)
            f.__dict__.pop("_ref_width_rejected", None)
            f.__dict__.pop("_lane_evidence", None)
            with mock.patch.object(roadlanes, "generated_counts", lambda feats: [2] * len(feats)):
                for p in patches:
                    p.start()
                try:
                    out[side] = outcome(fn, f)
                finally:
                    for p in patches:
                        p.stop()
        want, got = out["ref"], out["new"]
        self.assertEqual(got[0][0], want[0][0], (got, want))
        self.assertEqual(got[1], want[1])
        if want[0][0] == "ok":
            self.assertTrue(same(got[0][1], want[0][1]), (got[0][1], want[0][1]))
        else:
            self.assertIs(got[0][1], want[0][1])
        return want[0]

    def test_subtype_rows_taken_as_numpy_asks(self):
        # (n good rows then an error past them: numpy's count stops before it; fewer good rows: the error)
        self.assertEqual(self.compare(self.width_case(3, LazyRows(["road"] * 5, 3)))[0], "ok")
        self.assertEqual(self.compare(self.width_case(3, LazyRows(["road"] * 5, 2))), ("raised", LookupError))
        rows = ["road", "road", "road"]
        rows[0] = Rewriting("road", rows, 0)

        class Column:
            def tolist(self):
                return rows
        got = self.compare(self.width_case(3, Column()))
        self.assertEqual(got[0], "ok")

    def test_rule_masks_of_other_lengths(self):
        real = roads._road_rules()
        for mode in ("long", "one", "same"):
            f = self.width_case(2, None)
            f.attrs.pop("subtype")
            f.rules["width"] = rules([(0, 30.5), (1, 31.0)], "float")
            got = self.compare(f, [mock.patch.object(roads, "_road_rules", lambda m=mode: Kernel(real, m))])
            self.assertEqual(got[0], "raised" if mode == "long" else "ok", mode)

    def test_surface_kind_of_class_objects(self):
        import test_roadnet_surfaces as ts

        class Raising(str):
            def __str__(self):
                raise ArithmeticError("str")
        rng = random.Random(6)
        for k in range(400):
            cls = rng.choice([OtherStr("track"), OtherStr("residential"), Raising("track"), LikeMotorway(), None, 3,
                              "track", np.str_("track"), [1]])
            surf = rng.choice([None, "unpaved", "asphalt", "gravel", "ground", "x"])
            want = outcome(ts.ref_surface_kind, cls, surf)[0]
            got = outcome(roads.surface_kind, cls, surf)[0]
            self.assertEqual(got, want, (k, cls, surf))


if __name__ == "__main__":
    unittest.main()
