"""Snow on the ground photos (winter plan W2, ~/bnge-tests/winterplan/PLAN.md 3.1 / 3.2; the user, 2026-11-24: a "white
Christmas" build first). BNE_WINTER=on (the settings page's Experimental "Winter") turns it on; off, nothing here is
read, keyed or applied and every photo and key is today's.

The block's base photo and every far tile take the same snow term (earthfast.SnowLook, snowlook.rs), after their roads
and rock, so the two agree at their seam: white where the snow state says so at the texel's own height (our DEM, the
texture's height grid), lower on slopes facing the pole, none on steep rock, the forest's dark crowns mostly kept,
already-white texels left alone, permanent snow above the climatic snowline in polar / alpine climates.

The snow state: a FIELD of band tables per forcing cell (the interface with W1's kernel and W5's pack). A Table is up to
8 rows (band elevation m, cover 0..8, depth bin 0..6, age 0..3), lowest first, linear in elevation between rows and
held past the ends, and the road shares P0..P4 (eighths of the ground's cover a road of that plow class keeps: W3's
hook; the photos get no road mask yet, so the roads take the ground's cover). A Field is a resolution (deg) and the
tables by cell (ci, cj) = (floor((lon + 180) / res), floor((90 - lat) / res)), or one table everywhere. Where it comes
from (BNE_SNOW):
  none                  nothing (the keys are today's exactly);
  dusting light heavy deep alpine spring: a fixed debug preset (PRESETS), the same everywhere and on every machine;
  real (the default with BNE_WINTER on): the provider W1 registers (set_provider); none until it does.

Keys: a texture's snow is a pure function of its SPEC (spec_for_box: the tables of the cells round its square, a hash
of them, and whether permanent snow can occur there); the spec's key part (key_parts) joins the season's in the far
tiles' and base photos' revisit keys. No snow on a square (no table with cover there, no polar / alpine climate near
it) is no key part at all: those keys stay what they were. A builder never asks "the current state" twice: the server
makes the spec once and hands the same one to the key and to the render (tex["snow"], earthd's calls).

Never in plans, cell facts or the decision archives (revisit.PLAN_NEUTRAL; nothing the planning reaches imports this).
"""
import hashlib
import os
from collections import namedtuple

import numpy as np

import fast

ON = os.environ.get("BNE_WINTER", "off").strip().lower() in ("on", "1", "yes", "true")
MODE = (os.environ.get("BNE_SNOW", "") or ("real" if ON else "none")).strip().lower()
# The look's own version: in every spec's key part (bump it with any change of PARAMS, the kernel's snow term or how a
# spec is cut from a field)
LOOK_VERSION = 1
GEO_N = 9               # nodes a side of the geodetic grid over a texture (lat, lon, curvature drop, snowline)
FIELD_PAD_CELLS = 1     # cells past the square a spec holds (the texels near its edge blend with them)

# rows: ((z m, cover 0..8, depth 0..6, age 0..3), ...); road: 5 x 0..8; ice: 0..8, the share of still water (lakes,
# ponds, reservoirs) frozen and snow-covered (W1: from freezing degree-days; W6 makes it drivable). Moving water (rivers,
# canals) and the sea stay open whatever it says.
Table = namedtuple("Table", "rows road ice", defaults=(0,))
NO_ROAD = (8, 8, 8, 8, 8)
# The photos' paved surfaces (surface.road_mask: carriageways, lots, pads) as one plow class until W3 tells them apart
# (W3: the road graph's classes P0..P4): P2's share of the cover. Main and residential streets are plowed within hours
# of a storm in most snowy places (Minneapolis in January, Mapillary 2026-01-21: bare wet streets, white yards).
PHOTO_ROAD_CLASS = 3            # 1..5 = P0..P4
ROAD_MASK_ON = 128              # of road_mask's 0..255 coverage
# preset road shares (eighths of the ground's cover, P0..P4): plowed main roads, packed side streets
PLOWED = (1, 1, 2, 3, 6)
BARE_ROADS = (0, 0, 0, 1, 3)    # a dusting does not lie on pavement

# Debug presets (the plan's 2.11: "a preset is a fixed synthetic forcing record"; here the band table it leads to)
PRESETS = {
    "dusting": Table(((0, 4, 1, 0),), BARE_ROADS, 0),
    "light": Table(((0, 7, 2, 0),), PLOWED, 4),
    "heavy": Table(((0, 8, 4, 0),), PLOWED, 8),      # (a long cold spell: lakes frozen and white)
    "deep": Table(((0, 8, 6, 0),), PLOWED, 8),
    # valleys bare, a line about 900 m, deep above 2,200 m
    "alpine": Table(((700, 0, 0, 1), (1100, 8, 3, 0), (2200, 8, 5, 0)), PLOWED, 6),
    # a late-spring line about 1,700 m, old snow
    "spring": Table(((1500, 0, 0, 2), (1900, 6, 2, 2), (2600, 8, 4, 1)), BARE_ROADS, 0),
}

# snowlook.rs SnowParams, in its order
PARAMS = np.array((
    300.0,                  # aspect_m: the line this much lower on a slope facing the pole (higher facing the sun)
    0.70, 1.19,             # steep_tan: snow slides off from 35 deg, none holds past 50 deg (surface.ROCK_SLOPE_DEG 35)
    150.0, 250.0,           # perm_band: permanent snow fades in from 150 m below the climatic snowline to 250 m above
    3.0, 20.0, 0.4,         # patch_m, patch_w: grass tips through a dusting, drifts and melt patches
    0.15, 1.4, 0.2,         # soft, gain, bias of the patch threshold
    0.20, 0.34, 0.92,       # canopy_v, canopy_keep: dark green crowns keep most of their colour (shadows do not):
                            # a dense conifer stand near black in winter Sentinel-2 (Breckenridge, 2025-03-01)
    0.97, 1.0, 0.12,        # white_v, white_sat: only blown-out white texels left as they are (snowy imagery takes
                            # the snow's colour and keeps its own shading: no yellow summer firn beside our white)
    0.80, 0.45, 1.04,       # shade: the snow's brightness follows the photo's own shading a little
    236.0, 240.0, 246.0,    # colour: fresh
    222.0, 225.0, 230.0,    # settled
    204.0, 207.0, 212.0,    # old
    186.0, 183.0, 178.0,    # dirty
    0.0, 0.6, 0.85, 1.0, 1.0, 1.0, 1.0,    # opacity by depth bin D0..D6
    4.0, 256.0,             # coarse_m, coarse_max: the cover's grid
    6.0, 1.0,               # perm_depth, perm_age
    0.70, 0.98,             # canopy_rel: a green texel this much darker than the ground round it is a crown
    -0.012, 0.012,          # canopy_green: green over red (G - R) from which a dark texel is vegetation (olive
                            # conifers in the USGS mosaic are barely green; dark soil, scree and rock are red)
    250.0,                  # canopy_r_m: the radius of "the ground round it"
    -0.06, 0.0,             # canopy_gb: green against blue: bluer is shade or water, not a crown
    0.7, 0.85,              # canopy_pct: the ground's reference at least 0.85 of the texture's 70th percentile value
    0.22, 0.38,             # water_v: a dark ...
    0.02, 0.06,             # water_slope: ... flat blue texel is open water: the photo's own (lake ice is W6's)
    0.08, 0.20, 0.65,       # canopy_sat, canopy_sat_floor: a grey (bare, the winter grade's) crown keeps 0.65 of it
                            # at a fine texel (at 10 m a bare stand is as dark as a conifer one)
    300.0,                  # treeline_band: m, crowns fade out about the climatic treeline
    0.55,                   # struct_keep: the darker part of a textured coarse texel (a town's streets, alleys, roofs'
                            # shaded sides) keeps 0.55 of its colour
    0.05, 0.11,             # struct_cv: the value's coefficient of variation in 5 x 5 texels: smooth (yards, parks,
                            # fields, ski runs: white) .. textured (streets, roofs, trees and shadows: kept)
    1.5, 4.0,               # struct_texel: m a texel over which the window rules fade in (block photos: none)
    0.3,                    # winter_dark: kept crowns and textured texels this much darker under full cover (low sun)
    0.75, 1.1,              # rock_rel: on steep ground, a texel darker than 0.75 of the ground round it is bare rock
    0.35, 0.55,             # lake_frac: water with this share of water within lake_r_m is still water (a lake)
    400.0,                  # lake_r_m
    2.0, 6.0,               # sea_h: m, water below is the sea (open)
    0.0001, 0.0004,         # flat: height steps per m between grid nodes of a hydro-flattened water surface (a lake
                            # is exactly flat in the DEMs; at 0.0008-0.0025 Furano's flat valley fields read as water)
    0.40, 0.55,             # flat_v: ... where the photo is no brighter than this
    0.05, 0.10,             # flat_cv: ... and smooth
    700.0, 1400.0, 0.85,    # montane: mid-latitude crowns evergreen (spruce, fir, pine) from 700 m to 1,400 m, at most 0.85
    138.0, 106.0, 76.0,     # bare: a leafless crown over snow at 10 m: warm brown (Sentinel-2's Spessart beech and
                            # oak under snow, 2021-02-13, Vermont's hardwoods 2022-02-06), its texture the photo's own
    0.9,                    # bare_k: its share of a deciduous crown's texel under full cover
    0.3,                    # ever_dark: an evergreen crown 0.3 darker under full cover (low winter sun)
    0.55,                   # open_dark: open water 0.55 darker under full cover (Sentinel-2's open Mississippi in
                            # February: near black between white banks)
), np.float64)


class Field:
    """A snow state over the world: tables by forcing cell at res degrees, or one table everywhere (uniform)."""

    def __init__(self, res=0.25, cells=None, uniform=None):
        self.res = float(res)
        self.cells = dict(cells or {})      # (ci, cj) -> Table
        self.uniform = uniform

    def table(self, ci, cj):
        return self.uniform if self.uniform is not None else self.cells.get((ci, cj))


def _has_cover(t):
    return t is not None and any(int(r[1]) > 0 for r in t.rows)


_provider = None


def set_provider(fn):
    """W1's snow state: fn(south, west, north, east) -> Field (or None: no snow known there) for BNE_SNOW=real. The
    Field it gives for a box must not change while a spec made from it is in use (the server makes a spec once per
    texture); a change of state is a new spec, and the server re-renders what is shown (W1 / W5)."""
    global _provider
    _provider = fn


def set_mode(mode):
    """Another snow mode live (the server's main process: the specs are made there and handed to the workers):
    True when it changed (the caller re-renders what is shown: earthd.Server.snow_changed). Winter must be on."""
    global MODE
    mode = str(mode or "").strip().lower()
    if not ON or mode == MODE or not (mode in PRESETS or mode in ("none", "real")):
        return False
    MODE = mode
    return True


def field_for(south, west, north, east):
    if not ON or MODE == "none":
        return None
    if MODE in PRESETS:
        return Field(1.0, uniform=PRESETS[MODE])
    if MODE == "real" and _provider is not None:
        return _provider(south, west, north, east)
    return None


# field: (res, ci0, cj0, idx int32 (nj, ni), tables, counts) or None; leaf, wsh: the winter packs' grids round the box
# (pack_grid: (values uint8, north, west, res, scale, nodata)) or None; credits: the data credits' ids of what it used
Spec = namedtuple("Spec", "sig field perm leaf wsh credits trees", defaults=(None, None, frozenset(), None))

# The winter packs (winterpacks.py, the data repo's leaftype / watershare: ESA CCI's evergreen share, JRC's water share),
# set by the server's main process (set_packs); without them the climate's evergreen share and the photo's water.
_packs = None
PACK_SIDE_MAX = 256         # cells a side of a pack window a spec carries (a far global tile's box: strided)


def set_packs(p):
    """The server's winterpacks.Packs (None: none)."""
    global _packs
    _packs = p


def _pack_window(values, north, west, res, south, west_, north_, east, pad):
    """The cells of a (rows, cols) grid (row 0 at north, column 0 at west) round the box, strided to PACK_SIDE_MAX:
    (values, its north, west, res)."""
    rows, cols = values.shape
    j0 = max(int(np.floor((north - north_) / res)) - pad, 0)
    j1 = min(int(np.floor((north - south) / res)) + pad, rows - 1)
    i0 = max(int(np.floor((west_ - west) / res)) - pad, 0)
    i1 = min(int(np.floor((east - west) / res)) + pad, cols - 1)
    if j1 < j0 or i1 < i0:
        return None
    k = max(1, int(np.ceil(max(j1 - j0 + 1, i1 - i0 + 1) / PACK_SIDE_MAX)))
    v = np.ascontiguousarray(values[j0:j1 + 1:k, i0:i1 + 1:k])
    # (strided: each kept cell stands for k x k; its centre where the sampler expects it)
    return v, north - j0 * res + 0.5 * res * (k - 1), west + i0 * res - 0.5 * res * (k - 1), res * k


def _pack_grids(south, west, north, east):
    """(leaf, wsh, credits, versions, trees) of the winter packs round the box (each grid None when not had)."""
    if _packs is None:
        return None, None, frozenset(), None, None
    import winterpacks
    leaf = wsh = trees = None
    credits = set()
    try:
        g = _packs.leaf_grid()
        if g is not None:
            w = _pack_window(g.evergreen, g.north, g.west, g.res, south, west, north, east, 2)
            if w is not None:
                leaf = (w[0], w[1], w[2], w[3], float(g.scale), -1)
                credits.add("land:esa-cci-lc")
            t = _pack_window(g.trees, g.north, g.west, g.res, south, west, north, east, 2)
            if t is not None:
                trees = (t[0], t[1], t[2], t[3], float(g.scale), -1)
    except Exception as e:
        import logging
        logging.getLogger("earthd.snowcover").debug("leaf type pack: %s: %s", type(e).__name__, e)
    try:
        if west <= east:
            pad = 2.0 / winterpacks.WATER_PER_DEG
            g = _packs.water_grid(max(south - pad, -90.0), max(west - pad, -180.0), min(north + pad, 90.0),
                                  min(east + pad, 180.0))
            if g is not None:
                w = _pack_window(g.values, g.north, g.west, g.res, south, west, north, east, 2)
                if w is not None:
                    wsh = (w[0], w[1], w[2], w[3], float(g.scale), int(g.nodata))
                    credits.add("land:jrc-gsw")
    except Exception as e:
        import logging
        logging.getLogger("earthd.snowcover").debug("water share pack: %s: %s", type(e).__name__, e)
    return leaf, wsh, frozenset(credits), _packs.versions(), trees


def credit_ids(spec):
    """The data credits' ids of what a texture's snow read (the winter packs), for its tile's sources."""
    return frozenset() if spec is None else frozenset(getattr(spec, "credits", ()) or ())


def _window(f, south, west, north, east):
    """(res, ci0, cj0, idx, tables (n, 8, 4) int32, counts (n, 7) int32: rows, road shares, ice) of the field's cells
    round the box, or None
    when none of them has any cover."""
    if f is None:
        return None
    res = f.res
    if f.uniform is not None:
        if not _has_cover(f.uniform):
            return None
        ci0 = int(np.floor((0.5 * (west + east) + 180.0) / res))
        cj0 = int(np.floor((90.0 - 0.5 * (south + north)) / res))
        tabs, idx = [f.uniform], np.zeros((1, 1), np.int32)
    else:
        ci0 = int(np.floor((west + 180.0) / res)) - FIELD_PAD_CELLS
        ci1 = int(np.floor((east + 180.0) / res)) + FIELD_PAD_CELLS
        cj0 = int(np.floor((90.0 - north) / res)) - FIELD_PAD_CELLS
        cj1 = int(np.floor((90.0 - south) / res)) + FIELD_PAD_CELLS
        idx = np.full((cj1 - cj0 + 1, ci1 - ci0 + 1), -1, np.int32)
        tabs, seen = [], {}
        for cj in range(cj0, cj1 + 1):
            for ci in range(ci0, ci1 + 1):
                t = f.table(ci, cj)
                if not _has_cover(t):
                    continue
                k = (tuple(map(tuple, t.rows)), tuple(t.road), int(t.ice))
                if k not in seen:
                    seen[k] = len(tabs)
                    tabs.append(t)
                idx[cj - cj0, ci - ci0] = seen[k]
        if not tabs:
            return None
    tables = np.zeros((len(tabs), 8, 4), np.int32)
    counts = np.zeros((len(tabs), 7), np.int32)
    for n, t in enumerate(tabs):
        rows = sorted((int(z), int(c), int(d), int(a)) for z, c, d, a in t.rows)[:8]
        if not rows:
            raise ValueError("snow table without rows")
        for r, row in enumerate(rows):
            if not (0 <= row[1] <= 8 and 0 <= row[2] <= 6 and 0 <= row[3] <= 3):
                raise ValueError(f"snow table row out of range: {row}")
            tables[n, r] = row
        counts[n, 0] = len(rows)
        counts[n, 1:6] = [min(max(int(v), 0), 8) for v in tuple(t.road)[:5]]
        counts[n, 6] = min(max(int(t.ice), 0), 8)
    return (res, ci0, cj0, idx, tables, counts)


def deep_possible():
    """Can a block get deep snow (its .ter then lists the snow layer: surface.write_ter / snow_layers)? While winter is
    on, whatever the mode: a preset's depth or W1's state may reach D3 any time (set_mode, the state's hours), and the
    block processes that write the .ter do not follow a live mode change."""
    return ON


def perm_possible(south, west, north, east):
    """Can permanent snow lie in the box (a polar / alpine Koppen class within a pixel of it)?"""
    import climate
    fast.require(625, "snow_perm_possible")
    return bool(fast._ef.snow_perm_possible(climate._load(), climate.RES, float(south), float(west), float(north),
                                            float(east)))


def spec_for_box(south, west, north, east):
    """The snow a texture over the box (deg) is made with, or None (no snow can lie there: no key part)."""
    if not ON or MODE == "none":
        return None
    win = _window(field_for(south, west, north, east), south, west, north, east)
    perm = perm_possible(south, west, north, east)
    if win is None and not perm:
        return None
    leaf, wsh, credits, versions, trees = _pack_grids(south, west, north, east)
    # (the packs' versions, and whether each was had for this box: what the texture is made with)
    h = hashlib.sha1(repr(("snow", LOOK_VERSION, PARAMS.tobytes(), GEO_N, perm, versions, leaf is not None,
                           wsh is not None)).encode())
    if win is not None:
        res, ci0, cj0, idx, tables, counts = win
        h.update(repr((res, ci0, cj0, idx.shape)).encode())
        h.update(idx.tobytes())
        h.update(tables.tobytes())
        h.update(counts.tobytes())
    return Spec(("snow", h.hexdigest()[:20]), win, perm, leaf, wsh, credits, trees)


def key_parts(spec):
    """A spec's part of a revisit key: () for no snow (the key stays what it was), else (its signature,)."""
    return () if spec is None else (tuple(spec.sig),)


def box_of(frame, x0, y0, size):
    """(south, west, north, east) of the square [x0, x0 + size]^2 of frame (its corners and edge midpoints)."""
    s = np.array((0.0, 0.5, 1.0))
    e, n = np.meshgrid(x0 + s * size, y0 + s * size)
    lat, lon, _ = frame.enu_to_geodetic(e.ravel(), n.ravel(), 0.0)
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    return float(lat.min()), float(lon.min()), float(lat.max()), float(lon.max())


def spec_for_square(frame, x0, y0, size):
    """spec_for_box of a frame's square (None at once while winter is off: no geodesy)."""
    if not ON or MODE == "none":
        return None
    return spec_for_box(*box_of(frame, x0, y0, size))


def look(spec, frame, x0, y0, size, U, ux0, uy0, usp, road=None, water=None):
    """The earthfast.SnowLook of a texture over [x0, x0 + size]^2 of frame with height grid U (frame z, [j north from
    uy0, i east from ux0], spacing usp), or None (no spec / no heights). road: its plow classes per texel (road_classes),
    or None."""
    if spec is None or U is None:
        return None
    U = np.ascontiguousarray(np.asarray(U, np.float64))
    if U.ndim != 2 or min(U.shape) < 2:
        return None
    fast.require(625, "SnowLook")
    t = np.linspace(0.0, 1.0, GEO_N)
    e, n = np.meshgrid(x0 + t * size, y0 + size - t * size)       # row 0 north
    lat, lon, h = frame.enu_to_geodetic(e.ravel(), n.ravel(), 0.0)
    lat = np.asarray(lat, np.float64)
    lon = np.asarray(lon, np.float64)
    # the geodetic height of a point at frame up 0 there: the frame's curvature drop (a far tile's z is lower by it)
    zoff = np.asarray(h, np.float64)
    if spec.perm:
        import climate
        ela = np.asarray(fast._ef.snow_ela(lat, lon, climate._load(), climate.RES), np.float64)
    else:
        ela = np.full(lat.shape, 1.0e9)
    import climate
    # the evergreen share of the crowns by climate (snow_evergreen: Koppen; the mountains' conifers by height in the
    # kernel): a deciduous crown under snow is a light bare crown, an evergreen one near black. A climate fallback until a
    # leaf-type map is packed (MODIS MCD12C1 0.05 deg evergreen / deciduous shares, or ESA CCI land cover 300 m)
    ever = np.asarray(fast._ef.snow_evergreen(lat, lon, climate._load(), climate.RES), np.float64)
    shp = (GEO_N, GEO_N)
    geo = tuple(np.ascontiguousarray(a.reshape(shp)) for a in (lat, lon, zoff, ela, ever))
    field = None
    if spec.field is not None:
        res, ci0, cj0, idx, tables, counts = spec.field
        field = (float(res), int(ci0), int(cj0), np.ascontiguousarray(idx, np.int32),
                 np.ascontiguousarray(tables, np.int32), np.ascontiguousarray(counts, np.int32))
    return fast._ef.SnowLook((float(x0), float(y0), float(size)), (frame.lat0, frame.lon0), U,
                             (float(ux0), float(uy0), float(usp)), geo, field, PARAMS,
                             None if road is None else np.ascontiguousarray(road, np.uint8),
                             None if water is None else np.ascontiguousarray(water, np.uint8),
                             getattr(spec, "leaf", None), getattr(spec, "wsh", None), getattr(spec, "trees", None))


# Overture water (the map data's "water" layer): moving water stays open (a river, a canal, a stream: their current),
# still water freezes by the tables' ice share, the sea stays open. snowlook.rs WATER_* values.
WATER_MOVING, WATER_STILL, WATER_SEA = 1, 2, 3
MOVING_WATER = {"river", "stream", "canal", "drain", "ditch", "rapids", "waterfall", "riverbank", "tidal_channel"}
SEA_WATER = {"ocean", "sea", "bay", "strait", "sound", "fjord"}
NOT_WATER = {"swimming_pool", "reflecting_pool", "fountain", "pool"}
LINE_WIDTH_M = {"river": 20.0, "canal": 10.0, "stream": 4.0, "drain": 3.0, "ditch": 2.0}
WATER_MAX_TEXEL = 2.0       # m: the mask only where the photo's roads are painted in (their cells are on the disk)


def water_classes(frame, x0, y0, size, px, md):
    """The mapped water per texel (px x px uint8: 0 land, WATER_MOVING / WATER_STILL / WATER_SEA) of a photo over
    [x0, x0 + size]^2, from the map data's water layer (polygons and the moving water's lines), or None (no map data,
    texels coarser than WATER_MAX_TEXEL, no water there). A cell that cannot be read faults the build (revisit.fault:
    the texture is shown, not kept)."""
    if md is None or size / max(px, 1) > WATER_MAX_TEXEL:
        return None
    from PIL import Image, ImageDraw
    import areaprops
    import mapcells
    import mapgeom
    s_, w_, n_, e_ = box_of(frame, x0, y0, size)
    keys = mapcells.cells_for_bbox(s_, w_, n_, e_)
    im = Image.new("L", (px, px), 0)
    draw = ImageDraw.Draw(im)
    k = px / size

    def to_px(xy):
        return [((x - x0) * k, (y0 + size - y) * k) for x, y in np.asarray(xy, np.float64)]

    def kind_of(feats, i):
        vals = set()
        for name in ("subtype", "class"):
            a = feats.attrs.get(name)
            if a is not None and i < len(a):
                vals.add(str(a[i]).strip().lower())
        if vals & NOT_WATER:
            return None, vals
        if vals & SEA_WATER:
            return WATER_SEA, vals
        if vals & MOVING_WATER:
            return WATER_MOVING, vals
        return WATER_STILL, vals

    feats_list = []
    for key in keys:
        try:
            f = md.cell(key, layers=("water",), kind="demand").layers.get("water")
        except Exception:
            import revisit
            revisit.fault()
            continue
        if f is not None and len(getattr(f, "kind", ())):
            feats_list.append(f)
    drew = False
    box = (x0, y0, x0 + size, y0 + size)
    for order in (WATER_STILL, WATER_SEA, WATER_MOVING):       # (a river through a lake stays a river)
        for f in feats_list:
            for i in range(len(f.kind)):
                cls, vals = kind_of(f, i)
                if cls != order:
                    continue
                if int(f.kind[i]) == mapgeom.POLYGON:
                    r = areaprops._rings(f, frame, i)
                    if r is None:
                        continue
                    pts, off = r
                    outer = pts[off[0]:off[1]]
                    if not areaprops._meets(box, (outer[:, 0].min(), outer[:, 1].min(), outer[:, 0].max(),
                                                  outer[:, 1].max()), 50.0):
                        continue
                    draw.polygon(to_px(outer), fill=cls)
                    for a, b in zip(off[1:-1], off[2:]):        # (islands)
                        draw.polygon(to_px(pts[a:b]), fill=0)
                    drew = True
                elif cls == WATER_MOVING:
                    width = max(1, int(round(max(LINE_WIDTH_M.get(v, 0.0) for v in vals | {""}) * k))) \
                        if any(v in LINE_WIDTH_M for v in vals) else 0
                    if not width:
                        continue
                    for part in f.parts(int(i)):
                        line = np.asarray(part[0] if part and np.ndim(part[0]) == 2 else part, np.float64)
                        if len(line) < 2:
                            continue
                        ex, ny = mapgeom.enu_xy(f, frame, line)
                        draw.line(to_px(np.column_stack([ex, ny])), fill=cls, width=width)
                        drew = True
    if not drew:
        return None
    return np.asarray(im, np.uint8)


def masks(frame, x0, y0, size, px, surface=None, widths=None, md=None):
    """(road_classes, water_classes) of a photo square: the snow's per-texel map knowledge (each None when unknown)."""
    road = water = None
    try:
        road = road_classes(frame, x0, y0, size, px, surface, widths)
    except Exception as e:
        import logging
        logging.getLogger("earthd.snowcover").debug("snow road mask: %s: %s", type(e).__name__, e)
    try:
        water = water_classes(frame, x0, y0, size, px, md)
    except Exception as e:
        import logging
        import revisit
        logging.getLogger("earthd.snowcover").debug("snow water mask: %s: %s", type(e).__name__, e)
        revisit.fault()
    return road, water


def road_classes(frame, x0, y0, size, px, surface, widths=None):
    """The plow class per texel (px x px uint8: 0 none, PHOTO_ROAD_CLASS on paved ground) of a photo over
    [x0, x0 + size]^2 (surface.road_mask's carriageways, lots and pads, without the curb corners), or None (no surface,
    texels coarser than surface.ROAD_MASK_MAX_TEXEL, no roads)."""
    if surface is None:
        return None
    import surface as surfacemod
    m = surfacemod.road_mask(frame, x0, y0, size, px, surface, widths, corners=False)
    if m is None:
        return None
    return np.where(np.asarray(m) >= ROAD_MASK_ON, PHOTO_ROAD_CLASS, 0).astype(np.uint8)


def look_from_rock(spec, frame, rock, road=None, water=None):
    """look() from a far texture's rock inputs (rockface.tile_inputs: (U, (x0, y0, size, ux0, uy0, usp, lat, lon))).
    A spec whose look cannot be made (no heights, an error): None, and the texture is not kept (revisit.fault: its
    key says snow)."""
    if spec is None:
        return None
    try:
        if rock is None:
            raise ValueError("no height grid")
        U, (x0, y0, size, ux0, uy0, usp, _lat, _lon) = rock
        sl = look(spec, frame, x0, y0, size, U, ux0, uy0, usp, road, water)
        if sl is None:
            raise ValueError("no look")
        return sl
    except Exception as e:
        import logging
        import revisit
        logging.getLogger("earthd.snowcover").debug("snow look not made: %s: %s", type(e).__name__, e)
        revisit.fault()
        return None


def apply(spec, rgb, frame, x0, y0, size, U, ux0, uy0, usp, road=None, water=None):
    """rgb (px, px, 3) over [x0, x0 + size]^2 of frame with its snow, or rgb itself (no snow on it). road: its plow
    classes (road_classes) or None."""
    sl = look(spec, frame, x0, y0, size, U, ux0, uy0, usp, road, water)
    if sl is None:
        return rgb
    out = sl.apply(np.ascontiguousarray(np.asarray(rgb, np.uint8)[..., :3]))
    return rgb if out is None else out


def apply_kept(spec, rgb, frame, x0, y0, size, U, ux0, uy0, usp, road=None, water=None):
    """apply() for a texture that is kept under its spec's key: when its snow cannot be made (no heights, an error) it
    is shown without it and not kept (revisit.fault: its key says snow)."""
    if spec is None:
        return rgb
    try:
        if U is None:
            raise ValueError("no height grid")
        return apply(spec, rgb, frame, x0, y0, size, U, ux0, uy0, usp, road, water)
    except Exception as e:          # (the photo without its snow, this once)
        import logging
        import revisit
        logging.getLogger("earthd.snowcover").debug("snow not applied: %s: %s", type(e).__name__, e)
        revisit.fault()
        return rgb


def hello(lat=None, lon=None):
    """The mod's part of the server's hello (winterlook.lua): None while off, else the mode and, for a preset, its
    cover and depth at the anchor's height band (the ground cover's density, the snow layer's look)."""
    if not ON:
        return None
    m = {"mode": MODE}
    t = PRESETS.get(MODE)
    if t is not None:
        top = max(t.rows, key=lambda r: (r[1], r[2]))
        m["cover"], m["depth"] = int(top[1]), int(top[2])
    return m
