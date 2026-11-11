"""The rail beds' heights as one surface over the anchor, the same for every tile and every grading cell (plan 035).

railways.profile lays a bed along the lines inside ONE window (a tile's, a grading cell's): a bridge deck or a road tunnel
just outside the window's edge changes the bed inside it (the deck's ramp climbs 2 % for hundreds of metres), so two
tiles drew one rail at two heights, a step of 0.85 m at a seam in Kyiv (railcheck.py), and the grader's plan, laid with
60 m of reach, differed from the mesh, laid with 200 m.

Here the beds are laid at the nodes of a lattice (a tile corner each), every node with a window much wider than the
reach of what shapes a bed: its own profile, a pure function of (anchor, node). A point's bed is the nodes' beds around
it (the four corners of its lattice cell) weighted bilinearly, so it is the same whichever tile or cell asks, and
continuous across every seam by construction (the weights are). The elevation is the grader's (plan_store.elev): the mesh reads it
in its own frame (the ENU heights, railways.point_ground), the grader as the DEM's heights, each with its own lattice (a
frame's), so the mesh's bed is the bed the ground is graded to to within the nodes' blending. Pure given its inputs; BNE_RAIL_PROFILE=off keeps the per-window beds."""
import math
import os
from collections import OrderedDict

import numpy as np

import railways
import roads

ENABLED = os.environ.get("BNE_RAIL_PROFILE", "on") not in ("off", "0", "false")
SMOOTH_ON = os.environ.get("BNE_RAIL_SMOOTH", "on") not in ("off", "0", "false")
NODE_M = roads.TILE                 # m between the nodes (a tile corner each)
MARGIN_M = 350.0                    # m of line taken round a node's support (+-NODE_M) for its profile
SMOOTH_STEPS = 25                   # steps (STEP_M each) either side the ground beds are averaged over, twice more
MAX_CUT_M = 3.0                     # m the long smoothing moves a bed at most (cuttings and embankments stay modest)
PIN_M = 30.0                        # m from a part's end the bed is left as railways.profile made it (a joint, a deck)
JOIN_ON = os.environ.get("BNE_RAIL_SWITCH_FIX", "on") not in ("off", "0", "false")
JOIN_M = 2.0                        # m: a ground part's end this near another part's line (not near its ends) is a switch
JOIN_INTERIOR_M = 3.0               # m: ... farther than this from the other part's own ends (that is a joint: profile's)
JOIN_REACH_M = 60.0                 # m along the part the bed is brought to the other's (to nothing, smoothly)
NODE_MEMO_MAX = 48                  # nodes kept (each a few thousand points)
_NODES = OrderedDict()              # (anchor key, ix, iy) -> {point key: bed height}


def _key(fid, kind, xy):
    """Per point of xy (n, 2): (fid, kind, x in cm, y in cm). The points lie every STEP_M from their part's start whatever
    the window (railways.parts), so the same point has the same key in every node's window."""
    k = np.round(np.asarray(xy, np.float64) * 100.0).astype(np.int64)
    return [(fid, kind, a, b) for a, b in k.tolist()]


def smooth(xy, zb):
    """A ground part's bed (railways.profile's: ground heights averaged over 20 m) averaged again over SMOOTH_STEPS either
    side, twice: a railway rides no bump of the DEM (a main line's vertical curves are kilometres long). It moves the bed
    by MAX_CUT_M at most, and not at all at the part's ends (PIN_M: a joint with another part, a deck's ramp)."""
    zb = np.asarray(zb, np.float64)
    if len(zb) < 8:
        return zb
    r = min(SMOOTH_STEPS, (len(zb) - 1) // 2)
    z = railways._avg(railways._avg(zb, r), max(1, r // 2))
    d = np.clip(z - zb, -MAX_CUT_M, MAX_CUT_M)
    s = railways._arc(xy)
    edge = np.minimum(s, s[-1] - s) / PIN_M
    w = np.clip(edge, 0.0, 1.0)
    return zb + d * (w * w * (3.0 - 2.0 * w))


def _on_line(p, xy, s):
    """(distance m, arc m along xy of the point of it nearest p) for the line xy (k, 2) with arcs s."""
    a, b = xy[:-1], xy[1:]
    ab = b - a
    t = np.clip(((p - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
    d = np.hypot(*(a + t[:, None] * ab - p).T)
    k = int(np.argmin(d))
    return float(d[k]), float(s[k] + t[k] * (s[k + 1] - s[k]))


def join_beds(rails, zbs, inside=None):
    """zbs (the ground parts' beds, one array per part of rails [(fid, class, kind, xy)]) with each part's end that lies on
    another part's line (a switch: a siding, a crossover's end; JOIN_M from it, JOIN_INTERIOR_M from its ends) brought
    to that part's bed there: the difference run out along the part over JOIN_REACH_M (a part tied at both ends over its
    whole length, a smooth blend of the two). railways.profile and smooth lay a short part's bed from its own ground, 0.45 m
    over and 0.58 m under the main lines a crossover joins at Kalynivka, a step across the rails (plan 040). inside:
    (x0, y0, x1, y1) the window; an end outside it is a clipped one, left alone. Pure."""
    if not JOIN_ON:
        return zbs
    ground = [n for n, (_f, c, k, xy) in enumerate(rails) if k == "ground" and c != "tram" and len(xy) >= 2]
    arcs = {n: railways._arc(np.asarray(rails[n][3], np.float64)) for n in ground}
    boxes = {n: (np.asarray(rails[n][3], np.float64).min(0) - JOIN_M, np.asarray(rails[n][3], np.float64).max(0) + JOIN_M)
             for n in ground}
    out = [np.asarray(z, np.float64).copy() for z in zbs]
    for p in ground:
        xy = np.asarray(rails[p][3], np.float64)
        s = arcs[p]
        S = float(s[-1])
        delta = [None, None]
        for e, idx in enumerate((0, len(xy) - 1)):
            pt = xy[idx]
            if inside is not None and not (inside[0] <= pt[0] <= inside[2] and inside[1] <= pt[1] <= inside[3]):
                continue
            best = None
            for q in ground:
                if q == p or rails[q][0] == rails[p][0]:
                    continue
                lo, hi = boxes[q]
                if (pt < lo).any() or (pt > hi).any():
                    continue
                qxy = np.asarray(rails[q][3], np.float64)
                d, at = _on_line(pt, qxy, arcs[q])
                if d < JOIN_M and JOIN_INTERIOR_M < at < arcs[q][-1] - JOIN_INTERIOR_M and (best is None or d < best[0]):
                    best = (d, float(np.interp(at, arcs[q], np.asarray(zbs[q], np.float64))))
            if best is not None and np.isfinite(zbs[p][idx]):
                delta[e] = best[1] - float(zbs[p][idx])
        if delta[0] is None and delta[1] is None:
            continue
        both = delta[0] is not None and delta[1] is not None
        L = min(JOIN_REACH_M, S)
        corr = np.zeros(len(xy))
        if both and S <= 2 * L:
            u = np.clip(s / max(S, 1e-9), 0.0, 1.0)     # (linear: a part laid on a slope stays as straight)
            z0, z1 = float(zbs[p][0]) + delta[0], float(zbs[p][-1]) + delta[1]
            out[p] = z0 + (z1 - z0) * u          # (a crossover is a straight grade between the two beds it joins:
            continue                             # its own ground's bumps between them were 0.3 m over the lines)
        else:
            for e, d in enumerate(delta):
                if d is None:
                    continue
                dist = s if e == 0 else S - s
                w = np.clip(1.0 - dist / max(L, 1e-9), 0.0, 1.0)
                corr += d * w * w * (3.0 - 2.0 * w)
        out[p] = out[p] + corr
    return out


_NEIGHBOURS = ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1))


def _find(node, key):
    """node's height at key, or at a key a centimetre off (two windows' float noise round a point lying on a centimetre's
    edge to neighbouring keys), or NaN."""
    fid, kind, a, b = key
    for da, db in _NEIGHBOURS:
        z = node.get((fid, kind, a + da, b + db))
        if z is not None:
            return z
    return math.nan


def _node(frame, akey, load, ground, ix, iy):
    """{point key: bed height} of node (ix, iy): the lines of a window NODE_M + MARGIN_M round it, laid by railways.profile
    on ground's heights (the underpasses read in the same window)."""
    ck = (akey, ix, iy)
    got = _NODES.get(ck)
    if got is not None:
        _NODES.move_to_end(ck)
        return got
    cx, cy = ix * NODE_M, iy * NODE_M
    box = (cx - NODE_M, cy - NODE_M, cx + NODE_M, cy + NODE_M)
    reach = MARGIN_M
    cells = [load(k) for k in railways.cells_near(frame, box, reach)]
    rails = [r for r in railways.parts(frame, cells, box, reach=reach, clip=True) if r[1] != "tram"]
    out = {}
    if rails:
        tunnels = railways.road_underpasses(frame, cells, box, reach)
        tunnels = tunnels + railways.coarse_underpasses(frame, cells, box, rails, reach)
        zbs = []
        for (fid, _c, kind, xy), (zb, _zg) in zip(rails, railways.profile(rails, ground, tunnels)):
            if kind == "ground" and SMOOTH_ON:
                zb = smooth(xy, zb)
            zbs.append(zb)
        inner = (box[0] - reach + 3 * railways.STEP_M, box[1] - reach + 3 * railways.STEP_M,
                 box[2] + reach - 3 * railways.STEP_M, box[3] + reach - 3 * railways.STEP_M)
        for (fid, _c, kind, xy), zb in zip(rails, join_beds(rails, zbs, inner)):
            out.update(zip(_key(fid, kind, xy), np.asarray(zb).tolist()))
    _NODES[ck] = out
    while len(_NODES) > NODE_MEMO_MAX:
        _NODES.popitem(last=False)
    return out


def beds(frame, akey, load, ground, rails):
    """Per part of rails ([(fid, class, kind, xy)]): its bed heights (n,) from the nodes round each point, NaN where no node
    has the point (a part the node's window dropped, a tram: the caller lays those by railways.profile)."""
    out = []
    for fid, cls, kind, xy in rails:
        xy = np.asarray(xy, np.float64)
        z = np.full(len(xy), np.nan)
        if cls != "tram" and len(xy):
            keys = _key(fid, kind, xy)
            fx, fy = xy[:, 0] / NODE_M, xy[:, 1] / NODE_M
            ix, iy = np.floor(fx).astype(np.int64), np.floor(fy).astype(np.int64)
            tx, ty = fx - ix, fy - iy
            num, den = np.zeros(len(xy)), np.zeros(len(xy))
            for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
                w = (tx if dx else 1.0 - tx) * (ty if dy else 1.0 - ty)
                for cell in set(zip((ix + dx).tolist(), (iy + dy).tolist())):
                    m = (ix + dx == cell[0]) & (iy + dy == cell[1]) & (w > 1e-9)
                    if not m.any():
                        continue
                    node = _node(frame, akey, load, ground, cell[0], cell[1])
                    idx = np.flatnonzero(m)
                    zz = np.array([_find(node, keys[i]) for i in idx])
                    ok = np.isfinite(zz)
                    num[idx[ok]] += w[idx[ok]] * zz[ok]
                    den[idx[ok]] += w[idx[ok]]
            have = den > 1e-9
            z[have] = num[have] / den[have]
        out.append(z)
    return out
