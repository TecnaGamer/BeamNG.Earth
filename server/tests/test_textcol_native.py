"""mapgeom.text_column in Rust (earthfast 454, textcol: the full Rust port) against the Python it replaced: random
value lists (text of every width and script, emoji, lone surrogates, NUL characters, None, numbers, numpy scalars,
str subclasses with their own __str__, generators): the same array, dtype, shape and bytes; str() raising: the same
exception.

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
import mapgeom  # noqa: E402


def ref_text_column(values):
    """mapgeom.text_column before earthfast 454, verbatim."""
    v = ["" if x is None else str(x) for x in values]
    width = max([1] + [len(x) for x in v])
    return np.array(v, dtype=f"<U{width}")


class Shouty(str):
    def __str__(self):
        return self.upper() + "!"


class Raising:
    def __str__(self):
        raise ArithmeticError("no text")


VALUES = ["", "a", "residential", "Rue de l'Église", "Ŝtrato", "大街", "😀 road", "a" * 60, "\ud800x", "x\udfff",
          "nul\x00", "\x00", None, None, 3, 2.5, -0.0, float("nan"), True, np.str_("np"), np.float32(1.5),
          np.int64(-7), Shouty("abc"), b"bytes", ("t", 1)]


def same(a, b):
    return a.dtype == b.dtype and a.shape == b.shape and a.tobytes() == b.tobytes()


class TextColumnParityTest(unittest.TestCase):
    def test_random_lists(self):
        rng = random.Random(454)
        for k in range(5000):
            vals = [rng.choice(VALUES) for _ in range(rng.randint(0, 20))]
            want = ref_text_column(vals)
            got = mapgeom.text_column(vals)
            self.assertTrue(same(got, want), (k, vals, got, want))
            self.assertTrue(got.flags.c_contiguous and got.flags.owndata and got.flags.writeable, k)
            gen = mapgeom.text_column(v for v in vals)
            self.assertTrue(same(gen, want), k)
            self.assertTrue(same(mapgeom.text_column(tuple(vals)), want), k)

    def test_raising_str(self):
        for vals in ([Raising()], ["a", Raising(), "b"]):
            with self.assertRaises(ArithmeticError):
                ref_text_column(vals)
            with self.assertRaises(ArithmeticError):
                mapgeom.text_column(vals)

    def test_str_returning_a_subclass(self):
        """__str__ may return a str subclass: the Python's own len() (a lying one too) and numpy's conversion of it;
        every str() before any len()."""
        calls = []

        class Long(str):
            def __len__(self):
                calls.append("len")
                return 6

        class Short(str):
            def __len__(self):
                return 1

        class Makes:
            def __init__(self, cls, text):
                self.cls, self.text = cls, text

            def __str__(self):
                calls.append("str")
                return self.cls(self.text)
        for vals in ([Makes(Long, "x")], [Makes(Short, "wxyz"), "ab"], ["abc", None, Makes(Long, "é")],
                     [Makes(Long, "a"), Makes(Long, "b")]):
            calls.clear()
            want = ref_text_column(vals)
            ref_calls = list(calls)
            calls.clear()
            got = mapgeom.text_column(vals)
            self.assertTrue(same(got, want), (vals, got, want))
            self.assertEqual(calls, ref_calls)

    def test_arrays_in(self):
        for a in (np.array(["a", "bb", ""]), np.array([1.5, np.nan]), np.array(["x", None, 3], object), np.zeros(0)):
            self.assertTrue(same(mapgeom.text_column(a), ref_text_column(a)))


class VersionSkewTest(unittest.TestCase):
    def test_a_453_extension_is_refused(self):
        self.assertGreaterEqual(fast.MIN_VERSION, 454)
        with mock.patch.object(fast, "_ef", types.SimpleNamespace(VERSION=453)):
            with self.assertRaises(fast.NativeMissing):
                mapgeom.text_column(["a"])


if __name__ == "__main__":
    unittest.main()
