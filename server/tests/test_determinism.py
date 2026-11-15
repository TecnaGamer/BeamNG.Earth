"""The same map data must give the same plans and tiles on every platform (GitHub #1169): Windows built a 0.78 g crest
and a 1.41 m step where Linux had 0.26 g and 0.96 m, from the same Overture roads and DEM.

The cause was the maths library, not the code: the platform's C library (glibc 2.35 and 2.44 differ, the UCRT and
mingw differ from both) and numpy's CPU-dispatched SIMD and BLAS / LAPACK kernels give other last bits on a few
percent of the inputs to sin, atan2, hypot, exp, pow (Python's x ** 2 on 0.04-0.09 %), dot / @ / einsum / norm, and
a planner's decision (a crossing found or not, the nearer part, a sort by a computed key) turns such a bit into
another road. A one-ulp perturbation of every libm result moved 16 of the 27 replay sites (2026-11-12).

So the server's maths goes through dnp.py / dmath.py (the native module's libm-crate functions, exact formulations,
small native linear algebra) and the Rust kernels through dm.rs. These tests keep platform maths from coming back:
a call that needs it anyway (where the bits cannot reach any output) carries "# det-ok: <why>" on its line."""
import ast
import io
import math
import os
import re
import sys
import tokenize
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
ROOT = os.path.dirname(SERVER)
sys.path.insert(0, SERVER)
import dmath  # noqa: E402
import dnp  # noqa: E402

NP_FUNCS = {"sin", "cos", "tan", "arcsin", "arccos", "arctan", "arctan2", "hypot", "exp", "expm1", "exp2", "log",
            "log2", "log10", "log1p", "power", "float_power", "arcsinh", "arccosh", "arctanh", "sinh", "cosh", "tanh",
            "cbrt", "dot", "matmul", "einsum", "inner", "vdot", "tensordot", "convolve", "correlate", "polyfit",
            "cov", "corrcoef", "linalg", "logaddexp", "sinc"}
MATH_FUNCS = {"sin", "cos", "tan", "asin", "acos", "atan", "atan2", "exp", "expm1", "exp2", "log", "log2", "log10",
              "log1p", "pow", "sinh", "cosh", "tanh", "asinh", "acosh", "atanh", "cbrt"}
RUST_METHODS = ("sin", "cos", "tan", "asin", "acos", "atan", "atan2", "sin_cos", "hypot", "exp", "exp2", "exp_m1", "ln",
                "ln_1p", "log", "log2", "log10", "powf", "cbrt", "sinh", "cosh", "tanh", "asinh", "acosh", "atanh",
                "mul_add")
PY_EXEMPT = {"dnp.py", "dmath.py"}


def _server_files():
    files = [os.path.join(SERVER, f) for f in sorted(os.listdir(SERVER)) if f.endswith(".py") and f not in PY_EXEMPT]
    return files + [os.path.join(HERE, "drivability.py"), os.path.join(HERE, "interchanges.py")]


def _pow_exempt(n):
    """int-literal base (2 ** z, 10 ** k, (-1) ** k), 2.0 ** z (a power of two) or both literal: exact."""
    left = n.left.operand if isinstance(n.left, ast.UnaryOp) else n.left
    return isinstance(left, ast.Constant) and (isinstance(left.value, int) or left.value == 2.0
                                               or isinstance(n.right, ast.Constant))


def platform_maths(path):
    """[(line, what)] of the platform maths in one Python file."""
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    ok = {i + 1 for i, ln in enumerate(src.splitlines()) if "# det-ok" in ln}
    out = []
    toks = [t for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT)]
    for i, t in enumerate(toks[:-2]):
        if t.type != tokenize.NAME or (i and toks[i - 1].string == ".") or toks[i + 1].string != ".":
            continue
        name = toks[i + 2].string
        if name == "linalg" and i + 4 < len(toks) and toks[i + 4].string == "LinAlgError":
            continue
        if (t.string == "np" and name in NP_FUNCS) or (t.string == "math" and name in MATH_FUNCS):
            out.append((t.start[0], f"{t.string}.{name}"))
        elif name == "dot" and i + 3 < len(toks) and toks[i + 3].string == "(" and t.string not in ("dnp", "self"):
            out.append((t.start[0], f"{t.string}.dot (ndarray.dot is BLAS)"))
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.MatMult):
            out.append((n.lineno, "@ (BLAS)"))
        elif isinstance(n, ast.AugAssign) and isinstance(n.op, (ast.MatMult, ast.Pow)):
            out.append((n.lineno, "@= / **="))
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Pow) and not _pow_exempt(n):
            out.append((n.lineno, "** (the C library's pow: dnp.sq / dnp.power)"))
    return [(ln, what) for ln, what in out if ln not in ok]


class NoPlatformMathsTest(unittest.TestCase):
    def test_server_python(self):
        bad = [f"{os.path.relpath(p, ROOT)}:{ln}: {what}" for p in _server_files() for ln, what in platform_maths(p)]
        self.assertFalse(bad, "platform maths in the server (use dnp / dmath, GitHub #1169; '# det-ok: why' on a "
                         "line where the bits cannot reach any output):\n  " + "\n  ".join(bad[:60]))

    def test_rust_kernels(self):
        src_dir = os.path.join(ROOT, "native", "earthfast", "src")
        pat = re.compile(r"\.(" + "|".join(RUST_METHODS) + r")\(")
        bad = []
        for dirpath, _dirs, files in os.walk(src_dir):
            for f in files:
                p = os.path.join(dirpath, f)
                if not f.endswith(".rs") or p.endswith(os.path.join("src", "dm.rs")):
                    continue
                with open(p, encoding="utf-8") as fh:
                    for i, line in enumerate(fh, 1):
                        code = line.split("//")[0]
                        if pat.search(code) and "det-ok" not in line:
                            bad.append(f"{os.path.relpath(p, ROOT)}:{i}: {line.strip()[:100]}")
        self.assertFalse(bad, "the platform's C maths in a kernel (use dm::Dm: x.dsin() for x.sin(), GitHub #1169):"
                         "\n  " + "\n  ".join(bad[:60]))


class DeterministicMathsTest(unittest.TestCase):
    """dnp / dmath agree with numpy / math within a few ulps (they are other implementations, not other functions),
    and return what numpy / math return (shapes, dtypes, Python floats)."""

    def setUp(self):
        rng = np.random.default_rng(1169)
        self.x = rng.uniform(-3.0, 3.0, (40, 3))
        self.p = rng.uniform(0.01, 40.0, (40, 3))

    def close(self, a, b, rtol=4e-16 * 8):
        self.assertEqual(np.shape(a), np.shape(b))
        np.testing.assert_allclose(a, b, rtol=rtol, atol=1e-300)

    def test_functions(self):
        for name in ("sin", "cos", "tan", "arctan", "sinh", "cosh", "tanh", "arcsinh", "exp"):
            self.close(getattr(dnp, name)(self.x), getattr(np, name)(self.x))
        for name in ("log", "log2", "log10", "sqrt"):
            if hasattr(dnp, name):
                self.close(getattr(dnp, name)(self.p), getattr(np, name)(self.p))
        self.close(dnp.arctan2(self.x, self.p), np.arctan2(self.x, self.p))
        self.close(dnp.hypot(self.x, self.p), np.hypot(self.x, self.p))
        self.close(dnp.power(self.p, 1.37), np.power(self.p, 1.37))
        self.assertIsInstance(dnp.sin(0.5), np.float64)
        self.assertEqual(dnp.sin(self.x.astype(np.float32)).dtype, np.float32)
        self.assertEqual(dnp.power(3, 4), 81)
        for name in ("sin", "cos", "tan", "atan", "exp", "sinh", "asinh"):
            self.assertAlmostEqual(getattr(dmath, name)(0.7), getattr(math, name)(0.7), places=15)
        self.assertEqual(dmath.log(8.0, 2.0), 3.0)
        self.assertIsInstance(dmath.sin(1), float)

    def test_products(self):
        a, b, v = self.x, self.p[:3].T.copy(), self.p[0]
        self.close(dnp.matmul(a, b), a @ b, rtol=1e-13)
        self.close(dnp.matmul(a, v), a @ v, rtol=1e-13)
        self.close(dnp.matmul(v, v), v @ v, rtol=1e-13)
        self.close(dnp.dot(a.T, a), np.dot(a.T, a), rtol=1e-13)
        self.close(dnp.norm(a, axis=1), np.linalg.norm(a, axis=1), rtol=1e-14)
        self.close(dnp.norm(v), np.linalg.norm(v), rtol=1e-14)
        self.close(dnp.vecdot(a, self.p), np.einsum("ij,ij->i", a, self.p), rtol=1e-13)
        z = self.x[:, 0]
        for mode in ("full", "same", "valid"):
            for k in (1, 2, 5):
                self.close(dnp.convolve(z, np.ones(k) / k, mode), np.convolve(z, np.ones(k) / k, mode), rtol=1e-12)

    def test_linalg(self):
        m = self.x[:3, :3] + 4 * np.eye(3)
        self.close(dnp.solve(m, self.p[0]), np.linalg.solve(m, self.p[0]), rtol=1e-12)
        self.close(dnp.inv(m), np.linalg.inv(m), rtol=1e-12)
        self.assertAlmostEqual(dnp.det(m), np.linalg.det(m), places=10)
        A = np.c_[self.x[:, :2], np.ones(40)]
        self.close(dnp.lstsq(A, self.p[:, 0])[0], np.linalg.lstsq(A, self.p[:, 0], rcond=None)[0], rtol=1e-10)
        dep = np.c_[np.ones(5), np.arange(5.0), 2 * np.arange(5.0)]
        x, _res, rank, _sv = dnp.lstsq(dep, np.arange(5.0) * 3 + 1)
        self.assertEqual(rank, 2)       # numpy's minimum-norm solution of a rank-deficient system
        self.close(x, np.linalg.lstsq(dep, np.arange(5.0) * 3 + 1, rcond=None)[0], rtol=1e-9)
        w, vec = dnp.eigh(A.T @ A)      # det-ok: the reference
        self.close(w, np.linalg.eigh(A.T @ A)[0], rtol=1e-10)   # det-ok: the reference
        self.close(np.abs(vec), np.abs(np.linalg.eigh(A.T @ A)[1]), rtol=1e-8)  # det-ok: the reference
        self.close(dnp.polyfit(self.x[:, 0], 2 * self.x[:, 0] + 1, 1), np.array([2.0, 1.0]), rtol=1e-12)

    def test_known_bits(self):
        """Values pinned to the libm crate's bits where platform libraries are known to round otherwise."""
        self.assertEqual(float(dnp.sin(1.0)).hex(), "0x1.aed548f090ceep-1")
        self.assertEqual(dmath.atan2(1.0, 2.0), 0.4636476090008061)
        self.assertEqual(float(dnp.hypot(3.0, 4.0)), 5.0)


if __name__ == "__main__":
    unittest.main()
