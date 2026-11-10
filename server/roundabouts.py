"""Roundabout furniture (plan 038, Nachod's ring at 50.41730 N 16.16901 E): what a roundabout has beyond its ring road.

- island_layers: the central island as layers, outside in (curb line offset from the ring's edge, width, curb height,
  paving): a low paved truck apron (the Czech "prstenec", CDV 2012: sett 2-3.5 m, a car does not drive over it but a lorry
  must) and a raised planter curb round the planting. An island too small for both keeps today's single curb band.
- outer_curbs: the ring's outside curb between its entries, in towns (the asphalt ended in the grass).
- entry_marks: the give-way line across every entry (it was Sweden's and Germany's alone): a dashed transverse line
  (markings.wait_lines; Czechia V 6b, Poland P-12, France / Italy / Austria likewise), triangles where the yield line
  is a row of them (the US, MUTCD 3B.16).
- drop_on_islands: the signs standing inside a roundabout's loop (Nachod's two 50 signs stood on the apron).
BNE_ROUNDABOUTS=off turns all three off. Pure numpy (like urbanground)."""
import math
import os
import types

import numpy as np

APRON_MIN_INNER_M = 4.2  # an island whose inner radius (ring edge) is smaller keeps the single curb band
APRON_MAX_INNER_M = 24.0  # a larger island (a big roundabout, ~60 m across) has no apron: a plain curb
APRON_FRAC = 0.22       # the apron's width as a share of the island's radius ...
APRON_MIN_M, APRON_MAX_M = 1.3, 2.6     # ... clamped (CDV: 2.5 m of sett abroad, 3.5 m in Letovice, ~2.3 m in Nachod)
APRON_H = 0.04          # m: a low, mountable curb to the road (sett flush with the lane edge, 3-5 cm)
PLANTER_H = 0.17        # m: its curb, the bed raised over the apron
PLANTER_MIN_M = 1.2     # m the planting keeps inside (else the apron reaches the middle)
OUTER_W = 0.6           # m: the outer curb's band
OUTER_H = 0.15
OUTER_STEP_M = 1.0      # m between the samples that decide where an entry blocks the curb
OUTER_CLEAR_M = 3.0     # m past an entry road's edge the curb leaves the ring (its corner radius begins)
OUTER_JOIN_M = 4.0      # m out from the ring's edge along an entry's edge the corner radius ends: roads.py's sidewalks start there
                        # (islands.ENTRY_CLEAR_M), so the curb meets them
OUTER_BEZIER_N = 8      # points of a corner radius
OUTER_REACH_M = 16.0    # m a corner radius's control point is at most from the curb's end
OUTER_MIN_RUN_M = 4.0   # a curb run shorter than this is dropped
OUTER_NEAR_M = 60.0     # m round the ring the entry roads are looked for
TEETH_COUNTRIES = ("US", "CA")          # where the yield line is a row of triangles
DOUBLE_COUNTRIES = ("GB", "IE")         # where it is two dashed lines (UK TSRGD diagram 1003: 0.45 m apart, 150 mm)
DOUBLE_GAP_M = 0.45


def enabled():
    return os.environ.get("BNE_ROUNDABOUTS", "").strip().lower() not in ("off", "0", "false", "no")


def _area(xy):
    x, y = xy[:, 0], xy[:, 1]
    return abs(0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def inner_radius(ring):
    """The radius of the circle with the island curb line's area (ring (k, >=2): its edge, as islands.find)."""
    return math.sqrt(_area(np.asarray(ring, np.float64)[:, :2]) / math.pi)


def apron_width(radius):
    return float(min(APRON_MAX_M, max(APRON_MIN_M, APRON_FRAC * radius)))


LAWN = 8
PLANT_WEIGHTS = ((LAWN, 0.40), (9, 0.30), (10, 0.15), (11, 0.15))     # lawn, shrub bed, gravel, mulch: a Czech town's islands
PLANT_BAND_M = 0.5      # m: the planting's curb band (the fan inside it is the planting)
MOUND_PER_M = 0.012     # m the planting's middle stands over its edge per m of island radius (a gentle mound) ...
MOUND_MAX_M = 0.2       # ... at most


def planting(lat, lon):
    """The planting slot (sidewalks.PLANTINGS) of the island whose middle is at lat, lon: one of the four by a hash of the
    place at ~10 m (the same island whichever tile or anchor builds it; the map says nothing of an island's planting)."""
    h = (int(round(lat * 1e4)) * 73856093) ^ (int(round(lon * 1e4)) * 19349663)
    u = ((h * 2654435761) % 1000003) / 1000003.0
    acc = 0.0
    for slot, w in PLANT_WEIGHTS:
        acc += w
        if u < acc:
            return slot
    return PLANT_WEIGHTS[0][0]


def island_layers(found, plain_band, at=(0.0, 0.0)):
    """[(offset m inward of the ring's edge, width m, curb height m, slab material or 0)] of one islands.find result, outside
    in: the paved apron (sidewalks.SETT), then the planting's raised bed (a curb round a slab that fills the island to its
    middle, so it rises a little to it: sidewalks.CROSS_SLOPE) in the material planting(*at) gives. plain_band: today's
    single band (offset, width) for a loop that is no roundabout, or too small for the layers."""
    off, width = plain_band
    single = [(off, width, 0.15, 0)]
    if not enabled() or not found.get("round") or found.get("ring") is None:
        return single
    r = inner_radius(found["ring"])
    w = apron_width(r)
    if r - w < PLANTER_MIN_M or not APRON_MIN_INNER_M <= r <= APRON_MAX_INNER_M:
        return single
    # (a layer's offset is its middle's: roads walks' pts are the slab's middle line, run_geometry takes the curb from it)
    return [(w / 2, w, APRON_H, 7), (w + PLANT_BAND_M / 2, PLANT_BAND_M, PLANTER_H, 0)]


def planting_fill(found, plain_band, at, offset):
    """(ring (k+1, 3) closed counter-clockwise, apex (3,), slab material, ring lift m, mound m) of the island's planting: a fan
    from the inner edge of its curb band (the layers' second) to the island's middle, in the material planting(*at) gives; None
    when the island has no layers (island_layers' single band). offset: roads.offset_polyline."""
    layers = island_layers(found, plain_band, at)
    if len(layers) < 2:
        return None
    r = inner_radius(found["ring"])
    ring = np.asarray(found["ring"], np.float64)
    closed = np.vstack([ring[-1:], ring, ring[:2]])
    edge = offset(closed, layers[0][1] + PLANT_BAND_M)[1:-1]         # (the band's inner edge, as run_geometry gives it)
    apex = ring.mean(0)               # (the curb line's rays are evenly spread: its mean is its middle)
    lift = PLANTER_H + PLANT_BAND_M * 0.015                      # (the band's slab top: sidewalks.CROSS_SLOPE)
    return edge, apex, planting(*at), lift, min(MOUND_MAX_M, MOUND_PER_M * r)


def _dist_to_line(points, line):
    """Distance from each of points (n, 2) to polyline line (k, 2)."""
    a, b = line[:-1], line[1:]
    d = b - a
    L2 = np.maximum((d * d).sum(1), 1e-12)
    t = np.clip(((points[:, None, :] - a[None]) * d[None]).sum(2) / L2[None], 0.0, 1.0)
    q = a[None] + t[:, :, None] * d[None]
    return np.hypot(q[:, :, 0] - points[:, None, 0], q[:, :, 1] - points[:, None, 1]).min(1)


def outer_curbs(found, roads_list, offset, densify):
    """[run (k, 3)] of the curb along the outside of one roundabout's ring (counter-clockwise: its outside is the right of
    the way), between the entries, in towns only (an approach with sidewalks round it). found: islands.find's result;
    roads_list: the tile's [(fid, spec, pts, lon)]; offset, densify: roads' helpers. A run's z is the loop's mean."""
    if not enabled() or not found.get("round"):
        return []
    poly = np.asarray(found["poly"], np.float64)
    ring_ids = set(found["inner"])
    hw = max((sp.width / 2.0 for fid, sp, _p, _l in roads_list if fid in ring_ids), default=0.0)
    if hw <= 0.0 or len(poly) < 6:
        return []
    c = poly.mean(0)
    reach = float(np.max(np.hypot(*(poly - c).T))) + hw + OUTER_NEAR_M
    others = []
    for fid, sp, pts, _l in roads_list:
        if fid in ring_ids or getattr(sp, "kind", "paved") != "paved":
            continue
        xy = np.asarray(pts, np.float64)[:, :2]
        if len(xy) < 2 or float(np.min(np.hypot(*(xy - c).T))) > reach:
            continue
        # (from the ring out: the end nearer the loop's middle first)
        xy = xy if np.hypot(*(xy[0] - c)) <= np.hypot(*(xy[-1] - c)) else xy[::-1]
        others.append((xy, sp.width / 2.0, bool(getattr(sp, "urban", False))))
    if not any(u for _, _, u in others):
        return []                               # no town round it: the asphalt meets the verge, as it does
    closed = np.vstack([poly[-1:], poly, poly[:2]])
    edge = offset(closed, -(hw + 0.05))[1:-1]       # right of a counter-clockwise loop: outwards
    edge = np.vstack([edge, edge[:1]])
    line = densify(np.column_stack([edge, np.zeros(len(edge))]), spacing=OUTER_STEP_M)[:, :2]
    blocked = np.zeros(len(line), bool)
    who = np.full(len(line), -1)                    # the entry that blocks a point (the one it is nearest to the edge of)
    best = np.full(len(line), np.inf)
    for k, (xy, h, _u) in enumerate(others):
        gap = _dist_to_line(line, xy) - (h + OUTER_CLEAR_M)
        hit = (gap < 0) & (gap < best)
        who[hit], best[hit] = k, gap[hit]
        blocked |= gap < 0
    z = float(np.mean(found["ring"][:, 2])) if found.get("ring") is not None else 0.0
    # a closed loop starting inside a run: rotate to start at a blocked point so no run is split
    if blocked.any() and not blocked.all():
        s = int(np.flatnonzero(blocked)[0])
        line, blocked, who = np.roll(line, -s, 0), np.roll(blocked, -s), np.roll(who, -s)
    runs, cur, first = [], [], 0
    for i, (p, b) in enumerate(zip(line, blocked)):
        if b:
            if len(cur) >= 2:
                runs.append((np.array(cur), first, i - 1))
            cur = []
        else:
            if not cur:
                first = i
            cur.append(p)
    if len(cur) >= 2:
        runs.append((np.array(cur), first, len(line) - 1))
    out = []
    for r, i0, i1 in runs:
        if float(np.hypot(*np.diff(r, axis=0).T).sum()) < OUTER_MIN_RUN_M:
            continue
        if blocked.any():
            ahead, behind = who[(i1 + 1) % len(line)], who[(i0 - 1) % len(line)]
            if ahead >= 0:
                tail = _corner(r[-1], r[-1] - r[-2], others[ahead], hw)
                if tail is not None:
                    r = np.vstack([r, tail])
            if behind >= 0:
                head = _corner(r[0], r[0] - r[1], others[behind], hw)
                if head is not None:
                    r = np.vstack([head[::-1], r])
        out.append(np.column_stack([r, np.full(len(r), z)]))
    return out


def _corner(p, d, entry, ring_hw):
    """(n, 2) points continuing a curb run that ends at p heading d (away from the run) to the edge of the entry road entry (xy
    from the ring out, half width, urban): a quadratic Bezier from p, its control point where the run's tangent meets the road's
    edge line, to the edge OUTER_JOIN_M past the ring's edge. None where there is no such corner (parallel, behind, too far)."""
    xy, half, _u = entry
    d = np.asarray(d, np.float64)
    d = d / max(float(np.hypot(*d)), 1e-9)
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    sq = min(ring_hw + OUTER_JOIN_M, float(s[-1]))
    at = np.array([np.interp(sq, s, xy[:, 0]), np.interp(sq, s, xy[:, 1])])
    k = int(np.clip(np.searchsorted(s, sq) - 1, 0, len(xy) - 2))
    u = xy[k + 1] - xy[k]
    u = u / max(float(np.hypot(*u)), 1e-9)
    n = np.array([-u[1], u[0]])
    side = 1.0 if float(np.dot(p - at, n)) >= 0 else -1.0
    q = at + n * side * half
    den = d[0] * u[1] - d[1] * u[0]
    if abs(den) < 1e-6:
        return None
    w = q - p
    t = (w[0] * u[1] - w[1] * u[0]) / den
    if not 0.2 < t <= OUTER_REACH_M:
        return None
    ctrl = p + d * t
    tau = np.linspace(0.0, 1.0, OUTER_BEZIER_N + 1)[1:, None]
    return (1 - tau) ** 2 * p + 2 * tau * (1 - tau) * ctrl + tau ** 2 * q


def entry_arms(roads_list, signals=None, country=None, priority=None):
    """furniture.give_way_arms' arms that are roundabout entries (a road ending at a ring: islands.ring_approaches)."""
    import furniture
    import islands
    app = islands.ring_approaches(roads_list)
    if not app:
        return []
    return [a for a in furniture.give_way_arms(roads_list, signals, country, priority) if a[0] in app]


def entry_marks(roads_list, signals, country, priority, crosswalks, left_hand=False):
    """(mark polygons [(4, 3) quads or (3, 3) triangles], line cuts {fid: [...]}) of the entries' give-way lines outside
    Sweden and Germany (their own paint is markings.give_way_lines' and designs'). Nothing when switched off."""
    if not enabled():
        return [], {}
    arms = entry_arms(roads_list, signals, country, priority)
    if not arms:
        return [], {}
    import markings
    if str(country or "").upper() in TEETH_COUNTRIES:
        teeth, _gone = markings.give_way_lines(arms, crosswalks, left_hand)
        return list(teeth), {}
    dashes, cuts = markings.wait_lines(arms, crosswalks, left_hand)
    if str(country or "").upper() in DOUBLE_COUNTRIES:
        moved = [(fid, sp, _from(line, DOUBLE_GAP_M), wide) for fid, sp, line, wide in arms]
        more, _cuts = markings.wait_lines([a for a in moved if a[2] is not None], crosswalks, left_hand)
        dashes = list(dashes) + list(more)
    return list(dashes), cuts


def _from(line, m):
    """The centreline line (k, 3) from m metres along it on (the line a second yield line stands one gap out on), None if shorter."""
    line = np.asarray(line, np.float64)
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(line[:, :2], axis=0).T))]
    if s[-1] <= m + 1.0:
        return None
    keep = s > m
    start = np.array([np.interp(m, s, line[:, k]) for k in range(3)])
    return np.vstack([start, line[keep]])


SIGN_NEAR_M = 130.0     # m: a road this near a sign (box to box) may be a ring round it (a ring is at most ~120 m across)
JOIN_M = 1.5


def _loops(roads_list, ring):
    """[(k, 2) ring points ordered by angle round their centre] of each roundabout of the ring fids (roads joined at their ends)."""
    ends = {}
    up = {}

    def root(f):
        up.setdefault(f, f)
        while up[f] != f:
            up[f] = up[up[f]]
            f = up[f]
        return f
    pts = {fid: np.asarray(p, np.float64)[:, :2] for fid, _s, p, _l in roads_list if fid in ring}
    for fid, p in pts.items():
        root(fid)
        for e in (p[0], p[-1]):
            key = (round(float(e[0]) / JOIN_M), round(float(e[1]) / JOIN_M))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    other = ends.get((key[0] + dx, key[1] + dy))
                    if other is not None and other != fid:
                        up[root(fid)] = root(other)
            ends.setdefault(key, fid)
    groups = {}
    for fid, p in pts.items():
        groups.setdefault(root(fid), []).append(p)
    out = []
    for g in groups.values():
        xy = np.concatenate(g)
        c = xy.mean(0)
        out.append(xy[np.argsort(np.arctan2(xy[:, 1] - c[1], xy[:, 0] - c[0]))])
    return out


def drop_on_islands(signs, rows):
    """signs (k, >=2) [x, y, ...] without those inside a roundabout's loop. rows: speedsigns._cell_rows' values (line, xy line,
    meta [half width, dir, ...], ...) of the roads round the signs; a ring is found as islands.ring_directions finds it."""
    signs = np.asarray(signs)
    if not enabled() or not len(signs) or not rows:
        return signs
    import islands
    lo, hi = signs[:, :2].min(0) - SIGN_NEAR_M, signs[:, :2].max(0) + SIGN_NEAR_M
    roads_list = []
    for k, r in enumerate(rows):
        xy = r[1]
        if xy[:, 0].max() < lo[0] or xy[:, 0].min() > hi[0] or xy[:, 1].max() < lo[1] or xy[:, 1].min() > hi[1]:
            continue
        roads_list.append((k, types.SimpleNamespace(oneway=bool(r[2][1]), dir=int(r[2][1])), xy, 0.0))
    ring = islands.ring_directions(roads_list) if roads_list else {}
    if not ring:
        return signs
    keep = np.ones(len(signs), bool)
    for loop in _loops(roads_list, set(ring)):
        c = loop.mean(0)
        if float(np.hypot(*(signs[:, :2] - c).T).min()) > float(np.hypot(*(loop - c).T).max()):
            continue
        for i, sgn in enumerate(signs):
            if keep[i] and islands._inside(loop, sgn[:2]):
                keep[i] = False
    return signs[keep]


# What grows on an island (plan 038: "no grass or shrubs here"): the stock shrub group's low variants as Forest items
# (trees.lua rows [group, x, y, z, yaw, scale, height, rank]; the group's shape nearest the height, scaled down to it).
# slot (sidewalks.PLANTINGS) -> [(trees.lua group, grid spacing m, height range m, share of the grid points planted)]: shrub is
# the stock bushes, tuft the clover / daisy patches (0.1 m), fern the stock ferns (trees.lua CATALOG)
PLANTS = {8: [("tuft", 1.5, (0.09, 0.15), 0.65), ("shrub", 4.0, (0.4, 0.8), 0.5)],        # lawn: clover and daisy patches, a few low shrubs
          9: [("shrub", 1.25, (0.6, 1.5), 0.9), ("fern", 2.6, (0.5, 0.9), 0.5)],          # shrub bed: dense, ferns between
          10: [("shrub", 3.5, (0.4, 1.0), 0.5), ("tuft", 2.0, (0.09, 0.14), 0.4)],        # gravel: a few
          11: [("fern", 2.2, (0.4, 0.8), 0.6), ("shrub", 2.4, (0.3, 0.7), 0.6), ("tuft", 1.8, (0.09, 0.15), 0.5)]}   # mulch
PLANT_EDGE_M = 0.7      # m the plants keep from the planting's edge (its curb band)


def _h01(*key):
    import zlib
    return (zlib.crc32(repr(key).encode()) & 0xFFFFFF) / float(0x1000000)


def plant_rows(fill, ground, owns):
    """[[group, x, y, z, yaw, scale, height, rank]] of the plants of one island's planting (fill: planting_fill's; ground(xy (n, 2))
    -> z of the ground there; owns(x, y): the tile's own points): a hashed grid from the island's middle, jittered, thinned to the
    slot's share, each standing on the mound's surface (the fan's: lift at the edge, + mound at the middle)."""
    import islands
    edge, apex, slot, lift, mound = fill
    xy = np.asarray(edge, np.float64)[:, :2]
    c = np.asarray(apex, np.float64)[:2]
    rad = float(np.hypot(*(xy - c).T).mean())
    inner = c + (xy - c) * max(0.0, 1.0 - PLANT_EDGE_M / max(rad, 1e-6))
    reach = float(np.hypot(*(xy - c).T).max())
    out = []
    for kind, (group, spacing, (h0, h1), share) in enumerate(PLANTS[slot]):
        n = int(reach // spacing) + 1
        pts, meta = [], []
        for i in range(-n, n + 1):
            for j in range(-n, n + 1):
                key = (i, j, slot, kind)
                if _h01("keep", *key) >= share:
                    continue
                p = c + ((i + (_h01("pi", *key) - 0.5) * 0.8) * spacing, (j + (_h01("pj", *key) - 0.5) * 0.8) * spacing)
                if islands._inside(inner, p) and owns(p[0], p[1]):
                    pts.append(p)
                    meta.append(key)
        if not pts:
            continue
        pts = np.array(pts)
        z = np.asarray(ground(pts), np.float64).ravel()
        for p, zg, key in zip(pts, z, meta):
            top = lift + mound * max(0.0, 1.0 - float(np.hypot(*(p - c))) / rad)
            out.append([group, round(float(p[0]), 2), round(float(p[1]), 2), round(float(zg + top - 0.03), 2),
                        round(360.0 * _h01("yaw", *key), 1), 1.0, round(h0 + (h1 - h0) * _h01("h", *key), 2),
                        round(_h01("rank", *key), 3)])
    return out


# Splitter islands (plan 038): a raised teardrop in the middle of an approach where the map says the crossing there has a refuge
# (crossing:island=yes on the node: Nachod's three, 2081183730 / 3499131216 / 3916664198).
SPLIT_NODE_M = 4.0      # m a refuge node may lie off an approach's centreline
SPLIT_REACH_M = 45.0    # m from the ring an approach's refuge is looked for
SPLIT_HALF_M = 0.8      # m: the island's half width at the crossing
SPLIT_TIP_M = 0.45      # m: its half width at the tip
SPLIT_FAR_M = 2.5       # m the blunt end lies past the crossing
SPLIT_NEAR_M = 6.0      # m the tip lies short of the crossing (towards the ring)
SPLIT_RING_CLEAR_M = 2.5    # m from the ring's end the tip keeps at least
SPLIT_MIN_ROAD_M = 6.0  # an approach narrower than this keeps its whole width
SPLIT_CURB_W = 0.35
SPLIT_CURB_H = 0.15


def splitters(roads_list, refuges):
    """[{"outline": (k, 3) counter-clockwise, "fid"}] of the splitter islands of the roundabout approaches of roads_list
    ([(fid, spec, pts, lon)]) at the refuge nodes refuges [(x, y)] (secrossings.nodes' "islands"): along the approach's
    centreline from its crossing (the node's foot on it) SPLIT_FAR_M out to SPLIT_NEAR_M towards the ring, SPLIT_HALF_M wide
    at the crossing and SPLIT_TIP_M at the tip. None where the approach is narrow, the node is off it, or the tip would be
    too near the ring."""
    if not enabled() or refuges is None or not len(refuges):
        return []
    import islands
    ring = islands.ring_fids(roads_list)
    app = islands.ring_approaches(roads_list, ring)
    if not app:
        return []
    ends = np.array([p[k, :2] for fid, _s, p, _l in roads_list if fid in ring for k in (0, -1)], np.float64).reshape(-1, 2)
    out, used = [], set()
    for node in np.asarray(refuges, np.float64).reshape(-1, 2):
        best = None
        for fid, sp, pts, _l in roads_list:
            if fid not in app or sp.width < SPLIT_MIN_ROAD_M or len(pts) < 2:
                continue
            xy = np.asarray(pts, np.float64)
            e0 = float(np.min(np.hypot(*(ends - xy[0, :2]).T)))
            e1 = float(np.min(np.hypot(*(ends - xy[-1, :2]).T)))
            line = xy if e0 <= e1 else xy[::-1]           # (from the ring out)
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(line[:, :2], axis=0).T))]
            a, b = line[:-1, :2], line[1:, :2]
            d = b - a
            t = np.clip(((node - a) * d).sum(1) / np.maximum((d * d).sum(1), 1e-12), 0.0, 1.0)
            q = a + d * t[:, None]
            dist = np.hypot(*(q - node).T)
            k = int(np.argmin(dist))
            at = float(s[k] + t[k] * np.hypot(*d[k]))
            if dist[k] <= SPLIT_NODE_M and at <= SPLIT_REACH_M and (best is None or dist[k] < best[0]):
                best = (float(dist[k]), fid, line, s, at)
        if best is None or best[1] in used:
            continue
        _d, fid, line, s, sc = best
        near = max(sc - SPLIT_NEAR_M, SPLIT_RING_CLEAR_M)
        far = sc + SPLIT_FAR_M
        if far - near < 3.0 or far >= s[-1]:
            continue
        used.add(fid)
        st = np.linspace(near, far, 9)
        c = np.stack([np.interp(st, s, line[:, k]) for k in range(3)], 1)
        u = np.gradient(c[:, :2], axis=0)
        u /= np.maximum(np.hypot(*u.T), 1e-9)[:, None]
        nrm = np.stack([-u[:, 1], u[:, 0]], 1)
        hw = np.where(st <= sc, SPLIT_TIP_M + (SPLIT_HALF_M - SPLIT_TIP_M) * (st - near) / max(sc - near, 1e-6), SPLIT_HALF_M)
        left = c[:, :2] + nrm * hw[:, None]
        right = c[:, :2] - nrm * hw[:, None]
        outline = np.vstack([right, left[::-1]])
        poly = np.column_stack([outline, np.r_[c[:, 2], c[::-1, 2]]])
        area = 0.5 * float(np.dot(outline[:, 0], np.roll(outline[:, 1], -1)) - np.dot(outline[:, 1], np.roll(outline[:, 0], -1)))
        if area < 0:
            poly = poly[::-1]
        out.append({"outline": poly, "fid": fid})
    return out
