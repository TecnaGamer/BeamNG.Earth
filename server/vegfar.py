"""The far forest ring (2026-11-19, the user: "a far ring that reaches farther than the buildings, only 2D"): past the
vegetation tiles' reach (vegetation.SEND_M, ~1.3 km) the far field was the photo alone, its forests flat, while the
skyline's buildings stand out to ~4.8 km. Here the forests out to FAR_SEND_M (past the skyline) as the stock far
clumps (trees.lua's *_clump groups: one forest-filler shape for 4-8 trees), which past ~230 m the engine draws as their
imposters only: a flat picture each.

Where: the canopy height's overviews (maprasters.canopy_coarse: Meta / WRI CHMv2, ~19 m pixels, a whole 40 km file in
2048² of them; no full-resolution reads, no land cover, roads or buildings). A geotile of zoom Z (~0.8 km at mid
latitudes) is cut into squares of SPACING_M of ground; a square whose share of pixels with a canopy of at least
CANOPY_MIN_M is FOREST_SHARE or more gets one clump, at a hashed spot in it, as tall as the square's canopy (clamped
to HEIGHT_M), its kind by the place (the conifers' share of the leaf type maps, the Mediterranean, Australia, the
tropics, the north's birches). Its ground the DEM's at the far field's coarse zoom.

The rows are the vegetation tiles' (group, x, y, z, yaw, scale, height, rank) in the geotile's frame, ranks under
RANK_MAX (trees.lua RINGS: the far rings show ranks under 0.2), so the mod takes them as any vegetation geotile."""
import numpy as np
import dnp

import vegetation as veg

Z = 15                      # geotiles of ~0.8 km (the turbines' zoom)
FAR_SEND_M = 6000.0         # past the skyline's 4.8 km (buildings.SKY_SEND_M)
FAR_DROP_M = 7000.0
PER_POSE = 2
SPACING_M = 60.0            # m of ground a square (its side): one clump each at most
PROTO = 1                   # hello.vegFar
CANOPY_MIN_M = 5.0
FOREST_SHARE = 0.45
HEIGHT_M = (8.0, 30.0)
RANK_MAX = 0.2               # (trees.lua RINGS: 0.2 shown to 2.5 km, fewer beyond)
MERC_HALF = 20037508.342789244


_to3857, _to4326 = veg._to3857, veg._to4326      # (dnp's: the same on every platform, GitHub #1169)


def squares(h, block, nodata=255):
    """Pure: per block x block square of canopy heights h (rows, cols): (row, col of its top-left pixel, the share of
    its pixels with canopy >= CANOPY_MIN_M, their mean height); the squares at FOREST_SHARE or more only."""
    h = np.asarray(h)
    r, c = h.shape[0] // block, h.shape[1] // block
    if r == 0 or c == 0:
        return np.zeros((0, 4))
    a = h[:r * block, :c * block].astype(np.float32)
    a = np.where(a == nodata, 0.0, a).reshape(r, block, c, block).swapaxes(1, 2).reshape(r, c, block * block)
    tree = a >= CANOPY_MIN_M
    share = tree.mean(axis=2)
    mean = np.where(tree.any(axis=2), (a * tree).sum(axis=2) / np.maximum(tree.sum(axis=2), 1), 0.0)
    rr, cc = np.nonzero(share >= FOREST_SHARE)
    return np.column_stack([rr * block, cc * block, share[rr, cc], mean[rr, cc]])


def kinds(lat, lon, u):
    """Per clump: its group name by the place (u: a uniform 0..1 per clump, the conifers' draw)."""
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    con = np.asarray(veg._conifer_share(lat, lon), np.float64)
    out = np.where(np.asarray(u) < con, "conifer_clump", "deciduous_clump").astype(object)
    broad = out == "deciduous_clump"
    north = broad & (lat >= 56.0) & veg.eurasia(lat, lon)
    out[north] = "birch_clump"
    med = np.asarray(veg.mediterranean(lat, lon), bool)
    out[med & broad] = "mediterranean_clump"
    aus = broad & (lat < -10.0) & (lat > -44.0) & (lon > 112.0) & (lon < 155.0)
    out[aus] = "eucalyptus_clump"
    return out


def far_rows(gx, gy, rasters, elev, z=Z):
    """(rows, pivot) of geotile (gx, gy) of zoom z: its far clumps in its own frame (geotiles.frame), [] where it has no
    forest or no canopy file. elev(lat, lon): the ground (m above the ellipsoid as the far field draws it)."""
    import geotiles
    clat, clon, south, west, north, east = geotiles.info(gx, gy, z)
    mx0, my0 = _to3857(west, south)
    mx1, my1 = _to3857(east, north)
    ras = rasters.canopy_coarse(float(mx0), float(my0), float(mx1), float(my1))
    if ras is None:
        return [], 0.0
    # (2026-11-23, the user's screenshot of the hills over Split: rows of clumps up the slopes, a grid, as only an
    # orchard stands: one clump in every SPACING_M square, near its middle. Now cells of about half that (of ground: a
    # Web-Mercator pixel is cos(lat) of its size there), each with a clump at a hashed chance that keeps one per
    # SPACING_M square on average, anywhere in its cell: pairs and gaps as a wood has, no lattice)
    px_m = ras.dx * max(0.05, float(dnp.cos(np.radians(clat))))
    cell = max(1, int(round(SPACING_M / 2.0 / px_m)))
    chance = min(1.0, dnp.sq(cell * px_m / SPACING_M))
    sq = squares(ras.data, cell)
    if len(sq) == 0:
        return [], 0.0
    r0, c0 = sq[:, 0], sq[:, 1]
    # (world-fixed hashes: the cell's own pixel indices in the global overview grid)
    gi = np.round((ras.x0 + MERC_HALF) / ras.dx).astype(np.int64) + c0.astype(np.int64)
    gj = np.round((MERC_HALF - ras.y0) / ras.dy).astype(np.int64) + r0.astype(np.int64)
    take = veg._hash01(gi, gj, 96) < chance
    if not take.any():
        return [], 0.0
    sq, r0, c0, gi, gj = sq[take], r0[take], c0[take], gi[take], gj[take]
    u1, u2 = veg._hash01(gi, gj, 91), veg._hash01(gi, gj, 92)
    px = ras.x0 + (c0 + cell * u1) * ras.dx
    py = ras.y0 - (r0 + cell * u2) * ras.dy
    lon, lat = _to4326(px, py)
    tx, ty = geotiles.tile_of(lat, lon, z)
    own = (tx == gx) & (ty == gy)
    if not own.any():
        return [], 0.0
    lat, lon, gi, gj = lat[own], lon[own], gi[own], gj[own]
    height = np.clip(sq[own, 3], *HEIGHT_M)
    group = kinds(lat, lon, veg._hash01(gi, gj, 93))
    frame = geotiles.frame(gx, gy, z)
    zg = np.asarray(elev(lat, lon), np.float64)
    e, n, zz = frame.geodetic_to_enu(lat, lon, zg)
    yaw = veg._hash01(gi, gj, 94) * 360.0
    rank = veg._hash01(gi, gj, 95) * RANK_MAX
    index = {g: i for i, g in enumerate(veg.GROUPS)}
    rows = [[index[g], round(float(x), 2), round(float(y), 2), round(float(h0), 2), round(float(w), 1), 1.0,
             round(float(hh), 2), round(float(k), 4)]
            for g, x, y, h0, w, hh, k in zip(group, e, n, zz, yaw, height, rank)]
    pivot = round(float(np.median(np.asarray(zz))), 2) if rows else 0.0
    return rows, pivot


def send_m(sky_m=None):
    """The ring's reach: FAR_SEND_M, or past the skyline's (sky_m: buildings.SKY_SEND_M times the building distance)
    when that is farther."""
    return max(FAR_SEND_M, (sky_m or 0.0) + 1200.0)


def ring_inner_m(tree_scale=1.0):
    """Where the ring starts: the vegetation tiles' reach (vegetation.SEND_M times the tree distance)."""
    return veg.SEND_M * float(tree_scale or 1.0)


__all__ = ["far_rows", "squares", "kinds", "send_m", "ring_inner_m", "Z", "FAR_DROP_M", "PER_POSE"]
