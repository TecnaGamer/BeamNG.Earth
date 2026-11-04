"""The status bar's town, state and country, from map data we download ourselves (no outside geocoder).

Until 1.7.3 the bar asked Photon's reverse geocoding (photon.komoot.io) every time the player crossed a ~1 km cell,
for every player; komoot then answered the app's requests with HTTP 403 (GitHub #602). The user, 2026-11-03: the
town, state and country must come from local data; the map's address search keeps Photon (geocode.py).

Data: Overture Maps' divisions theme (overture.LAYERS "divisions", "division_areas"), read like the other map layers
(range reads of the release's GeoParquet through netfetch, no geometry column: only the bboxes):
  towns  the locality / localadmin divisions' points (the bbox centre), with class (megacity, city, town, village,
         hamlet; none for a city's districts), population, country (ISO 3166-1) and region (ISO 3166-2) codes, and
         their names: primary (the local one) and names.common in overture.DIVISION_LANGS;
  areas  the countries', dependencies' and regions' rows of division_area (codes, names, bbox), so a state's or
         country's name is known however far its own point lies.
Both per Web-Mercator cell at ZOOM (9: ~78 km at the equator, ~60 km at 40 N; coarser towards the poles, zoom_at):
one fetch covers a region, kept on disk forever (a release is immutable; PLACES_FORMAT in the path: bump it when the
stored arrays or the layers' queries change). Fetched as kind "background" (netfetch: only while Overture's hosts have tokens to spare, never
ahead of a road, building or terrain request; a Deferred fetch is asked again later, earthd where()).

Lookup (pick(), pure): the place is the town with the smallest distance / reach, where reach grows with its
population (REACH_*: a city's reach covers its districts and the hamlets beside it, a hamlet's ~1.5 km), among the
classed towns first (a locality without a class is a city's district in Overture, e.g. Köln's "Nippes": only when no
classed town reaches), ties by id; none within reach: no place. Region and country by the chosen town's codes (none
chosen: the region / country whose bbox holds the point, the nearest town's codes deciding between overlapping
boxes; no town in the cells read: nothing, the open sea). Names in geocode.lang_for(country)'s language where Overture has it, else the primary name.
"""
import json
import logging
import math
import os
import threading
from collections import OrderedDict

import numpy as np

import geocode
import mapcells
import overture
import staging

log = logging.getLogger("earthd.placenames")

PLACES_FORMAT = 1               # the cell files' format (bump when what a cell holds or how it is read changes)
ZOOM = 9                        # the cells' Web-Mercator zoom up to 60 degrees of latitude (zoom_at)
SEARCH_M = 25000.0              # towns this far around the player are read (the cells under that box)
MEM_CELLS = 64                  # decoded cells kept in memory
# reach of a town (km) = clamp(REACH_K * REACH_R_KM * (population / 1000) ** REACH_EXP, REACH_MIN_KM, REACH_MAX_KM):
# REACH_R_KM * (pop / 1000) ** 0.4 is about a town's equivalent radius (Baltimore 585k: 8.7 km, its 238 km2;
# Köln 1.07 M: 11 km, 405 km2; Leverkusen 163k: 5.2 km, 79 km2), REACH_K = 2: its edge with the outskirts
REACH_R_KM, REACH_EXP, REACH_K = 0.68, 0.4, 2.0
REACH_MIN_KM, REACH_MAX_KM = 1.5, 40.0
CLASS_POP = {"megacity": 5e6, "city": 1e5, "town": 5e3, "village": 500.0, "hamlet": 50.0}   # population when none
UNCLASSED_POP = 1e4             # a locality without a class (a district) or a localadmin, without a population
M_PER_DEG = 111320.0
TOWN_COLS = ("subtype", "class", "country", "region", "name") + tuple(f"name_{lang}" for lang in overture.DIVISION_LANGS)
AREA_COLS = ("subtype", "country", "region", "division_id", "name") + tuple(
    f"name_{lang}" for lang in overture.DIVISION_LANGS)
EMPTY = {"place": "", "region": "", "country": ""}


def zoom_at(lat):
    """The cells' zoom at latitude lat: ZOOM, one less for each halving of cos(lat) (a Web-Mercator tile's ground size
    shrinks with it: the cells stay ~35-80 km wide from the equator to the Arctic, a few per lookup). Pure."""
    c = max(0.01, math.cos(math.radians(max(-mapcells.MAX_LAT, min(mapcells.MAX_LAT, float(lat))))))
    return max(2, ZOOM - int(math.floor(math.log2(1.0 / c))))


def cells_for(lat, lon, search_m=SEARCH_M, z=None):
    """The cells (mapcells.CellKey at z, else zoom_at(lat)) under the box of search_m around (lat, lon). Pure."""
    lat = max(-mapcells.MAX_LAT, min(mapcells.MAX_LAT, float(lat)))
    z = zoom_at(lat) if z is None else z
    dlat = search_m / M_PER_DEG
    dlon = min(179.0, search_m / (M_PER_DEG * max(0.05, math.cos(math.radians(lat)))))
    lon = ((float(lon) + 180.0) % 360.0) - 180.0
    lon0 = ((lon - dlon + 180.0) % 360.0) - 180.0
    lon1 = ((lon + dlon + 180.0) % 360.0) - 180.0
    return mapcells.cells_for_bbox(lat - dlat, lon0, lat + dlat, lon1, z)


def _text(a, n):
    return np.asarray(a if a is not None else [""] * n).astype(str)


def reach_km(population, klass):
    """Each town's reach in km (population: <= 0 or NaN = unknown, then its class's CLASS_POP). Pure."""
    pop = np.asarray(population, np.float64)
    klass = np.asarray(klass).astype(str)
    dflt = np.array([CLASS_POP.get(c, UNCLASSED_POP) for c in klass], np.float64)
    pop = np.where(np.isfinite(pop) & (pop > 0), pop, dflt)
    return np.clip(REACH_K * REACH_R_KM * (pop / 1000.0) ** REACH_EXP, REACH_MIN_KM, REACH_MAX_KM)


def _name(table, i, lang):
    """A row's name in lang (names.common), else its primary name."""
    v = str(table.get(f"name_{lang}", [""] * (i + 1))[i]) if lang else ""
    return v or str(table["name"][i])


def _dist_km(lat, lon, lats, lons):
    dlon = ((np.asarray(lons, np.float64) - lon + 180.0) % 360.0) - 180.0
    dx = dlon * M_PER_DEG * math.cos(math.radians(lat))
    dy = (np.asarray(lats, np.float64) - lat) * M_PER_DEG
    return np.hypot(dx, dy) / 1000.0


def pick(towns, areas, lat, lon):
    """{"place", "region", "country"} at (lat, lon) ("" each when unknown) from towns {"lat", "lon", "population",
    "id", "subtype", "class", "country", "region", "name", "name_<lang>"...} and areas {"subtype", "country",
    "region", "division_id", "x0", "y0", "x1", "y1", "name", "name_<lang>"...} (arrays of equal length within each;
    either may be empty). Pure and deterministic (the same rows in any order give the same answer)."""
    out = dict(EMPTY)
    lat, lon = float(lat), float(lon)
    n = len(towns.get("lat", ())) if towns else 0
    best = near = None
    if n:
        d = _dist_km(lat, lon, towns["lat"], towns["lon"])
        klass = _text(towns.get("class"), n)
        score = d / reach_km(towns.get("population", np.full(n, np.nan)), klass)
        tier = np.array([0 if c in CLASS_POP else 1 for c in klass], np.int8)
        ids = _text(towns.get("id"), n)
        ok = np.flatnonzero(score <= 1.0)
        if len(ok):
            best = int(ok[np.lexsort((ids[ok], score[ok], tier[ok]))[0]])
        near = int(np.lexsort((ids, d))[0])
    cc = rc = ""
    if best is not None:
        cc, rc = str(towns["country"][best]), str(towns["region"][best])
    m = len(areas.get("subtype", ())) if areas else 0
    sub = _text(areas.get("subtype"), m) if m else np.zeros(0, str)
    a_cc = _text(areas.get("country"), m) if m else np.zeros(0, str)
    a_rc = _text(areas.get("region"), m) if m else np.zeros(0, str)
    a_id = _text(areas.get("division_id"), m) if m else np.zeros(0, str)
    if best is None and m and near is not None:
        # no town in reach: the region / country whose box holds the point; overlapping boxes: the nearest town's
        # (no town in the cells read at all, the open sea: nothing; the boxes alone put the Atlantic in the US)
        inside = ((np.asarray(areas["x0"], np.float64) <= lon) & (lon <= np.asarray(areas["x1"], np.float64))
                  & (np.asarray(areas["y0"], np.float64) <= lat) & (lat <= np.asarray(areas["y1"], np.float64)))
        hint_cc, hint_rc = str(towns["country"][near]), str(towns["region"][near])

        def one(codes, kinds, hint):
            got = sorted({c for c in codes[inside & np.isin(sub, kinds)] if c})
            if len(got) == 1:
                return got[0]
            return hint if hint in got else ""
        cc = one(a_cc, ("country", "dependency"), hint_cc)
        rc = one(a_rc, ("region",), hint_rc)
        if rc and not cc:
            cc = rc.split("-", 1)[0]
    lang = geocode.lang_for(cc)
    if best is not None:
        out["place"] = _name(towns, best, lang)
    if m and rc:
        rows = np.flatnonzero((sub == "region") & (a_rc == rc))
        if len(rows):
            out["region"] = _name(areas, int(rows[np.argsort(a_id[rows], kind="stable")[0]]), lang)
    if m and cc:
        for kind in ("dependency", "country"):
            rows = np.flatnonzero((sub == kind) & (a_cc == cc))
            if len(rows):
                out["country"] = _name(areas, int(rows[np.argsort(a_id[rows], kind="stable")[0]]), lang)
                break
    return out


def _concat(tables):
    """One table of several cells' tables (same keys)."""
    tables = [t for t in tables if t and len(t.get("subtype", ()))]
    if not tables:
        return {}
    return {k: np.concatenate([np.asarray(t[k]) for t in tables]) for k in tables[0]}


class PlaceNames:
    """The lookup with its cell files (one per server; lookup() from any thread, blocking: fetches what is missing)."""

    def __init__(self, cache_dir, fetcher, reader=None, release=overture.RELEASE):
        self.cache_dir, self.fetcher, self.release = cache_dir, fetcher, release
        self._reader = reader
        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()     # one cell fetched at a time (a background job)
        self._mem = OrderedDict()               # CellKey -> (towns, areas)

    def reader(self):
        with self._lock:
            if self._reader is None:
                self._reader = overture.Reader(os.path.join(self.cache_dir, "mapdata", "raw"), self.fetcher,
                                               self.release)
            return self._reader

    def path(self, key):
        return os.path.join(self.cache_dir, "mapdata", "placenames", f"f{PLACES_FORMAT}", self.release,
                            str(key.z), str(key.x), f"{key.y}.npz")

    def ready(self, lat, lon):
        """True when every cell a lookup at (lat, lon) reads is in memory or on disk (lookup() will not fetch)."""
        for k in cells_for(lat, lon):
            with self._lock:
                if k in self._mem:
                    continue
            if not os.path.exists(self.path(k)):
                return False
        return True

    def lookup(self, lat, lon, kind="background"):
        """{"place", "region", "country"} at (lat, lon) (pick()); the cells missing are fetched with kind (netfetch
        errors raised: asked again later)."""
        cells = [self.cell(k, kind) for k in cells_for(lat, lon)]
        return pick(_concat([c[0] for c in cells]), _concat([c[1] for c in cells]), lat, lon)

    def cell(self, key, kind="background"):
        """(towns, areas) of one cell: memory, disk, or fetched and written."""
        with self._lock:
            got = self._mem.get(key)
            if got is not None:
                self._mem.move_to_end(key)
                return got
        path = self.path(key)
        got = self._load(path)
        if got is None:
            with self._fetch_lock:
                got = self._load(path)
                if got is None:
                    towns, areas, prov = self._fetch(key, kind)
                    self._store(path, towns, areas, prov)
                    got = towns, areas
        with self._lock:
            self._mem[key] = got
            while len(self._mem) > MEM_CELLS:
                self._mem.popitem(last=False)
        return got

    def _fetch(self, key, kind):
        r = self.reader()
        towns, ts = r.query("divisions", key, kind)
        areas, as_ = r.query("division_areas", key, kind)
        t = {"lat": np.asarray(towns.attrs["cy"], np.float64), "lon": np.asarray(towns.attrs["cx"], np.float64),
             "population": np.asarray(towns.attrs.get("population", np.zeros(len(towns))), np.float32),
             "id": _text(towns.ids, len(towns))}
        for c in TOWN_COLS:
            t[c] = _text(towns.attrs.get(c), len(towns))
        a = {c: np.asarray(areas.attrs.get(c, np.zeros(len(areas))), np.float64) for c in ("x0", "y0", "x1", "y1")}
        for c in AREA_COLS:
            a[c] = _text(areas.attrs.get(c), len(areas))
        prov = [overture.sources_provenance("divisions", self.release, s) for s in (ts, as_) if s]
        log.info("place names: cell %s: %d towns, %d areas", key, len(towns), len(areas))
        return t, a, prov

    @staticmethod
    def _load(path):
        try:
            with np.load(path, allow_pickle=False) as z:
                arrays = {k: z[k] for k in z.files}
        except FileNotFoundError:
            return None
        except Exception as e:          # a damaged file: fetched again
            log.warning("place names: unreadable %s (%s: %s)", path, type(e).__name__, e)
            return None
        towns = {k[2:]: v for k, v in arrays.items() if k.startswith("t_")}
        areas = {k[2:]: v for k, v in arrays.items() if k.startswith("a_")}
        return towns, areas

    @staticmethod
    def _store(path, towns, areas, prov):
        arrays = {f"t_{k}": v for k, v in towns.items()}
        arrays.update({f"a_{k}": v for k, v in areas.items()})
        arrays["provenance"] = np.array(json.dumps(prov, sort_keys=True))
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp.npz"
            np.savez_compressed(tmp, **arrays)
            staging.replace_cached(tmp, path)
        except OSError as e:            # (a full disk: asked again next time)
            log.warning("place names: %s not written: %s", path, e)
