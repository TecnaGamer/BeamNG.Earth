"""Two static raster packs on the project's tile host for the winter looks (W2 white ground, W4 bare trees), built by
the data repo's winterpacks workflow (BeamNG.Earth-data tools/build_leaftype_pack.py, tools/build_water_pack.py):

  leaftype    ESA CCI Land Cover 2015 (300 m) condensed to 0.05 degrees: per cell the evergreen share of the trees
              (broadleaved + needleleaved evergreen over all tree cover, by the classes' plant functional types; a cell
              with few trees leans to its region's mix) and the tree cover, each 0..250 (/ 250). One PNG (two grey
              bands, 7200 x 3600 from 180 W, 90 N), ~6 MB, fetched once per pack version.
  watershare  JRC Global Surface Water v1.5 occurrence (30 m, 1984-2024) condensed to 0.004 degrees (~445 m): per
              cell the expected share of water (0..250 / 250; 255 no data: the open sea past the mapped coast). Shards
              of 2 x 2 degrees (<x>/<y>.png, x = floor((lon + 180) / 2), y = floor((90 - lat) / 2), 500 x 500 cells,
              row 0 the north edge), fetched as asked; a shard the manifest calls uniform is not fetched, one it does
              not name is all no data.

Each pack is read only when cdn.json (the data repo's switch, cdn.switch) has {"<pack>": {"enabled": true,
"version": "v1"}}, or BNGE_LEAFTYPE / BNGE_WATERSHARE names a version ("v1") or "off". BNGE_WINTERPACKS_DIR reads a
local build instead (<dir>/leaftype/manifest.json, <dir>/watershare/<x>/<y>.png ...: the builders' output), nothing
fetched. A pack version's files are immutable; the manifest (every file's size and SHA-256, "build": 16 hex of the
content) is decided once per server run for every process (pagesdata.session_take: the first decision wins) and
pinned on disk per version (never fetched again). Every file fetched is checked against its size and SHA-256 before
it is kept (<cache>/pagesdata/winterpacks/<pack>/<version>-<build>/, the managed cache: evicted by LRU, fetched again).

What a consumer keys on: versions() ({"leaftype": "v1-<build>" or None, "watershare": ...}); None is "no pack this run"
(off, not published, or its manifest not had: a failed fetch faults the build reading it, netfetch). The lookups give
NaN where they know nothing (the pack off or not had, no data). Nothing here runs per frame or per texel: the grids
go to the caller's Rust (leaf_grid, water_grid), the point lookups are index arithmetic.

    p = winterpacks.packs(cache_dir, fetcher)
    p.evergreen_share(lat, lon) / p.tree_cover(lat, lon)    float or float64 array, NaN unknown
    p.leaf_grid()                                           LeafGrid(evergreen, trees, res, west, north, scale) or None
    p.water_share(lat, lon)                                 float or float64 array, NaN no data / unknown
    p.water_grid(south, west, north, east)                  WaterGrid(values, north, west, res, scale, nodata) or None
    p.water_share_box(south, west, north, east)             the box's mean share over its cells with data, NaN none
    p.versions()                                            the cache-key part
"""
import hashlib
import io
import json
import logging
import math
import os
import re
import threading
import time
from collections import OrderedDict, namedtuple

import numpy as np

import netfetch
import pagesdata

log = logging.getLogger("earthd.winterpacks")

LEAF, WATER = "leaftype", "watershare"
PACKS = (LEAF, WATER)
LOCAL_ENV = "BNGE_WINTERPACKS_DIR"
ENV = {LEAF: "BNGE_LEAFTYPE", WATER: "BNGE_WATERSHARE"}
VERSION_RE = re.compile(r"v[1-9][0-9]{0,3}")
BUILD_RE = re.compile(r"[0-9a-f]{16}")
HEX64 = re.compile(r"[0-9a-f]{64}")
SHARD_KEY = re.compile(r"(\d{1,3})/(\d{1,2})")
SCALE = 250                 # share = value / SCALE
NODATA = 255
LEAF_W, LEAF_H, LEAF_PER_DEG = 7200, 3600, 20       # 0.05 degrees
WATER_SHARD, WATER_DEG, WATER_PER_DEG = 500, 2, 250  # 0.004 degrees, 2-degree shards
MAX_MANIFEST = 4 << 20
MAX_FILE = 64 << 20
RETRY_S = 300.0             # a file that could not be had: asked again after this long
SHARDS_KEPT = 96            # decoded water shards held in memory (250 kB each)

LeafGrid = namedtuple("LeafGrid", "evergreen trees res west north scale")
WaterGrid = namedtuple("WaterGrid", "values north west res scale nodata")


def local_dir():
    return os.environ.get(LOCAL_ENV) or None


def cdn_base():
    import imagery
    return imagery.CDN_BASE


def check_manifest(m, name, version=None):
    """The manifest m (a dict) when it is a pack manifest of name (and version) this reader understands, else None."""
    if not isinstance(m, dict) or m.get("format") != 1 or m.get("name") != name:
        return None
    if not VERSION_RE.fullmatch(str(m.get("version"))) or (version and m["version"] != version):
        return None
    if not BUILD_RE.fullmatch(str(m.get("build"))):
        return None
    g = m.get("grid")
    if not isinstance(g, dict) or g.get("scale") != SCALE:
        return None
    if name == LEAF:
        f = m.get("files")
        if (g.get("width"), g.get("height"), g.get("bands")) != (LEAF_W, LEAF_H, ["evergreen", "trees"]) or \
                not isinstance(f, list) or len(f) != 1 or not isinstance(f[0], dict) or \
                f[0].get("path") != "leaftype.png" or not _entry_ok(f[0].get("size"), f[0].get("sha256")):
            return None
    else:
        sh, un = m.get("shards"), m.get("uniform")
        if (g.get("cells_per_shard"), g.get("shard_deg"), g.get("nodata")) != (WATER_SHARD, WATER_DEG, NODATA) or \
                not isinstance(sh, dict) or not isinstance(un, dict):
            return None
        for k, v in sh.items():
            if not SHARD_KEY.fullmatch(k) or not isinstance(v, list) or len(v) != 2 or not _entry_ok(v[0], v[1]):
                return None
        for k, v in un.items():
            if not SHARD_KEY.fullmatch(k) or not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= NODATA:
                return None
    return m


def _entry_ok(size, sha):
    return isinstance(size, int) and not isinstance(size, bool) and 0 < size <= MAX_FILE and isinstance(sha, str) \
        and HEX64.fullmatch(sha) is not None


class Pack:
    """One pack's manifest in use (decided once for the server run) and its files (checked, kept on disk). Another
    pack reader of the data repo's (nbiskew.py) subclasses it: ROOT (its folder under pagesdata and its log name),
    env_value (the switch's override) and check (its manifest's own test)."""

    ROOT = "winterpacks"

    def env_value(self):
        """The switch's override (BNGE_LEAFTYPE / BNGE_WATERSHARE), read by its literal name."""
        if self.name == LEAF:
            return os.environ.get("BNGE_LEAFTYPE", "")
        if self.name == WATER:
            return os.environ.get("BNGE_WATERSHARE", "")
        return ""

    def check(self, m, version=None):
        return check_manifest(m, self.name, version)

    def __init__(self, cache_dir, fetcher, name, local=None):
        self.name = name
        self.cache_dir = cache_dir
        self.fetcher = fetcher
        self.local = local
        self.lock = threading.Lock()
        self._decided = False
        self._manifest = None
        self._down = {}             # rel -> monotonic time it could not be had

    # --- the manifest ------------------------------------------------------------------------------------------------
    def _wanted_version(self):
        """The version the switch (or BNGE_LEAFTYPE / BNGE_WATERSHARE) turns on, else None."""
        env = self.env_value().strip().lower()
        if env == "off":
            return None
        if VERSION_RE.fullmatch(env):
            return env
        if self.fetcher is None or not self.cache_dir:
            return None
        import cdn
        on = cdn.switch(self.fetcher, self.cache_dir).get(self.name)
        v = str(on.get("version") or "") if isinstance(on, dict) and on.get("enabled") else ""
        return v if VERSION_RE.fullmatch(v) else None

    def _pin_dir(self, version):
        return os.path.join(self.cache_dir, "pagesdata", self.ROOT, self.name, version) if self.cache_dir else None

    def _decide(self):
        """The manifest's text for this run (None: no pack this run): the pin of the version the switch names, else
        fetched from the tile host and pinned."""
        v = self._wanted_version()
        if v is None:
            return None
        d = self._pin_dir(v)
        if d:
            try:
                with open(os.path.join(d, "manifest.json"), encoding="utf-8") as fh:
                    text = fh.read(MAX_MANIFEST + 1)
                if self.check(json.loads(text), v) is not None:
                    return text
            except (OSError, ValueError):
                pass
        url = f"{cdn_base()}/{self.name}/{v}/manifest.json"
        try:
            body = self.fetcher.get(url, kind="demand", max_bytes=MAX_MANIFEST).body
            text = body.decode("utf-8")
            if self.check(json.loads(text), v) is None:
                raise ValueError("not a manifest of this pack and version")
        except Exception as e:      # (a FetchError faulted the asking build already: what it made is not kept)
            log.info("%s %s %s: no manifest this run (%s: %s)", self.ROOT, self.name, v, type(e).__name__, e)
            return None
        if d:
            os.makedirs(d, exist_ok=True)
            pagesdata.write_atomic(os.path.join(d, "manifest.json"), body)
        log.info("%s %s %s: build %s", self.ROOT, self.name, v, json.loads(text)["build"])
        return text

    def manifest(self):
        """The manifest in use (a dict), or None (no pack this run)."""
        with self.lock:
            if self._decided:
                return self._manifest
        if self.local:
            try:
                with open(os.path.join(self.local, self.name, "manifest.json"), encoding="utf-8") as fh:
                    text = fh.read(MAX_MANIFEST + 1)
            except OSError:
                text = None
        else:
            ident = f"{self.ROOT}:{self.name}|{cdn_base()}/{self.name}"
            text, _ = pagesdata.session_take(self.cache_dir, ident, self._decide, self.name)
        m = None
        if text is not None:
            try:
                m = self.check(json.loads(text))
            except ValueError:
                m = None
        with self.lock:
            if not self._decided:
                self._manifest, self._decided = m, True
            return self._manifest

    def version(self):
        """"<version>-<build>" of the manifest in use (the cache-key part), or None."""
        m = self.manifest()
        return f"{m['version']}-{m['build']}" if m else None

    # --- the files -----------------------------------------------------------------------------------------------------
    def file(self, rel, size, sha):
        """rel's bytes (a file the manifest names with size and sha), from the disk or the tile host, checked; None
        when not had now (asked again after RETRY_S)."""
        m = self.manifest()
        if m is None:
            return None
        if self.local:
            try:
                with open(os.path.join(self.local, self.name, *rel.split("/")), "rb") as fh:
                    return fh.read()
            except OSError:
                return None
        d = os.path.join(self.cache_dir, "pagesdata", self.ROOT, self.name, self.version()) \
            if self.cache_dir else None
        path = os.path.join(d, *rel.split("/")) if d else None
        if path:
            try:
                if os.path.getsize(path) == size:
                    with open(path, "rb") as fh:
                        body = fh.read()
                    if hashlib.sha256(body).hexdigest() == sha:    # (a damaged copy is fetched again)
                        return body
            except OSError:
                pass
        with self.lock:
            if time.monotonic() - self._down.get(rel, -1e18) < RETRY_S:
                return None
        if self.fetcher is None:
            return None
        url = f"{cdn_base()}/{self.name}/{m['version']}/{rel}"
        try:
            body = self.fetcher.get(url, kind="demand", max_bytes=size).body
            if len(body) != size or hashlib.sha256(body).hexdigest() != sha:
                raise ValueError(f"{len(body)} bytes, not the manifest's {size} bytes and SHA-256")
        except Exception as e:      # (a FetchError faulted the asking build; a wrong body is a fault too)
            if not isinstance(e, netfetch.FetchError):
                import revisit
                revisit.fault()
            log.info("%s %s %s: %s: %s", self.ROOT, self.name, rel, type(e).__name__, e)
            with self.lock:
                self._down[rel] = time.monotonic()
            return None
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            pagesdata.write_atomic(path, body)
        return body


def _decode_png(body, mode, w, h):
    from PIL import Image
    with Image.open(io.BytesIO(body)) as im:
        if im.format != "PNG" or im.mode != mode or im.size != (w, h):
            raise ValueError(f"{im.format} {im.mode} {im.size}, not PNG {mode} {(w, h)}")
        return np.asarray(im, np.uint8)


def _points(lat, lon):
    lat, lon = np.broadcast_arrays(np.asarray(lat, np.float64), np.asarray(lon, np.float64))
    return lat, lon


def _out(a):
    return a if a.ndim else float(a[()])


class Packs:
    """Both packs of one cache folder (packs(): one per process and folder)."""

    def __init__(self, cache_dir, fetcher, local=None):
        local = local or local_dir()
        self.leaf = Pack(cache_dir, fetcher, LEAF, local)
        self.water = Pack(cache_dir, fetcher, WATER, local)
        self.lock = threading.Lock()
        self._leaf = None           # LeafGrid, or False: not had (asked again after RETRY_S)
        self._leaf_at = -1e18
        self._shards = OrderedDict()    # "x/y" -> (500, 500) uint8

    def versions(self):
        """{pack: "<version>-<build>" or None}: what anything built from the packs keys on."""
        return {LEAF: self.leaf.version(), WATER: self.water.version()}

    # --- leaf type ---------------------------------------------------------------------------------------------------
    def leaf_grid(self):
        """LeafGrid (evergreen and trees: (3600, 7200) uint8, value / scale; res degrees from west, north), or None."""
        with self.lock:
            if self._leaf:
                return self._leaf
            if self._leaf is False and time.monotonic() - self._leaf_at < RETRY_S:
                return None
        m = self.leaf.manifest()
        got = None
        if m is not None:
            f = m["files"][0]
            body = self.leaf.file(f["path"], f["size"], f["sha256"])
            if body is not None:
                try:
                    a = _decode_png(body, "LA", LEAF_W, LEAF_H)
                    got = LeafGrid(np.ascontiguousarray(a[:, :, 0]), np.ascontiguousarray(a[:, :, 1]),
                                   1.0 / LEAF_PER_DEG, -180.0, 90.0, SCALE)
                except Exception as e:
                    log.warning("winterpacks leaftype: %s: %s", type(e).__name__, e)
        with self.lock:
            self._leaf = got or False
            self._leaf_at = time.monotonic()
            return got

    def _leaf_values(self, band, lat, lon):
        lat, lon = _points(lat, lon)
        g = self.leaf_grid()
        if g is None:
            return _out(np.full(lat.shape, np.nan))
        j = np.clip(np.floor((90.0 - lat) * LEAF_PER_DEG), 0, LEAF_H - 1).astype(np.int64)
        i = np.floor(np.mod(lon + 180.0, 360.0) * LEAF_PER_DEG).astype(np.int64) % LEAF_W
        return _out(getattr(g, band)[j, i] / float(SCALE))

    def evergreen_share(self, lat, lon):
        """The evergreen share (0..1) of the trees at each (lat, lon) (the 0.05 degree cell's; a cell with few trees
        its region's; 0 where the cell has no land), NaN with no pack."""
        return self._leaf_values("evergreen", lat, lon)

    def tree_cover(self, lat, lon):
        """The tree cover (0..1 of the area) of the 0.05 degree cell at each (lat, lon), NaN with no pack."""
        return self._leaf_values("trees", lat, lon)

    # --- water share -------------------------------------------------------------------------------------------------
    def shard(self, x, y):
        """Water shard (x, y) as (500, 500) uint8 (all NODATA where none is published or it is not had now), or None
        with no pack."""
        m = self.water.manifest()
        if m is None:
            return None
        k = f"{x}/{y}"
        with self.lock:
            a = self._shards.get(k)
            if a is not None:
                self._shards.move_to_end(k)
                return a
        u = m["uniform"].get(k)
        e = m["shards"].get(k)
        keep = True
        if e is None:
            a = np.full((WATER_SHARD, WATER_SHARD), NODATA if u is None else u, np.uint8)
        else:
            body = self.water.file(f"{k}.png", e[0], e[1])
            a = None
            if body is not None:
                try:
                    a = _decode_png(body, "L", WATER_SHARD, WATER_SHARD)
                except Exception as e2:
                    log.warning("winterpacks watershare %s: %s: %s", k, type(e2).__name__, e2)
            if a is None:
                a = np.full((WATER_SHARD, WATER_SHARD), NODATA, np.uint8)
                keep = False        # (not had now: asked again later, Pack.file's RETRY_S)
        a.setflags(write=False)
        if keep:
            with self.lock:
                self._shards[k] = a
                while len(self._shards) > SHARDS_KEPT:
                    self._shards.popitem(last=False)
        return a

    def water_share(self, lat, lon):
        """The water share (0..1) of the 0.004 degree cell at each (lat, lon), NaN where there is no data (the open
        sea) or no pack."""
        lat, lon = _points(lat, lon)
        out = np.full(lat.shape, np.nan)
        if self.water.manifest() is None:
            return _out(out)
        fj = np.clip(np.floor((90.0 - lat) * WATER_PER_DEG), 0, 90 * WATER_PER_DEG - 1).astype(np.int64)
        fi = np.floor(np.mod(lon + 180.0, 360.0) * WATER_PER_DEG).astype(np.int64) % (360 * WATER_PER_DEG)
        sx, sy = fi // WATER_SHARD, fj // WATER_SHARD
        flat = (sy * 180 + sx).ravel()
        o = out.reshape(-1)
        for s in np.unique(flat):
            a = self.shard(int(s % 180), int(s // 180))
            sel = flat == s
            v = a[(fj.ravel()[sel] % WATER_SHARD), (fi.ravel()[sel] % WATER_SHARD)]
            o[sel] = np.where(v == NODATA, np.nan, v / float(SCALE))
        return _out(out)

    def water_grid(self, south, west, north, east):
        """WaterGrid over the box (whole cells; values (rows, cols) uint8, row 0 at north, column 0 at west, value /
        scale, nodata no data), or None with no pack. The box may not cross the antimeridian (east >= west)."""
        if self.water.manifest() is None:
            return None
        if not (-90.0 <= south <= north <= 90.0 and west <= east and -180.0 <= west and east <= 180.0):
            raise ValueError(f"bad box {(south, west, north, east)}")
        j0 = min(int(math.floor((90.0 - north) * WATER_PER_DEG)), 90 * WATER_PER_DEG - 1)
        j1 = min(int(math.floor((90.0 - south) * WATER_PER_DEG)), 90 * WATER_PER_DEG - 1)
        i0 = min(int(math.floor((west + 180.0) * WATER_PER_DEG)), 360 * WATER_PER_DEG - 1)
        i1 = min(int(math.floor((east + 180.0) * WATER_PER_DEG)), 360 * WATER_PER_DEG - 1)
        out = np.empty((j1 - j0 + 1, i1 - i0 + 1), np.uint8)
        for sy in range(j0 // WATER_SHARD, j1 // WATER_SHARD + 1):
            for sx in range(i0 // WATER_SHARD, i1 // WATER_SHARD + 1):
                a = self.shard(sx, sy)
                r0, r1 = max(j0, sy * WATER_SHARD), min(j1, sy * WATER_SHARD + WATER_SHARD - 1)
                c0, c1 = max(i0, sx * WATER_SHARD), min(i1, sx * WATER_SHARD + WATER_SHARD - 1)
                out[r0 - j0:r1 - j0 + 1, c0 - i0:c1 - i0 + 1] = \
                    a[r0 - sy * WATER_SHARD:r1 - sy * WATER_SHARD + 1, c0 - sx * WATER_SHARD:c1 - sx * WATER_SHARD + 1]
        return WaterGrid(out, 90.0 - j0 / WATER_PER_DEG, i0 / WATER_PER_DEG - 180.0, 1.0 / WATER_PER_DEG, SCALE, NODATA)

    def water_share_box(self, south, west, north, east):
        """The mean water share of the box's cells with data, NaN when none has any (or no pack)."""
        g = self.water_grid(south, west, north, east)
        if g is None:
            return float("nan")
        v = g.values[g.values != NODATA]
        return float(v.sum(dtype=np.int64)) / (v.size * SCALE) if v.size else float("nan")


_PACKS = {}
_PACKS_LOCK = threading.Lock()


def packs(cache_dir, fetcher):
    """The process's Packs of cache_dir (the first fetcher given is kept)."""
    with _PACKS_LOCK:
        p = _PACKS.get(cache_dir)
        if p is None:
            p = _PACKS[cache_dir] = Packs(cache_dir, fetcher)
        return p


def reset():
    """Forget the process's Packs (tests)."""
    with _PACKS_LOCK:
        _PACKS.clear()
