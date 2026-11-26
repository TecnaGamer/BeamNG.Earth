"""Generated ground (BNE_GEN_GROUND=on: the settings page's Experimental "Generated ground"; the user, 2026-11-15: "What
if instead of trying to use the highest res pics, we use them as a base and generate the extra detail?").

Why: the regional 1 m aerial photo services were the world's bottleneck (telemetry of 2026-11-15: our own per-host
limits queued ~70 % of their requests, PASDA, Ohio, Japan's GSI, North Carolina, Virginia, Spain's PNOA, NSW, Indiana,
New Jersey, Vienna, Connecticut; Poland's and Ontario's services gave up on tens of thousands a day; USGS's NAIP
service answered 11.7 % 5xx), and many countries have no free 1 m photo at all.

What: a block's base colour (earthd.Server.write_base / blockworker.base_photo, BASE_RENDER_PX over the 1 km block) is
made from data every place has, instead of the 1 m photo:
- the colour: Sentinel-2 (10 m; the project's own packs, bngs2, else EOX's 2016 cloudless mosaic), rendered over the
  block's texels as before (imagery.render: the chain cut to SOURCES, chain());
- the regions at 1 m: ESA WorldCover's class (read at warped points, surface.classes, as the ground's materials are),
  the built-up land's lawns (surface.lawns), Overture's land-use and water polygons, the canopy heights (Meta / WRI
  CHM, 1.2 m: real tree crowns, street trees included), the drawn roads, lots and pads (surface.road_mask) and the
  buildings' grown footprints (buildings.photo_mask);
- earthfast.gen_ground (native/earthfast/src/genground.rs): near each region's border the Sentinel-2 colour of the
  region's own pure texels (no 10 m smear across a forest edge, a field's edge, a road), inside the photo itself, and
  the kind's fine detail (noise at a few scales, crowns shaded from the canopy heights).
Stage 2 (the user, 2026-11-16: "we can use OSM and other map data to help guide the generator"): OSM's tags as
Overture's layers carry them (land_use / water / infrastructure source_tags, the roads layer's osm_* columns) draw what
a 10 m photo cannot: pitches by surface and sport (turf with mown stripes and its outline, clay, tartan, hard courts),
squares by surface (pavers, brick, concrete), pools, orchards / vineyards / allotments / glasshouses / cemeteries as
rows along each polygon's dominant edge (earthfast.gg_poly_axes; the rows' offset hashed from its id, so its pieces in
every cell and block agree), commercial / industrial land's yards paved, unpaved lots; lines buffered to their widths
(earthfast.gg_fill_lines): footways and paths (concrete in town, earth elsewhere, or their mapped surface), tracks and
unpaved drives, railway ballast, hedges and tree rows, streams and ditches, piers; the canopy model's hedgerows grown
by a metre (it draws their tall core only). The season grade (imagery's: fall colours, the bare winter canopy, the
spring flush) goes over the generated ground (render). Farmland keeps the photo path's furrows (surface.photo_fields,
the crop shrubs line up with them).
The rest of the photo's path is unchanged: the roofs filled, the roads, lots, fields' furrows and golf painted in at
0.5 m, the cliffs as rock, the terrain material layers' sharp detail textures over it in the game.

With the switch on, the imagery chain holds SOURCES alone (chain(), earthd's main): no 1 m photo is fetched for the
ground, the far rings, the warm-up or the prefetch, nor for the roofs, squares or street fronts
(buildings._photo_sources).
Off (the default): nothing changes. The switch is a BNE_* variable, so it is in revisit.code_key (the base photos' and
far tiles' revisit keys, the blocks, the road tiles), switches_key and the decision archives' vkey_doc like every build
switch (experimental.py). Every input is map data whose versions are in the base photo's key (mapdata.versions()); a
photo made before some of it was on disk is marked (GEN_PENDING) and made again once it is.

The noise is anchor-locked (the anchor frame's east / north): blocks of one anchor agree along their seams; another
anchor's view of the same place draws another grain (each anchor's photos are its own in the caches anyway).
"""
import hashlib
import json
import logging
import os
import threading
import time

import numpy as np

import dnp
import fast
import mapcells
import revisit

log = logging.getLogger("earthd.genground")

ON = os.environ.get("BNE_GEN_GROUND", "off").strip().lower() in ("on", "1", "yes", "true")
PAIR_NEED = 370             # earthfast gen_ground_pair (the ground and its canopy version in one pass)
NEED = 314                  # earthfast gg_* / gen_ground (stage 2: lines, surfaces, patterns; linear axes)
# m round a block its generated ground is made over and cut from: a texel's colour reads the inputs this far round
# (genground.rs: windows FAR_R_M 48 + the lattice and purity margins), so overlapping blocks agree
HALO_M = 128.0
# the photo's pending mark while some map data it reads is not on disk (earthd.Server.base_ready / _base_kept)
GEN_PENDING = ("genground_data",)
LAYERS = ("land_use", "water", "infrastructure", "canopy", "landcover", "roads", "buildings")
# The imagery sources the generated ground is made from (imagery.SOURCES): Sentinel-2 alone, global (the project's packs
# at their own zooms and enlarged for the block's finer texels, else EOX's 2016 mosaic; EOX 2023, CC BY-NC-SA, until
# 2026-11-23)
SOURCES = ("bngs2", "bngs2fine", "eox2016")

# Kinds of ground (genground.rs: the same numbers)
(NONE, TREE, SHRUB, GRASS, CROP, BUILT, BARE, SNOW, WATER, WETLAND, LAWN, PAVED, ROOF, GRAVEL, PATH, BRICK, PAVERS,
 CONCRETE, TURF, ARTTURF, CLAY, TARTAN, POOL, ORCHARD, VINEYARD, ALLOTMENT, GREENHOUSE, CEMETERY, COURT) = range(29)
LOT, PATH_AUTO = 29, 30         # (a polygon's / a line's kind only: gg_regions makes them another per texel)
KIND_NAMES = ("none", "tree", "shrub", "grass", "crop", "built", "bare", "snow", "water", "wetland", "lawn", "paved",
              "roof", "gravel", "path", "brick", "pavers", "concrete", "turf", "artturf", "clay", "tartan", "pool",
              "orchard", "vineyard", "allotment", "greenhouse", "cemetery", "court")
# WorldCover class -> kind (maprasters.CLASSES)
WC_KIND = {10: TREE, 20: SHRUB, 30: GRASS, 40: CROP, 50: BUILT, 60: BARE, 70: SNOW, 80: WATER, 90: WETLAND,
           95: WETLAND, 100: GRASS}
_WC_LUT = np.zeros(256, np.uint8)
for _c, _k in WC_KIND.items():
    _WC_LUT[_c] = _k
# Overture land_use class -> kind (others: the land cover's own); OSM's tags refine some (land_use_kind)
LAND_USE_KIND = {
    **{c: LAWN for c in ("park", "garden", "village_green", "recreation_ground", "golf_course", "dog_park", "lawn",
                         "common")},
    **{c: GRASS for c in ("meadow", "grass", "grassland", "greenfield", "fell")},
    **{c: CROP for c in ("farmland", "plant_nursery", "farm")},
    **{c: SHRUB for c in ("heath", "scrub", "shrubland")},
    **{c: TREE for c in ("forest", "wood")},
    **{c: BARE for c in ("quarry", "landfill", "construction", "brownfield", "sand", "beach", "bare_rock",
                         "scree", "salt_pond")},
    **{c: WETLAND for c in ("wetland", "marsh", "swamp", "bog", "reedbed")},
    # (stage 2: OSM's surfaces and patterns)
    "orchard": ORCHARD, "vineyard": VINEYARD, "allotments": ALLOTMENT, "greenhouse_horticulture": GREENHOUSE,
    "cemetery": CEMETERY, "grave_yard": CEMETERY, "track": TARTAN, "railway": GRAVEL,
    **{c: LOT for c in ("commercial", "industrial", "retail")},
}
# a pitch's surface (OSM surface=*) -> kind; without one, by its sport, else grass
PITCH_SURFACE = {"grass": TURF, "artificial_turf": ARTTURF, "artificial_grass": ARTTURF, "clay": CLAY,
                 "tartan": TARTAN, "rubber": TARTAN, "asphalt": COURT, "concrete": COURT, "acrylic": COURT,
                 "paved": COURT, "sand": BARE, "dirt": BARE}
PITCH_SPORT = {"tennis": COURT, "basketball": COURT, "volleyball": COURT, "handball": COURT,
               "beachvolleyball": BARE}
# a square's / walk's / lot's surface -> kind
SURFACE_KIND = {"paving_stones": PAVERS, "sett": PAVERS, "cobblestone": PAVERS, "unhewn_cobblestone": PAVERS,
                "bricks": BRICK, "brick": BRICK, "concrete": CONCRETE, "concrete:plates": CONCRETE,
                "concrete:lanes": CONCRETE, "asphalt": PAVED, "paved": PAVED, "metal": CONCRETE, "wood": PATH,
                "gravel": GRAVEL, "fine_gravel": GRAVEL, "pebblestone": GRAVEL, "compacted": GRAVEL,
                "unpaved": PATH, "dirt": PATH, "ground": PATH, "earth": PATH, "mud": PATH, "sand": BARE,
                "grass": LAWN, "grass_paver": LAWN}
# road lines (the roads layer's class) drawn into the ground: (kind without a surface, half-width m)
LINE_ROADS = {"footway": (PATH_AUTO, 0.9), "path": (PATH, 0.6), "cycleway": (PATH_AUTO, 1.0),
              "bridleway": (PATH, 1.0), "steps": (PATH_AUTO, 1.0), "pedestrian": (PATH_AUTO, 2.0),
              "track": (GRAVEL, 1.5)}
SERVICE_HALF = {"driveway": 1.5, "parking_aisle": 3.0, "alley": 2.0}
UNPAVED = {"gravel", "fine_gravel", "compacted", "unpaved", "dirt", "ground", "earth", "pebblestone", "grass", "sand",
           "mud"}
RAIL_HALF_M = 1.8               # a track's ballast
# waterway lines: half-width m (a mapped width wins)
WATERWAY_HALF = {"stream": 1.5, "ditch": 0.8, "drain": 0.8, "canal": 3.0, "river": 4.0, "brook": 1.0}
# Overture infrastructure class -> kind, drawn over the water (a pier's deck is not water)
INFRA_KIND = {"pier": BUILT, "dock": BUILT, "breakwater": BARE, "groyne": BARE}
PIER_HALF_M = 1.5               # man_made=pier lines (floating piers, boardwalks over the water)
HEDGE_HALF_M = 1.2              # barrier=hedge
TREE_ROW_HALF_M = 3.0           # natural=tree_row
CANOPY_NODATA = 255
R_MERC = 6378137.0


def chain(names):
    """The imagery chain the generated ground keeps (SOURCES, in the chain's order); all of SOURCES when it has none of
    them (a test's or a hand-set chain)."""
    kept = tuple(n for n in names if n in SOURCES)
    return kept or SOURCES


def _grid(frame, x0, y0, size, px):
    """(lat, lon, mx, my) (px, px) of the texel centres of [x0, x0 + size]^2 of frame, row 0 = north (imagery.render's),
    with their Web-Mercator metres (the canopy rasters'): earthfast.gg_grid on the kernels' threads, the numbers
    frame.enu_to_geodetic and numpy / dnp gave (tests: test_genground_v3)."""
    t = size / px
    if hasattr(frame, "origin") and hasattr(frame, "rot"):
        return _grid_rect(frame, x0, y0 + size, t, px, px)
    k = (np.arange(px) + 0.5) * t   # (a stand-in frame: its own enu_to_geodetic)
    E, N = np.meshgrid(x0 + k, y0 + size - k)
    lat, lon, _ = frame.enu_to_geodetic(E, N, 0.0)
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    return lat, lon, R_MERC * np.radians(lon), R_MERC * dnp.log(dnp.tan(np.pi / 4 + np.radians(lat) / 2))


def _grid_rect(frame, x0, top, t, rows, cols):
    return fast._ef.gg_grid(np.asarray(frame.origin, np.float64), np.asarray(frame.rot, np.float64), float(x0),
                            float(top), float(t), int(rows), int(cols))


# The per-texel fields that are pure functions of the texel's place (its lat / lon, Web-Mercator metres, land-cover
# class read at its warped point, its land-cover kind): the last block's kept, and a block that overlaps it (the next
# block along a drive: 1,024 of its 1,152 columns) computes only the strips it adds (stage 3: they were ~60 % of
# inputs()'s CPU). Exact: at 1 m texels on the anchor's half-metre texel grid the centres are the same doubles
# whichever block names them (x0 + (j + 0.5) t: halves of integers, exact), so the fields are the same bits; kept only
# when their land cover was complete (a class read before its cell came is not kept), and reused only with the same
# frame, texel size, Surface, map-data versions and, for every cell both squares read, the very same land-cover Raster
# object (held by the cache, so its identity cannot be reused; Codex review of f25f5d5f..5cff9200).
FIELD_NAMES = ("lat", "lon", "mx", "my", "classes", "base")
_FIELDS_LOCK = threading.Lock()
_fields_last = []           # [(key, x0, top, rows, cols, {name: array})]


def _fields_rect(frame, x0, top, t, rows, cols, surface):
    lat, lon, mx, my = _grid_rect(frame, x0, top, t, rows, cols)
    complete = True
    if surface is not None:
        classes, complete = surface.classes(lat, lon)
    else:
        classes = np.zeros(lat.shape, np.uint8)
    base = base_kinds(classes, lat, lon)
    return {"lat": lat, "lon": lon, "mx": mx, "my": my, "classes": np.asarray(classes, np.uint8),
            "base": np.asarray(base, np.uint8)}, bool(complete)


def _versions_key(surface):
    """The map data's versions (mapdata.versions(): the land cover's release among them) as a string, or None."""
    md = getattr(surface, "md", None)
    try:
        return json.dumps(md.versions(), sort_keys=True, default=str) if md is not None else None
    except Exception:           # (no versions: no reuse rather than a wrong one)
        return object()


WARP_MARGIN_DEG = 0.0005        # (the land cover is read up to ~11 m off each texel: surface.WARP_OCTAVES)


def _land_cover_sources(frame, x0, top, size, surface):
    """{cell: its land-cover Raster object} of the cells the square's texels read (the square and WARP_MARGIN_DEG round
    it), held by identity: a cell re-read (another version, decoded again) is another object. None without a
    Surface's cache to ask (then nothing is reused)."""
    if surface is None:
        return {}
    cached = getattr(surface, "_cached", None)
    if cached is None:
        return None
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0 + size]), np.array([top - size, top]), 0.0)
    keys = mapcells.cells_for_bbox(float(np.min(la)) - WARP_MARGIN_DEG, float(np.min(lo)) - WARP_MARGIN_DEG,
                                   float(np.max(la)) + WARP_MARGIN_DEG, float(np.max(lo)) + WARP_MARGIN_DEG)
    return {str(k): cached(k) for k in keys}


def _same_sources(old, new):
    """Whether every cell both squares read has the very same land-cover Raster object (and neither is missing)."""
    if old is None or new is None:
        return False
    for k in old.keys() & new.keys():
        if old[k] is None or new[k] is None or old[k] is not new[k]:
            return False
    return True


def texel_fields(frame, x0, y0, size, px, surface):
    """({FIELD_NAMES: (px, px) arrays}, complete) of the square (see the cache's note above)."""
    t = size / px
    top = y0 + size
    if not (hasattr(frame, "origin") and hasattr(frame, "rot")):
        lat, lon, mx, my = _grid(frame, x0, y0, size, px)
        complete = True
        if surface is not None:
            classes, complete = surface.classes(lat, lon)
        else:
            classes = np.zeros(lat.shape, np.uint8)
        return {"lat": lat, "lon": lon, "mx": mx, "my": my, "classes": classes,
                "base": base_kinds(classes, lat, lon)}, bool(complete)
    exact = t == 1.0 and float(2 * x0).is_integer() and float(2 * top).is_integer()
    key = (np.asarray(frame.origin, np.float64).tobytes(), np.asarray(frame.rot, np.float64).tobytes(), t,
           id(surface), _versions_key(surface))
    sources = _land_cover_sources(frame, x0, top, size, surface) if exact else None
    prev = None
    if exact:
        with _FIELDS_LOCK:
            prev = _fields_last[0] if _fields_last else None
    out, complete = None, True
    if prev is not None and prev[0] == key and _same_sources(prev[6], sources):
        _k, ox0, otop, orows, ocols, old, _src = prev
        dc, dr = x0 - ox0, otop - top               # (new texel (i, j) is the old (i + dr, j + dc))
        if float(dc).is_integer() and float(dr).is_integer():
            dc, dr = int(dc), int(dr)
            r_lo, r_hi = max(0, -dr), min(px, orows - dr)
            c_lo, c_hi = max(0, -dc), min(px, ocols - dc)
            if r_lo < r_hi and c_lo < c_hi:
                out = {}
                for n in FIELD_NAMES:
                    a = np.empty((px, px), old[n].dtype)
                    a[r_lo:r_hi, c_lo:c_hi] = old[n][r_lo + dr:r_hi + dr, c_lo + dc:c_hi + dc]
                    out[n] = a
                strips = [(0, r_lo, 0, px), (r_hi, px, 0, px), (r_lo, r_hi, 0, c_lo), (r_lo, r_hi, c_hi, px)]
                for a0, a1, b0, b1 in strips:
                    if a1 <= a0 or b1 <= b0:
                        continue
                    got, ok = _fields_rect(frame, x0 + b0 * t, top - a0 * t, t, a1 - a0, b1 - b0, surface)
                    complete = complete and ok
                    for n in FIELD_NAMES:
                        out[n][a0:a1, b0:b1] = got[n]
    if out is None:
        out, complete = _fields_rect(frame, x0, top, t, px, px, surface)
    if exact:
        with _FIELDS_LOCK:
            _fields_last[:] = [(key, x0, top, px, px, out, sources)] if complete and sources is not None else []
    return out, complete


def _cells(lat, lon):
    return mapcells.cells_for_bbox(float(lat.min()), float(lon.min()), float(lat.max()), float(lon.max()))


# A missing layer is asked for again with a backoff (RETRY_FIRST_S, doubling, at most RETRY_MAX_S) and given up after
# RETRY_TRIES asks (the Codex review of 308: a layer whose fetch failed once was never asked again, and the block stayed
# stale for good; asked at every photo it would hammer the host): given up, the photo is made without it and is final
# for this process (still a revisit fault: not kept, a later start asks again).
RETRY_FIRST_S = 30.0
RETRY_MAX_S = 600.0
RETRY_TRIES = 6
_retries = {}               # (cell, layer) -> [asks so far, when the next may be]
_retry_lock = threading.Lock()


def _now():
    return time.monotonic()


def gave_up(key, layer):
    """Whether the layer of the cell was asked for RETRY_TRIES times without coming (and its last wait is over)."""
    with _retry_lock:
        r = _retries.get((key, layer))
        return r is not None and r[0] >= RETRY_TRIES and _now() >= r[1]


def _ask(key, layer, surface):
    """Queue a background fetch of the layer of the cell if its backoff allows (Surface._queue: the land cover's
    thread, which drops a repeat while one is queued). True when asked."""
    queue = getattr(surface, "_queue", None)
    now = _now()
    with _retry_lock:
        r = _retries.setdefault((key, layer), [0, 0.0])
        if r[0] >= RETRY_TRIES or now < r[1]:
            return False
        r[1] = now + min(RETRY_MAX_S, RETRY_FIRST_S * 2.0 ** r[0])
        r[0] += 1
    if queue is not None:
        try:
            queue(key, (layer,))
        except Exception as e:      # (asked again after the backoff)
            log.debug("genground: queue %s of %s: %s", layer, key, e)
    return True


def _absent(md, key, layer, missing, surface):
    """A layer not on disk: asked for (_ask), a revisit fault, and on missing unless given up (gave_up)."""
    revisit.fault()
    _ask(key, layer, surface)
    if not gave_up(key, layer):
        missing.append((key, layer))


def _layer(md, key, layer, missing, surface=None):
    """A cached layer of a cell, or None. Never fetches here: a missing one goes on `missing` (the photo is redone once
    it is on disk: GEN_PENDING, earthd.Server.base_ready), is a revisit fault (the photo is not kept) and is asked
    for in the background with a backoff (_absent)."""
    if md is None:
        return None
    if not md.has(key, layer):
        _absent(md, key, layer, missing, surface)
        return None
    try:
        return md.cell(key, layers=(layer,), kind="background").layers.get(layer)
    except Exception as e:          # an unreadable cache file: like a missing one
        log.debug("genground: %s of %s: %s: %s", layer, key, type(e).__name__, e)
        _absent(md, key, layer, missing, surface)
        return None


def _data_cells(frame, x0, y0, size):
    """(the cells under the square and its halo, those and the ring of cells round them: a clipped field's other
    pieces, _axis_rings)."""
    hx0, hy0, hsize, _hpx, _h = halo_square(x0, y0, size, 1024)
    la, lo, _ = frame.enu_to_geodetic(np.array([hx0, hx0 + hsize]), np.array([hy0, hy0 + hsize]), 0.0)
    keys = mapcells.cells_for_bbox(float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo)))
    ring = sorted({mapcells.CellKey(k.z, k.x + dx, k.y + dy) for k in keys for dx in (-1, 0, 1) for dy in (-1, 0, 1)},
                  key=str)
    return keys, ring


def data_ready(md, frame, x0, y0, size, surface=None, veg=None, dem=None):
    """Every layer the generated ground reads (LAYERS) on disk, or given up (gave_up), for the cells under the square
    and its halo, and the land use of the cells round them (never fetches; what is missing is asked for again when its
    backoff allows: base_ready polls this): a photo made without some of them (GEN_PENDING) is made again then."""
    if md is None:
        return True
    keys, ring = _data_cells(frame, x0, y0, size)
    ready = True
    for k, layer in [(k, layer) for k in keys for layer in LAYERS] + [(k, "land_use") for k in ring]:
        if md.has(k, layer) or gave_up(k, layer):
            continue
        _ask(k, layer, surface)
        ready = ready and gave_up(k, layer)
    if veg is not None:         # (the game's trees: their cells built by the vegetation, not here; given up likewise)
        for k in keys:
            if veg.ready(k) or gave_up(k, TREES_LAYER):
                continue
            _ask_trees(veg, k)
            ready = ready and gave_up(k, TREES_LAYER)
    return shade_ready(frame, x0, y0, size, keys, dem) and ready


# (stage 4) The forest floor under the game's own trees: vegetation.Vegetation's placed trees (the same cells the tree
# tiles draw, read from its cache, never built here), each a disc of its crown's radius and FLOOR_FADE_M of fade
# (earthfast.gg_discs). The trees the mod shows out to its 700 m ring at the Normal density (rank < FLOOR_RANK,
# trees.lua RINGS): nearer it shows more of them, standing on open ground at their edges. A cell not built yet: the
# canopy's core alone (>= 3 m, no skirt) and the photo is made again once it is (GEN_PENDING, data_ready).
TREES_LAYER = "trees"


def _ask_trees(veg, k):
    """A vegetation cell the generated ground waits for: built in the background (Vegetation.build_soon: the warm
    thread, after the player's own cells, a bounded queue) when its backoff allows, so a block outside the pose's
    warm-up goes final after one redo instead of redoing its photo at every retry until given up (the backstop)."""
    if not _ask(k, TREES_LAYER, None):
        return
    soon = getattr(veg, "build_soon", None)
    if soon is not None:
        try:
            soon(k)
        except Exception as e:      # (asked again after the backoff)
            log.debug("genground: trees of %s: build_soon: %s: %s", k, type(e).__name__, e)
FLOOR_RANK = 0.6
FLOOR_FADE_M = 2.0
FLOOR_REACH_M = 12.0            # (a crown's radius at most: the cells round the square whose trees reach into it)


def tree_floor(frame, x0, y0, size, px, veg, missing):
    """(floor (px, px) uint8 or None, mode): where the game's trees stand (mode 2), None and mode 1 while a cell's trees
    are not built (each such cell on missing, a revisit fault), None and mode 0 without trees (the canopy's own colour:
    nothing is drawn over it)."""
    if veg is None:
        return None, 0
    import vegetation as vegetationmod
    t = size / px
    top = y0 + size
    la, lo, _ = frame.enu_to_geodetic(np.array([x0 - FLOOR_REACH_M, x0 + size + FLOOR_REACH_M]),
                                      np.array([y0 - FLOOR_REACH_M, top + FLOOR_REACH_M]), 0.0)
    lat_lo, lat_hi, lon_lo, lon_hi = float(np.min(la)), float(np.max(la)), float(np.min(lo)), float(np.max(lo))
    keys = mapcells.cells_for_bbox(lat_lo, lon_lo, lat_hi, lon_hi)
    cells, waiting = [], False
    for k in keys:
        if not veg.ready(k):
            if not gave_up(k, TREES_LAYER):
                _ask_trees(veg, k)      # (every cell it waits for asked at once)
                missing.append((k, TREES_LAYER))
                waiting = True
            continue            # (given up: its trees left out)
        if waiting:
            continue
        try:
            cells.append(veg.cell(k))
        except Exception as e:  # (unreadable: as not built)
            log.debug("genground: trees of %s: %s: %s", k, type(e).__name__, e)
            missing.append((k, TREES_LAYER))
            waiting = True
    if waiting:
        revisit.fault()
        return None, 1
    floor = np.zeros((px, px), np.uint8)
    for it in cells:
        lat_, lon_ = np.asarray(it.lat), np.asarray(it.lon)
        kd = np.asarray(it.kind)
        sel = (((kd == vegetationmod.KIND_TREE) | (kd == vegetationmod.KIND_BUSH)) & (np.asarray(it.rank) < FLOOR_RANK)
               & (lat_ >= lat_lo) & (lat_ <= lat_hi) & (lon_ >= lon_lo) & (lon_ <= lon_hi))   # (the square's own)
        if not sel.any():
            continue
        e, n, _ = frame.geodetic_to_enu(np.asarray(it.lat, np.float64)[sel], np.asarray(it.lon, np.float64)[sel], 0.0)
        fast._ef.gg_discs(floor, np.ascontiguousarray((np.asarray(e, np.float64) - x0) / t),
                          np.ascontiguousarray((top - np.asarray(n, np.float64)) / t),
                          np.ascontiguousarray(np.asarray(it.radius, np.float64)[sel] / t), float(FLOOR_FADE_M / t))
    return floor, 2


def base_kinds(classes, lat, lon):
    """The land cover's kind per texel: WC_KIND of the WorldCover classes, built-up land a lawn where surface.lawns
    says (the ground's LAWN layer: the same patches)."""
    import surface as surfacemod
    base = _WC_LUT[np.asarray(classes, np.uint8)]
    urban = np.asarray(classes) == 50
    if urban.any():
        sub = base[urban]
        sub[surfacemod.lawns(lat[urban], lon[urban])] = LAWN
        base[urban] = sub
    return base


def _tags(f, i):
    """OSM's tags of feature i (Overture's source_tags JSON), {} when none."""
    t = f.attrs.get("tags")
    if t is None:
        return {}
    v = str(t[i])
    if not v or v in ("None", "null", "nan"):
        return {}
    try:
        d = json.loads(v)
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _attr(f, name, i):
    """Attribute name of feature i as a stripped lower-case string, or None."""
    a = f.attrs.get(name)
    if a is None:
        return None
    v = a[i]
    if v is None or (isinstance(v, (float, np.floating)) and not np.isfinite(v)):
        return None
    out = str(v).strip().lower()
    return None if out in ("", "none", "nan", "null") else out


def _width(v):
    """A mapped width in metres ("3", "3 m", "2.5"), or None."""
    try:
        w = float(str(v).lower().replace("m", "").strip())
    except (TypeError, ValueError):
        return None
    return w if 0.3 <= w <= 60.0 else None


def polygon_kind(layer, f, i):
    """(kind, order) a polygon draws (kind 255: none). Order: 0 land use (largest first), 1 surfaces (pitches, squares,
    playgrounds, unpaved lots: over the parks they lie in), 2 water and pools, 3 piers and docks."""
    cls = _attr(f, "class", i) or ""
    tags = _tags(f, i)
    surf = _attr(f, "surface", i) or str(tags.get("surface") or "").strip().lower()
    if layer == "water":
        return (POOL if cls == "swimming_pool" or tags.get("leisure") == "swimming_pool" else WATER), 2
    if layer == "infrastructure":
        if cls in INFRA_KIND:
            return INFRA_KIND[cls], 3
        if (cls == "parking" or tags.get("amenity") == "parking") and surf in UNPAVED:
            return GRAVEL, 1        # (a paved lot is painted asphalt: surface.paint_square's lots)
        return 255, 0
    if cls == "pitch":
        sport = str(tags.get("sport") or "").split(";")[0].strip()
        return PITCH_SURFACE.get(surf) or PITCH_SPORT.get(sport) or TURF, 1
    if cls in ("pedestrian", "plaza") or tags.get("place") == "square" or tags.get("highway") == "pedestrian":
        return SURFACE_KIND.get(surf, PAVERS), 1     # (no surface: pavers, as its material layer: plazas.paving)
    if cls in ("schoolyard", "playground"):
        if surf in SURFACE_KIND:
            return SURFACE_KIND[surf], 1
        return (BARE if cls == "playground" else 255), 1
    if cls == "parking" and surf in UNPAVED:
        return GRAVEL, 1
    return LAND_USE_KIND.get(cls, 255), 0


def _phase(fid):
    """m a polygon's rows are moved across, from its id (the same for each of its pieces in every cell and block)."""
    h = hashlib.blake2b(str(fid).encode(), digest_size=8).digest()
    return int.from_bytes(h, "little") / 2.0 ** 64 * 12.0


# kinds whose rows need a direction (gen_ground's patterns): the rest (forests, farmland, water) never ask for one
ROW_KINDS = frozenset((TURF, ARTTURF, CLAY, TARTAN, COURT, ORCHARD, VINEYARD, ALLOTMENT, GREENHOUSE, CEMETERY))
CLIP_EPS_DEG = 1e-9


def _clip_flags(lonlat, key):
    """Per point of a clipped piece's ring (lon, lat), whether its edge to the next point runs along a border of its map
    cell (the clip, not the polygon's own outline): both ends on the same border."""
    lon0, lat0, lon1, lat1 = key.bounds()
    p = np.asarray(lonlat, np.float64)
    q = np.roll(p, -1, axis=0)
    out = np.zeros(len(p), np.uint8)
    for col, b in ((0, lon0), (0, lon1), (1, lat0), (1, lat1)):
        out |= ((np.abs(p[:, col] - b) <= CLIP_EPS_DEG) & (np.abs(q[:, col] - b) <= CLIP_EPS_DEG)).astype(np.uint8)
    return out


def _axis_rings(frame, fid, f, i, key, more):
    """[(xy ring (k, 2) m, skip (k,) uint8)] the row direction of polygon fid is found from: its own rings, and where
    Overture clipped it to its map cell, every piece of it in the 3 x 3 cells round that one (more(cell): their
    land_use Features or None), the clip's edges left out. The same pieces whichever block asks (the Codex review of
    308: a field split across cells took another direction in each)."""
    pieces = [(f, i, key)]
    clipped = getattr(f, "clipped", None)
    if more is not None and key is not None and clipped is not None and bool(clipped[i]):
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                k2 = mapcells.CellKey(key.z, key.x + dx, key.y + dy)
                g = more(k2)
                if g is None or not len(g):
                    continue
                j = g.index(fid)
                if j >= 0:
                    pieces.append((g, j, k2))
    out = []
    for g, j, k in pieces:
        xy = g.xy_in(frame)
        is_clipped = getattr(g, "clipped", None) is not None and bool(g.clipped[j])
        for part_ll, part_xy in zip(g.parts(j), g.parts(j, xy)):
            for r_ll, r_xy in zip(part_ll, part_xy):
                if len(r_xy) < 2:
                    continue
                skip = _clip_flags(r_ll, k) if is_clipped and k is not None else np.zeros(len(r_xy), np.uint8)
                out.append((np.asarray(r_xy, np.float64)[:, :2], skip))
    return out


def polygons(frame, x0, y0, size, px, feats_list, more=None):
    """(poly (px, px) uint32: 0 or 1 + the index of the polygon over each texel, kinds (polygons,) uint8, dirs
    (polygons, 2) each one's rows' direction (east, north), phases (polygons,) m) of the land-use, water and
    infrastructure Features in feats_list ([(layer name, Features, its map cell or None)]), drawn in polygon_kind's
    order (land use by area, largest first: a park inside a bigger farm area wins). more(cell): the land_use Features
    of another cell, for a clipped polygon's other pieces (_axis_rings)."""
    fast.require(NEED, "generated ground")
    t = size / px
    top = y0 + size
    items = []          # (order, -area, id, part, kind, [rings as texel (col, row)], (Features, row, cell))
    seen = set()
    for entry in feats_list:
        layer, f = entry[0], entry[1]
        key = entry[2] if len(entry) > 2 else None
        if f is None or not len(f):
            continue
        poly_rows = np.flatnonzero(np.asarray(f.kind) == 3)
        if not len(poly_rows):
            continue
        xy = None
        for i in poly_rows:
            kind, order = polygon_kind(layer, f, i)
            if kind == 255:
                continue
            if xy is None:
                xy = f.xy_in(frame)
            fid = str(f.ids[i])
            for pi, part in enumerate(f.parts(i, xy)):
                if not part or (fid, pi, layer) in seen:
                    continue
                seen.add((fid, pi, layer))
                outer = np.asarray(part[0], np.float64)
                if outer.shape[0] < 3 or outer[:, 0].max() < x0 or outer[:, 0].min() > x0 + size \
                        or outer[:, 1].max() < y0 or outer[:, 1].min() > top:
                    continue
                x, y = outer[:, 0], outer[:, 1]
                area = abs(float(np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]) + x[-1] * y[0] - x[0] * y[-1])) / 2.0
                rings = [np.stack([(np.asarray(r, np.float64)[:, 0] - x0) / t,
                                   (top - np.asarray(r, np.float64)[:, 1]) / t], 1) for r in part if len(r) >= 3]
                items.append((order, -area, fid, pi, int(kind), rings, (layer, f, i, key)))
    poly = np.zeros((px, px), np.uint32)
    if not items:
        return poly, np.zeros(0, np.uint8), np.zeros((0, 2)), np.zeros(0)
    items.sort(key=lambda it: it[:4])
    pts, ring_off, poly_off, kinds, phases = [], [0], [0], [], []
    apts, askip, aring, apoly = [], [], [0], [0]
    axes_memo = {}
    for _o, _a, fid, _pi, k, rings, (layer, f, i, key) in items:
        for r in rings:
            pts.append(r)
            ring_off.append(ring_off[-1] + len(r))
        poly_off.append(len(ring_off) - 1)
        kinds.append(k)
        phases.append(_phase(fid))
        if k in ROW_KINDS:      # (only the patterns' polygons: a 50,000-vertex forest never needs a direction)
            got = axes_memo.get((layer, fid))
            if got is None:
                got = axes_memo[(layer, fid)] = _axis_rings(frame, fid, f, i, key,
                                                            more if layer == "land_use" else None)
            for r, sk in got:
                apts.append(r)
                askip.append(sk)
                aring.append(aring[-1] + len(r))
        apoly.append(len(aring) - 1)
    pts = np.ascontiguousarray(np.concatenate(pts), np.float64)
    ring_off = np.asarray(ring_off, np.int64)
    poly_off = np.asarray(poly_off, np.int64)
    fast._ef.gg_fill_polys(poly, pts, ring_off, poly_off, np.arange(1, len(kinds) + 1, dtype=np.uint32))
    dirs = np.asarray(fast._ef.gg_poly_axes(
        np.ascontiguousarray(np.concatenate(apts) if apts else np.zeros((0, 2)), np.float64),
        np.asarray(aring, np.int64), np.asarray(apoly, np.int64),
        np.ascontiguousarray(np.concatenate(askip) if askip else np.zeros(0, np.uint8))), np.float64)
    return poly, np.asarray(kinds, np.uint8), np.ascontiguousarray(dirs), np.asarray(phases, np.float64)


def _memo(f, name, make):
    """make() kept on the Features object f (a decoded map-data layer: immutable, shared by the blocks that read its
    cell): the tags' decisions are made once per layer, not again for every block (stage 3)."""
    d = getattr(f, "__dict__", None)
    if d is None:
        return make()
    got = d.get(name)
    if got is None:
        got = d[name] = make()
    return got


def line_items(roads_list, water_list, infra_list):
    """[(order, id, kind, half m, Features, row)] of the lines the ground draws: railway ballast (0), paths, tracks and
    unpaved drives (1), hedges and tree rows (2), streams and ditches (3), piers (4), later orders over earlier. Not
    underground or on a bridge (a road's layer other than 0, a waterway's tunnel or culvert). Each layer's decisions are
    made once (_memo)."""
    out = []
    for layer, fs in (("roads", roads_list), ("infrastructure", infra_list), ("water", water_list)):
        for f in fs:
            if f is None or not len(f):
                continue
            out.extend((o, fid, k, h, f, i)
                       for o, fid, k, h, i in _memo(f, "_gg_lines_v1", lambda f=f, layer=layer: _line_rows(layer, f)))
    return out


def _line_rows(layer, f):
    """line_items' [(order, id, kind, half m, row)] of one layer's Features."""
    out = []
    if layer == "roads":
        lines_ = np.flatnonzero(np.asarray(f.kind) == 2)
        cls_all = f.attrs.get("class")
        sub_all = f.attrs.get("subtype")
        if cls_all is not None and len(lines_):      # (the candidates by class at once: thousands of road segments)
            c = np.char.strip(np.char.lower(np.asarray(cls_all, str)[lines_]))
            s = (np.char.strip(np.char.lower(np.asarray(sub_all, str)[lines_])) if sub_all is not None
                 else np.full(len(lines_), ""))
            lines_ = lines_[(s == "rail") | np.isin(c, list(LINE_ROADS)) | (c == "service")]
        for i in lines_:
            cls = _attr(f, "class", i) or ""
            sub = _attr(f, "subtype", i) or ""
            try:
                level = float(_attr(f, "osm_layer", i) or 0.0)
            except ValueError:
                level = 0.0
            if level != 0:          # (underground, or a bridge: the ground under it is not the line)
                continue
            surf = _attr(f, "osm_surface", i)
            width = _width(_attr(f, "osm_width", i))
            if sub == "rail":
                rw = _attr(f, "osm_railway", i) or ""
                if cls in ("subway", "tram", "funicular") or rw in ("subway", "tram", "abandoned", "razed",
                                                                   "proposed", "construction"):
                    continue
                out.append((0, str(f.ids[i]), GRAVEL, RAIL_HALF_M, i))
            elif cls in LINE_ROADS:
                kind, half = LINE_ROADS[cls]
                if surf in SURFACE_KIND:
                    kind = SURFACE_KIND[surf]
                out.append((1, str(f.ids[i]), kind, width / 2 if width else half, i))
            elif cls == "service" and surf in UNPAVED:
                half = width / 2 if width else SERVICE_HALF.get(_attr(f, "osm_service", i) or "", 2.0)
                out.append((1, str(f.ids[i]), GRAVEL if surf not in ("grass", "dirt", "ground", "earth", "mud")
                            else PATH, half, i))
    elif layer == "infrastructure":
        for i in np.flatnonzero(np.asarray(f.kind) == 2):
            tags = _tags(f, i)
            cls = _attr(f, "class", i) or ""
            if cls == "hedge" or tags.get("barrier") == "hedge":
                out.append((2, str(f.ids[i]), TREE, HEDGE_HALF_M, i))
            elif tags.get("natural") == "tree_row":
                out.append((2, str(f.ids[i]), TREE, TREE_ROW_HALF_M, i))
            elif cls == "pier" or tags.get("man_made") == "pier":
                # (a pier's deck over the water: built ground, as INFRA_KIND's pier polygons; drawn after the water)
                width = _width(tags.get("width"))
                out.append((4, str(f.ids[i]), BUILT, width / 2 if width else PIER_HALF_M, i))
    else:
        for i in np.flatnonzero(np.asarray(f.kind) == 2):
            tags = _tags(f, i)
            if tags.get("tunnel") or str(tags.get("location") or "") == "underground":
                continue
            cls = _attr(f, "class", i) or str(tags.get("waterway") or "")
            way = str(tags.get("waterway") or cls)
            half = WATERWAY_HALF.get(way, WATERWAY_HALF.get(cls))
            if half is None:
                continue
            width = _width(tags.get("width"))
            out.append((3, str(f.ids[i]), WATER, width / 2 if width else half, i))
    return out


def lines(frame, x0, y0, size, px, roads_list, water_list, infra_list):
    """(line (px, px) uint32: 0 or 1 + the index of the line over each texel, kinds (lines,) uint8) of line_items,
    buffered to their widths (earthfast.gg_fill_lines), in their order."""
    fast.require(NEED, "generated ground")
    t = size / px
    top = y0 + size
    pts, offs, halves, kinds, keys, seen = [], [0], [], [], [], set()
    items = sorted(line_items(roads_list, water_list, infra_list), key=lambda it: it[:2])
    xys = {}
    for order, fid, kind, half, f, i in items:
        if (order, fid) in seen:
            continue
        seen.add((order, fid))
        xy = xys.get(id(f))
        if xy is None:
            xy = xys[id(f)] = f.xy_in(frame)
        for part in f.parts(i, xy):
            for ring in part:
                r = np.asarray(ring, np.float64)
                if len(r) < 2 or r[:, 0].max() < x0 - half or r[:, 0].min() > x0 + size + half \
                        or r[:, 1].max() < y0 - half or r[:, 1].min() > top + half:
                    continue
                pts.append(np.stack([(r[:, 0] - x0) / t, (top - r[:, 1]) / t], 1))
                offs.append(offs[-1] + len(r))
                halves.append(half / t)
                kinds.append(kind)
    line = np.zeros((px, px), np.uint32)
    if not kinds:
        return line, np.zeros(0, np.uint8)
    fast._ef.gg_fill_lines(line, np.ascontiguousarray(np.concatenate(pts), np.float64), np.asarray(offs, np.int64),
                           np.asarray(halves, np.float64), np.arange(1, len(kinds) + 1, dtype=np.uint32))
    return line, np.asarray(kinds, np.uint8)


def canopy_grid(mx, my, rasters):
    """The canopy height (m, uint8; CANOPY_NODATA where none) at the texels (Web-Mercator metres mx, my: _grid's)
    from the cells' canopy Rasters (EPSG:3857, nearest pixel)."""
    out = np.full(mx.shape, CANOPY_NODATA, np.uint8)
    if not rasters:
        return out
    for r in rasters:
        if r is None:
            continue
        h, w = r.data.shape
        inside = (mx >= r.x0) & (mx < r.x0 + w * r.dx) & (my <= r.y0) & (my > r.y0 - h * r.dy)
        if inside.any():
            out[inside] = r.sample(mx[inside], my[inside])
    return out


# (stage 5) The capture sun's shadows out of the Sentinel-2 colour (the user, 2026-11-16: "As well as any shadows that
# appear from mountains and buildings"; "better methods of shadow removal for harsh shadows"; the Codex report
# .claude/work/codex-harsh-shadows-2026-11-16.md, its rank 1 and a limited rank 3): the game lights the ground again,
# so the photo's slope shading, mountain shadows and building shadows doubled up. earthfast ggshade.rs:
# - the sun per data cell (z14, ~2 km; its texels relit with it wherever a block lies: every block alike): Sentinel-2
#   flies at about 10:30 local solar time, the composite mixes dates, so the candidates are S2_TIMES x every DOY_STEP
#   days, scored on the cell's photo (gg_sun_fit): Pearson's r of its luminance with the DEM's lam x lit (gate R2_MIN,
#   relief at least RELIEF_MIN_M) and, independently (a flat city is not skipped by the terrain's gate), the Matthews
#   correlation of the buildings' and canopy's predicted shadows with the open ground's dark texels (gate MCC_MIN,
#   SHADOW_MIN texels); the shadow / lit ratio k measured across the predicted shadows' edges;
# - per block (gg_relight): the matte (geometry where a shadow may be, the image how dark), the relight, the lost
#   texels filled from the kind's lit texels (gen_ground's gap table). Roofs, crowns and water are not relit; roads are.
SHADE_VERSION = 1
SHADE_DEM_Z = 12                # (Mapterhorn is complete on land to here: the far field's tiles)
SHADE_NODE = 16                 # texels between the block's terrain nodes (16 m at its 1 m texels)
SHADE_PAD_M = 1536.0            # the terrain's march: a 1000 m ridge's shadow down to a 33 degree sun
SHADE_REACH_M = 88.0            # buildings' and canopy's shadows at most this long (the sweep's heights capped)
FIT_RES_B = 4.0                 # the fit's buildings / canopy grid (m)
FIT_RES_T = 32.0                # the fit's terrain grid (m)
FIT_COARSE = 8                  # FIT_RES_T / FIT_RES_B
R2_MIN = 0.25                   # the terrain's slope shading taken out from here (its Lambert term unconfirmed per texel)
R_CAST_MIN = 0.3                # ... its cast shadows from here (each confirmed by the image: the matte)
RELIEF_MIN_M = 30.0
MCC_MIN = 0.2
SHADOW_MIN = 2000
OCC_MIN = 500                   # 4 m texels of buildings or canopy (>= 2 m) for the buildings' fit
K_DEFAULT = (0.40, 0.42, 0.55)  # shadow / lit (r, g, b) without enough of an edge to measure
K_MIN_COUNT = 200
S2_TIMES = (10.0, 10.5, 11.0)
DOY_STEP = 12
EL_MIN_DEG = 15.0
SHADE_LAYER = "shade"
LOST = 0x20000000               # (gen_ground's mark on a region's texel it fills from the kind's lit texels round it)
FIT_PARTIAL_S = 60.0            # a fit made while its photo's tiles were still coming: made again after this
# the kinds relit (not NONE, TREE, WATER, ROOF, POOL, GREENHOUSE: crowns, water and roofs are not the ground's colour)
RELIT = np.ones(32, np.uint8)
RELIT[[NONE, TREE, WATER, ROOF, POOL, GREENHOUSE]] = 0
_fits = {}                      # (cell key, SHADE_VERSION, imagery id, dem on, versions) -> (time, partial, row, info)
_fits_lock = threading.Lock()
FITS_MAX = 64


def _s2_season(lat, doy):
    """Whether day doy can be a cloudless composite's capture day at lat: its local summer half (the composites pick
    snow- and cloud-free scenes: a free date fitted winter suns to noise, Lauterbrunnen's cells 2026-11-16); every
    day in the tropics."""
    if abs(lat) < 23.5:
        return True
    return 100 <= doy <= 260 if lat > 0 else (doy <= 75 or doy >= 285)


def sun_candidates(lat):
    """(cands (k, 5) float32: se, sn, tan, sin, cos of each candidate sun at lat, [(hour, day of year)]): S2_TIMES local
    solar time x every DOY_STEP days, above EL_MIN_DEG. dmath (no platform libm), rounded to 1e-6."""
    import dmath
    phi = dmath.radians(float(lat))
    sp, cp = dmath.sin(phi), dmath.cos(phi)
    rows, meta = [], []
    for hour in S2_TIMES:
        hr = dmath.radians(15.0 * (hour - 12.0))
        sh, ch = dmath.sin(hr), dmath.cos(hr)
        for doy in range(1, 366, DOY_STEP):
            if not _s2_season(lat, doy):
                continue
            dec = dmath.radians(23.44 * dmath.sin(dmath.radians(360.0 / 365.0 * (doy - 81))))
            sd, cd = dmath.sin(dec), dmath.cos(dec)
            up = sp * sd + cp * cd * ch
            if up <= dmath.sin(dmath.radians(EL_MIN_DEG)):
                continue
            east, north = -cd * sh, cp * sd - sp * cd * ch
            hl = dmath.hypot(east, north)
            if hl <= 1e-9:
                continue
            cos_el = dmath.sqrt(max(0.0, 1.0 - up * up))
            rows.append([round(east / hl, 6), round(north / hl, 6), round(up / cos_el, 6), round(up, 6),
                         round(cos_el, 6)])
            meta.append((hour, doy))
    return np.asarray(rows, np.float32).reshape(-1, 5), meta


def building_heights(frame, x0, y0, size, px, md, missing):
    """(px, px) float32: the buildings' heights over the ground (m) at the texels of the square (their outer rings, the
    tallest over the others; 8 m where none is mapped, as buildings.photo_mask's), None without buildings."""
    import buildings as buildingsmod
    if md is None:
        return None
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0 + size, x0, x0 + size]),
                                      np.array([y0, y0, y0 + size, y0 + size]), 0.0)
    keys = mapcells.cells_for_bbox(float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo)))
    t = size / px
    top = y0 + size
    items, seen = [], set()
    for k in keys:
        f = _layer(md, k, "buildings", missing)
        if f is None or not len(f.ids):
            continue
        hs = f.attrs.get("height")
        ids = f.ids.tolist()
        rows, a, b = buildingsmod._photo_candidates(f, frame, x0, y0, size)
        if len(rows):
            xy = f.xy_in(frame)
            for j, i in enumerate(np.asarray(rows).tolist()):
                if ids[i] in seen or b[j] - a[j] < 3:
                    continue
                h = 8.0 if hs is None else float(hs[i])
                if not (h > 0.0 and h < 1000.0):
                    h = 8.0
                ring = np.asarray(xy[a[j]:b[j]], np.float64)
                items.append((int(round(h * 10.0)), str(ids[i]), np.stack([(ring[:, 0] - x0) / t,
                                                                           (top - ring[:, 1]) / t], 1)))
        seen.update(ids)
    if not items:
        return None
    items.sort(key=lambda it: it[:2])           # (the tallest over the others; equal ones by id)
    pts = np.ascontiguousarray(np.concatenate([it[2] for it in items]), np.float64)
    ring_off = np.r_[0, np.cumsum([len(it[2]) for it in items])].astype(np.int64)
    poly_off = np.arange(len(items) + 1, dtype=np.int64)
    out = np.zeros((px, px), np.uint32)
    fast._ef.gg_fill_polys(out, pts, ring_off, poly_off, np.asarray([it[0] for it in items], np.uint32))
    return out.astype(np.float32) / np.float32(10.0)


def _occluders(frame, x0, y0, size, px, md, missing, inner=None):
    """The buildings' and canopy's heights over the ground (m, float32 (px, px)): the canopy model where it has one.
    inner (m, canopy): the canopy already read for the square m texels inside this one (inputs()'s): only the ring
    round it is read here (the grid and the rasters' sampling were most of the cost)."""
    occ = building_heights(frame, x0, y0, size, px, md, missing)
    occ = np.zeros((px, px), np.float32) if occ is None else occ
    t = size / px
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0 + size, x0, x0 + size]),
                                      np.array([y0, y0, y0 + size, y0 + size]), 0.0)
    keys = mapcells.cells_for_bbox(float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo)))
    rasters = [_layer(md, k, "canopy", missing) for k in keys]
    canopy = np.full((px, px), CANOPY_NODATA, np.uint8)
    top = y0 + size
    if inner is None:
        rects = [(0, 0, px, px)]
    else:
        m, cin = inner
        canopy[m:px - m, m:px - m] = cin
        rects = [(0, 0, m, px), (px - m, 0, m, px), (m, 0, px - 2 * m, m), (m, px - m, px - 2 * m, m)]
    for r0, c0, nr, nc in rects:            # (row, col, rows, cols)
        if nr <= 0 or nc <= 0:
            continue
        if hasattr(frame, "origin") and hasattr(frame, "rot"):
            _la, _lo, mx, my = _grid_rect(frame, x0 + c0 * t, top - r0 * t, t, nr, nc)
        else:
            q = max(nr, nc)         # (a stand-in frame: a square's grid, cut)
            sub = _grid(frame, x0 + c0 * t, top - r0 * t - q * t, q * t, q)
            mx, my = sub[2][:nr, :nc], sub[3][:nr, :nc]
        canopy[r0:r0 + nr, c0:c0 + nc] = canopy_grid(mx, my, rasters)
    ch = np.where(canopy != CANOPY_NODATA, canopy, 0).astype(np.float32)
    return np.ascontiguousarray(np.maximum(occ, ch))


def shade_margin(t):
    """Texels of occluders round the square for gg_relight: the sweep's reach and sideways spread, the zone's 6 m."""
    return int(np.ceil((1.45 * SHADE_REACH_M + 8.0) / t))


def _usable_dem(dem):
    """dem when it is a dem.Dem (has, sample, prefetch), else None (a stand-in without them: no terrain term)."""
    return dem if all(hasattr(dem, a) for a in ("has", "sample", "prefetch")) else None


def _dem_keys(lat, lon):
    """The SHADE_DEM_Z tiles (z, x, y) under the points."""
    n = 1 << SHADE_DEM_Z
    x = np.floor((np.asarray(lon, np.float64) + 180.0) / 360.0 * n).astype(np.int64)
    my = R_MERC * dnp.log(dnp.tan(np.pi / 4 + np.radians(np.asarray(lat, np.float64)) / 2))
    y = np.floor((np.pi * R_MERC - my) / (2 * np.pi * R_MERC) * n).astype(np.int64)
    x, y = np.clip(x, 0, n - 1), np.clip(y, 0, n - 1)
    return sorted({(SHADE_DEM_Z, int(a), int(b)) for a in range(int(x.min()), int(x.max()) + 1)
                   for b in range(int(y.min()), int(y.max()) + 1)})


_dem_asked = set()
_dem_lock = threading.Lock()


def _dem_ready(dem, keys):
    """Whether every tile is at hand (dem.has); the missing ones fetched in a background thread (once at a time)."""
    want = [k for k in keys if not dem.has(*k)]
    if not want:
        return True
    with _dem_lock:
        want = [k for k in want if k not in _dem_asked]
        _dem_asked.update(want)
    if want:
        def fetch():
            try:
                dem.prefetch(want)
            except Exception as e:      # (asked again after the backoff: _dem_asked cleared below)
                log.debug("genground: dem prefetch: %s", e)
            finally:
                with _dem_lock:
                    _dem_asked.difference_update(want)
        threading.Thread(target=fetch, name="genground-dem", daemon=True).start()
    return False


def _dem_nodes(frame, x0, top, t, r0, c0, step, rows, cols, dem):
    """The ground's heights (float32 (rows, cols)) at the texels (r0 + i step, c0 + j step) of the grid from (x0, top)
    at t m, or None when a tile is not at hand."""
    jj = c0 + step * np.arange(cols)
    ii = r0 + step * np.arange(rows)
    E, N = np.meshgrid(x0 + (jj + 0.5) * t, top - (ii + 0.5) * t)
    la, lo, _ = frame.enu_to_geodetic(E, N, 0.0)
    if not _dem_ready(dem, _dem_keys(la, lo)):
        return None
    return np.ascontiguousarray(np.asarray(dem.sample(la, lo, SHADE_DEM_Z), np.float32).reshape(rows, cols))


def _render_square(imagery, frame, x0, y0, size, px, pending):
    if hasattr(imagery, "_render") and hasattr(imagery, "square"):
        rgb, _used = imagery._render(imagery.square(frame, x0, y0, size, px), px, pending)
    else:
        rgb, _used = imagery.render(frame, x0, y0, size, px, pending)
    return np.ascontiguousarray(np.asarray(rgb, np.uint8)[:, :, :3])


def _imagery_key(imagery):
    """An imagery's identity for the memos (sun fits, far squares): everything that changes its pixels: its chain's
    sources, tone, leaf-on, season, whether it reads the finished tiles alone and their settings (a raw driving render
    and a finished one never share an entry: the Codex review of a3c7895f), a quick render (this thread's or the
    worker's). A stand-in without a chain: by its object."""
    chain = getattr(imagery, "chain", None)
    if chain is None:
        return ("stand-in", id(imagery))
    caller = getattr(imagery, "_caller", None)
    quick = bool(getattr(imagery, "quick_all", False) or getattr(caller, "quick", False))
    sigs = tuple(sorted((str(k), str(v)) for k, v in (getattr(imagery, "_fin_sigs", None) or {}).items()))
    return (tuple(str(getattr(s, "name", s)) for s in chain), str(getattr(imagery, "tone", None)),
            str(getattr(imagery, "leaf_on", None)), str(getattr(imagery, "season_mode", None)),
            bool(getattr(imagery, "finished_only", False)), sigs, quick)


def _fit_key(key, imagery, md, dem):
    """A sun fit's memo key: the cell, SHADE_VERSION, the imagery by its settings (its chain's sources, tone, leaf-on,
    season: not id(), which a new object can reuse once the old one is gone: the mini PC's gate, 2026-11-16), the map
    data (which, and its versions) and whether the ground's heights were there."""
    try:
        ver = json.dumps(md.versions(buildings=True), sort_keys=True, default=str) if md is not None else None
    except Exception:
        ver = None
    im = _imagery_key(imagery)
    mdk = None if md is None else (type(md).__name__, str(getattr(md, "cache_dir", "")))
    return (str(key), SHADE_VERSION, im, mdk, dem is not None, ver)


def cell_sun(key, imagery, md, dem, pending=None, missing=None):
    """(row (10,) float32 for gg_relight's suns, or None: no shadow to take out; info dict) of data cell key: its
    capture sun fitted on its own photo, map data and ground (sun_candidates, gg_sun_fit), memoized per process.
    A fit whose photo or data were not all at hand is made again after FIT_PARTIAL_S (and the block is pending)."""
    dem = _usable_dem(dem)
    fk = _fit_key(key, imagery, md, dem)
    now = _now()
    with _fits_lock:
        memo = _fits.get(fk)
    # (made again: after FIT_PARTIAL_S when its photo or ground was not all there; at once when the map data it
    # missed is here now, data_ready's own wait)
    if memo is not None and not (memo[1] and now - memo[0] >= FIT_PARTIAL_S) \
            and not (memo[4] and md is not None and all(md.has(k2, layer) for k2, layer in memo[4])):
        if missing is not None and _fit_pending(key, memo[1], memo[4]):
            missing.append((key, SHADE_LAYER))
        return memo[2], memo[3]
    import geodesy
    lon0, lat0, lon1, lat1 = key.bounds()
    latc, lonc = (lat0 + lat1) / 2, (lon0 + lon1) / 2
    fr = geodesy.EnuFrame(latc, lonc)
    e, n, _u = fr.geodetic_to_enu(np.array([lat0, lat0, lat1, lat1]), np.array([lon0, lon1, lon0, lon1]), 0.0)
    half = float(max(np.max(np.abs(e)), np.max(np.abs(n))))
    nt = int(np.ceil(2 * half / FIT_RES_T))
    size = nt * FIT_RES_T
    x0 = y0 = -size / 2
    nb = nt * FIT_COARSE
    sub = []
    pend = set()        # (its own: a tile the block already waits for is the fit's too, Codex review of a8da6b7b)
    try:
        rgb_b = _render_square(imagery, fr, x0, y0, size, nb, pend)
    except Exception as e:      # (a worker's strict imagery without the cell's tiles on disk: no fit yet, asked again)
        log.debug("genground: shade fit %s: photo: %s: %s", key, type(e).__name__, e)
        rgb_b = None
    partial = bool(pend) or rgb_b is None
    if pending is not None:
        pending.update(pend)
    if rgb_b is None:
        info = {"cell": str(key), "partial": True, "photo": False}
        with _fits_lock:
            if len(_fits) >= FITS_MAX:
                _fits.pop(next(iter(_fits)))
            _fits[fk] = (now, True, None, info, [])
        _ask(key, SHADE_LAYER, None)
        revisit.fault()
        if missing is not None and _fit_pending(key, True, []):
            missing.append((key, SHADE_LAYER))
        return None, info
    occ = _occluders(fr, x0, y0, size, nb, md, sub)
    sub = [(k2, layer) for k2, layer in sub if k2 == key]     # (its neighbours' layers: only the square's fringe)
    rgb_t = (rgb_b.reshape(nt, FIT_COARSE, nt, FIT_COARSE, 3).astype(np.uint32).sum((1, 3))
             // (FIT_COARSE * FIT_COARSE)).astype(np.uint8)
    pad = int(SHADE_PAD_M / FIT_RES_T)
    dn = None
    if dem is not None:
        dn = _dem_nodes(fr, x0, y0 + size, FIT_RES_T, -pad, -pad, 1, nt + 2 * pad, nt + 2 * pad, dem)
        if dn is None:
            partial = True
        else:
            core = dn[pad:pad + nt, pad:pad + nt]
            if float(core.max() - core.min()) < RELIEF_MIN_M:
                dn = None
    if int((occ >= 2.0).sum()) < OCC_MIN:
        occ = None
    cands, meta = sun_candidates(latc)
    info = {"cell": str(key), "partial": partial, "data_missing": len(sub)}
    row = None
    if len(cands) and (dn is not None or occ is not None):
        scores, _ = fast._ef.gg_sun_fit(dn, FIT_RES_T, pad, rgb_t if dn is not None else None, occ, FIT_RES_B,
                                        rgb_b if occ is not None else None, cands, SHADE_REACH_M, pad, False)
        r_t = np.nan_to_num(scores[:, 0]) if dn is not None else np.zeros(len(cands))
        ok_b = (scores[:, 2] >= SHADOW_MIN) if occ is not None else np.zeros(len(cands), bool)
        mcc = np.where(ok_b, np.nan_to_num(scores[:, 1]), 0.0)
        rp = np.maximum(r_t, 0.0)
        total = rp * rp + 0.5 * np.maximum(mcc, 0.0)
        best = int(np.argmax(total))            # (the first of equal scores)
        t_ok = bool(dn is not None and r_t[best] >= R_CAST_MIN)
        t_level = 2.0 if t_ok and r_t[best] * r_t[best] >= R2_MIN else 1.0 if t_ok else 0.0
        b_ok = bool(occ is not None and mcc[best] >= MCC_MIN)
        info.update(hour=meta[best][0], doy=meta[best][1], r_t=round(float(r_t[best]), 4),
                    mcc=round(float(mcc[best]), 4), terrain=t_ok, buildings=b_ok)
        if t_ok or b_ok:
            _, sums = fast._ef.gg_sun_fit(dn, FIT_RES_T, pad, rgb_t if dn is not None else None, occ, FIT_RES_B,
                                          rgb_b if occ is not None else None, cands[best:best + 1], SHADE_REACH_M,
                                          pad, True)
            k = K_DEFAULT
            for use, s in ((b_ok, sums[0]), (t_ok, sums[1])):
                if use and s[3] >= K_MIN_COUNT and s[7] >= K_MIN_COUNT and min(s[4:7]) > 0:
                    k = tuple(min(0.85, max(0.12, round((int(s[c]) * int(s[7])) / (int(s[4 + c]) * int(s[3]))
                                                        * 1024) / 1024)) for c in range(3))
                    break
            info["k"] = k
            row = np.asarray(list(cands[best]) + [t_level, float(b_ok)] + list(k), np.float32)
    with _fits_lock:
        if len(_fits) >= FITS_MAX:
            _fits.pop(next(iter(_fits)))
        _fits[fk] = (now, partial, row, info, sorted({(k2, layer) for k2, layer in sub}, key=str))
    if partial:
        _ask(key, SHADE_LAYER, None)
    if partial or sub:
        revisit.fault()
        if missing is not None and _fit_pending(key, partial, sub):
            missing.append((key, SHADE_LAYER))
    log.debug("genground: shade fit %s", info)
    return row, info


def shade(rgb, frame, x0, y0, size, px, kind, region, canopy, md, dem, imagery, pending, missing, gr, gc,
          surface=None):
    """The square's photo (rgb, (px, px, 3) uint8: a relit copy) with the capture sun's shadows taken out, and the
    region with LOST on the texels it could not recover (gg_relight). The sun of each texel's data cell (cell_sun)."""
    fields, _complete = texel_fields(frame, x0, y0, size, px, surface)
    mx, my = fields["mx"], fields["my"]
    nz = 1 << mapcells.ZOOM
    span = 2 * np.pi * R_MERC
    tx = np.clip(np.floor((mx + np.pi * R_MERC) / span * nz), 0, nz - 1).astype(np.int64)
    ty = np.clip(np.floor((np.pi * R_MERC - my) / span * nz), 0, nz - 1).astype(np.int64)
    code = tx * nz + ty
    cells = np.unique(code)
    cellmap = np.full((px, px), 255, np.uint8)
    rows = []
    dem = _usable_dem(dem)
    for cd in cells.tolist():
        key = mapcells.CellKey(mapcells.ZOOM, int(cd // nz), int(cd % nz))
        row, _info = cell_sun(key, imagery, md, dem, pending, missing)
        if row is None or len(rows) >= 254:
            continue
        cellmap[code == cd] = len(rows)
        rows.append(row)
    if not rows:
        return rgb, region
    suns = np.ascontiguousarray(np.stack(rows), np.float32)
    t = size / px
    dn, r0, c0, step, steps = None, 0, 0, SHADE_NODE, int(SHADE_PAD_M / (SHADE_NODE * t))
    if dem is not None and bool((suns[:, 5] > 0.5).any()):
        padn = int(round(SHADE_PAD_M / t))
        r0 = -padn + ((-(gr - padn)) % step)
        c0 = -padn + ((-(gc - padn)) % step)
        nr = (px + padn - 1 - r0) // step + 1
        nc = (px + padn - 1 - c0) // step + 1
        dn = _dem_nodes(frame, x0, y0 + size, t, r0, c0, step, nr, nc, dem)
        if dn is None:
            missing.append(((SHADE_DEM_Z,), SHADE_LAYER))
            revisit.fault()
    # (the occluders a margin further out: a texel's shadow from the heights within ~1.42 x reach, its matte from the
    # lit texels within ~104 m of it and those texels' own shadows: every block computes each texel alike)
    m = shade_margin(t)
    occ = _occluders(frame, x0 - m * t, y0 - m * t, size + 2 * m * t, px + 2 * m, md, missing, (m, canopy))
    out = np.array(rgb, np.uint8, copy=True)
    lost = fast._ef.gg_relight(out, kind, occ, cellmap, suns, RELIT, float(t), dn, int(r0), int(c0), int(step),
                               int(steps), SHADE_REACH_M, int(gr), int(gc))
    if lost.any():
        region = np.where(lost > 0, region | np.uint32(LOST), region).astype(np.uint32)
    return out, region


def _fit_pending(key, partial, sub):
    """Whether a fit made without all of its photo (partial) or map data (sub: (cell, layer)) is to be made again: its
    photo not given up, or one of its layers not (gave_up)."""
    return (partial and not gave_up(key, SHADE_LAYER)) or any(not gave_up(k2, layer) for k2, layer in sub)


def _fit_waiting(key):
    """Whether cell key's fit was made without all of its photo or data less than FIT_PARTIAL_S ago (data_ready)."""
    now = _now()
    with _fits_lock:
        return any(fk[0] == str(key) and v[1] and now - v[0] < FIT_PARTIAL_S for fk, v in _fits.items())


def shade_ready(frame, x0, y0, size, keys, dem):
    """data_ready's part for the shadows: the DEM's tiles under the square, its halo and the terrain's march, and no cell
    fit waiting for its photo (each given up after RETRY_TRIES, as a layer; its map data: data_ready's layers)."""
    ready = True
    dem = _usable_dem(dem)
    if dem is not None:
        e = SHADE_PAD_M + HALO_M
        la, lo, _ = frame.enu_to_geodetic(np.array([x0 - e, x0 + size + e]), np.array([y0 - e, y0 + size + e]), 0.0)
        for k in _dem_keys(la, lo):
            if dem.has(*k) or gave_up(k, "dem"):
                continue
            _ask(k, "dem", None)
            _dem_ready(dem, [k])
            ready = ready and gave_up(k, "dem")
    for k in keys:
        if _fit_waiting(k) and not gave_up(k, SHADE_LAYER):
            ready = False
    return ready


def inputs(frame, x0, y0, size, px, md, surface, widths=None, veg=None):
    """(kind, region, canopy, complete, poly, dirs, phases) of the square for gen_ground, from the cached map data
    (never waits: what is missing is queued, a revisit fault, and complete is False)."""
    import buildings as buildingsmod
    import surface as surfacemod
    fast.require(NEED, "generated ground")
    fields, complete = texel_fields(frame, x0, y0, size, px, surface)
    lat, lon, mx, my, base = fields["lat"], fields["lon"], fields["mx"], fields["my"], fields["base"]
    feats, canopies, missing = [], [], []
    by_layer = {"roads": [], "water": [], "infrastructure": []}
    keys = _cells(lat, lon)
    for key in keys:
        for layer in ("land_use", "water", "infrastructure"):
            f = _layer(md, key, layer, missing, surface)
            if f is not None:
                feats.append((layer, f, key))
                if layer in by_layer:
                    by_layer[layer].append(f)
        canopies.append(_layer(md, key, "canopy", missing, surface))
        f = _layer(md, key, "roads", missing, surface)
        if f is not None:
            by_layer["roads"].append(f)
    if md is not None:
        for key in keys:
            for layer in ("landcover", "buildings"):
                if not md.has(key, layer):
                    _absent(md, key, layer, missing, surface)
    loaded = {key: f for layer, f, key in feats if layer == "land_use"}

    def more(k2):           # (a clipped field's other cells: read from disk, a missing one is pending like the rest)
        if k2 not in loaded:
            loaded[k2] = _layer(md, k2, "land_use", missing, surface)
        return loaded[k2]
    poly, pkinds, dirs, phases = polygons(frame, x0, y0, size, px, feats, more)
    line, lkinds = lines(frame, x0, y0, size, px, by_layer["roads"], by_layer["water"], by_layer["infrastructure"])
    canopy = canopy_grid(mx, my, canopies)
    cover = np.zeros((px, px), np.uint8)
    if surface is not None:
        try:
            m = surfacemod.road_mask(frame, x0, y0, size, px, surface, widths, corners=False)
            if m is not None:
                cover[m >= 128] |= 2
        except Exception as e:      # (the roads are painted over it anyway)
            log.debug("genground: road mask: %s: %s", type(e).__name__, e)
    if md is not None:
        try:
            m = buildingsmod.photo_mask(frame, x0, y0, size, px, md)
            if m is not None:
                cover[np.asarray(m, bool)] |= 4
        except Exception as e:      # (the roofs are filled in after anyway)
            log.debug("genground: building mask: %s: %s", type(e).__name__, e)
    if getattr(_far_tl, "on", False):
        # (a far square, render_far: the trees the mod draws at its distance, not each placed one: the canopy's core
        # wooded, its floor faded by the tile's level, FAR_STRENGTH; no trees drawn: their colour)
        floor, mode = None, 0
        if veg is not None:
            share = _far_tl.share
            floor = np.where((canopy != CANOPY_NODATA) & (canopy >= 3), int(round(255 * share)), 0).astype(np.uint8)
            mode = 2
    else:
        floor, mode = tree_floor(frame, x0, y0, size, px, veg, missing)
    kind, region = fast._ef.gg_regions(np.ascontiguousarray(base), poly, pkinds, line, lkinds, canopy, cover,
                                       float(size / px), floor, mode == 0)
    return kind, region, canopy, bool(complete) and not missing, poly, dirs, phases, floor, mode


def halo_square(x0, y0, size, px):
    """(x0, y0, size, px, h) of the square with HALO_M (whole texels, h of them) round it."""
    t = size / px
    h = int(round(HALO_M / t))
    return x0 - h * t, y0 - h * t, size + 2 * h * t, px + 2 * h, h


def generate(rgb, frame, x0, y0, size, px, md, surface, widths=None, veg=None, dem=None, imagery=None, pending=None,
             relight=True, canopy_out=None):
    """(the generated ground (px, px, 3) uint8 of the square, complete) from rgb, its Sentinel-2 photo (imagery.render
    over the same square at px), and the map data (inputs()). Its outer HALO_M is not final (the windows reach out of
    the square there): render() keeps the middle of a larger square."""
    fast.require(NEED, "generated ground")
    rgb = np.ascontiguousarray(np.asarray(rgb, np.uint8)[:, :, :3])
    if rgb.shape[:2] != (px, px):
        raise ValueError(f"generated ground: a {rgb.shape[:2]} photo for {px} px")
    kind, region, canopy, complete, poly, dirs, phases, floor, mode = inputs(frame, x0, y0, size, px, md, surface,
                                                                             widths, veg)
    t = size / px
    # (the global texel grid of the anchor frame: the blocks' texels lie on it, so their lattices agree)
    gc, gr = int(np.floor(x0 / t + 0.5)), int(np.floor(-(y0 + size) / t + 0.5))
    if imagery is not None and relight:  # (stage 5) the capture sun's shadows out of the photo (shade())
        miss = []
        rgb, region = shade(rgb, frame, x0, y0, size, px, kind, region, canopy, md, dem, imagery, pending, miss, gr,
                            gc, surface)
        complete = complete and not miss
    args = (rgb, kind, region, canopy, poly, dirs, phases, float(x0 + t / 2), float(y0 + size - t / 2), float(t), gr,
            gc)
    if canopy_out is not None and (mode == 1 or (mode == 2 and floor is not None)) \
            and getattr(fast._ef, "VERSION", 0) >= PAIR_NEED:
        # (the mip fade, far rings and block: the same ground with no forest floor under the trees, every other texel
        # alike: the woods in the canopy's colour, written into the coarser mips, write_far_dds / bc7.dds; in one pass,
        # gen_ground_pair. Placed trees (mode 2): gen_ground with a zero floor; not built yet (mode 1, all of
        # FLOOR_MIX): floor mode 0, the mode's only use past the regions. The block's placed trees (not a far square):
        # its unplaced gaps under the canopy model (their gap colour, the forest floor in dense woods: half a wood's
        # texels at Penns Valley and Patapsco) the photo's own colour too, as the far squares' woods (every wooded
        # texel a tree there). An older module: no canopy version, the plain encoding)
        out, can = fast._ef.gen_ground_pair(*args, floor, int(mode), not getattr(_far_tl, "on", False))
        canopy_out.append(can)
    else:
        out = fast._ef.gen_ground(*args, floor, int(mode))
    return out, complete


def render(imagery, frame, x0, y0, size, px, md, surface, widths=None, pending=None, veg=None, dem=None,
           relight=True, canopy_out=None):
    """(rgb (px, px, 3), sources used): the generated ground of [x0, x0 + size]^2 of frame at px, made over the square
    with HALO_M round it and cut to it (every block covering a spot draws it alike, the Codex review of 41d74b61). With
    map data still missing GEN_PENDING goes into pending (earthd.Server.base_ready: made again once it is here).
    The season (imagery's season grade: fall colours, bare winter canopy, the spring flush) goes over the generated
    ground, not under it: Sentinel-2 rendered as it is, the ground made, then graded like any photo of the place."""
    hx0, hy0, hsize, hpx, h = halo_square(x0, y0, size, px)
    where = None
    if hasattr(imagery, "_render") and hasattr(imagery, "_seasoned"):
        where = imagery.square(frame, hx0, hy0, hsize, hpx)
        rgb, used = imagery._render(where, hpx, pending)
    else:                           # (a stand-in imagery: its render as it is)
        rgb, used = imagery.render(frame, hx0, hy0, hsize, hpx, pending)
    cout = [] if canopy_out is not None else None
    out, complete = generate(rgb, frame, hx0, hy0, hsize, hpx, md, surface, widths, veg, dem, imagery, pending,
                             relight, cout)
    if where is not None:
        out = imagery._seasoned(where, np.asarray(out, np.uint8))
        cout = [imagery._seasoned(where, np.asarray(c, np.uint8)) for c in cout or ()] if cout is not None else None
    if canopy_out is not None:
        canopy_out.extend(np.ascontiguousarray(np.asarray(c, np.uint8)[h:h + px, h:h + px]) for c in cout)
    if not complete and pending is not None:
        pending.add(GEN_PENDING)
    return np.ascontiguousarray(np.asarray(out, np.uint8)[h:h + px, h:h + px]), used


# The far rings' generated ground (the user, 2026-11-16: "the texture between the ground with collision vs no collision
# is very different ... visible border"; Patapsco: the block's forest floor beside the far tiles' plain Sentinel-2): a
# far tile of the levels to FAR_MAX_LEVEL (L1: to ~3 km) is cut from the generated ground of FAR_SQ_M squares of the
# anchor frame's grid at FAR_MIN_T (the same function, its windows in metres: the same kinds and colours as the block
# at the seam), memoized per process (FAR_SQ_MAX). At far LODs: no shadow relight (no sun fits); the trees as the mod
# draws them there, not each placed one (no vegetation cell read or waited for): the canopy's core wooded, its forest
# floor as the block's; the fade to the canopy's colour with distance (the user, 2026-11-16, "Sure try the fade": the
# forest-floor colour where the game draws ground between dense trees, near; from height olive-brown ridges read
# less like forest) is in the textures' mips (write_far_dds): each mip blends the floor and canopy versions by its
# texel's size on the ground, FADE_NEAR_M (all floor) to FADE_FAR_M (all canopy). The GPU picks the mip whose texel
# matches the pixel's footprint, so every tile (and the block, whose packed base fades the same way: bc7.dds fade,
# earthd.Server.write_base) shows the same weight at the same footprint: continuous per pixel, no ring seams or block
# square from above, nothing player-dependent in the keys (per-level steps,
# c18ac2f5, showed as nested squares). No GEN_PENDING (a square made without all of its map data is a revisit fault
# and made again after FAR_RETRY_S).
FAR_VERSION = 3
FAR_SQ_M = 1024.0
FAR_MIN_T = 2.0
FAR_MAX_T = 2.0
FAR_MAX_LEVEL = 1
FAR_STRENGTH = {-2: 1.0, -1: 1.0, 0: 1.0, 1: 1.0}
FADE_NEAR_M = 1.0               # a mip texel this fine or finer: all forest floor (the block's: ~1 m at its far side)
FADE_FAR_M = 2.0                # ... this coarse or coarser: the canopy's colour (the plain photo's L2 on: 2 m)
FAR_SQ_MAX = 32
FAR_RETRY_S = 60.0
_far_sq = {}                    # key -> (time, complete, rgb (n, n, 3), used, pending markers)
_far_lock = threading.Lock()
_far_tl = threading.local()     # .on: inside render_far (tree_floor neither waits for nor asks for trees)


def far_strength(level):
    """The forest floor's weight on a far tile of level (FAR_STRENGTH), or None: not made by render_far (off, or past
    FAR_MAX_LEVEL: the plain photo)."""
    if not ON or level is None or level > FAR_MAX_LEVEL:
        return None
    return FAR_STRENGTH.get(int(level), 1.0)


def far_texel(size, px):
    """The squares' texel (m) for a far tile's square of size m at px, or None past FAR_MAX_T * 2 (a coarser level's):
    the tile's texel rounded up to a power of 2, at least FAR_MIN_T."""
    t = size / px
    if not ON or t > 2 * FAR_MAX_T * 1.01:
        return None
    ts = FAR_MIN_T
    while ts < t * 0.99 and ts < FAR_MAX_T:
        ts *= 2.0
    return ts


def far_key(md, trees):
    """The far tiles' key part for the generated ground (their revisit keys: blockworker._far_tile_key): its version,
    the map data's versions and whether the game's trees are drawn. None when off."""
    if not ON:
        return None
    try:
        ver = json.dumps(md.versions(), sort_keys=True, default=str) if md is not None else None
    except Exception:
        ver = None
    return ("genfar", FAR_VERSION, FAR_SQ_M, FAR_MIN_T, FAR_MAX_T, FAR_MAX_LEVEL, tuple(sorted(FAR_STRENGTH.items())),
            FADE_NEAR_M, FADE_FAR_M, ver, bool(trees))


def _far_square(imagery, frame, i, j, ts, md, surface, widths, veg, share=1.0):
    """(rgb, used, complete, pending markers) of the anchor grid's square (i, j) at ts m texels (memoized)."""
    org = tuple(round(float(v), 6) for v in np.asarray(getattr(frame, "origin", (0.0, 0.0, 0.0))).ravel()[:3])
    key = (org, i, j, ts, _fit_key("far", imagery, md, None), id(surface), id(widths), bool(veg), round(share, 6))
    now = _now()
    with _far_lock:
        got = _far_sq.get(key)
    if got is not None and (got[1] or now - got[0] < FAR_RETRY_S):
        if not got[1]:
            revisit.fault()
        return got[2], got[3], got[1], got[4], got[5]
    n = int(round(FAR_SQ_M / ts))
    pend = set()
    cout = []
    _far_tl.on = True
    _far_tl.share = share
    try:
        rgb, used = render(imagery, frame, i * FAR_SQ_M, j * FAR_SQ_M, FAR_SQ_M, n, md, surface, widths, pend, veg,
                           None, relight=False, canopy_out=cout)
    finally:
        _far_tl.on = False
    complete = GEN_PENDING not in pend
    pend.discard(GEN_PENDING)
    with _far_lock:
        while len(_far_sq) >= FAR_SQ_MAX:
            _far_sq.pop(next(iter(_far_sq)))
        _far_sq[key] = (now, complete and not pend, rgb, used, frozenset(pend), cout[0] if cout else None)
    return rgb, used, complete and not pend, frozenset(pend), cout[0] if cout else None


def far_fetch(x0, y0, size, px, level):
    """[(x0, y0, size, px)] the photo render_far reads for a far tile's square (its squares with their halos, at the
    squares' texel), for Imagery.fetch_needed; [] past FAR_MAX_LEVEL."""
    ts = far_texel(size, px)
    if ts is None or far_strength(level) is None:
        return []
    out = []
    n = int(round(FAR_SQ_M / ts))
    for j in range(int(np.floor(y0 / FAR_SQ_M)), int(np.floor((y0 + size) / FAR_SQ_M - 1e-9)) + 1):
        for i in range(int(np.floor(x0 / FAR_SQ_M)), int(np.floor((x0 + size) / FAR_SQ_M - 1e-9)) + 1):
            hx0, hy0, hs, hpx, _h = halo_square(i * FAR_SQ_M, j * FAR_SQ_M, FAR_SQ_M, n)
            out.append((hx0, hy0, hs, hpx))
    return out


def render_far(imagery, frame, x0, y0, size, px, md, surface, widths=None, pending=None, veg=None, level=0):
    """(rgb (px, px, 3), sources used) of a far tile's square [x0, x0 + size]^2 of the anchor frame (its gutter square)
    cut from the generated ground's squares (bilinear at its texel centres), or None (off, or past FAR_MAX_T: the
    plain photo then). The squares' imagery markers still to come go into pending. level: the tile's (its floor's
    weight, far_strength)."""
    share = far_strength(level)
    ts = far_texel(size, px)
    if ts is None or share is None:
        return None
    t = size / px
    i0, i1 = int(np.floor(x0 / FAR_SQ_M)), int(np.floor((x0 + size) / FAR_SQ_M - 1e-9))
    j0, j1 = int(np.floor(y0 / FAR_SQ_M)), int(np.floor((y0 + size) / FAR_SQ_M - 1e-9))
    n = int(round(FAR_SQ_M / ts))
    squares = []
    used_all = {}
    for j in range(j0, j1 + 1):
        for i in range(i0, i1 + 1):
            rgb, used, complete, pend, crgb = _far_square(imagery, frame, i, j, ts, md, surface, widths, veg, share)
            squares.append((rgb, crgb))
            for s, v in (used or {}).items():
                used_all[s] = used_all.get(s, 0) + v
            if pending is not None:
                pending.update(pend)
            if not complete:
                revisit.fault()
    fast.require(559, "cut_ground")
    out, canopy = fast._ef.cut_ground(squares, i1 - i0 + 1, j1 - j0 + 1, n, px, x0, y0, size,
                                      i0 * FAR_SQ_M, (j1 + 1) * FAR_SQ_M, ts)
    _fade_tl.last = (out, canopy, t) if canopy is not None else None
    scale = px * px / max(1, sum(used_all.values()))
    return out, {s: int(v * scale) for s, v in used_all.items()}


_fade_tl = threading.local()    # .last: (the tile's floor rgb, its canopy rgb, texel m) of this thread's last render_far


def clear_fade():
    """Forget this thread's last render_far (a far tile's render starts: earthd.Server.render_tile, blockworker)."""
    _fade_tl.last = None


def fade_weights(t, levels):
    """The forest floor's weight in each of a texture's mips (level k: texel t * 2^k m): 1 to FADE_NEAR_M, 0 from
    FADE_FAR_M, linear in log2 between (dmath)."""
    fast.require(559, "fade_weights")
    return fast._ef.fade_weights(t, levels, FADE_NEAR_M, FADE_FAR_M)


def fade_mix(a, c, s):
    """One mip of the fade: lerp(c, a, s) of two uint8 arrays (a the floor version's mip, c the canopy's), in
    integers (s in 1/256ths): a itself at s 1, c itself at s 0 (both exact: (x * 256 + 128) >> 8 is x)."""
    q = int(round(s * 256))
    if q >= 256:
        return a
    if q <= 0:
        return c
    fast.require(559, "photo_mix")
    return fast._ef.photo_mix(a, c, q)


def keep_painted(before, after, canopy):
    """The canopy version after a step that turned the floor version before into after (roofs filled, roads and rock
    painted): the texels it changed as it painted them, the rest the canopy's. None stays None."""
    if canopy is None:
        return None
    fast.require(559, "photo_keep_painted")
    return fast._ef.photo_keep_painted(np.asarray(before, np.uint8), np.asarray(after, np.uint8),
                                      np.asarray(canopy, np.uint8))


# The block's base (detail mode) fades like the far tiles (bc7.dds fade, terraincache.base_entry): its mips by their
# texels on the ground, fade_weights; in the base photo's revisit key with the switch on (earthd.base_revisit_key).
# 1: the forest floor under the placed trees and the unplaced gaps' colour faded to the photo's (gen_ground_pair gaps)
BLOCK_FADE_VERSION = 1


def block_fade_key():
    return ("blockfade", BLOCK_FADE_VERSION, FADE_NEAR_M, FADE_FAR_M)


def canopy_if_differs(rgb, canopy_out):
    """The canopy version render() gave (canopy_out), or None when there is none or it is the floor version itself (no
    trees: nothing to fade, the plain encoding)."""
    if not canopy_out:
        return None
    c = np.asarray(canopy_out[0], np.uint8)
    if c.shape != np.shape(rgb) or np.array_equal(c, rgb):
        return None
    return c


def write_far_dds(path, rgb, srgb=False):
    """A far tile's texture (imagery.write_dds) with the forest floor faded out through its mips when this thread's
    last render_far made it (the texels the roads, rock faces ... painted over since: as painted in every mip)."""
    import imagery as imagerymod
    last = getattr(_fade_tl, "last", None)
    _fade_tl.last = None
    rgb = np.asarray(rgb, np.uint8)
    if last is None or not ON or last[0].shape != rgb.shape:
        return imagerymod.write_dds(path, rgb, srgb)
    floor, canopy, t = last
    canopy = keep_painted(floor, rgb, canopy)
    return imagerymod.write_dds(path, rgb, srgb, fade=(canopy, t))
