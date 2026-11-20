"""The bus lines near the player, as OpenStreetMap has them: for driving a real line in the game (GitHub #1363 "Bus
routes missions in city"; the user, 2026-11-17: "yes start the bus lines").

A line comes from a route=bus or route=trolleybus relation. It has a reference number, colour, termini and ordered
stops. A Public Transport v2 stop is often represented by a road stop position and a nearby platform. Legacy
relations can store both directions together. One bounded Overpass lookup per place
(one alternate endpoint on an outage), on demand only (the game's Bus lines button). Successful replies are kept
for the session and for 30 days on disk: no geometry is downloaded, only the stops.

Measured 2026-11-17, a 1.3 x 1.8 km box in central Göteborg: 85 lines, all Public Transport v2, every stop named.
"""
import calendar
import hashlib
import json
import logging
import math
import os
import re
from pathlib import Path
import tempfile
import time
import urllib.parse

import dmath

import gtfsbus
import netfetch

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_FALLBACK_URL = "https://overpass.private.coffee/api/interpreter"
log = logging.getLogger(__name__)
RADIUS_M = 700.0                # lines with a stop within this of the player
SNAP_DEG = 0.005                # the request's centre is rounded to this, so a few metres' drive asks nothing new
CACHE_TTL_S = 30 * 86400
MAX_LINES = 40
MAX_STOPS = 120
STALE_S = 14 * 86400           # s: an Overpass whose data (osm3s.timestamp_osm_base) is older is skipped, as a failure
                               # (overpass.private.coffee answered from 2026-06-01 data on 2026-11-19: no stops in
                               # downtown Atlanta, which overpass-api.de had; such an answer would be kept 30 days)
JOIN_M = 60.0                   # m: a platform this near the stop_position before it is that stop's platform
# A platform's own letter or number (Västtrafik's läge: name=A, ref=A beside the stop "Fässbergsskolan", measured
# 2026-11-17 near Mölndal: 250 of the 8 lines' stops were named only that), not a stop's name
_CODE = re.compile(r"[A-Za-z]{1,2}\d{0,2}|\d{1,2}")
_STOP_ROLE = re.compile(
    r"(?:(forward|backward)[_:])?(stop|platform)(?:_entry_only|_exit_only|[_:]([0-9]{1,9}))?"
)
_MASTER_TAGS = ("ref", "name", "network", "operator", "colour")
_ROUTE_MODES = ("bus", "trolleybus")
DEFAULT_COLOUR = "#ff6600"      # the stock bus mode's route colour (missionTypes/busMode/constructor.lua)
ATTRIBUTION = "Bus lines: © OpenStreetMap contributors (Overpass)"

_cache = {}


def overpass_url(lat, lon):
    """The request for the lines with a stop within RADIUS_M of lat, lon: the relations (their members, in order), the
    stop and platform nodes, and the platforms mapped as areas (their centres). Only numbers enter Overpass QL."""
    lat, lon = float(lat), float(lon)
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Bus lookup coordinates are outside the world")
    lat, lon = round(lat / SNAP_DEG) * SNAP_DEG, round(lon / SNAP_DEG) * SNAP_DEG
    lon = (lon + 180) % 360 - 180
    # Cover the whole snapped cell (including a player at its corner). `around` handles the poles and date line
    # without invalid bounding boxes; parse still filters at the player's actual position.
    padding = math.ceil(111_320 * SNAP_DEG / math.sqrt(2))
    area = f"around:{RADIUS_M + padding:.0f},{lat:.5f},{lon:.5f}"
    # Select nearby stops first, then their parent bus routes. A relation bbox query also walks the routes' road
    # members (including through routes with no nearby stop); selecting stops avoids that unnecessary work.
    route_type = '[type=route]["route"~"^(bus|trolleybus)$"]'
    query = (f'[out:json][timeout:25][maxsize:16777216];('
             f'node({area})[highway=bus_stop];'
             f'node({area})[public_transport=stop_position];'
             f'nwr({area})[public_transport=platform];'
             f'nw({area})[highway=platform];)->.near;'
             f'(rel(bn.near){route_type};rel(bw.near){route_type};rel(br.near){route_type};)->.r;'
             '.r out body;rel(br.r)[type=route_master]["route_master"~"^(bus|trolleybus)$"];out body;node(r.r);out body;'
             '(way(r.r)[public_transport=platform];way(r.r)[highway=platform];'
             'rel(r.r)[public_transport=platform];);out tags center;')
    return OVERPASS_URL + "?" + urllib.parse.urlencode({"data": query})


def colour(value):
    """#rrggbb of an OpenStreetMap colour tag (#rgb, #rrggbb or a CSS name the stock display can show), else None."""
    v = str(value or "").strip().lower()
    names = {"red": "#ff0000", "green": "#008000", "blue": "#0000ff", "yellow": "#ffff00", "orange": "#ffa500",
             "purple": "#800080", "black": "#000000", "white": "#ffffff", "grey": "#808080", "gray": "#808080",
             "brown": "#a52a2a", "pink": "#ffc0cb", "cyan": "#00ffff", "magenta": "#ff00ff", "lime": "#00ff00",
             "navy": "#000080", "maroon": "#800000", "teal": "#008080", "olive": "#808000"}
    if v in names:
        return names[v]
    h = v[1:] if v.startswith("#") else v
    if len(h) in (3, 6) and all(c in "0123456789abcdef" for c in h):
        return "#" + (h if len(h) == 6 else "".join(c * 2 for c in h))
    return None


def _code(name, tags):
    """Whether a name is only a platform's letter or number (its ref / local_ref, or a short code like A, B2, 12)."""
    return bool(name) and (name in (tags.get("ref"), tags.get("local_ref")) or bool(_CODE.fullmatch(name)))


def _named_near(lat, lon, nodes):
    """The name of the nearest stop node in nodes within JOIN_M of lat, lon that is a stop's name, else ""."""
    best, name = JOIN_M, ""
    for n in nodes.values():
        t = n.get("tags") or {}
        nm = str(t.get("name") or "")
        if not nm or _code(nm, t) or n.get("lat") is None or not (
                t.get("public_transport") == "stop_position" or t.get("highway") == "bus_stop"):
            continue
        d = _metres(lat, lon, float(n["lat"]), float(n["lon"]))
        if d < best:
            best, name = d, nm
    return name


def _stop_members(rel, nodes, ways, relations):
    """Yield (direction, sequence, kind, element) for known passenger stops in relation order."""
    by_type = {"node": nodes, "way": ways, "relation": relations}
    for member in rel.get("members", ()):
        kind_of_element = member.get("type")
        el = by_type.get(kind_of_element, {}).get(member.get("ref"))
        if not el:
            continue
        role = str(member.get("role") or "")
        match = _STOP_ROLE.fullmatch(role)
        if match:
            direction, kind, number = match.groups()
            yield direction, int(number) if number else None, kind, el
        elif not role or (kind_of_element == "node" and role in ("forward", "backward")):
            tags = el.get("tags") or {}
            if tags.get("public_transport") == "stop_position":
                kind = "stop"
            elif tags.get("public_transport") == "platform" or tags.get("highway") in ("bus_stop", "platform"):
                kind = "platform"
            else:
                continue
            yield role or None, None, kind, el


def _ordered_stops(members, nodes):
    """Pair each road stop position with its nearby platform (after it, or just before it) and keep distinct visits
    in order."""
    if members and all(number is not None for _, number, _, _ in members):
        members = sorted(members, key=lambda m: (m[1], m[2] == "platform"))
    out, cur, current_id, previous_id = [], None, None, None

    def close():
        nonlocal previous_id
        if cur and cur.get("lat") is not None:
            if current_id != previous_id:
                out.append(cur)
                previous_id = current_id

    for _, _, kind, el in members:
        tags = el.get("tags") or {}
        name = str(tags.get("name") or "")
        code = kind == "platform" and _code(name, tags)
        lat, lon = (el.get("lat"), el.get("lon")) if "lat" in el else ((el.get("center") or {}).get("lat"),
                                                                        (el.get("center") or {}).get("lon"))
        if lat is None or lon is None:
            continue
        if kind == "stop" and cur is not None and cur.get("platform") and not cur.get("on_road") and (
                _metres(cur["lat"], cur["lon"], float(lat), float(lon)) <= JOIN_M):
            # Members mapped platform, then stop (Barcelona's TMB, Athens, Seattle, Leeds, 2026-11-19: every stop came
            # twice 13 m apart without this): one stop, at the stop_position on the road the bus drives.
            cur.update(lat=float(lat), lon=float(lon), on_road=True, name=cur["name"] or name)
            continue
        if kind == "stop":
            close()
            current_id = (el["type"], el["id"])
            # Some PTv1 and PTv2 relations use role "stop" for a roadside bus_stop node.
            on_road = tags.get("public_transport") == "stop_position" or (
                el["type"] == "node" and tags.get("highway") != "bus_stop"
                and tags.get("public_transport") != "platform")
            cur = {"name": name, "lat": float(lat), "lon": float(lon), "on_road": on_road}
        elif cur is not None and not cur.get("platform") and (
                _metres(cur["lat"], cur["lon"], float(lat), float(lon)) <= JOIN_M):
            cur["platform"] = True
            cur["name"] = cur["name"] or ("" if code else name)
        else:
            close()
            current_id = (el["type"], el["id"])
            cur = {"name": "" if code else name, "lat": float(lat), "lon": float(lon), "on_road": False,
                   "platform": True}
        if not cur["name"]:
            cur["name"] = _named_near(cur["lat"], cur["lon"], nodes) or name
    close()
    for s in out:
        s.pop("platform", None)
    return out


def stops(rel, nodes, ways, relations=None):
    """Read the stops of a single-direction relation."""
    members = [m for m in _stop_members(rel, nodes, ways, relations or {}) if m[0] is None]
    return _ordered_stops(members, nodes)


def _variants(rel, nodes, ways, relations, tags):
    members = list(_stop_members(rel, nodes, ways, relations))
    directions = {direction for direction, _, _, _ in members if direction}
    if not directions:
        yield rel["id"], tags.get("from"), tags.get("to"), _ordered_stops(members, nodes)
        return
    for direction in ("forward", "backward"):
        if direction not in directions:
            continue
        selected = [m for m in members if m[0] in (direction, None)]
        ordered = _ordered_stops(selected, nodes)
        start, end = tags.get("from"), tags.get("to")
        if direction == "backward":
            start, end = end, start
        # Some older relations list shared stops in outbound order for both directions.
        # Reverse the whole result only when the named termini support it.
        if len(ordered) > 1 and any(m[1] is None for m in selected):
            first, last = ordered[0]["name"], ordered[-1]["name"]
            if (first == end) + (last == start) > (first == start) + (last == end):
                ordered.reverse()
        yield f'{rel["id"]}-{direction}', start, end, ordered


def _route_masters(relations):
    """Index direct bus route parents without following unrelated siblings or road geometry."""
    parents = {}
    for rel in relations.values():
        tags = rel.get("tags") or {}
        if tags.get("type") == "route_master" and tags.get("route_master") in _ROUTE_MODES:
            for member in rel.get("members", ()):
                if member.get("type") == "relation":
                    parents.setdefault(member["ref"], []).append(tags)
    return parents


def _metres(lat0, lon0, lat1, lon1):
    k = 111_320.0
    dlon = (lon1 - lon0 + 180) % 360 - 180
    return math.hypot((lat1 - lat0) * k, dlon * k * dmath.cos(math.radians((lat0 + lat1) / 2)))


def parse(data, lat, lon):
    """The lines of an Overpass answer: [{"id", "ref", "name", "from", "to", "colour", "operator", "stops", "near_m",
    "near_i"}], ordered by reference and distance (near_i: the closest stop's index), at most MAX_LINES. A line
    with fewer than two stops is left out, and the same line (ref, stops) mapped twice is once."""
    els = data.get("elements", ())
    nodes = {e["id"]: e for e in els if e.get("type") == "node"}
    ways = {e["id"]: e for e in els if e.get("type") == "way"}
    relations = {e["id"]: e for e in els if e.get("type") == "relation"}
    masters = _route_masters(relations)
    lines, seen = [], set()
    for rel in relations.values():
        tags = rel.get("tags") or {}
        mode = tags.get("route")
        if tags.get("type") != "route" or mode not in _ROUTE_MODES:
            continue
        tags = dict(tags)
        for field in _MASTER_TAGS:
            if not tags.get(field):
                values = {p[field] for p in masters.get(rel["id"], ()) if p["route_master"] == mode and p.get(field)}
                if len(values) == 1:
                    tags[field] = values.pop()
        for ident, depart, arrive, st in _variants(rel, nodes, ways, relations, tags):
            if len(st) < 2:
                continue
            d = [_metres(lat, lon, s["lat"], s["lon"]) for s in st]
            i = min(range(len(d)), key=d.__getitem__)
            if d[i] > RADIUS_M:
                continue
            key = (mode, tags.get("ref"), tags.get("network"), tags.get("operator"),
                   tuple((s["name"], s["lat"], s["lon"]) for s in st))
            if key in seen:
                continue
            seen.add(key)
            # Choose the window after locating the player, so a stop near the end of a long route is still discoverable.
            offset = min(i, len(st) - MAX_STOPS) if i >= MAX_STOPS else 0
            lines.append({"id": ident, "ref": str(tags.get("ref") or ""), "name": str(tags.get("name") or ""),
                          "from": str(depart or st[0]["name"]), "to": str(arrive or st[-1]["name"]),
                          "colour": colour(tags.get("colour")) or DEFAULT_COLOUR,
                          "operator": str(tags.get("operator") or ""), "mode": mode,
                          "stops": st[offset:offset + MAX_STOPS],
                          "near_m": round(d[i], 1), "near_i": i - offset})
    lines.sort(key=lambda l: (_ref_key(l["ref"]), l["near_m"]))
    return lines[:MAX_LINES]


def _ref_key(ref):
    """Line numbers in their natural order: 16 before 158, X1 after the numbers."""
    # isdigit() also accepts superscript characters (e.g. ²), which int() cannot parse.
    digits = "".join(c for c in ref if c.isdecimal())
    return (not ref[:1].isdigit(), int(digits) if digits else 0, ref)


class Busy(ValueError):
    """Every Overpass server failed (overloaded, down, its breaker open) and no lookup saved nearby: the game's message."""


BUSY_TEXT = ("OpenStreetMap's bus-line servers (Overpass) are overloaded or down right now: try again in a few minutes. "
             "Lines looked up here before still show while they are.")


def error_text(error):
    """Keep the failure reason visible: the encoded query URL otherwise fills the whole game message."""
    if isinstance(error, Busy):
        return str(error)
    text = str(error)
    url = getattr(error, "url", None)
    if url:
        text = text.replace(url, urllib.parse.urlsplit(url).netloc)
    return f"{type(error).__name__}: {text}"


def _cache_path(cache_dir, url):
    return Path(cache_dir) / "buslines" / (hashlib.sha256(url.encode()).hexdigest() + ".json")


def _read_cache(path):
    try:
        if not 0 <= time.time() - path.stat().st_mtime <= CACHE_TTL_S:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("elements"), list) and not data.get("remark"):
            return data
    except (OSError, ValueError):
        pass
    return None


def _write_cache(path, data):
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".buslines-", delete=False) as stream:
            temporary = stream.name
            json.dump(data, stream, ensure_ascii=False)
        os.replace(temporary, path)
    except OSError as error:
        log.warning("bus lines cache: %s", error)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def near(lat, lon, net, cache_dir=None):
    """The lines near lat, lon: in Västtrafik's area from its own timetable data (gtfsbus: no Overpass; while its first
    download runs, or when it cannot be had, Overpass as below). Elsewhere one bounded, cached Overpass lookup per
    snapped place, one alternate endpoint on connection/server failure. Offline mode, budgets, throttling and other
    refusals are respected without contacting another service."""
    if cache_dir is not None and gtfsbus.covers(float(lat), float(lon)):
        try:
            lines = gtfsbus.near(float(lat), float(lon), gtfsbus.index(cache_dir, net), RADIUS_M, MAX_LINES, _ref_key,
                                 DEFAULT_COLOUR)
            if lines:
                return lines
        except gtfsbus.Preparing as e:
            note = str(e)
        except RuntimeError as e:
            note = "Västtrafik's lines: " + str(e)
        else:
            # The regional bounding rectangle also contains places with only another operator's service.
            return _overpass(lat, lon, net, cache_dir)
        try:
            return _overpass(lat, lon, net, cache_dir)
        except Busy as e:
            raise Busy(note + ". " + str(e)) from None
    return _overpass(lat, lon, net, cache_dir)


def attribution(lines):
    """The window's attribution line for these lines (near's)."""
    return gtfsbus.ATTRIBUTION if lines and lines[0].get("source") == "vt" else ATTRIBUTION


def _overpass(lat, lon, net, cache_dir=None):
    url = overpass_url(lat, lon)
    data = _cache.get(url)
    path = _cache_path(cache_dir, url) if cache_dir is not None else None
    if data is None and path is not None:
        data = _read_cache(path)
        if data is not None:
            _cache[url] = data
    if data is None:
        failures = []
        for endpoint in (OVERPASS_URL, OVERPASS_FALLBACK_URL):
            request_url = endpoint + "?" + urllib.parse.urlsplit(url).query
            try:
                data = json.loads(net.get(request_url, kind="demand").body)
            except netfetch.Offline:
                raise
            except (netfetch.HostUnavailable, netfetch.FetchFailed) as error:
                # A 4xx refusal or malformed query is not fixed by asking another server.
                if getattr(error, "status", None) is not None and error.status < 500:
                    raise
                log.warning("bus lines endpoint: %s", error_text(error))
                failures.append(error_text(error))
                continue
            if isinstance(data, dict) and data.get("remark"):
                reason = (urllib.parse.urlsplit(endpoint).netloc + ": Overpass did not complete: "
                          + str(data["remark"])[:120])
                log.warning("bus lines endpoint: %s", reason)
                failures.append(reason)
                continue
            age = _data_age(data)
            if age is not None and age > STALE_S:
                reason = (urllib.parse.urlsplit(endpoint).netloc + f": Overpass data {age / 86400:.0f} days old")
                log.warning("bus lines endpoint: %s", reason)
                failures.append(reason)
                continue
            # Validate before storing: malformed replies must not survive a server restart.
            if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
                raise ValueError("Overpass returned no elements list")
            parse(data, float(lat), float(lon))
            _cache[url] = data
            if path is not None:
                _write_cache(path, data)
            break
        else:
            saved = _nearby(float(lat), float(lon), cache_dir)
            if saved:
                log.warning("bus lines: Overpass failed, %d lines from a saved lookup nearby", len(saved))
                return saved
            raise Busy(BUSY_TEXT + " (" + "; ".join(failures) + ")")
    return parse(data, float(lat), float(lon))


def _data_age(data):
    """s since the OpenStreetMap data an Overpass answer was made from (its osm3s.timestamp_osm_base), None when it
    does not say."""
    try:
        stamp = time.strptime(str(data["osm3s"]["timestamp_osm_base"]), "%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, TypeError, ValueError):
        return None
    return time.time() - calendar.timegm(stamp)


def _nearby(lat, lon, cache_dir=None):
    """The lines with a stop within RADIUS_M of lat, lon from the saved lookups (this session's and the disk's, within
    CACHE_TTL_S) that have the most of them, each marked "saved": for when every Overpass server fails (all of them
    overloaded on 2026-11-17 for an hour). [] when none has such a line."""
    found = list(_cache.values())
    folder = Path(cache_dir) / "buslines" if cache_dir is not None else None
    if folder is not None and folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            data = _read_cache(path)
            if data is not None:
                found.append(data)
    best = []
    for data in found:
        try:
            lines = [l for l in parse(data, lat, lon) if l["near_m"] <= RADIUS_M]
        except (KeyError, TypeError, ValueError):
            continue
        if len(lines) > len(best):
            best = lines
    return [dict(l, saved=True) for l in best]
