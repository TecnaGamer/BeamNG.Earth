"""Streaming by time to reach along the road graph (the retro optimization study's section J, 2026-11-11: Crash's
rails, GTA III's streaming window, Just Cause's request queues; the user: keep up at speed and after teleports without
more CPU cores).

At speed a car is on a road, and where it can be in T seconds is a tree along the road graph, not a disc or a straight
line ahead: a motorway's curve, an interchange's ramps. Reach keeps the drivable roads of the map cells near a fast
car as a graph (earthfast.RoadReach: Overture's segments split at their connectors, built from the cells already on
disk, no download of its own) and grows that tree at each pose (Tree): points along the roads ahead with their
earliest arrival (the car's speed along the road, plus the time a turn costs to brake for and speed up from) and the
chance the driver goes that way (by that cost and the road class; a GPS route's way strongly preferred). Where the
loaded map data ends the tree goes on straight (its frontier: the data fetched next, Server.download_ahead).

What follows it (earthd, roads.GeoTracker): the road geotiles wanted ahead are those along the tree instead of a
straight corridor, built in order of their time to reach (a likely branch first, the sides after); queued ones the
tree no longer reaches (an exit passed, a turn not taken) are dropped (GeoTracker.unwanted, beside Server.left_behind);
the terrain blocks prefetched and the map cells, elevation tiles, road plans and photos fetched ahead follow its most
probable path. Slower than MIN_V, with no road under the car, or with the 3D map: none of it (distance order as before).

At city speeds (MIN_V 8 m/s, 18 mph, since 2026-11-13; 25 m/s before): a cold 40 mph drive through Midtown Manhattan
held the car 57 % of the time, its road tiles built nearest first all round it (a disc) while the car only ever needed
the ones along its avenue and the next turns. Below FAST_V the tree follows less likely ways too (P_MIN_CITY) and a turn
onto a smaller street costs less of its chance (CLASS_K_CITY: in a town a driver turns off the avenue far more often than
off a motorway); the trackers keep the tiles right round the car first whatever the tree says (roads.INSURE_M). The
driver's intent (Intent: the car already turning, or braking hard, from its own poses) makes the ways at the junctions
just ahead that it is turning or braking for likelier (earthfast >= INTENT_NEED).
"""
import logging
import math
import os
import threading
import time

import numpy as np

import fast
import dmath
import dnp

log = logging.getLogger("earthd.reach")

NEED = 215                  # earthfast.RoadReach
INTENT_NEED = 293           # ... with the driver's intent (grow's intent_turn / intent_brake / intent_s)
# m/s (18 mph): slower, the trackers' distance order as before. (25 m/s, roads.FAST_MS, until 2026-11-13: a car at
# 40 mph in a city had its road tiles built in distance order, the ones behind and beside it as soon as those ahead)
MIN_V = float(os.environ.get("BNE_REACH_MIN_V", "8.0"))
FAST_V = 25.0               # m/s: from here up the tree's motorway parameters (P_MIN, CLASS_K) ...
CITY_V = 12.0               # ... at this speed and below the city's (P_MIN_CITY, CLASS_K_CITY), in between a blend
P_MIN_CITY = 0.01           # a branch less likely than this is not followed (a town's junctions every 80-200 m split
                            # the chance fast: at P_MIN the tree stopped two or three blocks on)
CLASS_K_CITY = 0.75         # each class step down this much less likely (a side street off an avenue: 0.42, not 0.22)
HORIZON_S = 30.0            # s ahead the tree reaches (roads.LEAD_S) ...
HORIZON_MAX_M = 5000.0      # ... and this far at most (roads.LEAD_MAX_M) ...
HORIZON_MIN_M = 900.0       # ... and this far at least (a slow car's: past the road tiles' disc, roads.SEND_M + a tile)
STEP_M = 32.0               # m between the tree's points along a road
P_MIN = 0.03                # a branch less likely than this is not followed
P_WANT = 0.1                # a branch this likely or more is wanted (its tiles built); less likely ones only ordered
GAMMA = 0.5                 # a place's priority: its ETA / chance ** GAMMA (a 1-in-4 branch counts twice as far)
SNAP_M = 30.0               # m from a road the car may be to be on it ...
SNAP_COS = 0.7              # ... going along it (cos of the angle)
A_TURN = 6.0                # m/s^2 a turn's braking and speeding up back (its time cost: (v - v_turn)^2 / (A v))
V90 = 12.0                  # m/s through a 90 degree turn (faster through gentler ones: V90 * 90 / angle)
T_P = 2.0                   # s: a way whose turn costs this much more is e times less likely
CLASS_K = 0.6               # each class step down (motorway -> trunk -> primary ...) this much less likely
LINK_K = 0.5                # ... and off a road onto a ramp
ROUTE_SHARE = 0.97          # a GPS route's way takes this share of the chance at a junction
ROUTE_M = 20.0              # m from the route's line a way follows it
MAX_STATES = 20000          # directed edges walked at most per tree
CELL_REACH_M = 1500.0       # m beyond the horizon's reach the cells round the car are loaded into the graph (and
                            # 2.5 km more kept: cells_wanted)
CELLS_PER_ROUND = 12        # cells added per update (the nearest first; the rest at the next pose)
GROW_EVERY_S = 0.4          # s between two trees (each pose otherwise)
GROW_MOVE_M = 25.0          # ... unless the car moved this far
TREE_MAX_AGE_S = 2.0        # s a tree is used for at most (none after: distance order)
# A queued job is dropped (GeoTracker.unwanted) when the trackers have not wanted it for this long while the tree
# leads, and it is farther than roads.SEND_M + a tile from the car
UNWANTED_GRACE_S = 2.0
# The order of a fast car's tiles (order_key): by time to reach, a less likely branch later (GAMMA), one off the road
# later by its gap at V_SIDE (lateral work later, the same at any speed), and one that could only be done when most of
# the time the car spends near it is over (its build time estimated from the last ones) after those still on time: at
# 500 mph a tile under the car takes longer to build than the car stays near it, and building it first kept the
# whole queue behind the car (the drive simulation, tools/eta_sim.py)
V_SIDE = 25.0               # m/s at most (side_speed)
WINDOW_M = 750.0            # m of driving a tile matters for, from when the car reaches it (a tile and ~250 m either way)
LATE_KEEP = 0.6             # a tile done with less than this share of that time left counts as late ...
LATE_S = 30.0               # ... and comes this much later (s)
BUILD_EST_S = 2.0           # s a tile's build is taken to need before any was timed
# The driver's intent (Intent, from the poses): at the junctions the car reaches within INTENT_S, the ways turning the
# way the car already turns (its heading's rate over YAW_FULL rad/s: full) and, while it brakes (DECEL_MIN m/s^2 and
# more, DECEL_FULL: full), every turn likelier (earthfast reach.rs INTENT_TURN_K / INTENT_BRAKE_K)
INTENT_S = 6.0
INTENT_WINDOW_S = 1.2       # s of poses the car's turning and braking are measured over
INTENT_MIN_V = 3.0          # m/s: slower, no turn rate (a heading at a crawl is noise)
YAW_MIN = 0.05              # rad/s: less is a road's own curve, not a turn (a 300 m bend at 15 m/s)
YAW_FULL = 0.30
DECEL_MIN = 1.0
DECEL_FULL = 4.0

RANK = {"motorway": 7, "trunk": 6, "primary": 5, "secondary": 4, "tertiary": 3, "unclassified": 2, "residential": 2,
        "living_street": 1, "service": 1, "unknown": 1, "busway": 0, "track": 0}
LINK_BIT = 0x80
SKIP_SUBCLASSES = {"driveway", "parking_aisle", "sidewalk", "crosswalk", "cycle_crossing", "alley"}


def available():
    """Is the native kernel there (earthfast >= NEED)?"""
    return fast._ef is not None and getattr(fast._ef, "VERSION", 0) >= NEED


def city_share(speed):
    """1 at CITY_V and below, 0 at FAST_V and above, linear in between: how much of the city's parameters."""
    return min(1.0, max(0.0, (FAST_V - speed) / (FAST_V - CITY_V)))


def p_min_at(speed):
    """The tree's P_MIN at this speed (P_MIN_CITY in a town's speeds)."""
    c = city_share(speed)
    return P_MIN + (P_MIN_CITY - P_MIN) * c


def class_k_at(speed):
    """The tree's CLASS_K at this speed (CLASS_K_CITY in a town's speeds)."""
    c = city_share(speed)
    return CLASS_K + (CLASS_K_CITY - CLASS_K) * c


class Intent:
    """The driver's intent from the car's own poses (feed each pose's time and velocity): turn (-1 right .. 1 left)
    from its heading's rate, brake (0 .. 1) from its slowing down, over the last INTENT_WINDOW_S. No input or
    indicator is read: what the car does is what the server sees."""

    def __init__(self):
        self.hist = []          # (t, vx, vy)

    def feed(self, t, vx, vy):
        h = self.hist
        if h and t <= h[-1][0]:
            if t < h[-1][0] - 5.0:      # (another clock: a new connection's)
                h.clear()
            else:
                return
        h.append((float(t), float(vx), float(vy)))
        while len(h) > 2 and t - h[1][0] >= INTENT_WINDOW_S:
            h.pop(0)

    def reset(self):
        self.hist = []

    def value(self):
        """(turn, brake)."""
        h = self.hist
        if len(h) < 2:
            return 0.0, 0.0
        t0, vx0, vy0 = h[0]
        t1, vx1, vy1 = h[-1]
        dt = t1 - t0
        if dt < 0.2 or dt > 3.0 * INTENT_WINDOW_S:
            return 0.0, 0.0
        s0, s1 = math.hypot(vx0, vy0), math.hypot(vx1, vy1)
        turn = 0.0
        if min(s0, s1) >= INTENT_MIN_V:
            yaw = dmath.atan2(vx0 * vy1 - vy0 * vx1, vx0 * vx1 + vy0 * vy1) / dt     # (+: to the left)
            if abs(yaw) > YAW_MIN:
                turn = math.copysign(min(1.0, (abs(yaw) - YAW_MIN) / (YAW_FULL - YAW_MIN)), yaw)
        decel = (s0 - s1) / dt
        brake = min(1.0, max(0.0, (decel - DECEL_MIN) / (DECEL_FULL - DECEL_MIN)))
        return turn, brake


def horizon_s(speed):
    """The tree's horizon at this speed (s): HORIZON_S, HORIZON_MIN_M at least, HORIZON_MAX_M at most."""
    v = max(speed, 1.0)
    return min(max(HORIZON_S, HORIZON_MIN_M / v), HORIZON_MAX_M / v)


def priority(eta, p):
    """A place's priority key (s): its ETA, a less likely branch's later (eta / p ** GAMMA)."""
    return eta / dnp.power(max(p, P_MIN), GAMMA)


def side_speed(speed):
    """m/s a tile's gap off the road counts at (order_key): as roads.SIDE_PENALTY (a quarter of the car's speed), at
    most V_SIDE (at speed the sides later still)."""
    return min(speed / 4.0, V_SIDE)


def order_key(eta, p, gap, speed, build_s):
    """A fast car's tile's place in the build order (s, smaller first): eta (s) the car reaches it in with chance p,
    gap (m) it lies off the road; build_s the time a build takes now. See V_SIDE / WINDOW_M / LATE_KEEP."""
    key = priority(eta, p) + gap / side_speed(speed)
    win = WINDOW_M / max(speed, 1.0)
    left = (eta + win - max(build_s, eta)) / win
    if left < LATE_KEEP:
        key += LATE_S * (2.0 if left <= 0.0 else 1.0)
    return key


ORDER_NEAR_M = 128.0        # m: order_geotiles' tiles this close to the car first (roads.INSURE_M)
ORDER_CORRIDOR_M = 160.0    # ... the tree's within this of its roads next (roads.CORRIDOR_M)


def order_geotiles(tree, frame, keys, x, y, z=None):
    """keys (zoom-z geotiles, the trackers' wanted ones round the car at (x, y)) in the order the car's tree reaches
    them: those within ORDER_NEAR_M of the car first (by distance: the ground right round it whatever the tree says),
    then the ones within ORDER_CORRIDOR_M of its roads by their priority (time to reach, a less likely way later), then
    the rest by distance. The detail trackers' order (vegetation: the trees ahead along the way the car goes first)."""
    import geotiles
    keys = list(keys)
    if not keys:
        return keys
    z = geotiles.Z if z is None else int(z)
    tk = tree.geotile_keys(frame, ORDER_CORRIDOR_M, z=z, horizon=horizon_s(tree.speed))
    d = geotiles.dists(frame, keys, z, [(x, y)])[:, 0].tolist()

    def key(i):
        k, dk = keys[i], d[i]
        if dk <= ORDER_NEAR_M:
            return (0, dk)
        got = tk.get(tuple(k))
        if got is not None:
            return (1, got[0])
        return (2, dk)
    return [keys[i] for i in sorted(range(len(keys)), key=key)]


def _ranges(arrays, n):
    """Each feature's [start, end) rows of lonlat (the first part's first ring: a road segment is one line)."""
    po, ro, co = arrays["part_off"], arrays["ring_off"], arrays["coord_off"]
    first = ro[po[:n]]
    return co[first].astype(np.int64), co[first + 1].astype(np.int64)


def cell_inputs(frame, arrays):
    """add_cell's arguments of a cell's raw roads layer (mapdata.layer_arrays) in frame's world: the drivable road
    segments (RANK's classes, footway-like subclasses left out), with their connectors and one-way rules. None: none."""
    import geotiles
    ids = arrays["ids"]
    n = len(ids)
    if n == 0:
        return None
    cls = arrays["attr.class"]
    sub = arrays.get("attr.subclass")
    subtype = arrays.get("attr.subtype")
    keep = np.fromiter((c in RANK for c in cls.tolist()), bool, n)
    if subtype is not None:
        keep &= subtype == "road"
    if sub is not None:
        keep &= ~np.isin(sub, list(SKIP_SUBCLASSES))
    link = np.zeros(n, bool)
    if sub is not None:
        link |= sub == "link"
    for name in ("flags", "subclass"):
        f = arrays.get(f"rule.{name}.feature")
        if f is not None and len(f):
            v = arrays[f"rule.{name}.value"]
            hit = f[(v == "is_link") | (v == "link")]
            link[hit[(hit >= 0) & (hit < n)]] = True
    idx = np.flatnonzero(keep)
    if not len(idx):
        return None
    remap = np.full(n, -1, np.int64)
    remap[idx] = np.arange(len(idx))
    s0, s1 = _ranges(arrays, n)
    s0, s1 = s0[idx], s1[idx]
    lens = s1 - s0
    rows = np.concatenate([np.arange(a, b) for a, b in zip(s0.tolist(), s1.tolist())]) if len(idx) else np.zeros(0, int)
    ll = np.asarray(arrays["lonlat"], np.float64)[rows]
    x, y, _ = geotiles.geodetic_to_world(frame, ll[:, 1], ll[:, 0])
    xy = np.ascontiguousarray(np.stack([np.asarray(x, np.float64), np.asarray(y, np.float64)], 1))
    end = np.cumsum(lens)
    start = end - lens
    rank = np.array([RANK[c] for c in cls[idx].tolist()], np.uint8) | np.where(link[idx], LINK_BIT, 0).astype(np.uint8)

    def rule(name, kind):
        f = arrays.get(f"rule.{name}.feature")
        if f is None or not len(f):
            return (np.zeros(0, np.int32), np.zeros(0, kind), np.zeros(0, np.float32), np.zeros(0, np.float32))
        f = np.asarray(f, np.int64)
        ok = (f >= 0) & (f < n)
        f2 = np.where(ok, remap[np.clip(f, 0, n - 1)], -1)
        ok &= f2 >= 0
        return (f2[ok].astype(np.int32), arrays[f"rule.{name}.value"][ok],
                np.asarray(arrays[f"rule.{name}.start"], np.float32)[ok],
                np.asarray(arrays[f"rule.{name}.end"], np.float32)[ok])
    cf, cv, ca, _ce = rule("connectors", object)
    of, ov, os_, oe = rule("oneway", np.float32)
    return dict(feat_ids=[str(v) for v in ids[idx].tolist()], xy=xy, start=start.astype(np.int64),
                end=end.astype(np.int64), rank=rank, conn_feat=cf, conn_at=np.asarray(ca, np.float32),
                conn_ids=[str(v) for v in cv.tolist()], ow_feat=of, ow_val=np.asarray(ov, np.float32),
                ow_start=os_, ow_end=oe)


def empty_inputs():
    """add_cell's arguments of a cell with no drivable road (the cell is still loaded: no frontier there)."""
    return dict(feat_ids=[], xy=np.zeros((0, 2)), start=np.zeros(0, np.int64), end=np.zeros(0, np.int64),
                rank=np.zeros(0, np.uint8), conn_feat=np.zeros(0, np.int32), conn_at=np.zeros(0, np.float32),
                conn_ids=[], ow_feat=np.zeros(0, np.int32), ow_val=np.zeros(0, np.float32),
                ow_start=np.zeros(0, np.float32), ow_end=np.zeros(0, np.float32))


def cell_quad(frame, key):
    """The cell's outline (4, 2) in frame's world."""
    import geotiles
    lon0, lat0, lon1, lat1 = key.bounds()
    la = np.array([lat0, lat0, lat1, lat1])
    lo = np.array([lon0, lon1, lon1, lon0])
    x, y, _ = geotiles.geodetic_to_world(frame, la, lo)
    return [[float(a), float(b)] for a, b in zip(np.asarray(x), np.asarray(y))]


class Tree:
    """One grown tree (Reach.grow): pts (n, 4) x, y, eta (s), chance; mpp (k, 3) x, y, eta of the most probable path;
    frontier (m, 4) where the loaded data ends; made at (x, y), (vx, vy), time t (monotonic)."""

    def __init__(self, got, x, y, vx, vy, t):
        self.pts = np.asarray(got["pts"], np.float64).reshape(-1, 4)
        self.mpp = np.asarray(got["mpp"], np.float64).reshape(-1, 3)
        self.frontier = np.asarray(got["frontier"], np.float64).reshape(-1, 4)
        self.states = int(got.get("states", 0))
        self.x, self.y, self.vx, self.vy, self.t = x, y, vx, vy, t
        self.speed = math.hypot(vx, vy)
        self._geo = {}

    def mpp_xy(self):
        """The most probable path's line (k, 2), from where the car was."""
        return self.mpp[:, :2]

    def points_at(self, times):
        """[(x, y)] on the most probable path at these ETAs (s; past its end: straight on from its last way)."""
        m = self.mpp
        if len(m) == 0:
            return [(self.x + self.vx * t, self.y + self.vy * t) for t in times]
        out = []
        for t in times:
            if t <= m[-1, 2] or len(m) < 2:
                j = int(np.searchsorted(m[:, 2], t))
                j = min(max(j, 0), len(m) - 1)
                out.append((float(m[j, 0]), float(m[j, 1])))
            else:
                d = m[-1, :2] - m[-2, :2]
                n = float(dnp.hypot(*d)) or 1.0
                k = (t - m[-1, 2]) * self.speed
                out.append((float(m[-1, 0] + d[0] / n * k), float(m[-1, 1] + d[1] / n * k)))
        return out

    def geotile_keys(self, frame, corridor_m, z=None, p_want=P_WANT, horizon=None):
        """{(gx, gy): (priority s, wanted, eta s, chance, gap m)} of the zoom-z geotiles within corridor_m of the tree's
        points: the smallest priority() + gap / side_speed of the points near each (gap: the point's distance to the
        tile) and that point's eta, chance and gap; wanted when a point with a chance of p_want or more (and an ETA
        within horizon) reaches it. Kept per (frame, z, corridor, ...) for this tree."""
        import geotiles
        z = geotiles.Z if z is None else int(z)
        key = (float(frame.lat0), float(frame.lon0), z, float(corridor_m), p_want, horizon)
        got = self._geo.get(key)
        if got is not None:
            return got
        out = {}
        p = self.pts
        if horizon is not None:
            p = p[p[:, 2] <= horizon]
        if len(p):
            lla = geotiles.world_to_geodetic(frame, np.column_stack([p[:, 0], p[:, 1], np.zeros(len(p))]))
            gx, gy, kk, ww, ee, cc, gg = fast._ef.reach_tile_keys(
                np.ascontiguousarray(lla[0], np.float64), np.ascontiguousarray(lla[1], np.float64),
                np.ascontiguousarray(p[:, 2]), np.ascontiguousarray(p[:, 3]), z, float(corridor_m), float(p_want),
                side_speed(self.speed), GAMMA, P_MIN)
            out = {(a, b): (k, w, e, c, g) for a, b, k, w, e, c, g in zip(gx.tolist(), gy.tolist(), kk.tolist(),
                                                                          ww.tolist(), ee.tolist(), cc.tolist(),
                                                                          gg.tolist())}
        self._geo[key] = out
        return out


class Reach:
    """Per connection: the road graph of the map cells round a fast car on one anchor and its latest tree. Cells come
    from mapdata's disk cache only (has()): never a download of its own (the trackers' and download_ahead's do that)."""

    def __init__(self, mapdata, frame, aid):
        fast.require(NEED, "reach")
        self.mapdata, self.frame, self.aid = mapdata, frame, aid
        self.net = fast._ef.RoadReach()
        self.lock = threading.Lock()
        self.tree = None
        self.grown_at = (-1e9, None)
        self.busy = False
        self.stats = {"cells": 0, "trees": 0, "none": 0, "grow_s": 0.0, "cells_s": 0.0}

    def cells_wanted(self, x, y, vx, vy):
        """The map cells (nearest first) round (x, y) within the horizon's reach (and CELL_REACH_M more)."""
        import geotiles
        import mapcells
        s = math.hypot(vx, vy)
        r = horizon_s(s) * s + CELL_REACH_M
        lla = geotiles.world_to_geodetic(self.frame, np.array([[float(x), float(y), 0.0]]))
        la, lo = float(lla[0][0]), float(lla[1][0])
        dla = r / 111320.0
        dlo = r / (111320.0 * max(0.05, dmath.cos(math.radians(la))))
        keys = mapcells.cells_for_bbox(la - dla, lo - dlo, la + dla, lo + dlo)
        out = []
        for k in keys:
            cla, clo = k.center()
            d = math.hypot((cla - la) * 111320.0, (clo - lo) * 111320.0 * dmath.cos(math.radians(la)))
            out.append((d, k))
        out.sort(key=lambda t: t[0])
        return [k for d, k in out if d <= r + 2500.0], r

    def update_cells(self, x, y, vx, vy, limit=CELLS_PER_ROUND):
        """Load the cells round the car that are on disk and not in the graph yet (at most limit, the nearest first),
        drop the ones out of reach. Worker thread (reads the cells' files; the graph only under the lock: grow runs on
        the event loop). Returns the cells added."""
        t0 = time.perf_counter()
        keys, _r = self.cells_wanted(x, y, vx, vy)
        with self.lock:
            have = set(self.net.cell_keys())
        added = 0
        for k in keys:
            if added >= limit:
                break
            sk = str(k)
            if sk in have:
                continue
            try:
                if not self.mapdata.has(k, "roads"):
                    continue
                arrays, _hit = self.mapdata.layer_arrays(k, "roads", kind="prefetch")
                args = cell_inputs(self.frame, arrays) or empty_inputs()
            except Exception as e:      # (a cell that cannot be read only costs the tree there)
                log.debug("reach: cell %s: %s: %s", k, type(e).__name__, e)
                continue
            quad = cell_quad(self.frame, k)
            with self.lock:
                self.net.add_cell(sk, quad, **args)
            added += 1
        keep = {str(k) for k in keys}
        with self.lock:
            for sk in have - keep:
                self.net.remove_cell(sk)
            self.net.prepare()          # (here, not in the next grow on the event loop: ~ms per 10k roads)
            _nodes, live, dead, _cells = self.net.stats()
        if dead > max(20000, 2 * live):
            self._compact()
        self.stats["cells"] += added
        self.stats["cells_s"] += time.perf_counter() - t0
        return added

    def _compact(self):
        """A fresh graph from the cells in this one (dropped cells' edges leave dead entries behind)."""
        import mapcells
        with self.lock:
            keys = self.net.cell_keys()
        net = fast._ef.RoadReach()
        for sk in keys:
            k = mapcells.parse(sk)
            try:
                arrays, _hit = self.mapdata.layer_arrays(k, "roads", kind="prefetch")
                args = cell_inputs(self.frame, arrays)
            except Exception:
                continue
            net.add_cell(sk, cell_quad(self.frame, k), **(args or empty_inputs()))
        net.prepare()
        with self.lock:
            self.net = net

    def grow(self, x, y, vx, vy, route_xy=None, now=None, intent=None):
        """The tree from (x, y) going (vx, vy) (Tree), or None (slower than MIN_V, on no road of the graph). Kept for
        GROW_EVERY_S / GROW_MOVE_M (the last one returned meanwhile). intent: the driver's (turn, brake) (Intent),
        with earthfast >= INTENT_NEED."""
        now = time.monotonic() if now is None else now
        s = math.hypot(vx, vy)
        if s < MIN_V:
            self.tree = None
            return None
        t_last, at = self.grown_at
        if at is not None and now - t_last < GROW_EVERY_S and math.hypot(x - at[0], y - at[1]) < GROW_MOVE_M:
            return self.tree
        t0 = time.perf_counter()
        route = None
        if route_xy is not None and len(route_xy) >= 2:
            route = np.ascontiguousarray(np.asarray(route_xy, np.float64)[:, :2])
        kw = {}
        if intent is not None and (intent[0] or intent[1]) and getattr(fast._ef, "VERSION", 0) >= INTENT_NEED:
            kw = {"intent_turn": float(intent[0]), "intent_brake": float(intent[1]), "intent_s": INTENT_S}
        with self.lock:
            got = self.net.grow(float(x), float(y), float(vx), float(vy), horizon_s(s), p_min=p_min_at(s),
                                step_m=STEP_M, snap_m=SNAP_M, snap_cos=SNAP_COS, a_turn=A_TURN, v90=V90, t_p=T_P,
                                class_k=class_k_at(s), link_k=LINK_K, max_states=MAX_STATES, route=route,
                                route_m=ROUTE_M, route_share=ROUTE_SHARE, **kw)
        self.stats["grow_s"] += time.perf_counter() - t0
        self.grown_at = (now, (x, y))
        if got is None:
            self.stats["none"] += 1
            self.tree = None
            return None
        self.stats["trees"] += 1
        self.tree = Tree(got, x, y, vx, vy, now)
        return self.tree

    def current(self, now=None):
        """The latest tree while it is fresh (TREE_MAX_AGE_S), else None."""
        t = self.tree
        now = time.monotonic() if now is None else now
        return t if t is not None and now - t.t <= TREE_MAX_AGE_S else None
