"""Traffic islands inside small road loops: roundabouts, turnaround loops, teardrop islands (the user's report
2026-10-14, Harbor Point: a one-way loop and the link closing it each had sidewalks on both sides, and the inner
ones overlapped into a D-shaped slab with stray corner pieces).

find(roads) looks for short cycles of the road graph (endpoints joined within JOIN_M) that enclose a small area,
mostly bounded by one-way roads (a ring is one-way; two-way loops this small are usually city blocks of alleys,
which keep their sidewalks). For each: the side of each bounding road that faces the inside (no sidewalk, gutter
or curb-return corner there) and the island's curb line, the loop inset by each road's half width: the tile gets
a curbed island along it (a sidewalk run of ISLAND_BAND_M inward) and the inside shows the ground (the photo's
planting). Pure.
"""
import math

import numpy as np

import fast

JOIN_M = 1.5            # road ends this close are one graph node
MAX_EDGES = 10          # roads in a loop (a ring split at every entry; matches RING_MAX_EDGES)
MAX_LOOP_M = 450.0      # loop length (matches RING_MAX_LOOP_M)
# Roundabouts up to ~120 m across (RING_MAX_AREA_M2). The old 1200 m2 cut off rings ~40 m across: their inner
# sidewalks stayed and junctions cut them into four arcs with gaps at the entries (Chavagnes-en-Paillers,
# 46.91387, -1.24230, 2026-10-28). City blocks of one-way streets are filtered by roundness below, not by area.
MAX_AREA_M2 = 12000.0
SMALL_AREA_M2 = 1200.0  # below this: any mostly-one-way loop (turnarounds, teardrops); above: must be round
MIN_AREA_M2 = 25.0
ONEWAY_SHARE = 0.4      # at least this share of the loop's length one-way
ISLAND_BAND_M = 1.2     # the island's curb band (a sidewalk run this wide, inward)
MIN_INSET_AREA_M2 = 6.0  # an inset loop smaller than this (a thin median): no island ring, only no inner sidewalks


def _area(xy):
    x, y = xy[:, 0], xy[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _inside(xy, p):
    x, y = p
    return fast._ef.point_inside(np.asarray(xy, np.float64), float(x), float(y))     # (Rust; even-odd)


def _length(pts):
    return float(np.sum(np.hypot(*np.diff(pts[:, :2], axis=0).T)))


def _offset(pts, d):
    """pts (k, >=2) moved d m to its left (mitred joints, as roads.offset_polyline)."""
    import roads
    return roads.offset_polyline(pts, d)


def _graph(roads_list):
    """(edges [(fid, spec, pts, a, b, length)], adjacency {node: [edge index]}) of roads_list: road ends closer than
    JOIN_M are one node."""
    nodes = []                       # node xy
    grid = {}                        # (cell x, cell y) of JOIN_M -> node indices there, ascending

    def node_of(p):
        # the first node within JOIN_M, as a scan of every node would find it (they were ~7 s of a teleport's event
        # loop in Fells Point, 2026-10-16): only the 3 x 3 cells around p can hold one
        cx, cy = math.floor(p[0] / JOIN_M), math.floor(p[1] / JOIN_M)
        best = None
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for i in grid.get((gx, gy), ()):
                    if (best is None or i < best) and math.hypot(p[0] - nodes[i][0], p[1] - nodes[i][1]) < JOIN_M:
                        best = i
                        break               # ascending: the cell's first hit is its lowest
        if best is not None:
            return best
        nodes.append((float(p[0]), float(p[1])))
        k = len(nodes) - 1
        grid.setdefault((math.floor(nodes[k][0] / JOIN_M), math.floor(nodes[k][1] / JOIN_M)), []).append(k)
        return k
    edges = []
    for fid, spec, pts, _ in roads_list:
        if len(pts) < 2:
            continue
        a, b = node_of(pts[0]), node_of(pts[-1])
        L = _length(pts)
        if L <= 0:
            continue
        edges.append((fid, spec, pts, a, b, L))
    adj = {}
    for k, (_, _, _, a, b, _) in enumerate(edges):
        adj.setdefault(a, []).append(k)
        adj.setdefault(b, []).append(k)
    return edges, adj


def _cycles(edges, adj, max_edges=MAX_EDGES, max_loop=MAX_LOOP_M):
    """Each short cycle of the road graph once: [(edge index, traversed from its start)]. earthfast.road_cycles (Rust:
    per edge a stack walk back to its start, each node's edges in index order; adj is _graph's, the same order)."""
    fast.require(89, "road_cycles")
    a = np.array([e[3] for e in edges], np.int64)
    b = np.array([e[4] for e in edges], np.int64)
    L = np.array([e[5] for e in edges], np.float64)
    off, edge, fwd = fast._ef.road_cycles(a, b, L, int(max_edges), float(max_loop))
    for c in range(len(off) - 1):
        yield [(int(edge[m]), bool(fwd[m])) for m in range(off[c], off[c + 1])]


RING_MAX_AREA_M2 = 12000.0  # a roundabout up to ~120 m across
RING_MAX_LOOP_M = 450.0
RING_MAX_EDGES = 10
# roundness 4 pi area / perimeter^2: a circle 1, a square block 0.785, the two carriageways of a divided road (a loop
# "driven round" too) ~0.1. City blocks of one-way streets came out 0.66-0.81 in Timisoara, its rings 0.87-0.99.
RING_ROUND_ANY = 0.9        # this round: a ring whatever its oneway tags (Timisoara's ring had none at all)
RING_ROUND = 0.82           # this round: a ring if RING_ONEWAY_SHARE of it is one-way, all driven the same way round
RING_ONEWAY_SHARE = 0.5


def ring_directions(roads_list, left_hand=False):
    """{fid: direction along its geometry} of roundabout rings: small loops round as a circle, or nearly that round and at least
    half one-way, all those driven the same way round (spec.dir: +1 along the geometry, -1 against). A junction on a ring gets no
    signals or stop signs (furniture.junction_controls): a ring entry looked like a crossroads of three major roads,
    and its signal poles stood on the ring (Timisoara 2026-10-15). Shared by the road tiles and the road graph, so
    both decide the same. Mapped one-way directions take precedence; missing directions follow the ring's mapped
    circulation, or the driving side when none is mapped. Sorting makes inference independent of cell/load order."""
    edges, adj = _graph(sorted(roads_list, key=lambda r: r[0]))
    boxes = _edge_boxes(edges)
    out = {}
    for cyc in _cycles(edges, adj, RING_MAX_EDGES, RING_MAX_LOOP_M):
        L = sum(edges[k][5] for k, _ in cyc)
        parts = [edges[k][2] if fwd else edges[k][2][::-1] for k, fwd in cyc]
        poly = np.concatenate([p[:-1, :2] for p in parts])
        signed_area = _area(poly) if len(poly) >= 3 else 0.0
        area = abs(signed_area)
        if L > RING_MAX_LOOP_M or not (MIN_AREA_M2 <= area <= RING_MAX_AREA_M2):
            continue
        rnd = 4 * math.pi * area / (L * L)
        ow = [(k, fwd) for k, fwd in cyc if getattr(edges[k][1], "oneway", False) and getattr(edges[k][1], "dir", 0)]
        along = [(edges[k][1].dir > 0) == fwd for k, fwd in ow]      # driven round: all along, or all against
        if along and not (all(along) or not any(along)):
            continue                                                 # one-ways against each other: not a ring
        oneway_ok = along and sum(edges[k][5] for k, _ in ow) >= RING_ONEWAY_SHARE * L
        if rnd >= RING_ROUND_ANY or (rnd >= RING_ROUND and oneway_ok):
            # A through road makes this a connected road loop, not a roundabout with a central island.
            members = {k for k, _ in cyc}
            if any(_crosses(poly, edges[j][2]) for j in _near_edges(boxes, poly) if j not in members):
                continue
            forward = along[0] if along else (signed_area > 0) != bool(left_hand)
            for k, fwd in cyc:
                out[edges[k][0]] = 1 if fwd == forward else -1
    return out


def ring_fids(roads_list):
    """Feature ids of roundabout rings, shared by the signs and the AI graph."""
    return set(ring_directions(roads_list))


JOIN_APPROACH_M = 1.5       # an approach end this close to a ring end meets it
APPROACH_NO_WALK_M = 80.0   # approaches shorter than this: no sidewalks (entry/exit lanes)
ENTRY_CLEAR_M = 4.0         # sidewalks stop this far outside a ring's carriageway (no stub into the circle)


def ring_approaches(roads_list, ring=None):
    """Feature ids of roads that meet a roundabout ring at an end (not themselves on the ring). Short ones are the
    entry/exit lanes whose sidewalks became rectangular stubs against the circle (Chavagnes-en-Paillers 2026-10-28)."""
    ring = set(ring if ring is not None else ring_fids(roads_list))
    if not ring:
        return set()
    ends = []
    for fid, _, pts, _ in roads_list:
        if fid in ring and len(pts) >= 2:
            ends.append(pts[0, :2])
            ends.append(pts[-1, :2])
    if not ends:
        return set()
    ends = np.asarray(ends, np.float64)
    out = set()
    for fid, _, pts, _ in roads_list:
        if fid in ring or len(pts) < 2:
            continue
        for p in (pts[0, :2], pts[-1, :2]):
            if float(np.min(np.hypot(*(ends - p).T))) < JOIN_APPROACH_M:
                out.add(fid)
                break
    return out


def find(roads_list, is_oneway=lambda spec: bool(getattr(spec, "oneway", False)), structures=()):
    """[{"poly": (k, 2) loop centreline (counter-clockwise), "inner": {fid: side (+1 left / -1 right of its
    geometry)}, "ring": (k, 3) curb line counter-clockwise or None}] of roads_list [(fid, spec, pts (k, 3), lon)].
    structures: building centres (x, y); a loop round one is a block, not an island."""
    edges, adj = _graph(roads_list)
    boxes = _edge_boxes(edges)
    found = []
    for cyc in _cycles(edges, adj, MAX_EDGES, MAX_LOOP_M):
        key = {k for k, _ in cyc}
        L = sum(edges[k][5] for k, _ in cyc)
        if L > MAX_LOOP_M or L <= 0:
            continue
        parts = [edges[k][2] if fwd else edges[k][2][::-1] for k, fwd in cyc]
        poly = np.concatenate([p[:-1, :2] for p in parts])
        area = _area(poly)
        abs_area = abs(area)
        if not (MIN_AREA_M2 <= abs_area <= MAX_AREA_M2):
            continue
        ow = sum(edges[k][5] for k, _ in cyc if is_oneway(edges[k][1]))
        oneway_ok = ow >= ONEWAY_SHARE * L
        rnd = 4 * math.pi * abs_area / (L * L)
        # Roundabouts: same roundness rule as ring_directions (a mapped ring often has no oneway tags in Overture,
        # Chavagnes-en-Paillers 2026-10-28: rnd 0.99 but ow 0/102, so the central island was skipped and only the
        # entry teardrops matched). Small non-round loops: mostly one-way (turnarounds, teardrops).
        is_roundabout = rnd >= RING_ROUND_ANY or (rnd >= RING_ROUND and oneway_ok)
        if not (is_roundabout or (abs_area <= SMALL_AREA_M2 and oneway_ok)):
            continue
        if any(_inside(poly, c) for c in structures):
            continue                                # a building inside: a city block, not an island
        if any(_crosses(poly, edges[j][2]) for j in _near_edges(boxes, poly) if j not in key):
            continue                                # a road through it (a turn lane across the triangle, not
                                                    # joined at its corners): the AI drove into the island's curb
                                                    # and backed out, twice (Timisoara 2026-10-15)
        ccw = area > 0
        inner = {}
        for (k, fwd), p in zip(cyc, parts):
            fid = edges[k][0]
            # the traversed part's left is the inside on a counter-clockwise loop
            side_trav = 1 if ccw else -1
            inner[fid] = side_trav if fwd else -side_trav
        if not ccw:
            poly = poly[::-1]
        z = float(np.mean(np.concatenate([edges[k][2][:, 2] for k, _ in cyc])))
        ring = _island_ring(poly, [(edges[k][2][:, :2], edges[k][1].width / 2.0) for k, _ in cyc], z)
        found.append({"poly": poly, "inner": inner, "ring": ring, "round": is_roundabout})
    # A roundabout's island wins over entry teardrops that reuse its ring edges (those left four short curb
    # scraps and no central island at Chavagnes-en-Paillers).
    ring_roads = {fid for f in found if f.get("round") for fid in f["inner"]}
    if ring_roads:
        found = [f for f in found if f.get("round") or not (set(f["inner"]) & ring_roads)]
    return found


RAYS = 72               # the island's curb traced along this many rays from its centre
RAY_STEP_M = 0.2


CROSS_MARGIN_M = 2.0    # a road's point this far inside a loop's centreline polygon is through it


def _edge_boxes(edges):
    """(n, 4) x0, y0, x1, y1 of each edge's points (_graph's edges)."""
    if not edges:
        return np.zeros((0, 4))
    xy = [np.asarray(e[2], np.float64)[:, :2] for e in edges]
    return np.array([(*p.min(0), *p.max(0)) for p in xy], np.float64)


def _near_edges(boxes, poly):
    """Indices (ascending) of the edges whose box meets loop poly's: only those can have a point inside it (_crosses
    of every edge for every candidate loop was ~390k calls on one Las Vegas road tile, 2026-11-01)."""
    if not len(boxes):
        return ()
    lo, hi = poly.min(0), poly.max(0)
    return np.flatnonzero((boxes[:, 0] <= hi[0]) & (boxes[:, 1] <= hi[1]) & (boxes[:, 2] >= lo[0])
                          & (boxes[:, 3] >= lo[1])).tolist()


def _crosses(poly, pts):
    """Does road pts (k, 3) run through the inside of loop poly (k, 2) (points every ~2 m, those within CROSS_MARGIN_M
    of the loop's line not counted: roads joining at its corners)?"""
    xy = np.asarray(pts, np.float64)[:, :2]
    fast.require(1, "loop_crossed")
    return fast._ef.loop_crossed(np.ascontiguousarray(poly, np.float64), np.ascontiguousarray(xy),
                                 float(CROSS_MARGIN_M))


def _seg_dist(pts, p):
    """Distance from p to polyline pts (k, 2)."""
    a, b = pts[:-1], pts[1:]
    d = b - a
    L2 = np.maximum((d * d).sum(1), 1e-12)
    t = np.clip(((p - a) * d).sum(1) / L2, 0.0, 1.0)
    q = a + d * t[:, None]
    return float(np.min(np.hypot(*(q - p).T)))


def _island_ring(poly, roads_hw, z):
    """The island's curb line (k, 3) counter-clockwise: from the loop's centre along RAYS rays, the last point
    still clear of every bounding road's carriageway (its centreline +- half width). None for a sliver.
    Ray-traced instead of offsetting each road (offsets spiked where roads meet at sharp angles)."""
    c = poly.mean(0)
    if not _inside(poly, c) or any(_seg_dist(pts, c) <= hw for pts, hw in roads_hw):
        return None
    rmax = float(np.max(np.hypot(*(poly - c).T)))
    import fast
    return _ring_finish(fast.island_ring_rays(poly, roads_hw, c, RAYS, RAY_STEP_M, rmax), z)


def _ring_finish(ring, z):
    # a light smoothing (the rays' steps), the curb keeps its shape
    ring = (np.roll(ring, 1, 0) + 2 * ring + np.roll(ring, -1, 0)) / 4.0
    if abs(_area(ring)) < MIN_INSET_AREA_M2:
        return None
    return np.column_stack([ring, np.full(len(ring), z)])


def inner_sides(found):
    """{(fid, side)} of the road sides facing an island."""
    return {(fid, side) for f in found for fid, side in f["inner"].items()}


def in_island(found, p):
    """Is point p (x, y) inside one of the loops?"""
    return any(_inside(f["poly"], p) for f in found)


def _splitter_flow(point, into, lanes):
    """A keep-right marker needs opposing traffic, with the incoming lane to its right."""
    import furniture

    left = np.array([-into[1], into[0]])
    sides = set()
    for spec, line in lanes:
        q, tangent = furniture._at(line, furniture._project(line, point[None]))
        delta = q[0, :2] - point
        gap = np.linalg.norm(delta) - spec.width / 2
        across = float(delta @ left)
        if not .32 <= gap <= 7. or abs(across) <= .6:
            continue
        side = 1 if across > 0 else -1
        forward = float(tangent[0] @ into)
        if (spec.oneway and -side * forward * (spec.dir or 1) >= .48
                or not spec.oneway and abs(forward) >= .65):
            sides.add(side)
    return len(sides) == 2


def _splitter_noses(ring):
    """A point inside each end of the raised curb and its inward approach axis."""
    xy = ring[:, :2]
    _, _, axes = np.linalg.svd(xy - xy.mean(axis=0), full_matrices=False)
    along, across = (xy @ axes.T).T
    if not 4. <= np.ptp(along) <= 115. or not .85 <= np.ptp(across) <= 16.:
        return
    for sign in (1, -1):
        signed = sign * along
        next_s = np.roll(signed, -1)
        for depth in (.9, 1.5, 2.4, 3.2):
            level = signed.min() + depth
            cross = (signed <= level) != (next_s <= level)
            u = (level - signed[cross]) / (next_s[cross] - signed[cross])
            meets = across[cross] + u * (np.roll(across, -1)[cross] - across[cross])
            if len(meets) == 2 and np.ptp(meets) >= .82:
                yield np.array([sign * level, meets.mean()]) @ axes, sign * axes[0]
                break


def splitter_heads(found, runs, roads_list, box):
    """Positions and approach axes for raised traffic separators, owned once per tile."""
    import furniture
    import geotiles
    import roads

    local = {"primary", "secondary", "tertiary", "unclassified", "residential", "living_street"}
    streets = {fid: (spec, line) for fid, spec, line, _ in roads_list if spec.kind == "paved" and spec.cls in local}
    if len(streets) < 2:
        return []
    bounds = roads._blocker_segments([(line[:, :2], max(.2, spec.width / 2 + .23))
                                      for _, spec, line, _ in roads_list])
    from streetfront import _road_overlap

    candidates = []
    for f in found:
        if f["round"] or f["ring"] is None:
            continue
        roadsides = [streets[fid] for fid in f["inner"] if fid in streets]
        if len(roadsides) >= 2:
            candidates.extend((p, d) for p, d in _splitter_noses(f["ring"]) if _splitter_flow(p, d, roadsides))
    medians = [w for w in runs if w.get("median") and w["fid"] in streets]
    for walk in medians:
        spec, road = streets[walk["fid"]]
        run = walk["pts"]
        s = furniture._project(road, run[[0, -1], :2]) * (spec.dir or 1)
        start = int(s[1] < s[0])
        if walk["open"][start]:
            continue
        run = run if start == 0 else run[::-1]
        if furniture._arclen(run)[-1] < 3.:
            continue
        p, into = furniture._at(run, np.array([1.4]))
        p, into = p[0, :2], into[0]
        # Both raised bands must start at this physical opening, rather than at a split in the map data.
        for other in medians:
            if walk["fid"] == other["fid"] or not any(
                    not is_open and np.linalg.norm(end[:2] - run[0, :2]) <= 5.
                    for end, is_open in zip(other["pts"][[0, -1]], other["open"])):
                continue
            q, _ = furniture._at(other["pts"], furniture._project(other["pts"], p[None]))
            q = q[0, :2]
            if .35 <= np.linalg.norm(p - q) <= 5.:
                middle = (p + q) / 2
                if _splitter_flow(middle, into, [streets[walk["fid"]], streets[other["fid"]]]):
                    candidates.append((middle, into))
    placed = []
    for point, into in candidates:
        if not geotiles.box_owns(box, *point):
            continue
        if any(np.linalg.norm(point - q) < 3. and float(into @ d) > .5 for q, d in placed):
            continue
        across = .29 * np.array([into[1], -into[0]])
        if not _road_overlap(np.array([[point - across, point + across]]), bounds)[0]:
            placed.append((point, into))
    return placed
