"""Read-only NVDB V4 tunnel data for Norwegian BeamNG.Earth tiles.

The game never waits for HTTP: a small daemon fetches new cells and caches
validated results.  An explicit PowerShell prefetch can warm a location before
playing.  No NVDB data is fabricated when the API is unavailable.
"""

import datetime as dt
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen


LOG = logging.getLogger("earthd.norway_tunnel_nvdb")
HOST = "nvdbapiles.atlas.vegvesen.no"
BASE = "https://" + HOST + "/vegobjekter/api/v4/vegobjekter/"
TYPES = (69, 67, 581)    # portals, bores, tunnel objects
X_CLIENT = "BeamNG.Earth-Norway-Tunnel-Rebuild"
CACHE = Path(__file__).resolve().parent / "data" / "norway_tunnel_nvdb_v025"
# Elevation-bearing type-67 geometry must not be confused with the old
# 2-D-only v02 cache. Keep both caches so rolling back is non-destructive.
SCHEMA = 3
RADIUS_M = 1250
PAGE_LIMIT = 4
RESULT_LIMIT = 500
DETAIL_LIMIT = 6
MAX_JSON_BYTES = 4 * 1024 * 1024
TIMEOUT_S = 12
POS_TOLERANCE_M = 45.0  # Portal road anchors can differ from OSM mouths by ~30 m.
_COORD_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_cache_memory = {}
_jobs = queue.Queue(maxsize=8)
_scheduled = set()
_started = False
_lock = threading.RLock()


class NVDBError(RuntimeError):
    pass


def country_bounds(lat, lon):
    return 57.9 <= lat <= 71.4 and 4.0 <= lon <= 31.5


def radius_bounds(lat, lon, radius_m=RADIUS_M):
    dlat = radius_m / 111320.0
    dlon = dlat / max(math.cos(math.radians(lat)), 0.05)
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def cell_key(lat, lon):
    # ~1.1km north/south and 0.5km east/west at Norwegian latitudes.
    return f"{round(float(lat) * 100):04d}_{round(float(lon) * 100):04d}"


def _file(key, cache_dir=CACHE):
    if not re.fullmatch(r"\d{4}_\d{4}", key):
        raise ValueError("Invalid cell key")
    return Path(cache_dir) / (key + ".json")


def query_url(type_id, lat, lon, radius_m=RADIUS_M):
    if type_id not in TYPES or not country_bounds(lat, lon):
        raise ValueError("NVDB query outside permitted country/type")
    bbox = ",".join(f"{v:.7f}" for v in radius_bounds(lat, lon, radius_m))
    # The V4 API accepts repeated 'inkluder' parameters. Unlike the previous
    # experimental survey, requests include the mandatory X-Client header.
    params = (("kartutsnitt", bbox), ("srid", "4326"), ("antall", "100"),
              ("inkluder", "metadata"), ("inkluder", "egenskaper"),
              ("inkluder", "geometri"), ("inkluder", "lokasjon"))
    return BASE + str(type_id) + "?" + urlencode(params)


def _trusted(url, type_id):
    p = urlsplit(str(url))
    return (p.scheme == "https" and p.hostname == HOST and p.port is None and
            p.username is None and p.password is None and bool(p.query) and
            p.path.rstrip("/") in (f"/vegobjekter/api/v4/vegobjekter/{type_id}",
                                  f"/vegobjekter/vegobjekter/{type_id}",
                                  f"/vegobjekter/{type_id}"))


def _page_link(href, current_url, type_id):
    """Resolve official absolute/relative V4 links without leaving one type."""
    raw = str(href)
    if not raw or len(raw) > 2048:
        raise NVDBError("Missing or oversized next-page link")
    if raw.startswith("?"):
        next_url = urljoin(current_url, raw)
    elif not urlsplit(raw).scheme and not raw.startswith("/"):
        next_url = urljoin("https://" + HOST + "/vegobjekter/", raw)
    else:
        next_url = urljoin(current_url, raw)
    if not _trusted(next_url, type_id):
        # Show URL *shape* only (cursor/query values need not enter logs).
        p = urlsplit(next_url)
        keys = ",".join(sorted(parse_qs(p.query)))
        raise NVDBError(f"NVDB next-page link has unexpected format: "
                        f"host={p.hostname!r}, path={p.path!r}, query_keys={keys!r}")
    return next_url


def fetch_json(url, timeout=TIMEOUT_S):
    req = Request(url, headers={"Accept": "application/json",
                                "X-Client": X_CLIENT,
                                "User-Agent": "BeamNG.Earth-Norway-Tunnels/0.2"})
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read(MAX_JSON_BYTES + 1)
    except HTTPError as exc:
        # RFC 7807 details are essential to diagnose 400 instead of claiming
        # the selected region contains no NVDB records.
        detail = exc.read(2500).decode("utf-8", "replace").replace("\n", " ")
        raise NVDBError(f"HTTP {exc.code}: {detail[:650]}") from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise NVDBError(f"Network error: {exc}") from exc
    if len(raw) > MAX_JSON_BYTES:
        raise NVDBError("Response exceeded 4 MiB")
    try:
        result = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise NVDBError("Unexpected non-JSON response") from exc
    if not isinstance(result, dict):
        raise NVDBError("Unexpected top-level JSON type")
    return result


def pages(type_id, lat, lon, radius_m=RADIUS_M, request=fetch_json):
    """Follow only bounded same-host links; fail closed on truncation."""
    url = query_url(type_id, lat, lon, radius_m)
    objects, seen_url, seen_id = [], set(), set()
    for page in range(PAGE_LIMIT):
        if not _trusted(url, type_id):
            raise NVDBError(f"Invalid NVDB page URL on page {page + 1}")
        if url in seen_url:
            raise NVDBError(f"NVDB repeated a page link on page {page + 1}; "
                            f"kept {len(objects)} unique records before stopping")
        seen_url.add(url)
        data = request(url)
        current = data.get("objekter")
        if not isinstance(current, list):
            raise NVDBError("Response missing 'objekter' list")
        for item in current:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                continue
            if item["id"] not in seen_id:
                seen_id.add(item["id"])
                objects.append(item)
        if len(objects) > RESULT_LIMIT:
            raise NVDBError("Exceeded local object limit; reduce search radius")
        if not current:
            # V4 documents a final *empty page*, sometimes with the last
            # cursor/next marker repeated.  That is the normal end, not an
            # error or an instruction to fetch the same page forever.
            return objects
        nxt = (data.get("metadata") or {}).get("neste")
        if not nxt or not isinstance(nxt, dict) or not nxt.get("href"):
            return objects
        url = _page_link(nxt["href"], url, type_id)
    raise NVDBError("Pagination exceeded safety limit; reduce search radius")


def _utm_to_wgs84(easting, northing, zone=33):
    """Inverse WGS84 UTM for NVDB own geometry (no third-party dependency).

    Supports Norway's documented 32N/33N/34N SRIDs. We validate the
    transformed position before allowing it to influence a game portal.
    """
    if not (-300000 <= easting <= 1500000 and 6200000 <= northing <= 8100000):
        return None
    a, ecc2, k0 = 6378137.0, 0.0066943799901413165, 0.9996
    ep2 = ecc2 / (1.0 - ecc2)
    m = northing / k0
    mu = m / (a * (1 - ecc2 / 4 - 3 * ecc2 ** 2 / 64 - 5 * ecc2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - ecc2)) / (1 + math.sqrt(1 - ecc2))
    phi = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu) +
           (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu) +
           151 * e1 ** 3 / 96 * math.sin(6 * mu) +
           1097 * e1 ** 4 / 512 * math.sin(8 * mu))
    s, c = math.sin(phi), math.cos(phi)
    t = math.tan(phi) ** 2
    cc = ep2 * c ** 2
    nn = a / math.sqrt(1 - ecc2 * s ** 2)
    rr = a * (1 - ecc2) / (1 - ecc2 * s ** 2) ** 1.5
    d = (easting - 500000) / (nn * k0)
    lat = phi - (nn * math.tan(phi) / rr) * (d ** 2 / 2 -
        (5 + 3 * t + 10 * cc - 4 * cc ** 2 - 9 * ep2) * d ** 4 / 24 +
        (61 + 90 * t + 298 * cc + 45 * t ** 2 - 252 * ep2 - 3 * cc ** 2) * d ** 6 / 720)
    lon = math.radians(zone * 6 - 183) + (d - (1 + 2 * t + cc) * d ** 3 / 6 +
          (5 - 2 * cc + 28 * t - 3 * cc ** 2 + 8 * ep2 + 24 * t ** 2) * d ** 5 / 120) / c
    result = (math.degrees(lat), math.degrees(lon))
    return result if 55 < result[0] < 73 and 3 < result[1] < 33 else None


def _wkt_coords(wkt, srid=4326):
    if not isinstance(wkt, str):
        return []
    head = wkt.upper().split("(", 1)[0]
    if not any(name in head for name in ("POINT", "LINESTRING")):
        return []
    dim = 3 if " Z" in head or "ZM" in head else 2
    if "M " in head and "Z" not in head:
        dim = 3
    text = wkt.split("(", 1)[-1].rstrip(")")
    coords = []
    zones = {5972: 32, 25832: 32, 32632: 32,
             5973: 33, 25833: 33, 32633: 33, 6173: 33,
             5974: 34, 25834: 34, 32634: 34}
    try:
        srid = int(srid)
    except (ValueError, TypeError):
        return []
    if srid != 4326 and srid not in zones:
        return []
    for piece in text.replace("(", "").replace(")", "").split(","):
        values = _COORD_RE.findall(piece)
        if len(values) >= dim:
            x, y = float(values[0]), float(values[1])
            if not (math.isfinite(x) and math.isfinite(y)):
                continue
            if srid == 4326:
                # NVDB V4 has returned lat/lon (61.54, 10.00) in WKT even
                # though many WKT producers use lon/lat (10.00, 61.54).
                # These axes are unambiguous in mainland Norway; require a
                # single valid interpretation and never guess elsewhere.
                candidates = [(y, x), (x, y)]
                valid = [p for p in candidates if country_bounds(*p)]
                value = valid[0] if len(valid) == 1 else None
            else:
                value = _utm_to_wgs84(x, y, zones[srid])
            if value is not None:
                coords.append(value)
    return coords


def _wkt_line3(wkt, srid):
    """Bounded, *explicitly WGS84* NVDB centreline (lat, lon, altitude).

    Do not accept an unlabelled property WKT: NVDB sends such properties in
    UTM coordinates even when the outer response requests EPSG:4326.  The
    3-D points represent *surveyed tunnel geometry*, not an inferred DEM.
    """
    if not isinstance(wkt, str) or len(wkt) > 400000 or not re.match(
            r"^\s*LINESTRING\s+Z\s*\(", wkt, re.I):
        return []
    try:
        if int(srid) != 4326:
            return []
    except (ValueError, TypeError):
        return []
    points = []
    for piece in wkt.split("(", 1)[1].rstrip().rstrip(")").split(","):
        values = _COORD_RE.findall(piece)
        if len(values) != 3:
            return []
        x, y, z = map(float, values)
        if not all(map(math.isfinite, (x, y, z))) or not -300 < z < 2500:
            return []
        valid = [(a, b) for a, b in ((x, y), (y, x)) if country_bounds(a, b)]
        if len(valid) != 1:
            return []
        points.append((valid[0][0], valid[0][1], z))
        if len(points) > 5000:
            return []
    if len(points) < 2:
        return []
    # Max 192 samples per object for cheap, predictable per-tile matching.
    if len(points) > 192:
        indices = [round(i * (len(points) - 1) / 191) for i in range(192)]
        points = [points[i] for i in indices]
    return [[round(lat, 8), round(lon, 8), round(z, 3)] for lat, lon, z in points]


def _bore_line(obj):
    """NVDB 67 tunnel axis; reject edge/collar and unlabelled property lines."""
    for where, geo in _geometry_candidates(obj):
        if where == "geometri":
            line = _wkt_line3(geo.get("wkt"), geo.get("srid"))
            if line:
                return line
    return []


def _geometry_candidates(obj):
    """Official V4 geometry may live at the object, property, or location."""
    options = []
    geoms = obj.get("geometri")
    if isinstance(geoms, dict):
        options.append(("geometri", geoms))
    elif isinstance(geoms, list):
        options.extend(("geometri", v) for v in geoms if isinstance(v, dict))
    for prop in obj.get("egenskaper") or ():
        if not isinstance(prop, dict) or "geometri" not in str(prop.get("navn", "")).lower():
            continue
        value = prop.get("verdi", prop.get("geometri"))
        if isinstance(value, dict):
            options.insert(0, ("egenskaper", value))
        elif isinstance(value, list):
            options[:0] = [("egenskaper", v) for v in value if isinstance(v, dict)]
        elif isinstance(value, str):
            # In real NVDB V4 responses these *raw* property coordinates
            # remained UTM33 even when top-level geometri was requested as
            # WGS84. The property has no SRID, so we must not label it 4326.
            # Trust explicit top-level or location geometry instead.
            options.insert(0, ("egenskaper", {"wkt": value, "srid": None}))
    loc = obj.get("lokasjon") or {}
    if isinstance(loc, dict):
        value = loc.get("geometri")
        if isinstance(value, dict):
            options.append(("lokasjon.geometri", value))
    return options


def _own_geometry(obj, prefer_location=False):
    """Use explicitly labelled geometry; portal road anchors get priority."""
    candidates = _geometry_candidates(obj)
    if prefer_location:
        # Tunnelportal egengeometri can describe a line across a concrete
        # collar, while lokasjon.geometri places it on the actual road.
        candidates.sort(key=lambda item: item[0] != "lokasjon.geometri")
    for _where, geometry in candidates:
        coords = _wkt_coords(geometry.get("wkt"), geometry.get("srid", 4326))
        if coords:
            return coords
    return []


def _geometry_hint(obj):
    """Small public-data structure report, no huge WKT text or credentials."""
    options = _geometry_candidates(obj)
    return {"top_keys": sorted(obj.keys()),
            "geometry_candidates": [{"where": where, "srid": value.get("srid"),
                                     "wkt_kind": str(value.get("wkt", "")).split("(", 1)[0][:30]}
                                    for where, value in options[:5]],
            "location_keys": sorted((obj.get("lokasjon") or {}).keys())
            if isinstance(obj.get("lokasjon"), dict) else []}


def _properties(obj):
    found = {}
    for prop in obj.get("egenskaper") or ():
        if not isinstance(prop, dict):
            continue
        name = str(prop.get("navn", "")).strip().lower()
        if name and not name.startswith("geometri"):
            value = prop.get("verdi")
            if isinstance(value, (str, int, float, bool)):
                found[name] = value
    return found


def _shape(props):
    value = str(next((v for k, v in props.items() if "tverrsnitt" in k), "")).lower()
    if any(word in value for word in ("rektang", "firkant", "box")):
        return "box"
    if any(word in value for word in ("sirkul", "buet", "hvelv", "bue")):
        return "arch"
    return None


def summarize(type_id, objects):
    result = []
    for obj in objects:
        coords = _own_geometry(obj, prefer_location=(type_id == 69))
        if not coords:
            continue  # No coordinate => never match a portal by object ID alone.
        props = _properties(obj)
        mid = coords[len(coords) // 2] if len(coords) == 1 else (
            (coords[0][0] + coords[-1][0]) / 2,
            (coords[0][1] + coords[-1][1]) / 2)
        row = {"id": obj["id"], "lat": round(mid[0], 8),
               "lon": round(mid[1], 8), "kind": type_id}
        if type_id == 69:
            row["shape"] = _shape(props)
            row["properties"] = {k: v for k, v in props.items()
                                  if "tverrsnitt" in k or k == "lengde"}
        elif type_id == 67:
            line = _bore_line(obj)
            if line:
                row["line3d"] = line
        result.append(row)
    return result


def _enrich_missing_geometry(type_id, objects, request):
    """V4 list responses can omit geometry; GET-by-ID returns complete data.

    For performance, hydrate at most six actual records per local query. This
    is only needed for types 69/67, never for the often geometry-less parent 581.
    """
    if type_id not in (69, 67):
        return objects, []
    enriched, warnings, fetched = [], [], 0
    for obj in objects:
        if _own_geometry(obj) or fetched >= DETAIL_LIMIT:
            enriched.append(obj)
            continue
        object_id = obj.get("id")
        if not isinstance(object_id, int) or not 0 < object_id < 10 ** 12:
            enriched.append(obj)
            continue
        fetched += 1
        url = BASE + f"{type_id}/{object_id}?inkluder=alle&srid=4326"
        try:
            detail = request(url)
            if isinstance(detail, dict) and detail.get("id") == object_id:
                obj = {**obj, **detail}
            else:
                warnings.append(f"type {type_id} id {object_id}: unexpected detail response")
        except NVDBError as exc:
            warnings.append(f"type {type_id} id {object_id}: {exc}")
        enriched.append(obj)
    return enriched, warnings


def fetch_cell(lat, lon, radius_m=RADIUS_M, request=fetch_json):
    """Read-only record fetch. Errors are distinct from true empty responses."""
    if not country_bounds(lat, lon) or radius_m < 100 or radius_m > 2500:
        raise ValueError("Only bounded locations within Norway are allowed")
    report = {"schema": SCHEMA, "source": "Statens vegvesen / NVDB V4 (NLOD)",
              "lat": float(lat), "lon": float(lon), "radius_m": radius_m,
              "fetched_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "records": {}, "counts": {}, "errors": {},
              "geometry_hints": {}, "detail_warnings": {}}
    for type_id in TYPES:
        try:
            raw = pages(type_id, lat, lon, radius_m, request=request)
            report["counts"][str(type_id)] = len(raw)
            raw, warnings = _enrich_missing_geometry(type_id, raw, request)
            if warnings:
                report["detail_warnings"][str(type_id)] = warnings[:DETAIL_LIMIT]
            report["records"][str(type_id)] = summarize(type_id, raw)
            if raw and not report["records"][str(type_id)]:
                report["geometry_hints"][str(type_id)] = _geometry_hint(raw[0])
        except NVDBError as exc:
            report["errors"][str(type_id)] = str(exc)
            report["records"][str(type_id)] = []
    report["ok"] = not report["errors"]
    return report


def save_cell(report, cache_dir=CACHE):
    key = cell_key(report["lat"], report["lon"])
    path = _file(key, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    with _lock:
        _cache_memory[key] = report
    return path


def _read(key, cache_dir=CACHE):
    with _lock:
        if key in _cache_memory:
            return _cache_memory[key]
    path = _file(key, cache_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != SCHEMA or not isinstance(data.get("records"), dict):
            return None
    except (OSError, ValueError, AttributeError):
        return None
    with _lock:
        _cache_memory[key] = data
    return data


def _worker():
    while True:
        key, lat, lon = _jobs.get()
        try:
            result = fetch_cell(lat, lon)
            save_cell(result)
            if result["errors"]:
                LOG.warning("NVDB tunnel data for %s partially unavailable: %s", key, result["errors"])
        except Exception as exc:
            LOG.warning("NVDB tunnel fetch for %s failed: %s", key, exc)
        finally:
            with _lock:
                _scheduled.discard(key)
            _jobs.task_done()


def _schedule(lat, lon):
    global _started
    if os.environ.get("BNE_NORWAY_TUNNEL_NVDB_OFFLINE") == "1":
        return
    key = cell_key(lat, lon)
    with _lock:
        if key in _scheduled:
            return
        _scheduled.add(key)
        if not _started:
            threading.Thread(target=_worker, name="nvdb-tunnel-fetch", daemon=True).start()
            _started = True
        try:
            _jobs.put_nowait((key, lat, lon))
        except queue.Full:
            _scheduled.discard(key)


def _distance_m(a_lat, a_lon, b_lat, b_lon):
    dx = (b_lon - a_lon) * 111320 * math.cos(math.radians((a_lat + b_lat) / 2))
    dy = (b_lat - a_lat) * 111320
    return math.hypot(dx, dy)


def _neighbour_keys(lat, lon):
    i, j = round(lat * 100), round(lon * 100)
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            yield f"{i + di:04d}_{j + dj:04d}"


def portal_assessment(lat, lon, schedule=True, cache_dir=CACHE):
    """Return (matched portal, status): present, no_portal, or unknown.

    Only a successfully completed type-69 query containing *zero* objects
    establishes the no-portal fallback. Nonzero records with unusable geometry,
    conflicting matches, incomplete cache, and API errors remain unknown.
    The caller must already have a mapped tunnel: an empty NVDB response must
    never create a tunnel by itself.
    """
    if not country_bounds(lat, lon):
        return None, "unknown"
    reports = [_read(key, cache_dir) for key in _neighbour_keys(lat, lon)]
    # A CLI-warmed point is reusable across small grid boundaries. Only
    # schedule another request if no successful nearby cache covers this point.
    covering = [data for data in reports if data and
                "69" not in data.get("errors", {}) and
                _distance_m(lat, lon, data["lat"], data["lon"]) <
                min(data.get("radius_m", 0) - 100, 1150)]
    if not covering and schedule and cache_dir == CACHE and _read(cell_key(lat, lon)) is None:
        _schedule(lat, lon)  # Never blocks road generation.
    rows = []
    seen_ids = set()
    for data in reports:
        for row in (data or {}).get("records", {}).get("69", []):
            if row["id"] not in seen_ids:
                seen_ids.add(row["id"])
                rows.append(row)
    candidates = []
    for record in rows:
        d = _distance_m(lat, lon, record["lat"], record["lon"])
        if d <= POS_TOLERANCE_M:
            candidates.append((d, record))
    if not candidates:
        # Older caches and failed requests might have zero *usable* geometry
        # while holding actual NVDB objects: that is not a rock-cut verdict.
        if covering and all(data.get("counts", {}).get("69") == 0 for data in covering):
            return None, "no_portal"
        return None, "unknown"
    candidates.sort(key=lambda r: r[0])
    # Close competing records in a twin tunnel: do not arbitrarily pick one.
    if len(candidates) > 1 and candidates[1][0] < candidates[0][0] + 4:
        return None, "unknown"
    return candidates[0][1], "present"


def portal_near(lat, lon, schedule=True, cache_dir=CACHE):
    """Compatibility: return a verified NVDB 69 portal, otherwise None."""
    return portal_assessment(lat, lon, schedule=schedule, cache_dir=cache_dir)[0]


def bore_lines_near(lat, lon, schedule=True, cache_dir=CACHE):
    """Cached NVDB 67 3-D tunnel axes potentially crossing a Norway location.

    Never blocks game rendering. A stored zero or failed query cannot create a
    tunnel; the geometry consumer separately validates every axis against the
    actual mapped tunnel coordinates before using any elevation.
    """
    if not country_bounds(lat, lon):
        return []
    reports = [_read(key, cache_dir) for key in _neighbour_keys(lat, lon)]
    covering = any(data and "67" not in data.get("errors", {}) and
                   _distance_m(lat, lon, data["lat"], data["lon"]) <
                   min(data.get("radius_m", 0) - 100, 1150)
                   for data in reports)
    if not covering and schedule and cache_dir == CACHE and _read(cell_key(lat, lon)) is None:
        _schedule(lat, lon)
    seen, rows = set(), []
    for report in reports:
        for record in (report or {}).get("records", {}).get("67", []):
            if record.get("id") in seen:
                continue
            seen.add(record.get("id"))
            line = record.get("line3d")
            if isinstance(line, list) and 2 <= len(line) <= 192:
                try:
                    distance = min(_distance_m(lat, lon, p[0], p[1]) for p in line)
                except (TypeError, ValueError, IndexError):
                    continue
                if distance <= 150:
                    rows.append(record)
    return rows

