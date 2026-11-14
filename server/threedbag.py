"""Measured roofs for the Netherlands from 3DBAG (TU Delft 3D geoinformation group and 3DGI, CC BY 4.0).

PR #977 (Vabian124) asked 3DBAG's OGC API for each 200 m square from inside the building loop: CityJSON features of
~17 kB each with every LoD's geometry, 50 a page, a blocking request per square (up to 12 s), at most 120 buildings
a square. Reworked (2026-11-10): 3DBAG's WFS layer BAG3D:lod22 has one record per roof face (its slope, the way it
faces, its heights) for a whole 250 m square of the Dutch national grid (RD New, EPSG:28992) in one request (~0.7 MB
and ~1 s for central Amsterdam's 900 faces), through netfetch (its host policy) and kept on disk under
<cache>/3dbag/v1/; a road tile asks for its squares on a thread as it starts (Fetch), so the buildings wait for it
only where it is still coming. A failed square is asked again on a later build (not kept as empty).

Each building's faces give its measured roof: the ground (b3_h_maaiveld), the ridge (b3_h_nok, else the highest
face), the eaves (the sloped faces' lowest point), the pitch (the sloped faces' area-weighted slope) and the shape:
no sloped face flat, one facing skillion, two facing opposite ways gabled, three or more facing apart hipped (the
direction the main face faces is roof_direction, OSM's convention). A record joins the map's footprint whose
centroid is within MATCH_M of its own and whose area is within AREA_RATIO of its own (the BAG id when the map has
one), and fills only what the map does not say (as lod2roofs). Outside the Netherlands, offline, or where a square
failed: no records, today's roofs. BNE_3DBAG=off: as before.

    (c) 3DBAG by tudelft3d and 3DGI (CC BY 4.0)
"""
import gzip
import json
import logging
import math
import os
import threading

import numpy as np

log = logging.getLogger("earthd.threedbag")

ON = os.environ.get("BNE_3DBAG", "on") != "off"
CREDIT = "© 3DBAG by tudelft3d and 3DGI"
WFS = "https://data.3dbag.nl/api/BAG3D/wfs"
NL = (50.75, 3.2, 53.7, 7.3)            # south, west, north, east (no request outside)
SQUARE_M = 250.0                        # RD New squares fetched and kept whole
PAD_M = 30.0                            # each square's request reaches this far past it (a building on its edge)
MATCH_M = 12.0
AREA_RATIO = (0.55, 1.8)
SLOPED_DEG = 8.0                        # a face steeper than this is pitched
OPPOSITE_DEG = 35.0                     # two faces within this of facing opposite ways: a gable's two sides
SKILLION_MAX_M2 = 600.0                 # a single-slope roof larger than this is a complex roof's part
WAIT_S = 10.0                           # s the buildings wait for a tile's squares at most
VERSION = "v1"
PROPS = ("identificatie", "b3_pand_deel_id", "b3_dak_type", "b3_h_maaiveld", "b3_h_min", "b3_h_max", "b3_h_nok",
         "b3_azimut", "b3_hellingshoek", "b3_opp_grond", "oorspronkelijkbouwjaar", "geom")

# RD New <-> WGS84 (the polynomial approximation of Schreutelkamp and Strang van Hees, ~1 m)
_X0, _Y0 = 155000.0, 463000.0
_PHI0, _LAM0 = 52.15517440, 5.38720621
_RD_LAT = ((0, 1, 3235.65389), (2, 0, -32.58297), (0, 2, -0.24750), (2, 1, -0.84978), (0, 3, -0.06550),
           (2, 2, -0.01709), (1, 0, -0.00738), (4, 0, 0.00530), (2, 3, -0.00039), (4, 1, 0.00033), (1, 1, -0.00012))
_RD_LON = ((1, 0, 5260.52916), (1, 1, 105.94684), (1, 2, 2.45656), (3, 0, -0.81885), (1, 3, 0.05594),
           (3, 1, -0.05607), (0, 1, 0.01199), (3, 2, -0.00256), (1, 4, 0.00128), (0, 2, 0.00022),
           (2, 0, -0.00022), (5, 0, 0.00026))
_WGS_X = ((0, 1, 190094.945), (1, 1, -11832.228), (2, 1, -114.221), (0, 3, -32.391), (1, 0, -0.705),
          (3, 1, -2.340), (1, 3, -0.608), (0, 2, -0.008), (2, 3, 0.148))
_WGS_Y = ((1, 0, 309056.544), (0, 2, 3638.893), (2, 0, 73.077), (1, 2, -157.984), (3, 0, 59.788),
          (0, 1, 0.433), (2, 2, -6.439), (1, 1, -0.032), (0, 4, 0.092), (1, 4, -0.054))


def in_nl(lat, lon):
    return NL[0] <= float(lat) <= NL[2] and NL[1] <= float(lon) <= NL[3]


def rd_to_wgs(x, y):
    dx, dy = (float(x) - _X0) * 1e-5, (float(y) - _Y0) * 1e-5
    lat, lon = _PHI0, _LAM0
    for p, q, k in _RD_LAT:
        lat += k * dx ** p * dy ** q / 3600.0
    for p, q, k in _RD_LON:
        lon += k * dx ** p * dy ** q / 3600.0
    return lat, lon


def wgs_to_rd(lat, lon):
    dp, dl = 0.36 * (float(lat) - _PHI0), 0.36 * (float(lon) - _LAM0)
    x, y = _X0, _Y0
    for p, q, k in _WGS_X:
        x += k * dp ** p * dl ** q
    for p, q, k in _WGS_Y:
        y += k * dp ** p * dl ** q
    return x, y


def _bag_key(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits if len(digits) >= 10 else ""


def _ring_area_centroid(ring):
    """(area m^2, cx, cy) of a ring [(x, y), ...] (shoelace)."""
    r = np.asarray(ring, np.float64)[:, :2]
    if len(r) < 3:
        return 0.0, float(r[:, 0].mean()) if len(r) else 0.0, float(r[:, 1].mean()) if len(r) else 0.0
    x, y = r[:, 0], r[:, 1]
    xs, ys = np.roll(x, -1), np.roll(y, -1)
    cross = x * ys - xs * y
    a = cross.sum() / 2.0
    if abs(a) < 1e-9:
        return 0.0, float(x.mean()), float(y.mean())
    return abs(a), float(((x + xs) * cross).sum() / (6 * a)), float(((y + ys) * cross).sum() / (6 * a))


def _ang_diff(a, b):
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def records_from_faces(features):
    """The measured roof of each building of a WFS GeoJSON answer (one feature per roof face): [{bag, lat, lon, area,
    height, roof_height, roof_shape, roof_direction, year}]. Pure."""
    by = {}
    for f in features or ():
        p = f.get("properties") or {}
        bag = _bag_key(p.get("identificatie"))
        if not bag:
            continue
        g = f.get("geometry") or {}
        coords = g.get("coordinates") or []
        if g.get("type") == "MultiPolygon":
            coords = coords[0] if coords else []
        ring = coords[0] if coords else []
        if len(ring) < 3:
            continue
        area, cx, cy = _ring_area_centroid([c[:2] for c in ring])
        by.setdefault(bag, []).append((p, area, cx, cy))
    out = []
    for bag, faces in by.items():
        p0 = faces[0][0]
        try:
            ground = float(p0.get("b3_h_maaiveld"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(ground):
            continue
        tot = sum(a for _p, a, _x, _y in faces) or 1.0
        cx = sum(a * x for _p, a, x, _y in faces) / tot
        cy = sum(a * y for _p, a, _x, y in faces) / tot
        tops = [float(p.get("b3_h_max")) for p, *_ in faces if p.get("b3_h_max") is not None]
        nok = p0.get("b3_h_nok")
        ridge = float(nok) if nok is not None else (max(tops) if tops else None)
        if ridge is None or not math.isfinite(ridge):
            continue
        height = ridge - ground
        if not 2.0 <= height <= 400.0:
            continue
        sloped = [(p, a) for p, a, _x, _y in faces
                  if p.get("b3_hellingshoek") is not None and float(p["b3_hellingshoek"]) >= SLOPED_DEG
                  and p.get("b3_azimut") is not None]
        sloped_area = sum(a for _p, a in sloped)
        shape, direction, roof_h = "flat", None, 0.0
        if sloped and sloped_area >= 0.3 * tot:
            sloped.sort(key=lambda t: -t[1])
            main = float(sloped[0][0]["b3_azimut"]) % 360.0
            dirs = [float(p["b3_azimut"]) % 360.0 for p, a in sloped if a >= 0.08 * sloped_area]
            facing = [d for d in dirs if _ang_diff(d, main) < OPPOSITE_DEG]
            opposite = [d for d in dirs if _ang_diff(d, main) > 180.0 - OPPOSITE_DEG]
            apart = [d for d in dirs if OPPOSITE_DEG <= _ang_diff(d, main) <= 180.0 - OPPOSITE_DEG]
            if apart and opposite:
                shape = "hipped"
            elif opposite:
                shape = "gabled"
            elif not apart and len(facing) == len(dirs) and tot <= SKILLION_MAX_M2 and sloped_area >= 0.6 * tot:
                shape = "skillion"      # (a lean-to: one face over most of a modest roof; on a big block, complex)
            else:
                shape = None            # (a complex roof: the shape predicted, the heights kept)
            direction = main
            # (each face's own rise, eave to top: the area-weighted median over the sloped faces, the main face's
            # alone for a skillion; the lowest eave of all over the ridge stood a 4,100 m2 Amsterdam block's roof
            # 15 m tall, its courtyard's eaves 4 m up)
            rises = sorted((float(p["b3_h_max"]) - float(p["b3_h_min"]), a) for p, a in sloped
                           if p.get("b3_h_max") is not None and p.get("b3_h_min") is not None)
            if shape == "skillion" and sloped[0][0].get("b3_h_max") is not None \
                    and sloped[0][0].get("b3_h_min") is not None:
                roof_h = float(sloped[0][0]["b3_h_max"]) - float(sloped[0][0]["b3_h_min"])
            elif rises:
                half, acc = sum(a for _r, a in rises) / 2.0, 0.0
                for r_, a in rises:
                    acc += a
                    if acc >= half:
                        roof_h = r_
                        break
            roof_h = float(min(max(roof_h, 0.4), 0.6 * height))
        lat, lon = rd_to_wgs(cx, cy)
        year = p0.get("oorspronkelijkbouwjaar")
        try:
            area = float(p0.get("b3_opp_grond")) if p0.get("b3_opp_grond") is not None else tot
        except (TypeError, ValueError):
            area = tot
        out.append({"bag": bag, "lat": lat, "lon": lon, "area": area, "height": height,
                    "roof_height": roof_h if shape != "flat" else 0.0, "roof_shape": shape,
                    "roof_direction": direction,
                    "year": int(year) if isinstance(year, (int, float)) and math.isfinite(year) else None})
    return out


def squares_over(lat0, lon0, lat1, lon1):
    """[(ix, iy)] of the RD squares over a lat/lon box (none outside the Netherlands)."""
    if not (lat1 >= NL[0] and lat0 <= NL[2] and lon1 >= NL[1] and lon0 <= NL[3]):
        return []
    xs, ys = zip(*(wgs_to_rd(a, b) for a, b in ((lat0, lon0), (lat0, lon1), (lat1, lon0), (lat1, lon1))))
    return [(ix, iy) for ix in range(int(math.floor(min(xs) / SQUARE_M)), int(math.floor(max(xs) / SQUARE_M)) + 1)
            for iy in range(int(math.floor(min(ys) / SQUARE_M)), int(math.floor(max(ys) / SQUARE_M)) + 1)]


class Source:
    """The squares' records, on disk under <cache>/3dbag/<VERSION>/ and in memory (LRU), fetched through netfetch."""

    MEM = 64

    def __init__(self, cache_dir, fetcher=None):
        self.dir = os.path.join(cache_dir, "3dbag", VERSION)
        self.fetcher = fetcher
        self.mem = {}
        self.lock = threading.Lock()

    def _path(self, sq):
        return os.path.join(self.dir, f"{sq[0]}_{sq[1]}.json.gz")

    def square(self, sq):
        """The records of square sq, or None (not had: offline, failed: asked again next time)."""
        with self.lock:
            got = self.mem.get(sq)
        if got is not None:
            return got
        path = self._path(sq)
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                got = json.load(fh)
        except (OSError, ValueError):
            got = None
        if got is None:
            got = self._fetch(sq)
            if got is None:
                return None
            try:
                os.makedirs(self.dir, exist_ok=True)
                tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
                with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                    json.dump(got, fh, separators=(",", ":"))
                os.replace(tmp, path)
            except OSError as ex:
                log.debug("3dbag: keep %s: %s", sq, ex)
        with self.lock:
            self.mem[sq] = got
            while len(self.mem) > self.MEM:
                self.mem.pop(next(iter(self.mem)))
        return got

    def _fetch(self, sq):
        import netfetch
        fetcher = self.fetcher
        if fetcher is None:
            fetcher = self.fetcher = netfetch.Fetcher()
        x0, y0 = sq[0] * SQUARE_M - PAD_M, sq[1] * SQUARE_M - PAD_M
        x1, y1 = (sq[0] + 1) * SQUARE_M + PAD_M, (sq[1] + 1) * SQUARE_M + PAD_M
        url = (f"{WFS}?service=WFS&version=2.0.0&request=GetFeature&typeNames=BAG3D:lod22"
               f"&outputFormat=application/json&count=20000&propertyName={','.join(PROPS)}"
               f"&bbox={x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f},EPSG:28992")
        try:
            body = fetcher.get(url, kind="demand").body
            data = json.loads(body)
        except Exception as ex:             # (offline, limits, a bad answer: today's roofs, asked again later)
            log.debug("3dbag: square %s: %s: %s", sq, type(ex).__name__, ex)
            return None
        return records_from_faces(data.get("features"))


_SOURCES = {}
_SRC_LOCK = threading.Lock()
_POOL = None


def source(cache_dir):
    with _SRC_LOCK:
        got = _SOURCES.get(cache_dir)
        if got is None:
            got = _SOURCES[cache_dir] = Source(cache_dir)
        return got


class Fetch:
    """A road tile's squares asked for on a thread as the tile starts; records(lat, lon) waits WAIT_S at most."""

    def __init__(self, frame, x0, y0, x1, y1, cache_dir):
        global _POOL
        self.fut = None
        if not ON or not cache_dir:
            return
        la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1], np.float64),
                                         np.array([y0, y0, y1, y1], np.float64), 0.0)
        sqs = squares_over(float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo)))
        if not sqs:
            return
        with _SRC_LOCK:
            if _POOL is None:
                from concurrent.futures import ThreadPoolExecutor
                _POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="3dbag")
        src = source(cache_dir)
        self.fut = _POOL.submit(lambda: [src.square(sq) for sq in sqs])

    def index(self):
        """Matcher over the tile's records, or None (none here, or not had: the tile faults then)."""
        if self.fut is None:
            return None
        try:
            got = self.fut.result(timeout=WAIT_S)
        except Exception:
            got = None
        if got is None or any(g is None for g in got):
            import revisit
            revisit.fault()             # (a later visit builds it again with the measured roofs)
            got = [g for g in (got or ()) if g is not None]
        recs = [r for g in got for r in g]
        return Matcher(recs) if recs else None


class Matcher:
    """The records near a footprint: by BAG id, else the nearest centroid within MATCH_M of a similar area."""

    CELL = 32.0

    def __init__(self, recs):
        self.recs = recs
        self.by_bag = {r["bag"]: r for r in recs if r.get("bag")}
        self.grid = {}
        if recs:
            self.lat0 = recs[0]["lat"]
            self.k = 111320.0 * math.cos(math.radians(self.lat0))
        for r in recs:
            self.grid.setdefault(self._cell(r["lat"], r["lon"]), []).append(r)

    def _cell(self, lat, lon):
        return int(math.floor(lat * 111320.0 / self.CELL)), int(math.floor(lon * self.k / self.CELL))

    def match(self, lat, lon, area, bag_id=None):
        want = _bag_key(bag_id)
        if want and want in self.by_bag:
            return self.by_bag[want]
        cy, cx = self._cell(lat, lon)
        best, best_d = None, MATCH_M
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for r in self.grid.get((cy + dy, cx + dx), ()):
                    d = math.hypot((r["lat"] - lat) * 111320.0, (r["lon"] - lon) * self.k)
                    if d >= best_d:
                        continue
                    if area and r.get("area") and not AREA_RATIO[0] <= float(area) / float(r["area"]) <= AREA_RATIO[1]:
                        continue
                    best, best_d = r, d
        return best


def _known(v):
    try:
        return v is not None and math.isfinite(float(v)) and float(v) > 0
    except (TypeError, ValueError):
        return False


def apply(at, rec):
    """at with the measured roof of rec where the map says nothing itself (a mapped shape or height wins)."""
    if rec is None:
        return at
    out = dict(at)
    if not _known(out.get("height")):
        out["height"] = rec["height"]
    if not out.get("roof_shape") and rec.get("roof_shape"):
        out["roof_shape"] = rec["roof_shape"]
        if rec.get("roof_direction") is not None and out.get("roof_direction") in (None, ""):
            out["roof_direction"] = rec["roof_direction"]
        if rec["roof_shape"] != "flat" and not _known(out.get("roof_height")) and rec.get("roof_height"):
            out["roof_height"] = rec["roof_height"]
    if rec.get("year") and not out.get("_year"):
        out["_year"] = rec["year"]      # (its era: buildings.era)
    out["_3dbag"] = True
    return out


def credited_near(cache_dir, lat, lon, near_m):
    """Is a 3DBAG square with records on this computer within near_m of lat, lon (credits.feature_ids)? Disk only."""
    if not ON or not cache_dir or not in_nl(lat, lon):
        return False
    d = near_m / 111320.0
    k = max(0.05, math.cos(math.radians(lat)))
    for sq in squares_over(lat - d, lon - d / k, lat + d, lon + d / k):
        p = os.path.join(cache_dir, "3dbag", VERSION, f"{sq[0]}_{sq[1]}.json.gz")
        try:
            if os.path.getsize(p) > 40:
                return True
        except OSError:
            continue
    return False
