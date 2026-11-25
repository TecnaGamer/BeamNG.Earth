"""OpenStreetMap's mapped road areas (area:highway=* polygons: the actual paved width beside the centre line, traffic
islands, junction areas) for the road system (BNE_ROADSYS, native/roadsys areas.rs).

tools/build_osm_road_areas.py cuts the planet's ~485k area:highway polygons (2026-11-23: traffic islands 153k,
footways 98k, residential 33k, service 31k, tertiary 21k, secondary 17k ...; densest in Poland and Germany) into
zoom-14 tiles (one per map cell) in PMTiles parts, as the building years are (osmyears.py: the same reader); a tile is
a few KB to ~100 KB.

    BNE_ROADSYS_AREAS   off (default): nothing here runs, nothing is fetched or keyed.
                        audit: the road system's products (the shadow tiles, M4's decks) read the areas of their cells
                            and report how their surface meets them (the product's JSON "areas"); the surface itself
                            is unchanged.
                        on: also the carriageway edges' soft targets (areas.rs fit: lane widths within the lane rules'
                            bounds, the outer shoulders, a lateral shift); what it builds is keyed (revisit: a BNE_
                            switch; MapData.versions()["osmareas"]: the build in use).
    BNE_OSMAREAS_BASE   the dataset's folder elsewhere than <BNE_DATA_PAGES>/osmareas (a staged build on a host)
    BNE_OSMAREAS_DIR    a staged build on disk (manifest.json and its <key>/ parts: tools, tests, measurements), read
                        without the network

The build used is the server run's (AreaShards.frozen, as osmyears': pagesdata's session), the same in every process
of the run, and it is in the keys (identity()); with BNE_OSMAREAS_DIR the folder's own manifest. A tile that
could not be had makes no areas for the read and faults the build (revisit.fault: built again later, with them).

Data (c) OpenStreetMap contributors, ODbL 1.0.
"""
import hashlib
import json
import logging
import os
import threading
from collections import OrderedDict

import numpy as np

import fast
import netfetch
import osmtagshards
import osmyears

log = logging.getLogger("earthd.osmareas")

MODE = os.environ.get("BNE_ROADSYS_AREAS", "off").strip().lower()
ON = MODE in ("on", "audit")
FIT = MODE == "on"
BASE = os.environ.get("BNE_OSMAREAS_BASE")
DIR = os.environ.get("BNE_OSMAREAS_DIR")
NATIVE = 608                    # earthfast.VERSION of oah_decode, RoadsysAreas
LOCAL_BASE = "https://osmareas.local/osmareas"      # (BNE_OSMAREAS_DIR's files under this name: LocalFetcher)
CELL_ZOOM = 14
MEM_CELLS = 512                 # projected cells kept per process (by build, cell and frame)


class LocalFetcher:
    """A staged build's files (BNE_OSMAREAS_DIR) answered as the network would: whole files, or a Range."""

    def __init__(self, root):
        self.root = os.path.realpath(root)

    def get(self, url, *, kind="demand", headers=None, lane=None, max_bytes=None):
        if not url.startswith(LOCAL_BASE + "/"):
            raise netfetch.NotFound(f"{url}: not in {self.root}", url, "osmareas.local", status=404)
        path = os.path.realpath(os.path.join(self.root, url[len(LOCAL_BASE) + 1:]))
        if not path.startswith(self.root + os.sep) or not os.path.isfile(path):
            raise netfetch.NotFound(f"{url}: 404", url, "osmareas.local", status=404)
        size = os.path.getsize(path)
        rng = (headers or {}).get("Range")
        with open(path, "rb") as f:
            if not rng:
                return netfetch.Response(200, f.read(), {})
            a, b = rng.split("=", 1)[1].split("-")
            a, b = int(a), min(int(b), size - 1)
            f.seek(a)
            body = f.read(b - a + 1)
        st = os.stat(path)
        return netfetch.Response(206, body, {"etag": f'"{st.st_size}-{st.st_mtime_ns}"',
                                             "content-range": f"bytes {a}-{b}/{size}"})


class AreaShards(osmyears.YearShards):
    NAME, FORMAT, NATIVE, LABEL = "osmareas", 1, NATIVE, "osm area shards"
    MAX_TILE_BYTES = 8 << 20        # a tile's stored length at most (the densest, Wroclaw's, ~100 KB)
    MAX_LEAF_BYTES = 4 << 20

    def _on(self):
        return ON

    def _base(self):
        return LOCAL_BASE if DIR else BASE

    def _decode(self, b, m):
        if len(b) > self.MAX_TILE_BYTES:
            raise ValueError(f"tile of {len(b)} bytes")
        return fast._ef.oah_decode(b)

    def frozen(self):
        """The build in use: BNE_OSMAREAS_DIR's own manifest, else the one pinned on disk at the first ask."""
        if not DIR:
            return super().frozen()
        with self._lock:
            if self._frozen is not self._UNSET:
                return self._frozen
        got = None
        try:
            with open(os.path.join(DIR, "manifest.json"), encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            m = None
        parts = osmtagshards.parts_of(m) if isinstance(m, dict) else None
        checks = osmyears.pinned_parts(m) if parts is not None else None
        if checks is not None and m.get("format") == self.FORMAT and m.get("zoom") == CELL_ZOOM \
                and isinstance(m.get("built"), str):
            m["_parts"], m["_firsts"], m["_checks"] = parts, [p[0] for p in parts], checks
            got = (m["built"] + "/" + str(m.get("key")), m)
        with self._lock:
            if self._frozen is self._UNSET:
                self._frozen = got
                if got is not None:
                    self._man = got
            return self._frozen

    def manifest(self):
        if DIR:
            return self.frozen()
        return super().manifest()


_SHARDS = {}
_LOCK = threading.Lock()
_MEM = OrderedDict()


def shards(md):
    """The area shards of map data md (one per cache folder and fetcher), None when off."""
    if not ON:
        return None
    cache_dir = getattr(md, "cache_dir", None)
    fetcher = LocalFetcher(DIR) if DIR else getattr(md, "fetcher", None)
    if fetcher is None or not isinstance(cache_dir, str) or not cache_dir:
        return None
    k = (cache_dir, DIR or id(fetcher), BASE)
    with _LOCK:
        got = _SHARDS.get(k)
        if got is None:
            got = _SHARDS[k] = AreaShards(cache_dir, fetcher)
        return got


def identity(md):
    """The road products' key for the areas of map data md: "off", "none" (nothing frozen), or the frozen build."""
    try:
        sh = shards(md)
    except Exception:
        return "off"
    if sh is None:
        return "off"
    got = sh.frozen()
    return got[0] if got is not None else "none"


def build_id(md):
    """identity() as a 64-bit number (the native keys' word)."""
    return int(hashlib.sha256(identity(md).encode()).hexdigest()[:15], 16)


def areas_for(md, cells, frame):
    """earthfast.RoadsysAreas of the map cells' areas (each polygon once, by key) in frame (geotiles.WorldFrame), or
    None: off, none in use, none in these cells, or a tile not had (then revisit.fault: built again later)."""
    polys = polygons_for(md, cells, frame)
    if not polys:
        return None
    keys = sorted(polys)
    values, layers, flags, ring_poly, ring_role, pt_off, pts = [], [], [], [], [], [0], []
    for i, k in enumerate(keys):
        v, layer, fl, rings = polys[k]
        values.append(v)
        layers.append(layer)
        flags.append(fl)
        for role, xy in rings:
            ring_poly.append(i)
            ring_role.append(role)
            pts.append(xy)
            pt_off.append(pt_off[-1] + len(xy))
    p = np.ascontiguousarray(np.concatenate(pts) if pts else np.zeros((0, 2)), np.float64)
    return fast._ef.RoadsysAreas(np.asarray(keys, np.uint64), values, np.asarray(layers, np.int64),
                                 np.asarray(flags, np.int64), np.asarray(ring_poly, np.int64),
                                 np.asarray(ring_role, np.int64), np.asarray(pt_off, np.int64), p, build_id(md))


def polygons_for(md, cells, frame):
    """{key: (value, layer, flags, [(role, (k, 2) points in frame)])} of the map cells' areas (each polygon once), or
    None: off, none in use, or a tile not had (revisit.fault)."""
    sh = shards(md)
    if sh is None:
        return None
    got = sh.usable()
    if not got:
        return None
    built, m = got
    import mapcells
    z = m["zoom"]
    fid = (round(frame.lat0, 9), round(frame.lon0, 9)) if hasattr(frame, "lat0") else id(frame)
    polys = {}
    for c in sorted({(k.x >> max(0, k.z - z), k.y >> max(0, k.z - z)) for k in cells}):
        mk = (built, c, fid)
        with _LOCK:
            rows = _MEM.get(mk)
            if rows is not None:
                _MEM.move_to_end(mk)
        if rows is None:
            try:
                t = sh.tile(mapcells.CellKey(z, c[0], c[1]))
            except osmtagshards.NotInUse:
                return None
            except Exception as e:      # (offline, limits, the host failing, a damaged tile)
                log.debug("%s tile %d/%d/%d: %s: %s", sh.LABEL, z, c[0], c[1], type(e).__name__, e)
                if not isinstance(e, netfetch.Deferred):
                    sh.down(e)
                import revisit
                revisit.fault()
                return None
            rows = _project(t, frame)
            with _LOCK:
                _MEM[mk] = rows
                while len(_MEM) > MEM_CELLS:
                    _MEM.popitem(last=False)
        for key, rec in rows:
            polys.setdefault(key, rec)
    return polys


def _project(t, frame):
    """[(key, (value, layer, flags, [(role, (k, 2) frame metres)]))] of a decoded tile (None: none)."""
    if t is None:
        return []
    keys, values, _names, layer, flags, _lo, _links, ring_poly, ring_role, pt_off, pts = t
    if not len(keys):
        return []
    e, n, _u = frame.geodetic_to_enu(np.asarray(pts[:, 1]), np.asarray(pts[:, 0]), 0.0)
    xy = np.column_stack((e, n))
    by = {}
    for r in range(len(ring_poly)):
        by.setdefault(int(ring_poly[r]), []).append((int(ring_role[r]), xy[int(pt_off[r]):int(pt_off[r + 1])]))
    out = []
    for i, k in enumerate(keys.tolist()):
        out.append((int(k), (str(values[i]), int(layer[i]), int(flags[i]), by.get(i, []))))
    return out
