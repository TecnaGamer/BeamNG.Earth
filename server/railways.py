"""Railway tracks: Overture's rail segments (transportation/segment subtype "rail", in the roads layer) as a ballast
bed with concrete sleepers and two steel rails, and their bridges as concrete decks on piers: one colliding mesh per
road tile (write_tile's "rail"). Mapped tram bridges use the same visual/collision
pipeline, with a narrow concrete slab and embedded rails instead of ballast and sleepers.

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

import os

import numpy as np
import dmath
import dnp

import footbridges
import mapgeom
import railxing
import roads

# the ballasted classes drawn (Overture's rail classes); ground trams stay in rails.py,
# their mapped bridges alone are meshed here. Subways, monorails and funiculars are not.
# Nor "unknown": OpenStreetMap's railway=proposed / construction (99 of 101
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
RAIL_FOOT_CLEAR_M = 1.0      # m between a fill's foot and another track's deck/ballast edge
ROAD_FOOT_CLEAR_M = 0.5      # m between a fill's foot and a road's carriageway edge
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
TRAM_DECK_HALF_M = 1.3       # m: a single track's embedded slab (rails.TRACK's tram bed width / 2)
PIER_EVERY = 25.0           # m between piers (from the deck's start)
PIER_W, PIER_T = 3.6, 1.4   # m across and along the deck
PIER_MIN_H = 1.5            # m: a deck lower than this over the ground stands on its ends alone
PIER_FOOT_M = 2.0           # m a pier reaches into the ground
CROSSING_PAD_M = 0.5        # m past a crossing road's edge the track stops (rails.py's street track runs across the road
                            # from 0.3 m inside its edge: 1.5 left ~1.8 m of bare ground either side)
CROSSING_DZ = 1.5           # m: a road this far over or under the bed passes it by (no level crossing)
VERTS_MAX = 60000           # under the engine's 16-bit indices: one shape's vertices (split_mesh cuts a bigger tile mesh)
PARTS_MAX = 3               # shapes a tile's track is cut into ("rail", "rail2", "rail3": roads.RAIL_PARTS_MAX, the mod's list)
TILE_VERTS_MAX = 150000     # a tile's whole track (past it the tracks of lowest rank are left out; cut into PARTS_MAX
                            # shapes of at most VERTS_MAX: the packing wastes up to ~6000 of each)
SPLIT_CHUNK_TRIS = 2000     # triangles taken into a shape at a time
BALLAST = "bne_rail_ballast"
FOOTBRIDGES_ON = os.environ.get("BNE_FOOTBRIDGES", "on") != "off"     # (footbridges.py)
CONCRETE = "bne_bridge_concrete"    # (roadmesh.DECK_SIDE: the decks and piers)
SLEEPER = "bne_rail_sleeper"        # weathered concrete sleepers (bne_bridge_concrete stood out near-white, 2026-10-25)
COLLIDER = "bne_rail_collider"    # a rail's physics-only box (never drawn): the inner face a flange rides along, tile_mesh
COLLIDER_W = 0.30           # m the box reaches out from the rail's inner face (BeamNG wants colliders 30 cm thick)
COLLIDER_DOWN = 0.10        # m the box reaches under the ballast top
STEEL = "bne_rail_steel"            # rust-brown rails
# per wear (wear()): a disused line's ballast weedy, an abandoned one's grown over, their rails and sleepers older, and
# a share of an abandoned line's sleepers gone (SLEEPERS_GONE)
BALLAST_STONE = "bne_rail_ballast_stone"    # crushed granite (BNE_RAIL_XING; tools/ingest_rail_textures.py): a track in use
BALLAST_TILE_M = 0.8        # m one tile of it covers (stones of 5-10 cm); the stock gravel's 3 m
BALLAST_WORN = (BALLAST, "bne_rail_ballast_weedy", "bne_rail_ballast_grown")
STEEL_WORN = (STEEL, "bne_rail_steel_old", "bne_rail_steel_old")
SLEEPER_WORN = (SLEEPER, "bne_rail_sleeper_old", "bne_rail_sleeper_old")
SLEEPERS_GONE = (0.0, 0.0, 0.3)


_GROUND_MEMO = {}            # anchor key -> {point (cm): z}: point_ground's heights, shared by the tiles of an anchor
GROUND_MEMO_MAX = 400000     # points per anchor kept (then forgotten, and asked again)
GROUND_MEMO_ANCHORS = 2


def _point_memo():
    """A {point (cm, int64 x, y): z} memo (earthfast.PointMemo: get(keys (n, 2)) -> z, NaN where none; put; clear)."""
    import fast
    fast.require(240, "PointMemo")
    return fast._ef.PointMemo()


def point_ground(frame, elev, key=None):
    """ground(xy (n, 2)) -> z in frame at exactly those points, from elev(lat, lon) (the graded physics ground). Not
    roadmesh.drawn_ground: that fills a 2 m grid over the points' whole box, and the lines here span ~1 km (a road
    tile's workers spun on it for minutes, 2026-10-25). key (the anchor's): heights remembered per point. Every tile
    within REACH_M of a line asks for the same points (each part resampled from its start), ~6 tiles each, and the
    graded ground's lookups were most of a rail tile's time (16 tiles round Baltimore Penn Station: 28 of 68 s); with a
    key, each point's height is the ground's at its centimetre's own point (the same whichever point asked first)."""
    memo = None
    if key is not None:
        memo = _GROUND_MEMO.get(key)
        if memo is None:
            while len(_GROUND_MEMO) >= GROUND_MEMO_ANCHORS:
                _GROUND_MEMO.pop(next(iter(_GROUND_MEMO)))
            memo = _GROUND_MEMO[key] = _point_memo()

    def look(xy):
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2], np.float64)

    def ground(xy):
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        if not len(xy):
            return np.zeros(0)
        if memo is None:
            return look(xy)
        keys = np.ascontiguousarray(np.round(xy * 100.0).astype(np.int64))
        out = memo.get(keys)
        miss = np.flatnonzero(np.isnan(out))
        if len(miss):
            z = look(keys[miss] / 100.0)        # (at the key's own point, as grade_plans' ground)
            out[miss] = z
            if len(memo) + len(miss) > GROUND_MEMO_MAX:
                memo.clear()
            memo.put(np.ascontiguousarray(keys[miss]), np.ascontiguousarray(z, np.float64))
        return out
    return ground


GRADE_HALF_M = BALLAST_TOP_M + 1.2     # m: the ground graded flat to the bed line either side of a track (grade_plans)
# An abandoned line (wear 2) is not graded: no cutting or embankment is laid for it, its worn track lies on the ground
# as it is (tile_mesh follows the drawn ground). Its mapped remnants are often a few metres off their old bed, or the
# bed is gone: graded to a 2 % line, they cut walls into mountainsides (GitHub #706, Nikko and Mount Haruna) and
# trenches across the land (#837). A disused line (1) keeps its bed, graded as before.
UNGRADED_WEAR = 2
GRADE_REACH_M = 60.0        # m round a grading cell the lines are taken for its bed plans (the track's smoothing and the
                            # ramps near it: REACH_M's 200 m had every cell profile ~1 km of line, 8.5 of 13 s at the A11)


def cells_near(frame, box, reach=REACH_M):
    """The map cells within reach of box (x0, y0, x1, y1) in frame."""
    import mapcells
    x0, y0, x1, y1 = box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach
    lat, lon, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), 0.0)
    return mapcells.cells_for_bbox(float(np.min(lat)), float(np.min(lon)), float(np.max(lat)), float(np.max(lon)))


def _flushed(frame, cells, box, rails, prof, ground, tunnels):
    """prof (profile's [(bed, ground)] per part) with the ground parts' beds brought flush with the roads they cross at
    grade (_flush, as tile_mesh does when it builds the track), for the grader's plans: the terrain is then graded to the
    bed the mesh really has, not to the smoothed ground under a bed that tile_mesh lowers to a road's level (the ballast
    and sleepers sank under the terrain by the road, Kalynivka 2026-11-05)."""
    all_cr = crossings(frame, cells, box, reach=GRADE_REACH_M)
    cr = (true_crossers([xy for _f, c, kind, xy in rails if kind == "ground" and c != "tram"], all_cr)
          if TRUE_CROSSERS_ON else all_cr) + list(tunnels)
    if not cr:
        return prof
    zc = np.asarray(ground(np.concatenate([ln for ln, _ in cr])), np.float64)
    crossers, k = [], 0
    for line, hw in cr:
        crossers.append((line, hw, zc[k:k + len(line)]))
        k += len(line)
    out = []
    for (_fid, _c, kind, xy), (zb, zg) in zip(rails, prof):
        if kind == "ground":
            blk = _blocked(xy, zb, crossers)
            if blk.any():
                zb = _flush(_arc(xy), zb, blk, np.asarray(ground(xy[blk]), np.float64))
        out.append((zb, zg))
    return out


def _beds(frame, rails, ground, underpasses, load=None, elev=None, raw=None):
    """railways.profile of rails, its beds from railprofile's lattice of node profiles (the same bed in every tile and
    grading cell: plan 035) where load (key -> roads layer) and elev (the grader's DEM) are given; railways.profile's own
    where not, for a tram and for a point no node has. raw: ground(xy) -> the DEM's heights as they are (the grader's,
    which does not read the frame's curvature); without it the nodes read elev in the frame (point_ground: the mesh's)."""
    import railprofile
    new = None
    if railprofile.ENABLED and load is not None and (elev is not None or raw is not None) and rails:
        lat0, lon0 = round(float(getattr(frame, "lat0", 0.0)), 7), round(float(getattr(frame, "lon0", 0.0)), 7)
        if raw is None:
            new = railprofile.beds(frame, ("beds", lat0, lon0), load,
                                   point_ground(frame, elev, ("nodes", lat0, lon0)), rails)
        else:
            new = railprofile.beds(frame, ("beds-dem", lat0, lon0), load, raw, rails)
    if new is None or any(np.isnan(z).any() for z in new):
        old = profile(rails, ground, underpasses)
        if new is None:
            return old
        return [(np.where(np.isfinite(z), z, zb), zg) for z, (zb, zg) in zip(new, old)]
    return [(z, np.asarray(ground(xy), np.float64)) for z, (_f, _c, _k, xy) in zip(new, rails)]


def grade_plans(frame, cells, box, elev, load=None):
    """[(fid, xy (k, 2), z (k,))]: the bed line of each on-ground rail part near box, in elev's heights (elev(lat, lon):
    the grader's DEM), for grading.Grader to lay the ground to (the cutting's walls back from the track, the ramps'
    embankments filled). tile_mesh then finds the ground at its bed. Pure given elev."""
    cells = list(cells)
    tunnels = road_underpasses(frame, cells, box, GRADE_REACH_M)
    worn = {}
    rails = parts(frame, cells, box, reach=GRADE_REACH_M, clip=True, worn=worn, underpasses=tunnels)
    if not rails:
        return []
    ungraded = {fid for fid, w in worn.items() if w >= UNGRADED_WEAR}
    memo_key = ("dem", round(float(getattr(frame, "lat0", 0.0)), 7), round(float(getattr(frame, "lon0", 0.0)), 7))
    memo = _GROUND_MEMO.get(memo_key)
    if memo is None:
        memo = _GROUND_MEMO.setdefault(memo_key, _point_memo())

    def ground(xy):
        # (the DEM's heights, remembered per point: every cell's plan asks for the same lines' points)
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        keys = np.ascontiguousarray(np.round(xy * 100.0).astype(np.int64))
        out = memo.get(keys)
        miss = np.flatnonzero(np.isnan(out))
        if len(miss):
            # (at the key's own point, not the asking one's: a memo by the centimetre answered every later point in it
            # with the first one's height, and the copies of a line in neighbouring cells lie ~1e-5 m apart: a cell's
            # rail beds depended on which cells were planned before it, 2026-11-12)
            kxy = keys[miss] / 100.0
            la, lo, _ = frame.enu_to_geodetic(kxy[:, 0], kxy[:, 1], 0.0)
            z = np.asarray(elev(la, lo), np.float64)
            out[miss] = z
            if len(memo) + len(miss) > GROUND_MEMO_MAX:
                memo.clear()
            memo.put(np.ascontiguousarray(keys[miss]), np.ascontiguousarray(z, np.float64))
        return out
    pad = GRADE_HALF_M + 15.0
    out = []
    tunnels = tunnels + coarse_underpasses(frame, cells, box, rails, GRADE_REACH_M)
    prof = _beds(frame, rails, ground, tunnels, load, elev, raw=ground)
    if railxing.ENABLED:        # (the bed flush with the road at a level crossing: the ground is graded to it, else the
        prof = _flushed(frame, cells, box, rails, prof, ground, tunnels)     # ballast and sleepers lay under the ground)
    for (fid, _c, kind, xy), (zb, _zg) in zip(rails, prof):
        if kind != "ground" or fid in ungraded:
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


def _box_hits(fb, box, mask=None):
    """The rows of fb (n, 4) whose box _box_hit's box (and mask's, if given), ascending: the per-feature loops below test
    every feature of a city's cells once per window (a node of railprofile's lattice, a tile, a grading cell), ~700k
    calls a 16-tile probe at Penn Station."""
    fb = np.asarray(fb, np.float64).reshape(-1, 4)
    hit = ~((fb[:, 2] < box[0]) | (fb[:, 0] > box[2]) | (fb[:, 3] < box[1]) | (fb[:, 1] > box[3]))
    if mask is not None:
        hit &= mask
    return np.flatnonzero(hit).tolist()


def _is(col, values):
    """(n,) bool: str(col[i]) in values."""
    return np.isin(np.asarray(col).astype(str), list(values))


def parts(frame, cells, box, reach=REACH_M, clip=False, worn=None, gauges=None, underpasses=None):
    """[(fid, class, kind, xy (k, 2))]: the rail parts ("ground" / "bridge"; tunnels left out) of the lines drawn
    (drawn(); worn: a dict given here gets {fid: wear} of the worn ones, gauges {fid: m} OSM's gauge where tagged)
    within reach of box (x0, y0, x1, y1), resampled every STEP_M from each part's start, each feature once (by id).
    Each part must intersect that window: a feature's bbox can cover far-away bridges or a long bend around it.
    clip: the ground parts cut to their points within reach of box (a segment of line runs on for km, and every one of
    its points was profiled for each cell and tile: _clip); the bridges stay whole (a deck runs between its ends).
    underpasses: road_underpasses(frame, cells, box, reach) when the caller has them already."""
    cells = list(cells)
    near = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    seen, out = set(), []
    wear_of = worn if worn is not None else {}
    # (each feature from the cell holding its longest copy: a long line's copies stitched from the map's tiles can be
    # cut short in some cells, and its level ranges (fractions of its length) then land 100+ m off in the short one:
    # a rail bridge over a road at 60.7771,11.21896 laid on the ground across it, a 2.3 m hump, GitHub #1137)
    longest = {}
    for ci, feats in enumerate(cells):
        if feats is None or len(feats) == 0 or "subtype" not in feats.attrs:
            continue
        for i in _box_hits(feats.boxes(frame), near, _is(feats.attrs["subtype"], ("rail",))):
            fid = str(feats.ids[i])
            L = roads._feature_length_m(feats, i)
            if fid not in longest or L > longest[fid][0] + 1.0:
                longest[fid] = (L, ci)
    for ci, feats in enumerate(cells):
        if feats is None or len(feats) == 0 or "subtype" not in feats.attrs:
            continue
        for i in _box_hits(feats.boxes(frame), near, _is(feats.attrs["subtype"], ("rail",))):
            fid = str(feats.ids[i])
            if fid in seen or longest.get(fid, (0.0, ci))[1] != ci:
                continue
            got = drawn(feats, i, bridges=True)
            if got is None:
                continue
            c = got[0]
            seen.add(fid)
            if got[1]:
                wear_of[fid] = got[1]
            if gauges is not None:
                g = osm_gauge(feats, i)
                if g:
                    gauges[fid] = g
            for kind, rings in _pieces(feats, i):
                if c == "tram" and kind != "bridge":
                    continue                    # street track stays draped in rails.py, never terrain grading
                for ring in rings:
                    got = _ring_xy(feats, frame, ring)
                    if not _box_hit(got[2], near):
                        continue
                    if not roads.clip_polyline(got[1], *near):
                        continue
                    # Test relevance before resampling, but keep the original part: a nearby bridge needs both
                    # ends, and the ground samples must keep their alignment from the part's original start.
                    if got[3] is None:
                        got[3] = _resample(got[1], STEP_M)
                    if len(got[3]) >= 2:
                        out.append((fid, c, kind, got[3].copy()))
    out.sort(key=lambda p: (p[0], p[2], float(p[3][0, 0]), float(p[3][0, 1])))
    if any(kind == "ground" and wear_of.get(fid) for fid, _c, kind, _xy in out):
        out = _cut_worn_at_motorways(out, wear_of, motorway_crossings(frame, cells, box, reach))
    out = _drop_stubs(out)
    out = level_bridges(frame, cells, out, near)
    out = _span_underpasses(out, road_underpasses(frame, cells, box, reach) if underpasses is None else underpasses)
    return _clip(out, near) if clip else out



RING_MEMO_MAX = 20000        # rings kept per layer and frame (_ring_xy)


def _ring_xy(feats, frame, ring):
    """[ring, xy (k, 2) in frame, its box, its resampling (None until asked)] of a lon/lat ring of feats (_pieces' rings:
    the same objects every time), kept on the layer for the frame's coordinates (feats.xy_in's array: when that is made
    again, so are these) and reused by every window: each node of railprofile's lattice converted and resampled the
    same lines again (~150 windows a line in a city)."""
    xin = feats.xy_in(frame) if isinstance(feats, mapgeom.Features) else None
    memos = feats.__dict__.setdefault("_rail_ring_xy", [])
    memo = next((m for x, m in memos if x is xin), None) if xin is not None else None
    if memo is None:
        memo = {}
        if xin is not None:
            memos.append((xin, memo))
            del memos[:-2]                  # (an anchor or two at a time, as xy_in)
    got = memo.get(id(ring))
    if got is None or got[0] is not ring:
        e, n = mapgeom.enu_xy(feats, frame, ring)
        xy = np.column_stack((e, n))
        got = [ring, xy, (xy[:, 0].min(), xy[:, 1].min(), xy[:, 0].max(), xy[:, 1].max()), None]
        if len(memo) >= RING_MEMO_MAX:
            memo.clear()
        memo[id(ring)] = got
    return got


# A short road tunnel may be tagged only on the lower road, with no bridge flag on
# the railway above. Its ballast and grading must not fill the lower carriageway.
UNDERPASS_CLEAR_M = 3.6
UNDERPASS_SIDE_M = 1.0
UNDERPASS_MIN_SIN = 0.25    # conservatively exclude near-parallel tracks beside a tunnel


def _tunnel_tagged(feats):
    """(n,) bool, kept on feats: has feature i a tunnel among the map's ranges (roads.tag_level_ranges)?"""
    got = feats.__dict__.get("_rail_tunnel_tagged")
    if got is None or len(got) != len(feats):
        got = feats.__dict__["_rail_tunnel_tagged"] = np.array(
            [any(k == "tunnel" for _a, _b, k in roads.tag_level_ranges(feats, i)) for i in range(len(feats))], bool)
    return got


def road_underpasses(frame, cells, box, reach=REACH_M):
    """Short road tunnels near box as (xy, half width), including source tunnels made ground by gradesep."""
    near = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    out, seen = [], set()
    for feats in cells:
        if feats is None or not len(feats):
            continue
        specs = roads.tunnel_specs(feats)
        ground_specs = None
        tagged_any = _tunnel_tagged(feats)
        has = np.fromiter((sp is not None for sp in specs), bool, len(specs)) | tagged_any
        for i in _box_hits(feats.boxes(frame), near, has):     # (a road with neither: nothing below)
            sp = specs[i]
            rings = roads.tunnel_parts(feats, i) if sp is not None else []
            # gradesep can turn a short road tunnel into ground and the tracks above it into bridges.
            # Keep that source clearance evidence (Nashville, Carney Street), including when another
            # tunnel on the same feature remains. Retained tunnels keep their cleared portal ends.
            tagged = ([(a, b, kind) for a, b, kind in roads.tag_level_ranges(feats, i) if kind == "tunnel"]
                      if tagged_any[i] else [])
            missing = []
            if tagged:          # (the map's tunnels first: most features have none)
                effective = [(a, b) for a, b, kind in roads.raw_level_ranges(feats, i) if kind == "tunnel"]
                missing = [(a, b, kind) for a, b, kind in tagged
                           if not any(c <= a + roads.MIN_PART and d >= b - roads.MIN_PART for c, d in effective)]
            if missing:
                if ground_specs is None:
                    ground_specs = roads.road_specs(feats)
                sp = sp or ground_specs[i]
                if sp is not None:
                    rings += roads._parts_by_level_new(feats, i, "tunnel", rngs=missing)
            if sp is None:
                continue
            for ring in rings:
                e, n = mapgeom.enu_xy(feats, frame, ring)
                xy = np.column_stack((e, n))
                if len(xy) < 2 or _arc(xy)[-1] > roads.UNDERPASS_MAX_M:
                    continue
                key = (str(feats.ids[i]), tuple(np.round(xy[[0, -1]].ravel(), 2)))
                if key not in seen:
                    seen.add(key)
                    out.append((xy, sp.width / 2 + UNDERPASS_SIDE_M))
    return out


# Where the elevation is only the coarse global model (dem.coarse_at; the server sets COARSE_AT to its Dem's), a rail
# bridge over a ground road stands on an embankment the 30 m data smeared away: the track is raised over the road (its
# piece COARSE_PIECE_M either side of the crossing, as a tunnel's mouths) and ramps at GRADE, and the road stays at
# grade (grading leaves rail spans alone there). Perm, GitHub #414: the underpass rule dug Первомайская улица 7.5 m
# under the Балмошная–Лёвшино line, a walled trench 400 m long where the street is at grade under an embankment.
COARSE_AT = None
COARSE_PIECE_M = 25.0
# ... and on lidar wherever the elevation shows no grade separation (a rail bridge built after the survey: Davis Road
# under the Armadale Line at Kelmscott, WA, where the 5 m DEM still has the old level crossing; the underpass rule dug
# the street 8 m under the rail, the user 2026-10-29). grading's UNDER_LIDAR rule (PR #504) keeps such a road at its
# own level where the deck (its ends' ground) stands less than roads.AT_GRADE_M over it; flat_rail_crossing asks the
# same of a rail bridge, so every crossing that rule leaves at grade gets its track raised over the road (as on coarse
# elevation), not a deck lying across the road. RAW_ELEV: the server sets it to the grader's own elevation
# (PROFILE_ZOOM). Off with BNE_RAIL_FLAT_SEP=0, or with the rule itself (BNE_UNDER_LIDAR=off).
FLAT_SEP_ON = os.environ.get("BNE_RAIL_FLAT_SEP", "0") == "1" and os.environ.get("BNE_UNDER_LIDAR", "on") != "off"    # (opt-in: grading.NEIGHBOUR_ON)
FLAT_SEP_M = roads.AT_GRADE_M
FLAT_SAMPLE_M = (-12.0, -6.0, 0.0, 6.0, 12.0)  # m along the road either side of the crossing (the median)
RAW_ELEV = None


LEVEL_BRIDGES_ON = os.environ.get("BNE_RAIL_LEVEL_BRIDGES", "on") != "off"   # (plan 035: Nachod's "ravine")
LEVEL_BRIDGE_MAX_M = 40.0   # m: a rail bridge this short or shorter over a road on level ground is no bridge
LEVEL_BRIDGE_SEP_M = 1.5    # m: level ground: the track's ground past the bridge's ends less than this over the road's
LEVEL_BRIDGE_OUT_M = 8.0    # m past each end of the bridge, along the track, where its ground is taken


def level_bridge(frame, bridge_xy, road_xy, at, elev=None):
    """Is the short rail bridge (LEVEL_BRIDGE_MAX_M) over road_xy (crossing it at arc at) on level ground? The track's
    natural ground LEVEL_BRIDGE_OUT_M past each of its ends (the higher one) stands less than LEVEL_BRIDGE_SEP_M over the
    road's (the median of FLAT_SAMPLE_M about the crossing). A short mapped bridge over a primary
    road beside Nachod station on level ground (lidar: 344 m at both) dug the road 4 m down, and the bed beside it with
    it (railroadseam): a ravine the user saw, 2026-11-09. Off lidar (no RAW_ELEV): False."""
    if not LEVEL_BRIDGES_ON:
        return False
    b = np.asarray(bridge_xy, np.float64)[:, :2]
    if len(b) < 2 or float(_arc(b)[-1]) > LEVEL_BRIDGE_MAX_M:
        return False
    elev = elev if elev is not None else RAW_ELEV
    if elev is None:
        return False
    road_xy = np.asarray(road_xy, np.float64)[:, :2]
    s = _arc(road_xy)
    a = np.clip(at + np.asarray(FLAT_SAMPLE_M), 0.0, s[-1])
    road = np.c_[np.interp(a, s, road_xy[:, 0]), np.interp(a, s, road_xy[:, 1])]
    outs = []                       # (the track's own ground past each end: a mapped bridge's end may lie on the cut's
    for i, j in ((0, 1), (-1, -2)):  # slope, Nachod's road 14 under the Kladska line, 1.3 m over the road there)
        d = b[i] - b[j]
        outs.append(b[i] + d / max(float(dnp.hypot(*d)), 1e-9) * LEVEL_BRIDGE_OUT_M)
    pts = np.r_[road, np.asarray(outs)]
    la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
    try:
        z = np.asarray(elev(np.asarray(la, np.float64), np.asarray(lo, np.float64)), np.float64).ravel()
    except Exception:
        return False
    if not np.all(np.isfinite(z)):
        return False
    return bool(float(np.max(z[-2:])) < float(np.median(z[:-2])) + LEVEL_BRIDGE_SEP_M)


LIMIT_NEAR_M = 4.0      # m: a road's height limit holds at a crossing the road passes under a bridge within this


def height_limits(frame, cells):
    """[(xy (k, 2) ENU, limit m)] of the height limits (roads.height_limit_lines) of the cells' roads."""
    out = []
    for feats in cells:
        if feats is None or len(feats) == 0:
            continue
        for ring, h in roads.height_limit_lines(feats):
            e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
            out.append((np.column_stack((np.asarray(e, np.float64), np.asarray(n, np.float64))), h))
    return out


def limit_near(limits, pt):
    """Does a height limit [(xy, m)] pass within LIMIT_NEAR_M of the point pt (x, y)?"""
    p = np.asarray(pt, np.float64)[None, :2]
    return any(len(xy) >= 2 and float(roads._seg_dist(p, xy[:-1], xy[1:]).min()) <= LIMIT_NEAR_M for xy, _h in limits)


def level_bridges(frame, cells, rails, reach_box):
    """rails with each short bridge part over a road on level ground (level_bridge) made a ground part: the road then
    crosses it at grade (a level crossing) instead of being dug under it."""
    if not LEVEL_BRIDGES_ON or RAW_ELEV is None or not any(k == "bridge" for _f, _c, k, _x in rails):
        return rails
    ways = [ln for ln, _hw in crossings(frame, cells, reach_box, reach=0.0)]
    if not ways:
        return rails
    limits = height_limits(frame, cells)        # (a road with a height limit under it is under a bridge: never a crossing)
    out = []
    for fid, c, kind, xy in rails:
        if kind == "bridge" and c != "tram" and float(_arc(xy)[-1]) <= LEVEL_BRIDGE_MAX_M:
            # (every way's crossings in one kernel call, as _crossings gives them way by way: a call per way per short
            # bridge per window was 1.6 million calls, ~30 s of a 16-tile probe at Brno)
            for line, hits in zip(ways, roads._crossings_by([xy], ways)):
                if hits and level_bridge(frame, xy, line, float(np.clip(_seg_arc(line, hits[0][2]), 0.0, 1e9))) \
                        and not limit_near(limits, hits[0][2]):
                    kind = "ground"
                    break
        out.append((fid, c, kind, xy))
    return out


def _seg_arc(line, pt):
    """The arc (m) along line of its point nearest pt."""
    line = np.asarray(line, np.float64)[:, :2]
    s = _arc(line)
    ab = np.diff(line, axis=0)
    t = np.clip(((pt - line[:-1]) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
    j = int(np.argmin(dnp.norm(line[:-1] + t[:, None] * ab - pt, axis=1)))
    return float(s[j] + t[j] * (s[j + 1] - s[j]))


def flat_rail_crossing(frame, bridge_xy, road_xy, at, elev=None):
    """Does the natural ground show no separation where road_xy (k, 2) crosses under the rail bridge bridge_xy at arc
    at (m along the road)? As grading's UNDER_LIDAR rule: the deck there (its ends' ground, interpolated along it, the
    lowest within roads.DECK_CLEAR_REACH_M of the crossing) stands less than FLAT_SEP_M (roads.AT_GRADE_M) over the
    road's ground (the median of FLAT_SAMPLE_M). elev(lat, lon): RAW_ELEV by default; False without one (as before).
    Pure given elev."""
    elev = elev if elev is not None else RAW_ELEV
    if not FLAT_SEP_ON or elev is None:
        return False
    road_xy = np.asarray(road_xy, np.float64)[:, :2]
    b = np.asarray(bridge_xy, np.float64)[:, :2]
    s = _arc(road_xy)
    a = np.clip(at + np.asarray(FLAT_SAMPLE_M), 0.0, s[-1])
    pts = np.r_[np.c_[np.interp(a, s, road_xy[:, 0]), np.interp(a, s, road_xy[:, 1])], b[[0, -1]]]
    la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
    try:
        z = np.asarray(elev(np.asarray(la, np.float64), np.asarray(lo, np.float64)), np.float64).ravel()
    except Exception:
        return False
    if not np.all(np.isfinite(z)):
        return False
    # (where along the bridge the road crosses: the deck within roads.DECK_CLEAR_REACH_M of it, as the rule takes it)
    sb = _arc(b)
    L = max(float(sb[-1]), 1e-9)
    hits = _seg_hits(b, road_xy)
    arc_b = hits[0] if hits else 0.5 * L
    corridor = np.clip([arc_b - roads.DECK_CLEAR_REACH_M, arc_b + roads.DECK_CLEAR_REACH_M], 0.0, L)
    deck = float(np.min(z[-2] + (z[-1] - z[-2]) * corridor / L))
    return bool(deck < float(np.median(z[:-2])) + FLAT_SEP_M)


def _coarse_here(frame, box):
    if COARSE_AT is None:
        return False
    try:
        la, lo, _ = frame.enu_to_geodetic(0.5 * (box[0] + box[2]), 0.5 * (box[1] + box[3]), 0.0)
        return bool(COARSE_AT(float(la), float(lo)))
    except Exception:
        return False


def _seg_hits(a, b):
    """Arc positions along polyline a where it crosses polyline b (proper segment intersections). Pure."""
    out = []
    sa = _arc(a)
    for i in range(len(a) - 1):
        p, r = a[i], a[i + 1] - a[i]
        for j in range(len(b) - 1):
            q, t = b[j], b[j + 1] - b[j]
            den = r[0] * t[1] - r[1] * t[0]
            if abs(den) < 1e-12:
                continue
            u = ((q[0] - p[0]) * t[1] - (q[1] - p[1]) * t[0]) / den
            v = ((q[0] - p[0]) * r[1] - (q[1] - p[1]) * r[0]) / den
            if 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0:
                out.append(float(sa[i] + u * (sa[i + 1] - sa[i])))
    return out


def coarse_underpasses(frame, cells, box, rails, reach=REACH_M):
    """[(xy, half width)] (as road_underpasses): on coarse elevation (COARSE_AT), each ground road's piece
    COARSE_PIECE_M either side of where a rail bridge part crosses it."""
    bridges = [xy for (_fid, _c, kind, xy) in rails if kind == "bridge" and len(xy) >= 2]
    if not bridges:
        return []
    coarse = _coarse_here(frame, box)
    if not coarse and not (FLAT_SEP_ON and RAW_ELEV is not None):
        return []
    out = []
    for line, half in crossings(frame, cells, box, reach):
        if len(line) < 2:
            continue
        sl = _arc(line)
        for b in bridges:
            for sc in _seg_hits(line, b):
                if not coarse and not flat_rail_crossing(frame, b, line, sc):
                    continue
                a0, a1 = max(0.0, sc - COARSE_PIECE_M), min(float(sl[-1]), sc + COARSE_PIECE_M)
                if a1 - a0 < 1.0:
                    continue
                ss = np.linspace(a0, a1, max(3, int((a1 - a0) / STEP_M) + 1))
                piece = np.c_[np.interp(ss, sl, line[:, 0]), np.interp(ss, sl, line[:, 1])]
                out.append((piece, half + UNDERPASS_SIDE_M))
    return out


def _underpass_distance(xy, line):
    """Distance to a finite tunnel centreline, and fraction along it, per point."""
    s = _arc(line)
    dist = np.full(len(xy), np.inf)
    along = np.zeros(len(xy))
    for j, (a, b) in enumerate(zip(line[:-1], line[1:])):
        d = b - a
        length = float(dnp.hypot(*d))
        if length < 1e-6:
            continue
        t = np.clip(dnp.matmul(xy - a, d) / (length * length), 0.0, 1.0)
        dd = dnp.norm(xy - (a + t[:, None] * d), axis=1)
        take = dd < dist
        dist[take] = dd[take]
        along[take] = (s[j] + t[take] * length) / max(s[-1], 1e-6)
    return dist, along


def _rail_boxes(rails):
    """(n, 4) the boxes (x0, y0, x1, y1) of the rails' parts, for _underpass_masks (made once for every tunnel of a
    call: each tunnel took every part's min and max again, ~6 of a cold Le Mans block + 4 tiles' ~50 CPU-s)."""
    out = np.zeros((len(rails), 4))
    for n, (*_, xy) in enumerate(rails):
        out[n, :2] = xy.min(0)
        out[n, 2:] = xy.max(0)
    return out


def _underpass_masks(rails, line, half, margin, boxes=None):
    """Points near a tunnel, connected locally to a track that actually crosses it.

    A crossing on one track must not qualify a neighbouring track. Connect only
    the runs inside the tunnel's padded footprint: this preserves source-feature
    splits without following a turnout or a returning track far outside it.
    boxes: _rail_boxes(rails), made once by a caller with several tunnels.
    """
    masks = [np.zeros(len(xy), bool) for *_, xy in rails]
    candidates, runs, owners = [], [], []
    pad = half + margin
    near = (*(line.min(0) - pad), *(line.max(0) + pad))
    direction = line[-1] - line[0]
    hit = set(_box_hits(_rail_boxes(rails) if boxes is None else boxes, near))
    for n, (*_, xy) in enumerate(rails):
        if n not in hit:
            candidates.append(masks[n])         # (nothing near: no run; the zeros mask, never written to)
            continue
        tangent = np.gradient(xy, axis=0)
        cross = np.abs(tangent[:, 0] * direction[1] - tangent[:, 1] * direction[0])
        cross /= np.maximum(dnp.norm(tangent, axis=1) * dnp.norm(direction), 1e-9)
        distance, _ = _underpass_distance(xy, line)
        covered = (distance <= pad) & (cross > UNDERPASS_MIN_SIN)
        candidates.append(covered)
        spans = covered[:-1] | covered[1:]
        cuts = np.flatnonzero(np.diff(np.concatenate(([False], spans, [False]))))
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
    boxes = _rail_boxes(rails)
    for line, half in tunnels:
        for keep, mask in zip(covered, _underpass_masks(rails, line, half, FOOT_MAX_M + STEP_M, boxes)):
            keep |= mask
    out = []
    for (fid, cls, kind, xy), mask in zip(rails, covered):
        if kind != "ground":
            out.append((fid, cls, kind, xy))
            continue
        spans = mask[:-1] | mask[1:]
        cuts = np.concatenate(([0], np.flatnonzero(spans[1:] != spans[:-1]) + 1, [len(spans)]))
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
    # A tram's approaches are drawn by rails.py, not present in this graph. Its
    # short mapped bridges are complete structures, not disconnected sidings.
    return [r for r, (a, _b, _s) in zip(rails, edges) if r[1] == "tram" or total[find(a)] >= MIN_LINE_M]


WEAR_FLAGS = (("is_abandoned", 2), ("is_disused", 1))    # a line's wear: 1 out of use, 2 abandoned (drawn worn: the
# user, 2026-10-27, "id still like abandond lines to show. perhapse with some grass on them or sumilate wear")
UNBUILT_FLAGS = ("is_under_construction",)              # not drawn (no track yet), as OSM's proposed lines
TUNNELS_ON = os.environ.get("BNE_RAIL_TUNNELS", "off") not in ("off", "0", "false", "")   # (plan 035: unproven in the game)
TUNNEL_HALF_M = 3.9         # m: half the rail tunnel's inner width
TUNNEL_WALL_H = 1.6         # m its walls stand straight over the ballast top before the vault springs
TUNNEL_RISE_M = 4.4         # m the vault rises over the springing (a half ellipse: the tube is 6 m over the ballast)
TUNNEL_FLOOR_M = 0.3        # m the tube's walls go under the ballast top
TUNNEL_SECTION_N = 14       # points of the vault
PORTAL_REACH_M = 1.3        # m a portal's face reaches round the opening
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


def drawn(feats, i, bridges=False):
    """(class, wear) of a built railway track, or None. bridges also admits trams:
    only their mapped bridge pieces belong in the 3D mesh; ground trams remain
    street track in rails.py, not ballast, grading or inferred underpasses."""
    sub = feats.attrs.get("subtype")
    if sub is None or str(sub[i]) != "rail":
        return None
    w = wear(feats, i)
    if w is None:
        return None
    cls = feats.attrs.get("class")
    c = str(cls[i]) if cls is not None else "standard_gauge"
    osm = feats.attrs.get("osm_railway")
    if osm is not None and osm[i]:          # (OSM's own railway value: railtags.py, the weekly rail file)
        v = str(osm[i])
        if v == "tram":
            return ("tram", w) if bridges else None
        if v in OSM_RAILWAY_NOT:
            return None
        hw = feats.attrs.get("osm_highway")
        if v == "abandoned" and hw is not None and hw[i]:
            return None             # a rail trail: the rails are gone, a road or path runs on the old bed (#1103)
        if v in OSM_RAILWAY_WEAR:
            w = OSM_RAILWAY_WEAR[v]
            return (c if c in DRAWN else "standard_gauge", w)
    if c == "unknown" and w:
        c = "standard_gauge"
    return (c, w) if c in DRAWN or (bridges and c == "tram") else None


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
    """[("ground" | "bridge" | "tunnel", [lon/lat polylines])] of feature i: its level ranges with the short tunnels
    (SHORT_TUNNEL_M) on the ground and the long ones left out (BNE_RAIL_TUNNELS: a part of their own, kind "tunnel")."""
    rngs = roads.level_ranges(feats, i)
    if not rngs:
        return [("ground", roads.ground_parts(feats, i))]
    # (remembered on the layer per feature and its ranges, which are asked every time (the lidar rule's revisit fault
    # with them): every node window of railprofile's lattice cut the same lines again, ~150 times a line in a city)
    memo = feats.__dict__.setdefault("_rail_pieces", {})
    key = (i, tuple(tuple(r) for r in rngs), TUNNELS_ON)
    got = memo.get(key)
    if got is None:
        got = memo[key] = _pieces_of(feats, i, rngs)
    return [(kind, list(rings)) for kind, rings in got]


MIN_PART_M = 1.0          # m: a piece of a rail line shorter than this is a rounding sliver, whatever the line's length


def _pieces_of(feats, i, rngs):
    L = roads._feature_length_m(feats, i)
    # (roads.MIN_PART is a share of the segment: 1e-3 of a 19 km rail segment is 19 m, and its 11 m bridges over the
    # roads were dropped, the track lying across them; at most MIN_PART_M, as for a short feature)
    min_part = min(roads.MIN_PART, MIN_PART_M / max(L, 1.0))
    off = [(a, b, k) for a, b, k in rngs if not (k == "tunnel" and (b - a) * L < SHORT_TUNNEL_M)]
    ground, at = [], 0.0
    for a, b, _k in off:
        if a - at > min_part:
            ground.append((at, a))
        at = max(at, b)
    if 1.0 - at > min_part:
        ground.append((at, 1.0))
    out = {"ground": [], "bridge": [], "tunnel": []}
    for part in feats.parts(i):
        for ring in part:
            if len(ring) < 2:
                continue
            for kind, spans in (("ground", ground), ("bridge", [(a, b) for a, b, k in off if k == "bridge"]),
                                ("tunnel", [(a, b) for a, b, k in off if k == "tunnel"] if TUNNELS_ON else [])):
                for a, b in spans:
                    if b - a > min_part:
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
        for i in _box_hits(feats.boxes(frame), near):
            sp = specs[i]
            fid = str(feats.ids[i])
            if sp is None or fid in seen:
                continue
            seen.add(fid)
            for ring in roads.ground_parts(feats, i):
                e, n = mapgeom.enu_xy(feats, frame, ring)
                out.append((np.column_stack((e, n)), sp.width / 2))
    return out


# GitHub #579: a road's sidewalks and kerbs stop where a railway crosses it at grade (the ballast runs across them)
WALK_CLEAR_M = BALLAST_TOP_M + BALLAST_H * BALLAST_SLOPE    # m from the track's centre: the ballast's foot
WALK_MIN_SIN = 0.3          # a track this square or more to a road crosses it (a parallel one beside it leaves it be)


def walk_cut_lines(frame, cells, box, reach=60.0):
    """[(xy (k, 2), half width)]: the drawn railway tracks on the ground near box (trams left out: street track, its
    sidewalks stay; Sweden's are rails.tram_lines), what a sidewalk crossing them stops at (roads.build_tile)."""
    near = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    seen, out = set(), []
    for feats in cells:
        if feats is None or len(feats) == 0 or "subtype" not in feats.attrs:
            continue
        for i in _box_hits(feats.boxes(frame), near, _is(feats.attrs["subtype"], ("rail",))):
            fid = str(feats.ids[i])
            if fid in seen:
                continue
            seen.add(fid)
            if drawn(feats, i) is None:
                continue
            for kind, rings in _pieces(feats, i):
                if kind != "ground":
                    continue
                for ring in rings:
                    e, n = mapgeom.enu_xy(feats, frame, ring)
                    xy = np.column_stack((e, n))
                    if len(xy) >= 2 and _box_hit((xy[:, 0].min(), xy[:, 1].min(), xy[:, 0].max(), xy[:, 1].max()),
                                                 near):
                        out.append((xy, WALK_CLEAR_M))
    return out


def crosses(a, b, min_sin=WALK_MIN_SIN):
    """Does polyline b (k, 2) cross polyline a (m, 2) somewhere at |sin| >= min_sin between their segments?"""
    a = np.asarray(a, np.float64)[:, :2]
    b = np.asarray(b, np.float64)[:, :2]
    if len(a) < 2 or len(b) < 2:
        return False
    if a[:, 0].max() < b[:, 0].min() or b[:, 0].max() < a[:, 0].min() or             a[:, 1].max() < b[:, 1].min() or b[:, 1].max() < a[:, 1].min():
        return False
    p, r = a[:-1, None, :], (a[1:] - a[:-1])[:, None, :]
    q, s = b[None, :-1, :], (b[1:] - b[:-1])[None, :, :]
    rxs = r[..., 0] * s[..., 1] - r[..., 1] * s[..., 0]
    qp = q - p
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (qp[..., 0] * s[..., 1] - qp[..., 1] * s[..., 0]) / rxs
        u = (qp[..., 0] * r[..., 1] - qp[..., 1] * r[..., 0]) / rxs
        sin = np.abs(rxs) / (dnp.hypot(r[..., 0], r[..., 1]) * dnp.hypot(s[..., 0], s[..., 1]))
    hit = (t >= 0) & (t <= 1) & (u >= 0) & (u <= 1) & (sin >= min_sin)
    return bool(np.any(hit))


# A worn line (disused / abandoned) mapped across a motorway on the ground: the road was built over the old bed, and
# the rails are long gone there (a motorway has no level crossing). Drawn and graded, the track's bed line made the
# carriageway dip to it (GitHub #918: four such dips on the A20 in Mecklenburg-Vorpommern, the lines tagged historic,
# abandoned or dismantled). The track is cut across the carriageway and WORN_CUT_M past its edges. An abandoned line
# is cut at a trunk road too (rebuilt over it); a disused one only at a motorway (a trunk road's level crossing can
# still have its rails).
WORN_CUT_M = 6.0
WORN_CUT_CLASSES = {1: {"motorway"}, 2: {"motorway", "trunk"}}
WORN_CUT_MIN_SIN = 0.2      # (a crossing flatter than this cuts as if at this angle: the cut stays bounded)


def motorway_crossings(frame, cells, box, reach=REACH_M):
    """[(xy (k, 2), half width, class)]: the ground motorway and trunk carriageways (their links too) near box."""
    near = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    seen, out = set(), []
    for feats in cells:
        if feats is None or len(feats) == 0 or "class" not in feats.attrs:
            continue
        clss = feats.attrs["class"]
        big = _is(clss, ("motorway", "trunk", "motorway_link", "trunk_link"))
        if not big.any():
            continue
        for i in _box_hits(feats.boxes(frame), near, big):
            c = str(clss[i])
            base = c[:-5] if c.endswith("_link") else c
            fid = str(feats.ids[i])
            if fid in seen:
                continue
            seen.add(fid)
            st = roads.STYLE.get(c) or roads.STYLE.get(base)
            if st is None or roads.unbuilt(feats, i):
                continue
            for ring in roads.ground_parts(feats, i):
                e, n = mapgeom.enu_xy(feats, frame, ring)
                if len(e) >= 2:
                    out.append((np.column_stack((e, n)), st[0] / 2.0, base))
    return out


def _segment_at(line, p):
    """The direction (b - a) of line's segment nearest point p. Pure."""
    a, b = line[:-1], line[1:]
    d = b - a
    t = np.clip(dnp.vecdot(p - a, d) / np.maximum(dnp.vecdot(d, d), 1e-9), 0.0, 1.0)
    k = int(np.argmin(dnp.norm(p - (a + t[:, None] * d), axis=1)))
    return d[k]


def _cut_worn_at_motorways(rails, wear_of, roads_xy):
    """rails with each worn ground part (wear_of {fid: 1 | 2}) cut where it crosses one of roads_xy ((xy, half width,
    class): motorway_crossings) of its wear's WORN_CUT_CLASSES: no point within the carriageway's half width (over
    the crossing's angle) and WORN_CUT_M either side of the crossing. Pure."""
    if not roads_xy:
        return rails
    out = []
    for fid, cls, kind, xy in rails:
        w = wear_of.get(fid)
        if kind != "ground" or not w:
            out.append((fid, cls, kind, xy))
            continue
        lines = [(line, half) for line, half, c in roads_xy if c in WORN_CUT_CLASSES.get(w, ())]
        if not lines:
            out.append((fid, cls, kind, xy))
            continue
        s = _arc(xy)
        cut = np.zeros(len(xy), bool)
        for (line, half), hits in zip(lines, roads._crossings_by([xy], [ln for ln, _h in lines])):
            for _pi, arc, p in hits:
                j = int(np.clip(np.searchsorted(s, arc) - 1, 0, len(xy) - 2))
                t = xy[j + 1] - xy[j]
                r = _segment_at(line, p)        # (the road's direction at the crossing)
                sin = abs(t[0] * r[1] - t[1] * r[0]) / max(dnp.norm(t) * dnp.norm(r), 1e-9)
                cut |= np.abs(s - arc) <= half / max(sin, WORN_CUT_MIN_SIN) + WORN_CUT_M
        if not cut.any():
            out.append((fid, cls, kind, xy))
            continue
        keep = ~cut
        idx = np.flatnonzero(keep)
        for run in np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1) if len(idx) else []:
            if len(run) >= 2:
                out.append((fid, cls, kind, xy[run[0]:run[-1] + 1]))
    return out


def _resample(xy, step):
    # (np.concatenate / column_stack, not np.r_ / np.c_: the same arrays, and those index tricks were a fifth of a node
    # profile's time, railprofile.py)
    s = _arc(xy)
    if s[-1] <= 1e-6:
        return xy[:1]
    t = np.append(np.arange(0.0, s[-1], step), s[-1])
    if len(t) > 2 and t[-1] - t[-2] < 0.25 * step:     # (no sliver section at the end)
        t = np.append(t[:-2], t[-1])
    return np.column_stack((np.interp(t, s, xy[:, 0]), np.interp(t, s, xy[:, 1])))


def _arc(xy):
    return np.concatenate(([0.0], np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))))


def _left(xy):
    d = np.gradient(xy, axis=0)
    d /= np.maximum(dnp.hypot(d[:, 0], d[:, 1]), 1e-9)[:, None]
    return np.c_[-d[:, 1], d[:, 0]]


def _avg(v, r):
    """Running mean over 2r + 1 values, the ends extended along their grade (odd reflection: a straight grade stays
    straight to its end, and two parts meeting at a joint meet at its height)."""
    r = min(r, len(v) - 1)
    if r < 1:
        return np.asarray(v, np.float64)
    pad = np.concatenate((2 * v[0] - v[r:0:-1], v, 2 * v[-1] - v[-2:-r - 2:-1]))
    c = np.concatenate(([0.0], np.cumsum(pad)))
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


DECK_RAISE_MAX_M = 15.0   # m a bridge deck may rise over the straight chord between its ends to clear high
                            # ground under it (a bad-data ridge: over a real valley the chord already stands over the
                            # floor, so genuine gorge viaducts are unaffected)
SEED_LIFT_MAX_M = 20.0    # m a propagated deck height may lift a ground track over its own smoothed bed (one bad
                            # deck must not poison kilometres of connected line, and the graded embankment with it)


def deck_heights(s, z_ground, z_start, z_end):
    """A bridge's bed line: straight from z_start to z_end (the bed at its ends: a line keeps its grade over what it
    crosses, the stream or road under it goes down, and the bare-earth DEM has that dip), raised only where the ground
    under it stands higher than the track itself (bad data, a ridge), climbing at most GRADE. Pure. (A required 6 m
    clearance stood an 8 m culvert's track 6.8 m up on 4.5 m ramps, 2026-10-25.)"""
    s = np.asarray(s, np.float64)
    L = max(float(s[-1]), 1e-9)
    chord = z_start + (z_end - z_start) * s / L
    req = np.maximum(chord, np.asarray(z_ground, np.float64))
    # A single bad height under the deck (an unfiltered building/tree in the surface model, a DEM spike) must not
    # raise the whole deck: over a real valley the chord already stands over the floor, so only the raise over the
    # chord is capped (seen as a ~100 m deck hump with piers and graded embankment from one bad sample under it).
    req = np.minimum(req, chord + DECK_RAISE_MAX_M)
    return _envelope(s, req)


# A bridge's mapped ends often lie part way down the slopes of what it crosses (a cutting, a valley's sides): the
# bare-earth DEM there is the slope, not the track, which runs on at its embankment's level up to the deck. The Abbey
# Line's bridge over the M1 near Bricket Wood (51.7103, -0.3537): the track at 78.2 m on both sides, the mapped
# 46 m bridge's ends at 73.5 m down the cutting's sides, the motorway at 71.5 m; the deck laid between those ends
# blocked the motorway (the user's screenshot, 2026-10-31). The track keeps its level: within APPROACH_REACH_M beyond
# an end, where the ground along the track's line runs level (within its grade, APPROACH_FLAT_M long), that level less
# the grade back to the end is the deck's end at least, raised by at most APPROACH_RAISE_MAX_M. A track climbing a
# hillside on from the bridge (no level stretch) keeps the ground's height.
APPROACH_REACH_M = 40.0     # m beyond a bridge's end the track's level is looked for
APPROACH_FROM_M = 4.0       # m beyond the end the level stretch may start, at the earliest
APPROACH_FLAT_M = 16.0      # m of track over which the ground has to be level (within the grade, + APPROACH_TOL_M)
APPROACH_TOL_M = 0.4        # m: the DEM's own bumps on a level stretch
APPROACH_MIN_M = 0.5        # m: a smaller rise is left as it is
APPROACH_RAISE_MAX_M = 8.0  # m an end is raised at most (more: bad data, left as it is)
LINE_GRADE = {"light_rail": 0.05}   # a class's steepest grade where it is not GRADE's (light rail climbs to 5-6 %)


def approach_levels(xy, ground, grade=GRADE):
    """(start, end): per end of a bridge part xy (k, 2), (level m, reach m) of the level the track holds beyond it
    (APPROACH_*: the deck's end and the track up to reach m out are at it at least), or None where there is none.
    Beyond each end the line goes straight on along its last segment. ground(xy (n, 2)) -> z. Pure given ground."""
    return approach_levels_many([xy], ground, [grade])[0]


def approach_levels_many(xys, ground, grades=None):
    """approach_levels of many bridge parts with one ground call (its cost is per call). grades: per part (GRADE)."""
    d = np.arange(0.0, APPROACH_REACH_M + APPROACH_FLAT_M + 1e-9, STEP_M)
    rays = []
    for xy in xys:
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        for end, nxt in (((xy[0], xy[1]), (xy[-1], xy[-2])) if len(xy) >= 2 else ()):
            u = end - nxt
            n = float(dnp.hypot(*u))
            rays.append(end + (u / n)[None, :] * d[:, None] if n > 1e-6 else None)
        if len(xy) < 2:
            rays += [None, None]
    pts = [r for r in rays if r is not None]
    z_all = np.asarray(ground(np.concatenate(pts)), np.float64).ravel() if pts else np.zeros(0)
    found, k = [], 0
    w = int(round(APPROACH_FLAT_M / STEP_M))
    for j, r in enumerate(rays):
        if r is None:
            found.append(None)
            continue
        z = z_all[k:k + len(d)]
        k += len(d)
        grade = grades[j // 2] if grades is not None else GRADE
        got = None
        if np.isfinite(z).all():
            for i in range(int(round(APPROACH_FROM_M / STEP_M)), len(d) - w):
                seg = z[i:i + w + 1]
                if seg.max() - seg.min() <= grade * APPROACH_FLAT_M + APPROACH_TOL_M:
                    level = float(np.median(seg))
                    if APPROACH_MIN_M <= level - float(z[0]) <= APPROACH_RAISE_MAX_M:
                        got = (level, float(d[i]))
                    break
        found.append(got)
    return [(found[2 * j], found[2 * j + 1]) for j in range(len(xys))]


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
    lines = [xy for *_, xy in rails]
    nodes, edges, adj = roads._chain_graph(lines)
    level = {}                                  # node -> (level, reach): the track's level beyond a bridge's end there
    decks = [n for n, (_, _, kind, _) in enumerate(rails) if kind == "bridge"]
    got_all = approach_levels_many([rails[n][3] for n in decks], ground,
                                   [LINE_GRADE.get(rails[n][1], GRADE) for n in decks]) if decks else []
    for n, ends_got in zip(decks, got_all):
        for node, got in zip(edges[n][:2], ends_got):
            if got is not None and got[0] > level.get(node, (-np.inf, 0.0))[0]:
                level[node] = got
    zb = []
    shared = {}                                 # (a double track's beds are smoothed from one profile: railxing.py)
    if railxing.ENABLED:
        gi = [n for n, (_, _, kind, _) in enumerate(rails) if kind == "ground"]
        if len(gi) > 1:
            shared = dict(zip(gi, railxing.corridor_ground([rails[n][3] for n in gi], [zgs[n] for n in gi])))
    for n, ((_, _, kind, _), zg, (a, b, s)) in enumerate(zip(rails, zgs, edges)):
        if kind != "ground":
            zb.append(zg.copy())
            continue
        lifted = shared.get(n, zg)
        for node, dist in ((a, s), (b, s[-1] - s)):     # (the track on at that level up to the bridge, not down the
            if node in level:                           # slope under it: lifted before the bed's smoothing)
                lv, reach = level[node]
                lifted = np.where(dist <= reach + STEP_M, np.maximum(lifted, lv), lifted)
        zb.append(bed_heights(lifted))
    # Read the lower road at its mouths, not on the filled railway bed above it.
    boxes = _rail_boxes(rails) if underpasses else None
    crossings = [(line, half, _underpass_masks(rails, line, half, DECK_HALF_M + STEP_M, boxes))
                 for line, half in underpasses]
    crossings = [(line, half, masks) for line, half, masks in crossings if any(m.any() for m in masks)]
    mouths = np.concatenate([ln[[0, -1]] for ln, _hw, _masks in crossings]) if crossings else np.empty((0, 2))
    mouth_z = np.asarray(ground(mouths), np.float64).reshape(-1, 2) if len(mouths) else []
    ends = {}                                   # node -> the ground parts' bed there (a bridge's ends)
    for n, ((_, _, kind, _), (a, b, _)) in enumerate(zip(rails, edges)):
        if kind == "ground":
            ends.setdefault(a, []).append(zb[n][0])
            ends.setdefault(b, []).append(zb[n][-1])
    seeds = {}                                  # node -> the deck's height at it
    for n, ((_, cls, kind, xy), (a, b, _)) in enumerate(zip(rails, edges)):
        if kind == "tunnel":                    # (straight between its portals' beds, whatever the hill above it)
            g = zgs[n]
            sx = _arc(xy)
            za = float(np.mean(ends[a])) if a in ends else float(g[0])
            zb_ = float(np.mean(ends[b])) if b in ends else float(g[-1])
            zb[n] = za + (zb_ - za) * sx / max(float(sx[-1]), 1e-9)
            continue
        if kind != "bridge":
            continue
        g = zgs[n]
        za = float(np.mean(ends[a])) if a in ends else float(g[0])
        zb_ = float(np.mean(ends[b])) if b in ends else float(g[-1])
        za = max(za, level[a][0]) if a in level else za         # (the track's level beyond its ends)
        zb_ = max(zb_, level[b][0]) if b in level else zb_
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
        # (a ground track: how far a deck stands over the track's own bed where they meet, run out at GRADE per metre
        # on top of the bed, not the deck's height itself: on a mountain line climbing 10 % (a rack railway) a deck up
        # the slope carried down at 2 % stood Andermatt's station 14 m over its own ground, GitHub #1051. On level ground
        # the two are the same ramp.)
        lifts = {node: h - float(np.mean(ends[node])) for node, h in seeds.items() if node in ends}
        lifts = {node: v for node, v in lifts.items() if v > 0.0}
        if lifts:
            ltop = max(lifts.values())
            dl = roads._dijkstra(adj, {node: (ltop - v) / GRADE for node, v in lifts.items()})
        for n, (a, b, s) in enumerate(edges):
            if rails[n][2] == "bridge":         # (two decks end to end: still a straight run between their ends)
                reach = np.maximum(top - GRADE * (d[a] + s), top - GRADE * (d[b] + s[-1] - s))
                zb[n] = _envelope(s, np.maximum(zb[n], reach))
            elif lifts:                         # a ground track follows its own bed: a distant deck carries the
                lift = np.maximum(ltop - GRADE * (dl[a] + s), ltop - GRADE * (dl[b] + s[-1] - s))     # ramps to it
                zb[n] = zb[n] + np.clip(lift, 0.0, SEED_LIFT_MAX_M)
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


SEAM_FLUSH_ON = os.environ.get("BNE_RAIL_SEAM_FLUSH", "on") != "off"


def seam_lowered(xy, zb, roads_):
    """The indices of roads_ ([(xy (k, 2), z (k,), half width)]) railroadseam.couple takes SEAM_LOWER_M lower for the
    track xy with bed zb: each road it crosses at grade (its line crossing the track's, CROSSING_DZ of the bed's rail
    top), at the bed a flush crossing gives there (_flush: the road less RAIL_TOP_M): couple took the bed to the road's
    own height, 0.55 m over the flush bed, for 25 m either side of the crossing, and the ground graded to it buried the
    ballast and sleepers there (Nachod's Kladska, 2026-11-09). Pure."""
    if not SEAM_FLUSH_ON or not roads_ or len(xy) < 2:
        return ()
    at = {id(r[0]) for r in seam_crossers(xy, zb, roads_)}
    return tuple(k for k, r in enumerate(roads_) if id(r[0]) in at)


SEAM_LOWER_M = RAIL_TOP_M - FLUSH_LIFT_M


def flush_to_plans(xy, zb, roads_):
    """The bed zb of the track xy flush (_flush) with the road plans ([(xy (k, 2), z (k,), half width)]) it crosses at
    grade, at their planned heights: grade_plans flushed it to the DEM's road, which the road's plan then lays lower
    or higher (Nachod's Kladska: 0.23 m lower, and tile_mesh, flush with the graded road, lay the ballast that much
    under the ground). Pure."""
    if not SEAM_FLUSH_ON or not roads_ or len(xy) < 2:
        return zb
    xy = np.asarray(xy, np.float64)[:, :2]
    zb = np.asarray(zb, np.float64)
    crossers = [(np.asarray(r, np.float64)[:, :2], float(h), np.asarray(z, np.float64)[:len(r)])
                for r, z, h in seam_crossers(xy, zb, roads_)]
    if not crossers:
        return zb
    blk = _blocked(xy, zb, crossers)
    if not blk.any():
        return zb
    zr = np.empty(int(blk.sum()))
    for n, p in enumerate(xy[blk]):
        best = None
        for ln, _h, z in crossers:
            d = dnp.hypot(*(ln - p).T)
            k = int(np.argmin(d))
            if best is None or d[k] < best[0]:
                best = (float(d[k]), float(z[k]))
        zr[n] = best[1]
    return _flush(_arc(xy), zb, blk, zr)


def seam_crossers(xy, zb, roads_):
    """Of roads_ ([(xy, z, half)]), those the track xy (bed zb) crosses at grade: its line crossing theirs within
    CROSSING_DZ of its rail top. Pure."""
    xy = np.asarray(xy, np.float64)[:, :2]
    zb = np.asarray(zb, np.float64)
    s = _arc(xy)
    lo, hi = xy.min(0) - 1.0, xy.max(0) + 1.0
    out = []
    lines, box = _seam_boxes(roads_)
    # (the roads whose box meets the track's, all at once: a numpy max / min per road and track was most of it)
    near = np.flatnonzero(~((box[:, 2] < lo[0]) | (box[:, 3] < lo[1]) | (box[:, 0] > hi[0]) | (box[:, 1] > hi[1])))
    for k in near.tolist():
        rxy, rz, rh = roads_[k]
        r = lines[k]
        hits = roads._crossings([xy], [r])
        rzz = np.asarray(rz, np.float64)[:len(r)]
        if hits and any(abs(float(rzz[int(np.argmin(dnp.hypot(*(r - pt).T)))])
                            - (float(np.interp(arc, s, zb)) + RAIL_TOP_M)) < CROSSING_DZ for _p, arc, pt in hits):
            out.append((rxy, rz, rh))
    return out


_SEAM_BOXES = ()        # ((the roads list, (lines, boxes)), ...): a cell's tracks ask with the same few lists


def _seam_boxes(roads_):
    """([xy (k, 2) float64] per road, boxes (R, 4) x0 y0 x1 y1 of its points; a road of < 2 points one that meets
    nothing (+inf / -inf)) of roads [(xy, z, half)] (seam_crossers', grading.rail_yield_pieces'), kept for the last few
    lists asked."""
    global _SEAM_BOXES
    for lst, got in _SEAM_BOXES:        # (one read of the tuple: another thread may replace it meanwhile)
        if lst is roads_:
            return got
    lines = [np.asarray(rxy, np.float64)[:, :2] for rxy, _rz, _rh in roads_]
    box = np.empty((len(lines), 4))
    box[:, :2], box[:, 2:] = np.inf, -np.inf
    ok = [k for k, r in enumerate(lines) if len(r) >= 2]
    if ok:
        pts = np.concatenate([lines[k] for k in ok])
        st = np.r_[0, np.cumsum([len(lines[k]) for k in ok])[:-1]]
        box[ok, :2] = np.minimum.reduceat(pts, st, axis=0)
        box[ok, 2:] = np.maximum.reduceat(pts, st, axis=0)
    got = (lines, box)
    _SEAM_BOXES = ((roads_, got),) + _SEAM_BOXES[:3]
    return got


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
    un = np.c_[ux, uy] / np.maximum(dnp.hypot(ux, uy), 1e-9)[:, None]
    zt = np.interp(ss, s, top) - SLEEPER_SINK
    return np.c_[cx, cy], np.c_[un[:, 1], -un[:, 0]], un, zt, zt + SLEEPER_H


PIER_ROAD_M = 1.5           # m a pier keeps off a road's carriageway (its sidewalks included) or a footway under the bridge
PIER_OBST_REACH_M = 60.0    # m round the tile the roads and footways the piers keep off are taken (a site moves up to
                            # 0.4 of a span along the deck: within reach of any tile that draws it)


def pier_obstacles(frame, cells, box, reach=PIER_OBST_REACH_M):
    """(a (m, 2), b (m, 2), half (m,)): the segments of every ground road (its carriageway's half width and its drawn
    sidewalks) and footway near box, what a bridge's piers keep off. The level crossings' list (crossers: the roads the
    track's own ground parts cross) has none of the roads under a bridge: a pier stood on the kerb of Caledonian Road
    under the North London line (the user's report, London 2026-11-12)."""
    near = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    seen, lines = set(), []
    for feats in cells:
        if feats is None or len(feats) == 0:
            continue
        specs = roads.road_specs(feats)
        for i in _box_hits(feats.boxes(frame), near):
            sp = specs[i]
            fid = str(feats.ids[i])
            if sp is None or fid in seen:
                continue
            seen.add(fid)
            half = sp.width / 2 + (float(sp.walk or 0.0) if roads.has_sidewalks(sp) else 0.0)
            for ring in roads.ground_parts(feats, i):
                e, n = mapgeom.enu_xy(feats, frame, ring)
                lines.append((np.column_stack((e, n)), half))
    lines += [(xy, w / 2) for xy, w in walk_ways(frame, cells, box, reach=reach)]
    a, b, h = [], [], []
    for xy, half in lines:
        xy = np.asarray(xy, np.float64)
        if len(xy) >= 2:
            a.append(xy[:-1, :2])
            b.append(xy[1:, :2])
            h.append(np.full(len(xy) - 1, float(half)))
    if not a:
        return np.zeros((0, 2)), np.zeros((0, 2)), np.zeros(0)
    return np.ascontiguousarray(np.concatenate(a)), np.ascontiguousarray(np.concatenate(b)), np.concatenate(h)


def pier_sites(s, xy, u, top, zg, i, j, obstacles, ground, every, under, min_h, pier_half):
    """The piers under clip i..j of a bridge piece (its arcs s, line xy, left unit u, deck top and ground per point):
    (centres, along, across, z0, z1) or None. Sites every `every` m from the piece's start where the deck's underside
    (top - under) stands min_h or more over the ground, each moved up to 0.4 of a span along the piece off the roads and
    footways under it (obstacles: pier_obstacles, kept PIER_ROAD_M + pier_half off their edges) or left out
    (earthfast.rail_pier_sites); each reaching PIER_FOOT_M under the lowest ground at its four corners (from the ground
    at the line's centre one hung in the air where the ground fell away to the road)."""
    import fast
    fast.require(266, "rail_pier_sites")
    oa, ob, half = obstacles
    f64 = np.float64
    c, along, across, zg_c, z1 = fast._ef.rail_pier_sites(
        np.ascontiguousarray(s, f64), np.ascontiguousarray(np.asarray(xy, f64)[:, :2]),
        np.ascontiguousarray(np.asarray(u, f64)[:, :2]), np.ascontiguousarray(top, f64),
        np.ascontiguousarray(zg, f64), int(i), int(j), float(every), float(under), float(min_h), oa, ob,
        np.ascontiguousarray(half + PIER_ROAD_M + float(np.max(pier_half)), f64))
    if not len(c):
        return None
    pa, pw = pier_half
    corners = np.concatenate([c + sa * along * pa + sc * across * pw for sa in (-1, 1) for sc in (-1, 1)])
    zc = np.asarray(ground(corners), np.float64).reshape(4, len(c)).min(axis=0)
    return c, along, across, np.minimum(zg_c, zc) - PIER_FOOT_M, z1


def _ballast_bed(mesh, p, uu, t, v, foot_l, foot_r, zl, zr, mat, tile=3.0):
    up = np.array([0.0, 0.0, 1.0])
    v = v * 3.0 / tile                  # (v: arc / 3 m)
    out_l = np.c_[uu, np.zeros(len(p))]
    L = np.c_[p + uu * BALLAST_TOP_M, t]
    R = np.c_[p - uu * BALLAST_TOP_M, t]
    Lf = np.c_[p + uu * foot_l[:, None], np.minimum(zl, t) - BALLAST_FOOT_M]
    Rf = np.c_[p - uu * foot_r[:, None], np.minimum(zr, t) - BALLAST_FOOT_M]
    mesh.strip(L, R, 0.0, 2 * BALLAST_TOP_M / tile, v, mat, up)
    mesh.strip(L, Lf, 0.0, 0.8 * 3.0 / tile, v, mat, out_l + up)
    mesh.strip(R, Rf, 0.0, 0.8 * 3.0 / tile, v, mat, -out_l + up)


def _vault():
    """The rail tunnel's section: [(x across from the axis, z over the ballast top)] from the floor on the left wall up,
    over the vault and down the right wall, as one line."""
    th = np.linspace(np.pi, 0.0, TUNNEL_SECTION_N)
    arch = [(TUNNEL_HALF_M * dmath.cos(a), TUNNEL_WALL_H + TUNNEL_RISE_M * dmath.sin(a)) for a in th]
    return [(-TUNNEL_HALF_M, -TUNNEL_FLOOR_M)] + arch + [(TUNNEL_HALF_M, -TUNNEL_FLOOR_M)]


def _tunnel(mesh, p, uu, t, v, ballast, concrete, start, end):
    """A rail tunnel's run (points p, left unit uu, ballast top t, arc v): flat ballast between the walls and the tube round
    it, faced inwards, concrete; start / end: the run's end is a portal (a face round the opening, seen from outside)."""
    k = len(p)
    sec = _vault()
    flat = np.full(k, TUNNEL_HALF_M - 0.3)
    _ballast_bed(mesh, p, uu, t, v, flat, flat, t - BALLAST_H - TUNNEL_FLOOR_M, t - BALLAST_H - TUNNEL_FLOOR_M, ballast)
    rows = [np.c_[p + uu * x, t + z] for x, z in sec]
    for m in range(len(sec) - 1):
        mid = (np.asarray(sec[m]) + np.asarray(sec[m + 1])) / 2
        inward = np.c_[-uu * mid[0], np.full(k, 2.0 - mid[1])]
        mesh.strip(rows[m], rows[m + 1], 0.0, 0.3, v, concrete, inward)
    d = np.gradient(p, axis=0)
    d /= np.maximum(dnp.hypot(d[:, 0], d[:, 1]), 1e-9)[:, None]
    for at, going, on in ((0, -1.0, start), (-1, 1.0, end)):
        if not on:
            continue
        inner = np.array([np.r_[p[at] + uu[at] * x, t[at] + z] for x, z in sec])
        outer = np.array([np.r_[p[at] + uu[at] * (x + math.copysign(PORTAL_REACH_M, x) * (abs(x) > 0.1)),
                                t[at] + z + (PORTAL_REACH_M if z > -TUNNEL_FLOOR_M + 0.01 else 0.0)] for x, z in sec])
        out_dir = np.r_[d[at] * going, 0.0]
        mesh.strip(inner, outer, 0.0, 0.3, np.zeros(len(sec)), concrete, out_dir)
        mesh.strip(outer, inner, 0.0, 0.3, np.zeros(len(sec)), concrete, -out_dir)


def tunnel_hole_parts(frame, load, elev, box):
    """[(fid, xy (k, 2), floor z (k,))] of the rail tunnels near box (in the frame's heights, the beds of tile_mesh: the
    same lattice), for the terrain's portal holes (tunnel_hole_mask); [] unless BNE_RAIL_TUNNELS. Pure given its inputs."""
    if not TUNNELS_ON:
        return []
    cells = [load(k) for k in cells_near(frame, box, 60.0)]
    rails = parts(frame, cells, box, reach=60.0, clip=True)
    if not any(kind == "tunnel" for _f, _c, kind, _xy in rails):
        return []
    ground = point_ground(frame, elev, ("holes", round(float(frame.lat0), 7), round(float(frame.lon0), 7)))
    out = []
    for (fid, _c, kind, xy), (zb, _zg) in zip(rails, _beds(frame, rails, ground, [], load, elev)):
        if kind == "tunnel" and len(xy) >= 2:
            out.append((str(fid), xy, np.asarray(zb, np.float64) + BALLAST_H - TUNNEL_FLOOR_M))
    return out


def tunnel_hole_mask(parts_, u, cx, cy, square):
    """Where the terrain block (heights u [iy, ix], as roads.tunnel_hole_mask) must be a hole for the rail tunnels' portals."""
    import fast
    import roads as roadsmod
    n = u.shape[0]
    half = n * square / 2
    mask = np.zeros(u.shape, bool)
    if not parts_:
        return mask
    fast.require(88, "tunnel_holes")
    u = np.ascontiguousarray(u, np.float64)
    th = TUNNEL_WALL_H + TUNNEL_RISE_M + TUNNEL_FLOOR_M
    for _fid, xy, z in parts_:
        fast._ef.tunnel_holes(mask, u, float(cx - half), float(cy - half), float(square), np.ascontiguousarray(xy, np.float64),
                              np.ascontiguousarray(z, np.float64), float(TUNNEL_HALF_M + 0.4), float(th), False, True, True,
                              float(roadsmod.TUNNEL_HOLE_MARGIN), float(roadsmod.LOW_HOLE_M + roadsmod.LOW_HOLE_STEP),
                              float(roadsmod.ARCH_SIDE_M + 1.0), True)
    return mask


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


def _tram_deck(mesh, p, uu, t, v, concrete):
    """An embedded tram track's concrete slab, not a mainline ballast trough.
    t is the common ballast-top datum; keep its underside identical to _deck
    so grading and pier placement consume the same structural depth."""
    up = np.array([0.0, 0.0, 1.0])
    out_l = np.c_[uu, np.zeros(len(p))]
    left, right = p + uu * TRAM_DECK_HALF_M, p - uu * TRAM_DECK_HALF_M
    top, bottom = t - BALLAST_H + FLUSH_LIFT_M, t - DECK_D
    mesh.strip(np.c_[left, top], np.c_[right, top], 0.0, 0.9, v, concrete, up)
    for edge, normal in ((left, out_l), (right, -out_l)):
        mesh.strip(np.c_[edge, top], np.c_[edge, bottom], 0.0, 0.5, v, concrete, normal)
    mesh.strip(np.c_[left, bottom], np.c_[right, bottom], 0.0, 0.9, v, concrete, -up)


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


def crossing_props(frame, tx, ty, cells, ground, left_hand=False, kits=None, country=None):
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
    roads += _node_ways(frame, cells, near, roads)
    if not roads:
        return []
    americas = float(frame.lon0 if hasattr(frame, "lon0") else 0.0) < AMERICAS_LON
    if railxing.ENABLED:        # (a crossing of several tracks is one: railxing.py, BNE_RAIL_XING)
        usage = {str(f.ids[i]): str(u) for f in cells if f is not None and len(f)
                 for u, i in ((f.attrs.get("osm_usage", [None] * len(f))[i], i) for i in range(len(f))) if u}
        return railxing.crossing_rows(box, rails, roads, ground, worn=worn, usage=usage,
                                      nodes=railxing.node_lookup(frame, cells), fra=kits, country=country,
                                      lon=float(frame.lon0 if hasattr(frame, "lon0") else 0.0), left_hand=left_hand,
                                      owns=lambda x, y: roadsmod.box_owns(box, x, y))
    osm_kit = _osm_kits(frame, cells)
    rows, done = [], set()
    for (rfid, sp, line), crs in zip(roads, roadsmod._crossings_by([xy for _f, xy, _c in rails],
                                                                     [ln for *_, ln in roads])):
        for ri, _arc_r, pt in crs:
            pt = np.asarray(pt, np.float64)
            if not roadsmod.box_owns(box, pt[0], pt[1]):
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


NODE_WAY_M = 10.0           # m: a footway, path or pedestrian way passing this near an OSM level crossing node is its road
NODE_WAY_MIN_W = 4.0        # m: the least width the devices are set for there (a street mapped as a pedestrian way)


def _node_ways(frame, cells, near, roads_):
    """[(fid, spec, xy)] of the footways, paths, cycleways and pedestrian ways that an OSM level crossing node
    (railway=level_crossing) lies on, as roads for crossing_props: Nachod's Kladska, a street with gates and lights, is
    mapped there as a pedestrian way (no road profile, so no devices at all), and the node says it is a level crossing. None
    where a road with a profile already passes within NODE_WAY_M of the node. Pure."""
    import types
    nodes = []
    for f in cells:
        for lo, la, t in (f.__dict__.get("_rail_nodes", ()) if f is not None else ()):
            if t.get("railway") == "level_crossing":
                nodes.append((lo, la))
    if not nodes:
        return []
    e, n, _ = frame.geodetic_to_enu(np.array([q[1] for q in nodes]), np.array([q[0] for q in nodes]), 0.0)
    pts = np.c_[np.asarray(e, np.float64), np.asarray(n, np.float64)]
    pts = pts[(pts[:, 0] >= near[0]) & (pts[:, 0] <= near[2]) & (pts[:, 1] >= near[1]) & (pts[:, 1] <= near[3])]
    if not len(pts):
        return []
    served = [p for p in pts if not any(float(np.min(dnp.hypot(*(roads.densify(ln, 2.0)[:, :2] - p).T))) < NODE_WAY_M
                                        for _f, _s, ln in roads_)]
    out = []
    for xy, w in walk_ways(frame, cells, (near[0] + 60, near[1] + 60, near[2] - 60, near[3] - 60), 60.0):
        dense = roads.densify(xy, 2.0)                  # (a way's few vertices: the distance is to its line)
        if any(float(np.min(dnp.hypot(*(dense[:, :2] - p).T))) < NODE_WAY_M for p in served):
            out.append(("node-way", types.SimpleNamespace(width=max(w, NODE_WAY_MIN_W), cls="service", oneway=False, dir=None),
                        xy))
    return out


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
        d = dnp.hypot(*(xy - np.asarray(p)[:2]).T)
        j = int(np.argmin(d))
        return kit[j] if d[j] <= OSM_KIT_M else None
    return look


def _unit_at(line, pt):
    """The unit direction of polyline line at its segment nearest pt."""
    a, b = line[:-1], line[1:]
    d = b - a
    t = np.clip(((pt - a) * d).sum(1) / np.maximum((d * d).sum(1), 1e-12), 0.0, 1.0)
    j = int(np.argmin(dnp.hypot(*(a + t[:, None] * d - pt).T)))
    return d[j] / max(float(dnp.hypot(*d[j])), 1e-9)


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
                    round(math.degrees(dmath.atan2(side[1], side[0])) % 360.0, 1), 1.0, CROSSING_HEIGHT[kit], 0.0])
    return out


BUFFER_JOIN_M = 2.3         # m: another track's point this near an end makes it a joint or a switch, not a dead end
BUFFER_BACK_M = 1.0         # m the buffer stop stands back from the end of its track
BUFFER_HEIGHT = 1.8         # m: its model's vertical size (tools/build_rail_crossing_props.py)
WALK_CLASSES = {"footway": 2.0, "path": 1.5, "cycleway": 2.5, "pedestrian": 3.0}    # class -> the way's width (m)
WALK_NODE_W = 2.0           # m: the width of a crossing OSM only has a node for
WALK_NODE_JOIN_M = 6.0      # m: a node this near a crossing already found is that one
WALK_NODE_ON_M = 2.5        # m: a node this near a track's line is on it
WALK_SIGN_BEFORE_M = 1.0    # m from the ballast's foot to a walk sign
WALK_SIGN_OFF_M = 0.5       # m beside the way's edge
WALK_SIGN_HEIGHT = 2.52     # m: the sign's model's vertical size
WALK_GROUP_M = 14.0         # m along a way between two tracks' crossings of one place (a double track, a yard's throat)
WALK_SIGN_CLEAR_M = 2.6     # m a walk sign keeps from every track's axis (the ballast's foot)
WALK_STREET_W = 6.5         # m: the panels' width where a street mapped as a pedestrian way crosses (its level crossing node)
WALK_NODE_WAY_M = 10.0      # m from an OSM level_crossing node to its way
WALK_PANEL_MAX_HX = 4.0     # m: a footway panel's half length along the track (a street's: more, WALK_STREET_W)
WALK_MIN_SIN = 0.35         # a way crossing the track flatter than this (20 degrees) is left alone
WALK_PANEL_OUT_M = 0.45     # m: half the panel outside each rail (it is 0.9 m wide)
WALK_PANEL_GAP_M = 0.07     # m left between the middle panel and each rail (the flangeway)
STATION_FURNITURE_ON = os.environ.get("BNE_RAIL_FURNITURE", "on") != "off"       # buffer stops at the ends of tracks, footway crossings over them
TRUE_CROSSERS_ON = os.environ.get("BNE_RAIL_TRUE_CROSSINGS", "on") != "off"   # a road that only ends at a track or runs beside it cuts nothing


def true_crossers(rail_lines, cr):
    """cr ([(xy, half width)]) without the roads that only touch the track or run beside it (a service road ending at the
    rail, a street along it): a carriageway cuts the track where its centre line crosses the track's. rail_lines: the
    track parts' xy. Pure."""
    if not cr or not rail_lines:
        return cr
    out = []
    for line, hw in cr:
        if len(line) < 2:
            continue
        if roads._crossings(rail_lines, [line]):
            out.append((line, hw))
    return out


def walk_ways(frame, cells, box, reach=60.0):
    """[(xy (k, 2), width)]: the footways, paths, cycleways and pedestrian ways on the ground near box (the ways a person
    walks across a track by). Pure."""
    near = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    seen, out = set(), []
    for feats in cells:
        if feats is None or len(feats) == 0 or "subtype" not in feats.attrs or "class" not in feats.attrs:
            continue
        sub, cls = feats.attrs["subtype"], feats.attrs["class"]
        fb = feats.boxes(frame)
        for i in range(len(feats)):
            w = WALK_CLASSES.get(str(cls[i])) if str(sub[i]) == "road" else None
            fid = str(feats.ids[i])
            if w is None or fid in seen or not _box_hit(fb[i], near):
                continue
            seen.add(fid)
            for ring in roads.ground_parts(feats, i):
                e, n = mapgeom.enu_xy(feats, frame, ring)
                if len(e) >= 2:
                    out.append((np.c_[e, n], w))
    return out


WALK_STRICT_ON = os.environ.get("BNE_RAIL_WALK_STRICT", "on") not in ("off", "0", "false")
WALK_STRICT_SIN = 0.6       # a way crossing a track flatter than this (37 deg) is running along it, not across (plan 040:
                            # Kalynivka's path 20 deg to the line got panels 8 m long and a row of signs)
WALK_LEAVE_M = 12.0         # m along the way either side of the crossing ...
WALK_LEAVE_OFF_M = 5.0      # ... it must be this far from the track's line (it leaves the track on both sides)


def _leaves_track(way, pt, track):
    """Does the way (k, 2) go away from the track line (n, 2) on both sides of its crossing pt: its points WALK_LEAVE_M
    along it either side lie WALK_LEAVE_OFF_M or more off the track? A way that ends before (a short stub) counts by
    its end. Pure."""
    way = np.asarray(way, np.float64)
    s = _arc(way)
    ab = np.diff(way, axis=0)
    t = np.clip(((pt - way[:-1]) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
    j = int(np.argmin(dnp.norm(way[:-1] + t[:, None] * ab - pt, axis=1)))
    at = float(s[j] + t[j] * (s[j + 1] - s[j]))
    dense = roads.densify(track, 1.0)[:, :2]
    for sgn in (-1.0, 1.0):
        a = float(np.clip(at + sgn * WALK_LEAVE_M, 0.0, s[-1]))
        q = np.array([np.interp(a, s, way[:, 0]), np.interp(a, s, way[:, 1])])
        if float(np.min(dnp.hypot(*(dense - q).T))) < WALK_LEAVE_OFF_M:
            return False
    return True


def walk_crossings(rails, ways, nodes, carriage, ex):
    """The places a person crosses a ground track: [(point (2,), arc m, track part index, track unit t, way unit f, way
    width)]. Where a footway, path, cycleway or pedestrian way (ways: walk_ways) crosses a track at WALK_MIN_SIN or more, and
    where OSM has a railway=crossing node on a track (nodes: [(x, y)]) with no mapped way to it: the way then is taken square
    to the track, WALK_NODE_W wide (a crossing that leads nowhere on the map is still one). None beside a road's deck (the
    sidewalk goes over it with the road: carriage [(xy, half width)]). Only the points inside ex. Pure."""
    out = []
    for n_part, (fid, cls, kind, xy) in enumerate(rails):
        if kind != "ground" or cls == "tram" or len(xy) < 2:
            continue
        lo, hi = xy.min(0) - 8.0, xy.max(0) + 8.0
        s = _arc(xy)
        for way, ww in ways:
            if (way.max(0) < lo).any() or (way.min(0) > hi).any():
                continue
            for _pi, arc, pt in roads._crossings([xy], [way]):
                if not (ex[0] <= pt[0] <= ex[2] and ex[1] <= pt[1] <= ex[3]):
                    continue
                k = int(np.clip(np.searchsorted(s, arc), 1, len(xy) - 1))
                t = xy[k] - xy[k - 1]
                t = t / max(float(dnp.hypot(*t)), 1e-9)
                d = dnp.hypot(*(way[1:] - way[:-1]).T)
                j = int(np.argmin(dnp.hypot(*((way[:-1] + way[1:]) / 2 - pt).T)))
                f = (way[j + 1] - way[j]) / max(float(d[j]), 1e-9)
                if abs(t[0] * f[1] - t[1] * f[0]) < (WALK_STRICT_SIN if WALK_STRICT_ON else WALK_MIN_SIN):
                    continue
                if any(float(np.min(dnp.hypot(*(ln - pt).T))) < hw + 2.0 for ln, hw in carriage):
                    continue
                if WALK_STRICT_ON and not _leaves_track(way, pt, xy):
                    continue                    # (a way that runs along the track: no crossing, plan 040)
                out.append((np.asarray(pt, np.float64), float(arc), n_part, t, f, float(ww)))
    for x, y in nodes:
        if not (ex[0] <= x <= ex[2] and ex[1] <= y <= ex[3]):
            continue
        if any(math.hypot(x - c[0][0], y - c[0][1]) < WALK_NODE_JOIN_M for c in out):
            continue                            # (a way already crosses here)
        best = None
        for n_part, (fid, cls, kind, xy) in enumerate(rails):
            if kind != "ground" or cls == "tram" or len(xy) < 2:
                continue
            dd = dnp.hypot(xy[:, 0] - x, xy[:, 1] - y)
            k = int(np.argmin(dd))
            if dd[k] < WALK_NODE_ON_M and (best is None or dd[k] < best[0]):
                best = (float(dd[k]), n_part, k)
        if best is None:
            continue
        _d, n_part, k = best
        xy = rails[n_part][3]
        s = _arc(xy)
        t = xy[min(k + 1, len(xy) - 1)] - xy[max(k - 1, 0)]
        t = t / max(float(dnp.hypot(*t)), 1e-9)
        pt = xy[k]
        if any(float(np.min(dnp.hypot(*(ln - pt).T))) < hw + 2.0 for ln, hw in carriage):
            continue
        out.append((np.asarray(pt, np.float64), float(s[k]), n_part, t, np.array([-t[1], t[0]]), WALK_NODE_W))
    return out


def walk_groups(crossings, rails):
    """The crossings (walk_crossings) of one way over neighbouring tracks as one place: [[crossing, ...]] each sorted along
    the way. Crossings of the same way (its unit f and width) within WALK_GROUP_M of each other along it are one group."""
    groups = []
    for c in sorted(crossings, key=lambda c: (round(float(c[4][0]), 2), round(float(c[4][1]), 2), float(c[0][0]),
                                               float(c[0][1]))):
        for g in groups:
            h = g[-1]
            if abs(float(dnp.dot(h[4], c[4]))) > 0.97 and abs(h[5] - c[5]) < 0.01 and \
                    min(float(dnp.hypot(*(q[0] - c[0]))) for q in g) < WALK_GROUP_M:
                g.append(c)
                break
        else:
            groups.append([c])
    out = []
    for g in groups:
        f = g[0][4]
        out.append(sorted(g, key=lambda c: float(dnp.dot(c[0], f))))
    return out


def walk_panel_boxes(crossings, finals, rails=None):
    """The boxes of footway crossings (walk_crossings): [(centres (1, 2), along (1, 2), across (1, 2), z0 (1,), z1 (1,)),
    hx, hy] for the panels between and outside the rails, flush with their heads, with a flangeway beside each rail, and
    between two neighbouring tracks of one crossing a panel over the gap (the ballasts' sides dipped there: a ditch across
    the street, Nachod's Kladska 2026-11-09)."""
    out = []
    half_g = GAUGE_M / 2
    edge = half_g + RAIL_HEAD + 2 * WALK_PANEL_OUT_M          # (a track's panels' outer edge from its axis)
    for g in walk_groups(crossings, rails):
        for pt, arc, n_part, t, f, ww in g:
            zb = finals[n_part]
            sin = max(abs(float(t[0] * f[1] - t[1] * f[0])), WALK_MIN_SIN)
            z_bed = float(zb[min(int(round(arc / STEP_M)), len(zb) - 1)])
            z = z_bed + RAIL_TOP_M - 0.012
            z0 = z_bed + BALLAST_H - 0.15
            n = np.array([-t[1], t[0]])
            hx = min(max(WALK_PANEL_MAX_HX, ww) if WALK_STRICT_ON else 8.0, (ww / 2) / sin + 0.2)
            ctr = lambda off: (pt + n * off)[None, :]
            a, b = t[None, :], n[None, :]
            out.append(((ctr(0.0), a, b, np.array([z0]), np.array([z])), hx, half_g - WALK_PANEL_GAP_M))
            for sg in (1, -1):
                out.append(((ctr(sg * (half_g + RAIL_HEAD + WALK_PANEL_OUT_M)), a, b, np.array([z0]), np.array([z])),
                            hx, WALK_PANEL_OUT_M))
        for c0, c1 in zip(g, g[1:]):            # (the gap between two tracks' panels, along the way)
            f = c0[4]
            d = float(dnp.dot(c1[0] - c0[0], f))
            sin0 = max(abs(float(c0[3][0] * f[1] - c0[3][1] * f[0])), WALK_MIN_SIN)
            sin1 = max(abs(float(c1[3][0] * f[1] - c1[3][1] * f[0])), WALK_MIN_SIN)
            gap0, gap1 = edge / sin0, edge / sin1
            if d - gap0 - gap1 < 0.05 or d > WALK_GROUP_M:
                continue
            mid = c0[0] + f * (gap0 + (d - gap0 - gap1) / 2)
            zs = []
            for pt, arc, n_part, _t, _f, _w in (c0, c1):
                zb = finals[n_part]
                zs.append(float(zb[min(int(round(arc / STEP_M)), len(zb) - 1)]) + RAIL_TOP_M - 0.012)
            z = (zs[0] + zs[1]) / 2
            nf = np.array([-f[1], f[0]])
            out.append(((mid[None, :], nf[None, :], f[None, :], np.array([min(zs) - 0.45]), np.array([z])),
                        c0[5] / 2 + 0.2, (d - gap0 - gap1) / 2 + 0.02))
    return out


def walk_sign_rows(crossings, ground, finals=None, rails=None, streets=()):
    """Prop rows ([rrwalk, x, y, z, yaw, scale, height, 0]) of the warning signs at footway crossings: one at each end of
    a crossing of one or more neighbouring tracks (walk_groups), facing the people coming, on their right, past the outer
    track's ballast, never on another track's bed (WALK_SIGN_CLEAR_M from every track's axis). streets: the crossing
    points of streets mapped as pedestrian ways (an OSM level crossing node: their gates are the crossing's devices) get
    none."""
    pts, floors = [], []
    lines = [xy for _f, _c, _k, xy in (rails or [])]
    for g in walk_groups(crossings, rails):
        if any(min(float(dnp.hypot(*(c[0] - q))) for c in g) < WALK_NODE_WAY_M for q in streets):
            continue
        for sgn, (pt, arc, n_part, t, f, ww) in ((1.0, g[0]), (-1.0, g[-1])):
            sin = max(abs(float(t[0] * f[1] - t[1] * f[0])), WALK_MIN_SIN)
            before = min(6.0, (BALLAST_TOP_M + BALLAST_SLOPE * BALLAST_H + WALK_SIGN_BEFORE_M) / sin)
            floor = -np.inf
            if finals is not None:
                zb = finals[n_part]
                floor = (float(zb[min(int(round(arc / STEP_M)), len(zb) - 1)]) + BALLAST_H
                         - max(0.0, before * sin - BALLAST_TOP_M) / BALLAST_SLOPE)
            a = f * sgn                         # the way they walk, towards the crossing (from before its first track)
            side = np.array([a[1], -a[0]])      # its right
            q = pt - a * before + side * (ww / 2 + WALK_SIGN_OFF_M)
            if any(len(ln) >= 2 and float(np.min(dnp.hypot(*(roads.densify(ln, 1.0)[:, :2] - q).T))) < WALK_SIGN_CLEAR_M
                   for ln in lines):
                continue                        # (on a track's bed: no sign)
            pts.append((q, side))
            floors.append(floor)
    if not pts:
        return []
    z = np.maximum(np.asarray(ground(np.array([q for q, _ in pts])), np.float64).ravel(), np.asarray(floors))
    return [["rrwalk", round(float(q[0]), 2), round(float(q[1]), 2), round(float(zz), 2),
             round(math.degrees(dmath.atan2(side[1], side[0])) % 360.0, 1), 1.0, WALK_SIGN_HEIGHT, 0.0]
            for (q, side), zz in zip(pts, z)]


def dead_ends(rails, finals, worn, near):
    """The true ends of the tracks: [(end point (2,), unit direction of travel into it (2,), rail head height)]. A true end
    is the end of a ground part no other part joins and none passes within BUFFER_JOIN_M, well inside the window's reach
    (a clipped end is not one); abandoned lines (worn 2) and trams have none. Pure."""
    if not rails:
        return []
    nodes, edges, _adj = roads._chain_graph([xy for *_x, xy in rails])
    deg = np.zeros(len(nodes), np.int64)
    for a, b, _s in edges:
        deg[a] += 1
        deg[b] += 1
    inner = (near[0] + 3 * STEP_M, near[1] + 3 * STEP_M, near[2] - 3 * STEP_M, near[3] - 3 * STEP_M)
    out = []
    for n_part, ((fid, cls, kind, xy), zb, (a, b, _s)) in enumerate(zip(rails, finals, edges)):
        if kind != "ground" or cls == "tram" or worn.get(fid, 0) >= 2 or a == b or len(xy) < 4:
            continue
        for node, i, j in ((a, 0, 2), (b, len(xy) - 1, len(xy) - 3)):
            if deg[node] != 1:
                continue
            p = xy[i]
            if not (inner[0] <= p[0] <= inner[2] and inner[1] <= p[1] <= inner[3]):
                continue
            if any(float(np.min(dnp.hypot(*(oxy - p).T))) < BUFFER_JOIN_M
                   for o, (*_x, oxy) in enumerate(rails) if o != n_part and oxy is not xy):
                continue                        # (a track starting on another one's line: a switch, not an end)
            d = xy[i] - xy[j]
            out.append((p, d / max(float(dnp.hypot(*d)), 1e-9), float(zb[i]) + RAIL_TOP_M))
    return out


def buffer_rows(ends):
    """Prop rows ([rrbuffer, x, y, z, yaw, scale, height, 0]) of the buffer stops: the model's face towards the train, its
    block BUFFER_BACK_M before the track's end (the model's +Y is the direction the train runs, its +X the right of it)."""
    out = []
    for p, d, z in ends:
        at = p - d * BUFFER_BACK_M
        side = np.array([d[1], -d[0]])
        out.append(["rrbuffer", round(float(at[0]), 2), round(float(at[1]), 2), round(z, 2),
                    round(math.degrees(dmath.atan2(side[1], side[0])) % 360.0, 1), 1.0, BUFFER_HEIGHT, 0.0])
    return out


SWITCH_M = 1.7              # m: a track point this near another track's line, crossing it at SWITCH_MIN_SIN or more, is a
SWITCH_MIN_SIN = 0.087      # switch or a crossing: no collider box there (the flange of the through track would strike the side
                            # track's rail; a real switch's blades and frog leave the gap)


def _switch_masks(rails):
    """Per part: bool (n,), the points inside a switch (SWITCH_M from another part's line, crossing it at SWITCH_MIN_SIN or
    more; parallel and duplicated lines are left alone). Pure, order of rails alone."""
    cell = SWITCH_M
    grid = {}
    dirs = []
    for n, (_f, c, _k, xy) in enumerate(rails):
        d = np.gradient(xy, axis=0)
        d /= np.maximum(dnp.hypot(d[:, 0], d[:, 1]), 1e-9)[:, None]
        dirs.append(d)
        if c == "tram":
            continue
        for i, (x, y) in enumerate(np.floor(xy / cell).astype(np.int64).tolist()):
            grid.setdefault((x, y), []).append((n, i))
    out = []
    for n, (_f, c, _k, xy) in enumerate(rails):
        m = np.zeros(len(xy), bool)
        if c != "tram":
            for i, (x, y) in enumerate(np.floor(xy / cell).astype(np.int64).tolist()):
                for gx in (x - 1, x, x + 1):
                    for gy in (y - 1, y, y + 1):
                        for k, j in grid.get((gx, gy), ()):
                            if k == n:
                                continue
                            o = rails[k][3][j]
                            if math.hypot(o[0] - xy[i, 0], o[1] - xy[i, 1]) < SWITCH_M and                                     abs(dirs[n][i, 0] * dirs[k][j, 1] - dirs[n][i, 1] * dirs[k][j, 0]) >= SWITCH_MIN_SIN:
                                m[i] = True
        out.append(m)
    return out


def _collider(mesh, p, uu, t, v, half_g, up):
    """The rails' physics boxes along a run (points p, left unit uu, ballast top t, arc v): per rail a top, an inner face at
    exactly the gauge and an outer face COLLIDER_W further out, from COLLIDER_DOWN under the ballast top to the rail's top."""
    coll = mesh.mat(COLLIDER)
    zt = t - SLEEPER_SINK + SLEEPER_H + RAIL_H
    zl = t - COLLIDER_DOWN
    out_l = np.c_[uu, np.zeros(len(p))]
    gin = half_g - RAIL_HEAD / 2
    for sg in (1, -1):
        ci, co = p + uu * sg * gin, p + uu * sg * (gin + COLLIDER_W)
        mesh.strip(np.c_[ci, zt], np.c_[co, zt], 0.0, 0.02, v, coll, up)
        mesh.strip(np.c_[ci, zl], np.c_[ci, zt], 0.0, 0.05, v, coll, -sg * out_l)
        mesh.strip(np.c_[co, zl], np.c_[co, zt], 0.0, 0.05, v, coll, sg * out_l)


def _foot_limits(xy, side, foot, others, crossers):
    """Per vertex: the ballast foot's width, clipped at a road's carriageway edge or another track's deck so a fill's
    side is a wall there, not a slope over the road/track (the wide double-track embankment's foot ran over the road
    under it and the line beside it: a hollow overhang the player could drive into)."""
    p = np.asarray(xy, np.float64)[:, :2]
    sd = np.asarray(side, np.float64)
    lim = np.asarray(foot, np.float64).copy()

    def clip(line, extra):
        line = np.asarray(line, np.float64)[:, :2]
        if len(line) < 2:
            return
        a, b = line[:-1], line[1:]
        ab = b - a
        ab2 = np.maximum((ab * ab).sum(1), 1e-9)
        t = np.clip(((p[:, None, :] - a[None]) * ab[None]).sum(2) / ab2[None], 0.0, 1.0)
        q = a[None] + t[:, :, None] * ab[None]
        d = dnp.hypot(q[:, :, 0] - p[:, None, 0], q[:, :, 1] - p[:, None, 1])
        k = np.argmin(d, axis=1)
        near = d[np.arange(len(p)), k]
        on = ((q[np.arange(len(p)), k] - p) * sd).sum(1) > 0.0
        edge = near - extra
        ok = on & (edge > 0.0) & (edge < lim)
        lim[ok] = edge[ok]

    for line, half, _z in crossers:
        clip(line, float(half) + ROAD_FOOT_CLEAR_M)
    for _fid, line in others:
        clip(line, DECK_HALF_M + RAIL_FOOT_CLEAR_M)
    return np.maximum(lim, BALLAST_TOP_M + 0.01)


def tile_mesh(frame, tx, ty, cells, ground, sign_style=None, far_ground=None, return_decks=False, load=None,
              bed_elev=None, _drop=frozenset(), furniture=None, under=None):
    """The road tile (tx, ty)'s railway mesh (roadmesh.write's (positions, tris, uvs, materials, tri material)) or
    None. cells: the roads layers of the cells within REACH_M of the tile (cells_of_tile(..., margin=REACH_M));
    ground(xy (n, 2)) -> z: the drawn ground in the frame. far_ground: the natural ground, read beyond GRADED_NEAR_M
    of the tile (the lines REACH_M round it only smooth the track and carry its ramps: the graded ground there made the
    grader plan the cells round every tile, and the railways doubled a tile's time, 2026-10-25). return_decks: also
    return the bridge decks' centrelines ([{"n": xy+z line, "w": width}], the bed plus its ballast), for mapped
    furniture (fences.py) to stand on: the profile is already made here, no second pass. under(xy (k, 2)) -> mask: the
    points a 3dmr model bridge stands over (buildings.bridge_footprints'): a railway's or a walkway's bridge mostly
    under one is not drawn (the model carries it: Perth's Narrows and Mount Henry bridges' rail decks, the Elizabeth
    Quay footbridge, GitHub #1071); the track's profile still runs over it.
    load (a roads layer by cell key) and bed_elev (the grader's DEM): the beds from railprofile's lattice, the same in
    every tile (railprofile.py)."""
    cells = list(cells)
    box = roads.tile_bbox(tx, ty)
    tunnels = road_underpasses(frame, cells, box)
    ground = _mixed(ground, far_ground, box)
    ex = (box[0] - roads.OVERLAP, box[1] - roads.OVERLAP, box[2] + roads.OVERLAP, box[3] + roads.OVERLAP)
    worn, gauges = {}, {}
    rails = parts(frame, cells, box, clip=True, worn=worn, gauges=gauges, underpasses=tunnels)
    walk = footbridges.parts(frame, cells, box) if FOOTBRIDGES_ON else []    # (and the walkways' bridges: GitHub #905)
    exs = (ex[0] - STEP_M, ex[1] - STEP_M, ex[2] + STEP_M, ex[3] + STEP_M)
    if not any(_box_hit((xy[:, 0].min(), xy[:, 1].min(), xy[:, 0].max(), xy[:, 1].max()), exs) for *_, xy in rails):
        rails = []
    walk = [w for w in walk if _box_hit((w[1][:, 0].min(), w[1][:, 1].min(), w[1][:, 0].max(), w[1][:, 1].max()), exs)]
    if under is not None:
        walk = [w for w in walk if not np.asarray(under(w[1]), bool).mean() >= 0.5]
    if not rails and not walk:
        return (None, []) if return_decks else None
    prof = (_beds(frame, rails, ground, tunnels + coarse_underpasses(frame, cells, box, rails), load, bed_elev)
            if rails else [])
    keep = [n for n in range(len(rails)) if n not in _drop]
    if _drop:                                       # (a retry past the vertex budget: the tracks of lowest rank left out)
        rails, prof = [rails[n] for n in keep], [prof[n] for n in keep]
    carried = [under is not None and kind == "bridge" and np.asarray(under(xy), bool).mean() >= 0.5
               for _f, _c, kind, xy in rails]
    decks = ([{"n": np.column_stack((xy, zb + BALLAST_H)),
               "w": 2 * (TRAM_DECK_HALF_M if cls == "tram" else DECK_HALF_M)}
              for (_, cls, kind, xy), (zb, _zg), gone in zip(rails, prof, carried) if kind == "bridge" and not gone]
             if return_decks else [])
    ground_lines = [xy for _f, c, kind, xy in rails if kind == "ground" and c != "tram"]
    all_cr = crossings(frame, cells, box, reach=50.0)
    cr = (true_crossers(ground_lines, all_cr) if TRUE_CROSSERS_ON else all_cr) + tunnels
    crossers = []
    if cr:                                          # (one ground call for all of them: its cost is per call)
        zc = np.asarray(ground(np.concatenate([ln for ln, _ in cr])), np.float64)
        k = 0
        for line, hw in cr:
            crossers.append((line, hw, zc[k:k + len(line)]))
            k += len(line)
    switch = _switch_masks(rails)
    mesh = _Mesh((box[0], box[1], 0.0))
    ballast, concrete, steel = mesh.mat(BALLAST), mesh.mat(CONCRETE), mesh.mat(STEEL)
    up = np.array([0.0, 0.0, 1.0])
    small = []                                      # sleepers and piers: drawn last, as room allows
    obstacles = None                                # (pier_obstacles: made when a deck needs them)
    finals = [z for z, _g in prof]                  # (each part's bed, flush with the roads it crosses: below)
    for n_part, ((fid, cls, kind, xy), (zb, zg), sw, gone) in enumerate(zip(rails, prof, switch, carried)):
        inside = (xy[:, 0] >= ex[0] - STEP_M) & (xy[:, 0] <= ex[2] + STEP_M) & \
                 (xy[:, 1] >= ex[1] - STEP_M) & (xy[:, 1] <= ex[3] + STEP_M)
        if gone or not inside.any():
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
                finals[n_part] = zb
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
            # a fill's foot stops at a road's edge or another track's deck: a wall there, not a slope over them
            others = [(f2, xy2) for f2, _c2, _k2, xy2 in rails if f2 != fid]
            for k, sg in enumerate((1, -1)):
                foot, zs = feet[k]
                lim = _foot_limits(xy, sg * u, foot, others, crossers)
                if np.any(lim < foot):
                    foot = lim
                    zs = np.asarray(ground(xy + sg * u * foot[:, None]), np.float64)
                    feet[k] = (foot, zs)
        else:
            cut = ~inside
        for i, j in _runs(cut):
            sl = slice(i, j + 1)
            p, uu, t, v = xy[sl], u[sl], top[sl], s[sl] / 3.0
            if kind == "ground":
                (fl, zl), (fr, zr) = feet
                if railxing.ENABLED and w == 0:         # (a track in use: crushed stone, its own scale)
                    _ballast_bed(mesh, p, uu, t, v, fl[sl], fr[sl], zl[sl], zr[sl], mesh.mat(BALLAST_STONE),
                                 BALLAST_TILE_M)
                else:
                    _ballast_bed(mesh, p, uu, t, v, fl[sl], fr[sl], zl[sl], zr[sl], ballast_w)
            elif kind == "tunnel":
                _tunnel(mesh, p, uu, t, v, ballast_w, concrete, i == 0, j == len(xy) - 1)
            else:
                if cls == "tram":
                    _tram_deck(mesh, p, uu, t, v, concrete)
                else:
                    _deck(mesh, p, uu, t, v, ballast_w, concrete)
                pier_w = min(PIER_W, 2 * TRAM_DECK_HALF_M) if cls == "tram" else PIER_W
                if obstacles is None:
                    obstacles = pier_obstacles(frame, cells, box)
                pr = pier_sites(s, xy, u, top, zg, i, j, obstacles, ground, PIER_EVERY, DECK_D, PIER_MIN_H,
                                np.array([PIER_T / 2, pier_w / 2]))
                if pr is not None:
                    small.append((pr, PIER_T / 2, pier_w / 2, concrete))
            st = t - SLEEPER_SINK + SLEEPER_H           # the sleepers' top: the rails' foot
            if cls == "tram":                       # railheads embedded flush in the slab, no exposed sleepers
                st = t - BALLAST_H + FLUSH_LIFT_M - RAIL_H + 0.005
            out_l = np.c_[uu, np.zeros(len(p))]
            for sg in (1, -1):
                c = p + uu * sg * half_g
                a, b = c + uu * RAIL_HEAD / 2, c - uu * RAIL_HEAD / 2
                mesh.strip(np.c_[a, st + RAIL_H], np.c_[b, st + RAIL_H], 0.0, 0.02, v, steel_w, up)
                mesh.strip(np.c_[a, st], np.c_[a, st + RAIL_H], 0.0, 0.05, v, steel_w, out_l)
                mesh.strip(np.c_[b, st], np.c_[b, st + RAIL_H], 0.0, 0.05, v, steel_w, -out_l)
            if cls != "tram":
                sp = _sleepers(s, xy, u, top, i, j, SLEEPERS_GONE[w])
                if sp is not None:
                    small.append((sp, SLEEPER_W / 2, SLEEPER_L / 2, mesh.mat(SLEEPER_WORN[w])))
        if cls != "tram":       # (the rails' physics, on straight through a level crossing: its rail tops lie 3 cm over the
            for i, j in _runs(~inside | sw):     # road, as the bed is flush with it; none in a switch: SWITCH_M)
                sl = slice(i, j + 1)
                _collider(mesh, xy[sl], u[sl], top[sl], s[sl] / 3.0, half_g, up)
    rows = []                                       # (the props of the tile's furniture: appended to `furniture` once, at the end)
    if STATION_FURNITURE_ON:        # (buffer stops at the tracks' ends, the footways' panels and signs: plan 035)
        carriage = [(ln, hw) for ln, hw, _z in crossers if ln is not None]
        nodes, levels = [], []
        for f in cells:
            for lo, la, t in (f.__dict__.get("_rail_nodes", ()) if f is not None else ()):
                if t.get("railway") in ("crossing", "level_crossing"):
                    (nodes if t.get("railway") == "crossing" else levels).append((lo, la))

        def enu(q):
            if not q:
                return []
            e, n, _ = frame.geodetic_to_enu(np.array([a[1] for a in q]), np.array([a[0] for a in q]), 0.0)
            return [np.array(v) for v in zip(np.asarray(e, np.float64).tolist(), np.asarray(n, np.float64).tolist())]
        nodes, levels = enu(nodes), enu(levels)
        ways = []
        for xy, w in walk_ways(frame, cells, box, 60.0):
            if levels and any(float(np.min(dnp.hypot(*(roads.densify(xy, 2.0)[:, :2] - q).T))) < WALK_NODE_WAY_M
                              for q in levels):
                w = max(w, WALK_STREET_W)       # (a street mapped as a pedestrian way: Nachod's Kladska, gates and all)
            ways.append((xy, w))
        crossings_ = walk_crossings(rails, ways, [tuple(q) for q in nodes], carriage, ex)
        for arrs, hx, hy in walk_panel_boxes(crossings_, finals, rails):
            small.append((arrs, hx, hy, concrete))
        if furniture is not None:
            rows += [r for r in walk_sign_rows(crossings_, ground, finals, rails, levels)
                     if roads.box_owns(box, r[1], r[2])]
            rows += [r for r in buffer_rows(dead_ends(rails, finals, worn, (box[0] - REACH_M, box[1] - REACH_M,
                                                                                 box[2] + REACH_M, box[3] + REACH_M)))
                          if roads.box_owns(box, r[1], r[2])]
    if walk and obstacles is None:
        obstacles = pier_obstacles(frame, cells, box)
    footbridges.add(mesh, walk, ground, crossers, ex, concrete, small, obstacles)
    small.sort(key=lambda e: -e[2])                 # (the piers first: a deck needs them more than its sleepers)
    for (c, along, across, z0, z1), hx, hy, mi in small:
        room = (TILE_VERTS_MAX - mesh.nv) // 8
        if room <= 0:
            break
        n = min(room, len(c))
        mesh.boxes(c[:n], along[:n], across[:n], hx, hy, z0[:n], z1[:n], mi)
    out = mesh.result()
    if out is not None and len(out[0]) > TILE_VERTS_MAX:    # (the ballast and rails alone past it: a big rail yard)
        # not the whole tile's track gone: the tracks of lowest rank (worn ones, then the shortest in the tile) are left
        # out, a quarter more at each retry, a yard's sidings before its main lines
        rank = sorted(range(len(rails)), key=lambda n: (worn.get(rails[n][0], 0), -float(_arc(rails[n][3])[-1])))
        if len(rank) > 1:                           # (unworn and longest first: the last of the rank go)
            for n in rank[-min(len(rank) - 1, max(1, len(rank) // 4)):]:
                _drop = _drop | {keep[n]}
            return tile_mesh(frame, tx, ty, cells, ground, sign_style, far_ground, return_decks=return_decks,
                             load=load, bed_elev=bed_elev, _drop=_drop, furniture=furniture, under=under)
        return (None, decks) if return_decks else None
    if furniture is not None:
        furniture += rows
    return (out, decks) if return_decks else out


# The materials only drawn (from mpsteppe's lean rail collider, in Madderose's PR #41): the rails and sleepers were ~88 %
# of a track's triangles (Tehachapi Loop, 16 tiles: 118.8k of 135.0k), all colliding, and a collision rebuild's cost
# grows with every colliding triangle loaded. A car on the track now drives on the ballast's top (the railheads stand
# 0.28 m over it); at a level crossing the track is flush with the road, which the car drives on either way.
# The rails collide again (GitHub #821: a car on train wheels, or a train, rode the railheads until that collider; it fell
# onto the ballast): their heads' top and sides are ~1/5 of a track's triangles, the sleepers ~3/5 of them stay drawn
# only. BNE_RAIL_COLLIDE=off: the bed alone, as from PR #41.
# (opt-in: the rails in the collider make a track's about 2.7x the triangles (rails + sleepers were 118.8k of 135k), and
# collision rebuilds cost frame time on rail-heavy tiles; for riding the rails (GitHub #821) BNE_RAIL_COLLIDE=on)
RAIL_COLLIDE = os.environ.get("BNE_RAIL_COLLIDE", "off") == "on"
RENDER_ONLY = frozenset(SLEEPER_WORN)
RENDER_ONLY_LEAN = frozenset(STEEL_WORN + SLEEPER_WORN)


def split_mesh(mesh, limit=VERTS_MAX, parts_max=PARTS_MAX):
    """[mesh, ...]: tile_mesh's mesh (or collision_mesh's, render_mesh's) cut into meshes of at most `limit` vertices each
    (the engine's 16-bit indices), triangles kept in their order so a track's pieces stay together. One mesh, as it is,
    when it fits. At most parts_max (the mod's list: the triangles past them are left out, which tile_mesh's TILE_VERTS_MAX
    keeps from happening)."""
    if mesh is None:
        return []
    P, T, UV, mats, M = mesh[:5]
    if len(P) <= limit:
        return [mesh]
    T, M = np.asarray(T), np.asarray(M)
    groups, cur, seen, n = [], [], np.zeros(len(P), bool), 0
    for a in range(0, len(T), SPLIT_CHUNK_TRIS):
        idx = np.arange(a, min(a + SPLIT_CHUNK_TRIS, len(T)))
        vs = np.unique(T[idx].ravel())
        new = int((~seen[vs]).sum())
        if cur and n + new > limit:
            groups.append(cur)
            cur, seen, n = [], np.zeros(len(P), bool), 0
            new = len(vs)
        cur.append(idx)
        seen[vs] = True
        n += new
    if cur:
        groups.append(cur)
    out = []
    for g in groups[:parts_max]:
        ti = np.concatenate(g)
        used = np.unique(T[ti].ravel())
        remap = np.searchsorted(used, T[ti])
        out.append((np.asarray(P)[used], remap, np.asarray(UV)[used], mats, M[ti]) + tuple(mesh[5:]))
    return out


def collision_mesh(mesh):
    """tile_mesh's mesh for a collision-only shape (the vertices are the full mesh's: cdae drops the unused ones), or None:
    the ballast, the decks and the piers, the sleepers never (RENDER_ONLY), the rails' drawn geometry never either; with
    BNE_RAIL_COLLIDE=on the rails' own boxes (COLLIDER: the inner face at exactly the gauge, thick outside)."""
    if mesh is None:
        return None
    P, T, UV, mats, M = mesh
    M = np.asarray(M)
    gone = set(RENDER_ONLY_LEAN)
    if not RAIL_COLLIDE:
        gone.add(COLLIDER)
    keep = ~np.isin(M, [k for k, m in enumerate(mats) if m in gone])
    if not keep.any():
        return None
    return P, np.asarray(T)[keep], UV, mats, M[keep]


def render_mesh(mesh):
    """tile_mesh's mesh without the physics-only boxes (COLLIDER), for the drawn shape."""
    if mesh is None or COLLIDER not in mesh[3]:
        return mesh
    P, T, UV, mats, M = mesh[:5]
    M = np.asarray(M)
    keep = M != mats.index(COLLIDER)
    if not keep.any():
        return None
    return (P, np.asarray(T)[keep], UV, mats, M[keep]) + tuple(mesh[5:])
