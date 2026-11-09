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
Driveways, parking aisles and living streets (SKIP) take nothing: they run beside a street, not along its axis.
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

log = logging.getLogger("earthd.germanroads")

ON = os.environ.get("BNE_DE_ROADS", "on").lower() not in ("off", "0", "false")
VERSION = "de-roads-v1"
ZOOM = 10
BOX = (47.2, 5.8, 55.1, 15.1)   # south, west, north, east: Germany, so nowhere else asks for a shard
RETRY_S = 3600                  # unreachable: asked again after this long
MISS_TTL_S = 7 * 86400          # a shard the site did not have (404) asked again after this
MATCH_M = 7.0                   # m from a road's line to the axis: the axes' own +-3 m and the map's
MATCH_DEG = 25.0                # degrees at most between the road's and the axis' direction
MATCH_SHARE = 0.6               # share of a road's samples that must find its axis
SAMPLE_M = 10.0                 # m between a road's samples
CELL_M = 25.0                   # grid the axes' segments are looked up in
WIDTH_M = (2.5, 40.0)           # a surveyed width outside this is not taken
LANE_MIN_M = 2.4                # m per lane at least: a width narrower than its lanes need is not taken
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
    return int((lon + 180.0) / 360.0 * n), int((1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * n)


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


class Axes:
    """The axes of some shards in local metres round (lat0, lon0), their segments in a grid."""

    def __init__(self, shards, lat0, lon0):
        self.lat0, self.lon0 = lat0, lon0
        self.kx, self.ky = 111320.0 * math.cos(math.radians(lat0)), 110540.0
        seg_a, seg_b, line, attrs = [], [], [], []
        base = 0
        for d in shards:
            off = d["off"]
            n = len(off) - 1
            if n <= 0:
                continue
            xy = np.column_stack([(d["lon"] - lon0) * self.kx, (d["lat"] - lat0) * self.ky])
            lid = np.repeat(np.arange(n), np.diff(off))
            same = lid[1:] == lid[:-1]
            seg_a.append(xy[:-1][same])
            seg_b.append(xy[1:][same])
            line.append(lid[:-1][same] + base)
            attrs.append(np.column_stack([d["width"], d["lanes"], d["surf"], d["cls"], d["flags"]]).astype(np.int32))
            base += n
        if not seg_a:
            self.a = np.zeros((0, 2))
            return
        self.a, self.b = np.concatenate(seg_a), np.concatenate(seg_b)
        self.line, self.attrs = np.concatenate(line), np.concatenate(attrs)
        d = self.b - self.a
        self.len = np.hypot(d[:, 0], d[:, 1])
        self.dir = d / np.maximum(self.len, 1e-9)[:, None]
        lo = np.floor(np.minimum(self.a, self.b) / CELL_M - MATCH_M / CELL_M).astype(np.int64)
        hi = np.floor(np.maximum(self.a, self.b) / CELL_M + MATCH_M / CELL_M).astype(np.int64)
        self.grid = {}
        for s in range(len(self.a)):
            for gx in range(lo[s, 0], hi[s, 0] + 1):
                for gy in range(lo[s, 1], hi[s, 1] + 1):
                    self.grid.setdefault((gx, gy), []).append(s)

    def __bool__(self):
        return len(self.a) > 0

    def local(self, lonlat):
        ll = np.asarray(lonlat, np.float64)
        return np.column_stack([(ll[:, 0] - self.lon0) * self.kx, (ll[:, 1] - self.lat0) * self.ky])

    def match(self, lonlat, carriageway=None):
        """The axis (line index, a row of attrs) each of a road's samples runs along (-1 none) and the samples' count. carriageway: True
        only a divided road's carriageways, False none of them, None any."""
        p = self.local(lonlat)
        seg = np.diff(p, axis=0)
        L = np.hypot(seg[:, 0], seg[:, 1])
        pts, dirs = [], []
        for k in range(len(seg)):
            if L[k] <= 0:
                continue
            n = max(1, int(L[k] // SAMPLE_M))
            t = (np.arange(n) + 0.5) / n
            pts.append(p[k] + t[:, None] * seg[k])
            dirs.append(np.repeat((seg[k] / L[k])[None], n, axis=0))
        if not pts:
            return np.zeros(0, np.int64), 0
        pts, dirs = np.concatenate(pts), np.concatenate(dirs)
        cos_max = math.cos(math.radians(MATCH_DEG))
        got = np.full(len(pts), -1, np.int64)
        for j, (q, u) in enumerate(zip(pts, dirs)):
            cand = self.grid.get((int(math.floor(q[0] / CELL_M)), int(math.floor(q[1] / CELL_M))))
            if not cand:
                continue
            c = np.asarray(cand)
            if carriageway is not None:
                is_cw = (self.attrs[self.line[c], 4] & CARRIAGEWAY) > 0
                c = c[is_cw] if carriageway else c[~is_cw]
                if not len(c):
                    continue
            c = c[np.abs(self.dir[c] @ u) >= cos_max]
            if not len(c):
                continue
            t = np.clip(np.einsum("ij,ij->i", q - self.a[c], self.dir[c]), 0.0, self.len[c])
            near = self.a[c] + t[:, None] * self.dir[c]
            dist = np.hypot(near[:, 0] - q[0], near[:, 1] - q[1])
            k = int(np.argmin(dist))
            if dist[k] <= MATCH_M:
                got[j] = int(self.line[c[k]])
        return got, len(pts)


def axes_for(feats):
    """The Axes round feats' roads, None where there are none (outside Germany, no shard), False while a shard could
    not be had (the specs made now are then not kept: revisit.fault)."""
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
    ax = Axes(got, (s + n) / 2.0, (w + e) / 2.0)
    return ax if ax else None


def _mode(values):
    v = values[values > 0]
    if not len(v):
        return 0
    u, c = np.unique(v, return_counts=True)
    return int(u[np.argmax(c)])


def surveyed(ax, lonlat, oneway):
    """(width m or None, lanes or None, surface value or None) of the axis a road runs along, None where it runs
    along none. A one-way road (one carriageway of a divided road, or a one-way street) takes a carriageway's axis
    where there is one; a two-way road never one carriageway's. A one-way road on any other axis takes its surface
    alone: the map splits roads into two one-way halves the survey keeps as one axis with both halves' width and
    sometimes fewer lanes than one half has (Saarbrücken's Mainzer Straße: one 20 m, two-lane axis, two two-lane
    halves in the map, each drawn 20 m wide), and the open data does not say which axes are one-way streets."""
    hit, n = ax.match(lonlat, carriageway=True) if oneway else (None, 0)
    own = True
    if not oneway or (hit >= 0).sum() < MATCH_SHARE * max(n, 1):
        hit, n = ax.match(lonlat, carriageway=False)
        own = not oneway
    if not n or (hit >= 0).sum() < MATCH_SHARE * n:
        return None
    a = ax.attrs[hit[hit >= 0]]
    widths = a[:, 0][a[:, 0] > 0]
    width = float(np.median(widths)) / 10.0 if len(widths) >= MATCH_SHARE * len(a) else None
    lanes = _mode(a[:, 1]) or None
    surf = SURFACES.get(_mode(a[:, 2]))
    return (width, lanes, surf) if own else (None, None, surf)


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
    for i, sp in enumerate(out):
        if sp is None or sp.cls in SKIP or roads.is_ring(feats, i):
            continue
        a = feats.coord_off[feats.ring_off[feats.part_off[i]]]
        b = feats.coord_off[feats.ring_off[feats.part_off[i + 1]]]
        if b - a < 2:
            continue
        got = surveyed(ax, feats.lonlat[a:b], bool(sp.oneway))
        if got is None:
            continue
        width, lanes, surf = got
        mapped_w = roads._rule_value(rules.get("width"), i)
        if mapped_w is None:
            mapped_w = roads._osm(feats, "width", i, *roads.OSM_WIDTH_M)
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
                new = new._replace(width=float(w))
        out[i] = new
    return out
