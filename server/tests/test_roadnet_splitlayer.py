"""lanesections.split's new layer (earthfast 403, roadnet::splitlayer: the road revamp's R2a) against the Python
assembly it replaced, layer for layer: every attrs column (dtype, shape, values; the sections' profiles object for
object; R1j's LANE_MAX), the lane tag columns as objects, the geometry bit for bit, every rule table (the cut points'
connector ids merged after each job's own rows) and the memos kept. The jobs are lane_split_jobs' own over the random
merged motorways of test_roadnet_lanesplit, and random jobs over random layers: attrs of every dtype (float32, int,
bool, text, objects, 2-D, lists), lane columns already there as text or floats or missing, src_record / osm_lanes
missing, osm_lanes of objects (None, text), rule tables of text, numbers and objects, empty or absent, connectors with
and without rows, cut points at 0 / 1 / in between / NaN, ids of numpy text, own / clipped arrays or None. Malformed
jobs (short tuples, odd lines, features past the layer, a count numpy cannot store, tags that are no dict): the same
exception class.

The Python version is kept here only as the reference (the server has no Python twin: fast.require)."""
import os
import random
import sys
import types
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import lanesections  # noqa: E402
import mapgeom  # noqa: E402
import railtags  # noqa: E402
import roads  # noqa: E402
from lanesections import LANE_MAX, PROFILE  # noqa: E402
import test_roadnet_lanesplit as tl  # noqa: E402

CALLS = []


# ---- the Python version (lanesections.split's assembly before earthfast 403), verbatim ----

def ref_new_layer(f, jobs):
    import roads
    old = np.array([r[1] for r in jobs], np.int64)
    attrs = {k: np.asarray(v)[old].copy() for k, v in f.attrs.items()}
    attrs[PROFILE] = np.full(len(jobs), None, object)
    # (each section, and every road kept whole, with its road's most lanes before the split clears its lane parts)
    attrs[LANE_MAX] = np.ascontiguousarray(roads._lane_evidence(f)[old], np.int64)
    # Object columns permit new tag strings longer than the dominant source's string.
    for key in railtags.LANE_COLUMNS:
        attrs[railtags.lane_attr(key)] = np.asarray(attrs.get(railtags.lane_attr(key), [None] * len(jobs)), object)
    attrs["src_record"] = np.asarray(attrs.get("src_record", [""] * len(jobs)), object)
    attrs["osm_lanes"] = np.asarray(attrs.get("osm_lanes", [np.nan] * len(jobs)), np.float64)
    attrs[railtags.LANE_PARTS] = np.asarray(attrs.get(railtags.LANE_PARTS, [None] * len(jobs)), object)
    # the new layer's geometry and rule rows gathered natively (lanesections.rs, kernel 235): each job a copy of its
    # feature, or of its section's (or moved mouth's) line, and the rows over it
    fast.require(235, "lane_split_coords")
    moved = np.array([r[4] is not None for r in jobs], bool)
    lines = [np.asarray(r[4], np.float64).reshape(-1, 2) for r in jobs if r[4] is not None]
    p_off = np.r_[0, np.cumsum([len(r[4]) if r[4] is not None else 0 for r in jobs])].astype(np.int64)
    po, ro, co, coords = fast._ef.lane_split_coords(
        np.ascontiguousarray(f.part_off, np.int64), np.ascontiguousarray(f.ring_off, np.int64),
        np.ascontiguousarray(f.coord_off, np.int64), np.ascontiguousarray(f.lonlat, np.float64).reshape(-1, 2), old,
        moved, np.ascontiguousarray(np.concatenate(lines) if lines else np.zeros((0, 2)), np.float64), p_off)
    if not len(coords):
        coords = np.zeros(0, np.float64)
    ja = np.array([r[2] for r in jobs], np.float64)
    jb = np.array([r[3] for r in jobs], np.float64)
    extra = []                                      # (job, connector id, at) of the sections' cut points
    for z, (_fid, i, a, b, p, tags, n, prof) in enumerate(jobs):
        if prof is None:
            continue
        for at, value in ((a, 0.), (b, 1.)):
            if 0. < at < 1.:
                extra.append((z, f"lane:{f.ids[i]}:{round(at * 1e9)}", value))
        attrs[PROFILE][z] = prof
        attrs["osm_lanes"][z] = n
        # Preserve the section's tags, rather than repeating the dominant way's turn arrows.
        for key in railtags.LANE_COLUMNS:
            attrs[railtags.lane_attr(key)][z] = tags.get(key)
        attrs[railtags.lane_attr("lanes")][z] = n
        if railtags.lane_attr("placement") in attrs:
            attrs[railtags.lane_attr("placement")][z] = None     # (in its line: prof["end_off"])
        attrs[railtags.LANE_PARTS][z] = None
    tables = {}
    for name in list(f.rules) + ([] if "connectors" in f.rules else ["connectors"]):
        table = f.rules.get(name)
        if table is not None and len(table):
            rows, zs, us, vs = fast._ef.lane_split_rules(
                np.ascontiguousarray(table.feature, np.int64), np.ascontiguousarray(table.start, np.float64),
                np.ascontiguousarray(table.end, np.float64), old, ja, jb)
            values = np.asarray(table.value)[rows]
        else:
            rows = zs = np.zeros(0, np.int64)
            us = vs = np.zeros(0)
            values = None
        if name == "connectors":
            if extra:           # (after each job's own rows, in job order)
                ez = np.array([e[0] for e in extra], np.int64)
                order = np.lexsort((np.r_[np.arange(len(zs)), np.arange(len(ez))],
                                    np.r_[np.zeros(len(zs), np.int64), np.ones(len(ez), np.int64)], np.r_[zs, ez]))
                vals = np.empty(len(zs) + len(ez), object)
                if len(zs):
                    vals[:len(zs)] = list(values)
                vals[len(zs):] = [e[1] for e in extra]
                zs = np.r_[zs, ez][order]
                us = np.r_[us, [e[2] for e in extra]][order]
                vs = np.r_[vs, [e[2] for e in extra]][order]
                values = vals[order]
            if len(zs):
                values = np.array(list(values), dtype=str)
        if not len(zs):
            tables[name] = f.rules[name] if name in f.rules and not len(f.rules[name]) else mapgeom.RuleTable(
                np.zeros(0, np.int32), np.zeros(0, str), np.zeros(0), np.zeros(0))
            continue
        tables[name] = mapgeom.RuleTable(zs.astype(np.int32), values, np.asarray(us, np.float64),
                                        np.asarray(vs, np.float64))
    out = mapgeom.Features(f.layer, np.array([r[0] for r in jobs]), f.kind[old], po, ro, co, coords,
                           own=None if f.own is None else f.own[old],
                           clipped=None if f.clipped is None else f.clipped[old], attrs=attrs, rules=tables)
    # Join versions are retained; geometry/specification memos intentionally are not.
    for key in ("_lanes_built", "_rails_built"):
        if key in f.__dict__:
            out.__dict__[key] = f.__dict__[key]
    return out


# ---- random layers and jobs ----

def column(rng, n):
    kind = rng.choice(["f64", "f32", "i64", "i32", "bool", "str", "obj", "2d", "list", "u8"])
    if kind == "f64":
        return np.array([rng.choice([1.5, np.nan, -0.0, 3.0]) for _ in range(n)])
    if kind == "f32":
        return np.array([rng.random() for _ in range(n)], np.float32)
    if kind in ("i64", "i32", "u8"):
        return np.array([rng.randint(0, 200) for _ in range(n)], {"i64": np.int64, "i32": np.int32, "u8": np.uint8}[kind])
    if kind == "bool":
        return np.array([rng.random() < 0.5 for _ in range(n)], bool)
    if kind == "str":
        return np.array([rng.choice(["", "a", "residential", "Rue de l'Église"]) for _ in range(n)])
    if kind == "2d":
        return np.array([[rng.random(), rng.random()] for _ in range(n)]).reshape(n, 2)
    if kind == "list":
        return [rng.choice([1, 2.5, "x", None]) for _ in range(n)]
    a = np.empty(n, object)
    for i in range(n):
        a[i] = rng.choice([None, "x", 3, 2.5, ("t", 1), {"k": 1}])
    return a


def lane_column(rng, n):
    c = rng.choice(["obj", "str", "f64", "none"])
    if c == "obj":
        a = np.empty(n, object)
        for i in range(n):
            a[i] = rng.choice([None, "2", "none|right", ""])
        return a
    if c == "str":
        return np.array([rng.choice(["2", "", "left|through"]) for _ in range(n)])
    if c == "f64":
        return np.array([rng.choice([2.0, np.nan]) for _ in range(n)])
    return None


def table(rng, n, kind):
    if rng.random() < 0.15:
        return None
    m = rng.choice([0, 1, 3, 6, 10]) if n else 0
    feat = [rng.randrange(n) for _ in range(m)]
    st = [rng.choice([0.0, 0.25, 0.5, rng.random(), 1.0]) for _ in range(m)]
    en = [s if rng.random() < 0.3 else min(1.0, s + rng.random()) for s in st]
    if kind == "text":
        val = np.array([rng.choice(["c1", "c22", "link", "x" * 30]) for _ in range(m)]) if m else np.zeros(0, str)
    elif kind == "float":
        val = np.array([rng.choice([1.0, -1.0, 2.5]) for _ in range(m)], np.float32)
    else:
        val = np.empty(m, object)
        for k in range(m):
            val[k] = rng.choice(["a", None, 5])
    return mapgeom.RuleTable(np.array(feat, np.int32), val, np.array(st, np.float32), np.array(en, np.float32))


def random_case(rng, bad=False):
    """A layer and its jobs: each feature whole, or cut into sections (moved lines or not)."""
    n = rng.randint(1, 6)
    lines = []
    for _ in range(n):
        k = rng.choice([2, 3, 5])
        x0 = rng.uniform(-80, 80)
        lines.append([(x0 + 1e-4 * j, 40 + 1e-4 * rng.random()) for j in range(k)])
    f = tl.highway(lines)
    f.ids = np.array([rng.choice(["08b2a", "w1", "x" * 20]) + str(i) for i in range(n)])
    for k in range(rng.randint(0, 4)):
        f.attrs[f"c{k}"] = column(rng, n)
    for key in rng.sample(list(railtags.LANE_COLUMNS), rng.randint(0, 5)):
        v = lane_column(rng, n)
        if v is not None:
            f.attrs[railtags.lane_attr(key)] = v
    if rng.random() < 0.5:
        f.attrs["src_record"] = np.array([rng.choice(["r1", "rec"]) for _ in range(n)])
    r = rng.random()
    if r < 0.3:
        f.attrs.pop("osm_lanes", None)
    elif r < 0.5:
        a = np.empty(n, object)
        for i in range(n):
            a[i] = rng.choice([None, "3", 2, 2.5])
        f.attrs["osm_lanes"] = a
    if rng.random() < 0.4:
        f.attrs.pop("class", None)
    if rng.random() < 0.5:
        pa = np.empty(n, object)
        for i in range(n):
            pa[i] = rng.choice([None, [(0.0, 1.0, "1", {"lanes": "2"})]])
        f.attrs[railtags.LANE_PARTS] = pa
    for name, kind in (("connectors", "text"), ("oneway", "float"), ("subclass", "text"), ("maxspeed", "obj")):
        if rng.random() < 0.7:
            t = table(rng, n, kind)
            if t is not None:
                f.rules[name] = t
    if rng.random() < 0.3:
        f.own = np.array([rng.random() < 0.5 for _ in range(n)])
    if rng.random() < 0.3:
        f.clipped = np.array([rng.random() < 0.5 for _ in range(n)])
    for key in ("_lanes_built", "_rails_built", "_road_specs"):
        if rng.random() < 0.3:
            f.__dict__[key] = rng.random()
    jobs = []
    for i in range(n):
        if rng.random() < 0.5:
            jobs.append((str(f.ids[i]), i, 0.0, 1.0, None, None, None, None))
            continue
        cuts = sorted(rng.sample([0.1, 0.25, 0.5, 0.6, 0.9, rng.random(), rng.random(), 0.3333333337], rng.randint(0, 3)))
        edges = [0.0] + cuts + [1.0]
        for j in range(len(edges) - 1):
            a, b = edges[j], edges[j + 1]
            if rng.random() < 0.05:
                a = rng.choice([float("nan"), -0.0, 1e-12])
            line = None
            if rng.random() < 0.6:
                line = np.array([(rng.uniform(-80, 80), 40.0) for _ in range(rng.choice([1, 2, 4]))])
            tags = {k: rng.choice(["2", "none|right", None]) for k in rng.sample(list(railtags.LANE_COLUMNS), 3)}
            nl = rng.choice([2, 3, 4, np.int64(3)])
            prof = {"offset": 0.0, "line": [[0.0, 0.0]], "k": rng.random()} if rng.random() < 0.9 else None
            fid = f"{f.ids[i]}~lanes{round(a * 1e9) if a == a else 0:09d}-{round(b * 1e9):09d}"
            jobs.append((fid, i, a, b, line, tags, nl, prof))
    if rng.random() < 0.3:
        jobs.sort(key=lambda r: r[0])
    if bad:
        z = rng.randrange(len(jobs))
        r = list(jobs[z])
        what = rng.choice(["short", "long", "odd line", "feature", "count", "tags", "at", "neg feature"])
        if what == "short":
            r = r[:7]
        elif what == "long":
            r = r + [None]
        elif what == "odd line":
            r[4] = np.zeros(3)
        elif what == "feature":
            r[1] = n + rng.randint(0, 3)
        elif what == "neg feature":
            r[1] = -1
        elif what == "count":
            r[6] = rng.choice(["x", None, "3", 1e400])
            r[7] = r[7] or {"k": 1}
        elif what == "tags":
            r[5] = None
            r[7] = r[7] or {"k": 1}
        elif what == "at":
            r[2] = rng.choice(["0.5", None])
            r[7] = r[7] or {"k": 1}
        jobs[z] = tuple(r)
    return f, jobs


def evidence(f):
    """roads._lane_evidence of the layer, recorded (the same calls: once, at the same point)."""
    CALLS.append("evidence")
    return np.array([(i * 7) % 5 - 1 for i in range(len(f))], np.int64)


class SplitLayerParityTest(unittest.TestCase):
    def outcome(self, fn, *args):
        """("ok", the result) or ("raised", the exception's class); a Rust panic (PanicException, a BaseException
        `except Exception` does not catch) fails the test."""
        del CALLS[:]
        try:
            got = "ok", fn(*args)
        except Exception as e:      # noqa: BLE001 (the class is the point)
            got = "raised", type(e)
        except BaseException as e:  # noqa: BLE001
            self.fail(f"{type(e).__name__}: {e}")
        return got, list(CALLS)

    def compare(self, f, jobs, what):
        with mock.patch.object(roads, "_lane_evidence", evidence):
            want, wcalls = self.outcome(ref_new_layer, f, jobs)
            got, gcalls = self.outcome(lanesections._new_layer, f, jobs)
        self.assertEqual(got[0], want[0], (what, got, want))
        self.assertEqual(gcalls, wcalls, what)
        if want[0] == "raised":
            self.assertIs(got[1], want[1], what)
            return False
        tl.same_layer(got[1], want[1], what)
        self.assertEqual({k: v for k, v in got[1].__dict__.items() if k.startswith("_")},
                         {k: v for k, v in want[1].__dict__.items() if k.startswith("_")}, what)
        for name in want[1].rules:
            self.assertIs(type(got[1].rules[name]), type(want[1].rules[name]), what)
            # (an empty table of the layer's kept as it was, the same object)
            self.assertEqual(got[1].rules[name] is f.rules.get(name), want[1].rules[name] is f.rules.get(name), what)
        return True

    def test_jobs_of_merged_motorways(self):
        """lane_split_jobs' own jobs over test_roadnet_lanesplit's random merged motorways and slip roads."""
        rng = random.Random(403)
        split = 0
        mouth = lanesections._structure_mouth
        lanesections._structure_mouth = tl._structure_mouth
        try:
            for k in range(300):
                f = tl.random_layer(rng)
                ctx = (lambda la, lo: f) if rng.random() < 0.5 else None
                jobs = fast._ef.lane_split_jobs(*lanesections._cols(f), f, ctx, lanesections._joint_specs,
                                                lambda i, end: lanesections._structure_mouth(f, i, end))
                if jobs is None:
                    continue
                split += 1
                for real in (False, True):
                    if real:            # (the real evidence too: no stand-in)
                        want = ref_new_layer(f, jobs)
                        got = lanesections._new_layer(f, jobs)
                        tl.same_layer(got, want, f"case {k} real")
                    else:
                        self.assertTrue(self.compare(f, jobs, f"case {k}"))
        finally:
            lanesections._structure_mouth = mouth
        self.assertGreater(split, 150)

    def test_random_jobs(self):
        rng = random.Random(4031)
        extras = 0
        for k in range(2500):
            f, jobs = random_case(rng)
            ok = self.compare(f, jobs, f"case {k}")
            if ok:
                extras += sum(1 for r in jobs if r[7] is not None for at in (r[2], r[3]) if 0 < at < 1)
        self.assertGreater(extras, 1000)

    def test_malformed_jobs_raise_alike(self):
        rng = random.Random(4032)
        raised = 0
        for k in range(2500):
            f, jobs = random_case(rng, bad=True)
            raised += not self.compare(f, jobs, f"case {k}")
        self.assertGreater(raised, 1000)

    def test_byte_strided_moved_lines(self):
        """A moved line of any layout (byte strides (24, 12), unaligned, reversed, a column slice): numpy's own
        concatenation of it, as the Python read it (Codex on a6b93359: the port read a (2, 2) line of byte strides (24,
        12) at other bytes)."""
        from test_roadnet_layout import odd
        rng = random.Random(4034)
        moved = 0
        views = (odd, lambda a: odd(a, 20, 3), lambda a: np.ascontiguousarray(a[::-1])[::-1],
                 lambda a: np.c_[a, a][:, ::2])
        for k in range(600):
            f, jobs = random_case(rng)
            out = []
            for r in jobs:
                if r[4] is not None and len(r[4]) and rng.random() < 0.8:
                    line = np.asarray(r[4], np.float64).reshape(-1, 2)
                    r = r[:4] + (rng.choice(views)(line),) + r[5:]
                    moved += 1
                out.append(r)
            self.compare(f, out, f"case {k}")
            want = ref_new_layer(f, out)
            got = lanesections._new_layer(f, [r[:4] + (None if r[4] is None else np.array(r[4]),) + r[5:]
                                              for r in out])
            self.assertEqual(got.lonlat.tobytes(), want.lonlat.tobytes(), k)
        self.assertGreater(moved, 300)

    def test_split_uses_it(self):
        """split hands its jobs to _new_layer and localizes what it gives (frame) or returns it."""
        rng = random.Random(4033)
        mouth = lanesections._structure_mouth
        lanesections._structure_mouth = tl._structure_mouth
        try:
            for _ in range(40):
                f = tl.random_layer(rng)
                got = lanesections.split(f)
                want = tl.split(f)
                tl.same_layer(got, want, "split")
        finally:
            lanesections._structure_mouth = mouth


class VersionSkewTest(unittest.TestCase):
    """An extension of 402 has no lane_split_layer: refused at the start (fast.MIN_VERSION) and by the facade
    (fast.require: NativeMissing), never an AttributeError at the first lane section."""

    def test_a_402_extension_is_refused(self):
        self.assertGreaterEqual(fast.MIN_VERSION, 403)
        self.assertGreaterEqual(fast._ef.VERSION, fast.MIN_VERSION)
        f, jobs = random_case(random.Random(1))
        with mock.patch.object(fast, "_ef", types.SimpleNamespace(VERSION=402)):
            with self.assertRaises(fast.NativeMissing):
                lanesections._new_layer(f, jobs)


if __name__ == "__main__":
    unittest.main()
