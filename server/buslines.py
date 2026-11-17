"""The bus lines near the player, as OpenStreetMap has them: for driving a real line in the game (GitHub #1363 "Bus
routes missions in city"; the user, 2026-11-17: "yes start the bus lines").

A line is a route=bus relation (https://wiki.openstreetmap.org/wiki/Tag:route%3Dbus): its number (ref), colour, where it
goes (from, to) and its stops in order, the members with a stop or platform role (Public Transport v2: each stop is a
stop_position on the road, usually followed by the platform beside it). One bounded Overpass request per place, on
demand only (the game's Bus lines button), and kept for the session: no geometry is downloaded, only the stops.

Measured 2026-11-17, a 1.3 x 1.8 km box in central Göteborg: 85 lines, all Public Transport v2, every stop named.
"""
import json
import math
import urllib.parse

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
RADIUS_M = 700.0                # lines with a stop within this of the player
SNAP_DEG = 0.005                # the request's centre is rounded to this, so a few metres' drive asks nothing new
MAX_LINES = 40
MAX_STOPS = 120
DEFAULT_COLOUR = "#ff6600"      # the stock bus mode's route colour (missionTypes/busMode/constructor.lua)
ATTRIBUTION = "Bus lines: © OpenStreetMap contributors (Overpass)"

_cache = {}


def overpass_url(lat, lon):
    """The request for the lines with a member within RADIUS_M of lat, lon: the relations (their members, in order), the
    stop and platform nodes, and the platforms mapped as areas (their centres). Only numbers enter Overpass QL."""
    lat, lon = round(float(lat) / SNAP_DEG) * SNAP_DEG, round(float(lon) / SNAP_DEG) * SNAP_DEG
    dlat = RADIUS_M / 111_320.0
    dlon = dlat / max(math.cos(math.radians(lat)), 0.01)
    box = f"{lat - dlat:.5f},{lon - dlon:.5f},{lat + dlat:.5f},{lon + dlon:.5f}"
    query = (f"[out:json][timeout:60];rel({box})[type=route][route=bus]->.r;.r out body;node(r.r);out body;"
             "way(r.r)[public_transport=platform];out tags center;")
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


def stops(rel, nodes, ways):
    """[{"name", "lat", "lon", "on_road"}] of a relation's stops in order: a stop_position and the platform after it are
    one stop (at the stop_position: it is on the road the bus drives), a lone platform is a stop at the platform. A
    member Overpass did not return (outside what it sent) is skipped; the same stop twice in a row is once."""
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
        lat, lon = (el.get("lat"), el.get("lon")) if "lat" in el else ((el.get("center") or {}).get("lat"),
                                                                        (el.get("center") or {}).get("lon"))
        if lat is None or lon is None:
            continue
        if kind == "stop":
            close()
            cur = {"name": name, "lat": float(lat), "lon": float(lon), "on_road": True}
        elif cur is not None and not cur.get("platform") and (not cur["name"] or not name or name == cur["name"]):
            cur["platform"] = True
            cur["name"] = cur["name"] or name
        else:
            close()
            cur = {"name": name, "lat": float(lat), "lon": float(lon), "on_road": False, "platform": True}
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


def near(lat, lon, net):
    """The lines near lat, lon (parse), from one Overpass request per snapped place, kept for the session. net:
    netfetch's fetcher (its overpass-api.de policy: one request at a time, a daily budget). Raises on a failed request."""
    url = overpass_url(lat, lon)
    data = _cache.get(url)
    if data is None:
        data = json.loads(net.get(url, kind="demand").body)
        if data.get("remark") and not data.get("elements"):
            raise ValueError("Overpass did not complete: " + str(data["remark"])[:120])
        _cache[url] = data
    return parse(data, float(lat), float(lon))
