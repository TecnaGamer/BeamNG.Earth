"""Mapped chain-link fences and noise walls (Overture infrastructure lines).

OpenStreetMap's barrier=fence with fence_type=chain_link, and barrier=wall with wall=noise_barrier (or
barrier=noise_barrier), kept on the feature's class and in source_tags. Other fences and walls are left
out. Straight stock panels along the line: West Coast USA's chain-link panel (s_chainlink_old, 2.04 m,
origin at its forward end) and its concrete noise blocker (s_concrete_noiseblocker, 4 m along local +y,
origin at one end). A panel is kept when its centre is in the tile, so a line split across tiles is not
drawn twice.
"""
import json
import logging
import math

import numpy as np

import mapcells
import mapgeom

log = logging.getLogger("earthd.fences")

G_CHAIN, G_WALL = "chainlink", "noisewall"
# s_chainlink_old: x -2.02..0.02, z -0.16..1.60. The centre is 1 m behind the origin along local +x.
CHAIN_LEN, CHAIN_OX, CHAIN_H, CHAIN_LIFT = 2.04, 1.0, 1.76, 0.16
# s_concrete_noiseblocker: x 0..2.5 (thickness), y 0..4 (along the wall), z 0..2.4. Centre (1.25, 2).
WALL_LEN, WALL_ALONG, WALL_ACROSS, WALL_H = 4.0, 2.0, 1.25, 2.4
CHAIN_TYPES = {"chain_link", "chainlink", "chain-link", "chain"}
NOISE_TYPES = {"noise_barrier", "noisebarrier", "sound_barrier"}
BBOX_PAD_M = 8.0


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


def kind_of(feats, i):
    """'chainlink' or 'noisewall' when the mapped line is one, else None."""
    classes = feats.attrs.get("class")
    cls = str(classes[i]).strip().lower() if classes is not None else ""
    tags = _tags(feats, i)
    barrier = tags.get("barrier", "")
    fence_type = tags.get("fence_type") or tags.get("fence:type") or ""
    wall = tags.get("wall") or ""
    if cls in CHAIN_TYPES or barrier in CHAIN_TYPES or fence_type in CHAIN_TYPES:
        return G_CHAIN
    if cls in ("fence",) or barrier == "fence":
        return G_CHAIN if fence_type in CHAIN_TYPES else None
    if cls in NOISE_TYPES or barrier in NOISE_TYPES or wall in NOISE_TYPES:
        return G_WALL
    return None


def _yaw(dx, dy):
    """Degrees that turn a Forest item's local +x toward (dx, dy)."""
    return math.degrees(math.atan2(dy, dx)) % 360.0


def _centres(line, length):
    """(centre (2,), unit tangent) every `length` metres along a polyline, the first half a panel in."""
    line = np.asarray(line, np.float64)
    if len(line) < 2:
        return
    step = np.diff(line, axis=0)
    seglen = np.hypot(step[:, 0], step[:, 1])
    total = float(seglen.sum())
    if total < length * 0.5:
        return
    arc = np.concatenate(([0.0], np.cumsum(seglen)))
    s = length * 0.5
    while s <= total - length * 0.25:
        k = min(max(int(np.searchsorted(arc, s, side="right") - 1), 0), len(line) - 2)
        span = max(float(arc[k + 1] - arc[k]), 1e-9)
        t = (s - float(arc[k])) / span
        p = line[k] + t * (line[k + 1] - line[k])
        yield p[:2], step[k] / span
        s += length


def _place(group, centre, tangent):
    """(x, y, yaw) of one panel whose centre sits on the mapped line."""
    tx, ty = float(tangent[0]), float(tangent[1])
    cx, cy = float(centre[0]), float(centre[1])
    if group == G_CHAIN:
        return cx + CHAIN_OX * tx, cy + CHAIN_OX * ty, _yaw(tx, ty)
    # the noise wall's long axis is local +y, so local +x points to the right of the line
    return (cx - WALL_ACROSS * ty - WALL_ALONG * tx, cy + WALL_ACROSS * tx - WALL_ALONG * ty,
            _yaw(ty, -tx))


def rows(frame, infra_fn, box, elev):
    """Forest rows [group, x, y, z, yaw, scale, height, rank] of the chain-link fences and noise walls whose
    centre is inside box (x0, y0, x1, y1)."""
    if infra_fn is None:
        return []
    x0, y0, x1, y1 = map(float, box)
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
            log.debug("fences: infrastructure cell %s unavailable: %s", key, ex)
            continue
        if feats is None or not len(feats) or feats.attrs.get("class") is None and feats.attrs.get("tags") is None:
            continue
        boxes = feats.boxes(frame)
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            if fid in seen or int(feats.kind[i]) not in (mapgeom.LINE, mapgeom.POLYGON):
                continue
            group = kind_of(feats, i)
            if group is None:
                continue
            b = boxes[i]
            if b[2] < x0 - BBOX_PAD_M or b[0] > x1 + BBOX_PAD_M or b[3] < y0 - BBOX_PAD_M or b[1] > y1 + BBOX_PAD_M:
                continue
            seen.add(fid)
            length = CHAIN_LEN if group == G_CHAIN else WALL_LEN
            for part in feats.parts(i):
                if not part or len(part[0]) < 2:
                    continue
                e, n = mapgeom.enu_xy(feats, frame, np.asarray(part[0], np.float64))
                for centre, tangent in _centres(np.column_stack((e, n)), length):
                    if not (x0 <= centre[0] < x1 and y0 <= centre[1] < y1):
                        continue
                    px, py, yaw = _place(group, centre, tangent)
                    pending.append((group, px, py, yaw))
    if not pending:
        return []
    xy = np.array([[p[1], p[2]] for p in pending], np.float64)
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    out = []
    for (group, px, py, yaw), zz in zip(pending, np.asarray(z, np.float64)):
        lift = CHAIN_LIFT if group == G_CHAIN else 0.0
        height = CHAIN_H if group == G_CHAIN else WALL_H
        out.append([group, round(px, 2), round(py, 2), round(float(zz) + lift, 2), round(yaw, 1) % 360.0,
                    1.0, height, 0.0])
    return out


def tile_rows(frame, tx, ty, md, elev):
    """rows() for one road tile."""
    import roads
    box = roads.tile_bbox(tx, ty)

    def infra_fn(key):
        return md.cell(key, layers=("infrastructure",), kind="demand").layers.get("infrastructure")

    return rows(frame, infra_fn, box, elev)
