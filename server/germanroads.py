"""Germany's surveyed roads: paved width, lanes and surface from the states' official road axes (ATKIS Basis-DLM).

Overture and OpenStreetMap rarely give a German road's width, so roads.carriageway guessed it from the class and the
lane count. The states survey every classified road (Autobahn, Bundes-, Landes-, Kreisstraße): the paved width to
0.5 m (BRF, "befestigte Fläche"), the lanes without hard shoulders (FSZ), the surface (OFM), and each carriageway of a
divided road as its own axis (Fahrbahnachse). Most town streets carry their lanes, some their width too (Berlin's
almost all), farm tracks whether they are paved, gravel or earth.

tools/build_de_roads.py reads the basemap.de Open Data GeoPackages once and keeps each axis as a simplified line with
those attributes, served as shards of a z10 tile (~25 km): <PAGES>/deroads/<x>/<y>.npz, fetched like lod2roofs.

adjust_specs (last in roads._road_specs) matches each road to the axis it runs along (within MATCH_M, the same
direction, over MATCH_SHARE of its length) and takes from it what the map does not say itself:
    width    the surveyed paved width, where the map has no width of its own (a mapped width or OSM width wins);
             not on ramps (roads' ramp width)
    lanes    the surveyed lanes of motorways to tertiary roads, where OpenStreetMap has no lanes tag (its lanes win);
             the width follows the class's rule for that many lanes when none was surveyed
    surface  where OpenStreetMap and Overture say nothing: paved, setts, gravel or earth
FLOOR_ONLY: in towns the survey measures the width that is driven, between the cars parked at the curbs (Berlin, on
the aerial in game, 2026-11-10: 8 and 9 m surveyed, 11-12 m from curb to curb with a row of cars on each side), and
half of a single axis is no measure of one half of a divided road (Saarbrücken: a 5 m axis gave its two 7 m halves
2.5 m each). There a surveyed width only widens a road drawn narrower, never narrows one, and the width measured
between the buildings (roads._measured_widths) stays above it. Out of town (no parking) it is the width drawn; where
it is, parking stays only where the lanes still fit beside it (_fit_parking). Checked on 2,300 roads in five cities
and on the aerials in game (GitHub #1079).
Driveways, parking aisles and living streets (SKIP) take nothing: they run beside a street, not along its axis.
A divided road's two one-way halves on one two-way axis (a town avenue the survey keeps as one) take half its width
each. Decks and tunnels (roads._bridge_specs) take the same as the road on the ground, so a bridge is as wide as its
approaches.
Outside Germany, where a shard is missing, or offline: no axes, today's estimates. BNE_DE_ROADS=off turns it off.
The sources and their licences are in CREDITS.md.
"""
import io
import logging
import math
import os
import threading
import time
import zipfile

import numpy as np
import dmath
import dnp

log = logging.getLogger("earthd.germanroads")

ON = os.environ.get("BNE_DE_ROADS", "on").lower() not in ("off", "0", "false")
VERSION = "de-roads-v3"     # (v2: parking only where it fits, the survey over the buildings' width)
ZOOM = 10
BOX = (47.2, 5.8, 55.1, 15.1)   # south, west, north, east: Germany, so nowhere else asks for a shard
RETRY_S = 3600                  # unreachable: asked again after this long
MISS_TTL_S = 7 * 86400          # a shard the site did not have (404) asked again after this
MATCH_M = 7.0                   # m from a road's line to the axis: the axes' own +-3 m and the map's
PAIRED_SLACK_M = 3.0            # m past half a divided road's median its halves still find its single axis
REACH_MAX_M = 15.0              # m at most a road's samples look for an axis
MATCH_DEG = 25.0                # degrees at most between the road's and the axis' direction
MATCH_SHARE = 0.6               # share of a road's samples that must find its axis
SAMPLE_M = 10.0                 # m between a road's samples
CELL_M = 25.0                   # grid the axes' segments are looked up in
WIDTH_M = (2.5, 40.0)           # a surveyed width outside this is not taken
LANE_MIN_M = 2.4                # m per lane at least: a width narrower than its lanes need is not taken
LANE_FIT_M = 2.75               # m per lane a main road keeps beside curbside parking, else no parking (_fit_parking)
LANE_FIT_TOWN_M = 2.0           # the same for a town street (one shared lane between parked cars)
# build_de_roads.py's surface codes -> OpenStreetMap surface values (roads.surface_kind reads them)
SURFACES = {1: "asphalt", 2: "concrete", 3: "sett", 5: "compacted", 6: "ground", 7: "grass_paver",
            8: "paving_stones"}
# (4, "Gestein, zerkleinert", is not taken: Saarland's survey gives it to stretches of Autobahn, crushed stone in the
# asphalt rather than a gravel road)
SKIP = {"service", "living_street", "raceway", "busway"}    # driveways, parking aisles, yards: beside a street, not it
# Lanes only for these classes (and their width from that count where none was surveyed): the survey gives two lanes
# to almost every town street, which drew 8 m streets with a centre line (Saarland probe, 2026-11-09)
LANE_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary"}
UNPAVED_CLASSES = {"track", "unclassified", "residential"}  # the only ones the survey may make gravel or earth
DIVIDED_AXIS, CARRIAGEWAY = 1, 2      # flags: a divided road's centre axis / one of its carriageways

_lock = threading.Lock()
_state = {"cache": None, "fetcher": None, "shards": {}, "down_at": 0.0}
LOCAL = None                    # (tests: a folder of <x>/<y>.npz read instead; else BNE_DE_ROADS_DIR)


def configure(cache_dir, fetcher):
    """MapData does it in every process that has one; until then only BNE_DE_ROADS_DIR is read."""
    with _lock:
        if _state["fetcher"] is None:
            _state["cache"], _state["fetcher"] = cache_dir, fetcher


def _local():
    return LOCAL if LOCAL is not None else (os.environ.get("BNE_DE_ROADS_DIR") or None)


def tile_of(lat, lon, zoom=ZOOM):
    n = 1 << zoom
    r = math.radians(lat)
    return int((lon + 180.0) / 360.0 * n), int((1.0 - dmath.asinh(dmath.tan(r)) / math.pi) / 2.0 * n)


def tiles_over(south, west, north, east):
    """[(x, y)] of the z10 tiles over a box, none outside Germany."""
    s, w, n, e = max(south, BOX[0]), max(west, BOX[1]), min(north, BOX[2]), min(east, BOX[3])
    if s > n or w > e:
        return []
    x0, y0 = tile_of(n, w)
    x1, y1 = tile_of(s, e)
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def decode(body):
    """The arrays of a shard's bytes ({} empty or unreadable): lat, lon (degrees), off, and the attributes."""
    if not body:
        return {}
    try:
        with np.load(io.BytesIO(body), allow_pickle=False) as z:
            d = {k: z[k] for k in ("off", "width", "lanes", "surf", "cls", "flags", "level")}
            d["lat"] = np.cumsum(z["dlat"].astype(np.int64)) / 1e5
            d["lon"] = np.cumsum(z["dlon"].astype(np.int64)) / 1e5
            return d
    except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile) as e:
        log.warning("de roads shard unreadable: %s", e)
        return {}


def shard(x, y):
    """The shard of tile (x, y): {} none there, None while unreachable (the caller's build is then not kept)."""
    with _lock:
        got = _state["shards"].get((x, y))
    if got is not None:
        return got
    local = _local()
    if local:
        p = os.path.join(local, str(x), f"{y}.npz")
        body = b""
        if os.path.exists(p):
            with open(p, "rb") as fh:
                body = fh.read()
    else:
        body = _fetched(x, y)
        if body is None:
            return None
    got = decode(body)
    with _lock:
        _state["shards"][(x, y)] = got
        while len(_state["shards"]) > 32:      # (a drive's few tiles; a long one forgets the oldest)
            _state["shards"].pop(next(iter(_state["shards"])))
    return got


def clear_memory():
    """Drop the shards kept in memory (budget.maybe_shed: a process short of memory); read from the disk cache again
    when a cell asks."""
    with _lock:
        _state["shards"].clear()


def _fetched(x, y):
    """The shard's bytes from the disk cache or the site, b"" none there, None unreachable."""
    import netfetch
    import railtags
    cache, fetcher = _state["cache"], _state["fetcher"]
    path = os.path.join(cache, "pagesdata", "deroads", f"{x}_{y}.npz") if cache else None
    if path and os.path.exists(path):
        try:
            stale_miss = os.path.getsize(path) == 0 and time.time() - os.path.getmtime(path) > MISS_TTL_S
            if not stale_miss:
                with open(path, "rb") as fh:
                    return fh.read()
        except OSError:
            pass
    if fetcher is None:
        return b""
    if time.time() - _state["down_at"] < RETRY_S:
        return None
    try:
        body = fetcher.get(f"{railtags.PAGES}/deroads/{x}/{y}.npz", kind="demand").body
    except netfetch.NotFound:
        body = b""
    except Exception as e:      # (offline: today's estimates, and the build is not kept)
        log.debug("de roads shard %s %s: %s: %s", x, y, type(e).__name__, e)
        _state["down_at"] = time.time()
        return None
    if path:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".part", "wb") as fh:
            fh.write(body)
        os.replace(path + ".part", path)
    return body


def _segments(d):
    """A shard's segments as (a, b) lon/lat points, their line index and the lines' attribute rows, made once and kept
    on the shard (it is cached per process): every cell of a z10 tile asked for all of it again, 92 ms a call in Berlin."""
    got = d.get("_seg")
    if got is None:
        off = d["off"]
        lid = np.repeat(np.arange(len(off) - 1), np.diff(off))
        same = lid[1:] == lid[:-1]
        ll = np.column_stack([d["lon"], d["lat"]])
        at = np.column_stack([d["width"], d["lanes"], d["surf"], d["cls"], d["flags"]]).astype(np.int32)
        got = d["_seg"] = (ll[:-1][same], ll[1:][same], lid[:-1][same], at)
    return got


class Axes:
    """The axes of some shards in local metres round (lat0, lon0), their segments in a grid."""

    def __init__(self, shards, lat0, lon0, half=None):
        self.lat0, self.lon0 = lat0, lon0
        self.kx, self.ky = 111320.0 * dmath.cos(math.radians(lat0)), 110540.0
        seg_a, seg_b, line, attrs = [], [], [], []
        base = 0
        for d in shards:
            n = len(d["off"]) - 1
            if n <= 0:
                continue
            la, lb, lid, at = _segments(d)
            if half is not None and len(lid):     # (a shard is ~25 km across: only the segments near the roads, cut in
                hx, hy = half                     # degrees first so the metres are made for those alone; the exact
                m = REACH_MAX_M + 50.0            # test below is the old one on the same floats)
                dx, dy = (hx + m) / self.kx, (hy + m) / self.ky
                near = ((np.maximum(la[:, 0], lb[:, 0]) >= lon0 - dx) & (np.minimum(la[:, 0], lb[:, 0]) <= lon0 + dx)
                        & (np.maximum(la[:, 1], lb[:, 1]) >= lat0 - dy) & (np.minimum(la[:, 1], lb[:, 1]) <= lat0 + dy))
                la, lb, lid = la[near], lb[near], lid[near]
            seg_a.append(np.column_stack([(la[:, 0] - lon0) * self.kx, (la[:, 1] - lat0) * self.ky]))
            seg_b.append(np.column_stack([(lb[:, 0] - lon0) * self.kx, (lb[:, 1] - lat0) * self.ky]))
            line.append(lid + base)
            attrs.append(at)
            base += n
        if not seg_a:
            self.a = np.zeros((0, 2))
            return
        self.a, self.b = np.concatenate(seg_a), np.concatenate(seg_b)
        self.line, self.attrs = np.concatenate(line), np.concatenate(attrs)
        if half is not None:            # (only the segments near the roads asked about: a shard is ~25 km across)
            hx, hy = half
            m = REACH_MAX_M
            near = ((np.maximum(self.a[:, 0], self.b[:, 0]) >= -hx - m) & (np.minimum(self.a[:, 0], self.b[:, 0]) <= hx + m)
                    & (np.maximum(self.a[:, 1], self.b[:, 1]) >= -hy - m) & (np.minimum(self.a[:, 1], self.b[:, 1]) <= hy + m))
            self.a, self.b, self.line = self.a[near], self.b[near], self.line[near]
            if not len(self.a):
                return
        # (the attribute rows of the lines left alone, the lines numbered among them: a line's number only picks its row
        # (match, surveyed). Every Axes kept a copy of its shards' whole attribute table, ~25 km of roads, on the
        # Features it is memoised on (axes_for): ~75 MB of a road process after 25 tiles round the A7, 2026-11-16)
        used, inv = np.unique(self.line, return_inverse=True)
        self.attrs, self.line = self.attrs[used], inv.astype(np.int32 if len(used) < 2 ** 31 else np.int64)
        d = self.b - self.a
        self.len = dnp.hypot(d[:, 0], d[:, 1])
        self.dir = d / np.maximum(self.len, 1e-9)[:, None]
        lo = np.floor(np.minimum(self.a, self.b) / CELL_M - REACH_MAX_M / CELL_M).astype(np.int64)
        hi = np.floor(np.maximum(self.a, self.b) / CELL_M + REACH_MAX_M / CELL_M).astype(np.int64)
        nx, ny = hi[:, 0] - lo[:, 0] + 1, hi[:, 1] - lo[:, 1] + 1
        cnt = nx * ny
        # (segment numbers and the cells' bounds as int32: the grid is ~10 entries a segment, kept with the Axes)
        idx = np.int32 if len(self.a) < 2 ** 31 and int(cnt.sum()) < 2 ** 31 else np.int64
        sid = np.repeat(np.arange(len(self.a), dtype=idx), cnt)
        k = np.arange(int(cnt.sum())) - np.repeat(np.cumsum(cnt) - cnt, cnt)
        gx = lo[sid, 0] + k // ny[sid]
        gy = lo[sid, 1] + k % ny[sid]
        # the grid as sorted packed cell keys (a dict of ~400k cells took 100-150 ms a call: a cell is a whole block's roads)
        self.g0 = (int(gx.min()), int(gy.min())) if len(sid) else (0, 0)
        self.gny = int(gy.max()) - self.g0[1] + 1 if len(sid) else 1
        key = (gx - self.g0[0]) * self.gny + (gy - self.g0[1])
        order = np.argsort(key, kind="stable")
        self.gsid, key = sid[order], key[order]
        cut = np.flatnonzero(key[1:] != key[:-1]) + 1
        self.gkey = key[np.concatenate([[0], cut])] if len(key) else key
        if len(self.gkey) and int(self.gkey[-1]) < 2 ** 31:     # (int32 too: _candidates asks with keys cut to it)
            self.gkey = self.gkey.astype(np.int32)
        self.gcut = np.concatenate([[0], cut, [len(key)]]).astype(idx)

    def _candidates(self, cells):
        """The segments in any of the grid cells (an (n, 2) array of cell coordinates), sorted, once each."""
        cx, cy = cells[:, 0] - self.g0[0], cells[:, 1] - self.g0[1]
        ok = (cx >= 0) & (cy >= 0) & (cy < self.gny)
        key = np.unique(cx[ok] * self.gny + cy[ok])
        if self.gkey.dtype != key.dtype and len(self.gkey):      # (keys past the grid's last match nothing)
            key = key[key <= self.gkey[-1]].astype(self.gkey.dtype)
        at = np.searchsorted(self.gkey, key)
        inside = at < len(self.gkey)
        at = at[inside][self.gkey[at[inside]] == key[inside]]
        parts = [self.gsid[self.gcut[i]:self.gcut[i + 1]] for i in at]
        return np.unique(np.concatenate(parts)) if parts else None

    def __bool__(self):
        return len(self.a) > 0

    def local(self, lonlat):
        ll = np.asarray(lonlat, np.float64)
        return np.column_stack([(ll[:, 0] - self.lon0) * self.kx, (ll[:, 1] - self.lat0) * self.ky])

    def match(self, lonlat, carriageway=None, reach=MATCH_M):
        """The axis (line index, a row of attrs) each of a road's samples runs along (-1 none) and the samples' count.
        carriageway: True only a divided road's carriageways, False none of them, None any. All samples at once
        against the segments in their grid cells (one per sample was ~4x the specs' own time in Berlin)."""
        p = self.local(lonlat)
        seg = np.diff(p, axis=0)
        L = dnp.hypot(seg[:, 0], seg[:, 1])
        keep = L > 0
        if not keep.any():
            return np.zeros(0, np.int64), 0
        seg, L, p0 = seg[keep], L[keep], p[:-1][keep]
        n = np.maximum(1, (L // SAMPLE_M).astype(np.int64))
        k = np.repeat(np.arange(len(seg)), n)
        t = (np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n) + 0.5) / np.repeat(n, n)
        pts = p0[k] + t[:, None] * seg[k]
        dirs = seg[k] / L[k][:, None]
        got = np.full(len(pts), -1, np.int64)
        cells = np.floor(pts / CELL_M).astype(np.int64)
        c = self._candidates(cells)
        if c is None:
            return got, len(pts)
        if carriageway is not None:
            is_cw = (self.attrs[self.line[c], 4] & CARRIAGEWAY) > 0
            c = c[is_cw] if carriageway else c[~is_cw]
            if not len(c):
                return got, len(pts)
        a, d, ln = self.a[c], self.dir[c], self.len[c]
        rel = pts[:, None, :] - a[None, :, :]                       # (samples, candidates, 2)
        along = np.clip(dnp.vecdot(rel, d), 0.0, ln[None, :])
        off = rel - along[:, :, None] * d[None, :, :]
        dist = dnp.hypot(off[:, :, 0], off[:, :, 1])
        dist[np.abs(dnp.matmul(dirs, d.T)) < dmath.cos(math.radians(MATCH_DEG))] = np.inf
        best = np.argmin(dist, axis=1)
        ok = dist[np.arange(len(pts)), best] <= reach
        got[ok] = self.line[c[best[ok]]]
        return got, len(pts)

    PAIRS = 1_500_000           # sample-segment pairs measured at once (match_many)

    def match_many(self, lonlats, carriageway, reach):
        """match() for many roads at once: [(the samples' axes, their count)] for the roads' lines (lonlats), each
        within its own reach (an array). Every sample against the segments of its own grid cell: a segment within
        reach (at most REACH_MAX_M) of a sample is listed in the sample's cell, so the nearest one is the one match()
        finds among all of the road's cells, measured by the same sums. One call per road was a third of the specs'
        time, 6585 calls for a motorway drive's 62 road tiles (2026-11-17)."""
        out = [(np.zeros(0, np.int64), 0)] * len(lonlats)
        if not lonlats:
            return out
        npt = np.array([len(ll) for ll in lonlats], np.int64)
        p = self.local(np.concatenate([np.asarray(ll, np.float64).reshape(-1, 2) for ll in lonlats]))
        road = np.repeat(np.arange(len(lonlats)), npt)
        seg = np.diff(p, axis=0)
        L = dnp.hypot(seg[:, 0], seg[:, 1])
        keep = (L > 0) & (road[1:] == road[:-1])        # (not from a road's last point to the next road's first)
        if not keep.any():
            return out
        seg, L, p0, road = seg[keep], L[keep], p[:-1][keep], road[:-1][keep]
        n = np.maximum(1, (L // SAMPLE_M).astype(np.int64))
        k = np.repeat(np.arange(len(seg)), n)
        t = (np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n) + 0.5) / np.repeat(n, n)
        pts = p0[k] + t[:, None] * seg[k]
        dirs = seg[k] / L[k][:, None]
        road = road[k]                                  # (each sample's road; ascending)
        got = np.full(len(pts), -1, np.int64)
        cells = np.floor(pts / CELL_M).astype(np.int64)
        cx, cy = cells[:, 0] - self.g0[0], cells[:, 1] - self.g0[1]
        inside = (cx >= 0) & (cy >= 0) & (cy < self.gny) & (len(self.gkey) > 0)
        key = np.where(inside, cx * self.gny + cy, -1)
        at = np.minimum(np.searchsorted(self.gkey, key), max(len(self.gkey) - 1, 0))
        inside &= self.gkey[at] == key if len(self.gkey) else False
        cnt = np.where(inside, self.gcut[at + 1] - self.gcut[at], 0) if len(self.gkey) else np.zeros(len(pts), np.int64)
        cos_min = dmath.cos(math.radians(MATCH_DEG))
        reach_s = np.asarray(reach, np.float64)[road]
        ends = np.cumsum(cnt)
        s0 = 0
        while s0 < len(pts):            # (the pairs in runs of whole samples)
            s1 = max(s0 + 1, int(np.searchsorted(ends, (ends[s0 - 1] if s0 else 0) + self.PAIRS, side="right")))
            c_n = cnt[s0:s1]
            total = int(c_n.sum())
            if total:
                smp = np.repeat(np.arange(s0, s1), c_n)
                first = np.cumsum(c_n) - c_n                # (each sample's first pair)
                c = self.gsid[np.repeat(self.gcut[at[s0:s1]], c_n) + np.arange(total) - np.repeat(first, c_n)]
                d, ln = self.dir[c], self.len[c]
                rel = pts[smp] - self.a[c]
                along = np.clip(dnp.vecdot(rel, d), 0.0, ln)
                off = rel - along[:, None] * d
                dist = dnp.hypot(off[:, 0], off[:, 1])
                dist[np.abs(dirs[smp, 0] * d[:, 0] + dirs[smp, 1] * d[:, 1]) < cos_min] = np.inf
                if carriageway is not None:
                    is_cw = (self.attrs[self.line[c], 4] & CARRIAGEWAY) > 0
                    dist[~is_cw if carriageway else is_cw] = np.inf
                has = np.flatnonzero(c_n > 0)
                low = np.minimum.reduceat(dist, first[has])
                ok = low <= reach_s[s0:s1][has]
                # the first of a sample's nearest pairs (its segments in rising order, as match()'s argmin takes them)
                is_low = np.flatnonzero(dist == np.repeat(low, c_n[has]))
                if len(is_low):         # (none: distances that are no numbers, no axis there, as match())
                    best = is_low[np.minimum(np.searchsorted(is_low, first[has]), len(is_low) - 1)]
                    got[s0 + has[ok]] = self.line[c[best[ok]]]
            s0 = s1
        cut = np.searchsorted(road, np.arange(len(lonlats) + 1))
        for r in range(len(lonlats)):
            if cut[r + 1] > cut[r]:
                out[r] = (got[cut[r]:cut[r + 1]], int(cut[r + 1] - cut[r]))
        return out


def axes_for(feats):
    """The Axes round feats' roads, None where there are none (outside Germany, no shard), False while a shard could
    not be had (the specs made now are then not kept: revisit.fault). Kept on feats once known (surveyed_width asks it
    for every road of a cell: the whole cell's coordinates were scanned per road, 3.3 s of a cold block's 10 in Boise,
    Idaho, 2026-11-11); a False is asked again."""
    memo = feats.__dict__.get("_de_axes", _UNSET)
    if memo is not _UNSET:
        return memo
    got = _axes_for(feats)
    if got is not False:
        feats.__dict__["_de_axes"] = got
    return got


_UNSET = object()


def _axes_for(feats):
    ll = np.asarray(feats.lonlat, np.float64)
    if not len(ll) or not np.isfinite(ll).all():
        return None
    s, n = float(ll[:, 1].min()), float(ll[:, 1].max())
    w, e = float(ll[:, 0].min()), float(ll[:, 0].max())
    tiles = tiles_over(s - 0.001, w - 0.001, n + 0.001, e + 0.001)
    if not tiles:
        return None
    got, short = [], False
    for x, y in tiles:
        d = shard(x, y)
        if d is None:
            short = True
        elif d:
            got.append(d)
    if short:
        return False
    if not got:
        return None
    lat0, lon0 = (s + n) / 2.0, (w + e) / 2.0
    half = ((e - w) / 2.0 * 111320.0 * dmath.cos(math.radians(lat0)), (n - s) / 2.0 * 110540.0)
    ax = Axes(got, lat0, lon0, half)
    return ax if ax else None


def _mode(values):
    v = values[values > 0]
    if not len(v):
        return 0
    u, c = np.unique(v, return_counts=True)
    return int(u[np.argmax(c)])


def surveyed(ax, lonlat, oneway, paired=None):
    """(width m or None, lanes or None, surface value or None, halved) of the axis a road runs along, None where it
    runs along none; halved: the width is half a single axis' (paired, below). paired: the median (m, centre to centre) of a road that is one of a divided road's two one-way halves
    (roads.dual_carriageways): on a single two-way axis, found within half that and PAIRED_SLACK_M, it takes half the
    axis' paved width, the medians being unpaved. A one-way road (one carriageway of a divided road, or a one-way street) takes a carriageway's axis
    where there is one; a two-way road never one carriageway's. A one-way road on any other axis takes its surface
    alone: the map splits roads into two one-way halves the survey keeps as one axis with both halves' width and
    sometimes fewer lanes than one half has (Saarbrücken's Mainzer Straße: one 20 m, two-lane axis, two two-lane
    halves in the map, each drawn 20 m wide), and the open data does not say which axes are one-way streets."""
    return surveyed_many(ax, [(lonlat, oneway, paired)])[0]


def surveyed_many(ax, roads_):
    """surveyed() of many roads [(lonlat, oneway, paired)] at once (Axes.match_many): their answers in order."""
    out = [None] * len(roads_)
    first = [r for r, (_, oneway, _) in enumerate(roads_) if oneway]
    hits = dict(zip(first, ax.match_many([roads_[r][0] for r in first], True, np.full(len(first), MATCH_M))))
    again, reach = [], []
    for r, (_, oneway, paired) in enumerate(roads_):
        hit, n = hits.get(r, (None, 0))
        if not oneway or (hit >= 0).sum() < MATCH_SHARE * max(n, 1):
            again.append(r)
            reach.append(max(MATCH_M, min(REACH_MAX_M, paired / 2.0 + PAIRED_SLACK_M)) if oneway and paired
                         else MATCH_M)
    own = {r: True for r in first}
    for r, got in zip(again, ax.match_many([roads_[r][0] for r in again], False, np.array(reach, np.float64))):
        hits[r] = got
        own[r] = not roads_[r][1]
    for r, (_, oneway, paired) in enumerate(roads_):
        hit, n = hits[r]
        out[r] = _survey_answer(ax, hit, n, own[r], paired)
    return out


def _survey_answer(ax, hit, n, own, paired):
    if not n or (hit >= 0).sum() < MATCH_SHARE * n:
        return None
    a = ax.attrs[hit[hit >= 0]]
    widths = a[:, 0][a[:, 0] > 0]
    width = float(np.median(widths)) / 10.0 if len(widths) >= MATCH_SHARE * len(a) else None
    lanes = _mode(a[:, 1]) or None
    surf = SURFACES.get(_mode(a[:, 2]))
    if own:
        return width, lanes, surf, False
    if paired and width is not None and not (a[:, 4] & DIVIDED_AXIS).any():
        return width / 2.0, None, surf, True
    return None, None, surf, False


def adjust_specs(feats, specs):
    """specs with what Germany's surveyed axes say where the map says nothing (module docstring)."""
    if not ON or not any(sp is not None for sp in specs):
        return specs
    ax = axes_for(feats)
    if ax is False:
        import revisit
        revisit.fault()
        return specs
    if ax is None:
        return specs
    import roads
    rules = feats.rules
    out = list(specs)
    known = _survey_known(feats)
    asked, new = {}, []
    for i, sp in enumerate(out):
        if sp is None or sp.cls in SKIP or roads.is_ring(feats, i):
            continue
        a = feats.coord_off[feats.ring_off[feats.part_off[i]]]
        b = feats.coord_off[feats.ring_off[feats.part_off[i + 1]]]
        if b - a < 2:
            continue
        k = asked[i] = (i, bool(sp.oneway), float(sp.median) if sp.median else None)
        if k not in known:
            new.append((k, (feats.lonlat[a:b], k[1], sp.median)))
    new_rows = len(new)
    if new:                             # (the roads not asked about before, all at once)
        for (k, _), got in zip(new, surveyed_many(ax, [r for _, r in new])):
            known[k] = got
    for i, sp in enumerate(out):
        k = asked.get(i)
        if k is None:
            continue
        got = known[k]
        if got is None:
            continue
        width, lanes, surf, halved = got
        mapped_w = roads.mapped_rule_width(feats, i)
        if mapped_w is None:
            mapped_w = roads.mapped_osm_width(feats, i, *roads.OSM_WIDTH_M)
        new = sp
        if (lanes and sp.cls in LANE_CLASSES and not sp.link
                and roads._osm(feats, "lanes", i, *roads.OSM_LANES) is None):
            new = new._replace(lanes=int(lanes))
        if surf and roads._map_surface_value(feats, i) is None:
            kind = roads.surface_kind(sp.cls, surf)
            unpaved = kind not in roads.BUILT
            if kind != sp.kind and not (unpaved and (sp.urban or sp.cls not in UNPAVED_CLASSES)):
                new = new._replace(kind=kind, surf=roads.surface_token(surf))
        if mapped_w is None and not sp.link:        # (a ramp keeps roads' ramp width: the survey's is its lane alone)
            n = new.lanes or 1
            if width is not None and WIDTH_M[0] <= width <= WIDTH_M[1] and width >= LANE_MIN_M * n:
                w = width
            elif new.lanes != sp.lanes or new.kind != sp.kind:
                w = roads.carriageway(new.cls, new.kind, new.urban, None, new.oneway, new.lanes, link=new.link)
            else:
                w = None
            if w is not None:
                if new.median:
                    w = roads.dual_cap(new.median, w)
                if (sp.urban or halved) and w <= sp.width:
                    w = None                        # (a lower bound there: FLOOR_ONLY in the module docstring)
            if w is not None:
                new = new._replace(width=float(w))
                new = _fit_parking(new)
        out[i] = new
    if new_rows:
        _survey_keep(feats, known)
    return out


def _survey_extra(feats):
    """What a layer's surveyed() answers were made from, beside its cell: this module's version and the layer's own
    lines (a cell's roads come in more than one form: its lane sections split out, its bends rounded)."""
    ll = np.asarray(feats.lonlat, np.float64)
    return (VERSION, len(feats), len(ll), float(ll.sum()) if len(ll) else 0.0)


def _survey_known(feats):
    """{(feature, one-way, its median or None): surveyed()'s answer} of feats' roads, as far as they were asked before:
    on feats, and from the cell's facts (roads._facts_load) what another process of the server made. Every process
    drawing a cell asked the survey about every one of its roads again, the specs with the land cover being each
    process's own: 6 % of a motorway drive's CPU in eleven processes (2026-11-17)."""
    known = feats.__dict__.get("_de_survey")
    if known is not None:
        return known
    known = feats.__dict__["_de_survey"] = {}
    import roads
    try:
        for i, oneway, median, got in roads._facts_load(feats, "desurvey", _survey_extra(feats)) or ():
            known[(int(i), bool(oneway), None if median is None else float(median))] = None if got is None else (
                None if got[0] is None else float(got[0]), got[1], got[2], bool(got[3]))
    except (TypeError, ValueError, IndexError):
        known.clear()
    return known


def _survey_keep(feats, known):
    import roads
    try:
        roads._facts_store(feats, "desurvey", _survey_extra(feats),
                           [[i, oneway, median, None if got is None else list(got)]
                            for (i, oneway, median), got in sorted(known.items(), key=lambda kv: (kv[0][0], kv[0][1],
                                                                                              kv[0][2] or 0.0))])
    except Exception as e:      # (asked again by the next process then)
        log.debug("survey facts not kept: %s", e)


def _fit_parking(sp):
    """sp without its curbside parking where the surveyed width holds its lanes alone (GitHub #1079: a divided
    avenue's 6 m half with two lanes kept 2.3 m of parking, lanes of 1.85 m). The survey's width is the whole
    carriageway, parking included, so parking stays only where the lanes keep LANE_FIT_M beside it (a town street's
    one shared lane between parked cars: LANE_FIT_TOWN_M)."""
    import roads
    park = roads.assumed_parking(sp)
    if not park:
        return sp
    n = sp.lanes or (1 if sp.oneway else 2)
    fit = LANE_FIT_M if sp.cls in LANE_CLASSES else LANE_FIT_TOWN_M
    return sp if (sp.width - park) / n >= fit else sp._replace(parking=False)


def gave_lanes(feats, i, sp):
    """Are road i's lanes (its spec sp) the survey's, the map having none? Then they say nothing of the street's width,
    and roads._measured_widths still measures it between the buildings (Cologne: a 12.9 m street drawn 11.1 m from
    two surveyed lanes alone)."""
    if not ON or sp is None or not sp.lanes or sp.cls not in LANE_CLASSES or sp.link:
        return False
    return _map_lanes(feats, i, sp.cls) is None


def _map_lanes(feats, i, cls):
    """Road i's lane count from the map alone, as roads gives it without the survey: the ramp lane rule's (roads._lane_overrides),
    OpenStreetMap's own count, PR #471's validated ones, the one-way roads joined to it (lane_flow), else two lanes per
    motorway / trunk way, one on a one-way ramp; None otherwise. (roads' own lane count is in the spec kernels now.)"""
    import roadlanes
    import roads
    over = roads._lane_overrides(feats).get(i)
    if over is not None:
        return over
    lanes = roads._osm(feats, "lanes", i, *roads.OSM_LANES)
    if lanes is not None:
        return int(lanes)
    lanes = roadlanes.generated_counts(feats)[i]
    if lanes is None and (cls not in roadlanes.HIGHWAYS or roads._ramp_junction(feats)[i]):
        lanes = roads.lane_flow(feats)[i]
    if lanes is None and not roads.is_ring(feats, i):
        if roadlanes.is_link(feats, i):
            return 1 if roads._rule_value(feats.rules.get("oneway"), i) is not None else 2
        if cls in roadlanes.HIGHWAYS:
            return 2
    return lanes


def surveyed_width(feats, i, sp):
    """The surveyed width of road i (its spec sp), None where there is none (no axis, a mapped width, a ramp):
    roads._measured_widths keeps the width measured between the buildings above it (FLOOR_ONLY)."""
    if not ON or sp is None or sp.link or sp.cls in SKIP:
        return None
    memo = feats.__dict__.setdefault("_de_widths", {})
    key = (i, bool(sp.oneway), sp.median)
    if key in memo:
        return memo[key]
    import roads
    ax = axes_for(feats)
    if ax is False:
        return None
    got = None
    a = feats.coord_off[feats.ring_off[feats.part_off[i]]]
    b = feats.coord_off[feats.ring_off[feats.part_off[i + 1]]]
    if ax is not None and b - a >= 2 and not roads.is_ring(feats, i):
        known = _survey_known(feats)    # (adjust_specs asked about the cell's roads already, all at once)
        k = (i, bool(sp.oneway), float(sp.median) if sp.median else None)
        r = known.get(k, _UNSET)
        if r is _UNSET:
            r = known[k] = surveyed(ax, feats.lonlat[a:b], bool(sp.oneway), sp.median)
        if r is not None and r[0] is not None and WIDTH_M[0] <= r[0] <= WIDTH_M[1] \
                and r[0] >= LANE_MIN_M * (sp.lanes or 1):
            got = float(r[0])
    memo[key] = got
    return got
