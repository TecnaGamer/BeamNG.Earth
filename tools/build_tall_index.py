"""The tall-building index for the horizon ring (server bldhz.py, the user 2026-11-14: "buildings toward the real
horizon"): every Overture building at least --min-height tall (its own height, its storeys, or one of its parts),
with all its parts, one blob a zoom-12 tile.

    python3 tools/build_tall_index.py --footers DIR --work DIR --world --pages OUT      (the published layout)
    python3 tools/build_tall_index.py --footers DIR --work DIR --region NAME:LAT,LON,KM --out DIR   (local, tests)
    python3 tools/build_tall_index.py --footers DIR --fetch-footers                    (the footers first, once)

Standalone (numpy + duckdb, no server module): a pinned copy runs in the data repo's job, with
build_osm_tag_shards.py (the PMTiles writer) beside it and the release's STAC file list (--stac).

--footers: the release's parquet footers (one file per Overture file, 'PAR1' + footer + its length + 'PAR1', under
building/ and building_part/; --fetch-footers reads them with tail range requests, ~450 MB). Each file's row groups are
chosen from them (their bbox and height / storey statistics), and only those row groups' byte ranges are fetched from
Overture's S3, into sparse copies of the files (--work, kept: a second run fetches nothing again) that DuckDB reads
locally (file_row_number alone picks the row groups). The planet: 7.6k row groups, ~23 GB of ranges.

Blobs: npz (allow_pickle=False: plain arrays) b_* the buildings, p_* their parts: id, ring_off, ll (the first outer
ring, open, lon / lat in 1e-7 degrees, int32), t_<col> / f_<col> / b_<col> the attribute columns as the server's map
cells have them (overture.LAYERS: text "" missing, float32 NaN missing, bool; B_SPEC / P_SPEC here are copies). A
building goes to the tile of its outer ring's centroid, its parts with it (the server takes a tile's every building).
Deterministic for a release: rows sorted by id, the npz written with fixed zip entries.
--pages: <key>/tallbld-NNN.pmtiles + manifest.json (write_pages); --out: 12/<x>_<y>.npz + manifest.json (local).

Publishing anywhere needs the user's go (this writes local files only)."""
import argparse
import concurrent.futures as cf
import gzip
import hashlib
import io
import json
import math
import os
import sys
import threading
import time
import urllib.request
import zipfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FORMAT = 1
Z = 12
UA = "bnge-tall-index"
FETCH_THREADS = 16
FETCH_TRIES = 5
FOOTER_TAIL = 800_000

# (copies of server/overture.py LAYERS["buildings"] / ["building_parts"]: the columns the map cells have; a test keeps
# them equal)
_SRC = "sources[1].dataset AS src_dataset, sources[1].record_id AS src_record"
_NAME = "names.\"primary\" AS name"
B_SPEC = dict(cols=f"subtype, class, {_NAME}, {_SRC}, height, min_height, num_floors, "
                   "num_floors_underground, min_floor, level, is_underground, has_parts, roof_shape, "
                   "roof_height, roof_direction, roof_orientation, roof_material, roof_color, "
                   "facade_material, facade_color, "
                   "COALESCE(list_filter(sources, lambda s: s.property = '/properties/height')[1].dataset, "
                   "CASE WHEN height IS NOT NULL THEN sources[1].dataset END) AS height_src",
              text=("subtype", "class", "name", "src_dataset", "src_record", "roof_shape", "roof_orientation",
                    "roof_material", "roof_color", "facade_material", "facade_color", "height_src"),
              float=("height", "min_height", "num_floors", "num_floors_underground", "min_floor", "level",
                     "roof_height", "roof_direction"),
              bool=("is_underground", "has_parts"))
P_SPEC = dict(cols=f"building_id, {_SRC}, height, min_height, num_floors, min_floor, roof_shape, "
                   "roof_height, facade_color, roof_color, facade_material, roof_material, "
                   "roof_direction, roof_orientation",
              text=("building_id", "src_dataset", "src_record", "roof_shape", "facade_color", "roof_color",
                    "facade_material", "roof_material", "roof_orientation"),
              float=("height", "min_height", "num_floors", "min_floor", "roof_height", "roof_direction"))


def text_column(values):
    v = ["" if x is None else str(x) for x in values]
    return np.array(v, dtype=f"<U{max([1] + [len(x) for x in v])}")


def float_column(values):
    return np.array([np.nan if x is None else float(x) for x in values], dtype=np.float32)


def bool_column(values):
    return np.array([bool(x) for x in values], dtype=bool)


def tile_of(lat, lon, z=Z):
    """Web Mercator tile (x, y) arrays of lat / lon arrays (the server's geotiles grid)."""
    n = 1 << z
    lat = np.clip(np.asarray(lat, np.float64), -85.0511287798, 85.0511287798)
    x = np.floor((np.asarray(lon, np.float64) + 180.0) / 360.0 * n).astype(np.int64)
    r = np.radians(lat)
    y = np.floor((1.0 - np.log(np.tan(r) + 1.0 / np.cos(r)) / np.pi) / 2.0 * n).astype(np.int64)
    return np.clip(x, 0, n - 1), np.clip(y, 0, n - 1)


def tile_key(x, y, z=Z):
    return f"g{z}_{int(x)}_{int(y)}"


def first_ring_wkb(b):
    """The first outer ring of a WKB Polygon / MultiPolygon as float64 (k, 2) lon / lat (as written), or None."""
    import struct
    b = bytes(b)

    def head(i):
        le = b[i] == 1
        t = struct.unpack_from("<I" if le else ">I", b, i + 1)[0]
        return le, t % 1000, i + 5
    le, t, i = head(0)
    if t == 6:                          # (MultiPolygon: its first polygon)
        n = struct.unpack_from("<I" if le else ">I", b, i)[0]
        if n < 1:
            return None
        le, t, i = head(i + 4)
    if t != 3:
        return None
    nr = struct.unpack_from("<I" if le else ">I", b, i)[0]
    if nr < 1:
        return None
    npt = struct.unpack_from("<I" if le else ">I", b, i + 4)[0]
    return np.frombuffer(b, "<f8" if le else ">f8", npt * 2, i + 8).reshape(-1, 2).astype(np.float64)
# the leaf columns a query reads (top-level names: every leaf under one); geometry, bbox and the source / name structs
B_TOP = ("id", "version", "geometry", "bbox", "sources", "names", "subtype", "class", "height", "min_height",
         "num_floors", "num_floors_underground", "min_floor", "level", "is_underground", "has_parts", "roof_shape",
         "roof_height", "roof_direction", "roof_orientation", "roof_material", "roof_color", "facade_material",
         "facade_color")
P_TOP = ("id", "version", "geometry", "bbox", "sources", "building_id", "height", "min_height", "num_floors",
         "min_floor", "roof_shape", "roof_height", "facade_color", "roof_color", "facade_material", "roof_material",
         "roof_direction", "roof_orientation")


def region_box(lat, lon, km):
    dl = km / 111.2
    dx = km / (111.32 * math.cos(math.radians(lat)))
    return lon - dx, lat - dl, lon + dx, lat + dl


class Meta:
    """Row-group statistics and column-chunk ranges of one type's files, from the cached footers."""

    def __init__(self, con, footers, typ, top, stac_files):
        self.typ = typ
        self.files = {}         # name -> (url, size, footer path)
        for name, _bb, urls, size in stac_files:
            p = os.path.join(footers, typ, name)
            if os.path.exists(p):
                self.files[name] = (urls[0], int(size), p)
        missing = len(stac_files) - len(self.files)
        if missing:
            raise SystemExit(f"{typ}: {missing} footers missing under {footers}")
        tops = ", ".join(f"'{t}'" for t in top)
        rows = con.execute(f"""
            SELECT parse_filename(file_name) f, row_group_id rg, any_value(row_group_num_rows) n,
              min(CASE WHEN path_in_schema = 'bbox, xmin' THEN TRY_CAST(stats_min AS DOUBLE) END) x0,
              max(CASE WHEN path_in_schema = 'bbox, xmax' THEN TRY_CAST(stats_max AS DOUBLE) END) x1,
              min(CASE WHEN path_in_schema = 'bbox, ymin' THEN TRY_CAST(stats_min AS DOUBLE) END) y0,
              max(CASE WHEN path_in_schema = 'bbox, ymax' THEN TRY_CAST(stats_max AS DOUBLE) END) y1,
              max(CASE WHEN path_in_schema = 'height' THEN TRY_CAST(stats_max AS DOUBLE) END) hmax,
              max(CASE WHEN path_in_schema = 'num_floors' THEN TRY_CAST(stats_max AS DOUBLE) END) fmax,
              min(CASE WHEN split_part(path_in_schema, ',', 1) IN ({tops})
                  THEN CASE WHEN dictionary_page_offset > 0 THEN least(dictionary_page_offset, data_page_offset)
                       ELSE data_page_offset END END) a,
              max(CASE WHEN split_part(path_in_schema, ',', 1) IN ({tops})
                  THEN CASE WHEN dictionary_page_offset > 0 THEN least(dictionary_page_offset, data_page_offset)
                       ELSE data_page_offset END + total_compressed_size END) b
            FROM parquet_metadata('{os.path.join(footers, typ)}/*.parquet') GROUP BY ALL ORDER BY f, rg""").fetchall()
        self.rgs = {}           # name -> [(rg, first row, last row, x0, x1, y0, y1, hmax, fmax, a, b)]
        for f, rg, n, x0, x1, y0, y1, hmax, fmax, a, b in rows:
            lst = self.rgs.setdefault(f, [])
            first = lst[-1][2] + 1 if lst else 0
            lst.append((int(rg), first, first + int(n) - 1, x0, x1, y0, y1, hmax, fmax, int(a), int(b)))

    def select(self, box, hmin=None, fmin=None):
        """[(name, rg)] whose bbox statistics meet box (lon0, lat0, lon1, lat1) and, with hmin / fmin, whose height
        reaches hmin or storeys fmin (no statistics: all missing, not taken)."""
        lon0, lat0, lon1, lat1 = box
        out = []
        for f, lst in self.rgs.items():
            for rg, _a, _b, x0, x1, y0, y1, hmax, fmax, _o, _e in lst:
                if None in (x0, x1, y0, y1) or (x0 <= lon1 and x1 >= lon0 and y0 <= lat1 and y1 >= lat0):
                    if hmin is None and fmin is None:
                        out.append((f, rg))
                    elif (hmin is not None and hmax is not None and hmax >= hmin) or (
                            fmin is not None and fmax is not None and fmax >= fmin):
                        out.append((f, rg))
        return out


class Sparse:
    """Sparse local copies of Overture files: the footer at its place and the fetched row groups' column chunks at
    theirs (the rest zeros, never read: queries pick their row groups by file_row_number). Fetched ranges are noted in
    <copy>.got.json."""

    def __init__(self, work, meta):
        self.work, self.meta = work, meta
        self.lock = threading.Lock()
        self.bytes = self.requests = 0
        self.bad = []               # (file, row group) DuckDB could not read even fetched again (left out, reported)

    def path(self, f):
        return os.path.join(self.work, self.meta.typ, f)

    def _open(self, f):
        url, size, foot = self.meta.files[f]
        p = self.path(f)
        if not os.path.exists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(foot, "rb") as fh:
                tail = fh.read()[4:]            # (footer + its length + 'PAR1')
            tmp = p + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(b"PAR1")
                fh.truncate(size)
                fh.seek(size - len(tail))
                fh.write(tail)
            os.replace(tmp, p)
        g = p + ".got.json"
        got = set(json.load(open(g))) if os.path.exists(g) else set()
        return p, got

    def _get(self, url, a, b):
        for k in range(FETCH_TRIES):
            try:
                rq = urllib.request.Request(url, headers={"Range": f"bytes={a}-{b - 1}", "User-Agent": UA})
                data = urllib.request.urlopen(rq, timeout=120).read()
                if len(data) != b - a:
                    raise IOError(f"short read {len(data)} of {b - a}")
                return data
            except Exception:
                if k == FETCH_TRIES - 1:
                    raise
                time.sleep(2 ** k)

    def fill(self, pairs, again=False):
        """The row groups [(name, rg)] fetched where not yet (their needed columns' span, one request each); again:
        fetched whatever the note says (a copy DuckDB could not read)."""
        by = {}
        for f, rg in pairs:
            by.setdefault(f, set()).add(rg)
        jobs = []
        for f, rgs in sorted(by.items()):
            p, got = self._open(f)
            for rg, _a, _b, *_s, o, e in self.meta.rgs[f]:
                if rg in rgs and (again or rg not in got):
                    jobs.append((f, p, rg, o, e))

        def one(job):
            f, p, rg, o, e = job
            data = self._get(self.meta.files[f][0], o, e)
            with self.lock:
                with open(p, "r+b") as fh:
                    fh.seek(o)
                    fh.write(data)
                g = p + ".got.json"
                got = set(json.load(open(g))) if os.path.exists(g) else set()
                got.add(rg)
                with open(g + ".tmp", "w") as fh:
                    json.dump(sorted(got), fh)
                os.replace(g + ".tmp", g)
                self.bytes += len(data)
                self.requests += 1
        with cf.ThreadPoolExecutor(FETCH_THREADS) as ex:
            list(ex.map(one, jobs))

    def load(self, con, table, proj, pairs):
        """Temp table `table` of proj (a SELECT list) over the pairs' row groups alone: one read per file with only its
        row groups' row numbers in the WHERE (with another condition there DuckDB read row groups that were not
        fetched: the sparse copies' zeros), filtered afterwards from the table. False: no pairs."""
        by = {}
        for f, rg in pairs:
            by.setdefault(f, []).append(rg)
        con.execute(f"DROP TABLE IF EXISTS {table}")
        first = True
        for f, rgs in sorted(by.items()):
            p = self.path(f)
            rr = {r[0]: r for r in self.meta.rgs[f]}

            def run(sel):
                cond = " OR ".join(f"file_row_number BETWEEN {rr[rg][1]} AND {rr[rg][2]}" for rg in sorted(sel))
                q = f"SELECT {proj} FROM read_parquet('{p}', file_row_number = true) WHERE {cond}"
                con.execute(f"CREATE TEMP TABLE {table} AS {q}" if first else f"INSERT INTO {table} {q}")
            try:
                run(rgs)
            except Exception as e:      # (a copy with a row group not as fetched: fetched again, then each alone)
                print(f"  {f}: {type(e).__name__}: {str(e)[:80]}; its row groups fetched again", flush=True)
                self.fill([(f, rg) for rg in rgs], again=True)
                for rg in sorted(rgs):
                    try:
                        run([rg])
                        first = False
                    except Exception as e2:
                        self.bad.append((f, rg))
                        print(f"  {f} row group {rg} left out: {type(e2).__name__}", flush=True)
                if first:
                    continue
            first = False
        return not first


def _rows(con, spec, sparse, pairs, where):
    """Deduplicated rows (highest version per id) of the spec's columns + the wkb, from the pairs' row groups."""
    names = ["id", "wkb", "cx", "cy"] + [c for k in ("text", "float", "bool") for c in spec.get(k, ())]
    proj = (f"id, version, {spec['cols']}, ST_AsWKB(geometry) AS wkb, (bbox.xmin + bbox.xmax) / 2 AS cx, "
            "(bbox.ymin + bbox.ymax) / 2 AS cy, bbox")
    if not sparse.load(con, "s", proj, pairs):
        return [], names
    return con.execute(f"""SELECT {', '.join(names)} FROM s WHERE {where}
                           QUALIFY row_number() OVER (PARTITION BY id ORDER BY version DESC, wkb) = 1
                           ORDER BY id""").fetchall(), names


def _ring(wkb):
    """The first outer ring of a WKB polygon as int32 (k, 2) lon/lat x 1e7 (open: no repeated last point), or None."""
    try:
        r = first_ring_wkb(wkb)
    except Exception:
        return None
    if r is None:
        return None
    if len(r) > 1 and np.array_equal(r[0], r[-1]):
        r = r[:-1]
    if len(r) < 3:
        return None
    return np.round(r * 1e7).astype(np.int32)


def _columns(prefix, rows, names, spec):
    at = {c: i for i, c in enumerate(names)}
    out = {f"{prefix}id": text_column([r[at["id"]] for r in rows])}
    for c in spec.get("text", ()):
        out[f"{prefix}t_{c}"] = text_column([r[at[c]] for r in rows])
    for c in spec.get("float", ()):
        out[f"{prefix}f_{c}"] = float_column([r[at[c]] for r in rows])
    for c in spec.get("bool", ()):
        out[f"{prefix}b_{c}"] = bool_column([r[at[c]] for r in rows])
    rings = [_ring(r[at["wkb"]]) for r in rows]
    off = np.zeros(len(rows) + 1, np.int64)
    off[1:] = np.cumsum([len(x) for x in rings])
    out[f"{prefix}ring_off"] = off
    out[f"{prefix}ll"] = np.concatenate(rings) if rings else np.zeros((0, 2), np.int32)
    return out


def _want(con, ids):
    """Add building ids to the want table (DuckDB's executemany refuses an empty list: a region with no directly tall
    building, or a parts file with no new tall building, aborted the whole build; Codex review 2026-11-14)."""
    ids = list(ids)
    if ids:
        con.executemany("INSERT INTO want VALUES (?)", [(b,) for b in ids])


def _by_file(pairs):
    by = {}
    for f, rg in pairs:
        by.setdefault(f, []).append((f, rg))
    return [by[f] for f in sorted(by)]


def build_region(con, metas, sparses, box, min_h, log):
    """(buildings rows, names, parts rows, names) of the box's tall buildings and all their parts, one Overture file
    at a time (the planet's 7.5k candidate row groups never in DuckDB at once)."""
    mb, mp = metas
    sb, sp = sparses
    lon0, lat0, lon1, lat1 = box
    inbox = f"bbox.xmin <= {lon1!r} AND bbox.xmax >= {lon0!r} AND bbox.ymin <= {lat1!r} AND bbox.ymax >= {lat0!r}"
    fmin = math.ceil(min_h / 3.2)
    t0 = time.time()
    # 1. the buildings tall themselves (height, or storeys where no height)
    bp = mb.select(box, hmin=min_h, fmin=fmin)
    r0, b0 = sb.requests, sb.bytes
    sb.fill(bp)
    log(f"  buildings: {len(bp)} row groups, {sb.requests - r0} requests {(sb.bytes - b0) / 1e6:.1f} MB, "
        f"{time.time() - t0:.0f} s")
    rows, names = [], None
    for grp in _by_file(bp):
        got, names = _rows(con, B_SPEC, sb, grp, f"{inbox} AND (height >= {min_h} OR (height IS NULL AND "
                                                 f"num_floors >= {fmin}))")
        rows += got
    log(f"  {len(rows)} tall buildings, {time.time() - t0:.0f} s")
    # 2. the parts, a file at a time: the tall ones' buildings, then every part of a tall building
    pp = mp.select(box)
    r0, b0 = sp.requests, sp.bytes
    sp.fill(pp)
    log(f"  parts: {len(pp)} row groups, {sp.requests - r0} requests {(sp.bytes - b0) / 1e6:.1f} MB")
    have = {r[0] for r in rows}
    con.execute("DROP TABLE IF EXISTS want")
    con.execute("CREATE TEMP TABLE want (id VARCHAR)")
    _want(con, sorted(have))
    # (two passes: every tall part's building first, then the parts of all wanted buildings from every file; one pass
    # missed a tower's lower parts in a file read before the one holding its tall part, Codex review 2026-11-14)
    tall_parts, prows, pnames = [], [], None
    files = _by_file(pp)
    for grp in files:
        if not sp.load(con, "pall", "building_id, height, bbox, (bbox.xmin + bbox.xmax) / 2 AS cx, "
                                    "(bbox.ymin + bbox.ymax) / 2 AS cy", grp):
            continue
        tp = con.execute(f"""SELECT building_id, min(cx), max(cx), min(cy), max(cy) FROM pall
                             WHERE {inbox} AND coalesce(height, 0) >= {min_h} GROUP BY building_id""").fetchall()
        tall_parts += tp
        _want(con, sorted({t[0] for t in tp} - have))
        have.update(t[0] for t in tp)
    for grp in files:
        got, pnames = _rows(con, P_SPEC, sp, grp, f"{inbox} AND building_id IN (SELECT id FROM want)")
        prows += got
    # 3. the buildings tall only through their parts: each from the row groups round its parts
    tall_ids = {r[0] for r in rows}
    miss = sorted({t[0]: t for t in tall_parts if t[0] not in tall_ids}.values())
    extra = set()
    for bid, x0, x1, y0, y1 in miss:
        extra.update(mb.select((x0 - 1e-4, y0 - 1e-4, x1 + 1e-4, y1 + 1e-4)))
    r0, b0 = sb.requests, sb.bytes
    sb.fill(sorted(extra))
    log(f"  {len(miss)} buildings tall by their parts: {len(extra - set(bp))} more row groups, {sb.requests - r0} "
        f"requests {(sb.bytes - b0) / 1e6:.1f} MB")
    if miss:
        con.execute("DROP TABLE IF EXISTS miss")
        con.execute("CREATE TEMP TABLE miss (id VARCHAR)")
        con.executemany("INSERT INTO miss VALUES (?)", [(m[0],) for m in miss])
        more = []
        for grp in _by_file(sorted(extra)):
            got, names = _rows(con, B_SPEC, sb, grp, "id IN (SELECT id FROM miss)")
            more += got
        found = {r[0] for r in more}
        if len(miss) - len(found):
            log(f"  {len(miss) - len(found)} part-tall buildings not found round their parts")
        rows += more
    rows = sorted({r[0]: r for r in rows}.values(), key=lambda r: r[0])
    keep = {r[0] for r in rows}
    prows = sorted({r[0]: r for r in prows if r[(pnames or []).index("building_id")] in keep}.values(),
                   key=lambda r: r[0]) if pnames else []
    if names is None:
        names = ["id", "wkb", "cx", "cy"] + [c for k in ("text", "float", "bool") for c in B_SPEC.get(k, ())]
    return rows, names, prows, pnames


def tile_blobs(rows, names, prows, pnames, log):
    """{(x, y): npz bytes} of zoom-Z tiles: each building in the tile of its outer ring's centroid (the vertices'
    mean: as the server's tile_buildings finds a building's place), its parts with it."""
    at = {c: i for i, c in enumerate(names)}
    rings = {r[0]: _ring(r[at["wkb"]]) for r in rows}
    rows = [r for r in rows if rings[r[0]] is not None]
    if not rows:
        return {}
    c = np.array([rings[r[0]].astype(np.float64).mean(0) / 1e7 for r in rows])
    bx, by = tile_of(c[:, 1], c[:, 0], Z)
    tile = {r[0]: (int(x), int(y)) for r, x, y in zip(rows, np.atleast_1d(bx), np.atleast_1d(by))}
    pat = {c: i for i, c in enumerate(pnames)} if pnames else {}
    bt, pt = {}, {}
    for r in rows:
        bt.setdefault(tile[r[0]], []).append(r)
    for r in prows:
        k = tile.get(r[pat["building_id"]])
        if k is not None and _ring(r[pat["wkb"]]) is not None:
            pt.setdefault(k, []).append(r)
    out = {}
    for k in sorted(bt):
        arrays = _columns("b_", bt[k], names, B_SPEC)
        if pt.get(k):
            arrays.update(_columns("p_", pt[k], pnames, P_SPEC))
        out[k] = npz_bytes(arrays)
    log(f"  {len(rows)} buildings, {len(prows)} parts in {len(out)} tiles")
    return out


def npz_bytes(arrays):
    """arrays as .npz bytes with fixed zip entries (byte-identical for the same arrays)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k in sorted(arrays):
            b = io.BytesIO()
            np.lib.format.write_array(b, np.ascontiguousarray(arrays[k]), allow_pickle=False)
            zi = zipfile.ZipInfo(k + ".npy", date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, b.getvalue())
    return buf.getvalue()


def write_npz(path, arrays):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(npz_bytes(arrays))
    os.replace(tmp, path)


def write_local(out, blobs, release, min_h, regions):
    """The local layout (server BNE_BLDHZ_DIR): <out>/12/<x>_<y>.npz + manifest.json (each tile's SHA-1)."""

    os.makedirs(os.path.join(out, str(Z)), exist_ok=True)
    tiles = {}
    for (x, y), b in sorted(blobs.items()):
        p = os.path.join(out, str(Z), f"{x}_{y}.npz")
        with open(p + ".tmp", "wb") as f:
            f.write(b)
        os.replace(p + ".tmp", p)
        tiles[tile_key(x, y)] = {"sha1": hashlib.sha1(b).hexdigest(), "bytes": len(b)}
    m = {"format": FORMAT, "release": release, "z": Z, "min_height": min_h, "regions": regions, "tiles": tiles}
    with open(os.path.join(out, "manifest.json.tmp"), "w") as f:
        json.dump(m, f, indent=1, sort_keys=True)
    os.replace(os.path.join(out, "manifest.json.tmp"), os.path.join(out, "manifest.json"))
    return m


PAGES_NAME = "tallbld"           # the dataset's folder on the data site (server bldhz.PAGES_NAME)
PART_BYTES = 256 << 20
HEAD_BYTES = 16384


def write_pages(out, blobs, release, min_h, bbox=None):
    """The published layout (server bldhz.TallShards, as osmyears): <out>/<key>/tallbld-NNN.pmtiles (the tiles in
    tile-id order, each part a whole PMTiles archive, tiles stored as their npz bytes) and <out>/manifest.json
    {format, zoom, built, key, release, min_height, parts [{archive, first, last, bytes, tiles, head_sha256,
    sha256}], bytes, tiles, license}. <key>: the build time and a digest of its parts (its files never change once
    published: publish the folder before the manifest)."""
    import shutil
    import tempfile
    import build_osm_tag_shards as ots
    os.makedirs(out, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="tallbld-build-", dir=out)
    try:
        data_path = os.path.join(tmp, "tiles.bin")
        entries, at = [], 0
        with open(data_path, "wb") as data:
            for (x, y) in sorted(blobs, key=lambda t: ots.tile_id(Z, *t)):
                b = blobs[(x, y)]
                data.write(b)
                entries.append((ots.tile_id(Z, x, y), at, len(b), 1))
                at += len(b)
        built = time.gmtime()
        work = os.path.join(tmp, "parts")
        os.makedirs(work)
        lo = bbox or (-180.0, -85.0511, 180.0, 85.0511)
        parts = ots.write_parts(work, entries, data_path, lo, len(entries), Z, 16384 - 127, PART_BYTES,
                                prefix=PAGES_NAME, title="BeamNG.Earth tall buildings", fmt=f"tallnpz{FORMAT}")
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
        m = {"format": FORMAT, "zoom": Z, "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", built), "key": key,
             "release": release, "min_height": min_h, "parts": parts, "bytes": sum(p["bytes"] for p in parts),
             "tiles": len(entries),
             "license": "ODbL 1.0 (Overture Maps buildings: OpenStreetMap contributors and others, see Overture)"}
        if bbox is not None:
            m["bbox"] = list(bbox)
        with open(os.path.join(out, "manifest.json.tmp"), "w", encoding="utf-8") as f:
            json.dump(m, f)
        os.replace(os.path.join(out, "manifest.json.tmp"), os.path.join(out, "manifest.json"))
        return m
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def fetch_footers(out, stac):
    """Every buildings / building_part file's footer of the release (stac) into out/<type>/<name>, as 'PAR1' + footer
    + its length + 'PAR1' (what parquet_metadata reads); ones already there kept."""
    def rng(url, a, b):
        for k in range(FETCH_TRIES):
            try:
                rq = urllib.request.Request(url, headers={"Range": f"bytes={a}-{b}", "User-Agent": UA})
                return urllib.request.urlopen(rq, timeout=120).read()
            except Exception:
                if k == FETCH_TRIES - 1:
                    raise
                time.sleep(2 ** k)

    def one(job):
        typ, name, url, size = job
        p = os.path.join(out, typ, name)
        if os.path.exists(p):
            return 0
        tail = rng(url, max(0, size - FOOTER_TAIL), size - 1)
        if tail[-4:] != b"PAR1":
            raise IOError(f"{url}: no parquet footer")
        fl = int.from_bytes(tail[-8:-4], "little")
        if fl + 8 > len(tail):
            tail = rng(url, size - 8 - fl, size - len(tail) - 1) + tail
        with open(p + ".tmp", "wb") as f:
            f.write(b"PAR1" + tail[-8 - fl:-8] + fl.to_bytes(4, "little") + b"PAR1")
        os.replace(p + ".tmp", p)
        return len(tail)
    jobs = []
    for typ in ("building", "building_part"):
        os.makedirs(os.path.join(out, typ), exist_ok=True)
        for name, _bb, urls, size in stac["collections"][f"buildings/{typ}"]["files"]:
            jobs.append((typ, name, urls[0], int(size)))
    t0 = time.time()
    with cf.ThreadPoolExecutor(FETCH_THREADS) as ex:
        n = sum(ex.map(one, jobs))
    print(f"footers: {len(jobs)} files, {n / 1e6:.0f} MB read, {time.time() - t0:.0f} s", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--footers", required=True)
    ap.add_argument("--out", default=None, help="the local layout (server BNE_BLDHZ_DIR)")
    ap.add_argument("--pages", default=None, help="the published layout (PMTiles parts + manifest)")
    ap.add_argument("--work", default=None, help="the sparse copies (kept: a second run fetches nothing again)")
    ap.add_argument("--region", action="append", default=[], help="NAME:LAT,LON,KM (a square of 2 KM)")
    ap.add_argument("--world", action="store_true", help="the whole planet")
    ap.add_argument("--min-height", type=float, default=60.0)
    ap.add_argument("--stac", default=None, help="the release's file list (default: the server's bundled one)")
    ap.add_argument("--prefetch", action="store_true", help="only fetch the whole planet's candidate row groups")
    ap.add_argument("--fetch-footers", action="store_true", help="only fetch the footers (tail range reads)")
    a = ap.parse_args()
    import duckdb
    stac_path = a.stac
    if stac_path is None:
        import glob
        got = sorted(glob.glob(os.path.join(HERE, "..", "server", "overture_stac_*.json.gz")))
        if not got:
            raise SystemExit("--stac: the release's file list")
        stac_path = got[-1]
    stac = json.load(gzip.open(stac_path))
    release = stac["release"]
    if a.fetch_footers:
        fetch_footers(a.footers, stac)
        return
    if not a.work:
        raise SystemExit("--work: the sparse copies' folder")
    con = duckdb.connect()
    con.execute("SET threads = 4")
    metas = (Meta(con, a.footers, "building", B_TOP, stac["collections"]["buildings/building"]["files"]),
             Meta(con, a.footers, "building_part", P_TOP, stac["collections"]["buildings/building_part"]["files"]))
    sparses = (Sparse(a.work, metas[0]), Sparse(a.work, metas[1]))
    world = (-180.0, -90.0, 180.0, 90.0)
    t0 = time.time()
    if a.prefetch:
        sparses[1].fill(metas[1].select(world))
        print(f"parts: {sparses[1].requests} requests {sparses[1].bytes / 1e9:.2f} GB, {time.time() - t0:.0f} s", flush=True)
        pairs = metas[0].select(world, hmin=a.min_height, fmin=math.ceil(a.min_height / 3.2))
        for k in range(0, len(pairs), 400):         # (in slices: progress)
            sparses[0].fill(pairs[k:k + 400])
            print(f"buildings: {min(k + 400, len(pairs))}/{len(pairs)} row groups, {sparses[0].bytes / 1e9:.2f} GB, "
                  f"{time.time() - t0:.0f} s", flush=True)
        return
    boxes = [("world", world)] if a.world else []
    for spec in a.region:
        name, rest = spec.split(":", 1)
        lat, lon, km = map(float, rest.split(","))
        boxes.append((name, region_box(lat, lon, km)))
    if not boxes:
        raise SystemExit("--world or --region")
    blobs, regions = {}, {}
    log = lambda s: print(s, flush=True)       # noqa: E731
    rows_all, prows_all, names, pnames = {}, {}, None, None
    for name, box in boxes:
        t1 = time.time()
        log(f"{name}: box {tuple(round(v, 4) for v in box)}")
        rows, names_, prows, pnames_ = build_region(con, metas, sparses, box, a.min_height, log)
        names, pnames = names_ or names, pnames_ or pnames
        rows_all.update((r[0], r) for r in rows)
        prows_all.update((r[0], r) for r in prows)
        regions[name] = {"box": [round(v, 5) for v in box], "buildings": len(rows), "parts": len(prows),
                         "s": round(time.time() - t1)}
    rows = sorted(rows_all.values(), key=lambda r: r[0])
    prows = sorted(prows_all.values(), key=lambda r: r[0])
    blobs = tile_blobs(rows, names, prows, pnames, log)
    fetched = round((sparses[0].bytes + sparses[1].bytes) / 1e6, 1)
    log(f"fetched {fetched} MB in {sparses[0].requests + sparses[1].requests} requests, {time.time() - t0:.0f} s")
    if a.out:
        m = write_local(a.out, blobs, release, a.min_height, regions)
        log(f"local: {len(m['tiles'])} tiles, {sum(t['bytes'] for t in m['tiles'].values()) / 1e6:.2f} MB")
    if a.pages:
        m = write_pages(a.pages, blobs, release, a.min_height, None if a.world else boxes[0][1] if len(boxes) == 1
                        else None)
        log(f"pages: {m['tiles']} tiles, {m['bytes'] / 1e6:.2f} MB in {len(m['parts'])} part(s), key {m['key']}")


if __name__ == "__main__":
    main()
