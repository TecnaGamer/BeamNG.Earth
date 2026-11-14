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

import netfetch

# "Avoid highways" (GitHub #330): FOSSGIS's OSRM has no exclude=motorway, its Valhalla has use_highways=0 (a trip over
# 1500 km crow-fly is refused there: the normal route is used, like for any other failure)
VALHALLA_URL = "https://valhalla1.openstreetmap.de/route?json="
OSRM_URL = "https://routing.openstreetmap.de/routed-car/route/v1/driving/{lon0:.6f},{lat0:.6f};{lon1:.6f},{lat1:.6f}" \
           "?overview=full&geometries=geojson&steps=false"
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
    return {"points": [[round(a, 6), round(b, 6)] for a, b in pts], "dist": float(r.get("distance") or 0),
            "dur": float(r.get("duration") or 0)}


def valhalla_url(lat0, lon0, lat1, lon1, heading=None):
    a = {"lat": lat0, "lon": lon0} if heading is None else {"lat": lat0, "lon": lon0, "heading": int(heading) % 360}
    q = {"locations": [a, {"lat": lat1, "lon": lon1}], "costing": "auto", "costing_options": {"auto": {"use_highways": 0}}}
    return VALHALLA_URL + urllib.parse.quote(json.dumps(q, separators=(",", ":")))


def decode_polyline(s, precision=6):
    """[(lat, lon)] of an encoded polyline (Valhalla's shape). Raises ValueError when it is cut short."""
    pts, i, lat, lon = [], 0, 0, 0
    while i < len(s):
        for axis in (0, 1):
            shift = v = 0
            while True:
                if i >= len(s):
                    raise ValueError("bad shape")
                b = ord(s[i]) - 63
                i += 1
                v |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(v >> 1) if v & 1 else v >> 1
            lat, lon = (lat + d, lon) if axis == 0 else (lat, lon + d)
        pts.append((lat / 10.0 ** precision, lon / 10.0 ** precision))
    return pts


def parse_valhalla(data):
    """parse()'s answer for a Valhalla route ({"trip": {"legs": [{"shape"}], "summary": {"length" km, "time" s}}}), or
    raises ValueError. Pure."""
    trip = data.get("trip") if isinstance(data, dict) else None
    if not isinstance(trip, dict) or trip.get("status", 0) != 0 or not trip.get("legs"):
        raise ValueError("no route (valhalla)")
    sm = trip.get("summary") or {}
    pts = decode_polyline(trip["legs"][0]["shape"])
    return parse({"code": "Ok", "routes": [{"distance": float(sm.get("length") or 0) * 1000, "duration": sm.get("time"),
                                            "geometry": {"coordinates": [[lo, la] for la, lo in pts]}}]})


def plan(lat0, lon0, lat1, lon1, net=None, bearing=None, avoid_highways=False):
    """The road route from (lat0, lon0) to (lat1, lon1) (blocking), starting the way bearing points if it can (else
    without it). avoid_highways: Valhalla's route without motorways, {..., "avoided": True}; without its answer the
    normal route, "avoided": False. Raises netfetch.FetchError or ValueError."""
    net = net if net is not None else netfetch.default_fetcher()
    for b in ([bearing, None] if bearing is not None else [None]) if avoid_highways else []:
        try:
            r = parse_valhalla(json.loads(net.get(valhalla_url(lat0, lon0, lat1, lon1, b), kind="demand").body.decode("utf-8")))
            return dict(r, avoided=True)
        except (ValueError, netfetch.FetchError):
            pass
    r = _osrm(lat0, lon0, lat1, lon1, net, bearing)
    return dict(r, avoided=False) if avoid_highways else r


def _osrm(lat0, lon0, lat1, lon1, net, bearing):
    if bearing is not None:
        try:
            return parse(json.loads(net.get(url(lat0, lon0, lat1, lon1, bearing), kind="demand").body.decode("utf-8")))
        except (ValueError, netfetch.FetchFailed) as e:   # no road that way near the start (OSRM: 400 NoSegment,
            if isinstance(e, netfetch.FetchFailed) and e.status != 400:      # a car across a field): any direction
                raise
    return parse(json.loads(net.get(url(lat0, lon0, lat1, lon1), kind="demand").body.decode("utf-8")))
