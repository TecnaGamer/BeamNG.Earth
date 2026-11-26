"""Railway stations from the map data (task 037): the platforms with their edge on the standard distance from the track,
the station building, the roofs over the platforms and the pedestrian links across the tracks (a footbridge, an
underpass, a crossing at the level of the rails), found from what the map says and, where it says little, worked out
from the tracks.

Two layers, as busstations.py (it keeps the adapter: busstations.items_from_layers makes the items from the Overture
cells; tools/dump_railstation_fixture.py dumps them for the tests):

    find(items)                    -> [seed group]    (the station points / halts / hall buildings, clustered)
    assemble(items, seeds, country) -> RailStation | None   (pure: the station round those seeds, None when too little is
                                                             known: RailStation.confidence)

An item is a dict {"layer", "cls", "sub", "name", "tags", "kind", "xy", ...} (busstations). Everything is in the frame's
metres and pure: no randomness, the same on every tile, anchor and BeamMP peer. The heights over the rail head come from
the rail's own profile (railways._beds) when the mesh is made, not here: a platform keeps its mapped `height` tag and the
country's norm (norm()).
"""
import math
import os

import numpy as np
import fast
import dmath

import busstations as bs

ON = os.environ.get("BNE_RAILSTATIONS", "on").lower() not in ("off", "0", "false")

RAIL_CLASSES = {"standard_gauge", "narrow_gauge"}       # (a tram or light rail line has no station of this kind)
SEED_CLASSES = {"railway_station", "railway_halt"}
HALL_CLASSES = {"train_station"}                        # buildings
BUILDING_CLASSES = {"train_station", "transportation"}
ROOF_CLASSES = {"roof", "shelter", "canopy"}
LINK_CLASSES = {"footway", "pedestrian", "corridor", "path", "steps", "cycleway"}
CLUSTER_M = 450.0           # seeds closer than this are one station (a big station's halls and its halt: Kyiv's 3 points)
REACH_M = 380.0             # a station takes the platforms, roofs and links within this of its hub
PLATFORM_TRACK_M = 12.0     # a platform farther than this from every track (level 0) is not a railway one
SNAP_REACH_M = 5.5          # a platform's boundary this near a track and parallel to it is moved to the standard edge
SNAP_ANGLE_DEG = 28.0       # ... parallel: within this
RESAMPLE_M = 2.0            # the boundary's points (a curved track's edge follows it)
LINE_WIDTH_M = 4.0          # a platform mapped as a line: the strip's width on the side without a track
ISLAND_MAX_M = 14.0         # an island platform between two tracks is at most this wide
SYNTH_LEN_M = {"station": 250.0, "halt": 150.0}
SYNTH_WIDTH_M = 4.0
# Railway-at-road fixes (the user's in-game reports r63 / r64 / r92, 2026-11-17; Revamped roads switch set): a platform
# worked out from the track (no platform mapped) stops short of the roads and walkways it would cross, and a station
# point that says it has no passengers gets none (_off_roads, _no_passengers). Off by default until reviewed in game.
ROAD_FIT = os.environ.get("BNE_RAIL_ROAD_FIT", "off") == "on"
ROAD_CLEAR_M = 2.5          # m past a carriageway's half width a worked-out platform keeps clear (its kerb and footway)
WALK_CLEAR_M = 1.0          # m past a walkway's half width (one crossing the platform's line)
WALK_CLEAR_CLASSES = {"footway": 2.0, "path": 1.5, "cycleway": 2.5, "pedestrian": 3.0, "steps": 2.0,
                      "bridleway": 2.0}         # class -> width (m) when the map has none
SYNTH_MIN_M = 30.0          # m: a worked-out platform shorter than this is none (_synth's own minimum)
FIT_NATIVE = 423            # earthfast.VERSION of station_platform_trim (railfit.rs)
BUILDING_NEAR_M = 160.0
ROOF_NEAR_M = 6.0           # a roof this near a platform (or overlapping it) covers it
LINK_JOIN_M = 2.5           # ways whose ends are this near are one link
JOIN_CLASSES = ("steps", "footway", "corridor", "pedestrian", "path")    # (off level 0: they join a link)
LINK_PLATFORM_M = 6.0       # a link's end this near a platform ends on it
MIN_CONFIDENCE = 4
CANOPY_LEN_M = 110.0        # a canopy worked out for a platform without a mapped roof: this long at most ...
CANOPY_SHARE = 0.6          # ... and this share of the platform's edge at most
CANOPY_MIN_RUN_M = 60.0     # a platform's edge shorter than this gets none
CANOPY_IN_M = 1.2           # the canopy stands this far in from the track edge (the safety zone stays open)
CANOPY_BACK_M = 0.8         # ... and ends this far from the platform's other edge
CANOPY_MAX_W_M = 7.5        # the canopy reaches this far from its first line at most
CANOPY_MIN_W_M = 2.5
CANOPY_STEP_M = 2.0
MAIN_STATION_PLATFORMS = 3  # a station of this many platforms or more roofs the ones without a mapped roof

# Materials (tools/make_station_textures.py, railstation.materials.json)
PAVERS = "bne_platform_pavers"          # the platform's top
TACTILE = "bne_platform_tactile"        # the yellow band along the track edge
# the canopies' steel, tinted: the map's roof colour picks the nearest (the map rarely has one: then "grey")
CANOPY_PALETTE = {"grey": (0.71, 0.72, 0.72), "white": (0.90, 0.90, 0.88), "dark": (0.30, 0.31, 0.33),
                  "blue": (0.18, 0.36, 0.66), "green": (0.20, 0.45, 0.30), "red": (0.62, 0.16, 0.14),
                  "brown": (0.42, 0.28, 0.20), "yellow": (0.85, 0.68, 0.15)}
CANOPY_DEFAULT = "grey"
ROOF_MATERIAL_LOOK = {"glass": "white", "metal": "grey", "steel": "grey", "tin": "grey", "zinc": "grey",
                      "concrete": "white", "copper": "green", "plastic": "white", "polycarbonate": "white"}


def canopy_material(look):
    return "bne_canopy_" + look


def canopy_look(colour=None, material=None):
    """The palette name nearest to a mapped colour (hex or CSS name, as buildings.parse_color reads it), else the look of
    a mapped roof material, else CANOPY_DEFAULT."""
    fast.require(584, "rail_canopy_look")
    return fast._ef.rail_canopy_look(colour, material)


# Standard distances (m) of a platform's edge from the track's axis and its height over the rail head, by country
# (typical values of the national rules: UA PTE, CZ SŽ, DE EBO / TSI PRM). "high" is a main station, "low" a halt.
EDGE_M = {"UA": 1.92, "CZ": 1.67, "SK": 1.67, "DE": 1.675, "AT": 1.725, "PL": 1.67, "default": 1.7}
HEIGHT_M = {"UA": {"high": 1.10, "low": 0.20}, "CZ": {"high": 0.55, "low": 0.30}, "SK": {"high": 0.55, "low": 0.30},
            "DE": {"high": 0.76, "low": 0.55}, "AT": {"high": 0.55, "low": 0.30}, "PL": {"high": 0.76, "low": 0.55},
            "default": {"high": 0.55, "low": 0.30}}
HEIGHT_RANGE_M = (0.05, 1.3)


def norm(country, platforms=3):
    """(edge distance from the track's axis, height over the rail head) of a station's platforms by country; a station
    of 3 platforms or more is "high", a smaller one a halt."""
    c = (country or "").upper()
    edge = EDGE_M.get(c, EDGE_M["default"])
    h = HEIGHT_M.get(c, HEIGHT_M["default"])
    return edge, h["high"] if platforms >= 3 else h["low"]


class Platform:
    """One platform: ring (k, 2) counter-clockwise (the standard edge towards each track), snapped (k,) the points moved
    to it, tidx (k,) the track each was moved from (-1 not moved), the map's tags."""

    def __init__(self, pid, ring, tags, mapped=True):
        self.id = pid
        self.ring = ring
        self.snapped = np.zeros(len(ring), bool)
        self.tidx = np.full(len(ring), -1, np.int64)       # into self.tracks
        self.tracks = []                                   # [(id, xy)] the tracks beside it that its edges were put by
        self.tags = tags
        self.mapped = mapped            # False: worked out from the track (no platform in the data)
        self.roofs = []                 # ids of the roofs over it
        self.island = False

    def tag(self, key):
        return self.tags.get(key, "")

    @property
    def ref(self):
        for k in ("local_ref", "ref"):
            v = self.tag(k)
            if v and len(v) <= 8:
                return v
        return ""

    @property
    def height_tag(self):
        try:
            v = float(str(self.tag("height")).replace(",", ".").split()[0])
        except (ValueError, IndexError):
            return None
        return v if HEIGHT_RANGE_M[0] <= v <= HEIGHT_RANGE_M[1] else None

    @property
    def covered(self):
        return self.tag("covered") == "yes" or self.tag("shelter") == "yes" or bool(self.roofs)


class Link:
    """A pedestrian link of the station: kind "over" (a footbridge), "under" (an underpass) or "level" (a crossing at the
    rails' level); xy (k, 2) its main way, steps [(k, 2)] the stairs ends, crosses [(track index, arc length m)]."""

    def __init__(self, kind, xy, steps, level, ids, covered=False, width=0.0):
        self.kind = kind
        self.xy = xy
        self.steps = steps
        self.level = level
        self.ids = ids
        self.covered = covered
        self.width = width
        self.crosses = []


class RailStation:
    def __init__(self):
        self.name = ""
        self.hub = np.zeros(2)
        self.kind = "halt"              # "station" or "halt" (the hub's class)
        self.tracks = []                # [(id, xy)] the level-0 tracks near the platforms
        self.platforms = []
        self.buildings = []             # [(id, ring, height)] the station's hall(s)
        self.roofs = []                 # [(id, ring)] over platforms
        self.links = []
        self.confidence = 0
        self.synth = False
        self.country = None
        self.claimed = set()            # ids of buildings-layer roofs the station draws itself (tile_skip)
        self.roof_looks = {}            # roof id -> CANOPY_PALETTE name


# --- geometry --------------------------------------------------------------------------------------------------

def tracks_beside(ring, tracks, margin=SNAP_REACH_M + 2.0):
    """The tracks (id, xy) whose bounding box reaches the ring's, grown by margin: the only ones a platform's edge can be by."""
    fast.require(584, "rail_tracks_beside")
    return fast._ef.rail_tracks_beside(np.asarray(ring, np.float64), tracks, margin)


SNAP_NATIVE = 263          # earthfast.VERSION of station_snap_edges / station_link_chains (stationgeom.rs)


def _track_rows(tracks):
    """(rows (m, 2), off (k + 1)): the tracks' (id, xy) points one after another, track k's rows off[k]..off[k + 1]."""
    rows = [np.asarray(xy, np.float64).reshape(-1, 2) for _tid, xy in tracks]
    off = np.zeros(len(rows) + 1, np.int64)
    off[1:] = np.cumsum([len(r) for r in rows])
    return (np.concatenate(rows) if rows else np.zeros((0, 2))), off


def snap_edges(ring, tracks, edge_m):
    """(ring', snapped, tidx): the boundary of a platform (resampled every RESAMPLE_M) with every point that runs beside
    a track (within SNAP_REACH_M, parallel within SNAP_ANGLE_DEG) put exactly edge_m from that track's axis, on its own
    side; the corner after a run of snapped points beside the same track too, and points the snap brought within
    0.25 m of the next one dropped. A platform mapped to the rail's axis, or a metre off it, ends on the standard
    distance, and a platform beside a curve follows the curve (earthfast.station_snap_edges: the tracks' segments in a
    grid; the per-point numpy pass over every segment of the box was most of a King's Cross / Euston road tile)."""
    import fast
    fast.require(SNAP_NATIVE, "station platform edges")
    rows, off = _track_rows(tracks)
    return fast._ef.station_snap_edges(np.ascontiguousarray(np.asarray(ring, np.float64)[:, :2]), rows, off,
                                       float(edge_m), SNAP_REACH_M, dmath.cos(math.radians(SNAP_ANGLE_DEG)), RESAMPLE_M)


# --- what is a railway station ---------------------------------------------------------------------------------

def _num(v):
    try:
        return float(str(v).replace(",", ".").split()[0])
    except (ValueError, IndexError):
        return None


def is_train_platform(it):
    """A platform of the railway (not a tram, bus or metro one that the map calls a platform too)."""
    fast.require(584, "rail_train_platform")
    return fast._ef.rail_train_platform(it)


def find(items):
    """[[seed items]] clustered: the station points and halts (and a hall building when no point is near it)."""
    fast.require(584, "rail_find")
    return fast._ef.rail_find(items)


def _crossings(path, tracks):
    """[(track index, arc length m along path)] where the path crosses a track (each pair of segments crossing within
    both, t and u in [0, 1], not near parallel: |cross| >= 1e-9), in order of arc (earthfast.station_crossings: the
    Python loop's numbers, ~1 CPU-s of a Dijon group's layout)."""
    import fast
    fast.require(bs.GEOM_NATIVE, "station geometry")
    if not tracks:
        return []
    rows, off = _track_rows(tracks)
    return fast._ef.station_crossings(np.asarray(path, np.float64).reshape(-1, 2), rows, off)


def _links(items, tracks, platforms, hub):
    """The pedestrian links across the tracks: a footbridge (level > 0), an underpass (level < 0) with their stairs, and the
    footways that cross the tracks at the rails' level."""
    fast.require(584, "rail_links")
    return fast._ef.rail_links(items, tracks, platforms, hub)


SPAN_MIN = 0.4              # a way is a bridge / a tunnel when that is this share of it or more
SKIP_SUBCLASS = ("sidewalk", "crosswalk")      # (a road bridge's pavement is not a link of the station)


def span_kind(w):
    """"bridge", "tunnel" or None: what the way is by its spans (the map cells' level ranges), else by its tags."""
    fast.require(584, "rail_span_kind")
    return fast._ef.rail_span_kind(w)


def synth_canopies(st):
    fast.require(584, "rail_canopies")
    fast._ef.rail_canopies(st)


def _runs_of(pl):
    """[index list] the runs of the platform's track edge (railstationmesh._runs, without importing it at module level)."""
    import railstationmesh
    return railstationmesh._runs(pl.snapped, pl.ring, pl.tidx)


def hubs_of(groups):
    """[(2,)] the hub of each seed group (find's): the mean of its seeds."""
    fast.require(584, "rail_hubs")
    return fast._ef.rail_hubs(groups)


def stations(items, country=None):
    """[RailStation] of every station in items (a group of seeds that knows too little is left out); each platform, roof
    and link belongs to the station whose hub is nearest."""
    fast.require(584, "rail_stations")
    return fast._ef.rail_stations(items, country)


def assemble(items, seeds, country=None, hubs=None, me=0):
    """The station round the seed group `seeds` (find's), or None when too little is known to build one. hubs / me: all
    the hubs near (stations()) and this one's index: what is nearer to another hub is that station's."""
    fast.require(584, "rail_assemble")
    return fast._ef.rail_assemble(items, seeds, country, hubs or [], me)


def _synth(st, near, tracks, edge_m):
    """A platform where the map has none: along the longest track within 60 m of the hub, on the building's side."""
    fast.require(584, "rail_synth")
    return fast._ef.rail_synth(st, near, tracks, edge_m, ROAD_FIT)


def _off_roads(line, out, edge_m, near, ss, arc):
    """BNE_RAIL_ROAD_FIT: the indices of the synthesized platform's line samples (line (k, 2), out (k, 2) the unit side
    the platform lies on) to keep, or None: the platform's cross-section (edge_m .. edge_m + SYNTH_WIDTH_M from the
    track's axis) off every ground carriageway of near (its half width by class, or a wider mapped width, + ROAD_CLEAR_M:
    its kerbs and footways) and off the walkways crossing it (their width + WALK_CLEAR_M). Of the runs left, the one at
    the station's point (arc, along ss) else the longest, at least SYNTH_MIN_M long. A platform worked out from a bare
    station point ran 250 m along the track, straight across a level crossing (Hahnstaetten, the user's report r92,
    2026-11-17). earthfast.station_platform_trim. Pure."""
    import roads
    fast.require(584, "rail_platform_obstacles")
    cars, walks = fast._ef.rail_platform_obstacles(near, WALK_CLEAR_CLASSES, roads.STYLE)
    offs = np.array([edge_m, edge_m + SYNTH_WIDTH_M / 2.0, edge_m + SYNTH_WIDTH_M], np.float64)
    segs = roads._Segs(cars)
    rows = [v[0].reshape(len(v[0]), -1)[:, :2] for v in walks.values()]
    woff = np.zeros(len(rows) + 1, np.int64)
    woff[1:] = np.cumsum([len(r) for r in rows])
    wp = np.ascontiguousarray(np.concatenate(rows)) if rows else np.zeros((0, 2))
    whw = np.asarray([v[1] for v in walks.values()], np.float64)
    got = fast._ef.station_platform_trim(np.ascontiguousarray(np.asarray(line, np.float64)[:, :2]),
                                         np.ascontiguousarray(np.asarray(out, np.float64)[:, :2]), offs, segs.a, segs.b,
                                         segs.h, ROAD_CLEAR_M, wp, woff, whw, WALK_CLEAR_M,
                                         np.ascontiguousarray(np.asarray(ss, np.float64)), float(arc), SYNTH_MIN_M)
    return None if got is None else np.asarray(got, np.int64)


# --- the stations of a road tile (the map cells through busstations' adapter), kept per process ---------------------

import threading
from collections import OrderedDict

_memo = OrderedDict()
_memo_lock = threading.Lock()
MEMO_N = 64     # groups kept (about 20 kB each). 12 were too few in a city of stations: nine road tiles in central
                # Sydney use 18 groups, and each one pushed out was laid out again for the next tile (their median 2.9 s
                # with 12, 1.0 s with 64; the same files built, 2026-11-15)
SEED_DEG = (0.006, 0.0045)      # (lon, lat) seeds this near each other (about 450 m) are read together
GROUP_PAD_DEG = (0.0045, 0.0035)
TILE_REACH_M = 420.0            # a station this close to a tile is made in it (its pieces that fall in the tile are kept)
PAD_DEG = 0.005


def _group(md, frame, group):
    """[RailStation] round one group of seed points [(lon, lat)], laid out in frame; kept per process."""
    key = (round(frame.lat0, 6), round(frame.lon0, 6), tuple(group))
    with _memo_lock:
        if key in _memo:
            _memo.move_to_end(key)
            return _memo[key]
    import countrybounds
    import mapcells
    lons = np.array([g[0] for g in group])
    lats = np.array([g[1] for g in group])
    cells = mapcells.cells_for_points(lats, lons, pad_lat=GROUP_PAD_DEG[1], pad_lon=GROUP_PAD_DEG[0], max_cells=64)
    names = ("infrastructure", "buildings", "roads")
    cds = mapcells.load_cells(md, cells, names)
    layers = {name: [cd.layers.get(name) for cd in cds] for name in names}
    e, n, _ = frame.geodetic_to_enu(lats, lons, np.zeros(len(lats)))
    centre = (float(np.mean(e)), float(np.mean(n)))
    items = bs.items_from_layers(frame, layers, centre, REACH_M + 90.0)
    try:
        country = countrybounds.bundled().at(float(np.mean(lats)), float(np.mean(lons)))
    except Exception:
        country = None
    found = stations(items, country)
    for st in found:
        st.country = country
    with _memo_lock:
        _memo[key] = found
        while len(_memo) > MEMO_N:
            _memo.popitem(last=False)
    return found


def stations_near(md, frame, box, shared=False):
    """The RailStations within TILE_REACH_M of the tile box (x0, y0, x1, y1) of frame, [] almost always (one pass over the
    infrastructure cells for a railway station point). Errors cost the stations, not the tile. shared: each group laid
    out in its own frame (busstations.group_frame: the same for every tile, its geometry not in frame; tile_skip)."""
    if not ON or md is None:
        return []
    import logging
    x0, y0, x1, y1 = (float(v) for v in box)
    bounds = (x0, y0, x1, y1)
    fast.require(584, "rail_stations_near")
    try:
        found = fast._ef.rail_station_groups(md, frame, bounds, shared, _group)
    except Exception as ex:
        logging.getLogger("earthd.railstations").warning("rail stations of the tile at %s: %s: %s",
                                                         tuple(round(float(v)) for v in box[:2]), type(ex).__name__, ex)
        return []
    return fast._ef.rail_stations_near(found, frame, bounds)


def tile_skip(frame, tx, ty, md):
    """Ids of the buildings-layer roofs that the railway stations near road tile (tx, ty) draw themselves (buildings.
    tile_buildings' skip, as busstations.tile_skip)."""
    if not ON:
        return frozenset()
    import roads
    return frozenset().union(*(st.claimed for st in stations_near(md, frame, roads.tile_bbox(tx, ty), shared=True)))
