"""Trees, bushes and rocks per map-data cell (docs/ROADMAP.md v1.9 "Cities & nature"; the user, 2026-10-13:
"trees, rocks and other decoration").

    veg = Vegetation(md, cache_dir)
    items = veg.cell(key)          # Items: lon, lat, height, radius, kind, group, rank, yaw, scale

Deterministic (ROADMAP "Principle: deterministic generation"): every item is a pure function of the
cell's map data and VERSION; its random choices (species, yaw, size, thinning rank) hash its position.

- Trees: the tops of Meta/WRI's 1.2 m canopy height model (CHMv2), local maxima of the lightly
  smoothed heights, in a window that grows with the height (a crown about 0.12 h + 1 m across), taller
  than TREE_MIN_M. Not on buildings (the CHM counts roofs, solar panels: Kayenta), roads (a trunk in
  the lane, under canopy that overhangs it) or water.
- Bushes: canopy maxima from BUSH_MIN_M to TREE_MIN_M, and a hashed scatter over shrubland.
- Rocks: a hashed scatter over bare / sparse land (WorldCover 60), thinner over shrubs and grass.
- Groups: deciduous / conifer by a regional mix and height, shrub, rock (the mod maps groups to its
  stock shapes).
"""
import logging
import math
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

import fast

import mapcells
import pacer
import staging

log = logging.getLogger("earthd.vegetation")

VERSION = 14          # (the cached cells hold their groups: 14 for the regional rules of GitHub #110)
TREE_MIN_M = 3.0
CROWN_SPACING = 0.5          # tops kept this many crown radii apart (0.8 looked sparse: the user, 2026-10-13)
BUSH_MIN_M = 1.0
KIND_TREE, KIND_BUSH, KIND_ROCK = 0, 1, 2
GROUPS = ("deciduous", "conifer", "shrub", "rock", "pine_west", "palm", "juniper", "desert_shrub", "desert_rock",
          "oak_west", "tropical")
(G_DECIDUOUS, G_CONIFER, G_SHRUB, G_ROCK, G_PINE_WEST, G_PALM, G_JUNIPER, G_DESERT_SHRUB, G_DESERT_ROCK,
 G_OAK_WEST, G_TROPICAL) = range(len(GROUPS))
ROAD_CLEAR_M = 1.5          # trees kept this far off a road's edge (its width by class below)
TREE_PIT_M = 0.4            # m past a road's edge (+ the push's 0.6) where a top over the road lands: a sidewalk's
                            # tree pit by the curb (pushed out of the full corridor, Boston Street's trees landed on the
                            # row houses at the back of the sidewalk and all went; Mapillary: one every ~8 m, 2026-10-17)
PATH_CLASSES = {"footway", "path", "steps", "cycleway", "bridleway", "pedestrian"}   # no corridor: street trees
                            # stand in the sidewalk (mapped as a footway: Boston Street's row sat on its line)
ROAD_HALF_WIDTH_M = {"motorway": 7.0, "trunk": 6.0, "primary": 5.5, "secondary": 5.0, "tertiary": 4.5,
                     "residential": 4.0, "service": 3.0, "unclassified": 3.5, "living_street": 3.0}
ROAD_HALF_DEFAULT_M = 2.5
SCATTER_CELL_M = 6.0        # the hashed scatter's grid (one candidate per cell)
# class: (bush, rock). Tree cover (10) gets an understory (Columbia's photos: forest edges are thickets
# of shrubs and vines under the trees; ours were bare ground between trunks), also under the canopy.
SCATTER_P = {10: (0.30, 0.0), 20: (0.10, 0.04), 30: (0.01, 0.003), 60: (0.0, 0.08), 100: (0.02, 0.02)}
UNDERSTORY = 10
BUILT_NEAR_M = 60.0         # bare ground with built-up land this far around gets no scatter (squares, lots)
# Rocks on bare ground lie on slopes, outcrops and fans, not on flat playas, salt pans and dry lake beds (the
# Bonneville Salt Flats had a rock every ~75 m2, the user 2026-10-20; photos: a bare white crust for miles). A bare
# cell's rock is kept with the share of the ground's slope between these (the DEM ROCK_SLOPE_D m either side, at
# ROCK_SLOPE_ZOOM: coarse tiles, few downloads); without a DEM, as before.
ROCK_SLOPE = (0.02, 0.10)
BARREN_NEAR_M = 20.0        # m: a canopy top on bare ground or snow needs vegetated land this near (_build)
ROCK_SLOPE_D = 40.0
ROCK_SLOPE_ZOOM = 12
# Forest edges along roads in humid country (Columbia's photos: a wall of shrubs, saplings and vines from the
# ground up, where ours showed bare trunks over grass): a denser band of bigger bushes within EDGE_M of a road.
EDGE_M = 7.0
EDGE_CELL_M = 3.0
EDGE_P = 0.4
MEM_CELLS = 16
WARM_M = 1200.0              # cells within this of the player are built ahead (warm_around)
WC = {"tree": 10, "shrub": 20, "grass": 30, "crop": 40, "built": 50, "bare": 60, "snow": 70,
      "water": 80, "wetland": 90, "mangrove": 95, "moss": 100}


@dataclass
class Items:
    lon: np.ndarray
    lat: np.ndarray
    height: np.ndarray          # m (trees: the CHM top; bushes and rocks: their size)
    radius: np.ndarray          # m (crown / footprint)
    kind: np.ndarray            # uint8 KIND_*
    group: np.ndarray           # uint8 GROUPS index
    rank: np.ndarray            # float32 in [0, 1): thinning (a level shows those below its threshold)
    yaw: np.ndarray             # degrees
    scale: np.ndarray           # size variation (around 1)

    def __len__(self):
        return len(self.lon)

    def take(self, sel):
        return Items(*(getattr(self, f)[sel] for f in ("lon", "lat", "height", "radius", "kind", "group",
                                                       "rank", "yaw", "scale")))


def _hash01(a, b, salt):
    """Deterministic uniform [0, 1) from two integer arrays and a salt."""
    a = np.asarray(a, np.uint64)
    b = np.asarray(b, np.uint64)
    x = a * np.uint64(0x9E3779B97F4A7C15) ^ (b + np.uint64(salt)) * np.uint64(0xC2B2AE3D27D4EB4F)
    x ^= x >> np.uint64(31)
    x *= np.uint64(0xBF58476D1CE4E5B9)
    x ^= x >> np.uint64(29)
    return (x >> np.uint64(11)).astype(np.float64) / float(1 << 53)


def _shift_max(a, r):
    """The maximum over the disc of radius r around each pixel ((dj - r)^2 + (di - r)^2 <= r * r + r; outside the
    array -inf; float32). earthfast disc_max (2026-10-23: the numpy version's full-array pass per disc offset)."""
    fast.require(64, "disc_max")
    return fast._ef.disc_max(np.ascontiguousarray(a, np.float32), int(r))


def _dilate(m, r):
    """Bool mask grown by r pixels (a square window, separable)."""
    out = m.copy()
    for axis in (0, 1):
        acc = out.copy()
        for k in range(1, r + 1):
            acc[tuple(slice(k, None) if a == axis else slice(None) for a in range(2))] |= \
                out[tuple(slice(None, -k) if a == axis else slice(None) for a in range(2))]
            acc[tuple(slice(None, -k) if a == axis else slice(None) for a in range(2))] |= \
                out[tuple(slice(k, None) if a == axis else slice(None) for a in range(2))]
        out = acc
    return out


def _dry_west(lat, lon):
    lat, lon = np.asarray(lat), np.asarray(lon)
    return (lon < -102.0) & (lat > 25.0) & (lat < 46.0) & ~((lon < -121.0) & (lat > 37.0))


def regional(group, height, lat, lon, lcls, u):
    """The groups of a region (the stock maps' species: West Coast USA's pines, oaks and palms, Utah's junipers
    and desert bushes, Johnson Valley's rocks, Jungle Rock Island's palms and tropical trees) from the generic
    ones, by a coarse climate from the position and the land cover class at the item (until a species map): the Rust
    kernel earthfast.regional_groups (its rules there). u: a hashed [0, 1) per item. Pure."""
    fast.require(43, "regional_groups")
    f64 = np.float64
    n = np.size(group)
    bc = lambda a, t: np.ascontiguousarray(np.broadcast_to(np.asarray(a, t), (n,)))     # noqa: E731
    return np.asarray(fast._ef.regional_groups(
        bc(group, np.uint8), bc(height, f64), bc(lat, f64), bc(lon, f64), bc(lcls, np.uint8), bc(u, f64),
        [G_DECIDUOUS, G_CONIFER, G_SHRUB, G_ROCK, G_PINE_WEST, G_PALM, G_JUNIPER, G_DESERT_SHRUB, G_DESERT_ROCK,
         G_OAK_WEST, G_TROPICAL], [WC["shrub"], WC["bare"], WC["grass"], WC["built"]]), np.uint8)


def _north_america(lat, lon):
    lat, lon = np.asarray(lat), np.asarray(lon)
    return (lat > 24.0) & (lon > -170.0) & (lon < -50.0)


def _conifer_share(lat, lon):
    """The regional share of conifers (a coarse climate stand-in until a species map). Beyond North America
    (2026-10-15: Douglas firs stood in Punjab's fields): few in the tropics and subtropics, the pines of the
    Mediterranean, the boreal north."""
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    west = lon < -100.0
    share = np.where(west, 0.7, 0.2)
    share = np.where(np.abs(lat) > 45.0, np.maximum(share, 0.6), share)
    alat = np.abs(lat)
    other = np.where(alat < 23.5, 0.02, np.where(alat < 35.0, 0.08, np.where(alat <= 45.0, 0.2, share)))
    mediterranean = (lat > 30.0) & (lat < 46.0) & (lon > -10.0) & (lon < 40.0)
    other = np.where(mediterranean, 0.3, other)
    return np.where(_north_america(lat, lon), share, other)


class Vegetation:
    def __init__(self, md, cache_dir=None):
        self.md = md
        self.dem = None                 # (set by the server: rocks on bare ground by slope, ROCK_SLOPE)
        self.dir = os.path.join(cache_dir, "vegetation", f"v{VERSION}") if cache_dir else None
        self._mem = OrderedDict()
        self._lock = threading.Lock()
        self._cell_locks = {}
        self._tl = threading.local()    # per build thread: .dem_failed (the DEM could not answer: _load keeps no cell)
        self._warm, self._warm_thread = [], None
        self._warm_cv = threading.Condition()

    def cell(self, key):
        """The Items of map-data cell key ((z, x, y) or CellKey)."""
        k = (int(key[1]), int(key[2]))
        with self._lock:
            if k in self._mem:
                self._mem.move_to_end(k)
                return self._mem[k]
            lock = self._cell_locks.setdefault(k, threading.Lock())
        with lock:
            with self._lock:
                if k in self._mem:
                    return self._mem[k]
            items = self._load(*k)
            with self._lock:
                self._mem[k] = items
                while len(self._mem) > MEM_CELLS:
                    self._mem.popitem(last=False)
            return items

    def warm_around(self, lat, lon, radius_m=WARM_M):
        """Build the cells within radius_m of (lat, lon) in the background (nearest first): the tiles
        near a teleport's destination then find them ready instead of waiting ~3.5 s each."""
        d = radius_m / 111320.0
        dl = d / max(math.cos(math.radians(lat)), 0.01)
        try:
            keys = mapcells.cells_for_bbox(lat - d, lon - dl, lat + d, lon + dl, max_cells=64)
        except ValueError:
            return
        keys = sorted(keys, key=lambda k: (k.center()[0] - lat) ** 2 + ((k.center()[1] - lon) * math.cos(math.radians(lat))) ** 2)
        with self._warm_cv:
            for k in keys:
                kk = (int(k.x), int(k.y))
                if kk not in self._warm and not self.ready(k):
                    self._warm.append(kk)
            if self._warm_thread is None:
                self._warm_thread = threading.Thread(target=self._warm_loop, daemon=True, name="veg-warm")
                self._warm_thread.start()
            self._warm_cv.notify()

    def _warm_loop(self):
        while True:
            pacer.hold()                # (a teleport's first block first)
            with self._warm_cv:
                while not self._warm:
                    self._warm_cv.wait()
                x, y = self._warm.pop(0)
            try:
                self.cell((mapcells.ZOOM, x, y))
            except Exception:
                log.exception("vegetation: building cell %s", (x, y))

    def ready(self, key):
        k = (int(key[1]), int(key[2]))
        with self._lock:
            if k in self._mem:
                return True
        p = self._path(*k)
        return bool(p and os.path.exists(p))

    def _path(self, x, y):
        rel = getattr(self.md, "release", "x")
        return os.path.join(self.dir, str(rel), str(x), f"{y}.npz") if self.dir else None

    def _load(self, x, y):
        p = self._path(x, y)
        fields = ("lon", "lat", "height", "radius", "kind", "group", "rank", "yaw", "scale")
        if p and os.path.exists(p):
            with np.load(p, allow_pickle=False) as f:
                return Items(*(f[n] for n in fields))
        self._tl.dem_failed = False
        items = self._build(x, y)
        # (a cell built while the DEM could not answer, offline or no tile, lacks its slope rocks: it serves this
        # session but is not kept, so the next one builds it whole. From cristivasile's PR #295. No DEM at all,
        # self.dem None, is a setting, not a failure)
        if p and not self._tl.dem_failed:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as fh:
                np.savez_compressed(fh, **{n: getattr(items, n) for n in fields})
            staging.replace_cached(tmp, p)
        return items

    # --- the build ------------------------------------------------------------------------------
    def _build(self, x, y):
        key = (mapcells.ZOOM, x, y)
        cd = self.md.cell(key, layers=("canopy", "landcover", "buildings", "roads", "water"))
        chm = cd.layers.get("canopy")
        lc = cd.layers.get("landcover")
        if chm is None:
            return _empty()
        h = np.where(chm.data == (chm.nodata if chm.nodata is not None else 255), 0, chm.data).astype(np.float32)
        # pixel centres (EPSG:3857) of the CHM window, cut to the cell (the window may be larger)
        n_r, n_c = h.shape
        mx = chm.x0 + (np.arange(n_c) + 0.5) * chm.dx
        my = chm.y0 - (np.arange(n_r) + 0.5) * chm.dy
        lon0, lat0, lon1, lat1 = cd.bounds
        ex0, ey0 = _to3857(lon0, lat0)
        ex1, ey1 = _to3857(lon1, lat1)
        ci = (mx >= ex0) & (mx < ex1)
        rj = (my > ey0) & (my <= ey1)
        if not ci.any() or not rj.any():
            return _empty()
        # the blocked pixels: buildings, roads, water (on the CHM grid, with the neighbours' features)
        blocked = np.zeros(h.shape, bool)
        road = np.zeros(h.shape, bool)
        curb = np.zeros(h.shape, bool)      # the roads themselves (+ TREE_PIT_M): where a street tree may not land
        push = np.zeros((3,) + h.shape, np.float32)
        push[2] = np.inf
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                kk = (mapcells.ZOOM, x + dx, y + dy)
                try:
                    c2 = cd if (dx, dy) == (0, 0) else self.md.cell(kk, layers=("buildings", "roads", "water"))
                except Exception as e:          # a neighbour not at hand: its features unmasked
                    log.debug("vegetation: neighbour %s: %s", kk, e)
                    continue
                _mask_polys(c2.layers.get("buildings"), blocked, chm, 1.0)
                _mask_polys(c2.layers.get("water"), blocked, chm, 0.0, kind=3)
                _mask_roads(c2.layers.get("roads"), road, chm)
                _mask_roads(c2.layers.get("roads"), curb, chm, push, clear=TREE_PIT_M)
        # smoothed heights (3 x 3 mean), then the local maxima by height band
        p = np.pad(h, 1, mode="edge")
        hs = sum(p[a:a + n_r, b:b + n_c] for a in range(3) for b in range(3)) / 9.0
        tops = np.zeros(h.shape, bool)
        for lo_m, hi_m, r in ((BUSH_MIN_M, TREE_MIN_M, 1), (TREE_MIN_M, 8.0, 2), (8.0, 15.0, 3), (15.0, 1e9, 4)):
            band = (hs >= lo_m) & (hs < hi_m)
            if band.any():
                tops |= band & (hs >= _shift_max(hs, r))
        tops &= ~blocked
        tops[~rj, :] = False
        tops[:, ~ci] = False
        # street trees: a top over a road corridor (its crown overhangs the street) moves just out of it,
        # onto the verge / sidewalk by the curb, unless that lands on a building, water or another road
        on_road = tops & road
        tops &= ~road
        rj_, ri_ = np.nonzero(on_road)
        if len(rj_):
            nc = np.rint(ri_ + push[0, rj_, ri_]).astype(np.int64)
            nr = np.rint(rj_ + push[1, rj_, ri_]).astype(np.int64)
            ok = (nc >= 0) & (nc < n_c) & (nr >= 0) & (nr < n_r)
            ok[ok] &= ~blocked[nr[ok], nc[ok]] & ~curb[nr[ok], nc[ok]]
            moved = np.zeros(h.shape, bool)
            moved[nr[ok], nc[ok]] = True
            moved[~rj, :] = False
            moved[:, ~ci] = False
            hs = hs.copy()
            hs[nr[ok], nc[ok]] = np.maximum(hs[nr[ok], nc[ok]], hs[rj_[ok], ri_[ok]])     # keep its height
            tops |= moved
        blocked |= road
        jj, ii = np.nonzero(tops)
        jj, ii = _thin(jj, ii, hs[jj, ii], chm.dx)     # one top per crown (flat crowns had several)
        th = hs[jj, ii]
        tlon, tlat = _to4326(mx[ii], my[jj])
        is_tree = th >= TREE_MIN_M
        # the landcover class at each top (rocks / bushes scatter below)
        lcls = lc.sample(tlon, tlat) if lc is not None else np.zeros(len(tlon), np.uint8)
        is_tree &= lcls != WC["water"]
        # canopy heights on bare ground or permanent snow with no vegetated land around: the canopy map misreading
        # bright ground (the Bonneville Salt Flats had ~19k "trees" up to 21 m tall on bare salt, 2026-10-20). By
        # its definition bare land has under 10 % plant cover; an edge tree, or one by an oasis, has some within
        # BARREN_NEAR_M and stays
        barren = np.flatnonzero((lcls == WC["bare"]) | (lcls == WC["snow"])) if lc is not None else []
        if len(barren):
            k = max(0.05, float(np.cos(np.radians(tlat[barren[0]]))))
            dm = BARREN_NEAR_M / k
            near = np.zeros(len(barren), bool)
            for ox, oy in ((dm, 0.0), (-dm, 0.0), (0.0, dm), (0.0, -dm)):
                lo2, la2 = _to4326(mx[ii[barren]] + ox, my[jj[barren]] + oy)
                near |= ~np.isin(lc.sample(lo2, la2), (WC["bare"], WC["snow"], WC["water"]))
            keep = np.ones(len(jj), bool)
            keep[barren[~near]] = False
            jj, ii, th, tlon, tlat, lcls, is_tree = jj[keep], ii[keep], th[keep], tlon[keep], tlat[keep], lcls[keep], \
                is_tree[keep]
        # hashed choices keyed on the global pixel (z17 Web-Mercator index: the CHM grid)
        gx = np.floor((mx[ii] + 20037508.342789244) / chm.dx).astype(np.int64)
        gy = np.floor((20037508.342789244 - my[jj]) / chm.dy).astype(np.int64)
        u1, u2, u3, u4 = (_hash01(gx, gy, s) for s in (1, 2, 3, 4))
        conifer = u1 < _conifer_share(tlat, tlon) * np.clip(th / 12.0, 0.5, 1.2)
        group = np.where(is_tree, np.where(conifer, G_CONIFER, G_DECIDUOUS), G_SHRUB).astype(np.uint8)
        group = regional(group, th, tlat, tlon, lcls, _hash01(gx, gy, 7))
        kind = np.where(is_tree, KIND_TREE, KIND_BUSH).astype(np.uint8)
        jitter = chm.dx * 0.5
        tlon2, tlat2 = _to4326(mx[ii] + (u3 - 0.5) * jitter, my[jj] + (u4 - 0.5) * jitter)
        tree = Items(tlon2, tlat2, th.astype(np.float32), (0.12 * th + 1.0).astype(np.float32), kind, group,
                     _hash01(gx, gy, 5).astype(np.float32), (u2 * 360.0).astype(np.float32),
                     (0.85 + 0.3 * _hash01(gx, gy, 6)).astype(np.float32))
        # squares, pedestrian areas and parking lots: no scattered bushes or rocks (a real tree there stays)
        paved = np.zeros(h.shape, bool)
        try:
            cl = self.md.cell((mapcells.ZOOM, x, y), layers=("land_use", "infrastructure"))
            lu, inf = cl.layers.get("land_use"), cl.layers.get("infrastructure")
            if lu is not None and len(lu) and lu.attrs.get("subtype") is not None:
                _mask_polys(lu, paved, chm, 0.0, select=lu.attrs["subtype"].astype(str) == "pedestrian")
            if inf is not None and len(inf) and inf.attrs.get("class") is not None:
                _mask_polys(inf, paved, chm, 0.0, select=inf.attrs["class"].astype(str) == "parking")
        except Exception as e:                  # not at hand: scattered as before
            log.debug("vegetation: land use of %s: %s", (x, y), e)
        scatter = self._scatter(cd, lc, blocked | paved, chm, hs)
        edge = self._edge(cd, lc, blocked, road, chm, hs)
        return _concat(_concat(tree, scatter), edge)

    def _edge(self, cd, lc, blocked, road, chm, hs):
        """The thicket along forest edges beside roads (EDGE_*), outside the dry West."""
        if lc is None or not road.any():
            return _empty()
        lon0, lat0, lon1, lat1 = cd.bounds
        if _dry_west(np.array([(lat0 + lat1) / 2]), np.array([(lon0 + lon1) / 2]))[0]:
            return _empty()
        band = _dilate(road, max(1, int(round(EDGE_M / chm.dx)))) & ~blocked
        if not band.any():
            return _empty()
        ex0, ey0 = _to3857(lon0, lat0)
        ex1, ey1 = _to3857(lon1, lat1)
        s = EDGE_CELL_M
        GI, GJ = np.meshgrid(np.arange(math.floor(ex0 / s), math.ceil(ex1 / s)),
                             np.arange(math.floor(ey0 / s), math.ceil(ey1 / s)))
        GI, GJ = GI.ravel(), GJ.ravel()
        px = (GI + _hash01(GI, GJ, 21)) * s
        py = (GJ + _hash01(GI, GJ, 22)) * s
        c_ = np.floor((px - chm.x0) / chm.dx).astype(np.int64)
        r_ = np.floor((chm.y0 - py) / chm.dy).astype(np.int64)
        ok = (px >= ex0) & (px < ex1) & (py >= ey0) & (py < ey1) & (c_ >= 0) & (c_ < hs.shape[1]) \
            & (r_ >= 0) & (r_ < hs.shape[0])
        ok[ok] &= band[r_[ok], c_[ok]] & (hs[r_[ok], c_[ok]] >= TREE_MIN_M)     # under the forest's canopy
        ok &= _hash01(GI, GJ, 23) < EDGE_P
        if not ok.any():
            return _empty()
        GI, GJ, px, py = GI[ok], GJ[ok], px[ok], py[ok]
        lon, lat = _to4326(px, py)
        cls = lc.sample(lon, lat)
        forest = cls == WC["tree"]          # not under street trees in town
        if not forest.any():
            return _empty()
        GI, GJ, lon, lat, cls = GI[forest], GJ[forest], lon[forest], lat[forest], cls[forest]
        size = 1.2 + 1.8 * _hash01(GI, GJ, 24)
        n = len(lon)
        return Items(lon, lat, size.astype(np.float32), (size * 0.6).astype(np.float32),
                     np.full(n, KIND_BUSH, np.uint8),
                     regional(np.full(n, G_SHRUB, np.uint8), size, lat, lon, cls, _hash01(GI, GJ, 25)),
                     _hash01(GI, GJ, 26).astype(np.float32), (_hash01(GI, GJ, 27) * 360.0).astype(np.float32),
                     (0.8 + 0.4 * _hash01(GI, GJ, 28)).astype(np.float32))

    def _slope(self, px, py):
        """The ground's slope (rise over run) at Web Mercator points (px, py), from the DEM ROCK_SLOPE_D m either side
        in x and y; None when the DEM cannot say."""
        k = np.cos(np.radians(_to4326(px[:1], py[:1])[1][0]))       # Web Mercator m per ground m (one cell)
        d = ROCK_SLOPE_D / max(k, 0.05)
        x = np.concatenate([px + d, px - d, px, px])
        y = np.concatenate([py, py, py + d, py - d])
        lon, lat = _to4326(x, y)
        try:
            z = np.asarray(self.dem.sample(lat, lon, ROCK_SLOPE_ZOOM), np.float64).reshape(4, len(px))
        except Exception as e:          # (offline, no tile): rocks as without a DEM
            log.debug("vegetation: no slope: %s", e)
            self._tl.dem_failed = True
            return None
        return np.hypot(z[0] - z[1], z[2] - z[3]) / (2 * ROCK_SLOPE_D)

    def _scatter(self, cd, lc, blocked, chm, hs):
        """Bushes and rocks on a hashed SCATTER_CELL_M grid over shrubland, grass and bare ground."""
        if lc is None:
            return _empty()
        lon0, lat0, lon1, lat1 = cd.bounds
        ex0, ey0 = _to3857(lon0, lat0)
        ex1, ey1 = _to3857(lon1, lat1)
        s = SCATTER_CELL_M
        gi = np.arange(math.floor(ex0 / s), math.ceil(ex1 / s))
        gj = np.arange(math.floor(ey0 / s), math.ceil(ey1 / s))
        GI, GJ = np.meshgrid(gi, gj)
        GI, GJ = GI.ravel(), GJ.ravel()
        px = (GI + _hash01(GI, GJ, 11)) * s
        py = (GJ + _hash01(GI, GJ, 12)) * s
        keep = (px >= ex0) & (px < ex1) & (py >= ey0) & (py < ey1)
        px, py, GI, GJ = px[keep], py[keep], GI[keep], GJ[keep]
        lon, lat = _to4326(px, py)
        cls = lc.sample(lon, lat)
        u = _hash01(GI, GJ, 13)
        pb = np.zeros(len(px))
        pr = np.zeros(len(px))
        for c, (b, r) in SCATTER_P.items():
            pb = np.where(cls == c, b, pb)
            pr = np.where(cls == c, r, pr)
        # on the CHM grid: not blocked, no canopy there already (the trees are the CHM's)
        c_ = np.floor((px - chm.x0) / chm.dx).astype(np.int64)
        r_ = np.floor((chm.y0 - py) / chm.dy).astype(np.int64)
        ok = (c_ >= 0) & (c_ < hs.shape[1]) & (r_ >= 0) & (r_ < hs.shape[0])
        free = np.zeros(len(px), bool)
        free[ok] = ~blocked[r_[ok], c_[ok]] & ((hs[r_[ok], c_[ok]] < BUSH_MIN_M) | (cls[ok] == UNDERSTORY))
        # bare land in a town is a square, a lot or a building site, not a rock field (desert rocks stood on
        # Mexico City's Zocalo, 2026-10-15): none where built-up land is around (2 of 4 points BUILT_NEAR_M away)
        bare = np.flatnonzero(free & (cls == WC["bare"]))
        if len(bare):
            near = np.zeros(len(bare), np.int64)
            for dx, dy in ((BUILT_NEAR_M, 0.0), (-BUILT_NEAR_M, 0.0), (0.0, BUILT_NEAR_M), (0.0, -BUILT_NEAR_M)):
                lo2, la2 = _to4326(px[bare] + dx, py[bare] + dy)
                near += lc.sample(lo2, la2) == WC["built"]
            free[bare[near >= 2]] = False
        rock = free & (u < pr)
        bare_rock = np.flatnonzero(rock & (cls == WC["bare"]))
        if len(bare_rock) and self.dem is not None:
            slope = self._slope(px[bare_rock], py[bare_rock])
            if slope is not None:
                lo_s, hi_s = ROCK_SLOPE
                keep = _hash01(GI[bare_rock], GJ[bare_rock], 19) < np.clip((slope - lo_s) / (hi_s - lo_s), 0.0, 1.0)
                rock[bare_rock[~keep]] = False
                free[bare_rock[~keep]] = False     # (nor a bush there: bare ground stays bare)
        bush = free & ~rock & (u < pr + pb)
        sel = rock | bush
        n = int(sel.sum())
        size = 0.4 + 1.6 * _hash01(GI[sel], GJ[sel], 14) ** 2
        return Items(lon[sel], lat[sel], size.astype(np.float32), (size * 0.6).astype(np.float32),
                     np.where(rock[sel], KIND_ROCK, KIND_BUSH).astype(np.uint8),
                     regional(np.where(rock[sel], G_ROCK, G_SHRUB), size, lat[sel], lon[sel], cls[sel],
                              _hash01(GI[sel], GJ[sel], 18)),
                     _hash01(GI[sel], GJ[sel], 15).astype(np.float32),
                     (_hash01(GI[sel], GJ[sel], 16) * 360.0).astype(np.float32),
                     (0.8 + 0.4 * _hash01(GI[sel], GJ[sel], 17)).astype(np.float32)) if n else _empty()


def _thin(jj, ii, h, px):
    """Non-maximum suppression: the tallest tops first, each removing the others within its crown
    radius (0.12 h + 1 m, times CROWN_SPACING). Deterministic (ties broken by position). earthfast thin_tops
    (2026-10-23: the same tops as the Python loop it replaced)."""
    if len(jj) == 0:
        return jj, ii
    fast.require(64, "thin_tops")
    keep = fast._ef.thin_tops(np.ascontiguousarray(jj, np.int64), np.ascontiguousarray(ii, np.int64),
                              np.ascontiguousarray(h, np.float32), float(CROWN_SPACING), float(px))
    return jj[keep], ii[keep]


def _empty():
    z = np.zeros(0)
    return Items(z, z, z.astype(np.float32), z.astype(np.float32), z.astype(np.uint8), z.astype(np.uint8),
                 z.astype(np.float32), z.astype(np.float32), z.astype(np.float32))


def _concat(a, b):
    return Items(*(np.concatenate([getattr(a, f), getattr(b, f)]) for f in
                   ("lon", "lat", "height", "radius", "kind", "group", "rank", "yaw", "scale")))


R = 6378137.0


def _to3857(lon, lat):
    lon = np.asarray(lon, np.float64)
    lat = np.clip(np.asarray(lat, np.float64), -85.0511287798066, 85.0511287798066)
    return np.radians(lon) * R, np.log(np.tan(np.pi / 4 + np.radians(lat) / 2)) * R


def _to4326(x, y):
    x = np.asarray(x, np.float64)
    y = np.asarray(y, np.float64)
    return np.degrees(x / R), np.degrees(2 * np.arctan(np.exp(y / R)) - np.pi / 2)


def _mask_polys(f, mask, chm, grow_m, kind=3, select=None):
    """Mark the polygons of Features f (buildings, water) on the CHM-grid mask (bool, C order): every pixel whose
    centre lies inside a part (its rings XOR'd, matplotlib's point-in-path test), within the part's box grown by
    grow_m (select: a bool per feature, only those). The native module's (earthfast mask_polys, 2026-10-23: the same
    pixels as the per-polygon Python loop it replaced, 13x faster: half of a tree cell's build)."""
    if f is None:
        return
    fast.require(62, "vegetation mask_polys")
    feats = np.flatnonzero((f.kind == kind) & (True if select is None else select)).astype(np.int64)
    if not len(feats):
        return
    fast._ef.mask_polys(mask.view(np.uint8), np.ascontiguousarray(f.lonlat, np.float64),
                        np.ascontiguousarray(f.coord_off, np.int64), np.ascontiguousarray(f.ring_off, np.int64),
                        np.ascontiguousarray(f.part_off, np.int64), feats, float(chm.x0), float(chm.y0),
                        float(chm.dx), float(chm.dy), grow_m / chm.dx)


def _mask_roads(f, mask, chm, push=None, clear=ROAD_CLEAR_M):
    """Mark road corridors (half width by class + clear) on the CHM-grid mask (footpaths left out). push (optional, (3, rows,
    cols) float32: dx, dy in px and the distance to the nearest road, inf where none) gets, per corridor
    pixel, the move that takes it just out of its nearest road's corridor (street trees: vegetation._build)."""
    if f is None:
        return
    n_r, n_c = mask.shape
    cls = f.attrs.get("class", np.full(len(f.kind), "", object))
    fast.require(63, "road_segments")
    sel = [i for i in np.flatnonzero(f.kind == 2) if str(cls[i]) not in PATH_CLASSES]
    if not sel:
        return
    hw_m = np.array([ROAD_HALF_WIDTH_M.get(str(cls[i]), ROAD_HALF_DEFAULT_M) + clear for i in sel], np.float64)
    # every ring's segments on the CHM grid, its half width in grid pixels (a 3857 metre is 1 / cos(lat) of a ground
    # metre), rings off the grid left out (earthfast road_segments, 2026-10-23: the per-ring Python loop it
    # replaced gave the same segments); then the distances and the nearest road's push, without the GIL
    segs, hws = fast._ef.road_segments(np.ascontiguousarray(f.lonlat, np.float64),
                                       np.ascontiguousarray(f.coord_off, np.int64),
                                       np.ascontiguousarray(f.ring_off, np.int64),
                                       np.ascontiguousarray(f.part_off, np.int64), np.asarray(sel, np.int64), hw_m,
                                       float(chm.x0), float(chm.y0), float(chm.dx), float(chm.dy), n_r, n_c)
    if len(hws):
        fast._ef.road_corridors(mask, push, np.ascontiguousarray(segs), np.ascontiguousarray(hws))


# --- tiles for the mod --------------------------------------------------------------------------------
TILE = 256.0                # m, as the road tiles (roads.tile_bbox)
TILE_ROWS_MAX = 6000        # items a tile sends at most, the lowest ranks: the most the mod shows of one (trees.lua
                            # TILE_CAP). Dense forest has 10-20k a tile: Bodø's 145 tiles held 1.66 M rows in the game's
                            # Lua (394 MB of heap, 1.25 s garbage collection pauses on a Steam Deck; GitHub #66)
SEND_M = 1280.0             # tiles this close to the player (Chebyshev) are sent ...
DROP_M = 1536.0             # ... and dropped beyond this
BUILD_PER_POSE = 8


class JsonRows(str):
    """tile_items' rows already as their JSON text (as_json; write_tile writes it as is); n: how many; audio, biome:
    the tile's forest ambience (_ambience)."""
    n = 0
    audio = ()
    biome = (0, 0)


# The forest ambience (the mod's ambience.lua feeds the stock core_audioForest): the trees and bushes of a tile in
# squares of AUDIO_CELL_M (12 a tile; the stock nodes are ~20 m apart), sized by height as the stock classes (a third
# and two thirds of the 95th percentile's volume: 0.69 and 0.87 of its height, ~22 m)
AUDIO_CELL_M = 256.0 / 12
AUDIO_H_MID = 15.0
AUDIO_H_LARGE = 19.0
AUDIO_SOUND = [g for g in range(len(GROUPS)) if g not in (G_ROCK, G_DESERT_ROCK)]


def _ambience(group, e, n, z, height, lat, lon):
    """(audio, biome) of a tile's items: audio its sound nodes as one flat list of [x, y, z, depth, size, count] rows
    (earthfast.audio_nodes), biome its ambience set and variant (earthfast.audio_biome)."""
    fast.require(80, "audio_nodes")
    f64 = lambda a: np.ascontiguousarray(a, np.float64)     # noqa: E731
    g = np.ascontiguousarray(group, np.uint8)
    nodes = fast._ef.audio_nodes(g, f64(e), f64(n), f64(z), f64(height), AUDIO_SOUND, AUDIO_CELL_M, AUDIO_H_MID,
                                 AUDIO_H_LARGE)
    nodes[:, :4] = np.round(nodes[:, :4], 1)
    nodes[:, 4] = np.round(nodes[:, 4], 2)
    import climate
    biome = fast._ef.audio_biome(g, float(lat), float(lon), [G_DESERT_SHRUB, G_DESERT_ROCK, G_JUNIPER],
                                 climate.koppen_at(float(lat), float(lon)))
    return nodes.ravel().tolist(), list(biome)


def _no_items(as_json, lat, lon):
    if not as_json:
        return []
    out = JsonRows("[]")
    out.audio, out.biome = _ambience([], [], [], [], [], lat, lon)
    return out


def tile_items(frame, tx, ty, veg, cell_ready, elev, sea=None, as_json=False):
    """The items of anchor tile (tx, ty), each [group, x, y, z, yaw, scale, height, rank] in the anchor
    frame, the ground z from elev(lat, lon) (the DEM shaped as the block's ground: runways, carved
    water). None when a cell under it is not at hand yet (veg cells are built in the background)."""
    import roads as roadsmod
    x0, y0, x1, y1 = roadsmod.tile_bbox(tx, ty)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), np.zeros(4))
    keys = mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()))
    clat, clon = float(la.mean()), float(lo.mean())
    parts = []
    for k in keys:
        if not cell_ready(k):
            return None
        parts.append(veg.cell(k))
    items = parts[0] if len(parts) == 1 else _concat_all(parts)
    if len(items) == 0:
        return _no_items(as_json, clat, clon)
    e, n, _ = frame.geodetic_to_enu(items.lat, items.lon, np.zeros(len(items)))
    e, n = np.asarray(e), np.asarray(n)
    sel = (e >= x0) & (e < x1) & (n >= y0) & (n < y1)
    if not sel.any():
        return _no_items(as_json, clat, clon)
    it = items.take(sel)
    e, n = e[sel], n[sel]
    if sea is not None:                 # nothing in the water (lakes, rivers, sea: their carved beds)
        dry = sea.water(it.lat, it.lon)[0] <= 0
        it, e, n = it.take(dry), e[dry], n[dry]
    if len(it) == 0:
        return _no_items(as_json, clat, clon)
    if len(it) > TILE_ROWS_MAX:         # dense forest: the lowest ranks only (the mod shows no more of a tile)
        keep = np.sort(np.argpartition(it.rank, TILE_ROWS_MAX - 1)[:TILE_ROWS_MAX])
        it, e, n = it.take(keep), e[keep], n[keep]
    zg = np.asarray(elev(it.lat, it.lon), np.float64)
    _, _, z = frame.geodetic_to_enu(it.lat, it.lon, zg)
    z = _sink_rocks(frame, it, e, n, np.asarray(z, np.float64), elev)
    if as_json:
        # Rust: the same text json.dumps would write for the rows below, without a Python list per tree
        fast.require(0, "veg_rows_json")
        f64 = lambda a: np.ascontiguousarray(a, np.float64)     # noqa: E731
        out = JsonRows(fast._ef.veg_rows_json(np.ascontiguousarray(it.group, np.int64), f64(e), f64(n), f64(z),
                                               f64(it.yaw), f64(it.scale), f64(it.height), f64(it.rank)))
        out.n = len(it)
        out.audio, out.biome = _ambience(it.group, e, n, z, it.height, clat, clon)
        return out
    return [[int(g), round(float(a), 2), round(float(b), 2), round(float(c), 2), round(float(yw), 1),
             round(float(sc), 3), round(float(h), 1), round(float(r), 4)]
            for g, a, b, c, yw, sc, h, r in zip(it.group, e, n, np.asarray(z), it.yaw, it.scale, it.height, it.rank)]


ROCK_SINK = 0.35            # a rock sinks this share of its height per unit of ground slope (1 = 45 degrees) ...
ROCK_SINK_MAX = 0.6         # ... up to this share: on a steep slope its downhill side stood clear of the ground and
                            # its flat underside showed (Stelvio pass, the user's report 2026-10-17)
SLOPE_STEP_M = 1.5


def _sink_rocks(frame, it, e, n, z, elev):
    """z with the rocks lowered into sloping ground (ROCK_SINK)."""
    rock = (it.group == G_ROCK) | (it.group == G_DESERT_ROCK)
    if not rock.any():
        return z
    k = np.flatnonzero(rock)
    d = SLOPE_STEP_M
    ex = np.concatenate([e[k] + d, e[k] - d, e[k], e[k]])
    ny = np.concatenate([n[k], n[k], n[k] + d, n[k] - d])
    la, lo, _ = frame.enu_to_geodetic(ex, ny, np.zeros(len(ex)))
    _, _, zz = frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))
    zz = np.asarray(zz, np.float64).reshape(4, len(k))
    slope = np.hypot(zz[0] - zz[1], zz[2] - zz[3]) / (2 * d)
    z = z.copy()
    z[k] -= np.minimum(ROCK_SINK * slope, ROCK_SINK_MAX) * it.height[k]
    return z


def _concat_all(parts):
    out = parts[0]
    for p in parts[1:]:
        out = _concat(out, p)
    return out


FLAT_STRIDE = 8             # numbers an item in a flat tile: tile_items' row, in its order


def flat_json(rows):
    """tile_items' rows (a list, or its JsonRows text) as the JSON text of one flat array, FLAT_STRIDE numbers an item,
    the same numbers as json.dumps writes in the rows: no table per item in the game's Lua, which every garbage
    collection walks (trees.lua). The JsonRows text is flattened as text, without Python objects."""
    import json
    if isinstance(rows, JsonRows):
        return "[" + rows[2:-2].replace("],[", ",") + "]" if len(rows) > 2 else "[]"
    return json.dumps([v for r in rows for v in r], separators=(",", ":"))


def write_tile(out_dir, vfs_dir, aid, tx, ty, rows, flat=False):
    """The tile's file: {"groups", "items": rows}, or with flat (a mod that reads it: hello.vegFlat) {"groups",
    "stride": FLAT_STRIDE, "flat": [...]} (flat_json) under its own name. (vfs path, items)."""
    import json
    name = f"veg_{aid}_{tx}_{ty}.flat.json" if flat else f"veg_{aid}_{tx}_{ty}.json"
    path = os.path.join(out_dir, name)
    tmp = staging.tmp_for(path)
    with open(tmp, "w") as f:
        # (the tile's sound: the forest cloud's nodes and biome, ambience.lua; tile_items as_json's rows carry them)
        sound = {"audio": rows.audio, "biome": rows.biome} if isinstance(rows, JsonRows) else {}
        if flat:
            head = dict({"groups": list(GROUPS)}, **sound, stride=FLAT_STRIDE, flat=[])
            f.write(json.dumps(head, separators=(",", ":"))[:-3] + flat_json(rows) + "}")
        elif isinstance(rows, JsonRows):  # (tile_items as_json: the items' text, as json.dumps writes them)
            head = dict({"groups": list(GROUPS)}, **sound, items=[])
            f.write(json.dumps(head, separators=(",", ":"))[:-3] + rows + "}")
        else:
            f.write(json.dumps({"groups": list(GROUPS), "items": rows}, separators=(",", ":")))  # the C encoder
    staging.replace(tmp, path)
    return f"{vfs_dir}/{name}", rows.n if isinstance(rows, JsonRows) else len(rows)


class Tracker:
    """Per connection: which tiles of a kind the mod has (as roads.Tracker): vegetation tiles by default
    (messages veg_tile / veg_drop); the far buildings use one too (buildings.FAR_*)."""

    def __init__(self, send_m=SEND_M, drop_m=DROP_M, per_pose=BUILD_PER_POSE, kind="veg", skip_m=None, group=None,
                 unit=1, inner=None):
        # group G (the far buildings under the skyline tiles): every tile of a G x G group whose square comes within
        # send_m is wanted (a skyline tile shows only while none of its tiles do: no overlap, no hole). unit U (the
        # skyline tiles): keys are U x U squares of road tiles; inner (m, G): squares whose G-group comes within m are
        # not wanted (the far buildings' own groups)
        self.group, self.unit, self.inner = group, unit, inner
        self.aid = None
        self.sent = set()
        self.building = set()
        self.send_m, self.drop_m, self.per_pose, self.kind = send_m, drop_m, per_pose, kind
        # skip_m: tiles within this (Chebyshev, as roads.tiles_near) are not built: the far buildings under the road
        # tiles, which carry the full buildings (the mod would hide them at once; ~36 of a teleport's ~196 tiles)
        self.skip_m = skip_m
        # its own threads (per_pose): in the loop's shared executor the tiles queued behind other trackers' builds
        # blocked on the road processes, which re-plan every road cell for a new anchor: after a rebase at 90 mph
        # not one tree tile came for minutes (2026-10-17, salt flats)
        self.pool = None
        self.futs = {}

    def close(self):
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)

    def progress(self):
        """(done, wanted) of the tiles round the car at the last pose (done: sent to the mod); (0, 0) before one."""
        car = getattr(self, "car", ())
        return sum(1 for k in car if k in self.sent), len(car)

    def on_pose(self, conn, aid, x, y, loop, build, view=None, foci=()):
        """As roads.Tracker.on_pose: the tiles within send_m of the car (x, y) and of the camera when it is away
        (view), the car's first; dropped beyond drop_m of both. foci: other vehicles' collision foci (x, y, r, ...)
        whose trees collide: the tiles within r + 128 m of each, after the car's and camera's, kept within r + 256 m."""
        import roads as roadsmod
        if aid != self.aid:
            for f in self.futs.values():
                f.cancel()                  # the old anchor's that have not started
            self.aid, self.sent, self.building, self.futs = aid, set(), set(), {}
        if self.pool is None:
            from concurrent.futures import ThreadPoolExecutor
            self.pool = ThreadPoolExecutor(self.per_pose, thread_name_prefix=f"{self.kind}-tile")
        spots = [(x, y)] + ([tuple(view)] if view else [])
        skip = {k for p in spots for k in roadsmod.tiles_near(*p, self.skip_m)} if self.skip_m else ()
        if self.inner is not None:          # (the skyline: not where the far buildings' groups are)
            skip = {k for p in spots for k in self._near(*p, self.inner[0], unit=self.unit, group=self.inner[1])}
        car = set(self._near(x, y, self.send_m))
        self.car = car - set(skip)          # (progress: the loading line's tiles)
        seen = set(self._near(*view, self.send_m)) - car if view else set()
        others = set()
        for f in foci:
            others.update(self._near(f[0], f[1], min(self.send_m, f[2] + roadsmod.TILE / 2)))
        others -= car | seen
        want = [k for k in car | seen | others if k not in self.sent and k not in self.building and k not in skip]
        if self.unit == 1:
            car_key = roadsmod.view_key(conn, aid, x, y)
            cam_key = roadsmod.view_key(conn, aid, *view) if seen else None
        else:                               # (keys in larger squares: nearest first)
            car_key = lambda k: self._dist(k, x, y)     # noqa: E731
            cam_key = (lambda k: self._dist(k, *view)) if seen else None    # noqa: E731

        def focus_d(k):
            return min(self._dist(k, f[0], f[1]) for f in foci)
        want.sort(key=lambda k: (2, focus_d(k)) if k in others else (1, cam_key(k)) if k in seen else (0, car_key(k)))
        # at most per_pose in flight: each waits in a thread of the loop's shared executor, and a teleport's
        # hundreds (one per pose, uncapped) held every thread while the road tiles under the car waited for one
        # (Sinaia 2026-10-15: bare ground for minutes)
        for tx, ty in want[:max(0, self.per_pose - len(self.building))]:
            self.building.add((tx, ty))
            fut = self.futs[(tx, ty)] = loop.run_in_executor(self.pool, build, aid, tx, ty)
            fut.add_done_callback(lambda f, k=(tx, ty), a=aid: self._built(conn, a, k, f))
        for k in [k for k in self.sent if all(self._dist(k, *p) > self.drop_m for p in spots)
                  and all(self._dist(k, f[0], f[1]) > f[2] + roadsmod.TILE for f in foci)]:
            self.sent.discard(k)
            conn.send({"type": f"{self.kind}_drop", "anchorId": aid, "key": f"{k[0]}_{k[1]}"})

    def _near(self, x, y, m, unit=None, group=None):
        """Keys (tiles, or unit x unit squares of tiles) whose square, or whose group's square, comes within m."""
        import roads as roadsmod
        unit = self.unit if unit is None else unit
        group = self.group if group is None else group
        if group:                           # (whole groups of `group` tiles; keys in `unit` squares)
            gs = roadsmod.TILE * group
            g0x, g1x = math.floor((x - m) / gs), math.floor((x + m) / gs)
            g0y, g1y = math.floor((y - m) / gs), math.floor((y + m) / gs)
            per = group // unit
            return [(gx * per + i, gy * per + j) for gx in range(g0x, g1x + 1) for gy in range(g0y, g1y + 1)
                    for i in range(per) for j in range(per)]
        if unit == 1:
            return roadsmod.tiles_near(x, y, m)
        us = roadsmod.TILE * unit
        u0x, u1x = math.floor((x - m) / us), math.floor((x + m) / us)
        u0y, u1y = math.floor((y - m) / us), math.floor((y + m) / us)
        return [(ux, uy) for ux in range(u0x, u1x + 1) for uy in range(u0y, u1y + 1)]

    def _dist(self, k, x, y):
        """m (Chebyshev) from (x, y) to key k's square, or to its group's (the far buildings under the skyline)."""
        import roads as roadsmod
        size = roadsmod.TILE * (self.group or self.unit)
        per = (self.group // self.unit) if self.group else 1
        x0, y0 = (k[0] // per) * size, (k[1] // per) * size
        return max(x0 - x, x - (x0 + size), y0 - y, y - (y0 + size), 0.0)

    def _built(self, conn, aid, key, fut):
        if aid != self.aid:                 # (an old anchor's: the same key may be in flight for the new one)
            return
        self.building.discard(key)
        self.futs.pop(key, None)
        if fut.cancelled():
            return
        try:
            res = fut.result()
        except Exception as e:
            log.debug("%s tile %s: %s: %s", self.kind, key, type(e).__name__, e)
            return
        if res is None:                 # its cells not at hand yet: asked again on a later pose
            return
        path, n = res
        self.sent.add(key)
        m = {"type": f"{self.kind}_tile", "anchorId": aid, "key": f"{key[0]}_{key[1]}", "path": path, "n": n}
        out_dir = getattr(getattr(conn, "s", None), "out_dir", None)
        data = staging.inline_json(out_dir, path) if out_dir else None
        if data is not None:                # (the game need not open the file: staging.inline_json)
            m["data"] = data
        pool = getattr(getattr(conn, "s", None), "pool_shapes", None)
        if self.kind in ("bldfar", "bldsky", "brfar") and pool is not None:     # its mesh at a pooled shape path (matslots.py)
            slots = pool(conn, {"shape": f"{self.kind}_{aid}_{key[0]}_{key[1]}.cdae"})
            if slots:
                m["slots"] = slots
        conn.send(m)
