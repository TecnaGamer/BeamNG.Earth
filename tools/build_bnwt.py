#!/usr/bin/env python3
"""build_bnwt.py: a BNWT archive (server/bnwt.py: our prebaked z14 input tiles) for a bbox or a whole Overture release.

    python3 tools/build_bnwt.py --archive base --bbox -76.62,39.27,-76.56,39.30 --out OUT [--work WORK] [--jobs 4]
    python3 tools/build_bnwt.py --archive base --world --out OUT --work WORK --shard 3/40      (one job of a matrix)
    python3 tools/build_bnwt.py --archive base --world --out OUT --work WORK --assemble        (the archive from them)

Each cell's tile holds, per layer of the archive (bnwt.ARCHIVES), exactly what the server's own source gives for the
cell, encoded by the native codec (lossless; every tile is decoded again and compared before it is kept, unless
--no-verify):
- parquet archives (base): the block's rows read once by one query per unit (bnwtunit.py), each cell made from them as
  overture.Reader.query makes it (--check K compares every K-th cell with Reader.query itself);
- the other archives: pmtiles.Reader.query per cell (Overture's tiles, the unit's fetched together first).
A cell whose layers are all its source's empty layer gets no tile. Cells are built per unit (an aligned square of
2^(14 - unit_zoom) cells a side) in Hilbert order:

    WORK/<release>/<archive>/plan.json                what is being built (a resumed run must ask the same)
    WORK/<release>/<archive>/parts/<z>_<x>_<y>.bin    a unit's tiles, back to back, in tile-id order
                                    ...<z>_<x>_<y>.idx  their tile ids, lengths, cell-independence (written last)

so a run stopped at any point resumes with the units not done; memory: one unit's rows per worker. The archive is then
written in one pass, as parts of at most --part-mb (a CDN caches files of 512 MB or less whole), each a contiguous range
of tile ids; tiles of the same bytes (open sea) are kept once per part (consecutive ones: one run-length entry):

    OUT/bnwt<FORMAT>/<release>/<archive>.c<CONTENT>.index.json     coverage, layers, source, the parts' id ranges
    OUT/bnwt<FORMAT>/<release>/<archive>.c<CONTENT>.p<NNN>.pmtiles  the parts (PMTiles v3, clustered)
    OUT/bnwt<FORMAT>/<release>/<archive>.c<CONTENT>.manifest.json  the index + sizes, sha256s, licence section
    OUT/bnwt<FORMAT>/<release>/ATTRIBUTION-<section>.txt            the attribution and licence of each section

A matrix of jobs: each runs --shard I/N (its units' parts only), the parts folders are merged, one --assemble writes
the archive (the same bytes as one job building everything).

Deterministic: the same inputs give the same bytes (tiles in tile-id order, zstd at a fixed level, no timestamps,
JSON with sorted keys). Needs duckdb (server/requirements.txt) and the native module (fast.require(bnwt.NATIVE)).
Serve OUT as the server's BNGE_BNWT_URL (http(s), file:// or a folder).
"""
import argparse
import hashlib
import json
import logging
import multiprocessing as mp
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import numpy as np  # noqa: E402

import bnwt  # noqa: E402
import fast  # noqa: E402
import mapcells  # noqa: E402
import overture  # noqa: E402

UNIT_ZOOM = 10              # a unit: 16 x 16 cells
CHUNK_UNITS = 64            # units per worker pool (a fresh pool per chunk: workers' DuckDB / caches do not grow)
PART_MB = 400               # a part's data at most (Cloudflare caches files of 512 MB or less)
N = 1 << bnwt.ZOOM

# The licences of the archives' sections (the research report's section 5): Overture's ODbL themes in one; places
# (CDLA-Permissive-2.0 / Apache-2.0 / CC0 per source) in an archive and a section of their own, never mixed in
SECTIONS = {
    "overture-odbl": dict(themes=("transportation", "buildings", "base", "divisions"), licence="ODbL-1.0",
                          url="https://opendatacommons.org/licenses/odbl/1-0/"),
    "overture-places": dict(themes=("places",), licence="CDLA-Permissive-2.0 (and per source: Apache-2.0, CC0-1.0)",
                            url="https://cdla.dev/permissive-2-0/"),
}


# --- units ----------------------------------------------------------------------------------------
def unit_range(u):
    return fast._ef.bnwt_square_range(u[0], u[1], u[2], bnwt.ZOOM)


def units_of_rect(x0, y0, x1, y1, uz):
    sh = bnwt.ZOOM - uz
    return [(uz, ux, uy) for ux in range(x0 >> sh, (x1 >> sh) + 1) for uy in range(y0 >> sh, (y1 >> sh) + 1)]


def world_units(release, archive, uz, raw_dir):
    """The units any source file of the archive's layers reaches (the rest of the world has no feature: no tile)."""
    idx = overture.StacIndex(release, raw_dir, None)
    n = 1 << uz
    out = set()
    for layer in bnwt.ARCHIVES[archive]["layers"]:
        spec = overture.LAYERS[layer]
        c = idx.collection(spec["theme"], spec["type"])
        for bb in c["bboxes"]:
            lon0, lat0, lon1, lat1 = (float(v) for v in bb[:4])
            lat0, lat1 = max(lat0, -mapcells.MAX_LAT), min(lat1, mapcells.MAX_LAT)
            if lat0 > lat1:
                continue
            a = mapcells.cell_of(lat1, max(lon0, -180.0), uz)
            b = mapcells.cell_of(lat0, min(lon1, 180.0 - 1e-9), uz)
            out.update((uz, x, y) for x in range(a.x, b.x + 1) for y in range(a.y, b.y + 1) if 0 <= x < n and 0 <= y < n)
    return sorted(out)


# --- workers --------------------------------------------------------------------------------------
_W = {}


def _readers(raw_dir, release):
    if "rd" not in _W:
        import netfetch
        logging.basicConfig(level=logging.WARNING)
        f = netfetch.Fetcher()
        _W["rd"] = overture.Reader(raw_dir, f, release)
        _W["pmt"] = None
        _W["fetcher"] = f
    if _W.get("pmt") is None:
        import pmtiles
        _W["pmt"] = pmtiles.Reader(raw_dir, _W["fetcher"], release) if "fetcher" in _W else None
    return _W["rd"], _W["pmt"]


def cell_layers(archive, unit, cells, raw_dir, release, check=0):
    """{(x, y): {layer: (Features, sources)}} of the unit's cells, as the archive's source gives them."""
    import bnwtunit
    spec = bnwt.ARCHIVES[archive]
    rd, pmt = _readers(raw_dir, release)
    out = {(x, y): {} for _t, x, y in cells}
    uz, ux, uy = unit
    sh = bnwt.ZOOM - uz
    if spec["source"] == "parquet":
        ub = mapcells.CellKey(uz, ux, uy).bounds()
        x0, y0, side = ux << sh, uy << sh, 1 << sh
        for layer in spec["layers"]:
            ur = bnwtunit.unit_rows(rd, layer, ub, "demand")
            offs = rows = None
            if ur is not None:
                offs, rows = bnwtunit.assign(ur, x0, y0, side, side)
            for k, (_t, x, y) in enumerate(cells):
                key = mapcells.CellKey(bnwt.ZOOM, x, y)
                if ur is None:
                    got = (overture.empty(layer), {})
                else:
                    i = (x - x0) * side + (y - y0)
                    got = bnwtunit.cell_layer(ur, rows[offs[i]:offs[i + 1]], key)
                if check and k % check == 0:
                    want = rd.query(layer, key, "demand")
                    diff = bnwt.differences(want, got)
                    if diff:
                        raise RuntimeError(f"{key} {layer}: the block's cell is not Reader.query's: {diff[:3]}")
                out[(x, y)][layer] = got
    else:
        keys = [mapcells.CellKey(bnwt.ZOOM, x, y) for _t, x, y in cells]
        pmt.prefetch([(k, ly) for k in keys for ly in spec["layers"]], "demand")
        for key in keys:
            for layer in spec["layers"]:
                out[(key.x, key.y)][layer] = bnwt.blanked(archive, layer, pmt.query(layer, key, "demand"))
    return out


def build_unit(args):
    """One unit's part files (the tiles of its cells inside the rects). Returns (unit, tiles, bytes, cells, seconds)."""
    unit, rects, archive, release, raw_dir, parts, verify, level, check = args
    t0 = time.perf_counter()
    uz, ux, uy = unit
    sh = bnwt.ZOOM - uz
    cells = sorted({(fast._ef.pmt_tile_id(bnwt.ZOOM, x, y), x, y) for x0, y0, x1, y1 in rects
                    for x in range(max(x0, ux << sh), min(x1, ((ux + 1) << sh) - 1) + 1)
                    for y in range(max(y0, uy << sh), min(y1, ((uy + 1) << sh) - 1) + 1)})
    got = cell_layers(archive, unit, cells, raw_dir, release, check)
    empties = {ly: bnwt.empty(archive, ly) for ly in bnwt.ARCHIVES[archive]["layers"]}
    base = os.path.join(parts, f"{uz}_{ux}_{uy}")
    ids, lens, ind = [], [], []
    tmp = f"{base}.bin.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        for tid, x, y in cells:
            key = mapcells.CellKey(bnwt.ZOOM, x, y)
            layers = got[(x, y)]
            if all(not bnwt.differences(empties[ly], v) for ly, v in layers.items()):
                continue
            tile, independent = bnwt.encode_tile(key, layers, level)
            if verify:
                for ly, v in layers.items():
                    back = bnwt.decode_layer(tile, ly, key) or empties[ly]
                    diff = bnwt.differences(v, back)
                    if diff:
                        raise RuntimeError(f"{key} {ly}: the tile does not give the cell back: {diff[:3]}")
            f.write(tile)
            ids.append(tid)
            lens.append(len(tile))
            ind.append(int(independent))
    os.replace(tmp, base + ".bin")
    itmp = f"{base}.idx.{os.getpid()}.tmp"
    with open(itmp, "wb") as f:
        np.save(f, np.array([ids, lens, ind], np.uint64).reshape(3, -1))
    os.replace(itmp, base + ".idx")
    return unit, len(ids), int(sum(lens)), len(cells), time.perf_counter() - t0


# --- archive --------------------------------------------------------------------------------------
class PartWriter:
    """The archive's parts, written as the tiles come in tile-id order."""

    def __init__(self, outdir, name, meta, rects, part_bytes):
        self.outdir, self.name, self.meta, self.limit = outdir, name, meta, part_bytes
        x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
        x1, y1 = max(r[2] for r in rects), max(r[3] for r in rects)
        w, s = mapcells.CellKey(bnwt.ZOOM, x0, y1).bounds()[:2]
        e, n = mapcells.CellKey(bnwt.ZOOM, x1, y0).bounds()[2:]
        e7 = lambda v: int(round(v * 1e7))  # noqa: E731
        self.bounds, self.center = (e7(w), e7(s), e7(e), e7(n)), (bnwt.ZOOM, e7((w + e) / 2), e7((s + n) / 2))
        self.parts = []
        self.tiles = self.contents = 0
        os.makedirs(outdir, exist_ok=True)
        self._open()

    def _open(self):
        self.ids, self.offs, self.lens = [], [], []
        self.seen = {}
        self.data_len = 0
        self.tmp = os.path.join(self.outdir, f".{self.name}.p{len(self.parts):03d}.data.tmp")
        self.f = open(self.tmp, "wb")

    def add(self, tid, b, independent):
        if self.data_len >= self.limit and self.ids:
            self.close_part()
        # (a tile never holds its cell: the same bytes in two cells are the same layers there, a cell's edges coded as
        # such; open sea, and lines or pieces whose bbox spans cells without being cut. Kept once a part: ~100 B a
        # tile of memory)
        k = hashlib.sha256(b).digest()
        if k in self.seen:
            off = self.seen[k]
        else:
            off = self.data_len
            self.f.write(b)
            self.data_len += len(b)
            self.contents += 1
            self.seen[k] = off
        self.ids.append(tid)
        self.offs.append(off)
        self.lens.append(len(b))
        self.tiles += 1

    def close_part(self):
        self.f.close()
        i = len(self.parts)
        file = f"{self.name}.p{i:03d}.pmtiles"
        meta = dict(self.meta, part=i)
        head = fast._ef.bnwt_pmtiles_head(np.array(self.ids, np.uint64), np.array(self.offs, np.uint64),
                                          np.array(self.lens, np.uint64), self.data_len,
                                          json.dumps(meta, sort_keys=True, separators=(",", ":")).encode(),
                                          bnwt.ZOOM, bnwt.ZOOM, self.bounds, self.center)
        path = os.path.join(self.outdir, file)
        sha = hashlib.sha256(head)
        with open(path + ".tmp", "wb") as out:
            out.write(head)
            with open(self.tmp, "rb") as src:
                while True:
                    b = src.read(1 << 22)
                    if not b:
                        break
                    out.write(b)
                    sha.update(b)
        os.replace(path + ".tmp", path)
        os.remove(self.tmp)
        self.parts.append({"file": file, "first": int(self.ids[0]), "last": int(self.ids[-1]),
                           "bytes": os.path.getsize(path), "sha256": sha.hexdigest(), "tiles": len(self.ids)})
        self._open()

    def finish(self):
        if self.ids:
            self.close_part()
        self.f.close()                  # (the next part's file, opened by close_part: none comes)
        os.remove(self.tmp)
        return self.parts


def write_archive(units, parts, outdir, name, meta, rects, part_bytes):
    """The archive's parts from the units' parts (in Hilbert order), streamed; returns the parts' records."""
    pw = PartWriter(outdir, name, meta, rects, part_bytes)
    last = -1
    for u in sorted(units, key=unit_range):
        base = os.path.join(parts, f"{u[0]}_{u[1]}_{u[2]}")
        a = np.load(base + ".idx")
        if not a.shape[1]:
            continue
        with open(base + ".bin", "rb") as f:
            for tid, ln, ind in a.T.tolist():
                if tid <= last:
                    raise RuntimeError("tile ids out of order across units")
                last = tid
                b = f.read(ln)
                if len(b) != ln:
                    raise RuntimeError(f"{base}.bin: short")
                pw.add(tid, b, bool(ind))
    return pw.finish(), pw.tiles, pw.contents


def attribution_text(archive):
    layers = bnwt.ARCHIVES[archive]["layers"]
    themes = sorted({overture.LAYERS[ly]["theme"] for ly in layers})
    sec = SECTIONS[bnwt.ARCHIVES[archive]["section"]]
    lines = [f"BeamNG.Earth world input tiles (BNWT format {bnwt.FORMAT}), derived from Overture Maps data.", "",
             f"[{bnwt.ARCHIVES[archive]['section']}] archive {archive}, layers: " + ", ".join(layers),
             f"Licence: {sec['licence']} ({sec['url']})", f"Attribution: {overture.ATTRIBUTION}"]
    lines += [f"Sources ({t}): {overture.THEME_LICENCES.get(t, '')}" for t in themes]
    lines += ["https://docs.overturemaps.org/attribution/", ""]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--bbox", action="append", help="lon0,lat0,lon1,lat1 (repeatable)")
    g.add_argument("--world", action="store_true", help="every cell a source file of the archive's layers reaches")
    ap.add_argument("--out", required=True)
    ap.add_argument("--work", help="parts folder (default OUT/.work)")
    ap.add_argument("--archive", default="base", choices=sorted(bnwt.ARCHIVES))
    ap.add_argument("--release", default=overture.RELEASE)
    ap.add_argument("--cache-dir", default=os.path.expanduser("~/.cache/beamng-earth"),
                    help="where the source files' blocks are kept (its mapdata/raw)")
    ap.add_argument("--jobs", type=int, default=max(1, min(8, (os.cpu_count() or 2) // 2)))
    ap.add_argument("--unit-zoom", type=int, default=UNIT_ZOOM)
    ap.add_argument("--level", type=int, default=bnwt.LEVEL)
    ap.add_argument("--part-mb", type=float, default=PART_MB)
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--check", type=int, default=0, help="K: every K-th cell of a parquet archive compared with "
                                                         "Reader.query (slow; for verifying the block path)")
    ap.add_argument("--shard", help="I/N: build only every N-th unit from the I-th (parts only, no archive: one job of a "
                                    "matrix; the parts folders are merged and --assemble writes the archive)")
    ap.add_argument("--assemble", action="store_true", help="only write the archive from the parts (all must be done)")
    a = ap.parse_args(argv)
    fast.require(bnwt.NATIVE, "build_bnwt")
    raw_dir = os.path.join(a.cache_dir, "mapdata", "raw")
    work = os.path.join(a.work or os.path.join(a.out, ".work"), a.release, a.archive)
    parts = os.path.join(work, "parts")
    os.makedirs(parts, exist_ok=True)
    if a.world:
        rects = [(0, 0, N - 1, N - 1)]
        units = world_units(a.release, a.archive, a.unit_zoom, raw_dir)
    else:
        rects = []
        for bb in a.bbox:
            lon0, lat0, lon1, lat1 = (float(v) for v in bb.split(","))
            nw, se = mapcells.cell_of(lat1, lon0, bnwt.ZOOM), mapcells.cell_of(lat0, lon1, bnwt.ZOOM)
            rects.append((nw.x, nw.y, se.x, se.y))
        rects = sorted(set(rects))
        units = sorted({u for r in rects for u in units_of_rect(*r, a.unit_zoom)})
    spec = bnwt.ARCHIVES[a.archive]
    plan = {"archive": a.archive, "release": a.release, "rects": [list(r) for r in rects], "unit_zoom": a.unit_zoom,
            "format": bnwt.FORMAT, "content": bnwt.CONTENT[a.archive], "source": spec["source"], "level": a.level,
            "world": bool(a.world)}
    pf = os.path.join(work, "plan.json")
    if os.path.exists(pf):
        with open(pf, encoding="utf-8") as f:
            old = json.load(f)
        if old != plan:
            ap.error(f"{pf} is another build ({old}); use another --work")
    else:
        with open(pf, "w", encoding="utf-8") as f:
            json.dump(plan, f, sort_keys=True)
    units.sort(key=unit_range)
    mine = units
    if a.shard:
        si, sn = (int(v) for v in a.shard.split("/"))
        mine = units[si::sn]
    todo = [u for u in mine if not os.path.exists(os.path.join(parts, f"{u[0]}_{u[1]}_{u[2]}.idx"))]
    if a.assemble:
        if todo:
            ap.error(f"{len(todo)} units not built yet (first {todo[0]})")
        todo = []
    print(f"{len(units)} units ({len(units) - len(todo)} done), {a.jobs} jobs", file=sys.stderr)
    t0 = time.perf_counter()
    done = tiles = nbytes = ncells = 0
    jobs = [(u, rects, a.archive, a.release, raw_dir, parts, not a.no_verify, a.level, a.check) for u in todo]
    ctx = mp.get_context("spawn")
    for i in range(0, len(jobs), CHUNK_UNITS):
        chunk = jobs[i:i + CHUNK_UNITS]
        if a.jobs <= 1:
            results = map(build_unit, chunk)
        else:
            pool = ctx.Pool(a.jobs)
            results = pool.imap_unordered(build_unit, chunk)
        for u, nt, nb, nc, _s in results:
            done += 1
            tiles += nt
            nbytes += nb
            ncells += nc
            el = time.perf_counter() - t0
            print(f"\r{done}/{len(todo)} units, {ncells} cells, {tiles} tiles, {nbytes / 1e6:.1f} MB, {el:.0f} s",
                  end="", file=sys.stderr)
        if a.jobs > 1:
            pool.close()
            pool.join()
    print(file=sys.stderr)
    if a.shard:
        print(json.dumps({"shard": a.shard, "units": len(mine), "cells": ncells, "tiles": tiles, "bytes": nbytes,
                          "seconds": round(time.perf_counter() - t0, 1)}))
        return 0
    name = f"{a.archive}.c{bnwt.CONTENT[a.archive]}"
    outdir = os.path.join(a.out, f"bnwt{bnwt.FORMAT}", a.release)
    meta = {"format": "bnwt", "version": bnwt.FORMAT, "content": bnwt.CONTENT[a.archive], "release": a.release,
            "archive": a.archive, "source": spec["source"], "layers": list(spec["layers"]),
            "coverage": [list(r) for r in rects], "zoom": bnwt.ZOOM, "attribution": overture.ATTRIBUTION}
    plist, n, contents = write_archive(units, parts, outdir, name, meta, rects, int(a.part_mb * 1e6))
    index = dict(meta, parts=[{k: p[k] for k in ("file", "first", "last")} for p in plist])
    with open(os.path.join(outdir, name + ".index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, sort_keys=True, separators=(",", ":"))
    att = f"ATTRIBUTION-{spec['section']}.txt"
    with open(os.path.join(outdir, att), "w", encoding="utf-8") as f:
        f.write(attribution_text(a.archive))
    themes = sorted({overture.LAYERS[ly]["theme"] for ly in spec["layers"]})
    sec = SECTIONS[spec["section"]]
    manifest = dict(index, parts=plist, tiles=n, contents=contents, bytes=sum(p["bytes"] for p in plist),
                    native=bnwt.NATIVE, files=[att],
                    licence=dict(section=spec["section"], licence=sec["licence"], url=sec["url"],
                                 themes={t: overture.THEME_LICENCES.get(t, "") for t in themes}))
    with open(os.path.join(outdir, name + ".manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, sort_keys=True, indent=1)
    print(json.dumps({"index": os.path.join(outdir, name + ".index.json"), "parts": len(plist), "tiles": n,
                      "contents": contents, "bytes": manifest["bytes"], "cells": ncells,
                      "seconds": round(time.perf_counter() - t0, 1)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
