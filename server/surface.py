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
from PIL import Image, ImageDraw, ImageFilter
import geodesy

import heightmap
import mapcells
import mapdata as mapdatamod
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
ROAD_MATERIAL = {"dirt": DIRT, "gravel": LOOSE, "cobble": COBBLE, "paved": ASPHALT}
ROAD_ORDER = ("dirt", "gravel", "cobble", "paved")


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


def paint_roads(idx, lat, lon, feats_list, square, urban_fn=None, widths=None, extra=(), left_hand=False):
    """idx (material per sample, [row, col], as classify gives it) with the roads of feats_list (the
    "roads" Features of the cells around) drawn in at their width: ROAD_MATERIAL by surface kind.
    lat / lon: the samples' positions; square: m between samples; urban_fn: as roads.road_specs (the
    same widths as the drawn roads); extra: [(lat (k,), lon (k,), roads.Spec)] more roads (a tunnel's open
    sections, grading.Grader.open_tunnels). Pure (a new array)."""
    h, w = idx.shape
    feats_list = tuple(feats_list)
    to_px = grid_affine(lat, lon)
    frame = geodesy.EnuFrame(float(np.mean(lat)), float(np.mean(lon)), 0.)
    lon0, lon1, lat0, lat1 = float(lon.min()), float(lon.max()), float(lat.min()), float(lat.max())
    metres_lon = 111320. * max(.05, math.cos(math.radians(float(np.mean(lat)))))
    sections = roadsmod._join_sections(frame, feats_list)
    # Keep sub-cell edge coverage before reducing to the physics lattice. Rounding a
    # diagonal directly on that lattice can discard a cell touched by the pavement.
    raster_scale = 4
    layers = {k: Image.new("L", (w*raster_scale, h*raster_scale), 0) for k in ROAD_ORDER}
    layers["__verge"] = Image.new("L", (w*raster_scale, h*raster_scale), 0)
    draws = {k: ImageDraw.Draw(im) for k, im in layers.items()}
    lawn = Image.new("L", (w, h), 0)            # divided roads' medians (under the roads: they paint over it)
    lawn_draw = ImageDraw.Draw(lawn)
    seen = set()

    def ribbon(pts, width, kind):
        # Use the rendered ribbon's cross-sections, including mitres and asymmetric
        # shoulders. A thick Pillow line rounds both its centre and width to whole
        # pixels; at the 2 m physics resolution that left grass beneath paved edges.
        cross = (pts[:, 3:5] if pts.shape[1] == 5 else
                 roadsmod.offset_polyline(pts, 1.)[:, :2]-pts[:, :2])
        offset = cross*(np.asarray(width)[..., None]/2)
        edges = np.vstack([pts[:, :2]+offset, pts[:, :2]-offset])
        la, lo, _ = frame.enu_to_geodetic(edges[:, 0], edges[:, 1], np.tile(pts[:, 2], 2))
        c, r = to_px(lo, la)
        if c.max() < -2 or c.min() > w+2 or r.max() < -2 or r.min() > h+2:
            return
        pixels = np.rint(np.c_[c, r]*raster_scale+(raster_scale-1)/2).astype(np.int64)
        left, right = np.split(pixels, 2)
        for k in range(len(pts)-1):
            draws[kind].polygon([tuple(p) for p in (left[k], left[k+1], right[k+1], right[k])], fill=255)

    def line(x, y, spec, aisle):
        if len(x) < 2:
            return
        import roadlanes
        # Keep a generous allowance for lane tapers, asymmetric shoulders, sidewalks
        # and supersampled material coverage. Reject only whole ribbons outside the
        # block, before expensive joined normals and pavement-width interpolation.
        margin = max(100., 2*roadlanes.grading_spec(spec).width + 2*spec.walk + 2*square)
        if (np.max(x) < lon0-margin/metres_lon or np.min(x) > lon1+margin/metres_lon or
                np.max(y) < lat0-margin/110540. or np.min(y) > lat1+margin/110540.):
            return
        width = spec.width
        pts = np.column_stack(frame.geodetic_to_enu(y, x, 0.))
        pts, width = roadlanes.pavement(spec, roadsmod._with_cross(pts, sections), frame, left_hand)
        if aisle:
            width += 2 * AISLE_STALLS_M         # the stalls either side (parking.aisle_spots): no grass under cars
        if roadsmod.has_sidewalks(spec):
            width += 2 * (spec.walk + square)   # the sidewalks (+ a sample: tufts at the outer edge): grass grew up
                                                # through them (ground cover is the terrain's: Lauterbrunnen, Baltimore
                                                # 2026-10-15)
        ribbon(pts, width, spec.kind)
        if not roadsmod.has_sidewalks(spec):
            ribbon(pts, width+2*(VERGE_M+square), "__verge")

    for feats in feats_list:
        if feats is None or len(feats) == 0:
            continue
        specs = roadsmod.road_specs(feats, urban_fn, widths, left_hand)
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            if specs[i] is None or fid in seen:
                continue
            seen.add(fid)
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
    # Choose the same complete approach from neighbouring cells as the visible mesh,
    # and paint each taper at its actual varying width instead of its maximum width.
    for band in roadsmod.projected_ramp_layout(frame, feats_list, left_hand)[3]:
        for sl,appearance,kind in roadsmod.auxiliary_sections(band):
            ribbon(band['pts'][sl],band['widths'][sl],kind)
    for corner in roadsmod.median_layout(frame, feats_list, left_hand, urban_fn, widths)[0]:
        fill = corner.get('fill')
        if fill is not None:
            la, lo, _ = frame.enu_to_geodetic(fill[:, 0], fill[:, 1], fill[:, 2])
            c, r = to_px(lo, la)
            pixels = np.rint(np.c_[c, r]*raster_scale+(raster_scale-1)/2).astype(np.int64)
            draws['paved'].polygon([tuple(p) for p in pixels], fill=255)
    for la, lo, spec in extra:
        line(np.asarray(lo), np.asarray(la), spec, False)
    out = idx.copy()
    out[np.asarray(lawn) > 0] = LAWN
    verge = np.asarray(layers["__verge"].resize((w,h), Image.Resampling.BOX)) > 0
    out[verge & np.isin(out, VERGE_FROM)] = VERGE
    for k in ROAD_ORDER:
        # A material belongs to a whole terrain square, not just the sampled point.
        # Include the adjacent sample ring so every square touching pavement has its
        # road material, regardless of which side of the lattice a tyre contacts.
        coverage = layers[k].resize((w, h), Image.Resampling.BOX)
        m = np.asarray(coverage.filter(ImageFilter.MaxFilter(3))) > 0
        out[m] = ROAD_MATERIAL[k]
    return out


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


def paint_lots(idx, lat, lon, feats_list):
    """idx with the squares and pedestrian areas (land_use polygons of subtype "pedestrian") of feats_list
    drawn in as plain paving (the asphalt layer), then the parking lots (polygons of class "parking") as asphalt (Lancaster County's
    photos: the grass layer's ground cover grew on lots). Pure."""
    # (grey paving: the cobble layer's setts drew big squares and promenades pink, Harbor Point 2026-10-15)
    idx = _paint(idx, lat, lon, feats_list, _plazas, ASPHALT)
    return _paint(idx, lat, lon, feats_list, _parking, ASPHALT)


def _parking(feats):
    cls = feats.attrs.get("class")
    return None if cls is None else np.isin(cls.astype(str), list(PARKING_CLASSES))


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

    def close(self):
        self.pool.shutdown(wait=False)

    def _cached(self, key):
        """The cell's land-cover Raster from the cache, None if absent (a fetch is queued) or no
        coverage (open ocean: the layer is stored as missing)."""
        arrays = self.md._load(self.md.path(key, "landcover"))
        if arrays is None:
            self._queue(key)
            return None
        arrays = dict(arrays)
        arrays.pop("provenance", None)
        if "missing" in arrays:
            return None
        return mapdatamod._raster_from(arrays)

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
                if self.md._load(self.md.path(k, layer)) is None:
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
        for key in mapcells.cells_for_bbox(float(lat.min()), float(lon.min()), float(lat.max()), float(lon.max())):
            if self.md._load(self.md.path(key, "roads")) is None:
                self._queue(key, ("roads",))
                complete = False
                continue
            try:
                out.append(self.md.cell(key, layers=("roads",), kind="background").layers.get("roads"))
            except Exception as e:      # an unreadable cache file: like a missing one
                log.debug("surface: roads of %s: %s: %s", key, type(e).__name__, e)
                complete = False
        return out, complete

    def lots(self, lat, lon):
        """The "infrastructure" and "land_use" Features of the cells around the points that are cached
        (parking lots, squares: paint_lots); never waits (missing cells are fetched in the background)."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        out = []
        for key in mapcells.cells_for_bbox(float(lat.min()), float(lon.min()), float(lat.max()), float(lon.max())):
            for layer in ("infrastructure", "land_use"):
                if self.md._load(self.md.path(key, layer)) is None:
                    self._queue(key, (layer,))
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
        for key in mapcells.cells_for_bbox(float(lat.min()), float(lon.min()), float(lat.max()), float(lon.max())):
            r = self._cached(key)
            lon0, lat0, lon1, lat1 = key.bounds()
            inside = (lat >= lat0) & (lat < lat1) & (lon >= lon0) & (lon < lon1)
            if not inside.any():
                continue
            if r is None:
                complete = complete and not self._is_queued(key)
                continue
            out[inside] = r.sample(lon[inside], lat[inside])
        return out, complete

    def _is_queued(self, key):
        with self._lock:
            return key in self._pending
