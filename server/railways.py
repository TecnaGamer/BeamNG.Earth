"""Railway tracks: Overture's rail segments (transportation/segment subtype "rail", in the roads layer) as a ballast
bed with concrete sleepers and two steel rails, and their bridges as concrete decks on piers: one colliding mesh per
road tile (write_tile's "rail").

The heights are one profile along the connected track. On the ground the bed follows the drawn ground, smoothed
along its length (a railway does not ride every bump of the lattice). A bridge (the segment's is_bridge flag or level
above 0) is a deck straight between the bed at its ends (deck_heights), raised only where the ground under it would
come through, never steeper than GRADE. Where it is raised, its height reaches out along the track it joins, falling
GRADE per metre: the approach embankments, across segment ends and tile edges alike (the lines are taken from REACH_M
around the tile). The ballast's sides go down past the ground, a low
embankment on the flat and a tall one on a ramp. Where a road crosses at grade, the track stops either side of the
carriageway (a level crossing: the road's own surface across it). Tunnels are left out. Pure given its inputs."""
import numpy as np

import mapgeom
import roads

# the classes drawn (Overture's rail classes); trams lie in the street, subways underground, monorails and funiculars
# on their own structures: none of those
DRAWN = {"standard_gauge", "narrow_gauge", "light_rail", "unknown"}
GAUGE_M = 1.435             # standard gauge between the rails' inner faces ...
FI_GAUGE_M = 1.524          # ... the Finnish (Russian) broad gauge in the Finnish fork
NARROW_GAUGE_M = 1.0
STEP_M = 2.0                # m between cross-sections, from each part's start (the same at every tile)
SMOOTH_M = 10.0             # m either side the bed's height is averaged over: the real elevation, its DEM bumps out
                            # (50 lifted the bed off the ground, the user, 2026-10-25: "follow the real elevation")
GRADE = 0.02                # the steepest a deck or an approach ramp climbs (2 %: a main line's)
REACH_M = 200.0             # m around a tile the lines are taken: the smoothing's reach and a raised deck's ramps
BALLAST_H = 0.3             # m of ballast over the bed line
BALLAST_TOP_M = 1.8         # m: half the ballast's top width
BALLAST_SLOPE = 1.5         # m out per m down on its sides
BALLAST_FOOT_M = 0.4        # m its sides reach under the ground at their foot
FOOT_MAX_M = 6.0            # m out at most (the grading fills a tall ramp's embankment; 15 fanned out, 2026-10-25)
SLEEPER_EVERY = 0.6         # m between sleepers
SLEEPER_L, SLEEPER_W, SLEEPER_H = 2.6, 0.26, 0.2
SLEEPER_SINK = 0.08         # m of sleeper in the ballast
RAIL_HEAD, RAIL_H = 0.07, 0.16
DECK_HALF_M = 2.4           # m: half a bridge deck's width
DECK_D = 1.4                # m of deck under the ballast's top
DECK_WALL_H = 0.7           # m its edge walls stand over the ballast
DECK_WALL_T = 0.3
PIER_EVERY = 25.0           # m between piers (from the deck's start)
PIER_W, PIER_T = 3.6, 1.4   # m across and along the deck
PIER_MIN_H = 1.5            # m: a deck lower than this over the ground stands on its ends alone
PIER_FOOT_M = 2.0           # m a pier reaches into the ground
CROSSING_PAD_M = 1.5        # m past a crossing road's edge the track stops
CROSSING_DZ = 1.5           # m: a road this far over or under the bed passes it by (no level crossing)
VERTS_MAX = 60000           # under the engine's 16-bit indices (sleepers are dropped first past it)
BALLAST = "bne_rail_ballast"
CONCRETE = "bne_bridge_concrete"    # (roadmesh.DECK_SIDE: the decks and piers)
SLEEPER = "bne_rail_sleeper"        # weathered concrete sleepers (bne_bridge_concrete stood out near-white, 2026-10-25)
STEEL = "bne_rail_steel"            # rust-brown rails


def point_ground(frame, elev):
    """ground(xy (n, 2)) -> z in frame at exactly those points, from elev(lat, lon) (the graded physics ground). Not
    roadmesh.drawn_ground: that fills a 2 m grid over the points' whole box, and the lines here span ~1 km (a road
    tile's workers spun on it for minutes, 2026-10-25)."""
    def ground(xy):
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        if not len(xy):
            return np.zeros(0)
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2]
    return ground


GRADE_HALF_M = BALLAST_TOP_M + 1.2     # m: the ground graded flat to the bed line either side of a track (grade_plans)


def cells_near(frame, box, reach=REACH_M):
    """The map cells within reach of box (x0, y0, x1, y1) in frame."""
    import mapcells
    x0, y0, x1, y1 = box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach
    lat, lon, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), 0.0)
    return mapcells.cells_for_bbox(float(np.min(lat)), float(np.min(lon)), float(np.max(lat)), float(np.max(lon)))


def grade_plans(frame, cells, box, elev):
    """[(fid, xy (k, 2), z (k,))]: the bed line of each on-ground rail part near box, in elev's heights (elev(lat, lon):
    the grader's DEM), for grading.Grader to lay the ground to (the cutting's walls back from the track, the ramps'
    embankments filled). tile_mesh then finds the ground at its bed. Pure given elev."""
    rails = parts(frame, cells, box)
    if not rails:
        return []

    def ground(xy):
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return np.asarray(elev(la, lo), np.float64)
    pad = GRADE_HALF_M + 15.0
    out = []
    for (fid, _c, kind, xy), (zb, _zg) in zip(rails, profile(rails, ground)):
        if kind != "ground":
            continue
        if _box_hit((xy[:, 0].min(), xy[:, 1].min(), xy[:, 0].max(), xy[:, 1].max()),
                    (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)):
            out.append((fid, xy, zb))
    return out


def gauge(cls, sign_style=None):
    if cls == "narrow_gauge":
        return NARROW_GAUGE_M
    return FI_GAUGE_M if sign_style == "fi" else GAUGE_M


def _box_hit(b, box):
    return not (b[2] < box[0] or b[0] > box[2] or b[3] < box[1] or b[1] > box[3])


def parts(frame, cells, box, reach=REACH_M):
    """[(fid, class, kind, xy (k, 2))]: the rail parts ("ground" / "bridge"; tunnels left out) of the drawn classes
    within reach of box (x0, y0, x1, y1), resampled every STEP_M from each part's start, each feature once (by id)."""
    near = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    seen, out = set(), []
    for feats in cells:
        if feats is None or len(feats) == 0 or "subtype" not in feats.attrs:
            continue
        sub = feats.attrs["subtype"]
        cls = feats.attrs.get("class")
        fb = feats.boxes(frame)
        for i in range(len(feats)):
            if str(sub[i]) != "rail":
                continue
            c = str(cls[i]) if cls is not None else "unknown"
            fid = str(feats.ids[i])
            if c not in DRAWN or fid in seen or not _box_hit(fb[i], near) or _gone(feats, i):
                continue
            seen.add(fid)
            for kind, rings in _pieces(feats, i):
                for ring in rings:
                    e, n = mapgeom.enu_xy(feats, frame, ring)
                    xy = _resample(np.c_[e, n], STEP_M)
                    if len(xy) >= 2:
                        out.append((fid, c, kind, xy))
    out.sort(key=lambda p: (p[0], p[2], float(p[3][0, 0]), float(p[3][0, 1])))
    return _drop_stubs(out)


MIN_LINE_M = 50.0           # m: connected track shorter than this is left out (a siding's piece cut off by its
                            # abandoned rest, 12 m under a bridge on its own, 2026-10-25)


def _drop_stubs(rails):
    """rails without the connected groups of parts (ends joined within 0.5 m) shorter than MIN_LINE_M in all."""
    if not rails:
        return rails
    nodes, edges, _ = roads._chain_graph([xy for *_, xy in rails])
    parent = list(range(len(nodes)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for a, b, _s in edges:
        parent[find(a)] = find(b)
    total = {}
    for a, _b, s in edges:
        total[find(a)] = total.get(find(a), 0.0) + float(s[-1])
    return [r for r, (a, _b, _s) in zip(rails, edges) if total[find(a)] >= MIN_LINE_M]


GONE_FLAGS = ("is_abandoned", "is_disused")    # lines with no track left (or none in use): not drawn
SHORT_TUNNEL_M = 60.0       # m: a "tunnel" this short is the line passing under a road bridge (OSM tags those
                            # tunnel too): track there (the track stopped either side of an overpass, 2026-10-25)


def _gone(feats, i):
    """Is feature i an abandoned or disused line over most of its length (its flags)?"""
    t = feats.rules.get("flags")
    if t is None or len(t.feature) == 0:
        return False
    return sum(float(t.end[j]) - float(t.start[j]) for j in t.rows(i)
               if any(g in str(t.value[j]) for g in GONE_FLAGS)) > 0.5


def _pieces(feats, i):
    """[("ground" | "bridge", [lon/lat polylines])] of feature i: its level ranges with the short tunnels
    (SHORT_TUNNEL_M) on the ground and the long ones left out."""
    rngs = roads.level_ranges(feats, i)
    if not rngs:
        return [("ground", roads.ground_parts(feats, i))]
    L = roads._feature_length_m(feats, i)
    off = [(a, b, k) for a, b, k in rngs if not (k == "tunnel" and (b - a) * L < SHORT_TUNNEL_M)]
    ground, at = [], 0.0
    for a, b, _k in off:
        if a - at > roads.MIN_PART:
            ground.append((at, a))
        at = max(at, b)
    if 1.0 - at > roads.MIN_PART:
        ground.append((at, 1.0))
    out = {"ground": [], "bridge": []}
    for part in feats.parts(i):
        for ring in part:
            if len(ring) < 2:
                continue
            for kind, spans in (("ground", ground), ("bridge", [(a, b) for a, b, k in off if k == "bridge"])):
                for a, b in spans:
                    if b - a > roads.MIN_PART:
                        r = ring if (a, b) == (0.0, 1.0) else roads.sub_polyline(ring, a, b)
                        if len(r) >= 2:
                            out[kind].append(r)
    return list(out.items())


def crossings(frame, cells, box, reach=REACH_M):
    """[(xy (k, 2), half width)]: the ground roads near box (their carriageways cut the track at grade)."""
    near = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    seen, out = set(), []
    for feats in cells:
        if feats is None or len(feats) == 0:
            continue
        specs = roads.road_specs(feats)
        fb = feats.boxes(frame)
        for i, sp in enumerate(specs):
            fid = str(feats.ids[i])
            if sp is None or fid in seen or not _box_hit(fb[i], near):
                continue
            seen.add(fid)
            for ring in roads.ground_parts(feats, i):
                e, n = mapgeom.enu_xy(feats, frame, ring)
                out.append((np.c_[e, n], sp.width / 2))
    return out


def _resample(xy, step):
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    if s[-1] <= 1e-6:
        return xy[:1]
    t = np.r_[np.arange(0.0, s[-1], step), s[-1]]
    if len(t) > 2 and t[-1] - t[-2] < 0.25 * step:     # (no sliver section at the end)
        t = np.r_[t[:-2], t[-1]]
    return np.c_[np.interp(t, s, xy[:, 0]), np.interp(t, s, xy[:, 1])]


def _arc(xy):
    return np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]


def _left(xy):
    d = np.gradient(xy, axis=0)
    d /= np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)[:, None]
    return np.c_[-d[:, 1], d[:, 0]]


def _avg(v, r):
    """Running mean over 2r + 1 values, the ends extended along their grade (odd reflection: a straight grade stays
    straight to its end, and two parts meeting at a joint meet at its height)."""
    r = min(r, len(v) - 1)
    if r < 1:
        return np.asarray(v, np.float64)
    pad = np.r_[2 * v[0] - v[r:0:-1], v, 2 * v[-1] - v[-2:-r - 2:-1]]
    c = np.r_[0.0, np.cumsum(pad)]
    return (c[2 * r + 1:] - c[:-2 * r - 1]) / (2 * r + 1)


def bed_heights(z):
    """The bed line over ground heights z (every STEP_M): averaged over SMOOTH_M either side, twice (a smooth
    vertical curve), through a ridge and over a dip alike: the grader (grade_plans) cuts and fills the ground to it."""
    z = np.asarray(z, np.float64)
    if len(z) < 3:
        return z
    r = max(1, int(round(SMOOTH_M / STEP_M)))
    return _avg(_avg(z, r), r // 2)


def _envelope(s, req, grade=GRADE):
    """max over k of req[k] - grade * |s - s[k]|: the lowest line over every req that climbs at most grade."""
    out = np.array(req, np.float64)
    for k in range(1, len(s)):                  # forward then backward pass (s ascending)
        out[k] = max(out[k], out[k - 1] - grade * (s[k] - s[k - 1]))
    for k in range(len(s) - 2, -1, -1):
        out[k] = max(out[k], out[k + 1] - grade * (s[k + 1] - s[k]))
    return out


def deck_heights(s, z_ground, z_start, z_end):
    """A bridge's bed line: straight from z_start to z_end (the bed at its ends: a line keeps its grade over what it
    crosses, the stream or road under it goes down, and the bare-earth DEM has that dip), raised only where the ground
    under it stands higher than the track itself (bad data, a ridge), climbing at most GRADE. Pure. (A required 6 m
    clearance stood an 8 m culvert's track 6.8 m up on 4.5 m ramps, 2026-10-25.)"""
    s = np.asarray(s, np.float64)
    L = max(float(s[-1]), 1e-9)
    req = np.maximum(z_start + (z_end - z_start) * s / L, np.asarray(z_ground, np.float64))
    return _envelope(s, req)


def profile(rails, ground):
    """Per part of rails (parts()): the bed line (k,) and the ground under it (k,). ground(xy (n, 2)) -> z."""
    if not rails:
        return []
    allxy = np.concatenate([xy for *_, xy in rails])
    zg_all = np.asarray(ground(allxy), np.float64)
    zgs, k = [], 0
    for *_, xy in rails:
        zgs.append(zg_all[k:k + len(xy)])
        k += len(xy)
    zb = [bed_heights(zg) if kind == "ground" else zg.copy() for (_, _, kind, _), zg in zip(rails, zgs)]
    ends = {}                                   # node -> the ground parts' bed there (a bridge's ends)
    lines = [xy for *_, xy in rails]
    nodes, edges, adj = roads._chain_graph(lines)
    for n, ((_, _, kind, _), (a, b, _)) in enumerate(zip(rails, edges)):
        if kind == "ground":
            ends.setdefault(a, []).append(zb[n][0])
            ends.setdefault(b, []).append(zb[n][-1])
    seeds = {}                                  # node -> the deck's height at it
    for n, ((_, _, kind, xy), (a, b, _)) in enumerate(zip(rails, edges)):
        if kind != "bridge":
            continue
        g = zgs[n]
        za = float(np.mean(ends[a])) if a in ends else float(g[0])
        zb_ = float(np.mean(ends[b])) if b in ends else float(g[-1])
        zb[n] = deck_heights(_arc(xy), g, za, zb_)
        for node, h in ((a, zb[n][0]), (b, zb[n][-1])):
            seeds[node] = max(seeds.get(node, -np.inf), float(h))
    if seeds:                                   # the decks' heights out along the track, GRADE per metre down
        top = max(seeds.values())
        d = roads._dijkstra(adj, {n: (top - h) / GRADE for n, h in seeds.items()})
        for n, (a, b, s) in enumerate(edges):
            reach = np.maximum(top - GRADE * (d[a] + s), top - GRADE * (d[b] + s[-1] - s))
            zb[n] = np.maximum(zb[n], reach)
            if rails[n][2] == "bridge":         # (two decks end to end: still a straight run between their ends)
                zb[n] = _envelope(s, zb[n])
    return list(zip(zb, zgs))


def _blocked(xy, z, crossers):
    """Per point of xy (bed heights z): inside a crossing road's carriageway (+ CROSSING_PAD_M) at grade."""
    out = np.zeros(len(xy), bool)
    for line, hw, lz in crossers:
        if len(line) < 2:
            continue
        a, b = line[:-1], line[1:]
        ab = b - a
        L2 = np.maximum((ab ** 2).sum(1), 1e-12)
        lo, hi = xy.min(0) - hw - 5, xy.max(0) + hw + 5
        keep = ~((np.maximum(a, b) < lo).any(1) | (np.minimum(a, b) > hi).any(1))
        if not keep.any():
            continue
        a, ab, L2, za = a[keep], ab[keep], L2[keep], lz[:-1][keep]
        t = np.clip(((xy[:, None, :] - a[None]) * ab[None]).sum(2) / L2[None], 0.0, 1.0)
        d = np.hypot(*(xy[:, None, :] - (a[None] + t[..., None] * ab[None])).transpose(2, 0, 1))
        j = d.argmin(1)
        # (either way: a road over a cutting on a ground segment, its bridge range short of it, cut the track under it,
        # 2026-10-25)
        out |= (d[np.arange(len(xy)), j] < hw + CROSSING_PAD_M) & (np.abs(z - za[j]) < CROSSING_DZ)
    return out


def _runs(mask):
    """(i, j) inclusive ranges where mask is False, at least 2 long."""
    out, i, n = [], 0, len(mask)
    while i < n:
        if mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and not mask[j + 1]:
            j += 1
        if j > i:
            out.append((i, j))
        i = j + 1
    return out


class _Mesh:
    def __init__(self, origin):
        self.origin = np.asarray(origin, np.float64)
        self.P, self.T, self.UV, self.M = [], [], [], []
        self.mats = []
        self.nv = 0

    def mat(self, name):
        if name not in self.mats:
            self.mats.append(name)
        return self.mats.index(name)

    def strip(self, a, b, ua, ub, v, mi, up):
        """Quads between point rows a and b (k, 3), faces toward up ((3,) or (k, 3))."""
        k = len(a)
        P = np.concatenate([a, b]) - self.origin
        self.P.append(P)
        self.UV.append(np.c_[np.r_[np.full(k, ua), np.full(k, ub)], np.r_[v, v]])
        i = np.arange(k - 1)
        A, A1, B, B1 = i, i + 1, k + i, k + i + 1
        tri = np.concatenate([np.stack([A, B, B1], 1), np.stack([A, B1, A1], 1)])
        nrm = np.cross(P[tri[:, 1]] - P[tri[:, 0]], P[tri[:, 2]] - P[tri[:, 0]])
        want = np.concatenate([up[:-1], up[:-1]]) if np.ndim(up) == 2 else np.broadcast_to(up, nrm.shape)
        flip = (nrm * want).sum(1) < 0
        tri[flip] = tri[flip][:, ::-1]
        self.T.append(tri + self.nv)
        self.M.append(np.full(len(tri), mi))
        self.nv += 2 * k

    def boxes(self, c, ax, ay, hx, hy, z0, z1, mi):
        """Boxes at centres c (n, 2), axes ax / ay (n, 2) unit, half sizes hx / hy, from z0 to z1 (n,): top and four
        sides (no bottom), all at once."""
        n = len(c)
        if n == 0:
            return
        corners = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
        xy = np.stack([c + sx * hx * ax + sy * hy * ay for sx, sy in corners], 1)       # (n, 4, 2)
        lo = np.concatenate([xy, np.repeat(z0[:, None, None], 4, 1)], 2)
        hi = np.concatenate([xy, np.repeat(z1[:, None, None], 4, 1)], 2)
        P = np.concatenate([hi, lo], 1).reshape(-1, 3) - self.origin                  # 8 per box: top 0-3, foot 4-7
        base = (np.arange(n) * 8)[:, None]
        faces = [(0, 1, 2, 3)] + [(q, (q + 1) % 4, (q + 1) % 4 + 4, q + 4) for q in range(4)]
        tri = []
        for f in faces:
            tri.append(np.c_[base + f[0], base + f[1], base + f[2]])
            tri.append(np.c_[base + f[0], base + f[2], base + f[3]])
        tri = np.concatenate(tri)
        ctr = np.concatenate([np.c_[c, (z0 + z1) / 2] - self.origin] * 10)
        nrm = np.cross(P[tri[:, 1]] - P[tri[:, 0]], P[tri[:, 2]] - P[tri[:, 0]])
        flip = (nrm * (P[tri].mean(1) - ctr)).sum(1) < 0
        tri[flip] = tri[flip][:, ::-1]
        self.P.append(P)
        self.UV.append(np.tile(np.array([[0, 0], [1, 0], [1, 1], [0, 1]] * 2, np.float64) * 0.25, (n, 1)))
        self.T.append(tri + self.nv)
        self.M.append(np.full(len(tri), mi))
        self.nv += 8 * n

    def result(self):
        if not self.P:
            return None
        return (np.concatenate(self.P), np.concatenate(self.T), np.concatenate(self.UV), self.mats,
                np.concatenate(self.M))


def _sleepers(s, xy, u, top, i, j):
    """The sleepers of a run (i..j of a part): (centres, along, across, z0, z1), every SLEEPER_EVERY from the part's
    start (the same in the tile beside)."""
    ss = np.arange(np.ceil(s[i] / SLEEPER_EVERY) * SLEEPER_EVERY, s[j], SLEEPER_EVERY)
    if not len(ss):
        return None
    cx, cy = np.interp(ss, s, xy[:, 0]), np.interp(ss, s, xy[:, 1])
    ux, uy = np.interp(ss, s, u[:, 0]), np.interp(ss, s, u[:, 1])
    un = np.c_[ux, uy] / np.maximum(np.hypot(ux, uy), 1e-9)[:, None]
    zt = np.interp(ss, s, top) - SLEEPER_SINK
    return np.c_[cx, cy], np.c_[un[:, 1], -un[:, 0]], un, zt, zt + SLEEPER_H


PIER_ROAD_M = 1.5           # m a pier keeps off a road's carriageway under the bridge


def _sited(pr, crossers, ground):
    """The piers pr (_piers') clear of the roads under the deck (crossers: [(centreline, half width, z)]; a pier stood
    in the lanes of a road under a rail bridge, 2026-10-25), each reaching PIER_FOOT_M under the lowest ground at its
    four corners (from the ground at the line's centre one hung in the air where the ground fell away to the road)."""
    if pr is None:
        return None
    c, along, across, z0, z1 = pr
    keep = np.ones(len(c), bool)
    for line, hw, _z in crossers:
        if len(line) < 2:
            continue
        a, b = line[:-1], line[1:]
        d = b - a
        L2 = np.maximum((d * d).sum(1), 1e-12)
        t = np.clip(((c[:, None, :] - a[None]) * d[None]).sum(2) / L2[None], 0.0, 1.0)
        dist = np.hypot(*(c[:, None, :] - (a[None] + t[..., None] * d[None])).transpose(2, 0, 1)).min(1)
        keep &= dist > hw + PIER_ROAD_M + max(PIER_W, PIER_T) / 2
    if not keep.any():
        return None
    c, along, across, z0, z1 = c[keep], along[keep], across[keep], z0[keep], z1[keep]
    corners = np.concatenate([c + sa * along * PIER_T / 2 + sc * across * PIER_W / 2
                              for sa in (-1, 1) for sc in (-1, 1)])
    zg = np.asarray(ground(corners), np.float64).reshape(4, len(c)).min(axis=0)
    return c, along, across, np.minimum(z0, zg - PIER_FOOT_M), z1


def _ballast_bed(mesh, p, uu, t, v, foot_l, foot_r, zl, zr, mat):
    up = np.array([0.0, 0.0, 1.0])
    out_l = np.c_[uu, np.zeros(len(p))]
    L = np.c_[p + uu * BALLAST_TOP_M, t]
    R = np.c_[p - uu * BALLAST_TOP_M, t]
    Lf = np.c_[p + uu * foot_l[:, None], np.minimum(zl, t) - BALLAST_FOOT_M]
    Rf = np.c_[p - uu * foot_r[:, None], np.minimum(zr, t) - BALLAST_FOOT_M]
    mesh.strip(L, R, 0.0, 2 * BALLAST_TOP_M / 3.0, v, mat, up)
    mesh.strip(L, Lf, 0.0, 0.8, v, mat, out_l + up)
    mesh.strip(R, Rf, 0.0, 0.8, v, mat, -out_l + up)


def _deck(mesh, p, uu, t, v, ballast, concrete):
    """A bridge's deck: ballast on a concrete trough, its edge walls over the ballast, its underside DECK_D down."""
    up = np.array([0.0, 0.0, 1.0])
    out_l = np.c_[uu, np.zeros(len(p))]
    mesh.strip(np.c_[p + uu * (DECK_HALF_M - DECK_WALL_T), t], np.c_[p - uu * (DECK_HALF_M - DECK_WALL_T), t],
               0.0, 2 * DECK_HALF_M / 3.0, v, ballast, up)
    for sg in (1, -1):
        o = out_l * sg
        e_out, e_in = p + uu * sg * DECK_HALF_M, p + uu * sg * (DECK_HALF_M - DECK_WALL_T)
        hi = t + DECK_WALL_H
        mesh.strip(np.c_[e_in, t], np.c_[e_in, hi], 0.0, 0.2, v / 1.5, concrete, -o)
        mesh.strip(np.c_[e_in, hi], np.c_[e_out, hi], 0.0, 0.1, v / 1.5, concrete, up)
        mesh.strip(np.c_[e_out, hi], np.c_[e_out, t - DECK_D], 0.0, 0.6, v / 1.5, concrete, o)
    mesh.strip(np.c_[p + uu * DECK_HALF_M, t - DECK_D], np.c_[p - uu * DECK_HALF_M, t - DECK_D],
               0.0, 1.0, v / 1.5, concrete, -up)


def _piers(s, xy, u, t, zg, i, j):
    """The piers under a deck run: (centres, along, across, z0, z1), every PIER_EVERY from the part's start where the
    deck is PIER_MIN_H or more over the ground, not at its ends (the abutments: the embankment)."""
    ss = np.arange(PIER_EVERY, s[-1] - PIER_EVERY / 2, PIER_EVERY)
    ss = ss[(ss >= s[i]) & (ss <= s[j])]
    if not len(ss):
        return None
    zt = np.interp(ss, s, t) - DECK_D
    zgs = np.interp(ss, s, zg)
    keep = zt - zgs >= PIER_MIN_H
    ss, zt, zgs = ss[keep], zt[keep], zgs[keep]
    if not len(ss):
        return None
    cx, cy = np.interp(ss, s, xy[:, 0]), np.interp(ss, s, xy[:, 1])
    ux, uy = np.interp(ss, s, u[:, 0]), np.interp(ss, s, u[:, 1])
    un = np.c_[ux, uy] / np.maximum(np.hypot(ux, uy), 1e-9)[:, None]
    return np.c_[cx, cy], np.c_[un[:, 1], -un[:, 0]], un, zgs - PIER_FOOT_M, zt


def tile_mesh(frame, tx, ty, cells, ground, sign_style=None):
    """The road tile (tx, ty)'s railway mesh (roadmesh.write's (positions, tris, uvs, materials, tri material)) or
    None. cells: the roads layers of the cells within REACH_M of the tile (cells_of_tile(..., margin=REACH_M));
    ground(xy (n, 2)) -> z: the drawn ground in the frame."""
    box = roads.tile_bbox(tx, ty)
    ex = (box[0] - roads.OVERLAP, box[1] - roads.OVERLAP, box[2] + roads.OVERLAP, box[3] + roads.OVERLAP)
    rails = parts(frame, cells, box)
    if not any(_box_hit((xy[:, 0].min(), xy[:, 1].min(), xy[:, 0].max(), xy[:, 1].max()),
                        (ex[0] - STEP_M, ex[1] - STEP_M, ex[2] + STEP_M, ex[3] + STEP_M)) for *_, xy in rails):
        return None
    prof = profile(rails, ground)
    cr = crossings(frame, cells, box, reach=50.0)
    crossers = []
    if cr:                                          # (one ground call for all of them: its cost is per call)
        zc = np.asarray(ground(np.concatenate([ln for ln, _ in cr])), np.float64)
        k = 0
        for line, hw in cr:
            crossers.append((line, hw, zc[k:k + len(line)]))
            k += len(line)
    mesh = _Mesh((box[0], box[1], 0.0))
    ballast, concrete, steel = mesh.mat(BALLAST), mesh.mat(CONCRETE), mesh.mat(STEEL)
    up = np.array([0.0, 0.0, 1.0])
    small = []                                      # sleepers and piers: drawn last, as room allows
    for (fid, cls, kind, xy), (zb, zg) in zip(rails, prof):
        inside = (xy[:, 0] >= ex[0] - STEP_M) & (xy[:, 0] <= ex[2] + STEP_M) & \
                 (xy[:, 1] >= ex[1] - STEP_M) & (xy[:, 1] <= ex[3] + STEP_M)
        if not inside.any():
            continue
        u = _left(xy)
        s = _arc(xy)
        half_g = gauge(cls, sign_style) / 2 + RAIL_HEAD / 2
        top = zb + BALLAST_H
        if kind == "ground":
            cut = _blocked(xy, zb, crossers) | ~inside
            # the ballast's sides to the ground beside: wider where the bed stands high (a ramp's embankment)
            near = BALLAST_TOP_M + BALLAST_SLOPE * (BALLAST_H + BALLAST_FOOT_M)
            feet = []
            for sg in (1, -1):
                zs = np.asarray(ground(xy + sg * u * near), np.float64)
                foot = np.minimum(BALLAST_TOP_M + BALLAST_SLOPE * (np.maximum(top - zs, 0.0) + BALLAST_FOOT_M),
                                  FOOT_MAX_M)
                far = foot > near + 0.5
                if far.any():
                    zs = zs.copy()
                    zs[far] = np.asarray(ground(xy[far] + sg * u[far] * foot[far, None]), np.float64)
                feet.append((foot, zs))
        else:
            cut = ~inside
        for i, j in _runs(cut):
            sl = slice(i, j + 1)
            p, uu, t, v = xy[sl], u[sl], top[sl], s[sl] / 3.0
            if kind == "ground":
                (fl, zl), (fr, zr) = feet
                _ballast_bed(mesh, p, uu, t, v, fl[sl], fr[sl], zl[sl], zr[sl], ballast)
            else:
                _deck(mesh, p, uu, t, v, ballast, concrete)
                pr = _sited(_piers(s, xy, u, top, zg, i, j), crossers, ground)
                if pr is not None:
                    small.append((pr, PIER_T / 2, PIER_W / 2, concrete))
            st = t - SLEEPER_SINK + SLEEPER_H           # the sleepers' top: the rails' foot
            out_l = np.c_[uu, np.zeros(len(p))]
            for sg in (1, -1):
                c = p + uu * sg * half_g
                a, b = c + uu * RAIL_HEAD / 2, c - uu * RAIL_HEAD / 2
                mesh.strip(np.c_[a, st + RAIL_H], np.c_[b, st + RAIL_H], 0.0, 0.02, v, steel, up)
                mesh.strip(np.c_[a, st], np.c_[a, st + RAIL_H], 0.0, 0.05, v, steel, out_l)
                mesh.strip(np.c_[b, st], np.c_[b, st + RAIL_H], 0.0, 0.05, v, steel, -out_l)
            sp = _sleepers(s, xy, u, top, i, j)
            if sp is not None:
                small.append((sp, SLEEPER_W / 2, SLEEPER_L / 2, mesh.mat(SLEEPER)))
    small.sort(key=lambda e: -e[2])                 # (the piers first: a deck needs them more than its sleepers)
    for (c, along, across, z0, z1), hx, hy, mi in small:
        room = (VERTS_MAX - mesh.nv) // 8
        if room <= 0:
            break
        n = min(room, len(c))
        mesh.boxes(c[:n], along[:n], across[:n], hx, hy, z0[:n], z1[:n], mi)
    out = mesh.result()
    if out is not None and len(out[0]) > VERTS_MAX:     # (the ballast and rails alone past it: a rail yard)
        return None
    return out
