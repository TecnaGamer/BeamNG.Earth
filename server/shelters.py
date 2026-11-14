"""Bus shelters at mapped bus stops (Overture infrastructure class bus_stop).

A stop is a point (highway=bus_stop). It gets East Coast's glass shelter unless the tags say
shelter=no. The shelter's long side runs along the nearest road. The stock shape's signed side
reads as the forest mesh's opposite of local +x, so the row yaw points local +x away from the
carriageway and that signed side at the street, with the body on the sidewalk. A public-transport
shelter that is not a bus stop (amenity=shelter, shelter_type=public_transport) is included. A
stop with no road nearby still stands, turned by its id.
"""
import collections
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
CURB_PAD_M = 1.2            # m past the carriageway edge: the signed face, when the point is in the road. 0.6 left the
                            # roof over the edge line and the posts on the paved shoulder (Suchy Dul, 2026-11-05)
LONG_HALF = 1.45            # the shape's half length along the curb (local y)
POST_PAD_M = 0.5            # m every post keeps off any carriageway's edge, and not only the road the shelter faces: a
                            # post stood on the asphalt behind the kerb, at a junction by a car park (Suchy Dul, 2026-11-07)
CLEAR_PASSES = 4            # a shelter pushed off one road may reach another: a few passes, then it stays
POLY_MAX = 80.0             # m²: a bus stop drawn as a polygon is still one shelter
# The regional stock shelters (the user, 2026-11-10: "lets use as many assets as needed to give this world detail and
# variety"; the stock maps' own: art/shapes/objects/s_busstop_*). Each open toward its local -x (its roof's overhang,
# a step: the street side) and long along local y: (trees.lua group, m from the origin to the street side, to the back,
# half length, m its base lies under the origin, height). North America East Coast's glass box; Europe Italy's; the
# tropics Jungle Rock Island's; elsewhere West Coast USA's.
Shelter = collections.namedtuple("Shelter", "group front back half lift h")
ECU = Shelter(G_SHELTER, STREET_X, STREET_X, LONG_HALF, LIFT, SHELTER_H)
SHELTERS = {
    "na": ECU,
    "eu": Shelter("busshelter_it", 0.93, 0.84, 2.21, 0.29, 3.11),
    "tropics": Shelter("busshelter_jri", 1.21, 0.92, 1.55, 0.20, 2.99),
    "other": Shelter("busshelter_wcu", 1.17, 0.43, 1.94, 0.27, 3.12),
}
NA_SHELTER = {"US", "CA", "MX", "PR"}
TROPICS_LAT = 23.5
EUROPE = set("AD AL AM AT AX AZ BA BE BG BY CH CY CZ DE DK EE ES FI FO FR GB GE GG GI GR HR HU IE IM IS IT JE LI LT "
             "LU LV MC MD ME MK MT NL NO PL PT RO RS RU SE SI SJ SK SM TR UA VA XK".split())


def shelter_for(country, lat):
    """The stock shelter of a country (ISO alpha-2, or None: by latitude alone) at a latitude. Pure."""
    c = str(country or "").upper()
    if c in NA_SHELTER:
        return SHELTERS["na"]
    if c in EUROPE:
        return SHELTERS["eu"]
    if abs(float(lat)) < TROPICS_LAT:
        return SHELTERS["tropics"]
    return SHELTERS["other"] if c else SHELTERS["na"]
NO_SHELTER = {"no", "none"}
BODY_ON_ROAD_M = 0.05       # m a point of the body may go past a carriageway's edge before the shelter is left out
TRANSIT_SHELTER = {"public_transport", "public_transit", "bus_stop", "bus", "bus_shelter", "transit"}


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
    """A bus stop, or a shelter tagged for public transport. shelter=no is a pole only, and so is a stop_position
    node (a point on the road where the bus halts) unless it says shelter=yes."""
    tags = _tags(feats, i)
    if tags.get("shelter", "") in NO_SHELTER:
        return False
    amenity = tags.get("amenity", "")
    highway = tags.get("highway", "")
    stype = tags.get("shelter_type") or tags.get("shelter:type") or ""
    bus_stop = _cls(feats, i) == "bus_stop" or highway == "bus_stop" or amenity == "bus_stop"
    transit = amenity == "shelter" and stype in TRANSIT_SHELTER
    if tags.get("public_transport", "") == "stop_position" and tags.get("shelter", "") != "yes" and not transit:
        return False            # the place a bus halts, on the road itself: no platform and no mapped shelter
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
    """(closest x, y, tangent x, y, half width) of the carriageway whose edge is nearest, within NEAR_ROAD_M, or None.
    roads: build_tile's (fid, spec, centreline (k, 3), lon). By the edge, not the centre line: beside a wide road and a
    lane, the wide road is the one the kerb is on."""
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
        d = np.hypot(cx - px, cy - py)
        k = int(np.argmin(d))
        edge = float(d[k]) - half
        if best is None or edge < best[0]:
            span = math.hypot(float(ab[k, 0]), float(ab[k, 1]))
            if span < 1e-6:
                continue
            best = (edge, float(cx[k]), float(cy[k]), float(ab[k, 0]) / span, float(ab[k, 1]) / span, half)
    if best is None or best[0] > NEAR_ROAD_M:
        return None
    return best[1:]


def _posts(ox, oy, yaw, d=None):
    """The (4, 2) corners of a shelter's footprint (d: its Shelter, East Coast's when None): its posts."""
    d = d or ECU
    rad = math.radians(yaw)
    ux, uy, vx, vy = math.cos(rad), math.sin(rad), -math.sin(rad), math.cos(rad)
    return np.array([[ox + sx * ux + sy * vx, oy + sx * uy + sy * vy]
                     for sx in (-d.front, d.back) for sy in (-d.half, d.half)], np.float64)


def _body_points(ox, oy, yaw, d=None):
    """The (9, 2) points of a shelter's footprint: corners, edge middles and centre."""
    d = d or ECU
    rad = math.radians(yaw)
    ux, uy, vx, vy = math.cos(rad), math.sin(rad), -math.sin(rad), math.cos(rad)
    return np.array([[ox + sx * ux + sy * vx, oy + sx * uy + sy * vy]
                     for sx in (-d.front, (d.back - d.front) / 2.0, d.back) for sy in (-d.half, 0.0, d.half)],
                    np.float64)


def _intrusion(ox, oy, yaw, roads, d=None):
    """m the deepest point of the shelter's body is inside any carriageway (0 when none is)."""
    worst = 0.0
    pts9 = _body_points(ox, oy, yaw, d)
    for item in roads or ():
        spec, pts = item[1], np.asarray(item[2], np.float64)
        if len(pts) < 2:
            continue
        half = float(getattr(spec, "width", 0.0) or 0.0) * 0.5
        a, b = pts[:-1, :2], pts[1:, :2]
        ab = b - a
        ab2 = np.maximum((ab * ab).sum(1), 1e-9)
        for p in pts9:
            t = np.clip(((p - a) * ab).sum(1) / ab2, 0.0, 1.0)
            dist = np.hypot(*(p - (a + t[:, None] * ab)).T)
            worst = max(worst, half - float(dist.min()))
    return worst


def _clear_of_roads(ox, oy, yaw, roads, d=None):
    """(x, y) of a shelter moved straight off every carriageway a post stands on or within POST_PAD_M of."""
    for _ in range(CLEAR_PASSES):
        worst = None
        for item in roads or ():
            spec, pts = item[1], np.asarray(item[2], np.float64)
            if len(pts) < 2:
                continue
            half = float(getattr(spec, "width", 0.0) or 0.0) * 0.5
            a, b = pts[:-1, :2], pts[1:, :2]
            ab = b - a
            ab2 = np.maximum((ab * ab).sum(1), 1e-9)
            for p in _posts(ox, oy, yaw, d):
                t = np.clip(((p - a) * ab).sum(1) / ab2, 0.0, 1.0)
                foot = a + t[:, None] * ab
                dist = np.hypot(*(p - foot).T)
                k = int(np.argmin(dist))
                short = half + POST_PAD_M - float(dist[k])
                if short > 1e-3 and (worst is None or short > worst[0]):
                    # (off the road's own centre line: the shelter's centre says which side it is on)
                    n = np.array([ox, oy]) - foot[k]
                    worst = (short, n / max(float(np.hypot(*n)), 1e-6))
        if worst is None:
            break
        ox += float(worst[1][0]) * worst[0]
        oy += float(worst[1][1]) * worst[0]
    return ox, oy


RAIL_SLIDES_M = (0.0, 2.0, -2.0, 4.0, -4.0, 6.0, -6.0, 9.0, -9.0, 12.0, -12.0, 15.0, -15.0)


def _rail_short(ox, oy, yaw, rails, d=None):
    """m the worst post of a shelter stands inside a track's ballast (an item's half width + POST_PAD_M from its
    axis), 0 when clear. rails: railxing.rail_obstacles' [(id, half width, polyline (k, 3))]."""
    worst = 0.0
    for item in rails or ():
        pts = np.asarray(item[2], np.float64)
        if len(pts) < 2:
            continue
        a, b = pts[:-1, :2], pts[1:, :2]
        ab = b - a
        ab2 = np.maximum((ab * ab).sum(1), 1e-9)
        for p in _posts(ox, oy, yaw, d):
            t = np.clip(((p - a) * ab).sum(1) / ab2, 0.0, 1.0)
            dist = float(np.hypot(*(p - (a + t[:, None] * ab)).T).min())
            worst = max(worst, float(item[1]) + POST_PAD_M - dist)
    return worst


def _clear_of_rails(ox, oy, yaw, roads, rails, d=None):
    """(x, y) of a shelter slid along the curb (RAIL_SLIDES_M, the nearest first) to where no post is in a track's
    ballast and none on a carriageway, or None: the road and the track leave it no room (Nemiia, prospekt Heroiv,
    48.4553 N 27.7815 E: a track 3 m past the road's edge, the shelter stood on the rails, 2026-11-08)."""
    if not rails or _rail_short(ox, oy, yaw, rails, d) <= 1e-3:
        return ox, oy
    rad = math.radians(yaw)
    vx, vy = -math.sin(rad), math.cos(rad)             # local +y: along the curb
    for s in RAIL_SLIDES_M[1:]:
        cx, cy = _clear_of_roads(ox + s * vx, oy + s * vy, yaw, roads, d)
        if _rail_short(cx, cy, yaw, rails, d) <= 1e-3:
            return cx, cy
    return None


def _place(fid, x, y, roads, flip=False, d=None):
    """(origin x, y, yaw, ambiguous) with the signed face toward the road and the shelter on the sidewalk; ambiguous
    when the point is on the centre line, so that either side could be the right one (flip takes the other).
    Local +x points away from the carriageway: the stock shape's signed side is that axis reversed."""
    front = (d or ECU).front
    hit = _closest(roads, x, y)
    if hit is None:
        return x, y, _yaw_of(fid), False
    cx, cy, tx, ty, half = hit
    left = (-ty, tx)
    side = (x - cx) * left[0] + (y - cy) * left[1]
    ambiguous = abs(side) < 1.0
    if ambiguous:
        side = _side(fid)
    else:
        side = 1.0 if side > 0.0 else -1.0
    if flip:
        side = -side
    away = (side * left[0], side * left[1])
    toward = (-away[0], -away[1])
    dist = (x - cx) * away[0] + (y - cy) * away[1]
    curb = half + CURB_PAD_M
    if dist < curb:
        fx, fy = cx + away[0] * curb, cy + away[1] * curb
    else:
        fx, fy = x, y
    return (fx - front * toward[0], fy - front * toward[1], (_yaw(toward[0], toward[1]) + 180.0) % 360.0,
            ambiguous)


def _pose(fid, x, y, roads, d=None):
    """(origin x, y, yaw) of a shelter clear of every carriageway, or None when no spot is. A stop on a centre line
    (a junction node, a road beside another) tries both sides and keeps the one with less of the body on a road."""
    best = None
    for flip in (False, True):
        ox, oy, yaw, ambiguous = _place(fid, x, y, roads, flip, d)
        ox, oy = _clear_of_roads(ox, oy, yaw, roads, d)
        deep = _intrusion(ox, oy, yaw, roads, d)
        if best is None or deep < best[0] - 1e-6:
            best = (deep, ox, oy, yaw)
        if best[0] <= BODY_ON_ROAD_M or not ambiguous:
            break
    if best[0] > BODY_ON_ROAD_M:
        return None
    return best[1:]


def _base_heights(frame, elev, xy, yaw, d=None):
    """Local z of each shelter: the lowest ground under its four posts (and its centre). One height at the centre
    left the posts of a shelter on a bank hanging in the air; the lowest point sets the base on the ground and
    buries the uphill posts in the bank instead."""
    rad = np.radians(yaw)
    ux, uy = np.cos(rad), np.sin(rad)           # local +x (away from the road)
    vx, vy = -uy, ux                            # local +y (along the curb)
    n = len(xy)
    px = [xy[:, 0]]
    py = [xy[:, 1]]
    d = d or ECU
    for sx in (-d.front, d.back):
        for sy in (-d.half, d.half):
            px.append(xy[:, 0] + sx * ux + sy * vx)
            py.append(xy[:, 1] + sx * uy + sy * vy)
    px, py = np.concatenate(px), np.concatenate(py)
    lat, lon, _ = frame.enu_to_geodetic(px, py, np.zeros(len(px)))
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    return np.asarray(z, np.float64).reshape(5, n).min(axis=0)


def _rails_near(frame, road_cell_fn, box):
    """railxing.rail_obstacles of the tracks round box, [] without the level-crossing kit (BNE_RAIL_XING) or the cells."""
    if road_cell_fn is None:
        return []
    try:
        import railxing
        if not railxing.ENABLED:
            return []
        x0, y0, x1, y1 = (float(v) for v in box)
        la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
        keys = mapcells.cells_for_points(la, lo, pad_lat=0.0005, pad_lon=0.0005, max_cells=64)
        return railxing.rail_obstacles(frame, [road_cell_fn(k) for k in keys], box)
    except Exception as ex:             # shelters as before rather than none
        log.debug("shelters: tracks unavailable: %s: %s", type(ex).__name__, ex)
        return []


def rows(frame, infra_fn, box, elev, roads=(), road_cell_fn=None, country=None):
    """Forest rows [group, x, y, z, yaw, scale, height, rank] of the bus shelters whose stop is inside box: the
    region's stock shelter (shelter_for: the country, ISO alpha-2; None: the bundled borders at the box's centre)."""
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
    if not country:
        try:
            import countrybounds
            country = countrybounds.bundled().at(float(np.mean(la)), float(np.mean(lo)))
        except Exception:
            country = None
    d = shelter_for(country, float(np.mean(la)))
    rails = _rails_near(frame, road_cell_fn, box)
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
            pose = _pose(fid, spot[0], spot[1], roads, d)
            if pose is None:
                log.debug("shelters: stop %s at (%.1f, %.1f) left out: no spot clear of the carriageways",
                          fid, spot[0], spot[1])
                continue
            if rails:
                got = _clear_of_rails(pose[0], pose[1], pose[2], roads, rails, d)
                if got is None:         # (no room between the road and the track: the stop keeps its sign only)
                    continue
                pose = (got[0], got[1], pose[2])
            pending.append(pose)
    if not pending:
        return []
    xy = np.array([[p[0], p[1]] for p in pending], np.float64)
    zz_all = _base_heights(frame, elev, xy, np.array([p[2] for p in pending], np.float64), d)
    out = []
    for (ox, oy, yaw), zz in zip(pending, zz_all):
        out.append([d.group, round(ox, 2), round(oy, 2), round(float(zz) + d.lift, 2), round(yaw, 1) % 360.0,
                    1.0, d.h, 0.0])
    return out
