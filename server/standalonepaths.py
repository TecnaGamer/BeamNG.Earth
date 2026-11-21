"""Ground-only walking and cycling surfaces. They never enter the road graph or the street-edge generator."""
import math
import os

import numpy as np

import mapgeom
import osmtags
import roads


WIDTH = {"footway": 1.4, "path": 1.2, "cycleway": 2.0, "pedestrian": 3.0}
# Locally reported 4 m width for the Charzykowy waterfront, mapped as Jeziorna and Promenada.
CHARZYKOWY_PROMENADE = {
    176276386, 337675417, 173504090, 22967138, 276088780,  # Jeziorna
    359931164, 125941065, 125810533, 22967104, 125954292,  # Promenada
}
EXCLUDE = {"sidewalk", "crossing", "crosswalk", "cycle_crossing", "traffic_island", "link", "stairs", "steps"}
# An unlabelled line following the roadside can be a separately surveyed sidewalk.
VERGE_M = 4.0
PARALLEL = math.cos(math.radians(35))


def enabled():
    return os.environ.get("BNE_STANDALONE_PATHS", "off").lower() in {"1", "on", "true"}


def profile(f, i):
    cls = str(f.attrs["class"][i])
    if cls not in WIDTH or f.kind[i] != mapgeom.LINE or roads.unbuilt(f, i):
        return None
    for key in ("subclass", "osm_footway", "osm_cycleway", "osm_path"):
        col = f.attrs.get(key)
        if col is not None and str(col[i]) in EXCLUDE:
            return None
    sub = f.rules.get("subclass")
    if sub is not None and any(sub.value[j] in EXCLUDE for j in sub.rows(i)):
        return None
    width = roads._osm(f, "width", i, .4, 12.)
    if width is None:
        try:
            width = float(roads._rule_value(f.rules.get("width"), i))
        except (ValueError, TypeError):
            width = None
    if width is None or not .4 <= width <= 12.:
        width = WIDTH[cls]
    source = f.attrs.get("src_dataset")
    record = f.attrs.get("src_record")
    if (source is not None and source[i] == "OpenStreetMap"
            and record is not None and osmtags.way_id(str(record[i])) in CHARZYKOWY_PROMENADE):
        width = 4.0
    raw = roads._map_surface_value(f, i)
    kind = roads.surface_kind("track" if cls == "path" else cls, raw)
    return width, kind


def _outside_verges(xy, width, starts, delta, lengths, radii, bounds):
    """Discard only the portions that run along an existing street. Perpendicular entrances can meet it."""
    lo, hi = xy.min(axis=0) - width / 2, xy.max(axis=0) + width / 2
    near = np.flatnonzero((bounds[:, 0] <= hi[0]) & (bounds[:, 1] <= hi[1])
                         & (bounds[:, 2] >= lo[0]) & (bounds[:, 3] >= lo[1]))
    if not len(near):
        return [xy]
    xyz = roads.densify(np.c_[xy, np.zeros(len(xy))], 1.)
    step = np.diff(xyz[:, :2], axis=0)
    mid = xyz[:-1, :2] + step * .5
    size = np.linalg.norm(step, axis=1)
    keep = size > 1e-8
    # Test segments in batches, with bounded temporary arrays even for long paths.
    # Rounded ends still exclude parallel sidewalks at street bends and junctions.
    for first in range(0, len(near), 32):
        j = near[first:first + 32]
        unit = delta[j] / lengths[j, None]
        reach2 = (radii[j] + width / 2) ** 2
        for offset in range(0, len(mid), 1024):
            idx = np.flatnonzero(keep[offset:offset + 1024]) + offset
            rel = mid[idx, None, :] - starts[j]
            along = rel[:, :, 0] * unit[:, 0] + rel[:, :, 1] * unit[:, 1]
            across = rel[:, :, 0] * unit[:, 1] - rel[:, :, 1] * unit[:, 0]
            past_end = np.maximum(np.maximum(-along, along - lengths[j]), 0.)
            parallel = np.abs(step[idx] @ unit.T) >= PARALLEL * size[idx, None]
            keep[idx] &= ~np.any(parallel & (across ** 2 + past_end ** 2 <= reach2), axis=1)
    if keep.all():
        return [xy]
    switches = np.flatnonzero(np.diff(np.r_[False, keep, False]))
    return [xyz[a:b + 1, :2] for a, b in switches.reshape(-1, 2) if size[a:b].sum() >= 1.]


def keep_outside(points, blockers, radii=None):
    """Keep footprints off accepted strips, including their rounded ends and bends.

    With no radii this is a point test. Vegetation supplies its ground clearance radius;
    checking only its centre lets a bush beside a narrow path cover the paving.
    """
    import fast
    if not blockers or not len(points):
        return np.ones(len(points), bool)
    points = np.ascontiguousarray(points, np.float64)
    if radii is None:
        return ~np.asarray(fast._ef.inside_blockers(points, *roads._blocker_segments(blockers)), bool)
    radii = np.broadcast_to(np.asarray(radii, np.float64), (len(points),))
    x, y = points.T
    left, right = x - radii, x + radii
    bottom, top = y - radii, y + radii
    keep = np.ones(len(points), bool)
    for line, half in blockers:
        line = np.ascontiguousarray(line, np.float64)
        if len(line) < 2:
            continue
        xmin, ymin = line.min(0) - half
        xmax, ymax = line.max(0) + half
        near = np.flatnonzero(keep & (right >= xmin) & (left <= xmax) & (top >= ymin) & (bottom <= ymax))
        if len(near):
            keep[near] = fast._ef.polyline_dist(line, points[near]) > half + radii[near]
    return keep


def clear_props(rows, blockers):
    """Reject scenery overlapping a walking strip, including mapped and late-added props."""
    import furniture
    import pathfootprints
    if not rows or not blockers:
        return rows
    # These are freestanding ground objects. Signs, bridge fixtures, overhead
    # wires and building attachments can share their XY with a path on another
    # level and must not be removed by a 2D test.
    boxed = {furniture.G_HYDRANT, furniture.G_BENCH, furniture.G_BIN}
    candidates = {}
    for i, row in enumerate(rows):
        if row[0] in pathfootprints.CATALOG or row[0] in boxed or row[0] == furniture.G_STREET_TREE:
            candidates.setdefault(row[0], []).append(i)
    keep = np.ones(len(rows), bool)
    clearance = None
    for group, indices in candidates.items():
        if group in boxed:
            if clearance is None:
                clearance = furniture.RoadClearance([(half, line) for line, half in blockers])
            keep[indices] = [clearance.clear(rows[i], margin=0.0) for i in indices]
        else:
            radii = (0.0 if group == furniture.G_STREET_TREE else
                     pathfootprints.radii(group, [rows[i][6] for i in indices], 0.))
            keep[indices] = keep_outside([rows[i][1:3] for i in indices], blockers, radii)
    return [row for row, ok in zip(rows, keep) if ok]


def finish_props(rows, frame, box, cells, urban=None, widths=None, *, path_parts=None):
    """The final generation gate, after every source has contributed its scenery."""
    if not rows or not enabled():
        return rows
    if path_parts is None:
        path_parts = parts(frame, box, cells, urban, widths)
    blockers = [(xy, width / 2) for _, xy, width, _ in path_parts]
    return clear_props(rows, blockers)


def parts(frame, box, cells, urban=None, widths=None, quick=False):
    """(feature id, ENU polyline, width, material) near a square. All callers share the same exclusions."""
    if not enabled():
        return []
    parts_of = roads.quick_ground_parts if quick else roads.ground_parts
    candidates, segments, half = [], [], []
    seen = set()
    placement = roads.placement_context(cells, urban, widths)
    x0, y0, x1, y1 = box
    for f in cells:
        if f is None or not len(f) or "class" not in f.attrs:
            continue
        specs = roads.road_specs(f, urban, widths)
        boxes = f.boxes(frame)
        for i in np.flatnonzero((boxes[:, 0] <= x1 + roads.NEAR_M) & (boxes[:, 2] >= x0 - roads.NEAR_M)
                                & (boxes[:, 1] <= y1 + roads.NEAR_M) & (boxes[:, 3] >= y0 - roads.NEAR_M)):
            fid = str(f.ids[i])
            if fid in seen or ("subtype" in f.attrs and f.attrs["subtype"][i] != "road"):
                continue
            seen.add(fid)
            sp = specs[i]
            style = profile(f, i) if sp is None else None
            if sp is None and style is None:
                continue
            for ring in parts_of(f, i):
                if sp is not None:
                    ring = placement.ring(frame, f, i, ring, sp, fid)
                xy = np.column_stack(mapgeom.enu_xy(f, frame, ring))
                if len(xy) < 2:
                    continue
                if sp is None:
                    candidates.append((fid, xy, *style))
                else:
                    edge = max((w.setback + w.width for _, w in sp.street.walks()), default=0.) if sp.street else sp.walk
                    radius = sp.width / 2 + max(VERGE_M, edge)
                    segments.append(np.column_stack((xy[:-1], xy[1:])))
                    half.append(np.full(len(xy) - 1, radius))
    if not segments or not candidates:
        return candidates
    seg = np.concatenate(segments)
    delta = seg[:, 2:] - seg[:, :2]
    lengths = np.linalg.norm(delta, axis=1)
    valid = lengths > 1e-8
    a, b, delta, lengths, radii = seg[valid, :2], seg[valid, 2:], delta[valid], lengths[valid], np.concatenate(half)[valid]
    bounds = np.c_[np.minimum(a, b) - radii[:, None], np.maximum(a, b) + radii[:, None]]
    return [(fid, run, width, kind) for fid, xy, width, kind in candidates
            for run in _outside_verges(xy, width, a, delta, lengths, radii, bounds)]
