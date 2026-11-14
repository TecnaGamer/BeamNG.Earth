#!/usr/bin/env python3
"""Offline drive simulation of the world streaming's order (no game): a car driven along a real road route at a fixed
speed through the server's own selection and priority code, with simulated worker pools, to compare the distance /
straight-line order against the time-to-reach tree (server/reach.py).

Each run replays one route at one speed under one policy:
  straight  today's order: the road geotiles' straight corridor ahead (roads.geo_wanted), the terrain chain on a
            straight line (earthd.predict_chain), download_ahead's points on the straight line, queued work dropped
            only when left behind (Server.left_behind)
  eta       the tree: road geotiles along it in order of their time to reach, queued ones it no longer reaches
            dropped (GeoTracker.unwanted), the terrain chain and download_ahead along its most probable path

What is simulated: the real roads.GeoTracker (its on_pose with a simulated executor), the road process pool (W workers,
FIFO, a fixed build time per geotile, a tile waiting for its 3 x 3 map cells when they are not fetched yet), the map
cells (fetched by the tiles' builds and by download_ahead, AHEAD_CELLS at once, a fixed time each), the mod's terrain
follow rule (earthd.predict_centre, the 1 km block) and the server's block prefetch (PREFETCH_PARALLEL, a fixed time
per block), the GPS route when --gps. Data: Overture's real roads (mapdata, a scratch cache) along an OSRM route.

  python3 tools/eta_sim.py --cache ~/bnge-tests/eta/cache --routes phx_i10 --speeds 100,300,500
  python3 tools/eta_sim.py --cache ~/bnge-tests/eta/cache --routes all --speeds 100,200,300,500 --json out.json

Metrics (per run): road geotiles built / s, of them used (the car came within USED_M) and wasted, needed tiles (the
path within NEED_M) late (sent after the car came within NEED_M) and by how much, the road lead ahead of the car
(p10 / median, share of poses with the car past its roads), the tiles' build CPU (built x the build time); terrain
blocks: the mod's requests, its hold (s waiting for a block), prefetches built and used; map cells fetched ahead and
used. Same CPU for both policies means the same number of builds; the gain is how many are needed in time.
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.request
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")
DEV = os.environ.get("BNGE_DEV_NATIVE")         # (a development build of earthfast first on the path)
if DEV:
    sys.path.insert(0, DEV)
sys.path.insert(1 if DEV else 0, SERVER)

import numpy as np  # noqa: E402

import earthd  # noqa: E402
import geodesy  # noqa: E402
import geotiles  # noqa: E402
import mapcells  # noqa: E402
import mapdata  # noqa: E402
import reach  # noqa: E402
import roads  # noqa: E402
import routing  # noqa: E402

ROUTES = {
    # I-10 east through Phoenix: straight desert, the Deck Park tunnel's curves, the Broadway curve, interchanges
    "phx_i10": ((33.46211, -112.2580), (33.3065, -111.9690)),
    # I-70 up Glenwood Canyon: a motorway curving with the river, few junctions
    "glenwood_i70": ((39.5531, -107.3105), (39.6447, -107.0650)),
    # Manhattan's avenues and cross streets (nyc_speed.py's "uptown")
    "nyc_uptown": ((40.7238, -73.9887), (40.7967, -73.9380)),
    # the FDR Drive up Manhattan's east side: a curving urban motorway with ramps every few hundred metres
    "nyc_fdr": ((40.7105, -73.9790), (40.7930, -73.9330)),
    # through Midtown's towers: Chelsea to the Upper East Side (nyc_speed.py's "midtown": city speeds, reach.MIN_V)
    "nyc_midtown": ((40.7426, -73.9928), (40.7740, -73.9560)),
    # Fells Point, Baltimore: the waterfront's grid of small streets
    "fells": ((39.2853, -76.6020), (39.2790, -76.5690)),
}
MPH = 0.44704
POSE_S = 0.5                # the mod's pose interval (core.lua POSE_INTERVAL)
FRAME_S = 0.05              # the mod's follow rule is tried every frame
NEED_M = 250.0              # m: a road geotile is needed once the car comes this close to it ...
ON_PATH_M = 30.0            # ... and is the road the car drives on within this of its path
USED_M = roads.SEND_M       # a built tile is used when the car comes within this of it
LEAD_LOOK_M = 4000.0
CELL_RING = 1               # a road geotile's build reads the roads of its cell and this ring round it


def fetch_route(name, cache):
    """The OSRM route's points [(lat, lon)] (kept in cache/routes)."""
    path = os.path.join(cache, "routes", f"{name}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)["points"]
    (la0, lo0), (la1, lo1) = ROUTES[name]
    req = urllib.request.Request(routing.url(la0, lo0, la1, lo1),
                                 headers={"User-Agent": "BeamNG.Earth-dev-eta-sim/1 (one-off test routes)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    out = routing.parse(data)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(out, f)
    return out["points"]


class Path:
    """The route in the anchor's world, by arc length."""

    def __init__(self, frame, pts):
        la = np.array([p[0] for p in pts])
        lo = np.array([p[1] for p in pts])
        x, y, _ = geotiles.geodetic_to_world(frame, la, lo)
        xy = np.stack([np.asarray(x), np.asarray(y)], 1)
        keep = np.r_[True, np.hypot(*(xy[1:] - xy[:-1]).T) > 0.01]
        self.xy = xy[keep]
        self.cum = np.r_[0.0, np.cumsum(np.hypot(*(self.xy[1:] - self.xy[:-1]).T))]
        self.length = float(self.cum[-1])
        ss = np.arange(0.0, self.length, 5.0)        # (dense: projections)
        self.dense = np.stack([np.interp(ss, self.cum, self.xy[:, 0]), np.interp(ss, self.cum, self.xy[:, 1])], 1)
        self.dense_s = ss

    def project(self, x, y):
        """(m along, m off) of the path's nearest point to (x, y) (5 m resolution)."""
        d = np.hypot(self.dense[:, 0] - x, self.dense[:, 1] - y)
        j = int(np.argmin(d))
        return float(self.dense_s[j]), float(d[j])

    def at(self, s):
        s = min(max(s, 0.0), self.length)
        j = int(np.clip(np.searchsorted(self.cum, s) - 1, 0, len(self.cum) - 2))
        t = (s - self.cum[j]) / max(self.cum[j + 1] - self.cum[j], 1e-9)
        return self.xy[j] + (self.xy[j + 1] - self.xy[j]) * t

    def heading(self, s, w=12.0):
        a, b = self.at(s - w), self.at(s + w)
        d = b - a
        n = float(np.hypot(*d)) or 1.0
        return d / n


class Fut:
    """A simulated future (asyncio's interface as the trackers use it)."""

    def __init__(self):
        self.cbs, self.res, self._done, self._cancelled = [], None, False, False

    def add_done_callback(self, cb):
        if self._done:
            cb(self)
        else:
            self.cbs.append(cb)

    def set_result(self, r):
        self.res, self._done = r, True
        for cb in self.cbs:
            cb(self)

    def cancel(self):
        if self._done:
            return False
        self._cancelled, self._done = True, True
        for cb in self.cbs:
            cb(self)
        return True

    def cancelled(self):
        return self._cancelled

    def done(self):
        return self._done

    def result(self):
        return self.res

    def exception(self):
        return None


class Pool:
    """W workers taking jobs in order; a job is (key, fut, work s, ready-check)."""

    def __init__(self, workers):
        self.free_at = [0.0] * workers
        self.queue = deque()
        self.running = []           # (finish t, key, fut)

    def submit(self, key, fut, work, t, wait_fn=None):
        self.queue.append((key, fut, work, t, wait_fn))

    def step(self, t, on_start=None):
        """Finish the jobs done by t, start queued ones on free workers (in time order)."""
        while True:
            self.running.sort(key=lambda r: r[0])
            nxt = self.running[0][0] if self.running else math.inf
            free = [i for i, f in enumerate(self.free_at) if f <= t]
            if self.queue and free:
                start_t = max(self.free_at[free[0]], self.queue[0][3])
                if start_t <= nxt:
                    key, fut, work, _t0, wait_fn = self.queue.popleft()
                    extra = wait_fn(start_t) if wait_fn else 0.0
                    fin = start_t + extra + work
                    self.free_at[free[0]] = fin
                    self.running.append((fin, key, fut))
                    if on_start:
                        on_start(key, start_t)
                    continue
            if nxt <= t:
                fin, key, fut = self.running.pop(0)
                fut.set_result(("done", fin))
                continue
            break

    def drop(self, pred):
        """Queued jobs pred(key) is true for: removed; their futures answered None (the server's Abandoned)."""
        keep, gone = deque(), []
        for j in self.queue:
            if pred(j[0]):
                gone.append(j)
            else:
                keep.append(j)
        self.queue = keep
        for j in gone:
            j[1].set_result(("dropped", None))
        return len(gone)


class Loop:
    """The trackers' event loop as seen by GeoTracker.on_pose: run_in_executor queues a road geotile job."""

    def __init__(self, sim):
        self.sim = sim

    def now(self):
        return self.sim.t

    def run_in_executor(self, _pool, _build, aid, gx, gy, content, skin, make):
        return self.sim.road_job((gx, gy), content, skin)


class Conn:
    s = None

    def __init__(self):
        self.sent = []

    def send(self, m):
        self.sent.append(m)


class CellsProxy:
    """mapdata for reach.Reach: a cell is there once the simulation has fetched it."""

    def __init__(self, md, sim):
        self.md, self.sim = md, sim

    def has(self, key, layer):
        return self.sim.avail is None or str(key) in self.sim.avail

    def layer_arrays(self, key, layer, kind="demand"):
        return self.md.layer_arrays(key, layer, kind)


class Sim:
    def __init__(self, md, frame, path, speed, policy, a):
        self.md, self.frame, self.path, self.v, self.policy, self.a = md, frame, path, speed, policy, a
        self.t = 0.0
        self.road = Pool(a.road_workers)
        self.tracker = roads.GeoTracker()
        self.tracker.pool = object()
        self.loop = Loop(self)
        self.conn = Conn()
        self.ready = {}             # geotile -> t its content and skin were sent
        self.built = {}             # geotile -> times built
        self.started = 0
        self.dropped = 0
        self.avail = set() if a.cold else None
        self.fetching = {}          # cell -> t it is there
        self.cell_by_ahead = set()
        self.cell_used = set()
        self.ahead_busy_until = -1.0
        self.ahead_last = None
        self.reach = reach.Reach(CellsProxy(md, self), frame, 1) if policy == "eta" else None
        self.gps = path.xy if a.gps else None
        self.tree = None
        self.grow_s = []
        # terrain
        self.rule = earthd.FOLLOW_RULES[1024.0]
        self.blocks = {}            # centre -> t ready (or inf: in flight)
        self.block_pool = Pool(a.block_workers)
        self.req_pool = Pool(1)
        self.cur = None
        self.pending = None         # (centre, t asked)
        self.holds = []
        self.requests = []
        self.prefetched = set()
        self.prefetch_busy = set()
        self.predicted = []

    # --- map cells ---------------------------------------------------------------------------------------------
    def cell_there(self, k, t):
        if self.avail is None:
            return True
        if k in self.avail:
            return True
        d = self.fetching.get(k)
        if d is not None and d <= t:
            self.avail.add(k)
            return True
        return False

    def fetch_cell(self, k, t, by_ahead=False):
        """t the cell is there (fetched now if it is neither there nor on its way)."""
        if self.avail is None or k in self.avail:
            return t
        d = self.fetching.get(k)
        if d is None:
            d = self.fetching[k] = t + self.a.cell_s
            if by_ahead:
                self.cell_by_ahead.add(k)
        return d

    def tile_cells(self, key):
        la, lo = geotiles.centre(*key)
        c = mapcells.cell_of(la, lo)
        return [str(mapcells.CellKey(14, c.x + dx, c.y + dy)) for dx in range(-CELL_RING, CELL_RING + 1)
                for dy in range(-CELL_RING, CELL_RING + 1)]

    # --- road geotiles -----------------------------------------------------------------------------------------
    def road_job(self, key, content, skin):
        fut = Fut()
        sim = self

        def wait(t):
            if sim.avail is None:
                return 0.0
            ks = sim.tile_cells(key)
            sim.cell_used.update(ks)
            return max(0.0, max(sim.fetch_cell(k, t) for k in ks) - t)

        self.road.submit(key, fut, self.a.tile_s, self.t, wait)
        out = Fut()

        def done(f):
            kind, _fin = f.res
            if kind == "dropped":
                sim.dropped += 1
                out.set_result(None)
                return
            res = {"content": (f"c_{key}", 1, {"h": 0.0, "parts": {}}) if content else None,
                   "skin": (f"s_{key}",) if skin else None, "time": {}}
            if key not in sim.ready and (content or key in sim.tracker.sent) and skin:
                sim.ready[key] = sim.t
            out.set_result(res)
        fut.add_done_callback(done)
        return out

    def road_started(self, key, _t):
        self.started += 1
        self.built[key] = self.built.get(key, 0) + 1

    def left_behind(self, key, x, y, vx, vy):
        """Server.left_behind for one player (geo_left_behind: the tile's middle, 150 m of margin)."""
        la, lo = geotiles.centre(*key)
        gx, gy, _ = geotiles.geodetic_to_world(self.frame, np.array([la]), np.array([lo]))
        dx, dy = float(gx[0]) - x, float(gy[0]) - y
        d = math.hypot(dx, dy)
        if d > earthd.LEFT_FAR_M + 150.0:
            return True
        return d > earthd.LEFT_BEHIND_M + 150.0 and math.hypot(vx, vy) >= earthd.LEFT_MIN_V \
            and dx * vx + dy * vy < 0

    # --- terrain -----------------------------------------------------------------------------------------------
    def mod_frame(self, px, py, vx, vy):
        """The mod's follow rule at one frame: a request when the player leaves the prepare box (predict_request)."""
        r = self.rule
        if self.cur is None:
            lead, rch, _ = r.mode(vx, vy)
            self.cur = earthd.predict_centre(px, py, vx, vy, lead, rch, r.grid, r.min_lead, r.min_lead_v)
            return
        if self.pending is not None:
            c, t0 = self.pending
            if self.blocks.get(c, math.inf) <= self.t:
                self.holds.append(self.t - t0)
                self.cur, self.pending = c, None
            return
        lead, rch, ahead = r.mode(vx, vy)
        qx, qy = px + vx * ahead, py + vy * ahead
        if max(abs(qx - self.cur[0]), abs(qy - self.cur[1])) <= r.prepare:
            return
        c = earthd.predict_centre(px, py, vx, vy, lead, rch, r.grid, r.min_lead, r.min_lead_v)
        if c == self.cur:
            return
        best = None             # (follow.lua preferReady: a built block near the target instead)
        for k, tr in self.blocks.items():
            if tr > self.t or k == self.cur:
                continue
            d = max(abs(k[0] - c[0]), abs(k[1] - c[1]))
            if d <= earthd.READY_STEPS * r.grid and max(abs(px - k[0]), abs(py - k[1])) <= rch:
                along = (k[0] - px) * vx + (k[1] - py) * vy
                if best is None or (d, -along) < best[0]:
                    best = ((d, -along), k)
        if best is not None:
            c = best[1]
        self.requests.append((self.t, c, c in self.prefetched))
        if c not in self.blocks:
            self.blocks[c] = math.inf
            fut = Fut()
            fut.add_done_callback(lambda f, c=c: self.blocks.__setitem__(c, f.res[1]))
            self.req_pool.submit(c, fut, self.a.block_s, self.t)
        self.pending = (c, self.t)

    def server_prefetch(self, px, py, vx, vy):
        base = self.pending[0] if self.pending else self.cur
        if base is None:
            return
        r = self.rule
        want = []
        ahead = r.mode(vx, vy)[2]
        qx, qy = px + vx * ahead, py + vy * ahead
        if max(abs(qx - base[0]), abs(qy - base[1])) >= r.prefetch_start:
            c = earthd.predict_request(px, py, vx, vy, base[0], base[1], r)
            if c != base:
                want.append(c)
        if math.hypot(vx, vy) > earthd.FAST_BASE_V:
            cs = None
            route = self.gps if self.gps is not None else (self.tree.mpp_xy() if self.tree is not None else None)
            if route is not None and (self.gps is not None or self.policy == "eta"):
                cs = earthd.predict_route_chain(route, px, py, vx, vy, base[0], base[1], r, earthd.PREFETCH_CHAIN + 1)
                if cs:
                    if want:
                        want[0] = cs[0]
                    cs = cs[1:]
            if not cs:
                cs = earthd.predict_chain(px, py, vx, vy, base[0], base[1], r, earthd.PREFETCH_CHAIN)
            want += cs
        self.predicted = want
        if self.policy == "eta":        # (queued prefetches the prediction has left: dropped)
            keep = set(want) | ({self.pending[0]} if self.pending else set())
            self.block_pool.drop(lambda c: c not in keep)
        for c in want:
            if len(self.prefetch_busy) >= earthd.PREFETCH_PARALLEL:
                break
            if c in self.blocks:
                continue
            # (Connection.ready_will_do: a block built or on its way near it, the player within reach of it)
            _l, rch, _a = r.mode(vx, vy)
            if any(k != base and max(abs(k[0] - c[0]), abs(k[1] - c[1])) <= earthd.READY_STEPS * r.grid
                   and max(abs(px - k[0]), abs(py - k[1])) <= rch + 2 * r.grid for k in self.blocks):
                continue
            self.blocks[c] = math.inf
            self.prefetched.add(c)
            self.prefetch_busy.add(c)
            fut = Fut()

            def fin(f, c=c):
                self.prefetch_busy.discard(c)
                if f.res[0] == "dropped":
                    del self.blocks[c]
                    self.prefetched.discard(c)
                else:
                    self.blocks[c] = f.res[1]
            fut.add_done_callback(fin)
            self.block_pool.submit(c, fut, self.a.block_s, self.t)

    # --- download_ahead ----------------------------------------------------------------------------------------
    def download_ahead(self, x, y, vx, vy):
        if self.avail is None:
            return
        v = math.hypot(vx, vy)
        if self.t < self.ahead_busy_until or v < earthd.AHEAD_MIN_V:
            return
        last = self.ahead_last
        if last is not None and math.hypot(x - last[0], y - last[1]) < earthd.AHEAD_MOVE_M:
            return
        self.ahead_last = (x, y)
        pts = earthd.ahead_points(x, y, vx, vy, self.tree if self.policy == "eta" else None)
        half = 1024 / 2 + earthd.AHEAD_MARGIN_M
        jobs = []
        for px, py in pts:
            lla = geotiles.world_to_geodetic(self.frame, np.array([[px, py, 0.0]]))
            la, lo = float(lla[0][0]), float(lla[1][0])
            dla = half / 111320.0
            dlo = half / (111320.0 * math.cos(math.radians(la)))
            for k in mapcells.cells_for_bbox(la - dla, lo - dlo, la + dla, lo + dlo):
                sk = str(k)
                if sk not in self.avail and sk not in self.fetching and sk not in jobs:
                    jobs.append(sk)
        for i, k in enumerate(jobs):
            self.fetching[k] = self.t + (i // earthd.AHEAD_CELLS + 1) * self.a.cell_s
            self.cell_by_ahead.add(k)
        self.ahead_busy_until = self.t + math.ceil(len(jobs) / earthd.AHEAD_CELLS) * self.a.cell_s

    # --- the drive ---------------------------------------------------------------------------------------------
    def run(self):
        a = self.a
        dur = min(a.max_s, (self.path.length - 200.0) / self.v)
        if self.avail is not None:      # (a cold start: the cells round the start fetched first, as a teleport)
            x0, y0 = self.path.at(0.0)
            lla = geotiles.world_to_geodetic(self.frame, np.array([[x0, y0, 0.0]]))
            c = mapcells.cell_of(float(lla[0][0]), float(lla[1][0]))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    self.avail.add(str(mapcells.CellKey(14, c.x + dx, c.y + dy)))
        poses = []
        t_next_pose = 0.0
        while self.t <= dur:
            s = self.v * self.t
            x, y = self.path.at(s)
            hx, hy = self.path.heading(s)
            vx, vy = hx * self.v, hy * self.v
            self.road.step(self.t, self.road_started)
            self.block_pool.step(self.t)
            self.req_pool.step(self.t)
            self.mod_frame(float(x), float(y), vx, vy)
            if self.t >= t_next_pose - 1e-9:
                t_next_pose += POSE_S
                if self.avail is not None:
                    for k, d in list(self.fetching.items()):
                        if d <= self.t:
                            self.avail.add(k)
                tree = None
                if self.reach is not None:
                    self.reach.update_cells(float(x), float(y), vx, vy, limit=64)    # (the server: its own thread)
                    t0 = time.perf_counter()
                    tree = self.reach.grow(float(x), float(y), vx, vy, route_xy=self.gps, now=self.t)
                    if tree is not None:
                        tree.geotile_keys(self.frame, roads.CORRIDOR_M,
                                          horizon=min(roads.LEAD_MAX_M, self.v * roads.LEAD_S) / self.v)
                    self.grow_s.append(time.perf_counter() - t0)
                self.tree = tree
                self.download_ahead(float(x), float(y), vx, vy)
                self.server_prefetch(float(x), float(y), vx, vy)
                self.tracker.on_pose(self.conn, 1, float(x), float(y), self.loop, None, vel=(vx, vy),
                                     frame=self.frame, tree=tree)
                if self.policy == "eta":
                    self.road.drop(lambda k: self.tracker.unwanted(k, self.t)
                                   or self.left_behind(k, float(x), float(y), vx, vy))
                else:
                    self.road.drop(lambda k: self.left_behind(k, float(x), float(y), vx, vy))
                poses.append((self.t, s))
            self.t += FRAME_S
        self.dur = dur
        self.poses = poses
        return self.metrics()

    def metrics(self):
        a, path, v = self.a, self.path, self.v
        end_s = v * self.dur
        # the needed tiles: the path within NEED_M (first time the car is that close: its deadline)
        need, on_path = {}, set()
        step = 20.0
        s = 0.0
        while s <= end_s:
            x, y = path.at(s)
            for gx, gy, d in geotiles.tiles_near(self.frame, float(x), float(y), NEED_M, exact=True):
                need.setdefault((gx, gy), s / v)
                if d <= ON_PATH_M:
                    on_path.add((gx, gy))
            s += step
        used = set()
        s = 0.0
        while s <= end_s:
            x, y = path.at(s)
            for gx, gy, d in geotiles.tiles_near(self.frame, float(x), float(y), USED_M, exact=True):
                used.add((gx, gy))
            s += step
        tail = 25.0             # (the last seconds' tiles: their deadline past the run's end, not counted)
        needed = {k: t for k, t in need.items() if t > 2.0 and t < self.dur - 1.0}
        late = {k: (self.ready.get(k, math.inf) - t) for k, t in needed.items() if self.ready.get(k, math.inf) > t}
        late_path = {k: d for k, d in late.items() if k in on_path}
        built = sum(self.built.values())
        built_in = {k for k in self.built}
        wasted = [k for k in built_in if k not in used]
        # the road lead: at each pose the m along the path to the first on-path tile not sent
        leads = []
        for t, s0 in self.poses:
            if t < 2.0 or s0 + LEAD_LOOK_M > end_s + 2000:
                continue
            lead = LEAD_LOOK_M
            s = s0
            while s < s0 + LEAD_LOOK_M:
                x, y = path.at(s)
                miss = False
                for gx, gy, d in geotiles.tiles_near(self.frame, float(x), float(y), ON_PATH_M, exact=True):
                    if self.ready.get((gx, gy), math.inf) > t:
                        miss = True
                        break
                if miss:
                    lead = s - s0
                    break
                s += 40.0
            leads.append(lead)
        leads = np.array(leads) if leads else np.zeros(1)
        lateness = np.array([min(d, 60.0) for d in late.values()]) if late else np.zeros(1)
        cpu = built * a.tile_s
        req = len(self.requests)
        pre_used = len({c for _t, c, hit in self.requests if hit})
        out = {
            "dur_s": round(self.dur, 1), "km": round(end_s / 1000, 2),
            "tiles_built": built, "tiles_built_per_s": round(built / self.dur, 2),
            "tiles_used_share": round(1 - len(wasted) / max(1, len(built_in)), 3),
            "tiles_wasted": len(wasted), "tiles_dropped": self.dropped,
            "needed": len(needed), "late": len(late), "late_share": round(len(late) / max(1, len(needed)), 3),
            "on_path_needed": sum(1 for k in needed if k in on_path), "on_path_late": len(late_path),
            "late_mean_s": round(float(lateness.mean()) if late else 0.0, 2),
            "needed_in_time_per_s": round((len(needed) - len(late)) / self.dur, 2),
            "lead_p10_m": round(float(np.percentile(leads, 10)), 0),
            "lead_med_m": round(float(np.median(leads)), 0),
            "past_roads_share": round(float((leads <= 0.0).mean()), 3),
            "road_cpu_s": round(cpu, 1),
            "blocks_requested": req, "block_hold_s": round(float(sum(self.holds)), 1),
            "block_hold_max_s": round(float(max(self.holds)) if self.holds else 0.0, 2),
            "blocks_prefetched": len(self.prefetched), "prefetch_used": pre_used,
            "prefetch_wasted": len(self.prefetched) - pre_used,
        }
        if self.avail is not None:
            ahead_used = self.cell_by_ahead & self.cell_used
            out.update(cells_ahead=len(self.cell_by_ahead), cells_ahead_used=len(ahead_used),
                       cells_fetched=len(self.fetching))
        if self.reach is not None:
            st = self.reach.stats
            out.update(tree_none_share=round(st["none"] / max(1, st["none"] + st["trees"]), 3))
        if self.grow_s:
            g = np.array(self.grow_s) * 1000
            out.update(tree_ms_med=round(float(np.median(g)), 2), tree_ms_p95=round(float(np.percentile(g, 95)), 2))
        return out


def prepare_cells(md, frame, path, radius_m=3500.0):
    """Every map cell within radius_m of the route, fetched into the scratch cache (once)."""
    keys = set()
    s = 0.0
    while s <= path.length:
        x, y = path.at(s)
        lla = geotiles.world_to_geodetic(frame, np.array([[float(x), float(y), 0.0]]))
        la, lo = float(lla[0][0]), float(lla[1][0])
        dla = radius_m / 111320.0
        dlo = radius_m / (111320.0 * math.cos(math.radians(la)))
        keys.update(str(k) for k in mapcells.cells_for_bbox(la - dla, lo - dlo, la + dla, lo + dlo))
        s += 500.0
    from concurrent.futures import ThreadPoolExecutor
    todo = [k for k in sorted(keys) if not md.has(mapcells.parse(k), "roads")]
    if todo:
        print(f"  fetching {len(todo)} map cells", flush=True)
        def one(k):
            for i in range(5):
                try:
                    return md.layer_arrays(mapcells.parse(k), "roads", "demand")
                except Exception as e:      # (netfetch's rate limits: again a little later)
                    print(f"  {k}: {type(e).__name__}: {e}", flush=True)
                    time.sleep(2.0 * (i + 1))
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(one, todo))
    return keys


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=os.path.expanduser("~/bnge-tests/eta/cache"))
    ap.add_argument("--routes", default="phx_i10")
    ap.add_argument("--speeds", default="100,300,500", help="mph")
    ap.add_argument("--policies", default="straight,eta")
    ap.add_argument("--road-workers", type=int, default=3)
    ap.add_argument("--tile-s", type=float, default=1.5, help="s a road geotile's build takes (content and skin)")
    ap.add_argument("--block-workers", type=int, default=1)
    ap.add_argument("--block-s", type=float, default=2.5)
    ap.add_argument("--cell-s", type=float, default=0.6, help="s a map cell's fetch takes (cold)")
    ap.add_argument("--cold", action="store_true", help="map cells only once fetched (else all on disk)")
    ap.add_argument("--gps", action="store_true", help="the route is the player's GPS route")
    ap.add_argument("--max-s", type=float, default=120.0)
    ap.add_argument("--json")
    a = ap.parse_args()
    names = sorted(ROUTES) if a.routes == "all" else a.routes.split(",")
    md = mapdata.MapData(a.cache)
    results = []
    try:
        for name in names:
            pts = fetch_route(name, a.cache)
            frame = geodesy.EnuFrame(pts[0][0], pts[0][1], 0.0)
            path = Path(frame, pts)
            print(f"{name}: {path.length / 1000:.1f} km", flush=True)
            prepare_cells(md, frame, path)
            for mph in [float(v) for v in a.speeds.split(",")]:
                for pol in a.policies.split(","):
                    geotiles._near_memo.clear()
                    sim = Sim(md, frame, path, mph * MPH, pol, a)
                    t0 = time.perf_counter()
                    m = sim.run()
                    m.update(route=name, mph=mph, policy=pol, cold=a.cold, gps=a.gps,
                             sim_s=round(time.perf_counter() - t0, 1))
                    results.append(m)
                    print(json.dumps(m), flush=True)
    finally:
        md.close()
    if a.json:
        with open(a.json, "w") as f:
            json.dump(results, f, indent=1)


if __name__ == "__main__":
    main()
