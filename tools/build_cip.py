#!/usr/bin/env python3
"""build_cip.py: a CIP build (server/cip.py: architecture v2's compact per-cell inputs) for a set of z14 cells.

    python3 tools/build_cip.py --cache SRC --out OUT --around 39.2834,-76.5934,2000 [--online]
    python3 tools/build_cip.py --cache SRC --out OUT --cells 14/4705/6244,14/4706/6244
    python3 tools/build_cip.py --cache SRC --out OUT --cached          (every cell the cache holds a map layer of)
    python3 tools/build_cip.py --cache SRC --out OUT --world --shard I/N --shard-parts   (a CI job; see cip.yml)

Each cell's sections are made by the server's own code, in this process, with CIP off (cip.py lists them):
  m:<layer>   MapData's cell file arrays of the layer: the cache's file when it holds one (what the server reads), else
              MapData._build from the sources (Overture through the build's map source; the roads without OSM's tags,
              joined by the reader at the decode from its run's build: mapdata roads 10) and
              stored there as the server stores it. A parquet stand-in (the tiles failed) is never put in a build.
  t:rail t:lanes   railtags.cell_slice of the weekly shards for the cell's roads' way ids (decisions.ids_of)
  z:<src>     the roadz answer files the cache holds for the cell (--online: measured first, for the cells that want
              a lidar: roadz.RoadZ.lines, the 3DEP reads moved here from the players)
  f:osmfuel f:picnic   the per-cell rows of OpenStreetMap's fuel stations / picnic tables the cache holds (--online:
              read first)
  q           the cell's data-quality record (server/quality.py: its data part, made from the 3 x 3 cells' layers by
              the server's own kernel; a neighbour outside the build is read as a cell is); left out when a 3 x 3
              cell's layers could not be had (the server makes it then), and with --quality off
SRC is a cache of the builder's own (never the player's: pcommon.check_cache refuses it); --online lets the sources be
fetched into it. Every cell is decoded again and compared with what went in before it is kept (--no-verify skips).

Written (the BNWT container: tools/build_bnwt.PartWriter, parts of at most --part-mb):
    OUT/cip<FORMAT>/<release>/<build>/cells.index.json     format, release, build, inputs {code, data}, coverage, parts
    OUT/cip<FORMAT>/<release>/<build>/cells.pNNN.pmtiles   the parts (PMTiles v3, Hilbert-clustered, same cells once)
    OUT/cip<FORMAT>/<release>/<build>/airports.csv.gz, runways.csv.gz   the OurAirports snapshot (sha256 in the index)
    OUT/cip<FORMAT>/<release>/<build>/stats.json           per section: cells, raw and zstd bytes (not served)
    OUT/cip<FORMAT>/<release>/latest.json                  {"build": <build>} (the readers' default)
    OUT/cip<FORMAT>/<release>/<build>/ATTRIBUTION.txt
<build> is the hash of the inputs (cip.build_id): the code-made part (formats, versions, the release, the map source)
and the weekly data builds the cells were made from (OSM road tags, rail, lanes, stations), with the build's pin set
(inputs.data.pins: every weekly dataset's manifest, by its build, data folder and SHA-256). Deterministic: the same
inputs give the same bytes (cells in tile-id order, zstd at a fixed level, JSON with sorted keys, no timestamps).

The pin set (2026-11-24: planet run 45290114494's 333 shards refused, exit 3, once the weekly rail.yml run published
new rail / lane / OSM tag builds while they ran): every weekly dataset (pagesdata's: rail, lanes, stations, signs,
FRA, OSM tags, years, colours, areas, tall buildings, regional) is decided ONCE, when the build is planned
(--write-pins FILE: the manifests as served then, written with the build id), and every shard given that file (--pins
FILE) reads exactly those builds (a pinned pagesdata session: pagesdata.seed_session; a dataset it does not name is no
data) from their build-addressed folders (data/<name>/<key>/ on the tile host, <name>/<key>/ parts: never
overwritten), whatever Pages serves meanwhile; a shard checks first that the pinned builds it reads are there (exit 4,
"pinned build unavailable", not a retry of the same thing). A build days later, a rerun, reads the same file (cip.yml:
the bucket's cip<FORMAT>/<release>/<build>/pins.json) and resumes the same build id. Without --pins the run decides the
pin set itself at its start (one session for its worker processes too). Exit 3 (--expect-build) now only means other
code or other switches than the build's.
"""
import argparse
import gzip
import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "portcheck"))
os.environ["BNE_V2_CIP"] = "off"            # (the builder reads the sources: never a build)

try:
    import npyfirst  # noqa: E402,F401
except ImportError:
    pass
import numpy as np  # noqa: E402

import cip  # noqa: E402
import quality  # noqa: E402
import fast  # noqa: E402
import mapcells  # noqa: E402

N = 1 << cip.ZOOM
VECTOR = ("roads", "connectors", "buildings", "water", "land_use", "infrastructure", "building_parts", "places",
          "road_routes", "town_areas", "town_points", "hoods", "land", "bathymetry")
RASTER = ("landcover", "canopy")
DEFAULT_LAYERS = VECTOR + RASTER
# --scope: which cells of a z10 unit a world build holds. "all": every one (open sea and empty land too: ~229 M of the
# planet's 268 M cells hold only the sea or a repeat of a neighbour's content, at ~10-40 ms a cell); "content": the
# cells with a BNWT tile of the transportation, buildings or places archive (roads, buildings, places: ~16-20 M, where
# a player drives); "roads": the cells with roads (~15 M). A cell left out is read from its sources by the players, as
# every cell is without CIP (cip.Reader: a cell of the coverage with no tile).
# --cap-kb: a division layer's section of a cell bigger than this (its arrays' bytes) is left out of the cell and read
# from its source (cip.Reader: per section). The division areas are whole outlines, not cut to the cell (overture.LAYERS
# clip=False): a Quebec or Yukon region's outline (290-960 kB of arrays) was in each of its thousands of cells, 51 of a
# rural Quebec unit's 53 MB, most of a planet build; Manhattan's are 33 kB (median) to 74 kB.
CAPPED = ("town_areas", "hoods")
CAP_KB = 128
SCOPES = {"all": None, "content": ("transportation", "buildings", "places"), "roads": ("transportation",)}
# the planet build's: the rasters stay on their sources (WorldCover / the canopy model: ~2 / ~100-200 kB a land cell, a
# planet of ~109 M land cells would be ~0.2 / several TB for ~1-4 MB of a cold visit's downloads)
WORLD_LAYERS = VECTOR


# --- the pin set: every weekly dataset's build, decided once for a build (see the docstring) --------------------------
PINS_FORMAT = 1
# inputs.data's parts that name weekly data builds: plan_key leaves them out (a build's code and switches alone)
PIN_KEYS = ("rails", "lanes", "osmtags", "stations", "pins")
EXIT_UNAVAILABLE = 4


class PinsUnavailable(Exception):
    """A pinned build the build reads cannot be had (its folder gone, the host failing, or not build-addressed)."""


def load_pins(path):
    """The pin set document of file path ({"format", "datasets": {ident: manifest text or None}, "build", ...}),
    checked; raises SystemExit when it is not one."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict) or doc.get("format") != PINS_FORMAT or not isinstance(doc.get("datasets"), dict):
            raise ValueError("not a pin set of this format")
        for ident, body in doc["datasets"].items():
            if not isinstance(ident, str) or "|" not in ident:
                raise ValueError(f"dataset {ident!r}")
            if body is not None and not isinstance(json.loads(body), dict):
                raise ValueError(f"{ident}: its manifest is not an object")
        pin_summary(doc["datasets"])            # (two datasets of one name: refused here)
        return doc
    except (OSError, ValueError, TypeError) as e:
        raise SystemExit(f"--pins {path}: {type(e).__name__}: {e}")


def pin_summary(datasets):
    """inputs.data["pins"]: {dataset name: {"built", "key", "data", "sha256"} of its pinned manifest, or None (no data
    this build)}. The SHA-256 is the manifest text's: the build id is a function of the whole pin set."""
    import pagesdata
    out = {}
    for ident in sorted(datasets):
        body = datasets[ident]
        name = ident.split("|", 1)[0]
        if name in out:
            raise ValueError(f"two pinned datasets named {name}")
        if body is None:
            out[name] = None
            continue
        m = json.loads(body)
        out[name] = {"built": m.get("built"), "key": m.get("key"), "data": pagesdata.data_url(m),
                     "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()}
    return out


def plan_key(inputs):
    """The build's code and switches without its weekly data (PIN_KEYS): equal for every build of the same code and
    settings (cip.yml's pointer to the build in progress: cip<FORMAT>/<release>/open/<plan key>.json)."""
    return cip.build_id({"code": inputs["code"],
                         "data": {k: v for k, v in inputs["data"].items() if k not in PIN_KEYS}})


def collect_pins(b, strict):
    """The run's pin set: every weekly dataset decided in the run's session (after MapData.freeze_manifests).
    strict (a build's plan, --write-pins): a dataset decided "none" must be one not published (its manifest a 404 now),
    never one that failed or answered late (a whole planet build without it)."""
    import netfetch
    import pagesdata
    got = pagesdata.session_entries(b.cache)
    if strict:
        for ident, body in sorted(got.items()):
            if body is not None:
                continue
            base = ident.split("|", 1)[1]
            try:
                b.md.fetcher.get(f"{base}/manifest.json", kind="demand")
            except netfetch.NotFound:
                continue
            except Exception as e:
                raise PinsUnavailable(f"{ident}: its manifest could not be had ({type(e).__name__}: {e}): plan again")
            raise PinsUnavailable(f"{ident}: its manifest came only after the plan's wait: plan again")
    return got


def read_sources(b):
    """[(name, pagesdata.Manifest)] of the weekly datasets whose files the build's cells are made from (data_builds':
    rail, lanes, OSM road tags)."""
    md = b.md
    out = []
    for name, t in (("rails", getattr(md, "rail_tags", None)), ("lanes", getattr(md, "lane_tags", None))):
        if t is not None:
            out.append((name, t.manifest))
    try:
        sh = md._osm_shards()
    except Exception:
        sh = None
    if sh is not None:
        out.append(("osmtags", sh.pages.manifest))
    return out


def check_pins(b, pinned):
    """A pinned build (--pins) reads the builds it pinned: each weekly dataset the cells are made from is in the pin set
    (by its ident: the same BNE_DATA_PAGES as the plan's) and its pinned build's files are there: its data folder's
    manifest names the build, each archive part (absolute: the build's own folder) answers, or (no data folder) Pages
    still serves that build. Raises PinsUnavailable."""
    import pagesdata
    f = b.md.fetcher
    for name, mf in read_sources(b):
        ident = mf.ident()
        if pinned is not None and ident not in pinned:
            raise PinsUnavailable(f"{name}: {ident} is not in the pin set (pinned: {sorted(pinned)}): run with the "
                                  "plan's BNE_DATA_PAGES / *_BASE")
        m = mf.current()
        if m is None:
            continue                    # (no data this build: pinned so)
        built = pagesdata.build_of(m)
        try:
            parts = m.get("parts")
            archives = [p.get("archive") if isinstance(p, dict) else None for p in parts] \
                if isinstance(parts, list) and parts else ([m["archive"]] if m.get("archive") else [])
            if archives:                # (an archive's parts: osmtags' <name>/<build>/ folders)
                for a in archives:
                    if not isinstance(a, str) or not a.startswith("https://"):
                        raise PinsUnavailable(f"{name} {built}: part {a!r} is not in a folder of its build's own")
                    r = f.get(a, kind="demand", headers={"Range": "bytes=0-6"})
                    if not r.body.startswith(b"PMTiles"):
                        raise PinsUnavailable(f"{name} {built}: {a} is not its PMTiles part")
            elif pagesdata.data_url(m):
                d = pagesdata.data_url(m)
                got = json.loads(f.get(f"{d}/manifest.json", kind="demand").body.decode("utf-8"))
                if pagesdata.build_of(got) != built:
                    raise PinsUnavailable(f"{name}: {d} holds build {pagesdata.build_of(got)!r}, not {built!r}")
            else:
                got = json.loads(f.get(f"{mf.base}/manifest.json", kind="demand").body.decode("utf-8"))
                if pagesdata.build_of(got) != built:
                    raise PinsUnavailable(f"{name}: Pages serves build {pagesdata.build_of(got)!r} now and the pinned "
                                          f"{built!r} names no data folder of its own")
        except PinsUnavailable:
            raise
        except Exception as e:
            raise PinsUnavailable(f"{name} {built}: {type(e).__name__}: {e}")


def cells_around(lat, lon, r):
    import geodesy  # noqa: F401
    dlat = r / 111320.0
    dlon = r / (111320.0 * max(0.05, np.cos(np.radians(lat))))
    return mapcells.cells_for_bbox(lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def cached_cells(cache):
    """Every z14 cell the cache holds a map layer file of."""
    out = set()
    base = os.path.join(cache, "mapdata", "v2")
    for d, _dirs, files in os.walk(base):
        rel = os.path.relpath(d, base).split(os.sep)
        if len(rel) == 5 and rel[3] == "14":
            for f in files:
                if f.endswith(".npz"):
                    out.add(mapcells.CellKey(14, int(rel[4]), int(f[:-4])))
    return sorted(out, key=lambda k: (k.x, k.y))


class Builder:
    def __init__(self, cache, online, layers, release=None):
        # (the server's road processes' own setup: blockworker.init, as tools/portcheck/build.py; the same switches,
        # sources and map data as the players' processes have)
        import logging
        if release:
            os.environ["BNGE_OVERTURE_RELEASE"] = release
        import npyguard
        npyguard.apply()
        import blockworker as bw
        import earthd
        earthd.raise_open_files()
        self.cache, self.online, self.layers = cache, online, layers
        bw.init(cache, not online, True, True, True, True, None, logging.WARNING, True, True, 0, None, True)
        import mapdata
        # (no uncompressed mirror of the layer files: a server's shared read cache; the builder reads each layer once,
        # and pool workers never prune it: 2.1 GB beside a Manhattan unit's 78 MB of cells, ~50-80 GB a CI shard)
        mapdata.MIRROR_MB = 0
        self.md = bw._s.md
        self.store = True           # the layer files kept in the cache (False: --world, each cell's layers built once)
        self.release = self.md.release
        self.stats = {}
        self.partial = True
        self.lidar = "have"
        self.only = None            # {cell: layers} (--cellset), None: self.layers for every cell
        self.lidar_cells = None     # the cells whose lidar is measured (--cellset's "lidar"), None: every one wanting it
        self.cap = 0                # bytes: CAPPED sections bigger than this left out (0: none)
        self.quality = True         # the "q" section (q_ready)

    # --- data builds ---------------------------------------------------------------------------------------------
    def data_builds(self):
        md = self.md
        out = {}
        for name, t in (("rails", getattr(md, "rail_tags", None)), ("lanes", getattr(md, "lane_tags", None))):
            # (online: the build in use asked of the network; known() alone is None in a fresh cache, and the planet
            # trial pinned no rail / lane build and left out every cell's t:rail / t:lanes)
            out[name] = (t.version() if self.online else t.known()) if t is not None else None
        try:
            # (the OSM road tags' build the "q" records are counted with: the roads' sections hold none, roads 10)
            got = md.osm_tags_build(network=self.online)
            out["osmtags"] = got if got not in ("layercake", "none") else None
        except Exception:
            out["osmtags"] = None
        return out

    # --- the quality record ------------------------------------------------------------------------------------------
    def q_ready(self):
        """Does this build make the "q" section (every layer its counts read, for every cell)?"""
        return self.quality and self.only is None and all(ly in self.layers for ly in quality.LAYERS)

    def q_counts(self, key, secs=None, lacks=()):
        """Cell key's quality counts (quality.counts) from its own sections (secs, lacks: cell()'s) or, for a cell
        not built here (a neighbour), from its layers read as cell() reads them; None when a layer or the lane slice
        could not be had (the cell's neighbours get no "q" then; the server makes theirs)."""
        md = self.md
        layers = {}
        try:
            if secs is not None:
                if any(s == "t:lanes" or s[2:].split(".")[0] in quality.LAYERS for s, _w in lacks):
                    return None
                for ly in quality.LAYERS:
                    layers[ly] = secs[f"m:{md.layer_name(ly)}"]
                layers["roads"], ok = md.roads_joined_arrays(key, layers["roads"])   # (as the server counts them)
                if not ok:
                    return None
                lanes = secs.get("t:lanes")
                t = getattr(md, "lane_tags", None)
                if lanes is None and t is not None and t.known() is not None:
                    return None
            else:
                for ly in quality.LAYERS:
                    layers[ly] = self.layer(key, ly)[1]
                layers["roads"], ok = md.roads_joined_arrays(key, layers["roads"])
                if not ok:
                    return None
                lanes = quality.lanes_slice(md, key, layers["roads"])
            return quality.counts(key, layers, lanes)
        except Exception as e:      # (a source failed: no record for the cells next to it)
            print(f"quality {key}: {type(e).__name__}: {e}", file=sys.stderr)
            return None

    # --- one cell --------------------------------------------------------------------------------------------------
    def layer(self, key, layer):
        import mapdata
        md = self.md
        name = md.layer_name(layer)
        path = md.path(key, layer)
        arrays = md._load(path)
        if arrays is None and not self.store:
            # (the arrays layer_arrays would store and the server read back: the same dict, without the file write,
            # its deflate and the read back; a stand-in is never built)
            arrays = md._build(key, layer, "demand")
            if arrays.pop("_fallback", None) is not None:
                raise RuntimeError(f"{key} {layer}: a parquet stand-in (the tiles failed): not built")
        if arrays is None:
            got, _hit = md.layer_arrays(key, layer, "demand")
            if isinstance(got, mapdata.StandIn):
                raise RuntimeError(f"{key} {layer}: a parquet stand-in (the tiles failed): not built")
            arrays = md._load(path)
            if arrays is None:
                raise RuntimeError(f"{key} {layer}: no cell file after its build")
        return name, arrays

    def cell(self, key):
        """{section: payload} of cell key, and [(section, why)] of what it lacks."""
        import mapgeom
        import decisions
        import railtags
        md = self.md
        secs, lacks = {}, []
        roads, roads_ok = None, True
        for layer in (self.only.get(key, ()) if self.only is not None else self.layers):
            try:
                name, arrays = self.layer(key, layer)
            except Exception as e:
                lacks.append((f"m:{layer}", f"{type(e).__name__}: {e}"))
                continue
            if self.cap and layer in CAPPED and sum(np.asarray(v).nbytes for v in arrays.values()) > self.cap:
                continue            # (read from its source: a whole outline repeated in each cell it reaches)
            secs[f"m:{name}"] = arrays
            if layer == "roads":
                # (the ways the server's joins look up: its decode's, the OSM road tags joined; the section holds none)
                joined, ok = md.roads_joined_arrays(key, arrays)
                roads = mapgeom.Features.from_arrays("roads", {k: v for k, v in joined.items() if k != "provenance"},
                                                     "")
                if not ok:          # (the slices' way ids not the server's: left out, read from the shards)
                    lacks.append(("t:osmtags", "the OSM road tags of the build could not be had"))
                    roads_ok = False
        if roads is not None and roads_ok:
            ids = decisions.ids_of(roads)
            for sec, t in (("t:rail", getattr(md, "rail_tags", None)), ("t:lanes", getattr(md, "lane_tags", None))):
                if t is None or t.known() is None:
                    continue
                sh = t.of_cell(key)
                if sh is None:
                    lacks.append((sec, "shard unreachable"))
                    continue
                secs[sec] = json.dumps(railtags.cell_slice(sh, key, ids), separators=(",", ":"),
                                       ensure_ascii=False).encode("utf-8")
        import roadz
        rz = roadz.PROVIDER
        if rz is not None and self.lidar != "skip":
            if self.lidar == "measure" and rz.lidar_wanted(key) and roads is not None \
                    and (self.lidar_cells is None or key in self.lidar_cells):
                for src in rz.lidar_sources(key):
                    try:
                        rz.lines(src, key)
                    except Exception as e:
                        lacks.append((f"z:{src}", f"{type(e).__name__}: {e}"))
            for src in cip.ROADZ_SRCS:
                p = os.path.join(rz.dir, src, str(key.z), str(key.x), f"{key.y}.npz") if rz.dir else None
                if p and os.path.exists(p):
                    with np.load(p, allow_pickle=False) as z:
                        secs[f"z:{src}"] = {k: z[k] for k in z.files}
                    if os.path.exists(p + ".gaps.json"):
                        with open(p + ".gaps.json", "rb") as fh:
                            secs[f"z:{src}.gaps"] = fh.read()
        fp = md.osm_fuel_path(key)
        if not os.path.exists(fp) and self.online and roads is not None:
            try:
                md.osm_fuel_fetch(key, "demand")
            except Exception as e:
                lacks.append(("f:osmfuel", f"{type(e).__name__}: {e}"))
        if os.path.exists(fp):
            with open(fp, "rb") as fh:
                secs["f:osmfuel"] = fh.read()
        pdir = os.path.join(self.cache, "mapdata", "raw", "picnicsource")
        pin = os.path.join(pdir, "pinned")
        if self.online and roads is not None:
            try:
                if md.picnic_osm._cell(key, "demand") is None:
                    lacks.append(("f:picnic", "the POI file is unreachable"))
            except Exception as e:
                lacks.append(("f:picnic", f"{type(e).__name__}: {e}"))
        if os.path.exists(pin):
            import re
            with open(pin) as fh:
                etag = fh.read().split("\n")[0]
            pp = os.path.join(pdir, re.sub(r"[^A-Za-z0-9]+", "_", etag).strip("_"), "cells", str(key.z), str(key.x),
                              f"{key.y}.json")
            if os.path.exists(pp):
                with open(pp, "rb") as fh:
                    secs["f:picnic"] = fh.read()
        return secs, lacks

    def encode(self, key, secs, verify=True, stats=True):
        b = cip.encode_cell(key, secs)
        if verify:
            back = cip.decode_cell(key, b)
            if list(back) != sorted(back, key=lambda n: list(back).index(n)) or set(back) != set(secs):
                raise RuntimeError(f"{key}: sections {sorted(back)} vs {sorted(secs)}")
            for name, v in secs.items():
                w = back[name]
                if isinstance(v, (bytes, bytearray)):
                    if bytes(v) != w:
                        raise RuntimeError(f"{key} {name}: blob differs")
                    continue
                if list(v) != list(w):
                    raise RuntimeError(f"{key} {name}: array names differ")
                for k in v:
                    a, c = np.asarray(v[k]), w[k]
                    if a.dtype != c.dtype or a.shape != c.shape or a.tobytes() != c.tobytes():
                        raise RuntimeError(f"{key} {name} {k}: {a.dtype}{a.shape} vs {c.dtype}{c.shape} or bytes")
        return b


def zsize(b, level=19):
    try:
        from compression import zstd
        return len(zstd.compress(b, level))
    except ImportError:
        import zstandard
        return len(zstandard.ZstdCompressor(level=level).compress(b))


def section_stats(key, secs, cellbytes, stats):
    """Per section: raw codec bytes (the cell file's directory) and the section's own zstd-19 size (alone: an upper
    bound of its share of the one frame)."""
    info = cip.cell_sections(cellbytes)
    for name, _codec, raw in info:
        base = name.split(".")[0] if name.startswith("m:") else name
        s = stats.setdefault(base, dict(cells=0, raw=0, alone=0))
        s["cells"] += 1
        s["raw"] += int(raw)
        one = cip.encode_cell(key, {name: secs[name]})
        s["alone"] += len(one)


def quality_cells(b, keys, made):
    """(key, secs, lacks, why) of every cell of keys, made(key) -> (secs, lacks), in rows from the north: secs with
    the "q" section when the build makes it (Builder.q_ready) and the 3 x 3 cells' counts could all be had (a cell of
    keys counted from its own sections, a neighbour outside keys from its layers), else why (None: made or not made
    by this build). Two rows of cells are held back at most (their neighbours below come first)."""
    on = b.q_ready()
    keys = sorted(set(keys), key=lambda k: (k.y, k.x))
    counts = {}
    pending = []

    def finish(item):
        key, secs, lacks = item
        if not on:
            return key, secs, lacks, None
        nine = []
        for nk in quality.ring(key):
            if nk is None:
                nine.append(None)
                continue
            if nk not in counts:
                counts[nk] = b.q_counts(nk)
            if counts[nk] is None:
                return key, secs, lacks, f"{nk}: its layers or lane slice could not be had"
            nine.append(counts[nk])
        return key, dict(secs, **{quality.SECTION: quality.record(key, nine)}), lacks, None

    for key in keys:
        while pending and pending[0][0].y + 1 < key.y:
            yield finish(pending.pop(0))
        secs, lacks = made(key)
        if on:
            counts[key] = b.q_counts(key, secs, lacks)
        pending.append((key, secs, lacks))
        lo = key.y - 3                  # (counts of rows no pending cell reads any more)
        if len(counts) > 4096:
            for k in [k for k in counts if k.y < lo]:
                del counts[k]
    while pending:
        yield finish(pending.pop(0))


def ring_of(keys):
    """The cells next to keys (8-neighbourhood) that are not in keys."""
    ks = set(keys)
    out = set()
    for k in ks:
        for nk in quality.ring(k):
            if nk is not None and nk not in ks:
                out.add(nk)
    return sorted(out, key=lambda k: (k.x, k.y))


def rects_of(cells):
    """Coverage rectangles (x0, y0, x1, y1) of a set of cells: runs of x per row, merged down equal runs."""
    rows = {}
    for k in cells:
        rows.setdefault(k.y, []).append(k.x)
    runs = []
    for y in sorted(rows):
        xs = sorted(rows[y])
        a = b = xs[0]
        for x in xs[1:]:
            if x == b + 1:
                b = x
            else:
                runs.append((a, y, b))
                a = b = x
        runs.append((a, y, b))
    out = []
    for a, y, b in runs:
        for r in out:
            if r[0] == a and r[2] == b and r[3] == y - 1:
                r[3] = y
                break
        else:
            out.append([a, y, b, y])
    return [tuple(r) for r in out]


# --- world builds (a CI matrix: tools/build_bnwt's units, shards, parts and records) ---------------------------------------
_B = {}


def _unit_init(cache, online, layers, release, lidar, partial, data=None, cap=0, q=True):
    _B["b"] = b = Builder(cache, online, layers, release)
    b.lidar, b.partial, b.store, b.cap, b.quality = lidar, partial, False, cap, q
    _B["data"] = data
    same_data(b)


def same_data(b):
    """The weekly data builds still the build's (a weekly build landed meanwhile: never two in one build; the shard
    fails and the next run of the same build id makes it again)."""
    want = _B.get("data")
    if want is not None:
        got = b.data_builds()
        if got != want:
            raise RuntimeError(f"the weekly data builds changed under the build: {got} vs {want}")


def unit_ahead(b, keys, ring=()):
    """What a unit's cells read from the network, fetched at once: the BNWT tiles of its layers (one range read per
    archive instead of one per 4 x 4 block) and the OSM fuel / picnic rows of all its cells (one scan each instead of
    256: ~0.2 s of every cell, an empty one too). A failure here is not one: each cell reads its own then."""
    md = b.md
    if not b.online:
        return
    bw = getattr(md, "bnwt", None)
    if bw is not None:
        try:
            bw.prefetch([(k, ly) for k in keys for ly in b.layers]
                        + [(k, ly) for k in ring for ly in quality.LAYERS if ly in b.layers], "demand")
        except Exception as e:
            print(f"unit prefetch: BNWT: {type(e).__name__}: {e}", file=sys.stderr)
    try:
        md.osm_fuel_fetch_many(keys, "demand")
    except Exception as e:
        print(f"unit prefetch: OSM fuel: {type(e).__name__}: {e}", file=sys.stderr)
    try:
        md.picnic_osm.store_many(keys, "demand")
    except Exception as e:
        print(f"unit prefetch: picnic: {type(e).__name__}: {e}", file=sys.stderr)


def in_scope(b, names, x, y):
    """Has cell (x, y) a tile in one of the BNWT archives names (on disk after unit_ahead)? True when that cannot be
    told (no BNWT, a tile not read): the cell is built, as with --scope all."""
    import pmtiles
    bw = getattr(b.md, "bnwt", None)
    if bw is None:
        return True
    for name in names:
        try:
            arch = bw.archive(name).open("demand")
        except Exception:
            return True
        if not arch.covers_xy((cip.ZOOM, x, y)):
            continue
        t = pmtiles._read(arch._tile_path(cip.ZOOM, x, y))
        if t is None or t:
            return True
    return False


def build_unit(args):
    """One z10 unit's part files (every cell of the unit inside the rects, empty ones too: a reader falls back to the
    sources only where the build has no cell). Returns (unit, cells, bytes, lacking, seconds)."""
    import build_bnwt
    unit, rects, parts, verify = args[:4]
    scope = args[4] if len(args) > 4 else "all"
    b = _B["b"]
    t0 = time.perf_counter()
    uz, ux, uy = unit
    sh = cip.ZOOM - uz
    cells = sorted({(fast._ef.pmt_tile_id(cip.ZOOM, x, y), x, y) for x0, y0, x1, y1 in rects
                    for x in range(max(x0, ux << sh), min(x1, ((ux + 1) << sh) - 1) + 1)
                    for y in range(max(y0, uy << sh), min(y1, ((uy + 1) << sh) - 1) + 1)})
    base = os.path.join(parts, f"{uz}_{ux}_{uy}")
    keys = [mapcells.CellKey(cip.ZOOM, x, y) for _tid, x, y in cells]
    unit_ahead(b, keys, ring_of(keys) if b.q_ready() else ())
    if SCOPES[scope] is not None:
        cells = [c for c in cells if in_scope(b, SCOPES[scope], c[1], c[2])]
    ids, lens, ind, lacking = [], [], [], {}
    tid_of = {mapcells.CellKey(cip.ZOOM, x, y): tid for tid, x, y in cells}
    done = {}
    for key, secs, lacks, qwhy in quality_cells(b, list(tid_of), b.cell):
        if lacks and not b.partial:
            raise RuntimeError(f"{key}: lacks {lacks[:3]}")     # (a world build never ships a source's failure)
        for sec, _why in lacks + ([(quality.SECTION, qwhy)] if qwhy else []):
            lacking[sec] = lacking.get(sec, 0) + 1
        done[tid_of[key]] = b.encode(key, secs, verify=verify)
    tmp = f"{base}.bin.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        for tid in sorted(done):        # (the cells in tile-id order, as the parts list them)
            cb = done[tid]
            f.write(cb)
            ids.append(tid)
            lens.append(len(cb))
            ind.append(0)
    same_data(b)
    os.replace(tmp, base + ".bin")
    itmp = f"{base}.idx.{os.getpid()}.tmp"
    with open(itmp, "wb") as f:
        np.save(f, np.array([ids, lens, ind], np.uint64).reshape(3, -1))
    os.replace(itmp, base + ".idx")
    del build_bnwt
    return unit, len(ids), int(sum(lens)), lacking, time.perf_counter() - t0


def write_index(outdir, meta, inputs, rects, plist, cells, contents, cache, airports_dir, layers=None, online=False):
    index = dict(meta, archive=cip.ARCHIVE, inputs=inputs, coverage=[list(r) for r in rects], parts=plist,
                 cells=cells, contents=contents)
    if layers is not None:          # (the map layers the build holds; readers take the others from their sources)
        index["layers"] = sorted(set(layers))
    ap_dir = airports_dir
    if ap_dir is None and online and not os.path.exists(os.path.join(cache, "airports", "ourairports", "current.json")):
        # (the road processes never download OurAirports, the server does: a fresh CI cache had none, and the build's
        # index would have gone out without its snapshot)
        try:
            import airports
            airports.Store(cache).refresh(kind="demand", force=True)
        except Exception as e:
            print(f"airports: no OurAirports snapshot ({type(e).__name__}: {e}): the index has none", file=sys.stderr)
    if ap_dir is None:
        try:
            with open(os.path.join(cache, "airports", "ourairports", "current.json"), encoding="utf-8") as fh:
                ap_dir = os.path.join(cache, "airports", "ourairports", json.load(fh)["gen"])
        except (OSError, ValueError, KeyError):
            ap_dir = None
    if ap_dir and all(os.path.exists(os.path.join(ap_dir, n)) for n in ("airports.csv", "runways.csv")):
        rec = {}
        for n in ("airports.csv", "runways.csv"):
            with open(os.path.join(ap_dir, n), "rb") as fh:
                z = gzip.compress(fh.read(), 9, mtime=0)
            with open(os.path.join(outdir, n + ".gz"), "wb") as fh:
                fh.write(z)
            rec[n + ".gz"] = hashlib.sha256(z).hexdigest()
        index["airports"] = rec
    with open(os.path.join(outdir, f"{cip.ARCHIVE}.index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, sort_keys=True, separators=(",", ":"))
    with open(os.path.join(os.path.dirname(outdir), "latest.json"), "w", encoding="utf-8") as fh:
        json.dump({"build": meta["build"]}, fh, sort_keys=True)
    with open(os.path.join(outdir, "ATTRIBUTION.txt"), "w", encoding="utf-8") as fh:
        fh.write(attribution())
    return index


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", required=True, help="the builder's source cache (never the player's)")
    ap.add_argument("--out", required=True)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--around", help="lat,lon,metres")
    g.add_argument("--cells", help="z/x/y,z/x/y,...")
    g.add_argument("--cached", action="store_true", help="every cell the cache holds a map layer of")
    g.add_argument("--cellset", help='a JSON {"cells": {"14/x/y": [layers]}}: those cells, each with its layers only '
                                     '(a recorded visit\'s footprint)')
    g.add_argument("--world", action="store_true", help="every cell any Overture source file reaches (z10 units)")
    ap.add_argument("--inputs-only", action="store_true", help="print the build's id and inputs, build nothing (with "
                                                               "--world: the planet build's, whose layers differ)")
    ap.add_argument("--expect-build", metavar="ID", help="refuse to build (exit 3) unless the inputs are build ID (the "
                                                         "CI's: other code or switches than the plan's)")
    ap.add_argument("--pins", metavar="FILE", help="the build's pin set (--write-pins' file): every weekly dataset read "
                                                   "as that build, whatever Pages serves now (exit 4: a pinned build "
                                                   "the cells read is not available)")
    ap.add_argument("--write-pins", metavar="FILE", help="write the pin set this run decided (with the build id and "
                                                         "inputs: a plan's, for --pins)")
    ap.add_argument("--online", action="store_true", help="fetch the sources (else the cache alone)")
    ap.add_argument("--lidar", default=None, choices=("measure", "have", "skip"),
                    help="the lidar answers: measured here for the cells that want one (default with --online), the "
                         "cache's (default offline), or none (players read the lidar themselves)")
    ap.add_argument("--layers", default=None, help="comma-separated (default: every map layer; --world: the vector "
                                                   "layers, WORLD_LAYERS)")
    ap.add_argument("--release", default=None, help="the Overture release (default: the server's)")
    ap.add_argument("--part-mb", type=float, default=cip.PART_MB)
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--stats", action="store_true", help="per-section sizes (slower: every section compressed alone)")
    ap.add_argument("--airports", default=None, help="the OurAirports folder to pin (default: the cache's current)")
    ap.add_argument("--partial", action="store_true", help="world: keep a cell a source failed for (its section left "
                                                           "out: readers read that source); default: the shard fails")
    ap.add_argument("--jobs", type=int, default=1, help="world: build processes")
    ap.add_argument("--unit-zoom", type=int, default=10)
    ap.add_argument("--scope", default="all", choices=sorted(SCOPES), help="world: the cells built (SCOPES)")
    ap.add_argument("--cap-kb", type=int, default=None, help=f"a {'/'.join(CAPPED)} section bigger than this is left "
                                                            f"out (CAP_KB; default {CAP_KB} with --world, else none)")
    ap.add_argument("--quality", default="on", choices=("on", "off"), help="the cells' data-quality record (section "
                                                                        "\"q\", server/quality.py)")
    ap.add_argument("--work", help="world: units' parts folder (default OUT/.work)")
    ap.add_argument("--shard", help="world: I/N, the I-th of N contiguous runs of the units (Hilbert order)")
    ap.add_argument("--shard-parts", action="store_true", help="with --shard: the shard's own parts and its record")
    ap.add_argument("--assemble", action="store_true", help="world: the index from the parts (or --from-shards)")
    ap.add_argument("--from-shards", type=int, metavar="N", help="with --assemble: from the N shards' records")
    a = ap.parse_args(argv)
    if not a.inputs_only and not (a.around or a.cells or a.cached or a.cellset or a.world):
        ap.error("one of --around --cells --cached --cellset --world (or --inputs-only) is required")
    import pcommon
    pcommon.check_cache(a.cache)
    fast.require(cip.NATIVE, "build_cip")
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    layers = tuple(x for x in (a.layers or ",".join(WORLD_LAYERS if a.world else DEFAULT_LAYERS)).split(",") if x)
    if a.pins and a.write_pins:
        ap.error("--pins and --write-pins: one or the other")
    import pagesdata
    pins = load_pins(a.pins) if a.pins else None
    # (the run's one decision of every weekly dataset, in every worker process too: the pin set's, else this run's own)
    if pins is not None:
        pagesdata.seed_session(a.cache, pins["datasets"])
    else:
        pagesdata.start_session(a.cache)
    b = Builder(a.cache, a.online, layers, a.release)
    try:
        if pins is None:
            b.md.freeze_manifests()()
            datasets = collect_pins(b, strict=bool(a.write_pins))
        else:
            datasets = pins["datasets"]
        if a.online and (pins is not None or a.write_pins):
            check_pins(b, set(datasets) if pins is not None else None)
    except PinsUnavailable as e:
        print(f"build_cip: pinned build unavailable: {e}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    b.partial = a.partial
    b.lidar = a.lidar or ("measure" if a.online else "have")
    b.cap = 1024 * (a.cap_kb if a.cap_kb is not None else (CAP_KB if a.world else 0))
    b.quality = a.quality == "on"
    if a.cellset:
        with open(a.cellset, encoding="utf-8") as fh:
            built = sorted({ly for v in json.load(fh)["cells"].values() for ly in v})
    else:
        built = sorted(layers)
    inputs = {"code": cip.code_inputs(b.release),
              "data": dict(b.data_builds(), lidar=b.lidar, zstd=cip.LEVEL, layers=built)}
    if a.world and a.scope != "all":
        inputs["data"]["scope"] = a.scope       # (another set of cells: another build; "all" keeps the ids as before)
    if b.cap:
        inputs["data"]["cap"] = {ly: b.cap for ly in CAPPED if ly in layers}
    if a.cellset:
        b.only = {}                     # (q_ready: a cellset's cells hold their visit's layers only)
    if b.q_ready():
        inputs["data"]["q"] = quality.version()     # (the record's rules: another build; readers check each section)
    inputs["data"]["pins"] = pin_summary(datasets)  # (every weekly dataset's build: the build id is the pin set's too)
    build = cip.build_id(inputs)
    outdir = os.path.join(a.out, f"cip{cip.FORMAT}", b.release, build)
    unpinned = sorted(i for i, v in datasets.items() if v is None)
    if pins is not None and pins.get("build") not in (None, build):
        # (the pin set's own build is another: other code or switches than its plan's; --expect-build says so too)
        print(f"build_cip: the pin set is of build {pins.get('build')}, these inputs are {build}", file=sys.stderr)
    if a.write_pins:
        doc = {"format": PINS_FORMAT, "build": build, "plan_key": plan_key(inputs), "inputs": inputs,
               "pages": pagesdata.PAGES, "datasets": datasets}
        tmp = a.write_pins + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, sort_keys=True, separators=(",", ":"))
        os.replace(tmp, a.write_pins)
    if a.inputs_only:
        print(json.dumps({"build": build, "inputs": inputs, "out": outdir, "plan_key": plan_key(inputs),
                          "none": unpinned}, sort_keys=True))
        return 0
    if a.expect_build and a.expect_build != build and a.world and a.assemble and a.from_shards:
        # (the index of a finished build: its inputs are the ones its shards' records name, of this code; a weekly
        # data build that landed since does not make it another build)
        build, inputs = a.expect_build, None
        outdir = os.path.join(a.out, f"cip{cip.FORMAT}", b.release, build)
    elif a.expect_build and a.expect_build != build:
        print(json.dumps({"build": build, "expected": a.expect_build, "inputs": inputs}, sort_keys=True))
        print(f"build_cip: the inputs are build {build} now, not {a.expect_build} (other code or switches than the "
              f"plan's{'' if pins is not None else ', or no --pins: the weekly data as served now'})", file=sys.stderr)
        return 3
    os.makedirs(outdir, exist_ok=True)
    import build_bnwt
    meta = {"format": "cip", "version": cip.FORMAT, "release": b.release, "build": build}
    if a.world:
        return world(a, b, inputs, build, outdir, meta, layers, build_bnwt)
    if a.around:
        lat, lon, r = (float(v) for v in a.around.split(","))
        cells = cells_around(lat, lon, r)
    elif a.cells:
        cells = [mapcells.parse(c) for c in a.cells.split(",") if c]
    elif a.cellset:
        with open(a.cellset, encoding="utf-8") as fh:
            got = json.load(fh)
        only = {mapcells.parse(c): tuple(v) for c, v in got["cells"].items()}
        cells = list(only)
        b.only = only
        if "lidar" in got:              # (the visit's lidar cells: measured there only)
            b.lidar_cells = {mapcells.parse(c) for c in got["lidar"]}
    else:
        cells = cached_cells(a.cache)
    cells = sorted(set(cells), key=lambda k: fast._ef.pmt_tile_id(14, k.x, k.y))
    rects = rects_of(cells)
    pw = build_bnwt.PartWriter(outdir, cip.ARCHIVE, meta, rects, int(a.part_mb * 1e6))
    stats, lacking, t0 = {}, {}, time.perf_counter()
    cpu0 = time.process_time()
    total = 0
    done = {}
    for i, (key, secs, lacks, qwhy) in enumerate(quality_cells(b, cells, b.cell)):
        for s, why in lacks + ([(quality.SECTION, qwhy)] if qwhy else []):
            lacking.setdefault(s, []).append(f"{key}: {why}")
        cb = b.encode(key, secs, verify=not a.no_verify)
        if a.stats:
            section_stats(key, secs, cb, stats)
        stats.setdefault("_cells", {})[str(key)] = len(cb)
        total += len(cb)
        done[fast._ef.pmt_tile_id(14, key.x, key.y)] = cb
        if sys.stderr.isatty() or i % 25 == 0:
            print(f"\r{i + 1}/{len(cells)} cells, {total / 1e6:.1f} MB", end="", file=sys.stderr)
    print(file=sys.stderr)
    for tid in sorted(done):            # (the parts' directory in tile-id order)
        pw.add(tid, done[tid], False)
    parts = pw.finish()
    write_index(outdir, meta, inputs, rects, parts, len(cells), pw.contents, a.cache, a.airports,
                sorted({ly for v in b.only.values() for ly in v}) if b.only is not None else layers, a.online)
    stats["_total"] = dict(cells=len(cells), bytes=total, stored=sum(p["bytes"] for p in parts), parts=len(parts),
                           wall_s=round(time.perf_counter() - t0, 2), cpu_s=round(time.process_time() - cpu0, 2),
                           lacking={k: len(v) for k, v in lacking.items()})
    with open(os.path.join(outdir, "stats.json"), "w", encoding="utf-8") as fh:
        json.dump(stats, fh, indent=1, sort_keys=True)
    for k, v in sorted(lacking.items()):
        print(f"lacking {k}: {len(v)} cells, e.g. {v[0]}", file=sys.stderr)
    print(json.dumps(dict(build=build, out=outdir, **stats["_total"])))
    return 0


def world(a, b, inputs, build, outdir, meta, layers, build_bnwt):
    """The planet in z10 units (build_bnwt's plan, shards, parts and records; see .github/workflows/cip.yml)."""
    import multiprocessing as mp
    raw_dir = os.path.join(a.cache, "mapdata", "raw")
    N_ = 1 << cip.ZOOM
    rects = [(0, 0, N_ - 1, N_ - 1)]
    units = set()
    for arch in ("transportation", "buildings", "base"):        # (every cell any Overture layer reaches)
        units.update(build_bnwt.world_units(b.release, arch, a.unit_zoom, raw_dir))
    units = sorted(units, key=build_bnwt.unit_range)
    work = os.path.join(a.work or os.path.join(a.out, ".work"), b.release, build)
    parts = os.path.join(work, "parts")
    os.makedirs(parts, exist_ok=True)
    plan = {"build": build, "inputs": inputs, "unit_zoom": a.unit_zoom, "layers": list(layers), "world": True}
    pf = os.path.join(work, "plan.json")
    if inputs is None:
        pass                        # (--assemble of an expected build: the plan is its records')
    elif os.path.exists(pf):
        with open(pf, encoding="utf-8") as fh:
            if json.load(fh) != json.loads(json.dumps(plan)):
                raise SystemExit(f"{pf} is another build; use another --work")
    else:
        with open(pf, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, sort_keys=True)
    name = cip.ARCHIVE
    mine = units
    sn = si = None
    if a.shard:
        si, sn = (int(v) for v in a.shard.split("/"))
        mine = units[si * len(units) // sn:(si + 1) * len(units) // sn]
        rec_path = build_bnwt.shard_record(outdir, name, sn, si)
        if a.shard_parts and os.path.exists(rec_path):
            print(f"shard {a.shard}: done before ({rec_path})", file=sys.stderr)
            return 0
    if a.assemble and a.from_shards:
        if inputs is None:
            try:
                with open(build_bnwt.shard_record(outdir, name, a.from_shards, 0), encoding="utf-8") as fh:
                    plan = json.load(fh)["plan"]
            except (OSError, ValueError, KeyError) as e:
                raise SystemExit(f"build {build}: no readable record of shard 0 ({type(e).__name__}: {e})")
            inputs = plan["inputs"]
            if plan.get("build") != build or cip.build_id(inputs) != build \
                    or inputs.get("code") != cip.code_inputs(b.release):
                raise SystemExit(f"build {build}: its records are of other code or another build")
        plist, n, contents = build_bnwt.shards_assembled(outdir, name, a.from_shards, plan,
                                                         argparse.ArgumentParser())
        write_index(outdir, dict(meta, shards=a.from_shards), inputs, rects, plist, n, contents, a.cache, a.airports,
                    layers, a.online)
        print(json.dumps({"build": build, "parts": len(plist), "cells": n}))
        return 0
    todo = [u for u in mine if not os.path.exists(os.path.join(parts, f"{u[0]}_{u[1]}_{u[2]}.idx"))]
    if a.assemble and todo:
        raise SystemExit(f"{len(todo)} units not built yet")
    t0 = time.perf_counter()
    shown, done, cells, nbytes, lack = 0.0, 0, 0, 0, {}
    jobs = [(u, rects, parts, not a.no_verify, a.scope) for u in todo]
    b.store = False                 # (each cell's layers are built once: no layer files kept, nor read back)
    _B["data"] = {k: inputs["data"][k] for k in ("rails", "lanes", "osmtags")}     # (the build's: never two)
    same_data(b)
    if a.jobs <= 1:
        _B["b"] = b
        results = map(build_unit, jobs)
        pool = None
    else:
        pool = mp.get_context("spawn").Pool(a.jobs, _unit_init, (a.cache, a.online, layers, b.release, b.lidar,
                                                                  b.partial, _B["data"], b.cap, b.quality))
        results = pool.imap_unordered(build_unit, jobs)
    for _u, nc, nb, lk, _s in results:
        done, cells, nbytes = done + 1, cells + nc, nbytes + nb
        for k, v in lk.items():
            lack[k] = lack.get(k, 0) + v
        el = time.perf_counter() - t0
        if el - shown >= 60 or done == len(todo):
            shown = el
            print(f"{done}/{len(todo)} units, {cells} cells, {nbytes / 1e6:.1f} MB, {el:.0f} s", file=sys.stderr,
                  flush=True)
    if pool is not None:
        pool.close()
        pool.join()
    if a.shard and a.shard_parts:
        plist, n, contents = build_bnwt.write_archive(mine, parts, outdir, f"{name}.s{sn}-{si:04d}",
                                                      dict(meta, shard=[si, sn]), rects, int(a.part_mb * 1e6))
        rec = {"shard": si, "of": sn, "plan": plan, "units": len(mine), "parts": plist, "tiles": n,
               "contents": contents, "lacking": lack}
        os.makedirs(os.path.dirname(rec_path), exist_ok=True)
        with open(rec_path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(rec, fh, sort_keys=True, separators=(",", ":"))
        os.replace(rec_path + ".tmp", rec_path)
        print(json.dumps({"shard": a.shard, "build": build, "out": outdir, "units": len(mine), "cells": cells,
                          "bytes": nbytes, "parts": len(plist), "stored": sum(p["bytes"] for p in plist),
                          "contents": contents, "lacking": lack, "seconds": round(time.perf_counter() - t0, 1)}))
        return 0
    if a.shard:
        print(json.dumps({"shard": a.shard, "units": len(mine), "cells": cells, "bytes": nbytes, "lacking": lack}))
        return 0
    plist, n, contents = build_bnwt.write_archive(units, parts, outdir, name, meta, rects, int(a.part_mb * 1e6))
    write_index(outdir, meta, inputs, rects, plist, n, contents, a.cache, a.airports, layers, a.online)
    print(json.dumps({"build": build, "parts": len(plist), "cells": n, "bytes": sum(p["bytes"] for p in plist)}))
    return 0


def attribution():
    import overture
    return "\n".join([
        f"BeamNG.Earth compact per-cell inputs (CIP format {cip.FORMAT}): a collective database of separate sections.",
        "",
        "[m:roads m:connectors m:road_routes m:buildings m:building_parts m:water m:land_use m:infrastructure "
        "m:town_* m:hoods m:land] Overture Maps data, ODbL-1.0 (https://opendatacommons.org/licenses/odbl/1-0/).",
        f"Attribution: {overture.ATTRIBUTION}",
        "[m:bathymetry] Overture Maps bathymetry (ETOPO / GLOBathy), CC0-1.0.",
        "[m:places] Overture Maps places: CDLA-Permissive-2.0 / Apache-2.0 / CC0 per source (Overture's NOTICE).",
        "[t:rail t:lanes f:osmfuel f:picnic] OpenStreetMap contributors, ODbL-1.0.",
        "[z:*] heights derived from USGS 3DEP (public domain), IGN BD TOPO (Etalab 2.0), NVDB (NLOD), HK CSDI.",
        "[m:landcover] ESA WorldCover 2021 v200, CC BY 4.0. [m:canopy] Meta and WRI canopy height, CC BY 4.0.",
        "[airports] OurAirports, public domain.", ""])


if __name__ == "__main__":
    sys.exit(main())
