"""Airports and runways from OurAirports (docs/research/2026-10-11-airports.md; ROADMAP quick win 4).

    store = airports.Store(cache_dir, fetcher)          # the server's netfetch.Fetcher
    db = store.load()                                   # AirportDB: refreshed weekly, last good copy offline
    for d, rw in db.near(39.1754, -76.6683, 5000):      # runways within 5 km, nearest first
        rw.le.ident, rw.length_m, rw.width_m, rw.ground_model
    db.in_cell(mapcells.cell_of(lat, lon))              # runways whose graded footprint touches a z14 cell
    db.helipads_near(lat, lon, 2000)

Source: OurAirports' nightly CSV dump (public domain: "All data is released to the Public Domain, and
comes with no guarantee of accuracy or fitness for use.", https://ourairports.com/data/, read
2026-10-11 through the Internet Archive because ourairports.com's TLS certificate had expired that
day). Since 2021-11-03 the files live on GitHub Pages (DATA_URL; the repository davidmegginson/
ourairports-data is under The Unlicense); only airports.csv and runways.csv are used. Columns: the
data dictionary https://ourairports.com/help/data-dictionary.html (REQUIRED_* below).

Cache (<cache>/airports/, cache.py KEPT: counted, never evicted or cleared):
  ourairports/current.json           pointer: the generation in use, its download time, hashes, rows,
                                     the last refresh attempt and its error
  ourairports/<gen>/airports.csv     one generation (gen = UTC time + content hash); a new one is
  ourairports/<gen>/runways.csv      written to a temp dir, checked (columns, parse, at least
                                     MIN_ROWS_SHARE of the previous rows), renamed into place, then
                                     the pointer is replaced; older generations are deleted after
Refresh: a copy older than REFRESH_S (7 days) is downloaded again (kind "background" when a copy
exists: the limiter may drop it; "demand" for the first copy), at most once per RETRY_S (6 h) after
a failure; whatever fails, the last good copy is used (offline too). Same content: only the pointer's
check time moves. Requests go through netfetch (POLICIES["davidmegginson.github.io"]: 1 request at a
time, a hard daily cap) with Accept-Encoding: gzip (12.7 + 4.0 MB -> ~3 MB on the wire).

Model (deterministic: a pure function of the two CSV files; sorted by OurAirports' persistent integer
ids; Runway.key = "oa:<id>"): every runway has both ends (lat/lon, elevation m, true heading,
displaced threshold m), its length / width in m (width_source "data" or "default", by class),
surface (raw, class, FAA condition), BeamNG ground model (art/groundmodels.json names), lighted,
closed, and a `quality`:
  surveyed   both end coordinates given and consistent with the length (the normal case)
  derived    one end + its true heading + the length (the other end computed)
  arp        no end at all, but a true heading, a length, and the only runway of its airport: centred
             on the airport reference point (an estimate: excluded unless asked for)
Rows that cannot be placed are skipped and counted (AirportDB.stats). Helipads (le_ident H1, H2...,
or any surface at a heliport) are kept apart (AirportDB.helipads).

CLI (development):
    python3 server/airports.py --near 39.1754,-76.6683 --radius 5
    python3 server/airports.py --airport KBWI --json
    python3 server/airports.py --preview KBWI kbwi.png
"""
import argparse
import csv
import gzip
import hashlib
import io
import json
import logging
import math
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass

import numpy as np

import geodesy
import mapcells
import netfetch
import runwaygeom

log = logging.getLogger("earthd.airports")

VERSION = 1                     # parser version: bump when what a Runway holds changes
DATA_URL = "https://davidmegginson.github.io/ourairports-data/"
FILES = ("airports.csv", "runways.csv")
DEFAULT_CACHE = os.path.expanduser("~/.cache/beamng-earth")
REFRESH_S = 7 * 86400.0         # a copy older than this is downloaded again
RETRY_S = 6 * 3600.0            # after a failed refresh, the next attempt waits this long
MIN_ROWS_SHARE = 0.5            # a new copy with fewer rows than this share of the old one is refused
FT = 0.3048                     # m per international foot (exact)
EARTH_R = 6371008.8             # m, mean radius (great-circle prefilter only)
CREDIT = "Airports: OurAirports (public domain)"
LICENCE = "Public Domain (OurAirports: \"All data is released to the Public Domain, and comes with no " \
          "guarantee of accuracy or fitness for use.\")"

REQUIRED_AIRPORT = ("id", "ident", "type", "name", "latitude_deg", "longitude_deg", "elevation_ft",
                    "iso_country", "municipality")
REQUIRED_RUNWAY = ("id", "airport_ref", "airport_ident", "length_ft", "width_ft", "surface", "lighted",
                   "closed", "le_ident", "le_latitude_deg", "le_longitude_deg", "le_elevation_ft",
                   "le_heading_degT", "le_displaced_threshold_ft", "he_ident", "he_latitude_deg",
                   "he_longitude_deg", "he_elevation_ft", "he_heading_degT", "he_displaced_threshold_ft")

QUALITIES = ("surveyed", "derived", "arp")      # best first
MIN_LENGTH_M = 60.0             # shorter "runways" are data errors (or helipads under a runway ident)
COINCIDENT_M = 10.0             # ends closer than this: no ends
LENGTH_RATIO = (0.5, 1.5)       # end distance / length_ft outside this (and > 100 m off): inconsistent

# --- surfaces -----------------------------------------------------------------------------------
# OurAirports' surface is free text (668 distinct values on 2026-10-11): FAA 5010 codes (ASPH-G, TURF-F,
# GRVL-DIRT, PEM, TRTD...), ICAO-style abbreviations (ASP, CON, GRS, GRE), words in several languages
# and single letters from the Australian import (G, X, N, C, S, B, L) whose meaning is not documented
# (G is also used in Italy and the UK, where it reads as grass): those count as unknown.
SURFACE_TOKENS = {
    "asphalt": ("ASP", "ASPH", "ASPHALT", "ASPHALTIC", "ASFALT", "ASFALTO", "ASPHALTE", "BIT", "BITU",
                "BITUMEN", "BITUMINOUS", "BITUMINOSO", "TAR", "TARMAC", "MACADAM", "PEM", "BLACKTOP",
                "SEALED", "PAVED", "PAV", "PAVEMENT", "PER", "PERMANENT", "HARD", "COP", "COM",
                "COMPOSITE", "MAC", "BITUM", "TARRED"),
    "concrete": ("CON", "CONC", "CONCRETE", "CEMENT", "BETON", "HORMIGON", "CONCRETO"),
    "grass": ("TURF", "GRS", "GRASS", "GRASSED", "SOD", "GRAS", "GRAMA", "CESPED", "HERBE", "GAZON",
              "PASTO", "ERBA", "TRAWA", "GRAMADO"),
    "gravel": ("GRE", "GRVL", "GVL", "GRV", "GRAVEL", "GRAVA", "CASCALHO", "CRUSHED", "CORAL", "PICARRA",
               "LATERITE", "LAT", "COR", "CALICHE", "CALCRETE", "CINDER", "CINDERS", "SHALE", "ROCK", "STONE",
               "SCORIA", "RIPIO", "OILGRAVEL"),
    "dirt": ("DIRT", "EARTH", "SOIL", "GROUND", "CLAY", "CLA", "LOAM", "SILT", "TER", "TERRA", "TIERRA",
             "TERRE", "TERRENO", "BARRO", "UNPAVED", "NATURAL", "MUD", "LAKEBED", "SALT"),
    "sand": ("SAND", "SAN", "ARENA", "AREIA", "BEACH"),
    "water": ("WATER", "WAT", "H2O"),
    "brick": ("BRICK", "BRI", "COBBLESTONE", "PAVERS"),
    "snow": ("SNOW", "SNO", "NEIGE"),
    "ice": ("ICE",),
    "mats": ("MATS", "MAT", "PSP", "MARSTON", "PIERCED", "STEEL", "MET", "METAL", "ALUMINUM", "ALUM"),
    "wood": ("WOOD", "WOODEN", "DECK"),
    "rooftop": ("ROOF", "ROOFTOP", "ROOF-TOP"),
}
SURFACE_OF = {tok: cls for cls, toks in SURFACE_TOKENS.items() for tok in toks}
CONDITIONS = {"E": "excellent", "G": "good", "F": "fair", "P": "poor", "L": "failed"}
PAVED = ("asphalt", "concrete", "brick")
# BeamNG ground models (gameengine.zip art/groundmodels.json, 0.39.4; docs/research/
# 2026-10-10-world-data-sources.md §4.3). There is no CONCRETE model ("concrete" is an alias of ASPHALT)
# and no WATER model (a water runway is not ground: None).
GROUND_MODEL = {"asphalt": "ASPHALT", "concrete": "ASPHALT", "brick": "COBBLESTONE", "grass": "GRASS", "gravel": "GRAVEL",
                "dirt": "DIRT", "sand": "SAND", "water": None, "snow": "SNOW", "ice": "ICE",
                "mats": "METAL", "wood": "WOOD", "rooftop": None}
RUNWAY_IDENT = re.compile(r"^(0?[1-9]|[12][0-9]|3[0-6])([LCR]?)$")


def surface_class(raw, lighted=False):
    """(class, condition, ground model, paved, known) of an OurAirports surface text. The first
    recognised word is the main surface (FAA lists the dominant one first: ASPH-TURF is mostly
    asphalt); a trailing one-letter FAA condition (E/G/F/P/L) after it is the condition. An unknown
    or empty surface is asphalt when the runway is lighted (lit runways are paved in practice), dirt
    otherwise (known = False)."""
    txt = unicodedata.normalize("NFKD", raw or "").encode("ascii", "ignore").decode().upper()
    toks = [t for t in re.split(r"[^A-Z0-9]+", txt) if t]
    cls = cond = None
    for i, t in enumerate(toks):
        if cls is None:
            if t in SURFACE_OF:
                cls = SURFACE_OF[t]
            elif len(t) > 3 and t[:4] in ("ASPH", "CONC", "GRAS", "GRAV", "TURF", "DIRT"):
                cls = SURFACE_OF.get(t[:4], {"GRAS": "grass", "GRAV": "gravel"}.get(t[:4]))
        elif len(t) == 1 and t in CONDITIONS and cond is None:
            cond = CONDITIONS[t]
    known = cls is not None
    if cls is None:
        cls = "asphalt" if lighted else "dirt"
    gm = GROUND_MODEL[cls]
    if cls == "asphalt" and cond in ("poor", "failed"):
        gm = "ASPHALT_OLD"
    return cls, cond, gm, cls in PAVED, known


# --- the model ----------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RunwayEnd:
    ident: str                      # "10", "28L", "N", "" ...
    lat: float
    lon: float
    elev_m: float | None            # MSL (data, else the airport's elevation, else None: the DEM decides)
    elev_source: str                # "data" / "airport" / "none"
    heading: float                  # true degrees: the direction of a landing on this end
    displaced_m: float              # displaced threshold length (0 = none)
    position: str                   # "data" / "computed"


@dataclass(frozen=True, slots=True)
class Runway:
    id: int                         # OurAirports runway id (persistent)
    airport_id: int
    airport_ident: str
    airport_type: str
    airport_name: str
    country: str
    le: RunwayEnd                   # low-numbered end ("from")
    he: RunwayEnd                   # high-numbered end
    length_m: float                 # le -> he (surveyed: the ends' distance)
    width_m: float
    width_source: str               # "data" / "default"
    surface_raw: str
    surface: str                    # class (SURFACE_TOKENS keys)
    condition: str | None           # FAA condition word, if given
    ground_model: str | None        # BeamNG ground model name (None: water / rooftop)
    paved: bool
    surface_known: bool
    lighted: bool
    closed: bool
    quality: str                    # QUALITIES
    length_data_m: float | None     # length_ft as given (m)
    flags: tuple = ()               # data-quality notes ("length_mismatch", "elev_from_airport", ...)

    @property
    def key(self):
        return f"oa:{self.id}"

    @property
    def name(self):
        return f"{self.airport_ident} {self.le.ident or '?'}/{self.he.ident or '?'}"

    @property
    def center(self):
        """(lat, lon) of the midpoint (the ECEF chord's midpoint, dropped onto the ellipsoid)."""
        lat, lon = midpoints(np.array([[self.le.lat, self.le.lon]]), np.array([[self.he.lat, self.he.lon]]))
        return float(lat[0]), float(lon[0])

    @property
    def bearing(self):
        """True bearing le -> he in degrees [0, 360)."""
        return self.le.heading

    @property
    def water(self):
        return self.surface == "water"


@dataclass(frozen=True, slots=True)
class Helipad:
    id: int
    airport_ident: str
    airport_type: str
    ident: str
    lat: float
    lon: float
    position: str                   # "data" / "airport"
    length_m: float | None
    width_m: float | None
    surface: str
    ground_model: str | None
    lighted: bool
    closed: bool
    elev_m: float | None

    @property
    def key(self):
        return f"oa:{self.id}"


@dataclass(frozen=True, slots=True)
class Airport:
    id: int
    ident: str
    type: str
    name: str
    lat: float
    lon: float
    elev_m: float | None
    country: str
    municipality: str
    region: str = ""                # ISO 3166-2 (OurAirports iso_region, e.g. "US-CA"): furniture.lamp_region's state


def _num(v):
    v = (v or "").strip()
    if not v:
        return None
    try:
        x = float(v)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def _int(v, default=0):
    x = _num(v)
    return int(x) if x is not None else default


def _flag(v):
    return (v or "").strip().lower() in ("1", "yes", "true")


def _bearing(lat0, lon0, lat1, lon1):
    """True bearing (deg) and distance (m) from point 0 to point 1 (in 0's tangent plane)."""
    f = geodesy.EnuFrame(lat0, lon0, 0.0)
    e, n, _ = f.geodetic_to_enu(lat1, lon1, 0.0)
    return math.degrees(math.atan2(e, n)) % 360.0, math.hypot(e, n)


def _offset(lat, lon, heading, dist):
    """The point dist m from (lat, lon) along the true heading (tangent plane: mm-exact at runway lengths)."""
    f = geodesy.EnuFrame(lat, lon, 0.0)
    h = math.radians(heading)
    la, lo, _ = f.enu_to_geodetic(dist * math.sin(h), dist * math.cos(h), 0.0)
    return float(la), float(lo)


runway_code = runwaygeom.runway_code
midpoints = runwaygeom.midpoints


DEFAULT_WIDTH = {True: {1: 18.0, 2: 23.0, 3: 30.0, 4: 45.0}, False: {1: 15.0, 2: 18.0, 3: 23.0, 4: 30.0}}


def default_width(length_m, paved):
    """Width (m) when the data has none: ICAO Annex 14 3.1.10's minimum per code number for paved
    runways (18 / 23 / 30 / 45 m), a little narrower for unpaved strips."""
    return DEFAULT_WIDTH[bool(paved)][runway_code(length_m)]


def is_helipad(row, airport_type):
    le = (row.get("le_ident") or "").strip().upper()
    return bool(re.match(r"^H\d*$", le)) or (airport_type == "heliport" and not RUNWAY_IDENT.match(le))


def designator(ident, heading):
    """(number 1..36, side '', 'L', 'C' or 'R', from_heading) of a runway end: the data's ident when it
    is a valid designator, else the true heading / 10 (real designators use the magnetic heading; the
    difference is the local declination, so this guess is flagged)."""
    m = RUNWAY_IDENT.match((ident or "").strip().upper())
    if m:
        return int(m.group(1)), m.group(2), False
    n = int(math.floor(heading / 10.0 + 0.5)) % 36
    return (36 if n == 0 else n), "", True


# --- parsing ------------------------------------------------------------------------------------
class DataError(ValueError):
    """A CSV that is not the OurAirports file (missing columns, no rows)."""


def _rows(data, required, name):
    text = data.decode("utf-8-sig") if isinstance(data, (bytes, bytearray)) else data
    rd = csv.DictReader(io.StringIO(text, newline=""))
    missing = [c for c in required if c not in (rd.fieldnames or ())]
    if missing:
        raise DataError(f"{name}: columns missing: {', '.join(missing)}")
    return list(rd)


def _end(row, p, lat, lon, heading, position, airport_elev):
    e = _num(row.get(f"{p}_elevation_ft"))
    if e is not None:
        elev, src = e * FT, "data"
    elif airport_elev is not None:
        elev, src = airport_elev, "airport"
    else:
        elev, src = None, "none"
    d = _num(row.get(f"{p}_displaced_threshold_ft"))
    return RunwayEnd(ident=(row.get(f"{p}_ident") or "").strip(), lat=float(lat), lon=float(lon),
                     elev_m=elev, elev_source=src, heading=float(heading) % 360.0,
                     displaced_m=max(0.0, d * FT) if d else 0.0, position=position)


def build_runway(row, ap, single):
    """A Runway from one runways.csv row (ap: its Airport; single: the airport has only this runway),
    or (None, skip reason)."""
    flags = []
    length_data = _num(row.get("length_ft"))
    length_data = length_data * FT if length_data and length_data > 0 else None
    lighted, closed = _flag(row.get("lighted")), _flag(row.get("closed"))
    raw = (row.get("surface") or "").strip()
    cls, cond, gm, paved, known = surface_class(raw, lighted)
    if not known:
        flags.append("surface_unknown")
    le_lat, le_lon = _num(row.get("le_latitude_deg")), _num(row.get("le_longitude_deg"))
    he_lat, he_lon = _num(row.get("he_latitude_deg")), _num(row.get("he_longitude_deg"))
    le_h, he_h = _num(row.get("le_heading_degT")), _num(row.get("he_heading_degT"))
    has_le = le_lat is not None and le_lon is not None and abs(le_lat) <= 90 and abs(le_lon) <= 180
    has_he = he_lat is not None and he_lon is not None and abs(he_lat) <= 90 and abs(he_lon) <= 180
    if has_le and has_he:
        brg, dist = _bearing(le_lat, le_lon, he_lat, he_lon)
        if dist < COINCIDENT_M:
            has_he = False              # one point given twice: treat as one end
            flags.append("ends_coincide")
    quality = None
    if has_le and has_he:
        if length_data:
            r = dist / length_data
            if not (LENGTH_RATIO[0] <= r <= LENGTH_RATIO[1]) and abs(dist - length_data) > 100.0:
                return None, "inconsistent_ends"
            if abs(dist - length_data) > max(30.0, 0.05 * length_data):
                flags.append("length_mismatch")
        quality, length = "surveyed", dist
        if le_h is not None and abs((le_h - brg + 180) % 360 - 180) > 5:
            flags.append("heading_mismatch")
        le_pos = he_pos = "data"
        hdg = brg
    elif (has_le or has_he) and length_data:
        if has_le and le_h is not None:
            hdg = le_h
        elif has_he and he_h is not None:
            hdg = (he_h + 180.0) % 360.0
        elif le_h is not None:
            hdg = le_h
        elif he_h is not None:
            hdg = (he_h + 180.0) % 360.0
        else:
            return None, "no_heading"
        length = length_data
        if has_le:
            he_lat, he_lon = _offset(le_lat, le_lon, hdg, length)
            le_pos, he_pos = "data", "computed"
        else:
            le_lat, le_lon = _offset(he_lat, he_lon, (hdg + 180.0) % 360.0, length)
            le_pos, he_pos = "computed", "data"
        quality = "derived"
    elif single and length_data and (le_h is not None or he_h is not None):
        hdg = le_h if le_h is not None else (he_h + 180.0) % 360.0
        length = length_data
        le_lat, le_lon = _offset(ap.lat, ap.lon, (hdg + 180.0) % 360.0, length / 2)
        he_lat, he_lon = _offset(ap.lat, ap.lon, hdg, length / 2)
        le_pos = he_pos = "computed"
        quality = "arp"
    else:
        if not (has_le or has_he):
            return None, "no_position"
        return None, "no_length" if not length_data else "no_heading"
    if length < MIN_LENGTH_M:
        return None, "too_short"
    w = _num(row.get("width_ft"))
    if w and w > 0:
        width, wsrc = w * FT, "data"
        if width > 0.5 * length or width > 120.0:
            width, wsrc = default_width(length, paved), "default"
            flags.append("width_implausible")
    else:
        width, wsrc = default_width(length, paved), "default"
        flags.append("width_default")
    le = _end(row, "le", le_lat, le_lon, hdg, le_pos, ap.elev_m)
    he = _end(row, "he", he_lat, he_lon, (hdg + 180.0) % 360.0, he_pos, ap.elev_m)
    for e in (le, he):
        if e.elev_source != "data":
            flags.append(f"{'le' if e is le else 'he'}_elev_{e.elev_source}")
        if e.displaced_m > 0.5 * length:
            flags.append("displaced_implausible")
    return Runway(id=_int(row.get("id")), airport_id=ap.id, airport_ident=ap.ident, airport_type=ap.type,
                  airport_name=ap.name, country=ap.country, le=le, he=he, length_m=float(length),
                  width_m=float(width), width_source=wsrc, surface_raw=raw, surface=cls, condition=cond,
                  ground_model=gm, paved=paved, surface_known=known, lighted=lighted, closed=closed,
                  quality=quality, length_data_m=length_data, flags=tuple(flags)), None


def build_helipad(row, ap):
    lat, lon = _num(row.get("le_latitude_deg")), _num(row.get("le_longitude_deg"))
    pos = "data"
    if lat is None or lon is None:
        lat, lon, pos = ap.lat, ap.lon, "airport"
    lighted = _flag(row.get("lighted"))
    cls, _, gm, _, _ = surface_class(row.get("surface"), lighted)
    ln, w = _num(row.get("length_ft")), _num(row.get("width_ft"))
    e = _num(row.get("le_elevation_ft"))
    return Helipad(id=_int(row.get("id")), airport_ident=ap.ident, airport_type=ap.type,
                   ident=(row.get("le_ident") or "").strip(), lat=float(lat), lon=float(lon), position=pos,
                   length_m=ln * FT if ln else None, width_m=w * FT if w else None, surface=cls,
                   ground_model=gm, lighted=lighted, closed=_flag(row.get("closed")),
                   elev_m=e * FT if e is not None else ap.elev_m)


# --- the index ----------------------------------------------------------------------------------
def _hav(lat0, lon0, lat, lon):
    """Great-circle distance (m) from one point to arrays of points (spherical: a prefilter)."""
    p0, p = math.radians(lat0), np.radians(lat)
    dp, dl = p - p0, np.radians(lon) - math.radians(lon0)
    a = np.sin(dp / 2) ** 2 + math.cos(p0) * np.cos(p) * np.sin(dl / 2) ** 2
    return 2 * EARTH_R * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def _band(lat_sorted, order, lat, lon, lons, r_m):
    """Indices (ascending) of the points within a lat/lon box sure to hold every point within r_m (great circle) of
    (lat, lon): a prefilter before _hav (the same points pass, ~47k runways otherwise each query). lat_sorted / order:
    the points' latitudes sorted and their argsort; lons the points' longitudes. None: no box (a pole or the
    antimeridian within reach) -> test them all."""
    dlat = math.degrees(r_m / EARTH_R) * 1.001 + 1e-9
    if abs(lat) + dlat >= 89.0:
        return None
    dlon = dlat / math.cos(math.radians(abs(lat) + dlat))
    if abs(lon) + dlon >= 180.0:
        return None
    i0 = int(np.searchsorted(lat_sorted, lat - dlat, side="left"))
    i1 = int(np.searchsorted(lat_sorted, lat + dlat, side="right"))
    cand = order[i0:i1]
    cand = cand[np.abs(lons[cand] - lon) <= dlon]
    return np.sort(cand)


def _seg_dist(e0, n0, e1, n1):
    """Distance from the origin to the segment (e0, n0)-(e1, n1), arrays."""
    de, dn = e1 - e0, n1 - n0
    ll = de * de + dn * dn
    t = np.where(ll > 0, np.clip(-(e0 * de + n0 * dn) / np.where(ll > 0, ll, 1.0), 0.0, 1.0), 0.0)
    return np.hypot(e0 + t * de, n0 + t * dn)


class AirportDB:
    """Every airport, runway and helipad of one OurAirports snapshot, with spatial queries. A pure
    function of the two files: rows sorted by their integer ids, so results never depend on the
    file's row order."""

    def __init__(self, airports, runways, helipads, stats, snapshot):
        self.airports = airports                        # ident -> Airport
        self.runways = runways                          # list, sorted by id
        self.helipads = helipads                        # list, sorted by id
        self.stats = stats                              # Counter: parsed / skipped by reason / flags
        self.snapshot = snapshot                        # "oa-<12 hex>" (content hash of both files)
        self._by_airport = {}
        for i, r in enumerate(runways):
            self._by_airport.setdefault(r.airport_ident, []).append(i)
        n = len(runways)
        self._le = np.array([(r.le.lat, r.le.lon) for r in runways], np.float64).reshape(n, 2)
        self._he = np.array([(r.he.lat, r.he.lon) for r in runways], np.float64).reshape(n, 2)
        self._mid = np.stack(midpoints(self._le, self._he), axis=1) if n else np.zeros((0, 2))
        self._half = np.array([r.length_m / 2 for r in runways], np.float64)
        self._qual = np.array([QUALITIES.index(r.quality) for r in runways], np.int8)
        self._closed = np.array([r.closed or r.airport_type == "closed" for r in runways], bool)
        self._hp = np.array([(h.lat, h.lon) for h in helipads], np.float64).reshape(len(helipads), 2)
        self._mid_order = np.argsort(self._mid[:, 0], kind="stable")
        self._mid_lat = self._mid[self._mid_order, 0]
        self._hp_order = np.argsort(self._hp[:, 0], kind="stable")
        self._hp_lat = self._hp[self._hp_order, 0]
        self._half_max = float(self._half.max()) if n else 0.0
        self._cells = {}                                # in_cell memo: (key, margin, quality, closed) -> runways

    def _near_mid(self, lat, lon, r_m):
        """Indices of the runways whose midpoint may be within r_m (all of them when no box helps)."""
        c = _band(self._mid_lat, self._mid_order, lat, lon, self._mid[:, 1], r_m)
        return np.arange(len(self.runways)) if c is None else c

    @classmethod
    def from_csv(cls, airports_csv, runways_csv, snapshot=None):
        if snapshot is None:
            snapshot = snapshot_id(airports_csv, runways_csv)
        arows = _rows(airports_csv, REQUIRED_AIRPORT, "airports.csv")
        rrows = _rows(runways_csv, REQUIRED_RUNWAY, "runways.csv")
        if not arows or not rrows:
            raise DataError("airports.csv / runways.csv without rows")
        stats = Counter()
        airports = {}
        for a in sorted(arows, key=lambda a: _int(a.get("id"))):
            lat, lon = _num(a.get("latitude_deg")), _num(a.get("longitude_deg"))
            ident = (a.get("ident") or "").strip()
            if lat is None or lon is None or not ident or ident in airports:
                stats["airport_skipped"] += 1
                continue
            e = _num(a.get("elevation_ft"))
            airports[ident] = Airport(id=_int(a.get("id")), ident=ident, type=(a.get("type") or "").strip(),
                                      name=(a.get("name") or "").strip(), lat=lat, lon=lon,
                                      elev_m=e * FT if e is not None else None,
                                      country=(a.get("iso_country") or "").strip(),
                                      municipality=(a.get("municipality") or "").strip(),
                                      region=(a.get("iso_region") or "").strip())
        stats["airports"] = len(airports)
        rrows.sort(key=lambda r: _int(r.get("id")))
        count = Counter()
        for r in rrows:
            ap = airports.get((r.get("airport_ident") or "").strip())
            if ap is not None and not is_helipad(r, ap.type):
                count[ap.ident] += 1
        runways, helipads = [], []
        for r in rrows:
            ap = airports.get((r.get("airport_ident") or "").strip())
            if ap is None:
                stats["skip_no_airport"] += 1
                continue
            if is_helipad(r, ap.type):
                helipads.append(build_helipad(r, ap))
                continue
            rw, why = build_runway(r, ap, count[ap.ident] == 1)
            if rw is None:
                stats[f"skip_{why}"] += 1
                continue
            runways.append(rw)
            stats[f"quality_{rw.quality}"] += 1
            for f in rw.flags:
                stats[f"flag_{f}"] += 1
        stats["runways"] = len(runways)
        stats["helipads"] = len(helipads)
        stats["runway_rows"] = len(rrows)
        # only the airports with a runway or helipad kept (every lookup goes through those; the rest, ~70k strips
        # and heliports without one, cost ~50 MB of strings in every server process, a Steam Deck 2026-10-20)
        used = {r.airport_ident for r in runways} | {h.airport_ident for h in helipads}
        airports = {k: a for k, a in airports.items() if k in used}
        return cls(airports, runways, helipads, stats, snapshot)

    # --- lookups ----------------------------------------------------------------------------
    def airport(self, ident):
        return self.airports.get(ident)

    def runways_of(self, ident, min_quality="arp", include_closed=True):
        q = QUALITIES.index(min_quality)
        return [self.runways[i] for i in self._by_airport.get(ident, ())
                if self._qual[i] <= q and (include_closed or not self._closed[i])]

    def _select(self, min_quality, include_closed):
        m = self._qual <= QUALITIES.index(min_quality)
        if not include_closed:
            m &= ~self._closed
        return m

    def near(self, lat, lon, radius_m, min_quality="derived", include_closed=False):
        """[(distance m, Runway)] of the runways whose centreline comes within radius_m of the point,
        nearest first (ties by id). Closed runways (or runways of closed airports) only on request;
        `arp` (estimated) runways only with min_quality="arp"."""
        if not self.runways:
            return []
        c = self._near_mid(lat, lon, radius_m + self._half_max)
        m = self._select(min_quality, include_closed)[c]
        d0 = _hav(lat, lon, self._mid[c, 0], self._mid[c, 1]) - self._half[c]
        idx = c[np.flatnonzero(m & (d0 <= radius_m))]
        if not len(idx):
            return []
        f = geodesy.EnuFrame(lat, lon, 0.0)
        e0, n0, _ = f.geodetic_to_enu(self._le[idx, 0], self._le[idx, 1], 0.0)
        e1, n1, _ = f.geodetic_to_enu(self._he[idx, 0], self._he[idx, 1], 0.0)
        d = _seg_dist(np.asarray(e0), np.asarray(n0), np.asarray(e1), np.asarray(n1))
        out = [(float(dd), self.runways[i]) for dd, i in zip(d, idx) if dd <= radius_m]
        out.sort(key=lambda x: (round(x[0], 3), x[1].id))
        return out

    def helipads_near(self, lat, lon, radius_m, include_closed=False):
        if not self.helipads:
            return []
        c = _band(self._hp_lat, self._hp_order, lat, lon, self._hp[:, 1], radius_m)
        c = np.arange(len(self.helipads)) if c is None else c
        d = _hav(lat, lon, self._hp[c, 0], self._hp[c, 1])
        out = [(float(d[j]), self.helipads[i]) for j, i in zip(np.flatnonzero(d <= radius_m), c[d <= radius_m])
               if include_closed or not (self.helipads[i].closed or self.helipads[i].airport_type == "closed")]
        out.sort(key=lambda x: (round(x[0], 3), x[1].id))
        return out

    def in_cell(self, key, margin_m=None, min_quality="derived", include_closed=False):
        """Runways whose influence footprint (runwaygeom.footprint: the graded strip plus the largest
        blend, or the runway rectangle grown by margin_m) touches the z14 cell `key` (any z), sorted by
        id. Exact rectangle-against-cell test in the cell's tangent plane."""
        key = mapcells.check(mapcells.CellKey(*key))
        memo = (tuple(key), margin_m, min_quality, include_closed)
        hit = self._cells.get(memo)
        if hit is not None:
            return list(hit)
        out = self._in_cell(key, margin_m, min_quality, include_closed)
        if len(self._cells) >= 4096:
            self._cells.clear()
        self._cells[memo] = tuple(out)
        return out

    def _in_cell(self, key, margin_m, min_quality, include_closed):
        lon0, lat0, lon1, lat1 = key.bounds()
        clat, clon = key.center()
        half_diag = _hav(lat0, lon0, np.array([lat1]), np.array([lon1]))[0] / 2
        extra = (runwaygeom.MAX_REACH if margin_m is None else margin_m) + 200.0
        c = self._near_mid(clat, clon, half_diag + self._half_max + extra)
        m = self._select(min_quality, include_closed)[c]
        reach = self._half[c] + extra
        idx = c[np.flatnonzero(m & (_hav(clat, clon, self._mid[c, 0], self._mid[c, 1]) <= half_diag + reach))]
        if not len(idx):
            return []
        f = geodesy.EnuFrame(clat, clon, 0.0)
        cx, cy, _ = f.geodetic_to_enu(np.array([lat0, lat0, lat1, lat1]), np.array([lon0, lon1, lon1, lon0]), 0.0)
        cell_poly = np.stack([cx, cy], axis=1)
        out = []
        for i in idx:
            rw = self.runways[i]
            poly = runwaygeom.footprint_polygon(rw, f, margin_m)
            if _convex_overlap(poly, cell_poly):
                out.append(rw)
        return out

    def group(self, rw, min_quality="derived", include_closed=False):
        """The runways graded together with rw (runwaygeom.groups: influence rectangles that overlap,
        transitively), in grading order. Built from the whole file around rw, so a cell or block that
        touches any of them grades the same set in the same order."""
        reach = 2 * (runwaygeom.MAX_REACH + runwaygeom.PROTECT_M)
        found, todo = {rw.id: rw}, [rw]
        while todo:
            r = todo.pop()
            lat, lon = r.center
            for _, o in self.near(lat, lon, r.length_m / 2 + reach, min_quality, include_closed):
                if o.id not in found:
                    found[o.id] = o
                    todo.append(o)
        for g in runwaygeom.groups(found.values()):
            if any(o.id == rw.id for o in g):
                return g
        return [rw]

    def groups_in_cell(self, key, min_quality="derived", include_closed=False):
        """The grading groups (lists of runways) that touch cell `key`, each whole, ordered by their
        first runway's id."""
        out = {}
        for rw in self.in_cell(key, None, min_quality, include_closed):
            if not any(rw.id in ids for ids in out):
                g = self.group(rw, min_quality, include_closed)
                out[frozenset(o.id for o in g)] = g
        return [g for _, g in sorted(out.items(), key=lambda kv: min(kv[0]))]

    def summary(self):
        return {"snapshot": self.snapshot, **dict(sorted(self.stats.items()))}


def _convex_overlap(a, b):
    """Separating-axis test for two convex polygons ((k, 2) arrays)."""
    for poly in (a, b):
        k = len(poly)
        for i in range(k):
            ex, ey = poly[(i + 1) % k] - poly[i]
            nx, ny = -ey, ex
            pa = a @ np.array([nx, ny])
            pb = b @ np.array([nx, ny])
            if pa.max() < pb.min() or pb.max() < pa.min():
                return False
    return True


def snapshot_id(airports_csv, runways_csv):
    h = hashlib.sha256()
    for d in (airports_csv, runways_csv):
        h.update(hashlib.sha256(d).digest())
    return "oa-" + h.hexdigest()[:12]


# --- download, cache, refresh -------------------------------------------------------------------
class AirportsUnavailable(IOError):
    """No copy on disk and the download failed (or --offline)."""


class Store:
    """The OurAirports files under <cache>/airports/ourairports/ (see the module docstring)."""

    def __init__(self, cache_dir=DEFAULT_CACHE, fetcher=None, wall=time.time, base_url=DATA_URL):
        self.root = os.path.join(cache_dir, "airports", "ourairports")
        self.fetcher = fetcher if fetcher is not None else netfetch.Fetcher()
        self.wall = wall
        self.base_url = base_url
        self._lock = threading.Lock()
        self._db = None                 # (generation, AirportDB)

    @property
    def pointer_path(self):
        return os.path.join(self.root, "current.json")

    def pointer(self):
        try:
            with open(self.pointer_path, encoding="utf-8") as f:
                p = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(p, dict) or not isinstance(p.get("gen"), str) or "/" in p["gen"] or \
                not all(os.path.exists(os.path.join(self.root, p["gen"], n)) for n in FILES):
            return None
        return p

    def _write_pointer(self, p):
        os.makedirs(self.root, exist_ok=True)
        tmp = f"{self.pointer_path}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(p, f, indent=1, sort_keys=True)
        os.replace(tmp, self.pointer_path)

    def _download(self, name, kind):
        r = self.fetcher.get(self.base_url + name, kind=kind, headers={"Accept-Encoding": "gzip"})
        body = r.body
        if (r.headers.get("content-encoding") or "").lower() == "gzip" or body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        return body, len(r.body), r.headers.get("last-modified")

    def refresh(self, kind="background", force=False):
        """Download both files now (unless the copy is fresh and not force). Returns the pointer of the
        copy in use afterwards; raises netfetch.FetchError / DataError when it failed (the old copy,
        if any, stays in use)."""
        with self._lock:
            old = self.pointer()
            now = self.wall()
            if old and not force and now - old.get("checked", old.get("downloaded", 0)) < REFRESH_S:
                return old
            try:
                bodies, wire, modified = {}, 0, {}
                for n in FILES:
                    bodies[n], w, modified[n] = self._download(n, kind)
                    wire += w
                db = AirportDB.from_csv(bodies["airports.csv"], bodies["runways.csv"])
                rows = {"airports": db.stats["airports"], "runway_rows": db.stats["runway_rows"]}
                if old:
                    for k, v in (old.get("rows") or {}).items():
                        if rows.get(k, 0) < MIN_ROWS_SHARE * v:
                            raise DataError(f"{k}: {rows.get(k, 0)} rows, the copy in use has {v}: refused")
            except (netfetch.FetchError, DataError, OSError, EOFError) as e:
                if old is not None:
                    old["attempt"], old["error"] = now, f"{type(e).__name__}: {e}"
                    self._write_pointer(old)
                raise
            if old and old.get("snapshot") == db.snapshot:
                old.update(checked=now, attempt=now, error=None)
                self._write_pointer(old)
                return old
            gen = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + "-" + db.snapshot[3:]
            tmpdir = os.path.join(self.root, f".{gen}.{threading.get_ident()}.tmp")
            os.makedirs(tmpdir, exist_ok=True)
            for n in FILES:
                with open(os.path.join(tmpdir, n), "wb") as f:
                    f.write(bodies[n])
            final = os.path.join(self.root, gen)
            if os.path.exists(final):
                shutil.rmtree(final)
            os.replace(tmpdir, final)
            p = {"gen": gen, "snapshot": db.snapshot, "downloaded": now, "checked": now, "attempt": now,
                 "error": None, "source": self.base_url, "last_modified": modified, "rows": rows,
                 "bytes": {n: len(bodies[n]) for n in FILES}, "wire_bytes": wire, "version": VERSION}
            self._write_pointer(p)
            for d in os.listdir(self.root):         # older generations and stale temp dirs
                full = os.path.join(self.root, d)
                if d != gen and os.path.isdir(full):
                    shutil.rmtree(full, ignore_errors=True)
            self._db = (gen, db)
            log.info("airports: OurAirports %s: %d airports, %d runways, %d helipads (%.1f MB on the wire)",
                     db.snapshot, db.stats["airports"], db.stats["runways"], db.stats["helipads"], wire / 1e6)
            return p

    def load(self, max_age=REFRESH_S, kind=None):
        """The AirportDB of the copy on disk, refreshed first when it is older than max_age (and the
        last failed attempt is older than RETRY_S). Any refresh failure falls back to the copy on
        disk; with no copy at all, AirportsUnavailable."""
        p = self.pointer()
        now = self.wall()
        stale = p is None or now - p.get("checked", p.get("downloaded", 0)) >= max_age
        recent_fail = p is not None and p.get("error") and now - p.get("attempt", 0) < RETRY_S
        if stale and not recent_fail:
            try:
                p = self.refresh(kind=kind or ("background" if p else "demand"), force=True)
            except (netfetch.FetchError, DataError, OSError, EOFError) as e:
                if p is None:
                    raise AirportsUnavailable(f"OurAirports: no copy on disk and the download failed: {e}") from e
                log.warning("airports: refresh failed (%s); using the copy from %s", e,
                            time.strftime("%Y-%m-%d", time.gmtime(p.get("downloaded", 0))))
                p = self.pointer() or p
        with self._lock:
            if self._db is not None and self._db[0] == p["gen"]:
                return self._db[1]
            d = os.path.join(self.root, p["gen"])
            db = self._parsed(d)
            if db is None:
                with open(os.path.join(d, "airports.csv"), "rb") as f:
                    a = f.read()
                with open(os.path.join(d, "runways.csv"), "rb") as f:
                    r = f.read()
                db = AirportDB.from_csv(a, r)
                self._keep_parsed(d, db)
            self._db = (p["gen"], db)
            return db

    # The parsed snapshot, pickled next to its CSV files: the server's main process and every worker parsed the
    # 86k airports themselves, ~257 MB a process for ~50 MB kept (a Steam Deck, 2026-10-20). PARSED_V: raise it when
    # the classes change.
    PARSED_V = 4

    def _parsed(self, d):
        import pickle
        path = os.path.join(d, f"parsed.v{self.PARSED_V}.pickle")
        try:
            with open(path, "rb") as f:
                db = pickle.load(f)
            return db if isinstance(db, AirportDB) else None
        except (OSError, EOFError, pickle.UnpicklingError, AttributeError, ImportError, TypeError, ValueError):
            return None

    def _keep_parsed(self, d, db):
        import pickle
        path = os.path.join(d, f"parsed.v{self.PARSED_V}.pickle")
        for old in os.listdir(d):           # an older version's copy
            if old.startswith("parsed.v") and old.endswith(".pickle") and old != os.path.basename(path):
                try:
                    os.remove(os.path.join(d, old))
                except OSError:
                    pass
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "wb") as f:
                pickle.dump(db, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, path)
        except OSError as e:
            log.debug("airports: parsed copy not kept: %s", e)
            try:
                os.remove(tmp)
            except OSError:
                pass


# --- CLI ----------------------------------------------------------------------------------------
def runway_row(rw, dist=None):
    return {"key": rw.key, "airport": rw.airport_ident, "name": rw.airport_name, "type": rw.airport_type,
            "runway": f"{rw.le.ident or '?'}/{rw.he.ident or '?'}",
            "dist_m": None if dist is None else round(dist, 1),
            "length_m": round(rw.length_m, 1), "width_m": round(rw.width_m, 1), "width_source": rw.width_source,
            "surface": rw.surface_raw, "class": rw.surface, "ground_model": rw.ground_model,
            "lighted": rw.lighted, "closed": rw.closed, "quality": rw.quality,
            "le": {"ident": rw.le.ident, "lat": round(rw.le.lat, 7), "lon": round(rw.le.lon, 7),
                   "elev_m": None if rw.le.elev_m is None else round(rw.le.elev_m, 1), "elev_source": rw.le.elev_source,
                   "heading": round(rw.le.heading, 2), "displaced_m": round(rw.le.displaced_m, 1),
                   "position": rw.le.position},
            "he": {"ident": rw.he.ident, "lat": round(rw.he.lat, 7), "lon": round(rw.he.lon, 7),
                   "elev_m": None if rw.he.elev_m is None else round(rw.he.elev_m, 1), "elev_source": rw.he.elev_source,
                   "heading": round(rw.he.heading, 2), "displaced_m": round(rw.he.displaced_m, 1),
                   "position": rw.he.position},
            "flags": list(rw.flags)}


def _latlon(text):
    lat, lon = (float(v) for v in text.split(","))
    return lat, lon


def main(argv=None):
    ap = argparse.ArgumentParser(description="BeamNG.Earth airports and runways from OurAirports (development tool)")
    ap.add_argument("--near", type=_latlon, help="lat,lon: list the runways around this point")
    ap.add_argument("--radius", type=float, default=5.0, help="km (with --near; default 5)")
    ap.add_argument("--airport", help="list one airport's runways (its ident, e.g. KBWI)")
    ap.add_argument("--preview", nargs=2, metavar=("IDENT", "PNG"),
                    help="top-down render of an airport's (or one runway key's, oa:<id>) graded height field "
                         "and markings")
    ap.add_argument("--px", type=float, default=2.0, help="preview: metres per pixel (default 2)")
    ap.add_argument("--all", action="store_true", help="include closed runways and estimated (arp) placements")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--stats", action="store_true", help="print the data-quality counts of the whole file")
    ap.add_argument("--refresh", action="store_true", help="download now, whatever the copy's age")
    ap.add_argument("--cache-dir", default=DEFAULT_CACHE)
    ap.add_argument("--offline", action="store_true", help="disk cache only (elevation too)")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    fetcher = netfetch.Fetcher(offline=a.offline, stats_dir=os.path.join(a.cache_dir, "netstats"))
    try:
        store = Store(a.cache_dir, fetcher)
        t0 = time.perf_counter()
        if a.refresh:
            store.refresh(kind="demand", force=True)
        db = store.load()
        t_load = time.perf_counter() - t0
        mq, ic = ("arp", True) if a.all else ("derived", False)
        if a.stats:
            print(json.dumps({"load_s": round(t_load, 2), **db.summary()}, indent=1))
        if a.near:
            t1 = time.perf_counter()
            res = db.near(a.near[0], a.near[1], a.radius * 1000.0, min_quality=mq, include_closed=ic)
            dt = time.perf_counter() - t1
            if a.json:
                print(json.dumps({"query_ms": round(dt * 1000, 2), "runways": [runway_row(r, d) for d, r in res]},
                                 indent=1, ensure_ascii=False))
            else:
                print(f"{len(res)} runways within {a.radius:g} km of {a.near[0]:.5f},{a.near[1]:.5f} "
                      f"(OurAirports {db.snapshot}; load {t_load:.2f} s, query {dt * 1000:.1f} ms)")
                for d, r in res:
                    print(f"{d:8.0f} m  {r.airport_ident:8} {r.le.ident or '?':>3}/{r.he.ident or '?':<3} "
                          f"{r.length_m:6.0f} x {r.width_m:4.0f} m{'*' if r.width_source != 'data' else ' '} "
                          f"{r.surface_raw[:14]:14} {str(r.ground_model):12} {r.quality:8} "
                          f"{'lit ' if r.lighted else '    '}{'CLOSED ' if r.closed else ''}{r.airport_name}")
        if a.airport:
            rows = db.runways_of(a.airport)
            if a.json:
                print(json.dumps([runway_row(r) for r in rows], indent=1, ensure_ascii=False))
            else:
                apt = db.airport(a.airport)
                print(apt)
                for r in rows:
                    print(json.dumps(runway_row(r), ensure_ascii=False))
        if a.preview:
            import airportpreview
            rep = airportpreview.preview(db, a.preview[0], a.preview[1], a.cache_dir, fetcher, px=a.px,
                                         include_closed=True)
            print(json.dumps(rep, indent=1, ensure_ascii=False))
        return 0
    except (AirportsUnavailable, netfetch.FetchError, DataError, KeyError) as e:
        print(f"airports: {e}", file=sys.stderr)
        return 1
    finally:
        fetcher.close()


if __name__ == "__main__":
    sys.exit(main())
