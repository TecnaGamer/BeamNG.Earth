"""Picnic tables and grills in parks.

Overture maps a picnic table as infrastructure class picnic_table (a point), and a park as land use
class park, national_park or state_park (leisure=park in source_tags). That class is not filled in the
pinned release, so the tables also come from OpenStreetMap (picnicsource.py). A picnic area is a table,
or a polygon tagged tourism=picnic_site, when it lies in a park. There is no barbecue class; a mapped
table or picnic area gets a grill beside it, and a point tagged amenity=bbq gets a grill of its own. A
park with neither is left empty. A picnic area bigger than a clearing is left to its mapped tables, so a
city park is not filled with furniture. A table or grill that lands inside a drawn building is left
out; a courtyard (a hole in the footprint) is not inside.
"""
import hashlib
import json
import logging
import math

import numpy as np

import fast
import mapcells
import mapgeom

log = logging.getLogger("earthd.picnic")

G_TABLE, G_GRILL = "picnictable", "picnicgrill"
# ut_picnic_table_a is 2.0 x 1.4 x 0.85 m, centred, on the ground (Collada centimetres, the shipped
# shape is in metres). eca_BBQ is 0.80 x 0.80 x 1.48 m; its origin is a corner, the footprint centre
# is (GRILL_CX, GRILL_CY) in local axes.
TABLE_H, GRILL_H = 0.85, 1.48
GRILL_CX, GRILL_CY = 0.326, -0.392
GRILL_OFF = 2.6                 # m from the table's centre to the grill's, along the table's local +y
PARK_CLASSES = {"park", "national_park", "state_park"}
BBQ_TAGS = {"bbq", "barbecue", "bbq_grill"}
TABLE_POLY_MAX = 80.0          # m²: a picnic_table drawn as a polygon is still one table
SITE_AREA_MAX = 4000.0         # m²: larger picnic polygons are the park itself
SITE_M = 16.0                  # m between scattered tables in a picnic area
SITE_MAX = 8
NEAR_TABLE_M = 8.0             # a scattered table this close to a mapped one is skipped


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


def is_park(feats, i):
    """A park polygon (class, subtype, or leisure=park / national_park / state_park)."""
    subtype = feats.attrs.get("subtype")
    sub = str(subtype[i]).strip().lower() if subtype is not None else ""
    leisure = _tags(feats, i).get("leisure", "")
    return _cls(feats, i) in PARK_CLASSES or sub in PARK_CLASSES or leisure in PARK_CLASSES


def marked(feats, i):
    """'table', 'site' or 'grill' when the feature is picnic furniture, else None."""
    tags = _tags(feats, i)
    amenity = tags.get("amenity", "")
    tourism = tags.get("tourism", "")
    leisure = tags.get("leisure", "")
    if _cls(feats, i) == "picnic_table" or amenity == "picnic_table" or leisure == "picnic_table":
        return "table"
    if tourism == "picnic_site" or leisure == "picnic_site":
        return "site"
    if amenity in BBQ_TAGS:
        return "grill"
    return None


def _inside(ring, x, y):
    return bool(fast._ef.point_inside(np.asarray(ring, np.float64), float(x), float(y)))


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


def _yaw_of(fid, salt):
    digest = hashlib.blake2b(f"{fid}\0{salt}".encode(), digest_size=2).digest()
    return (int.from_bytes(digest, "little") % 3600) / 10.0


def _phase(fid):
    digest = hashlib.blake2b(f"{fid}\0site".encode(), digest_size=4).digest()
    u = int.from_bytes(digest[:2], "little") / 65535.0
    v = int.from_bytes(digest[2:], "little") / 65535.0
    return u * SITE_M, v * SITE_M


def _grill_origin(cx, cy, yaw):
    """World origin of eca_BBQ so its footprint centre sits at (cx, cy)."""
    rad = math.radians(yaw)
    c, s = math.cos(rad), math.sin(rad)
    return cx - (GRILL_CX * c - GRILL_CY * s), cy - (GRILL_CX * s + GRILL_CY * c)


def _in_box(x, y, box):
    x0, y0, x1, y1 = box
    return x0 <= x < x1 and y0 <= y < y1


def _covered(covers, x, y):
    """True when (x, y) is inside a drawn building footprint and not in one of its holes."""
    for xmin, xmax, ymin, ymax, outer, holes in covers:
        if x < xmin or x > xmax or y < ymin or y > ymax:
            continue
        if not _inside(outer, x, y):
            continue
        if any(_inside(h, x, y) for h in holes):
            continue
        return True
    return False


def _drawn_building(feats, i):
    """A footprint buildings.py extrudes: not a roof, not underground, not a flat mapped area."""
    if int(feats.kind[i]) != mapgeom.POLYGON:
        return False
    if _cls(feats, i) == "roof":
        return False
    ug = feats.attrs.get("is_underground")
    if ug is not None and i < len(ug) and bool(ug[i]):
        return False
    lv = feats.attrs.get("level")
    if lv is not None and i < len(lv):
        v = lv[i]
        try:
            if np.isfinite(v) and float(v) < 0:
                return False
        except (TypeError, ValueError):
            pass
    h = feats.attrs.get("height")
    if h is not None and i < len(h):
        v = h[i]
        try:
            if np.isfinite(v) and 0 < float(v) < 0.1:   # buildings.FLAT_FOOTPRINT_M: a plaza, not a building
                return False
        except (TypeError, ValueError):
            pass
    return True


def _covers(frame, bld_fn, keys):
    """(xmin, xmax, ymin, ymax, outer, holes) of drawn building footprints in these cells."""
    import buildings
    covers, seen = [], set()
    for feats in _load(bld_fn, keys):
        ids = getattr(feats, "ids", None)
        for i in range(len(feats)):
            fid = str(ids[i]) if ids is not None else None
            if fid is not None and fid in seen:
                continue
            if not _drawn_building(feats, i):
                continue
            if fid is not None:
                seen.add(fid)
            for part in feats.parts(i):
                if not part or len(part[0]) < 3:
                    continue
                outer = _open(_xy_ring(feats, frame, part[0]))
                if len(outer) < 3 or _area(outer) < buildings.MIN_AREA_M2:
                    continue
                holes = []
                for hole in part[1:]:
                    if len(hole) < 3:
                        continue
                    h = _open(_xy_ring(feats, frame, hole))
                    if len(h) >= 3:
                        holes.append(h)
                covers.append((float(outer[:, 0].min()), float(outer[:, 0].max()),
                               float(outer[:, 1].min()), float(outer[:, 1].max()), outer, holes))
    return covers


def _xy_ring(feats, frame, ring):
    e, n = mapgeom.enu_xy(feats, frame, np.asarray(ring, np.float64))
    return np.column_stack((np.asarray(e, np.float64), np.asarray(n, np.float64)))


def _emit(fid, x, y, table, grill, covers):
    """Forest items of this spot, dropping a table or grill that sits inside a building."""
    keep_table = bool(table) and not _covered(covers, x, y)
    grill_at = None
    if grill:
        if table:
            rad = math.radians(_yaw_of(fid, "table"))
            gx = x + GRILL_OFF * -math.sin(rad)
            gy = y + GRILL_OFF * math.cos(rad)
        else:
            gx, gy = x, y
        if not _covered(covers, gx, gy):
            grill_at = (gx, gy)
    if keep_table:
        return _furniture(fid, x, y, True, grill_at is not None)
    if grill_at is None:
        return []
    return _furniture(fid, grill_at[0], grill_at[1], False, True)


def _in_park(parks, x, y):
    for ring in parks:
        if x < ring[:, 0].min() or x > ring[:, 0].max() or y < ring[:, 1].min() or y > ring[:, 1].max():
            continue
        if _inside(ring, x, y):
            return True
    return False


def _outers(feats, frame, i):
    """Outer rings of feature i in frame, each (N, 2)."""
    out = []
    for part in feats.parts(i):
        if not part or len(part[0]) < 1:
            continue
        e, n = mapgeom.enu_xy(feats, frame, np.asarray(part[0], np.float64))
        ring = np.column_stack((e, n))
        if len(ring) >= 1:
            out.append(ring)
    return out


def _scatter(ring, box, phase, limit):
    """Lattice points of the ring that fall in the tile, at most `limit`, spread across the area."""
    ring = _open(ring)
    if len(ring) < 3:
        return []
    x0, y0, x1, y1 = (float(v) for v in box)
    mn, mx = ring.min(0), ring.max(0)
    ax, ay = max(float(mn[0]), x0), max(float(mn[1]), y0)
    bx, by = min(float(mx[0]), x1), min(float(mx[1]), y1)
    if bx <= ax or by <= ay:
        return []

    def start(a, p):
        s = p + math.floor((a - p) / SITE_M) * SITE_M
        if s < a:
            s += SITE_M
        return s

    pts = []
    x = start(ax, phase[0])
    while x < bx:
        y = start(ay, phase[1])
        while y < by:
            if _inside(ring, x, y):
                pts.append((x, y))
            y += SITE_M
        x += SITE_M
    if len(pts) > limit:
        step = len(pts) / limit
        pts = [pts[int(i * step)] for i in range(limit)]
    return pts


def _furniture(fid, x, y, table, grill):
    """(group, x, y, yaw) of a table and the grill beside it, or a grill alone."""
    yaw = _yaw_of(fid, "table" if table else "grill")
    out = []
    if table:
        out.append((G_TABLE, x, y, yaw))
    if grill:
        rad = math.radians(yaw)
        gx = x + (GRILL_OFF * -math.sin(rad) if table else 0.0)
        gy = y + (GRILL_OFF * math.cos(rad) if table else 0.0)
        gyaw = (yaw + 90.0) % 360.0 if table else yaw
        ox, oy = _grill_origin(gx, gy, gyaw)
        out.append((G_GRILL, ox, oy, gyaw))
    return out


def _load(fn, keys):
    found = []
    if fn is None:
        return found
    for key in keys:
        try:
            feats = fn(key)
        except Exception as ex:
            log.debug("picnic: cell %s unavailable: %s", key, ex)
            continue
        if feats is not None and len(feats):
            found.append(feats)
    return found


def rows(frame, infra_fn, land_fn, box, elev, osm_fn=None, bld_fn=None):
    """Forest rows [group, x, y, z, yaw, scale, height, rank] of picnic tables and grills whose
    mapped spot is inside box and inside a park. osm_fn(keys) is the OpenStreetMap picnic features
    of those map cells (picnicsource.Source.cells), or None. bld_fn(key) is the buildings of a map
    cell; a table or grill inside one of those footprints is left out."""
    if infra_fn is None and land_fn is None:
        return []
    x0, y0, x1, y1 = (float(v) for v in box)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    try:
        keys = mapcells.cells_for_points(la, lo, pad_lat=0.00005, pad_lon=0.00005, max_cells=64)
    except (ValueError, OverflowError):
        return []
    loaded = _load(land_fn, keys) + _load(infra_fn, keys)
    if osm_fn is not None:
        try:
            loaded.extend(f for f in (osm_fn(keys) or ()) if f is not None and len(f))
        except Exception as ex:
            log.warning("picnic tables: %s: %s", type(ex).__name__, ex)
    parks = []
    seen_park = set()
    for feats in loaded:
        if feats.attrs.get("class") is None and feats.attrs.get("tags") is None:
            continue
        for i in range(len(feats)):
            if not is_park(feats, i) or int(feats.kind[i]) != mapgeom.POLYGON:
                continue
            fid = str(feats.ids[i])
            for ring in _outers(feats, frame, i):
                if len(ring) < 3:
                    continue
                key = (fid, round(float(ring[:, 0].min()), 1), round(float(ring[:, 1].min()), 1),
                       round(float(ring[:, 0].max()), 1), round(float(ring[:, 1].max()), 1))
                if key in seen_park:
                    continue
                seen_park.add(key)
                parks.append(_open(ring))
    tables, sites, grills = [], [], []
    seen = set()
    for feats in loaded:
        if feats.attrs.get("class") is None and feats.attrs.get("tags") is None:
            continue
        boxes = feats.boxes(frame)
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            kind = int(feats.kind[i])
            mark = marked(feats, i)
            if mark is None or kind not in (mapgeom.POINT, mapgeom.POLYGON):
                continue
            b = boxes[i]
            if b[2] < x0 or b[0] >= x1 or b[3] < y0 or b[1] >= y1:
                continue
            # A point is stored whole in every cell it touches. A land-use polygon is clipped per cell,
            # so each piece is kept and the tile box stops the pieces placing the same spot twice.
            key = fid if kind == mapgeom.POINT else (
                fid, round(float(b[0]), 1), round(float(b[1]), 1), round(float(b[2]), 1), round(float(b[3]), 1))
            if key in seen:
                continue
            rings = _outers(feats, frame, i)
            if not rings:
                continue
            seen.add(key)
            if kind == mapgeom.POINT:
                spot = [(float(rings[0][0, 0]), float(rings[0][0, 1]))]
                area = 0.0
            else:
                ring = _open(rings[0])
                area = _area(ring)
                c = ring.mean(0) if len(ring) else None
                spot = [(float(c[0]), float(c[1]))] if c is not None else []
            if mark == "table" and area > TABLE_POLY_MAX:
                mark = "site" if area <= SITE_AREA_MAX else None
            if mark == "site":
                if kind == mapgeom.POINT:
                    for x, y in spot:
                        if _in_box(x, y, box) and _in_park(parks, x, y):
                            tables.append((fid, x, y))
                elif 0.0 < area <= SITE_AREA_MAX:
                    sites.append((fid, rings[0]))
                continue
            if mark is None:
                continue
            for x, y in spot:
                if not _in_box(x, y, box) or not _in_park(parks, x, y):
                    continue
                (tables if mark == "table" else grills).append((fid, x, y))
    taken = [(x, y) for _, x, y in tables]
    covers = _covers(frame, bld_fn, keys) if bld_fn is not None and (tables or sites or grills) else []
    pending = []
    for fid, x, y in tables:
        pending.extend(_emit(fid, x, y, True, True, covers))
    for fid, ring in sites:
        for x, y in _scatter(ring, box, _phase(fid), SITE_MAX):
            if not _in_park(parks, x, y):
                continue
            if any((x - tx) ** 2 + (y - ty) ** 2 < NEAR_TABLE_M ** 2 for tx, ty in taken):
                continue
            got = _emit(fid, x, y, True, True, covers)
            if not got:
                continue
            pending.extend(got)
            if any(g[0] == G_TABLE for g in got):
                taken.append((x, y))
    for fid, x, y in grills:
        pending.extend(_emit(fid, x, y, False, True, covers))
    if not pending:
        return []
    xy = np.array([[p[1], p[2]] for p in pending], np.float64)
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    out = []
    for (group, px, py, yaw), zz in zip(pending, np.asarray(z, np.float64)):
        height = TABLE_H if group == G_TABLE else GRILL_H
        out.append([group, round(px, 2), round(py, 2), round(float(zz), 2), round(yaw, 1) % 360.0,
                    1.0, height, 0.0])
    return out
