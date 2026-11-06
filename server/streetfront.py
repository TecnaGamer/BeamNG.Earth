"""Paved ground between a European street and its mapped frontage."""

from functools import lru_cache
import json
import os

import numpy as np

import mapgeom
import polygon
import roadwidth

MAX_GAP_M = 2.4
BUILT_FRONT_M = 15.0
STEP_M = 1.5
GREEN = {"garden", "grass", "flowerbed", "grassland", "meadow", "park", "village_green",
         "allotments", "hedge", "orchard", "wood", "forest", "cemetery", "farmland",
         "playground", "recreation_ground", "water", "wetland"}
COMMERCIAL = {"retail", "commercial", "shopping_centre", "supermarket", "office"}
PAVED_AREAS = {"footway", "pedestrian", "cycleway", "living_street", "service"}
PHOTO_WIDTH_M = 5.5
PAVER_SURFACES = {"pavers", "pavers_mixed", "slabs", "stone", "brick"}


class PhotoPaving:
    """Hard ground and vegetation signals from the cached regional orthophoto."""

    def __init__(self, rgb, x0, y0, size):
        self.x0, self.y0, self.size = x0, y0, size
        r, g, b = np.asarray(rgb, dtype=np.int16).transpose(2, 0, 1)
        # Grey paved areas, excluding green vegetation, red roofs and deep shadows.
        self.mask = ((r + g + b >= 3 * 62) & (r + g + b <= 3 * 224) &
                     (np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b) <= 26) &
                     (2 * g - r - b <= 13))
        self.green = (g >= 38) & (g - r >= 6) & (2 * g - r - b >= 20)

    def sample(self, mask, samples):
        rows, cols = mask.shape
        x = np.floor((samples[..., 0] - self.x0) * cols / self.size).astype(int)
        y = np.floor((self.y0 + self.size - samples[..., 1]) * rows / self.size).astype(int)
        inside = (x >= 0) & (x < cols) & (y >= 0) & (y < rows)
        return mask[np.clip(y, 0, rows - 1), np.clip(x, 0, cols - 1)] & inside

    def depths(self, xy, outward):
        steps = np.arange(.5, PHOTO_WIDTH_M + .01, .5)
        tangent = np.c_[outward[:, 1], -outward[:, 0]]
        samples = xy[:, None, None, :] + steps[None, :, None, None] * outward[:, None, None, :]
        samples = samples + np.array([-.32, 0., .32])[None, None, :, None] * tangent[:, None, None, :]
        valid = self.sample(self.mask, samples)
        return .5 * np.logical_and.accumulate(valid.sum(axis=2) >= 2, axis=1).sum(axis=1)

    def strip_share(self, strips, mask):
        """Share of matching pixels over each four-corner strip, away from its edges."""
        across = np.array([.18, .5, .82])[None, :, None]
        along = np.linspace(.08, .92, 12)[None, None, :, None]
        a = strips[:, :1] * (1 - across) + strips[:, 1:2] * across
        b = strips[:, 3:] * (1 - across) + strips[:, 2:3] * across
        return self.sample(mask, a[:, :, None] * (1 - along) + b[:, :, None] * along).mean(axis=(1, 2))


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

    paved, protected, explicit, commercial, residential = [], [], [], [], []
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
            courtyard = cls in {"courtyard", "driveway"} and bool(mat)
            hard = (parking or square or area or courtyard) and mat not in plazas.UNPAVED
            green = feats.layer == "water" or cls in GREEN or mat in plazas.UNPAVED
            if not (hard or green or cls in COMMERCIAL or cls == "residential"):
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
                    if (area or courtyard) and not (parking or square):
                        explicit.append((rings, mat))
                elif cls in COMMERCIAL:
                    commercial.append(rings[0])
                else:
                    residential.append(rings[0])
    return paved, protected, explicit, commercial, residential


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
            "p": 17, "layer": "edge", "poly": poly.tolist(), "n": [], "frontage": True}


def pad_items(frame, box, tile_key, paving, grass=()):
    """The paving and photographed grass footprints for the terrain's material layers."""
    import surface

    def rings_in_tile(polygons):
        rings = [r for poly in polygons if (r := mapgeom.clip_ring(np.asarray(poly), box)) is not None]
        if not rings:
            return []
        xy = np.concatenate(rings)
        lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
        ends = np.cumsum([len(ring) for ring in rings])[:-1]
        return list(zip(np.split(lat, ends), np.split(lon, ends)))

    return [(surface.FRONTAGE_PAD_PREFIX + tile_key,
             rings_in_tile(c["poly"] for c in paving if c.get("frontage"))),
            (surface.FRONTAGE_GRASS_PREFIX + tile_key, rings_in_tile(grass))]


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


def _road_edges(carriageways, box):
    """A frontage can start at the road edge on a street with no drawn sidewalk."""
    import europewalks
    import roads

    for fid, spec, points, _ in carriageways:
        if (spec.cls not in europewalks.STREETS or spec.kind != "paved" or spec.link or spec.median
                or not (spec.urban or spec.street and spec.street.village)):
            continue
        for side in (1, -1):
            edge = roads.offset_polyline(points, side * (spec.width / 2 + .12))
            for piece in roads.clip_polyline(edge, *box):
                yield {"pts": piece, "side": side, "w": 0., "fid": fid, "road_edge": True}


def _street_edge_distance(points, carriageways):
    import furniture

    distance = np.full(len(points), np.inf)
    for _, spec, road, _ in carriageways:
        if spec.cls == "service":
            continue
        lo, hi = road[:, :2].min(axis=0) - 9., road[:, :2].max(axis=0) + 9.
        near = np.flatnonzero(np.all((points >= lo) & (points <= hi), axis=1))
        if len(near):
            p, _ = furniture._at(road, furniture._project(road, points[near]))
            gap = np.abs(np.linalg.norm(p[:, :2] - points[near], axis=1) - spec.width / 2)
            distance[near] = np.minimum(distance[near], gap)
    return distance


def chunks(frame, walks, buildings, lots, carriageways, drives, box, *, shops=(), barriers=(),
           extend=True, residential=True, areas=None, photo=None, driveways=(), grass=None):
    """Draped paving between a developed street edge and its buildings or mapped hard ground."""
    import europewalks
    import roads
    import sidewalks

    candidates = [w for w in walks if w.get("surface") in sidewalks.EU_SLAB and "pts" in w
                  and (not w.get("detached") or w.get("mapped")) and not w.get("median")]
    paved, green, explicit, retail, _ = _areas(frame, lots, box) if areas is None else areas
    targets = (list(buildings) if residential else list(shops)) + paved
    walls = _walls(targets)
    hard_walls = _walls(list(shops) + paved)
    green_walls = _walls(green, barriers)
    blockers = [(p[:, :2], spec.width / 2) for _, spec, p, _ in carriageways]
    blockers += [(np.asarray(d["n"], np.float64)[:, :2], float(d["w"]) / 2) for d in drives]
    blocks = roads._blocker_segments(blockers) if blockers else None
    walk_blocks = roads._blocker_segments([(w["pts"][:, :2], w["w"] / 2 + .13) for w in walks
                                          if "pts" in w and not w.get("median")])
    driveway_styles = {}
    for _, spec, points in driveways:
        style = europewalks.MAPPED_SURFACE.get(spec.surf)
        if style in PAVER_SURFACES:
            driveway_styles.setdefault(style, []).append((points[:, :2], spec.width / 2 + 1.2))
    driveway_styles = {style: roads._blocker_segments(lines) for style, lines in driveway_styles.items()}
    result = _mapped_paving(explicit, green + list(buildings), blocks)
    result.extend(_access_chunks(candidates, paved, green, carriageways))
    if not targets:
        return result
    if extend and residential:
        candidates.extend(_road_edges(carriageways, box))
    for walk in candidates:
        line = roads.densify(np.asarray(walk["pts"]), STEP_M)
        side = _outside_side(line, carriageways) if walk.get("mapped") else walk["side"]
        if side is None:
            continue
        curb, normal = sidewalks.run_geometry(line, side, walk["w"])
        back = curb[:, :2] + walk["w"] * normal
        normal /= np.maximum(np.linalg.norm(normal, axis=1), 1e-9)[:, None]
        limit = BUILT_FRONT_M if extend else MAX_GAP_M
        if walk.get("road_edge"):
            limit = min(limit, 10.)
        dist = walls.rays(back, normal, limit)
        limit_depth = green_walls.rays(back, normal, limit)
        hard_dist = hard_walls.rays(back, normal, limit) if shops or paved else np.full(len(back), np.inf)
        hard_target = np.isfinite(dist) & np.isclose(hard_dist, dist, rtol=0., atol=.08)
        free = ~_inside(back, targets + green)
        valid = free & np.isfinite(dist) & (dist >= .18)
        if walk.get("mapped"):
            # A mapped sidewalk's position must agree with the street edge.
            road_edge = _street_edge_distance(back, carriageways)
            valid &= _inside(back, retail) | hard_target | (road_edge <= 2.8)
        observed = np.zeros(len(back), bool)
        depth = np.fmin(dist, limit_depth)
        if photo is not None and extend:
            photo_depth = np.fmin(photo.depths(back, normal), depth)
            observed = free & ~valid & (photo_depth >= .9) & (np.isfinite(dist) | _inside(back, retail))
            depth[observed] = photo_depth[observed]
            valid |= observed
        valid &= (depth >= .18) & np.isfinite(depth)
        depth = np.where(valid, depth, 0.)
        end = back + normal * np.maximum(depth - .07, 0.)[:, None]
        i = np.flatnonzero(valid[:-1] & valid[1:] & (np.abs(np.diff(depth)) <= 1.65))
        if not len(i):
            continue
        middle = (back[i] + back[i + 1]) * .5
        outward = (normal[i] + normal[i + 1]) * .5
        outward /= np.maximum(np.linalg.norm(outward, axis=1), 1e-9)[:, None]
        mid_dist = np.fmin(walls.rays(middle, outward, limit), green_walls.rays(middle, outward, limit))
        expected = np.minimum(depth[i], depth[i + 1])
        connected = np.isfinite(mid_dist) & (np.abs(mid_dist - expected) <= 1.8)
        if photo is not None and (observed[i] | observed[i + 1]).any():
            connected |= ((observed[i] | observed[i + 1])
                          & (photo.depths(middle, outward) + .15 >= expected))
        i = i[connected]
        quads = np.stack([back[i], back[i + 1], end[i + 1], end[i]], axis=1)
        if len(quads):
            quads = quads[~_road_overlap(quads, blocks)]
        if len(quads) and walk.get("road_edge"):
            quads = quads[~_road_overlap(quads, walk_blocks)]
        if len(quads) and (green or barriers):
            quads = quads[~_green_overlap(quads, green, green_walls, barriers)]
        if len(quads) and photo is not None:
            share = photo.strip_share(quads, photo.green)
            if grass is not None:
                grass.extend(quads[share >= .64])
            quads = quads[share < .23]
        if not len(quads):
            continue
        style = walk.get("surface") if walk.get("mapped") and walk.get("surface") in PAVER_SURFACES else "concrete"
        styles = np.full(len(quads), style, dtype=object)
        for driveway_style, bounds in driveway_styles.items():
            styles[_road_overlap(quads, bounds)] = driveway_style
        result.extend(_paving(quad, style) for quad, style in zip(quads, styles))
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
         photo=None, redrape=False, props=None, grass=None):
    """Common street frontage pass for anchor tiles and retained geotiles."""
    import buildings
    import europewalks
    import streetparking
    from francehouses import HOUSE_CLASSES, UNTYPED

    x0, y0, x1, y1 = box
    margin = BUILT_FRONT_M + 2.
    near = (x0 - margin, y0 - margin, x1 + margin, y1 + margin)
    origin = np.array([x0, y0])
    shops, homes, yards = [], [], []
    for spec, *_ in items:
        if getattr(spec, "deck", None) or (spec.min_h or 0.) > .5:
            continue
        ring = buildings.drawn_ring(spec) + origin
        if spec.cls in COMMERCIAL:
            shops.append(ring)
        if (spec.cls or "") in HOUSE_CLASSES | UNTYPED and (spec.floors or 0) <= 3:
            homes.append(ring)
            if (country == "FR" and props is not None and getattr(spec, "door", None) is not None
                    and not spec.shop and spec.cls != "terrace" and (spec.cls in HOUSE_CLASSES or spec.roof != "flat")
                    and 35. <= abs(buildings._area2(ring)) / 2 <= 340.):
                yards.append((spec, ring))
    lat, lon, _ = frame.enu_to_geodetic((x0 + x1) / 2, (y0 + y1) / 2, 0.)
    open_verge = europewalks.grassy_verges(country, float(lon), float(lat))
    areas = _areas(frame, features, near)
    barriers = barrier_lines(frame, features, near)
    bays = list(streetparking.areas(frame, box, features))
    shifted = streetparking.adjust_walks(walks, bays, list(rings) + areas[1], barriers)
    widened = widen_walks(walks, photo if not open_verge else None, rings,
                         areas[1] + [parts[0] for _, _, parts, _ in bays], barriers, carriageways)
    if (shifted or widened) and redrape:
        import roads
        roads.drape_walks(frame, None, walks, z_at)
    paved = chunks(frame, walks, rings, features, carriageways, drives, near, shops=shops, barriers=barriers,
                   extend=not open_verge, residential=not open_verge, areas=areas, photo=photo, driveways=driveways,
                   grass=grass)
    spots, marks = streetparking.tile_layout(frame, box, features, carriageways, driveways, walks, rings, homes,
                                            barriers, areas[1], z_at, left_hand, bays)
    if yards:
        import fences
        props.extend(fences.inferred_rows(frame, yards, rings, walks, carriageways, barriers, areas[0],
                                         areas[1], drives, z_at, photo, residential=areas[4]))
    return paved + marks, spots


def finish(frame, tx, ty, md, walks, rings, items, carriageways, driveways, drives, z_at, country, left_hand,
           redrape=False, props=None, grass=None):
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
    photo = cached_photo(frame, box, cache_dir) if cache_dir and rings else None
    return tile(frame, box, features, walks, rings, items, carriageways, driveways, drives, z_at, country,
                left_hand, photo, redrape, props, grass)
