"""Roads near the player (docs/ROADMAP.md v1.6 "Roads"; first cut 2026-10-11): Overture road segments
from the map-data cache (mapdata.py "roads"), drawn by the mod as DecalRoads on the physics ground, and
painted into the surface masks (surface.py) so the tyres feel asphalt or dirt.

Tiles: TILE x TILE m squares of the anchor's ENU frame, keyed (anchor, tx, ty). The Tracker of a
connection keeps the tiles within SEND_M of the player sent (built in a worker, written to
`roads_<a>_<tx>_<ty>.json` in the session folder, announced with a `roads_tile` message) and drops
those beyond DROP_M (`roads_drop`). An anchor change re-sends everything in the new frame; the mod
drops the old anchor's roads then.

A tile's file is a list of chunks, each one DecalRoad:
    {"m": material, "w": width m, "tl": texture length m, "p": render priority, "n": [[x, y, z], ...],
     "h": curb height m (sidewalks only: the mod raises a slab this far above the road near the player)}
with x, y, z in the anchor frame (z on the DEM, the DecalRoad snaps its nodes to the ground anyway)
and at most MAX_NODES nodes. Roads are clipped to the tile (OVERLAP m past its edge), simplified
(RDP, SIMPLIFY_M) and split. Deterministic: a pure function of the cells' road data, the frame and
the DEM.

Not yet: grading the ground under roads, markings, junction shapes, bridges and tunnels (segments
flagged as bridge / tunnel or off ground level are left out), the navgraph.
"""
import json
import logging
import math
import os
import threading
import time
from collections import namedtuple

import numpy as np

import junctions
import mapcells
import mapgeom
import staging

log = logging.getLogger("earthd.roads")

VERSION = 1
TILE = 256.0            # m, tile side in the anchor frame
SEND_M = 640.0          # tiles whose square comes this close to the player (Chebyshev) are sent
DROP_M = 960.0          # ... and dropped beyond this
OVERLAP = 2.0           # m a road reaches past its tile's edge (no gap at tile seams)
SIMPLIFY_M = 0.25       # RDP tolerance (Road Spline's layerMgr)
SIMPLIFY_Z_M = 0.05     # ... and the vertical one of lines drawn at their own heights (decks, tunnels: simplify ztol)
MAX_NODES = 100         # nodes per DecalRoad (Road Spline's chunk size)
MAX_SPACING = 12.0      # m between nodes at most (official maps: median 11-12 m)
BUILD_PER_POSE = 4      # tiles started per pose at most
# At speed the tiles reach along the car's path (the user, 2026-10-19: "Going high speeds the road texture can struggle
# to keep up ... even if we have to sacrifice some things further away from the car"): above FAST_MS the tiles within
# CORRIDOR_M of the path ahead are wanted too, up to LEAD_S of driving (LEAD_MAX_M at most), and every wanted tile is
# built in the order the car reaches it (path_key): the corridor first, the ones beside and behind the car last.
FAST_MS = 25.0          # m/s (56 mph)
LEAD_S = 14.0           # s of driving ahead
LEAD_MAX_M = 2000.0
CORRIDOR_M = 160.0      # m either side of the path
SIDE_PENALTY = 4.0      # a tile off the path counts this many times its sideways distance (path_key)

# class -> (width m, paved by default). Classes not here (footway, path, steps, cycleway, ...) are
# left out for now.
STYLE = {
    "motorway": (11.0, True), "trunk": (9.0, True), "primary": (8.0, True), "secondary": (7.5, True),
    "tertiary": (6.5, True), "residential": (6.0, True), "living_street": (5.0, True),
    "unclassified": (5.5, True), "service": (4.0, True), "track": (3.5, False), "unknown": (5.0, True),
}
PAVED = {"paved", "asphalt", "concrete", "metal", "wood"}
COBBLE = {"paving_stones", "sett", "cobblestone", "unhewn_cobblestone", "bricks"}   # Overture: "paving_stones"
GRAVEL = {"gravel", "fine_gravel", "compacted", "pebblestone"}
# kinds with a hard, built surface: carriageway extras (parking, strips, sidewalks) apply
BUILT = {"paved", "cobble"}
# surface kind -> DecalRoad look (stock materials, loaded by the mod from the official levels)
LOOK = {
    "paved": {"m": "road_asphalt_2lane", "tl": 11.0, "p": 20},
    # Italy's cobblestone (a mesh material with groundType COBBLESTONE): a DecalRoad stretches its
    # texture across the whole width, so cobbled roads are drawn as parallel strips of about
    # "strip" m with a square texture repeat, which keeps the stones their size
    "cobble": {"m": "italy_cobblestone", "tl": 2.5, "p": 20, "strip": 2.5},
    "gravel": {"m": "m_dirt_road", "tl": 88.0, "p": 21},
    "dirt": {"m": "m_dirt_road", "tl": 88.0, "p": 21},
}
Spec = namedtuple("Spec", "width kind urban cls oneway walk dir kmh gap median shoulder bias link lanes lane_w lane_model surface urban_share",
                  defaults=(False, 2.5, 0, None, None, None, 0.0, 0.0, False, None, 3.6, "", "", 1.0))
# road_specs' result per feature (walk: the sidewalk width, measured from the buildings where known: roadwidth.py;
# dir: a one-way road's direction along its geometry, +1 / -1, 0 two-way; kmh: its speed limit, None unknown;
# gap: m of road per junction along it (its connectors), None unknown)
# (width = carriageway). One-way driving widths (Overture's divided roads are two one-way segments, each
# drawn before as a full two-way street: 2026-10-11 orthophoto of Broadway, 14-15 m drawn for 8-9 m)
ONEWAY_STYLE = {"primary": 6.5, "secondary": 6.5, "tertiary": 4.0, "residential": 3.5, "living_street": 3.5,
                "unclassified": 3.5, "service": 3.5, "unknown": 3.5}

# Sidewalks and markings (user 2026-10-11, after the first street-view comparison of Fells Point:
# real streets have concrete sidewalks behind a curb and a double yellow centre line). Stock West
# Coast USA materials. renderPriority: lower draws on top (WCUSA: lines 2-6, the asphalt strip 20).
SIDEWALK = {"m": "sidewalk_concrete", "tl": 6.0, "p": 16, "w": 2.5}      # beyond the carriageway, city streets
# Storm drain inlets: a metal grate in the gutter (Driver Training's m_metal_grid_drain), DRAIN_IN m
# before the real end of each sidewalk run (one per corner, where real inlets sit) and every
# DRAIN_EVERY m along long runs (each is a DecalRoad: kept to ~40 per city tile)
DRAIN = {"m": "m_metal_grid_drain", "tl": 2.0, "p": 12, "w": 0.45, "len": 1.0}
DRAIN_IN, DRAIN_EVERY = 2.5, 90.0
GUTTER = {"m": "gutter1", "tl": 8.0, "p": 14, "w": 0.5}                  # at the curb line
CENTRE = {"tl": 12.0, "p": 6, "w": 0.12, "gap": 0.12}                    # a double line: two lines this far from the centre
EDGE_LINE = {"m": "line_white", "tl": 6.4, "p": 2, "w": 0.45, "inset": 0.3}
RAMP_DOT_W = .25
RAMP_DOT_M, RAMP_DOT_GAP_M = 1.8, 5.5
CURVE_SIMPLIFY_M = .005
CURVE_HEADING_DEG = .1  # chord error against the fitted tangent; distance alone permits visible knees
AUX_LANE_M, AUX_TAPER_M = 200., 70.
MEASURE_SKIP = {"service"}      # classes not measured between buildings (lots, alleys: far walls)
MEASURE_MAX = 3.0               # a measured carriageway at most this times the class default
SIDEWALK_CLASSES = {"residential", "tertiary", "secondary", "primary", "unclassified", "living_street"}
CENTRE_CLASSES = {"primary", "secondary", "tertiary", "trunk"}
EDGE_LINE_CLASSES = {"primary", "secondary", "tertiary", "trunk", "motorway"}
LANE_CLASSES = {"primary", "secondary", "tertiary", "trunk", "motorway"}
# urban arterials not lined by buildings (roadwidth.py measures those): 4 lanes (Moab's Main Street, US-191,
# 4 lanes + a turn lane in the photos, was drawn 9 m)
# Ramps and slip roads (Overture subclass "link", any class): one lane and its shoulders, whatever the class
# (Columbia's US-29 ramps were drawn 13 m wide, a whole motorway, 2026-10-14 photos): m paved by lanes a way.
LINK_M = {1: 6.0, 2: 9.0}
LINK_LANE_M = 3.7       # a one-lane ramp's lane between its shoulders
URBAN_ARTERIAL = {"trunk": 13.2}     # primaries in towns are often 2 lanes (Lancaster County's PA-340)
LANE_M = 3.3            # m per lane when counting the lanes a carriageway holds
LANE_LINE = {"m": "line_dashed_long", "tl": 12.0, "p": 6, "w": 0.12}     # West Coast USA's 3 m dash, 9 m gap
CUT_MARGIN = 0.5                # m: a sidewalk / line stops this far outside a crossing road's carriageway
MIN_PIECE_M = 3.0               # shorter sidewalk / line pieces are dropped
WALK_STEP_M = 1.0               # m the ground under a sidewalk may lie off the road's level: further, a cutting's wall or
                                # a drop is there and the sidewalk stops (it was draped up a wall, Munich's McGraw-Graben)
NEAR_M = 40.0                   # roads this far outside the tile still cut its sidewalks / lines
# Carriageway = driving width + parking lanes on city streets or paved edge strips on rural roads
# (docs/research/2026-10-11-official-road-study.md §2: rural paved surface = AI width + 2-2.5 m;
# West Coast USA's plain streets 12.5 m for 8 m of driving: ~2.3 m of parking each side). A width
# rule, where Overture has one, is the carriageway already.
PARKING_M = 2.3
# Rural motorways: two lanes a direction (most rural Interstates and European motorways; every US Interstate was drawn
# with three, GitHub #43) between paved shoulders, the edge line along each; urban ones keep STYLE's three lanes.
# Overture has no lane counts.
MOTORWAY_LANE_M = 3.6
MOTORWAY_SHOULDER_M = 3.0          # outside paved shoulder, beside the edge line
MOTORWAY_INNER_SHOULDER_M = 1.2    # median-side paved shoulder
MOTORWAY_RURAL_M = 2 * MOTORWAY_LANE_M
SHOULDER_M = MOTORWAY_SHOULDER_M
# Keep the existing 1.5.5 bridge cross-sections and paint widths stable; this batch changes their surface UVs only.
BRIDGE_MOTORWAY_RURAL_M = 7.4
BRIDGE_SHOULDER_M = 2.2
BRIDGE_EDGE_LINE_WIDTH = 0.15
PARKING_CLASSES = {"residential", "tertiary", "secondary", "unclassified", "living_street"}
EDGE_STRIP_M = 1.0
URBAN_CLASS = 50                # ESA WorldCover "built-up"
URBAN_SHARE = 0.5               # a road is urban when this share of its vertices is built-up


def _rule_value(table, i):
    """The value of the rule of feature i covering the largest share of it, or None."""
    if table is None or len(table.feature) == 0:
        return None
    j = table.major(i)
    return None if j < 0 else table.value[j]


def level_ranges(feats, i):
    """The off-ground ranges of feature i as sorted (start, end, "bridge" | "tunnel") fractions of its
    length, from its flags (is_bridge / is_tunnel) and level (> 0 bridge, < 0 tunnel) rules. Overture
    puts most bridges on a range of a longer segment (2026-10-11: a street with a few metres of bridge
    was taken whole as a bridge and dropped). Pure."""
    memo = feats.__dict__.setdefault("_level_ranges", {})
    got = memo.get(i)
    if got is None:
        got = memo[i] = _level_ranges(feats, i)
    return list(got)


def _level_ranges(feats, i):
    got = {"flags": [], "level": []}
    for name in ("flags", "level"):
        t = feats.rules.get(name)
        if t is None or len(t.feature) == 0:
            continue
        for j in t.rows(i):
            v = t.value[j]
            if name == "flags":
                kind = "tunnel" if "is_tunnel" in str(v) else "bridge" if "is_bridge" in str(v) else None
            else:
                try:
                    lv = float(v)
                except (TypeError, ValueError):
                    lv = 0.0
                kind = "bridge" if lv > 0 else "tunnel" if lv < 0 else None
            if kind is not None and t.end[j] > t.start[j]:
                got[name].append((float(t.start[j]), float(t.end[j]), kind))
    # the flags first; a level range only where no flag says otherwise (Tower Bridge: level 1 over 0.03-0.88 of its
    # segment, flags bridge / tunnel (through a tower) / bridge / tunnel / bridge: merged, tunnel winning, the whole
    # bridge was a tunnel, under water for most of it, and left out, 2026-10-18)
    out = sorted(got["flags"])
    for a, b, k in sorted(got["level"]):
        for fa, fb, _ in out[:]:
            if fb <= a or fa >= b:
                continue
            if fa > a:
                out.append((a, fa, k))
            a = max(a, fb)
            if a >= b:
                break
        if a < b:
            out.append((a, b, k))
    out.sort()
    merged = []                                 # overlapping ranges merged (a tunnel wins over a bridge); touching
    for a, b, k in out:                         # ones of the same kind joined, of different kinds kept apart
        if merged and (a < merged[-1][1] - 1e-9 or (a <= merged[-1][1] + 1e-9 and k == merged[-1][2])):
            pa, pb, pk = merged[-1]
            merged[-1] = (pa, max(pb, b), "tunnel" if "tunnel" in (pk, k) else "bridge")
        else:
            merged.append((a, b, k))
    if len(merged) >= 3:                        # a short tunnel between two bridge ranges: a passage through a
        L = _feature_length_m(feats, i)         # bridge's tower (Tower Bridge's): the bridge goes on
        for n in range(1, len(merged) - 1):
            a, b, k = merged[n]
            if k == "tunnel" and (b - a) * L < PASSAGE_M and merged[n - 1][2] == "bridge" and \
                    merged[n + 1][2] == "bridge" and abs(merged[n - 1][1] - a) < 1e-6 and abs(b - merged[n + 1][0]) < 1e-6:
                merged[n] = (a, b, "bridge")
        joined = []
        for a, b, k in merged:
            if joined and k == joined[-1][2] and a <= joined[-1][1] + 1e-9:
                joined[-1] = (joined[-1][0], b, k)
            else:
                joined.append((a, b, k))
        merged = joined
    if sum(1 for _, _, k in merged if k == "tunnel") > 1:
        # one tunnel, not several: Overture splits a tunnel's range (0.08-0.09, 0.10-0.13, 0.14-0.69 of the Fort
        # McHenry Tunnel's segment), and each gap would be a few metres of ground road inside the mountain
        L = _feature_length_m(feats, i)
        joined = []
        for a, b, k in merged:
            if joined and k == "tunnel" and joined[-1][2] == "tunnel" and (a - joined[-1][1]) * L < TUNNEL_GAP_M:
                joined[-1] = (joined[-1][0], b, "tunnel")
            else:
                joined.append((a, b, k))
        merged = joined
    return merged


PASSAGE_M = 40.0        # m: a tunnel range this short between two bridge ranges is a passage through a tower
TUNNEL_GAP_M = 30.0     # m: tunnel ranges of a segment this close together are one tunnel


def _feature_length_m(feats, i):
    """Length of feature i in metres (its rings, lon scaled by cos lat). Pure."""
    total = 0.0
    for part in feats.parts(i):
        for ring in part:
            r = np.asarray(ring, np.float64)
            if len(r) >= 2:
                c = math.cos(math.radians(float(np.mean(r[:, 1]))))
                total += float(np.hypot(np.diff(r[:, 0]) * c, np.diff(r[:, 1])).sum()) * 111320.0
    return total


def sub_polyline(ring, a, b):
    """The part of a lon/lat polyline (k, >=2) between fractions a < b of its length (metres along it,
    lon scaled by cos lat). Pure."""
    ring = np.asarray(ring, np.float64)
    c = math.cos(math.radians(float(np.mean(ring[:, 1]))))
    seg = np.hypot(np.diff(ring[:, 0]) * c, np.diff(ring[:, 1]))
    s = np.r_[0.0, np.cumsum(seg)]
    if s[-1] <= 0:
        return ring[:1]
    sa, sb = a * s[-1], b * s[-1]
    inner = ring[(s > sa) & (s < sb)]
    pa = np.array([np.interp(sa, s, ring[:, d]) for d in range(ring.shape[1])])
    pb = np.array([np.interp(sb, s, ring[:, d]) for d in range(ring.shape[1])])
    return np.vstack([pa, inner, pb])


MIN_PART = 1e-3     # parts shorter than this share of a segment are dropped


def _parts_by_level(feats, i, want):
    memo = feats.__dict__.setdefault("_parts_by_level", {})      # remembered: cells stay decoded (mapdata)
    got = memo.get((i, want))
    if got is None:
        got = memo[(i, want)] = _parts_by_level_new(feats, i, want)
    return list(got)


def _parts_by_level_new(feats, i, want):
    rngs = level_ranges(feats, i)
    if want == "ground":
        pieces, at = [], 0.0
        for a, b, _ in rngs:
            if a - at > MIN_PART:
                pieces.append((at, a))
            at = max(at, b)
        if 1.0 - at > MIN_PART:
            pieces.append((at, 1.0))
    else:
        pieces = [(a, b) for a, b, k in rngs if k == want and b - a > MIN_PART]
    if not pieces:
        return []
    out = []
    for pi, part in enumerate(geometry_parts(feats, i) if want == "ground" else feats.parts(i)):
        for ri, ring in enumerate(part):
            if len(ring) < 2:
                continue
            for a, b in pieces:
                param = feats.__dict__.get("_geometry_params", {}).get((i, pi, ri)) if want == 'ground' else None
                if param is None:
                    r = ring if (a, b) == (0.0, 1.0) else sub_polyline(ring, a, b)
                else:
                    # Map rules refer to distance on the original line. A fitted curve has a
                    # different length; retain those exact source positions at bridge portals.
                    r = np.vstack([[np.interp(a, param, ring[:, d]) for d in range(ring.shape[1])],
                                   ring[(param > a+1e-10) & (param < b-1e-10)],
                                   [np.interp(b, param, ring[:, d]) for d in range(ring.shape[1])]])
                if len(r) >= 2:
                    out.append(r)
    return out


def bridge_levels(feats, i):
    """The level of each of bridge_parts(feats, i) (same order): the highest level rule overlapping its
    range, 1 for a bridge flagged without one. Overture's levels order stacked roads. Pure."""
    t = feats.rules.get("level")
    lv = []
    for a, b, k in level_ranges(feats, i):
        if k != "bridge" or b - a <= MIN_PART:
            continue
        best = 1.0
        if t is not None and len(t.feature):
            for j in t.rows(i):
                if t.end[j] > a and t.start[j] < b:
                    try:
                        best = max(best, float(t.value[j]))
                    except (TypeError, ValueError):
                        pass
        lv.append(best)
    rings = sum(1 for part in feats.parts(i) for ring in part if len(ring) >= 2)
    return lv * rings


def ground_parts(feats, i):
    """The on-ground pieces of feature i (lon/lat polylines): its rings minus the bridge / tunnel ranges."""
    return _parts_by_level(feats, i, "ground")


def bridge_parts(feats, i):
    """The bridge pieces of feature i (lon/lat polylines)."""
    return _parts_by_level(feats, i, "bridge")


def tunnel_parts(feats, i):
    """The tunnel pieces of feature i (lon/lat polylines), each end made longer where its portal would stand on a road
    it passes under (_clear_portals)."""
    memo = feats.__dict__.setdefault("_parts_by_level", {})
    got = memo.get((i, "tunnel+"))
    if got is None:
        got = memo[(i, "tunnel+")] = _clear_portals(feats, i, _parts_by_level(feats, i, "tunnel"))
    return list(got)


PORTAL_CLEAR_M = 1.0        # m a tunnel's portal face keeps from the edge of a carriageway it passes under, its tube
PORTAL_EXTEND_MAX_M = 25.0  # going on straight up to this far past the mapped end for it (an underpass mapped short: its
                            # portal face stood across the E6's lanes in Norway, GitHub #2)
PORTAL_JOIN_M = 1.5         # a road this near a tunnel's end meets it there (a garage ramp ending in a street), not over it
UNDERPASS_MAX_M = 80.0      # only tunnel parts this short (an underpass) under roads of PORTAL_CLASSES are carried on
PORTAL_CLASSES = {"motorway", "trunk", "primary", "secondary"}


def _clear_portals(feats, i, parts):
    """parts (lon/lat polylines of feature i's tunnel ranges) with each end near a road the tube passes under (a road
    on the ground the part crosses, not joined at that end) carried on straight until the portal face (roadmesh: the
    tube's half width and its frame either side) is PORTAL_CLEAR_M clear of its carriageway and the mouth past the
    ground graded with it (grading.T_MAX beyond its edge: the floor at a mouth is that ground's height, and a mouth on
    the road's level put the tube's end over the road); an end that does not clear within PORTAL_EXTEND_MAX_M stays.
    Pure."""
    if not parts:
        return parts
    ts = tunnel_specs(feats)
    if ts[i] is None:
        return parts
    import roadmesh
    half = ts[i].width / 2 + roadmesh.TUNNEL_SHOULDER + roadmesh.PORTAL_FRAME
    rs = road_specs(feats)
    out = []
    for ring in parts:
        ring = np.asarray(ring, np.float64)
        kx = 111320.0 * math.cos(math.radians(float(ring[0, 1])))
        if float(np.hypot(*(np.diff(ring, axis=0) * [kx, 111320.0]).T).sum()) <= UNDERPASS_MAX_M:
            for at_start in (True, False):
                ring = _clear_end(feats, i, rs, ring, at_start, half)
        out.append(ring)
    return out


def _crosses(a, b):
    """Do the polylines a and b (k, 2) cross?"""
    for p, q in zip(a[:-1], a[1:]):
        r = q - p
        c, d = b[:-1], b[1:]
        s_ = d - c
        den = r[0] * s_[:, 1] - r[1] * s_[:, 0]
        ok = np.abs(den) > 1e-12
        w = c - p
        t = np.where(ok, (w[:, 0] * s_[:, 1] - w[:, 1] * s_[:, 0]) / np.where(ok, den, 1.0), -1.0)
        u = np.where(ok, (w[:, 0] * r[1] - w[:, 1] * r[0]) / np.where(ok, den, 1.0), -1.0)
        if np.any(ok & (t >= 0) & (t <= 1) & (u >= 0) & (u <= 1)):
            return True
    return False


def _clear_end(feats, i, rs, ring, at_start, half):
    import grading
    p0 = ring[0] if at_start else ring[-1]
    kx, ky = 111320.0 * math.cos(math.radians(p0[1])), 111320.0      # m per degree there
    m = (ring - p0) * [kx, ky]
    tail = m if at_start else m[::-1]                                  # from the end inwards
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(tail, axis=0).T))]
    if s[-1] < 1.0:
        return ring
    q = tail[min(np.searchsorted(s, 3.0), len(tail) - 1)]
    d = -q / max(float(np.hypot(*q)), 1e-9)                            # out of the tunnel
    nrm = np.array([-d[1], d[0]])
    reach = (half + PORTAL_EXTEND_MAX_M + 40.0) / ky                   # roads that could matter, in degrees
    roads_m = []
    for j in range(len(feats)):
        if j == i or rs[j] is None or rs[j].cls not in PORTAL_CLASSES:
            continue
        for g in ground_parts(feats, j):
            g = np.asarray(g, np.float64)
            if g[:, 1].min() > p0[1] + reach or g[:, 1].max() < p0[1] - reach or \
                    (g[:, 0].min() - p0[0]) * kx / ky > reach or (p0[0] - g[:, 0].max()) * kx / ky > reach:
                continue
            gm = (g - p0) * [kx, ky]
            a_, d_ = gm[:-1], np.diff(gm, axis=0)
            t_ = np.clip(-(a_ * d_).sum(1) / np.maximum((d_ * d_).sum(1), 1e-9), 0.0, 1.0)
            if float(np.hypot(*(a_ + t_[:, None] * d_).T).min()) < PORTAL_JOIN_M or not _crosses(m, gm):
                continue                        # joined here (an end at it or a T junction) / not over the tunnel
            hw = rs[j].width / 2
            roads_m.append((gm, ((0, hw + grading.T_MAX), (1, hw + PORTAL_CLEAR_M), (2, hw + PORTAL_CLEAR_M))))

    def clear(c):
        pts = np.array([c, c + nrm * half, c - nrm * half])
        for gm, needs in roads_m:
            a, b = gm[:-1], gm[1:]
            dd = b - a
            L2 = np.maximum((dd * dd).sum(1), 1e-9)
            for k, need in needs:
                t = np.clip(((pts[k] - a) * dd).sum(1) / L2, 0.0, 1.0)
                if np.hypot(*(a + t[:, None] * dd - pts[k]).T).min() < need:
                    return False
        return True
    if not roads_m or clear(np.zeros(2)):
        return ring
    for ext in np.arange(1.0, PORTAL_EXTEND_MAX_M + 1e-6, 1.0):
        if clear(d * ext):
            k = max(1, int(math.ceil(ext / 4.0)))
            add = np.array([d * ext * (t / k) for t in range(1, k + 1)]) / [kx, ky] + p0
            return np.vstack([add[::-1], ring]) if at_start else np.vstack([ring, add])
    return ring


def surface_kind(cls, surface):
    """'paved', 'cobble', 'gravel' or 'dirt' from the class and the surface rule value (None if unknown)."""
    if surface is not None:
        s = str(surface).lower()
        if s in PAVED:
            return "paved"
        if s in COBBLE:
            return "cobble"
        if s in GRAVEL:
            return "gravel"
        return "dirt"
    return "paved" if STYLE.get(cls, STYLE["unknown"])[1] else "dirt"


def carriageway(cls, kind, urban, rule_width=None, oneway=False):
    """Paved / dirt width (m) of a road: the width rule if any, else the class's driving width plus
    parking lanes (urban paved streets; one side on a one-way street) or edge strips (rural paved
    roads). One-way: ONEWAY_STYLE's driving width (motorways and trunks are per carriageway already).
    Pure."""
    if rule_width is not None:
        return rule_width
    w = STYLE.get(cls, STYLE["unknown"])[0]
    if oneway and cls in ONEWAY_STYLE:
        w = ONEWAY_STYLE[cls]
    if kind not in BUILT:
        return w
    if urban and not oneway and cls in URBAN_ARTERIAL:
        return URBAN_ARTERIAL[cls]          # four lanes, no parking (a walled street is measured instead)
    if urban and cls in PARKING_CLASSES:
        return w + (1 if oneway else 2) * PARKING_M
    if (not urban or cls == "motorway") and cls != "service":
        if cls == "motorway":
            return MOTORWAY_RURAL_M + 2 * shoulder_width(cls, oneway=oneway)
        return w + 2 * EDGE_STRIP_M
    return w


def bridge_carriageway(cls, kind, rule_width=None, oneway=False):
    """1.5.5 cross-section defaults for bridge decks, unaffected by the new road-width defaults."""
    if rule_width is not None:
        return rule_width
    w = STYLE.get(cls, STYLE["unknown"])[0]
    if oneway and cls in ONEWAY_STYLE:
        w = ONEWAY_STYLE[cls]
    if kind not in BUILT:
        return w
    if cls != "service":
        if oneway and cls == "motorway":
            return BRIDGE_MOTORWAY_RURAL_M + 2 * BRIDGE_SHOULDER_M
        return w + 2 * EDGE_STRIP_M
    return w


def shoulder_width(cls, rule_width=None, oneway=False):
    """Default paved shoulder allowance; a mapped width remains authoritative."""
    if cls != "motorway" or rule_width is not None:
        return EDGE_STRIP_M
    return (MOTORWAY_INNER_SHOULDER_M + MOTORWAY_SHOULDER_M) / 2 if oneway else MOTORWAY_SHOULDER_M


def shoulder_bias(cls, rule_width=None, oneway=False):
    """Offset an asymmetric one-way motorway pavement toward its wider outer shoulder."""
    return (MOTORWAY_SHOULDER_M - MOTORWAY_INNER_SHOULDER_M) / 2 if (
        cls == "motorway" and rule_width is None and oneway) else 0.0


def pavement_half(spec):
    """Conservative reach either side of the driving centreline, for grading and keep-out tests."""
    return spec.width / 2 + abs(spec.bias)


def pavement_offset(spec, left_hand=False):
    """Signed offset from the driving centreline to the full pavement centre."""
    if spec.kind not in BUILT:
        return 0.0
    return spec.bias * (-1 if spec.dir >= 0 else 1) * (-1 if left_hand else 1)


def pavement_line(spec, pts, left_hand=False):
    return offset_polyline(pts, pavement_offset(spec, left_hand)) if spec.bias else np.asarray(pts).copy()


def driving_width(spec):
    """Driving width, excluding paved shoulders and parking lanes."""
    if spec.lanes is not None:
        return float(sum(spec.lanes) * spec.lane_w)
    if spec.cls == "motorway":
        shoulder = spec.shoulder if spec.kind in BUILT else 0.0
        return float(max(min(spec.width, MOTORWAY_LANE_M), spec.width - 2 * shoulder))
    if spec.urban and not spec.oneway and spec.cls in URBAN_ARTERIAL:
        return float(spec.width)
    width = STYLE.get(spec.cls, STYLE["unknown"])[0]
    if spec.oneway and spec.cls in ONEWAY_STYLE:
        width = ONEWAY_STYLE[spec.cls]
    return float(min(spec.width, width))


def lane_counts(spec):
    """Directional driving lanes; paved shoulders are never counted as lanes."""
    if spec.lanes is not None:
        f, b, _ = spec.lanes
        return (f + b, 0) if spec.oneway else (f, b)
    count = max(1, int(math.floor(driving_width(spec) / LANE_M + 1e-9)) if spec.oneway
                else int(round(driving_width(spec) / LANE_M)))
    return (count, 0) if spec.oneway else (max(1, count // 2), max(1, count // 2))


def rural_motorway(spec):
    """One carriageway of a rural motorway: two lanes between shoulders (carriageway)."""
    return spec.cls == "motorway" and spec.oneway and not spec.urban and spec.kind in BUILT


def edge_margin(spec):
    """m of a drawn carriageway beside its driving lanes on each side, parking aside: a rural road's edge strip or
    a rural motorway's shoulder (carriageway), 0 in towns."""
    if spec.urban or spec.kind not in BUILT or spec.cls == "service":
        return 0.0
    return motorway_shoulder(spec) if rural_motorway(spec) else EDGE_STRIP_M


def motorway_shoulder(spec):
    """A rural motorway carriageway's shoulder: SHOULDER_M, or on a ramp (one lane, LINK_M) what one LINK_LANE_M lane
    leaves (GitHub #107: a 6 m ramp's edge lines stood 1.6 m apart, inside the motorway's 2.2 m shoulders)."""
    if spec.cls == "motorway":
        return float(shoulder_width(spec.cls, oneway=spec.oneway))
    return float(np.clip((spec.width - LINK_LANE_M) / 2, EDGE_LINE["inset"], SHOULDER_M))


def bridge_motorway_shoulder(spec):
    """The pre-port bridge edge-line offset, kept with the existing bridge cross-section."""
    return float(np.clip((spec.width - LINK_LANE_M) / 2, EDGE_LINE["inset"], BRIDGE_SHOULDER_M))


def _dir_kmh(feats, i, ncon, lengths=None):
    """(one-way direction along the geometry +1 / -1 or 0, speed limit km/h or None, m per junction or None)
    of feature i; ncon: connectors per feature (None: unknown); lengths: _lengths(feats) (None: measured
    here)."""
    rules = feats.rules
    v = _rule_value(rules.get("oneway"), i)
    d = 0 if v is None else (-1 if float(v) < 0 else 1)
    k = _rule_value(rules.get("max_speed_kmh"), i)
    gap = None
    if ncon is not None and ncon[i] >= 2:
        L = (lengths if lengths is not None else _lengths(feats, i))[i if lengths is not None else 0]
        if L is not None and not np.isnan(L):
            gap = float(L) / (ncon[i] - 1)
    return d, (float(k) if k is not None and float(k) > 0 else None), gap


def _lengths(feats, only=None):
    """Length (m, lon scaled by the cos of each feature's first latitude) of every feature's geometry, NaN for
    fewer than two points; only: just that feature (an array of one). All features at once: one numpy pass
    instead of ~3000 small ones a cell."""
    po, ro, co = feats.part_off, feats.ring_off, feats.coord_off
    idx = np.arange(len(feats)) if only is None else np.array([only])
    a = np.asarray(co[ro[po[idx]]], np.int64)
    b = np.asarray(co[ro[po[idx + 1]]], np.int64)
    out = np.full(len(idx), np.nan)
    ll = np.asarray(feats.lonlat, np.float64)
    cnt = b - a
    if not len(idx) or cnt.sum() < 2:
        return out
    m = 111320.0
    fid = np.repeat(np.arange(len(idx)), cnt)
    pos = np.concatenate([np.arange(x, y) for x, y in zip(a, b)]) if only is not None else np.arange(a[0], b[-1])
    pl = ll[pos]
    same = fid[1:] == fid[:-1]
    c = np.cos(np.radians(ll[np.minimum(a, len(ll) - 1), 1]))
    seg = np.hypot(np.diff(pl[:, 0]) * m * c[fid[:-1]], np.diff(pl[:, 1]) * m)
    L = np.bincount(fid[:-1][same], weights=seg[same], minlength=len(idx))
    ok = cnt >= 2
    out[ok] = L[ok]
    return out


DUAL_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary"}
DUAL_GAP_M = (4.0, 45.0)    # centreline to centreline of a divided road's two carriageways
DUAL_SHARE = 0.6            # this share of the shorter one runs beside the other ...
DUAL_ANGLE_DEG = 25.0       # ... parallel within this


def _dual_side(a, b):
    """For polylines a, b (k, 2) in metres: the share of a's points (every ~10 m) with b beside it (DUAL_GAP_M,
    parallel within DUAL_ANGLE_DEG) and the mean side b is on (+1 left / -1 right of a's direction)."""
    import fast
    fast.require(0, "dual_side")       # the Rust kernel (~25k calls a Las Vegas road tile set)
    return fast._ef.dual_side(np.ascontiguousarray(a, np.float64), np.ascontiguousarray(b, np.float64),
                              DUAL_GAP_M[0], DUAL_GAP_M[1], math.cos(math.radians(DUAL_ANGLE_DEG)))


def dual_carriageways(feats, specs, left_hand=False):
    """specs with the two carriageways of a divided road made one-way when the map says neither is (Boston St,
    Baltimore: two "primary" roads either side of a wide median, each with its own double yellow centre line, the
    user's report 2026-10-14). Two roads of one name and class running side by side DUAL_GAP_M apart over most of
    the shorter one; each runs so that the other is on its left (driving on the right) or right. Pure."""
    groups = {}
    names = feats.attrs.get("name")
    if names is None:
        return specs
    for i, sp in enumerate(specs):
        if sp is None or sp.cls not in DUAL_CLASSES:     # (one-way already: paired for its median only)
            continue
        nm = str(names[i] or "").strip()
        if nm and nm != "None":
            groups.setdefault((nm, sp.cls), []).append(i)
    out = list(specs)
    for idx in groups.values():
        if len(idx) < 2:
            continue
        lines = {}
        for i in idx:
            a = feats.coord_off[feats.ring_off[feats.part_off[i]]]
            b = feats.coord_off[feats.ring_off[feats.part_off[i + 1]]]
            ll = feats.lonlat[a:b]
            lines[i] = ll
        lat0 = float(np.mean(np.concatenate(list(lines.values()))[:, 1]))
        m = np.array([111320.0 * math.cos(math.radians(lat0)), 110540.0])
        ref = lines[idx[0]][0]
        xy = {i: (ll - ref) * m for i, ll in lines.items()}
        for i in idx:
            best = None
            for j in idx:
                if i == j:
                    continue
                share, side = _dual_side(xy[i], xy[j])
                if share >= DUAL_SHARE and abs(side) > 0.5:
                    # the nearest of the carriageways beside it (by share, cells holding different neighbours
                    # paired one piece of Boston Street 20 m and 38 m away: a sidewalk on its median in one tile)
                    gap = _centre_gap(xy[i], xy[j])
                    if best is None or gap < best[3]:
                        best = (share, side, j, gap)
            if best is None:
                continue
            sp = out[i]
            # driving on the right the other carriageway is on the left
            gap = best[3]
            if sp.oneway:               # the map's own one-way carriageway: its direction stands
                out[i] = sp._replace(median=gap, width=_dual_width(sp, sp.width, gap, feats, i))
                continue
            d = (1 if best[1] > 0 else -1) * (-1 if left_hand else 1)
            w = sp.width if _rule_value(feats.rules.get("width"), i) is not None else \
                carriageway(sp.cls, sp.kind, sp.urban, None, True)
            out[i] = sp._replace(oneway=True, dir=d, width=_dual_width(sp, w, gap, feats, i), median=gap)
    return out


URBAN_MEDIAN_M = 1.5    # m: a city's divided road (centre lines within URBAN_MEDIAN_GAP_M): a narrow concrete median,
URBAN_MEDIAN_GAP_M = 24.0   # its carriageways out to it (Boston Street, Mapillary 2026-10-17: two lanes and parking
URBAN_DUAL_MAX_W = 12.0     # each way either side of a ~1.5 m curb; we drew 6.5 m carriageways and a 4.5 m strip)


DUAL_MIN_MEDIAN_M = 0.6    # m between a divided road's carriageways at the least (a concrete barrier): the class's width
                            # overlapped them (Munich's McGraw-Graben: two 11 m carriageways 9.6 m apart, wider than its
                            # trench, their edges up its walls)
DUAL_MIN_W = 3.5            # ... but a carriageway keeps one lane


def dual_cap(gap, w):
    """w capped so carriageways gap m apart (centre to centre) do not overlap (DUAL_MIN_MEDIAN_M between them)."""
    return float(min(w, max(gap - DUAL_MIN_MEDIAN_M, DUAL_MIN_W)))


def _dual_width(sp, w, gap, feats, i):
    """A divided road carriageway's width: a mapped width stands; in a city with its centre lines within
    URBAN_MEDIAN_GAP_M, out to an URBAN_MEDIAN_M median (at most URBAN_DUAL_MAX_W); never overlapping the other
    (dual_cap)."""
    if _rule_value(feats.rules.get("width"), i) is not None:
        return w
    if sp.urban and gap <= URBAN_MEDIAN_GAP_M:
        w = float(np.clip(gap - URBAN_MEDIAN_M, w, URBAN_DUAL_MAX_W))
    return dual_cap(gap, w)


def _centre_gap(a, b):
    """The median distance (m) from polyline a's points to polyline b (both (k, 2) in metres) where b is beside it
    (within DUAL_GAP_M): a divided road's centre lines apart."""
    import fast
    fast.require(0, "polyline_dist")
    d = np.asarray(fast._ef.polyline_dist(np.ascontiguousarray(b, np.float64), np.ascontiguousarray(a, np.float64)))
    beside = d[d <= DUAL_GAP_M[1]]       # where it runs beside the other (a longer piece's far end inflated it 2x)
    return float(np.median(beside)) if len(beside) else float(np.median(d))


def road_specs(feats, urban_fn=None, widths=None, left_hand=False):
    """Per feature: None (left out) or (width m, surface kind). urban_fn(lat, lon) -> bool array (built-up
    land cover), None = rural everywhere; called once for every candidate's points (its cost is per
    call: 2026-10-11, one call per road made 4.4 of a physics block's 6 s). Pure given urban_fn.
    Remembered on feats (mapdata keeps decoded cells in memory: every tile of a cell asked again)."""
    memo = feats.__dict__.setdefault("_road_specs", {})
    k = (urban_fn, widths, bool(left_hand))     # a bound method equals another of the same object and function
    got = memo.get(k)
    if got is None:
        got = _road_specs(feats, urban_fn, widths, left_hand)
        context = feats.__dict__.get('_road_context')
        if context is not None:
            for i, spec in enumerate(got):
                if spec is not None and (spec.cls in {'motorway', 'trunk'} or spec.link):
                    source, j = context.feature(feats, i)
                    got[i] = road_specs(source, urban_fn, widths, left_hand)[j]
        memo[k] = got
    return list(got)


def _road_specs(feats, urban_fn, widths, left_hand):
    out = []
    rules = feats.rules
    ask = []                                    # (index in out, cls, kind, rule_w, coordinate range)
    cr = rules.get("connectors")
    ncon = np.bincount(np.asarray(cr.feature), minlength=len(feats)) if cr is not None and len(cr.feature) else None
    lens = _lengths(feats) if ncon is not None else None
    for i in range(len(feats)):
        cls = str(feats.attrs["class"][i]) if "class" in feats.attrs else "unknown"
        if str(feats.attrs.get("subtype", ["road"] * len(feats))[i]) != "road" or cls not in STYLE:
            out.append(None)
            continue
        rngs = level_ranges(feats, i)
        if rngs and sum(b - a for a, b, _ in rngs) > 1.0 - MIN_PART:     # entirely off the ground
            out.append(None)
            continue
        width = _rule_value(rules.get("width"), i)
        rule_w = float(width) if width is not None and 1.0 <= float(width) <= 40.0 else None
        kind = surface_kind(cls, _rule_value(rules.get("surface"), i))
        ow = _rule_value(rules.get("oneway"), i) is not None
        if rule_w is None and _rule_value(rules.get("subclass"), i) == "link":
            rule_w = LINK_M[1] if ow else LINK_M[2]
        dk = _dir_kmh(feats, i, ncon, lens)
        if urban_fn is not None and rule_w is None and kind in BUILT:
            a = feats.coord_off[feats.ring_off[feats.part_off[i]]]
            b = feats.coord_off[feats.ring_off[feats.part_off[i + 1]]]
            if b > a:
                ask.append((len(out), cls, kind, a, b, ow, dk))
                out.append(None)
                continue
        link = _rule_value(rules.get("subclass"), i) == "link"
        out.append(Spec(carriageway(cls, kind, False, rule_w, ow), kind, False, cls, ow, 2.5, *dk,
                        shoulder=shoulder_width(cls, rule_w, ow), bias=shoulder_bias(cls, rule_w, ow), link=link))
    if ask:
        ll = np.concatenate([feats.lonlat[a:b] for _, _, _, a, b, _, _ in ask])
        u = np.asarray(urban_fn(ll[:, 1], ll[:, 0]), bool)
        k = 0
        for j, cls, kind, a, b, ow, dk in ask:
            urban = float(np.mean(u[k:k + (b - a)])) >= URBAN_SHARE
            k += b - a
            link = _rule_value(rules.get("subclass"), j) == "link"
            out[j] = Spec(carriageway(cls, kind, urban, None, ow), kind, urban, cls, ow, 2.5, *dk,
                          shoulder=shoulder_width(cls, oneway=ow), bias=shoulder_bias(cls, oneway=ow), link=link)
    out = dual_carriageways(feats, out, left_hand)
    if widths is not None:                      # measured between the buildings (roadwidth.py)
        for j, sp in enumerate(out):
            if sp is None or sp.kind not in BUILT or sp.cls not in SIDEWALK_CLASSES or sp.cls in MEASURE_SKIP:
                continue
            if _rule_value(rules.get("width"), j) is not None:
                continue
            if sp.median:       # one carriageway of a divided road: the width between the buildings is both of them
                continue        # and the median (each took all of it, one wide road over Boston Street's median)
            a = feats.coord_off[feats.ring_off[feats.part_off[j]]]
            b = feats.coord_off[feats.ring_off[feats.part_off[j + 1]]]
            m = widths(str(feats.ids[j]), feats.lonlat[a:b]) if b > a else None
            dflt = carriageway(sp.cls, sp.kind, True, None, sp.oneway)
            # Historic streets can be one shared lane between close building fronts. Rejecting these measurements
            # kept a default carriageway wider than the street itself.
            minimum = 3.0 if sp.cls in {"residential", "living_street", "unclassified", "tertiary"} else 0.6 * dflt
            if m is not None and minimum <= m[0] <= MEASURE_MAX * dflt:
                out[j] = sp._replace(width=m[0], walk=m[1])
    # OSM's directional through-lane counts supersede the class estimate on exact way matches.
    # The associated lane model is evaluated in the tile frame, so count changes ease across the
    # source-way joins without touching bridge decks (which use bridge_specs independently).
    import roadlanes
    out = [roadlanes.apply_spec(sp, feats, j) if sp is not None else None for j, sp in enumerate(out)]
    out = roadlanes.continuations(feats, out)
    return out


def bridge_specs(feats):
    """Per feature: the Spec of a road bridge (flag is_bridge, or level above ground; not a tunnel), else
    None. No parking widths on bridges (urban False). Pure."""
    got = feats.__dict__.get("_bridge_specs")      # remembered: cells stay decoded (mapdata)
    if got is None:
        got = feats.__dict__["_bridge_specs"] = _bridge_specs(feats)
    return list(got)


def tunnel_specs(feats):
    """Per feature: the Spec of a road with a tunnel range, else None (as bridge_specs). Pure."""
    got = feats.__dict__.get("_tunnel_specs")
    if got is None:
        got = feats.__dict__["_tunnel_specs"] = _bridge_specs(feats, "tunnel")
    return list(got)


def _bridge_specs(feats, want="bridge"):
    out = []
    rules = feats.rules
    for i in range(len(feats)):
        cls = str(feats.attrs["class"][i]) if "class" in feats.attrs else "unknown"
        if str(feats.attrs.get("subtype", ["road"] * len(feats))[i]) != "road" or cls not in STYLE:
            out.append(None)
            continue
        if not any(k == want for _, _, k in level_ranges(feats, i)):
            out.append(None)
            continue
        width = _rule_value(rules.get("width"), i)
        rule_w = float(width) if width is not None and 1.0 <= float(width) <= 40.0 else None
        kind = surface_kind(cls, _rule_value(rules.get("surface"), i))
        ow = _rule_value(rules.get("oneway"), i) is not None
        if rule_w is None and _rule_value(rules.get("subclass"), i) == "link":
            rule_w = LINK_M[1] if ow else LINK_M[2]
        spec = Spec(bridge_carriageway(cls, kind, rule_w, ow), kind, False, cls, ow)
        if want == 'bridge':
            # Paint uses the same travel direction/counts as the approaches. Keep the
            # upstream deck profile; enlarge a deck only when its tagged lanes need it.
            direction, kmh, gap = _dir_kmh(feats, i, None)
            shoulder = (spec.width-MOTORWAY_RURAL_M)/2 if (
                cls == 'motorway' and ow and rule_w is None) else EDGE_STRIP_M
            spec = spec._replace(dir=direction, kmh=kmh, gap=gap, shoulder=shoulder,
                                 link=is_link(feats, i))
            import roadlanes
            paint = roadlanes.apply_spec(spec, feats, i, allow_bridge=True)
            spec = paint._replace(width=max(spec.width, paint.width))
        out.append(spec)
    return out


def is_link(feats, i):
    return _rule_value(feats.rules.get("subclass"), i) == "link"


def _interpolate_road(xy, boundaries=(), end_dirs=None, source_arc=None, bend_m=12.):
    """Interpolating, tangent-continuous cubic road curves, sampled at two metre intervals.

    Mapped vertices (junctions included) stay in place. Rule boundaries are sampled at their
    original distance parameter, so the bridge and both approaches use exactly the same cut.
    """
    delta = np.diff(xy, axis=0)
    lengths = np.linalg.norm(delta, axis=1)
    if len(xy) < 2 or lengths.sum() < 1e-6:
        return xy, np.linspace(0., 1., len(xy))
    if (lengths < 1e-6).any():
        keep = np.r_[True, lengths >= 1e-6]
        xy = xy[keep]
        source_arc = source_arc[keep] if source_arc is not None else None
        delta = np.diff(xy, axis=0)
        lengths = np.linalg.norm(delta, axis=1)
    unit = delta / lengths[:, None]
    tangent = np.vstack([unit[0], unit[:-1]+unit[1:], unit[-1]])
    tangent /= np.maximum(np.linalg.norm(tangent, axis=1), 1e-9)[:, None]
    if np.linalg.norm(xy[0]-xy[-1]) < .01:
        joint = unit[0]+unit[-1]
        tangent[0] = tangent[-1] = joint/max(np.linalg.norm(joint), 1e-9)
    elif end_dirs is not None:
        for k, direction in ((0, end_dirs[0]), (-1, end_dirs[1])):
            if direction is not None:
                tangent[k] = direction
    arc = (np.r_[0., np.cumsum(lengths)] / lengths.sum() if source_arc is None else source_arc.copy())
    for boundary in boundaries:
        k = int(np.argmin(np.abs(arc-boundary)))
        if 0 < k < len(arc)-1 and abs(arc[k]-boundary)*lengths.sum() < .01:
            arc[k] = boundary       # float32 range fractions can differ by micrometres from a mapped vertex
    out, params = [xy[0]], [float(arc[0])]
    for k, length in enumerate(lengths):
        a, b = xy[k:k+2]
        ta, tb = tangent[k:k+2]
        # Do not overshoot a hairpin or tiny mapped stub. Ordinary road curves retain their
        # shared tangent; a reversed segment is intentionally kept as a hard turn.
        if min(ta @ unit[k], tb @ unit[k]) < .2:
            ta = tb = unit[k]
        curved = max(abs(unit[k, 0]*ta[1]-unit[k, 1]*ta[0]),
                     abs(unit[k, 0]*tb[1]-unit[k, 1]*tb[0])) > 1e-6
        t = np.linspace(0., 1., max(2, int(math.ceil(length/2.))+1)) if curved else np.array([0., 1.])
        ha = min(length/3., bend_m/max(abs(unit[k, 0]*ta[1]-unit[k, 1]*ta[0]), 1e-9))
        hb = min(length/3., bend_m/max(abs(unit[k, 0]*tb[1]-unit[k, 1]*tb[0]), 1e-9))
        t = t[:, None]
        curve = (1-t)**3*a + 3*(1-t)**2*t*(a+ta*ha) + 3*(1-t)*t*t*(b-tb*hb) + t**3*b
        distance = np.r_[0., np.cumsum(np.linalg.norm(np.diff(curve, axis=0), axis=1))]
        param = arc[k]+distance/max(distance[-1], 1e-9)*(arc[k+1]-arc[k])
        at = np.unique(np.r_[param, [v for v in boundaries if arc[k]+1e-10 < v < arc[k+1]-1e-10]])
        curve = np.c_[np.interp(at, param, curve[:, 0]), np.interp(at, param, curve[:, 1])]
        out.extend(curve[1:])
        params.extend(at[1:])
    return np.asarray(out), np.asarray(params)


def _smooth_road(xy, boundaries=(), end_dirs=None, source_arc=None, bend_m=12., fixed=(), _closed_context=False):
    """Fair ordinary map bends without forcing an S-curve through every survey vertex.

    Quintic corner arcs have zero curvature where they meet the straight approaches. Their
    control polygon turns in one direction, so a single mapped bend cannot develop a reverse
    turn. Junctions are fixed; all other points may move by at most half bend_m. The original
    distance parameter still locates rule cuts identically in every consumer of the curve.
    """
    xy = np.asarray(xy, float)
    lengths = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    if len(xy) < 2 or lengths.sum() < 1e-6:
        return xy, np.linspace(0., 1., len(xy))
    keep = np.r_[True, lengths >= 1e-6]
    xy = xy[keep]
    lengths = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    arc = (np.r_[0., np.cumsum(lengths)]/lengths.sum() if source_arc is None else np.asarray(source_arc)[keep])
    protected = {int(np.argmin(abs(arc-v))) for v in fixed}
    unit = np.diff(xy, axis=0)/lengths[:, None]
    if len(xy) == 2:
        return _interpolate_road(xy, boundaries, end_dirs, arc, bend_m)
    closed = np.linalg.norm(xy[0]-xy[-1]) < .01
    if closed and not _closed_context:
        # Include the two neighbouring corners on either side of the seam, then slice it.
        padded = np.vstack([xy[-3:-1], xy, xy[1:3]])
        ap = np.r_[arc[-3:-1]-1., arc, 1.+arc[1:3]]
        fit, param = _smooth_road(padded, source_arc=ap, bend_m=bend_m,
                                  fixed=np.r_[fixed, np.asarray(fixed)-1., np.asarray(fixed)+1.],
                                  _closed_context=True)
        at = np.unique(np.r_[0., param[(param > 0.) & (param < 1.)], 1., boundaries])
        return np.c_[np.interp(at, param, fit[:, 0]), np.interp(at, param, fit[:, 1])], at
    out, params = [xy[0]], [float(arc[0])]
    for k in range(1, len(xy)-1):
        u, v = unit[k-1:k+1]
        cosine = float(np.clip(u @ v, -1., 1.))
        if cosine > 1.-1e-10 or cosine < -.9:
            out.append(xy[k]); params.append(float(arc[k]))
            continue
        half_sine = math.sqrt((1.-cosine)*.5)
        # Narrow streets retain more of small mapped S-bends beside building fronts.
        share = .48 if bend_m >= 8. else .32
        handle = .32 if bend_m >= 8. else .4
        reach = min(share*lengths[k-1], share*lengths[k], bend_m/((2.-3.125*handle)*half_sine))
        a, b = xy[k]-u*reach, xy[k]+v*reach
        controls = np.array([a, a+u*handle*reach, a+u*2.*handle*reach,
                             b-v*2.*handle*reach, b-v*handle*reach, b])
        t = np.linspace(0., 1., max(8, int(math.ceil(2.*reach/1.5)))+1)
        weights = np.array([math.comb(5,j)*(1.-t)**(5-j)*t**j for j in range(6)]).T
        curve = weights @ controls
        pa = arc[k]-(arc[k]-arc[k-1])*reach/lengths[k-1]
        pb = arc[k]+(arc[k+1]-arc[k])*reach/lengths[k]
        if k in protected:
            # Keep an actual crossing/AI connector exact, without forcing all the other
            # shape vertices on this road back into an interpolating spline.
            curve, at = _interpolate_road(np.array([a, xy[k], b]), end_dirs=(u, v),
                                          source_arc=np.array([pa, arc[k], pb]), bend_m=bend_m)
        else:
            at = pa+(pb-pa)*t
        out.extend(curve); params.extend(at)
    out.append(xy[-1]); params.append(float(arc[-1]))
    out, params = np.asarray(out), np.asarray(params)
    # Spread a connected endpoint's tangent over its approach, rather than forcing the
    # change into the first 1-2 m sample. Real interior connectors remain pinned.
    if end_dirs is not None and len(out) >= 2:
        for reverse, direction in ((False, end_dirs[0]), (True, end_dirs[1])):
            if direction is None:
                continue
            p = out[::-1] if reverse else out
            ps = params[::-1] if reverse else params
            tangent = -np.asarray(direction) if reverse else np.asarray(direction)
            chord = p[1]-p[0]
            chord /= max(np.linalg.norm(chord), 1e-9)
            if tangent @ chord > 1.-1e-9:
                continue
            distance = np.r_[0., np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]
            reach = min(90. if bend_m >= 8. else 25., .4*distance[-1])
            for value in fixed:
                increasing = ps[::-1] if reverse else ps
                along = np.interp(value, increasing, distance[::-1]) if reverse else np.interp(value, ps, distance)
                if along > 1e-5:
                    reach = min(reach, .8*along)
            if reach <= 1e-4:
                continue
            q = np.array([np.interp(reach, distance, p[:, d]) for d in (0, 1)])
            j = min(max(1, int(np.searchsorted(distance, reach))), len(p)-1)
            final = p[j]-p[j-1]
            final /= max(np.linalg.norm(final), 1e-9)
            handle = reach/5.
            controls = np.array([p[0], p[0]+tangent*handle, p[0]+tangent*2.*handle,
                                 q-final*2.*handle, q-final*handle, q])
            t = np.linspace(0., 1., max(8, int(math.ceil(reach/1.5)))+1)
            weights = np.array([math.comb(5, j)*(1.-t)**(5-j)*t**j for j in range(6)]).T
            segment = weights @ controls
            travelled = np.r_[0., np.cumsum(np.linalg.norm(np.diff(segment, axis=0), axis=1))]
            target = np.interp(reach, distance, ps)
            at = ps[0]+travelled/travelled[-1]*(target-ps[0])
            keep = distance > reach+1e-8
            p, ps = np.vstack([segment, p[keep]]), np.r_[at, ps[keep]]
            out, params = (p[::-1], ps[::-1]) if reverse else (p, ps)
    # Float32 rule fractions occasionally differ from an original vertex by micrometres.
    for boundary in boundaries:
        k = int(np.argmin(np.abs(params-boundary)))
        if abs(params[k]-boundary)*lengths.sum() < .01:
            params[k] = boundary
    at = np.unique(np.r_[params, boundaries])
    return np.c_[np.interp(at, params, out[:, 0]), np.interp(at, params, out[:, 1])], at


def _fair_highway_shape(xy, fixed, sigma=35., max_shift=3.):
    """Remove small sideways jogs before rounding, within a three metre map corridor.

    Smooth by distance, so densely surveyed points do not receive extra weight. Real junctions
    and endpoints stay exact; the correction fades near them to retain the approach direction.
    """
    lengths = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    arc = np.r_[0., np.cumsum(lengths)]
    if len(xy) < 3 or arc[-1] < 4.:
        return xy.copy()
    count = int(math.ceil(arc[-1]/2.))
    step = arc[-1]/count
    radius = int(math.ceil(3.*sigma/step))
    at = np.arange(-radius, count+radius+1)*step
    closed = np.linalg.norm(xy[0]-xy[-1]) < .01
    probe = np.mod(at, arc[-1]) if closed else at
    samples = np.c_[np.interp(probe, arc, xy[:, 0]), np.interp(probe, arc, xy[:, 1])]
    if not closed:
        first = next((k for k, length in enumerate(lengths) if length > 1e-6), None)
        last = next((k for k in range(len(lengths)-1, -1, -1) if lengths[k] > 1e-6), None)
        if first is None or last is None:
            return xy.copy()
        samples[at < 0.] = xy[0]+at[at < 0., None]*(xy[first+1]-xy[first])/lengths[first]
        samples[at > arc[-1]] = xy[-1]+(at[at > arc[-1]]-arc[-1])[:, None]*(xy[last+1]-xy[last])/lengths[last]
    kernel = np.exp(-.5*(np.arange(-radius, radius+1)*step/sigma)**2)
    kernel /= kernel.sum()
    filtered = np.column_stack([np.convolve(samples[:, d], kernel, mode='valid') for d in (0, 1)])
    fitted = np.column_stack([np.interp(arc, at[radius:-radius], filtered[:, d]) for d in (0, 1)])
    delta = fitted-xy
    delta *= np.minimum(1., max_shift/np.maximum(np.linalg.norm(delta, axis=1), 1e-9))[:, None]
    locks = np.unique(np.r_[0, np.asarray(fixed, dtype=np.intp), len(xy)-1]).astype(np.intp)
    distance = np.min(abs(arc[:, None]-arc[locks][None, :]), axis=1)
    t = np.clip(distance/30., 0., 1.)
    weight = t**3*(10.-15.*t+6.*t*t)
    out = xy+delta*weight[:, None]
    out[locks] = xy[locks]
    return out


def _geometry_nodes(feats):
    """Raw, shared endpoint headings, without recursing through fitted geometry."""
    got = feats.__dict__.get("_geometry_nodes")
    if got is not None:
        return got
    got = {}
    fixed = set()
    for j in range(len(feats)):
        if is_link(feats, j):
            for part in feats.parts(j):
                for ring in part:
                    if len(ring):
                        fixed.update(tuple(np.round(p[:2]*1e7).astype(np.int64)) for p in ring[[0, -1]])
            continue
        for part in feats.parts(j):
            for ring in part:
                for k, p in enumerate(ring):
                    key = tuple(np.round(p[:2]*1e7).astype(np.int64))
                    for other in (k-1, k+1):
                        if 0 <= other < len(ring):
                            got.setdefault(key, []).append((j, ring[other, :2]-p[:2]))
    # Keep the topology keyed by the original map coordinates, but use the same faired
    # neighbours for both halves of every shared endpoint curve.
    shapes = {}
    for j in range(len(feats)):
        if is_link(feats, j):
            continue
        for pi, part in enumerate(feats.parts(j)):
            for ri, ring in enumerate(part):
                if level_ranges(feats, j):
                    continue
                if len(ring) < 3 or str(feats.attrs.get('class', ['unknown']*len(feats))[j]) not in {'motorway', 'trunk'}:
                    continue
                ref = ring[0, :2]
                scale = np.array([111320.*math.cos(math.radians(float(ref[1]))), 110540.])
                xy = (ring[:, :2]-ref)*scale
                locks = {0, len(ring)-1}
                for k, p in enumerate(ring):
                    key = tuple(np.round(p[:2]*1e7).astype(np.int64))
                    if len(got.get(key, [])) != 2 or key in fixed:
                        locks.add(k)
                connectors = feats.rules.get('connectors')
                if connectors is not None:
                    seg = np.hypot(np.diff(ring[:, 0])*math.cos(math.radians(float(ring[:, 1].mean()))), np.diff(ring[:, 1]))
                    arc = np.r_[0., np.cumsum(seg)]/max(seg.sum(), 1e-15)
                    locks.update(int(np.argmin(abs(arc-float(connectors.start[k])))) for k in connectors.rows(j))
                lock = sorted(locks)
                shape = _fair_highway_shape(xy, lock)/scale+ref
                shape[lock] = ring[lock, :2]
                shapes[(j, pi, ri)] = shape
    if shapes:
        got = {}
        for j in range(len(feats)):
            if is_link(feats, j):
                continue
            for pi, part in enumerate(feats.parts(j)):
                for ri, ring in enumerate(part):
                    shape = shapes.get((j, pi, ri), ring[:, :2])
                    for k, p in enumerate(ring):
                        key = tuple(np.round(p[:2]*1e7).astype(np.int64))
                        for other in (k-1, k+1):
                            if 0 <= other < len(ring):
                                got.setdefault(key, []).append((j, shape[other]-shape[k]))
    feats.__dict__["_road_shapes"] = shapes
    feats.__dict__["_geometry_nodes"] = got
    feats.__dict__["_geometry_fixed"] = fixed
    return got


def _ramp_tangent(xy, main, direction):
    """A ramp leaves/joins the through carriageway tangentially, without moving its shared node."""
    out = xy.copy()
    for reverse in (False, True):
        p = out[::-1] if reverse else out
        delta = np.diff(p, axis=0)
        arc = np.r_[0., np.cumsum(np.linalg.norm(delta, axis=1))]
        if arc[-1] < 12.:
            continue
        inward = p[1]-p[0]
        inward /= max(np.linalg.norm(inward), 1e-9)
        best = None
        for fid, line, spec in main:
            ab = np.diff(line, axis=0)
            lengths = np.linalg.norm(ab, axis=1)
            t = np.clip(((p[0]-line[:-1])*ab).sum(1)/np.maximum(lengths**2, 1e-12), 0., 1.)
            dist = np.linalg.norm(line[:-1]+t[:, None]*ab-p[0], axis=1)
            k = int(np.argmin(dist))
            if dist[k] > 1.:
                continue
            heading = ab[k]/max(lengths[k], 1e-9)
            if spec.dir < 0:
                heading = -heading
            away = (direction >= 0) != reverse
            tangent = heading if away else -heading
            if not spec.oneway and tangent @ inward < 0:
                tangent = -tangent
            alignment = float(tangent @ inward)
            if alignment < math.cos(math.radians(65.)):
                continue                       # a ramp's terminal T-junction, not a freeway merge
            score = (-driving_width(spec), -alignment, fid)
            if best is None or score < best[0]:
                best = (score, tangent)
        if best is None:
            continue
        reach = min(90., .45*arc[-1])
        j = min(int(np.searchsorted(arc, reach)), len(p)-1)
        q = np.array([np.interp(reach, arc, p[:, d]) for d in (0, 1)])
        end = p[j]-p[j-1]
        end /= max(np.linalg.norm(end), 1e-9)
        # Balanced handles spread the turn along the whole approach. The old long first
        # handle delayed all lateral movement, then forced a short, sharp turn at its end.
        # Keep their projections ordered so a tight ramp cannot fold backwards.
        advance = max(1.,float((q-p[0]) @ best[1]))
        first = min(reach/5., .225*advance)
        final = reach/5.
        along = float(end @ best[1])
        if along > .05:
            final = min(final,max(.5,advance-2.*first)/(2.*along))
        t = np.linspace(0., 1., max(8, int(math.ceil(reach/2.))+1))[:, None]
        controls = np.array([p[0], p[0]+best[1]*first, p[0]+best[1]*2.*first,
                             q-end*2.*final, q-end*final, q])
        curve = sum(math.comb(5, k)*(1.-t)**(5-k)*t**k*control for k, control in enumerate(controls))
        p = np.vstack([curve, p[arc > reach]])
        out = p[::-1] if reverse else p
    return out


def _joined_end_curve(feats, i, ring, end, nodes, bend):
    """Half of one rounded corner shared by two ordinary source features.

    Fit in the shared node's own coordinate frame: either source order and either cell then
    compute the same join. A ramp or crossing fixes the node and uses the junction tangent
    instead. Moving an ordinary degree-two shape node avoids the S-curve that would otherwise
    be needed to interpolate it from a straight approach on each side.
    """
    node = ring[end, :2]
    key = tuple(np.round(node*1e7).astype(np.int64))
    neighbours = nodes.get(key, [])
    if len(neighbours) != 2 or key in feats.__dict__.get('_geometry_fixed', set()):
        return None
    if any(level_ranges(feats, j) for j, _ in neighbours):
        return None
    others = [(j, v) for j, v in neighbours if j != i]
    if len(others) != 1:
        return None
    j, other = others[0]
    if feats.attrs.get('class', ['unknown']*len(feats))[j] != feats.attrs.get('class', ['unknown']*len(feats))[i]:
        return None
    scale = np.array([111320.*math.cos(math.radians(float(node[1]))), 110540.])
    own = (ring[1 if end == 0 else -2, :2]-node)*scale
    other = other*scale
    la, lb = np.linalg.norm(other), np.linalg.norm(own)
    if min(la, lb) < 1e-5:
        return None
    cosine = float(np.clip((-other/la) @ (own/lb), -1., 1.))
    if cosine < math.cos(math.radians(35.)) or cosine > 1.-1e-10:
        return None
    factor = 1. if bend >= 8. else .75
    reach = min(.48*la, .48*lb, bend/(factor*math.sqrt((1.-cosine)*.5)))
    fit, at = _smooth_road(np.array([other, [0., 0.], own]), boundaries=(0.,),
                           source_arc=np.array([-la, 0., lb])/(la+lb), bend_m=bend)
    keep = (at >= -1e-10) & (at <= reach/(la+lb)+1e-10)
    return fit[keep]/scale+node, at[keep]*(la+lb)/lb


def _endpoint_tangents(feats, i, ring, xy, nodes, scale):
    """Shared inward/outward directions at pinned source-feature endpoints."""
    ends = []
    for k, other in ((0, 1), (-1, -2)):
        u = xy[other]-xy[k]
        u /= max(np.linalg.norm(u), 1e-9)
        candidates = []
        key = tuple(np.round(ring[k, :2]*1e7).astype(np.int64))
        neighbours = nodes.get(key, [])
        for j, v in neighbours if len(neighbours) == 2 else ():
            if j == i:
                continue
            v = v*scale
            v /= max(np.linalg.norm(v), 1e-9)
            if u @ v < -math.cos(math.radians(35.)):
                candidates.append(v)
        v = min(candidates, key=lambda v: u @ v) if candidates else -u
        t = u-v
        t /= max(np.linalg.norm(t), 1e-9)
        ends.append(t if k == 0 else -t)
    return ends


def _ground_level_geometry(feats, i, spec):
    """Smooth ground sections of a mixed-level feature, with exact, tangent-matched portals.

    A small bridge flag on a many-kilometre highway must not disable all its ground curves.
    Keep off-ground geometry raw and fit each ground interval in the original source-distance
    parameter. Bridge/tunnel callers continue to use the original source coordinates.
    """
    ranges = level_ranges(feats, i)
    nodes = _geometry_nodes(feats)
    fixed_nodes = feats.__dict__.get('_geometry_fixed', set())
    connectors = feats.rules.get('connectors')
    cuts = sorted({0., 1., *(v for a, b, _ in ranges for v in (a, b))})
    bend = 12. if spec is not None and spec.cls in {'motorway', 'trunk'} else 4.
    result = []
    for pi, part in enumerate(feats.parts(i)):
        rings = []
        for ri, ring in enumerate(part):
            if len(ring) < 2:
                rings.append(ring)
                continue
            ref = ring[0, :2]
            scale = np.array([111320.*math.cos(math.radians(float(ref[1]))), 110540.])
            raw_xy = (ring[:, :2]-ref)*scale
            seg = np.hypot(np.diff(ring[:, 0])*math.cos(math.radians(float(ring[:, 1].mean()))), np.diff(ring[:, 1]))
            length_m = float(seg.sum())*111320.
            arc = np.r_[0., np.cumsum(seg)]/max(seg.sum(), 1e-15)
            ends = _endpoint_tangents(feats, i, ring, raw_xy, nodes, scale)
            fixed = [float(connectors.start[j]) for j in connectors.rows(i)] if connectors is not None else []
            for k, p in enumerate(ring):
                key = tuple(np.round(p[:2]*1e7).astype(np.int64))
                if len(nodes.get(key, ())) != 2 or key in fixed_nodes:
                    fixed.append(float(arc[k]))
            lines, params = [], []
            for a, b in zip(cuts[:-1], cuts[1:]):
                off_ground = any(start <= (a+b)/2 <= end for start, end, _ in ranges)
                # A float32 rule cut can be a fraction of a millimetre from a raw vertex.
                # Keep its exact portal, without a duplicate ground sample next to it.
                margin = 0. if off_ground else .01/max(length_m, 1e-9)
                at = np.unique(np.r_[a, arc[(arc > a+margin) & (arc < b-margin)], b])
                ll = np.c_[np.interp(at, arc, ring[:, 0]), np.interp(at, arc, ring[:, 1])]
                if not off_ground and b-a > MIN_PART:
                    xy = (ll-ref)*scale
                    local = (at-a)/(b-a)
                    pinned = [(v-a)/(b-a) for v in fixed if a < v < b]
                    pinned = [v for v in pinned if 0 < int(np.argmin(abs(local-v))) < len(local)-1]
                    locks = {0, len(xy)-1, *(int(np.argmin(abs(local-v))) for v in pinned)}
                    if spec is not None and spec.cls in {'motorway', 'trunk'} and not spec.link:
                        # Retain the mapped approach directions at an exact junction.
                        # Moving its nearest shape samples while pinning only the node
                        # introduces a small S-bend on an otherwise straight approach.
                        shape_locks = {j for k in locks for j in (k-1, k, k+1) if 0 <= j < len(xy)}
                        xy = _fair_highway_shape(xy, sorted(shape_locks))
                    directions = list(ends)
                    for end, cut, side in ((0, a, 'left'), (1, b, 'right')):
                        if (end == 0 and a > 0.) or (end == 1 and b < 1.):
                            k = int(np.clip(np.searchsorted(arc, cut, side=side)-1, 0, len(ring)-2))
                            d = raw_xy[k+1]-raw_xy[k]
                            directions[end] = d/max(np.linalg.norm(d), 1e-9)
                    smooth, param = _smooth_road(xy, end_dirs=directions, source_arc=local,
                                                 bend_m=bend, fixed=pinned)
                    fitted = smooth/scale+ref
                    fitted[[0, -1]] = ll[[0, -1]]
                    ll, at = fitted, a+param*(b-a)
                lines.append(ll if not lines else ll[1:])
                params.append(at if not params else at[1:])
            rings.append(np.concatenate(lines))
            feats.__dict__.setdefault('_geometry_params', {})[(i, pi, ri)] = np.concatenate(params)
        result.append(rings)
    return result


def geometry_parts(feats, i):
    """Shared road geometry for rendering, grading, materials, bridges and the AI graph.

    Fit against the whole source feature's neighbourhood, before clipping: road skins,
    paint, terrain and AI use identical curves on either side of a source-cell boundary.
    """
    ranges = level_ranges(feats, i)
    if ranges and sum(b-a for a, b, _ in ranges) >= 1.-MIN_PART:
        return feats.parts(i)
    memo = feats.__dict__.setdefault("_road_geometry", {})
    if i in memo:
        return memo[i]
    context = feats.__dict__.get('_road_context')
    cls = str(feats.attrs.get('class', ['unknown'] * len(feats))[i])
    if context is not None and (cls in {'motorway', 'trunk'} or is_link(feats, i)):
        source, j = context.feature(feats, i)
        result = geometry_parts(source, j)
        params = source.__dict__.get('_geometry_params', {})
        for part, rings in enumerate(result):
            for ring in range(len(rings)):
                param = params.get((j, part, ring))
                if param is not None:
                    feats.__dict__.setdefault('_geometry_params', {})[(i, part, ring)] = param
        memo[i] = result
        return result
    specs = road_specs(feats)
    bs = bridge_specs(feats)
    if ranges:
        result = memo[i] = _ground_level_geometry(feats, i, specs[i] or bs[i])
        return result
    link = is_link(feats, i)
    mains = [(str(feats.ids[j]), geometry_parts(feats, j), sp or bs[j]) for j, sp in enumerate(specs)
             if not is_link(feats, j) and (sp or bs[j]) is not None
             and (sp or bs[j]).cls in {"motorway", "trunk"}] if link else []
    if link:
        # Raw neighbour geometry avoids recursive ramp fitting while giving
        # interstate connector chains the same shared endpoint tangent.
        mains += [(str(feats.ids[j]),feats.parts(j),sp or bs[j]) for j,sp in enumerate(specs)
                  if j!=i and is_link(feats,j) and (sp or bs[j]) is not None
                  and (sp or bs[j]).cls in {'motorway','trunk'}]
    direction, _, _ = _dir_kmh(feats, i, None)
    result = []
    nodes = _geometry_nodes(feats) if not link else {}
    for pi, part in enumerate(feats.parts(i)):
        rings = []
        for ri, ring in enumerate(part):
            if len(ring) < 2:
                rings.append(ring)
                continue
            ref = np.asarray(ring[0, :2])
            scale = np.array([111320.*math.cos(math.radians(float(ref[1]))), 110540.])
            xy = (np.asarray(ring[:, :2])-ref)*scale
            main = [(fid, (r[:, :2]-ref)*scale, sp) for fid, parts, sp in mains
                    for pp in parts for r in pp if len(r) >= 2]
            if link:
                xy = _ramp_tangent(_smooth_road(xy, bend_m=8.)[0], main, direction)
            else:
                shape = feats.__dict__.get('_road_shapes', {}).get((i, pi, ri), ring[:, :2])
                xy = (shape-ref)*scale
                sp = specs[i] or bs[i]
                bend = 12. if sp is not None and sp.cls in {'motorway', 'trunk'} else 4.
                joined = [_joined_end_curve(feats, i, shape, k, nodes, bend) for k in (0, -1)]
                ends = _endpoint_tangents(feats, i, ring, xy, nodes, scale)
                ends = [None if join is not None else end for join, end in zip(joined, ends)]
                boundaries = [v for a, b, _ in level_ranges(feats, i) for v in (a, b)]
                seg = np.hypot(np.diff(ring[:, 0])*math.cos(math.radians(float(ring[:, 1].mean()))), np.diff(ring[:, 1]))
                source_arc = np.r_[0., np.cumsum(seg)]/max(seg.sum(), 1e-15)
                connectors = feats.rules.get('connectors')
                fixed = [float(connectors.start[j]) for j in connectors.rows(i)] if connectors is not None else []
                junctions = feats.__dict__.get('_geometry_fixed', set())
                for k in range(1, len(ring)-1):
                    key = tuple(np.round(ring[k, :2]*1e7).astype(np.int64))
                    if len(nodes.get(key, [])) != 2 or key in junctions:
                        fixed.append(float(source_arc[k]))
                xy, param = _smooth_road(xy, boundaries, ends, source_arc, bend, fixed)
                for reverse, join in zip((False, True), joined):
                    if join is None:
                        continue
                    ll, fraction = join
                    head = (ll-ref)*scale
                    at = 1.-fraction*(1.-source_arc[-2]) if reverse else fraction*source_arc[1]
                    if reverse:
                        keep = param < at[-1]-1e-10
                        xy, param = np.vstack([xy[keep], head[::-1]]), np.r_[param[keep], at[::-1]]
                    else:
                        keep = param > at[-1]+1e-10
                        xy, param = np.vstack([head, xy[keep]]), np.r_[at, param[keep]]
                if any(j is not None for j in joined):
                    at = np.unique(np.r_[param, boundaries])
                    xy = np.c_[np.interp(at, param, xy[:, 0]), np.interp(at, param, xy[:, 1])]
                    param = at
                feats.__dict__.setdefault("_geometry_params", {})[(i, pi, ri)] = param
            rings.append(xy/scale+ref)
        result.append(rings)
    memo[i] = result
    return result


def _road_connections(roads, links):
    """Pairs whose paint must continue across a segment boundary, and freeway ramp merges."""
    nodes = {}
    for fid, sp, pts, _ in roads:
        for k, p in enumerate(pts):
            key = (round(float(p[0])*2), round(float(p[1])*2))
            nodes.setdefault(key, []).append((fid, sp, pts, k))
    continuous, merges = set(), {}
    for entries in nodes.values():
        if len(entries) < 2:
            continue
        arms=sum(1 if k in (0,len(p)-1) else 2 for _,_,p,k in entries)
        if arms==2:
            a,b=entries
            if a[1].cls==b[1].cls and a[1].oneway==b[1].oneway:
                continuous.add(frozenset((a[0],b[0])))
            continue
        # A connector can itself be the through route at a freeway fork.
        # Select its best aligned incoming/outgoing pair, then attach only the
        # remaining branch. Never treat two ends of one link chain as a ramp.
        main_links=set()
        if all(fid in links for fid,_,_,_ in entries):
            choices=[]
            for ai,a in enumerate(entries):
                fid,sp,p,k=a
                u=p[1,:2]-p[0,:2] if k==0 else p[-2,:2]-p[-1,:2]
                u/=max(np.linalg.norm(u),1e-9)
                for b in entries[ai+1:]:
                    oid,osp,op,ok=b
                    if fid==oid or sp.cls!=osp.cls:continue
                    v=op[1,:2]-op[0,:2] if ok==0 else op[-2,:2]-op[-1,:2]
                    v/=max(np.linalg.norm(v),1e-9)
                    dot=float(-u@v)
                    if dot>.8:choices.append((dot,min(driving_width(sp),driving_width(osp)),sorted((fid,oid))))
            if choices:
                main_links=set(max(choices,key=lambda r:(round(r[0],2),r[1],r[2]))[2])
                continuous.add(frozenset(main_links))
        for fid, sp, pts, k in entries:
            if fid in links and fid not in main_links:
                continue
            a, b = max(0,k-1), min(len(pts)-1,k+1)
            u = pts[b,:2]-pts[a,:2]
            u /= max(np.linalg.norm(u),1e-9)
            for other, osp, op, ok in entries:
                if other == fid:
                    continue
                a2, b2 = max(0,ok-1), min(len(op)-1,ok+1)
                v = op[b2,:2]-op[a2,:2]
                v /= max(np.linalg.norm(v),1e-9)
                if abs(u @ v) < math.cos(math.radians(35.)):
                    continue
                if other in links and other not in main_links and sp.cls in {"motorway", "trunk"}:
                    merges.setdefault(fid,{})[other] = (op[:,:2], osp.width/2)
                elif sp.cls == osp.cls:
                    continuous.add(frozenset((fid,other)))
    return continuous, merges


def _nearest_road(points, lines, with_owner=False):
    """Nearest source cross-section and driving half-width on a connected through road."""
    best = np.full(len(points), np.inf)
    q = np.zeros((len(points), 5))
    half = np.zeros(len(points))
    owners = np.zeros(len(points),int)
    for index,(line, width) in enumerate(lines):
        a, ab = line[:-1], np.diff(line, axis=0)
        t = np.clip(((points[:, None, :2]-a[None, :, :2])*ab[None, :, :2]).sum(2)
                    / np.maximum((ab[:, :2]**2).sum(1), 1e-12), 0., 1.)
        d = ((points[:, None, :2]-a[None, :, :2]-t[:, :, None]*ab[None, :, :2])**2).sum(2)
        k = d.argmin(1)
        dist = d[np.arange(len(points)), k]
        use = dist < best
        q[use] = a[k[use]]+t[use, k[use], None]*ab[k[use]]
        if np.ndim(width):
            half[use] = np.interp(k[use]+t[use,k[use]], np.arange(len(line)), width)
        else:
            half[use] = width
        best[use] = dist[use]
        owners[use]=index
    return (q,half,owners) if with_owner else (q,half)


def _auxiliary_lane(node, away, mains, side, lane, stops=()):
    """Follow the through road back from the ramp, across source-feature boundaries.

    The returned order points toward the ramp node. For an entrance this is opposite traffic,
    so the same geometry produces a downstream acceleration lane and closing taper.
    Ground parts only: a bridge/tunnel gap ends the available extension.
    """
    choices = []
    for index, (line, _) in enumerate(mains):
        ab = np.diff(line[:, :2], axis=0)
        lengths = np.linalg.norm(ab, axis=1)
        t = np.clip(((node[:2]-line[:-1, :2])*ab).sum(1)/np.maximum(lengths**2, 1e-12), 0., 1.)
        dist = np.linalg.norm(line[:-1, :2]+t[:, None]*ab-node[:2], axis=1)
        k = int(dist.argmin())
        if dist[k] > 1.:
            continue
        p = np.vstack([node[:3], line[:k+1, :3][::-1]])
        p = p[np.r_[True, np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1) > 1e-6]]
        if len(p) > 1 and (p[1, :2]-p[0, :2]) @ away < 0:
            choices.append((np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1).sum(), index, p))
    if not choices:
        return None
    _, current, p = max(choices, key=lambda v: (v[0], -v[1]))
    used = {current}
    target = AUX_LANE_M+AUX_TAPER_M
    for _ in range(len(mains)):
        arc = np.r_[0., np.cumsum(np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1))]
        if arc[-1] >= target:
            break
        heading = p[-1, :2]-p[-2, :2]
        heading /= max(np.linalg.norm(heading), 1e-9)
        options = []
        for index, (line, _) in enumerate(mains):
            if index in used or np.linalg.norm(line[-1, :2]-p[-1, :2]) > .5:
                continue
            v = line[-2, :2]-line[-1, :2]
            alignment = v @ heading/max(np.linalg.norm(v), 1e-9)
            if alignment > .8:
                options.append((alignment, index, line))
        if not options:
            break
        _, current, line = max(options, key=lambda v: (v[0], -v[1]))
        used.add(current)
        p = np.vstack([p, line[-2::-1, :3]])
    arc = np.r_[0., np.cumsum(np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1))]
    length = min(target, float(arc[-1]))
    for stop in stops:
        ab = np.diff(p[:, :2], axis=0)
        t = np.clip(((stop[:2]-p[:-1, :2])*ab).sum(1)/np.maximum((ab*ab).sum(1), 1e-12), 0., 1.)
        dist = np.linalg.norm(p[:-1, :2]+t[:, None]*ab-stop[:2], axis=1)
        k = int(dist.argmin())
        at = arc[k]+t[k]*(arc[k+1]-arc[k])
        if dist[k] < 1. and at > 25.:
            length = min(length, max(0., at-20.))
    if length < 35.:
        return None
    # The first point is exactly where the taper reaches the existing highway shoulder.
    at = np.linspace(length, 0., max(3, int(math.ceil(length/2.))+1))
    path = np.column_stack([np.interp(at, arc, p[:, d]) for d in range(3)])
    q, half = _nearest_road(path, mains)
    # A mapped additional lane may already occupy part/all of the approach. Use its
    # existing width instead of either adding a duplicate lane or omitting the whole join.
    _, ahead = _nearest_road(np.asarray([node[:3]+np.r_[away*180.,0.]]),mains)
    base_half = float(min(half.min(),ahead[0]))
    allocated = np.clip(2.*(half-base_half),0.,lane)
    normal = q[:, 3:5]
    outward = normal*side/np.maximum(np.linalg.norm(normal, axis=1), 1e-9)[:, None]
    taper = min(AUX_TAPER_M, .3*length)
    distance = np.r_[0., np.cumsum(np.linalg.norm(np.diff(path[:, :2], axis=0), axis=1))]
    t = np.clip(distance/taper, 0., 1.)
    opening = t*t*(3.-2.*t)
    boundary = q[:, :3].copy()
    boundary[:, :2] += normal*side*half[:, None]
    guide = boundary.copy()
    guide[:,:2] -= normal*side*allocated[:,None]
    extra = np.maximum(0.,lane*opening-allocated)
    outer = boundary.copy()
    outer[:, :2] += outward*extra[:, None]
    # Paved outer shoulder remains 3 m wide. The inner edge overlaps existing pavement by
    # 0.2 m; the cross-section width grows with the taper, rather than making a square stub.
    widths = 3.2+extra
    centre = boundary.copy()
    centre[:, :2] += outward*(1.4+.5*extra)[:, None]
    return {'pts': centre, 'widths': widths, 'outer': outer, 'guide': guide,
            'base_half':base_half,'taper': int(np.searchsorted(distance, taper)), 'lane': lane}


def _fair_edge_offset(delta, arc, minimum, end):
    """A rounded outside envelope, measured in metres along the ramp, not vertex indices.

    Filtering a positive offset anticipates the turn instead of holding the line flat until
    the ramp has already turned away. Fade the correction with zero first/second derivatives
    at both ends, so the shared highway node and the untouched ramp paint still meet exactly.
    """
    step, sigma = 1., 12.
    radius = int(3*sigma/step)
    at = np.arange(-radius, int(math.ceil(arc[-1]/step))+radius+1)*step
    values = np.interp(at, arc, delta-minimum)
    slope = (delta[-1]-delta[-2])/max(arc[-1]-arc[-2], 1e-9)
    values[at > arc[-1]] = delta[-1]-minimum+(at[at > arc[-1]]-arc[-1])*slope
    values = np.maximum(values, 0.)
    kernel = np.exp(-.5*(np.arange(-radius, radius+1)*step/sigma)**2)
    kernel /= kernel.sum()
    filtered = np.convolve(values, kernel, mode='valid')
    smooth = np.interp(arc[:end], at[radius:-radius], filtered)
    raw = np.maximum(delta[:end]-minimum, 0.)
    t = np.minimum(np.clip(arc[:end]/20., 0., 1.),
                   np.clip((arc[end-1]-arc[:end])/40., 0., 1.))
    weight = t**3*(10.-15.*t+6.*t*t)
    return minimum+raw+(smooth-raw)*weight


def _ramp_markings(roads, continuous, merges, sections, left_hand, frame=None, allow_aux=True):
    """Coordinate the mainline and ramp edges as a single painted junction.

    Dotted guidance divides the extra lane, including its taper, from the through lanes. A continuous outer white edge
    curves onto the ramp; the two solid gore edges share the through edge until they diverge.
    Clamping to the outside of the through lanes prevents paint from cutting across them.
    """
    by_id = {}
    for fid, sp, p, lon in roads:
        by_id.setdefault(fid, []).append((sp, p, lon))
    connected = {}
    for main, ramps in merges.items():
        for ramp in ramps:
            connected.setdefault(ramp, set()).add(main)
    overrides, zones, exclusions, bands = {}, {}, {}, []
    for fid, main_ids in connected.items():
        for spec, original, lon in by_id[fid]:
            pts = densify(_with_cross(original, sections), 1.)
            paints=extras(spec,pts,lon,left_hand,frame)
            edges = {look['edge_side']: (look, line[:, :3].copy())
                     for look, _, line in paints if 'edge_side' in look}
            native_edges={s:line.copy() for s,(_,line) in edges.items()}
            if not edges:
                continue
            keep = {side: np.ones(len(pts), bool) for side in edges}
            white = {side: np.zeros(len(pts), bool) for side in edges}
            changed = False
            for reverse in (False, True):
                order = np.arange(len(pts))[::-1] if reverse else np.arange(len(pts))
                p = pts[order]
                away = p[1, :2]-p[0, :2]
                away /= max(np.linalg.norm(away), 1e-9)
                ids = {mid for mid in main_ids if any(
                    _seg_dist(p[:1, :2], mp[:-1, :2], mp[1:, :2]).min() < 1.
                    for _, mp, _ in by_id[mid])}
                if not ids:
                    continue
                # Include successive map features of the same carriageway, including reversed
                # source order. The paint layout must not stop at an arbitrary feature boundary.
                for _ in range(len(roads)):
                    more = set().union(*(pair for pair in continuous if pair & ids)) if continuous else set()
                    if more <= ids:
                        break
                    ids |= more
                mains = []
                for mid in sorted(ids):
                    for msp, mp, _ in by_id[mid]:
                        mp = _with_cross(mp, sections)
                        k = int(np.argmin(_seg_dist(p[:1, :2], mp[:-1, :2], mp[1:, :2])[0]))
                        heading = mp[k+1, :2]-mp[k, :2]
                        if abs(heading @ away)/max(np.linalg.norm(heading), 1e-9) < .7:
                            continue
                        if heading @ away < 0:
                            mp = mp[::-1].copy()
                            mp[:, 3:5] *= -1
                        if msp.lanes is not None:
                            import roadlanes
                            section_counts = roadlanes.counts(msp, mp, frame).sum(1)
                            mains.append((mp, section_counts*msp.lane_w/2))
                        else:
                            mains.append((mp, driving_width(msp)/2 if msp.cls == 'motorway' else msp.width/2-EDGE_LINE['inset']))
                if not mains:
                    continue
                arc = np.r_[0., np.cumsum(np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1))]
                n = min(len(p), int(np.searchsorted(arc, min(400., .8*arc[-1])))+2)
                q, half = _nearest_road(p[:n], mains)
                normal = q[:, 3:5].copy()
                normal /= np.maximum(np.linalg.norm(normal, axis=1), 1e-9)[:, None]
                lateral = ((p[:n, :2]-q[:, :2])*normal).sum(1)
                side = 1 if lateral[np.argmax(np.abs(lateral))] > 0 else -1
                outward = normal*side
                boundary = q[:, :3].copy()
                boundary[:, :2] += q[:, 3:5]*side*half[:, None]
                deltas = {s: ((line[order[:n], :2]-boundary[:, :2])*outward).sum(1)
                          for s, (_, line) in edges.items()}
                outer = max(deltas, key=lambda s: np.mean(deltas[s]))
                inner = -outer
                if inner not in deltas:
                    continue
                lane = min(driving_width(spec),max(1,sum(lane_counts(spec)))*MOTORWAY_LANE_M)
                stops = [point for other in connected if other != fid
                         for _, op, _ in by_id[other] for point in op[[0, -1]]]
                # A mapped lane addition/drop near this branch already provides the auxiliary
                # pavement. Do not append another generated lane beyond that counted lane.
                auxiliary = _auxiliary_lane(p[0], away, mains, side, lane, stops) if allow_aux else None
                if auxiliary is not None:
                    allocated = np.clip(2.*(half-auxiliary['base_half']),0.,lane)
                    boundary[:,:2] -= q[:,3:5]*side*allocated[:,None]
                    deltas = {s: ((line[order[:n],:2]-boundary[:,:2])*outward).sum(1)
                              for s,(_,line) in edges.items()}
                opens = np.flatnonzero(deltas[outer] >= (lane if auxiliary is None else 0.))
                gore = np.flatnonzero(deltas[inner] >= .25)
                clears = np.flatnonzero(deltas[inner] >= 3.)
                if not len(opens) or not len(clears) or not len(gore):
                    continue
                end = min(n, int(np.searchsorted(arc, arc[int(clears[0])]+80.))+1)
                fair = {s: _fair_edge_offset(delta, arc[:n],
                                            lane if auxiliary is not None and s == outer else 0., end)
                        for s, delta in deltas.items()}
                gore = np.flatnonzero(fair[inner] >= .025)
                if not len(gore):
                    continue
                start = max(0, int(gore[0])-1)
                if end <= start+1:
                    continue
                for s, (_, line) in edges.items():
                    delta = deltas[s][:end]
                    line[order[:end], :2] += outward[:end]*(fair[s]-delta)[:, None]
                    begin = start if s == inner else (0 if auxiliary is not None else max(0, int(opens[0])-1))
                    keep[s][order[:begin]] = False
                    white[s][order[:end]] = True
                dotted = boundary[:start+1, :2]
                dotted_outer = edges[outer][1][order[:start+1],:2]
                if auxiliary is not None:
                    # Fill under the new outer edge beyond the shared map node as well.
                    # Blend back into the ramp's own pavement before this added band ends.
                    tail = edges[outer][1][order[:end]].copy()
                    normals = offset_polyline(tail, 1.)[:, :2]-tail[:, :2]
                    normals /= np.maximum(np.linalg.norm(normals, axis=1), 1e-9)[:, None]
                    tail[:, :2] += normals*side*(1.4-lane/2)
                    u = np.clip((arc[:end]-(arc[end-1]-60.))/60., 0., 1.)
                    u = u**3*(10.-15.*u+6.*u*u)
                    tail = tail*(1-u[:, None])+p[:end, :3]*u[:, None]
                    auxiliary['approach_n'] = len(auxiliary['pts'])
                    auxiliary['pts'] = np.vstack([auxiliary['pts'], tail[1:]])
                    auxiliary['widths'] = np.r_[auxiliary['widths'], ((3.2+lane)*(1-u)+spec.width*u)[1:]]
                    auxiliary['fid'] = fid
                    auxiliary['surface'] = spec.surface
                    auxiliary['kind'] = spec.kind
                    main_fid,main_item = min(((mid,item) for mid in ids for item in by_id[mid]),
                                    key=lambda pair: _seg_dist(p[:1,:2],pair[1][1][:-1,:2],pair[1][1][1:,:2]).min())
                    main_spec=main_item[0]
                    auxiliary['main_fid']=main_fid
                    auxiliary['approach_surface'] = main_spec.surface
                    auxiliary['approach_kind'] = main_spec.kind
                    bands.append(auxiliary)
                    # Start at the opening/closing taper, rather than leaving its entire
                    # divider blank until the added lane reaches full width.
                    dotted = np.vstack([auxiliary['guide'][:, :2], dotted])
                    dotted_outer = np.vstack([auxiliary['outer'][:, :2], dotted_outer])
                keep_points=np.r_[True,np.linalg.norm(np.diff(dotted,axis=0),axis=1)>1e-6]
                dotted,dotted_outer=dotted[keep_points],dotted_outer[keep_points]
                for mid in ids:
                    zones.setdefault(mid, []).append({'line': dotted, 'outer': dotted_outer,
                                                      'mode': 'dotted', 'ramp': fid,'mains':sorted(ids)})
                exclusions.setdefault(fid, set()).update(ids)
                changed = True
            if not changed:
                continue
            for look,_,line in paints:
                if 'lane_index' not in look or len(line)!=len(pts):continue
                old=native_edges[1][:,:2]-native_edges[-1][:,:2]
                fraction=((line[:,:2]-native_edges[-1][:,:2])*old).sum(1)/np.maximum((old*old).sum(1),1e-9)
                fitted=edges[-1][1][:,:2]*(1-fraction[:,None])+edges[1][1][:,:2]*fraction[:,None]
                run=line[:,:3].copy();use=white[-1]|white[1];run[use,:2]=fitted[use]
                style={**look,'ramp_part':(tuple(original[0,:2]),tuple(original[-1,:2]))}
                overrides.setdefault((fid,('lane',look['lane_index'])),[]).append((style,run))
            for s, (look, line) in edges.items():
                tags = np.where(keep[s][:-1] & keep[s][1:], (white[s][:-1] | white[s][1:]).astype(int), -1)
                cuts = np.r_[0, np.flatnonzero(tags[1:] != tags[:-1])+1, len(tags)]
                runs = []
                for a, b in zip(cuts[:-1], cuts[1:]):
                    if tags[a] >= 0:
                        style = {**look, 'ramp_part': (tuple(original[0, :2]), tuple(original[-1, :2]))}
                        if tags[a]:
                            style['m'] = 'line_white'
                        runs.append((style, line[a:b+1]))
                overrides.setdefault((fid, s), []).extend(runs)
    return overrides, zones, exclusions, bands


def _ramp_paint_key(look):
    return ('lane',look['lane_index']) if 'lane_index' in look else look.get('edge_side')


def cached_ramp_layout(feats, left_hand=False):
    """One full-cell layout shared by tiles, terrain grading and surface materials.

    Solve before tile filtering: a ramp node outside a tile can have a 270 m auxiliary lane
    inside it. Fitting independently from each tile's nearby roads made the paint drift.
    """
    memo = feats.__dict__.setdefault('_ramp_layouts', {})
    if bool(left_hand) in memo:
        return memo[bool(left_hand)]
    links = {str(feats.ids[i]) for i in range(len(feats)) if is_link(feats, i)}
    if not links:
        memo[bool(left_hand)] = None
        return None
    import geodesy
    ref = feats.lonlat[0]
    frame = geodesy.EnuFrame(float(ref[1]), float(ref[0]), 0.)
    if feats.__dict__.get('_road_context') is not None:
        # projected_ramp_layout selects each ramp's source neighbourhood independently.
        # A neighbouring cell cannot restart or shorten its auxiliary lane.
        result = projected_ramp_layout(frame, [feats], left_hand)
        memo[bool(left_hand)] = frame, result
        return memo[bool(left_hand)]
    rows = []
    for i, sp in enumerate(road_specs(feats, left_hand=left_hand)):
        if sp is None:
            continue
        for ring in ground_parts(feats, i):
            e, n, u = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.)
            rows.append((str(feats.ids[i]), sp, np.c_[e, n, u], float(ring[:, 0].mean())))
    continuous, merges = _road_connections(rows, links)
    result = _ramp_markings(rows, continuous, merges, _join_sections(frame, [feats]), left_hand, frame)
    memo[bool(left_hand)] = (frame, result)
    return memo[bool(left_hand)]


def auxiliary_parts(feats, i, left_hand=False):
    """(lon/lat, Spec) of extra paved lanes owned by this ramp, for physics and road materials."""
    if not is_link(feats, i):
        return []
    got = cached_ramp_layout(feats, left_hand)
    if got is None:
        return []
    frame, (_, _, _, bands) = got
    out = []
    for band in bands:
        if band['fid'] != str(feats.ids[i]):
            continue
        p = band['pts']
        la, lo, _ = frame.enu_to_geodetic(p[:, 0], p[:, 1], p[:, 2])
        sp = Spec(float(max(band['widths'])), 'paved', False, 'motorway', True, shoulder=0., link=True)
        out.append((np.c_[lo, la], sp))
    return out


def auxiliary_sections(band):
    """Use the highway material on the approach, then the ramp material after its node."""
    n=band.get('approach_n',len(band['pts']))
    out=[(slice(0,n),band.get('approach_surface',band.get('surface','')),
          band.get('approach_kind',band.get('kind','paved')))]
    if n<len(band['pts']):
        out.append((slice(n-1,None),band.get('surface',''),band.get('kind','paved')))
    return out


def _outside_mainline(line, width, mains, fixed=False):
    """Trim an added ribbon to the actual edge of the connected highway pavement.

    Main lanes own their surface through a split. Rendering a full ramp/auxiliary ribbon
    underneath them left contrasting triangular strips and coplanar texture fighting.
    Keep cross-sections (including zero-width tips) so clipping cannot shift driving lanes.
    """
    if not mains:return line
    import roadlanes
    line=densify(line,2.)
    cross=roadlanes.cross(line)
    left,right=line[:,:2]+cross*width/2,line[:,:2]-cross*width/2
    q,half,owners=_nearest_road(line,mains,with_owner=True)
    normal=q[:,3:5]
    norm=np.maximum(np.linalg.norm(normal,axis=1),1e-9)
    unit=normal/norm[:,None]
    delta=((line[:,:2]-q[:,:2])*unit).sum(1)
    # A connector may leave one interstate on the right and meet another on
    # the left. One global side clipped the wrong half of its far-end lanes.
    side=np.ones(len(line))
    for owner in np.unique(owners):
        ids=np.flatnonzero(owners==owner)
        chosen=ids[int(np.argmax(np.abs(delta[ids])))]
        side[ids]=1. if delta[chosen]>=0. else -1.
    boundary=q[:,:2]+normal*(side*half)[:,None]
    da=((left-boundary)*unit).sum(1)*side
    db=((right-boundary)*unit).sum(1)*side
    near=np.linalg.norm(line[:,:2]-q[:,:2],axis=1)<=half*norm+width*2+3.
    if fixed:
        residual=line[:,:2]-q[:,:2]-delta[:,None]*unit
        near&=np.linalg.norm(residual,axis=1)<.5
        near&=np.abs(line[:,2]-q[:,2])<.5  # retain flyovers above a connected highway
    crossing=near&((da<0)!=(db<0))
    t=np.clip(da/np.where(np.abs(da-db)>1e-9,da-db,1.),0.,1.)
    hit=left+(right-left)*t[:,None]
    left[crossing&(da<0)]=hit[crossing&(da<0)]
    right[crossing&(db<0)]=hit[crossing&(db<0)]
    inside=near&(da<0)&(db<0)
    left[inside]=right[inside]=boundary[inside]
    centre=(left+right)/2
    return np.c_[centre,line[:,2],(left-right)/width]


def projected_ramp_layout(frame, cells, left_hand=False):
    """Cached full source layouts projected into this tile's anchor frame, with duplicate cells removed."""
    edges, zones, exclusions, bands = {}, {}, {}, []
    def transform(p, source):
        p = np.asarray(p)
        la, lo, _ = source.enu_to_geodetic(p[:, 0], p[:, 1], p[:, 2] if p.shape[1] >= 3 else 0.)
        return np.column_stack(frame.geodetic_to_enu(la, lo, 0.))
    best = {}
    layouts = []
    for feats in cells:
        if feats is None:
            continue
        context = feats.__dict__.get('_road_context')
        if context is None:
            layouts.append((cached_ramp_layout(feats, left_hand), None))
        else:
            for i in range(len(feats)):
                if is_link(feats, i):
                    source, _ = context.feature(feats, i)
                    layouts.append((cached_ramp_layout(source, left_hand), {str(feats.ids[i])}))
    for got, owners in layouts:
        if got is None:
            continue
        source, (paint, guides, cuts, pavement) = got
        for fid in {band['fid'] for band in pavement} | {fid for fid, _ in paint}:
            if owners is not None and fid not in owners:
                continue
            length = max((np.linalg.norm(np.diff(b['guide'][:, :2], axis=0), axis=1).sum()
                          for b in pavement if b['fid'] == fid), default=0.)
            score = (round(float(length), 3), -source.lat0, -source.lon0)
            if fid not in best or score > best[fid][0]:
                best[fid] = (score, got)
    # Prefer the complete approach when a neighbouring source cell has less context.
    # Sorting by id and using a deterministic tie-break also removes cell-order dependence.
    for owner in sorted(best):
        source, (paint, guides, cuts, pavement) = best[owner][1]
        fresh = {owner}
        for key, runs in paint.items():
            if key[0] not in fresh:
                continue
            for look, line in runs:
                part = transform(np.asarray(look['ramp_part']), source)
                style = {**look, 'ramp_part': (tuple(part[0, :2]), tuple(part[-1, :2]))}
                edges.setdefault(key, []).append((style, transform(line, source)))
        # Guide ownership is per ramp, not per through feature; remember that in the cache.
        for fid, runs in guides.items():
            for zone in runs:
                if zone.get('ramp') in fresh:
                    projected={**zone,'line':transform(zone['line'],source)[:,:2]}
                    if 'outer' in zone:projected['outer']=transform(zone['outer'],source)[:,:2]
                    zones.setdefault(fid, []).append(projected)
        for fid in fresh:
            exclusions.setdefault(fid, set()).update(cuts.get(fid, ()))
        for band in pavement:
            if band['fid'] in fresh:
                bands.append({**band, 'pts': transform(band['pts'], source), 'outer': transform(band['outer'], source),
                              'guide': transform(band['guide'], source)})
    return edges, zones, exclusions, bands


def _merge_edge_runs(line, look, zones):
    """Dotted auxiliary-lane divider and solid gore, independent of the pavement overlap."""
    if not zones or not ((look.get("edge_side") is not None and look['m']=='line_white') or look.get('highway_lane')):
        return [(look,line)]
    line = densify(line,1.)
    mid = (line[:-1,:2]+line[1:,:2])/2
    tags = np.zeros(len(mid), int)
    for index, zone in enumerate(zones):
        guide = zone['line']
        if len(guide) > 1:
            if 'outer' in zone:
                # Remove the native shoulder AND any counted lane divider inside
                # the auxiliary lane. One dedicated guide replaces them below.
                # Matching only within 55 cm left the shoulder stripe running
                # diagonally across the ramp whenever OSM counts varied.
                near=_ramp_corridor(mid,guide,zone['outer'])
                tags[near]=-1
            else:
                near = _seg_dist(mid,guide[:-1],guide[1:]).min(1) < .55
                tags[near] = -1 if zone['mode'] == 'erase' else index+1
    starts = np.r_[0, np.flatnonzero(tags[1:] != tags[:-1])+1, len(tags)]
    out = []
    for a,b in zip(starts[:-1],starts[1:]):
        if tags[a] < 0:
            continue
        if tags[a]:
            # Retain each ramp's guide so the phase remains stable across clipped tiles
            # and across adjacent acceleration/deceleration lanes.
            guidance = zones[tags[a]-1]
            style = {**look, 'm': LANE_LINE['m'], 'w': RAMP_DOT_W,
                     'tl': RAMP_DOT_M+RAMP_DOT_GAP_M, 'dash': RAMP_DOT_M, 'gap': RAMP_DOT_GAP_M,
                     'uv_reference': guidance['line'], 'ramp_guidance': True}
        else:
            style = look
        out.append((style,line[a:b+1]))
    return out


def _ramp_corridor(points, guide, outer):
    """Points in the auxiliary lane's swept cross-sections, with square end cuts."""
    a,ab=guide[:-1],np.diff(guide,axis=0)
    l2=np.maximum((ab*ab).sum(1),1e-12)
    t=np.clip(((points[:,None]-a)*ab).sum(2)/l2,0.,1.)
    near=a+t[...,None]*ab
    k=np.linalg.norm(points[:,None]-near,axis=2).argmin(1)
    at=t[np.arange(len(points)),k]
    q=near[np.arange(len(points)),k]
    edge=outer[k]+at[:,None]*(outer[k+1]-outer[k])
    tangent=ab[k]/np.sqrt(l2[k,None])
    side=edge-q
    normal=np.c_[-tangent[:,1],tangent[:,0]]
    normal*=np.where((normal*side).sum(1)<0.,-1.,1.)[:,None]
    across=((points-q)*normal).sum(1)
    width=np.abs((side*normal).sum(1))
    along=np.abs(((points-q)*tangent).sum(1))
    return (across>=-.28)&(across<=width+.28)&(along<.55)


def _ramp_guides(frame,zones,box,elev,fixed_height=None):
    """Paint each shared ramp divider once, independent of native lane/edge runs."""
    seen=set()
    for fid in sorted(zones):
        for zone in zones[fid]:
            if 'outer' not in zone or len(zone['line'])<2:continue
            p=zone['line']
            key=(zone.get('ramp'),tuple(np.round(p[[0,-1]].ravel(),3)))
            if key in seen:continue
            seen.add(key)
            if p[:,0].max()<box[0] or p[:,0].min()>box[2] or p[:,1].max()<box[1] or p[:,1].min()>box[3]:continue
            if fixed_height is not None:
                z=fixed_height(p,zone)
            else:
                la,lo,_=frame.enu_to_geodetic(p[:,0],p[:,1],0.)
                z=frame.geodetic_to_enu(la,lo,np.asarray(elev(la,lo),float))[2]
            reference=np.c_[p,z]
            look={**LANE_LINE,'w':RAMP_DOT_W,'tl':RAMP_DOT_M+RAMP_DOT_GAP_M,
                  'dash':RAMP_DOT_M,'gap':RAMP_DOT_GAP_M,'ramp_guidance':True,'uv_reference':reference}
            for piece in clip_polyline(reference,*box):
                for c in split(densify(simplify(piece,CURVE_SIMPLIFY_M,ztol=.005 if fixed_height is not None else None,
                                                heading=CURVE_HEADING_DEG))):
                    chunk=_chunk(look,RAMP_DOT_W,c,reference)
                    if fixed_height is not None:chunk.update(fixed=True,structure='bridge')
                    yield chunk


def _join_sections(frame, cells):
    """Cross-sections at map vertices, before level/tile/chunk splits, shared by road and deck.

    A normal fitted separately on each clipped part leaves wedges at bends. Joined vertices,
    including adjacent features with opposite vertex order, instead use one mitred section.
    """
    features = {}
    for feats in cells or ():
        if feats is None:
            continue
        for i in range(len(feats)):
            if str(feats.attrs.get("class", ["unknown"] * len(feats))[i]) not in STYLE:
                continue
            if str(feats.attrs.get("subtype", ["road"] * len(feats))[i]) != "road":
                continue
            fid = str(feats.ids[i])
            rings = [r for part in geometry_parts(feats, i) for r in part if len(r) >= 2]
            if fid not in features or sum(map(len, rings)) > sum(map(len, features[fid][1])):
                features[fid] = (feats, rings, is_link(feats, i))
    import roadmesh
    out = {'_uv':{}}
    for fid in sorted(features):
        feats, rings, link = features[fid]
        out['_uv'][fid]=[]
        for ring in rings:
            e, n = mapgeom.enu_xy(feats, frame, ring)
            pts = np.c_[e, n, np.zeros(len(e))]
            out['_uv'][fid].append(roadmesh.PavementReference(pts,ring))
            cross = offset_polyline(pts, 1.)[:, :2] - pts[:, :2]
            for p, v in zip(pts, cross):
                key = (round(float(p[0]) * 2), round(float(p[1]) * 2))
                out.setdefault(key, []).append((p[:2], v, link))
    return out


def source_id(fid):
    """Native OSM feature of a generated fixed approach (other feature IDs unchanged)."""
    return str(fid).split('::approach:',1)[0]


def _uv_reference(sections,fid,points):
    """The complete source curve, before tile, level, lane-width or shoulder clipping."""
    refs=(sections or {}).get('_uv',{}).get(source_id(fid),())
    if not refs:return points
    if len(refs)==1:return refs[0]
    return min(refs,key=lambda ref: _seg_dist(np.asarray(points)[:1,:2],ref.points[:-1,:2],ref.points[1:,:2]).min())


def _with_cross(pts, sections):
    pts = np.asarray(pts, np.float64)
    cross = offset_polyline(pts[:, :3], 1.)[:, :2] - pts[:, :2]
    out = pts[:, :3].copy()
    for i, (p, own) in enumerate(zip(pts, cross)):
        key = (round(float(p[0]) * 2), round(float(p[1]) * 2))
        # The coarse key is an index, not a snapping tolerance. A rule cut can sit only
        # centimetres from another sample on the same curve. Averaging those positions
        # collapses a segment and introduces a little twist into pavement and paint.
        neighbours = [v for v in (sections or {}).get(key, ()) if np.linalg.norm(v[0]-p[:2]) < .002]
        # A branch must not turn the through lanes toward itself. Through-road normals win;
        # ramp-only vertices retain the ramp's own smooth cross-section.
        through = [v for v in neighbours if len(v) < 3 or not v[2]]
        if through:
            neighbours = through
        own_unit = own / max(np.linalg.norm(own), 1e-9)
        near = [(item[0], item[1] * (1 if item[1] @ own_unit >= 0 else -1)) for item in neighbours
                for v in [item[1]]
                if abs(v @ own_unit) / max(np.linalg.norm(v), 1e-9) >= .5]
        if near:
            units = np.array([v / max(np.linalg.norm(v), 1e-9) for _, v in near])
            normal = units.mean(0)
            normal /= max(np.linalg.norm(normal), 1e-9)
            scale = max(max(np.linalg.norm(v) for _, v in near),
                        1. / max(.5, float((units @ normal).min())))
            cross[i] = normal * scale
            out[i, :2] = np.mean([q for q, _ in near], axis=0)
    return np.c_[out, cross]


def _median_links(rows):
    """Identify actual short crossovers with opposing carriageways at their two ends."""
    nodes = {}
    for fid, sp, p, lon in rows:
        if not sp.oneway or sp.link or sp.cls not in {'motorway', 'trunk'} or sp.kind not in BUILT:
            continue
        for k, q in enumerate(p):
            tangent = p[min(k+1, len(p)-1), :2]-p[max(k-1, 0), :2]
            tangent /= max(np.linalg.norm(tangent), 1e-9)
            heading = tangent * (1 if sp.dir >= 0 else -1)
            key = tuple(np.rint(q[:2]*2).astype(np.int64))
            nodes.setdefault(key, []).append((fid, heading))
    selected, pairs, crossovers = set(), set(), set()
    for fid, sp, p, lon in rows:
        length = np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1).sum()
        if sp.kind not in BUILT or not 14. < length < 180.:
            continue
        ends = []
        for k, step in ((0, 1), (len(p)-1, -1)):
            direction = junctions._branch_dir(p, k, step)
            key = tuple(np.rint(p[k, :2]*2).astype(np.int64))
            ends.append([(main, heading) for main, heading in nodes.get(key, ())
                         if main != fid and abs(direction @ heading) < .65])
        if not any(a != b and ha @ hb < -.7 for a, ha in ends[0] for b, hb in ends[1]):
            continue
        selected.add(fid)
        crossovers.add(fid)
        for main, _ in ends[0]+ends[1]:
            selected.add(main)
            pairs.add(frozenset((fid, main)))
    return selected, pairs, crossovers


def median_connections(rows, left_hand=False):
    """Rounded returns meet the actual widened crossover and asymmetric highway pavement."""
    selected, pairs, _ = _median_links(rows)
    if not pairs:
        return [], {}, set()
    subset = [r for r in rows if r[0] in selected]
    corners, cuts, *_ = junctions.find(subset, lambda sp: False, None, paved=lambda fid, sp: True,
                                     edge_half=lambda sp, side: sp.width/2+side*pavement_offset(sp, left_hand),
                                     join_filter=lambda a, b: frozenset((a, b)) in pairs)
    for corner in corners:
        corner['median_crossover'] = True
    return corners, cuts, pairs


def median_layout(frame, cells, left_hand=False, urban_fn=None, widths=None):
    """The same crossover returns for ground materials as for the visible road mesh."""
    seen = {}
    for feats in cells:
        if feats is None:
            continue
        for i, sp in enumerate(road_specs(feats, urban_fn, widths, left_hand)):
            if sp is None:
                continue
            fid = str(feats.ids[i])
            parts = ground_parts(feats, i)
            if fid not in seen or sum(map(len, parts)) > sum(map(len, seen[fid][1])):
                seen[fid] = (sp, parts)
    rows = []
    for fid, (sp, parts) in sorted(seen.items()):
        for ring in parts:
            p = np.column_stack(frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.))
            rows.append((fid, sp, p, float(ring[:, 0].mean())))
    return median_connections(rows, left_hand)


def _chain_graph(lines):
    """Nodes (end points within 0.5 m joined) and edges (start node, end node, arc lengths) of parts."""
    keys, nodes = {}, []

    def node(p):
        k = (round(float(p[0]) * 2), round(float(p[1]) * 2))
        if k not in keys:
            keys[k] = len(nodes)
            nodes.append(np.asarray(p[:2], np.float64))
        return keys[k]
    edges = []
    for ln in lines:
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(ln[:, :2], axis=0).T))]
        edges.append((node(ln[0]), node(ln[-1]), s))
    adj = [[] for _ in nodes]
    for a, b, s in edges:
        adj[a].append((b, s[-1]))
        adj[b].append((a, s[-1]))
    return nodes, edges, adj


def _dijkstra(adj, seeds):
    """Shortest distances along the chain from seeds {node: start distance}."""
    import heapq
    d = np.full(len(adj), np.inf)
    q = []
    for n, d0 in seeds.items():
        if d0 < d[n]:
            d[n] = d0
            q.append((d0, n))
    heapq.heapify(q)
    while q:
        dd, u = heapq.heappop(q)
        if dd > d[u]:
            continue
        for v, w in adj[u]:
            if dd + w < d[v]:
                d[v] = dd + w
                heapq.heappush(q, (dd + w, v))
    return d


MERGE_TOUCH_M = 1.5     # m: decks side by side this close (edge to edge) are pulled together too (the Queensboro
                        # Bridge's upper roadway's two directions met at a 0.5-2 m step, a wall and stubs on it)


def _merge_overlaps(lines, zs, widths, levels, rounds=3):
    """Heights of decks lying over each other (a ramp leaving the mainline, parallel carriageways of
    one level) pulled to their mean where their surfaces overlap, so a split is one surface until the
    decks part (2026-10-11 the user: exits and splits not clean). Parts are related when they share an
    end node or a level and run within ~30 degrees of each other. Abutment ends stay. Pure."""
    nodes, edges, _ = _chain_graph(lines)
    deg = np.zeros(len(nodes), int)
    for a, b, _ in edges:
        deg[a] += 1
        deg[b] += 1
    n = len(lines)
    pairs = []
    # the related pairs (i, j), i then j ascending, as a double loop testing each would find them, one line against
    # all others at a time (that loop was ~4 s of a Fells Point teleport, on the event loop)
    if n:
        ea = np.array([e[0] for e in edges[:n]])
        eb = np.array([e[1] for e in edges[:n]])
        lo = np.array([np.asarray(ln)[:, :2].min(0) for ln in lines])
        hi = np.array([np.asarray(ln)[:, :2].max(0) for ln in lines])
        lv = None if levels is None else np.empty(n, object)
        if lv is not None:
            lv[:] = list(levels)[:n]
        idx = np.arange(n)
        for i in range(n):
            ok = (ea[i] == ea) | (ea[i] == eb) | (eb[i] == ea) | (eb[i] == eb)
            if lv is not None:
                ok |= ~(lv != lv[i]).astype(bool)          # elementwise, the same != as levels[i] != levels[j]
            ok &= ~(hi[i] < lo - 30).any(1) & ~(hi < lo[i] - 30).any(1)
            ok &= idx != i
            pairs.extend((i, int(j)) for j in np.flatnonzero(ok))
    if not pairs:
        return zs
    zs = [np.array(z, np.float64) for z in zs]
    for _ in range(rounds):
        new = [z.copy() for z in zs]
        acc = [np.zeros(len(z)) for z in zs]
        cnt = [np.zeros(len(z)) for z in zs]
        up = [np.zeros(len(z)) for z in zs]     # twin carriageways: how far up to the other's height
        for i, j in pairs:
            A, B = lines[i][:, :2], lines[j][:, :2]
            P, Q = B[:-1], B[1:]
            ab = Q - P
            L2 = np.maximum((ab ** 2).sum(1), 1e-12)
            t = np.clip(((A[:, None, :] - P[None]) * ab[None]).sum(2) / L2[None], 0, 1)
            near = P[None] + t[..., None] * ab[None]
            d = np.hypot(*(A[:, None, :] - near).transpose(2, 0, 1))
            m = np.argmin(d, axis=1)
            dmin = d[np.arange(len(A)), m]
            tj = t[np.arange(len(A)), m]
            zj = zs[j][m] + tj * (zs[j][np.minimum(m + 1, len(zs[j]) - 1)] - zs[j][m])
            ta = np.gradient(A, axis=0)
            tb = ab[m]
            cos = np.abs((ta * tb).sum(1)) / (np.hypot(*ta.T) * np.hypot(*tb.T) + 1e-12)
            # soft: full weight 2 m into the overlap and when parallel, none at its edge (a hard mask
            # made 4 m steps mid-viaduct where a neighbour's footprint started)
            depth = np.clip(((widths[i] + widths[j]) / 2 + MERGE_TOUCH_M - dmin) / 2.0, 0.0, 1.0)
            w = depth * np.clip((cos - 0.85) / 0.1, 0.0, 1.0) * np.isfinite(zj)
            acc[i] += w * np.nan_to_num(zj)
            cnt[i] += w
            if levels is not None and levels[i] == levels[j]:
                # one level's carriageways side by side are one deck (the Tsing Ma Bridge's two directions stood 15 m
                # apart in height, the one over the lower deck lifted by the stacking, 2026-10-18): the lower lifted
                # to the higher, fading out from DUAL_DECK_M apart
                w2 = np.clip((DUAL_DECK_M - dmin) / 10.0, 0.0, 1.0) * np.clip((cos - 0.85) / 0.1, 0.0, 1.0)
                lift = np.where(np.isfinite(zj), w2 * np.maximum(np.nan_to_num(zj) - zs[i], 0.0), 0.0)
                up[i] = np.maximum(up[i], lift)
        for i in range(n):
            m = cnt[i] > 0
            new[i][m] = (zs[i][m] + acc[i][m]) / (1 + cnt[i][m])
            new[i] = np.maximum(new[i], zs[i] + up[i])
            a, b, _ = edges[i]
            if deg[a] == 1:
                new[i][0] = zs[i][0]
            if deg[b] == 1:
                new[i][-1] = zs[i][-1]
        zs = new
    out = []
    for i, z in enumerate(zs):                  # re-smoothed, ends kept
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(lines[i][:, :2], axis=0).T))]
        out.append(_smooth_pinned(z, s[-1] / max(len(s) - 1, 1), DECK_SIGMA) if len(z) > 3 else z)
    return out


DUAL_DECK_M = 30.0      # m: parts of one level parallel within this are one deck's carriageways (_merge_overlaps)


def deck_profile(lines, ground_fn, crossings=(), clear=None, grade=None, lift=None, levels=None, widths=None):
    """deck_profile_once, then repeated with the deck-over-deck requirements: where two parts of
    different levels cross, the higher one clears the lower one's deck by `clear` (2026-10-11: crossing
    decks clipped into each other). Pure given ground_fn."""
    zs = deck_profile_once(lines, ground_fn, crossings, clear, grade, lift)
    n_road = len(crossings)
    if levels is None or len(lines) < 2:
        return zs
    over = _deck_crossings(lines)
    stacks = _deck_stacks(lines, levels, widths) if widths is not None else []
    if not over and not stacks:
        return _merge_overlaps(lines, zs, widths, levels) if widths is not None else zs
    arcs = [np.r_[0.0, np.cumsum(np.hypot(*np.diff(ln[:, :2], axis=0).T))] for ln in lines]
    for _ in range(4):
        extra = []
        for i, j, si, sj in over:
            if levels[i] == levels[j]:
                continue
            hi, lo, sh, sl = (i, j, si, sj) if levels[i] > levels[j] else (j, i, sj, si)
            extra.append((hi, sh, float(np.interp(sl, arcs[lo], zs[lo])), None, levels[hi]))
        for hi, sh, lo, sl in stacks:               # a deck over another along it (an upper roadway)
            extra.append((hi, sh, float(np.interp(sl, arcs[lo], zs[lo])), None, levels[hi]))
        if not extra:
            break
        new = deck_profile_once(lines, ground_fn, list(crossings) + extra, clear, grade, lift, n_road=n_road,
                                levels=levels)
        if all(np.allclose(a, b, atol=0.02, equal_nan=True) for a, b in zip(new, zs)):
            zs = new
            break
        zs = new
    return _merge_overlaps(lines, zs, widths, levels) if widths is not None else zs


STACK_STEP_M = 16.0     # m between the points where an upper deck must clear the one under it (_deck_stacks)


def _deck_stacks(lines, levels, widths, step=STACK_STEP_M):
    """(upper part, arc on it, lower part, arc on that) every `step` m where a part of a higher level runs over one
    of a lower level, their surfaces overlapping in plan: the Queensboro Bridge's upper and lower roadways lay at
    one height, one road through the other (the user's report, 2026-10-17); _deck_crossings only finds crossings.
    Pure."""
    out = []
    n = len(lines)
    if levels is None or n < 2:
        return out
    pts = [np.asarray(ln, np.float64)[:, :2] for ln in lines]
    arcs = [np.r_[0.0, np.cumsum(np.hypot(*np.diff(p, axis=0).T))] for p in pts]
    lo_b = [p.min(0) for p in pts]
    hi_b = [p.max(0) for p in pts]
    for i in range(n):
        for j in range(n):
            if levels[i] <= levels[j] or len(pts[i]) < 2 or len(pts[j]) < 2:
                continue
            reach = (float(widths[i]) + float(widths[j])) / 2
            if (hi_b[i] + reach < lo_b[j]).any() or (hi_b[j] + reach < lo_b[i]).any():
                continue
            s = np.arange(0.0, arcs[i][-1], step)
            q = np.c_[np.interp(s, arcs[i], pts[i][:, 0]), np.interp(s, arcs[i], pts[i][:, 1])]
            P, ab = pts[j][:-1], np.diff(pts[j], axis=0)
            L2 = np.maximum((ab * ab).sum(1), 1e-12)
            t = np.clip(((q[:, None, :] - P[None]) * ab[None]).sum(2) / L2[None], 0.0, 1.0)
            near = P[None] + t[..., None] * ab[None]
            d = np.hypot(*(q[:, None, :] - near).transpose(2, 0, 1))
            m = np.argmin(d, axis=1)
            dd = d[np.arange(len(q)), m]
            # (two parts joined at an end: a ramp leaving the mainline overlaps it from the fork on. A requirement there
            # falls back to the shared end at DECK_GRADE and lifted the upper part's end alone: a ramp of a higher
            # level started 2.2 m over the fork, a ledge across the lanes, I-83 in Baltimore 2026-10-21. None within
            # the distance it takes to fall away.)
            shared = [ei for ei in (0, -1) for ej in (0, -1) if np.hypot(*(pts[i][ei] - pts[j][ej])) < 0.5]
            for a in np.flatnonzero(dd < reach):
                k = m[a]
                sl = arcs[j][k] + t[a, k] * (arcs[j][k + 1] - arcs[j][k])
                if any((s[a] if ei == 0 else arcs[i][-1] - s[a]) * DECK_GRADE < DECK_ROAD_CLEAR for ei in shared):
                    continue
                out.append((i, float(s[a]), j, float(sl)))
    return out


def _deck_beside(lines, widths, step=STACK_STEP_M):
    """(i, j), i < j: deck parts whose surfaces come within MERGE_TOUCH_M of each other in plan (side by side or one
    over the other: solved together). Pure."""
    out = []
    n = len(lines)
    pts = [np.asarray(ln, np.float64)[:, :2] for ln in lines]
    lo_b = [p.min(0) if len(p) else np.zeros(2) for p in pts]
    hi_b = [p.max(0) if len(p) else np.zeros(2) for p in pts]
    for i in range(n):
        if len(pts[i]) < 2:
            continue
        si = np.r_[0.0, np.cumsum(np.hypot(*np.diff(pts[i], axis=0).T))]
        t = np.arange(0.0, si[-1] + 1e-9, step)
        q = np.c_[np.interp(t, si, pts[i][:, 0]), np.interp(t, si, pts[i][:, 1])]
        for j in range(i + 1, n):
            if len(pts[j]) < 2:
                continue
            reach = (float(widths[i]) + float(widths[j])) / 2 + MERGE_TOUCH_M
            if (hi_b[i] + reach < lo_b[j]).any() or (hi_b[j] + reach < lo_b[i]).any():
                continue
            P, ab = pts[j][:-1], np.diff(pts[j], axis=0)
            L2 = np.maximum((ab * ab).sum(1), 1e-12)
            tt = np.clip(((q[:, None, :] - P[None]) * ab[None]).sum(2) / L2[None], 0.0, 1.0)
            d = np.hypot(*(q[:, None, :] - (P[None] + tt[..., None] * ab[None])).transpose(2, 0, 1)).min(1)
            if (d < reach).sum() >= 2:
                out.append((i, j))
    return out


def water_clear(w):
    """m a deck clears the water under it for a wet run w m long: a share of the span (a river's channel wants a
    ship's room: the Queensboro Bridge's ~40 m over 250 m channels), at least WATER_CLEAR_MIN; a run longer than
    WATER_CAUSEWAY_M is a causeway, low over the water. Pure."""
    if w > WATER_CAUSEWAY_M:
        return WATER_CAUSEWAY_CLEAR
    return float(np.clip(WATER_CLEAR_SHARE * w, WATER_CLEAR_MIN, WATER_CLEAR_MAX))


WATER_CLEAR_SHARE = 0.15
WATER_CLEAR_MIN = 2.5
WATER_CLEAR_MAX = 40.0
WATER_CAUSEWAY_M = 1500.0
WATER_CAUSEWAY_CLEAR = 5.0
WATER_STEP_M = 16.0         # m between a wet run's clearance points
NAV_RUN_M = 100.0           # a wet run this long takes the bridge's navigation clearance (NBI item 39)


def deck_profile_once(lines, ground_fn, crossings=(), clear=None, grade=None, lift=None, n_road=None, levels=None):
    """Deck heights (one array per part) of a bridge chain: deck_heights between the abutments, at
    least `lift` over the ground under it, and at least `clear` over each crossing road below
    ((part index, arc m, road height)), the requirement falling at `grade` along the chain; then
    smoothed per part with its end heights kept. A road crossing (the first n_road crossings, default all, without
    their own clearance) by an abutment the deck cannot get AT_GRADE_M over is a junction at grade: the deck
    meets the road's height there. Pure given ground_fn(xy (m, 2)) -> heights."""
    clear = DECK_ROAD_CLEAR if clear is None else clear
    grade = DECK_GRADE if grade is None else grade
    lift = DECK_CLEAR if lift is None else lift
    zs = deck_heights(lines, ground_fn)
    if not zs:
        return zs
    nodes, edges, adj = _chain_graph(lines)
    allxy = np.concatenate([ln[:, :2] for ln in lines])
    gz = np.asarray(ground_fn(allxy), np.float64)
    deg = np.zeros(len(nodes), int)
    for a, b, _ in edges:
        deg[a] += 1
        deg[b] += 1
    # no steeper than DECK_MAX_GRADE from an abutment: a short bridge over a road cannot reach full
    # headroom (2026-10-11: such decks became steep wedges, "walls" in the user's screenshot)
    cap = [np.full(len(ln), np.inf) for ln in lines]
    dab = [np.full(len(ln), np.inf) for ln in lines]      # m to the nearest abutment
    for k, (a, b, s) in enumerate(edges):
        for node, zend in ((a, zs[k][0]), (b, zs[k][-1])):
            if deg[node] != 1 or not np.isfinite(zend):
                continue
            d = _dijkstra(adj, {node: 0.0})
            for k2, (ea, eb, es) in enumerate(edges):
                dk = np.minimum(d[ea] + es, d[eb] + (es[-1] - es))
                cap[k2] = np.minimum(cap[k2], zend + DECK_MAX_GRADE * dk)
                dab[k2] = np.minimum(dab[k2], dk)
    n_road = len(crossings) if n_road is None else n_road
    req = [np.full(len(ln), -np.inf) for ln in lines]
    for ci, cr in enumerate(crossings):     # (part, arc, height under it[, its own clearance: over water])
        pi, sc, zc = cr[:3]
        cl = cr[3] if len(cr) > 3 and cr[3] is not None else clear
        # a deck-over-deck requirement (its 5th element: the upper part's level) lifts the parts of that level and
        # above only: falling along the chain it reached the lower deck it was measured on, lifting it, and so the
        # upper again each round (the Tsing Ma Bridge's upper deck 33 m over its lower one, 7 m in life, 2026-10-18)
        min_lv = cr[4] if len(cr) > 4 and levels is not None else None
        a, b, s = edges[pi]
        d = _dijkstra(adj, {a: sc, b: s[-1] - sc})
        # a junction at grade (Queens Plaza, the user's report #55, 2026-10-17: a ramp's map line starts in the
        # intersection it climbs from; its deck rose 0.8 m over the cross street there, a hump across it)
        flat = (ci < n_road and len(cr) == 3 and float(np.interp(sc, s, dab[pi])) <= AT_GRADE_REACH_M
                and float(np.interp(sc, s, cap[pi])) < zc + AT_GRADE_M)
        for k, (ea, eb, es) in enumerate(edges):
            dk = np.minimum(d[ea] + es, d[eb] + (es[-1] - es))
            if k == pi:
                dk = np.minimum(dk, np.abs(es - sc))
            if flat:
                cap[k] = np.minimum(cap[k], zc + DECK_MAX_GRADE * dk)
            elif min_lv is None or levels[k] >= min_lv:
                req[k] = np.maximum(req[k], zc + cl - grade * dk)
    # the caps (from an abutment, from a junction at grade) never under the straight line between the abutments: two
    # abutments further apart in height than DECK_MAX_GRADE allows had the higher one pulled under its own road, a
    # step onto the deck (0.9 m up onto the I-20 bridge near Temple, GA, once its approaches were raised, 2026-10-20)
    cap = [np.where(np.isfinite(z), np.maximum(c, z), c) for c, z in zip(cap, zs)]
    out, k0 = [], 0
    for k, (z, (a, b, s)) in enumerate(zip(zs, edges)):
        g = gz[k0:k0 + len(z)] + lift
        k0 += len(z)
        if not np.isfinite(z).all():
            out.append(z)
            continue
        # ground standing over a cap raises it, falling at DECK_MAX_GRADE either way: capped from its abutment, a deck
        # met a steep embankment's ground in a step where the ground took over (0.86 m in 4 m onto Lombard Street's
        # east abutment in Baltimore, the bridge's map line ending down the embankment; cars stopped dead, 2026-10-20)
        # the floor: the ground rounded over its peaks (a vertical curve of DECK_CREST_R), so a deck smoothed under a
        # sharp peak and floored there has no spike
        g = np.maximum(g, _ground_envelope(s, g, 0.0, 1.0 / (2.0 * DECK_CREST_R)))
        cap[k] = np.maximum(cap[k], _ground_envelope(s, g, DECK_MAX_GRADE))
        zz = np.maximum(np.minimum(np.maximum(z, req[k]), cap[k]), g)
        if deg[a] == 1:                         # an abutment keeps the ground's height
            zz[0] = z[0]
        if deg[b] == 1:
            zz[-1] = z[-1]
        if len(zz) > 3:
            # no sag between two higher points (the Queensboro Bridge dipped 7 m over Roosevelt Island between its
            # channel spans: one arch in life, 2026-10-18), within the caps
            zz = np.maximum(np.minimum(_no_sag(s, zz), np.maximum(cap[k], zz)), zz)
            step = s[-1] / (len(s) - 1)
            sigma = float(np.clip(s[-1] / DECK_SIGMA_SHARE, DECK_SIGMA, DECK_SIGMA_MAX))   # a long deck: a long curve
            sm = np.minimum(_smooth_pinned(zz, step, sigma), cap[k])         # (no hump back over a
            zz = np.maximum(sm, g)                                                          # junction at grade)
            zz[0], zz[-1] = sm[0], sm[-1]
        out.append(zz)
    return out


DECK_CREST_R = 60.0     # m: the ground under a deck is its floor rounded to vertical curves this wide (deck_profile_once)
DECK_SIGMA = 12.0       # m, deck profile smoothing (at least) ...
DECK_SIGMA_SHARE = 25.0  # ... a 25th of the part's length ...
DECK_SIGMA_MAX = 40.0   # ... at most


def _no_sag(s, z):
    """The upper concave hull of the profile (s, z) at s: every dip between two higher points filled with the
    straight grade between them. Pure."""
    hull = []
    for p in zip(s, z):
        while len(hull) >= 2:
            (x1, y1), (x2, y2) = hull[-2], hull[-1]
            if (x2 - x1) * (p[1] - y1) - (y2 - y1) * (p[0] - x1) >= 0:      # hull[-1] under the chord: dropped
                hull.pop()
            else:
                break
        hull.append(p)
    hx, hy = zip(*hull)
    return np.interp(s, hx, hy)


def _dedupe(xy, tol=0.5):
    """A polyline without points closer than tol m to the previous one (ends kept). Pure."""
    if len(xy) < 3:
        return xy
    keep = [0]
    for k in range(1, len(xy) - 1):
        if np.hypot(*(xy[k] - xy[keep[-1]])) >= tol:
            keep.append(k)
    if np.hypot(*(xy[-1] - xy[keep[-1]])) < tol and len(keep) > 1:
        keep[-1] = len(xy) - 1
    else:
        keep.append(len(xy) - 1)
    return xy[keep]


def _ground_envelope(s, g, grade, curve=0.0):
    """The least profile over the ground heights g (at arcs s) that falls from each no faster than grade * d + curve *
    d^2 at d m from it: max_j g_j - grade |s - s_j| - curve (s - s_j)^2. Pure."""
    s, g = np.asarray(s, np.float64), np.asarray(g, np.float64)
    out = np.full(len(s), -np.inf)
    for i0 in range(0, len(s), 512):        # (in blocks: a long chain's (n, n) would be large)
        d = np.abs(s[None, :] - s[i0:i0 + 512, None])
        out = np.maximum(out, (g[i0:i0 + 512, None] - grade * d - curve * d * d).max(axis=0))
    return out


def _smooth_pinned(z, step, sigma):
    """Gaussian smoothing that keeps both end values and the trend at the ends (the profile is
    point-reflected about each end: plain mirroring pulled a hump's height into its pinned ends and
    made 15 % steps there). Pure."""
    z = np.asarray(z, np.float64)
    r = int(math.ceil(3 * sigma / step))
    r = min(r, len(z) - 1)
    if r < 1:
        return z.copy()
    k = np.exp(-0.5 * (np.arange(-r, r + 1) * step / sigma) ** 2)
    k /= k.sum()
    pad = np.r_[2 * z[0] - z[r:0:-1], z, 2 * z[-1] - z[-2:-r - 2:-1]]
    out = np.convolve(pad, k, mode="valid")
    out[0], out[-1] = z[0], z[-1]
    return out


def deck_heights(lines, end_z):
    """Deck heights of chained bridge parts. lines: polylines (k, 2) in metres (one per bridge part);
    parts sharing an end point (within 0.5 m) are joined. end_z(xy (m, 2)) -> heights of the ground at
    the abutments (chain ends: points used by one part only). A point's height is the abutments'
    heights weighted by 1 / their distance along the chain (between two abutments: a straight line).
    Returns one height array per line. Pure given end_z."""
    if not lines:
        return []
    edges, ez, dist = _end_dists(lines, end_z)
    out = []
    for a, b, s in edges:
        if not dist:                            # a closed loop: no abutment known
            out.append(np.full(len(s), np.nan))
            continue
        num = np.zeros(len(s))
        den = np.zeros(len(s))
        for k, d in enumerate(dist):
            dk = np.minimum(d[a] + s, d[b] + (s[-1] - s))
            ok = np.isfinite(dk)
            w = np.where(ok, 1.0 / np.maximum(dk, 1e-6), 0.0)
            num += w * np.where(ok, ez[k], 0.0)
            den += w
        out.append(np.where(den > 0, num / np.maximum(den, 1e-12), np.nan))
    return out


def _end_dists(lines, end_z):
    """The chain of lines (joined at end points within 0.5 m): its edges (start node, end node, arc (k,)),
    the heights end_z gives at its ends (points of one line only) and per end the distance along the chain
    to every node. Pure given end_z."""
    keys, nodes = {}, []

    def node(p):
        k = (round(float(p[0]) * 2), round(float(p[1]) * 2))      # 0.5 m
        if k not in keys:
            keys[k] = len(nodes)
            nodes.append(np.asarray(p[:2], np.float64))
        return keys[k]
    edges = []
    for ln in lines:
        seg = np.hypot(*np.diff(ln[:, :2], axis=0).T)
        s = np.r_[0.0, np.cumsum(seg)]
        edges.append((node(ln[0]), node(ln[-1]), s))
    deg = np.zeros(len(nodes), int)
    adj = [[] for _ in nodes]
    for a, b, s in edges:
        deg[a] += 1
        deg[b] += 1
        adj[a].append((b, s[-1]))
        adj[b].append((a, s[-1]))
    ends = [i for i in range(len(nodes)) if deg[i] == 1]
    ez = np.asarray(end_z(np.array([nodes[i] for i in ends]).reshape(-1, 2)), np.float64) if ends else np.zeros(0)
    import heapq
    dist = []                                   # per end: distance along the chain to every node
    for e in ends:
        d = np.full(len(nodes), np.inf)
        d[e] = 0.0
        q = [(0.0, e)]
        while q:
            dd, u = heapq.heappop(q)
            if dd > d[u]:
                continue
            for v, w in adj[u]:
                if dd + w < d[v]:
                    d[v] = dd + w
                    heapq.heappush(q, (dd + w, v))
        dist.append(d)
    return edges, ez, dist


TUNNEL_COVER = 8.0      # m from the floor up to the ground over it wanted inside a tunnel (the tube's 6.5 m and rock)
TUNNEL_GRADE = 0.05     # a tunnel floor falls no steeper than this from a portal
TUNNEL_STEP = 4.0       # m between tunnel profile points
TUNNEL_WET_SHARE = 0.25 # a tunnel part with more of its points under water is left out (TunnelProfiles.wet)
TUNNEL_OPEN_COVER = 4.0 # m: a 'tunnel' whose ground nowhere rises this far over the straight line between its portals
                        # is an underpass in the open (under a bridge the map does not draw as a deck over it): a road
                        # on the ground, no tube, portals or holes (Central Park's 86th Street Transverse under its
                        # park bridges got portal frames standing in the open, the user's report 2026-10-17). Between it
                        # and TUNNEL_COVER: a low arch (a park's stone bridge over a sunken road, Central Park's West
                        # Drive over the 86th Street Transverse: 3.4 m clearance under ~1.5 m of arch and soil):
                        # straight between its portals, its tube as high as the ground over it allows (TUNNEL_SOIL_M
                        # under it, at least TUNNEL_LOW_H), of stone (roadmesh.tunnel)
PORTAL_LOOK_M = 12.0    # m inside a portal where the ground over it is read for its face's height ...
PORTAL_GROUND_M = 0.5   # ... which reaches this far over that ground ...
PORTAL_UP_MIN = 1.0     # ... and at least this far over the vault
SECTION_MIN_M = 8.0     # m: a tunnel's covered sections and the open gaps between them at least this long
FULL_MIN_M = 60.0       # a covered section reaching TUNNEL_COVER is a full tunnel (a rock tube) if it is this long or
FULL_DEEP_M = 16.0      # has this much ground over it somewhere; shorter and shallower, an underpass under an embankment:
                        # a low arch, the ground built up over it (a 31 m underpass under the E6 in Norway with 8.6 m of
                        # embankment over it stood as a bare 6.8 m concrete tube on the ground beside the road, GitHub #2)
TUNNEL_SOIL_M = 1.0     # m of arch and soil kept over a low tunnel's vault
ARCH_SIZE_SKIP_M = 5.0  # m at a low arch's mouths left out when its height is sized from the cover over it
ROAD_CAP_JOIN_M = 20.0  # a road with an end this near a low arch's mouth joins it (its own road going on), not one over it
ROAD_CAP_GAP_M = 0.5    # m a low arch's stone top stops short of a carriageway over its mouth ...
ROAD_CAP_OVER_M = 1.0   # ... and its portal face reaches at most this far over that road
TUNNEL_LOW_H = 3.6      # m: the lowest tube (floor to vault)


def tunnel_profile(lines, ground_fn):
    """Floor heights of chained tunnel parts (as deck_heights: polylines (k, 2), joined at their ends; the chain's
    ends are the portals, at the ground's height there). Straight between the portals, but at least TUNNEL_COVER
    under the ground over it (an underwater tunnel dips under the bed: Fort McHenry's portals are on land), falling
    no steeper than TUNNEL_GRADE from a portal; smoothed. One array per line (NaN for a closed loop). Pure given
    ground_fn(xy (m, 2)) -> heights."""
    if not lines:
        return []
    straight = deck_heights(lines, ground_fn)
    edges, ez, dist = _end_dists(lines, ground_fn)
    allxy = np.concatenate([np.asarray(ln[:, :2], np.float64) for ln in lines])
    gz = np.asarray(ground_fn(allxy), np.float64)
    out, k = [], 0
    import grading
    for (a, b, s), z0 in zip(edges, straight):
        g = gz[k:k + len(s)]
        k += len(s)
        if not dist or not np.isfinite(z0).all():
            out.append(np.full(len(s), np.nan))
            continue
        # under TUNNEL_COVER of ground where the straight line has less (a hollow, a low hill), reached at no more than
        # TUNNEL_GRADE from the portals (a mountain's steep portal face gives the cover within metres)
        z = np.minimum(z0, g - TUNNEL_COVER)
        low = np.full(len(s), -np.inf)
        for e, d in enumerate(dist):
            dk = np.minimum(d[a] + s, d[b] + (s[-1] - s))
            low = np.maximum(low, np.where(np.isfinite(dk), ez[e] - TUNNEL_GRADE * dk, -np.inf))
        z = np.maximum(z, low)
        if len(z) > 3:          # (the ends' trend kept: smoothed then pinned, a floor bent up beside a portal)
            z = _smooth_pinned(z, TUNNEL_STEP, grading.SIGMA_M)
        out.append(z)
    return out


def centre_colour(lon):
    """Centre line colour: yellow in the Americas, white elsewhere (the country rule comes later)."""
    return "line_yellow" if -170.0 <= float(lon) <= -30.0 else "line_white"


def offset_polyline(pts, d):
    """The polyline moved d m to its left (xy; z kept), with mitred joints (miter at most 2x). Pure."""
    pts = np.asarray(pts, np.float64)
    if len(pts) < 2:
        return pts.copy()
    if pts.shape[1] == 5:
        out = pts.copy()
        out[:, :2] += pts[:, 3:5] * d
        return out
    t = np.diff(pts[:, :2], axis=0)
    L = np.hypot(t[:, 0], t[:, 1])
    L[L < 1e-9] = 1e-9
    t /= L[:, None]
    nrm = np.stack([-t[:, 1], t[:, 0]], axis=1)          # left normals per segment
    vn = np.empty((len(pts), 2))
    vn[0], vn[-1] = nrm[0], nrm[-1]
    if len(pts) > 2:
        m = nrm[:-1] + nrm[1:]
        ml = np.hypot(m[:, 0], m[:, 1])
        ml[ml < 1e-9] = 1e-9
        m /= ml[:, None]
        cosh = np.clip(np.sum(m * nrm[1:], axis=1), 0.5, 1.0)  # miter scale 1/cos(half angle), at most 2
        vn[1:-1] = m / cosh[:, None]
    out = pts.copy()
    out[:, :2] += vn * d
    return out


def _seg_dist(p, a, b):
    """Distance from points p (N, 2) to segments a-b (M, 2): (N, M)."""
    ab = b - a
    L2 = np.maximum(np.sum(ab * ab, axis=1), 1e-12)
    ap = p[:, None, :] - a[None, :, :]
    t = np.clip(np.sum(ap * ab[None], axis=2) / L2[None], 0.0, 1.0)
    q = a[None] + t[..., None] * ab[None]
    return np.hypot(p[:, None, 0] - q[..., 0], p[:, None, 1] - q[..., 1])


def cut_outside(pts, blockers, spacing=2.0):
    """The pieces of a polyline (k, 3) outside every blocker (centreline xy (m, 2), half width): densified
    to `spacing`, points inside a blocker removed, runs of at least MIN_PIECE_M kept. Pure."""
    if not blockers:
        return [pts]
    d = densify(np.asarray(pts, np.float64), spacing)
    import fast
    fast.require(0, "inside_blockers")      # the Rust test (712 calls a tile set, ~1 s of numpy)
    segs, starts, hws = _blocker_segments(blockers)
    inside = np.asarray(fast._ef.inside_blockers(np.ascontiguousarray(d[:, :2]), segs, starts, hws))
    return _runs_outside(d, inside)


_BLOCKERS = ()                  # ((blockers list, its segment arrays), ...): the last few lists (a tile alternates
                                # between its sidewalk and road blockers over many calls)


def _blocker_segments(blockers):
    """(segs (m, 4), starts (B + 1,), half widths (B,)) of [(centreline (k, 2), half width)] (lines of < 2
    points left out)."""
    global _BLOCKERS
    for last, arr in _BLOCKERS:         # one read of the tuple: another thread may replace it meanwhile
        if last is blockers and arr[3] == len(blockers):
            return arr[:3]
    segs, starts, hws = [], [0], []
    for line, hw in blockers:
        line = np.asarray(line, np.float64)
        if len(line) < 2:
            continue
        segs.append(np.hstack([line[:-1, :2], line[1:, :2]]))
        starts.append(starts[-1] + len(line) - 1)
        hws.append(float(hw))
    arr = (np.ascontiguousarray(np.vstack(segs)) if segs else np.zeros((0, 4)), np.array(starts, np.int64),
           np.array(hws, np.float64), len(blockers))
    _BLOCKERS = ((blockers, arr),) + _BLOCKERS[:3]
    return arr[:3]


def _on_level(frame, elev, run):
    """The pieces of a sidewalk run (k, 3) whose ground (elev(lat, lon), the graded ground) lies within WALK_STEP_M of
    the run's own level (MIN_PIECE_M long at least)."""
    if len(run) < 2:
        return [run]
    la, lo, _ = frame.enu_to_geodetic(run[:, 0], run[:, 1], np.zeros(len(run)))
    g = np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2], np.float64)
    off = ~(np.abs(g - run[:, 2]) <= WALK_STEP_M)
    return [run] if not off.any() else _runs_outside(run, off)


def _runs_outside(d, inside):
    out, start = [], None
    for k in range(len(d) + 1):
        if k < len(d) and not inside[k]:
            if start is None:
                start = k
        elif start is not None:
            run = d[start:k]
            if len(run) >= 2 and float(np.sum(np.hypot(*np.diff(run[:, :2], axis=0).T))) >= MIN_PIECE_M:
                out.append(run)
            start = None
    return out


ARTERIAL_KMH = 73.0             # a divided urban trunk this slow (45 mph) is a town arterial, not an expressway ...
ARTERIAL_GAP_M = 150.0          # ... or, with no limit, one with a junction this often (Coastal Highway ~50 m)


def has_sidewalks(spec):
    # a two-way urban trunk is a town's main street on a state highway (Bishop's US-395, Moab's US-191);
    # divided trunks are one-way per carriageway: expressways get none, slow ones (Ocean City's Coastal
    # Highway, 35 mph, 2026-10-14 street photos) one on their outer side (walk_sides)
    return spec.kind in BUILT and spec.urban and (spec.cls in SIDEWALK_CLASSES
                                                  or (spec.cls == "trunk" and (not spec.oneway or _arterial(spec))))


def _arterial(spec):
    if not spec.oneway:
        return False
    if spec.kmh is not None:
        return spec.kmh <= ARTERIAL_KMH
    return spec.gap is not None and spec.gap <= ARTERIAL_GAP_M


def walk_sides(spec, left_hand=False):
    """The sides (+1 left of the geometry, -1 right) of a road with sidewalks that get one: both, or a
    divided trunk carriageway's outer side only (the curb on the driver's right, left where they drive on
    the left; the other side is the median)."""
    if (spec.cls == "trunk" or has_median(spec)) and spec.oneway:     # (a divided road's median: its curbed band)
        right = -1 if spec.dir >= 0 else 1
        return (-right,) if left_hand else (right,)
    return (1, -1)


MEDIAN_SLAB_M = 6.0     # m: a divided road's median up to this wide is one raised island, curb to curb (each
                        # carriageway's half of it); wider, ...
MEDIAN_CURB_W = 0.6     # ... a curbed band this wide along each carriageway, ...
MEDIAN_MIN_M = 0.8      # ... where its ground between them is this wide or more (Boston Street's is ~1.5 m of concrete
                        # curb, Mapillary 2026-10-17), lawn on one wider than MEDIAN_SLAB_M (surface.paint_roads), ...
MEDIAN_MAX_M = 16.0     # ... and no wider (two one-way streets a block apart are not one road with a median)


def has_median(spec):
    """A divided road's carriageway with a median of MEDIAN_MIN_M..MEDIAN_MAX_M of ground beside it."""
    return bool(spec.median) and spec.oneway and MEDIAN_MIN_M <= median_width(spec) <= MEDIAN_MAX_M


def median_side(spec, left_hand=False):
    """The side (+1 left / -1 right of the geometry) of a divided road's carriageway that faces its median, or None."""
    if not has_median(spec):
        return None
    return -walk_sides(spec, left_hand)[0]


def median_width(spec):
    """m of ground between a divided road's two carriageways (its centre lines' distance less both halves; the
    other's width taken as this one's), or 0."""
    return max(0.0, float(spec.median or 0.0) - spec.width) if spec.median else 0.0


def sidewalk_stop(spec, fid, other, ofid):
    """How far from the centreline of a crossing road (other) the sidewalks of road spec stop. The corner
    square is one sidewalk's: when both roads have sidewalks the wider road's (then the smaller id) runs on
    to the other's curb and the other's stops against its outer edge; with none on the other road, they
    run on to its curb. No gap and no overlap (two slabs at one height would z-fight). Pure."""
    if has_sidewalks(other) and (-other.width, ofid) < (-spec.width, fid):
        return other.width / 2 + other.walk
    return other.width / 2


def _bridge_extras(spec, pts, lon, left_hand=False, frame=None, bridge=False):
    """The sidewalk / gutter / marking polylines of one road: [(look dict with m / tl / p, width, pts)].
    pts: its centreline (k, 3) in the anchor frame; lon: its longitude (centre line colour). Pure."""
    out = []
    w, kind, urban, cls = spec[:4]
    edge_line = EDGE_LINE if not bridge else {**EDGE_LINE, "w": BRIDGE_EDGE_LINE_WIDTH}
    if kind not in BUILT:
        return out
    if has_sidewalks(spec):
        for side in walk_sides(spec, left_hand):
            out.append(({**SIDEWALK, "side": side}, spec.walk,
                        offset_polyline(pts, side * (w / 2 + spec.walk / 2))))
            out.append(({**GUTTER, "side": side}, GUTTER["w"], offset_polyline(pts, side * (w / 2 - GUTTER["w"] / 2))))
        ms = median_side(spec, left_hand)
        if ms is not None:
            # the median: a curbed band, lawn beyond it (two sidewalks back to back made a concrete strip, the user's
            # report #48 on Boston Street, 2026-10-17: "Improve how medians are made")
            mw = median_width(spec)
            # (each carriageway's half, a hair short of the middle: touching the other's carriageway where the gap
            # narrows, the band was cut away)
            band = max(0.3, mw / 2 - 0.15) if mw <= MEDIAN_SLAB_M else MEDIAN_CURB_W
            out.append(({**SIDEWALK, "side": ms, "median": True}, band, offset_polyline(pts, ms * (w / 2 + band / 2))))
            if kind == "paved":     # a double yellow line along it (Boston Street, Mapillary 2026-10-17)
                look = {"m": centre_colour(lon), "tl": CENTRE["tl"], "p": CENTRE["p"]}
                for k in (0, 1):
                    out.append((look, CENTRE["w"], offset_polyline(
                        pts, ms * (w / 2 - EDGE_LINE["inset"] - 2 * k * CENTRE["gap"]))))
            out.append(({**GUTTER, "side": ms}, GUTTER["w"], offset_polyline(pts, ms * (w / 2 - GUTTER["w"] / 2))))
    if kind == "paved" and spec.lanes is None and cls in CENTRE_CLASSES and w >= 6.0 and not spec.oneway:   # no paint on cobbles
        if centre_colour(lon) == "line_white":
            # outside the Americas one broken white line (the Gotthard road on Mapillary, 2026-10-18: ours had the
            # Americas' double line in white)
            out.append((LANE_LINE, LANE_LINE["w"], pts.copy()))
        else:
            look = {"m": centre_colour(lon), "tl": CENTRE["tl"], "p": CENTRE["p"]}
            for side in (1, -1):
                out.append((look, CENTRE["w"], offset_polyline(pts, side * CENTRE["gap"])))
    if kind == "paved" and spec.lanes is None and cls in LANE_CLASSES:
        # lane lines (white dashes) where the driving width holds two or more lanes a direction
        parked = urban and cls in PARKING_CLASSES and not (not spec.oneway and cls in URBAN_ARTERIAL)
        # (a rural road's edge strips counted as lanes: three on every rural Interstate, GitHub #43)
        drive = w - ((PARKING_M if spec.oneway else 2 * PARKING_M) if parked else 2 * edge_margin(spec))
        lanes = int(drive // LANE_M)
        lane_line_w = lane_marking_width(pts, frame)
        if spec.oneway and lanes >= 2:
            lw = drive / lanes
            for k in range(1, lanes):
                out.append((LANE_LINE, lane_line_w, offset_polyline(pts, -drive / 2 + k * lw)))
        elif not spec.oneway and lanes >= 4:
            n = lanes // 2
            lw = drive / 2 / n
            for side in (1, -1):
                for k in range(1, n):
                    out.append((LANE_LINE, lane_line_w, offset_polyline(pts, side * k * lw)))
    if kind == "paved" and spec.lanes is None and (not urban or not has_sidewalks(spec)) and cls in EDGE_LINE_CLASSES:   # no curb: edge lines
        for side in (1, -1):
            inset = (bridge_motorway_shoulder(spec) if bridge else motorway_shoulder(spec)) if rural_motorway(spec) else EDGE_LINE["inset"]
            out.append((edge_line, edge_line["w"], offset_polyline(pts, side * (w / 2 - inset))))
    if kind == "paved" and spec.lanes is not None:
        import roadlanes
        out.extend(roadlanes.markings(spec, pts, lon, left_hand, frame))
    return out


def extras(spec, pts, lon, left_hand=False, frame=None, bridge=False):
    """The sidewalk / gutter / marking polylines of one road: [(look dict with m / tl / p, width, pts)].
    pts: its centreline (k, 3) in the anchor frame; lon: its longitude (centre line colour). Pure."""
    out = []
    w, kind, urban, cls = spec[:4]
    if kind not in BUILT:
        return out
    if has_sidewalks(spec):
        for side in walk_sides(spec, left_hand):
            out.append(({**SIDEWALK, "side": side}, spec.walk,
                        offset_polyline(pts, side * (w / 2 + spec.walk / 2))))
            out.append(({**GUTTER, "side": side}, GUTTER["w"], offset_polyline(pts, side * (w / 2 - GUTTER["w"] / 2))))
        ms = median_side(spec, left_hand)
        if ms is not None:
            # the median: a curbed band, lawn beyond it (two sidewalks back to back made a concrete strip, the user's
            # report #48 on Boston Street, 2026-10-17: "Improve how medians are made")
            mw = median_width(spec)
            # (each carriageway's half, a hair short of the middle: touching the other's carriageway where the gap
            # narrows, the band was cut away)
            band = max(0.3, mw / 2 - 0.15) if mw <= MEDIAN_SLAB_M else MEDIAN_CURB_W
            out.append(({**SIDEWALK, "side": ms, "median": True}, band, offset_polyline(pts, ms * (w / 2 + band / 2))))
            if kind == "paved":     # a double yellow line along it (Boston Street, Mapillary 2026-10-17)
                look = {"m": centre_colour(lon), "tl": CENTRE["tl"], "p": CENTRE["p"]}
                for k in (0, 1):
                    out.append((look, CENTRE["w"], offset_polyline(
                        pts, ms * (w / 2 - EDGE_LINE["inset"] - 2 * k * CENTRE["gap"]))))
            out.append(({**GUTTER, "side": ms}, GUTTER["w"], offset_polyline(pts, ms * (w / 2 - GUTTER["w"] / 2))))
    if kind == "paved" and spec.lanes is not None:
        import roadlanes
        return roadlanes.move_roadside(spec, pts, out, frame) + roadlanes.markings(spec, pts, lon, left_hand, frame)
    if kind == "paved" and cls in CENTRE_CLASSES and w >= 6.0 and not spec.oneway:   # no paint on cobbles
        if centre_colour(lon) == "line_white":
            # outside the Americas one broken white line (the Gotthard road on Mapillary, 2026-10-18: ours had the
            # Americas' double line in white)
            out.append((LANE_LINE, LANE_LINE["w"], pts.copy()))
        else:
            look = {"m": centre_colour(lon), "tl": CENTRE["tl"], "p": CENTRE["p"]}
            for side in (1, -1):
                out.append((look, CENTRE["w"], offset_polyline(pts, side * CENTRE["gap"])))
    if kind == "paved" and cls in LANE_CLASSES:
        # lane lines (white dashes) where the driving width holds two or more lanes a direction
        drive = driving_width(spec)
        along, _ = lane_counts(spec)
        lane_look = {**LANE_LINE, 'highway_lane': True} if cls in {'motorway', 'trunk'} and not spec.link else LANE_LINE
        if spec.oneway and along >= 2:
            lw = drive / along
            for k in range(1, along):
                out.append(({**lane_look,'lane_index':k}, LANE_LINE["w"], offset_polyline(pts, -drive / 2 + k * lw)))
        elif not spec.oneway and along >= 2:
            n = along
            lw = drive / 2 / n
            for side in (1, -1):
                for k in range(1, n):
                    out.append((lane_look, LANE_LINE["w"], offset_polyline(pts, side * k * lw)))
    if kind == "paved" and (not urban or not has_sidewalks(spec)) and cls in EDGE_LINE_CLASSES:   # no curb: edge lines
        # Highway edge lines delimit the travel lanes, leaving paved shoulders beyond them. Other
        # classes keep their curb/edge treatment. The inside line follows traffic, not vertex order,
        # and does not depend on detecting a nearby carriageway across a wide rural median.
        edge = driving_width(spec) / 2 if cls == "motorway" else w / 2 - EDGE_LINE["inset"]
        yellow_side = (1 if spec.dir >= 0 else -1) if (
            spec.oneway and cls in {"motorway", "trunk"} and not left_hand
            and not spec.link and centre_colour(lon) == "line_yellow") else None
        for side in (1, -1):
            look = {**EDGE_LINE, "edge_side": side}
            if side == yellow_side:
                look["m"] = "line_yellow"
            out.append((look, EDGE_LINE["w"], offset_polyline(pts, side * edge)))
    return out


def lane_marking_width(pts, frame=None):
    """Base width; highway_markings applies exact state polygons after tile clipping."""
    return LANE_LINE["w"]


JOIN_GAP_M = 14.0       # sidewalk ends this close (and facing each other) are joined: a sharp corner / road merge
JOIN_TURN_DEG = 70.0    # ... when the run turns by at most this to reach the other end


def _end_dir(pts, at_end):
    """Unit direction a run leaves by at its start (at_end False: pointing out of the run backwards) or end."""
    if at_end:
        d = pts[-1, :2] - pts[-2, :2]
    else:
        d = pts[0, :2] - pts[1, :2]
    n = math.hypot(*d)
    return d / n if n > 1e-9 else d


def join_walk_ends(runs, carriageways):
    """Sidewalk runs whose open ends face each other across a short gap merged into one run (the user,
    2026-10-14: sidewalks at sharp corners do not join; junctions.py makes no curb return under 35 degrees or with a
    long tangent, so each street's sidewalk stopped at the other's carriageway with bare ground between). runs: the
    runs near the tile before clipping ({pts, side, w, fid, j, open, ramp_fids}); two are merged when the straight
    gap between their ends stays off every carriageway and they are on the same side (one continuous polyline: the
    mesh mitres it, no seam). Pairs found greedily, nearest first. Returns the new list of runs."""
    ends = []                                   # (run index, at_end, point, leaving direction)
    for k, rn in enumerate(runs):
        p = rn["pts"]
        if len(p) < 2:
            continue
        for at_end in (False, True):
            if not rn["open"][1 if at_end else 0] or rn["j"][1 if at_end else 0]:
                continue                        # cut by the near box, or at a curb return: not a loose end
            ends.append((k, at_end, (p[-1] if at_end else p[0]), _end_dir(p, at_end)))
    pairs = []
    cos_t = math.cos(math.radians(JOIN_TURN_DEG))
    for a in range(len(ends)):
        for b in range(a + 1, len(ends)):
            ki, ei, qi, di = ends[a]
            kj, ej, qj, dj = ends[b]
            if ki == kj:
                continue
            gap = qj[:2] - qi[:2]
            L = math.hypot(*gap)
            if L < 0.3 or L > JOIN_GAP_M:
                continue
            u = gap / L
            if float(u @ di) < cos_t or float(-u @ dj) < cos_t:
                continue                        # each end must leave toward the other
            pairs.append((L, a, b))
    runs = [dict(r) for r in runs]
    alias = list(range(len(runs)))              # a merged run's index (runs consumed by merges point to it)
    used = set()
    for L, a, b in sorted(pairs):
        if a in used or b in used:
            continue
        ki, ei, qi, di = ends[a]
        kj, ej, qj, dj = ends[b]
        while alias[ki] != ki:
            ki = alias[ki]
        while alias[kj] != kj:
            kj = alias[kj]
        if ki == kj:
            continue
        ri, rj = runs[ki], runs[kj]
        # the pieces in travel order: ri toward its joined end, then rj away from its joined end
        pi_ = ri["pts"] if ei else ri["pts"][::-1]
        pj_ = rj["pts"] if not ej else rj["pts"][::-1]
        si = ri["side"] if ei else -ri["side"]
        sj = rj["side"] if not ej else -rj["side"]
        if si != sj:
            continue                            # opposite sides of the streets: not one sidewalk
        gap_pts = np.linspace(pi_[-1], pj_[0], max(2, int(L / 2.0) + 2))[1:-1]
        hw = 0.25 * (ri["w"] + rj["w"])
        if any(_seg_dist_min(cl, q[:2]) < h + hw * 0.5 for cl, h in carriageways for q in gap_pts):
            continue                            # the gap would cross a road
        used.update((a, b))
        pts = np.vstack([pi_, gap_pts, pj_]) if len(gap_pts) else np.vstack([pi_, pj_])
        j0 = ri["j"][0] if ei else ri["j"][1]
        j1 = rj["j"][1] if not ej else rj["j"][0]
        o0 = ri["open"][0] if ei else ri["open"][1]
        o1 = rj["open"][1] if not ej else rj["open"][0]
        runs[ki] = {"pts": densify(simplify(pts)), "side": si, "w": ri["w"], "fid": ri["fid"], "j": (j0, j1),
                    "open": (o0, o1), "ramp_keys": sorted(set(ri.get("ramp_keys", [(ri["fid"], ri["side"])])
                                                               + rj.get("ramp_keys", [(rj["fid"], rj["side"])])))}
        runs[kj] = None
        alias[kj] = ki
    return [r for r in runs if r is not None]


def _left_normal(two):
    d = two[1] - two[0]
    n = math.hypot(*d)
    return np.array([-d[1], d[0]]) / n if n > 1e-9 else np.array([0.0, 0.0])


def _seg_dist_min(line, p):
    import fast
    fast.require(0, "seg_dist_min")
    return fast._ef.seg_dist_min(np.asarray(line, np.float64), float(p[0]), float(p[1]))


def clip_polyline(pts, x0, y0, x1, y1):
    """The pieces of a polyline (k, D >= 2, xy first) inside the box, as a list of arrays (Liang-Barsky
    per segment, consecutive pieces joined). Pure."""
    pieces, cur = [], []
    for a, b in zip(pts[:-1], pts[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        t0, t1 = 0.0, 1.0
        ok = True
        for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
            if p == 0:
                if q < 0:
                    ok = False
                    break
            else:
                t = q / p
                if p < 0:
                    t0 = max(t0, t)
                else:
                    t1 = min(t1, t)
        if not ok or t0 > t1:
            if len(cur) >= 2:
                pieces.append(np.array(cur))
            cur = []
            continue
        pa = a + (b - a) * t0
        pb = a + (b - a) * t1
        if not cur or np.abs(np.asarray(cur[-1]) - pa).max() > 1e-6:
            if len(cur) >= 2:
                pieces.append(np.array(cur))
            cur = [pa]
        cur.append(pb)
        if t1 < 1.0:
            pieces.append(np.array(cur))
            cur = []
    if len(cur) >= 2:
        pieces.append(np.array(cur))
    return pieces


def simplify(pts, tol=SIMPLIFY_M, ztol=None, heading=None):
    """Ramer-Douglas-Peucker on the xy of a polyline (k, D). ztol (D >= 3): a point is also kept where its height is
    more than this off the line between the kept ones (a deck drawn at its own heights: on xy alone a straight bridge
    kept its two ends in a tile and lost its rise between, 2.5 m below the abutment it met; a car drove into the
    ground, Baltimore's Lombard Street 2026-10-20). heading: maximum chord/tangent error in degrees;
    retaining curve directions prevents long straight chords from putting knees back into fitted roads. Pure."""
    if len(pts) < 3:
        return pts
    widths = np.linalg.norm(pts[:, 3:5], axis=1) if pts.shape[1] == 5 else None
    varying = widths is not None and np.ptp(widths) > .001
    if heading is not None:
        delta = np.diff(pts[:, :2], axis=0)
        length = np.linalg.norm(delta, axis=1)
        unit = delta/np.maximum(length[:, None], 1e-9)
        tangents = np.vstack([unit[0], unit[:-1]+unit[1:], unit[-1]])
        tangents /= np.maximum(np.linalg.norm(tangents, axis=1)[:, None], 1e-9)
        arc = np.r_[0., np.cumsum(length)]
        cosine = math.cos(math.radians(heading))
    keep = np.zeros(len(pts), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        a, b = pts[i, :2], pts[j, :2]
        ab = b - a
        L = math.hypot(ab[0], ab[1])
        seg = pts[i + 1:j, :2]
        if len(seg) == 0:
            continue
        if L < 1e-9:
            d = np.hypot(seg[:, 0] - a[0], seg[:, 1] - a[1])
        else:
            d = np.abs(ab[0] * (seg[:, 1] - a[1]) - ab[1] * (seg[:, 0] - a[0])) / L
        if ztol is not None and pts.shape[1] >= 3:
            t = (np.clip(((seg[:, 0] - a[0]) * ab[0] + (seg[:, 1] - a[1]) * ab[1]) / (L * L), 0.0, 1.0)
                 if L >= 1e-9 else np.zeros(len(seg)))
            dz = np.abs(pts[i + 1:j, 2] - (pts[i, 2] + t * (pts[j, 2] - pts[i, 2])))
            d = np.maximum(d, dz * (tol / ztol))
        if varying:
            t = np.clip(((seg-a)*ab).sum(1)/max(L*L, 1e-12), 0., 1.)
            expected = widths[i]+t*(widths[j]-widths[i])
            d = np.maximum(d, 20.*np.abs(widths[i+1:j]-expected))
        k = int(np.argmax(d))
        if d[k] > tol:
            m = i + 1 + k
            keep[m] = True
            stack.append((i, m))
            stack.append((m, j))
        elif heading is not None and L > 1e-9 and np.min(tangents[[i, j]] @ (ab/L)) < cosine:
            m = int(np.clip(np.searchsorted(arc, (arc[i]+arc[j])/2), i+1, j-1))
            keep[m] = True
            stack.append((i, m))
            stack.append((m, j))
    return pts[keep]


def densify(pts, spacing=MAX_SPACING):
    """Nodes added so no two are more than `spacing` m apart (xy). Pure."""
    import fast
    pts = np.asarray(pts, np.float64)
    if not len(pts):
        return pts
    fast.require(0, "densify")
    return fast._ef.densify(pts, float(spacing))     # the Rust loop


def split(pts, n=MAX_NODES):
    """Chunks of at most n nodes sharing their end nodes. Pure."""
    if len(pts) <= n:
        return [pts]
    out, i = [], 0
    while i < len(pts) - 1:
        out.append(pts[i:i + n])
        i += n - 1
    return out


def tile_bbox(tx, ty):
    return tx * TILE, ty * TILE, (tx + 1) * TILE, (ty + 1) * TILE


def tiles_near(x, y, radius):
    """Tile keys whose square comes within `radius` of (x, y) (Chebyshev)."""
    t0x, t1x = int(math.floor((x - radius) / TILE)), int(math.floor((x + radius) / TILE))
    t0y, t1y = int(math.floor((y - radius) / TILE)), int(math.floor((y + radius) / TILE))
    return [(tx, ty) for tx in range(t0x, t1x + 1) for ty in range(t0y, t1y + 1)]


def tile_distance(tx, ty, x, y):
    x0, y0, x1, y1 = tile_bbox(tx, ty)
    return max(x0 - x, x - x1, y0 - y, y - y1, 0.0)


def wanted_tiles(x, y, vx=0.0, vy=0.0):
    """The tile keys the mod should have around (x, y): those within SEND_M, and moving at FAST_MS or more also those
    within CORRIDOR_M of the path ahead, up to LEAD_S of driving (LEAD_MAX_M at most)."""
    want = set(tiles_near(x, y, SEND_M))
    s = math.hypot(vx, vy)
    lead = min(LEAD_MAX_M, s * LEAD_S)
    if s < FAST_MS or lead <= SEND_M:
        return want
    ux, uy = vx / s, vy / s
    d = 0.0
    while d <= lead:
        want.update(tiles_near(x + ux * d, y + uy * d, CORRIDOR_M))
        d += TILE / 2
    return want


def path_key(x, y, vx, vy):
    """The build order at speed: by how far along the path ahead the tile is, its sideways distance from the path
    counting SIDE_PENALTY times, a tile behind the car by its distance times SIDE_PENALTY (after those ahead)."""
    s = math.hypot(vx, vy)
    ux, uy = vx / s, vy / s

    def key(k):
        d = tile_distance(k[0], k[1], x, y)
        if d <= 0:
            return 0.0
        x0, y0, x1, y1 = tile_bbox(k[0], k[1])
        dx, dy = (x0 + x1) / 2 - x, (y0 + y1) / 2 - y
        along, side = dx * ux + dy * uy, abs(dy * ux - dx * uy)
        if along < -TILE / 2:
            return d * SIDE_PENALTY + LEAD_MAX_M
        return max(0.0, along) + SIDE_PENALTY * max(0.0, side - TILE / 2)
    return key


def view_key(conn, aid, x, y):
    """The build order of a tracker's tiles: nearest first, a tile outside the camera's view cone counting
    farfield.VIEW_PENALTY times as far (the far field's own rule, farfield.view_distance): what the player sees
    comes first, the tiles behind the car later. Plain distance without the pose's camera."""
    import farfield
    cam = getattr(conn, "cam", None)
    cone = farfield.view_cone(cam) if isinstance(cam, dict) and getattr(conn, "cam_aid", None) == aid else None

    def key(k):
        d = tile_distance(k[0], k[1], x, y)
        if cone is None or d <= 0:
            return d
        cx, cy, fx, fy, cosmax = cone
        x0, y0, x1, y1 = tile_bbox(k[0], k[1])
        dx, dy = (x0 + x1) / 2 - cx, (y0 + y1) / 2 - cy
        r = math.hypot(dx, dy)
        if r <= (x1 - x0) or (dx * fx + dy * fy) / r >= cosmax:
            return d
        return d * farfield.VIEW_PENALTY
    return key




def cells_of_tile(frame, tx, ty, margin=OVERLAP):
    x0, y0, x1, y1 = tile_bbox(tx, ty)
    lat, lon, _ = frame.enu_to_geodetic(np.array([x0 - margin, x1 + margin, x0 - margin, x1 + margin]),
                                        np.array([y0 - margin, y0 - margin, y1 + margin, y1 + margin]), 0.0)
    return mapcells.cells_for_bbox(float(np.min(lat)), float(np.min(lon)), float(np.max(lat)), float(np.max(lon)))


def _chunk(look, w, c, node_widths=None, reference=None):
    if node_widths is not None and np.ndim(node_widths) == 2:
        reference, node_widths = node_widths, None
    reference = look.get("uv_reference", reference)
    reference = getattr(reference, "points", reference)
    if look.get("ramp_guidance") and reference is not None:
        ab = np.diff(reference[:, :2], axis=0)
        t = np.clip(((c[0, :2]-reference[:-1, :2])*ab).sum(1)/np.maximum((ab*ab).sum(1), 1e-12), 0., 1.)
        k = int(np.argmin(np.linalg.norm(reference[:-1, :2]+t[:, None]*ab-c[0, :2], axis=1)))
        steps = np.diff(c[:, :2], axis=0)
        moving = np.flatnonzero(np.linalg.norm(steps, axis=1) > 1e-9)
        if len(moving) and steps[moving[0]] @ ab[k] < 0.:
            c = c[::-1].copy()
            if c.shape[1] == 5: c[:, 3:5] *= -1.
    chunk = {"m": look["m"], "w": round(float(w), 2), "tl": look["tl"], "p": look["p"],
             "n": [[round(float(v), 3) for v in p[:3]] for p in c]}
    if look.get("uv_scale"):
        chunk["uv_scale"] = list(look["uv_scale"])
    for key in ("uv_shift", "uv_start"):
        if look.get(key) is not None:
            chunk[key] = float(look[key])
    if node_widths is not None:
        widths = np.asarray(node_widths, np.float64)
        if len(widths) == len(c) and np.isfinite(widths).all():
            chunk["wn"] = [round(float(v), 3) for v in widths]
    if c.shape[1] == 5:
        chunk["cross"] = np.round(c[:, 3:5], 7).tolist()
    for key in ("highway_lane", "ramp_guidance", "terminal_solid"):
        if look.get(key): chunk[key] = True
    if look.get("dash") and look.get("gap"):
        chunk.update(dash=float(look["dash"]), gap=float(look["gap"]))
    if reference is not None and (look["m"].startswith("line_dashed") or look.get("continuous_uv")):
        ab = np.diff(reference[:, :2], axis=0)
        lengths = np.linalg.norm(ab, axis=1)
        t = np.clip(((c[0, :2]-reference[:-1, :2])*ab).sum(1)/np.maximum(lengths**2, 1e-12), 0., 1.)
        k = int(np.argmin(np.linalg.norm(reference[:-1, :2]+t[:, None]*ab-c[0, :2], axis=1)))
        heading = ab[0]/max(lengths[0], 1e-9)
        chunk["uv_start"] = float(reference[0, :2] @ heading + lengths[:k].sum()+t[k]*lengths[k])
    return chunk


DECK = {"m": "bne_bridge_pavement", "tl": 5.0, "p": 20,
        "uv_scale": (7.2, 5.0)}     # grooved concrete slab, lane-scale UVs follow the deck curve
DECK_STEP = 4.0         # m between deck profile points
DECK_CLEAR = 0.05       # m a deck stays above the ground under it (no z-fighting where it rests on it)
# m a deck's top stays over a road crossing under it: 5.3 m under the slab and girders (roadmesh DECK_DEPTH 1.2 +
# GIRDER_D 1.0), over the 4.9 m (16 ft) of US interstates. It was 6.5 (5 m + the slab, before the girders): 4.3 m under
# the girders, a truck's height (trucks hit a deck over I-20 near Temple, GA; from KyzoFox's 1.5.4, PR #64)
DECK_ROAD_CLEAR = 7.5
DECK_GRADE = 0.05       # that requirement falls 5 % along the deck from the crossing
DECK_MAX_GRADE = 0.06   # a deck climbs no steeper than this from an abutment
AT_GRADE_M = 3.0        # m: a road crossing a deck within AT_GRADE_REACH_M of an abutment that cannot get this
AT_GRADE_REACH_M = 12.0     # high over it is a junction at grade (the intersection a ramp starts from)
CHAIN_CELLS = 12        # map cells read at most to close one bridge chain


def _crossings(lines, ground_lines):
    """(part index, arc m, point xy) where a ground road (polylines in metres) crosses under a deck
    part. Pure."""
    out = []
    for pi, ln in enumerate(lines):
        lo, hi = ln.min(axis=0), ln.max(axis=0)
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(ln, axis=0).T))]
        P, Q = ln[:-1], ln[1:]
        for gl in ground_lines:
            if gl[:, 0].max() < lo[0] or gl[:, 0].min() > hi[0] or gl[:, 1].max() < lo[1] or gl[:, 1].min() > hi[1]:
                continue
            for a, b in zip(gl[:-1], gl[1:]):
                r, d = Q - P, b - a
                den = r[:, 0] * d[1] - r[:, 1] * d[0]
                ok = np.abs(den) > 1e-12
                ap = a - P
                t = np.where(ok, (ap[:, 0] * d[1] - ap[:, 1] * d[0]) / np.where(ok, den, 1), -1)
                u = np.where(ok, (ap[:, 0] * r[:, 1] - ap[:, 1] * r[:, 0]) / np.where(ok, den, 1), -1)
                for j in np.nonzero(ok & (t >= 0) & (t <= 1) & (u >= 0) & (u <= 1))[0]:
                    out.append((pi, s[j] + t[j] * (s[j + 1] - s[j]), P[j] + t[j] * r[j]))
    return out


def _deck_crossings(lines):
    """(i, j, arc on i, arc on j) where deck parts i < j cross (not at their end points). Pure."""
    import fast
    if not lines:
        return []
    fast.require(0, "deck_crossings")
    pts = [np.asarray(ln, np.float64)[:, :2] for ln in lines]
    starts = np.r_[0, np.cumsum([len(p) for p in pts])].astype(np.int64)
    return fast._ef.deck_crossings(np.ascontiguousarray(np.vstack(pts)), starts)


PIER_SPAN_M = 32.0      # m between a bridge's piers (a span), longer under a high deck: + PIER_SPAN_PER_M per m of height
PIER_SPAN_PER_M = 0.6
PIER_SPAN_MAX = 60.0
PIER_MIN_CLEAR = 1.5    # m from the ground up to a pier's top (under the girders): lower, the deck rests on the ground
PIER_ROAD_M = 7.0       # m a pier keeps from the centre line of a road under the deck (its half width + room)
PIER_CHANNEL_CLEAR = 15.0   # no pier in water whose run wants this much clearance (water_clear): a ship channel
PIER_IN_WATER_M = 8.0   # m a pier in water reaches under its surface


def pier_sites(xy, z, width, ground_fn, road_lines=(), wet_fn=None):
    """The piers under a bridge deck (xy (k, 2), deck top z (k,), width m): [(x, y, ux, uy, z_top, z_bottom, width)],
    (ux, uy) along the deck, z_top under its girders (roadmesh.DECK_DEPTH + GIRDER_D), z_bottom in the ground (or
    PIER_IN_WATER_M under water); evenly spaced inside the part (its ends are abutments or joints) at PIER_SPAN_M,
    longer under a higher deck; none where the deck is within PIER_MIN_CLEAR of the ground, moved along up to 40 % of
    a span off a road under it (PIER_ROAD_M) or left out; none in a ship channel. The official maps' bridges stand
    on piers (Italy's tapered walls, East Coast's) where ours floated (the user, 2026-10-17: "Bridges need a lot of
    work in general"). Pure given ground_fn(xy) -> heights, wet_fn(xy) -> bool."""
    import fast
    import roadmesh
    xy = np.asarray(xy, np.float64)
    if len(xy) < 2 or not np.isfinite(z).all():
        return []
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    L = float(s[-1])
    g_all = np.asarray(ground_fn(xy), np.float64)
    h = float(np.median(z - g_all))
    span = float(np.clip(PIER_SPAN_M + PIER_SPAN_PER_M * max(h, 0.0), PIER_SPAN_M, PIER_SPAN_MAX))
    n = int(round(L / span))
    if n < 2:
        return []
    under = roadmesh.DECK_DEPTH + roadmesh.GIRDER_D
    segs = [np.asarray(ln, np.float64) for ln in road_lines if len(ln) >= 2]
    A = np.concatenate([ln[:-1] for ln in segs]) if segs else np.zeros((0, 2))
    B = np.concatenate([ln[1:] for ln in segs]) if segs else np.zeros((0, 2))
    wet_runs = None
    if wet_fn is not None:
        w = np.asarray(wet_fn(xy), bool)
        wet_runs = (w, s)
    out = []
    pw = float(np.clip(0.7 * width, 2.5, 14.0))
    for i in range(1, n):
        placed = None
        for off in (0.0, 0.25, -0.25, 0.4, -0.4):
            sc = L * i / n + off * span
            if sc < 0.5 * span or sc > L - 0.5 * span:
                continue
            p = np.array([np.interp(sc, s, xy[:, 0]), np.interp(sc, s, xy[:, 1])])
            j = min(int(np.searchsorted(s, sc)), len(s) - 1)
            t = xy[j] - xy[max(j - 1, 0)]
            t = t / max(float(np.hypot(*t)), 1e-9)
            nrm = np.array([-t[1], t[0]])
            foot = np.array([p + nrm * pw / 2, p - nrm * pw / 2, p])
            if len(A):
                fast.require(0, "points_near_segments")
                hit = fast._ef.points_near_segments(foot, A, B, np.full(len(A), PIER_ROAD_M))
                if np.asarray(hit).any():
                    continue
            zt = float(np.interp(sc, s, z)) - under
            g = float(ground_fn(p[None])[0])
            if zt - g < PIER_MIN_CLEAR:
                continue
            zb = g - roadmesh.FOOTING_M
            if wet_runs is not None:
                w, ss = wet_runs
                k = int(np.argmin(np.abs(ss - sc)))
                if w[k]:
                    a = k
                    while a > 0 and w[a - 1]:
                        a -= 1
                    b = k
                    while b + 1 < len(w) and w[b + 1]:
                        b += 1
                    if water_clear(ss[b] - ss[a]) >= PIER_CHANNEL_CLEAR and a < k < b:
                        continue
                    zb = g - PIER_IN_WATER_M
            placed = (float(p[0]), float(p[1]), float(t[0]), float(t[1]), zt, zb, float(width))
            break
        if placed is not None:
            out.append(placed)
    return out


class BridgeProfiles:
    """Deck heights per bridge part for one anchor frame, each chain resolved once from all of it, so
    every tile gets the same deck (2026-10-11: computed per tile from the tile's cells, a chain's
    two halves disagreed at a tile seam). cell_fn(key) -> the roads Features of a map cell; elev:
    the physics elevation. Worker threads."""

    wet = None      # wet(lat, lon) -> water depth (m) or None: decks clear the water (water_clear)
    infra_fn = None     # infra_fn(cell key) -> the infrastructure Features (bridge:structure tags: bridgetypes)

    def __init__(self, frame, cell_fn, elev):
        self.frame, self.cell_fn, self.elev = frame, cell_fn, elev
        self.done = {}                          # (fid, j) -> (spec, xy (k, 2), z (k,), lon)
        self.piers = {}                         # (fid, j) -> pier_sites of its deck
        self.trusses = {}                       # a chain's first part key -> its truss beams (bridgetypes)
        self.lock = threading.Lock()

    def _water(self, lines, nav=0.0):
        """Clearance requirements over water (deck_profile_once crossings with their own clearance): every
        WATER_STEP_M along each wet run of each line, water_clear of the run's length over the ground there; over a
        run of NAV_RUN_M or more at least nav (m: the bridge's navigation clearance, bridgetypes.nav_clearance) plus
        the slab and girders under the road."""
        out = []
        if self.wet is None:
            return out
        import roadmesh
        under = roadmesh.DECK_DEPTH + roadmesh.GIRDER_D
        for k, ln in enumerate(lines):
            xy = np.asarray(ln, np.float64)[:, :2]
            if len(xy) < 2:
                continue
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
            w = np.asarray(self.wet(la, lo), np.float64) > 0.5
            if not w.any():
                continue
            g = self._ground(xy)
            i = 0
            while i < len(w):
                if not w[i]:
                    i += 1
                    continue
                j = i
                while j + 1 < len(w) and w[j + 1]:
                    j += 1
                c = water_clear(s[j] - s[i] + (s[1] - s[0]))
                if nav > 0 and s[j] - s[i] >= NAV_RUN_M:
                    c = max(c, nav + under)
                for sc in np.arange(s[i], s[j] + 1e-6, WATER_STEP_M):
                    out.append((k, float(sc), float(np.interp(sc, s, g)), c))
                i = j + 1
        return out

    def _ground(self, xy):
        la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return self.frame.geodetic_to_enu(la, lo, np.asarray(self.elev(la, lo), np.float64))[2]

    specs_of = staticmethod(lambda feats: bridge_specs(feats))
    parts_of = staticmethod(lambda feats, i: bridge_parts(feats, i))
    levels_of = staticmethod(lambda feats, i: bridge_levels(feats, i))

    def _load(self, key, pool, ground):
        feats = self.cell_fn(key)
        if feats is None or len(feats) == 0:
            return
        bs = self.specs_of(feats)
        rs = road_specs(feats)
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            if bs[i] is not None:
                lvls = self.levels_of(feats, i)
                for j, ring in enumerate(self.parts_of(feats, i)):
                    # a part in several cells can come clipped differently: the longest (the same part whichever
                    # cell a process loads first: the terrain and the road tiles took different ones, 2026-10-17)
                    old = pool.get((fid, j))
                    if old is None or len(ring) > old[4]:
                        e, n = mapgeom.enu_xy(feats, self.frame, ring)
                        pool[(fid, j)] = (bs[i], densify(_dedupe(np.stack([e, n], axis=1)), DECK_STEP),
                                          float(np.mean(ring[:, 0])), lvls[j] if j < len(lvls) else 1.0, len(ring))
            if rs[i] is not None and (fid, "g") not in ground:
                ground[(fid, "g")] = []
                for ring in ground_parts(feats, i):
                    e, n = mapgeom.enu_xy(feats, self.frame, ring)
                    ground[(fid, "g")].append(np.stack([e, n], axis=1))

    def _resolve(self, start, pool, ground, loaded):
        """The chain of part `start` (keys), loading the cell at each loose end until closed."""
        def nk(p):
            return (round(float(p[0]) * 2), round(float(p[1]) * 2))
        while True:
            by_node = {}
            for k, (_, xy, *_) in pool.items():
                by_node.setdefault(nk(xy[0]), []).append(k)
                by_node.setdefault(nk(xy[-1]), []).append(k)
            keys = sorted(pool)
            over = _deck_crossings([pool[k][1] for k in keys])
            crossing = {}
            for a, b, _, _ in over:
                crossing.setdefault(keys[a], set()).add(keys[b])
                crossing.setdefault(keys[b], set()).add(keys[a])
            # and side by side / one over the other (solved apart, the Queensboro Bridge's two upper roadways met at
            # a 0.5 m step and its tiles' decks at different heights stood as blocks in the lanes, 2026-10-17)
            for a, b in _deck_beside([pool[k][1] for k in keys], [pool[k][0].width for k in keys]):
                crossing.setdefault(keys[a], set()).add(keys[b])
                crossing.setdefault(keys[b], set()).add(keys[a])
            comp, stack = {start}, [start]
            while stack:                        # joined at nodes, or crossing (solved together)
                k = stack.pop()
                xy = pool[k][1]
                nxt = [k2 for p in (xy[0], xy[-1]) for k2 in by_node.get(nk(p), ())]
                for k2 in nxt + sorted(crossing.get(k, ())):
                    if k2 not in comp:
                        comp.add(k2)
                        stack.append(k2)
            deg = {}
            for k in comp:
                xy = pool[k][1]
                for p in (xy[0], xy[-1]):
                    deg[nk(p)] = deg.get(nk(p), 0) + 1
            grew = False
            for k in comp:
                xy = pool[k][1]
                for p in (xy[0], xy[-1]):
                    if deg[nk(p)] != 1:
                        continue
                    la, lo, _ = self.frame.enu_to_geodetic(np.array([p[0]]), np.array([p[1]]), 0.0)
                    key = mapcells.cell_of(float(la[0]), float(lo[0]))
                    if str(key) not in loaded and len(loaded) < CHAIN_CELLS:
                        loaded.add(str(key))
                        self._load(key, pool, ground)
                        grew = True
            if not grew:
                return sorted(comp)

    def _solve(self, comp, pool, ground):
        """Deck heights of the chain comp (keys of pool)."""
        lines = [pool[k][1] for k in comp]
        gl = [g for v in ground.values() for g in v]
        cr = _crossings(lines, gl)
        road_z = self._ground(np.array([c[2] for c in cr])) if cr else []
        import bridgetypes
        try:
            nav = bridgetypes.nav_clearance(self.frame, lines)
        except Exception as ex:             # the share-of-span rule alone
            log.debug("nav clearance: %s: %s", type(ex).__name__, ex)
            nav = 0.0
        return deck_profile(lines, self._ground, [(c[0], c[1], z) for c, z in zip(cr, road_z)] + self._water(lines, nav),
                            levels=[pool[k][3] for k in comp], widths=[pool[k][0].width for k in comp])

    def parts(self, cells_feats, box):
        """[(fid, spec, xy, z, lon)] of the bridge parts in these cells that come near box."""
        x0, y0, x1, y1 = box
        out, todo, queued = [], [], set()
        for feats in cells_feats:
            if feats is None or len(feats) == 0:
                continue
            bs = self.specs_of(feats)
            for i in range(len(feats)):
                if bs[i] is None:
                    continue
                fid = str(feats.ids[i])
                for j, ring in enumerate(self.parts_of(feats, i)):
                    if (fid, j) in queued:          # a feature in several of the cells: once (2026-10-17: every
                        continue                    # part came once per cell it is in, decks made 2-3 times over)
                    e, n = mapgeom.enu_xy(feats, self.frame, ring)
                    if e.max() < x0 - NEAR_M or e.min() > x1 + NEAR_M or n.max() < y0 - NEAR_M or n.min() > y1 + NEAR_M:
                        continue
                    queued.add((fid, j))
                    todo.append(((fid, j), feats, i))
        for key, feats, i in todo:
            with self.lock:
                got = self.done.get(key)
            if got is None:
                pool, ground, loaded = {}, {}, set()
                ring = self.parts_of(feats, i)[key[1]]
                for p in (ring[0], ring[-1]):
                    ck = mapcells.cell_of(float(p[1]), float(p[0]))
                    if str(ck) not in loaded:
                        loaded.add(str(ck))
                        self._load(ck, pool, ground)
                if key not in pool:
                    continue
                comp = self._resolve(key, pool, ground, loaded)
                zs = self._solve(comp, pool, ground)
                with self.lock:
                    for k, z in zip(comp, zs):
                        spec, xy, lon = pool[k][:3]
                        old = self.done.get(k)
                        # the first solve of a part is kept, and what goes with it, unless this one saw more of it
                        if old is None or len(xy) > len(old[1]):
                            self.done[k] = (spec, xy, z, lon)
                            self._commit(k, ground)
                    chain = [(self.done[k][1], self.done[k][2], self.done[k][0].width) for k in comp
                             if k in self.done]
                    ck = min(comp) if comp else None
                    fresh = ck is not None and ck not in self.trusses
                    if fresh:
                        self.trusses[ck] = []
                    got = self.done.get(key)
                if fresh:
                    beams, tpiers = self._structure(chain)
                    with self.lock:
                        self.trusses[ck] = beams
                        self.piers[("truss", ck)] = tpiers
            if got is not None:
                spec, xy, z, lon = got
                out.append((self._part_id(key), spec, xy, z, lon))
        return out

    def _commit(self, k, ground=None):
        """Under self.lock: part k's solve was kept: its piers (pier_sites; TunnelProfiles keeps its sections)."""
        spec, xy, z, _lon = self.done[k]
        lines = [ln for v in (ground or {}).values() for ln in v]
        try:
            self.piers[k] = pier_sites(np.asarray(xy, np.float64), np.asarray(z, np.float64), spec.width,
                                       self._ground, lines, self._wet_at)
        except Exception as ex:             # no piers rather than no bridge
            log.debug("piers of %s: %s: %s", k, type(ex).__name__, ex)
            self.piers[k] = []

    def _wet_at(self, xy):
        if self.wet is None:
            return np.zeros(len(xy), bool)
        la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return np.asarray(self.wet(la, lo), np.float64) > 0.5

    def _roads_near(self, xy):
        """The ground road lines (k, 2) of the map cells around the points xy (n, 2) (an anchor pier's site)."""
        la, lo, _ = self.frame.enu_to_geodetic(np.asarray(xy)[:, 0], np.asarray(xy)[:, 1], 0.0)
        out = []
        for key in {mapcells.cell_of(float(a), float(b)) for a, b in zip(la, lo)}:
            feats = self.cell_fn(key)
            if feats is None or len(feats) == 0:
                continue
            rs = road_specs(feats)
            for i in range(len(feats)):
                if rs[i] is not None:
                    for ring in ground_parts(feats, i):
                        e, n = mapgeom.enu_xy(feats, self.frame, ring)
                        out.append(np.stack([e, n], axis=1))
        return out

    def _structure(self, chain):
        """The truss beams of a solved chain [(xy, z, width)] and the piers under its ends (bridgetypes); ([], []) for a
        plain deck."""
        import bridgetypes
        try:
            infra = []
            if self.infra_fn is not None:
                pts = np.concatenate([np.asarray(xy, np.float64) for xy, _, _ in chain])
                la, lo, _ = self.frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
                infra = [self.infra_fn(k) for k in mapcells.cells_for_bbox(float(la.min()), float(lo.min()),
                                                                           float(la.max()), float(lo.max()))]
            wet = self._wet_at if self.wet is not None else None
            beams, piers = [], []
            for info in bridgetypes.chain_structures(self.frame, chain, wet, infra):
                b, p = bridgetypes.structure_members(chain, info, self._ground, wet, road_lines=self._roads_near)
                beams += b
                piers += p
            return beams, piers
        except Exception as ex:             # a plain deck rather than no bridge
            log.debug("bridge structure: %s: %s", type(ex).__name__, ex)
            return [], []

    def beams_in(self, box):
        """The truss beams (p, q, w, h) of the chains solved so far whose middle lies in box (each in one tile)."""
        x0, y0, x1, y1 = box
        with self.lock:
            allb = [b for v in self.trusses.values() for b in v]
        return [b for b in allb if x0 <= (b[0][0] + b[1][0]) / 2 < x1 and y0 <= (b[0][1] + b[1][1]) / 2 < y1]

    def piers_in(self, box):
        """The piers (pier_sites) of the parts solved so far standing in box (x0, y0, x1, y1): x0 <= x < x1, y0 <= y
        < y1 (each in one tile)."""
        x0, y0, x1, y1 = box
        with self.lock:
            allp = [p for v in self.piers.values() for p in v]
        return [p for p in allp if x0 <= p[0] < x1 and y0 <= p[1] < y1]

    @staticmethod
    def _part_id(key):
        return key[0]


def tunnel_sections(s, g, open_cover=None, full_cover=None, min_m=SECTION_MIN_M):
    """A tunnel's road level and its sections from the ground over it (g at arc lengths s): (floor, kinds). floor: the
    lower convex hull of (s, g), the road's level under the covered parts and the ground where it lies open; kinds per
    sample: 0 open road, 1 a low arch, 2 a full tunnel (a run with TUNNEL_OPEN_COVER over the floor, full where it
    reaches TUNNEL_COVER; runs and gaps shorter than min_m merged away). One long 'tunnel' in the map can be a sunken
    road with arches and a rock tunnel along it (Central Park's 79th Street Transverse, 683 m: one tube, 2026-10-17).
    The Rust kernel (fast). Pure."""
    s = np.ascontiguousarray(s, np.float64)
    g = np.ascontiguousarray(g, np.float64)
    oc = TUNNEL_OPEN_COVER if open_cover is None else open_cover
    fc = TUNNEL_COVER if full_cover is None else full_cover
    step = float(np.median(np.diff(s))) if len(s) > 1 else 1.0
    min_n = max(1, int(math.ceil(min_m / max(step, 1e-6))))
    import fast
    fast.require(28, "tunnel_sections")
    f, k = fast._ef.tunnel_sections(s, g, float(oc), float(fc), int(min_n))
    return _shallow_full_low(s, g, np.asarray(f), np.asarray(k))


def _shallow_full_low(s, g, floor, kinds):
    """kinds with each full-tunnel run (2) shorter than FULL_MIN_M and nowhere FULL_DEEP_M under the ground a low arch
    (1). Pure."""
    kinds = np.array(kinds, np.int64)
    i, n = 0, len(kinds)
    while i < n:
        if kinds[i] != 2:
            i += 1
            continue
        j = i
        while j + 1 < n and kinds[j + 1] == 2:
            j += 1
        if s[j] - s[i] < FULL_MIN_M and float(np.max(g[i:j + 1] - floor[i:j + 1])) < FULL_DEEP_M:
            kinds[i:j + 1] = 1
        i = j + 1
    return floor, kinds


class TunnelProfiles(BridgeProfiles):
    """Floor heights per tunnel part for one anchor frame, each chain (joined parts: a tunnel split over several
    segments) resolved once, as BridgeProfiles; each part cut into its sections (tunnel_sections): open roads
    (open_parts), low arches (heights) and full tunnels, each a part of its own (fid#k) with its own portals. Worker
    threads."""
    specs_of = staticmethod(lambda feats: tunnel_specs(feats))
    parts_of = staticmethod(lambda feats, i: tunnel_parts(feats, i))
    levels_of = staticmethod(lambda feats, i: [-1.0] * len(tunnel_parts(feats, i)))

    def _structure(self, chain):                # (no truss over a tunnel)
        return [], []

    wet = None      # wet(lat, lon) -> water depth (m): tunnels under water are left out (the sea's WaterBlocks reach
                    # 100 m down: a car in the tube would float; earthd / blockworker set the sea's depth)
    open = None
    raw = None      # raw(lat, lon) -> the ground without the roads' grading (the cover over a tunnel is read on it)
    ends = None     # ends(lat, lon) -> the graded ground (its portals' floor), else the profiles' elev. Both at one DEM
                    # zoom (grading.PROFILE_ZOOM) in every process: the terrain blocks and the road tiles are built in
                    # different processes at different zooms, and cut 97th Street's arch 15 m apart: its west mouth
                    # filled with ground behind the portal face (2026-10-17)

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.open = set()           # sub-part fids that are open roads
        self.heights = {}           # sub-part fid -> a low arch's tube height
        self.ups = {}               # sub-part fid -> its portal faces' reach over the vault at its start and end
        self.sections = {}          # part fid -> [(i0, i1, kind)] (inclusive sample ranges, sharing their ends)
        self.caps = {}              # sub-part fid -> a low arch's stone top's depth at its start and end (None: the
                                    # full roadmesh.LOW_CAP_M), stopping short of a road over its mouth (_road_caps)
        self._hw = {}               # (fid, "g") -> half the carriageway of a ground road in `ground` (_load)
        self.mouths = {}            # sub-part fid -> (its start is a portal, its end is one): not where a low arch
                                    # meets a full tunnel inside the hill (no hole, no portal face there)
        self._tls = threading.local()   # a thread's solved sections, kept with its parts (_commit) or dropped

    def _raw_ground(self, xy):
        la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return self.frame.geodetic_to_enu(la, lo, np.asarray(self.raw(la, lo), np.float64))[2]

    @staticmethod
    def _part_id(key):
        # one feature can come as several parts (clipped differently in the map cells around: 97th Street's arch as
        # 45 m and 30 m pieces, the shorter one's sections cut the longer one's terrain 15 m short, 2026-10-17)
        return f"{key[0]}.{key[1]}"

    def _commit(self, k, ground=None):
        staged = getattr(self._tls, "staged", {}).pop(k, None)
        if staged is None:
            return
        pid = self._part_id(k)
        secs, subs = staged
        self.sections[pid] = secs
        for n, (kind, th, ups, mouths, caps) in subs.items():
            sub = f"{pid}#{n}"
            if kind == 0:
                self.open.add(sub)
                continue
            if kind == 1:
                self.heights[sub] = th
            self.ups[sub] = ups
            self.mouths[sub] = mouths
            if caps is not None:
                self.caps[sub] = caps

    def _ends_ground(self, xy):
        if self.ends is None:
            return self._ground(xy)
        la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return self.frame.geodetic_to_enu(la, lo, np.asarray(self.ends(la, lo), np.float64))[2]

    def _solve(self, comp, pool, ground):
        """Per part: the floor (the hull of the ungraded ground, its ends at the roads' graded height at its portals,
        where the approaches arrive) and its sections; a low arch's tube under the thin part of the cover over its
        middle (a 20th percentile of the middle 60 %, less TUNNEL_SOIL_M; sized from the most, the ground came down
        into it and was cut away, a bare stone vault), a full tunnel's roadmesh.TUNNEL_H."""
        import roadmesh
        gfn = self._raw_ground if self.raw is not None else self._ground
        out = []
        self._tls.staged = {}           # (committed by parts() right after, the rest dropped with the next solve)
        for k in comp:
            xy = np.asarray(pool[k][1], np.float64)
            if len(xy) < 2:
                out.append(np.full(len(xy), np.nan))
                continue
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            g = np.asarray(gfn(xy), np.float64)
            ge = np.asarray(self._ends_ground(xy[[0, -1]]), np.float64)
            g = g.copy()
            g[0], g[-1] = ge[0], ge[-1]
            floor, kinds = tunnel_sections(s, g)
            secs, i = [], 0
            while i < len(kinds):
                j = i
                while j + 1 < len(kinds) and kinds[j + 1] == kinds[i]:
                    j += 1
                secs.append((i, j, int(kinds[i])))
                i = j + 1
            # neighbouring sections share their end sample (no gap between a portal and the open road)
            secs = [(a if n == 0 else a - 1, b, kd) for n, (a, b, kd) in enumerate(secs)]
            cover = g - floor
            log.debug("tunnel %s: sections %s over %.0f m", k, secs, s[-1])
            subs = {}
            for n, (a, b, kd) in enumerate(secs):
                if kd == 0:
                    subs[n] = (0, None, None, None, None)
                    continue
                th = roadmesh.TUNNEL_H
                if kd == 1:
                    c_ = cover[a:b + 1]
                    k0, k1 = int(len(c_) * 0.2), max(int(len(c_) * 0.8), int(len(c_) * 0.2) + 1)
                    base = float(np.percentile(c_[k0:k1], 20))
                    # nor over the thinnest cover along it (ARCH_SIZE_SKIP_M at its mouths aside, where the ground
                    # rises over it): the vault follows a sloping floor, and sized from the middle it rose out of the
                    # ground near a mouth, its stone top 2 m over the E6 beside it (an underpass in Norway, GitHub #2)
                    s_ = s[a:b + 1]
                    inner = (s_ >= s_[0] + ARCH_SIZE_SKIP_M) & (s_ <= s_[-1] - ARCH_SIZE_SKIP_M)
                    if inner.any():
                        base = min(base, float(c_[inner].min()))
                    th = float(np.clip(base - TUNNEL_SOIL_M, TUNNEL_LOW_H, roadmesh.TUNNEL_H))
                ups = []
                for near in (s[a:b + 1] <= s[a] + PORTAL_LOOK_M, s[a:b + 1] >= s[b] - PORTAL_LOOK_M):
                    cv = float(np.max(cover[a:b + 1][near])) if near.any() else th
                    ups.append(float(np.clip(cv + PORTAL_GROUND_M - th, PORTAL_UP_MIN, roadmesh.PORTAL_FRAME)))
                mouths = (n == 0 or secs[n - 1][2] == 0, n == len(secs) - 1 or secs[n + 1][2] == 0)
                caps = None
                if kd == 1:
                    caps, ups = self._road_caps(xy[a:b + 1], floor[a:b + 1], th, ups, mouths, pool[k][0].width, ground)
                subs[n] = (kd, th, ups, mouths, caps)
            self._tls.staged[k] = (secs, subs)
            out.append(floor)
        return out

    def _load(self, key, pool, ground):
        super()._load(key, pool, ground)
        feats = self.cell_fn(key)
        if feats is None or len(feats) == 0:
            return
        rs = road_specs(feats)
        for i in range(len(feats)):
            if rs[i] is not None:
                self._hw[(str(feats.ids[i]), "g")] = rs[i].width / 2

    def _road_caps(self, xy, floor, th, ups, mouths, width, ground):
        """A low arch's stone tops (roadmesh.LOW_CAP_M back from each portal face over its mouth) and face reaches where
        a road on the ground runs over a mouth (not one joined there: ends of it within ROAD_CAP_JOIN_M): the top stops
        ROAD_CAP_GAP_M short of the carriageway, the face ROAD_CAP_OVER_M over the road at most (an underpass under
        the E6 in Norway: its stone top 1.7 m over the lanes, 4 m into them, GitHub #2). ((start, end) depths or None
        where nothing changes, ups)."""
        import fast
        import roadmesh
        fast.require(45, "first_probe_near_lines")
        side = width / 2 + roadmesh.TUNNEL_SHOULDER + roadmesh.LOW_PORTAL_SIDE
        caps, ups, any_ = [None, None], list(ups), False
        lines, radius = [], []
        for gk, lns in ground.items():
            hw = self._hw.get(gk)
            if hw is None:
                continue
            for ln in lns:
                lines.append(np.asarray(ln, np.float64).reshape(-1, 2))
                radius.append(hw)
        if not lines or len(xy) < 2:
            return None, ups
        pts = np.concatenate(lines)
        starts = np.concatenate([[0], np.cumsum([len(ln) for ln in lines])]).astype(np.int64)
        radius = np.asarray(radius, np.float64)
        ds = np.arange(0.0, roadmesh.LOW_CAP_M + 0.25, 0.5)
        for e, (at, nb) in enumerate(((0, 1), (-1, -2))):
            if not mouths[e]:
                continue
            p = xy[at]
            tin = xy[nb] - p
            tin = tin / max(float(np.hypot(*tin)), 1e-9)
            nrm = np.array([-tin[1], tin[0]])
            mid = p + tin * ds[:, None]
            probes = np.stack([mid, mid + nrm * side, mid - nrm * side], 1).reshape(-1, 2)
            got = fast._ef.first_probe_near_lines(pts, starts, radius, ROAD_CAP_GAP_M, (float(p[0]), float(p[1])),
                                                  ROAD_CAP_JOIN_M, probes)
            depth = None
            if got is not None:
                i, hx, hy = got
                depth = max(0.0, float(ds[i // 3]) - ROAD_CAP_GAP_M)
                road_z = float(self._ground(np.array([[hx, hy]]))[0])
                top = road_z + ROAD_CAP_OVER_M - float(floor[at]) - th
                ups[e] = float(max(roadmesh.LOW_PORTAL_UP, min(ups[e], top)))
            if depth is not None:
                caps[e], any_ = depth, True
        return (tuple(caps) if any_ else None), ups

    def cap_m(self, fid):
        """(start, end): a low arch's stone top depth over each mouth, None for roadmesh.LOW_CAP_M."""
        with self.lock:
            return self.caps.get(fid, (None, None))

    def portal_ups(self, fid):
        """(at the part's start, at its end): the portal faces' reach over the vault, PORTAL_GROUND_M over the ground
        just inside (within PORTAL_LOOK_M), PORTAL_UP_MIN..roadmesh.PORTAL_FRAME (a 6 m frame stood ~10 m over the
        flat park at the 79th Street Transverse, 2026-10-17); the full frame when not known."""
        import roadmesh
        with self.lock:
            return self.ups.get(fid, [roadmesh.PORTAL_FRAME, roadmesh.PORTAL_FRAME])

    def height(self, fid):
        """The tube's height (floor to vault) of a sub-part: a low arch's, else roadmesh.TUNNEL_H."""
        import roadmesh
        with self.lock:
            return self.heights.get(fid, roadmesh.TUNNEL_H)

    def mouth(self, fid):
        """(start, end) of a sub-part: True where it is a portal."""
        with self.lock:
            return self.mouths.get(fid, (True, True))

    def _split(self, parts):
        """parts (whole map parts) as their sections' sub-parts (fid#n, spec, xy, z, lon)."""
        out = []
        for fid, spec, xy, z, lon in parts:
            with self.lock:
                secs = self.sections.get(fid)
            if not secs:
                out.append((f"{fid}#0", spec, xy, z, lon))
                continue
            for n, (a, b, kd) in enumerate(secs):
                if b > a:
                    out.append((f"{fid}#{n}", spec, np.asarray(xy)[a:b + 1], np.asarray(z)[a:b + 1], lon))
        return out

    def open_parts(self, cells_feats, box):
        """[(fid, spec, xy, z, lon)] of the open sections near box: roads at the floor (the ground graded to it,
        grading.Grader._open_tunnels; _tunnel_chunks)."""
        whole = super().parts(cells_feats, box)
        with self.lock:
            opened = set(self.open)
        # (an open section under water is an underwater tunnel with no ground over it: the Sydney Harbour Tunnel
        # was drawn as a road across the harbour, 2026-10-18)
        return self._dry([p for p in self._split(whole) if p[0] in opened])

    def parts(self, cells_feats, box):
        out = self._split(super().parts(cells_feats, box))
        with self.lock:
            opened = set(self.open)
        return self._dry([p for p in out if p[0] not in opened])

    def _dry(self, out):
        """The parts not under water for more than TUNNEL_WET_SHARE of their points."""
        if self.wet is None or not out:
            return out
        keep = []
        for p in out:
            xy = np.asarray(p[2], np.float64)
            la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
            # under water for a good part of it (a portal on a river bank, Fort Pitt's east end 0.6 m "in" the
            # Monongahela, is not: that dropped one of its two tubes, 2026-10-17)
            if (np.asarray(self.wet(la, lo)) > 0.5).mean() <= TUNNEL_WET_SHARE:
                keep.append(p)
        return keep


TUNNEL = {"m": "road_asphalt_2lane", "tl": 11.0, "p": 20}     # the road surface in a tunnel (a deck: collides)
LOW_HOLE_M = 3.0            # m from a low arch's ends: its mouths, flat at its floor (arch_raise) and cut away ...
LOW_HOLE_STEP = 2.5         # ... the cut a terrain grid step further (the squares from the floor up to the soil)
TUNNEL_HOLE_MARGIN = 0.5    # m around the tube a portal's terrain hole reaches (above, below and sideways)


def tunnel_hole_mask(parts, u, cx, cy, square, heights=None, mouths=None):
    """Where the terrain block (heights u [iy, ix]: sample (ix, iy) at (cx - half + ix * square, cy - half +
    iy * square), half = n * square / 2) must be a hole for the tunnels `parts` ([(fid, spec, xy, z, lon)]: a
    TunnelProfiles' parts): the samples over a tube's footprint whose ground lies between its floor and its
    vault (+- TUNNEL_HOLE_MARGIN): the portals' openings into the hillside (the ground over the vault stays, as
    does ground under a tube crossing a hollow). Pure."""
    import fast
    import roadmesh
    n = u.shape[0]
    half = n * square / 2
    x0, y0 = cx - half, cy - half
    mask = np.zeros(u.shape, bool)
    # twin tunnels (twin_tunnels): their one tube round both reaches over the strip between them too, and its mouths are
    # cut whatever the height (the ground left between the carriageways sloped into the shared opening, its portal face
    # covering what is above)
    parts = list(parts)
    n_own = len(parts)
    parts += [(parts[i][0], parts[i][1]._replace(width=w), mid[:, :2], mid[:, 2], parts[i][4])
              for i, j, mid, w in twin_tunnels([p for p in parts if np.isfinite(np.asarray(p[3])).all()]) if len(mid) >= 2]
    fast.require(0, "tunnel_holes")
    u = np.ascontiguousarray(u, np.float64)
    for k_, (_fid, spec, xy, z, _lon) in enumerate(parts):
        xy = np.asarray(xy, np.float64)
        z = np.asarray(z, np.float64)
        if len(xy) < 2 or not np.isfinite(z).all():
            continue
        hw = spec.width / 2 + roadmesh.TUNNEL_SHOULDER + TUNNEL_HOLE_MARGIN
        th = (heights or {}).get(_fid, roadmesh.TUNNEL_H)       # heights: a low arch's tube (TunnelProfiles.heights)
        low = th < roadmesh.TUNNEL_H - 0.05
        if k_ >= n_own and not low:     # a twin tube's mouths, whatever the height (the low arches' cut, no side reach)
            m0, m1 = (mouths or {}).get(_fid, (True, True))
            mouth_mask = np.zeros(u.shape, bool)
            fast._ef.tunnel_holes(mouth_mask, u, float(x0), float(y0), float(square), np.ascontiguousarray(xy),
                                  np.ascontiguousarray(z), float(hw), float(th), True, bool(m0), bool(m1),
                                  float(TUNNEL_HOLE_MARGIN), float(LOW_HOLE_M + LOW_HOLE_STEP), 0.0)
            mask |= mouth_mask
        # the Rust kernel: the samples over the part between its floor and vault (a low arch: its mouths only)
        m0, m1 = (mouths or {}).get(_fid, (True, True))
        fast._ef.tunnel_holes(mask, u, float(x0), float(y0), float(square), np.ascontiguousarray(xy),
                              np.ascontiguousarray(z), float(hw), float(th), bool(low), bool(m0), bool(m1),
                              float(TUNNEL_HOLE_MARGIN), float(LOW_HOLE_M + LOW_HOLE_STEP), float(ARCH_SIDE_M + 1.0))
    return mask


ARCH_SIDE_M = 1.0           # m beside a low arch's tube its soil is raised too
ARCH_SOIL_MIN = 0.6         # m of ground kept over a low arch's vault (arch_raise)


def arch_raise(parts, heights, px, py, z, mouths=None):
    """z (ground heights at the anchor-frame points px, py) raised over the low arches among parts (TunnelProfiles
    parts; heights: their tubes, TunnelProfiles.heights) to ARCH_SOIL_MIN over the vault, beside it too, but for
    their mouths (LOW_HOLE_M, cut there by tunnel_hole_mask): the ground dipping into a low tube near its mouths
    made one-sided terrain triangles from the road's level up to the soil, seen as dark wedges in the opening from
    outside (Central Park, 2026-10-17). Pure."""
    import fast
    import roadmesh
    z = np.array(z, np.float64, copy=True)
    fast.require(0, "arch_raise_part")
    px = np.ascontiguousarray(px, np.float64)
    py = np.ascontiguousarray(py, np.float64)
    for fid, spec, xy, zf, _lon in parts:
        th = (heights or {}).get(fid)
        if th is None or th >= roadmesh.TUNNEL_H - 0.05:
            continue
        xy = np.asarray(xy, np.float64)
        zf = np.asarray(zf, np.float64)
        if len(xy) < 2 or not np.isfinite(zf).all():
            continue
        hw = spec.width / 2 + roadmesh.TUNNEL_SHOULDER + ARCH_SIDE_M
        # the Rust kernel (per part) on the points in its box
        m0, m1 = (mouths or {}).get(fid, (True, True))
        lo, hi = xy.min(0) - hw, xy.max(0) + hw
        k = np.flatnonzero((px >= lo[0]) & (px <= hi[0]) & (py >= lo[1]) & (py <= hi[1]))
        if len(k):
            zk = np.ascontiguousarray(z[k])
            fast._ef.arch_raise_part(zk, np.ascontiguousarray(px[k]), np.ascontiguousarray(py[k]),
                                     np.ascontiguousarray(xy), np.ascontiguousarray(zf), float(hw), float(th),
                                     float(ARCH_SOIL_MIN), float(LOW_HOLE_M), bool(m0), bool(m1))
            z[k] = zk
    return z


def tunnel_floor_at(parts, x, y):
    """The floor height of the tunnel tubes among parts ([(fid, spec, xy, z, lon)]) over the point (x, y), the lowest
    one, or None outside them (within the road's half width + roadmesh.TUNNEL_SHOULDER). Pure."""
    import roadmesh
    best = None
    p = np.array([x, y], np.float64)
    for _fid, spec, xy, z, _lon in parts:
        xy = np.asarray(xy, np.float64)
        z = np.asarray(z, np.float64)
        if len(xy) < 2 or not np.isfinite(z).all():
            continue
        hw = spec.width / 2 + roadmesh.TUNNEL_SHOULDER
        a, b = xy[:-1], xy[1:]
        ab = b - a
        L2 = np.maximum((ab * ab).sum(1), 1e-12)
        t = np.clip(((p - a) * ab).sum(1) / L2, 0.0, 1.0)
        d = np.hypot(*(a + t[:, None] * ab - p).T)
        j = int(np.argmin(d))
        if d[j] <= hw:
            f = float(z[j] + t[j] * (z[j + 1] - z[j]))
            best = f if best is None else min(best, f)
    return best


def block_arch_raise(tunnels, cell_fn, frame, la, lo, z, box):
    """arch_raise for ground heights at the points (la, lo) of a block in the anchor-frame box (x0, y0, x1, y1),
    the tunnels from the map cells under it. z as it was when the cells are not at hand."""
    try:
        x0, y0, x1, y1 = box
        cla, clo, _ = frame.enu_to_geodetic(np.array([x0, x1]), np.array([y0, y1]), np.zeros(2))
        keys = mapcells.cells_for_bbox(float(cla.min()), float(clo.min()), float(cla.max()), float(clo.max()))
        cells = []
        for key in keys:
            try:
                cells.append(cell_fn(key))
            except Exception:
                cells.append(None)
        parts = tunnels.parts(cells, box)
        if not parts or not tunnels.heights:
            return z
        e, n, _ = frame.geodetic_to_enu(np.asarray(la, np.float64).ravel(), np.asarray(lo, np.float64).ravel(), 0.0)
        return arch_raise(parts, tunnels.heights, np.asarray(e), np.asarray(n),
                          np.asarray(z, np.float64).ravel(), getattr(tunnels, "mouths", None)).reshape(np.shape(z))
    except Exception as ex:         # no raise rather than no block (a network error quietly; a bug where it shows)
        (log.debug if isinstance(ex, OSError) else log.warning)("arch raise: %s: %s", type(ex).__name__, ex)
        return z


def block_tunnel_holes(tunnels, cell_fn, frame, keep, cx, cy, square):
    """tunnel_hole_mask for a terrain block being built (heightmap.generate's keep: lat, lon, u), its tunnels from
    the map cells under it (cell_fn(key) -> roads Features) through the anchor's TunnelProfiles."""
    lat, lon, u = keep["lat"], keep["lon"], keep["u"]
    keys = mapcells.cells_for_bbox(float(np.min(lat)), float(np.min(lon)), float(np.max(lat)), float(np.max(lon)))
    cells = []
    for k in keys:
        try:
            cells.append(cell_fn(k))
        except Exception as e:      # a cell not at hand (offline, a network error): no tunnels from it this time
            log.debug("tunnel holes: cell %s: %s: %s", k, type(e).__name__, e)
            cells.append(None)
    half = u.shape[0] * square / 2
    try:
        parts = tunnels.parts(cells, (cx - half, cy - half, cx + half, cy + half))
    except Exception as e:          # a chain's other cells not at hand (offline): no holes this time
        (log.debug if isinstance(e, OSError) else log.warning)("tunnel holes: %s: %s", type(e).__name__, e)
        return None
    if not parts:
        return None
    return tunnel_hole_mask(parts, u, cx, cy, square, getattr(tunnels, "heights", None),
                            getattr(tunnels, "mouths", None))


WALL_STEP = 2.0         # m between a retaining wall's points
WALL_PROBE_M = 1.0      # m beyond the cut's foot (grading.WALL_T_M) where the natural ground's height is read
WALL_TOP_M = 0.15       # m the wall stands over the ground behind it
WALL_FOOT_M = 0.3       # m it reaches under the road
WALL_FULL = 0.9         # the graded ground this share of the way up to the natural ground behind a wall: its top ends
                        # there ...
WALL_THICK_MIN_M = 1.0  # ... at least this deep and at most WALL_THICK_MAX_M
WALL_THICK_MAX_M = 3.5
WALL_OTHER_GAP_M = 0.3  # m a wall's top keeps from another road's carriageway
WALL_TAPER_M = 8.0      # m at each end of a wall over which it comes down ...
WALL_TAPER_LOW_M = 0.6  # ... to this over the road
WALL_SLAB_DZ = 0.8      # m the ground behind a wall may fall under its top before the top ends there ...
WALL_COPING_MIN_M = 0.5  # ... at least its coping
WALL_DIAG_M = 2.9       # the terrain lattice's diagonal (heightmap.SQUARE 2 m)
# a major road's cutting is walled in concrete (Munich's McGraw-Graben, Mapillary 2026-10-21), a smaller road's in stone
# (Central Park's transverses)
CONCRETE_WALL_CLASSES = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link"}
WALL_EVEN_N = 2         # a wall's top as deep as its deepest within this many WALL_STEPs either side
WALL_STEP_UP_M = 0.5     # m the graded ground climbs over the road where a wall's face goes (short of the core's edge)


WALL_KEEP_M = 1.2       # m: a wall goes on while the cut stays this deep (it starts at grading.WALL_CUT_M) ...
WALL_GAP_N = 3          # ... over gaps of up to this many WALL_STEPs, and none shorter than WALL_MIN_N (the cut's depth
WALL_MIN_N = 3          # wavered round 2 m: stubs of wall with gaps, the 97th Street Transverse, 2026-10-17)


def _wall_runs(cut):
    """Which samples along one side of a road get a wall, from the cut's depth there (m). Pure."""
    import grading
    cut = np.asarray(cut, np.float64)
    keep = cut > WALL_KEEP_M
    out = np.zeros(len(cut), bool)
    i = 0
    while i < len(cut):                     # runs over WALL_KEEP_M that reach WALL_CUT_M somewhere
        if not keep[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(cut) and keep[j + 1]:
            j += 1
        if (cut[i:j + 1] > grading.WALL_CUT_M).any():
            out[i:j + 1] = True
        i = j + 1
    i = 0
    while i < len(out):                     # runs shorter than WALL_MIN_N dropped ...
        if not out[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(out) and out[j + 1]:
            j += 1
        if j - i + 1 < WALL_MIN_N:
            out[i:j + 1] = False
        i = j + 1
    idx = np.flatnonzero(out)               # ... then gaps of up to WALL_GAP_N filled
    for a, b in zip(idx[:-1], idx[1:]):
        if 1 < b - a <= WALL_GAP_N + 1:
            out[a:b] = True
    return out


WALL_DECK_MARGIN_M = 3.0    # m beyond a bridge deck's edge where a road under it gets no retaining wall ...
WALL_DECK_OVER_M = 2.0      # ... a deck this far over the road (an overpass, not the road's own approach)


def _under_decks(xy, z, decks):
    """Which points xy (k, 2) at road heights z lie under a bridge deck (deck chunks: "n" points [x, y, z], "w" m):
    within its half width + WALL_DECK_MARGIN_M and the deck WALL_DECK_OVER_M or more over them. Pure."""
    out = np.zeros(len(xy), bool)
    for ch in decks or ():
        n = np.asarray(ch["n"], np.float64)
        if len(n) < 2:
            continue
        a, b = n[:-1, :2], n[1:, :2]
        d = _seg_dist(np.asarray(xy, np.float64), a, b)        # (k, m)
        j = d.argmin(axis=1)
        near = d[np.arange(len(xy)), j] <= float(ch.get("w") or 0) / 2 + WALL_DECK_MARGIN_M
        dz = np.minimum(n[j, 2], n[j + 1, 2]) - np.asarray(z, np.float64)
        out |= near & (dz >= WALL_DECK_OVER_M)
    return out


def _off_wall_tops(chunks, walls):
    """chunks with the ground roads' surfaces and lines cut away over the retaining walls' tops (a path on the bank
    above a cutting lay on the lattice's low strip under the top, in patches through it, Munich's McGraw-Graben).
    Decks, tunnels and filled polygons are left as they are."""
    if not walls:
        return chunks
    blockers = []
    for w in walls:
        th = np.asarray(w["thick"], np.float64)
        mid = np.asarray(w["base"], np.float64) - np.asarray(w["toward"], np.float64) * (th[:, None] / 2)
        blockers.append((mid, max(float(th.max()) / 2 - WALL_TOP_EDGE_M, 0.1)))
    lo = np.min([b[0].min(0) - b[1] for b in blockers], axis=0)
    hi = np.max([b[0].max(0) + b[1] for b in blockers], axis=0)
    out = []
    for c in chunks:
        n = c.get("n")
        if c.get("deck") or c.get("tunnel") or c.get("poly") is not None or not n or len(n) < 2:
            out.append(c)
            continue
        pts = np.asarray(n, np.float64)
        if (pts[:, 0].max() < lo[0] or pts[:, 0].min() > hi[0] or pts[:, 1].max() < lo[1]
                or pts[:, 1].min() > hi[1]):
            out.append(c)
            continue
        pieces = cut_outside(pts, blockers, 1.0)
        if len(pieces) == 1 and len(pieces[0]) >= len(pts) and np.allclose(pieces[0][[0, -1]], pts[[0, -1]]):
            out.append(c)
            continue
        for piece in pieces:
            out.append(dict(c, n=[[round(float(v), 3) for v in q] for q in simplify(piece)]))
    return out


WALL_TOP_EDGE_M = 0.3   # a road surface goes this far onto a wall's top at most (_off_wall_tops)


def _taper(xy, run):
    """Per point of a road's wall sample line xy (k, 2): its wall's height factor, 0 at the ends of each run of run (k,)
    rising (smoothstep) to 1 over WALL_TAPER_M (no full-height wall ending in a cliff, the user 2026-10-21). Pure."""
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(np.asarray(xy, np.float64)[:, :2], axis=0).T))]
    f = np.zeros(len(xy))
    i = 0
    while i < len(run):
        if not run[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(run) and run[j + 1]:
            j += 1
        d = np.minimum(s[i:j + 1] - s[i], s[j] - s[i:j + 1]) / WALL_TAPER_M
        d = np.clip(d, 0.0, 1.0)
        f[i:j + 1] = d * d * (3 - 2 * d)
        i = j + 1
    return f


def _on_carriageways(pts, others, gap):
    """Which points (k, 2) lie on a carriageway of others {fid: (centreline (m, 2), half width)} (+ gap). Pure."""
    import fast
    pts = np.ascontiguousarray(np.asarray(pts, np.float64))
    hit = np.zeros(len(pts), bool)
    if not len(pts):
        return hit
    lo, hi = pts.min(0), pts.max(0)
    for ln, h in others.values():
        if len(ln) < 2 or (ln.max(0) + h + gap < lo).any() or (ln.min(0) - h - gap > hi).any():
            continue
        fast.require(0, "points_near_segments")
        hit |= np.asarray(fast._ef.points_near_segments(pts, np.ascontiguousarray(ln[:-1]), np.ascontiguousarray(ln[1:]),
                                                        np.full(len(ln) - 1, h + gap)))
    return hit


def _clear_depth(base, out, depth, others, own):
    """Per point of a wall's face line base (k, 2): how far (up to depth (k,)) out from it along out (k, 2) stays off
    every other road's carriageway in others {fid: (centreline (m, 2), half width)} (less WALL_OTHER_GAP_M). Pure."""
    import fast
    steps = np.arange(0.5, float(np.max(depth)) + 0.51, 0.5)
    q = (base[:, None, :] + out[:, None, :] * steps[None, :, None]).reshape(-1, 2)
    hit = np.zeros(len(q), bool)
    lo, hi = q.min(0), q.max(0)
    for f2, (ln, h) in others.items():
        # (its own road too: a hairpin's other leg; the face stands past its own carriageway, so the stretch beside it
        # never hits)
        if len(ln) < 2:
            continue
        if (ln.max(0) + h < lo).any() or (ln.min(0) - h > hi).any():
            continue
        fast.require(0, "points_near_segments")
        hit |= np.asarray(fast._ef.points_near_segments(np.ascontiguousarray(q), np.ascontiguousarray(ln[:-1]),
                                                        np.ascontiguousarray(ln[1:]),
                                                        np.full(len(ln) - 1, h + WALL_OTHER_GAP_M)))
    hit = hit.reshape(len(base), len(steps))
    first = np.where(hit.any(axis=1), steps[np.argmax(hit, axis=1)] - 0.5, np.inf)
    return np.minimum(np.asarray(depth, np.float64), np.maximum(first, 0.0))


# Rock cliffs (the user, 2026-10-21, after the official maps: Italy's and West Coast USA's rural cuts are rock-face meshes
# sunk into the slope, overlapping): a rural road's retaining wall is drawn as Italy's rock-face pieces (trees.lua group
# "cliff") along its face instead of stone. (shape height m, its bottom under the pivot as a share of it, its depth in
# front of the pivot m per unit scale, its width m, its depth m)
CLIFF_SHAPES = ((2.9, 0.52, 1.5, 10.5, 2.9), (13.2, 0.17, 1.9, 12.9, 5.2))
CLIFF_TALL_M = 6.2      # a piece this tall or taller: the tall shape (trees.lua M.pick: nearer to it than the short one by log)
CLIFF_SINK_M = 1.0      # m the pieces reach under the road's level ...
CLIFF_OVER_M = 0.3      # ... and over the ground behind
CLIFF_SPACING = 0.55    # pieces along the face a share of their width apart (overlapping)
CLIFF_REACH_M = 8.0     # m behind the face a piece may reach at most (less where another road is)
CLIFF_EDGE_M = 0.5      # m off the carriageway's edge the pieces' fronts stand
CLIFF_ROAD_GAP_M = 0.3  # m a piece's footprint keeps from any carriageway


# Parapets (the Stelvio Pass, Mapillary 2026-10-21: a low dry-stone wall along the outside of every bend and drop): a
# rural road (not a motorway / trunk: guardrails there) whose ground falls PARAPET_DROP_M or more within PARAPET_PROBE_M of
# its edge gets a stone parapet just off its carriageway, not across a road joining it.
PARAPET_DROP_M = 2.0
PARAPET_PROBE_M = 3.0
PARAPET_H = 0.6
PARAPET_W = 0.45
PARAPET_OFF_M = 0.2
PARAPET_ROAD_GAP_M = 2.0
NO_PARAPET_CLASSES = {"motorway", "motorway_link", "trunk", "trunk_link", "service", "track"}
CLIFF_MIN_CUT_M = 6.0   # a rural cut this deep: rock faces (cliff_rows), when CLIFF_WALLS_ON; else a dry-stone wall
CLIFF_WALLS_ON = os.environ.get("BNE_CLIFF_WALLS", "off") == "on"


def _parapets(xy, z, out_n, rh, inside, steps, gs, others, own, decks):
    """The stone parapets along one side of a road (see PARAPET_*): wall dicts as retaining_walls' (roadmesh draws them
    with a back face). gs: the graded ground at steps (m off the centre line) per point. Pure but for decks."""
    import fast
    near = np.flatnonzero((steps >= rh + 0.5) & (steps <= rh + PARAPET_PROBE_M))
    if not len(near):
        return []
    drop = (z[:, None] - gs[:, near]).max(axis=1) > PARAPET_DROP_M
    base = xy + out_n * (rh + PARAPET_OFF_M)
    ok = drop & inside & ~_under_decks(base, z, decks) & ~_on_carriageways(base, others, 0.0)
    # not across a road joining it (its junctions), nor where another road runs beside
    for f2, (ln, h) in others.items():
        if f2 == own or len(ln) < 2 or not ok.any():
            continue
        fast.require(0, "points_near_segments")
        ok &= ~np.asarray(fast._ef.points_near_segments(np.ascontiguousarray(base), np.ascontiguousarray(ln[:-1]),
                                                        np.ascontiguousarray(ln[1:]),
                                                        np.full(len(ln) - 1, h + PARAPET_ROAD_GAP_M)))
    out, i = [], 0
    while i < len(ok):
        if not ok[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(ok) and ok[j + 1]:
            j += 1
        if j - i + 1 >= WALL_MIN_N:
            sl = slice(i, j + 1)
            # (its foot down to the ground behind it, which falls away: it stood in the air seen from below)
            back = np.flatnonzero((steps >= rh) & (steps <= rh + 2.5))
            g_back = gs[sl][:, back].min(axis=1) if len(back) else z[sl] - 1.0
            out.append({"base": base[sl], "top": z[sl] + PARAPET_H, "foot": np.minimum(z[sl] - 1.0, g_back - 0.3),
                        "toward": -out_n[sl],
                        "thick": np.full(j - i + 1, PARAPET_W), "concrete": False, "rock": False, "parapet": True,
                        "road_z": z[sl]})
        i = j + 1
    return out


def cliff_rows(walls, roads=()):
    """trees.lua rows [group, x, y, z, yaw, scale, height, rank] of rock-face pieces along the rural walls' faces
    (retaining_walls' "rock" walls). roads: build_tile's roads: no piece's footprint on a carriageway (shrunk until
    clear, else left out). Deterministic (hashed by position)."""
    import fast
    lines = [(np.asarray(pts, np.float64)[:, :2], spec.width / 2 + CLIFF_ROAD_GAP_M) for _, spec, pts, _ in roads
             if len(pts) >= 2]
    A = np.concatenate([ln[:-1] for ln, _ in lines]) if lines else np.zeros((0, 2))
    B = np.concatenate([ln[1:] for ln, _ in lines]) if lines else np.zeros((0, 2))
    H = np.concatenate([np.full(len(ln) - 1, h) for ln, h in lines]) if lines else np.zeros(0)

    def on_road(px, py, yaw, sc, width, front, depth):
        """Does the piece's footprint (its corners and edge midpoints) touch a carriageway?"""
        if not len(A):
            return False
        c, s_ = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
        ux, uy = np.array([c, s_]), np.array([-s_, c])          # its local x (along) and y (back from the road)
        pts = [np.array([px, py]) + ux * a * width * sc / 2 + uy * b * sc
               for a in (-1.0, -0.5, 0.0, 0.5, 1.0) for b in (-front * 0.6, depth - front)]
        fast.require(0, "points_near_segments")
        return bool(np.asarray(fast._ef.points_near_segments(np.ascontiguousarray(np.array(pts)), np.ascontiguousarray(A),
                                                             np.ascontiguousarray(B), np.ascontiguousarray(H))).any())
    rows = []
    for w in walls:
        # (their fronts just off the carriageway, not at the wall's face: the ground climbing in front of the face
        # showed as a mound under the rocks)
        b = np.asarray(w.get("edge", w["base"]), np.float64)
        if len(b) < 2:
            continue
        tw = np.asarray(w["toward"], np.float64)
        road_z, top = np.asarray(w["road_z"], np.float64), np.asarray(w["top"], np.float64)
        clear = np.asarray(w["clear"], np.float64)
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(b, axis=0).T))]
        at = 0.0
        while at <= s[-1]:
            i = min(int(np.searchsorted(s, at)), len(b) - 1)
            hh = float(np.interp(at, s, top) - np.interp(at, s, road_z)) + CLIFF_SINK_M + CLIFF_OVER_M
            jit = _hash01(round(float(b[i, 0]), 1), round(float(b[i, 1]), 1))
            hh *= 0.9 + 0.2 * jit
            sh, frac, front, width, depth = CLIFF_SHAPES[1 if hh >= CLIFF_TALL_M else 0]
            sc = hh / sh
            sc = min(sc, float(clear[i]) / depth)          # (not over another road)
            if sc < 0.2:
                at += 3.0
                continue
            t = tw[i]
            x, y = float(np.interp(at, s, b[:, 0])), float(np.interp(at, s, b[:, 1]))
            jit2 = _hash01(round(float(b[i, 0]), 1), round(float(b[i, 1]), 1), "z")
            yaw = math.degrees(math.atan2(t[0], -t[1])) + (jit - 0.5) * 30.0
            while sc >= 0.2:            # (shrunk until its footprint is off every carriageway, this road's included)
                px, py = x - t[0] * front * sc, y - t[1] * front * sc   # its front (-y, toward the road) on the face
                if not on_road(px, py, yaw, sc, width, front, depth):
                    break
                sc *= 0.8
            if sc < 0.2:
                at += 3.0
                continue
            hh = sc * sh
            zb = float(np.interp(at, s, road_z)) - CLIFF_SINK_M - jit2 * 0.8 * sc
            rows.append(["cliff", round(px, 2), round(py, 2), round(zb + frac * hh, 2), round(yaw % 360.0, 1), 1.0,
                         round(hh, 2), round(jit, 3)])
            at += max(CLIFF_SPACING * width * sc, 1.5)
    return rows


def _hash01(*key):
    import hashlib
    h = hashlib.blake2b(repr(key).encode(), digest_size=8).digest()
    return int.from_bytes(h, "little") / 2.0 ** 64


def retaining_walls(frame, roads, elev, exact, decks=()):
    """Stone retaining walls along the ground roads cut deeper than grading.WALL_CUT_M into the ground (the grading
    leaves the ground at its natural height right behind them): [dict(base (k, 2) the wall's face line, top (k,),
    foot (k,), toward (k, 2) unit vectors to the road)] of the walls in the tile box `exact`. The edge of the road's
    flat core (as grading plans it), from the road level up to the ground behind; none where another road comes in
    (its ground is graded to the road), nor under a bridge deck (decks: the deck chunks near the tile; the ground beside
    a motorway under an overpass is its abutment's fill, and the wall rose through the deck, the user's report on the
    Bruce Highway, 2026-10-21). Central Park's 86th Street Transverse between its stone walls (the user's photos,
    2026-10-17). Pure given elev."""
    import grading
    import roadmesh
    x0, y0, x1, y1 = exact
    pts_all, info = [], []
    for fid, spec, pts, _ in roads:
        if spec.kind not in BUILT or len(pts) < 2:
            continue
        xy, s = grading.resample(np.asarray(pts[:, :2], np.float64), WALL_STEP)
        if len(xy) < 2:
            continue
        seg = np.hypot(*np.diff(pts[:, :2], axis=0).T)
        z = np.interp(s, np.r_[0.0, np.cumsum(seg)], pts[:, 2])
        inside = (xy[:, 0] >= x0 - 2) & (xy[:, 0] < x1 + 2) & (xy[:, 1] >= y0 - 2) & (xy[:, 1] < y1 + 2)
        if not inside.any():
            continue
        d = np.gradient(xy, axis=0)
        d /= np.maximum(np.hypot(*d.T), 1e-9)[:, None]
        nl = np.stack([-d[:, 1], d[:, 0]], axis=1)
        width = max(spec.width, carriageway(spec.cls, spec.kind, True, None, spec.oneway))
        core = width / 2 + grading.shoulder(spec)
        # (the grading's sharp cuttings end the core sooner; its flat strip behind a wall reaches past the core)
        steps = np.arange(width / 2 + 0.5, core + grading.WALL_FLAT_M + 1.51, 1.0)
        for side in (1.0, -1.0):
            probe = xy + nl * side * (core + grading.WALL_FLAT_M + grading.WALL_T_M + WALL_PROBE_M)
            pts_all.append(probe)
            for o in steps:
                pts_all.append(xy + nl * side * o)
            info.append((xy, z, nl * side, core, inside, width / 2, steps, spec.cls in CONCRETE_WALL_CLASSES,
                         fid, not spec.urban, not spec.urban and spec.cls not in NO_PARAPET_CLASSES))
    if not pts_all:
        return []
    P = np.concatenate(pts_all)
    la, lo, _ = frame.enu_to_geodetic(P[:, 0], P[:, 1], np.zeros(len(P)))
    g = np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2], np.float64)
    out, k = [], 0
    others = {fid: (np.asarray(pts, np.float64)[:, :2], spec.width / 2) for fid, spec, pts, _ in roads if len(pts) >= 2}
    bend_of = {}
    for xy, z, out_n, core, inside, rh, steps, concrete, fid, rural, parapets in info:
        key = (id(xy), len(xy))
        if key not in bend_of:          # (a tight bend: no wall into it, grading.bend_zone)
            bend_of[key] = grading.bend_zone(xy)
        bends = bend_of[key]
        gg = g[k:k + len(xy)]
        k += len(xy)
        face = np.full(len(xy), core + 0.1)
        thick = np.full(len(xy), roadmesh.WALL_THICK_M)
        top_g = gg
        if len(steps):              # the wall where the graded ground starts to climb (a sharp cutting: before the core's edge)
            gs = g[k:k + len(xy) * len(steps)].reshape(len(steps), len(xy)).T
            k += len(xy) * len(steps)
            up = gs - z[:, None] > WALL_STEP_UP_M
            first = np.argmax(up, axis=1)
            # (the grading's flat strip in front of the climb: the terrain's 2 m lattice draws a step at an angle to it
            # as teeth reaching up to a grid diagonal out, under the wall's top; its face in front of them)
            face = np.where(up.any(axis=1), np.maximum(steps[first] - 1.0 - grading.WALL_FLAT_M, rh + 0.1), face)
            # its top back to where the graded ground is up again (no trough behind it: a street above drove into one)
            full = gs >= (z + WALL_FULL * (gg - z))[:, None]
            back = np.argmax(full, axis=1)
            # (and a grid diagonal more: the terrain's lattice reaches the top that much later between its points)
            thick = np.where(full.any(axis=1), np.clip(steps[back] - face + WALL_DIAG_M, WALL_THICK_MIN_M,
                                                       WALL_THICK_MAX_M), thick)
            # (its top over the highest graded ground under it, not the one probe's: the bank poked through it in
            # patches where that probe sat in a dip)
            under = steps[None, :] >= face[:, None]
            top_g = np.maximum(gg, np.where(under, gs, -np.inf).max(axis=1))
            # (and never past where the ground behind falls away again: between a hairpin's legs the bank tops out
            # and drops to the other leg, and the top hung out over that slope, its underside seen from the road
            # above, the Stelvio Pass 2026-10-21)
            low = (gs < (top_g - WALL_SLAB_DZ)[:, None]) & (steps[None, :] > (face + 1.0)[:, None])
            reach = np.where(low.any(axis=1), steps[np.argmax(low, axis=1)] - face, np.inf)
            thick = np.minimum(thick, np.maximum(reach - 0.5, WALL_COPING_MIN_M))
        if parapets and len(steps):
            out.extend(_parapets(xy, z, out_n, rh, inside, steps, gs, others, fid, decks))
        full = _wall_runs(gg - z) & ~_under_decks(xy + out_n * core, z, decks) & ~bends
        # (no face standing on a carriageway: a hairpin's other leg, the same road, the Stelvio Pass 2026-10-21)
        full &= ~_on_carriageways(xy + out_n * face[:, None], others, 0.0)
        # (its ends lowered over WALL_TAPER_M toward the road, the whole wall's ends, not a tile edge's)
        taper = _taper(xy, full)
        cut = full & inside
        i = 0
        while i < len(cut):
            if not cut[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(cut) and cut[j + 1]:
                j += 1
            if j > i:
                sl = slice(i, j + 1)
                # (its top's depth evened out along it: the 1 m probe steps drew a jagged back edge)
                th = thick[sl]
                th = np.maximum.reduce([np.r_[th[:1].repeat(m), th[:len(th) - m]] for m in range(WALL_EVEN_N + 1)]
                                       + [np.r_[th[m:], th[-1:].repeat(m)] for m in range(1, WALL_EVEN_N + 1)])
                base = xy[sl] + out_n[sl] * face[sl][:, None]
                # (never over another road: a hairpin's wall top covered the road above, the Stelvio Pass, the user's
                # report 2026-10-21)
                th = np.minimum(th, _clear_depth(base, out_n[sl], th, others, fid))
                tp = np.minimum(top_g[sl], z[sl] + grading.WALL_MAX_H_M) + WALL_TOP_M      # (no taller than that)
                tp = z[sl] + WALL_TAPER_LOW_M + (tp - z[sl] - WALL_TAPER_LOW_M).clip(0) * taper[sl]
                # (rock faces only as an experiment, BNE_CLIFF_WALLS=on, for tall rural cuts: the Stelvio's hairpins are
                # walled in dry stone, tall cuts too, Mapillary 2026-10-21; the user chose stone walls for passes)
                tall = CLIFF_WALLS_ON and float(np.max(top_g[sl] - z[sl])) > CLIFF_MIN_CUT_M
                out.append({"base": base, "top": np.maximum(tp, z[sl] + WALL_TAPER_LOW_M), "foot": z[sl] - WALL_FOOT_M,
                            "toward": -out_n[sl], "thick": th, "concrete": concrete, "rock": rural and tall,
                            "road_z": z[sl], "clear": _clear_depth(base, out_n[sl], np.full(len(base), CLIFF_REACH_M),
                                                                   others, fid),
                            "edge": xy[sl] + out_n[sl] * (rh + CLIFF_EDGE_M)})
            i = j + 1
    return out


TWIN_SHARE = 0.8        # a tunnel part beside another for this share of its length (their tubes overlapping) ...
TWIN_DZ_M = 2.0         # ... with its floor within this of the other's: twin tubes, one tube round both (GitHub #84:
                        # the Route des Tamarins' two carriageways 9.5 m apart on Réunion, each an 11 m road in its own
                        # tube, the tubes through each other's walls and portals)
TWIN_STEP_M = 10.0


def _line_near(xy, other):
    """Per point of xy (k, 2): (distance to the polyline other (m, 2), the nearest point on it, its index t along it
    as a float segment index). Pure."""
    a, b = other[:-1], other[1:]
    d = b - a
    L2 = np.maximum((d * d).sum(1), 1e-12)
    out_d, out_q = np.empty(len(xy)), np.empty((len(xy), 2))
    for i, p in enumerate(xy):
        t = np.clip(((p - a) * d).sum(1) / L2, 0.0, 1.0)
        q = a + t[:, None] * d
        dd = np.hypot(*(q - p).T)
        j = int(np.argmin(dd))
        out_d[i], out_q[i] = dd[j], q[j]
    return out_d, out_q


def twin_tunnels(parts):
    """Pairs of tunnel parts ([(fid, spec, xy, z, lon)]) side by side: [(i, j, midline (k, 3), width)] with i < j their
    indices, the midline along part i (each point halfway to j's line, the floors' mean) and the width of a road
    surface covering both carriageways (the tube round both: roadmesh.tunnel). Each part in one pair at most. Pure."""
    import roadmesh
    taken, out = set(), []
    for i, (fa, sa, xa, za, _) in enumerate(parts):
        if i in taken or len(xa) < 2:
            continue
        xa = np.asarray(xa, np.float64)
        sj = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xa, axis=0).T))]
        if sj[-1] < 1.0:
            continue
        ss = np.linspace(0.0, sj[-1], max(2, int(sj[-1] / TWIN_STEP_M) + 1))
        P = np.c_[np.interp(ss, sj, xa[:, 0]), np.interp(ss, sj, xa[:, 1])]
        Z = np.interp(ss, sj, np.asarray(za, np.float64))
        for j in range(i + 1, len(parts)):
            fb, sb, xb, zb, _ = parts[j]
            if j in taken or len(xb) < 2:
                continue
            xb = np.asarray(xb, np.float64)
            reach = (sa.width + sb.width) / 2 + 2 * roadmesh.TUNNEL_SHOULDER + 1.0      # their tubes would overlap
            d, q = _line_near(P, xb)
            near = d < reach
            if near.mean() < TWIN_SHARE:
                continue
            tb = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xb, axis=0).T))]
            _, qb = _line_near(q, xb)           # (q on xb: its floor by the arc to it)
            sb_at = np.array([tb[k] + np.hypot(*(qb[n] - xb[k])) for n, k in enumerate(
                [int(np.argmin(np.hypot(*(xb[:-1] - p).T) + np.hypot(*(xb[1:] - p).T))) for p in qb])])
            zq = np.interp(sb_at, tb, np.asarray(zb, np.float64))
            if np.abs(Z[near] - zq[near]).max() > TWIN_DZ_M:
                continue
            mid = np.c_[(P + q) / 2, (Z + zq) / 2]
            k0, k1 = int(np.flatnonzero(near)[0]), int(np.flatnonzero(near)[-1])     # (where they run side by side)
            width = float(np.median(d[near])) + (sa.width + sb.width) / 2
            out.append((i, j, mid[k0:k1 + 1], width))
            taken.update((i, j))
            break
    return out


def _tunnel_chunks(tunnels, cells, exact):
    """The chunks of the tunnels crossing the tile box `exact`: the road surface ("deck", "tunnel": the tube is
    built around it, roadmesh.tunnel) and its lines, every one "fixed" at the floor's heights; a surface chunk
    ending at its part's end carries "portal": [start is one, end is one]. Twin tunnels (twin_tunnels): their surfaces
    are floors without a tube ("no_tube"), and one "tube_only" chunk along their midline (not drawn as a road) carries
    the tube round both, with the first's portals."""
    out = []
    if getattr(tunnels, "open_parts", None) is not None:
        for fid, spec, xy, z, lon in tunnels.open_parts(cells, exact):
            if not np.isfinite(z).all():
                continue
            pts = np.c_[xy, z]
            for look, w, line in [(LOOK[spec.kind], spec.width, pts)] + [
                    (lk, w2, ln) for lk, w2, ln in extras(spec, pts, lon)
                    if lk["m"] != SIDEWALK["m"] and lk["m"] != GUTTER["m"]]:
                for piece in clip_polyline(line, *exact):
                    for c in split(densify(simplify(piece))):
                        if len(c) >= 2:
                            # on the ground as any road (the grading lays the ground at its floor, Grader._open_tunnels:
                            # at the floor's own heights the ground graded from the grader's profile covered it
                            # outside 97th Street's arch, 2026-10-17)
                            out.append(_chunk(look, w, c))
    parts = [p for p in tunnels.parts(cells, exact) if np.isfinite(p[3]).all()]
    twins = twin_tunnels(parts)
    twinned = {k for i, j, _, _ in twins for k in (i, j)}
    jobs = []                                   # (fid, its ends, [(look, width, line)])
    for i, j, mid, width in twins:              # the tube round both, with the first's portals
        if len(mid) >= 2:
            jobs.append((parts[i][0], (mid[0, :2], mid[-1, :2]), [({**TUNNEL, "tunnel": True, "tube_only": True}, width, mid)]))
    for n, (fid, spec, xy, z, lon) in enumerate(parts):
        pts = np.c_[xy, z]
        lines = [({**TUNNEL, "deck": True, "tunnel": True, "no_tube": n in twinned}, spec.width, pts)]
        for look, w, line in extras(spec, pts, lon, bridge=True):
            if look["m"] != SIDEWALK["m"] and look["m"] != GUTTER["m"]:
                lines.append((look, w, line))
        jobs.append((fid, (pts[0, :2], pts[-1, :2]), lines))
    for fid, ends, lines in jobs:
        for look, w, line in lines:
            for piece in clip_polyline(line, *exact):
                for c in split(densify(simplify(piece, ztol=SIMPLIFY_Z_M))):
                    if len(c) >= 2:
                        ch = _chunk(look, w, c)
                        ch["fixed"] = True
                        if look.get("tunnel"):
                            ch["tunnel"] = True
                            ch["deck"] = not look.get("tube_only")      # (a twin's tube: no floor, no road surface)
                            for flag in ("no_tube", "tube_only"):
                                if look.get(flag):
                                    ch[flag] = True
                            if getattr(tunnels, "height", None) is not None:
                                ch["tunnel_h"] = tunnels.height(fid)
                            if getattr(tunnels, "portal_ups", None) is not None:
                                u0, u1 = tunnels.portal_ups(fid)
                                at = lambda p: u0 if np.hypot(*(p - ends[0])) < 0.5 else u1    # noqa: E731
                                ch["portal_up"] = [at(c[0, :2]), at(c[-1, :2])]
                            if getattr(tunnels, "cap_m", None) is not None:
                                c0, c1 = tunnels.cap_m(fid)
                                if c0 is not None or c1 is not None:
                                    atc = lambda p: c0 if np.hypot(*(p - ends[0])) < 0.5 else c1    # noqa: E731
                                    ch["cap_m"] = [atc(c[0, :2]), atc(c[-1, :2])]
                            m0, m1 = tunnels.mouth(fid) if getattr(tunnels, "mouth", None) is not None else (True, True)
                            isp = lambda p: bool((np.hypot(*(p - ends[0])) < 0.5 and m0) or      # noqa: E731
                                                 (np.hypot(*(p - ends[1])) < 0.5 and m1))
                            ch["portal"] = [isp(c[0, :2]), isp(c[-1, :2])]
                        out.append(ch)
    return out


def _bridge_chunks(frame, bseen, elev, exact, bridges=None, cells=None, left_hand=False):
    """The chunks of the bridges crossing the tile box `exact` (deck surfaces flagged "deck", their
    centre / edge lines; every bridge chunk "fixed": its z is the deck's, not the ground's). Deck
    heights: from `bridges` (BridgeProfiles: whole chains, shared by every tile), else from the
    chains found among the given cells (deck_heights; tests)."""
    if bridges is not None:
        return _deck_chunks([(fid, spec, np.c_[xy, z], lon) for fid, spec, xy, z, lon in bridges.parts(cells, exact)
                             if np.isfinite(z).all()], exact, frame, left_hand)
    if not bseen:
        return []
    parts = []                                  # (fid, spec, xy (k, 2), lon)
    for fid in sorted(bseen):
        feats, i, spec = bseen[fid]
        for ring in bridge_parts(feats, i):
            if len(ring) >= 2:
                e, n = mapgeom.enu_xy(feats, frame, ring)
                parts.append((fid, spec, densify(np.stack([e, n], axis=1), DECK_STEP),
                              float(np.mean(ring[:, 0]))))
    if not parts:
        return []

    def ground(xy):
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        h = np.asarray(elev(la, lo), np.float64)
        return frame.geodetic_to_enu(la, lo, h)[2]
    zs = deck_heights([p[2] for p in parts], ground)
    allxy = np.concatenate([p[2] for p in parts])
    gz = ground(allxy)                          # one call: the ground under every deck point
    decks, k = [], 0
    for (fid, spec, xy, lon), z in zip(parts, zs):
        g = gz[k:k + len(xy)]
        k += len(xy)
        if not np.isfinite(z).all():
            continue
        # never below the ground under it (2026-10-11: abutments on low spots buried decks), then
        # smoothed along it like a road profile (the ends keep their heights: the abutments)
        g = g + DECK_CLEAR
        z = np.maximum(z, g)
        if len(z) > 3:          # (the ends' trend kept: smoothed then pinned, the deck bent up beside an abutment)
            import grading
            z = np.maximum(_smooth_pinned(z, DECK_STEP, grading.SIGMA_M), g)
        decks.append((fid, spec, np.c_[xy, z], lon))
    return _deck_chunks(decks, exact, frame, left_hand)


DECK_CONTEXT_M = 24.0   # m around a tile its bridges' decks are passed as context (roadmesh.deck: whether an edge has
                        # another deck beside it; a parapet stood across the lanes at a tile seam, the deck beside it
                        # in the next tile, Queensboro Bridge 2026-10-17)


def _distance_along(line, point):
    """Arc-length position of a point projected onto a bridge's complete path."""
    xy = np.asarray(line, np.float64)[:, :2]
    ab = np.diff(xy, axis=0)
    lengths = np.hypot(ab[:, 0], ab[:, 1])
    valid = lengths > 1e-9
    if not valid.any():
        return 0.0
    t = np.zeros(len(ab), np.float64)
    t[valid] = np.clip(((np.asarray(point) - xy[:-1])[valid] * ab[valid]).sum(1) /
                       (lengths[valid] ** 2), 0.0, 1.0)
    nearest = xy[:-1] + t[:, None] * ab
    distance = np.hypot(*(nearest - point).T)
    distance[~valid] = np.inf
    i = int(np.argmin(distance))
    return float(lengths[:i].sum() + t[i] * lengths[i])


def _deck_chunks(decks, exact, frame=None, left_hand=False):
    """Deck + line chunks of bridge parts [(fid, spec, pts (k, 3), lon)] clipped to the box `exact`, and their decks
    within DECK_CONTEXT_M of it as "ctx" chunks (not drawn: build_tile's caller hands them to roadmesh.deck)."""
    out = []
    x0, y0, x1, y1 = exact
    wide = (x0 - DECK_CONTEXT_M, y0 - DECK_CONTEXT_M, x1 + DECK_CONTEXT_M, y1 + DECK_CONTEXT_M)
    for fid, spec, pts, lon in decks:
        for piece in clip_polyline(pts, *wide):
            for c in split(densify(simplify(piece, ztol=SIMPLIFY_Z_M))):
                if len(c) >= 2:
                    ch = _chunk({**DECK, "deck": True}, spec.width, c)
                    ch.update(fixed=True, deck=True, fid=fid, ctx=True)
                    out.append(ch)
    for fid, spec, pts, lon in decks:
        deck_look = {**DECK, "deck": True, "uv_shift": -0.35 * (spec.dir or 1)}
        lines = [(deck_look, spec.width, pts)]
        for look, w, line in extras(spec, pts, lon, left_hand, frame, bridge=True):
            if look["m"] != SIDEWALK["m"] and look["m"] != GUTTER["m"]:
                lines.append((look, w, line))
        for look, w, line in lines:
            for piece in clip_polyline(line, *exact):
                for c in split(densify(simplify(piece, ztol=SIMPLIFY_Z_M))):
                    if len(c) >= 2:
                        ch = _chunk(look, w, c, reference=line)
                        ch["fixed"] = True
                        if look.get("deck"):
                            ch["uv_start"] = _distance_along(pts, c[0, :2])
                            ch["deck"] = True
                            ch["fid"] = fid             # (roadmesh.deck: no parapet between parts side by side)
                        out.append(ch)
    return out


def _on_box(p, box, eps=1e-6):
    x0, y0, x1, y1 = box
    return abs(p[0] - x0) < eps or abs(p[0] - x1) < eps or abs(p[1] - y0) < eps or abs(p[1] - y1) < eps


def drains(run, side, caps, walk_w=None):
    """The storm drain chunks along a sidewalk run (k, 3) on `side` of its road: DRAIN_IN m before its
    end if that is a real end (caps: (start, end) real) and every DRAIN_EVERY m along it. Pure."""
    import sidewalks
    curb, out = sidewalks.run_geometry(run, side, walk_w or SIDEWALK["w"])
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(curb[:, :2], axis=0).T))]
    L = s[-1]
    if L < 2 * DRAIN_IN:
        return []
    at = []
    if caps[1]:
        at.append(L - DRAIN_IN)
    if L > DRAIN_EVERY * 1.5:
        at += list(np.arange(DRAIN_EVERY, L - DRAIN_EVERY / 2, DRAIN_EVERY))
    res = []
    for a in sorted(at):
        pts = []
        for d in (a - DRAIN["len"] / 2, a + DRAIN["len"] / 2):
            x = np.interp(d, s, curb[:, 0])
            y = np.interp(d, s, curb[:, 1])
            z = np.interp(d, s, curb[:, 2])
            k = min(int(np.searchsorted(s, d)), len(s) - 1)
            o = out[k] / max(np.hypot(*out[k]), 1e-9)
            pts.append([x - o[0] * (GUTTER["w"] / 2), y - o[1] * (GUTTER["w"] / 2), z])
        res.append(_chunk(DRAIN, DRAIN["w"], np.array(pts)))
    return res


def build_tile(frame, tx, ty, cells, elev, urban_fn=None, walks=None, bridges=None, widths=None, props=None,
               lines=None, wires=None, town=None, park=None, left_hand=False, structures=(), mph=True, tunnels=None,
               walls=None, deck_context=None, osm_objects=(), exit_signs=None):
    """The chunks of one tile. cells: the mapgeom.Features of the "roads" layer of every cell touching
    the tile; elev(lat, lon) -> metres (the DEM); urban_fn: as road_specs. Features in several cells are
    taken once (by id). The carriageways, then each road's sidewalks / gutters / markings (extras), cut
    where another road's carriageway crosses them. walks: a list that gets the sidewalk runs (dicts
    pts / side / caps, clipped exactly to the tile, caps: whether each end is a real end) for the
    sidewalk mesh (sidewalks.py) and storm drains among the chunks; None: sidewalks as decals.
    bridges: a BridgeProfiles (deck heights of whole chains, shared by every tile); None: from the
    chains among these cells. Pure given its inputs (sorted by feature id)."""
    x0, y0, x1, y1 = tile_bbox(tx, ty)
    box = (x0 - OVERLAP, y0 - OVERLAP, x1 + OVERLAP, y1 + OVERLAP)
    near = (x0 - NEAR_M, y0 - NEAR_M, x1 + NEAR_M, y1 + NEAR_M)
    seen, bseen = {}, {}
    for feats in cells:
        if feats is None or len(feats) == 0:
            continue
        specs = road_specs(feats, urban_fn, widths, left_hand)
        bspecs = bridge_specs(feats)
        for i in range(len(feats)):
            if bspecs[i] is not None and str(feats.ids[i]) not in bseen:
                bseen[str(feats.ids[i])] = (feats, i, bspecs[i])
            if specs[i] is None or str(feats.ids[i]) in seen:
                continue
            seen[str(feats.ids[i])] = (feats, i, specs[i])
    rings = []                                  # (fid, spec, lon/lat ring) near the tile (xy at h = 0)
    for fid in sorted(seen):
        feats, i, spec = seen[fid]
        fb = feats.boxes(frame)[i]          # (the whole feature far from the tile: none of its parts is near)
        if fb[2] < near[0] or fb[0] > near[2] or fb[3] < near[1] or fb[1] > near[3]:
            continue
        for ring in ground_parts(feats, i):
            e, n = mapgeom.enu_xy(feats, frame, ring)
            if (e.max() < near[0] or e.min() > near[2] or n.max() < near[1] or n.min() > near[3]):
                continue
            rings.append((fid, spec, ring))
    roads = []                                  # (fid, spec, centreline (k, 3), lon) near the tile
    if rings:                                   # one elevation call for every ring (the grader's cost
        lon = np.concatenate([r[:, 0] for _, _, r in rings])     # is per call: 2026-10-11 profile,
        lat = np.concatenate([r[:, 1] for _, _, r in rings])     # 1,240 calls took 7 of a tile's 8 s)
        h = np.asarray(elev(lat, lon), np.float64)
        # Horizontal geometry must use the same zero-height projection as the shared
        # cross-sections and cached ramp layout. Projecting XY at the sampled elevation
        # moved distant nodes away from their paint overrides and shared join normals.
        e, n, _ = frame.geodetic_to_enu(lat, lon, 0.)
        u = frame.geodetic_to_enu(lat, lon, h)[2]
        k = 0
        for fid, spec, ring in rings:
            m = len(ring)
            roads.append((fid, spec, np.stack([e[k:k + m], n[k:k + m], u[k:k + m]], axis=1),
                          float(np.mean(ring[:, 0]))))
            k += m
    sections = _join_sections(frame, cells)
    ramp_edges, merge_zones, ramp_mains, auxiliary = projected_ramp_layout(frame, cells, left_hand)
    import roadterminals
    terminal_rows=roadterminals.context(frame,cells,roads,(x0,y0,x1,y1),urban_fn,widths,left_hand)
    terminals=roadterminals.find(frame,terminal_rows,roadterminals.tags_from_cells(cells))
    import roadlanes
    main_context = {}
    def main_pavements(fid):
        if fid not in main_context:
            main_context[fid] = [
                (line, width/2) for key in sorted(ramp_mains.get(fid, ())) if key in seen
                for ref in sections['_uv'].get(key, ())
                for line, width in [roadlanes.pavement(
                    seen[key][2], _with_cross(ref.points, sections), frame, left_hand)]]
        return main_context[fid]
    chunks = []
    for fid, spec, pts, _ in roads:
        pts = _with_cross(pts, sections)
        look = LOOK[spec.kind]
        if (spec.lanes is not None or spec.link) and not look.get("strip"):
            import roadlanes
            line, width = roadlanes.pavement(spec, pts, frame, left_hand)
            if spec.link:
                line=_outside_mainline(line,width,main_pavements(fid))
            for piece in clip_polyline(line, *box):
                for c in split(densify(simplify(piece, CURVE_SIMPLIFY_M, heading=CURVE_HEADING_DEG))):
                    if len(c) >= 2:
                        chunks.append(_chunk(look, width, c, reference=_uv_reference(sections,fid,pts)))
            continue
        lanes = [(0.0, spec.width)]
        if look.get("strip"):                   # parallel strips across the width (see LOOK)
            n = max(1, int(round(spec.width / look["strip"])))
            sw = spec.width / n
            lanes = [((k + 0.5) * sw - spec.width / 2, sw) for k in range(n)]
        for off, lw in lanes:
            line = offset_polyline(pts, off + pavement_offset(spec, left_hand))
            for piece in clip_polyline(line, *box):
                for c in split(densify(simplify(piece, CURVE_SIMPLIFY_M, heading=CURVE_HEADING_DEG))):
                    if len(c) >= 2:
                        chunks.append(_chunk(look, lw, c, reference=_uv_reference(sections,fid,pts)))
    decks_near = []                             # (retaining_walls: none under a bridge)
    for ch in _bridge_chunks(frame, bseen, elev, (x0, y0, x1, y1), bridges, cells, left_hand):
        if ch.get("deck"):
            decks_near.append(ch)
        if not ch.get("ctx"):
            chunks.append(ch)
        elif deck_context is not None:          # the decks around the tile (roadmesh.deck's parapet test)
            deck_context.append(ch)
    if tunnels is not None:                     # TunnelProfiles: road surfaces and lines through tunnels
        tch = _tunnel_chunks(tunnels, cells, (x0, y0, x1, y1))
        chunks.extend(tch)
        import roadmesh
        # (retaining walls: none in a tube either, its vault over them; between twin tunnels' carriageways one stood in
        # their shared mouth, GitHub #84)
        for c in tch:
            if c.get("tunnel") and not c.get("no_tube"):
                n = np.array(c["n"], np.float64)
                n[:, 2] += float(c.get("tunnel_h") or roadmesh.TUNNEL_H)
                decks_near.append({"n": n, "w": float(c["w"]) + 2 * roadmesh.TUNNEL_SHOULDER})
    exact = (x0, y0, x1, y1)
    if walls is not None:                       # retaining walls where a road is cut into the ground (roadmesh.tunnel)
        tile_walls = retaining_walls(frame, roads, elev, exact, decks=decks_near)
        if props is None:                       # (no props here: stone)
            for w in tile_walls:
                w["rock"] = False
        walls.extend(w for w in tile_walls if not w["rock"])
        if props is not None:                   # a rural cut: rock-face pieces instead (cliff_rows)
            props.extend(cliff_rows([w for w in tile_walls if w["rock"]], roads))
        chunks = _off_wall_tops(chunks, [w for w in tile_walls if not w["rock"]])
    link_ids = {fid for fid,(feats,i,_) in seen.items() if is_link(feats,i)}
    continuous, merges = _road_connections(roads,link_ids)
    for band in auxiliary:
        p, outer = band['pts'].copy(), band['outer'].copy()
        if p[:, 0].max() < box[0] or p[:, 0].min() > box[2] or p[:, 1].max() < box[1] or p[:, 1].min() > box[3]:
            continue
        both = np.vstack([p, outer])
        la, lo, _ = frame.enu_to_geodetic(both[:, 0], both[:, 1], both[:, 2])
        u = frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2]
        p[:, 2], outer[:, 2] = u[:len(p)], u[len(p):]
        cross = offset_polyline(p, 1.)[:, :2]-p[:, :2]
        width = float(max(band['widths']))
        cross *= (band['widths']/width)[:, None]
        for sl,surface,kind in auxiliary_sections(band):
            source=np.c_[p,cross][sl]
            ownership=[(band['fid'],surface,kind,source)]
            if sl.start==0:
                # The approach can cross several source features. Use each underlying
                # mainline's own curve/material, rather than extending one short tangent
                # across the entire acceleration/deceleration lane.
                candidates=[(key,sp,ref) for key in sorted(ramp_mains.get(band['fid'],()))
                            if key in seen for sp in [seen[key][2]]
                            for ref in sections.get('_uv',{}).get(key,())]
                if candidates and len(source)>1:
                    mid=(source[:-1,:2]+source[1:,:2])/2
                    distance=np.array([_seg_dist(mid,ref.points[:-1,:2],ref.points[1:,:2]).min(1)
                                       for _,_,ref in candidates])
                    owners=distance.argmin(0)
                    cuts=np.r_[0,np.flatnonzero(owners[1:]!=owners[:-1])+1,len(owners)]
                    ownership=[(candidates[owners[a]][0],candidates[owners[a]][1].surface,
                                candidates[owners[a]][1].kind,source[a:b+1]) for a,b in zip(cuts[:-1],cuts[1:])]
            for owner,appearance,road_kind,run in ownership:
                line=_outside_mainline(run,width,main_pavements(band['fid']))
                for piece in clip_polyline(line, *box):
                    for c in split(densify(simplify(piece, CURVE_SIMPLIFY_M, heading=CURVE_HEADING_DEG))):
                        chunks.append(_chunk(LOOK[road_kind], width, c,
                                             reference=_uv_reference(sections,owner,run)))
        for piece in clip_polyline(outer, *box):
            for c in split(densify(simplify(piece, CURVE_SIMPLIFY_M, heading=CURVE_HEADING_DEG))):
                chunks.append(_chunk(EDGE_LINE, EDGE_LINE['w'], c))
        # Continue the dividers between a connector's own lanes across its full
        # auxiliary approach; the mainline/ramp separator keeps its short dots.
        for lane_offset in np.arange(MOTORWAY_LANE_M,band['lane']-.1,MOTORWAY_LANE_M):
            guide,outside=band['guide'],band['outer']
            divider=guide+(outside-guide)*(lane_offset/band['lane'])
            available=np.linalg.norm(outside[:,:2]-guide[:,:2],axis=1)-lane_offset-.1
            for run in roadlanes._runs(divider,available):
                for piece in clip_polyline(run,*box):
                    chunks.append(_chunk({**LANE_LINE,'aux_divider':True},LANE_LINE['w'],piece))
    # Both carriageways must remain in the context even when only one is near this tile.
    median_corners, median_cuts, median_pairs = median_layout(frame, cells, left_hand, urban_fn, widths)
    corners, jcuts, crosswalks, jramps, jlines, jinternal = \
        junctions.find(roads, has_sidewalks, None, PARKING_M,
                       lambda sp: sp.urban and sp.cls in PARKING_CLASSES and sp.kind in BUILT and not sp.oneway
                       and sp.width >= STYLE.get(sp.cls, STYLE["unknown"])[0] + 2 * PARKING_M - 0.1,
                       paved=lambda fid,sp: sp.kind in BUILT and (sp.cls != "motorway" or fid in link_ids),
                       join_filter=lambda a, b: frozenset((a, b)) not in median_pairs)
    corners.extend(median_corners)
    for key, cuts in median_cuts.items():
        jcuts.setdefault(key, []).extend(cuts)
    # small road loops (roundabouts, turnaround loops): no sidewalks facing their inside, a curbed island instead
    import islands
    isl = islands.find(roads, structures=structures) if walks is not None else []
    inner = islands.inner_sides(isl)
    if walks is not None:
        for f in isl:
            if f["ring"] is None:
                continue
            ring = np.vstack([f["ring"], f["ring"][:1]])
            band = offset_polyline(ring, islands.ISLAND_BAND_M / 2)
            for piece in clip_polyline(band, *exact):
                run = densify(simplify(piece))
                if len(run) >= 2:
                    walks.append({"pts": run, "side": 1, "caps": (False, False), "ramps": [],
                                  "w": islands.ISLAND_BAND_M, "fid": "island"})
    cornered = set()                            # road pairs whose sidewalks meet at a curb return
    if corners:
        for (fid, side), cl in jcuts.items():
            for X, u, t in cl:
                for f2, s2, p2, _ in roads:
                    if f2 != fid and np.hypot(*(p2[:, :2] - X).T).min() < 1.0:
                        cornered.add((fid, f2))
        for c in corners:                       # each corner / crosswalk made by the tile it is in
            p = c["arc"][len(c["arc"]) // 2]
            if isl and islands.in_island(isl, p[:2]):
                continue                        # a curb return inside a loop: its island has its own curb
            if x0 <= p[0] < x1 and y0 <= p[1] < y1:
                if walks is not None and not c.get("paved_only"):
                    walks.append({"corner": c})
                for fill in ([c["fill"]] if c["fill"] is not None else []) + c.get("fills", []):
                    look = LOOK['paved']
                    import roadmesh
                    ref=_uv_reference(sections,c.get('uv_fid'),c['arc'])
                    chunks.append({"m": look['m'], "w": 0.0, "tl": look['tl'], "p": look['p'],
                                   'uv_shift': look.get('uv_shift',0.),
                                   "poly": [[round(float(v), 3) for v in q] for q in fill], "n": []})
                if c.get("paved_only") and not c.get('median_crossover'):
                    edge = c["arc"].copy()
                    edge[:,:2] -= c["inward"]*EDGE_LINE["inset"]
                    chunks.append(_chunk(EDGE_LINE,EDGE_LINE["w"],edge))
        for cw in crosswalks:                   # crosswalks and stop lines
            mid = 0.5 * (cw["a"] + cw["b"])
            if x0 <= mid[0] < x1 and y0 <= mid[1] < y1:
                look = junctions.STOP_LINE if cw.get("stop") else junctions.CROSSWALK
                chunks.append(_chunk(look, cw["w"], np.array([cw["a"], cw["b"]])))
    runs_near = []                              # sidewalk runs near the tile, before clipping (walks: below)
    for fid, spec, pts, lon in roads:
        ex = extras(spec, _with_cross(pts, sections), lon, left_hand, frame) if fid not in jinternal else []
        # A bridge range can split one feature into several ground pieces. A fitted ramp
        # edge belongs only to its source piece; applying it to every piece duplicates paint.
        part_key = (tuple(pts[0, :2]), tuple(pts[-1, :2]))
        painted = []
        for look, w, line in ex:
            runs = [(style, run) for style, run in ramp_edges.get((fid, _ramp_paint_key(look)), ())
                    if np.max(np.abs(np.asarray(style['ramp_part'])-part_key)) < .02]
            painted.extend((style, w, run) for style, run in (runs or [(look, line)]))
        ex = [(style,w,run) for look,w,line in painted
              for style,run in roadterminals.solid_runs(fid,look,line,terminals)]
        if not ex:
            continue
        blockers = [(pavement_line(s2,p2,left_hand)[:, :2], s2.width / 2 + CUT_MARGIN) for f2, s2, p2, _ in roads if f2 != fid
                    and frozenset((fid,f2)) not in continuous and f2 not in merges.get(fid,{})]
        lane_blockers = [(pavement_line(s2,p2,left_hand)[:, :2], s2.width/2+CUT_MARGIN)
                         for f2,s2,p2,_ in roads if f2 != fid and frozenset((fid,f2)) not in continuous
                         and f2 not in merges.get(fid,{}) and frozenset((fid,f2)) not in median_pairs]
        ramp_blockers = [(pavement_line(s2,p2,left_hand)[:, :2], s2.width/2+CUT_MARGIN)
                         for f2, s2, p2, _ in roads if f2 != fid and f2 not in ramp_mains.get(fid, ())]
        walk_blockers = [(pavement_line(s2,p2,left_hand)[:, :2], s2.width / 2 if (fid, f2) in cornered else sidewalk_stop(spec, fid, s2, f2))
                         for f2, s2, p2, _ in roads if f2 != fid]
        # a median's band stops at the other carriageways' curbs only (their sidewalk is on their far side: it cut
        # one carriageway's half of Boston Street's median away)
        curb_blockers = [(pavement_line(s2,p2,left_hand)[:, :2], s2.width / 2) for f2, s2, p2, _ in roads if f2 != fid]
        for look, w, line in ex:
            walk = look["m"] == SIDEWALK["m"]
            if walk or look["m"] == GUTTER["m"]:
                line = line[:, :3]              # sidewalk builders retain their own curb cross-sections
            side = look.get("side")
            wb = curb_blockers if look.get("median") else walk_blockers
            if (walk or look["m"] == GUTTER["m"]) and (fid, side) in inner:
                continue                        # facing a traffic island: no sidewalk there
            if walk and walks is not None:
                # whole runs near the tile first (joined across tile borders below), clipped to the tile after
                for piece in clip_polyline(line, *near):
                    for cpiece, (j0, j1) in junctions.apply_cuts(piece, jcuts.get((fid, side), [])):
                        for run in cut_outside(cpiece, wb, 0.5):
                            run = densify(simplify(run))
                            if len(run) < 2:
                                continue
                            at_j0 = j0 and np.hypot(*(run[0, :2] - cpiece[0, :2])) < 0.6
                            at_j1 = j1 and np.hypot(*(run[-1, :2] - cpiece[-1, :2])) < 0.6
                            runs_near.append({"pts": run, "side": side, "w": w, "fid": fid, "j": (at_j0, at_j1),
                                              "median": bool(look.get("median")),
                                              "open": (not _on_box(run[0], near), not _on_box(run[-1], near))})
                continue
            edge_cuts = jcuts.get((fid,look.get("edge_side")),[])
            if look.get("edge_side") is not None and edge_cuts:
                pieces = [cp for p in clip_polyline(line,*box) for cp,_ in junctions.apply_cuts(p,edge_cuts)]
            elif walks is not None and look["m"] == GUTTER["m"]:     # gutters end where their curb turns
                gcuts = jcuts.get((fid, side), [])
                pieces = [cp for p in clip_polyline(line, *box) for cp, _ in junctions.apply_cuts(p, gcuts)]
            elif walks is not None and jlines.get(fid):             # markings stop at the crosswalks
                pieces = [cp for p in clip_polyline(line, *box) for cp, _ in junctions.apply_cuts(p, jlines[fid])]
            else:
                pieces = clip_polyline(line, *box)
            for piece in pieces:
                cuts = ramp_blockers if look.get('ramp_part') is not None else blockers
                if look.get('highway_lane'):
                    cuts = lane_blockers
                for run in cut_outside(piece, wb if walk else cuts, 0.5 if walk else 2.0):
                    for style,paint in _merge_edge_runs(run,look,merge_zones.get(fid, ())):
                        paint_width = style.get('w', w) if style.get('edge_side') is not None or style.get('ramp_guidance') else w
                        for c in split(densify(simplify(paint, CURVE_SIMPLIFY_M, heading=CURVE_HEADING_DEG))):
                            if len(c) >= 2:
                                chunks.append(_chunk(style, paint_width, c, reference=line))
    chunks.extend(_ramp_guides(frame,merge_zones,box,elev))
    chunks.extend(roadterminals.arrows(frame,terminal_rows,terminals,exact))
    if walks is not None:
        runs_near = join_walk_ends(runs_near, [(p2[:, :2], s2.width / 2) for _, s2, p2, _ in roads])
        for rn in runs_near:
            fid, side, w, run0 = rn["fid"], rn["side"], rn["w"], rn["pts"]
            for run in clip_polyline(run0, *exact):
                if len(run) < 2:
                    continue
                first = np.hypot(*(run[0, :2] - run0[0, :2])) < 1e-6
                last = np.hypot(*(run[-1, :2] - run0[-1, :2])) < 1e-6
                at_j0 = first and rn["j"][0]
                at_j1 = last and rn["j"][1]
                caps = (first and not at_j0 and not _on_box(run[0], exact),
                        last and not at_j1 and not _on_box(run[-1], exact))
                rl = [r for key in rn.get("ramp_keys", [(fid, side)]) for r in jramps.get(key, [])]
                ramps = junctions.ramp_ranges(run, rl)
                walks.append({"pts": run, "side": side, "caps": caps, "ramps": ramps, "w": w, "fid": fid,
                              "median": rn.get("median", False)})
                if not rn.get("median"):                # (a median: a bare curb, no storm drains)
                    chunks.extend(drains(run, side, (caps[0] or at_j0, caps[1] or at_j1), w))
    if lines is not None:                           # the centrelines near the tile (buildings' front doors)
        lines.extend((pts[:, :2], spec.width / 2, spec.cls, spec.urban) for _, spec, pts, _ in roads)
    if props is not None and walks is not None:     # street lights and utility poles (furniture.py)
        import furniture
        import navgraph
        speeds = {fid: navgraph.road_mph(f, i, sp, mph) for fid, (f, i, sp) in seen.items()}
        decks = [(np.asarray(ch["n"], np.float64), ch["w"] / 2) for ch in chunks if ch.get("deck")]
        props.extend(furniture.place(roads, walks, (x0, y0, x1, y1), wires=wires, town=bool(town), left_hand=left_hand,
                                     speeds={k: v for k, v in speeds.items() if v is not None}, mph=mph,
                                     decks=decks))
    if park is not None and walks is not None:      # curbside parking spots (parking.py)
        import parking
        dirs = {}
        for fid, (f, i, sp) in seen.items():
            if sp.oneway:
                v = _rule_value(f.rules.get("oneway"), i)
                dirs[fid] = (-1 if float(v) < 0 else 1) if v is not None else (sp.dir or 1)
        park.extend(parking.curb_spots(roads, walks, (x0, y0, x1, y1), left_hand, dirs))
        aisles = {fid for fid, (f, i, sp) in seen.items()
                  if sp.kind == "paved" and _rule_value(f.rules.get("subclass"), i) == "parking_aisle"}
        if aisles:
            park.extend(parking.aisle_spots(roads, aisles, (x0, y0, x1, y1)))
    if osm_objects and exit_signs is not None:
        import exitsigns
        context = next((f.__dict__['_road_context'] for f in cells
                        if f is not None and f.__dict__.get('_road_context') is not None), None)
        if context is None:
            pavements = exitsigns.gore_pavements(frame, osm_objects, {fid: sp for fid, (_, _, sp) in seen.items()},
                                                sections, ramp_mains, auxiliary, left_hand)
            exit_signs.extend(exitsigns.place(frame, osm_objects, roads, exact, elev, chunks, left_hand, mph,
                                              pavements=pavements, ramp_mains=ramp_mains))
        else:
            for obj in {o['id']: o for o in osm_objects if o.get('exit')}.values():
                pavements, mains = exitsigns.source_pavements(frame, obj, context, left_hand)
                exit_signs.extend(exitsigns.place(frame, [obj], (), exact, elev, chunks, left_hand, mph,
                                                  pavements=pavements, ramp_mains=mains))
    import highway_markings
    return highway_markings.apply(chunks, frame)


class Tracker:
    """Per connection: which road tiles the mod has, building and announcing the ones it needs."""

    def __init__(self):
        self.aid = None
        self.sent = set()       # (tx, ty) announced for self.aid
        self.building = set()
        self.pool = None        # its own BUILD_PER_POSE threads (vegetation.Tracker)
        self.futs = {}
        # (from KyzoFox's 1.5.4, PR #64) a tile that came back with no roads was built again at every pose: kept here
        # while it is within reach; a failed one is asked again after TILE_RETRY_S, not at the next pose
        self.empty = set()
        self.retry = {}         # tile -> monotonic time it may be built again
        self.fails = {}         # tile -> builds failed in a row (TILE_FAILS_WARN)

    def close(self):
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)

    def on_pose(self, conn, aid, x, y, loop, build, vel=(0.0, 0.0), ground_busy=False, view=None):
        """Called with the player's position (and velocity, m/s) in the active anchor's frame. build(aid, tx, ty) runs
        in a worker and returns the tile's (path, n) or None; its result is announced with conn.send. ground_busy: a
        terrain block the mod asked for is being built: no tile beyond SEND_M starts meanwhile (the ground first; at
        180 mph the block ahead waited 7.5 s among them, 2026-10-19). view: the camera's (x, y) when it is away from
        the car (earthd camera_away): the tiles round it are wanted too, after the car's (which carry what it drives
        on and hits)."""
        if aid != self.aid:
            for f in self.futs.values():
                f.cancel()
            self.aid, self.sent, self.building, self.futs = aid, set(), set(), {}
            self.empty, self.retry, self.fails = set(), {}, {}
        if self.pool is None:
            from concurrent.futures import ThreadPoolExecutor
            self.pool = ThreadPoolExecutor(BUILD_PER_POSE, thread_name_prefix="roads-tile")
        vx, vy = (float(vel[0]), float(vel[1])) if vel else (0.0, 0.0)
        fast = math.hypot(vx, vy) >= FAST_MS
        car = wanted_tiles(x, y, vx, vy)
        seen = set(tiles_near(*view, SEND_M)) - car if view else set()
        wanted = car | seen
        start = set(tiles_near(x, y, SEND_M)) if ground_busy else wanted
        now = time.monotonic()
        want = [k for k in start if k not in self.sent and k not in self.building and k not in self.empty
                and self.retry.get(k, 0.0) <= now]
        car_key = path_key(x, y, vx, vy) if fast else view_key(conn, aid, x, y)
        cam_key = view_key(conn, aid, *view) if seen else None
        want.sort(key=lambda k: (1, cam_key(k)) if k in seen else (0, car_key(k)))
        for tx, ty in want[:max(0, BUILD_PER_POSE - len(self.building))]:   # in flight at most (vegetation.Tracker)
            self.building.add((tx, ty))
            fut = self.futs[(tx, ty)] = loop.run_in_executor(self.pool, build, aid, tx, ty)
            fut.add_done_callback(lambda f, k=(tx, ty), a=aid: self._built(conn, a, k, f))
        def far(k):
            return tile_distance(k[0], k[1], x, y) > DROP_M and (not view or tile_distance(k[0], k[1], *view) > DROP_M)
        drop = [k for k in self.sent if k not in wanted and far(k)]
        for k in drop:
            self.sent.discard(k)
            conn.send({"type": "roads_drop", "anchorId": aid, "key": f"{k[0]}_{k[1]}"})
        if self.empty:
            self.empty = {k for k in self.empty if k in wanted or not far(k)}
        if self.retry:
            self.retry = {k: t for k, t in self.retry.items() if t > now}

    def _built(self, conn, aid, key, fut):
        if aid != self.aid:                 # (an old anchor's: the same key may be in flight for the new one)
            return
        self.building.discard(key)
        self.futs.pop(key, None)
        if fut.cancelled():
            return
        try:
            res = fut.result()
        except Exception as e:      # network trouble: the next pose asks again
            # (a bug failed every tile on a Steam Deck with nothing in the log, 2026-10-20: each kind once, in full)
            kind = f"{type(e).__name__}: {e}"[:200]
            fails = self.fails[key] = self.fails.get(key, 0) + 1
            if kind not in _FAILS_SEEN and not isinstance(e, (OSError, TimeoutError)):
                _FAILS_SEEN.add(kind)
                log.warning("roads tile %s failed: %s", key, kind, exc_info=e)
            elif fails == TILE_FAILS_WARN:      # network trouble that does not pass (GitHub #91: no roads, no log)
                log.warning("roads tile %s of anchor %d: %d builds failed in a row, the last: %s", key, aid, fails, kind)
            else:
                log.debug("roads tile %s: %s: %s", key, type(e).__name__, e)
            self.retry[key] = time.monotonic() + TILE_RETRY_S
            return
        self.fails.pop(key, None)
        if res is None:                     # no roads there: not built again while it stays within reach
            self.empty.add(key)
            return
        path, n = res
        self.sent.add(key)
        m = {"type": "roads_tile", "anchorId": aid, "key": f"{key[0]}_{key[1]}", "path": path, "n": n}
        pool = getattr(getattr(conn, "s", None), "pool_shapes", None)
        if pool is not None:                # its meshes at pooled shape paths (matslots.py)
            slots = pool(conn, shape_files(aid, *key))
            if slots:
                m["slots"] = slots
        conn.send(m)


TILE_RETRY_S = 3.0              # s before a road tile whose build failed is built again (Tracker._built)
TILE_FAILS_WARN = 5             # a tile's builds failed in a row before an expected kind of failure is logged as a warning
_FAILS_SEEN = set()             # the kinds of road tile failures logged in full (Tracker._built)
BLD_PARTS_MAX = 4               # building meshes per road tile ("bld", "bld2".."bld4": the mod's list)


def shape_files(aid, tx, ty):
    """The shape files write_tile may write for a tile, by the field naming them in the tile ({what: file name})."""
    out = {"road": f"road_{aid}_{tx}_{ty}.cdae", "deck": f"deck_{aid}_{tx}_{ty}.cdae",
           "walk": f"walks_{aid}_{tx}_{ty}.cdae", "walkcol": f"walkcol_{aid}_{tx}_{ty}.cdae",
           "bldcol": f"bldcol_{aid}_{tx}_{ty}.cdae", "wire": f"wire_{aid}_{tx}_{ty}.cdae",
           "bld": f"bld_{aid}_{tx}_{ty}.cdae", "tunnel": f"tunnel_{aid}_{tx}_{ty}.cdae",
           "exits": f"exits_{aid}_{tx}_{ty}.cdae"}
    for k in range(2, BLD_PARTS_MAX + 1):
        out[f"bld{k}"] = f"bld_{aid}_{tx}_{ty}_{k}.cdae"
    return out


def write_tile(out_dir, vfs_dir, aid, tx, ty, chunks, walks=None, road_mesh=None, deck_mesh=None, bld=None,
               props=None, wires=None, park=None, tunnel_mesh=None, exit_mesh=None):
    """Write a tile's chunks (atomically), its sidewalk mesh (walks: build_tile's runs, if any:
    `walks_<a>_<tx>_<ty>.cdae`, announced in the tile as "walk": {shape, pos}) and its road mesh
    (road_mesh: roadmesh.build's result: `road_<a>_<tx>_<ty>.cdae`, "road": {shape, pos}; the mod then
    shows it instead of a DecalRoad per chunk) and its bridge decks (deck_mesh: roadmesh.deck's result:
    `deck_<a>_<tx>_<ty>.cdae`, "deck": {shape, pos}, colliding); returns (vfs path, number of chunks)."""
    import roadmesh
    import sidewalks
    name = f"roads_{aid}_{tx}_{ty}.json"
    path = os.path.join(out_dir, name)
    # (from PR #41, Madderose) with a road mesh the mod makes no DecalRoads: their chunks were ~36 % of the file it
    # parses on its main thread
    data = {"v": VERSION, "chunks": [] if road_mesh is not None else [c for c in chunks if not c.get("tube_only")]}
    if road_mesh is not None:
        x0, y0, _, _ = tile_bbox(tx, ty)
        shape = f"road_{aid}_{tx}_{ty}.cdae"
        roadmesh.write(os.path.join(out_dir, shape), road_mesh)
        data["road"] = {"shape": f"{vfs_dir}/{shape}", "pos": [x0, y0, 0.0]}
    if deck_mesh is not None:
        x0, y0, _, _ = tile_bbox(tx, ty)
        shape = f"deck_{aid}_{tx}_{ty}.cdae"
        roadmesh.write(os.path.join(out_dir, shape), deck_mesh)
        data["deck"] = {"shape": f"{vfs_dir}/{shape}", "pos": [x0, y0, 0.0]}
    if tunnel_mesh is not None:     # roadmesh.tunnel: the tubes and portals (colliding)
        x0, y0, _, _ = tile_bbox(tx, ty)
        shape = f"tunnel_{aid}_{tx}_{ty}.cdae"
        roadmesh.write(os.path.join(out_dir, shape), tunnel_mesh)
        data["tunnel"] = {"shape": f"{vfs_dir}/{shape}", "pos": [x0, y0, 0.0]}
    if exit_mesh is not None:
        x0, y0, _, _ = tile_bbox(tx, ty)
        shape = f"exits_{aid}_{tx}_{ty}.cdae"
        roadmesh.write(os.path.join(out_dir, shape), exit_mesh)
        data["exits"] = {"shape": f"{vfs_dir}/{shape}", "pos": [x0, y0, 0.0]}
    if walks:
        x0, y0, _, _ = tile_bbox(tx, ty)
        b = sidewalks.Builder((x0, y0, 0.0), collision=True)
        for wk in walks:
            if "corner" in wk:
                c = wk["corner"]
                b.add_corner(c["arc"], c["inward"], c["outer"], ramps=c.get("ramps", ()))
                continue
            ww = wk.get("w", SIDEWALK["w"])
            curb, out = sidewalks.run_geometry(wk["pts"], wk["side"], ww)
            b.add_run(curb, out, ww, caps=wk["caps"], ramps=wk.get("ramps", ()),
                      slab=sidewalks.BRICK if wk.get("brick") else 1)
        if not b.empty():
            shape = f"walks_{aid}_{tx}_{ty}.cdae"
            b.write(os.path.join(out_dir, shape))
            data["walk"] = {"shape": f"{vfs_dir}/{shape}", "pos": [x0, y0, 0.0]}
            shape = f"walkcol_{aid}_{tx}_{ty}.cdae"     # the lean physics-only mesh (the drawn one then only renders)
            b.write_collision(os.path.join(out_dir, shape))
            data["walkcol"] = {"shape": f"{vfs_dir}/{shape}", "pos": [x0, y0, 0.0]}
    boxes = collider_boxes(walks if data.get("walk") else None, chunks if data.get("deck") else None)
    if "walk" in boxes and data.get("walkcol"):
        boxes["walkcol"] = boxes["walk"]
    if bld is not None:             # buildings.road_tile: the tile's buildings, colliding ("bld")
        import buildings
        x0, y0, _, _ = tile_bbox(tx, ty)
        meshes = bld[0] if isinstance(bld[0], list) else [bld[0]]
        for k, mesh in enumerate(meshes[:BLD_PARTS_MAX]):   # under the engine's 16-bit index limit each
            what = "bld" if k == 0 else f"bld{k + 1}"
            shape = f"bld_{aid}_{tx}_{ty}.cdae" if k == 0 else f"bld_{aid}_{tx}_{ty}_{k + 1}.cdae"
            buildings.write(os.path.join(out_dir, shape), mesh)
            data[what] = {"shape": f"{vfs_dir}/{shape}", "pos": [x0, y0, 0.0]}
            boxes[what] = bld[1]
        if len(bld) > 2 and bld[2] is not None:     # the lean physics-only mesh (the drawn ones then only render)
            shape = f"bldcol_{aid}_{tx}_{ty}.cdae"
            buildings.write(os.path.join(out_dir, shape), bld[2], collision_only=True)
            data["bldcol"] = {"shape": f"{vfs_dir}/{shape}", "pos": [x0, y0, 0.0]}
            boxes["bldcol"] = bld[1]
    if boxes:
        data["colliders"] = boxes
    if wires:                       # utility pole wires (furniture.wire_mesh), render-only
        import cdae
        import furniture
        x0, y0, _, _ = tile_bbox(tx, ty)
        wm = furniture.wire_mesh(wires, (x0, y0, 0.0))
        if wm is not None:
            shape = f"wire_{aid}_{tx}_{ty}.cdae"
            cdae.write(os.path.join(out_dir, shape), wm[0], wm[1], uvs=wm[2], material="bne_wire", object_name="wires")
            data["wire"] = {"shape": f"{vfs_dir}/{shape}", "pos": [x0, y0, 0.0]}
    if props:                       # furniture.place's rows: Forest items (trees.lua), groups by index
        groups = sorted({r[0] for r in props})
        data["props"] = {"groups": groups, "items": [[groups.index(r[0])] + r[1:] for r in props]}
    if park:                        # parking.py's spots [x, y, z, heading] for the stock parked cars (parking.lua)
        data["park"] = park
    tmp = staging.tmp_for(path)
    with open(tmp, "w") as f:
        f.write(json.dumps(data, separators=(",", ":")))    # json.dump to a file streams through the pure-Python encoder
    staging.replace(tmp, path)
    return f"{vfs_dir}/{name}", len(chunks)


COLLIDER_PIECE_M = 20.0     # collider_boxes: a run / deck cut into boxes this long
COLLIDER_MARGIN_M = 0.5


def collider_boxes(walks, chunks):
    """{"walk": [...], "deck": [...]}: boxes [x0, y0, z0, x1, y1, z1] (anchor frame, m) around the
    sidewalk runs / corners and the bridge decks of a tile, in pieces of COLLIDER_PIECE_M. The mod
    does not make a colliding mesh solid while the player's vehicle is inside one of its boxes (a late
    curb or deck turning solid around a car; the intermittent NaN drives, 2026-10-13). Pure."""
    import roadmesh
    import sidewalks
    out = {}

    def add(what, pts, half_w, z_lo, z_hi):
        pts = np.asarray(pts, np.float64)
        seg = np.r_[0.0, np.cumsum(np.hypot(*np.diff(pts[:, :2], axis=0).T))] if len(pts) > 1 else np.zeros(1)
        cut = np.floor(seg / COLLIDER_PIECE_M).astype(int)
        for k in np.unique(cut):
            idx = np.flatnonzero(cut == k)
            sel = pts[max(idx[0] - 1, 0):idx[-1] + 1]     # the piece and the point before it
            m = half_w + COLLIDER_MARGIN_M
            out.setdefault(what, []).append([round(float(sel[:, 0].min() - m), 1), round(float(sel[:, 1].min() - m), 1),
                                             round(float(sel[:, 2].min() + z_lo), 1), round(float(sel[:, 0].max() + m), 1),
                                             round(float(sel[:, 1].max() + m), 1), round(float(sel[:, 2].max() + z_hi), 1)])
    for wk in walks or ():
        if "corner" in wk:
            c = wk["corner"]             # the curb arc and the outer corner points: one box over both
            pts = np.concatenate([np.asarray(c["arc"], np.float64), np.asarray(c["outer"], np.float64).reshape(-1, 3)])
            add("walk", pts[np.argsort(np.arctan2(pts[:, 1] - pts[:, 1].mean(), pts[:, 0] - pts[:, 0].mean()))],
                0.5, -1.0, 1.0)
            continue
        ww = wk.get("w", SIDEWALK["w"])
        curb, outv = sidewalks.run_geometry(wk["pts"], wk["side"], ww)
        mid = curb.copy()
        mid[:, :2] = curb[:, :2] + outv * (ww / 2)
        add("walk", mid, ww / 2 + 0.5, -1.0, 1.0)
    for c in chunks or ():
        if c.get("deck") and len(c["n"]) > 1:
            add("deck", c["n"], float(c["w"]) / 2 + 1.0, -roadmesh.DECK_DEPTH - 1.0, 2.0)
    return out
