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


def url(lat0, lon0, lat1, lon1, bearing=None, steps=False, alternatives=None):
    """bearing: the car's heading (compass degrees): the route starts on a road the car can follow that way.
    steps: also the turn-by-turn steps (the phone's instruction card). alternatives: how many routes to ask for
    (the phone's route choice)."""
    u = OSRM_URL.format(lat0=float(lat0), lon0=float(lon0), lat1=float(lat1), lon1=float(lon1))
    if steps and "steps=true" not in u:     # (OSRM_URL asks for them already: the GPS's turn-by-turn)
        u += "&steps=true"
    if alternatives:
        u += f"&alternatives={int(alternatives)}"
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


def parse_steps(route):
    """[{at, dist, dur, name, type, modifier, exit?}] of an OSRM route's legs: at = metres from the route's start
    where the maneuver is, type / modifier as OSRM names them ("turn" / "left", "depart", "arrive", ...), exit =
    the roundabout's exit number. Pure."""
    out, at = [], 0.0
    for leg in route.get("legs") or []:
        for s in leg.get("steps") or []:
            man = s.get("maneuver") or {}
            dist = float(s.get("distance") or 0.0)
            step = {"at": round(at, 1), "dist": round(dist, 1), "dur": round(float(s.get("duration") or 0.0), 1),
                    "name": str(s.get("name") or ""), "type": str(man.get("type") or ""),
                    "modifier": str(man.get("modifier") or "")}
            if man.get("exit") is not None:
                step["exit"] = int(man["exit"])
            out.append(step)
            at += dist
    return out


def resample(points, max_points=120, min_step_m=50.0):
    """points [(lat, lon)] (the route, however sparse its vertices are) at even distance steps, for an elevation
    profile: [(lat, lon, dist_m)], the first at 0 and the last at the route's whole length, at most max_points,
    step >= min_step_m. simplify() only keeps vertices at bends, so a long straight climb would else be two points
    and no profile at all (dem.sample wants points spread along it instead). Pure."""
    n = len(points)
    if n == 0:
        return []
    if n == 1:
        return [(points[0][0], points[0][1], 0.0)]
    lat_ref = points[0][0]
    xy = [_xy(la, lo, lat_ref) for la, lo in points]
    seg = [math.hypot(xy[i + 1][0] - xy[i][0], xy[i + 1][1] - xy[i][1]) for i in range(n - 1)]
    total = sum(seg)
    if total <= 0:
        return [(points[0][0], points[0][1], 0.0)]
    step = max(min_step_m, total / max(1, max_points - 1))
    out = []
    i, done, s = 0, 0.0, 0.0
    while s <= total:
        while i < n - 2 and done + seg[i] < s:
            done += seg[i]
            i += 1
        f = max(0.0, min(1.0, (s - done) / seg[i])) if seg[i] > 0 else 0.0
        la = points[i][0] + f * (points[i + 1][0] - points[i][0])
        lo = points[i][1] + f * (points[i + 1][1] - points[i][1])
        out.append((la, lo, s))
        s += step
    if out[-1][2] < total - 1e-6:
        out.append((points[-1][0], points[-1][1], total))
    return out


def _one(r):
    """{points, dist, dur, steps} of one OSRM route (geometry simplified). Pure."""
    coords = r["geometry"]["coordinates"]
    pts = simplify([(float(c[1]), float(c[0])) for c in coords])
    tol = SIMPLIFY_M
    while len(pts) > MAX_POINTS:            # very long routes: coarser
        tol *= 2
        pts = simplify(pts, tol)
    out = {"points": [[round(a, 6), round(b, 6)] for a, b in pts], "dist": float(r.get("distance") or 0),
           "dur": float(r.get("duration") or 0), "turns": turns(r)}
    steps = parse_steps(r)                  # (the phone's Maps card; the GPS minimap uses "turns")
    if steps:
        out["steps"] = steps
    return out


def parse(data, alternatives=None):
    """{points: [[lat, lon], ...], dist (m), dur (s), steps?, routes?} of an OSRM route response, or raises
    ValueError. With alternatives: "routes" carries every route OSRM offered, each {points, dist, dur, steps, tag},
    the first being the one the top-level fields describe. Pure."""
    if not isinstance(data, dict) or data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError(f"no route ({(data or {}).get('code') if isinstance(data, dict) else 'bad answer'})")
    out = _one(data["routes"][0])
    if alternatives and len(data["routes"]) > 1:
        tags = tag_alternatives(data["routes"])
        out["routes"] = [dict(_one(r), tag=t["tag"], main_share=t["main_share"])
                         for r, t in zip(data["routes"], tags)]
    return out


def main_share(route):
    """The share of a route's distance on numbered roads (motorways and the like carry a ref), 0..1. The phone ranks
    its route choices with it: OSRM's fastest route happily takes lanes that make no sense to drive. Pure."""
    total = float(route.get("distance") or 0.0)
    if total <= 0:
        return 0.0
    on_ref = 0.0
    for leg in route.get("legs") or []:
        for s in leg.get("steps") or []:
            if str(s.get("ref") or "").strip():
                on_ref += float(s.get("distance") or 0.0)
    return min(1.0, on_ref / total)


def tag_alternatives(routes):
    """[{tag, main_share}] for the routes OSRM answered with, in their order: the quickest, the one that stays most
    on numbered roads and the shortest get a name; the others are "Route n". Pure."""
    if not routes:
        return []
    dur = [float(r.get("duration") or 0.0) for r in routes]
    dist = [float(r.get("distance") or 0.0) for r in routes]
    share = [main_share(r) for r in routes]
    best = {"Fastest": dur.index(min(dur)), "Main roads": share.index(max(share)), "Shortest": dist.index(min(dist))}
    out = [{"tag": None, "main_share": round(share[i], 3)} for i in range(len(routes))]
    for name, i in best.items():
        if out[i]["tag"] is None:
            out[i]["tag"] = name
    for i, r in enumerate(out):
        if r["tag"] is None:
            r["tag"] = f"Route {i + 1}"
    return out


def plan(lat0, lon0, lat1, lon1, net=None, bearing=None, steps=False, alternatives=None):
    """The road route from (lat0, lon0) to (lat1, lon1) (blocking), starting the way bearing points if it can (else
    without it). With alternatives: also the other routes OSRM offers, as "routes" (each {points, dist, dur, steps,
    tag}). Raises netfetch.FetchError or ValueError."""
    net = net if net is not None else netfetch.default_fetcher()
    if bearing is not None:
        try:
            got = parse(json.loads(net.get(url(lat0, lon0, lat1, lon1, bearing, steps, alternatives),
                                            kind="demand").body.decode("utf-8")), alternatives)
        except (ValueError, netfetch.FetchFailed) as e:   # no road that way near the start (OSRM: 400 NoSegment,
            if isinstance(e, netfetch.FetchFailed) and e.status != 400:      # a car across a field): any direction
                raise
            got = None
    else:
        got = None
    if got is None:
        got = parse(json.loads(net.get(url(lat0, lon0, lat1, lon1, steps=steps, alternatives=alternatives),
                                       kind="demand").body.decode("utf-8")), alternatives)
    return got
