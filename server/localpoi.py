"""Local points of interest outside the parks: forest picnic tables, picnic huts, fire pits.

picnic.py draws a mapped table only inside a park polygon (and a grill beside it). Most of the world's tables stand
elsewhere: on a forest track's junction, at a viewpoint, in a clearing (OpenStreetMap leisure=picnic_table, 31 within
6 km of Police nad Metují, none in a park). This module draws what picnic.py leaves out, from the same features:

  leisure=picnic_table                      a forest table with its benches (lp_table)
  tourism=picnic_site (a point)             one table; a polygon up to SITE_AREA_MAX_M2 gets a few; none when a
                                            mapped table is within SITE_NEAR_M
  amenity=shelter, not a building           a picnic hut (lp_altan), unless a bus stop is within BUS_STOP_M
  leisure=firepit                           a stone ring with two logs (lp_firepit)
  historic=wayside_cross                    a roadside cross: sandstone, wooden or metal by the country's habit (or
                                            lp_cross_*), turned to face the nearest road (stage C)
  historic=wayside_shrine                   a "Bozi muka" column (lp_shrine_column)
  historic=memorial / monument / stone      a stele, an obelisk, a boulder with a plaque, a saint's statue or a low stone
                                            cross, by the OpenStreetMap description's words (stage F)
  information=guidepost (Overture)          the galvanised post of a hiking guidepost (lp_gp); its plates and arrows are the
                                            sign tile's (localpoisign.py, stage D). guideposts() is shared by both.
  amenity=bbq outside a park                picnic.py's grill

Every spot is left out when it lies inside a drawn building, and moved up to SHIFT_MAX_M off a carriageway (furniture.
RoadClearance, the collision boxes below) or dropped. BNE_LOCALPOI=off turns the module off.
"""
import hashlib
import json
import logging
import math
import os
import re
import unicodedata

import numpy as np

import mapcells
import mapgeom
import picnic

log = logging.getLogger("earthd.localpoi")

ON = os.environ.get("BNE_LOCALPOI", "on").lower() not in ("off", "0", "false")

G_TABLE, G_ALTAN, G_FIREPIT = "lp_table", "lp_altan", "lp_firepit"
G_CROSS_STONE, G_CROSS_WOOD, G_CROSS_METAL, G_SHRINE = "lp_cross_stone", "lp_cross_wood", "lp_cross_metal", "lp_shrine"
G_GP = "lp_gp"
G_STELE, G_OBELISK, G_BOULDER, G_STATUE, G_STONE_CROSS = "lp_stele", "lp_obelisk", "lp_boulder", "lp_statue", "lp_stone_cross"
HEIGHT = {G_TABLE: 0.77, G_ALTAN: 3.2, G_FIREPIT: 0.3, G_CROSS_STONE: 3.76, G_CROSS_WOOD: 3.78, G_CROSS_METAL: 2.65,
          G_SHRINE: 3.35, G_GP: 2.64, G_STELE: 1.92, G_OBELISK: 3.5, G_BOULDER: 1.3, G_STATUE: 3.75,
          G_STONE_CROSS: 1.05}      # the catalog heights (trees.lua), scale 1
MEMORIAL_GROUPS = (G_STELE, G_OBELISK, G_BOULDER, G_STATUE, G_STONE_CROSS)
FACING = {G_CROSS_STONE, G_CROSS_WOOD, G_CROSS_METAL, G_SHRINE, *MEMORIAL_GROUPS}   # models whose front (local -Y) faces the road
# Which cross a country puts up (no material tag in the OpenStreetMap rows: Czech wayside crosses 70 % sandstone, Polish ones
# mostly wood and iron, Ukraine's iron): (stone, wood, metal) shares, in this order
CROSS_SHARES = {
    "CZ": (0.70, 0.20, 0.10), "SK": (0.70, 0.20, 0.10), "AT": (0.65, 0.25, 0.10), "DE": (0.60, 0.25, 0.15),
    "HU": (0.60, 0.20, 0.20), "SI": (0.55, 0.30, 0.15), "HR": (0.55, 0.20, 0.25),
    "PL": (0.20, 0.50, 0.30), "UA": (0.10, 0.30, 0.60), "BY": (0.10, 0.30, 0.60), "LT": (0.10, 0.60, 0.30),
    "LV": (0.10, 0.60, 0.30),
}
CROSS_SHARES_DEFAULT = (0.55, 0.25, 0.20)
CROSS_GROUPS = (G_CROSS_STONE, G_CROSS_WOOD, G_CROSS_METAL)
FACE_ROAD_M = 30.0              # a cross within this of a carriageway faces it

# Colmesh footprints of the models (tools/make_localpoi_models.py), at catalog height, for RoadClearance
COLLISION_BOXES = {
    G_TABLE: (0.77, ((-0.90, -0.75, 0.0, 0.90, 0.75, 0.77),)),
    G_ALTAN: (3.2, ((-1.95, -2.15, 0.0, 1.95, 2.15, 3.2),)),
    G_FIREPIT: (0.3, ((-0.78, -0.78, 0.0, 0.78, 0.78, 0.3), (-0.6, -1.6, 0.0, 0.6, 1.6, 0.3))),
    G_CROSS_STONE: (3.76, ((-0.65, -0.65, -0.2, 0.65, 0.65, 1.9), (-0.35, -0.05, 1.9, 0.35, 0.05, 3.76))),
    G_CROSS_WOOD: (3.78, ((-0.1, -0.1, 0.0, 0.1, 0.1, 3.5), (-0.75, -0.08, 2.62, 0.75, 0.08, 2.8))),
    G_CROSS_METAL: (2.65, ((-0.28, -0.28, -0.2, 0.28, 0.28, 0.55), (-0.45, -0.05, 0.55, 0.45, 0.05, 2.65))),
    G_SHRINE: (3.35, ((-0.55, -0.55, -0.2, 0.55, 0.55, 0.15), (-0.36, -0.36, 0.15, 0.36, 0.36, 3.35))),
    G_GP: (2.64, ((-0.04, -0.04, 0.0, 0.04, 0.04, 2.64),)),
    G_STELE: (1.92, ((-0.75, -0.40, -0.1, 0.75, 0.40, 1.92),)),
    G_OBELISK: (3.5, ((-0.65, -0.65, -0.1, 0.65, 0.65, 3.5),)),
    G_BOULDER: (1.3, ((-0.65, -0.45, 0.0, 0.65, 0.45, 1.3),)),
    G_STATUE: (3.75, ((-0.55, -0.55, -0.1, 0.55, 0.55, 2.03), (-0.26, -0.26, 2.03, 0.26, 0.26, 3.75))),
    G_STONE_CROSS: (1.05, ((-0.36, -0.22, -0.1, 0.36, 0.22, 1.05),)),
}

SITE_AREA_MAX_M2 = 4000.0
SITE_MAX = 3                    # tables in a picnic polygon
SITE_NEAR_M = 20.0              # a picnic_site point is dropped when a mapped table is this close
BUS_STOP_M = 30.0               # a shelter this close to a bus stop is the stop's (shelters.py)
SHELTER_AREA_MAX_M2 = 120.0     # a bigger shelter polygon is a building or a pavilion of its own
TABLE_NEAR_M = 3.0              # two tables this close are one
HUT_TABLE_M = 3.0               # a table inside a hut is the hut's (its model has one)
CROSS_NEAR_M = 4.0              # two crosses this close are one (a node and its way)
MEMORIAL_AREA_MAX_M2 = 600.0    # a memorial polygon bigger than this is a place (a cemetery, a plaza), not a monument
MEMORIAL_KINDS = ("memorial", "monument", "stone")
SHIFT_MAX_M = 3.0
SHIFT_STEP_M = 1.0
BUS_CLASSES = {"bus_stop", "stop_position"}


def enabled():
    return ON


def _kind(feats, i):
    """'table' / 'site' / 'grill' (picnic.marked), 'firepit', 'shelter', or None for feature i."""
    got = picnic.marked(feats, i)
    if got is not None:
        return got
    cls = picnic._cls(feats, i)
    tags = picnic._tags(feats, i)
    if cls == "firepit" or tags.get("leisure") == "firepit":
        return "firepit"
    if cls == "shelter" or tags.get("amenity") == "shelter":
        return "shelter"
    for what in ("wayside_cross", "wayside_shrine", *MEMORIAL_KINDS):
        if cls == what or tags.get("historic") == what:
            return what
    return None


def _layer_marks(feats):
    out = []
    for i in range(len(feats)):
        kind = _kind(feats, i)
        if kind is not None and int(feats.kind[i]) in (mapgeom.POINT, mapgeom.POLYGON):
            out.append((i, kind))
    return out


def _bus_stops(loaded, frame):
    """xy of the bus stops (Overture transit/bus_stop and stop_position points) in these layers."""
    pts = []
    for feats in loaded:
        if feats.attrs.get("class") is None:
            continue
        for i in range(len(feats)):
            if picnic._cls(feats, i) not in BUS_CLASSES or int(feats.kind[i]) != mapgeom.POINT:
                continue
            rings = picnic._outers(feats, frame, i)
            if rings:
                pts.append((float(rings[0][0, 0]), float(rings[0][0, 1])))
    return np.array(pts, np.float64).reshape(-1, 2)


def _near(points, x, y, dist):
    if not len(points):
        return False
    return bool(((points[:, 0] - x) ** 2 + (points[:, 1] - y) ** 2 < dist * dist).any())


class Found(list):
    """candidates()' rows; .text is the description of the features that have one, by id."""

    def __init__(self, *args):
        super().__init__(*args)
        self.text = {}


def _fold(text):
    """Lower case without accents (so "Padlým" and "padlym" meet)."""
    return "".join(c for c in unicodedata.normalize("NFD", str(text or "").lower()) if unicodedata.category(c) != "Mn")


MEMORIAL_WORDS = (
    # (group, words found in the description or name), the first group with a hit wins
    (G_STATUE, ("socha", "sousosi", "statue", "statua", "figur", "nepomuk", "sv.", "svat", "saint", "skulptur",
                "rzezba", "figura", "pamiatnik sv", "памятник святому", "статуя", "скульптур")),
    (G_OBELISK, ("obelisk", "obelisque", "обелиск", "обеліск")),
    (G_BOULDER, ("deska", "tabule", "plaque", "tablica", "gedenktafel", "valoun", "kamen", "stone", "boulder",
                 "tabliczka", "мемориальн", "меморіальн", "дошка")),
    (G_STELE, ("padl", "valk", "vojn", "obet", "war", "fallen", "gefallen", "kriegs", "ofiar", "poleg", "wojn",
               "загиб", "жертв", "вoин", "воїн", "війн")),
)


def memorial_group(fid, kind, description=""):
    """The model of a mapped memorial: by the description's words (statue, obelisk, plaque, war memorial), else a stele
    or an obelisk by the id (a monument: an obelisk more often); historic=stone is a penance cross when it says so, else a
    boulder."""
    text = _fold(description)
    if kind == "stone":
        return G_STONE_CROSS if ("kriz" in text or "cross" in text or "krzyz" in text or "kreuz" in text) else G_BOULDER
    for group, words in MEMORIAL_WORDS:
        if any(w in text for w in words):
            return group
    u = _share(fid, "memorial")
    return G_OBELISK if u < (0.55 if kind == "monument" else 0.3) else G_STELE


def candidates(frame, infra_fn, land_fn, box, osm_fn=None):
    """(kind, id, x, y, ring or None, area) of the mapped spots in box that picnic.py leaves out (not in a park for a
    table, site, grill or firepit; a shelter anywhere), plus the bus stops' xy and the parks' rings. Deduplicated the
    way picnic.rows is (a point is whole in every cell; a polygon is clipped per cell)."""
    x0, y0, x1, y1 = (float(v) for v in box)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    try:
        keys = mapcells.cells_for_points(la, lo, pad_lat=0.00005, pad_lon=0.00005, max_cells=64)
    except (ValueError, OverflowError):
        return [], np.zeros((0, 2)), [], []
    loaded = picnic._load(land_fn, keys) + picnic._load(infra_fn, keys)
    if osm_fn is not None:
        try:
            loaded.extend(f for f in (osm_fn(keys) or ()) if f is not None and len(f))
        except Exception as ex:
            log.warning("local pois: %s: %s", type(ex).__name__, ex)
    parks, seen_park = [], set()
    for feats in loaded:
        if feats.attrs.get("class") is None and feats.attrs.get("tags") is None:
            continue
        for key, ring in picnic._kept(feats, "parks", frame, lambda feats=feats: picnic._layer_parks(frame, feats)):
            if key not in seen_park:
                seen_park.add(key)
                parks.append(ring)
    found, seen = Found(), set()
    for feats in loaded:
        if feats.attrs.get("class") is None and feats.attrs.get("tags") is None:
            continue
        marks = picnic._kept(feats, "lp_marks", None, lambda feats=feats: _layer_marks(feats))
        if not marks:
            continue
        boxes = feats.boxes(frame)
        for i, kind in marks:
            b = boxes[i]
            if b[2] < x0 or b[0] >= x1 or b[3] < y0 or b[1] >= y1:
                continue
            fid = str(feats.ids[i])
            pkind = int(feats.kind[i])
            key = fid if pkind == mapgeom.POINT else (
                fid, round(float(b[0]), 1), round(float(b[1]), 1), round(float(b[2]), 1), round(float(b[3]), 1))
            if key in seen:
                continue
            rings = picnic._outers(feats, frame, i)
            if not rings:
                continue
            seen.add(key)
            if pkind == mapgeom.POINT:
                x, y, area, ring = float(rings[0][0, 0]), float(rings[0][0, 1]), 0.0, None
            else:
                ring = picnic._open(rings[0])
                if len(ring) < 3:
                    continue
                area = picnic._area(ring)
                c = ring.mean(0)
                x, y = float(c[0]), float(c[1])
            found.append((kind, fid, x, y, ring, area))
            text = picnic._tags(feats, i).get("description", "")
            if text:
                found.text[fid] = text
    return found, _bus_stops(loaded, frame), parks, keys


def _share(fid, what):
    """A stable number in [0, 1) of a feature id."""
    digest = hashlib.blake2b(f"{fid}\0{what}".encode(), digest_size=4).digest()
    return int.from_bytes(digest, "little") / 4294967296.0


def cross_group(fid, country):
    """The cross model a mapped cross is drawn as: the country's shares, picked by the id."""
    shares = CROSS_SHARES.get(str(country or "").upper(), CROSS_SHARES_DEFAULT)
    u = _share(fid, "cross")
    acc = 0.0
    for group, share in zip(CROSS_GROUPS, shares):
        acc += share
        if u < acc:
            return group
    return CROSS_GROUPS[0]


def plan(found, buses, parks, box, country=None):
    """[(group, fid, x, y, yaw)] to draw from candidates(): the placement rules, before the road clearance."""
    items = []
    for kind, fid, x, y, ring, area in found:
        if kind == "table" and area > picnic.TABLE_POLY_MAX:            # a polygon table that big is a picnic area
            kind = "site" if area <= SITE_AREA_MAX_M2 else None
        if kind is not None:
            items.append((kind, fid, x, y, ring, area))
    out, huts = [], []
    for kind, fid, x, y, ring, area in items:
        if kind == "shelter" and picnic._in_box(x, y, box) and area <= SHELTER_AREA_MAX_M2                 and not _near(buses, x, y, BUS_STOP_M):
            huts.append((x, y))
            out.append((G_ALTAN, fid, x, y, picnic._yaw_of(fid, "altan")))
    hut_pts = np.array(huts, np.float64).reshape(-1, 2)
    tables = []                                   # mapped tables outside parks (their xy), for the picnic sites
    for kind, fid, x, y, ring, area in items:
        if kind not in ("table", "firepit", "grill") or not picnic._in_box(x, y, box) or picnic._in_park(parks, x, y):
            continue
        if kind == "table":
            if _near(hut_pts, x, y, HUT_TABLE_M):
                continue
            tables.append((x, y))
            out.append((G_TABLE, fid, x, y, picnic._yaw_of(fid, "table")))
        elif kind == "firepit":
            out.append((G_FIREPIT, fid, x, y, picnic._yaw_of(fid, "firepit")))
        else:
            out.extend((group, fid, gx, gy, gyaw) for group, gx, gy, gyaw in picnic._furniture(fid, x, y, False, True))
    texts = getattr(found, "text", {})
    for kind, fid, x, y, ring, area in items:                   # roadside crosses, shrines and memorials, in parks too
        if not picnic._in_box(x, y, box):
            continue
        if kind in ("wayside_cross", "wayside_shrine"):
            group = G_SHRINE if kind == "wayside_shrine" else cross_group(fid, country)
            out.append((group, fid, x, y, picnic._yaw_of(fid, "cross")))
        elif kind in MEMORIAL_KINDS and area <= MEMORIAL_AREA_MAX_M2:
            out.append((memorial_group(fid, kind, texts.get(fid, "")), fid, x, y, picnic._yaw_of(fid, "memorial")))
    mapped = np.array(tables, np.float64).reshape(-1, 2)
    for kind, fid, x, y, ring, area in items:
        if kind != "site":
            continue
        if ring is None:
            spots = [(x, y)] if picnic._in_box(x, y, box) and not _near(mapped, x, y, SITE_NEAR_M) else []
        elif 0.0 < area <= SITE_AREA_MAX_M2:
            spots = [p for p in picnic._scatter(ring, box, picnic._phase(fid), SITE_MAX)
                     if not _near(mapped, p[0], p[1], picnic.NEAR_TABLE_M)]
        else:
            spots = []
        for sx, sy in spots:
            if not picnic._in_park(parks, sx, sy) and not _near(hut_pts, sx, sy, HUT_TABLE_M):
                out.append((G_TABLE, fid, sx, sy, picnic._yaw_of(fid, "table")))
    # a table is not drawn twice (two mapped points, or a point and its polygon)
    kept, taken, crosses = [], [], []
    for row in out:
        if row[0] == G_TABLE:
            if any((row[2] - tx) ** 2 + (row[3] - ty) ** 2 < TABLE_NEAR_M ** 2 for tx, ty in taken):
                continue
            taken.append((row[2], row[3]))
        elif row[0] in FACING:
            if any((row[2] - cx) ** 2 + (row[3] - cy) ** 2 < CROSS_NEAR_M ** 2 for cx, cy in crosses):
                continue
            crosses.append((row[2], row[3]))
        kept.append(row)
    return kept


# ------------------------------------------------------------------------------------ guideposts, boards and ways

def _layer_information(feats):
    """[(row, kind)] of the layer's information points: 'guidepost', 'board' or 'map' (Overture infrastructure, source tags)."""
    out = []
    if feats.attrs.get("class") is None:
        return out
    for i in range(len(feats)):
        if picnic._cls(feats, i) != "information" or int(feats.kind[i]) != mapgeom.POINT:
            continue
        kind = picnic._tags(feats, i).get("information", "")
        if kind in ("guidepost", "board", "map"):
            out.append((i, kind))
    return out


def guideposts(frame, infra_fn, box, pad_m=0.0):
    """The information points (hiking guideposts, boards, maps) whose spot is within pad_m of box: dicts {fid, kind, x, y,
    name, ele, ref, hiking, bicycle, board_type, description, direction}, in id order. The same list for the road tile (the
    post) and the sign tile (its plates), so the two agree."""
    if infra_fn is None:
        return []
    x0, y0, x1, y1 = (float(v) for v in box)
    x0, y0, x1, y1 = x0 - pad_m, y0 - pad_m, x1 + pad_m, y1 + pad_m
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    try:
        keys = mapcells.cells_for_points(la, lo, pad_lat=0.00005, pad_lon=0.00005, max_cells=96)
    except (ValueError, OverflowError):
        return []
    out, seen = [], set()
    for feats in picnic._load(infra_fn, keys):
        marks = picnic._kept(feats, "lp_info", None, lambda feats=feats: _layer_information(feats))
        if not marks:
            continue
        names = feats.attrs.get("name")
        for i, kind in marks:
            fid = str(feats.ids[i])
            if fid in seen:
                continue
            rings = picnic._outers(feats, frame, i)
            if not rings:
                continue
            x, y = float(rings[0][0, 0]), float(rings[0][0, 1])
            if not (x0 <= x < x1 and y0 <= y < y1):
                continue
            seen.add(fid)
            tags = picnic._tags(feats, i)
            name = str(names[i]).strip() if names is not None else ""
            out.append({"fid": fid, "kind": kind, "x": x, "y": y, "name": "" if name.lower() in ("none", "nan") else name,
                        "ele": tags.get("ele", ""), "ref": tags.get("ref", ""), "hiking": tags.get("hiking") == "yes",
                        "bicycle": tags.get("bicycle") == "yes", "board_type": tags.get("board_type", ""),
                        "description": tags.get("description", ""), "direction": tags.get("direction", ""),
                        "operator": tags.get("operator", "")})
    out.sort(key=lambda g: g["fid"])
    return out


class Ways:
    """Road and path segments for facing things: .a / .b are (n, 3) endpoints in the tile frame (z unused)."""

    def __init__(self, a, b):
        self.a, self.b = a, b


def ways_near(frame, roads_fn, box, pad_m=60.0):
    """Every road and path segment (the roads layer, all classes) within pad_m of box, as Ways, or None."""
    if roads_fn is None:
        return None
    x0, y0, x1, y1 = (float(v) for v in box)
    x0, y0, x1, y1 = x0 - pad_m, y0 - pad_m, x1 + pad_m, y1 + pad_m
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    try:
        keys = mapcells.cells_for_points(la, lo, pad_lat=0.00005, pad_lon=0.00005, max_cells=64)
    except (ValueError, OverflowError):
        return None
    aa, bb = [], []
    for feats in picnic._load(roads_fn, keys):
        boxes = feats.boxes(frame)
        for i in range(len(feats)):
            b = boxes[i]
            if b[2] < x0 or b[0] >= x1 or b[3] < y0 or b[1] >= y1 or int(feats.kind[i]) != mapgeom.LINE:
                continue
            for part in feats.parts(i):
                if not part or len(part[0]) < 2:
                    continue
                e, n = mapgeom.enu_xy(feats, frame, np.asarray(part[0], np.float64))
                xy = np.column_stack((e, n, np.zeros(len(e))))
                aa.append(xy[:-1])
                bb.append(xy[1:])
    if not aa:
        return None
    return Ways(np.vstack(aa), np.vstack(bb))


def toward_way(ways, x, y, radius=FACE_ROAD_M):
    """The unit vector (dx, dy) from (x, y) to the nearest road or path segment within radius, or None."""
    if ways is None or not len(ways.a):
        return None
    p = np.array([x, y])
    a2, b2 = np.asarray(ways.a)[:, :2], np.asarray(ways.b)[:, :2]
    near = ((np.minimum(a2[:, 0], b2[:, 0]) - radius < x) & (np.maximum(a2[:, 0], b2[:, 0]) + radius > x)
            & (np.minimum(a2[:, 1], b2[:, 1]) - radius < y) & (np.maximum(a2[:, 1], b2[:, 1]) + radius > y))
    if not near.any():
        return None
    a2, b2 = a2[near], b2[near]
    d = b2 - a2
    t = np.clip(((p - a2) * d).sum(1) / np.maximum((d * d).sum(1), 1e-9), 0.0, 1.0)
    q = a2 + d * t[:, None]
    dist2 = ((q - p) ** 2).sum(1)
    k = int(np.argmin(dist2))
    if dist2[k] > radius * radius or dist2[k] < 1e-6:
        return None
    v = q[k] - p
    return float(v[0] / math.sqrt(dist2[k])), float(v[1] / math.sqrt(dist2[k]))


def guidepost_rows(posts, box, elev, frame, covers=()):
    """Forest rows of the guideposts' tubes whose spot is in box (not inside a drawn building)."""
    spots = [g for g in posts if g["kind"] == "guidepost" and picnic._in_box(g["x"], g["y"], box)
             and not (covers and picnic._covered(covers, g["x"], g["y"]))]
    if not spots:
        return []
    xy = np.array([[g["x"], g["y"]] for g in spots], np.float64)
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    return [[G_GP, round(g["x"], 2), round(g["y"], 2), round(float(zz), 2), 0.0, 1.0, HEIGHT[G_GP], 0.0]
            for g, zz in zip(spots, np.asarray(z, np.float64))]


def _offsets():
    out = [(0.0, 0.0)]
    r = SHIFT_STEP_M
    while r <= SHIFT_MAX_M + 1e-9:
        for k in range(8):
            a = math.pi * k / 4
            out.append((r * math.cos(a), r * math.sin(a)))
        r += SHIFT_STEP_M
    return out


def face_road(clearance, x, y):
    """The yaw (deg) that turns a model's front (local -Y) to the nearest carriageway within FACE_ROAD_M of (x, y), or None.
    clearance.a / .b are the carriageway segments (RoadClearance)."""
    a = getattr(clearance, "a", None)
    b = getattr(clearance, "b", None)
    if a is None or not len(a):
        return None
    p = np.array([x, y])
    a2, b2 = np.asarray(a)[:, :2], np.asarray(b)[:, :2]
    near = ((np.minimum(a2[:, 0], b2[:, 0]) - FACE_ROAD_M < x) & (np.maximum(a2[:, 0], b2[:, 0]) + FACE_ROAD_M > x)
            & (np.minimum(a2[:, 1], b2[:, 1]) - FACE_ROAD_M < y) & (np.maximum(a2[:, 1], b2[:, 1]) + FACE_ROAD_M > y))
    if not near.any():
        return None
    a2, b2 = a2[near], b2[near]
    d = b2 - a2
    L2 = np.maximum((d * d).sum(1), 1e-9)
    t = np.clip(((p - a2) * d).sum(1) / L2, 0.0, 1.0)
    q = a2 + d * t[:, None]
    dist2 = ((q - p) ** 2).sum(1)
    k = int(np.argmin(dist2))
    if dist2[k] > FACE_ROAD_M ** 2 or dist2[k] < 1e-6:
        return None
    dx, dy = q[k] - p
    return math.degrees(math.atan2(dx, -dy)) % 360.0


def settle(spots, elev, frame, clearance=None, covers=()):
    """Forest rows [group, x, y, z, yaw, scale, height, rank] of the planned spots, each moved to the nearest offset
    (up to SHIFT_MAX_M) where it stands clear of every carriageway, and dropped when none does or it is inside a drawn
    building."""
    if not spots:
        return []
    xy = np.array([[s[2], s[3]] for s in spots], np.float64)
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    out = []
    offsets = _offsets()
    for (group, fid, x, y, yaw), zz in zip(spots, np.asarray(z, np.float64)):
        for dx, dy in offsets:
            px, py = x + dx, y + dy
            if covers and picnic._covered(covers, px, py):
                continue
            turn = face_road(clearance, px, py) if group in FACING and clearance is not None else None
            row = [group, round(px, 2), round(py, 2), round(float(zz), 2),
                   round(yaw if turn is None else turn, 1) % 360.0, 1.0, HEIGHT.get(group, 1.0), 0.0]
            if clearance is None or group not in COLLISION_BOXES or clearance.clear(row):
                out.append(row)
                break
    return out


def rows(frame, infra_fn, land_fn, box, elev, osm_fn=None, bld_fn=None, clearance=None, country=None):
    """Forest rows of the local POIs in box (see the module). infra_fn / land_fn(key) are the map cells' layers,
    osm_fn(keys) the OpenStreetMap picnic features (picnicsource.Source.cells), bld_fn(key) the buildings."""
    if not ON or (infra_fn is None and land_fn is None and osm_fn is None):
        return []
    found, buses, parks, keys = candidates(frame, infra_fn, land_fn, box, osm_fn)
    posts = guideposts(frame, infra_fn, box)
    if not found and not posts:
        return []
    covers = picnic._covers(frame, bld_fn, keys) if bld_fn is not None and keys else []
    out = guidepost_rows(posts, box, elev, frame, covers)
    spots = plan(found, buses, parks, box, country) if found else []
    return out + (settle(spots, elev, frame, clearance, covers) if spots else [])
