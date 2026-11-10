"""The station finder's index (the user, 2026-11-09: "making a new layer on the map that you can enable, one for gas
stations and one for ev's to make them easier to find"; "nearest button is nice as well, if we can do a quick action
outside map too when fuel get low"): the gas stations and EV chargers of each map cell, from the lists the world's
layouts use (gasstations.pins / with_osm, evchargers.charger_pins / merge: Overture's places and OpenStreetMap's), kept
on disk as a small JSON file per cell so the map and Nearest never decode the places layer twice.

Which ones show (the user, 2026-11-09, of "show every station, but drop an icon once the world has been built there and
it turned out empty": "C"): every listing, until a road tile owning it was built and laid nothing out there (mark,
from gasstations.tile_sites' tried); a later build that lays it out brings it back.

Where (the user, 2026-11-09, "this works for me"): the map lists the cells whose places data is on disk already (around
the car, where the player has driven, the pre-downloaded areas): no download for a map view. Nearest and the low fuel
prompt search rings of cells outward from the car and may download the few cells they need.
"""
import json
import logging
import math
import os
import re
import threading

import mapcells

log = logging.getLogger("earthd.stationindex")

INDEX_VERSION = 2           # this file's rules (an index made by other rules is not read: built again)
MAX_ITEMS = 50              # the nearest this many in a Nearest ("near") answer
MAP_MAX_ITEMS = 300         # ... in a map view's: with the weekly shards a zoomed-out city view has hundreds (the user,
                            # 2026-11-10: "does it only show a certain amount of symbols?"; 50 left the view's edges bare)
R_KM_MAX = 25.0             # the farthest a query looks
MAP_BUILD_MAX = 16          # cells' indexes built per map query (more: partial, asked again on the map's next poll)
NEAR_FETCH_MAX = 4          # cells without places data downloaded per Nearest query (more: partial)
MARK_M = 30.0               # a road tile's report of a station applies to the index's item of its kind this close
DEDUP_M = 60.0              # two items of a kind this close are one station (gasstations.MERGE_M; seen from two cells)
MEM_CELLS = 4096            # cells' entries kept in memory (each ~1 KB)
EARTH_KM = 6371.0088


def _km(la0, lo0, la1, lo1):
    p0, p1 = math.radians(la0), math.radians(la1)
    a = math.sin((p1 - p0) / 2) ** 2 + math.cos(p0) * math.cos(p1) * math.sin(math.radians(lo1 - lo0) / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def _m(a, b):
    """Metres between two items (equirectangular: they are within a few hundred m)."""
    k = math.cos(math.radians(a["lat"]))
    return math.hypot((a["lon"] - b["lon"]) * k, a["lat"] - b["lat"]) * 111320.0


def _centre(key):
    lo0, la0, lo1, la1 = key.bounds()
    return (la0 + la1) / 2, (lo0 + lo1) / 2


def cell_items(md, key):
    """[{"lat", "lon", "name", "brand", "kind"}] of a map cell's gas stations ("fuel": a gas_station listing, a fuel
    brand or OpenStreetMap's fuel station; a convenience brand without either is left out, as it needs a mapped canopy
    to be laid out) and EV chargers ("ev"), merged as the road tiles merge them, at their merged positions. The cell's
    places layer through md.cell; OpenStreetMap's rows only when fetched already (osm_fuel_cached, never waited for)."""
    import geodesy
    import evchargers
    import gasstations as gs
    la, lo = _centre(key)
    frame = geodesy.EnuFrame(la, lo)
    places = md.cell(key, layers=("places",), kind="demand").layers.get("places")
    layers = [places] if places is not None and len(places) else []
    osm = [md.osm_fuel_cached(key) or []] if hasattr(md, "osm_fuel_cached") else []
    out = []

    def add(x, y, name, brand, kind):
        lat, lon, _ = frame.enu_to_geodetic(x, y, 0.0)
        out.append({"lat": round(float(lat), 6), "lon": round(float(lon), 6), "name": name or "", "brand": brand or "",
                    "kind": kind})
    for st in gs.with_osm(gs.merge(gs.pins(frame, layers)), gs._osm_points(frame, osm)):
        if st.confirmed or st.osm:
            add(st.x, st.y, st.name, st.brand, "fuel")
    for ch in evchargers.merge(evchargers.charger_pins(frame, layers),
                               gs._osm_points(frame, osm, amenity="charging_station")):
        add(ch.x, ch.y, ch.name, ch.brand, "ev")
    return out


def shard_items(md, key):
    """The cell's stations from the weekly OpenStreetMap shards (md.station_tags, railtags.StationTags; Bauer33333's
    find on #716) as items, for a cell whose map data is not downloaded; None when the shards are off or unreachable."""
    st = getattr(md, "station_tags", None)
    if st is None:
        return None
    lo0, la0, lo1, la1 = key.bounds()
    got = st.stations_in(la0, lo0, la1, lo1)
    if got is None:
        return None
    return [{"lat": round(float(r["lat"]), 6), "lon": round(float(r["lon"]), 6), "name": r.get("name") or "",
             "brand": r.get("brand") or "", "kind": r["kind"]} for r in got if r.get("kind") in ("fuel", "ev")]


class StationIndex:
    """The per-cell station lists under root (<cache>/mapdata/raw/stationindex), and the road tiles' reports."""

    def __init__(self, root, md):
        import mapdata
        self.md = md
        # the rules, the Overture release the places come from and the OSM cells' version: a newer release (the project
        # moving on, mapdata.MapData.release) builds every cell's list again (Codex on #1056)
        release = re.sub(r"[^0-9A-Za-z._-]", "_", str(getattr(md, "release", "") or "none"))
        self.dir = os.path.join(root, f"v{INDEX_VERSION}-{release}-{getattr(mapdata, 'OSM_POI_VERSION', 0)}")
        self._mem = {}
        self._lock = threading.Lock()
        # a cell file's read-modify-write (mark, _build's merge): road tiles report from several worker threads, and
        # two reports for one cell each writing what it read lost the other's
        self._wlock = threading.Lock()

    # --- the cells' files -------------------------------------------------------------------------------------------
    def _path(self, key):
        return os.path.join(self.dir, f"{key.z}_{key.x}_{key.y}.json")

    def _entry(self, key):
        """The cell's {"items": [...] | None (not built), "marks": [[kind, lat, lon, laid_out]]}."""
        with self._lock:
            e = self._mem.get(key)
        if e is not None:
            return e
        try:
            with open(self._path(key), encoding="utf-8") as f:
                e = json.load(f)
        except (OSError, ValueError):
            e = {"items": None, "marks": []}
        return self._remember(key, e)

    def _remember(self, key, e):
        with self._lock:
            self._mem[key] = e
            while len(self._mem) > MEM_CELLS:
                self._mem.pop(next(iter(self._mem)))
        return e

    def _save(self, key, e):
        os.makedirs(self.dir, exist_ok=True)
        p = self._path(key)
        tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(e, f, separators=(",", ":"))
        os.replace(tmp, p)

    def _osm_pending(self, key):
        """Whether the cell's OpenStreetMap stations are not fetched yet (mapdata.MapData.osm_fuel_cached None)."""
        f = getattr(self.md, "osm_fuel_cached", None)
        return f is not None and f(key) is None

    def _needs(self, key, e):
        """Whether the cell's list is to be built (again): never built; built while its OpenStreetMap stations were
        still being fetched and they are there now (Codex on #1056: not a complete Overture-only list for good); or
        taken from the weekly shards and its map data is on disk now (the lists the road tiles lay out)."""
        if e.get("items") is None:
            return True
        if e.get("osm_pending") and not self._osm_pending(key):
            return True
        return bool(e.get("from_shard")) and self.md.has(key, "places")

    def _from_shard(self, key):
        """The cell's list from the weekly shards, in memory only (no file per cell of a wide map view; read again from
        the shard, indexed by cell, after a restart), until its map data is on disk; False when they cannot be had."""
        items = shard_items(self.md, key)
        if items is None:
            return False
        with self._wlock:
            e = dict(self._entry(key))
            e["items"], e["from_shard"], e["osm_pending"] = items, True, False
            self._remember(key, e)
        return True

    def _build(self, key):
        pending = self._osm_pending(key)        # (before the read: rows that come meanwhile are read next time)
        items = cell_items(self.md, key)
        with self._wlock:               # (a mark that came meanwhile is kept)
            e = dict(self._entry(key))
            e["items"], e["osm_pending"], e["from_shard"] = items, pending, False
            self._save(key, e)
            self._remember(key, e)
        return e

    # --- the road tiles' reports ------------------------------------------------------------------------------------
    def mark(self, tried):
        """tried: [(kind, lat, lon, laid_out)] from road tiles (gasstations.tile_sites): each replaces its cell's
        earlier report of that kind within MARK_M."""
        by = {}
        for kind, lat, lon, laid in tried or ():
            try:
                by.setdefault(mapcells.cell_of(float(lat), float(lon)), []).append(
                    [str(kind), round(float(lat), 6), round(float(lon), 6), bool(laid)])
            except ValueError:
                continue
        for key, new in by.items():
            with self._wlock:
                e = dict(self._entry(key))
                marks = list(e.get("marks") or [])
                for m in new:
                    here = {"lat": m[1], "lon": m[2]}
                    marks = [o for o in marks if o[0] != m[0] or _m({"lat": o[1], "lon": o[2]}, here) > MARK_M]
                    marks.append(m)
                if marks == list(e.get("marks") or []):
                    continue            # (unchanged: no rewrite of the cell's file at every road tile, SSD writes)
                e["marks"] = marks
                try:                    # (a list from the shards stays in memory: the file keeps the reports only)
                    self._save(key, {**e, "items": None, "from_shard": False} if e.get("from_shard") else e)
                except OSError as ex:
                    log.debug("station index %s: %s", key, ex)
                self._remember(key, e)

    @staticmethod
    def _empty(item, marks):
        best = None
        for kind, lat, lon, laid in marks:
            if kind != item["kind"]:
                continue
            d = _m({"lat": lat, "lon": lon}, item)
            if d <= MARK_M and (best is None or d < best[0]):
                best = (d, laid)
        return best is not None and not best[1]

    # --- queries ----------------------------------------------------------------------------------------------------
    def query(self, lat, lon, r_km, kinds, mode="map"):
        """(items [{"lat", "lon", "name", "brand", "kind", "km"}] nearest first, partial). mode "map": the cells within
        r_km whose places data is on disk; "near": rings of cells outward, downloading up to NEAR_FETCH_MAX a call,
        until a station is nearer than the next ring could hold. Blocking (a worker's)."""
        lat, lon = float(lat), float(lon)
        r_km = min(max(float(r_km), 0.01), R_KM_MAX)
        kinds = {str(k) for k in (kinds or ())}
        found, partial = [], False
        if mode == "near":
            found, partial = self._near(lat, lon, r_km, kinds)
        else:
            found, partial = self._map(lat, lon, r_km, kinds)
        return self._finish(found, lat, lon, r_km, MAX_ITEMS if mode == "near" else MAP_MAX_ITEMS), partial

    def _take(self, key, kinds, out):
        e = self._entry(key)
        for it in e.get("items") or ():
            if it["kind"] in kinds and not self._empty(it, e.get("marks") or ()):
                out.append(it)

    def _cells_by_distance(self, lat, lon, r_km):
        dlat = r_km / 111.32
        dlon = r_km / (111.32 * max(math.cos(math.radians(lat)), 0.01))
        keys = mapcells.cells_for_bbox(lat - dlat, lon - dlon, lat + dlat, lon + dlon)
        return sorted(keys, key=lambda k: _km(lat, lon, *_centre(k)))

    def _map(self, lat, lon, r_km, kinds):
        out, built, partial = [], 0, False
        for key in self._cells_by_distance(lat, lon, r_km):
            e = self._entry(key)
            if self._needs(key, e):
                if not self.md.has(key, "places"):
                    self._from_shard(key)       # (the weekly shards; unreachable: the cell left out, as before)
                    self._take(key, kinds, out)
                    continue
                if built >= MAP_BUILD_MAX:
                    partial = True
                else:
                    self._safe_build(key)
                    built += 1
            self._take(key, kinds, out)
        return out, partial

    def _safe_build(self, key):
        try:
            return self._build(key)
        except Exception as ex:          # an unreadable cell costs its stations, not the answer
            log.debug("station index %s: %s: %s", key, type(ex).__name__, ex)
            return None

    def _near(self, lat, lon, r_km, kinds):
        c = mapcells.cell_of(lat, lon)
        lo0, la0, lo1, la1 = c.bounds()
        cell_km = min(_km(la0, lo0, la1, lo0), _km(lat, lo0, lat, lo1))
        out, fetched = [], 0
        rings = int(math.ceil(r_km / cell_km)) + 1
        n = c.n if hasattr(c, "n") else (1 << c.z)
        for r in range(rings):
            if (r - 1) * cell_km > r_km:
                break
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r or not (0 <= c.y + dy < n):
                        continue
                    key = mapcells.CellKey(c.z, (c.x + dx) % n, c.y + dy)
                    if self._needs(key, self._entry(key)):
                        if not self.md.has(key, "places") and self._from_shard(key):
                            self._take(key, kinds, out)
                            continue
                        if not self.md.has(key, "places"):
                            if fetched >= NEAR_FETCH_MAX:
                                return out, True
                            fetched += 1
                        self._safe_build(key)
                    self._take(key, kinds, out)
            best = min((_km(lat, lon, it["lat"], it["lon"]) for it in out), default=None)
            if best is not None and best <= r * cell_km:
                break
        return out, False

    @staticmethod
    def _finish(found, lat, lon, r_km, cap):
        items = []
        for it in found:
            km = _km(lat, lon, it["lat"], it["lon"])
            if km <= r_km:
                items.append({**it, "km": round(km, 2)})
        items.sort(key=lambda i: i["km"])
        kept = []
        for it in items:
            if any(k["kind"] == it["kind"] and _m(k, it) <= DEDUP_M for k in kept):
                continue
            kept.append(it)
            if len(kept) >= cap:
                break
        return kept
