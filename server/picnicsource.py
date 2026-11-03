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

    def cells(self, keys, kind="demand"):
        """Features of the picnic rows in these map cells (an empty list when the file cannot be read)."""
        found = []
        for key in keys or ():
            feats = self._cell(key, kind)
            if feats is not None and len(feats):
                found.append(feats)
        return found

    def _cell(self, key, kind):
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
                feats = features_of(self._query(remote, key.bounds(), kind))
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
            pin = os.path.join(self.raw_dir, "picnicsource", "pinned")
            etag = size = None
            try:
                with open(pin) as f:
                    etag, size, when = f.read().split("\n")[:3]
                    size, when = int(size), float(when)
                if time.time() - when > VERSION_TTL_S:
                    etag = None
            except (OSError, ValueError):
                etag = None
            if etag is None:
                down = os.path.join(self.raw_dir, "picnicsource", "unreachable")
                try:
                    recent = time.time() - os.path.getmtime(down) < RETRY_S
                except OSError:
                    recent = False
                if recent:
                    raise rangeread.RangeError(f"{self.url}: unreachable at the last try")
                try:
                    r = self.fetcher.get(self.url, kind=kind, headers={"Range": "bytes=0-15"})
                    etag = r.headers.get("etag", "").strip()
                    m = rangeread.CONTENT_RANGE.match(r.headers.get("content-range", ""))
                    if not etag or not m or m.group(3) == "*":
                        raise rangeread.RangeError(f"{self.url}: no ETag or size")
                except Exception:
                    os.makedirs(os.path.dirname(down), exist_ok=True)
                    open(down, "w").close()
                    raise
                size = int(m.group(3))
                os.makedirs(os.path.dirname(pin), exist_ok=True)
                with open(pin + ".tmp", "w") as f:
                    f.write(f"{etag}\n{size}\n{time.time()}\n")
                os.replace(pin + ".tmp", pin)
            d = os.path.join(self.raw_dir, "picnicsource", re.sub(r"[^A-Za-z0-9]+", "_", etag).strip("_"))
            self._remote = (etag, rangeread.RemoteFile([self.url], size, d, self.fetcher, if_match=etag))
            self._checked = time.time()
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
        tail = remote.read(max(0, remote.size - overture.FOOTER_GUESS), remote.size, kind)
        n = int.from_bytes(tail[-8:-4], "little")
        if n + 8 > len(tail):
            remote.read(remote.size - n - 8, remote.size, kind)
        meta = con.execute(f"""SELECT row_group_id, row_group_num_rows, path_in_schema, stats_min, stats_max,
            dictionary_page_offset, data_page_offset, total_compressed_size FROM parquet_metadata({uri})
            ORDER BY row_group_id, column_id""").fetchall()
        groups, first = {}, 0
        for rg, nrows, col, smin, smax, dict_off, data_off, size in meta:
            g = groups.get(rg)
            if g is None:
                g = groups[rg] = {"first": first, "n": nrows, "bbox": {}, "cols": {}}
                first += nrows
            offs = [o for o in (dict_off, data_off) if o]
            if not offs:
                continue
            top, leaf = col.split(", ")[0], col.split(", ")[-1]
            g["cols"].setdefault(top, []).append((min(offs), min(offs) + size))
            if top == "bbox":
                g["bbox"][leaf] = (smin, smax)
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
