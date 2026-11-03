#!/usr/bin/env python3
"""Run offline Lua unit/stub tests; this does not start or validate BeamNG/BeamMP.

Requires the development dependency lupa. Uses LuaJIT 2.1 for client tests and
Lua 5.3 for the BeamMP server plugin, in separate processes with a timeout.
"""
import argparse
import importlib
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "mod" / "tests"


def run_one(path, runtime):
    lua = importlib.import_module("lupa." + runtime).LuaRuntime()
    print(f"{path.name}: {lua.lua_implementation} (offline/stubbed)", flush=True)
    lua.globals().arg = lua.table_from({0: path.as_posix()})
    if path.stem == "test_scoping":
        lua.globals().BNE_TEST_LUA_FILES = lua.table_from(
            [p.as_posix() for p in sorted((ROOT / "mod" / "beamng_earth" / "lua").rglob("*.lua"))])
    # os.exit deliberately exits this child, preserving the test's exit code.
    lua.execute("dofile(...)", path.as_posix())
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tests", nargs="*", help="test stems or .lua filenames; default: all")
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--runtime", choices=("luajit21", "lua51", "lua53", "lua54"),
                    help="override automatic runtime selection")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.timeout <= 0:
        ap.error("--timeout must be positive")
    names = args.tests or [p.stem for p in sorted(TESTS.glob("test_*.lua"))]
    paths = [TESTS / (Path(name).stem + ".lua") for name in names]
    if any(not p.is_file() for p in paths):
        ap.error("unknown test: " + ", ".join(p.name for p in paths if not p.is_file()))
    if args.child:
        return run_one(paths[0], args.runtime)
    failures = []
    for path in paths:
        runtime = args.runtime or ("lua53" if path.stem == "test_beammp_plugin" else "luajit21")
        try:
            result = subprocess.run([sys.executable, __file__, "--child", "--runtime", runtime, path.stem],
                                    cwd=TESTS, timeout=args.timeout)
            if result.returncode:
                failures.append(path.stem)
        except subprocess.TimeoutExpired:
            print(f"TIMEOUT: {path.stem}", flush=True)
            failures.append(path.stem)
    print(f"Lua modules: {len(paths) - len(failures)} passed, {len(failures)} failed", flush=True)
    if failures:
        print("Failed: " + ", ".join(failures))
    return int(bool(failures))


if __name__ == "__main__":
    sys.exit(main())
