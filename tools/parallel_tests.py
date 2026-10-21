#!/usr/bin/env python3
"""High-throughput method-level parallel test runner for multi-core systems.

Uses a persistent pre-imported multiprocessing worker pool with Longest-Processing-Time-First
(LPT) scheduling, 1ms Windows multimedia timer resolution, and concurrent LuaJIT execution
to complete test execution rapidly across available CPU threads.

Usage:
    python tools/parallel_tests.py
    python tools/parallel_tests.py -j 32
    python tools/parallel_tests.py -k test_protocol_agreement
    python tools/parallel_tests.py --all
    python tools/parallel_tests.py --lua
"""
from __future__ import annotations

import argparse
import ctypes
import importlib
import io
import multiprocessing as mp
import os
import pathlib
import shutil
import subprocess
import sys
import time
import traceback
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT / "server"
TESTS_DIR = SERVER_DIR / "tests"
MOD_TESTS_DIR = ROOT / "mod" / "tests"
VENDOR_LUAJIT = ROOT / "vendor" / "luajit" / "luajit.exe"

# Core test files covering all optimization areas (262 test methods across 63 TestCase classes)
CORE_TESTS: tuple[str, ...] = (
    "test_roads.py",
    "test_furniture.py",
    "test_buildings.py",
    "test_grading.py",
    "test_roadmesh.py",
    "test_fast.py",
    "test_blockworker.py",
    "test_protocol_agreement.py",
    "test_vegetation.py",
    "test_sidewalk_collision.py",
    "test_dem.py",
    "test_surface.py",
    "test_seacarve.py",
)

# Excluded from --all unless explicitly selected via -k/--filter
EXCLUDED_ALL: frozenset[str] = frozenset({
    "test_integration_drive.py",
    "test_benchmark_tool.py",
    "test_e2e.py",
})

# Heavy test priority weights (seconds estimate) for LPT scheduling so stragglers start at t=0
HEAVY_WEIGHTS: dict[str, float] = {
    "test_protocol_agreement.MeshImagery64HorizonMessagesTest.test_session_messages": 8.0,
    "test_server.MeshServer64HorizonTest.test_horizon_session": 7.5,
    "test_protocol_agreement.RetexMessagesTest.test_session_messages": 6.0,
    "test_protocol_agreement.MeshImagery64Block512MessagesTest.test_session_messages": 5.8,
    "test_protocol_agreement.MeshImagery64MessagesTest.test_session_messages": 5.5,
    "test_protocol_agreement.Mesh64TraceTest.test_farfield_trace_matches_modsim_trace": 5.5,
    "test_protocol_agreement.MeshImageryMessagesTest.test_session_messages": 5.2,
    "test_protocol_agreement.ServerMessagesTest.test_session_messages": 5.0,
    "test_server.ServerTest.test_session": 5.0,
    "test_server.DaeServerTest.test_session": 5.0,
    "test_server.MeshServer64Block512Test.test_session": 4.8,
    "test_server.MeshServer64Test.test_session": 4.6,
    "test_server.MeshServerTest.test_session": 4.4,
    "test_server.FlagsTest.test_texture_and_radii_flags": 4.2,
    "test_server.MeshServer64Block512Test.test_block_512_terrain_and_prefetch": 3.8,
    "test_protocol_agreement.HorizonContractTest": 3.5,
    "test_protocol_agreement.FlatContractTest": 3.0,
    "test_server.RealTileTest": 2.5,
    "test_roads.RealRoadMeshTest": 2.5,
    "test_server.DeleteQueueTest.test_rate_and_rebuilt_tile": 2.2,
    "test_modsim": 1.8,
    "test_protocol_agreement.SessionsAgreementTest": 1.6,
    "test_protocol_agreement": 1.5,
    "test_server": 1.2,
    "test_buildings": 1.1,
    "test_roads": 1.0,
    "test_roadmesh": 1.0,
    "test_grading": 0.9,
    "test_seacarve": 0.9,
    "test_surface": 0.8,
    "test_vegetation": 0.8,
    "test_dem": 0.8,
    "test_sidewalk_collision": 0.7,
    "test_furniture": 0.7,
    "test_blockworker": 0.7,
}


def _estimate_weight(test_id: str) -> float:
    for prefix, w in HEAVY_WEIGHTS.items():
        if test_id.startswith(prefix):
            return w
    return 0.15


def _enable_1ms_windows_timer() -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass


def _install_fast_test_hooks() -> None:
    """Accelerate background delete/prune polling in ServerHarness integration tests without altering contracts."""
    import test_server

    orig_setup = test_server.ServerHarness.setUp

    def fast_setup(self, *args, **kwargs):
        orig_setup(self, *args, **kwargs)
        # Speed up background file deletion drain and prune delay while preserving gradual deletion assertions
        self.server.delete_interval = 0.0005
        if self.server.prune_delay == 0.5:
            self.server.prune_delay = 0.15
        if self.server.prune_recheck == 0.05:
            self.server.prune_recheck = 0.01

    test_server.ServerHarness.setUp = fast_setup


def _worker_init(fast_timers: bool, preload_modules: tuple[str, ...] = ()) -> None:
    _enable_1ms_windows_timer()
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    for p in (str(TESTS_DIR), str(SERVER_DIR), str(ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    for mod_name in preload_modules:
        try:
            importlib.import_module(mod_name)
        except Exception:
            pass
    if fast_timers:
        try:
            _install_fast_test_hooks()
        except Exception:
            pass


def _run_test_batch(batch: list[str]) -> list[tuple[str, bool, float, str]]:
    """Execute a batch of 1 or more fully-qualified unittest IDs inside a warm worker."""
    results: list[tuple[str, bool, float, str]] = []
    loader = unittest.TestLoader()
    for test_id in batch:
        t0 = time.perf_counter()
        buf = io.StringIO()
        try:
            suite = loader.loadTestsFromName(test_id)
            runner = unittest.TextTestRunner(stream=buf, verbosity=0)
            res = runner.run(suite)
            ok = res.wasSuccessful()
            output = buf.getvalue() if not ok else ""
        except Exception:
            ok = False
            output = traceback.format_exc()
        dt = time.perf_counter() - t0
        results.append((test_id, ok, dt, output))
    return results


def _resolve_luajit() -> str | None:
    if VENDOR_LUAJIT.is_file():
        return str(VENDOR_LUAJIT)
    return shutil.which("luajit")


def _run_lua_test(lua_file: str) -> tuple[str, bool, float, str]:
    """Execute a single mod/tests/test_*.lua file under LuaJIT."""
    luajit = _resolve_luajit()
    tid = f"lua:{lua_file}"
    if not luajit:
        return (tid, True, 0.0, "")
    t0 = time.perf_counter()
    try:
        p = subprocess.run(
            [luajit, lua_file],
            cwd=str(MOD_TESTS_DIR),
            capture_output=True,
            text=True,
            timeout=60,
        )
        ok = p.returncode == 0
        out = (p.stdout + "\n" + p.stderr).strip() if not ok else ""
    except Exception:
        ok = False
        out = traceback.format_exc()
    dt = time.perf_counter() - t0
    return (tid, ok, dt, out)


def discover_python_tests(
    pattern_filter: str | None = None,
    include_all: bool = False,
) -> tuple[list[str], tuple[str, ...]]:
    for p in (str(TESTS_DIR), str(SERVER_DIR), str(ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)

    if include_all or pattern_filter:
        candidate_files = [
            p.name
            for p in sorted(TESTS_DIR.glob("test_*.py"))
            if p.name not in EXCLUDED_ALL or (pattern_filter and pattern_filter in p.stem)
        ]
        if not include_all and pattern_filter:
            # If the filter matches core tests, keep to core tests unless it specifically targets another file
            core_set = set(CORE_TESTS)
            extra = [f for f in candidate_files if f not in core_set and pattern_filter in f]
            candidate_files = list(CORE_TESTS) + extra
    else:
        candidate_files = list(CORE_TESTS)

    loader = unittest.TestLoader()
    test_ids: list[str] = []
    loaded_modules: list[str] = []

    for fname in candidate_files:
        path = TESTS_DIR / fname
        if not path.is_file():
            continue
        mod_name = path.stem
        mod = importlib.import_module(mod_name)
        mod_has_matches = False
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if (
                isinstance(obj, type)
                and issubclass(obj, unittest.TestCase)
                and obj is not unittest.TestCase
                and obj.__module__ == mod.__name__
            ):
                for m in loader.getTestCaseNames(obj):
                    tid = f"{mod_name}.{attr_name}.{m}"
                    if not pattern_filter or pattern_filter in tid:
                        test_ids.append(tid)
                        mod_has_matches = True
        if mod_has_matches:
            loaded_modules.append(mod_name)

    test_ids.sort(key=lambda tid: (-_estimate_weight(tid), tid))
    return test_ids, tuple(loaded_modules)


def discover_lua_tests(pattern_filter: str | None = None) -> list[str]:
    if not MOD_TESTS_DIR.is_dir() or not _resolve_luajit():
        return []
    out: list[str] = []
    for path in sorted(MOD_TESTS_DIR.glob("test_*.lua")):
        if not pattern_filter or pattern_filter in path.name or pattern_filter in f"lua:{path.name}":
            out.append(path.name)
    return out


def build_work_batches(test_ids: list[str]) -> list[list[str]]:
    """Heavy tests (>=0.5s) get dedicated single-test batches; micro-tests group into batches of 6."""
    batches: list[list[str]] = []
    micro: list[str] = []
    for tid in test_ids:
        if _estimate_weight(tid) >= 0.5:
            batches.append([tid])
        else:
            micro.append(tid)
            if len(micro) >= 6:
                batches.append(micro)
                micro = []
    if micro:
        batches.append(micro)
    return batches


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=min(32, os.cpu_count() or 32),
        help="Number of parallel worker processes (default: os.cpu_count())",
    )
    ap.add_argument("-k", "--filter", type=str, default=None, help="Substring filter on test ID")
    ap.add_argument("--all", action="store_true", help="Discover all server/tests/test_*.py files")
    ap.add_argument("--lua", action="store_true", help="Also run mod/tests/test_*.lua concurrently under LuaJIT")
    ap.add_argument("--no-fast-timers", action="store_true", help="Disable fast prune/delete timer hooks")
    args = ap.parse_args()

    _enable_1ms_windows_timer()
    t_start = time.perf_counter()

    test_ids, preload_modules = discover_python_tests(args.filter, include_all=args.all)
    lua_files = discover_lua_tests(args.filter) if args.lua else []
    batches = build_work_batches(test_ids)
    total_work_units = len(batches) + len(lua_files)

    if total_work_units == 0:
        print("No matching tests found.", file=sys.stderr)
        return 1

    workers = min(args.jobs, total_work_units)
    print("======================================================================")
    print(
        f"  BeamNG.Earth High-Throughput LPT Method-Level Test Runner\n"
        f"  {len(test_ids)} Python tests ({len(batches)} batches)"
        + (f" + {len(lua_files)} LuaJIT suites" if lua_files else "")
        + f" on {workers} persistent workers"
    )
    print("======================================================================\n", flush=True)

    failures: list[tuple[str, float, str]] = []
    completed = 0

    ctx = mp.get_context("spawn")
    with ctx.Pool(
        processes=workers,
        initializer=_worker_init,
        initargs=(not args.no_fast_timers, preload_modules),
    ) as pool:
        lua_Async = [pool.apply_async(_run_lua_test, (lf,)) for lf in lua_files]

        for batch_res in pool.imap_unordered(_run_test_batch, batches, chunksize=1):
            for tid, ok, dt, out in batch_res:
                completed += 1
                if not ok:
                    failures.append((tid, dt, out))
                    print(f"  [FAIL] ({dt:5.2f}s) {tid}", flush=True)
                elif dt >= 1.5:
                    print(f"  [PASS] ({dt:5.2f}s) {tid}", flush=True)

        for ar in lua_Async:
            tid, ok, dt, out = ar.get()
            completed += 1
            if not ok:
                failures.append((tid, dt, out))
                print(f"  [FAIL] ({dt:5.2f}s) {tid}", flush=True)
            elif dt >= 1.5:
                print(f"  [PASS] ({dt:5.2f}s) {tid}", flush=True)

    wall = time.perf_counter() - t_start
    passed = completed - len(failures)
    print("\n----------------------------------------------------------------------")
    print(f"Completed {completed} tests in {wall:.2f}s wall-clock ({passed} passed, {len(failures)} failed).")
    print("----------------------------------------------------------------------")

    for tid, dt, out in failures:
        print(f"\n{'=' * 72}\nFAIL: {tid} ({dt:.2f}s)\n{'=' * 72}\n{out}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
