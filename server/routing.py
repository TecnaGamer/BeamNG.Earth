"""Whole-route planning for the world map's GPS (the user, 2026-10-14: "have the server plan out the whole route so
it can work with much longer distances").

The game's own road graph (navgraph.py) only covers ~1 km around the player, so the route itself comes from OSRM
on OpenStreetMap data, the FOSSGIS-hosted instance (https://routing.openstreetmap.de; fair use: a valid
User-Agent, at most ~1 request a second, no bulk use; netfetch's routing.openstreetmap.de policy). One request per
destination (and per re-route when the player leaves the route); the geometry is simplified and sent to the mod,
which draws it on the map and steers the stock route arrows along it on its local graph (gps.lua).
"""
import json
import math

import netfetch

OSRM_URL = "https://routing.openstreetmap.de/routed-car/route/v1/driving/{lon0:.6f},{lat0:.6f};{lon1:.6f},{lat1:.6f}" \
           "?overview=full&geometries=geojson&steps=true"
SIMPLIFY_M = 4.0
MAX_POINTS = 6000
ATTRIBUTION = "Routing: OSRM (FOSSGIS), data (c) OpenStreetMap contributors (ODbL)"


BEARING_RANGE = 60          # degrees either side of the car's heading the start may take


def url(lat0, lon0, lat1, lon1, bearing=None):
    """bearing: the car's heading (compass degrees): the route starts on a road the car can follow that way."""
    u = OSRM_URL.format(lat0=float(lat0), lon0=float(lon0), lat1=float(lat1), lon1=float(lon1))
    if bearing is not None:
        u += f"&bearings={int(bearing) % 360},{BEARING_RANGE};"
    return u


def _xy(lat, lon, lat_ref):
    m = 111320.0
    return lon * m * math.cos(math.radians(lat_ref)), lat * m


def simplify(points, tol_m=SIMPLIFY_M):
    """points [(lat, lon)] with the vertices closer than tol_m to their neighbours' line dropped (Ramer-Douglas-
    Peucker, iterative). Pure."""
    n = len(points)
    if n < 3:
        return list(points)
    lat_ref = points[0][0]
    xy = [_xy(la, lo, lat_ref) for la, lo in points]
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = xy[a]
        bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            if L2 > 0:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
                qx, qy = ax + t * dx, ay + t * dy
            else:
                qx, qy = ax, ay
            d = (px - qx) ** 2 + (py - qy) ** 2
            if d > best:
                best, bi = d, i
        if bi > 0 and best > tol_m * tol_m:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(points, keep) if k]


# The GPS's turn-by-turn (the user, 2026-10-25: "arrow indicators for turns ... distance to next turn"): OSRM's steps,
# only those where the driver does something (a "new name" or a "continue" straight on is no turn), each with the
# text and arrow the GPS app shows. Positions are OSRM's maneuver points, on the route's line.
TURN_TYPES = {"turn", "end of road", "fork", "merge", "on ramp", "off ramp", "roundabout", "rotary",
              "roundabout turn", "continue", "arrive"}
MAX_TURNS = 3000
ORDINAL = {1: "1st", 2: "2nd", 3: "3rd"}


def _arrow(kind, mod):
    """The GPS app's arrow for a maneuver: straight, slight-left, left, sharp-left, uturn (and the right ones),
    roundabout or arrive."""
    if kind == "arrive":
        return "arrive"
    if kind in ("roundabout", "rotary", "roundabout turn"):
        return "roundabout"
    mod = (mod or "straight").replace(" ", "-")
    return mod if mod in ("straight", "slight-left", "left", "sharp-left", "uturn", "slight-right", "right",
                          "sharp-right") else "straight"


def _road(step):
    """The road a step leads onto as a sign would name it: "Penn Ave", "I-76", "Penn Ave (US 30)"."""
    name, ref = (step.get("name") or "").strip(), (step.get("ref") or "").strip().split(";")[0].strip()
    if name and ref and ref not in name:
        return f"{name} ({ref})"
    return name or ref


def turn_text(step):
    """What the GPS card says for an OSRM step (a dict with maneuver, name, ref, destinations, exits)."""
    m = step.get("maneuver") or {}
    kind, mod = m.get("type", ""), (m.get("modifier") or "").strip()
    road = _road(step)
    onto = f" onto {road}" if road else ""
    toward = (step.get("destinations") or "").split(":")[-1].split(",")[0].strip()
    side = "left" if "left" in mod else "right" if "right" in mod else ""
    if kind == "arrive":
        return "Arrive at your destination"
    if kind in ("roundabout", "rotary", "roundabout turn"):
        n = m.get("exit")
        if isinstance(n, int) and n > 0:
            return f"At the roundabout, take the {ORDINAL.get(n, f'{n}th')} exit{onto}"
        return f"At the roundabout, continue{onto}"
    if kind == "off ramp":
        ex = (step.get("exits") or "").split(";")[0].strip()
        what = f"Take exit {ex}" if ex else (f"Take the exit on the {side}" if side else "Take the exit")
        return what + (f" toward {toward}" if toward else onto)
    if kind == "on ramp":
        return f"Take the ramp{(' toward ' + toward) if toward else onto}"
    if kind == "merge":
        return f"Merge{onto or (' ' + side if side else '')}"
    if kind == "fork":
        return f"Keep {side or 'straight'}" + (f" toward {toward}" if toward else onto)
    if mod == "uturn":
        return "Make a U-turn" + onto
    if mod in ("straight", ""):
        return "Continue straight" + onto
    word = {"slight": "Bear", "sharp": "Turn sharp"}.get(mod.split()[0], "Turn")
    return f"{word} {side}{onto}"


def turns(route):
    """The turn-by-turn of an OSRM route: [{"lat", "lon", "at", "arrow", "text"}...] in order, the arrival last. at: m
    along OSRM's route to the maneuver (its steps' distances before it): the mod finds the turn on its simplified line
    near there (gps.lua), as a route may pass one spot twice. Pure."""
    out = []
    at = 0.0
    for leg in route.get("legs") or []:
        for step in leg.get("steps") or []:
            here = at
            at += float(step.get("distance") or 0.0)
            m = step.get("maneuver") or {}
            kind, mod = m.get("type", ""), (m.get("modifier") or "").strip()
            if kind not in TURN_TYPES:
                continue
            if kind == "continue" and mod in ("", "straight"):
                continue
            loc = m.get("location") or []
            if len(loc) < 2:
                continue
            out.append({"lat": round(float(loc[1]), 6), "lon": round(float(loc[0]), 6), "at": round(here, 1),
                        "arrow": _arrow(kind, mod), "text": turn_text(step)})
            if len(out) >= MAX_TURNS:
                return out
    return out


def parse(data):
    """{points: [[lat, lon], ...], dist (m), dur (s), turns} of an OSRM route response (turns: the turn-by-turn,
    turns()), or raises ValueError. Pure."""
    if not isinstance(data, dict) or data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError(f"no route ({(data or {}).get('code') if isinstance(data, dict) else 'bad answer'})")
    r = data["routes"][0]
    coords = r["geometry"]["coordinates"]
    pts = [(float(c[1]), float(c[0])) for c in coords]
    pts = simplify(pts)
    tol = SIMPLIFY_M
    while len(pts) > MAX_POINTS:            # very long routes: coarser
        tol *= 2
        pts = simplify(pts, tol)
    return {"points": [[round(a, 6), round(b, 6)] for a, b in pts], "dist": float(r.get("distance") or 0),
            "dur": float(r.get("duration") or 0), "turns": turns(r)}


def plan(lat0, lon0, lat1, lon1, net=None, bearing=None):
    """The road route from (lat0, lon0) to (lat1, lon1) (blocking), starting the way bearing points if it can (else
    without it). Raises netfetch.FetchError or ValueError."""
    net = net if net is not None else netfetch.default_fetcher()
    if bearing is not None:
        try:
            return parse(json.loads(net.get(url(lat0, lon0, lat1, lon1, bearing), kind="demand").body.decode("utf-8")))
        except (ValueError, netfetch.FetchFailed) as e:   # no road that way near the start (OSRM: 400 NoSegment,
            if isinstance(e, netfetch.FetchFailed) and e.status != 400:      # a car across a field): any direction
                raise
    return parse(json.loads(net.get(url(lat0, lon0, lat1, lon1), kind="demand").body.decode("utf-8")))
