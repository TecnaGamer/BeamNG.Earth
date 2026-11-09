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
import rd

# The dataset's extent (south, west, north, east). A tile that misses it is not requested.
EXTENT = (52.27, 4.72, 52.44, 5.08)
WFS = "https://api.data.amsterdam.nl/v1/wfs/parkeervakken/"
REACH_M = 4.0                       # an inferred spot this close to a surveyed bay is the same bay
TIMEOUT_S = 8
MAX_BYTES = 4 << 20
_CACHE = {}


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
        lats, lons = rd.rd_to_wgs([p[0] for p in rings[0]], [p[1] for p in rings[0]])
        xy = [project(float(lat), float(lon)) for lat, lon in zip(lats, lons)]
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
        x0, y0, x1, y1 = rd.bounds(south, west, north, east)
        key = (round(x0), round(y0), round(x1), round(y1))
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
