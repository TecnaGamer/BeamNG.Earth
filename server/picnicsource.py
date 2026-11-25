"""Picnic tables from OpenStreetMap, for a map cell.

Overture's infrastructure class list includes picnic_table, but release 2026-09-23.1 does not
populate it: none of the cells cached from a drive had one, and Stagecoach Park (Carlsbad) is a
land-use park whose mapped tables are only in OSM. Layercake's POIs file carries leisure, amenity
and tourism (https://data.openstreetmap.us/layercake/pois.parquet, ODbL). Like osmtags, only the
row groups whose bbox touches the cell are read, and geometry only for a group that has a picnic
row. A cell is remembered for the session.
"""
import json
import logging
import os
import re
import threading
import time

import numpy as np

import mapgeom
import overture
import rangeread

log = logging.getLogger("earthd.picnic")

URL = "https://data.openstreetmap.us/layercake/pois.parquet"
PATH = "osm/pois.parquet"
VERSION_TTL_S = 7 * 86400
RETRY_S = 3600
COLUMNS = ("id", "leisure", "amenity", "tourism")
TABLES = {"picnic_table"}
SITES = {"picnic_site"}
GRILLS = {"bbq", "barbecue", "bbq_grill"}


class Source:
    """Picnic features of a map cell, from the pinned Layercake POIs version."""

    def __init__(self, reader, fetcher, url=URL):
        self.reader, self.fetcher, self.url = reader, fetcher, url
        self.raw_dir = reader.raw_dir
        self._lock = threading.Lock()
        self._remote = None
        self._checked = 0.0
        self._meta = None           # (RemoteFile, row groups)
        self._cells = {}            # (etag, z, x, y) -> Features
        self._down = False
        self._ready = {}            # (z, x, y) -> Features read (cells(wait=False))
        self._threads = {}          # (z, x, y) -> its background read

    def cells(self, keys, kind="demand", wait=True):
        """Features of the picnic rows in these map cells (an empty list when the file cannot be read). wait=False (a
        road tile): only cells already read in this process; the others are read in the background, for the tile's
        next build (a first visit's road tiles waited for this file's ~26 MB, 2026-11-03: minutes on a slow line)."""
        found = []
        for key in keys or ():
            if wait:
                feats = self._cell(key, kind)
            else:
                k = (key.z, key.x, key.y)
                feats = self._ready.get(k)
                if feats is None:
                    self._later(key)        # never waited for: the next build (its read began at the build's start)
                    feats = self._ready.get(k)
            if feats is not None and len(feats):
                found.append(feats)
        return found

    def prefetch(self, keys):
        """The background reads of these cells started now (a road tile's build start), without waiting: by the time the
        tile asks for them (cells(wait=False), at the end of its build) they are mostly there."""
        for key in keys or ():
            if (key.z, key.x, key.y) not in self._ready:
                self._later(key)

    def _later(self, key):
        """The background read of cell key (started once): its thread, or None (the file is down)."""
        k = (key.z, key.x, key.y)
        with self._lock:
            if self._down:
                return None
            t = self._threads.get(k)
            if t is None:
                def run():
                    feats = self._cell(key, "demand")
                    if feats is not None:
                        self._ready[k] = feats
                t = self._threads[k] = threading.Thread(target=run, name="picnic-osm", daemon=True)
                t.start()
            return t

    def _cell(self, key, kind):
        import cip
        got = cip.side_json(cip.any_reader(), "f:picnic", key)
        if got is not None:             # (the cell's rows as the builder read them: CIP on)
            return features_of([(int(fid), le, am, to, bytes.fromhex(g)) for fid, le, am, to, g in got])
        if self._down:
            return None
        for attempt in (0, 1):
            try:
                etag, remote = self._version(kind)
            except Exception as ex:
                self._down = True
                log.warning("picnic tables: OSM pois unavailable (%s: %s)", type(ex).__name__, ex)
                return None
            cached = self._cells.get((etag, key.z, key.x, key.y))
            if cached is not None:
                return cached
            try:
                rows = _rows_load(remote, key)
                if rows is None:
                    rows = self._query(remote, key.bounds(), kind)
                    _rows_save(remote, key, rows)
                feats = features_of(rows)
            except rangeread.Changed:
                if attempt:
                    raise
                log.info("picnic tables: %s changed (was %s)", self.url, etag)
                self._forget()
                continue
            except Exception as ex:
                self._down = True
                log.warning("picnic tables: OSM pois failed (%s: %s)", type(ex).__name__, ex)
                return None
            self._cells[(etag, key.z, key.x, key.y)] = feats
            return feats
        return None

    def _version(self, kind):
        with self._lock:
            if self._remote is not None and time.time() - self._checked < VERSION_TTL_S:
                return self._remote
            # (a pin past VERSION_TTL_S stays in use when the server cannot be asked: stale-if-error)
            etag, size, fresh_for = rangeread.pinned_version(
                self.fetcher, self.url, os.path.join(self.raw_dir, "picnicsource"), kind, VERSION_TTL_S, RETRY_S,
                f"picnic tables: {self.url}")
            d = os.path.join(self.raw_dir, "picnicsource", re.sub(r"[^A-Za-z0-9]+", "_", etag).strip("_"))
            self._remote = (etag, rangeread.RemoteFile([self.url], size, d, self.fetcher, if_match=etag))
            self._checked = time.time() - VERSION_TTL_S + fresh_for     # (asked again after fresh_for)
            return self._remote

    def _forget(self):
        with self._lock:
            self._remote = None
            self._meta = None
            self._cells.clear()
            try:
                os.remove(os.path.join(self.raw_dir, "picnicsource", "pinned"))
            except OSError:
                pass

    def _groups(self, con, uri, remote, kind):
        with self._lock:
            got = self._meta
        if got is not None and got[0] is remote:
            return got[1]
        # (parsed once per version on disk, as osmtags does: each server process parsed the planet file's footer)
        groups = overture.parquet_row_groups(con, uri, remote, kind)
        with self._lock:
            self._meta = (remote, groups)
        return groups

    def _query(self, remote, bounds, kind):
        lon0, lat0, lon1, lat1 = bounds
        rd = self.reader
        files = {PATH: remote}
        con, proto, errors = rd._connect(files, kind)
        try:
            uri = f"'{proto}://{PATH}'"
            groups = self._groups(con, uri, remote, kind)
            cand = _candidates(groups, bounds)
            if not cand:
                return []
            want = set(COLUMNS) | {"bbox"}
            rd._fetch(files, {PATH: [r for rg in cand for c, rs in groups[rg]["cols"].items() if c in want
                                     for r in rs]}, kind)
            only = " OR ".join(f"file_row_number BETWEEN {groups[rg]['first']} AND "
                               f"{groups[rg]['first'] + groups[rg]['n'] - 1}" for rg in cand)
            box = (f"bbox.xmin <= {lon1!r} AND bbox.xmax >= {lon0!r} AND bbox.ymin <= {lat1!r} "
                   f"AND bbox.ymax >= {lat0!r}")
            mark = ("lower(leisure) IN ('picnic_table', 'picnic_site') OR "
                    "lower(amenity) IN ('picnic_table', 'bbq', 'barbecue', 'bbq_grill') OR "
                    "lower(tourism) = 'picnic_site'")
            rows = con.execute(f"""SELECT id, leisure, amenity, tourism, file_row_number
                FROM read_parquet({uri}, file_row_number = true)
                WHERE ({only}) AND {box} AND ({mark})""").fetchall()
            if not rows:
                return []
            hit = sorted({rg for row in rows for rg in cand
                          if groups[rg]["first"] <= row[4] < groups[rg]["first"] + groups[rg]["n"]})
            rd._fetch(files, {PATH: [r for rg in hit for span in groups[rg]["cols"].get("geometry", [])
                                     for r in (span,)]}, kind)
            try:
                con.execute("SET enable_geoparquet_conversion = false")
            except Exception:
                pass
            nums = ", ".join(str(int(row[4])) for row in rows)
            geoms = dict(con.execute(f"""SELECT file_row_number, geometry
                FROM read_parquet({uri}, file_row_number = true)
                WHERE file_row_number IN ({nums})""").fetchall())
        except Exception:
            for e in errors:
                if isinstance(e, rangeread.Changed):
                    raise e
            raise
        finally:
            con.close()
        out = []
        for fid, leisure, amenity, tourism, num in rows:
            geom = geoms.get(num)
            if geom is None:
                continue
            out.append((int(fid), leisure, amenity, tourism, bytes(geom)))
        return out

    def store_many(self, keys, kind="demand"):
        """The rows of cells keys read in one scan of their union's row groups and kept as _cell keeps each cell's
        (_rows_save; tools/build_cip.py: a z10 unit's 256 cells were 256 scans of the same row groups, ~0.1 s each).
        Each cell's rows are _query's for it: its box tested in DuckDB with the same expression, the rows in file order
        (_query's single-threaded scan order). Errors are raised (the builder records the cells as lacking)."""
        keys = list(keys)
        for attempt in (0, 1):
            etag, remote = self._version(kind)
            todo = [k for k in keys if _rows_load(remote, k) is None]
            try:
                got = self._query_many(remote, [k.bounds() for k in todo], kind) if todo else []
            except rangeread.Changed:
                if attempt:
                    raise
                log.info("picnic tables: %s changed (was %s)", self.url, etag)
                self._forget()
                continue
            for k, rows in zip(todo, got):
                _rows_save(remote, k, rows)
            return len(todo)
        return 0

    def _query_many(self, remote, bounds_list, kind):
        out = [[] for _ in bounds_list]
        if not bounds_list:
            return out
        u = (min(b[0] for b in bounds_list), min(b[1] for b in bounds_list), max(b[2] for b in bounds_list),
             max(b[3] for b in bounds_list))
        rd = self.reader
        files = {PATH: remote}
        con, proto, errors = rd._connect(files, kind)

        def box(b):
            lon0, lat0, lon1, lat1 = b
            return (f"bbox.xmin <= {lon1!r} AND bbox.xmax >= {lon0!r} AND bbox.ymin <= {lat1!r} "
                    f"AND bbox.ymax >= {lat0!r}")
        try:
            uri = f"'{proto}://{PATH}'"
            groups = self._groups(con, uri, remote, kind)
            cand = _candidates(groups, u)
            if not cand:
                return out
            want = set(COLUMNS) | {"bbox"}
            rd._fetch(files, {PATH: [r for rg in cand for c, rs in groups[rg]["cols"].items() if c in want
                                     for r in rs]}, kind)
            only = " OR ".join(f"file_row_number BETWEEN {groups[rg]['first']} AND "
                               f"{groups[rg]['first'] + groups[rg]['n'] - 1}" for rg in cand)
            mark = ("lower(leisure) IN ('picnic_table', 'picnic_site') OR "
                    "lower(amenity) IN ('picnic_table', 'bbq', 'barbecue', 'bbq_grill') OR "
                    "lower(tourism) = 'picnic_site'")
            con.execute(f"""CREATE TEMP TABLE pu AS SELECT id, leisure, amenity, tourism, file_row_number, bbox
                FROM read_parquet({uri}, file_row_number = true) WHERE ({only}) AND {box(u)} AND ({mark})""")
            per = [con.execute(f"""SELECT id, leisure, amenity, tourism, file_row_number FROM pu WHERE {box(b)}
                ORDER BY file_row_number""").fetchall() for b in bounds_list]
            nums = sorted({int(row[4]) for rows in per for row in rows})
            if not nums:
                return out
            hit = sorted({rg for n in nums for rg in cand
                          if groups[rg]["first"] <= n < groups[rg]["first"] + groups[rg]["n"]})
            rd._fetch(files, {PATH: [r for rg in hit for span in groups[rg]["cols"].get("geometry", [])
                                     for r in (span,)]}, kind)
            try:
                con.execute("SET enable_geoparquet_conversion = false")
            except Exception:
                pass
            geoms = dict(con.execute(f"""SELECT file_row_number, geometry
                FROM read_parquet({uri}, file_row_number = true)
                WHERE file_row_number IN ({', '.join(map(str, nums))})""").fetchall())
        except Exception:
            for e in errors:
                if isinstance(e, rangeread.Changed):
                    raise e
            raise
        finally:
            con.close()
        for i, rows in enumerate(per):
            out[i] = [(int(fid), le, am, to, bytes(geoms[num])) for fid, le, am, to, num in rows
                      if geoms.get(num) is not None]
        return out


def _rows_path(remote, key):
    return os.path.join(remote.cache_dir, "cells", str(key.z), str(key.x), f"{key.y}.json")


def _rows_load(remote, key):
    """A cell's picnic rows kept from an earlier read of this version (_rows_save), or None. Every server process
    queried each cell's row groups again (a scan of the POI file's few columns per cell, ~1.3 CPU-s of a warm Le Mans
    block + 4 road tiles, 2026-11-11), and every run after the first."""
    try:
        with open(_rows_path(remote, key), encoding="utf-8") as f:
            return [(int(fid), le, am, to, bytes.fromhex(g)) for fid, le, am, to, g in json.load(f)]
    except (OSError, ValueError, TypeError):
        return None


def _rows_save(remote, key, rows):
    path = _rows_path(remote, key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([[int(fid), le, am, to, bytes(g).hex()] for fid, le, am, to, g in rows], f)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as e:
        log.debug("picnic rows of %s not kept: %s", key, e)


def _candidates(groups, bounds):
    lon0, lat0, lon1, lat1 = bounds

    def stat(g, leaf, i, default):
        try:
            return float(g["bbox"][leaf][i])
        except (KeyError, TypeError, ValueError):
            return default
    return [rg for rg, g in sorted(groups.items())
            if stat(g, "xmin", 0, -1e9) <= lon1 and stat(g, "xmax", 1, 1e9) >= lon0
            and stat(g, "ymin", 0, -1e9) <= lat1 and stat(g, "ymax", 1, 1e9) >= lat0]


def _mark(leisure, amenity, tourism):
    """(class, tags) of a picnic row, else None."""
    leisure = str(leisure or "").strip().lower()
    amenity = str(amenity or "").strip().lower()
    tourism = str(tourism or "").strip().lower()
    tags = {}
    if leisure:
        tags["leisure"] = leisure
    if amenity:
        tags["amenity"] = amenity
    if tourism:
        tags["tourism"] = tourism
    if leisure in TABLES or amenity in TABLES:
        return "picnic_table", tags
    if tourism in SITES or leisure in SITES:
        return "picnic_site", tags
    if amenity in GRILLS:
        return "bbq", tags
    return None


def features_of(rows):
    """Features of (id, leisure, amenity, tourism, wkb) rows. Ids sort as osm-picnic-<id>."""
    built = []
    for fid, leisure, amenity, tourism, wkb in rows:
        mark = _mark(leisure, amenity, tourism)
        if mark is None or not wkb:
            continue
        try:
            kind, parts = mapgeom.parse_wkb(wkb)
        except Exception:
            continue
        if kind not in (mapgeom.POINT, mapgeom.POLYGON) or not parts:
            continue
        built.append((int(fid), kind, parts, mark[0], mark[1]))
    built.sort(key=lambda r: r[0])
    if not built:
        return mapgeom.empty("infrastructure")
    ids = mapgeom.text_column([f"osm-picnic-{fid:012d}" for fid, *_ in built])
    feats = mapgeom.build("infrastructure", ids, [(k, p) for _, k, p, _, _ in built])
    feats.attrs = {
        "class": mapgeom.text_column([cls for *_, cls, _ in built]),
        "subtype": mapgeom.text_column(["recreation"] * len(built)),
        "tags": mapgeom.text_column([json.dumps(tags) for *_, tags in built]),
    }
    return feats
