"""Adapt map cells and coordinate frames to the native bus station layout.

Items retain their feature IDs, attributes and float64 frame coordinates. The station
owns its geometry and the per-layout memos shared by mesh, sign and furniture callers.
"""
import logging
import os
import threading
from collections import OrderedDict

import numpy as np

import fast

_native = fast.native(545, "bus stations")

log = logging.getLogger("earthd.busstations")


def tags_of(feats, i):
    return _native.bus_tags(feats, i)


def items_from_layers(frame, layers, hub_xy=(0.0, 0.0), radius=260.0):
    hx, hy = map(float, hub_xy[:2])
    hla, hlo, _ = frame.enu_to_geodetic(np.array([hx]), np.array([hy]), np.zeros(1))
    hla, hlo = float(hla[0]), float(hlo[0])
    bounds = _native.bus_search_bounds(hla, hlo, radius)
    out, seen = [], set()
    for name, cells in layers.items():
        for feats in cells:
            if feats is not None and len(feats):
                _native.bus_item_cell(feats, frame, name, _item_memo(feats), (hx, hy), radius, bounds, seen, out)
    return out


def _item_memo(feats):
    """Frame-independent rows kept with the cell's Features, new when a column or rule table it reads was replaced
    since (levelways.promote's class column, a join's): the columns themselves are its key, by identity."""
    if not hasattr(feats, "__dict__"):
        return {}
    a = feats.attrs
    sig = tuple(a.get(k) for k in ("class", "subtype", "name", "tags", "height", "min_height", "num_floors",
                                   "roof_shape", "surface", "basic_category", "taxonomy", "subclass", "roof_color",
                                   "roof_material")) + (feats.kind, feats.lonlat) \
        + tuple(v for _k, v in sorted((getattr(feats, "rules", None) or {}).items()))
    got = feats.__dict__.get("_bs_items")
    if got is None or len(got[0]) != len(sig) or any(x is not y for x, y in zip(got[0], sig)):
        got = feats.__dict__["_bs_items"] = (sig, {})
    return got[1]


WALK_SPAN_CLASSES = ("footway", "steps", "path", "pedestrian", "corridor", "cycleway")


def items_to_json(items):
    """JSON-able copy of items (arrays -> lists, rounded to 1 cm): a test fixture."""
    return [{k: (np.round(v, 2).tolist() if isinstance(v, np.ndarray) else v) for k, v in it.items()
             if not k.startswith("_")} for it in items]


def items_from_json(rows):
    out = []
    for r in rows:
        it = dict(r)
        it["xy"] = np.asarray(it["xy"], np.float64).reshape(-1, 2)
        out.append(it)
    return out


# --- geometry helpers ------------------------------------------------------------------------------------------

def _open(ring):
    ring = np.asarray(ring, np.float64)
    if len(ring) < 2:
        return ring
    n = _native.bus_open_len(ring.reshape(len(ring), -1))
    return ring if n == len(ring) else ring[:n]


def area(ring):
    r = _open(ring)
    return abs(_native.bus_signed(r)) if len(r) >= 3 else 0.0


def centroid(xy):
    """Centre of a point set or ring (the area's centroid for a ring with an area, else the mean)."""
    xy = np.asarray(xy, np.float64)
    if xy.ndim == 2:            # (the shoelace sums natively: earthfast ring_centroid, numpy's numbers)
        import fast
        fast.require(235, "ring_centroid")
        got = fast._ef.ring_centroid(np.ascontiguousarray(xy))
        if got is not None:
            return np.array(got, np.float64)
    return _open(xy).mean(0)


GEOM_NATIVE = 221          # earthfast.VERSION of station_contains / station_path_dist (stationgeom.rs)


def _xy(a):
    """a's rows (k, >= 2) as float64, x and y only."""
    a = np.asarray(a, np.float64)
    return a.reshape(-1, a.shape[-1] if a.ndim > 1 else 2)[:, :2]


def contains(ring, p):
    """Is point p inside the polygon ring (even-odd)? (earthfast.station_contains: the ring opened, the crossings'
    expression as it was in Python.)"""
    import fast
    fast.require(GEOM_NATIVE, "station geometry")
    return bool(fast._ef.station_contains(_xy(ring), np.array([[float(p[0]), float(p[1])]]))[0])


def contains_any(ring, pts):
    """any(contains(ring, p) for p in pts), in one kernel call."""
    import fast
    fast.require(GEOM_NATIVE, "station geometry")
    pts = _xy(pts)
    return bool(len(pts)) and bool(fast._ef.station_contains(_xy(ring), pts).any())


def dist_to_path(path, p, closed=False):
    """Distance from p to the polyline path (the closing edge too when closed). (earthfast.station_path_dist: numpy's
    steps to the bit, one call instead of a numpy round of a few rows.)"""
    import fast
    fast.require(GEOM_NATIVE, "station geometry")
    return float(fast._ef.station_path_dist(_xy(path), np.array([[float(p[0]), float(p[1])]]), bool(closed))[0])


def min_dist_to_path(path, pts, closed=False):
    """min(dist_to_path(path, p, closed) for p in pts) (pts not empty), in one kernel call."""
    import fast
    fast.require(GEOM_NATIVE, "station geometry")
    return float(fast._ef.station_path_dist(_xy(path), _xy(pts), bool(closed)).min())


def hull(xy):
    return _native.bus_hull(_xy(xy))


MIN_CONFIDENCE = 5
SIGN_CLEAR_M = 5.0          # the sign stands this far from a street's centreline at least


def name_matches(name, names):
    return _native.bus_name_matches(name or "", [s or "" for s in names])


def bay_label(*sources):
    return _native.bus_bay_label(sources)


def item_centroid(it):
    """centroid(it["xy"]) of an item, kept on it ("_c"; a copy returned): a group's layout asks for each item's centre
    and area again and again (every station's region, buildings, halls, obstacles, greens: ~35,000 numpy centroids of
    a few thousand items for Angers' stations, half the layout's CPU, 2026-11-11)."""
    c = it.get("_c")
    if c is None:
        c = it["_c"] = centroid(it["xy"])
    return c.copy()


def item_area(it):
    """area(it["xy"]) of an item, kept on it ("_a")."""
    a = it.get("_a")
    if a is None:
        a = it["_a"] = area(it["xy"])
    return a


class Station:
    """The station found round one hub. All geometry in the frame's metres."""

    def __init__(self):
        self.name = ""
        self.hub = np.zeros(2)
        self.boundary = None            # mapped polygon (n, 2) or None
        self.region = None              # the polygon the members were taken from (boundary, else a hull)
        self.surface = ""               # the mapped surface of the boundary ("asphalt", "paving_stones", ...)
        self.buildings = []             # [Item], the station's own first, the largest first
        self.platforms = []             # [Item] (line or polygon, bus platforms)
        self.bays = []                  # [{"xy", "label", "tags", "name", "id"}]
        self.canopies = []              # [{"ring", "height", "src", "area"}]
        self.claimed = set()            # ids of buildings-layer roofs this station draws itself (tile_skip)
        self.roads = []                 # [Item] service roads near the hub
        self.streets = []               # [Item] the public streets near it (the sign stands toward the nearest)
        self.obstacles = []             # [ring] the buildings round the hub (not roofs): no bay stands in one
        self.passages = []              # [Item] the service roads that are the yard's own drive lanes
        self.lanes = []                 # [{"xy", "half", "oneway"}] their corridors (travel direction along xy when oneway)
        self.mouths = []                # [(x, y)] where a lane meets a public street: kept clear for MOUTH_R_M
        self.greens = []                # [ring] the mapped lawns, parks and flowerbeds round the hub (the yard leaves them out)
        self.evidence = []              # [(what, points)]
        self.confidence = 0
        self.fid = ""

    def confident(self):
        return self.confidence >= MIN_CONFIDENCE

    def describe(self):
        return {"name": self.name, "hub": [round(float(v), 1) for v in self.hub], "confidence": self.confidence,
                "boundary_m2": round(area(self.boundary)) if self.boundary is not None else 0,
                "buildings": len(self.buildings), "platforms": len(self.platforms), "bays": len(self.bays),
                "canopies": len(self.canopies), "labels": sorted(b["label"] for b in self.bays if b["label"])}


def find(items):
    return _native.bus_find(items, Station, PASSAGE_IDS)


def assemble(items, seeds):
    return _native.bus_assemble(items, seeds, Station, PASSAGE_IDS)


def pad_ring(ring, pad):
    return _native.bus_pad(_xy(ring), float(pad))


def sign_spot(st):
    """(x, y, ux, uy) where the station's name sign stands and the unit vector from the street to it (the board faces
    the street), or None. In place of one of the station's own benches, the one nearest a public street (that bench is
    left out: busfurniture.rows); a station with no bench has no sign (the user 2026-11-09: "the sign instead of a bench
    if there is one; if not, no sign"). Memoised on the station."""
    if not st.streets:
        return None
    if "_sign_spot" in st.__dict__:
        return st._sign_spot
    st._sign_spot = _sign_spot(st)
    return st._sign_spot


def board_spot(st):
    """(x, y, ux, uy) of the departures board (unit vector from the hall to it: the board faces away from the hall), or None:
    in place of the station's bench nearest its hall, other than the name sign's. Memoised on the station."""
    if "_board_spot" in st.__dict__:
        return st._board_spot
    st._board_spot = _board_spot(st)
    return st._board_spot


def _benches(st):
    import busfurniture
    return [(x, y) for kind, x, y, _, _ in busfurniture.furniture(st) if kind == busfurniture.BENCH]


def _board_spot(st):
    if not st.buildings:
        return None
    sign = sign_spot(st)
    return _native.bus_board(_xy(st.buildings[0]["xy"]), _benches(st), sign)


def facade_spot(st):
    """(x, y, ux, uy, length) of the station hall's wall that faces the yard most (the longest edge whose outside, 3 m out,
    is the yard), for the name on the facade: (x, y) the wall's middle 0.12 m out, (ux, uy) the unit vector into the hall,
    or None (no hall, no such wall of 6 m). Memoised on the station."""
    if "_facade_spot" in st.__dict__:
        return st._facade_spot
    st._facade_spot = _facade_spot(st)
    return st._facade_spot


def _facade_spot(st):
    import busyard
    if not st.buildings:
        return None
    return _native.bus_facade(_xy(st.buildings[0]["xy"]), busyard.yard_polys(st))


def sign_both_sides(st, spot):
    return _native.bus_both_sides(spot, [_xy(s["xy"]) for s in (*st.streets, *st.lanes)])


def _sign_spot(st):
    import busstationmesh as bm
    return _native.bus_sign(_benches(st), [c["ring"] for c in st.canopies], [s["xy"] for s in st.streets],
                            bm._paths(st), st.mouths, *bm.walk_field(st))


def bs_lane_blocked(st, pts):
    return np.asarray(lane_blocked(st, pts, pad=0.3, mouths=True), bool) if (st.lanes or st.mouths) else np.zeros(len(pts), bool)


PASSAGE_W_ONEWAY_M, PASSAGE_W_TWOWAY_M = 6.5, 7.5     # a yard lane's drawn width (roads.py: a plain service road is 4 m)
LANE_CORE_M = 3.25          # (kept for callers: the one-way lane's drawn half width; a lane's own is its "core")
MOUTH_R_M = 12.0            # round the point where a lane meets a street nothing stands
PASSAGE_IDS = set()         # road ids (this process) that the road mesh draws as asphalt in a station (roads._map_surface_value)


def path_dist(path, pts):
    p = _xy(pts)
    r = _xy(path)
    if not len(r) and len(p):
        raise ValueError("zero-size array to reduction operation minimum which has no identity")
    return _native.station_path_dist(r, p, False)


def lane_blocked(st, pts, pad=0.0, core=False, mouths=True):
    lanes = [(_xy(ln["xy"]), ln["core"] if core else ln["half"]) for ln in st.lanes]
    return _native.bus_blocked(_xy(pts), lanes, st.mouths if mouths else (), float(pad))


# --- the tile side: which stations a tile sees --------------------------------------------------------------------

ON = os.environ.get("BNE_BUSSTATIONS", "on").strip().lower() not in ("off", "0", "no", "false")
REACH_M = 320.0             # a station this close to a tile is made in it (its pieces that fall in the tile are kept)
PAD_DEG = 0.004             # the cells' padding round the tile's corners (~300 m)


def _cells(frame, box, pad=PAD_DEG):
    import mapcells
    x0, y0, x1, y1 = (float(v) for v in box)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    return mapcells.cells_for_points(la, lo, pad_lat=pad, pad_lon=pad, max_cells=64)


GROUP_PAD_LAT, GROUP_PAD_LON = 0.006, 0.012   # the cells round a group: its layers are read whole, whatever tile asks
GROUP_RADIUS_M = 600.0
MEMO_N = 24
_memo = OrderedDict()
_memo_lock = threading.Lock()


def seed_points(feats):
    cls = feats.attrs.get("class") if feats is not None and len(feats) else None
    return [] if cls is None else _native.bus_seed_points(feats, np.flatnonzero(np.asarray(cls) == "bus_station").tolist())


def _group_stations(md, frame, group):
    """The confident stations round one group of seed points [(lon, lat)], laid out in frame; kept per process (the road
    tile's mesh, skip, furniture, signs and yard all ask for the same group: laying it out was 0.4-1.2 s a call in a
    city, four times a tile)."""
    key = (round(frame.lat0, 6), round(frame.lon0, 6), tuple(group))
    with _memo_lock:
        if key in _memo:
            _memo.move_to_end(key)
            return _memo[key]
    import mapcells
    lons = np.array([g[0] for g in group])
    lats = np.array([g[1] for g in group])
    cells = mapcells.cells_for_points(lats, lons, pad_lat=GROUP_PAD_LAT, pad_lon=GROUP_PAD_LON, max_cells=64)
    names = ("infrastructure", "buildings", "places", "roads", "land_use")
    cds = mapcells.load_cells(md, cells, names)
    layers = {name: [cd.layers.get(name) for cd in cds] for name in names}
    e, n, _ = frame.geodetic_to_enu(lats, lons, np.zeros(len(lats)))
    centre = (float(np.mean(e)), float(np.mean(n)))
    found = [st for st in find(items_from_layers(frame, layers, centre, GROUP_RADIUS_M)) if st.confident()]
    with _memo_lock:
        _memo[key] = found
        while len(_memo) > MEMO_N:
            _memo.popitem(last=False)
    return found


def group_frame(group):
    """A frame of a station group's own, at its first seed point (lon, lat): a group laid out in it is the same for
    every tile that asks (tile_skip), where in each tile's frame it was laid out again for each tile."""
    import geodesy
    return geodesy.EnuFrame(float(group[0][1]), float(group[0][0]), 0.0)


def to_frame(src, dst, xy):
    """Points (n, 2) of frame src in frame dst (through their lat / lon)."""
    xy = np.asarray(xy, np.float64)
    xy = xy.reshape(-1, xy.shape[-1] if xy.ndim else 2)[:, :2]
    if src is dst:
        return xy
    la, lo, _ = src.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    e, n, _ = dst.geodetic_to_enu(la, lo, np.zeros(len(xy)))
    return np.column_stack((np.asarray(e, np.float64), np.asarray(n, np.float64)))


def stations_near(md, frame, box, shared=False):
    """The confident stations within REACH_M of the tile box (x0, y0, x1, y1) of frame, [] almost always: one pass over
    the infrastructure cells for a bus_station feature, and only then the other layers. md: mapdata.MapData. Errors cost
    the stations, not the tile. shared: each group laid out in its own frame (group_frame: the same stations for every
    tile, their geometry in st.group_frame, not in frame; tile_skip, which wants only their claimed ids, busyard.tile_pads
    their yards' lat / lon: a group laid out for each of a city's road tiles was 2.5-6 s of each, Lower Manhattan
    2026-11-11)."""
    if not ON:
        return []
    x0, y0, x1, y1 = (float(v) for v in box)
    try:
        keys = _cells(frame, box)
        import mapcells
        groups = _native.bus_seed_groups([p for cd in mapcells.load_cells(md, keys, ("infrastructure",))
                                          for p in seed_points(cd.layers.get("infrastructure"))])
        found = []
        for g in groups:
            gf = group_frame(g) if shared else frame
            for st in _group_stations(md, gf, tuple(g)):
                if shared:
                    st.group_frame = gf         # (the frame its geometry is in: the same object for every tile)
                found.append((st, gf))
    except Exception as ex:         # (offline, a cell unreadable: no stations here)
        log.warning("bus stations of the tile at %s: %s: %s", tuple(round(float(v)) for v in box[:2]),
                    type(ex).__name__, ex)
        return []
    return _native.bus_near_tile(found, frame, to_frame, (x0, y0, x1, y1), REACH_M)


def tile_skip(frame, tx, ty, md):
    """Ids of the buildings-layer roofs that the stations near road tile (tx, ty) draw themselves (buildings.tile_buildings'
    skip, as gasstations.tile_sites' canopy outlines)."""
    if not ON:
        return frozenset()
    import roads
    return frozenset().union(*(st.claimed for st in stations_near(md, frame, roads.tile_bbox(tx, ty), shared=True)))
