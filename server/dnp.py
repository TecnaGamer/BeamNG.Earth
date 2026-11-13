"""Deterministic numpy maths: the same bits on every platform and CPU (GitHub #1169).

numpy's transcendental functions (sin, arctan2, exp, power ...) are the C library's (glibc on Linux, the UCRT on
Windows) or its own CPU-dispatched SIMD code (AVX-512 machines take other routines); np.dot / @ / einsum /
linalg.norm / convolve / linalg go through BLAS and LAPACK, whose kernels are chosen by the CPU (FMA or not, another
summation order). Their last bits differ between machines on a few percent of the inputs, and a planner's decision
turns such a bit into another road (Windows built a 0.78 g crest where Linux had 0.26 g from the same map data).

Here: the transcendental functions are the native module's libm-crate ones (native/earthfast/src/dm.rs, the same as
its kernels'; dmath.py is the scalar side), computed in float64 (a float32 input gives a float32 result); hypot is
sqrt(x * x + y * y); products and norms are elementwise multiplies and numpy's add.reduce (fixed order); solve /
lstsq / eigh are small native kernels. numpy's elementwise arithmetic, sqrt, sum / cumsum / mean, sort, interp, round
and friends are exact or the same code everywhere and are used as they are. tests/test_determinism.py fails on a
platform call in server/*.py.
"""
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

import fast

_ef = fast.native(264, "dnp")

_ATAN2, _POW, _HYPOT = 32, 33, 34


def _out_dtype(*arrays):
    dt = np.result_type(*arrays)
    return dt if dt in (np.float32, np.float16) else np.float64


_SCALAR = (float, int, np.float64)
_F64 = np.float64
_S1 = {0: "dm_sin", 1: "dm_cos", 2: "dm_tan", 3: "dm_asin", 4: "dm_acos", 5: "dm_atan", 6: "dm_sinh", 7: "dm_cosh",
       8: "dm_tanh", 9: "dm_asinh", 10: "dm_atanh", 11: "dm_exp", 12: "dm_log", 13: "dm_log2", 14: "dm_log10",
       15: "dm_cbrt", 16: "dm_expm1", 17: "dm_log1p", 18: "dm_acosh"}
_S2 = {32: "dm_atan2", 33: "dm_pow", 34: "dm_hypot"}


_F64D = np.dtype(np.float64)
_ND = np.ndarray


def _unary(op, x):
    if type(x) in _SCALAR:              # (a Python or numpy float: the scalar kernel, a numpy float64 back as numpy)
        return _F64(getattr(_ef, _S1[op])(x))
    if type(x) is _ND and x.dtype == _F64D and x.ndim:      # (the common case, without numpy's type machinery)
        r = _ef.dm_vec(op, x.ravel())
        return r if x.ndim == 1 else r.reshape(x.shape)
    a = np.asarray(x)
    dt = _out_dtype(a)
    r = _ef.dm_vec(op, np.ascontiguousarray(a, np.float64).ravel()).reshape(a.shape)
    if dt != np.float64:
        r = r.astype(dt)
    return r if r.ndim else r[()]


def _binary(op, x, y):
    tx, ty = type(x), type(y)
    if tx in _SCALAR and ty in _SCALAR:
        return _F64(getattr(_ef, _S2[op])(x, y))
    if tx is _ND and ty is _ND and x.dtype == _F64D and y.dtype == _F64D and x.shape == y.shape and x.ndim:
        r = _ef.dm_vec(op, x.ravel(), y.ravel())
        return r if x.ndim == 1 else r.reshape(x.shape)
    a, b = np.asarray(x), np.asarray(y)
    dt = _out_dtype(a, b)
    a, b = np.broadcast_arrays(a, b)
    r = _ef.dm_vec(op, np.ascontiguousarray(a, np.float64).ravel(),
                   np.ascontiguousarray(b, np.float64).ravel()).reshape(a.shape)
    if dt != np.float64:
        r = r.astype(dt)
    return r if r.ndim else r[()]


def _store(r, out):
    """numpy's out argument: the result written into out, out returned."""
    if out is None:
        return r
    out[...] = r
    return out


def _make(op):
    def f(x, out=None):
        return _store(_unary(op, x), out)
    return f


sin, cos, tan = _make(0), _make(1), _make(2)
arcsin, arccos, arctan = _make(3), _make(4), _make(5)
sinh, cosh, tanh = _make(6), _make(7), _make(8)
arcsinh, arctanh, arccosh = _make(9), _make(10), _make(18)
exp, log, log2, log10 = _make(11), _make(12), _make(13), _make(14)
cbrt, expm1, log1p = _make(15), _make(16), _make(17)


def arctan2(y, x, out=None):
    """np.arctan2."""
    return _store(_binary(_ATAN2, y, x), out)


def _float(a):
    return a if a.dtype.kind in "fc" else a.astype(np.float64)


def hypot(x, y, out=None):
    """np.hypot: the libm crate's hypot, the kernels' own (x.dhypot(y)), so that a length measured in Python and the
    same length measured in a kernel agree to the bit. out: numpy's third argument (hypot(*v.T) of an (n, 3) v writes
    the result into its third column, as numpy did)."""
    return _store(_binary(_HYPOT, x, y), out)


def sq(x):
    """x ** 2 as x * x: the exact square for Python numbers and arrays (Python's float ** 2 is the C library's pow,
    which differs from x * x on 0.04-0.09 % of the inputs, by platform)."""
    return x * x


def power(x, y):
    """x ** y: int ** int exact (as Python / numpy), x * x for y == 2, sqrt for y == 0.5, else the deterministic pow
    (a Python number in, a Python float out; arrays broadcast)."""
    if isinstance(y, (int, float)) and not isinstance(y, bool):
        if isinstance(x, (int, np.integer)) and isinstance(y, int) and y >= 0:
            return x ** y
        if y == 2:
            return x * x
        if y == 0.5:
            return np.sqrt(x) if not isinstance(x, (int, float)) else float(np.sqrt(x))
        if isinstance(x, (int, float)):
            return _ef.dm_pow(float(x), float(y))
    a, b = np.asarray(x), np.asarray(y)
    if a.dtype.kind in "iub" and b.dtype.kind in "iub":
        return np.power(a, b)
    return _binary(_POW, a, b)


def matmul(a, b):
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


def dot(a, b):
    """np.dot for operands of up to 2 axes (and an N-axis a with a 1- or 2-axis b): matmul's products, a scalar
    operand multiplies."""
    a, b = np.asarray(a), np.asarray(b)
    if a.ndim == 0 or b.ndim == 0:
        return a * b
    if b.ndim > 2:
        raise ValueError("dnp.dot: b with more than 2 axes")
    return matmul(a, b)


def vecdot(a, b, axis=-1):
    """Sum of a * b over axis (einsum "ij,ij->i" and friends)."""
    return np.add.reduce(np.asarray(a) * np.asarray(b), axis=axis)


def norm(x, ord=None, axis=None, keepdims=False):  # noqa: A002 (numpy's name)
    """np.linalg.norm: the 2-norm / Frobenius norm as sqrt(add.reduce(x * x)); other orders (inf, 1, ...) are not
    sums of squares and go to numpy (no BLAS there); a matrix's spectral norm (ord 2 over two axes) is refused."""
    x = np.asarray(x)
    two_axes = (axis is None and x.ndim == 2) or (isinstance(axis, tuple) and len(axis) == 2)
    if ord in (None, "fro") or (ord == 2 and not two_axes):
        x = _float(x)
        return np.sqrt(np.add.reduce(x * x, axis=axis, keepdims=keepdims))
    if ord == 2 or ord == -2 or ord == "nuc":
        raise ValueError("dnp.norm: spectral / nuclear norms are not deterministic here")
    return np.linalg.norm(x, ord, axis, keepdims)


def convolve(a, v, mode="full"):
    """np.convolve (1-D) as each window's products summed in a fixed order."""
    a, v = np.asarray(a, np.float64).ravel(), np.asarray(v, np.float64).ravel()
    if len(v) > len(a):
        a, v = v, a
    n, m = len(a), len(v)
    if m == 0:
        raise ValueError("v cannot be empty")
    left, right = {"full": (m - 1, m - 1), "same": (m // 2, m - 1 - m // 2), "valid": (0, 0)}[mode]
    ap = np.concatenate([np.zeros(left), a, np.zeros(right)]) if left or right else a
    w = sliding_window_view(ap, m)
    return np.add.reduce(w * v[::-1], axis=-1) if len(w) else np.zeros(0)


def polyfit(x, y, deg):
    """np.polyfit for a line (deg 1): (slope, intercept) by the centred least-squares formula."""
    if deg != 1:
        raise ValueError("dnp.polyfit: only deg 1")
    x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
    mx, my = np.add.reduce(x) / len(x), np.add.reduce(y) / len(y)
    dx = x - mx
    sxx = np.add.reduce(dx * dx)
    b = np.add.reduce(dx * (y - my)) / sxx if sxx > 0 else 0.0
    return np.array([b, my - b * mx])


def solve(a, b):
    """np.linalg.solve for a (n, n) and b (n,) or (n, m) (np.linalg.LinAlgError when a is singular)."""
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    vb = b.ndim == 1
    try:
        x = _ef.dm_solve(np.ascontiguousarray(a), np.ascontiguousarray(b[:, None] if vb else b))
    except ValueError as e:
        raise np.linalg.LinAlgError(str(e)) from None
    return x[:, 0] if vb else x


def inv(a):
    """np.linalg.inv of a (n, n)."""
    a = np.asarray(a, np.float64)
    return solve(a, np.eye(len(a)))


LSTSQ_RANK_TOL = 1e-10  # a (unit-scaled) column whose QR diagonal is below this share of the largest counts as
                        # dependent (QR's diagonal is no exact rank test; numpy's eps-level rcond on singular values)


def lstsq(a, b, rcond=None):
    """np.linalg.lstsq's (x, residuals, rank, singular values): the columns scaled to unit length, then x by
    Householder QR when they are independent, else numpy's minimum-norm x from the eigen decomposition of a^T
    a (rank: its eigenvalues over 1e-12 of the largest); residuals the summed squares of b - a x (as numpy: empty
    unless r > c and a has full rank); singular values None. (Unscaled, a plane fit in projected metres, columns
    x ~ 1e6 beside a column of ones, read as rank-deficient.)"""
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    vb = b.ndim == 1
    b2 = b[:, None] if vb else b
    r, c = a.shape
    scale = np.sqrt(np.add.reduce(a * a, axis=0))
    scale = np.where(scale > 0.0, scale, 1.0)
    s_ = a / scale
    tol = LSTSQ_RANK_TOL if rcond is None or rcond < 0 else max(rcond, LSTSQ_RANK_TOL)
    x = _ef.dm_lstsq(np.ascontiguousarray(s_), np.ascontiguousarray(b2), float(tol)) if r >= c else None
    rank = c
    if x is None:
        w, v = eigh(matmul(a.T, a))
        keep = w > 1e-12 * (w.max() if len(w) else 0.0)     # (a^T a's own rounding: eigenvalues near eps * max)
        rank = int(keep.sum())
        inv_w = np.where(keep, 1.0 / np.where(keep, w, 1.0), 0.0)
        x = matmul(v * inv_w, matmul(v.T, matmul(a.T, b2)))
    else:
        x = x / scale[:, None]
    res = b2 - matmul(a, x)
    resid = np.add.reduce(res * res, axis=0) if r > c and rank == c else np.zeros(0)
    return (x[:, 0] if vb else x), resid, rank, None


def eigh(a):
    """np.linalg.eigh of a symmetric (n, n) matrix: (eigenvalues ascending, eigenvectors as columns, each with its
    largest component positive)."""
    return _ef.dm_eigh(np.ascontiguousarray(np.asarray(a, np.float64)))


def det(a):
    """np.linalg.det of a (2, 2) or (3, 3) matrix (the cofactor formula)."""
    a = np.asarray(a, np.float64)
    if a.shape == (2, 2):
        return a[0, 0] * a[1, 1] - a[0, 1] * a[1, 0]
    if a.shape == (3, 3):
        return (a[0, 0] * (a[1, 1] * a[2, 2] - a[1, 2] * a[2, 1]) - a[0, 1] * (a[1, 0] * a[2, 2] - a[1, 2] * a[2, 0])
                + a[0, 2] * (a[1, 0] * a[2, 1] - a[1, 1] * a[2, 0]))
    raise ValueError("dnp.det: only 2x2 and 3x3")
