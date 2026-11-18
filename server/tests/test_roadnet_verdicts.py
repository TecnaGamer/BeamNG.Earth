"""The aerial photo's surface verdicts as _spec_inputs reads them (earthfast 392, roadnet::verdicts: the road revamp's
R2a) against the Python they replaced, value for value: roadsurf.candidates (class, surface value, lane count,
smoothness: cased, padded, numbers, None, non-ASCII), roadsurf.layer_verdicts' store lookup (a store of random entries,
falsy verdicts, roads in other cells), photosurface.verdicts' selection (a stand-in measure: the photo's tiles are not
the test's) and _spec_inputs' fill of the photo's values; short columns raise the Python's exception classes.

The Python versions are kept here only as the reference (the server has no Python twin: fast.require)."""
import os
import random
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import mapgeom  # noqa: E402
import photosurface  # noqa: E402
import roads  # noqa: E402
import roadsurf  # noqa: E402


# ---- the Python versions (before earthfast 392), verbatim ----

def ref_candidates(feats, vals=None):
    import roads as roadsmod
    n = len(feats)
    cls = feats.attrs.get("class")
    if cls is None or not n:
        return np.zeros(n, bool)
    vals = roadsmod._map_surface_values(feats) if vals is None else vals
    out = np.fromiter((str(c) in roadsurf.CANDIDATE_CLASSES and v is None for c, v in zip(cls.tolist(), vals)), bool, n)
    lanes = roadsmod._osm_values(feats, "lanes", *roadsmod.OSM_LANES)
    sm = feats.attrs.get("osm_smoothness")
    good = np.zeros(n, bool) if sm is None else np.fromiter(
        (str(v).strip().lower() in roadsmod.GOOD_SMOOTHNESS for v in sm.tolist()), bool, n)
    return out & np.isnan(lanes) & ~good


def ref_layer_verdicts(feats, vals=None):
    if not roadsurf.ON or not len(feats):
        return {}
    got = feats.__dict__.get("_roadsurf")
    if got is not None:
        return got
    if not roadsurf._store_exists():
        feats.__dict__["_roadsurf"] = {}
        return {}
    import roads as roadsmod
    out = {}
    cand = ref_candidates(feats, vals)
    if cand.any():
        homes = roadsmod._home_cells(feats)
        ids = feats.ids
        for i in np.flatnonzero(cand).tolist():
            v = roadsurf.entry(homes[i]).get(str(ids[i]))
            if v:
                out[i] = v
    feats.__dict__["_roadsurf"] = out
    return out


def ref_photo_verdicts(feats, surface_of, root=None):
    cls = feats.attrs.get("class")
    out, missing = {}, False
    if cls is None or not photosurface.ON:
        return out, missing
    for i in range(len(feats)):
        if str(cls[i]) not in photosurface.PHOTO_CLASSES or surface_of(i) is not None:
            continue
        best = None
        for part in feats.parts(i):
            for ring in part:
                st, miss = photosurface.measure(ring, root)
                missing |= miss
                if st is not None and (best is None or st["n"] > best["n"]):
                    best = st
        c = photosurface.classify(best)
        if c is not None:
            out[i] = c
    return out, missing


def ref_photo_values(vals, elig, photo_fn):
    vals = list(vals)
    if any(vals[i] is None for i in np.flatnonzero(elig).tolist()):
        ph = photo_fn()
        vals = [ph.get(i) if v is None else v for i, v in enumerate(vals)]
    return vals


CLASSES = ["residential", "service", "unclassified", "unknown", "track", "primary", "tertiary", "footway",
           "motorway", " track", "Track"]
SURFACES = [None, None, None, "asphalt", "gravel", " unpaved ", "nan", "None", "", "  ", "paving_stones;sett"]
SMOOTH = [None, "good", " Good ", "EXCELLENT", "intermediate　", "bad", "", "nan", 3, 1.5, "goodish", "ǅ", "\x1cgood",
          "İntermediate"]
LANES = [None, None, "2", "1", " 3 ", "x", 2, 2.0, "nan", "0", "12"]


def layer(rng, n=None, short=None):
    n = rng.randint(0, 9) if n is None else n
    po, ro, co, ll = [0], [0], [0], []
    lon0, lat0 = rng.uniform(-120, 30), rng.uniform(-40, 60)
    for _ in range(n):
        for _ in range(rng.choice([1, 2, 3])):
            ll.append((lon0 + rng.uniform(-0.05, 0.05), lat0 + rng.uniform(-0.05, 0.05)))
        co.append(len(ll))
        ro.append(len(co) - 1)
        po.append(len(ro) - 1)
    f = mapgeom.Features("roads", np.array([f"r{rng.randint(0, 3 * n + 3)}" for _ in range(n)]),
                         np.full(n, 2, np.uint8), np.array(po, np.int64), np.array(ro, np.int64),
                         np.array(co, np.int64), np.array(ll, np.float64).reshape(-1, 2))

    def col(choices, k=n):
        if rng.random() < 0.5:
            return np.array([rng.choice(choices) for _ in range(k)], object)
        return np.array([str(rng.choice(choices)) for _ in range(k)])
    if rng.random() < 0.9:
        f.attrs["class"] = col(CLASSES, n - 1 if short == "class" else n)
    if rng.random() < 0.7:
        f.attrs["osm_surface"] = col(SURFACES)
    if rng.random() < 0.6:
        f.attrs["osm_smoothness"] = col(SMOOTH, n - 1 if short == "osm_smoothness" else n)
    if rng.random() < 0.6:
        f.attrs["osm_lanes"] = col(LANES)
    return f


def outcome(fn):
    try:
        return "ok", fn()
    except Exception as e:      # noqa: BLE001
        return "raised", type(e)


def same(a, b):
    if isinstance(a, np.ndarray):
        return isinstance(b, np.ndarray) and a.dtype == b.dtype and np.array_equal(a, b)
    if isinstance(a, dict):
        return type(b) is dict and list(a) == list(b) and all(type(a[k]) is type(b[k]) and a[k] == b[k] for k in a)
    if isinstance(a, (list, tuple)):
        return type(a) is type(b) and len(a) == len(b) and all(x is y or (type(x) is type(y) and x == y)
                                                                for x, y in zip(a, b))
    return a == b


class CandidatesParityTest(unittest.TestCase):
    def test_random_layers(self):
        rng = random.Random(389)
        met = 0
        for k in range(3000):
            f = layer(rng)
            want = ref_candidates(f)
            f.__dict__.pop("_map_surface_values", None)
            got = roadsurf.candidates(f)
            self.assertTrue(same(got, want), (k, got, want))
            met += int(want.sum())
            # (the caller's own values: a shorter or a longer list)
            vals = [rng.choice([None, "asphalt"]) for _ in range(len(f) + rng.choice([0, 0, 1, -1]))]
            w, g = outcome(lambda: ref_candidates(f, vals)), outcome(lambda: roadsurf.candidates(f, vals))
            self.assertEqual(w[0], g[0], k)
            self.assertTrue(same(g[1], w[1]) if w[0] == "ok" else g[1] is w[1], (k, g, w))
        self.assertGreater(met, 1000)

    def test_short_columns_raise_alike(self):
        rng = random.Random(3891)
        raised = 0
        for k in range(400):
            short = rng.choice(["class", "osm_smoothness"])
            f = layer(rng, rng.randint(1, 7), short)
            want = outcome(lambda: ref_candidates(f))
            f.__dict__.pop("_map_surface_values", None)
            got = outcome(lambda: roadsurf.candidates(f))
            self.assertEqual(got[0], want[0], (k, short, got, want))
            if want[0] == "raised":
                raised += 1
                self.assertIs(got[1], want[1], k)
            else:
                self.assertTrue(same(got[1], want[1]), k)
        self.assertGreater(raised, 100)


class LookupParityTest(unittest.TestCase):
    def test_random_stores(self):
        rng = random.Random(3892)
        found = 0
        for k in range(1500):
            f = layer(rng)
            homes = sorted(set(roads._home_cells(f))) + ["14/1/1"]
            store = {h: {f"r{rng.randint(0, 30)}": rng.choice([1, 2, 0, None, 2, 1])
                         for _ in range(rng.randint(0, 25))} for h in homes}
            asked = {"ref": [], "new": []}

            def entry_for(tag):
                def entry(cell):
                    asked[tag].append(cell)
                    return store.get(str(cell), {})
                return entry
            with mock.patch.object(roadsurf, "ON", True), mock.patch.object(roadsurf, "_store_exists", lambda: True):
                with mock.patch.object(roadsurf, "entry", entry_for("ref")):
                    want = ref_layer_verdicts(f)
                f.__dict__.pop("_roadsurf", None)
                with mock.patch.object(roadsurf, "entry", entry_for("new")):
                    got = roadsurf.layer_verdicts(f)
            self.assertTrue(same(got, want), (k, got, want))
            self.assertEqual(asked["new"], asked["ref"], k)
            found += len(want)
        self.assertGreater(found, 150)

    def test_short_ids_raise_alike(self):
        rng = random.Random(3893)
        for k in range(200):
            f = layer(rng, rng.randint(2, 7))
            f.attrs["class"] = np.array(["track"] * len(f))
            f.attrs.pop("osm_surface", None)
            f.attrs.pop("osm_lanes", None)
            f.attrs.pop("osm_smoothness", None)
            homes = list(roads._home_cells(f))
            cut = rng.choice(["ids", "homes"])
            with mock.patch.object(roadsurf, "ON", True), mock.patch.object(roadsurf, "_store_exists", lambda: True), \
                    mock.patch.object(roadsurf, "entry", lambda cell: {}), \
                    mock.patch.object(roads, "_home_cells", (lambda feats: homes[:-1]) if cut == "homes"
                                      else roads._home_cells):
                ids = f.ids
                if cut == "ids":
                    f.ids = ids[:-1]
                    f.__dict__["_map_surface_values"] = [None] * len(ids)
                want = outcome(lambda: ref_layer_verdicts(f))
                f.__dict__.pop("_roadsurf", None)
                got = outcome(lambda: roadsurf.layer_verdicts(f))
            self.assertEqual(got, want, (k, cut))


class PhotoSelectionParityTest(unittest.TestCase):
    def test_random_layers(self):
        rng = random.Random(3894)
        picked = 0
        for k in range(1500):
            f = layer(rng)
            measured = {"ref": [], "new": []}
            stats = {}

            def measure_for(tag):
                def measure(ring, root=None):
                    key = (round(float(ring[0][0]), 9), round(float(ring[0][1]), 9), len(ring))
                    measured[tag].append(key)
                    if key not in stats:
                        r = random.Random(hash(key))
                        stats[key] = (None if r.random() < 0.3 else {
                            "n": r.randint(0, 60), "sat": r.uniform(0, 0.3), "exg": r.uniform(-0.1, 0.12),
                            "lum": r.uniform(60, 220), "vexg": r.choice([None, r.uniform(-0.1, 0.12)])},
                            r.random() < 0.2)
                    st, miss = stats[key]
                    return (dict(st) if st else None), miss
                return measure
            surface_of = (lambda i: roads._map_surface_value(f, i))
            with mock.patch.object(photosurface, "ON", True):
                with mock.patch.object(photosurface, "measure", measure_for("ref")):
                    want = ref_photo_verdicts(f, surface_of)
                with mock.patch.object(photosurface, "measure", measure_for("new")):
                    got = photosurface.verdicts(f, surface_of)
            self.assertEqual(got, want, k)
            self.assertEqual(measured["new"], measured["ref"], k)
            picked += len(measured["ref"])
        self.assertGreater(picked, 1000)

    def test_short_class_column_raises_alike(self):
        f = layer(random.Random(5), 6, "class")
        f.attrs["class"] = np.array(["track"] * 5)
        with mock.patch.object(photosurface, "ON", True), \
                mock.patch.object(photosurface, "measure", lambda ring, root=None: (None, False)):
            want = outcome(lambda: ref_photo_verdicts(f, lambda i: None))
            got = outcome(lambda: photosurface.verdicts(f, lambda i: None))
        self.assertEqual(got, want)
        self.assertEqual(want, ("raised", IndexError))


class PhotoValuesParityTest(unittest.TestCase):
    def test_random_values(self):
        import fast
        rng = random.Random(3895)
        for k in range(5000):
            n = rng.randint(0, 9)
            vals = [rng.choice([None, "asphalt", np.str_("gravel"), "x"]) for _ in range(n)]
            elig = np.array([rng.random() < 0.5 for _ in range(n)], bool)
            ph = {i: rng.choice(["asphalt", "grass", "gravel"]) for i in range(n) if rng.random() < 0.5}
            calls = {"ref": 0, "new": 0}

            def photo_for(tag):
                def photo():
                    calls[tag] += 1
                    return ph
                return photo
            want = ref_photo_values(vals, elig, photo_for("ref"))
            got = fast._ef.photo_values(vals, elig, photo_for("new"))
            self.assertTrue(same(got, want), (k, got, want))
            self.assertEqual(calls["new"], calls["ref"])
        # an eligible index past the values: the list's IndexError
        self.assertEqual(outcome(lambda: fast._ef.photo_values([None], np.array([False, True]), dict))[1], IndexError)
        self.assertEqual(outcome(lambda: ref_photo_values([None], np.array([False, True]), dict))[1], IndexError)


class VersionSkewTest(unittest.TestCase):
    """Codex round 2 on 7916e96d: an extension of dev's 390 (R1e's) has neither roadnet::countries nor ::verdicts; it
    must be refused at the start (fast.MIN_VERSION) and by each facade (fast.require: NativeMissing), never get as far
    as an AttributeError at the first road build."""

    def test_a_390_extension_is_refused(self):
        import types

        import europewalks
        import fast
        self.assertGreaterEqual(fast.MIN_VERSION, 392)
        self.assertGreaterEqual(fast._ef.VERSION, fast.MIN_VERSION)
        old = types.SimpleNamespace(VERSION=390)
        f = layer(random.Random(1), 3)
        f.attrs["class"] = np.array(["track"] * 3)
        with mock.patch.object(fast, "_ef", old):
            for call in (lambda: europewalks.country_for(10.0, 50.0),
                         lambda: europewalks.road_countries(f, np.ones(3, bool)),
                         lambda: roadsurf.candidates(f)):
                with self.assertRaises(fast.NativeMissing):
                    call()
            with mock.patch.object(photosurface, "ON", True):
                with self.assertRaises(fast.NativeMissing):
                    photosurface.verdicts(f, lambda i: None)


if __name__ == "__main__":
    unittest.main()
