"""France's agricultural parcels: IGN RPG `RPG.LATEST:parcelles_graphiques` (Etalab Licence Ouverte 2.0) for lavender
and lavandin fields (`code_cultu` in `LAV`, `LVH`).

All parcel filtering, Web Mercator projection, dominant-edge orientation, lattice sampling, CHM mask rasterisation and
deterministic `blake2` PRNG run in `native/earthfast/src/ignrpg.rs` (zero Python geometry fallback).
"""
from collections import OrderedDict
import gzip
import json
import logging
import os
import threading
import time

import numpy as np

import fast
import mapcells

log = logging.getLogger("earthd.ignrpg")

ON = os.environ.get("BNE_IGNRPG", "on") != "off"
NATIVE = 561
VERSION = 1
WFS_URL = "https://data.geopf.fr/wfs/ows"
LANE = "ign_wfs"
TYPENAME = "RPG.LATEST:parcelles_graphiques"
PAGE = 2000
PAGES = 6
RETRY_S = 600.0
LOCK_WAIT_S = 90.0
MEM_CELLS = 128

FIELDS_NPZ = ("lonlat", "coord_off", "ring_off", "seeds")


def _key(key):
    return mapcells.CellKey(int(key[0]), int(key[1]), int(key[2]))


def covers(bounds):
    """Does a cell (lon0, lat0, lon1, lat1) overlap France? Pure, native kernel."""
    fast.require(NATIVE, "ignrpg_covers")
    lon0, lat0, lon1, lat1 = (float(v) for v in bounds)
    return bool(fast._ef.ignrpg_covers((lon0, lat0, lon1, lat1)))


def parse(raw):
    """Parse IGN RPG GeoJSON bytes, str, FeatureCollection dict, or feature list into lavender parcel arrays via Rust."""
    fast.require(NATIVE, "ignrpg_parse_lavender")
    if isinstance(raw, (bytes, bytearray, memoryview)):
        data = bytes(raw)
    elif isinstance(raw, str):
        data = raw.encode("utf-8")
    elif isinstance(raw, dict):
        data = json.dumps(raw, separators=(",", ":")).encode("utf-8")
    elif isinstance(raw, (list, tuple)):
        data = json.dumps({"type": "FeatureCollection", "features": list(raw)}, separators=(",", ":")).encode("utf-8")
    else:
        data = b""
    lonlat, coord_off, ring_off, seeds = fast._ef.ignrpg_parse_lavender(data)
    return {
        "lonlat": np.ascontiguousarray(lonlat, np.float64),
        "coord_off": np.ascontiguousarray(coord_off, np.int64),
        "ring_off": np.ascontiguousarray(ring_off, np.int64),
        "seeds": np.ascontiguousarray(seeds, np.uint64),
    }


class Records:
    """A cell's parsed IGN RPG lavender parcels."""

    def __init__(self, arrays):
        self.lonlat = np.ascontiguousarray(arrays["lonlat"], np.float64)
        self.coord_off = np.ascontiguousarray(arrays["coord_off"], np.int64)
        self.ring_off = np.ascontiguousarray(arrays["ring_off"], np.int64)
        self.seeds = np.ascontiguousarray(arrays["seeds"], np.uint64)
        self.n = len(self.seeds)
        self.raw = arrays


EMPTY = Records({
    "lonlat": np.zeros((0, 2), np.float64),
    "coord_off": np.zeros(1, np.int64),
    "ring_off": np.zeros(1, np.int64),
    "seeds": np.zeros(0, np.uint64),
})


def lavender_rows(recs, chm, blocked, bounds, mask=None):
    """Sample oriented lavender rows inside `recs` via `earthfast.ignrpg_lavender_rows`."""
    fast.require(NATIVE, "ignrpg_lavender_rows")
    if recs is None or getattr(recs, "n", 0) == 0:
        z64 = np.zeros(0, np.float64)
        z32 = np.zeros(0, np.float32)
        return z64, z64, z32, z32, z32, z32
    bl = np.ascontiguousarray(np.asarray(blocked, np.uint8))
    if mask is None:
        m_u8 = np.zeros(bl.shape, np.uint8)
    else:
        m_u8 = mask.view(np.uint8)
    grid = (float(chm.x0), float(chm.y0), float(chm.dx), float(chm.dy))
    b = tuple(float(v) for v in bounds)
    return fast._ef.ignrpg_lavender_rows(recs.lonlat, recs.coord_off, recs.ring_off, recs.seeds, bl, m_u8, grid, b)


class Source:
    """Per-cell IGN RPG lavender records: fetched via netfetch.Fetcher (gzip), kept on disk and in memory."""

    def __init__(self, cache_dir, fetcher=None):
        self.dir = os.path.join(cache_dir, "ignrpg", f"v{VERSION}") if cache_dir else None
        self.fetcher = fetcher
        self._lock = threading.Lock()
        self._mem = OrderedDict()
        self._failed = {}
        self.stats = {"cells": 0, "requests": 0, "bytes": 0, "seconds": 0.0}

    def path(self, key):
        key = _key(key)
        return os.path.join(self.dir, str(key.z), str(key.x), f"{key.y}.npz") if self.dir else None

    def wanted(self, key):
        return ON and covers(_key(key).bounds())

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
                got = Records({f: z[f] for f in FIELDS_NPZ}) if "seeds" in z.files else EMPTY
        except Exception as e:
            log.debug("ignrpg read %s: %s", p, e)
            return None
        self._keep(k, got)
        return got

    def _keep(self, k, got):
        with self._lock:
            self._mem[k] = got
            self._mem.move_to_end(k)
            while len(self._mem) > MEM_CELLS:
                self._mem.popitem(last=False)

    def cell(self, key, kind="demand"):
        """Return Records for cell `key`, or EMPTY outside France / when offline without a fetcher."""
        if not ON:
            return EMPTY
        key = _key(key)
        if not covers(key.bounds()):
            return EMPTY
        got = self.cached(key)
        if got is not None:
            return got
        if self.fetcher is None:
            return EMPTY
        loaded = self.load(key, kind=kind)
        return EMPTY if loaded is None else loaded

    def load(self, key, kind="demand"):
        """Download and cache the cell's Records now (one process at a time via xproc lock), or None on failure."""
        import xproc
        key = _key(key)
        got = self.cached(key)
        if got is not None:
            return got
        if not covers(key.bounds()):
            return EMPTY
        k = (key.z, key.x, key.y)
        with self._lock:
            if time.time() - self._failed.get(k, -1e18) < RETRY_S:
                return None
        if self.fetcher is None:
            return None
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
                arrays = parse(self._fetch(key, kind))
            except Exception as e:
                log.debug("ignrpg fetch %s: %s: %s", key, type(e).__name__, e)
                with self._lock:
                    self._failed[k] = time.time()
                return None
            got = Records(arrays) if len(arrays["seeds"]) else EMPTY
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

    def _fetch(self, key, kind):
        lon0, lat0, lon1, lat1 = key.bounds()
        feats = None
        for page in range(PAGES):
            url = (f"{WFS_URL}?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature&TYPENAMES={TYPENAME}"
                   f"&BBOX={lon0:.6f},{lat0:.6f},{lon1:.6f},{lat1:.6f},CRS:84&SRSNAME=CRS:84"
                   f"&OUTPUTFORMAT=application/json&COUNT={PAGE}&STARTINDEX={page * PAGE}")
            res = self.fetcher.get(url, kind=kind, lane=LANE, headers={"Accept-Encoding": "gzip"})
            body = res.body
            if body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            with self._lock:
                self.stats["requests"] += 1
                self.stats["bytes"] += len(res.body)
            if page == 0:
                doc = json.loads(body.decode("utf-8"))
                got = doc.get("features") or []
                if len(got) < PAGE:
                    return body
                feats = list(got)
            else:
                got = json.loads(body.decode("utf-8")).get("features") or []
                feats.extend(got)
                if len(got) < PAGE:
                    return feats
        return feats or []
