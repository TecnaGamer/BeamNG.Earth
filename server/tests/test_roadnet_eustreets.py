"""_road_specs' European street pass (earthfast 401, roadnet::eustreets: the road revamp's R2a) against its Python, call
for call: random layers of street and other specs (None, built or not, links, divided, pass roads, rings, France's
rural flag), roads with no points, mapped widths of every kind (the width rule's float32 values in and out of 1-40 m,
NaN; OSM's text and numbers, rejected or not, out of 2-40 m), Germany's surveyed roads, frontage with NaN rows, names
and sidewalk tags of any type. europewalks.plan and country_for are stand-ins that record their arguments (types
included): the same calls in the same order, the same specs out, the same exception classes for short columns.

The Python version is kept here only as the reference (the server has no Python twin: fast.require)."""
import os
import random
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import europewalks  # noqa: E402
import franceroads  # noqa: E402
import mapgeom  # noqa: E402
import roads  # noqa: E402


def ref_eu_pass(feats, out, fr, surveyed, urban_fn):
    """_road_specs' `if european:` block (before earthfast 401), verbatim."""
    BUILT, is_ring, OSM_WIDTH_M = roads.BUILT, roads.is_ring, roads.OSM_WIDTH_M
    mapped_rule_width, mapped_osm_width = roads.mapped_rule_width, roads.mapped_osm_width
    names = feats.attrs.get("name")
    tags = feats.attrs.get("osm_sidewalk")
    for i, spec in enumerate(out):
        if (spec is None or spec.kind not in BUILT or spec.cls not in europewalks.STREETS
                or spec.link or spec.median or spec.mountain or is_ring(feats, i)):
            continue
        a = feats.coord_off[feats.ring_off[feats.part_off[i]]]
        b = feats.coord_off[feats.ring_off[feats.part_off[i + 1]]]
        if b <= a:
            continue
        lon, lat = map(float, feats.lonlat[(a + b) // 2])
        country = europewalks.country_for(lon, lat)
        if country is None:
            continue
        width = mapped_rule_width(feats, i)
        if width is None or not 1.0 <= float(width) <= 40.0:
            width = mapped_osm_width(feats, i, *OSM_WIDTH_M)
        if width is None and i in surveyed:     # (Germany's surveyed paved width stands as a mapped one)
            width = spec.width
        # France's rural experiment already has its own narrower widths.
        if width is None and franceroads.enabled(spec) and not spec.urban:
            width = spec.width
        front = (0., 0.) if np.isnan(fr[i, 0]) else (float(fr[i, 0]), float(fr[i, 1]))
        out[i] = europewalks.plan(spec, country, lon, lat, str(names[i]) if names is not None else "",
                                  str(tags[i]) if tags is not None else "", width, front, urban_fn is not None)
    return out


CLASSES = sorted(europewalks.STREETS) + ["motorway", "service", "track", "cycleway"]


def spec(rng):
    if rng.random() < 0.08:
        return None
    return roads.Spec(rng.choice([6.0, np.float32(7.5), np.float64(5.2), 9]), rng.choice(["paved", "paved", "cobble",
                      "gravel", "dirt"]), rng.random() < 0.5, rng.choice(CLASSES), rng.random() < 0.3,
                      median=rng.choice([None, None, None, 0.0, 6.5]), lanes=rng.choice([None, 1, 2, 3]),
                      link=rng.random() < 0.1, mountain=rng.random() < 0.08, france=rng.random() < 0.3)


def layer(rng, n):
    po, ro, co, ll = [0], [0], [0], []
    lon0, lat0 = rng.uniform(-10, 30), rng.uniform(36, 60)
    for _ in range(n):
        for _r in range(rng.choice([1, 1, 2])):
            for _ in range(rng.choice([0, 1, 2, 3, 5])):
                ll.append((lon0 + rng.uniform(-0.02, 0.02), lat0 + rng.uniform(-0.02, 0.02)))
            co.append(len(ll))
        ro.append(len(co) - 1)
        po.append(len(ro) - 1)
    f = mapgeom.Features("roads", np.array([f"r{i}" for i in range(n)]), np.full(n, 2, np.uint8),
                         np.array(po, np.int64), np.array(ro, np.int64), np.array(co, np.int64),
                         np.array(ll, np.float64).reshape(-1, 2))
    if rng.random() < 0.6:
        f.attrs["osm_junction"] = np.array([rng.choice(["", "roundabout", "circular", "yes", None]) for _ in range(n)],
                                           object)
    if rng.random() < 0.8:
        f.attrs["name"] = np.array([rng.choice(["High Street", "", None, 7, "Rue de l'Église"]) for _ in range(n)],
                                   object)
    if rng.random() < 0.7:
        f.attrs["osm_sidewalk"] = np.array([rng.choice(["both", "left", " yes ", "no", "", None]) for _ in range(n)],
                                           object)
    if rng.random() < 0.6:
        f.attrs["osm_width"] = np.array([rng.choice(["5", "6.5 m", 3.2, 1.5, 45, "nan", None, "12", np.nan])
                                         for _ in range(n)], object)
    return f


def table(rng, n):
    if rng.random() < 0.3:
        return None
    entries = [(i, rng.choice([5.5, 0.5, 42.0, 12.0, float("nan"), 3.0, 40.0, 1.0])) for i in range(n) if rng.random() < 0.5]
    if not entries:
        return mapgeom.RuleTable(np.zeros(0, np.int32), np.zeros(0, np.float32), np.zeros(0, np.float32),
                                 np.zeros(0, np.float32))
    return mapgeom.RuleTable(np.array([e[0] for e in entries], np.int32), np.array([e[1] for e in entries], np.float32),
                             np.zeros(len(entries), np.float32), np.ones(len(entries), np.float32))


def same(a, b):
    if type(a) is not type(b):
        return False
    if isinstance(a, (tuple, list)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, (float, np.floating)):
        return np.asarray(a).tobytes() == np.asarray(b).tobytes()
    return a is b or a == b


class EuStreetPassParityTest(unittest.TestCase):
    def run_both(self, rng, f, specs, fr, surveyed, urban_fn, wt, rej, fr_on):
        logs = {}
        out = {}
        for tag, fn in (("ref", ref_eu_pass), ("new", roads._eu_street_pass)):
            log = logs[tag] = []

            def country_for(lon, lat):
                log.append(("country", type(lon), lon, type(lat), lat))
                h = hash((round(lon, 4), round(lat, 4))) % 5
                return None if h == 0 else ("FR", "GB", "RO", "DE")[h - 1]

            def plan(*args):
                log.append(("plan",) + args)
                return ("planned", args[0], sum(1 for e in log if isinstance(e, tuple) and e[0] == "plan"))

            def width_table(feats):
                log.append("width_table")
                return wt

            def width_rejected(feats):
                log.append("width_rejected")
                return None, rej
            with mock.patch.object(europewalks, "country_for", country_for), \
                    mock.patch.object(europewalks, "plan", plan), \
                    mock.patch.object(roads, "width_table", width_table), \
                    mock.patch.object(roads, "width_rejected", width_rejected), \
                    mock.patch.object(franceroads, "ON", fr_on):
                try:
                    out[tag] = ("ok", fn(f, list(specs), fr, surveyed, urban_fn))
                except Exception as e:      # noqa: BLE001
                    out[tag] = ("raised", type(e))
        return out, logs

    def test_random_layers(self):
        rng = random.Random(401)
        planned = 0
        for k in range(3000):
            n = rng.randint(0, 10)
            f = layer(rng, n)
            specs = [spec(rng) for _ in range(n)]
            fr = np.array([rng.choice([[np.nan, np.nan], [np.nan, np.nan], [rng.random(), rng.random()],
                                       [rng.random(), rng.random()], [np.nan, 0.3], [0.2, np.nan]])
                           for _ in range(n)], np.float64).reshape(n, 2)
            surveyed = {i for i in range(n) if rng.random() < 0.2}
            out, logs = self.run_both(rng, f, specs, fr, surveyed, rng.choice([None, len]), table(rng, n),
                                      np.array([rng.random() < 0.3 for _ in range(n)], bool), rng.random() < 0.5)
            self.assertEqual(out["new"][0], out["ref"][0], (k, out))
            if out["ref"][0] == "ok":
                self.assertTrue(same(out["new"][1], out["ref"][1]), k)
            else:
                self.assertIs(out["new"][1], out["ref"][1], k)
            calls = {t: [e for e in logs[t] if isinstance(e, tuple)] for t in logs}
            self.assertTrue(same(calls["new"], calls["ref"]), (k, calls))
            # (the width rule / rejections: asked at all alike; the Python asked them per road, memoised there)
            for what in ("width_table", "width_rejected"):
                self.assertEqual(what in logs["new"], what in logs["ref"], (k, what))
            planned += sum(1 for e in calls["ref"] if e[0] == "plan")
        self.assertGreater(planned, 1500)

    def test_short_columns_raise_alike(self):
        rng = random.Random(4011)
        raised = 0
        for k in range(600):
            n = rng.randint(2, 8)
            f = layer(rng, n)
            specs = [roads.Spec(6.0, "paved", True, "residential", False) for _ in range(n)]
            fr = np.zeros((n, 2))
            cut = rng.choice(["name", "osm_sidewalk", "osm_junction", "fr", "rej", "osm_width"])
            rej = np.zeros(n, bool)
            if cut == "fr":
                fr = fr[:-1]
            elif cut == "rej":
                rej = rej[:-1]
            else:
                f.attrs[cut] = np.array(["x"] * (n - 1), object)
            out, _ = self.run_both(rng, f, specs, fr, set(), None, None, rej, False)
            self.assertEqual(out["new"], out["ref"], (k, cut))
            raised += out["ref"][0] == "raised"
        self.assertGreater(raised, 100)

    def test_none_rule_values(self):
        """A width rule whose value is None (an object column): no width, OSM's and the rest after it, the road planned
        and the later ones too (Codex on 490d400d: the port's float(None) raised TypeError)."""
        rng = random.Random(4012)
        fell_back = 0
        for k in range(1500):
            n = rng.randint(1, 8)
            f = layer(rng, n)
            if rng.random() < 0.7:
                f.attrs["osm_width"] = np.array([rng.choice(["5", "6.5", 3.2, None, "12"]) for _ in range(n)], object)
            specs = [roads.Spec(6.0, "paved", True, rng.choice(["residential", "tertiary", "unclassified"]), False,
                                france=rng.random() < 0.3) for _ in range(n)]
            entries = [(i, rng.choice([None, None, 5.5, 0.5, "7", None, np.float32(9.0)])) for i in range(n)
                       if rng.random() < 0.8]
            vals = np.empty(len(entries), object)
            vals[:] = [e[1] for e in entries]
            wt = mapgeom.RuleTable(np.array([e[0] for e in entries], np.int32), vals,
                                   np.zeros(len(entries), np.float32), np.ones(len(entries), np.float32))
            fr = np.zeros((n, 2))
            out, logs = self.run_both(rng, f, specs, fr, {i for i in range(n) if rng.random() < 0.3}, None, wt,
                                      np.array([rng.random() < 0.2 for _ in range(n)], bool), rng.random() < 0.5)
            self.assertEqual(out["new"][0], out["ref"][0], (k, out))
            self.assertEqual(out["ref"][0], "ok", k)
            self.assertTrue(same(out["new"][1], out["ref"][1]), k)
            calls = {t: [e for e in logs[t] if isinstance(e, tuple)] for t in logs}
            self.assertTrue(same(calls["new"], calls["ref"]), (k, calls))
            fell_back += sum(1 for e in calls["ref"] if e[0] == "plan" and isinstance(e[7], float))
        self.assertGreater(fell_back, 300)

    def test_extreme_offsets_wrap_as_numpy(self):
        """Coordinate offsets near the int64 ends: numpy's (a + b) // 2 wraps, a negative index counts from the end;
        never a panic or a build-dependent answer (Codex on 490d400d)."""
        import warnings
        big, small = np.iinfo(np.int64).max, np.iinfo(np.int64).min
        rng = random.Random(4013)
        seen = set()
        for k in range(600):
            n = rng.randint(1, 5)
            f = layer(rng, n)
            if len(f.lonlat) < 2:
                continue
            co = f.coord_off.copy()
            for _ in range(rng.choice([1, 2])):
                co[rng.randrange(len(co))] = rng.choice([big, big - 1, big - 2, small, small + 1, small + 3, -1, -3])
            f.coord_off = co
            specs = [roads.Spec(6.0, "paved", True, "residential", False) for _ in range(n)]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                out, logs = self.run_both(rng, f, specs, np.zeros((n, 2)), set(), None, None, np.zeros(n, bool), False)
            self.assertEqual(out["new"][0], out["ref"][0], (k, co.tolist(), out))
            if out["ref"][0] == "ok":
                self.assertTrue(same(out["new"][1], out["ref"][1]), k)
            else:
                self.assertIs(out["new"][1], out["ref"][1], k)
            calls = {t: [e for e in logs[t] if isinstance(e, tuple)] for t in logs}
            self.assertTrue(same(calls["new"], calls["ref"]), (k, calls))
            seen.add(out["ref"][0])
            if out["ref"][0] == "ok" and any(e[0] == "plan" for e in calls["ref"]):
                seen.add("planned")
        self.assertEqual(seen, {"ok", "raised", "planned"})


if __name__ == "__main__":
    unittest.main()
