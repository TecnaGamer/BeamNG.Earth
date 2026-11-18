"""dnp.matmul and dnp.dot in Rust (earthfast 472, dmmatmul: the full Rust port) against the numpy arithmetic they
replaced: random operands (1-D, 2-D and stacked, k from 0 past numpy's 128-element pairwise blocks, broadcast stacks,
strided / reversed / unaligned views, float64 and float32 and their mixes with integers, signed zeros, infinities,
NaNs, values that overflow) give the same dtype, shape, type (a numpy scalar for two vectors) and bytes; numpy's
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
                        self.assertTrue(same(got[1], want[1]), (dt, k, p, q, x.shape, y.shape))


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


class ContractTest(unittest.TestCase):
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
