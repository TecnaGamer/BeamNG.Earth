"""Marked parking bays from Gemeente Amsterdam (parkeervakken, CC0).

The WFS is the city's own rectangles, including the national E-code on a bay (ordinary, disabled,
loading, taxi). A tile outside the published extent gets nothing, and the inferred curb spots stay.
A failed or empty reply does the same: the road tile is not dropped.

    spots = for_tile(frame, box, ground)          # [x, y, z, heading, kind] in the frame, or []
    park.extend(replace_overlapping(inferred, spots))
"""
import json
import math
import urllib.request

import numpy as np

import netfetch

# The dataset's extent (south, west, north, east). A tile that misses it is not requested.
EXTENT = (52.27, 4.72, 52.44, 5.08)
WFS = "https://api.data.amsterdam.nl/v1/wfs/parkeervakken/"
REACH_M = 4.0                       # an inferred spot this close to a surveyed bay is the same bay
TIMEOUT_S = 8
MAX_BYTES = 4 << 20
_CACHE = {}

# Rijksdriehoek (EPSG:28992) <-> WGS84, the Kadaster approximation (centimetres).
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


def rd_to_wgs(x, y):
    """(lat, lon) of an RD New point. Pure."""
    dx, dy = (float(x) - _X0) * 1e-5, (float(y) - _Y0) * 1e-5
    lat, lon = _PHI0, _LAM0
    for p, q, k in _RD_LAT:
        lat += k * dx ** p * dy ** q / 3600.0
    for p, q, k in _RD_LON:
        lon += k * dx ** p * dy ** q / 3600.0
    return lat, lon


def wgs_to_rd(lat, lon):
    """(x, y) RD New of a WGS84 point. Pure."""
    dp, dl = 0.36 * (float(lat) - _PHI0), 0.36 * (float(lon) - _LAM0)
    x, y = _X0, _Y0
    for p, q, k in _WGS_X:
        x += k * dp ** p * dl ** q
    for p, q, k in _WGS_Y:
        y += k * dp ** p * dl ** q
    return x, y


def kind_of(e_type):
    """The bay's kind from its E-code. E6 disabled, E7 loading, E8 taxi; anything else is ordinary."""
    code = str(e_type or "").strip().upper()
    if code.startswith("E6"):
        return "disabled"
    if code.startswith("E7"):
        return "loading"
    if code.startswith("E8"):
        return "taxi"
    return "ordinary"


def _overlaps(south, west, north, east):
    return not (north < EXTENT[0] or south > EXTENT[2] or east < EXTENT[1] or west > EXTENT[3])


def _box_wgs(frame, box):
    x0, y0, x1, y1 = box
    lats, lons = [], []
    for x, y in ((x0, y0), (x0, y1), (x1, y0), (x1, y1)):
        lat, lon, _ = frame.enu_to_geodetic(float(x), float(y), 0.0)
        lats.append(float(lat))
        lons.append(float(lon))
    return min(lats), min(lons), max(lats), max(lons)


def _heading(ring):
    best, ang = 0.0, 0.0
    for a, b in zip(ring, ring[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        d = dx * dx + dy * dy
        if d > best:
            best, ang = d, math.degrees(math.atan2(dy, dx))
    return ang


def _ground_z(ground, x, y):
    if ground is None:
        return 0.0
    try:
        z = ground(x, y)
    except Exception:
        return 0.0
    z = float(np.asarray(z).reshape(-1)[0])
    return z if np.isfinite(z) else 0.0


def _inside(box, x, y):
    x0, y0, x1, y1 = box
    return x0 <= x < x1 and y0 <= y < y1


def spots_from_features(features, project, box, ground=None):
    """Rows [x, y, z, heading, kind] for GeoJSON features whose centre `project(lat, lon)` puts in `box`.

    `project` returns (x, y) in the same frame as `box`. A feature with no ring is skipped. Pure given project."""
    out = []
    for feat in features or ():
        geom = (feat or {}).get("geometry") or {}
        rings = []
        if geom.get("type") == "Polygon":
            rings = geom.get("coordinates") or []
        elif geom.get("type") == "MultiPolygon":
            rings = [r[0] for r in (geom.get("coordinates") or []) if r]
        if not rings or len(rings[0]) < 4:
            continue
        xy = []
        for x_rd, y_rd, *_ in rings[0]:
            lat, lon = rd_to_wgs(x_rd, y_rd)
            xy.append(project(lat, lon))
        cx = sum(p[0] for p in xy[:-1]) / (len(xy) - 1)
        cy = sum(p[1] for p in xy[:-1]) / (len(xy) - 1)
        if not _inside(box, cx, cy):
            continue
        props = feat.get("properties") or {}
        out.append([cx, cy, _ground_z(ground, cx, cy), _heading(xy), kind_of(props.get("e_type"))])
    return out


def replace_overlapping(inferred, surveyed, reach=REACH_M):
    """Inferred curb spots, without those that sit on a surveyed bay, then the surveyed bays.

    An empty survey leaves the inferred spots as they are. Pure."""
    if not surveyed:
        return list(inferred)
    reach2 = float(reach) * float(reach)
    kept = []
    for row in inferred:
        if any((row[0] - s[0]) ** 2 + (row[1] - s[1]) ** 2 < reach2 for s in surveyed):
            continue
        kept.append(row)
    kept.extend(surveyed)
    return kept


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": netfetch.USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=netfetch.windows_ssl_context()) as resp:
        return resp.read(MAX_BYTES + 1)


def for_tile(frame, box, ground=None, fetch=None):
    """The tile's surveyed bays, or [] outside Amsterdam and when the WFS fails. Does not raise."""
    try:
        south, west, north, east = _box_wgs(frame, box)
        if not _overlaps(south, west, north, east):
            return []
        corners = [wgs_to_rd(lat, lon) for lat, lon in (
            (south, west), (south, east), (north, west), (north, east))]
        xs, ys = [c[0] for c in corners], [c[1] for c in corners]
        key = (round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys)))
        if key not in _CACHE:
            url = (f"{WFS}?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature&TYPENAMES=parkeervakken"
                   f"&OUTPUTFORMAT=application/json&BBOX={key[0]},{key[1]},{key[2]},{key[3]}&COUNT=300")
            body = (fetch or _fetch)(url)
            if len(body) > MAX_BYTES:
                _CACHE[key] = []
            else:
                data = json.loads(body)
                _CACHE[key] = data.get("features") or []
        features = _CACHE[key]

        def project(lat, lon):
            x, y, _ = frame.geodetic_to_enu(float(lat), float(lon), 0.0)
            return float(x), float(y)

        return spots_from_features(features, project, box, ground)
    except Exception:
        return []
