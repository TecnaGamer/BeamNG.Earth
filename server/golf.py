"""Pin flags on a golf course (Overture land_use, OSM golf=pin / golf=hole).

A mapped pin is a point. A hole is a line from the tee to the green, and its last vertex is the
pin (OpenStreetMap's convention). Either one gets a flag, once, when that point lies in the road
tile. A hole end within a few metres of a pin node is the same hole. The playing surfaces
themselves are painted by surface.paint_golf; this only places the flag.
"""
import hashlib
import json
import logging

import numpy as np

import mapcells
import mapgeom

log = logging.getLogger("earthd.golf")

G_FLAG = "golfflag"
FLAG_H = 2.05            # m, bne_golf_flag.dae (tools/build_golf_flag.py)
NEAR_M = 4.0             # a hole end this close to a pin node is the same flag


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


def marked(feats, i):
    """'pin' or 'hole' when feature i marks a hole, else None."""
    tags = _tags(feats, i)
    golf = tags.get("golf", "")
    cls = _cls(feats, i)
    if golf == "pin" or cls == "pin":
        return "pin"
    if golf == "hole" or cls == "hole":
        return "hole"
    return None


def _candidates(feats):
    """Rows that can mark a hole: points and lines (a land_use cell is mostly polygons) whose class is pin / hole or
    whose tags mention golf, found without parsing every feature's tags (rows runs for every road tile, worldwide)."""
    kind = np.asarray(feats.kind)
    sel = (kind == mapgeom.POINT) | (kind == mapgeom.LINE)
    if not sel.any():
        return []
    idx = np.flatnonzero(sel)
    classes = feats.attrs.get("class")
    tags = feats.attrs.get("tags")
    out = []
    for i in idx:
        c = str(classes[i]).strip().lower() if classes is not None else ""
        if c in ("pin", "hole") or (tags is not None and "golf" in str(tags[i]).lower()):
            out.append(int(i))
    return out


def _yaw(fid):
    digest = hashlib.blake2b(f"golf\0{fid}".encode(), digest_size=2).digest()
    return (int.from_bytes(digest, "little") % 3600) / 10.0


def _in_box(x, y, box):
    x0, y0, x1, y1 = box
    return x0 <= x < x1 and y0 <= y < y1


def _spot(feats, frame, i, at_end):
    """(x, y) of the feature's first vertex, or its last when at_end (a hole way's pin)."""
    parts = feats.parts(int(i))
    if not parts or not parts[0]:
        return None
    ring = np.asarray(parts[0][0], np.float64)
    need = 2 if at_end else 1
    if ring.ndim != 2 or len(ring) < need:
        return None
    ll = ring[-1 if at_end else 0]
    e, n, _ = frame.geodetic_to_enu(np.array([ll[1]]), np.array([ll[0]]), np.zeros(1))
    return float(e[0]), float(n[0])


def rows(frame, land_fn, box, elev):
    """Forest rows [group, x, y, z, yaw, scale, height, rank] of pin flags inside box.

    land_fn(cell key) is the land_use Features (the same cells the picnic tables read). A point
    tagged golf=pin, or the last vertex of a golf=hole way, is one flag."""
    if land_fn is None or elev is None:
        return []
    x0, y0, x1, y1 = (float(v) for v in box)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    try:
        keys = mapcells.cells_for_points(la, lo, pad_lat=0.00005, pad_lon=0.00005, max_cells=64)
    except (ValueError, OverflowError):
        return []
    loaded = []
    for key in keys:
        try:
            feats = land_fn(key)
        except Exception as ex:
            log.debug("golf: cell %s unavailable: %s", key, ex)
            continue
        if feats is not None and len(feats):
            loaded.append(feats)
    pins, holes = [], []
    seen = set()
    for feats in loaded:
        if feats.attrs.get("class") is None and feats.attrs.get("tags") is None:
            continue
        for i in _candidates(feats):
            mark = marked(feats, i)
            if mark is None:
                continue
            kind = int(feats.kind[i])
            at_end = kind == mapgeom.LINE and mark == "hole"
            if kind == mapgeom.POINT:
                pass
            elif at_end:
                pass
            else:
                continue
            fid = str(feats.ids[i])
            if fid in seen:
                continue
            spot = _spot(feats, frame, i, at_end)
            if spot is None or not _in_box(spot[0], spot[1], box):
                continue
            seen.add(fid)
            (pins if mark == "pin" or kind == mapgeom.POINT else holes).append((fid, spot[0], spot[1]))
    pending = list(pins)
    for fid, x, y in holes:
        if any((x - px) ** 2 + (y - py) ** 2 < NEAR_M ** 2 for _, px, py in pending):
            continue
        pending.append((fid, x, y))
    if not pending:
        return []
    xy = np.array([[p[1], p[2]] for p in pending], np.float64)
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    out = []
    for (fid, px, py), zz in zip(pending, np.asarray(z, np.float64)):
        out.append([G_FLAG, round(px, 2), round(py, 2), round(float(zz), 2), round(_yaw(fid), 1) % 360.0,
                    1.0, FLAG_H, 0.0])
    return out
