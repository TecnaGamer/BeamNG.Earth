"""Clearance log: the space a vehicle needs, checked against the geometry a road tile is made of, one log line per violation.

While the game streams, the server builds every road tile's decks, tunnel tubes, railway meshes and (through the grader) the
ground the physics block is made of. Whether the world is drivable there is only seen in the game, one drive at a time. This
module stands reference vehicles on the tile's own plans, as a real one would stand, and says where the geometry would not let
one through: a truck (12 x 2.55 x 4.0 m) on every carriageway side of every road (both directions of a two-way road, both
carriageways of a dual road: each its own way), a wagon (20 x 3.0 x 4.3 m) on every track. Each violation is one structured
line in the server's own log (server.log), with the place's lat / lon, its frame and tile, the feature, what it hits and how
deep, so a play session's log is a list of test cases that can be rebuilt offline without the game.

A vehicle's body starts BODY_BASE_M over its wheels (what it stands on: a ground road's terrain, which the road is painted into,
the deck's top, the tunnel's floor, the track's rail head). A violation is any other geometry inside the body:
  terrain   the ground the physics block is made of rising into it (a cutting's slope or a mound beside the road, a ridge at
            a junction, the valley a bridge that was never built should span);
  deck, tunnel, rail   another road's or track's deck, a tunnel's tube or retaining wall, a bridge deck too low overhead;
  gap       a stretch of a road between two of its ground parts that has no deck or tunnel (an unbuilt bridge): the vehicle
            would stand on the planned surface straight across, and nothing is under its wheels.
The vehicle's own surface is under its body, never in it.

Cost: the check runs where the tile is built (a road-tile worker process or thread, never the event loop), on numpy arrays:
a vehicle every TRUCK_EVERY_M / WAGON_EVERY_M of road or track, at most MAX_VEHICLES a tile, the terrain read once for all their footprints
(one lattice call), the meshes' triangles looked up in a 16 m grid and tested against the bodies exactly (separating axes,
which also gives the depth). BUDGET_S bounds a tile's check, whatever its size. A fault in it is logged once and never breaks
the tile. BNE_CLEARANCE_LOG=off: no check.
"""
import logging
import math
import os
import threading
import time
from collections import namedtuple

import numpy as np

log = logging.getLogger("earthd.clearance")

Envelope = namedtuple("Envelope", "name length width height")
TRUCK = Envelope("truck", 12.0, 2.55, 4.0)      # m: a full-height road truck (the EU's 4 m)
WAGON = Envelope("wagon", 20.0, 3.0, 4.3)       # m: a full-height wagon (UIC's loading gauge over the rail head)
BODY_BASE_M = 0.5               # m the body starts over the wheels: what the vehicle's own surface and its wheels may fill
MIN_TERRAIN_DEPTH_M = 0.05      # m of terrain in the body before it is a violation (the heightmap's lattice is 1-2 m)
MIN_MESH_DEPTH_M = 0.02         # m of a mesh in the body (the triangles are exact: only float noise is left out)
GAP_DEPTH_M = 0.5               # m the terrain may lie under a vehicle over a gap in a road before nothing is under its wheels
TRUCK_EVERY_M = 24.0            # m between the trucks' middles along a road (every 20-40 m)
WAGON_EVERY_M = 30.0            # m between the wagons' middles along a track
SAG_MAX_M = 0.6                 # m a line may bow from the chord a vehicle's body lies along: a tighter bend a real one
                                # could not take in its lane (the body would be tested against the verge the bend sweeps)
MIN_ROAD_WIDTH_M = 2.8          # m: a narrower way is a track or a path, not a truck's road
NOT_TRUCK = frozenset(("footway", "cycleway", "path", "pedestrian", "steps", "bridleway", "sidewalk", "crossing",
                       "track"))
NOT_WAGON = frozenset(("tram",))
MAX_VEHICLES = 700              # a tile's vehicles at most (decks, tunnels and tracks first, then the ground roads)
BUDGET_S = 2.5                  # s a tile's whole check may take (placement, terrain, meshes): then what was found is kept
CELL_M = 16.0                   # m: the grid the meshes' triangles are looked up in
SAMPLE_U_M, SAMPLE_V_M = 1.5, 0.65   # m between the footprint's terrain samples along and across a body (the lattice is 1-2 m)
TILE_MAX = 25                   # lines of one tile at most (the deepest first) ...
MINUTE_MAX = 240                # ... and of all tiles in a minute: a long drive over a broken region is a few hundred lines
DEDUP_M = 10.0                  # m: one line per place (this grid) and kind of violation in a session
EARTH_M = 111320.0              # m of a degree of latitude (the dedup grid's)
ENV_OFF = "BNE_CLEARANCE_LOG"

Line = namedtuple("Line", "fid cls level xyz width oneway rail gap", defaults=(False,))
# fid: the feature's id; cls: its class (motorway ... , rail); level "ground" | "bridge" | "tunnel"; xyz (k, 3): its plan in the
# tile's frame, z the wheels' surface (a road's planned surface, a track's rail head); width: the carriageway's (m);
# oneway: one direction only; rail: a track; gap: a stretch the road's ground parts leave between them with no deck or tunnel
# built (gap_lines): its z is the planned surface straight between the two ends
Hit = namedtuple("Hit", "env line off x y z hdg what depth at")
# the vehicle's place (its middle over its wheels), its heading (deg from north, clockwise), what it hits ("terrain", "deck",
# "tunnel", "rail", "gap"), how deep (m) and the hit's own place (x, y, z)


def enabled():
    """False with BNE_CLEARANCE_LOG=off (also 0 / false / no): the check does nothing."""
    return os.environ.get(ENV_OFF, "on").strip().lower() not in ("off", "0", "false", "no")


# ---- placing the vehicles -----------------------------------------------------------------------------------------------
def _arc(xyz):
    """(k,) metres along the line in plan."""
    return np.r_[0.0, np.cumsum(np.hypot(*np.diff(xyz[:, :2], axis=0).T))]  # det-ok: the clearance log only


def _at(s, xyz, t):
    """The line's points (n, 3) at the arc lengths t."""
    return np.stack([np.interp(t, s, xyz[:, c]) for c in range(3)], axis=1)  # det-ok: the clearance log only


def _stations(length, env, every):
    """Arc lengths (n,) of the vehicles' middles along a line `length` m long: at most `every` m apart, the first and the last
    half a body from the line's ends (a vehicle fits the line, and both ends are looked at); a line shorter than a body
    has one at its middle, its body longer than the line (a short bridge, a short connector)."""
    if length < 4.0:
        return np.zeros(0)
    if length <= env.length + 1.0:
        return np.array([length / 2.0])
    n = int(math.ceil((length - env.length) / every)) + 1
    return np.linspace(env.length / 2.0, length - env.length / 2.0, n)


def lane_offsets(width, oneway, env):
    """m (+ left) from a carriageway's centre line of the vehicles on it: one in each direction of a two-way road wide enough
    for two bodies side by side (each in the middle of its half), two lanes of a one-way road wide enough for two (a
    motorway's carriageway), else one on the centre line (a narrow road: its traffic uses the middle). Pure."""
    if width < MIN_ROAD_WIDTH_M:
        return []
    if env.name != "truck":
        return [0.0]
    if width >= 2.0 * env.width and not oneway:
        return [width / 4.0, -width / 4.0]
    if width >= 2.5 * env.width and oneway:
        return [width / 4.0, -width / 4.0]
    return [0.0]


def place(lines, owns, ground=None, every=None, limit=MAX_VEHICLES, deadline=None):
    """The reference vehicles on lines (Line): dict of arrays, one row per vehicle: c (n, 3) its middle over its wheels, a
    (n, 3) unit axis along its length (sloping with the line), s (n, 3) unit horizontal side (left), up (n, 3) unit, perpendicular
    to both, env (n,) 0 truck / 1 wagon, line (n,) index into lines, off (n,) its lateral offset, hdg (n,) its heading in
    degrees from north. A vehicle's body lies along the chord of its line between its two ends (the line's points half a
    body before and behind its middle); where the line bows from the chord by more than SAG_MAX_M no vehicle is put (a bend
    tighter than a body can take). owns(x, y) -> bool array: only the vehicles whose middle is the tile's own (each place is
    checked by one tile). Decks, tunnels and tracks first: the limit leaves out ground streets last.
    A ground road is painted into the terrain: its wheels stand on ground(x, y) -> z (read once for all the vehicles, at the
    chord's ends on their own lane), not on its centre line's z, which is only linear between the map's vertices."""
    order = sorted(range(len(lines)), key=lambda i: (0 if lines[i].level == "bridge" else 1 if lines[i].level == "tunnel"
                                                      else 2 if lines[i].rail else 3, i))
    cols = {k: [] for k in ("p0", "p1", "d", "side", "env", "line", "off", "on_ground")}
    total = 0
    for li in order:
        ln = lines[li]
        if total >= limit or (deadline is not None and time.perf_counter() > deadline):
            break
        xyz = np.asarray(ln.xyz, np.float64)
        if len(xyz) < 2 or not np.isfinite(xyz).all():
            continue
        env = WAGON if ln.rail else TRUCK
        offs = lane_offsets(ln.width, ln.oneway, env) if not ln.rail else [0.0]
        if not offs:
            continue
        s = _arc(xyz)
        st = _stations(float(s[-1]), env, every or (WAGON_EVERY_M if ln.rail else TRUCK_EVERY_M))
        if not len(st):
            continue
        half = env.length / 2.0
        s0, s1 = np.clip(st - half, 0.0, s[-1]), np.clip(st + half, 0.0, s[-1])
        p0, p1 = _at(s, xyz, s0), _at(s, xyz, s1)
        chord = (p1 - p0)[:, :2]
        cl = np.hypot(chord[:, 0], chord[:, 1])  # det-ok: the clearance log only
        ok = cl >= 3.0
        d = chord / np.maximum(cl, 1e-9)[:, None]
        sag = np.zeros(len(st))
        for f in (0.25, 0.5, 0.75):         # (a bend anywhere along the body, not only at its middle)
            q = _at(s, xyz, s0 + f * (s1 - s0))
            r = q[:, :2] - p0[:, :2]
            sag = np.maximum(sag, np.abs(d[:, 0] * r[:, 1] - d[:, 1] * r[:, 0]))
        mid = (p0 + p1) / 2.0
        ok &= sag <= SAG_MAX_M
        ok &= np.asarray(owns(mid[:, 0], mid[:, 1]), bool)
        if not ok.any():
            continue
        d, p0, p1 = d[ok], p0[ok], p1[ok]
        side = np.c_[-d[:, 1], d[:, 0], np.zeros(len(d))]
        for off in offs:
            cols["p0"].append(p0 + side * off)
            cols["p1"].append(p1 + side * off)
            cols["d"].append(d)
            cols["side"].append(side)
            cols["env"].append(np.full(len(d), 1 if ln.rail else 0, np.int8))
            cols["line"].append(np.full(len(d), li, np.int64))
            cols["off"].append(np.full(len(d), off))
            cols["on_ground"].append(np.full(len(d), ln.level == "ground" and not ln.rail and not ln.gap))
        total += len(d) * len(offs)
    if not cols["p0"]:
        return {"c": np.zeros((0, 3)), "a": np.zeros((0, 3)), "s": np.zeros((0, 3)), "up": np.zeros((0, 3)),
                "env": np.zeros(0, np.int8), "line": np.zeros(0, np.int64), "off": np.zeros(0), "hdg": np.zeros(0)}
    c = {k: np.concatenate(v) for k, v in cols.items()}
    p0, p1 = c["p0"], c["p1"]
    gi = np.flatnonzero(c["on_ground"])
    if ground is not None and len(gi):
        z = np.asarray(ground(np.r_[p0[gi, 0], p1[gi, 0]], np.r_[p0[gi, 1], p1[gi, 1]]), np.float64)
        z0, z1 = z[:len(gi)], z[len(gi):]
        have = np.isfinite(z0) & np.isfinite(z1)         # (no terrain there: the plan's height stays)
        p0[gi[have], 2], p1[gi[have], 2] = z0[have], z1[have]
    a = p1 - p0
    a /= np.linalg.norm(a, axis=1)[:, None]  # det-ok: the clearance log only
    up = np.cross(a, c["side"])
    d = c["d"]
    return {"c": (p0 + p1) / 2.0, "a": a, "s": c["side"], "up": up, "env": c["env"], "line": c["line"], "off": c["off"],
            "hdg": np.degrees(np.arctan2(d[:, 0], d[:, 1])) % 360.0}  # det-ok: the clearance log only


def _env(k):
    return WAGON if k else TRUCK


def _half(fleet):
    """(n, 3): per vehicle half its length, half its width, half its body's height."""
    e = fleet["env"]
    L = np.where(e == 1, WAGON.length, TRUCK.length)
    W = np.where(e == 1, WAGON.width, TRUCK.width)
    H = np.where(e == 1, WAGON.height, TRUCK.height)
    return np.stack([L / 2.0, W / 2.0, (H - BODY_BASE_M) / 2.0], axis=1), H


# ---- the terrain ------------------------------------------------------------------------------------------------------------
def terrain_hits(fleet, ground, skip=None):
    """(depth (n,), at (n, 3), under (n,)): per vehicle how far the terrain rises into its body (m over the body's floor, at most
    the body's height) at the footprint's samples, where (x, y, z) it is deepest, and the terrain's height under the
    middle of the vehicle. ground(x (m,), y (m,)) -> z (m,) the terrain in the frame, NaN where it has none. skip (n,) bool:
    vehicles with no terrain test (in a tunnel the ground is the hill over it)."""
    n = len(fleet["c"])
    depth, at, under = np.zeros(n), np.full((n, 3), np.nan), np.full(n, np.nan)
    for k in (0, 1):
        idx = np.flatnonzero(fleet["env"] == k)
        if not len(idx):
            continue
        env = _env(k)
        nu = int(env.length / SAMPLE_U_M) | 1                       # (odd: a sample at the middle)
        nv = max(3, int(env.width / SAMPLE_V_M)) | 1
        u = np.linspace(-env.length / 2.0, env.length / 2.0, nu)
        v = np.linspace(-env.width / 2.0, env.width / 2.0, nv)
        c, a, s = fleet["c"][idx], fleet["a"][idx], fleet["s"][idx]
        # (the footprint's horizontal plan: the body's floor is the tilted plane, the terrain is read straight under it)
        xy = (c[:, None, None, :2] + u[None, :, None, None] * a[:, None, None, :2] + v[None, None, :, None] * s[:, None, None, :2])
        z = np.asarray(ground(xy[..., 0].ravel(), xy[..., 1].ravel()), np.float64).reshape(len(idx), nu, nv)
        zc = z[:, nu // 2, nv // 2]
        under[idx] = zc
        z = np.where(np.isfinite(z), z, -np.inf)
        rel = np.stack([xy[..., 0] - c[:, None, None, 0], xy[..., 1] - c[:, None, None, 1], z - c[:, None, None, 2]], axis=-1)
        w = (rel * fleet["up"][idx][:, None, None, :]).sum(-1)          # height over the wheels' plane, square to it
        h = np.clip(w, BODY_BASE_M, env.height) - BODY_BASE_M
        flat = h.reshape(len(idx), -1)
        best = flat.argmax(axis=1)
        depth[idx] = flat[np.arange(len(idx)), best]
        pick = np.stack([xy[..., 0].reshape(len(idx), -1)[np.arange(len(idx)), best],
                         xy[..., 1].reshape(len(idx), -1)[np.arange(len(idx)), best],
                         z.reshape(len(idx), -1)[np.arange(len(idx)), best]], axis=1)
        at[idx] = pick
    if skip is not None:
        depth[np.asarray(skip, bool)] = 0.0
    return depth, at, under


# ---- the meshes -------------------------------------------------------------------------------------------------------------
def _key(cx, cy):
    return (cx + (1 << 20)) * (1 << 21) + (cy + (1 << 20))


class _Grid:
    """Triangles by the CELL_M squares their plan boxes touch (a triangle of more than 4 squares a side is kept apart: tested
    against every vehicle), looked up for many boxes at once."""

    def __init__(self, tris):
        self.n = len(tris)
        lo, hi = tris.min(axis=1), tris.max(axis=1)
        self.lo, self.hi = lo, hi
        c0, c1 = np.floor(lo[:, :2] / CELL_M).astype(np.int64), np.floor(hi[:, :2] / CELL_M).astype(np.int64)
        span = c1 - c0
        big = (span > 3).any(axis=1)
        self.big = np.flatnonzero(big)
        keys, idx = [], []
        for dx in range(4):
            for dy in range(4):
                i = np.flatnonzero(~big & (span[:, 0] >= dx) & (span[:, 1] >= dy))
                if len(i):
                    keys.append(_key(c0[i, 0] + dx, c0[i, 1] + dy))
                    idx.append(i)
        keys = np.concatenate(keys) if keys else np.zeros(0, np.int64)
        idx = np.concatenate(idx) if idx else np.zeros(0, np.int64)
        order = np.argsort(keys, kind="stable")
        self.keys, self.idx = keys[order], idx[order]

    def pairs(self, vlo, vhi):
        """(vehicle, triangle) index arrays of the triangles whose squares meet the vehicle's box (vlo, vhi: (v, 3) its box;
        only the plan is looked up); a pair may come more than once (a triangle in several squares)."""
        c0, c1 = np.floor(vlo[:, :2] / CELL_M).astype(np.int64), np.floor(vhi[:, :2] / CELL_M).astype(np.int64)
        span = c1 - c0
        V, T = [], []
        for dx in range(int(span[:, 0].max()) + 1):
            for dy in range(int(span[:, 1].max()) + 1):
                v = np.flatnonzero((span[:, 0] >= dx) & (span[:, 1] >= dy))
                if not len(v):
                    continue
                k = _key(c0[v, 0] + dx, c0[v, 1] + dy)
                a, b = np.searchsorted(self.keys, k, "left"), np.searchsorted(self.keys, k, "right")
                cnt = b - a
                tot = int(cnt.sum())
                if not tot:
                    continue
                first = np.cumsum(cnt) - cnt
                V.append(np.repeat(v, cnt))
                T.append(self.idx[np.arange(tot) + np.repeat(a - first, cnt)])
        if len(self.big):
            V.append(np.repeat(np.arange(len(vlo)), len(self.big)))
            T.append(np.tile(self.big, len(vlo)))
        if not V:
            return np.zeros(0, np.int64), np.zeros(0, np.int64)
        return np.concatenate(V), np.concatenate(T)


def tri_box_depth(T, h):
    """(n,): how far each triangle T (n, 3, 3) (its corners relative to the middle of a box, in the box's own axes) reaches into
    the box with half sizes h (n, 3): 0 where they do not meet, else the least push (m) that separates them along any of the 13
    separating axes (the box's faces, the triangle's plane, the edges' cross products): the exact depth. Touching is no
    depth."""
    n = len(T)
    best = np.full(n, np.inf)
    sep = np.zeros(n, bool)
    for i in range(3):                              # the box's faces
        p = T[:, :, i]
        lo, hi = p.min(axis=1), p.max(axis=1)
        sep |= (lo >= h[:, i]) | (hi <= -h[:, i])
        best = np.minimum(best, np.minimum(h[:, i] - lo, hi + h[:, i]))
    e = (T[:, 1] - T[:, 0], T[:, 2] - T[:, 1], T[:, 0] - T[:, 2])
    N = np.cross(e[0], e[1])
    ln = np.linalg.norm(N, axis=1)  # det-ok: the clearance log only
    ok = ln > 1e-9
    N = N / np.where(ok, ln, 1.0)[:, None]
    d0 = (N * T[:, 0]).sum(1)
    r = (np.abs(N) * h).sum(1)
    sep |= ok & (np.abs(d0) >= r)
    best = np.minimum(best, np.where(ok, r - np.abs(d0), np.inf))
    zero = np.zeros(n)
    for f in e:                                     # the box's axes x the triangle's edges
        for a in (np.stack([zero, -f[:, 2], f[:, 1]], 1), np.stack([f[:, 2], zero, -f[:, 0]], 1),
                  np.stack([-f[:, 1], f[:, 0], zero], 1)):
            la = np.linalg.norm(a, axis=1)  # det-ok: the clearance log only
            ok = la > 1e-9
            p = (T * a[:, None, :]).sum(2)
            lo, hi = p.min(axis=1), p.max(axis=1)
            r = (np.abs(a) * h).sum(1)
            sep |= ok & ((lo >= r) | (hi <= -r))
            best = np.minimum(best, np.where(ok, np.minimum(r - lo, hi + r) / np.where(ok, la, 1.0), np.inf))
    return np.where(sep | ~np.isfinite(best), 0.0, np.maximum(best, 0.0))


def mesh_hits(fleet, tris, src, deadline=None, chunk=60000):
    """(depth (n, k), at (n, k, 3)): per vehicle and mesh source (src (m,) ints 0..k-1 per triangle of tris (m, 3, 3)) the
    deepest triangle in its body, its depth (0: none) and where its middle is. The body is the box from BODY_BASE_M to the
    envelope's height over the wheels, along the vehicle's own axes."""
    n = len(fleet["c"])
    k = int(src.max()) + 1 if len(src) else 1
    depth, at = np.zeros((n, k)), np.full((n, k, 3), np.nan)
    if not n or not len(tris):
        return depth, at
    half, H = _half(fleet)
    c, a, s, up = fleet["c"], fleet["a"], fleet["s"], fleet["up"]
    mid_w = (BODY_BASE_M + H) / 2.0                 # (the body's middle over the wheels, along the up axis)
    ctr = c + up * mid_w[:, None]
    # the body's box in the world (the plan lookup and a cheap height test)
    ext = (np.abs(a) * half[:, 0:1] + np.abs(s) * half[:, 1:2] + np.abs(up) * half[:, 2:3])
    vlo, vhi = ctr - ext, ctr + ext
    grid = _Grid(tris)
    vi, ti = grid.pairs(vlo, vhi)
    keep = (grid.lo[ti] < vhi[vi]).all(axis=1) & (grid.hi[ti] > vlo[vi]).all(axis=1)     # (the boxes meet, in height too)
    u = np.unique(vi[keep] * len(tris) + ti[keep])          # (each pair once: after the box test, the few that are left)
    vi, ti = u // len(tris), u % len(tris)
    for s0 in range(0, len(vi), chunk):
        if deadline is not None and time.perf_counter() > deadline:
            break
        v, t = vi[s0:s0 + chunk], ti[s0:s0 + chunk]
        rel = tris[t] - ctr[v][:, None, :]
        loc = np.stack([(rel * a[v][:, None, :]).sum(2), (rel * s[v][:, None, :]).sum(2), (rel * up[v][:, None, :]).sum(2)],
                       axis=2)
        d = tri_box_depth(loc, half[v])
        hit = d > MIN_MESH_DEPTH_M
        if not hit.any():
            continue
        vv, tt, dd = v[hit], t[hit], d[hit]
        kk = src[tt]
        # (the deepest triangle of each vehicle and mesh: one sort, no loop over the hits)
        order = np.lexsort((dd, vv * k + kk))
        key = (vv * k + kk)[order]
        pick = order[np.r_[key[1:] != key[:-1], True]]
        better = dd[pick] > depth[vv[pick], kk[pick]]
        pick = pick[better]
        depth[vv[pick], kk[pick]] = dd[pick]
        at[vv[pick], kk[pick]] = tris[tt[pick]].mean(axis=1)
    return depth, at


# ---- a tile's check -----------------------------------------------------------------------------------------------------------
MESH_NAMES = ("deck", "tunnel", "rail")


def find(lines, owns, ground, meshes, deadline=None):
    """[Hit] of the reference vehicles on lines (Line) that the geometry does not let through: the terrain (ground(x, y) -> z)
    and meshes ([(name, triangles (m, 3, 3) in the frame)], MESH_NAMES) in their body. owns(x, y): the tile's own places."""
    fleet = place(lines, owns, ground, deadline=deadline)
    n = len(fleet["c"])
    if not n:
        return []
    lvl = np.array([lines[i].level for i in fleet["line"]])
    out = []
    if ground is not None:
        depth, at, under = terrain_hits(fleet, ground, skip=lvl == "tunnel")
        for i in np.flatnonzero(depth > MIN_TERRAIN_DEPTH_M):
            out.append((i, "terrain", float(depth[i]), at[i]))
        # (over a gap in the road, left by an unbuilt bridge, there is nothing under the wheels)
        gp = np.array([lines[j].gap for j in fleet["line"]])
        for i in np.flatnonzero(gp & np.isfinite(under) & (fleet["c"][:, 2] - under > GAP_DEPTH_M)):
            out.append((i, "gap", float(fleet["c"][i, 2] - under[i]), np.array([fleet["c"][i, 0], fleet["c"][i, 1], under[i]])))
    tris = [np.asarray(t, np.float64).reshape(-1, 3, 3) for _n, t in meshes if len(t)]
    if tris:
        names = [i for i, (_n, t) in enumerate(meshes) if len(t)]
        src = np.concatenate([np.full(len(t), i, np.int64) for i, t in zip(names, tris)])
        depth, at = mesh_hits(fleet, np.concatenate(tris), src, deadline)
        for i, kk in zip(*np.nonzero(depth > 0)):
            out.append((i, meshes[kk][0], float(depth[i, kk]), at[i, kk]))
    hits = []
    for i, what, dep, pt in out:
        c = fleet["c"][i]
        hits.append(Hit(_env(fleet["env"][i]).name, int(fleet["line"][i]), float(fleet["off"][i]), float(c[0]), float(c[1]),
                        float(c[2]), float(fleet["hdg"][i]), what, dep, tuple(float(v) for v in pt)))
    return hits


# ---- the log ----------------------------------------------------------------------------------------------------------------
class Reporter:
    """What is logged of the hits found: one line per place (DEDUP_M grid of lat / lon), vehicle kind and kind of violation in
    this process's life, at most TILE_MAX of a tile (the deepest first) and MINUTE_MAX a minute; what was left out of a
    tile is counted in one more line. Thread safe."""

    def __init__(self, logger=None, tile_max=TILE_MAX, minute_max=MINUTE_MAX, clock=time.monotonic):
        self.log = logger or log
        self.tile_max, self.minute_max, self.clock = tile_max, minute_max, clock
        self.seen = set()
        self.window = []                    # the times of the lines of the last minute
        self.lock = threading.Lock()
        self.failed = False

    def cells(self, lat, lon):
        """The dedup grid's squares (i, j) of points: DEDUP_M each way, the east-west one at its row's latitude."""
        lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
        i = np.floor(lat * EARTH_M / DEDUP_M).astype(np.int64)
        latc = (i + 0.5) * DEDUP_M / EARTH_M
        j = np.floor(lon * EARTH_M * np.cos(np.radians(latc)) / DEDUP_M).astype(np.int64)  # det-ok: the clearance log only
        return i, j

    def emit(self, hits, lines, frame, tile, aid):
        """Log the new ones of hits (find's); returns how many lines were written. lines: the Lines the hits' vehicles stood on
        (name, class, level), frame: the tile's frame (x, y -> lat, lon), tile: its key, aid: the anchor's id or None."""
        if not hits:
            return 0
        hits = sorted(hits, key=lambda h: -h.depth)
        x = np.array([h.x for h in hits])
        y = np.array([h.y for h in hits])
        z = np.array([h.z for h in hits])
        la, lo, _ = frame.enu_to_geodetic(x, y, z)
        ci, cj = self.cells(la, lo)
        written, skipped = 0, 0
        now = self.clock()
        with self.lock:
            self.window = [t for t in self.window if now - t < 60.0]
            for n, h in enumerate(hits):
                key = (h.env, h.what, int(ci[n]), int(cj[n]))
                if key in self.seen:
                    continue
                if written >= self.tile_max or len(self.window) >= self.minute_max:
                    skipped += 1
                    continue
                self.seen.add(key)
                self.window.append(now)
                written += 1
                self.log.info(self.line(h, lines, frame, tile, aid, float(la[n]), float(lo[n])))
            if skipped:
                self.log.info("clearance: tile %s: %d more violation(s) not logged (rate limit)", tile, skipped)
        return written

    @staticmethod
    def line(h, lines, frame, tile, aid, lat, lon):
        """One violation as one log line: the vehicle, its feature and place (lat / lon, x y z in the tile's frame, the frame's
        own lat / lon, the tile's key and the anchor), what it hits and how deep, the hit's place; everything to rebuild the
        spot offline."""
        ln = lines[h.line]
        what = {"terrain": "terrain", "deck": "a deck", "tunnel": "a tunnel", "rail": "a railway",
                "gap": "has no deck or tunnel under it (an unbuilt bridge), terrain below"}
        verb = "hits " if h.what in ("terrain", "deck", "tunnel", "rail") else ""
        return ("clearance: %s on %s %s (%s, %s) at x %.1f y %.1f z %.1f lat %.6f lon %.6f lane %+.1f heading %.0f "
                "tile %s frame %.6f %.6f anchor %s %s%s by %.2f m (at x %.1f y %.1f z %.1f)"
                % (h.env, "rail" if ln.rail else "road", ln.fid, ln.cls, ln.level, h.x, h.y, h.z, lat, lon, h.off, h.hdg,
                   tile, frame.lat0, frame.lon0, "-" if aid is None else aid, verb, what.get(h.what, h.what), h.depth,
                   h.at[0], h.at[1], h.at[2]))


_reporter = Reporter()


def run(build, frame, tile, aid=None, reporter=None):
    """find + the log for one tile; never raises (a fault, in build too, is logged once as a warning and the tile goes on).
    build() -> (lines, owns, ground, meshes) as find takes them (made inside the guard: reading a tile's plans can fail too,
    and only when the check is on). Returns the hits found (the log's dedup and rate limit aside); [] when the check is off or
    failed."""
    if not enabled():
        return []
    rep = reporter or _reporter
    t0 = time.perf_counter()
    try:
        lines, owns, ground, meshes = build()
        t1 = time.perf_counter()
        hits = find(lines, owns, ground, meshes, deadline=t1 + BUDGET_S)
        rep.emit(hits, lines, frame, tile, aid)
        t2 = time.perf_counter()
        log.debug("clearance: tile %s: %d line(s), %d violation(s), %.0f ms (plans and meshes %.0f, check %.0f)", tile,
                  len(lines), len(hits), (t2 - t0) * 1000, (t1 - t0) * 1000, (t2 - t1) * 1000)
        return hits
    except Exception as ex:             # (a diagnostic never breaks the tile)
        if not rep.failed:
            rep.failed = True
            log.warning("clearance: check failed on tile %s (further failures not logged): %s: %s", tile, type(ex).__name__, ex,
                        exc_info=True)
        return []


# ---- a road tile's parts -------------------------------------------------------------------------------------------------------
def tile_lines(roads, decks, tunnels, tracks, rail_top_m):
    """[Line] of a road tile's plans. roads: [(fid, spec, centre line (k, 3), lon)] its ground carriageways (roads.build_tile's
    roads_out; z: the physics ground under them); decks, tunnels: [(fid, spec, xy (k, 2), z (k,), lon)] its bridge and tunnel
    parts (the decks' lateral shifts at merges applied); tracks: [(fid, class, kind, xy (k, 2), bed z (k,))] its railways
    (railways.tile_mesh tracks_out), rail_top_m: the rail head over the bed. Footways, tracks and the like carry no truck, trams
    no wagon."""
    out = []
    for fid, spec, pts, _lon in roads or ():
        if spec.cls in NOT_TRUCK:
            continue
        out.append(Line(str(fid), spec.cls, "ground", np.asarray(pts, np.float64)[:, :3], float(spec.width), bool(spec.oneway),
                        False))
    for level, parts in (("bridge", decks), ("tunnel", tunnels)):
        for fid, spec, xy, z, _lon in parts or ():
            if spec.cls in NOT_TRUCK:
                continue
            out.append(Line(str(fid), spec.cls, level, np.c_[np.asarray(xy, np.float64)[:, :2], np.asarray(z, np.float64)],
                            float(spec.width), bool(spec.oneway), False))
    for fid, cls, kind, xy, zb in tracks or ():
        if cls in NOT_WAGON:
            continue
        out.append(Line(str(fid), cls, kind, np.c_[np.asarray(xy, np.float64)[:, :2], np.asarray(zb, np.float64) + rail_top_m],
                        0.0, False, True))
    return out + gap_lines(roads, decks, tunnels)


GAP_MIN_M, GAP_MAX_M = 3.0, 120.0       # m between the ends of two ground parts of a road: a bridge (or tunnel) left unbuilt
GAP_COVER_M = 6.0                       # m: a deck or tunnel part of the same road this near the gap's middle fills it
GAP_ALIGN = 0.7                         # cosine: how nearly each part's end must point across the gap at the other


def gap_lines(roads, decks, tunnels):
    """[Line] of the stretches of a road between two of its ground parts (roads: build_tile's roads_out) that have no deck or
    tunnel part of the same road (decks, tunnels: the bridge and tunnel parts) in them: a bridge that was never built, the road
    stopping at one edge of a gorge and going on at the other's. The planned surface is the straight line between the two ends
    (the road's own heights there); a truck on it floats over a gorge or is buried in a ridge. Pure."""
    parts = {}
    for fid, spec, pts, _lon in roads or ():
        pts = np.asarray(pts, np.float64)
        if len(pts) >= 2 and np.isfinite(pts[:, :3]).all() and spec.cls not in NOT_TRUCK:
            parts.setdefault(str(fid), []).append((spec, pts[:, :3]))
    built = {}
    for fid, _spec, xy, _z, _lon in list(decks or ()) + list(tunnels or ()):
        built.setdefault(str(fid), []).append(np.asarray(xy, np.float64)[:, :2])
    out = []
    for fid, ps in parts.items():
        for i in range(len(ps)):
            for j in range(i + 1, len(ps)):
                d, a, b = min((float(np.hypot(*(ps[i][1][a, :2] - ps[j][1][b, :2]))), a, b) for a in (0, -1) for b in (0, -1))  # det-ok: the clearance log only
                if not GAP_MIN_M <= d <= GAP_MAX_M:
                    continue
                p, q = ps[i][1][a], ps[j][1][b]
                # (the road goes on across it: each part's end looks towards the other's, not a hairpin's far leg)
                v = (q - p)[:2] / d
                ta = ps[i][1][a, :2] - ps[i][1][1 if a == 0 else -2, :2]
                tb = ps[j][1][b, :2] - ps[j][1][1 if b == 0 else -2, :2]
                if float(ta @ v) < GAP_ALIGN * np.hypot(*ta) or float(tb @ -v) < GAP_ALIGN * np.hypot(*tb):  # det-ok: the clearance log only
                    continue
                mid = (p[:2] + q[:2]) / 2.0
                if any(float(np.hypot(*(xy - mid).T).min()) <= d / 2.0 + GAP_COVER_M for xy in built.get(fid, ())):  # det-ok: the clearance log only
                    continue
                spec = ps[i][0]
                out.append(Line(fid, spec.cls, "bridge", np.stack([p, q]), float(spec.width), bool(spec.oneway), False, True))
    return out


def tile_meshes(origin, **meshes):
    """[(name, triangles (m, 3, 3))] of a tile's meshes (roadmesh's (positions, tris, ...) or None, positions relative to
    origin), by their names in MESH_NAMES."""
    out = []
    o = np.asarray(origin, np.float64)
    for name in MESH_NAMES:
        m = meshes.get(name)
        if m is None or not len(m[1]):
            continue
        P, T = np.asarray(m[0], np.float64) + o, np.asarray(m[1], np.int64)
        out.append((name, P[T]))
    return out
