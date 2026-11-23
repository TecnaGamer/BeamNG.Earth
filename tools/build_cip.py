#!/usr/bin/env python3
"""build_cip.py: a CIP build (server/cip.py: architecture v2's compact per-cell inputs) for a set of z14 cells.

    python3 tools/build_cip.py --cache SRC --out OUT --around 39.2834,-76.5934,2000 [--online]
    python3 tools/build_cip.py --cache SRC --out OUT --cells 14/4705/6244,14/4706/6244
    python3 tools/build_cip.py --cache SRC --out OUT --cached          (every cell the cache holds a map layer of)
    python3 tools/build_cip.py --cache SRC --out OUT --world --shard I/N --shard-parts   (a CI job; see cip.yml)

Each cell's sections are made by the server's own code, in this process, with CIP off (cip.py lists them):
  m:<layer>   MapData's cell file arrays of the layer: the cache's file when it holds one (what the server reads), else
              MapData._build from the sources (Overture through the build's map source, OSM's road tags joined) and
              stored there as the server stores it. A parquet stand-in (the tiles failed) is never put in a build.
  t:rail t:lanes   railtags.cell_slice of the weekly shards for the cell's roads' way ids (decisions.ids_of)
  z:<src>     the roadz answer files the cache holds for the cell (--online: measured first, for the cells that want
              a lidar: roadz.RoadZ.lines, the 3DEP reads moved here from the players)
  f:osmfuel f:picnic   the per-cell rows of OpenStreetMap's fuel stations / picnic tables the cache holds (--online:
              read first)
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
and the weekly data builds the cells were made from (OSM road tags, rail, lanes, stations). Deterministic: the same
inputs give the same bytes (cells in tile-id order, zstd at a fixed level, JSON with sorted keys, no timestamps).
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
import fast  # noqa: E402
import mapcells  # noqa: E402

N = 1 << cip.ZOOM
VECTOR = ("roads", "connectors", "buildings", "water", "land_use", "infrastructure", "building_parts", "places",
          "road_routes", "town_areas", "town_points", "hoods", "land", "bathymetry")
RASTER = ("landcover", "canopy")
DEFAULT_LAYERS = VECTOR + RASTER
# the planet build's: the rasters stay on their sources (WorldCover / the canopy model: ~2 / ~100-200 kB a land cell, a
# planet of ~109 M land cells would be ~0.2 / several TB for ~1-4 MB of a cold visit's downloads)
WORLD_LAYERS = VECTOR


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
        self.md = bw._s.md
        self.release = self.md.release
        self.stats = {}
        self.partial = True
        self.lidar = "have"
        self.only = None            # {cell: layers} (--cellset), None: self.layers for every cell
        self.lidar_cells = None     # the cells whose lidar is measured (--cellset's "lidar"), None: every one wanting it

    # --- data builds ---------------------------------------------------------------------------------------------
    def data_builds(self):
        md = self.md
        out = {}
        for name, t in (("rails", getattr(md, "rail_tags", None)), ("lanes", getattr(md, "lane_tags", None))):
            out[name] = t.known() if t is not None else None
        try:
            sh = md._osm_shards()
            got = sh.manifest() if sh is not None else None
            out["osmtags"] = got[0] if got else None
        except Exception:
            out["osmtags"] = None
        return out

    # --- one cell --------------------------------------------------------------------------------------------------
    def layer(self, key, layer):
        import mapdata
        md = self.md
        name = md.layer_name(layer)
        path = md.path(key, layer)
        arrays = md._load(path)
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
        roads = None
        for layer in (self.only.get(key, ()) if self.only is not None else self.layers):
            try:
                name, arrays = self.layer(key, layer)
            except Exception as e:
                lacks.append((f"m:{layer}", f"{type(e).__name__}: {e}"))
                continue
            secs[f"m:{name}"] = arrays
            if layer == "roads":
                roads = mapgeom.Features.from_arrays("roads", {k: v for k, v in arrays.items() if k != "provenance"},
                                                     "")
        if roads is not None:
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
                md.picnic_osm._cell(key, "demand")
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


def _unit_init(cache, online, layers, release, lidar, partial):
    _B["b"] = b = Builder(cache, online, layers, release)
    b.lidar, b.partial = lidar, partial


def build_unit(args):
    """One z10 unit's part files (every cell of the unit inside the rects, empty ones too: a reader falls back to the
    sources only where the build has no cell). Returns (unit, cells, bytes, lacking, seconds)."""
    import build_bnwt
    unit, rects, parts, verify = args
    b = _B["b"]
    t0 = time.perf_counter()
    uz, ux, uy = unit
    sh = cip.ZOOM - uz
    cells = sorted({(fast._ef.pmt_tile_id(cip.ZOOM, x, y), x, y) for x0, y0, x1, y1 in rects
                    for x in range(max(x0, ux << sh), min(x1, ((ux + 1) << sh) - 1) + 1)
                    for y in range(max(y0, uy << sh), min(y1, ((uy + 1) << sh) - 1) + 1)})
    base = os.path.join(parts, f"{uz}_{ux}_{uy}")
    ids, lens, ind, lacking = [], [], [], {}
    tmp = f"{base}.bin.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        for tid, x, y in cells:
            key = mapcells.CellKey(cip.ZOOM, x, y)
            secs, lacks = b.cell(key)
            if lacks and not b.partial:
                raise RuntimeError(f"{key}: lacks {lacks[:3]}")     # (a world build never ships a source's failure)
            for sec, _why in lacks:
                lacking[sec] = lacking.get(sec, 0) + 1
            cb = b.encode(key, secs, verify=verify)
            f.write(cb)
            ids.append(tid)
            lens.append(len(cb))
            ind.append(0)
    os.replace(tmp, base + ".bin")
    itmp = f"{base}.idx.{os.getpid()}.tmp"
    with open(itmp, "wb") as f:
        np.save(f, np.array([ids, lens, ind], np.uint64).reshape(3, -1))
    os.replace(itmp, base + ".idx")
    del build_bnwt
    return unit, len(ids), int(sum(lens)), lacking, time.perf_counter() - t0


def write_index(outdir, meta, inputs, rects, plist, cells, contents, cache, airports_dir, layers=None):
    index = dict(meta, archive=cip.ARCHIVE, inputs=inputs, coverage=[list(r) for r in rects], parts=plist,
                 cells=cells, contents=contents)
    if layers is not None:          # (the map layers the build holds; readers take the others from their sources)
        index["layers"] = sorted(set(layers))
    ap_dir = airports_dir
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
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--around", help="lat,lon,metres")
    g.add_argument("--cells", help="z/x/y,z/x/y,...")
    g.add_argument("--cached", action="store_true", help="every cell the cache holds a map layer of")
    g.add_argument("--cellset", help='a JSON {"cells": {"14/x/y": [layers]}}: those cells, each with its layers only '
                                     '(a recorded visit\'s footprint)')
    g.add_argument("--world", action="store_true", help="every cell any Overture source file reaches (z10 units)")
    g.add_argument("--inputs-only", action="store_true", help="print the build's id and inputs, build nothing")
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
    ap.add_argument("--work", help="world: units' parts folder (default OUT/.work)")
    ap.add_argument("--shard", help="world: I/N, the I-th of N contiguous runs of the units (Hilbert order)")
    ap.add_argument("--shard-parts", action="store_true", help="with --shard: the shard's own parts and its record")
    ap.add_argument("--assemble", action="store_true", help="world: the index from the parts (or --from-shards)")
    ap.add_argument("--from-shards", type=int, metavar="N", help="with --assemble: from the N shards' records")
    a = ap.parse_args(argv)
    import pcommon
    pcommon.check_cache(a.cache)
    fast.require(cip.NATIVE, "build_cip")
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    layers = tuple(x for x in (a.layers or ",".join(WORLD_LAYERS if a.world else DEFAULT_LAYERS)).split(",") if x)
    b = Builder(a.cache, a.online, layers, a.release)
    b.partial = a.partial
    b.lidar = a.lidar or ("measure" if a.online else "have")
    if a.cellset:
        with open(a.cellset, encoding="utf-8") as fh:
            built = sorted({ly for v in json.load(fh)["cells"].values() for ly in v})
    else:
        built = sorted(layers)
    inputs = {"code": cip.code_inputs(b.release),
              "data": dict(b.data_builds(), lidar=b.lidar, zstd=cip.LEVEL, layers=built)}
    build = cip.build_id(inputs)
    if a.inputs_only:
        print(json.dumps({"build": build, "inputs": inputs}, sort_keys=True))
        return 0
    outdir = os.path.join(a.out, f"cip{cip.FORMAT}", b.release, build)
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
    for i, key in enumerate(cells):
        secs, lacks = b.cell(key)
        for s, why in lacks:
            lacking.setdefault(s, []).append(f"{key}: {why}")
        cb = b.encode(key, secs, verify=not a.no_verify)
        if a.stats:
            section_stats(key, secs, cb, stats)
        stats.setdefault("_cells", {})[str(key)] = len(cb)
        total += len(cb)
        pw.add(fast._ef.pmt_tile_id(14, key.x, key.y), cb, False)
        if sys.stderr.isatty() or i % 25 == 0:
            print(f"\r{i + 1}/{len(cells)} cells, {total / 1e6:.1f} MB", end="", file=sys.stderr)
    print(file=sys.stderr)
    parts = pw.finish()
    write_index(outdir, meta, inputs, rects, parts, len(cells), pw.contents, a.cache, a.airports,
                sorted({ly for v in b.only.values() for ly in v}) if b.only is not None else layers)
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
    if os.path.exists(pf):
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
        plist, n, contents = build_bnwt.shards_assembled(outdir, name, a.from_shards, plan,
                                                         argparse.ArgumentParser())
        write_index(outdir, dict(meta, shards=a.from_shards), inputs, rects, plist, n, contents, a.cache, a.airports,
                    layers)
        print(json.dumps({"build": build, "parts": len(plist), "cells": n}))
        return 0
    todo = [u for u in mine if not os.path.exists(os.path.join(parts, f"{u[0]}_{u[1]}_{u[2]}.idx"))]
    if a.assemble and todo:
        raise SystemExit(f"{len(todo)} units not built yet")
    t0 = time.perf_counter()
    shown, done, cells, nbytes, lack = 0.0, 0, 0, 0, {}
    jobs = [(u, rects, parts, not a.no_verify) for u in todo]
    if a.jobs <= 1:
        _B["b"] = b
        results = map(build_unit, jobs)
        pool = None
    else:
        pool = mp.get_context("spawn").Pool(a.jobs, _unit_init, (a.cache, a.online, layers, b.release, b.lidar,
                                                                  b.partial))
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
        print(json.dumps({"shard": a.shard, "units": len(mine), "cells": cells, "bytes": nbytes, "parts": len(plist),
                          "lacking": lack, "seconds": round(time.perf_counter() - t0, 1)}))
        return 0
    if a.shard:
        print(json.dumps({"shard": a.shard, "units": len(mine), "cells": cells, "bytes": nbytes, "lacking": lack}))
        return 0
    plist, n, contents = build_bnwt.write_archive(units, parts, outdir, name, meta, rects, int(a.part_mb * 1e6))
    write_index(outdir, meta, inputs, rects, plist, n, contents, a.cache, a.airports, layers)
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
        "[t:rail t:lanes f:osmfuel f:picnic, OSM tags in m:roads] OpenStreetMap contributors, ODbL-1.0.",
        "[z:*] heights derived from USGS 3DEP (public domain), IGN BD TOPO (Etalab 2.0), NVDB (NLOD), HK CSDI.",
        "[m:landcover] ESA WorldCover 2021 v200, CC BY 4.0. [m:canopy] Meta and WRI canopy height, CC BY 4.0.",
        "[airports] OurAirports, public domain.", ""])


if __name__ == "__main__":
    sys.exit(main())
