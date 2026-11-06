"""Short paved links from the back of a sidewalk to a mapped frontage."""

import numpy as np

import mapgeom
import polygon
import roadwidth

MAX_GAP_M = 2.4
STEP_M = 1.5
GREEN = {"garden", "grass", "flowerbed", "grassland", "meadow", "park", "village_green",
         "allotments", "hedge", "orchard", "wood", "forest", "cemetery", "farmland",
         "playground", "recreation_ground", "water", "wetland"}


def _areas(frame, cells, box):
    """Paved destination polygons and areas to leave open, in the tile's frame."""
    import plazas
    import surface

    paved, protected = [], []
    seen = set()
    x0, y0, x1, y1 = box
    for feats in cells:
        if feats is None or not len(feats):
            continue
        classes = feats.attrs.get("class")
        if classes is None:
            continue
        parks = surface._parking(feats)
        squares = surface._plazas(feats)
        materials = feats.attrs.get("surface")
        boxes = feats.boxes(frame)
        in_box = (boxes[:, 2] >= x0) & (boxes[:, 0] <= x1) & (boxes[:, 3] >= y0) & (boxes[:, 1] <= y1)
        for i in np.flatnonzero(in_box & (feats.kind == mapgeom.POLYGON)):
            fid = str(feats.ids[i])
            if feats.clipped is None or not feats.clipped[i]:
                if fid in seen:
                    continue
                seen.add(fid)
            mat = str(materials[i] or "").strip().lower() if materials is not None else ""
            hard = ((parks is not None and parks[i]) or (squares is not None and squares[i])) and mat not in plazas.UNPAVED
            if not (hard or str(classes[i]) in GREEN or mat in plazas.UNPAVED):
                continue
            for part in feats.parts(i):
                if not part or len(part[0]) < 3:
                    continue
                rings = [np.column_stack(mapgeom.enu_xy(feats, frame, ring)) for ring in part]
                if hard:
                    paved.append(rings[0])
                    protected.extend(rings[1:])
                else:
                    protected.append(rings[0])
    return paved, protected


def _walls(rings):
    return roadwidth.Walls(np.concatenate([np.c_[ring, np.roll(ring, -1, axis=0)] for ring in rings])
                           if rings else np.empty((0, 4)))


def _inside(points, rings):
    inside = np.zeros(len(points), bool)
    for ring in rings:
        lo, hi = ring.min(axis=0), ring.max(axis=0)
        ii = np.flatnonzero(~inside & np.all((points >= lo) & (points <= hi), axis=1))
        if len(ii):
            inside[ii] = polygon.contains_points(ring, points[ii])
    return inside


def _green_overlap(quads, rings, walls):
    """Reject a crossing or a planted island wholly inside a connector."""
    ends = np.roll(quads, -1, axis=1)
    start = quads.reshape(-1, 2)
    delta = ends.reshape(-1, 2) - start
    length = np.linalg.norm(delta, axis=1)
    directions = delta / np.maximum(length, 1e-9)[:, None]
    hit = walls.rays(start, directions, max(float(length.max()), .01))
    overlap = ((hit <= length + .001) | _inside(start, rings)).reshape(-1, 4).any(axis=1)
    lo, hi = quads.min(axis=1), quads.max(axis=1)
    for ring in rings:
        # With no crossing edges, only a polygon contained in the quad
        # can have been missed. One vertex then suffices.
        p = ring[0]
        for i in np.flatnonzero(~overlap & np.all((p >= lo) & (p <= hi), axis=1)):
            overlap[i] = polygon.contains_points(quads[i], p[None])[0]
    return overlap


def _access_chunks(walks, paved, green, carriageways):
    """Give a mapped driveway a continuous surface from the street to the mapped lot."""
    import roads

    if not paved or not roads.ROADS_IN_GROUND:
        return []
    walks = [(np.asarray(w["pts"]), w["w"] / 2) for w in walks]
    through = [(p[:, :2], spec.width / 2) for _, spec, p, _ in carriageways if spec.cls != "service"]
    chunks = []
    for fid, spec, pts, _ in carriageways:
        if spec.cls != "service" or spec.kind != "paved" or len(pts) < 2:
            continue
        end_points = pts[[0, -1], :2]
        ends = _inside(end_points, paved) & ~_inside(end_points, green)
        if ends[0] == ends[1]:
            continue
        line = roads.densify(pts if ends[1] else pts[::-1], .75)
        inside = _inside(line[:, :2], paved) & ~_inside(line[:, :2], green)
        start_lot = int(np.flatnonzero(inside)[0])
        access = line[:start_lot + 1]
        length = np.linalg.norm(np.diff(access[:, :2], axis=0), axis=1).sum()
        if not (1. <= length <= 16.):
            continue
        # The service way must actually reach a sidewalk at this end. A
        # parking aisle between two parcels is already painted as ground.
        if not any(roads._seg_dist_min(walk, p) <= hw + spec.width / 2 + .5
                   for walk, hw in walks for p in access[:-1, :2]):
            continue
        for run in roads.cut_outside(access, through, .5, min_piece_m=1.):
            chunks.append(roads._chunk(roads.look_of(spec, fid), spec.width, run))
    return chunks


def chunks(frame, walks, buildings, lots, carriageways, drives, box):
    """Draped paving for short, physically bounded gaps outside attached sidewalks."""
    import fast
    import roads
    import sidewalks

    candidates = [w for w in walks if w.get("surface") in sidewalks.EU_SLAB and "pts" in w
                  and not w.get("detached") and not w.get("median")]
    if not candidates:
        return []
    paved, green = _areas(frame, lots, box)
    targets = list(buildings) + paved
    if not targets:
        return []
    walls, green_walls = _walls(targets), _walls(green)
    blockers = [(p[:, :2], spec.width / 2) for _, spec, p, _ in carriageways]
    blockers += [(np.asarray(d["n"], np.float64)[:, :2], float(d["w"]) / 2) for d in drives]
    blocks = roads._blocker_segments(blockers) if blockers else None
    result = _access_chunks(candidates, paved, green, carriageways)
    for walk in candidates:
        line = roads.densify(np.asarray(walk["pts"]), STEP_M)
        curb, normal = sidewalks.run_geometry(line, walk["side"], walk["w"])
        back = curb[:, :2] + walk["w"] * normal
        normal /= np.maximum(np.linalg.norm(normal, axis=1), 1e-9)[:, None]
        dist = walls.rays(back, normal, MAX_GAP_M)
        valid = (dist >= .15) & ~_inside(back, targets)
        i = np.flatnonzero(valid[:-1] & valid[1:] & (np.abs(np.diff(dist)) <= 1.1))
        if not len(i):
            continue
        middle = (back[i] + back[i + 1]) * .5
        outward = (normal[i] + normal[i + 1]) * .5
        outward /= np.maximum(np.linalg.norm(outward, axis=1), 1e-9)[:, None]
        mid_dist = walls.rays(middle, outward, MAX_GAP_M)
        # Avoid spanning a gap between two unrelated façades.
        i = i[np.abs(mid_dist - .5 * (dist[i] + dist[i + 1])) <= .45]
        if not len(i):
            continue
        end = back + normal * (dist - .02)[:, None]
        quads = np.stack([back[i], back[i + 1], end[i + 1], end[i]], axis=1)
        if blocks is not None:
            fast.require(0, "inside_blockers")
            probe = np.concatenate([quads, (quads + np.roll(quads, -1, axis=1)) * .5,
                                    quads.mean(axis=1, keepdims=True)], axis=1)
            hit = fast._ef.inside_blockers(np.ascontiguousarray(probe.reshape(-1, 2)), *blocks)
            quads = quads[~np.asarray(hit).reshape(-1, 9).any(axis=1)]
        if len(quads) and green:
            quads = quads[~_green_overlap(quads, green, green_walls)]
        material = sidewalks.MATERIALS[sidewalks.EU_SLAB[walk["surface"]]]
        repeat = sidewalks.BRICK_REPEAT_M if walk["surface"] == "brick" else 2.
        result.extend({"m": material, "w": 0., "tl": repeat, "u_m": repeat, "p": 17, "layer": "edge",
                       "poly": quad.tolist(), "n": []} for quad in quads)
    return result
