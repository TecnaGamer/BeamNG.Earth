"""Bus shelters at mapped bus stops (Overture infrastructure class bus_stop).

A stop is a point (highway=bus_stop). Only a positive shelter tag gives it the
East Coast glass shelter; a plain stop is not evidence of a physical shelter. Its long side
runs along the nearest road. The stock shape's signed side
reads as the forest mesh's opposite of local +x, so the row yaw points local +x away from the
carriageway and that signed side at the street, with the body on the sidewalk. A public-transport
shelter that is not a bus stop (amenity=shelter, shelter_type=public_transport) is included. A
stop with no road nearby still stands, turned by its id.
"""
import hashlib
import json
import logging

import numpy as np

import fast
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


def _tags(feats, i):
    """source_tags of feature i as a lower-case dict, or {} (kept on the layer: shared, not to be changed)."""
    tags = feats.attrs.get("tags")
    if tags is None:
        return {}
    memo = feats.__dict__.setdefault("_lc_tags", {})    # (parsed once per layer: the decoded cells serve many tiles;
    got = memo.get(i)                                   # ~390k json.loads over 16 Midtown tiles, 2026-11-03)
    if got is not None:
        return got
    try:
        values = json.loads(str(tags[i]))
    except (TypeError, ValueError):
        values = None
    got = {str(k).strip().lower(): str(v).strip().lower() for k, v in values.items()} \
        if isinstance(values, dict) else {}
    memo[i] = got
    return got


def _cls(feats, i):
    classes = feats.attrs.get("class")
    return str(classes[i]).strip().lower() if classes is not None else ""


def is_shelter(feats, i):
    """Require positive shelter evidence, including explicit transit-shelter features."""
    tags = _tags(feats, i)
    fast.require(160, "mapped transit shelters")
    return fast._ef.shelter_is_shelter(_cls(feats, i), tags.get("shelter", ""), tags.get("amenity", ""),
                                     tags.get("highway", ""), tags.get("shelter_type") or tags.get("shelter:type", ""))


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


def _road_arrays(roads):
    """Pack road segments once; nearest-road selection and placement run in Rust."""
    segments, widths = [], []
    for _fid, spec, points, _lon in roads or ():
        points = np.asarray(points, np.float64)
        if len(points) >= 2:
            segments.extend(np.column_stack((points[:-1, :2], points[1:, :2])))
            widths.extend([max(0.0, float(spec.width)) * 0.5] * (len(points) - 1))
    return np.asarray(segments, np.float64).reshape(-1, 4), np.asarray(widths, np.float64)


def _place(fid, x, y, segments, widths):
    fast.require(160, "mapped transit shelters")
    return fast._ef.shelter_pose(x, y, segments, widths, _side(fid), _yaw_of(fid),
                                NEAR_ROAD_M, CURB_PAD_M, STREET_X)


def rows(frame, infra_fn, box, elev, roads=(), clearance=None):
    """Forest rows [group, x, y, z, yaw, scale, height, rank] of the bus shelters whose stop is inside box."""
    if infra_fn is None:
        return []
    x0, y0, x1, y1 = (float(v) for v in box)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    try:
        keys = mapcells.cells_for_points(la, lo, pad_lat=0.00005, pad_lon=0.00005, max_cells=64)
    except (ValueError, OverflowError):
        return []
    segments = widths = None
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
            if segments is None:
                segments, widths = _road_arrays(roads)
            ox, oy, yaw = _place(fid, spot[0], spot[1], segments, widths)
            pending.append((ox, oy, yaw))
    if not pending:
        return []
    xy = np.array([[p[0], p[1]] for p in pending], np.float64)
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    if clearance is None:
        import furniture
        clearance = furniture.RoadClearance([(spec.width / 2, pts) for _fid, spec, pts, _lon in roads])
    out = []
    for (ox, oy, yaw), zz in zip(pending, np.asarray(z, np.float64)):
        row = [G_SHELTER, round(ox, 2), round(oy, 2), round(float(zz) + LIFT, 2), round(yaw, 1) % 360.0,
               1.0, SHELTER_H, 0.0]
        if clearance.clear(row):
            out.append(row)
    return out
