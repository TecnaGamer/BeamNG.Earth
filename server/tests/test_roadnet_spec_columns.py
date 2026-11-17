"""The spec inputs' per-feature columns (earthfast 379, roadnet::columns: the road revamp's R2a) against the Python they
replaced, bit for bit: roads._rule_cols, _osm_values, _lengths, cycle_streets and _spec_inputs' inline columns (class
codes, eligibility, rings, smoothness, name codes, sidewalks, connector counts) over random layers whose columns hold
what a decoded layer can: str / numpy str / object / float32 / int / bool arrays, None, NaN, "", padded and cased tags,
numbers as text ("3", " 3 ", "1_0", "inf", Arabic-Indic digits, bytes), rule tables with ties, NaN and zero spans,
features of 0, 1 and many points in several parts.

The Python versions are kept here only as the reference (the server has no Python twin: fast.require)."""
import os
import random
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dnp  # noqa: E402
import fast  # noqa: E402
import mapgeom  # noqa: E402
import roads  # noqa: E402
from roads import OSM_LANES, OSM_WIDTH_M, STYLE  # noqa: E402

SIDEWALK_TAG = {"both": (1, -1), "left": (1,), "right": (-1,), "no": (), "none": (), "separate": ()}
SIDES_OF = ((1, -1), (1,), (-1,), ())
GOOD_SMOOTHNESS = {"excellent", "good", "intermediate"}
RING_JUNCTIONS = ("roundabout", "circular")


# ---- the Python versions (before earthfast 379), verbatim ----

def ref_cycle_streets(feats):
    n = len(feats)
    cls, tags = feats.attrs.get("class"), feats.attrs.get("osm_sidewalk")
    out = np.zeros(n, bool)
    if cls is None or tags is None:
        return out
    for i, (c, v) in enumerate(zip(cls.tolist(), tags.tolist())):
        if str(c) == "cycleway" and SIDEWALK_TAG.get(str(v).strip().lower()):
            out[i] = True
    return out


def ref_lengths(feats, only=None):
    po, ro, co = feats.part_off, feats.ring_off, feats.coord_off
    idx = np.arange(len(feats)) if only is None else np.array([only])
    a = np.asarray(co[ro[po[idx]]], np.int64)
    b = np.asarray(co[ro[po[idx + 1]]], np.int64)
    out = np.full(len(idx), np.nan)
    ll = np.asarray(feats.lonlat, np.float64)
    cnt = b - a
    if not len(idx) or cnt.sum() < 2:
        return out
    m = 111320.0
    fid = np.repeat(np.arange(len(idx)), cnt)
    pos = np.concatenate([np.arange(x, y) for x, y in zip(a, b)]) if only is not None else np.arange(a[0], b[-1])
    pl = ll[pos]
    same = fid[1:] == fid[:-1]
    c = dnp.cos(np.radians(ll[np.minimum(a, len(ll) - 1), 1]))
    seg = dnp.hypot(np.diff(pl[:, 0]) * m * c[fid[:-1]], np.diff(pl[:, 1]) * m)
    L = np.bincount(fid[:-1][same], weights=seg[same], minlength=len(idx))
    ok = cnt >= 2
    out[ok] = L[ok]
    return out


def ref_rule_cols(table, n):
    has, val = np.zeros(n, bool), np.full(n, np.nan)
    if table is None or len(table.feature) == 0 or not n:
        return has, val
    maj = table.majors(n)
    has = maj >= 0
    v = np.asarray(table.value)[maj[has]]
    val[has] = v.astype(np.float64) if v.dtype.kind in "fiub" else [float(x) for x in v.tolist()]
    return has, val


def ref_osm_values(feats, name, lo, hi):
    n = len(feats)
    col = feats.attrs.get("osm_" + name)
    if col is None:
        return np.full(n, np.nan)
    col = np.asarray(col)
    if col.dtype.kind in "fiub":
        v = col[:n].astype(np.float64)
    else:
        def num(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return np.nan
        v = np.array([num(x) for x in col[:n].tolist()], np.float64)
    with np.errstate(invalid="ignore"):
        bad = ~np.isfinite(v)
        if lo is not None:
            bad |= v < lo
        if hi is not None:
            bad |= v > hi
    v[bad] = np.nan
    return v


def ref_columns(feats, unbuilt):
    """_spec_inputs' inline columns as it made them: elig_pre before the cycleway test (the ring radii read it), elig
    after it."""
    n = len(feats)
    a, rules = feats.attrs, feats.rules
    names = list(STYLE)
    pos = {c: k for k, c in enumerate(names)}
    cls_s = [str(v) for v in a["class"].tolist()] if "class" in a else ["unknown"] * n
    cls = np.fromiter((pos.get(c, -1) for c in cls_s), np.int32, n)
    sub = a.get("subtype")
    road = np.ones(n, bool) if sub is None else np.fromiter((str(v) == "road" for v in sub.tolist()), bool, n)
    elig = road & (cls >= 0) & ~np.asarray(unbuilt, bool)
    cr = rules.get("connectors")
    ncon = np.bincount(np.asarray(cr.feature), minlength=n)[:n].astype(np.int64) if cr is not None and \
        len(cr.feature) else None
    jc = a.get("osm_junction")
    ring = np.zeros(n, bool) if jc is None else np.fromiter((str(v) in RING_JUNCTIONS for v in jc.tolist()), bool, n)
    sm = a.get("osm_smoothness")
    good = np.zeros(n, bool) if sm is None else np.fromiter(
        (str(v).strip().lower() in GOOD_SMOOTHNESS for v in sm.tolist()), bool, n)
    nm_col = a.get("name")
    name = None
    if nm_col is not None:
        codes = {}
        name = np.full(n, -1, np.int64)
        for i, v in enumerate(nm_col.tolist()):
            nm = str(v or "").strip()
            if nm and nm != "None":
                name[i] = codes.setdefault(nm, len(codes))
    elig_pre = elig.copy()
    tags = a.get("osm_sidewalk")
    sides = np.full(n, -1, np.int8)
    if tags is not None:
        for i, v in enumerate(tags.tolist()):
            s = SIDEWALK_TAG.get(str(v).strip().lower())
            if s is not None:
                sides[i] = SIDES_OF.index(s)
    cyc = cls == pos.get("cycleway", -2)
    if cyc.any():
        elig &= ~cyc | ref_cycle_streets(feats)
    return {"cls": cls, "elig_pre": elig_pre, "elig": elig, "ring": ring, "good": good, "name": name, "sides": sides,
            "ncon": ncon}


# ---- random layers ----

TAGS = ["both", "left", "right", "no", "none", "separate", " Both ", "LEFT", "Right\t", "\x1cno\x1f", "　none",
        "yes", "", "None", "nan", "b0th", "separate;both", " left ", "lEfT", "K", "Keft"]
SMOOTH = ["excellent", "good", "intermediate", " Good", "GOOD\n", "bad", "", "İntermediate", "good;bad", "x"]
JUNCS = ["roundabout", "circular", "Roundabout", " roundabout", "jughandle", "", "None"]
NUMS = ["3", " 3 ", "3.0", "3.5", "1_0", "inf", "-inf", "nan", "NaN", "1e1", "12", "13", "0", "-1", "two", "",
        "٣", " ١٢ ", "2;3", "4 ", "1__0", "_1", "0x10", "+5", "5.", ".5"]
NAMES = ["Main Street", " Main Street ", "Main Street　", "", None, "None", " None ", "nan", "Broadway",
         "\x1cBroadway", "0", 0, 0.0, float("nan"), "Ötztaler Straße", "  "]
CLASSES = list(STYLE) + ["unknown", "footway", "Motorway", "", "cycleway", "cycleway"]


def _col(rng, vals, n, kind):
    v = [rng.choice(vals) for _ in range(n)]
    if kind == "object" or any(x is None or not isinstance(x, str) for x in v):
        return np.array(v + [None], object)[:n]
    if kind == "str":
        return np.array(v, str)
    return np.array(v, object)


def _num_col(rng, n):
    kind = rng.choice(["obj", "f32", "f64", "i64", "u8", "bool", "str"])
    if kind == "obj":
        vals = NUMS + [None, 3, 3.0, float("nan"), np.float32(2.5), np.int64(4), True, b"3", b"x", bytearray(b"5"),
                       10 ** 400, [1]]
        return np.array([rng.choice(vals) for _ in range(n)] + [None], object)[:n]
    if kind == "str":
        return np.array([rng.choice(NUMS) for _ in range(n)], str)
    if kind == "f32":
        return np.array([rng.choice([1.0, 2.5, 3.0, np.nan, np.inf, 12.0, 41.0, 1e-30, 0.1]) for _ in range(n)],
                        np.float32)
    if kind == "f64":
        return np.array([rng.choice([1.0, 2.5, 3.0, np.nan, -np.inf, 12.0, 40.0, 40.000000001, 2.0, 0.1])
                         for _ in range(n)])
    if kind == "i64":
        return np.array([rng.choice([0, 1, 3, 12, 13, 40, 200, 201, 2 ** 60 + 1]) for _ in range(n)], np.int64)
    if kind == "u8":
        return np.array([rng.randrange(0, 255) for _ in range(n)], np.uint8)
    return np.array([rng.random() < 0.5 for _ in range(n)], bool)


def _table(rng, n, kind="num"):
    m = rng.randint(0, 3 * n)
    feat = np.array([rng.randrange(0, n + 2) for _ in range(m)], np.int32)
    st = np.array([rng.choice([0.0, 0.25, 0.5, rng.random(), np.nan]) for _ in range(m)], np.float32)
    en = np.array([rng.choice([1.0, 0.75, 0.5, st[k] + 0.25, rng.random(), np.nan]) for k in range(m)], np.float32)
    if kind == "num":
        vk = rng.choice(["f64", "f32", "i32", "str", "obj"])
        if vk == "str":
            val = np.array([rng.choice(["1", "-1", "30", "2.5", " 7 ", "1e2"]) for _ in range(m)], str)
        elif vk == "obj":
            val = np.array([rng.choice(["1", -1, 30.0, np.float32(2.5), True]) for _ in range(m)] + [None],
                           object)[:m]
        elif vk == "f32":
            val = np.array([rng.choice([1.0, -1.0, 2.7, 0.1]) for _ in range(m)], np.float32)
        elif vk == "i32":
            val = np.array([rng.choice([1, -1, 50, 0]) for _ in range(m)], np.int32)
        else:
            val = np.array([rng.choice([1.0, -1.0, 13.3, 0.1]) for _ in range(m)])
    else:
        val = np.array([rng.choice(["link", "road", "parking_aisle"]) for _ in range(m)], str)
    return mapgeom.RuleTable(feat, val, st, en)


def random_layer(rng, n=None):
    n = rng.randint(0, 12) if n is None else n
    po, ro, co, ll = [0], [0], [0], []
    for _ in range(n):
        for _p in range(rng.choice([1, 1, 1, 2])):
            for _r in range(rng.choice([1, 1, 2])):
                k = rng.choice([0, 1, 2, 3, 7])
                lon, lat = rng.uniform(-179, 179), rng.uniform(-84, 84)
                for _ in range(k):
                    ll.append((lon, lat))
                    lon += rng.uniform(-1e-3, 1e-3)
                    lat += rng.choice([0.0, rng.uniform(-1e-3, 1e-3)])
                co.append(len(ll))
            ro.append(len(co) - 1)
        po.append(len(ro) - 1)
    attrs = {}
    kind = rng.choice(["str", "object"])
    if rng.random() < 0.9:
        attrs["class"] = _col(rng, CLASSES, n, kind)
    if rng.random() < 0.7:
        attrs["subtype"] = _col(rng, ["road", "road", "rail", "water", " road"], n, kind)
    for name, vals in (("osm_junction", JUNCS), ("osm_smoothness", SMOOTH), ("name", NAMES), ("osm_sidewalk", TAGS)):
        if rng.random() < 0.8:
            attrs[name] = _col(rng, vals, n, kind)
    for name in ("osm_lanes", "osm_width", "osm_maxspeed"):
        if rng.random() < 0.8:
            attrs[name] = _num_col(rng, n)
    rules = {}
    for name in ("width", "oneway", "max_speed_kmh"):
        if rng.random() < 0.8:
            rules[name] = _table(rng, n)
    if rng.random() < 0.8:
        rules["connectors"] = _table(rng, n, "str")
    ids = np.array([f"r{k:04d}" for k in range(n)])
    return mapgeom.Features("roads", ids, np.full(n, mapgeom.LINE, np.uint8), np.array(po, np.int64),
                            np.array(ro, np.int64), np.array(co, np.int64), np.array(ll, np.float64).reshape(-1, 2),
                            attrs=attrs, rules=rules)


def bits(a):
    a = np.asarray(a)
    return a.view(np.uint64) if a.dtype == np.float64 else a


class SpecColumnsParityTest(unittest.TestCase):
    def same(self, got, want, what):
        if want is None:
            self.assertIsNone(got, what)
            return
        got, want = np.asarray(got), np.asarray(want)
        self.assertEqual(got.dtype, want.dtype, what)
        self.assertEqual(got.shape, want.shape, what)
        self.assertTrue(np.array_equal(bits(got), bits(want)), f"{what}: {got} vs {want}")

    def raises_alike(self, fn_ref, fn_new, what):
        try:
            want = fn_ref()
        except Exception as e:      # (the same kind of error from both)
            with self.assertRaises(type(e), msg=what):
                fn_new()
            return None, None
        return want, fn_new()

    def test_random_layers(self):
        rng = random.Random(375)
        for k in range(1500):
            f = random_layer(rng)
            n = len(f)
            what = f"layer {k}"
            self.same(roads.cycle_streets(f), ref_cycle_streets(f), what + " cycle_streets")
            self.same(roads._lengths(f), ref_lengths(f), what + " lengths")
            for name, (lo, hi) in (("lanes", OSM_LANES), ("width", OSM_WIDTH_M), ("maxspeed", (5.0, 200.0)),
                                   ("width", (None, None))):
                want, got = self.raises_alike(lambda: ref_osm_values(f, name, lo, hi),
                                              lambda: roads._osm_values(f, name, lo, hi), what + " osm " + name)
                if want is not None:
                    self.same(got, want, what + " osm " + name)
            for name in ("width", "oneway", "max_speed_kmh"):
                t = f.rules.get(name)
                want, got = self.raises_alike(lambda: ref_rule_cols(t, n), lambda: roads._rule_cols(t, n),
                                              what + " rule " + name)
                if want is not None:
                    self.same(got[0], want[0], what + " rule has " + name)
                    self.same(got[1], want[1], what + " rule value " + name)
            unbuilt = np.array([rng.random() < 0.2 for _ in range(n)], bool)
            cr = f.rules.get("connectors")
            want = ref_columns(f, unbuilt)
            got = fast._ef.spec_columns(n, list(STYLE), unbuilt, f.attrs.get("class"), f.attrs.get("subtype"),
                                        f.attrs.get("osm_junction"), f.attrs.get("osm_smoothness"),
                                        f.attrs.get("name"), f.attrs.get("osm_sidewalk"),
                                        None if cr is None or not len(cr.feature) else
                                        np.ascontiguousarray(cr.feature, np.int64))
            self.assertEqual(sorted(got), sorted(want), what)
            for key in want:
                self.same(got[key], want[key], f"{what} {key}")

    def test_columns_of_other_lengths_as_python(self):
        """Every attribute column spec_columns reads, shorter or longer than the layer: the same columns or the same
        exception class as _spec_inputs' Python (np.fromiter's ValueError, numpy's IndexError for a value set past the
        layer), never a panic (Codex review of 3eba68d9)."""
        rng = random.Random(3771)
        seen = set()
        for k in range(600):
            f = random_layer(rng, rng.randint(1, 8))
            n = len(f)
            name = rng.choice(["class", "subtype", "osm_junction", "osm_smoothness", "name", "osm_sidewalk"])
            col = f.attrs.get(name)
            if col is None:
                continue
            m = rng.choice([0, 1, max(n - 1, 0), n + 1, n + 3])
            vals = list(col.tolist()) + [rng.choice(["both", "x", "Main St", "good", "roundabout", "road"])
                                         for _ in range(max(m - n, 0))]
            f.attrs[name] = np.array(vals[:m] + [None], object)[:m]
            unbuilt = np.zeros(n, bool)
            cr = f.rules.get("connectors")
            try:
                want = ("ok", ref_columns(f, unbuilt))
            except Exception as e:      # noqa: BLE001
                want = ("raised", type(e))
            try:
                got = ("ok", fast._ef.spec_columns(n, list(STYLE), unbuilt, f.attrs.get("class"),
                                                   f.attrs.get("subtype"), f.attrs.get("osm_junction"),
                                                   f.attrs.get("osm_smoothness"), f.attrs.get("name"),
                                                   f.attrs.get("osm_sidewalk"),
                                                   None if cr is None or not len(cr.feature) else
                                                   np.ascontiguousarray(cr.feature, np.int64)))
            except Exception as e:      # noqa: BLE001
                got = ("raised", type(e))
            except BaseException as e:  # noqa: BLE001
                self.fail(f"{type(e).__name__}: {e}")
            self.assertEqual(got[0], want[0], (k, name, n, m))
            if want[0] == "raised":
                self.assertIs(got[1], want[1], (k, name, n, m))
                seen.add((name, want[1].__name__))
            else:
                for key in want[1]:
                    self.same(got[1][key], want[1][key], f"{k} {name} {key}")
        self.assertGreaterEqual(len(seen), 5, seen)

    def test_short_columns_raise_not_panic(self):
        """Columns shorter than the layer (or a sidewalk column longer than it): a Python exception, never a Rust
        panic (Codex review of d0888ef3)."""
        f = random_layer(random.Random(3), 6)
        n = len(f)
        for unbuilt, cls, sub in ((np.zeros(n - 2, bool), f.attrs.get("class"), None),
                                  (np.zeros(n, bool), np.array(["motorway"] * (n - 1), object), None),
                                  (np.zeros(n, bool), np.array(["motorway"] * n, object), np.array(["road"] * (n - 1)))):
            try:
                fast._ef.spec_columns(n, list(STYLE), unbuilt, cls, sub, None, None, None, None, None)
            except Exception as e:      # noqa: BLE001
                self.assertIsInstance(e, ValueError)
            except BaseException as e:  # noqa: BLE001
                self.fail(f"{type(e).__name__}: {e}")
        try:
            fast._ef.cycle_streets(2, np.array(["cycleway"] * 4, object), np.array(["both"] * 4, object))
        except Exception as e:          # noqa: BLE001
            self.assertIsInstance(e, IndexError)
        except BaseException as e:      # noqa: BLE001
            self.fail(f"{type(e).__name__}: {e}")
        with self.assertRaises(IndexError):
            ref_cycle_streets(type("L", (), {"attrs": {"class": np.array(["cycleway"] * 4, object),
                                                       "osm_sidewalk": np.array(["both"] * 4, object)},
                                             "__len__": lambda self: 2})())

    def test_large_ints_and_overflow(self):
        f = random_layer(random.Random(1), 3)
        f.attrs["osm_lanes"] = np.array([10 ** 400, "3", None], object)
        with self.assertRaises(OverflowError):
            ref_osm_values(f, "lanes", *OSM_LANES)
        with self.assertRaises(OverflowError):
            roads._osm_values(f, "lanes", *OSM_LANES)

    def test_rule_value_not_a_number_raises(self):
        t = mapgeom.RuleTable(np.array([0], np.int32), np.array(["x"], object), np.zeros(1, np.float32),
                              np.ones(1, np.float32))
        with self.assertRaises(ValueError):
            ref_rule_cols(t, 2)
        with self.assertRaises(ValueError):
            roads._rule_cols(t, 2)

    def test_lengths_known(self):
        f = random_layer(random.Random(2), 40)
        self.same(roads._lengths(f), ref_lengths(f), "40 features")


if __name__ == "__main__":
    unittest.main()
