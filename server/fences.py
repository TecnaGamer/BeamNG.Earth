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
G_HEDGE = "fr_hedge"
# s_chainlink_old: x -2.02..0.02, z -0.16..1.60. The centre is 1 m behind the origin along local +x.
CHAIN_LEN, CHAIN_OX, CHAIN_H, CHAIN_LIFT = 2.04, 1.0, 1.76, 0.16
# s_concrete_noiseblocker: x 0..2.5 (thickness), y 0..4 (along the wall), z 0..2.4. Centre (1.25, 2).
WALL_LEN, WALL_ALONG, WALL_ACROSS, WALL_H = 4.0, 2.0, 1.25, 2.4
CHAIN_TYPES = {"chain_link", "chainlink", "chain-link", "chain"}
NOISE_TYPES = {"noise_barrier", "noisebarrier", "sound_barrier"}
BBOX_PAD_M = 8.0
PANEL_M = 2.2
FR_HEIGHTS = {"limestone": .95, "dark_stone": .94, "brick": 1.05, "iron": 1.23, "mesh": 1.14,
              "wood": 1.16, "slats": 1.12, "rails": 1.19}
FR_PALETTES = {
    "alsace": ("wood", "mesh", "rails", "iron", "wood", "limestone"),
    "east": ("wood", "mesh", "iron", "rails", "limestone"),
    "north": ("brick", "brick", "rails", "mesh", "slats"),
    "loire": ("slats", "iron", "rails", "mesh", "slats", "limestone"),
    "southwest": ("iron", "mesh", "iron", "limestone", "wood"),
    "central": ("iron", "mesh", "wood", "rails", "dark_stone"),
    "brittany": ("mesh", "wood", "rails", "slats", "dark_stone"),
    "normandy": ("wood", "mesh", "limestone", "iron"),
    "paris_basin": ("iron", "rails", "limestone", "mesh"),
    "mediterranean": ("iron", "limestone", "slats", "mesh"),
    "alpine": ("wood", "rails", "dark_stone", "mesh"),
    "pyrenees": ("dark_stone", "wood", "iron", "mesh"),
    "basque": ("wood", "iron", "limestone", "mesh"),
    "toulouse": ("brick", "iron", "rails", "mesh"),
}
FR_WALLS = {"north": "brick", "toulouse": "brick", "central": "dark_stone", "alpine": "dark_stone",
            "brittany": "dark_stone", "pyrenees": "dark_stone"}


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


def _panels(line, spacing=PANEL_M):
    """Centre, axis and length, fitting each segment without extending past its ends."""
    import roads
    line = roads.simplify(np.c_[line, np.zeros(len(line))], .08)[:, :2]
    for a, b in zip(line[:-1], line[1:]):
        d = b - a
        length = float(np.linalg.norm(d))
        if length < .66:
            continue
        count = math.ceil(length / spacing)
        for i in range(count):
            yield a + d * ((i + .5) / count), d / length, length / count


def _french_panel_row(style, centre, direction, length):
    if style == "hedge":
        # The stock East Coast shrub has a broad crown. Use its height for a planted row and
        # overlap the foliage, so a mapped hedge reads continuously along bends and slopes.
        return G_HEDGE, *centre, _yaw(*direction), 1.22 + .18 * math.sin(float(centre.sum()) * .7), 0.
    return ("fr_fence_" + style, *centre, _yaw(*direction),
            FR_HEIGHTS[style] * length / PANEL_M, 0.)


def _french_panels(style, line):
    if style == "hedge":
        # Place along the whole arc instead of one shrub per short vertex-to-vertex span.
        return ((centre, tangent, .98) for centre, tangent in _centres(line, .98))
    return _panels(line)


def _forest_rows(pending, z):
    return [[group, round(x, 2), round(y, 2), round(float(zz) + lift, 2), round(yaw, 1) % 360.0, 1., height, 0.]
            for (group, x, y, yaw, height, lift), zz in zip(pending, z)]


def inferred_rows(frame, houses, rings, walks, carriageways, barriers, paved, green, drives, z_at, photo, *,
                  residential=()):
    """Supplement mapped French boundaries at suitable residential setbacks."""
    import europewalks
    import furniture
    import roads
    import streetfront
    from house_regions import profile

    streets = [(spec, road) for _, spec, road, _ in carriageways
               if spec.cls in europewalks.STREETS and spec.kind == "paved" and not spec.link and not spec.median]
    if not houses or not streets:
        return []
    streets = [(spec, road, road[:, :2].min(axis=0) - 25., road[:, :2].max(axis=0) + 25.)
               for spec, road in streets]
    road_blocks = roads._blocker_segments([(road[:, :2], spec.width / 2 + .30)
                                          for _, spec, road, _ in carriageways])
    access = [(road[:, :2], spec.width / 2 + .3) for _, spec, road, _ in carriageways if spec.cls == "service"]
    access.extend((np.asarray(d["n"])[:, :2], d["w"] / 2 + .3) for d in drives)
    access_blocks = roads._blocker_segments(access)
    footways = [(np.asarray(w["pts"]), w["w"] / 2) for w in walks if "pts" in w]
    foot_blocks = roads._blocker_segments([(p[:, :2], hw + .15) for p, hw in footways])
    footways = [(p, hw, p[:, :2].min(axis=0) - 8., p[:, :2].max(axis=0) + 8.) for p, hw in footways]
    exclusion = streetfront._walls(paved, barriers)
    building_walls = streetfront._walls(rings)
    pending = []
    for spec, ring in houses:
        seed = spec.seed
        chance = furniture._h01("fr-yard-fence", seed)
        if chance >= .72:
            continue
        edge, door = spec.door
        a, b = ring[edge], ring[(edge + 1) % len(ring)]
        mid = (a + b) / 2
        width = float(np.linalg.norm(b - a))
        if not 5.0 <= width <= 22.:
            continue
        along = (b - a) / width
        nearest = None
        for road_spec, line, lo, hi in streets:
            if np.any(mid < lo) or np.any(mid > hi):
                continue
            p, v = furniture._at(line, furniture._project(line, mid[None]))
            gap = float(np.linalg.norm(mid - p[0, :2]))
            if nearest is None or gap < nearest[0]:
                nearest = gap, p[0, :2], v[0], road_spec.width / 2
        if nearest is None:
            continue
        gap, kerb, tangent, half = nearest
        if not half + 2.7 < gap < half + 20. or abs(float(tangent @ along)) < .69:
            continue
        out = (mid - kerb) / gap
        offset = half + 1.
        for line, hw, lo, hi in footways:
            if np.any(kerb < lo) or np.any(kerb > hi):
                continue
            q, _ = furniture._at(line, furniture._project(line, kerb[None]))
            delta = q[0, :2] - kerb
            if half - .25 < float(delta @ out) < half + 4. and np.linalg.norm(delta - (delta @ out) * out) < 2.:
                offset = max(offset, float(delta @ out) + hw + .55)
        if not 1.8 <= gap - offset <= 15.:
            continue
        centre = kerb + offset * out
        tangent = tangent if tangent @ along > 0 else -tangent
        length = min(width - .6, 16.)
        ends = np.array([centre - tangent * length / 2, centre + tangent * length / 2])
        front = np.array([a + .18 * (mid - a), b + .18 * (mid - b)]) - .24 * out
        yard = np.array([*ends, front[1], front[0]])[None]
        if (streetfront._green_overlap(yard, paved, exclusion, barriers)[0]
                or streetfront._road_overlap(yard, access_blocks)[0]):
            continue
        # The boundary itself must clear the road, the sidewalk and all neighbouring buildings.
        strip = np.array([ends[0] - .38 * out, ends[1] - .38 * out,
                          ends[1] + .38 * out, ends[0] + .38 * out])[None]
        if (streetfront._road_overlap(strip, road_blocks)[0] or streetfront._road_overlap(strip, foot_blocks)[0]
                or streetfront._green_overlap(strip, rings, building_walls)[0]):
            continue
        yard_centre = np.array([(centre + mid) * .5])
        garden = streetfront._inside(yard_centre, green)[0]
        if photo is not None:
            garden |= photo.strip_share(yard, photo.green)[0] >= .23
            if not garden and photo.strip_share(yard, photo.mask)[0] >= .64:
                continue
        if not garden:
            known_residential = streetfront._inside(yard_centre, residential)[0]
            if gap - offset < (3.2 if known_residential else 4.3) or chance >= (.52 if known_residential else .38):
                continue
        lat, lon, _ = frame.enu_to_geodetic(*centre, 0.)
        region = profile(float(lat), float(lon))
        if region is None:
            continue
        style = ("hedge" if garden and furniture._h01("fr-yard-hedge", seed) < .20
                 else french_panel("fence", {}, region, seed))
        entry = float((a + door * along - centre) @ tangent)
        # Reserve a usable opening, including on an unmapped driveway. Keep the hedge's
        # foliage out of it as well.
        gap_half = 1.85 if style == "hedge" else 1.6
        for start, finish in ((-length / 2, entry - gap_half), (entry + gap_half, length / 2)):
            start, finish = max(start, -length / 2), min(finish, length / 2)
            if finish - start >= (.9 if style == "hedge" else .72):
                line = np.array([centre + start * tangent, centre + finish * tangent])
                pending.extend(_french_panel_row(style, p, d, panel_m)
                               for p, d, panel_m in _french_panels(style, line))
    if not pending:
        return []
    x, y = np.array([(r[1], r[2]) for r in pending]).T
    return _forest_rows(pending, z_at(x, y))


def rows(frame, infra_fn, box, elev, *, french=False, carriageways=()):
    """Forest rows of mapped barriers whose centre is inside box (x0, y0, x1, y1)."""
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
                la_, lo_, _ = frame.enu_to_geodetic(*boxes[i].reshape(2, 2).mean(axis=0), 0.)
                region = profile(float(la_), float(lo_))
                if region is not None:
                    style = french_panel(cls, tags, region, fid)
                    heights = feats.attrs.get("height")
                    if heights is not None and heights[i] is not None:
                        try:
                            if float(heights[i]) > 2.3 and style != "hedge":
                                style = None
                        except (TypeError, ValueError):
                            pass
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
                    for centre, tangent, length_m in _french_panels(style, line):
                        if geotiles.box_owns(box, *centre):
                            fr_pending.append((style, centre, tangent, length_m))
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
        offsets = np.array([row[2] * row[3] / 2 for row in fr_pending])
        if carriageways:
            bounds = roads._blocker_segments([(line[:, :2], max(.3, spec.width / 2 - .1))
                                              for _, spec, line, _ in carriageways])
            cross = streetfront._road_overlap(np.stack((centres - offsets, centres + offsets), axis=1), bounds)
        else:
            cross = np.zeros(len(centres), bool)
        for (style, centre, direction, length), bad in zip(fr_pending, cross):
            if bad or any(np.linalg.norm(centre - gate) < 1.3 + length / 2 for gate in gates):
                continue
            pending.append(_french_panel_row(style, centre, direction, length))
    if not pending:
        return []
    xy = np.array([[p[1], p[2]] for p in pending], np.float64)
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    return _forest_rows(pending, z)


def tile_rows(frame, tx, ty, md, elev):
    """rows() for one road tile."""
    import roads
    box = roads.tile_bbox(tx, ty)

    def infra_fn(key):
        return md.cell(key, layers=("infrastructure",), kind="demand").layers.get("infrastructure")

    return rows(frame, infra_fn, box, elev)
