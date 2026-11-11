"""Park alleys, footpaths and cycleways as drawn ground, with the lamps, benches and bins that stand along them.

Until now the road tiles drew no footway, path, cycleway or steps at all: a park was the blurred aerial photo and
the trees stood on its paths. The data is already in every map cell (Overture's transportation segments of these
classes, OpenStreetMap's surface / lit / width tags joined by osmtags.py, land_use parks, infrastructure lamps and
benches), so nothing new is downloaded. Pure functions over Features, no network (the photo's regional tiles are read
from the disk only, as photosurface does).

    collect(md, frame, box)            -> Data    paths, drive roads, parks and mapped furniture round a road tile
    classify(data)                     -> [Walk]  each path's role, width, surface (tag, then the photo, then the role's
                                                  usual), lit; crossings and the sidewalks the road mesh draws are dropped
    mesh(frame, box, walks, ...)       -> mesh    the strips of the tile (plazas.mesh's tuple), merged by plazas.merge
    rows(frame, md, box, elev)         -> [row]   forest rows of the lamps, benches and bins
    corridor(f)                        -> (idx, half width m) of the roads layer's paths, for vegetation's clear corridor

Roles: alley (a park's main path: lit, wide, named or paved), park (the other paths in a park), walk (a footway outside
parks), trail (a path outside parks), cycleway, sidewalk (a sidewalk mapped on its own where its road has none:
sidewalk=separate), steps. The width is the mapped one, else the role's. BNE_PARKS=off turns the module off.
"""
import hashlib
import math
import os
from collections import Counter as C_Counter

import numpy as np

import mapgeom

ON = os.environ.get("BNE_PARKS", "on").lower() not in ("off", "0", "false")
def _upstream_paths():
    """Upstream's mapped paths (GitHub #971, mappedpaths.py) paint and grade the footways, paths and cycleways as terrain
    themselves: where that module is here, gravel and bare-ground strips are theirs (their win on overlap), ours stay for
    the paving, asphalt and the red cycleway, and for the benches, lamps and bins. BNE_PARKS_LOOSE=on|off overrides."""
    v = os.environ.get("BNE_PARKS_LOOSE", "").lower()
    if v in ("on", "1", "true"):
        return False
    if v in ("off", "0", "false"):
        return True
    import importlib.util
    return importlib.util.find_spec("mappedpaths") is not None


UPSTREAM_LOOSE = _upstream_paths()      # True: no strip or pad of ours on a gravel or bare path
VERSION = "walkways-v1"                 # (in the vegetation cache key: its corridors follow these paths)

WALK_CLASSES = {"footway", "path", "cycleway", "bridleway", "steps", "pedestrian"}   # (pedestrian LINES: a park's
                                                    # axis or a promenade; pedestrian areas are plazas.py's)
DRIVE_HALF_M = {"motorway": 7.0, "trunk": 6.0, "primary": 5.5, "secondary": 5.0, "tertiary": 4.5, "residential": 4.0,
                "living_street": 3.0, "unclassified": 3.5, "service": 3.0, "track": 2.0}
PARK_CLASSES = {"park", "recreation_ground", "village_green", "garden", "cemetery"}
LONG_CROSSWALK_M = 100.0                # Overture's subclass crosswalk on a way this long is a footway (Vinnytsia: the south
                                        # half of the Solar System alley, 639 m, and Zamostyanska's median alley, 749 m)
PED_PARK_W = 5.0                        # m: a pedestrian line in a park without a mapped width (a mall, a promenade)
PED_TOWN_MIN_W, PED_TOWN_MAX_W = 4.0, 12.0     # m: in a town it fills the street between the facades (x PED_FILL), within these
PED_FILL = 0.8
PED_LOOK_M = 25.0                       # m: a facade farther than this on a side does not count
NEAR_SIDEWALK_M = 200.0                 # untagged sidewalk: the surface most tagged sidewalks this near have (else asphalt)
GARDEN_MIN_M2 = 3000.0                  # a mapped "garden" under this is a private plot, not a park
PAD_M = 40.0                            # m round the tile its paths and parks are read

ROLE_WIDTH = {"alley": 3.0, "park": 2.0, "walk": 2.0, "trail": 1.2, "cycleway": 2.5, "sidewalk": 1.8, "steps": 1.5}
# Untagged widths by surface (OSM width tags of 8 city centres, CZ / UA medians: paving 3.2 / 2.0-4.5, asphalt
# 3.0 / 2.5, gravel 3.0 (CZ), cycleway 2.0 / 1.5; the ČÚZK photo of Bezdekuv sad: its gravel paths ~3 m)
KIND_WIDTH = {"stone": 3.0, "brick": 3.0, "concrete": 2.5, "asphalt": 2.5, "gravel": 2.5, "ground": 2.0}
KIND_WIDTH_CZ = {"gravel": 3.0, "asphalt": 3.0}
CYCLE_WIDTH = {"UA": 1.8}
GREEN_CLASSES = {"grass", "flowerbed", "village_green", "meadow"}       # land_use: a lawn either side makes a square
SQUARE_CLASSES = {"pedestrian", "plaza"}
GREEN_SIDE_M = 3.0                      # m either side of a path its lawns are looked for
GREEN_SHARE = 0.5
RING_SQUARE_M = 15.0                    # a round place out of any park makes a square this far round it
CHAIN_MAIN_M = 60.0                     # a chain of paths (joined where only two meet) this long is a park's alley
RING_MAX_M = 40.0                       # a closed chain this small across: a round place (benches face its middle)
NEAR_SURFACE_M = 150.0                  # untagged: the surface most tagged footways this near have ...
NEAR_SURFACE_MIN_M = 60.0               # ... over this many metres of them ...
NEAR_SURFACE_SHARE = 0.6                # ... and this share of that length
WIDTH_MIN_M, WIDTH_MAX_M = 0.8, 12.0
CORRIDOR_CLEAR_M = 0.6                  # m of no tree or bush beside a path's edge (vegetation)

# the surface tag -> a kind
KIND_OF = {"asphalt": "asphalt", "paved": "asphalt", "chipseal": "asphalt", "bitumen": "asphalt",
           "concrete": "concrete", "concrete:plates": "concrete", "concrete_plates": "concrete",
           "concrete:lanes": "concrete",
           "paving_stones": "stone", "sett": "stone", "cobblestone": "stone", "unhewn_cobblestone": "stone",
           "pebblestone": "stone", "stone": "stone", "bricks": "brick", "brick": "brick",
           "gravel": "gravel", "fine_gravel": "gravel", "compacted": "gravel",
           "ground": "ground", "dirt": "ground", "unpaved": "ground", "earth": "ground", "mud": "ground",
           "sand": "ground", "woodchips": "ground", "grass_paver": "ground", "grass": "grass"}
DEFAULT_KIND = {"alley": "asphalt", "walk": "asphalt", "cycleway": "asphalt", "sidewalk": "asphalt", "park": "gravel",
                "trail": "ground", "steps": "ground"}
PAVED_KINDS = {"asphalt", "concrete", "stone", "brick", "cycle_red"}
RED_CYCLE_COUNTRIES = {"UA", "PL"}      # asphalt cycleways there are red-brown (Street View, Vinnytsia's pr. Kosmonavtiv)
DRAWN_ROLES = {"alley", "park", "walk", "cycleway", "sidewalk", "trail"}
TRAIL_DRAWN_KINDS = PAVED_KINDS | {"gravel"}        # (a bare trail stays the photo's: no strip)

LIFT_M = 0.040                          # m over the graded ground (plazas.LIFT_M); +RANK_LIFT_M per rank of the kind
RANK_LIFT_M = 0.003                     # (where two kinds cross, the harder one lies over: no z-fight)
RANK = {"ground": 0, "gravel": 1, "stone": 2, "brick": 2, "concrete": 3, "asphalt": 4, "cycle_red": 5}
STEP_M = 2.0                            # m: the longest edge along a strip (the physics block's lattice)
# (material, texture): "tile" = world-metre repeat; "strip" = a strip texture (u across, v along: aspect x the width)
LOOK = {"asphalt": ("bne_eu_asphalt_walk", "tile", 3.0), "concrete": ("m_gm_concrete_plain", "tile", 11.0),
        "gravel": ("bne_dirt_gravel", "strip", 2.0), "ground": ("bne_dirt_variation_01", "strip", 16.0),
        "cycle_red": ("bne_cycleway_red", "tile", 3.0)}
ROAD_CLIP_EXTRA_M = 0.3                 # m: a strip's triangle this near a drive road's edge is the road's


class Path:
    """One footway / path / cycleway / steps segment: xy (k, 2) metres of the frame, ll (k, 2) lon / lat."""
    __slots__ = ("fid", "cls", "sub", "xy", "ll", "surface", "lit", "width", "name")

    def __init__(self, fid, cls, sub, xy, ll, surface, lit, width, name):
        self.fid, self.cls, self.sub, self.xy, self.ll = fid, cls, sub, xy, ll
        self.surface, self.lit, self.width, self.name = surface, lit, width, name


class Data:
    """What round a road tile: paths, drives [(xy, half width m, sidewalk tag)], parks [(xy ring, area m2, name)],
    points [(class, x, y)] (mapped street_lamp / bench / waste_basket / fountain)."""

    def __init__(self, paths=(), drives=(), parks=(), points=(), greens=(), country=None, decks=()):
        self.paths, self.drives, self.parks, self.points = list(paths), list(drives), list(parks), list(points)
        self.decks = list(decks)        # [(xy, half width m, "")]: the bridges' and viaducts' stretches of the drive roads
                                        # (not in drives: a path under one is no path beside a road; furniture keeps off them)
        self.greens = list(greens)      # [(xy ring, area m2, class)]: lawns, flower beds, pedestrian areas
        self.country = country          # ISO alpha-2 or None
        self.rings = []                 # [(centre (2,), radius m, path width m, group)]: classify's round places
        self.box_m2 = 0.0               # the area read (the tile and its pad)
        self.urban = None               # classify: a town round the tile (the drive roads' density)
        self.buildings = []             # [xy ring (k, 2)]: the footprints near a pedestrian line without a width (read)


class Walk:
    """A classified path to draw: role, width m, surface kind (and where it came from: tag / photo / default)."""
    __slots__ = ("fid", "role", "xy", "ll", "width", "kind", "tag", "src", "lit", "name", "park_m2", "group", "chain_m",
                 "ring", "country")

    def __init__(self, fid, role, xy, ll, width, kind, tag, src, lit, name, park_m2, group=None, chain_m=0.0,
                 ring=False):
        self.fid, self.role, self.xy, self.ll, self.width, self.kind = fid, role, xy, ll, width, kind
        self.tag, self.src, self.lit, self.name, self.park_m2 = tag, src, lit, name, park_m2
        self.group, self.chain_m, self.ring = group, chain_m, ring      # its park (or square), its chain's length
        self.country = None

    @property
    def length(self):
        return float(np.hypot(*np.diff(self.xy, axis=0).T).sum()) if len(self.xy) > 1 else 0.0

    @property
    def meshed(self):
        """drawn as a strip of ours (the ground-painted loose paths are upstream's, UPSTREAM_LOOSE)."""
        return self.drawn and not (UPSTREAM_LOOSE and self.kind in LOOSE_KINDS)

    @property
    def drawn(self):
        if self.role not in DRAWN_ROLES or self.kind in ("grass", None):
            return False
        if self.role == "trail":
            return self.kind in TRAIL_DRAWN_KINDS
        return True


# --- reading the cells ----------------------------------------------------------------------------------------

def _text(f, name, i):
    a = f.attrs.get(name)
    if a is None or i >= len(a):
        return ""
    v = a[i]
    s = "" if v is None else str(v).strip().lower()
    return "" if s in ("nan", "none") else s


def _num(f, name, i):
    a = f.attrs.get(name)
    if a is None or i >= len(a):
        return math.nan
    try:
        return float(a[i])
    except (TypeError, ValueError):
        return math.nan


def _rule(f, name, i):
    """The value of feature i's range rule `name` covering most of it ("" without one)."""
    table = getattr(f, "rules", {}).get(name)
    try:
        if table is None or len(table.feature) == 0:
            return ""
        j = table.major(i)
        return "" if j < 0 else str(table.value[j]).strip().lower()
    except Exception:
        return ""


def _width_of(f, i):
    w = _num(f, "osm_width", i)
    if math.isnan(w):
        try:
            w = float(_rule(f, "width", i))
        except ValueError:
            w = math.nan
    return None if math.isnan(w) or w <= 0 else float(w)


def _elevated(f, i):
    try:
        return abs(float(_rule(f, "level", i) or 0.0)) >= 1.0
    except ValueError:
        return False


def _level_spans(f, i):
    """[(a, b)] fractions of feature i's length that its level rules put 1 m or more off the ground (a bridge or a
    viaduct part), sorted and joined. _rule/major names one rule for the whole feature: Overture joins a road into one
    segment, so a 21 m bridge on 1 km of road made all of it "elevated" (Bukovice: the whole of road 63bd35a7, 1041 m,
    was left out of the drives and a path's lamp stood on the bridge)."""
    table = getattr(f, "rules", {}).get("level")
    spans = []
    try:
        if table is not None and len(table.feature):
            for v, a, b in table.for_feature(i):
                try:
                    high = abs(float(v or 0.0)) >= 1.0
                except (TypeError, ValueError):
                    high = False
                if high and b > a:
                    spans.append((max(0.0, float(a)), min(1.0, float(b))))
    except Exception:
        return []
    spans.sort()
    out = []
    for a, b in spans:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(b, out[-1][1]))
        else:
            out.append((a, b))
    return out


def _pieces(xy, spans):
    """[(piece (k, 2), elevated)] of polyline xy cut at fractions of its length: spans are the elevated stretches."""
    xy = np.asarray(xy, np.float64)
    seg = np.hypot(*np.diff(xy, axis=0).T) if len(xy) > 1 else np.zeros(0)
    s = np.r_[0.0, np.cumsum(seg)]
    if not spans or s[-1] <= 0.0:
        return [(xy, False)]

    def cut(a, b):
        sa, sb = a * s[-1], b * s[-1]
        inner = xy[(s > sa) & (s < sb)]
        at = lambda v: np.array([np.interp(v, s, xy[:, d]) for d in (0, 1)])
        return np.vstack([at(sa), inner, at(sb)])
    out, pos = [], 0.0
    for a, b in spans:
        if a > pos:
            out.append((cut(pos, a), False))
        out.append((cut(max(a, pos), b), True))
        pos = max(pos, b)
    if pos < 1.0:
        out.append((cut(pos, 1.0), False))
    return [(p, hi) for p, hi in out if len(p) >= 2 and float(np.hypot(*np.diff(p, axis=0).T).sum()) > 0.5]


def _ring_area(xy):
    x, y = xy[:, 0], xy[:, 1]
    return abs(float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))) / 2.0


def inside(ring, pts):
    """Which pts (k, 2) lie inside the ring (m, 2): even-odd crossings, numpy (no kernel needed)."""
    ring = np.asarray(ring, np.float64)
    pts = np.asarray(pts, np.float64).reshape(-1, 2)
    a, b = ring, np.roll(ring, -1, axis=0)
    px, py = pts[:, 0:1], pts[:, 1:2]
    cond = (a[:, 1][None] > py) != (b[:, 1][None] > py)
    with np.errstate(divide="ignore", invalid="ignore"):
        xc = (b[:, 0] - a[:, 0])[None] * (py - a[:, 1][None]) / (b[:, 1] - a[:, 1])[None] + a[:, 0][None]
    return (cond & (px < xc)).sum(1) % 2 == 1


def read(frame, roads_list, land_list, infra_list, box, pad=PAD_M, country=None, bldg_list=None):
    """Data of the Features lists (roads, land_use, infrastructure layers of the cells round the tile) within box (x0, y0,
    x1, y1) of frame, pad m wider. A road feature several cells list is taken once (by id); a park or lawn polygon the cells'
    borders cut is read piece by piece (land_use is clipped to its cell) and every piece reports the whole park's area."""
    x0, y0, x1, y1 = (float(v) for v in box)
    x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    paths, drives, parks, points, greens, seen = [], [], [], [], [], set()
    decks = []
    park_raw, park_total = [], {}           # (id, ring, name) of the park pieces; id -> the pieces' area

    def rings_of(f, i, least=2):
        out = []
        for part in f.parts(i):
            if part and len(part[0]) >= least:
                ll = np.asarray(part[0], np.float64)
                e, n = mapgeom.enu_xy(f, frame, ll)
                out.append((np.column_stack((e, n)).astype(np.float64), ll))
        return out

    def near(xy):
        return not (xy[:, 0].max() < x0 or xy[:, 0].min() > x1 or xy[:, 1].max() < y0 or xy[:, 1].min() > y1)

    for f in roads_list or ():
        if f is None or not len(f):
            continue
        cls = f.attrs.get("class")
        if cls is None:
            continue
        for i in range(len(f)):
            c = str(cls[i]).strip().lower()
            if f.kind[i] != mapgeom.LINE or (c not in WALK_CLASSES and c not in DRIVE_HALF_M):
                continue
            key = ("r", str(f.ids[i]))
            if key in seen:
                continue
            if c in DRIVE_HALF_M:
                seen.add(key)
                w = _width_of(f, i)
                spans = _level_spans(f, i)
                for xy, _ll in rings_of(f, i):
                    if len(xy) < 2 or not near(xy):
                        continue
                    for piece, high in _pieces(xy, spans):
                        (decks if high else drives).append(
                            (piece, (w / 2.0 if w else DRIVE_HALF_M[c]), "" if high else _text(f, "osm_sidewalk", i)))
                continue
            if _elevated(f, i):
                continue
            seen.add(key)
            for k, (xy, ll) in enumerate(rings_of(f, i)):
                if len(xy) < 2 or not near(xy):
                    continue
                if c == "pedestrian" and len(xy) > 3 and float(np.hypot(*(xy[0] - xy[-1]))) < 0.5:
                    continue                # (a closed pedestrian way outlines an area: plazas.py's)
                if c in DRIVE_HALF_M:
                    w = _width_of(f, i)
                    drives.append((xy, (w / 2.0 if w else DRIVE_HALF_M[c]), _text(f, "osm_sidewalk", i)))
                    continue
                sub = _text(f, "subclass", i) or _rule(f, "subclass", i)
                if sub in ("crosswalk", "crossing") and float(np.hypot(*np.diff(xy, axis=0).T).sum()) >= LONG_CROSSWALK_M:
                    sub = ""            # (a crossing is 5-60 m: a 640 m "crosswalk" is a boulevard's alley)
                surf = _text(f, "osm_surface", i) or _rule(f, "surface", i)
                lit = {"yes": True, "no": False}.get(_text(f, "osm_lit", i))
                paths.append(Path(f"{f.ids[i]}.{k}", c, sub, xy, ll, surf, lit, _width_of(f, i), _text(f, "name", i)))
    for f in land_list or ():
        if f is None or not len(f):
            continue
        cls = f.attrs.get("class")
        if cls is None:
            continue
        for i in range(len(f)):
            c = str(cls[i]).strip().lower()
            green = c in GREEN_CLASSES or c in SQUARE_CLASSES
            if (c not in PARK_CLASSES and not green) or f.kind[i] != mapgeom.POLYGON:
                continue
            fid = str(f.ids[i])
            for xy, _ll in rings_of(f, i):
                if len(xy) < 4 or not near(xy):
                    continue
                key = ("l", fid, len(xy), round(float(xy.sum()), 1))     # (one piece once; the other pieces of its polygon too)
                if key in seen:
                    continue
                seen.add(key)
                area = _ring_area(xy)
                if green:
                    greens.append((xy, area, c))
                    continue
                park_total[(fid, c)] = park_total.get((fid, c), 0.0) + area
                park_raw.append((fid, c, xy, _text(f, "name", i)))
    for f in infra_list or ():
        if f is None or not len(f):
            continue
        cls = f.attrs.get("class")
        if cls is None:
            continue
        for i in range(len(f)):
            c = str(cls[i]).strip().lower()
            if c not in ("street_lamp", "bench", "waste_basket", "fountain") or f.kind[i] != mapgeom.POINT:
                continue
            key = ("i", str(f.ids[i]))
            if key in seen:
                continue
            seen.add(key)
            for xy, _ll in rings_of(f, i, 1)[:1]:
                if x0 <= xy[0, 0] <= x1 and y0 <= xy[0, 1] <= y1:
                    points.append((c, float(xy[0, 0]), float(xy[0, 1])))
    for fid, c, xy, name in park_raw:
        total = park_total[(fid, c)]
        if c == "garden" and total < GARDEN_MIN_M2:
            continue
        parks.append((xy, total, name))
    d = Data(paths, drives, parks, points, greens, country, decks)
    d.box_m2 = (x1 - x0) * (y1 - y0)
    if bldg_list and any(p.cls == "pedestrian" and not p.width for p in paths):
        d.buildings = _building_rings(frame, bldg_list, (x0 - PED_LOOK_M, y0 - PED_LOOK_M, x1 + PED_LOOK_M, y1 + PED_LOOK_M))
    return d


def _building_rings(frame, bldg_list, box):
    """The outer rings (k, 2) of the buildings of the Features lists within box (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = box
    out, seen = [], set()
    for f in bldg_list or ():
        if f is None or not len(f):
            continue
        for i in range(len(f)):
            fid = str(f.ids[i])
            if fid in seen:
                continue
            seen.add(fid)
            for part in f.parts(i):
                if part and len(part[0]) >= 3:
                    e, n = mapgeom.enu_xy(f, frame, np.asarray(part[0], np.float64))
                    if e.max() < x0 or e.min() > x1 or n.max() < y0 or n.min() > y1:
                        continue
                    out.append(np.column_stack((e, n)).astype(np.float64))
    return out


# crude boxes for the countries whose profiles differ, when the bundled country outlines are not there (lat0, lon0, lat1, lon1)
COUNTRY_BOXES = (("CZ", (48.55, 12.09, 51.06, 18.86)), ("UA", (44.38, 22.14, 52.38, 40.23)))


def country_of(lat, lon):
    """ISO alpha-2 of (lat, lon): the bundled outlines (countrybounds), else COUNTRY_BOXES, else None."""
    try:
        import countrybounds
        got = countrybounds.bundled().at(float(lat), float(lon))
        if got:
            return str(got).upper()
    except Exception:
        pass
    for iso, (a0, b0, a1, b1) in COUNTRY_BOXES:
        if a0 <= lat <= a1 and b0 <= lon <= b1:
            return iso
    return None


def collect(md, frame, box, pad=PAD_M):
    """Data round road tile box from the map cells (md: mapdata.MapData). Errors cost the paths, not the tile."""
    import mapcells
    x0, y0, x1, y1 = (float(v) for v in box)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0 - pad, x0 - pad, x1 + pad, x1 + pad]),
                                      np.array([y0 - pad, y1 + pad, y0 - pad, y1 + pad]), np.zeros(4))
    keys = mapcells.cells_for_points(la, lo, pad_lat=0.0, pad_lon=0.0, max_cells=16)
    roads_l, land_l, infra_l, bldg_l = [], [], [], []
    for k in keys:
        layers = md.cell(k, layers=("roads", "land_use", "infrastructure", "buildings"), kind="demand").layers
        roads_l.append(layers.get("roads"))
        land_l.append(layers.get("land_use"))
        infra_l.append(layers.get("infrastructure"))
        bldg_l.append(layers.get("buildings"))
    cla, clo, _ = frame.enu_to_geodetic(np.array([(x0 + x1) / 2]), np.array([(y0 + y1) / 2]), np.zeros(1))
    return read(frame, roads_l, land_l, infra_l, box, pad, country_of(float(cla[0]), float(clo[0])), bldg_l)


# --- classification --------------------------------------------------------------------------------------------

def _resample(xy, step):
    """The polyline xy (k, 2) with a point at least every `step` m (its own vertices kept), repeats dropped."""
    xy = np.asarray(xy, np.float64)
    keep = np.r_[True, np.hypot(*np.diff(xy, axis=0).T) > 1e-6] if len(xy) > 1 else np.ones(len(xy), bool)
    xy = xy[keep]
    if len(xy) < 2:
        return xy
    out = [xy[0:1]]
    for a, b in zip(xy[:-1], xy[1:]):
        n = max(1, int(math.ceil(float(np.hypot(*(b - a))) / step)))
        t = (np.arange(1, n + 1) / n)[:, None]
        out.append(a + (b - a) * t)
    return np.concatenate(out)


def _in_park(xy, parks):
    """(share of xy's points inside a park, the largest such park's area m2, its name)."""
    pts = _resample(xy, 5.0)
    if not len(pts) or not parks:
        return 0.0, 0.0, ""
    best, share = (0.0, ""), 0.0
    for ring, area, name in parks:
        if ring[:, 0].max() < pts[:, 0].min() or ring[:, 0].min() > pts[:, 0].max() or \
                ring[:, 1].max() < pts[:, 1].min() or ring[:, 1].min() > pts[:, 1].max():
            continue
        s = float(inside(ring, pts).mean())
        if s > share or (s == share and s > 0 and area > best[0]):
            share, best = s, (area, name)
    return share, best[0], best[1]


def _dist_to_lines(pts, lines):
    """Distance (k,) of pts (k, 2) to the nearest of lines [(xy, half width, tag)], and the index of that line."""
    best = np.full(len(pts), np.inf)
    which = np.full(len(pts), -1)
    for j, (xy, _hw, _t) in enumerate(lines):
        a, b = xy[:-1], xy[1:]
        d = b - a
        L2 = np.maximum((d ** 2).sum(1), 1e-9)
        t = np.clip(((pts[:, None, :] - a[None]) * d[None]).sum(2) / L2[None], 0.0, 1.0)
        c = a[None] + d[None] * t[:, :, None]
        dist = np.hypot(*(pts[:, None, :] - c).transpose(2, 0, 1)).min(1)
        better = dist < best
        best = np.where(better, dist, best)
        which = np.where(better, j, which)
    return best, which


def _photo(ll):
    """(kind, missing): what the regional photo says of a path (photosurface.measure / classify), (None, False) where no
    regional photo covers it."""
    try:
        import photosurface
        st, missing = photosurface.measure(ll)
        return {"paved": "asphalt", "gravel": "gravel", "grass": "grass"}.get(photosurface.classify(st)), missing
    except Exception:
        return None, False


ROAD_SIDEWALK_TAGS = {"both", "left", "right", "yes"}


def _side_road_has_none(p, drives):
    """A sidewalk mapped on its own is drawn unless the road beside it says it has its own (OpenStreetMap's sidewalk
    both / left / right / yes: roads.sidewalks draws that one). A road without the tag gets its sidewalks from the land
    cover rule, which leaves the side of a park bare (one side of a park without a sidewalk);
    where it does draw one, the raised sidewalk covers this flat strip."""
    mid = p.xy[len(p.xy) // 2][None]
    d, j = _dist_to_lines(mid, drives) if drives else (np.array([np.inf]), np.array([-1]))
    if not np.isfinite(d[0]) or d[0] > 25.0:
        return True
    return drives[int(j[0])][2] not in ROAD_SIDEWALK_TAGS


def role_of(p, park_share, drives=(), chain_m=0.0):
    """The role of path p (None: not drawn), park_share: the share of it in a park or a square, chain_m: the length of
    the chain of paths it is part of (joined where only two meet)."""
    if p.sub in ("crosswalk", "crossing"):
        return None
    if p.cls == "steps":
        return "steps"
    if p.sub == "sidewalk":
        return "sidewalk" if _side_road_has_none(p, drives) else None
    if p.cls == "cycleway":
        return "cycleway"
    if p.cls == "pedestrian":                # (a line: a park's mall or a promenade, a street closed to cars)
        return "alley" if park_share >= 0.6 else "walk"
    if park_share >= 0.6:
        main = (bool(p.lit) or (p.width or 0.0) >= 3.0 or bool(p.name) or KIND_OF.get(p.surface) in PAVED_KINDS
                or chain_m >= CHAIN_MAIN_M)
        return "alley" if main else "park"
    return "walk" if p.cls == "footway" else "trail"


def _near_surface(p, tagged):
    """The kind most tagged footways within NEAR_SURFACE_M of p's middle have (by length), when over NEAR_SURFACE_MIN_M of
    them agree on it at NEAR_SURFACE_SHARE or more; else None. tagged: [(middle (2,), length m, kind)]."""
    if not tagged:
        return None
    mid = p.xy[len(p.xy) // 2]
    tot = C_Counter()
    for m, length, kind in tagged:
        if float(np.hypot(*(m - mid))) <= NEAR_SURFACE_M:
            tot[kind] += length
    whole = sum(tot.values())
    if whole < NEAR_SURFACE_MIN_M:
        return None
    kind, length = tot.most_common(1)[0]
    return kind if length / whole >= NEAR_SURFACE_SHARE else None


def _near_sidewalk(p, tagged_side):
    """_near_surface for a separate sidewalk: the surface most tagged sidewalks within NEAR_SIDEWALK_M have."""
    if not tagged_side:
        return None
    mid = p.xy[len(p.xy) // 2]
    tot = C_Counter()
    for m, length, kind in tagged_side:
        if float(np.hypot(*(m - mid))) <= NEAR_SIDEWALK_M:
            tot[kind] += length
    whole = sum(tot.values())
    if whole < NEAR_SURFACE_MIN_M:
        return None
    kind, length = tot.most_common(1)[0]
    return kind if length / whole >= NEAR_SURFACE_SHARE else None


def surface_of(p, role, tagged=(), tagged_side=()):
    """(kind, tag, source): the surface tag's kind, else the one most tagged footways round it have ("near"), else the
    regional photo's, else the role's usual one; "missing" as the source when the photo's tiles are not on the disk yet
    (the caller then keeps nothing built from it)."""
    tag = p.surface.split(";")[0].strip() if p.surface else ""
    if tag in KIND_OF:
        return KIND_OF[tag], tag, "tag"
    if role in ("alley", "park", "walk") and p.cls in ("footway", "pedestrian"):
        near = _near_surface(p, tagged)
        if near is not None:
            return near, "", "near"
    if role == "sidewalk":                   # (untagged sidewalks take their neighbours' surface, else asphalt: no guessed paving)
        near = _near_sidewalk(p, tagged_side)
        if near is not None:
            return near, "", "near"
    if p.cls == "pedestrian" and p.surface == "":
        missing = False
        kind, missing = _photo(p.ll) if role in ("alley", "walk") else (None, False)
        if kind is not None:
            return kind, "", "photo"
        return "stone", "", "missing" if missing else "default"
    missing = False
    if role in ("alley", "park", "walk", "cycleway") and p.surface == "":
        kind, missing = _photo(p.ll)
        if kind is not None:
            return kind, "", "photo"
    return DEFAULT_KIND.get(role, "ground"), "", "missing" if missing else "default"


def _green_share(xy, greens):
    """(share of xy's points with a lawn / flower bed on both sides within GREEN_SIDE_M or lying in a pedestrian area,
    the indexes of those greens)."""
    pts = _resample(xy, 5.0)
    if len(pts) < 2 or not greens:
        return 0.0, set()
    n = _normals(pts)
    hit = np.zeros(len(pts), bool)
    left = np.zeros(len(pts), bool)
    right = np.zeros(len(pts), bool)
    used = set()
    lo, hi = pts.min(0) - GREEN_SIDE_M, pts.max(0) + GREEN_SIDE_M
    for j, (ring, _area, cls) in enumerate(greens):
        if ring[:, 0].max() < lo[0] or ring[:, 0].min() > hi[0] or ring[:, 1].max() < lo[1] or ring[:, 1].min() > hi[1]:
            continue
        if cls in SQUARE_CLASSES:
            k = inside(ring, pts)
            hit |= k
        else:
            kl = inside(ring, pts + n * GREEN_SIDE_M)
            kr = inside(ring, pts - n * GREEN_SIDE_M)
            left |= kl
            right |= kr
            k = kl | kr
        if k.any():
            used.add(j)
    return float((hit | (left & right)).mean()), used


def _chains(paths):
    """Per path: (its chain's length m, closed and small: a round place, the chain's id). A chain joins the paths that
    meet where no third path does (their ends rounded to 0.5 m)."""
    def key(q):
        return (round(float(q[0]) * 2), round(float(q[1]) * 2))
    deg = C_Counter()
    for p in paths:
        deg[key(p.xy[0])] += 1
        deg[key(p.xy[-1])] += 1
    parent = list(range(len(paths)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    at = {}
    for i, p in enumerate(paths):
        for q in (p.xy[0], p.xy[-1]):
            k = key(q)
            if deg[k] == 2:
                if k in at:
                    parent[find(i)] = find(at[k])
                else:
                    at[k] = i
    groups = {}
    for i in range(len(paths)):
        groups.setdefault(find(i), []).append(i)
    out = [None] * len(paths)
    for g, members in groups.items():
        length = sum(float(np.hypot(*np.diff(paths[i].xy, axis=0).T).sum()) for i in members)
        ends = C_Counter()
        for i in members:
            ends[key(paths[i].xy[0])] += 1
            ends[key(paths[i].xy[-1])] += 1
        closed = all(v % 2 == 0 for v in ends.values())
        pts = np.concatenate([paths[i].xy for i in members])
        across = float(np.hypot(*np.ptp(pts, axis=0)))
        for i in members:
            out[i] = (length, bool(closed and across <= RING_MAX_M), g)
    return out


RING_GAP_M = 1.5           # m a loop's ends may lie apart and still close it
RING_CURVE = 1.05          # a piece of a round place: its length at least this over its chord (or closed itself)


def _rings(paths):
    """Per path: the id of the round place it is a piece of, or None. A round place: short curved paths (or one closed
    path) that close a loop no wider than RING_MAX_M; the paths that run into it (a cross's arms) cut it into arcs, so the
    pieces are joined at their ends whatever else meets there."""
    def key(q):
        return (round(float(q[0]) * 2), round(float(q[1]) * 2))
    cand, alone = [], set()
    for i, p in enumerate(paths):
        if len(p.xy) < 2:
            continue
        length = float(np.hypot(*np.diff(p.xy, axis=0).T).sum())
        chord = float(np.hypot(*(p.xy[-1] - p.xy[0])))
        across = float(np.ptp(p.xy, axis=0).max())
        if across > RING_MAX_M:
            continue
        if len(p.xy) >= 4 and length >= 10.0 and chord <= max(RING_GAP_M, 0.06 * length):
            alone.add(i)            # (a loop whose ends the map leaves a step apart: Bezdekuv sad's, 0.8 m)
            cand.append(i)
        elif (len(p.xy) >= 3 and length >= RING_CURVE * chord) or length <= RING_GAP_M * 2:
            cand.append(i)          # an arc, or the short piece that closes one
    parent = {i: i for i in cand}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    at = {}
    for i in cand:
        for q in (paths[i].xy[0], paths[i].xy[-1]):
            k = key(q)
            if k in at:
                parent[find(i)] = find(at[k])
            else:
                at[k] = i
    comps = {}
    for i in cand:
        comps.setdefault(find(i), []).append(i)
    out = [None] * len(paths)
    for g, members in comps.items():
        ends = C_Counter()
        for i in members:
            ends[key(paths[i].xy[0])] += 1
            ends[key(paths[i].xy[-1])] += 1
        pts = np.concatenate([paths[i].xy for i in members])
        closed = all(v % 2 == 0 for v in ends.values()) and any(len(paths[i].xy) >= 3 for i in members)
        if (closed or any(i in alone for i in members)) and float(np.ptp(pts, axis=0).max()) <= RING_MAX_M:
            for i in members:
                out[i] = g
    return out


def _in_squares(xy, greens):
    """The share of xy's points inside a pedestrian area / plaza polygon of greens [(ring, area, class)]."""
    pts = _resample(xy, 5.0)
    rings = [g[0] for g in greens if g[2] in SQUARE_CLASSES]
    if not len(pts) or not rings:
        return 0.0
    hit = np.zeros(len(pts), bool)
    for ring in rings:
        if ring[:, 0].max() < pts[:, 0].min() or ring[:, 0].min() > pts[:, 0].max() or \
                ring[:, 1].max() < pts[:, 1].min() or ring[:, 1].min() > pts[:, 1].max():
            continue
        hit |= inside(ring, pts)
    return float(hit.mean())


def _pedestrian_width(p, in_park, buildings):
    """The width of a pedestrian line without a mapped one: PED_PARK_W in a park; in a town the street between the
    facades (the nearest footprint edge each side of the line, x PED_FILL), clamped to PED_TOWN_MIN_W..MAX_W (the
    minimum without facades near)."""
    if in_park:
        return PED_PARK_W
    if not buildings:
        return PED_TOWN_MIN_W
    pts = _resample(p.xy, 8.0)
    if len(pts) < 2:
        return PED_TOWN_MIN_W
    lo, hi = pts.min(0) - PED_LOOK_M, pts.max(0) + PED_LOOK_M
    A, B = [], []
    for r in buildings:
        if r[:, 0].max() < lo[0] or r[:, 0].min() > hi[0] or r[:, 1].max() < lo[1] or r[:, 1].min() > hi[1]:
            continue
        A.append(r[:-1])
        B.append(r[1:])
    if not A:
        return PED_TOWN_MIN_W
    A, B = np.concatenate(A), np.concatenate(B)
    n = _normals(pts)
    D = B - A
    L2 = np.maximum((D * D).sum(1), 1e-9)
    ws = []
    for q, nq in zip(pts, n):
        t = np.clip(((q - A) * D).sum(1) / L2, 0.0, 1.0)
        c = A + D * t[:, None]
        d = np.hypot(*(c - q).T)
        side = ((c - q) * nq).sum(1)
        left, right = d[(side > 0) & (d < PED_LOOK_M)], d[(side < 0) & (d < PED_LOOK_M)]
        if len(left) and len(right):
            ws.append((float(left.min()) + float(right.min())) * PED_FILL)
    if not ws:
        return PED_TOWN_MIN_W
    return float(min(max(float(np.median(ws)), PED_TOWN_MIN_W), PED_TOWN_MAX_W))


def _width_default(role, kind, country):
    """The width of a path without a mapped one: by its surface (KIND_WIDTH, Czechia's KIND_WIDTH_CZ), a cycleway's and
    a trail's by role."""
    if role == "cycleway":
        return CYCLE_WIDTH.get(country, 2.0)
    if role in ("trail", "steps", "sidewalk"):
        return ROLE_WIDTH[role]
    if country == "CZ" and kind in KIND_WIDTH_CZ:
        return KIND_WIDTH_CZ[kind]
    return KIND_WIDTH.get(kind, ROLE_WIDTH[role])


URBAN_DRIVE_KM = 8.0        # km of drive roads a km2 round the tile: a town (its parks' alleys are lit)


def _urban(drives, box_m2):
    total = sum(float(np.hypot(*np.diff(xy, axis=0).T).sum()) for xy, _hw, _t in drives)
    return bool(box_m2 > 0 and total / 1000.0 / (box_m2 / 1e6) >= URBAN_DRIVE_KM)


def classify(data):
    """[Walk] of data's paths; data.rings (the round places) and data.urban set."""
    out = []
    walkable = [p for p in data.paths if p.sub not in ("crosswalk", "crossing", "sidewalk") and p.cls != "steps"]
    chains = dict(zip((id(p) for p in walkable), _chains(walkable)))
    ring_of = dict(zip((id(p) for p in walkable), _rings(walkable)))
    tagged, tagged_side = [], []
    for p in data.paths:
        tag = p.surface.split(";")[0].strip() if p.surface else ""
        if p.cls in ("footway", "pedestrian") and p.sub not in ("crosswalk", "crossing") and tag in KIND_OF:
            item = (p.xy[len(p.xy) // 2], float(np.hypot(*np.diff(p.xy, axis=0).T).sum()), KIND_OF[tag])
            (tagged_side if p.sub == "sidewalk" else tagged).append(item)
    data.urban = _urban(data.drives, data.box_m2)
    # the round places' middles and radii, and the paths that run into one (its spokes)
    ring_geo = {}
    for q in walkable:
        rid = ring_of[id(q)]
        if rid is not None:
            ring_geo.setdefault(rid, []).append(q.xy)
    ring_geo = {rid: (np.concatenate(xs).mean(0), float(np.hypot(*(np.concatenate(xs) - np.concatenate(xs).mean(0)).T).mean()))
                for rid, xs in ring_geo.items()}

    def spoke_of(q):
        if q.cls not in ("footway", "path") or ring_of.get(id(q)) is not None:
            return None
        for rid, (c, r) in ring_geo.items():
            for end in (q.xy[0], q.xy[-1]):
                if float(np.hypot(*(end - c))) <= r + 2.0:
                    return rid
        return None
    green_key = {}          # a green's index -> the square it belongs to (greens sharing a path are one square)
    data.rings = []
    rings_seen = set()
    for p in data.paths:
        share, park_m2, _name = _in_park(p.xy, data.parks)
        group = None
        if share >= 0.6:
            group = "p%.0f" % park_m2
        elif p.cls in ("footway", "path", "pedestrian") and p.sub not in ("sidewalk", "crosswalk", "crossing"):
            gs, used = _green_share(p.xy, data.greens)
            if gs >= GREEN_SHARE and used:
                share = gs
                key = min(green_key.get(j, j) for j in used)
                for j in used:
                    green_key[j] = key
                park_m2 = sum(data.greens[j][1] for j in used)
                group = "g%d" % key
        chain_m, _closed, _chain = chains.get(id(p), (0.0, False, None))
        ring_id = ring_of.get(id(p))
        is_ring = ring_id is not None
        if group is None and (is_ring or spoke_of(p) is not None):
            rid = ring_id if is_ring else spoke_of(p)       # (a round place in town: a square round it)
            share, group = 1.0, "r%d" % rid
            park_m2 = math.pi * (ring_geo[rid][1] + RING_SQUARE_M) ** 2
        if p.cls == "pedestrian" and _in_squares(p.xy, data.greens) >= 0.6:
            continue                        # (lies in a pedestrian area plazas.py paves)
        role = role_of(p, share, data.drives, chain_m)
        if role is None:
            continue
        kind, tag, src = surface_of(p, role, tagged, tagged_side)
        if p.width:
            w = p.width
        elif p.cls == "pedestrian":
            w = _pedestrian_width(p, group is not None and share >= 0.6, data.buildings)
        else:
            w = _width_default(role, kind, data.country)
        w = float(min(max(w, WIDTH_MIN_M), WIDTH_MAX_M))
        if role == "cycleway" and kind == "asphalt" and data.country in RED_CYCLE_COUNTRIES:
            kind = "cycle_red"
        in_park = share >= 0.6 and role in ("alley", "park")
        wk = Walk(p.fid, role, p.xy, p.ll, w, kind, tag, src, p.lit, p.name, park_m2 if in_park else 0.0,
                  group if in_park else None, chain_m, bool(is_ring and in_park))
        wk.country = data.country
        out.append(wk)
        if wk.ring and ring_id not in rings_seen:
            rings_seen.add(ring_id)
            pts = np.concatenate([q.xy for q in walkable if ring_of[id(q)] == ring_id])
            c = pts.mean(0)
            data.rings.append((c, float(np.hypot(*(pts - c).T).mean()), w, wk.group))
    return out


def any_photo_missing(walks):
    """Did a walk wait for a photo tile that is not on the disk yet (nothing built from it is kept: revisit.fault)?"""
    return any(w.src == "missing" for w in walks)


# --- the strips ------------------------------------------------------------------------------------------------

def _normals(xy):
    import sidewalks
    return sidewalks._normals_2d(xy)


def strip(xy, width, step=STEP_M):
    """(P (2k, 2), T (n, 3), s (2k,)): the strip of `width` m along polyline xy, left / right points interleaved, triangles
    counter-clockwise from above, s the arc length of each point."""
    pts = _resample(xy, step)
    if len(pts) < 2:
        return None
    n = _normals(pts)
    if float(np.hypot(*(pts[0] - pts[-1]))) < 1e-6 and len(pts) > 3:        # a closed ring: one normal at its seam
        m = n[0] + n[-1]
        ml = float(np.hypot(*m)) or 1.0
        n[0] = n[-1] = m / ml
    L, R = pts + n * (width / 2.0), pts - n * (width / 2.0)
    P = np.empty((2 * len(pts), 2))
    P[0::2], P[1::2] = L, R
    k = np.arange(len(pts) - 1) * 2
    T = np.concatenate([np.stack([k, k + 1, k + 2], 1), np.stack([k + 1, k + 3, k + 2], 1)])
    s = np.repeat(np.r_[0.0, np.cumsum(np.hypot(*np.diff(pts, axis=0).T))], 2)
    return P, T, s


# concrete pavers by country (generated pictures): (material, m a repeat)
PAVERS = {"CZ": ("bne_park_pavers_cz", 1.6)}      # (Ukraine's yellow pavers are off: they showed where the data names no paving;
                                                  # they can return once the data decides where they belong)
PAVER_TAGS = {"paving_stones"}      # (the tag itself: an untagged or guessed kind gets no colour of ours)


def _look(w):
    """(material, kind of uv, repeat m or aspect) of a walk: the country's concrete pavers for paving stones (PAVERS),
    plazas' paving for the other stone and brick."""
    if w.kind == "stone" and w.country in PAVERS and w.tag in PAVER_TAGS and w.src == "tag":
        mat, rep = PAVERS[w.country]
        return mat, "tile", rep
    if w.kind in ("stone", "brick"):
        import plazas
        la, lo = float(w.ll[:, 1].mean()), float(w.ll[:, 0].mean())
        sv = w.tag if w.tag in plazas.PAVING else ("bricks" if w.kind == "brick" else "paving_stones")
        look = plazas.paving(sv, la, lo) or plazas.STONE
        return look[0], "tile", look[1]
    return LOOK[w.kind]


def _clip_roads(c, drives):
    """Which triangle centres c (k, 2) lie on a drive road (within its half width and ROAD_CLIP_EXTRA_M)."""
    on = np.zeros(len(c), bool)
    if not drives or not len(c):
        return on
    d, j = _dist_to_lines(c, drives)
    hw = np.array([drives[x][1] if x >= 0 else 0.0 for x in j])
    return d < hw + ROAD_CLIP_EXTRA_M


def mesh(frame, box, walks, elev, origin, cover=None, drives=()):
    """The strips of the drawn walks in tile box (x0, y0, x1, y1) as plazas.mesh's tuple (positions relative to origin
    (k, 3), tris, uvs, [material], tri material index) or None. A tile takes the triangles whose centre lies in it; the
    ones on the road mesh (cover (n, 6) as plazas.mesh's) or on a drive road are left out. elev(lat, lon) -> m."""
    x0, y0, x1, y1 = (float(v) for v in box)
    Ps, Ts, UVs, Ms, n0 = [], [], [], [], 0
    for w in walks:
        if not w.meshed:
            continue
        xy = w.xy
        m = w.width + 1.0
        if xy[:, 0].max() + m < x0 or xy[:, 0].min() - m > x1 or xy[:, 1].max() + m < y0 or xy[:, 1].min() - m > y1:
            continue
        sp = strip(xy, w.width)
        if sp is None:
            continue
        P, T, s = sp
        c = P[T].mean(1)
        keep = (c[:, 0] >= x0) & (c[:, 0] < x1) & (c[:, 1] >= y0) & (c[:, 1] < y1)
        if cover is not None and len(cover) and keep.any():
            import plazas
            k = np.flatnonzero(keep)
            keep[k[plazas._covered(cover, c[k])]] = False
        if keep.any() and drives:
            k = np.flatnonzero(keep)
            keep[k[_clip_roads(c[k], drives)]] = False
        if not keep.any():
            continue
        mat, how, rep = _look(w)
        if how == "tile":
            uv = P / rep
        else:
            across = np.tile([0.0, 1.0], len(P) // 2)
            uv = np.stack([across, s / (rep * w.width)], 1)
        T = T[keep]
        used = np.unique(T)
        remap = np.full(len(P), -1, np.int64)
        remap[used] = np.arange(len(used))
        Ps.append(np.c_[P[used], np.full(len(used), LIFT_M + RANK.get(w.kind, 0) * RANK_LIFT_M)])
        Ts.append(remap[T] + n0)
        UVs.append(uv[used])
        Ms.append((mat, len(T)))
        n0 += len(used)
    if not Ps:
        return None
    XYZ, T, UV = np.concatenate(Ps), np.concatenate(Ts), np.concatenate(UVs)
    la, lo, _ = frame.enu_to_geodetic(XYZ[:, 0], XYZ[:, 1], np.zeros(len(XYZ)))
    h = np.asarray(elev(la, lo), np.float64)
    _, _, z = frame.geodetic_to_enu(la, lo, h)
    V = np.c_[XYZ[:, :2], np.asarray(z) + XYZ[:, 2]] - np.asarray(origin, np.float64)
    mats, idx = [], []
    for mat, nt in Ms:
        if mat not in mats:
            mats.append(mat)
        idx.append(np.full(nt, mats.index(mat), np.int64))
    return V, T, UV, mats, np.concatenate(idx)


# --- vegetation's corridor ---------------------------------------------------------------------------------------

def corridor(f):
    """(rows, half width m) of the walk features of a roads layer whose line trees and bushes keep off: every footway,
    path, cycleway and steps but a sidewalk or a crossing (street trees stand in the sidewalk); the half width is the
    mapped width's or the class's, plus CORRIDOR_CLEAR_M."""
    if not ON or f is None or not len(f.kind):
        return np.zeros(0, np.int64), np.zeros(0)
    cls = f.attrs.get("class")
    if cls is None:
        return np.zeros(0, np.int64), np.zeros(0)
    rows, hws = [], []
    base = {"footway": ROLE_WIDTH["walk"], "path": ROLE_WIDTH["trail"], "bridleway": 2.0,
            "cycleway": ROLE_WIDTH["cycleway"], "steps": ROLE_WIDTH["steps"], "pedestrian": PED_PARK_W}
    for i in np.flatnonzero(f.kind == mapgeom.LINE):
        c = str(cls[i]).strip().lower()
        if c not in WALK_CLASSES:
            continue
        sub = _text(f, "subclass", i) or _rule(f, "subclass", i)
        if sub in ("sidewalk", "crosswalk", "crossing") or _elevated(f, i):
            continue
        rows.append(int(i))
        hws.append((_width_of(f, i) or base[c]) / 2.0 + CORRIDOR_CLEAR_M)
    return np.asarray(rows, np.int64), np.asarray(hws, np.float64)


# --- lamps, benches and bins -------------------------------------------------------------------------------------

BENCH, BIN = "bench", "bin"
BENCH_METAL = "bench_metal"                 # Italy's metal bench (trees.lua): the modern towns'
BENCHES = (BENCH, BENCH_METAL)
LAMP_STEEL, LAMP_LANTERN = "eu_lamp_steel", "fr_lantern"       # (citylamps.G_STEEL; the classic lantern on its post)
LAMPS = (LAMP_STEEL, LAMP_LANTERN, "streetlight")
LAMP = LAMP_STEEL                           # a mapped lamp's model where the country has no rule
BENCH_H, BENCH_METAL_H, BIN_H = 0.9, 0.63, 1.1
LAMP_HEIGHT = {LAMP_STEEL: 5.0, LAMP_LANTERN: 5.4, "streetlight": 4.9}   # a park post: under the street's 9 m
# the park lamp by country: old Central European towns a lantern on its post, elsewhere a short steel post
LANTERN_COUNTRIES = {"CZ", "SK", "AT", "DE", "HU", "CH", "SI"}
METAL_BENCH_COUNTRIES = {"UA", "PL", "MD", "RO", "BY", "LT", "LV", "EE"}
BENCH_YAW = 90.0                            # both benches: long along local x, the seat's front local -y
LAMP_SPACING = {"UA": 20.0}                 # m between a lit path's lamps (measured NN 12-27 m)
LAMP_SPACING_M = 25.0
LAMP_SIDE_M, BENCH_SIDE_M = 1.0, 1.1        # m past the path's edge (measured: lamps 2.7-3.5 m, benches ~3 m off the axis)
LIT_SHARE = 0.4                             # of a town park's paved alleys lit when the map says nothing (measured 37-38 %)
SMALL_PARK_M2 = 5000.0                      # a square under this: its main paths all lit, every path gets a bench
# benches per km of path by the park's area (medians where mapped, 8 city centres), scaled down: the parks whose
# benches are mapped are the furnished ones
BENCH_DENSITY = ((2000.0, 80.0), (5000.0, 45.0), (20000.0, 18.0), (1e12, 7.0))
BENCH_SCALE = 0.75
BENCH_PAIR_M = 3.0                          # two benches of a pair this far apart (measured NN 3-5 m)
BIN_EVERY = 3                               # a bin by every third bench (measured 0.2-0.33 a bench)
SHORT_PATH_M = 15.0
RING_BENCHES = 4
MAPPED_NEAR_WALK_M = 8.0
ROAD_KEEP_M = 1.5                           # auto furniture this far off a drive road's edge
ROAD_LAMP_KEEP_M = 8.0                      # a lit walk's own lamps this far from a drive road (the street has its own)
FOUNTAIN_BENCHES = 4
FOUNTAIN_R_M = 4.0
FOUNTAIN_WALK_M = 10.0
CLEAR_M = 1.6                               # two pieces are at least this far apart


def _h01(*vals):
    h = hashlib.blake2b("|".join(str(v) for v in vals).encode(), digest_size=8).digest()
    return int.from_bytes(h, "little") / 2.0 ** 64


def _along(xy, spacing, phase):
    """[(point, unit tangent, arclength)] every `spacing` m along xy from phase."""
    xy = np.asarray(xy, np.float64)
    seg = np.diff(xy, axis=0)
    ln = np.hypot(seg[:, 0], seg[:, 1])
    cum = np.r_[0.0, np.cumsum(ln)]
    out, s = [], phase
    while s < cum[-1]:
        i = min(int(np.searchsorted(cum, s, side="right")) - 1, len(ln) - 1)
        if ln[i] > 1e-9:
            t = seg[i] / ln[i]
            out.append((xy[i] + t * (s - cum[i]), t, s))
        s += spacing
    return out


def _at(xy, s):
    """(point, unit tangent) at arclength s along xy."""
    got = _along(xy, 1e9, max(0.0, s))
    if got:
        return got[0][0], got[0][1]
    t = xy[-1] - xy[-2]
    return xy[-1], t / (float(np.hypot(*t)) or 1.0)


def _nearest_on(xy, p):
    """(distance, nearest point) of p on polyline xy."""
    a, b = xy[:-1], xy[1:]
    d = b - a
    t = np.clip(((p - a) * d).sum(1) / np.maximum((d ** 2).sum(1), 1e-9), 0.0, 1.0)
    c = a + d * t[:, None]
    k = int(np.argmin(np.hypot(*(c - p).T)))
    return float(np.hypot(*(c[k] - p))), c[k]


def bench_density(area_m2):
    """Benches a km of path in a park or square of this area (BENCH_DENSITY x BENCH_SCALE)."""
    for top, d in BENCH_DENSITY:
        if area_m2 < top:
            return d * BENCH_SCALE
    return BENCH_DENSITY[-1][1] * BENCH_SCALE


def bench_model(country):
    return (BENCH_METAL, BENCH_METAL_H) if country in METAL_BENCH_COUNTRIES else (BENCH, BENCH_H)


def lamp_model(country):
    g = LAMP_LANTERN if country in LANTERN_COUNTRIES else LAMP_STEEL
    return g, LAMP_HEIGHT[g]


def lit_walks(data, walks):
    """The set of walk ids lit: lit=yes, else in a town's park or square (data.urban) its main paths: a square under
    SMALL_PARK_M2 all of its paths, a larger park its alleys of a hard surface from the longest chain down until
    LIT_SHARE of their length; lit=no never."""
    out = set()
    groups = {}
    for w in walks:
        if not w.drawn:
            continue
        if w.lit is True:
            out.add(w.fid)
        elif w.lit is None and w.group is not None and data.urban:
            groups.setdefault(w.group, []).append(w)
    for g, ws in groups.items():
        if ws[0].park_m2 < SMALL_PARK_M2:
            out.update(w.fid for w in ws)
            continue
        paved = sorted((w for w in ws if w.role == "alley" and w.kind in PAVED_KINDS),
                       key=lambda w: (-w.chain_m, -w.length, w.fid))
        total = sum(w.length for w in paved)
        done = 0.0
        for w in paved:
            if done >= LIT_SHARE * total:
                break
            out.add(w.fid)
            done += w.length
    return out


def furniture(data, walks, box):
    """[(group, x, y, fx, fy, height)] in tile box: the mapped lamps, benches and bins in parks and by paths; then
    benches round the round places (data.rings) and mapped fountains, facing their middle; benches in pairs along the
    paths of parks and squares (bench_density by the park's area; every path of a small square at least one), a bin by
    every BIN_EVERY-th; lamps along the lit walks (lit_walks), one side. fx, fy: where it faces (a bench's front, a
    lamp's arm)."""
    x0, y0, x1, y1 = (float(v) for v in box)
    target = [w for w in walks if w.drawn and w.role in ("alley", "park", "walk", "cycleway")]
    drives = data.drives
    country = data.country
    bench_g, bench_h = bench_model(country)
    lamp_g, lamp_h = lamp_model(country)

    def mine(q):
        return x0 <= q[0] < x1 and y0 <= q[1] < y1

    roads_all = list(drives) + list(getattr(data, "decks", ()))     # (a deck too: a lamp stood on a bridge, Bukovice)

    def road_gap(p):
        if not roads_all:
            return 1e9
        d, j = _dist_to_lines(np.asarray(p, float)[None], roads_all)
        return float(d[0]) - roads_all[int(j[0])][1]

    def toward_walk(p):
        best = (1e9, None)
        for w in target:
            d, c = _nearest_on(w.xy, p)
            if d < best[0]:
                best = (d, c)
        return best

    def on_a_path(q, own):
        return any(w2 is not own and _nearest_on(w2.xy, q)[0] < w2.width / 2 + 0.4 for w2 in target)

    out = []
    for cls, x, y in data.points:
        if cls == "fountain" or not mine((x, y)):
            continue
        p = np.array([x, y])
        in_park = any(inside(r, p[None])[0] for r, _a, _n in data.parks)
        d, c = toward_walk(p)
        if not (in_park or d <= MAPPED_NEAR_WALK_M) or (c is None and not in_park) or road_gap(p) < ROAD_KEEP_M:
            continue
        face = (c - p) if c is not None and d > 1e-6 else np.array([1.0, 0.0])
        g, h = {"street_lamp": (lamp_g, lamp_h), "bench": (bench_g, bench_h), "waste_basket": (BIN, BIN_H)}[cls]
        out.append((g, x, y, float(face[0]), float(face[1]), h))
    mapped = [(g, x, y) for g, x, y, *_ in out]

    def free(p, groups, r):
        return all(float(np.hypot(p[0] - x, p[1] - y)) >= r for g, x, y in mapped if g in groups)

    # lamps along the lit walks
    lit = lit_walks(data, walks)
    spacing = LAMP_SPACING.get(country, LAMP_SPACING_M)
    for w in target:
        if w.fid not in lit:
            continue
        side = 1.0 if _h01(w.fid, "lampside") < 0.5 else -1.0
        centre = None
        if w.ring and data.rings:             # a round place's lamps stand round its outside (its middle: a fountain)
            mid = w.xy[len(w.xy) // 2]
            centre = min(data.rings, key=lambda r: float(np.hypot(*(r[0] - mid))))[0]
        for p, t, _s in _along(w.xy, spacing, spacing * (0.25 + 0.5 * _h01(w.fid, "lamp"))):
            nrm = np.array([-t[1], t[0]]) * side
            if centre is not None and float(np.dot(nrm, p - centre)) < 0:
                nrm = -nrm
            q = p + nrm * (w.width / 2.0 + LAMP_SIDE_M)
            if not mine(q) or road_gap(q) < (ROAD_LAMP_KEEP_M if w.group is None else ROAD_KEEP_M):
                continue
            if not free(q, LAMPS, spacing * 0.6) or on_a_path(q, w):
                continue
            out.append((lamp_g, float(q[0]), float(q[1]), float(-nrm[0]), float(-nrm[1]), lamp_h))
    n_bench = [0]

    def taken(q):
        return any(float(np.hypot(q[0] - o[1], q[1] - o[2])) < CLEAR_M for o in out)

    def bench(q, face, t):
        if not mine(q) or road_gap(q) < ROAD_KEEP_M or not free(q, BENCHES, 6.0) or taken(q):
            return False
        out.append((bench_g, float(q[0]), float(q[1]), float(face[0]), float(face[1]), bench_h))
        n_bench[0] += 1
        placed = True
        if n_bench[0] % BIN_EVERY == 0:
            b = q + t * 1.4
            if mine(b) and not taken(b):
                out.append((BIN, float(b[0]), float(b[1]), float(face[0]), float(face[1]), BIN_H))
        return placed

    # the round places: benches round their outer edge, facing the middle
    for c, r, w, _g in data.rings:
        n = RING_BENCHES if 2 * math.pi * r >= 25.0 else 2
        a0 = _h01("ring", round(float(c[0]), 1), round(float(c[1]), 1)) * 2 * math.pi
        for k in range(n):
            a = a0 + 2 * math.pi * (k + 0.5) / n
            u = np.array([math.cos(a), math.sin(a)])
            q = c + u * (r + w / 2 + BENCH_SIDE_M)
            if not on_a_path(q, None):
                bench(q, -u, np.array([-u[1], u[0]]))
    # benches in pairs along the parks' and squares' paths
    for w in target:
        if w.group is None or w.ring:
            continue
        length = w.length
        small = w.park_m2 < SMALL_PARK_M2
        if w.role != "alley" and not small:
            continue
        n = int(round(length * bench_density(w.park_m2) / 1000.0))
        if small and length >= SHORT_PATH_M:
            n = max(n, 1)
        if n <= 0:
            continue
        pairs = max(1, (n + 1) // 2)
        side = 1.0 if _h01(w.fid, "side") < 0.5 else -1.0
        for j in range(pairs):
            s0 = (j + 0.5) * length / pairs
            sd = side * (1.0 if j % 2 == 0 else -1.0)
            for k in ((-0.5, 0.5) if (n - 2 * j) >= 2 else (0.0,)):
                pt, t = _at(w.xy, s0 + k * BENCH_PAIR_M)
                for side_try in (sd, -sd):          # (the other side where a lamp or a path is in the way)
                    nrm = np.array([-t[1], t[0]]) * side_try
                    q = pt + nrm * (w.width / 2.0 + BENCH_SIDE_M)
                    if not on_a_path(q, w) and bench(q, -nrm, t):
                        break
    # mapped fountains beside a path
    for cls, x, y in data.points:
        if cls != "fountain":
            continue
        p = np.array([x, y])
        d, _c = toward_walk(p)
        if d > FOUNTAIN_WALK_M or not free(p, BENCHES, 10.0):
            continue
        for k in range(FOUNTAIN_BENCHES):
            a = 2 * math.pi * (k + 0.5) / FOUNTAIN_BENCHES
            u = np.array([math.cos(a), math.sin(a)])
            bench(p + FOUNTAIN_R_M * u, -u, np.array([-u[1], u[0]]))
    kept = []
    for item in out:
        if all(float(np.hypot(item[1] - k[1], item[2] - k[2])) >= CLEAR_M for k in kept):
            kept.append(item)
    return kept


def ground(frame, elev, xy):
    """Ground height (frame z) at xy (k, 2): elev(lat, lon) in the frame."""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    _, _, z = frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))
    return np.asarray(z, np.float64)


def rows_for(frame, data, walks, box, elev):
    """Forest rows [group, x, y, z, yaw, scale, height, rank] of furniture(data, walks, box), lamp lenses included."""
    pend = furniture(data, walks, box)
    if not pend:
        return []
    z = ground(frame, elev, np.array([[p[1], p[2]] for p in pend]))
    out, lamps = [], []
    for (group, x, y, fx, fy, h), zz in zip(pend, z):
        yaw = math.degrees(math.atan2(fy, fx)) + (BENCH_YAW if group in BENCHES else 0.0)
        row = [group, round(x, 2), round(y, 2), round(float(zz), 2), round(yaw % 360.0, 1), 1.0, h, 0.0]
        out.append(row)
        if group in LAMPS:
            lamps.append(row)
    if lamps:
        import furniture as streetfurniture
        out += streetfurniture.lamp_lenses(lamps)
    return out


# --- the ground under the strips (terrain layer: no grass through them) ------------------------------------------------

PAD_MARGIN_M = 0.3          # m the painted ground reaches past a strip's edge
LOOSE_KINDS = {"gravel", "ground"}


def pad_rings(frame, walks, box):
    """({"paved": [(lat (k,), lon (k,))], "loose": [...]}): the outlines of the drawn walks whose middle lies in tile box,
    each PAD_MARGIN_M wider than its strip, for the terrain's painters: the ground under a strip is its asphalt layer
    (paved kinds) or loose ground (gravel, bare), so the grass of the ground cover does not grow through the strip."""
    x0, y0, x1, y1 = (float(v) for v in box)
    out = {"paved": [], "loose": []}
    for w in walks:
        if not w.meshed:
            continue
        mid = w.xy[len(w.xy) // 2]
        if not (x0 <= mid[0] < x1 and y0 <= mid[1] < y1):
            continue
        sp = strip(w.xy, w.width + 2 * PAD_MARGIN_M, step=4.0)
        if sp is None:
            continue
        P = sp[0]
        ring = np.concatenate([P[0::2], P[1::2][::-1]])
        la, lo, _ = frame.enu_to_geodetic(ring[:, 0], ring[:, 1], np.zeros(len(ring)))
        out["loose" if w.kind in LOOSE_KINDS else "paved"].append((np.asarray(la, np.float64), np.asarray(lo, np.float64)))
    return out


def tile_pads(frame, tx, ty, md, pads):
    """Append the ground under the strips of road tile (tx, ty) to pads [(id, rings)] as busyard.tile_pads does its yards:
    "walk:" (painted asphalt, photo too) and "walkg:" (loose ground); the server keeps them and hands them to the terrain
    blocks' processes with their jobs, so no block process reads the map layers for them."""
    if not ON or md is None:
        return
    import roads
    try:
        box = roads.tile_bbox(tx, ty)
        _data, walks = tile_data(md, frame, box)
        rings = pad_rings(frame, walks, box)
        la, lo, _ = frame.enu_to_geodetic(np.array([(box[0] + box[2]) / 2]), np.array([(box[1] + box[3]) / 2]), np.zeros(1))
        tag = f"{float(la[0]):.5f},{float(lo[0]):.5f}"
        if rings["paved"]:
            pads.append(("walk:" + tag, rings["paved"]))
        if rings["loose"]:
            pads.append(("walkg:" + tag, rings["loose"]))
    except Exception as ex:                             # no paint rather than no road tile
        import logging
        logging.getLogger("earthd.blockworker").warning("walkway ground of tile (%s, %s): %s: %s", tx, ty,
                                                        type(ex).__name__, ex)


# --- tile entry points (blockworker, earthd) ---------------------------------------------------------------------

_MEMO = {}
MEMO_N = 8


def tile_data(md, frame, box):
    """(Data, [Walk]) of road tile box, remembered for the few tiles a build asks twice (the mesh and the furniture)."""
    key = (round(frame.lat0, 6), round(frame.lon0, 6)) + tuple(round(float(v), 1) for v in box)
    got = _MEMO.get(key)
    if got is None:
        data = collect(md, frame, box)
        got = (data, classify(data))
        if any_photo_missing(got[1]):       # a photo tile not on the disk yet: nothing built from this is kept
            import revisit
            revisit.fault()
            return got
        if len(_MEMO) >= MEMO_N:
            _MEMO.pop(next(iter(_MEMO)))
        _MEMO[key] = got
    return got


def rows(frame, md, box, elev):
    """Forest rows of the lamps, benches and bins of the tile at box (the walks' own furniture)."""
    if not ON or md is None:
        return []
    data, walks = tile_data(md, frame, box)
    return rows_for(frame, data, walks, box, elev)
