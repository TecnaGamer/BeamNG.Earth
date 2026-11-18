"""dnp.matmul and dnp.dot in Rust (earthfast 472, dmmatmul: the full Rust port) against the numpy arithmetic they
replaced: random operands (1-D, 2-D and stacked, k from 0 past numpy's 128-element pairwise blocks, broadcast stacks,
strided / reversed / unaligned views, float64 and float32 and their mixes with integers, signed zeros, infinities,
NaNs, values that overflow; C, Fortran, transposed, sliced, reversed and broadcast layouts, which decide the order of
the Python's k > 8 sum; empty results and results too big) give the same dtype, shape, type (a numpy scalar for two
vectors) and bytes; numpy's
floating-point errors are reported as before (np.errstate "raise" raises the same FloatingPointError, "ignore" stays
quiet); misaligned shapes and scalar operands raise as before; the input contract refuses what the kernel does not
take.

The Python versions are kept here only as the reference (the server has no Python twin: fast.require)."""
import os
import random
import sys
import types
import unittest
import warnings
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dnp  # noqa: E402
import fast  # noqa: E402

# The numpy build the kernel's two-NaN rule was fitted to (numpy 2.5.4's x86-64 wheel for CPython 3.14). numpy's
# wheels for other Pythons are compiled apart and keep the other of two different NaNs in places (the mini PC's 3.12
# wheel, gate batch212): the old Python's NaN payload was never the same everywhere; the kernel's is.
FITTED_BUILD = (np.__version__ == "2.5.4" and sys.version_info[:2] == (3, 14)
                and os.uname().machine in ("x86_64", "AMD64") if hasattr(os, "uname") else False)


# ---- the Python versions (before earthfast 472), verbatim ----

def ref_matmul(a, b):
    """a @ b (numpy's matmul rules: 1-D operands promoted and their axis removed, stacks broadcast) as elementwise
    products summed over k in a fixed order."""
    a, b = np.asarray(a), np.asarray(b)
    if a.ndim == 0 or b.ndim == 0:
        raise ValueError("matmul: scalar operands are not allowed, use '*' instead")
    if a.dtype.kind not in "fc" and b.dtype.kind not in "fc":
        return np.matmul(a, b)          # integers: exact
    va, vb = a.ndim == 1, b.ndim == 1
    if va:
        a = a[None, :]
    if vb:
        b = b[:, None]
    k = a.shape[-1]
    if b.shape[-2] != k:
        raise ValueError(f"matmul: shapes {a.shape} and {b.shape} not aligned")
    if k == 0:
        out = np.zeros(np.broadcast_shapes(a.shape[:-1] + (1,), b.shape[:-2] + (1, b.shape[-1])),
                       np.result_type(a, b))
    elif k <= 8:
        out = a[..., :, 0:1] * b[..., 0:1, :]
        for j in range(1, k):
            out = out + a[..., :, j:j + 1] * b[..., j:j + 1, :]
    else:
        out = np.add.reduce(a[..., :, :, None] * b[..., None, :, :], axis=-2)
    if va:
        out = out[..., 0, :]
    if vb:
        out = out[..., 0]
    return out if out.ndim else out[()]


def ref_dot(a, b):
    """np.dot for operands of up to 2 axes (and an N-axis a with a 1- or 2-axis b): matmul's products, a scalar
    operand multiplies."""
    a, b = np.asarray(a), np.asarray(b)
    if a.ndim == 0 or b.ndim == 0:
        return a * b
    if b.ndim > 2:
        raise ValueError("dnp.dot: b with more than 2 axes")
    return ref_matmul(a, b)


# ---- comparison ----

def same(got, want):
    """The same type, dtype, shape and bytes (a NaN's bytes too)."""
    if type(got) is not type(want):
        return False
    g, w = np.asarray(got), np.asarray(want)
    return g.dtype == w.dtype and g.shape == w.shape and g.tobytes() == w.tobytes()


def outcome(fn, *args):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return "ok", fn(*args)
    except Exception as e:      # noqa: BLE001
        return "raised", type(e)


def values(rng, shape, dt, special):
    v = rng.standard_normal(shape) * 10.0 ** rng.uniform(-6, 6, shape)
    if special and v.size:
        flat = v.reshape(-1)
        for _ in range(rng.integers(1, 4)):
            flat[rng.integers(0, flat.size)] = rng.choice([0.0, -0.0, np.inf, -np.inf, np.nan, 1e300, -1e300, 1e-320])
    zero = rng.random(shape) < 0.1
    v = np.where(zero, rng.choice([0.0, -0.0]), v)
    with np.errstate(over="ignore"):       # (float32: the large values become infinities)
        return v.astype(dt)


def view(rng, v):
    """v as itself, a strided / reversed / transposed-copy view, or an unaligned copy (the same values)."""
    c = rng.integers(0, 4)
    if c == 1 and v.ndim:
        big = np.zeros(v.shape[:-1] + (v.shape[-1] * 2,), v.dtype)
        big[..., ::2] = v
        return big[..., ::2]
    if c == 2 and v.ndim:
        return v[..., ::-1].copy()[..., ::-1]
    if c == 3:
        raw = np.zeros(v.nbytes + 1, np.uint8)
        u = raw[1:].view(v.dtype).reshape(v.shape)
        u[...] = v
        return u
    return v


def case(rng):
    k = int(rng.choice([0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 15, 16, 17, 64, 127, 128, 129, 136, 255, 256, 300, 1000, 4100]))
    n, m = int(rng.integers(1, 5)), int(rng.integers(1, 5))
    if k > 200:
        n = m = 1
    kind = rng.integers(0, 6)
    if kind == 0:
        sa, sb = (k,), (k,)
    elif kind == 1:
        sa, sb = (n, k), (k,)
    elif kind == 2:
        sa, sb = (k,), (k, m)
    elif kind == 3:
        sa, sb = (n, k), (k, m)
    elif kind == 4:
        s = int(rng.integers(1, 4))
        sa, sb = (s, n, k), (rng.choice([1, s]), k, m) if rng.random() < 0.7 else (k, m)
        sb = tuple(int(x) for x in sb)
    else:
        sa, sb = (2, 1, n, k), (3, k, m) if rng.random() < 0.5 else (k,)
    dts = rng.choice(["f8", "f8", "f8", "f4", "f8i8", "f4i4", "i8f4", "f4f8", "bf8"])
    special = rng.random() < 0.15
    if dts in ("f8", "f4"):
        a, b = values(rng, sa, dts, special), values(rng, sb, dts, special)
    elif dts == "f8i8":
        a, b = values(rng, sa, "f8", special), rng.integers(-1000, 1000, sb)
    elif dts == "f4i4":
        a, b = values(rng, sa, "f4", special), rng.integers(-1000, 1000, sb).astype(np.int16)
    elif dts == "i8f4":
        a, b = rng.integers(-1000, 1000, sa), values(rng, sb, "f4", special)
    elif dts == "f4f8":
        a, b = values(rng, sa, "f4", special), values(rng, sb, "f8", special)
    else:
        a, b = rng.random(sa) < 0.5, values(rng, sb, "f8", special)
    return view(rng, a), view(rng, b)


class ParityTest(unittest.TestCase):
    def test_random_operands(self):
        rng = np.random.default_rng(472)
        n = 0
        for _ in range(6000):
            a, b = case(rng)
            for fn, ref in ((dnp.matmul, ref_matmul), (dnp.dot, ref_dot)):
                got, want = outcome(fn, a, b), outcome(ref, a, b)
                self.assertEqual(got[0], want[0], (a.shape, b.shape, a.dtype, b.dtype, got, want))
                if want[0] == "ok":
                    self.assertTrue(same(got[1], want[1]), (fn.__name__, a.shape, b.shape, a.dtype, b.dtype,
                                                            got[1], want[1]))
                    n += 1
                else:
                    self.assertIs(got[1], want[1])
        self.assertGreater(n, 10000)

    def test_lists_ints_and_scalars(self):
        r = random.Random(4)
        for _ in range(500):
            k = r.randint(1, 12)
            a = [r.uniform(-9, 9) for _ in range(k)]
            b = [[r.randint(-9, 9) for _ in range(2)] for _ in range(k)]
            for x, y in ((a, b), (a, a), (np.array(b), np.array([1, 2])),
                         (np.int64(3), a), (a, np.float64(2.0)), (2.0, a), (np.array(2.5), np.array(a))):
                for fn, ref in ((dnp.matmul, ref_matmul), (dnp.dot, ref_dot)):
                    got, want = outcome(fn, x, y), outcome(ref, x, y)
                    self.assertEqual(got[0], want[0])
                    if want[0] == "ok":
                        self.assertTrue(same(got[1], want[1]), (x, y, got, want))
                    else:
                        self.assertIs(got[1], want[1])

    def test_integers_stay_integers(self):
        a, b = np.arange(6).reshape(2, 3), np.arange(3)
        self.assertTrue(same(dnp.matmul(a, b), ref_matmul(a, b)))
        self.assertTrue(same(dnp.dot(a[0], b), ref_dot(a[0], b)))

    def test_signed_zero(self):
        z = np.array([-0.0])
        for k in (1, 3, 8, 9, 20):
            a, b = np.full(k, -0.0), np.full(k, 1.0)
            self.assertTrue(same(dnp.matmul(a, b), ref_matmul(a, b)), k)
            a2, b2 = np.full((2, k), -0.0), np.full((k, 3), 1.0)
            self.assertTrue(same(dnp.matmul(a2, b2), ref_matmul(a2, b2)), k)
        self.assertTrue(same(dnp.dot(z, z), ref_dot(z, z)))

    def test_errors(self):
        for a, b in ((np.zeros(3), np.zeros(4)), (np.zeros((2, 3)), np.zeros((2, 3))), (np.zeros((2, 2, 3)),
                     np.zeros((3, 3, 2))), (np.zeros(3), np.float64(1.0))):
            for fn, ref in ((dnp.matmul, ref_matmul), (dnp.dot, ref_dot)):
                got, want = outcome(fn, a, b), outcome(ref, a, b)
                self.assertEqual(got[0], want[0])
                self.assertTrue(same(got[1], want[1]) if got[0] == "ok" else got[1] is want[1])
        with self.assertRaises(ValueError) as e:
            dnp.matmul(np.zeros(3), np.zeros(4))
        with self.assertRaises(ValueError) as w:
            ref_matmul(np.zeros(3), np.zeros(4))
        self.assertEqual(str(e.exception), str(w.exception))
        with self.assertRaises(ValueError):
            dnp.dot(np.zeros(3), np.zeros((3, 3, 3)))


class NaNTest(unittest.TestCase):
    def test_two_nans_meet(self):
        """A +NaN and a -NaN (np.nan, and inf * 0's default NaN) placed anywhere in a sum: the NaN kept is the one
        numpy's compiled pairwise sum and binary loops kept (every depth of the halving, both dtypes, every path)."""
        rnd = random.Random(472)
        for dt in (np.float64, np.float32):
            for k in list(range(1, 140)) + [150, 255, 256, 257, 300, 513, 1000, 1030, 2049, 4100]:
                for _ in range(12):
                    p, q = rnd.sample(range(k), 2) if k > 1 else (0, 0)
                    a = np.ones(k, dt)
                    a[p] = np.nan
                    a[q] = -dt(np.nan) if rnd.random() < 0.5 else np.inf
                    b = np.ones(k, dt)
                    if a[q] == np.inf:
                        b[q] = 0.0
                    m = rnd.choice([1, 2, 3])
                    for x, y in ((a, b), (np.stack([a, b, a]), b), (a, np.stack([b] * m, axis=1)),
                                 (b, np.stack([a] * m, axis=1))):
                        got, want = outcome(dnp.matmul, x, y), outcome(ref_matmul, x, y)
                        if FITTED_BUILD:
                            self.assertTrue(same(got[1], want[1]), (dt, k, p, q, x.shape, y.shape))
                        else:   # (which of two different NaNs numpy keeps is its compiled build's: only NaN-ness)
                            self.assertTrue(np.array_equal(np.isnan(got[1]), np.isnan(want[1])),
                                            (dt, k, p, q, x.shape, y.shape))


class FloatingPointErrorsTest(unittest.TestCase):
    CASES = [
        (np.array([1e300, 1e300]), np.array([1e300, 1.0])),                    # multiply overflow (k <= 8)
        (np.array([1.7e308, 1.7e308]), np.array([1.0, 1.0])),                  # add overflow (k <= 8)
        (np.array([np.inf, 1.0]), np.array([0.0, 1.0])),                       # multiply invalid
        (np.array([np.inf, -np.inf]), np.array([1.0, 1.0])),                   # add invalid
        (np.array([1e300] * 12), np.array([1e300] * 12)),                      # multiply overflow (k > 8)
        (np.array([1.7e308] * 12), np.ones(12)),                               # reduce overflow (pairwise)
        (np.array([[1.7e308] * 12] * 2), np.ones((12, 2))),                    # reduce overflow (sequential)
        (np.array([np.inf, -np.inf] * 6), np.ones(12)),                        # reduce invalid
        (np.array([np.nan, 1.0]), np.array([1.0, 1.0])),                       # a NaN: no flag
        (np.array([1e30, 1e30], np.float32), np.array([1e30, 1.0], np.float32)),
    ]

    def test_raise(self):
        for a, b in self.CASES:
            with np.errstate(all="raise"):
                got, want = outcome(dnp.matmul, a, b), outcome(ref_matmul, a, b)
            self.assertEqual(got[0], want[0], (a, b))
            if got[0] == "raised":
                self.assertIs(got[1], want[1])
                with np.errstate(all="raise"):
                    try:
                        ref_matmul(a, b)
                    except FloatingPointError as e:
                        msg = str(e)
                    with self.assertRaises(FloatingPointError) as c:
                        dnp.matmul(a, b)
                self.assertEqual(str(c.exception), msg, (a, b))

    def test_warn_and_ignore(self):
        for a, b in self.CASES:
            def caught(fn):
                with warnings.catch_warnings(record=True) as w:
                    warnings.simplefilter("always")
                    with np.errstate(all="warn", under="ignore"):
                        r = fn(a, b)
                return r, sorted({str(x.message) for x in w})
            (g, gw), (r, rw) = caught(dnp.matmul), caught(ref_matmul)
            self.assertTrue(same(g, r))
            self.assertEqual(gw, rw, (a, b))
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                with np.errstate(all="ignore"):
                    self.assertTrue(same(dnp.matmul(a, b), r))


def layout(rng, v):
    """v's values in another memory layout: C, Fortran, a transposed copy viewed back, sliced (steps on any axes of a
    C or Fortran block), reversed axes, all of those mixed, or one row broadcast (stride 0: its values repeat)."""
    nd = v.ndim
    c = rng.integers(0, 7)
    if c == 0:
        return v.copy()
    if c == 1:
        return np.asfortranarray(v)
    if c == 2:
        p = rng.permutation(nd)
        return np.ascontiguousarray(v.transpose(p)).transpose(np.argsort(p))
    if c == 3:
        steps = [int(rng.choice([1, 2, 3])) for _ in range(nd)]
        big = np.zeros(tuple(s * t for s, t in zip(v.shape, steps)), v.dtype)
        if rng.random() < 0.5:
            big = np.asfortranarray(big)
        sl = tuple(slice(None, None, t) for t in steps)
        big[sl] = v
        return big[sl]
    if c == 4:
        flips = tuple(slice(None, None, -1) if rng.random() < 0.5 else slice(None) for _ in range(nd))
        base = np.asfortranarray(v[flips]) if rng.random() < 0.5 else v[flips].copy()
        return base[flips]
    if c == 5:
        p = rng.permutation(nd)
        w = np.ascontiguousarray(v.transpose(p)).transpose(np.argsort(p))
        flips = tuple(slice(None, None, -1) if rng.random() < 0.5 else slice(None) for _ in range(nd))
        w = w[flips].copy() if rng.random() < 0.5 else np.asfortranarray(w[flips])
        return w[flips]
    ax = int(rng.integers(0, nd))
    return np.broadcast_to(np.take(v, [0], axis=ax), v.shape)


def layout_case(rng):
    k = int(rng.choice([2, 5, 8, 9, 10, 12, 16, 17, 33, 64, 129, 136, 200, 300]))

    def d():
        return int(rng.choice([1, 1, 2, 3, 4]))
    n, m = d(), d()
    sa = [(k,), (n, k), (d(), n, k), (d(), 1, n, k)][rng.integers(0, 4)]
    c = rng.integers(0, 4)
    if c == 0:
        sb = (k,)
    elif c == 1:
        sb = (k, m)
    elif c == 2:
        sb = (sa[0] if len(sa) == 3 and rng.random() < 0.7 else d(), k, m)
    else:
        sb = (d(), k, m)
    dts = rng.choice(["f8", "f8", "f8", "f4", "f8i8", "i4f4", "f4f8"])

    def vals(shape, dt):
        if dt[0] == "i":
            return rng.integers(-1000, 1000, shape).astype(dt)
        v = rng.standard_normal(shape) * 10.0 ** rng.integers(-10, 17, shape)     # sums that cancel: order shows
        if rng.random() < 0.1 and v.size:
            v.reshape(-1)[rng.integers(0, v.size)] = rng.choice([np.inf, -np.inf, np.nan, -np.nan, -0.0, 1e300])
        with np.errstate(over="ignore"):
            return v.astype(dt)
    da, db = {"f8": ("f8", "f8"), "f4": ("f4", "f4"), "f8i8": ("f8", "i8"), "i4f4": ("i4", "f4"),
              "f4f8": ("f4", "f8")}[dts]
    return layout(rng, vals(sa, da)), layout(rng, vals(sb, db))


class LayoutTest(unittest.TestCase):
    """The Python's k > 8 sum (np.add.reduce over the products) ran its loops in the operands' memory order: k the
    inner loop (a pairwise sum) or an outer one (left to right), whichever numpy's iterator chose (Codex review of
    r2a6: the kernel had chosen by m alone)."""

    def test_codex_cancellations(self):
        p = np.array([1e16, 1, -1e16, 1, 0, 0, 0, 0, 0])
        for a, b in ((np.asfortranarray(np.stack([p, p])), np.ones(9)),        # reference [1, 1] (left to right)
                     (p, np.asfortranarray(np.ones((9, 2)))),                   # reference [0, 0] (pairwise)
                     (np.stack([p, p]), np.ones(9)), (p, np.ones((9, 2))),
                     (np.stack([p, p]).T.copy().T, np.ones((9, 1))), (p, np.ones((2, 9)).T)):
            for fn, ref in ((dnp.matmul, ref_matmul), (dnp.dot, ref_dot)):
                self.assertTrue(same(fn(a, b), ref(a, b)), (fn.__name__, a.strides, b.strides, fn(a, b), ref(a, b)))
        self.assertEqual(list(ref_matmul(np.asfortranarray(np.stack([p, p])), np.ones(9))), [1.0, 1.0])
        self.assertEqual(list(ref_matmul(p, np.asfortranarray(np.ones((9, 2))))), [0.0, 0.0])

    def test_random_layouts(self):
        rng = np.random.default_rng(4721)
        n = differs = 0
        for _ in range(6000):
            a, b = layout_case(rng)
            for fn, ref in ((dnp.matmul, ref_matmul), (dnp.dot, ref_dot)):
                got, want = outcome(fn, a, b), outcome(ref, a, b)
                self.assertEqual(got[0], want[0], (a.shape, b.shape, a.strides, b.strides, got, want))
                if want[0] == "ok":
                    self.assertTrue(same(got[1], want[1]), (fn.__name__, a.shape, b.shape, a.dtype, b.dtype,
                                                            a.strides, b.strides, got[1], want[1]))
                    n += 1
                else:
                    self.assertIs(got[1], want[1])
            c = outcome(ref_matmul, np.ascontiguousarray(a), np.ascontiguousarray(b))
            w = outcome(ref_matmul, a, b)
            if w[0] == "ok" and c[0] == "ok" and not same(c[1], w[1]):
                differs += 1            # the layout changed the Python's own result: the order is exercised
        self.assertGreater(n, 8000)
        self.assertGreater(differs, 800)

    def test_cast_operand_keeps_its_layout(self):
        """An integer / float32 operand the glue casts: the copy's layout differs (a stride-0 row becomes a full
        one), the order follows the operand the Python's ufuncs saw."""
        p = np.array([1e16, 1, -1e16, 1, 0, 0, 0, 0, 0])
        cases = [(np.stack([p, p], axis=1).T, np.broadcast_to(np.ones((1, 2), np.int64), (9, 2))),
                 (np.broadcast_to(np.arange(9)[None, :] % 2, (3, 9)), np.asfortranarray(np.stack([p] * 3, axis=1))),
                 (np.asfortranarray(np.stack([p, p])).astype(np.float32), np.ones(9)),
                 (p, np.asfortranarray(np.ones((9, 2), np.int16)))]
        for a, b in cases:
            for fn, ref in ((dnp.matmul, ref_matmul), (dnp.dot, ref_dot)):
                self.assertTrue(same(fn(a, b), ref(a, b)), (a.strides, b.strides, fn(a, b), ref(a, b)))
        # a float32 stack broadcast over an axis times a broadcast float64 vector: the cast copies are C-ordered and
        # would sum the other way (pairwise); the Python's operands summed left to right
        a = np.broadcast_to(np.stack([p, p]).astype(np.float32)[:, None, :], (2, 4, 9))
        b = np.broadcast_to(np.float64(1.0), (9,))
        self.assertTrue(same(dnp.matmul(a, b), ref_matmul(a, b)))
        self.assertFalse(same(fast._ef.dm_matmul(a.astype(np.float64), b.copy()), ref_matmul(a, b)))

    def test_nan_signs_on_layouts(self):
        """A NaN of one kind in a sum (+NaN, -NaN, inf * 0's default NaN, inf + -inf's, the NaN in either operand
        or both at one q) on Fortran / transposed / sliced / reversed / broadcast operands, both orders, both dtypes:
        its sign and payload as the Python's. (Two different NaNs meeting in the elementwise loops of these layouts
        are not compared: numpy's SIMD body and its tail / broadcast-scalar loops keep different operands' NaNs and
        where the body ends depends on the CPU's vector width, so the Python's own result was the machine's; see
        dmmatmul.rs.)"""
        rnd = np.random.default_rng(47)
        n_ok = 0
        for dt in (np.float64, np.float32):
            for k in (9, 12, 17, 64, 129, 300):
                for _ in range(40):
                    n, m = int(rnd.integers(1, 6)), int(rnd.integers(1, 12))
                    a, b = np.ones((n, k), dt), np.ones((k, m), dt)
                    p, q = rnd.choice(k, 2, replace=False)
                    kind = rnd.integers(0, 5)
                    if kind == 0:
                        a[:, p] = np.nan
                    elif kind == 1:
                        a[:, p] = -dt(np.nan)
                        b[p, :] = -dt(np.nan)
                    elif kind == 2:
                        a[:, p] = np.inf
                        b[p, :] = 0.0
                    elif kind == 3:
                        a[:, p], a[:, q] = np.inf, -np.inf
                    else:
                        b[q, :] = np.nan
                        b[p, :] = np.nan
                    a, b = layout(rnd, a), layout(rnd, b)
                    for x, y in ((a, b), (a[0], b), (a, b[:, 0])):
                        got, want = outcome(dnp.matmul, x, y), outcome(ref_matmul, x, y)
                        self.assertTrue(same(got[1], want[1]), (dt, k, kind, x.strides, y.strides, got, want))
                        n_ok += 1
        self.assertGreater(n_ok, 1400)

    def test_errstate_on_layouts(self):
        big = np.full((2, 12), 1.7e308)
        cases = [(np.asfortranarray(big), np.ones(12)), (big[0], np.asfortranarray(np.ones((12, 2)))),
                 (np.asfortranarray(np.full((2, 12), 1e300)), np.full(12, 1e300)),
                 (np.asfortranarray(np.tile([np.inf, -np.inf], (3, 6))), np.ones(12)),
                 (np.tile([np.inf, -np.inf], 6), np.asfortranarray(np.ones((12, 3))))]
        for a, b in cases:
            with np.errstate(all="raise"):
                got, want = outcome(dnp.matmul, a, b), outcome(ref_matmul, a, b)
            self.assertEqual(got[0], want[0], (a.strides, b.strides))
            if got[0] == "raised":
                self.assertIs(got[1], want[1])

            def caught(fn):
                with warnings.catch_warnings(record=True) as w:
                    warnings.simplefilter("always")
                    with np.errstate(all="warn", under="ignore"):
                        r = fn(a, b)
                return r, sorted({str(x.message) for x in w})
            (g, gw), (r, rw) = caught(dnp.matmul), caught(ref_matmul)
            self.assertTrue(same(g, r))
            self.assertEqual(gw, rw, (a.strides, b.strides))


class EmptyAndHugeTest(unittest.TestCase):
    """Empty results and k == 0 come before any copy or table (Codex review of r2a6: a (2^25, 1, 0, 0) @
    (1, 2^25, 0, 1) result reserved 2^50 blocks and aborted); a result too big is numpy's ValueError, as the
    Python's."""

    def test_cases(self):
        bt = np.broadcast_to
        cases = [
            (np.zeros((33554432, 1, 0, 0)), np.zeros((1, 33554432, 0, 1))),
            (np.zeros((33554432, 1, 0, 9)), np.zeros((1, 33554432, 9, 1))),
            (np.zeros((2 ** 40, 1, 0, 9)), bt(np.zeros((1, 1, 9, 2)), (1, 2 ** 40, 9, 2))),
            (np.zeros((2 ** 28, 1, 0, 20)), bt(np.zeros((1, 1, 20, 1)), (1, 2 ** 28, 20, 1))),   # products too big
            (np.zeros((2 ** 28, 1, 0, 5)), bt(np.zeros((1, 1, 5, 1)), (1, 2 ** 28, 5, 1))),
            (bt(np.ones((1, 1, 1, 9)), (2 ** 40, 1, 1, 9)), bt(np.zeros((1, 1, 9, 0)), (1, 2 ** 40, 9, 0))),
            (bt(np.ones((1, 1, 1, 9)), (2 ** 32, 1, 1, 9)), bt(np.zeros((1, 1, 9, 0)), (1, 2 ** 32, 9, 0))),
            (bt(np.ones((1, 1, 9)), (2 ** 40, 1, 9)), bt(np.ones((1, 9, 1)), (1, 9, 2 ** 40))),
            (bt(np.ones((1, 1, 9)), (2 ** 31, 1, 9)), bt(np.ones((1, 9, 1)), (1, 9, 2 ** 31))),
            (bt(np.ones((1, 1, 0)), (2 ** 31, 1, 0)), bt(np.ones((1, 0, 1)), (1, 0, 2 ** 31))),
            (np.zeros(0), np.zeros(0)), (np.zeros((3, 0)), np.zeros(0)), (np.zeros(0), np.zeros((0, 4))),
            (np.zeros((0, 3)), np.zeros(3)), (np.zeros((2, 0, 12)), np.zeros((12, 5))),
            (np.zeros((0, 2, 12)), np.zeros((3, 12, 5))), (np.zeros((4, 0), np.float32), np.zeros((0, 2), np.float32)),
        ]
        for a, b in cases:
            for fn, ref in ((dnp.matmul, ref_matmul), (dnp.dot, ref_dot)):
                if fn is dnp.dot and b.ndim > 2:
                    continue
                got, want = outcome(fn, a, b), outcome(ref, a, b)
                self.assertEqual(got[0], want[0], (a.shape, b.shape, got, want))
                if want[0] == "ok":
                    self.assertTrue(same(got[1], want[1]), (a.shape, b.shape))
                else:
                    self.assertIs(got[1], want[1], (a.shape, b.shape))


    def test_cast_broadcast_operand_is_not_materialised(self):
        """A float32 operand broadcast to 2^30 stacks, an empty result: never cast whole (Codex review of 5ba7172b:
        a 72 GiB cast)."""
        a = np.zeros((1, 0, 9))
        b = np.broadcast_to(np.ones((1, 9, 1), np.float32), (2 ** 30, 9, 1))
        self.assertTrue(same(dnp.matmul(a, b), ref_matmul(a, b)))
        b = np.broadcast_to(np.ones((1, 0, 1), np.float32), (2 ** 30, 0, 1))
        self.assertTrue(same(dnp.matmul(np.zeros((1, 0, 0)), b), ref_matmul(np.zeros((1, 0, 0)), b)))

    @unittest.skipUnless(sys.platform.startswith("linux"), "RLIMIT_AS")
    def test_out_of_memory_raises(self):
        """A result the address space cannot hold: MemoryError, the interpreter alive (Codex review of 5ba7172b: an
        infallible Vec aborted)."""
        import subprocess
        code = (
            "import os, resource, sys\n"
            f"sys.path.insert(0, {os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')!r})\n"
            "import numpy as np, dnp\n"
            "a, b = np.ones((4096, 1)), np.ones((1, 4096))\n"
            "vm = int([l for l in open('/proc/self/status') if l.startswith('VmSize')][0].split()[1]) * 1024\n"
            "resource.setrlimit(resource.RLIMIT_AS, (vm + (64 << 20), resource.RLIM_INFINITY))\n"
            "try:\n"
            "    dnp.matmul(a, b)\n"
            "    print('NO ERROR')\n"
            "except MemoryError:\n"
            "    print('MEMORYERROR')\n")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertEqual(r.stdout.strip(), "MEMORYERROR", r.stderr[-2000:])

    @unittest.skipUnless(sys.platform.startswith("linux"), "RLIMIT_AS")
    def test_broadcast_operand_bounded_memory(self):
        """A broadcast (10^6, 1, 1) operand times a (1, 1): the 8 MB result in 12 MiB of extra address space, as the
        numpy Python returned it (Codex review: the kernel copied every broadcast element, held a block table and a
        second copy of the result, MemoryError). Also a broadcast k (a (1, 3 * 10^6) row of one value: its products
        summed as they are made, no k-long scratch, Codex review) and broadcast stacks on both sides."""
        import subprocess
        code = (
            "import os, resource, sys\n"
            f"sys.path.insert(0, {os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')!r})\n"
            "import numpy as np, dnp\n"
            "a = np.broadcast_to(np.full((1, 1, 1), 1.5), (1_000_000, 1, 1))\n"
            "b = np.full((1, 1), 3.0)\n"
            "r = np.broadcast_to(np.full((1, 1), 0.5), (1, 3_000_000))\n"
            "c = np.broadcast_to(np.full((1, 1), 2.0), (3_000_000, 1))\n"
            "sa = np.broadcast_to(np.arange(6.0).reshape(1, 2, 3), (300_000, 2, 3))\n"
            "sb = np.broadcast_to(np.arange(3.0).reshape(1, 3, 1), (300_000, 3, 1))\n"
            "dnp.matmul(np.ones((1, 1)), b)\n"
            "vm = int([l for l in open('/proc/self/status') if l.startswith('VmSize')][0].split()[1]) * 1024\n"
            "resource.setrlimit(resource.RLIMIT_AS, (vm + (12 << 20), resource.RLIM_INFINITY))\n"
            "out = dnp.matmul(a, b)\n"
            "assert out.shape == (1_000_000, 1, 1) and bool((out == 4.5).all()), out\n"
            "del out\n"
            "s = dnp.matmul(r, c)\n"
            "assert s.shape == (1, 1), s.shape\n"
            "print(repr(float(s[0, 0])))\n"
            "out = dnp.matmul(sa, sb)\n"
            "assert out.shape == (300_000, 2, 1) and out[-1, 0, 0] == 5.0 and out[-1, 1, 0] == 14.0, out[-1]\n"
            "print('OK')\n")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        lines = r.stdout.split()
        self.assertEqual(lines[-1], "OK", r.stderr[-2000:])
        want = ref_matmul(np.broadcast_to(np.full((1, 1), 0.5), (1, 3_000_000)),
                          np.broadcast_to(np.full((1, 1), 2.0), (3_000_000, 1)))
        self.assertEqual(float(lines[0]), float(want[0, 0]))


class ContractTest(unittest.TestCase):
    def test_kernel_refuses_bad_strides(self):
        with self.assertRaises(TypeError):
            fast._ef.dm_matmul(np.zeros(3), np.zeros(3), (8, 8), None)
        with self.assertRaises(TypeError):
            fast._ef.dm_matmul(np.zeros(3), np.zeros(3), None, ())

    def test_kernel_refuses(self):
        ef = fast._ef
        class Sub(np.ndarray):
            pass
        for a, b in ((np.zeros(3).view(Sub), np.zeros(3)), (np.zeros(3), np.zeros(3, np.float32)),
                     (np.zeros(3, np.int64), np.zeros(3, np.int64)), ([1.0, 2.0], np.zeros(2)),
                     (np.zeros(3, np.float16), np.zeros(3, np.float16)), (np.zeros(3, ">f8"), np.zeros(3, ">f8"))):
            with self.assertRaises(TypeError):
                ef.dm_matmul(a, b)

    def test_glue_refuses_other_floats(self):
        for dt in (np.float16, np.complex128):
            with self.assertRaises(TypeError):
                dnp.matmul(np.ones(3, dt), np.ones(3, dt))

    def test_byte_order_and_subclass_through_the_glue(self):
        a, b = np.arange(10.0).astype(">f8"), np.arange(10.0)
        self.assertTrue(same(dnp.matmul(a, b), ref_matmul(a, b)))
        m = np.asmatrix(np.ones((2, 2)))
        self.assertTrue(same(dnp.matmul(m, np.ones(2)), ref_matmul(m, np.ones(2))))

    def test_result_is_fresh(self):
        a = np.ones((3, 2))
        r = dnp.matmul(a, np.ones(2))
        r[0] = 5.0
        self.assertEqual(a[0, 0], 1.0)
        self.assertTrue(r.flags.c_contiguous and r.flags.writeable)


class VersionSkewTest(unittest.TestCase):
    def test_a_471_extension_is_refused(self):
        self.assertGreaterEqual(fast.MIN_VERSION, 472)
        with mock.patch.object(fast, "_ef", types.SimpleNamespace(VERSION=471)), \
                mock.patch.object(dnp, "_ef_mm", fast.native(472, "dnp.matmul")):
            with self.assertRaises(fast.NativeMissing):
                dnp.matmul(np.ones(2), np.ones(2))
            with self.assertRaises(fast.NativeMissing):
                dnp.dot(np.ones((2, 2)), np.ones(2))


if __name__ == "__main__":
    unittest.main()
