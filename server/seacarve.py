"""Sea water: the ground carved under the sea, and land kept above the mod's sea plane (user 2026-10-13:
sea and bays first, roughly real depths, cars sink; docs/research/2026-10-13-water-probe.md).

    sc = SeaCarver(md, cache_dir)                 # md: mapdata.MapData
    z = sc.carve(lat, lon, z_dem)                 # the DEM with the sea floor carved in
    src = SeaDem(dem, sc)                         # a dem.Dem-like sampler (carver None: sink only)
    sc.sea_at(dem, lat, lon, size)                # which points of a water tile are sea (seatiles.py)

The sea is Overture's `ocean` polygons (OSM's coastline: open sea, bays, tidal rivers). Per map-data
cell (mapcells z14) it is rasterised on the z16 Web-Mercator pixel grid (~1.9 m at 39 N) with a margin
from the neighbouring cells, and the floor depth grows from the shoreline at DEPTH_SLOPE to DEPTH_CAP
(no bathymetry yet: GEBCO / NOAA CUDEM later). A point's depth is the bilinear sample of that raster,
so the floor is continuous across the shoreline and across cells; a point's carved height depends
only on its position and the map data (deterministic, the same in every process).

The mod draws the sea as WaterBlock tiles that follow the Earth's curve (seatiles.py). Beyond the
carved levels the far tiles' DEM sea (SEA_LO..SEA_HI) is sunk SINK_M so it never shows through them.
"""
import io
import logging
import math
import os
import threading
import time
from collections import OrderedDict

import numpy as np
from polygon import Path

import mapcells
import pacer
import cache as cachemod
import staging

log = logging.getLogger("earthd.seacarve")

VERSION = 19
SEA_E = 0.0                 # m: the sea surface (the DEMs' heights are above mean sea level)
DEPTH_SLOPE = 0.15          # floor depth per metre from the shoreline (1 : 6.7)
DEPTH_CAP = 6.0             # m: the floor's depth away from the shore without bathymetry (Chesapeake mean
                            # ~6.4 m)
# Bathymetry: NOAA NCEI's DEM global mosaic (the best depths NOAA has: CUDEM ~3 m along the US coasts,
# coarser elsewhere; public domain). One export per cell, aligned with its raster at BATHY_DOWN x coarser.
BATHY_URL = ("https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/DEM_global_mosaic/ImageServer/exportImage"
             "?bbox={x0},{y0},{x1},{y1}&bboxSR=3857&imageSR=3857&size={n},{n}&format=tiff&pixelType=F32"
             "&interpolation=RSP_BilinearInterpolation&f=image")
BATHY_DOWN = 4              # ~7.5 m pixels
BATHY_MIN, BATHY_MAX = 1.0, 80.0    # m: a sea floor this deep at least / at most
BATHY_RETRY_S = 120.0       # s: a cell built without bathymetry is built again after this (in memory)
SHORE_SLOPE = 0.3           # m/m: with bathymetry, the floor reaches its depth from the shore this steeply
RAW_MB = 1536               # the uncompressed rasters (memory-mapped; ~9.5 MB a cell) kept at most (earthd's start-up
                            # prune: they grew to 13.7 GB on a Steam Deck beside 0.15 GB of the compressed ones they repeat)
PX_ZOOM = 16                # raster pixels: Web-Mercator z16 (256 px tiles): 1024 px per z14 cell
CELL_PX = 256 << (PX_ZOOM - mapcells.ZOOM)      # raster pixels per cell edge (1024)
PAD_PX = 32                 # margin rasterised from the neighbouring cells (> the cap's distance)
SEA_SUBTYPES = ("ocean",)
SEA_WATER_TAGS = ('"water":"harbour"', '"water":"bay"', '"water":"lagoon"', '"water":"sound"', '"water":"strait"',
                  '"water":"fjord"', '"water":"cove"', '"tidal":"yes"')   # OSM water areas at sea level: the sea
                  # (Sydney Harbour, natural=water + water=harbour, not coastline: a lake 2-6 m up on the DEM, the
                  # Harbour Bridge's deck 35 m over "ground"; 2026-10-18)
TAG_SEA_MAX_M = 15.0        # m: a water area tagged as the sea's (SEA_WATER_TAGS) is the sea only this near sea level
SEA_LO, SEA_HI = -0.5, 0.3  # m: DEM heights in this band are the sea where no polygons are at hand (far
                            # tiles, the large water tiles): the DEMs' sea surfaces are flat at ~0
SINK_M = 6.0                # m: such ground sunk below the surface (no flicker through the water tiles), as
                            # deep as the carved floor away from the shore (DEPTH_CAP) and past the water's
                            # colour ramp (sea.lua depthGradientMax 5-6 m): at 1.5 m the water's colour followed
                            # the WaterBlock tiles' flat surfaces (up to 1.3 m off the curve at their corners),
                            # nested pale and dark squares over the open sea (Italy 40.190, 16.689; 2026-10-15)
POLY_TILE_MAX = 2048.0      # water tiles up to this size are placed from the polygons, larger ones by
                            # the DEM band (map data is fetched near the player only)
CARVE_MAX_LEVEL = 2         # far tiles up to this level are carved (their map data is near the player:
                            # L2 reaches ~3 km, past the lakes' water tiles, seatiles.LAKE_RADIUS_M);
                            # coarser ones have the DEM's sea sunk (sink)
LAKE_SUBTYPES = ("lake", "reservoir", "pond", "water")
RIVER_SUBTYPES = ("river", "stream")  # polygons whose level follows the DEM, pixel by pixel (canals with
                                      # locks break into many tiny tiles: later, as River objects)
RIVER_SKIP_CLASSES = ("drain", "ditch")
RIVER_DEPTH_CAP = 3.0       # m
RIVER_DEM_STEP = 4          # river levels: the DEM every 4th pixel (~7.5 m), the lowest of 3 x 3 such
                            # samples (the flattened surface, not a bank),
WIDE_RIVER_M = 120.0        # a flattened river this wide or more is tiles (area water), narrower Rivers
OUTLINE_PAD_PX = 2          # the river mask spread this far past the river's water
LINE_CELLS = 1              # stream / river lines from the 3 x 3 cells around: a line is in every cell it crosses
                            # (owned by one); the 5 x 5 around fetched 16 more cells of water per raster on a
                            # first visit (~50 for one road tile in a Pennsylvania town, 2026-10-27)
LINE_STEP_PX = 4            # a level every 4 px (~7.5 m) along them
LINE_PROBE_MAX_M = 15.0     # the level: the lowest DEM within half the width, at most this far
LINE_DEPTH_M = (0.6, 3.0)   # the bed's depth in the middle: width / 6, clamped
LINE_BANK_M = 4.0           # the bank beyond the half width ...
LINE_BANK_SLOPE = 1.0       # ... kept below level + its distance (1:1)
RIVER_SMOOTH = 3            # flattened river levels: the mean of the river's samples within 3 (~22 m)
RIVER_FLAT_M = 0.25         # a river whose samples stand more than this above its surface (p90) is not
                            # flattened by the DEM: left out (rough creeks; River objects later)
WATER_BANK_M = 1.0          # m: a lake / river pixel whose DEM is higher than its level by more is dry
                            # (outlines that include banks, islands, a floodplain)
LAKE_SKIP_CLASSES = ("fountain", "swimming_pool", "wastewater", "basin", "drain", "fish_pass")
LAKE_DEPTH_CAP = 4.0        # m: lakes and ponds (no bathymetry); the slope is DEPTH_SLOPE
LAKE_MIN_PX = 100           # a lake smaller than this in the 3 x 3 cells' raster (~350 m^2) is left out
LAKE_FLAT_M = 1.5           # a "lake" whose DEM inside spans more (p10..p90) is not flat (a river): out
LAKE_DEM_ZOOM = 15          # its level: the median DEM inside, at this zoom (US DEMs flatten lakes)
JOIN_SEA_PX = 8             # a lake at sea level (SEA_LO..SEA_HI) this close to the sea's pixels joins it
LAKE_NOT_SEA_M = 0.02       # ... else its level is kept this far off SEA_E (sea_at tells the sea by its level)
SHORE_RING_PX = (2, 6)      # ... at most the ground this far outside its shore (~4-11 m; its SHORE_PCT percentile)
SHORE_PCT = 25              #     less SHORE_DROP_M: a DEM that does not see a canal (a town's, 10-20 m wide) puts its
SHORE_DROP_M = 0.3          #     water at street level, and its tiles flooded the streets (Petten, Maarssen: GitHub
                            #     #8 / #53); a dam's low side is a small part of a reservoir's ring
LEVEL_Q = 0.1               # m: lake levels quantised (the same in every cell for a flattened lake)
LEVEL_SAMPLES = 4000
LEVEL_PAD_PX = 2            # the level raster spread this far onto the shore (lookups beside the water)
MEM_CELLS = 24              # rasters kept per process
EARTH_R = 6371000.0


# A cell's raster also as one uncompressed file, memory-mapped (depth, level and outline side by side per pixel): a
# lookup reads only the pages it touches, and the server's processes share them in the system's file cache. Read
# from the compressed npz instead, every lookup of a cell out of a process's MEM_CELLS decoded ~10 MB again (the
# far tiles near the player and the ground ahead take turns over more cells than that: ~30 s of a Baltimore
# teleport on a Steam Deck, 2026-10-20).
RAW_DTYPE = np.dtype([("depth", "<f4"), ("level", "<f4"), ("outline", "?")])


def _raw_path(p):
    return p[:-4] + ".raw.npy" if p else None


def _write_raw(raw, r):
    if not raw:
        return
    a = np.empty(r[0].shape, RAW_DTYPE)
    a["depth"], a["level"], a["outline"] = r[0], r[1], r[2]
    tmp = f"{raw}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(os.path.dirname(raw), exist_ok=True)
        with open(tmp, "wb") as fh:
            np.save(fh, a)
        staging.replace_cached(tmp, raw)
    except OSError as e:                # (the npz serves)
        log.debug("sea: %s: %s", raw, e)
        try:
            os.remove(tmp)
        except OSError:
            pass


def _open_raw(raw):
    a = np.asarray(np.load(raw, mmap_mode="r", allow_pickle=False))     # (a plain view: np.memmap's per-slice hooks)
    if a.dtype != RAW_DTYPE or a.ndim != 2:
        raise ValueError(f"not a raster: {a.dtype} {a.shape}")
    return a["depth"], a["level"], a["outline"]


def _merc(lat, lon, z=PX_ZOOM):
    """Fractional Web-Mercator pixel coordinates (256 px tiles) at zoom z."""
    lat = np.clip(np.asarray(lat, np.float64), -mapcells.MAX_LAT, mapcells.MAX_LAT)
    n = 256.0 * (1 << z)
    x = (np.asarray(lon, np.float64) + 180.0) / 360.0 * n
    s = np.sin(np.radians(lat))
    y = (0.5 - np.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def _px_m(lat):
    return 2 * math.pi * 6378137.0 / (256 << PX_ZOOM) * math.cos(math.radians(lat))


def shore_steps(water, kmax):
    """For each True pixel, the number of erosion steps (alternating 4- and 8-neighbour, an octagonal
    approximation of the euclidean distance) until it is reached from a False pixel, capped at kmax;
    0 on False pixels (outside the raster counts as water: the padded margin). Rust (earthfast.shore_steps)."""
    import fast
    fast.require(67, "shore_steps")
    return fast._ef.shore_steps(np.ascontiguousarray(water, bool), int(kmax))


def _touches(m, other, k):
    """Are pixels of `other` within k pixels (shore_steps) of the mask m (its box only)?"""
    jm, im = np.nonzero(m)
    if len(jm) == 0:
        return False
    j0, j1 = max(0, int(jm.min()) - k - 1), min(m.shape[0], int(jm.max()) + k + 2)
    i0, i1 = max(0, int(im.min()) - k - 1), min(m.shape[1], int(im.max()) + k + 2)
    near = shore_steps(~m[j0:j1, i0:i1], k + 1) <= k
    return bool((near & other[j0:j1, i0:i1]).any())


def _box_mean(v, w, r):
    """The w-weighted mean of v over the (2r+1)^2 box around each cell (integral images); NaN where the
    box has no weight."""
    def box(a):
        c = np.pad(np.cumsum(np.cumsum(a, 0), 1), ((1, 0), (1, 0)))
        n, m = a.shape
        j, i = np.arange(n), np.arange(m)
        j0, j1 = np.clip(j - r, 0, n), np.clip(j + r + 1, 0, n)
        i0, i1 = np.clip(i - r, 0, m), np.clip(i + r + 1, 0, m)
        return c[j1][:, i1] - c[j0][:, i1] - c[j1][:, i0] + c[j0][:, i0]
    sw = box(w)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(sw > 0, box(v * w) / sw, np.nan)


def _components(mask):
    """The 8-connected components of a boolean grid: a list of (rows, cols) index arrays."""
    seen = np.zeros(mask.shape, bool)
    n, m = mask.shape
    out = []
    for j0, i0 in zip(*np.nonzero(mask)):
        if seen[j0, i0]:
            continue
        stack, rows, cols = [(j0, i0)], [], []
        seen[j0, i0] = True
        while stack:
            j, i = stack.pop()
            rows.append(j)
            cols.append(i)
            for dj in (-1, 0, 1):
                for di in (-1, 0, 1):
                    a, b = j + dj, i + di
                    if 0 <= a < n and 0 <= b < m and mask[a, b] and not seen[a, b]:
                        seen[a, b] = True
                        stack.append((a, b))
        out.append((np.array(rows), np.array(cols)))
    return out


def _lonlat(mx, my, z=PX_ZOOM):
    """The inverse of _merc."""
    n = 256.0 * (1 << z)
    lon = np.asarray(mx, np.float64) / n * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * np.asarray(my, np.float64) / n))))
    return lat, lon


def _cells_of(cx, cy):
    """The distinct (cx, cy) of the points, sorted by cx then cy (np.unique's order). A terrain block's ~260k
    points lie in a few cells: their range is walked (np.unique(axis=0) sorted every point: ~0.8 s a 9-tile
    profile, 2026-10-15)."""
    if cx.size == 0:
        return []
    x0, x1, y0, y1 = int(cx.min()), int(cx.max()), int(cy.min()), int(cy.max())
    if (x1 - x0 + 1) * (y1 - y0 + 1) > 64:      # spread-out points: one sort of a combined key
        k = np.unique((cx.ravel() - x0) * (y1 - y0 + 1) + (cy.ravel() - y0))
        return [(int(v // (y1 - y0 + 1)) + x0, int(v % (y1 - y0 + 1)) + y0) for v in k]
    out = []
    for kx in range(x0, x1 + 1):
        mx = cx == kx
        if not mx.any():
            continue
        cyk = cy[mx]
        for ky in range(y0, y1 + 1):
            if (cyk == ky).any():
                out.append((kx, ky))
    return out


class SeaCarver:
    """The water of the map-data cells near the player: the sea (level SEA_E) and lakes (each at its own
    level, measured from the DEM), with their floors carved from the shore."""

    def __init__(self, md, cache_dir=None, dem=None):
        self.md = md
        self.dem = dem                  # dem.Dem for the lakes' levels (None: no lakes)
        self.dir = os.path.join(cache_dir, "seacarve", f"v{VERSION}") if cache_dir else None
        self._mem = OrderedDict()       # (x, y) -> ((depth, level) or None, complete, built at)
        self._lock = threading.Lock()
        self._cell_locks = {}
        self._warm, self._warm_thread = [], None    # cells to build in the background (warm)
        self.warm_asked = 0             # warm() calls with cells: a lookup met a cell not at hand (earthd's water plans)
        self._warm_cv = threading.Condition()

    # --- rasters --------------------------------------------------------------------------------
    def _water(self, key):
        z, _, y = key
        if not 0 <= y < (1 << z):       # past the Web-Mercator rows (the poles): no map cells, no water data
            return None
        try:
            return self.md.cell(key, layers=("water",)).layers.get("water")
        except Exception as e:          # offline / fetch refused: no water there for now
            log.warning("sea: no water data for %s (%s)", key, e)
            return None

    def raster(self, x, y):
        """(depth, level) of cell (x, y): (CELL_PX + 2)² float32 each, pixel [j, i] centred on z16 pixel
        (x * CELL_PX - 1 + i + 0.5, y * CELL_PX - 1 + j + 0.5); level NaN away from water (spread
        LEVEL_PAD_PX onto the shore, for the lookups next to it); None when the cell has no water."""
        if not 0 <= y < (1 << mapcells.ZOOM):     # past the Web-Mercator rows (the poles)
            return None
        k = (x, y)
        now = time.monotonic()
        with self._lock:
            hit = self._mem.get(k)
            if hit is not None and (hit[1] or now - hit[2] < BATHY_RETRY_S):
                self._mem.move_to_end(k)
                return hit[0]
            lock = self._cell_locks.setdefault(k, threading.Lock())
        with lock:
            with self._lock:
                hit = self._mem.get(k)
                if hit is not None and (hit[1] or now - hit[2] < BATHY_RETRY_S):
                    return hit[0]
            r, complete = self._load(x, y)
            with self._lock:
                self._mem[k] = (r, complete, time.monotonic())
                self._mem.move_to_end(k)
                while len(self._mem) > MEM_CELLS:
                    self._mem.popitem(last=False)
            return r

    def _path(self, x, y):
        rel = getattr(self.md, "release", "x")
        lakes = "lakes" if self.dem is not None else "sea"
        return os.path.join(self.dir, str(rel), lakes, str(x), f"{y}.npz") if self.dir else None

    def _load(self, x, y):
        p = self._path(x, y)
        raw = _raw_path(p)
        if raw and os.path.exists(raw):
            try:
                got = _open_raw(raw)
                cachemod.touch_once(raw)        # (recent for the start-up prune: cache.prune_dir, RAW_MB)
                return got, True
            except (OSError, ValueError) as e:      # (a damaged file: the npz, or a build, again)
                log.debug("sea: %s: %s", raw, e)
        if p and os.path.exists(p):
            with np.load(p, allow_pickle=False) as f:
                if not f["any"]:
                    return None, True
                r = (f["depth"].astype(np.float32) / 100.0, f["level"].astype(np.float32),
                     f["outline"].astype(bool))
            _write_raw(raw, r)          # (a cell cached before the raw files: once)
            return r, True
        r = self._build(x, y)
        complete = r is None or r[3]
        if r is not None:
            r = (np.round(r[0] * 100.0).astype(np.float32) / 100.0, r[1], r[2])   # as the file gives back
        if p and complete:              # an incomplete raster (no bathymetry yet) is built again later
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as fh:
                if r is None:
                    np.savez_compressed(fh, any=False)
                else:
                    np.savez_compressed(fh, any=True, depth=np.round(r[0] * 100.0).astype(np.int16), level=r[1],
                                        outline=r[2])
            staging.replace_cached(tmp, p)
            if r is not None:
                _write_raw(raw, r)
        return r, complete

    def _build(self, x, y):
        own = self._water((mapcells.ZOOM, x, y))
        if own is None or not (self._sea_rows(own).size
                               or (self.dem is not None and (self._lake_rows(own).size
                                                             or self._river_rows(own).size))):
            return None                 # no water in the cell itself: nothing to carve here
        n = CELL_PX + 2 * PAD_PX
        ox, oy = x * CELL_PX - PAD_PX, y * CELL_PX - PAD_PX      # z16 pixel of the raster's corner
        sea = np.zeros((n, n), bool)
        river = np.zeros((n, n), bool)
        lake = np.full((n, n), -1, np.int32)
        lake_ids = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                f = own if (dx, dy) == (0, 0) else self._water((mapcells.ZOOM, x + dx, y + dy))
                if f is None:
                    continue
                for i in self._sea_rows(f):
                    if self.dem is not None and str(f.attrs["subtype"][i]) not in SEA_SUBTYPES:
                        m = np.zeros((n, n), bool)
                        self._rasterise(f, i, m, ox, oy)
                        if not self._near_sea_level(m, ox, oy):
                            continue    # a lake's harbour or bay (the lake rows take it)
                        sea |= m
                        continue
                    self._rasterise(f, i, sea, ox, oy)
                if self.dem is not None:
                    for i in self._river_rows(f):
                        self._rasterise(f, i, river, ox, oy)
                    for i in self._lake_rows(f):
                        gid = str(f.ids[i])
                        if gid not in lake_ids:
                            lake_ids.append(gid)
                        m = np.zeros((n, n), bool)
                        self._rasterise(f, i, m, ox, oy)
                        lake[m] = lake_ids.index(gid)
        lat_c = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / (1 << mapcells.ZOOM)))))
        px = _px_m(lat_c)
        level = np.full((n, n), np.nan, np.float32)
        depth = np.zeros((n, n), np.float32)
        lake[sea] = -1                  # the sea wins where they overlap
        river &= ~sea & (lake < 0)
        outline = river.copy()          # every river outline (riverlines.py measures widths across it)
        through = self._river_line_px(x, y, ox, oy, n) if (self.dem is not None and lake_ids) else None
        for k in range(len(lake_ids)):
            m = lake == k
            if through is not None and (m & through).any():
                river |= m              # a river runs through it (a pool behind a dam, water=river): it
                continue                # flows, as the river (River objects), not a still lake tile
            lv = self._lake_level(m, ox, oy, px, wet=sea | river | (lake >= 0))
            if lv is None:
                continue
            if SEA_LO <= lv <= SEA_HI and _touches(m, sea, JOIN_SEA_PX):
                sea |= m                # a lagoon, a tidal pond: part of the sea
                continue
            if abs(lv - SEA_E) < 0.01:  # a lake at sea level away from the sea (the Netherlands'): not the sea's
                lv = SEA_E + LAKE_NOT_SEA_M     # tiles (sea_at), which flooded every town round it (GitHub #8)
            m &= ~self._lake_dry(m, lv, ox, oy)
            kmax = int(math.ceil(LAKE_DEPTH_CAP / DEPTH_SLOPE / px)) + 2
            st = shore_steps(m, kmax).astype(np.float32)
            depth[m] = np.minimum(LAKE_DEPTH_CAP, DEPTH_SLOPE * (st[m] - 0.5) * px)
            level[m] = lv
        if river.any():
            rl = self._river_levels(river, ox, oy)
            if rl is not None:
                # (a river at sea level with no sea near: an inland one, the Vecht at Maarssen 50 km from the coast)
                tidal = river & (rl >= SEA_LO) & (rl <= SEA_HI) & sea.any()
                sea |= tidal            # a tidal river: the sea
                outline &= ~tidal
                river &= ~tidal & np.isfinite(rl)
                kmax = int(math.ceil(RIVER_DEPTH_CAP / DEPTH_SLOPE / px)) + 2
                st = shore_steps(river, kmax).astype(np.float32)
                depth[river] = np.minimum(RIVER_DEPTH_CAP, DEPTH_SLOPE * (st[river] - 0.5) * px)
                level[river] = rl[river]
                # wide stretches (a half width of WIDE_RIVER_M / 2 or more somewhere: the Susquehanna) are
                # area water like a lake, tiles fitted to its fall; one River ribbon that wide folded over
                # itself at bends and islands (holes, 2026-10-13). Narrow ones stay Rivers (the outline).
                core_k = int(math.ceil(WIDE_RIVER_M / 2 / px))
                wide = shore_steps(river, core_k) >= core_k
                for _ in range(core_k):                 # the core grown back over its own river
                    q = np.pad(wide, 1)
                    wide = (q[1:-1, 1:-1] | q[:-2, 1:-1] | q[2:, 1:-1] | q[1:-1, :-2] | q[1:-1, 2:]) & river
                outline &= ~wide
        if self.dem is not None:        # streams and the rivers the DEM does not flatten: their lines
            self._carve_lines(x, y, ox, oy, n, px, depth, level, outline)
        complete = True
        if sea.any():
            bathy = self._bathy(ox, oy, n)
            if bathy is None:           # no bathymetry (offline, refused): the generic floor, not cached
                complete = False
                kmax = int(math.ceil(DEPTH_CAP / DEPTH_SLOPE / px)) + 2
                st = shore_steps(sea, kmax).astype(np.float32)
                depth[sea] = np.minimum(DEPTH_CAP, DEPTH_SLOPE * (st[sea] - 0.5) * px)
            else:
                kmax = int(math.ceil(BATHY_MAX / SHORE_SLOPE / px)) + 2
                st = shore_steps(sea, kmax).astype(np.float32)
                # where the mosaic has no sea floor (its land DEM over Sydney Harbour: 2-6 m up) the generic floor,
                # not BATHY_MIN: 1 m of clear water showed the aerial photo's bridge shadow on the bed, 2026-10-18
                generic = np.minimum(DEPTH_CAP, DEPTH_SLOPE * (st - 0.5) * px)
                target = np.where(np.isfinite(bathy) & (bathy < -BATHY_MIN), np.clip(-bathy, BATHY_MIN, BATHY_MAX),
                                  generic)
                depth[sea] = np.minimum(target[sea], SHORE_SLOPE * (st[sea] - 0.5) * px)
            level[sea] = SEA_E
        for _ in range(OUTLINE_PAD_PX):  # the river mask a little wider than the water: a lookup next to a
            q = np.pad(outline, 1)      # river (the depth is bilinear) is never taken for a lake
            outline = q[1:-1, 1:-1] | q[:-2, 1:-1] | q[2:, 1:-1] | q[1:-1, :-2] | q[1:-1, 2:]
        inner = (slice(PAD_PX, -PAD_PX), slice(PAD_PX, -PAD_PX))
        if not (np.isfinite(level[inner]).any() or outline[inner].any()):
            return None
        for _ in range(LEVEL_PAD_PX):   # spread the levels onto the shore (bilinear lookups beside it)
            p = np.pad(level, 1, constant_values=np.nan)
            nb = np.stack([p[:-2, 1:-1], p[2:, 1:-1], p[1:-1, :-2], p[1:-1, 2:]])
            with np.errstate(all="ignore"):
                fill = np.nanmax(np.where(np.isfinite(nb), nb, -np.inf), axis=0)
            level = np.where(np.isnan(level) & np.isfinite(fill), fill, level).astype(np.float32)
        c = slice(PAD_PX - 1, PAD_PX + CELL_PX + 1)
        return depth[c, c].copy(), level[c, c].copy(), outline[c, c].copy(), complete

    def _bathy(self, ox, oy, n):
        """NOAA's elevations (m, negative below the sea) on the n x n raster at z16 pixel (ox, oy), or None
        when it can't be had now."""
        fetcher = getattr(self.md, "fetcher", None)
        if fetcher is None:
            return None
        res = 2 * math.pi * 6378137.0 / (256 << PX_ZOOM)
        half = math.pi * 6378137.0
        x0, x1 = ox * res - half, (ox + n) * res - half
        y1, y0 = half - oy * res, half - (oy + n) * res
        m = n // BATHY_DOWN
        url = BATHY_URL.format(x0=round(x0, 2), y0=round(y0, 2), x1=round(x1, 2), y1=round(y1, 2), n=m)
        try:
            body = fetcher.get(url).body
            from PIL import Image
            im = Image.open(io.BytesIO(body))
            if im.mode != "F" or im.size != (m, m):
                raise ValueError(f"unexpected bathymetry image {im.mode} {im.size}")
            a = np.array(im.resize((n, n), Image.BILINEAR), np.float32)
        except Exception as e:          # netfetch.FetchError, a bad image: later
            log.info("sea: no bathymetry for the cell at %d,%d (%s)", ox, oy, e)
            return None
        a[(a < -11000) | (a > 9000)] = np.nan     # nodata
        return a

    @staticmethod
    def _river_rows(f):
        sub = f.attrs["subtype"]
        cls = f.attrs.get("class", np.full(len(sub), "", object))
        inter = f.attrs.get("is_intermittent", np.zeros(len(sub), bool))
        ok = np.isin(sub, RIVER_SUBTYPES) & ~np.isin(cls, RIVER_SKIP_CLASSES) & (f.kind == 3)
        return np.flatnonzero(ok & ~np.asarray(inter, bool))

    def _carve_lines(self, x, y, ox, oy, n, px, depth, level, outline):
        """Streams and rivers along their lines (riverlines.py draws them): every LINE_STEP_PX, the level is
        the lowest DEM within half the width (the channel), the width the river's outline's where it has
        one (twice the distance to its edge) else riverlines.WIDTH_M; the bed is carved to
        level - D (1 - (d / hw)^2) inside the half width hw (D = width / 6 in LINE_DEPTH_M), the bank
        above kept below level + (d - hw) * LINE_BANK_SLOPE (a negative depth). Pixels the sea, a lake or a
        flattened river already hold are left alone. The pixels drawn join the outline (no lake tiles
        there; riverlines reads their level)."""
        import riverlines
        held = np.isfinite(level)
        kmax = int(math.ceil(riverlines.WIDTH_MAX_M / 2 / px))
        o_dist = shore_steps(outline, min(kmax, 256)).astype(np.float32) * px if outline.any() else None
        seen, lines = set(), []
        drawn = np.zeros((n, n), bool)
        for dy in range(-LINE_CELLS, LINE_CELLS + 1):
            for dx in range(-LINE_CELLS, LINE_CELLS + 1):
                f = self._water((mapcells.ZOOM, x + dx, y + dy))
                if f is None:
                    continue
                cls = f.attrs.get("class", np.full(len(f.kind), "", object))
                inter = f.attrs.get("is_intermittent", np.zeros(len(f.kind), bool))
                for i in np.flatnonzero(f.kind == 2):
                    kind = riverlines._kind(str(f.attrs["subtype"][i]), str(cls[i]))
                    gid = str(f.ids[i])
                    if kind is None or bool(inter[i]) or gid in seen:
                        continue
                    seen.add(gid)
                    lines.append((gid, kind, [ring for part in f.parts(i) for ring in part]))
        lines.sort(key=lambda r: r[0])      # (in id order: the same whichever cell met a line first)
        for _gid, kind, rings in lines:
            for ring in rings:
                mx, my = _merc(ring[:, 1], ring[:, 0])
                self._carve_line(np.stack([mx - ox, my - oy], -1), kind, ox, oy, n, px, depth, level,
                                 held, drawn, o_dist)
        outline |= drawn

    def _river_line_px(self, x, y, ox, oy, n):
        """The pixels the centrelines of class river (not streams) of the cells around (LINE_CELLS) cross."""
        import riverlines
        out = np.zeros((n, n), bool)
        seen = set()
        for dy in range(-LINE_CELLS, LINE_CELLS + 1):
            for dx in range(-LINE_CELLS, LINE_CELLS + 1):
                f = self._water((mapcells.ZOOM, x + dx, y + dy))
                if f is None:
                    continue
                cls = f.attrs.get("class", np.full(len(f.kind), "", object))
                for i in np.flatnonzero(f.kind == 2):
                    gid = str(f.ids[i])
                    if gid in seen or riverlines._kind(str(f.attrs["subtype"][i]), str(cls[i])) != "river":
                        continue
                    seen.add(gid)
                    for part in f.parts(i):
                        for ring in part:
                            mx, my = _merc(ring[:, 1], ring[:, 0])
                            q = np.stack([mx - ox, my - oy], -1)
                            seg = np.hypot(*np.diff(q, axis=0).T)
                            t = np.r_[0.0, np.cumsum(seg)]
                            if t[-1] <= 0:
                                continue
                            u = np.arange(0.0, t[-1], 0.5)
                            ii = np.floor(np.interp(u, t, q[:, 0])).astype(np.int64)
                            jj = np.floor(np.interp(u, t, q[:, 1])).astype(np.int64)
                            ok = (ii >= 0) & (ii < n) & (jj >= 0) & (jj < n)
                            out[jj[ok], ii[ok]] = True
        return out

    def _carve_line(self, pts, kind, ox, oy, n, px, depth, level, held, drawn, o_dist):
        import riverlines
        wmax = riverlines.WIDTH_MAX_M / px
        if (pts[:, 0].max() < -wmax or pts[:, 1].max() < -wmax or pts[:, 0].min() > n + wmax
                or pts[:, 1].min() > n + wmax or len(pts) < 2):
            return
        seg = np.hypot(*np.diff(pts, axis=0).T)
        s = np.r_[0.0, np.cumsum(seg)]
        if s[-1] <= 0:
            return
        t = np.linspace(0.0, s[-1], max(2, int(math.ceil(s[-1] / LINE_STEP_PX)) + 1))
        q = np.stack([np.interp(t, s, pts[:, 0]), np.interp(t, s, pts[:, 1])], -1)
        # only the samples whose channel and bank can reach the raster, and one either side (their segments): each
        # sample's level is its own; the whole river's were read for every raster, a long river's ~20,000 at a time
        # (~300 km2 of DEM for one road tile on a first visit to rural Pennsylvania, 2026-10-27)
        m = wmax + LINE_BANK_M / px + 2 * LINE_STEP_PX
        near = (q[:, 0] >= -m) & (q[:, 0] <= n + m) & (q[:, 1] >= -m) & (q[:, 1] <= n + m)
        if not near.any():
            return
        if not near.all():
            keep = near.copy()
            keep[:-1] |= near[1:]
            keep[1:] |= near[:-1]
            runs = np.flatnonzero(np.diff(np.r_[0, keep.astype(np.int8), 0]))
            for a, b in zip(runs[::2], runs[1::2]):
                if b - a >= 2:
                    self._carve_run(q[a:b], kind, n, px, depth, level, held, drawn, o_dist, ox, oy)
            return
        self._carve_run(q, kind, n, px, depth, level, held, drawn, o_dist, ox, oy)

    def _carve_run(self, q, kind, n, px, depth, level, held, drawn, o_dist, ox, oy):
        import riverlines
        w = np.full(len(q), riverlines.WIDTH_M[kind])
        if o_dist is not None:          # in an outline: twice the distance to its edge
            qi = np.clip(np.round(q).astype(np.int64), 0, n - 1)
            inr = (q[:, 0] >= 0) & (q[:, 0] < n) & (q[:, 1] >= 0) & (q[:, 1] < n)
            od = np.where(inr, o_dist[qi[:, 1], qi[:, 0]], 0.0)
            w = np.where(od > 0, np.maximum(2 * od, w), w)
        w = np.minimum(w, riverlines.WIDTH_MAX_M)
        # the level: the lowest DEM at the point and 8 around it at half the width (<= LINE_PROBE_MAX_M)
        r = np.clip(w / 2, 2.0, LINE_PROBE_MAX_M) / px
        ang = np.arange(8) * (np.pi / 4)
        pxs = np.concatenate([q[:, 0]] + [q[:, 0] + r * math.cos(a) for a in ang])
        pys = np.concatenate([q[:, 1]] + [q[:, 1] + r * math.sin(a) for a in ang])
        la, lo = _lonlat(ox + pxs + 0.5, oy + pys + 0.5)
        try:
            e = np.asarray(self.dem.sample(la, lo, LAKE_DEM_ZOOM), np.float64).reshape(9, -1).min(axis=0)
        except Exception as ex:
            log.debug("stream levels: %s", ex)
            return
        lv = np.round(e / LEVEL_Q) * LEVEL_Q
        hw = w / 2 / px
        D = np.clip(w / 6.0, *LINE_DEPTH_M)
        bank = LINE_BANK_M / px
        # each segment's box of pixels: the bed inside the half width, the bank beyond it; the deeper cut wins where
        # lines meet (a confluence), a pixel not cut yet takes this one (Rust: earthfast.carve_segments)
        import fast
        fast.require(67, "carve_segments")
        fast._ef.carve_segments(np.ascontiguousarray(q, np.float64), np.asarray(hw, np.float64),
                                np.asarray(lv, np.float64), np.asarray(D, np.float64), float(bank), float(px),
                                float(LINE_BANK_SLOPE), held, depth, level, drawn)

    def _coarse_dem(self, m, ox, oy):
        """The DEM every RIVER_DEM_STEP pixels over m's bounding box: (grid, j0, i0), None without one."""
        jj, ii = np.nonzero(m)
        k = RIVER_DEM_STEP
        j0, j1 = jj.min() // k, jj.max() // k + 1
        i0, i1 = ii.min() // k, ii.max() // k + 1
        gj, gi = np.meshgrid(np.arange(j0, j1 + 1), np.arange(i0, i1 + 1), indexing="ij")
        lat, lon = _lonlat(ox + gi * k + k / 2, oy + gj * k + k / 2)
        try:
            e = np.asarray(self.dem.sample(lat.ravel(), lon.ravel(), LAKE_DEM_ZOOM), np.float64).reshape(gj.shape)
        except Exception as ex:
            log.debug("water levels: %s", ex)
            return None
        return e, j0, i0

    def _river_levels(self, m, ox, oy):
        """The water level of every river pixel (m): from the DEM every RIVER_DEM_STEP pixels, the lowest of
        the 3 x 3 samples around (the surface rather than a bank), quantised to LEVEL_Q. Only rivers whose
        surface the DEM flattens (US DEMs do for rivers wider than ~30 m): a connected river whose inner
        samples (away from its edge) stand more than RIVER_FLAT_M above that surface at their 90th
        percentile (a creek in a wide outline, a rough channel), or that has none, gets none (NaN: no
        water, no carving; riverlines.py draws it as River objects). NaN too where the DEM
        is more than WATER_BANK_M above the level. None without a DEM."""
        c = self._coarse_dem(m, ox, oy)
        if c is None:
            return None
        e, j0, i0 = c
        k = RIVER_DEM_STEP
        jj, ii = np.nonzero(m)
        cj, ci = jj // k - j0, ii // k - i0
        cm = np.zeros(e.shape, bool)
        cm[cj, ci] = True
        p = np.pad(np.where(cm, e, np.inf), 1, constant_values=np.inf)
        lo = np.min(np.stack([p[a:a + e.shape[0], b:b + e.shape[1]] for a in range(3) for b in range(3)]), axis=0)
        q = np.pad(cm, 1)
        inner = np.all(np.stack([q[a:a + e.shape[0], b:b + e.shape[1]] for a in range(3) for b in range(3)]), axis=0)
        flat = np.zeros(e.shape, bool)
        for comp in _components(cm):
            core = inner[comp]          # judged away from its edge (the outline's cells there read the banks)
            if core.any() and np.percentile((e[comp] - lo[comp])[core], 90) <= RIVER_FLAT_M:
                flat[comp] = True
        # the surface smoothed over the river's own samples (a flattened DEM still steps by ~0.6 m within
        # 128 m below a dam: tiles fitted to it split into dozens of small ones, 2026-10-13)
        sm = _box_mean(np.where(cm & np.isfinite(lo), lo, 0.0), (cm & np.isfinite(lo)).astype(np.float64),
                       RIVER_SMOOTH)
        lvl = np.round(sm[cj, ci] / LEVEL_Q) * LEVEL_Q
        ok = flat[cj, ci] & (e[cj, ci] <= lvl + WATER_BANK_M) & np.isfinite(lvl)
        out = np.full(m.shape, np.nan, np.float32)
        out[jj, ii] = np.where(ok, lvl, np.nan)
        return out

    def _lake_dry(self, m, level, ox, oy):
        """The pixels of a lake (m) whose DEM stands more than WATER_BANK_M above its level: banks and
        islands its outline includes, left dry."""
        c = self._coarse_dem(m, ox, oy)
        dry = np.zeros(m.shape, bool)
        if c is None:
            return dry
        e, j0, i0 = c
        k = RIVER_DEM_STEP
        jj, ii = np.nonzero(m)
        dry[jj, ii] = e[jj // k - j0, ii // k - i0] > level + WATER_BANK_M
        return dry

    @staticmethod
    def _sea_rows(f):
        """The rows of the sea: ocean polygons and water areas tagged as tidal ones (SEA_WATER_TAGS)."""
        sub = f.attrs["subtype"]
        ok = np.isin(sub, SEA_SUBTYPES)
        tags = f.attrs.get("tags")
        if tags is not None:
            ok |= np.array([any(t in str(v).replace(" ", "") for t in SEA_WATER_TAGS) for v in tags], bool)
        return np.flatnonzero(ok & (f.kind == 3))

    def _near_sea_level(self, m, ox, oy):
        """Is the DEM inside the pixels m (a water area tagged as the sea's: SEA_WATER_TAGS) near sea level (median at
        most TAG_SEA_MAX_M)? Lake Michigan's harbours, tagged harbour at 176 m, were carved as the sea down to 0 m:
        holes 180 m deep along Chicago's lakeshore (GitHub #45); Sydney Harbour's DEM reads 2-6 m. Yes without a DEM."""
        jj, ii = np.nonzero(m)
        if len(jj) == 0:
            return True
        if len(jj) > LEVEL_SAMPLES:
            sel = np.linspace(0, len(jj) - 1, LEVEL_SAMPLES).astype(np.int64)
            jj, ii = jj[sel], ii[sel]
        lat, lon = _lonlat(ox + ii + 0.5, oy + jj + 0.5)
        try:
            e = np.asarray(self.dem.sample(lat, lon, LAKE_DEM_ZOOM), np.float64)
        except Exception as ex:         # no DEM there yet: as tagged
            log.debug("tagged sea level: %s", ex)
            return True
        e = e[np.isfinite(e)]
        return not len(e) or float(np.median(e)) <= TAG_SEA_MAX_M

    @staticmethod
    def _lake_rows(f):
        sub = f.attrs["subtype"]
        cls = f.attrs.get("class", np.full(len(sub), "", object))
        inter = f.attrs.get("is_intermittent", np.zeros(len(sub), bool))
        name = f.attrs.get("name", np.full(len(sub), "", object))
        fountain = np.array(["fountain" in str(v).lower() for v in name], bool)
        ok = np.isin(sub, LAKE_SUBTYPES) & ~np.isin(cls, LAKE_SKIP_CLASSES) & (f.kind == 3) & ~fountain
        return np.flatnonzero(ok & ~np.asarray(inter, bool))

    def _lake_level(self, m, ox, oy, px, wet=None):
        """The water level of a lake (its pixels m): the median DEM height inside, away from the banks,
        quantised to LEVEL_Q, no higher than the ground round its shore
        (_shore_ground, away from the other water wet); None if it is too small or not flat (a river, bad data)."""
        cnt = int(m.sum())
        if cnt < LAKE_MIN_PX:
            return None
        inner = shore_steps(m, 3) >= 3
        jj, ii = np.nonzero(inner if inner.sum() >= 20 else m)
        if len(jj) > LEVEL_SAMPLES:
            sel = np.linspace(0, len(jj) - 1, LEVEL_SAMPLES).astype(np.int64)   # deterministic subsample
            jj, ii = jj[sel], ii[sel]
        lat, lon = _lonlat(ox + ii + 0.5, oy + jj + 0.5)
        try:
            e = np.asarray(self.dem.sample(lat, lon, LAKE_DEM_ZOOM), np.float64)
        except Exception as ex:         # no DEM there yet: no lake for now
            log.debug("lake level: %s", ex)
            return None
        p10, p50, p90 = np.percentile(e, [10, 50, 90])
        if p90 - p10 > LAKE_FLAT_M:
            return None
        lv = p50
        shore = self._shore_ground(m, ox, oy, wet)
        if shore is not None:
            lv = min(lv, shore - SHORE_DROP_M)
        return float(round(lv / LEVEL_Q) * LEVEL_Q)     # (one at sea level and touching it joins the sea: raster)

    def _shore_ground(self, m, ox, oy, wet=None):
        """The SHORE_PCT percentile of the ground in a ring SHORE_RING_PX outside the lake's pixels m, but on the
        water wet (None: no ring in the raster or no DEM)."""
        jm, im = np.nonzero(m)
        if len(jm) == 0:
            return None
        r = SHORE_RING_PX[1] + 2                    # (the lake's box and the ring round it only)
        j0, j1 = max(0, int(jm.min()) - r), min(m.shape[0], int(jm.max()) + r + 1)
        i0, i1 = max(0, int(im.min()) - r), min(m.shape[1], int(im.max()) + r + 1)
        out = shore_steps(~m[j0:j1, i0:i1], SHORE_RING_PX[1] + 1)
        ring = (out >= SHORE_RING_PX[0]) & (out <= SHORE_RING_PX[1])
        if wet is not None:
            ring &= ~wet[j0:j1, i0:i1]
        jj, ii = np.nonzero(ring)
        jj, ii = jj + j0, ii + i0
        if len(jj) < 20:
            return None
        if len(jj) > LEVEL_SAMPLES:
            sel = np.linspace(0, len(jj) - 1, LEVEL_SAMPLES).astype(np.int64)
            jj, ii = jj[sel], ii[sel]
        lat, lon = _lonlat(ox + ii + 0.5, oy + jj + 0.5)
        try:
            e = np.asarray(self.dem.sample(lat, lon, LAKE_DEM_ZOOM), np.float64)
        except Exception as ex:
            log.debug("lake shore: %s", ex)
            return None
        e = e[np.isfinite(e)]
        return float(np.percentile(e, SHORE_PCT)) if len(e) >= 20 else None

    @staticmethod
    def _rasterise(f, i, water, ox, oy):
        n = water.shape[0]
        for part in f.parts(i):
            rings = []
            for ring in part:
                mx, my = _merc(ring[:, 1], ring[:, 0])
                rings.append(np.stack([mx - ox, my - oy], -1))
            allp = np.concatenate(rings)
            i0, j0 = max(0, int(np.floor(allp[:, 0].min()))), max(0, int(np.floor(allp[:, 1].min())))
            i1, j1 = min(n, int(np.ceil(allp[:, 0].max())) + 1), min(n, int(np.ceil(allp[:, 1].max())) + 1)
            if i1 <= i0 or j1 <= j0:
                continue
            gi, gj = np.meshgrid(np.arange(i0, i1) + 0.5, np.arange(j0, j1) + 0.5)
            pts = np.stack([gi.ravel(), gj.ravel()], -1)
            inside = np.zeros(len(pts), bool)
            for r in rings:             # even-odd: holes (islands) cut out
                inside ^= Path(r).contains_points(pts)
            water[j0:j1, i0:i1] |= inside.reshape(j1 - j0, i1 - i0)

    # --- queries --------------------------------------------------------------------------------
    def ready(self, x, y):
        """Cell (x, y)'s raster is at hand (in memory or on disk): a lookup will not wait for the network."""
        with self._lock:
            if (x, y) in self._mem:
                return True
        p = self._path(x, y)
        return bool(p and os.path.exists(p))

    def _warm_loop(self):
        while True:
            pacer.hold()                # (a teleport's first block first)
            with self._warm_cv:
                while not self._warm:
                    self._warm_cv.wait()
                k = self._warm.pop(0)
            try:
                self.raster(*k)
            except Exception:
                log.exception("sea: building cell %s", k)

    def warm(self, keys):
        """Build these cells' rasters in the background (one thread, in order)."""
        with self._warm_cv:
            if keys:
                self.warm_asked += 1
            for k in keys:
                if k not in self._warm:
                    self._warm.append(k)
            if self._warm_thread is None:
                self._warm_thread = threading.Thread(target=self._warm_loop, daemon=True, name="sea-warm")
                self._warm_thread.start()
            self._warm_cv.notify()

    def water(self, lat, lon, wait=True):
        """(floor depth below the surface, the surface's level) at the points: depth 0 and level NaN on
        land; the depth bilinear (continuous across the shore), the level of the nearest pixel.
        wait=False: cells not at hand are left out (and built in the background); a third array then
        says which points were known."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        d = np.zeros(lat.shape, np.float32)
        lv = np.full(lat.shape, np.nan, np.float32)
        known = np.ones(lat.shape, bool)
        if lat.size == 0:
            return (d, lv) if wait else (d, lv, known)
        mx, my = _merc(lat, lon)
        cx = np.floor(mx / CELL_PX).astype(np.int64)
        cy = np.floor(my / CELL_PX).astype(np.int64)
        later = []
        for kx, ky in _cells_of(cx, cy):
            sel = (cx == kx) & (cy == ky)
            if not wait and not self.ready(int(kx), int(ky)):
                later.append((int(kx), int(ky)))
                known[sel] = False
                continue
            r = self.raster(int(kx), int(ky))
            if r is None:
                continue
            rd, rl = r[0], r[1]
            fx = mx[sel] - kx * CELL_PX + 0.5      # raster coords: pixel centres at i + 0.5 - 1
            fy = my[sel] - ky * CELL_PX + 0.5
            i0 = np.clip(np.floor(fx).astype(np.int64), 0, rd.shape[1] - 2)
            j0 = np.clip(np.floor(fy).astype(np.int64), 0, rd.shape[0] - 2)
            tx, ty = np.clip(fx - i0, 0, 1), np.clip(fy - j0, 0, 1)
            d[sel] = ((rd[j0, i0] * (1 - tx) + rd[j0, i0 + 1] * tx) * (1 - ty)
                      + (rd[j0 + 1, i0] * (1 - tx) + rd[j0 + 1, i0 + 1] * tx) * ty)
            ni = np.clip(np.floor(fx + 0.5).astype(np.int64), 0, rl.shape[1] - 1)
            nj = np.clip(np.floor(fy + 0.5).astype(np.int64), 0, rl.shape[0] - 1)
            lv[sel] = rl[nj, ni]
        if later:
            import revisit
            revisit.fault()                 # (left out: nothing built now is kept)
            self.warm(later)
        return (d, lv) if wait else (d, lv, known)

    def depth(self, lat, lon):
        """Floor depth below the water surface at the points (0 on land and away from water)."""
        return self.water(lat, lon)[0]

    def carve(self, lat, lon, z):
        """z (DEM heights at the points) with the beds of the sea, the lakes and the rivers carved in (a
        negative depth: a stream's bank, kept no higher than its level plus that)."""
        d, lv = self.water(lat, lon)
        cut = (d != 0) & np.isfinite(lv)
        if not cut.any():
            return z
        z = np.asarray(z, np.float64)
        return np.where(cut, np.minimum(z, np.where(cut, lv, 0.0) - d), z)

    def lake_at(self, lat, lon, wait=True):
        """The level of the lakes (still water above sea level) at the points: NaN on land, at sea, away
        from water and in rivers (they slope: River objects, riverlines.py, not flat tiles; their beds are
        carved here all the same); wait=False: also in cells not at hand yet, built in the background."""
        d, lv = self.water(lat, lon, wait=wait)[:2]
        return np.where((d > 0) & (np.abs(lv - SEA_E) >= 0.01) & ~self.outline_at(lat, lon), lv, np.nan)

    def outline_at(self, lat, lon):
        """Inside a river outline above sea level (any, flattened or not) at the points; False in cells not
        at hand yet (built in the background)."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        out = np.zeros(lat.shape, bool)
        if lat.size == 0:
            return out
        mx, my = _merc(lat, lon)
        cx = np.floor(mx / CELL_PX).astype(np.int64)
        cy = np.floor(my / CELL_PX).astype(np.int64)
        later = []
        for kx, ky in _cells_of(cx, cy):
            if not self.ready(kx, ky):
                later.append((kx, ky))
                continue
            r = self.raster(kx, ky)
            if r is None:
                continue
            sel = (cx == kx) & (cy == ky)
            ni = np.clip(np.floor(mx[sel] - kx * CELL_PX + 1.0).astype(np.int64), 0, r[2].shape[1] - 1)
            nj = np.clip(np.floor(my[sel] - ky * CELL_PX + 1.0).astype(np.int64), 0, r[2].shape[0] - 1)
            out[sel] = r[2][nj, ni]
        if later:
            self.warm(later)
        return out

    def near(self, lat, lon, radius_m, step_m=250.0):
        """Any sea within radius_m of (lat, lon) (sampled on a step_m grid)."""
        k = int(radius_m // step_m)
        o = np.arange(-k, k + 1) * step_m
        E, N = np.meshgrid(o, o)
        inside = np.hypot(E, N) <= radius_m
        la = lat + N[inside] / 111320.0
        lo = lon + E[inside] / (111320.0 * math.cos(math.radians(lat)))
        d, lv = self.water(la, lo)
        return bool(((d > 0) & (np.abs(lv - SEA_E) < 0.01)).any())

    def sea_at(self, dem, lat, lon, size, wait=True):
        """Which points of a water tile of `size` m are sea (seatiles.plan): the polygons for the tiles near
        the player, the DEM band for larger tiles (wait=False: also for the cells not at hand yet, which
        are built in the background)."""
        import dem as demmod
        lat = np.asarray(lat, np.float64)
        known = np.zeros(lat.shape, bool)
        if size <= POLY_TILE_MAX:       # the polygons (wait=False: where the cells are at hand; the rest are
            d, lv, known = self.water(lat, lon, wait=False)   # built meanwhile)
            if wait and not known.all():
                d, lv = self.water(lat, lon)
                known[:] = True
            sea = (d > 0) & (np.abs(lv - SEA_E) < 0.01)
            if known.all():
                return sea
        else:
            sea = np.zeros(lat.shape, bool)
        z = demmod.zoom_for(float(np.mean(lat)), size / 16.0)
        z = min(max(z, getattr(demmod, "MIN_ZOOM", 0)), demmod.MAX_ZOOM)
        lon = np.asarray(lon, np.float64)
        e = np.asarray(dem.sample(lat[~known], lon[~known], z), np.float64)
        sea[~known] = (e >= SEA_LO) & (e <= SEA_HI)
        return sea


# Shorelines for the shore sound (the mod's ambience.lua lays the stock "Waves" ribbons along them): the sea's and the
# lakes' edges (not rivers', which have their own sound), lakes under SHORE_MIN_M2 left out (ponds), simplified to
# SHORE_EPS_PX, pieces under SHORE_MIN_LEN_PX dropped; within SHORE_RADIUS_M of the player
SHORE_MIN_M2 = 50000.0
SHORE_EPS_PX = 1.5
SHORE_MIN_LEN_PX = 8.0
SHORE_RADIUS_M = 1500.0
SHORE_KINDS = {1: "lake", 2: "sea"}


def cell_shores(carver, x, y):
    """The shorelines of cell (x, y): [(kind "lake" / "sea", level m, lat (k,), lon (k,))], from its raster
    (earthfast.shore_lines over its own pixels: a line on its edge is the neighbour's)."""
    import fast
    r = carver.raster(x, y)
    if r is None:
        return []
    depth, level, outline = r
    wet = (depth > 0) & np.isfinite(level) & ~outline
    sea = wet & (np.abs(level - SEA_E) < 0.01)
    kind = np.ascontiguousarray(np.where(sea, 2, np.where(wet, 1, 0)), np.uint8)
    if not kind.any():
        return []
    lat_c = float(_lonlat((x + 0.5) * CELL_PX, (y + 0.5) * CELL_PX)[0])
    min_px = int(SHORE_MIN_M2 / _px_m(lat_c) ** 2)
    fast.require(79, "shore_lines")
    pts, off, wpx, kinds = fast._ef.shore_lines(kind, min_px, SHORE_EPS_PX, SHORE_MIN_LEN_PX, 1, CELL_PX + 1, 1,
                                                CELL_PX + 1)
    out = []
    for k in range(len(kinds)):
        p = pts[off[k]:off[k + 1]]
        la, lo = _lonlat(x * CELL_PX - 1 + p[:, 0], y * CELL_PX - 1 + p[:, 1])     # (corner i: px x*CELL_PX - 1 + i)
        out.append((SHORE_KINDS[int(kinds[k])], float(level[wpx[k, 0], wpx[k, 1]]), la, lo))
    return out


def shore_plan(frame, x, y, carver, radius=SHORE_RADIUS_M):
    """The shorelines within radius of (x, y) in `frame`, from the cells at hand (the others are built in the
    background and come with a later plan): [[key, kind, [[x, y, z], ...]], ...] (z: the water's level), keys naming
    their nodes."""
    import hashlib
    la0, lo0, _ = frame.enu_to_geodetic(x - radius, y - radius, 0.0)
    la1, lo1, _ = frame.enu_to_geodetic(x + radius, y + radius, 0.0)
    try:
        keys = mapcells.cells_for_bbox(float(la0), float(lo0), float(la1), float(lo1), max_cells=64)
    except ValueError:
        return []
    out, later = [], []
    for key in keys:
        cx, cy = int(key.x), int(key.y)
        if not carver.ready(cx, cy):
            later.append((cx, cy))
            continue
        for n, (kind, lv, la, lo) in enumerate(cell_shores(carver, cx, cy)):
            e, nn, z = frame.geodetic_to_enu(la, lo, np.full(len(la), lv))
            nodes = [[round(float(a), 1), round(float(b), 1), round(float(c), 2)] for a, b, c in zip(e, nn, z)]
            h = hashlib.sha1(repr(nodes).encode()).hexdigest()[:8]
            out.append([f"S{cx}_{cy}_{n}_{h}", kind, nodes])
    if later:
        carver.warm(later)
    return out


def sink(e):
    """Far-tile elevations with the DEM's sea (SEA_LO..SEA_HI m) sunk SINK_M below the surface, so the
    far tiles never show through the mod's water tiles (levels without the carved polygons)."""
    e = np.asarray(e, np.float64)
    return np.where((e >= SEA_LO) & (e <= SEA_HI), np.minimum(e, SEA_E - SINK_M), e)


class SeaDem:
    """A dem.Dem-like sampler with the sea floor carved in (carver) or, for the far levels without map
    data (carver None), the DEM's sea sunk (sink)."""

    def __init__(self, dem, carver=None):
        self.dem, self.carver = dem, carver

    def sample(self, lat, lon, zoom, progress=None):
        z = self.dem.sample(lat, lon, zoom, progress) if progress is not None else self.dem.sample(lat, lon, zoom)
        return self.carver.carve(lat, lon, z) if self.carver is not None else sink(z)

    def __getattr__(self, name):
        return getattr(self.dem, name)
