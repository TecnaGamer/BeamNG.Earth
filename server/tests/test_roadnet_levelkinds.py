"""_bridge_specs' "has a range of that kind" mask (earthfast 402, roadnet::levelkinds: the road revamp's R2a) against
its Python loop, value for value: random level lists (a dict of _level_lists' or a list of _raw_all's) of (a, b, kind)
ranges, kinds of str, numpy str_, None, other objects; eligible roads in any order, repeated, negative; and malformed
input: a missing road, a range of 2 or 4 values or none at all, a str of three characters as a range, a kind whose ==
raises or gives an array, before and after the first match (any() stops there); a road past the mask. The same mask, the
same exception class, the same == calls in the same order.

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
import roads  # noqa: E402


def ref_has(lv, elig, want, n):
    """_bridge_specs' loop (before earthfast 402), verbatim."""
    has = np.zeros(n, bool)
    for i in elig:
        has[i] = any(k == want for _, _, k in lv[i])
    return has


LOG = []


class Kind:
    """A kind that records its == (and may raise, or answer with an array)."""

    def __init__(self, name, mode="ok"):
        self.name, self.mode = name, mode

    def __eq__(self, other):
        LOG.append(("eq", self.name, other))
        if self.mode == "raise":
            raise ArithmeticError("kind")
        if self.mode == "array":
            return np.array([True, False])
        if self.mode == "truthy":
            return 1
        if self.mode == "stop":
            raise StopIteration("eq")
        if self.mode == "boolstop":
            return BoolStop()
        return self.name == other

    __hash__ = None


class BoolStop:
    """An == answer whose truth raises StopIteration (any() takes it outside the generator: not a RuntimeError)."""

    def __bool__(self):
        LOG.append("bool")
        raise StopIteration("bool")


class IterStop:
    """A range (or a road's range list) whose iterator cannot be made: StopIteration from __iter__."""

    def __iter__(self):
        LOG.append("iter")
        raise StopIteration("iter")


class Short:
    """A range whose iterator stops after `k` values (StopIteration from __next__: an exhausted iterator)."""

    def __init__(self, k):
        self.k = k

    def __iter__(self):
        return iter([0.0, 1.0, "bridge", 9][:self.k])


def kind(rng):
    return rng.choice(["bridge", "tunnel", "bridge", "tunnel", np.str_("bridge"), np.str_("tunnel"), None, 3,
                       "Bridge", Kind("bridge"), Kind("tunnel"), Kind("x", "truthy")])


def ranges(rng, bad):
    out = []
    for _ in range(rng.choice([0, 0, 1, 1, 2, 3, 4])):
        r = (rng.random(), rng.random(), kind(rng))
        if bad and rng.random() < 0.15:
            r = rng.choice([(0.1, "bridge"), (0.1, 0.2, "bridge", 4), "abc", "xyz", 5, None,
                            (0.1, 0.2, Kind("raise", "raise")), (0.0, 1.0, Kind("arr", "array")),
                            [0.2, 0.3, "bridge"], np.array([0.1, 0.2, 0.3]), (0.1, 0.2, Kind("stop", "stop")),
                            (0.1, 0.2, Kind("boolstop", "boolstop")), IterStop(), Short(2), Short(3), Short(4)])
        out.append(r)
    if bad and rng.random() < 0.03:
        return IterStop()               # (lv[i]'s iterator, made outside the generator: StopIteration as it is)
    return rng.choice([out, tuple(out), out])


def layer(rng, bad):
    n = rng.randint(0, 9)
    lv = [ranges(rng, bad) for _ in range(n)]
    if rng.random() < 0.5:
        lv = dict(enumerate(lv))
        if bad and lv and rng.random() < 0.2:
            del lv[rng.randrange(n)]
    elif bad and lv and rng.random() < 0.1:
        lv = lv[:-1]
    elig = [i for i in range(n) if rng.random() < 0.7]
    if rng.random() < 0.3:
        rng.shuffle(elig)
    if bad and rng.random() < 0.2:
        elig.append(rng.choice([-1, -n - 1 if n else -1, n, n + 2, 0]))
    m = n
    if bad and rng.random() < 0.15:
        m = max(0, n - rng.randint(1, 3))
    return lv, elig, m


def outcome(fn, *args):
    del LOG[:]
    try:
        got = ("ok", fn(*args))
    except Exception as e:      # noqa: BLE001 (the class is the point)
        got = ("raised", (type(e), type(e.__cause__)))
    return got, list(LOG)


def port(lv, elig, want, n):
    return roads._has_kind(lv, elig, want, n)


class LevelKindsParityTest(unittest.TestCase):
    def check(self, seed, count, bad):
        rng = random.Random(seed)
        tally = {"ok": 0, "raised": 0, "true": 0}
        for k in range(count):
            lv, elig, n = layer(rng, bad)
            want = rng.choice(["bridge", "tunnel"])
            want_got, want_log = outcome(ref_has, lv, elig, want, n)
            got, log = outcome(port, lv, elig, want, n)
            self.assertEqual(got[0], want_got[0], (k, lv, elig, n, got, want_got))
            if got[0] == "ok":
                self.assertEqual(got[1].dtype, np.bool_)
                self.assertEqual(got[1].shape, (n,))
                self.assertEqual(got[1].tolist(), want_got[1].tolist(), (k, lv, elig))
                tally["true"] += int(want_got[1].sum())
            else:
                self.assertEqual(got[1], want_got[1], (k, lv, elig, n))
                tally[got[1]] = tally.get(got[1], 0) + 1
            self.assertEqual(log, want_log, k)
            tally[got[0]] += 1
        return tally

    def test_random_lists(self):
        t = self.check(402, 6000, False)
        self.assertEqual(t["raised"], 0)
        self.assertGreater(t["true"], 3000)

    def test_malformed_lists_raise_alike(self):
        t = self.check(4021, 6000, True)
        self.assertGreater(t["raised"], 1000)
        self.assertGreater(t["ok"], 1000)
        # (StopIteration from == or a range's unpacking: the generator's RuntimeError, caused by it; from lv[i]'s own
        # iterator or the answer's truth, outside the generator: StopIteration itself; Codex on 27762111)
        self.assertGreater(t.get((RuntimeError, StopIteration), 0), 20)
        self.assertGreater(t.get((StopIteration, type(None)), 0), 20)


class VersionSkewTest(unittest.TestCase):
    """An extension of 401 (chunk 4's) has no level_kind_mask: refused at the start (fast.MIN_VERSION) and by the
    facade (fast.require: NativeMissing), never an AttributeError at the first bridge build."""

    def test_a_401_extension_is_refused(self):
        self.assertGreaterEqual(fast.MIN_VERSION, 402)
        self.assertGreaterEqual(fast._ef.VERSION, fast.MIN_VERSION)
        with mock.patch.object(fast, "_ef", types.SimpleNamespace(VERSION=401)):
            with self.assertRaises(fast.NativeMissing):
                roads._has_kind({0: []}, [0], "bridge", 1)


if __name__ == "__main__":
    unittest.main()
