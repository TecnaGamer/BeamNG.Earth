"""build_osm_tag_shards.py SOURCE OUT_DIR: OpenStreetMap's road tags (lanes, width, speed limit, sidewalk, surface...),
the very rows server/osmtags.py reads from OSM US's Layercake highways file, cut into one blob per zoom-12 tile and
packed into one PMTiles v3 archive the server reads a tile of with one range request (server/osmtagshards.py).

    python3 tools/build_osm_tag_shards.py https://data.openstreetmap.us/layercake/highways.parquet out/osmtags
    python3 tools/build_osm_tag_shards.py highways.parquet out/osmtags --bbox -112,35,-111,36     (a region: tests)

Why (2026-11-11, "sub-second world gen, even with clear cache"): a fresh place's roads waited 8-10 s for their tags
from the 29 GB parquet file: its 9.4 MB footer (the row groups' statistics and offsets) read and parsed by DuckDB in
every process, then ~2.6 MB of tag columns per 123k-row group (1-5 groups a place) and, where a raceway or busway was
near, the group's whole geometry column (14-19 MB). A tile of this archive is ~30-350 KB (a fresh cache: the
manifest, the archive's head, a leaf directory, the tiles; the first three shared by everything around).

Output, in OUT_DIR:
- osmtags.pmtiles: tile type 0 (our own blob, below, raw deflate), gzip directories, zoom SHARD_ZOOM only, tiles in
  tile-id order (clustered).
- manifest.json: {"format", "zoom", "built", "source", "source_etag", "archive": "osmtags.pmtiles", "bytes", "tiles",
  "rows", "schema", "license"}. The server reads it from the data repo's GitHub Pages (<pages>/osmtags/); "archive"
  is made an absolute URL when the archive is hosted elsewhere (the planet's is ~2.5 GB: Pages sites stop at 1 GB).
  "schema": the blob's columns in order (schema()); the server decodes by it and refuses one without osmtags' columns.

A tile holds every row of the Layercake file whose float32 bbox touches the tile (the test osmtags.py makes per cell:
a tile's cells' tests at once, their edges being the same doubles), in file order, of the rows osmtags decodes (a tag
the roads use, or highway=raceway / busway / construction / proposed), with osmtags.COLUMNS' values computed by the
same SQL (DuckDB), plus:
- "lines": the raceways' and busways' raw RACEWAY_COLUMNS and geometry, those of the tile and those inside the bbox of
  a tile's raceway that may be a facility's outline (osmtags._maybe_outline): the server decides the outlines as
  before (osmtags.is_outline).
- "inside": per such raceway, the file rows of the raceways inside its bbox (osmtags._inside_rows, file-wide).
- "geom": the geometry of the rows with a lane count and a motorway / trunk class (osmtags.highway_geometry).
Blob (OTG1, little endian, uv = LEB128 varint, zz = zig-zag): "OTG1", uv n; frn n x uv delta; id n x uv zz(delta);
flags n x u8 (1 raceway, 2 busway); bbox: bx0, by0, bx1, by1 each n x uv zz(delta of the float32's ordered bits); per
numeric column uv nnz, nnz x uv row delta, nnz raw float32 / float64; per text column uv ndict, ndict x (uv length,
UTF-8), uv nnz, nnz x uv row delta, nnz x uv dict index; lines: uv m, m x (uv frn, uv zz(id), per line column uv
length + 1 (0: NULL) and UTF-8, coords); inside: uv m, m x (uv frn, uv j, j x uv frn); geom: uv m, m x (uv frn,
coords). coords: u8 mode, uv count; mode 1: lon / lat x 1e7 as zz deltas (exact: each value is that integer / 1e7 as
a double), mode 0: raw float64 pairs.

Cost (2026-11-11, from the PC, 4 workers): ~3.6 s a row group decoded (its tag columns and geometry, ~16 MB), ~30 us a
row cut and encoded: the planet's 2,477 groups ~40 min + ~20 min, ~11 GB of temporary rows (--tmp), ~2.5 GB out
(12-24 B a row, ~150 M rows).

The file is rewritten in place weekly: its ETag is read before and after (and is the manifest's source_etag); a change
in between fails the build. Data (c) OpenStreetMap contributors, ODbL 1.0.
"""
import argparse
import gzip
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import time
import urllib.request
import zlib
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor

import numpy as np

FORMAT = 1
SHARD_ZOOM = 12
TILE_MAGIC = b"OTG1"

# --- osmtags.py's query (tests/test_osm_tag_shards.py keeps these equal to server/osmtags.py's) ---------------------
_NUM = r"'^\s*([0-9]+(?:\.[0-9]+)?)'"
COLUMNS = {
    "lanes": "TRY_CAST(regexp_extract(lanes, '^\\s*([0-9]+)\\s*$', 1) AS INTEGER)",
    "width": (f"TRY_CAST(regexp_extract(width, {_NUM}, 1) AS DOUBLE) * "
              "CASE WHEN width LIKE '%ft%' OR width LIKE '%''%' THEN 0.3048 ELSE 1.0 END"),
    "maxspeed": (f"TRY_CAST(regexp_extract(maxspeed, {_NUM}, 1) AS DOUBLE) * "
                 "CASE WHEN maxspeed LIKE '%mph%' THEN 1.609344 ELSE 1.0 END"),
    "sidewalk": "sidewalk",
    "lit": "lit",
    "smoothness": "smoothness",
    "osm_surface": "surface",
    "osm_oneway": "oneway",
    "osm_layer": "TRY_CAST(layer AS INTEGER)",
    "name": "name",
    "osm_highway": "CASE WHEN highway IN ('construction', 'proposed') THEN highway END",
    "lane_hw": "CASE WHEN highway IN ('motorway', 'trunk', 'motorway_link', 'trunk_link') THEN highway END",
    "bx0": "bbox.xmin", "by0": "bbox.ymin", "bx1": "bbox.xmax", "by1": "bbox.ymax",
}
TEXT_COLUMNS = {"sidewalk", "lit", "smoothness", "osm_surface", "osm_oneway", "name", "osm_highway", "lane_hw"}
EXACT32 = {"bx0", "by0", "bx1", "by1", "lanes", "osm_layer"}
USED = {"id", "bbox", "lanes", "width", "maxspeed", "sidewalk", "lit", "smoothness", "surface", "oneway", "layer", "name"}
UNBUILT = ("construction", "proposed")
RACEWAY_COLUMNS = ("id", "highway", "name", "surface", "width", "lanes", "layer", "bridge", "tunnel", "oneway")
# ---------------------------------------------------------------------------------------------------------------------

BOX = ("bx0", "by0", "bx1", "by1")
NUMERIC = [c for c in COLUMNS if c not in TEXT_COLUMNS and c not in BOX]          # lanes, width, maxspeed, osm_layer
TEXTS = [c for c in COLUMNS if c in TEXT_COLUMNS]
LINE_COLS = RACEWAY_COLUMNS[1:]


# ------------------------------------------------------------------------------------------------ tiles and cells
def lat_of(yf, n):
    """mapcells._lat_of: the latitude of tile row yf (0 = north edge) at n tiles per axis."""
    return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * yf / n))))


def tile_bounds32(z, x, y):
    """(lon0, lat0, lon1, lat1) of tile z/x/y as mapcells.CellKey.bounds, as float32 (osmtags' comparisons)."""
    n = 1 << z
    b = (x / n * 360.0 - 180.0, lat_of(y + 1, n), (x + 1) / n * 360.0 - 180.0, lat_of(y, n))
    return tuple(np.float32(v) for v in b)


def _edges32(z):
    """float32 tile edges at zoom z: (lon[x] for x in 0..n, lat[y] for y in 0..n), lat decreasing."""
    n = 1 << z
    lon = np.array([x / n * 360.0 - 180.0 for x in range(n + 1)], np.float64).astype(np.float32)
    lat = np.array([lat_of(y, n) for y in range(n + 1)], np.float64).astype(np.float32)
    return lon, lat


_EDGES = {}


def tile_ranges(bx0, by0, bx1, by1, z=SHARD_ZOOM):
    """Per row (float32 bbox arrays) the zoom-z tiles its bbox touches as osmtags tests a cell: (x0, x1, y0, y1)
    inclusive, x1 < x0 where none. A tile x is touched when bx0 <= lon[x + 1] and bx1 >= lon[x]; a row y when
    by0 <= lat[y] and by1 >= lat[y + 1] (all float32)."""
    if z not in _EDGES:
        _EDGES[z] = _edges32(z)
    lon, lat = _EDGES[z]
    n = 1 << z
    # x: the first x with lon[x + 1] >= bx0, the last with lon[x] <= bx1
    x0 = np.searchsorted(lon[1:], bx0, side="left")
    x1 = np.searchsorted(lon[:-1], bx1, side="right") - 1
    # y (lat decreasing in y): the first y with by1 >= lat[y + 1], the last with by0 <= lat[y]
    neg = -lat                                                  # increasing
    y0 = np.searchsorted(neg[1:], -by1, side="left")
    y1 = np.searchsorted(neg[:-1], -by0, side="right") - 1
    x0, x1 = np.clip(x0, 0, n - 1), np.clip(x1, -1, n - 1)
    y0, y1 = np.clip(y0, 0, n - 1), np.clip(y1, -1, n - 1)
    return x0.astype(np.int64), x1.astype(np.int64), y0.astype(np.int64), y1.astype(np.int64)


def tile_id(z, x, y):
    """The PMTiles tile id of z/x/y (the lower zooms' tiles first, then the Hilbert curve's index at z)."""
    acc = sum(4 ** t for t in range(z))
    n = 1 << z
    d, s = 0, n // 2
    while s > 0:
        rx = 1 if x & s else 0
        ry = 1 if y & s else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x, y = s - 1 - x, s - 1 - y
            x, y = y, x
        s //= 2
    return acc + d


# ------------------------------------------------------------------------------------------------ encoding
def _uv_py(v, out):
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)


def uv(a):
    """LEB128 varints of the non-negative integers a (numpy or a list), concatenated."""
    a = np.asarray(a, np.uint64).ravel()
    if len(a) < 48:                     # (numpy's per-call cost is most of a short column's)
        out = bytearray()
        for v in a.tolist():
            _uv_py(v, out)
        return bytes(out)
    nb = np.ones(len(a), np.int64)
    for k in range(1, 10):
        nb += a >= np.uint64(1 << (7 * k))
    pos = np.concatenate([[0], np.cumsum(nb)[:-1]])
    out = np.zeros(int(nb.sum()), np.uint8)
    for k in range(int(nb.max())):
        sel = nb > k
        byte = ((a[sel] >> np.uint64(7 * k)) & np.uint64(0x7F)).astype(np.uint8)
        byte |= np.where(nb[sel] > k + 1, 0x80, 0).astype(np.uint8)
        out[pos[sel] + k] = byte
    return out.tobytes()


def uv1(v):
    out = bytearray()
    _uv_py(int(v), out)
    return bytes(out)


def zz(a):
    """Zig-zag of int64 values (as uint64)."""
    a = np.asarray(a, np.int64)
    return ((a << 1) ^ (a >> 63)).astype(np.uint64)


def zz1(v):
    v = int(v)
    return (v << 1) ^ (v >> 63) if v < 0 else v << 1


def name(s):
    b = s.encode("utf-8")
    return uv1(len(b)) + b


def ordered32(f):
    """float32 values' bits as int64 keys in the floats' order (negative floats' low 31 bits flipped)."""
    b = np.asarray(f, np.float32).view(np.int32).astype(np.int64)
    return np.where(b < 0, b ^ 0x7FFFFFFF, b)


def deltas(a):
    a = np.asarray(a, np.int64)
    return np.diff(a, prepend=np.int64(0)) if len(a) else a


def coords_bytes(c):
    """A line's coords: mode 1 (x 1e7 integer deltas) when every value is exactly its integer / 1e7, else raw."""
    c = np.ascontiguousarray(c, np.float64).reshape(-1, 2)
    q = np.round(c * 1e7)
    if len(c) and np.all(np.abs(q) < 2 ** 52) and np.array_equal(q / 1e7, c):
        q = q.astype(np.int64)
        d = np.diff(q, axis=0, prepend=np.zeros((1, 2), np.int64))
        return b"\x01" + uv1(len(c)) + uv(zz(d.ravel()))
    return b"\x00" + uv1(len(c)) + c.astype("<f8").tobytes()


def linestring(wkb):
    """osmtags._linestring: (k, 2) lon/lat of a WKB LineString (2D, either byte order), else None."""
    if wkb is None or len(wkb) < 9:
        return None
    end = "<" if wkb[0] == 1 else ">"
    typ = struct.unpack_from(end + "I", wkb, 1)[0]
    if typ % 1000 != 2 or typ >= 1000 or typ & 0x20000000:
        return None
    n = struct.unpack_from(end + "I", wkb, 5)[0]
    if 9 + 16 * n > len(wkb):
        return None
    return np.frombuffer(wkb, dtype=end + "f8", count=2 * n, offset=9).reshape(n, 2).astype(np.float64)


def text_column(v):
    """(dictionary [str], rows with a value (int64), their dictionary index (uint64)) of a text column given as a
    list of str / None, or already so (a tuple)."""
    if isinstance(v, tuple):
        return v
    nz = [i for i, s in enumerate(v) if s is not None]
    uniq = sorted({v[i] for i in nz})
    at = {s: k for k, s in enumerate(uniq)}
    return uniq, np.array(nz, np.int64), np.array([at[v[i]] for i in nz], np.uint64)


def schema():
    """The blob's columns, in their order (the manifest's "schema": the server's decoder takes them from it)."""
    return {"numeric": [[c, 4 if c in EXACT32 else 8] for c in NUMERIC], "text": list(TEXTS),
            "lines": list(LINE_COLS)}


def encode_tile(rows, lines, inside, geom):
    """One tile's blob (before deflate). rows: {"frn", "id", "race", "bus", COLUMNS...} arrays in file order (texts as
    lists of str / None, or text_column's tuple); lines: {frn: (id, [LINE_COLS values], coords)}; inside: {frn:
    [frns]}; geom: {frn: coords}. The columns in schema()'s order."""
    n = len(rows["frn"])
    out = [TILE_MAGIC, uv1(n)]
    out.append(uv(deltas(rows["frn"])))
    out.append(uv(zz(deltas(rows["id"]))))
    out.append((np.asarray(rows["race"], np.uint8) | (np.asarray(rows["bus"], np.uint8) << 1)).astype(np.uint8)
               .tobytes())
    for c in BOX:
        out.append(uv(zz(deltas(ordered32(rows[c])))))
    for c in NUMERIC:
        v = np.asarray(rows[c], np.float32 if c in EXACT32 else np.float64)
        nz = np.flatnonzero(~np.isnan(v))
        out += [uv1(len(nz)), uv(deltas(nz)), v[nz].astype("<f4" if c in EXACT32 else "<f8").tobytes()]
    for c in TEXTS:
        uniq, nz, codes = text_column(rows[c])
        out.append(uv1(len(uniq)))
        out += [name(s) for s in uniq]
        out += [uv1(len(nz)), uv(deltas(nz)), uv(codes)]
    out.append(uv1(len(lines)))
    for f in sorted(lines):
        wid, vals, coords = lines[f]
        out += [uv1(f), uv1(zz1(wid))]
        for v in vals:
            if v is None:
                out.append(b"\x00")
            else:
                b = str(v).encode("utf-8")
                out += [uv1(len(b) + 1), b]
        out.append(coords_bytes(coords))
    out.append(uv1(len(inside)))
    for f in sorted(inside):
        out += [uv1(f), uv1(len(inside[f])), uv(np.array(inside[f], np.uint64))]
    out.append(uv1(len(geom)))
    for f in sorted(geom):
        out += [uv1(f), coords_bytes(geom[f])]
    return b"".join(out)


def deflate(b):
    """A tile as stored: raw deflate (no gzip header: ~20 B of a few-hundred-byte rural tile)."""
    c = zlib.compressobj(6, zlib.DEFLATED, -15)
    return c.compress(b) + c.flush()


# ------------------------------------------------------------------------------------------------ PMTiles writer
def _dir_bytes(entries):
    """entries [(tile id, offset, length, run)] sorted -> a gzip directory."""
    t = np.array([e[0] for e in entries], np.int64)
    off = [e[1] for e in entries]
    ln = [e[2] for e in entries]
    offs = [0 if k > 0 and off[k] == off[k - 1] + ln[k - 1] else off[k] + 1 for k in range(len(entries))]
    raw = (uv1(len(entries)) + uv(deltas(t)) + uv(np.array([e[3] for e in entries], np.uint64))
           + uv(np.array(ln, np.uint64)) + uv(np.array(offs, np.uint64)))
    return gzip.compress(raw, 9, mtime=0)


def directories(entries, root_max=16384 - 127):
    """(root, leaves) of the tile entries: all in the root when it fits, else leaves of a size that lets it."""
    root = _dir_bytes(entries)
    if len(root) <= root_max:
        return root, b""
    size = 4096
    while True:
        leaves, ptrs = [], []
        at = 0
        for i in range(0, len(entries), size):
            chunk = _dir_bytes(entries[i:i + size])
            ptrs.append((entries[i][0], at, len(chunk), 0))
            leaves.append(chunk)
            at += len(chunk)
        root = _dir_bytes(ptrs)
        if len(root) <= root_max:
            return root, b"".join(leaves)
        size *= 2


def write_pmtiles(path, entries, data_path, bounds, n_rows, z=SHARD_ZOOM, root_max=16384 - 127):
    """The archive at path: header, root directory, metadata, leaves, then the tile data (data_path's bytes)."""
    root, leaves = directories(entries, root_max)
    meta = gzip.compress(json.dumps({"name": "BeamNG.Earth OSM road tags", "format": f"otg{FORMAT}",
                                     "rows": n_rows, "attribution": "(c) OpenStreetMap contributors, ODbL 1.0"}
                                    ).encode(), 9, mtime=0)
    data_len = os.path.getsize(data_path)
    root_off = 127
    meta_off = root_off + len(root)
    leaf_off = meta_off + len(meta)
    data_off = leaf_off + len(leaves)
    lon0, lat0, lon1, lat1 = bounds
    h = bytearray(b"PMTiles\x03")
    h += struct.pack("<QQQQQQQQ", root_off, len(root), meta_off, len(meta), leaf_off, len(leaves), data_off, data_len)
    h += struct.pack("<QQQ", len(entries), len(entries), len(entries))
    h += bytes([1, 2, 1, 0, z, z])          # clustered, gzip directories, tiles as stored (raw deflate), type 0
    h += struct.pack("<iiii", *(int(round(v * 1e7)) for v in (lon0, lat0, lon1, lat1)))
    h += bytes([z]) + struct.pack("<ii", int(round((lon0 + lon1) / 2 * 1e7)),
                                          int(round((lat0 + lat1) / 2 * 1e7)))
    assert len(h) == 127, len(h)
    with open(path + ".part", "wb") as f:
        f.write(h)
        f.write(root)
        f.write(meta)
        f.write(leaves)
        with open(data_path, "rb") as d:
            shutil.copyfileobj(d, f, 1 << 22)
    os.replace(path + ".part", path)


# ------------------------------------------------------------------------------------------------ the source
_CON = None


def _connect(threads=2):
    import duckdb
    con = duckdb.connect(config={"threads": threads})
    for sql in ("LOAD httpfs", "SET enable_http_metadata_cache = true", "SET parquet_metadata_cache = true",
                "SET enable_geoparquet_conversion = false"):
        try:
            con.execute(sql)
        except Exception:
            pass
    return con


def etag_of(src):
    if not src.startswith(("http://", "https://")):
        st = os.stat(src)
        return f"{st.st_size}-{int(st.st_mtime)}"
    req = urllib.request.Request(src, method="HEAD", headers={"User-Agent": "BeamNG.Earth osmtags build"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return (r.headers.get("ETag") or "").strip()


def row_groups(src):
    """[(row group, first row, rows, (xmin, ymin, xmax, ymax) of its bbox statistics)] of the file."""
    con = _connect()
    meta = con.execute(f"""SELECT row_group_id, row_group_num_rows, path_in_schema, stats_min, stats_max
        FROM parquet_metadata('{src}') ORDER BY row_group_id, column_id""").fetchall()
    out, first, cur = [], 0, None
    for rg, nrows, col, smin, smax in meta:
        if cur is None or cur[0] != rg:
            if cur is not None:
                out.append(cur)
            cur = [rg, first, nrows, {}]
            first += nrows
        parts = col.split(", ")
        if parts[0] == "bbox":
            try:
                cur[3][parts[-1]] = (float(smin), float(smax))
            except (TypeError, ValueError):
                pass
    if cur is not None:
        out.append(cur)
    res = []
    for rg, first, nrows, st in out:
        try:
            box = (st["xmin"][0], st["ymin"][0], st["xmax"][1], st["ymax"][1])
        except KeyError:
            box = (-1e9, -1e9, 1e9, 1e9)
        res.append((rg, first, nrows, box))
    return res


def decode_group(args):
    """One row group's rows (osmtags._decode's query, plus the raceways' raw columns and the geometry the tiles keep),
    saved to tmp/<rg>.npz; returns (rg, rows, path)."""
    src, rg, first, nrows, tmp, z, cz, clip = args
    global _CON
    if _CON is None:
        _CON = _connect()
    cols = ", ".join(f"{expr} AS \"{c}\"" for c, expr in COLUMNS.items())
    some = " OR ".join([f"{c} IS NOT NULL" for c in sorted(USED - {"id", "bbox"})]
                       + [f"highway IN ({', '.join(repr(v) for v in UNBUILT)})"])
    lanes, hw = COLUMNS["lanes"], COLUMNS["lane_hw"]
    raw = ", ".join(f"CASE WHEN highway IN ('raceway', 'busway') THEN \"{c}\" END" for c in LINE_COLS)
    got = _CON.execute(f"""SELECT id, highway = 'raceway', highway = 'busway', file_row_number, {cols}, {raw},
        CASE WHEN highway IN ('raceway', 'busway') OR (({lanes}) IS NOT NULL AND ({hw}) IS NOT NULL)
             THEN geometry END
        FROM read_parquet('{src}', file_row_number = true)
        WHERE file_row_number BETWEEN {first} AND {first + nrows - 1}
        AND (highway = 'raceway' OR highway = 'busway' OR {some})
        ORDER BY file_row_number""").fetchall()
    names = list(COLUMNS)
    t = {"id": np.array([r[0] for r in got], np.int64), "race": np.array([bool(r[1]) for r in got], bool),
         "bus": np.array([bool(r[2]) for r in got], bool), "frn": np.array([r[3] for r in got], np.int64)}
    for j, c in enumerate(names, 4):
        vals = [r[j] for r in got]
        if c in TEXT_COLUMNS:
            t[c] = np.array(["\0" if v is None else str(v) for v in vals], object)
            t[c + "_ok"] = np.array([v is not None for v in vals], bool)
        else:
            t[c] = np.array([np.nan if v is None else float(v) for v in vals],
                            np.float32 if c in EXACT32 else np.float64)
    j0 = 4 + len(names)
    lines = {}
    for r in got:
        if r[1] or r[2]:
            for v in r[j0:j0 + len(LINE_COLS)]:
                assert v is None or isinstance(v, str), (rg, r[3], v)
            lines[int(r[3])] = list(r[j0:j0 + len(LINE_COLS)])
    geo_frn, geo_off, geo_xy = [], [0], []
    for r in got:
        c = linestring(bytes(r[-1])) if r[-1] is not None else None
        if c is not None:
            geo_frn.append(int(r[3]))
            geo_xy.append(c)
            geo_off.append(geo_off[-1] + len(c))
    t["geo_frn"] = np.array(geo_frn, np.int64)
    t["geo_off"] = np.array(geo_off, np.int64)
    t["geo_xy"] = np.concatenate(geo_xy) if geo_xy else np.zeros((0, 2))
    lf = sorted(lines)
    t["line_frn"] = np.array(lf, np.int64)
    for k, c in enumerate(LINE_COLS):
        vals = [lines[f][k] for f in lf]
        t["line_" + c] = np.array(["\0" if v is None else v for v in vals], object)
        t["line_" + c + "_ok"] = np.array([v is not None for v in vals], bool)
    path = os.path.join(tmp, f"{rg}.npy")
    np.save(path, t, allow_pickle=True)
    # the coarse areas (zoom cz) its rows reach, and its raceways' boxes (the file-wide inside test)
    t = _prepare(t, z, clip)
    sh = z - cz
    x0, x1, y0, y1 = t["_r"]
    sel = np.flatnonzero(t["_ok"])
    a0, a1, b0, b1 = x0[sel] >> sh, x1[sel] >> sh, y0[sel] >> sh, y1[sel] >> sh
    one = (a0 == a1) & (b0 == b1)
    areas = set(zip(a0[one].tolist(), b0[one].tolist()))
    for i in np.flatnonzero(~one):
        areas.update((cx, cy) for cx in range(int(a0[i]), int(a1[i]) + 1) for cy in range(int(b0[i]), int(b1[i]) + 1))
    races = [(int(t["frn"][i]), float(t["bx0"][i]), float(t["by0"][i]), float(t["bx1"][i]), float(t["by1"][i]), rg)
             for i in np.flatnonzero(t["race"])]
    return rg, len(t["id"]), path, areas, races


def _load(path):
    return np.load(path, allow_pickle=True).item()


# ------------------------------------------------------------------------------------------------ the build
def _prepare(t, z, clip=None):
    """A row group's decoded rows made ready for cutting into tiles: each row's tile ranges (_r; clip (x0, x1, y0,
    y1): only those tiles), which rows reach a tile (_ok), the rows the lines / geometry sections look at (_special),
    each text column as codes into its distinct values (_codes), and where a row's line and geometry are."""
    x0, x1, y0, y1 = tile_ranges(t["bx0"], t["by0"], t["bx1"], t["by1"], z)
    if clip is not None:
        x0, x1 = np.maximum(x0, clip[0]), np.minimum(x1, clip[1])
        y0, y1 = np.maximum(y0, clip[2]), np.minimum(y1, clip[3])
    t["_r"] = (x0, x1, y0, y1)
    t["_ok"] = ~(np.isnan(t["bx0"]) | np.isnan(t["by0"]) | np.isnan(t["bx1"]) | np.isnan(t["by1"])) \
        & (x1 >= x0) & (y1 >= y0)
    t["_special"] = t["race"] | t["bus"] | (~np.isnan(t["lanes"]) & t["lane_hw_ok"])
    t["_codes"] = {}
    for c in TEXTS:
        uniq, inv = np.unique(t[c].astype(str), return_inverse=True) if len(t[c]) else (np.zeros(0, str),
                                                                                         np.zeros(0, np.int64))
        t["_codes"][c] = (uniq, inv.astype(np.int64))
    t["_geo"] = {int(f): k for k, f in enumerate(t["geo_frn"])}
    t["_line"] = {int(f): k for k, f in enumerate(t["line_frn"])}
    return t


def _line(t, f):
    """(way id, LINE_COLS values, coords) of raceway / busway file row f of prepared table t, None without a line
    (osmtags: a geometry that is not a LineString of two points or more is left out)."""
    k, g = t["_line"].get(f), t["_geo"].get(f)
    if k is None or g is None:
        return None
    c = t["geo_xy"][t["geo_off"][g]:t["geo_off"][g + 1]]
    if len(c) < 2:
        return None
    vals = [str(t["line_" + c_][k]) if t["line_" + c_ + "_ok"][k] else None for c_ in LINE_COLS]
    return int(t["id"][np.searchsorted(t["frn"], f)]), vals, c


def _tile(parts, table, inside_of, race_rg):
    """One tile's (rows, lines, inside, geom) for encode_tile from parts [(row group, row indices)] (row groups in
    file order, rows ascending: the rows come out in file order)."""
    cat = {c: np.concatenate([table(rg)[c][idx] for rg, idx in parts])
           for c in ("frn", "id", "race", "bus") + BOX + tuple(NUMERIC)}
    assert np.all(np.diff(cat["frn"]) > 0)
    for c in TEXTS:
        used = []
        for rg, idx in parts:
            t = table(rg)
            ok = t[c + "_ok"][idx]
            uniq, inv = t["_codes"][c]
            used.append((uniq, inv[idx][ok], np.flatnonzero(ok)))
        words = sorted({str(s) for uniq, codes, _ in used for s in uniq[np.unique(codes)]})
        at = {s: k for k, s in enumerate(words)}
        nz, codes, base = [], [], 0
        for (uniq, cd, pos), (rg, idx) in zip(used, parts):
            look = np.full(len(uniq), -1, np.int64)
            u = np.unique(cd)
            look[u] = [at[str(s)] for s in uniq[u]]
            nz.append(pos + base)
            codes.append(look[cd])
            base += len(idx)
        cat[c] = (words, np.concatenate(nz).astype(np.int64) if nz else np.zeros(0, np.int64),
                  np.concatenate(codes).astype(np.uint64) if codes else np.zeros(0, np.uint64))
    lines, inside, geom = {}, {}, {}
    for rg, idx in parts:
        t = table(rg)
        for i in idx[t["_special"][idx]].tolist():
            f = int(t["frn"][i])
            if t["race"][i] or t["bus"][i]:
                got = _line(t, f)
                if got is not None:
                    lines[f] = got
            # osmtags._maybe_outline: a raceway without surface, width, lanes or oneway
            if t["race"][i] and not t["osm_surface_ok"][i] and not t["osm_oneway_ok"][i] \
                    and np.isnan(t["width"][i]) and np.isnan(t["lanes"][i]):
                inside[f] = inside_of.get(f, [])
                for g in inside[f]:
                    if g not in lines:
                        got = _line(table(race_rg[g]), g)
                        if got is not None:
                            lines[g] = got
            if not np.isnan(t["lanes"][i]) and t["lane_hw_ok"][i]:
                gk = t["_geo"].get(f)
                if gk is not None:
                    geom[f] = t["geo_xy"][t["geo_off"][gk]:t["geo_off"][gk + 1]]
    return cat, lines, inside, geom


_G = {}            # a cutting worker's shared state (_init_cut): paths, zoom, clip, inside_of, race_rg, its table cache


def _init_cut(paths, z, cz, clip, inside_of, race_rg):
    _G.update(paths=paths, z=z, cz=cz, clip=clip, inside_of=inside_of, race_rg=race_rg, cache=OrderedDict())


def _table(rg):
    cache = _G["cache"]
    got = cache.get(rg)
    if got is None:
        got = cache[rg] = _prepare(_load(_G["paths"][rg]), _G["z"], _G["clip"])
        while len(cache) > 24:
            cache.popitem(last=False)
    else:
        cache.move_to_end(rg)
    return got


def cut_area(args):
    """The tiles of coarse area (cx, cy) (zoom cz) from its row groups, written to out: ([(tile id, offset in out,
    length)] in tile-id order, rows)."""
    cx, cy, rgs, out = args
    z, cz = _G["z"], _G["cz"]
    sh = z - cz
    per = {}             # tile -> [(rg, row indices)], the row groups in file order
    for rg in sorted(rgs):
        t = _table(rg)
        x0, x1, y0, y1 = t["_r"]
        sel = np.flatnonzero(t["_ok"] & ((x0 >> sh) <= cx) & ((x1 >> sh) >= cx)
                             & ((y0 >> sh) <= cy) & ((y1 >> sh) >= cy))
        tx0 = np.maximum(x0[sel], cx << sh)
        tx1 = np.minimum(x1[sel], ((cx + 1) << sh) - 1)
        ty0 = np.maximum(y0[sel], cy << sh)
        ty1 = np.minimum(y1[sel], ((cy + 1) << sh) - 1)
        nx = np.maximum(tx1 - tx0 + 1, 0)
        cnt = nx * np.maximum(ty1 - ty0 + 1, 0)
        if not cnt.sum():
            continue
        rows = np.repeat(sel, cnt)
        k = np.arange(int(cnt.sum())) - np.repeat(np.cumsum(cnt) - cnt, cnt)
        nxr = np.repeat(nx, cnt)
        xs = np.repeat(tx0, cnt) + k % nxr
        ys = np.repeat(ty0, cnt) + k // nxr
        order = np.lexsort((rows, ys, xs))
        xs, ys, rows = xs[order], ys[order], rows[order]
        cut = np.flatnonzero((np.diff(xs) != 0) | (np.diff(ys) != 0)) + 1
        for a, b in zip(np.r_[0, cut], np.r_[cut, len(xs)]):
            per.setdefault((int(xs[a]), int(ys[a])), []).append((rg, rows[a:b]))
    entries, at, n_rows = [], 0, 0
    with open(out, "wb") as data:
        for (tx, ty) in sorted(per, key=lambda k: tile_id(z, *k)):
            cat, lines, inside, geom = _tile(per[(tx, ty)], _table, _G["inside_of"], _G["race_rg"])
            blob = deflate(encode_tile(cat, lines, inside, geom))
            data.write(blob)
            entries.append((tile_id(z, tx, ty), at, len(blob)))
            at += len(blob)
            n_rows += len(cat["frn"])
    return entries, n_rows


def build(src, out, bbox=None, workers=4, tmp=None, log=print, z=SHARD_ZOOM, root_max=16384 - 127):
    """The archive and manifest in out from src (the Layercake file's URL or path; bbox: only that region)."""
    t0 = time.time()
    os.makedirs(out, exist_ok=True)
    etag0 = etag_of(src)
    groups = row_groups(src)
    if bbox is not None:
        bx0, by0, bx1, by1 = bbox
        groups = [g for g in groups if g[3][0] <= bx1 and g[3][2] >= bx0 and g[3][1] <= by1 and g[3][3] >= by0]
    log(f"{len(groups)} row groups, {time.time() - t0:.0f} s")
    own_tmp = tmp is None
    tmp = tmp or tempfile.mkdtemp(prefix="osmtags-build-")
    os.makedirs(tmp, exist_ok=True)
    try:
        # each row group decoded (its rows saved), with the coarse areas (zoom cz) its rows reach and its raceways
        cz = min(6, z)
        clip = None
        if bbox is not None:
            clip = tuple(int(v[0]) for v in tile_ranges(*(np.array([v], np.float32) for v in bbox), z))
        paths, done, coarse, races = {}, 0, {}, []
        jobs = [(src, rg, first, n, tmp, z, cz, clip) for rg, first, n, _ in groups]
        with ProcessPoolExecutor(workers) as ex:
            for rg, n, path, areas, rr in ex.map(decode_group, jobs):
                paths[rg] = path
                for a in areas:
                    coarse.setdefault(a, set()).add(rg)
                races += rr
                done += 1
                if done % 50 == 0 or done == len(jobs):
                    log(f"  decoded {done}/{len(jobs)} row groups, {time.time() - t0:.0f} s")
        if etag_of(src) != etag0:
            raise SystemExit(f"{src} changed during the build ({etag0} -> {etag_of(src)})")
        # the raceways inside each raceway's bbox (osmtags._inside_rows, file-wide; float32 comparisons)
        rb = np.array([r[1:5] for r in races], np.float32).reshape(-1, 4)
        rf = np.array([r[0] for r in races], np.int64)
        inside_of = {}
        for f, a0, b0, a1, b1, rg in races:
            m = ((rf != f) & (rb[:, 0] >= np.float32(a0)) & (rb[:, 2] <= np.float32(a1))
                 & (rb[:, 1] >= np.float32(b0)) & (rb[:, 3] <= np.float32(b1)))
            inside_of[f] = [int(v) for v in rf[m]]
        race_rg = {r[0]: r[5] for r in races}
        log(f"{len(coarse)} zoom-{cz} areas, {len(races)} raceways, {time.time() - t0:.0f} s")
        areas = sorted(coarse, key=lambda k: tile_id(cz, *k))
        jobs = [(cx, cy, sorted(coarse[(cx, cy)]), os.path.join(tmp, f"area_{cx}_{cy}.bin")) for cx, cy in areas]
        entries, n_rows, at = [], 0, 0
        data_path = os.path.join(tmp, "tiles.bin")
        with open(data_path, "wb") as data, \
                ProcessPoolExecutor(workers, initializer=_init_cut,
                                    initargs=(paths, z, cz, clip, inside_of, race_rg)) as ex:
            for k, ((ents, rows), job) in enumerate(zip(ex.map(cut_area, jobs), jobs), 1):
                with open(job[3], "rb") as f:
                    shutil.copyfileobj(f, data, 1 << 22)
                os.remove(job[3])
                entries += [(t, at + o, n, 1) for t, o, n in ents]
                at += sum(n for _, _, n in ents)
                n_rows += rows
                if k % 100 == 0 or k == len(jobs):
                    log(f"  cut {k}/{len(jobs)} areas, {len(entries):,} tiles, {time.time() - t0:.0f} s")
        lo = bbox or (-180.0, -85.0511, 180.0, 85.0511)
        arch = os.path.join(out, "osmtags.pmtiles")
        write_pmtiles(arch, entries, data_path, lo, n_rows, z, root_max)
        manifest = {"format": FORMAT, "zoom": z, "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "source": src if src.startswith("http") else os.path.basename(src), "source_etag": etag0,
                    "archive": "osmtags.pmtiles", "bytes": os.path.getsize(arch), "tiles": len(entries),
                    "rows": n_rows, "schema": schema(), "license": "ODbL 1.0, (c) OpenStreetMap contributors"}
        if bbox is not None:
            manifest["bbox"] = list(bbox)
        with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        log(f"{len(entries):,} tiles, {n_rows:,} rows, {os.path.getsize(arch) / 1e6:.1f} MB, {time.time() - t0:.0f} s")
        return manifest
    finally:
        if own_tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", nargs="?", default="https://data.openstreetmap.us/layercake/highways.parquet",
                    help="the Layercake highways GeoParquet (URL or path)")
    ap.add_argument("out")
    ap.add_argument("--bbox", help="lon0,lat0,lon1,lat1: only this region (tests)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--tmp", help="the row groups' decoded rows (default: a temporary folder)")
    ap.add_argument("--zoom", type=int, default=SHARD_ZOOM, help=argparse.SUPPRESS)
    a = ap.parse_args()
    bbox = tuple(float(v) for v in a.bbox.split(",")) if a.bbox else None
    build(a.source, a.out, bbox, a.workers, a.tmp, log=lambda s: print(s, flush=True), z=a.zoom)


if __name__ == "__main__":
    sys.exit(main())
