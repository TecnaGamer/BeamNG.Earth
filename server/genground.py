"""Generated ground (BNE_GEN_GROUND=on: the settings page's Experimental "Generated ground"; the user, 2026-11-15: "What
if instead of trying to use the highest res pics, we use them as a base and generate the extra detail?").

Why: the regional 1 m aerial photo services were the world's bottleneck (telemetry of 2026-11-15: our own per-host
limits queued ~70 % of their requests, PASDA, Ohio, Japan's GSI, North Carolina, Virginia, Spain's PNOA, NSW, Indiana,
New Jersey, Vienna, Connecticut; Poland's and Ontario's services gave up on tens of thousands a day; USGS's NAIP
service answered 11.7 % 5xx), and many countries have no free 1 m photo at all.

What: a block's base colour (earthd.Server.write_base / blockworker.base_photo, BASE_RENDER_PX over the 1 km block) is
made from data every place has, instead of the 1 m photo:
- the colour: Sentinel-2 (10 m; the project's own packs, bngs2, else EOX's cloudless mosaics), rendered over the
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
NEED = 309                  # earthfast gg_* / gen_ground (stage 2: lines, surfaces, patterns; linear axes)
# m round a block its generated ground is made over and cut from: a texel's colour reads the inputs this far round
# (genground.rs: windows FAR_R_M 48 + the lattice and purity margins), so overlapping blocks agree
HALO_M = 64.0
# the photo's pending mark while some map data it reads is not on disk (earthd.Server.base_ready / _base_kept)
GEN_PENDING = ("genground_data",)
LAYERS = ("land_use", "water", "infrastructure", "canopy", "landcover", "roads", "buildings")
# The imagery sources the generated ground is made from (imagery.SOURCES): Sentinel-2 alone, global
SOURCES = ("bngs2", "eox2023", "eox2016")

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
    """(lat, lon) (px, px) of the texel centres of [x0, x0 + size]^2 of frame, row 0 = north (imagery.render's)."""
    t = size / px
    k = (np.arange(px) + 0.5) * t
    E, N = np.meshgrid(x0 + k, y0 + size - k)
    lat, lon, _ = frame.enu_to_geodetic(E, N, 0.0)
    return np.asarray(lat, np.float64), np.asarray(lon, np.float64)


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


def data_ready(md, frame, x0, y0, size, surface=None):
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
    return ready


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


def line_items(roads_list, water_list, infra_list):
    """[(order, id, kind, half m, Features, row)] of the lines the ground draws: railway ballast (0), paths, tracks and
    unpaved drives (1), hedges and tree rows (2), streams and ditches (3), piers (4), later orders over earlier. Not
    underground or on a bridge (a road's layer other than 0, a waterway's tunnel or culvert)."""
    out = []
    for f in roads_list:
        if f is None or not len(f):
            continue
        for i in np.flatnonzero(np.asarray(f.kind) == 2):
            cls = _attr(f, "class", i) or ""
            sub = _attr(f, "subtype", i) or ""
            try:
                layer = float(_attr(f, "osm_layer", i) or 0.0)
            except ValueError:
                layer = 0.0
            if layer != 0:          # (underground, or a bridge: the ground under it is not the line)
                continue
            surf = _attr(f, "osm_surface", i)
            width = _width(_attr(f, "osm_width", i))
            if sub == "rail":
                rw = _attr(f, "osm_railway", i) or ""
                if cls in ("subway", "tram", "funicular") or rw in ("subway", "tram", "abandoned", "razed",
                                                                   "proposed", "construction"):
                    continue
                out.append((0, str(f.ids[i]), GRAVEL, RAIL_HALF_M, f, i))
            elif cls in LINE_ROADS:
                kind, half = LINE_ROADS[cls]
                if surf in SURFACE_KIND:
                    kind = SURFACE_KIND[surf]
                out.append((1, str(f.ids[i]), kind, width / 2 if width else half, f, i))
            elif cls == "service" and surf in UNPAVED:
                half = width / 2 if width else SERVICE_HALF.get(_attr(f, "osm_service", i) or "", 2.0)
                out.append((1, str(f.ids[i]), GRAVEL if surf not in ("grass", "dirt", "ground", "earth", "mud")
                            else PATH, half, f, i))
    for f in infra_list:
        if f is None or not len(f):
            continue
        for i in np.flatnonzero(np.asarray(f.kind) == 2):
            tags = _tags(f, i)
            cls = _attr(f, "class", i) or ""
            if cls == "hedge" or tags.get("barrier") == "hedge":
                out.append((2, str(f.ids[i]), TREE, HEDGE_HALF_M, f, i))
            elif tags.get("natural") == "tree_row":
                out.append((2, str(f.ids[i]), TREE, TREE_ROW_HALF_M, f, i))
            elif cls == "pier" or tags.get("man_made") == "pier":
                # (a pier's deck over the water: built ground, as INFRA_KIND's pier polygons; drawn after the water)
                width = _width(tags.get("width"))
                out.append((4, str(f.ids[i]), BUILT, width / 2 if width else PIER_HALF_M, f, i))
    for f in water_list:
        if f is None or not len(f):
            continue
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
            out.append((3, str(f.ids[i]), WATER, width / 2 if width else half, f, i))
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


def canopy_grid(lat, lon, rasters):
    """The canopy height (m, uint8; CANOPY_NODATA where none) at the texels from the cells' canopy Rasters (EPSG:3857,
    nearest pixel)."""
    out = np.full(lat.shape, CANOPY_NODATA, np.uint8)
    if not rasters:
        return out
    mx = R_MERC * np.radians(lon)
    my = R_MERC * dnp.log(dnp.tan(np.pi / 4 + np.radians(lat) / 2))
    for r in rasters:
        if r is None:
            continue
        h, w = r.data.shape
        inside = (mx >= r.x0) & (mx < r.x0 + w * r.dx) & (my <= r.y0) & (my > r.y0 - h * r.dy)
        if inside.any():
            out[inside] = r.sample(mx[inside], my[inside])
    return out


def inputs(frame, x0, y0, size, px, md, surface, widths=None):
    """(kind, region, canopy, complete, poly, dirs, phases) of the square for gen_ground, from the cached map data
    (never waits: what is missing is queued, a revisit fault, and complete is False)."""
    import buildings as buildingsmod
    import surface as surfacemod
    fast.require(NEED, "generated ground")
    lat, lon = _grid(frame, x0, y0, size, px)
    complete = True
    if surface is not None:
        classes, complete = surface.classes(lat, lon)
    else:
        classes = np.zeros(lat.shape, np.uint8)
    base = base_kinds(classes, lat, lon)
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
    canopy = canopy_grid(lat, lon, canopies)
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
    kind, region = fast._ef.gg_regions(np.ascontiguousarray(base), poly, pkinds, line, lkinds, canopy, cover)
    return kind, region, canopy, bool(complete) and not missing, poly, dirs, phases


def halo_square(x0, y0, size, px):
    """(x0, y0, size, px, h) of the square with HALO_M (whole texels, h of them) round it."""
    t = size / px
    h = int(round(HALO_M / t))
    return x0 - h * t, y0 - h * t, size + 2 * h * t, px + 2 * h, h


def generate(rgb, frame, x0, y0, size, px, md, surface, widths=None):
    """(the generated ground (px, px, 3) uint8 of the square, complete) from rgb, its Sentinel-2 photo (imagery.render
    over the same square at px), and the map data (inputs()). Its outer HALO_M is not final (the windows reach out of
    the square there): render() keeps the middle of a larger square."""
    fast.require(NEED, "generated ground")
    rgb = np.ascontiguousarray(np.asarray(rgb, np.uint8)[:, :, :3])
    if rgb.shape[:2] != (px, px):
        raise ValueError(f"generated ground: a {rgb.shape[:2]} photo for {px} px")
    kind, region, canopy, complete, poly, dirs, phases = inputs(frame, x0, y0, size, px, md, surface, widths)
    t = size / px
    # (the global texel grid of the anchor frame: the blocks' texels lie on it, so their lattices agree)
    gc, gr = int(np.floor(x0 / t + 0.5)), int(np.floor(-(y0 + size) / t + 0.5))
    out = fast._ef.gen_ground(rgb, kind, region, canopy, poly, dirs, phases, float(x0 + t / 2),
                              float(y0 + size - t / 2), float(t), gr, gc)
    return out, complete


def render(imagery, frame, x0, y0, size, px, md, surface, widths=None, pending=None):
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
    out, complete = generate(rgb, frame, hx0, hy0, hsize, hpx, md, surface, widths)
    if where is not None:
        out = imagery._seasoned(where, np.asarray(out, np.uint8))
    if not complete and pending is not None:
        pending.add(GEN_PENDING)
    return np.ascontiguousarray(np.asarray(out, np.uint8)[h:h + px, h:h + px]), used
