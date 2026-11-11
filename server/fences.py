"""Mapped chain-link fences and noise walls (Overture infrastructure lines).

OpenStreetMap's barrier=fence with fence_type=chain_link, and barrier=wall with wall=noise_barrier (or
barrier=noise_barrier), kept on the feature's class and in source_tags; stone walls (wall=dry_stone ...) as
stone meshes (stone_walls). In France with PR #505's European road details on (streetfront.country_at_tile), the
mapped fences, walls and hedges take regional panels and the stock shrub instead (french_rows), and yards get inferred
boundaries (inferred_rows). Other fences and walls are left out. Straight stock panels along the line: West Coast USA's chain-link panel (s_chainlink_old, 2.04 m,
origin at its forward end) and its concrete noise blocker (s_concrete_noiseblocker, 4 m along local +y,
origin at one end). A panel is kept when its centre is in the tile, so a line split across tiles is not
drawn twice.
"""
import json
import logging
import math
import os

import numpy as np

import mapcells
import mapgeom
from guardrails import DECK_AUTO_SNAP_TOLERANCE_M, DECK_WIDE_REACH_M, _deck_level_hint, _surface_info

log = logging.getLogger("earthd.fences")

G_CHAIN, G_WALL = "chainlink", "noisewall"
G_HEDGE = "fr_hedge"           # (PR #505: French hedges, the stock shrub)
# s_chainlink_old: x -2.02..0.02, z -0.16..1.60. The centre is 1 m behind the origin along local +x.
CHAIN_LEN, CHAIN_OX, CHAIN_H, CHAIN_LIFT = 2.04, 1.0, 1.76, 0.16
# s_concrete_noiseblocker: x 0..2.5 (thickness), y 0..4 (along the wall), z 0..2.4. Centre (1.25, 2).
WALL_LEN, WALL_ALONG, WALL_ACROSS, WALL_H = 4.0, 2.0, 1.25, 2.4
# its flat face is the full-height one at local x = 2.5; the other side has a 0.48 m base and a slope up
# (the mesh: 9.6 m2 of face at +x against 2.0 m2 at -x), so a panel is laid with local +x toward the road.
# A noise wall stands off the carriageway it runs along, not on the mapped line the way happens to have: the line
# follows the real deck's outer edge, which is a shoulder's width outside the carriageway drawn here. It is laid
# along the carriageway, WALL_EDGE_M beyond its painted edge, with the flat face toward it.
WALL_EDGE_M = 0.5           # m between the carriageway's edge and the wall's face (its inner side)
WALL_FIT_REACH_M = 20.0     # m: a wall farther from every carriageway keeps its mapped line
WALL_FIT_MAX_DEG = 40.0     # a wall this far off the carriageway's direction is not laid along it
WALL_ACROSS_DEG = 50.0      # ... and over the carriageway: never drawn, the wall must not cross the road
WALL_ON_ROAD_M = 0.3        # m past a carriageway's edge no wall panel stands, whatever its direction (GitHub #1022)
WALL_LEVEL_M = 3.0          # m: a carriageway this far above or below the wall is not the one it runs along
WALL_DECK_REACH_M = 4.0     # m past a deck's edge a bridge wall (its own layer/bridge tags) still stands on it
                            # (a road passing under a rail viaduct)
CHAIN_TYPES = {"chain_link", "chainlink", "chain-link", "chain"}
NOISE_TYPES = {"noise_barrier", "noisebarrier", "sound_barrier"}
BBOX_PAD_M = 8.0
ALONG_EDGE_M = 4.0      # m past a deck's edge an untagged barrier running along it is its parapet (ALONG_*)
ALONG_DEG = 15.0        # within this angle of the deck's direction
ALONG_FRAC = 0.7        # ... for this share of its panels
PARAPET_ON = os.environ.get("BNE_FENCE_PARAPET", "on") != "off"
CHAIN_OFF_ROAD_ON = os.environ.get("BNE_FENCE_OFF_ROAD", "on") != "off"
OUTLINE_PAD_M = 1.0     # m past a bridge outline's edge a barrier along it still counts as on it (both mapped by hand)
# PR #505 (exencendre): French boundary panels and hedges
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


def _in_outlines(rings, pts):
    """Which points lie in one of the rings ((k, 2) bridge outlines) or within OUTLINE_PAD_M of its edge."""
    from polygon import contains_points
    pts = np.asarray(pts, np.float64).reshape(-1, 2)
    m = np.zeros(len(pts), bool)
    for ring in rings:
        m |= np.asarray(contains_points(ring, pts), bool)
        a, b = ring[:-1], ring[1:]
        d = b - a
        l2 = np.maximum(np.sum(d * d, axis=1), 1e-12)
        t = np.clip(np.sum((pts[:, None, :] - a[None]) * d[None], axis=2) / l2[None], 0.0, 1.0)
        near = a[None] + t[:, :, None] * d[None]
        m |= np.hypot(pts[:, None, 0] - near[:, :, 0], pts[:, None, 1] - near[:, :, 1]).min(axis=1) <= OUTLINE_PAD_M
    return m


def _on_road(points, z, roads):
    """Which XY points (at heights z) stand on a carriageway of roads (build_tile's (fid, spec, centreline (k, 3), _))
    at their own level (WALL_LEVEL_M): within its half width + WALL_ON_ROAD_M. Pure."""
    hit = np.zeros(len(points), bool)
    for r in roads:
        try:
            pts = np.asarray(r[2] if len(r) > 2 else r, np.float64)
            wide = float(getattr(r[1] if len(r) > 1 else None, "width", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if pts.ndim != 2 or pts.shape[1] < 3 or len(pts) < 2 or wide <= 0:
            continue
        a, b = pts[:-1, :2], pts[1:, :2]
        d = b - a
        l2 = np.maximum((d * d).sum(1), 1e-12)
        v = points[:, None, :] - a[None, :, :]
        t = np.clip((v * d[None]).sum(2) / l2[None], 0.0, 1.0)
        q = a[None, :, :] + t[:, :, None] * d[None, :, :]
        dm = np.hypot(q[:, :, 0] - points[:, None, 0], q[:, :, 1] - points[:, None, 1])
        k = np.argmin(dm, axis=1)
        rr = np.arange(len(points))
        zq = pts[k, 2] + t[rr, k] * (pts[k + 1, 2] - pts[k, 2])
        hit |= (dm[rr, k] < wide / 2.0 + WALL_ON_ROAD_M) & (np.abs(zq - z) <= WALL_LEVEL_M)
    return hit


def _along_deck(points, tangents, decks):
    """(distance from the nearest deck's centreline, its half width, |cos| of the angle between tangents and the
    deck there) for XY points (inf, 0, 0 with no deck within its half width + DECK_WIDE_REACH_M). Pure."""
    n = len(points)
    dist, half, cosa = np.full(n, np.inf), np.zeros(n), np.zeros(n)
    for deck in decks or ():
        line = np.asarray(deck.get("n", ()), np.float64)
        if line.ndim != 2 or len(line) < 2:
            continue
        a, b = line[:-1, :2], line[1:, :2]
        d = b - a
        l2 = np.maximum((d * d).sum(1), 1e-12)
        v = points[:, None, :] - a[None, :, :]
        t = np.clip((v * d[None]).sum(2) / l2[None], 0.0, 1.0)
        q = a[None, :, :] + t[:, :, None] * d[None, :, :]
        dm = np.hypot(q[:, :, 0] - points[:, None, 0], q[:, :, 1] - points[:, None, 1])
        k = np.argmin(dm, axis=1)
        rr = np.arange(n)
        dk = dm[rr, k]
        h = float(deck.get("w", 0.0) or 0.0) / 2.0
        take = (dk < dist) & (dk <= h + DECK_WIDE_REACH_M)
        if take.any():
            seg = d[k] / np.sqrt(l2[k])[:, None]
            c = np.abs((seg * tangents).sum(1))
            dist[take], half[take], cosa[take] = dk[take], h, c[take]
    return dist, half, cosa


# PR #505 (exencendre): French boundaries
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



def french_rows(frame, infra_fn, box, elev, carriageways=()):
    """(rows, fids) of PR #505's French mapped boundaries in box: fences (chain-link ones too), garden walls and hedges
    as regional panels / the stock shrub (french_panel), clear of the carriageways and of mapped gates; fids: the
    features drawn so (rows() leaves them out). Noise walls, the stone walls stone_walls draws and anything on a bridge
    (its tags or Overture's level) are left to the other passes."""
    if infra_fn is None:
        return [], set()
    import geotiles
    from house_regions import profile
    x0, y0, x1, y1 = map(float, box)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    try:
        keys = mapcells.cells_for_points(la, lo, pad_lat=0.00005, pad_lon=0.00005, max_cells=64)
    except (ValueError, OverflowError):
        return [], set()
    fr_pending, gates, taken, seen = [], [], set(), set()
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
            seen.add(fid)
            cls = str(classes[i]).strip().lower() if classes is not None else ""
            if feats.kind[i] == mapgeom.POINT and cls in {"gate", "lift_gate", "swing_gate"}:
                gates.append(boxes[i, :2])
                continue
            if feats.kind[i] not in (mapgeom.LINE, mapgeom.POLYGON):
                continue
            tags = _tags(feats, i)
            if not (cls in {"fence", "wall", "hedge", *CHAIN_TYPES} or tags.get("barrier") in {"fence", "wall", "hedge"}):
                continue
            if kind_of(feats, i, tags) == G_WALL or (STONE_ON and stone_height(feats, i) is not None):
                continue
            if _deck_level_hint(feats, i):
                continue
            try:
                lv = float(np.asarray(feats.attrs.get("level"))[i])
            except (TypeError, ValueError, IndexError):
                lv = 0.0
            if lv > 0:
                continue
            la_, lo_, _ = frame.enu_to_geodetic(*boxes[i].reshape(2, 2).mean(axis=0), 0.)
            region = profile(float(la_), float(lo_))
            if region is None:
                continue
            style = french_panel(cls, tags, region, fid)
            heights = feats.attrs.get("height")
            if style is not None and style != "hedge" and heights is not None and heights[i] is not None:
                try:
                    if float(heights[i]) > 2.3:
                        style = None
                except (TypeError, ValueError):
                    pass
            if style is None:
                continue
            taken.add(fid)
            for part in feats.parts(i):
                if not part or len(part[0]) < 2:
                    continue
                e, n = mapgeom.enu_xy(feats, frame, np.asarray(part[0], np.float64))
                for centre, tangent, length_m in _french_panels(style, np.column_stack((e, n))):
                    if geotiles.box_owns(box, *centre):
                        fr_pending.append((style, centre, tangent, length_m))
    if not fr_pending:
        return [], taken
    import roads
    import streetfront
    carriageways = list(carriageways or ())
    centres = np.array([row[1] for row in fr_pending])
    offsets = np.array([row[2] * row[3] / 2 for row in fr_pending])
    if carriageways:
        bounds = roads._blocker_segments([(np.asarray(line)[:, :2], max(.3, spec.width / 2 - .1))
                                          for _, spec, line, *_ in carriageways])
        cross = streetfront._road_overlap(np.stack((centres - offsets, centres + offsets), axis=1), bounds)
    else:
        cross = np.zeros(len(centres), bool)
    drawn = [_french_panel_row(style, centre, direction, length) for style, centre, direction, length in fr_pending]
    bands = [(np.asarray(line)[:, :2], spec.width / 2) for _, spec, line, *_ in carriageways]
    pending = []
    for (style, _, _, length), bad, row in zip(fr_pending, cross, _clear_hedges(drawn, bands)):
        if row is None or (bad and style != "hedge"):
            continue
        clearance = (row[4] * HEDGE_CROWN_PER_M + .75) if style == "hedge" else 1.3 + length / 2
        if any(np.linalg.norm(np.array(row[1:3]) - gate) < clearance for gate in gates):
            continue
        pending.append(row)
    if not pending:
        return [], taken
    xy = np.array([[p[1], p[2]] for p in pending], np.float64)
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    return _forest_rows(pending, z), taken


def rows(frame, infra_fn, box, elev, decks=(), roads=(), french=False, carriageways=None):
    """Forest rows [group, x, y, z, yaw, scale, height, rank] of the chain-link fences and noise walls whose
    centre is inside box (x0, y0, x1, y1). decks ([{"n": (k, 3) XYZ, "w": road width}], as guardrails.forest_rows
    takes them): a wall or fence on a bridge stands on its deck, not on the ground under it (GitHub #775: a noise
    wall tagged layer=1 on the B 29's bridge stood on the road under it and blocked it). Which panels are on a deck
    is decided as for the mapped guardrails (guardrails._deck_level_hint / _surface_info): the way's own bridge /
    layer tags, else only where the ground already agrees with the deck. A way tagged on a bridge past its road
    deck's edge but in the bridge's mapped outline (man_made=bridge, the infrastructure's class bridge) stands at
    the nearest deck's height (guardrails.DECK_WIDE_REACH_M): the Stelling Road Overcrossing's noise wall and
    chain-link fence (OSM ways 921088133 / 921088134, layer=1) run along its outline (way 921088131) about 10 m
    from the 6.6 m road deck's centreline, its sidewalks mapped as footways (no deck), and stood on the road
    under it. roads ((fid, spec, centreline (k, 3), lon), roads.build_tile's roads_out) and the decks: a noise
    wall is laid along the carriageway it runs along, WALL_EDGE_M beyond its painted edge and with its flat face
    toward it, whichever way the way is drawn (a wall farther from every carriageway than WALL_FIT_REACH_M keeps
    its mapped line; one drawn across the carriageway is left out). french (PR #505, a French tile with the European
    road details on): the fences, garden walls and hedges as french_rows draws them, the rest as here; carriageways:
    roads' other name (PR #505's)."""
    if infra_fn is None:
        return []
    if carriageways is not None and not roads:      # (PR #505's name for the same roads_out rows)
        roads = carriageways
    fr_rows, fr_taken = french_rows(frame, infra_fn, box, elev, roads) if french else ([], set())
    x0, y0, x1, y1 = map(float, box)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    try:
        keys = mapcells.cells_for_points(la, lo, pad_lat=0.00005, pad_lon=0.00005, max_cells=64)
    except (ValueError, OverflowError):
        return fr_rows
    pending = []
    seen, outlines, seen_outlines = set(fr_taken), [], set()
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
            classes = feats.attrs.get("class")
            if (decks and int(feats.kind[i]) == mapgeom.POLYGON and classes is not None
                    and str(classes[i]).strip().lower() == "bridge" and fid not in seen_outlines):
                seen_outlines.add(fid)
                for part in feats.parts(i):
                    if part and len(part[0]) >= 4:
                        e, n = mapgeom.enu_xy(feats, frame, np.asarray(part[0], np.float64))
                        outlines.append(np.column_stack((e, n)))
                continue
            group = kind_of(feats, i)
            if group is None:
                continue
            b = boxes[i]
            if b[2] < x0 - BBOX_PAD_M or b[0] > x1 + BBOX_PAD_M or b[3] < y0 - BBOX_PAD_M or b[1] > y1 + BBOX_PAD_M:
                continue
            seen.add(fid)
            hint = _deck_level_hint(feats, i) if decks else None
            if hint is None and decks:
                # Overture keeps a way's OSM layer in the feature's own level, not always in its tags
                # (rail-viaduct noise walls seen with tags without layer, level=2.0): that counts as on a bridge.
                try:
                    lv = float(np.asarray(feats.attrs.get("level"))[i])
                except (TypeError, ValueError, IndexError):
                    lv = float("nan")
                if lv == lv:
                    hint = True if lv > 0 else (False if lv < 0 else None)
            length = CHAIN_LEN if group == G_CHAIN else WALL_LEN
            for pi, part in enumerate(feats.parts(i)):
                if not part or len(part[0]) < 2:
                    continue
                e, n = mapgeom.enu_xy(feats, frame, np.asarray(part[0], np.float64))
                for centre, tangent in _centres(np.column_stack((e, n)), length):
                    px, py, yaw = _place(group, centre, tangent)
                    # (a panel is kept when its centre is in the tile; the part's panels outside it are still
                    # made, for the bridge-continuity test below, and only not drawn)
                    keep = x0 <= centre[0] < x1 and y0 <= centre[1] < y1
                    pending.append((group, px, py, yaw, float(centre[0]), float(centre[1]), hint, fid, pi,
                                    keep, float(tangent[0]), float(tangent[1])))
    if not pending:
        return fr_rows
    near = np.zeros(len(pending), bool)     # (set below: walls laid along a carriageway)
    pz = np.zeros(len(pending))             # (that carriageway's own height there)
    if roads or decks:
        # A noise wall is laid along the carriageway it runs along, WALL_EDGE_M beyond its painted edge, with
        # its flat face (the stock model's local +x) toward it. The mapped line follows the real deck's outer
        # edge, a shoulder beyond the carriageway drawn here, and a way drawn at an angle laid panels over
        # the road; the carriageway decides both.
        fits = []                   # (polyline, half width): the carriageways a wall can be laid along
        deck_fits = []              # ... the decks alone: a wall tagged on a bridge keeps its own level
        for r in roads:
            try:
                pts = np.asarray(r[2] if len(r) > 2 else r, np.float64)
                spec = r[1] if len(r) > 1 else None
            except (TypeError, ValueError):
                continue
            if pts.ndim == 2 and pts.shape[1] >= 2 and len(pts) >= 2:
                try:
                    wide = float(getattr(spec, "width", 0.0) or 0.0)
                except (TypeError, ValueError):
                    wide = 0.0
                if wide > 0:
                    fits.append((pts, wide / 2.0, False))
        for d in decks:
            pts = np.asarray(d.get("n", ()), np.float64)
            if pts.ndim == 2 and pts.shape[1] >= 2 and len(pts) >= 2:
                try:
                    wide = float(d.get("w", 0.0) or 0.0)
                except (TypeError, ValueError):
                    wide = 0.0
                if wide > 0:
                    fits.append((pts, wide / 2.0, True))
                    deck_fits.append((pts, wide / 2.0))
        walls = np.array([p[0] == G_WALL for p in pending], bool)
        if fits and walls.any():
            # A wall stands off the edge of the carriageway it runs along and is laid along it, its flat face
            # toward it. The mapped line follows the real deck's outer edge (a shoulder beyond the carriageway
            # drawn here), and a way drawn at an angle put panels across the road.
            mid = np.array([[p[4], p[5]] for p in pending], np.float64)
            row = np.arange(len(pending))
            ang = np.radians([p[3] for p in pending])
            wt = np.stack([-np.sin(ang), np.cos(ang)], axis=1)      # (wall tangent, local +y)
            par_min = math.cos(math.radians(WALL_FIT_MAX_DEG))
            inside = _in_outlines(outlines, mid) if outlines else None   # (a wall on a mapped bridge's plan)
            # The wall's own level. One tagged on a bridge stands on its deck, not on the ground under it: a
            # carriageway at another level (a road passing under a rail viaduct) is not the one it runs along.
            la, lo, _ = frame.enu_to_geodetic(mid[:, 0], mid[:, 1], np.zeros(len(mid)))
            ref = np.asarray(elev(la, lo), np.float64).reshape(-1)
            if deck_fits and any(p[6] is True for p in pending):
                dd = np.full(len(pending), np.inf)
                dz = np.zeros(len(pending))
                for line, _half in deck_fits:
                    xy = line[:, :2]
                    a, b = xy[:-1], xy[1:]
                    d = b - a
                    l2 = np.maximum((d * d).sum(1), 1e-12)
                    v = mid[:, None, :] - a[None, :, :]
                    t = np.clip((v * d[None]).sum(2) / l2[None], 0.0, 1.0)
                    q = a[None, :, :] + t[:, :, None] * d[None, :, :]
                    dm = np.hypot(q[:, :, 0] - mid[:, None, 0], q[:, :, 1] - mid[:, None, 1])
                    k = np.argmin(dm, axis=1)
                    dm = dm[row, k]
                    if line.shape[1] >= 3:
                        zq = line[k, 2] + t[row, k] * (line[k + 1, 2] - line[k, 2])
                    else:
                        zq = ref
                    take = dm < dd
                    dd[take] = dm[take]
                    dz[take] = zq[take]
                on = np.array([p[6] is True for p in pending], bool) & np.isfinite(dd) & (dd <= WALL_FIT_REACH_M)
                ref = np.where(on, dz, ref)
            # An untagged wall's level is the road it runs along too: the ground beside it can be a bridge's
            # approach fill, higher than the road the wall stands next to (the wall climbed it at the abutments).
            untagged = np.array([p[0] == G_WALL and p[6] is None for p in pending], bool)
            if untagged.any():
                rd = np.full(len(pending), np.inf)
                rz = np.zeros(len(pending))
                for line, _half, is_deck in fits:
                    if is_deck:
                        continue
                    xy = line[:, :2]
                    a, b = xy[:-1], xy[1:]
                    d = b - a
                    l2 = np.maximum((d * d).sum(1), 1e-12)
                    v = mid[:, None, :] - a[None, :, :]
                    t = np.clip((v * d[None]).sum(2) / l2[None], 0.0, 1.0)
                    q = a[None, :, :] + t[:, :, None] * d[None, :, :]
                    dm = np.hypot(q[:, :, 0] - mid[:, None, 0], q[:, :, 1] - mid[:, None, 1])
                    k = np.argmin(dm, axis=1)
                    dm = dm[row, k]
                    seg = d[k]
                    seg = seg / np.maximum(np.hypot(seg[:, 0], seg[:, 1]), 1e-12)[:, None]
                    if line.shape[1] >= 3:
                        zq = line[k, 2] + t[row, k] * (line[k + 1, 2] - line[k, 2])
                    else:
                        zq = ref
                    along = np.abs((wt * seg).sum(1))
                    take = (dm < rd) & (along >= par_min) & (dm <= WALL_FIT_REACH_M)
                    rd[take] = dm[take]
                    rz[take] = zq[take]
                ref = np.where(untagged & np.isfinite(rd), rz, ref)
            fd = np.full(len(pending), np.inf)      # (nearest carriageway of any direction, at its level)
            fv = np.zeros((len(pending), 2))
            ft = np.zeros((len(pending), 2))
            fh = np.zeros(len(pending))
            pd = np.full(len(pending), np.inf)      # (nearest one the wall runs along)
            pv = np.zeros((len(pending), 2))
            pt = np.zeros((len(pending), 2))
            ph = np.zeros(len(pending))
            for line, half, is_deck in fits:
                xy = line[:, :2]
                a, b = xy[:-1], xy[1:]
                d = b - a
                l2 = np.maximum((d * d).sum(1), 1e-12)
                v = mid[:, None, :] - a[None, :, :]
                t = np.clip((v * d[None]).sum(2) / l2[None], 0.0, 1.0)
                q = a[None, :, :] + t[:, :, None] * d[None, :, :]
                dm = np.hypot(q[:, :, 0] - mid[:, None, 0], q[:, :, 1] - mid[:, None, 1])
                k = np.argmin(dm, axis=1)
                dm = dm[row, k]
                seg = d[k]
                seg = seg / np.maximum(np.hypot(seg[:, 0], seg[:, 1]), 1e-12)[:, None]
                if line.shape[1] >= 3:
                    zq = line[k, 2] + t[row, k] * (line[k + 1, 2] - line[k, 2])
                else:
                    zq = ref
                lev = np.abs(zq - ref) <= WALL_LEVEL_M      # (not a road under a viaduct)
                if is_deck:
                    # (a wall leaves a bridge at its mapped outline: an approach past it keeps its line; without
                    # an outline only the deck's own footprint counts, as in guardrails._surface_info)
                    plan = (dm <= half + WALL_DECK_REACH_M) | (inside if inside is not None else False)
                else:
                    plan = np.ones(len(pending), bool)
                take = (dm < fd) & lev & plan
                if take.any():
                    fd[take] = dm[take]
                    fv[take] = q[take, k[take]] - mid[take]
                    ft[take] = seg[take]
                    fh[take] = half
                along = np.abs((wt * seg).sum(1))
                take = (dm < pd) & (along >= par_min) & lev & plan   # (a crossing way must not steal the fit)
                if take.any():
                    pd[take] = dm[take]
                    pv[take] = q[take, k[take]] - mid[take]
                    pt[take] = seg[take]
                    ph[take] = half
                    pz[take] = zq[take]
            near = walls & np.isfinite(pd) & (pd <= WALL_FIT_REACH_M)
            across = walls & np.isfinite(fd) & (fd <= WALL_FIT_REACH_M) & (fd <= fh + 1.0) \
                & (np.abs((wt * ft).sum(1)) < math.cos(math.radians(WALL_ACROSS_DEG)))
            for i in np.flatnonzero(near).tolist():
                p = pending[i]
                T = pt[i]
                left = np.array([-T[1], T[0]])
                side = 1.0 if float((pv[i] * left).sum()) < 0.0 else -1.0
                here = mid[i] + pv[i] + side * left * (ph[i] + WALL_EDGE_M + WALL_ACROSS)
                t_new = side * T
                px, py, yaw = _place(p[0], (float(here[0]), float(here[1])),
                                     (float(t_new[0]), float(t_new[1])))
                keep = x0 <= here[0] < x1 and y0 <= here[1] < y1
                pending[i] = ((p[0], px, py, yaw, float(here[0]), float(here[1])) + p[6:9]
                              + (keep, float(t_new[0]), float(t_new[1])))
            for i in np.flatnonzero(across).tolist():
                p = pending[i]
                pending[i] = p[:9] + (False,) + p[10:]      # (never a panel across the road)
            # nor one standing on a carriageway at its own level, whatever its direction: a wall mapped along the edges of
            # a wildlife overpass over the S7 (Jankowo Gdanskie, GitHub #1022), its deck not built, left panels on the
            # lanes after the across rule took the rest (its centre or either end within the carriageway + WALL_ON_ROAD_M)
            keep_ix = [i for i, p in enumerate(pending) if p[0] == G_WALL and p[9]]
            if keep_ix:
                kp = np.array([[pending[i][4], pending[i][5]] for i in keep_ix], np.float64)
                kt = np.array([[pending[i][10], pending[i][11]] for i in keep_ix], np.float64)
                probes = (kp, kp + kt * (WALL_LEN / 2.0), kp - kt * (WALL_LEN / 2.0))
                kref = ref[keep_ix]
                on = np.zeros(len(keep_ix), bool)
                for line, half, is_deck in fits:
                    if is_deck:
                        continue
                    xy_l = line[:, :2]
                    a, b = xy_l[:-1], xy_l[1:]
                    d = b - a
                    l2 = np.maximum((d * d).sum(1), 1e-12)
                    for pt in probes:
                        v = pt[:, None, :] - a[None, :, :]
                        t = np.clip((v * d[None]).sum(2) / l2[None], 0.0, 1.0)
                        q = a[None, :, :] + t[:, :, None] * d[None, :, :]
                        dm = np.hypot(q[:, :, 0] - pt[:, None, 0], q[:, :, 1] - pt[:, None, 1])
                        k = np.argmin(dm, axis=1)
                        r = np.arange(len(pt))
                        lev = np.ones(len(pt), bool)
                        if line.shape[1] >= 3:
                            zq = line[k, 2] + t[r, k] * (line[k + 1, 2] - line[k, 2])
                            lev = np.abs(zq - kref) <= WALL_LEVEL_M
                        on |= (dm[r, k] < half + WALL_ON_ROAD_M) & lev
                for j in np.flatnonzero(on).tolist():
                    p = pending[keep_ix[j]]
                    pending[keep_ix[j]] = p[:9] + (False,) + p[10:]
    xy = np.array([[p[1], p[2]] for p in pending], np.float64)
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    z = np.asarray(z, np.float64)
    if decks:
        # (the deck's height under the panel's centre on the mapped line: the panel's origin is beside it)
        mid = np.array([[p[4], p[5]] for p in pending], np.float64)
        surf, ground, wide = _surface_info(frame, mid, elev, decks, [p[6] for p in pending])
        on = np.abs(surf - ground) > 1e-6
        if outlines:
            edge = ~on & np.isfinite(wide) & np.array([p[6] is True for p in pending])
            if edge.any():
                edge[edge] = _in_outlines(outlines, mid[edge])
            surf, on = np.where(edge, wide, surf), on | edge
        z = np.where(on, surf, z)
        if walls.any():
            # A wall laid along a carriageway follows that carriageway's own height: the ground beside it can be a
            # bridge's approach fill, and the wall climbed it to the deck's height at the abutments (two end panels
            # stood at the deck where a wall crossed under the Galgenbergstraße bridge).
            z = np.where(near, pz, z)
        # A wall running onto a bridge keeps to its deck: a stretch of its panels that would stand on the
        # ground under the deck, with the panels on both sides of that stretch at the deck's height, is on
        # the bridge (OSM often leaves layer / bridge off a noise wall along one: the wall broke into the
        # road under the bridge mid-span, 7.5 m down). A wall really under a bridge has no such panels.
        groups = {}
        for i, p in enumerate(pending):
            if p[6] is None:            # (its own tags/level say nothing about the bridge)
                groups.setdefault((p[7], p[8]), []).append(i)
        for idx in groups.values():
            if len(idx) < 3:
                continue
            idx = np.asarray(idx, np.int64)     # (in the order the part's panels were made: along the line)
            near_deck = np.isfinite(wide[idx])
            at_deck = near_deck & (np.abs(wide[idx] - ground[idx]) <= DECK_AUTO_SNAP_TOLERANCE_M)
            off_deck = near_deck & ~at_deck
            i0 = 0
            while i0 < len(idx):
                if not off_deck[i0]:
                    i0 += 1
                    continue
                i1 = i0
                while i1 + 1 < len(idx) and off_deck[i1 + 1]:
                    i1 += 1
                # (an end of the part: the panels on that side stop there, no bridge to run onto)
                before = at_deck[i0 - 1] if i0 > 0 else None
                after = at_deck[i1 + 1] if i1 + 1 < len(idx) else None
                if (before is None or before) and (after is None or after) and (before or after):
                    z[idx[i0:i1 + 1]] = wide[idx[i0:i1 + 1]]
                i0 = i1 + 1
        # A barrier running along a bridge's edge for most of its length is the bridge's parapet, whatever its tags
        # say (none here): OSM ways 'barrier=jersey_barrier, fence_type=chain_link' along both sides of SR 103's
        # bridge over I-75 at Bluffton, Ohio, no bridge / layer tag, stood their chain-link across the interstate
        # under it (the user, 2026-11-10). Each of its panels within ALONG_EDGE_M of the deck's edge and running with
        # it (ALONG_DEG) stands on the deck, when ALONG_FRAC of the part's panels do.
        for idx in groups.values() if PARAPET_ON else ():
            idx = np.asarray(idx, np.int64)
            tang = np.array([[pending[i][10], pending[i][11]] for i in idx], np.float64)
            dist, half, cosa = _along_deck(mid[idx], tang, decks)
            along = np.isfinite(dist) & (dist <= half + ALONG_EDGE_M) & (cosa >= math.cos(math.radians(ALONG_DEG))) \
                & np.isfinite(wide[idx])
            if along.sum() < max(2, ALONG_FRAC * len(idx)):
                continue
            # (only one that would otherwise stand across a road under the bridge: a wall along a viaduct with
            # nothing under it can be on the ground there)
            if not roads or not _on_road(mid[idx[along]], z[idx[along]], roads).any():
                continue
            z[idx[along]] = wide[idx[along]]
    # and never a chain-link panel standing on a carriageway at its own level (a fence ending at a road is mapped
    # to its edge; one drawn over it is a level the data left out)
    chain_on = [i for i, p in enumerate(pending) if p[0] == G_CHAIN and p[9]]
    if chain_on and roads and CHAIN_OFF_ROAD_ON:
        cxy = np.array([[pending[i][4], pending[i][5]] for i in chain_on], np.float64)
        for j in np.flatnonzero(_on_road(cxy, z[chain_on], roads)).tolist():
            p = pending[chain_on[j]]
            pending[chain_on[j]] = p[:9] + (False,) + p[10:]
    out = list(fr_rows)
    for p, zz in zip(pending, z):
        if not p[9]:
            continue
        group, px, py, yaw = p[0], p[1], p[2], p[3]
        lift = CHAIN_LIFT if group == G_CHAIN else 0.0
        height = CHAIN_H if group == G_CHAIN else WALL_H
        out.append([group, round(px, 2), round(py, 2), round(float(zz) + lift, 2), round(yaw, 1) % 360.0,
                    1.0, height, 0.0])
    return out


# Mapped stone walls (GitHub #938: the field walls along Ireland's and Britain's country roads). OSM's barrier=wall with
# wall=dry_stone (or another stone wall type, or material=stone and no wall=), drawn as a stone solid along the mapped
# line: STONE_THICK_M thick, its own height= tag within STONE_H_RANGE else STONE_H_M over the ground, its foot
# STONE_FOOT_M into it (the ground between the line's points is not flat). The retaining walls' mesh draws it
# (roadmesh._retaining_walls, a parapet: stone on both faces down to its foot). Left out: a wall on a bridge (its
# bridge / layer tags or Overture's level), and the metres of it within STONE_ROAD_GAP_M of a ground road's edge (a
# wall mapped to the road's centre line at a gate or a junction would block it). BNE_STONE_WALLS=off: none.
STONE_ON = os.environ.get("BNE_STONE_WALLS", "on") != "off"
STONE_TYPES = {"dry_stone", "drystone", "dry_stone_wall", "stone", "field_stone", "fieldstone", "flint", "rubble",
               "stone_wall"}
STONE_MATERIALS = {"stone", "dry_stone", "granite", "limestone", "sandstone", "slate", "flint"}
STONE_H_M = 1.1
STONE_H_RANGE = (0.4, 2.5)
STONE_THICK_M = 0.5         # (roadmesh.WALL_COPING_M: all of its top is stone, no strip of soil)
STONE_FOOT_M = 0.3
STONE_STEP_M = 1.0
STONE_ROAD_GAP_M = 0.6


def stone_height(feats, i):
    """The height (m) of mapped stone wall i, or None when it is not one (STONE_TYPES / STONE_MATERIALS)."""
    classes = feats.attrs.get("class")
    cls = str(classes[i]).strip().lower() if classes is not None else ""
    tags = _tags(feats, i)
    if tags.get("barrier", cls) != "wall" and cls != "wall":
        return None
    wall = tags.get("wall", "")
    if not (wall in STONE_TYPES or (not wall and tags.get("material", "") in STONE_MATERIALS)):
        return None
    try:                            # Overture's height, else the OSM tag
        h = float(np.asarray(feats.attrs.get("height"))[i])
    except (TypeError, ValueError, IndexError):
        h = float("nan")
    if not h == h:
        try:
            h = float(tags.get("height", "").replace("m", "").strip())
        except ValueError:
            h = float("nan")
    return h if STONE_H_RANGE[0] <= h <= STONE_H_RANGE[1] else STONE_H_M


def _resample(line, step):
    """Points every ~step m along a polyline, its own end points kept."""
    seg = np.hypot(*np.diff(line, axis=0).T)
    arc = np.concatenate(([0.0], np.cumsum(seg)))
    if arc[-1] < 1e-6:
        return line[:1]
    n = max(int(math.ceil(arc[-1] / step)), 1)
    s = np.linspace(0.0, arc[-1], n + 1)
    return np.column_stack((np.interp(s, arc, line[:, 0]), np.interp(s, arc, line[:, 1])))


def _near_road(xy, roads):
    """(n,) bool: the points within a ground road's half width + STONE_ROAD_GAP_M of its centre line."""
    hit = np.zeros(len(xy), bool)
    lo, hi = xy.min(0), xy.max(0)
    for _fid, spec, pts, *_r in roads:
        line = np.asarray(pts, np.float64)[:, :2]
        if len(line) < 2:
            continue
        reach = float(getattr(spec, "width", 0.0) or 0.0) / 2 + STONE_ROAD_GAP_M
        if (line.min(0) > hi + reach).any() or (line.max(0) < lo - reach).any():
            continue
        a, d = line[:-1], np.diff(line, axis=0)
        dd = np.maximum((d * d).sum(1), 1e-12)
        t = np.clip(((xy[:, None, :] - a[None]) * d[None]).sum(2) / dd[None], 0.0, 1.0)
        foot = a[None] + t[..., None] * d[None]
        dist = np.hypot(foot[..., 0] - xy[:, None, 0], foot[..., 1] - xy[:, None, 1]).min(1)
        hit |= dist < reach
    return hit


def stone_walls(frame, infra_fn, box, elev, roads=()):
    """The mapped stone walls in box (x0, y0, x1, y1) as roads.retaining_walls' wall dicts (base, top, foot, toward,
    thick, back_top, parapet), for roadmesh.tunnel's stone: each mapped line's pieces whose segments' middles are in
    the box (a wall split across tiles meets at the edge, not drawn twice), cut where a ground road (roads: build_tile's
    roads_out, (fid, spec, pts, ...)) runs. elev(lat, lon) -> the ground's height."""
    if infra_fn is None or not STONE_ON:
        return []
    x0, y0, x1, y1 = map(float, box)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    try:
        keys = mapcells.cells_for_points(la, lo, pad_lat=0.00005, pad_lon=0.00005, max_cells=64)
    except (ValueError, OverflowError):
        return []
    runs, seen = [], set()
    for key in keys:
        try:
            feats = infra_fn(key)
        except Exception as ex:
            log.debug("stone walls: infrastructure cell %s unavailable: %s", key, ex)
            continue
        if feats is None or not len(feats) or feats.attrs.get("class") is None and feats.attrs.get("tags") is None:
            continue
        boxes = feats.boxes(frame)
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            if fid in seen or int(feats.kind[i]) not in (mapgeom.LINE, mapgeom.POLYGON):
                continue
            b = boxes[i]
            if b[2] < x0 - BBOX_PAD_M or b[0] > x1 + BBOX_PAD_M or b[3] < y0 - BBOX_PAD_M or b[1] > y1 + BBOX_PAD_M:
                continue
            h = stone_height(feats, i)
            if h is None:
                continue
            seen.add(fid)
            if _deck_level_hint(feats, i):
                continue
            try:
                lv = float(np.asarray(feats.attrs.get("level"))[i])
            except (TypeError, ValueError, IndexError):
                lv = 0.0
            if lv > 0:
                continue
            for part in feats.parts(i):
                if not part or len(part[0]) < 2:
                    continue
                e, n = mapgeom.enu_xy(feats, frame, np.asarray(part[0], np.float64))
                xy = _resample(np.column_stack((e, n)), STONE_STEP_M)
                if len(xy) < 2:
                    continue
                mid = (xy[:-1] + xy[1:]) / 2
                keep = (mid[:, 0] >= x0) & (mid[:, 0] < x1) & (mid[:, 1] >= y0) & (mid[:, 1] < y1)
                if not keep.any():
                    continue
                if roads:
                    blocked = _near_road(xy, roads)
                    keep &= ~(blocked[:-1] | blocked[1:])
                j = 0
                while j < len(keep):            # each run of kept segments is a piece
                    if not keep[j]:
                        j += 1
                        continue
                    k = j
                    while k < len(keep) and keep[k]:
                        k += 1
                    runs.append((xy[j:k + 1], h))
                    j = k
    if not runs:
        return []
    allxy = np.concatenate([r for r, _h in runs])
    lat, lon, _ = frame.enu_to_geodetic(allxy[:, 0], allxy[:, 1], np.zeros(len(allxy)))
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    z = np.asarray(z, np.float64)
    out, k = [], 0
    for xy, h in runs:
        zz = z[k:k + len(xy)]
        k += len(xy)
        d = np.gradient(xy, axis=0)
        d /= np.maximum(np.hypot(*d.T), 1e-9)[:, None]
        nl = np.column_stack((-d[:, 1], d[:, 0]))
        top = zz + h
        out.append({"base": xy + nl * (STONE_THICK_M / 2), "toward": nl, "top": top, "back_top": top.copy(),
                    "foot": zz - STONE_FOOT_M, "thick": np.full(len(xy), STONE_THICK_M), "parapet": True,
                    "concrete": False, "mountain": False, "rock": False})
    return out


def tile_rows(frame, tx, ty, md, elev):
    """rows() for one road tile."""
    import roads
    box = roads.tile_bbox(tx, ty)

    def infra_fn(key):
        return md.cell(key, layers=("infrastructure",), kind="demand").layers.get("infrastructure")

    return rows(frame, infra_fn, box, elev)
