#!/usr/bin/env python3
"""Profile the world server's builds where the map data is cached (no downloads timed): road tiles (with buildings,
sidewalks, street furniture), far building tiles and terrain blocks, in one process under cProfile. Prints each job's
wall time and the functions that took the most time, so a port (Rust, numba) goes where it pays.

    python3 tools/profile_build.py --at 36.17,-115.145 --tiles 2 --out /tmp/prof.txt
"""
import argparse
import cProfile
import os
import pstats
import queue
import sys
import time
try:        # (first: numpy's AVX-512 kernels held back before anything imports numpy, tools/npyfirst.py)
    import npyfirst  # noqa: E402,F401
except ImportError:     # (loaded by its path into another process, a test: that process's entry point guards)
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--at", default="36.17,-115.145", help="lat,lon (an area whose map data is cached)")
    ap.add_argument("--tiles", type=int, default=1, help="road tiles from -n to n on each axis (256 m each)")
    ap.add_argument("--blocks", type=int, default=2, help="terrain blocks to build")
    ap.add_argument("--cache-dir", default=os.path.expanduser("~/.cache/beamng-earth"))
    ap.add_argument("--out", default=None, help="write the full stats here too")
    ap.add_argument("--top", type=int, default=35)
    ap.add_argument("--no-profile", action="store_true", help="times only (cProfile's overhead off)")
    ap.add_argument("--dump", default=None, help="save the raw profile here (pstats, e.g. for print_callers)")
    a = ap.parse_args()
    lat, lon = (float(v) for v in a.at.split(","))

    import blockworker
    blockworker.init(a.cache_dir, True, True, True, True, True, queue.Queue(), 30, True, True)
    import earthd
    import geodesy
    import dem as demmod
    out_dir = os.path.join("/tmp", "bnge_profile")
    os.makedirs(out_dir, exist_ok=True)
    zoom = demmod.zoom_for(lat, 2.5)
    times = {}
    cpu = {}
    prof = cProfile.Profile()

    def timed(name, fn, *args, **kw):
        t0 = time.perf_counter()
        c0 = time.process_time()
        if not a.no_profile:
            prof.enable()
        try:
            fn(*args, **kw)
        except Exception as e:      # a missing cell offline: reported, the rest goes on
            print(f"{name}: {type(e).__name__}: {e}", file=sys.stderr)
        finally:
            prof.disable()
        times.setdefault(name.split("(")[0], []).append(time.perf_counter() - t0)
        cpu.setdefault(name.split("(")[0], []).append(time.process_time() - c0)

    # road tiles (with buildings, sidewalks, furniture)
    n = a.tiles
    for tx in range(-n, n + 1):
        for ty in range(-n, n + 1):
            timed(f"roads_tile({tx},{ty})", blockworker.roads_tile, 1, lat, lon, zoom, tx, ty, out_dir, "/temp/x",
                  True, 0.0, True, False, True)
    # far building tiles
    for tx in range(-n, n + 1):
        timed(f"far_buildings({tx},0)", blockworker.far_buildings_tile, 1, lat, lon, zoom, tx, 0, out_dir, "/temp/x")
    # terrain blocks (the 512 block's 128 m grid)
    frame = geodesy.EnuFrame(lat, lon)
    q = earthd.survey_q(frame, blockworker._s.dem, 1024)
    for k in range(a.blocks):
        cx = 128.0 * k
        timed(f"block({cx},0)", blockworker.build, 1, lat, lon, zoom, q, cx, 0.0, 512, out_dir, f"hm_{k}.png",
              f"hm_{k}.ter")

    print("job wall times:")
    for k, v in times.items():
        c = cpu[k]
        print(f"  {k}: {len(v)} x, avg {sum(v) / len(v):.2f} s, total {sum(v):.1f} s; CPU (all threads) avg "
              f"{sum(c) / len(c):.2f} s, total {sum(c):.1f} s")
    if a.no_profile:
        return
    st = pstats.Stats(prof)
    print("\nby own time (tottime):")
    st.sort_stats("tottime").print_stats(a.top)
    print("\nby cumulative time:")
    st.sort_stats("cumulative").print_stats(a.top)
    if a.dump:
        prof.dump_stats(a.dump)
    if a.out:
        with open(a.out, "w") as f:
            pstats.Stats(prof, stream=f).sort_stats("tottime").print_stats(200)


if __name__ == "__main__":
    main()
