"""Bus stations from the map data (task 020): the station's yard, its building, platforms, bays and roofs, found
from what the map says about it and, where it says little, worked out from the roads and buildings round it.

Two layers, so that the logic is testable without a network:

    items_from_layers(frame, layers) -> [Item]    (adapter: Overture features -> plain items in frame metres)
    assemble(items, hub_xy) -> Station | None     (pure: the station round one hub point, or None when too little
                                                   is known to build one: Station.confidence)

An Item is a dict {"layer", "cls", "sub", "name", "tags", "kind", "xy", ...}: kind is "point", "line" or "polygon"
(the outer ring only), xy an (n, 2) float array in metres of the frame, tags the lower-cased OpenStreetMap
source_tags, plus height / roof_shape / num_floors for buildings and surface for roads and land use. The tests run on
items dumped from real cells (tools/dump_busstation_fixture.py) of six stations.
Pure given its inputs: no randomness (ids are hashes), the same on every tile, anchor and BeamMP peer.
"""
import json
import logging
import math
import os
import threading
from collections import OrderedDict

import numpy as np
import dmath
import dnp

import mapgeom

log = logging.getLogger("earthd.busstations")

KIND = {mapgeom.POINT: "point", mapgeom.LINE: "line", mapgeom.POLYGON: "polygon"}


def tags_of(feats, i):
    """source_tags of feature i as a lower-case dict ({} when it has none)."""
    tags = feats.attrs.get("tags")
    if tags is None:
        return {}
    try:
        values = json.loads(str(tags[i]))
    except (TypeError, ValueError):
        return {}
    return {str(k).strip().lower(): str(v).strip().lower() for k, v in values.items()} if isinstance(values, dict) \
        else {}


def _attr(feats, name, i):
    col = feats.attrs.get(name)
    if col is None:
        return None
    v = col[i]
    if isinstance(v, (float, np.floating)):
        return None if not math.isfinite(float(v)) else float(v)
    s = str(v).strip()
    return None if s in ("", "None", "nan") else s


def _rule(feats, name, i):
    """The value of feature i's range rule `name` that covers most of it ("" when it has none)."""
    table = getattr(feats, "rules", {}).get(name)
    try:
        if table is None or len(table.feature) == 0:
            return ""
        j = table.major(i)
        return "" if j < 0 else str(table.value[j]).strip().lower()
    except Exception:
        return ""


def items_from_layers(frame, layers, hub_xy=(0.0, 0.0), radius=260.0):
    """Items of the features within radius metres of hub_xy, from layers {name: [Features, ...]} (cells of the
    layers "infrastructure", "buildings", "places", "roads", "land_use"); frame: the geodesy.EnuFrame the metres are in.
    A feature that several cells list is taken once (by id)."""
    out, seen = [], set()
    hx, hy = float(hub_xy[0]), float(hub_xy[1])
    # (only the features whose lon / lat box comes within radius of the hub (a box no farther than its points, the
    # degrees taken generously), their rings alone into frame: every feature of a group's cells, each to metres through
    # its whole cell in each road tile's frame (Features.xy_in), was 2.5-6 s of each Lower Manhattan tile, 2026-11-11)
    hla, hlo, _ = frame.enu_to_geodetic(np.array([hx]), np.array([hy]), np.zeros(1))
    hla, hlo = float(np.asarray(hla)[0]), float(np.asarray(hlo)[0])
    rla = 1.02 * radius / 110540.0 + 1e-7
    rlo = 1.02 * radius / (111320.0 * max(dmath.cos(math.radians(min(abs(hla) + rla, 89.0))), 1e-3)) + 1e-7
    for name, cells in layers.items():
        for feats in cells:
            if feats is None or not len(feats):
                continue
            b = feats.lonlat_boxes() if hasattr(feats, "lonlat_boxes") else None
            if b is not None and len(b) == len(feats):
                idx = np.flatnonzero((b[:, 0] <= hlo + rlo) & (b[:, 2] >= hlo - rlo)
                                     & (b[:, 1] <= hla + rla) & (b[:, 3] >= hla - rla)).tolist()
            else:
                idx = range(len(feats))
            # (each feature's frame-free part, its lon / lat ring and attributes, made once and kept with its cell: a
            # station group is laid out again in every road tile's frame, ~60k feature dicts for London's 4 groups in
            # 4 tiles, 2026-11-12)
            memo = _item_memo(feats)
            for i in idx:
                fid = str(feats.ids[i])
                if (name, fid) in seen:
                    continue
                got = memo.get(i, _MISSING)
                if got is _MISSING:
                    got = memo[i] = _item_base(feats, name, i, fid)
                if got is None:
                    continue
                ring, base = got
                e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                xy = np.column_stack((e, n)).astype(np.float64)
                if float(dnp.hypot(xy[:, 0] - hx, xy[:, 1] - hy).min()) > radius:
                    continue
                seen.add((name, fid))
                it = dict(base)
                it["xy"] = xy
                out.append(it)
    return out


_MISSING = object()


def _item_memo(feats):
    """{feature row: _item_base} kept with the cell's Features, new when a column or rule table it reads was replaced
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


def _item_base(feats, name, i, fid):
    """(lon / lat ring, item without its metres: "xy" None in its place) of feature i of a layer's cell, or None
    without a geometry: the part of an item that no frame changes (items_from_layers keeps it with the cell)."""
    kind = KIND.get(int(feats.kind[i]))
    parts = feats.parts(i)
    if kind is None or not parts or not parts[0] or len(parts[0][0]) < 1:
        return None
    ring = np.asarray(parts[0][0], np.float64)
    it = {"layer": name, "id": fid, "cls": (_attr(feats, "class", i) or "").lower(),
          "sub": (_attr(feats, "subtype", i) or "").lower(), "name": _attr(feats, "name", i) or "",
          "tags": tags_of(feats, i), "kind": kind, "xy": None}
    for k in ("height", "min_height", "num_floors", "roof_shape", "surface", "basic_category",
              "taxonomy", "subclass", "roof_color", "roof_material"):
        v = _attr(feats, k, i)
        if v is not None:
            it[k] = v
    if name == "roads":     # a road's surface and level are range rules, not columns
        v = _rule(feats, "surface", i)
        if v and "surface" not in it:
            it["surface"] = v
        lv = _rule(feats, "level", i)
        try:
            it["level"] = float(lv) if lv else 0.0
        except ValueError:
            it["level"] = 0.0
        ow = _rule(feats, "oneway", i)
        try:
            it["oneway"] = float(ow) if ow else 0.0
        except ValueError:
            it["oneway"] = 0.0
        if "subclass" not in it:
            sc = _rule(feats, "subclass", i)
            if sc:
                it["subclass"] = sc
        if it["cls"] in WALK_SPAN_CLASSES:      # (railstations: a footbridge / an underpass is a span of the way)
            spans = _spans(feats, i)
            if spans:
                it["spans"] = spans
    return ring, it


WALK_SPAN_CLASSES = ("footway", "steps", "path", "pedestrian", "corridor", "cycleway")


def _spans(feats, i):
    """[[from, to, "bridge" | "tunnel"]] the parts (fractions of the way) that are a bridge or a tunnel."""
    import roads
    try:
        rngs = roads.level_ranges(feats, i)
    except Exception:
        return []
    return [[round(float(a), 3), round(float(b), 3), k] for a, b, k in (rngs or [])]


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
    if len(ring) >= 2 and float(np.max(np.abs(ring[0] - ring[-1]))) < 1e-6:
        return ring[:-1]
    return ring


def area(ring):
    ring = _open(ring)
    if len(ring) < 3:
        return 0.0
    x, y = ring[:, 0], ring[:, 1]
    return abs(float(dnp.dot(x, np.roll(y, -1)) - dnp.dot(y, np.roll(x, -1)))) * 0.5


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
    """Convex hull (monotone chain), counter-clockwise, (k, 2)."""
    pts = sorted({(round(float(x), 3), round(float(y), 3)) for x, y in np.asarray(xy, np.float64)})
    if len(pts) < 3:
        return np.asarray(pts, np.float64).reshape(-1, 2)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, up = [], []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(up) >= 2 and cross(up[-2], up[-1], p) <= 0:
            up.pop()
        up.append(p)
    return np.asarray(lo[:-1] + up[:-1], np.float64)


# --- what is a station -----------------------------------------------------------------------------------------

GENERIC_NAMES = ("автовокзал", "автостанц", "автовозал", "autobusov", "autobus", "terminál", "terminal", "nádraží",
                 "bus station", "busbahnhof", "dworzec", "otogar", "estación", "estacion", "bus terminal",
                 "coach station", "busstation", "autostazione", "stazione")
CLUSTER_M = 120.0           # seeds (station polygon, node, place) closer than this are one station
POINT_REGION_M = 70.0       # a station with no mapped boundary reaches this far round its hub
BOUNDARY_PAD_M = 12.0       # a mapped boundary's pad: the roof or stop just outside it is the station's
BAY_CLUSTER_MIN = 3         # stops without the station's name count when at least this many stand together
CANOPY_MIN_M2 = 14.0        # a smaller roof is a bus shelter, not a station roof (shelters.py)
CANOPY_H_M = 3.8
ROOF_CLASSES = {"roof", "shelter", "carport", "canopy"}
MIN_CONFIDENCE = 5
STREET_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary", "residential", "unclassified",
                  "living_street"}
STREET_NEAR_M = 160.0       # the streets the name sign can face are this near the hub
SIGN_CLEAR_M = 5.0          # the sign stands this far from a street's centreline at least
SIGN_IN_M = 1.5             # ... and this far inside the yard's edge


def name_matches(name, names):
    """Does a stop's name call it the station: a generic bus-station word, or a word of the station's own name."""
    n = (name or "").lower()
    if not n:
        return False
    if any(g in n for g in GENERIC_NAMES) or "zob" in n.replace("-", " ").split():
        return True
    words = {w.strip('"«»()') for nm in names for w in (nm or "").lower().replace(",", " ").split() if len(w) >= 5}
    return any(w and w in n for w in words)


def bay_label(*sources):
    """The bay's short mark ("A1", "7", "Z2") from the first of local_ref, ref and name that has one: the word before
    a note ("A1 (výstupní)"), up to 4 characters, with a digit or a lone letter. Overture keeps Hradec's A1..J3 in
    the stop's name, not in local_ref (2026-11-06)."""
    for s in sources:
        word = (s or "").strip().split(" ")[0].strip("()[]\"'«»,;")
        if 0 < len(word) <= 4 and (any(c.isdigit() for c in word) or len(word) == 1):
            return word.upper()
    return ""


def _is_seed(it):
    """A mapped bus station: the infrastructure feature or the building tagged so. A carrier's office in the places layer
    (Overture lists Green Line, Saiz, RegioJet as bus_station places) is no station: it only supports one (_is_place)."""
    t = it["tags"]
    if it["layer"] == "infrastructure" and it["cls"] == "bus_station":
        return True
    return it["layer"] == "buildings" and t.get("amenity") == "bus_station"


def _is_place(it):
    return it["layer"] == "places" and it.get("taxonomy") == "bus_station"


def _real_seed(group):
    """Is this group of mapped seeds a station: one with a mapped area, or a bare node that is not named after a carrier
    (a node named "Green Line Coaches" or "Автоперевізник ..." is a ticket office; "ZOB Hamburg" or no name is a station)."""
    if any(s["kind"] == "polygon" and item_area(s) >= 400.0 for s in group):
        return True
    return any(not s["name"] or name_matches(s["name"], ()) for s in group)


def _centre(it):
    return item_centroid(it) if it["kind"] != "point" else it["xy"][0]


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


def _bus_platform(it):
    """A platform for buses (not a railway platform that the map shares)."""
    t = it["tags"]
    if t.get("railway") == "platform" or t.get("train") == "yes" or t.get("tram") == "yes":
        return t.get("bus") == "yes" or t.get("highway") in ("platform", "bus_stop")
    return True


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


def _cluster(seeds):
    """Groups of seeds (single link, CLUSTER_M)."""
    cs = [_centre(s) for s in seeds]
    group = list(range(len(seeds)))

    def find_root(i):
        while group[i] != i:
            group[i] = group[group[i]]
            i = group[i]
        return i
    for i in range(len(seeds)):
        for j in range(i + 1, len(seeds)):
            if float(dnp.hypot(*(cs[i] - cs[j]))) <= CLUSTER_M:
                group[find_root(i)] = find_root(j)
    out = {}
    for i in range(len(seeds)):
        out.setdefault(find_root(i), []).append(seeds[i])
    return list(out.values())


def _fid(items):
    import hashlib
    h = hashlib.blake2b("|".join(sorted(str(i["id"]) for i in items)).encode(), digest_size=6)
    return h.hexdigest()


def find(items):
    """Every station in items, each assembled (build those with Station.confident())."""
    seeds = [i for i in items if _is_seed(i)]
    places = [i for i in items if _is_place(i)]
    out = []
    for g in _cluster(seeds):
        if not _real_seed(g):
            continue
        cs = [_centre(s) for s in g]
        near = [p for p in places if min(float(dnp.hypot(*(_centre(p) - c))) for c in cs) <= CLUSTER_M]
        out.append(assemble(items, g + near))
    return out


def assemble(items, seeds):
    st = Station()
    polys = sorted((s for s in seeds if s["kind"] == "polygon" and item_area(s) >= 400.0),
                   key=lambda s: -item_area(s))
    pts = [_centre(s) for s in seeds]
    names = [s["name"] for s in seeds if s["name"] and (_is_seed(s) or name_matches(s["name"], ()))]
    st.name = names[0] if names else ""
    if polys:
        ring = _open(polys[0]["xy"])
        st.hub = centroid(ring)
        # the "boundary" that is the building's own outline (Vinnytsia West: the station way is the hall) is no yard
        # polygon: decided before the members are taken, so that a roof beside the hall is not outside a boundary
        own_outline = any(it["layer"] == "buildings" and it["kind"] == "polygon" and item_area(it) >= 0.7 * area(ring)
                          and contains(ring, _centre(it)) for it in items)
        if not own_outline:
            st.boundary = ring
            st.surface = polys[0].get("surface", "")
        st.evidence.append(("boundary polygon", 3))
    else:
        st.hub = np.mean(pts, 0)
    if any(s["layer"] == "places" for s in seeds):
        st.evidence.append(("place", 1))
    if names:
        st.evidence.append(("name", 1))

    def in_region(p, pad=BOUNDARY_PAD_M):
        if st.boundary is not None:
            return contains(st.boundary, p) or dist_to_path(st.boundary, p, True) <= pad
        return float(dnp.hypot(*(np.asarray(p) - st.hub))) <= POINT_REGION_M

    taken = {id(s) for s in seeds}
    for it in items:                                    # platforms and canopies
        if id(it) in taken or it["layer"] not in ("infrastructure", "buildings"):
            continue
        if not in_region(_centre(it)):
            continue
        t = it["tags"]
        if it["layer"] == "infrastructure" and it["cls"] == "platform" and it["kind"] != "point" \
                and _bus_platform(it):
            st.platforms.append(it)
        roof = (it["cls"] in ROOF_CLASSES or t.get("building") in ROOF_CLASSES
                or (t.get("covered") == "yes" and it["kind"] == "polygon" and it["layer"] == "infrastructure"
                    and it["cls"] == "platform"))
        if roof and it["layer"] == "buildings":
            st.claimed.add(it["id"])        # the buildings layer must not draw it as a shed as well
        if roof and it["kind"] == "polygon" and item_area(it) >= CANOPY_MIN_M2:
            h = it.get("height")
            st.canopies.append({"ring": _open(it["xy"]), "height": float(h) if h else CANOPY_H_M,
                                "src": it["id"], "area": item_area(it), "mapped": bool(h)})
    # bays: bus_stop points, counted by the boundary, the station's name, or a cluster of them
    stops = [it for it in items if it["layer"] == "infrastructure" and it["cls"] == "bus_stop"
             and it["kind"] == "point" and in_region(it["xy"][0], BOUNDARY_PAD_M + 25.0)]
    near = [s for s in stops if float(dnp.hypot(*(s["xy"][0] - st.hub))) <= POINT_REGION_M]
    cluster_ok = len(near) >= BAY_CLUSTER_MIN
    for s in stops:
        inside = st.boundary is not None and in_region(s["xy"][0], 2.0)
        if not (inside or name_matches(s["name"], names) or (cluster_ok and s in near)):
            continue
        t = s["tags"]
        label = bay_label(t.get("local_ref"), t.get("ref"), s["name"])
        st.bays.append({"xy": s["xy"][0], "label": label, "tags": t, "name": s["name"], "id": s["id"]})
    # buildings: the station's own (transportation / its tags), else the largest one by the hub
    cand = []
    for it in items:
        if it["layer"] != "buildings" or it["kind"] != "polygon" or it["cls"] in ROOF_CLASSES:
            continue
        a = item_area(it)
        c = _centre(it)
        d = float(dnp.hypot(*(c - st.hub)))
        own = it["cls"] == "transportation" or it["sub"] == "transportation" or it["tags"].get("amenity") == "bus_station"
        plain = it["sub"] not in ("residential", "garage") and it["cls"] not in ("garage", "garages", "house",
                                                                                 "apartments", "residential")
        if (own and in_region(c)) or (a >= 150.0 and d <= 45.0 and plain):
            cand.append((0 if own else 1, -a, it))
    cand.sort(key=lambda r: (r[0], r[1]))
    st.buildings = [r[2] for r in cand if r[0] == 0] or [r[2] for r in cand[:1]]
    # a "building" with the buses' stops standing in it is an open hall (Brno Zvonařka: the whole roof is mapped as a
    # building): a roof on columns over the platforms, not a closed box
    halls = [it for it in items if it["layer"] == "buildings" and it["kind"] == "polygon" and item_area(it) >= 200.0
             and it["cls"] not in ROOF_CLASSES and it["id"] not in st.claimed
             and float(dnp.hypot(*(_centre(it) - st.hub))) <= HALL_M
             and (sum(contains(it["xy"], s["xy"]) for s in st.bays) >= 2
                  or (any(contains(it["xy"], s["xy"]) for s in st.bays)
                      and any(contains(it["xy"], _centre(p)) for p in st.platforms)))]
    for b in halls:
        if b in st.buildings:
            st.buildings.remove(b)
        st.claimed.add(b["id"])
        h = b.get("height")
        st.canopies.append({"ring": _open(b["xy"]), "height": float(h) if h and float(h) <= 8.0 else CANOPY_H_M,
                            "src": b["id"], "area": item_area(b), "mapped": bool(h and float(h) <= 8.0)})
    if st.buildings:
        st.evidence.append(("building", 2 if st.buildings[0]["cls"] == "transportation" else 1))
    if st.platforms:
        st.evidence.append(("platforms", 2))
    if st.bays:
        st.evidence.append(("bays", 2))
    if st.canopies:
        st.evidence.append(("canopy", 1))
    st.evidence.append(("seed", 1))
    st.confidence = sum(p for _, p in st.evidence)
    st.roads = [it for it in items if it["layer"] == "roads" and it["kind"] == "line"
                and it["cls"] in ("service", "living_street", "unclassified")
                and dist_to_path(it["xy"], st.hub) <= POINT_REGION_M + 30.0]
    st.streets = [it for it in items if it["layer"] == "roads" and it["kind"] == "line"
                  and it["cls"] in STREET_CLASSES and dist_to_path(it["xy"], st.hub) <= STREET_NEAR_M]
    _lanes(st)
    members = [st.hub] + [b["xy"] for b in st.bays] + [c for b in st.buildings for c in _open(b["xy"])] \
        + [c for p in st.platforms for c in p["xy"]]
    st.obstacles = [_open(it["xy"]) for it in items if it["layer"] == "buildings" and it["kind"] == "polygon"
                    and it["cls"] not in ROOF_CLASSES and it["id"] not in st.claimed and item_area(it) >= 15.0
                    and float(dnp.hypot(*(_centre(it) - st.hub))) <= OBSTACLE_M]
    st.greens = [_open(it["xy"]) for it in items if it["layer"] == "land_use" and it["kind"] == "polygon"
                 and it["cls"] in GREEN_CLASSES and float(dnp.hypot(*(_centre(it) - st.hub))) <= OBSTACLE_M + 60.0]
    st.canopies = _dedupe_canopies(st.canopies)
    if st.boundary is not None:
        st.region = st.boundary
    else:                               # the yard: the members' hull, apron-wide round them (bays on the street too far
        near_bays = [b["xy"] for b in st.bays if float(dnp.hypot(*(b["xy"] - st.hub))) <= YARD_BAY_M]  # are not in it)
        pts = [st.hub] + near_bays + [c for b in st.buildings for c in _open(b["xy"])] \
            + [c for p in st.platforms for c in p["xy"]]
        st.region = pad_ring(hull(np.asarray(pts)), YARD_APRON_M)
    st.fid = _fid(list(seeds))
    return st


GREEN_CLASSES = ("grass", "park", "forest", "meadow", "garden", "wood", "shrub", "flowerbed", "scrub")
HALL_M = 150.0              # a building this near the hub with stops standing in it is a hall
OBSTACLE_M = 140.0          # the buildings this near the hub keep the bays off
YARD_BAY_M = 40.0           # a stop farther from the hub than this does not widen a boundary-less yard
YARD_APRON_M = 5.0          # the yard reaches this far past its members


def pad_ring(ring, pad):
    """A counter-clockwise convex ring moved out by pad metres (each corner along its bisector)."""
    ring = _open(ring)
    n = len(ring)
    if n < 3:
        return ring
    out = []
    for i in range(n):
        a, p, b = ring[i - 1], ring[i], ring[(i + 1) % n]
        e1, e2 = p - a, b - p
        n1 = np.array([e1[1], -e1[0]]) / max(float(dnp.hypot(*e1)), 1e-9)
        n2 = np.array([e2[1], -e2[0]]) / max(float(dnp.hypot(*e2)), 1e-9)
        v = n1 + n2
        norm = float(dnp.hypot(*v))
        out.append(p + v / norm * pad / max(norm / 2.0, 0.35) if norm > 1e-9 else p + n1 * pad)
    return np.asarray(out)


SIGN_LANE_PAD_M = 2.5       # the sign stands this far off a drive lane's corridor and a mouth's circle


def _walkable_at(st, p):
    """Is p on the station's raised walking ground (platforms, the walking zone under the roofs)?"""
    import busstationmesh as bm
    x0, y0, F = bm.walk_field(st)
    if not F.size:
        return False
    i, j = int(round(p[0] - x0)), int(round(p[1] - y0))
    return 0 <= j < F.shape[0] and 0 <= i < F.shape[1] and bool(F[j, i] >= 0.5)


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


def _board_spot(st):
    import busfurniture
    if not st.buildings:
        return None
    hall = _open(np.asarray(st.buildings[0]["xy"], float))
    sign = sign_spot(st)
    best = None
    for g, x, y, _fx, _fy in busfurniture.furniture(st):
        if g != busfurniture.BENCH or (sign is not None and abs(x - sign[0]) < 0.01 and abs(y - sign[1]) < 0.01):
            continue
        d = dist_to_path(hall, (x, y), True)
        if best is None or d < best[0]:
            best = (d, x, y)
    if best is None or best[0] > 40.0:
        return None
    _d, x, y = best
    u = np.array([x, y]) - centroid(hall)
    ln = float(dnp.hypot(*u))
    u = u / ln if ln > 1e-6 else np.array([1.0, 0.0])
    return float(x), float(y), float(u[0]), float(u[1])


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
    import busstationkeep
    if not st.buildings:
        return None
    hall = _open(np.asarray(st.buildings[0]["xy"], float))
    if len(hall) < 3:
        return None
    yard = busstationkeep.Keepout(busyard.yard_polys(st))
    ccw = float(dnp.dot(hall[:, 0], np.roll(hall[:, 1], -1)) - dnp.dot(np.roll(hall[:, 0], -1), hall[:, 1])) >= 0
    best = None
    for a, b in zip(hall, np.roll(hall, -1, axis=0)):
        ln = float(dnp.hypot(*(b - a)))
        if ln < 6.0:
            continue
        t = (b - a) / ln
        out = np.array([t[1], -t[0]]) if ccw else np.array([-t[1], t[0]])
        mid = (a + b) / 2.0
        if not bool(yard.contains((mid + out * 3.0)[None], 0.0)[0]):
            continue
        if best is None or ln > best[0]:
            best = (ln, mid + out * 0.12, -out)
    if best is None:
        return None
    ln, p, u = best
    return float(p[0]), float(p[1]), float(u[0]), float(u[1]), ln


SIGN_ROOF_CLEAR_M = 1.5         # the name sign stands this far from a roof edge when a bench outside the roofs is to be had
SIGN_BACK_REACH_M = 30.0        # a road this near on the sign's far side gives it a second face


def sign_both_sides(st, spot):
    """True when a street or yard lane runs on the far side of the sign too (the user 2026-11-09: "make it double-sided if
    there is a road on both sides"): a point on the ray away from the street, 3..SIGN_BACK_REACH_M out, within 3 m of one."""
    x, y, ux, uy = spot
    k = np.arange(3.0, SIGN_BACK_REACH_M + 0.1, 1.5)
    pts = np.stack([x + ux * k, y + uy * k], axis=1)
    paths = [np.asarray(s_["xy"], float) for s_ in st.streets] + [np.asarray(ln["xy"], float) for ln in st.lanes]
    return any(len(q) >= 2 and bool((path_dist(q, pts) < 3.0).any()) for q in paths)


def _under_roof(st, p):
    for c in st.canopies:
        if contains(c["ring"], p) or dist_to_path(c["ring"], p, True) < SIGN_ROOF_CLEAR_M:
            return True
    return False


SIGN_SIDE_M = (-1.6, -0.8, 0.0, 0.8, 1.6)   # across the board (it is up to ~4 m wide, hung on the pole's middle)


def _sign_spot(st):
    """The bench whose sign board stays on the platform: the pole and the board's middle never in a lane or a mouth (the
    user 2026-11-09: "a sign on the road"), its ends too when a bench allows; the one nearest a street of those."""
    import busfurniture
    import busstationmesh
    benches = [(x, y) for g, x, y, _fx, _fy in busfurniture.furniture(st) if g == busfurniture.BENCH]
    free = [b for b in benches if not _under_roof(st, np.array(b, float))]
    benches = free or benches                   # (a bench clear of the roofs first: its column and beam do not hide the board)
    best = None
    for x, y in benches:
        p = np.array([x, y], float)
        near = None
        for s_ in st.streets:
            path = np.asarray(s_["xy"], float)
            if len(path) < 2:
                continue
            a, b = path[:-1], path[1:]
            ab = b - a
            t = np.clip(((p[0] - a[:, 0]) * ab[:, 0] + (p[1] - a[:, 1]) * ab[:, 1]) / np.maximum((ab * ab).sum(1), 1e-9),
                        0, 1)
            q = a + ab * t[:, None]
            d = dnp.hypot(*(q - p).T)
            k = int(np.argmin(d))
            if near is None or d[k] < near[0]:
                near = (float(d[k]), q[k])
        if near is None:
            continue
        d, r = near
        u = (p - r) / d if d > 1e-6 else np.array([1.0, 0.0])
        side = np.array([-u[1], u[0]])
        pts = np.array([p + side * k for k in SIGN_SIDE_M])
        blocked = bs_lane_blocked(st, pts)
        if blocked[1:4].any():                  # the pole or the middle of the board in a lane: a sign on the road
            continue
        walk = np.asarray(busstationmesh._walk_ok(st, pts), bool)
        key = (int(blocked.sum()), int((~walk).sum()), d)
        if best is None or key < best[0]:
            best = (key, p, u)
    if best is None:
        return None
    _key, p, u = best
    return float(p[0]), float(p[1]), float(u[0]), float(u[1])


def bs_lane_blocked(st, pts):
    return np.asarray(lane_blocked(st, pts, pad=0.3, mouths=True), bool) if (st.lanes or st.mouths) else np.zeros(len(pts), bool)


PASSAGE_W_ONEWAY_M, PASSAGE_W_TWOWAY_M = 6.5, 7.5     # a yard lane's drawn width (roads.py: a plain service road is 4 m)
LANE_HALF_ONEWAY_M, LANE_HALF_TWOWAY_M = 3.4, 4.0     # a corridor's half width: the drawn lane and a little more
LANE_CORE_M = 3.25          # (kept for callers: the one-way lane's drawn half width; a lane's own is its "core")
MOUTH_R_M = 12.0            # round the point where a lane meets a street nothing stands
MOUTH_STREET_M = 8.0        # a lane's end this near a street (level 0) is a mouth
PASSAGE_IDS = set()         # road ids (this process) that the road mesh draws as asphalt in a station (roads._map_surface_value)


def _lanes(st):
    """The yard's drive lanes, mouths and passages (plan 027): the service roads of the yard (those inside a mapped boundary
    or, with none, near the hub), their corridors, and where they meet a public street."""
    for r in st.roads:
        xy = np.asarray(r["xy"], float)
        if len(xy) < 2:
            continue
        if st.boundary is not None and not contains(st.boundary, xy[len(xy) // 2]):
            continue
        st.passages.append(r)
        ow = float(r.get("oneway", 0.0) or 0.0)
        if ow < 0:
            xy = xy[::-1]
        st.lanes.append({"xy": xy, "half": LANE_HALF_ONEWAY_M if ow else LANE_HALF_TWOWAY_M, "oneway": bool(ow),
                         "core": (PASSAGE_W_ONEWAY_M if ow else PASSAGE_W_TWOWAY_M) / 2.0, "id": r["id"]})
    flat = [s_ for s_ in st.streets if not float(s_.get("level", 0.0) or 0.0)]
    for ln in st.lanes:
        for end in (ln["xy"][0], ln["xy"][-1]):
            if flat and min(dist_to_path(s_["xy"], end) for s_ in flat) < MOUTH_STREET_M:
                st.mouths.append((float(end[0]), float(end[1])))
    with _memo_lock:
        PASSAGE_IDS.update(r["id"] for r in st.passages)


def path_dist(path, pts):
    """Distances (k,) from points pts (k, 2) to the polyline path (in chunks: a station is 100 000 points)."""
    pts = np.asarray(pts, float).reshape(-1, 2)
    path = np.asarray(path, float)
    if len(path) == 1:
        return dnp.hypot(*(pts - path[0]).T)
    a, b = path[:-1], path[1:]
    ab = b - a
    ab2 = np.maximum((ab * ab).sum(1), 1e-9)
    out = np.empty(len(pts))
    step = max(1, 400000 // max(len(a), 1))
    for k in range(0, len(pts), step):
        q = pts[k:k + step]
        t = np.clip(((q[:, None, 0] - a[None, :, 0]) * ab[None, :, 0] + (q[:, None, 1] - a[None, :, 1]) * ab[None, :, 1])
                    / ab2[None, :], 0.0, 1.0)
        qx = a[None, :, 0] + t * ab[None, :, 0]
        qy = a[None, :, 1] + t * ab[None, :, 1]
        out[k:k + step] = dnp.hypot(qx - q[:, None, 0], qy - q[:, None, 1]).min(1)
    return out


def lane_blocked(st, pts, pad=0.0, core=False, mouths=True):
    """Which of pts (k, 2) are in the way of the yard's buses: within a corridor (half width + pad; with core, only the
    drawn road's half width + pad) or a mouth's circle. (k,) bool; all False without lanes."""
    pts = np.asarray(pts, float).reshape(-1, 2)
    out = np.zeros(len(pts), bool)
    for ln in st.lanes:
        out |= path_dist(ln["xy"], pts) < (ln["core"] if core else ln["half"]) + pad
    for mx, my in (st.mouths if mouths else ()):
        out |= dnp.hypot(pts[:, 0] - mx, pts[:, 1] - my) < MOUTH_R_M + pad
    return out


def _dedupe_canopies(canopies):
    """One roof where two layers list the same one (a platform area with building=roof, and that building)."""
    out = []
    for c in sorted(canopies, key=lambda c: -c["area"]):
        ctr = centroid(c["ring"])
        if any((contains(o["ring"], ctr) or float(dnp.hypot(*(ctr - centroid(o["ring"])))) < 3.0)
               and c["area"] >= 0.6 * o["area"] for o in out):         # (a concave roof's centroid may lie outside)
            continue
        out.append(c)
    return out


# --- the tile side: which stations a tile sees --------------------------------------------------------------------

ON = os.environ.get("BNE_BUSSTATIONS", "on").strip().lower() not in ("off", "0", "no", "false")
REACH_M = 320.0             # a station this close to a tile is made in it (its pieces that fall in the tile are kept)
PAD_DEG = 0.004             # the cells' padding round the tile's corners (~300 m)


def _cells(frame, box, pad=PAD_DEG):
    import mapcells
    x0, y0, x1, y1 = (float(v) for v in box)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    return mapcells.cells_for_points(la, lo, pad_lat=pad, pad_lon=pad, max_cells=64)


def _has_seed(feats):
    cls = feats.attrs.get("class") if feats is not None and len(feats) else None
    return cls is not None and bool(np.any(np.asarray(cls) == "bus_station"))


SEED_LON_DEG, SEED_LAT_DEG = 0.004, 0.003     # mapped stations this near each other (about 300 m) are one group
GROUP_PAD_LAT, GROUP_PAD_LON = 0.006, 0.012   # the cells round a group: its layers are read whole, whatever tile asks
GROUP_RADIUS_M = 600.0
MEMO_N = 24
_memo = OrderedDict()
_memo_lock = threading.Lock()


def seed_points(feats):
    """(lon, lat) of the bus_station features of an infrastructure layer (the mean of their first part)."""
    if feats is None or not len(feats) or not _has_seed(feats):
        return []
    cls = np.asarray(feats.attrs.get("class"))
    out = []
    for i in np.flatnonzero(cls == "bus_station"):
        parts = feats.parts(int(i))
        if parts and parts[0] and len(parts[0][0]):
            ll = np.asarray(parts[0][0], float)
            out.append((float(ll[:, 0].mean()), float(ll[:, 1].mean())))
    return out


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
        seeds = sorted({(round(a, 5), round(b, 5)) for cd in mapcells.load_cells(md, keys, ("infrastructure",))
                        for a, b in seed_points(cd.layers.get("infrastructure"))})
        if not seeds:
            return []
        groups = []
        for s in seeds:
            for g in groups:
                if abs(g[0][0] - s[0]) < SEED_LON_DEG and abs(g[0][1] - s[1]) < SEED_LAT_DEG:
                    g.append(s)
                    break
            else:
                groups.append([s])
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
    out, seen = [], set()
    for st, gf in found:
        if st.fid in seen:
            continue
        seen.add(st.fid)
        reg = to_frame(gf, frame, st.region)
        rx0, ry0 = reg.min(0)
        rx1, ry1 = reg.max(0)
        if rx0 - REACH_M <= x1 and rx1 + REACH_M >= x0 and ry0 - REACH_M <= y1 and ry1 + REACH_M >= y0:
            out.append(st)
    return out


def tile_skip(frame, tx, ty, md):
    """Ids of the buildings-layer roofs that the stations near road tile (tx, ty) draw themselves (buildings.tile_buildings'
    skip, as gasstations.tile_sites' canopy outlines)."""
    if not ON:
        return frozenset()
    import roads
    return frozenset().union(*(st.claimed for st in stations_near(md, frame, roads.tile_bbox(tx, ty), shared=True)))
