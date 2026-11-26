"""roadlanes.count and roadlanes.join in Rust (earthfast 456, lanesjoin: the full Rust port) against the Python they
replaced: count over random values (numbers, numpy scalars, text, None, bools, huge ints, objects whose float()
raises) and minimums; join over random road layers (motorways / trunks / streets, own ways with and without counts,
missing ways, relations) with tagged highway ways along the roads (the geometry match: offsets, reversed and one-way
ways, links, rule tables, a geometry callback answering, raising or answering nothing): the same need boxes asked,
the same attrs (keys in order, dtype, bytes), the same tags after, the same exception class.

The Python versions are kept here only as the reference (the server has no Python twin: fast.require)."""
import math
import os
import random
import sys
import types
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dnp  # noqa: E402
import fast  # noqa: E402
import mapgeom  # noqa: E402
import roadlanes  # noqa: E402
from roadlanes import HIGHWAYS, MAX_LANES, invalidate, is_link, rule  # noqa: E402


# ---- the Python versions (before earthfast 456), verbatim ----

def ref_count(value, minimum=1):
    """A whole lane count, not a prefix of a list, fraction or conditional tag."""
    try:
        n = float(value)
        return int(n) if np.isfinite(n) and n.is_integer() and minimum <= n <= MAX_LANES else None
    except (TypeError, ValueError):
        return None


def ref_project(line, points):
    delta = np.diff(line, axis=0)
    length = np.maximum(dnp.norm(delta, axis=1), 1e-9)
    t = np.clip(((points[:, None] - line[None, :-1]) * delta).sum(2) / (length * length), 0., 1.)
    q = line[None, :-1] + t[:, :, None] * delta
    dist = dnp.norm(points[:, None] - q, axis=2)
    k = dist.argmin(1)
    return dist[np.arange(len(points)), k], delta[k] / length[k, None]


def ref_join(feats, tags, ids, geometry=None):
    count, _project = ref_count, ref_project
    n = len(feats)
    if geometry is not None:
        classes_ = feats.attrs.get("class", [""] * n)
        need = []           # (lon0, lat0, lon1, lat1) of each highway whose own way is not among the tags
        for i in range(n):
            if classes_[i] in HIGHWAYS and ids[i] not in tags:
                parts = feats.parts(i)
                if len(parts) == 1 and len(parts[0]) == 1 and len(parts[0][0]) >= 2:
                    ll = np.asarray(parts[0][0], np.float64)
                    need.append((float(ll[:, 0].min()), float(ll[:, 1].min()), float(ll[:, 0].max()),
                                 float(ll[:, 1].max())))
        if need:
            try:
                for wid, p in (geometry(need) or {}).items():
                    if wid in tags:
                        tags[wid]["_coords"] = p
            except Exception as e:      # (the counts from the ways' own ids only)
                import logging
                logging.getLogger("earthd.roadlanes").warning("highway geometry: %s: %s", type(e).__name__, e)
    counts = np.array([count(v) or np.nan for v in feats.attrs.get("osm_lanes", [None] * n)])
    sources = ["osm-way" if np.isfinite(v) else "" for v in counts]
    matched = np.array([w or 0 for w in ids], dtype=np.int64)
    classes = feats.attrs.get("class", [""] * n)
    origin = feats.lonlat[0] if len(feats.lonlat) else np.zeros(2)
    scale = np.array([111320. * dnp.cos(np.radians(origin[1])), 110574.])
    candidates = []
    for wid, tag in tags.items():
        p, lanes, cls = tag.get("_coords"), count(tag.get("lanes")), (tag.get("lane_hw") or tag.get("_highway") or "")
        if lanes is None or p is None or len(p) < 2 or cls.removesuffix("_link") not in HIGHWAYS:
            continue
        p = (np.asarray(p) - origin) * scale
        p = p[np.r_[True, dnp.norm(np.diff(p, axis=0), axis=1) > .01]]
        if len(p) < 2:
            continue
        ow = str(tag.get("osm_oneway", ""))
        direction = -1 if ow == "-1" else 1 if ow in ("yes", "1", "true") else 0
        candidates.append((wid, lanes, cls.removesuffix("_link"), cls.endswith("_link"), direction,
                           p, p.min(0) - 6., p.max(0) + 6.))
    for i in range(n):
        if classes[i] not in HIGHWAYS:
            continue
        counts[i] = count(tags.get(ids[i], {}).get("lanes")) or np.nan
        sources[i] = "osm-way" if np.isfinite(counts[i]) else ""
        if sources[i] or not candidates:
            continue
        parts = feats.parts(i)
        if len(parts) != 1 or len(parts[0]) != 1:
            continue
        line = (parts[0][0] - origin) * scale
        if len(line) < 2:
            continue
        line = line[np.r_[True, dnp.norm(np.diff(line, axis=0), axis=1) > .01]]
        if len(line) < 2:
            continue
        s = np.r_[0., np.cumsum(dnp.norm(np.diff(line, axis=0), axis=1))]
        samples = np.column_stack([dnp.interp(np.linspace(0., s[-1], 9), s, line[:, d]) for d in (0, 1)])
        _, tangent = _project(line, samples)
        direction = rule(feats, "oneway", i)
        direction = 0 if direction is None else int(np.sign(direction))
        matches = []
        for wid, lanes, cls, link, od, p, lo, hi in candidates:
            if (cls != classes[i] or link != is_link(feats, i) or bool(od) != bool(direction)
                    or np.any(samples < lo) or np.any(samples > hi)):
                continue
            distance, heading = _project(p, samples)
            alignment = (tangent * heading).sum(1)
            alignment = alignment * direction * od if direction else np.abs(alignment)
            if distance.max() <= 6. and distance.mean() <= 3. and alignment.min() >= .9:
                matches.append((float(distance.mean()), wid, lanes))
        matches.sort()
        if matches and not any(m[2] != matches[0][2] and m[0] < matches[0][0] + 1. for m in matches[1:]):
            _, matched[i], counts[i] = matches[0]
            sources[i] = "osm-geometry"
    feats.attrs["lane_count"] = counts
    feats.attrs["lane_source"] = np.array(sources, dtype="<U16")
    feats.attrs["lane_osm_way"] = matched
    invalidate(feats)


class Odd:
    def __init__(self, exc):
        self.exc = exc

    def __float__(self):
        raise self.exc


COUNT_VALUES = [None, 0, 1, 2, 3, 12, 13, -1, 2.0, 2.5, 1e300, float("nan"), float("inf"), -0.0, True, False,
                "2", " 3 ", "2;3", "1e1", "nan", "inf", "４", "two", "", np.int64(4), np.float64(5.0),
                np.float32(6.0), np.int8(-3), np.float64(np.nan), np.str_("7"), b"8", 10 ** 400, -(10 ** 400),
                Odd(ValueError()), Odd(TypeError()), [1], (2,), np.array([3.0])]


def outcome(fn, *args):
    try:
        return "ok", fn(*args)
    except Exception as e:      # noqa: BLE001 (the class is the point)
        return "raised", type(e)


class CountParityTest(unittest.TestCase):
    def test_values(self):
        import warnings
        for v in COUNT_VALUES:
            for m in (None, 0, 1, 2, -5, 12, 13):
                args = (v,) if m is None else (v, m)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    want, got = outcome(ref_count, *args), outcome(roadlanes.count, *args)
                self.assertEqual(got[0], want[0], (v, m, got, want))
                if want[0] == "ok":
                    self.assertEqual((type(got[1]), got[1]), (type(want[1]), want[1]), (v, m))
                else:
                    self.assertIs(got[1], want[1], (v, m))
        self.assertEqual(outcome(roadlanes.count, 3, 1.0), ("raised", TypeError))
        self.assertEqual(roadlanes.count(3, minimum=4), None)


class Oneway:
    """A rule table stand-in: major(i) and value, as roadlanes.rule reads them."""
    def __init__(self, values):
        self.value = np.asarray(values, np.float64)

    def major(self, i):
        return i if i < len(self.value) and not np.isnan(self.value[i]) else -1


M = 111320.0


def case(rng):
    n = rng.randint(0, 8)
    lon0, lat0 = rng.uniform(-120, 30), rng.uniform(-50, 65)
    cosl = math.cos(math.radians(lat0))
    po, ro, co, ll = [0], [0], [0], []
    lines = []
    for _ in range(n):
        nparts = rng.choice([1, 1, 1, 2])
        for _p in range(nparts):
            k = rng.choice([0, 1, 2, 3, 5])
            x, y, h = rng.uniform(-300, 300), rng.uniform(-300, 300), rng.uniform(0, 2 * math.pi)
            pts = []
            for j in range(k):
                step = rng.choice([40.0, 40.0, 40.0, 0.0, 0.005, 25.0])
                x, y = x + step * math.cos(h), y + step * math.sin(h)
                h += rng.uniform(-0.2, 0.2)
                pts.append((lon0 + x / (M * cosl), lat0 + y / 110574.0))
            ll.extend(pts)
            lines.append(pts)
            co.append(len(ll))
            ro.append(len(co) - 1)
        po.append(len(ro) - 1)
    f = mapgeom.Features("roads", np.array([f"s{i}" for i in range(n)]), np.full(n, mapgeom.LINE, np.uint8),
                         np.array(po, np.int64), np.array(ro, np.int64), np.array(co, np.int64),
                         np.array(ll, np.float64).reshape(-1, 2))
    classes = [rng.choice(["motorway", "trunk", "motorway", "primary"]) for _ in range(n)]
    if rng.random() < 0.95:
        f.attrs["class"] = mapgeom.text_column(classes)
    if rng.random() < 0.4:
        f.attrs["subclass"] = mapgeom.text_column([rng.choice(["link", "", "", ""]) for _ in range(n)])
    if rng.random() < 0.5:
        f.rules = {"oneway": Oneway([rng.choice([1.0, -1.0, np.nan, np.nan]) for _ in range(n)])}
    tags, ids = {}, []
    for i in range(n):
        r = rng.random()
        w = rng.randrange(1, 10 ** 6)
        ids.append(None if r < 0.15 else w)
        if r > 0.5:
            tags[w] = {"lanes": rng.choice([2, 3, 13, 0])} if rng.random() < 0.6 else {}
    lanes_col = rng.random()
    if lanes_col < 0.3:
        f.attrs["osm_lanes"] = np.array([rng.choice([1.0, 2.0, np.nan, 13.0, 2.5]) for _ in range(n)])
    elif lanes_col < 0.5:
        f.attrs["osm_lanes"] = np.array([rng.choice([1, 2, 4]) for _ in range(n)], np.int64)
    elif lanes_col < 0.6:
        f.attrs["osm_lanes"] = [rng.choice([None, 2, "3", 2.5]) for _ in range(n)]
    # highway ways along the roads (some reversed, offset, cut short, links), some far away
    geo = {}
    road_class = [classes[i] for i in range(n) for _p in range(po[i + 1] - po[i])]
    for pts, rc in zip(lines, road_class):
        for _ in range(rng.choice([0, 1, 1, 2])):
            if len(pts) < 2:
                continue
            w = rng.randrange(10 ** 6, 2 * 10 ** 6)
            off = rng.choice([0.0, 1.0, 2.5, 8.0])
            q = np.array(pts, np.float64) + np.array([off / (M * cosl), 0.0])
            if rng.random() < 0.3:
                q = q[::-1].copy()
            if rng.random() < 0.2:
                q = q[:2].copy()
            t = {"lanes": rng.choice([2, 3, 3, 4, None])}
            if t["lanes"] is None:
                del t["lanes"]
            t["lane_hw"] = rng.choice([rc, rc, rc, rc + "_link", "primary", ""])
            if rng.random() < 0.3:
                t["_highway"] = rng.choice(["motorway", "trunk_link"])
            if rng.random() < 0.4:
                t["osm_oneway"] = rng.choice(["yes", "-1", "no", "1", "true"])
            if rng.random() < 0.7:
                t["_coords"] = q
            else:
                geo[w] = q
            tags[w] = t
    mode = rng.random()
    if mode < 0.25:
        geometry = None
    elif mode < 0.35:
        def geometry(need):
            raise RuntimeError("down")
    elif mode < 0.45:
        def geometry(need):
            return None
    else:
        def geometry(need, geo=geo):
            return dict(geo)
    return f, tags, ids, geometry


def copy_layer(f):
    g = mapgeom.Features(f.layer, f.ids.copy(), f.kind.copy(), f.part_off.copy(), f.ring_off.copy(), f.coord_off.copy(),
                         f.lonlat.copy())
    g.attrs = {k: (v.copy() if isinstance(v, np.ndarray) else list(v) if isinstance(v, list) else v)
               for k, v in f.attrs.items()}
    if "rules" in f.__dict__:
        g.rules = f.rules
    return g


def copy_tags(tags):
    return {w: dict(t) for w, t in tags.items()}


def same_attrs(a, b):
    if list(a) != list(b):
        return False
    for k in a:
        x, y = a[k], b[k]
        if isinstance(y, np.ndarray):
            if not (isinstance(x, np.ndarray) and x.dtype == y.dtype and x.shape == y.shape and x.tobytes() == y.tobytes()):
                return False
        elif x != y:
            return False
    return True


def same_tags(a, b):
    if list(a) != list(b):
        return False
    for w in a:
        if list(a[w]) != list(b[w]):
            return False
        for k in a[w]:
            x, y = a[w][k], b[w][k]
            if (x is not y) and not (isinstance(x, np.ndarray) and isinstance(y, np.ndarray) and np.array_equal(x, y)) \
                    and x != y:
                return False
    return True


class JoinParityTest(unittest.TestCase):
    def check(self, f, tags, ids, geometry):
        g, ta, tb = copy_layer(f), copy_tags(tags), copy_tags(tags)
        asked = {"ref": [], "new": []}

        def wrap(side):
            if geometry is None:
                return None
            return lambda need: (asked[side].append(list(need)), geometry(need))[1]
        want = outcome(ref_join, f, ta, list(ids), wrap("ref"))
        got = outcome(roadlanes.join, g, tb, list(ids), wrap("new"))
        self.assertEqual(got[0], want[0], (got, want))
        if want[0] != "ok":
            self.assertIs(got[1], want[1])
            return "raised"
        self.assertEqual(asked["new"], asked["ref"])
        self.assertTrue(same_attrs(g.attrs, f.attrs), (g.attrs, f.attrs))
        self.assertTrue(same_tags(tb, ta))
        return f.attrs["lane_source"]

    def test_random_layers(self):
        rng = random.Random(456)
        kinds = {"osm-way": 0, "osm-geometry": 0, "": 0, "raised": 0}
        for k in range(6000):
            f, tags, ids, geometry = case(rng)
            with self.subTest(k=k):
                src = self.check(f, tags, ids, geometry)
                if isinstance(src, str):
                    kinds["raised"] += 1
                else:
                    for s in src:
                        kinds[str(s)] += 1
        self.assertGreater(kinds["osm-geometry"], 400, kinds)
        self.assertGreater(kinds["osm-way"], 2000, kinds)
        self.assertLess(kinds["raised"], 1500, kinds)

    def test_contract(self):
        f, tags, ids, geometry = case(random.Random(5))
        while not len(f):
            f, tags, ids, geometry = case(random.Random(len(tags) + 7))

        class D(dict):
            pass
        bad_ids = list(ids)
        bad_ids[0] = "1"
        for t, i in ((D(tags), ids), ({"1": {}}, ids), ({1: D()}, ids), (tags, tuple(ids)), (tags, bad_ids),
                     (tags, list(ids) + [None])):
            self.assertEqual(outcome(roadlanes.join, copy_layer(f), t, i), ("raised", TypeError))
        q = {5: {"lanes": 2, "lane_hw": "motorway", "_coords": np.zeros((3, 2), np.float32)}}
        self.assertEqual(outcome(roadlanes.join, copy_layer(f), q, list(ids)), ("raised", TypeError))


class VersionSkewTest(unittest.TestCase):
    def test_a_455_extension_is_refused(self):
        self.assertGreaterEqual(fast.MIN_VERSION, 456)
        with mock.patch.object(fast, "_ef", types.SimpleNamespace(VERSION=455)), \
                mock.patch.object(roadlanes, "_ef_lanes", fast.native(456, "roadlanes")):
            with self.assertRaises(fast.NativeMissing):
                roadlanes.count(2)
            f, tags, ids, geometry = case(random.Random(1))
            with self.assertRaises(fast.NativeMissing):
                roadlanes.join(f, tags, ids)


if __name__ == "__main__":
    unittest.main()
