"""The road specs' stored form and its reading back (earthfast 450, roadnet::specjson: the road revamp's R2a) against
roads._spec_json / spec_from_json / _specs_from's Python: random Specs (fields of float, int, bool, str, None and numpy
scalars, sides tuples, European Streets with and without walks): the same dicts (keys, order, values with their
types) and the same JSON text; random stored lists, whole and damaged (unknown or missing fields, a street of three
values, a walk of the wrong arity, sides of a number, a wrong length): the same specs or None, and spec_from_json's
same exception class. Outside the input contract (a Spec subclass, another tuple, a field of another type, stored
data json.loads does not give): TypeError (None from _specs_from, which takes TypeError as damaged).

The Python versions are kept here only as the reference (the server has no Python twin: fast.require)."""
import json
import os
import random
import sys
import types
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import europewalks  # noqa: E402
import fast  # noqa: E402
import roads  # noqa: E402
from europewalks import Street, Walk  # noqa: E402
from roads import Spec  # noqa: E402


# ---- the Python versions (before earthfast 450), verbatim ----

_PLAIN = (float, int, bool, str, type(None))


def ref_spec_json(sp):
    out = {}
    for f, v in zip(sp._fields, sp):
        t = type(v)
        if t in _PLAIN:
            if f != "street" or v is not None:
                out[f] = v
        else:
            out[f] = v.item() if hasattr(v, "item") else list(v) if isinstance(v, tuple) else v
    return out


def ref_spec_from_json(data):
    from europewalks import Street

    return Spec(**{**data, "sides": tuple(data["sides"]) if data.get("sides") is not None else None,
                   "street": Street.from_json(data["street"]) if data.get("street") is not None else None})


def ref_specs_from(data, n):
    if not isinstance(data, list) or len(data) != n:
        return None
    try:
        return [None if d is None else ref_spec_from_json(d) for d in data]
    except (TypeError, KeyError, ValueError):
        return None


def num(rng):
    return rng.choice([6.5, 7.0, np.float32(7.5), np.float64(5.2), 9, np.int64(4), None, 0.0, -0.0, 1e300])


def walk(rng):
    return Walk(rng.choice([1.5, 2.0, np.float64(2.5)]), rng.choice([0.0, 0.5]), rng.choice([0.15, 0.0]),
                rng.choice(["asphalt", "paving_stones", np.str_("sett")]))


def spec(rng):
    if rng.random() < 0.1:
        return None
    street = None
    if rng.random() < 0.3:
        street = Street(walk(rng) if rng.random() < 0.7 else None, walk(rng) if rng.random() < 0.7 else None,
                        rng.choice([0.0, 2.0, 2.25]), rng.choice([True, False]))
    return Spec(num(rng), rng.choice(["paved", "gravel", np.str_("dirt")]), rng.choice([True, False, np.bool_(False)]),
                rng.choice(["residential", "motorway"]), rng.choice([True, False]), walk=num(rng),
                dir=rng.choice([0, 1, -1, np.int64(1)]), kmh=num(rng), gap=num(rng), median=num(rng),
                lanes=rng.choice([None, 1, 2, np.int64(3)]), surf=rng.choice([None, "asphalt", np.str_("sett")]),
                link=rng.choice([False, True]), sides=rng.choice([None, (1, -1), (1,), ()]),
                parking=rng.choice([True, False]), mountain=rng.choice([False, True]),
                france=rng.choice([False, True]), state=rng.choice([None, "CA", np.str_("TX")]), street=street)


def same(a, b):
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return list(a) == list(b) and all(same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, float):
        return repr(a) == repr(b)
    return a == b


def outcome(fn, *args):
    try:
        return "ok", fn(*args)
    except Exception as e:      # noqa: BLE001 (the class is the point)
        return "raised", type(e)


def damage(rng, d):
    d = dict(d)
    what = rng.choice(["unknown", "missing", "street3", "walk", "sides", "street_str", "none", "fine"])
    if what == "unknown":
        d["colour"] = "red"
    elif what == "missing":
        d.pop(rng.choice(["width", "kind", "urban", "cls", "oneway"]), None)
    elif what == "street3":
        d["street"] = [None, None, 0.0]
    elif what == "walk":
        d["street"] = [[1.0, 2.0], None, 0.0, False]
    elif what == "sides":
        d["sides"] = 3
    elif what == "street_str":
        d["street"] = "abcd"
    elif what == "none":
        d.pop("street", None)
        d["sides"] = None
    return d


class SpecJsonParityTest(unittest.TestCase):
    def test_store(self):
        rng = random.Random(450)
        streets = 0
        for k in range(3000):
            specs = [spec(rng) for _ in range(rng.randint(0, 12))]
            want = [None if sp is None else ref_spec_json(sp) for sp in specs]
            got = roads._specs_json(specs)
            self.assertTrue(same(got, want), k)
            self.assertEqual(json.dumps(got), json.dumps(want), k)
            for sp in specs:
                if sp is not None:
                    self.assertTrue(same(roads._spec_json(sp), ref_spec_json(sp)), k)
                    streets += sp.street is not None
        self.assertGreater(streets, 3000)

    def test_load(self):
        rng = random.Random(4501)
        none = ok = 0
        for k in range(3000):
            specs = [spec(rng) for _ in range(rng.randint(0, 10))]
            data = json.loads(json.dumps([None if sp is None else ref_spec_json(sp) for sp in specs]))
            if rng.random() < 0.4 and data:
                j = rng.randrange(len(data))
                if data[j] is not None:
                    data[j] = damage(rng, data[j])
            n = len(data) if rng.random() < 0.9 else len(data) + 1
            want = ref_specs_from(data, n)
            got = roads._specs_from(data, n)
            self.assertTrue(same(got, want), (k, got, want))
            none += want is None
            ok += want is not None
            for d in data:
                if d is not None:
                    w, g = outcome(ref_spec_from_json, d), outcome(roads.spec_from_json, d)
                    self.assertEqual(g[0], w[0], (k, d))
                    if w[0] == "ok":
                        self.assertTrue(same(g[1], w[1]), k)
                    else:
                        self.assertIs(g[1], w[1], (k, d))
        self.assertGreater(none, 300)
        self.assertGreater(ok, 1500)


class ContractTest(unittest.TestCase):
    def test_refused(self):
        class SubSpec(Spec):
            pass

        class F(float):
            pass
        good = Spec(6.0, "paved", True, "residential", False)
        for bad in (SubSpec(*good), tuple(good), good._replace(width=F(6.0)), good._replace(sides=[1, -1]),
                    good._replace(state=object())):
            self.assertEqual(outcome(roads._specs_json, [bad]), ("raised", TypeError), bad)
        self.assertEqual(outcome(roads._specs_json, (good,))[0], "ok")
        d = roads._spec_json(good)
        for bad in ({**d, "width": object()}, {**d, "sides": (1, -1)}, types.MappingProxyType(d)):
            self.assertIsNone(roads._specs_from([bad], 1))
            self.assertEqual(outcome(roads.spec_from_json, bad), ("raised", TypeError))


class DeepJsonTest(unittest.TestCase):
    """Stored values json.loads gives at any depth are read as the Python read them (Codex on 7d348090: a depth cut-off
    refused them); a malformed street beside them raises the Python's class."""

    def test_nested_values(self):
        good = roads._spec_json(Spec(6.0, "paved", True, "residential", False))
        for d in ({**good, "width": [[[[0]]]]}, {**good, "state": {"a": [{"b": [1, [2, [3]]]}]}},
                  {**good, "width": [[[[0]]]], "street": []}, {**good, "sides": [[1, [2]]]}):
            d = json.loads(json.dumps(d))
            w, g = outcome(ref_spec_from_json, d), outcome(roads.spec_from_json, d)
            self.assertEqual(g[0], w[0], d)
            if w[0] == "ok":
                self.assertTrue(same(g[1], w[1]), d)
            else:
                self.assertIs(g[1], w[1], d)
            self.assertTrue(same(roads._specs_from([d], 1), ref_specs_from([d], 1)), d)

    def test_cycles_refused(self):
        good = roads._spec_json(Spec(6.0, "paved", True, "residential", False))
        loop = []
        loop.append(loop)
        self.assertEqual(outcome(roads.spec_from_json, {**good, "state": loop}), ("raised", TypeError))


class VersionSkewTest(unittest.TestCase):
    def test_a_449_extension_is_refused(self):
        self.assertGreaterEqual(fast.MIN_VERSION, 450)
        self.assertGreaterEqual(fast._ef.VERSION, fast.MIN_VERSION)
        good = Spec(6.0, "paved", True, "residential", False)
        with mock.patch.object(fast, "_ef", types.SimpleNamespace(VERSION=449)):
            with self.assertRaises(fast.NativeMissing):
                roads._specs_json([good])
            with self.assertRaises(fast.NativeMissing):
                roads._specs_from([{"width": 6.0}], 1)


if __name__ == "__main__":
    unittest.main()
