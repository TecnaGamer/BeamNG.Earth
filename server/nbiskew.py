"""The US National Bridge Inventory's skews (FHWA NBI, public domain, US DOT) as a static table on the project's tile
host, built by the data repo's nbiskew workflow (BeamNG.Earth-data tools/build_nbiskew.py) from FHWA's delimited
files of every state:

  nbiskew     one npz (~5 MB): per record carrying a structure (item 5A record type 1) with its coordinates (items 16 /
              17): lat, lon (float32, the rows sorted by lat, then lon), skew (uint8: item 34, degrees; 99 the
              inventory's "major variation"), length (uint16: item 49, the structure's length in dm, 0 unknown).

roadshape's deck-end skew (BNE_ROADSHAPE decks, BNE_DECK_SKEW; earthfast 627 deck_shape's nbi) reads it: a deck whose
line has a record within roadshape.NBI_MATCH_M takes the inventory's skew over the map's rule. Read only when cdn.json
(the data repo's switch) has {"nbiskew": {"enabled": true, "version": "v1"}}, or BNGE_NBISKEW names a version ("v1") or
"off"; BNGE_NBISKEW_DIR reads a local build (<dir>/nbiskew/manifest.json, <dir>/nbiskew/nbiskew.npz), nothing fetched.
The manifest (format 1, the file's size and SHA-256, "build": 16 hex of the content) is decided once per server run for
every process and pinned on disk per version (winterpacks.Pack: the same reader); the file is checked against it
before it is kept or read. What a consumer keys on: identity() ("v1-<build>", or None: no table this run), in the map
data's versions (mapdata.MapData.versions) while the skew rule reads it. No table: the map's rule alone (the fallback).

    nbiskew.configure(cache_dir, fetcher)       (mapdata.MapData: once per process)
    nbiskew.identity()                          the cache-key part
    t = nbiskew.table()                         Table(lat, lon, skew, length) or None
    t.near(lat0, lon0, lat1, lon1, pad_m)       indices of the records in the box (pad m round it), ascending
"""
import io
import math
import json
import logging
import os
import threading
import time
from collections import namedtuple

import numpy as np

import dmath
import winterpacks

log = logging.getLogger("earthd.nbiskew")

NAME = "nbiskew"
ENV = "BNGE_NBISKEW"
LOCAL_ENV = "BNGE_NBISKEW_DIR"
FILE = "nbiskew.npz"
COLUMNS = {"lat": np.float32, "lon": np.float32, "skew": np.uint8, "length": np.uint16}
MAX_RECORDS = 2_000_000
VARIES = 99


class _Pack(winterpacks.Pack):
    ROOT = "nbiskew"

    def env_value(self):
        return os.environ.get("BNGE_NBISKEW", "")

    def check(self, m, version=None):
        return check_manifest(m, version)


def check_manifest(m, version=None):
    """m when it is a manifest of this table (and version) this reader understands, else None."""
    if not isinstance(m, dict) or m.get("format") != 1 or m.get("name") != NAME:
        return None
    if not winterpacks.VERSION_RE.fullmatch(str(m.get("version"))) or (version and m["version"] != version):
        return None
    if not winterpacks.BUILD_RE.fullmatch(str(m.get("build"))):
        return None
    f = m.get("files")
    n = m.get("records")
    if not isinstance(f, list) or len(f) != 1 or not isinstance(f[0], dict) or f[0].get("path") != FILE \
            or not winterpacks._entry_ok(f[0].get("size"), f[0].get("sha256")) \
            or not isinstance(n, int) or isinstance(n, bool) or not 0 < n <= MAX_RECORDS:
        return None
    return m


class Table(namedtuple("Table", "lat lon skew length")):
    def near(self, lat0, lon0, lat1, lon1, pad_m):
        """Indices (ascending) of the records in the box lat0..lat1, lon0..lon1 grown by pad_m."""
        dlat = pad_m / 110000.0 + 1e-6
        i0 = int(np.searchsorted(self.lat, np.float32(lat0 - dlat), side="left"))
        i1 = int(np.searchsorted(self.lat, np.float32(lat1 + dlat), side="right"))
        if i1 <= i0:
            return np.zeros(0, np.int64)
        mid = 0.5 * (lat0 + lat1)
        kx = max(dmath.cos(math.radians(min(abs(mid), 89.0))), 0.01)
        dlon = pad_m / (111320.0 * kx) + 1e-6
        lo = self.lon[i0:i1]
        keep = np.flatnonzero((lo >= np.float32(lon0 - dlon)) & (lo <= np.float32(lon1 + dlon)))
        return (keep + i0).astype(np.int64)


def decode(body):
    """The table's arrays from the npz bytes (no pickles), checked: the columns, dtypes and lengths agree, lat sorted."""
    with np.load(io.BytesIO(body), allow_pickle=False) as z:
        if sorted(z.files) != sorted(COLUMNS):
            raise ValueError(f"columns {sorted(z.files)}")
        cols = {k: z[k] for k in COLUMNS}
    n = len(cols["lat"])
    for k, dt in COLUMNS.items():
        if cols[k].dtype != dt or cols[k].shape != (n,):
            raise ValueError(f"{k}: {cols[k].dtype} {cols[k].shape}")
    if not 0 < n <= MAX_RECORDS or not np.isfinite(cols["lat"]).all() or not np.isfinite(cols["lon"]).all() \
            or np.any(np.diff(cols["lat"]) < 0):
        raise ValueError("records out of order or not finite")
    return Table(cols["lat"], cols["lon"], cols["skew"], cols["length"])


_lock = threading.Lock()
_pack = None
_table = None           # Table, or False: not had (asked again after winterpacks.RETRY_S)
_table_at = -1e18


def configure(cache_dir, fetcher, local=None):
    """The process's reader (mapdata.MapData's cache folder and fetcher)."""
    global _pack, _table, _table_at
    with _lock:
        _pack = _Pack(cache_dir, fetcher, NAME, local or os.environ.get(LOCAL_ENV) or None)
        _table, _table_at = None, -1e18


def identity():
    """"<version>-<build>" of the table in use this run, or None (none: off, not published, not had)."""
    p = _pack
    return p.version() if p is not None else None


def table():
    """The Table, or None (no table this run, or its file not had now)."""
    global _table, _table_at
    p = _pack
    if p is None:
        return None
    with _lock:
        if _table:
            return _table
        recent = _table is False and time.monotonic() - _table_at < winterpacks.RETRY_S
    m = p.manifest()
    if m is None:
        return None
    if recent:
        # (the run keys on the table (identity) but has not got its file: what is built without it is not kept)
        import revisit
        revisit.fault()
        return None
    got = None
    if m is not None:
        f = m["files"][0]
        body = p.file(FILE, f["size"], f["sha256"])
        if body is None:
            import revisit
            revisit.fault()
        else:
            try:
                got = decode(body)
                if len(got.lat) != m["records"]:
                    raise ValueError(f"{len(got.lat)} records, the manifest's {m['records']}")
            except Exception as e:      # (a table that does not read: none, and the build not kept)
                import revisit
                revisit.fault()
                log.info("nbiskew: %s: %s", type(e).__name__, e)
                got = None
    with _lock:
        if got is not None:
            _table = got
        else:
            _table, _table_at = False, time.monotonic()
        return got


def describe():
    """(tools) the table in use as a dict."""
    t = table()
    return {"identity": identity(), "records": None if t is None else int(len(t.lat))}


if __name__ == "__main__":
    print(json.dumps(describe()))
