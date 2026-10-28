"""Railway tracks: Overture's rail segments (transportation/segment subtype "rail", in the roads layer) as a ballast
bed with concrete sleepers and two steel rails, and their bridges as concrete decks on piers: one colliding mesh per
road tile (write_tile's "rail").

The heights are one profile along the connected track. On the ground the bed follows the drawn ground, smoothed
along its length (a railway does not ride every bump of the lattice). A bridge (the segment's is_bridge flag or level
above 0) is a deck straight between the bed at its ends (deck_heights), raised only where the ground under it would
come through, never steeper than GRADE. Where it is raised, its height reaches out along the track it joins, falling
GRADE per metre: the approach embankments, across segment ends and tile edges alike (the lines are taken from REACH_M
around the tile). The ballast's sides go down past the ground, a low
embankment on the flat and a tall one on a ramp. Short mapped road tunnels also create supported spans when the upper rail lacks a bridge tag.
Where a road crosses at grade, the track stops either side of the
carriageway (a level crossing: the road's own surface across it). Tunnels are left out. Pure given its inputs."""
import math
import re

import numpy as np

import mapgeom
import roads

# the classes drawn (Overture's rail classes); trams lie in the street, subways underground, monorails and funiculars
# on their own structures: none of those. Nor "unknown": OpenStreetMap's railway=proposed / construction (99 of 101
# such ways not flagged abandoned or disused, in 3000 cached cells, 2026-10-27; the rest 2 trams and a miniature
# line): Los Angeles' planned Link Union Station tracks lay across US-101 on the ground and the freeway was dipped
# under a planned high-speed line's 2.6 km "viaduct", a 38 % step at Union Station
DRAWN = {"standard_gauge", "narrow_gauge", "light_rail"}
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
CROSSING_PAD_M = 0.5        # m past a crossing road's edge the track stops (rails.py's street track runs across the road
                            # from 0.3 m inside its edge: 1.5 left ~1.8 m of bare ground either side)
CROSSING_DZ = 1.5           # m: a road this far over or under the bed passes it by (no level crossing)
VERTS_MAX = 60000           # under the engine's 16-bit indices (sleepers are dropped first past it)
BALLAST = "bne_rail_ballast"
CONCRETE = "bne_bridge_concrete"    # (roadmesh.DECK_SIDE: the decks and piers)
SLEEPER = "bne_rail_sleeper"        # weathered concrete sleepers (bne_bridge_concrete stood out near-white, 2026-10-25)
STEEL = "bne_rail_steel"            # rust-brown rails
# per wear (wear()): a disused line's ballast weedy, an abandoned one's grown over, their rails and sleepers older, and
# a share of an abandoned line's sleepers gone (SLEEPERS_GONE)
BALLAST_WORN = (BALLAST, "bne_rail_ballast_weedy", "bne_rail_ballast_grown")
STEEL_WORN = (STEEL, "bne_rail_steel_old", "bne_rail_steel_old")
SLEEPER_WORN = (SLEEPER, "bne_rail_sleeper_old", "bne_rail_sleeper_old")
SLEEPERS_GONE = (0.0, 0.0, 0.3)


_GROUND_MEMO = {}            # anchor key -> {point (cm): z}: point_ground's heights, shared by the tiles of an anchor
GROUND_MEMO_MAX = 400000     # points per anchor kept (then forgotten, and asked again)
GROUND_MEMO_ANCHORS = 2


def point_ground(frame, elev, key=None):
    """ground(xy (n, 2)) -> z in frame at exactly those points, from elev(lat, lon) (the graded physics ground). Not
    roadmesh.drawn_ground: that fills a 2 m grid over the points' whole box, and the lines here span ~1 km (a road
    tile's workers spun on it for minutes, 2026-10-25). key (the anchor's): heights remembered per point. Every tile
    within REACH_M of a line asks for the same points (each part resampled from its start), ~6 tiles each, and the
    graded ground's lookups were most of a rail tile's time (16 tiles round Baltimore Penn Station: 28 of 68 s)."""
    memo = None
    if key is not None:
        memo = _GROUND_MEMO.get(key)
        if memo is None:
            while len(_GROUND_MEMO) >= GROUND_MEMO_ANCHORS:
                _GROUND_MEMO.pop(next(iter(_GROUND_MEMO)))
            memo = _GROUND_MEMO[key] = {}

    def look(xy):
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2], np.float64)

    def ground(xy):
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        if not len(xy):
            return np.zeros(0)
        if memo is None:
            return look(xy)
        keys = list(map(tuple, np.round(xy * 100.0).astype(np.int64).tolist()))
        out = np.array([memo.get(k, np.nan) for k in keys], np.float64)
        miss = np.flatnonzero(np.isnan(out))
        if len(miss):
            z = look(xy[miss])
            out[miss] = z
            if len(memo) + len(miss) > GROUND_MEMO_MAX:
                memo.clear()
            for n, zz in zip(miss.tolist(), z.tolist()):
                memo[keys[n]] = zz
        return out
    return ground


GRADE_HALF_M = BALLAST_TOP_M + 1.2     # m: the ground graded flat to the bed line either side of a track (grade_plans)
GRADE_REACH_M = 60.0        # m round a grading cell the lines are taken for its bed plans (the track's smoothing and the
                            # ramps near it: REACH_M's 200 m had every cell profile ~1 km of line, 8.5 of 13 s at the A11)


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
    cells = list(cells)
    tunnels = road_underpasses(frame, cells, box, GRADE_REACH_M)
    rails = parts(frame, cells, box, reach=GRADE_REACH_M, clip=True)
    if not rails:
        return []
    memo_key = ("dem", round(float(getattr(frame, "lat0", 0.0)), 7), round(float(getattr(frame, "lon0", 0.0)), 7))
    memo = _GROUND_MEMO.setdefault(memo_key, {})

    def ground(xy):
        # (the DEM's heights, remembered per point: every cell's plan asks for the same lines' points)
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        keys = list(map(tuple, np.round(xy * 100.0).astype(np.int64).tolist()))
        out = np.array([memo.get(k, np.nan) for k in keys], np.float64)
        miss = np.flatnonzero(np.isnan(out))
        if len(miss):
            la, lo, _ = frame.enu_to_geodetic(xy[miss, 0], xy[miss, 1], 0.0)
            z = np.asarray(elev(la, lo), np.float64)
            out[miss] = z
            if len(memo) + len(miss) > GROUND_MEMO_MAX:
                memo.clear()
            for n, zz in zip(miss.tolist(), z.tolist()):
                memo[keys[n]] = zz
        return out
    pad = GRADE_HALF_M + 15.0
    out = []
    for (fid, _c, kind, xy), (zb, _zg) in zip(rails, profile(rails, ground, tunnels)):
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


def parts(frame, cells, box, reach=REACH_M, clip=False, worn=None, gauges=None):
    """[(fid, class, kind, xy (k, 2))]: the rail parts ("ground" / "bridge"; tunnels left out) of the lines drawn
    (drawn(); worn: a dict given here gets {fid: wear} of the worn ones, gauges {fid: m} OSM's gauge where tagged)
    within reach of box (x0, y0, x1, y1), resampled every STEP_M from each part's start, each feature once (by id).
    Each part must intersect that window: a feature's bbox can cover far-away bridges or a long bend around it.
    clip: the ground parts cut to their points within reach of box (a segment of line runs on for km, and every one of
    its points was profiled for each cell and tile: _clip); the bridges stay whole (a deck runs between its ends)."""
    cells = list(cells)
    near = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    seen, out = set(), []
    for feats in cells:
        if feats is None or len(feats) == 0 or "subtype" not in feats.attrs:
            continue
        sub = feats.attrs["subtype"]
        fb = feats.boxes(frame)
        for i in range(len(feats)):
            if str(sub[i]) != "rail":
                continue
            fid = str(feats.ids[i])
            if fid in seen or not _box_hit(fb[i], near):
                continue
            got = drawn(feats, i)
            if got is None:
                continue
            c = got[0]
            seen.add(fid)
            if worn is not None and got[1]:
                worn[fid] = got[1]
            if gauges is not None:
                g = osm_gauge(feats, i)
                if g:
                    gauges[fid] = g
            for kind, rings in _pieces(feats, i):
                for ring in rings:
                    e, n = mapgeom.enu_xy(feats, frame, ring)
                    xy = np.c_[e, n]
                    if not _box_hit((xy[:, 0].min(), xy[:, 1].min(), xy[:, 0].max(), xy[:, 1].max()), near):
                        continue
                    if not roads.clip_polyline(xy, *near):
                        continue
                    # Test relevance before resampling, but keep the original part: a nearby bridge needs both
                    # ends, and the ground samples must keep their alignment from the part's original start.
                    xy = _resample(xy, STEP_M)
                    if len(xy) >= 2:
                        out.append((fid, c, kind, xy))
    out.sort(key=lambda p: (p[0], p[2], float(p[3][0, 0]), float(p[3][0, 1])))
    out = _drop_stubs(out)
    out = _span_underpasses(out, road_underpasses(frame, cells, box, reach))
    return _clip(out, near) if clip else out



# A short road tunnel may be tagged only on the lower road, with no bridge flag on
# the railway above. Its ballast and grading must not fill the lower carriageway.
UNDERPASS_CLEAR_M = 3.6
UNDERPASS_SIDE_M = 1.0
UNDERPASS_MIN_SIN = 0.25    # conservatively exclude near-parallel tracks beside a tunnel


def road_underpasses(frame, cells, box, reach=REACH_M):
    """Short mapped road tunnels near box as (xy, half width), each part once."""
    near = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    out, seen = [], set()
    for feats in cells:
        if feats is None or not len(feats):
            continue
        specs = roads.tunnel_specs(feats)
        boxes = feats.boxes(frame)
        for i, sp in enumerate(specs):
            if sp is None or not _box_hit(boxes[i], near):
                continue
            for ring in roads.tunnel_parts(feats, i):
                e, n = mapgeom.enu_xy(feats, frame, ring)
                xy = np.c_[e, n]
                if len(xy) < 2 or _arc(xy)[-1] > roads.UNDERPASS_MAX_M:
                    continue
                key = (str(feats.ids[i]), tuple(np.round(xy[[0, -1]].ravel(), 2)))
                if key not in seen:
                    seen.add(key)
                    out.append((xy, sp.width / 2 + UNDERPASS_SIDE_M))
    return out


def _underpass_distance(xy, line):
    """Distance to a finite tunnel centreline, and fraction along it, per point."""
    s = _arc(line)
    dist = np.full(len(xy), np.inf)
    along = np.zeros(len(xy))
    for j, (a, b) in enumerate(zip(line[:-1], line[1:])):
        d = b - a
        length = float(np.hypot(*d))
        if length < 1e-6:
            continue
        t = np.clip((xy - a) @ d / (length * length), 0.0, 1.0)
        dd = np.linalg.norm(xy - (a + t[:, None] * d), axis=1)
        take = dd < dist
        dist[take] = dd[take]
        along[take] = (s[j] + t[take] * length) / max(s[-1], 1e-6)
    return dist, along


def _underpass_masks(rails, line, half, margin):
    """Points near a tunnel, connected locally to a track that actually crosses it.

    A crossing on one track must not qualify a neighbouring track. Connect only
    the runs inside the tunnel's padded footprint: this preserves source-feature
    splits without following a turnout or a returning track far outside it.
    """
    masks = [np.zeros(len(xy), bool) for *_, xy in rails]
    candidates, runs, owners = [], [], []
    pad = half + margin
    near = (*(line.min(0) - pad), *(line.max(0) + pad))
    direction = line[-1] - line[0]
    for n, (*_, xy) in enumerate(rails):
        covered = np.zeros(len(xy), bool)
        if _box_hit((*xy.min(0), *xy.max(0)), near):
            tangent = np.gradient(xy, axis=0)
            cross = np.abs(tangent[:, 0] * direction[1] - tangent[:, 1] * direction[0])
            cross /= np.maximum(np.linalg.norm(tangent, axis=1) * np.linalg.norm(direction), 1e-9)
            distance, _ = _underpass_distance(xy, line)
            covered = (distance <= pad) & (cross > UNDERPASS_MIN_SIN)
        candidates.append(covered)
        spans = covered[:-1] | covered[1:]
        cuts = np.flatnonzero(np.diff(np.r_[False, spans, False]))
        for a, b in zip(cuts[::2], cuts[1::2]):
            runs.append(xy[a:b + 1])
            owners.append((n, a, b))
    crossings = roads._crossings(runs, [line])
    if not crossings:
        return masks
    _nodes, edges, adj = roads._chain_graph(runs)
    connected = {node for n, _s, _xy in crossings for node in edges[n][:2]}
    pending = list(connected)
    while pending:
        for node, _length in adj[pending.pop()]:
            if node not in connected:
                connected.add(node)
                pending.append(node)
    for (n, a, b), (start, _end, _s) in zip(owners, edges):
        if start in connected:
            masks[n][a:b + 1] = candidates[n][a:b + 1]
    return masks


def _span_underpasses(rails, tunnels):
    """Replace the ground track over mapped road tunnels with supported spans.

    Keep the original sample lattice and shared endpoints, including across source
    feature and tile boundaries. The margin keeps the approach ballast's feet out
    of the carriageway as well as the bridge itself.
    """
    if not tunnels:
        return rails
    covered = [np.zeros(len(xy), bool) for *_, xy in rails]
    for line, half in tunnels:
        for keep, mask in zip(covered, _underpass_masks(rails, line, half, FOOT_MAX_M + STEP_M)):
            keep |= mask
    out = []
    for (fid, cls, kind, xy), mask in zip(rails, covered):
        if kind != "ground":
            out.append((fid, cls, kind, xy))
            continue
        spans = mask[:-1] | mask[1:]
        cuts = np.r_[0, np.flatnonzero(spans[1:] != spans[:-1]) + 1, len(spans)]
        for a, b in zip(cuts[:-1], cuts[1:]):
            out.append((fid, cls, "bridge" if spans[a] else kind, xy[a:b + 1]))
    return out


def _clip(rails, near):
    """rails with each ground part cut to its runs of points inside near (x0, y0, x1, y1), one STEP_M more each side
    (the same sample points as the whole part: they lie every STEP_M from its start). Pure."""
    out = []
    for fid, c, kind, xy in rails:
        if kind != "ground":
            out.append((fid, c, kind, xy))
            continue
        inside = (xy[:, 0] >= near[0]) & (xy[:, 0] <= near[2]) & (xy[:, 1] >= near[1]) & (xy[:, 1] <= near[3])
        if inside.all():
            out.append((fid, c, kind, xy))
            continue
        grow = inside.copy()
        grow[1:] |= inside[:-1]
        grow[:-1] |= inside[1:]
        idx = np.flatnonzero(grow)
        if not len(idx):
            continue
        for run in np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1):
            if len(run) >= 2:
                out.append((fid, c, kind, xy[run[0]:run[-1] + 1]))
    return out


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


WEAR_FLAGS = (("is_abandoned", 2), ("is_disused", 1))    # a line's wear: 1 out of use, 2 abandoned (drawn worn: the
# user, 2026-10-27, "id still like abandond lines to show. perhapse with some grass on them or sumilate wear")
UNBUILT_FLAGS = ("is_under_construction",)              # not drawn (no track yet), as OSM's proposed lines
SHORT_TUNNEL_M = 60.0       # m: a "tunnel" this short is the line passing under a road bridge (OSM tags those
                            # tunnel too): track there (the track stopped either side of an overpass, 2026-10-25)


def wear(feats, i):
    """Feature i's wear by its flags over most of its length: 0 in use, 1 disused, 2 abandoned; None not built yet."""
    t = feats.rules.get("flags")
    if t is None or len(t.feature) == 0:
        return 0
    rows = [(float(t.end[j]) - float(t.start[j]), str(t.value[j])) for j in t.rows(i)]
    if sum(L for L, v in rows if any(g in v for g in UNBUILT_FLAGS)) > 0.5:
        return None
    for flag, w in WEAR_FLAGS:
        if sum(L for L, v in rows if flag in v) > 0.5:
            return w
    return 0


def drawn(feats, i):
    """(class, wear) of feature i drawn as a railway track, or None: a rail segment of the DRAWN classes, or an
    "unknown" one flagged abandoned or disused (an old line, as standard gauge); not one under construction, nor an
    "unknown" one that is neither (OpenStreetMap's proposed lines: DRAWN)."""
    sub = feats.attrs.get("subtype")
    if sub is None or str(sub[i]) != "rail":
        return None
    cls = feats.attrs.get("class")
    c = str(cls[i]) if cls is not None else "standard_gauge"
    osm = feats.attrs.get("osm_railway")
    if osm is not None and osm[i]:          # (OSM's own railway value: railtags.py, the weekly rail file)
        v = str(osm[i])
        if v in OSM_RAILWAY_WEAR:
            w = OSM_RAILWAY_WEAR[v]
            return (c if c in DRAWN else "standard_gauge", w)
        if v in OSM_RAILWAY_NOT:
            return None
    w = wear(feats, i)
    if w is None:
        return None
    if c == "unknown" and w:
        c = "standard_gauge"
    return (c, w) if c in DRAWN else None


# OpenStreetMap's railway values (railtags.py), when the weekly file has the line: drawn with this wear, or not at all
OSM_RAILWAY_WEAR = {"rail": 0, "light_rail": 0, "narrow_gauge": 0, "preserved": 0, "disused": 1, "abandoned": 2}
OSM_RAILWAY_NOT = {"proposed", "construction", "razed", "dismantled", "planned", "subway", "tram", "monorail",
                   "funicular", "miniature", "platform"}


def osm_gauge(feats, i):
    """m between the rails by OSM's gauge tag (mm, the first of several), or None."""
    g = feats.attrs.get("osm_gauge")
    if g is None or not g[i]:
        return None
    m = re.match(r"\s*(\d{3,4})", str(g[i]))
    if not m:
        return None
    mm = int(m.group(1))
    return mm / 1000.0 if 381 <= mm <= 1676 else None


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


def profile(rails, ground, underpasses=()):
    """Per part: bed and ground heights. Underpasses reserve clearance above their lower road mouths.
    ground(xy (n, 2)) -> z. The resulting deck heights propagate along connected approach ramps.
    """
    if not rails:
        return []
    allxy = np.concatenate([xy for *_, xy in rails])
    zg_all = np.asarray(ground(allxy), np.float64)
    zgs, k = [], 0
    for *_, xy in rails:
        zgs.append(zg_all[k:k + len(xy)])
        k += len(xy)
    zb = [bed_heights(zg) if kind == "ground" else zg.copy() for (_, _, kind, _), zg in zip(rails, zgs)]
    # Read the lower road at its mouths, not on the filled railway bed above it.
    crossings = [(line, half, _underpass_masks(rails, line, half, DECK_HALF_M + STEP_M))
                 for line, half in underpasses]
    crossings = [(line, half, masks) for line, half, masks in crossings if any(m.any() for m in masks)]
    mouths = np.concatenate([ln[[0, -1]] for ln, _hw, _masks in crossings]) if crossings else np.empty((0, 2))
    mouth_z = np.asarray(ground(mouths), np.float64).reshape(-1, 2) if len(mouths) else []
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
        for (line, _half, masks), (z0, z1) in zip(crossings, mouth_z):
            over = masks[n]
            if over.any():
                _distance, t = _underpass_distance(xy, line)
                req = zb[n].copy()
                req[over] = np.maximum(req[over], z0 + (z1 - z0) * t[over]
                                       + UNDERPASS_CLEAR_M + DECK_D - BALLAST_H)
                zb[n] = _envelope(_arc(xy), req)
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


RAIL_TOP_M = BALLAST_H - SLEEPER_SINK + SLEEPER_H + RAIL_H    # the rails' top over the bed
FLUSH_LIFT_M = 0.03         # m: the road's surface over the drawn ground (grading.ROAD_LIFT, a little under)
FLUSH_GRADE = 1.0 / 60.0    # the track's ramp to and from a level crossing (a main line's 1.7 %) ...
FLUSH_REACH_M = 40.0        # ... steeper when it would run longer than this (inside the 50 m the tiles look for roads)


def _flush(s, zb, blk, zroad):
    """Bed heights zb (at arcs s) lowered or raised so the rails' top meets the road at a level crossing (blk: the
    points in a road's carriageway, zroad: the drawn ground there), on straight ramps (FLUSH_GRADE, at most
    FLUSH_REACH_M long) either side: a real crossing's rails lie in the road's surface (the user, 2026-10-27: "make
    road/rail crossings smooth like real ones"); they stood 0.6 m over the road at its edge. The same in every tile:
    every crossing within FLUSH_REACH_M of a tile's points is among its crossers."""
    sb = s[blk]
    target = zroad + FLUSH_LIFT_M - RAIL_TOP_M
    delta = target - zb[blk]
    g = np.maximum(FLUSH_GRADE, np.abs(delta) / FLUSH_REACH_M)
    d = np.abs(s[:, None] - sb[None, :]) * g[None, :]
    low = np.min(np.where(delta[None, :] < 0, target[None, :] + d, np.inf), axis=1)
    high = np.max(np.where(delta[None, :] > 0, target[None, :] - d, -np.inf), axis=1)
    return np.minimum(np.maximum(zb, high), low)


def _blocked(xy, z, crossers):
    """Per point of xy (bed heights z): inside a crossing road's carriageway (+ CROSSING_PAD_M) at grade: its nearest
    segment's start within CROSSING_DZ of the bed (earthfast.near_crossers)."""
    import fast
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    pack = _crosser_pack(crossers)
    if pack is None or not len(xy):
        return np.zeros(len(xy), bool)
    fast.require(74, "near_crossers")
    return np.asarray(fast._ef.near_crossers(np.ascontiguousarray(xy), np.ascontiguousarray(np.asarray(z, np.float64)),
                                             *pack, float(CROSSING_DZ)), bool)


_CROSSER_PACK = [(None, None)]  # [(the crossers list, its packed arrays)]: a tile's parts ask with the same list


def _crosser_pack(crossers):
    """(points (n, 3) [x, y, road z], starts (C + 1,), reach (C,)) of the crossing roads with 2 points or more."""
    if not crossers:
        return None
    last = _CROSSER_PACK[0]
    if last[0] is crossers:
        return last[1]
    keep = [(np.asarray(line, np.float64)[:, :2], float(hw), np.asarray(lz, np.float64))
            for line, hw, lz in crossers if len(line) >= 2]
    pack = None
    if keep:
        pts = np.ascontiguousarray(np.concatenate([np.c_[ln, lz[:len(ln)]] for ln, _hw, lz in keep]))
        starts = np.r_[0, np.cumsum([len(ln) for ln, _hw, _lz in keep])].astype(np.int64)
        reach = np.array([hw + CROSSING_PAD_M for _ln, hw, _lz in keep], np.float64)
        pack = (pts, starts, reach)
    _CROSSER_PACK[0] = (crossers, pack)
    return pack


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


def _sleepers(s, xy, u, top, i, j, gone=0.0):
    """The sleepers of a run (i..j of a part): (centres, along, across, z0, z1), every SLEEPER_EVERY from the part's
    start (the same in the tile beside); gone: the share of them left out (by a hash of their number: the same ones in
    every tile)."""
    ss = np.arange(np.ceil(s[i] / SLEEPER_EVERY) * SLEEPER_EVERY, s[j], SLEEPER_EVERY)
    if gone > 0 and len(ss):
        k = np.rint(ss / SLEEPER_EVERY).astype(np.uint64)
        h = ((k * np.uint64(2654435761)) % np.uint64(1 << 32)).astype(np.float64) / float(1 << 32)
        ss = ss[h >= gone]
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


GRADED_NEAR_M = 40.0        # m around the tile where the track's heights read the graded ground (tile_mesh far_ground)


def _mixed(ground, far_ground, box):
    """ground(xy) within GRADED_NEAR_M of box, far_ground(xy) beyond it."""
    if far_ground is None:
        return ground
    lo = np.array([box[0] - GRADED_NEAR_M, box[1] - GRADED_NEAR_M])
    hi = np.array([box[2] + GRADED_NEAR_M, box[3] + GRADED_NEAR_M])

    def mixed(xy):
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        near = ((xy >= lo) & (xy <= hi)).all(1)
        out = np.empty(len(xy))
        if near.any():
            out[near] = ground(xy[near])
        if (~near).any():
            out[~near] = far_ground(xy[~near])
        return out
    return mixed


# Level crossings' equipment (crossing_props; the models: tools/build_rail_crossing_props.py, trees.lua's rr* groups):
# per road class, a gate (its arm raised), a mast with lamps or a cross on its post, at each approach on the kerb side
# of the traffic, facing it. A disused line keeps its crosses, an abandoned one has none.
CROSSING_KIT = {"motorway": "gate", "trunk": "gate", "primary": "gate", "secondary": "gate", "tertiary": "flash"}
CROSSING_KIT_DEFAULT = "cross"
CROSSING_NO_KIT = {"footway", "cycleway", "path", "pedestrian", "steps", "track", "bridleway", "corridor"}
CROSSING_SIGN_OFF_M = 1.2       # m from the carriageway's edge to the post
CROSSING_SIGN_BEFORE_M = 2.5    # m before the track's ballast (along the road)
CROSSING_HEIGHT = {"cross": 3.263, "flash": 4.7, "gate": 8.25}
AMERICAS_LON = -30.0            # west of this the crossbuck (the Americas), else the St Andrew's cross
# light rail mostly runs in the street (a German Stadtbahn's tracks down Bielefeld's Schildescher Strasse had crosses
# at every side street, 2026-10-27): its crossings have devices only where the FRA's inventory or OSM's tags say
CROSSING_DATA_ONLY = {"light_rail"}


def crossing_props(frame, tx, ty, cells, ground, left_hand=False, kits=None):
    """Prop rows ([group, x, y, z, yaw, scale, height, 0]) of the level crossings in road tile (tx, ty): where a drawn
    line's ground part crosses a road's (cells: the road layers around the tile; ground(xy (n, 2)) -> z). kits: an
    optional fn(xy (2,), road class) -> "gate" / "flash" / "cross" / None from crossing data (the FRA's inventory in
    the US), else CROSSING_KIT by the road's class. Each crossing is the tile's that holds its point."""
    import roads as roadsmod
    box = roadsmod.tile_bbox(tx, ty)
    reach = 60.0
    worn = {}
    rails = [(fid, xy, c) for fid, c, kind, xy in parts(frame, cells, box, reach=reach, worn=worn) if kind == "ground"]
    if not rails:
        return []
    near = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    roads, seen = [], set()
    for feats in cells:
        if feats is None or not len(feats):
            continue
        specs = roadsmod.road_specs(feats)
        fb = feats.boxes(frame)
        for i, sp in enumerate(specs):
            fid = str(feats.ids[i])
            if sp is None or fid in seen or sp.cls in CROSSING_NO_KIT or not _box_hit(fb[i], near):
                continue
            seen.add(fid)
            for ring in roadsmod.ground_parts(feats, i):
                e, n = mapgeom.enu_xy(feats, frame, ring)
                if len(e) >= 2:
                    roads.append((fid, sp, np.c_[e, n]))
    if not roads:
        return []
    americas = float(frame.lon0 if hasattr(frame, "lon0") else 0.0) < AMERICAS_LON
    osm_kit = _osm_kits(frame, cells)
    rows, done = [], set()
    for (rfid, sp, line), crs in zip(roads, roadsmod._crossings_by([xy for _f, xy, _c in rails],
                                                                     [ln for *_, ln in roads])):
        for ri, _arc_r, pt in crs:
            pt = np.asarray(pt, np.float64)
            if not (box[0] <= pt[0] < box[2] and box[1] <= pt[1] < box[3]):
                continue
            lfid, rxy, rcls = rails[ri]
            w = worn.get(lfid, 0)
            key = (rfid, lfid, round(float(pt[0]), 1), round(float(pt[1]), 1))
            if w >= 2 or key in done:
                continue
            done.add(key)
            kit = kits(pt, sp.cls) if kits is not None else None
            kit = kit or osm_kit(pt)
            if kit is None and rcls in CROSSING_DATA_ONLY:
                continue                # (a tram-like line in the street: only where the data says it has devices)
            kit = kit or CROSSING_KIT.get(sp.cls, CROSSING_KIT_DEFAULT)
            if w:
                kit = "cross"
            rows += _crossing_rows(pt, line, rxy, sp, kit, americas, left_hand, ground)
    return rows


OSM_KIT_M = 15.0        # m between a crossing and OSM's level crossing node it takes the devices of


def osm_kit_of(t):
    """"gate" / "flash" / "cross" / None of a level crossing node's OSM tags (crossing:barrier, crossing:light ...)."""
    barrier, light = str(t.get("crossing:barrier") or ""), str(t.get("crossing:light") or "")
    if barrier and barrier != "no":
        return "gate"
    if light and light != "no":
        return "flash"
    if barrier == "no" and light == "no":
        return "cross"
    return None             # (no word on its devices: the road class's kit)


def _osm_kits(frame, cells):
    """fn(xy) -> the kit of OSM's nearest level crossing node within OSM_KIT_M (railtags' nodes of the cells), or
    None."""
    pts = [(lo, la, t) for f in cells if f is not None for lo, la, t in f.__dict__.get("_rail_nodes", ())
           if t.get("railway") == "level_crossing"]
    if not pts:
        return lambda p: None
    a = np.array([[la, lo] for lo, la, _t in pts], np.float64)
    e, n, _ = frame.geodetic_to_enu(a[:, 0], a[:, 1], np.zeros(len(a)))
    xy = np.stack([np.asarray(e), np.asarray(n)], -1)
    kit = [osm_kit_of(t) for _lo, _la, t in pts]

    def look(p):
        d = np.hypot(*(xy - np.asarray(p)[:2]).T)
        j = int(np.argmin(d))
        return kit[j] if d[j] <= OSM_KIT_M else None
    return look


def _unit_at(line, pt):
    """The unit direction of polyline line at its segment nearest pt."""
    a, b = line[:-1], line[1:]
    d = b - a
    t = np.clip(((pt - a) * d).sum(1) / np.maximum((d * d).sum(1), 1e-12), 0.0, 1.0)
    j = int(np.argmin(np.hypot(*(a + t[:, None] * d - pt).T)))
    return d[j] / max(float(np.hypot(*d[j])), 1e-9)


def _crossing_rows(pt, line, rxy, sp, kit, americas, left_hand, ground):
    """The rows of one crossing: a kit at each approach (one on a one-way road), on its kerb side, facing it."""
    r = _unit_at(line, pt)
    t = _unit_at(rxy, pt)
    sin = abs(float(r[0] * t[1] - r[1] * t[0]))
    before = (BALLAST_TOP_M + BALLAST_SLOPE * BALLAST_H + CROSSING_SIGN_BEFORE_M) / max(sin, 0.35)
    off = sp.width / 2 + CROSSING_SIGN_OFF_M
    group = {"cross": "rrcross", "flash": "rrflash", "gate": "rrgate"}[kit] + ("" if americas else "_eu")
    if kit == "gate" and left_hand:
        group += "_l"
    dirs = (1.0, -1.0) if not sp.oneway else (-float(sp.dir or 1),)   # (sgn -1: travel along the geometry)
    out, pts = [], []
    for sgn in dirs:
        a = -r * sgn                                    # the travel direction towards the crossing
        side = np.array([a[1], -a[0]])                  # its right
        kerb = -side if left_hand else side
        q = pt - a * before + kerb * off
        pts.append((q, side))
    z = np.asarray(ground(np.array([q for q, _ in pts])), np.float64).ravel()
    for (q, side), zz in zip(pts, z):
        out.append([group, round(float(q[0]), 2), round(float(q[1]), 2), round(float(zz), 2),
                    round(math.degrees(math.atan2(side[1], side[0])) % 360.0, 1), 1.0, CROSSING_HEIGHT[kit], 0.0])
    return out


def tile_mesh(frame, tx, ty, cells, ground, sign_style=None, far_ground=None):
    """The road tile (tx, ty)'s railway mesh (roadmesh.write's (positions, tris, uvs, materials, tri material)) or
    None. cells: the roads layers of the cells within REACH_M of the tile (cells_of_tile(..., margin=REACH_M));
    ground(xy (n, 2)) -> z: the drawn ground in the frame. far_ground: the natural ground, read beyond GRADED_NEAR_M
    of the tile (the lines REACH_M round it only smooth the track and carry its ramps: the graded ground there made the
    grader plan the cells round every tile, and the railways doubled a tile's time, 2026-10-25)."""
    cells = list(cells)
    box = roads.tile_bbox(tx, ty)
    tunnels = road_underpasses(frame, cells, box)
    ground = _mixed(ground, far_ground, box)
    ex = (box[0] - roads.OVERLAP, box[1] - roads.OVERLAP, box[2] + roads.OVERLAP, box[3] + roads.OVERLAP)
    worn, gauges = {}, {}
    rails = parts(frame, cells, box, clip=True, worn=worn, gauges=gauges)
    if not any(_box_hit((xy[:, 0].min(), xy[:, 1].min(), xy[:, 0].max(), xy[:, 1].max()),
                        (ex[0] - STEP_M, ex[1] - STEP_M, ex[2] + STEP_M, ex[3] + STEP_M)) for *_, xy in rails):
        return None
    prof = profile(rails, ground, tunnels)
    cr = crossings(frame, cells, box, reach=50.0) + tunnels
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
        w = worn.get(fid, 0)
        ballast_w, steel_w = mesh.mat(BALLAST_WORN[w]), mesh.mat(STEEL_WORN[w])
        half_g = gauges.get(fid, gauge(cls, sign_style)) / 2 + RAIL_HEAD / 2
        top = zb + BALLAST_H
        if kind == "ground":
            blk = _blocked(xy, zb, crossers)
            if blk.any():           # (its rails flush with the road at a level crossing)
                zb = _flush(s, zb, blk, np.asarray(ground(xy[blk]), np.float64))
                top = zb + BALLAST_H
            cut = blk | ~inside
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
                _ballast_bed(mesh, p, uu, t, v, fl[sl], fr[sl], zl[sl], zr[sl], ballast_w)
            else:
                _deck(mesh, p, uu, t, v, ballast_w, concrete)
                pr = _sited(_piers(s, xy, u, top, zg, i, j), crossers, ground)
                if pr is not None:
                    small.append((pr, PIER_T / 2, PIER_W / 2, concrete))
            st = t - SLEEPER_SINK + SLEEPER_H           # the sleepers' top: the rails' foot
            out_l = np.c_[uu, np.zeros(len(p))]
            for sg in (1, -1):
                c = p + uu * sg * half_g
                a, b = c + uu * RAIL_HEAD / 2, c - uu * RAIL_HEAD / 2
                mesh.strip(np.c_[a, st + RAIL_H], np.c_[b, st + RAIL_H], 0.0, 0.02, v, steel_w, up)
                mesh.strip(np.c_[a, st], np.c_[a, st + RAIL_H], 0.0, 0.05, v, steel_w, out_l)
                mesh.strip(np.c_[b, st], np.c_[b, st + RAIL_H], 0.0, 0.05, v, steel_w, -out_l)
            sp = _sleepers(s, xy, u, top, i, j, SLEEPERS_GONE[w])
            if sp is not None:
                small.append((sp, SLEEPER_W / 2, SLEEPER_L / 2, mesh.mat(SLEEPER_WORN[w])))
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
