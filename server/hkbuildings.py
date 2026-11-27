"""Hong Kong's measured building heights: the Lands Department's Building layer (CSDI Portal, (c) Government of the
Hong Kong SAR) joined to the map's footprints the way France's BD TOPO is (bdtopo.py: same flow, same native join).

- What comes down: per z14 map cell, the layer's rows (an ArcGIS FeatureServer, portal.csdi.gov.hk: its host policy and
  gzip in netfetch, 2 000 rows a page, ~5 500 in the densest cell) over the cell's box padded by PAD_M. A row is one
  building PART (a tower on a podium is two rows) with a top and a base LEVEL in metres above the Hong Kong Principal
  Datum, not heights above the ground; many rows have none. Kept on disk as one record per usable row
  (<cache>/hkbuildings/v1/<z>/<x>/<y>.npz: a point inside its outline, the outline's area, height above ground,
  storeys, Tower or not), the outlines dropped. A cell with none is kept as an empty record set; a failed request is
  not kept (RETRY_S).
- When: as for France (bdtopo.py): MapData.warm / prefetch start a cell's download in the background, a building tile
  waits for its cells together (wait()), MapData.cell() joins the records as soon as they are there and never waits;
  a Hong Kong buildings layer handed out without its records is a revisit fault.
- Ground: a row's height above ground is its top less the lowest base among the row itself, the rows whose outline
  holds its point and the rows whose point lies in its outline (a tower's base is its podium's top; measured 2026-11-18
  against the LiDAR: chaining on through those rows reached 81 rows and did worse). A row without a top still lends
  its base. Rows under MIN_H_M above ground are dropped.
- The join (earthfast.bdtopo_match): of the records whose point stands in a footprint, the highest Tower (TOWER_FIRST),
  else the highest row. An outline over BIG_M2 takes it only when the record's own outline is COVER of the footprint's
  (the map's outline of a whole podium took its tower's height: 68 solid blocks in 80 places, one 10 000 m2 and 204 m
  tall), else the largest record when that one is, else nothing. A record goes to one footprint, as in New York's join
  (nycmassing.Records.match_layer: one record to one building, the closest in area): of the footprints that picked it,
  the one whose area is closest to the record's keeps it and the others pick again from what is left (the map's
  podium and the tower on it no longer both take the tower's height). A footprint the map splits into parts is left to
  them (a part without a height takes the footprint's) and, like one with a height of the map's own, still claims its
  record, though nothing is written on it. The map's own height stays; a machine-learned one
  (buildings.ML_HEIGHT_SOURCES) gives way.
  Written: height (height_src "hk_landsd"), and a flat roof from FLAT_MIN_H_M where the map names no roof shape (a
  tower under 260 m2 got a pitched house roof). Storeys against the height, in buildings.ML_FLOOR_M's band of metres a
  storey: a record under the map's own storeys' band is another building's (no height written); over it the map's
  storeys go (a tower mapped with 1 storey drew one storey of 129 m); the record's storeys are written where the
  map has none or they went, when they suit its height.

Off: BNE_HK_BUILDINGS=off. Outside Hong Kong (hklidar.BOUNDS) nothing is asked and nothing changes.
"""
import json
import logging
import math
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import fast
import dmath
import hkdtm
import hklidar
import mapcells
import polygon

log = logging.getLogger("earthd.hkbuildings")

ON = os.environ.get("BNE_HK_BUILDINGS", "on") != "off"
NATIVE = 183                    # earthfast.VERSION of bdtopo_points / bdtopo_match
VERSION = 1                     # the records' layout on disk
URL = "https://portal.csdi.gov.hk/server/rest/services/common/landsd_rcd_1637211194312_35158/FeatureServer/0/query"
FIELDS = "OBJECTID,BuildingBlockType,BaseHeight,TopHeight,Storeys"
PAD_M = 100.0                   # the cell's box is asked for this much wider (a row's neighbours across the edge)
MIN_H_M = 2.5                   # a row lower than this above its ground is no building to draw
TOWER_FIRST = 1e6               # added to a Tower's weight: the highest Tower of a footprint beats any other row
FLAT_MIN_H_M = 12.0             # from this height a filled-in height gets a flat roof (no pitched roof on a tower)
BIG_M2 = 2000.0                 # a footprint over this takes a record only when the record covers COVER of it
COVER = 0.5
CLAIM_ROUNDS = 8                # picks at most for shared records (measured: one cell of 77 reaches 8, median 2)
SOURCE = "hk_landsd"            # the height_src it writes
THREADS = 2                     # background cell downloads at once per process (the host allows 2 at once: roadz's too)
# (BNGE_: not part of the revisit cache's code key; the wait changes when a tile is built, not what)
WAIT_S = float(os.environ.get("BNGE_HK_BUILDINGS_WAIT", "4"))
SLOW_WAIT_S = 0.5               # a tile's wait for a while after one did not come in WAIT_S (a slow line, host busy)
SLOW_FOR_S = 60.0
RETRY_S = 600.0                 # a cell whose download failed is asked again after this
LOCK_WAIT_S = 90.0              # another process downloading the same cell: waited for this long at most
MEM_CELLS = 128
FIELDS_NPZ = ("lon", "lat", "area", "h", "floors", "tower")


def covers(bounds):
    """Does a cell (lon0, lat0, lon1, lat1) overlap Hong Kong (hklidar.BOUNDS)? Pure, no network."""
    lon0, lat0, lon1, lat1 = (float(v) for v in bounds)
    s, w, n, e = hklidar.BOUNDS
    return lon0 < e and lon1 > w and lat0 < n and lat1 > s


def _key(key):
    return mapcells.CellKey(int(key[0]), int(key[1]), int(key[2]))


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return math.nan
    return f if math.isfinite(f) else math.nan


def _grounds(coords, off, pts, base):
    """The ground level of each row: the lowest finite base among the row, the rows whose outline holds its point and
    the rows whose point lies in its outline (NaN none). Not chained: only the rows' own bases count."""
    inf = np.where(np.isfinite(base), base, np.inf)
    ground = inf.copy()
    order = np.argsort(pts[:, 0], kind="stable")
    xs = pts[order, 0]
    x0, x1 = np.minimum.reduceat(coords[:, 0], off[:-1]), np.maximum.reduceat(coords[:, 0], off[:-1])
    y0, y1 = np.minimum.reduceat(coords[:, 1], off[:-1]), np.maximum.reduceat(coords[:, 1], off[:-1])
    lo, hi = np.searchsorted(xs, x0, "left"), np.searchsorted(xs, x1, "right")
    for j in range(len(base)):
        k = order[lo[j]:hi[j]]
        k = k[(pts[k, 1] >= y0[j]) & (pts[k, 1] <= y1[j]) & (k != j)]
        if len(k):
            k = k[polygon.contains_points(coords[off[j]:off[j + 1]], pts[k])]
        if len(k):
            ground[k] = np.minimum(ground[k], inf[j])
            ground[j] = min(ground[j], inf[k].min())
    return np.where(np.isfinite(ground), ground, np.nan)


def parse(features):
    """The records (dict of arrays) of the layer's ArcGIS JSON features (outlines in lon / lat): one per row with a
    height above ground of MIN_H_M or more. A row's outline is its first ring. Pure (but for the native points)."""
    fast.require(NATIVE, "bdtopo_points")
    rings, base, top, floors, tower = [], [], [], [], []
    for f in features or ():
        if not isinstance(f, dict):
            continue
        a = f.get("attributes") or {}
        g = f.get("geometry") or {}
        try:
            ring = np.asarray(g["rings"][0], np.float64)[:, :2]
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if len(ring) < 4 or not np.isfinite(ring).all():
            continue
        rings.append(ring)
        base.append(_num(a.get("BaseHeight")))
        top.append(_num(a.get("TopHeight")))
        nf = _num(a.get("Storeys"))
        floors.append(int(nf) if 0 < nf < 1000 else 0)
        tower.append(a.get("BuildingBlockType") == "Tower")
    if not rings:
        pts = np.zeros((0, 2))
        area = h = np.zeros(0)
    else:
        off = np.cumsum([0] + [len(r) for r in rings]).astype(np.int64)
        coords = np.ascontiguousarray(np.concatenate(rings))
        pts, area = fast._ef.bdtopo_points(off, coords)
        h = np.asarray(top, np.float64) - _grounds(coords, off, pts, np.asarray(base, np.float64))
    keep = np.isfinite(h) & (h >= MIN_H_M) & np.isfinite(pts).all(axis=1)
    return {"lon": np.asarray(pts[keep, 0], np.float64), "lat": np.asarray(pts[keep, 1], np.float64),
            "area": np.asarray(area, np.float32)[keep], "h": np.asarray(h[keep], np.float32),
            "floors": np.asarray(floors, np.int16)[keep], "tower": np.asarray(tower, bool)[keep]}


def join(feats, recs):
    """The attrs of buildings layer feats with what the records add (a new dict: the layer is shared, it is swapped
    whole), or None when no footprint has a record. The map's own values stay. recs: Records."""
    import mapgeom
    n = len(feats)
    if n == 0 or recs.n == 0:
        return None
    po, ro, co = (np.asarray(a, np.int64) for a in (feats.part_off, feats.ring_off, feats.coord_off))
    has = (po[1:] > po[:-1]) & (np.asarray(feats.kind) == mapgeom.POLYGON)
    has &= ro[np.minimum(po[:-1] + 1, len(ro) - 1)] > ro[np.minimum(po[:-1], len(ro) - 1)]  # (a part with a ring)
    first = ro[np.minimum(po[:-1], len(ro) - 1)]
    first = np.minimum(first, len(co) - 2)
    start = np.where(has, co[first], 0)
    end = np.where(has, co[first + 1], 0)
    lonlat = np.ascontiguousarray(feats.lonlat, np.float64)
    # (the footprints' own areas, as the records' were measured: their first rings side by side)
    off = np.concatenate([[0], np.cumsum(end - start)]).astype(np.int64)
    _pts, area = fast._ef.bdtopo_points(off, lonlat[np.repeat(start - off[:-1], end - start) + np.arange(off[-1])])
    area = np.asarray(area)
    need = COVER * np.where(area > BIG_M2, area, 0.0)

    def match(weight, pool):
        if not len(pool):
            return np.full(n, -1, np.int64)
        got = np.asarray(fast._ef.bdtopo_match(np.ascontiguousarray(start), np.ascontiguousarray(end), lonlat,
                                               recs.lon[pool], recs.lat[pool], weight[pool]), np.int64)
        return np.where(got >= 0, pool[got], -1)

    def pick(pool):
        """Each footprint's record out of the pool's (record indexes; -1 none)."""
        idx = match(recs.weight, pool)
        small = (idx >= 0) & (recs.area[idx] < need)
        if small.any():             # (a podium-wide outline: the podium's record, if that is its size)
            big = match(recs.area.astype(np.float64), pool)
            idx[small] = np.where((big[small] >= 0) & (recs.area[big[small]] >= need[small]), big[small], -1)
        return idx
    # (a record belongs to one building, whoever gets written: every polygon footprint takes part, the ones with parts
    # and the ones with a height of the map's own too)
    idx = pick(np.arange(recs.n))
    settled, free = np.zeros(n, bool), np.ones(recs.n, bool)
    ra = np.maximum(recs.area.astype(np.float64), 1e-3)        # (a record without area: closest to the smallest)
    for _ in range(CLAIM_ROUNDS - 1):
        c = np.flatnonzero(idx >= 0)
        r = idx[c]
        dup = np.bincount(r)[r] >= 2
        if not dup.any():
            break
        c, r = c[dup], r[dup]
        wide, narrow = np.maximum(area[c], ra[r]), np.minimum(area[c], ra[r])
        order = np.lexsort((wide / narrow, r))      # (the closest in area: the least ratio; a tie: stable, lower index)
        c, r = c[order], r[order]
        win = np.r_[True, r[1:] != r[:-1]]
        settled[c[win]] = True
        free[r[win]] = False
        idx = np.where(settled, idx, pick(np.flatnonzero(free)))
    a = dict(feats.attrs)
    n_parts = a.get("has_parts")
    m = (idx >= 0) & ~(np.asarray(n_parts, bool) if n_parts is not None else np.zeros(n, bool))

    def num_col(name):
        v = a.get(name)
        return np.asarray(v, np.float32).copy() if v is not None else np.full(n, np.nan, np.float32)

    def text(name):
        v = a.get(name)
        return np.asarray(v).astype(str).astype(object) if v is not None else np.full(n, "", dtype=object)

    import buildings
    lo, hi = buildings.ML_FLOOR_M
    src = text("height_src")
    height = num_col("height")
    floors = num_col("num_floors")
    rec_h = np.where(m, recs.h[idx], np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        # (metres a storey of the map's own storeys under the record's height; NaN: the map has none)
        per = rec_h / np.where(floors > 0, floors, np.nan)
        rec_f = recs.floors[idx].astype(np.float32)
        rec_f = np.where(m & (rec_f > 0) & (rec_h / rec_f >= lo) & (rec_h / rec_f <= hi), rec_f, np.nan)
    take = m & (~np.isfinite(height) | np.isin(src, list(buildings.ML_HEIGHT_SOURCES))) & ~(per < lo)
    if not take.any():
        return None
    height[take] = rec_h[take]
    src[take] = SOURCE
    a["height"], a["height_src"] = height, mapgeom.text_column(src.tolist())
    t = take & ~(per <= hi)
    floors[t] = rec_f[t]
    a["num_floors"] = floors
    shape = text("roof_shape")
    shape[take & (rec_h >= FLAT_MIN_H_M) & (shape == "")] = "flat"
    a["roof_shape"] = mapgeom.text_column(shape.tolist())
    return a


class Records:
    """A cell's records (FIELDS_NPZ arrays)."""

    def __init__(self, arrays):
        self.lon = np.ascontiguousarray(arrays["lon"], np.float64)
        self.lat = np.ascontiguousarray(arrays["lat"], np.float64)
        self.area = np.ascontiguousarray(arrays["area"], np.float32)
        self.h = np.ascontiguousarray(arrays["h"], np.float32)
        self.floors = np.ascontiguousarray(arrays["floors"], np.int16)
        self.n = len(self.lon)
        self.weight = self.h.astype(np.float64) + np.where(np.asarray(arrays["tower"], bool), TOWER_FIRST, 0.0)


EMPTY = Records({k: np.zeros(0) for k in FIELDS_NPZ})


class Source:
    """The cells' records: fetched in the background, kept on disk and in memory (see the module docstring)."""

    def __init__(self, cache_dir, fetcher):
        self.dir = os.path.join(cache_dir, "hkbuildings", f"v{VERSION}") if cache_dir else None
        self.fetcher = fetcher
        self._lock = threading.Lock()
        self._mem = OrderedDict()           # (z, x, y) -> Records
        self._pending = {}                  # (z, x, y) -> threading.Event of its background download
        self._failed = {}                   # (z, x, y) -> time its download failed
        self._ex = None
        self._slow_until = 0.0
        self.stats = {"cells": 0, "requests": 0, "bytes": 0, "seconds": 0.0, "waits": 0, "missed": 0,
                      "wait_s": 0.0}

    def path(self, key):
        return os.path.join(self.dir, str(key.z), str(key.x), f"{key.y}.npz") if self.dir else None

    def wanted(self, key):
        return ON and covers(_key(key).bounds())

    # ---- what is here ----
    def cached(self, key):
        """The cell's Records in memory or on disk, or None (never the network)."""
        key = _key(key)
        k = (key.z, key.x, key.y)
        with self._lock:
            got = self._mem.get(k)
            if got is not None:
                self._mem.move_to_end(k)
                return got
        p = self.path(key)
        if not p or not os.path.exists(p):
            return None
        try:
            with np.load(p, allow_pickle=False) as z:
                got = Records({f: z[f] for f in FIELDS_NPZ}) if "lon" in z.files else EMPTY
        except Exception as e:          # (cut short, an older layout: fetched again)
            log.debug("hkbuildings read %s: %s", p, e)
            return None
        self._keep(k, got)
        return got

    def _keep(self, k, got):
        with self._lock:
            self._mem[k] = got
            self._mem.move_to_end(k)
            while len(self._mem) > MEM_CELLS:
                self._mem.popitem(last=False)

    # ---- the background downloads ----
    def prefetch(self, keys):
        """Start the downloads of these cells' records (Hong Kong ones not here yet) without waiting; [Event] of
        theirs."""
        evs = []
        if not ON or self.fetcher is None:
            return evs
        for key in keys or ():
            key = _key(key)
            if not covers(key.bounds()):
                continue
            k = (key.z, key.x, key.y)
            with self._lock:
                if k in self._mem:
                    continue
            p = self.path(key)
            if p and os.path.exists(p) and self.cached(key) is not None:     # (on disk: read here, ~1 ms)
                continue
            with self._lock:
                ev = self._pending.get(k)
                if ev is None:
                    if time.time() - self._failed.get(k, -1e18) < RETRY_S:
                        continue
                    ev = self._pending[k] = threading.Event()
                    if self._ex is None:
                        self._ex = ThreadPoolExecutor(THREADS, thread_name_prefix="hkbuildings")
                    self._ex.submit(self._run, key, ev)
            evs.append(ev)
        return evs

    def _run(self, key, ev):
        k = (key.z, key.x, key.y)
        try:
            if self.cached(key) is None:
                self.load(key)
        except Exception as e:
            log.debug("hkbuildings %s: %s: %s", key, type(e).__name__, e)
        finally:
            with self._lock:
                self._pending.pop(k, None)
            ev.set()

    def load(self, key, kind="demand"):
        """The cell's Records, downloaded now when not here (one process at a time: an xproc lock), or None when the
        download failed (asked again after RETRY_S). Blocks: the background threads and saved-area downloads only."""
        import xproc
        key = _key(key)
        got = self.cached(key)
        if got is not None:
            return got
        if not covers(key.bounds()):
            return EMPTY
        k = (key.z, key.x, key.y)
        p = self.path(key)
        mine = False
        if p:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            got, mine = xproc.once(p + ".lock", lambda: self.cached(key), LOCK_WAIT_S, kind)
            if got is not None:
                return got
        try:
            t0 = time.monotonic()
            try:
                arrays = parse(self._features(key, kind))
            except Exception as e:      # (offline, the host down, a bad answer: the predicted buildings meanwhile)
                log.debug("hkbuildings fetch %s: %s: %s", key, type(e).__name__, e)
                with self._lock:
                    self._failed[k] = time.time()
                return None
            got = Records(arrays) if len(arrays["lon"]) else EMPTY
            if p:
                import staging
                tmp = staging.tmp_for(p)
                with open(tmp, "wb") as fh:
                    np.savez_compressed(fh, **({f: arrays[f] for f in FIELDS_NPZ} if got.n else {"empty": np.zeros(0)}))
                staging.replace_cached(tmp, p)
            with self._lock:
                self.stats["cells"] += 1
                self.stats["seconds"] += time.monotonic() - t0
                self._failed.pop(k, None)
            self._keep(k, got)
            return got
        finally:
            if mine:
                xproc.release(p + ".lock")

    def _features(self, key, kind):
        lon0, lat0, lon1, lat1 = key.bounds()
        e, n = hkdtm.grid(np.array([lat0, lat0, lat1, lat1]), np.array([lon0, lon1, lon0, lon1]))
        env = f"{e.min() - PAD_M:.1f},{n.min() - PAD_M:.1f},{e.max() + PAD_M:.1f},{n.max() + PAD_M:.1f}"

        def get_json(url):
            body = self.fetcher.get(url, kind=kind).body
            with self._lock:
                self.stats["requests"] += 1
                self.stats["bytes"] += len(body)
            return json.loads(body.decode("utf-8"))
        # (the box in HK1980 Grid: given in WGS 84 it returned fewer rows; the outlines back in WGS 84)
        return hklidar.arcgis(get_json, URL, {
            "where": "1=1", "geometry": env, "geometryType": "esriGeometryEnvelope", "inSR": 2326,
            "spatialRel": "esriSpatialRelIntersects", "outSR": 4326, "geometryPrecision": 7, "outFields": FIELDS,
            "orderByFields": "OBJECTID"})

    # ---- a tile's wait ----
    def wait(self, keys):
        """Wait for these cells' records together (their downloads started now if not yet), at most WAIT_S (SLOW_WAIT_S
        for SLOW_FOR_S after one did not come). True when all Hong Kong ones are here."""
        if not ON:
            return True
        evs = self.prefetch(keys)
        if not evs:
            return all(self.cached(k) is not None for k in keys if covers(_key(k).bounds()))
        t0 = time.monotonic()
        budget = SLOW_WAIT_S if t0 < self._slow_until else WAIT_S
        t_end = t0 + budget
        try:
            for ev in evs:
                if not ev.wait(max(0.0, t_end - time.monotonic())):
                    with self._lock:
                        self.stats["missed"] += 1
                    self._slow_until = time.monotonic() + SLOW_FOR_S
                    return False
            return all(self.cached(k) is not None for k in keys if covers(_key(k).bounds()))
        finally:
            with self._lock:
                self.stats["waits"] += 1
                self.stats["wait_s"] += time.monotonic() - t0


def credited_near(cache_dir, lat, lon, near_m):
    """Did Building layer records with buildings come to this computer for the cells within near_m of lat, lon
    (credits: the Government's terms ask for the source)? On-disk records only, never a download."""
    if not ON or not cache_dir:
        return False
    d = os.path.join(cache_dir, "hkbuildings", f"v{VERSION}")
    dlat = near_m / 110540.0
    dlon = near_m / (111320.0 * max(0.1, dmath.cos(math.radians(lat))))
    try:
        keys = mapcells.cells_for_bbox(lat - dlat, lon - dlon, lat + dlat, lon + dlon, max_cells=64)
    except ValueError:
        return False
    for k in keys:
        try:
            if os.path.getsize(os.path.join(d, str(k.z), str(k.x), f"{k.y}.npz")) > 400:   # (an empty set: ~200 B)
                return True
        except OSError:
            pass
    return False
