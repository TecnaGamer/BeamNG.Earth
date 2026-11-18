"""The modding API's places (api.places / api.buildings in the mod's api.lua, docs/ADDONS.md "Modding API v1"):
Overture's places of every kind round a point, and optionally the buildings' footprints, for add-ons that need real
places (a career's jobs, a mission's pick-up and drop-off).

Only what the server has already: the map cells' places and buildings layers (mapdata.py, the same cells the 3D map's
labels read, maplabels.near_cells); a request never adds a source or a download of its own. earthd.Connection.places
answers from the cells on disk at once and, when some were missing, once more after they came (the same way as
map_labels), each answer saying whether it was complete.

Request {type="places", id, lat, lon, radius, categories, limit, cursor, buildings, minConfidence, cachedOnly} ->
places_result {id, places, buildings, complete, pending, next, truncated, error}:
  places     [{id, name, category, lat, lon, confidence, open}, ...]   nearest first; `open` is Overture's
             operating_status ("open", "temporarily_closed", "permanently_closed", "" not known)
  buildings  [{id, lat, lon, height, levels, name, class, outline}, ...] with buildings=true only; outline the
             footprint's outer ring [[lat, lon], ...]
  next       the cursor of the next page (absent on the last)
Rows are sorted by (distance, id), and a cursor is the key of a page's last row, so a page asked again after more
cells came neither repeats nor skips the rows of the pages before it (a nearer place come since is not in them).

cachedOnly asks for no cell it does not already have on disk, and for no second answer: it queues no background job
of its own, so a radius well past RADIUS_MAX_M costs only what is already there (an add-on looking for the few car
dealers of a whole region). It is not a promise that nothing is downloaded: a cell whose stored layer is damaged is
a miss like any other and mapdata rebuilds it from its sources (test_corrupt_layer_files_are_rebuilt). Such a
wide search must name categories and reads at most CACHED_CELLS_MAX cells; truncated says the box round the point
held more than that, so the reach was shortened and cells inside the radius asked for were left unread (a radius
whose box fits is never shortened, however close to the cap it comes). Buildings keep BUILDINGS_RADIUS_MAX_M either
way."""
import math

import numpy as np
import dmath

import mapcells
import maplabels

RADIUS_DEFAULT_M = 1000.0
RADIUS_MAX_M = 3000.0           # (up to ~16 z14 cells round the point at mid latitudes: read in well under a second)
# cachedOnly only, which fetches nothing: the reach then costs no more than the cells of it already on disk
RADIUS_WIDE_MAX_M = 25000.0     # (484 z14 cells at the equator, 784 round Baltimore, 1024 round Munich, 1764 at 60 N)
CACHED_CELLS_MAX = 2048         # (half mapcells.cells_for_bbox's own 4096-cell box limit, so it is never asked for more)
LIMIT_DEFAULT, LIMIT_MAX = 50, 200
CATEGORIES_MAX = 32
# footprints are bigger than places (~20 vertices each) and fill a town's centre quickly: a smaller reach and count
BUILDINGS_RADIUS_MAX_M = 500.0
BUILDINGS_MAX = 100
OUTLINE_MAX_POINTS = 200        # (a building's outer ring longer than this is thinned evenly: a stadium's 600)
CURSOR_MAX_LEN = 200


def _num(msg, name, default):
    v = msg.get(name)
    if v is None:
        return default
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def cached_reach(lat, lon, radius_m):
    """(the reach a cachedOnly request reads, whether cells inside radius_m were left out of it): radius_m itself
    where the box round the point holds at most CACHED_CELLS_MAX cells, else a shorter reach whose box does.
    The box is counted, not estimated, so a radius that fits is never cut back by a metre and never reports
    truncated; truncated means the box really held more cells than the cap, so cells inside the radius really
    were left unread. The fall-back reach comes from the cell size instead: a z14 cell is
    EARTH_CIRC / 2**14 * cos(lat) metres a side, so the box holds about (2 * reach / that)**2 cells, and
    side * (sqrt(CACHED_CELLS_MAX) - 2) / 2 is inside the cap at every latitude (its worst box is 1980 cells)
    and always past RADIUS_MAX_M (4564 m at the Web-Mercator limit, where a cell is 211 m).
    Cutting the reach back rather than dropping cells out of the box keeps the answer concentric: it is then every
    place out to the shorter reach, which is what the (distance, id) order and its cursors need, and it is the same
    cut on every page, which a cut by what is on disk would not be."""
    try:
        if len(maplabels.near_cells(lat, lon, radius_m)) <= CACHED_CELLS_MAX:
            return radius_m, False
    except ValueError:      # (mapcells.cells_for_bbox refuses a box past its own 4096, far past the cap)
        pass
    side_m = mapcells.EARTH_CIRC / (1 << mapcells.ZOOM) * max(0.05, dmath.cos(math.radians(lat)))
    return min(radius_m, side_m * (math.sqrt(CACHED_CELLS_MAX) - 2.0) / 2.0), True


def request(msg):
    """The checked request as a dict, or (None, why) for one that cannot be answered. "radius" is the reach to read,
    which a cachedOnly request's cell cap can cut back ("truncated")."""
    lat, lon = _num(msg, "lat", None), _num(msg, "lon", None)
    if lat is None or lon is None or not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None, "bad coordinates"
    radius = _num(msg, "radius", RADIUS_DEFAULT_M)
    limit = _num(msg, "limit", LIMIT_DEFAULT)
    min_conf = _num(msg, "minConfidence", 0.0)
    if radius is None or limit is None or min_conf is None:
        return None, "radius, limit and minConfidence must be numbers"
    cats = msg.get("categories")
    if cats is None or cats == {}:      # (the mod's JSON writes an empty Lua table as {})
        cats = []
    if not isinstance(cats, list) or len(cats) > CATEGORIES_MAX or not all(isinstance(c, str) for c in cats):
        return None, f"categories must be a list of at most {CATEGORIES_MAX} strings"
    cursor = msg.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > CURSOR_MAX_LEN or _cursor_key(cursor) is None):
        return None, "bad cursor"
    cached = msg.get("cachedOnly") is True
    cats = [c.strip().lower() for c in cats if c.strip()]
    radius = max(1.0, min(radius, RADIUS_WIDE_MAX_M if cached else RADIUS_MAX_M))
    # (only a cachedOnly radius gets past RADIUS_MAX_M, and a reach that wide is for sparse kinds. It holds down the
    # rows kept in memory, not the cells read: every cell in reach is read and matcher() filters its rows after)
    if radius > RADIUS_MAX_M and not cats:
        return None, f"a radius over {RADIUS_MAX_M:.0f} m needs categories"
    reach, cut = cached_reach(lat, lon, radius) if cached else (radius, False)
    return {"lat": lat, "lon": lon, "radius": reach, "truncated": cut,
            "limit": int(max(1, min(limit, LIMIT_MAX))), "categories": cats, "cached_only": cached,
            "cursor": cursor, "buildings": msg.get("buildings") is True, "min_conf": max(0.0, min(min_conf, 1.0))}, None


def matcher(categories):
    """A test of (basic_category, taxonomy) against the asked categories: an exact Overture basic_category or taxonomy
    value ("gas_station"), a prefix ending in '*' ("car_*": car_dealer, car_wash ...), or "group:<name>", the 3D map's
    icon groups (maplabels.POI_EXACT: food, shop, lodging, ...). No categories: every place."""
    if not categories:
        return lambda cat, tax: True
    exact = {c for c in categories if not c.endswith("*") and not c.startswith("group:")}
    prefixes = tuple(c[:-1] for c in categories if c.endswith("*") and len(c) > 1)
    groups = {c[6:] for c in categories if c.startswith("group:")}

    def test(cat, tax):
        if cat in exact or (tax and tax in exact):
            return True
        if prefixes and (cat.startswith(prefixes) or (tax and tax.startswith(prefixes))):
            return True
        return bool(groups) and maplabels.poi_group(cat) in groups
    return test


def _col(a, name, n):
    v = a.get(name)
    return v if v is not None else np.full(n, "")


def place_rows(cells, lat, lon, radius_m, match=None, min_conf=0.0):
    """[((distance m, id), row), ...] of the named places in cells (mapdata Features of the places layer, None
    skipped) within radius_m that match, nearest first; a place in two cells' copies taken once."""
    match = match or matcher([])
    out, seen = [], set()
    for f in cells:
        if f is None or not len(f.ids):
            continue
        a, n = f.attrs, len(f.ids)
        conf = np.asarray(a["confidence"], np.float64) if a.get("confidence") is not None else np.full(n, np.nan)
        cx, cy = np.asarray(a["cx"], np.float64), np.asarray(a["cy"], np.float64)
        names, cats, taxes, status = (_col(a, "name", n), _col(a, "basic_category", n), _col(a, "taxonomy", n),
                                      _col(a, "operating_status", n))
        d = maplabels._dist_m(lat, lon, cy, cx)
        # (a place with no confidence is kept unless one was asked for: NaN fails every >=)
        ok = (d <= radius_m) & ((conf >= min_conf) | (np.isnan(conf) & (min_conf <= 0.0)))
        for i in np.flatnonzero(ok):
            pid = str(f.ids[i])
            name = maplabels.clean(names[i])
            if not name or pid in seen:
                continue
            cat, tax = str(cats[i]).lower(), str(taxes[i]).lower()
            if not match(cat, tax):
                continue
            seen.add(pid)
            c = float(conf[i])
            out.append(((round(float(d[i]), 2), pid),
                        {"id": pid, "name": name, "category": cat or tax, "lat": round(float(cy[i]), 6),
                         "lon": round(float(cx[i]), 6), "confidence": round(c, 3) if math.isfinite(c) else None,
                         "open": str(status[i])}))
    out.sort(key=lambda r: r[0])
    return out


def _cursor_key(cursor):
    d, sep, pid = str(cursor).partition("|")
    try:
        d = float(d)
    except ValueError:
        return None
    return (d, pid) if sep and pid and math.isfinite(d) else None


def page(keyed, cursor, limit):
    """(rows, next cursor or None): the rows after cursor (None: from the start), at most limit."""
    start = 0
    if cursor is not None:
        key = _cursor_key(cursor)
        start = next((i for i, (k, _) in enumerate(keyed) if k > key), len(keyed))
    chunk = keyed[start:start + limit]
    more = start + limit < len(keyed)
    nxt = f"{chunk[-1][0][0]:.2f}|{chunk[-1][0][1]}" if more and chunk else None
    return [r for _, r in chunk], nxt


def _f(v):
    v = float(v)
    return round(v, 1) if math.isfinite(v) else None


def building_rows(cells, lat, lon, radius_m, limit=BUILDINGS_MAX):
    """[{id, lat, lon, height, levels, name, class, outline}, ...] of the buildings in cells (mapdata Features of the
    buildings layer) whose centre is within radius_m, nearest first, at most limit; height (m) and levels (Overture's
    num_floors) None where not mapped."""
    radius_m = min(radius_m, BUILDINGS_RADIUS_MAX_M)
    found, seen = [], set()
    for f in cells:
        if f is None or not len(f.ids):
            continue
        a = f.attrs
        cx, cy = np.asarray(a["cx"], np.float64), np.asarray(a["cy"], np.float64)
        d = maplabels._dist_m(lat, lon, cy, cx)
        for i in np.flatnonzero(d <= radius_m):
            bid = str(f.ids[i])
            p = int(f.part_off[i])
            if bid in seen or f.part_off[i + 1] <= p or f.ring_off[p + 1] <= f.ring_off[p]:
                continue
            seen.add(bid)
            found.append((round(float(d[i]), 2), bid, f, i))
    found.sort(key=lambda r: (r[0], r[1]))
    out = []
    for _, bid, f, i in found[:limit]:
        a, n = f.attrs, len(f.ids)
        r = f.ring_off[f.part_off[i]]               # (the first part's outer ring)
        ring = f.lonlat[int(f.coord_off[r]):int(f.coord_off[r + 1])]
        if len(ring) > OUTLINE_MAX_POINTS:
            ring = ring[np.linspace(0, len(ring) - 1, OUTLINE_MAX_POINTS).round().astype(np.int64)]
        height = a.get("height")
        levels = a.get("num_floors")
        out.append({"id": bid, "lat": round(float(a["cy"][i]), 6), "lon": round(float(a["cx"][i]), 6),
                    "height": _f(height[i]) if height is not None else None,
                    "levels": _f(levels[i]) if levels is not None else None,
                    "name": maplabels.clean(_col(a, "name", n)[i]), "class": str(_col(a, "class", n)[i]),
                    "outline": [[round(float(p[1]), 6), round(float(p[0]), 6)] for p in ring]})
    return out


def near_cells(lat, lon, radius_m):
    """The map cells within radius_m of (lat, lon): the 3D map's labels' (maplabels.near_cells)."""
    return maplabels.near_cells(lat, lon, radius_m)
