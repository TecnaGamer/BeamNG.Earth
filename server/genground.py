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
The rest of the photo's path is unchanged: the roofs filled, the roads, lots, fields' furrows and golf painted in at
0.5 m, the cliffs as rock, the terrain material layers' sharp detail textures over it in the game.

With the switch on, the imagery chain holds SOURCES alone (chain(), earthd's main): no 1 m photo is fetched for the
ground, the far rings, the warm-up or the prefetch. Off (the default): nothing changes. The switch is a BNE_* variable,
so it is in revisit.code_key (the base photos' and far tiles' revisit keys, the blocks), switches_key and the decision
archives' vkey_doc like every build switch (experimental.py).

The noise is anchor-locked (the anchor frame's east / north): blocks of one anchor agree along their seams; another
anchor's view of the same place draws another grain (each anchor's photos are its own in the caches anyway).
"""
import logging
import os

import numpy as np

import dnp
import fast
import mapcells
import revisit

log = logging.getLogger("earthd.genground")

ON = os.environ.get("BNE_GEN_GROUND", "off").strip().lower() in ("on", "1", "yes", "true")
NEED = 307                  # earthfast gg_fill_polys / gg_regions / gen_ground (gr, gc)
# m round a block its generated ground is made over and cut from: a texel's colour reads the inputs this far round
# (genground.rs: windows FAR_R_M 48 + the lattice and purity margins), so overlapping blocks agree
HALO_M = 64.0
# the photo's pending mark while some map data it reads is not on disk (earthd.Server.base_ready / _base_kept)
GEN_PENDING = ("genground_data",)
LAYERS = ("land_use", "water", "infrastructure", "canopy", "landcover", "roads", "buildings")
# The imagery sources the generated ground is made from (imagery.SOURCES): Sentinel-2 alone, global
SOURCES = ("bngs2", "eox2023", "eox2016")

# Kinds of ground (genground.rs: the same numbers)
NONE, TREE, SHRUB, GRASS, CROP, BUILT, BARE, SNOW, WATER, WETLAND, LAWN, PAVED, ROOF = range(13)
KIND_NAMES = ("none", "tree", "shrub", "grass", "crop", "built", "bare", "snow", "water", "wetland", "lawn", "paved",
              "roof")
# WorldCover class -> kind (maprasters.CLASSES)
WC_KIND = {10: TREE, 20: SHRUB, 30: GRASS, 40: CROP, 50: BUILT, 60: BARE, 70: SNOW, 80: WATER, 90: WETLAND,
           95: WETLAND, 100: GRASS}
_WC_LUT = np.zeros(256, np.uint8)
for _c, _k in WC_KIND.items():
    _WC_LUT[_c] = _k
# Overture land_use class -> kind (others: the land cover's own)
LAND_USE_KIND = {
    **{c: LAWN for c in ("park", "garden", "village_green", "recreation_ground", "cemetery", "grave_yard",
                         "golf_course", "pitch", "playground", "dog_park", "lawn", "common", "schoolyard")},
    **{c: GRASS for c in ("meadow", "grass", "grassland", "greenfield", "fell")},
    **{c: CROP for c in ("farmland", "allotments", "plant_nursery", "farm")},
    **{c: SHRUB for c in ("orchard", "vineyard", "heath", "scrub", "shrubland")},
    **{c: TREE for c in ("forest", "wood")},
    **{c: BARE for c in ("quarry", "landfill", "construction", "brownfield", "sand", "beach", "bare_rock",
                         "scree", "salt_pond")},
    **{c: WETLAND for c in ("wetland", "marsh", "swamp", "bog", "reedbed")},
}
# Overture infrastructure class -> kind, drawn over the water (a pier's deck is not water)
INFRA_KIND = {"pier": BUILT, "dock": BUILT, "breakwater": BARE, "groyne": BARE}
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


def _layer(md, key, layer, missing, surface=None):
    """A cached layer of a cell, or None. Never fetches here: a missing one goes on `missing` (the photo is redone once
    it is on disk: GEN_PENDING, earthd.Server.base_ready), is a revisit fault (the photo is not kept) and is queued
    for a background fetch (Surface._queue: the land cover's thread)."""
    if md is None:
        return None
    if not md.has(key, layer):
        missing.append((key, layer))
        revisit.fault()
        queue = getattr(surface, "_queue", None)
        if queue is not None:
            try:
                queue(key, (layer,))
            except Exception as e:  # (asked again by the next photo)
                log.debug("genground: queue %s of %s: %s", layer, key, e)
        return None
    try:
        return md.cell(key, layers=(layer,), kind="background").layers.get(layer)
    except Exception as e:          # an unreadable cache file: like a missing one
        log.debug("genground: %s of %s: %s: %s", layer, key, type(e).__name__, e)
        missing.append((key, layer))
        revisit.fault()
        return None


def data_ready(md, frame, x0, y0, size):
    """Every layer the generated ground reads (LAYERS) on disk for the cells under the square and its halo (never
    fetches): a photo made without some of them (GEN_PENDING) is made again then."""
    if md is None:
        return True
    hx0, hy0, hsize, _hpx, _h = halo_square(x0, y0, size, 1024)
    la, lo, _ = frame.enu_to_geodetic(np.array([hx0, hx0 + hsize]), np.array([hy0, hy0 + hsize]), 0.0)
    keys = mapcells.cells_for_bbox(float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo)))
    return all(md.has(k, layer) for k in keys for layer in LAYERS)


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


def polygons(frame, x0, y0, size, px, feats_list):
    """(poly (px, px) uint32: 0 or 1 + the index of the polygon over each texel, kinds (polygons,) uint8) of the
    land-use, water and infrastructure Features in feats_list ([(layer name, Features)]): the land use by area,
    largest first (a park inside a bigger farm area wins), then the water, then piers and docks (INFRA_KIND)."""
    fast.require(NEED, "generated ground")
    t = size / px
    top = y0 + size
    items = []          # (order, -area, id, kind, [rings as texel (col, row)])
    seen = set()
    for layer, f in feats_list:
        if f is None or not len(f):
            continue
        if layer == "water":
            sel = np.ones(len(f), bool)
            kinds = np.full(len(f), WATER, np.uint8)
            order = 1
        elif layer == "infrastructure":     # piers and docks over the water (their decks: built ground)
            cls = f.attrs.get("class")
            if cls is None:
                continue
            kinds = np.array([INFRA_KIND.get(str(c), 255) for c in cls], np.uint8)
            sel = kinds != 255
            order = 2
        else:
            cls = f.attrs.get("class")
            if cls is None:
                continue
            kinds = np.array([LAND_USE_KIND.get(str(c), 255) for c in cls], np.uint8)
            sel = kinds != 255
            order = 0
        sel &= np.asarray(f.kind) == 3
        if not sel.any():
            continue
        xy = f.xy_in(frame)
        for i in np.flatnonzero(sel):
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
                items.append((order, -area, fid, pi, int(kinds[i]), rings))
    poly = np.zeros((px, px), np.uint32)
    if not items:
        return poly, np.zeros(0, np.uint8)
    items.sort(key=lambda it: it[:4])
    pts, ring_off, poly_off, kinds = [], [0], [0], []
    for _o, _a, _fid, _pi, k, rings in items:
        for r in rings:
            pts.append(r)
            ring_off.append(ring_off[-1] + len(r))
        poly_off.append(len(ring_off) - 1)
        kinds.append(k)
    fast._ef.gg_fill_polys(poly, np.ascontiguousarray(np.concatenate(pts), np.float64),
                           np.asarray(ring_off, np.int64), np.asarray(poly_off, np.int64),
                           np.arange(1, len(kinds) + 1, dtype=np.uint32))
    return poly, np.asarray(kinds, np.uint8)


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
    """(kind, region, canopy, complete) (px, px) of the square for gen_ground, from the cached map data (never waits:
    what is missing is queued, a revisit fault, and complete is False)."""
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
    keys = _cells(lat, lon)
    for key in keys:
        for layer in ("land_use", "water", "infrastructure"):
            f = _layer(md, key, layer, missing, surface)
            if f is not None:
                feats.append((layer, f))
        canopies.append(_layer(md, key, "canopy", missing, surface))
    if md is not None:
        for key in keys:
            for layer in ("landcover", "roads", "buildings"):
                if not md.has(key, layer):
                    missing.append((key, layer))
    poly, pkinds = polygons(frame, x0, y0, size, px, feats)
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
    kind, region = fast._ef.gg_regions(np.ascontiguousarray(base), poly, pkinds, canopy, cover)
    return kind, region, canopy, bool(complete) and not missing


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
    kind, region, canopy, complete = inputs(frame, x0, y0, size, px, md, surface, widths)
    t = size / px
    # (the global texel grid of the anchor frame: the blocks' texels lie on it, so their lattices agree)
    gc, gr = int(np.floor(x0 / t + 0.5)), int(np.floor(-(y0 + size) / t + 0.5))
    out = fast._ef.gen_ground(rgb, kind, region, canopy, float(x0 + t / 2), float(y0 + size - t / 2), float(t), gr, gc)
    return out, complete


def render(imagery, frame, x0, y0, size, px, md, surface, widths=None, pending=None):
    """(rgb (px, px, 3), sources used): the generated ground of [x0, x0 + size]^2 of frame at px, made over the square
    with HALO_M round it and cut to it (every block covering a spot draws it alike, the Codex review of 41d74b61). With
    map data still missing GEN_PENDING goes into pending (earthd.Server.base_ready: made again once it is here)."""
    hx0, hy0, hsize, hpx, h = halo_square(x0, y0, size, px)
    rgb, used = imagery.render(frame, hx0, hy0, hsize, hpx, pending)
    out, complete = generate(rgb, frame, hx0, hy0, hsize, hpx, md, surface, widths)
    if not complete and pending is not None:
        pending.add(GEN_PENDING)
    return np.ascontiguousarray(out[h:h + px, h:h + px]), used
