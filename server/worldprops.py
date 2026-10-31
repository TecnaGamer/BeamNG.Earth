"""Stock props on a road tile (the user, 2026-10-29: cell towers, water towers, containers, blank
billboards, barns): Forest rows in furniture.place's form, [group, x, y, z, yaw, scale, height, rank],
drawn by trees.lua. Heights are the meshes' vertical size, so a row of that height stands at 1:1
(trees.pick scales height / catalog height).

Towers are Overture infrastructure points (or a polygon's centre). Containers fill an industrial /
works / farmyard polygon, lined up with its long side. A blank billboard is placed only where the
source tags say advertising=billboard. That tag is not filled in the pinned Overture release, so the
boards also come from OpenStreetMap (billboardsource.py). The mesh is West Coast's drag-strip board,
which carries the poster; its face looks along local +y. A barn replaces the
procedural one when the footprint is close to the stock mesh; the building id comes back for the
road tile to skip.

A feature is emitted by the tile that owns its centre, except containers: every tile that sees the
yard places the same grid and keeps the boxes inside its own square, so a yard on a boundary is not
doubled and not cut in half. Deterministic (hashed yaw, colour and which boxes stay).
"""
import json
import logging
import math

import numpy as np

log = logging.getLogger("earthd.worldprops")

import mapgeom

# Measured vertex bounds (Z up, metres). Lift is how far the mesh origin sits above the ground.
# The catalog height is the mesh's vertical size, and the row sends that height so the Forest scale is 1.
CELL_H, CELL_LIFT = 28.93, 0.02
# industrial_watertower: the legs rise out of concrete pads whose tops are at z=0.60. A centre pipe
# reaches -1.29; lifting by that pipe left the feet in the air. Dropping to the pad tops puts the
# visible feet on the ground and buries the pads.
WATER_H, WATER_LIFT = 26.16, -0.60
CONT_H = 2.59
# dragbillboard: z 0..11.95, the poster centred near the origin and facing local +y.
BILL_H, BILL_LIFT = 11.95, 0.0
BILL_OX, BILL_OY = 0.02, 0.14          # the poster's centre, in the mesh's local axes
# farm_barn: the walls sit on z=0. A foundation reaches -0.45; lifting by it left the walls a few feet up.
BARN_H, BARN_LEN, BARN_LIFT = 8.41, 10.36, 0.0
BARN_SCALE = (0.7, 1.75)               # outside this the procedural barn fits better than a scaled mesh

CELL_CLASSES = {"communication_tower", "mobile_phone_tower", "radar"}
WATER_CLASSES = {"water_tower"}
YARD_CLASSES = {"industrial", "works"}
FARMYARD_CLASS = "farmyard"

CONT_ALONG_M, CONT_ACROSS_M = 16.0, 6.5
CONT_KEEP = 0.55
CONT_YARD_MAX = {"industrial": 8, "works": 8, "farmyard": 3}
CONT_AREA = {"industrial": (800.0, 80000.0), "works": (800.0, 80000.0), "farmyard": (400.0, 15000.0)}
CONT_TILE_MAX = 30
CONT_ROAD_M = 8.0
BILL_ROAD_M = 120.0
ROAD_MARGIN_M = 140.0


def tile_props(frame, tx, ty, md, elev):
    """(rows, barn ids) of road tile (tx, ty). barn ids are the footprints replaced by the stock barn
    (buildings.road_tile_items skip). elev(lat, lon) is the ground. md None, or a cell that cannot be
    read, yields nothing of that layer."""
    import buildings
    import roads
    if md is None:
        return [], set()
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    keys = roads.cells_of_tile(frame, tx, ty)
    infra = _layer(md, keys, "infrastructure")
    land = _layer(md, keys, "land_use")
    blds = _layer(md, keys, "buildings")
    roads_xy = _polylines(frame, _layer(md, roads.cells_of_tile(frame, tx, ty, margin=ROAD_MARGIN_M), "roads"))
    bld_rings = _building_rings(frame, blds)
    placed, skip, boxes = [], set(), []
    for feats in infra:
        _towers(frame, feats, (x0, y0, x1, y1), placed)
        _billboards(frame, feats, (x0, y0, x1, y1), roads_xy, placed)
    for feats in land:
        _containers(frame, feats, (x0, y0, x1, y1), bld_rings, roads_xy, boxes)
        _billboards(frame, feats, (x0, y0, x1, y1), roads_xy, placed)
    osm = getattr(md, "billboard_osm", None)
    if osm is not None:
        try:
            for feats in osm.cells(keys) or ():
                if feats is not None and len(feats):
                    _billboards(frame, feats, (x0, y0, x1, y1), roads_xy, placed)
        except Exception as ex:
            log.warning("billboards: %s: %s", type(ex).__name__, ex)
    for feats in blds:
        _barns(frame, feats, (x0, y0, x1, y1), placed, skip)
    boxes.sort(key=lambda b: (b["x"], b["y"]))
    placed.extend(boxes[:CONT_TILE_MAX])
    if not placed:
        return [], skip
    lat = np.array([p["lat"] for p in placed], np.float64)
    lon = np.array([p["lon"] for p in placed], np.float64)
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    z = np.asarray(z, np.float64)
    rows = []
    for p, zz in zip(placed, z):
        rows.append([p["group"], round(p["x"], 2), round(p["y"], 2), round(float(zz) + p["lift"], 2),
                     round(p["yaw"], 1), 1.0, round(p["h"], 2), round(p["rank"], 4)])
    return rows, skip


def _layer(md, keys, name):
    out, seen = [], set()
    for key in keys:
        try:
            feats = md.cell(key, layers=(name,), kind="demand").layers.get(name)
        except Exception:
            continue
        if feats is None or len(getattr(feats, "kind", ())) == 0:
            continue
        ids = getattr(feats, "ids", None)
        if ids is None:
            out.append(feats)
            continue
        keep = [i for i in range(len(feats.kind)) if str(ids[i]) not in seen]
        for i in keep:
            seen.add(str(ids[i]))
        if len(keep) == len(feats.kind):
            out.append(feats)
        elif keep:
            out.append(_take(feats, keep))
    return out


def _take(feats, rows):
    """feats with only these rows (a cell overlaps its neighbour: a feature is placed once)."""
    rows = np.asarray(rows, np.int64)
    if isinstance(feats, mapgeom.Features):
        return feats.take(rows) if hasattr(feats, "take") else _take_features(feats, rows)
    return feats


def _take_features(feats, rows):
    """A Features of the selected rows. Built from the parts, so a neighbour cell's copy is the same."""
    ids, geoms = [], []
    for i in rows:
        geoms.append((int(feats.kind[i]), feats.parts(int(i))))
        ids.append(feats.ids[i])
    out = mapgeom.build(feats.layer, np.array(ids), geoms)
    for k, v in feats.attrs.items():
        out.attrs[k] = np.asarray(v)[rows]
    return out


def _cls(feats):
    cls = feats.attrs.get("class")
    return None if cls is None else np.asarray(cls).astype(str)


def _tags_of(feats, i):
    tags = feats.attrs.get("tags")
    if tags is None or i >= len(tags):
        return {}
    val = tags[i]
    if isinstance(val, dict):
        return val
    if val is None or (isinstance(val, float) and not np.isfinite(val)):
        return {}
    text = str(val).strip()
    if not text or text == "nan":
        return {}
    try:
        got = json.loads(text)
    except Exception:
        return {}
    return got if isinstance(got, dict) else {}


def _advertising(tags):
    for k, v in tags.items():
        if str(k).strip().lower() == "advertising" and str(v).strip().lower() == "billboard":
            return True
    return False


def _hash(fid, *more):
    import buildings
    return buildings._hash01(str(fid), *more)


def _ring_xy(feats, i, frame):
    parts = feats.parts(int(i))
    if not parts or not parts[0] or len(parts[0][0]) == 0:
        return None
    ring = np.asarray(parts[0][0], np.float64)
    e, n = mapgeom.enu_xy(feats, frame, ring)
    xy = np.column_stack([np.asarray(e, np.float64), np.asarray(n, np.float64)])
    if len(xy) >= 2 and np.allclose(xy[0], xy[-1]):
        xy = xy[:-1]
    return xy if len(xy) else None


def _centre(xy):
    return float(xy[:, 0].mean()), float(xy[:, 1].mean())


def _inside(x, y, box):
    x0, y0, x1, y1 = box
    return x0 <= x < x1 and y0 <= y < y1


def _ll(frame, x, y):
    la, lo, _ = frame.enu_to_geodetic(np.array([x], np.float64), np.array([y], np.float64), np.zeros(1))
    return float(la[0]), float(lo[0])


def _towers(frame, feats, box, placed):
    cls = _cls(feats)
    if cls is None:
        return
    ids = getattr(feats, "ids", None)
    for i in range(len(feats.kind)):
        kind = str(cls[i])
        if kind in CELL_CLASSES:
            group, h, lift = "celltower", CELL_H, CELL_LIFT
        elif kind in WATER_CLASSES:
            group, h, lift = "watertower", WATER_H, WATER_LIFT
        else:
            continue
        if int(feats.kind[i]) not in (mapgeom.POINT, mapgeom.POLYGON):
            continue
        xy = _ring_xy(feats, i, frame)
        if xy is None:
            continue
        x, y = _centre(xy)
        if not _inside(x, y, box):
            continue
        fid = ids[i] if ids is not None else (round(x, 1), round(y, 1))
        la, lo = _ll(frame, x, y)
        placed.append({"group": group, "x": x, "y": y, "lat": la, "lon": lo, "lift": lift, "h": h,
                       "yaw": _hash(fid, "yaw") * 360.0, "rank": 0.0})


def _billboards(frame, feats, box, roads_xy, placed):
    ids = getattr(feats, "ids", None)
    if feats.attrs.get("tags") is None:
        return
    for i in range(len(feats.kind)):
        if int(feats.kind[i]) not in (mapgeom.POINT, mapgeom.POLYGON):
            continue
        if not _advertising(_tags_of(feats, i)):
            continue
        xy = _ring_xy(feats, i, frame)
        if xy is None:
            continue
        x, y = _centre(xy)
        if not _inside(x, y, box):
            continue
        fid = ids[i] if ids is not None else (round(x, 1), round(y, 1))
        delta = _road_delta(np.array([x, y]), roads_xy, BILL_ROAD_M)
        if delta is None:
            yaw = _hash(fid, "bill") * 360.0
        else:
            import furniture
            # local +x turns toward yaw, and this poster's face is local +y, so aim +y at the road
            yaw = furniture._yaw(float(delta[0]), float(delta[1])) - 90.0
        ox, oy = _local_offset(yaw, BILL_OX, BILL_OY)
        x, y = x - ox, y - oy
        if any(p["group"] == "billboard" and (p["x"] - x) ** 2 + (p["y"] - y) ** 2 < 9.0 for p in placed):
            continue
        la, lo = _ll(frame, x, y)
        placed.append({"group": "billboard", "x": x, "y": y, "lat": la, "lon": lo, "lift": BILL_LIFT,
                       "h": BILL_H, "yaw": yaw, "rank": _hash(fid, "face") * 0.3})


def _local_offset(yaw_deg, ox, oy):
    """World offset of local (ox, oy) at yaw (trees.lua: local +x turns toward yaw, +y is +x rotated 90° CCW)."""
    t = math.radians(yaw_deg)
    c, s = math.cos(t), math.sin(t)
    return ox * c + oy * -s, ox * s + oy * c


def _barns(frame, feats, box, placed, skip):
    import buildings
    cls = _cls(feats)
    if cls is None:
        return
    ids = getattr(feats, "ids", None)
    want = buildings.BARN_CLASSES
    for i in range(len(feats.kind)):
        if str(cls[i]) not in want or int(feats.kind[i]) != mapgeom.POLYGON:
            continue
        xy = _ring_xy(feats, i, frame)
        if xy is None or len(xy) < 3:
            continue
        x, y = _centre(xy)
        if not _inside(x, y, box):
            continue
        try:
            _, u, hl, hw = buildings.oriented_box(xy)
        except Exception:
            continue
        scale = float(2.0 * hl) / BARN_LEN
        if not (BARN_SCALE[0] <= scale <= BARN_SCALE[1]):
            continue
        import furniture
        la, lo = _ll(frame, x, y)
        placed.append({"group": "barn", "x": x, "y": y, "lat": la, "lon": lo, "lift": BARN_LIFT * scale,
                       "h": BARN_H * scale, "yaw": furniture._yaw(float(u[0]), float(u[1])), "rank": 0.0})
        if ids is not None:
            skip.add(ids[i])
            skip.add(str(ids[i]))


def _containers(frame, feats, box, bld_rings, roads_xy, placed):
    import buildings
    from polygon import Path
    cls = _cls(feats)
    if cls is None:
        return
    ids = getattr(feats, "ids", None)
    x0, y0, x1, y1 = box
    for i in range(len(feats.kind)):
        kind = str(cls[i])
        if kind not in CONT_AREA or int(feats.kind[i]) != mapgeom.POLYGON:
            continue
        xy = _ring_xy(feats, i, frame)
        if xy is None or len(xy) < 3:
            continue
        if xy[:, 0].max() < x0 or xy[:, 0].min() >= x1 or xy[:, 1].max() < y0 or xy[:, 1].min() >= y1:
            continue
        area = _area(xy)
        lo, hi = CONT_AREA[kind]
        if not (lo <= area <= hi):
            continue
        try:
            c, u, hl, hw = buildings.oriented_box(xy)
        except Exception:
            continue
        fid = ids[i] if ids is not None else (round(float(c[0]), 1), round(float(c[1]), 1))
        v = np.array([-float(u[1]), float(u[0])])
        import furniture
        yaw = furniture._yaw(float(v[0]), float(v[1]))
        path = Path(xy)
        pts = []
        ia = range(-int(hl / CONT_ALONG_M), int(hl / CONT_ALONG_M) + 1)
        ib = range(-int(hw / CONT_ACROSS_M), int(hw / CONT_ACROSS_M) + 1)
        for a in ia:
            for b in ib:
                if _hash(fid, int(a), int(b), "keep") >= CONT_KEEP:
                    continue
                p = c + u * (a * CONT_ALONG_M) + v * (b * CONT_ACROSS_M)
                pts.append(p)
        if not pts:
            continue
        pxy = np.asarray(pts, np.float64)
        keep = path.contains_points(pxy)
        keep &= ~_in_rings(pxy, bld_rings)
        keep &= ~_near_roads(pxy, roads_xy, CONT_ROAD_M)
        keep &= (pxy[:, 0] >= x0) & (pxy[:, 0] < x1) & (pxy[:, 1] >= y0) & (pxy[:, 1] < y1)
        pxy = pxy[keep]
        if len(pxy) == 0:
            continue
        order = np.argsort(np.sum((pxy - c) ** 2, axis=1))
        pxy = pxy[order[:CONT_YARD_MAX[kind]]]
        for p in pxy:
            la, lo = _ll(frame, float(p[0]), float(p[1]))
            placed.append({"group": "container", "x": float(p[0]), "y": float(p[1]), "lat": la, "lon": lo,
                           "lift": 0.0, "h": CONT_H, "yaw": yaw,
                           "rank": _hash(fid, round(float(p[0]), 1), round(float(p[1]), 1), "tone") * 0.3})


def _area(xy):
    x, y = xy[:, 0], xy[:, 1]
    return abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) * 0.5


def _building_rings(frame, layers):
    rings = []
    for feats in layers:
        cls = _cls(feats)
        for i in range(len(feats.kind)):
            if int(feats.kind[i]) != mapgeom.POLYGON:
                continue
            if cls is not None and str(cls[i]) in ("roof",):
                continue
            xy = _ring_xy(feats, i, frame)
            if xy is not None and len(xy) >= 3:
                rings.append(xy)
    return rings


def _in_rings(pts, rings):
    from polygon import Path
    hit = np.zeros(len(pts), bool)
    if not len(pts):
        return hit
    for ring in rings:
        if ring[:, 0].max() < pts[:, 0].min() or ring[:, 0].min() > pts[:, 0].max():
            continue
        if ring[:, 1].max() < pts[:, 1].min() or ring[:, 1].min() > pts[:, 1].max():
            continue
        hit |= np.asarray(Path(ring).contains_points(pts), bool)
        if hit.all():
            return hit
    return hit


def _polylines(frame, layers):
    lines = []
    for feats in layers:
        for i in range(len(feats.kind)):
            if int(feats.kind[i]) != mapgeom.LINE:
                continue
            for part in feats.parts(int(i)):
                for ring in part:
                    ring = np.asarray(ring, np.float64)
                    if len(ring) < 2:
                        continue
                    e, n = mapgeom.enu_xy(feats, frame, ring)
                    lines.append(np.column_stack([np.asarray(e, np.float64), np.asarray(n, np.float64)]))
    return lines


def _near_roads(pts, lines, dist):
    hit = np.zeros(len(pts), bool)
    if not len(pts) or not lines:
        return hit
    lim = dist * dist
    for poly in lines:
        if len(poly) < 2:
            continue
        if poly[:, 0].max() < pts[:, 0].min() - dist or poly[:, 0].min() > pts[:, 0].max() + dist:
            continue
        if poly[:, 1].max() < pts[:, 1].min() - dist or poly[:, 1].min() > pts[:, 1].max() + dist:
            continue
        a, b = poly[:-1], poly[1:]
        for i in range(len(a)):
            ab = b[i] - a[i]
            L2 = float(np.dot(ab, ab)) or 1e-9
            t = np.clip(((pts - a[i]) @ ab) / L2, 0.0, 1.0)
            proj = a[i] + t[:, None] * ab
            hit |= np.sum((pts - proj) ** 2, axis=1) < lim
            if hit.all():
                return hit
    return hit


def _road_delta(pt, lines, max_m):
    """The vector from pt to the nearest point of a road within max_m, or None."""
    best = max_m * max_m
    delta = None
    for poly in lines:
        if len(poly) < 2:
            continue
        a, b = poly[:-1], poly[1:]
        ab = b - a
        L2 = np.maximum(np.sum(ab * ab, axis=1), 1e-9)
        t = np.clip(np.sum((pt - a) * ab, axis=1) / L2, 0.0, 1.0)
        proj = a + t[:, None] * ab
        d = proj - pt
        d2 = np.sum(d * d, axis=1)
        k = int(np.argmin(d2))
        if d2[k] < best and d2[k] > 0.25:
            best = float(d2[k])
            delta = d[k]
    return delta
