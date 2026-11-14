"""The tall-building index for the horizon ring (server bldhz.py, the user 2026-11-14: "buildings toward the real
horizon"): every Overture building at least --min-height tall (its own height, its storeys, or one of its parts),
with all its parts, in local shards, one a zoom-12 geotile.

    python3 tools/build_tall_index.py --footers DIR --out DIR --region NAME:LAT,LON,KM [--region ...] [--min-height 60]

--footers: the release's parquet footers already fetched (one file per Overture file, 'PAR1' + footer + its length +
'PAR1', under building/ and building_part/). Each file's row groups are chosen from them (their bbox and height
statistics), and only those row groups' byte ranges are fetched from Overture's S3, into sparse copies of the files
(--work, kept: a second run fetches nothing again) that DuckDB reads locally (file_row_number picks the row groups).

Shards: <out>/12/<x>_<y>.npz (allow_pickle=False: plain arrays) and <out>/manifest.json (format, release, zoom,
threshold, regions, each tile's buildings and the shard's SHA-1, which the server keys its tiles on). A building goes
to the tile of its bbox centre, its parts with it. Rings: the first outer ring of each, lon/lat in 1e-7 degrees (int32);
attribute columns as the server's map cells have them (overture.LAYERS: text "" missing, float32 NaN missing, bool).
Deterministic for a release: rows sorted by id, the npz written with fixed zip entries.

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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import overture  # noqa: E402

FORMAT = 1
Z = 12
UA = "bnge-tall-index"
FETCH_THREADS = 8
FETCH_TRIES = 5

B_SPEC = overture.LAYERS["buildings"]
P_SPEC = overture.LAYERS["building_parts"]
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
                  THEN CASE WHEN dictionary_page_offset > 4 THEN least(dictionary_page_offset, data_page_offset)
                       ELSE data_page_offset END END) a,
              max(CASE WHEN split_part(path_in_schema, ',', 1) IN ({tops})
                  THEN CASE WHEN dictionary_page_offset > 4 THEN least(dictionary_page_offset, data_page_offset)
                       ELSE data_page_offset END + total_compressed_size END) b
            FROM parquet_metadata('{os.path.join(footers, typ)}/*.parquet') GROUP BY ALL ORDER BY f, rg""").fetchall()
        self.rgs = {}           # name -> [(rg, first row, last row, x0, x1, y0, y1, hmax, fmax, a, b)]
        for f, rg, n, x0, x1, y0, y1, hmax, fmax, a, b in rows:
            lst = self.rgs.setdefault(f, [])
            first = lst[-1][2] + 1 if lst else 0
            lst.append((int(rg), first, first + int(n) - 1, x0, x1, y0, y1, hmax, fmax, int(a), int(b)))

    def select(self, box, hmin=None, fmin=None):
        """[(name, rg)] whose bbox statistics meet box (lon0, lat0, lon1, lat1) and, with hmin / fmin, whose height
        reaches hmin or storeys fmin (statistics missing: taken)."""
        lon0, lat0, lon1, lat1 = box
        out = []
        for f, lst in self.rgs.items():
            for rg, _a, _b, x0, x1, y0, y1, hmax, fmax, _o, _e in lst:
                if None in (x0, x1, y0, y1) or (x0 <= lon1 and x1 >= lon0 and y0 <= lat1 and y1 >= lat0):
                    if hmin is None and fmin is None:
                        out.append((f, rg))
                    elif (hmin is not None and (hmax is None or hmax >= hmin)) or (
                            fmin is not None and (fmax is None or fmax >= fmin)):
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

    def fill(self, pairs):
        """The row groups [(name, rg)] fetched where not yet (their needed columns' span, one request each)."""
        by = {}
        for f, rg in pairs:
            by.setdefault(f, set()).add(rg)
        jobs = []
        for f, rgs in sorted(by.items()):
            p, got = self._open(f)
            for rg, _a, _b, *_s, o, e in self.meta.rgs[f]:
                if rg in rgs and rg not in got:
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
            cond = " OR ".join(f"file_row_number BETWEEN {rr[rg][1]} AND {rr[rg][2]}" for rg in sorted(rgs))
            q = f"SELECT {proj} FROM read_parquet('{p}', file_row_number = true) WHERE {cond}"
            con.execute(f"CREATE TEMP TABLE {table} AS {q}" if first else f"INSERT INTO {table} {q}")
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
    import mapgeom as mg
    kind, parts = mg.parse_wkb(wkb)
    if kind != mg.POLYGON or not parts or not parts[0]:
        return None
    r = np.asarray(parts[0][0], np.float64)
    if len(r) > 1 and np.array_equal(r[0], r[-1]):
        r = r[:-1]
    if len(r) < 3:
        return None
    return np.round(r * 1e7).astype(np.int32)


def _columns(prefix, rows, names, spec):
    import mapgeom as mg
    at = {c: i for i, c in enumerate(names)}
    out = {f"{prefix}id": mg.text_column([r[at["id"]] for r in rows])}
    for c in spec.get("text", ()):
        out[f"{prefix}t_{c}"] = mg.text_column([r[at[c]] for r in rows])
    for c in spec.get("float", ()):
        out[f"{prefix}f_{c}"] = mg.float_column([r[at[c]] for r in rows])
    for c in spec.get("bool", ()):
        out[f"{prefix}b_{c}"] = mg.bool_column([r[at[c]] for r in rows])
    rings = [_ring(r[at["wkb"]]) for r in rows]
    off = np.zeros(len(rows) + 1, np.int64)
    off[1:] = np.cumsum([len(x) for x in rings])
    out[f"{prefix}ring_off"] = off
    out[f"{prefix}ll"] = np.concatenate(rings) if rings else np.zeros((0, 2), np.int32)
    return out


def write_npz(path, arrays):
    """arrays as an .npz with fixed zip entries (byte-identical for the same arrays)."""
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for k in sorted(arrays):
            buf = io.BytesIO()
            np.lib.format.write_array(buf, np.ascontiguousarray(arrays[k]), allow_pickle=False)
            zi = zipfile.ZipInfo(k + ".npy", date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, buf.getvalue())
    os.replace(tmp, path)


def build_region(con, metas, sparses, box, min_h, log):
    """(buildings rows, names, parts rows, names) of the region's tall buildings and all their parts."""
    mb, mp = metas
    sb, sp = sparses
    lon0, lat0, lon1, lat1 = box
    inbox = f"bbox.xmin <= {lon1!r} AND bbox.xmax >= {lon0!r} AND bbox.ymin <= {lat1!r} AND bbox.ymax >= {lat0!r}"
    fmin = math.ceil(min_h / 3.2)
    # 1. the parts: every part row group in the region (the parts' data is small), the tall ones' buildings
    pp = mp.select(box)
    t0 = time.time()
    sp.fill(pp)
    log(f"  parts: {len(pp)} row groups, {sp.requests} requests {sp.bytes / 1e6:.1f} MB, {time.time() - t0:.0f} s")
    tall_parts = []
    if sp.load(con, "pall", "building_id, height, min_height, bbox, (bbox.xmin + bbox.xmax) / 2 AS cx, "
                            "(bbox.ymin + bbox.ymax) / 2 AS cy", pp):
        tall_parts = con.execute(f"""SELECT building_id, min(cx), max(cx), min(cy), max(cy) FROM pall
                                     WHERE {inbox} AND coalesce(height, 0) >= {min_h}
                                     GROUP BY building_id ORDER BY 1""").fetchall()
    # 2. the buildings tall themselves (height, or storeys where no height)
    bp = mb.select(box, hmin=min_h, fmin=fmin)
    t0 = time.time()
    r0, b0 = sb.requests, sb.bytes
    sb.fill(bp)
    log(f"  buildings: {len(bp)} row groups, {sb.requests - r0} requests {(sb.bytes - b0) / 1e6:.1f} MB, "
        f"{time.time() - t0:.0f} s")
    rows, names = _rows(con, B_SPEC, sb, bp, f"{inbox} AND (height >= {min_h} OR (height IS NULL AND "
                                              f"num_floors >= {fmin}))")
    have = {r[0] for r in rows}
    # 3. the buildings tall only through their parts: each from the row groups round its parts
    miss = [t for t in tall_parts if t[0] not in have]
    extra = set()
    for bid, x0, x1, y0, y1 in miss:
        extra.update(mb.select((x0 - 1e-4, y0 - 1e-4, x1 + 1e-4, y1 + 1e-4)))
    extra -= set(bp)
    r0, b0 = sb.requests, sb.bytes
    sb.fill(sorted(extra))
    log(f"  {len(miss)} buildings tall by their parts: {len(extra)} more row groups, {sb.requests - r0} requests "
        f"{(sb.bytes - b0) / 1e6:.1f} MB")
    if miss:
        ids = ", ".join("'" + m[0].replace("'", "''") + "'" for m in miss)
        more, _names = _rows(con, B_SPEC, sb, sorted(set(bp) | extra), f"id IN ({ids})")
        got = {r[0] for r in more}
        rows = sorted({r[0]: r for r in rows + more}.values(), key=lambda r: r[0])
        lost = len(miss) - len(got)
        if lost:
            log(f"  {lost} part-tall buildings not found round their parts")
    # 4. every part of those buildings
    bids = sorted({r[0] for r in rows})
    con.execute("DROP TABLE IF EXISTS want")
    con.execute("CREATE TEMP TABLE want (id VARCHAR)")
    con.executemany("INSERT INTO want VALUES (?)", [(b,) for b in bids])
    prows, pnames = _rows(con, P_SPEC, sp, pp, f"{inbox} AND building_id IN (SELECT id FROM want)")
    return rows, names, prows, pnames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--footers", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--work", default=None, help="sparse copies (default <out>/../tall_index_work)")
    ap.add_argument("--region", action="append", required=True, help="NAME:LAT,LON,KM (a square of 2 KM)")
    ap.add_argument("--min-height", type=float, default=60.0)
    ap.add_argument("--stac", default=None)
    a = ap.parse_args()
    import duckdb
    import geotiles
    stac_path = a.stac or overture.BUNDLED.format(release=overture.RELEASE)
    stac = json.load(gzip.open(stac_path))
    release = stac["release"]
    work = a.work or os.path.join(os.path.dirname(os.path.abspath(a.out)), "tall_index_work")
    os.makedirs(os.path.join(a.out, str(Z)), exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads = 4")
    metas = (Meta(con, a.footers, "building", B_TOP, stac["collections"]["buildings/building"]["files"]),
             Meta(con, a.footers, "building_part", P_TOP, stac["collections"]["buildings/building_part"]["files"]))
    sparses = (Sparse(work, metas[0]), Sparse(work, metas[1]))
    mpath = os.path.join(a.out, "manifest.json")
    manifest = json.load(open(mpath)) if os.path.exists(mpath) else {}
    if manifest and (manifest.get("release") != release or manifest.get("min_height") != a.min_height
                     or manifest.get("format") != FORMAT):
        raise SystemExit(f"{mpath}: another release / threshold / format: use a new --out")
    manifest.update({"format": FORMAT, "release": release, "z": Z, "min_height": a.min_height})
    tiles = manifest.setdefault("tiles", {})
    regions = manifest.setdefault("regions", {})
    for spec in a.region:
        name, rest = spec.split(":", 1)
        lat, lon, km = map(float, rest.split(","))
        box = region_box(lat, lon, km)
        t0 = time.time()
        print(f"{name}: box {tuple(round(v, 4) for v in box)}", flush=True)
        rows, names, prows, pnames = build_region(con, metas, sparses, box, a.min_height,
                                                  lambda s: print(s, flush=True))
        at = {c: i for i, c in enumerate(names)}
        bx, by = geotiles.tile_of(np.array([r[at["cy"]] for r in rows], np.float64),
                                  np.array([r[at["cx"]] for r in rows], np.float64), Z)
        tile_of = {r[0]: (int(x), int(y)) for r, x, y in zip(rows, np.atleast_1d(bx), np.atleast_1d(by))}
        pat = {c: i for i, c in enumerate(pnames)} if pnames else {}
        nb = np_ = 0
        for key in sorted(set(tile_of.values())):
            brs = [r for r in rows if tile_of[r[0]] == key and _ring(r[at["wkb"]]) is not None]
            mine = {r[0] for r in brs}
            prs = [r for r in prows if r[pat["building_id"]] in mine and _ring(r[pat["wkb"]]) is not None]
            arrays = _columns("b_", brs, names, B_SPEC)
            if prs:
                arrays.update(_columns("p_", prs, pnames, P_SPEC))
            kx, ky = key
            path = os.path.join(a.out, str(Z), f"{kx}_{ky}.npz")
            if os.path.exists(path):        # (a tile two regions share: kept when it holds this region's already)
                with np.load(path, allow_pickle=False) as old:
                    if set(old["b_id"].tolist()) >= mine:
                        continue
                print(f"  {kx}_{ky}: rewritten with this region's buildings (regions overlap: give one larger "
                      f"region instead)", flush=True)
            write_npz(path, arrays)
            with open(path, "rb") as fh:
                sha = hashlib.sha1(fh.read()).hexdigest()
            tiles[geotiles.key(kx, ky, Z)] = {"n": len(brs), "parts": len(prs), "sha1": sha,
                                              "bytes": os.path.getsize(path)}
            nb += len(brs)
            np_ += len(prs)
        regions[name] = {"lat": lat, "lon": lon, "km": km, "buildings": len(rows), "parts": len(prows),
                         "fetched_MB": round((sparses[0].bytes + sparses[1].bytes) / 1e6, 1),
                         "requests": sparses[0].requests + sparses[1].requests, "s": round(time.time() - t0)}
        print(f"{name}: {len(rows)} buildings, {len(prows)} parts in {len(set(tile_of.values()))} tiles "
              f"({nb} / {np_} written), {time.time() - t0:.0f} s", flush=True)
        sparses[0].bytes = sparses[0].requests = sparses[1].bytes = sparses[1].requests = 0
    with open(mpath + ".tmp", "w") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    os.replace(mpath + ".tmp", mpath)
    tot = sum(t["bytes"] for t in tiles.values())
    print(f"manifest: {len(tiles)} tiles, {tot / 1e3:.0f} KB")


if __name__ == "__main__":
    main()
