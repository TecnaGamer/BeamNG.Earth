"""Rounded junction pavement without curbs (ported from PR #363 by cody196, 2026-10-28; the user, 2026-10-28: at a rural
T or crossroads the asphalt had square corners and the gravel edge decals stopped square, Carroll County MD).

Where two paved roads without sidewalks meet, the asphalt between their edges is rounded off: junctions.find(pavement=
True) gives each corner an arc tangent to both carriageway edges (junctions.PAVE_RADIUS) and the fill between it and the
edges' old square corner. That corner is
    - painted into the ground (surface.paint_roads: the asphalt material; surface.photo_roads / road_mask: the photo's
      asphalt colour and the near tiles' asphalt mask), or drawn as a fill polygon when the roads are meshes (tile);
    - graded at the roads' heights where its fan reaches past the roads' flat shoulders (grade: a Plan along its arc;
      grading.Grader._make_cell);
    - kept clear of trees, bushes and rocks (vegetation: the pads on its road masks);
    - outlined: the gravel edge decal (roads.EDGE_DECAL) follows its arc, and where both roads have edge lines a curved
      edge line joins them, the straight lines and decals cut at the tangent points (tile).
Corners between roads with sidewalks are curb returns (junctions.find(has_sidewalks) in roads.build_tile): left out.
Deterministic: pure functions of the roads (sorted by id), kept for a few calls (_CACHE).
"""
from collections import OrderedDict
from concurrent.futures import Future
from threading import Lock
import math

import numpy as np
import dmath
import dnp

import junctions

_CACHE = OrderedDict()          # key -> (the cells and spec memos it was made from, kept alive: ids, entry)
_PENDING = {}                   # key -> Future: a build in flight (the photo, mask and material ask at once)
_LOCK = Lock()
CACHE_N = 16
BOUNDS_ROUND_M = 512.0          # m: requested bounds rounded out to this grid (blocks move 128 m: one build serves several)
ROWS_MARGIN_M = 160.0           # m around the bounds the roads are read (a corner's junction and its roads within 60 m)
MERGE_DEG = junctions.ACUTE_DEG     # two one-way roads meeting under this: a merge or diverge (markings.gores' wedge)


def _paved(sp):
    """A road that gets corners: paved, no ring, no race track, no town's alley or lot aisle (a service road in town:
    Baltimore's rowhouse alleys gave thousands of 3 m corners and doubled its tiles' build time)."""
    return sp.kind == "paved" and sp.cls != "raceway" and not getattr(sp, "roundabout", False) \
        and not (sp.urban and sp.cls == "service")


def _candidate(rings, ground=False):
    """has_walk for junctions.find: the roads that get corners (rings: the rings' fids). ground: the grading's (no
    service roads: a 3 m corner's fan lies within its roads' flat shoulders, grading.shoulder)."""
    import roads

    def ok(fid, sp):
        return _paved(sp) and fid not in rings and not roads.has_sidewalks(sp) and not (ground and sp.cls == "service")
    return ok


LOW_SHOULDER_M = 2.0    # m: a road with a flat shoulder narrower than this (grading.shoulder: motorways, expressways,
                        # their ramps; the classes that get sidewalks in towns keep a sidewalk's width everywhere) ...


def _low(sp):
    """... is one whose corners' fans reach past the graded ground (a 6 m corner's fan reaches 1.8 m past the edges,
    a 12 m one's 3.5 m): the grading grades the corners near such roads only (grade)."""
    import grading
    return grading.shoulder(sp) < LOW_SHOULDER_M


GROUND_NEAR_M = 80.0    # m: ... the grading's (no islands: a junction's roads and the carriageways within 60 m of it)
NEAR_CELL_M = 140.0     # m: the roads within this of a road that gets corners are the ones its junctions see (their
                        # branches, the carriageways tested within 60 m, a traffic island's loop of <= 400 m round)


def relevant(rows, rings=(), ground=False):
    """The indices of rows [(fid, spec, pts, lon)] that can matter to a corner: every road with a point within about
    NEAR_CELL_M of a road that gets corners (a 140 m grid's 3 x 3 cells round its points every 70 m). A town's
    junctions of streets with sidewalks are left out at once (junctions.find's Python loops over them were most of its
    cost); the corners of the rest come out the same."""
    import roads
    ok = _candidate(rings, ground)
    if ground:      # (the grading's: only round roads with a narrow flat shoulder, _low)
        cand = [k for k, (fid, sp, _p, _l) in enumerate(rows) if ok(fid, sp) and _low(sp)]
    else:
        cand = [k for k, (fid, sp, _p, _l) in enumerate(rows) if ok(fid, sp)]
    if not cand:
        return []
    if len(cand) == len(rows):
        return list(range(len(rows)))
    g = GROUND_NEAR_M if ground else NEAR_CELL_M
    dens = [roads.densify(np.ascontiguousarray(p[:, :3] if p.shape[1] >= 3 else np.c_[p, np.zeros(len(p))]), g / 2)
            for _f, _s, p, _l in rows]
    owner = np.repeat(np.arange(len(rows)), [len(d) for d in dens])
    cells = np.floor(np.concatenate(dens)[:, :2] / g).astype(np.int64)
    code = cells[:, 0] * 4294967296 + cells[:, 1]
    is_cand = np.zeros(len(rows), bool)
    is_cand[cand] = True
    ck = np.unique(cells[is_cand[owner]], axis=0)
    near = (ck[:, None, :] + np.array([(a, b) for a in (-1, 0, 1) for b in (-1, 0, 1)])[None]).reshape(-1, 2)
    hit = np.isin(code, np.unique(near[:, 0] * 4294967296 + near[:, 1]))
    return np.flatnonzero(is_cand | np.bincount(owner, weights=hit, minlength=len(rows)).astype(bool)).tolist()


def find(rows, bounds=None, rings=(), ground=False):
    """The curbless corners of rows [(fid, spec, pts (k, 3), lon)] (junctions.find's corner dicts with road_sides and
    edge_cuts): paved roads, neither with sidewalks; none on a roundabout ring (rings: their fids, roads.is_ring) or
    facing a traffic island (islands.find), none in a merge's gore. bounds: only the corners that may reach it.
    ground: the grading's (grade): no service roads' corners, the islands' not left out (their ground graded too)."""
    import roads
    import islands
    if not rows:
        return []
    keep = relevant(rows, rings, ground)
    if not keep:
        return []
    if len(keep) < len(rows):
        rows = [rows[k] for k in keep]
    ok = _candidate(rings, ground)
    good = {id(sp) for fid, sp, _, _ in rows if ok(fid, sp)}
    # (roads with sidewalks get no pavement corner: not tried at all)
    corners = junctions.find(rows, lambda sp: id(sp) in good, 0.0, pavement=True, pavement_bounds=bounds)[0]
    if not corners:
        return []
    by_id = {fid: sp for fid, sp, _, _ in rows}
    inner = None
    out = []
    for c in corners:
        (fa, sa), (fb, sb) = c["road_sides"]
        a, b = by_id[fa], by_id[fb]
        if roads.has_sidewalks(a) or roads.has_sidewalks(b):
            continue                            # (a curb return's: roads.build_tile)
        ua, ub = c["edge_cuts"][0][1], c["edge_cuts"][1][1]
        if a.oneway and b.oneway and float(dnp.dot(ua, ub)) > dmath.cos(math.radians(MERGE_DEG)):
            continue                            # a ramp's gore, a divided road's split: no asphalt fan between them
        if ground:
            out.append(c)
            continue
        if inner is None:
            inner = islands.inner_sides(islands.find(rows))
        if (fa, sa) in inner or (fb, sb) in inner:
            continue                            # inside a small loop: its island
        out.append(c)
    return out


def polygons(c, grow=0.0):
    """A corner's fill polygon(s) (m, 3): its old square corner and its arc, the arc grown outward by grow m."""
    out = []
    if c.get("fill") is not None:
        fill = np.array(c["fill"], np.float64)
        if grow:
            fill[1:, :2] += c["inward"][:len(fill) - 1] * grow
            if c.get("edge_cuts") is not None and len(fill) == len(c["arc"]) + 1:
                arc = np.asarray(c["arc"], np.float64)
                for at, away in zip((1, -1), _outward(c)):
                    fill[at, :2] = arc[0 if at == 1 else -1, :2] + away * grow
        out.append(fill)
    out.extend(np.asarray(p, np.float64) for p in c.get("fills", []))
    return out


def ring_ids(feats):
    """{fid} of a road Features' mapped roundabout rings (roads.is_ring)."""
    import roads
    if feats.attrs.get("osm_junction") is None:
        return set()
    return {str(feats.ids[i]) for i in range(len(feats)) if roads.is_ring(feats, i)}


def _entry(cells, urban=None, widths=None, frame=None, left_hand=False, bounds=None, lonlat_bounds=None):
    """[frame, rows, corners] of road Features cells (kept: _CACHE; one build for concurrent askers)."""
    import roads
    import geodesy
    cells = tuple(f for f in cells if f is not None and len(f))
    if not cells:
        return [None, [], []]
    anchor = frame is not None
    if frame is None:
        lon, lat = cells[0].lonlat[0]
        frame = geodesy.EnuFrame(float(lat), float(lon), 0.0)
    if lonlat_bounds is not None:
        lo0, la0, lo1, la1 = lonlat_bounds
        e, n, _ = frame.geodetic_to_enu(np.array([la0, la0, la1, la1]), np.array([lo0, lo1, lo0, lo1]), np.zeros(4))
        bounds = (float(np.min(e)) - 4.0, float(np.min(n)) - 4.0, float(np.max(e)) + 4.0, float(np.max(n)) + 4.0)
    if bounds is not None:
        g = BOUNDS_ROUND_M
        bounds = (math.floor(bounds[0] / g) * g, math.floor(bounds[1] / g) * g,
                  math.ceil(bounds[2] / g) * g, math.ceil(bounds[3] / g) * g)
    specs = [roads.road_specs(f, urban, widths, left_hand) for f in cells]
    memos = tuple(f.__dict__.get("_road_specs") for f in cells)
    key = (tuple((id(f), id(m)) for f, m in zip(cells, memos)), frame.lat0, frame.lon0, frame.h0, urban, widths,
           bool(left_hand), bounds)
    with _LOCK:
        saved = _CACHE.get(key)
        if saved is not None:
            _CACHE.move_to_end(key)
            return saved[1]
        pending = _PENDING.get(key)
        owner = pending is None
        if owner:
            pending = _PENDING[key] = Future()
    if not owner:
        return pending.result()
    try:
        rows, rings = _rows(cells, specs, frame, bounds, anchor)
        entry = [frame, rows, find(rows, bounds, rings)]
        with _LOCK:
            while len(_CACHE) >= CACHE_N:
                _CACHE.popitem(last=False)
            _CACHE[key] = ((cells, memos), entry)       # (the ids in the key stay valid while it is kept)
            _PENDING.pop(key, None)
        pending.set_result(entry)
        return entry
    except BaseException as exc:
        with _LOCK:
            _PENDING.pop(key, None)
        pending.set_exception(exc)
        raise


def _rows(cells, specs, frame, bounds=None, anchor=True):
    """The ground parts of the cells' roads as junctions.find takes them (each feature once, sorted by id), only those
    within ROWS_MARGIN_M of bounds; and the rings' ids. anchor: frame is an anchor's (the features keep their
    coordinates in it: mapgeom.Features.xy_in); else each part is converted (a passing frame must not push the anchors'
    out of the features' memo)."""
    import roads
    import mapgeom
    import surface
    rows, seen, rings = [], set(), set()
    if bounds is not None:
        m = ROWS_MARGIN_M
        bx0, by0, bx1, by1 = bounds[0] - m, bounds[1] - m, bounds[2] + m, bounds[3] + m
        if not anchor:          # (the box in lon / lat: the features' own boxes, kept with them)
            la, lo, _ = frame.enu_to_geodetic(np.array([bx0, bx1, bx0, bx1]), np.array([by0, by0, by1, by1]),
                                              np.zeros(4))
            lbox = (float(np.min(lo)), float(np.max(lo)), float(np.min(la)), float(np.max(la)))
    for f, fspecs in zip(cells, specs):
        boxes = None if bounds is None else f.boxes(frame) if anchor else surface._lonlat_boxes(f)
        rings |= ring_ids(f)
        for i, sp in enumerate(fspecs):
            fid = str(f.ids[i])
            if sp is None or fid in seen:
                continue
            if boxes is not None:
                b = boxes[i]
                if anchor and (b[2] < bx0 or b[0] > bx1 or b[3] < by0 or b[1] > by1):
                    continue
                if not anchor and (b[1] < lbox[0] or b[0] > lbox[1] or b[3] < lbox[2] or b[2] > lbox[3]):
                    continue
            seen.add(fid)
            for p in roads.ground_parts(f, i):
                if anchor:
                    e, n = mapgeom.enu_xy(f, frame, p)
                    rows.append((fid, sp, np.c_[e, n, np.zeros(len(p))], float(np.mean(p[:, 0]))))
                else:
                    rows.append((fid, sp, np.asarray(p, np.float64), float(np.mean(p[:, 0]))))
    if not anchor and rows:     # (one conversion for all)
        ll = np.concatenate([r[2] for r in rows])
        e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
        k, out = 0, []
        for fid, sp, p, lon in rows:
            out.append((fid, sp, np.c_[e[k:k + len(p)], n[k:k + len(p)], np.zeros(len(p))], lon))
            k += len(p)
        rows = out
    rows.sort(key=lambda row: (row[0], tuple(row[2][0])))
    return rows, rings


def source(cells, urban=None, widths=None, frame=None, left_hand=False, bounds=None):
    """(frame, rows, corners) of road Features cells."""
    return tuple(_entry(cells, urban, widths, frame, left_hand, bounds=bounds)[:3])


def pads(cells, urban=None, widths=None, frame=None, bounds=None, lonlat_bounds=None, grow=0.0):
    """The corners' fills as (lat (k,), lon (k,)) pads (surface.paint_pads / photo_pads / pads_mask), their arcs grown
    outward by grow m (the roads' own paint reaches past their edges). bounds (x0, y0, x1, y1) in frame or lonlat_bounds
    (lon0, lat0, lon1, lat1): the corners that may reach it (more may come)."""
    frame, _rows_, corners = _entry(cells, urban, widths, frame, bounds=bounds, lonlat_bounds=lonlat_bounds)[:3]
    out = []
    for c in corners:
        for poly in polygons(c, grow):
            la, lo, _ = frame.enu_to_geodetic(poly[:, 0], poly[:, 1], np.zeros(len(poly)))
            out.append((np.asarray(la), np.asarray(lo)))
    return out


def curb_corners(entry):
    """The curb returns (junctions.find(has_sidewalks), as roads.build_tile makes them) of an _entry's rows: their fills,
    the asphalt between the arc and the old square corner, belong to the road's paint (the user, 2026-11-11: "side walk
    corners still need improvment, you can tell because the road paint is a diffrent shape": with the roads painted
    into the ground the fills were drawn by nothing, the photo's own ground showing between the sharp paint corner and
    the curb). Kept in the entry (computed once, under its lock); none inside an island or at a round ring (as
    build_tile)."""
    import roads
    import islands
    with _LOCK:
        if len(entry) > 3:
            return entry[3]
    rows = entry[1]
    out = []
    if rows:
        corners = junctions.find(rows, roads.has_sidewalks, None)[0]
        isl = islands.find(rows) if corners else []
        for c in corners:
            p = np.asarray(c["arc"], np.float64)[len(c["arc"]) // 2]
            if isl and islands.in_island(isl, p[:2]):
                continue
            if any(float(dnp.hypot(*(p[:2] - f["poly"].mean(0))))
                   < float(np.max(dnp.hypot(*(f["poly"] - f["poly"].mean(0)).T))) + islands.ENTRY_CLEAR_M
                   for f in isl if f.get("round")):
                continue
            out.append(c)
    with _LOCK:
        if len(entry) == 3:
            entry.append(out)
        return entry[3]


def curb_pads(cells, urban=None, widths=None, frame=None, bounds=None, lonlat_bounds=None, grow=0.0):
    """pads' (lat, lon) fills of the curb returns (curb_corners) that may reach bounds."""
    entry = _entry(cells, urban, widths, frame, bounds=bounds, lonlat_bounds=lonlat_bounds)
    if entry[0] is None:
        return []
    frame = entry[0]
    out = []
    for c in curb_corners(entry):
        for poly in polygons(c, grow):
            la, lo, _ = frame.enu_to_geodetic(poly[:, 0], poly[:, 1], np.zeros(len(poly)))
            out.append((np.asarray(la), np.asarray(lo)))
    return out


def _edge_inset(sp):
    import roads
    return roads.motorway_shoulder(sp) if roads.rural_motorway(sp) else roads.EDGE_LINE["inset"]


def _outward(c):
    """The unit normals away from the pavement at a corner's two tangent points (square to each road)."""
    (_, ui, _), (_, uj, _) = c["edge_cuts"]
    return np.array([-ui[1], ui[0]]), -np.array([-uj[1], uj[0]])


def _decal_line(c):
    """The gravel edge decal's centre line along a corner's arc (as roads.extras puts it beside a straight edge: the road
    on the right of its direction)."""
    import roads
    dw = roads.EDGE_DECAL["w"]
    arc = np.array(c["arc"], np.float64)
    off = (roads.EDGE_DECAL_AT - 0.5) * dw
    line = arc.copy()
    line[:, :2] += c["inward"] * off
    for at, out in zip((0, -1), _outward(c)):
        line[at, :2] = arc[at, :2] + out * off          # (on the straight decals' line exactly)
    d = np.diff(line[:, :2], axis=0)
    k = len(d) // 2
    right = np.array([d[k, 1], -d[k, 0]])
    if float(dnp.matmul(right, -c["inward"][k])) < 0:          # the pavement must be on its right
        line = line[::-1].copy()
    return line


JUNCTION_NEAR_M = junctions.MERGE_M + junctions.NODE_TOL     # m: a road with a vertex this near a corner's node meets there


def _cut_decal(piece, blockers):
    """A corner's decal piece without its points near other carriageways (roads.cut_outside), whole when none is (a
    short arc would go: cut_outside keeps runs of MIN_PIECE_M or more)."""
    import roads
    import fast
    d = roads.densify(np.asarray(piece, np.float64), 0.5)
    segs, starts, hws = roads._blocker_segments(blockers)
    if not np.asarray(fast._ef.inside_blockers(np.ascontiguousarray(d[:, :2]), segs, starts, hws)).any():
        return [piece]
    return roads.cut_outside(piece, blockers, 0.5)


def tile(rows, box, draw_pavement, country=None, left_hand=False, rings=(), no_decal=(), exact=None):
    """(chunks, cuts) of a road tile: the corners' fill polygons (draw_pavement: the roads are meshes, not painted into
    the ground; each corner by the tile its arc's middle is in, exact (x0, y0, x1, y1)), the gravel edge decal along each
    arc and, where both roads have edge lines, the curved edge line, clipped to box; cuts {(fid, side): [(node xy, u,
    t)]}: the straight edge lines and decals of those road sides end at the tangents (junctions.apply_cuts). rows: the
    tile's roads [(fid, spec, pts (k, 3), lon)]; no_decal: fids whose edges have no gravel decal (parking aisles)."""
    import roads
    import markings
    chunks, cuts = [], {}
    if not rows:
        return chunks, cuts
    by_id = {fid: sp for fid, sp, _, _ in rows}
    x0, y0, x1, y1 = box
    ex0, ey0, ex1, ey1 = exact if exact is not None else box
    st = markings.style(country, float(np.mean([r[3] for r in rows])))
    packed = None
    for c in find(rows, box, rings):
        sides = c["road_sides"]
        for side, cut in zip(sides, c["edge_cuts"]):
            cuts.setdefault(side, []).append(cut)       # (every corner's: its cut reaches past its arc)
        polys = polygons(c)
        arc = np.array(c["arc"], np.float64)
        allp = np.vstack([arc] + polys)
        if allp[:, 0].max() < x0 or allp[:, 0].min() > x1 or allp[:, 1].max() < y0 or allp[:, 1].min() > y1:
            continue
        specs = [by_id[fid] for fid, _ in sides]
        mid = arc[len(arc) // 2]
        if draw_pavement and ex0 <= mid[0] < ex1 and ey0 <= mid[1] < ey1:
            look = roads._corner_look(mid, rows)
            for p in polys:
                chunks.append({"m": look["m"], "w": 0.0, "tl": look["tl"], "p": look["p"],
                               "poly": [[round(float(v), 3) for v in q] for q in p], "n": []})
        if not any(fid in no_decal for fid, _ in sides):
            # (as the straight decals: none where another carriageway is near; the roads at this junction not counted,
            # the arc keeps off them, and a through road split at the node had its other half wipe the arc)
            X = c["edge_cuts"][0][0]
            if packed is None:          # (the roads' points once a tile: each corner's distances in one native call)
                import fast
                fast.require(235, "lines_point_min_dist")
                lens = [len(r[2]) for r in rows]
                packed = (np.ascontiguousarray(np.concatenate([np.asarray(r[2], np.float64)[:, :2] for r in rows])),
                          np.r_[0, np.cumsum(lens)].astype(np.int64))
            near = fast._ef.lines_point_min_dist(*packed, float(X[0]), float(X[1]))
            others = [(p2[:, :2], s2.width / 2 + roads.EDGE_DECAL["w"] + roads.EDGE_DECAL_GAP_M)
                      for (f2, s2, p2, _), dk in zip(rows, near.tolist())
                      if s2.kind in roads.BUILT and f2 not in (sides[0][0], sides[1][0]) and dk > JUNCTION_NEAR_M]
            for piece in roads.clip_polyline(_decal_line(c), x0, y0, x1, y1):
                for run in (_cut_decal(piece, others) if others else [piece]):
                    for ch in roads.split(roads.densify(run)):
                        if len(ch) >= 2:
                            chunks.append(roads._chunk(roads.EDGE_DECAL, roads.EDGE_DECAL["w"], ch))
        if not all(roads.paints_edge_line(sp.cls, st) for sp in specs):
            continue
        import franceroads
        if any(franceroads.enabled(sp) and franceroads.edge_pattern(sp) is None for sp in specs):
            continue
        # the curved edge line: inset like each road's straight one (roads.extras), its ends on them exactly
        inset = [_edge_inset(sp) for sp in specs]
        line = arc.copy()
        line[:, :2] -= c["inward"] * np.linspace(inset[0], inset[1], len(arc))[:, None]
        for at, out, ins in zip((0, -1), _outward(c), inset):
            line[at, :2] = arc[at, :2] - out * ins
        width = max(st.edge_motorway if sp.cls == "motorway" else st.edge for sp in specs)
        mats = []
        for sp, (_, side) in zip(specs, sides):
            travel_left = 1 if getattr(sp, "dir", 0) >= 0 else -1
            mats.append(st.left_edge if (st.left_edge and sp.oneway and side == travel_left) else "line_white")
        look = {"m": mats[0] if mats[0] == mats[1] else "line_white", "tl": roads.EDGE_LINE["tl"],
                "p": roads.CENTRE["p"]}
        french = all(franceroads.enabled(sp) for sp in specs)
        if french:
            look["m"] = franceroads.PAINT_MATERIAL
        pattern = franceroads.edge_pattern(specs[0]) if french else None
        runs = markings.dashes(line, pattern) if pattern else [line]
        for run in runs:
            for piece in roads.clip_polyline(run, x0, y0, x1, y1):
                for ch in roads.split(roads.densify(piece)):
                    if len(ch) >= 2:
                        chunks.append(roads._chunk(look, width, ch))
    return chunks, cuts


def _z_on(xy, z, pts):
    """Heights at points pts (m, 2) from the nearest point of polyline xy (k, 2) with heights z (k,)."""
    if len(pts) * len(xy) > 200000:     # (in pieces: m x k arrays)
        n = max(1, 200000 // len(xy))
        return np.concatenate([_z_on(xy, z, pts[i:i + n]) for i in range(0, len(pts), n)])
    a, b = xy[:-1], xy[1:]
    d = b - a
    L2 = np.maximum(np.sum(d * d, axis=1), 1e-12)
    t = np.clip(dnp.vecdot(pts[:, None, :] - a[None], d) / L2[None], 0.0, 1.0)
    q = a[None] + t[..., None] * d[None]
    k = np.argmin(dnp.hypot(pts[:, None, 0] - q[..., 0], pts[:, None, 1] - q[..., 1]), axis=1)
    i = np.arange(len(pts))
    return z[k] + t[i, k] * (z[k + 1] - z[k])


def grade_rows(parts, plans, frame, rings=()):
    """(rows, heights) of a grading cell's road parts for grade: parts [(fid, spec, lon/lat ring)] with their Plans
    (plans[i] for parts[i], None: none) as junctions rows [(fid, spec, pts (k, 3), lon)], only the roads that can matter
    to a corner (relevant), and {fid: [(plan xy (n, 2), plan heights (n,))]}: the plans' profiles."""
    got = [(fid, sp, np.asarray(ring, np.float64), p) for (fid, sp, ring), p in zip(parts, plans)
           if p is not None and len(p.xy) >= 2 and len(ring) >= 2]
    if not got:
        return []
    ll = np.concatenate([r for _f, _s, r, _p in got])
    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
    e, n = np.asarray(e, np.float64), np.asarray(n, np.float64)
    rows, plan_of, k = [], [], 0
    for fid, sp, ring, p in got:
        xy = np.c_[e[k:k + len(ring)], n[k:k + len(ring)]]
        k += len(ring)
        rows.append((fid, sp, np.c_[xy, np.zeros(len(xy))], float(np.mean(ring[:, 0]))))
        plan_of.append(p)
    out, heights = [], {}
    for i in relevant(rows, rings, ground=True):
        fid, sp, pts, lon = rows[i]
        p = plan_of[i]
        heights.setdefault(fid, []).append((np.asarray(p.xy, np.float64)[:, :2], np.asarray(p.z, np.float64)))
        out.append((fid, sp, pts, lon))
    out.sort(key=lambda row: (row[0], tuple(np.round(row[2][0, :2], 3))))
    return out, heights


def _edge_dists(c, specs, pts):
    """m of points pts (m, 2) of a corner's fan beyond each of its two roads' carriageway edges: (d_i, d_j)."""
    (Pi, ui, _), (Pj, uj, _) = c["edge_cuts"]
    di = dnp.matmul(pts - Pi, np.array([-ui[1], ui[0]])) - specs[0].width / 2
    dj = -(dnp.matmul(pts - Pj, np.array([-uj[1], uj[0]]))) - specs[1].width / 2
    return di, dj


GRADE_STEP_M = 1.0      # m between a corner's grading lines (grade) ...
GRADE_CORE_M = 0.6      # ... each this flat either side


def grade(rows, rings=(), bounds=None, heights=None):
    """[(fid, Plan)] holding the ground under each corner at its roads' heights where the fan reaches past both roads'
    flat shoulders (grading.shoulder): a corner of a road with a narrow one (_low; a street's 3 m keeps a 6 m corner's
    fan on it, a 12 m one's within 0.75 m of it, in the transition's first metre). Lines along the arc every
    GRADE_STEP_M in toward the old square corner, each kept only beyond the shoulders (PR #363's one plan along the
    arc, its core as wide as the fan, lay over the carriageways by the tangents: the roads' own heights there were
    pulled to the arc's, Otto Road's dip under its overpass). Without the land cover (grading's rule) every town street
    counts as curbless here: the low roads' alone keep a town's cost down. The same corner in neighbouring cells is
    one grading surface (its rid). heights: grade_rows' plan profiles: the arc's ends at the roads' planned heights by
    its tangent points (rows' own z: at their vertices, maybe 100 m apart)."""
    import grading
    by_id = {fid: sp for fid, sp, _, _ in rows}
    out = []
    for c in find(rows, bounds, rings, ground=True):
        specs = [by_id[fid] for fid, _ in c["road_sides"]]
        if c.get("fill") is None or not any(_low(sp) for sp in specs):
            continue
        arc = np.array(c["arc"], np.float64)
        if heights:
            ends = []
            for (f, _), at in zip(c["road_sides"], (0, -1)):
                q = arc[at:at + 1 if at == 0 else None, :2]
                best = min(heights.get(f, ()), default=None,
                           key=lambda h, q=q: float(np.min(dnp.hypot(*(h[0] - q[0]).T))))
                ends.append(arc[at, 2] if best is None else float(_z_on(best[0], best[1], q)[0]))
            arc[:, 2] = np.linspace(ends[0], ends[1], len(arc))
        flat = np.array([grading.shoulder(sp) for sp in specs])
        fid = "corner:" + repr((sorted(c["road_sides"]), tuple(np.round(np.asarray(c["fill"])[0, :2], 1).tolist())))
        rid = grading.road_id(fid)
        for k in range(64):
            xy = arc[:, :2] - c["inward"] * (k * GRADE_STEP_M)
            di, dj = _edge_dists(c, specs, xy)
            keep = np.minimum(di - flat[0], dj - flat[1]) > GRADE_CORE_M - 0.25
            if not keep.any():
                break
            idx = np.flatnonzero(keep)
            for run in np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1):
                if len(run) < 2:
                    continue
                p = grading.Plan(xy[run].copy(), arc[run, 2].copy(), GRADE_CORE_M, road_half=0.0)
                p.rid = rid
                out.append((f"{fid}:{k}", p))
    return out
