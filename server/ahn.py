"""The Netherlands' height at 0.5 m from the AHN (Actueel Hoogtebestand Nederland, the national lidar; PDOK, CC0), for
the terrain and the decks near the player (--nl-bgt on).

Mapterhorn's tiles carry the AHN too, but resampled to 3-6 m: at 's-Gravendijkwal x Droogleever Fortuynplein the 15 m
wide street between two sunken carriageways read as one 40 m pit, every deck's edge as a 10 m slope (2026-10-21). The
AHN's own 0.5 m grids:

    dtm   the ground ("maaiveld"): buildings, bridges and water left out (no data there)
    dsm   the surface: the decks, roofs and trees on it

    a = Ahn(cache_dir, fetcher)
    dtm, hole, dsm = a.sample(lat, lon)   # per point: the ground (holes filled), whether it was a hole, the surface
    dem = AhnDem(base_dem, a)            # a dem.Dem whose sample() is the AHN's ground where it has it (zoom >= MIN_ZOOM)

A hole is filled from its rim (onion rings, then smoothed): under a deck that is the road under it, its trench's floor
on both sides (the underpass is in the ground as it comes); under a building the ground round it. Water is a hole as
well: AhnDem keeps the lower of the fill and the base DEM there (a canal's fill from its quays would be its quays).
Tiles of TILE_M in RD (EPSG:28992), fetched with MARGIN_M round them (a hole at a tile's edge filled from both sides),
kept under <cache>/regional/ahn/v<VERSION>/<x>_<y>.npz.
"""
import io
import logging
import math
import os
import threading
from collections import OrderedDict

import numpy as np

log = logging.getLogger("earthd.ahn")

VERSION = 5
WCS = ("https://service.pdok.nl/rws/ahn/wcs/v1_0?SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage&COVERAGEID={cov}"
       "&SUBSET=x({x0},{x1})&SUBSET=y({y0},{y1})&FORMAT=image/tiff")
RES = 0.5                   # m per pixel
TILE_M = 250.0              # m: a cached tile's square (500 x 500 px)
MARGIN_M = 50.0             # m fetched round it for the fill
FILL_ROUNDS = 400           # onion rings at most (200 m from a rim)
SMOOTH_ROUNDS = 60          # then the filled cells smoothed (Jacobi) this often
MEM_TILES = 48
MIN_ZOOM = 14               # AhnDem: coarser samples (the far tiles) keep the base DEM
DECK_RISE_M = 2.0           # a hole whose surface is this far over the filled ground is a deck (deck_top)
DECK_AT_GRADE_M = 1.0          # ... and within this of the higher side round it (not a roof)
NODATA = 1e30


def wgs_to_rd(lat, lon):
    """RD New (EPSG:28992) x, y of WGS84 lat, lon (Schreutelkamp & Strang van Hees' polynomial: < 1 m)."""
    dphi = 0.36 * (np.asarray(lat, np.float64) - 52.15517440)
    dlam = 0.36 * (np.asarray(lon, np.float64) - 5.38720621)
    x = 155000.0 + (190094.945 * dlam - 11832.228 * dphi * dlam - 114.221 * dphi ** 2 * dlam - 32.391 * dlam ** 3
                    - 0.705 * dphi - 2.340 * dphi ** 3 * dlam - 0.608 * dphi * dlam ** 3 - 0.008 * dlam ** 2
                    + 0.148 * dphi ** 2 * dlam ** 3)
    y = 463000.0 + (309056.544 * dphi + 3638.893 * dlam ** 2 + 73.077 * dphi ** 2 - 157.984 * dphi * dlam ** 2
                    + 59.788 * dphi ** 3 + 0.433 * dlam - 6.439 * dphi ** 2 * dlam ** 2 - 0.032 * dphi * dlam
                    + 0.092 * dlam ** 4 - 0.054 * dphi * dlam ** 4)
    return x, y


def rd_to_wgs(x, y, lat0=52.15517440, lon0=5.38720621):
    """WGS84 lat, lon of RD x, y (wgs_to_rd inverted: Newton on its local derivative, mm in four steps)."""
    x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
    lat = np.full(np.broadcast(x, y).shape, lat0, np.float64)
    lon = np.full(lat.shape, lon0, np.float64)
    for _ in range(6):
        rx, ry = wgs_to_rd(lat, lon)
        d = 1e-5
        rx1, ry1 = wgs_to_rd(lat + d, lon)
        rx2, ry2 = wgs_to_rd(lat, lon + d)
        j11, j12, j21, j22 = (rx1 - rx) / d, (rx2 - rx) / d, (ry1 - ry) / d, (ry2 - ry) / d
        det = j11 * j22 - j12 * j21
        ex, ey = x - rx, y - ry
        lat = lat + (j22 * ex - j12 * ey) / det
        lon = lon + (-j21 * ex + j11 * ey) / det
    return lat, lon


def covers(lat, lon):
    """Inside the AHN's box (the Netherlands; points over the sea or the neighbours get no data from it)."""
    return 3.2 <= lon <= 7.3 and 50.7 <= lat <= 53.6


def fill_low(a, rounds=FILL_ROUNDS):
    """a (2-D, NaN = no data) with its holes filled ring by ring with the lowest of the known 4-neighbours: what lies
    under a deck is the lower of its sides (a trench's floor runs on under the street over it; the mean of its rim,
    the street's 3 m and the floor's -1.3 m, left 1.5 m under the deck: a tunnel blocked). Pure."""
    a = np.asarray(a, np.float32)
    hole = ~np.isfinite(a)
    if not hole.any() or hole.all():
        return a
    out = np.where(hole, np.inf, a).astype(np.float32)
    for _ in range(rounds):         # (until the lowest of its rim has reached every cell of a hole: ring by ring the
        p = np.pad(out, 1, mode="constant", constant_values=np.inf)   # street beside the deck came in first)
        m = np.minimum.reduce([p[:-2, 1:-1], p[2:, 1:-1], p[1:-1, :-2], p[1:-1, 2:]])
        lower = hole & (m < out)
        if not lower.any():
            break
        out[lower] = m[lower]
    out[~np.isfinite(out)] = np.nan
    return out


def ground(dtm, dsm, decks=False):
    """The ground under the AHN's holes: under a deck (its surface within DECK_AT_GRADE_M of the higher side round it, the
    street it carries, yet DECK_RISE_M over the lower side's) the low fill, the road under it; elsewhere (a building:
    its roof well over the ground round it) the smooth fill; decks: and the deck mask. Pure."""
    hole = ~np.isfinite(dtm)
    if not hole.any():
        g = np.asarray(dtm, np.float32)
        return (g, np.zeros(g.shape, bool)) if decks else g
    mean = fill(dtm)
    low = fill_low(dtm)
    high = -fill_low(-np.asarray(dtm, np.float32))
    deck = hole & np.isfinite(dsm) & (np.abs(dsm - high) <= DECK_AT_GRADE_M) & (dsm - low >= DECK_RISE_M)
    g = np.where(deck, low, mean).astype(np.float32)
    return (g, deck) if decks else g


def fill(a, rounds=FILL_ROUNDS, smooth=SMOOTH_ROUNDS):
    """a (2-D, NaN = no data) with its holes filled from their rims: the mean of the known 4-neighbours ring by ring,
    then the filled cells smoothed toward the mean of their neighbours (a smooth surface between the rims, not the
    rings' terraces). Cells further than `rounds` from any data stay NaN. Pure."""
    a = np.asarray(a, np.float32)
    hole = ~np.isfinite(a)
    if not hole.any() or hole.all():
        return a
    out = np.where(hole, 0.0, a).astype(np.float32)
    known = ~hole
    for _ in range(rounds):
        todo = ~known
        if not todo.any():
            break
        p = np.pad(out, 1, mode="edge")
        v = np.pad(known, 1, mode="edge").astype(np.float32)
        s = (p[:-2, 1:-1] * v[:-2, 1:-1] + p[2:, 1:-1] * v[2:, 1:-1] + p[1:-1, :-2] * v[1:-1, :-2]
             + p[1:-1, 2:] * v[1:-1, 2:])
        n = v[:-2, 1:-1] + v[2:, 1:-1] + v[1:-1, :-2] + v[1:-1, 2:]
        got = todo & (n > 0)
        if not got.any():
            break
        out[got] = s[got] / n[got]
        known |= got
    filled = hole & known
    for _ in range(smooth):
        p = np.pad(out, 1, mode="edge")
        m = (p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:]) / 4.0
        out[filled] = m[filled]
    out[~known] = np.nan
    return out


class Ahn:
    def __init__(self, cache_dir, fetcher):
        self.dir = os.path.join(cache_dir, "regional", "ahn", f"v{VERSION}")
        self.fetcher = fetcher
        self.mem = OrderedDict()
        self.lock = threading.Lock()

    def _fetch(self, cov, x0, y0, x1, y1):
        """A coverage's grid over the RD box (rows north to south), NaN where it has no data."""
        from PIL import Image
        import netfetch
        try:
            body = self.fetcher.get(WCS.format(cov=cov, x0=x0, x1=x1, y0=y0, y1=y1)).body
        except netfetch.NotFound:
            return None
        a = np.array(Image.open(io.BytesIO(body)), np.float32)
        a[~(np.abs(a) < NODATA)] = np.nan
        return a

    def tile(self, i, j):
        """(ground filled, hole mask, surface) of tile (i, j) (TILE_M squares from RD 0, 0; rows north to south), or
        None where the AHN has none."""
        with self.lock:
            if (i, j) in self.mem:
                self.mem.move_to_end((i, j))
                return self.mem[(i, j)]
        path = os.path.join(self.dir, f"{i}_{j}.npz")
        got = None
        try:
            with np.load(path) as z:
                got = (z["dtm"], z["kind"], z["dsm"]) if z["dtm"].size else None
        except (OSError, KeyError, ValueError):
            x0, y0 = i * TILE_M - MARGIN_M, j * TILE_M - MARGIN_M
            x1, y1 = (i + 1) * TILE_M + MARGIN_M, (j + 1) * TILE_M + MARGIN_M
            dtm = self._fetch("dtm_05m", x0, y0, x1, y1)
            dsm = self._fetch("dsm_05m", x0, y0, x1, y1)
            m = int(round(MARGIN_M / RES))
            if dtm is None or dsm is None or not np.isfinite(dtm).any():
                os.makedirs(self.dir, exist_ok=True)
                np.savez_compressed(path, dtm=np.zeros(0, np.float32), kind=np.zeros(0, np.uint8), dsm=np.zeros(0, np.float32))
            else:
                hole = ~np.isfinite(dtm)
                full, deck = ground(dtm, dsm, decks=True)
                kind = hole.astype(np.uint8) + deck.astype(np.uint8)        # 0 ground, 1 a hole, 2 a deck
                got = (full[m:-m, m:-m].astype(np.float32), kind[m:-m, m:-m], dsm[m:-m, m:-m].astype(np.float32))
                os.makedirs(self.dir, exist_ok=True)
                tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp.npz"
                np.savez_compressed(tmp, dtm=got[0], kind=got[1], dsm=got[2])
                os.replace(tmp, path)
        with self.lock:
            self.mem[(i, j)] = got
            while len(self.mem) > MEM_TILES:
                self.mem.popitem(last=False)
        return got

    def sample(self, lat, lon, kinds=False):
        """(ground, hole, surface) at the points (bilinear; NaN / False where the AHN has nothing); kinds: hole as the
        cell's kind instead (0 ground, 1 a hole, 2 a deck)."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        shape = np.broadcast(lat, lon).shape
        la, lo = np.broadcast_to(lat, shape).ravel(), np.broadcast_to(lon, shape).ravel()
        dtm = np.full(la.shape, np.nan)
        dsm = np.full(la.shape, np.nan)
        hole = np.zeros(la.shape, np.uint8)
        ok = (lo >= 3.2) & (lo <= 7.3) & (la >= 50.7) & (la <= 53.6)
        if ok.any():
            x, y = wgs_to_rd(la[ok], lo[ok])
            ti, tj = np.floor(x / TILE_M).astype(np.int64), np.floor(y / TILE_M).astype(np.int64)
            k_ok = np.flatnonzero(ok)
            for i, j in set(zip(ti.tolist(), tj.tolist())):
                sel = (ti == i) & (tj == j)
                t = self.tile(i, j)
                if t is None:
                    continue
                g, h, s = t
                n = g.shape[0]
                # pixel centres: column c at x = i*TILE_M + (c + 0.5) RES, row r at y = (j+1)*TILE_M - (r + 0.5) RES
                fc = (x[sel] - i * TILE_M) / RES - 0.5
                fr = ((j + 1) * TILE_M - y[sel]) / RES - 0.5
                c0 = np.clip(np.floor(fc).astype(np.int64), 0, n - 2)
                r0 = np.clip(np.floor(fr).astype(np.int64), 0, n - 2)
                u = np.clip(fc - c0, 0.0, 1.0)
                v = np.clip(fr - r0, 0.0, 1.0)

                def bil(a):
                    return ((1 - u) * (1 - v) * a[r0, c0] + u * (1 - v) * a[r0, c0 + 1] + (1 - u) * v * a[r0 + 1, c0]
                            + u * v * a[r0 + 1, c0 + 1])
                idx = k_ok[sel]
                dtm[idx] = bil(g)
                dsm[idx] = bil(s)
                hole[idx] = h[np.clip(np.rint(fr).astype(np.int64), 0, n - 1), np.clip(np.rint(fc).astype(np.int64), 0, n - 1)]
        hole = hole.reshape(shape)
        return dtm.reshape(shape), (hole if kinds else hole > 0), dsm.reshape(shape)

    def deck_top(self, lat, lon):
        """The surface over a deck (a hole in the ground whose surface is at the higher side's level and DECK_RISE_M over
        the lower's: ground(); not an overhang's or a roof's edge, 2026-10-21), NaN elsewhere."""
        g, k, s = self.sample(lat, lon, kinds=True)
        return np.where((k == 2) & np.isfinite(s), s, np.nan)


class AhnDem:
    """A dem.Dem whose sample() is the AHN's ground (filled) where it has it, at MIN_ZOOM and finer; the base DEM
    elsewhere. In its holes the lower of the fill and the base: water (a hole the base has at the water's level)."""

    def __init__(self, base, ahn):
        self.base = base
        self.ahn = ahn

    def sample(self, lat, lon, zoom, progress=None):
        z = np.asarray(self.base.sample(lat, lon, zoom, progress) if progress is not None
                       else self.base.sample(lat, lon, zoom), np.float64)
        if zoom < MIN_ZOOM:
            return z
        try:
            g, h, _ = self.ahn.sample(lat, lon)
        except Exception as e:            # no AHN rather than no terrain
            log.warning("ahn: %s: %s", type(e).__name__, e)
            return z
        g = np.broadcast_to(g, z.shape)
        h = np.broadcast_to(h, z.shape)
        m = np.isfinite(g)
        if not m.any():
            return z
        out = z.copy()
        out[m] = np.where(h[m], np.minimum(g[m], z[m]), g[m])
        return out

    def __getattr__(self, name):
        return getattr(self.base, name)
