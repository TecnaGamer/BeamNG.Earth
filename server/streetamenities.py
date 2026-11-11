"""street payphones and post boxes from OpenStreetMap (amenity=telephone / post_box rows of osmfuel.py's cells), one prop
each, in the gas stations' road tile records (gasstations.tile_sites: the same background fetch of the cells and the same
resend of the tiles when one arrives). Australia only for now: MODELS maps a region to its models, another region adds its
own entry and a test in region_of. Pure and deterministic (every tile, anchor and BeamMP peer agrees): the points are
merged within DUP_M by id order, one off a carriageway is moved onto the verge, one inside a building (an indoor phone) is
left out, each faces its nearest road."""
import numpy as np

import gasstations as gs

_SHAPES = "/levels/beamng_earth/art/shapes/beamngEarth/"
MODELS = {      # region -> amenity -> the model (tools/build_au_street_amenities.py: no logos, front towards -Y)
    "AU": {"telephone": _SHAPES + "bne_au_payphone.dae", "post_box": _SHAPES + "bne_au_postbox.dae"},
}
AMENITIES = ("telephone", "post_box")
FRONT_DYAW = 90.0       # the mod's yaw turns a model's -Y front to yaw + 90 degrees (evchargers.CHARGER_DYAW)
DUP_M = 3.0             # points of one kind this close are one (a node and its way, two mappers)
EDGE_MIN_M = 0.4        # a point nearer than this to a carriageway's edge (or on it) is moved off it ...
KERB_OFF_M = 1.0        # ... to this far beyond the edge, on its own side
FACE_REACH_M = 30.0     # faces a road within this; else a turn by its id
ROAD_CLEAR_M = 0.3      # a moved point stays this far off every other carriageway
BLD_CLEAR_M = 0.2


def region_of(lat, lon):
    """The region whose models apply at (lat, lon) (scalars), or None."""
    import climate
    return "AU" if climate.au_state(lat, lon) is not None else None


def points(frame, osm):
    """[{"id", "kind", "x", "y", "name", "region"}] of the amenities among the cached OSM cells' rows (gasstations._osm_points)
    in a region with models, in a fixed order, kept apart by DUP_M. Costs only the rows' amenity check where there are none."""
    out = []
    for kind in AMENITIES:
        pts = gs._osm_points(frame, osm, amenity=kind)
        pts.sort(key=lambda p: p["id"])
        seen = {}
        for p in pts:
            la, lo, _ = frame.enu_to_geodetic(p["x"], p["y"], 0.0)
            region = region_of(float(la), float(lo))
            if region is None or kind not in MODELS[region]:
                continue
            gx, gy = int(p["x"] // DUP_M), int(p["y"] // DUP_M)
            if any(np.hypot(p["x"] - q[0], p["y"] - q[1]) < DUP_M
                   for i in (-1, 0, 1) for j in (-1, 0, 1) for q in seen.get((gx + i, gy + j), ())):
                continue
            seen.setdefault((gx, gy), []).append((p["x"], p["y"]))
            out.append({"id": f"{kind}:{p['id']}", "kind": kind, "x": p["x"], "y": p["y"], "name": p["name"],
                        "region": region})
    return out


def _nearest(roads, p):
    """(point on the nearest centreline, distance, half width) of the drive lines (gasstations.drive_lines) nearest p; None."""
    best = None
    for pts, half in roads:
        a, b = pts[:-1], pts[1:]
        d = b - a
        t = np.clip(((p - a) * d).sum(1) / np.maximum((d * d).sum(1), 1e-12), 0.0, 1.0)
        q = a + d * t[:, None]
        dist = np.hypot(*(q - p).T)
        i = int(np.argmin(dist))
        if best is None or dist[i] < best[1]:
            seg = d[i] / max(float(np.hypot(*d[i])), 1e-12)
            best = (q[i], float(dist[i]), half, seg)
    return best


def _in_building(p, blds, clear=0.0):
    import polygon
    pt = p[None]
    for b in blds:
        lo, hi = b.ring.min(0) - clear, b.ring.max(0) + clear
        if not ((p >= lo) & (p <= hi)).all():
            continue
        if polygon.contains_points(b.ring, pt).any() or (clear and gs._seg_dist(pt, b.ring[:-1], b.ring[1:])[0] < clear):
            return True
    return False


def place(pt, roads, blds):
    """(x, y, yaw degrees) for amenity point pt among drive lines and buildings (gasstations.drive_lines /
    buildings_touching), or None: a point inside a building is left out; one on or at the edge of a carriageway is moved to
    KERB_OFF_M beyond it; the model faces its nearest road."""
    p = np.array([pt["x"], pt["y"]], np.float64)
    if _in_building(p, blds):
        return None
    near = _nearest(roads, p) if roads else None
    if near is not None and near[1] - near[2] < EDGE_MIN_M:
        q, d, half, seg = near
        v = p - q
        n = v / d if d > 0.05 else np.array([-seg[1], seg[0]])      # (on the centreline: its left)
        p = q + n * (half + KERB_OFF_M)
        for line, h in roads:
            if gs._seg_dist(p[None], line[:-1], line[1:])[0] < h + ROAD_CLEAR_M:
                return None             # (a junction: another road's carriageway)
        if _in_building(p, blds, BLD_CLEAR_M):
            return None
    near = _nearest(roads, p) if roads else None
    if near is not None and near[1] <= FACE_REACH_M:
        front = near[0] - p
        yaw = gs._yaw(front / max(float(np.hypot(*front)), 1e-9))
    else:
        yaw = 90.0 * (sum(map(ord, pt["id"])) % 4)
    return float(p[0]), float(p[1]), (yaw + FRONT_DYAW) % 360.0


def sites(pts, box, roads, blds):
    """[gasstations.Site] of the points in tile box (roads.box_owns) that place() keeps."""
    import roads as roadsmod
    out = []
    for pt in pts:
        if not roadsmod.box_owns(box, pt["x"], pt["y"]):
            continue
        at = place(pt, roads, blds)
        if at is None:
            continue
        x, y, yaw = at
        props = [dict(shape=MODELS[pt["region"]][pt["kind"]], x=x, y=y, dz=0.0, yaw=yaw, scale=[1.0, 1.0, 1.0],
                      collide=True)]
        st = gs.Station("amen:" + pt["id"], x, y, pt["name"], "", (pt["id"],), True, True)
        out.append(gs.Site(st, props, [], (), shop_id="amen:" + pt["id"]))
    return out


def shapes_at(lat, lon):
    """The shapes the mod preloads for an anchor at (lat, lon) (earthd's fuelShapes)."""
    region = region_of(lat, lon)
    return list(MODELS[region].values()) if region else []
