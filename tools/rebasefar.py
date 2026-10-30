#!/usr/bin/env python3
"""rebasefar.py LAT LON [--km 15] [--scale 1.0] [--offline]: what a rebase costs the far buildings, the skyline and the
far bridges, the anchor tiles' way and as geotiles (.claude/work/geotiles_plan.md phase 2).

The far trackers as earthd makes them (Connection.make_far_trackers, buildingScale --scale) are posed at the place from
an anchor KM west (the session before) and then from one at the place (the rebase), the jobs they start recorded. Then,
in one process like the server's far buildings process (blockworker, map data warm):
  - geotiles: the first visit's jobs built (blockworker.far_geo); after the rebase none (the mod re-places what it has);
  - anchor tiles (--geo-tiles off): the rebase's jobs built again for the new anchor (far_buildings_tile, skyline_tile,
    far_bridge_tile), in the trackers' order (far buildings first, then the skyline and the bridges, as earthd gates
    them), timing when the far buildings round the car are all in again (until then: the old anchor's ghosts).
Prints jobs, CPU-s, wall s, the mod's loads (tiles with a shape) per rebase and the time to the far buildings exact
after it. Map data from ~/.cache/beamng-earth; the revisit cache off. Run from the repository root."""
import argparse
import math
import os
import shutil
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "server"))
os.environ["BNE_REVISIT"] = "off"


class Loop:
    def __init__(self):
        self.futs = []

    def run_in_executor(self, pool, fn, *args):
        f = pool.submit(fn, *args)
        self.futs.append(f)
        return f


class Conn:
    def __init__(self):
        self.msgs, self.lock = [], threading.Lock()

    def send(self, m):
        with self.lock:
            self.msgs.append(m)


def jobs_of(geo, frames, lat, lon, scale):
    """[[(kind, args...)] per frame] the far trackers start, posed at (lat, lon) in each frame until all are in."""
    import earthd
    import geotiles
    from concurrent.futures import wait
    c = types.SimpleNamespace(bldfar=None, bldsky=None, brfar=None, building_scale=scale)
    earthd.Connection.make_far_trackers(c, geo)
    out, lock = [], threading.Lock()

    def mk(kind, aid):
        if geo:
            z = {"bldfar": geotiles.FAR_Z, "bldsky": geotiles.SKY_Z, "brfar": geotiles.BRIDGE_Z}[kind]

            def build(gx, gy):
                with lock:
                    out[-1].append((kind, gx, gy))
                return "/x/j.json", 1, geotiles.centre(gx, gy, z), 0.0, {}
            return build

        def build_a(a, tx, ty):
            with lock:
                out[-1].append((kind, a, tx, ty))
            return "/x/j.json", 1
        return build_a
    loop, conn = Loop(), Conn()
    trs = [(k, getattr(c, k)) for k in ("bldfar", "bldsky", "brfar") if getattr(c, k) is not None]
    for aid, f in frames:
        out.append([])
        x, y, _ = f.geodetic_to_enu(lat, lon, 0.0)
        for _ in range(1000):
            for k, tr in trs:
                if k != "bldfar" and c.bldfar.progress()[0] < c.bldfar.progress()[1]:
                    continue            # (earthd: the skyline and the bridges once the far buildings are in)
                tr.on_pose(conn, aid, float(x), float(y), loop, mk(k, aid), frame=f)
            wait(loop.futs)
            if all(tr.aid == aid and not tr.building and all(k_ in tr.sent for k_ in tr.car) for _k, tr in trs):
                break
    for _k, tr in trs:
        tr.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("--km", type=float, default=15.0)
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()
    import json
    import blockworker as bw
    import geodesy
    import revisit
    revisit.ON = False
    lat, lon = args.lat, args.lon
    lon_a = lon - args.km * 1000.0 / (111320.0 * math.cos(math.radians(lat)))
    fa, fb = geodesy.EnuFrame(lat, lon_a), geodesy.EnuFrame(lat, lon)
    geo_jobs = jobs_of(True, [(1, fa), (2, fb)], lat, lon, args.scale)
    old_jobs = jobs_of(False, [(1, fa), (2, fb)], lat, lon, args.scale)
    bw._s = bw._State(os.path.expanduser("~/.cache/beamng-earth"), args.offline, True, True, True, True, True, True)
    out = tempfile.mkdtemp(prefix="rebasefar_")
    zoom = 15
    try:
        def count(jobs):
            by = {}
            for j in jobs:
                by[j[0]] = by.get(j[0], 0) + 1
            return by
        # geotiles: the first visit (the map data warm for both after it)
        t0, c0 = time.perf_counter(), time.process_time()
        loads_geo, cpu_kind = 0, {}
        for kind, gx, gy in geo_jobs[0]:
            k0 = time.process_time()
            r = bw.far_geo(kind, gx, gy, zoom, out, "/x")
            cpu_kind[kind] = cpu_kind.get(kind, 0.0) + time.process_time() - k0
            if r is not None:
                loads_geo += len(r[4])
        w_geo, c_geo = time.perf_counter() - t0, time.process_time() - c0
        print(f"geotiles, first visit: {len(geo_jobs[0])} jobs {count(geo_jobs[0])}, {c_geo:.1f} CPU-s "
              f"({', '.join(f'{k} {v:.1f}' for k, v in cpu_kind.items())}), {w_geo:.1f} s wall in one process, "
              f"{loads_geo} shapes the mod loads")
        print(f"geotiles, the rebase ({args.km:g} km): {len(geo_jobs[1])} jobs, 0 shapes loaded again (re-placed in "
              f"the rebase's frame: the far buildings exact at once)")
        # the anchor tiles: the rebase's jobs for the new anchor
        t0, c0 = time.perf_counter(), time.process_time()
        far_done, loads_old, cpu_kind = None, 0, {}
        n_far = sum(1 for j in old_jobs[1] if j[0] == "bldfar")
        seen_far = 0
        for kind, aid, tx, ty in old_jobs[1]:
            k0 = time.process_time()
            if kind == "bldfar":
                r = bw.far_buildings_tile(aid, lat, lon, zoom, tx, ty, out, "/x")
            elif kind == "bldsky":
                r = bw.skyline_tile(aid, lat, lon, zoom, tx, ty, out, "/x")
            else:
                r = bw.far_bridge_tile(aid, lat, lon, zoom, tx, ty, out, "/x")
            cpu_kind[kind] = cpu_kind.get(kind, 0.0) + time.process_time() - k0
            if r is not None:
                d = json.load(open(os.path.join(out, os.path.basename(r[0]))))
                loads_old += 1 if d.get("shape") else 0
            if kind == "bldfar":
                seen_far += 1
                if seen_far == n_far:
                    far_done = time.perf_counter() - t0
        w_old, c_old = time.perf_counter() - t0, time.process_time() - c0
        print(f"anchor tiles, the rebase ({args.km:g} km): {len(old_jobs[1])} jobs {count(old_jobs[1])}, {c_old:.1f} "
              f"CPU-s ({', '.join(f'{k} {v:.1f}' for k, v in cpu_kind.items())}), {w_old:.1f} s wall in one process, "
              f"{loads_old} shapes the mod loads again; the far buildings round the car in again after "
              f"{far_done if far_done is not None else 0:.1f} s (their old anchor's ghosts until then)")
    finally:
        shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    main()
