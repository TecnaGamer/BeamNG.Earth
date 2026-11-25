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
import fast
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
# A walkway bridge tested against every carriageway it crosses (the road revamp's R1, 2026-11-16): tile_mesh handed
# footbridges.add the rail's crossers, which true_crossers cuts to the roads crossing a railway at grade wherever a
# line is near, so a footbridge over a motorway near a railway saw no road under it and its slab stood 0.5-1.9 m over
# the lanes (Brisbane's Ipswich Motorway, a 72 m footway over a secondary road near Hofheim). With BNE_FOOT_CLEAR=on it
# gets every carriageway (and tunnel) near the tile: one that cannot clear them is left out, as footbridges.py
# intends. Off by default until reviewed in game.
FOOT_CLEAR = os.environ.get("BNE_FOOT_CLEAR", "off") == "on"
# Worn railways (disused, abandoned; OSM railway=historic not drawn) cut at every road they cross on the ground (the
# road revamp's R1, plan §2.2 LIFE; 1.9.0 cut them at motorways and, abandoned, trunk roads only: complaints of old
# lines across country roads in South Wales, Rheinland-Pfalz, Indiana). BNE_RAIL_LIFECYCLE=on; off by default until
# reviewed in game (a disused street track, as Fells Point's trolley lines, then stops at every street it crosses).
RAIL_LIFECYCLE = os.environ.get("BNE_RAIL_LIFECYCLE", "off") == "on"
# Railway-at-road fixes (the user's in-game reports r63 / r64 / r92, 2026-11-17; the Revamped roads switch set): a track
# that ends at a road with its line going on beyond it (a worn line cut at the road, a siding mapped up to each side of
# a paved-over crossing) gets no buffer stop there (dead_ends, _road_ends); a level crossing is one record per track
# part (crossing_records: the roads crossing that part, the at-grade test) that both cuts the 3D track and lays the
# crossing's deck (rails.tile_chunks), the track running on to the deck's exact edges. Off by default until reviewed
# in game (railstations.ROAD_FIT: the platforms).
RAIL_ROAD_FIT = os.environ.get("BNE_RAIL_ROAD_FIT", "off") == "on"
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
            import revisit
            with revisit.scope() as b:
                z = look(keys[miss] / 100.0)    # (at the key's own point, as grade_plans' ground)
            out[miss] = z
            if b.complete:                      # (a short read's heights not kept: asked again, read and faulted anew)
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
            import revisit
            with revisit.scope() as b:
                z = np.asarray(elev(la, lo), np.float64)
            out[miss] = z
            if not b.complete:      # (read short: not kept, so a later cell reading these points reads, and faults, anew;
                return out          # kept, they were its heights without a word: its plans counted as complete)
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


def parts(frame, cells, box, reach=REACH_M, clip=False, worn=None, gauges=None, underpasses=None, cut_ends=None):
    """[(fid, class, kind, xy (k, 2))]: the rail parts ("ground" / "bridge"; tunnels left out) of the lines drawn
    (drawn(); worn: a dict given here gets {fid: wear} of the worn ones, gauges {fid: m} OSM's gauge where tagged)
    within reach of box (x0, y0, x1, y1), resampled every STEP_M from each part's start, each feature once (by id).
    Each part must intersect that window: a feature's bbox can cover far-away bridges or a long bend around it.
    clip: the ground parts cut to their points within reach of box (a segment of line runs on for km, and every one of
    its points was profiled for each cell and tile: _span_clip); the bridges stay whole (a deck runs between its ends).
    underpasses: road_underpasses(frame, cells, box, reach) when the caller has them already. cut_ends: a set given here
    gets the ends _cut_worn_at_motorways made (end_key: a worn line cut at a road just ends, dead_ends)."""
    import fast
    fast.require(491, "rail_parts_select")
    cells = list(cells)
    near = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    wear_of = worn if worn is not None else {}
    kept = set()        # (worn heritage lines: cut only where 1.9.0 cut them, _cut_worn_at_motorways)
    # (each feature from the cell holding its longest copy: a long line's copies stitched from the map's tiles can be
    # cut short in some cells, and its level ranges (fractions of its length) then land 100+ m off in the short one:
    # a rail bridge over a road at 60.7771,11.21896 laid on the ground across it, a 2.3 m hump, GitHub #1137. The
    # layers' rail features in their RailIndex, the window's selection, pieces, clip test, resampling and sort one call:
    # earthfast.rail_parts_select; roads.level_ranges asked for each feature taken, roads.ground_parts for one without
    # any, as _pieces)
    idx = [_rail_index(feats, frame) if feats is not None and len(feats) and "subtype" in feats.attrs else None
           for feats in cells]
    out = fast._ef.rail_parts_select(idx, cells, tuple(float(v) for v in near), roads.level_ranges, roads.ground_parts,
                                     wear_of, kept, gauges, TUNNELS_ON, RAIL_LIFECYCLE)
    if any(kind == "ground" and wear_of.get(fid) for fid, _c, kind, _xy in out):
        out = _cut_worn_at_motorways(out, wear_of, motorway_crossings(frame, cells, box, reach), kept, cut_ends)
    out = _drop_stubs(out)
    out = level_bridges(frame, cells, out, near)
    return _span_clip(out, road_underpasses(frame, cells, box, reach) if underpasses is None else underpasses,
                      near if clip else None)



def _rail_index(feats, frame):
    """earthfast.RailIndex of feats' rail features in frame (parts' selection reads it), kept on the layer for the frame's
    coordinates (feats.xy_in's array: when that is made again, so is this) and the columns it was made from (an anchor
    or two at a time, as xy_in): every node window of railprofile's lattice read the same lines again (~150 windows a
    line in a city)."""
    import fast
    xin = feats.xy_in(frame)
    a = feats.attrs
    cols = _drawn_cols(feats)
    src = (xin, cols, a["subtype"], a.get("osm_usage"), a.get("osm_railway:preserved"), a.get("osm_gauge"), feats.ids,
           feats.lonlat, feats.part_off, feats.ring_off, feats.coord_off, STEP_M, roads.MIN_PART, MIN_PART_M,
           SHORT_TUNNEL_M)
    memos = feats.__dict__.setdefault("_rail_index", [])
    for got_src, got in memos:
        if len(got_src) == len(src) and all(x is y for x, y in zip(got_src, src)):
            return got
    rows = np.flatnonzero(_is(a["subtype"], ("rail",)))
    rl = rows.tolist()
    _plain, cls_of, wear_at = cols
    ids = feats.ids
    ix = fast._ef.RailIndex(
        rows.astype(np.int64), np.asarray(feats.part_off, np.int64), np.asarray(feats.ring_off, np.int64),
        np.asarray(feats.coord_off, np.int64), _lonlat(feats),
        np.asarray(xin, np.float64).reshape(-1, 2), np.asarray(feats.boxes(frame), np.float64).reshape(-1, 4)[rows],
        np.array([cls_of[i] for i in rl], np.int64), np.array([wear_at[i] for i in rl], np.int64),
        np.array([roads._feature_length_m(feats, i) for i in rl], np.float64), [str(ids[i]) for i in rl],
        [heritage(feats, i) for i in rl], np.array([osm_gauge(feats, i) or np.nan for i in rl], np.float64),
        np.asarray(frame.origin, np.float64), np.asarray(frame.rot, np.float64), float(STEP_M),
        float(roads.MIN_PART), float(MIN_PART_M), float(SHORT_TUNNEL_M))
    memos.append((src, ix))
    del memos[:-2]
    return ix


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
    """Short road tunnels near box as (xy, half width), including source tunnels made ground by gradesep: layer by
    layer, its roads in its earthfast.UnderpassIndex (_underpass_index), a road's tunnel geometry made once
    (_underpass_entry), the window's selection, specs, arc test and dedupe by (id, ends) one call a layer
    (rail_underpass_select). A road's spec is each call's: its tunnel spec (roads.tunnel_specs, asked every call: a
    layer's specs made on the lidar rule fault their readers), or the layer's road_specs asked at its first road taken
    with a map tunnel missing from its ranges (specs made while a download failed are made again)."""
    import fast
    fast.require(491, "rail_underpass_select")
    near = tuple(float(v) for v in (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach))
    out, seen = [], fast._ef.UnderpassSeen()
    for feats in cells:
        if feats is None or not len(feats):
            continue
        specs = roads.tunnel_specs(feats)
        fast._ef.rail_underpass_select(_underpass_index(feats, frame), specs, near,
                                       lambda i: _underpass_entry(feats, frame, specs, i),
                                       lambda: roads.road_specs(feats), UNDERPASS_SIDE_M, seen, out)
    return out


def _lonlat(feats):
    """feats.lonlat itself where it is (N, 2) float64 (RailIndex tells its ground_parts views by it), else a copy."""
    ll = feats.lonlat
    if isinstance(ll, np.ndarray) and ll.dtype == np.float64 and ll.ndim == 2 and ll.shape[1] == 2:
        return ll
    return np.asarray(ll, np.float64).reshape(-1, 2)


def _underpass_index(feats, frame):
    """earthfast.UnderpassIndex of feats' roads in frame (their boxes, a map tunnel or not), kept on the layer for the
    frame's coordinates and its road revision (roadlanes' reset makes the specs and ranges again)."""
    import fast
    xin = feats.xy_in(frame)
    tagged = _tunnel_tagged(feats)
    src = (xin, feats.__dict__.get("_road_revision", 0), tagged, roads.UNDERPASS_MAX_M)
    memos = feats.__dict__.setdefault("_rail_underpass_index", [])
    for got_src, got in memos:
        if len(got_src) == len(src) and all(x is y or (k == 1 and x == y) for k, (x, y) in enumerate(zip(got_src, src))):
            return got
    ix = fast._ef.UnderpassIndex(np.asarray(feats.boxes(frame), np.float64).reshape(-1, 4),
                                 np.ascontiguousarray(tagged, bool), float(roads.UNDERPASS_MAX_M))
    memos.append((src, ix))
    del memos[:-2]
    return ix


def _underpass_entry(feats, frame, specs, i):
    """(spec, missing, id, tunnel parts, missing tunnels): road i's tunnel geometry in frame for the UnderpassIndex: its
    tunnel parts when it has a tunnel spec (retained tunnels keep their cleared portal ends) and the map's tunnels that
    gradesep made ground (missing from its ranges: Nashville's Carney Street keeps that clearance evidence, also when
    another tunnel on the feature remains), each an (k, 2) array."""
    spec = specs[i] is not None
    rings = roads.tunnel_parts(feats, i) if spec else []
    tagged = ([(a, b, kind) for a, b, kind in roads.tag_level_ranges(feats, i) if kind == "tunnel"]
              if _tunnel_tagged(feats)[i] else [])
    missing = []
    if tagged:          # (the map's tunnels first: most features have none)
        effective = [(a, b) for a, b, kind in roads.raw_level_ranges(feats, i) if kind == "tunnel"]
        missing = [(a, b, kind) for a, b, kind in tagged
                   if not any(c <= a + roads.MIN_PART and d >= b - roads.MIN_PART for c, d in effective)]
    gone = roads._parts_by_level_new(feats, i, "tunnel", rngs=missing) if missing else []

    def enu(rs):
        return [np.ascontiguousarray(np.column_stack(mapgeom.enu_xy(feats, frame, r)), np.float64) for r in rs]
    return spec, bool(missing), str(feats.ids[i]), enu(rings), enu(gone)


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
    road = np.c_[dnp.interp(a, s, road_xy[:, 0]), dnp.interp(a, s, road_xy[:, 1])]
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
    short = [k for k, (_fid, c, kind, xy) in enumerate(rails)
             if kind == "bridge" and c != "tram" and float(_arc(xy)[-1]) <= LEVEL_BRIDGE_MAX_M]
    # (every short bridge's crossings with every way in ONE kernel call: per way, each bridge's hits in the order a call
    # for that bridge alone gives them (the kernel's loop is bridge, way, way segment, bridge segment). A call per way
    # per short bridge per window was 1.6 million calls, ~30 s of a 16-tile probe at Brno; a call per short bridge
    # still handed the kernel every way again, ~6 s of a Montreal rail tile's rails stage, 2026-11-21)
    hits_of = {k: {} for k in short}         # bridge -> {way: its hits} (the ways without one are passed over)
    if short:
        for g, hits in enumerate(roads._crossings_by([rails[k][3] for k in short], ways)):
            for pi, arc, pt in hits:
                hits_of[short[pi]].setdefault(g, []).append((0, arc, pt))
    out = []
    for k, (fid, c, kind, xy) in enumerate(rails):
        if k in hits_of:
            for g in sorted(hits_of[k]):
                line, hits = ways[g], hits_of[k][g]
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
    pts = np.r_[np.c_[dnp.interp(a, s, road_xy[:, 0]), dnp.interp(a, s, road_xy[:, 1])], b[[0, -1]]]
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


SEG_HITS_PAIRS = 1 << 18    # segment pairs _seg_hits tests at once (its arrays stay a few MB whatever the lines)


def _seg_hits(a, b):
    """Arc positions along polyline a where it crosses polyline b (proper segment intersections). Every segment of b
    at once per run of a's segments, the hits in the order the loop over both gave them (a's segment, then b's), the
    same products per pair. Pure."""
    n, m = len(a) - 1, len(b) - 1
    if n < 1 or m < 1:
        return []
    out = []
    sa = _arc(a)
    q, t = b[:-1], b[1:] - b[:-1]
    rows = max(1, SEG_HITS_PAIRS // m)
    with np.errstate(divide="ignore", invalid="ignore"):
        for i0 in range(0, n, rows):
            i1 = min(i0 + rows, n)
            p, r = a[i0:i1], a[i0 + 1:i1 + 1] - a[i0:i1]
            den = r[:, None, 0] * t[None, :, 1] - r[:, None, 1] * t[None, :, 0]
            d0, d1 = q[None, :, 0] - p[:, None, 0], q[None, :, 1] - p[:, None, 1]
            u = (d0 * t[None, :, 1] - d1 * t[None, :, 0]) / den
            v = (d0 * r[:, None, 1] - d1 * r[:, None, 0]) / den
            i, j = np.nonzero((np.abs(den) >= 1e-12) & (u >= 0.0) & (u <= 1.0) & (v >= 0.0) & (v <= 1.0))
            out.extend(float(x) for x in sa[i0 + i] + u[i, j] * (sa[i0 + i + 1] - sa[i0 + i]))
    return out


def coarse_underpasses(frame, cells, box, rails, reach=REACH_M):
    """[(xy, half width)] (as road_underpasses): on coarse elevation (COARSE_AT), each ground road's piece
    COARSE_PIECE_M either side of where a rail bridge part crosses it."""
    bridges = [r for r in rails if r[2] == "bridge" and len(r[3]) >= 2]
    if not bridges:
        return []
    coarse = _coarse_here(frame, box)
    if not coarse and not (FLAT_SEP_ON and RAW_ELEV is not None):
        return []
    out = []
    boxes = _rail_boxes(bridges)
    for line, half in crossings(frame, cells, box, reach):
        if len(line) < 2:
            continue
        # (only the bridge parts whose box meets the road's can cross it: every road against every part's every
        # segment was 1.5e9 segment pairs for one plan in Hong Kong (22.414 N 114.213 E), which never finished)
        lo, hi = line.min(0), line.max(0)
        near = np.flatnonzero((boxes[:, 0] <= hi[0]) & (boxes[:, 2] >= lo[0])
                              & (boxes[:, 1] <= hi[1]) & (boxes[:, 3] >= lo[1]))
        if not len(near):
            continue
        sl = _arc(line)
        for b in (bridges[k][3] for k in near):
            for sc in _seg_hits(line, b):
                if not coarse and not flat_rail_crossing(frame, b, line, sc):
                    continue
                a0, a1 = max(0.0, sc - COARSE_PIECE_M), min(float(sl[-1]), sc + COARSE_PIECE_M)
                if a1 - a0 < 1.0:
                    continue
                ss = np.linspace(a0, a1, max(3, int((a1 - a0) / STEP_M) + 1))
                piece = np.c_[dnp.interp(ss, sl, line[:, 0]), dnp.interp(ss, sl, line[:, 1])]
                out.append((piece, half + UNDERPASS_SIDE_M))
    return out


def _underpass_distance(xy, line):
    """Distance to a finite tunnel centreline, and fraction along it, per point of xy (k, 2): the first nearest of its
    segments (of length 1e-6 m or more), inf and 0 where none (earthfast.rail_underpass_distance). Pure."""
    import fast
    fast.require(487, "rail_underpass_distance")
    xy, line = roads._lines64([xy, line])
    return fast._ef.rail_underpass_distance(xy, line)


def _rail_boxes(rails):
    """(n, 4) the boxes (x0, y0, x1, y1) of the rails' parts (coarse_underpasses' bridges)."""
    out = np.zeros((len(rails), 4))
    for n, (*_, xy) in enumerate(rails):
        out[n, :2] = xy.min(0)
        out[n, 2:] = xy.max(0)
    return out


def _underpass_hits(rails, tunnels, margin):
    """Per tunnel of tunnels [(line, half width)]: {part index of rails: (its masked points (ascending), their fraction
    along the tunnel)}, the parts with any. A part's points near the tunnel (half width + margin) where it crosses the
    tunnel (UNDERPASS_MIN_SIN or squarer) are masked when their run inside the tunnel's padded box connects (ends within
    0.5 m) to a run that actually crosses it: a crossing on one track must not qualify a neighbouring track, and only the
    runs inside the footprint are connected (the source feature's splits kept, a turnout or a returning track far
    outside it not followed). earthfast.rail_underpass_hits. Pure."""
    tunnels = list(tunnels)
    out = [{} for _ in tunnels]
    if not tunnels:
        return out
    import fast
    fast.require(487, "rail_underpass_hits")
    ti, pi, ki, fr = fast._ef.rail_underpass_hits(roads._lines64([xy for *_, xy in rails]),
                                                  roads._lines64([ln for ln, _h in tunnels]),
                                                  [float(h) for _ln, h in tunnels], float(margin))
    if not len(ti):
        return out
    cut = np.flatnonzero((np.diff(ti) != 0) | (np.diff(pi) != 0)) + 1
    for a, b in zip(np.r_[0, cut].tolist(), np.r_[cut, len(ti)].tolist()):
        out[int(ti[a])][int(pi[a])] = (ki[a:b], fr[a:b])
    return out


def _underpass_masks(rails, line, half, margin):
    """(k,) bool per part of rails: _underpass_hits' points of one tunnel (line, half)."""
    masks = [np.zeros(len(xy), bool) for *_, xy in rails]
    for n, (idx, _t) in _underpass_hits(rails, [(line, half)], margin)[0].items():
        masks[n][idx] = True
    return masks


def _span_clip(rails, tunnels, near=None):
    """rails with each ground part's track over mapped road tunnels (_underpass_hits, FOOT_MAX_M + STEP_M past their half
    width: the approach ballast's feet out of the carriageway too) made supported spans ("bridge" pieces), the sample
    lattice and the shared end points kept (across source features and tiles too); then, near (x0, y0, x1, y1) given,
    each ground piece cut to its runs of points inside near, one STEP_M more each side (the same sample points as the
    whole part: they lie every STEP_M from its start), a run of one point left out. earthfast.rail_span_clip. Pure."""
    tunnels = list(tunnels)     # (an iterable read once, as the Python's loop did)
    if not tunnels and near is None:
        return rails
    import fast
    fast.require(487, "rail_span_clip")
    pi, ai, bi, br = fast._ef.rail_span_clip(roads._lines64([xy for *_, xy in rails]),
                                             [kind == "ground" for _f, _c, kind, _xy in rails],
                                             roads._lines64([ln for ln, _h in tunnels]), [float(h) for _ln, h in tunnels],
                                             FOOT_MAX_M + STEP_M, None if near is None else tuple(map(float, near)))
    out = []
    for n, a, b, bridge in zip(pi.tolist(), ai.tolist(), bi.tolist(), br.tolist()):
        r = rails[n]
        xy = r[3]
        if a == 0 and b == len(xy) - 1 and not bridge:
            out.append(r if r[2] != "ground" or not tunnels else (r[0], r[1], r[2], xy[a:b + 1]))
        else:
            out.append((r[0], r[1], "bridge" if bridge else r[2], xy[a:b + 1]))
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


# (a line's wear, drawn(): 1 out of use, 2 abandoned (drawn worn: the user, 2026-10-27, "id still like abandond lines
# to show. perhapse with some grass on them or sumilate wear"); under construction: not drawn (no track yet), as OSM's
# proposed lines; OSM's railway values (railtags.py) drawn with a wear or not at all: earthfast's raildrawn.rs)
TUNNELS_ON = os.environ.get("BNE_RAIL_TUNNELS", "off") not in ("off", "0", "false", "")   # (plan 035: unproven in the game)
TUNNEL_HALF_M = 3.9         # m: half the rail tunnel's inner width
TUNNEL_WALL_H = 1.6         # m its walls stand straight over the ballast top before the vault springs
TUNNEL_RISE_M = 4.4         # m the vault rises over the springing (a half ellipse: the tube is 6 m over the ballast)
TUNNEL_FLOOR_M = 0.3        # m the tube's walls go under the ballast top
TUNNEL_SECTION_N = 14       # points of the vault
PORTAL_REACH_M = 1.3        # m a portal's face reaches round the opening
SHORT_TUNNEL_M = 60.0       # m: a "tunnel" this short is the line passing under a road bridge (OSM tags those
                            # tunnel too): track there (the track stopped either side of an overpass, 2026-10-25)


def _str_rows(a):
    """A column as UCS-4 rows of each value's str(v or "") (numpy's unicode arrays as they are), None for None. An
    object value's trailing NULs, which the rows' padding would drop, become one U+FFFF (the kernel only compares with
    and searches for ASCII words and asks for emptiness: the same answers)."""
    if a is None:
        return None
    import fast
    a = np.asarray(a)
    if a.dtype.kind != "U":
        s = [str(v or "") for v in a.tolist()]
        a = np.array([x.rstrip("\0") + "￿" if x.endswith("\0") else x for x in s], dtype=str).reshape(-1)
    return fast.ucs4(a)


_DRAWN_CLASSES = ("standard_gauge", "narrow_gauge", "light_rail", "tram")   # (earthfast.rail_drawn_cols' class codes)


def _drawn_cols(feats):
    """(plain, bridges, wear) lists over feats' features: drawn()'s class code (_DRAWN_CLASSES, -1 not drawn) with
    bridges False / True and its wear, made in one pass of the Rust a layer (earthfast.rail_drawn_cols; ~640k calls of
    the Python a Manhattan visit) and kept on the layer while its columns are the same objects (railtags adds osm_*;
    as RuleTable.rows and the layer's other memos, nothing edits a column in place). The flags' sums are Python 3.12+'s
    compensated sum() (the release builds' 3.12): on a source install with 3.10 / 3.11, whose plain sum() could fall the
    other side of half a line, the same answer as every other install."""
    a, t = feats.attrs, feats.rules.get("flags")
    src = (a.get("subtype"), a.get("class"), a.get("osm_railway"), a.get("osm_highway"), t,
           *((t.feature, t.start, t.end, t.value) if t is not None else ()), RAIL_LIFECYCLE)
    got = feats.__dict__.get("_rail_drawn")
    if got is not None and len(got[0]) == len(src) and all(x is y for x, y in zip(got[0], src)):
        return got[1]
    import fast
    fast.require(486, "rail_drawn_cols")
    fl = None
    if t is not None:
        fl = (np.ascontiguousarray(t.feature, np.int64), np.ascontiguousarray(t.start, np.float64),
              np.ascontiguousarray(t.end, np.float64), _str_rows(t.value))
    plain, br, w = fast._ef.rail_drawn_cols(len(feats), *(_str_rows(c) for c in src[:4]), fl, RAIL_LIFECYCLE)
    cols = (plain.tolist(), br.tolist(), w.tolist())
    feats.__dict__["_rail_drawn"] = (src, cols)
    return cols


def drawn(feats, i, bridges=False):
    """(class, wear) of a built railway track, or None (wear: 0 in use, 1 disused, 2 abandoned, by OSM's railway value
    or the line's flags over most of its length; a line under construction is not drawn). bridges also admits trams:
    only their mapped bridge pieces belong in the 3D mesh; ground trams remain street track in rails.py, not ballast,
    grading or inferred underpasses. The rules are earthfast.rail_drawn_cols' (a layer at a time)."""
    plain, br, w = _drawn_cols(feats)
    k = (br if bridges else plain)[i]
    return None if k < 0 else (_DRAWN_CLASSES[k], w[i])


def heritage(feats, i):
    """A heritage line by OSM's tags (railtags.py): usage=tourism or railway:preserved=yes. A worn one of these (a
    preserved line tagged disused out of season, a museum line) keeps its level crossings with BNE_RAIL_LIFECYCLE: cut
    only at the roads 1.9.0 cut worn lines at (HERITAGE_CUT_CLASSES; the road revamp plan's R1h)."""
    u = feats.attrs.get("osm_usage")
    p = feats.attrs.get("osm_railway:preserved")
    return bool((u is not None and str(u[i] or "") == "tourism") or (p is not None and str(p[i] or "") == "yes"))



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
    pieces, kinds = [], []
    for kind, spans in (("ground", ground), ("bridge", [(a, b) for a, b, k in off if k == "bridge"]),
                        ("tunnel", [(a, b) for a, b, k in off if k == "tunnel"] if TUNNELS_ON else [])):
        for a, b in spans:
            if b - a > min_part:
                pieces.append((a, b))
                kinds.append(kind)
    rings = [ring for part in feats.parts(i) for ring in part if len(ring) >= 2]
    # (every ring's pieces in one native call, ring by ring as before)
    for r, kind in zip(roads.sub_polylines(rings, pieces), kinds * len(rings)):
        if len(r) >= 2:
            out[kind].append(r)
    return list(out.items())


def crossings(frame, cells, box, reach=REACH_M):
    """[(xy (k, 2), half width)]: the ground roads near box (their carriageways cut the track at grade)."""
    return _road_crossings(frame, cells, box, reach, worn=False)


def _road_crossings(frame, cells, box, reach, worn):
    import fast
    fast.require(549, "rail_crossing_roads")
    near = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    seen, out = set(), []
    for feats in cells:
        if feats is None or not len(feats):
            continue
        specs = classes = eligible = None
        if worn:
            classes = feats.attrs.get("class")
            if classes is None:
                continue
            if RAIL_LIFECYCLE:
                sub = feats.attrs.get("subtype")
                eligible = None if sub is None else _is(sub, ("road",))
            else:
                eligible = _is(classes, ("motorway", "trunk", "motorway_link", "trunk_link"))
        else:
            specs = roads.road_specs(feats)
        fast._ef.rail_crossing_roads(feats.boxes(frame), near, feats.ids, specs, classes, eligible, roads.STYLE,
                                     feats, frame, roads, mapgeom.enu_rings, seen, out)
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
                for xy in mapgeom.enu_rings(feats, frame, rings):
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
HERITAGE_CUT_CLASSES = WORN_CUT_CLASSES     # (a heritage line's: 1.9.0's, with the lifecycle switch too)
if RAIL_LIFECYCLE:                  # (every road class: roads.STYLE's)
    WORN_CUT_CLASSES = {1: frozenset(roads.STYLE), 2: frozenset(roads.STYLE)}
WORN_CUT_MIN_SIN = 0.2      # (a crossing flatter than this cuts as if at this angle: the cut stays bounded)


def motorway_crossings(frame, cells, box, reach=REACH_M):
    """[(xy (k, 2), half width, class)]: the ground motorway and trunk carriageways (their links too) near box."""
    return _road_crossings(frame, cells, box, reach, worn=True)


def end_key(p):
    """A track end's point as a key (mm): the cut ends _cut_worn_at_motorways records, as dead_ends looks them up."""
    return (int(round(float(p[0]) * 1000.0)), int(round(float(p[1]) * 1000.0)))


def _cut_worn_at_motorways(rails, wear_of, roads_xy, kept=frozenset(), cut_ends=None):
    """rails with each worn ground part (wear_of {fid: 1 | 2}) cut where it crosses one of roads_xy ((xy, half width,
    class): motorway_crossings) of its wear's WORN_CUT_CLASSES (HERITAGE_CUT_CLASSES for the fids in kept: heritage
    lines): no point within the carriageway's half width (over the crossing's angle) and WORN_CUT_M either side of the
    crossing. Pure."""
    import fast
    fast.require(549, "rail_cut_worn")
    return fast._ef.rail_cut_worn(rails, wear_of, roads_xy, kept, WORN_CUT_CLASSES, HERITAGE_CUT_CLASSES,
                                  cut_ends, end_key, WORN_CUT_MIN_SIN, WORN_CUT_M)


def _resample(xy, step):
    # (np.concatenate / column_stack, not np.r_ / np.c_: the same arrays, and those index tricks were a fifth of a node
    # profile's time, railprofile.py)
    s = _arc(xy)
    if s[-1] <= 1e-6:
        return xy[:1]
    t = np.append(np.arange(0.0, s[-1], step), s[-1])
    if len(t) > 2 and t[-1] - t[-2] < 0.25 * step:     # (no sliver section at the end)
        t = np.append(t[:-2], t[-1])
    return np.column_stack((dnp.interp(t, s, xy[:, 0]), dnp.interp(t, s, xy[:, 1])))


def _arc(xy):
    fast.require(584, "rail_arcs")
    return fast._ef.rail_arcs(np.asarray(xy, np.float64))


def _left(xy):
    fast.require(584, "rail_left")
    return fast._ef.rail_left(np.asarray(xy, np.float64))


def bed_heights(z):
    """The bed line over ground heights z (every STEP_M): averaged over SMOOTH_M either side, twice (a smooth
    vertical curve), through a ridge and over a dip alike: the grader (grade_plans) cuts and fills the ground to it."""
    return bed_heights_many([z])[0]


def bed_heights_many(zs):
    """[bed_heights(z) for z in zs], in one call of the Rust (earthfast.rail_bed_heights: each running mean over 2r + 1
    values, the ends extended along their grade by odd reflection, so a straight grade stays straight to its end and two
    parts meeting at a joint meet at its height; numpy's cumsum order). A line of fewer than 3 heights is its own bed
    (the same array); an (n, 1) column gives the (n,) bed, as numpy's flattening cumsum did. numpy's floating-point
    warnings are not raised (an accepted difference: nothing sets np.errstate on this path and the server's lines make
    no non-finite beds, the online cold caches' every call)."""
    import fast
    fast.require(483, "rail_bed_heights")
    zs = [np.asarray(z, np.float64) for z in zs]
    # (the kernel reads aligned contiguous float64 lines: a packed record view is copied first)
    got = iter(fast._ef.rail_bed_heights([np.require(z.reshape(-1) if z.ndim == 2 and z.shape[1] == 1 else z,
                                                     None, ("C", "A")) for z in zs if len(z) >= 3],
                                         max(1, int(round(SMOOTH_M / STEP_M)))))
    return [z if len(z) < 3 else next(got) for z in zs]


def _envelope(s, req, grade=GRADE):
    """max over k of req[k] - grade * |s - s[k]|: the lowest line over every req that climbs at most grade."""
    out = np.array(req, np.float64).tolist()    # (Python floats: the same arithmetic, 5x faster than numpy's scalars)
    rise = (grade * np.diff(np.asarray(s)[:len(out)])).tolist()
    for k in range(1, len(rise) + 1):           # forward then backward pass (s ascending)
        v = out[k - 1] - rise[k - 1]
        if v > out[k]:                          # (max(out[k], v): out[k] on a tie or a NaN)
            out[k] = v
    for k in range(len(rise) - 1, -1, -1):
        v = out[k + 1] - rise[k]
        if v > out[k]:
            out[k] = v
    return np.array(out, np.float64)


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
    earthfast.rail_profile (the Python's numpy, its order and its ties); ground is asked here as the Python asked it: the
    parts' points, the decks' approach rays (approach_levels_many's), the underpasses' mouths. The contract (every real
    caller's, railways.parts' lines and point_ground / grade_plans' ground): float64 (k, 2) lines, ground a new (n,)
    array each call (not a reused buffer: the beds are laid after its last call). Outside it the Python's results are not
    kept (accepted, Codex r2a20: float32 lines' arcs, an (n, 1) ground, a NaN's payload bytes).
    """
    if not rails:
        return []
    import fast
    fast.require(525, "rail_profile")
    allxy = np.concatenate([xy for *_, xy in rails])
    zg_all = np.asarray(ground(allxy), np.float64)
    zgs, k = [], 0
    for *_, xy in rails:
        zgs.append(zg_all[k:k + len(xy)])
        k += len(xy)
    xy64 = np.ascontiguousarray(allxy, np.float64)
    off = np.cumsum([0] + [len(z) for z in zgs], dtype=np.int64)
    kinds = [r[2] for r in rails]
    d = np.arange(0.0, APPROACH_REACH_M + APPROACH_FLAT_M + 1e-9, STEP_M)
    ray_z = np.zeros(0)
    if "bridge" in kinds:
        pts = fast._ef.rail_approach_rays(xy64, off, kinds, d)
        if len(pts):
            ray_z = np.asarray(ground(pts), np.float64).ravel()
    shared = {}                                 # (a double track's beds are smoothed from one profile: railxing.py)
    base = zg_all
    if railxing.ENABLED:
        gi = [n for n, (_, _, kind, _) in enumerate(rails) if kind == "ground"]
        if len(gi) > 1:
            shared = dict(zip(gi, railxing.corridor_ground([rails[n][3] for n in gi], [zgs[n] for n in gi])))
            base = np.concatenate([np.asarray(shared[n], np.float64).ravel() if n in shared else zgs[n]
                                   for n in range(len(rails))])
    # Read the lower road at its mouths, not on the filled railway bed above it.
    underpasses = list(underpasses)
    ti = pi = ki = np.zeros(0, np.int64)
    fr = mouth_z = np.zeros(0)
    if underpasses:
        fast.require(487, "rail_underpass_hits")
        ti, pi, ki, fr = fast._ef.rail_underpass_hits(roads._lines64([xy for *_, xy in rails]),
                                                      roads._lines64([ln for ln, _h in underpasses]),
                                                      [float(h) for _ln, h in underpasses], DECK_HALF_M + STEP_M)
        if len(ti):
            mouths = np.concatenate([underpasses[t][0][[0, -1]] for t in np.unique(ti).tolist()])
            mouth_z = np.asarray(ground(mouths), np.float64).ravel()
    zb, same = fast._ef.rail_profile(
        xy64, off, kinds, [r[1] for r in rails], LINE_GRADE, zg_all, np.ascontiguousarray(base, np.float64), ray_z, d,
        ti, pi, ki, fr, mouth_z,
        (max(1, int(round(SMOOTH_M / STEP_M))), int(round(APPROACH_FROM_M / STEP_M)), int(round(APPROACH_FLAT_M / STEP_M))),
        [STEP_M, GRADE, APPROACH_FLAT_M, APPROACH_TOL_M, APPROACH_MIN_M, APPROACH_RAISE_MAX_M, UNDERPASS_CLEAR_M, DECK_D,
         BALLAST_H, DECK_RAISE_MAX_M, SEED_LIFT_MAX_M])
    # (a ground part of fewer than 3 points that nothing lifted: the Python's bed was the ground array itself)
    return [((shared.get(n, zgs[n]) if s else z), zgs[n]) for n, (z, s) in enumerate(zip(zb, same))]


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
    fast.require(584, "rail_flush")
    return fast._ef.rail_flush(s, zb, blk, zroad)


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
    near = near.tolist()
    # (every near road's crossings in one kernel call, each road's as a call of its own gives them)
    for k, hits in zip(near, roads._crossings_by([xy], [lines[k] for k in near]) if near else ()):
        rxy, rz, rh = roads_[k]
        r = lines[k]
        rzz = np.asarray(rz, np.float64)[:len(r)]
        if hits and any(abs(float(rzz[int(np.argmin(dnp.hypot(*(r - pt).T)))])
                            - (float(dnp.interp(arc, s, zb)) + RAIL_TOP_M)) < CROSSING_DZ for _p, arc, pt in hits):
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


RUN_END_SAMPLES = 40        # points tried along a STEP_M section for a crossing's edge (5 cm apart) ...
RUN_END_BISECT = 6          # ... then halvings between the two round it (under a millimetre)


FIT_NATIVE = 423          # earthfast.VERSION of the railway-at-road kernels (railfit.rs)


XING_RUN_MAX_M = 150.0      # m along a part a crossing's run reaches past where the road crosses it (or runs down it):
                            # inside the parts' REACH_M, so every tile drawing a run holds its anchor; a crossing as
                            # shallow as 2 degrees of a 4 m carriageway (4.5 / sin) is cut all through its road
XING_GRADED_M = 170.0       # m round the tile within which the records take the roads (every copy) and read their
                            # graded surface near the track (XING_TRACK_M; a run there can reach the tile:
                            # XING_RUN_MAX_M and a carriageway's reach); past it the natural ground
XING_TRACK_M = 12.0         # m from a ground track: the road points whose graded surface the records read
XING_RAIL_OUT_M = GAUGE_M / 2 + RAIL_HEAD   # m: the outer rail off the axis; a road's reach grows by it times |cos| of
                                            # the crossing's angle (a shallow crossing's rail kept out of the lane)


def crossing_records(frame, tx, ty, cells, ground, sign_style=None, far_ground=None, load=None, bed_elev=None):
    """BNE_RAIL_ROAD_FIT: the level crossings of road tile (tx, ty)'s track parts, made once and read by both the 3D track
    (tile_mesh: where it is cut, flushed and where its runs end) and the crossing decks (rails.tile_chunks:
    crossing_panels), so the two cannot disagree. The tile's rail parts, beds and crossers as tile_mesh makes them
    (its arguments' meaning); per ground part (_part_crossings) a road cuts it only where its centre line crosses that
    part or the part runs down it (street_along), with the at-grade test that cuts the track (_blocked's, at the bed
    the roads every STEP_M on their graded surface near the tile (XING_GRADED_M), the natural ground past it, against
    the track's bed on the DEM (railprofile's lattice, else profiled on the natural ground): the same at a point in
    every tile), each run at most XING_RUN_MAX_M past its anchor,
    each street-running stretch qualified on its own (STREET_ALONG_M), and each crossing's two edges found with that
    same test. A dict: tunnels, ground (mixed), worn,
    gauges, cut (end_key of the cut ends), rails, prof, all_cr, cr, crossers, runs (per part: None or {blk, runs,
    start_t, end_t}), half_g (per part: the rails' half gauge), sign_style."""
    cells = list(cells)
    box = roads.tile_bbox(tx, ty)
    tunnels = road_underpasses(frame, cells, box)
    graded = ground
    ground = _mixed(ground, far_ground, box)
    ex = (box[0] - roads.OVERLAP, box[1] - roads.OVERLAP, box[2] + roads.OVERLAP, box[3] + roads.OVERLAP)
    worn, gauges, cut = {}, {}, set()
    rails = parts(frame, cells, box, clip=True, worn=worn, gauges=gauges, underpasses=tunnels, cut_ends=cut)
    exs = (ex[0] - STEP_M, ex[1] - STEP_M, ex[2] + STEP_M, ex[3] + STEP_M)
    if not any(_box_hit((xy[:, 0].min(), xy[:, 1].min(), xy[:, 0].max(), xy[:, 1].max()), exs) for *_, xy in rails):
        rails = []
    # (the bed the mesh draws and the crossings test: railprofile's lattice, the same in every tile; a point it has none
    # for from the natural ground smoothed along the part (_beds_fit), not profiled in the tile's window)
    nat = far_ground if far_ground is not None else graded
    trust = []
    prof = (_beds_fit(frame, rails, ground, nat, tunnels + coarse_underpasses(frame, cells, box, rails), load, bed_elev,
                      trust) if rails else [])
    ground_lines = [xy for _f, c, kind, xy in rails if kind == "ground" and c != "tram"]
    all_cr = crossings(frame, cells, box, reach=50.0) if rails else []
    cr = ((true_crossers(ground_lines, all_cr) if TRUE_CROSSERS_ON else all_cr) + tunnels) if rails else []
    crossers = []
    if cr:
        zc = np.asarray(ground(np.concatenate([ln for ln, _ in cr])), np.float64)
        k = 0
        for line, hw in cr:
            crossers.append((line, hw, zc[k:k + len(line)]))
            k += len(line)
    # (the at-grade test of the records on heights no tile's window changes, both sides of it: the roads every STEP_M
    # (a segment's start within STEP_M of the nearest point) on their graded surface within XING_GRADED_M of the tile,
    # where a run reaching it can be, the same at a point whichever tile asks (point_ground), the natural ground past
    # it; the roads within XING_GRADED_M of the tile, whatever tracks the tile has; the track's bed below. Each tile's
    # own window (_mixed) told one tile "at grade" and the next "not": a sparse road's segment start 60 m off, a bed
    # profiled on the graded ground in one tile and the natural in the next)

    def level(q):
        q = np.asarray(q, np.float64).reshape(-1, 2)
        near = ((q[:, 0] >= box[0] - XING_GRADED_M) & (q[:, 0] <= box[2] + XING_GRADED_M)
                & (q[:, 1] >= box[1] - XING_GRADED_M) & (q[:, 1] <= box[3] + XING_GRADED_M))
        z = np.empty(len(q))
        if near.any():
            z[near] = np.asarray(graded(q[near]), np.float64).ravel()
        if (~near).any():
            z[~near] = np.asarray(nat(q[~near]), np.float64).ravel()
        return z
    pool = (_crossing_pool(frame, cells, box, XING_GRADED_M) + list(tunnels)) if rails else []
    xcrossers = []
    if pool:
        dense = [(roads.densify(np.asarray(line, np.float64)[:, :2], STEP_M)[:, :2], hw) for line, hw in pool]
        allp = np.ascontiguousarray(np.concatenate([ln for ln, _hw in dense]))
        tracks = {n: (np.asarray(xy, np.float64)[:, :2], XING_TRACK_M) for n, (_f, c, kind, xy) in enumerate(rails)
                  if kind == "ground" and c != "tram" and len(xy) >= 2}
        by = roads._on_carriageways(allp, tracks, 0.0) if tracks else np.zeros(len(allp), bool)
        zx = np.empty(len(allp))
        if by.any():
            zx[by] = level(allp[by])
        if (~by).any():
            zx[~by] = np.asarray(nat(allp[~by]), np.float64).ravel()
        k = 0
        for line, hw in dense:
            xcrossers.append((line, hw, zx[k:k + len(line)]))
            k += len(line)
    # (the track's side: the bed the mesh draws, before any flush: prof, where it may decide a crossing (trust: the
    # lattice's own beds and a short gap's between two of them); a long gap's natural ground is drawn but never at grade
    # with a road (NaN: the at-grade test fails), the 3D track kept there)
    runs = [_part_crossings(xy, np.where(ok, zb, np.nan), xcrossers) if kind == "ground" and c != "tram" else None
            for (_f, c, kind, xy), (zb, _zg), ok in zip(rails, prof, trust)]
    half_g = [gauges.get(fid, gauge(c, sign_style)) / 2 + RAIL_HEAD / 2 for fid, c, _k, _xy in rails]
    return {"tunnels": tunnels, "ground": ground, "worn": worn, "gauges": gauges, "cut": cut, "rails": rails,
            "prof": prof, "all_cr": all_cr, "cr": cr, "crossers": crossers, "runs": runs, "half_g": half_g,
            "sign_style": sign_style}


def _crossing_pool(frame, cells, box, reach):
    """[(xy (k, 2), half width)]: crossing_records' roads: every ground part of every copy of each road (the cells' own
    clips of it, not the first copy met as crossings() keeps: a copy ending short of the track, met first in one tile,
    hid the copy holding the crossing there while the next tile had it) near box. Pure."""
    near = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    out = []
    for feats in cells:
        if feats is None or len(feats) == 0:
            continue
        specs = roads.road_specs(feats)
        for i in _box_hits(feats.boxes(frame), near):
            sp = specs[i]
            if sp is None:
                continue
            for ring in roads.ground_parts(feats, i):
                e, n = mapgeom.enu_xy(feats, frame, ring)
                if len(e) >= 2:
                    out.append((np.column_stack((e, n)), sp.width / 2))
    return out


FIT_SMOOTH_N = 10            # points (STEP_M) either side the natural ground is averaged over where the lattice has no bed
FIT_GAP_M = 40.0            # m: a lattice gap this short or shorter, between two points it has, is filled between them
                            # (any point within XING_RUN_MAX_M + its reach of a tile then has both inside the tile's
                            # REACH_M window); a longer one, or one running to the part's end, on the natural ground


def _beds_fit(frame, rails, ground, nat, underpasses, load=None, elev=None, trust=None):
    """BNE_RAIL_ROAD_FIT: _beds with a ground track's points railprofile's lattice has no bed for laid from what does not
    change with the asking tile: across a short gap (FIT_GAP_M) between points it has, linearly along the part from the
    two (_fill_gaps: an approach's lift kept), any other on the natural ground (nat) averaged FIT_SMOOTH_N points either
    side along the part (_smooth_ground); not railways.profile in the tile's window
    (graded within GRADED_NEAR_M of the asking tile, natural past it): the bed the mesh draws and the crossing records
    test, the same whichever tile asks. Bridges, tunnels and trams as _beds lays them (their clearances, portal grades
    and decks), and without the lattice at all (BNE_RAIL_PROFILE off, no load or elevation) _beds for every part.
    trust: a list given here gets per part a mask (n,) of the points whose bed may decide a level crossing: the
    lattice's own and a short gap's between two of them; a long gap's natural ground is drawn, never a crossing (the
    lattice off altogether: every point, as before)."""
    import railprofile

    def window():
        got = _beds(frame, rails, ground, underpasses, load, elev)
        if trust is not None:
            trust.extend(np.ones(len(xy), bool) for _f, _c, _k, xy in rails)
        return got
    if not (railprofile.ENABLED and load is not None and elev is not None and rails):
        return window()
    lat0, lon0 = round(float(getattr(frame, "lat0", 0.0)), 7), round(float(getattr(frame, "lon0", 0.0)), 7)
    new = railprofile.beds(frame, ("beds", lat0, lon0), load, point_ground(frame, elev, ("nodes", lat0, lon0)), rails)
    if new is None:
        return window()
    old = None
    out = []
    for n, (z, (_f, c, kind, xy)) in enumerate(zip(new, rails)):
        z = np.asarray(z, np.float64)
        ok = np.isfinite(z)
        if not ok.all():
            if kind != "ground" or c == "tram":
                if old is None:         # (_beds' own: the lattice where it has the point, profile's past it)
                    old = _beds(frame, rails, ground, underpasses, load, elev)
                z = np.asarray(old[n][0], np.float64)
            else:
                z, ok = _fill_gaps(xy, z, nat, with_trust=True)
        out.append((z, np.asarray(ground(xy), np.float64)))
        if trust is not None:
            trust.append(ok)
    return out


def _fill_gaps(xy, z, nat, with_trust=False):
    """z (n,) with its NaN runs filled along the part (xy (n, 2)): a run between two finite points whose arc between them
    is FIT_GAP_M or less linearly by the arc from the first (each section's length summed from it); any other run (longer,
    or reaching the part's clipped end, where a tile cannot see its far point) from the natural ground averaged along the
    part (_smooth_ground). The same at a point wherever the part was clipped, for every point within REACH_M -
    FIT_GAP_M of the clip's ends. with_trust: (z, mask) the mask True at the finite points and a short gap's (the
    points whose bed may decide a crossing). Pure given nat."""
    xy = np.asarray(xy, np.float64)[:, :2]
    z = np.array(z, np.float64)
    ok = np.isfinite(z)
    idx = np.flatnonzero(~ok)
    seg = dnp.hypot(*np.diff(xy, axis=0).T)
    smooth = None
    for run in np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1) if len(idx) else []:
        a, b = int(run[0]) - 1, int(run[-1]) + 1
        s = np.cumsum(seg[a:b]) if a >= 0 and b < len(z) else None       # (from point a: sections a..b-1)
        if s is not None and s[-1] <= FIT_GAP_M:
            z[run] = z[a] + (z[b] - z[a]) * (s[:-1] / s[-1] if s[-1] > 0.0 else 0.0)
            ok[run] = True
        else:
            if smooth is None:
                smooth = _smooth_ground(xy, nat)
            z[run] = smooth[run]
    return (z, ok) if with_trust else z


def _smooth_ground(xy, nat, n=FIT_SMOOTH_N):
    """nat(xy) (n,) averaged over the n points either side along the part (fewer at its ends), each point's own sum in
    the same order wherever the part was clipped (no running sum from the clip's start). Pure given nat."""
    g = np.asarray(nat(np.asarray(xy, np.float64)[:, :2]), np.float64).reshape(len(xy))
    k = len(g)
    acc, cnt = np.zeros(k), np.zeros(k)
    at = np.arange(k)
    for d in range(-n, n + 1):
        idx = at + d
        ok = (idx >= 0) & (idx < k)
        acc[ok] += g[idx[ok]]
        cnt[ok] += 1.0
    return acc / np.maximum(cnt, 1.0)


def _part_crossings(xy, zb, crossers):
    """crossing_records' of one ground part (xy (n, 2), its level zb (n,)): None without a crossing, else
    {blk (n,), runs (r, 2), start_t (r,), end_t (r,)} (earthfast.rail_crossing_runs): the anchors are where each
    crosser's centre line crosses the part (the section holding the crossing point) and each stretch of it that runs
    down the street on its own (STREET_INSET_M inside the carriageway for STREET_ALONG_M or more: street_along's rule
    per stretch, not summed over the part, which a distant stretch only one tile holds made a short brush by the seam
    a cut in that tile alone)."""
    import fast
    fast.require(FIT_NATIVE, "level crossing records")
    xy = np.ascontiguousarray(np.asarray(xy, np.float64)[:, :2])
    pack = _crosser_pack(crossers)
    if pack is None or len(xy) < 2:
        return None
    lines = [(np.asarray(line, np.float64)[:, :2], float(hw)) for line, hw, _lz in crossers if len(line) >= 2]
    s = _arc(xy)
    ar, alo, ahi = [], [], []
    for c, hits in enumerate(roads._crossings_by([xy], [ln for ln, _hw in lines])):
        for _pi, arc, _p in hits:
            k = int(np.clip(np.searchsorted(s, arc, side="right") - 1, 0, len(xy) - 2))
            ar.append(c)
            alo.append(k)
            ahi.append(k + 1)
    lo_, hi_ = xy.min(0), xy.max(0)
    for c, (line, hw) in enumerate(lines):
        if hw <= STREET_INSET_M:
            continue
        if ((line.min(0) - hw) > hi_).any() or ((line.max(0) + hw) < lo_).any():
            continue
        idx = np.flatnonzero(roads._on_carriageways(xy, {0: (line, hw)}, -STREET_INSET_M))
        for run in (np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1) if len(idx) else []):
            if len(run) * STEP_M >= STREET_ALONG_M:
                ar.append(c)
                alo.append(int(run[0]))
                ahi.append(int(run[-1]))
    if not ar:
        return None
    blk, runs, ts, te = fast._ef.rail_crossing_runs(xy, np.ascontiguousarray(np.asarray(zb, np.float64)), *pack,
                                                     float(CROSSING_DZ), np.asarray(ar, np.int64),
                                                     np.asarray(alo, np.int64), np.asarray(ahi, np.int64),
                                                     RUN_END_SAMPLES, RUN_END_BISECT, XING_RUN_MAX_M, XING_RAIL_OUT_M)
    if not len(runs):
        return None
    return {"blk": np.asarray(blk, bool), "runs": np.asarray(runs, np.int64), "start_t": np.asarray(ts, np.float64),
            "end_t": np.asarray(te, np.float64)}


def _insert_edges(xy, zb, rec):
    """(xy', zb', blk', src): the part's points with each crossing's edges (crossing_records: start_t on the section
    before a run, end_t on the one after it) added where they fall inside their section, bed heights (zb, flushed)
    between their neighbours'; an edge on a vertex (a fraction of 0 or 1) is that vertex, the end of the visible run
    beside it (unblocked where it was the run's own; crossing_panels ends the deck there too); src: each point's index
    in xy, -1 for an added one. Pure."""
    fast.require(584, "rail_insert_edges")
    return fast._ef.rail_insert_edges(np.asarray(xy, np.float64), zb, rec)


def crossing_panels(rec):
    """[(fid, class, line (k, 2), the rails' half gauge)]: the crossing decks of crossing_records rec, each from the 3D
    track's last point before the crossing to its first after it (the records' edges; the part's own end where the run
    reaches it), standard and narrow gauge ground parts only (rails.tile_chunks lays them). Pure."""
    out = []
    kept = rec.get("kept")          # (tile_mesh's: the parts it drew, a vertex budget's retry leaving some out)
    for n, ((fid, cls, kind, xy), r, hg) in enumerate(zip(rec["rails"], rec["runs"], rec["half_g"])):
        if r is None or kind != "ground" or cls == "tram" or (kept is not None and n not in kept):
            continue
        xy = np.asarray(xy, np.float64)[:, :2]
        n = len(xy)
        for (a, b), t0, t1 in zip(r["runs"].tolist(), r["start_t"].tolist(), r["end_t"].tolist()):
            pts = []
            if a > 0 and np.isfinite(t0) and t0 < 1.0:     # (at 1: the run's own first vertex, _insert_edges)
                pts.append(xy[a - 1] + (xy[a] - xy[a - 1]) * t0)
            pts.extend(xy[a:b + 1])
            if b < n - 1 and np.isfinite(t1) and t1 > 0.0:  # (at 0: its own last vertex)
                pts.append(xy[b] + (xy[b + 1] - xy[b]) * t1)
            if len(pts) >= 2:
                out.append((fid, cls, np.asarray(pts, np.float64), hg))
    return out


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
    fast.require(584, "rail_visible_runs")
    return fast._ef.rail_visible_runs(mask)


def _Mesh(origin):
    fast.require(584, "RailMesh")
    return fast._ef.RailMesh(origin)


def _sleepers(s, xy, u, top, i, j, gone=0.0):
    """The sleepers of a run (i..j of a part): (centres, along, across, z0, z1), every SLEEPER_EVERY from the part's
    start (the same in the tile beside); gone: the share of them left out (by a hash of their number: the same ones in
    every tile)."""
    fast.require(584, "rail_sleepers")
    return fast._ef.rail_sleepers(s, xy, u, top, i, j, gone)


PIER_ROAD_M = 1.5           # m a pier keeps off a road's carriageway (its sidewalks included) or a footway under the bridge
PIER_OBST_REACH_M = 60.0    # m round the tile the roads and footways the piers keep off are taken (a site moves up to
                            # 0.4 of a span along the deck: within reach of any tile that draws it)


def pier_obstacles(frame, cells, box, reach=PIER_OBST_REACH_M):
    """(a (m, 2), b (m, 2), half (m,)): the segments of every ground road (its carriageway's half width and its drawn
    sidewalks) and footway near box, what a bridge's piers keep off. The level crossings' list (crossers: the roads the
    track's own ground parts cross) has none of the roads under a bridge: a pier stood on the kerb of Caledonian Road
    under the North London line (the user's report, London 2026-11-12)."""
    fast.require(584, "rail_pier_obstacles")
    return fast._ef.rail_pier_obstacles(frame, cells, box, reach)


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


GRADED_NEAR_M = 40.0


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
    rows, done, street = [], set(), {}
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
            if w and _street_crossing(rxy, pt, street, ri, roads):
                continue                # (an old street track: no crossbucks at its side streets)
            kit = kits(pt, sp.cls) if kits is not None else None
            kit = kit or osm_kit(pt)
            if kit is None and rcls in CROSSING_DATA_ONLY:
                continue                # (a tram-like line in the street: only where the data says it has devices)
            kit = kit or CROSSING_KIT.get(sp.cls, CROSSING_KIT_DEFAULT)
            if w:
                kit = "cross"
            rows += _crossing_rows(pt, line, rxy, sp, kit, americas, left_hand, ground)
    return rows


# A worn line's crossing where its track runs down a street (street_along: STREET_INSET_M inside a carriageway for
# STREET_ALONG_M or more) within STREET_KIT_M of it, along the track, is an old street track's, at a side street or where
# it wanders across its own street's centre line: no devices (Fells Point's disused trolley tracks had crossbucks at
# every corner of Thames Street and South Broadway, the user's report 15, 2026-11-12, "These signs and ectra props
# shoudnt spawn")
STREET_KIT_M = 12.0


def _street_crossing(rxy, pt, memo, key, roads_):
    """Does the track rxy ((k, 2), every STEP_M) run down a street of roads_ ([(fid, spec, xy)]) within STREET_KIT_M of its
    point nearest pt? memo: {key: the track's street mask}, filled here. Pure."""
    rxy = np.asarray(rxy, np.float64)
    if len(rxy) < 2:
        return False
    m = memo.get(key)
    if m is None:
        cr = [(np.asarray(ln, np.float64)[:, :2], float(sp.width) / 2) for _f, sp, ln in roads_]
        m = np.zeros(len(rxy), bool)
        for got in street_along([rxy], cr):
            if got is not None:
                m |= got
        memo[key] = m
    i = int(np.argmin(dnp.hypot(*(rxy - np.asarray(pt, np.float64)[:2]).T)))
    k = int(round(STREET_KIT_M / STEP_M))
    return bool(m[max(0, i - k):i + k + 1].any())


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
    rail, a street along it): a carriageway cuts the track where its centre line crosses the track's, or where the track
    runs down the street inside it (street_along). rail_lines: the track parts' xy (every STEP_M). Pure."""
    if not cr or not rail_lines:
        return cr
    out = []
    along = None
    for k, (line, hw) in enumerate(cr):
        if len(line) < 2:
            continue
        if roads._crossings(rail_lines, [line]):
            out.append((line, hw))
            continue
        if along is None:
            along = street_along(rail_lines, cr)
        if along[k] is not None:
            out.append((line, hw))
    return out


# Street running: a track lying STREET_INSET_M or more inside a carriageway, along it for STREET_ALONG_M or more, runs
# down the street (Baltimore's old trolley tracks in Fells Point's Thames Street, embedded in its setts: the user's
# report 16, 2026-11-12, "Street track turned into full track"). That road cuts the 3D track like a crossing one
# (true_crossers: a street whose centre line the track never crossed left a ballasted track down the middle of the
# cobbles) and rails.py draws its street track there (street_windows). A service road ending at the track holds a chord
# of it at most 2 x (its half width - STREET_INSET_M) long: under STREET_ALONG_M for any road up to 14 m wide.
STREET_ALONG_M = 12.0
STREET_INSET_M = 1.0


def street_along(rail_lines, cr, step=STEP_M):
    """Per road of cr ([(xy, half width)]): the mask over the concatenated track points (rail_lines, every step m) that lie
    STREET_INSET_M or more inside its carriageway when STREET_ALONG_M or more of track does, else None. Pure."""
    out = [None] * len(cr)
    lines = [np.asarray(xy, np.float64)[:, :2] for xy in rail_lines if len(xy)]
    if not lines or not cr:
        return out
    pts = np.concatenate(lines)
    lo, hi = pts.min(0), pts.max(0)
    for k, (line, hw) in enumerate(cr):
        line = np.asarray(line, np.float64)
        if len(line) < 2 or hw <= STREET_INSET_M:
            continue
        llo, lhi = line[:, :2].min(0) - hw, line[:, :2].max(0) + hw
        if (llo > hi).any() or (lhi < lo).any():
            continue
        m = roads._on_carriageways(pts, {0: (line[:, :2], float(hw))}, -STREET_INSET_M)
        if m.sum() * step >= STREET_ALONG_M:
            out[k] = m
    return out


def street_spans(xy, cr, pad=4.0):
    """The arc ranges [(a, b)] (m along track xy (k, 2), any spacing) where it runs down a street of cr ([(centreline,
    half width)]: street_along, the track resampled every STEP_M), each padded by pad m. Pure."""
    xy = np.asarray(xy, np.float64)[:, :2]
    if len(xy) < 2 or not cr:
        return []
    s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]
    ss = np.arange(0.0, float(s[-1]) + 1e-9, STEP_M)
    pts = np.c_[dnp.interp(ss, s, xy[:, 0]), dnp.interp(ss, s, xy[:, 1])]
    hit = np.zeros(len(pts), bool)
    for m in street_along([pts], cr):
        if m is not None:
            hit |= m
    return [(max(0.0, float(ss[i]) - pad), min(float(s[-1]), float(ss[j]) + pad))
            for i, j in _runs(~hit)]


def walk_ways(frame, cells, box, reach=60.0):
    """[(xy (k, 2), width)]: the footways, paths, cycleways and pedestrian ways on the ground near box (the ways a person
    walks across a track by). Pure."""
    fast.require(584, "rail_walk_ways")
    return fast._ef.rail_walk_ways(frame, cells, box, reach)


WALK_STRICT_ON = os.environ.get("BNE_RAIL_WALK_STRICT", "on") not in ("off", "0", "false")
WALK_STRICT_SIN = 0.6       # a way crossing a track flatter than this (37 deg) is running along it, not across (plan 040:
                            # Kalynivka's path 20 deg to the line got panels 8 m long and a row of signs)
WALK_LEAVE_M = 12.0         # m along the way either side of the crossing ...
WALK_LEAVE_OFF_M = 5.0      # ... it must be this far from the track's line (it leaves the track on both sides)


def _leaves_track(way, pt, track):
    """Does the way (k, 2) go away from the track line (n, 2) on both sides of its crossing pt: its points WALK_LEAVE_M
    along it either side lie WALK_LEAVE_OFF_M or more off the track? A way that ends before (a short stub) counts by
    its end. Pure."""
    fast.require(584, "rail_leaves_track")
    return fast._ef.rail_leaves_track(way, pt, track)


def walk_crossings(rails, ways, nodes, carriage, ex):
    """The places a person crosses a ground track: [(point (2,), arc m, track part index, track unit t, way unit f, way
    width)]. Where a footway, path, cycleway or pedestrian way (ways: walk_ways) crosses a track at WALK_MIN_SIN or more, and
    where OSM has a railway=crossing node on a track (nodes: [(x, y)]) with no mapped way to it: the way then is taken square
    to the track, WALK_NODE_W wide (a crossing that leads nowhere on the map is still one). None beside a road's deck (the
    sidewalk goes over it with the road: carriage [(xy, half width)]). Only the points inside ex. Pure."""
    fast.require(584, "rail_walk_crossings")
    return fast._ef.rail_walk_crossings(rails, ways, nodes, carriage, ex)


def walk_groups(crossings, rails):
    """The crossings (walk_crossings) of one way over neighbouring tracks as one place: [[crossing, ...]] each sorted along
    the way. Crossings of the same way (its unit f and width) within WALK_GROUP_M of each other along it are one group."""
    fast.require(584, "rail_walk_groups")
    return fast._ef.rail_walk_groups(crossings)


def walk_panel_boxes(crossings, finals, rails=None):
    """The boxes of footway crossings (walk_crossings): [(centres (1, 2), along (1, 2), across (1, 2), z0 (1,), z1 (1,)),
    hx, hy] for the panels between and outside the rails, flush with their heads, with a flangeway beside each rail, and
    between two neighbouring tracks of one crossing a panel over the gap (the ballasts' sides dipped there: a ditch across
    the street, Nachod's Kladska 2026-11-09)."""
    fast.require(584, "rail_walk_panels")
    return fast._ef.rail_walk_panels(crossings, finals)


def walk_sign_rows(crossings, ground, finals=None, rails=None, streets=()):
    """Prop rows ([rrwalk, x, y, z, yaw, scale, height, 0]) of the warning signs at footway crossings: one at each end of
    a crossing of one or more neighbouring tracks (walk_groups), facing the people coming, on their right, past the outer
    track's ballast, never on another track's bed (WALK_SIGN_CLEAR_M from every track's axis). streets: the crossing
    points of streets mapped as pedestrian ways (an OSM level crossing node: their gates are the crossing's devices) get
    none."""
    fast.require(584, "rail_walk_signs")
    return fast._ef.rail_walk_signs(crossings, ground, finals, rails, streets)


def dead_ends(rails, finals, worn, near, carriage=None, cut_ends=None, gauges=None):
    """The true ends of the tracks: [(end point (2,), unit direction of travel into it (2,), rail head height)]. A true end
    is the end of a ground part no other part joins and none passes within BUFFER_JOIN_M, well inside the window's reach
    (a clipped end is not one); abandoned lines (worn 2) and trams have none, nor an end on a carriageway of carriage
    ([(centreline, half width)]: a street track's end in the street, the user's report 15). With BNE_RAIL_ROAD_FIT
    neither has an end a worn line's cut at a road made (cut_ends: parts' provenance, end_key) nor one whose track goes
    on past a road (_road_ends). Pure."""
    fast.require(584, "rail_dead_ends")
    return fast._ef.rail_dead_ends(rails, finals, worn, near, carriage, cut_ends, gauges)


ACROSS_M = 40.0             # m ahead of a track's end looked along for a road and a track beyond it (_road_ends)
ACROSS_STEP_M = 0.5
ACROSS_JOIN_M = 3.0         # m from that line ahead a track's point beyond the road is the line going on ...
ACROSS_COS = 0.9396926207859084     # ... running along it (within 20 degrees) ...
ACROSS_DZ = CROSSING_DZ     # ... with its bed this near the end's


def _road_ends(ends, rails, finals, carriage, gauges=None):
    """BNE_RAIL_ROAD_FIT: per end of ends ([(p, unit d running into it, z, its part's index in rails)]): does the same
    track go on past a road there: the line ahead (ACROSS_STEP_M apart, ACROSS_M) crosses a carriageway of carriage
    ([(centreline, half width)]) and, past its far edge, another part of rails has a point within ACROSS_JOIN_M of the
    line, running on from it away from the road (ACROSS_COS), of the same gauge (gauges {fid: m}, else gauge(class)),
    its rail heads (finals) within ACROSS_DZ of the end's: the sidings mapped up to each side of a paved-over crossing
    (Hahnstaetten's, the user's report r92). A siding's real end before a street keeps its stop, a through track
    beside it notwithstanding. earthfast.rail_road_ends. Pure."""
    import fast
    import roads
    fast.require(FIT_NATIVE, "buffer stops at roads")
    segs = roads._Segs({n: (np.asarray(ln, np.float64)[:, :2], float(hw)) for n, (ln, hw) in enumerate(carriage)
                        if len(ln) >= 2})
    rows = [np.asarray(oxy, np.float64).reshape(len(oxy), -1)[:, :2] for *_x, oxy in rails]
    off = np.zeros(len(rows) + 1, np.int64)
    off[1:] = np.cumsum([len(r) for r in rows])
    q = np.ascontiguousarray(np.concatenate(rows)) if rows else np.zeros((0, 2))
    qz = (np.ascontiguousarray(np.concatenate([np.asarray(z, np.float64)[:len(r)] + RAIL_TOP_M
                                               for z, r in zip(finals, rows)])) if rows else np.zeros(0))
    p = np.ascontiguousarray(np.asarray([e[0][:2] for e in ends], np.float64).reshape(-1, 2))
    d = np.ascontiguousarray(np.asarray([e[1][:2] for e in ends], np.float64).reshape(-1, 2))
    z = np.asarray([e[2] for e in ends], np.float64)        # (the rail heads' heights, as qz)
    part = np.asarray([e[3] for e in ends], np.int64)
    g = np.asarray([(gauges or {}).get(fid, gauge(c)) for fid, c, *_r in rails], np.float64)
    return fast._ef.rail_road_ends(p, d, z, part, q, qz, off, segs.a, segs.b, segs.h, g, ACROSS_STEP_M, ACROSS_M,
                                   ACROSS_JOIN_M, ACROSS_COS, float(ACROSS_DZ))


def buffer_rows(ends):
    """Prop rows ([rrbuffer, x, y, z, yaw, scale, height, 0]) of the buffer stops: the model's face towards the train, its
    block BUFFER_BACK_M before the track's end (the model's +Y is the direction the train runs, its +X the right of it)."""
    fast.require(584, "rail_buffer_rows")
    return fast._ef.rail_buffer_rows(ends)


SWITCH_M = 1.7              # m: a track point this near another track's line, crossing it at SWITCH_MIN_SIN or more, is a
SWITCH_MIN_SIN = 0.087      # switch or a crossing: no collider box there (the flange of the through track would strike the side
                            # track's rail; a real switch's blades and frog leave the gap)


def _switch_masks(rails):
    """Per part: bool (n,), the points inside a switch (SWITCH_M from another part's line, crossing it at SWITCH_MIN_SIN or
    more; parallel and duplicated lines are left alone). Pure, order of rails alone."""
    fast.require(584, "rail_switch_masks")
    return fast._ef.rail_switch_masks(rails)


def _foot_limits(xy, side, foot, others, crossers):
    """Per vertex: the ballast foot's width, clipped at a road's carriageway edge or another track's deck so a fill's
    side is a wall there, not a slope over the road/track (the wide double-track embankment's foot ran over the road
    under it and the line beside it: a hollow overhang the player could drive into)."""
    fast.require(584, "rail_foot_limits")
    return fast._ef.rail_foot_limits(np.asarray(xy, np.float64), np.asarray(side, np.float64), foot, others, crossers)


def tile_mesh(frame, tx, ty, cells, ground, sign_style=None, far_ground=None, return_decks=False, load=None,
              bed_elev=None, _drop=frozenset(), furniture=None, under=None, records=None):
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
    every tile (railprofile.py). records: crossing_records of the same arguments (BNE_RAIL_ROAD_FIT: made here when not
    given; the caller hands the same ones to rails.tile_chunks for the crossing decks)."""
    fast.require(584, "rail_tile_mesh")
    return fast._ef.rail_tile_mesh(frame, tx, ty, list(cells), ground, sign_style, far_ground, return_decks,
                                   load, bed_elev, _drop, furniture, under, records)


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
