"""build_osm_building_years.py SOURCE OUT_DIR: OpenStreetMap's construction years of buildings (start_date), from OSM
US's Layercake buildings file, cut into one blob per zoom-12 tile and packed into PMTiles archive parts the server
reads a tile of with one range request (server/osmyears.py), as build_osm_tag_shards.py does for the road tags.

    python3 tools/build_osm_building_years.py https://data.openstreetmap.us/layercake/buildings.parquet out/osmyears
    python3 tools/build_osm_building_years.py buildings.parquet out/osmyears --bbox -74.03,40.69,-73.90,40.89   (tests)

Why (streets.gl comparison 2026-11-14, fix 7): a tower with no mapped material is drawn as a glass curtain wall
(buildings.plan), and pre-war skyscrapers (the Empire State Building, 1931; Chrysler, 1930; Woolworth, 1913) came out
as glass. Overture's buildings carry no year (its schema has none; start_date is one of the OSM tags it drops), but
the building keeps its OSM way or relation id (sources[1].record_id), and Layercake's buildings file has start_date
as a column (17.4 M buildings, 2026-11-14: 20 MB of the 43 GB file).

The year (YEAR_SQL, DuckDB): the first four-digit year 1000-2100 in the value ("1931", "1930-1931", "1976..1991",
"~1900", "July 2023", "1950s"), one less after "before" / "<", one more after "after" / ">"; a century "C19",
"early C20", "late C19" its middle (1850), 15 or 85 into it; anything else (no year in it) left out.

Output, in OUT_DIR:
- <key>/osmyears-000.pmtiles, ...: the tiles in tile-id order in parts of at most --part-bytes each, every part a
  whole PMTiles v3 archive (tile type 0: the blob below, raw deflate; gzip directories; zoom SHARD_ZOOM only;
  clustered). <key> (the build time and a digest of its parts, "20261114T065758Z-1a2b3c4d") is the build's own
  folder: its files never change once published (a server keeps reading the build it started on: osmyears.frozen;
  fixed names let a process frozen on one build read the next one's header and tiles under the old identity, Codex
  review of b1f2499b). Publish the folder before the manifest, and keep the last KEEP_BUILDS folders.
- manifest.json: {"format", "zoom", "built", "key", "source", "source_etag", "parts": [{"archive": "<key>/...",
  "first", "last", "bytes", "tiles", "head_sha256", "sha256"}, ...], "bytes", "tiles", "rows", "license"}
  (osmtagshards.parts_of reads the parts). head_sha256: of the part's first HEAD_BYTES (or all of a shorter part),
  the read a server starts with (header and root directory): one that does not match is not read; sha256: the whole
  part (the publisher's upload check).

A tile holds every dated building whose float32 bbox touches it (a building on a tile edge is in both). Blob (OBY1,
earthfast buildyears.rs): "OBY1", uv n, n x uv key deltas (ascending, one row a key: 2 x the OSM id, + 1 for a
relation), n x uv zz(year - YEAR_BASE).

The file is rewritten in place weekly: its ETag is read before and after (the manifest's source_etag); a change in
between fails the build. Data (c) OpenStreetMap contributors, ODbL 1.0 (Layercake: OSM US, the same licence).
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_osm_tag_shards as ots      # noqa: E402  (the PMTiles writer, tile maths, varints, the source's metadata)

FORMAT = 1
SHARD_ZOOM = 12
PART_BYTES = ots.PART_BYTES
TILE_MAGIC = b"OBY1"
HEAD_BYTES = 16384              # (osmtagshards.HEAD_BYTES: a part's first read)
KEEP_BUILDS = 3                 # build folders a publisher keeps (a running server reads the one it started on)
YEAR_BASE = 1900
SOURCE = "https://data.openstreetmap.us/layercake/buildings.parquet"

# start_date -> a year (NULL: none in it); tests/test_osm_building_years.py checks it on the values OSM has most
_S = "start_date"
_Y = f"TRY_CAST(regexp_extract({_S}, '(?:^|[^0-9])([12][0-9]{{3}})(?:[^0-9]|$)', 1) AS INTEGER)"
_C = f"TRY_CAST(regexp_extract({_S}, '^\\s*(?:early\\s+|mid\\s+|late\\s+)?[Cc]\\s?([0-9]{{1,2}})\\s*(?:\\.\\.|$|[^0-9])', 1) AS INTEGER)"
_RAW = (f"CASE WHEN {_C} BETWEEN 10 AND 21 THEN ({_C} - 1) * 100 + "
        f"CASE WHEN regexp_matches(lower({_S}), '^\\s*early') THEN 15 "
        f"WHEN regexp_matches(lower({_S}), '^\\s*late') THEN 85 ELSE 50 END "
        f"ELSE {_Y} + CASE WHEN regexp_matches(lower({_S}), '^\\s*(before|<)') THEN -1 "
        f"WHEN regexp_matches(lower({_S}), '^\\s*(after|>)') THEN 1 ELSE 0 END END")
YEAR_SQL = f"CASE WHEN ({_RAW}) BETWEEN 1000 AND 2100 THEN ({_RAW}) END"


def key_of(osm_type, osm_id):
    """The blob's key of an OSM way / relation (None: another type)."""
    return 2 * int(osm_id) + (1 if osm_type == "relation" else 0) if osm_type in ("way", "relation") else None


def encode_tile(keys, years):
    """A tile's blob (before deflate) of its rows: keys ascending and unique, years (int)."""
    keys = np.asarray(keys, np.uint64)
    years = np.asarray(years, np.int64)
    d = np.diff(keys, prepend=np.uint64(0)) if len(keys) else keys
    return TILE_MAGIC + ots.uv1(len(keys)) + ots.uv(d) + ots.uv(ots.zz(years - YEAR_BASE))


_CON = None


def decode_group(args):
    """One row group's dated buildings: (rg, keys uint64, years int16, bbox float32 (k, 4)). One DuckDB connection
    per worker process (its footer parsed once: parquet_metadata_cache), as build_osm_tag_shards.decode_group."""
    src, rg, first, nrows, bbox = args
    global _CON
    if _CON is None:
        _CON = ots._connect()
    con = _CON
    box = ""
    if bbox is not None:
        bx0, by0, bx1, by1 = bbox
        box = (f" AND bbox.xmin <= {bx1!r} AND bbox.xmax >= {bx0!r} AND bbox.ymin <= {by1!r} "
               f"AND bbox.ymax >= {by0!r}")
    rows = con.execute(f"""SELECT type, id, {YEAR_SQL} AS year, bbox.xmin, bbox.ymin, bbox.xmax, bbox.ymax
        FROM read_parquet('{src}', file_row_number = true)
        WHERE file_row_number BETWEEN {first} AND {first + nrows - 1} AND start_date IS NOT NULL
        AND type IN ('way', 'relation'){box}""").fetchall()
    keys, years, bb = [], [], []
    for t, i, y, x0, y0, x1, y1 in rows:
        if y is None or i is None or x0 is None:
            continue
        keys.append(key_of(t, i))
        years.append(int(y))
        bb.append((x0, y0, x1, y1))
    return (rg, np.array(keys, np.uint64), np.array(years, np.int16),
            np.array(bb, np.float32).reshape(-1, 4))


def cut(keys, years, bb, z=SHARD_ZOOM, clip=None):
    """{(x, y): (keys ascending unique, years)} of the rows: each in every tile its bbox touches (ots.tile_ranges;
    clip (x0, x1, y0, y1): only those tiles). A key twice in a tile (one building in two row groups): its first row
    in file order."""
    if not len(keys):
        return {}
    tx0, tx1, ty0, ty1 = ots.tile_ranges(bb[:, 0], bb[:, 1], bb[:, 2], bb[:, 3], z)
    if clip is not None:
        tx0, tx1 = np.maximum(tx0, clip[0]), np.minimum(tx1, clip[1])
        ty0, ty1 = np.maximum(ty0, clip[2]), np.minimum(ty1, clip[3])
    nx, ny = np.maximum(tx1 - tx0 + 1, 0), np.maximum(ty1 - ty0 + 1, 0)
    cnt = nx * ny
    row = np.repeat(np.arange(len(keys)), cnt)
    k = np.arange(int(cnt.sum())) - np.repeat(np.cumsum(cnt) - cnt, cnt)
    x = tx0[row] + k % np.maximum(nx[row], 1)
    y = ty0[row] + k // np.maximum(nx[row], 1)
    tile = x.astype(np.int64) * (1 << z) + y
    order = np.lexsort((row, keys[row], tile))      # (by tile, then key, then file order)
    tile, row = tile[order], row[order]
    kk = keys[row]
    first = np.ones(len(row), bool)
    first[1:] = (tile[1:] != tile[:-1]) | (kk[1:] != kk[:-1])
    tile, row, kk = tile[first], row[first], kk[first]
    out = {}
    cuts = np.flatnonzero(np.diff(tile)) + 1
    for s in np.split(np.arange(len(tile)), cuts):
        t = int(tile[s[0]])
        out[(t >> z, t & ((1 << z) - 1))] = (kk[s], years[row[s]])
    return out


def build(src, out, bbox=None, workers=4, log=print, z=SHARD_ZOOM, part_bytes=PART_BYTES):
    """The archive's parts and the manifest in out from src (the Layercake buildings file's URL or path; bbox: only
    that region)."""
    t0 = time.time()
    os.makedirs(out, exist_ok=True)
    etag0 = ots.etag_of(src)
    groups = ots.row_groups(src)
    if bbox is not None:
        bx0, by0, bx1, by1 = bbox
        groups = [g for g in groups if g[3][0] <= bx1 and g[3][2] >= bx0 and g[3][1] <= by1 and g[3][3] >= by0]
    log(f"{len(groups)} row groups, {time.time() - t0:.0f} s")
    clip = None
    if bbox is not None:
        clip = tuple(int(v[0]) for v in ots.tile_ranges(*(np.array([v], np.float32) for v in bbox), z))
    got, n = {}, 0
    jobs = [(src, g[0], g[1], g[2], bbox) for g in groups]
    # (a fresh pool per chunk of DECODE_TASKS row groups a worker: the workers' DuckDB memory back, as the tag build)
    chunk = max(1, workers * ots.DECODE_TASKS)
    for c0 in range(0, len(jobs), chunk):
        with ProcessPoolExecutor(workers) as ex:
            for rg, k, y, b in ex.map(decode_group, jobs[c0:c0 + chunk]):
                got[rg] = (k, y, b)
                n += 1
                if n % 200 == 0 or n == len(jobs):
                    log(f"  read {n}/{len(jobs)} row groups, {time.time() - t0:.0f} s")
    if ots.etag_of(src) != etag0:
        raise SystemExit(f"{src} changed during the build ({etag0} -> {ots.etag_of(src)})")
    rgs = sorted(got)
    keys = np.concatenate([got[r][0] for r in rgs]) if rgs else np.zeros(0, np.uint64)
    years = np.concatenate([got[r][1] for r in rgs]) if rgs else np.zeros(0, np.int16)
    bb = np.concatenate([got[r][2] for r in rgs]) if rgs else np.zeros((0, 4), np.float32)
    del got
    tiles = cut(keys, years, bb, z, clip)
    log(f"{len(keys):,} dated buildings in {len(tiles):,} tiles, {time.time() - t0:.0f} s")
    tmp = tempfile.mkdtemp(prefix="osmyears-build-")
    try:
        data_path = os.path.join(tmp, "tiles.bin")
        entries, at, n_rows = [], 0, 0
        with open(data_path, "wb") as data:
            for (x, y_) in sorted(tiles, key=lambda t: ots.tile_id(z, *t)):
                k, yr = tiles[(x, y_)]
                blob = ots.deflate(encode_tile(k, yr))
                data.write(blob)
                entries.append((ots.tile_id(z, x, y_), at, len(blob), 1))
                at += len(blob)
                n_rows += len(k)
        lo = bbox or (-180.0, -85.0511, 180.0, 85.0511)
        built = time.gmtime()
        manifest = {"format": FORMAT, "zoom": z, "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", built),
                    "source": src if src.startswith("http") else os.path.basename(src), "source_etag": etag0}
        work = os.path.join(out, ".parts-tmp")
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work)
        parts = ots.write_parts(work, entries, data_path, lo, n_rows, z, 16384 - 127, part_bytes, prefix="osmyears",
                                title="BeamNG.Earth OSM building years", fmt=f"oby{FORMAT}")
        digest = hashlib.sha256()
        for p in parts:
            with open(os.path.join(work, p["archive"]), "rb") as f:
                b = f.read()
            p["head_sha256"] = hashlib.sha256(b[:HEAD_BYTES]).hexdigest()
            p["sha256"] = hashlib.sha256(b).hexdigest()
            digest.update(p["sha256"].encode())
        key = f"{time.strftime('%Y%m%dT%H%M%SZ', built)}-{digest.hexdigest()[:8]}"
        os.replace(work, os.path.join(out, key))
        for p in parts:
            p["archive"] = f"{key}/{p['archive']}"
        manifest["key"] = key
        manifest.update(parts=parts, bytes=sum(p["bytes"] for p in parts), tiles=len(entries), rows=n_rows,
                        license="ODbL 1.0, (c) OpenStreetMap contributors")
        if bbox is not None:
            manifest["bbox"] = list(bbox)
        with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        log(f"{len(entries):,} tiles, {n_rows:,} rows, {manifest['bytes'] / 1e6:.1f} MB in {len(parts)} part(s), "
            f"{time.time() - t0:.0f} s")
        return manifest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", nargs="?", default=SOURCE, help="the Layercake buildings GeoParquet (URL or path)")
    ap.add_argument("out")
    ap.add_argument("--bbox", help="lon0,lat0,lon1,lat1: only this region (tests)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--part-bytes", type=int, default=PART_BYTES, help="an archive part at most (bytes)")
    a = ap.parse_args()
    bbox = tuple(float(v) for v in a.bbox.split(",")) if a.bbox else None
    build(a.source, a.out, bbox, a.workers, log=lambda s: print(s, flush=True), part_bytes=a.part_bytes)


if __name__ == "__main__":
    sys.exit(main())
