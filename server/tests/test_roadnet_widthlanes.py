"""A road layer's lane evidence and its mapped widths' repair (earthfast 404, roadnet::widthlanes: the road revamp's
R2a) against roads._lane_evidence / width_rejected's Python, value for value: random layers of lane columns of every
kind (numbers, text with spaces, lists, conditional and fractional counts, numpy scalars, None, lists as columns), the
direction columns, lane parts (lists / tuples of 4-sequences with tag dicts, empty or not, of other lengths, dict
subclasses, strings), generated counts; classes of STYLE and others, subtypes, oneway and link rules, width rules of
float32 or object values, OSM widths. The home copies and each layer's lane evidence are recording stand-ins in the
width test: asked alike (which roads, in which order, which layers), the same masks out; short columns and values
float() refuses raise alike.

The Python versions are kept here only as the reference (the server has no Python twin: fast.require)."""
import os
import random
import sys
import types
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from test_roads import features, rules  # noqa: E402
import fast  # noqa: E402
import lanesections  # noqa: E402
import mapgeom  # noqa: E402
import railtags  # noqa: E402
import roadlanes  # noqa: E402
import roads  # noqa: E402


# ---- the Python versions (before earthfast 404), verbatim (their memos under other names) ----

def ref_lane_evidence(feats):
    got = feats.__dict__.get("_ref_lane_evidence")
    if got is not None:
        return got
    import railtags
    import roadlanes
    n = len(feats)
    a = feats.attrs
    ev = np.full(n, -1, np.int64)

    def take(i, v):
        c = roadlanes.count(v)
        if c is not None and c > ev[i]:
            ev[i] = c
    import lanesections
    cols = [a.get(k) for k in ("osm_lanes", railtags.lane_attr("lanes"), "lane_count", lanesections.LANE_MAX)]
    dirs = [a.get(railtags.lane_attr(k)) for k in ("lanes:forward", "lanes:backward", "lanes:both_ways")]
    gen = roadlanes.generated_counts(feats)
    parts = a.get(railtags.LANE_PARTS)
    for i in range(n):
        for c in cols:
            if c is not None:
                take(i, c[i])
        if gen[i] is not None:
            take(i, gen[i])
        tot = [roadlanes.count(d[i], 0) if d is not None else None for d in dirs]
        if any(t for t in tot):
            take(i, sum(t or 0 for t in tot))
        ps = parts[i] if parts is not None else None
        if isinstance(ps, (list, tuple)):
            for p in ps:
                tags = p[3] if isinstance(p, (list, tuple)) and len(p) == 4 and isinstance(p[3], dict) else None
                if tags:
                    take(i, tags.get("lanes"))
                    t = [roadlanes.count(tags.get(k), 0) for k in ("lanes:forward", "lanes:backward",
                                                                   "lanes:both_ways")]
                    if any(x for x in t):
                        take(i, sum(x or 0 for x in t))
    feats.__dict__["_ref_lane_evidence"] = ev
    return ev


def ref_width_rejected(feats):
    STYLE, WIDTH_REPAIR_ON, WIDTH_PLATFORM_X = roads.STYLE, roads.WIDTH_REPAIR_ON, roads.WIDTH_PLATFORM_X
    _rule_cols, _road_rules, _osm_values = roads._rule_cols, roads._road_rules, roads._osm_values
    _lane_evidence, home_copy = roads._lane_evidence, roads.home_copy
    got = feats.__dict__.get("_ref_width_rejected")
    if got is not None:
        return got
    n = len(feats)
    wt = feats.rules.get("width")
    nw = len(wt.feature) if wt is not None else 0
    out = (np.zeros(nw, bool), np.zeros(n, bool))
    ow = feats.attrs.get("osm_width")
    if not n or (not nw and ow is None) or not WIDTH_REPAIR_ON:
        feats.__dict__["_ref_width_rejected"] = out
        return out
    import fast
    import roadlanes
    fast.require(376, "RoadRules.width_rejects")
    pos = {c: k for k, c in enumerate(STYLE)}
    a = feats.attrs
    cls_s = [str(v) for v in a["class"].tolist()] if "class" in a else ["unknown"] * n
    sub = a.get("subtype")
    cls = np.fromiter((pos.get(c, -1) if sub is None or str(s) == "road" else -1
                       for c, s in zip(cls_s, sub.tolist() if sub is not None else cls_s)), np.int32, n)
    if not (cls >= 0).any():
        feats.__dict__["_ref_width_rejected"] = out
        return out
    oneway, _ = _rule_cols(feats.rules.get("oneway"), n)
    link = np.asarray(roadlanes.links(feats), bool)
    rr = _road_rules()
    srcs = []                   # (feature per value, values)
    if nw:
        wv = np.asarray(wt.value)
        wv = (wv.astype(np.float64) if wv.dtype.kind in "fiub" else
              np.array([float(x) for x in wv.tolist()], np.float64))
        srcs.append((np.ascontiguousarray(wt.feature, np.int64), np.ascontiguousarray(wv)))
    if ow is not None:
        srcs.append((np.arange(n, dtype=np.int64), np.ascontiguousarray(_osm_values(feats, "width", None, None))))
    one = np.ones(n, np.int64)
    maybe = np.zeros(n, bool)
    for f_, v_ in srcs:
        hit = np.asarray(rr.width_rejects(cls, oneway, link, one, f_, v_, WIDTH_PLATFORM_X), bool)
        maybe[f_[hit]] = True
    if not maybe.any():
        feats.__dict__["_ref_width_rejected"] = out
        return out
    lanes = np.full(n, -1, np.int64)
    own = _lane_evidence(feats)
    for i in np.flatnonzero(maybe).tolist():
        h = home_copy(feats, i)
        lanes[i] = int(_lane_evidence(h[0])[h[1]]) if h is not None else int(own[i])
    guess = np.fromiter((c == "trunk" for c in cls_s), bool, n) & ~link
    maybe &= (lanes > 0) | guess
    maybe &= ~oneway.astype(bool)
    if not maybe.any():
        feats.__dict__["_ref_width_rejected"] = out
        return out
    res = []
    for f_, v_ in srcs:
        bad = np.asarray(rr.width_rejects(cls, oneway, link, lanes, f_, v_, WIDTH_PLATFORM_X), bool)
        res.append(bad & maybe[f_])
    k = 0
    rule_bad = res[k] if nw else out[0]
    k += 1 if nw else 0
    osm_bad = res[k] if ow is not None else out[1]
    out = (rule_bad, osm_bad)
    feats.__dict__["_ref_width_rejected"] = out
    return out


# ---- random layers ----

COUNTS = ["2", " 3 ", "2;3", "1_0", "x", "inf", "nan", None, 2, 3.0, 2.5, 0, -1, 12, 13, 12.0, True, np.float32(4),
          np.int64(6), np.str_("5"), "", "4.0", 1e300]
DIRS = ["1", "0", "2", None, 0, 1, 3, "x", np.str_("1"), 2.0, -2]


def column(rng, n, values, bad):
    kind = rng.choice(["obj", "obj", "float", "str", "list", "int"])
    m = n - (rng.randint(1, 2) if bad and rng.random() < 0.3 and n > 1 else 0)
    if kind == "float":
        return np.array([rng.choice([np.nan, 2.0, 3.0, 2.5, 13.0, 0.0]) for _ in range(m)])
    if kind == "int":
        return np.array([rng.choice([-1, 2, 4, 0, 14]) for _ in range(m)], np.int64)
    if kind == "str":
        return np.array([str(rng.choice(values)) for _ in range(m)])
    v = [rng.choice(values) for _ in range(m)]
    if kind == "list":
        return v
    a = np.empty(m, object)
    a[:] = v
    return a


class TagDict(dict):
    pass


def part_tags(rng):
    t = dict()
    for k in rng.sample(["lanes", "lanes:forward", "lanes:backward", "lanes:both_ways", "turn:lanes"], rng.randint(0, 4)):
        t[k] = rng.choice(COUNTS if k == "lanes" else DIRS)
    return t


def parts_value(rng):
    r = rng.random()
    if r < 0.3:
        return None
    if r < 0.35:
        return rng.choice(["abc", {"lanes": "3"}, 5])
    ps = []
    for _ in range(rng.randint(0, 3)):
        q = rng.random()
        if q < 0.7:
            p = (rng.random(), rng.random(), "w", part_tags(rng))
            ps.append(p if rng.random() < 0.5 else list(p))
        elif q < 0.8:
            ps.append((0.0, 1.0, part_tags(rng)))
        elif q < 0.9:
            ps.append((0.0, 1.0, "w", "lanes=3"))
        else:
            ps.append(None)
    return ps if rng.random() < 0.7 else tuple(ps)


def evidence_layer(rng, bad=False):
    n = rng.randint(0, 8)
    f = features([[(0, 10 * i), (50, 10 * i)] for i in range(n)], ["motorway"] * n)
    f.attrs.pop("osm_lanes", None)
    for k in ("osm_lanes", railtags.lane_attr("lanes"), "lane_count", lanesections.LANE_MAX):
        if rng.random() < 0.6:
            f.attrs[k] = column(rng, n, COUNTS, bad)
    for k in ("lanes:forward", "lanes:backward", "lanes:both_ways"):
        if rng.random() < 0.5:
            f.attrs[railtags.lane_attr(k)] = column(rng, n, DIRS, bad)
    if rng.random() < 0.6:
        pa = np.empty(n, object)
        for i in range(n):
            pa[i] = parts_value(rng)
        f.attrs[railtags.LANE_PARTS] = pa if rng.random() < 0.8 else list(pa)
    gen = [rng.choice([None, None, 2, 3, 0, 13]) for _ in range(n - (1 if bad and n and rng.random() < 0.2 else 0))]
    return f, gen


def outcome(fn, *args):
    try:
        return "ok", fn(*args)
    except Exception as e:      # noqa: BLE001 (the class is the point)
        return "raised", type(e)


def same(a, b):
    if isinstance(a, tuple):
        return isinstance(b, tuple) and len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return (isinstance(a, np.ndarray) and isinstance(b, np.ndarray) and a.dtype == b.dtype and a.shape == b.shape
            and a.tobytes() == b.tobytes())


class LaneEvidenceParityTest(unittest.TestCase):
    def check(self, seed, count, bad):
        rng = random.Random(seed)
        tally = {"ok": 0, "raised": 0, "counted": 0}
        for k in range(count):
            f, gen = evidence_layer(rng, bad)
            calls = {"ref": 0, "new": 0}
            for side, fn in (("ref", ref_lane_evidence), ("new", roads._lane_evidence)):
                def generated(feats, side=side):
                    calls[side] += 1
                    return gen
                with mock.patch.object(roadlanes, "generated_counts", generated):
                    if side == "ref":
                        want = outcome(fn, f)
                    else:
                        got = outcome(fn, f)
            self.assertEqual(got[0], want[0], (k, got, want))
            self.assertEqual(calls["new"], calls["ref"], k)
            if want[0] == "ok":
                self.assertTrue(same(got[1], want[1]), (k, got[1], want[1]))
                self.assertIs(f.__dict__.get("_lane_evidence"), got[1])
                tally["counted"] += int((want[1] > 0).sum())
            else:
                self.assertIs(got[1], want[1], k)
            tally[want[0]] += 1
        return tally

    def test_random_layers(self):
        t = self.check(404, 4000, False)
        self.assertGreater(t["counted"], 4000)
        self.assertGreater(t["ok"], 3000)

    def test_short_columns_raise_alike(self):
        t = self.check(4041, 3000, True)
        self.assertGreater(t["raised"], 500)

    def test_huge_numbers_and_other_errors(self):
        for v in (10 ** 400, -(10 ** 400)):
            f = features([[(0, 0), (50, 0)]], ["motorway"])
            a = np.empty(1, object)
            a[0] = v
            f.attrs["lane_count"] = a
            want = outcome(ref_lane_evidence, f)
            got = outcome(roads._lane_evidence, f)
            self.assertEqual(got, want)
            self.assertEqual(want, ("raised", OverflowError))


CLASSES = ["trunk", "trunk", "motorway", "primary", "residential", "service", "unknown", "path", "trunk_link"]
WIDTHS = [5.0, 9.0, 12.0, 18.0, 25.0, 30.5, 45.0, np.nan, np.inf, 60.0]


def width_layer(rng, bad=False):
    n = rng.randint(1, 8)
    f = features([[(0, 10 * i), (50, 10 * i)] for i in range(n)], [rng.choice(CLASSES) for _ in range(n)])
    f.ids = np.array([f"w{rng.randint(0, 3)}{i}" for i in range(n)])
    r = rng.random()
    if r < 0.15:
        del f.attrs["class"]
    elif r < 0.3:
        c = np.empty(n, object)
        c[:] = [rng.choice(CLASSES + [None, 3]) for _ in range(n)]
        f.attrs["class"] = c
    elif bad and r < 0.4:
        f.attrs["class"] = f.attrs["class"][:-1] if n > 1 else None
    if rng.random() < 0.4:
        m = n - (1 if bad and rng.random() < 0.3 and n > 1 else 0)
        f.attrs["subtype"] = np.array([rng.choice(["road", "road", "rail", None, "water"]) for _ in range(m)], object)
    if rng.random() < 0.5:
        f.rules["oneway"] = rules([(i, rng.choice([1.0, -1.0])) for i in range(n) if rng.random() < 0.3], "float")
    if rng.random() < 0.4:
        f.rules["subclass"] = rules([(i, "link") for i in range(n) if rng.random() < 0.3])
    if rng.random() < 0.7:
        entries = [(rng.randrange(n + (2 if bad else 0)), rng.choice(WIDTHS)) for _ in range(rng.randint(0, 2 * n))]
        if rng.random() < 0.7:
            f.rules["width"] = rules(entries, "float")
        else:
            vals = np.empty(len(entries), object)
            vals[:] = [rng.choice([str(w), w, f"{w} m" if bad else str(w)]) for _, w in entries]
            f.rules["width"] = mapgeom.RuleTable(np.array([e[0] for e in entries], np.int32), vals,
                                                 np.zeros(len(entries), np.float32), np.ones(len(entries), np.float32))
    if rng.random() < 0.6:
        f.attrs["osm_width"] = np.array([rng.choice([None, "30.5", "12", 45, "x", "25 m"]) for _ in range(n)], object)
    return f


class WidthRejectedParityTest(unittest.TestCase):
    def check(self, seed, count, bad):
        rng = random.Random(seed)
        tally = {"ok": 0, "raised": 0, "rejected": 0, "homes": 0}
        for k in range(count):
            f = width_layer(rng, bad)
            n = len(f)
            others = [features([[(0, 0), (9, 9)]] * 6, ["trunk"] * 6) for _ in range(2)]
            evid = {id(f): np.array([rng.choice([-1, 1, 2, 3, 6]) for _ in range(n)], np.int64)}
            for o in others:
                evid[id(o)] = np.array([rng.choice([-1, 2, 4]) for _ in range(6)], np.int64)
            homes = {i: (rng.choice(others), rng.randrange(6)) if rng.random() < 0.4 else None for i in range(n)}
            logs = {}
            for side, fn in (("ref", ref_width_rejected), ("new", roads.width_rejected)):
                log = logs[side] = []

                def evidence(feats, log=log):
                    log.append(("evidence", id(feats)))
                    return evid[id(feats)]

                def home_copy(feats, i, log=log):
                    log.append(("home", id(feats), type(i), i))
                    return homes[i]
                f.__dict__.pop("_width_rejected", None)
                f.__dict__.pop("_ref_width_rejected", None)
                with mock.patch.object(roads, "_lane_evidence", evidence), \
                        mock.patch.object(roads, "home_copy", home_copy):
                    logs[side + "_out"] = outcome(fn, f)
            want, got = logs["ref_out"], logs["new_out"]
            self.assertEqual(got[0], want[0], (k, got, want))
            self.assertEqual(logs["new"], logs["ref"], k)
            if want[0] == "ok":
                self.assertTrue(same(got[1], want[1]), (k, got[1], want[1]))
                self.assertIs(f.__dict__.get("_width_rejected"), got[1])
                tally["rejected"] += int(want[1][0].sum() + want[1][1].sum())
                tally["homes"] += sum(1 for e in logs["ref"] if e[0] == "home")
            else:
                self.assertIs(got[1], want[1], k)
            tally[want[0]] += 1
        return tally

    def test_random_layers(self):
        t = self.check(4042, 4000, False)
        self.assertEqual(t["raised"], 0)
        self.assertGreater(t["rejected"], 1000)
        self.assertGreater(t["homes"], 1000)

    def test_malformed_layers_raise_alike(self):
        t = self.check(4043, 3000, True)
        self.assertGreater(t["raised"], 300)
        self.assertGreater(t["ok"], 1000)

    def test_repair_off(self):
        f = width_layer(random.Random(5))
        with mock.patch.object(roads, "WIDTH_REPAIR_ON", False):
            got = roads.width_rejected(f)
            want = ref_width_rejected(f)
        self.assertTrue(same(got, want))
        self.assertFalse(got[0].any() or got[1].any())

    def test_real_evidence_and_home_copies(self):
        """The real _lane_evidence and home_copy (layers without cells: no neighbours read)."""
        rng = random.Random(4044)
        for k in range(800):
            f = width_layer(rng)
            f.__dict__.pop("_width_rejected", None)
            want = outcome(ref_width_rejected, f)
            f.__dict__.pop("_lane_evidence", None)
            got = outcome(roads.width_rejected, f)
            self.assertEqual(got[0], want[0], k)
            if want[0] == "ok":
                self.assertTrue(same(got[1], want[1]), k)


class ContractTest(unittest.TestCase):
    """Inputs outside the contract (roadnet::pyobj::Contract): TypeError before a row is read."""

    def test_refused(self):
        gen = lambda feats: [None] * len(feats)     # noqa: E731
        bad_parts = [[(0.0, 1.0, "w", TagDict(lanes="3"))], [[0.0, 1.0, "w", {3: "3"}]], [(0.0, 1.0, "w", {"lanes": [3]})],
                     (x for x in ()), [object()]]
        with mock.patch.object(roadlanes, "generated_counts", gen):
            for bad in bad_parts:
                f = features([[(0, 0), (50, 0)]], ["motorway"])
                pa = np.empty(1, object)
                pa[0] = bad
                f.attrs[railtags.LANE_PARTS] = pa
                self.assertEqual(outcome(roads._lane_evidence, f), ("raised", TypeError), bad)
            for col in (np.array([[2.0]]), np.array(["2"], dtype="datetime64[D]") if False else np.zeros(1, complex),
                        np.ma.array([2.0]), {0: 2}, "2", 2.0):
                f = features([[(0, 0), (50, 0)]], ["motorway"])
                f.attrs["lane_count"] = col
                self.assertEqual(outcome(roads._lane_evidence, f), ("raised", TypeError), type(col))
            f = features([[(0, 0), (50, 0)]], ["motorway"])
            f.attrs["lane_count"] = np.array([2], np.int64)
            self.assertEqual(outcome(roads._lane_evidence, f)[0], "ok")
        with mock.patch.object(roadlanes, "generated_counts", lambda feats: (2 for _ in range(1))):
            f = features([[(0, 0), (50, 0)]], ["motorway"])
            self.assertEqual(outcome(roads._lane_evidence, f), ("raised", TypeError))


class VersionSkewTest(unittest.TestCase):
    """An extension of 403 has neither lane_evidence nor width_rejected: refused at the start (fast.MIN_VERSION) and by
    each facade (fast.require: NativeMissing)."""

    def test_a_403_extension_is_refused(self):
        self.assertGreaterEqual(fast.MIN_VERSION, 404)
        self.assertGreaterEqual(fast._ef.VERSION, fast.MIN_VERSION)
        f = features([[(0, 0), (50, 0)]], ["trunk"])
        f.rules["width"] = rules([(0, 30.5)], "float")
        with mock.patch.object(fast, "_ef", types.SimpleNamespace(VERSION=403)), \
                mock.patch.object(roadlanes, "generated_counts", lambda feats: [None]):
            with self.assertRaises(fast.NativeMissing):
                roads._lane_evidence(f)
            with self.assertRaises(fast.NativeMissing):
                roads.width_rejected(f)


if __name__ == "__main__":
    unittest.main()
