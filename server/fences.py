"""Mapped fences, hedges and noise walls (Overture infrastructure lines).

OpenStreetMap's barrier=fence with fence_type=chain_link, and barrier=wall with wall=noise_barrier (or
barrier=noise_barrier), kept on the feature's class and in source_tags. French roadside boundaries
also use regional panels. Straight stock panels elsewhere: West Coast USA's chain-link panel (s_chainlink_old, 2.04 m,
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
PANEL_M = 2.2
FR_HEIGHTS = {"limestone": .95, "dark_stone": .94, "brick": 1.05, "iron": 1.23, "mesh": 1.14,
              "wood": 1.16, "slats": 1.12, "rails": 1.19, "hedge": 1.36}
FR_PALETTES = {
    "alsace": ("wood", "mesh", "rails", "iron", "wood"),
    "north": ("brick", "brick", "rails", "mesh", "slats"),
    "loire": ("slats", "iron", "rails", "mesh", "slats"),
    "southwest": ("iron", "mesh", "iron", "rails", "wood"),
    "central": ("iron", "mesh", "wood", "rails"),
    "brittany": ("mesh", "wood", "rails", "slats"),
}
FR_WALLS = {"north": "brick", "central": "dark_stone", "alpine": "dark_stone", "brittany": "dark_stone"}


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


def kind_of(feats, i, tags=None):
    """'chainlink' or 'noisewall' when the mapped line is one, else None."""
    classes = feats.attrs.get("class")
    cls = str(classes[i]).strip().lower() if classes is not None else ""
    tags = _tags(feats, i) if tags is None else tags
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


def french_panel(cls, tags, region, fid):
    """Choose a model for an existing boundary. The regional mix fills missing appearance tags."""
    import furniture

    kind = tags.get("barrier") or cls
    if kind == "hedge":
        return "hedge"
    mat = tags.get("material", "")
    style = tags.get("fence_type") or tags.get("fence:type") or ""
    wall = tags.get("wall", "")
    if kind == "wall":
        if wall in {"retaining", "retaining_wall", "seawall", "flood_wall", "dyke", "castle_wall"}:
            return None
        if mat == "brick" or wall == "brick":
            return "brick"
        if mat in {"concrete", "plaster"}:
            return "limestone"
        return FR_WALLS.get(region, "limestone")
    if kind not in {"fence", *CHAIN_TYPES}:
        return None
    if mat == "wood" or style in {"wood", "wooden", "paling", "picket"}:
        return "wood"
    if mat in {"plastic", "pvc"} or style == "plastic":
        return "slats"
    if style in CHAIN_TYPES | {"wire", "wire_mesh", "welded_mesh", "mesh"} or kind in CHAIN_TYPES:
        return "mesh"
    if style in {"railings", "railing", "wrought_iron"} or mat in {"iron", "wrought_iron"}:
        return "iron"
    if mat in {"metal", "steel", "aluminium"}:
        return "rails"
    options = FR_PALETTES.get(region, FR_PALETTES["central"])
    return options[int(furniture._h01("fr-fence", fid) * len(options))]


def _panels(line):
    """Panel centre, axis and scale, fitting each mapped segment without extending past its ends."""
    import roads
    line = roads.simplify(np.c_[line, np.zeros(len(line))], .08)[:, :2]
    for a, b in zip(line[:-1], line[1:]):
        d = b - a
        length = float(np.linalg.norm(d))
        if length < .66:
            continue
        count = math.ceil(length / PANEL_M)
        for i in range(count):
            yield a + d * ((i + .5) / count), d / length, length / (count * PANEL_M)


def rows(frame, infra_fn, box, elev, *, french=False, carriageways=()):
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
    import geotiles

    pending, fr_pending, gates = [], [], []
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
        nearby = ((boxes[:, 2] >= x0 - BBOX_PAD_M) & (boxes[:, 0] <= x1 + BBOX_PAD_M) &
                  (boxes[:, 3] >= y0 - BBOX_PAD_M) & (boxes[:, 1] <= y1 + BBOX_PAD_M))
        classes = feats.attrs.get("class")
        for i in np.flatnonzero(nearby):
            fid = str(feats.ids[i])
            if fid in seen:
                continue
            cls = str(classes[i]).strip().lower() if classes is not None else ""
            if french and feats.kind[i] == mapgeom.POINT and cls in {"gate", "lift_gate", "swing_gate"}:
                gates.append(boxes[i, :2])
                seen.add(fid)
            if feats.kind[i] not in (mapgeom.LINE, mapgeom.POLYGON):
                continue
            tags = _tags(feats, i)
            group = kind_of(feats, i, tags)
            style = None
            if french and group != G_WALL and (cls in {"fence", "wall", "hedge", *CHAIN_TYPES}
                                               or tags.get("barrier") in {"fence", "wall", "hedge"}):
                from house_regions import profile
                lo_, la_ = feats.parts(i)[0][0][0, :2]
                region = profile(float(la_), float(lo_))
                if region is not None:
                    style = french_panel(cls, tags, region, fid)
                    heights = feats.attrs.get("height")
                    if heights is not None and heights[i] is not None and float(heights[i]) > 2.3:
                        style = None
            if group is None and style is None:
                continue
            seen.add(fid)
            length = CHAIN_LEN if group == G_CHAIN else WALL_LEN
            for part in feats.parts(i):
                if not part or len(part[0]) < 2:
                    continue
                e, n = mapgeom.enu_xy(feats, frame, np.asarray(part[0], np.float64))
                line = np.column_stack((e, n))
                if style is not None:
                    for centre, tangent, scale in _panels(line):
                        if geotiles.box_owns(box, *centre):
                            fr_pending.append((style, centre, tangent, scale))
                    continue
                for centre, tangent in _centres(line, length):
                    if not geotiles.box_owns(box, *centre):
                        continue
                    px, py, yaw = _place(group, centre, tangent)
                    height, lift = (CHAIN_H, CHAIN_LIFT) if group == G_CHAIN else (WALL_H, 0.)
                    pending.append((group, px, py, yaw, height, lift))
    if fr_pending:
        import roads
        import streetfront
        centres = np.array([row[1] for row in fr_pending])
        offsets = np.array([row[2] * row[3] * PANEL_M / 2 for row in fr_pending])
        if carriageways:
            bounds = roads._blocker_segments([(line[:, :2], max(.3, spec.width / 2 - .1))
                                              for _, spec, line, _ in carriageways])
            cross = streetfront._road_overlap(np.stack((centres - offsets, centres + offsets), axis=1), bounds)
        else:
            cross = np.zeros(len(centres), bool)
        for (style, centre, direction, scale), bad in zip(fr_pending, cross):
            if bad or any(np.linalg.norm(centre - gate) < 1.3 + scale * PANEL_M / 2 for gate in gates):
                continue
            pending.append(("fr_fence_" + style, *centre, _yaw(*direction), FR_HEIGHTS[style] * scale, 0.))
    if not pending:
        return []
    xy = np.array([[p[1], p[2]] for p in pending], np.float64)
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    out = []
    for (group, px, py, yaw, height, lift), zz in zip(pending, np.asarray(z, np.float64)):
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
