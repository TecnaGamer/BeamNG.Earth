"""Dutch roofs from 3DBAG LoD 2.2 (api.3dbag.nl, CC BY 4.0).

One bbox request per map cell, CityJSON features, not the 3D Tiles. A footprint matches on its BAG id,
or on the lidar footprint's centre and area when the map has no BAG id. BAG use and year ride on that
same record when 3DBAG has them. No second mesh and no second download. Outside the Netherlands, and
when the feed fails, the record is missing and today's roof stays.

    © 3DBAG by tudelft3d and 3DGI
"""
import json
import math
import urllib.request

import netfetch

CREDIT = "© 3DBAG by tudelft3d and 3DGI"
API = "https://api.3dbag.nl/collections/pand/items"
NL = (50.75, 3.2, 53.7, 7.3)          # south, west, north, east
CELL_M = 200.0
MATCH_M = 12.0
TIMEOUT_S = 12
MAX_BYTES = 6 << 20
_SHAPES = {"horizontal": "flat", "multiple horizontal": "flat", "slanted": "gabled"}
_CACHE = {}

_X0, _Y0 = 155000.0, 463000.0
_PHI0, _LAM0 = 52.15517440, 5.38720621
_RD_LAT = ((0, 1, 3235.65389), (2, 0, -32.58297), (0, 2, -0.24750), (2, 1, -0.84978), (0, 3, -0.06550),
           (2, 2, -0.01709), (1, 0, -0.00738), (4, 0, 0.00530), (2, 3, -0.00039), (4, 1, 0.00033), (1, 1, -0.00012))
_RD_LON = ((1, 0, 5260.52916), (1, 1, 105.94684), (1, 2, 2.45656), (3, 0, -0.81885), (1, 3, 0.05594),
           (3, 1, -0.05607), (0, 1, 0.01199), (3, 2, -0.00256), (1, 4, 0.00128), (0, 2, 0.00022),
           (2, 0, -0.00022), (5, 0, 0.00026))
_WGS_X = ((0, 1, 190094.945), (1, 1, -11832.228), (2, 1, -114.221), (0, 3, -32.391), (1, 0, -0.705),
          (3, 1, -2.340), (1, 3, -0.608), (0, 2, -0.008), (2, 3, 0.148))
_WGS_Y = ((1, 0, 309056.544), (0, 2, 3638.893), (2, 0, 73.077), (1, 2, -157.984), (3, 0, 59.788),
          (0, 1, 0.433), (2, 2, -6.439), (1, 1, -0.032), (0, 4, 0.092), (1, 4, -0.054))


def _in_nl(lat, lon):
    return lat is not None and lon is not None and NL[0] <= float(lat) <= NL[2] and NL[1] <= float(lon) <= NL[3]


def rd_to_wgs(x, y):
    dx, dy = (float(x) - _X0) * 1e-5, (float(y) - _Y0) * 1e-5
    lat, lon = _PHI0, _LAM0
    for p, q, k in _RD_LAT:
        lat += k * dx ** p * dy ** q / 3600.0
    for p, q, k in _RD_LON:
        lon += k * dx ** p * dy ** q / 3600.0
    return lat, lon


def wgs_to_rd(lat, lon):
    dp, dl = 0.36 * (float(lat) - _PHI0), 0.36 * (float(lon) - _LAM0)
    x, y = _X0, _Y0
    for p, q, k in _WGS_X:
        x += k * dp ** p * dl ** q
    for p, q, k in _WGS_Y:
        y += k * dp ** p * dl ** q
    return x, y


def _bag_key(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits if len(digits) >= 10 else ""


def _first_ring(geometry):
    bounds = (geometry or {}).get("boundaries")
    while isinstance(bounds, list) and bounds and isinstance(bounds[0], list):
        bounds = bounds[0]
    if isinstance(bounds, list) and bounds and isinstance(bounds[0], int):
        return bounds
    return None


def from_cityjson(feat):
    """The roof record of one CityJSON feature, or None when it is not a LoD 2.2 roof. Pure."""
    objects = (feat or {}).get("CityObjects") or {}
    building = None
    for obj in objects.values():
        attrs = obj.get("attributes") or {}
        if obj.get("type") == "Building" and attrs.get("b3_dak_type") and attrs.get("b3_h_maaiveld") is not None:
            building = obj
            break
    if building is None:
        return None
    attrs = building["attributes"]
    shape = _SHAPES.get(str(attrs.get("b3_dak_type") or "").lower())
    if shape is None:
        return None
    try:
        ground = float(attrs["b3_h_maaiveld"])
        peak = float(attrs["b3_h_dak_max"])
        ridge = float(attrs["b3_h_nok"]) if attrs.get("b3_h_nok") is not None else peak
        eave = float(attrs["b3_h_dak_min"]) if attrs.get("b3_h_dak_min") is not None else ground
    except (TypeError, ValueError, KeyError):
        return None
    if not all(math.isfinite(v) for v in (ground, peak, ridge, eave)):
        return None
    height = ridge - ground
    if not 2.5 <= height <= 400:
        return None
    ring = None
    for geom in building.get("geometry") or ():
        ring = _first_ring(geom)
        if ring:
            break
    verts = feat.get("vertices") or []
    if not ring:
        return None
    pts = [verts[i] for i in ring if isinstance(i, int) and i < len(verts)]
    if len(pts) < 3:
        return None
    lat, lon = rd_to_wgs(sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    year = attrs.get("oorspronkelijkbouwjaar")
    use = attrs.get("gebruiksdoel") or attrs.get("gebruiksdoelen") or attrs.get("b3_gebruiksdoel")
    try:
        area = float(attrs["b3_opp_grond"]) if attrs.get("b3_opp_grond") is not None else None
    except (TypeError, ValueError):
        area = None
    return {"height": height, "roof_height": 0.0 if shape == "flat" else max(0.4, peak - eave),
            "roof_shape": shape, "ground": ground, "dak_type": str(attrs.get("b3_dak_type")),
            "year": int(year) if isinstance(year, (int, float)) and math.isfinite(year) else None,
            "use": str(use) if use else None, "lat": lat, "lon": lon, "area": area,
            "bag": _bag_key(attrs.get("identificatie") or next(iter(objects), ""))}


def _metres(lat0, lon0, lat1, lon1):
    dy = (lat1 - lat0) * 111320.0
    dx = (lon1 - lon0) * 111320.0 * math.cos(math.radians(lat0))
    return math.hypot(dx, dy)


def match_records(records, bag_id, lat, lon, area):
    """The record for this footprint, or None. BAG id wins; otherwise the centre within MATCH_M and a similar area."""
    want = _bag_key(bag_id)
    best, best_d = None, MATCH_M
    for rec in records or ():
        if want and rec.get("bag") == want:
            return rec
        if lat is None or rec.get("lat") is None:
            continue
        dist = _metres(float(lat), float(lon), rec["lat"], rec["lon"])
        footprint = rec.get("area")
        if footprint and area and not 0.55 <= float(area) / float(footprint) <= 1.8:
            continue
        if dist < best_d:
            best, best_d = rec, dist
    return best


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": netfetch.USER_AGENT, "Accept": "application/city+json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=netfetch.windows_ssl_context()) as resp:
        return resp.read(MAX_BYTES + 1)


def _cell(lat, lon, fetch):
    x, y = wgs_to_rd(lat, lon)
    key = (int(math.floor(x / CELL_M)), int(math.floor(y / CELL_M)))
    if key not in _CACHE:
        x0, y0 = key[0] * CELL_M, key[1] * CELL_M
        url = f"{API}?bbox={x0 - 15:.0f},{y0 - 15:.0f},{x0 + CELL_M + 15:.0f},{y0 + CELL_M + 15:.0f}&limit=120"
        try:
            body = (fetch or _fetch)(url)
            data = json.loads(body) if body and len(body) <= MAX_BYTES else {}
            _CACHE[key] = [rec for feat in (data.get("features") or ()) if (rec := from_cityjson(feat))]
        except Exception:
            _CACHE[key] = []
    return _CACHE[key]


def attach(attrs, lat, lon, area, fetch=None):
    """`attrs` with a `b3` roof when this footprint has one. The same dict when it does not. Does not raise."""
    if not isinstance(attrs, dict) or not _in_nl(lat, lon):
        return attrs
    try:
        rec = match_records(_cell(float(lat), float(lon), fetch), attrs.get("bag_id") or attrs.get("seed_id"),
                            float(lat), float(lon), area)
    except Exception:
        return attrs
    if rec is None:
        return attrs
    out = dict(attrs)
    out["b3"] = rec
    return out
