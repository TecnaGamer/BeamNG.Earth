"""Measured Polish roadside signs. Coordinates are WGS84; absolute bearings face the viewer."""
from collections import defaultdict
from functools import lru_cache
import hashlib
import json
import logging
import math
import os
from pathlib import Path

import numpy as np
import plmapillary
from roadsigns import ON

DATA = Path(__file__).with_name("data") / "pl_signs"
SOURCES = ("mapillary", "wroclaw", "osm")
HEIGHT = 2.75
REACH = 24.0


def group(code):
    return "pl_" + code.lower().replace("-", "_").replace(".", "_")


@lru_cache(maxsize=1)
def supported_codes():
    base = tuple(json.loads((DATA / "catalog.json").read_bytes()))
    import plsignart
    user = os.environ.get("BNGE_USER_DIR")
    if not user:
        return base
    root = plsignart.folder(user)
    try:
        ready = json.loads((root / "ready.json").read_bytes())
    except (OSError, ValueError):
        return base
    inventory = json.loads((DATA / "commons.json").read_bytes())
    extra = [code for code, item in ready.items() if code in inventory and code not in base and
             item.get("sha1") == inventory[code]["sha1"] and item.get("group") == group(code) and
             (root / (group(code) + "_plate.dae")).is_file() and (root / (group(code) + "_face_256.png")).is_file()]
    return base + tuple(sorted(extra))


@lru_cache(maxsize=1)
def catalog():
    return tuple(group(c) for c in supported_codes())


@lru_cache(maxsize=1)
def signature():
    digest = hashlib.sha256()
    digest.update(repr(supported_codes()).encode())
    digest.update((DATA / "prg.npz").read_bytes())
    for name in ("catalog", "mapillary-classes", *SOURCES):
        path = DATA / (name + ".json")
        digest.update(name.encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def covers(frame, box):
    if not ON:
        return False
    x0, y0, x1, y1 = box[:4]
    la, lo, _ = frame.enu_to_geodetic(np.array([x0,x1,x0,x1]), np.array([y0,y0,y1,y1]), 0.)
    return (min(lo) < 25 and max(lo) > 14 and min(la) < 55.1 and max(la) > 48.9
            and max(lo) - min(lo) < 180 and intersects_poland((min(lo),min(la),max(lo),max(la))))


def cache_parts(frame=None, box=None, rsigns=True):
    if not rsigns or not ON:
        return ()
    if box is not None:
        if isinstance(frame, tuple):
            import geodesy
            frame = geodesy.EnuFrame(*frame)
        if not covers(frame, box):
            return ()
    return (("pl_signs", signature(), plmapillary.epoch() if plmapillary.token() else "local"),)


def cell(lon, lat):
    return math.floor(lon * 100), math.floor(lat * 100)


@lru_cache(maxsize=1)
def border_bands():
    with np.load(DATA / "prg.npz", allow_pickle=False) as archive:
        edges = archive["edges"]
    low = np.minimum(edges[:, 1], edges[:, 3])
    high = np.maximum(edges[:, 1], edges[:, 3])
    # A margin includes all edges within a metre across a band boundary.
    return {i: edges[(low < (i + 1) / 10 + 0.00002) & (high > i / 10 - 0.00002)].T
            for i in range(math.floor(low.min() * 10), math.floor(high.max() * 10) + 1)}


def inside_poland(lon, lat):
    edges = border_bands().get(math.floor(lat * 10))
    if edges is None:
        return False
    ax, ay, bx, by = edges
    k = (ay > lat) != (by > lat)
    crossing = ax[k] + (lat - ay[k]) * (bx[k] - ax[k]) / (by[k] - ay[k])
    if np.count_nonzero(crossing > lon) % 2 == 0:
        return False
    # Leave room for the whole plate and the millimetre rounding of Forest coordinates.
    # 0.00002 degrees exceeds one metre on both axes throughout Poland.
    pad = 0.00002
    nearby = ((np.minimum(ax, bx) <= lon + pad) & (np.maximum(ax, bx) >= lon - pad) &
              (np.minimum(ay, by) <= lat + pad) & (np.maximum(ay, by) >= lat - pad))
    if not np.any(nearby):
        return True
    start = np.column_stack((ax[nearby] - lon, ay[nearby] - lat))
    delta = np.column_stack((bx[nearby] - ax[nearby], by[nearby] - ay[nearby]))
    length2 = (delta * delta).sum(axis=1)
    t = np.clip(-(start * delta).sum(axis=1) / np.maximum(length2, 1e-30), 0, 1)
    return bool(np.all(((start + t[:, None] * delta) ** 2).sum(axis=1) > pad ** 2))


@lru_cache(maxsize=1024)
def intersects_poland(bounds):
    west, south, east, north = bounds
    if inside_poland((west + east) / 2, (south + north) / 2):
        return True
    for band in range(math.floor(south * 10), math.floor(north * 10) + 1):
        edges = border_bands().get(band)
        if edges is None:
            continue
        ax, ay, bx, by = edges
        if np.any((np.minimum(ax, bx) <= east) & (np.maximum(ax, bx) >= west) &
                  (np.minimum(ay, by) <= north) & (np.maximum(ay, by) >= south)):
            return True
    return False


@lru_cache(maxsize=1)
def index():
    cells = defaultdict(list)
    # Compare across cell boundaries; retain separately surveyed opposite-facing signs.
    seen = defaultdict(list)
    for source in SOURCES:
        path = DATA / (source + ".json")
        if not path.is_file():
            continue
        records = json.loads(path.read_text(encoding="utf8"))
        allowed = set(supported_codes())
        for ident, lon, lat, code, angle in records:
            if code not in allowed:
                continue
            gx, gy = math.floor(lon * 10000), math.floor(lat * 10000)
            nearby = (item for i in range(gx - 1, gx + 2) for j in range(gy - 1, gy + 2)
                      for item in seen.get((i, j, code), ()))
            def same(other):
                ox, oy, a, src = other
                if src == source or (a is not None and angle is not None and abs((angle - a + 180) % 360 - 180) > 30):
                    return False
                dx = (lon - ox) * 111320 * math.cos(math.radians(lat))
                return math.hypot(dx, (lat - oy) * 111320) < 2.0
            if any(same(item) for item in nearby):
                continue
            cells[cell(lon, lat)].append((lon, lat, group(code), angle, ident if source == "mapillary" else None))
            seen[gx, gy, code].append((lon, lat, angle, source))
    return dict(cells)


def road_segments(roads, box):
    """Ground carriageway segments near the tile, in local metres."""
    segments = []
    for _, spec, points, *_ in roads:
        xy = np.asarray(points, dtype=float)[:, :2]
        if len(xy) < 2:
            continue
        a, b = xy[:-1], xy[1:]
        low = np.minimum(a, b)
        high = np.maximum(a, b)
        mask = ((low[:, 0] <= box[2] + REACH) & (high[:, 0] >= box[0] - REACH) &
                (low[:, 1] <= box[3] + REACH) & (high[:, 1] >= box[1] - REACH))
        for start, end in zip(a[mask], b[mask]):
            v = end - start
            length = float(np.linalg.norm(v))
            if length > 0.01:
                segments.append((start, v / length, length, spec.width / 2, spec.dir if spec.oneway else 0))
    return segments


def placement(point, angle, segments, measured=False):
    """Keep a marker on the shoulder. Infer travel from its side only when a side can be resolved."""
    starts, units, lengths, widths, directions = segments
    offsets = point - starts
    along = np.clip((offsets * units).sum(axis=1), 0, lengths)
    deltas = offsets - along[:, None] * units
    squared = (deltas * deltas).sum(axis=1)
    i = squared.argmin()
    distance = math.sqrt(squared[i])
    if distance >= REACH:
        return None
    delta, unit, half, oneway = deltas[i], units[i], widths[i], directions[i]
    foot = point - delta
    right = np.array([unit[1], -unit[0]])
    lateral = float(np.dot(delta, right))
    if angle is None:
        if oneway:
            travel = unit if oneway > 0 else -unit
        elif abs(lateral) >= 0.8:
            travel = unit if lateral > 0 else -unit
        elif measured:
            travel = unit
        else:
            return None
    else:
        r = math.radians(angle)
        travel = np.array([-math.sin(r), -math.cos(r)])
    if distance < half + 0.45:
        side = lateral if abs(lateral) >= 0.8 else float(np.dot(travel, unit))
        point = foot + right * math.copysign(half + 0.45, side)
    return point, travel


def tile_rows(frame, box, roads, elev, cache_dir=None, fetcher=None):
    if not ON:
        return []
    x0, y0, x1, y1 = box
    pad = REACH
    lat, lon, _ = frame.enu_to_geodetic(np.array([x0 - pad, x0 - pad, x1 + pad, x1 + pad]),
                                        np.array([y0 - pad, y1 + pad, y0 - pad, y1 + pad]), 0.)
    west, south, east, north = min(lon), min(lat), max(lon), max(lat)
    if east - west > 180 or east < 14 or west > 25 or north < 48.9 or south > 55.1:
        return []
    a, b = cell(west, south)
    c, d = cell(east, north)
    records = [r for i in range(a, c + 1) for j in range(b, d + 1) for r in index().get((i, j), ())
               if west <= r[0] <= east and south <= r[1] <= north]
    if cache_dir and fetcher is not None and plmapillary.token() and intersects_poland((west, south, east, north)):
        import mapcells
        by_id = {r[4]: r for r in records if r[4]}
        local = [r for r in records if r[4] is None]
        for key in mapcells.cells_for_bbox(south, west, north, east):
            if not intersects_poland(key.bounds()):
                continue
            for ident, lng, lat_, value in plmapillary.fetch_cell(key, cache_dir, fetcher):
                if not (west <= lng <= east and south <= lat_ <= north):
                    continue
                code = plmapillary.mapillary_code(value)
                by_id.pop(ident, None)
                if code not in supported_codes():
                    continue
                if not inside_poland(lng, lat_):
                    continue
                name = group(code)
                match = next((r for r in local if r[2] == name and
                              math.hypot((r[0]-lng)*111320*math.cos(math.radians(lat_)),
                                         (r[1]-lat_)*111320) < 2), None)
                if match is not None:
                    local.remove(match)
                by_id[ident] = (lng, lat_, name, match[3] if match else None, ident)
        records = local + list(by_id.values())
    if not records:
        return []
    import guardrails
    import geotiles
    segments = road_segments(roads, box)
    if segments:
        segments = tuple(np.asarray(column) for column in zip(*segments))
    rows = []
    for lng, lat, name, angle, mapillary_id in records:
        east, north, _ = frame.geodetic_to_enu(lat, lng, 0.)
        point = np.array([east, north])
        pos = placement(point, angle, segments, measured=bool(mapillary_id)) if segments else None
        if pos is None and mapillary_id:
            # A sign on a cycle path can have no car road. Keep its measured point.
            # Where direction is unresolved the front faces north.
            pos = (point, (0., -1.))
        if pos is None:
            continue
        (x, y), (tx, ty) = pos
        if not geotiles.box_owns(box, float(x), float(y)):
            continue
        la, lo, _ = frame.enu_to_geodetic(np.array([x]), np.array([y]), 0.)
        if not inside_poland(float(lo[0]), float(la[0])):
            continue
        z = float(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo)))[2][0])
        fx, fy = float(ty), float(-tx)
        rows.append([guardrails.polish_groups().index(name), round(float(x), 3), round(float(y), 3), round(z, 3),
                     round(math.degrees(math.atan2(fy, fx)), 2), 0., HEIGHT, 0., fx, fy, 0., -fy, fx, 0., 0., 0., 1.])
    if rows:
        import plstock
        nearby = plstock.Nearby(rows)
        priority = guardrails.polish_groups().index("pl_a_7")
        for row in rows:
            if row[0] == priority and (post := nearby.same_post("C-12", row[1], row[2], row[4])):
                row[1:4] = [post[1], post[2], post[3] + .85]
    return rows


def road_layer(frame, box, roads, elev, md, props, guardrail_rows):
    """Add measured signs owned by this tile and localize its generated junction furniture."""
    if not covers(frame, box):
        return props
    import geotiles
    import plstock
    # Junction furniture may extend 20 m into the next tile.
    pad = 20 + plstock.MATCH_M
    window = (box[0]-pad, box[1]-pad, box[2]+pad, box[3]+pad)
    try:
        found = tile_rows(frame, window, roads, elev, getattr(md, "cache_dir", None), getattr(md, "fetcher", None))
    except (OSError, ValueError) as error:
        import revisit
        revisit.fault(here=True)
        logging.getLogger(__name__).warning("Using generated Polish junction signs: %s", error)
        found = []
    guardrail_rows.extend(r for r in found if geotiles.box_owns(box, r[1], r[2]))
    return plstock.furniture(props, frame, plstock.Nearby(found))
