#!/usr/bin/env python3
"""build_bnwt.py: a BNWT archive (server/bnwt.py: our prebaked z14 input tiles) for a bbox or a whole Overture release.

    python3 tools/build_bnwt.py --bbox -76.62,39.27,-76.56,39.30 --out OUT [--work WORK] [--jobs 4]
    python3 tools/build_bnwt.py --world --out OUT --work WORK --jobs 4        (every cell a source file reaches)

Each cell's tile holds, per layer of the archive (bnwt.ARCHIVES), exactly what the server's own parquet path gives for
the cell (overture.Reader.query, run here per cell: the same code, so the same features, columns, clipping and order),
encoded by the native codec (lossless; every tile is decoded again and compared before it is kept, unless
--no-verify). The cells are built per unit (an aligned square of 2^(14-unit_zoom) cells a side) in Hilbert order:

    WORK/<release>/<archive>/plan.json                what is being built (a resumed run must ask the same)
    WORK/<release>/<archive>/parts/<z>_<x>_<y>.bin    a unit's tiles, back to back, in tile-id order
                                    ...<z>_<x>_<y>.idx  its tile ids and lengths (written last: the unit is done)

so a run stopped at any point resumes with the units not done, and memory stays one cell's features per worker (plus
16 bytes a tile for the directories at the end). The archive is then written in one pass:

    OUT/bnwt<FORMAT>/<release>/<archive>.c<CONTENT>.pmtiles        the archive (PMTiles v3, clustered)
    OUT/bnwt<FORMAT>/<release>/<archive>.c<CONTENT>.manifest.json  release, format, size, sha256, coverage, licences
    OUT/bnwt<FORMAT>/<release>/ATTRIBUTION.txt                     the attribution and licence of each section

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
N = 1 << bnwt.ZOOM

# The licences of the archive's sections (the research report's section 5): Overture's ODbL themes in one section;
# places (CDLA-Permissive-2.0 / Apache-2.0 / CC0 per source) would be a section of their own, never mixed in
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
    for layer in bnwt.ARCHIVES[archive]:
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


def _reader(raw_dir, release):
    if "rd" not in _W:
        import netfetch
        logging.basicConfig(level=logging.WARNING)
        _W["rd"] = overture.Reader(raw_dir, netfetch.Fetcher(), release)
    return _W["rd"]


def build_unit(args):
    """One unit's part files (the tiles of its cells inside the rects). Returns (unit, tiles, bytes, seconds)."""
    unit, rects, archive, release, raw_dir, parts, verify, level = args
    t0 = time.perf_counter()
    rd = _reader(raw_dir, release)
    uz, ux, uy = unit
    sh = bnwt.ZOOM - uz
    cells = sorted({(fast._ef.pmt_tile_id(bnwt.ZOOM, x, y), x, y) for x0, y0, x1, y1 in rects
                    for x in range(max(x0, ux << sh), min(x1, ((ux + 1) << sh) - 1) + 1)
                    for y in range(max(y0, uy << sh), min(y1, ((uy + 1) << sh) - 1) + 1)})
    base = os.path.join(parts, f"{uz}_{ux}_{uy}")
    ids, lens = [], []
    tmp = f"{base}.bin.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        for tid, x, y in cells:
            key = mapcells.CellKey(bnwt.ZOOM, x, y)
            layers = {ly: rd.query(ly, key, "demand") for ly in bnwt.ARCHIVES[archive]}
            if not any(len(feats) or src for feats, src in layers.values()):
                continue
            tile = bnwt.encode_tile(key, layers, level)
            if verify:
                for ly, got in layers.items():
                    back = bnwt.decode_layer(tile, ly) or (overture.empty(ly), {})
                    diff = bnwt.differences(got, back)
                    if diff:
                        raise RuntimeError(f"{key} {ly}: the tile does not give the cell back: {diff[:3]}")
            f.write(tile)
            ids.append(tid)
            lens.append(len(tile))
    os.replace(tmp, base + ".bin")
    itmp = f"{base}.idx.{os.getpid()}.tmp"
    with open(itmp, "wb") as f:
        np.save(f, np.array([ids, lens], np.uint64).reshape(2, -1))
    os.replace(itmp, base + ".idx")
    return unit, len(ids), int(sum(lens)), time.perf_counter() - t0


# --- archive --------------------------------------------------------------------------------------
def write_archive(units, parts, out_path, meta, rects):
    """The archive from the units' parts (in Hilbert order), streamed; returns (tiles, bytes, sha256)."""
    allids, alllens, files = [], [], []
    for u in sorted(units, key=unit_range):
        base = os.path.join(parts, f"{u[0]}_{u[1]}_{u[2]}")
        a = np.load(base + ".idx")
        if a.shape[1]:
            allids.append(a[0])
            alllens.append(a[1])
            files.append((base + ".bin", int(a[1].sum())))
    ids = np.concatenate(allids) if allids else np.zeros(0, np.uint64)
    lens = np.concatenate(alllens) if alllens else np.zeros(0, np.uint64)
    if len(ids) > 1 and not (np.diff(ids.astype(np.int64)) > 0).all():
        raise RuntimeError("tile ids out of order across units")
    meta = dict(meta, tiles=int(len(ids)))
    x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
    x1, y1 = max(r[2] for r in rects), max(r[3] for r in rects)
    w, s = mapcells.CellKey(bnwt.ZOOM, x0, y1).bounds()[:2]
    e, n = mapcells.CellKey(bnwt.ZOOM, x1, y0).bounds()[2:]
    e7 = lambda v: int(round(v * 1e7))  # noqa: E731
    head = fast._ef.bnwt_pmtiles_head(ids, lens, json.dumps(meta, sort_keys=True, separators=(",", ":")).encode(),
                                      bnwt.ZOOM, bnwt.ZOOM, (e7(w), e7(s), e7(e), e7(n)),
                                      (bnwt.ZOOM, e7((w + e) / 2), e7((s + n) / 2)))
    sha = hashlib.sha256()
    tmp = out_path + ".tmp"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(tmp, "wb") as f:
        f.write(head)
        sha.update(head)
        for path, nbytes in files:
            with open(path, "rb") as src:
                got = 0
                while True:
                    b = src.read(1 << 22)
                    if not b:
                        break
                    f.write(b)
                    sha.update(b)
                    got += len(b)
            if got != nbytes:
                raise RuntimeError(f"{path}: {got} bytes, its index says {nbytes}")
    os.replace(tmp, out_path)
    return int(len(ids)), os.path.getsize(out_path), sha.hexdigest()


def attribution_text(archive):
    themes = sorted({overture.LAYERS[ly]["theme"] for ly in bnwt.ARCHIVES[archive]})
    lines = [f"BeamNG.Earth world input tiles (BNWT format {bnwt.FORMAT}), derived from Overture Maps data.", ""]
    for name, sec in SECTIONS.items():
        mine = [t for t in themes if t in sec["themes"]]
        if not mine:
            continue
        lines += [f"[{name}] layers: " + ", ".join(ly for ly in bnwt.ARCHIVES[archive]
                                                    if overture.LAYERS[ly]["theme"] in mine),
                  f"Licence: {sec['licence']} ({sec['url']})", f"Attribution: {overture.ATTRIBUTION}"]
        lines += [f"Sources ({t}): {overture.THEME_LICENCES.get(t, '')}" for t in mine]
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
                    help="where the parquet blocks are kept (its mapdata/raw)")
    ap.add_argument("--jobs", type=int, default=max(1, min(8, (os.cpu_count() or 2) // 2)))
    ap.add_argument("--unit-zoom", type=int, default=UNIT_ZOOM)
    ap.add_argument("--level", type=int, default=bnwt.LEVEL)
    ap.add_argument("--no-verify", action="store_true")
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
    plan = {"archive": a.archive, "release": a.release, "rects": [list(r) for r in rects], "unit_zoom": a.unit_zoom,
            "format": bnwt.FORMAT, "content": bnwt.CONTENT, "level": a.level, "world": bool(a.world)}
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
    done = tiles = nbytes = 0
    jobs = [(u, rects, a.archive, a.release, raw_dir, parts, not a.no_verify, a.level) for u in todo]
    ctx = mp.get_context("spawn")
    for i in range(0, len(jobs), CHUNK_UNITS):
        chunk = jobs[i:i + CHUNK_UNITS]
        if a.jobs <= 1:
            results = map(build_unit, chunk)
        else:
            pool = ctx.Pool(a.jobs)
            results = pool.imap_unordered(build_unit, chunk)
        for u, nt, nb, _s in results:
            done += 1
            tiles += nt
            nbytes += nb
            el = time.perf_counter() - t0
            print(f"\r{done}/{len(todo)} units, {tiles} tiles, {nbytes / 1e6:.1f} MB, {el:.0f} s", end="",
                  file=sys.stderr)
        if a.jobs > 1:
            pool.close()
            pool.join()
    print(file=sys.stderr)
    if a.shard:
        print(json.dumps({"shard": a.shard, "units": len(mine), "tiles": tiles, "bytes": nbytes,
                          "seconds": round(time.perf_counter() - t0, 1)}))
        return 0
    name = f"{a.archive}.c{bnwt.CONTENT}"
    outdir = os.path.join(a.out, f"bnwt{bnwt.FORMAT}", a.release)
    coverage = [list(r) for r in rects]
    meta = {"format": "bnwt", "version": bnwt.FORMAT, "content": bnwt.CONTENT, "release": a.release,
            "archive": a.archive, "layers": list(bnwt.ARCHIVES[a.archive]), "coverage": coverage,
            "zoom": bnwt.ZOOM, "attribution": overture.ATTRIBUTION}
    n, size, sha = write_archive(units, parts, os.path.join(outdir, name + ".pmtiles"), meta, rects)
    with open(os.path.join(outdir, "ATTRIBUTION.txt"), "w", encoding="utf-8") as f:
        f.write(attribution_text(a.archive))
    themes = sorted({overture.LAYERS[ly]["theme"] for ly in bnwt.ARCHIVES[a.archive]})
    manifest = dict(meta, file=name + ".pmtiles", bytes=size, sha256=sha, tiles=n, native=bnwt.NATIVE,
                    files=["ATTRIBUTION.txt"],
                    licences=[dict(section=k, licence=s["licence"], url=s["url"],
                                   themes={t: overture.THEME_LICENCES.get(t, "") for t in themes if t in s["themes"]})
                              for k, s in SECTIONS.items() if any(t in s["themes"] for t in themes)])
    with open(os.path.join(outdir, name + ".manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, sort_keys=True, indent=1)
    print(json.dumps({"archive": os.path.join(outdir, name + ".pmtiles"), "tiles": n, "bytes": size, "sha256": sha,
                      "seconds": round(time.perf_counter() - t0, 1)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
