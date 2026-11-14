#!/usr/bin/env python3
"""shortcells.py --cache DIR NAME=LAT,LON [...] [--reach M] [--server-dir DIR]: which map cells' road plans are made
SHORT round a place, and why.

A cell's plans made short of something (revisit.fault: a download that failed, the lidar rule for want of the lidar, an
underwater tunnel whose bore could not be solved ...) are not kept, and neither is anything built on them: the terrain
blocks, the road geotiles, the skins round that cell are built again on every visit (Times Square, 2026-11-13: one
cell short on every build, a warm revisit's graded block 38 s). This plans every cell within --reach m of each place
in one process (blockworker's own state: the road processes' code path, online unless --offline), each in a revisit
scope of its own, and prints per cell whether its plans are complete or the sites that made them short
(BNGE_REVISIT_SITES) and the warnings logged meanwhile; then per place the count. Run it twice on the same cache to
tell "short on a first visit" (downloads still coming) from "short on every visit". Never the real cache.

    python3 tools/shortcells.py --cache ~/bnge-tests/short/cache midtown=40.7580,-73.9855 loop=41.8819,-87.6278
"""
import argparse
import logging
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Grab(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.got = []

    def emit(self, record):
        try:
            self.got.append(f"{record.name.split('.')[-1]}: {record.getMessage()[:160]}")
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("places", nargs="+", help="NAME=LAT,LON")
    ap.add_argument("--cache", required=True)
    ap.add_argument("--reach", type=float, default=1500.0, help="m round each place whose cells are planned")
    ap.add_argument("--server-dir", default=os.path.join(ROOT, "server"))
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()
    real = os.path.realpath(os.path.expanduser("~/.cache/beamng-earth"))
    if os.path.realpath(a.cache).startswith(real):
        raise SystemExit("refusing the real cache: use a copy")
    os.makedirs(a.cache, exist_ok=True)
    os.environ["BNGE_REVISIT_SITES"] = "1"
    os.environ.setdefault("BNE_TELEMETRY", "off")
    sys.path.insert(0, a.server_dir)
    import numpy as np
    import blockworker
    import geodesy
    import grading as gradingmod
    import plancache
    import revisit
    plancache.start_run(a.cache)
    blockworker.init(a.cache, a.offline, True, True, True, True, None, logging.WARNING, True, True, 0, None, True)
    grab = _Grab()
    logging.getLogger().addHandler(grab)
    total = {}
    for aid, spec in enumerate(a.places, 1):
        name, _, ll = spec.partition("=")
        lat, lon = (float(v) for v in ll.split(","))
        g, _chunks = blockworker._s.anchor(aid, lat, lon)
        frame = geodesy.EnuFrame(lat, lon)
        n = max(2, int(a.reach // 250) + 1)
        xs = np.linspace(-a.reach, a.reach, 2 * n + 1)
        X, Y = np.meshgrid(xs, xs)
        la, lo, _ = frame.enu_to_geodetic(X.ravel(), Y.ravel(), 0.0)
        cells = gradingmod.cells_of_points(np.asarray(la), np.asarray(lo))
        short = []
        for k in sorted(cells, key=str):
            grab.got = []
            t0 = time.perf_counter()
            with revisit.scope() as b:
                try:
                    g.store.planner_of_cell(k)._cell_plans(k)
                    err = None
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"[:160]
            why = err or b.why_not()
            warns = sorted(set(grab.got))
            print(f"{name:10} cell {k}: {'complete' if not why else 'SHORT: ' + why} ({time.perf_counter() - t0:.1f} s)"
                  + ("".join(f"\n{'':14}{w}" for w in warns[:6]) if why else ""), flush=True)
            if why:
                short.append((str(k), why))
        total[name] = (len(short), len(cells), short)
    print()
    for name, (n, m, short) in total.items():
        print(f"{name}: {n} of {m} cells short" + ("".join(f"\n    {k}: {why}" for k, why in short)))


if __name__ == "__main__":
    main()
