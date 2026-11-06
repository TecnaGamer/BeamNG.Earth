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
HEDGE_STEP_M = 1.15
# The Forest instance scales the stock shrub uniformly, including its crown.
HEDGE_CROWN_PER_M = 1.0
HEDGE_ROAD_MARGIN_M = .15
HEDGE_SHIFT_M = 2.5
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
        return G_HEDGE, *centre, _yaw(*direction), 1.8 + .18 * math.sin(float(centre.sum()) * .7), 0.
    return ("fr_fence_" + style, *centre, _yaw(*direction),
            FR_HEIGHTS[style] * length / PANEL_M, 0.)


def _french_panels(style, line):
    if style == "hedge":
        # Place along the whole arc instead of one shrub per short vertex-to-vertex span.
        return ((centre, tangent, HEDGE_STEP_M) for centre, tangent in _centres(line, HEDGE_STEP_M))
    return _panels(line)


def _forest_rows(pending, z):
    def drawn_yaw(group, x, y, yaw):
        if group == G_HEDGE:
            # Rotate the stock shrub independently of the row. The road clearance
            # is calculated beforehand with the unrotated tangent and a circular crown.
            seed = (round(x * 16) * 73856093) ^ (round(y * 16) * 19349663)
            yaw += seed % 360
        return round(yaw, 1) % 360.

    return [[group, round(x, 2), round(y, 2), round(float(zz) + lift, 2), drawn_yaw(group, x, y, yaw), 1., height, 0.]
            for (group, x, y, yaw, height, lift), zz in zip(pending, z)]


def _clear_hedges(rows, carriageways):
    """Leave room for the entire crown. Recede from a roadside edge, and leave crossings open."""
    import roads

    hedges = [i for i, r in enumerate(rows) if r[0] == G_HEDGE]
    if not hedges or not carriageways:
        return rows
    segments, starts, half = roads._blocker_segments(carriageways)
    if not len(segments):
        return rows
    a, b = segments[:, :2], segments[:, 2:]
    span = np.hypot(*(b - a).T)
    half = np.repeat(half, np.diff(starts))
    keep = span > 1e-6
    a, b, half = a[keep], b[keep], half[keep]
    span = span[keep]
    direction = (b - a) / span[:, None]
    reach = half[:, None] + HEDGE_SHIFT_M + 2.5
    low, high = np.minimum(a, b) - reach, np.maximum(a, b) + reach
    adjusted = list(rows)
    for i in hedges:
        group, x, y, yaw, height, lift = rows[i]
        p = np.array([x, y])
        near = np.all((p >= low) & (p <= high), axis=1)
        if not near.any():
            continue
        u, d, s, hw = a[near], direction[near], span[near], half[near]

        def distances(q):
            along = np.clip(np.sum((q - u) * d, axis=1), 0., s)
            delta = q - (u + along[:, None] * d)
            return delta, np.hypot(*delta.T)

        radius = height * HEDGE_CROWN_PER_M + HEDGE_ROAD_MARGIN_M
        delta, distance = distances(p)
        gaps = distance - hw
        j = int(gaps.argmin())
        if gaps[j] >= radius:
            continue
        tangent = np.array([math.cos(math.radians(yaw)), math.sin(math.radians(yaw))])
        move = radius - gaps[j] + .06
        if gaps[j] < -.45 or move > HEDGE_SHIFT_M or abs(d[j] @ tangent) < .65:
            adjusted[i] = None
            continue
        p = p + move * delta[j] / max(distance[j], 1e-6)
        if np.any(distances(p)[1] - hw < radius):
            adjusted[i] = None
        else:
            adjusted[i] = (group, *p, yaw, height, lift)
    return adjusted


def _street_segments(carriageways):
    import europewalks
    import roads

    starts, ends, widths, local = [], [], [], []
    for _, spec, line, _ in carriageways:
        if spec.cls not in europewalks.STREETS or spec.kind != "paved" or spec.link or spec.median:
            continue
        line = roads.simplify(line, .12)[:, :2]
        if len(line) < 2:
            continue
        starts.extend(line[:-1])
        ends.extend(line[1:])
        widths.extend([spec.width / 2] * (len(line) - 1))
        local.extend([spec.cls in {"residential", "living_street", "unclassified"}] * (len(line) - 1))
    a, b = np.asarray(starts).reshape(-1, 2), np.asarray(ends).reshape(-1, 2)
    delta = b - a
    length = np.linalg.norm(delta, axis=1)
    keep = length > .01
    return a[keep], delta[keep] / length[keep, None], length[keep], np.asarray(widths)[keep], np.asarray(local)[keep]


def _street_front(ring, streets, door):
    """The nearest plausible street-facing wall, even when its house has no modelled door."""
    from buildings import _area2

    start, direction, length, half, local = streets
    ends = np.roll(ring, -1, axis=0)
    mid = (ring + ends) / 2
    edges = ends - ring
    widths = np.linalg.norm(edges, axis=1)
    along = edges / np.maximum(widths, 1e-9)[:, None]
    toward = np.c_[along[:, 1], -along[:, 0]] * (1 if _area2(ring) > 0 else -1)
    rel = mid[:, None, :] - start
    station = np.sum(rel * direction, axis=2)
    q = start + np.clip(station, 0., length)[..., None] * direction
    delta = mid[:, None, :] - q
    gap = np.linalg.norm(delta, axis=2)
    setback = gap - half
    faces = (np.sum(delta * toward[:, None, :], axis=2) <= -.57 * gap)
    parallel = np.abs(along @ direction.T) >= .58
    # A parallel street ending well before the wall is not its frontage.
    abreast = (station >= -2.5) & (station <= length + 2.5)
    eligible = faces & parallel & abreast & (setback >= 1.7) & (setback <= 22.) & (widths[:, None] >= 3.8)
    if not eligible.any():
        return None
    rank = np.where(eligible, setback - .45 * local, np.inf)
    if door is not None and door[0] < len(ring):
        rank[door[0]] -= 1.1
    edge, road = np.unravel_index(rank.argmin(), rank.shape)
    tangent = direction[road] * (1 if direction[road] @ along[edge] > 0 else -1)
    return (edge, mid[edge], along[edge], widths[edge], q[edge, road], delta[edge, road] / gap[edge, road],
            tangent, half[road], setback[edge, road], local[road])


def inferred_rows(frame, houses, rings, walks, carriageways, barriers, paved, green, drives, z_at, photo, *,
                  residential=(), public=(), driveways=()):
    """Supplement mapped French boundaries at suitable residential setbacks."""
    import furniture
    import roads
    import streetfront
    from francehouses import HOUSE_CLASSES
    from house_regions import profile

    streets = _street_segments(carriageways)
    if not houses or not len(streets[0]):
        return []
    road_bands = [(road[:, :2], spec.width / 2) for _, spec, road, _ in carriageways]
    road_blocks = roads._blocker_segments([(line, half + .30) for line, half in road_bands])
    access = [(road[:, :2], spec.width / 2 + .3) for _, spec, road, _ in carriageways if spec.cls == "service"]
    access.extend((np.asarray(d["n"])[:, :2], d["w"] / 2 + .3) for d in drives)
    access.extend((road[:, :2], spec.width / 2 + .3) for _, spec, road in driveways)
    access_blocks = roads._blocker_segments(access)
    footways = [(np.asarray(w["pts"]), w["w"] / 2) for w in walks if "pts" in w]
    foot_blocks = roads._blocker_segments([(p[:, :2], hw + .15) for p, hw in footways])
    walk_edges, corners = [], []
    for walk in walks:
        if "pts" in walk or "corner" in walk:
            curb, back = streetfront._walk_outline(walk)
            walk_edges.extend(line for line in (curb, back) if len(line) >= 2)
            if "corner" in walk:
                corners.append(np.concatenate([curb, back[::-1]]))
    walk_walls = streetfront._walls([], walk_edges)
    blocked_ground = [*paved, *public]
    exclusion = streetfront._walls(blocked_ground, barriers)
    building_walls = streetfront._walls(rings)
    corner_walls = streetfront._walls(corners)
    pending = []
    for spec, ring in houses:
        seed = spec.seed
        chance = furniture._h01("fr-yard-fence", seed)
        if chance >= .9:
            continue
        door = getattr(spec, "door", None)
        front = _street_front(ring, streets, door)
        if front is None:
            continue
        edge, mid, along, width, q, out, tangent, half, setback, local = front
        a, b = ring[edge], ring[(edge + 1) % len(ring)]
        offset = half + 1.
        curb = q + (half - .15) * out
        first = walk_walls.ray(curb, out, min(4., setback))
        if first is not None:
            second = walk_walls.ray(curb + (first + .04) * out, out, min(4.9, setback - first + .1))
            edge_of_walk = first if second is None else first + .04 + second
            offset = max(offset, half - .15 + edge_of_walk + .53)
        depth = setback + half - offset
        if not 1. <= depth <= 18.:
            continue
        centre = q + offset * out
        hidden = building_walls.ray(centre, out, depth - .45)
        if hidden is not None:
            continue
        length = min(width * abs(along @ tangent) - .4, 20.)
        ends = np.array([centre - tangent * length / 2, centre + tangent * length / 2])
        near_wall = np.array([a + .18 * (mid - a), b + .18 * (mid - b)]) - .22 * out
        yard = np.array([*ends, near_wall[1], near_wall[0]])[None]
        if (streetfront._green_overlap(yard, blocked_ground, exclusion, barriers)[0]
                or streetfront._road_overlap(yard, access_blocks)[0]):
            continue
        yard_centre = np.array([(centre + mid) * .5])
        garden = streetfront._inside(yard_centre, green)[0]
        if photo is not None:
            garden |= photo.strip_share(yard, photo.green)[0] >= .23
            if not garden and photo.strip_share(yard, photo.mask)[0] >= .71:
                continue
        if not garden:
            known_residential = streetfront._inside(yard_centre, residential)[0]
            likely_home = getattr(spec, "cls", None) in HOUSE_CLASSES or getattr(spec, "roof", None) != "flat"
            if known_residential or local:
                minimum, density = (1.15, .84) if likely_home else (1.7, .66)
            else:
                minimum, density = (1.7, .71) if likely_home else (2.7, .46)
            if depth < minimum or chance >= density:
                continue
        # The boundary itself must clear curbs, footways, existing barriers and nearby walls.
        strip = np.array([ends[0] - .36 * out, ends[1] - .36 * out,
                          ends[1] + .36 * out, ends[0] + .36 * out])[None]
        if (streetfront._road_overlap(strip, road_blocks)[0] or streetfront._road_overlap(strip, foot_blocks)[0]
                or streetfront._green_overlap(strip, rings, building_walls)[0]
                or streetfront._green_overlap(strip, blocked_ground, exclusion, barriers)[0]
                or corners and streetfront._green_overlap(strip, corners, corner_walls)[0]):
            continue
        lat, lon, _ = frame.enu_to_geodetic(*centre, 0.)
        region = profile(float(lat), float(lon))
        if region is None:
            continue
        style = ("hedge" if garden and depth > 3. and furniture._h01("fr-yard-hedge", seed) < .20
                 else french_panel("fence", {}, region, seed))
        if door is not None and door[0] == edge:
            entry = float((a + door[1] * along - centre) @ tangent)
        else:
            entry = length * (.32 if furniture._h01("fr-yard-entry", seed) < .5 else -.32)
        # An open gate stays wide enough for a car, with clearance for the shrub's crown.
        gap_half = 2.2 if style == "hedge" else 1.6
        for start, finish in ((-length / 2, entry - gap_half), (entry + gap_half, length / 2)):
            start, finish = max(start, -length / 2), min(finish, length / 2)
            if finish - start >= (.9 if style == "hedge" else .72):
                line = np.array([centre + start * tangent, centre + finish * tangent])
                pending.extend(_french_panel_row(style, p, d, panel_m)
                               for p, d, panel_m in _french_panels(style, line))
    pending = [r for r in _clear_hedges(pending, road_bands + access) if r is not None]
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
        drawn = [_french_panel_row(style, centre, direction, length) for style, centre, direction, length in fr_pending]
        bands = [(line[:, :2], spec.width / 2) for _, spec, line, _ in carriageways]
        for (style, _, _, length), bad, row in zip(fr_pending, cross, _clear_hedges(drawn, bands)):
            if row is None or (bad and style != "hedge"):
                continue
            clearance = (row[4] * HEDGE_CROWN_PER_M + .75) if style == "hedge" else 1.3 + length / 2
            if any(np.linalg.norm(np.array(row[1:3]) - gate) < clearance for gate in gates):
                continue
            pending.append(row)
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
