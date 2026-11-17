"""Road surfaces from the map's values (earthfast 380, roadnet::surfaces: the road revamp's R2a) against the Python they
replaced, bit for bit: roads.surface_token, surface_kind, _map_surface_values and _spec_inputs' surface columns (kinds,
tokens in order of first appearance, the aerial's verdicts, the regional rule's candidates, Sweden's tags, parking
aisles) over values of every shape a decoded layer holds: cased, padded (Python's whitespace: U+001C-U+001F, NBSP,
ideographic space), "a;b" / "a:b" details, non-ASCII ones whose lower case is CPython's own (sigma, dotted I, German
sharp s, title-case digraphs), numbers and numpy scalars, None, NaN, "", "None", "nan".

The Python versions are kept here only as the reference (the server has no Python twin: fast.require)."""
import os
import random
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import mapgeom  # noqa: E402
import roads  # noqa: E402
from roads import COBBLE, COBBLE_ALIASES, GRAVEL, PAVED, REGIONAL_CLASSES, STYLE, UNPAVED  # noqa: E402

_KINDS = ("paved", "cobble", "gravel", "dirt")
_KIND_CODE = {k: c for c, k in enumerate(_KINDS)}
UNPAVED_VERDICT, PAVED_VERDICT = 2, 1


# ---- the Python versions (before earthfast 380), verbatim ----

def ref_surface_token(surface):
    if surface is None:
        return None
    return str(surface).lower().split(";")[0].split(":")[0].strip()


def ref_surface_kind(cls, surface):
    if surface is not None:
        s = ref_surface_token(surface)
        if s in PAVED:
            return "paved"
        if s in COBBLE or s in COBBLE_ALIASES:
            return "cobble"
        if s in GRAVEL:
            return "gravel"
        if s == "unpaved" and cls != "track":
            return "gravel"
        if s in UNPAVED:
            return "dirt"
    return "paved" if STYLE.get(cls, STYLE["unknown"])[1] else "dirt"


def ref_map_surface_values(feats):
    n = len(feats)
    out = [None] * n
    osm = feats.attrs.get("osm_surface")
    if osm is not None:
        for i, v in enumerate(osm.tolist()[:n] if hasattr(osm, "tolist") else list(osm)[:n]):
            s = str(v).strip()
            if s and s.lower() not in ("nan", "none"):
                out[i] = s
    t = feats.rules.get("surface")
    if t is not None and len(t.feature) and n:
        maj = t.majors(n)
        for i in np.flatnonzero(maj >= 0).tolist():
            if out[i] is None:
                out[i] = t.value[maj[i]]
    return out


def ref_surfaces(cls_s, vals, verdicts, osm_lanes, good, regional_on, se, stags, sub_table):
    """_spec_inputs' surface block as it was (from the aerial's verdicts to the parking aisles)."""
    n = len(cls_s)
    vals = list(vals)
    photo_paved = np.zeros(n, bool)
    for i, v in verdicts.items():
        if vals[i] is not None:
            continue
        if v == UNPAVED_VERDICT:
            if cls_s[i] != "track":
                vals[i] = "gravel"
        else:
            photo_paved[i] = True
            if cls_s[i] == "track":
                vals[i] = "asphalt"
    tokens, tok_of, kind_of = [], {}, {}

    def token(v):
        if v is None:
            return -1
        t = ref_surface_token(v)
        k = tok_of.get(t)
        if k is None:
            k = tok_of[t] = len(tokens)
            tokens.append(t)
        return k

    def kind(c, v):
        k = kind_of.get((c, v))
        if k is None:
            k = kind_of[(c, v)] = _KIND_CODE[ref_surface_kind(c, v)]
        return k
    skind = np.fromiter((kind(c, v) for c, v in zip(cls_s, vals)), np.int8, n)
    stok = np.fromiter((token(v) for v in vals), np.int32, n)
    reg_pre = (np.fromiter((regional_on and v is None and c in REGIONAL_CLASSES for c, v in zip(cls_s, vals)), bool, n)
               & np.isnan(osm_lanes) & ~good & ~photo_paved)
    se_tag, se_kind, se_tok = np.zeros(n, bool), np.zeros(n, np.int8), np.full(n, -1, np.int32)
    aisle = np.zeros(n, bool)
    if se.any():
        for j in np.flatnonzero(se).tolist():
            tag = None if stags is None or stags[j] is None else str(stags[j])
            if tag not in (None, "", "None", "nan"):
                se_tag[j], se_kind[j], se_tok[j] = True, kind(cls_s[j], tag), token(tag)
        t = sub_table
        if t is not None and len(t.feature):
            maj = t.majors(n)
            for j in np.flatnonzero(se & (maj >= 0)).tolist():
                aisle[j] = t.value[maj[j]] in ("parking_aisle",)
    return {"skind": skind, "stok": stok, "reg_pre": reg_pre, "se_tag": se_tag, "se_kind": se_kind, "se_tok": se_tok,
            "aisle": aisle, "tokens": tokens}


# ---- values ----

WORDS = sorted(PAVED | COBBLE | COBBLE_ALIASES | GRAVEL | UNPAVED) + ["Asphalt", "ASPHALT", " asphalt ", "asphalt;sett",
         "sett;asphalt", "paving_stones:30", "cobblestone:flattened", "Unpaved", "unpaved", "UNPAVED:x", "grass_paver",
         "\x1cgravel\x1f", "　dirt ", " sand", "metal_grid", "", ";", ":", " ; ", "None", "none", "nan",
         "NaN", "NONE", "İzmir", "ΣΑΣ", "ΟΔΟΣ:Σ", "Straße", "STRASSE", "ǅ", "ﬀ", "Kelvin", "Ⅻ", "ǈ;x"]
OTHER = [None, float("nan"), 3, 3.5, True, np.float64(2.0), np.str_("Asphalt"), np.str_(" gravel")]
CLASSES = list(STYLE) + ["track", "track", "unknown", "footway", "Track", ""]


def _value(rng):
    return rng.choice(WORDS) if rng.random() < 0.85 else rng.choice(OTHER)


class SurfacesParityTest(unittest.TestCase):
    def test_token_and_kind(self):
        rng = random.Random(376)
        for v in WORDS + OTHER:
            self.assertEqual(roads.surface_token(v), ref_surface_token(v), repr(v))
            for c in CLASSES + [None]:
                self.assertEqual(roads.surface_kind(c, v), ref_surface_kind(c, v), (c, v))
        for _ in range(3000):
            v = "".join(rng.choice(["a", "S", "Σ", ";", ":", " ", "\x1d", "İ", "ß", "_", "x", " ", "Ǆ", "ς"])
                        for _ in range(rng.randint(0, 8)))
            self.assertEqual(roads.surface_token(v), ref_surface_token(v), repr(v))
            self.assertEqual(roads.surface_kind("residential", v), ref_surface_kind("residential", v), repr(v))

    def test_map_surface_values(self):
        rng = random.Random(3761)
        for k in range(1500):
            n = rng.randint(0, 10)
            attrs, rules = {}, {}
            if rng.random() < 0.8:
                vals = [_value(rng) for _ in range(n + rng.randint(0, 2))]
                attrs["osm_surface"] = np.array(vals + [None], object)[:len(vals)] if rng.random() < 0.6 else \
                    np.array([str(v) for v in vals], str)
            if rng.random() < 0.7:
                m = rng.randint(0, 2 * n)
                rules["surface"] = mapgeom.RuleTable(
                    np.array([rng.randrange(0, n + 1) for _ in range(m)], np.int32),
                    np.array([rng.choice(WORDS[:40]) for _ in range(m)], str),
                    np.array([rng.choice([0.0, 0.5]) for _ in range(m)], np.float32),
                    np.array([rng.choice([0.5, 1.0]) for _ in range(m)], np.float32))
            f = mapgeom.Features("roads", np.array([f"r{i}" for i in range(n)]), np.full(n, 2, np.uint8),
                                 np.arange(n + 1, dtype=np.int64), np.arange(n + 1, dtype=np.int64),
                                 np.zeros(n + 1, np.int64), np.zeros((0, 2)), attrs=attrs, rules=rules)
            want = ref_map_surface_values(f)
            got = roads._map_surface_values(f)
            self.assertEqual(len(got), len(want), k)
            for g, w in zip(got, want):
                self.assertIs(type(g), type(w), k)
                self.assertEqual(g, w, k)

    def test_lane_column_of_other_length(self):
        """The OSM lanes column shorter than the layer (_osm_values gives its first n values): one value broadcasts as
        numpy's, another length is numpy's ValueError; never a panic (Codex review of 3eba68d9)."""
        rules = roads._surface_rules()
        rng = random.Random(3781)
        for k in range(200):
            n = rng.randint(1, 6)
            cls_s = [rng.choice(CLASSES) for _ in range(n)]
            vals = [None if rng.random() < 0.7 else _value(rng) for _ in range(n)]
            lanes = np.array([rng.choice([np.nan, 2.0]) for _ in range(rng.choice([0, 1, n - 1, n, n + 2]))])
            good = np.zeros(n, bool)
            try:
                want = ("ok", ref_surfaces(cls_s, vals, {}, lanes, good, True, np.zeros(n, bool), None, None)["reg_pre"])
            except Exception as e:      # noqa: BLE001
                want = ("raised", type(e))
            try:
                got = ("ok", rules.spec_surfaces(cls_s, list(vals), [], UNPAVED_VERDICT, lanes, good, True,
                                                 sorted(REGIONAL_CLASSES))["reg_pre"])
            except Exception as e:      # noqa: BLE001
                got = ("raised", type(e))
            except BaseException as e:  # noqa: BLE001
                self.fail(f"{type(e).__name__}: {e}")
            self.assertEqual(got[0], want[0], (k, n, len(lanes)))
            if want[0] == "raised":
                self.assertIs(got[1], want[1], (k, n, len(lanes)))
            else:
                self.assertTrue(np.array_equal(got[1], want[1]), (k, n, len(lanes)))

    def test_spec_surface_columns(self):
        rng = random.Random(3762)
        rules = roads._surface_rules()
        for k in range(2000):
            n = rng.randint(0, 14)
            cls_s = [rng.choice(CLASSES) for _ in range(n)]
            vals = [_value(rng) if rng.random() < 0.6 else None for _ in range(n)]
            verdicts = {i: rng.choice([UNPAVED_VERDICT, PAVED_VERDICT, 3]) for i in range(n) if rng.random() < 0.3}
            osm_lanes = np.array([rng.choice([np.nan, 2.0]) for _ in range(n)])
            good = np.array([rng.random() < 0.2 for _ in range(n)], bool)
            regional_on = rng.random() < 0.8
            se = np.array([rng.random() < 0.5 for _ in range(n)], bool) if rng.random() < 0.5 else np.zeros(n, bool)
            stags = None
            if rng.random() < 0.8:
                tv = [_value(rng) for _ in range(n)]
                stags = np.array(tv + [None], object)[:n] if rng.random() < 0.5 else np.array([str(v) for v in tv], str)
            sub = None
            if rng.random() < 0.7:
                m = rng.randint(0, 2 * n)
                sv = [rng.choice(["parking_aisle", "link", "driveway", 3.0]) for _ in range(m)]
                sub = mapgeom.RuleTable(np.array([rng.randrange(0, n + 1) for _ in range(m)], np.int32),
                                        np.array(sv + [None], object)[:m] if rng.random() < 0.5 else
                                        np.array([str(v) for v in sv], str),
                                        np.zeros(m, np.float32), np.array([rng.random() for _ in range(m)], np.float32))
            want = ref_surfaces(cls_s, vals, verdicts, osm_lanes, good, regional_on, se, stags, sub)
            sf = rules.spec_surfaces(cls_s, list(vals), sorted(verdicts.items()), UNPAVED_VERDICT, osm_lanes, good,
                                     regional_on, sorted(REGIONAL_CLASSES))
            se_tag, se_kind, se_tok, tokens = rules.sweden_surfaces(cls_s, se, stags, sf["tokens"])
            got = {"skind": sf["skind"], "stok": sf["stok"], "reg_pre": sf["reg_pre"], "se_tag": se_tag,
                   "se_kind": se_kind, "se_tok": se_tok, "aisle": fast._ef.aisles(se, sub), "tokens": tokens}
            for key, w in want.items():
                g = got[key]
                if key == "tokens":
                    self.assertEqual(g, w, k)
                    continue
                self.assertEqual(g.dtype, w.dtype, (k, key))
                self.assertTrue(np.array_equal(g, w), (k, key, g, w))


if __name__ == "__main__":
    unittest.main()
