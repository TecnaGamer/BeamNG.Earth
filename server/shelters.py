"""Bus shelters at mapped bus stops (Overture infrastructure class bus_stop).

How strong a stop's evidence is (earthfast.shelter_evidence): a positive shelter tag, or a public-transport
amenity=shelter, is a shelter; shelter=no is none. An untagged stop or bus platform beside the road is kept only
where the shelter's whole body clears every road. An untagged public_transport=stop_position, or an untagged
bus_stop node on the carriageway itself, is where the bus halts, never a shelter spot (the Hautes-Alpes and
Grenoble map every stop that way, on the road's junction node: an AI car hit one at Risoul, PR #869's review); a
shelter=yes stop position gives way to a platform mapped within PLATFORM_NEAR_M.

The long side runs along the road whose EDGE is nearest, the signed face CURB_PAD_M past that kerb (PR #889's
rules). The stock shape's signed side reads as the forest mesh's opposite of local +x, so the row yaw points local
+x away from the carriageway and that signed side at the street, with the body on the sidewalk. A shelter whose
body meets another road is set back in PUSH_STEPS, then left out; its base is at its lowest corner's ground. A
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
CURB_PAD_M = 1.2            # m past the carriageway edge: the signed face, when the point is in the road (PR #889)
ON_ROAD_M = 0.5             # m outside a carriageway edge: an untagged bus_stop node closer stands on the road
PLATFORM_NEAR_M = 30.0      # m: a shelter=yes stop position gives way to a bus platform this close
PUSH_STEPS = (0.5, 1.0, 1.5, 2.0)   # m farther from the road, tried when the body meets another one
BODY_Y = (-1.45, 1.44)      # the body along the kerb (furniture.py's busshelter box)
GATHER_PAD_DEG = 0.0004     # ~40 m: platforms of the next tile, for a stop position near the edge
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


def evidence(feats, i):
    """earthfast.shelter_evidence of feature i: NONE, EXPLICIT, CANDIDATE, ON_ROAD or EXPLICIT_ON_ROAD."""
    tags = _tags(feats, i)
    fast.require(170, "mapped transit shelters")
    return fast._ef.shelter_evidence(_cls(feats, i), tags.get("shelter", ""), tags.get("amenity", ""),
                                     tags.get("highway", ""), tags.get("shelter_type") or tags.get("shelter:type", ""),
                                     tags.get("public_transport", ""), tags.get("bus", ""))


NONE, EXPLICIT, CANDIDATE, ON_ROAD, EXPLICIT_ON_ROAD = range(5)


def is_shelter(feats, i):
    """Could feature i carry a shelter at all (any evidence short of a stop position on the road)?"""
    return evidence(feats, i) in (EXPLICIT, CANDIDATE, EXPLICIT_ON_ROAD)


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
    """(x, y, yaw, beside a road) of the shelter of stop fid at (x, y)."""
    fast.require(170, "mapped transit shelters")
    return fast._ef.shelter_pose(x, y, segments, widths, _side(fid), _yaw_of(fid),
                                 NEAR_ROAD_M, CURB_PAD_M, STREET_X)


def _on_road(x, y, segments, widths):
    return len(segments) > 0 and fast._ef.shelter_on_road(x, y, segments, widths, ON_ROAD_M)


def _corners(ox, oy, yaw):
    """The body's four ground corners (4, 2) of a row at (ox, oy, yaw)."""
    a = np.radians(yaw)
    ax, ay, tx, ty = np.cos(a), np.sin(a), -np.sin(a), np.cos(a)
    return np.array([[ox + u * ax + v * tx, oy + u * ay + v * ty]
                     for u in (-STREET_X, STREET_X) for v in BODY_Y], np.float64)


def _base(frame, elev, ox, oy, yaw):
    """The ground z under the body's lowest corner (PR #889: a post on a slope never hangs in the air)."""
    xy = _corners(ox, oy, yaw)
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    return float(np.min(z))


def rows(frame, infra_fn, box, elev, roads=(), clearance=None):
    """Forest rows [group, x, y, z, yaw, scale, height, rank] of the bus shelters whose stop is inside box."""
    if infra_fn is None:
        return []
    x0, y0, x1, y1 = (float(v) for v in box)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    try:
        keys = mapcells.cells_for_points(la, lo, pad_lat=GATHER_PAD_DEG, pad_lon=GATHER_PAD_DEG, max_cells=64)
    except (ValueError, OverflowError):
        return []
    segments, widths = _road_arrays(roads)
    stops, platforms, seen = [], [], set()
    for key in keys:
        try:
            feats = infra_fn(key)
        except Exception as ex:
            log.debug("shelters: infrastructure cell %s unavailable: %s", key, ex)
            continue
        if feats is None or not len(feats) or feats.attrs.get("class") is None and feats.attrs.get("tags") is None:
            continue
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            if fid in seen:
                continue
            code = evidence(feats, i)
            if code in (NONE, ON_ROAD):
                continue
            spot = _spot(feats, frame, i)
            if spot is None:
                continue
            seen.add(fid)
            if code == CANDIDATE and _on_road(spot[0], spot[1], segments, widths):
                continue                    # (an untagged node on the carriageway: a stop position by its place)
            if code != EXPLICIT_ON_ROAD and not _on_road(spot[0], spot[1], segments, widths):
                platforms.append(spot)
            if x0 <= spot[0] < x1 and y0 <= spot[1] < y1:
                stops.append((fid, spot, code))
    out = []
    if not stops:
        return out
    if clearance is None:
        import furniture
        clearance = furniture.RoadClearance([(spec.width / 2, pts) for _fid, spec, pts, _lon in roads])
    plat = np.asarray(platforms, np.float64).reshape(-1, 2)
    for fid, (x, y), code in stops:
        if code == EXPLICIT_ON_ROAD and len(plat) and                 float(np.min(np.hypot(plat[:, 0] - x, plat[:, 1] - y))) <= PLATFORM_NEAR_M:
            continue                        # (the platform mapped beside it carries the shelter)
        ox, oy, yaw, beside = _place(fid, x, y, segments, widths)
        a = np.radians(yaw)
        for push in (0.0,) + (PUSH_STEPS if beside else ()):
            px, py = ox + push * np.cos(a), oy + push * np.sin(a)
            row = [G_SHELTER, round(px, 2), round(py, 2), round(_base(frame, elev, px, py, yaw) + LIFT, 2),
                   round(yaw, 1) % 360.0, 1.0, SHELTER_H, 0.0]
            if clearance.clear(row):
                out.append(row)
                break
    return out
