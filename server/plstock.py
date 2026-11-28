"""Use the same Polish sign artwork for measured signs and the project's generated sign layers."""
from collections import defaultdict
from functools import lru_cache
import math
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import numpy as np
import plsigns

# Only roles that specify this exact Polish design. The three roundabout roles are independent.
ROLES = {
    "warn.roundabout": "A-8", "mand.roundabout": "C-12", "prio.give_way": "A-7", "prio.stop": "B-20",
    "reg.no_entry": "B-2", "info.dead_end": "D-4a", "info.crossing_right": "D-6",
    "warn.curve_right": "A-1", "warn.curve_left": "A-2", "warn.double_right": "A-3", "warn.double_left": "A-4",
    "warn.signals": "A-29", "info.motorway": "D-9", "info.motorway_end": "D-10",
    "warn.side_right": "A-6b", "warn.side_left": "A-6c",
    "info.living_street": "D-40", "info.living_street_end": "D-41", "info.oneway": "D-3",
    "warn.narrow_both": "A-12a", "info.tunnel": "D-37", "mand.bicycle": "C-13",
    "info.bus_stop": "D-15", "info.parking": "D-18",
}
FURNITURE = {"giveway": "A-7", "stop": "B-20"}
MATCH_M = 12.
POST = "pl_a_7_c_12"
POST_REACH = 20.  # Allow for a detection upstream of the generated shoulder point.


def approach_distance(row, x, y, yaw, reach, lateral, angle):
    dx, dy = row[1] - x, row[2] - y
    a = math.radians(yaw)
    if abs((row[4] - yaw + 180) % 360 - 180) > angle or abs(dx*math.cos(a) + dy*math.sin(a)) > lateral:
        return math.inf
    distance = math.hypot(dx, dy)
    return distance if distance <= reach else math.inf


def in_country(frame, x, y):
    lat, lon, _ = frame.enu_to_geodetic(x, y, 0.)
    return 14. < lon < 25. and 48.9 < lat < 55.1 and plsigns.inside_poland(float(lon), float(lat))


class Nearby:
    def __init__(self, rows, anchors=()):
        import guardrails
        self.rows = rows
        self.items = defaultdict(list)
        for row in (*rows, *anchors):
            self.items[guardrails.polish_groups()[row[0]]].append(row)

    def matched(self, code, x, y, yaw):
        # Yield/stop and posted speed values describe one control on this approach.
        # Roundabout warning, mandatory circulation and yield are separate controls.
        name = plsigns.group(code)
        if code in ("A-7", "C-12", "B-20") and any(
                math.isfinite(approach_distance(r, x, y, yaw, POST_REACH, 4., 40.))
                for r in self.items.get(POST, ())):
            return True
        groups = ("pl_a_7", "pl_b_20") if code in ("A-7", "B-20") else (name,)
        if code.startswith("B-33-"):
            groups = [g for g in self.items if g.startswith("pl_b_33_")]
        for g in groups:
            for r in self.items.get(g, ()):
                if math.isfinite(approach_distance(r, x, y, yaw, MATCH_M, 2.5, 30.)):
                    return True
        return False


def furniture(rows, frame, nearby):
    if not rows:
        return rows
    result = []
    for row in rows:
        code = FURNITURE.get(row[0])
        if not code or not in_country(frame, row[1], row[2]):
            result.append(row)
        elif not nearby.matched(code, row[1], row[2], row[4]):
            localized = [plsigns.group(code), *row[1:6], plsigns.HEIGHT, *row[7:]]
            result.append(localized)
    return result


def ring_entries(roads, ring, frame, bounds):
    """Nominal roadside posts on incoming arms, including split one-way entrances."""
    import furniture
    if not ring:
        return []
    nodes = np.vstack([p[[0, -1], :2] for fid, _, p, _ in roads if fid in ring])
    result = []
    for fid, spec, line, _ in roads:
        if fid in ring or len(line) < 2:
            continue
        for start in (False, True):
            p = line[0 if start else -1, :2]
            if not (bounds[0]-10 <= p[0] <= bounds[2]+10 and bounds[1]-10 <= p[1] <= bounds[3]+10):
                continue
            if (spec.oneway and (spec.dir > 0) == start) or np.min(np.sum((nodes - p)**2, axis=1)) > 2.5**2:
                continue
            arm = line if start else line[::-1]
            spot, direction = furniture._at(arm, np.array([min(8., furniture._arclen(arm)[-1] / 2)]))
            side, yaw = furniture.approach_pose(direction[0], furniture.G_YIELD)
            x, y = spot[0, :2] + side * (spec.width / 2 + 1.)
            if in_country(frame, x, y):
                result.append([None, x, y, None, yaw])
    return sorted(result, key=lambda row: tuple(row[1:3]) + (row[4],))


def roundabout_layout(entries, observed, frame, elev):
    """A measured circle or an incoming arm owns one assembly. A yield sign alone cannot identify a roundabout."""
    import guardrails
    groups = guardrails.polish_groups()
    circle, priority, stop, pair = (groups.index(g) for g in ("pl_c_12", "pl_a_7", "pl_b_20", POST))
    anchors = list(entries)
    assignments = [[] for _ in anchors]
    use_arm_pose = set()
    remaining = []
    for row in sorted(observed, key=lambda r: (r[0] != circle, tuple(r[:5]))):
        if row[0] not in (circle, priority, stop):
            remaining.append(row)
            continue
        dist, i = min(((approach_distance(a, row[1], row[2], row[4], POST_REACH, 4., 40.), i)
                       for i, a in enumerate(anchors)), default=(math.inf, -1))
        if not math.isfinite(dist) and row[0] == priority:
            # Two plates at one surveyed spot can inherit different headings from neighbouring road segments.
            dist, i = min(((approach_distance(r, row[1], row[2], row[4], 3., 3., 65.), j)
                           for j, hits in enumerate(assignments) for r in hits if r[0] == circle),
                          default=(math.inf, -1))
        if not math.isfinite(dist) and row[0] == circle:
            # A detection snapped to the circulating road can face across its own approach.
            # Accept a wider bearing only when there is one plausible incoming arm.
            close = sorted((approach_distance(a, row[1], row[2], row[4], 14., 9., 65.), j)
                           for j, a in enumerate(entries))
            if close and math.isfinite(close[0][0]) and (len(close) == 1 or close[1][0] - close[0][0] > 4.):
                dist, i = close[0]
                use_arm_pose.add(i)
        if math.isfinite(dist):
            assignments[i].append(row)
        elif row[0] == circle:
            anchors.append(row)
            assignments.append([row])
        else:
            remaining.append(row)
    aliases = []
    needs_ground = []
    for i, (anchor, hits) in enumerate(zip(anchors, assignments)):
        # A mapped STOP is a different priority control; do not infer a yield above it.
        stops = [r for r in hits if r[0] == stop]
        if stops:
            remaining.append(stops[0])
        at = min((r for r in hits if r[0] != stop),
                 key=lambda r: (r[0] != circle, math.hypot(r[1]-anchor[1], r[2]-anchor[2])),
                 default=anchor)
        x, y, z, yaw = (anchor if i in use_arm_pose else at)[1:5]
        a = math.radians(yaw)
        fx, fy = math.cos(a), math.sin(a)
        group, height = (circle, plsigns.HEIGHT) if stops else (pair, 3.6)
        row = [group, x, y, z if z is not None else 0., yaw, 0., height, 0., fx, fy, 0., -fy, fx, 0., 0., 0., 1.]
        remaining.append(row)
        if z is None and elev is not None:
            needs_ground.append(row)
        aliases.append([group, *anchor[1:5]])
        aliases.extend([group, *r[1:5]] for r in hits if r[0] != stop)
    if needs_ground:
        x, y = np.array([r[1:3] for r in needs_ground]).T
        lat, lon, _ = frame.enu_to_geodetic(x, y, 0.)
        heights = frame.geodetic_to_enu(lat, lon, elev(lat, lon))[2]
        for r, height in zip(needs_ground, heights):
            r[3] = float(height)
    return Nearby(remaining, aliases)


def roads_in_window(feats, raw, window):
    """Cache ground road bounds with the decoded map cell. A new lane or anchor projection replaces the index."""
    cached = getattr(feats, "_pl_road_bounds", None)
    if cached is None or cached[0] is not raw:
        rows = [(fid, xy, meta) for fid, (_, xy, meta, _, _, skips, _) in raw.items()
                if not skips and len(xy) >= 2]
        bounds = np.empty((0, 4), dtype=np.float64)
        if rows:
            xy = np.concatenate([line[:, :2] for _, line, _ in rows])
            starts = np.cumsum([0] + [len(line) for _, line, _ in rows[:-1]])
            bounds = np.column_stack((np.minimum.reduceat(xy, starts, axis=0),
                                      np.maximum.reduceat(xy, starts, axis=0)))
        cached = raw, rows, bounds.T
        feats._pl_road_bounds = cached
    _, rows, (west, south, east, north) = cached
    x0, y0, x1, y1 = window
    pad = plsigns.REACH
    selected = (west <= x1+pad) & (south <= y1+pad) & (east >= x0-pad) & (north >= y0-pad)
    for i in np.flatnonzero(selected):
        fid, xy, meta = rows[i]
        spec = SimpleNamespace(width=2*meta[0], dir=meta[1], oneway=bool(meta[1]))
        yield fid, spec, xy, 0.


def post_layout(md, frame, bounds, elev, context, left_hand, mph, local_roads=()):
    """Use the same road geometry and survey matching for Forest instances and streamed mesh suppression."""
    import mapcells
    import roadsigns
    import speedsigns
    x0, y0, x1, y1 = bounds
    pad = 2 * POST_REACH
    window = (x0-pad, y0-pad, x1+pad, y1+pad)
    if context is None:
        import islands
        roads = [(fid, sp, np.asarray(p), lon) for fid, sp, p, lon in local_roads]
        ring = islands.ring_fids(roads)
    else:
        coverage = plsigns._polish_tile_bounds(frame, bounds, pad + 30.)
        if coverage is None:
            return Nearby([])
        west, south, east, north = coverage
        by_id, ring = {}, set()
        for key in mapcells.cells_for_bbox(south, west, north, east):
            try:
                feats = context.cell(key)
            except OSError:
                import revisit
                revisit.fault(here=True)
                continue
            if feats is None or not len(feats):
                continue
            raw = speedsigns._cell_rows(feats, frame, context.urban, context.widths, left_hand, mph)
            ring.update(roadsigns.cell_rings(feats, frame, context.urban, context.widths, left_hand, mph, raw))
            for road in roads_in_window(feats, raw, window):
                by_id.setdefault(road[0], road)
        roads = list(by_id.values())
        ring.intersection_update(by_id)
        if not roads and local_roads:
            import islands
            roads = list(local_roads)
            ring = islands.ring_fids(roads)
    found = plsigns.tile_rows(frame, window, roads, elev, getattr(md, "cache_dir", None), getattr(md, "fetcher", None))
    return roundabout_layout(ring_entries(roads, ring, frame, window), found, frame, elev)


def sign_stream(md, frame, box, elev, context, left_hand, mph):
    # This layer suppresses duplicate meshes using only position and heading.
    return post_layout(md, frame, box, None, context, left_hand, mph)


@lru_cache(maxsize=128)
def template(code):
    """Read the display geometry from the same DAE as the Forest instance, with its original UVs."""
    import plsignart
    group = plsigns.group(code)
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    file = root / "mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/pl_signs" / (group + "_plate.dae")
    if not file.is_file():
        file = plsignart.folder(os.environ["BNGE_USER_DIR"]) / file.name
    mesh = ET.parse(file).find(".//{*}geometry/{*}mesh")
    pos_id = mesh.find("{*}vertices/{*}input").get("source")[1:]
    def array(ident, width):
        elem = mesh.find(f"{{*}}source[@id='{ident}']/{{*}}float_array")
        return np.fromstring(elem.text, sep=" ").reshape(-1, width)
    pos = array(pos_id, 3)
    blocks = mesh.findall("{*}triangles")
    uv = array(blocks[0].find("{*}input[@semantic='TEXCOORD']").get("source")[1:], 2)
    tris, mats, indices = [], [], []
    for b in blocks:
        t = np.fromstring(b.find("{*}p").text, dtype=np.int64, sep=" ").reshape(-1, 3)
        mats.append(b.get("material").removesuffix("-mat"))
        indices.append(np.full(len(t), len(mats)-1, dtype=np.int64))
        tris.append(t)
    bottom = pos[tris[mats.index(group)].ravel(), 2].min()
    return pos, np.vstack(tris), uv, mats, np.concatenate(indices), bottom


def replacement(code, row, origin, mount=None):
    pos, triangles, uv, mats, indices, bottom = template(code)
    x, y, zg, zr, tx, ty = row[:6]
    length = math.hypot(tx, ty)
    tx, ty = tx / length, ty / length
    px, py, pz = pos.T
    dz = 0 if mount is None else max(zg + 1.2, zr + mount) - zg - bottom
    world = np.column_stack((x - origin[0] + px*ty + py*tx, y - origin[1] - px*tx + py*ty,
                             zg + pz + np.where(pz > 0, dz, 0)))
    return world, triangles, uv, mats, indices


def split(frame, signs, table, codes, nearby, origin, stacked=False):
    """Resolve signs after the stock rules ran, retaining the stock artwork for unverified variants."""
    import dezones
    keep, extra = [], None
    ready = set(plsigns.supported_codes())
    for i, (s, row, code) in enumerate(zip(signs, table, codes)):
        if not code or not in_country(frame, s[0], s[1]):
            keep.append(i)
            continue
        yaw = math.degrees(math.atan2(-s[2], s[3]))
        if nearby.matched(code, s[0], s[1], yaw):
            continue
        if code not in ready:
            keep.append(i)
            continue
        try:
            mount = row[9] if stacked else None
            part = replacement(code, row, origin, mount)
        except (OSError, KeyError, ET.ParseError):
            keep.append(i)
            continue
        extra = dezones._merged(extra, part)
    return table[keep], extra
