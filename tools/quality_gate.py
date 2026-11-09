#!/usr/bin/env python3
"""Bounded foundation regression gate. Uses synthetic data, not a gameplay benchmark.

Run after installing server/requirements.txt, lupa and scipy and building tools/build_native.py.
The existing full server suite remains available through tools/run_tests.py.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "test_protocol", "test_protocol_agreement", "test_geodesy", "test_cache",
    "test_budget", "test_class_grid", "test_install", "test_updates", "test_selfupdate", "test_bmplaunch",
    "test_launcher_linux", "test_launcher_lifecycle", "test_process_lifecycle",
    "test_connection_cleanup", "test_initial_loading", "test_tile_ownership", "test_matslots", "test_stream_reconnect",
    "test_netfetch", "test_mapdata", "test_rangeread", "test_grading", "test_roadz", "test_roadz_inputs",
    "test_road_seams", "test_roadgeo", "test_fast", "test_restart",
    "test_revisit", "test_plancache", "test_bake_formats", "test_chunkcache", "test_pools",
    "test_prefetch_chain", "test_demand", "test_roadwidth", "test_roadcorners",
    "test_sketchfab_assets", "test_subsea_tunnels", "test_tunnels",
    "test_worldbundle", "test_world_cache", "test_peerwork",
    "test_host_package", "test_carriageway_solver",
    "test_portugal_imagery",
    "test_rail_crossing_assets",
    "test_native_stress",
    "test_bridge_level_spans",
    "test_host_package", "test_container",
    "test_designs", "test_de_signs", "test_germany", "test_lane_rules", "test_regionaldata",
)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--par", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=600, help="seconds per Python test module")
    args = ap.parse_args()
    if args.par < 1 or args.timeout < 1:
        ap.error("--par and --timeout must be positive")
    # Optional cached-world tests must not silently change the CI workload on a developer's machine.
    os.environ["BNGE_ROADGEO_REAL"] = "0"
    # Fail before tests instead of accepting skipped native/data-provider coverage.
    sys.path.insert(0, str(ROOT / "server"))
    import fast
    import duckdb
    import fsspec
    import lupa.luajit21
    import lupa.lua53
    import scipy.optimize       # test-only independent reference for the native carriageway QP
    fast.require(fast.MIN_VERSION, "quality gate")
    print(f"Python {sys.version.split()[0]}, earthfast {fast._ef.VERSION}, "
          f"DuckDB {duckdb.__version__}, fsspec {fsspec.__version__}", flush=True)
    commands = [
        [sys.executable, "tools/run_tests.py", *MODULES, "--par", str(args.par),
         "--timeout", str(args.timeout), "--logs", "build/quality-tests"],
        [sys.executable, "tools/run_lua_tests.py"],
        ["node", "--test", "mod/tests/test_mapmath.mjs", "server/tests/test_mapmath.mjs"],
        ["cargo", "test", "--locked", "--manifest-path", "native/serverlist/Cargo.toml"],
        [sys.executable, "tools/package.py", "--beammp", "--out", "build/quality-packages"],
    ]
    for command in commands:
        print("+ " + " ".join(command), flush=True)
        result = subprocess.run(command, cwd=ROOT)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
