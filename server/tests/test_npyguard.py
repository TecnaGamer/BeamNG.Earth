"""npyguard: numpy's AVX-512 kernels held back before numpy's import (determinism between AVX2 and AVX-512 CPUs),
never in a way that stops numpy from starting, in every entry point and the processes they start."""
import ast
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
ROOT = os.path.dirname(SERVER)
sys.path.insert(0, SERVER)
import npyguard  # noqa: E402

# the processes that make plans, tiles, packs or shared items (the diagnostic probes under tools/ are not: their
# outputs go nowhere); each imports npyguard before any module outside the standard library
ENTRY_POINTS = ("server/earthd.py", "server/sharehelper.py", "tools/bake_area.py", "tools/build_decisions.py",
                "tools/build_lidarz.py", "tools/rebaseroads.py", "tools/rebasefar.py")


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
                mock.patch.dict(os.environ, {npyguard.FORCE_ENV: ""}), \
                mock.patch.object(npyguard, "_probe", side_effect=AssertionError("no probe")):
            self.assertEqual(npyguard.apply({}), "")
        with mock.patch.object(npyguard, "_x86", return_value=False), \
                mock.patch.object(npyguard, "has_avx512", side_effect=AssertionError("not asked")):
            self.assertEqual(npyguard.features(), ())

    def test_frozen_names_from_the_build(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(npyguard, "_x86", return_value=True), \
                mock.patch.object(npyguard, "has_avx512", return_value=True), \
                mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(npyguard, "_probe", side_effect=AssertionError("no probe")):
            f = os.path.join(d, "names.json")
            with open(f, "w") as fh:
                json.dump({"numpy": "2.5.4", "names": ["X86_V4", "AVX512_ICL"]}, fh)
            with mock.patch.object(npyguard, "FROZEN_FILE", f):
                self.assertEqual(npyguard.features(), ("X86_V4", "AVX512_ICL"))
            # (no file: the PyPI wheels' names for the version)
            with mock.patch.object(npyguard, "FROZEN_FILE", os.path.join(d, "none.json")):
                with mock.patch.object(npyguard, "_numpy_version", return_value=(2, 5)):
                    self.assertEqual(npyguard.features(), npyguard.NAMES_2_4)
                with mock.patch.object(npyguard, "_numpy_version", return_value=(1, 26)):
                    self.assertEqual(npyguard.features(), npyguard.NAMES_OLD)

    def test_probe_is_numpys_own_list(self):
        """The probe names only AVX-512 targets outside this numpy's baseline."""
        got = npyguard._probe()
        self.assertIsNotNone(got)
        import numpy._core._multiarray_umath as m
        self.assertTrue(set(got) <= set(m.__cpu_dispatch__) - set(m.__cpu_baseline__))
        self.assertTrue(all(f.startswith(("X86_V4", "AVX512", "AVX10")) for f in got))

    def test_children_ask_this_process_numpy(self):
        """The probe and the check run isolated (-I) with this process's sys.path: the numpy they ask is the one this
        process imports, not one found through the current folder or the user's site folder."""
        seen = []

        def run(cmd, **kw):
            seen.append((cmd, kw.get("env", {})))
            return subprocess.CompletedProcess(cmd, 0, "X86_V4\n", "")
        with mock.patch.object(npyguard.subprocess, "run", side_effect=run):
            self.assertEqual(npyguard._probe(), ("X86_V4",))
            npyguard._validate(("X86_V4",))
        for cmd, _env in seen:
            self.assertIn("-I", cmd)
            self.assertEqual(json.loads(cmd[-1]), sys.path)
        self.assertNotIn(npyguard.ENV, seen[0][1])
        self.assertEqual(seen[1][1][npyguard.ENV], "X86_V4")

    def test_unvalidated_names_not_set(self):
        """Names numpy would not start with (or that leave AVX-512 dispatched) are not used."""
        with mock.patch.object(npyguard, "_x86", return_value=True), \
                mock.patch.object(npyguard, "has_avx512", return_value=True), \
                mock.patch.object(npyguard, "_probe", return_value=("X86_V4",)), \
                mock.patch.object(npyguard, "_validate", return_value=False):
            self.assertEqual(npyguard.features(), ())
        self.assertFalse(npyguard._validate(("NOT_A_NUMPY_FEATURE",)))     # (an ImportWarning: -W error)
        names = npyguard._probe()
        if names:
            self.assertTrue(npyguard._validate(names))

    def test_numpy_starts_with_it(self):
        """numpy imports with the names set (warnings as errors: no unknown name), on a CPU without those features
        too, and dispatches no AVX-512 kernel then."""
        # (this numpy's own AVX-512 targets: none on a wheel that dispatches nothing past X86_V3, e.g. the Windows
        # wheels, where the PyPI names would be unknown to it, an ImportWarning)
        names = " ".join(npyguard._probe() or ()) if npyguard._x86() else ""
        code = ("import sys; sys.path.insert(0, %r); import numpy, npyguard; "
                "print(npyguard.avx512_active(), float(numpy.sort(numpy.arange(5.0))[::-1].sum()))" % SERVER)
        r = subprocess.run([sys.executable, "-W", "error", "-c", code], capture_output=True, text=True,
                           env=dict(os.environ, **{npyguard.ENV: names}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split("]")[0] + "]", "[]")

    def test_self_check(self):
        """launcher.py --check-numpy (the release build's check): ok here, in a fresh process."""
        r = subprocess.run([sys.executable, os.path.join(ROOT, "launcher.py"), "--check-numpy"], capture_output=True,
                           text=True, env={k: v for k, v in os.environ.items() if k != npyguard.ENV}, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(r.stdout.startswith("ok: "), r.stdout)

    def test_frozen_self_check_is_strict(self):
        """The packaged app's check (launcher.py --check-numpy) needs a readable manifest of the bundled numpy: its
        version and its full set of AVX-512 targets; a missing, empty, incomplete or other-version one fails (the run
        time's fallback is not taken)."""
        import numpy
        full = list(npyguard._probe() or ())
        cases = {"missing": None, "other version": {"numpy": "1.0.0", "names": full},
                 "empty": {"numpy": numpy.__version__, "names": []},
                 "incomplete": {"numpy": numpy.__version__, "names": full[:-1]},
                 "malformed": "not json", "right": {"numpy": numpy.__version__, "names": full}}
        with tempfile.TemporaryDirectory() as d:
            for name, doc in cases.items():
                f = os.path.join(d, name.replace(" ", "_") + ".json")
                if doc is not None:
                    with open(f, "w") as fh:
                        fh.write(doc if isinstance(doc, str) else json.dumps(doc))
                code = ("import sys; sys.frozen = True; sys.path.insert(0, %r); import npyguard; "
                        "npyguard.FROZEN_FILE = %r; ok, msg = npyguard.self_check(); print(ok); print(msg)"
                        % (SERVER, f))
                r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                   env={k: v for k, v in os.environ.items() if k != npyguard.ENV})
                self.assertEqual(r.returncode, 0, r.stderr)
                ok = r.stdout.split("\n")[0] == "True"
                if name == "right" or (name == "empty" and not full) or (name == "incomplete" and not full):
                    self.assertTrue(ok, (name, r.stdout))
                else:
                    self.assertFalse(ok, (name, r.stdout))
            # (a numpy built for x86-64-v4: a matching manifest of its optional targets alone still fails the check)
            f = os.path.join(d, "right.json")
            code = ("import sys, types; sys.frozen = True; sys.path.insert(0, %r); import npyguard; "
                    "npyguard.FROZEN_FILE = %r; real = npyguard._umath\n"
                    "def fake():\n"
                    "    m = real()\n"
                    "    return types.SimpleNamespace(__cpu_baseline__=list(m.__cpu_baseline__) + ['X86_V4'],\n"
                    "                                 __cpu_dispatch__=m.__cpu_dispatch__, __cpu_features__=m.__cpu_features__)\n"
                    "npyguard._umath = fake\n"
                    "ok, msg = npyguard.self_check(); print(ok); print(msg)" % (SERVER, f))
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                               env={k: v for k, v in os.environ.items() if k != npyguard.ENV})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.split("\n")[0], "False", r.stdout)
            self.assertIn("baseline needs AVX-512", r.stdout)

    def test_entry_points_import_it_first(self):
        """Each entry point imports npyguard (the build tools: tools/npyfirst.py, which imports it) before any module
        outside the standard library (numpy directly or through a server module): the named ones, and every
        executable build tool (tools/build_*.py, bake_*.py, rebase*.py, profile_build.py) that imports numpy or a
        server module anywhere, in a function too (it runs after the module's top). portcheck's build child imports it
        before its numpy."""
        import fnmatch
        std = set(sys.stdlib_module_names)
        server_mods = {f[:-3] for f in os.listdir(SERVER) if f.endswith(".py")}

        def names_of(n):
            return ([a.name for a in n.names] if isinstance(n, ast.Import) else
                    [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
        tools = []
        for f in sorted(os.listdir(os.path.join(ROOT, "tools"))):
            if not any(fnmatch.fnmatch(f, p) for p in ("build_*.py", "bake_*.py", "rebase*.py", "profile_build.py")):
                continue
            with open(os.path.join(ROOT, "tools", f), encoding="utf-8") as fh:
                src = fh.read()
            if "__main__" in src and any(m.split(".")[0] == "numpy" or m.split(".")[0] in server_mods
                                         for n in ast.walk(ast.parse(src)) for m in names_of(n)):
                tools.append("tools/" + f)
        self.assertIn("tools/profile_build.py", tools)
        self.assertGreater(len(tools), 50)
        for rel in tuple(ENTRY_POINTS) + tuple(tools):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            guard = None
            for n in tree.body:
                names = [m for b in n.body for m in names_of(b)] if isinstance(n, ast.Try) else names_of(n)
                if "npyguard" in names or "npyfirst" in names:
                    guard = n.lineno
                    break
                bad = [m for m in names if m.split(".")[0] not in std]
                self.assertFalse(bad, f"{rel}:{n.lineno}: {bad} imported before npyguard")
            self.assertIsNotNone(guard, f"{rel}: no npyguard import at its top")
        with open(os.path.join(ROOT, "tools", "portcheck", "build.py"), encoding="utf-8") as fh:
            src = fh.read()
        child = src[src.index("def child("):]
        self.assertLess(child.index("import npyguard"), child.index("import numpy"))

    def test_npyfirst_applies_it(self):
        r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import npyfirst, os, npyguard; "
                            "print(repr(os.environ.get(npyguard.ENV)))" % os.path.join(ROOT, "tools")],
                           capture_output=True, text=True,
                           env=dict({k: v for k, v in os.environ.items() if k != npyguard.ENV},
                                    **{npyguard.FORCE_ENV: "1"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        want = " ".join(npyguard._probe() or ()) if npyguard._x86() else ""
        self.assertEqual(r.stdout.strip(), repr(want))

    def test_spawned_workers_inherit_it(self):
        """sharehelper's spawn pool (its children import blockworker before earthd): the parent's import sets the
        variable, the workers start with it and import numpy with no AVX-512 kernel (BNGE_NPYGUARD_FORCE: as on an
        AVX-512 CPU, so the names are really set)."""
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {SERVER!r})
            import sharehelper  # noqa: F401  (its top: the guard)
            import multiprocessing, os


            def check(_):
                import numpy  # noqa: F401
                import npyguard
                return os.environ.get(npyguard.ENV), npyguard.avx512_active()


            if __name__ == "__main__":
                with multiprocessing.get_context("spawn").Pool(2) as pool:
                    print(repr(pool.map(check, range(2))))
        """)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "spawn_check.py")
            with open(p, "w") as fh:
                fh.write(script)
            env = {k: v for k, v in os.environ.items() if k != npyguard.ENV}
            env[npyguard.FORCE_ENV] = "1"
            r = subprocess.run([sys.executable, p], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = eval(r.stdout.strip())   # noqa: S307 (the test's own output)
        want = " ".join(npyguard._probe() or ()) if npyguard._x86() else ""
        self.assertEqual(got, [(want, [])] * 2)


if __name__ == "__main__":
    unittest.main()
