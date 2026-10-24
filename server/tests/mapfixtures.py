"""Synthetic map-data sources for test_mapdata.py (no network, nothing real committed).

Everything a cell of the map-data pipeline reads, generated deterministically at test time:
  * an Overture-like release: STAC catalog JSON (collections with per-file boxes, one item per
    file) and GeoParquet files written by DuckDB with Overture's column names and nested types
    (bbox struct, sources / rules / connectors lists, GEOMETRY), in 2048-row row groups so the
    row-group planning has something to skip;
  * a WorldCover-shaped COG (36000 x 36000 px, 3 x 3 deg, 1024 px Deflate tiles, sparse outside
    the test cell) and a CHM-shaped COG (32768 x 32768 px quadkey-10 tile in EPSG:3857, 512 px
    tiles), written by a minimal TIFF writer (tiff_bytes);
  * a fake netfetch transport serving them by URL with HTTP Range requests (206 + Content-Range),
    counting requests, with injectable failures per host.
Pixel values follow formulas (worldcover_class, canopy_value) so tests can check any window.
"""
import json
import os
import re
import struct
import sys
import threading
import zlib
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mapcells  # noqa: E402
import maprasters  # noqa: E402
import netfetch  # noqa: E402
import overture  # noqa: E402

CELL = mapcells.CellKey(14, 4706, 6244)         # the Fells Point benchmark's cell
RELEASE = "2099-01-01.0"                         # a release id no real server has
S3 = "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com"
AZURE = "https://overturemapswestus2.blob.core.windows.net"
FAST = {h: replace(p, rate=1e9, burst=1e9) for h, p in netfetch.POLICIES.items()}


# --- TIFF writer -----------------------------------------------------------------------------------
def tiff_bytes(width, height, tile, tile_fn, dtype=np.uint8, *, predictor=1, compress=True, pixel_scale=None,
               tiepoint=None, epsg=None, projected=False, nodata=None, big_endian=False):
    """A tiled single-band TIFF. tile_fn(tx, ty) -> (tile, tile) array or None (a sparse tile:
    offset 0, byte count 0)."""
    bo = ">" if big_endian else "<"
    dt = np.dtype(dtype).newbyteorder(bo)
    tx_n, ty_n = -(-width // tile), -(-height // tile)
    blobs = []
    for ty in range(ty_n):
        for tx in range(tx_n):
            a = tile_fn(tx, ty)
            if a is None:
                blobs.append(None)
                continue
            a = np.asarray(a, dtype)
            if predictor == 2:
                a = np.diff(a, axis=1, prepend=np.zeros((tile, 1), dtype)).astype(dtype)
            if predictor == 3:              # floating point: each row's bytes as planes (most significant first),
                b = a.astype(np.dtype(dtype).newbyteorder(">")).view(np.uint8).reshape(tile, tile, -1)
                b = b.transpose(0, 2, 1).reshape(tile, -1)      # ... differenced along the row
                raw = np.diff(b, axis=1, prepend=np.zeros((tile, 1), np.uint8)).astype(np.uint8).tobytes()
            else:
                raw = a.astype(dt).tobytes()
            blobs.append(zlib.compress(raw) if compress else raw)
    entries = []                    # (tag, type, values)
    entries += [(256, 4, [width]), (257, 4, [height]), (258, 3, [8 * dt.itemsize]),
                (259, 3, [8 if compress else 1]), (262, 3, [1]), (277, 3, [1]), (284, 3, [1]),
                (317, 3, [predictor]), (322, 3, [tile]), (323, 3, [tile])]
    n_tiles = len(blobs)
    entries.append((324, 4, [0] * n_tiles))          # patched below
    entries.append((325, 4, [0 if b is None else len(b) for b in blobs]))
    entries.append((339, 3, [3 if dt.kind == "f" else 2 if dt.kind == "i" else 1]))
    if pixel_scale:
        entries.append((33550, 12, list(pixel_scale) + [0.0]))
    if tiepoint:
        entries.append((33922, 12, [0.0, 0.0, 0.0, tiepoint[0], tiepoint[1], 0.0]))
    if epsg:
        keys = [1, 1, 0, 3, 1024, 0, 1, 1 if projected else 2, 1025, 0, 1, 1,
                3072 if projected else 2048, 0, 1, epsg]
        entries.append((34735, 3, keys))
    if nodata is not None:
        entries.append((42113, 2, (str(nodata) + "\0").encode()))
    entries.sort(key=lambda e: e[0])
    fmt = {3: "H", 4: "I", 12: "d", 2: "s"}
    ifd_off = 8
    n = len(entries)
    data_off = ifd_off + 2 + n * 12 + 4
    extra = b""

    def value_bytes(typ, vals):
        if typ == 2:
            return vals
        return struct.pack(bo + fmt[typ] * len(vals), *vals)

    # first pass: layout of out-of-line values; tile data after them
    lens = [len(value_bytes(t, v)) for _, t, v in entries]
    extra_len = sum(ln for ln in lens if ln > 4)
    data_start = data_off + extra_len
    offsets, pos = [], data_start
    for b in blobs:
        if b is None:
            offsets.append(0)
        else:
            offsets.append(pos)
            pos += len(b)
    entries = [(t, ty, offsets if t == 324 else v) for t, ty, v in entries]
    body = struct.pack(bo + "H", n)
    cur = data_off
    for (tag, typ, vals) in entries:
        vb = value_bytes(typ, vals)
        count = len(vals)
        if len(vb) <= 4:
            body += struct.pack(bo + "HHI", tag, typ, count) + vb.ljust(4, b"\0")
        else:
            body += struct.pack(bo + "HHII", tag, typ, count, cur)
            extra += vb
            cur += len(vb)
    body += struct.pack(bo + "I", 0)
    head = (b"MM" if big_endian else b"II") + struct.pack(bo + "HI", 42, ifd_off)
    return head + body + extra + b"".join(b for b in blobs if b is not None)


# --- rasters ---------------------------------------------------------------------------------------
WC_CLASSES = np.array([10, 20, 30, 40, 50, 60, 80], np.uint8)


def worldcover_class(col, row):
    """The synthetic WorldCover class at global pixel (col from -180, row from +90)."""
    return WC_CLASSES[((np.asarray(col) // 7) + (np.asarray(row) // 5)) % len(WC_CLASSES)]


def canopy_value(col, row):
    """The synthetic canopy height (m) at pixel (col, row) of the CHM file."""
    return ((np.asarray(col) + 3 * np.asarray(row)) % 40).astype(np.uint8)


def worldcover_tiff(lat_sw, lon_sw, cells):
    """The WorldCover file with SW corner (lat_sw, lon_sw); tiles under `cells` filled, the rest sparse."""
    p, size, tile = maprasters.WORLDCOVER_PPD, 3 * maprasters.WORLDCOVER_PPD, 1024
    col_f = (lon_sw + 180) * p
    row_f = (90 - (lat_sw + 3)) * p
    need = set()
    for c in cells:
        c0, r0, c1, r1 = maprasters.worldcover_window(c.bounds())
        for ty in range((r0 - row_f) // tile, (r1 - 1 - row_f) // tile + 1):
            for tx in range((c0 - col_f) // tile, (c1 - 1 - col_f) // tile + 1):
                need.add((tx, ty))

    def tile_fn(tx, ty):
        if (tx, ty) not in need:
            return None
        cc, rr = np.meshgrid(np.arange(tile) + col_f + tx * tile, np.arange(tile) + row_f + ty * tile)
        return worldcover_class(cc, rr)
    return tiff_bytes(size, size, tile, tile_fn, pixel_scale=(1.0 / p, 1.0 / p), tiepoint=(lon_sw, lat_sw + 3),
                      epsg=4326, nodata=0)


def chm_tiff(parent, cells, predictor=1):
    """The CHM file of quadkey-10 tile `parent`; the 512 px tiles under `cells` filled."""
    size, tile = 32768, 512
    half = maprasters.MERC_HALF
    tsize = 2 * half / (1 << parent.z)
    px = tsize / size
    need = set()
    for c in cells:
        s = 1 << (c.z - parent.z)
        x0, y0 = (c.x - parent.x * s) * (size // s), (c.y - parent.y * s) * (size // s)
        for ty in range(y0 // tile, (y0 + size // s) // tile):
            for tx in range(x0 // tile, (x0 + size // s) // tile):
                need.add((tx, ty))

    def tile_fn(tx, ty):
        if (tx, ty) not in need:
            return None
        cc, rr = np.meshgrid(np.arange(tile) + tx * tile, np.arange(tile) + ty * tile)
        return canopy_value(cc, rr)
    return tiff_bytes(size, size, tile, tile_fn, predictor=predictor, pixel_scale=(px, px),
                      tiepoint=(parent.x * tsize - half, half - parent.y * tsize), epsg=3857, projected=True)


# --- Overture --------------------------------------------------------------------------------------
SEG_SCHEMA = """id VARCHAR, version INTEGER, subtype VARCHAR, class VARCHAR, subclass VARCHAR,
    names STRUCT("primary" VARCHAR), sources STRUCT(property VARCHAR, dataset VARCHAR, license VARCHAR,
    record_id VARCHAR)[], road_surface STRUCT("value" VARCHAR, "between" DOUBLE[])[],
    road_flags STRUCT("values" VARCHAR[], "between" DOUBLE[])[], width_rules STRUCT("value" DOUBLE,
    "between" DOUBLE[])[], level_rules STRUCT("value" INTEGER, "between" DOUBLE[])[],
    subclass_rules STRUCT("value" VARCHAR, "between" DOUBLE[])[], speed_limits STRUCT(max_speed
    STRUCT("value" INTEGER, unit VARCHAR), "between" DOUBLE[], "when" STRUCT(heading VARCHAR))[],
    connectors STRUCT(connector_id VARCHAR, "at" DOUBLE)[], access_restrictions STRUCT(access_type VARCHAR,
    "when" STRUCT(heading VARCHAR, "mode" VARCHAR[], during VARCHAR, "using" VARCHAR[], recognized VARCHAR[],
    vehicle VARCHAR[]), "between" DOUBLE[])[]"""
CON_SCHEMA = """id VARCHAR, version INTEGER, sources STRUCT(property VARCHAR, dataset VARCHAR, license VARCHAR,
    record_id VARCHAR)[]"""
BLD_SCHEMA = """id VARCHAR, version INTEGER, subtype VARCHAR, class VARCHAR, names STRUCT("primary" VARCHAR),
    sources STRUCT(property VARCHAR, dataset VARCHAR, license VARCHAR, record_id VARCHAR)[], height DOUBLE,
    min_height DOUBLE, num_floors INTEGER, num_floors_underground INTEGER, min_floor INTEGER, level INTEGER,
    is_underground BOOLEAN, has_parts BOOLEAN, roof_shape VARCHAR, roof_height DOUBLE, roof_direction DOUBLE,
    roof_orientation VARCHAR, roof_material VARCHAR, roof_color VARCHAR, facade_material VARCHAR,
    facade_color VARCHAR"""
BASE_SCHEMA = """id VARCHAR, version INTEGER, subtype VARCHAR, class VARCHAR, names STRUCT("primary" VARCHAR),
    sources STRUCT(property VARCHAR, dataset VARCHAR, license VARCHAR, record_id VARCHAR)[], level INTEGER,
    source_tags MAP(VARCHAR, VARCHAR)"""
PLACE_SCHEMA = """id VARCHAR, version INTEGER, names STRUCT("primary" VARCHAR), basic_category VARCHAR,
    taxonomy STRUCT("primary" VARCHAR), brand STRUCT(wikidata VARCHAR, names STRUCT("primary" VARCHAR)),
    sources STRUCT(property VARCHAR, dataset VARCHAR, license VARCHAR, record_id VARCHAR)[], operating_status VARCHAR,
    confidence DOUBLE"""
SCHEMAS = {
    ("transportation", "segment"): SEG_SCHEMA,
    ("transportation", "connector"): CON_SCHEMA,
    ("buildings", "building"): BLD_SCHEMA,
    ("base", "water"): BASE_SCHEMA + ", is_salt BOOLEAN, is_intermittent BOOLEAN",
    ("base", "land_use"): BASE_SCHEMA + ", surface VARCHAR, elevation INTEGER",
    ("base", "infrastructure"): BASE_SCHEMA + ", height DOUBLE, surface VARCHAR",
    ("places", "place"): PLACE_SCHEMA,
}
OSM = {"property": "", "dataset": "OpenStreetMap", "license": "ODbL-1.0", "record_id": "w1@1"}
MSFT_H = {"property": "/properties/height", "dataset": "Microsoft ML Buildings", "license": "ODbL-1.0",
          "record_id": None}


def _cell_xy(fx, fy, cell=CELL):
    """lon, lat at fractions (0..1 west->east, 0..1 north->south) of the cell."""
    lon0, lat0, lon1, lat1 = cell.bounds()
    return lon0 + fx * (lon1 - lon0), lat1 - fy * (lat1 - lat0)


def _wkt_line(pts):
    return "LINESTRING(" + ", ".join(f"{x!r} {y!r}" for x, y in pts) + ")"


def _wkt_poly(rings):
    return "POLYGON(" + ", ".join("(" + ", ".join(f"{x!r} {y!r}" for x, y in r) + ")" for r in rings) + ")"


def _box(fx0, fy0, fx1, fy1, cell=CELL):
    (x0, y0), (x1, y1) = _cell_xy(fx0, fy1, cell), _cell_xy(fx1, fy0, cell)
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def features():
    """{(theme, type): [row dict with 'wkt' (and 'id', 'version', ...)]} inside / around CELL."""
    seg = []
    for i, (cls, sub) in enumerate([("residential", None), ("primary", None), ("footway", "sidewalk"),
                                    ("service", "alley"), ("track", None)]):
        seg.append(dict(id=f"seg-{i:02d}", version=1, subtype="road", **{"class": cls}, subclass=sub,
                        names={"primary": f"Street {i}"}, sources=[OSM],
                        wkt=_wkt_line([_cell_xy(0.1 + 0.15 * i, 0.2), _cell_xy(0.1 + 0.15 * i, 0.8)])))
    seg[0].update(road_surface=[{"value": "paved", "between": None}],
                  road_flags=[{"values": ["is_bridge", "is_link"], "between": [0.25, 0.5]}],
                  width_rules=[{"value": 9.5, "between": None}],
                  level_rules=[{"value": 1, "between": [0.25, 0.5]}],
                  speed_limits=[{"max_speed": {"value": 25, "unit": "mph"}, "between": None, "when": None},
                                {"max_speed": {"value": 15, "unit": "mph"}, "between": None,
                                 "when": {"heading": "forward"}}],
                  connectors=[{"connector_id": "con-a", "at": 0.0}, {"connector_id": "con-b", "at": 1.0}])
    seg[1].update(road_surface=[{"value": "paved", "between": [0.0, 0.5]},
                                {"value": "gravel", "between": [0.5, 1.0]}],
                  speed_limits=[{"max_speed": {"value": 50, "unit": "km/h"}, "between": None, "when": None}],
                  # one-way along the geometry; a delivery-only restriction that must not count
                  access_restrictions=[{"access_type": "denied", "when": {"heading": "backward"}, "between": None},
                                       {"access_type": "denied", "when": {"heading": "forward", "using": ["as_customer"]},
                                        "between": None}])
    # crosses the east edge: listed here, owned by the east neighbour
    seg.append(dict(id="seg-edge", version=1, subtype="road", **{"class": "secondary"}, sources=[OSM],
                    wkt=_wkt_line([_cell_xy(0.9, 0.5), _cell_xy(1.6, 0.5)])))
    # an older version of seg-03 in the same file: dropped
    seg.append(dict(id="seg-03", version=0, subtype="road", **{"class": "motorway"}, sources=[OSM],
                    wkt=_wkt_line([_cell_xy(0.5, 0.5), _cell_xy(0.6, 0.6)])))
    con = [dict(id=f"con-{c}", version=1, sources=[OSM], wkt=f"POINT({x!r} {y!r})")
           for c, (x, y) in (("a", _cell_xy(0.1, 0.2)), ("b", _cell_xy(0.1, 0.8)))]
    bld = []
    for i in range(6):
        fx, fy = 0.05 + 0.15 * i, 0.4
        b = dict(id=f"bld-{i:02d}", version=1, subtype="residential", **{"class": "house" if i % 2 else None},
                 sources=[OSM], wkt=_wkt_poly([_box(fx, fy, fx + 0.05, fy + 0.05)]))
        if i == 1:
            b.update(height=7.5, sources=[OSM, MSFT_H], num_floors=2, roof_shape="gabled")
        if i == 2:
            b.update(num_floors=3)
        bld.append(b)
    # a courtyard building (a hole)
    bld.append(dict(id="bld-yard", version=1, sources=[OSM],
                    wkt=_wkt_poly([_box(0.3, 0.6, 0.5, 0.8), _box(0.35, 0.65, 0.45, 0.75)][:1]
                                  + [list(reversed(_box(0.35, 0.65, 0.45, 0.75)))])))
    wat = [dict(id="wat-harbour", version=1, subtype="ocean", **{"class": "ocean"}, sources=[OSM],
                is_salt=True, wkt=_wkt_poly([_box(0.5, 0.7, 1.5, 1.5)])),          # crosses south/east edges
           dict(id="wat-canal", version=1, subtype="canal", **{"class": "canal"}, sources=[OSM],
                wkt=_wkt_line([_cell_xy(0.2, 0.9), _cell_xy(0.8, 0.9)]))]
    lu = [dict(id="lu-park", version=1, subtype="park", **{"class": "park"}, sources=[OSM],
               names={"primary": "Patterson Park"}, source_tags={"leisure": "park"},
               wkt=_wkt_poly([_box(-0.5, -0.5, 1.5, 1.5)]))]                       # covers the whole cell
    inf = [dict(id="inf-pole", version=1, subtype="power", **{"class": "power_pole"}, sources=[OSM],
                wkt="POINT({!r} {!r})".format(*_cell_xy(0.5, 0.5))),
           dict(id="inf-line", version=1, subtype="power", **{"class": "power_line"}, sources=[OSM],
                height=20.0, wkt=_wkt_line([_cell_xy(0.0, 0.1), _cell_xy(1.0, 0.1)]))]
    # a Sheetz (a convenience store by its category, a fuel seller by its brand: the gas stations, 2026-10-23)
    pla = [dict(id="pla-sheetz", version=1, names={"primary": "Sheetz"}, basic_category="convenience_store",
                taxonomy={"primary": "convenience_store"}, brand={"wikidata": "Q7492551", "names": {"primary": "Sheetz"}},
                sources=[dict(OSM, dataset="meta", record_id="m1")], operating_status="open", confidence=0.92,
                wkt="POINT({!r} {!r})".format(*_cell_xy(0.6, 0.3)))]
    return {("transportation", "segment"): seg, ("transportation", "connector"): con,
            ("buildings", "building"): bld, ("base", "water"): wat, ("base", "land_use"): lu,
            ("base", "infrastructure"): inf, ("places", "place"): pla}


def _coords(wkt):
    nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?(?:e-?\d+)?", wkt)]
    return nums[0::2], nums[1::2]


def split_top(text):
    """Split at commas outside parentheses."""
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
            continue
        depth += (ch == "(") - (ch == ")")
        cur += ch
    return out + [cur]


def _param(col, v):
    if col == "source_tags" and v is not None:        # a MAP parameter
        return {"key": list(v), "value": list(v.values())}
    return v


FILLER = 2100           # rows of filler before and after the real ones (row groups hold 2048)


def parquet_bytes(theme, typ, rows, tmpdir, name, shuffle=None, filler=True):
    """One GeoParquet file: FILLER far-west rows, the given rows, then FILLER rows split between
    far west and far east (so the third row group's bbox statistics span the cell but hold no
    match). shuffle: a numpy Generator to shuffle the real rows' order in the file."""
    import duckdb
    con = duckdb.connect(config={"autoinstall_known_extensions": False, "autoload_known_extensions": False})
    schema = SCHEMAS[(theme, typ)]
    con.execute(f"CREATE TABLE f ({schema}, wkt VARCHAR, bbox STRUCT(xmin DOUBLE, xmax DOUBLE, ymin DOUBLE, "
                f"ymax DOUBLE), ord INTEGER)")
    cols = [c.strip().split()[0].strip('"') for c in split_top(" ".join(schema.split()))]
    real = list(rows)
    if shuffle is not None:
        real = [real[i] for i in shuffle.permutation(len(real))]
    recs = []
    for r in real:
        xs, ys = _coords(r["wkt"])
        rec = [_param(c, r.get(c)) for c in cols] + [r["wkt"], {"xmin": min(xs), "xmax": max(xs), "ymin": min(ys),
                                                       "ymax": max(ys)}]
        recs.append(rec)
    ph = ", ".join(["?"] * (len(cols) + 2))
    lon0, lat0, _, _ = CELL.bounds()
    n0 = FILLER if filler else 0

    def filler_sql(start, count, lon_fn):
        geom = "'LINESTRING(' || x || ' ' || y || ', ' || (x + 0.0001) || ' ' || (y + 0.0001) || ')'"
        return f"""INSERT INTO f (id, version, wkt, bbox, ord) SELECT 'zz-filler-' || lpad(i::VARCHAR, 6, '0'), 1,
            {geom}, {{'xmin': x, 'xmax': x + 0.0001, 'ymin': y, 'ymax': y + 0.0001}}, i FROM
            (SELECT i, ({lon_fn})::DOUBLE AS x, ({lat0} + 0.001 * (i % 50))::DOUBLE AS y
             FROM range({start}, {start + count}) t(i))"""
    if filler:
        con.execute(filler_sql(0, FILLER, f"{lon0} - 3.0 - 0.0001 * i"))
    for k, rec in enumerate(recs):
        con.execute(f"INSERT INTO f ({', '.join(chr(34) + c + chr(34) for c in cols)}, wkt, bbox, ord) VALUES "
                    f"({ph}, {n0 + k})", rec)
    if filler:
        start = n0 + len(recs)
        con.execute(filler_sql(start, FILLER,
                               f"CASE WHEN i % 2 = 0 THEN {lon0} - 3.0 ELSE {lon0} + 3.0 END - 0.0001 * (i % 7)"))
    path = os.path.join(tmpdir, name)
    sel = ", ".join(f'"{c}"' for c in cols)
    con.execute(f"""COPY (SELECT {sel}, wkt::GEOMETRY AS geometry, bbox FROM f ORDER BY ord)
        TO '{path}' (FORMAT parquet, ROW_GROUP_SIZE 2048, COMPRESSION zstd)""")
    con.close()
    with open(path, "rb") as fh:
        data = fh.read()
    xs, ys = [], []
    for r in real:
        x, y = _coords(r["wkt"])
        xs += x
        ys += y
    if filler:
        xs += [lon0 - 3.0 - 0.0001 * FILLER, lon0 + 3.0001]
        ys += [lat0, lat0 + 0.0501]
    return data, [min(xs), min(ys), max(xs), max(ys)]


def release(tmpdir, release_id=RELEASE, shuffle=None, extra_far_file=True):
    """{url: bytes} of a whole synthetic release (STAC + parquet), and {(theme, type): [file names]}."""
    urls, names = {}, {}
    cat = {"type": "Catalog", "latest": release_id, "links": []}
    urls[f"{overture.STAC_ROOT}/catalog.json"] = json.dumps(cat).encode()
    for (theme, typ), rows in features().items():
        files = [(f"part-00000-{theme}-{typ}.zstd.parquet",) + parquet_bytes(theme, typ, rows, tmpdir,
                                                                               f"{theme}-{typ}.parquet", shuffle)]
        if extra_far_file:              # a file far away: never opened
            far = [dict(r, wkt=r["wkt"]) for r in rows[:1]]
            far = [dict(r, wkt=re.sub(r"-?\d+\.\d+", lambda m: repr(float(m.group()) + 40.0), r["wkt"]))
                   for r in far]
            files.append((f"part-00001-{theme}-{typ}.zstd.parquet",)
                         + parquet_bytes(theme, typ, far, tmpdir, f"{theme}-{typ}-far.parquet", None, False))
        items, boxes = [], []
        for k, (fname, data, bb) in enumerate(files):
            href = f"{overture.STAC_ROOT}/{release_id}/{theme}/{typ}/{k:05d}/{k:05d}.json"
            aws = f"{S3}/release/{release_id}/theme={theme}/type={typ}/{fname}"
            item = {"type": "Feature", "id": f"{k:05d}", "bbox": bb, "assets": {
                "aws": {"href": aws, "file:size": len(data)},
                "azure": {"href": aws.replace(S3, AZURE), "file:size": len(data)}}}
            urls[href] = json.dumps(item).encode()
            urls[aws] = data
            urls[aws.replace(S3, AZURE)] = data
            items.append({"rel": "item", "href": href})
            boxes.append(bb)
        all_box = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes),
                   max(b[3] for b in boxes)]
        coll = {"type": "Collection", "license": "ODbL-1.0", "links": [{"rel": "root", "href": "x"}] + items,
                "extent": {"spatial": {"bbox": [all_box] + boxes}}}
        urls[f"{overture.STAC_ROOT}/{release_id}/{theme}/{typ}/collection.json"] = json.dumps(coll).encode()
        names[(theme, typ)] = [f[0] for f in files]
    return urls, names


def rasters(cells=(CELL,)):
    """{url: bytes} of the WorldCover and CHM files under `cells`."""
    urls = {}
    tiles = {}
    for c in cells:
        lon0, lat0, _, _ = c.bounds()
        key = (int(lat0 // 3 * 3), int(lon0 // 3 * 3))
        tiles.setdefault(key, []).append(c)
    for (la, lo), cs in tiles.items():
        name = maprasters.worldcover_tile_name(la, lo)
        urls[maprasters.WORLDCOVER_URL.format(tile=name)] = worldcover_tiff(la, lo, cs)
    parents = {}
    for c in cells:
        parents.setdefault(c.parent(maprasters.CHM_FILE_Z), []).append(c)
    for p, cs in parents.items():
        urls[maprasters.CHM_URL.format(quadkey=p.quadkey())] = chm_tiff(p, cs)
    return urls


class Transport:
    """netfetch transport serving {url: bytes} with Range support; unknown URLs are 404.
    fail: {host: status} answered instead (e.g. 503 to test the mirror failover)."""

    def __init__(self, files):
        self.files = dict(files)
        self.fail = {}
        self.log = []                   # (url, range header)
        self.lock = threading.Lock()

    def request(self, url, headers, timeout):
        rng = headers.get("Range")
        with self.lock:
            self.log.append((url, rng))
        host = netfetch.host_of(url)
        if host in self.fail:
            return self.fail[host], {}, b""
        data = self.files.get(url)
        if data is None:
            return 404, {}, b"not found"
        if rng:
            a, b = (int(v) for v in rng.split("=")[1].split("-"))
            b = min(b, len(data) - 1)
            return 206, {"Content-Range": f"bytes {a}-{b}/{len(data)}"}, data[a:b + 1]
        return 200, {"Content-Type": "application/json"}, data

    def count(self, pattern=""):
        with self.lock:
            return sum(1 for u, _ in self.log if pattern in u)

    def bytes(self, pattern=""):
        total = 0
        with self.lock:
            for u, r in self.log:
                if pattern in u and r:
                    a, b = (int(v) for v in r.split("=")[1].split("-"))
                    total += min(b, len(self.files.get(u, b"")) - 1) - a + 1
        return total


def fetcher(transport, policies=None, **kw):
    return netfetch.Fetcher(transport=transport, sleep=lambda s: None, policies=policies or FAST, **kw)
