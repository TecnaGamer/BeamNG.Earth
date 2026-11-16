"""Per-pose cost of the far field's event-loop work, offline (no game, no network, no world builds).

A Connection on a fresh anchor (Baltimore, the horizon on with a synthetic zmax grid and far PVS ground, global far
tiles, the screen-space LOD camera, stub imagery), every tile of its selection taken as built and shown after each pose
(the steady state of a drive: ~600 far tiles), then a pose stream at the mod's 2 Hz (POSE_INTERVAL) of a car driving
at SPEED m/s along a gentle curve. Each pose runs Connection.on_pose as the server does, then the callbacks it leads to
(the far PVS's set coming in); the event-loop thread's CPU time of each (time.thread_time) is printed (median, p90,
max, total) and, with --profile, the cProfile top.

    python3 bench_pose.py [--poses 240] [--speed 30] [--lod-scale 1.5] [--profile]

Run it under `nice -n 19 taskset -c 14,15` on a shared PC. BNGE_BENCH_THREADS=N adds N busy Python threads (the GIL
contention of a drive's imagery / lidar / net pools); then the wall time per pose is printed too.
"""
import argparse
import asyncio
import cProfile
import os
import pstats
import statistics
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("BNGE_LOG", "warning")

import numpy as np  # noqa: E402

import dmath  # noqa: E402
import dnp  # noqa: E402
import earthd  # noqa: E402
import farfield as ff  # noqa: E402

LAT, LON = 39.2847, -76.5928        # Baltimore (Fells Point)
X0, Y0 = 137.3, -61.9               # the start (off the lattice: at 0, 0 every bound is a whole number of tiles away)


class _Writer:
    def __init__(self):
        self.n = 0

    def write(self, data):
        self.n += 1

    def close(self):
        pass

    def is_closing(self):
        return False

    def get_extra_info(self, name):
        return ("127.0.0.1", 0)


def synthetic_grid(half, seed=7):
    """A zmax grid over [-half, half]²: a coastal plain rising to hills inland (0-600 m), deterministic."""
    s = ff.level_size(ff.HORIZON_GRID_LEVEL)
    n = int(round(2 * half / s))
    rng = np.random.default_rng(seed)
    i = np.arange(n) - n / 2
    X, Y = np.meshgrid(i, i)
    base = np.clip(40.0 * (X - Y) + rng.uniform(0.0, 120.0, (n, n)), 0.0, 600.0)
    return ff.HorizonGrid(half, base)


class FakeDem:
    """Synthetic ground for the far PVS's rings: a plain-to-hills ramp with ridges (lat / lon in)."""

    def sample(self, lat, lon, zoom, progress=None):
        lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
        x, y = (lon - LON) * 86000.0, (lat - LAT) * 111000.0
        return np.clip((x - y) / 400.0, 0.0, 600.0) + 60.0 * (dnp.sin(x / 3000.0) * dnp.cos(y / 2300.0) + 1.0)


class StubImagery:
    """What the far field's per-pose code asks the imagery for (the US: USGS's z16), no fetches."""
    chain = ()

    def sharp_zoom(self, lat, lon, reach=None):
        return 16

    def far_warm(self, *a, **kw):
        pass

    def __getattr__(self, name):
        return lambda *a, **kw: None


def make(tmp, lod_scale):
    s = earthd.Server(tmp, os.path.join(tmp, "cache"), imagery=None, cells=64, block=1024, near_visual="mesh",
                      horizon=True)
    s.global_far = True
    s.dem = FakeDem()
    s.imagery = StubImagery()
    c = earthd.Connection(s, None, _Writer())
    c.global_far = True
    a = s.new_anchor(LAT, LON)
    a.first_terrain = (0.0, 0.0)
    s.anchors[a.id] = a
    eye = 12.0
    s.horizon_grids[a.id] = synthetic_grid(ff.horizon_extent(eye, earthd.ANCHOR_CENTRE_MAX))
    c.active = a.id
    c.focus = (0.0, 0.0)
    c.eye = c.eye_now = eye
    c._set_block(a.id, 0.0, 0.0)
    c.lod_scale = lod_scale
    return s, c, a


def pose_msg(aid, t, speed):
    """The car at time t on a circle of 6 km radius (a gentle curve), heading along it."""
    r = 6000.0
    a = speed * t / r
    x, y = X0 + r * dmath.sin(a), Y0 + r * (1.0 - dmath.cos(a))
    vx, vy = speed * dmath.cos(a), speed * dmath.sin(a)
    return {"type": "pose", "anchorId": aid, "x": x, "y": y, "z": 10.0, "vx": vx, "vy": vy, "lat": LAT, "lon": LON,
            "speed": speed, "follow": True,
            "cam": {"x": x - 6.0 * dmath.cos(a), "y": y - 6.0 * dmath.sin(a), "z": 12.0, "fx": dmath.cos(a),
                    "fy": dmath.sin(a), "fz": -0.05, "fovDeg": 65.0, "h": 1080}}


def settle(c):
    """Every desired tile built and swapped in (the mod shows them all, none sent and waiting for its group): the
    steady state of a drive. Each has a server record (its texture its level's size), as a built tile has."""
    c.sent.clear()
    c.shown = {c.ck(k): t.bbox for k, t in c.desired.items()}
    s = c.s
    for k, t in c.desired.items():
        ck = c.ck(k)
        if ck not in s.tiles:
            node = t.base if isinstance(t, ff.Piece) else t
            s.tiles[ck] = earthd.TileInfo(pos=[0.0, 0.0, 0.0], err=0.5, sink=None, skirt=2.0, grid=None, bytes=0,
                                          seconds=0.0, material=f"m{len(s.tiles)}", texture="t.dds",
                                          tex_px=s.tex_size(node.level), tile=t)


async def drain(c):
    """Let the far PVS's worker finish and its callbacks run on the loop (they are the loop's work too)."""
    for _ in range(500):
        await asyncio.sleep(0)
        if getattr(c, "pvs_job", None) is None:
            break
        await asyncio.sleep(0.002)
    for _ in range(3):
        await asyncio.sleep(0)


BREAKDOWN = {}          # name -> [calls, main-thread CPU s]
WATCH = [("earthd", "Connection", n) for n in ("update_far", "horizon_select", "_regroup", "pvs_tick", "pvs_reveal",
                                               "far_lazy_tick", "send_progress", "refresh_status", "maybe_prefetch",
                                               "emit_groups")] + \
        [("earthd", "Server", n) for n in ("tile_tex_px", "pvs_px", "up_px")] + \
        [("farfield", None, n) for n in ("select_global", "select", "swap_groups", "view_distances")]


def watch():
    """Wrap the WATCH functions with a main-thread CPU (time.thread_time) tally, outermost calls only."""
    main = threading.main_thread()
    import importlib
    for mod, cls, name in WATCH:
        owner = importlib.import_module(mod)
        if cls:
            owner = getattr(owner, cls)
        f = getattr(owner, name)
        st = BREAKDOWN.setdefault(name, [0, 0.0, 0])

        def wrap(*a, _f=f, _st=st, **kw):
            if threading.current_thread() is not main:
                return _f(*a, **kw)
            _st[2] += 1
            t0 = time.thread_time()
            try:
                return _f(*a, **kw)
            finally:
                _st[2] -= 1
                if _st[2] == 0:
                    _st[0] += 1
                    _st[1] += time.thread_time() - t0
        setattr(owner, name, wrap)
        if mod == "farfield":           # (earthd calls them through the module)
            setattr(earthd.farfield, name, wrap)


class _SyncLoop:
    """run_in_executor done at once in the caller (a finished future): the trackers' builds land within the pass."""

    def run_in_executor(self, pool, fn, *args):
        from concurrent.futures import Future
        f = Future()
        f.set_result(fn(*args))
        return f


def make_trackers(c):
    """The geotile trackers a drive passes at every pose (vegetation.Tracker: the trees, the far buildings, the packs,
    the skyline, the far bridges, as Connection.make_far_trackers makes them), their builds instant (no files)."""
    import buildings as bm
    import geotiles
    import vegetation as vm
    far_m, far_drop = bm.FAR_SEND_M, bm.FAR_DROP_M
    inner = min(far_m, bm.PACK_INNER_M)
    return [
        ("veg", vm.Tracker(per_pose=64, geo=True)),
        ("bldfar", vm.Tracker(inner, inner + (far_drop - far_m), bm.FAR_PER_POSE, "bldfar", geo=True,
                              z=geotiles.FAR_Z, group_z=geotiles.SKY_Z)),
        ("bldpack", vm.Tracker(far_m, far_drop, bm.PACK_PER_POSE, "bldpack", geo=True, z=geotiles.SKY_Z,
                               inner=(inner, 1))),
        ("bldsky", vm.Tracker(bm.SKY_SEND_M, bm.SKY_DROP_M, bm.SKY_PER_POSE, "bldsky", geo=True, z=geotiles.SKY_Z,
                              inner=(far_m, bm.SKY_UNIT))),
        ("brfar", vm.Tracker(bm.BRIDGE_FAR_SEND_M, bm.BRIDGE_FAR_SEND_M + 512.0, bm.BRIDGE_FAR_PER_POSE, "brfar",
                             geo=True, z=geotiles.BRIDGE_Z)),
    ]


def tracker_pass(c, trackers, aid, msg, loop):
    import geotiles
    frame = c.s.anchors[aid].frame
    for _name, tr in trackers:
        z = tr.z or geotiles.Z

        def build(gx, gy, z=z):
            return "/vfs/none.json", 1, tuple(geotiles.centre(gx, gy, z)), 0.0
        tr.on_pose(c, aid, msg["x"], msg["y"], loop, build, frame=frame)


def busy(stop):
    x = 0
    while not stop.is_set():
        for i in range(1000):
            x += i * i


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--poses", type=int, default=240)
    ap.add_argument("--speed", type=float, default=30.0)
    ap.add_argument("--lod-scale", type=float, default=1.5, help="the mod's terrain detail (lodScale)")
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--breakdown", action="store_true", help="main-thread CPU per watched function")
    ap.add_argument("--trackers", action="store_true", help="the trees' and far buildings' geotile trackers too")
    args = ap.parse_args()
    nthreads = int(os.environ.get("BNGE_BENCH_THREADS", "0"))

    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            s, c, a = make(tmp, args.lod_scale)
            end = time.monotonic() + 120.0
            for k in range(1000):           # (until the far PVS's rings and first set are in)
                c.on_pose(pose_msg(a.id, 0.0, args.speed))
                await drain(c)
                settle(c)
                if k >= 2 and (c.pvs is not None or not s.pvs_on or time.monotonic() > end):
                    break
                if s.pvs_building:
                    await asyncio.sleep(0.05)
            print(f"far tiles: {len(c.desired)} (radius {c.radius / 1000:.0f} km, PVS {'on' if c.pvs else 'off'})")
            stop = threading.Event()
            ths = [threading.Thread(target=busy, args=(stop,), daemon=True) for _ in range(nthreads)]
            for th in ths:
                th.start()
            prof = cProfile.Profile() if args.profile else None
            if args.breakdown:
                watch()
            trackers = make_trackers(c) if args.trackers else []
            sync = _SyncLoop()
            for _ in range(200):            # (every tile round the start sent: the steady state)
                n0 = sum(len(tr.sent) for _n, tr in trackers)
                tracker_pass(c, trackers, a.id, pose_msg(a.id, 0.0, args.speed), sync)
                if sum(len(tr.sent) for _n, tr in trackers) == n0:
                    break
            if trackers:
                print("tracker tiles sent:", {n: len(tr.sent) for n, tr in trackers})
            cpu, wall, tcpu = [], [], []
            for p in range(args.poses):
                msg = pose_msg(a.id, p * 0.5, args.speed)
                t0, w0 = time.thread_time(), time.perf_counter()
                if prof:
                    prof.enable()
                c.on_pose(msg)
                await drain(c)
                t1 = time.thread_time()
                tracker_pass(c, trackers, a.id, msg, sync)
                if prof:
                    prof.disable()
                tcpu.append(time.thread_time() - t1)
                cpu.append(time.thread_time() - t0)
                wall.append(time.perf_counter() - w0)
                settle(c)                   # (the new tiles built and swapped in before the next pose)
            stop.set()
            for th in ths:
                th.join()

            def line(name, v):
                ms = sorted(t * 1000 for t in v)
                return (f"{name}: median {statistics.median(ms):.2f} ms, p90 {ms[int(0.9 * len(ms))]:.2f} ms, "
                        f"max {ms[-1]:.2f} ms, total {sum(ms):.0f} ms")
            print(f"poses {len(cpu)}, far tiles at the end {len(c.desired)}, PVS sets {s.pvs_stats['sets']}")
            print(line("loop CPU per pose", cpu))
            if trackers:
                print(line("  of it the geotile trackers", tcpu))
            if nthreads:
                print(line(f"wall per pose ({nthreads} busy threads)", wall))
            if args.breakdown:
                for name, (n, t, _) in sorted(BREAKDOWN.items(), key=lambda kv: -kv[1][1]):
                    if n:
                        print(f"  {name:16s} {n:5d} calls  {t * 1000 / len(cpu):7.2f} ms per pose")
            if prof:
                prof.dump_stats(os.environ.get("BNGE_BENCH_PROF", os.devnull))
                pstats.Stats(prof).sort_stats("tottime").print_stats(args.top)
            for pool in (s.hm_pool, s.tile_pool):
                pool.shutdown(wait=False, cancel_futures=True)
    asyncio.run(go())


if __name__ == "__main__":
    main()
