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
import heapq
import math
import re

import os
import threading

import numpy as np
import fast
import dmath
import dnp

import findground
import footbridges
import gradelaw
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
AT_GRADE_END_M = 6.0        # m: a road crossing a bridge closer than this to its structure's end meets it at grade (grading's
                            # APPROACH_MIN_CROSS_M): no road dips there, so findground's cost law is not asked and the
                            # bridge rises (a 7 m rail bridge over a roundabout: the arms have no room to dip, the
                            # drivability ratchet's Compiegne, 3.9 -> 41.4 when they were asked to)
GRADE = 0.05                # THE grade law of a track, read by everything that climbs one (the decks' and the approaches' ramps,
                            # the lifts run along the line, the shapes' laws gradeshapes.max_grade, the seam's spread
                            # railroadseam): 5 %, a car game's (the user, 2026-11-18: "rail grade to 5% is a car game after
                            # all"). It was 2 % here (a main line's), 2.5 % in gradeshapes and 2 % in railroadseam: three
                            # laws for one track. The survey's tracks climb 2.5 % over 100 m at the 99th percentile, 2.8 % the
                            # steepest on the ground (the Gotthard line), 4.1 % on a bridge (Zurich's Letzigraben flyover ramps:
                            # Earth's, at 2 %, stood 3 m over the real one's climb). Light rail's own 5 % (LINE_GRADE, light
                            # rail climbs to 5-6 %) is this now: one value for every class.
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


TRACK_Z_HOOK = None     # grading sets it (BNE_SHAPES): hook(frame, rails) -> [bed (k,) or None] per part: the bed an interchange's
#                         solve gave the track (NaN where it gave none), in frame's heights. The tile draws what was solved.
_OWN = threading.local()


class own_beds:
    """With this, the beds are the railways' own: no interchange's solve read back. The interchange's own steps read them so
    (the solve is made from the bed the railways lay; reading its answer back there would ask the solve it is making)."""

    def __enter__(self):
        _OWN.depth = getattr(_OWN, "depth", 0) + 1

    def __exit__(self, *a):
        _OWN.depth -= 1


def _read_back(frame, rails, out):
    """out ([(bed, ground)] per part) with the beds an interchange's solve gave tracks (TRACK_Z_HOOK) in place of the
    railways' own where it gave them."""
    hook = TRACK_Z_HOOK
    if hook is None or getattr(_OWN, "depth", 0) or not rails:
        return out
    got = hook(frame, rails)
    if not got:
        return out
    res = []
    for (zb, zg), z in zip(out, got):
        if z is not None:
            z = np.asarray(z, np.float64)
            zb = np.where(np.isfinite(z), z, np.asarray(zb, np.float64))
        res.append((zb, zg))
    return res


def _beds(frame, rails, ground, underpasses, load=None, elev=None, raw=None):
    """_beds_own, with the beds a solved interchange gave its tracks read back (_read_back)."""
    return _read_back(frame, rails, _beds_own(frame, rails, ground, underpasses, load, elev, raw))


def _beds_own(frame, rails, ground, underpasses, load=None, elev=None, raw=None):
    """railways.profile of rails, its beds from railprofile's lattice of node profiles (the same bed in every tile and
    grading cell: plan 035) where load (key -> roads layer) and elev (the grader's DEM) are given; railways.profile's own
    where not, for a tram and for a point no node has. raw: ground(xy) -> the DEM's heights as they are (the grader's,
    in its planning frame); without it (the mesh, in an anchor's frame) the beds are the grader's lattice's
    (_region_beds), in the frame's heights."""
    import railprofile
    new = None
    if railprofile.ENABLED and load is not None and (elev is not None or raw is not None) and rails:
        if raw is None:
            new = _region_beds(frame, load, elev, rails)
        else:
            lat0, lon0 = round(float(getattr(frame, "lat0", 0.0)), 7), round(float(getattr(frame, "lon0", 0.0)), 7)
            new = railprofile.beds(frame, ("beds-dem", lat0, lon0), load, raw, rails)
    if new is None or any(np.isnan(z).any() for z in new):
        old = profile(rails, ground, underpasses)
        if new is None:
            return old
        return [(np.where(np.isfinite(z), z, zb), zg) for z, (zb, zg) in zip(new, old)]
    return [(z, np.asarray(ground(xy), np.float64)) for z, (_f, _c, _k, xy) in zip(new, rails)]


def dem_ground(frame, elev):
    """ground(xy (n, 2)) -> the DEM's heights at frame's points (elev(lat, lon), as they are), remembered per point and
    read at each centimetre key's own point: grade_plans' ground, and the mesh's reading of the same lattice
    (_region_beds)."""
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
    return ground


# The mesh reads its beds from the grader's lattice, not from one of its anchor's frame. A frame's lattice is a pure
# function of it, but a node's bed depends on its window wherever something past MARGIN_M shapes it (a raised deck's
# ramp runs on at GRADE for up to SEED_LIFT_MAX_M / GRADE), and two frames' nodes stand at other places: north of the
# SR17's underpass at Popoli (42.1712 N 13.8250 E) the nodes of the Rome-Pescara line's track were 7 m apart, the
# grader's blend stood the ground 1.4-1.7 m over the anchor's blend the track was drawn at, and the wagons hit it.
def _region_beds(frame, load, elev, rails):
    """railprofile.beds of rails (parts in frame, an anchor's) as the grader lays them: in each point's planning region's
    frame (geotiles.region_frame, PlanStore.frame_of's), on the DEM's heights (dem_ground), from that frame's lattice
    ("beds-dem"), the points mapped into it as the plans are mapped out of it (geotiles.world_xy_map); the heights then
    in frame's (as point_ground reads the ground for the mesh). NaN where no node has the point."""
    import geotiles
    import railprofile
    pts = [np.asarray(r[3], np.float64).reshape(-1, 2) for r in rails]
    off = np.r_[0, np.cumsum([len(p) for p in pts])].astype(np.int64)
    z = np.full(int(off[-1]), np.nan)
    if not len(z):
        return np.split(z, off[1:-1])
    allxy = np.concatenate(pts)
    la, lo, _ = frame.enu_to_geodetic(allxy[:, 0], allxy[:, 1], 0.0)
    la, lo = np.asarray(la, np.float64).ravel(), np.asarray(lo, np.float64).ravel()
    rx, ry = geotiles.region_of(la, lo)
    rx, ry = np.atleast_1d(rx), np.atleast_1d(ry)
    for a, b in sorted({(int(a), int(b)) for a, b in zip(rx.tolist(), ry.tolist())}):
        rf = geotiles.region_frame(a, b)
        akey = ("beds-dem", round(float(rf.lat0), 7), round(float(rf.lon0), 7))
        here = (rx == a) & (ry == b)
        asks, idx = [], []
        for n, (fid, cls, kind, _xy) in enumerate(rails):
            m = np.flatnonzero(here[off[n]:off[n + 1]])
            if len(m):
                asks.append((fid, cls, kind, geotiles.world_xy_map(frame, rf, pts[n][m])))
                idx.append(off[n] + m)
        for k, h in zip(idx, railprofile.beds(rf, akey, load, dem_ground(rf, elev), asks)):
            z[k] = h
    ok = np.isfinite(z)
    if ok.any():
        z[ok] = np.asarray(frame.geodetic_to_enu(la[ok], lo[ok], z[ok])[2], np.float64)
    return np.split(z, off[1:-1])


def track_beds(frame, cells, box, elev, load=None):
    """(rails, beds, worn): the rail parts near box (parts', GRADE_REACH_M round it: [(fid, class, kind, xy (k, 2))]),
    each one's bed (k,) in elev's heights (elev(lat, lon): the grader's DEM) and {fid: wear} of the worn lines. The
    track's height wherever the grader asks it: grade_plans lays the ground to these beds, the shapes and the underpasses
    clear these decks (grading), tile_mesh draws them. Pure given elev."""
    cells = list(cells)
    tcls = []
    tunnels = road_underpasses(frame, cells, box, GRADE_REACH_M, classes=tcls)
    worn = {}
    rails = parts(frame, cells, box, reach=GRADE_REACH_M, clip=True, worn=worn, underpasses=tunnels)
    if not rails:
        return [], [], worn
    ground = dem_ground(frame, elev)
    tunnels = raising_underpasses(tunnels, tcls) + plain_underpasses(frame, cells, box, rails, GRADE_REACH_M, tunnels)
    prof = _beds(frame, rails, ground, tunnels, load, elev, raw=ground)
    if railxing.ENABLED:        # (the bed flush with the road at a level crossing: the ground is graded to it, else the
        prof = _flushed(frame, cells, box, rails, prof, ground, tunnels)     # ballast and sleepers lay under the ground)
    return rails, [np.asarray(zb, np.float64) for zb, _zg in prof], worn


def bed_at(rails, beds, xy, fid=None, distance=False, bridge=False):
    """The bed (n,) at points xy (n, 2) on a track's own line (track_beds' rails and beds): each point's on the part
    nearest it (of line fid only, when given), between the two samples of its segment there. NaN where there is no
    part (the railways lay none of that line: a stub they leave out). distance: (bed, how far the nearest part is);
    bridge: (bed, on_bridge (n,) bool: that part is a bridge, a deck over what is under it); both: (bed, distance,
    on_bridge). Pure.

    Every segment of every part at once (the nearest of all, the first on a tie: the earliest part, as parts taken one
    after another with a nearer one replacing it only when strictly nearer), the points in blocks that keep the
    points x segments arrays small: a part at a time was 500 parts' arrays of a yard for each of the 3 points a
    crossing asked (379 calls, 11 of 200 CPU-s of a Zurich + Gotthard replay)."""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    out = np.full(len(xy), np.nan)
    near = np.full(len(xy), np.inf)
    on = np.zeros(len(xy), bool)

    def answer():
        got = (out,) + ((near,) if distance else ()) + ((on,) if bridge else ())
        return got if len(got) > 1 else out
    use = [(np.asarray(pxy, np.float64), np.asarray(zb, np.float64), kind == "bridge")
           for (f, _c, kind, pxy), zb in zip(rails, beds) if not ((fid is not None and f != fid) or len(pxy) < 2)]
    if not use or not len(xy):
        return answer()
    a = np.concatenate([p[:-1] for p, _z, _b in use])
    ab = np.concatenate([np.diff(p, axis=0) for p, _z, _b in use])
    z0 = np.concatenate([z[:-1] for _p, z, _b in use])
    z1 = np.concatenate([z[1:] for _p, z, _b in use])
    deck = np.concatenate([np.full(len(p) - 1, b) for p, _z, b in use])
    length2 = np.maximum((ab * ab).sum(1), 1e-12)[None]
    block = max(1, 2_000_000 // len(a))
    for i0 in range(0, len(xy), block):
        q = xy[i0:i0 + block]
        t = np.clip(((q[:, None, :] - a[None]) * ab[None]).sum(2) / length2, 0.0, 1.0)
        d = dnp.hypot(*(a[None] + t[:, :, None] * ab[None] - q[:, None, :]).transpose(2, 0, 1))
        j = np.argmin(d, axis=1)
        tj = t[np.arange(len(q)), j]
        out[i0:i0 + len(q)] = z0[j] + (z1[j] - z0[j]) * tj
        near[i0:i0 + len(q)] = d[np.arange(len(q)), j]
        on[i0:i0 + len(q)] = deck[j]
    return answer()


def grade_plans(frame, cells, box, elev, load=None):
    """[(fid, xy (k, 2), z (k,))]: the bed line of each on-ground rail part near box, in elev's heights (elev(lat, lon):
    the grader's DEM), for grading.Grader to lay the ground to (the cutting's walls back from the track, the ramps'
    embankments filled). tile_mesh then finds the ground at its bed. Pure given elev."""
    rails, beds, worn = track_beds(frame, cells, box, elev, load)
    ungraded = {fid for fid, w in worn.items() if w >= UNGRADED_WEAR}
    pad = GRADE_HALF_M + 15.0
    out = []
    for (fid, _c, kind, xy), zb in zip(rails, beds):
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
    out = fast._ef.rail_parts_select(idx, cells, tuple(float(v) for v in near), _rail_ranges, _rail_ground_parts,
                                     wear_of, kept, gauges, TUNNELS_ON, RAIL_LIFECYCLE)
    if any(kind == "ground" and wear_of.get(fid) for fid, _c, kind, _xy in out):
        out = _cut_worn_at_motorways(out, wear_of, motorway_crossings(frame, cells, box, reach), kept, cut_ends)
    out = _drop_stubs(out)
    return _span_clip(out, road_underpasses(frame, cells, box, reach) if underpasses is None else underpasses,
                      near if clip else None)



def _rail_ranges(feats, i):
    """roads.level_ranges of rail feature i with its short tunnels taken out (laid on the ground, as _pieces): short by
    the run of the tunnel they are a piece of (tunnel_run_m, through the map's splits at switches), not by the piece's
    own length: rail_parts_select's own short test (by the piece alone) is given 0, so a 450 m tunnel split into 6-55 m
    pieces (Birmingham's lines under the Bullring) is not laid on the ground in the mesh while gradesep and the shapes
    have it in its tunnel."""
    return [r for r in roads.level_ranges(feats, i)
            if not (r[2] == "tunnel" and tunnel_run_m(feats, i, r[0], r[1]) < SHORT_TUNNEL_M)]


def _rail_ground_parts(feats, i):
    """rail_parts_select's ground pieces of a feature _rail_ranges left no range: its whole rings where the map's ranges
    were all short tunnels (laid on the ground, as _pieces), roads.ground_parts (which leaves every tunnel out) else."""
    if roads.level_ranges(feats, i):
        return [ring for part in feats.parts(i) for ring in part if len(ring) >= 2]
    return roads.ground_parts(feats, i)


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
           0.0)
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
        float(roads.MIN_PART), float(MIN_PART_M), 0.0)
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


def _lonlat(feats):
    """feats.lonlat itself where it is (N, 2) float64 (RailIndex tells its ground_parts views by it), else a copy."""
    ll = feats.lonlat
    if isinstance(ll, np.ndarray) and ll.dtype == np.float64 and ll.ndim == 2 and ll.shape[1] == 2:
        return ll
    return np.asarray(ll, np.float64).reshape(-1, 2)


# A railway over a road the map has in a short tunnel stays at its bed, the road under dips (grading's _lower_underpasses,
# _over_mapped_underpass: the same test): only a motorway's or a trunk's tunnel (never lowered, grading.APPROACH_SKIP)
# raises the track over it. Raised over every such tunnel, the track stood 4.7 m up on Popoli's two short bridge pieces
# over the SR17 and a service road while grading left both roads at the ground under them: the trucks hit the decks and the
# track stepped 4.5-5.2 m down to the ground beside them (the user, 2026-11-16: "rail are continuous like roads").
TUNNEL_AT_GRADE_ON = os.environ.get("BNE_RAIL_TUNNEL_AT_GRADE", "on") != "off"
RAISE_OVER = ("motorway", "trunk")      # (grading.APPROACH_SKIP)


def raising_underpasses(tunnels, classes):
    """The road tunnels of road_underpasses (classes: their road classes, its list) the track is raised over: a motorway's
    or a trunk's (TUNNEL_AT_GRADE_ON), every one without the rule."""
    if not TUNNEL_AT_GRADE_ON:
        return list(tunnels)
    return [t for t, c in zip(tunnels, classes) if str(c) in RAISE_OVER]


def road_underpasses(frame, cells, box, reach=REACH_M, classes=None):
    """Short road tunnels near box as (xy, half width), including source tunnels made ground by gradesep. classes: a
    list that gets each one's road class, in the same order (grading's UNDER_TUNNEL_FIXED_ON); without it the native
    selection (_road_underpasses_native: the same tunnels, the classes not kept)."""
    if classes is None:
        return _road_underpasses_native(frame, cells, box, reach)
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
                floor = roads.part_floor(feats, i)
                missing = [(a, b, kind) for a, b, kind in tagged
                           if not any(c <= a + floor and d >= b - floor for c, d in effective)]
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
                    if classes is not None:
                        classes.append(sp.cls)
    return out


def _road_underpasses_native(frame, cells, box, reach=REACH_M):
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


# A railway's bridge over a road on the ground (findground: a bridge over a plain way leaves the ground, the plain way
# is the ground). Where the track's bed between the bridge's ends lacks the road's headroom over the road's ground, the
# track rises over it (the road's piece RISE_PIECE_M either side of the crossing, as a tunnel's mouths) and ramps at
# GRADE; the road is never dug (grading._lower_underpasses takes no rail span). Whatever the elevation and the bridge's
# length: on the coarse global model the embankment is smeared away (Perm, GitHub #414: the underpass rule dug
# Первомайская улица 7.5 m under the Балмошная–Лёвшино line, a walled trench 400 m long where the street is at grade
# under an embankment); on lidar a viaduct is taken out of it and a bridge built after the survey is not in it yet
# (Zurich's Wipkingen viaduct, 13 m over its streets, which were dug under it; Davis Road under the Armadale Line at
# Kelmscott, WA, dug 8 m, the user 2026-10-29); a short bridge on level ground is a bridge all the same (the user,
# 2026-11-17: the map wins; Nachod's was made a level crossing). A track on the ground under the bridge is a plain way
# as a road is, with a track's headroom (roads.RAIL_HEADROOM_M): Zurich's Letzigraben flyover, 1.2 km over the tracks
# into the main station, lay 2 to 5 m over them.
RISE_PIECE_M = 25.0
OWN_TUNNEL_M = 1.0      # m: a road crossing under a rail bridge this near a mapped road tunnel's line is in that tunnel


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


def _seg_arc(line, pt, s):
    """The arc (m) along line of its point nearest pt (s: _arc(line), made once for all the points asked)."""
    line = np.asarray(line, np.float64)[:, :2]
    ab = np.diff(line, axis=0)
    t = np.clip(((pt - line[:-1]) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
    j = int(np.argmin(dnp.norm(line[:-1] + t[:, None] * ab - pt, axis=1)))
    return float(s[j] + t[j] * (s[j + 1] - s[j]))


class _Rise(tuple):
    """(xy, half width) of a plain road's piece a rail bridge may rise over (as road_underpasses' entries), with clear: m
    the track's bed stands over the road's ground there at least, and grade: the grade the road dips at (the bridge
    rises only where findground says it is the one that moves: profile)."""

    def __new__(cls, xy, half, clear, grade):
        t = super().__new__(cls, (xy, half))
        t.clear = float(clear)
        t.grade = float(grade)
        return t


def plain_underpasses(frame, cells, box, rails, reach=REACH_M, tunnels=None):
    """[_Rise] (as road_underpasses): each plain way's piece RISE_PIECE_M either side of where a rail bridge part
    crosses over it, the track's bed over it by the way's headroom under the deck's own depth. A ground road's: its
    signed limit, else the law's (roads.signed_road_clear, deck_road_clear); a track's on the ground (rails' ground
    parts): a track's (roads.deck_rail_clear), not where the two meet end to end (the bridge's own approach). Not a
    road where the map has it in its own short tunnel under the bridge (tunnels: road_underpasses' of the window, made
    here when not given): by the map that way is a tunnel, which goes down while the track stays (findground), though
    the server's roads have it as an open road under a bridge by then (gradesep: the tunnel made ground, the track over
    it a bridge). Popoli's SS17, 19 m of tunnel under the two tracks of the Roma - Pescara line: taken for a plain
    road, both tracks rose 5 to 8 m over it."""
    bridges = [xy for (_fid, _c, kind, xy) in rails if kind == "bridge" and len(xy) >= 2]
    if not bridges:
        return []
    if tunnels is None:
        tunnels = road_underpasses(frame, cells, box, reach)
    limits = height_limits(frame, cells)
    depth = DECK_D - BALLAST_H
    ways = [(line, half, None, g) for line, half, g in graded_crossings(frame, cells, box, reach) if len(line) >= 2]
    ways += [(xy, BALLAST_TOP_M, roads.deck_rail_clear(depth), GRADE) for (_fid, _c, kind, xy) in rails
             if kind == "ground" and len(xy) >= 2]
    out = []
    # (every segment of the mapped tunnels at once: a crossing is in one when it lies within OWN_TUNNEL_M of any, the
    # nearest of all the segments the nearest of each tunnel's; a call per tunnel and crossing was 7 of the 200 profiled
    # CPU-s of a Zurich + Gotthard replay)
    own = [tl for tl, _th in tunnels if len(tl) >= 2]
    own_a = np.concatenate([tl[:-1] for tl in own]) if own else None
    own_b = np.concatenate([tl[1:] for tl in own]) if own else None
    for (line, half, track, grade), hits in zip(ways, roads._crossings_by(bridges, [w[0] for w in ways])):
        if not hits:
            continue
        sl = _arc(line)
        for hit in hits:
            pt = np.asarray(hit[2], np.float64)[:2]
            if track is not None and min(float(dnp.hypot(*(e - pt))) for e in (bridges[hit[0]][0], bridges[hit[0]][-1],
                                                                              line[0], line[-1])) < STEP_M:
                continue                        # (a joint: the track runs on from the bridge, or ends at it)
            if track is None and own and float(roads._seg_dist(pt[None], own_a, own_b).min()) <= OWN_TUNNEL_M:
                continue                        # (in its own mapped tunnel there)
            sc = _seg_arc(line, pt, sl)
            a0, a1 = max(0.0, sc - RISE_PIECE_M), min(float(sl[-1]), sc + RISE_PIECE_M)
            if a1 - a0 < 1.0:
                continue
            ss = np.linspace(a0, a1, max(3, int((a1 - a0) / STEP_M) + 1))
            piece = np.c_[dnp.interp(ss, sl, line[:, 0]), dnp.interp(ss, sl, line[:, 1])]
            if track is not None:
                clear = track
            else:
                sign = roads.limit_at(limits, pt, LIMIT_NEAR_M) if limits else None
                clear = roads.signed_road_clear(sign, depth) if sign is not None else roads.deck_road_clear(depth)
            out.append(_Rise(piece, half + UNDERPASS_SIDE_M, clear, grade))
    return out


def _underpass_distance(xy, line):
    """Distance to a finite tunnel centreline, and fraction along it, per point of xy (k, 2): the first nearest of its
    segments (of length 1e-6 m or more), inf and 0 where none (earthfast.rail_underpass_distance). Pure."""
    import fast
    fast.require(487, "rail_underpass_distance")
    xy, line = roads._lines64([xy, line])
    return fast._ef.rail_underpass_distance(xy, line)


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


def _roads_under_bridges(frame, cells, box, reach, rails):
    """[(line, half width)]: the roads on the ground that pass under a track's mapped bridge (the bridge parts of
    rails cross their lines), for _spans_over_roads."""
    bridges = [xy for (_fid, _c, kind, xy) in rails if kind == "bridge" and len(xy) >= 2]
    if not bridges:
        return []
    ways = [(np.asarray(line, np.float64)[:, :2], float(half))
            for line, half, _g in graded_crossings(frame, cells, box, reach) if len(line) >= 2]
    return [w for w, hits in zip(ways, roads._crossings_by(bridges, [w[0] for w in ways])) if hits]


def _spans_over_roads(rails, prof, ways):
    """(rails, prof) with each ground part's run over a road that passes under the track's bridge (ways:
    _roads_under_bridges') drawn as a span: the bridge goes on past the road's carriageway and the bed's feet
    (_span_clip's reach, FOOT_MAX_M + STEP_M past its half width), at the beds already made. The map's bridge often ends
    at the road's centre line, not past its edge: Vienna's primary road under the five tracks by the A23, the bridges
    ending 2.8 m from its centre on a 4.3 m half width, the ground tracks' beds stood on its carriageway and their walls
    rose 7 m out of it. Drawn only: which of the two moves (the track rises, the road goes under) is decided on the
    mapped bridge (_structure_run, grading's own), so the decks' and the beds' heights are those of the mapped parts."""
    if not ways or not rails:
        return rails, prof
    import fast
    fast.require(487, "rail_span_clip")
    pi, ai, bi, br = fast._ef.rail_span_clip(roads._lines64([xy for *_, xy in rails]),
                                             [kind == "ground" for _f, _c, kind, _xy in rails],
                                             roads._lines64([ln for ln, _h in ways]), [float(h) for _ln, h in ways],
                                             FOOT_MAX_M + STEP_M, None)
    out_r, out_p = [], []
    for n, a, b, bridge in zip(pi.tolist(), ai.tolist(), bi.tolist(), br.tolist()):
        fid, cls, kind, xy = rails[n]
        zb, zg = prof[n]
        if a == 0 and b == len(xy) - 1 and not bridge:
            out_r.append(rails[n])
            out_p.append(prof[n])
            continue
        out_r.append((fid, cls, "bridge" if bridge else kind, xy[a:b + 1]))
        out_p.append((np.asarray(zb)[a:b + 1], np.asarray(zg)[a:b + 1]))
    return out_r, out_p


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


TUNNEL_NODE_M = 0.5         # m: two lines' ends this close are one node (the map's segments of a line share their end)


def tunnel_run_m(feats, i, a, b):
    """m of the tunnel that feature i's tunnel range (a, b) (fractions of it) is a piece of, as SHORT_TUNNEL_M reads
    it: its own length and, from each end of the feature it reaches, the tunnel going on through the drawn railways
    joined there (their map ranges, roads.tag_level_ranges: a tunnel reaching that end), the longest way, counted as
    far as SHORT_TUNNEL_M (enough to tell). The map splits a line at every switch: the tracks east of Birmingham New
    Street run 450 m under the Bullring at level -2 in segments of 7 to 55 m, each taken for a short tunnel under a
    road bridge and laid on the ground in the city centre; Worcester Street over them was bridged (gradesep) and its
    own short tunnel dug 7 m under two of them, 8 m on from the deck that cleared four: its shapes infeasible. A node
    outside the cell is read with the neighbouring cells' lines (mapdata's _bnge_neighbours): every copy of a line
    finds the same run."""
    L = roads._feature_length_m(feats, i)
    own = (b - a) * L
    ends = [at_end for at_end, gap in ((False, a * L), (True, (1.0 - b) * L)) if gap <= TUNNEL_NODE_M]
    if own >= SHORT_TUNNEL_M or not ends:
        return own
    lays, asked = [feats], set()
    while True:
        outside = []
        got = own + _tunnel_on(lays, feats, i, ends, SHORT_TUNNEL_M - own, {str(feats.ids[i])}, outside)
        want = set(outside) - asked
        if got >= SHORT_TUNNEL_M or not want:
            return got
        asked |= want
        more = _neighbour_layers(feats, want)
        if not more:
            return got
        lays = lays + more


def _tunnel_on(lays, f, i, ends, need, seen, outside):
    """m of tunnel going on from feature i's (of layer f) ends ([at its end?]) through the lines of lays joined there:
    over its ends, the longest way from each, up to need. outside: the cells of the nodes outside lays[0]'s cell."""
    total = 0.0
    for at_end in ends:
        ring = f.parts(i)[-1][-1] if at_end else f.parts(i)[0][0]
        total += _tunnel_from(lays, np.asarray(ring[-1] if at_end else ring[0], np.float64), need - total, seen,
                              outside)
        if total >= need:
            break
    return total


def _tunnel_from(lays, q, need, seen, outside):
    """m of tunnel going on from node q (lon, lat) into the lines of lays whose end it is (seen: the lines walked),
    the longest way, up to need."""
    cell = _cell_outside(lays[0], q)
    if cell is not None:
        outside.append(cell)
    c = dmath.cos(math.radians(float(q[1])))
    best = 0.0
    for g in lays:
        rows, at_end, ll = _rail_ends(g)
        if not len(rows):
            continue
        d = dnp.hypot((ll[:, 0] - q[0]) * 111320.0 * c, (ll[:, 1] - q[1]) * 110540.0)
        for k in np.flatnonzero(d <= TUNNEL_NODE_M).tolist():
            j, e = int(rows[k]), bool(at_end[k])
            fid = str(g.ids[j])
            if fid in seen:
                continue
            Lj = roads._feature_length_m(g, j)
            rng = [(ra, rb) for ra, rb, kind in roads.tag_level_ranges(g, j) if kind == "tunnel"
                   and ((1.0 - rb) * Lj if e else ra * Lj) <= TUNNEL_NODE_M]
            if not rng:
                continue                    # (the line goes on out of the tunnel there: its portal)
            ra, rb = rng[-1] if e else rng[0]
            got = (rb - ra) * Lj
            if got < need and ra * Lj <= TUNNEL_NODE_M and (1.0 - rb) * Lj <= TUNNEL_NODE_M:
                got += _tunnel_on(lays, g, j, [not e], need - got, seen | {fid}, outside)
            best = max(best, got)
            if best >= need:
                return best
    return best


def _rail_ends(feats):
    """(rows (k,), at its end? (k,), lon/lat (k, 2)) of the drawn railways' ends in feats (each one's first and last
    point), kept on feats."""
    got = feats.__dict__.get("_rail_ends")
    if got is None:
        rows, at_end, ll = [], [], []
        sub = feats.attrs.get("subtype")
        for i in range(len(feats) if sub is not None else 0):
            if str(sub[i]) != "rail" or drawn(feats, i) is None:
                continue
            parts = feats.parts(i)
            if not parts or not len(parts[0]) or len(parts[0][0]) < 2:
                continue
            rows += [i, i]
            at_end += [False, True]
            ll += [parts[0][0][0], parts[-1][-1][-1]]
        got = feats.__dict__["_rail_ends"] = (np.array(rows, np.int64), np.array(at_end, bool),
                                              np.array(ll, np.float64).reshape(-1, 2))
    return got


def _cell_outside(feats, q):
    """str of the cell node q (lon, lat) lies in when it is not feats' own cell (the lines joined there may be in the
    neighbours' layers alone), else None (or no cell known: a test's layer)."""
    cid = feats.__dict__.get("_bnge_cell")
    if cid is None:
        return None
    import mapcells
    k = str(mapcells.cell_of(float(q[1]), float(q[0])))
    return None if k == cid[0] else k


def _neighbour_layers(feats, want):
    """The roads layers of the neighbouring cells named in want (mapdata's _bnge_neighbours), [] without them."""
    hook = feats.__dict__.get("_bnge_neighbours")
    if hook is None:
        return []
    try:
        return [f for f in roads._near_layers(hook, want) if f is not None and len(f)]
    except Exception:                   # (not at hand: what is built on it is not kept)
        import revisit
        revisit.fault()
        return []


def _pieces(feats, i):
    """[("ground" | "bridge" | "tunnel", [lon/lat polylines])] of feature i: its level ranges with the short tunnels
    (SHORT_TUNNEL_M along the line: tunnel_run_m) on the ground and the long ones left out (BNE_RAIL_TUNNELS: a part
    of their own, kind "tunnel")."""
    rngs = roads.level_ranges(feats, i)
    if not rngs:
        return [("ground", roads.ground_parts(feats, i))]
    # (remembered on the layer per feature and its ranges, which are asked every time (the lidar rule's revisit fault
    # with them): every node window of railprofile's lattice cut the same lines again, ~150 times a line in a city)
    memo = feats.__dict__.setdefault("_rail_pieces", {})
    key = (i, tuple(tuple(r) for r in rngs), TUNNELS_ON)
    got = memo.get(key)
    if got is None:
        got = memo[key] = _pieces_and_levels(feats, i, rngs)
    return [(kind, list(rings)) for kind, rings in got[0]]


def bridge_levels(feats, i):
    """The map's level (OSM's layer as Overture's level rules, 1 without one: roads._bridge_level_spans, the road
    bridges' own) of each bridge piece of feature i, in _pieces' order: which of two decks that cross is over the
    other, a track's as a road's (gradeshapes._over)."""
    rngs = roads.level_ranges(feats, i)
    if not rngs:
        return []
    _pieces(feats, i)
    return list(feats.__dict__["_rail_pieces"][(i, tuple(tuple(r) for r in rngs), TUNNELS_ON)][1])


MIN_PART_M = 1.0          # m: a piece of a rail line shorter than this is a rounding sliver, whatever the line's length


def _pieces_of(feats, i, rngs):
    return _pieces_and_levels(feats, i, rngs)[0]


def _pieces_and_levels(feats, i, rngs):
    """(_pieces' list for ranges rngs, the map's level of each of its bridge pieces: bridge_levels)."""
    L = roads._feature_length_m(feats, i)
    # (roads.MIN_PART is a share of the segment: 1e-3 of a 19 km rail segment is 19 m, and its 11 m bridges over the
    # roads were dropped, the track lying across them; at most MIN_PART_M, as for a short feature)
    min_part = min(roads.MIN_PART, MIN_PART_M / max(L, 1.0))
    off = [(a, b, k) for a, b, k in rngs if not (k == "tunnel" and tunnel_run_m(feats, i, a, b) < SHORT_TUNNEL_M)]
    ground, at = [], 0.0
    for a, b, _k in off:
        if a - at > min_part:
            ground.append((at, a))
        at = max(at, b)
    if 1.0 - at > min_part:
        ground.append((at, 1.0))
    out = {"ground": [], "bridge": [], "tunnel": []}
    lv = roads._bridge_level_spans(feats, i, rngs) if any(k == "bridge" for _a, _b, k in off) else []
    levels = []             # (per bridge piece: bridge_levels)
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
                            if kind == "bridge":
                                mid = (a + b) / 2
                                levels.append(next((float(v) for s0, s1, v in lv if s0 <= mid <= s1), 1.0))
    return list(out.items()), levels


def crossings(frame, cells, box, reach=REACH_M):
    """[(xy (k, 2), half width)]: the ground roads near box (their carriageways cut the track at grade)."""
    return [(xy, half) for xy, half, _g in graded_crossings(frame, cells, box, reach)]


def graded_crossings(frame, cells, box, reach=REACH_M):
    """[(xy (k, 2), half width, the grade the road may hold)]: crossings' roads with their grade law (gradelaw.road_grade:
    what findground weighs a cut of them by), made beside them by earthfast.rail_crossing_roads."""
    return _road_crossings(frame, cells, box, reach, worn=False, graded=True)


def _road_crossings(frame, cells, box, reach, worn, graded=False):
    import fast
    fast.require(581 if graded else 549, "rail_crossing_roads")
    near = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    seen, out = set(), []
    grade = (lambda sp: gradelaw.road_grade(sp.cls, sp.kmh)) if graded else None
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
                                     feats, frame, roads, mapgeom.enu_rings, seen, out, grade)
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
    """max over k of req[k] - grade * |s - s[k]|: the lowest line over every req that climbs at most grade. A forward then
    a backward pass (s ascending), each point raised to the one before it less its rise where that is higher (max(req[k],
    v): req[k] on a tie or a NaN); only the points a pass raises are visited, and where a pass raises one it goes on to the
    next, so a line within its grade is two vector comparisons, and the values are the ones the pass over every point gave
    (the same subtractions in the same order, in Python floats)."""
    out = np.array(req, np.float64)
    return _raise_to_grade(out, grade * np.diff(np.asarray(s)[:len(out)]))


def _raise_to_grade(out, rise):
    """_envelope's passes over out (float64, raised in place and returned) with the rises between its points given
    (grade * diff of the arc: a caller laying many requirements over one line makes them once)."""
    m = len(rise)
    if not m:
        return out
    cells, step = memoryview(out), memoryview(rise)     # (Python floats read and written in place)
    done = 0
    for k in (np.flatnonzero(out[:m] - rise > out[1:m + 1]) + 1).tolist():
        if k <= done:                           # (reached by the run before it)
            continue
        while k <= m:
            v = cells[k - 1] - step[k - 1]
            if not v > cells[k]:
                break
            cells[k] = v
            k += 1
        done = k
    done = m
    for k in np.flatnonzero(out[1:m + 1] - rise > out[:m])[::-1].tolist():
        if k >= done:
            continue
        while k >= 0:
            v = cells[k + 1] - step[k]
            if not v > cells[k]:
                break
            cells[k] = v
            k -= 1
        done = k
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


def approach_levels(xy, ground, grade=GRADE):
    """(start, end): per end of a bridge part xy (k, 2), (level m, reach m) of the level the track holds beyond it
    (APPROACH_*: the deck's end and the track up to reach m out are at it at least), or None where there is none.
    Beyond each end the line goes straight on along its last segment. ground(xy (n, 2)) -> z. Pure given ground."""
    return approach_levels_many([xy], ground, [grade])[0]


def approach_levels_many(xys, ground, grades=None):
    """approach_levels of many bridge parts with one ground call (its cost is per call). grades: per part (GRADE: a track's
    one law, light rail's included)."""
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


SWITCH_JOIN_M = 0.5         # m: a structure part's end this near another structure part's line (not near its ends) is
                            # a switch on it (the chain graph's own tolerance for two ends)


def _switch_split(rails):
    """(parts, owner) with each structure part (bridge, tunnel) cut where another structure part's end lies on it, or
    None where none is: the map cuts a viaduct into parts at the ends of its tracks, and a branch leaving the middle of
    a through track (a crossover or a switch on the viaduct, the through track not cut there) joins no node of the
    chain graph, which has them at part ends only. The branch's end was left alone and read the ground under the viaduct,
    6 m under the through track at the Duttweilerbruecke's viaduct in Zurich (both ends of a 118 m crossover, 6.0 and
    6.2 m). The cut puts a node where the branch's end lies (a vertex of the through part there: the part's point nearest
    the line, inserted when no sample lies on it), so profile solves the branch and the through parts as one structure
    (_inner_nodes). owner: per part of parts, (index in rails, the point's index in that part's rails entry per point of
    the piece, -1 for an inserted one). A ground track ending on a viaduct's interior is not cut: it takes the
    viaduct's bed through its own lift (profile). Pure."""
    struct = [n for n, r in enumerate(rails) if r[2] in ("bridge", "tunnel") and len(r[3]) >= 2]
    if len(struct) < 2:
        return None
    boxes = {n: (rails[n][3].min(0) - SWITCH_JOIN_M, rails[n][3].max(0) + SWITCH_JOIN_M) for n in struct}
    box_lo, box_hi = np.array([boxes[m][0] for m in struct]), np.array([boxes[m][1] for m in struct])
    cuts = {}                                   # through part -> {(segment, along): (segment, along, point)}
    for n in struct:
        xy = rails[n][3]
        for end in (xy[0], xy[-1]):
            # (the parts whose box holds the end, in the order of struct: one comparison of all the boxes)
            for m in (struct[i] for i in np.flatnonzero(~((end < box_lo) | (end > box_hi)).any(1)).tolist()):
                if m == n:
                    continue
                q = rails[m][3]
                if min(float(dnp.hypot(*(q[0] - end))), float(dnp.hypot(*(q[-1] - end)))) <= SWITCH_JOIN_M:
                    continue                    # (a node of the chain graph already)
                a, ab = q[:-1], np.diff(q, axis=0)
                t = np.clip(((end - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
                at = a + t[:, None] * ab
                d = dnp.hypot(*(at - end).T)
                j = int(np.argmin(d))
                if float(d[j]) > SWITCH_JOIN_M:
                    continue
                tj = float(t[j])
                if tj < 1e-3:                   # (on a sample: cut there)
                    c = (j, 0.0, None)
                elif tj > 1.0 - 1e-3:
                    c = (j + 1, 0.0, None)
                else:
                    c = (j, tj, at[j].copy())
                mine = cuts.setdefault(m, {})
                if not any((c[:2] == o[:2]) if c[2] is None or o[2] is None
                           else float(dnp.hypot(*(c[2] - o[2]))) < 1e-3 for o in mine.values()):
                    mine[c[:2]] = c
    if not cuts:
        return None
    parts, owner = [], []
    for n, r in enumerate(rails):
        if n not in cuts:
            parts.append(r)
            owner.append((n, np.arange(len(r[3]))))
            continue
        xy = r[3]
        lead, s = None, 0
        for j, _t, pt in sorted(cuts[n].values(), key=lambda c: (c[0], c[1])):
            if pt is None:
                if j <= s and lead is None:
                    continue
                body, idx = xy[s:j + 1], list(range(s, j + 1))
                nxt = (None, j)
            else:
                body, idx = np.vstack([xy[s:j + 1], pt[None]]), list(range(s, j + 1)) + [-1]
                nxt = (pt, j + 1)
            if lead is not None:
                body, idx = np.vstack([lead[None], body]), [-1] + idx
            if len(body) < 2:
                continue
            parts.append((r[0], r[1], r[2], body))
            owner.append((n, np.array(idx)))
            lead, s = nxt
        body, idx = xy[s:], list(range(s, len(xy)))
        if lead is not None:
            body, idx = np.vstack([lead[None], body]), [-1] + idx
        parts.append((r[0], r[1], r[2], body))
        owner.append((n, np.array(idx)))
    return parts, owner


def _structure_at(rails, edges):
    """{node: the structure parts (bridge, tunnel) that end at it} of profile's chain graph (_structure_run's)."""
    at = {}
    for k, (_c, _cls, kind, _xy) in enumerate(rails):
        if kind in ("bridge", "tunnel") and edges[k][0] != edges[k][1]:
            at.setdefault(edges[k][0], []).append(k)
            at.setdefault(edges[k][1], []).append(k)
    return at


def _structure_run(rails, edges, ends, n, sc, at=None):
    """(m from arc sc of structure part n along the structure to its nearest end each way (toward the part's start, its
    end; inf where it goes on with no end), the structure being the bridge and tunnel parts joined end to end): an end is
    a node a ground track ends at, or where no other structure part goes on. The bridge's own run, findground's cost
    law: what of a rise its length climbs at the track's grade before an approach has to. at: _structure_at's of rails
    and edges, made here when not given (a caller with many crossings makes it once)."""
    if at is None:
        at = _structure_at(rails, edges)
    out = []
    for first, d0 in ((edges[n][0], float(sc)), (edges[n][1], float(edges[n][2][-1] - sc))):
        best, heap, seen = math.inf, [(d0, first, n)], set()
        while heap:
            d, node, frm = heapq.heappop(heap)
            if node in seen:
                continue
            seen.add(node)
            more = [k for k in at.get(node, ()) if k != frm]
            if node in ends or not more:
                best = d
                break
            for k in more:
                a, b, s = edges[k]
                heapq.heappush(heap, (d + float(s[-1]), b if node == a else a, k))
        out.append(best)
    return tuple(out)


def profile(rails, ground, underpasses=()):
    """Per part: bed and ground heights (_profile's, the structure parts joined at the switches on them cut for it:
    _switch_split, and the heights of each part's own points handed back)."""
    cut = _switch_split(rails) if rails else None
    if cut is None:
        return _profile(rails, ground, underpasses)
    parts, owner = cut
    got = _profile(parts, ground, underpasses)
    out = [(np.full(len(r[3]), np.nan), np.full(len(r[3]), np.nan)) for r in rails]
    for (n, idx), (zb, zg) in zip(owner, got):
        keep = idx >= 0
        out[n][0][idx[keep]] = np.asarray(zb, np.float64)[keep]
        out[n][1][idx[keep]] = np.asarray(zg, np.float64)[keep]
    return out


def _profile(rails, ground, underpasses=()):
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
    got_all = approach_levels_many([rails[n][3] for n in decks], ground) if decks else []
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
    on_ground, lifts = [], []                   # (the ground parts' beds: smoothed in one call, bed_heights_many)
    for n, ((_, _, kind, _), zg, (a, b, s)) in enumerate(zip(rails, zgs, edges)):
        if kind != "ground":
            zb.append(zg.copy())
            continue
        lifted = shared.get(n, zg)
        for node, dist in ((a, s), (b, s[-1] - s)):     # (the track on at that level up to the bridge, not down the
            if node in level:                           # slope under it: lifted before the bed's smoothing)
                lv, reach = level[node]
                lifted = np.where(dist <= reach + STEP_M, np.maximum(lifted, lv), lifted)
        zb.append(None)
        on_ground.append(n)
        lifts.append(lifted)
    for n, bed in zip(on_ground, bed_heights_many(lifts)):
        zb[n] = bed
    # Read the lower road at its mouths, not on the filled railway bed above it.
    underpasses = list(underpasses)
    # (the bed over a road's tunnel by the tunnel's own height; over a plain road by the road's headroom: _Rise.clear.
    # The points over each crossing and their fractions along it: _underpass_hits, earthfast.rail_underpass_hits)
    hits_all = (_underpass_hits(rails, [(u[0], u[1]) for u in underpasses], DECK_HALF_M + STEP_M)
                if underpasses else [])
    crossings = []                              # (line, half, {part: (points over, fractions along line)}, clear, grade)
    for u, hits in zip(underpasses, hits_all):
        if hits:
            crossings.append((u[0], u[1], hits, getattr(u, "clear", UNDERPASS_CLEAR_M + DECK_D - BALLAST_H),
                              getattr(u, "grade", None)))
    over_part = {}                              # part -> the crossings over it, in order (each one raises the bed again)
    for c, (_line, _half, covers, _clear, _grade) in enumerate(crossings):
        for n in covers:
            over_part.setdefault(n, []).append(c)
    mouths = np.concatenate([c[0][[0, -1]] for c in crossings]) if crossings else np.empty((0, 2))
    mouth_z = np.asarray(ground(mouths), np.float64).reshape(-1, 2) if len(mouths) else []
    ends = {}                                   # node -> the ground parts' bed there (a bridge's ends)
    for n, ((_, _, kind, _), (a, b, _)) in enumerate(zip(rails, edges)):
        if kind == "ground":
            ends.setdefault(a, []).append(zb[n][0])
            ends.setdefault(b, []).append(zb[n][-1])
    seeds = {}                                  # node -> the deck's height at it
    struct_at = _structure_at(rails, edges)
    inner = _inner_nodes(rails, edges, zgs, ends, level)
    for n, ((_, cls, kind, xy), (a, b, _)) in enumerate(zip(rails, edges)):
        if kind == "tunnel":                    # (straight between its portals' beds, whatever the hill above it)
            g = zgs[n]
            sx = _arc(xy)
            za = inner[a] if a in inner else float(np.mean(ends[a])) if a in ends else float(g[0])
            zb_ = inner[b] if b in inner else float(np.mean(ends[b])) if b in ends else float(g[-1])
            zb[n] = za + (zb_ - za) * sx / max(float(sx[-1]), 1e-9)
            continue
        if kind != "bridge":
            continue
        g = zgs[n]
        za = inner[a] if a in inner else float(np.mean(ends[a])) if a in ends else float(g[0])
        zb_ = inner[b] if b in inner else float(np.mean(ends[b])) if b in ends else float(g[-1])
        za = max(za, level[a][0]) if a in level and a not in inner else za      # (the track's level beyond its ends)
        zb_ = max(zb_, level[b][0]) if b in level and b not in inner else zb_
        sx = _arc(xy)
        zb[n] = deck_heights(sx, g, za, zb_)
        rise = None                             # (the deck's rises between samples, for each crossing over it)
        for c in over_part.get(n, ()):
            over, t = crossings[c][2][n]
            z0, z1 = mouth_z[c]
            want = z0 + (z1 - z0) * t + crossings[c][3]
            if crossings[c][4] is not None:
                # (a plain way: the bridge rises over it, or the way goes under the track (grading's underpass), by
                # findground: the lack is what the bed falls short by, the run the bridge has to its structure's ends
                lack = want - zb[n][over]
                k = int(np.argmax(lack))
                ends_m = _structure_run(rails, edges, ends, n, float(np.mean(sx[over])), struct_at)     # (the road's own crossing)
                if min(ends_m) >= AT_GRADE_END_M and findground.leaves(findground.GROUND, findground.BRIDGE, float(lack[k]), ends_m,
                                                                       GRADE, crossings[c][4]) != "upper":
                    continue
            req = zb[n].copy()
            req[over] = np.maximum(req[over], want)
            if rise is None:
                rise = GRADE * np.diff(sx)
            zb[n] = _raise_to_grade(req, rise)
        for node, h in ((a, zb[n][0]), (b, zb[n][-1])):
            seeds[node] = max(seeds.get(node, -np.inf), float(h))
    if seeds and STEPLESS_ON:
        return list(zip(_stepless(rails, edges, adj, zb, ends), zgs))
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
                # ... never over the decks' own heights run out at GRADE (or its own bed, where that is higher): a
                # bridge's mapped end down a stream's bank read the bed there, in the channel, 11 m under the deck,
                # and that 11 m rode on the bed's own level for 500 m either side (Findlay, OH: an embankment 8-11 m
                # over a floodplain the line crosses at grade, GitHub #1384). Up a slope (#1051) the bed + lift stays
                # under the deck's run-out, as before.
                reach = np.maximum(top - GRADE * (d[a] + s), top - GRADE * (d[b] + s[-1] - s))
                zb[n] = np.minimum(zb[n] + np.clip(lift, 0.0, SEED_LIFT_MAX_M), np.maximum(zb[n], reach))
    return list(zip(zb, zgs))


def _inner_nodes(rails, edges, zgs, ends, level):
    """{node: height} of the nodes inside a structure: where bridge and tunnel parts meet each other and no track on the
    ground ends (profile's rails, their chain graph's edges, the ground under each part zgs, the ground parts' beds at the
    nodes ends, the track's level beyond a bridge's end level). The map cuts one viaduct into a part per switch and per
    line: the ground at such a joint is what the bridge crosses, not where it stands. The joints' heights are the ones
    that leave every part straight between the structure's own ends (the nodes a ground track ends at, or a part ends
    alone at) and the grade the same either side of a joint that two parts make, the mean of its neighbours' by their
    distances where more meet (a switch on the viaduct). Zurich's Wipkingen viaduct, 13 m over the streets from end to
    end, was laid at their level at each of its joints, and Earth dug the streets round it to pass under it
    (the survey's tracks: its bed a median 6 m under them, 16 m at the worst). A ring of structures with no end keeps
    its ground. Pure."""
    struct = [n for n, r in enumerate(rails) if r[2] in ("bridge", "tunnel") and edges[n][0] != edges[n][1]]
    deg, g_at = {}, {}
    for n in struct:
        a, b, _s = edges[n]
        for node, g in ((a, zgs[n][0]), (b, zgs[n][-1])):
            deg[node] = deg.get(node, 0) + 1
            g_at[node] = float(g)
    free = sorted(node for node, d in deg.items() if d >= 2 and node not in ends)
    if not free:
        return {}
    at = {node: i for i, node in enumerate(free)}
    held = {}
    for n in struct:
        for node in edges[n][:2]:
            if node in at or node in held:
                continue
            z = float(np.mean(ends[node])) if node in ends else g_at[node]
            held[node] = max(z, level[node][0]) if node in level else z
    A, rhs = np.zeros((len(free), len(free))), np.zeros(len(free))
    par = list(range(len(free)))
    anchored = np.zeros(len(free), bool)

    def root(i):
        while par[i] != i:
            par[i] = par[par[i]]
            i = par[i]
        return i
    for n in struct:
        a, b, s = edges[n]
        w = 1.0 / max(float(s[-1]), 1e-6)
        for p, q in ((a, b), (b, a)):
            if p not in at:
                continue
            A[at[p], at[p]] += w
            if q in at:
                A[at[p], at[q]] -= w
                par[root(at[p])] = root(at[q])
            else:
                rhs[at[p]] += w * held[q]
                anchored[at[p]] = True
    roots = np.array([root(i) for i in range(len(free))])
    ok = np.isin(roots, np.unique(roots[anchored]))
    out = {}
    if ok.any():
        idx = np.flatnonzero(ok)
        for i, z in zip(idx, dnp.solve(A[np.ix_(idx, idx)], rhs[idx])):
            out[free[i]] = float(z)
    return out


# The decks' heights carried along the track as one pass to a fixed point: every deck enveloped to its reach, the ground
# track lifted to the decks' final ends (not their heights before the reach: a deck flanked by spans laid flat between ground
# ends seeded the ground at its own height, so the ramps were never built and the spans, raised after, stepped 4.5-5.5 m
# down to the ground track at Popoli), the decks riding the lifted track (a short bridge on a ramp keeps to it), again until
# no end moves. Every part's bed equal at every node: never a step. Off with BNE_RAIL_STEPLESS=off (the single pass).
STEPLESS_ON = os.environ.get("BNE_RAIL_STEPLESS", "on") != "off"
STEPLESS_PASSES = 6
STEPLESS_TOL_M = 0.005


def _stepless(rails, edges, adj, zb, ends):
    """profile's last step (see STEPLESS_ON): the beds zb of every part (decks with their clearance already in them), the
    ground parts' beds at the nodes ends (node -> [bed]), the parts' chain graph (edges, adj): the beds, every part's end
    equal at each node, the decks' heights run out at GRADE per metre along the track."""
    zb = [np.asarray(z, np.float64).copy() for z in zb]
    kind = [r[2] for r in rails]
    req = {n: zb[n].copy() for n in range(len(rails)) if kind[n] == "bridge"}       # (a deck no lower than this)
    base = {n: zb[n].copy() for n in range(len(rails)) if kind[n] == "ground"}      # (a ground track's own bed)
    own = {node: float(np.mean(v)) for node, v in ends.items()}
    # (a lift runs out along the ground track only, not over a deck: carried across one at GRADE it lifted the ground at
    # the deck's far end, which raised the deck's end, which lifted more: a rack line up a 10 % slope ran away, #1051)
    gadj = [[] for _ in adj]
    for n in base:
        a, b, s = edges[n]
        gadj[a].append((b, float(s[-1])))
        gadj[b].append((a, float(s[-1])))
    for _ in range(STEPLESS_PASSES):
        seeds = {}
        for n in req:
            a, b, s = edges[n]
            for node, h in ((a, zb[n][0]), (b, zb[n][-1])):
                seeds[node] = max(seeds.get(node, -np.inf), float(h))
        if not seeds:
            break
        top = max(seeds.values())
        d = roads._dijkstra(adj, {n: (top - h) / GRADE for n, h in seeds.items()})
        lifts = {node: h - own[node] for node, h in seeds.items() if node in own and h - own[node] > 0.0}
        dl = None
        if lifts:
            ltop = max(lifts.values())
            dl = roads._dijkstra(gadj, {node: (ltop - v) / GRADE for node, v in lifts.items()})
        da = roads._dijkstra(gadj, {node: (top - h) / GRADE for node, h in seeds.items()})
        moved = 0.0
        for n in base:                          # the ground track: its own bed, lifted on the ramps to the decks ...
            a, b, s = edges[n]
            new = base[n]
            if dl is not None:
                lift = np.maximum(ltop - GRADE * (dl[a] + s), ltop - GRADE * (dl[b] + s[-1] - s))
                new = base[n] + np.clip(lift, 0.0, SEED_LIFT_MAX_M)
                # ... no higher than the decks' own heights run out along it at GRADE (or its bed): a lift added to a
                # mound between two bridges stood its top 2 m over both decks (Compiegne's roundabout island)
                ramp = np.maximum(top - GRADE * (da[a] + s), top - GRADE * (da[b] + s[-1] - s))
                new = np.minimum(new, np.maximum(base[n], ramp))
            moved = max(moved, float(np.max(np.abs(new - zb[n]))))
            zb[n] = new
        at = {}                                 # node -> the ground track's bed there now
        for n in base:
            a, b, _s = edges[n]
            at.setdefault(a, []).append(float(zb[n][0]))
            at.setdefault(b, []).append(float(zb[n][-1]))
        for n in req:                           # a deck: its clearance, its reach, and the track it meets at its ends
            a, b, s = edges[n]
            reach = np.maximum(top - GRADE * (d[a] + s), top - GRADE * (d[b] + s[-1] - s))
            za = max([float(zb[n][0])] + at.get(a, []))
            zb_ = max([float(zb[n][-1])] + at.get(b, []))
            chord = za + (zb_ - za) * s / max(float(s[-1]), 1e-9)
            new = _envelope(s, np.maximum(np.maximum(req[n], reach), chord))
            moved = max(moved, float(np.max(np.abs(new - zb[n]))))
            zb[n] = new
        if moved <= STEPLESS_TOL_M:
            break
    return zb


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
    tcls = []
    tunnels = road_underpasses(frame, cells, box, classes=tcls)
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
    prof = (_beds_fit(frame, rails, ground, nat, raising_underpasses(tunnels, tcls) + plain_underpasses(frame, cells, box, rails, tunnels=tunnels), load, bed_elev,
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


def _bridge_decks(rails, prof, carried):
    """The bridge decks' centrelines of rails with their beds prof, a bridge a model carries (carried) left out:
    [{"n": xy + z line (the bed plus its ballast), "w": width, "under": m the deck reaches under that line}]."""
    return [{"n": np.column_stack((xy, zb + BALLAST_H)), "w": 2 * (TRAM_DECK_HALF_M if cls == "tram" else DECK_HALF_M),
             "under": DECK_D}
            for (_, cls, kind, xy), (zb, _zg), gone in zip(rails, prof, carried) if kind == "bridge" and not gone]


def tile_decks(frame, tx, ty, cells, ground, far_ground=None, load=None, bed_elev=None, under=None):
    """tile_mesh's bridge decks ([{"n", "w", "under"}], _bridge_decks') of the railways within REACH_M of the tile (tx,
    ty), made before the road tile's retaining walls: a wall in a cutting stands up to the underside of a deck over the
    road, a railway's too (the walls rose through the rail bridges over the busway and the carriageways of Vienna's
    Erzherzog-Karl-Strasse: 11 of the 15 crossings the eval found cutting, 2026-11-18). The same beds as the mesh's (one
    profile along the track: _beds), arguments as tile_mesh's. Pure given its inputs."""
    cells = list(cells)
    box = roads.tile_bbox(tx, ty)
    tcls = []
    tunnels = road_underpasses(frame, cells, box, classes=tcls)
    rails = parts(frame, cells, box, clip=True, worn={}, underpasses=tunnels)    # (worn: as tile_mesh asks)
    if not any(kind == "bridge" for _f, _c, kind, _xy in rails):
        return []
    ground = _mixed(ground, far_ground, box)
    prof = _beds(frame, rails, ground,
                 raising_underpasses(tunnels, tcls) + plain_underpasses(frame, cells, box, rails, tunnels=tunnels),
                 load, bed_elev)
    rails, prof = _spans_over_roads(rails, prof, _roads_under_bridges(frame, cells, box, REACH_M, rails))  # (tile_mesh's)
    carried = [under is not None and kind == "bridge" and np.asarray(under(xy), bool).mean() >= 0.5
               for _f, _c, kind, xy in rails]
    return _bridge_decks(rails, prof, carried)


def tile_mesh(frame, tx, ty, cells, ground, sign_style=None, far_ground=None, return_decks=False, load=None,
              bed_elev=None, _drop=frozenset(), furniture=None, under=None, records=None, tracks_out=None):
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
    given; the caller hands the same ones to rails.tile_chunks for the crossing decks). tracks_out: a list that gets (feature
    id, class, "ground" | "bridge" | "tunnel", xy, bed z) of every track part the mesh draws, the bed as it was finally laid
    (flush with the road at a level crossing): what the clearance check (clearance.py) stands its wagons on."""
    fast.require(630, "rail_tile_mesh")       # (630: tracks_out, the fork's raised underpasses, spans over roads and decks' "under")
    return fast._ef.rail_tile_mesh(frame, tx, ty, list(cells), ground, sign_style, far_ground, return_decks,
                                   load, bed_elev, _drop, furniture, under, records, tracks_out)


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
