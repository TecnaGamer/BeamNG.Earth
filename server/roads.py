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
from collections import OrderedDict, namedtuple

import numpy as np

import junctions
import mapcells
import mapgeom
import roadmesh
import staging

log = logging.getLogger("earthd.roads")

VERSION = 1
TILE = 256.0            # m, tile side in the anchor frame
SEND_M = float(os.environ.get("BNE_ROADS_SEND_M", "640"))     # tiles whose square comes this close to the player
                                                                # (Euclidean, tiles_near: a disc) are sent
DROP_M = float(os.environ.get("BNE_ROADS_DROP_M", str(SEND_M + 320)))   # ... and dropped beyond this
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
    # racetracks (OpenStreetMap highway=raceway, osmtags.raceways: GitHub #143): a circuit's typical 12 m unless its
    # width is mapped; no markings, sidewalks, furniture or traffic (not in those classes' sets nor navgraph.DRIVE),
    # and graded by the terrain alone (grading.UNGRADED: an oval's banking stays)
    "raceway": (12.0, True),
}
PAVED = {"paved", "asphalt", "concrete", "metal", "wood", "tar", "bitumen"}
COBBLE = {"paving_stones", "sett", "cobblestone", "unhewn_cobblestone", "bricks",   # Overture: "paving_stones"
          "interlocking", "paver", "paver_blocks"}      # (South Asia's names, PR #146)
# interlocking concrete pavers under the names the map gives them (surface_kind: a street paved with them is a
# cobble street, not a gravel track)
COBBLE_ALIASES = {"interlocking_pavers", "interlocking_stones", "pavers", "block_pavers", "paving_stone",
                  # OSM's surface=stone is mostly stone paving (flagstones, slabs), not a rock track (merge review of
                  # PR #119, 2026-10-22)
                  "stone", "stones"}
GRAVEL = {"gravel", "fine_gravel", "compacted", "pebblestone", "wbm", "laterite"}   # (water-bound macadam: PR #146)
# ... and the values that do mean "no hard surface": an unrecognised one is not one of these (the class decides)
UNPAVED = {"unpaved", "ground", "dirt", "earth", "mud", "grass", "grass_paver", "sand", "soil",
           # the rock family: a rough, unpaved surface, never a marked asphalt street (gridmap v2's Rock is a
           # TerrainMaterial — a heightfield shader, not a surface a decal can carry: 2026-10-22)
           "rock", "rocky", "bedrock",
           # more of OSM's recognised unpaved values, which an unrecognised value must not outrank
           "clay", "shells", "woodchips"}
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
# a `surface=concrete` street is still a paved one (markings, widths, the tyres' asphalt feel) but is drawn with
# gridmap v2's plain concrete instead of asphalt (the user, 2026-10-22: "for concrete road surfaces")
CONCRETE = {"m": "m_gm_concrete_plain", "tl": 11.0, "p": 20}


def terrain_surface(spec):
    """'rock', 'grass' or None: a spec whose surface belongs to the ground layer (surface.py) and must not be drawn
    as a road decal. A decal is a smooth ribbon; these surfaces are not, and the terrain materials that describe them
    (gridmap v2's Rock / Grass) cannot be carried by one."""
    if spec.surf in ROCKISH:
        return "rock"
    if spec.surf in GRASSY:
        return "grass"
    return None


def surface_material_kind(spec):
    """The material the ground under a spec is painted with (surface.py): 'rock' / 'grass' for those surfaces, else
    the spec's kind (paved, cobble, gravel, dirt)."""
    return terrain_surface(spec) or spec.kind


def look_of(spec):
    """A spec's road look: gridmap v2's concrete for a `surface=concrete` street, else its kind's (LOOK)."""
    if spec.kind == "paved" and spec.surf == "concrete":
        return CONCRETE
    return LOOK[spec.kind]
Spec = namedtuple("Spec", "width kind urban cls oneway walk dir kmh gap median lanes surf link sides",
                  defaults=(False, 2.5, 0, None, None, None, None, None, False, None))   # median: m to the other carriageway's centre line
# (sides: the sidewalk sides OpenStreetMap's sidewalk tag gives (_osm_sidewalks; walk_sides' +1 left / -1 right of the
# geometry, () none), None where it has none: the class and land-cover rule, has_sidewalks; from mpsteppe's PR #292)
# (lanes: OpenStreetMap's lane count, both directions on a two-way road, None unknown: osmtags.py; surf: the surface
# rule value's base token, e.g. "concrete": the look's concrete, roads.py look_of; link: a ramp / slip road, Overture's
# subclass "link", its class the road's it leads to)
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
EDGE_LINE = {"m": "line_white", "tl": 6.4, "p": 2, "w": 0.15, "inset": 0.3}
# The asphalt's edge where no curb ends it: East Coast USA's decal (a ragged opaque edge of gravel and dirt fading
# into the ground; its texture's u = 1 side lies on the asphalt, the opaque edge EDGE_DECAL_AT across it). The
# ground photo is ~2 m a pixel and the terrain's materials change on its 2 m grid: the edge was blurred and stepped
# (the user, 2026-10-28: "the edge of the road is blurry and slightly jagged").
EDGE_DECAL = {"m": "ECA_road_edge_dirt", "tl": 6.4, "p": 1, "w": 1.6, "layer": "edge"}
EDGE_DECAL_AT = 0.95
LADDER_LINE_W = 0.30            # a ladder crosswalk's side lines (MUTCD 3C.04: 6-24 in)
EDGE_DECAL_GAP_M = 1.5          # ... none where another carriageway's edge is this near past the decal (gores, medians)
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
# carriageways painted into the drawn ground instead of meshes (surface.photo_roads; the user, 2026-10-22: "I dont love
# that we are applying a floating road texture over the normal terrain"; it also ended the white diamonds on the
# Stelvio's hairpins, GitHub #135). BNGE_ROADS_IN_GROUND=0: the carriageway meshes as before
ROADS_IN_GROUND = os.environ.get("BNGE_ROADS_IN_GROUND", "1") != "0"
# m per driving lane by class when OpenStreetMap gives the lane count (osmtags.py; typical design widths)
LANE_W = {"motorway": 3.6, "trunk": 3.5, "primary": 3.3, "secondary": 3.3, "tertiary": 3.1}
LANE_W_DEFAULT = 3.0
OSM_LANES = (1, 12)     # a lane count outside this is not believed
OSM_WIDTH_M = (2.0, 40.0)


def _osm(feats, name, i, lo=None, hi=None):
    """OpenStreetMap's value `name` of feature i (osmtags.join's attrs column osm_<name>), or None (absent, not a number,
    outside lo..hi)."""
    col = feats.attrs.get("osm_" + name)
    if col is None:
        return None
    try:
        v = float(col[i])
    except (TypeError, ValueError):
        return None
    if not np.isfinite(v) or (lo is not None and v < lo) or (hi is not None and v > hi):
        return None
    return v
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
MOTORWAY_RURAL_M = 7.4
SHOULDER_M = 2.2
PARKING_CLASSES = {"residential", "tertiary", "secondary", "unclassified", "living_street"}
EDGE_STRIP_M = 1.0
# ... by class where measured narrower: rural minor roads' paved width on the official aerials (PEMA 6-inch, Lancaster
# County PA, 20 tertiary 6.4 m and 20 secondary 6.7 m against 8.5 and 9.5 drawn; NAIP, El Paso County CO, tertiary 7.2;
# 2026-10-28, the user: "compare against high res satellite photos"): no strips on tertiary and unclassified roads,
# narrow ones on secondaries
EDGE_STRIP_CLASS_M = {"tertiary": 0.0, "unclassified": 0.0, "secondary": 0.25}
URBAN_CLASS = 50                # ESA WorldCover "built-up"
URBAN_SHARE = 0.5               # a road is urban when this share of its vertices is built-up ...
URBAN_BESIDE_M = 30.0           # ... and at least URBAN_BESIDE_SHARE of them have built-up land this far to one side:
URBAN_BESIDE_SHARE = 0.2        # the land cover counts a wide road's own pavement as built-up, and the Eyre Highway
                                # through Eucla (centre 94 %, beside it 3 %) got a town's footpaths, bins and hydrants
                                # across the Nullarbor (GitHub #267). Baltimore and Chicago keep every urban road.
URBAN_BOTH_SHARE = 0.1          # a motorway or trunk needs built-up land on both
                                # sides of this share of its vertices: a roadhouse beside the Eyre Highway (Eucla: one
                                # side 33 %, the other 0 %) is no town


def _beside(ll, counts, d):
    """The points d m to the left and to the right of each vertex of the lines (lon/lat (n, 2), counts: the vertices of
    each line in turn), square to the line there; a one-vertex line's are the vertex itself. Pure."""
    left, right = np.empty_like(ll), np.empty_like(ll)
    k = 0
    for c in counts:
        p = ll[k:k + c]
        if c < 2:
            left[k:k + c] = right[k:k + c] = p
            k += c
            continue
        m = np.array([111320.0 * math.cos(math.radians(float(p[:, 1].mean()))), 110540.0])   # m per degree
        t = np.gradient(p * m, axis=0)
        t /= np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None]
        off = np.stack([-t[:, 1], t[:, 0]], axis=1) * d / m
        left[k:k + c], right[k:k + c] = p + off, p - off
        k += c
    return left, right


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
    was taken whole as a bridge and dropped). Pure. A cell's are made all at once and shared (cell_facts)."""
    memo = feats.__dict__.setdefault("_level_ranges", {})
    got = memo.get(i)
    if got is None:
        _cell_ranges(feats)
        got = memo.get(i)
        if got is None:
            got = memo[i] = _lidar_gaps(feats, i, rule_level_ranges(feats, i))
    if feats.__dict__.get("_lidar_rule"):
        import revisit                      # (the rule for want of the lidar: nothing built on it is kept)
        revisit.fault()
    return list(got)


def rule_level_ranges(feats, i):
    """level_ranges before the lidar's elevated stretches (_lidar_gaps): the map's ranges lengthened (_skew_decks) and
    their short ground gaps between decks filled (_deck_gaps). roadz.py measures the lidar along these (its answer is
    what level_ranges adds). Remembered on feats."""
    memo = feats.__dict__.setdefault("_rule_level_ranges", {})
    got = memo.get(i)
    if got is None:
        got = memo[i] = _deck_gaps(feats, i, _skew_decks(feats, i, raw_level_ranges(feats, i)))
    return list(got)


def raw_level_ranges(feats, i):
    """level_ranges as the map has them, before _skew_decks (road_specs reads these: _skew_decks needs the specs)."""
    memo = feats.__dict__.setdefault("_raw_level_ranges", {})
    got = memo.get(i)
    if got is None:
        _cell_ranges(feats, load_only=True)
        got = memo.get(i)
        if got is None:
            got = memo[i] = _level_ranges(feats, i)
    return list(got)


# Per-cell facts shared by the server's processes and kept for the next run (the revisit cache: one file per cell and
# kind, keyed by the cell, its map data versions and the code): a cell's road specs and level ranges, which every
# process that draws or grades its roads made for itself (~0.2 s a city cell each; the terrain blocks', the road
# tiles' and the AI graph's processes, 2026-10-25). Kept only when complete (revisit.faults unchanged).
_FACTS_LOCK = threading.Lock()


def _facts_key(feats, kind, extra):
    cid = feats.__dict__.get("_bnge_cell")
    import revisit
    if cid is None or not revisit.ON or revisit.CACHE_DIR is None:
        return None
    return revisit.key("cellfacts", kind, cid, extra)


def _facts_load(feats, kind, extra):
    k = _facts_key(feats, kind, extra)
    if k is None:
        return None
    import revisit
    got = revisit.load(revisit.CACHE_DIR, "cellfacts", k)
    if got is None or "facts.json" not in got[0]:
        return None
    try:
        return json.loads(got[0]["facts.json"])
    except ValueError:
        return None


def _facts_store(feats, kind, extra, data):
    k = _facts_key(feats, kind, extra)
    if k is None:
        return
    import revisit
    revisit.store(revisit.CACHE_DIR, "cellfacts", k, {"facts.json": json.dumps(data).encode("utf-8")})


def _cell_ranges(feats, load_only=False):
    """Every feature's raw and lengthened level ranges into feats' memos: from the shared facts, or (not load_only)
    made here and kept there."""
    st = feats.__dict__
    with _FACTS_LOCK:
        if st.get("_ranges_state") is not None:
            if load_only or st["_ranges_state"] != "loadable":
                return
        tried = st.get("_ranges_state")
        st["_ranges_state"] = "busy"
    if tried is None:
        got = _facts_load(feats, "ranges", None)
        if got is not None and len(got.get("raw", ())) == len(feats) and len(got.get("lvl", ())) == len(feats):
            raw, lvl = st.setdefault("_raw_level_ranges", {}), st.setdefault("_level_ranges", {})
            for i in range(len(feats)):
                raw.setdefault(i, [tuple(r) for r in got["raw"][i]])
                lvl.setdefault(i, [tuple(r) for r in got["lvl"][i]])
            st["_ranges_state"] = "done"
            return
    if load_only:
        st["_ranges_state"] = "loadable"        # (the eager build waits for level_ranges: road_specs reads the raw)
        return
    import revisit
    f0 = revisit.faults()
    raw = [raw_level_ranges(feats, i) for i in range(len(feats))]
    lvl = [level_ranges(feats, i) for i in range(len(feats))]
    st["_ranges_state"] = "done"
    if revisit.faults() == f0:
        _facts_store(feats, "ranges", None, {"raw": [[[float(a), float(b), k] for a, b, k in r] for r in raw],
                                             "lvl": [[[float(a), float(b), k] for a, b, k in r] for r in lvl]})


# A bridge over a road spans more than its carriageway: the road's shoulders and the abutments' slopes stand under it.
# The map's bridge range often ends close to the lower road's edge (an 18 m deck of the Baltimore-Washington Parkway
# over an 11 m local road, 2026-10-25: the ground climbed 10 m from the road's edge to the Parkway within 3 m, wedges
# into its lanes), or, crossing at an angle, short of its edge under the upper road's outer lanes. A bridge range is
# lengthened until it covers, across the upper road's whole width, the lower road's carriageway, SKEW_MARGIN_M and an
# abutment slope of SKEW_SLOPE_M either side: (lower half width + margin + slope) / sin + upper half width / tan of
# the angle between them, at most SKEW_MAX_M more per end (onto the road joined there: _skew_decks).
SKEW_ON = os.environ.get("BNE_SKEW_DECKS", "on") != "off"
SKEW_MARGIN_M = 1.5         # m past the lower road's carriageway edge kept level (its shoulder)
SKEW_SLOPE_M = 6.0          # m for the abutment's slope up to the upper road, under the deck
SKEW_MAX_M = 30.0           # m a bridge range grows at most at each end
SKEW_MIN_SIN = math.sin(math.radians(20.0))    # flatter crossings are taken as this (the reach would run away)


def _feature_boxes(feats):
    """(n, 4) lon/lat boxes (lon0, lat0, lon1, lat1) of the features, remembered on feats."""
    got = feats.__dict__.get("_lonlat_boxes")
    if got is None:
        n = len(feats)
        got = np.full((n, 4), np.nan)
        po = np.asarray(feats.part_off, np.int64)
        co, ro = np.asarray(feats.coord_off, np.int64), np.asarray(feats.ring_off, np.int64)
        a, b = co[ro[po[:-1]]], co[ro[po[1:]]]
        ok = b > a
        ll = np.asarray(feats.lonlat, np.float64)
        if ok.any() and np.all(a[ok][1:] == b[ok][:-1]) and b[ok][-1] == len(ll):   # (contiguous: one reduce each)
            st = a[ok]
            got[ok, 0], got[ok, 1] = np.minimum.reduceat(ll[:, 0], st), np.minimum.reduceat(ll[:, 1], st)
            got[ok, 2], got[ok, 3] = np.maximum.reduceat(ll[:, 0], st), np.maximum.reduceat(ll[:, 1], st)
        else:
            for i in np.flatnonzero(ok):
                seg = ll[a[i]:b[i]]
                got[i] = (seg[:, 0].min(), seg[:, 1].min(), seg[:, 0].max(), seg[:, 1].max())
        feats.__dict__["_lonlat_boxes"] = got
    return got


def _raw_ground_parts(feats, j):
    """Feature j's on-ground pieces from its raw_level_ranges (_skew_decks: level_ranges of the others would recurse)."""
    pieces, at = [], 0.0
    for a, b, _ in raw_level_ranges(feats, j):
        if a - at > MIN_PART:
            pieces.append((at, a))
        at = max(at, b)
    if 1.0 - at > MIN_PART:
        pieces.append((at, 1.0))
    out = []
    for part in feats.parts(j):
        for ring in part:
            if len(ring) >= 2:
                for a, b in pieces:
                    out.append(ring if (a, b) == (0.0, 1.0) else sub_polyline(ring, a, b))
    return out


def _seg_hits(m, lines):
    """(line index, arc m along polyline m, sin of the angle) where each of the polylines lines (all (k, 2) m) crosses
    m (earthfast.seg_hits). Pure."""
    import fast
    fast.require(73, "seg_hits")
    starts = np.r_[0, np.cumsum([len(g) for g in lines])].astype(np.int64)
    return fast._ef.seg_hits(np.ascontiguousarray(m, np.float64),
                             np.ascontiguousarray(np.vstack(lines), np.float64), starts)


def _structure_width(feats, i):
    """m: the carriageway of road feature i as _bridge_specs makes it, without its level ranges (a road bridged along
    its whole length has no road_specs entry: its ranges were never lengthened over the roads under it; from cody196's
    PR #368), or None (not a drawn road)."""
    cls = str(feats.attrs["class"][i]) if "class" in feats.attrs else "unknown"
    if str(feats.attrs.get("subtype", ["road"] * len(feats))[i]) != "road" or cls not in STYLE or unbuilt(feats, i):
        return None
    rules = feats.rules
    width = _rule_value(rules.get("width"), i)
    rule_w = float(width) if width is not None and 1.0 <= float(width) <= 40.0 else None
    if rule_w is None:
        rule_w = _osm(feats, "width", i, *OSM_WIDTH_M)
    lanes = _osm(feats, "lanes", i, *OSM_LANES)
    lanes = int(lanes) if lanes is not None else None
    kind = surface_kind(cls, _rule_value(rules.get("surface"), i))
    ow = _rule_value(rules.get("oneway"), i) is not None
    if rule_w is None and _rule_value(rules.get("subclass"), i) == "link" and lanes is None:
        rule_w = LINK_M[1] if ow else LINK_M[2]
    return carriageway(cls, kind, False, rule_w, ow, lanes)


def _bridge_reach(feats, i):
    """Bridge feature i's ranges lengthened over the ground roads crossing under them (SKEW_*), and how far past each
    end of the feature they would reach: {"ranges": [...], "over": [m past its start, m past its end]} (_skew_decks
    puts that on the road joined there). Remembered on feats. Pure given feats."""
    memo = feats.__dict__.setdefault("_bridge_reach", {})
    got = memo.get(i)
    if got is not None:
        return got
    rngs = raw_level_ranges(feats, i)
    got = {"ranges": rngs, "over": [0.0, 0.0]}
    memo[i] = got
    specs = road_specs(feats)
    up_w = specs[i].width if specs[i] is not None else _structure_width(feats, i)
    rings = [np.asarray(r, np.float64) for part in feats.parts(i) for r in part if len(r) >= 2]
    if up_w is None or not rings or not any(k == "bridge" for _, _, k in rngs):
        return got
    ring = rings[0]
    kx, ky = 111320.0 * math.cos(math.radians(float(ring[0, 1]))), 111320.0
    m = (ring - ring[0]) * [kx, ky]
    sm = np.r_[0.0, np.cumsum(np.hypot(*np.diff(m, axis=0).T))]
    L = float(sm[-1])
    if L <= 0:
        return got
    boxes = _feature_boxes(feats)
    out, over = [], [0.0, 0.0]
    for n, (a, b, k) in enumerate(rngs):
        if k == "bridge":
            a0, b0 = a, b
            lo_m, hi_m = a0 * L, b0 * L
            # (only the bridge's own stretch: a crossing counts under it; a 3 km segment's whole line and every road
            # near it cost a rail-yard tile seconds, 2026-10-25)
            inner = (sm > lo_m) & (sm < hi_m)      # (metres: sub_polyline scales lon/lat)
            sub = np.vstack([[np.interp(lo_m, sm, m[:, 0]), np.interp(lo_m, sm, m[:, 1])], m[inner],
                             [np.interp(hi_m, sm, m[:, 0]), np.interp(hi_m, sm, m[:, 1])]])
            pad = 1.0
            blo = (sub.min(0) - pad) / [kx, ky] + ring[0]
            bhi = (sub.max(0) + pad) / [kx, ky] + ring[0]
            near = np.flatnonzero((boxes[:, 0] <= bhi[0]) & (boxes[:, 2] >= blo[0]) & (boxes[:, 1] <= bhi[1]) &
                                  (boxes[:, 3] >= blo[1]))
            gl, gj = [], []
            for j in near:
                if j == i or specs[j] is None:
                    continue
                for g in _raw_ground_parts(feats, j):
                    gl.append((np.asarray(g, np.float64) - ring[0]) * [kx, ky])
                    gj.append(j)
            lo0, hi0 = lo_m, hi_m           # (arcs along sub: from lo0; a crossing at either end of it does not count)
            for g, sc, sin in (_seg_hits(sub, gl) if gl else ()):
                sc += lo0
                if not lo0 < sc < hi0:
                    continue
                j = gj[g]
                sin = max(sin, SKEW_MIN_SIN)
                cos = math.sqrt(max(0.0, 1.0 - sin * sin))
                h = (specs[j].width / 2 + SKEW_MARGIN_M + SKEW_SLOPE_M) / sin + (up_w / 2) * cos / sin
                lo_m = min(lo_m, max(a0 * L - SKEW_MAX_M, sc - h))
                hi_m = max(hi_m, min(b0 * L + SKEW_MAX_M, sc + h))
            prev = out[-1][1] if out else 0.0   # (never into a neighbouring range; past the feature's ends: "over")
            nxt = rngs[n + 1][0] if n + 1 < len(rngs) else 1.0
            if lo_m < 0 and a0 <= 1e-9:
                over[0] = max(over[0], -lo_m)
            if hi_m > L and b0 >= 1.0 - 1e-9:
                over[1] = max(over[1], hi_m - L)
            a, b = max(lo_m / L, prev, 0.0), min(hi_m / L, nxt, 1.0)
        out.append((a, b, k))
    got = memo[i] = {"ranges": out, "over": over}
    return got


# A tunnel's portal moved out past a ground road crossing over the tunnel's road at it (earthfast.tunnel_reach): its
# carriageway and TUNNEL_PORTAL_CLEAR_M past it (along the road: / the sine of the crossing, at least TUNNEL_ROAD_MIN_SIN),
# a crossing up to TUNNEL_PORTAL_REACH_M outside the portal, TUNNEL_PORTAL_GROW_M at most. OSM often ends an underpass's
# tunnel inside the carriageway over it: the portal's approach cut ran through the A-7's lanes near Malaga (GitHub #450).
TUNNEL_PORTAL_CLEAR_M = 3.0
TUNNEL_PORTAL_REACH_M = 10.0
TUNNEL_PORTAL_GROW_M = 30.0
TUNNEL_PORTAL_END_M = 2.0   # m: a crossing this close to the other road's end is a junction
# A portal inside the carriageway of a road running along the tunnel (the map's ground stretch of a service tunnel laid
# in a motorway's lanes: the Gotthard road tunnel's service gallery at Goeschenen, an arch and a mound in the A2's
# lanes, 2026-10-28) moves into the tunnel until clear, TUNNEL_PORTAL_SHRINK_M at most; a tunnel shorter than
# TUNNEL_PORTAL_MIN_M left that way is none.
TUNNEL_PORTAL_SHRINK_M = 60.0
TUNNEL_PORTAL_MIN_M = 8.0


def _tunnel_reach(feats, i, rngs):
    """rngs with feature i's tunnel ranges grown over the ground roads crossing it at their portals
    (TUNNEL_PORTAL_*), never into a neighbouring range. Pure given feats."""
    specs = road_specs(feats)
    rings = [np.asarray(r, np.float64) for part in feats.parts(i) for r in part if len(r) >= 2]
    if specs[i] is None or not rings:
        return rngs
    ring = rings[0]
    kx, ky = 111320.0 * math.cos(math.radians(float(ring[0, 1]))), 111320.0
    m = (ring - ring[0]) * [kx, ky]
    sm = np.r_[0.0, np.cumsum(np.hypot(*np.diff(m, axis=0).T))]
    L = float(sm[-1])
    if L <= 0:
        return rngs
    tun = [n for n, (_, _, k) in enumerate(rngs) if k == "tunnel"]
    pad = (TUNNEL_PORTAL_REACH_M + TUNNEL_PORTAL_GROW_M + 1.0) / np.array([kx, ky])
    boxes = _feature_boxes(feats)
    near = np.zeros(len(boxes), bool)
    for n in tun:                   # (the roads near the portals only)
        for f in rngs[n][:2]:
            p = (np.array([np.interp(f * L, sm, m[:, 0]), np.interp(f * L, sm, m[:, 1])]) / [kx, ky]) + ring[0]
            near |= ((boxes[:, 0] <= p[0] + pad[0]) & (boxes[:, 2] >= p[0] - pad[0]) & (boxes[:, 1] <= p[1] + pad[1]) &
                     (boxes[:, 3] >= p[1] - pad[1]))
    gl, hw = [], []
    for j in np.flatnonzero(near):
        if j == i or specs[j] is None:
            continue
        for g in _raw_ground_parts(feats, j):
            gl.append((np.asarray(g, np.float64)[:, :2] - ring[0]) * [kx, ky])
            hw.append(specs[j].width / 2)
    if not gl:
        return rngs
    import fast
    fast.require(97, "tunnel_reach")
    got = fast._ef.tunnel_reach(
        np.ascontiguousarray(m), np.ascontiguousarray(sm),
        np.ascontiguousarray([[rngs[n][0] * L, rngs[n][1] * L] for n in tun], np.float64),
        np.ascontiguousarray(np.concatenate(gl)), np.r_[0, np.cumsum([len(g) for g in gl])].astype(np.int64),
        np.asarray(hw, np.float64), np.array([TUNNEL_PORTAL_CLEAR_M, TUNNEL_ROAD_MIN_SIN, TUNNEL_PORTAL_REACH_M,
                                              TUNNEL_PORTAL_GROW_M, TUNNEL_PORTAL_END_M, TUNNEL_PORTAL_SHRINK_M,
                                              TUNNEL_PORTAL_MIN_M], np.float64))
    out = list(rngs)
    for q, n in enumerate(tun):
        prev = out[n - 1][1] if n > 0 else 0.0
        nxt = out[n + 1][0] if n + 1 < len(out) else 1.0
        out[n] = (max(float(got[q, 0]) / L, prev), min(float(got[q, 1]) / L, nxt), "tunnel")
    return [r for r in out if r[1] > r[0]]      # (a tunnel left shorter than nothing: ground)


def _bridge_ends(feats):
    """(indices (k,), end points lon/lat (k, 2, 2)) of the features with a bridge range, once per cell."""
    got = feats.__dict__.get("_bridge_ends")
    if got is None:
        idx, ends = [], []
        for b in range(len(feats)):
            if not any(k == "bridge" for _, _, k in raw_level_ranges(feats, b)):
                continue
            rings = [r for part in feats.parts(b) for r in part if len(r) >= 2]
            if rings:
                idx.append(b)
                ends.append((rings[0][0], rings[0][-1]))
        got = (np.asarray(idx, np.int64), np.asarray(ends, np.float64).reshape(-1, 2, 2))
        feats.__dict__["_bridge_ends"] = got
    return got


def _near_bridge_ends(feats):
    """Per feature: may an end of its first ring lie within _skew_decks' 0.75 m of a bridge feature's end? A box test
    in lon / lat, wider than that everywhere (earthfast.ends_near; most features are nowhere near a bridge, and their
    exact test was most of _skew_decks' time, 2026-10-25). None: no pre-check. Kept on feats."""
    got = feats.__dict__.get("_near_bridge_ends")
    if got is not None:
        return got
    import fast
    bidx, bends = _bridge_ends(feats)
    n = len(feats)
    ends = np.full((n, 2, 2), np.nan)
    exact = np.zeros(n, bool)       # (no first ring of 2 points or more: _skew_decks' own test, which finds its ring)
    for i in range(n):
        if feats.part_off[i + 1] <= feats.part_off[i]:
            continue                # (no parts: nothing to lengthen)
        r = feats.ring_off[feats.part_off[i]]
        a, b = feats.coord_off[r], feats.coord_off[r + 1]
        if b - a >= 2:
            ends[i, 0], ends[i, 1] = feats.lonlat[a], feats.lonlat[b - 1]
        else:
            exact[i] = True
    lat = float(np.nanmax(np.abs(ends[:, :, 1]))) if np.isfinite(ends[:, :, 1]).any() else 0.0
    if not np.isfinite(lat) or lat > 89.0:
        feats.__dict__["_near_bridge_ends"] = got = None
        return got
    ty = 2.0 / 111320.0                                         # (2 m: 0.75 m and room)
    tx = ty / max(math.cos(math.radians(min(lat + 0.1, 89.0))), 1e-3)
    fast.require(75, "ends_near")
    m = np.asarray(fast._ef.ends_near(np.ascontiguousarray(ends.reshape(-1, 2)),
                                     np.ascontiguousarray(bends.reshape(-1, 2)), float(tx), float(ty)), bool)
    got = m.reshape(n, 2).any(axis=1) | exact
    feats.__dict__["_near_bridge_ends"] = got
    return got


def _skew_decks(feats, i, rngs):
    """rngs (feature i's raw_level_ranges) with its bridge ranges lengthened over the roads crossing under them
    (_bridge_reach), and a bridge range at an end of i where a bridge feature joined there reaches past its own end
    onto i (Overture makes most short bridges a segment of their own). Pure given feats."""
    if any(k == "tunnel" for _, _, k in rngs):
        rngs = _tunnel_reach(feats, i, rngs)
    if not SKEW_ON:
        return rngs
    if any(k == "bridge" for _, _, k in rngs):
        rngs = _bridge_reach(feats, i)["ranges"]
    bidx, bends = _bridge_ends(feats)
    if not len(bidx):
        return rngs
    near = _near_bridge_ends(feats)
    if near is not None and not near[i]:
        return rngs                             # (no end of it near a bridge's end: nothing joins it)
    rings = [np.asarray(r, np.float64) for part in feats.parts(i) for r in part if len(r) >= 2]
    if not rings:
        return rngs
    ring = rings[0]
    kx, ky = 111320.0 * math.cos(math.radians(float(ring[0, 1]))), 111320.0
    tol = 0.75
    add = []
    L = None
    for end, p in ((0, ring[0]), (1, ring[-1])):
        if any((a <= 1e-9 if end == 0 else b >= 1.0 - 1e-9) for a, b, _ in rngs):
            continue                            # off the ground at that end already
        d = np.hypot((bends[:, :, 0] - p[0]) * kx, (bends[:, :, 1] - p[1]) * ky)     # (k, 2) to each bridge's ends
        best = 0.0
        for r, e2 in zip(*np.nonzero(d < tol)):
            b = int(bidx[r])
            if b != i:
                best = max(best, _bridge_reach(feats, b)["over"][int(e2)])
        if best > 0:
            if L is None:
                L = float(np.hypot(*(np.diff(ring, axis=0) * [kx, ky]).T).sum())
            if L <= 0:
                break
            f = min(best, SKEW_MAX_M) / L
            add.append((0.0, min(f, 1.0), "bridge") if end == 0 else (max(1.0 - f, 0.0), 1.0, "bridge"))
    if not add:
        return rngs
    out = []
    for a, b, k in sorted(list(rngs) + add):
        if out and a <= out[-1][1] + 1e-9:     # (overlapping a range already there: joined, as level_ranges does)
            pa, pb, pk = out[-1]
            out[-1] = (pa, max(pb, b), "tunnel" if "tunnel" in (pk, k) else "bridge")
        else:
            out.append((a, b, k))
    return out


# A ground piece of a road shorter than GAP_DECK_M with a deck on both sides (a bridge range of its own, or another
# road's bridge going on from its end with nothing else meeting there) and no road joining it is deck too: a viaduct's
# stretch the map leaves untagged between two bridges was drawn on the ground, each deck eased down to it and up again
# (with the measured decks, 10-15 m: the Springfield Interchange, 2026-10-28). A junction on it keeps it on the ground.
GAP_DECK_ON = os.environ.get("BNE_DECK_GAPS", "on") != "off"
GAP_DECK_M = 60.0
GAP_JUNCTION_M = 1.5
GAP_JOIN_M = 0.75           # (_skew_decks' tolerance for a road's end at another's)


def _first_ring_ends(feats):
    """(n, 2, 2) lon / lat of each feature's first ring's two ends (NaN: none), kept on feats."""
    got = feats.__dict__.get("_first_ring_ends")
    if got is None:
        n = len(feats)
        got = np.full((n, 2, 2), np.nan)
        for i in range(n):
            if feats.part_off[i + 1] <= feats.part_off[i]:
                continue
            r = feats.ring_off[feats.part_off[i]]
            a, b = feats.coord_off[r], feats.coord_off[r + 1]
            if b - a >= 2:
                got[i, 0], got[i, 1] = feats.lonlat[a][:2], feats.lonlat[b - 1][:2]
        feats.__dict__["_first_ring_ends"] = got
    return got


def _deck_gaps(feats, i, rngs):
    """rngs (feature i's level ranges) with its short ground gaps between decks made bridge (GAP_DECK_*;
    earthfast.deck_gaps). A road with a tunnel range keeps its own. Pure given feats."""
    if not GAP_DECK_ON or any(k == "tunnel" for _, _, k in rngs):
        return rngs
    if not any(k == "bridge" for _, _, k in rngs):
        near = _near_bridge_ends(feats)
        if near is not None and not near[i]:
            return rngs                         # (no bridge of its own, none ending at it)
    if _structure_width(feats, i) is None:
        return rngs                             # (railways: their own pieces)
    rings = [np.asarray(r, np.float64) for part in feats.parts(i) for r in part if len(r) >= 2]
    if not rings:
        return rngs
    ring = rings[0][:, :2]
    kx, ky = 111320.0 * math.cos(math.radians(float(ring[0, 1]))), 111320.0
    m = (ring - ring[0]) * [kx, ky]
    L = float(np.hypot(*np.diff(m, axis=0).T).sum())
    if L <= 0:
        return rngs
    specs = road_specs(feats)
    ends = _first_ring_ends(feats)
    boxes = _feature_boxes(feats)
    end_deck = []
    for e, p in ((0, ring[0]), (1, ring[-1])):
        d = np.hypot((ends[:, :, 0] - p[0]) * kx, (ends[:, :, 1] - p[1]) * ky)
        d[i] = np.inf
        hit = [(int(j), int(k)) for j, k in zip(*np.nonzero(d < GAP_JOIN_M))]
        ok = len(hit) == 1
        if ok:
            j, k = hit[0]
            rj = raw_level_ranges(feats, j)
            ok = any(kk == "bridge" and (a <= 1e-6 if k == 0 else b >= 1.0 - 1e-6) for a, b, kk in rj)
        end_deck.append(ok)
    bridges = [(a * L, b * L) for a, b, k in rngs if k == "bridge"]
    pad = (GAP_JUNCTION_M + 1.0) / np.array([kx, ky])
    lo, hi = ring.min(0) - pad, ring.max(0) + pad
    near = np.flatnonzero((boxes[:, 0] <= hi[0]) & (boxes[:, 2] >= lo[0]) & (boxes[:, 1] <= hi[1]) &
                          (boxes[:, 3] >= lo[1]))
    others = [(ends[j, k] - ring[0]) * [kx, ky] for j in near if j != i and specs[j] is not None for k in (0, 1)
              if np.isfinite(ends[j, k]).all()]
    import fast
    fast.require(105, "deck_gaps")
    got = np.asarray(fast._ef.deck_gaps(np.ascontiguousarray(m), np.asarray(bridges, np.float64).reshape(-1, 2),
                                        np.asarray(end_deck, np.int64),
                                        np.asarray(others, np.float64).reshape(-1, 2), GAP_DECK_M, GAP_JUNCTION_M),
                     np.float64).reshape(-1, 2)
    if len(got) == len(bridges) and np.allclose(got, np.asarray(bridges, np.float64).reshape(-1, 2)):
        return rngs
    out = [(float(min(max(a / L, 0.0), 1.0)), float(min(max(b / L, 0.0), 1.0)), "bridge") for a, b in got]
    out[0] = (0.0, out[0][1], "bridge") if got[0, 0] <= 1e-6 else out[0]
    out[-1] = (out[-1][0], 1.0, "bridge") if got[-1, 1] >= L - 1e-6 else out[-1]
    return out


# Ground stretches the lidar shows on a structure (roadz.py, US 3DEP; the user, 2026-10-29: "Sure add the check"): a
# drawn road's ground piece that touches a deck's end (rule_level_ranges) is measured along its first LIDAR_GAP_REACH_M
# with the decks' own lidar read, and is deck where its surface goes on from the deck's end well over the DTM (an
# untagged viaduct stretch: Springfield's, a measured deck eased 10-15 m down to it). roadz answers per map cell (the
# cell holding the deck's end), the same in every process (kept on disk; a cell whose lidar is not there in time keeps
# the rule for the session). BNE_LIDAR_GAPS=off: none.
LIDAR_GAPS_ON = os.environ.get("BNE_LIDAR_GAPS", "on") != "off"
LIDAR_GAP_TOUCH_M = 1.0     # m: a ground piece's end this near a deck piece's end goes on from it
LIDAR_GAP_REACH_M = 150.0   # m of a ground piece measured from that end at most


def _ll_metres(lat):
    return 111320.0 * math.cos(math.radians(lat)), 111320.0


def rule_level_ranges_near(feats):
    """{i: rule_level_ranges} of the features that can have a bridge range by the rule: one of their own (raw) or a
    bridge ending near them (_skew_decks / _deck_gaps add none elsewhere). Kept on feats."""
    got = feats.__dict__.get("_rule_near")
    if got is not None:
        return got
    near = _near_bridge_ends(feats)
    memo = feats.__dict__.get("_raw_level_ranges", {})
    got = {}
    for i in range(len(feats)):
        raw = memo.get(i)
        raw = raw if raw is not None else raw_level_ranges(feats, i)
        if any(k == "bridge" for _, _, k in raw) or near is None or near[i]:
            got[i] = rule_level_ranges(feats, i)
    feats.__dict__["_rule_near"] = got
    return got


def lidar_gap_candidates(feats):
    """[(fid, line lon/lat (k, 2) from the deck's end outward, LIDAR_GAP_REACH_M at most, half width m)] of the drawn
    roads' ground pieces (rule_level_ranges) with an end within LIDAR_GAP_TOUCH_M of a bridge piece's end (any road's).
    Pure given feats; kept on it."""
    got = feats.__dict__.get("_lidar_gap_candidates")
    if got is not None:
        return got
    n = len(feats)
    rule = [None] * n
    ends = []
    for i, rr in rule_level_ranges_near(feats).items():
        rule[i] = rr                            # (the others: no bridge of their own, none ending at them)
        for r in _parts_by_level_new(feats, i, "bridge", rule[i]):
            r = np.asarray(r, np.float64)
            ends += [r[0, :2], r[-1, :2]]
    out = []
    if ends:
        E = np.asarray(ends, np.float64)
        for i in range(n):
            if rule[i] is None or any(k == "tunnel" for _, _, k in rule[i]):
                continue
            w = _structure_width(feats, i)
            if w is None:
                continue
            fid = str(feats.ids[i])
            for r in _parts_by_level_new(feats, i, "ground", rule[i]):
                r = np.asarray(r, np.float64)[:, :2]
                if len(r) < 2:
                    continue
                kx, ky = _ll_metres(float(r[0, 1]))
                for at in (0, -1):
                    d = np.hypot((E[:, 0] - r[at, 0]) * kx, (E[:, 1] - r[at, 1]) * ky)
                    if not (d < LIDAR_GAP_TOUCH_M).any():
                        continue
                    line = r if at == 0 else r[::-1]
                    m = (line - line[0]) * [kx, ky]
                    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(m, axis=0).T))]
                    if s[-1] > LIDAR_GAP_REACH_M:
                        keep = s < LIDAR_GAP_REACH_M
                        cut = [np.interp(LIDAR_GAP_REACH_M, s, line[:, k]) for k in (0, 1)]
                        line = np.vstack([line[keep], cut])
                    out.append((fid, np.ascontiguousarray(line), max(2.5, w / 2)))
    feats.__dict__["_lidar_gap_candidates"] = out
    return out


def _lidar_cell(cid):
    """May the lidar know cell cid ("z/x/y", or a test's name: yes)? (No candidates looked for elsewhere.)"""
    try:
        z, x, y = (int(v) for v in str(cid).split("/"))
    except ValueError:
        return True
    import mapcells
    import roadz
    la, lo = mapcells.CellKey(z, x, y).center()
    return "lidar" in roadz.sources_at(la, lo, True)


def _lidar_answers(feats):
    """{fid: [(lon/lat from, lon/lat to)]} of the elevated stretches roadz found on feats' candidates (lidar_gap_
    candidates), from the answers of the cells holding their deck ends; {} without lidar. Kept on feats."""
    st = feats.__dict__
    got = st.get("_lidar_answers")
    if got is not None:
        return got
    lock = st.setdefault("_lidar_answers_lock", threading.Lock())
    with lock:
        got = st.get("_lidar_answers")
        if got is not None:
            return got
        got = {}
        try:
            import roadz
            prov = roadz.PROVIDER
            cid = st.get("_bnge_cell")
            if (LIDAR_GAPS_ON and prov is not None and prov.enabled and prov.lidar and cid is not None
                    and hasattr(prov, "gap_answers") and _lidar_cell(cid[0])):
                cands = lidar_gap_candidates(feats)
                if cands:
                    import mapcells
                    owners = sorted({mapcells.cell_of(float(c[1][0, 1]), float(c[1][0, 0])) for c in cands}, key=str)
                    fids = {c[0] for c in cands}
                    for _key, gaps in prov.gap_answers(owners).items():
                        if gaps is None:
                            st["_lidar_rule"] = True
                        for fid, a, b in gaps or ():
                            if fid in fids:
                                got.setdefault(fid, []).append((tuple(a), tuple(b)))
        except Exception as ex:             # (the rule rather than no ranges)
            log.debug("lidar gaps: %s: %s", type(ex).__name__, ex)
            got = {}
        st["_lidar_answers"] = got
    return got


def _ring_fraction(ring, p):
    """The fraction of lon/lat polyline ring's length (sub_polyline's metric) at its point nearest p (lon, lat)."""
    ring = np.asarray(ring, np.float64)[:, :2]
    c = math.cos(math.radians(float(np.mean(ring[:, 1]))))
    q = ring * [c, 1.0]
    pp = np.asarray(p, np.float64)[:2] * [c, 1.0]
    a, b = q[:-1], q[1:]
    ab = b - a
    l2 = np.maximum((ab ** 2).sum(1), 1e-30)
    t = np.clip(((pp - a) * ab).sum(1) / l2, 0.0, 1.0)
    d = np.hypot(*(a + t[:, None] * ab - pp).T)
    k = int(np.argmin(d))
    seg = np.sqrt(l2)
    s = np.r_[0.0, np.cumsum(seg)]
    return float((s[k] + t[k] * seg[k]) / max(s[-1], 1e-30))


def _lidar_gaps(feats, i, rngs):
    """rngs (rule_level_ranges) with the stretches the lidar found elevated (_lidar_answers) made bridge, never over a
    tunnel's range. Pure given feats and roadz's answer."""
    if not LIDAR_GAPS_ON or any(k == "tunnel" for _, _, k in rngs):
        return rngs
    got = _lidar_answers(feats).get(str(feats.ids[i]))
    if not got:
        return rngs
    rings = [np.asarray(r, np.float64) for part in feats.parts(i) for r in part if len(r) >= 2]
    if not rings:
        return rngs
    add = []
    for a, b in got:
        fa, fb = sorted((_ring_fraction(rings[0], a), _ring_fraction(rings[0], b)))
        if fb - fa > MIN_PART:
            add.append((0.0 if fa < 1e-4 else fa, 1.0 if fb > 1.0 - 1e-4 else fb, "bridge"))
    out = []
    for a, b, k in sorted(list(rngs) + add):
        if out and a <= out[-1][1] + 1e-9:
            out[-1] = (out[-1][0], max(out[-1][1], b), "bridge")
        else:
            out.append((a, b, k))
    return out


def _level_ranges(feats, i):
    """tag_level_ranges with the grade separations the map has no bridge for (gradesep.py: the cell's own, from its
    roads and its neighbours'), where no range of the map's is, and its underpasses' tunnel ranges made ground."""
    out = tag_level_ranges(feats, i)
    hook = feats.__dict__.get("_bnge_gradesep")
    extra = hook(feats).get(i) if hook is not None else None
    if not extra:
        return out
    for a, b, _k in (r for r in extra if r[2] == "ground"):       # (an underpass: its tunnel range is ground)
        cut = []
        for fa, fb, k in out:
            if k != "tunnel" or fb <= a or fa >= b:
                cut.append((fa, fb, k))
                continue
            if fa < a - 1e-5:
                cut.append((fa, a, k))
            if fb > b + 1e-5:
                cut.append((b, fb, k))
        out = cut
    add = []
    for a, b, _k in (r for r in extra if r[2] == "bridge"):
        for fa, fb, _ in out:
            if fb <= a or fa >= b:
                continue
            if fa > a:
                add.append((a, fa, "bridge"))
            a = max(a, fb)
            if a >= b:
                break
        if b - a > 1e-5:
            add.append((a, b, "bridge"))
    merged = []                                 # (touching bridge ranges joined: the map's fractions are float32s)
    for a, b, k in sorted(out + add):
        if merged and k == merged[-1][2] and a <= merged[-1][1] + 1e-5:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b), k)
        else:
            merged.append((a, b, k))
    return merged


def tag_level_ranges(feats, i):
    """Feature i's off-ground ranges as the map's flags and level rules have them (level_ranges, before gradesep)."""
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


def _parts_by_level_new(feats, i, want, rngs=None):
    rngs = level_ranges(feats, i) if rngs is None else rngs
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
    out = []
    for part in feats.parts(i):
        for ring in part:
            if len(ring) < 2:
                continue
            for a, b in pieces:
                r = ring if (a, b) == (0.0, 1.0) else sub_polyline(ring, a, b)
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


# surfaces that belong to the ground layer, not to a road decal: rock and grass are rough, mask-driven terrain
# materials (gridmap v2 draws them exactly that way), so the mod paints their ground (surface.py) instead of laying
# a smooth ribbon over it (2026-10-22)
ROCKISH = frozenset({"rock", "rocky", "bedrock"})
GRASSY = frozenset({"grass", "grass_paver"})


def surface_token(surface):
    """A surface rule value's base token: the detail after a colon and a second value after a semicolon dropped
    (`sett:pattern` -> `sett`, `sett;asphalt` -> `sett`); None for no value. surface_kind and look_of read it."""
    if surface is None:
        return None
    return str(surface).lower().split(";")[0].split(":")[0].strip()


def surface_kind(cls, surface):
    """'paved', 'cobble', 'gravel' or 'dirt' from the class and the surface rule value (None if unknown).

    The value's base token decides: OSM writes the detail after a colon (`cobblestone:flattened`,
    `paving_stones:30`, `sett:pattern`) and more than one value after a semicolon, and matched whole they all fell
    through to "dirt", which is drawn with gravel's own material (LOOK) and painted DIRT: İzmir's sett streets read
    as gravel tracks (2026-10-22). Interlocking pavers have their own names in the map. A value not understood at
    all is the class's own surface, not dirt: an unknown tag must not turn a residential street into a track."""
    if surface is not None:
        s = surface_token(surface)
        if s in PAVED:
            return "paved"
        if s in COBBLE or s in COBBLE_ALIASES:
            return "cobble"
        if s in GRAVEL:
            return "gravel"
        if s in UNPAVED:
            return "dirt"
    return "paved" if STYLE.get(cls, STYLE["unknown"])[1] else "dirt"


def carriageway(cls, kind, urban, rule_width=None, oneway=False, lanes=None):
    """Paved / dirt width (m) of a road: the width rule if any, else the driving width (OpenStreetMap's lanes x the
    class's LANE_W when known, else the class's) plus parking lanes (urban paved streets; one side on a one-way street)
    or edge strips (rural paved roads). One-way: ONEWAY_STYLE's driving width (motorways and trunks are per carriageway
    already). Pure."""
    if rule_width is not None:
        return rule_width
    w = STYLE.get(cls, STYLE["unknown"])[0]
    if oneway and cls in ONEWAY_STYLE:
        w = ONEWAY_STYLE[cls]
    if lanes:
        w = lanes * LANE_W.get(cls, LANE_W_DEFAULT)
    if kind not in BUILT:
        return w
    if lanes and not urban and oneway and cls == "motorway":
        return w + 2 * SHOULDER_M
    if urban and not oneway and cls in URBAN_ARTERIAL and not lanes:
        return URBAN_ARTERIAL[cls]          # four lanes, no parking (a walled street is measured instead)
    if urban and cls in PARKING_CLASSES:
        return w + (1 if oneway else 2) * PARKING_M
    if not urban and cls != "service":
        if oneway and cls == "motorway":
            return MOTORWAY_RURAL_M + 2 * SHOULDER_M
        return w + 2 * EDGE_STRIP_CLASS_M.get(cls, EDGE_STRIP_M)
    return w


def rural_motorway(spec):
    """One carriageway of a rural motorway: two lanes between shoulders (carriageway)."""
    return spec.cls == "motorway" and spec.oneway and not spec.urban and spec.kind in BUILT


def edge_margin(spec):
    """m of a drawn carriageway beside its driving lanes on each side, parking aside: a rural road's edge strip or
    a rural motorway's shoulder (carriageway), 0 in towns."""
    if spec.urban or spec.kind not in BUILT or spec.cls == "service":
        return 0.0
    return motorway_shoulder(spec) if rural_motorway(spec) else EDGE_STRIP_CLASS_M.get(spec.cls, EDGE_STRIP_M)


def motorway_shoulder(spec):
    """A rural motorway carriageway's shoulder: SHOULDER_M, or on a ramp (one lane, LINK_M) what one LINK_LANE_M lane
    leaves (GitHub #107: a 6 m ramp's edge lines stood 1.6 m apart, inside the motorway's 2.2 m shoulders)."""
    return float(np.clip((spec.width - LINK_LANE_M) / 2, EDGE_LINE["inset"], SHOULDER_M))


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
            if sp.lanes:                # OpenStreetMap counts this way's lanes as a two-way road (a divided road's
                continue                # carriageways are one-way ways there): Portland's Rosa Parks Way, 1+1 lanes and
                                        # a turn lane, was drawn as two one-ways of 3 lanes each (2026-10-27)
            d = (1 if best[1] > 0 else -1) * (-1 if left_hand else 1)
            w = sp.width if (_rule_value(feats.rules.get("width"), i) is not None
                             or _osm(feats, "width", i, *OSM_WIDTH_M) is not None) else \
                carriageway(sp.cls, sp.kind, sp.urban, None, True, sp.lanes)
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
    if (_rule_value(feats.rules.get("width"), i) is not None
            or _osm(feats, "width", i, *OSM_WIDTH_M) is not None):
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
    if got is None:             # (the measured widths on the specs without them: those are asked for too, _bridge_reach)
        extra = (widths is not None, bool(left_hand))
        if urban_fn is None:    # (the land cover's are the server's own: not shared, cell_facts)
            got = _specs_from(_facts_load(feats, "specs", extra), len(feats))
        if got is None:
            import revisit
            f0 = revisit.faults()
            got = (_road_specs(feats, urban_fn, left_hand) if widths is None else
                   _measured_widths(feats, road_specs(feats, urban_fn, None, left_hand), widths))
            if urban_fn is None and revisit.faults() == f0:
                _facts_store(feats, "specs", extra, [None if sp is None else _spec_json(sp) for sp in got])
        got = memo.setdefault(k, got)
    return list(got)


def _spec_json(sp):
    return {f: (v.item() if hasattr(v, "item") else list(v) if isinstance(v, tuple) else v)
            for f, v in sp._asdict().items()}


def _specs_from(data, n):
    """road_specs' list from the shared facts (_spec_json), or None."""
    if not isinstance(data, list) or len(data) != n:
        return None
    try:
        return [None if d is None else Spec(**{**d, "sides": tuple(d["sides"]) if d.get("sides") is not None else None})
                for d in data]
    except (TypeError, KeyError):
        return None


# OpenStreetMap's sidewalk tag (osmtags.py) -> walk_sides' sides; "separate" (a footway mapped on its own beside the
# road) and "no" give none. Other values (or none) leave the class and land-cover rule.
SIDEWALK_TAG = {"both": (1, -1), "left": (1,), "right": (-1,), "no": (), "none": (), "separate": ()}


def _osm_sidewalks(feats, specs):
    """specs with sides set where OpenStreetMap's sidewalk tag says which (SIDEWALK_TAG). Pure."""
    tags = feats.attrs.get("osm_sidewalk")
    if tags is None:
        return specs
    out = list(specs)
    for i, sp in enumerate(out):
        if sp is None or sp.kind not in BUILT:
            continue
        tag = str(tags[i]).strip().lower()
        if tag in SIDEWALK_TAG:
            out[i] = sp._replace(sides=SIDEWALK_TAG[tag])
    return out


UNBUILT_SHARE = 0.5     # a feature Overture flags is_under_construction over this share of its length is not built


def unbuilt(feats, i):
    """Is feature i a road not built yet (OpenStreetMap's highway=construction / proposed: osmtags osm_highway, or
    Overture's is_under_construction flag over most of it)? Not drawn, as on the stock map views (GitHub #184). (The
    Gotthard road tunnel's second bore, highway=construction until ~2029, was built beside the real one, its portal in
    the A2's lanes: Layercake leaves construction ways out, and the join took the open tunnel's tags, 2026-10-28.)"""
    col = feats.attrs.get("osm_highway")
    if col is not None and str(col[i]) in ("construction", "proposed"):
        return True
    t = feats.rules.get("flags")
    if t is None or len(t.feature) == 0:
        return False
    share = 0.0
    for j in t.rows(i):
        if "is_under_construction" in str(t.value[j]):
            a, b = float(t.start[j]), float(t.end[j])
            share += (b - a) if b > a else 1.0      # (no range: the whole feature)
    return share >= UNBUILT_SHARE


def _road_specs(feats, urban_fn, left_hand):
    out = []
    rules = feats.rules
    ask = []                                    # (index in out, cls, kind, rule_w, coordinate range)
    developed_both = {}
    cr = rules.get("connectors")
    ncon = np.bincount(np.asarray(cr.feature), minlength=len(feats)) if cr is not None and len(cr.feature) else None
    lens = _lengths(feats) if ncon is not None else None
    for i in range(len(feats)):
        cls = str(feats.attrs["class"][i]) if "class" in feats.attrs else "unknown"
        if str(feats.attrs.get("subtype", ["road"] * len(feats))[i]) != "road" or cls not in STYLE or unbuilt(feats, i):
            out.append(None)
            continue
        rngs = raw_level_ranges(feats, i)        # (raw: _skew_decks reads these specs)
        if rngs and sum(b - a for a, b, _ in rngs) > 1.0 - MIN_PART:     # entirely off the ground
            out.append(None)
            continue
        width = _rule_value(rules.get("width"), i)
        rule_w = float(width) if width is not None and 1.0 <= float(width) <= 40.0 else None
        if rule_w is None:
            rule_w = _osm(feats, "width", i, *OSM_WIDTH_M)      # OpenStreetMap's width (osmtags.py)
        lanes = _osm(feats, "lanes", i, *OSM_LANES)
        lanes = int(lanes) if lanes is not None else None
        surf = _rule_value(rules.get("surface"), i)
        kind = surface_kind(cls, surf)
        ow = _rule_value(rules.get("oneway"), i) is not None
        link = _rule_value(rules.get("subclass"), i) == "link"
        if rule_w is None and link and lanes is None:
            rule_w = LINK_M[1] if ow else LINK_M[2]
        dk = _dir_kmh(feats, i, ncon, lens)
        if dk[1] is None:                       # OpenStreetMap's speed limit where Overture has none
            ms = _osm(feats, "maxspeed", i, 5.0, 200.0)
            if ms is not None:
                dk = (dk[0], ms, dk[2])
        if urban_fn is not None and rule_w is None and kind in BUILT:
            a = feats.coord_off[feats.ring_off[feats.part_off[i]]]
            b = feats.coord_off[feats.ring_off[feats.part_off[i + 1]]]
            if b > a:
                ask.append((len(out), cls, kind, a, b, ow, dk, lanes, surface_token(surf), link))
                out.append(None)
                continue
        out.append(Spec(carriageway(cls, kind, False, rule_w, ow, lanes), kind, False, cls, ow, 2.5, *dk,
                        lanes=lanes, surf=surface_token(surf), link=link))
    if ask:
        ll = np.concatenate([feats.lonlat[a:b] for _, _, _, a, b, *_ in ask])
        left, right = _beside(ll, [b - a for _, _, _, a, b, *_ in ask], URBAN_BESIDE_M)
        n = len(ll)
        pts = np.concatenate([ll, left, right])
        u_all = np.asarray(urban_fn(pts[:, 1], pts[:, 0]), bool)      # (one call: its cost is per call)
        u, by, both = u_all[:n], u_all[n:2 * n] | u_all[2 * n:], u_all[n:2 * n] & u_all[2 * n:]
        k = 0
        for j, cls, kind, a, b, ow, dk, lanes, token, link in ask:
            developed_both[j] = float(np.mean(both[k:k + (b - a)])) >= URBAN_BOTH_SHARE
            urban = (float(np.mean(u[k:k + (b - a)])) >= URBAN_SHARE
                     and float(np.mean(by[k:k + (b - a)])) >= URBAN_BESIDE_SHARE
                     and (cls not in ("motorway", "trunk")
                          or float(np.mean(both[k:k + (b - a)])) >= URBAN_BOTH_SHARE))
            k += b - a
            out[j] = Spec(carriageway(cls, kind, urban, None, ow, lanes), kind, urban, cls, ow, 2.5, *dk, lanes=lanes,
                          surf=token, link=link)
    out = _rings(feats, out)
    out = dual_carriageways(feats, out, left_hand)
    # A primary's opposite carriageway can supply all of its apparent development
    # on one side. Apply the major-highway rule after pairing, without changing
    # ordinary town main streets or explicitly mapped sidewalk/lighting tags.
    for j, sp in enumerate(out):
        if (sp is not None and sp.cls == "primary" and sp.median and sp.urban
                and not developed_both.get(j, True)):
            width = _rule_value(rules.get("width"), j)
            if width is None:
                width = _osm(feats, "width", j, *OSM_WIDTH_M)
            w = carriageway(sp.cls, sp.kind, False, width, sp.oneway, sp.lanes)
            out[j] = sp._replace(urban=False, width=_dual_width(sp._replace(urban=False), w, sp.median, feats, j))
    return _osm_sidewalks(feats, _rural_connectors(feats, out))


def _rural_connectors(feats, specs):
    """Short links across a rural divided highway inherit its roadside context.

    The two carriageways themselves can make a median crossing look built-up.
    Require both ends to meet distinct rural divided roads; an urban connection
    or a road merely running nearby is not evidence. OSM overrides follow this.
    """
    candidates = [i for i, sp in enumerate(specs) if sp is not None and sp.link and sp.urban
                  and sp.cls in ("trunk", "primary", "secondary", "tertiary")]
    rural = [i for i, sp in enumerate(specs) if sp is not None and not sp.urban and sp.oneway
             and sp.median and sp.cls in ("motorway", "trunk", "primary")]
    if not candidates or len(rural) < 2:
        return specs
    ends = {}
    for i in candidates + rural:
        a = feats.coord_off[feats.ring_off[feats.part_off[i]]]
        b = feats.coord_off[feats.ring_off[feats.part_off[i + 1]]]
        if b > a:
            ends[i] = feats.lonlat[[a, b - 1]]
    points = np.concatenate([ends[i] for i in rural if i in ends])
    owners = np.repeat([i for i in rural if i in ends], 2)
    scale = np.array([111320.0 * math.cos(math.radians(float(points[:, 1].mean()))), 110540.0])
    out = list(specs)
    lengths = _lengths(feats)
    for i in candidates:
        if i not in ends or lengths[i] > 2 * URBAN_BESIDE_M:
            continue
        distance = np.linalg.norm((ends[i][:, None] - points[None]) * scale, axis=2)
        attached = [set(owners[distance[j] < 2.5]) for j in (0, 1)]
        if not attached[0] or not attached[1] or len(attached[0] | attached[1]) < 2:
            continue
        sp = out[i]
        width = _rule_value(feats.rules.get("width"), i)
        if width is None:
            width = _osm(feats, "width", i, *OSM_WIDTH_M)
        out[i] = sp._replace(urban=False, width=carriageway(sp.cls, sp.kind, False, width, sp.oneway, sp.lanes))
    return out


# Roundabouts (OpenStreetMap junction=roundabout / circular: osmtags osm_junction): the circulatory roadway as wide as
# the design guides make it, not a one-way street's lane (3.5-4 m: cars clipped the central island's curb). FHWA /
# NCHRP Report 672 (Roundabouts: An Informational Guide, 2nd ed., 6.4.5 / 6.5.5): single-lane 16-20 ft (4.9-6.1 m),
# two-lane 29-32 ft (8.8-9.8 m). No parking lanes or inferred sidewalks on the ring (urban False; mapped sidewalks
# stay). (cody196's PR #363 took 9 m a lane: twice the guides.)
RING_LANE_M = 5.5               # m: a single-lane ring
RING_EXTRA_LANE_M = 4.3         # m per further circulating lane
RING_JUNCTIONS = ("roundabout", "circular")


def ring_width(lanes):
    """The circulatory roadway's width for its lane count (None: one)."""
    return RING_LANE_M + RING_EXTRA_LANE_M * (max(1, int(lanes or 1)) - 1)


def is_ring(feats, i):
    """Is feature i a mapped roundabout ring (osmtags osm_junction)?"""
    col = feats.attrs.get("osm_junction")
    return col is not None and str(col[i]) in RING_JUNCTIONS


def _rings(feats, specs):
    """specs with the mapped roundabout rings widened to ring_width (or their mapped width, if wider). Pure."""
    if feats.attrs.get("osm_junction") is None:
        return specs
    out = list(specs)
    rules = feats.rules
    for i, sp in enumerate(out):
        if sp is None or sp.kind not in BUILT or not is_ring(feats, i):
            continue
        width = _rule_value(rules.get("width"), i)
        mapped = float(width) if width is not None and 1.0 <= float(width) <= 40.0 else None
        if mapped is None:
            mapped = _osm(feats, "width", i, *OSM_WIDTH_M)
        out[i] = sp._replace(width=max(mapped or 0.0, ring_width(sp.lanes)), urban=False, median=None)
    return out


def _measured_widths(feats, specs, widths):
    """specs with the widths measured between the buildings (roadwidth.py: widths(id, lonlat) -> (carriageway m,
    sidewalk m) or None) where the map gives none. Pure given widths."""
    rules = feats.rules
    out = list(specs)
    for j, sp in enumerate(out):
        if sp is None or sp.kind not in BUILT or sp.cls not in SIDEWALK_CLASSES or sp.cls in MEASURE_SKIP:
            continue
        if _rule_value(rules.get("width"), j) is not None:
            continue
        if sp.median:       # one carriageway of a divided road: the width between the buildings is both of them
            continue        # and the median (each took all of it, one wide road over Boston Street's median)
        if is_ring(feats, j):
            continue        # (a roundabout: _rings' circulatory width; the buildings stand round the whole circle)
        a = feats.coord_off[feats.ring_off[feats.part_off[j]]]
        b = feats.coord_off[feats.ring_off[feats.part_off[j + 1]]]
        if _osm(feats, "width", j, *OSM_WIDTH_M) is not None or sp.lanes:
            continue        # (OpenStreetMap's own width, or its lane count's)
        m = widths(str(feats.ids[j]), feats.lonlat[a:b]) if b > a else None
        dflt = carriageway(sp.cls, sp.kind, True, None, sp.oneway, sp.lanes)
        # Historic streets can be one shared lane between close building fronts. Rejecting these measurements
        # kept a default carriageway wider than the street itself.
        minimum = 3.0 if sp.cls in {"residential", "living_street", "unclassified", "tertiary"} else 0.6 * dflt
        if m is not None and minimum <= m[0] <= MEASURE_MAX * dflt:
            out[j] = sp._replace(width=m[0], walk=m[1])
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
        if str(feats.attrs.get("subtype", ["road"] * len(feats))[i]) != "road" or cls not in STYLE or unbuilt(feats, i):
            out.append(None)
            continue
        # (the lengthened ranges: a bridge's range carried onto the road joined at its end (_skew_decks) is a deck
        # too; with the raw ranges that stretch was neither deck nor ground, the deck's end lost its approach ramp
        # and sat ~4.5 m low over BR-343 at Teresina, GitHub #306)
        if not any(k == want for _, _, k in (level_ranges(feats, i) if want == "bridge" else raw_level_ranges(feats, i))):
            out.append(None)
            continue
        width = _rule_value(rules.get("width"), i)
        rule_w = float(width) if width is not None and 1.0 <= float(width) <= 40.0 else None
        # (OpenStreetMap's width and lane count, as _road_specs: a 4-lane bridge was drawn at its class's 8 m, one lane
        # each way, Kommunalny Bridge in Perm, GitHub #378 by RiDexSS; tunnels too)
        if rule_w is None:
            rule_w = _osm(feats, "width", i, *OSM_WIDTH_M)
        lanes = _osm(feats, "lanes", i, *OSM_LANES)
        lanes = int(lanes) if lanes is not None else None
        surf = _rule_value(rules.get("surface"), i)
        kind = surface_kind(cls, surf)
        ow = _rule_value(rules.get("oneway"), i) is not None
        link = _rule_value(rules.get("subclass"), i) == "link"
        if rule_w is None and link and lanes is None:
            rule_w = LINK_M[1] if ow else LINK_M[2]
        out.append(Spec(carriageway(cls, kind, False, rule_w, ow, lanes), kind, False, cls, ow,
                        lanes=lanes, surf=surface_token(surf)))
    return out


class _EndJoin:
    """Line end points joined into nodes when within tol m of each other (the first point seen names the node; a later
    one joins the nearest node within tol). Rounding to a 0.5 m grid split ends a few cm apart either side of a grid
    line and joined ends 0.68 m apart on a diagonal (from cody196's PR #368). A grid of tol cells, the 3 x 3 around a
    point looked up: as cheap as the rounding. Deterministic for a given order."""

    def __init__(self, tol=0.5):
        self.tol, self.grid, self.pts = tol, {}, []

    def __call__(self, p):
        x, y = float(p[0]), float(p[1])
        gx, gy = math.floor(x / self.tol), math.floor(y / self.tol)
        best, bd = None, self.tol * self.tol
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self.grid.get((gx + dx, gy + dy), ()):
                    q = self.pts[i]
                    d = (q[0] - x) ** 2 + (q[1] - y) ** 2
                    if d < bd or (d == bd and best is not None and i < best):
                        best, bd = i, d
        if best is not None:
            return best
        self.pts.append((x, y))
        self.grid.setdefault((gx, gy), []).append(len(self.pts) - 1)
        return len(self.pts) - 1


def _chain_graph(lines):
    """Nodes (end points within 0.5 m joined) and edges (start node, end node, arc lengths) of parts."""
    join, nodes = _EndJoin(), []

    def node(p):
        k = join(p)
        if k == len(nodes):
            nodes.append(np.asarray(p[:2], np.float64))
        return k
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


# A deck part's end that no other part's end meets (within _EndJoin's 0.5 m) and no ground road goes on from (within
# END_JOIN_GROUND_M) is no abutment when it lies on another deck part's end, along it: a carriageway's split or merge
# drawn a few metres off the joint. Held at the ground as an abutment, the branch's deck rose from the street under the
# viaduct beside the main deck, a cliff of 7-14 m across a motorway's lanes the AI drove off (I-83 in Baltimore, 3.3 m
# off the joint, the Springfield Interchange; 2026-10-29). Such an end is solved as joined there (earthfast.
# deck_end_joins): within min(END_JOIN_M, the wider part's half width), directions within acos(END_JOIN_COS).
END_JOIN_ON = os.environ.get("BNE_DECK_END_JOINS", "on") != "off"
END_JOIN_M = 6.0
END_JOIN_COS = 0.8
END_JOIN_GROUND_M = 2.5     # (grading's APPROACH_TOUCH_M: a road ending this near leads onto the deck)
END_JOIN_MIN_M = 10.0       # m: shorter parts keep their ends


def _end_joins(lines, halves, ground_lines):
    """[(part, 0 | -1, (x, y))]: the free ends of deck lines (k, 2+) that join another line's end there (END_JOIN_*;
    earthfast.deck_end_joins), halves their half widths, ground_lines the ground roads near them. Pure."""
    if not END_JOIN_ON or len(lines) < 2:
        return []
    import fast
    fast.require(105, "deck_end_joins")
    pts = [np.asarray(ln, np.float64)[:, :2] for ln in lines]
    off = np.r_[0, np.cumsum([len(p) for p in pts])].astype(np.int64)
    gl = [np.asarray(g, np.float64)[:, :2] for g in ground_lines if len(g) >= 2]
    gends = np.array([p for g in gl for p in (g[0], g[-1])], np.float64).reshape(-1, 2)
    got = np.asarray(fast._ef.deck_end_joins(
        np.ascontiguousarray(np.concatenate(pts)), off, np.asarray(halves, np.float64), np.ascontiguousarray(gends),
        np.array([0.5, END_JOIN_M, END_JOIN_COS, END_JOIN_GROUND_M, END_JOIN_MIN_M], np.float64)), np.float64)
    return [(e // 2, 0 if e % 2 == 0 else -1, (float(got[e, 0]), float(got[e, 1])))
            for e in range(len(got)) if np.isfinite(got[e, 0])]


# ... and a free end standing on another deck part's carriageway of its level (within its half width and END_ON_MARGIN_M;
# no road going on on the ground there): a ramp's end on the cross street's bridge. Held at the ground, the ramp's
# short deck rose 8 m in 6 m to its measured joint, a wall (the Springfield Interchange, 2026-10-29). Solved again with
# that end at the other deck's height where they differ by more than END_ON_STEP_M (earthfast.deck_end_on).
END_ON_MARGIN_M = 0.5
END_ON_STEP_M = 0.3


def _end_ons(lines, halves, levels, ground_lines):
    """[(part, 0 | -1, the part it stands on, arc m along that one)]: the free ends of deck lines standing on another
    deck of their level (END_ON_*; earthfast.deck_end_on). Pure."""
    if not END_JOIN_ON or len(lines) < 2:
        return []
    import fast
    fast.require(107, "deck_end_on")
    pts = [np.asarray(ln, np.float64)[:, :2] for ln in lines]
    off = np.r_[0, np.cumsum([len(q) for q in pts])].astype(np.int64)
    gl = [np.asarray(g, np.float64)[:, :2] for g in ground_lines if len(g) >= 2]
    gends = np.array([q for g in gl for q in (g[0], g[-1])], np.float64).reshape(-1, 2)
    got = np.asarray(fast._ef.deck_end_on(
        np.ascontiguousarray(np.concatenate(pts)), off, np.asarray(halves, np.float64),
        np.asarray([float(v) for v in levels], np.float64), np.ascontiguousarray(gends),
        np.array([0.5, END_JOIN_GROUND_M, END_ON_MARGIN_M], np.float64)), np.float64)
    return [(e // 2, 0 if e % 2 == 0 else -1, int(got[e, 0]), float(got[e, 1]))
            for e in range(len(got)) if got[e, 0] >= 0]


def _joined_lines(lines, joins):
    """lines with each end of joins moved onto its joint (copies; the others as they are)."""
    out = list(lines)
    for k, at, p in joins:
        ln = np.array(out[k], np.float64, copy=True)
        ln[at, 0], ln[at, 1] = p
        out[k] = ln
    return out


def _ground_pins(lines, ground_lines):
    """The joints of decks (lines' ends shared by two or more) a ground road of PIN_ROAD_M or more goes on from (an
    end of it within PIN_M): _pin's points. Pure."""
    ends = np.array([p[:2] for ln in lines for p in (ln[0], ln[-1])], np.float64).reshape(-1, 2)
    if not len(ends) or not ground_lines:
        return []
    shared = [p for k, p in enumerate(ends) if (np.hypot(*(ends - p).T) < 0.5).sum() > 1]
    if not shared:
        return []
    out = []
    for gl in ground_lines:
        gl = np.asarray(gl, np.float64)[:, :2]
        if len(gl) < 2 or float(np.hypot(*np.diff(gl, axis=0).T).sum()) < PIN_ROAD_M:
            continue
        for e in (gl[0], gl[-1]):
            for p in shared:
                if np.hypot(*(e - p)) < PIN_M:
                    out.append(p)
    return out


def _merge_overlaps(lines, zs, widths, levels, rounds=3, pins=None):
    """Heights of decks lying over each other (a ramp leaving the mainline, parallel carriageways of
    one level) pulled to their mean where their surfaces overlap, so a split is one surface until the
    decks part (2026-10-11 the user: exits and splits not clean). Parts are related when they share an
    end node or a level and run within ~30 degrees of each other. Abutment ends stay (pins: deck_profile's). Pure."""
    nodes, edges, adj = _chain_graph(lines)
    deg = np.zeros(len(nodes), int)
    for a, b, _ in edges:
        deg[a] += 1
        deg[b] += 1
    deg = _pin(nodes, deg, pins)
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
    floors = [np.array(z, np.float64) for z in zs]
    zs = [z.copy() for z in floors]
    # never raised faster than DECK_MAX_GRADE from any abutment of the chain, along it (a lift at a joint reached the
    # parts beyond it: Spaghetti Junction's ramps, lifted to a higher deck of their level beside them, stood 21 m up at
    # the joint of a 48 m part ending on the ground, 40 %)
    reach = [np.full(len(z), np.inf) for z in zs]
    for k0, (a0, b0, _s) in enumerate(edges[:n]):
        for node, zend in ((a0, floors[k0][0]), (b0, floors[k0][-1])):
            if deg[node] != 1 or not np.isfinite(zend):
                continue
            d = _dijkstra(adj, {node: 0.0})
            for k, (ea, eb, es) in enumerate(edges[:n]):
                reach[k] = np.minimum(reach[k], zend + DECK_MAX_GRADE * np.minimum(d[ea] + es, d[eb] + (es[-1] - es)))
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
            a, b, es = edges[i]
            # a lift fades along the deck no faster than DECK_MAX_GRADE: it ended where the parts drew apart, over a
            # few metres of their spacing (a High Five ramp lifted 11 m to its twin fell back over ~60 m, 35 %)
            lifted = new[i] > zs[i] + 1e-6
            if lifted.any():
                new[i] = np.maximum(new[i], _ground_envelope(es, np.where(lifted, new[i], -np.inf), DECK_MAX_GRADE))
            # never raised faster than DECK_MAX_GRADE from an abutment (a road's bridge forking off uphill near one,
            # taken for a carriageway beside it, pulled US-24's deck 1.1 m up in the first 3 m off its abutment near
            # Buena Vista CO, 2026-10-27)
            # (fading out toward a joint with other parts, where the parts must meet: capped right up to it, an 8 m
            # ramp part of a Calgary diverging diamond stood 0.7 m under the joint it leaves, #369)
            rise = np.full(len(es), np.inf)
            to_joint = np.full(len(es), np.inf)
            for end, d, at in ((a, es, 0), (b, es[-1] - es, -1)):
                if deg[end] == 1:
                    rise = np.minimum(rise, floors[i][at] + DECK_MAX_GRADE * d)
                else:
                    to_joint = np.minimum(to_joint, d)
            capped = np.minimum(new[i], np.maximum(floors[i], rise))     # (from the unmerged heights: no creep per round)
            if np.isfinite(to_joint).any() and np.isfinite(rise).any():
                w = np.clip(to_joint / max(float(es[-1]), 1e-6), 0.0, 1.0)
                capped = w * capped + (1.0 - w) * new[i]
            new[i] = np.minimum(capped, np.maximum(reach[i], floors[i]))
            if deg[a] == 1:
                new[i][0] = zs[i][0]
            if deg[b] == 1:
                new[i][-1] = zs[i][-1]
        zs = new
    out = []
    for i, z in enumerate(zs):                  # re-smoothed, ends kept
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(lines[i][:, :2], axis=0).T))]
        sm = _smooth_pinned(z, s[-1] / max(len(s) - 1, 1), DECK_SIGMA) if len(z) > 3 else z
        out.append(np.maximum(sm, floors[i]))
    return out


DUAL_DECK_M = 30.0      # m: parts of one level parallel within this are one deck's carriageways (_merge_overlaps)


def deck_profile(lines, ground_fn, crossings=(), clear=None, grade=None, lift=None, levels=None, widths=None, pins=None):
    """deck_profile_once, then repeated with the deck-over-deck requirements: where two parts of
    different levels cross, the higher one clears the lower one's deck by `clear` (2026-10-11: crossing
    decks clipped into each other). pins: points where a ground road goes on from a joint of decks (abutments too: _pin).
    Pure given ground_fn."""
    zs = deck_profile_once(lines, ground_fn, crossings, clear, grade, lift, pins=pins)
    n_road = len(crossings)
    if levels is None or len(lines) < 2:
        return zs
    over = _deck_crossings(lines)
    stacks = _deck_stacks(lines, levels, widths) if widths is not None else []
    if not over and not stacks:
        zs = _merge_overlaps(lines, zs, widths, levels, pins=pins) if widths is not None else zs
        return _ease(lines, zs, ground_fn, lift) if bridge_profile_mode() == "curve" else zs
    arcs = [np.r_[0.0, np.cumsum(np.hypot(*np.diff(ln[:, :2], axis=0).T))] for ln in lines]
    # curve mode: one round. Its grades carry a requirement along the line to the parts joined to it whatever their
    # level, so a ramp under the upper deck rose with it and lifted it again each round (the Morrison Bridge 21 ->
    # 41 m over the Willamette, 2026-10-19)
    for _ in range(1 if bridge_profile_mode() == "curve" else 4):
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
                                levels=levels, pins=pins)
        if all(np.allclose(a, b, atol=0.02, equal_nan=True) for a, b in zip(new, zs)):
            zs = new
            break
        zs = new
    zs = _merge_overlaps(lines, zs, widths, levels, pins=pins) if widths is not None else zs
    return _ease(lines, zs, ground_fn, lift) if bridge_profile_mode() == "curve" else zs


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
            # over it means over at least half the narrower deck: two carriageways side by side that the map gives
            # different layers touch edge to edge (the Vincent Thomas Bridge's, 8.5 m apart, 11.8 m wide: the upper
            # was lifted 10 m over its twin, 2026-10-27)
            reach -= 0.5 * min(float(widths[i]), float(widths[j]))
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
    over the other: solved together): 2 or more of part i's points every `step` m along it within half their widths
    + MERGE_TOUCH_M of part j's line. earthfast.deck_beside (Rust) on the samples. Pure."""
    import fast
    fast.require(89, "deck_beside")
    pts = [np.asarray(ln, np.float64)[:, :2] for ln in lines]
    qs = []
    for p in pts:
        if len(p) < 2:
            qs.append(np.zeros((0, 2)))
            continue
        si = np.r_[0.0, np.cumsum(np.hypot(*np.diff(p, axis=0).T))]
        t = np.arange(0.0, si[-1] + 1e-9, step)
        qs.append(np.c_[np.interp(t, si, p[:, 0]), np.interp(t, si, p[:, 1])])
    off = lambda arrs: np.r_[0, np.cumsum([len(a) for a in arrs])].astype(np.int64)      # noqa: E731
    pairs = fast._ef.deck_beside(np.ascontiguousarray(np.concatenate(qs) if qs else np.zeros((0, 2))), off(qs),
                                 np.ascontiguousarray(np.concatenate(pts) if pts else np.zeros((0, 2))), off(pts),
                                 np.asarray(widths, np.float64), float(MERGE_TOUCH_M))
    return [(int(a), int(b)) for a, b in pairs]


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
GAP_DIP_M = 2.0             # BridgeProfiles._gap: the chain's lowest ground this far under its highest end, ...
GAP_LOW_M = 1.5             # ... the stretch within this of it is the water
GAP_SEA_M = 1.0             # ... and at most this high (a river at sea level)


def _curve_grade(lines, zs):
    """The steepest grade (rise / run) between consecutive points of any part of a deck profile. Pure."""
    g = 0.0
    for ln, z in zip(lines, zs):
        if len(ln) < 2:
            continue
        run = np.maximum(np.hypot(*np.diff(np.asarray(ln, np.float64)[:, :2], axis=0).T), 0.5)
        g = max(g, float(np.max(np.abs(np.diff(np.asarray(z, np.float64))) / run)))
    return g


def _end_step(lines, zs):
    """The steepest grade of a deck's first or last segment at a free end (an abutment: no other part ends within
    0.75 m), where a curve that cannot meet its end leaves a step. Pure."""
    ends = [np.asarray(ln, np.float64)[k, :2] for ln in lines for k in (0, -1)]
    g = 0.0
    for ln, z in zip(lines, zs):
        ln = np.asarray(ln, np.float64)
        if len(ln) < 2:
            continue
        for a, b in ((0, 1), (-1, -2)):
            if sum(np.hypot(*(e - ln[a, :2])) < 0.75 for e in ends) > 1:
                continue
            run = max(float(np.hypot(*(ln[b, :2] - ln[a, :2]))), 0.5)
            g = max(g, abs(float(z[b]) - float(z[a])) / run)
    return g


def _curve_crossings(crossings, n_road=None, levels=None):
    """deck_profile_once's crossings for deck_curve: (part, arc, height under it, its clearance or None). A
    deck-over-deck requirement holds on the upper part itself (deck_curve applies a requirement where it is, not
    falling along the chain, so it never reaches the lower deck). Pure."""
    return [(cr[0], cr[1], cr[2], cr[3] if len(cr) > 3 else None) for cr in crossings]


def deck_profile_once(lines, ground_fn, crossings=(), clear=None, grade=None, lift=None, n_road=None, levels=None,
                      pins=None, curve=True):
    """Deck heights (one array per part) of a bridge chain: deck_heights between the abutments, at
    least `lift` over the ground under it, and at least `clear` over each crossing road below
    ((part index, arc m, road height)), the requirement falling at `grade` along the chain; then
    smoothed per part with its end heights kept. A road crossing (the first n_road crossings, default all, without
    their own clearance) by an abutment the deck cannot get AT_GRADE_M over is a junction at grade: the deck
    meets the road's height there. Pure given ground_fn(xy (m, 2)) -> heights."""
    clear = DECK_ROAD_CLEAR if clear is None else clear
    grade = DECK_GRADE if grade is None else grade
    lift = DECK_CLEAR if lift is None else lift
    # (curve mode for a deck that clears something; one over nothing but the ground keeps the ground envelope's rounded
    # crest: Lombard Street, Baltimore's deck over its embankment, test_no_step_onto_a_steep_abutment)
    if curve and lines and bridge_profile_mode() == "curve" and len(crossings):
        cl, cg = _unpinned(lines, ground_fn, pins)
        got = deck_curve(cl, cg, _curve_crossings(crossings, n_road, levels), (), clear, lift)
        if got is not None:
            g = _curve_grade(lines, got)
            if g <= DECK_STEEP_GRADE or _end_step(lines, got) <= DECK_STEEP_GRADE:
                return got
            # (a curve steeper than that may not meet its ends: a "bridge" mapped along a hillside stepped 40 m at
            # its abutment, GitHub #382 near Tata. The ground envelope's profile when it is the gentler one)
            old = deck_profile_once(lines, ground_fn, crossings, clear, grade, lift, n_road, levels, pins, False)
            return got if not old or _end_step(lines, got) <= _end_step(lines, old) + 0.05 else old
    zs = deck_heights(lines, ground_fn, pins)
    if not zs:
        return zs
    nodes, edges, adj = _chain_graph(lines)
    allxy = np.concatenate([ln[:, :2] for ln in lines])
    gz = np.asarray(ground_fn(allxy), np.float64)
    deg = np.zeros(len(nodes), int)
    for a, b, _ in edges:
        deg[a] += 1
        deg[b] += 1
    deg = _pin(nodes, deg, pins)
    # no steeper than DECK_MAX_GRADE from an abutment: a short bridge over a road cannot reach full
    # headroom (2026-10-11: such decks became steep wedges, "walls" in the user's screenshot)
    cap = [np.full(len(ln), np.inf) for ln in lines]
    dab = [np.full(len(ln), np.inf) for ln in lines]      # m to the nearest abutment
    steep = [np.full(len(ln), np.inf) for ln in lines]    # DECK_STEEP_GRADE off each abutment (below)
    for k, (a, b, s) in enumerate(edges):
        for node, zend in ((a, zs[k][0]), (b, zs[k][-1])):
            if deg[node] != 1 or not np.isfinite(zend):
                continue
            d = _dijkstra(adj, {node: 0.0})
            for k2, (ea, eb, es) in enumerate(edges):
                dk = np.minimum(d[ea] + es, d[eb] + (es[-1] - es))
                cap[k2] = np.minimum(cap[k2], zend + DECK_MAX_GRADE * dk)
                dab[k2] = np.minimum(dab[k2], dk)
                steep[k2] = np.minimum(steep[k2], zend + DECK_STEEP_GRADE * dk)
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
                # A lower road has width: keep a level clearance corridor around its
                # centreline instead of reaching the target at just one point.
                reach = DECK_CLEAR_REACH_M if len(cr) == 3 or cr[3] is None else 0.0
                req[k] = np.maximum(req[k], zc + cl - grade * np.maximum(dk - reach, 0.0))
                if k == pi:
                    # A crossing can lie between mesh vertices. Both bracketing vertices
                    # need full height, otherwise linear interpolation loses headroom.
                    for point in (sc - reach, sc, sc + reach):
                        if not 0 <= point <= es[-1]:
                            continue
                        j = int(np.searchsorted(es, point, side="left"))
                        near = [j] if j < len(es) and abs(es[j] - point) < 1e-8 else [j - 1, j]
                        for at in near:
                            if 0 <= at < len(es):
                                req[k][at] = max(req[k][at], zc + cl)
    # the caps (from an abutment, from a junction at grade) never under the straight line between the abutments: two
    # abutments further apart in height than DECK_MAX_GRADE allows had the higher one pulled under its own road, a
    # step onto the deck (0.9 m up onto the I-20 bridge near Temple, GA, once its approaches were raised, 2026-10-20)
    cap = [np.where(np.isfinite(z), np.maximum(c, z), c) for c, z in zip(cap, zs)]
    out, k0 = [], 0
    for k, (z, (a, b, s)) in enumerate(zip(zs, edges)):
        # Meet the graded approach exactly. A constant skin allowance right up
        # to a pinned abutment otherwise restores a small lip after smoothing.
        blend = np.ones(len(z))
        for distance in ([s] if deg[a] == 1 else []) + ([s[-1] - s] if deg[b] == 1 else []):
            u = np.clip(distance / DECK_SIGMA, 0.0, 1.0)
            blend = np.minimum(blend, u * u * (3.0 - 2.0 * u))
        g = gz[k0:k0 + len(z)] + lift * blend
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
        # ... but never steeper than DECK_STEEP_GRADE off an abutment where the ground under it is lower (nor under
        # the straight line between the abutments): raised by the ground 90 m off at the far, 10 m higher abutment, a
        # water clearance stood the New York Thruway's deck over the Vloman Kill 5 m up in its first 3.6 m, a wall an
        # AI car hit at 65 mph (2026-10-27)
        cap[k] = np.minimum(cap[k], np.maximum(np.maximum(steep[k], g), np.where(np.isfinite(z), z, -np.inf)))
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
            sm = np.minimum(_smooth_pinned(zz, step, sigma), cap[k])   # no hump over an at-grade junction
            # Smoothing is aesthetic: it must not remove feasible structural headroom.
            # Keep the abutment/grade cap, rather than turning a short bridge into a wall.
            floor = np.maximum(np.minimum(req[k], cap[k]), g)
            zz = np.maximum(sm, floor)
            zz[0], zz[-1] = sm[0], sm[-1]
            zz = _curve_deck(s, zz, floor, cap[k])
        out.append(zz)
    return out


DECK_CREST_R = 60.0     # m: the ground under a deck is its floor rounded to vertical curves this wide (deck_profile_once)
# ---- clearance-aware deck profile ----------------------------------------------------------------------
# 2026-10-18 (a user in Portland, OR): decks came out as "two flat pieces of road joined in the middle with slight
# up, then down slopes": deck_heights' straight line between abutments, a 5 % tent where a road crosses
# under, smoothed. deck_curve plans the deck as a road designer would: grades of DECK_DESIGN_GRADE up to
# whatever must be cleared beneath (never steeper than DECK_MAX_GRADE from an abutment), joined by parabolic vertical
# curves (one moving average of a piecewise-linear profile), the whole simple chain solved as one line.
# BNE_BRIDGE_PROFILE=legacy (earthd --bridge-profile legacy) brings the old profile back for comparison.
DECK_CURVE_K = 1500.0   # m of vertical curve per unit of grade change (15 m per %)
DECK_CURVE_MIN = 16.0   # m, shortest vertical curve
DECK_CURVE_KMIN = 260.0 # m per unit grade change at least: under 2 % of grade change between 4 m deck points
DECK_ROAD_HALF = 8.0    # m either side of a crossing road's centreline the clearance is held
DECK_CURVE_STEP = 1.0   # m, the profile's working grid
DECK_DESIGN_GRADE = 0.05  # a deck climbs to (and leaves) what it must clear at this grade (DECK_MAX_GRADE at most)
APPROACH_GRADE = 0.08  # m/m: the approach road's grade read from the ground 12 m past an abutment, at most


def bridge_profile_mode():
    """'curve' (default) or 'legacy', from BNE_BRIDGE_PROFILE (set by earthd --bridge-profile)."""
    return "legacy" if os.environ.get("BNE_BRIDGE_PROFILE", "curve").lower() == "legacy" else "curve"


def _upper_hull(s, z):
    """Indices of the least concave majorant's vertices of points (s ascending, z). Pure."""
    h = []
    for i in range(len(s)):
        while len(h) >= 2:
            a, b = h[-2], h[-1]
            if (s[b] - s[a]) * (z[i] - z[a]) - (z[b] - z[a]) * (s[i] - s[a]) >= 0:
                h.pop()
            else:
                break
        h.append(i)
    return h


def _simple_path(lines):
    """[(edge index, reversed)] in order along the chain if the parts form one open path, else None."""
    nodes, edges, adj = _chain_graph(lines)
    deg = np.zeros(len(nodes), int)
    for a, b, _ in edges:
        deg[a] += 1
        deg[b] += 1
    ends = np.flatnonzero(deg == 1)
    if len(ends) != 2 or deg.max() > 2 or len(edges) != len(nodes) - 1:
        return None
    at, used, order = int(ends[0]), set(), []
    while len(order) < len(edges):
        for k, (a, b, _) in enumerate(edges):
            if k in used or at not in (a, b):
                continue
            used.add(k)
            order.append((k, at == b and a != b))
            at = a if at == b else b
            break
        else:
            return None
    return order


CHORD_KINK = 0.02       # curve_profile keeps the straight line between abutments meeting the approaches within this grade
CHORD_SHELF_M = 1.0     # m
CHORD_TOL = 0.35        # m a requirement may go short: 0.21 m of it margins, 6 m headroom to 5.86 m at worst


def _chord(s, g, need, z0, zL, g0, gL, max_grade, cap=None):
    """curve_profile's straight line or single parabola between the abutments (see there), or None. Pure."""
    L = float(s[-1])
    if L <= 0:
        return None
    t = s / L
    line = z0 + (zL - z0) * t
    gl = (zL - z0) / L
    if abs(gl) > max_grade:
        return None
    p = 4.0 * t * (1.0 - t)
    # (where the ground stands within CHORD_SHELF_M of it, an abutment's shelf, nothing under it is to be cleared: the
    # crossing's +-DECK_CLEAR_REACH_M corridor reached over the abutments of a 24 m overpass)
    on = g >= line - CHORD_SHELF_M
    shelf = (np.cumsum(~on) == 0) | (np.cumsum((~on)[::-1])[::-1] == 0)     # (from each end, while on it)
    if shelf.all():
        shelf[:] = False
    short = np.maximum(np.where(shelf, -np.inf, need - CHORD_TOL - line), g - line)
    h = 0.0
    inner = p > 1e-6
    if (short[~inner] > 1e-6).any():
        return None
    if (short[inner] > 0).any():
        h = float(np.max(short[inner] / p[inner]))
    # its grade out of each abutment, against the approach's there
    if abs(gl + 4.0 * h / L - g0) > CHORD_KINK or abs(gl - 4.0 * h / L - gL) > CHORD_KINK             or 4.0 * h / L > CHORD_KINK or abs(gl) + 4.0 * h / L > max_grade:
        return None
    z = line + h * p
    if cap is not None and not bool(np.all(z <= np.asarray(cap) + 1e-6)):
        return None
    return z


def curve_profile(s, g, req, z0, zL, g0=0.0, gL=0.0, max_grade=None, lift=None, iters=20, cap=None):
    """One deck line's heights on a uniform grid s (0..L): from z0 to zL (the abutments), meeting the
    approach grades g0 (dz/ds just before s=0) and gL (just after L); at least req (the clearances) where
    the grade cap lets it, reached and left at DECK_DESIGN_GRADE (no steeper than max_grade from either end), joined by parabolic
    vertical curves; never below g + lift nor the straight line between the abutments; under cap (m, per point: a
    junction at grade's) where given. Pure."""
    max_grade = DECK_MAX_GRADE if max_grade is None else max_grade
    lift = DECK_CLEAR if lift is None else lift
    s = np.asarray(s, np.float64)
    L = s[-1]
    floor = np.asarray(g, np.float64) + lift
    req = np.maximum(np.asarray(req, np.float64), floor)
    cap = np.minimum(np.minimum(z0 + max_grade * s, zL + max_grade * (L - s)),
                     np.inf if cap is None else cap)        # (and under a junction at grade's own cap)
    line = z0 + (zL - z0) * s / max(L, 1e-9)    # never under the straight line between the abutments
    # the straight line between the abutments, or one parabola over it rising at most CHORD_KINK more steeply out of
    # each abutment, when that clears everything (the requirements within CHORD_TOL: they carry 0.21 m of margin;
    # the ground itself, which it may rest on) and meets both approaches within CHORD_KINK. The curve's floor stood
    # DECK_CLEAR over the approach ground beside each abutment and its smoothing crowned a 24 m overpass by 0.2-0.3 m
    # (I-275 by the Sunshine Skyway, 2026-10-27: "it just has a HUMP in the middle that's not necessary")
    chord = _chord(s, np.asarray(g, np.float64), np.asarray(req, np.float64) - lift, z0, zL, g0, gL, max_grade, cap)
    if chord is not None:
        return chord
    want = np.minimum(np.maximum(req, line), cap)
    want[0], want[-1] = z0, zL
    step = L / max(len(s) - 1, 1)
    # every curve length from long to the shortest allowed: of those holding the clearance (within 5 cm)
    # the one that rises least above what is needed (a long curve over a short clearance stretch had to
    # be lifted 14 m over the Fremont Bridge's 58 m), else the one falling least short
    best = None
    for kf in (1.0, 0.5, 0.25, 0.12, 0.067):
        z = _curve_try(s, want, cap, z0, zL, g0, gL, step, L, DECK_CURVE_K * kf, iters)
        short = float(np.max(want - z))
        over = float(np.max(z) - np.max(want))
        key = (0, over) if short <= 0.05 else (1, short)
        if best is None or key < best[0]:
            best = (key, z)
    z = best[1].copy()
    return _lift_smooth(s, z, floor)                # the abutments stay on the approach road


def _tents(s, z, grade):
    """max over points i of z_i - grade * |s - s_i|: every requirement reached at `grade` and left at it
    (the deck rides low where nothing is under it, then climbs to what it must clear). Pure."""
    ds = np.diff(s)
    f = np.array(z, np.float64)
    for i in range(1, len(f)):
        f[i] = max(f[i], f[i - 1] - grade * ds[i - 1])
    b = np.array(z, np.float64)
    for i in range(len(b) - 2, -1, -1):
        b[i] = max(b[i], b[i + 1] - grade * ds[i])
    return np.maximum(f, b)


def _curve_try(s, want, cap, z0, zL, g0, gL, step, L, K, iters):
    raise_ = np.zeros(len(s))
    z = want
    for _ in range(iters):
        pts = want + raise_
        pts[0], pts[-1] = z0, zL
        hull = np.minimum(_tents(s, pts, DECK_DESIGN_GRADE), cap)
        gr = np.diff(hull) / np.maximum(np.diff(s), 1e-9)
        # the whole crest's change (close hull corners act as one curve), or an end's
        A = max(float(np.max(gr) - np.min(gr)), abs(gr[0] - g0), abs(gr[-1] - gL))
        W = float(np.clip(K * A, max(DECK_CURVE_MIN, DECK_CURVE_KMIN * A), max(DECK_CURVE_MIN, 0.8 * L)))
        n = max(int(round(W / step)) | 1, 1)
        r = n // 2
        ext = np.r_[z0 - g0 * step * np.arange(r, 0, -1), hull, zL + gL * step * np.arange(1, r + 1)]
        sm = np.convolve(ext, np.ones(n) / n, mode="valid") if r else hull.copy()
        sm = sm + (z0 - sm[0]) + ((zL - sm[-1]) - (z0 - sm[0])) * (s / max(L, 1e-9))
        z = sm
        short = want - z
        short[0] = short[-1] = 0.0
        if short.max() <= 0.02:
            break
        # (not where the grade cap holds want down: no raise reaches it there, and raising it again and again
        # tented a 5 m hump past the cap onto the Vincent Thomas Bridge's span, 2026-10-28)
        raise_ += np.maximum(short, 0.0)
    return z



def _path_curve(lines, order, ground_fn, crossings, spans, clear, lift, max_grade, ends=(None, None)):
    """curve_profile along one path of parts (order: [(part, reversed)]). ends: per end None (an abutment:
    the ground's height, the approach grade from the ground beyond it) or (z, grade dz/ds arriving) (a
    junction with a part already solved). Returns {part: heights}, the path's polyline (m, 2), its grid s,
    heights z. None if degenerate."""
    pts, off, info = [], 0.0, {}
    for k, rev in order:
        ln = np.asarray(lines[k], np.float64)[:, :2]
        a = np.r_[0.0, np.cumsum(np.hypot(*np.diff(ln, axis=0).T))]
        info[k] = (off, rev, a)
        seq = ln[::-1] if rev else ln
        pts.append(seq if not pts else seq[1:])
        off += a[-1]
    P = np.concatenate(pts)
    S = np.r_[0.0, np.cumsum(np.hypot(*np.diff(P, axis=0).T))]
    L = S[-1]
    if L < 1.0 or len(P) < 2:
        return None
    n = max(int(math.ceil(L / DECK_CURVE_STEP)), 2)
    s = np.linspace(0.0, L, n + 1)
    xy = np.c_[np.interp(s, S, P[:, 0]), np.interp(s, S, P[:, 1])]

    def to_s(k, arc):
        o, rev, a = info[k]
        return o + (a[-1] - arc if rev else arc)
    t0 = (P[0] - P[1]) / max(np.hypot(*(P[0] - P[1])), 1e-9)
    t1 = (P[-1] - P[-2]) / max(np.hypot(*(P[-1] - P[-2])), 1e-9)
    out_pts = np.array([P[0] + t0 * 12.0, P[-1] + t1 * 12.0])
    gz = np.asarray(ground_fn(np.r_[xy, out_pts]), np.float64)
    g, go = gz[:len(s)], gz[len(s):]
    if ends[0] is None:
        z0, g0 = float(g[0]), float(np.clip((g[0] - go[0]) / 12.0, -APPROACH_GRADE, APPROACH_GRADE))
    else:
        z0, g0 = ends[0]
    if ends[1] is None:
        zL, gL = float(g[-1]), float(np.clip((go[1] - g[-1]) / 12.0, -APPROACH_GRADE, APPROACH_GRADE))
    else:
        zL, gL = ends[1]
    req = g + lift
    junction = np.full(len(s), np.inf)
    for cr in crossings:
        k, arc, zc = cr[:3]
        own = len(cr) > 3 and cr[3] is not None
        cl = cr[3] if own else clear
        if k in info:
            sc = to_s(k, arc)
            # a road by an abutment the deck cannot get AT_GRADE_M over is a junction at grade (the intersection a
            # ramp climbs from: deck_profile_once's rule): no requirement, the deck leaves the road's height
            d_end = min(sc if ends[0] is None else np.inf, L - sc if ends[1] is None else np.inf)
            if not own and d_end <= AT_GRADE_REACH_M and                     min(z0, zL) + DECK_MAX_GRADE * d_end < zc + AT_GRADE_M:
                junction = np.minimum(junction, zc + DECK_MAX_GRADE * np.abs(s - sc))
                continue
            # (upstream's full-headroom corridor, DECK_CLEAR_REACH_M either side, and the curve's 5 cm acceptance
            # taken up front: the corridor came out 4 cm short, test_bridge_clearance)
            m = np.abs(s - sc) <= max(DECK_ROAD_HALF, DECK_CLEAR_REACH_M if not own else 0.0)
            req[m] = np.maximum(req[m], zc + cl + (0.06 if not own else 0.0))
    for k, a0, a1, zmin in spans:
        if k in info:
            x0, x1 = sorted((to_s(k, a0), to_s(k, a1)))
            m = (s >= x0) & (s <= x1)
            req[m] = np.maximum(req[m], zmin)
    z = curve_profile(s, g, req, z0, zL, g0, gL, max_grade, lift, cap=junction)
    out = {}
    for k, rev in order:
        o, r, a = info[k]
        out[k] = np.interp(o + (a[-1] - a if r else a), s, z)
    return out, xy, s, z


def deck_curve(lines, ground_fn, crossings=(), spans=(), clear=None, lift=None, max_grade=None):
    """Deck heights (one array per part) of a bridge chain by curve_profile. A chain of parts in one line is
    solved as one profile; a branched chain (ramps leaving a viaduct) as its longest abutment-to-abutment
    line first, then each remaining branch from the solved deck it leaves (its height and grade there) to
    its own abutment or the deck it rejoins. None when the chain has no abutment (a closed loop). crossings:
    (part, arc m, height under it[, its own clearance]) roads and water under it (held `clear`, or their own
    clearance, over +-DECK_ROAD_HALF); spans: (part, arc from, arc to, min deck height). Pure given ground_fn."""
    clear = DECK_ROAD_CLEAR if clear is None else clear
    lift = DECK_CLEAR if lift is None else lift
    if not lines:
        return None
    args = (ground_fn, crossings, spans, clear, lift, max_grade)
    order = _simple_path(lines)
    if order is not None:
        got = _path_curve(lines, order, *args)
        return None if got is None else [got[0][k] for k in range(len(lines))]
    nodes, edges, adj = _chain_graph(lines)
    # parts not joined at all (a chain also holds the decks crossing it, solved together for the deck-over-deck
    # rule): each joined group on its own (2026-10-19: one crossing deck sent a whole interchange to the legacy
    # profile)
    comp = list(range(len(nodes)))

    def find(a):
        while comp[a] != a:
            comp[a] = comp[comp[a]]
            a = comp[a]
        return a
    for a, b, _ in edges:
        comp[find(a)] = find(b)
    groups = {}
    for k, (a, _, _) in enumerate(edges):
        groups.setdefault(find(a), []).append(k)
    if len(groups) > 1:
        out = [None] * len(lines)
        for ks in groups.values():
            idx = {k: i for i, k in enumerate(ks)}
            sub = deck_curve([lines[k] for k in ks], ground_fn,
                             [(idx[cr[0]],) + tuple(cr[1:]) for cr in crossings if cr[0] in idx],
                             [(idx[k], a0, a1, z) for k, a0, a1, z in spans if k in idx], clear, lift, max_grade)
            if sub is None:
                sub = deck_profile_once([lines[k] for k in ks], ground_fn,
                                        [(idx[cr[0]],) + tuple(cr[1:]) for cr in crossings if cr[0] in idx], clear, None, lift)
                if bridge_profile_mode() == "curve" and sub is not None and any(
                        s is None for s in sub):
                    return None
            for k, z in zip(ks, sub):
                out[k] = z
        return out
    deg = np.zeros(len(nodes), int)
    inc = [[] for _ in nodes]
    for k, (a, b, _) in enumerate(edges):
        deg[a] += 1
        deg[b] += 1
        inc[a].append(k)
        inc[b].append(k)
    abut = [i for i in range(len(nodes)) if deg[i] == 1]
    if not abut:
        return None
    out = [None] * len(lines)
    solved = {}                                 # node -> (z, [(unit tangent arriving, grade along it)])

    def walk(start, done_edges, stop):
        """Longest path from start through edges not in done_edges, ending at an abutment or a solved node
        (stop): [(edge, reversed)] (Dijkstra on lengths, the farthest such end)."""
        import heapq
        dist, prev = {start: 0.0}, {}
        q = [(0.0, start)]
        while q:
            d, u = heapq.heappop(q)
            if d > dist.get(u, np.inf):
                continue
            if u != start and stop(u):
                continue
            for k in inc[u]:
                if k in done_edges:
                    continue
                a, b, s = edges[k]
                v = b if u == a else a
                if d + s[-1] < dist.get(v, np.inf):
                    dist[v] = d + s[-1]
                    prev[v] = (u, k)
                    heapq.heappush(q, (d + s[-1], v))
        ends = [v for v in dist if v != start and stop(v)]
        if not ends:
            return None
        v = max(ends, key=lambda x: dist[x])
        path = []
        while v != start:
            u, k = prev[v]
            path.append((k, edges[k][0] != u))
            v = u
        return path[::-1]

    def record(order, s, z, xy):
        """The solved path's nodes: their heights and the tangents / grades arriving there both ways."""
        node_s = 0.0
        seq = []
        for k, rev in order:
            a, b, es = edges[k]
            seq.append((a if not rev else b, node_s))
            node_s += es[-1]
        last_k, last_rev = order[-1]
        seq.append((edges[last_k][1] if not last_rev else edges[last_k][0], node_s))
        gr = np.gradient(z, s)
        tg = np.gradient(xy, s, axis=0)
        for nd, sn in seq:
            j = int(np.clip(np.searchsorted(s, sn), 0, len(s) - 1))
            t = tg[j] / max(np.hypot(*tg[j]), 1e-9)
            zz, lst = solved.get(nd, (float(np.interp(sn, s, z)), []))
            lst += [(t, float(gr[j])), (-t, -float(gr[j]))]
            solved[nd] = (zz, lst)

    def end_of(node, direction):
        """(z, grade arriving) for a path leaving / reaching a solved node in this unit direction."""
        z, lst = solved[node]
        t, gr = max(lst, key=lambda tg: float(np.dot(tg[0], direction)))
        return (z, gr)

    # the trunk: of the abutment-to-abutment paths, the one carrying the highest requirement (the span over the
    # river: solved first, so a branch never pins it low), then the longest. (2026-10-19: the Crescent City
    # Connection's river decks were branches of a longer, low line through the approaches and stayed 17 m up)
    need = {}
    for cr in crossings:
        k, zc = cr[0], cr[2]
        cl = cr[3] if len(cr) > 3 and cr[3] is not None else clear
        need[k] = max(need.get(k, -np.inf), zc + cl)
    for k, _, _, zmin in spans:
        need[k] = max(need.get(k, -np.inf), zmin)
    best = None
    for a0 in abut:
        for a1 in abut:
            if a1 <= a0:
                continue
            p = walk(a0, set(), lambda v, a1=a1: v == a1)
            if p is None:
                continue
            L = sum(edges[k][2][-1] for k, _ in p)
            top = max((need.get(k, -np.inf) for k, _ in p), default=-np.inf)
            key = (top if np.isfinite(top) else -1e9, L)
            if best is None or key > best[0]:
                best = (key, p)
    if best is None:
        return None
    done = set()
    todo = [best[1]]
    first = True
    while todo:
        order = todo.pop()
        k0, r0 = order[0]
        start = edges[k0][1] if r0 else edges[k0][0]
        kl, rl = order[-1]
        end = edges[kl][0] if rl else edges[kl][1]
        e0 = e1 = None
        if not first:
            ln0 = np.asarray(lines[k0], np.float64)[:, :2]
            ln0 = ln0[::-1] if r0 else ln0
            d0 = (ln0[1] - ln0[0]) / max(np.hypot(*(ln0[1] - ln0[0])), 1e-9)
            e0 = end_of(start, d0)
            if end in solved:
                lnl = np.asarray(lines[kl], np.float64)[:, :2]
                lnl = lnl[::-1] if rl else lnl
                d1 = (lnl[-1] - lnl[-2]) / max(np.hypot(*(lnl[-1] - lnl[-2])), 1e-9)
                z1, g1 = end_of(end, d1)
                e1 = (z1, g1)
        first = False
        got = _path_curve(lines, order, *args, ends=(e0, e1))
        for k, _ in order:
            done.add(k)
        if got is None:
            continue
        res, xy, s, z = got
        for k, v in res.items():
            out[k] = v
        record(order, s, z, xy)
        if not todo:                            # the next branch: from any solved node with an open edge
            for nd in list(solved):
                if any(k not in done for k in inc[nd]):
                    p = walk(nd, done, lambda v: deg[v] == 1 or v in solved)
                    if p:
                        todo.append(p)
                        break
    if any(o is None for o in out):             # parts not reached (should not happen): the legacy answer
        return None
    return out


DECK_EASE_M = 24.0      # m, the moving average run over each part after the merges (deck_profile, curve mode)


def _ease(lines, zs, ground_fn, lift=None):
    """Each part's heights after _merge_overlaps / the deck-over-deck rounds, run through a DECK_EASE_M moving
    average on a 1 m grid with its end heights kept (a linear correction) and never under the ground + lift:
    blending a part with its neighbour where their surfaces overlap left 2.5-4.3 % grade changes between deck
    points (Steel, Broadway, 2026-10-19). Pure given ground_fn."""
    lift = DECK_CLEAR if lift is None else lift
    out = []
    for ln, z in zip(lines, zs):
        z = np.asarray(z, np.float64)
        if len(z) < 3 or not np.isfinite(z).all():
            out.append(z)
            continue
        xy = np.asarray(ln, np.float64)[:, :2]
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
        L = s[-1]
        n = int(round(DECK_EASE_M))
        if L < 2 * n:
            out.append(z)
            continue
        g = np.arange(0.0, L + 1e-9, 1.0)
        zg = np.interp(g, s, z)
        r = n // 2
        ext = np.r_[2 * zg[0] - zg[r:0:-1], zg, 2 * zg[-1] - zg[-2:-r - 2:-1]]
        sm = np.convolve(ext, np.ones(2 * r + 1) / (2 * r + 1), mode="valid")
        sm = sm + (zg[0] - sm[0]) + ((zg[-1] - sm[-1]) - (zg[0] - sm[0])) * g / L
        zz = np.interp(s, g, sm)
        gz = np.asarray(ground_fn(xy), np.float64) + lift
        zz[0], zz[-1] = z[0], z[-1]
        out.append(_lift_smooth(s, zz, gz))
    return out


LIFT_GRADE = 0.03       # _lift_smooth: a lift over the ground is run out along the deck at this grade ...
LIFT_EASE_M = 40.0      # ... and rounded over this many m
LIFT_END_GRADE = 0.01   # by an abutment the floor rises from the abutment no faster than this


def _lift_smooth(s, z, floor, iters=8):
    """z (heights at arcs s) raised to at least floor between its ends, the lift run out at LIFT_GRADE and rounded
    over LIFT_EASE_M, not stepped: clamping each point to the ground left a 0.6-0.9 m step in the last few metres
    where a deck lands on a bank the DEM shows steep (the Steel and St. Johns bridges' ends: 17-31 % between two deck
    points). The ends keep their heights; whatever the rounding leaves short is clamped last. Pure."""
    s = np.asarray(s, np.float64)
    z = np.array(z, np.float64)
    floor = np.asarray(floor, np.float64)
    if len(z) < 3 or s[-1] <= 0:
        z[1:-1] = np.maximum(z[1:-1], floor[1:-1])
        return z
    g = np.arange(0.0, s[-1] + 1e-9, 1.0)
    if g[-1] < s[-1]:
        g = np.r_[g, s[-1]]
    zg = np.interp(g, s, z)
    # within LIFT_EASE_M / 2 of an abutment the floor is no higher than the abutment allows at LIFT_END_GRADE (a bank
    # the DEM shows over the road's end: the deck lands on the road, not on the bank's top)
    d0, d1 = g, g[-1] - g
    cap = np.minimum(np.where(d0 < LIFT_EASE_M / 2, z[0] + LIFT_END_GRADE * d0, np.inf),
                     np.where(d1 < LIFT_EASE_M / 2, z[-1] + LIFT_END_GRADE * d1, np.inf))
    fl = np.interp(g, s, floor)
    fg = np.maximum(np.minimum(fl, cap), fl - DECK_CLEAR)     # (never under the ground itself)
    r = int(LIFT_EASE_M // 2)
    inner = np.ones(len(g), bool)
    inner[0] = inner[-1] = False
    if not (fg[inner] > zg[inner] + 0.01).any():
        out = np.array(z)
        out[1:-1] = np.maximum(out[1:-1], np.interp(s, g, fg)[1:-1])
        return out
    want = fg.copy()
    zz = zg
    for _ in range(iters):
        cone = _tents(g, np.where(inner, want, -np.inf), LIFT_GRADE)
        tgt = np.maximum(zg, cone)
        ext = np.r_[np.full(r, tgt[0]), tgt, np.full(r, tgt[-1])]
        zz = np.convolve(ext, np.ones(2 * r + 1) / (2 * r + 1), mode="valid")
        zz = np.maximum(zz, zg)
        zz[0], zz[-1] = zg[0], zg[-1]
        short = np.where(inner, fg - zz, 0.0)
        if short.max() <= 0.01:
            break
        want = want + np.maximum(short, 0.0)
    zg = zz
    out = np.interp(s, g, zg)
    out[0], out[-1] = z[0], z[-1]
    ff = np.interp(s, g, fg)
    out[1:-1] = np.maximum(out[1:-1], ff[1:-1])
    return out




DECK_SIGMA = 12.0       # m, deck profile smoothing (at least) ...
DECK_SIGMA_SHARE = 25.0  # ... a 25th of the part's length ...
DECK_SIGMA_MAX = 40.0   # ... at most


def _curve_deck(s, z, floor, cap):
    """The deck profile z (at arcs s) projected onto its bounds [min(floor, cap), cap] and a change of grade of at most
    1/300 per metre (a 300 m vertical radius), its ends pinned; z itself when the map's constraints cannot all hold.
    Only bridge decks use this (wyo-backroads, PR #374: I-80 over Otto Road's 6 % crests). Pure."""
    import fast
    fast.require(83, "curve_deck")      # (Dykstra's projections, up to 2000 rounds: ~170 ms a deck in Python)
    f64 = np.float64
    return fast._ef.curve_deck(np.ascontiguousarray(s, f64), np.ascontiguousarray(z, f64),
                               np.ascontiguousarray(floor, f64), np.ascontiguousarray(cap, f64))


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
    import fast
    fast.require(66, "dedupe_polyline")     # (the Rust loop: 14-21 CPU-s a Deck teleport in each road process)
    xy = np.asarray(xy, np.float64)
    return xy[fast._ef.dedupe_polyline(xy, float(tol))]


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


PIN_M = 1.0             # m: a ground road's end this near a joint of decks makes the joint an abutment (_pin) ...
PIN_ROAD_M = 20.0       # ... a road at least this long (a shorter piece between decks is a gap in the map's bridge tags)


PIN_SPLIT_M = 0.7      # m between the ends of the parts at a pinned joint as _unpinned parts them (> _EndJoin's 0.5)


def _unpinned(lines, ground_fn, pins):
    """(lines, ground_fn) for deck_curve with each joint at pins (_pin: a ground road goes on from it) made an end of
    each part there: their ends moved PIN_SPLIT_M apart (one more step east each), ground_fn giving the ground at the
    joint itself there. deck_curve's graph joined them, the path went through the joint, and the ground road went on
    from it 8.5 m under the deck (Charles Street over I-83 in Baltimore: the AI crashed at the step, 2026-10-29). The
    heights stay one per point of each line. Pure."""
    if pins is None or not len(pins) or len(lines) < 2:
        return lines, ground_fn
    nodes, edges, _adj = _chain_graph(lines)
    deg = np.zeros(len(nodes), int)
    for a, b, _ in edges:
        deg[a] += 1
        deg[b] += 1
    pinned = _pin(nodes, deg, pins)
    split = [i for i in range(len(nodes)) if deg[i] > 1 and pinned[i] == 1]
    if not split:
        return lines, ground_fn
    out = [np.array(ln, np.float64, copy=True) for ln in lines]
    moved = []                      # (moved point, the joint)
    for nd in split:
        q = np.asarray(nodes[nd], np.float64)
        n = 0
        for k, (a, b, _s) in enumerate(edges):
            for at, end in ((0, a), (-1, b)):
                if end != nd:
                    continue
                n += 1
                if n == 1:
                    continue        # (the first part keeps the joint's point)
                out[k][at, 0] = q[0] + PIN_SPLIT_M * (n - 1)
                out[k][at, 1] = q[1]
                moved.append((out[k][at, :2].copy(), q))
    if not moved:
        return lines, ground_fn
    M = np.array([m for m, _ in moved], np.float64)
    Q = np.array([q for _, q in moved], np.float64)

    def ground_at(xy):
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        hit = np.full(len(xy), -1)
        for j, m in enumerate(M):
            hit[(np.abs(xy[:, 0] - m[0]) < 1e-6) & (np.abs(xy[:, 1] - m[1]) < 1e-6)] = j
        if not (hit >= 0).any():
            return ground_fn(xy)
        q = np.where((hit >= 0)[:, None], Q[np.maximum(hit, 0)], xy)
        return ground_fn(q)
    return out, ground_at


def _pin(nodes, deg, pins):
    """deg with the joints at pins made ends (1): abutments, held at the ground. Two motorway decks met at a node the
    motorway went on from on the ground; taken for a joint of decks it stood 3.9 m over the road it ran on to, a wall
    (the Four Level Interchange, Los Angeles, 2026-10-27). Pure."""
    if pins is None or not len(pins):
        return deg
    P = np.asarray(pins, np.float64).reshape(-1, 2)
    deg = deg.copy()
    for i, nd in enumerate(nodes):
        if deg[i] > 1 and (np.hypot(P[:, 0] - nd[0], P[:, 1] - nd[1]) < PIN_M).any():
            deg[i] = 1
    return deg


def deck_heights(lines, end_z, pins=None):
    """Deck heights of chained bridge parts. lines: polylines (k, 2) in metres (one per bridge part);
    parts sharing an end point (within 0.5 m) are joined. end_z(xy (m, 2)) -> heights of the ground at
    the abutments (chain ends: points used by one part only). A joint's height is the abutments' heights weighted by
    1 / their distance along the chain, and each part runs straight between its two ends (between two abutments: a
    straight line). Weighting every point, a part from an abutment to a joint of three parts rose ~16 % off its
    abutment and flattened toward the joint, 38 % once smoothed: US-24's deck near Buena Vista CO, 1.1 m up in its
    first 3 m, 2026-10-27. Returns one height array per line. Pure given end_z."""
    if not lines:
        return []
    edges, ez, dist = _end_dists(lines, end_z, pins)
    out = []

    def weighted(dk):
        num, den = np.zeros(len(dk[0])), np.zeros(len(dk[0]))
        for k, d in enumerate(dk):
            ok = np.isfinite(d)
            w = np.where(ok, 1.0 / np.maximum(d, 1e-6), 0.0)
            num += w * np.where(ok, ez[k], 0.0)
            den += w
        return np.where(den > 0, num / np.maximum(den, 1e-12), np.nan)
    for a, b, s in edges:
        if not dist:                            # a closed loop: no abutment known
            out.append(np.full(len(s), np.nan))
            continue
        za, zb = weighted([np.array([d[a], d[b]], np.float64) for d in dist])
        if np.isfinite(za) and np.isfinite(zb) and s[-1] > 0:
            out.append(za + (zb - za) * (s / s[-1]))
        else:
            out.append(weighted([np.minimum(d[a] + s, d[b] + (s[-1] - s)) for d in dist]))
    return out


def _end_dists(lines, end_z, pins=None):
    """The chain of lines (joined at end points within 0.5 m): its edges (start node, end node, arc (k,)),
    the heights end_z gives at its ends (points of one line only) and per end the distance along the chain
    to every node. Pure given end_z."""
    join, nodes = _EndJoin(), []

    def node(p):
        k = join(p)                                                 # 0.5 m
        if k == len(nodes):
            nodes.append(np.asarray(p[:2], np.float64))
        return k
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
    deg = _pin(nodes, deg, pins)
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
TUNNEL_WET_COVER_M = 60.0   # m: water this far over the chain's lower portal is no underwater tunnel's (_mark_drowned)
TUNNEL_WET_RUN_M = 20.0 # ... and a tunnel chain passing under water this far away from its ends (_mark_drowned)
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
MAJOR_LOW_H = 5.0       # m: ... on a major road (CONCRETE_WALL_CLASSES: road tunnels clear ~4.5 m, a truck ~4 m)
# A covered section (a low arch or a tube) goes on along the tunnel until every ground road crossing over it within
# TUNNEL_ROAD_REACH_M of its ends is covered, its carriageway and TUNNEL_ROAD_CLEAR_M past it (along the tunnel: / the
# sine of the crossing angle, at least TUNNEL_ROAD_MIN_SIN). On lidar each carriageway of a divided motorway over an
# underpass is its own embankment: the arch under one was 8 m long under 13 m of lanes, its mouths and terrain holes
# in them (GitHub #450, the A-7 near Málaga).
TUNNEL_ROAD_CLEAR_M = 2.0
TUNNEL_ROAD_MIN_SIN = 0.35
TUNNEL_ROAD_REACH_M = 15.0


PORTAL_SHALLOW_COVER_M = 1.5    # m: a major road's open end run with at least this much ground over its floor is covered


def portal_cover(kinds, cover, spec):
    """kinds with a major road's (CONCRETE_WALL_CLASSES) open run at either end of its tunnel covered (a low arch, in
    concrete: roadmesh) where the median ground over its floor is PORTAL_SHALLOW_COVER_M or more: the map's portal is a
    cut-and-cover box under shallow ground there (the Gotthard road tunnel's north portal under the meadow at
    Goeschenen stood 90 m further in, an open cut before it, 2026-10-28). Park roads keep their open cuts (Central
    Park's transverses). The meshes (TunnelProfiles) and the grading both."""
    kinds = np.array(kinds, np.int64)
    if spec is None or spec.cls not in CONCRETE_WALL_CLASSES or not (kinds > 0).any():
        return kinds
    cover = np.asarray(cover, np.float64)
    n = len(kinds)
    for rng in (range(n), range(n - 1, -1, -1)):
        run = []
        for q in rng:
            if kinds[q] != 0:
                break
            run.append(q)
        if run and len(run) < n and np.isfinite(cover[run]).any() and \
                float(np.nanmedian(cover[run])) >= PORTAL_SHALLOW_COVER_M:
            kinds[run] = 1
    return kinds


def cover_roads(xy, s, kinds, lines, halves):
    """kinds (tunnel_sections') with each covered section carried on until the ground roads crossing over it (lines
    (k, 2) in xy's frame, carriageway half widths) are covered, TUNNEL_ROAD_*: earthfast.tunnel_cover_roads. The
    road meshes (TunnelProfiles) and the grading (grading._tunnel_sections) both: an open section the grading did
    not know of cut its trench through the A-7's lanes near Malaga (GitHub #450)."""
    kinds = np.asarray(kinds, np.int64)
    if not lines or not (kinds > 0).any() or len(xy) < 2:
        return kinds
    import fast
    fast.require(95, "tunnel_cover_roads")
    pts = np.ascontiguousarray(np.concatenate(lines))
    starts = np.concatenate([[0], np.cumsum([len(ln) for ln in lines])]).astype(np.int64)
    return np.asarray(fast._ef.tunnel_cover_roads(
        np.ascontiguousarray(np.asarray(xy, np.float64)[:, :2]), np.ascontiguousarray(s, np.float64),
        np.ascontiguousarray(kinds), pts, starts, np.asarray(halves, np.float64), TUNNEL_ROAD_CLEAR_M,
        TUNNEL_ROAD_MIN_SIN, TUNNEL_ROAD_REACH_M, SECTION_MIN_M), np.int64)


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
    if getattr(spec, "sides", None) is not None:      # the map says (_osm_sidewalks)
        return spec.kind in BUILT and bool(spec.sides)
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
    the left; the other side is the median). Where OpenStreetMap's sidewalk tag says which: those (_osm_sidewalks)."""
    if getattr(spec, "sides", None):
        return tuple(spec.sides)
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


def extras(spec, pts, lon, left_hand=False, country=None, osm=None):
    """The sidewalk / gutter / marking polylines of one road: [(look dict with m / tl / p, width, pts)].
    pts: its centreline (k, 3) in the anchor frame (a whole ground part: its broken lines' dashes start their cycle at
    its start, so they meet across tiles); lon: its longitude; country: ISO code (markings.style: the country's line
    colours, widths and dash patterns; by longitude when unknown); osm: OpenStreetMap's lane tags of the road
    (railtags.LANE_COLUMNS: the lines lane by lane, markings.lane_plan). Pure."""
    import markings
    out = []
    st = markings.style(country, lon)
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
            if kind == "paved":     # a double yellow line along it (Boston Street, Mapillary 2026-10-17) ...
                look = {"m": st.centre, "tl": CENTRE["tl"], "p": CENTRE["p"]}
                # ... where centre lines are white one solid white edge line along the median's curb (PR #146)
                for k in ((0,) if st.centre == "line_white" else (0, 1)):
                    out.append((look, st.w, offset_polyline(
                        pts, ms * (w / 2 - EDGE_LINE["inset"] - k * (st.w + st.double_gap)))))
            out.append(({**GUTTER, "side": ms}, GUTTER["w"], offset_polyline(pts, ms * (w / 2 - GUTTER["w"] / 2))))
    motorway = cls == "motorway"
    lw_ = st.w_motorway if motorway else st.w           # a line's width

    def mark(kind_, mat, width, along, pattern=None, side=None):
        """A solid line, or a broken one's dashes (pattern: dash, gap); kind_: "centre", "lane" or "edge"; side: an edge
        line's (+1 left / -1 right of the geometry: build_tile cuts it at a rounded corner, roadcorners.tile)."""
        if pattern is None:
            look = {"m": mat, "tl": EDGE_LINE["tl"], "p": CENTRE["p"], "kind": kind_}
            if side is not None:
                look["side"] = side
            out.append((look, width, along))
            return
        for piece in markings.dashes(along, pattern):
            out.append(({"m": mat, "tl": max(1.0, pattern[0]), "p": CENTRE["p"], "kind": kind_}, width, piece))
    # the driving width (a town street's parking lanes and a rural road's edge strips left out: three lanes on every
    # rural Interstate, GitHub #43) and its lanes (OpenStreetMap's count where known)
    parked = urban and cls in PARKING_CLASSES and not (not spec.oneway and cls in URBAN_ARTERIAL)
    drive, lanes = lane_layout(spec)
    plan = markings.lane_plan(osm, not spec.oneway, drive, lanes, left_hand) \
        if kind == "paved" and (cls in CENTRE_CLASSES or cls in LANE_CLASSES) else None
    if plan is not None:
        # the map's lanes: each line where its lanes meet (OpenStreetMap lanes:forward / backward / both_ways,
        # overtaking, change:lanes; research §5)
        pat = st.lane_motorway if motorway else (st.lane_urban if urban else st.lane_rural)
        cpat = st.centre_urban if urban else st.centre_rural
        d = (st.double_gap + lw_) / 2

        def at(off):
            return offset_polyline(pts, off) if abs(off) > 1e-6 else pts.copy()
        for off, role, how in plan:
            if role == "lane":
                mark("lane", "line_white", lw_, at(off), None if how else pat)
            elif role == "twltl":           # solid on the through side, broken on the turn lane's (MUTCD 3B.05)
                mark("centre", st.centre, lw_, at(off + how * d))
                mark("centre", st.centre, lw_, at(off - how * d), cpat)
            elif how == "double":
                if st.centre == "line_yellow" or st is markings.STYLES.get("GB"):
                    for sd in (1, -1):
                        mark("centre", st.centre, lw_, at(off + sd * d))
                else:
                    mark("centre", st.centre, lw_, at(off))
            elif how in ("solid_right", "solid_left"):
                sd = -1 if how == "solid_right" else 1
                mark("centre", st.centre, lw_, at(off + sd * d))
                mark("centre", st.centre, lw_, at(off - sd * d), cpat)
            else:
                mark("centre", st.centre, lw_, at(off), cpat)
    if plan is None and kind == "paved" and cls in CENTRE_CLASSES \
            and w - (2 * PARKING_M if parked else 0.0) >= st.min_centre_m \
            and not spec.oneway:   # no paint on cobbles (the width between the parking lanes, edge strips included)
        # the centre line (MUTCD 3B.01-02, TSRGD / RMS / IISR centre lines): too narrow a road has none; four lanes or
        # more get a solid line (the Americas: double solid yellow), and in the US a town's street a double solid one;
        # otherwise one broken line in the country's colour and town / country pattern
        solid = lanes >= 4 or (st.double_centre_urban and urban)
        if solid and st.centre == "line_yellow":
            for side in (1, -1):
                mark("centre", st.centre, lw_, offset_polyline(pts, side * (st.double_gap + lw_) / 2))
        elif solid:
            mark("centre", st.centre, lw_, pts.copy())
        else:
            mark("centre", st.centre, lw_, pts.copy(), st.centre_urban if urban else st.centre_rural)
    if plan is None and kind == "paved" and cls in LANE_CLASSES:
        # lane lines between the lanes of one direction, broken in the country's pattern
        pat = st.lane_motorway if motorway else (st.lane_urban if urban else st.lane_rural)
        if spec.oneway and lanes >= 2:
            lw = drive / lanes
            for k in range(1, lanes):
                mark("lane", "line_white", lw_, offset_polyline(pts, -drive / 2 + k * lw), pat)
        elif not spec.oneway and lanes >= 4:
            n = lanes // 2
            lw = drive / 2 / n
            for side in (1, -1):
                for k in range(1, n):
                    mark("lane", "line_white", lw_, offset_polyline(pts, side * k * lw), pat)
    if kind == "paved" and not has_sidewalks(spec):          # no curb: the asphalt's edge
        dw = EDGE_DECAL["w"]
        for side in (1, -1):
            line = offset_polyline(pts, side * (w / 2 + (EDGE_DECAL_AT - 0.5) * dw))
            # (u runs from the ribbon's left to its right: the road is on the right of the left side's, so the right
            # side's runs backwards)
            out.append(({**EDGE_DECAL, "side": side}, dw, line if side > 0 else line[::-1].copy()))
    if kind == "paved" and (not urban or not has_sidewalks(spec)) and cls in (st.edge_classes or EDGE_LINE_CLASSES):
        # no curb: edge lines
        # the right edge white; a one-way carriageway's left edge yellow where the country says so (MUTCD 3B.09:
        # divided roads, ramps); wider on motorways (TSRGD 1012, RMS: 0.20-0.30 m)
        ew = st.edge_motorway if motorway else st.edge
        travel_left = 1 if getattr(spec, "dir", 0) >= 0 else -1      # (dir < 0: one way against the line)
        for side in (1, -1):
            inset = motorway_shoulder(spec) if rural_motorway(spec) else EDGE_LINE["inset"]
            mat = st.left_edge if (st.left_edge and spec.oneway and side == travel_left) else "line_white"
            mark("edge", mat, ew, offset_polyline(pts, side * (w / 2 - inset)), side=side)
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
            if kind == "paved":     # a double yellow line along it (Boston Street, Mapillary 2026-10-17) ...
                look = {"m": centre_colour(lon), "tl": CENTRE["tl"], "p": CENTRE["p"]}
                # ... outside the Americas one solid white edge line along the median's curb (PR #146)
                for k in ((0,) if centre_colour(lon) == "line_white" else (0, 1)):
                    out.append((look, CENTRE["w"], offset_polyline(
                        pts, ms * (w / 2 - EDGE_LINE["inset"] - 2 * k * CENTRE["gap"]))))
            out.append(({**GUTTER, "side": ms}, GUTTER["w"], offset_polyline(pts, ms * (w / 2 - GUTTER["w"] / 2))))
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
        parked = urban and cls in PARKING_CLASSES and not (not spec.oneway and cls in URBAN_ARTERIAL)
        # (a rural road's edge strips counted as lanes: three on every rural Interstate, GitHub #43)
        drive = w - ((PARKING_M if spec.oneway else 2 * PARKING_M) if parked else 2 * edge_margin(spec))
        lanes = spec.lanes if spec.lanes else int(drive // LANE_M)      # (OpenStreetMap's count where known)
        if spec.oneway and lanes >= 2:
            lw = drive / lanes
            for k in range(1, lanes):
                out.append((LANE_LINE, LANE_LINE["w"], offset_polyline(pts, -drive / 2 + k * lw)))
        elif not spec.oneway and lanes >= 4:
            n = lanes // 2
            lw = drive / 2 / n
            for side in (1, -1):
                for k in range(1, n):
                    out.append((LANE_LINE, LANE_LINE["w"], offset_polyline(pts, side * k * lw)))
    if kind == "paved" and not has_sidewalks(spec):          # no curb: the asphalt's edge
        dw = EDGE_DECAL["w"]
        for side in (1, -1):
            line = offset_polyline(pts, side * (w / 2 + (EDGE_DECAL_AT - 0.5) * dw))
            # (u runs from the ribbon's left to its right: the road is on the right of the left side's, so the right
            # side's runs backwards)
            out.append(({**EDGE_DECAL, "side": side}, dw, line if side > 0 else line[::-1].copy()))
    if kind == "paved" and (not urban or not has_sidewalks(spec)) and cls in EDGE_LINE_CLASSES:   # no curb: edge lines
        for side in (1, -1):
            inset = motorway_shoulder(spec) if rural_motorway(spec) else EDGE_LINE["inset"]
            out.append((EDGE_LINE, EDGE_LINE["w"], offset_polyline(pts, side * (w / 2 - inset))))
    return out


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
    per segment, consecutive pieces joined; earthfast.clip_polyline). Pure."""
    import fast
    fast.require(74, "clip_polyline")
    P = np.ascontiguousarray(np.asarray(pts, np.float64))
    if P.ndim != 2 or len(P) < 2:
        return []
    out, starts = fast._ef.clip_polyline(P, float(x0), float(y0), float(x1), float(y1))
    out, starts = np.asarray(out), np.asarray(starts)
    return [out[starts[i]:starts[i + 1]].copy() for i in range(len(starts) - 1)]


def simplify(pts, tol=SIMPLIFY_M, ztol=None):
    """Ramer-Douglas-Peucker on the xy of a polyline (k, D). ztol (D >= 3): a point is also kept where its height is
    more than this off the line between the kept ones (a deck drawn at its own heights: on xy alone a straight bridge
    kept its two ends in a tile and lost its rise between, 2.5 m below the abutment it met; a car drove into the
    ground, Baltimore's Lombard Street 2026-10-20). Pure."""
    if len(pts) < 3:
        return pts
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
        k = int(np.argmax(d))
        if d[k] > tol:
            m = i + 1 + k
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


WALK_DRAPE_M = 2.0      # m between the ground samples under a sidewalk (roadmesh.DRAPE_STEP: the road mesh's edges)
WALK_GROUND_M = 0.5     # ... on the drawn ground (ground): its squares' kinks then cost the run under 0.5 cm over
                        # WALK_DRAPE_TOL_M (a 3 m dip on a hill, 1/32 m quantum: 1.9-2.0 cm; every 1 m: 2.7-2.8)
WALK_DRAPE_TOL_M = 0.02     # m a sidewalk point dropped again may lie off the line through its kept neighbours


def drape_walks(frame, elev, walks, ground=None):
    """The sidewalk runs and corner pieces of build_tile's walks put on the ground, in place: z = elev at the curb line,
    as the road mesh's draped edges there. The runs carried the road's centreline z, sampled at its vertices only and
    interpolated along straight stretches (simplify keeps the xy bends), so a sidewalk crossing a dip ran as a chord
    up to the dip's depth in the air beside the draped road. Each run is sampled every WALK_DRAPE_M m, thinned to the
    points the ground's shape needs (WALK_DRAPE_TOL_M) and densified again: a flat street keeps exactly its points.
    One elev call (its cost is per call). ground(x (n,), y (n,)) -> z (n,): the terrain block's drawn ground in the
    frame (roadmesh.drawn_ground), which the road surfaces lie on (roadmesh.build follow) and elev is off by its
    height quantum and more between its samples; the curbs take it instead, sampled every WALK_GROUND_M m (the user,
    2026-10-21: sidewalks on the drawn ground with upstream's road surfaces). One ground call."""
    import sidewalks
    items, xy = [], []
    for wk in walks:
        if "corner" in wk:
            arc = np.asarray(wk["corner"]["arc"], np.float64)
            items.append((wk, arc))
            xy.append(arc[:, :2])
            continue
        pts = densify(np.asarray(wk["pts"], np.float64), WALK_DRAPE_M if ground is None else WALK_GROUND_M)
        curb, _ = sidewalks.run_geometry(pts, wk["side"], wk.get("w", SIDEWALK["w"]))
        items.append((wk, pts))
        xy.append(curb[:, :2])
    if not items:
        return
    xy = np.concatenate(xy)
    if ground is not None:
        u = np.asarray(ground(xy[:, 0], xy[:, 1]), np.float64)
    else:
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        _, _, u = frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))
    k = 0
    for wk, pts in items:
        z = u[k:k + len(pts)]
        k += len(pts)
        if "corner" in wk:
            c = wk["corner"]
            arc = pts.copy()
            arc[:, 2] = z
            # the outer corners: the ends at the ends of the arc (where the runs' slabs end), the rest between
            zo = 0.5 * (z[0] + z[-1])
            outer = [tuple(q) for q in c["outer"]]
            outer = [(*outer[0][:2], z[-1])] + [(*q[:2], zo) for q in outer[1:-1]] + [(*outer[-1][:2], z[0])]
            wk["corner"] = {**c, "arc": arc, "outer": outer}
            continue
        pts = pts.copy()
        pts[:, 2] = z
        wk["pts"] = densify(pts[sidewalks.simplify(pts, WALK_DRAPE_TOL_M)])


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
    """A tile's box (x0, y0, x1, y1) in its frame: an anchor tile's square; a geotile's (tx a geotiles.GeoBox, ty
    None) its quad's bounding box in its own frame, an OwnedBox (box_owns: its own points exactly)."""
    b = getattr(tx, "box", None)
    if b is not None:
        return b
    return tx * TILE, ty * TILE, (tx + 1) * TILE, (ty + 1) * TILE


def box_owns(box, x, y):
    """geotiles.box_owns: the tile's own points (a geotile's exactly, an anchor tile's half-open box)."""
    import geotiles
    return geotiles.box_owns(box, x, y)


def tile_side(tx, ty):
    """m: the side of a square of the tile's area (TILE for an anchor tile)."""
    side = getattr(tx, "side", None)
    return side() if side is not None else TILE


def box_gap(x0, y0, x1, y1, x, y):
    """m from (x, y) to the nearest point of the box [x0, x1] x [y0, y1] (Euclidean; 0 inside)."""
    return math.hypot(max(x0 - x, x - x1, 0.0), max(y0 - y, y - y1, 0.0))


def tiles_near(x, y, radius):
    """Tile keys whose square comes within `radius` of (x, y): the Euclidean distance to its nearest point, a disc (the
    user, 2026-10-28: "change the quality/view distance radius from a square to a circle"). Before it was the square
    (Chebyshev): its corners reached 41 % farther than its sides, ~27 % more tiles than the disc for no reach ahead
    (the anchor frame's axes are not the way the car drives). Order: tx, then ty (as before)."""
    t0x, t1x = int(math.floor((x - radius) / TILE)), int(math.floor((x + radius) / TILE))
    t0y, t1y = int(math.floor((y - radius) / TILE)), int(math.floor((y + radius) / TILE))
    r2 = radius * radius
    out = []
    for tx in range(t0x, t1x + 1):
        dx = max(tx * TILE - x, x - (tx + 1) * TILE, 0.0)
        for ty in range(t0y, t1y + 1):
            dy = max(ty * TILE - y, y - (ty + 1) * TILE, 0.0)
            if dx * dx + dy * dy <= r2:
                out.append((tx, ty))
    return out


def tile_distance(tx, ty, x, y):
    """m from (x, y) to tile (tx, ty)'s square (Euclidean, as tiles_near; 0 inside)."""
    return box_gap(*tile_bbox(tx, ty), x, y)


def wanted_tiles(x, y, vx=0.0, vy=0.0, send_m=SEND_M):
    """The tile keys the mod should have around (x, y): those within send_m, and moving at FAST_MS or more also those
    within CORRIDOR_M of the path ahead, up to LEAD_S of driving (LEAD_MAX_M at most)."""
    want = set(tiles_near(x, y, send_m))
    s = math.hypot(vx, vy)
    lead = min(LEAD_MAX_M, s * LEAD_S)
    if s < FAST_MS or lead <= send_m:
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


TILE_BUILDING_LAYERS = ("buildings", "building_parts", "water")   # a road tile's own cells, with its buildings


def tile_needs(frame, tx, ty, buildings=True, widths=True, water_ring=0):
    """The (cell, layer)s a road tile asks the map data for, nearest first (MapData.warm, before building it): the
    roads and infrastructure (bridges) of the cells under it and their buildings (buildings on), the roads of the ring
    around (grading's neighbours), the buildings of two rings around (widths on: roadwidth's walls) and the water of
    water_ring cells around (seacarve's rasters and river lines), and the roads of the second ring (gradesep's
    neighbours of the first)."""
    keys = cells_of_tile(frame, tx, ty)
    if not keys:
        return []
    z = keys[0].z
    n = 1 << z
    x0, x1 = min(k.x for k in keys), max(k.x for k in keys)
    y0, y1 = min(k.y for k in keys), max(k.y for k in keys)
    cx, cy = 0.5 * (x0 + x1), 0.5 * (y0 + y1)

    def ring(r):
        out = [mapcells.CellKey(z, x % n, y) for x in range(x0 - r, x1 + r + 1) for y in range(y0 - r, y1 + r + 1)
               if 0 <= y < n]
        return sorted(out, key=lambda k: (abs((k.x - cx + n / 2) % n - n / 2) + abs(k.y - cy), k))
    own = set(keys)
    tile = ("roads", "infrastructure") + (TILE_BUILDING_LAYERS if buildings else ())
    needs = [(k, ly) for k in sorted(keys) for ly in tile]
    needs += [(k, "roads") for k in ring(1) if k not in own]
    # (and the ring past that: gradesep reads the neighbours of every cell whose level ranges are asked, the ring's too)
    import gradesep
    if gradesep.ON:
        r1 = set(ring(1))
        needs += [(k, "roads") for k in ring(2) if k not in r1]
    if widths:      # the walls of the cells the ring's roads cross, each with its neighbours' buildings
        needs += [(k, "buildings") for k in ring(2) if k not in own]
    if water_ring:
        done = {k for k, ly in needs if ly == "water"}
        needs += [(k, "water") for k in ring(water_ring) if k not in done]
    return needs


COVER_MARGIN_M = 30.0   # m past the tile: the ground carriageways a deck's parapet is tested against (build_tile cover_out)


def lane_layout(spec):
    """(driving width m, lanes) of a road as its lane lines are painted (extras) and the AI drives it (navgraph): the
    carriageway less a town street's parking lanes or a rural road's edge strips (counted as lanes, every rural
    Interstate had three, GitHub #43), its lanes OpenStreetMap's count or as many LANE_M as fit (at least 1). Pure."""
    w, cls = spec.width, spec.cls
    parked = spec.urban and cls in PARKING_CLASSES and not (not spec.oneway and cls in URBAN_ARTERIAL)
    drive = w - ((PARKING_M if spec.oneway else 2 * PARKING_M) if parked else 2 * edge_margin(spec))
    lanes = int(spec.lanes) if spec.lanes else int(drive // LANE_M)
    return drive, max(1, lanes)


def lane_tags(feats, i):
    """OpenStreetMap's lane tags of feature i ({key: value}, railtags.LANE_COLUMNS joined by MapData), or None."""
    import railtags
    out = {}
    for k in railtags.LANE_COLUMNS:
        if k == "junction":
            continue                            # (a ring's tag, not lane data: roads._rings reads it)
        col = feats.attrs.get(railtags.lane_attr(k))
        if col is not None and col[i] is not None:
            out[k] = col[i]
    return out or None


def _chunk(look, w, c):
    out = {"m": look["m"], "w": round(float(w), 2), "tl": look["tl"], "p": look["p"],
           "n": [[round(float(v), 3) for v in p] for p in c]}
    if look.get("layer"):
        out["layer"] = look["layer"]
    return out


DECK = {"m": "road_asphalt_2lane", "tl": 11.0, "p": 20}      # bridge decks (roadmesh.deck)
DECK_STEP = 4.0         # m between deck profile points
DECK_CLEAR = 0.05       # m a deck stays above the ground under it (no z-fighting where it rests on it)
# Usable space below the lowest structural girder, not merely to the road surface.
# Six metres accommodates tall road vehicles such as semi-trucks; unlimited-height
# modded vehicles and raised equipment cannot have a finite universal guarantee.
MIN_BRIDGE_HEADROOM_M = 6.0
BRIDGE_HEADROOM_MARGIN_M = 0.15   # mesh simplification/float and vehicle clearance tolerance
DECK_ROAD_CLEAR = MIN_BRIDGE_HEADROOM_M + BRIDGE_HEADROOM_MARGIN_M + roadmesh.DECK_DEPTH + roadmesh.GIRDER_D
DECK_FULL_DEPTH = roadmesh.DECK_DEPTH + roadmesh.GIRDER_D    # m of slab and girders under a long span's road
DECK_MIN_DEPTH = 0.9    # m: the thinnest deck (a short slab bridge)
DECK_SPAN_RATIO = 22.0  # a deck is its main span / this deep (girder bridges run span/20-25), DECK_MIN..FULL_DEPTH


def deck_depth(span):
    """m of structure (slab + girders) under a deck's road for a main span of span m. A short overpass or channel
    span drew 2.2 m of slab and girders whatever its span, and had to climb that much more to clear what is under it
    (a crown on 24 m overpasses and 2.7 m humps on the Sunshine Skyway causeway's channel bridges, 2026-10-27). Pure."""
    return float(np.clip(float(span) / DECK_SPAN_RATIO, DECK_MIN_DEPTH, DECK_FULL_DEPTH))


def deck_split(depth):
    """(slab m, girders m) of a deck depth m: the full deck's 1.2 : 1.0 kept. Pure."""
    slab = roadmesh.DECK_DEPTH * float(depth) / DECK_FULL_DEPTH
    return slab, float(depth) - slab


def deck_road_clear(depth=None):
    """m a deck's road stands over a road under it: the headroom, its margin and the deck's depth. Pure."""
    return MIN_BRIDGE_HEADROOM_M + BRIDGE_HEADROOM_MARGIN_M + (DECK_FULL_DEPTH if depth is None else float(depth))


def geom_span(lines):
    """m: a bridge chain's main span from its geometry alone: its longest part, or (one longer than 1.5 PIER_SPAN_M)
    that part's pier spacing (pier_sites' PIER_SPAN_M grid). Pure."""
    L = max((float(np.hypot(*np.diff(np.asarray(ln, np.float64)[:, :2], axis=0).T).sum())
             for ln in lines if len(ln) >= 2), default=0.0)
    if L <= 1.5 * PIER_SPAN_M:
        return L
    return L / max(1, int(round(L / PIER_SPAN_M)))
DECK_CLEAR_REACH_M = 20.0   # full headroom either side of a road crossing (matches approach fills' flat reach)
DECK_GRADE = 0.05       # that requirement falls 5 % along the deck from the crossing
DECK_MAX_GRADE = 0.06   # a deck climbs no steeper than this from an abutment
DECK_STEEP_GRADE = 0.15  # ... nor, whatever ground the caps were raised to, than this over lower ground (no walls)
CORNER_CONCRETE_M = 4.0  # m: a concrete street this close to a curb return fills its corner in concrete too
AT_GRADE_M = 3.0        # m: a road crossing a deck within AT_GRADE_REACH_M of an abutment that cannot get this
AT_GRADE_REACH_M = 12.0     # high over it is a junction at grade (the intersection a ramp starts from)
CHAIN_CELLS = 12        # map cells read at most to close one bridge chain


def _rail_drawn(feats, i):
    """Is feature i a railway line drawn on the map (railways.drawn: worn ones too)?"""
    import railways
    return railways.drawn(feats, i) is not None


def _clearance_deficits(lines, zs, crossings, depth=None):
    """(part, usable m) below the road target in a modelled crossing corridor.

    Road heights are the graded lower road's height at the crossing. Keep true
    abutment junctions at grade; data/grade constraints cannot promise global clearance.
    """
    out = []
    for pi, sc, zc in crossings:
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(lines[pi][:, :2], axis=0).T))]
        if min(sc, s[-1] - sc) <= AT_GRADE_REACH_M:
            continue
        lo, hi = max(0.0, sc - DECK_CLEAR_REACH_M), min(s[-1], sc + DECK_CLEAR_REACH_M)
        at = np.r_[lo, s[(s > lo) & (s < hi)], hi]
        headroom = float(np.min(np.interp(at, s, zs[pi]))) - zc - (DECK_FULL_DEPTH if depth is None else depth)
        if not np.isfinite(headroom) or headroom < MIN_BRIDGE_HEADROOM_M - 1e-6:
            out.append((pi, headroom))
    return out


def _crossings(lines, ground_lines):
    """(part index, arc m, point xy) where a ground road (polylines in metres) crosses under a deck
    part (earthfast.segment_crossings). Pure."""
    import fast
    lp = [np.asarray(ln, np.float64)[:, :2] for ln in lines]
    gp = [np.asarray(gl, np.float64)[:, :2] for gl in ground_lines]
    if not lp or not gp:
        return []
    fast.require(57, "segment_crossings")
    starts = lambda ps: np.r_[0, np.cumsum([len(p) for p in ps])].astype(np.int64)
    got = fast._ef.segment_crossings(np.ascontiguousarray(np.vstack(lp)), starts(lp),
                                     np.ascontiguousarray(np.vstack(gp)), starts(gp))
    return [(pi, arc, np.array([x, y])) for pi, arc, x, y in got]


def _crossings_by(lines, ground_lines):
    """_crossings for each of ground_lines apart, in one kernel call: [[(part index, arc m, point xy)] per ground line],
    each list as _crossings(lines, [that line]) gives it. Pure."""
    import fast
    lp = [np.asarray(ln, np.float64)[:, :2] for ln in lines]
    gp = [np.asarray(gl, np.float64)[:, :2] for gl in ground_lines]
    out = [[] for _ in gp]
    if not lp or not gp:
        return out
    fast.require(76, "segment_crossings_g")
    starts = lambda ps: np.r_[0, np.cumsum([len(p) for p in ps])].astype(np.int64)
    for pi, g, arc, x, y in fast._ef.segment_crossings_g(np.ascontiguousarray(np.vstack(lp)), starts(lp),
                                                          np.ascontiguousarray(np.vstack(gp)), starts(gp)):
        out[g].append((pi, arc, np.array([x, y])))
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


def pier_sites(xy, z, width, ground_fn, road_lines=(), wet_fn=None, depth=None):
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
    under = DECK_FULL_DEPTH if depth is None else float(depth)
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


OBS_BLEND_DG = 0.04     # a measured deck eased onto its joints' heights changes its grade by at most this (BridgeProfiles)
OBS_BLEND_MIN_M = 10.0  # m, the shortest easing
OBS_MAX_GRADE = 0.14    # a measured deck steeper than this after the easing (2 m steps) keeps the inference
OBS_END_MAX_M = float(os.environ.get("BNE_OBS_END_MAX", "inf"))  # m a measured deck's end may lie off the graded road at
                        # its abutment, else the inference (inf: always eased onto it; 4 m made Springfield worse)


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
        self.depth = {}                         # (fid, j) -> m of slab and girders under its road (deck_depth)
        self.lock = threading.Lock()

    def _water(self, lines, nav=0.0, depth=None, channel=0.0):
        """Clearance requirements over water (deck_profile_once crossings with their own clearance): every
        WATER_STEP_M along each wet run of each line, water_clear of the run's length over the ground there; over a
        run of NAV_RUN_M or more at least nav (m: the bridge's navigation clearance, bridgetypes.nav_clearance) plus
        the slab and girders under the road."""
        out = []
        if self.wet is None:
            return out
        under = DECK_FULL_DEPTH if depth is None else float(depth)
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
            sea = self.frame.geodetic_to_enu(la, lo, np.zeros(len(la)))[2]
            i = 0
            while i < len(w):
                if not w[i]:
                    i += 1
                    continue
                j = i
                while j + 1 < len(w) and w[j + 1]:
                    j += 1
                c = water_clear(s[j] - s[i] + (s[1] - s[0]))
                base = g
                if nav > 0:
                    # the inventory's clearance decides over the share-of-span rule, from the water's surface (sea
                    # level) not the carved bed: a causeway's 100 m channel bridge with 3.8 m of clearance was asked
                    # for 15 m over a 3 m deep bed and humped to its grade cap (the Sunshine Skyway causeway, 2026-10-27)
                    c = nav + under if s[j] - s[i] >= NAV_RUN_M else min(c, nav + under)
                    base = np.maximum(g, sea)
                run = s[j] - s[i]
                # the nav clearance holds over the channel only: the main span (channel m: NBI item 48) over the
                # run's deepest water, else the middle half of the run; outside it the clearance asked eases off at the
                # design grade to the run's own (water_clear: a causeway's low). Held flat across the whole run the
                # Sunshine Skyway stood 58 m up for 3.5 km of its causeway, and where the grade cap only just reached
                # the run's start the solver humped the Vincent Thomas Bridge 5 m over its span (2026-10-28)
                c_low = min(c, water_clear(run + (s[1] - s[0])))
                if nav > 0 and channel > 0 and 3 * channel < run:      # (a river crossing: its whole run)
                    seg = np.arange(i, j + 1)
                    mid = float(s[seg[int(np.argmin(g[seg]))]])
                    mid = float(np.clip(mid, s[i] + channel / 2, s[j] - channel / 2))
                    c0, c1 = mid - channel / 2, mid + channel / 2
                else:
                    c0, c1 = s[i] + run / 4, s[j] - run / 4
                for sc in np.arange(s[i], s[j] + 1e-6, WATER_STEP_M):
                    out_by = max(0.0, c0 - sc, sc - c1)
                    cc = max(c_low, c - DECK_DESIGN_GRADE * out_by) if nav > 0 else c
                    out.append((k, float(sc), float(np.interp(sc, s, base)), cc))
                i = j + 1
        return out

    def _ground(self, xy):
        la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return self.frame.geodetic_to_enu(la, lo, np.asarray(self.elev(la, lo), np.float64))[2]

    specs_of = staticmethod(lambda feats: bridge_specs(feats))
    parts_of = staticmethod(lambda feats, i: bridge_parts(feats, i))
    levels_of = staticmethod(lambda feats, i: bridge_levels(feats, i))

    def _load(self, key, pool, ground):
        """Cell key's bridge parts into pool and its ground roads into ground (as the chain solves gather them)."""
        parts, roads_ = self._cell_entries(key)
        for k, v in parts:
            # a part in several cells can come clipped differently: the longest (the same part whichever
            # cell a process loads first: the terrain and the road tiles took different ones, 2026-10-17)
            old = pool.get(k)
            if old is None or v[4] > old[4]:
                pool[k] = v
        for k, lines in roads_:
            if k not in ground:
                ground[k] = lines

    def _cell_entries(self, key):
        """([((fid, j), (spec, deck line, lon, level, ring points))], [((fid, "g"), [ground lines])]) of cell key, made
        once (every chain solved gathered its cells again: each a loop over all their roads, 2026-10-25). Not to be
        changed by the caller."""
        cache = self.__dict__.setdefault("cell_entries", {})
        with self.lock:
            got = cache.get(str(key))
        if got is not None:
            return got
        parts, roads_ = [], []
        feats = self.cell_fn(key)
        if feats is not None and len(feats):
            bs = self.specs_of(feats)
            rs = road_specs(feats)
            names = feats.attrs.get("name")
            for i in range(len(feats)):
                fid = str(feats.ids[i])
                if bs[i] is not None:
                    lvls = self.levels_of(feats, i)
                    best = {}
                    for j, ring in enumerate(self.parts_of(feats, i)):
                        if (fid, j) in best and len(ring) <= best[(fid, j)][4]:
                            continue
                        e, n = mapgeom.enu_xy(feats, self.frame, ring)
                        best[(fid, j)] = (bs[i], densify(_dedupe(np.stack([e, n], axis=1)), DECK_STEP),
                                          float(np.mean(ring[:, 0])), lvls[j] if j < len(lvls) else 1.0, len(ring),
                                          str(names[i]) if names is not None else "")     # (roadz: name matching)
                    parts.extend(best.items())
                if rs[i] is not None:
                    lines = []
                    for ring in ground_parts(feats, i):
                        e, n = mapgeom.enu_xy(feats, self.frame, ring)
                        lines.append(np.stack([e, n], axis=1))
                    roads_.append(((fid, "g"), lines))
                elif _rail_drawn(feats, i):
                    # a railway on the ground: the decks over it keep their headroom too (GitHub #324: a road bridge
                    # lay on the tracks; only roads under a deck were counted)
                    import railways
                    lines = []
                    for kind, rings in railways._pieces(feats, i):
                        if kind == "ground":
                            for ring in rings:
                                e, n = mapgeom.enu_xy(feats, self.frame, ring)
                                lines.append(np.stack([e, n], axis=1))
                    if lines:
                        roads_.append(((fid, "r"), lines))
        got = (parts, roads_)
        with self.lock:
            if len(cache) > 64:
                cache.clear()
            cache.setdefault(str(key), got)
            return cache[str(key)]

    END_JOINS = True        # (TunnelProfiles: none)

    def _end_joins_of(self, lines, halves, ground):
        """_end_joins of deck lines with the ground roads of ground ({(fid, "g"): lines}); [] for tunnels."""
        if not self.END_JOINS:
            return []
        return _end_joins(lines, halves, [g for k, v in ground.items() if k[1] == "g" for g in v])

    def _end_ons_of(self, lines, halves, levels, ground):
        """_end_ons of deck lines with the ground roads of ground; [] for tunnels."""
        if not self.END_JOINS:
            return []
        return _end_ons(lines, halves, levels, [g for k, v in ground.items() if k[1] == "g" for g in v])

    def _end_join_pairs(self, lines, halves, ground, levels=None):
        """(i, j): line i's free end joins line j's end (_end_joins) or stands on line j (_end_ons)."""
        joins = self._end_joins_of(lines, halves, ground)
        out = set()
        if levels is not None:
            for k, _at, j, _arc in self._end_ons_of(lines, halves, levels, ground):
                out.add((min(k, j), max(k, j)))
        if not joins:
            return sorted(out)
        moved = {(k, at): p for k, at, p in joins}
        for k, at, p in joins:
            for j, ln in enumerate(lines):
                if j != k and any(np.hypot(*(np.asarray(moved.get((j, a2), np.asarray(ln)[a2, :2]), np.float64) -
                                             np.asarray(p))) < 1e-6 for a2 in (0, -1)):
                    out.add((min(k, j), max(k, j)))
        return sorted(out)

    def _resolve(self, start, pool, ground, loaded):
        """The chain of part `start` (keys), loading the cell at each loose end until closed."""
        while True:
            nk = _EndJoin()
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
            # and a free end joining another part's end (_end_joins): solved as one
            for a, b in self._end_join_pairs([pool[k][1] for k in keys], [pool[k][0].width / 2 for k in keys], ground,
                                             [pool[k][3] for k in keys]):
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
            # the loose ends first, in key order: a cell loaded can put a longer copy of a part in the pool (_load), whose
            # ends deg never saw (KeyError: a Chicago road tile on a Windows test PC, 2026-10-24), and the set's order
            # changes with each process's string hashing (which cells CHAIN_CELLS let in)
            ends = [p for k in sorted(comp) for p in (pool[k][1][0], pool[k][1][-1]) if deg[nk(p)] == 1]
            grew = False
            for p in ends:
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
        # (solved with its free ends that join another part's moved onto those joints: _end_joins; the heights are
        # per point, so the parts keep their own lines)
        lines = _joined_lines(lines, self._end_joins_of(lines, [pool[k][0].width / 2 for k in comp], ground))
        gl = [g for k, v in ground.items() if k[1] == "g" for g in v]
        rl = [g for k, v in ground.items() if k[1] == "r" for g in v]
        cr = _crossings(lines, gl)
        road_z = self._ground(np.array([c[2] for c in cr])) if cr else []
        # (the railways under the chain: the same headroom, given as their own clearance, so never a junction at
        # grade by an abutment as a road can be)
        rcr = _crossings(lines, rl) if rl else []
        rail_z = self._ground(np.array([c[2] for c in rcr])) if rcr else []
        import bridgetypes
        try:
            nav = bridgetypes.nav_clearance(self.frame, lines)
        except Exception as ex:             # the share-of-span rule alone
            log.debug("nav clearance: %s: %s", type(ex).__name__, ex)
            nav = 0.0
        required = [(c[0], c[1], z) for c, z in zip(cr, road_z)]
        depth = self._depth(lines) if bridge_profile_mode() == "curve" else None
        dd = self.__dict__.setdefault("depth", {})
        for k in comp:
            dd[k] = DECK_FULL_DEPTH if depth is None else depth
        try:
            channel = bridgetypes.nav_span(self.frame, lines) if nav > 0 else 0.0
        except Exception:
            channel = 0.0
        water = self._water(lines, nav, depth, channel)
        if not water and nav > 0 and bridge_profile_mode() == "curve":
            water = self._gap(lines, nav, depth)
        rails = [(c[0], c[1], z, deck_road_clear(depth)) for c, z in zip(rcr, rail_z)]
        levels = [pool[k][3] for k in comp]

        def solve(ground_fn):
            return deck_profile(lines, ground_fn, required + rails + water, clear=deck_road_clear(depth),
                                levels=levels, widths=[pool[k][0].width for k in comp], pins=_ground_pins(lines, gl))
        zs = solve(self._ground)
        # a free end standing on another deck: solved again with it at that deck's height as drawn (measured too:
        # _observed) (_end_ons)
        over = {}
        ons = self._end_ons_of(lines, [pool[c][0].width / 2 for c in comp], levels, ground)
        final = self._observed(comp, pool, lines, zs, pins=_ground_pins(lines, gl)) if ons else zs
        for k, at, j, arc in ons:
            lj = np.asarray(lines[j], np.float64)[:, :2]
            sj = np.r_[0.0, np.cumsum(np.hypot(*np.diff(lj, axis=0).T))]
            zt = float(np.interp(arc, sj, np.asarray(final[j], np.float64)))
            if np.isfinite(zt) and abs(zt - float(final[k][at])) > END_ON_STEP_M:
                q = np.asarray(lines[k], np.float64)[at, :2]
                over[(float(q[0]), float(q[1]))] = zt
        if over:
            keys_ = np.array(list(over), np.float64)
            vals = np.array(list(over.values()), np.float64)

            def ground_on(xy):
                z = np.asarray(self._ground(xy), np.float64)
                xy = np.asarray(xy, np.float64).reshape(-1, 2)
                for (px, py), zt in zip(keys_, vals):
                    z = np.where((np.abs(xy[:, 0] - px) < 1e-6) & (np.abs(xy[:, 1] - py) < 1e-6), zt, z)
                return z
            zs = solve(ground_on)
        deficits = _clearance_deficits(lines, zs, required + [r[:3] for r in rails], depth)
        if deficits:
            # (debug: a city has hundreds of such bridges, each solved again in every process and tile; as a warning it
            # filled players' consoles, GitHub #391, TGForever Discord 2026-10-28)
            log.debug("bridge clearance below %.1f m target: %s; abutment/grade or map constraints prevent full headroom",
                      MIN_BRIDGE_HEADROOM_M, [(comp[i], round(h, 2)) for i, h in deficits[:3]])
        if ons and not over:
            return final
        return self._observed(comp, pool, lines, zs, pins=_ground_pins(lines, gl))

    def _observed(self, comp, pool, lines, zs, level=1, pins=None):
        """The chain's deck heights where a source measured them (roadz.py: IGN BD TOPO, NVDB, US lidar), the solved
        ones (the inference) elsewhere. Each joint ends up at one height: an abutment (a joint no other part of the
        chain shares) at the solved one (the graded road it meets), a joint between parts at the mean of their measured
        ends; each part's profile is eased onto its joints' heights (earthfast.deck_blend: the grade changes by
        OBS_BLEND_DG at most). A part whose measured profile still steps or climbs past OBS_MAX_GRADE keeps the
        solve. Pure given the provider's cached data."""
        import roadz
        prov = roadz.PROVIDER
        if prov is None or not prov.enabled or not lines:
            return zs
        obs = []
        for k, ln in zip(comp, lines):
            xy = np.asarray(ln, np.float64)[:, :2]
            got = None
            try:
                spec = pool[k][0]
                nm = pool[k][5] if len(pool[k]) > 5 else ""
                z = prov.observed(self.frame, xy, spec.width / 2, level, "" if nm in ("None", "nan") else nm)
                if z is not None:
                    la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
                    ok = np.isfinite(z)
                    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
                    zf = np.interp(s, s[ok], np.asarray(z)[ok])         # (the ends past the last measurement: flat)
                    got = np.asarray(self.frame.geodetic_to_enu(la, lo, zf)[2], np.float64)
            except Exception as ex:             # the inference rather than no bridge
                log.debug("observed deck %s: %s: %s", k, type(ex).__name__, ex)
            obs.append(got)
        if all(o is None for o in obs):
            return zs
        nk = _EndJoin()
        ends = [(nk(np.asarray(ln)[0]), nk(np.asarray(ln)[-1])) for ln in lines]
        deg = {}
        for a, b in ends:
            deg[a] = deg.get(a, 0) + 1
            deg[b] = deg.get(b, 0) + 1
        # a joint a ground road goes on from (pins: the solve's abutment there) keeps the solve's height, the graded
        # road's, as an abutment does: eased to the decks' measured mean, the street went on 2.5 m over them (Charles
        # Street over I-83 in Baltimore, 2026-10-29)
        if pins is not None and len(pins):
            P = np.asarray(pins, np.float64).reshape(-1, 2)
            for i_, (a, b) in enumerate(ends):
                for nd, at in ((a, 0), (b, -1)):
                    q = np.asarray(lines[i_], np.float64)[at, :2]
                    if deg[nd] > 1 and (np.hypot(P[:, 0] - q[0], P[:, 1] - q[1]) < PIN_M).any():
                        deg[nd] = 1
        # a measured deck whose end is far off the graded road it meets there is a map gap (a flyover's untagged
        # stretch drawn on the ground: Springfield's 15 m): the inference keeps that part rather than a long ramp
        for i, (o, z, (a, b)) in enumerate(zip(obs, zs, ends)):
            z = np.asarray(z, np.float64)
            if o is not None and len(z) and OBS_END_MAX_M is not None and (
                    (deg[a] == 1 and abs(o[0] - z[0]) > OBS_END_MAX_M)
                    or (deg[b] == 1 and abs(o[-1] - z[-1]) > OBS_END_MAX_M)):
                log.debug("observed deck %s: its end %.1f m off the road it meets, the solve kept", comp[i],
                          max(abs(o[0] - z[0]), abs(o[-1] - z[-1])))
                obs[i] = None
        if all(o is None for o in obs):
            return zs
        target = {}
        for node in deg:
            if deg[node] == 1:
                continue
            got = [(o[0] if a == node else o[-1]) for o, (a, b) in zip(obs, ends) if o is not None and node in (a, b)]
            if got:
                target[node] = float(np.mean(got))
        import fast
        out = []
        for k, (ln, z, o, (a, b)) in enumerate(zip(lines, zs, obs, ends)):
            z = np.asarray(z, np.float64)
            if not np.isfinite(z).all():
                out.append(z)
                continue
            base = o if o is not None else z
            t0 = z[0] if deg[a] == 1 else target.get(a, z[0])
            t1 = z[-1] if deg[b] == 1 else target.get(b, z[-1])
            if o is None and t0 == z[0] and t1 == z[-1]:
                out.append(z)
                continue
            xy = np.asarray(ln, np.float64)[:, :2]
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            zz = np.asarray(fast._ef.deck_blend(s, np.ascontiguousarray(base), float(t0), float(t1), OBS_BLEND_DG,
                                                OBS_BLEND_MIN_M, 0.5), np.float64)
            if o is not None and len(s) > 3:
                step = np.interp(np.arange(0.0, s[-1], 2.0), s, zz)
                g = np.abs(np.diff(step)) / 2.0 if len(step) > 1 else np.zeros(1)
                if len(g) and float(g.max()) > OBS_MAX_GRADE:
                    log.debug("observed deck %s: %.0f %% after the blend, the solve kept", comp[k], 100 * g.max())
                    zz = np.asarray(fast._ef.deck_blend(s, np.ascontiguousarray(z), float(t0), float(t1), OBS_BLEND_DG,
                                                        OBS_BLEND_MIN_M, 0.5), np.float64)
                else:
                    # never under both the solve (its clearances) and the measurement: an easing toward a joint
                    # pulled a deck through the road under it (-1.8 m over I-95 at Springfield)
                    # (the ends as eased: the parts at a joint keep meeting at one height)
                    zz = np.r_[zz[0], np.maximum(zz, np.minimum(z, o))[1:-1], zz[-1]]
            out.append(zz)
        return out

    def _depth(self, lines):
        """m of slab and girders of a chain: deck_depth of the inventory's main span where a record matches, else of
        geom_span. Never raises."""
        span = 0.0
        try:
            import bridgetypes
            got = bridgetypes.matches(self.frame, lines)
            if got:
                span = bridgetypes.record(got[0][1])["span"]
        except Exception as ex:
            log.debug("deck span: %s: %s", type(ex).__name__, ex)
        return deck_depth(span if span > 0 else geom_span(lines))

    def depth_of(self, fid):
        """m of slab and girders under part fid's road (DECK_FULL_DEPTH when not solved here)."""
        ds = [d for k, d in list(self.__dict__.get("depth", {}).items()) if self._part_id(k) == fid]
        return max(ds) if ds else DECK_FULL_DEPTH

    def _gap(self, lines, nav, depth=None):
        """Clearance requirements where the sea's depth knows no water but the inventory gives the bridge a
        navigation clearance (a river: the Calcasieu, the Mississippi at New Orleans, 2026-10-19 stayed 10-14 m up
        under a 41-46 m clearance): every WATER_STEP_M along the chain's low stretch (ground within GAP_LOW_M of its
        lowest point, which lies at sea level, GAP_SEA_M at most, and GAP_DIP_M or more under the chain's highest
        end), nav plus the slab and girders over it. Only at sea level: up a river the inventory's clearances are
        often the span raised or measured from another datum (Portland's lift and bascule bridges came out 7-29 m
        high with it). Pure given the ground."""
        under = DECK_FULL_DEPTH if depth is None else float(depth)
        gs = [np.asarray(self._ground(np.asarray(ln, np.float64)[:, :2]), np.float64) for ln in lines]
        if not gs or not all(len(g) for g in gs):
            return []
        lo = float(min(g.min() for g in gs))
        top = float(max(max(g[0], g[-1]) for g in gs))
        if lo > GAP_SEA_M or lo > top - GAP_DIP_M:
            return []
        out = []
        for k, (ln, g) in enumerate(zip(lines, gs)):
            xy = np.asarray(ln, np.float64)[:, :2]
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            low = g <= lo + GAP_LOW_M
            for at in np.arange(0.0, s[-1] + 1e-9, WATER_STEP_M):
                if low[min(int(np.searchsorted(s, at)), len(s) - 1)]:
                    out.append((k, float(at), lo, nav + under))
        return out

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
        return BridgeProfiles.parts_todo(self, todo)      # (the whole parts: TunnelProfiles splits them)

    owner_of = None     # owner_of(self, comp, pool) -> the profiles that solve chain comp (ChainStore: the one of its
                        # first part's region), or None: this one

    def parts_todo(self, todo, solved_only=False):
        """[(fid, spec, xy, z, lon)] of the parts todo [((fid, j), feats, i)], each solved with its chain (once);
        solved_only: only those solved already."""
        out = []
        for key, feats, i in todo:
            with self.lock:
                got = self.done.get(key)
            if got is None and not solved_only:
                ring = self.parts_of(feats, i)[key[1]]
                got = self._chain_of(key, [(float(p[1]), float(p[0])) for p in (ring[0], ring[-1])])
            if got is not None:
                spec, xy, z, lon = got
                out.append((self._part_id(key), spec, xy, z, lon))
        return out

    def _chain_of(self, key, ends, adopt=True):
        """Solve the chain of part key (its ring's ends (lat, lon) say where to start loading): its parts into done.
        Returns done[key] or None. A chain another store owns (owner_of) is solved there and its result taken in, its
        geometry mapped into this frame (one deck profile per chain, whichever store a tile asks)."""
        with self.lock:
            got = self.done.get(key)
        if got is not None:
            return got
        pool, ground, loaded = {}, {}, set()
        for la_, lo_ in ends:
            ck = mapcells.cell_of(la_, lo_)
            if str(ck) not in loaded:
                loaded.add(str(ck))
                self._load(ck, pool, ground)
        if key not in pool:
            return None
        comp = self._resolve(key, pool, ground, loaded)
        owner = self.owner_of(self, comp, pool) if (adopt and self.owner_of is not None) else None
        if owner is not None and owner is not self:
            try:
                if self._adopt(owner, comp, pool):
                    with self.lock:
                        got = self.done.get(key)
                    if got is not None:
                        return got
            except Exception as ex:         # (solved here rather than not at all)
                log.debug("chain %s from its owner: %s: %s", key, type(ex).__name__, ex)
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
        return got

    def _ends_ll(self, xy):
        """(lat, lon) of a deck line's two ends (this frame's world)."""
        la, lo, _ = self.frame.enu_to_geodetic(np.array([xy[0][0], xy[-1][0]]), np.array([xy[0][1], xy[-1][1]]), 0.0)
        return [(float(la[0]), float(lo[0])), (float(la[1]), float(lo[1]))]

    def _adopt(self, owner, comp, pool):
        """Chain comp solved by owner (from its first part), its parts, piers, trusses and depths taken into this
        frame. False when owner's solve did not cover comp's parts."""
        first = min(comp)
        if owner._chain_of(first, self._ends_ll(pool[first][1]), adopt=False) is None:
            return False
        import geotiles
        with owner.lock:
            mine = [k for k in comp if k in owner.done]
            if len(mine) != len(comp):
                return False
            done = {k: owner.done[k] for k in mine}
            piers = {k: owner.piers.get(k, []) for k in mine}
            ck = min(comp)
            beams = owner.trusses.get(ck)
            tpiers = owner.piers.get(("truss", ck))
            depth = {k: owner.__dict__.get("depth", {}).get(k) for k in mine}
            extra = owner._adopt_extra(comp)
        mp = ChainView.mapper(owner.frame, self.frame)
        with self.lock:
            for k, (spec, xy, z, lon) in done.items():
                if k in self.done:
                    continue
                xyz = mp.pts(np.c_[np.asarray(xy, np.float64)[:, :2], np.asarray(z, np.float64)])
                self.done[k] = (spec, xyz[:, :2].copy(), xyz[:, 2].copy(), lon)
                self.piers[k] = [mp.pier(q) for q in piers[k]]
                if depth.get(k) is not None:
                    self.__dict__.setdefault("depth", {})[k] = depth[k]
            if beams is not None and ck not in self.trusses:
                self.trusses[ck] = [mp.beam(b) for b in beams]
                self.piers[("truss", ck)] = [mp.pier(q) for q in (tpiers or [])]
            self._adopt_take(extra)
        return True

    def _adopt_extra(self, comp):
        """Under self.lock: what else goes with comp's solve (TunnelProfiles: its sections)."""
        return None

    def _adopt_take(self, extra):
        """Under self.lock: _adopt_extra's from another store."""

    def _commit(self, k, ground=None):
        """Under self.lock: part k's solve was kept: its piers (pier_sites; TunnelProfiles keeps its sections)."""
        spec, xy, z, _lon = self.done[k]
        lines = [ln for v in (ground or {}).values() for ln in v]
        try:
            self.piers[k] = pier_sites(np.asarray(xy, np.float64), np.asarray(z, np.float64), spec.width,
                                       self._ground, lines, self._wet_at, self.__dict__.get("depth", {}).get(k))
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
                infra = [self.infra_fn(k) for k in mapcells.cells_for_points(la, lo)]
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
        return [b for b in allb if box_owns(box, (b[0][0] + b[1][0]) / 2, (b[0][1] + b[1][1]) / 2)]

    def piers_in(self, box):
        """The piers (pier_sites) of the parts solved so far standing in box (x0, y0, x1, y1): x0 <= x < x1, y0 <= y
        < y1 (each in one tile)."""
        x0, y0, x1, y1 = box
        with self.lock:
            allp = [p for v in self.piers.values() for p in v]
        return [p for p in allp if box_owns(box, p[0], p[1])]

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
    END_JOINS = False

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
        self._drowned = {}          # part id (fid.j) -> its chain has a part under water (drowned)

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

    def _mark_drowned(self, comp, pool):
        """Mark every part of the chain comp drowned when one of its parts is under water for more than
        TUNNEL_WET_SHARE of its points: an underwater tunnel is left out whole. Overture splits one into pieces (the
        Burnley Tunnel, Melbourne: the piece under the Yarra was left out, the dry piece from Batman Avenue to the
        bank was built, an open trench across the avenue with its mouth at the river; GitHub #128)."""
        if self.wet is None:
            return
        wet = False
        gfn = self._raw_ground if self.raw is not None else self._ground
        low = None                      # (the lowest ground at the parts' ends: the lower portal's)
        for k in comp:
            xy = np.asarray(pool[k][1], np.float64)
            if len(xy) >= 2:
                ge = np.asarray(gfn(xy[[0, -1]]), np.float64)
                if np.isfinite(ge).any():
                    low = float(np.nanmin(ge)) if low is None else min(low, float(np.nanmin(ge)))
        for k in comp:
            xy = np.asarray(pool[k][1], np.float64)
            if len(xy) < 2:
                continue
            la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
            w = np.asarray(self.wet(la, lo), np.float64) > 0.5
            if w.any() and low is not None:
                # (water far above the portals is a mountain lake over a deep tunnel, not a river over a shallow one:
                # one pond 900 m over the Gotthard road tunnel left all 17 km of it out, 2026-10-28)
                g = np.asarray(gfn(xy), np.float64)
                w &= ~(g - low > TUNNEL_WET_COVER_M)
            if w.mean() > TUNNEL_WET_SHARE or wet_run_inside(xy, w) >= TUNNEL_WET_RUN_M:
                wet = True
                break
        with self.lock:
            for k in comp:
                self._drowned[self._part_id(k)] = wet

    def drowned(self, key, ring):
        """Is tunnel part key (fid, j) (ring: its lon/lat points) in a chain with a part under water
        (_mark_drowned)? The grader asks the same (grading.Grader._tunnel_sections), so the ground under a left-out
        tunnel is not cut for it either."""
        pid = self._part_id(key)
        with self.lock:
            got = self._drowned.get(pid)
        if got is not None or self.wet is None:
            return bool(got)
        pool, ground, loaded = {}, {}, set()
        for p in (ring[0], ring[-1]):
            ck = mapcells.cell_of(float(p[1]), float(p[0]))
            if str(ck) not in loaded:
                loaded.add(str(ck))
                self._load(ck, pool, ground)
        if key not in pool:
            return False
        self._mark_drowned(self._resolve(key, pool, ground, loaded), pool)
        with self.lock:
            return bool(self._drowned.get(pid))

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
        self._mark_drowned(comp, pool)
        gfn = self._raw_ground if self.raw is not None else self._ground
        out = []
        self._tls.staged = {}           # (committed by parts() right after, the rest dropped with the next solve)
        nk = _EndJoin()                 # (the parts' ends another part of the chain goes on from: no portal there)
        jends = {k: (nk(np.asarray(pool[k][1])[0]), nk(np.asarray(pool[k][1])[-1])) for k in comp
                 if len(pool[k][1]) >= 2}
        jdeg = {}
        for a, b in jends.values():
            jdeg[a] = jdeg.get(a, 0) + 1
            jdeg[b] = jdeg.get(b, 0) + 1
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
            kinds = portal_cover(kinds, g - floor, pool[k][0])
            kinds = self._cover_roads(xy, s, kinds, ground)
            secs, i = [], 0
            while i < len(kinds):
                j = i
                while j + 1 < len(kinds) and kinds[j + 1] == kinds[i]:
                    j += 1
                secs.append((i, j, int(kinds[i])))
                i = j + 1
            # neighbouring sections share their end sample (no gap between a portal and the open road)
            secs = [(a if n == 0 else a - 1, b, kd) for n, (a, b, kd) in enumerate(secs)]
            floor = self._observed_floor(k, pool, xy, s, floor, secs,
                                         tuple(jdeg.get(e, 0) > 1 for e in jends.get(k, (None, None))))
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
                    low_h = MAJOR_LOW_H if pool[k][0] is not None and pool[k][0].cls in CONCRETE_WALL_CLASSES \
                        else TUNNEL_LOW_H         # (a motorway's box clears a truck, its top in a mound if need be)
                    th = float(np.clip(base - TUNNEL_SOIL_M, low_h, roadmesh.TUNNEL_H))
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

    def _observed_floor(self, k, pool, xy, s, floor, secs, joints=(False, False)):
        """floor with each covered section (kind 1 / 2) at the measured height where a source has one (roadz.py,
        level < 0: BD TOPO, NVDB) and it is deeper: never over the ground's lower hull (the cover stays); at a portal
        (next to an open section, or the part's end where no other part of the chain goes on: joints (start, end))
        at the hull's (the portals, the open sections and the grader's mouth plans keep theirs), eased in as the decks
        are (earthfast.deck_blend); at a joint inside the hill as measured (the next part reads the same source).
        Pure given the provider's cached data."""
        import roadz
        prov = roadz.PROVIDER
        if prov is None or not prov.enabled or not any(kd for _a, _b, kd in secs):
            return floor
        try:
            spec = pool[k][0]
            nm = pool[k][5] if len(pool[k]) > 5 else ""
            z = prov.observed(self.frame, xy, spec.width / 2, -1, "" if nm in ("None", "nan") else nm)
            if z is None:
                return floor
            import fast
            la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
            z = np.asarray(z, np.float64)
            zf = np.where(np.isfinite(z), z, 0.0)
            zg = np.where(np.isfinite(z), np.asarray(self.frame.geodetic_to_enu(la, lo, zf)[2], np.float64), np.nan)
            out = np.array(floor, np.float64)
            last = len(s) - 1
            for n, (a, b, kd) in enumerate(secs):
                seg = zg[a:b + 1]
                if not kd or b - a < 2 or np.isfinite(seg).mean() < roadz.MIN_COVER:
                    continue
                ok = np.isfinite(seg)
                ss = s[a:b + 1]
                filled = np.interp(ss, ss[ok], seg[ok])
                inside0 = a == 0 and joints[0]
                inside1 = b == last and joints[1]
                if n > 0 and secs[n - 1][2]:            # (another covered section before it: no portal between)
                    inside0 = True
                if n + 1 < len(secs) and secs[n + 1][2]:
                    inside1 = True
                eased = np.asarray(fast._ef.deck_blend(ss - ss[0], np.ascontiguousarray(filled),
                                                       float("nan") if inside0 else float(out[a]),
                                                       float("nan") if inside1 else float(out[b]),
                                                       OBS_BLEND_DG, OBS_BLEND_MIN_M, 0.5), np.float64)
                out[a:b + 1] = np.minimum(eased, out[a:b + 1])
            return out
        except Exception as ex:             # the hull rather than no tunnel
            log.debug("observed tunnel floor %s: %s: %s", k, type(ex).__name__, ex)
            return floor

    def _load(self, key, pool, ground):
        super()._load(key, pool, ground)
        done = self.__dict__.setdefault("hw_done", set())
        if str(key) in done:            # (the same half widths each time: once per cell)
            return
        done.add(str(key))
        feats = self.cell_fn(key)
        if feats is None or len(feats) == 0:
            return
        rs = road_specs(feats)
        for i in range(len(feats)):
            if rs[i] is not None:
                self._hw[(str(feats.ids[i]), "g")] = rs[i].width / 2

    def _cover_roads(self, xy, s, kinds, ground):
        """kinds with each covered section carried on until the ground roads crossing over it are covered (their
        carriageway and TUNNEL_ROAD_CLEAR_M past it): earthfast.tunnel_cover_roads (GitHub #450)."""
        lines, halves = [], []
        for gk, lns in (ground or {}).items():
            hw = self._hw.get(gk)
            if hw is None:
                continue
            for ln in lns:
                ln = np.asarray(ln, np.float64).reshape(-1, 2)
                if len(ln) >= 2:
                    lines.append(ln)
                    halves.append(hw)
        return cover_roads(xy, s, kinds, lines, halves)

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
        return self._open_of(super().parts(cells_feats, box))

    def _open_of(self, whole):
        with self.lock:
            opened = set(self.open)
        # (an open section under water is an underwater tunnel with no ground over it: the Sydney Harbour Tunnel
        # was drawn as a road across the harbour, 2026-10-18)
        return self._dry([p for p in self._split(whole) if p[0] in opened])

    def parts(self, cells_feats, box):
        return self._closed_of(super().parts(cells_feats, box))

    def _closed_of(self, whole):
        out = self._split(whole)
        with self.lock:
            opened = set(self.open)
        return self._dry([p for p in out if p[0] not in opened])

    def parts_todo(self, todo, solved_only=False, open_=False):
        """BridgeProfiles.parts_todo split into sections: the closed ones (parts), or the open ones (open_)."""
        whole = super().parts_todo(todo, solved_only)
        return self._open_of(whole) if open_ else self._closed_of(whole)

    def _adopt_extra(self, comp):
        pids = {self._part_id(k) for k in comp}

        def mine(d):
            return {k: v for k, v in d.items() if str(k).split("#")[0] in pids}
        return {"sections": mine(self.sections), "heights": mine(self.heights), "ups": mine(self.ups),
                "caps": mine(self.caps), "mouths": mine(self.mouths), "open": {o for o in self.open
                                                                               if o.split("#")[0] in pids},
                "drowned": {k: v for k, v in self._drowned.items() if k in pids}}

    def _adopt_take(self, extra):
        if not extra:
            return
        for name in ("sections", "heights", "ups", "caps", "mouths"):
            for k, v in extra[name].items():
                getattr(self, name).setdefault(k, v)
        self.open.update(extra["open"])
        for k, v in extra["drowned"].items():
            self._drowned.setdefault(k, v)

    def _dry(self, out):
        """The parts not under water for more than TUNNEL_WET_SHARE of their points, nor in a chain with one."""
        if self.wet is None or not out:
            return out
        keep = []
        for p in out:
            with self.lock:
                if self._drowned.get(str(p[0]).split("#")[0]):      # (its chain is under water: _mark_drowned)
                    continue
            xy = np.asarray(p[2], np.float64)
            la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
            # under water for a good part of it (a portal on a river bank, Fort Pitt's east end 0.6 m "in" the
            # Monongahela, is not: that dropped one of its two tubes, 2026-10-17)
            if (np.asarray(self.wet(la, lo)) > 0.5).mean() <= TUNNEL_WET_SHARE:
                keep.append(p)
        return keep


class ChainStore:
    """Bridge (or tunnel) chain profiles shared by every anchor of a process (geotiles, 2026-10-29): one
    BridgeProfiles / TunnelProfiles per geotiles.CHAIN_Z region in that region's frame (a WorldFrame: nearly level
    over a chain), a part asked for in the store of its first point's region, a chain solved in the store of its first
    part's (owner_of): one deck profile per chain, whatever the anchor or the store a tile asks. cell_fn, elev (the
    physics ground: anchor-free, as grading's heights are) as BridgeProfiles'; setup(profiles) sets wet, infra_fn,
    raw, ends on each store made. Anchors see it through a ChainView."""

    KEPT = 12           # stores kept (the farthest regions behind go first)

    def __init__(self, cls, cell_fn, elev, setup=None):
        self.cls, self.cell_fn, self.elev, self.setup = cls, cell_fn, elev, setup
        self.stores = OrderedDict()     # region -> profiles
        self.lock = threading.Lock()

    def store(self, region):
        region = (int(region[0]), int(region[1]))
        with self.lock:
            got = self.stores.get(region)
            if got is not None:
                self.stores.move_to_end(region)
                return got
            import geotiles
            got = self.cls(geotiles.frame(region[0], region[1], geotiles.CHAIN_Z), self.cell_fn, self.elev)
            got.owner_of = self._owner
            got.region = region
            if self.setup is not None:
                self.setup(got)
            self.stores[region] = got
            while len(self.stores) > self.KEPT:
                self.stores.popitem(last=False)
            return got

    def store_at(self, lat, lon):
        import geotiles
        return self.store(geotiles.tile_of(float(lat), float(lon), geotiles.CHAIN_Z))

    def _owner(self, prof, comp, pool):
        """The store whose region holds chain comp's first part's start."""
        first = min(comp)
        (la, lo), _ = prof._ends_ll(pool[first][1])
        return self.store_at(la, lo)


class _Mapper:
    """Points of one frame's world in another's (geotiles.world_map), rigid parts by the rotation between them."""

    def __init__(self, src, dst):
        import geotiles
        self.src, self.dst, self.same = src, dst, src.lat0 == dst.lat0 and src.lon0 == dst.lon0
        self._gt = geotiles
        if not self.same:
            r, _t, _c = geotiles.placement(src, dst, (0.0, 0.0, 0.0))
            self.r = r

    def pts(self, p):
        p = np.asarray(p, np.float64)
        if self.same or not len(p):
            return p.copy()
        return self._gt.world_map(self.src, self.dst, p)

    def dir2(self, ux, uy):
        if self.same:
            return ux, uy
        v = self.r[:2, :2] @ np.array([ux, uy])
        n = max(float(np.hypot(*v)), 1e-12)
        return float(v[0] / n), float(v[1] / n)

    def pier(self, q):
        if self.same:
            return q
        x, y, ux, uy, zt, zb = q[:6]
        a = self.pts(np.array([[x, y, zt], [x, y, zb]], np.float64))
        ux, uy = self.dir2(ux, uy)
        return (float(a[0, 0]), float(a[0, 1]), ux, uy, float(a[0, 2]), float(a[1, 2])) + tuple(q[6:])

    def beam(self, b):
        if self.same:
            return b
        a = self.pts(np.array([np.asarray(b[0], np.float64)[:3], np.asarray(b[1], np.float64)[:3]]))
        return (a[0].copy(), a[1].copy()) + tuple(b[2:])


class ChainView:
    """An anchor's view of a ChainStore, with BridgeProfiles' / TunnelProfiles' read side (parts, open_parts,
    piers_in, beams_in, depth_of, _ground, heights, mouths, height, portal_ups, cap_m, mouth): the parts asked for in
    their regions' stores, their geometry mapped exactly into this frame's world (kept per part). prefer: another
    ChainStore whose chains, where solved already, are taken first (the AI graph and the far bridges take the road
    tiles' decks: one profile per chain)."""

    def __init__(self, cs, frame, prefer=None):
        self.cs, self.frame, self.prefer = cs, frame, prefer
        self.elev = cs.elev
        self.lock = threading.Lock()
        self._mapped = {}           # (store region, pid, id(xy)) -> (xy ref, mapped part)
        self._piers = {}            # store region -> (version, mapped piers, mapped beams)
        self.specs_of, self.parts_of = cs.cls.specs_of, cs.cls.parts_of

    mapper = staticmethod(lambda src, dst: _Mapper(src, dst))

    def __getattr__(self, name):        # wet, infra_fn, raw, ends: the stores' settings (setup)
        if name in ("wet", "infra_fn", "raw", "ends"):
            st = list(self.cs.stores.values())
            return getattr(st[0], name, None) if st else None
        raise AttributeError(name)

    def _todo(self, cells_feats, box):
        """{store: [((fid, j), feats, i)]} of the parts in these cells that come near box (this frame)."""
        x0, y0, x1, y1 = box
        by, queued = {}, set()
        for feats in cells_feats:
            if feats is None or len(feats) == 0:
                continue
            bs = self.specs_of(feats)
            for i in range(len(feats)):
                if bs[i] is None:
                    continue
                fid = str(feats.ids[i])
                for j, ring in enumerate(self.parts_of(feats, i)):
                    if (fid, j) in queued:
                        continue
                    e, n = mapgeom.enu_xy(feats, self.frame, ring)
                    if e.max() < x0 - NEAR_M or e.min() > x1 + NEAR_M or n.max() < y0 - NEAR_M or n.min() > y1 + NEAR_M:
                        continue
                    queued.add((fid, j))
                    st = self.cs.store_at(float(ring[0][1]), float(ring[0][0]))
                    by.setdefault(st.region, (st, []))[1].append(((fid, j), feats, i))
        return by

    def _map(self, st, got):
        out = []
        mp = None
        for pid, spec, xy, z, lon in got:
            k = (st.region, pid, id(xy))
            with self.lock:
                hit = self._mapped.get(k)
            if hit is None or hit[0] is not xy:
                mp = mp or _Mapper(st.frame, self.frame)
                xyz = mp.pts(np.c_[np.asarray(xy, np.float64)[:, :2], np.asarray(z, np.float64)])
                hit = (xy, (pid, spec, xyz[:, :2].copy(), xyz[:, 2].copy(), lon))
                with self.lock:
                    if len(self._mapped) > 20000:
                        self._mapped.clear()
                    self._mapped[k] = hit
            out.append(hit[1])
        return out

    def _parts(self, cells_feats, box, **kw):
        out = []
        for _r, (st, todo) in sorted(self._todo(cells_feats, box).items()):
            if self.prefer is not None:
                pst = self.prefer.store(st.region)
                with pst.lock:
                    solved = [t for t in todo if t[0] in pst.done]
                out += self._map(pst, pst.parts_todo(solved, solved_only=True, **kw))
                todo = [t for t in todo if t not in solved]
            out += self._map(st, st.parts_todo(todo, **kw))
        return out

    def parts(self, cells_feats, box):
        return self._parts(cells_feats, box)

    def open_parts(self, cells_feats, box):
        return self._parts(cells_feats, box, open_=True)

    def _stores(self):
        out = list(self.cs.stores.values())
        if self.prefer is not None:
            out += list(self.prefer.stores.values())
        return out

    def _mapped_piers(self, st):
        with st.lock:
            ver = (len(st.piers), sum(len(v) for v in st.piers.values()), len(st.trusses),
                   sum(len(v) for v in st.trusses.values()))
            piers = [q for v in st.piers.values() for q in v]
            beams = [b for v in st.trusses.values() for b in v]
        key = (id(st), st.region)
        with self.lock:
            hit = self._piers.get(key)
        if hit is None or hit[0] != ver:
            mp = _Mapper(st.frame, self.frame)
            hit = (ver, [mp.pier(q) for q in piers], [mp.beam(b) for b in beams])
            with self.lock:
                self._piers[key] = hit
        return hit[1], hit[2]

    def piers_in(self, box):
        x0, y0, x1, y1 = box
        out, seen = [], set()
        for st in self._stores():
            for q in self._mapped_piers(st)[0]:
                k = (round(q[0], 3), round(q[1], 3))
                if box_owns(box, q[0], q[1]) and k not in seen:
                    seen.add(k)
                    out.append(q)
        return out

    def beams_in(self, box):
        x0, y0, x1, y1 = box
        out, seen = [], set()
        for st in self._stores():
            for b in self._mapped_piers(st)[1]:
                mx, my = (b[0][0] + b[1][0]) / 2, (b[0][1] + b[1][1]) / 2
                k = (round(float(mx), 3), round(float(my), 3), round(float(b[0][2]), 3))
                if box_owns(box, mx, my) and k not in seen:
                    seen.add(k)
                    out.append(b)
        return out

    def depth_of(self, fid):
        ds = []
        for st in self._stores():
            with st.lock:
                ds += [d for k, d in st.__dict__.get("depth", {}).items() if st._part_id(k) == fid and d is not None]
        return max(ds) if ds else DECK_FULL_DEPTH

    def _ground(self, xy):
        xy = np.asarray(xy, np.float64)
        la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return self.frame.geodetic_to_enu(la, lo, np.asarray(self.elev(la, lo), np.float64))[2]

    def _merged(self, name):
        out = {}
        for st in self._stores():
            with st.lock:
                for k, v in getattr(st, name, {}).items():
                    out.setdefault(k, v)
        return out

    heights = property(lambda self: self._merged("heights"))
    mouths = property(lambda self: self._merged("mouths"))

    def _sub(self, method, fid, default):
        for st in self._stores():
            with st.lock:
                if any(fid in getattr(st, d, {}) for d in ("heights", "ups", "caps", "mouths")):
                    break
        else:
            st = None
        if st is None:
            return default
        return getattr(st, method)(fid)

    def height(self, fid):
        return self._sub("height", fid, roadmesh.TUNNEL_H)

    def portal_ups(self, fid):
        return self._sub("portal_ups", fid, [roadmesh.PORTAL_FRAME, roadmesh.PORTAL_FRAME])

    def cap_m(self, fid):
        return self._sub("cap_m", fid, (None, None))

    def mouth(self, fid):
        return self._sub("mouth", fid, (True, True))


def wet_run_inside(xy, wet):
    """The longest run (m) of wet points along the line xy that touches neither end: a tunnel under a river (the
    Burnley Tunnel's 70 m under the Yarra, 2 % of its 3.2 km, GitHub #128), not one whose portal is on a bank (Fort
    Pitt's east end 0.6 m "in" the Monongahela). earthfast.wet_run_inside (Rust)."""
    import fast
    fast.require(48, "wet_run_inside")
    return float(fast._ef.wet_run_inside(np.ascontiguousarray(xy, np.float64), np.ascontiguousarray(wet, bool)))


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
    fast.require(88, "tunnel_holes")
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
                                  float(TUNNEL_HOLE_MARGIN), float(LOW_HOLE_M + LOW_HOLE_STEP), 0.0, True)
            mask |= mouth_mask
        # the Rust kernel: the samples over the part between its floor and vault (a low arch: its mouths only)
        m0, m1 = (mouths or {}).get(_fid, (True, True))
        # (a low arch: the squares from its flat mouth band, LOW_HOLE_M, up to the soil over its vault)
        fast._ef.tunnel_holes(mask, u, float(x0), float(y0), float(square), np.ascontiguousarray(xy),
                              np.ascontiguousarray(z), float(hw), float(th), bool(low), bool(m0), bool(m1),
                              float(TUNNEL_HOLE_MARGIN), float(LOW_HOLE_M if low else LOW_HOLE_M + LOW_HOLE_STEP),
                              float(ARCH_SIDE_M + 1.0), not low)
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
    keys = mapcells.cells_for_points(lat, lon)
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
    mask = tunnel_hole_mask(parts, u, cx, cy, square, getattr(tunnels, "heights", None),
                            getattr(tunnels, "mouths", None))
    if mask is not None and mask.any():
        mask = spare_carriageways(mask, parts, cells, frame, cx, cy, square)
    return mask


HOLE_ROAD_MARGIN = 0.3      # m past another road's carriageway no tunnel hole is cut (spare_carriageways)
HOLE_JOIN_M = 3.0           # m: a road with an end this near a tunnel part's end is its approach (its mouth cut)


def spare_carriageways(mask, parts, cells, frame, cx, cy, square):
    """mask with the hole samples inside the carriageway of a ground road that is not the tunnel's (another feature, not
    an approach joined at a mouth) filled again: a portal's opening reaching into a road beside or over it left a hole in
    its lanes (GitHub #450, the A-7 near Málaga, a 2 m drop at the edge of a carriageway beside an underpass's mouth)."""
    import fast
    own = {str(p[0]).split("#")[0] for p in parts}
    ends = np.array([np.asarray(p[2], np.float64)[k, :2] for p in parts for k in (0, -1) if len(p[2])], np.float64)
    others = []
    for feats in cells:
        if feats is None or len(feats) == 0:
            continue
        rs = road_specs(feats)
        for i, sp in enumerate(rs):
            fid = str(feats.ids[i])
            if sp is None or sp.kind not in BUILT or fid in own:
                continue
            for ring in ground_parts(feats, i):
                e, n = mapgeom.enu_xy(feats, frame, ring)
                xy = np.c_[e, n]
                if len(xy) < 2:
                    continue
                if len(ends) and min(float(np.min(np.hypot(*(ends - xy[0]).T))),
                                     float(np.min(np.hypot(*(ends - xy[-1]).T)))) < HOLE_JOIN_M:
                    continue            # (its approach: the mouth is cut into it)
                others.append((fid, sp.width / 2, xy))
    if not others:
        return mask
    oth = fast.Others(others)
    n = mask.shape[0]
    x0, y0 = cx - n * square / 2, cy - n * square / 2
    out = mask.copy()
    for iy, ix in zip(*np.nonzero(mask)):
        if not oth.clear((x0 + ix * square, y0 + iy * square), None, HOLE_ROAD_MARGIN):
            out[iy, ix] = False
    return out


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
# A wall stands only where the graded ground behind its face climbs at least this steeply (m per m, its median over
# the wall; across WALL_STEEP_SPAN from 1 m in front of the face): the step the grading cut. A natural bank the
# terrain already draws as a slope is no cutting: a 1.5 m concrete wall stood in front of a low eroded bank beside
# US-163 in Utah (its ground rose 0.4-1.2 m per m; Munich's McGraw-Graben's cut walls 5-9, measured 2026-10-29)
WALL_STEEP = 2.0
WALL_STEEP_SPAN = (-1.0, 6.0)


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
    within its half width + WALL_DECK_MARGIN_M of its nearest segment and the deck WALL_DECK_OVER_M or more over them
    (earthfast.under_decks). Pure."""
    import fast
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    pack = _deck_pack(decks)
    if pack is None or not len(xy):
        return np.zeros(len(xy), bool)
    fast.require(74, "under_decks")
    return np.asarray(fast._ef.under_decks(np.ascontiguousarray(xy), np.ascontiguousarray(np.asarray(z, np.float64)),
                                           *pack, float(WALL_DECK_OVER_M)), bool)


_DECK_PACK = [(None, None)]     # [(the decks list, its packed arrays)]: one tile's walls ask with the same list


def _deck_pack(decks):
    """(points (n, 3), starts (C + 1,), reach (C,)) of the deck chunks with 2 points or more, or None."""
    if not decks:
        return None
    last = _DECK_PACK[0]
    if last[0] is decks:
        return last[1]
    ns = [np.asarray(ch["n"], np.float64)[:, :3] for ch in decks]
    keep = [k for k, n in enumerate(ns) if len(n) >= 2]
    pack = None
    if keep:
        pts = np.ascontiguousarray(np.concatenate([ns[k] for k in keep]))
        starts = np.r_[0, np.cumsum([len(ns[k]) for k in keep])].astype(np.int64)
        reach = np.array([float(decks[k].get("w") or 0) / 2 + WALL_DECK_MARGIN_M for k in keep], np.float64)
        pack = (pts, starts, reach)
    _DECK_PACK[0] = (decks, pack)
    return pack


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


class _Segs:
    """others {fid: (centreline (m, 2), half width)} as one table of segments (a, b, half width, its road's fid), with
    each segment's box: the carriageway tests below in one Rust call each (one per road cost retaining_walls ~2 s of
    a Canton tile set, 2026-10-25)."""

    def __init__(self, others):
        rows = [(f, np.asarray(ln, np.float64), h) for f, (ln, h) in others.items() if len(ln) >= 2]
        f64 = np.float64
        self.a = np.concatenate([ln[:-1, :2] for _f, ln, _h in rows]) if rows else np.zeros((0, 2), f64)
        self.b = np.concatenate([ln[1:, :2] for _f, ln, _h in rows]) if rows else np.zeros((0, 2), f64)
        self.h = np.concatenate([np.full(len(ln) - 1, h, f64) for _f, ln, h in rows]) if rows else np.zeros(0, f64)
        self.road = np.concatenate([np.full(len(ln) - 1, n) for n, (_f, ln, _h) in enumerate(rows)]) if rows else \
            np.zeros(0, np.int64)
        self.fids = [f for f, _ln, _h in rows]
        self.lo = np.minimum(self.a, self.b)
        self.hi = np.maximum(self.a, self.b)

    def near(self, pts, gap, skip=None):
        """Which points (k, 2) lie within a segment's half width + gap (its road's fid not skip). Pure."""
        import fast
        pts = np.ascontiguousarray(np.asarray(pts, np.float64))
        if not len(pts) or not len(self.h):
            return np.zeros(len(pts), bool)
        lo, hi = pts.min(0), pts.max(0)
        r = self.h + gap
        keep = (self.hi[:, 0] + r >= lo[0]) & (self.hi[:, 1] + r >= lo[1]) & (self.lo[:, 0] - r <= hi[0]) & \
            (self.lo[:, 1] - r <= hi[1])
        if skip is not None and skip in self.fids:
            keep &= self.road != self.fids.index(skip)
        if not keep.any():
            return np.zeros(len(pts), bool)
        fast.require(0, "points_near_segments")
        return np.asarray(fast._ef.points_near_segments(pts, np.ascontiguousarray(self.a[keep]),
                                                        np.ascontiguousarray(self.b[keep]),
                                                        np.ascontiguousarray(r[keep])), bool)


def _on_carriageways(pts, others, gap):
    """Which points (k, 2) lie on a carriageway of others {fid: (centreline (m, 2), half width)} or a _Segs of them
    (+ gap). Pure."""
    segs = others if isinstance(others, _Segs) else _Segs(others)
    return segs.near(pts, gap)


def _clear_depth(base, out, depth, others, own):
    """Per point of a wall's face line base (k, 2): how far (up to depth (k,)) out from it along out (k, 2) stays off
    every other road's carriageway in others {fid: (centreline (m, 2), half width)} or a _Segs of them (less
    WALL_OTHER_GAP_M). Pure."""
    segs = others if isinstance(others, _Segs) else _Segs(others)
    steps = np.arange(0.5, float(np.max(depth)) + 0.51, 0.5)
    q = (base[:, None, :] + out[:, None, :] * steps[None, :, None]).reshape(-1, 2)
    # (its own road too: a hairpin's other leg; the face stands past its own carriageway, so the stretch beside it
    # never hits)
    hit = segs.near(q, WALL_OTHER_GAP_M).reshape(len(base), len(steps))
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
    near = np.flatnonzero((steps >= rh + 0.5) & (steps <= rh + PARAPET_PROBE_M))
    if not len(near):
        return []
    drop = (z[:, None] - gs[:, near]).max(axis=1) > PARAPET_DROP_M
    base = xy + out_n * (rh + PARAPET_OFF_M)
    ok = drop & inside & ~_under_decks(base, z, decks) & ~_on_carriageways(base, others, 0.0)
    # not across a road joining it (its junctions), nor where another road runs beside
    if ok.any():
        segs = others if isinstance(others, _Segs) else _Segs(others)
        ok &= ~segs.near(base, PARAPET_ROAD_GAP_M, skip=own)
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


def _cut_behind(base, out_n, z, ground):
    """Whether the graded ground behind a wall's face line base (k, 2) (out_n: unit vectors away from the road; z: the
    road's heights) climbs as steeply as a cutting does (WALL_STEEP over 1 m, its median along the wall)."""
    offs = np.arange(WALL_STEEP_SPAN[0], WALL_STEEP_SPAN[1] + 0.01, 0.5)
    P = (base[:, None, :] + out_n[:, None, :] * offs[None, :, None]).reshape(-1, 2)
    g = np.asarray(ground(P), np.float64).reshape(len(base), len(offs))
    rise = (g[:, 2:] - g[:, :-2]).max(axis=1)           # (over 1 m: two 0.5 m steps)
    return bool(np.median(rise) >= WALL_STEEP)


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
            info.append((xy, z, nl * side, core, inside, width / 2, steps, spec.cls in CONCRETE_WALL_CLASSES,
                         fid, not spec.urban, not spec.urban and spec.cls not in NO_PARAPET_CLASSES and not spec.median,
                         grading.wall_max_h(spec)))
    if not pts_all:
        return []

    def ground(P):
        la, lo, _ = frame.enu_to_geodetic(P[:, 0], P[:, 1], np.zeros(len(P)))
        return np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2], np.float64)
    g = ground(np.concatenate(pts_all))
    # the graded ground at the steps out from the road only where a wall or a parapet can stand: a side whose probe
    # finds no cutting (_wall_runs) and that gets no parapets has no use for them (most of a town's streets: 8 of 9 of
    # the ground lookups, 2026-10-25)
    probes, k = [], 0
    for xy, z, out_n, core, inside, rh, steps, concrete, fid, rural, parapets, max_h in info:
        gg = g[k:k + len(xy)]
        k += len(xy)
        need = len(steps) and (parapets or _wall_runs(gg - z).any())
        probes.append((gg, need))
    sp = []
    for (xy, z, out_n, core, inside, rh, steps, *_r), (gg, need) in zip(info, probes):
        if need:
            sp.extend(xy + out_n * o for o in steps)
    gsteps = ground(np.concatenate(sp)) if sp else np.zeros(0)
    out, k = [], 0
    others = {fid: (np.asarray(pts, np.float64)[:, :2], spec.width / 2) for fid, spec, pts, _ in roads if len(pts) >= 2}
    others = _Segs(others)          # (one table for all the carriageway tests below)
    bend_of = {}
    for (xy, z, out_n, core, inside, rh, steps, concrete, fid, rural, parapets, max_h), (gg, need) in zip(info, probes):
        key = (id(xy), len(xy))
        if key not in bend_of:          # (a tight bend: no wall into it, grading.bend_zone)
            bend_of[key] = grading.bend_zone(xy)
        bends = bend_of[key]
        face = np.full(len(xy), core + 0.1)
        thick = np.full(len(xy), roadmesh.WALL_THICK_M)
        top_g = gg
        if need:                    # the wall where the graded ground starts to climb (a sharp cutting: before the core's edge)
            gs = gsteps[k:k + len(xy) * len(steps)].reshape(len(steps), len(xy)).T
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
        if parapets and need:
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
                if not _cut_behind(base, out_n[sl], z[sl], ground):        # (a natural slope: no wall, WALL_STEEP)
                    i = j + 1
                    continue
                # (never over another road: a hairpin's wall top covered the road above, the Stelvio Pass, the user's
                # report 2026-10-21)
                th = np.minimum(th, _clear_depth(base, out_n[sl], th, others, fid))
                tp = np.minimum(top_g[sl], z[sl] + max_h) + WALL_TOP_M      # (no taller than that)
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


def _tunnel_chunks(tunnels, cells, exact, terrain_paint=True):
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
            for look, w, line in [(look_of(spec), spec.width, pts)] + [
                    (lk, w2, ln) for lk, w2, ln in extras(spec, pts, lon)
                    if lk["m"] not in (SIDEWALK["m"], GUTTER["m"], EDGE_DECAL["m"])]:
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
            jobs.append((parts[i][0], (mid[0, :2], mid[-1, :2]), [({**TUNNEL, "tunnel": True, "tube_only": True}, width, mid)],
                         parts[i][1]))
    for n, (fid, spec, xy, z, lon) in enumerate(parts):
        pts = np.c_[xy, z]
        look = _deck_look(spec) if not terrain_surface(spec) else None
        if terrain_paint and terrain_surface(spec):
            look = None                     # painted in the ground layer, as any other ground part
        lines = [] if look is None else [({**look, "deck": True, "tunnel": True, "no_tube": n in twinned}, spec.width, pts)]
        for look, w, line in extras(spec, pts, lon):
            if look["m"] not in (SIDEWALK["m"], GUTTER["m"], EDGE_DECAL["m"]):     # (no ground beside a deck)
                lines.append((look, w, line))
        jobs.append((fid, (pts[0, :2], pts[-1, :2]), lines, spec))
    for fid, ends, lines, spec in jobs:
        length = max((float(np.hypot(*np.diff(np.asarray(ln)[:, :2], axis=0).T).sum()) for _, _, ln in lines
                      if len(ln) >= 2), default=0.0)
        for look, w, line in lines:
            for piece in clip_polyline(line, *exact):
                for c in split(densify(simplify(piece, ztol=SIMPLIFY_Z_M))):
                    if len(c) >= 2:
                        ch = _chunk(look, w, c)
                        ch["fixed"] = True
                        if look.get("tunnel"):
                            ch["tunnel"] = True
                            ch["deck"] = not look.get("tube_only")      # (a twin's tube: no floor, no road surface)
                            ch["tunnel_len"] = round(length, 1)             # (the whole sub-part's: sound_boxes)
                            if spec.cls in CONCRETE_WALL_CLASSES:
                                ch["concrete"] = True   # (a major road's low arch: concrete, as its cutting's walls)
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


def _bridge_chunks(frame, bseen, elev, exact, bridges=None, cells=None):
    """The chunks of the bridges crossing the tile box `exact` (deck surfaces flagged "deck", their
    centre / edge lines; every bridge chunk "fixed": its z is the deck's, not the ground's). Deck
    heights: from `bridges` (BridgeProfiles: whole chains, shared by every tile), else from the
    chains found among the given cells (deck_heights; tests)."""
    if bridges is not None:
        got = [(fid, spec, np.c_[xy, z], lon) for fid, spec, xy, z, lon in bridges.parts(cells, exact)
               if np.isfinite(z).all()]
        dd = getattr(bridges, "depth_of", None)
        return _deck_chunks(got, exact, bridges._ground, {d[0]: dd(d[0]) for d in got} if dd else None)
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
    return _deck_chunks(decks, exact, ground)


DECK_CONTEXT_M = 24.0   # m around a tile its bridges' decks are passed as context (roadmesh.deck: whether an edge has
                        # another deck beside it; a parapet stood across the lanes at a tile seam, the deck beside it
                        # in the next tile, Queensboro Bridge 2026-10-17)


def _deck_look(spec):
    """A bridge deck's look: the concrete of a `surface=concrete` street (it stays concrete over the bridge), else
    asphalt — and asphalt for rock/grass surfaces, whose ground mask cannot be painted on a deck at all."""
    if not terrain_surface(spec) and spec.kind == "paved" and spec.surf == "concrete":
        return CONCRETE
    return DECK


def _corner_look(point, roads):
    """A junction's corner-fill polygon look: the asphalt of the paved streets meeting there, or the concrete of
    them when a concrete street is within CORNER_CONCRETE_M of the corner (its wedges would otherwise be asphalt
    wedges in a concrete junction)."""
    px, py = float(point[0]), float(point[1])
    for _fid, spec, pts, _lon in roads:
        if spec.kind == "paved" and spec.surf == "concrete":
            if np.hypot(pts[:, 0] - px, pts[:, 1] - py).min() <= CORNER_CONCRETE_M:
                return CONCRETE
    return LOOK["paved"]


DECK_JOIN_M = 1.0       # m: deck parts' ends this close go on one from the other (no parapet end there)
ABUT_GROUND_M = 1.0     # a deck part's end within this of the ground under it is an abutment (roadmesh.deck: its
                        # parapets ramp down there to the approach)


def _abutments(pts, ground_fn):
    """[(x, y)] of the ends of a deck part (pts (k, 3)) that land on the ground (within ABUT_GROUND_M); [] without
    ground_fn. Never raises."""
    if ground_fn is None or len(pts) < 2:
        return []
    try:
        ends = np.asarray(pts, np.float64)[[0, -1]]
        g = np.asarray(ground_fn(ends[:, :2]), np.float64)
        return [(float(e[0]), float(e[1])) for e, gz in zip(ends, g) if e[2] - gz <= ABUT_GROUND_M]
    except Exception:
        return []


# Junction pads on bridges: where deck parts meet (with each other, or with a ground road going on from the joint), the
# convex hull of their carriageways within PAD_REACH_M of the joint is paved at their heights (roadmesh.deck draws it,
# colliding): the corner of a turn, the wedge where a ramp leaves, a wider deck's edge past a narrower street's. The map
# leaves those open, over whatever lies under the bridge: at North Charles Street over I-83 in Baltimore the AI cut the
# corner onto the station's road and fell 6 m into the motorway's cut, 2 of 2 runs (2026-10-29). None where a deck simply
# goes on straight from another part (BNE_DECK_PADS=off: none at all).
PADS_ON = os.environ.get("BNE_DECK_PADS", "on") != "off"
PAD_REACH_M = 12.0
PAD_STEP_M = 2.0
PAD_JOIN_M = 0.75           # m: ends this near are one joint (_skew_decks' tolerance)
PAD_GROUND_M = 1.0          # m: a ground road's end this near a joint goes on from it
PAD_STRAIGHT_COS = -0.97    # two parts leaving a joint this straight need none
# The pad reaches PAD_MARGIN_M past the carriageways (a turn cut across the corner stays that far from its rim); the
# decks' parapets stand nowhere inside it, and its rim has a railing where the ground under it lies PAD_WALL_DROP_M or
# more lower, except where a carriageway leaves it (within its half width + PAD_OPEN_M): the station road's parapet
# began at the joint, in the turn's path, and the AI clipped its end (Charles Street over I-83, 2026-10-29).
PAD_MARGIN_M = 2.5
PAD_OPEN_M = 0.75
PAD_WALL_DROP_M = 1.0
PAD_RIM_STEP_M = 1.0


def deck_pads(decks, ground_roads, box, ground_fn, own=True):
    """[(ring (k, 3), centre (3,), walls [(pts (m, 3), out (m, 2))])] of the junction pads of the joints of decks
    [(fid, spec, xy (k, 2), z (k,))] (the bridge parts round tile box, as bridges.parts gives them) whose joint the box
    owns (own False: every joint, the parapets' test round the tile); ground_roads [(fid, spec, pts (k, 2+), _)] the
    ground carriageways near it (build_tile roads_out), the ground's heights ground_fn(xy) (the decks' abutments').
    walls: the rim's railings (PAD_*). earthfast.deck_pad, deck_pad_rim. Pure given ground_fn."""
    if not PADS_ON or not decks:
        return []
    import fast
    fast.require(113, "deck_pad_rim")
    join = _EndJoin(PAD_JOIN_M)
    nodes, inc = [], {}
    for fid, spec, xy, z in decks:
        xy = np.asarray(xy, np.float64)[:, :2]
        z = np.asarray(z, np.float64)
        if len(xy) < 2 or not np.isfinite(z).all():
            continue
        for at in (0, -1):
            nd = join(xy[at])
            if nd == len(nodes):
                nodes.append(xy[at].copy())
            line, zz = (xy, z) if at == 0 else (xy[::-1], z[::-1])
            inc.setdefault(nd, []).append((line, zz, spec.width / 2))
    out = []
    gl = [(np.asarray(pts, np.float64)[:, :2], spec.width / 2) for _fid, spec, pts, _ in ground_roads
          if spec is not None and len(pts) >= 2]
    for nd, parts in sorted(inc.items()):
        q = nodes[nd]
        if own and not box_owns(box, float(q[0]), float(q[1])):
            continue
        parts = list(parts)
        for line, half in gl:
            for at in (0, -1):
                if np.hypot(*(line[at] - q)) < PAD_GROUND_M:
                    ln = line if at == 0 else line[::-1]
                    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(ln, axis=0).T))]
                    keep = s <= PAD_REACH_M + 2 * PAD_STEP_M
                    ln = ln[keep] if keep.sum() >= 2 else ln[:2]
                    parts.append((ln, np.asarray(ground_fn(ln), np.float64), half))
        if len(parts) < 2:
            continue
        if len(parts) == 2:
            d = []
            for line, _z, _h in parts:
                v = line[min(len(line) - 1, 1)] - line[0]
                d.append(v / max(np.hypot(*v), 1e-9))
            if float(np.dot(d[0], d[1])) < PAD_STRAIGHT_COS:
                continue                        # (a deck going on straight: its own end, an abutment, no corner)
        pts = np.ascontiguousarray(np.concatenate([p[0] for p in parts]), np.float64)
        zz = np.ascontiguousarray(np.concatenate([p[1] for p in parts]), np.float64)
        off = np.r_[0, np.cumsum([len(p[0]) for p in parts])].astype(np.int64)
        halves = np.array([p[2] for p in parts], np.float64)
        hull = np.asarray(fast._ef.deck_pad(pts, zz, off, halves + PAD_MARGIN_M, PAD_REACH_M, PAD_STEP_M), np.float64)
        if len(hull) < 3:
            continue
        zc = float(np.mean([p[1][0] for p in parts]))
        rim = np.asarray(fast._ef.deck_pad_rim(np.ascontiguousarray(hull), pts, off, halves, PAD_RIM_STEP_M,
                                               PAD_OPEN_M), np.float64)
        walls = []
        if len(rim):
            drop = rim[:, 2] - np.asarray(ground_fn(rim[:, :2]), np.float64)
            wall = (rim[:, 5] < 0.5) & (drop >= PAD_WALL_DROP_M)
            # runs round the ring (one closing over its start joined)
            k = len(rim)
            if wall.all():
                walls.append((np.r_[rim[:, :3], rim[:1, :3]], np.r_[rim[:, 3:5], rim[:1, 3:5]]))
            elif wall.any():
                start = int(np.flatnonzero(~wall)[0])
                order = [(start + i) % k for i in range(1, k + 1)]
                run = []
                for i in order:
                    if wall[i]:
                        run.append(i)
                    elif run:
                        if len(run) >= 2:
                            walls.append((rim[run, :3], rim[run, 3:5]))
                        run = []
                if len(run) >= 2:
                    walls.append((rim[run, :3], rim[run, 3:5]))
        out.append((hull, np.array([q[0], q[1], zc]), walls))
    return out


def pad_inside(pads, xy, shrink=0.3):
    """Per point of xy (n, 2): inside a pad of pads (deck_pads) by shrink m or more (its interior: no parapet). Pure."""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    out = np.zeros(len(xy), bool)
    for pad in pads:
        r = np.asarray(pad[0], np.float64)[:, :2]
        a, b = r, np.roll(r, -1, axis=0)
        e = b - a
        ln = np.maximum(np.hypot(*e.T), 1e-9)
        # signed distance inside every edge (counter-clockwise: left of each edge is inside)
        d = ((e[None, :, 0] * (xy[:, None, 1] - a[None, :, 1]) - e[None, :, 1] * (xy[:, None, 0] - a[None, :, 0]))
             / ln[None, :])
        out |= (d >= shrink).all(1)
    return out


def _deck_chunks(decks, exact, ground_fn=None, depths=None):
    """Deck + line chunks of bridge parts [(fid, spec, pts (k, 3), lon)] clipped to the box `exact`, and their decks
    within DECK_CONTEXT_M of it as "ctx" chunks (not drawn: build_tile's caller hands them to roadmesh.deck).
    ground_fn(xy) -> heights: a part's ends on the ground are its abutments (each deck chunk's "abut" points: the
    parapets ramp down there)."""
    out = []
    abut = {}
    ends = [(i, e, np.asarray(pts, np.float64)[e, :2]) for i, (_, _, pts, _) in enumerate(decks) if len(pts) >= 2
            for e in (0, -1)]
    for i, (fid, spec, pts, lon) in enumerate(decks):
        abut.setdefault(fid, []).extend(_abutments(pts, ground_fn))
        # an end no other deck part goes on from is an end of its parapets too (it stood there full height, square, on
        # the slab's edge: a short channel bridge's ends on the Sunshine Skyway causeway, 2026-10-27)
        for i2, e, q in ends:
            if i2 != i:
                continue
            if not any(j != i and np.hypot(*(q - q2)) < DECK_JOIN_M for j, _, q2 in ends):
                abut[fid].append((float(q[0]), float(q[1])))
    x0, y0, x1, y1 = exact
    wide = (x0 - DECK_CONTEXT_M, y0 - DECK_CONTEXT_M, x1 + DECK_CONTEXT_M, y1 + DECK_CONTEXT_M)
    for fid, spec, pts, lon in decks:
        for piece in clip_polyline(pts, *wide):
            for c in split(densify(simplify(piece, ztol=SIMPLIFY_Z_M))):
                if len(c) >= 2:
                    ch = _chunk({**_deck_look(spec), "deck": True}, spec.width, c)
                    ch.update(fixed=True, deck=True, fid=fid, ctx=True)
                    if depths and fid in depths:
                        ch["dd"] = round(float(depths[fid]), 3)
                    out.append(ch)
    for fid, spec, pts, lon in decks:
        lines = [({**_deck_look(spec), "deck": True}, spec.width, pts)]
        for look, w, line in extras(spec, pts, lon):
            if look["m"] not in (SIDEWALK["m"], GUTTER["m"], EDGE_DECAL["m"]):     # (no ground beside a deck)
                lines.append((look, w, line))
        for look, w, line in lines:
            for piece in clip_polyline(line, *exact):
                for c in split(densify(simplify(piece, ztol=SIMPLIFY_Z_M))):
                    if len(c) >= 2:
                        ch = _chunk(look, w, c)
                        ch["fixed"] = True
                        if look.get("deck"):
                            ch["deck"] = True
                            ch["fid"] = fid             # (roadmesh.deck: no parapet between parts side by side)
                            if depths and fid in depths:
                                ch["dd"] = round(float(depths[fid]), 3)     # (roadmesh.deck: its slab and girders)
                            if abut.get(fid):
                                ch["abut"] = abut[fid]
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


GOING_ON_DEG = 20.0     # a road whose end meets another's end turning by under this (the straightest there) goes on in it


def _going_on(roads):
    """{(fid, fid2)} (both ways) of the roads [(fid, spec, pts (k, 3), lon)] whose gravel edge decals need not keep
    EDGE_DECAL_GAP_M off each other's carriageway (meant for a ramp's gore, it left a ~10 m gap on the far side of every
    T junction, the user 2026-10-28, Carroll County MD): roads at one junction node, not both one-way (a merge's or a
    diverge's gore keeps it) and the second without sidewalks (its slab's end), and a road going on in another at a
    shared end (the map splits a road at every junction:
    the straightest of each other's there). Pure."""
    import junctions
    ends = {}                                   # node key -> [(fid, outward unit direction)]
    at_node = {}                                # node key -> {fid: one-way}
    for fid, sp, pts, _ in roads:
        if len(pts) < 2 or sp.kind not in BUILT:
            continue
        keys = np.round(np.asarray(pts[:, :2], np.float64) / junctions.NODE_TOL).astype(np.int64)
        for key in {(int(a), int(b)) for a, b in keys}:
            at_node.setdefault(key, {})[fid] = (bool(sp.oneway), has_sidewalks(sp))
        for a, b in ((0, 1), (-1, -2)):
            d = pts[a, :2] - pts[b, :2]
            n = math.hypot(d[0], d[1])
            if n > 1e-6:
                ends.setdefault((int(keys[a, 0]), int(keys[a, 1])), []).append((fid, d / n))
    out = set()
    for key in sorted(at_node):
        fids = at_node[key]
        if len(fids) >= 2:
            for f1 in fids:
                for f2 in fids:
                    if f1 != f2 and not (fids[f1][0] and fids[f2][0]) and not fids[f2][1]:
                        out.add((f1, f2))       # (f1's decal by f2: up to f2's curb when it has one)
    lim = -math.cos(math.radians(GOING_ON_DEG))
    for key in sorted(ends):
        at = ends[key]
        best = {}
        for fid, u in at:
            # the straightest other road ending here: going on is turning least (outward directions opposite)
            got = min(((float(u @ v), f2) for f2, v in at if f2 != fid), default=None)
            if got is not None and got[0] <= lim:
                best[fid] = got[1]
        for fid, f2 in best.items():
            if best.get(f2) == fid:             # each the other's (a ramp leaving a motorway is not its going on)
                out.add((fid, f2))
                out.add((f2, fid))
    return out


def build_tile(frame, tx, ty, cells, elev, urban_fn=None, walks=None, bridges=None, widths=None, props=None,
               lines=None, wires=None, town=None, park=None, left_hand=False, structures=(), mph=True, tunnels=None,
               buried=0.0, lamp_region=None, country=None,
               walls=None, deck_context=None, ground=None, terrain_paint=True, cover_out=None, walk_cuts=(),
               signals=None, marks=None, roads_out=None, skin_margin=0.0, walks_raw=None):
    """The chunks of one tile. cells: the mapgeom.Features of the "roads" layer of every cell touching
    the tile; elev(lat, lon) -> metres (the DEM); urban_fn: as road_specs. Features in several cells are
    taken once (by id). The carriageways, then each road's sidewalks / gutters / markings (extras), cut
    where another road's carriageway crosses them. walks: a list that gets the sidewalk runs (dicts
    pts / side / caps, clipped exactly to the tile, caps: whether each end is a real end) for the
    sidewalk mesh (sidewalks.py) and storm drains among the chunks; None: sidewalks as decals.
    bridges: a BridgeProfiles (deck heights of whole chains, shared by every tile); None: from the
    chains among these cells. ground(x, y) -> z: the terrain block's drawn ground in the frame, the sidewalks' heights
    (drape_walks); None: elev's. walk_cuts: [(centreline (k, 2), half width)] where no sidewalk runs (a gas station's
    made-up driveway: gasstations.tile_sites cuts). roads_out: a list that gets the ground carriageways near the tile
    ((fid, spec, centreline (k, 3), lon): the median barriers', medians.tile_rows). skin_margin (m, a geotile's content:
    blockworker.roads_geo): the draped surfaces' pieces (sidewalk runs and corners, markings, crosswalks, corner fills)
    taken this far past the tile's box, its own, the skin clipping them exactly at the tile's quad in the anchor's
    world (a seam-straddling crosswalk drawn by both tiles, each its half); walks_raw: a list that gets the sidewalk
    pieces before they are put on the ground (the skin drapes them on its anchor's). Pure given its inputs (sorted by
    feature id)."""
    own_box = tile_bbox(tx, ty)                 # (a geotile's knows its own points: box_owns)
    x0, y0, x1, y1 = own_box
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
        # east / north of the point at h = 0, as the ground's (heightmap.surface: its samples' lat / lon at plane
        # z 0) and every outline's (mapgeom.enu_xy); up of the point at its height. At its height the point lands
        # h x distance / earth radius farther out: US-163 in Utah at 1650 m, 13 km out, the lines lay 2.8 m east and
        # 2.0 m north of the asphalt and grip painted into the ground (the user's screenshot, 2026-10-29)
        e, n, _ = frame.geodetic_to_enu(lat, lon, 0.0)
        u = frame.geodetic_to_enu(lat, lon, h)[2]
        k = 0
        for fid, spec, ring in rings:
            m = len(ring)
            roads.append((fid, spec, np.stack([e[k:k + m], n[k:k + m], u[k:k + m]], axis=1),
                          float(np.mean(ring[:, 0]))))
            k += m
    # Geometric roundabout rings (islands.ring_directions): Overture often leaves junction=roundabout and oneway
    # off the ring ways (Chavagnes-en-Paillers 2026-10-28). No parking / inferred sidewalks on the ring, one-way so
    # centre lines are not painted through the circle. Short approaches that meet the ring lose sidewalks too:
    # their stubs against the circle were rectangular concrete blocks.
    ring_dir = {}
    if roads:
        import islands
        ring_dir = islands.ring_directions(roads, left_hand)
        if ring_dir:
            apps = islands.ring_approaches(roads, ring_dir)
            short_apps = {fid for fid, sp, pts, _ in roads if fid in apps
                          and float(np.sum(np.hypot(*np.diff(pts[:, :2], axis=0).T))) < islands.APPROACH_NO_WALK_M}
            roads = [(fid,
                      (sp._replace(urban=False, median=None, oneway=True, dir=ring_dir[fid]) if fid in ring_dir
                       else sp._replace(urban=False, median=None) if fid in short_apps
                       else sp),
                      pts, lon) for fid, sp, pts, lon in roads]
    chunks = []
    if roads_out is not None:
        roads_out.extend(roads)
    if cover_out is not None:
        # the ground carriageways for the decks' parapet test (roadmesh.deck ground=): drawn or painted into the ground,
        # a deck at grade across one has no parapet over its lanes (GitHub #96). With the roads in the ground there
        # were none, and a small bridge's wall stood across the road at its end (GitHub #258)
        for fid, spec, pts, _ in roads:
            for piece in clip_polyline(pts, x0 - COVER_MARGIN_M, y0 - COVER_MARGIN_M, x1 + COVER_MARGIN_M,
                                       y1 + COVER_MARGIN_M):
                for c in split(densify(simplify(piece))):
                    if len(c) >= 2:
                        cover_out.append(_chunk(look_of(spec), spec.width, c))
    for fid, spec, pts, _ in roads:
        if ROADS_IN_GROUND:                     # the carriageway painted into the ground (surface.photo_roads)
            continue
        # no smooth ribbon over rough ground — but only where the ground layer will paint it (--surface on); with
        # the surface pass off there is no ROCK/GRASS mask, and the road would otherwise vanish entirely
        if terrain_paint and terrain_surface(spec):
            continue
        look = look_of(spec)
        lanes = [(0.0, spec.width)]
        if look.get("strip"):                   # parallel strips across the width (see LOOK)
            n = max(1, int(round(spec.width / look["strip"])))
            sw = spec.width / n
            lanes = [((k + 0.5) * sw - spec.width / 2, sw) for k in range(n)]
        for off, lw in lanes:
            line = pts if off == 0.0 else offset_polyline(pts, off)
            for piece in clip_polyline(line, *box):
                for c in split(densify(simplify(piece))):
                    if len(c) >= 2:
                        chunks.append(_chunk(look, lw, c))
    decks_near = []                             # (retaining_walls: none under a bridge)
    for ch in _bridge_chunks(frame, bseen, elev, (x0, y0, x1, y1), bridges, cells):
        if ch.get("deck"):
            decks_near.append(ch)
        if not ch.get("ctx"):
            chunks.append(ch)
        elif deck_context is not None:          # the decks around the tile (roadmesh.deck's parapet test)
            deck_context.append(ch)
    if tunnels is not None:                     # TunnelProfiles: road surfaces and lines through tunnels
        tch = _tunnel_chunks(tunnels, cells, (x0, y0, x1, y1), terrain_paint)
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
    sm = float(skin_margin or 0.0)
    wexact = (x0 - sm, y0 - sm, x1 + sm, y1 + sm) if sm else exact    # (the skin's pieces: skin_margin)

    def skin_own(px, py):                       # a draped piece's owner test (skin_margin: the margin box)
        if sm:
            return wexact[0] <= px < wexact[2] and wexact[1] <= py < wexact[3]
        return box_owns(own_box, px, py)
    if walls is not None:                       # retaining walls where a road is cut into the ground (roadmesh.tunnel)
        tile_walls = retaining_walls(frame, roads, elev, exact, decks=decks_near)
        if props is None:                       # (no props here: stone)
            for w in tile_walls:
                w["rock"] = False
        walls.extend(w for w in tile_walls if not w["rock"])
        if props is not None:                   # a rural cut: rock-face pieces instead (cliff_rows)
            props.extend(cliff_rows([w for w in tile_walls if w["rock"]], roads))
        chunks = _off_wall_tops(chunks, [w for w in tile_walls if not w["rock"]])
    corners, jcuts, crosswalks, jramps, jlines, jinternal = ({}, {}, [], {}, {}, set()) if walks is None else \
        junctions.find(roads, has_sidewalks, None)
    # (no curb extensions: they were on every town street with parking, after Fells Point's, and most streets have none:
    # the corners stuck out into the road in Tustin's suburbs, GitHub #208)
    # small road loops (roundabouts, turnaround loops): no sidewalks facing their inside, a curbed island instead
    import islands
    isl = islands.find(roads, structures=structures) if walks is not None else []
    inner = islands.inner_sides(isl)
    if walks is not None:
        for f in isl:
            if f["ring"] is None:
                continue
            # (from its south-west-most point: each tile finds the loop from its own roads, starting anywhere)
            ring = np.roll(f["ring"], -int(np.lexsort((f["ring"][:, 1], f["ring"][:, 0]))[0]), axis=0)
            # (closed: offset with a vertex either side of its start, so its ends meet mitred, not 5 cm apart)
            band = offset_polyline(np.vstack([ring[-1:], ring, ring[:2]]), islands.ISLAND_BAND_M / 2)[1:-1]
            # tight simplify + denser nodes: the default 0.25 m / 12 m spacing faceted the curb into a hexagon (PR
            # #468). The whole ring simplified before it is clipped, so neighbouring tiles' pieces (each to its box and
            # the skin margin, the skin cutting them exactly at the tile's quad) share its vertices and meet
            band = simplify(band, tol=0.08)
            for piece in clip_polyline(band, *wexact):
                run = densify(piece, spacing=3.0)
                if len(run) >= 2:
                    walks.append({"pts": run, "side": 1, "caps": (False, False), "ramps": [],
                                  "w": islands.ISLAND_BAND_M, "fid": "island"})
    cornered = set()                            # road pairs whose sidewalks meet at a curb return
    if walks is not None:
        for (fid, side), cl in jcuts.items():
            for X, u, t in cl:
                for f2, s2, p2, _ in roads:
                    if f2 != fid and np.hypot(*(p2[:, :2] - X).T).min() < 1.0:
                        cornered.add((fid, f2))
        for c in corners:                       # each corner / crosswalk made by the tile it is in
            p = c["arc"][len(c["arc"]) // 2]
            if isl and islands.in_island(isl, p[:2]):
                continue                        # a curb return inside a loop: its island has its own curb
            # a curb return on the circulatory roadway or at a ring entry (approach sidewalks meeting there): the
            # ring has no sidewalk, so the corner was a rectangular block on the asphalt (PR #468)
            if any(float(np.hypot(*(p[:2] - f["poly"].mean(0))))
                   < float(np.max(np.hypot(*(f["poly"] - f["poly"].mean(0)).T))) + islands.ENTRY_CLEAR_M
                   for f in isl if f.get("round")):
                continue
            if skin_own(p[0], p[1]):
                walks.append({"corner": c})
                fill_look = _corner_look(p, roads)
                for fill in ([] if ROADS_IN_GROUND else ([c["fill"]] if c["fill"] is not None else []) + c.get("fills", [])):
                    chunks.append({"m": fill_look["m"], "w": 0.0, "tl": fill_look["tl"], "p": fill_look["p"],
                                   "poly": [[round(float(v), 3) for v in q] for q in fill], "n": []})
        import markings as _mk
        ladder = _mk.style(country, float(np.mean([r[3] for r in roads])) if roads else None).ladder
        for cw in crosswalks:                   # crosswalks and stop lines
            mid = 0.5 * (cw["a"] + cw["b"])
            if skin_own(mid[0], mid[1]):
                look = junctions.STOP_LINE if cw.get("stop") else junctions.CROSSWALK
                ab = np.array([cw["a"], cw["b"]])
                chunks.append(_chunk(look, cw["w"], ab))
                if ladder and not cw.get("stop"):
                    # a ladder crosswalk (MUTCD 3C.07, Baltimore's on the MD iMAP 3-inch aerial): a solid line along
                    # each side of the bars
                    for sd in (1, -1):
                        chunks.append(_chunk(EDGE_LINE, LADDER_LINE_W, offset_polyline(ab, sd * cw["w"] / 2)))
    import markings
    for fid, spec, pts, lon in roads:               # turn arrows (OpenStreetMap turn:lanes; MUTCD 3B.20)
        tags = lane_tags(*seen[fid][:2]) if fid in seen else None
        if not tags or spec.kind != "paved" or fid in jinternal:
            continue
        drv, nl = lane_layout(spec)
        for centre, t, kind_ in markings.arrows(tags, pts, not spec.oneway, drv, nl, left_hand):
            corners, uv = markings.arrow_quad(centre, t, kind_)
            if (corners[:, 0].max() >= wexact[0] and corners[:, 0].min() < wexact[2]
                    and corners[:, 1].max() >= wexact[1] and corners[:, 1].min() < wexact[3]):
                chunks.append({"m": markings.ARROW_MATERIAL, "w": 0.0, "tl": 4.0, "p": CENTRE["p"], "layer": "mark",
                               "poly": [[round(float(v), 3) for v in q] for q in corners],
                               "uvq": uv.tolist(), "n": []})
    gore_look = {"m": "line_white", "tl": EDGE_LINE["tl"], "p": CENTRE["p"]}
    for kind_, gw, gl in markings.gores(roads):     # ramp gores: channelizing lines and stripes (MUTCD 3B.08 / 3B.25)
        if kind_ == "fill":
            continue                                # (painted into the ground: surface.photo_roads)
        if kind_ == "stripe":
            mid = 0.5 * (gl[0] + gl[-1])
            if skin_own(mid[0], mid[1]):
                chunks.append(_chunk(gore_look, gw, gl))
            continue
        for piece in clip_polyline(gl, x0, y0, x1, y1):
            for c in split(densify(simplify(piece))):
                if len(c) >= 2:
                    chunks.append(_chunk(gore_look, gw, c))
    runs_near = []                              # sidewalk runs near the tile, before clipping (walks: below)
    # a parking aisle lies in a paved lot (painted asphalt: surface.photo_lots): no gravel edge along it (a dirt strip
    # stood across the asphalt between the stall rows, Canton, 2026-10-28)
    lot_aisles = {fid for fid, (f, i, sp) in seen.items()
                  if sp.kind == "paved" and _rule_value(f.rules.get("subclass"), i) == "parking_aisle"}
    # rounded corners where curbless paved roads meet (roadcorners.py, from PR #363): their fills when the roads are
    # meshes (else painted into the ground: surface.py), the gravel edge decal along each arc and a curved edge line
    # where both roads have them; corner_cuts end those road sides' straight edge lines and decals at the tangents
    import roadcorners
    corner_chunks, corner_cuts = roadcorners.tile(
        roads, box, not ROADS_IN_GROUND, country, left_hand,
        rings={fid for fid, (f, i, sp) in seen.items() if is_ring(f, i)}, no_decal=lot_aisles, exact=wexact)
    chunks.extend(corner_chunks)
    going_on = _going_on(roads)
    for fid, spec, pts, lon in roads:
        ex = extras(spec, pts, lon, left_hand, country, lane_tags(*seen[fid][:2]) if fid in seen else None) \
            if fid not in jinternal else []    # inside a junction: none
        if not ex:
            continue
        blockers = [(p2[:, :2], s2.width / 2 + CUT_MARGIN) for f2, s2, p2, _ in roads if f2 != fid]
        decal_blockers = [(p2[:, :2], s2.width / 2 + (CUT_MARGIN if (fid, f2) in going_on else
                                                      EDGE_DECAL["w"] + EDGE_DECAL_GAP_M))
                          for f2, s2, p2, _ in roads if f2 != fid and s2.kind in BUILT]
        # ring roads: sidewalks stop ENTRY_CLEAR_M outside the carriageway (no stub into the circle)
        walk_blockers = [(p2[:, :2],
                          (s2.width / 2 + (islands.ENTRY_CLEAR_M if f2 in ring_dir else 0.0)
                           if (fid, f2) in cornered or f2 in ring_dir
                           else sidewalk_stop(spec, fid, s2, f2)))
                         for f2, s2, p2, _ in roads if f2 != fid] + list(walk_cuts)
        # a median's band stops at the other carriageways' curbs only (their sidewalk is on their far side: it cut
        # one carriageway's half of Boston Street's median away)
        curb_blockers = [(p2[:, :2], s2.width / 2) for f2, s2, p2, _ in roads if f2 != fid]
        for look, w, line in ex:
            walk = look["m"] == SIDEWALK["m"]
            side = look.get("side")
            wb = curb_blockers if look.get("median") else walk_blockers
            if (walk or look["m"] == GUTTER["m"]) and (fid, side) in inner:
                continue                        # facing a traffic island: no sidewalk there
            if walk and walks is not None:
                # whole runs near the tile first (joined across tile borders below), clipped to the tile after
                for piece in clip_polyline(line, *near):
                    for cpiece, (j0, j1) in junctions.apply_cuts(piece, jcuts.get((fid, side), [])):
                        for run in [r for r0 in cut_outside(cpiece, wb, 0.5)
                                    for r in _on_level(frame, elev, densify(simplify(r0)))]:
                            if len(run) < 2:
                                continue
                            at_j0 = j0 and np.hypot(*(run[0, :2] - cpiece[0, :2])) < 0.6
                            at_j1 = j1 and np.hypot(*(run[-1, :2] - cpiece[-1, :2])) < 0.6
                            runs_near.append({"pts": run, "side": side, "w": w, "fid": fid, "j": (at_j0, at_j1),
                                              "median": bool(look.get("median")),
                                              "open": (not _on_box(run[0], near), not _on_box(run[-1], near))})
                continue
            if walks is not None and look["m"] == GUTTER["m"]:     # gutters end where their curb turns
                gcuts = jcuts.get((fid, side), [])
                pieces = [cp for p in clip_polyline(line, *box) for cp, _ in junctions.apply_cuts(p, gcuts)]
            elif walks is not None and jlines.get(fid):             # markings stop at the crosswalks
                pieces = [cp for p in clip_polyline(line, *box) for cp, _ in junctions.apply_cuts(p, jlines[fid])]
            else:
                pieces = clip_polyline(line, *box)
            if (look.get("kind") == "edge" or look["m"] == EDGE_DECAL["m"]) and (fid, side) in corner_cuts:
                pieces = [cp for p in pieces for cp, _ in junctions.apply_cuts(p, corner_cuts[(fid, side)])]
            if look["m"] == EDGE_DECAL["m"]:
                if fid in lot_aisles:
                    continue
                # no gravel edge where another carriageway is near: a ramp's gore and a narrow median are paved
                pieces = [r for pc in pieces for r in cut_outside(pc, decal_blockers, 0.5)]
            for piece in pieces:
                for run in cut_outside(piece, wb if walk else blockers, 0.5 if walk else 2.0):
                    for c in split(densify(simplify(run))):
                        if len(c) >= 2:
                            chunks.append(_chunk(look, w, c))
    if walks is not None:
        runs_near = join_walk_ends(runs_near, [(p2[:, :2], s2.width / 2) for _, s2, p2, _ in roads]
                                   + list(walk_cuts))       # (a cut's gap stays open: a driveway crosses it)
        drain_runs = []                         # (walks entry, side, real ends, width): drains once on the ground
        for rn in runs_near:
            fid, side, w, run0 = rn["fid"], rn["side"], rn["w"], rn["pts"]
            for run in clip_polyline(run0, *wexact):
                if len(run) < 2:
                    continue
                first = np.hypot(*(run[0, :2] - run0[0, :2])) < 1e-6
                last = np.hypot(*(run[-1, :2] - run0[-1, :2])) < 1e-6
                at_j0 = first and rn["j"][0]
                at_j1 = last and rn["j"][1]
                caps = (first and not at_j0 and not _on_box(run[0], wexact),
                        last and not at_j1 and not _on_box(run[-1], wexact))
                rl = [r for key in rn.get("ramp_keys", [(fid, side)]) for r in jramps.get(key, [])]
                ramps = junctions.ramp_ranges(run, rl)
                walks.append({"pts": run, "side": side, "caps": caps, "ramps": ramps, "w": w, "fid": fid,
                              "median": rn.get("median", False)})
                if not rn.get("median"):                # (a median: a bare curb, no storm drains)
                    drain_runs.append((walks[-1], side, (caps[0] or at_j0, caps[1] or at_j1), w))
        if walks_raw is not None:               # (the skin's: put on its anchor's ground there)
            import copy
            walks_raw.extend(copy.deepcopy(walks))
        drape_walks(frame, elev, walks, ground)
        for wk, side, ends, w in drain_runs:
            chunks.extend(drains(wk["pts"], side, ends, w))
    if lines is not None:                           # the centrelines near the tile (buildings' front doors)
        lines.extend((pts[:, :2], spec.width / 2, spec.cls, spec.urban) for _, spec, pts, _ in roads)
    if props is not None and walks is not None:     # street lights and utility poles (furniture.py)
        # (the speed-limit signs are the sign tiles' since 2026-10-28: speedsigns.py)
        import furniture
        decks = [(np.asarray(ch["n"], np.float64), ch["w"] / 2) for ch in chunks if ch.get("deck")]
        props.extend(furniture.place(roads, walks, own_box, wires=wires, town=bool(town), left_hand=left_hand,
                                     decks=decks, signals=signals, buried=buried, region=lamp_region,
                                     lighting={fid: str(f.attrs["osm_lit"][i]).strip().lower()
                                               for fid, (f, i, _sp) in seen.items() if "osm_lit" in f.attrs}))
    aisles = {fid for fid, (f, i, sp) in seen.items()
              if sp.kind == "paved" and _rule_value(f.rules.get("subclass"), i) == "parking_aisle"}
    if (park is not None and walks is not None) or (marks is not None and aisles):
        import parking
        if park is not None and walks is not None:      # curbside parking spots (parking.py)
            dirs = {}
            for fid, (f, i, sp) in seen.items():
                if sp.oneway:
                    v = _rule_value(f.rules.get("oneway"), i)
                    dirs[fid] = (-1 if float(v) < 0 else 1) if v is not None else (sp.dir or 1)
            park.extend(parking.curb_spots(roads, walks, own_box, left_hand, dirs))
            if aisles:
                park.extend(parking.aisle_spots(roads, aisles, own_box))
        if marks is not None and aisles:            # stall lines along the aisles (parking.aisle_markings)
            marks.extend(parking.aisle_markings(roads, aisles, wexact))
    return chunks


class Tracker:
    """Per connection: which road tiles the mod has, building and announcing the ones it needs."""

    def __init__(self, send_m=SEND_M, drop_m=DROP_M):
        self.send_m = send_m
        self.drop_m = drop_m
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
        self.started = {}       # tile -> monotonic time its build started (TILE_STUCK_S)
        self.stuck_said = set()

    def close(self):
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)

    def progress(self):
        """(done, wanted) of the road tiles round the car at the last pose: done = sent to the mod, empty, or failed
        and waiting for its retry (nothing more comes for it now). (0, 0) before a pose."""
        car = getattr(self, "car", ())
        done = sum(1 for k in car if k in self.sent or k in self.empty or k in self.retry)
        return done, len(car)

    def on_pose(self, conn, aid, x, y, loop, build, vel=(0.0, 0.0), ground_busy=False, view=None, foci=()):
        """Called with the player's position (and velocity, m/s) in the active anchor's frame. build(aid, tx, ty) runs
        in a worker and returns the tile's (path, n) or None; its result is announced with conn.send. ground_busy: a
        terrain block the mod asked for is being built: no tile beyond SEND_M starts meanwhile (the ground first; at
        180 mph the block ahead waited 7.5 s among them, 2026-10-19). view: the camera's (x, y) when it is away from
        the car (earthd camera_away): the tiles round it are wanted too, after the car's (which carry what it drives
        on and hits). foci: other vehicles' collision foci (x, y, r, trees) (the mod's collisionfoci.lua): the tiles
        within r + TILE / 2 of each are wanted too, after the car's and the camera's, and kept within r + TILE."""
        if aid != self.aid:
            for f in self.futs.values():
                f.cancel()
            self.aid, self.sent, self.building, self.futs = aid, set(), set(), {}
            self.empty, self.retry, self.fails = set(), {}, {}
            self.started, self.stuck_said = {}, set()
        if self.pool is None:
            from concurrent.futures import ThreadPoolExecutor
            self.pool = ThreadPoolExecutor(BUILD_PER_POSE, thread_name_prefix="roads-tile")
        vx, vy = (float(vel[0]), float(vel[1])) if vel else (0.0, 0.0)
        fast = math.hypot(vx, vy) >= FAST_MS
        car = wanted_tiles(x, y, vx, vy, send_m=self.send_m)
        self.car = car                      # (progress: the loading line's road tiles)
        seen = set(tiles_near(*view, self.send_m)) - car if view else set()
        others = set()
        for f in foci:
            others.update(tiles_near(f[0], f[1], min(self.send_m, f[2] + TILE / 2)))
        others -= car | seen
        wanted = car | seen | others
        start = set(tiles_near(x, y, self.send_m)) if ground_busy else wanted
        now = time.monotonic()
        want = [k for k in start if k not in self.sent and k not in self.building and k not in self.empty
                and self.retry.get(k, 0.0) <= now]
        car_key = path_key(x, y, vx, vy) if fast else view_key(conn, aid, x, y)
        cam_key = view_key(conn, aid, *view) if seen else None
        def focus_d(k):
            return min(tile_distance(k[0], k[1], f[0], f[1]) for f in foci)
        want.sort(key=lambda k: (2, focus_d(k)) if k in others else (1, cam_key(k)) if k in seen else (0, car_key(k)))
        for tx, ty in want[:max(0, BUILD_PER_POSE - len(self.building))]:   # in flight at most (vegetation.Tracker)
            self.building.add((tx, ty))
            self.started[(tx, ty)] = now
            fut = self.futs[(tx, ty)] = loop.run_in_executor(self.pool, build, aid, tx, ty)
            fut.add_done_callback(lambda f, k=(tx, ty), a=aid: self._built(conn, a, k, f))
        # a build that never ends leaves the loading line at "roads & buildings 0/36" with nothing in the log (GitHub
        # #241, a Discord report): said once per tile, with how many are in flight
        for k in self.building:
            t0 = self.started.get(k)
            if t0 is not None and now - t0 > TILE_STUCK_S and k not in self.stuck_said:
                self.stuck_said.add(k)
                log.warning("roads tile %s of anchor %d: building for %.0f s (%d tiles in flight, %d sent): the road "
                            "workers may be stuck", k, aid, now - t0, len(self.building), len(self.sent))
        def far(k):
            return tile_distance(k[0], k[1], x, y) > self.drop_m and (not view or tile_distance(k[0], k[1], *view) > self.drop_m) \
                and all(tile_distance(k[0], k[1], f[0], f[1]) > f[2] + TILE for f in foci)
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
        self.started.pop(key, None)
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
        out_dir = getattr(getattr(conn, "s", None), "out_dir", None)
        data = staging.inline_json(out_dir, path) if out_dir else None
        if data is not None:                # (the game need not open the file: staging.inline_json)
            m["data"] = data
        pool = getattr(getattr(conn, "s", None), "pool_shapes", None)
        if pool is not None:                # its meshes at pooled shape paths (matslots.py)
            slots = pool(conn, shape_files(aid, *key))
            if slots:
                m["slots"] = slots
        conn.send(m)


TILE_RETRY_S = 3.0              # s before a road tile whose build failed is built again (Tracker._built)
TILE_STUCK_S = 90.0             # s a road tile's build may take before the log says it may be stuck
TILE_FAILS_WARN = 5             # a tile's builds failed in a row before an expected kind of failure is logged as a warning
_FAILS_SEEN = set()             # the kinds of road tile failures logged in full (Tracker._built)
BLD_PARTS_MAX = 4               # building meshes per road tile ("bld", "bld2".."bld4": the mod's list)


# --- road geotiles (phase 1c, .claude/work/geotiles_plan.md) -------------------------------------------------------
# A road tile is a geotile (geotiles.Z: the global grid, key "g17_x_y"): its content (buildings, decks, tunnels,
# walls, wires, rails, props and guard rails, parking, stations, wells, sound) built once in its own frame and kept by
# the mod across rebases (placed into each anchor's world: geotile.lua), its skin (the draped road mesh, plazas,
# sidewalks) built per anchor on that anchor's lattice. After a rebase the server sends skins only (and the exact
# variants of the tall parts the rigid placement leaves off: GeoTracker.exact_need).

ROAD_GEO = 1                # the road geotiles' protocol (hello.roadGeo, the mod's roads.lua M.ROAD_GEO)
EXACT_TOL_COL_M = 0.03      # m a colliding part placed rigidly may be off (relief x the shear) before its exact variant
EXACT_TOL_M = 0.10          # ... a render-only one


def geo_wanted(frame, x, y, vx=0.0, vy=0.0):
    """wanted_tiles for geotiles (their quads in frame's world): those within SEND_M, and at FAST_MS or more those
    within CORRIDOR_M of the path ahead (LEAD_S of driving, LEAD_MAX_M at most). {(gx, gy): m from (x, y)}."""
    import geotiles
    want = {(a, b): d for a, b, d in geotiles.tiles_near(frame, x, y, SEND_M)}
    s = math.hypot(vx, vy)
    lead = min(LEAD_MAX_M, s * LEAD_S)
    if s < FAST_MS or lead <= SEND_M:
        return want
    ux, uy = vx / s, vy / s
    d = 0.0
    while d <= lead:
        for a, b, _dd in geotiles.tiles_near(frame, x + ux * d, y + uy * d, CORRIDOR_M):
            want.setdefault((a, b), d)
        d += TILE / 2
    return want


class GeoTracker:
    """Per connection: the road geotiles the mod has. Content is sent once for the connection (kept by the mod across
    anchors, sent: its keys), skins per anchor (skins), exact variants per anchor (shown: the variant's anchor per
    part the mod shows; none: its base shape). build(aid, gx, gy, content, skin, exact) runs in a worker and returns
    blockworker.roads_geo's result."""

    geo = True

    def __init__(self):
        self.aid = None
        self.frame = None
        self.frames = {}        # anchor id -> its frame (the exact variants' error: _shown_err)
        self.sent = set()       # (gx, gy) whose content the mod has
        self.info = {}          # (gx, gy) -> its content's {"h", "parts": {what: {pivot, relief, col}}}
        self.skins = set()      # (gx, gy) with self.aid's skin sent
        self.shown = {}         # (gx, gy) -> {what: anchor id of the exact variant the mod shows}
        self.need = {}          # ((gx, gy), aid) -> {what: pivot} its exact variants due (exact_need, cached)
        self.building = set()
        self.pool = None
        self.futs = {}
        self.retry = {}
        self.fails = {}
        self.started = {}
        self.stuck_said = set()
        self.car = set()
        self.empty = set()      # (anchor tiles' interface: progress)
        self.stats = {"content": 0, "skins": 0, "exact": 0, "exact_base": 0, "anchors": 0,
                      "content_cpu": 0.0, "skin_cpu": 0.0}

    def close(self):
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)

    def progress(self):
        """(done, wanted) of the tiles round the car: done = content and this anchor's skin sent, or waiting for a
        retry."""
        done = sum(1 for k in self.car if (k in self.sent and k in self.skins) or k in self.retry)
        return done, len(self.car)

    def ready_tiles(self, frame, x, y, r):
        """The anchor road tiles (tx, ty) within r of (x, y) (tiles_near) whose road geotiles are all with the mod
        (content and this anchor's skin, or waiting for a retry): their highway and speed signs may come (signstream).
        Before (2026-10-29) the signs waited until every road geotile round the car was in, which while driving it
        seldom was: signs came only once the car slowed down or stopped, one tile at a time, right next to it."""
        import geotiles
        reach = TILE / 2 * math.sqrt(2.0)
        out = set()
        for tx, ty in tiles_near(x, y, r):
            ks = geotiles.tiles_near(frame, (tx + 0.5) * TILE, (ty + 0.5) * TILE, reach)
            if all(((a, b) in self.sent and (a, b) in self.skins) or (a, b) in self.retry for a, b, _ in ks):
                out.add((tx, ty))
        return out

    def _shown_err(self, key, what, part, frame):
        """m the part (pivot, relief) of tile key is off in frame's world as the mod shows it (its base shape, or the
        exact variant of the anchor in shown) under rigid placement."""
        import geotiles
        tf = geotiles.frame(*key)
        piv = float(part.get("pivot", 0.0))
        rel = float(part.get("relief", 0.0))
        r_b, _t, c_b = geotiles.placement(tf, frame, (0.0, 0.0, piv))
        src = self.shown.get(key, {}).get(what)
        if src is None or src not in self.frames:
            off = c_b
        else:
            r_a, _ta, c_a = geotiles.placement(tf, self.frames[src], (0.0, 0.0, piv))
            off = r_b @ r_a.T @ c_a - c_b
        return rel * float(np.hypot(off[0], off[1]))

    def exact_need(self, key, aid=None, frame=None):
        """({what: {pivot}} of tile key's parts whose exact variant for the anchor must be made, [what] of those
        whose base shape is exact enough again while the mod shows a variant)."""
        aid = self.aid if aid is None else aid
        frame = self.frame if frame is None else frame
        info = self.info.get(key)
        if info is None or frame is None:
            return {}, []
        make, base = {}, []
        for what, part in (info.get("parts") or {}).items():
            tol = EXACT_TOL_COL_M if part.get("col") else EXACT_TOL_M
            src = self.shown.get(key, {}).get(what)
            if src == aid:
                continue
            if self._shown_err(key, what, part, frame) <= tol:
                continue
            saved = self.shown.get(key, {}).pop(what, None)         # (the base shape's error, for the choice)
            base_err = self._shown_err(key, what, part, frame)
            if saved is not None:
                self.shown[key][what] = saved
            if base_err <= tol:
                base.append(what)
            else:
                make[what] = {"pivot": part.get("pivot", 0.0)}
        return make, base

    def on_pose(self, conn, aid, x, y, loop, build, vel=(0.0, 0.0), ground_busy=False, view=None, foci=(),
                frame=None):
        import geotiles
        if frame is None:
            return
        if aid != self.aid:
            for f in self.futs.values():
                f.cancel()
            self.aid, self.frame, self.building, self.futs = aid, frame, set(), {}
            self.frames[aid] = frame
            while len(self.frames) > 4:
                old = min(a for a in self.frames if a != aid)
                del self.frames[old]
            self.skins, self.retry, self.fails, self.started, self.stuck_said = set(), {}, {}, {}, set()
            self.need = {}
            self.stats["anchors"] += 1
        if self.pool is None:
            from concurrent.futures import ThreadPoolExecutor
            self.pool = ThreadPoolExecutor(BUILD_PER_POSE, thread_name_prefix="roads-geo")
        vx, vy = (float(vel[0]), float(vel[1])) if vel else (0.0, 0.0)
        s = math.hypot(vx, vy)
        car = geo_wanted(frame, x, y, vx, vy)
        self.car = set(car)
        seen = {(a, b): d for a, b, d in geotiles.tiles_near(frame, *view, SEND_M)} if view else {}
        others = {}
        for f in foci:
            for a, b, d in geotiles.tiles_near(frame, f[0], f[1], min(SEND_M, f[2] + TILE / 2)):
                others.setdefault((a, b), d)
        wanted = set(car) | set(seen) | set(others)
        start = {(a, b) for a, b, _ in geotiles.tiles_near(frame, x, y, SEND_M)} if ground_busy else wanted
        now = time.monotonic()
        jobs = []
        for k in start:
            if k in self.building or self.retry.get(k, 0.0) > now:
                continue
            if k not in self.sent:
                jobs.append((k, True, True, "auto"))
                continue
            make = None
            if k not in self.skins or (k, self.aid) not in self.need:
                mk, base = self.exact_need(k)
                self.need[(k, self.aid)] = mk
                if base:                    # (the base shape is exact enough again: no build)
                    for w in base:
                        self.shown.get(k, {}).pop(w, None)
                    self.stats["exact_base"] += len(base)
                    conn.send({"type": "roads_exact", "anchorId": aid, "key": geotiles.key(*k),
                               "parts": {w: False for w in base}})
            make = self.need.get((k, self.aid)) or None
            if k not in self.skins or make:
                jobs.append((k, False, k not in self.skins, make))

        def order(job):
            k = job[0]
            if k in car:
                d = car[k]
                if s >= FAST_MS:        # (ahead first: the corridor's distance along the path)
                    q = geotiles.quad(frame, *k)
                    cx, cy = float(np.mean(q[:, 0])), float(np.mean(q[:, 1]))
                    along = ((cx - x) * vx + (cy - y) * vy) / s
                    if along < -TILE / 2:
                        d = d * SIDE_PENALTY + LEAD_MAX_M
                return (0, d)
            if k in seen:
                return (1, seen[k])
            return (2, others.get(k, 1e9))
        jobs.sort(key=order)
        for k, content, skin, make in jobs[:max(0, BUILD_PER_POSE - len(self.building))]:
            self.building.add(k)
            self.started[k] = now
            fut = self.futs[k] = loop.run_in_executor(self.pool, build, aid, k[0], k[1], content, skin, make)
            fut.add_done_callback(lambda f, kk=k, a=aid, c=content, sk=skin, m=make:
                                  self._built(conn, a, kk, f, c, sk, m))
        for k in self.building:
            t0 = self.started.get(k)
            if t0 is not None and now - t0 > TILE_STUCK_S and k not in self.stuck_said:
                self.stuck_said.add(k)
                log.warning("road geotile %s of anchor %d: building for %.0f s (%d in flight, %d sent): the road "
                            "workers may be stuck", k, aid, now - t0, len(self.building), len(self.sent))

        def far(k):
            q = geotiles.quad(frame, *k)
            if geotiles.quad_dist(q, x, y) <= DROP_M:
                return False
            if view and geotiles.quad_dist(q, *view) <= DROP_M:
                return False
            return all(geotiles.quad_dist(q, f[0], f[1]) > f[2] + TILE for f in foci)
        for k in [k for k in self.sent if k not in wanted and k not in self.building and far(k)]:
            self.sent.discard(k)
            self.skins.discard(k)
            self.info.pop(k, None)
            self.shown.pop(k, None)
            self.need = {nk: v for nk, v in self.need.items() if nk[0] != k}
            conn.send({"type": "roads_drop", "anchorId": aid, "key": geotiles.key(*k)})
        if self.retry:
            self.retry = {k: t for k, t in self.retry.items() if t > now}

    def _built(self, conn, aid, key, fut, content, skin, make):
        import geotiles
        if self.futs.get(key) is fut:
            self.building.discard(key)
            self.futs.pop(key, None)
            self.started.pop(key, None)
        if fut.cancelled():
            return
        try:
            res = fut.result()
        except Exception as e:
            kind = f"{type(e).__name__}: {e}"[:200]
            fails = self.fails[key] = self.fails.get(key, 0) + 1
            if kind not in _FAILS_SEEN and not isinstance(e, (OSError, TimeoutError)):
                _FAILS_SEEN.add(kind)
                log.warning("road geotile %s failed: %s", key, kind, exc_info=e)
            elif fails == TILE_FAILS_WARN:
                log.warning("road geotile %s: %d builds failed in a row, the last: %s", key, fails, kind)
            else:
                log.debug("road geotile %s: %s: %s", key, type(e).__name__, e)
            if aid == self.aid:
                self.retry[key] = time.monotonic() + TILE_RETRY_S
            return
        if res is None:
            self.retry[key] = time.monotonic() + TILE_RETRY_S
            return
        self.fails.pop(key, None)
        gkey = geotiles.key(*key)
        s = getattr(conn, "s", None)
        out_dir = getattr(s, "out_dir", None)
        pool = getattr(s, "pool_shapes", None)
        tm = res.get("time") or {}
        self.stats["content_cpu"] += float(tm.get("content_cpu", 0.0))
        self.stats["skin_cpu"] += float(tm.get("skin_cpu", 0.0))
        c = res.get("content")
        ex = (res.get("exact") or {}) if aid == self.aid else {}
        if c is not None:
            path, n, info = c[0], c[1], c[2]
            self.sent.add(key)
            self.info[key] = info
            self.shown.pop(key, None)
            self.stats["content"] += 1
            m = {"type": "roads_tile", "anchorId": aid, "key": gkey, "path": path, "n": n,
                 "frame": list(geotiles.centre(*key)), "h": info.get("h")}
            data = staging.inline_json(out_dir, path) if out_dir else None
            if data is not None:
                m["data"] = data
            if ex:                      # (the parts this anchor needs exact: the mod loads those, not the base)
                m["exact"] = dict(ex)
            if pool is not None:
                import blockworker
                names = blockworker.geo_names(gkey)
                files = {w: n_ for w, n_ in names.items() if w not in ("json", "inputs") and w not in ex}
                files.update({f"x:{w}": os.path.basename(p) for w, p in ex.items()})
                slots = pool(conn, files)
                if slots:
                    m["slots"] = slots
            conn.send(m)
        if aid != self.aid:             # (an old anchor's skin and variants: the new anchor's come on their own)
            return
        sk = res.get("skin")
        if skin and sk is not None:
            self.skins.add(key)
            self.stats["skins"] += 1
            m = {"type": "roads_skin", "anchorId": aid, "key": gkey, "path": sk[0]}
            data = staging.inline_json(out_dir, sk[0]) if out_dir else None
            if data is not None:
                m["data"] = data
            if pool is not None:
                import blockworker
                names = blockworker.geo_names(gkey, aid)
                slots = pool(conn, {w: n_ for w, n_ in names.items() if w != "json"})
                if slots:
                    m["slots"] = slots
            conn.send(m)
        for w in ex:
            self.shown.setdefault(key, {})[w] = aid
        self.stats["exact"] += len(ex)
        if c is not None:               # (what is left due: none, with "auto")
            mk, _base = self.exact_need(key)
            self.need[(key, aid)] = mk
        elif ex:
            m = {"type": "roads_exact", "anchorId": aid, "key": gkey, "parts": dict(ex)}
            if pool is not None:
                slots = pool(conn, {w: os.path.basename(p) for w, p in ex.items()})
                if slots:
                    m["slots"] = slots
            conn.send(m)
        if make and c is None:
            self.need[(key, aid)] = {w: v for w, v in (self.need.get((key, aid)) or {}).items() if w not in ex}


def geo_exact_auto(tile_frame, frame, parts):
    """{what: {pivot}} of a fresh content's parts (parts: {what: {pivot, relief, col}}) whose base shape placed rigidly
    in frame's world would be off by more than their tolerance (EXACT_TOL_COL_M colliding, EXACT_TOL_M else)."""
    import geotiles
    out = {}
    for what, part in (parts or {}).items():
        piv = float(part.get("pivot", 0.0))
        _r, _t, c = geotiles.placement(tile_frame, frame, (0.0, 0.0, piv))
        tol = EXACT_TOL_COL_M if part.get("col") else EXACT_TOL_M
        if float(part.get("relief", 0.0)) * float(np.hypot(c[0], c[1])) > tol:
            out[what] = {"pivot": piv}
    return out


def shape_files(aid, tx, ty):
    """The shape files write_tile may write for a tile, by the field naming them in the tile ({what: file name})."""
    out = {"road": f"road_{aid}_{tx}_{ty}.cdae", "deck": f"deck_{aid}_{tx}_{ty}.cdae",
           "walk": f"walks_{aid}_{tx}_{ty}.cdae", "walkcol": f"walkcol_{aid}_{tx}_{ty}.cdae",
           "bldcol": f"bldcol_{aid}_{tx}_{ty}.cdae", "wire": f"wire_{aid}_{tx}_{ty}.cdae",
           "bld": f"bld_{aid}_{tx}_{ty}.cdae", "tunnel": f"tunnel_{aid}_{tx}_{ty}.cdae",
           "rail": f"rail_{aid}_{tx}_{ty}.cdae", "railcol": f"railcol_{aid}_{tx}_{ty}.cdae"}
    for k in range(2, BLD_PARTS_MAX + 1):
        out[f"bld{k}"] = f"bld_{aid}_{tx}_{ty}_{k}.cdae"
    return out


# Sound spaces (the mod's ambience.lua makes them SFXSpaces: the stock maps' tunnel reverb in a tube, the one under a
# bridge below its deck): kinds, and how far a bridge's space reaches under the deck's surface
SOUND_TUNNEL, SOUND_BRIDGE, SOUND_TUNNEL_OPEN, SOUND_GARAGE, SOUND_PORTAL = 0, 1, 2, 3, 4
SOUND_REFINERY = (5, 6, 7)      # points: West Coast USA's refinery tower engines, large, small (industry.py)
SOUND_POWER_PLANT = 8           # points: West Coast USA's power plant sound (industry.py), played quieter by the mod
SOUND_UNDER_M = 25.0
# a closed tunnel's mouths: the stock portal snapshot event this far inside each (recorded 2026-10-26: the outside cut
# off within ~10 m of it, 7 dB down at 20 m, untouched from 40 m: a ~25 m fade outside the portal, both ways), its
# position SOUND_PORTAL_UP over the floor (a row [kind, x, y, z, 0, 0, 0, yaw (into the tube)])
SOUND_PORTAL_IN_M = 10.0
SOUND_PORTAL_UP = 2.0
# a low arch, or a tube shorter than this, has the open tunnel's (the outside heard, muffled ~5 dB) instead of the
# closed one's (the outside cut off): recorded 2026-10-26
SOUND_SHORT_TUNNEL_M = 120.0
SOUND_BOX_M, SOUND_TURN, SOUND_DZ_M, SOUND_EXTEND_M = 60.0, math.radians(10.0), 1.0, 1.0


def _along(line, dist):
    """The point dist along polyline line (k, 3) from its start (its end if shorter)."""
    seg = np.hypot(*np.diff(line[:, :2], axis=0).T)
    for i, L in enumerate(seg):
        if dist <= L and L > 0:
            return line[i] + (line[i + 1] - line[i]) * (dist / L)
        dist -= L
    return line[-1]


def sound_boxes(chunks):
    """The sound spaces of a tile's chunks: along every tunnel tube (its floor to its vault) and under every bridge
    deck (from its slab down SOUND_UNDER_M), as earthfast.sound_boxes rows [kind, x, y, z, length, width, height, yaw]
    rounded, one flat list; first a closed tunnel's portal points (SOUND_PORTAL) at its mouths."""
    import fast
    import roadmesh
    lines, out = [], []
    for c in chunks:
        if c.get("tunnel") and not c.get("no_tube"):
            h = float(c.get("tunnel_h") or roadmesh.TUNNEL_H)
            short = h < roadmesh.TUNNEL_H - 0.01 or float(c.get("tunnel_len") or 1e9) < SOUND_SHORT_TUNNEL_M
            lines.append((SOUND_TUNNEL_OPEN if short else SOUND_TUNNEL, c["n"],
                          float(c["w"]) + 2 * roadmesh.TUNNEL_SHOULDER, 0.5, h + 0.5))
            n = np.asarray(c["n"], np.float64)[:, :3]
            for end, line in ((0, n), (1, n[::-1])):
                if not short and (c.get("portal") or (False, False))[end] and len(line) >= 2:
                    p = _along(line, SOUND_PORTAL_IN_M)
                    d = line[1, :2] - line[0, :2]
                    out += [float(SOUND_PORTAL), round(float(p[0]), 2), round(float(p[1]), 2),
                            round(float(p[2]) + SOUND_PORTAL_UP, 2), 0.0, 0.0, 0.0,
                            round(math.atan2(float(d[1]), float(d[0])), 4)]
        elif c.get("deck") and not c.get("tunnel"):
            lines.append((SOUND_BRIDGE, c["n"], float(c["w"]), SOUND_UNDER_M, -roadmesh.DECK_DEPTH))
    if not lines:
        return out
    fast.require(78, "sound_boxes")
    pts = np.ascontiguousarray(np.concatenate([np.asarray(n, np.float64)[:, :3] for _, n, _, _, _ in lines]))
    off = np.cumsum([0] + [len(n) for _, n, _, _, _ in lines]).astype(np.int64)
    col = lambda k: np.ascontiguousarray([ln[k] for ln in lines], np.float64)     # noqa: E731
    rows = fast._ef.sound_boxes(pts, off, col(0), col(2), col(3), col(4), SOUND_BOX_M, SOUND_TURN, SOUND_DZ_M,
                                SOUND_EXTEND_M)
    rows[:, 1:7] = np.round(rows[:, 1:7], 2)
    rows[:, 7] = np.round(rows[:, 7], 4)
    return out + rows.ravel().tolist()


def write_tile(out_dir, vfs_dir, aid, tx, ty, chunks, walks=None, road_mesh=None, deck_mesh=None, bld=None,
               props=None, wires=None, park=None, tunnel_mesh=None, fuel=None,
               rail_mesh=None, garage_sound=(), wells=(), guardrail_rows=None, decals=True):
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
    data = {"v": VERSION, "chunks": [] if road_mesh is not None or not decals else
            [c for c in chunks if not c.get("tube_only")]}
    sound = sound_boxes(chunks) + list(garage_sound)      # (garage_sound: buildings.garage_sound_boxes)
    if sound:
        data["sound"] = sound
    if wells:                                               # oil wells' pump jacks (oilwells.tile_wells)
        data["wells"] = list(wells)
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
    if guardrail_rows:              # mapped guard rails and the median barriers (guardrails / medians): Forest rows
        import guardrails
        data["guardrails"] = {"items": guardrail_rows, "groups": list(guardrails.GROUPS)}
    if rail_mesh is not None:       # railways.tile_mesh: ballast, sleepers and rails
        import cdae
        import railways
        x0, y0, _, _ = tile_bbox(tx, ty)
        shape = f"rail_{aid}_{tx}_{ty}.cdae"
        roadmesh.write(os.path.join(out_dir, shape), rail_mesh)
        data["rail"] = {"shape": f"{vfs_dir}/{shape}", "pos": [x0, y0, 0.0]}
        col = railways.collision_mesh(rail_mesh)
        if col is not None:         # the lean physics-only mesh (the mod then only draws "rail"): its bed and decks
            shape = f"railcol_{aid}_{tx}_{ty}.cdae"
            cdae.write(os.path.join(out_dir, shape), col[0], col[1], uvs=col[2], material=col[3],
                       tri_materials=col[4], collision_only=True)
            data["railcol"] = {"shape": f"{vfs_dir}/{shape}", "pos": [x0, y0, 0.0]}
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
    if fuel:                        # gasstations.tile_sites' stations: pumps and refuel areas (gasstations.lua)
        data["fuel"] = fuel
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
