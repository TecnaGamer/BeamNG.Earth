#!/usr/bin/env python3
"""The mod's Lua tests (mod/tests/test_*.lua) without a luajit binary: each file under the LuaJIT that the lupa package
bundles (pip install lupa), in its own process, as `luajit mod/tests/<file>` runs it (arg[0] is the file, the tests'
folder the working directory). For Windows contributors and anyone with Python but no luajit on the PATH; with luajit
installed the README's loop (cd mod/tests && for f in test_*.lua; do luajit $f; done) is the same thing.

  python3 tools/run_lua_tests.py                    every mod/tests/test_*.lua
  python3 tools/run_lua_tests.py test_gps test_places  just these

Prints each file's last line (testlib's "<name>: N passed, M failed") and exits 1 when any file failed or errored.
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(os.path.dirname(HERE), "mod", "tests")
TIMEOUT_S = 120


def run_one(path):
    """Run one Lua file under lupa's LuaJIT in this process (the child side of main)."""
    try:
        from lupa import luajit21 as lupa_rt
    except ImportError:
        try:
            from lupa import luajit20 as lupa_rt
        except ImportError:
            sys.exit("run_lua_tests: no LuaJIT in lupa (pip install lupa)")
    path = os.path.abspath(path)
    os.chdir(os.path.dirname(path))
    rt = lupa_rt.LuaRuntime()
    rt.execute("arg = {}")
    rt.globals().arg[0] = path
    run = rt.eval("function(p) local ok, err = pcall(dofile, p) if not ok then io.stderr:write(tostring(err), '\\n')"
                  " os.exit(1) end end")
    run(path)


def main(argv):
    if len(argv) == 2 and argv[0] == "--one":
        run_one(argv[1])
        return 0
    names = argv or sorted(os.path.basename(p)[:-4] for p in glob.glob(os.path.join(TESTS, "test_*.lua")))
    bad = []
    for n in names:
        path = os.path.join(TESTS, n if n.endswith(".lua") else n + ".lua")
        out = []
        try:
            r = subprocess.run([sys.executable, os.path.abspath(__file__), "--one", path], capture_output=True,
                               text=True, timeout=TIMEOUT_S)
            out = (r.stdout + r.stderr).strip().splitlines()
            last = out[-1] if out else "(no output)"
            ok = r.returncode == 0 and " 0 failed" in last
        except subprocess.TimeoutExpired:
            last, ok = f"timed out after {TIMEOUT_S} s", False
        print(f"{'ok  ' if ok else 'FAIL'} {n}: {last}", flush=True)
        if not ok:
            bad.append(n)
            for line in out[-15:]:
                print("     " + line)
    print(f"{len(names) - len(bad)} of {len(names)} Lua test files passed" + (f"; failed: {', '.join(bad)}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
