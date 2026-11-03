"""Bus shelters at mapped bus stops (Overture infrastructure class bus_stop).

A stop is a point (highway=bus_stop). It gets East Coast's glass shelter unless the tags say
shelter=no. The shelter's long side runs along the nearest road. The stock shape's signed side
reads as the forest mesh's opposite of local +x, so the row yaw points local +x away from the
carriageway and that signed side at the street, with the body on the sidewalk. A public-transport
shelter that is not a bus stop (amenity=shelter, shelter_type=public_transport) is included. A
stop with no road nearby still stands, turned by its id.
"""
import hashlib
import json
import logging
import math

import numpy as np

import mapcells
import mapgeom

log = logging.getLogger("earthd.shelters")

G_SHELTER = "busshelter"
# s_busstop_ecu: x -0.74..0.74 (the signed face reads as the forest mesh's -x), y -1.45..1.44 along the curb,
# z -0.20..2.70.
STREET_X = 0.74
LIFT = 0.20
SHELTER_H = 2.90
NEAR_ROAD_M = 30.0          # a stop farther from every carriageway is not turned toward one
CURB_PAD_M = 0.6            # m past the carriageway edge: the signed face, when the point is in the road
POLY_MAX = 80.0             # m²: a bus stop drawn as a polygon is still one shelter
NO_SHELTER = {"no", "none"}
TRANSIT_SHELTER = {"public_transport", "public_transit", "bus_stop", "bus", "bus_shelter", "transit"}


def _tags(feats, i):
    """source_tags of feature i as a lower-case dict, or {}."""
    tags = feats.attrs.get("tags")
    if tags is None:
        return {}
    try:
        values = json.loads(str(tags[i]))
    except (TypeError, ValueError):
        return {}
    if not isinstance(values, dict):
        return {}
    return {str(k).strip().lower(): str(v).strip().lower() for k, v in values.items()}


def _cls(feats, i):
    classes = feats.attrs.get("class")
    return str(classes[i]).strip().lower() if classes is not None else ""


def is_shelter(feats, i):
    """A bus stop, or a shelter tagged for public transport. shelter=no is a pole only."""
    tags = _tags(feats, i)
    if tags.get("shelter", "") in NO_SHELTER:
        return False
    amenity = tags.get("amenity", "")
    highway = tags.get("highway", "")
    stype = tags.get("shelter_type") or tags.get("shelter:type") or ""
    bus_stop = _cls(feats, i) == "bus_stop" or highway == "bus_stop" or amenity == "bus_stop"
    transit = amenity == "shelter" and stype in TRANSIT_SHELTER
    return bus_stop or transit


def _yaw(dx, dy):
    """Degrees that turn a Forest item's local +x toward (dx, dy)."""
    return math.degrees(math.atan2(dy, dx)) % 360.0


def _side(fid):
    """1 or -1, stable for a stop that sits on the centreline."""
    digest = hashlib.blake2b(f"{fid}\0side".encode(), digest_size=2).digest()
    return 1.0 if int.from_bytes(digest, "little") % 2 else -1.0


def _yaw_of(fid):
    digest = hashlib.blake2b(f"{fid}\0yaw".encode(), digest_size=2).digest()
    return (int.from_bytes(digest, "little") % 3600) / 10.0


def _open(ring):
    ring = np.asarray(ring, np.float64)
    if len(ring) >= 2 and float(np.max(np.abs(ring[0] - ring[-1]))) < 1e-6:
        return ring[:-1]
    return ring


def _area(ring):
    ring = _open(ring)
    if len(ring) < 3:
        return 0.0
    x, y = ring[:, 0], ring[:, 1]
    return abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) * 0.5


def _spot(feats, frame, i):
    """(x, y) of a point, or of a small polygon's centre. None when it is not one shelter."""
    kind = int(feats.kind[i])
    parts = feats.parts(i)
    if not parts or not parts[0] or len(parts[0][0]) < 1:
        return None
    e, n = mapgeom.enu_xy(feats, frame, np.asarray(parts[0][0], np.float64))
    ring = np.column_stack((e, n))
    if kind == mapgeom.POINT:
        return float(ring[0, 0]), float(ring[0, 1])
    if kind != mapgeom.POLYGON:
        return None
    ring = _open(ring)
    if len(ring) < 3 or _area(ring) > POLY_MAX:
        return None
    c = ring.mean(0)
    return float(c[0]), float(c[1])


def _closest(roads, x, y):
    """(closest x, y, tangent x, y, half width) of the nearest carriageway within NEAR_ROAD_M, or None.
    roads: build_tile's (fid, spec, centreline (k, 3), lon)."""
    best = None
    px, py = float(x), float(y)
    for item in roads or ():
        spec, pts = item[1], np.asarray(item[2], np.float64)
        if len(pts) < 2:
            continue
        half = float(getattr(spec, "width", 0.0) or 0.0) * 0.5
        a, b = pts[:-1, :2], pts[1:, :2]
        ab = b - a
        ab2 = np.maximum(ab[:, 0] ** 2 + ab[:, 1] ** 2, 1e-9)
        t = np.clip(((px - a[:, 0]) * ab[:, 0] + (py - a[:, 1]) * ab[:, 1]) / ab2, 0.0, 1.0)
        cx = a[:, 0] + t * ab[:, 0]
        cy = a[:, 1] + t * ab[:, 1]
        d2 = (cx - px) ** 2 + (cy - py) ** 2
        k = int(np.argmin(d2))
        if best is None or d2[k] < best[0]:
            span = math.hypot(float(ab[k, 0]), float(ab[k, 1]))
            if span < 1e-6:
                continue
            best = (float(d2[k]), float(cx[k]), float(cy[k]), float(ab[k, 0]) / span, float(ab[k, 1]) / span, half)
    if best is None or best[0] > NEAR_ROAD_M ** 2:
        return None
    return best[1:]


def _place(fid, x, y, roads):
    """(origin x, y, yaw) with the signed face toward the road and the shelter on the sidewalk.
    Local +x points away from the carriageway: the stock shape's signed side is that axis reversed."""
    hit = _closest(roads, x, y)
    if hit is None:
        return x, y, _yaw_of(fid)
    cx, cy, tx, ty, half = hit
    left = (-ty, tx)
    side = (x - cx) * left[0] + (y - cy) * left[1]
    if abs(side) < 1.0:
        side = _side(fid)
    else:
        side = 1.0 if side > 0.0 else -1.0
    away = (side * left[0], side * left[1])
    toward = (-away[0], -away[1])
    dist = (x - cx) * away[0] + (y - cy) * away[1]
    curb = half + CURB_PAD_M
    if dist < curb:
        fx, fy = cx + away[0] * curb, cy + away[1] * curb
    else:
        fx, fy = x, y
    return fx - STREET_X * toward[0], fy - STREET_X * toward[1], (_yaw(toward[0], toward[1]) + 180.0) % 360.0


def rows(frame, infra_fn, box, elev, roads=()):
    """Forest rows [group, x, y, z, yaw, scale, height, rank] of the bus shelters whose stop is inside box."""
    if infra_fn is None:
        return []
    x0, y0, x1, y1 = (float(v) for v in box)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    try:
        keys = mapcells.cells_for_points(la, lo, pad_lat=0.00005, pad_lon=0.00005, max_cells=64)
    except (ValueError, OverflowError):
        return []
    pending = []
    seen = set()
    for key in keys:
        try:
            feats = infra_fn(key)
        except Exception as ex:
            log.debug("shelters: infrastructure cell %s unavailable: %s", key, ex)
            continue
        if feats is None or not len(feats) or feats.attrs.get("class") is None and feats.attrs.get("tags") is None:
            continue
        boxes = feats.boxes(frame)
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            if fid in seen or not is_shelter(feats, i):
                continue
            b = boxes[i]
            if b[2] < x0 or b[0] >= x1 or b[3] < y0 or b[1] >= y1:
                continue
            spot = _spot(feats, frame, i)
            if spot is None or not (x0 <= spot[0] < x1 and y0 <= spot[1] < y1):
                continue
            seen.add(fid)
            ox, oy, yaw = _place(fid, spot[0], spot[1], roads)
            pending.append((ox, oy, yaw))
    if not pending:
        return []
    xy = np.array([[p[0], p[1]] for p in pending], np.float64)
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    out = []
    for (ox, oy, yaw), zz in zip(pending, np.asarray(z, np.float64)):
        out.append([G_SHELTER, round(ox, 2), round(oy, 2), round(float(zz) + LIFT, 2), round(yaw, 1) % 360.0,
                    1.0, SHELTER_H, 0.0])
    return out
