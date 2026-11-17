"""The bus lines near the player, as OpenStreetMap has them: for driving a real line in the game (GitHub #1363 "Bus
routes missions in city"; the user, 2026-11-17: "yes start the bus lines").

A line is a route=bus relation (https://wiki.openstreetmap.org/wiki/Tag:route%3Dbus): its number (ref), colour, where it
goes (from, to) and its stops in order, the members with a stop or platform role (Public Transport v2: each stop is a
stop_position on the road, usually followed by the platform beside it). One bounded Overpass lookup per place
(one alternate endpoint on an outage), on demand only (the game's Bus lines button). Successful replies are kept
for the session and for 30 days on disk: no geometry is downloaded, only the stops.

Measured 2026-11-17, a 1.3 x 1.8 km box in central Göteborg: 85 lines, all Public Transport v2, every stop named.
"""
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
JOIN_M = 60.0                   # m: a platform this near the stop_position before it is that stop's platform
# A platform's own letter or number (Västtrafik's läge: name=A, ref=A beside the stop "Fässbergsskolan", measured
# 2026-11-17 near Mölndal: 250 of the 8 lines' stops were named only that), not a stop's name
_CODE = re.compile(r"[A-Za-z]{1,2}\d{0,2}|\d{1,2}")
DEFAULT_COLOUR = "#ff6600"      # the stock bus mode's route colour (missionTypes/busMode/constructor.lua)
ATTRIBUTION = "Bus lines: © OpenStreetMap contributors (Overpass)"

_cache = {}


def overpass_url(lat, lon):
    """The request for the lines with a stop within RADIUS_M of lat, lon: the relations (their members, in order), the
    stop and platform nodes, and the platforms mapped as areas (their centres). Only numbers enter Overpass QL."""
    lat, lon = round(float(lat) / SNAP_DEG) * SNAP_DEG, round(float(lon) / SNAP_DEG) * SNAP_DEG
    dlat = RADIUS_M / 111_320.0
    dlon = dlat / max(math.cos(math.radians(lat)), 0.01)
    box = f"{lat - dlat:.5f},{lon - dlon:.5f},{lat + dlat:.5f},{lon + dlon:.5f}"
    # Select nearby stops first, then their parent bus routes. A relation bbox query also walks the routes' road
    # members (including through routes with no nearby stop); selecting stops avoids that unnecessary work.
    query = (f'[out:json][timeout:25][maxsize:16777216];('
             f'node({box})[highway=bus_stop];'
             f'node({box})["public_transport"~"^(platform|stop_position)$"];'
             f'way({box})[public_transport=platform];)->.near;'
             '(rel(bn.near)[type=route][route=bus];rel(bw.near)[type=route][route=bus];)->.r;'
             '.r out body;node(r.r);out body;way(r.r)[public_transport=platform];out tags center;')
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


def _stop_role(role):
    """"stop" / "platform" for a member role that is one (stop_entry_only, platform_exit_only ... too), else None."""
    for kind in ("stop", "platform"):
        if role == kind or role.startswith(kind + "_"):
            return kind
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


def stops(rel, nodes, ways):
    """[{"name", "lat", "lon", "on_road"}] of a relation's stops in order: a stop_position and the platform after it are
    one stop (at the stop_position: it is on the road the bus drives): the platform has its name, none, only its
    letter, or stands within JOIN_M. A lone platform is a stop at the platform, named after the stop beside it when its
    own name is only a letter. A member Overpass did not return (outside what it sent) is skipped; the same stop twice
    in a row is once."""
    out, cur = [], None

    def close():
        if cur and cur.get("lat") is not None:
            if not (out and out[-1]["name"] == cur["name"] and cur["name"]):
                out.append(cur)

    for m in rel.get("members", ()):
        kind = _stop_role(str(m.get("role", "")))
        if kind is None:
            continue
        el = nodes.get(m.get("ref")) if m.get("type") == "node" else ways.get(m.get("ref")) if m.get("type") == "way" \
            else None
        if el is None:
            continue
        tags = el.get("tags") or {}
        name = str(tags.get("name") or "")
        code = _code(name, tags)
        lat, lon = (el.get("lat"), el.get("lon")) if "lat" in el else ((el.get("center") or {}).get("lat"),
                                                                        (el.get("center") or {}).get("lon"))
        if lat is None or lon is None:
            continue
        if kind == "stop":
            close()
            cur = {"name": "" if code else name, "lat": float(lat), "lon": float(lon), "on_road": True}
        elif cur is not None and not cur.get("platform") and (
                not cur["name"] or not name or code or name == cur["name"]
                or _metres(cur["lat"], cur["lon"], float(lat), float(lon)) <= JOIN_M):
            cur["platform"] = True
            cur["name"] = cur["name"] or ("" if code else name)
        else:
            close()
            cur = {"name": "" if code else name, "lat": float(lat), "lon": float(lon), "on_road": False,
                   "platform": True}
        if not cur["name"]:
            cur["name"] = _named_near(cur["lat"], cur["lon"], nodes) or name
    close()
    for s in out:
        s.pop("platform", None)
    return out[:MAX_STOPS]


def _metres(lat0, lon0, lat1, lon1):
    k = 111_320.0
    return math.hypot((lat1 - lat0) * k, (lon1 - lon0) * k * math.cos(math.radians(lat0)))


def parse(data, lat, lon):
    """The lines of an Overpass answer: [{"id", "ref", "name", "from", "to", "colour", "operator", "stops", "near_m",
    "near_i"}], the line with a stop nearest lat, lon first (near_i: that stop's index), at most MAX_LINES. A line
    with fewer than two stops is left out, and the same line (ref, stops) mapped twice is once."""
    els = data.get("elements", ())
    nodes = {e["id"]: e for e in els if e.get("type") == "node"}
    ways = {e["id"]: e for e in els if e.get("type") == "way"}
    lines, seen = [], set()
    for rel in (e for e in els if e.get("type") == "relation"):
        tags = rel.get("tags") or {}
        if tags.get("route") != "bus":
            continue
        st = stops(rel, nodes, ways)
        if len(st) < 2:
            continue
        key = (tags.get("ref"), tuple(s["name"] for s in st))
        if key in seen:
            continue
        seen.add(key)
        d = [_metres(lat, lon, s["lat"], s["lon"]) for s in st]
        i = min(range(len(d)), key=d.__getitem__)
        lines.append({"id": rel["id"], "ref": str(tags.get("ref") or ""), "name": str(tags.get("name") or ""),
                      "from": str(tags.get("from") or st[0]["name"]), "to": str(tags.get("to") or st[-1]["name"]),
                      "colour": colour(tags.get("colour")) or DEFAULT_COLOUR, "operator": str(tags.get("operator") or ""),
                      "stops": st, "near_m": round(d[i], 1), "near_i": i})
    lines.sort(key=lambda l: (l["near_m"] > RADIUS_M, _ref_key(l["ref"]), l["near_m"]))
    return lines[:MAX_LINES]


def _ref_key(ref):
    """Line numbers in their natural order: 16 before 158, X1 after the numbers."""
    digits = "".join(c for c in ref if c.isdigit())
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
            return gtfsbus.near(float(lat), float(lon), gtfsbus.index(cache_dir, net), RADIUS_M, MAX_LINES, _ref_key,
                                DEFAULT_COLOUR)
        except gtfsbus.Preparing as e:
            note = str(e)
        except RuntimeError as e:
            note = "Västtrafik's lines: " + str(e)
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
                if data.get("remark"):
                    raise ValueError("Overpass did not complete: " + str(data["remark"])[:120])
            except netfetch.Offline:
                raise
            except (netfetch.HostUnavailable, netfetch.FetchFailed) as error:
                # A 4xx refusal or malformed query is not fixed by asking another server.
                if getattr(error, "status", None) is not None and error.status < 500:
                    raise
                log.warning("bus lines endpoint: %s", error_text(error))
                failures.append(error_text(error))
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
