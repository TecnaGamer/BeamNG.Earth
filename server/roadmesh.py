"""Road surfaces and markings as one render-only mesh per road tile (docs/ARCHITECTURE.md "Roads").

Why not DecalRoads (2026-10-11, the user's drive at speed): a DecalRoad re-clips itself to the
terrain whenever the TerrainBlock changes, and ours moves every few seconds at speed. With 3,740
DecalRoads one block move cost a 140-148 ms frame (9 ms without them; ~37 us per decal), so the
game stuttered on every block commit. The official maps never move their terrain.

Here every chunk of roads.build_tile (asphalt, cobble strips, lines, gutters, drains) becomes a
ribbon (the DecalRoad's own layout: u 0..1 across, v = distance / texture length along) draped on the
graded ground: the ribbon is cut every DRAPE_STEP m and both edges sampled from the same elevation
function as the physics block (raised ROAD_LIFT under roads over the visible ground tiles, so the
mesh lies on what the tyres touch and above what is drawn), in one call per tile, plus its layer's
LIFT (markings over the asphalt). The stock decal
materials render on a mesh (probe 2026-10-11: they are translucent with zwrite, so overlapping
ribbons in one mesh draw in a fixed order and do not flicker). No collision: the tyres stay on the
terrain, whose surface masks carry the asphalt / dirt ground models. Pure given elev.
"""
from runtimeprofiler import step as profile_step
import math
import os

import numpy as np

import cdae
import heightmap
import portals
import shells

DRAPE_STEP = 2.0        # m between ribbon cross-sections (the physics block's lattice)
# Metres above the physics ground, which grading.ROAD_LIFT raises under roads.
LIFT = {"base": 0.0, "edge": 0.001, "gutter": 0.0015, "mark": 0.002, "frontage": 0.03,
        # road wear (roadwear.py): its materials write no depth (roadwear.materials.json), so their order in the mesh,
        # not their height, decides what lies on top: the strips (fade materials) first, the paint over them
        # (2026-11-11, the user: "can we make the road decals even closer to the ground?": 3-3.2 cm -> 1.2-1.4 cm, the
        # crack lines on the paint's own tight ground following)
        # then (the user: "make both a pixel or 2 above the ground so they dont z fight"): the paint 1 cm -> 5 mm, the wear
        # 6-7 mm, the spots 4 mm (each wear layer's lift its own: the strips are told apart by it)
        # then (the user: "they still float. i want to make them virtualy the same height of the road"): paint 2 mm, the
        # wear 2.2-2.6 mm, spots 1.8 mm (FOLLOW_TOL stays 2 cm: the ground steps by 1/32 m)
        "wear": 0.0022, "wear2": 0.0024, "spot": 0.0018, "wline": 0.0026}
# The wear strips may depart further from the drawn ground than the paint before follow_ground cuts them, lifted about
# as far (a soft decal shows no parallax): a motorway tile's lanes of tyre polish (I-95, Baltimore) were +21k triangles
# at FOLLOW_TOL, +9k at 4 cm. The spots (small quads) keep FOLLOW_TOL.
WEAR_STRIPS = ("wear", "wear2")
WEAR_TOL = float(os.environ.get("BNE_WEAR_TOL", "0.02"))
FRONTAGE_ASPHALT = "bne_eu_frontage_asphalt"
FRONTAGE_REPEAT_M = 5.
FRONTAGE_TERRAIN_REPEAT_M = 4.
FRONTAGE_TEXEL_M = .2

# Dirt and gravel strips (from PR #455 by AnthonyGraceffa; roads.DIRT_STRIPS): BeamNG's continuous rutted road strips
# (roads.DIRT_LOOK, our copies in dirtroad.materials.json) over the ground photo's tint, fading into the ground.
# A strip follows the mapped vertices, so a bend was one mitre and the ruts kinked: round_dirt_centerline replaces a
# bend sharper than DIRT_BEND_DEG with an arc of radius DIRT_BEND_R (less on short arms), cut into DIRT_BEND_STEP_DEG
# pieces, on the mapped segments (not the 12 m densify, which left a corner a truck cannot take). Its two corners may
# use DIRT_BEND_SEG of a segment between them, the inner edge may sit DIRT_EDGE_SLACK m past the graded half width,
# and a bend whose arms cannot hold an arc with DIRT_INNER_MIN m inside it keeps its point.
DIRT_BEND_DEG = 10.0
DIRT_BEND_STEP_DEG = 5.0
DIRT_BEND_R = 28.0          # m, a bend a vehicle follows instead of a polygon joint
DIRT_BEND_SEG = 0.85
DIRT_INNER_MIN = 0.4
DIRT_EDGE_SLACK = 12.0
# The fade: the materials' opacity map (dirt_fade_o.data.png) on the second uv (the fade uv: vertex alpha only tints
# the colour). Its u is 0 / 1 on the strip's edges and 0.5 over the solid middle, EDGE_FADE_M in from each edge (at most
# EDGE_FADE_SHARE of the width: a narrow track keeps a middle); its v is 0 at an open start, 1 at an open end and 0.5
# from END_FADE_M in (at most END_FADE_SHARE of a short road), the map's own ramps in FADE_V_BAND of each end. An end
# where the road joins another strip or goes on over a bridge does not fade (roads._strip_open_ends). Anything else in
# a mesh that has a strip samples the solid middle.
FADE_PREFIX = ("bne_dirt_", "bne_wear_")   # the materials that fade (dirtroad.materials.json, roadwear.materials.json)
EDGE_FADE_M = 1.2
EDGE_FADE_SHARE = 0.38
END_FADE_M = 14.0
END_FADE_SHARE = 0.3
FADE_V_BAND = 0.25


def round_dirt_centerline(pts, half):
    """pts (k, 3) with each bend sharper than DIRT_BEND_DEG replaced by an arc of about DIRT_BEND_R (less on a short
    arm, or where a wider arc would put the inner edge more than DIRT_EDGE_SLACK past the half width `half`); the ends
    stay. Vectorized (PR #455's per-corner loops, in numpy). Pure."""
    P = np.asarray(pts, np.float64)
    n = len(P)
    half = float(half)
    if n < 3 or half <= 0.05:
        return P
    d = np.diff(P[:, :2], axis=0)
    seg = np.hypot(d[:, 0], d[:, 1])
    lab, lbc = seg[:-1], seg[1:]                         # the arms of the corners 1 .. n-2
    ok = (lab >= 0.5) & (lbc >= 0.5)
    t0 = d[:-1] / np.maximum(lab, 1e-9)[:, None]
    t1 = d[1:] / np.maximum(lbc, 1e-9)[:, None]
    delta = np.arctan2(t0[:, 0] * t1[:, 1] - t0[:, 1] * t1[:, 0], (t0 * t1).sum(1))
    alpha = np.abs(delta) / 2.0
    cos_a = np.cos(alpha)
    bend = 1.0 - cos_a
    ok &= (np.abs(delta) > math.radians(DIRT_BEND_DEG)) & (cos_a >= 1e-3) & (bend >= 1e-8)
    if not ok.any():
        return P
    radius = np.minimum(DIRT_BEND_R, half + DIRT_EDGE_SLACK / np.where(ok, bend, 1.0))
    want = np.where(ok, radius * np.tan(np.where(ok, alpha, 0.0)), 0.0)
    back = np.r_[0.0, want, 0.0]                         # per vertex: m of arm used before it ...
    fwd = back.copy()                                    # ... and after it
    budget = DIRT_BEND_SEG * seg                         # per segment: what its two corners may use between them
    need = fwd[:-1] + back[1:]
    scale = np.where(budget <= 1e-6, 0.0, np.where(need > budget, budget / np.maximum(need, 1e-12), 1.0))
    fwd[:-1] *= scale
    back[1:] *= scale
    dd = np.minimum(back, fwd)[1:-1]
    ok &= dd >= 0.3
    r = np.where(ok, dd / np.tan(np.where(ok, alpha, 1.0)), 0.0)
    ok &= r >= half + DIRT_INNER_MIN
    if not ok.any():
        return P
    c = np.nonzero(ok)[0]                                # corners kept (vertex c + 1)
    a, b, e = P[c], P[c + 1], P[c + 2]
    dc, rc, dl, la, lb = dd[c], r[c], delta[c], lab[c], lbc[c]
    tc0, tc1 = t0[c], t1[c]
    T0 = b[:, :2] - tc0 * dc[:, None]
    z0 = a[:, 2] + (b[:, 2] - a[:, 2]) * ((la - dc) / la)
    z1 = b[:, 2] + (e[:, 2] - b[:, 2]) * (dc / lb)
    side = np.where(dl > 0.0, 1.0, -1.0)
    centre = T0 + (side * rc)[:, None] * np.stack([-tc0[:, 1], tc0[:, 0]], 1)
    a0 = np.arctan2(T0[:, 1] - centre[:, 1], T0[:, 0] - centre[:, 0])
    steps = np.maximum(1, np.ceil(np.abs(dl) / math.radians(DIRT_BEND_STEP_DEG))).astype(np.int64)
    cnt = steps + 1
    which = np.repeat(np.arange(len(c)), cnt)
    kth = np.arange(int(cnt.sum())) - np.repeat(np.cumsum(cnt) - cnt, cnt)     # 0 .. steps along each arc
    f = kth / np.repeat(steps, cnt)
    ang = a0[which] + dl[which] * f
    arcs = np.c_[centre[which, 0] + rc[which] * np.cos(ang), centre[which, 1] + rc[which] * np.sin(ang),
                 z0[which] + (z1[which] - z0[which]) * f]
    if P.shape[1] > 3:                                   # (extra columns: the corner's own)
        arcs = np.c_[arcs, P[c + 1][which, 3:]]
    per = np.ones(n, np.int64)                           # points each vertex becomes, in order
    per[c + 1] = cnt
    out = np.repeat(P, per, axis=0)
    start = np.cumsum(per) - per
    out[np.repeat(start[c + 1], cnt) + kth] = arcs
    keep = np.r_[True, np.hypot(*np.diff(out[:, :2], axis=0).T) >= 1e-3]
    out = out[keep]
    return out if len(out) >= 2 else P


LAYERS_OF_MAT = {}      # material -> the layers it was drawn in (ribbons): one only ever drawn over another surface
                        # (markings, gutters, edge blends: layer != "base") is an overlay, left single-faced (write)


def overlay_material(name):
    """Whether the road mesh's material name is only ever an overlay (drawn over the closed road surface or the
    ground: no hole to see through, so not closed itself; shells.close open_tris)."""
    s = LAYERS_OF_MAT.get(name)
    return bool(s) and "base" not in s


def layer_of(chunk):
    if chunk.get("layer") in LIFT:             # a chunk naming its own (rails.py: sleepers over ballast, rails over both)
        return chunk["layer"]
    m = chunk["m"]
    if m.startswith("line_") or m.startswith("crossing") or m == "m_metal_grid_drain":
        return "mark"
    if m == "gutter1":
        return "gutter"
    return "base"


def _resample(pts, step, keep_vertices=False, corner_half=0.0):
    """Points every `step` m along a polyline (k, 3), ends kept: (xy (m, 2), distance (m,), z (m,))."""
    seg = np.hypot(*np.diff(pts[:, :2], axis=0).T)
    s = np.concatenate(([0.0], np.cumsum(seg)))
    if s[-1] <= 1e-6:
        return pts[:1, :2], s[:1], pts[:1, 2]
    n = max(1, int(np.ceil(s[-1] / step)))
    t = np.linspace(0.0, s[-1], n + 1)
    if keep_vertices:
        # Prefer the original profile knots over almost coincident lattice
        # samples: tiny segments destabilize the ribbon's miter normals.
        j = np.searchsorted(s, t)
        distance = np.minimum(np.abs(t - s[np.maximum(j - 1, 0)]),
                              np.abs(t - s[np.minimum(j, len(s) - 1)]))
        t = np.unique(np.r_[t[distance > 1e-8], s])
        if corner_half > 0 and len(pts) > 2:
            # A fixed deck keeps its mapped height knots. Additional lattice rows closer
            # to a corner than its offset intersection fold the inner edge backwards.
            # Remove only those inserted rows, including the parapet's width in the reach.
            d = np.diff(pts[:, :2], axis=0)
            unit = d / np.maximum(seg[:, None], 1e-9)
            cos = np.clip(np.sum(unit[:-1] * unit[1:], axis=1), -1., 1.)
            original = np.isin(t, s)
            keep = np.ones(len(t), bool)
            for j in np.flatnonzero((cos < 1 - 1e-10) & (cos > -1 + 1e-8)):
                reach = corner_half * np.sqrt((1 - cos[j]) / (1 + cos[j]))
                keep &= original | (np.abs(t - s[j + 1]) >= reach + 1e-8)
            t = t[keep]
    return (np.stack([np.interp(t, s, pts[:, 0]), np.interp(t, s, pts[:, 1])], axis=1), t,
            np.interp(t, s, pts[:, 2]))


def _normals(xy):
    d = np.diff(xy, axis=0)
    L = np.hypot(d[:, 0], d[:, 1])
    L[L < 1e-9] = 1e-9
    t = d / L[:, None]
    ns = np.stack([-t[:, 1], t[:, 0]], axis=1)
    n = np.empty((len(xy), 2))
    n[0], n[-1] = ns[0], ns[-1]
    if len(xy) > 2:
        m = ns[:-1] + ns[1:]
        ml = np.hypot(m[:, 0], m[:, 1])
        ml[ml < 1e-9] = 1.0
        m = m / ml[:, None]
        cos = np.clip((m * ns[1:]).sum(1), 0.5, 1.0)
        n[1:-1] = m / cos[:, None]
    return n


def ribbon_pairs(chunks):
    """[(ribbon, its chunk)]: ribbons() of each chunk with it (ribbons() leaves out a chunk too short); one batched
    ribbons() call (per chunk it was the cost the batch removed)."""
    return [(r, c) for r, c in zip(ribbons(chunks, keep_none=True), chunks) if r is not None]


def ribbons(chunks, keep_none=False):
    """Per chunk: (left (k, 2), right (k, 2), v (k,), material, layer, z (k,) or None) in the anchor
    frame; z: the chunk's own heights where it is "fixed" (bridge decks), else None (draped). Pure. The draped and
    plain chunks' resampling and normals in one Rust call (earthfast.ribbon_edges: numpy per chunk was ~0.9 s of 8
    city road tiles, 2026-10-28); a fixed deck's (its profile's knots kept) here."""
    import fast
    out = [None] * len(chunks)
    batch, pts, half = [], [], []
    for k, c in enumerate(chunks):
        p = np.asarray(c["n"], np.float64)
        if len(p) < 2:
            continue
        if c.get("deck") and c.get("fixed"):
            corner_half = float(np.max(c.get("ws", c["w"]))) / 2 + PARAPET_W
            xy, s, z = _resample(p, DRAPE_STEP, keep_vertices=True, corner_half=corner_half)
            if len(xy) < 2:
                continue
            hw = float(c["w"]) / 2
            if c.get("ws") is not None and len(c["ws"]) == len(p):     # (tapering: roads.deck_width_fn)
                s0 = np.r_[0.0, np.cumsum(np.hypot(*np.diff(p[:, :2], axis=0).T))]
                hw = (np.interp(s - s[0], s0, np.asarray(c["ws"], np.float64)) / 2)[:, None]
            n = _normals(xy) * hw
            out[k] = (xy + n, xy - n, s / float(c.get("tl") or 10.0), c["m"], layer_of(c), z)
            LAYERS_OF_MAT.setdefault(c["m"], set()).add(out[k][4])
            continue
        batch.append(k)
        pts.append(p[:, :3])
        half.append(float(c["w"]) / 2)
    if batch:
        fast.require(155, "ribbon_edges")
        off = np.r_[0, np.cumsum([len(p) for p in pts])].astype(np.int64)
        L, R, S, Z, o = fast._ef.ribbon_edges(np.ascontiguousarray(np.concatenate(pts)), off,
                                             np.asarray(half, np.float64), float(DRAPE_STEP))
        for n, k in enumerate(batch):
            a, b = int(o[n]), int(o[n + 1])
            if b - a < 2:
                continue
            c = chunks[k]
            out[k] = (L[a:b], R[a:b], _ribbon_v(S[a:b], c), c["m"], layer_of(c),
                      Z[a:b] if c.get("fixed") else None)
            LAYERS_OF_MAT.setdefault(c["m"], set()).add(out[k][4])
    return out if keep_none else [r for r in out if r is not None]


def _ribbon_v(s, c):
    """v along a ribbon: s (m along the chunk) over its texture length. A dirt or gravel strip's chunk carries s0 / s1,
    m along its whole road to its ends (roads._surface_pieces): v is that distance, so the ruts run on across a tile's
    edge instead of restarting there (PR #455)."""
    tl = float(c.get("tl") or 10.0)
    s0, s1 = c.get("s0"), c.get("s1")
    if s0 is not None and s1 is not None and len(s) and float(s[-1] - s[0]) > 1e-9:
        return (float(s0) + (float(s1) - float(s0)) * ((s - s[0]) / float(s[-1] - s[0]))) / tl
    return s / tl


TRIM_DZ = (0.3, 4.5)                # m: another deck overlapping a deck in plan this far above or below it (two
                                    # surfaces, too near to pass over it: a ramp leaving its mainline) ...
TRIM_GAP_M = 0.05                   # ... each slab ends this short of halfway between their centre lines

# Short, connected bridge forks need a common two-dimensional surface. Three independently sloped, flat-width
# ribbons can start at one height and still overlap by tens of centimetres a few metres later. Keep the longitudinal
# solver's profiles; reconcile their shared footprint here, using one triangulated grid for skin and collision.
JUNCTION_MAX_PART_M = 60.0
JUNCTION_MAX_BRANCHES = 8
JUNCTION_CELL_M = 2.0
JUNCTION_JOIN_M = 0.75
JUNCTION_JOIN_Z_M = 0.75
# A junction of several joints a few metres apart (a lid's or a viaduct's: dual carriageways, turn lanes), each short part
# between two of them, is one field (earthfast junction_groups' clusters; GitHub #1083: Rautenweg over the S1 was 16
# overlapping slabs at their own heights and tilts); its ends going on alone are pinned to their approaches, centre and
# edges (JUNCTION_PIN_R_M: the correction's reach). BNE_JUNCTION_CLUSTER=off: per joint only, as before.
JUNCTION_CLUSTER_ON = os.environ.get("BNE_JUNCTION_CLUSTER", "on") != "off"
JUNCTION_CLUSTER_MAX_PARTS = 40
JUNCTION_CLUSTER_MAX_M = 120.0      # m: its parts' box at most this either way (every tile round it has all its parts)
JUNCTION_PIN_R_M = 8.0
CLUSTER_SKIN_TILE_M = 11.0              # m: a cluster's deck skins' texture square in the world (roads.DECK's "tl")
JUNCTION_CLUSTER_PIN_TOL_M = 0.05   # m: a pin may miss by this much (two arms' ends nearer than a lattice cell apart at
                                    # different heights: Rautenweg's west banks, 4 cm), never more


class _JunctionSurface:
    def __init__(self, entries, joint, pins=None):
        self.fids = frozenset(c['fid'] for c, _p in entries)
        self.joint = np.asarray(joint, np.float64)
        # A road-relative basis, rather than an anchor's terrain grid: kept geometry and a new anchor's skin must
        # choose the same triangulation. Entry order is canonical (fid), independent of tile/context ordering.
        first = entries[0][1]
        d = first[1, :2] - first[0, :2]
        u = d / np.linalg.norm(d)
        self.basis = np.array([u, [-u[1], u[0]]])
        self.lines = [(np.c_[(p[:, :2] - self.joint[:2]) @ self.basis.T, p[:, 2]], float(c['w']) / 2)
                      for c, p in entries]
        allxy = np.concatenate([p[:, :2] for p, _w in self.lines])
        margin = max(w for _p, w in self.lines) + 4.0
        self.i0, self.j0 = np.floor((allxy.min(0) - margin) / JUNCTION_CELL_M).astype(int)
        i1, j1 = np.ceil((allxy.max(0) + margin) / JUNCTION_CELL_M).astype(int)
        x, y = np.meshgrid(np.arange(self.i0, i1 + 1) * JUNCTION_CELL_M,
                           np.arange(self.j0, j1 + 1) * JUNCTION_CELL_M)
        self.pin_xy, self.pin_w = None, None
        nodes = np.c_[x.ravel(), y.ravel()]
        base = self._height(nodes)
        self.grid = np.ascontiguousarray(base.reshape(x.shape))
        if pins is not None and len(pins):
            import fast
            self.pin_xy = np.ascontiguousarray(self.local(np.asarray(pins, np.float64)[:, :2]))
            self.pin_w = np.zeros(len(pins))
            target = np.asarray(pins, np.float64)[:, 2]
            for _ in range(4):
                r = target - self.at(np.asarray(pins, np.float64)[:, :2])
                if np.max(np.abs(r)) < 1e-4:
                    break
                self.pin_w = self.pin_w + np.asarray(fast._ef.field_pins(self.pin_xy, np.ascontiguousarray(r),
                                                                         np.zeros((0, 2)), JUNCTION_PIN_R_M, 0))
                self.grid = np.ascontiguousarray((base + self._pin(nodes)).reshape(self.grid.shape))

    def _pin(self, xy):
        """The pins' correction at local xy (0 without pins)."""
        if self.pin_w is None:
            return np.zeros(len(xy))
        import fast
        return np.asarray(fast._ef.field_pins(self.pin_xy, np.ascontiguousarray(self.pin_w, np.float64),
                                              np.ascontiguousarray(xy, np.float64), JUNCTION_PIN_R_M, 1))

    def local(self, xy):
        return (np.asarray(xy, np.float64)[:, :2] - self.joint[:2]) @ self.basis.T

    def _grid_height(self, xy):
        return self._height(xy) + self._pin(xy)

    def _ensure_grid(self, local):
        """Extend the same lattice when a pad/miter goes beyond the initial profile bounds. Clamping the cell
        index while retaining an outside point's fractional coordinate would repeat boundary heights."""
        if not len(local):
            return
        low = np.floor(local.min(0) / JUNCTION_CELL_M).astype(int) - 1
        high = np.ceil(local.max(0) / JUNCTION_CELL_M).astype(int) + 1
        lo = np.minimum(low, [self.i0, self.j0])
        hi = np.maximum(high, [self.i0 + self.grid.shape[1] - 1, self.j0 + self.grid.shape[0] - 1])
        if (lo == [self.i0, self.j0]).all() and (hi == [self.i0 + self.grid.shape[1] - 1,
                                                       self.j0 + self.grid.shape[0] - 1]).all():
            return
        x, y = np.meshgrid(np.arange(lo[0], hi[0] + 1) * JUNCTION_CELL_M,
                           np.arange(lo[1], hi[1] + 1) * JUNCTION_CELL_M)
        self.grid = np.ascontiguousarray(self._grid_height(np.c_[x.ravel(), y.ravel()]).reshape(x.shape))
        self.i0, self.j0 = int(lo[0]), int(lo[1])

    def _height(self, xy):
        weights, heights, distances = [], [], []
        for p, half in self.lines:
            a, d = p[:-1, :2], np.diff(p[:, :2], axis=0)
            raw_t = ((xy[:, None] - a) * d).sum(2) / np.maximum((d * d).sum(1), 1e-12)
            t = np.clip(raw_t, 0, 1)
            dist2 = ((xy[:, None] - a - t[:, :, None] * d) ** 2).sum(2)
            j = np.argmin(dist2, axis=1)
            at = np.arange(len(xy))
            ds = dist2[at, j]
            # The approach immediately past an abutment continues its tangent, rather than becoming a flat cap.
            f = np.where(((j == 0) & (raw_t[at, j] < 0)) |
                         ((j == len(d) - 1) & (raw_t[at, j] > 1)), raw_t[at, j], t[at, j])
            heights.append(p[j, 2] + f * (p[j + 1, 2] - p[j, 2]))
            # Compact support: another branch cannot pull a free abutment up once its carriageway has parted.
            weights.append(np.maximum(0.0, 1.0 - ds / (half + PARAPET_W) ** 2) ** 2)
            distances.append(ds)
        w, h, d = np.array(weights), np.array(heights), np.array(distances)
        total = w.sum(0)
        # The corner pad can extend beyond all carriageways; continue their nearby surfaces there smoothly.
        fallback = 1.0 / np.maximum(d, 0.25) ** 2
        w = np.where((total > 1e-12)[None], w, fallback)
        return (w * h).sum(0) / w.sum(0)

    def at(self, xy):
        local = self.local(xy)
        self._ensure_grid(local)
        p = local / JUNCTION_CELL_M
        i, j = np.floor(p[:, 0]).astype(int), np.floor(p[:, 1]).astype(int)
        fx, fy = p[:, 0] - i, p[:, 1] - j
        li, lj = i - self.i0, j - self.j0
        a, b, c, d = self.grid[lj, li], self.grid[lj, li + 1], self.grid[lj + 1, li], self.grid[lj + 1, li + 1]
        return np.where(fy > fx, a + fy * (c - a) + fx * (d - c), a + fx * (b - a) + fy * (d - b))

    def top(self, p, uv, t, lift=0.0):
        import fast
        fast.require(52, 'follow_tris')
        q = np.ascontiguousarray(self.local(p))
        self._ensure_grid(q)
        # No terrain-step suppression on a bridge. All intersecting surfaces use these exact grid triangles.
        v, uv, tris, src = fast._ef.follow_tris(q, np.ascontiguousarray(uv, np.float64),
                                               np.ascontiguousarray(t, np.int64), JUNCTION_CELL_M,
                                               self.grid, int(self.i0), int(self.j0), 0.001, 1e9, 0.0)
        v = np.asarray(v)
        v[:, :2] = v[:, :2] @ self.basis + self.joint[:2]
        v[:, 2] += lift
        return v, np.asarray(uv), np.asarray(tris), np.asarray(src)

    def edge(self, xy):
        """Split a boundary at the same grid edges/diagonals as top(), including vertical collision skirts."""
        xy = np.asarray(xy, np.float64)
        local = self.local(xy) / JUNCTION_CELL_M
        indices, fractions = [], []
        for j, (a, b) in enumerate(zip(local[:-1], local[1:])):
            cuts = [0.0]
            for u, v in zip((a[0], a[1], a[0] - a[1]), (b[0], b[1], b[0] - b[1])):
                if abs(v - u) < 1e-10:
                    continue
                k = np.arange(math.floor(min(u, v)) + 1, math.ceil(max(u, v)))
                f = (k - u) / (v - u)
                cuts.extend(f[(f > 1e-9) & (f < 1 - 1e-9)])
            cuts = np.unique(cuts)
            indices.extend([j] * len(cuts)); fractions.extend(cuts)
        indices.append(len(xy) - 2); fractions.append(1.0)
        j, f = np.asarray(indices, np.int64), np.asarray(fractions)
        return xy[j] + f[:, None] * (xy[j + 1] - xy[j]), j, f


def _junction_surfaces(chunks, context=()):
    """By fid: complete, short 3+-branch junctions only. Crossing/stacked decks and incomplete clipped parts stay
    with the existing profile. Context supplies the same complete profiles to each neighbouring tile."""
    by_fid = {}
    for c in list(context) + list(chunks):
        if not c.get('deck') or not c.get('fixed') or c.get('tunnel') or not c.get('fid'):
            continue
        if c.get('jn') is None or not len(c['jn']):
            continue
        p = np.asarray(c['jn'], np.float64)
        if p.ndim != 2 or p.shape[1] != 3 or len(p) < 2 or not np.isfinite(p).all():
            continue
        if not (0 < float(c['w']) <= 60.0):
            continue
        p = p[np.r_[True, np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1) > 1e-6]]
        if len(p) < 2:
            continue
        length = float(np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1).sum())
        key = (length, tuple(p.ravel()))
        old = by_fid.get(c['fid'])
        if old is None or key > old[0]:
            by_fid[c['fid']] = (key, c, p)
    import fast
    fast.require(241, "junction_groups")
    kept = [(c, p) for _fid, (key, c, p) in sorted(by_fid.items()) if key[0] <= JUNCTION_MAX_PART_M]
    if not kept:
        return {}
    ends, plen, half, skip, bbox = [], [], [], [], []
    for c, p in kept:
        for q, r in ((p[0], p[1]), (p[-1], p[-2])):
            d = r[:2] - q[:2]
            d = d / max(float(np.hypot(*d)), 1e-9)
            ends.append([q[0], q[1], q[2], d[0], d[1]])
        plen.append(float(np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1).sum()))
        half.append(float(c['w']) / 2)
        skip.append(1 if c.get('jskip') else 0)
        bbox.append([*p[:, :2].min(0), *p[:, :2].max(0)])
    prm = np.array([JUNCTION_JOIN_M, JUNCTION_JOIN_Z_M, JUNCTION_MAX_PART_M, JUNCTION_MAX_BRANCHES,
                    1.0 if JUNCTION_CLUSTER_ON else 0.0, JUNCTION_CLUSTER_MAX_PARTS, JUNCTION_CLUSTER_MAX_M], np.float64)
    _grp, _field, fields, members, pins = fast._ef.junction_groups(
        np.ascontiguousarray(ends, np.float64), np.asarray(plen, np.float64), np.asarray(half, np.float64),
        np.asarray(skip, np.int64), np.ascontiguousarray(bbox, np.float64), prm)
    out = {}
    for f, (kind, jx, jy, jz) in enumerate(np.asarray(fields)):
        ent = [(kept[k][0], kept[k][1][::-1] if rev else kept[k][1]) for ff, k, rev in members if ff == f]
        pp = pins[pins[:, 0] == f][:, 1:]
        if kind == 0:
            field = _JunctionSurface(ent, np.array([jx, jy, jz]))
            # Each far endpoint is pinned to its ground approach. Nearby outgoing arms
            # can pull it up or down; the 2 m lattice can also move an off-grid grade knot.
            # Reject the whole shared field if any transition would gain a visible step.
            if np.max(np.abs(field.at(pp[:, :2]) - pp[:, 2])) > 0.01:
                continue
        else:
            # a cluster of joints (a junction over a lid): one field, its lone ends pinned to their approaches
            field = _JunctionSurface(ent, np.array([jx, jy, jz]), pins=pp)
            if len(pp) and np.max(np.abs(field.at(pp[:, :2]) - pp[:, 2])) > JUNCTION_CLUSTER_PIN_TOL_M:
                continue
            field.cluster = True
        out.update({c['fid']: field for c, _p in ent})
    return out


class _PadSurface(_JunctionSurface):
    """A pad follows its adjoining deck surface; away from it the existing approach heights stay. The overlap
    includes a grid-cell margin so the pad's triangles cannot interpolate an old approach height through a deck."""
    def __init__(self, ring, centre, ribbons_, junctions):
        self.ring = np.asarray(ring, np.float64)
        self.centre = np.asarray(centre, np.float64)
        self.ribbons = []
        for (left, right, _v, _m, _layer, z), c in ribbons_:
            if c.get('fid') not in junctions:
                continue
            mid = (left + right) / 2
            profile = np.asarray(c['jn'], np.float64)
            if not any(np.linalg.norm(profile[at, :2] - self.centre[:2]) <= JUNCTION_JOIN_M and
                       abs(profile[at, 2] - centre[2]) <= JUNCTION_JOIN_Z_M for at in (0, -1)):
                continue
            j = np.argmin(np.linalg.norm(mid - self.centre[:2], axis=1))
            if abs(z[j] - centre[2]) > JUNCTION_JOIN_Z_M:
                continue
            self.ribbons.append((left, right, z, junctions.get(c.get('fid'))))
        field = next((f for _l, _r, _z, f in self.ribbons if f is not None), None)
        self.joint = field.joint if field is not None else self.centre
        self.basis = field.basis if field is not None else np.eye(2)
        xy = self.local(self.ring)
        self.i0, self.j0 = np.floor((xy.min(0) - 2) / JUNCTION_CELL_M).astype(int)
        i1, j1 = np.ceil((xy.max(0) + 2) / JUNCTION_CELL_M).astype(int)
        x, y = np.meshgrid(np.arange(self.i0, i1 + 1) * JUNCTION_CELL_M,
                           np.arange(self.j0, j1 + 1) * JUNCTION_CELL_M)
        world = np.c_[x.ravel(), y.ravel()] @ self.basis + self.joint[:2]
        self.grid = np.ascontiguousarray(self._pad_height(world).reshape(x.shape))

    def _grid_height(self, xy):
        return self._pad_height(xy @ self.basis + self.joint[:2])

    def _pad_height(self, xy):
        ring, c = self.ring, self.centre
        # Extend the original centre/rim fan to the surrounding grid's boundary.
        original = np.full(len(xy), c[2] - PAD_DROP)
        closest = np.full(len(xy), np.inf)
        for a, b in zip(ring, np.roll(ring, -1, axis=0)):
            d, e = a[:2] - c[:2], b[:2] - c[:2]
            det = d[0] * e[1] - d[1] * e[0]
            if abs(det) < 1e-9:
                continue
            q = xy - c[:2]
            u, v = (q[:, 0] * e[1] - q[:, 1] * e[0]) / det, (d[0] * q[:, 1] - d[1] * q[:, 0]) / det
            outside = np.maximum(-u, 0) + np.maximum(-v, 0) + np.maximum(u + v - 1, 0)
            hit = outside < closest
            original[hit] = c[2] + u[hit] * (a[2] - c[2]) + v[hit] * (b[2] - c[2]) - PAD_DROP
            closest[hit] = outside[hit]
        distance, target = np.full(len(xy), np.inf), original.copy()
        for left, right, z, field in self.ribbons:
            mid = (left + right) / 2
            a, d = mid[:-1], np.diff(mid, axis=0)
            t = np.clip(((xy[:, None] - a) * d).sum(2) / np.maximum((d * d).sum(1), 1e-12), 0, 1)
            ds = np.linalg.norm(xy[:, None] - a - t[:, :, None] * d, axis=2)
            j = np.argmin(ds, axis=1)
            idx = np.arange(len(xy))
            f = t[idx, j]
            # Distance to the flat-ended slab, not a capsule around its centreline. Capsule end caps had pulled
            # the elevated surface several metres too far along a ground approach.
            gap = np.full(len(xy), np.inf)
            for la, ra, lb, rb in zip(left[:-1], right[:-1], left[1:], right[1:]):
                poly = np.array([la, ra, rb, lb])
                e = np.roll(poly, -1, axis=0) - poly
                delta = xy[:, None] - poly
                cross = e[:, 0] * delta[:, :, 1] - e[:, 1] * delta[:, :, 0]
                inside = (cross >= -1e-8).all(1) | (cross <= 1e-8).all(1)
                u = np.clip((delta * e).sum(2) / np.maximum((e * e).sum(1), 1e-12), 0, 1)
                edge_dist = np.linalg.norm(delta - u[:, :, None] * e, axis=2).min(1)
                gap = np.minimum(gap, np.where(inside, 0, edge_dist))
            hit = gap < distance
            hh = field.at(xy) if field is not None else z[j] + f * (z[j + 1] - z[j])
            target[hit], distance[hit] = hh[hit], gap[hit]
        weight = np.clip((2 * JUNCTION_CELL_M + 2.5 - distance) / 2.5, 0, 1)
        weight = weight * weight * (3 - 2 * weight)
        return weight * target + (1 - weight) * original


def _pad_without_ground(p, t, uv, material, origin, ground, centre):
    """Cut a filler pad out of its adjoining ground carriageways. Those already have terrain collision; a pad fan
    superimposed on a curved approach ended in a 63 cm lip in the captured Swiss junction. Clip sides/railings too,
    without adding artificial closing faces at the opening. Bridge slabs themselves are never removed here."""
    for c in ground:
        line = np.asarray(c.get('n', ()), np.float64)
        if (c.get('fixed') or c.get('deck') or c.get('tunnel') or c.get('h') is not None or
                c.get('drive') or layer_of(c) != 'base' or float(c.get('w', 0)) < GROUND_COVER_MIN_W or
                line.ndim != 2 or len(line) < 2 or line.shape[1] < 3):
            continue
        if not any(np.linalg.norm(line[at, :2] - centre[:2]) < 1.0 and
                   abs(line[at, 2] - centre[2]) < JUNCTION_JOIN_Z_M for at in (0, -1)):
            continue
        xy = line[:, :2] - np.asarray(origin)[:2]
        normals = _normals(xy) * (float(c['w']) / 2 + 0.05)
        left, right = xy + normals, xy - normals
        for la, ra, lb, rb in zip(left[:-1], right[:-1], left[1:], right[1:]):
            quad = np.array([la, ra, rb, lb])
            qlo, qhi = quad.min(0), quad.max(0)
            tri = p[t, :2]
            hit = (tri.max(1) >= qlo).all(1) & (tri.min(1) <= qhi).all(1)
            if not hit.any():
                continue
            # Partition a triangle into the portions outside each successive half-plane. The remainder after
            # all four edges is inside the road and discarded. Interpolation retains the original 3D surface/UV.
            edge = np.roll(quad, -1, axis=0) - quad
            sign = np.sign(np.sum(quad[:, 0] * np.roll(quad[:, 1], -1) -
                                  np.roll(quad[:, 0], -1) * quad[:, 1]))
            if not sign:
                continue
            pieces, labels = [], []
            for indices, mat in zip(t[hit], material[hit]):
                inside = np.c_[p[indices], uv[indices]]
                for a, d in zip(quad, edge):
                    if len(inside) < 3:
                        break
                    distance = sign * (d[0] * (inside[:, 1] - a[1]) - d[1] * (inside[:, 0] - a[0]))
                    if (distance >= -1e-9).all():
                        continue
                    if (distance <= 1e-9).all():
                        pieces.append(inside); labels.append(mat); inside = np.zeros((0, 5)); break
                    inn, out = [], []
                    for j in range(len(inside)):
                        k = (j + 1) % len(inside)
                        point, nxt, da, db = inside[j], inside[k], distance[j], distance[k]
                        (inn if da >= 0 else out).append(point)
                        if (da >= 0) != (db >= 0):
                            cut = point + (nxt - point) * (da / (da - db))
                            inn.append(cut); out.append(cut)
                    if len(out) >= 3:
                        pieces.append(np.array(out)); labels.append(mat)
                    inside = np.array(inn)
            tt, mm, pp, uu = [t[~hit]], [material[~hit]], [p], [uv]
            at = len(p)
            for poly, mat in zip(pieces, labels):
                pp.append(poly[:, :3]); uu.append(poly[:, 3:])
                fan = np.array([(at, at + i, at + i + 1) for i in range(1, len(poly) - 1)], np.int64)
                tt.append(fan); mm.append(np.full(len(fan), mat)); at += len(poly)
            p, t, uv, material = np.concatenate(pp), np.concatenate(tt), np.concatenate(uu), np.concatenate(mm)
    used, inverse = np.unique(t, return_inverse=True)
    return p[used], inverse.reshape(-1, 3), uv[used], material


def trim_overlaps(rb, context=(), shared=None):
    """The deck ribbons rb ([(ribbon, chunk)]: ribbons()' (left, right, v, m, layer, z) of "fixed" decks) with each
    edge pulled in where another deck (rb's or the context's, the decks around the tile: the same trim in every tile)
    overlaps it in plan at a height TRIM_DZ apart: both end halfway between their centre lines (a gore; a parapet on the
    higher one from PARAPET_COVER_DZ apart), each road on its own surface, instead of the higher one's side standing in
    the lower one's lanes, and a car on the lower one's line riding the higher one's slab until it ends: a
    US-101 exit ramp diverging downhill under its mainline's 18.8 m deck put that deck's edge 0.9 m from the ramp's
    centre, and an AI car drove into it at 29 mph (Los Angeles, 2026-10-27). Pure."""
    if not rb:
        return rb
    import fast
    fast.require(235, "deck_trim_overlaps")
    # (the cuts are native: earthfast deck_trim_overlaps, meshtrim.rs; each deck against every other deck near it was
    # a numpy round of a few rows, hundreds a tile)
    owners, groups, lefts, rights, zs, lens, own_in, targets = {}, {}, [], [], [], [], [], []
    oid, gid, sh = [], [], []
    for n, ((left, right, v, m, layer, z), c) in enumerate(list(rb) + list(context)):
        if z is None:
            continue
        if n < len(rb):
            targets.append(len(oid))
        owner = c.get("fid", ("n", n))
        oid.append(owners.setdefault(owner, len(owners)))
        if shared:
            g = shared.get(owner)
            gid.append(groups.setdefault(id(g), len(groups)))
            sh.append(owner in shared)
        else:
            gid.append(-1)
            sh.append(False)
        lefts.append(np.asarray(left, np.float64)[:, :2])
        rights.append(np.asarray(right, np.float64)[:, :2])
        zs.append(np.asarray(z, np.float64).reshape(-1))
        lens.append(len(lefts[-1]))
    if not targets:
        return list(rb)
    tl_all, tr_all = fast._ef.deck_trim_overlaps(
        np.ascontiguousarray(np.concatenate(lefts)), np.ascontiguousarray(np.concatenate(rights)),
        np.ascontiguousarray(np.concatenate(zs)), np.r_[0, np.cumsum(lens)].astype(np.int64),
        np.array(oid, np.int64), np.array(gid, np.int64), np.array(sh, bool), np.array(targets, np.int64),
        np.array([TRIM_DZ[0], TRIM_DZ[1], TRIM_GAP_M, 1.0 if shared else 0.0], np.float64))
    out = []
    at = 0
    for n, ((left, right, v, m, layer, z), c) in enumerate(rb):
        if z is None:
            out.append(((left, right, v, m, layer, z), c))
            continue
        k = len(left)
        tl, tr = tl_all[at:at + k], tr_all[at:at + k]
        at += k
        if not (tl.any() or tr.any()):
            out.append(((left, right, v, m, layer, z), c))
            continue
        hw = np.hypot(*(left - right).T) / 2
        u = (left - right) / np.maximum(2 * hw, 1e-9)[:, None]       # unit left
        tl = np.minimum(tl, 0.9 * hw)           # (never past the centre line: at least a tenth of the deck stays)
        tr = np.minimum(tr, 0.9 * hw)
        out.append(((left - u * tl[:, None], right + u * tr[:, None], v, m, layer, z), c))
    return out


def _clip_tris(P, T, UV, M, box):
    """The triangles (positions P (n, 3), T (m, 3), UV (n, 2), material per triangle M) cut to the box (x0, y0, x1, y1):
    those inside kept, those outside dropped, those across its edges clipped (Sutherland-Hodgman, position and uv
    interpolated) and fanned again. Pure."""
    x0, y0, x1, y1 = box
    xy = P[T][:, :, :2]
    inside = ((xy[..., 0] >= x0) & (xy[..., 0] <= x1) & (xy[..., 1] >= y0) & (xy[..., 1] <= y1)).all(1)
    out_ = ((xy[..., 0] < x0).all(1) | (xy[..., 0] > x1).all(1) | (xy[..., 1] < y0).all(1) | (xy[..., 1] > y1).all(1))
    cut = ~inside & ~out_
    if not cut.any():
        return P, T[inside], UV, M[inside]
    planes = ((0, x0, 1.0), (0, x1, -1.0), (1, y0, 1.0), (1, y1, -1.0))     # keep sign * (coord - value) >= 0
    newP, newUV, newT, newM = [], [], [], []
    n = len(P)
    for t, m in zip(T[cut], M[cut]):
        poly = [(P[i], UV[i]) for i in t]
        for ax, val, sg in planes:
            if not poly:
                break
            res = []
            for j in range(len(poly)):
                (pa, ua), (pb, ub) = poly[j], poly[(j + 1) % len(poly)]
                da, db = sg * (pa[ax] - val), sg * (pb[ax] - val)
                if da >= 0:
                    res.append((pa, ua))
                if (da >= 0) != (db >= 0):
                    f = da / (da - db)
                    res.append((pa + (pb - pa) * f, ua + (ub - ua) * f))
            poly = res
        if len(poly) < 3:
            continue
        base = n + sum(len(q) for q in newP)
        newP.append(np.array([q[0] for q in poly]))
        newUV.append(np.array([q[1] for q in poly]))
        k = len(poly)
        newT.append(np.array([(base, base + a, base + a + 1) for a in range(1, k - 1)], np.int64))
        newM.append(np.full(k - 2, m))
    if not newP:
        return P, T[inside], UV, M[inside]
    return (np.concatenate([P] + newP), np.concatenate([T[inside]] + newT), np.concatenate([UV] + newUV),
            np.concatenate([M[inside]] + newM))


def _reach_past(c, box):
    """Chunk c with each end outside the box (cut at roads.OVERLAP past the tile's edge) carried on straight by
    1.5 x its width: a ribbon's square end on a road crossing the seam at a slant left a triangle of the road
    uncovered on each side once both tiles were cut exactly at the edge (the E6 in Norway, 50 degrees). Pure."""
    pts = np.asarray(c["n"] if c.get("n") is not None else (), np.float64)
    if len(pts) < 2:
        return c
    x0, y0, x1, y1 = box
    out_ = lambda p: p[0] < x0 or p[0] > x1 or p[1] < y0 or p[1] > y1     # noqa: E731
    reach = 1.5 * float(c["w"])
    ends = []
    for at, nb in ((0, 1), (-1, -2)):
        if not out_(pts[at]):
            ends.append(None)
            continue
        t = pts[at, :2] - pts[nb, :2]
        L = float(np.hypot(*t))
        if L < 1e-6:
            ends.append(None)
            continue
        q = pts[at].copy()
        q[:2] += t / L * reach
        ends.append(q)
    if ends[0] is None and ends[1] is None:
        return c
    n = [pts]
    if ends[0] is not None:
        n.insert(0, ends[0][None])
    if ends[1] is not None:
        n.append(ends[1][None])
    out = {**c, "n": np.concatenate(n).tolist()}
    if c.get("s0") is not None and c.get("s1") is not None:    # (a strip's distance along its road: the same reach)
        out["s0"] = float(c["s0"]) - (reach if ends[0] is not None else 0.0)
        out["s1"] = float(c["s1"]) + (reach if ends[1] is not None else 0.0)
    return out


GRID_M = heightmap.SQUARE  # the terrain block's square (1 m; 2 m on the lighter ground): its samples on multiples of it, each square cut
                        # from its south-west corner to its north-east one (probed in-game 2026-10-20)


FOLLOW_TOL_MIN = 0.008   # m: road_tol's finest
FOLLOW_TOL = 0.02       # m a road triangle may depart from the drawn ground inside it before it is cut along the
                        # ground's own triangles (follow): flat graded roads stay whole, bends and junctions are cut.
                        # The ground's heights step by its quantum (1/32 m): at 5 mm nearly every triangle was cut (7-15x
                        # the triangles); at 2 cm 1.7x in Baltimore, 5.7x on the E6 in Norway, 7.5x in San Francisco


def _height_grid(frame, elev, xy, q=None, cell=GRID_M, tris=None):
    """(grid [j, i], i0, j0): the terrain block's samples (elev on the cell lattice, rounded to the height quantum q
    as heightmap.write_png does) over the points xy (n, 2) and a sample around. One elev call. tris (m, 3) into xy:
    only the samples round those triangles (their boxes and a sample more: all follow_ground reads), the rest nan,
    the same grid: a strip across a tile (a dirt road, roads.DIRT_LOOK) covers a few % of its box, whose every
    sample was most of the mesh's time (elev is pointwise, so the same heights)."""
    if tris is not None:
        tris = np.asarray(tris, np.int64)
        xy = xy[np.unique(tris)]
    i0 = int(np.floor(xy[:, 0].min() / cell)) - 1
    j0 = int(np.floor(xy[:, 1].min() / cell)) - 1
    i1 = int(np.ceil(xy[:, 0].max() / cell)) + 1
    j1 = int(np.ceil(xy[:, 1].max() / cell)) + 1
    nx, ny = i1 - i0 + 1, j1 - j0 + 1
    if tris is None or not len(tris):
        gx, gy = np.meshgrid(np.arange(i0, i1 + 1) * cell, np.arange(j0, j1 + 1) * cell)
        la, lo, _ = frame.enu_to_geodetic(gx.ravel(), gy.ravel(), 0.0)
        h = np.asarray(elev(la, lo), np.float64)
        u = np.asarray(frame.geodetic_to_enu(la, lo, h)[2], np.float64).reshape(gx.shape)
        if q:
            u = np.rint(u / q) * q
        return np.ascontiguousarray(u), i0, j0
    pt = xy[np.searchsorted(np.unique(tris), tris)]     # (m, 3, 2) the triangles' corners
    lo_i = np.clip(np.floor(pt[..., 0].min(1) / cell).astype(np.int64) - 1 - i0, 0, nx - 1)
    hi_i = np.clip(np.floor(pt[..., 0].max(1) / cell).astype(np.int64) + 1 - i0, 0, nx - 1)
    lo_j = np.clip(np.floor(pt[..., 1].min(1) / cell).astype(np.int64) - 1 - j0, 0, ny - 1)
    hi_j = np.clip(np.floor(pt[..., 1].max(1) / cell).astype(np.int64) + 1 - j0, 0, ny - 1)
    w = hi_i - lo_i + 1
    cnt = w * (hi_j - lo_j + 1)
    if int(cnt.sum()) > 8 * nx * ny:                    # (big triangles over most of the box: all of it)
        return _height_grid(frame, elev, xy, q, cell)
    k = np.arange(int(cnt.sum())) - np.repeat(np.cumsum(cnt) - cnt, cnt)
    rw = np.repeat(w, cnt)
    nodes = np.unique((np.repeat(lo_j, cnt) + k // rw) * nx + np.repeat(lo_i, cnt) + k % rw)
    if len(nodes) > 0.6 * nx * ny:                      # (most of the box anyway)
        return _height_grid(frame, elev, xy, q, cell)
    ex, ey = (nodes % nx + i0) * cell, (nodes // nx + j0) * cell
    la, lo, _ = frame.enu_to_geodetic(ex.astype(np.float64), ey.astype(np.float64), 0.0)
    h = np.asarray(elev(la, lo), np.float64)
    u = np.asarray(frame.geodetic_to_enu(la, lo, h)[2], np.float64)
    grid = np.full(ny * nx, np.nan)
    grid[nodes] = np.rint(u / q) * q if q else u
    return np.ascontiguousarray(grid.reshape(ny, nx)), i0, j0


def drawn_ground(frame, drawn, xy, q=None, cell=GRID_M):
    """The drawn ground (n,) at the points xy (n, 2) over one _height_grid of drawn (lat, lon) -> metres: the block's
    squares, each cut from its south-west corner to its north-east one, a point on the grid's last line in the square
    before it - the ground follow_ground (earthfast.follow_tris) puts the road surfaces on. The sidewalks' curbs on
    the ground the road surfaces lie on (the user, 2026-10-21: sidewalks on the drawn ground with upstream's road
    surfaces): elev and the block differ by its height quantum q alone, more between its samples on hills. One drawn
    call."""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    if not len(xy):
        return np.zeros(0)
    # (_height_grid's lattice, drawn only at the corners of the squares the points are in: sidewalks cover a fraction
    # of a tile's bounds, and drawn (the DEM, grading, arch raise) is pointwise, so the same heights)
    i0 = int(np.floor(xy[:, 0].min() / cell)) - 1
    j0 = int(np.floor(xy[:, 1].min() / cell)) - 1
    nx = int(np.ceil(xy[:, 0].max() / cell)) + 1 - i0 + 1
    ny = int(np.ceil(xy[:, 1].max() / cell)) + 1 - j0 + 1
    gx, gy = xy[:, 0] / cell, xy[:, 1] / cell
    i, j = np.floor(gx).astype(np.int64), np.floor(gy).astype(np.int64)
    i -= (i - i0 == nx - 1) & (gx == i)         # a point on the grid's last line belongs to the square before it
    j -= (j - j0 == ny - 1) & (gy == j)
    li, lj = i - i0, j - j0
    ok = (li >= 0) & (lj >= 0) & (li + 1 < nx) & (lj + 1 < ny)
    li, lj = np.clip(li, 0, nx - 2), np.clip(lj, 0, ny - 2)
    base = (lj * nx + li)[ok]
    nodes = np.unique(np.concatenate([base, base + 1, base + nx, base + nx + 1]))
    grid = np.full(ny * nx, np.nan)
    if len(nodes):
        ex, ny_ = (nodes % nx + i0) * cell, (nodes // nx + j0) * cell
        la, lo, _ = frame.enu_to_geodetic(ex.astype(np.float64), ny_.astype(np.float64), 0.0)
        h = np.asarray(drawn(la, lo), np.float64)
        u = np.asarray(frame.geodetic_to_enu(la, lo, h)[2], np.float64)
        grid[nodes] = np.rint(u / q) * q if q else u
    grid = grid.reshape(ny, nx)
    fx, fy = gx - i, gy - j
    a, b, c, d = grid[lj, li], grid[lj, li + 1], grid[lj + 1, li], grid[lj + 1, li + 1]
    z = np.where(fy > fx, a + fy * (c - a) + fx * (d - c), a + fx * (b - a) + fy * (d - b))
    return np.where(ok, z, np.nan)


FOLLOW_STEP_M = 0.45 * GRID_M  # a terrain cell whose corners are further apart holds a step (a cutting's wall, the lattice's
FOLLOW_MAX_UP = 0.1     # teeth): a draped surface goes at most this over its lowest corner there (the McGraw-Graben's
                        # edge lines climbed the teeth as white tents up to 1.3 m, 2026-10-22). A road would need a
                        # ~32 % grade along a cell's diagonal (GRID_M 1 m) to count as a step.


def road_tol(q):
    """The road's follow tolerance for the anchor's height quantum q: 2.5 quanta, FOLLOW_TOL_MIN .. FOLLOW_TOL (the user,
    2026-11-11: "they still float. i want to make them virtualy the same height of the road": a 2 cm tolerance let the
    paint hang up to 2 cm over a 4 mm-quantum ground; a coarse ground keeps 2 cm, its steps would cut every triangle)."""
    if not q:
        return FOLLOW_TOL
    return min(FOLLOW_TOL, max(FOLLOW_TOL_MIN, 2.5 * float(q)))


def follow_ground(P, UV, T, grid, i0, j0, tol=None, cell=GRID_M, step_m=None):
    """(points (m, 3), uvs, triangles, source triangle) of the triangles T over P (n, 2) on the drawn ground (grid:
    _height_grid), held down in the cells holding a step (FOLLOW_STEP_M, or step_m): earthfast.follow_tris."""
    import fast
    tol = FOLLOW_TOL if tol is None else tol
    P = np.ascontiguousarray(np.asarray(P, np.float64)[:, :2])
    UV = np.ascontiguousarray(UV, np.float64)
    T = np.ascontiguousarray(T, np.int64)
    fast.require(52, "follow_tris")
    a, b, c, d = fast._ef.follow_tris(P, UV, T, float(cell), np.ascontiguousarray(grid, np.float64), int(i0), int(j0),
                                      float(tol), FOLLOW_STEP_M if step_m is None else float(step_m), FOLLOW_MAX_UP)
    return np.asarray(a), np.asarray(b), np.asarray(c), np.asarray(d)


def _shared(P, UV, T):
    """The mesh with its equal points (position and uv to 0.1 mm) made one."""
    import fast
    fast.require(235, "shared_points")
    key = np.round(np.c_[P, UV] * 1e4).astype(np.int64)
    first, inv = fast._ef.shared_points(np.ascontiguousarray(key))       # (np.unique(axis=0)'s, natively)
    return P[first], UV[first], inv[T]


def _frontage_mask(polys, box):
    """Rasterize the union so the apron feathers only at its exposed edges."""
    from PIL import Image, ImageFilter
    import fast

    texel = FRONTAGE_TEXEL_M
    bounds = np.asarray(box).reshape(2, 2)
    lo = np.floor((bounds[0] - 1.5) / texel) * texel
    hi = bounds[1] + 1.5
    cols, rows = np.ceil((hi - lo) / texel).astype(int)
    rings = []
    for poly in polys:
        p = (np.asarray(poly)[:, :2] - lo) / texel
        if np.all(p.max(axis=0) > 0) and np.all(p.min(axis=0) < [cols, rows]):
            rings.append(tuple(np.ascontiguousarray(v) for v in p.T))
    fast.require(140, "pad coverage")
    cover = fast._ef.pad_cover(int(rows), int(cols), rings)
    if cover is None:
        return None
    mask = Image.fromarray(np.asarray(cover, np.uint8) * 63).filter(ImageFilter.GaussianBlur(1.15))
    return lo, np.asarray(mask, dtype=np.float32) / 252.


def _frontage_weight(xy, mask):
    if mask is None:
        return np.ones(len(xy))
    lo, val = mask
    texel = FRONTAGE_TEXEL_M
    p = (xy - lo) / texel - .5
    i, j = np.floor(p).astype(int).T
    h, w = val.shape
    inside = (i >= 0) & (j >= 0) & (i < w - 1) & (j < h - 1)
    i, j = i.clip(0, w - 2), j.clip(0, h - 2)
    x, y = (p - np.c_[i, j]).T
    a = (1 - x) * val[j, i] + x * val[j, i + 1]
    b = (1 - x) * val[j + 1, i] + x * val[j + 1, i + 1]
    return np.where(inside, np.clip(((1 - y) * a + y * b - .50) / .40, 0., 1.), 1.)


def _frontage_mesh(poly, fan, mask):
    """Insert an inner row only at a pavement boundary. Keep the broad interior light."""
    from buildings import _area2

    step = np.roll(poly, -1, axis=0) - poly
    distance = np.hypot(*step.T)
    good = distance > 1e-6
    if not good.all():
        return poly, fan
    samples = [np.arange(n) / n for n in np.maximum(1, np.ceil(distance / 1.4).astype(int))]
    full_edge = np.concatenate([a + t[:, None] * d for a, d, t in zip(poly, step, samples)])
    weight = _frontage_weight(full_edge, mask)
    if np.all(weight >= .99):
        return poly, fan
    # Pull both ends of each edge inwards far enough to put a vertex behind the fade.
    centre = poly.mean(axis=0)
    normal = np.c_[-step[:, 1], step[:, 0]] * (1 if _area2(poly) > 0 else -1) / distance[:, None]
    clearance = np.sum((centre - poly) * normal, axis=1)
    share = np.minimum(.8, .45 / np.maximum(clearance, .02))
    inner = poly + (centre - poly) * np.maximum(share, np.roll(share, 1))[:, None]
    ends = np.r_[0, np.cumsum([len(t) for t in samples])]
    outer, inset = [], []
    for i, t in enumerate(samples):
        j = (i + 1) % len(poly)
        if np.all(weight[ends[i]:ends[i + 1]] >= .99) and weight[ends[j]] >= .99:
            t = t[:1]
        outer.extend(poly[i] + t[:, None] * step[i])
        inset.extend(inner[i] + t[:, None] * (inner[j] - inner[i]))
    k = len(outer)
    i = np.arange(k)
    j = (i + 1) % k
    tri = np.concatenate((np.c_[i, j, j + k], np.c_[i, j + k, i + k], np.c_[i + k, j + k, np.full(k, 2 * k)]))
    return np.vstack((outer, inset, centre)), tri


def _refine_frontage(P, T, UV, M, RI, material, mask, repeat):
    """Refine the fade only after draping, interpolating on the existing terrain triangles."""
    indexes = np.flatnonzero(M == material)
    xy = P[T[indexes], :2]
    side = np.roll(xy, -1, axis=1) - xy
    span = np.hypot(side[:, :, 0], side[:, :, 1]).max(axis=1)
    weights = _frontage_weight(xy.reshape(-1, 2), mask).reshape(-1, 3)
    large = span > 1.6
    mids = xy[large] + .5 * side[large]
    on_edge = _frontage_weight(mids.reshape(-1, 2), mask).reshape(-1, 3).min(axis=1) < .995
    large[large] = on_edge | (weights[large].min(axis=1) < .995)
    # Tiny triangles already follow the border. A broad flat polygon with
    # boundary-only vertices needs an inner row to keep its centre solid.
    thin = (span > .65) & (weights.max(axis=1) <= .02)
    candidates = indexes[large | thin]
    if not len(candidates):
        return P, T, UV, M, RI
    keep = np.ones(len(T), bool)
    xyz, tris, uvs = [P], [], [UV]
    n = len(P)
    fan = np.array([[0, 1, 2]])
    for j in candidates:
        pts = P[T[j]]
        edge = pts[1:] - pts[0]
        determinant = edge[0, 0] * edge[1, 1] - edge[0, 1] * edge[1, 0]
        if abs(determinant) < 1e-7:
            continue
        vertices, tri = _frontage_mesh(pts[:, :2], fan, mask)
        if len(vertices) == 3:
            continue
        gradient = np.linalg.solve(edge[:, :2], edge[:, 2])
        xyz.append(np.c_[vertices, pts[0, 2] + (vertices - pts[0, :2]) @ gradient])
        uvs.append(vertices / repeat)
        tris.append(tri + n)
        n += len(vertices)
        keep[j] = False
    if not tris:
        return P, T, UV, M, RI
    added = np.concatenate(tris)
    return (np.concatenate(xyz), np.concatenate((T[keep], added)), np.concatenate(uvs),
            np.r_[M[keep], np.full(len(added), material)], np.r_[RI[keep], np.full(len(added), -1)])
def _marks_on_decks(P, T, M, mats, fixed, cell=2.0):
    """The markings on decks (triangles on fixed heights in a material only ever drawn in the "mark" layer) put on the
    deck surface drawn under them (fixed base triangles: the deck asphalt, retriangulated over a junction by
    _junction_surfaces), its height there + LIFT["mark"]; a marking triangle with a point off the deck surface (a line past a deck's end) left out (the user, 2026-11-07: markings follow the surface they are painted on
    and stop at a deck's end; a lane line on its own heights rode up to 16 cm over the asphalt at Pittsburgh's deck
    ends, 40.442792,-79.992235). (P, T, M, the triangles kept or None)."""
    if not len(T) or not fixed.any():
        return P, T, M, None
    mark = np.array([LAYERS_OF_MAT.get(m) == {"mark"} for m in mats], bool)[np.asarray(M, np.int64)]
    ot = mark & fixed
    bt = ~mark & ~np.array([overlay_material(m) for m in mats], bool)[np.asarray(M, np.int64)] & fixed
    if not ot.any() or not bt.any():
        return P, T, M, None
    B = T[bt]
    A, Bv, C = P[B[:, 0]], P[B[:, 1]], P[B[:, 2]]
    lo = np.floor(np.minimum(np.minimum(A, Bv), C)[:, :2] / cell).astype(np.int64)
    hi = np.floor(np.maximum(np.maximum(A, Bv), C)[:, :2] / cell).astype(np.int64)
    grid = {}
    for t in range(len(B)):
        for gx in range(lo[t, 0], hi[t, 0] + 1):
            for gy in range(lo[t, 1], hi[t, 1] + 1):
                grid.setdefault((gx, gy), []).append(t)
    P = P.copy()
    hit = np.zeros(len(P), bool)
    for v in np.unique(T[ot].ravel()).tolist():
        x, y = P[v, 0], P[v, 1]
        cand = grid.get((int(np.floor(x / cell)), int(np.floor(y / cell))))
        if not cand:
            continue
        c = np.asarray(cand)
        a, b, cc = A[c], Bv[c], C[c]
        d = (b[:, 1] - cc[:, 1]) * (a[:, 0] - cc[:, 0]) + (cc[:, 0] - b[:, 0]) * (a[:, 1] - cc[:, 1])
        ok = np.abs(d) > 1e-12
        dd = np.where(ok, d, 1.0)
        l1 = ((b[:, 1] - cc[:, 1]) * (x - cc[:, 0]) + (cc[:, 0] - b[:, 0]) * (y - cc[:, 1])) / dd
        l2 = ((cc[:, 1] - a[:, 1]) * (x - cc[:, 0]) + (a[:, 0] - cc[:, 0]) * (y - cc[:, 1])) / dd
        l3 = 1.0 - l1 - l2
        inside = ok & (l1 >= -1e-6) & (l2 >= -1e-6) & (l3 >= -1e-6)
        if not inside.any():
            continue
        z = (l1 * a[:, 2] + l2 * b[:, 2] + l3 * cc[:, 2])[inside]
        P[v, 2] = z[np.argmin(np.abs(z - P[v, 2]))] + LIFT["mark"]     # (the deck nearest its own height)
        hit[v] = True
    keep = ~ot | hit[T].all(1)      # (a triangle reaching past the deck's edge or end left out whole)
    return P, T[keep], np.asarray(M)[keep], keep


OVERLAY_DECK_END_M = 0.05  # m an overlay on a deck may reach past the end of the deck surface it is painted on


def _overlays_on_decks(rbc):
    """The overlays (markings, layer != "base") on a deck follow the deck surface they are painted on and stop at
    its end (the user, 2026-11-07: a lane line's own fixed heights, knotted elsewhere than its deck's, rode up to
    4 cm over the asphalt and on past the deck's end 0.3 m over the ground, Pittsburgh 40.443,-79.9928): each of
    their cross sections takes the height of the nearest point of its fid's base deck ribbons' centre line, and the
    sections whose nearest point is past an end of that line are left out (the ribbon cut to its longest run on the
    deck). Ribbons on the ground and decks' own surfaces as they were."""
    base = {}
    for r, c in rbc:
        if r[5] is not None and r[4] == "base" and c.get("deck"):
            mid = (np.asarray(r[0]) + np.asarray(r[1])) * 0.5
            hw = np.hypot(*(np.asarray(r[0]) - np.asarray(r[1])).T) * 0.5
            base.setdefault(c.get("fid"), []).append((np.c_[mid, np.asarray(r[5], np.float64)], hw))
    if not base:
        return rbc
    out = []
    for r, c in rbc:
        left, right, v, m, layer, z = r
        # (its own road's deck; a line of another way painted over a deck: any deck under it)
        lines = base.get(c.get("fid")) or [x for v_ in base.values() for x in v_]
        if z is None or layer == "base" or not lines or len(left) < 2:
            out.append((r, c))
            continue
        mid = (np.asarray(left) + np.asarray(right)) * 0.5
        bz = np.full(len(mid), np.nan)
        bd = np.full(len(mid), np.inf)
        own = c.get("fid") in base
        for ln, hw in lines:
            if len(ln) < 2:
                continue
            a, b = ln[:-1, :2], ln[1:, :2]
            ab = b - a
            L2 = np.maximum((ab * ab).sum(1), 1e-12)
            t = ((mid[:, None, :] - a[None]) * ab[None]).sum(2) / L2[None]
            tc = np.clip(t, 0.0, 1.0)
            q = a[None] + tc[..., None] * ab[None]
            d = np.hypot(*(mid[:, None, :] - q).transpose(2, 0, 1))
            j = np.argmin(d, 1)
            k = np.arange(len(mid))
            # past an end: beyond the line's first or last segment by more than OVERLAY_DECK_END_M
            seg_len = np.sqrt(L2[j])
            past = ((j == 0) & (t[k, j] * seg_len < -OVERLAY_DECK_END_M)) |                    ((j == len(ab) - 1) & ((t[k, j] - 1.0) * seg_len > OVERLAY_DECK_END_M))
            zz = ln[j, 2] + tc[k, j] * (ln[j + 1, 2] - ln[j, 2])
            wide = np.maximum(hw[j], hw[np.minimum(j + 1, len(hw) - 1)]) + OVERLAY_DECK_END_M
            better = (d[k, j] < bd) & ~past & (d[k, j] <= wide)      # (on the deck surface as drawn: within its half width)
            bz[better], bd[better] = zz[better], d[k, j][better]
        on = np.isfinite(bz)
        if on.all():
            out.append(((left, right, v, m, layer, bz), c))
            continue
        # the longest run of sections on the deck
        best, run, s0 = (0, 0), 0, 0
        for i, f in enumerate(np.r_[on, False]):
            if f:
                if run == 0:
                    s0 = i
                run += 1
            else:
                if run > best[1] - best[0]:
                    best = (s0, s0 + run)
                run = 0
        a_, b_ = best
        if b_ - a_ >= 2:
            out.append(((left[a_:b_], right[a_:b_], v[a_:b_], m, layer, bz[a_:b_]), c))
    return out


@profile_step('Road mesh generation')
def build(frame, chunks, elev, origin, lift=0.0, clip=None, follow=False, q=None, drawn=None, context=(),
          clip_poly=None):
    """The road mesh of a tile (render only; on bridges it lies DECK_SKIN over deck()'s slab): (positions
    relative to origin, tris, uvs, materials, tri material index), or None; with a dirt or gravel strip in it (a
    FADE_PREFIX material) a sixth element, the fade uv (n, 2) (_fade_uv; write, plazas.merge), and those materials
    first. A chunk with "slen" (roads._surface_pieces) is drawn in four rows, its sides feathered.
    elev(lat, lon) -> m (graded,
    as the physics block). One elev call. lift: m more for the draped surfaces (detail mode: the physics
    block is drawn, so the asphalt must sit over it, not in it; earthd DETAIL_ROAD_LIFT). clip: the tile's box
    (x0, y0, x1, y1): the ribbons (built roads.OVERLAP past it, no gap at the seam) cut exactly at its edges; two
    tiles' meshes overlapping there at one height flickered in a 4 m band along every tile seam (the user,
    2026-10-20: z-fighting on the road texture). follow: the draped surfaces given the terrain block's own heights
    and cut along its triangles where it bends under them (follow_ground; q its height quantum), so they lie `lift`
    over the drawn ground (within FOLLOW_TOL; drawn(lat, lon): the block's heights where they are not elev's, its
    ground raised over low arches): a ribbon straight across the road stood under the ground's bumps (up to 30 cm on hills), so it was drawn
    5 cm over it and the tyres sank into it (the user, 2026-10-20). clip_poly (k, 2): a convex polygon the mesh is cut
    to instead of the box (a geotile's quad in the anchor's world: earthfast.clip_tris_convex, the neighbours' cuts
    along the very same lines); clip then only says where the ribbons reach past the tile (_reach_past)."""
    chunks = [c for c in chunks if not c.get("tube_only")]  # (twin tunnels' tube: no road surface, roads._tunnel_chunks)
    if clip is not None:
        chunks = [_reach_past(c, clip) for c in chunks]
    junctions = _junction_surfaces(chunks, context)
    rbc = ribbon_pairs(chunks)                  # deck surfaces too: their asphalt over deck()'s concrete
    if any(r[5] is not None for r, _c in rbc):  # (trimmed as deck() trims their slabs: context, the decks around)
        ctx = [(r, c) for r, c in ribbon_pairs(list(context)) if r[5] is not None]
        rbc = list(trim_overlaps(rbc, ctx, junctions))
    if shells.ON:           # (the overlays on decks follow the deck surface and stop at its end: PR #987)
        rbc = _overlays_on_decks(rbc)
    # (each deck end with its layer and width: a ground ribbon's end takes the edges of a deck end of its own layer and
    # a like width only; the nearest of any kind gave a ground centre line (0.12 m) the deck asphalt's 7.1 m edges at
    # a Pittsburgh bridge's end, GitHub #739)
    deck_abuts = []
    for r, _c in rbc + (ribbon_pairs(list(context)) if context else []):
        if r[5] is not None:
            l_d, r_d = r[0], r[1]
            if len(l_d) >= 2:
                for e in (0, -1):
                    deck_abuts.append(((l_d[e] + r_d[e]) * 0.5, l_d[e].copy(), r_d[e].copy(), r[4],
                                       float(np.hypot(*(l_d[e] - r_d[e])))))

    ab_pts = np.array([a[0] for a in deck_abuts], np.float64) if deck_abuts else np.zeros((0, 2), np.float64)
    ab_layer = [a[3] for a in deck_abuts]
    ab_w = np.array([a[4] for a in deck_abuts], np.float64) if deck_abuts else np.zeros(0)

    def _abut(d, layer, w):
        """The deck end a ground ribbon's end (distances d to every deck end) takes: of its layer, its width within half
        to double of the ground's, within 0.75 m; None."""
        ok = (d <= 0.75) & np.array([ly == layer for ly in ab_layer]) & (ab_w <= 2.0 * max(w, 0.05)) & (ab_w >= 0.5 * w)
        if not ok.any():
            return None
        return int(np.where(ok, d, np.inf).argmin())
    if len(deck_abuts):
        new_rbc = []
        for r, c in rbc:
            if r[5] is None and len(r[0]) >= 2:
                left, right, v, m, layer, fz = r
                left, right = left.copy(), right.copy()
                mid0 = (left[0] + right[0]) * 0.5
                mid1 = (left[-1] + right[-1]) * 0.5
                d0 = np.hypot(ab_pts[:, 0] - mid0[0], ab_pts[:, 1] - mid0[1])
                d1 = np.hypot(ab_pts[:, 0] - mid1[0], ab_pts[:, 1] - mid1[1])
                hit0 = _abut(d0, layer, float(np.hypot(*(left[0] - right[0]))))
                hit1 = _abut(d1, layer, float(np.hypot(*(left[-1] - right[-1]))))
                if hit0 is not None:
                    _, l_ab, r_ab = deck_abuts[hit0][:3]
                    if (np.hypot(*(left[0] - l_ab)) + np.hypot(*(right[0] - r_ab))) <= (np.hypot(*(left[0] - r_ab)) + np.hypot(*(right[0] - l_ab))):
                        left[0], right[0] = l_ab, r_ab
                    else:
                        left[0], right[0] = r_ab, l_ab
                if hit1 is not None:
                    _, l_ab, r_ab = deck_abuts[hit1][:3]
                    if (np.hypot(*(left[-1] - l_ab)) + np.hypot(*(right[-1] - r_ab))) <= (np.hypot(*(left[-1] - r_ab)) + np.hypot(*(right[-1] - l_ab))):
                        left[-1], right[-1] = l_ab, r_ab
                    else:
                        left[-1], right[-1] = r_ab, l_ab
                new_rbc.append(((left, right, v, m, layer, fz), c))
            else:
                new_rbc.append((r, c))
        rbc = new_rbc
    rb = [r for r, _c in rbc]
    polys = [c for c in chunks if c.get("poly")]
    if not rb and not polys:
        return None
    draped = [r for r in rb if r[5] is None]
    pv, fans = [], []
    for c in polys:
        p = np.asarray(c["poly"], np.float64)[:, :2]
        if c.get("quads"):                      # (many quads, 4 corners each: roadwear's spots, one chunk a material)
            fan = (np.array([[0, 1, 2], [0, 2, 3]], np.int64)[None]
                   + 4 * np.arange(len(p) // 4, dtype=np.int64)[:, None, None]).reshape(-1, 3)
        else:
            fan = np.array([(0, j, j + 1) for j in range(1, len(p) - 1)], dtype=np.int64).reshape(-1, 3)
        pv.append(p)
        fans.append(fan)
    if draped or pv:
        edges = np.concatenate([np.concatenate([l, r]) for l, r, _, _, _, _ in draped] + pv)
        la, lo, _ = frame.enu_to_geodetic(edges[:, 0], edges[:, 1], 0.0)
        h = np.asarray(elev(la, lo), np.float64)
        _, _, u = frame.geodetic_to_enu(la, lo, h)
    mats = []
    P, UV, T, M = [], [], [], []
    TL = []                                     # per triangle: its layer's lift over the ground (None: on a deck)
    RI = []                                     # per triangle: its ribbon (-1: a filled polygon), for the fade uv
    RW = []                                     # per triangle: a raceway's (no step cap: follow_ground, below)
    rpar = []                                   # per ribbon: _fade_uv's (feather share, tl, tu, slen, open ends)
    k0 = nverts = 0
    for ri, ((left, right, v, m, layer, fz), c) in enumerate(rbc):
        k = len(left)
        if fz is None:
            cur_lift = np.full(k, lift)
            if len(deck_abuts) and k >= 2:
                mid = (left + right) * 0.5
                s_from_start = np.r_[0.0, np.cumsum(np.hypot(*np.diff(mid, axis=0).T))]
                d0 = np.hypot(ab_pts[:, 0] - mid[0, 0], ab_pts[:, 1] - mid[0, 1]).min()
                d1 = np.hypot(ab_pts[:, 0] - mid[-1, 0], ab_pts[:, 1] - mid[-1, 1]).min()
                if d0 <= 0.75:
                    w0 = np.clip(s_from_start / 4.0, 0.0, 1.0)
                    cur_lift = np.minimum(cur_lift, DECK_SKIN + (lift - DECK_SKIN) * w0)
                if d1 <= 0.75:
                    s_from_end = s_from_start[-1] - s_from_start
                    w1 = np.clip(s_from_end / 4.0, 0.0, 1.0)
                    cur_lift = np.minimum(cur_lift, DECK_SKIN + (lift - DECK_SKIN) * w1)
            lift_2k = np.concatenate((cur_lift, cur_lift))
            z = u[k0:k0 + 2 * k] + LIFT[layer] + lift_2k
            k0 += 2 * k
        else:                                   # on a deck: over its opaque concrete top
            z = np.concatenate((fz, fz)) + LIFT[layer] + DECK_SKIN
            # (a cluster's decks overlap all over its junction at one height: their skins, and the wear (patches, cracks:
            # roadwear) of one over another's skin, flickered as noise, Rautenweg in game 2026-11-12. Its plain asphalt
            # skins take the world's texture coordinates (below: where two lie on each other, one texture), its wear is
            # left out; the markings stay. Left to the deck tops instead, the slabs' tops and the pad shaded apart)
            jf = junctions.get(c.get('fid'))
            if jf is not None and getattr(jf, 'cluster', False) and not c.get('deck') and layer != "mark":
                continue
        if m not in mats:
            mats.append(m)
        mi = mats.index(m)
        tu = float(c.get("tu") or 1.0)          # ("tu": the texture's repeats across, else once)
        fu = 0.0                                # a strip's feather: this share of its width each side (EDGE_FADE_M)
        slen = c.get("slen")
        if slen and fz is None and c.get("feather", 1):      # ("feather": 0, a wear strip whose texture is soft)
            w = float(c.get("w") or 0.0)
            feather = min(EDGE_FADE_M, EDGE_FADE_SHARE * w)
            if feather > 0.05 and w > 2.0 * feather:
                fu = feather / w
        if fu:                                  # four rows: the edges, and the solid middle's edges between them
            zl, zr = z[:k], z[k:]
            xy = np.concatenate((left, left + (right - left) * fu, right + (left - right) * fu, right))[:, :2]
            P.append(np.c_[xy, np.concatenate((zl, (1.0 - fu) * zl + fu * zr, fu * zl + (1.0 - fu) * zr, zr))])
            UV.append(np.column_stack((np.repeat(np.array([0.0, fu, 1.0 - fu, 1.0]) * tu, k), np.tile(v, 4))))
            tri = np.concatenate([_quads(nverts, k, row, row + 1) for row in range(3)])
            nrows = 4
        else:
            xy = np.concatenate([left, right])[:, :2]
            P.append(np.c_[xy, z])
            UV.append(np.column_stack((np.concatenate((np.zeros(k), np.full(k, tu))), np.concatenate((v, v)))))
            tri = _quads(nverts, k, 0, 1)
            nrows = 2
        # where a ribbon folds over itself (a mitred joint at a sharp merge pulls its edges across: the user saw
        # black triangles in President St, 2026-10-14) its triangles turn over: dropped, the neighbours cover it
        tri = _keep_up(tri, xy, nverts)
        field = junctions.get(c.get('fid')) if fz is not None else None
        if field is not None:
            pp, uu, tt, _src = field.top(P[-1], UV[-1], tri - nverts, LIFT[layer] + DECK_SKIN)
            if getattr(field, 'cluster', False) and c.get('deck'):
                uu = pp[:, :2] / CLUSTER_SKIN_TILE_M
            P[-1], UV[-1], tri = pp, uu, tt + nverts
        T.append(tri)
        M.append(np.full(len(tri), mi))
        TL.append(np.full(len(tri), np.nan if fz is not None else LIFT[layer] + lift))
        RI.append(np.full(len(tri), ri))
        RW.append(np.full(len(tri), str(c.get("fid", "")).startswith("osm-raceway-")))
        ends = c.get("fends") or (1, 1)
        rpar.append((fu, float(c.get("tl") or 10.0), tu, float(slen) if slen else np.nan,
                     float(bool(ends[0])), float(bool(ends[1]))))
        nverts += len(P[-1])
    # Match the asphalt tile scale of the visible terrain or of satmat's mesh layer.
    asphalt_repeat = FRONTAGE_TERRAIN_REPEAT_M if lift > 0 else FRONTAGE_REPEAT_M
    for c, pq, fan in zip(polys, pv, fans):     # filled polygons (corner asphalt): a fan from vertex 0
        k = len(pq)
        z = u[k0:k0 + k] + LIFT[layer_of(c)] + lift
        k0 += k
        if c["m"] not in mats:
            mats.append(c["m"])
        P.append(np.c_[pq[:, :2], z])
        if c["m"] == FRONTAGE_ASPHALT:
            UV.append(pq / asphalt_repeat)
        else:
            UV.append(np.asarray(c["uvq"], np.float64) if c.get("uvq") is not None else   # markings.arrow_quad
                      np.c_[pq[:, 0] / float(c.get("u_m") or 8.0), pq[:, 1] / float(c.get("tl") or 10.0)])
        fan = fan + nverts
        pts = np.c_[pq[:, :2], z]
        nrm = np.cross(pts[fan[:, 1] - nverts] - pts[fan[:, 0] - nverts], pts[fan[:, 2] - nverts] - pts[fan[:, 0] - nverts])
        flip = nrm[:, 2] < 0
        fan[flip] = fan[flip][:, ::-1]
        T.append(fan)
        M.append(np.full(len(fan), mats.index(c["m"])))
        TL.append(np.full(len(fan), LIFT[layer_of(c)] + lift))
        RI.append(np.full(len(fan), -1))
        RW.append(np.zeros(len(fan), bool))
        nverts += k
    P, T, UV, M = np.concatenate(P), np.concatenate(T), np.concatenate(UV), np.concatenate(M)
    TL, RI, RW = np.concatenate(TL), np.concatenate(RI), np.concatenate(RW)
    tid = np.arange(len(T))
    if clip_poly is not None:
        import fast
        fast.require(104, "clip_tris_convex")
        P, UV, T, src = fast._ef.clip_tris_convex(np.ascontiguousarray(P[:, :3], np.float64),
                                                  np.ascontiguousarray(UV, np.float64),
                                                  np.ascontiguousarray(T, np.int64),
                                                  np.ascontiguousarray(np.asarray(clip_poly, np.float64)[:, :2]))
        tid = tid[src]
        if not len(T):
            return None
    elif clip is not None:
        P, T, UV, tid = _clip_tris(P, T, UV, tid, clip)
        if not len(T):
            return None
    M, TL, RI, RW = M[tid], TL[tid], RI[tid], RW[tid]
    FX = ~np.isfinite(TL)                       # per triangle: on its own fixed heights (a deck's), not draped
    if follow:
        dr = np.isfinite(TL)
        if dr.any():
            grid, i0, j0 = _height_grid(frame, drawn or elev, P[:, :2], q, tris=T[dr])
            # Keep paint on the road and the paved frontage over the road's gravel shoulder.
            # Tighten only the frontage where the road can use its usual tolerance.
            front = TL == LIFT["frontage"] + lift
            if "line_bne_fr_white" in mats:
                asphalt = M == mats.index(FRONTAGE_ASPHALT) if FRONTAGE_ASPHALT in mats else np.zeros(len(M), bool)
                batches = [(dr & ~asphalt, .002), (asphalt, .004)]
            else:
                batches = [(dr & ~front, road_tol(q)), (front, .004)]
            wear = np.isin(TL, [LIFT[k] + lift for k in WEAR_STRIPS]) & dr
            if wear.any():                          # (the wear strips: their own looser batch, WEAR_TOL)
                batches = [(m & ~wear, t) for m, t in batches] + [(wear, WEAR_TOL)]
            points, uvs, tris, materials, ribbons = [P], [UV], [T[~dr]], [M[~dr]], [RI[~dr]]
            n0 = len(P)
            # (a raceway's surface: no step cap. Its banking (Daytona's 31 deg, 0.85 m along a 1 m square's diagonal)
            # passes FOLLOW_STEP_M in every square, and the cap sank the asphalt to each square's lowest corner +
            # FOLLOW_MAX_UP: a sawtooth across the banking with the ground showing through; from PR #992 by
            # jonathonbarton)
            if RW.any():
                batches = [(m & ~RW, t, None) for m, t in batches] + [(m & RW, t, float("inf")) for m, t in batches]
            else:
                batches = [(m, t, None) for m, t in batches]
            for mask, tol, step in batches:
                if not mask.any():
                    continue
                fP, fUV, fT, src = follow_ground(P[:, :2], UV, T[mask], grid, i0, j0, tol=tol, step_m=step)
                lifts = TL[mask][src]
                vl = np.zeros(len(fP))              # (a cut piece's points are its fan's: one lift each, set once)
                for kk in range(3):
                    vl[fT[:, kk]] = lifts
                fP[:, 2] += vl
                points.append(fP)
                uvs.append(fUV)
                tris.append(fT + n0)
                materials.append(M[mask][src])
                ribbons.append(RI[mask][src])
                n0 += len(fP)
            P, UV, T, M, RI = (np.concatenate(parts) for parts in (points, uvs, tris, materials, ribbons))
            FX = np.r_[FX[~dr], np.zeros(len(T) - int((~dr).sum()), bool)]     # (the draped ones follow the fixed)
            if not len(T):
                return None
    if shells.ON:           # the markings on decks put on the deck surface and stopped at its end (PR #987)
        P, T, M, keep = _marks_on_decks(P, T, M, mats, FX)
        RI = RI[keep] if keep is not None else RI
        if not len(T):
            return None
    asphalt = FRONTAGE_ASPHALT in mats and np.any(M == mats.index(FRONTAGE_ASPHALT))
    dirt = any(m.startswith(FADE_PREFIX) for m in mats)
    if asphalt:
        mi = mats.index(FRONTAGE_ASPHALT)
        bounds = clip
        if bounds is None:
            xy = P[T[M == mi], :2].reshape(-1, 2)
            bounds = np.r_[xy.min(axis=0), xy.max(axis=0)]
        frontage_mask = _frontage_mask((c["poly"] for c in polys if c.get("frontage")), bounds)
        P, T, UV, M, RI = _refine_frontage(P, T, UV, M, RI, mi, frontage_mask, asphalt_repeat)
    if follow or asphalt:
        P, UV, T = _shared(P, UV, T)
    out =(P - np.asarray(origin, np.float64), T, UV, mats, M)
    if not asphalt and not dirt:
        return out
    # a strip in the mesh: the fade uv for every point (a sixth element: write, plazas.merge), and the strips' materials
    # first, so the markings and anything else translucent in the mesh draw over them (one primitive per material, in
    # the list's order)
    vr = np.full(len(P), -1, np.int64)
    vr[T.ravel()] = np.repeat(RI, 3)
    fade = _fade_uv(UV, vr, rpar)
    if asphalt:
        used = np.unique(T[M == mats.index(FRONTAGE_ASPHALT)])
        weight = _frontage_weight(P[used, :2], frontage_mask)
        blend = weight ** 2 * (3 - 2 * weight)
        out[0][used, 2] -= (LIFT["frontage"] + lift - .001) * (1 - blend)
        fade[used] = np.c_[.001 + .499 * weight, np.full(len(used), .5)]
    order = sorted(range(len(mats)), key=lambda j: (not (mats[j].startswith(FADE_PREFIX)
                                                        or mats[j] == FRONTAGE_ASPHALT), j))
    remap = np.empty(len(mats), np.int64)
    remap[order] = np.arange(len(mats))
    return out[0], T, UV, [mats[j] for j in order], remap[np.asarray(M, np.int64)], fade


def _quads(base, k, row_a, row_b):
    """The triangles of one band of a ribbon whose rows of k points start at base + row * k: row_a to its right row_b,
    left to right then along ((A, B, B1) counter-clockwise seen from above)."""
    i = np.arange(k - 1)
    A, A1 = base + row_a * k + i, base + row_a * k + i + 1
    B, B1 = base + row_b * k + i, base + row_b * k + i + 1
    return np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)])


def _keep_up(tri, xy, base):
    """tri with the triangles facing the ribbon's way kept (a mitre folding over itself turns some over)."""
    import fast
    fast.require(235, "keep_up_mask")
    return tri[fast._ef.keep_up_mask(np.ascontiguousarray(tri, np.int64).reshape(-1, 3),
                                     np.ascontiguousarray(xy, np.float64), int(base))]


def _fade_uv(UV, vr, par):
    """The fade uv (n, 2) of a mesh's points (the opacity map's: FADE_PREFIX) from their colour uv: vr (n,) each
    point's ribbon (-1: none), par per ribbon (feather share fu, texture length tl, repeats across tu, slen (nan: not a
    strip), start open, end open). Across: u / tu within fu of an edge ramps 0 -> 0.5 (1 -> 0.5), else 0.5: the strips'
    rows lie on those lines, so a point the clip or the ground cut put inside a band gets what the GPU interpolates
    there. Along: v x tl is m along the road (ribbons' _ribbon_v): 0 at an open start ramping to FADE_V_BAND
    END_FADE_M in, 1 at an open end, 0.5 between. Vectorized (PR #455 carried a colour per point through the cuts).
    Pure."""
    C = np.full((len(UV), 2), 0.5)
    has = vr >= 0
    if not has.any() or not len(par):
        return C
    fu, tl, tu, slen, o0, o1 = np.asarray(par, np.float64)[vr[has]].T
    u = UV[has, 0] / tu
    f = np.maximum(fu, 1e-9)
    cu = np.where(u < fu, 0.5 * u / f, np.where(u > 1.0 - fu, 1.0 - 0.5 * (1.0 - u) / f, 0.5))
    C[has, 0] = np.where(fu > 0.0, np.clip(cu, 0.0, 1.0), 0.5)
    s = UV[has, 1] * tl
    strip = np.isfinite(slen)
    sl = np.where(strip, slen, 0.0)
    fade = np.minimum(END_FADE_M, END_FADE_SHARE * sl)
    strip &= fade > 1e-3
    fd = np.maximum(fade, 1e-9)
    cv = np.full(len(s), 0.5)
    at = strip & (o0 > 0.0) & (s <= fade)
    cv[at] = FADE_V_BAND * np.clip(s[at] / fd[at], 0.0, 1.0)
    at = strip & (o1 > 0.0) & (s >= sl - fade)
    cv[at] = (1.0 - FADE_V_BAND) + FADE_V_BAND * np.clip((s[at] - (sl[at] - fade[at])) / fd[at], 0.0, 1.0)
    C[has, 1] = cv
    return C


DECK_DEPTH = 1.2        # m of deck slab under the surface
DECK_SKIN = 0.01        # m the road mesh lies over a deck's top (the decal materials are translucent:
                        # 2026-10-11 a deck topped with road_asphalt_2lane was see-through)
DECK_TOP = "bne_bridge_asphalt"     # the slab's top: plain opaque asphalt (the stock terrain's), under the road's own
                                    # (concrete for a while, 2026-10-31: where trim_overlaps cut the road mesh between
                                    # two decks it showed as pale triangles in the lanes, Old Keene Mill Road)
DECK_TOP_TILE_M = 4.0               # m of asphalt a texture tile covers across ...
DECK_TOP_V = 10.0 / DECK_TOP_TILE_M  # ... and along (v: the road's, 10 m a tile)
DECK_SIDE = "bne_bridge_concrete"  # the slab, girders, parapets, piers and tunnel tubes: plain concrete (the stock
                        # concrete_plain textures; West Coast USA's "sidewalk1" had drain covers in it, round marks
                        # along every parapet next to Mapillary's Queensboro Bridge, 2026-10-17)
# A bridge as the official maps build theirs (Italy's road bridges on tapered wall piers, East Coast's): girders under
# the slab, a parapet along each free edge, piers (roads.pier_sites); ours was a bare floating slab (the user,
# 2026-10-17: "Bridges need a lot of work in general")
GIRDER_D = 1.0          # m of girders under the slab
GIRDER_W = 0.6          # m wide each
GIRDER_EVERY = 3.0      # m between girders across the deck
PARAPET_W = 0.45        # m: the slab reaches this far past the road's edge, a parapet on it ...
PARAPET_H = 0.84        # ... this high over the road (a jersey barrier's: JERSEY_H)
PARAPET_COVER_DZ = 2.0  # m: another deck's surface within this height at an edge point: no parapet there
BRIDGE_RAILS = True     # stock rail modules on decks with mapped support; unsupported decks keep full parapets
BRIDGE_CURB_H = 0.3     # m curb under generated guardrails, only on decks supported by mapped data
PARAPET_MIN_M = 8.0     # m: a parapet run shorter than this is left out (stubs stood in the Queensboro Bridge's lanes
                        # where its two directions' decks meet and the cover test missed a few points, 2026-10-17)
MEDIAN_JOIN_M = 6.0     # m: two decks' edges closer than this (a divided road's carriageways on one bridge) face a
                        # median: no parapets there, each slab widened to meet the other halfway (two parapets and a
                        # dark gap stood in a divided road's median over a canal, Port St. Lucie, 2026-10-23)
FOOTING_M = 2.0         # m a pier reaches into the ground
PIER_T = 1.8            # m: a wall pier's thickness along the deck (at its top; FOOTING tapers wider)
PIER_TAPER = 0.012      # its width grows this much per m down (each side)
PIER_CAP_H = 1.2        # m of pier cap under the girders, as wide as the deck
PIER_BENT_W = 16.0      # a deck wider than this stands on columns (a bent) rather than one wall
PIER_COLUMN = 1.6       # m square columns of a bent


# The parapet (2026-10-27): a closed New Jersey barrier, not three open faces (its ends and underside were missing: the
# far faces were culled where it ended, the top read as a lip floating over the water, the Sunshine Skyway's north
# end, the user's screenshots). A precast barrier's dimensions (the user's drawing): 610 mm base, 840 mm high; a 130 mm
# vertical toe, a lower slope to 380 mm (125 mm in), a steep upper face to the top (105 mm in), a 150 mm top. On a deck
# edge the half section: the outer face vertical, the road face the jersey's. (m in from the slab's outer edge, m up).
JERSEY_H = 0.84
JERSEY_TOE = 0.13
JERSEY_BREAK = 0.38
JERSEY_TOP_W = 0.15
JERSEY_UPPER_IN = 0.105
JERSEY_LOWER_IN = 0.125
JERSEY_HALF_W = JERSEY_TOP_W + JERSEY_UPPER_IN + JERSEY_LOWER_IN       # 0.38 m
JERSEY = ((0.0, 0.0), (0.0, JERSEY_H), (JERSEY_TOP_W, JERSEY_H), (JERSEY_TOP_W + JERSEY_UPPER_IN, JERSEY_BREAK),
          (JERSEY_HALF_W, JERSEY_TOE), (JERSEY_HALF_W, 0.0))
# Its ends (a precast sloped end section, Jensen Precast's; the user's photos): over END_M the top falls from full
# height to END_H, the section keeping its shape, and ends in a square low nose. At an abutment and wherever a run of
# parapet stops (a gap, a ramp's gore); not at a tile seam, where it goes on in the next tile.
END_M = 4.0
END_H = 0.18
PARAPET_TAPER_M, PARAPET_END_H = END_M, END_H       # (names of pass 2)


def jersey_section(h):
    """The half jersey section [(m in, m up)] of a barrier h high: full (JERSEY) at JERSEY_H; lower, the toe kept and
    the faces above it squeezed into what is left, so the shape narrows into a low plinth at an end. Pure."""
    toe = min(JERSEY_TOE, h)
    k = (h - toe) / (JERSEY_H - JERSEY_TOE)
    return [(x, y if y <= JERSEY_TOE else toe + (y - JERSEY_TOE) * k) for x, y in JERSEY]


def parapet_heights(line, ends):
    """Per point of a parapet's line (k, 2): its height, JERSEY_H, falling to END_H within END_M of an end point
    (abutments, the ends of a run where it stops: [(x, y)]; none: full height everywhere). Pure."""
    h = np.full(len(line), JERSEY_H)
    if ends:
        a = np.asarray(ends, np.float64)
        d = np.hypot(line[:, None, 0] - a[None, :, 0], line[:, None, 1] - a[None, :, 1]).min(1)
        h = END_H + (JERSEY_H - END_H) * np.clip(d / END_M, 0.0, 1.0)
    return h


UV_M = 0.25             # uv per m on a barrier (the concrete's repeat: 4 m)


def barrier_solid(edge, inward, z, h, v=None, section=JERSEY):
    """A closed barrier along an outer edge (k, 2): its section (offset toward `inward` (k, 2) m, share of the height
    h (k,)) swept from the base heights z (k,), both ends capped: a watertight solid whose every face looks out (no
    culled face shows from any side). (positions (k n, 3), triangles, uv). Pure."""
    edge, inward = np.asarray(edge, np.float64), np.asarray(inward, np.float64)
    z, h = np.asarray(z, np.float64), np.asarray(h, np.float64)
    k, n = len(edge), len(section)
    inward = inward / np.maximum(np.hypot(inward[:, 0], inward[:, 1]), 1e-9)[:, None]
    if section is JERSEY:                           # (m up per station: the section narrowing toward an end)
        secs = np.array([jersey_section(float(hh)) for hh in h], np.float64)        # (k, n, 2)
        off2, up2 = secs[:, :, 0], secs[:, :, 1]
    else:                                           # (a share of h)
        off2 = np.tile([q[0] for q in section], (k, 1)).astype(np.float64)
        up2 = h[:, None] * np.array([q[1] for q in section], np.float64)[None, :]
    up = up2[0] / max(float(h[0]), 1e-9)
    xy = edge[:, None, :] + inward[:, None, :] * off2[:, :, None]
    zz = z[:, None] + up2
    R = np.concatenate([xy, zz[..., None]], axis=2)                # (k, n, 3): the section at each station
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(edge, axis=0).T))] if v is None else np.asarray(v, np.float64)
    seg = np.linalg.norm(np.diff(np.concatenate([R, R[:, :1]], axis=1), axis=1), axis=2)    # (k, n) edge lengths
    per = np.concatenate([np.zeros((k, 1)), np.cumsum(seg, axis=1)], axis=1)               # (k, n + 1)
    # each face its own vertices (flat, not one normal smoothed over the barrier's corners: its top read as a soft
    # pillow) and world-scale uv: the sides along the barrier by the section's perimeter, the ends planar (their
    # uv was one column stretched across the face)
    Ps, UVs, Ts, outs, nv_ = [], [], [], [], 0
    i = np.arange(k - 1)
    for e in range(n):
        f = (e + 1) % n
        Ps.append(np.concatenate([R[:, e], R[:, f]]))
        UVs.append(np.c_[np.r_[s, s] * UV_M, np.r_[per[:, e], per[:, e + 1]] * UV_M])
        A, A1, B, B1 = nv_ + i, nv_ + i + 1, nv_ + k + i, nv_ + k + i + 1
        Ts.append(np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)]))
        mid = (R[:-1, e] + R[1:, e] + R[:-1, f] + R[1:, f]) / 4
        cen = (R[:-1].mean(1) + R[1:].mean(1)) / 2
        outs.append(np.tile(mid - cen, (2, 1)))
        nv_ += 2 * k
    for st, away in ((0, (edge[0] - edge[1]) if k > 1 else np.zeros(2)),
                     (k - 1, (edge[-1] - edge[-2]) if k > 1 else np.zeros(2))):
        Ps.append(R[st])
        UVs.append(np.c_[off2[st] * UV_M, up2[st] * UV_M])
        fan = np.arange(1, n - 1)
        Ts.append(np.stack([np.zeros(n - 2, np.int64), fan, fan + 1], 1) + nv_)
        outs.append(np.tile(np.r_[away, 0.0], (n - 2, 1)))
        nv_ += n
    P = np.concatenate(Ps)
    UV = np.concatenate(UVs)
    T = np.concatenate(Ts).astype(np.int64)
    out = np.concatenate(outs)
    nrm = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
    flip = (nrm * out).sum(1) < 0
    T[flip] = T[flip][:, ::-1]
    return P, T, UV


def _slabs_from_tops(P, T, UV, M, tops, side):
    """The decks' slabs as closed solids from their tops as drawn (the user, 2026-11-07: watertight): per slab depth,
    the tops of its pieces taken together (their shared end rows weld: no wall inside a deck) and closed by
    shells.close: sides straight down the depth from every edge of the outline, the underside under them, in the
    side material. Sides from each piece's own edge rows left 13 cm slivers where a junction field retriangulated
    the top (Pittsburgh 40.44225,-79.99391). P, T, UV, M with the tops replaced (T absolute indices)."""
    import shells
    off = np.r_[0, np.cumsum([len(a) for a in P])]
    drop = set()
    add = []
    for depth, idx in sorted(tops.items()):
        pp = np.concatenate([P[i] for i in idx])
        uu = np.concatenate([UV[i] for i in idx])
        base = np.r_[0, np.cumsum([len(P[i]) for i in idx])]
        tt = np.concatenate([T[i] - off[i] + base[j] for j, i in enumerate(idx)])
        mt = np.concatenate([M[i] for i in idx])
        P2, T2, M2, (UV2,) = shells.close(pp, tt, np.full(len(tt), side), (uu,), sink=depth)
        M2 = np.asarray(M2).copy()
        M2[:len(tt)] = mt
        add.append((P2, T2, UV2, M2))
        drop.update(idx)
    keep = [i for i in range(len(P)) if i not in drop]
    P_, T_, UV_, M_, nv = [], [], [], [], 0
    for i in keep:
        P_.append(P[i]), UV_.append(UV[i]), M_.append(M[i]), T_.append(T[i] - off[i] + nv)
        nv += len(P[i])
    for P2, T2, UV2, M2 in add:
        P_.append(P2), UV_.append(UV2), M_.append(M2), T_.append(T2 + nv)
        nv += len(P2)
    return P_, T_, UV_, M_


def deck(chunks, origin, piers=(), context=(), beams=(), ground=(), parapets=None, parapet_h=PARAPET_H,
         low_parapet_decks=(), pads=()):
    """The bridge decks of a tile as one colliding mesh: per "deck" chunk a slab of opaque concrete
    (DECK_SIDE) whose top is at the chunk's fixed heights (the road surface, drawn DECK_SKIN above it
    by build()), with sides and underside DECK_DEPTH below; the piers, and the truss beams (p, q, width, height:
    bridgetypes.truss_members) in painted steel (TRUSS_STEEL). (positions, tris, uvs, materials, tri
    material index) or None. ground: the tile's ground carriageways (roads.build_tile cover_out) for the parapet
    test, drawn or not (with the roads painted into the ground the chunks have none). Pure."""
    dch = [c for c in chunks if c.get("deck")]
    junctions = _junction_surfaces(chunks, context)
    cr_all = [(r, c) for r, c in ribbon_pairs(list(context)) if r[5] is not None]
    rb = trim_overlaps([(r, c) for r, c in ribbon_pairs(dch) if r[5] is not None], cr_all, junctions)
    if not rb and not piers and not beams and not pads:
        return None
    low_parapet_decks = set(low_parapet_decks or ())
    if parapets is None:
        parapets = parapet_runs(chunks, context, _rb=rb, ground=ground, pads=pads)
    parapets_by_deck = {}
    for parapet in parapets:
        parapets_by_deck.setdefault(parapet["deck"], []).append(parapet)
    mats, P, UV, T, M = [], [], [], [], []
    nv = 0
    active_field = None

    def quads(a, b, ua, ub, va, vb, mi, up):
        """A strip between point rows a and b (k, 3); `up`: the side its faces look to (3,) or (k, 3)."""
        nonlocal nv
        if active_field is not None and mi != top and np.array_equal(a[:, :2], b[:, :2]):
            # A straight top edge between old profile samples can stick through the new top's triangles.
            # Cut vertical sides at every grid boundary and preserve their thickness below the common surface.
            xy, j, f = active_field.edge(a[:, :2] + origin[:2])
            old = active_field.at(a[:, :2] + origin[:2])
            delta = active_field.at(xy) - (old[j] + f * (old[j + 1] - old[j]))
            a = a[j] + f[:, None] * (a[j + 1] - a[j]); a[:, 2] += delta
            b = b[j] + f[:, None] * (b[j + 1] - b[j]); b[:, 2] += delta
            va = va[j] + f * (va[j + 1] - va[j])
            vb = vb[j] + f * (vb[j + 1] - vb[j])
            if np.ndim(up) == 2:
                up = up[j] + f[:, None] * (up[j + 1] - up[j])
        k = len(a)
        P.append(np.concatenate([a, b]))
        UV.append(np.column_stack((np.concatenate((np.full(k, ua), np.full(k, ub))), np.concatenate((va, vb)))))
        i = np.arange(k - 1)
        A, A1, B, B1 = nv + i, nv + i + 1, nv + k + i, nv + k + i + 1
        tri = np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)])
        pts = np.concatenate(P)
        nrm = np.cross(pts[tri[:, 1]] - pts[tri[:, 0]], pts[tri[:, 2]] - pts[tri[:, 0]])
        want = np.broadcast_to(np.concatenate([up[:-1], up[:-1]]) if np.ndim(up) == 2 else up, nrm.shape)
        flip = (nrm * want).sum(1) < 0
        tri[flip] = tri[flip][:, ::-1]
        if active_field is not None and mi == top:
            pp, uu, tt, _src = active_field.top(P[-1] + origin, UV[-1], tri - nv)
            if getattr(active_field, 'cluster', False):     # (as the pads': world texture coordinates, build()'s note)
                uu = pp[:, :2] / DECK_TOP_TILE_M
            P[-1], UV[-1], tri = pp - origin, uu, tt + nv
        T.append(tri)
        M.append(np.full(len(tri), mi))
        nv += len(P[-1])

    if DECK_SIDE not in mats:
        mats.append(DECK_SIDE)
    side = mats.index(DECK_SIDE)                # opaque concrete; the asphalt is the road mesh's
    if DECK_TOP not in mats:
        mats.append(DECK_TOP)
    top = mats.index(DECK_TOP)
    cr = [r for r in ribbons(list(context)) if r[5] is not None]
    cov_ctx = [(r, c) for r, c in zip(cr, context)] + _ground_carriageways(list(chunks) + list(ground))
    covered = _parapet_cover([(r, c) for r, c in rb], cov_ctx)
    medians = _median_gaps([(r, c) for r, c in rb],
                           [(r, c) for r, c in zip(ribbons(list(context)), context) if r[5] is not None])
    tops = {}               # slab depth -> the indices of its pieces' tops in P (_slabs_from_tops)
    for n, ((left, right, v, m, layer, z), c) in enumerate(rb):
        active_field = junctions.get(c.get('fid'))
        k = len(z)
        lat = left - right
        u = lat / np.maximum(np.hypot(lat[:, 0], lat[:, 1]), 1e-9)[:, None]     # unit left
        wide = 0.0 if c.get("tunnel") else PARAPET_W
        gl_, gr_, ol_, or_, vl_, vr_ = medians[n]
        (xl, cl), (xr, cr_) = _opposite(gl_, ol_, *_noses(gl_), vl_), _opposite(gr_, or_, *_noses(gr_), vr_)
        covered[n] = (covered[n][0] | cl, covered[n][1] | cr_)
        # the slab's edges, past the road's (to the middle of a median beside another deck; to a point at a gore nose),
        # changing no faster than REACH_SLOPE along it
        el = left + u * _smooth_reach(_reach(wide, xl), left)[:, None]
        er = right - u * _smooth_reach(_reach(wide, xr), right)[:, None]
        zl = active_field.at(el) if active_field is not None else z
        zr = active_field.at(er) if active_field is not None else z
        L = np.c_[el, zl] - origin
        R = np.c_[er, zr] - origin
        # (a chunk's "dd": its bridge's slab and girders, roads.deck_depth of its span, split 1.2 : 1.0 as the full deck)
        dd = float(c.get("dd") or (DECK_DEPTH + GIRDER_D))
        slab = DECK_DEPTH * dd / (DECK_DEPTH + GIRDER_D)
        gird = dd - slab
        Lb, Rb = L - [0, 0, slab], R - [0, 0, slab]
        out_l = np.c_[u, np.zeros(k)]
        # (the top plain asphalt, under the road's own surface: where that does not cover it, between decks side by
        # side, a median, a gore, the slab showed as pale concrete wedges, US-101 over the Los Angeles River)
        across = float(np.median(np.hypot(*(el - er).T))) / DECK_TOP_TILE_M
        i0, nv0 = len(P), nv
        quads(L, R, 0.0, across, v * DECK_TOP_V, v * DECK_TOP_V, top if not c.get("tunnel") else side,
              np.array([0.0, 0.0, 1.0]))
        # the slab a closed solid from its top's own outline (the user, 2026-11-07: watertight): sides straight down
        # `slab` from every edge of the top as drawn (a junction field retriangulates the top: sides built from the
        # chunk's edge rows left 13 cm slivers between them, Pittsburgh 40.44225,-79.99391), the underside under them
        if shells.ON:
            tops.setdefault(round(slab, 3), []).append(i0)
        else:               # (BNE_WATERTIGHT off: the slab's sides and underside from the chunk's edge rows, as before)
            quads(L, Lb, 0.0, 0.1, v, v, side, out_l)
            quads(R, Rb, 0.0, 0.1, v, v, side, -out_l)
            quads(Lb, Rb, 0.0, 1.0, v, v, side, np.array([0.0, 0.0, -1.0]))
        if c.get("tunnel"):                     # a tunnel's floor: the slab alone
            continue
        # girders under the slab
        half = float(np.median(np.hypot(lat[:, 0], lat[:, 1]))) / 2
        ng = max(2, int(round(2 * half / GIRDER_EVERY)))
        for off in np.linspace(-(half - 0.6), half - 0.6, ng):
            mid = (left + right) / 2 + u * off
            gl, gr = mid + u * GIRDER_W / 2, mid - u * GIRDER_W / 2
            glz = active_field.at(gl) if active_field is not None else z
            grz = active_field.at(gr) if active_field is not None else z
            GL, GR = np.c_[gl, glz - slab] - origin, np.c_[gr, grz - slab] - origin
            GLb, GRb = GL - [0, 0, gird], GR - [0, 0, gird]
            quads(GL, GLb, 0.0, 0.1, v, v, side, out_l)
            quads(GR, GRb, 0.0, 0.1, v, v, side, -out_l)
            quads(GLb, GRb, 0.0, 0.05, v, v, side, np.array([0.0, 0.0, -1.0]))
        # a parapet on each free edge (parapet_runs: not where another deck goes on beside it, a ramp leaving, a
        # carriageway); a low curb under the rail where mapped guard rails put stock rail modules on it (guardrails)
        for parapet in parapets_by_deck.get(n, ()):
            h = parapet_heights(parapet["mid"], parapet["ends"])
            h = np.minimum(h, BRIDGE_CURB_H if BRIDGE_RAILS and n in low_parapet_decks and not parapet.get("nose")
                           else parapet_h)
            pz = active_field.at(parapet['edge']) if active_field is not None else parapet['z']
            bp, bt_, buv = barrier_solid(parapet["edge"], -parapet["out"], pz, h, parapet["v"])
            P.append(bp - origin)
            UV.append(buv)
            T.append(bt_ + nv)
            M.append(np.full(len(bt_), side))
            nv += len(bp)
    if tops:                # (the tops of a deck's pieces closed together: their shared end rows weld, no wall between)
        P, T, UV, M = _slabs_from_tops(P, T, UV, M, tops, side)
        nv = sum(len(a) for a in P)
    for p in _piers_clear(piers, rb):
        _pier(p, origin, P, T, UV, M, side)
        nv = sum(len(a) for a in P)
    for pad in pads:                # (junction pads on bridges: roads.deck_pads; their rim's railings)
        start_p, start_t, start_uv, start_m, start_nv = len(P), len(T), len(UV), len(M), nv
        ring, centre = pad[0], pad[1]
        joints = np.asarray(pad[3], np.float64) if len(pad) > 3 else np.asarray([centre], np.float64)
        incident = [(r, c) for r, c in rb if c.get('fid') in junctions and any(
            np.linalg.norm(np.asarray(c['jn'])[at, :2] - q[:2]) <= JUNCTION_JOIN_M and
            abs(c['jn'][at][2] - q[2]) <= JUNCTION_JOIN_Z_M for at in (0, -1) for q in joints)]
        field = _PadSurface(ring, centre, incident, junctions) if incident else None
        cl = {id(junctions[c['fid']]) for _r, c in incident}
        if incident and len(cl) == 1 and getattr(junctions[incident[0][1]['fid']], 'cluster', False):
            field = junctions[incident[0][1]['fid']]
        _pad(ring, centre, origin, P, T, UV, M, top, side, field)
        nv = sum(len(a) for a in P)
        for pts, out in (pad[2] if len(pad) > 2 else ()):
            pts = np.asarray(pts, np.float64)
            out = np.asarray(out, np.float64)
            edge = pts[:, :2] + out * (JERSEY_HALF_W + 0.02)
            ends = [tuple(edge[0]), tuple(edge[-1])]
            h = parapet_heights(edge, ends)
            pz = field.at(edge) if field is not None else pts[:, 2] - PAD_DROP
            bp, bt_, buv = barrier_solid(edge, -out, pz, h)
            P.append(bp - origin)
            UV.append(buv)
            T.append(bt_ + nv)
            M.append(np.full(len(bt_), side))
            nv += len(bp)
        if field is None:
            continue
        pp, tt, uu, mm = _pad_without_ground(np.concatenate(P[start_p:]), np.concatenate(T[start_t:]) - start_nv,
                                             np.concatenate(UV[start_uv:]), np.concatenate(M[start_m:]),
                                             origin, ground, centre)
        P[start_p:], T[start_t:], UV[start_uv:], M[start_m:] = [pp], [tt + start_nv], [uu], [mm]
        nv = start_nv + len(pp)
    if len(beams):
        for pt in sorted({b[4] if len(b) > 4 else "grey" for b in beams}):
            mats.append(steel(pt))
            _beams([b for b in beams if (b[4] if len(b) > 4 else "grey") == pt], origin, P, T, UV, M, len(mats) - 1)
    if not P:
        return None
    pp, tt, uu = np.concatenate(P), np.concatenate(T), np.concatenate(UV)
    if junctions:
        pp, uu, tt = _shared(pp, uu, tt)
    return pp, tt, uu, mats, np.concatenate(M)


TRUSS_STEEL = "bne_truss_steel"     # painted steel (the stock metal_paint, grey: tunnel.materials.json) ...


def steel(paint):
    """... and in the other paints (bridgetypes.PAINTS): bne_steel_<paint>."""
    return TRUSS_STEEL if paint in (None, "grey") else f"bne_steel_{paint}"


def _beams(beams, origin, P, T, UV, M, mat):
    """Box beams [(p (3,), q (3,), width, height)] appended to P, T, UV, M (all at once: a truss has hundreds)."""
    b = list(beams)
    p = np.array([x[0] for x in b], np.float64) - np.asarray(origin, np.float64)
    q = np.array([x[1] for x in b], np.float64) - np.asarray(origin, np.float64)
    w = np.array([x[2] for x in b], np.float64)[:, None] / 2
    h = np.array([x[3] for x in b], np.float64)[:, None] / 2
    d = q - p
    ln = np.maximum(np.linalg.norm(d, axis=1), 1e-9)
    d = d / ln[:, None]
    a = np.cross(d, [0.0, 0.0, 1.0])
    vert = np.linalg.norm(a, axis=1) < 1e-3                 # a vertical post: its width along x
    a[vert] = [1.0, 0.0, 0.0]
    a /= np.linalg.norm(a, axis=1)[:, None]
    c = np.cross(a, d)
    corners = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
    V = np.stack([e + a * (sa * w) + c * (sc * h) for e in (p, q) for sa, sc in corners], 1)    # (n, 8, 3)
    nv0 = sum(len(x) for x in P)
    n = len(b)
    P.append(V.reshape(-1, 3))
    u = np.r_[0, 0.25, 0.5, 0.75, 0, 0.25, 0.5, 0.75]
    UV.append(np.stack([np.broadcast_to(u, (n, 8)), np.c_[np.zeros((n, 4)), np.repeat(ln[:, None] / 4.0, 4, 1)]],
                       -1).reshape(-1, 2))
    tri = []
    for i in range(4):                          # the four sides (ends left open: they meet other beams)
        j = (i + 1) % 4
        tri += [(i, j, j + 4), (i, j + 4, i + 4)]
    tri = np.array(tri, np.int64)
    Tn = (tri[None] + (np.arange(n) * 8)[:, None, None]).reshape(-1, 3)
    pts = np.concatenate([V.reshape(-1, 3)])
    cen = np.repeat(((p + q) / 2), len(tri), 0)
    nrm = np.cross(pts[Tn[:, 1]] - pts[Tn[:, 0]], pts[Tn[:, 2]] - pts[Tn[:, 0]])
    flip = (nrm * (pts[Tn].mean(1) - cen)).sum(1) < 0
    Tn[flip] = Tn[flip][:, ::-1]
    T.append(Tn + nv0)
    M.append(np.full(len(Tn), mat))


PAD_DROP = 0.06        # m a junction pad's top lies under the roads' surfaces at its edge points (they draw over it)
PAD_SKIRT = 0.5        # m of slab edge under its rim


def _pad(ring, centre, origin, P, T, UV, M, top, side, field=None):
    """A junction pad (roads.deck_pads: ring (k, 3) counter-clockwise, centre (3,)): its top a fan from the centre in
    the decks' top material, its rim a skirt PAD_SKIRT deep, appended to P, T, UV, M (part of the colliding deck
    mesh)."""
    ring = np.asarray(ring, np.float64)
    if len(ring) < 3:
        return
    c = np.asarray(centre, np.float64)
    o = np.asarray(origin, np.float64)
    nv = sum(len(a) for a in P)
    k = len(ring)
    top_v = np.vstack([c - [0, 0, PAD_DROP], ring - [0, 0, PAD_DROP]]) - o
    i = np.arange(k)
    tris = np.c_[np.zeros(k, np.int64), 1 + i, 1 + (i + 1) % k]
    uv = (top_v[:, :2] + o[:2]) / DECK_TOP_TILE_M      # (world: as a cluster's deck tops, deck()'s quads)
    if field is not None:
        top_v, uv, tris, _src = field.top(top_v + o, uv, tris)
        top_v -= o
    P.append(top_v)
    UV.append(uv)
    T.append(tris + nv)
    M.append(np.full(len(tris), top))
    nv += len(top_v)
    if field is not None:
        # Skirt top follows the shared surface too; it must not remain as an old fan's concrete lip through a lane.
        xy, _j, _f = field.edge(np.vstack([ring[:, :2], ring[:1, :2]]))
        ring = np.c_[xy[:-1], field.at(xy[:-1])]
        k = len(ring)
        i = np.arange(k)
    a = ring - [0, 0, PAD_DROP if field is None else 0.0]
    b = a - [0, 0, PAD_SKIRT]
    sk = np.vstack([a, b]) - o
    P.append(sk)
    UV.append(np.c_[np.r_[np.zeros(k), np.ones(k)] * 0.1, np.r_[i, i] * 0.25])
    j = (i + 1) % k
    # (outward: the ring runs counter-clockwise, so (a_i, b_j, a_j) and (a_i, b_i, b_j) face out)
    T.append(np.concatenate([np.c_[i, k + j, j], np.c_[i, k + i, k + j]]) + nv)
    M.append(np.full(2 * k, side))


def _piers_clear(piers, rb):
    """The piers that cross no other deck under their top: a pier of the Queensboro Bridge's upper roadway stood on
    the ground through the lower one, across its lanes (the user, 2026-10-17: "objects blocking the bridge road")."""
    if not piers or not rb:
        return list(piers)
    lines = [(((l + r) / 2), float(np.median(np.hypot(*(l - r).T))) / 2 + PARAPET_W, z) for (l, r, v, m, layer, z), c in rb
             if not c.get("tunnel")]
    out = []
    for p in piers:
        x, y, ux, uy, zt, zb, width = p[:7]
        q = np.array([x, y])
        reach = float(np.clip(0.7 * width, 2.5, 14.0)) / 2 + PIER_T
        hit = False
        for mid, hw, z in lines:
            d = np.hypot(*(mid - q).T)
            k = int(np.argmin(d))
            if d[k] < hw + reach and z[k] < zt - 1.0:       # a deck below the pier's top, across its footprint
                hit = True
                break
        if not hit:
            out.append(p)
    return out


GROUND_COVER_MIN_W = 3.0    # m: a ground road this wide at a deck edge's height leaves it without a parapet


def _ground_carriageways(chunks):
    """[(ribbon with the nodes' own heights, chunk)] of the tile's ground roads (no decks, tunnels, sidewalks, markings,
    gutters or driveways) for _parapet_cover: a deck at grade across a road has no parapet over its lanes (GitHub #96:
    the Pitt River Bridge's ramps, at grade over Lougheed Highway, stood their walls across its lanes)."""
    out = []
    for c in chunks:
        if c.get("deck") or c.get("tunnel") or c.get("fixed") or c.get("h") is not None or c.get("drive") \
                or float(c.get("w", 0.0)) < GROUND_COVER_MIN_W or layer_of(c) != "base":
            continue
        pts = np.asarray(c["n"], np.float64)
        if len(pts) < 2 or pts.shape[1] < 3:
            continue
        xy, sv, z = _resample(pts, DRAPE_STEP)
        if len(xy) < 2:
            continue
        n = _normals(xy) * (float(c["w"]) / 2)
        out.append(((xy + n, xy - n, sv, c["m"], "base", z), c))
    return out


MEDIAN_NOSE_M = 1.5     # m of gap before MEDIAN_JOIN_M over which a diverging pair's joined slab narrows to a point
                        # (the gore up to it flat pavement, as the real bridge's: 3 m ran the nose's barriers far up
                        # the gore, a lip between the lanes, the user 2026-10-31)
NOSE_LOOK_PTS = 5       # ... where the gap widens toward the run's end by NOSE_WIDEN_M over this many points
NOSE_WIDEN_M = 0.5


def _noses(g):
    """(slab extension m, covered) per edge point from its median gap g (_median_gaps): to the median's middle and no
    parapet, as before, except where the gap widens toward the end of its run past MEDIAN_JOIN_M - MEDIAN_NOSE_M (an
    exit or entrance ramp parting from its road's deck): there the extension narrows to nothing and the parapets follow
    it, the two decks' meeting at a point (a gore nose; the user, 2026-10-31, an I-95 exit by the Middle Branch: the
    slab ended square 6 m across and both walls started there in a step). Pure."""
    g = np.asarray(g, np.float64)
    ext = np.where(g > 0, g / 2 + 0.05, 0.0)
    cov = g > 0
    k = len(g)
    i = 0
    while i < k:
        if g[i] <= 0:
            i += 1
            continue
        j = i
        while j + 1 < k and g[j + 1] > 0:
            j += 1
        for end, inward in ((j, -1), (i, 1)):
            back = g[min(max(end + inward * NOSE_LOOK_PTS, i), j)]
            if g[end] <= MEDIAN_JOIN_M - MEDIAN_NOSE_M or g[end] - back < NOSE_WIDEN_M:
                continue
            q = end
            while i <= q <= j and g[q] > MEDIAN_JOIN_M - MEDIAN_NOSE_M:
                f = min(max((MEDIAN_JOIN_M - g[q]) / MEDIAN_NOSE_M, 0.0), 1.0)
                ext[q] *= f
                cov[q] = False
                q += inward
        i = j + 1
    return ext, cov


def _travel_vec(left, right, tdir):
    """Per ribbon point its traffic's direction (k, 2): the line's tangent times tdir (+1 / -1; 0 two-way: zeros)."""
    lat = left - right
    u = lat / np.maximum(np.hypot(lat[:, 0], lat[:, 1]), 1e-9)[:, None]
    return np.stack([u[:, 1], -u[:, 0]], 1) * float(tdir or 0)


MEDIAN_ALONG = 0.6      # another deck's nearest edge point counts as across a median only this far along (x the gap) ...
MEDIAN_ALONG_M = 1.2    # ... or m (its edge points' spacing)
MEDIAN_INSIDE_M = 0.3   # m: and no further inside this deck's edge


def _median_gaps(rb, context=()):
    """Per deck ribbon (left, right, opposite left, opposite right): the gap (m) from each edge point to the nearest
    edge of another deck within MEDIAN_JOIN_M at about its height (PARAPET_COVER_DZ), else 0: a median between two
    carriageways' decks; and whether that deck carries the opposite traffic (both one-way, their chunks' "tdir").
    rb and context: [(ribbon, chunk)]; the context's pieces of the same deck (its fid: the deck going on in the tiles
    around) are not another deck (they had hidden every median at 0 m)."""
    edges = []                      # (owner, points (k, 3), its traffic's direction (k, 2))
    for n, ((left, right, v, m, layer, z), c) in enumerate(rb):
        key = c.get("fid", ("own", n))
        tv = _travel_vec(left, right, c.get("tdir", 0))
        edges.append((key, np.c_[left, z], tv))
        edges.append((key, np.c_[right, z], tv))
    for k, ((left, right, v, m, layer, z), c) in enumerate(context):
        key = c.get("fid", ("ctx", k))
        tv = _travel_vec(left, right, c.get("tdir", 0))
        edges.append((key, np.c_[left, z], tv))
        edges.append((key, np.c_[right, z], tv))
    out = []
    for n, ((left, right, v, m, layer, z), c) in enumerate(rb):
        own = c.get("fid", ("own", n))
        mine = _travel_vec(left, right, c.get("tdir", 0))
        sides, opps, hit_owners, overs = [], [], [], []
        for E, out_ in ((np.c_[left, z], left - right), (np.c_[right, z], right - left)):
            g = np.zeros(len(E))
            opp = np.zeros(len(E), bool)
            over = np.zeros(len(E), bool)
            own_hit = [None] * len(E)
            tan = np.gradient(E[:, :2], axis=0) if len(E) > 1 else np.zeros((len(E), 2))
            tan = tan / np.maximum(np.hypot(tan[:, 0], tan[:, 1]), 1e-9)[:, None]
            out_ = out_ / np.maximum(np.hypot(out_[:, 0], out_[:, 1]), 1e-9)[:, None]
            for owner, O, tv in edges:
                if owner == own or not len(O):
                    continue
                # (only near ones: a cheap box test first)
                if (O[:, 0].min() > E[:, 0].max() + MEDIAN_JOIN_M or O[:, 0].max() < E[:, 0].min() - MEDIAN_JOIN_M
                        or O[:, 1].min() > E[:, 1].max() + MEDIAN_JOIN_M
                        or O[:, 1].max() < E[:, 1].min() - MEDIAN_JOIN_M):
                    continue
                d = np.hypot(E[:, None, 0] - O[None, :, 0], E[:, None, 1] - O[None, :, 1])
                dz = np.abs(E[:, None, 2] - O[None, :, 2])
                d = np.where(dz < PARAPET_COVER_DZ, d, np.inf)
                j = d.argmin(1)
                d = d[np.arange(len(E)), j]
                # (beside, not behind or ahead: a deck going on from this one's end at a merge was a "median" growing
                # with the distance back to its end, the slab filled toward it and the parapet gone; the two ramps
                # merging on I-95 by the Middle Branch, 2026-10-31)
                # and outside this edge (another deck's edge over this one's slab, two merging roads side by side on
                # one deck: no gap)
                rel = O[j, :2] - E[:, :2]
                along = np.abs((rel * tan).sum(1))
                # (nor this edge's end on another's end: one deck going on from the other, the parapet going on too)
                ends = np.zeros(len(E), bool)
                ends[[0, -1]] = (j[[0, -1]] == 0) | (j[[0, -1]] == len(O) - 1)
                against = (mine * tv[j]).sum(1) < -0.5
                # (opposite traffic over each other's slab still gets its median barrier between them)
                hit = ((d < MEDIAN_JOIN_M) & ((g == 0) | (d < g)) & (along <= np.maximum(MEDIAN_ALONG * d, MEDIAN_ALONG_M))
                       & (((rel * out_).sum(1) > -MEDIAN_INSIDE_M) | against) & ~ends)
                g = np.where(hit, np.maximum(d, 1e-3), g)
                opp = np.where(hit, against, opp)
                over = np.where(hit, (rel * out_).sum(1) < 0.0, over)     # (its edge over this slab: _opposite)
                for idx in np.flatnonzero(hit):
                    own_hit[idx] = owner
            sides.append(g)
            opps.append(opp)
            hit_owners.append(own_hit)
            overs.append(over)
        # (an edge facing away from the other deck is near it too where the decks are narrow: only the nearer edge
        # of the two faces the median when both edges saw the same target deck; when facing different decks on left
        # and right, both medians are kept, #651)
        gl, gr = sides
        same_target = np.array([ol is not None and ol == or_ for ol, or_ in zip(hit_owners[0], hit_owners[1])])
        both = (gl > 0) & (gr > 0) & same_target
        gl = np.where(both & (gl > gr), 0.0, gl)
        gr = np.where(both & (gr >= sides[0]), 0.0, gr)
        gl, gr = _fill_short(gl, MEDIAN_FILL_PTS), _fill_short(gr, MEDIAN_FILL_PTS)
        out.append((gl, gr, opps[0] & (gl > 0), opps[1] & (gr > 0), overs[0] & (gl > 0), overs[1] & (gr > 0)))
    return out


# Opposite traffic either side of a median (a divided road's two carriageways on their decks): never one open slab,
# their traffic meeting across it with nothing between (the I-95 viaduct by the Middle Branch, joined where its two
# decks came within MEDIAN_JOIN_M; the aerial: two decks, a narrow gap). Up to MEDIAN_BARRIER_M apart the gap is
# filled and each deck's parapet stands at its middle (back to back: a median barrier); wider, each its own at its own
# edge, the gap open. The user, 2026-10-31.
MEDIAN_BARRIER_M = 2.5


REACH_SLOPE = 0.35      # m the slab's reach past its road (a median's fill, a nose) may change per m along its edge


def _reach(wide, ext):
    """The slab's reach past the road's edge: at least wide, or ext where it is negative (a median barrier in the
    middle of two overlapping decks, _opposite)."""
    ext = np.asarray(ext, np.float64)
    return np.where(ext < 0.0, ext, np.maximum(wide, ext))


def _smooth_reach(ext, edge):
    """ext (per edge point the slab's reach past the road) with no change steeper than REACH_SLOPE along the edge
    (k, 2): only ever more (a fill ends in a wedge instead of a square end), never less (no hole). The fill to a
    median's middle came and went with the deck beside it, a 1.2-2.6 m notch each time (11 along Springfield's Old
    Keene Mill Road, deckedges.py 2026-10-31). Pure."""
    ext = np.asarray(ext, np.float64).copy()
    if len(ext) < 2:
        return ext
    ds = np.hypot(*np.diff(np.asarray(edge, np.float64)[:, :2], axis=0).T) * REACH_SLOPE
    for k in range(1, len(ext)):
        ext[k] = max(ext[k], ext[k - 1] - ds[k - 1])
    for k in range(len(ext) - 2, -1, -1):
        ext[k] = max(ext[k], ext[k + 1] - ds[k])
    return ext


def _opposite(g, opp, ext, cov, over=None):
    """(ext, cov) of _noses with the points facing opposite traffic (opp) made a median barrier or an open gap. Where
    the other deck's edge lies over this slab (over: the drawn carriageways overlap), the barrier stands in the
    middle of the overlap, ext negative (within the road's edge): at this one's edge it stood 1.7 m into the other
    carriageway's lanes (the S7's viaducts at Gdansk, GitHub #1025)."""
    ext, cov = ext.copy(), cov.copy()
    near = opp & (g > 0) & (g <= MEDIAN_BARRIER_M)
    far = opp & (g > MEDIAN_BARRIER_M)
    inside = near & over if over is not None else np.zeros(len(g), bool)
    ext = np.where(inside, -g / 2, np.where(near, g / 2 + 0.05, np.where(far, 0.0, ext)))
    cov = np.where(opp, False, cov)
    return ext, cov


MEDIAN_FILL_PTS = 8     # a median's run of points missing it between two that face it (this many or fewer) faces it too


def _fill_short(g, k):
    """g (per edge point the median gap, 0 none) with each run of zeros of k points or fewer between two nonzero
    points filled by the interpolation of its ends: an edge facing a median now and then missed it a point at a time
    and the slab's edge zigzagged, black teeth of the ground under the Four Level's 110 deck (Los Angeles, 2026-10-27)."""
    g = np.asarray(g, np.float64).copy()
    nz = np.flatnonzero(g > 0)
    if len(nz) < 2:
        return g
    for a, b in zip(nz[:-1], nz[1:]):
        if 1 < b - a <= k + 1:
            g[a + 1:b] = np.interp(np.arange(a + 1, b), [a, b], [g[a], g[b]])
    return g


def _parapet_cover(rb, context=()):
    """Per deck ribbon (left, right edge points) whether another deck's surface lies there (no parapet):
    earthfast.deck_edges_covered over every ribbon's centre line, its half width + PARAPET_W; Python else."""
    import fast
    out = []
    if not rb:
        return out
    pts, own, A, B, HW, OS, EN = [], [], [], [], [], [], []

    def square(k):                  # (its first and last segments end the deck square: earthfast's ends)
        e = np.zeros(k - 1, np.int64)
        if k > 1:
            e[0] |= 1
            e[-1] |= 2
        return e
    for n, ((left, right, v, m, layer, z), c) in enumerate(rb):
        lat = left - right
        u = lat / np.maximum(np.hypot(lat[:, 0], lat[:, 1]), 1e-9)[:, None]
        mid = (left + right) / 2
        hw = float(np.median(np.hypot(lat[:, 0], lat[:, 1]))) / 2 + PARAPET_W
        oid = hash(c.get("fid", n)) & 0x3FFFFFFF
        for e in (left + u * PARAPET_W / 2, right - u * PARAPET_W / 2):
            pts.append(np.c_[e, z])
            own.append(np.full(len(z), oid))
        A.append(np.c_[mid[:-1], z[:-1]])
        B.append(np.c_[mid[1:], z[1:]])
        HW.append(np.full(len(z) - 1, hw))
        OS.append(np.full(len(z) - 1, oid))
        EN.append(square(len(z)))
    for (left, right, v, m, layer, z), c in context:     # the decks around the tile: surfaces only
        lat = left - right
        mid = (left + right) / 2
        hw = float(np.median(np.hypot(lat[:, 0], lat[:, 1]))) / 2 + PARAPET_W
        oid = hash(c.get("fid", "ctx")) & 0x3FFFFFFF
        A.append(np.c_[mid[:-1], z[:-1]])
        B.append(np.c_[mid[1:], z[1:]])
        HW.append(np.full(len(z) - 1, hw))
        OS.append(np.full(len(z) - 1, oid))
        # (a ground road's end is no deck's: one meeting a deck's side at grade ends on its edge, the parapet open
        # there; square, they stood across a driveway at the JFX and one in Springfield, rampcheck.py)
        EN.append(square(len(z)) if c.get("deck") else np.zeros(max(len(z) - 1, 0), np.int64))
    P_, O_ = np.concatenate(pts), np.concatenate(own).astype(np.int64)
    A_, B_, H_, S_ = np.concatenate(A), np.concatenate(B), np.concatenate(HW), np.concatenate(OS).astype(np.int64)
    fast.require(122, "deck_edges_covered")
    cov = np.asarray(fast._ef.deck_edges_covered(np.ascontiguousarray(P_), O_, np.ascontiguousarray(A_),
                                                  np.ascontiguousarray(B_), H_, S_, PARAPET_COVER_DZ,
                                                  np.concatenate(EN)))
    k0 = 0
    for (left, right, v, m, layer, z), c in rb:
        k = len(z)
        out.append((cov[k0:k0 + k], cov[k0 + k:k0 + 2 * k]))
        k0 += 2 * k
    return out



def parapet_runs(chunks, context=(), _rb=None, ground=(), pads=()):
    """Actual generated bridge parapets as XY centre lines plus the mesh geometry.

    Uses the same coverage masks, edge offsets, tunnel exclusion, and minimum-run rule as deck().
    _rb lets deck() share its already-computed ribbons. Returned dictionaries are ordered by deck
    chunk and edge, and include the points deck() uses verbatim.
    """
    junctions = _junction_surfaces(chunks, context)
    if _rb is None:
        dch = [c for c in chunks if c.get("deck")]
        cr_all = [(r, c) for r, c in ribbon_pairs(list(context)) if r[5] is not None]
        rb = trim_overlaps([(r, c) for r, c in ribbon_pairs(dch) if r[5] is not None], cr_all,
                           junctions)
    else:
        rb = _rb
    if not rb:
        return []

    cr = [r for r in ribbons(list(context)) if r[5] is not None]
    cov_ctx = [(r, c) for r, c in zip(cr, context)] + _ground_carriageways(list(chunks) + list(ground))
    covered = _parapet_cover([(r, c) for r, c in rb], cov_ctx)
    medians = _median_gaps([(r, c) for r, c in rb],
                           [(r, c) for r, c in zip(ribbons(list(context)), context) if r[5] is not None])
    out = []
    for n, ((left, right, v, _m, _layer, z), c) in enumerate(rb):
        if c.get("tunnel"):       # deck() draws the tunnel floor without parapets
            continue
        lat = left - right
        u = lat / np.maximum(np.hypot(lat[:, 0], lat[:, 1]), 1e-9)[:, None]
        wide = 0.0 if c.get("tunnel") else PARAPET_W
        gl_, gr_, ol_, or_, vl_, vr_ = medians[n]
        (xl, cl), (xr, cr_) = _opposite(gl_, ol_, *_noses(gl_), vl_), _opposite(gr_, or_, *_noses(gr_), vr_)
        covered[n] = (covered[n][0] | cl, covered[n][1] | cr_)
        # (full barriers, never the low curb: a gore nose's points, _noses; a median barrier's, _opposite)
        nose = (((gl_ > 0) & ~cl & ~ol_) | (ol_ & (gl_ <= MEDIAN_BARRIER_M)),
                ((gr_ > 0) & ~cr_ & ~or_) | (or_ & (gr_ <= MEDIAN_BARRIER_M)))
        if len(pads):               # (none inside a junction pad: roads.deck_pads)
            import roads as _roads
            covered[n] = (covered[n][0] | _roads.pad_inside(pads, left, z=z),
                          covered[n][1] | _roads.pad_inside(pads, right, z=z))
        el = left + u * _smooth_reach(_reach(wide, xl), left)[:, None]
        er = right - u * _smooth_reach(_reach(wide, xr), right)[:, None]
        k = len(z)
        for sgn, edge, inner, cov, nz in ((1, el, left, covered[n][0], nose[0]),
                                          (-1, er, right, covered[n][1], nose[1])):
            i = 0
            while i < k:
                if cov[i]:
                    i += 1
                    continue
                j = i
                while j + 1 < k and not cov[j + 1]:
                    j += 1
                run_len = float(np.hypot(*np.diff(edge[i:j + 1], axis=0).T).sum()) if j > i else 0.0
                if j > i and (run_len >= PARAPET_MIN_M or i == 0 or j == k - 1):
                    sl = slice(i, j + 1)
                    edge_run, inner_run = edge[sl], inner[sl]
                    mid_ = (left + right) / 2
                    ends = list(c.get("abut") or [])
                    if i > 0:                                       # the run stops there (a gap, a gore)
                        ends.append(tuple(mid_[i]))
                    if j < k - 1:
                        ends.append(tuple(mid_[j]))
                    out.append({
                        "mid": mid_[sl],
                        "ends": ends,
                        "deck": n,
                        "line": (edge_run + inner_run) * 0.5,
                        "edge": edge_run,
                        "inner": inner_run,
                        "z": junctions[c['fid']].at(edge_run) if c.get('fid') in junctions else z[sl],
                        "v": v[sl],
                        "out": u[sl] * sgn,
                        "nose": bool(nz[sl].any()),     # (a gore nose's: a full barrier, never the low curb)
                    })
                i = j + 1
    return out


def _pier(p, origin, P, T, UV, M, mat):
    """One pier (roads.pier_sites) appended to P, T, UV, M: a tapered wall with a cap under the girders, or for a deck
    wider than PIER_BENT_W a bent: square columns under a cap beam."""
    x, y, ux, uy, zt, zb, width = p[:7]
    thick = p[7] if len(p) > 7 else None        # a truss's anchor pier: one solid wall this thick, the truss's width
    t = np.array([ux, uy])
    nrm = np.array([-uy, ux])
    c = np.array([x, y])
    boxes = []                                  # (centre, half along t, half across, z0, z1, widen per m down)
    cap_w = width / 2 + PARAPET_W
    if thick is not None:
        boxes.append((c, thick / 2, width / 2, zb, zt, PIER_TAPER))
    else:
        boxes.append((c, PIER_T / 2 + 0.3, cap_w, zt - PIER_CAP_H, zt, 0.0))
    if thick is not None:
        pass
    elif width > PIER_BENT_W:
        nc = max(2, int(round(width / 9.0)))
        for off in np.linspace(-(width / 2 - 2.0), width / 2 - 2.0, nc):
            boxes.append((c + nrm * off, PIER_COLUMN / 2, PIER_COLUMN / 2, zb, zt - PIER_CAP_H, 0.0))
    else:
        wall = float(np.clip(0.7 * width, 2.5, 14.0)) / 2
        boxes.append((c, PIER_T / 2, wall, zb, zt - PIER_CAP_H, PIER_TAPER))
    for cc, ht, hn, z0, z1, taper in boxes:
        grow = taper * max(z1 - z0, 0.0)
        top = [cc + t * a * ht + nrm * b * hn for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        bot = [cc + t * a * (ht + grow * 0.5) + nrm * b * (hn + grow) for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        nv = sum(len(a) for a in P)
        V = np.array([[*q, z1] for q in top] + [[*q, z0] for q in bot]) - np.asarray(origin, np.float64)
        P.append(V)
        UV.append(np.c_[np.r_[0, 1, 0, 1, 0, 1, 0, 1] * 0.25, np.r_[0, 0, 0, 0, 1, 1, 1, 1] * (z1 - z0) / 8.0])
        tri = []
        for i in range(4):                      # sides (outward: the corners go counter-clockwise from above)
            a, b = i, (i + 1) % 4
            tri += [(a, b + 4, b), (a, a + 4, b + 4)]
        tri += [(0, 1, 2), (0, 2, 3)]           # top
        tri = np.array(tri, np.int64)
        # outward winding: check one side's normal against the outward direction, flip all if wrong
        Vn = V[tri]
        nrm_ = np.cross(Vn[:, 1] - Vn[:, 0], Vn[:, 2] - Vn[:, 0])
        cen = V.mean(0)
        flip = (nrm_ * (Vn.mean(1) - cen)).sum(1) < 0
        tri[flip] = tri[flip][:, ::-1]
        T.append(tri + nv)
        M.append(np.full(len(tri), mat))


TUNNEL_SHOULDER = 1.2      # m of concrete floor between the road's edge and the wall
TUNNEL_WALL_H = 4.6        # m of upright wall before the vault
TUNNEL_H = 6.8             # m from the floor to the vault's top
PORTAL_FRAME = 6.0         # m of portal face around the opening (hides the terrain hole's 2 m steps)
TUNNEL_SIDE = DECK_SIDE    # the tube's concrete (portal faces) ...
TUNNEL_INNER = "bne_tunnel_inner"   # ... and inside a full tube darker (tunnel.materials.json): lit by the sky as the
                            # engine lights it, the Caldecott's tube was as bright as the day outside (its photos:
                            # dark, lit by its lamps, 2026-10-18)
TUNNEL_STONE = "bne_tunnel_stone"  # a low arch's portal faces and the retaining walls: grey coursed rubble (Central
                                    # Park's transverse walls and arches; the buildings' pale dressed stone read as
                                    # concrete, 2026-10-17)
TUNNEL_STONE_IN = "bne_tunnel_stone_in"   # the same stone darker: a low arch's vault (in its shade)
PORTAL_COLLAR = "bne_portal_collar"   # a portal's collar round the opening: fair-faced / painted concrete, lighter than
                                      # its headwall (Loibl's white ring in a stone wall, Laerdal's pale hood in the
                                      # slope; Mapillary 2026-11-12)
TUNNEL_SOIL = "bne_tunnel_soil"     # grass on the ground over the masonry: a retaining wall's top behind its coping,
                                    # a low arch's top (stone slabs on the grass, the user 2026-10-17: "blend the
                                    # stone and terrain better")
WALL_SCREE = "bne_wall_scree"       # ... a mountain pass road's: rocky ground (the terrain's bngEarthGroundRock detail)
SCREE_UV_M = 2.0           # m per repeat of the scree (bngEarthGroundRock's detail size)
SOIL_UV_M = 1.0            # m per repeat of the grass (the terrain's detail texture: bngEarthGroundGrass, size 1)
WALL_COPING_M = 0.5        # m of a retaining wall's top in stone (its coping), the rest of it soil
WALL_SKIRT_M = 2.0         # m a retaining wall's back reaches down from its top
STONE_UV_M = 3.0           # m per repeat of the stone on a low arch
CONCRETE_UV_M = 4.0        # m per repeat of the concrete (as a tunnel's)
LOW_PORTAL_UP = 1.4        # m a low arch's portal face reaches over the vault (to the ground over it) ...
LOW_PORTAL_SIDE = 3.0      # ... and beside the opening
LOW_RISE = 0.4             # a low arch's vault rises this share of its height from its walls (a segmental arch)
BOX_HAUNCH_M = 0.5         # m a cut-and-cover box's corner haunch reaches into its roof and walls
ARCH_CAP_OVER = 0.75       # m a low arch's top lies over its vault (roads.ARCH_SOIL_MIN of soil there, + a little)
LOW_CAP_MIN = 1.0          # m: a stone top shorter than this (a road just behind the face) is left out
LOW_CAP_M = 7.0            # m a low arch's stone top runs back from its portal face over the mouth: the terrain there
                           # dips to the floor and is cut away (roads.arch_raise, LOW_HOLE_M + LOW_HOLE_STEP): from
                           # above a row of dark terrain teeth behind the face (79th Street Transverse, 2026-10-17)
TUNNEL_OVERLAP = 0.25      # m a tube piece reaches past a tile edge
TUNNEL_LIGHT = "bne_tunnel_light"   # the lamps' emissive material (levels/beamng_earth/art/shapes/beamngEarth)
TUNNEL_LAMP_EVERY = 6.0    # m between the lamp fixtures along each wall ...
TUNNEL_LAMP_L = 1.2        # ... each this long, ...
TUNNEL_LAMP_W = 0.3        # ... this tall, ...
TUNNEL_LAMP_DROP = 0.25    # ... its middle this far under the wall's top, ...
TUNNEL_LAMP_IN = 0.08      # ... this far in from the wall


def _tunnel_section(hw, th=TUNNEL_H, low=False, box=False):
    """The tube's cross-section from the left road edge's floor over the vault to the right one: (lateral offset
    from the centre line, positive = left; height over the floor) for half-width hw (centre to wall) and a vault
    th over the floor (its walls in proportion). low: a park arch's segmental vault, a shallow circular arc rising
    LOW_RISE of th from its walls (Central Park's transverse arches). box: a cut-and-cover box, its roof flat at th
    with a haunch BOX_HAUNCH_M into each upper corner (a major road's low tube: Perth's Northbridge, Washington's
    I-395 and Paris's Porte d'Italie mouths are rectangular, not arches, 2026-11-12)."""
    if box:
        hn = min(BOX_HAUNCH_M, 0.25 * th, 0.25 * hw)
        vault = [(hw, th - hn), (hw - hn, th), (-hw + hn, th), (-hw, th - hn)]
    elif low:
        rise = LOW_RISE * th
        wall = th - rise
        r = (hw * hw + rise * rise) / (2 * rise)
        a0 = math.asin(min(1.0, hw / r))
        vault = [(r * math.sin(a), th - r + r * math.cos(a)) for a in np.linspace(a0, -a0, 9)]
    else:
        wall = TUNNEL_WALL_H * th / TUNNEL_H
        vault = [(np.cos(a) * hw, wall + np.sin(a) * (th - wall))
                 for a in np.linspace(0.0, np.pi, 9)]
    return np.array([(hw - TUNNEL_SHOULDER, 0.0), (hw, 0.0)] + vault + [(-hw, 0.0), (-hw + TUNNEL_SHOULDER, 0.0)],
                    np.float64)


WALL_THICK_M = 2.4         # a retaining wall's top: over the grading's flat strip behind it (grading.WALL_FLAT_M)


def _retaining_walls(walls, origin, P, T, UV, M, nv):
    """roads.retaining_walls' walls as stone (material index 2): the face toward the road, the top, the ends;
    appended to P, T, UV, M. Returns the new vertex count."""
    for w in walls:
        b = np.asarray(w["base"], np.float64)
        top, foot, tw = np.asarray(w["top"]), np.asarray(w["foot"]), np.asarray(w["toward"], np.float64)
        k = len(b)
        if k < 2:
            continue
        s = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(b, axis=0).T))))
        thick = np.asarray(w.get("thick", np.full(k, WALL_THICK_M)), np.float64)    # (roads.retaining_walls)
        face_m, face_uv = (0, CONCRETE_UV_M) if w.get("concrete") else (2, STONE_UV_M)  # (a major road's: concrete)
        back = b - tw * thick[:, None]
        # (its top behind the coping up to the ground at its back edge: roads.retaining_walls' back_top)
        btop = np.maximum(np.asarray(w.get("back_top", top), np.float64), top)
        face_lo = np.c_[b, foot] - origin
        face_hi = np.c_[b, top] - origin
        back_hi = np.c_[back, btop] - origin
        cop = np.minimum(WALL_COPING_M, thick)
        cop_hi = np.c_[b - tw * cop[:, None], top] - origin
        quads = [(face_lo, face_hi, foot, top, face_m), (face_hi, cop_hi, None, float(np.mean(cop)), face_m)]
        if (thick > cop + 0.01).any():          # the soil behind the coping (a parapet is all stone)
            # (a mountain pass road's: the rocky ground of the slopes round it, WALL_SCREE; grass elsewhere)
            quads.append((cop_hi, back_hi, None, float(np.mean(thick - cop)), 6 if w.get("mountain") else 4))
        if w.get("parapet"):                    # a parapet's back, seen from the valley
            bot = foot
        else:                                   # a retaining wall's back: down into the ground behind it (where the
            bot = btop - WALL_SKIRT_M           # ground there lies lower, its edge was open, seen from above)
        back_lo = np.c_[back, bot] - origin
        quads.append((back_lo, back_hi, bot, top, face_m))
        # its underside, from the back's bottom to the face's foot: a closed solid (2026-10-27: no face of it open,
        # its ends one-sided caps of its section; every edge shared by two faces, test_shortdecks.WallSolidTest)
        quads.append((back_lo, face_lo, None, float(np.mean(thick)), face_m))
        for qi, (lo_, hi_, vlo, vhi, mat) in enumerate(quads):
            P.append(np.concatenate([lo_, hi_]))
            if vlo is not None:
                UV.append(np.column_stack((np.concatenate((s, s)) / face_uv, np.concatenate((vlo, vhi)) / face_uv)))
            else:
                sc = SOIL_UV_M if mat == 4 else (SCREE_UV_M if mat == 6 else face_uv)
                UV.append(np.column_stack((np.concatenate((s, s)) / sc, np.concatenate((np.zeros(k), np.full(k, vhi))) / sc)))
            i = np.arange(k - 1)
            A, A1, B, B1 = nv + i, nv + i + 1, nv + k + i, nv + k + i + 1
            tri = np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)])
            pts = np.concatenate([lo_, hi_])
            nrm = np.cross(pts[tri[:, 1] - nv] - pts[tri[:, 0] - nv], pts[tri[:, 2] - nv] - pts[tri[:, 0] - nv])
            # the face looks toward the road, the top up, the back away from the road, the underside down
            if vlo is not None:
                sgn = -1.0 if qi == len(quads) - 2 else 1.0
                look = np.c_[sgn * np.concatenate([tw[i], tw[i]]), np.zeros(2 * (k - 1))]
            else:
                look = np.tile([0.0, 0.0, -1.0 if qi == len(quads) - 1 else 1.0], (2 * (k - 1), 1))
            flip = (nrm * look).sum(1) < 0
            tri[flip] = tri[flip][:, ::-1]
            T.append(tri)
            M.append(mat)
            nv += 2 * k
        for at, nb in ((0, 1), (k - 1, k - 2)):     # the ends: the section (face foot, face top, coping, back top,
            # back bottom) as one-sided caps facing out along the wall (they were two-sided quads foot to top, open to
            # the coping and the skirt), world-scale planar uv
            q = np.array([face_lo[at], face_hi[at], cop_hi[at], back_hi[at], back_lo[at]], np.float64)
            out = np.r_[b[at] - b[nb], 0.0]
            out /= max(float(np.hypot(out[0], out[1])), 1e-9)
            across = np.r_[-tw[at], 0.0]
            P.append(q)
            UV.append(np.column_stack(((q @ across) / face_uv, q[:, 2] / face_uv)))
            tri = np.array([[0, 1, 2], [0, 2, 3], [0, 3, 4]], np.int64)
            e1, e2 = q[tri[:, 1]] - q[tri[:, 0]], q[tri[:, 2]] - q[tri[:, 0]]
            nrm = np.cross(e1, e2)
            keep = np.hypot(*nrm.T) > 1e-9              # (the coping at the back: no soil, a corner twice)
            tri = tri[keep]
            flip = (nrm[keep] @ out) < 0
            tri[flip] = tri[flip][:, ::-1]
            T.append(tri + nv)
            M.append(face_m)
            nv += 5
    return nv


def tunnel(chunks, origin, walls=()):
    """The tunnels of a tile as one colliding mesh: around every "tunnel" chunk (its road surface: a deck, deck()
    makes its slab) a tube of concrete (TUNNEL_SIDE) faced inwards: shoulders, walls and a vault TUNNEL_H over the
    floor; at a chunk end that is a portal ("portal") a face around the opening, seen from both sides. (positions,
    tris, uvs, materials, tri material index) or None. Pure."""
    tc = [c for c in chunks if c.get("tunnel") and not c.get("no_tube")]     # (twins: one tube_only round both)
    rb = [(r, c) for r, c in zip(ribbons(tc), tc) if r[5] is not None] if tc else []
    if not rb and not walls:
        return None
    P, T, UV, M = [], [], [], []           # M: per T entry its material index (0 concrete, 1 lamps, 2 stone, 3 dark stone,
                                            # 4 soil)
    nv = _retaining_walls(walls, np.asarray(origin, np.float64), P, T, UV, M, 0) if walls else 0
    for (left, right, v, _, _, z), c in rb:
        # a piece ending at a tile edge reaches TUNNEL_OVERLAP past it: the next tile's piece meets it there without a
        # hairline of daylight (2026-10-17, inside Eisenhower)
        portal = c.get("portal") or (False, False)
        left, right, z = left.copy(), right.copy(), z.copy()
        for at, nb in ((0, 1), (-1, -2)):
            if portal[0 if at == 0 else 1] or len(left) < 2:
                continue
            for arr in (left, right):
                t = arr[at] - arr[nb]
                arr[at] += t / max(float(np.hypot(*t)), 1e-9) * TUNNEL_OVERLAP
        mid = (left + right) / 2
        lat = left - right
        lw = np.maximum(np.hypot(lat[:, 0], lat[:, 1]), 1e-9)
        u = lat / lw[:, None]                               # unit left
        hw = float(np.median(lw)) / 2 + TUNNEL_SHOULDER
        th = float(c.get("tunnel_h") or TUNNEL_H)
        low = th < TUNNEL_H - 0.05                          # a low arch: stone, a short portal face
        # (a major road's low arch in concrete, as its cutting's walls: the Gotthard road tunnel's portal, a cut-and-cover
        # box under the meadow at Goeschenen, stood as a park's stone arch, 2026-10-28)
        stone = low and not c.get("concrete")
        mat = 3 if stone else 5                               # (its portal faces: 2, the stone in daylight; a full
                                                            # tube's inside: 5, TUNNEL_INNER)
        sec = _tunnel_section(hw, th, low, box=low and not stone and portals.ON)    # (a major road's low tube: a box)
        k, m = len(mid), len(sec)
        rows = np.empty((m, k, 3))
        for j, (off, h) in enumerate(sec):
            rows[j, :, :2] = mid + u * off
            rows[j, :, 2] = z + h
        rows -= origin
        # inward: towards the tube's axis (a point th / 2 over the floor's centre)
        axis = np.c_[mid, z + th / 2] - origin
        for j in range(m - 1):
            a, b = rows[j], rows[j + 1]
            P.append(np.concatenate([a, b]))
            if stone:                                       # the stone at its scale: around the section, along
                sj = float(np.hypot(*(sec[min(j + 1, m - 1)] - sec[j])))
                s0 = float(np.sum(np.hypot(*np.diff(sec[:j + 1], axis=0).T))) if j else 0.0
                along = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(mid, axis=0).T))))
                UV.append(np.column_stack((np.concatenate((np.full(k, s0), np.full(k, s0 + sj))) / STONE_UV_M, np.concatenate((along, along)) / STONE_UV_M)))
            else:
                UV.append(np.column_stack((np.concatenate((np.full(k, j / 4.0), np.full(k, (j + 1) / 4.0))), np.concatenate((v, v)))))
            i = np.arange(k - 1)
            A, A1, B, B1 = nv + i, nv + i + 1, nv + k + i, nv + k + i + 1
            tri = np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)])
            pts = np.concatenate(P)
            nrm = np.cross(pts[tri[:, 1]] - pts[tri[:, 0]], pts[tri[:, 2]] - pts[tri[:, 0]])
            cen = (pts[tri[:, 0]] + pts[tri[:, 1]] + pts[tri[:, 2]]) / 3
            ax = np.concatenate([axis[i], axis[i]])
            flip = (nrm * (ax - cen)).sum(1) < 0
            tri[flip] = tri[flip][:, ::-1]
            # a low arch is seen from outside too (where its thin cover is cut away: its stone, not a hole)
            T.append(np.concatenate([tri, tri[:, ::-1]]) if low and j >= 2 and j < m - 3 else tri)
            M.append(mat)
            nv += 2 * k
        # the lamps: under the vault along both lines, every other step, faced down (a long tunnel's; not under an arch)
        if not low:
            nv = _tunnel_lamps(P, T, UV, M, nv, mid, u, z, hw, origin)
        fits = c.get("fit") or (None, None)
        new_portal = portals.ON
        if new_portal and not low and c.get("ground") is not None and len(c["ground"]) == len(c["n"]) and k >= 2:
            # the tube's outside where the ground does not cover it (portals.cover: a box, soil on its top); the
            # chunk's ground at the ribbon's rows, by the arc along it
            cn = np.asarray(c["n"], np.float64)[:, :2]
            sc = np.r_[0.0, np.cumsum(np.hypot(*np.diff(cn, axis=0).T))]
            sm = np.r_[0.0, np.cumsum(np.hypot(*np.diff(mid, axis=0).T))]
            gr = np.interp(sm * (sc[-1] / max(float(sm[-1]), 1e-9)), sc, np.asarray(c["ground"], np.float64))
            # (no cover under a hood: it is the tube's outside from its mouth back to the hill's face)
            for e_, f_ in enumerate(fits if c.get("portal") else ()):
                if f_ is not None and (c.get("portal") or (False, False))[e_]:
                    d_ = sm if e_ == 0 else sm[-1] - sm
                    gr = np.where(d_ <= portals.setback(f_) + 0.5 if portals.setback(f_) > 0 else False, np.inf, gr)
            nv = _add_blocks(P, T, UV, M, nv, portals.cover(mid - np.asarray(origin, np.float64)[:2], u, z - origin[2],
                                                             gr - origin[2], hw, th, (False, False), CONCRETE_UV_M,
                                                             SOIL_UV_M, sec), 0)
        pups = c.get("portal_up") or (None, None)
        caps = c.get("cap_m") or (None, None)       # (a road over the mouth: its stone top shorter, TunnelProfiles.caps)
        for end, at, pup, cap_m in zip(c.get("portal") or (False, False), (0, k - 1), pups, caps):
            if not end:
                continue
            fitd = fits[0 if at == 0 else 1] if new_portal else None
            if fitd is not None:
                # the mouth's structure from the ground round it (portals.structure: collar, headwall, wing walls);
                # in concrete (a park's low arch: stone)
                tt = mid[1] - mid[0] if at == 0 else mid[-1] - mid[-2]
                tt = tt / max(float(np.hypot(*tt)), 1e-9)
                ul, outv = np.array([-tt[1], tt[0]]), (-tt if at == 0 else tt)
                near = [False, False]
                for si, sg in enumerate((1.0, -1.0)):
                    q = mid[at] + ul * sg * (hw + 3.0) + outv * 4.0
                    near[si] = any(float(np.hypot(*(np.asarray(w["base"], np.float64)[:, :2] - q).T).min()) < 5.0
                                   for w in walls if len(w["base"]))
                stone_p = stone if low else False     # (a full tube's mouth: concrete, its hood or box; stone only
                                                      # on a park's low arch)
                nv = _add_blocks(P, T, UV, M, nv, portals.structure(
                    fitd, np.r_[mid[at], z[at]] - origin, ul, outv, sec, th, hw, STONE_UV_M if stone_p else CONCRETE_UV_M,
                    near, low), 2 if stone_p else 0, 2 if (low and stone_p) else 7)
                if low and (cap_m is None or cap_m >= LOW_CAP_MIN):
                    # a low arch's top over its mouth (the terrain is cut away there): at the ground behind the
                    # headwall, as wide as the headwall
                    soil = portals.crown_top(fitd, th, low) - portals.parapet(fitd, low)
                    nv = _arch_cap(P, T, UV, M, nv, mid, u, z, at, k, hw + min(float(fitd["sl"]), float(fitd["sr"])),
                                   soil + (LOW_PORTAL_UP - ARCH_CAP_OVER), th, origin,
                                   LOW_CAP_M if cap_m is None else min(LOW_CAP_M, float(cap_m)), 2 if stone_p else 0)
                continue
            # the face: from the opening's outline out to a frame PORTAL_FRAME wider and higher, both sides
            inner = rows[:, at]
            o = sec.copy()
            up, side = (LOW_PORTAL_UP, LOW_PORTAL_SIDE) if low else (PORTAL_FRAME, PORTAL_FRAME)
            if pup is not None:     # to the ground over the portal (TunnelProfiles.portal_ups); a low arch's at least
                # LOW_PORTAL_UP (under a hill higher than its top, the ground dipping into its mouth showed over it)
                up = max(LOW_PORTAL_UP, float(pup)) if low else float(pup)
            o[:, 0] = np.sign(o[:, 0]) * (np.abs(o[:, 0]) + side)
            # its foot under the ground (2 m into a hillside; a low arch's mouth is flat: just under its floor, or the
            # face's bottom stood as a dark wedge beside the opening)
            if low:     # a flat top over the arch (a parapet), not following its curve
                o[:, 1] = np.where(o[:, 1] > 0.0, th + up, -0.3)
            else:
                o[:, 1] = np.where(o[:, 1] > 0.0, o[:, 1] + up, -2.0)
            outer = np.empty((m, 3))
            outer[:, :2] = mid[at] + u[at] * o[:, :1]
            outer[:, 2] = z[at] + o[:, 1]
            outer -= origin
            P.append(np.concatenate([inner, outer]))
            sc = STONE_UV_M if stone else 4.0
            UV.append(np.column_stack((np.concatenate((sec[:, 0], o[:, 0])) / sc, np.concatenate((sec[:, 1], o[:, 1])) / sc)))
            i = np.arange(m - 1)
            A, A1, B, B1 = nv + i, nv + i + 1, nv + m + i, nv + m + i + 1
            tri = np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)])
            T.append(np.concatenate([tri, tri[:, ::-1]]))   # seen from outside and from in the tube
            M.append(2 if stone else 0)
            nv += 2 * m
            if low and (cap_m is None or cap_m >= LOW_CAP_MIN):
                nv = _arch_cap(P, T, UV, M, nv, mid, u, z, at, k, float(np.max(o[:, 0])), float(o[:, 1].max()), th, origin,
                               LOW_CAP_M if cap_m is None else min(LOW_CAP_M, float(cap_m)), 2 if stone else 0)
    allm = [TUNNEL_SIDE, TUNNEL_LIGHT, TUNNEL_STONE, TUNNEL_STONE_IN, TUNNEL_SOIL, TUNNEL_INNER, WALL_SCREE,
            PORTAL_COLLAR]
    Tall = np.concatenate(T)
    Mall = np.concatenate([np.full(len(t), m, np.int64) for t, m in zip(T, M)]) if M else np.zeros(len(Tall), np.int64)
    used = sorted(set(int(v) for v in np.unique(Mall)) | {0, 1})     # (concrete and lamps first, as before)
    remap = np.zeros(len(allm), np.int64)
    remap[used] = np.arange(len(used))
    return (np.concatenate(P), Tall, np.concatenate(UV), [allm[i] for i in used], remap[Mall])


def _add_blocks(P, T, UV, M, nv, mesh, face_mat, collar_mat=7):
    """A portal kernel's mesh (positions, triangles, uvs, codes: 0 the structure, 1 soil, 2 the collar) appended to
    tunnel()'s lists, the structure in material index face_mat, the collar in collar_mat; returns the new vertex
    count."""
    Pm, Tm, UVm, Cm = mesh
    if not len(Tm):
        return nv
    P.append(np.asarray(Pm, np.float64))
    UV.append(np.asarray(UVm, np.float64))
    Tm, Cm = np.asarray(Tm, np.int64) + nv, np.asarray(Cm, np.int64)
    for code, mat in ((0, face_mat), (1, 4), (2, collar_mat)):
        sel = Cm == code
        if sel.any():
            T.append(Tm[sel])
            M.append(mat)
    return nv + len(Pm)


def _arch_cap(P, T, UV, M, nv, mid, u, z, at, k, side, top, th, origin, depth=None, side_mat=2):
    """A low arch's stone top over its mouth (LOW_CAP_M back from the portal face at row `at`, side either way of
    the centre, `top` over the floor): its top, its sides down to the floor's level (outside the tube's walls) and
    its back down to just over the vault (th), each face one-sided, facing out. Appended to P, T, UV, M; returns the new vertex count."""
    nb = 1 if at == 0 else k - 2
    t = mid[nb] - mid[at]
    t = t / max(float(np.hypot(*t)), 1e-9)              # into the tube
    a = mid[at]
    b = a + t * (LOW_CAP_M if depth is None else depth)
    ua = u[at]
    z0 = float(z[at]) + top - (LOW_PORTAL_UP - ARCH_CAP_OVER)   # the soil's level (the face stands over it)
    corners = np.array([[*(a + ua * side), z0], [*(a - ua * side), z0], [*(b - ua * side), z0], [*(b + ua * side), z0]])
    foot = corners.copy()
    foot[:, 2] = float(z[at])
    over = corners.copy()
    over[:, 2] = float(z[at]) + th + 0.1
    quads = [corners,                                   # the top
             np.array([corners[3], corners[2], over[2], over[3]]),     # its back, toward the soil over the vault
             np.array([corners[0], corners[3], foot[3], foot[0]]),     # its sides
             np.array([corners[1], corners[2], foot[2], foot[1]])]
    # each face one-sided, facing out of the block (up, back into the soil, sideways): the top drawn both ways was two
    # coincident triangles facing opposite ways, its collision pulled a car driving over it down into the block
    # (the I-280 underpass at Millbrae, 2026-11-09; buildings.py: a 3dmr bridge's two-sided lattice caught the car too)
    looks = [np.array([0.0, 0.0, 1.0]), np.r_[t, 0.0], np.r_[ua, 0.0], np.r_[-ua, 0.0]]
    for qi, q in enumerate(quads):
        P.append(q - origin)
        w = float(np.hypot(*(q[1, :2] - q[0, :2]))) or float(abs(q[1, 2] - q[0, 2]))
        h = float(np.hypot(*(q[2, :2] - q[1, :2]))) + float(abs(q[2, 2] - q[1, 2]))
        UV.append(np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float64) / (SOIL_UV_M if qi == 0 else STONE_UV_M))
        tri = np.array([[nv, nv + 1, nv + 2], [nv, nv + 2, nv + 3]], np.int64)
        e = q - q[0]
        nrm = np.cross(e[1], e[2]) + np.cross(e[2], e[3])
        T.append(tri if float(nrm @ looks[qi]) >= 0.0 else tri[:, ::-1])
        M.append(4 if qi == 0 else side_mat)            # its top grass, its sides stone (concrete: side_mat 0)
        nv += 4
    return nv


def _tunnel_lamps(P, T, UV, M, nv, mid, u, z, hw, origin):
    """Lamp fixtures along both walls of a tube (material TUNNEL_LIGHT, index 1): TUNNEL_LAMP_L long every
    TUNNEL_LAMP_EVERY m, just under the springing, faced into the tube; appended to P, T, UV, M; returns the new vertex
    count. (Two dashed strips under the vault read as lane markings on the ceiling: the Caldecott Tunnel on Mapillary
    has fixtures along its walls, 2026-10-18.) Pure but for the lists."""
    k = len(mid)
    if k < 2:
        return nv
    s = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(mid, axis=0).T))))
    h = TUNNEL_WALL_H - TUNNEL_LAMP_DROP
    for sc in np.arange(TUNNEL_LAMP_EVERY / 2, s[-1] - TUNNEL_LAMP_L / 2, TUNNEL_LAMP_EVERY):
        j = min(int(np.searchsorted(s, sc)), k - 1)
        jj = max(j - 1, 0)
        t = mid[j] - mid[jj]
        t = t / max(float(np.hypot(*t)), 1e-9)
        p = np.array([np.interp(sc, s, mid[:, 0]), np.interp(sc, s, mid[:, 1])])
        zc = float(np.interp(sc, s, z))
        uu = u[j]
        for side in (-1.0, 1.0):
            e = p + uu * side * (hw - TUNNEL_LAMP_IN)
            quad = [[*(e + t * a * TUNNEL_LAMP_L / 2), zc + h + b * TUNNEL_LAMP_W / 2] for a in (-1, 1) for b in (-1, 1)]
            q = np.asarray(quad, np.float64) - origin
            P.append(q)
            UV.append(np.array([[0, 0], [0, 1], [1, 0], [1, 1]], np.float64))
            tri = np.array([[nv, nv + 2, nv + 3], [nv, nv + 3, nv + 1]], np.int64)
            nrm = np.cross(q[2] - q[0], q[1] - q[0])
            inward = -side * np.r_[uu, 0.0]
            if float(nrm @ inward) < 0:     # faced into the tube
                tri = tri[:, ::-1]
            T.append(tri)
            M.append(1)
            nv += 4
    return nv


def write(path, mesh, close=True):
    """mesh: build's (positions, tris, uvs, materials, tri material) and, with a dirt or gravel strip in it, its fade
    uv: the second uv, in the vertex format that also carries a colour (white: the strips' materials leave vertColor
    off, and the others in the mesh ignore it or multiply by 1)."""
    P, T, UV, mats, M = mesh[:5]
    uvs2 = mesh[5] if len(mesh) > 5 else None
    colors = None if uvs2 is None else np.full((len(P), 4), 255, np.uint8)
    over = np.array([overlay_material(m) for m in mats], bool) if len(mats) else np.zeros(0, bool)
    open_tris = over[np.asarray(M, np.int64)] if over.any() and M is not None else None
    return cdae.write(path, P, T, uvs=UV, uvs2=uvs2, material=mats, tri_materials=M, object_name="road", open_tris=open_tris,
                      colors=colors, close=close)   # (close "under": a tunnel's tubes, whose mouths are the way in)
