"""The 3D map's labels (mod map3d.lua; the user, 2026-11-07: "handle labels similar how modern map apps like Apple Maps
dose it. As you zoom in more and more info appears and try to make it as clean as this").

Every label carries the web-map zoom it starts at (min zoom) and stops at (max zoom) and a rank (lower first): the
map screen (EarthMap3d.vue) shows those its zoom allows, the most important first, none overlapping, fading in and out.
Sources, by zoom:
  world   continents, oceans and seas, countries, states and provinces, cities, mountain ranges and deserts from
          Natural Earth (server/data/world_labels.json.gz, tools/build_world_labels.py; public domain): its own
          min / max label zooms;
  towns   Overture's localities round the view (placenames.labels), from zoom ~9; a town Natural Earth has already
          (the same name within TOWN_SAME_M) is left out;
  streets the named roads of the map cells round the view (zoom STREET_MIN_Z+): points along each every few hundred
          metres with the road's bearing (earthfast.polyline_label_points), names shortened and in capitals as map
          apps write them on the photo ("E FAYETTE ST");
  places  Overture places of a few kinds people look for (food, shops, hotels, sights, parks, schools, hospitals,
          stations), from zoom POI_MIN_Z: the surer and the more notable first.
Rows: [kind, text, lat, lon, min zoom, max zoom, rank, extra, bearing]."""
import gzip
import json
import math
import os
import re
import threading

import numpy as np

import fast
import dmath
import dnp
import mapcells

WORLD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "world_labels.json.gz")
WORLD_RADIUS_MAX_M = 4000e3     # world labels farther than this from the view are never sent
TOWN_SAME_M = 6000.0            # an Overture town this near a Natural Earth city of the same name is left out
TOWN_MIN_Z = {"megacity": 3.0, "city": 6.5, "town": 9.0, "village": 11.0, "hamlet": 12.5}
TOWN_MIN_Z_DEFAULT = 12.5       # a locality without a class (a city's district)
STREET_MIN_Z = 13.0             # the view's zoom from which street names are read
STREET_RADIUS_M = 3500.0
STREET_CLASS_Z = {"motorway": 11.5, "trunk": 12.0, "primary": 13.0, "secondary": 13.5, "tertiary": 14.0,
                  "residential": 14.8, "unclassified": 14.8, "living_street": 15.2, "pedestrian": 15.8,
                  "service": 16.5}
STREET_CLASS_RANK = {"motorway": 50, "trunk": 51, "primary": 52, "secondary": 53, "tertiary": 54, "residential": 56,
                     "unclassified": 56, "living_street": 57, "pedestrian": 58, "service": 59}
POI_MIN_Z = 15.0
POI_RADIUS_M = 1800.0
POI_MAX = 300
POI_MIN_CONFIDENCE = 0.75

# Overture basic_category -> icon group: the kinds people look for on a map (the rest, offices, services, clinics,
# shops' back rooms, is left out: ~200 kinds round Fells Point, 2026-11-07)
POI_EXACT = {
    "transit": {"airport", "train_station", "subway_station", "bus_station", "light_rail_station", "metro_station",
                "ferry_terminal", "transportation_station", "railway_station", "bus_terminal"},
    # (not historic_site: Overture's are mostly apartment blocks, "1305 Dock Street Apartments"; nor the vague
    # arts_and_entertainment)
    "culture": {"museum", "art_gallery", "monument", "music_venue", "landmark", "theatre", "theater", "performing_arts_theater", "cinema", "movie_theater",
                "stadium_or_arena", "stadium", "arena", "aquarium", "zoo", "tourist_attraction", "castle",
                "landmark_and_historical_building", "amusement_park", "concert_hall", "planetarium"},
    "park": {"park", "marina", "garden", "botanical_garden", "beach", "nature_reserve", "playground", "dog_park",
             "national_park", "state_park", "campground"},
    "lodging": {"hotel", "lodging", "resort", "motel", "hostel", "bed_and_breakfast", "inn", "guest_house"},
    "food": {"restaurant", "casual_eatery", "bar", "coffee_shop", "cafe", "fast_food_restaurant", "smoothie_juice_bar",
             "brewery", "winery", "lounge", "bakery", "dessert_shop", "ice_cream_shop", "pub", "food_court", "diner",
             "food_truck_stand", "tea_room", "wine_bar", "cocktail_bar", "sports_bar", "deli"},
    "shop": {"shopping", "farmers_market", "supermarket", "grocery_store", "shopping_mall", "department_store",
             "market"},
    "education": {"college_university", "elementary_school", "high_school", "middle_school", "school", "library",
                  "university"},
    "health": {"hospital"},
    "fuel": {"gas_station", "ev_charging_station"},
}
POI_SUFFIX = (("food", "_restaurant"), ("shop", "_store"))
# (close together: a group ahead of another filled the screen's few places with museums only, 2026-11-07; within
# them the surer go first)
POI_GROUP_RANK = {"transit": 70, "culture": 72, "food": 72, "park": 72.5, "lodging": 72.5, "shop": 73, "education": 73,
                  "health": 73, "fuel": 74}

_world = None
_world_lock = threading.Lock()


def world():
    """Natural Earth's labels as arrays (loaded once)."""
    global _world
    with _world_lock:
        if _world is None:
            with gzip.open(WORLD_FILE, "rt", encoding="utf-8") as fh:
                rows = json.load(fh)["rows"]
            _world = {"rows": rows, "lat": np.array([r[2] for r in rows], np.float64),
                      "lon": np.array([r[3] for r in rows], np.float64),
                      "minz": np.array([r[4] for r in rows], np.float64),
                      "maxz": np.array([r[5] for r in rows], np.float64)}
        return _world


def _dist_m(lat, lon, lats, lons):
    p1, p2 = math.radians(lat), np.radians(lats)
    dl = np.radians(lons - lon)
    a = dnp.sq(dnp.sin((p2 - p1) / 2)) + dmath.cos(p1) * dnp.cos(p2) * dnp.sq(dnp.sin(dl / 2))
    return 2 * 6371000.0 * dnp.arcsin(np.minimum(1.0, np.sqrt(a)))


def world_labels(lat, lon, zoom, radius_m):
    """Natural Earth's rows within radius_m of (lat, lon) whose zooms take in zoom (+- a level: they fade at the
    map screen by their own zooms)."""
    w = world()
    r = min(float(radius_m), WORLD_RADIUS_MAX_M)
    ok = (w["minz"] <= zoom + 1.0) & (w["maxz"] >= zoom - 1.0)
    idx = np.flatnonzero(ok)
    if not len(idx):
        return []
    d = _dist_m(lat, lon, w["lat"][idx], w["lon"][idx])
    out = []
    for i in idx[d <= r]:
        row = list(w["rows"][i]) + [None]
        row[1] = clean(row[1])
        out.append(row)
    return out


def clean(text):
    """A label's text with its runs of spaces made one ("Washington,  D.C.")."""
    return " ".join(str(text).split())


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def town_labels(towns, world_rows):
    """placenames.labels rows [(name, lat, lon, class, population)] as label rows; those Natural Earth has (a city of
    the same name within TOWN_SAME_M) left out."""
    cities = [(r[2], r[3], _norm(r[1])) for r in world_rows if r[0] == "city"]
    out = []
    for name, la, lo, cls, pop in towns:
        n = _norm(name)
        if any(c[2] == n and abs(c[0] - la) < 0.08 and abs(c[1] - lo) < 0.12
               and float(_dist_m(la, lo, np.array([c[0]]), np.array([c[1]]))[0]) < TOWN_SAME_M for c in cities):
            continue
        pop = float(pop) if pop and math.isfinite(float(pop)) else 0.0
        z = TOWN_MIN_Z.get(cls, TOWN_MIN_Z_DEFAULT)
        if pop > 0:     # (the bigger the earlier, within its class's level)
            z = min(z, max(3.0, 15.0 - 1.15 * dmath.log10(max(pop, 10.0))))
        kind = "town" if cls else "district"
        out.append([kind, clean(name), round(la, 5), round(lo, 5), round(z, 2), 99, round(40 - dmath.log10(max(pop, 1.0)), 2),
                    cls or "", None])
    return out


# --- streets --------------------------------------------------------------------------------------------------------
_ABBREV = {"street": "ST", "avenue": "AVE", "road": "RD", "boulevard": "BLVD", "drive": "DR", "lane": "LN",
           "court": "CT", "place": "PL", "terrace": "TER", "parkway": "PKWY", "highway": "HWY", "square": "SQ",
           "expressway": "EXPY", "circle": "CIR", "alley": "ALY", "way": "WAY", "trail": "TRL", "turnpike": "TPKE"}
_DIRS = {"north": "N", "south": "S", "east": "E", "west": "W", "northeast": "NE", "northwest": "NW",
         "southeast": "SE", "southwest": "SW"}


def street_text(name):
    """A road's name as map apps write it on the photo: in capitals, the English kind and direction words shortened
    ("East Fayette Street" -> "E FAYETTE ST"; other languages' names only in capitals)."""
    words = str(name).split()
    if not words:
        return ""
    if len(words) > 1 and words[0].lower() in _DIRS:
        words[0] = _DIRS[words[0].lower()]
    if len(words) > 1 and words[-1].lower() in _ABBREV:
        words[-1] = _ABBREV[words[-1].lower()]
    if len(words) > 2 and words[-1].lower() in _DIRS:
        words[-1] = _DIRS[words[-1].lower()]
    return " ".join(words).upper()


def street_spacing(zoom):
    """Metres between a road's labels: a few screen widths of text."""
    return max(120.0, 450.0 * 2 ** (15.0 - zoom))


def street_labels(cells, lat, lon, zoom, radius_m=STREET_RADIUS_M):
    radius_m = min(radius_m, STREET_RADIUS_M)
    """Label rows of the named roads in cells (mapgeom.Features of the roads layer, None skipped) within radius_m."""
    fast.require(159, "polyline_label_points")
    seen, starts, ends, meta = set(), [], [], []
    coords = []
    off = 0
    for f in cells:
        if f is None or not len(f.ids):
            continue
        names, classes = f.attrs.get("name"), f.attrs.get("class")
        if names is None or classes is None:
            continue
        local = []
        for i in range(len(f.ids)):
            nm, cls = str(names[i]), str(classes[i])
            if not nm or cls not in STREET_CLASS_Z or f.ids[i] in seen:
                continue
            seen.add(f.ids[i])
            for p in range(f.part_off[i], f.part_off[i + 1]):
                for r in range(f.ring_off[p], f.ring_off[p + 1]):
                    a, b = int(f.coord_off[r]), int(f.coord_off[r + 1])
                    if b - a >= 2:
                        local.append((a, b, nm, cls))
        if local:
            coords.append(f.lonlat)
            for a, b, nm, cls in local:
                starts.append(off + a)
                ends.append(off + b)
                meta.append((nm, cls))
            off += len(f.lonlat)
    if not starts:
        return []
    lonlat = np.ascontiguousarray(np.concatenate(coords), np.float64)
    fi, lo, la, br = fast._ef.polyline_label_points(lonlat, np.asarray(starts, np.int64), np.asarray(ends, np.int64),
                                                    float(street_spacing(zoom)))
    d = _dist_m(lat, lon, la, lo)
    out, done = [], {}
    sp = street_spacing(zoom) * 0.6
    for k in np.flatnonzero(d <= radius_m):
        nm, cls = meta[int(fi[k])]
        text = street_text(nm)
        if not text:
            continue
        # (a street cut into many segments: one label per spacing along it)
        near = done.setdefault(text, [])
        if any(abs(q[0] - la[k]) * 111320 < sp and abs(q[1] - lo[k]) * 111320 * dmath.cos(math.radians(lat)) < sp
               for q in near):
            continue
        near.append((float(la[k]), float(lo[k])))
        out.append(["street", text, round(float(la[k]), 6), round(float(lo[k]), 6), STREET_CLASS_Z[cls], 99,
                    STREET_CLASS_RANK[cls], cls, round(float(br[k]), 1)])
    return out


# --- districts -----------------------------------------------------------------------------------------------------
HOOD_MIN_Z = 12.5               # the view's zoom from which district names are read
HOOD_RADIUS_M = 4000.0
HOOD_Z = {"macrohood": 13.3, "neighborhood": 14.6}
HOOD_RANK = {"macrohood": 43.0, "neighborhood": 45.0}
HOOD_SKIP = re.compile(r"\b(association|homeowners|hoa|condominium|condo|council|improvement)\b", re.I)


def hood_labels(cells, lat, lon, radius_m=HOOD_RADIUS_M):
    """Label rows of the neighbourhoods and macrohoods in cells (mapgeom.Features of the hoods layer) within radius_m;
    residents' associations ("McHenry Pointe Homeowners Association") left out."""
    rows, seen = [], set()
    for f in cells:
        if f is None or not len(f.ids):
            continue
        a = f.attrs
        cx, cy = np.asarray(a["cx"], np.float64), np.asarray(a["cy"], np.float64)
        d = _dist_m(lat, lon, cy, cx)
        for i in np.flatnonzero(d <= radius_m):
            name, sub = clean(a["name"][i]), str(a["subtype"][i])
            if not name or f.ids[i] in seen or sub not in HOOD_Z or HOOD_SKIP.search(name):
                continue
            seen.add(f.ids[i])
            rows.append(["district", name, round(float(cy[i]), 6), round(float(cx[i]), 6), HOOD_Z[sub], 99,
                         HOOD_RANK[sub], sub, None])
    return rows


# --- places ---------------------------------------------------------------------------------------------------------
def poi_group(category):
    """The icon group of an Overture basic_category, or None (a kind not shown)."""
    c = str(category or "").lower()
    if not c:
        return None
    for g, kinds in POI_EXACT.items():
        if c in kinds:
            return g
    for g, suffix in POI_SUFFIX:
        if c.endswith(suffix):
            return g
    return None


def poi_labels(cells, lat, lon, radius_m=POI_RADIUS_M, limit=POI_MAX):
    """Label rows of the places worth showing in cells (mapgeom.Features of the places layer) within radius_m: the
    surer and the more notable first, at most limit."""
    rows, seen = [], set()
    for f in cells:
        if f is None or not len(f.ids):
            continue
        a = f.attrs
        conf = np.asarray(a.get("confidence", np.zeros(len(f.ids))), np.float64)
        cx, cy = np.asarray(a["cx"], np.float64), np.asarray(a["cy"], np.float64)
        d = _dist_m(lat, lon, cy, cx)
        for i in np.flatnonzero((d <= radius_m) & (conf >= POI_MIN_CONFIDENCE)):
            if f.ids[i] in seen:
                continue
            name = str(a["name"][i]) if a.get("name") is not None else ""
            status = str(a["operating_status"][i]) if a.get("operating_status") is not None else ""
            if not name or status in ("closed", "permanently_closed", "temporarily_closed"):
                continue
            g = poi_group(a["basic_category"][i] if a.get("basic_category") is not None else "")
            if g is None:
                continue
            seen.add(f.ids[i])
            brand = bool(a.get("brand_name") is not None and str(a["brand_name"][i]))
            rank = POI_GROUP_RANK[g] + (1.0 - float(conf[i])) * 4 - (0.5 if brand else 0.0)
            z = POI_MIN_Z + (0.0 if g in ("transit", "culture", "park") else 0.7) + (1.0 - float(conf[i])) * 2
            rows.append(["poi", clean(name), round(float(cy[i]), 6), round(float(cx[i]), 6), round(z, 2), 99, round(rank, 2),
                         g, None])
    rows.sort(key=lambda r: (r[6], r[1]))
    return rows[:limit]


def near_cells(lat, lon, radius_m):
    """The map cells (zoom 14) within radius_m of (lat, lon)."""
    dla = radius_m / 111320.0
    dlo = radius_m / (111320.0 * max(0.05, dmath.cos(math.radians(lat))))
    return mapcells.cells_for_bbox(lat - dla, lon - dlo, lat + dla, lon + dlo)
