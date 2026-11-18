"""osmtags.join's columns in Rust (earthfast 455, osmjoin: the full Rust port) against the Python it replaced: random
road layers (way records, relation records, empty ones, names shared or not, motorways / trunks / streets, one or more
parts, lines without points) and tag dicts (lanes / width / maxspeed / layer numbers, text tags, names with and without
boxes, one-way senses): the same attrs columns (keys in order, dtype, bytes: an all-counts lanes column int64, else
float64) and the same way ids handed to roadlanes.join. Outside the input contract (a tag dict subclass, a str way
id, a list attrs): TypeError.

The Python version is kept here only as the reference (the server has no Python twin: fast.require)."""
import math
import os
import random
import sys
import types
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dmath  # noqa: E402
import fast  # noqa: E402
import mapgeom  # noqa: E402
import osmtags  # noqa: E402
import roadlanes  # noqa: E402
from osmtags import FLOATS, NAME_MATCH_M, ONEWAY_YES, TEXTS, way_id  # noqa: E402


# ---- the Python versions (before earthfast 455), verbatim; roadlanes.join left out (passed in) ----

def ref_named_way(feats, i, by_name):
    names = feats.attrs.get("name")
    cands = by_name.get(str(names[i])) if names is not None else None
    if not cands:
        return None
    a = feats.coord_off[feats.ring_off[feats.part_off[i]]]
    b = feats.coord_off[feats.ring_off[feats.part_off[i + 1]]]
    if b <= a:
        return None
    lon, lat = (float(v) for v in feats.lonlat[(a + b - 1) // 2][:2])
    my = NAME_MATCH_M / 111320.0
    mx = my / max(dmath.cos(math.radians(lat)), 0.1)
    best = None
    for w, x0, y0, x1, y1 in cands:
        if x0 - mx <= lon <= x1 + mx and y0 - my <= lat <= y1 + my:
            area = (x1 - x0) * (y1 - y0)
            if best is None or area < best[0]:
                best = (area, w)
    return best[1] if best else None


def ref_join(feats, tags, lanes_join):
    import mapgeom as mg
    _named_way = ref_named_way
    recs = feats.attrs.get("src_record")
    n = len(feats)
    ids = [way_id(str(recs[i])) if recs is not None else None for i in range(n)]
    by_name = {}
    for w, t in tags.items():
        if t.get("name") and all(k in t for k in ("bx0", "by0", "bx1", "by1")):
            by_name.setdefault(str(t["name"]), []).append((w, t["bx0"], t["by0"], t["bx1"], t["by1"]))
    named = [w if w in tags else _named_way(feats, i, by_name) for i, w in enumerate(ids)]
    for c in FLOATS:
        if c == "lanes":
            classes = feats.attrs.get("class", [""] * n)
            src = [ids[i] if classes[i] in roadlanes.HIGHWAYS else w for i, w in enumerate(named)]
            feats.attrs["osm_lanes"] = np.array([roadlanes.count(tags.get(w, {}).get(c)) or np.nan for w in src])
            continue
        feats.attrs["osm_" + c if not c.startswith("osm_") else c] = np.array(
            [float(tags.get(w, {}).get(c, np.nan)) if w is not None else np.nan for w in named], np.float64)
    for c in TEXTS:
        src = ids if c == "osm_oneway" else named
        feats.attrs[c if c.startswith("osm_") else "osm_" + c] = mg.text_column(
            [str(tags.get(w, {}).get(c, "")) if w is not None else "" for w in src])
    lanes, ows = feats.attrs["osm_lanes"], feats.attrs["osm_oneway"]
    names = feats.attrs.get("name")
    sense = {}
    for w, t in tags.items():
        if "lanes" in t and t.get("name") and all(k in t for k in ("bx0", "by0", "bx1", "by1")):
            key = (str(t["name"]), str(t.get("osm_oneway", "")) in ONEWAY_YES)
            sense.setdefault(key, []).append((w, t["bx0"], t["by0"], t["bx1"], t["by1"]))
    for i in range(n if sense and names is not None else 0):
        if np.isnan(lanes[i]) and classes[i] not in roadlanes.HIGHWAYS:
            nm = str(names[i])
            w = _named_way(feats, i, {nm: sense.get((nm, str(ows[i]) in ONEWAY_YES), [])})
            if w is not None:
                lanes[i] = roadlanes.count(tags[w]["lanes"]) or np.nan
    lanes_join(feats, tags, ids, None)
    return feats


NAMES = ["Main St", "Broadway", "Rue de l'Église", "", "大街"]


def case(rng):
    n = rng.randint(0, 9)
    lon0, lat0 = rng.uniform(-80, 30), rng.uniform(-50, 70)
    po, ro, co, ll = [0], [0], [0], []
    for _ in range(n):
        for _p in range(rng.choice([1, 1, 2])):
            for _ in range(rng.choice([0, 2, 3])):
                ll.append((lon0 + rng.uniform(-2e-3, 2e-3), lat0 + rng.uniform(-2e-3, 2e-3)))
            co.append(len(ll))
            ro.append(len(co) - 1)
        po.append(len(ro) - 1)
    f = mapgeom.Features("roads", np.array([f"s{i}" for i in range(n)]), np.full(n, mapgeom.LINE, np.uint8),
                         np.array(po, np.int64), np.array(ro, np.int64), np.array(co, np.int64),
                         np.array(ll, np.float64).reshape(-1, 2))
    ways = [rng.randrange(1, 60) for _ in range(rng.randint(0, 12))]
    recs = [rng.choice([f"w{rng.choice(ways) if ways else 5}@{rng.randint(0, 3)}", f"r{rng.randint(1, 9)}@1", "",
                        "w", "w12x", f"w{rng.randrange(1, 60)}"]) for _ in range(n)]
    if rng.random() < 0.9:
        f.attrs["src_record"] = mapgeom.text_column(recs)
    if rng.random() < 0.9:
        f.attrs["name"] = mapgeom.text_column([rng.choice(NAMES) for _ in range(n)])
    if rng.random() < 0.9:
        f.attrs["class"] = mapgeom.text_column([rng.choice(["motorway", "trunk", "residential", "primary"])
                                                for _ in range(n)])
    tags = {}
    for w in ways:
        t = {}
        if rng.random() < 0.7:
            t["lanes"] = rng.choice([1, 2, 3, 4, 13, 0])
        for c in ("width", "maxspeed", "osm_layer"):
            if rng.random() < 0.3:
                t[c] = rng.choice([7.5, 50.0, -1, 2])
        for c in TEXTS:
            if rng.random() < 0.3:
                t[c] = rng.choice(["yes", "no", "-1", "both", "asphalt", "construction"])
        if rng.random() < 0.7:
            t["name"] = rng.choice(NAMES)
        if rng.random() < 0.7:
            x, y = lon0 + rng.uniform(-2e-3, 2e-3), lat0 + rng.uniform(-2e-3, 2e-3)
            t.update(bx0=x, by0=y, bx1=x + rng.uniform(0, 2e-3), by1=y + rng.uniform(0, 2e-3))
        tags[w] = t
    return f, tags


def copy_layer(f):
    g = mapgeom.Features(f.layer, f.ids.copy(), f.kind.copy(), f.part_off.copy(), f.ring_off.copy(), f.coord_off.copy(),
                         f.lonlat.copy())
    g.attrs = {k: None if v is None else v.copy() for k, v in f.attrs.items()}
    return g


def outcome(fn, *args):
    try:
        return "ok", fn(*args)
    except Exception as e:      # noqa: BLE001 (the class is the point)
        return "raised", type(e)


class OsmJoinParityTest(unittest.TestCase):
    def test_random_layers(self):
        rng = random.Random(455)
        named = laned = 0
        for k in range(4000):
            f, tags = case(rng)
            g = copy_layer(f)
            seen = {}

            def lanes_join(feats, tags_, ids, geometry, side="ref"):
                seen[side] = list(ids)
            want = outcome(ref_join, f, {w: dict(t) for w, t in tags.items()}, lanes_join)
            with mock.patch.object(roadlanes, "join", lambda feats, tags_, ids, geometry: lanes_join(
                    feats, tags_, ids, geometry, "new")):
                got = outcome(osmtags.join, g, {w: dict(t) for w, t in tags.items()})
            self.assertEqual(got[0], want[0], (k, got, want))
            if want[0] != "ok":
                self.assertIs(got[1], want[1], k)
                continue
            self.assertEqual(seen["new"], seen["ref"], k)
            self.assertEqual(list(g.attrs), list(f.attrs), k)
            for key in f.attrs:
                a, b = g.attrs[key], f.attrs[key]
                self.assertEqual((a.dtype, a.shape, a.tobytes()), (b.dtype, b.shape, b.tobytes()), (k, key))
            named += sum(1 for w in seen["ref"] if w is None)
            laned += int(np.isfinite(f.attrs["osm_lanes"].astype(float)).sum())
        self.assertGreater(named, 1000)
        self.assertGreater(laned, 1000)

    def check_same(self, f, tags):
        g = copy_layer(f)
        seen = {}
        want = outcome(ref_join, f, {w: dict(t) for w, t in tags.items()}, lambda a, b, ids, c: seen.update(ref=ids))
        with mock.patch.object(roadlanes, "join", lambda a, b, ids, c: seen.update(new=list(ids))):
            got = outcome(osmtags.join, g, {w: dict(t) for w, t in tags.items()})
        self.assertEqual(got[0], want[0], (got, want))
        if want[0] != "ok":
            self.assertIs(got[1], want[1])
            return
        self.assertEqual(seen["new"], seen["ref"])
        self.assertEqual(list(g.attrs), list(f.attrs))
        for key in f.attrs:
            a, b = g.attrs[key], f.attrs[key]
            if b is None:
                self.assertIsNone(a, key)
                continue
            self.assertEqual((a.dtype, a.shape, a.tobytes()), (b.dtype, b.shape, b.tobytes()), key)

    def test_lone_surrogates(self):
        """Records and names with lone surrogates (valid Python str, no UTF-8): way ids by re's \\d, names as keys."""
        rng = random.Random(7)
        for k in range(300):
            f, tags = case(rng)
            n = len(f)
            nm = ["Main\udc80", "Main", "\ud800", "Broadway"]
            f.attrs["src_record"] = np.array([rng.choice(["w12\udc80", "w\udc80", "w3\ud800x", "w١٢", "w7@1", "r1"])
                                              for _ in range(n)], dtype="<U8")
            f.attrs["name"] = np.array([rng.choice(nm) for _ in range(n)], dtype="<U8")
            for w, t in tags.items():
                if rng.random() < 0.5:
                    t["name"] = rng.choice(nm)
                if rng.random() < 0.3:
                    t["osm_oneway"] = rng.choice(["yes\udc80", "yes", "-1"])
            tags.setdefault(12, {}).update(lanes=2)
            self.check_same(f, tags)

    def test_incomplete_boxes(self):
        """A named way without all four box keys is skipped before any value is read (a non-number there too)."""
        rng = random.Random(8)
        for k in range(300):
            f, tags = case(rng)
            for t in tags.values():
                if "bx0" not in t and rng.random() < 0.5:
                    t["bx0"] = "invalid"
                    t["name"] = "Main St"
            self.check_same(f, tags)

    def test_none_columns(self):
        """A src_record / name column present as None reads as no column (the Python's attrs.get)."""
        rng = random.Random(9)
        for k in range(300):
            f, tags = case(rng)
            for c in ("src_record", "name"):
                if rng.random() < 0.5:
                    f.attrs[c] = None
            self.check_same(f, tags)

    def test_contract(self):
        f, tags = case(random.Random(3))

        class D(dict):
            pass
        box = dict(name="Main St", bx0=0.0, by0=0.0, bx1=1.0, by1=1.0)
        for bad in ({1: D(lanes=2)}, {"1": {"lanes": 2}}, {1: {"lanes": [2]}}, {1: dict(box, bx0="0")},
                    {1: dict(box, by1=1)}, {1: dict(box, bx1=np.float32(1.0))},
                    {1: dict(box, bx0=np.float64(0.0))}):
            with mock.patch.object(roadlanes, "join", lambda *a: None):
                self.assertEqual(outcome(osmtags.join, copy_layer(f), bad), ("raised", TypeError), bad)


class VersionSkewTest(unittest.TestCase):
    def test_a_454_extension_is_refused(self):
        self.assertGreaterEqual(fast.MIN_VERSION, 455)
        f, tags = case(random.Random(4))
        with mock.patch.object(fast, "_ef", types.SimpleNamespace(VERSION=454)):
            with self.assertRaises(fast.NativeMissing):
                osmtags.join(f, tags)


if __name__ == "__main__":
    unittest.main()
