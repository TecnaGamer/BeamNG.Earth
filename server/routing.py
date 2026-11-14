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
import dmath
import dnp

import netfetch

OSRM_URL = "https://routing.openstreetmap.de/routed-car/route/v1/driving/{coords}" \
           "?overview=full&geometries=geojson&steps=true"
# Waypoints between the start and the destination (GitHub #935 / #886: a charging stop, a route with several stops):
# OSRM's own intermediate coordinates, at most MAX_VIA of them (one request for the whole trip, the FOSSGIS fair use).
MAX_VIA = 10
SIMPLIFY_M = 4.0
MAX_POINTS = 6000
ATTRIBUTION = "Routing: OSRM (FOSSGIS), data (c) OpenStreetMap contributors (ODbL)"


BEARING_RANGE = 60          # degrees either side of the car's heading the start may take


def url(lat0, lon0, lat1, lon1, bearing=None, via=()):
    """bearing: the car's heading (compass degrees): the route starts on a road the car can follow that way. via:
    [(lat, lon)] the route passes through on the way, in order (at most MAX_VIA)."""
    pts = [(lat0, lon0)] + list(via)[:MAX_VIA] + [(lat1, lon1)]
    u = OSRM_URL.format(coords=";".join(f"{float(lo):.6f},{float(la):.6f}" for la, lo in pts))
    if bearing is not None:         # (the start's bearing, none for the others: as many entries as coordinates)
        u += f"&bearings={int(bearing) % 360},{BEARING_RANGE}" + ";" * (len(pts) - 1)
    return u


def _xy(lat, lon, lat_ref):
    m = 111320.0
    return lon * m * dmath.cos(math.radians(lat_ref)), lat * m


def _unwrap(points):
    """A continuous longitude along the route: crossing +/-180 takes the short way, not through Greenwich."""
    out = []
    offset = 0.0
    previous = points[0][1]
    for lat, lon in points:
        delta = lon + offset - previous
        if delta > 180.0:
            offset -= 360.0
        elif delta < -180.0:
            offset += 360.0
        previous = lon + offset
        out.append((lat, previous))
    return out


def simplify(points, tol_m=SIMPLIFY_M):
    """points [(lat, lon)] with the vertices closer than tol_m to their neighbours' line dropped (Ramer-Douglas-
    Peucker, iterative). Pure."""
    n = len(points)
    if n < 3:
        return list(points)
    lat_ref = points[0][0]
    xy = [_xy(la, lo, lat_ref) for la, lo in _unwrap(points)]
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
            d = dnp.sq(px - qx) + dnp.sq(py - qy)
            if d > best:
                best, bi = d, i
        if bi > 0 and best > tol_m * tol_m:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(points, keep) if k]


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
    continuous = _unwrap(points)
    xy = [_xy(la, lo, lat_ref) for la, lo in continuous]
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
        lo = continuous[i][1] + f * (continuous[i + 1][1] - continuous[i][1])
        if not -180.0 <= lo <= 180.0:
            lo = (lo + 180.0) % 360.0 - 180.0
        out.append((la, lo, s))
        s += step
    if out[-1][2] < total - 1e-6:
        out.append((points[-1][0], points[-1][1], total))
    return out


def _nonnegative(value, default=0.0):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else default


def _steps(legs):
    """OSRM maneuvers in leg order, independent of the simplified overview's vertices (GitHub #1075).

    A step's distance/duration run FROM its maneuver to the next. offset is its distance from the route's start;
    leg is one-based, including each leg's depart/arrive steps. Only the first intersection's lanes belong to
    the maneuver; later intersections along the step must not be shown as its turn lanes.
    """
    out, offset, available = [], 0.0, False
    for index, leg in enumerate(legs, 1):
        if not isinstance(leg, dict):
            continue
        raw = leg.get("steps")
        available |= isinstance(raw, list)
        along = 0.0
        for s in raw if isinstance(raw, list) else ():
            if not isinstance(s, dict):
                continue
            distance = _nonnegative(s.get("distance"))
            at = offset + along
            along += distance
            m = s.get("maneuver")
            if not isinstance(m, dict) or not isinstance(m.get("type"), str):
                continue
            try:
                lon, lat = map(float, m["location"][:2])
            except (KeyError, TypeError, ValueError):
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            step = {"type": m["type"], "name": s.get("name") if isinstance(s.get("name"), str) else "",
                    "distance": distance, "duration": _nonnegative(s.get("duration")), "leg": index,
                    "offset": round(at, 1), "location": {"lat": round(lat, 6), "lon": round(lon, 6)}}
            if isinstance(m.get("modifier"), str):
                step["modifier"] = m["modifier"]
            if type(m.get("exit")) is int and m["exit"] > 0:
                step["exit"] = m["exit"]
            for key in ("ref", "destinations", "rotary_name", "driving_side"):
                if isinstance(s.get(key), str) and s[key]:
                    step[key] = s[key]
            for src, dst in (("bearing_before", "bearingBefore"), ("bearing_after", "bearingAfter")):
                value = _nonnegative(m.get(src), None)
                if value is not None and value < 360:
                    step[dst] = value
            intersections = s.get("intersections")
            first = intersections[0] if isinstance(intersections, list) and intersections else None
            lanes = first.get("lanes") if isinstance(first, dict) else None
            if isinstance(lanes, list):
                step["lanes"] = [{"valid": lane["valid"],
                                  "indications": [v for v in lane["indications"] if isinstance(v, str)]}
                                 for lane in lanes if isinstance(lane, dict) and type(lane.get("valid")) is bool
                                 and isinstance(lane.get("indications"), list)]
            out.append(step)
        offset += _nonnegative(leg.get("distance"), along)
    return out if available else None


def parse(data):
    """{points: [[lat, lon], ...], dist (m), dur (s)} of an OSRM route response, or raises ValueError. Pure."""
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
    out = {"points": [[round(a, 6), round(b, 6)] for a, b in pts], "dist": float(r.get("distance") or 0),
           "dur": float(r.get("duration") or 0)}
    legs = r.get("legs") or []
    steps = _steps(legs)
    if steps is not None:
        out["steps"] = steps
    if len(legs) > 1:               # (a route through waypoints: each leg's length and time, start to the first ...)
        out["legs"] = [[round(float(g.get("distance") or 0), 1), round(float(g.get("duration") or 0), 1)]
                       for g in legs]
    return out


def via_points(raw):
    """A route message's via ([[lat, lon], ...]) as [(lat, lon)]: the valid coordinates, at most MAX_VIA. Pure."""
    out = []
    for p in raw if isinstance(raw, (list, tuple)) else ():
        try:
            la, lo = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if -90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0:
            out.append((la, lo))
    return out[:MAX_VIA]


def plan(lat0, lon0, lat1, lon1, net=None, bearing=None, via=()):
    """The road route from (lat0, lon0) to (lat1, lon1) (blocking) through via [(lat, lon)] in order, starting the way
    bearing points if it can (else without it). Raises netfetch.FetchError or ValueError."""
    net = net if net is not None else netfetch.default_fetcher()
    if bearing is not None:
        try:
            return parse(json.loads(net.get(url(lat0, lon0, lat1, lon1, bearing, via), kind="demand")
                                    .body.decode("utf-8")))
        except (ValueError, netfetch.FetchFailed) as e:   # no road that way near the start (OSRM: 400 NoSegment,
            if isinstance(e, netfetch.FetchFailed) and e.status != 400:      # a car across a field): any direction
                raise
    return parse(json.loads(net.get(url(lat0, lon0, lat1, lon1, via=via), kind="demand").body.decode("utf-8")))
