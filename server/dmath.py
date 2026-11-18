"""Deterministic scalar maths: math's transcendental functions with the same bits on every platform (GitHub #1169).

math.sin / atan2 / exp / log ... and float ** call the C library of the machine: glibc on Linux (2.35 and 2.44
differ), the UCRT on Windows. Their last bits differ on a few percent of the inputs (x ** 2 on 0.08 %), and a
planner's decision turns such a bit into another road. These are the native module's libm-crate functions
(native/earthfast/src/dm.rs), the same ones its kernels use; dnp.py is the numpy side. tests/test_determinism.py
fails on a platform call in server/*.py.

Exact everywhere, so taken from math as they are: sqrt, hypot (CPython's own), floor, ceil, fabs, copysign, fmod,
radians, degrees, isfinite / isnan / isinf, and the constants.
"""
import math

import fast

from math import (ceil, copysign, degrees, e, fabs, floor, fmod, hypot, inf, isclose, isfinite, isinf,  # noqa: F401
                  isnan, nan, pi, radians, sqrt, tau, trunc)

_ef = fast.native(264, "dmath")
sin, cos, tan = _ef.dm_sin, _ef.dm_cos, _ef.dm_tan
asin, acos, atan, atan2 = _ef.dm_asin, _ef.dm_acos, _ef.dm_atan, _ef.dm_atan2
sinh, cosh, tanh = _ef.dm_sinh, _ef.dm_cosh, _ef.dm_tanh
asinh, acosh, atanh = _ef.dm_asinh, _ef.dm_acosh, _ef.dm_atanh
exp, expm1, log1p, log2, log10, cbrt = _ef.dm_exp, _ef.dm_expm1, _ef.dm_log1p, _ef.dm_log2, _ef.dm_log10, _ef.dm_cbrt
_log, _pow = _ef.dm_log, _ef.dm_pow


def log(x, base=None):
    """math.log: the natural logarithm, or the logarithm to base."""
    return _log(x) if base is None else _log(x) / _log(base)


def pow(x, y):  # noqa: A001 (math.pow's name)
    """x ** y for Python numbers: exact for int ** int (as Python), x * x for y == 2, sqrt for y == 0.5, else the
    deterministic pow."""
    if type(x) is int and type(y) is int and y >= 0:
        return x ** y
    if y == 2:
        return x * x
    if y == 0.5:
        return sqrt(x)
    return _pow(x, y)
