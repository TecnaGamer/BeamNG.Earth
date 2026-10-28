"""Ground surfaces under the physics block (docs/ROADMAP.md v1.5 "Per-cell surface physics"; earthd
--surface, on by default since 2026-10-11).

The TerrainBlock is physics-only in mesh mode, so its layer map decides nothing but the tyres'
ground model (and GroundCover, which binds to a layer). Every 2 m sample of the block gets one of
MATERIALS from the ESA WorldCover class under it (docs/research/2026-10-10-world-data-sources.md
§5.4, without the OSM overrides yet) and the slope (ROCK where steeper than ROCK_SLOPE_DEG); the
server writes one 0/255 mask per material next to the heightmap and the mod imports them with the
block (8 greyscale masks: +4-5 ms per 512² import, docs/research/2026-10-11-engine-probes.md).

Land cover comes from the map-data cache (mapdata.py "landcover" layer, per z14 cell). Building a
block never waits for the network: a cell that is not cached yet reads as "no data" (DIRT, the
material the block had before this module) and is fetched in the background (prefetch around the
player, kind "prefetch"); the blocks built after it arrives have the right surfaces. The masks are a
pure function of the cached land cover and the heights, so a block rebuilt from a complete cache is
the same every time.
"""
import logging
import math
import os
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image, ImageDraw

import fast
import mapgeom
import heightmap
import mapcells
import revisit
import roads as roadsmod
import staging

log = logging.getLogger("earthd.surface")

VERSION = 1
# (TerrainMaterial name, BeamNG ground model). The last one is the level's own dirt material: the
# default, and the one every block had before (an all-zero sample falls to the later layer on ties,
# docs/research/2026-10-10-world-data-sources.md §4.4, so DIRT goes last).
# Names carry their own prefix: "bngEarthSnow" is the weather's Precipitation object (weather.lua), and
# a TerrainMaterial of that name was never made - its layer fell back to asphalt (in-game 2026-10-11).
MATERIALS = (
    ("bngEarthGroundAsphalt", "ASPHALT_OLD"),
    ("bngEarthGroundGrass", "GRASS"),
    ("bngEarthGroundDusty", "DIRT_DUSTY"),
    ("bngEarthGroundLoose", "DIRT_DUSTY_LOOSE"),
    ("bngEarthGroundRock", "ROCK"),
    ("bngEarthGroundSnow", "SNOW"),
    ("bngEarthGroundMud", "MUD"),
    ("bngEarthGroundCobble", "COBBLESTONE"),      # roads with paving stones / setts (roads.py "cobble")
    ("bngEarthGroundLawn", "GRASS"),              # mown yards in towns (short grass only, groundcover.lua)
    ("beamngEarthDirt", "DIRT"),
)
ASPHALT, GRASS, DUSTY, LOOSE, ROCK, SNOW, MUD, COBBLE, LAWN, DIRT = range(len(MATERIALS))
# WorldCover class -> material (§5.4 of the world-data research). Built-up land is the plain dirt layer
# (2026-10-14): its yards, lawns and lots took the asphalt layer's grey detail and macro textures, which
# washed whole towns pale grey in the street photos (Moab, Frederick); the roads are painted asphalt anyway
# (paint_roads), and the photo's own colours now show through.
CLASS_MATERIAL = {0: DIRT, 10: GRASS, 20: DUSTY, 30: GRASS, 40: DIRT, 50: DIRT, 60: LOOSE, 70: SNOW,
                  80: MUD, 90: MUD, 95: MUD, 100: GRASS}
# Lawns (2026-10-14, street photos of Ocean City, Lancaster, Frederick, Moab): built-up land between the
# buildings is mostly mown grass, which the dirt layer drew pale and bare. A share of it is LAWN, in patches
# of smooth noise on a fixed world grid: most of it where the climate is humid, less in the dry West (yards
# of gravel and dirt there). The rest stays dirt (lots, gravel, bare yards); roads and lots paint over both.
LAWN_SHARE_HUMID = 0.9
LAWN_SHARE_DRY = 0.75
LAWN_PATCH_M = 30.0
# Past Web Mercator's limit (+-85.05 deg) there are no map cells: all of it is ice, Antarctica's ice sheet and the
# Arctic Ocean's year-round sea ice (the user, 2026-10-22: "add polar support"), WorldCover's "snow and ice".
POLAR_CLASS = 70
ROCK_SLOPE_DEG = 35.0       # steeper ground is rock (cliffs, scree), except snow
# Organic borders (user 2026-10-11: rectangular grass in Monument Valley). WorldCover's 10 m square
# pixels give stair-stepped, ruler-straight class borders, which the grass makes visible. Every
# sample reads the land cover a few metres off, by smooth value noise on a fixed world grid (no
# state, the same for every block): (feature size m, amplitude m) per octave.
WARP_OCTAVES = ((40.0, 8.0), (12.0, 3.0))
M_PER_DEG = 111320.0
PREFETCH_M = 3000.0         # land cover is fetched this far around the player
PREFETCH_MOVE_M = 500.0     # ... again after the player moved this far

_LUT = np.full(256, DIRT, np.uint8)
for _c, _m in CLASS_MATERIAL.items():
    _LUT[_c] = _m


def dry_west(lat, lon):
    """The arid West (a coarse climate stand-in, vegetation.regional's): its towns have fewer lawns."""
    lat, lon = np.asarray(lat), np.asarray(lon)
    return (lon < -102.0) & (lat > 25.0) & (lat < 46.0) & ~((lon < -121.0) & (lat > 37.0))


def lawns(lat, lon):
    """True where built-up land is a lawn (LAWN_* above). A pure function of the world position."""
    lat = np.asarray(lat, np.float64)
    lon = np.asarray(lon, np.float64)
    xm = lon * M_PER_DEG * np.cos(np.radians(lat))
    ym = lat * M_PER_DEG
    n = 0.65 * _value_noise(xm / LAWN_PATCH_M, ym / LAWN_PATCH_M, 5) \
        + 0.35 * _value_noise(xm / (LAWN_PATCH_M / 3), ym / (LAWN_PATCH_M / 3), 6)
    share = np.where(dry_west(lat, lon), LAWN_SHARE_DRY, LAWN_SHARE_HUMID)
    # threshold near the noise sum's share quantile (see the test)
    return n > (0.5 - share) * 1.1


def classify(classes, u, square, lat=None, lon=None):
    """Material index per sample: the land-cover class's material, ROCK where the slope of the
    height grid u (metres, [row, col], `square` m apart) is steeper than ROCK_SLOPE_DEG (snow stays
    snow). With the samples' positions, built-up land is LAWN where lawns() says. Pure."""
    classes = np.asarray(classes, np.uint8)
    idx = _LUT[classes]
    if lat is not None and lon is not None:
        # (KyzoFox 1.5.4: lawns() only at the built-up samples, the only ones it can change: on rural ground
        # that is almost none of a block)
        urban = classes == 50
        if urban.any():
            idx = idx.copy()
            sub = idx[urban]
            sub[lawns(np.asarray(lat, np.float64)[urban], np.asarray(lon, np.float64)[urban])] = np.uint8(LAWN)
            idx[urban] = sub
    u = np.asarray(u, np.float64)
    if min(u.shape) < 2:            # no slope from a single row / column
        return idx.astype(np.uint8)
    gy, gx = np.gradient(u, square)
    steep = (gx * gx + gy * gy) > math.tan(math.radians(ROCK_SLOPE_DEG)) ** 2
    return np.where(steep & (idx != SNOW), np.uint8(ROCK), idx).astype(np.uint8)


def _hash01(ix, iy, seed):
    """Deterministic pseudo-random in [-1, 1] per integer lattice point (uint64 arithmetic)."""
    h = (ix.astype(np.int64).astype(np.uint64) * np.uint64(0x9E3779B185EBCA87)
         ^ iy.astype(np.int64).astype(np.uint64) * np.uint64(0xC2B2AE3D27D4EB4F)
         ^ np.uint64(seed) * np.uint64(0x165667B19E3779F9))
    h ^= h >> np.uint64(29)
    h *= np.uint64(0xBF58476D1CE4E5B9)
    h ^= h >> np.uint64(32)
    return (h >> np.uint64(11)).astype(np.float64) / float(1 << 53) * 2.0 - 1.0


def _value_noise(x, y, seed):
    """Smooth value noise in [-1, 1] at points (x, y) given in lattice units."""
    return _value_noises(x, y, (seed,))[0]


def _value_noises(x, y, seeds):
    """_value_noise(x, y, seed) for each of seeds, the lattice work done once (warp's dx and dy share their points).
    KyzoFox 1.5.4: the four corner hashes come from a table of the lattice points the samples touch. A 1 km
    block's million samples sit in a few thousand lattice cells (12-40 m features, 2 m samples), and hashing every
    corner of every sample was most of the terrain block's "landcover" time (warp + lawns: 6 noises). The same values
    exactly (the same hash and arithmetic, looked up)."""
    x0 = np.floor(x)
    y0 = np.floor(y)
    fx = x - x0
    fy = y - y0
    fx = fx * fx * (3.0 - 2.0 * fx)
    fy = fy * fy * (3.0 - 2.0 * fy)
    ix = x0.astype(np.int64)
    iy = y0.astype(np.int64)
    table = None
    if ix.size >= NOISE_TABLE_MIN:
        bx, by = int(ix.min()), int(iy.min())
        w, h = int(ix.max()) - bx + 2, int(iy.max()) - by + 2
        if w * h * NOISE_TABLE_SHARE <= ix.size:
            gy, gx = np.mgrid[by:by + h, bx:bx + w]
            k = (iy - by) * w + (ix - bx)
            table = (gx, gy, k, w)
    out = []
    for seed in seeds:
        if table is not None:
            gx, gy, k, w = table
            tab = _hash01(gx, gy, seed).ravel()
            a, b, c, d = tab[k], tab[k + 1], tab[k + w], tab[k + w + 1]
        else:
            a = _hash01(ix, iy, seed)
            b = _hash01(ix + 1, iy, seed)
            c = _hash01(ix, iy + 1, seed)
            d = _hash01(ix + 1, iy + 1, seed)
        top = b - a
        top *= fx
        top += a                        # a + (b - a) * fx
        bot = d - c
        bot *= fx
        bot += c                        # c + (d - c) * fx
        bot -= top
        bot *= fy
        bot += top                      # top + (bot - top) * fy
        out.append(bot)
    return out


NOISE_TABLE_MIN = 4096      # samples from which _value_noise hashes a lattice table ...
NOISE_TABLE_SHARE = 4       # ... when the table has at most a quarter as many points as there are samples


def warp(lat, lon):
    """(lat, lon) moved by a few metres of smooth noise (WARP_OCTAVES): where each sample reads the
    land cover, so class borders follow organic curves instead of the 10 m pixel grid. A pure
    function of the world position (blocks agree along their shared samples). Pure."""
    lat = np.asarray(lat, np.float64)
    lon = np.asarray(lon, np.float64)
    m_lon = M_PER_DEG * np.cos(np.radians(lat))
    xm = lon * m_lon                     # metres east of the lon 0 meridian (at this latitude)
    ym = lat * M_PER_DEG                 # metres north of the equator
    dx = np.zeros_like(lat)
    dy = np.zeros_like(lat)
    for k, (size, amp) in enumerate(WARP_OCTAVES):
        nx, ny = _value_noises(xm / size, ym / size, (2 * k + 1, 2 * k + 2))
        dx += amp * nx
        dy += amp * ny
    return lat + dy / M_PER_DEG, lon + dx / np.maximum(m_lon, 1e-6)


# road surface kind (roads.py) -> material; drawn in this order (paved last: it wins at junctions)
ROAD_MATERIAL = {"dirt": DIRT, "gravel": LOOSE, "cobble": COBBLE, "paved": ASPHALT,
                 "rock": ROCK, "grass": GRASS}     # rock and grass roads: the ground's own materials (roads.terrain_surface)
ROAD_ORDER = ("dirt", "gravel", "rock", "grass", "cobble", "paved")


def grid_affine(lat, lon):
    """Least-squares affine map (lon, lat) -> (col, row) of a sample grid given by its lat / lon arrays
    (a 1 km block is affine to well under a centimetre). Returns a function (lon, lat) -> (col, row)."""
    lat = np.asarray(lat, np.float64)
    lon = np.asarray(lon, np.float64)
    h, w = lat.shape
    step = max(1, min(h, w) // 16)
    rr, cc = np.mgrid[0:h:step, 0:w:step]
    la, lo = lat[rr, cc].ravel(), lon[rr, cc].ravel()
    lo0, la0 = float(lo.mean()), float(la.mean())
    A = np.stack([lo - lo0, la - la0, np.ones_like(lo)], axis=1)
    coef, *_ = np.linalg.lstsq(A, np.stack([cc.ravel(), rr.ravel()], axis=1).astype(np.float64), rcond=None)

    def f(lon_, lat_):
        B = np.stack([np.asarray(lon_, np.float64) - lo0, np.asarray(lat_, np.float64) - la0,
                      np.ones(np.shape(lon_))], axis=-1)
        cr = B @ coef
        return cr[..., 0], cr[..., 1]
    return f


def _lonlat_boxes(feats):
    """(n, 4) lon min / max, lat min / max of each feature's coordinates (+-inf for none), kept with the feats."""
    got = feats.__dict__.get("_lonlat_boxes")
    if got is not None:
        return got
    n = len(feats)
    got = np.empty((n, 4))
    got[:, 0::2], got[:, 1::2] = np.inf, -np.inf
    a = feats.coord_off[feats.ring_off[feats.part_off[:-1]]]
    b = feats.coord_off[feats.ring_off[feats.part_off[1:]]]
    rows = np.flatnonzero(b > a)
    if len(rows):
        ll = feats.lonlat
        for col, (u, f) in enumerate([(0, np.minimum), (0, np.maximum), (1, np.minimum), (1, np.maximum)]):
            got[rows, col] = mapgeom._reduce(f, ll[:, u], a[rows], b[rows])
    feats.__dict__["_lonlat_boxes"] = got
    return got


def paint_roads(idx, lat, lon, feats_list, square, urban_fn=None, widths=None, extra=()):
    """idx (material per sample, [row, col], as classify gives it) with the roads of feats_list (the
    "roads" Features of the cells around) drawn in at their width: ROAD_MATERIAL by surface kind.
    lat / lon: the samples' positions; square: m between samples; urban_fn: as roads.road_specs (the
    same widths as the drawn roads); extra: [(lat (k,), lon (k,), roads.Spec)] more roads (a tunnel's open
    sections, grading.Grader.open_tunnels). Pure (a new array)."""
    h, w = idx.shape
    to_px = grid_affine(lat, lon)
    layers = {k: Image.new("L", (w, h), 0) for k in ROAD_ORDER}
    draws = {k: ImageDraw.Draw(im) for k, im in layers.items()}
    lawn = Image.new("L", (w, h), 0)            # divided roads' medians (under the roads: they paint over it)
    lawn_draw = ImageDraw.Draw(lawn)
    verge = Image.new("L", (w, h), 0)           # a bare strip beside roads without sidewalks (VERGE_M)
    verge_draw = ImageDraw.Draw(verge)
    seen = set()

    def line(x, y, spec, aisle):
        width, kind = spec.width, roadsmod.surface_material_kind(spec)
        if aisle:
            width += 2 * AISLE_STALLS_M         # the stalls either side (parking.aisle_spots): no grass under cars
        walks = roadsmod.has_sidewalks(spec)
        if walks:
            width += 2 * (spec.walk + square)   # the sidewalks (+ a sample: tufts at the outer edge): grass grew up
                                                # through them (ground cover is the terrain's: Lauterbrunnen, Baltimore
                                                # 2026-10-15)
        # rounded up to an odd number of samples: a 9 m road was painted 8 m wide (round(4.5) = 4), and an even width
        # sits half a sample off the line (PIL), its one edge a metre short
        px = max(1, int(math.ceil(width / square - 1e-9)))
        px += 1 - px % 2
        # to a millionth of a sample: PIL floors, and a line on a sample row came out 63.99999999996 on Windows (its
        # libm, the affine fit), drawn a sample north of where it lands on Linux
        c, r = (np.round(v, 6) for v in to_px(x, y))
        vx = 0 if walks else 2 * int(math.ceil(VERGE_M / square))     # (even: the verge stays odd, centred)
        if c.max() < -px - vx or c.min() > w + px + vx or r.max() < -px - vx or r.min() > h + px + vx:
            return
        pts = list(zip(c.tolist(), r.tolist()))
        draws[kind].line(pts, fill=255, width=px, joint="curve")
        if vx:
            verge_draw.line(pts, fill=255, width=px + vx, joint="curve")

    for feats in feats_list:
        if feats is None or len(feats) == 0:
            continue
        specs = roadsmod.road_specs(feats, urban_fn, widths)
        box = _lonlat_boxes(feats)
        bc, br = to_px(box[:, [0, 1, 0, 1]], box[:, [2, 2, 3, 3]])     # the boxes' corners on the grid
        c_lo, c_hi, r_lo, r_hi = bc.min(1), bc.max(1), br.min(1), br.max(1)
        ids = feats.ids.tolist()
        for i in range(len(feats)):
            fid = str(ids[i])
            if specs[i] is None or fid in seen:
                continue
            seen.add(fid)
            # (all of it off the grid, with room for everything drawn beside it: no sample of it is painted)
            m = (specs[i].width + 2 * AISLE_STALLS_M + 2 * (specs[i].walk + square) + 2 * VERGE_M
                 + 3 * roadsmod.median_width(specs[i])) / square + 4
            if not (c_hi[i] >= -m and c_lo[i] <= w + m and r_hi[i] >= -m and r_lo[i] <= h + m):
                continue
            aisle = specs[i].kind == "paved" and \
                roadsmod._rule_value(feats.rules.get("subclass"), i) == "parking_aisle"
            mw = roadsmod.median_width(specs[i])
            ms = roadsmod.median_side(specs[i])
            for ring in roadsmod.ground_parts(feats, i):      # not bridges / tunnels
                line(ring[:, 0], ring[:, 1], specs[i], aisle)
                if ms is not None and mw > roadsmod.MEDIAN_SLAB_M and len(ring) >= 2:
                    # the median's ground: lawn (report #48, Boston Street, 2026-10-17)
                    sc = np.array([111320.0 * math.cos(math.radians(float(ring[0, 1]))), 110540.0])
                    xy = (ring[:, :2] - ring[0, :2]) * sc
                    off = roadsmod.offset_polyline(np.c_[xy, np.zeros(len(xy))],
                                                   ms * (specs[i].width / 2 + mw / 2))[:, :2] / sc + ring[0, :2]
                    c, r = to_px(off[:, 0], off[:, 1])
                    lawn_draw.line(list(zip(c.tolist(), r.tolist())), fill=255,
                                   width=max(1, int(round(mw / square))), joint="curve")
    for la, lo, spec in extra:
        line(np.asarray(lo), np.asarray(la), spec, False)
    out = idx.copy()
    out[np.asarray(lawn) > 0] = LAWN
    v = np.asarray(verge) > 0
    out[v & np.isin(out, VERGE_FROM)] = VERGE
    for k in ROAD_ORDER:
        m = np.asarray(layers[k]) > 0
        out[m] = ROAD_MATERIAL[k]
    return out


# Roads in the ground (the user, 2026-10-22: "I dont love that we are applying a floating road texture over the normal
# terrain"): by default (BNGE_ROADS_IN_GROUND=0 turns it off) the road tiles carry no carriageway mesh (roads.ROADS_IN_GROUND) and the
# ground's photos have the carriageways painted in at their width (photo_roads): the drawn block's base photo and the
# far tiles' textures down to PHOTO_ROADS_MAX_TEXEL (the near ring the block does not cover, where the meshes were);
# the asphalt / cobble terrain layer (paint_roads) gives them their grain up close. Markings, curbs and sidewalks stay
# meshes.
# colour and opacity by surface, blended in this order (paved last: it wins at junctions); a trace of the photo is kept
# (stains, patches), and most of it on unpaved tracks, which the photo shows well and a flat colour drew as stripes
PHOTO_ROAD_RGBA = {"dirt": (122, 104, 82, 0.45), "gravel": (128, 118, 102, 0.6), "cobble": (112, 104, 96, 0.85),
                   "paved": (95, 95, 94, 0.92)}
PHOTO_ROADS_MAX_TEXEL = 2.0     # m per texel: coarser far tiles keep the photo's own roads (far, under a pixel wide)


def _road_segments(frame, x0, y0, size, w, h, feats_list, urban_fn=None, widths=None):
    """(segs (m, 4), half widths (m,), kind index into PHOTO_ROAD_RGBA (m,)) of the carriageways of feats_list in pixel
    coordinates of a w x h photo over [x0, x0 + size] x [y0, y0 + size] (row 0 north), or None when none is in it."""
    px_m = w / size
    kinds = list(PHOTO_ROAD_RGBA)
    segs, hws, kind = [], [], []
    seen = set()
    for feats in feats_list:
        if feats is None or len(feats) == 0:
            continue
        specs = roadsmod.road_specs(feats, urban_fn, widths)
        box = _feature_boxes(feats, frame)
        ids = feats.ids.tolist()
        for i in range(len(feats)):
            fid = str(ids[i])
            sp = specs[i]
            if sp is None or fid in seen or sp.kind not in PHOTO_ROAD_RGBA:
                continue
            seen.add(fid)
            m = sp.width / 2        # (all of the feature off the photo: none of its rings is in it)
            if box[i, 1] < x0 - m or box[i, 0] > x0 + size + m or box[i, 3] < y0 - m or box[i, 2] > y0 + size + m:
                continue
            hw = sp.width * px_m / 2
            for ring in roadsmod.ground_parts(feats, i):
                e, n = mapgeom.enu_xy(feats, frame, ring)
                c = (np.asarray(e) - x0) * px_m
                r = (y0 + size - np.asarray(n)) * px_m
                if len(c) < 2 or c.max() < -hw or c.min() > w + hw or r.max() < -hw or r.min() > h + hw:
                    continue
                segs.append(np.stack([c[:-1], r[:-1], c[1:], r[1:]], axis=1))
                hws.append(np.full(len(c) - 1, hw))
                kind.append(np.full(len(c) - 1, kinds.index(sp.kind), np.uint8))
    if not segs:
        return None
    return np.concatenate(segs), np.concatenate(hws), np.concatenate(kind)


def _feature_boxes(feats, frame):
    """(n, 4) east min / max, north min / max of each feature's coordinates in frame (+-inf for none); kept with the
    feats per frame, as their converted coordinates (mapgeom.Features.xy_in)."""
    key = (frame.lat0, frame.lon0, frame.h0)
    memo = feats.__dict__.setdefault("_boxes_in", {})
    got = memo.get(key)
    if got is not None:
        return got
    n = len(feats)
    got = np.empty((n, 4))
    got[:, 0::2], got[:, 1::2] = np.inf, -np.inf
    a = feats.coord_off[feats.ring_off[feats.part_off[:-1]]]
    b = feats.coord_off[feats.ring_off[feats.part_off[1:]]]
    rows = np.flatnonzero(b > a)
    if len(rows):
        xy = feats.xy_in(frame)
        for col, (u, f) in enumerate([(0, np.minimum), (0, np.maximum), (1, np.minimum), (1, np.maximum)]):
            got[rows, col] = mapgeom._reduce(f, xy[:, u], a[rows], b[rows])
    if len(memo) >= 2:
        memo.pop(next(iter(memo)))
    memo[key] = got
    return got


def photo_roads(rgb, frame, x0, y0, size, feats_list, urban_fn=None, widths=None):
    """The photo rgb (H x W x 3, row 0 = north, covering [x0, x0 + size] x [y0, y0 + size] of frame) with the
    carriageways of feats_list (road Features: roads.road_specs widths, ground parts only) painted in, antialiased
    (earthfast.paint_lines, PHOTO_ROAD_RGBA). Pure (a new array)."""
    fast.require(49, "roads in the ground")
    h, w = rgb.shape[:2]
    got = _road_segments(frame, x0, y0, size, w, h, feats_list, urban_fn, widths)
    if got is None:
        return rgb
    colors = np.array([PHOTO_ROAD_RGBA[k] for k in PHOTO_ROAD_RGBA], np.float32)
    return fast._ef.paint_lines(np.ascontiguousarray(rgb, np.uint8), *got, colors)


def _square_roads(frame, x0, y0, size, surface):
    """The road Features of surface's cached cells around [x0, x0 + size]² of frame (never waits)."""
    gx = x0 + np.array([0.0, size, 0.0, size, size / 2])
    gy = y0 + np.array([0.0, 0.0, size, size, size / 2])
    la, lo, _ = frame.enu_to_geodetic(gx, gy, np.zeros(5))
    feats, _ = surface.roads(np.asarray(la), np.asarray(lo))
    return feats


def paint_square(rgb, frame, x0, y0, size, surface, widths=None):
    """photo_roads for a ground photo over [x0, x0 + size]² of frame with the roads of surface's cached cells (a
    Surface; never waits); as it was when its texels are coarser than PHOTO_ROADS_MAX_TEXEL or no cell is cached."""
    if surface is None or size / max(rgb.shape[1], 1) > PHOTO_ROADS_MAX_TEXEL:
        return rgb
    rgb = photo_pads(rgb, frame, x0, y0, size, _square_pads(frame, x0, y0, size, surface))     # (under the roads)
    feats = _square_roads(frame, x0, y0, size, surface)
    if not feats:
        return rgb
    return photo_roads(rgb, frame, x0, y0, size, feats, surface.urban, widths)


def _square_pads(frame, x0, y0, size, surface):
    """The gas station pads (Surface.fuel_pads) of the cells around [x0, x0 + size]² of frame (never waits)."""
    if not getattr(surface, "gas_pads", False) and getattr(surface, "extra_pads", None) is None:
        return []
    gx = x0 + np.array([0.0, size, 0.0, size, size / 2])
    gy = y0 + np.array([0.0, 0.0, size, size, size / 2])
    la, lo, _ = frame.enu_to_geodetic(gx, gy, np.zeros(5))
    return surface.fuel_pads(np.asarray(la), np.asarray(lo))


# The near tiles' asphalt (mesh near-visual mode, where the tiles are the ground seen): a mask of the built
# carriageways (paved, cobble) beside the tile's texture, far_<a>_<key>_road.dds (road_mask_name), finer than the photo
# (farfield.NEAR_MASK_PX; the GPU has room, the CPU less, the user 2026-10-22); the mod's material gives the masked
# part a second layer of the stock asphalt (its colour, grain and bumps, world-locked; satmat.lua), the rest the photo.
ROAD_MASK_MAX_TEXEL = 1.0       # m per texel of the mask
ROAD_MASK_KINDS = ("paved", "cobble")


def road_mask_name(texture):
    """The road mask file beside a far tile's texture (far_<a>_<key>[_r<n>].dds -> far_<a>_<key>_road.dds)."""
    import re
    return re.sub(r"(_r\d+)?\.dds$", "_road.dds", texture)


def road_mask(frame, x0, y0, size, px, surface, widths=None):
    """The built carriageways' coverage (px x px uint8, 255 on the road, antialiased) of a tile photo's square
    [x0, x0 + size]² (its gutter included: the mask lines up with it on UV0), or None (texels coarser than
    ROAD_MASK_MAX_TEXEL, no roads, no cell cached)."""
    if surface is None or size / max(px, 1) > ROAD_MASK_MAX_TEXEL:
        return None
    pads = pads_mask(frame, x0, y0, size, px, _square_pads(frame, x0, y0, size, surface))
    feats = _square_roads(frame, x0, y0, size, surface)
    got = _road_segments(frame, x0, y0, size, px, px, feats, surface.urban, widths) if feats else None
    if got is None:
        return pads
    colors = np.array([(255.0, 255.0, 255.0, 1.0 if k in ROAD_MASK_KINDS else 0.0) for k in PHOTO_ROAD_RGBA],
                      np.float32)
    out = fast._ef.paint_lines(np.zeros((px, px, 3), np.uint8), *got, colors)[..., 0]
    if pads is not None:
        out = np.maximum(out, pads)
    return out if out.any() else None


# A road without sidewalks gets a verge of VERGE_M a side painted bare where the ground would carry grass or shrubs:
# the ground cover goes by the 2 m layer samples, and tufts stood on the asphalt along its edges in many places (the
# user, 2026-10-20: "grass on the road")
VERGE_M = 2.0
VERGE_FROM = (GRASS, LAWN, DUSTY)
VERGE = DIRT
AISLE_STALLS_M = 5.5        # a parking aisle's stall rows (parking.LOT_SPOT_D_M), painted with it
PARKING_CLASSES = {"parking"}     # Overture infrastructure (transit) polygons drawn as asphalt: no grass on a lot
# Squares and pedestrian areas (Overture land_use subtype "pedestrian": Mexico City's Zocalo read as bare dirt,
# 2026-10-15): paving, under the parking lots and the roads drawn over them
PLAZA_CLASSES = {"pedestrian", "plaza"}
# Substations' yards (Overture infrastructure class substation; powerlines.py fills them with switchgear): crushed
# stone, as West Coast USA's yard (the gravel layer), 2026-10-26
YARD_CLASSES = {"substation"}


def paint_lots(idx, lat, lon, feats_list):
    """idx with the squares and pedestrian areas (land_use polygons of subtype "pedestrian") of feats_list
    drawn in as plain paving (the asphalt layer), then the parking lots (polygons of class "parking") as asphalt (Lancaster County's
    photos: the grass layer's ground cover grew on lots), the substations' yards as gravel. Pure."""
    # (grey paving: the cobble layer's setts drew big squares and promenades pink, Harbor Point 2026-10-15)
    idx = _paint(idx, lat, lon, feats_list, _plazas, ASPHALT)
    idx = _paint(idx, lat, lon, feats_list, _yards, LOOSE)
    return _paint(idx, lat, lon, feats_list, _parking, ASPHALT)


# Gas station forecourts (gasstations.forecourt_pad: Surface.fuel_pads, from the road tiles), asphalt in the ground
# models, the photo (the paved roads' colour, under the roads) and the near tiles' asphalt mask
FUEL_PAD_STATIONS = 4096        # stations' pads kept (a few small rings each)


def paint_pads(idx, lat, lon, pads):
    """idx with the pads [(lat (k,), lon (k,))] drawn in as asphalt. Pure."""
    if not pads:
        return idx
    h, w = idx.shape
    to_px = grid_affine(lat, lon)
    im = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(im)
    for la, lo in pads:
        c, r = to_px(np.asarray(lo), np.asarray(la))
        if c.max() < 0 or c.min() > w or r.max() < 0 or r.min() > h:
            continue
        draw.polygon(list(zip(c.tolist(), r.tolist())), fill=255)
    out = idx.copy()
    out[np.asarray(im) > 0] = ASPHALT
    return out


def _pads_px(frame, x0, y0, size, w, h, pads):
    """A w x h coverage (float 0..1, 2x supersampled edges) of the pads in a photo over [x0, x0 + size]² of frame
    (row 0 north), or None when none is in it. Drawn only over the pads' bounding box (most tiles have none: no image
    is made for them)."""
    if not pads:
        return None
    k = 2
    px_m = w * k / size
    polys = []
    for la, lo in pads:
        e, n, _ = frame.geodetic_to_enu(np.asarray(la), np.asarray(lo), np.zeros(len(la)))
        c = (np.asarray(e) - x0) * px_m
        r = (y0 + size - np.asarray(n)) * px_m
        if c.max() < 0 or c.min() > w * k or r.max() < 0 or r.min() > h * k:
            continue
        polys.append((c, r))
    if not polys:
        return None
    c0 = max(0, int(min(c.min() for c, _ in polys)) // k * k)
    r0 = max(0, int(min(r.min() for _, r in polys)) // k * k)
    c1 = min(w * k, -(-int(max(c.max() for c, _ in polys) + 1) // k) * k)
    r1 = min(h * k, -(-int(max(r.max() for _, r in polys) + 1) // k) * k)
    if c1 <= c0 or r1 <= r0:
        return None
    im = Image.new("L", (c1 - c0, r1 - r0), 0)
    draw = ImageDraw.Draw(im)
    for c, r in polys:
        draw.polygon(list(zip((c - c0).tolist(), (r - r0).tolist())), fill=255)
    sub = np.asarray(im, np.float32).reshape((r1 - r0) // k, k, (c1 - c0) // k, k).mean(axis=(1, 3)) / 255.0
    out = np.zeros((h, w), np.float32)
    out[r0 // k:r1 // k, c0 // k:c1 // k] = sub
    return out


def photo_pads(rgb, frame, x0, y0, size, pads):
    """The photo rgb (H x W x 3, row 0 north, over [x0, x0 + size]² of frame) with the pads painted in the paved
    roads' colour (PHOTO_ROAD_RGBA). Pure."""
    h, w = rgb.shape[:2]
    a = _pads_px(frame, x0, y0, size, w, h, pads)
    if a is None:
        return rgb
    rows, cols = np.flatnonzero(a.any(axis=1)), np.flatnonzero(a.any(axis=0))
    if not len(rows):
        return rgb
    r0, r1, c0, c1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1     # (only the pads' box blended)
    r, g, b, op = PHOTO_ROAD_RGBA["paved"]
    sub = (a[r0:r1, c0:c1] * op)[..., None]
    out = rgb.copy()
    blend = rgb[r0:r1, c0:c1].astype(np.float32) * (1.0 - sub) + np.array([r, g, b], np.float32) * sub
    out[r0:r1, c0:c1] = np.clip(np.round(blend), 0, 255).astype(np.uint8)
    return out


def pads_mask(frame, x0, y0, size, px, pads):
    """The pads' coverage (px x px uint8, 255 inside) of a tile photo's square, or None (none in it)."""
    a = _pads_px(frame, x0, y0, size, px, px, pads)
    return None if a is None else np.round(a * 255.0).astype(np.uint8)


def _parking(feats):
    cls = feats.attrs.get("class")
    return None if cls is None else np.isin(cls.astype(str), list(PARKING_CLASSES))


def _yards(feats):
    cls = feats.attrs.get("class")
    return None if cls is None else np.isin(cls.astype(str), list(YARD_CLASSES))


def _plazas(feats):
    cls, sub = feats.attrs.get("class"), feats.attrs.get("subtype")
    if cls is None or sub is None:
        return None
    return (sub.astype(str) == "pedestrian") & np.isin(cls.astype(str), list(PLAZA_CLASSES))


def _paint(idx, lat, lon, feats_list, select, material):
    h, w = idx.shape
    to_px = grid_affine(lat, lon)
    im = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(im)
    n = 0
    for feats in feats_list:
        if feats is None or len(feats) == 0:
            continue
        sel = select(feats)
        if sel is None:
            continue
        for i in np.flatnonzero(sel & (feats.kind == 3)):
            for part in feats.parts(i):
                if not part:
                    continue
                ring = np.asarray(part[0], np.float64)
                c, r = to_px(ring[:, 0], ring[:, 1])
                if c.max() < 0 or c.min() > w or r.max() < 0 or r.min() > h:
                    continue
                draw.polygon(list(zip(c.tolist(), r.tolist())), fill=255)
                n += 1
    if n == 0:
        return idx
    out = idx.copy()
    out[np.asarray(im) > 0] = material
    return out


def mask_names(stem):
    return [f"{stem}_m{i}.png" for i in range(len(MATERIALS))]


def write_masks(idx, out_dir, stem):
    """One 0/255 greyscale PNG per material (rows as the heightmap's: row 0 = north), written
    atomically. Returns the file names (in out_dir), in MATERIALS order."""
    names = mask_names(stem)
    for i, name in enumerate(names):
        path = os.path.join(out_dir, name)
        tmp = staging.tmp_for(path)
        Image.fromarray(np.where(idx == i, 255, 0).astype(np.uint8), "L").save(tmp, format="PNG", compress_level=1)
        staging.replace(tmp, path)
    return names


SNOW_BRIGHT = 165           # mean of r, g, b of the ground photo (0-255) from which a snow candidate stays snow, ...
SNOW_GREY = 40              # ... with at most this spread between its channels (white, not pale scree)
LAWN_GREEN = 10             # 2g - r - b of the ground photo above which a lawn candidate stays a lawn


def photo_lawns(out_dir, ter_name, mask_names_, rgb):
    """Detail mode: the block's LAWN samples where the ground photo (rgb (px, px, 3), row 0 = north, the
    block's footprint) is not green turn DIRT (paved yards, lots and driveways: the grass layer's blades
    over a pale photo looked like snow, Lancaster 2026-10-14). Patches the .ter's layer map and the two
    masks in place, atomically. Returns the number of samples changed. Likewise SNOW (WorldCover "snow and ice")
    where the photo is not snow-white turns LOOSE scree (the Stelvio Pass's summit in late summer: the snow layer's
    white blur where the orthophoto has grey-brown gravel, the user's reports #40 / #44, 2026-10-17)."""
    path = os.path.join(out_dir, ter_name)
    with open(path, "rb") as f:
        data = bytearray(f.read())
    n = struct.unpack_from("<I", data, 1)[0]
    off = 5 + 2 * n * n
    lay = np.frombuffer(bytes(data[off:off + n * n]), np.uint8).reshape(n, n)       # row 0 = south
    # the .ter's layer indices are into its own material list (write_ter: only the used ones): back to MATERIALS
    o = off + n * n + 4
    names = []
    for _ in range(struct.unpack_from("<I", data, off + n * n)[0]):
        names.append(bytes(data[o + 1:o + 1 + data[o]]).decode())
        o += 1 + data[o]
    order = [m for m, _ in MATERIALS]
    glob = [order.index(m) if m in order else DIRT for m in names]
    to_glob = np.full(256, heightmap.TER_HOLE, np.uint8)
    to_glob[:len(glob)] = glob
    to_local = np.full(256, heightmap.TER_HOLE, np.uint8)
    for li, gi in enumerate(glob):
        to_local[gi] = li
    lay = to_glob[lay]
    lawn = lay == LAWN
    snow = lay == SNOW
    if not lawn.any() and not snow.any():
        return 0
    px = rgb.shape[0]
    rows = np.clip(((n - 1 - np.arange(n)) + 0.5) * px / n, 0, px - 1).astype(np.int64)
    cols = np.clip((np.arange(n) + 0.5) * px / n, 0, px - 1).astype(np.int64)
    c = rgb.astype(np.int32)[rows[:, None], cols[None, :]]
    exg = 2 * c[:, :, 1] - c[:, :, 0] - c[:, :, 2]
    green = exg > LAWN_GREEN
    white = (c.mean(2) >= SNOW_BRIGHT) & (c.max(2) - c.min(2) <= SNOW_GREY)
    bare = lawn & ~green
    scree = snow & ~white
    if to_local[DIRT] == heightmap.TER_HOLE:
        bare[:] = False
    if to_local[LOOSE] == heightmap.TER_HOLE:
        scree[:] = False
    k = int(bare.sum() + scree.sum())
    if not k:
        return 0
    lay = lay.copy()
    lay[bare] = DIRT
    lay[scree] = LOOSE
    data[off:off + n * n] = to_local[lay].tobytes()
    tmp = staging.tmp_for(path)
    with open(tmp, "wb") as f:
        f.write(data)
    staging.replace(tmp, path)
    north = lay[::-1]
    for i in (LAWN, DIRT, SNOW, LOOSE):
        p = os.path.join(out_dir, mask_names_[i])
        tmp = staging.tmp_for(p)
        Image.fromarray(np.where(north == i, 255, 0).astype(np.uint8), "L").save(tmp, format="PNG", compress_level=1)
        staging.replace(tmp, p)
    return k


def used_materials(idx):
    """The MATERIALS indices a block's .ter lists: those its samples use (holes aside), plus what photo_lawns may
    turn them into (LAWN -> DIRT, SNOW -> LOOSE), in MATERIALS order. Each distinct material a .ter lists costs
    the engine's first draw after the swap ~20 ms (in-game probes 2026-10-18 at Tower Bridge: one material
    55-80 ms, three 80-180, all ten 250-560 ms in RenderShadows, whether a sample uses them or not)."""
    u = set(np.unique(np.asarray(idx, np.uint8)).tolist())
    u.discard(heightmap.TER_HOLE)
    if LAWN in u:
        u.add(DIRT)
    if SNOW in u:
        u.add(LOOSE)
    return sorted(u) or [DIRT]


def write_ter(out_dir, name, qgrid, idx):
    """The block's .ter (heightmap.write_ter) with the materials its samples use (idx: MATERIALS indices, row
    0 = south; used_materials) or, idx None, the level's single dirt layer (the mod's terrain.MATERIAL).
    Returns (name, the MATERIALS indices listed in order, None for the single dirt layer)."""
    if idx is None:
        heightmap.write_ter(os.path.join(out_dir, name), qgrid, None, [MATERIALS[DIRT][0]])
        return name, None
    used = used_materials(idx)
    lut = np.full(256, heightmap.TER_HOLE, np.uint8)
    lut[used] = np.arange(len(used), dtype=np.uint8)
    heightmap.write_ter(os.path.join(out_dir, name), qgrid, lut[np.asarray(idx, np.uint8)],
                        [MATERIALS[i][0] for i in used])
    return name, used


class Surface:
    """Land cover for the blocks, from the map-data cache, never waiting for the network."""

    def __init__(self, mapdata):
        self.md = mapdata
        self.pool = ThreadPoolExecutor(1, thread_name_prefix="surface-prefetch")
        self._lock = threading.Lock()
        self._pending = set()
        self._pending_layers = set()    # (key, layers) queued
        self._last = None           # (lat, lon) of the last prefetch
        self.gas_pads = False       # gas station forecourts painted asphalt (earthd --gas-stations on: fuel_pads)
        self.extra_pads = None      # (lat, lon) -> more pads painted the same (airportgrade's runway_pads)
        self._fuel_pads = {}        # station id -> (rings [(lat (k,), lon (k,))], bbox), the last FUEL_PAD_STATIONS

    def close(self):
        self.pool.shutdown(wait=False)

    def _cached(self, key):
        """The cell's land-cover Raster from the cache, None if absent (a fetch is queued) or no
        coverage (open ocean: the layer is stored as missing)."""
        if not self.md.has(key, "landcover"):
            self._queue(key)
            return None
        try:        # (mapdata's memory LRU: a far tile's road paint and every block read the same cells again)
            return self.md.cell(key, layers=("landcover",), kind="background").layers.get("landcover")
        except Exception as e:      # an unreadable cache file: like a missing one
            log.debug("surface: land cover of %s: %s: %s", key, type(e).__name__, e)
            return None

    def _queue(self, key, layers=("landcover",)):
        with self._lock:
            if (key, layers) in self._pending_layers:
                return
            self._pending_layers.add((key, layers))
            self._pending.add(key)

        def run():
            try:
                self.md.prefetch(key, layers)
            except Exception as e:      # network trouble: asked again by the next block / prefetch
                log.debug("surface: land cover of %s: %s: %s", key, type(e).__name__, e)
            finally:
                with self._lock:
                    self._pending_layers.discard((key, layers))
                    if not any(k == key for k, _ in self._pending_layers):
                        self._pending.discard(key)
        self.pool.submit(run)

    def prefetch(self, lat, lon, radius_m=PREFETCH_M):
        """Queue the land cover of the cells within radius_m (at most every PREFETCH_MOVE_M)."""
        if self._last is not None:
            dlat = (lat - self._last[0]) * 111320.0
            dlon = (lon - self._last[1]) * 111320.0 * math.cos(math.radians(lat))
            if dlat * dlat + dlon * dlon < PREFETCH_MOVE_M * PREFETCH_MOVE_M:
                return
        self._last = (lat, lon)
        dlat = radius_m / 111320.0
        dlon = radius_m / (111320.0 * max(0.05, math.cos(math.radians(lat))))
        # a file test, not a load: this runs on the server's event loop at every pose, and reading each
        # cell's arrays there stalled it for seconds while driving (2026-10-12); an unreadable file is
        # rebuilt when a block reads it
        for key in mapcells.cells_for_bbox(lat - dlat, lon - dlon, lat + dlat, lon + dlon):
            if not self.md.has(key, "landcover"):
                self._queue(key)
            if not self.md.has(key, "roads"):
                self._queue(key, ("roads",))

    def ready(self, lat, lon, radius_m=1000.0, timeout_s=3.0):
        """Fetch the land cover within radius_m now, and the squares and lots (land_use, infrastructure:
        paint_lots) too, and wait for them, at most timeout_s (a teleport's first block: built once and kept
        while the player stands still; Cluj's Piata Unirii stayed bare ground, its square not cached yet).
        Returns True if it all arrived. Worker threads only (it blocks)."""
        dlat = radius_m / 111320.0
        dlon = radius_m / (111320.0 * max(0.05, math.cos(math.radians(lat))))
        cells = mapcells.cells_for_bbox(lat - dlat, lon - dlon, lat + dlat, lon + dlon)
        keys = []
        for layer in ("landcover", "land_use", "infrastructure"):
            for k in cells:
                if not self.md.has(k, layer):
                    self._queue(k, (layer,))
                    keys.append(k)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not any(self._is_queued(k) for k in keys):
                return True
            time.sleep(0.05)
        return False

    def roads(self, lat, lon):
        """(the "roads" Features of the cells around the points that are cached, complete). Never waits:
        missing cells are fetched in the background."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        out, complete = [], True
        for key in mapcells.cells_for_points(lat, lon):
            if not self.md.has(key, "roads"):
                self._queue(key, ("roads",))
                complete = False
                revisit.fault()             # (left out: nothing built now is kept)
                continue
            try:
                out.append(self.md.cell(key, layers=("roads",), kind="background").layers.get("roads"))
            except Exception as e:      # an unreadable cache file: like a missing one
                log.debug("surface: roads of %s: %s: %s", key, type(e).__name__, e)
                complete = False
        return out, complete

    def add_fuel_pads(self, items):
        """Keep gas station forecourt pads [(station id, [(lat (k,), lon (k,))])]: the road tiles' own
        (gasstations.tile_sites pads), handed over by earthd (and to a block's process with its job). No other process
        lays the stations out again for them: that read every cell's buildings and places into the terrain blocks'
        processes too (~60 MB a city cell, out of a Steam Deck's map data budget; the user, 2026-10-23: "this is meant
        to run on steam decks too"). Returns the stations new to it [(id, rings)] (their ground to paint again)."""
        new = []
        with self._lock:
            for sid, rings in items:
                rings = [(np.asarray(la, np.float64), np.asarray(lo, np.float64)) for la, lo in rings]
                if not rings:
                    continue
                box = (min(float(la.min()) for la, _ in rings), min(float(lo.min()) for _, lo in rings),
                       max(float(la.max()) for la, _ in rings), max(float(lo.max()) for _, lo in rings))
                if self._fuel_pads.pop(sid, None) is None:
                    new.append((sid, rings))
                self._fuel_pads[sid] = (rings, box)
                while len(self._fuel_pads) > FUEL_PAD_STATIONS:
                    self._fuel_pads.pop(next(iter(self._fuel_pads)))
        return new

    def clear_fuel_pads(self):
        """Drop every kept pad (the server's gas stations switched off; a block's job brings its own)."""
        with self._lock:
            self._fuel_pads.clear()

    def fuel_pad_items(self, lat, lon):
        """[(station id, rings)] of the kept pads within the points' bounding box; [] when gas_pads is off."""
        if not self.gas_pads or not self._fuel_pads:
            return []
        lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
        la0, la1, lo0, lo1 = float(lat.min()), float(lat.max()), float(lon.min()), float(lon.max())
        with self._lock:
            return [(sid, rings) for sid, (rings, (a0, b0, a1, b1)) in self._fuel_pads.items()
                    if a1 >= la0 and a0 <= la1 and b1 >= lo0 and b0 <= lo1]

    def fuel_pads(self, lat, lon):
        """[(lat (k,), lon (k,))] of the kept pads within the points' bounding box (the painters'; never waits), and
        extra_pads' (the runways)."""
        out = [r for _, rings in self.fuel_pad_items(lat, lon) for r in rings]
        if self.extra_pads is not None:
            try:
                out += self.extra_pads(np.asarray(lat, np.float64), np.asarray(lon, np.float64))
            except Exception as e:      # (no runways rather than no ground)
                log.debug("surface: extra pads: %s: %s", type(e).__name__, e)
        return out

    def lots(self, lat, lon):
        """The "infrastructure" and "land_use" Features of the cells around the points that are cached
        (parking lots, squares: paint_lots); never waits (missing cells are fetched in the background)."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        out = []
        for key in mapcells.cells_for_points(lat, lon):
            for layer in ("infrastructure", "land_use"):
                if not self.md.has(key, layer):
                    self._queue(key, (layer,))
                    revisit.fault()         # (left out: nothing built now is kept)
                    continue
                try:
                    out.append(self.md.cell(key, layers=(layer,), kind="background").layers.get(layer))
                except Exception as e:      # an unreadable cache file: like a missing one
                    log.debug("surface: %s of %s: %s: %s", layer, key, type(e).__name__, e)
        return out

    def urban(self, lat, lon):
        """Built-up land cover (WorldCover 50) at the points: roads.road_specs's urban_fn. Uncached cells
        read as not built-up."""
        return self.classes(lat, lon)[0] == roadsmod.URBAN_CLASS

    def classes(self, lat, lon):
        """(WorldCover class per point, complete): 0 (no data) where the cell is not cached yet. The
        land cover is read at the warped points (warp: organic borders)."""
        lat, lon = warp(lat, lon)
        out = np.zeros(lat.shape, np.uint8)
        complete = True
        for key in mapcells.cells_for_points(lat, lon):
            r = self._cached(key)
            lon0, lat0, lon1, lat1 = key.bounds()
            inside = (lat >= lat0) & (lat < lat1) & (lon >= lon0) & (lon < lon1)
            if not inside.any():
                continue
            if r is None:
                complete = complete and not self._is_queued(key)
                if not complete:
                    revisit.fault()         # (not cached yet: nothing built now is kept)
                continue
            out[inside] = r.sample(lon[inside], lat[inside])
        out[np.abs(lat) > mapcells.MAX_LAT] = POLAR_CLASS
        return out, complete

    def _is_queued(self, key):
        with self._lock:
            return key in self._pending
