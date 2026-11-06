"""Paved ground between a European street and its mapped frontage."""

from functools import lru_cache
import json
import os

import numpy as np

import mapgeom
import polygon
import roadwidth

MAX_GAP_M = 2.4
BUILT_FRONT_M = 10.0
STEP_M = 1.5
GREEN = {"garden", "grass", "flowerbed", "grassland", "meadow", "park", "village_green",
         "allotments", "hedge", "orchard", "wood", "forest", "cemetery", "farmland",
         "playground", "recreation_ground", "water", "wetland"}
COMMERCIAL = {"retail", "commercial", "shopping_centre", "supermarket", "office"}
PAVED_AREAS = {"footway", "pedestrian", "cycleway", "living_street", "service"}
PHOTO_WIDTH_M = 5.5


class PhotoPaving:
    """Conservative evidence of continuous hard ground in the cached orthophoto."""

    def __init__(self, rgb, x0, y0, size):
        self.x0, self.y0, self.size = x0, y0, size
        r, g, b = np.asarray(rgb, dtype=np.int16).transpose(2, 0, 1)
        # Grey paved areas, excluding green vegetation, red roofs and deep shadows.
        self.mask = ((r + g + b >= 3 * 62) & (r + g + b <= 3 * 224) &
                     (np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b) <= 26) &
                     (2 * g - r - b <= 13))

    def depths(self, xy, outward):
        steps = np.arange(.5, PHOTO_WIDTH_M + .01, .5)
        tangent = np.c_[outward[:, 1], -outward[:, 0]]
        samples = xy[:, None, None, :] + steps[None, :, None, None] * outward[:, None, None, :]
        samples = samples + np.array([-.32, 0., .32])[None, None, :, None] * tangent[:, None, None, :]
        rows, cols = self.mask.shape
        x = np.floor((samples[..., 0] - self.x0) * cols / self.size).astype(int)
        y = np.floor((self.y0 + self.size - samples[..., 1]) * rows / self.size).astype(int)
        inside = (x >= 0) & (x < cols) & (y >= 0) & (y < rows)
        valid = self.mask[np.clip(y, 0, rows - 1), np.clip(x, 0, cols - 1)] & inside
        return .5 * np.logical_and.accumulate(valid.sum(axis=2) >= 2, axis=1).sum(axis=1)


@lru_cache(maxsize=6)
def _photo_reader(cache_dir, sources):
    import imagery
    import netfetch
    return imagery.Imagery(os.path.join(cache_dir, "imagery"), sources, fetch_workers=1, mem_tiles=18,
                           fetcher=netfetch.Fetcher(offline=True), leaf_on=False)


def cached_photo(frame, box, cache_dir):
    """Sample only high-resolution regional imagery that the game has already fetched."""
    import imagery
    import imagery_regional

    x0, y0, x1, y1 = box
    lat, lon, _ = frame.enu_to_geodetic((x0 + x1) / 2, (y0 + y1) / 2, 0.)
    sources = tuple(name for name, (_, _, (s, w, n, e), _, _) in imagery_regional.REGIONAL.items()
                    if s <= lat <= n and w <= lon <= e)
    if not sources:
        return None
    pad = BUILT_FRONT_M + 2.
    size = max(x1 - x0, y1 - y0) + 2 * pad
    x0, y0 = x0 - pad, y0 - pad
    px = 512 if size > 230. else 256
    try:
        rgb, used = _photo_reader(os.fspath(cache_dir), sources).render(frame, x0, y0, size, px)
    except imagery.ImageryError:
        return None
    if sum(used.get(name, 0) for name in sources) < px * px * .95:
        return None
    return PhotoPaving(rgb, x0, y0, size)


def tags_of(feats, i):
    values = feats.attrs.get("tags")
    if values is None or not values[i]:
        return {}
    try:
        tags = json.loads(str(values[i]))
    except (ValueError, TypeError):
        return {}
    return tags if isinstance(tags, dict) else {}


def _areas(frame, cells, box):
    """Paved destination polygons and areas to leave open, in the tile's frame."""
    import plazas
    import surface

    paved, protected, explicit, commercial = [], [], [], []
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
            cls = str(classes[i])
            tags = tags_of(feats, i)
            mat = str((materials[i] if materials is not None else None) or tags.get("surface") or "").strip().lower()
            area = tags.get("area:highway") in PAVED_AREAS or (
                tags.get("area") == "yes" and tags.get("highway") in PAVED_AREAS)
            parking = parks is not None and parks[i]
            square = squares is not None and squares[i]
            hard = (parking or square or area) and mat not in plazas.UNPAVED
            green = feats.layer == "water" or cls in GREEN or mat in plazas.UNPAVED
            if not (hard or green or cls in COMMERCIAL):
                continue
            for part in feats.parts(i):
                if not part or len(part[0]) < 3:
                    continue
                rings = [np.column_stack(mapgeom.enu_xy(feats, frame, ring)) for ring in part]
                if green:
                    protected.append(rings[0])
                elif hard:
                    paved.append(rings[0])
                    protected.extend(rings[1:])
                    if area and not (parking or square):
                        explicit.append((rings, mat))
                elif cls in COMMERCIAL:
                    commercial.append(rings[0])
    return paved, protected, explicit, commercial


def _walls(rings, lines=()):
    edges = [np.c_[ring, np.roll(ring, -1, axis=0)] for ring in rings]
    edges.extend(np.c_[line[:-1], line[1:]] for line in lines)
    return roadwidth.Walls(np.concatenate(edges) if edges else np.empty((0, 4)))


def _inside(points, rings):
    inside = np.zeros(len(points), bool)
    for ring in rings:
        lo, hi = ring.min(axis=0), ring.max(axis=0)
        ii = np.flatnonzero(~inside & np.all((points >= lo) & (points <= hi), axis=1))
        if len(ii):
            inside[ii] = polygon.contains_points(ring, points[ii])
    return inside


def _green_overlap(quads, rings, walls, lines=()):
    """Reject a crossing or a planted island wholly inside a connector."""
    ends = np.roll(quads, -1, axis=1)
    start = quads.reshape(-1, 2)
    delta = ends.reshape(-1, 2) - start
    length = np.linalg.norm(delta, axis=1)
    directions = delta / np.maximum(length, 1e-9)[:, None]
    hit = walls.rays(start, directions, max(float(length.max()), .01))
    overlap = ((hit <= length + .001) | _inside(start, rings)).reshape(quads.shape[:2]).any(axis=1)
    lo, hi = quads.min(axis=1), quads.max(axis=1)
    for ring in list(rings) + list(lines):
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


def _paving(poly, surface):
    import sidewalks
    repeat = sidewalks.BRICK_REPEAT_M if surface == "brick" else 2.
    return {"m": sidewalks.MATERIALS[sidewalks.EU_SLAB[surface]], "w": 0., "tl": repeat, "u_m": repeat,
            "p": 17, "layer": "edge", "poly": poly.tolist(), "n": []}


def _mapped_paving(explicit, protected, road_blocks):
    import buildings
    import europewalks
    import plazas
    result = []
    for rings, raw in explicit:
        outline, *holes = rings
        if np.linalg.norm(outline[0] - outline[-1]) < 1e-4:
            outline = outline[:-1]
        if len(outline) < 3:
            continue
        triangles = buildings.triangulate(outline)
        if len(triangles) == 0:
            continue
        points, triangles = plazas._subdivide(outline, triangles, 2.)
        pieces = points[triangles]
        exclusions = protected + holes
        if exclusions and len(pieces):
            pieces = pieces[~_green_overlap(pieces, exclusions, _walls(exclusions))]
        if len(pieces):
            pieces = pieces[~_road_overlap(pieces, road_blocks)]
        surface = europewalks.MAPPED_SURFACE.get(raw, "pavers")
        result.extend(_paving(piece, surface) for piece in pieces)
    return result


def _road_overlap(pieces, blockers):
    import fast
    if blockers is None:
        return np.zeros(len(pieces), dtype=bool)
    fast.require(0, "inside_blockers")
    samples = np.concatenate([pieces, (pieces + np.roll(pieces, -1, axis=1)) * .5,
                              pieces.mean(axis=1, keepdims=True)], axis=1)
    hit = fast._ef.inside_blockers(np.ascontiguousarray(samples.reshape(-1, 2)), *blockers)
    return np.asarray(hit).reshape(samples.shape[:2]).any(axis=1)


def _outside_side(line, carriageways):
    """Side away from the street for a separately mapped sidewalk, in either OSM direction."""
    import furniture
    middle = line[len(line) // 2, :2] if len(line) > 2 else line[:, :2].mean(axis=0)
    closest = None
    for _, spec, road, _ in carriageways:
        if spec.cls == "service":
            continue
        p, d = furniture._at(road, furniture._project(road, middle[None]))
        delta = middle - p[0, :2]
        distance = np.linalg.norm(delta)
        if not spec.width / 2 - .3 <= distance <= spec.width / 2 + 7.:
            continue
        if closest is None or distance < closest[0]:
            closest = distance, delta
    if closest is None:
        return None
    dx, dy = line[-1, :2] - line[0, :2]
    return 1 if np.dot([-dy, dx], closest[1]) > 0 else -1


def widen_walks(walks, photo, buildings, excluded, barriers, carriageways):
    """Widen an inferred sidewalk when a continuous paved strip follows it along the frontage."""
    import roads
    import sidewalks

    if photo is None or not buildings:
        return False
    frontage = _walls(buildings, barriers)
    limits = _walls(list(buildings) + excluded, barriers)
    road_blocks = roads._blocker_segments([(line[:, :2], spec.width / 2) for _, spec, line, _ in carriageways])
    changed = False
    for walk in walks:
        if ("pts" not in walk or not walk.get("europe") or walk.get("detached") or walk.get("median")
                or walk.get("mapped") or walk.get("bay_shifted") or walk["w"] >= 3.):
            continue
        line = roads.densify(np.asarray(walk["pts"], np.float64), 1.5)
        if np.linalg.norm(np.diff(line[:, :2], axis=0), axis=1).sum() < 8.:
            continue
        width = walk["w"]
        curb, out = sidewalks.run_geometry(line, walk["side"], width)
        back = curb[:, :2] + out * width
        norm = out / np.maximum(1e-9, np.linalg.norm(out, axis=1))[:, None]
        reach = frontage.rays(back, norm, BUILT_FRONT_M)
        if np.isfinite(reach).mean() < .8:
            continue
        clearance = limits.rays(back, norm, PHOTO_WIDTH_M)
        safe = np.minimum(photo.depths(back, norm), np.where(np.isfinite(clearance), clearance - .18, PHOTO_WIDTH_M))
        extra = min(4.8 - width, float(np.floor(np.percentile(safe, 15) * 4.) / 4.))
        if extra < .75:
            continue
        end = back + extra * out
        quads = np.stack((back[:-1], back[1:], end[1:], end[:-1]), axis=1)
        if _road_overlap(quads, road_blocks).any() or _green_overlap(
                quads, list(buildings) + excluded, limits, barriers).any():
            continue
        line[:, :2] += out * (extra / 2)
        walk["pts"], walk["w"] = line, width + extra
        changed = True
    return changed


def chunks(frame, walks, buildings, lots, carriageways, drives, box, *, shops=(), barriers=(),
           extend=True, residential=True, areas=None, photo=None):
    """Draped paving for bounded gaps. Longer aprons require mapped commercial use or a paved destination."""
    import roads
    import sidewalks

    candidates = [w for w in walks if w.get("surface") in sidewalks.EU_SLAB and "pts" in w
                  and (not w.get("detached") or w.get("mapped")) and not w.get("median")]
    paved, green, explicit, retail = _areas(frame, lots, box) if areas is None else areas
    targets = (list(buildings) if residential else list(shops)) + paved
    walls = _walls(targets)
    hard_walls = _walls(list(shops) + paved)
    green_walls = _walls(green, barriers)
    blockers = [(p[:, :2], spec.width / 2) for _, spec, p, _ in carriageways]
    blockers += [(np.asarray(d["n"], np.float64)[:, :2], float(d["w"]) / 2) for d in drives]
    blocks = roads._blocker_segments(blockers) if blockers else None
    result = _mapped_paving(explicit, green + list(buildings), blocks)
    result.extend(_access_chunks(candidates, paved, green, carriageways))
    if not targets:
        return result
    for walk in candidates:
        line = roads.densify(np.asarray(walk["pts"]), STEP_M)
        side = _outside_side(line, carriageways) if walk.get("mapped") else walk["side"]
        if side is None:
            continue
        curb, normal = sidewalks.run_geometry(line, side, walk["w"])
        back = curb[:, :2] + walk["w"] * normal
        normal /= np.maximum(np.linalg.norm(normal, axis=1), 1e-9)[:, None]
        limit = BUILT_FRONT_M if extend and (retail or shops or paved or photo is not None) else MAX_GAP_M
        dist = walls.rays(back, normal, limit)
        end = back + normal * (np.minimum(dist, limit) - .02)[:, None]
        long = dist > MAX_GAP_M
        confirmed = ~long
        hard_dist = hard_walls.rays(back, normal, limit) if shops or paved else np.full(len(back), np.inf)
        hard_target = np.isfinite(dist) & np.isclose(hard_dist, dist, rtol=0., atol=.08)
        if long.any() and limit > MAX_GAP_M:
            checked = np.flatnonzero(long)
            confirmed[checked] = hard_target[checked] | (
                _inside(back[checked], retail) & _inside(end[checked], retail))
        valid = (dist >= .15) & (dist <= limit) & confirmed & ~_inside(back, targets)
        if walk.get("mapped"):
            # A park path with a nearby building is not evidence of a paved yard.
            valid &= _inside(back, retail) | hard_target
        if photo is not None and extend:
            # A hard strip must still be beside a mapped frontage or commercial land.
            # The first garden, wall or building bounds the result even if the photo is pale beyond it.
            bounds = green_walls.rays(back, normal, BUILT_FRONT_M)
            next_edge = np.fmin(dist, bounds)
            urban_edge = np.isfinite(dist) | np.isfinite(bounds) | _inside(back, retail)
            depth = np.minimum(photo.depths(back, normal), np.where(np.isfinite(next_edge), next_edge, PHOTO_WIDTH_M))
            observed = (depth >= .9) & urban_edge & ~_inside(back, targets)
            extend_here = observed & (~valid | (depth > dist))
            dist[extend_here] = depth[extend_here]
            valid |= extend_here
            end = back + normal * (np.minimum(dist, limit) - .04)[:, None]
        else:
            extend_here = np.zeros(len(back), bool)
        i = np.flatnonzero(valid[:-1] & valid[1:] & (np.abs(np.diff(dist)) <= 1.1))
        if not len(i):
            continue
        middle = (back[i] + back[i + 1]) * .5
        outward = (normal[i] + normal[i + 1]) * .5
        outward /= np.maximum(np.linalg.norm(outward, axis=1), 1e-9)[:, None]
        mid_dist = walls.rays(middle, outward, limit)
        # Avoid spanning a gap between two unrelated façades.
        expected = .5 * (dist[i] + dist[i + 1])
        connected = np.abs(mid_dist - expected) <= .45
        if photo is not None and extend:
            connected |= ((extend_here[i] | extend_here[i + 1]) &
                          (photo.depths(middle, outward) + .15 >= expected))
        i = i[connected]
        if not len(i):
            continue
        quads = np.stack([back[i], back[i + 1], end[i + 1], end[i]], axis=1)
        if len(quads):
            quads = quads[~_road_overlap(quads, blocks)]
        if len(quads) and (green or barriers):
            quads = quads[~_green_overlap(quads, green, green_walls, barriers)]
        result.extend(_paving(quad, walk["surface"]) for quad in quads)
    return result


def country_at_tile(frame, box):
    import europewalks
    if not europewalks.enabled():
        return None
    lat, lon, _ = frame.enu_to_geodetic((box[0] + box[2]) / 2, (box[1] + box[3]) / 2, 0.)
    return europewalks.country_for(float(lon), float(lat))


def barrier_lines(frame, features, box):
    """Mapped boundaries block inferred ground and vehicles even where the model is not drawn."""
    classes = {"fence", "wall", "hedge", "ditch", "drain", "retaining_wall", "noise_barrier", "guard_rail", "guardrail"}
    out, seen = [], set()
    x0, y0, x1, y1 = box
    for feats in features:
        if feats is None or not len(feats) or "class" not in feats.attrs:
            continue
        bounds = feats.boxes(frame)
        near = (bounds[:, 2] >= x0) & (bounds[:, 0] <= x1) & (bounds[:, 3] >= y0) & (bounds[:, 1] <= y1)
        for i in np.flatnonzero(near & np.isin(feats.attrs["class"], list(classes)) &
                                np.isin(feats.kind, [mapgeom.LINE, mapgeom.POLYGON])):
            fid = str(feats.ids[i])
            if fid in seen:
                continue
            seen.add(fid)
            for part in feats.parts(i):
                for ring in part:
                    if len(ring) >= 2:
                        out.append(np.column_stack(mapgeom.enu_xy(feats, frame, ring)))
    return out


def tile(frame, box, features, walks, rings, items, carriageways, driveways, drives, z_at, country, left_hand,
         photo=None, redrape=False):
    """Common street frontage pass for anchor tiles and retained geotiles."""
    import buildings
    import europewalks
    import streetparking
    from francehouses import HOUSE_CLASSES, UNTYPED

    x0, y0, x1, y1 = box
    margin = BUILT_FRONT_M + 2.
    near = (x0 - margin, y0 - margin, x1 + margin, y1 + margin)
    origin = np.array([x0, y0])
    shops, homes = [], []
    for spec, *_ in items:
        if getattr(spec, "deck", None) or (spec.min_h or 0.) > .5:
            continue
        ring = buildings.drawn_ring(spec) + origin
        if spec.cls in COMMERCIAL:
            shops.append(ring)
        if (spec.cls or "") in HOUSE_CLASSES | UNTYPED and (spec.floors or 0) <= 3:
            homes.append(ring)
    lat, lon, _ = frame.enu_to_geodetic((x0 + x1) / 2, (y0 + y1) / 2, 0.)
    _, locality = europewalks._locality(country, float(lon), float(lat))
    rural_ro = country == "RO" and locality not in {"city", "town"}
    areas = _areas(frame, features, near)
    barriers = barrier_lines(frame, features, near)
    bays = list(streetparking.areas(frame, box, features))
    shifted = streetparking.adjust_walks(walks, bays, list(rings) + areas[1], barriers)
    widened = widen_walks(walks, photo if not rural_ro else None, rings,
                         areas[1] + [parts[0] for _, _, parts, _ in bays], barriers, carriageways)
    if (shifted or widened) and redrape:
        import roads
        roads.drape_walks(frame, None, walks, z_at)
    paved = chunks(frame, walks, rings, features, carriageways, drives, near, shops=shops, barriers=barriers,
                   extend=not rural_ro, residential=not rural_ro, areas=areas, photo=photo)
    spots, marks = streetparking.tile_layout(frame, box, features, carriageways, driveways, walks, rings, homes,
                                            barriers, areas[1], z_at, left_hand, bays)
    return paved + marks, spots


def finish(frame, tx, ty, md, walks, rings, items, carriageways, driveways, drives, z_at, country, left_hand,
           redrape=False):
    import logging
    import roads

    if country is None:
        return [], []
    try:
        features = [layer for key in roads.cells_of_tile(frame, tx, ty, BUILT_FRONT_M + 2.)
                    for layer in md.cell(key, layers=("land_use", "infrastructure", "water"), kind="demand").layers.values()]
    except (OSError, RuntimeError, ValueError) as exc:
        logging.getLogger("earthd.roads").debug("street frontage data unavailable: %s", exc)
        return [], []
    box = roads.tile_bbox(tx, ty)
    cache_dir = getattr(md, "cache_dir", None)
    photo = cached_photo(frame, box, cache_dir) if cache_dir and walks and rings and country != "RO" else None
    return tile(frame, box, features, walks, rings, items, carriageways, driveways, drives, z_at, country,
                left_hand, photo, redrape)
