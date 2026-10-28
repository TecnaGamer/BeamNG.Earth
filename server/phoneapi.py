"""The phone app's place search, a dropped pin's name and the road route: `/api/search`, `/api/reverse` and
`/api/route` on the map's 127.0.0.1 http endpoint (maptiles.MapTiles.respond) and, through these same functions, the
mod's socket messages (earthPhone.lua: the game's UI browser cannot reach http at all, so the phone asks over the
socket).

Places: the saved places of the game's user folder (places.lua writes settings/beamngEarth_places.json) first, then
Photon (geocode.py: autocomplete-safe, its own rate limits) nearest the car first. Routing: OSRM with the
turn-by-turn steps (routing.py).
"""
import json
import math
import os
import time
import urllib.parse

import geocode
import routing

LIMIT = 7
NEAR_KM = 0.03          # a Photon hit this close to a saved place is that place
NAME_MAX = 60
EARTH_KM = 6371.0088
BIAS_ZOOM = 12          # Photon's bias area around the car, and how hard it pulls (local suggestions first)
BIAS_SCALE = 0.4
CACHE_S = 120.0         # a query's answer is reused this long: typing a name asks once per few keystrokes
CACHE_MAX = 96
_cache = {}             # (query, lat, lon) -> (monotonic time, results, error)


def distance_km(lat0, lon0, lat1, lon1):
    """Great-circle distance (km). Pure."""
    p0, p1 = math.radians(lat0), math.radians(lat1)
    dp, dl = p1 - p0, math.radians(lon1 - lon0)
    a = math.sin(dp / 2) ** 2 + math.cos(p0) * math.cos(p1) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(min(1.0, a)))


def places_file(user_dir):
    """The saved places' file in the game's user folder."""
    return os.path.join(user_dir or "", "settings", "beamngEarth_places.json")


def bookmarks(user_dir):
    """[{name, lat, lon}] of the saved places ([] when there is no file or it cannot be read)."""
    try:
        with open(places_file(user_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for p in (data or {}).get("places") or []:
        if not isinstance(p, dict):
            continue
        try:
            lat, lon = float(p["lat"]), float(p["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            out.append({"name": str(p.get("name") or "?")[:NAME_MAX], "lat": round(lat, 6), "lon": round(lon, 6)})
    return out


def _words(text):
    return "".join(c if c.isalnum() else " " for c in str(text).casefold()).split()


def matches(query, name):
    """The query's words all start a word of name ("main st" -> "Main Street"). Pure."""
    have = _words(name)
    want = _words(query)
    return bool(want) and all(any(h.startswith(w) for h in have) for w in want)


def _hit(name, lat, lon, detail, source, at):
    d = distance_km(at[0], at[1], lat, lon) if at else None
    return {"name": name, "lat": round(lat, 6), "lon": round(lon, 6),
            "distance_km": None if d is None else round(d, 2), "detail": detail, "source": source}


def search(query, lat=None, lon=None, user_dir=None, net=None, limit=LIMIT):
    """(results, error): the saved places matching query first, then Photon's places nearest first, each
    {name, lat, lon, distance_km, detail, source}. error: a short text when Photon could not be asked."""
    query = (query or "").strip()
    at = (float(lat), float(lon)) if lat is not None and lon is not None else None
    out = [_hit(p["name"], p["lat"], p["lon"], "Saved place", "saved", at)
           for p in bookmarks(user_dir) if matches(query, p["name"])]
    error = None
    try:
        remote = geocode.search(query, lat, lon, net, zoom=BIAS_ZOOM if at else None,
                                bias=BIAS_SCALE if at else None)
    except Exception as e:              # offline, rate limited: the saved places still answer
        remote, error = [], f"{type(e).__name__}: {e}"[:120]
    near = [_hit(r["name"], r["lat"], r["lon"], r.get("detail") or "", "osm", at) for r in remote]
    if at:
        near.sort(key=lambda r: r["distance_km"])
    kept = out[:limit]
    for r in near:
        if len(kept) >= limit:
            break
        if [k for k in out if k["name"] == r["name"]
                or distance_km(k["lat"], k["lon"], r["lat"], r["lon"]) < NEAR_KM]:
            continue
        kept.append(r)
    return kept, error


def search_cached(query, lat=None, lon=None, user_dir=None, net=None, limit=LIMIT, clock=time.monotonic):
    """search() behind a short cache: the phone asks on every few keystrokes, Photon allows about one a second."""
    key = ((query or "").strip().lower(),
           None if lat is None else round(float(lat), 3), None if lon is None else round(float(lon), 3))
    hit = _cache.get(key)
    if hit and clock() - hit[0] < CACHE_S:
        return hit[1][:limit], hit[2]
    results, error = search(query, lat, lon, user_dir, net, limit)
    if len(_cache) >= CACHE_MAX:
        _cache.clear()
    _cache[key] = (clock(), results, error)
    return results, error


def reverse(lat, lon, net=None):
    """{name, detail} of the place at (lat, lon) for a dropped pin; a coordinates label when Photon fails."""
    try:
        name, detail = geocode.point_name(lat, lon, net)
    except Exception:
        name, detail = "", ""
    if not name:
        name = f"{float(lat):.5f}, {float(lon):.5f}"
    return {"name": name[:NAME_MAX], "detail": detail[:NAME_MAX], "lat": round(float(lat), 6),
            "lon": round(float(lon), 6)}


def route(lat0, lon0, lat1, lon1, net=None, bearing=None, steps=True):
    """The road route with its steps (routing.plan). Raises like it."""
    return routing.plan(lat0, lon0, lat1, lon1, net, bearing, steps=steps)


def _num(args, key):
    """The query's float (None when absent); ValueError when it is not a number."""
    v = args.get(key)
    if v is None or v == "":
        return None
    return float(v)


def _json(status, obj):
    return status, "application/json", json.dumps(obj, separators=(",", ":")).encode()


class PhoneApi:
    """`/api/search?q=&lat=&lon=` (a JSON array) and `/api/route?lat0=&lon0=&lat1=&lon1=&bearing=` (a JSON object)
    on the map's http server; `user_dir` is where the game keeps the saved places."""

    def __init__(self, user_dir=None, net=None):
        self.user_dir, self.net = user_dir, net
        self.stats = {"search": 0, "route": 0, "reverse": 0, "failed": 0}

    def respond(self, method, path):
        """(status, content type, body) for an /api/ path, None for anything else."""
        p, _, query = path.partition("?")
        if not p.startswith("/api/"):
            return None
        if method not in ("GET", "HEAD"):
            return _json(405, {"error": "GET only"})
        args = {k: v[-1] for k, v in urllib.parse.parse_qs(query).items()}
        try:
            if p == "/api/search":
                return self._search(args)
            if p == "/api/route":
                return self._route(args)
            if p == "/api/reverse":
                return self._reverse(args)
        except ValueError as e:
            return _json(400, {"error": f"bad parameter: {e}"})
        except Exception as e:                  # offline, no route, rate limited
            self.stats["failed"] += 1
            return _json(503, {"error": f"{type(e).__name__}: {e}"[:200]})
        return _json(404, {"error": "no such api"})

    def _search(self, args):
        query = (args.get("q") or "").strip()
        if not query:
            return _json(400, {"error": "q is required"})
        lat, lon = _num(args, "lat"), _num(args, "lon")
        if (lat is None) != (lon is None):
            return _json(400, {"error": "lat and lon go together"})
        limit = int(_num(args, "limit") or LIMIT)
        results, error = search_cached(query, lat, lon, self.user_dir, self.net, max(1, min(limit, 20)))
        self.stats["search"] += 1
        if error and not results:
            return _json(503, {"error": error})
        return 200, "application/json", json.dumps(results, separators=(",", ":")).encode()

    def _reverse(self, args):
        lat, lon = _num(args, "lat"), _num(args, "lon")
        if lat is None or lon is None:
            return _json(400, {"error": "lat and lon are required"})
        self.stats["reverse"] += 1
        return _json(200, reverse(lat, lon, self.net))

    def _route(self, args):
        try:
            lat0, lon0 = float(args["lat0"]), float(args["lon0"])
            lat1, lon1 = float(args["lat1"]), float(args["lon1"])
        except (KeyError, ValueError):
            return _json(400, {"error": "lat0, lon0, lat1, lon1 are required"})
        bearing = _num(args, "bearing")
        r = route(lat0, lon0, lat1, lon1, self.net, bearing)
        self.stats["route"] += 1
        return _json(200, r)
