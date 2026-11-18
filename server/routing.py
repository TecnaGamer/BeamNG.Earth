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
import urllib.parse
import dmath
import dnp

import netfetch

# "Avoid highways" (GitHub #330): FOSSGIS's OSRM has no exclude=motorway, its Valhalla has use_highways=0 (a trip over
# 1500 km crow-fly is refused there: the normal route is used, like for any other failure)
VALHALLA_URL = "https://valhalla1.openstreetmap.de/route?json="
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


def valhalla_url(lat0, lon0, lat1, lon1, heading=None):
    a = {"lat": lat0, "lon": lon0} if heading is None else {"lat": lat0, "lon": lon0, "heading": int(heading) % 360}
    q = {"locations": [a, {"lat": lat1, "lon": lon1}], "costing": "auto", "costing_options": {"auto": {"use_highways": 0}}}
    return VALHALLA_URL + urllib.parse.quote(json.dumps(q, separators=(",", ":")))


SHAPE_MAX_CHARS = 2_000_000     # an encoded shape longer than this is refused (~200k points, a 4,000 km drive's)
SHAPE_MAX_SHIFT = 30            # 7 groups of 5 bits: more is no coordinate (an endless varint)


class ValhallaUnavailable(Exception):
    """Valhalla's answer could not be used (malformed, oversized): the normal route, with no "no route" note."""


def decode_polyline(s, precision=6):
    """[(lat, lon)] of an encoded polyline (Valhalla's shape). Raises ValueError when it is cut short, too long, has a
    character outside the encoding's range or an overlong number, or leaves the lat / lon ranges."""
    if not isinstance(s, str) or len(s) > SHAPE_MAX_CHARS:
        raise ValueError("bad shape")
    scale = {5: 1e5, 6: 1e6}.get(precision)     # (exact powers of ten: no C library pow, test_determinism)
    if scale is None:
        raise ValueError("bad precision")
    pts, i, lat, lon = [], 0, 0, 0
    while i < len(s):
        for axis in (0, 1):
            shift = v = 0
            while True:
                if i >= len(s) or shift > SHAPE_MAX_SHIFT:
                    raise ValueError("bad shape")
                b = ord(s[i]) - 63
                if not 0 <= b < 64:
                    raise ValueError("bad shape")
                i += 1
                v |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(v >> 1) if v & 1 else v >> 1
            lat, lon = (lat + d, lon) if axis == 0 else (lat, lon + d)
        la, lo = lat / scale, lon / scale
        if not (-90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0):
            raise ValueError("bad shape")
        pts.append((la, lo))
    return pts


def parse_valhalla(data):
    """parse()'s answer for a Valhalla route ({"trip": {"legs": [{"shape"}], "summary": {"length" km, "time" s}}}), or
    raises ValueError. Pure."""
    trip = data.get("trip") if isinstance(data, dict) else None
    if isinstance(data, dict) and ("error_code" in data or "error" in data) and trip is None:
        raise ValueError("no route (valhalla)")        # (its "no path" answers: error_code 442 and the like)
    if not isinstance(trip, dict):
        raise ValhallaUnavailable("no trip in the answer")
    if trip.get("status", 0) != 0 or not trip.get("legs"):
        raise ValueError("no route (valhalla)")
    sm = trip.get("summary")
    leg = trip["legs"][0] if isinstance(trip["legs"], list) else None
    if not isinstance(sm, dict) or not isinstance(leg, dict) or not isinstance(leg.get("shape"), str) \
            or not isinstance(sm.get("length", 0), (int, float)):
        raise ValhallaUnavailable("malformed trip")
    try:
        pts = decode_polyline(leg["shape"])
    except ValueError as e:
        raise ValhallaUnavailable(str(e)) from None
    if len(pts) < 2:
        raise ValhallaUnavailable("shape of under two points")
    out = parse({"code": "Ok", "routes": [{"distance": float(sm.get("length") or 0) * 1000, "duration": sm.get("time"),
                                           "geometry": {"coordinates": [[lo, la] for la, lo in pts]}}]})
    steps = valhalla_steps(leg.get("maneuvers"), pts)
    if steps is not None:
        out["steps"] = steps
    return out


# Valhalla's maneuver types as OSRM's (type, modifier): the GPS's turn-by-turn reads OSRM's steps (_steps)
_VALHALLA_TYPES = {
    1: ("depart", None), 2: ("depart", "right"), 3: ("depart", "left"),
    4: ("arrive", None), 5: ("arrive", "right"), 6: ("arrive", "left"),
    7: ("new name", "straight"), 8: ("continue", "straight"),
    9: ("turn", "slight right"), 10: ("turn", "right"), 11: ("turn", "sharp right"), 12: ("turn", "uturn"),
    13: ("turn", "uturn"), 14: ("turn", "sharp left"), 15: ("turn", "left"), 16: ("turn", "slight left"),
    17: ("on ramp", "straight"), 18: ("on ramp", "right"), 19: ("on ramp", "left"),
    20: ("off ramp", "slight right"), 21: ("off ramp", "slight left"),
    22: ("fork", "straight"), 23: ("fork", "slight right"), 24: ("fork", "slight left"),
    25: ("merge", "straight"), 26: ("roundabout", None), 27: ("exit roundabout", None),
    37: ("merge", "slight right"), 38: ("merge", "slight left"),
}


def valhalla_steps(maneuvers, pts):
    """_steps' list (one leg) from a Valhalla leg's maneuvers (length km, time s, begin_shape_index into pts, the
    decoded shape), or None without them. Pure."""
    if not isinstance(maneuvers, list):
        return None
    out, offset = [], 0.0
    for m in maneuvers:
        if not isinstance(m, dict):
            continue
        distance = _nonnegative(m.get("length")) * 1000.0
        at = offset
        offset += distance
        kind = _VALHALLA_TYPES.get(m.get("type"), ("continue", None))
        k = m.get("begin_shape_index")
        if type(k) is not int or not 0 <= k < len(pts):
            continue
        lat, lon = pts[k]
        names = m.get("street_names")
        step = {"type": kind[0], "name": names[0] if isinstance(names, list) and names and isinstance(names[0], str)
                else "", "distance": round(distance, 1), "duration": _nonnegative(m.get("time")), "leg": 1,
                "offset": round(at, 1), "location": {"lat": round(lat, 6), "lon": round(lon, 6)}}
        if kind[1]:
            step["modifier"] = kind[1]
        if kind[0] == "roundabout" and type(m.get("roundabout_exit_count")) is int and m["roundabout_exit_count"] > 0:
            step["exit"] = m["roundabout_exit_count"]
        out.append(step)
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


def plan(lat0, lon0, lat1, lon1, net=None, bearing=None, via=(), avoid_highways=False):
    """The road route from (lat0, lon0) to (lat1, lon1) (blocking) through via [(lat, lon)] in order, starting the way
    bearing points if it can (else without it). avoid_highways (only without via): Valhalla's route without
    motorways, {..., "avoided": True}; without its answer the normal route, "avoided": False. Raises
    netfetch.FetchError or ValueError."""
    net = net if net is not None else netfetch.default_fetcher()
    avoid_highways = avoid_highways and not via
    avoided = None          # (None: Valhalla not reached, the normal route without a "no route" note)
    for b in ([bearing, None] if bearing is not None else [None]) if avoid_highways else []:
        try:
            body = net.get(valhalla_url(lat0, lon0, lat1, lon1, b), kind="demand").body
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as e:
                raise ValhallaUnavailable(f"not JSON: {e}") from None
            try:
                r = parse_valhalla(data)
            except (TypeError, KeyError, AttributeError, IndexError, OverflowError) as e:
                raise ValhallaUnavailable(f"malformed answer: {type(e).__name__}") from None
            return dict(r, avoided=True)
        except ValhallaUnavailable:     # (a malformed answer: the normal route, no "no route" note)
            avoided = None
            break
        except ValueError:      # (no route that way: again without the heading, then the normal route)
            avoided = False
        except netfetch.FetchError:     # (the service down or slow: straight to the normal route, asked once)
            avoided = None
            break
    r = _osrm(lat0, lon0, lat1, lon1, net, bearing, via)
    return dict(r, avoided=avoided) if avoid_highways else r


def _osrm(lat0, lon0, lat1, lon1, net, bearing, via=()):
    if bearing is not None:
        try:
            return parse(json.loads(net.get(url(lat0, lon0, lat1, lon1, bearing, via), kind="demand")
                                    .body.decode("utf-8")))
        except (ValueError, netfetch.FetchFailed) as e:   # no road that way near the start (OSRM: 400 NoSegment,
            if isinstance(e, netfetch.FetchFailed) and e.status != 400:      # a car across a field): any direction
                raise
    return parse(json.loads(net.get(url(lat0, lon0, lat1, lon1, via=via), kind="demand").body.decode("utf-8")))
