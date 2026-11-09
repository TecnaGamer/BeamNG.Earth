"""OpenStreetMap road tags (lane count, width, sidewalks, speed limit, lighting, smoothness...) for a map cell's
roads, joined to Overture's road segments by their OpenStreetMap way id (the user, 2026-10-22: "map data sources that
have more info about the roads, lane count, road types").

Overture's transportation theme has no lane count (removed in 2024, not back in schema v2.0.0) and few widths, but
98.6 % of its segments come from OpenStreetMap and keep the way id (sources[1].record_id "w<id>@<version>"). OSM US's
Layercake publishes every OSM highway as one GeoParquet file (https://layercake.openstreetmap.us/layers/highways/,
ODbL, ~29 GB, 2,467 row groups, spatially clustered, rebuilt about weekly), with the tags as columns and a bbox struct.
Like overture.py, only what a cell needs is read, through rangeread (netfetch's limits, the block cache), with DuckDB
reading the blocks: the footer once (9 MB), the bbox columns of the row groups whose statistics touch the cell, then
the tag columns of the row groups that hold a matching way. Tagged highways also supply geometry for matching
replaced source way IDs. The tag values are parsed in the query
(lanes must be a whole count, width "7 m" / "23'" -> metres, maxspeed "30 mph" -> km/h).

The file is rewritten in place: a version is pinned by its ETag (the cache directory; every range request carries
If-Match, rangeread.Changed when it moved on), and the next version is taken on the next start (version()).

A layer of its own is not needed: mapdata joins the tags into the roads layer when it builds a cell (all processes
read the same cell file, so a road's lanes and width agree everywhere). Unreachable: the cell keeps no tags and the
roads use their width guesses (roads.carriageway), as before.
"""
import json
import logging
import math
import os
import zipfile
import re
import threading
import time

import numpy as np

import netfetch
import overture
import rangeread
import xproc

log = logging.getLogger("earthd.mapdata")

URL = "https://data.openstreetmap.us/layercake/highways.parquet"
HOST = "data.openstreetmap.us"
VERSION_TTL_S = 7 * 86400       # a pinned version is checked again after this long (the file changes ~weekly)
RETRY_S = 3600                  # the source unreachable: asked again after this long (offline, blocked, gone)
PATH = "osm/highways.parquet"   # (the path DuckDB reads it under)

# The query: OSM way id and the parsed tags (NULL where absent or not understood).
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
    # for the segments Overture took from a relation (no way id: Samara's Novo-Sadovaya, "r3851280@53", drawn 10 m wide
    # instead of its 6 lanes, the user 2026-10-24): the way of the same name around them (join)
    "name": "name",
    # a road not built yet (GitHub #184: a trunk road under construction, opening 2026-12-31, drawn as a finished road;
    # Overture calls highway=construction "unknown"): only these two values, every other highway NULL
    "osm_highway": "CASE WHEN highway IN ('construction', 'proposed') THEN highway END",
    # a motorway's or trunk's (and their links') class: their current geometry is read to match ways Overture's
    # release still names by an older id (roadlanes.join, PR #471)
    "lane_hw": "CASE WHEN highway IN ('motorway', 'trunk', 'motorway_link', 'trunk_link') THEN highway END",
    "bx0": "bbox.xmin", "by0": "bbox.ymin", "bx1": "bbox.xmax", "by1": "bbox.ymax",
}
TEXT_COLUMNS = {"sidewalk", "lit", "smoothness", "osm_surface", "osm_oneway", "name", "osm_highway", "lane_hw"}
INT_COLUMNS = {"lanes", "osm_layer"}
GROUPS_FILE = "groups.json"     # the version's parsed row groups (OsmTags._groups)
TABLE_DIR = "rg"        # a row group's decoded ways (OsmTags._table), in the version's folder
TABLE_MEM = 6           # decoded row groups kept in memory (~6 MB each; a place's cells share 3-5)
EXACT32 = {"bx0", "by0", "bx1", "by1", "lanes", "osm_layer"}   # float32 (the file's bbox; small integers)
TABLE_WAIT_S = 60.0     # a row group another process decodes is waited for this long
USED = {"id", "bbox", "lanes", "width", "maxspeed", "sidewalk", "lit", "smoothness", "surface", "oneway", "layer", "name"}
FLOATS = ("lanes", "width", "maxspeed", "osm_layer")
TEXTS = ("sidewalk", "lit", "smoothness", "osm_surface", "osm_oneway", "osm_highway")
GEOMETRY_MARGIN_M = 6.0    # m round a tagged highway's bbox a replaced way's line may reach (roadlanes.join's 6 m)
UNBUILT = ("construction", "proposed")      # osm_highway's values: not drawn (roads.unbuilt)
NAME_MATCH_M = 25.0     # m a segment's middle may lie outside the bbox of the same-named way it takes the tags of
ONEWAY_YES = ("yes", "1", "-1", "true")


# Racetracks (GitHub #143) and bus-only carriageways: Overture's transportation theme has no highway=raceway (Daytona's
# oval and road course, the Nordschleife: nothing) and no highway=busway (the Dutch Zuidtangent). Both come from the
# same Layercake file with their geometry, read only from the row groups that contain one.
RACEWAY_COLUMNS = ("id", "highway", "name", "surface", "width", "lanes", "layer", "bridge", "tunnel", "oneway")


def way_id(record):
    """The OSM way id of an Overture source record ("w1360453999@2" -> 1360453999), else None."""
    m = re.match(r"^w(\d+)", record or "")
    return int(m.group(1)) if m else None


class OsmTags:
    """Tags by OSM way id per map cell, from the pinned Layercake version."""

    DIR = "osmtags"             # the pinned version's and its blocks' folder under raw_dir (osmfuel.OsmFuel: its own)

    def __init__(self, raw_dir, fetcher, url=URL):
        self.raw_dir, self.fetcher, self.url = raw_dir, fetcher, url
        self._lock = threading.Lock()
        self._remote = None         # (etag, RemoteFile)
        self._checked = 0.0
        self._reader = overture.Reader(raw_dir, fetcher)
        self._meta = None           # (RemoteFile, row groups) of the version in use: _groups
        self._tables = {}           # (id(RemoteFile), row group) -> its decoded ways (_table), least recent first

    def version(self, kind="demand"):
        """(etag, RemoteFile) of the version in use: the one pinned on disk while fresh, else the server's."""
        with self._lock:
            if self._remote is not None and time.time() - self._checked < VERSION_TTL_S:
                return self._remote
            pin = os.path.join(self.raw_dir, self.DIR, "pinned")
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
                down = os.path.join(self.raw_dir, self.DIR, "unreachable")
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
            d = os.path.join(self.raw_dir, self.DIR, re.sub(r"[^A-Za-z0-9]+", "_", etag).strip("_"))
            self._remote = (etag, rangeread.RemoteFile([self.url], size, d, self.fetcher, if_match=etag))
            self._checked = time.time()
            return self._remote

    def _forget(self):
        with self._lock:
            self._remote = None
            try:
                os.remove(os.path.join(self.raw_dir, self.DIR, "pinned"))
            except OSError:
                pass

    def cell(self, bounds, kind="demand"):
        """({way id: {column: value}} of the OSM highways whose bbox touches bounds (lon0, lat0, lon1, lat1), [the
        highway=raceway and highway=busway ways among them: {"id", "coords" (k, 2) lon/lat, tags...}]): one pass for
        both. A version that changed under the reads is dropped and the cell asked once more on the new one."""
        for attempt in (0, 1):
            etag, remote = self.version(kind)
            try:
                return self._cell(remote, bounds, kind)
            except rangeread.Changed:
                if attempt:
                    raise
                log.info("osmtags: %s changed (was %s), taking the new version", self.url, etag)
                self._forget()
        return {}, []

    def highway_geometry(self, bounds, kind="demand", need=None):
        """{way id: (k, 2) lon/lat} of the tagged motorways / trunks (and their links) with a lane count whose bbox
        touches bounds: PR #471's match for a highway whose Overture way id OSM no longer has (roadlanes.join). Asked
        only then (roadlanes.join's geometry callback): the geometry column of their row groups was ~33 MB more of a
        cold downtown Los Angeles visit (4 road tiles, 2026-11-04) when every cell read it. need: [(lon0, lat0, lon1,
        lat1)] of the highways to match: only the ways whose bbox (GEOMETRY_MARGIN_M round it) holds one of them
        (roadlanes.join's own first test) and their row groups' geometry are read; none: nothing is read."""
        lon0, lat0, lon1, lat1 = bounds
        etag, remote = self.version(kind)
        rd = self._reader
        files = {PATH: remote}
        con, proto, errors = rd._connect(files, kind)
        out = {}
        try:
            uri = f"'{proto}://{PATH}'"
            groups = self._groups(con, uri, remote, kind)
            cand = self._candidates(groups, bounds)
            names = list(COLUMNS)
            hw = names.index("lane_hw")
            lanes = names.index("lanes")
            lane_rows = []
            for rg in cand:
                t = self._table(con, uri, groups, rg, remote, kind)
                if not len(t["id"]):
                    continue
                b = np.float32
                m = np.flatnonzero((t["bx0"].astype(b) <= b(lon1)) & (t["bx1"].astype(b) >= b(lon0))
                                   & (t["by0"].astype(b) <= b(lat1)) & (t["by1"].astype(b) >= b(lat0)))
                for r in _rows(t, m):
                    if r[_ROW_VAL + lanes] is None or r[_ROW_VAL + hw] is None:
                        continue
                    if need is not None:
                        x0, y0, x1, y1 = (r[_ROW_VAL + names.index(c)] for c in ("bx0", "by0", "bx1", "by1"))
                        dx = GEOMETRY_MARGIN_M / (111320.0 * max(0.1, float(np.cos(np.radians((y0 + y1) / 2)))))
                        dy = GEOMETRY_MARGIN_M / 110574.0
                        if not any(x0 - dx <= a0 and x1 + dx >= a1 and y0 - dy <= b0 and y1 + dy >= b1
                                   for a0, b0, a1, b1 in need):
                            continue
                    lane_rows.append(r[_ROW_FRN])
            if not lane_rows or not any('geometry' in groups[rg]['cols'] for rg in cand):
                return out
            hit = sorted({rg for r in lane_rows for rg in cand
                          if groups[rg]['first'] <= r < groups[rg]['first'] + groups[rg]['n']})
            rd._fetch(files, {PATH: [v for rg in hit for v in groups[rg]['cols'].get('geometry', [])]}, kind)
            try:
                con.execute('SET enable_geoparquet_conversion = false')
            except Exception:
                pass
            wanted = ', '.join(str(int(r)) for r in lane_rows)
            for wid, wkb in con.execute(f'SELECT id, geometry FROM read_parquet({uri}, file_row_number=true) '
                                        f'WHERE file_row_number IN ({wanted})').fetchall():
                if wkb is not None:
                    p = _linestring(bytes(wkb))
                    if p is not None:
                        out[int(wid)] = p
        except rangeread.Changed:
            raise
        except Exception as e:
            for err in errors:
                if isinstance(err, rangeread.Changed):
                    raise err
            log.warning('osmtags: highway geometry unavailable; keeping way tags: %s', e)
        finally:
            con.close()
        return out

    def _groups(self, con, uri, remote, kind):
        """{row group: {"first", "n", "bbox": {leaf: (min, max)}, "cols": {column: [byte ranges]}}} of the file,
        parsed once per version (0.6 s of every cell's two queries, 2026-10-22: Central Park's first block waited
        90 s for its cells)."""
        with self._lock:
            got = self._meta
        if got is not None and got[0] is remote:
            return got[1]
        # parsed once per version on disk too (0.8 s of every server process's first cell, the footer's ~84k rows)
        saved = os.path.join(remote.cache_dir, GROUPS_FILE)
        try:
            with open(saved, encoding="utf-8") as f:
                groups = {int(rg): {"first": g["first"], "n": g["n"],
                                    "bbox": {k: tuple(v) for k, v in g["bbox"].items()},
                                    "cols": {k: [tuple(r) for r in v] for k, v in g["cols"].items()}}
                          for rg, g in json.load(f).items()}
            with self._lock:
                self._meta = (remote, groups)
            return groups
        except (OSError, ValueError, KeyError, TypeError):
            pass
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
        try:
            os.makedirs(remote.cache_dir, exist_ok=True)
            tmp = f"{saved}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({str(rg): g for rg, g in groups.items()}, f)
            os.replace(tmp, saved)
        except (OSError, TypeError, ValueError) as e:
            log.debug("osmtags: row groups not saved: %s", e)
        with self._lock:
            self._meta = (remote, groups)
        return groups

    @staticmethod
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

    def _table(self, con, uri, groups, rg, remote, kind):
        """Row group rg's ways with a tag the roads use (or highway=raceway): {"id", "race", "frn", COLUMNS..., and for
        a text column "<c>_off" (its UTF-8 bytes' offsets) and "<c>_ok" (not NULL)} as arrays, NaN for a missing number.
        Decoded once per version: kept in memory (TABLE_MEM) and in the version's folder (TABLE_DIR, the other
        processes read it; xproc: one decodes)."""
        k = (id(remote), rg)
        with self._lock:
            t = self._tables.pop(k, None)
            if t is not None:
                self._tables[k] = t             # (most recently used last)
                return t
        path = os.path.join(remote.cache_dir, TABLE_DIR, f"{int(rg)}.npz")

        def load():
            try:
                with np.load(path, allow_pickle=False) as f:
                    if "lane_hw_ok" not in f.files or "bus" not in f.files:  # (decoded before busways: decoded again)
                        return None
                    return {c: f[c] for c in f.files}
            except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile):    # (damaged: decoded again)
                return None
        t, mine = xproc.once(path + ".lock", load, TABLE_WAIT_S, kind)
        if t is None:
            try:
                t = self._decode(con, uri, groups, rg, remote, kind)
                self._save_table(path, t)
            finally:
                if mine:
                    xproc.release(path + ".lock")
        with self._lock:
            self._tables[k] = t
            while len(self._tables) > TABLE_MEM:
                self._tables.pop(next(iter(self._tables)))
        return t

    @staticmethod
    def _save_table(path, t):
        """Row group table t saved at path for the other processes; the decoded table is used whether or not that
        works. A bare os.replace failed on Windows whenever another process had the file open (it loads it, or
        decoded it too after waiting TABLE_WAIT_S): PermissionError (WinError 5), and the whole cell lost its OpenStreetMap
        tags (lanes, widths: cached so for good) and decoded the row group again for each of its neighbours (GitHub #663:
        16 times in a session, each before the road workers "may be stuck")."""
        import staging
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp.npz"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            np.savez_compressed(tmp, **t)
            staging.replace_cached(tmp, path)       # (Windows: retried; a copy there and in use is kept)
        except OSError as e:
            log.debug("osmtags: row group table %s not saved: %s: %s", os.path.basename(path), type(e).__name__, e)
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _decode(self, con, uri, groups, rg, remote, kind):
        g = groups[rg]
        rd = self._reader
        want = {c for c in g["cols"] if c in USED or c == "highway"}
        rd._fetch({PATH: remote}, {PATH: [r for c, rs in g["cols"].items() if c in want for r in rs]}, kind)
        cols = ", ".join(f"{expr} AS \"{name}\"" for name, expr in COLUMNS.items())
        # only the ways with a tag the roads use (and the raceways and busways): join() treats a way without one as
        # one not here, and building a dict per way of every highway in the cell was most of this thread's CPU
        # (osmtags, 14 CPU-s of a Deck's teleport, 2026-10-23)
        some = " OR ".join([f"{c} IS NOT NULL" for c in sorted(USED - {"id", "bbox"})]
                           + [f"highway IN ({', '.join(repr(v) for v in UNBUILT)})"])
        got = con.execute(f"""SELECT id, highway = 'raceway', highway = 'busway', file_row_number, {cols}
            FROM read_parquet({uri}, file_row_number = true)
            WHERE file_row_number BETWEEN {g['first']} AND {g['first'] + g['n'] - 1}
            AND (highway = 'raceway' OR highway = 'busway' OR {some})""").fetchall()
        t = {"id": np.array([r[0] for r in got], np.int64), "race": np.array([bool(r[1]) for r in got], bool),
             "bus": np.array([bool(r[2]) for r in got], bool), "frn": np.array([r[3] for r in got], np.int64)}
        for j, name in enumerate(COLUMNS, 4):
            vals = [r[j] for r in got]
            if name in TEXT_COLUMNS:          # (one UTF-8 blob and offsets: fixed-width strings took ~20 MB)
                enc = [b"" if v is None else str(v).encode("utf-8") for v in vals]
                t[name + "_ok"] = np.array([v is not None for v in vals], bool)
                t[name + "_off"] = np.concatenate([[0], np.cumsum([len(b) for b in enc])]).astype(np.int32)
                t[name] = np.frombuffer(b"".join(enc), np.uint8)
            else:
                t[name] = np.array([np.nan if v is None else float(v) for v in vals],
                                   np.float32 if name in EXACT32 else np.float64)
        return t

    def _cell(self, remote, bounds, kind):
        lon0, lat0, lon1, lat1 = bounds
        rd = self._reader
        files = {PATH: remote}
        con, proto, errors = rd._connect(files, kind)
        try:
            uri = f"'{proto}://{PATH}'"
            groups = self._groups(con, uri, remote, kind)
            cand = self._candidates(groups, bounds)
            if not cand:
                return {}, []
            # each row group decoded once (_table) and the cell's ways picked from it by their bbox: the file is
            # spatially clustered, so a place's cells share a few row groups (81 cells round Nottingham: 243 decodes
            # of 3 row groups), and the decoding was most of this thread's CPU (23 CPU-s of a teleport, 2026-10-30)
            rows = []
            for rg in cand:
                t = self._table(con, uri, groups, rg, remote, kind)
                if not len(t["id"]):
                    continue
                # (in float32, as the file's bbox and DuckDB's comparison with it: a way whose box ends on the cell's edge)
                b = np.float32
                m = np.flatnonzero((t["bx0"].astype(b) <= b(lon1)) & (t["bx1"].astype(b) >= b(lon0))
                                   & (t["by0"].astype(b) <= b(lat1)) & (t["by1"].astype(b) >= b(lat0)))
                rows.extend(_rows(t, m))
            extra = [r[_ROW_FRN] for r in rows if r[1] or r[2]]
            got = []
            if extra:           # (rare: the raceways' and busways' own columns and geometry, of their row groups only)
                hit = sorted({rg for r in extra for rg in cand
                              if groups[rg]["first"] <= r < groups[rg]["first"] + groups[rg]["n"]})
                rd._fetch(files, {PATH: [r for rg in hit for c in RACEWAY_COLUMNS + ("geometry",)
                                         for r in groups[rg]["cols"].get(c, [])]}, kind)
                try:            # (the geometry as its WKB bytes: no spatial extension needed)
                    con.execute("SET enable_geoparquet_conversion = false")
                except Exception:
                    pass
                rcols = ", ".join(f'"{c}"' for c in RACEWAY_COLUMNS)
                rows_in = ", ".join(str(int(r)) for r in extra)
                got = con.execute(f"""SELECT {rcols}, geometry FROM read_parquet({uri}, file_row_number = true)
                    WHERE file_row_number IN ({rows_in})""").fetchall()
        except Exception:
            for e in errors:
                if isinstance(e, rangeread.Changed):
                    raise e
            raise
        finally:
            con.close()
        names = list(COLUMNS)
        tags = {int(r[0]): {k: v for k, v in zip(names, r[_ROW_VAL:]) if v is not None} for r in rows}
        ways = []
        for row in got:
            coords = _linestring(bytes(row[-1]))
            if coords is None or len(coords) < 2:
                continue
            d = {c: v for c, v in zip(RACEWAY_COLUMNS, row[:-1]) if v is not None}
            d["coords"] = coords
            ways.append(d)
        return tags, ways

_ROW_FRN = 3    # _rows: (id, raceway, busway, file row, COLUMNS...)
_ROW_VAL = 4


def _rows(t, idx):
    """Rows idx of a decoded row group (OsmTags._table) as (id, raceway, busway, file row, COLUMNS' values)."""
    names = list(COLUMNS)
    out = []
    for i in idx:
        vals = []
        for c in names:
            if c in TEXT_COLUMNS:
                o = t[c + "_off"]
                vals.append(t[c][o[i]:o[i + 1]].tobytes().decode("utf-8") if t[c + "_ok"][i] else None)
            else:
                v = float(t[c][i])
                vals.append(None if v != v else (int(v) if c in INT_COLUMNS else v))
        out.append((int(t["id"][i]), bool(t["race"][i]), bool(t["bus"][i]), int(t["frn"][i]), *vals))
    return out


RACEWAY_PAVED = {"", "asphalt", "concrete", "paved", "chipseal"}


def raceway_features(ways):
    """The raceways (raceways()) as road Features: class "raceway", ids "osm-raceway-<way id>" (after every Overture
    GERS id: mapgeom.concat appends them), OSM's surface / width / lanes / layer / bridge / tunnel as Overture's rules
    and the osm_* columns."""
    return _appended_features(ways, "raceway")


def busway_features(ways):
    """The busways as road Features: class "busway", ids "osm-busway-<way id>" (these sort before osm-raceway ids, and
    both after an Overture id, so mapgeom.concat keeps the cell ascending). Seven metres unless a width is mapped.
    OSM's surface / width / lanes / layer / bridge / tunnel / oneway become Overture's rules."""
    return _appended_features(ways, "busway", default_width=7.0, oneway=True)


def _appended_features(ways, cls, default_width=None, oneway=False):
    """OSM ways Overture does not emit, as road Features of class cls and ids "osm-<cls>-<way id>"."""
    import mapgeom as mg
    ways = sorted(ways, key=lambda w: int(w["id"]))
    if not ways:
        return mg.empty("roads")
    coords, coord_off, flags, levels, surfaces, widths, oneways = [], [0], [], [], [], [], []
    for i, w in enumerate(ways):
        c = np.asarray(w["coords"], np.float64)
        coords.append(c)
        coord_off.append(coord_off[-1] + len(c))
        if str(w.get("bridge") or "no") not in ("no", ""):
            flags.append((i, "is_bridge", None))
        if str(w.get("tunnel") or "no") not in ("no", ""):
            flags.append((i, "is_tunnel", None))
        try:
            lv = float(w.get("layer"))
            if lv:
                levels.append((i, lv, None))
        except (TypeError, ValueError):
            pass
        sf = str(w.get("surface") or "").lower()
        surfaces.append((i, "paved" if sf in RACEWAY_PAVED else sf, None))
        try:
            wd = float(re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)", str(w.get("width") or "")).group(1))
        except (AttributeError, ValueError):
            wd = default_width
        if wd is not None:
            widths.append((i, wd, None))
        ow = str(w.get("oneway") or "").lower()
        if oneway and ow in ONEWAY_YES:
            oneways.append((i, -1.0 if ow == "-1" else 1.0, None))
    n = len(ways)
    lonlat = np.concatenate(coords)
    f = mg.Features("roads", mg.text_column([f"osm-{cls}-{int(w['id']):012d}" for w in ways]),
                    np.full(n, mg.LINE, np.uint8), np.arange(n + 1, dtype=np.int64), np.arange(n + 1, dtype=np.int64),
                    np.array(coord_off, np.int64), lonlat)
    f.attrs = {"class": mg.text_column([cls] * n), "subtype": mg.text_column(["road"] * n),
               "subclass": mg.text_column([""] * n), "name": mg.text_column([w.get("name") for w in ways]),
               "src_dataset": mg.text_column(["OpenStreetMap"] * n),
               "src_record": mg.text_column([f"w{int(w['id'])}@0" for w in ways]),
               "cx": np.array([float(c[:, 0].min() + c[:, 0].max()) / 2 for c in coords]),
               "cy": np.array([float(c[:, 1].min() + c[:, 1].max()) / 2 for c in coords])}
    f.rules = {"flags": mg.rule_table(flags), "level": mg.rule_table(levels, "float"),
               "surface": mg.rule_table(surfaces), "width": mg.rule_table(widths, "float")}
    if oneway:
        f.rules["oneway"] = mg.rule_table(oneways, "float")
    f.clipped = np.zeros(n, bool)
    # the mapped lane count, so a busway's dashes can see it. join() writes this same column from the cell's tags.
    import roadlanes
    counted = [roadlanes.count(w.get("lanes")) for w in ways]
    f.attrs["osm_lanes"] = np.array([np.nan if v is None else float(v) for v in counted], np.float64)
    return f


def _linestring(wkb):
    """(k, 2) lon/lat of a WKB LineString (2D, either byte order), else None."""
    import struct
    if len(wkb) < 9:
        return None
    end = "<" if wkb[0] == 1 else ">"
    typ = struct.unpack_from(end + "I", wkb, 1)[0]
    if typ % 1000 != 2 or typ >= 1000 or typ & 0x20000000:
        return None
    n = struct.unpack_from(end + "I", wkb, 5)[0]
    if 9 + 16 * n > len(wkb):
        return None
    return np.frombuffer(wkb, dtype=end + "f8", count=2 * n, offset=9).reshape(n, 2).astype(np.float64)


def _named_way(feats, i, by_name):
    """The OSM way id of the same-named way (by_name: name -> [(way id, lon0, lat0, lon1, lat1)]) whose bbox, grown by
    NAME_MATCH_M, holds feature i's middle vertex: the smallest such, else None."""
    names = feats.attrs.get("name")
    cands = by_name.get(str(names[i])) if names is not None else None
    if not cands:
        return None
    a = feats.coord_off[feats.ring_off[feats.part_off[i]]]
    b = feats.coord_off[feats.ring_off[feats.part_off[i + 1]]]
    if b <= a:
        return None
    lon, lat = (float(v) for v in feats.lonlat[(a + b - 1) // 2][:2])
    my = NAME_MATCH_M / 111320.0
    mx = my / max(math.cos(math.radians(lat)), 0.1)
    best = None
    for w, x0, y0, x1, y1 in cands:
        if x0 - mx <= lon <= x1 + mx and y0 - my <= lat <= y1 + my:
            area = (x1 - x0) * (y1 - y0)
            if best is None or area < best[0]:
                best = (area, w)
    return best[1] if best else None


def join(feats, tags, geometry=None):
    """The tags of each road feature (its source record's way id) as attrs columns: floats (NaN where unknown) for
    FLOATS, text ('' where unknown) for TEXTS. feats: an Overture roads Features (attrs src_record). A segment without a
    known way (from a relation) takes the tags of the same-named way around it (_named_way), but its oneway: that way may
    be one carriageway of the road the segment draws."""
    import mapgeom as mg
    import roadlanes
    recs = feats.attrs.get("src_record")
    n = len(feats)
    ids = [way_id(str(recs[i])) if recs is not None else None for i in range(n)]
    by_name = {}
    for w, t in tags.items():
        if t.get("name") and all(k in t for k in ("bx0", "by0", "bx1", "by1")):
            by_name.setdefault(str(t["name"]), []).append((w, t["bx0"], t["by0"], t["bx1"], t["by1"]))
    named = [w if w in tags else _named_way(feats, i, by_name) for i, w in enumerate(ids)]
    for c in FLOATS:
        if c == "lanes":
            classes = feats.attrs.get("class", [""] * n)
            src = [ids[i] if classes[i] in roadlanes.HIGHWAYS else w for i, w in enumerate(named)]
            feats.attrs["osm_lanes"] = np.array([roadlanes.count(tags.get(w, {}).get(c)) or np.nan for w in src])
            continue
        feats.attrs["osm_" + c if not c.startswith("osm_") else c] = np.array(
            [float(tags.get(w, {}).get(c, np.nan)) if w is not None else np.nan for w in named], np.float64)
    for c in TEXTS:
        src = ids if c == "osm_oneway" else named
        feats.attrs[c if c.startswith("osm_") else "osm_" + c] = mg.text_column(
            [str(tags.get(w, {}).get(c, "")) if w is not None else "" for w in src])
    # A way without a lane count takes the count of the same-named way around it going the same way (one-way or
    # not): OpenStreetMap's lanes ahead of the class's width (Portland's arterials carry lanes on some of their ways
    # only, the rest were drawn at the class's guess, the user 2026-10-27).
    lanes, ows = feats.attrs["osm_lanes"], feats.attrs["osm_oneway"]
    names = feats.attrs.get("name")
    sense = {}
    for w, t in tags.items():
        if "lanes" in t and t.get("name") and all(k in t for k in ("bx0", "by0", "bx1", "by1")):
            key = (str(t["name"]), str(t.get("osm_oneway", "")) in ONEWAY_YES)
            sense.setdefault(key, []).append((w, t["bx0"], t["by0"], t["bx1"], t["by1"]))
    for i in range(n if sense and names is not None else 0):
        if np.isnan(lanes[i]) and classes[i] not in roadlanes.HIGHWAYS:
            nm = str(names[i])
            w = _named_way(feats, i, {nm: sense.get((nm, str(ows[i]) in ONEWAY_YES), [])})
            if w is not None:
                lanes[i] = roadlanes.count(tags[w]["lanes"]) or np.nan
    roadlanes.join(feats, tags, ids, geometry)
    return feats
