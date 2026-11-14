"""OpenStreetMap's construction years of buildings (start_date), for the towers' facades by era (buildings.plan:
TOWER_MASONRY_YEAR, TOWER_MIXED_YEAR) and the other era rules (buildings.era).

Overture's buildings have no year (start_date is one of the OSM tags it drops), but each keeps its OSM way or relation
(src_record "w34633854@12"). tools/build_osm_building_years.py cuts the dated buildings of OSM US's Layercake buildings
file (ODbL; 17.4 M of them, 2026-11-14) into zoom-12 tiles in PMTiles parts, as the road tag shards are
(osmtagshards.py: the same reader, YearShards); a tile is a few KB (Manhattan's 1,700 dated buildings: 5 KB).

A road tile asks for the year tiles under it as it starts (Fetch, on a thread); its buildings wait WAIT_S at most and
take the year of their way (key: 2 x id, + 1 for a relation), unless they have one (3DBAG's).

The build used is the one pinned on disk when this process first asks (YearShards.frozen), the same for the whole
process, and it is the building tiles' key (identity(): MapData.versions(buildings=True), "off" or "none" without one;
Codex review of 751dc6b2: a tile built without years kept its key after the dataset came out, beside new ones with
them). A newer manifest is fetched and pinned in the background (MapData.warm_manifests) for the next start.
- Off (BNE_OSMYEARS=off, BNE_RAILTAGS=off), nothing pinned at the process's first ask (not published yet, a first
  run), or a manifest this cannot read: identity "off" / "none", no years, nothing faulted.
- A tile that could not be had (offline, the host failing, a directory entry past MAX_TILE_BYTES): no years and the
  build faulted (revisit.fault: built again later, with them); not asked again for DOWN_S.

    BNE_OSMYEARS_BASE   the dataset's folder elsewhere than <BNE_DATA_PAGES>/osmyears (a staged build)

Data (c) OpenStreetMap contributors, ODbL 1.0 (Layercake: OpenStreetMap US).
"""
import hashlib
import logging
import os
import re
import threading
import time

import numpy as np

import fast
import netfetch
import osmtagshards

log = logging.getLogger("earthd.osmyears")

ON = os.environ.get("BNE_OSMYEARS", "on") != "off"
BASE = os.environ.get("BNE_OSMYEARS_BASE")
NATIVE = 298                    # earthfast.VERSION of oby_decode
WAIT_S = 10.0                   # s a tile's buildings wait for its year tiles at most
CELL_ZOOM = 14                  # (the tile's map cells, each mapped to the shards' zoom)

_EMPTY = (np.zeros(0, np.uint64), np.zeros(0, np.int16))


class YearShards(osmtagshards.TagShards):
    NAME, FORMAT, NATIVE, LABEL = "osmyears", 1, NATIVE, "osm years shards"
    MAX_TILE_BYTES = 8 << 20        # a tile's stored length at most (a dense European z12 tile is ~1 MB)
    MAX_LEAF_BYTES = 4 << 20        # a leaf directory's

    _UNSET = object()

    def __init__(self, cache_dir, fetcher):
        super().__init__(cache_dir, fetcher)
        self._frozen = self._UNSET

    def _on(self):
        return ON

    def _base(self):
        return BASE

    def _valid(self, m):
        return True

    def _decode(self, b, m):
        if len(b) > self.MAX_TILE_BYTES:
            raise ValueError(f"tile of {len(b)} bytes")
        return fast._ef.oby_decode(b)

    def version(self):
        """The newest build (pinned on disk for the next start; this process keeps its frozen one)."""
        return self.pages.version()

    def frozen(self):
        """(build, manifest) pinned on disk when this process first asked, or None (none, or one this cannot read)."""
        with self._lock:
            if self._frozen is not self._UNSET:
                return self._frozen
        got = None
        import json
        import railtags
        if self._on() and self.fetcher is not None and railtags.ON and self.pages.dir:
            try:
                with open(os.path.join(self.pages.dir, "manifest.json"), encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, ValueError, TypeError):
                m = None
            parts = osmtagshards.parts_of(m) if isinstance(m, dict) else None
            checks = pinned_parts(m) if parts is not None else None
            if checks is not None and isinstance(m.get("built"), str) and 0 < len(m["built"]) <= 256 \
                    and m.get("format") == self.FORMAT and isinstance(m.get("zoom"), int) and 0 < m["zoom"] <= 14:
                m["_parts"] = parts
                m["_firsts"] = [p[0] for p in parts]
                m["_checks"] = checks
                got = (m["built"], m)
        with self._lock:
            if self._frozen is self._UNSET:
                self._frozen = got
                if got is not None:
                    self._man = got
            return self._frozen

    def _check_head(self, part, b):
        got = self.frozen()
        want = got[1]["_checks"].get(part[3]) if got is not None else None
        if want is None or hashlib.sha256(bytes(b)).hexdigest() != want[0]:
            raise osmtagshards.ShardError(f"{self.LABEL}: {part[2]}: not the archive build {got and got[0]} names")

    def _check_size(self, url, total):
        got = self.frozen()
        if got is None:
            return
        for p in got[1]["_parts"]:
            if self._url(p) == url and got[1]["_checks"][p[3]][1] != total:
                raise osmtagshards.Changed(f"{self.LABEL}: {url}: {total} bytes, its manifest names "
                                           f"{got[1]['_checks'][p[3]][1]}")

    def manifest(self):
        """The frozen build (never a newer one: the tiles' keys name it); None during DOWN_S (a tile failed)."""
        if time.monotonic() - self._down < osmtagshards.DOWN_S:
            return None
        return self.frozen()

    def usable(self):
        """manifest()'s (build, manifest); False: none to read (off, none frozen); None: a tile failed a moment ago."""
        got = self.frozen()
        if got is None:
            return False
        return self.manifest()


_SHARDS = {}
_LOCK = threading.Lock()
_POOL = None


def shards(md):
    """The year shards of map data md (one per cache folder and fetcher), None when off or md has no fetcher."""
    fetcher = getattr(md, "fetcher", None)
    cache_dir = getattr(md, "cache_dir", None)
    if not ON or fetcher is None or not isinstance(cache_dir, str) or not cache_dir:
        return None
    k = (cache_dir, id(fetcher), BASE)
    with _LOCK:
        got = _SHARDS.get(k)
        if got is None:
            got = _SHARDS[k] = YearShards(cache_dir, fetcher)
        return got


def pinned_parts(m):
    """{part subfolder (parts_of's "p<i>"): (head_sha256, bytes)} of manifest m, or None when m does not pin every part
    to its build: an archive in the build's own folder (m["key"]: "<key>/osmyears-000.pmtiles", immutable once
    published), with its first read's digest and its size (Codex review of b1f2499b: fixed names let a process
    frozen on one build read the next one's tiles under the old identity)."""
    key = m.get("key") if isinstance(m, dict) else None
    ps = m.get("parts") if isinstance(m, dict) else None
    if not isinstance(key, str) or not re.fullmatch(r"[0-9A-Za-z_.-]{4,80}", key) or not isinstance(ps, list):
        return None
    out = {}
    for i, p in enumerate(ps):
        a, h, n = p.get("archive"), p.get("head_sha256"), p.get("bytes")
        if not isinstance(a, str) or f"{key}/" not in a or not isinstance(h, str) \
                or not re.fullmatch(r"[0-9a-f]{64}", h) or not isinstance(n, int) or n < 127:
            return None
        out[f"p{i}"] = (h, n)
    return out


def identity(md):
    """The building tiles' key for the years of map data md: "off", "none" (nothing frozen), or the frozen build."""
    try:
        sh = shards(md)
    except Exception:
        return "off"
    if sh is None:
        return "off"
    got = sh.frozen()
    return got[0] if got is not None else "none"


def key_of(record):
    """The year tiles' key of an Overture source record ("w123@4" -> 246, "r7@1" -> 15), else None."""
    m = re.match(r"^([wr])(\d+)", record or "")
    return 2 * int(m.group(2)) + (1 if m.group(1) == "r" else 0) if m else None


def tiles_of(sh, lat0, lon0, lat1, lon1):
    """(keys ascending, years) of the year tiles over a lat / lon box; None: not had (a fault); _EMPTY: none in use."""
    import mapcells
    got = sh.usable()
    if not got:
        return _EMPTY if got is False else None
    z = got[1]["zoom"]
    sh_ = max(0, CELL_ZOOM - z)
    tiles = sorted({(c.x >> sh_, c.y >> sh_) for c in mapcells.cells_for_bbox(lat0, lon0, lat1, lon1, z=CELL_ZOOM)})
    ks, ys = [], []
    for x, y in tiles:
        try:
            t = sh.tile(mapcells.CellKey(z, x, y))
        except osmtagshards.NotInUse:
            return None
        except Exception as e:      # (offline, limits, the host failing, a damaged tile: built again later)
            log.debug("osm years tile %d/%d/%d: %s: %s", z, x, y, type(e).__name__, e)
            if not isinstance(e, netfetch.Deferred):
                sh.down(e)              # (not asked again for DOWN_S: the tiles meanwhile fault at once, no waits)
            return None
        if t is not None:
            ks.append(np.asarray(t[0], np.uint64))
            ys.append(np.asarray(t[1], np.int16))
    if not ks:
        return _EMPTY
    k, y = np.concatenate(ks), np.concatenate(ys)
    k, first = np.unique(k, return_index=True)          # (a building on a tile edge is in both: one year)
    return k, y[first]


class Fetch:
    """A tile's year tiles asked for on a thread as the tile starts; years() waits WAIT_S at most."""

    def __init__(self, frame, x0, y0, x1, y1, md):
        global _POOL
        self.fut = None
        try:
            sh = shards(md)
        except Exception as e:          # (no native module at 298, a cache folder it cannot use: no years)
            log.warning("osm years: %s: %s", type(e).__name__, e)
            return
        if sh is None:
            return
        la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1], np.float64),
                                         np.array([y0, y0, y1, y1], np.float64), 0.0)
        box = (float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo)))
        with _LOCK:
            if _POOL is None:
                from concurrent.futures import ThreadPoolExecutor
                _POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="osmyears")
        self.fut = _POOL.submit(tiles_of, sh, *box)

    def years(self):
        """Years (keys, years) of the tile's buildings, or None (none in use; one not had faults the tile)."""
        if self.fut is None:
            return None
        try:
            got = self.fut.result(timeout=WAIT_S)
        except Exception as e:
            log.debug("osm years: %s: %s", type(e).__name__, e)
            got = None
        if got is None:
            import revisit
            revisit.fault()             # (a later visit builds it with the years)
            return None
        return got if len(got[0]) else None


def lookup(years, records):
    """The years (int, 0 where none) of src_record strings records, from years (Fetch.years's)."""
    out = np.zeros(len(records), np.int64)
    if years is None or not len(records):
        return out
    keys, ys = years
    if not len(keys):
        return out
    q = np.array([key_of(str(r)) if r is not None else None for r in records], object)
    ok = np.array([v is not None for v in q], bool)
    if not ok.any():
        return out
    qk = np.array([int(v) for v in q[ok]], np.uint64)
    j = np.minimum(np.searchsorted(keys, qk), len(keys) - 1)
    hit = keys[j] == qk
    sub = np.zeros(len(qk), np.int64)
    sub[hit] = ys[j[hit]]
    out[ok] = sub
    return out
