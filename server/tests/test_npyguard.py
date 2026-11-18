"""npyguard: numpy's AVX-512 kernels held back before numpy's import (determinism between AVX2 and AVX-512 CPUs),
never in a way that stops numpy from starting."""
import os
import subprocess
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
sys.path.insert(0, SERVER)
import npyguard  # noqa: E402


class NpyGuardTest(unittest.TestCase):
    def test_kept_when_set(self):
        env = {npyguard.ENV: "X86_V4"}
        with mock.patch.object(npyguard, "features", side_effect=AssertionError("not asked")):
            self.assertEqual(npyguard.apply(env), "X86_V4")
        env = {npyguard.ENV: ""}            # (a parent that found nothing to hold back: not asked again)
        with mock.patch.object(npyguard, "features", side_effect=AssertionError("not asked")):
            self.assertEqual(npyguard.apply(env), "")

    def test_nothing_without_avx512(self):
        with mock.patch.object(npyguard, "_x86", return_value=True), \
                mock.patch.object(npyguard, "has_avx512", return_value=False), \
                mock.patch.object(npyguard, "_probe", side_effect=AssertionError("no probe")):
            env = {}
            self.assertEqual(npyguard.apply(env), "")
        with mock.patch.object(npyguard, "_x86", return_value=False), \
                mock.patch.object(npyguard, "has_avx512", side_effect=AssertionError("not asked")):
            self.assertEqual(npyguard.features(), ())

    def test_frozen_names_by_version(self):
        with mock.patch.object(npyguard, "_x86", return_value=True), \
                mock.patch.object(npyguard, "has_avx512", return_value=True), \
                mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(npyguard, "_probe", side_effect=AssertionError("no probe")):
            with mock.patch.object(npyguard, "_numpy_version", return_value=(2, 5)):
                self.assertEqual(npyguard.features(), npyguard.NAMES_2_4)
            with mock.patch.object(npyguard, "_numpy_version", return_value=None):
                self.assertEqual(npyguard.features(), npyguard.NAMES_2_4)
            with mock.patch.object(npyguard, "_numpy_version", return_value=(1, 26)):
                self.assertEqual(npyguard.features(), npyguard.NAMES_OLD)

    def test_probe_is_numpys_own_list(self):
        """The probe names only AVX-512 targets outside this numpy's baseline: a numpy given them still starts."""
        got = npyguard._probe()
        self.assertIsNotNone(got)
        import numpy._core._multiarray_umath as m
        self.assertTrue(set(got) <= set(m.__cpu_dispatch__) - set(m.__cpu_baseline__))
        self.assertTrue(all(f.startswith(("X86_V4", "AVX512", "AVX10")) for f in got))

    def test_numpy_starts_with_it(self):
        """numpy imports with the names set (warnings as errors: no unknown name), on a CPU without those features
        too, and dispatches no AVX-512 kernel then."""
        names = " ".join(npyguard._probe() or npyguard._wheel_names()) if npyguard._x86() else ""
        code = ("import sys; sys.path.insert(0, %r); import numpy, npyguard; "
                "print(npyguard.avx512_active(), float(numpy.sort(numpy.arange(5.0))[::-1].sum()))" % SERVER)
        r = subprocess.run([sys.executable, "-W", "error", "-c", code], capture_output=True, text=True,
                           env=dict(os.environ, **{npyguard.ENV: names}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split("]")[0] + "]", "[]")

    def test_earthd_sets_it_first(self):
        """earthd.py applies the guard before anything imports numpy."""
        with open(os.path.join(SERVER, "earthd.py"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        guard = next(i for i, ln in enumerate(lines) if ln.strip() == "npyguard.apply()")
        imports = [i for i, ln in enumerate(lines) if ln.startswith(("import ", "from "))]
        first_other = next(i for i in imports if not lines[i].startswith(("import os", "import npyguard")))
        self.assertLess(guard, first_other)


if __name__ == "__main__":
    unittest.main()
