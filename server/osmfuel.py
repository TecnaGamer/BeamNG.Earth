"""OpenStreetMap's fuel stations (amenity=fuel) per map cell, from OSM US's Layercake POIs file: the stations Overture's
places do not list (142 of the 600 OSM fuel stations in the 54 test places, 2026-10-28), and a second source for those
it does (the user, 2026-10-28: "lets test OSM if it is too laggy we will revert").
Read as osmtags.py reads the highways file: the pinned version (ETag), the footer once per version, the row groups whose
bbox statistics touch the cell, and only the columns used, through rangeread's block cache; in two steps (the small
columns to find the cell's fuel rows, then names, brand, building and geometry of the row groups that have any).
Unreachable: no OSM stations (the caller goes on with Overture's).
"""
import logging

import osmtags
import rangeread

log = logging.getLogger("earthd.osmfuel")

URL = "https://data.openstreetmap.us/layercake/pois.parquet"
HOST = "data.openstreetmap.us"
OSM_FUEL_SHARE = 0.5        # of the host's daily requests budget: past it no OSM fuel cell is fetched that day (the
                            # road tags, osmtags.py, share the host and its budget: lanes, widths, sidewalks first)


class OverShare(rangeread.RangeError):
    """OSM fuel's share of the host's daily budget is used up (budget_ok): the cell is asked again later."""


def budget_ok(fetcher, share=OSM_FUEL_SHARE):
    """Whether today's requests to HOST are under share of its daily budget (netfetch's own stops prefetch at 80 %:
    this leaves the road tags most of it)."""
    pol = getattr(fetcher, "policies", {}).get(HOST)
    b = getattr(pol, "daily_budget", None)
    if not b:
        return True
    used = (fetcher.snapshot().get(HOST) or {}).get("requests", 0)
    return used < share * b
PATH = "osm/pois.parquet"
BLOCKS_CAP = 64 * 1024 * 1024       # the POI file's cached blocks kept (prune_blocks: 461 MB for 54 places without it)
KEEP_TAIL = 16 * 1024 * 1024        # ... besides its last 16 MB: the footer (row-group index, 4.9 MB; 40 s to read again)
PRUNE_EVERY_S = 30.0                # (MapData.osm_fuel_fetch prunes at most this often)
# the amenities kept: fuel stations, and EV charging stations (the user, 2026-10-29: "real life ev stations mapped like
# the gas stations are"), found in the same pass: the same columns, no more requests
AMENITIES = ("fuel", "charging_station")
FIND = ("bbox", "amenity")                              # step 1: the cell's fuel rows (no id: a third of
                                                        # the bytes; ids with the names in step 2)
DETAIL = ("id", "brand", "name", "building")           # step 2: of the row groups that have any (no geometry:
                                                       # its chunks were most of 1-6 MB a cell; bbox is enough)


class OsmFuel(osmtags.OsmTags):
    """The amenity=fuel features per map cell, from the pinned Layercake POIs version."""

    DIR = "osmfuel"

    def __init__(self, raw_dir, fetcher, url=URL):
        super().__init__(raw_dir, fetcher, url)

    def cell(self, bounds, kind="demand"):
        """[{"id", "amenity", "brand", "name", "building", "bbox": (lon0, lat0, lon1, lat1)}] of the fuel and
        charging stations (AMENITIES) whose bbox touches bounds (lon0, lat0, lon1, lat1). A version that changed under the reads is dropped
        and the cell asked once more on the new one."""
        for attempt in (0, 1):
            etag, remote = self.version(kind)
            try:
                return self._fuel(remote, bounds, kind)
            except rangeread.Changed:
                if attempt:
                    raise
                log.info("osmfuel: %s changed (was %s), taking the new version", self.url, etag)
                self._forget()
        return []

    def warm(self, kind="prefetch"):
        """The pinned version and its row groups' index (the footer, 4.9 MB) read, nothing else: no cell (2026-10-29:
        a cell at (0, 0) was 1342 requests and 414 MB, turned away ten times as prefetch)."""
        etag, remote = self.version(kind)
        con, proto, errors = self._reader._connect({PATH: remote}, kind)
        try:
            self._groups(con, f"'{proto}://{PATH}'", remote, kind)
        finally:
            con.close()

    def _fuel(self, remote, bounds, kind):
        lon0, lat0, lon1, lat1 = bounds
        rd = self._reader
        files = {PATH: remote}
        con, proto, errors = rd._connect(files, kind)
        try:
            uri = f"'{proto}://{PATH}'"
            groups = self._groups(con, uri, remote, kind)
            cand = self._candidates(groups, bounds)
            if not cand:
                return []
            rd._fetch(files, {PATH: [r for rg in cand for c in FIND for r in groups[rg]["cols"].get(c, [])]}, kind)
            only = " OR ".join(f"file_row_number BETWEEN {groups[rg]['first']} AND "
                               f"{groups[rg]['first'] + groups[rg]['n'] - 1}" for rg in cand)
            box = (f"bbox.xmin <= {lon1!r} AND bbox.xmax >= {lon0!r} AND bbox.ymin <= {lat1!r} "
                   f"AND bbox.ymax >= {lat0!r}")
            # amenity compared here, not in the query: amenity = 'fuel' there made DuckDB read the amenity bloom filter
            # of each of the file's 509 row groups, block by block (2026-10-29: 133 of a cell's 144 requests, 35 MB)
            hits = [int(r[0]) for r in con.execute(f"""SELECT file_row_number, amenity FROM read_parquet({uri},
                file_row_number = true) WHERE ({only}) AND {box}""").fetchall() if r[1] in AMENITIES]
            if not hits:
                return []
            hit = sorted({rg for r in hits for rg in cand
                          if groups[rg]["first"] <= r < groups[rg]["first"] + groups[rg]["n"]})
            rd._fetch(files, {PATH: [r for rg in hit for c in DETAIL for r in groups[rg]["cols"].get(c, [])]}, kind)
            try:                # (the geometry as its WKB bytes: no spatial extension needed)
                con.execute("SET enable_geoparquet_conversion = false")
            except Exception:
                pass
            rows = con.execute(f"""SELECT id, amenity, brand, name, building, bbox.xmin, bbox.ymin, bbox.xmax, bbox.ymax
                FROM read_parquet({uri},
                file_row_number = true) WHERE file_row_number IN ({', '.join(map(str, hits))})""").fetchall()
        except Exception:
            for e in errors:
                if isinstance(e, rangeread.Changed):
                    raise e
            raise
        finally:
            con.close()
        return [x for x in map(_row, rows) if x is not None]

    def cells_many(self, bounds_list, kind="demand"):
        """[cell(bounds) for bounds in bounds_list] in one scan of the union's row groups (tools/build_cip.py: a z10
        unit's 256 cells were 256 DuckDB scans of the same row groups, ~0.1 s each, most of an empty cell's build).
        Each cell's rows are the ones cell() gives: its box tested in DuckDB with the same expression, the rows in file
        order (cell()'s single-threaded scan order)."""
        for attempt in (0, 1):
            etag, remote = self.version(kind)
            try:
                return self._fuel_many(remote, bounds_list, kind)
            except rangeread.Changed:
                if attempt:
                    raise
                log.info("osmfuel: %s changed (was %s), taking the new version", self.url, etag)
                self._forget()
        return [[] for _ in bounds_list]

    def _fuel_many(self, remote, bounds_list, kind):
        out = [[] for _ in bounds_list]
        if not bounds_list:
            return out
        u = (min(b[0] for b in bounds_list), min(b[1] for b in bounds_list), max(b[2] for b in bounds_list),
             max(b[3] for b in bounds_list))
        rd = self._reader
        files = {PATH: remote}
        con, proto, errors = rd._connect(files, kind)
        try:
            uri = f"'{proto}://{PATH}'"
            groups = self._groups(con, uri, remote, kind)
            cand = self._candidates(groups, u)
            if not cand:
                return out
            rd._fetch(files, {PATH: [r for rg in cand for c in FIND for r in groups[rg]["cols"].get(c, [])]}, kind)
            only = " OR ".join(f"file_row_number BETWEEN {groups[rg]['first']} AND "
                               f"{groups[rg]['first'] + groups[rg]['n'] - 1}" for rg in cand)
            con.execute(f"""CREATE TEMP TABLE fu AS SELECT file_row_number, amenity, bbox FROM read_parquet({uri},
                file_row_number = true) WHERE ({only}) AND {_box(u)}""")
            per = []
            for b in bounds_list:
                per.append([int(r[0]) for r in con.execute(f"""SELECT file_row_number, amenity FROM fu WHERE {_box(b)}
                    ORDER BY file_row_number""").fetchall() if r[1] in AMENITIES])
            hits = sorted({r for h in per for r in h})
            if not hits:
                return out
            hit = sorted({rg for r in hits for rg in cand
                          if groups[rg]["first"] <= r < groups[rg]["first"] + groups[rg]["n"]})
            rd._fetch(files, {PATH: [r for rg in hit for c in DETAIL for r in groups[rg]["cols"].get(c, [])]}, kind)
            try:
                con.execute("SET enable_geoparquet_conversion = false")
            except Exception:
                pass
            got = {int(r[0]): r[1:] for r in con.execute(f"""SELECT file_row_number, id, amenity, brand, name,
                building, bbox.xmin, bbox.ymin, bbox.xmax, bbox.ymax FROM read_parquet({uri}, file_row_number = true)
                WHERE file_row_number IN ({', '.join(map(str, hits))})""").fetchall()}
        except Exception:
            for e in errors:
                if isinstance(e, rangeread.Changed):
                    raise e
            raise
        finally:
            con.close()
        for i, h in enumerate(per):
            out[i] = [x for x in (_row(got[r]) for r in h if r in got) if x is not None]
        return out


def _box(bounds):
    lon0, lat0, lon1, lat1 = bounds
    return f"bbox.xmin <= {lon1!r} AND bbox.xmax >= {lon0!r} AND bbox.ymin <= {lat1!r} AND bbox.ymax >= {lat0!r}"


def _row(r):
    """A station's dict of (id, amenity, brand, name, building, xmin, ymin, xmax, ymax), None without a bbox."""
    fid, amenity, brand, name, building, x0, y0, x1, y1 = r
    if x0 is None:
        return None
    if isinstance(name, (list, tuple)):
        name = name[0] if name else None
    return {"id": int(fid), "amenity": amenity, "brand": brand or "", "name": name or "", "building": building or "",
            "bbox": (float(x0), float(y0), float(x1), float(y1))}



def prune_blocks(d, size, cap=BLOCKS_CAP, keep_tail=KEEP_TAIL, block=rangeread.BLOCK):
    """The cached blocks in folder d of a size-byte file (rangeread's "<i>.blk") kept under cap, the oldest (by mtime)
    deleted first; the blocks of its last keep_tail bytes (the footer) never. A cell's stations are kept apart
    (MapData.osm_fuel_cached): a block is only worth keeping for the next cells of the same row groups."""
    import os
    first_tail = max(0, (size - keep_tail) // block)
    body = []
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        if not n.endswith(".blk"):
            continue
        try:
            i = int(n[:-4])
        except ValueError:
            continue
        if i >= first_tail:
            continue
        p = os.path.join(d, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        body.append((st.st_mtime, st.st_size, p))
    total = sum(b[1] for b in body)
    for _, sz, p in sorted(body):
        if total <= cap:
            break
        try:
            os.remove(p)
            total -= sz
        except OSError:
            pass
