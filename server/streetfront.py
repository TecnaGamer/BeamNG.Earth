"""Paved ground between a European street and its mapped frontage."""

from functools import lru_cache
import json
import os

import numpy as np
import dnp

import mapgeom
import polygon
import roadwidth

MAX_GAP_M = 2.4
BUILT_FRONT_M = 18.0
STEP_M = 1.5
FRONTAGE_JOIN_M = 7.5
FRONTAGE_FLANK_M = 4.
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

    def _transects(self, mask, xy, outward, steps, half_width):
        tangent = np.c_[outward[:, 1], -outward[:, 0]]
        samples = xy[:, None, None, :] + steps[None, :, None, None] * outward[:, None, None, :]
        samples = samples + np.array([-half_width, 0., half_width])[None, None, :, None] * tangent[:, None, None, :]
        return self.sample(mask, samples).sum(axis=2)

    def depths(self, xy, outward):
        steps = np.arange(.5, PHOTO_WIDTH_M + .01, .5)
        paved = self._transects(self.mask, xy, outward, steps, .32) >= 2
        return .5 * np.logical_and.accumulate(paved, axis=1).sum(axis=1)

    def lawn_edges(self, xy, outward):
        """Stop at a sustained patch of vegetation seen across a whole strip."""
        steps = np.arange(.25, BUILT_FRONT_M + .5, .5)
        green = self._transects(self.green, xy, outward, steps, .55) >= 2
        spans = green[:, :-2] & green[:, 1:-1] & green[:, 2:]
        return np.where(spans.any(axis=1), .5 * spans.argmax(axis=1), np.inf)

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
    import buildings
    import imagery

    x0, y0, x1, y1 = box
    sources = buildings._photo_sources(frame, x0, y0, x1, y1)     # (the roofs' photos: Maryland's md6in too)
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

    paved, protected, explicit, commercial, residential, public_green = [], [], [], [], [], []
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
                    if cls in GREEN - {"grass", "flowerbed", "garden", "hedge"} or tags.get("access") == "public":
                        public_green.append(rings[0])
                elif hard:
                    paved.append(rings[0])
                    protected.extend(rings[1:])
                    if (area or courtyard) and not (parking or square):
                        explicit.append((rings, mat))
                elif cls in COMMERCIAL:
                    commercial.append(rings[0])
                else:
                    residential.append(rings[0])
    return paved, protected, explicit, commercial, residential, public_green


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
    length = dnp.norm(delta, axis=1)
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
        length = dnp.norm(np.diff(access[:, :2], axis=0), axis=1).sum()
        if not (1. <= length <= 16.):
            continue
        # The service way must actually reach a sidewalk at this end. A
        # parking aisle between two parcels is already painted as ground.
        if not any(roads._seg_dist_min(walk, p) <= hw + spec.width / 2 + .5
                   for walk, hw in walks for p in access[:-1, :2]):
            continue
        for run in roads.cut_outside(access, through, .5, min_piece=1.):
            chunks.append(roads._chunk(roads.look_of(spec, fid), spec.width, run))
    return chunks


def _paving(poly, surface):
    import roadmesh
    import sidewalks
    if surface == "road_asphalt":
        material, repeat = roadmesh.FRONTAGE_ASPHALT, roadmesh.FRONTAGE_REPEAT_M
    else:
        material = sidewalks.MATERIALS[sidewalks.EU_SLAB[surface]]
        repeat = sidewalks.BRICK_REPEAT_M if surface == "brick" else 2.
    return {"m": material, "w": 0., "tl": repeat, "u_m": repeat,
            "p": 17, "layer": "frontage", "poly": poly.tolist(), "n": [], "frontage": True}


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
        distance = dnp.norm(delta)
        if not spec.width / 2 - .3 <= distance <= spec.width / 2 + 7.:
            continue
        if closest is None or distance < closest[0]:
            closest = distance, delta
    if closest is None:
        return None
    dx, dy = line[-1, :2] - line[0, :2]
    return 1 if dnp.dot([-dy, dx], closest[1]) > 0 else -1


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
        if dnp.norm(np.diff(line[:, :2], axis=0), axis=1).sum() < 8.:
            continue
        width = walk["w"]
        curb, out = sidewalks.run_geometry(line, walk["side"], width)
        back = curb[:, :2] + out * width
        norm = out / np.maximum(1e-9, dnp.norm(out, axis=1))[:, None]
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
            edge = roads.offset_polyline(points, side * spec.width / 2)
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
            gap = np.abs(dnp.norm(p[:, :2] - points[near], axis=1) - spec.width / 2)
            distance[near] = np.minimum(distance[near], gap)
    return distance


def _walk_outline(walk):
    """The two sides of the slab, including its corner pieces."""
    import sidewalks

    return sidewalks._slab_edges(walk)


def _band(a, b):
    """Strips between paired outlines, combining straight runs before clipping."""
    import sidewalks

    keep = sidewalks.simplify(np.c_[a, b], .015)
    a, b = a[keep], b[keep]
    return zip(a[:-1], a[1:], b[1:], b[:-1])


def _coverage(box, walks, buildings, green, roads_and_drives):
    import frontage_geometry
    import roads

    coverage = frontage_geometry.Coverage(box)
    for line, half in roads_and_drives:
        for strip in _band(roads.offset_polyline(line, half)[:, :2], roads.offset_polyline(line, -half)[:, :2]):
            coverage.reserve(strip)
    for walk in walks:
        if "corner" in walk or "pts" in walk:
            curb, back = _walk_outline(walk)
            if "corner" in walk:
                coverage.reserve(np.concatenate([curb, back[::-1]]))
                corner = walk["corner"]
                for fill in (corner.get("fill"), *corner.get("fills", ())):
                    if fill is not None:
                        coverage.reserve(fill)
            else:
                for strip in _band(curb, back):
                    coverage.reserve(strip)
    for ring in (*buildings, *green):
        coverage.reserve(ring)
    return coverage


def _connected_depths(back, depth):
    """Join nearby setbacks along a street and ease the ends around a building's flank."""
    known = np.flatnonzero(np.isfinite(depth))
    if not len(known):
        return depth
    arc = np.r_[0., np.cumsum(dnp.norm(np.diff(back, axis=0), axis=1))]
    unknown = np.flatnonzero(~np.isfinite(depth))
    j = np.searchsorted(known, unknown)
    left, right = known[np.maximum(j - 1, 0)], known[np.minimum(j, len(known) - 1)]
    a, b = np.abs(arc[unknown] - arc[left]), np.abs(arc[right] - arc[unknown])
    between = (left != right) & (a + b <= FRONTAGE_JOIN_M)
    near = np.minimum(a, b)
    flank = near < FRONTAGE_FLANK_M
    taper = np.where(a <= b, depth[left], depth[right]) * (1 - near / FRONTAGE_FLANK_M)
    depth[unknown[flank]] = taper[flank]
    depth[unknown[between]] = np.interp(arc[unknown[between]], arc[known], depth[known])
    return depth


def _back_edge(walk, carriageways):
    import buildings
    import roads
    import sidewalks

    curb, back = _walk_outline(walk)
    if len(back) < 2:
        return None, None
    if "corner" in walk:
        side = -1 if buildings._area2(np.concatenate([curb, back[::-1]])) < 0. else 1
    else:
        side = _outside_side(back, carriageways) if walk.get("mapped") else walk["side"]
    if side is None:
        return None, None
    back = back[sidewalks.simplify(back, .025)]
    back = roads.densify(np.c_[back, np.zeros(len(back))], STEP_M)[:, :2]
    normal = sidewalks._normals_2d(back) * side
    normal /= np.maximum(dnp.norm(normal, axis=1), 1e-9)[:, None]
    return back, normal


def chunks(frame, walks, buildings, lots, carriageways, drives, box, *, shops=(), barriers=(),
           extend=True, residential=True, areas=None, photo=None, grass=None, bays=()):
    """Give the developed part of the street a continuous surface from the curb to the frontage."""
    import europewalks
    import roads
    import sidewalks

    candidates = [w for w in walks if w.get("surface") in sidewalks.EU_SLAB
                  and (not w.get("detached") or w.get("mapped")) and not w.get("median")]
    paved, green, explicit, retail, *_ = _areas(frame, lots, box) if areas is None else areas
    targets = (list(buildings) if residential else list(shops)) + paved
    access = _access_chunks([w for w in candidates if "pts" in w], paved, green, carriageways)
    if not (targets or explicit):
        return access
    walls = _walls(targets)
    hard_walls = _walls(list(shops) + paved)
    green_walls = _walls(green, barriers)
    street_sides = [roads.offset_polyline(p, side * spec.width / 2)[:, :2]
                    for _, spec, p, _ in carriageways if spec.cls != "service" for side in (1, -1)]
    street_walls = _walls([], street_sides)
    bands = [(p, spec.width / 2) for _, spec, p, _ in carriageways]
    bands += [(np.asarray(d["n"], np.float64), float(d["w"]) / 2) for d in (*drives, *access)]
    explicit_outlines = {id(rings[0]) for rings, _ in explicit}
    reserved = [ring for ring in paved if id(ring) not in explicit_outlines]
    reserved.extend(rings[0] for _, _, rings, _ in bays)
    reserved.extend(hole for rings, _ in explicit for hole in rings[1:])
    coverage = _coverage(box, walks, buildings, [*green, *reserved], bands)
    result = list(access)
    for rings, raw in explicit:
        surface = europewalks.MAPPED_SURFACE.get(raw, "pavers")
        result.extend(_paving(part, surface) for part in coverage.claim(rings[0]))
    # Mapped paving is continuous with its sidewalk. Inferred sidewalks and
    # street edges share one finish for the rest of the frontage.
    candidates.sort(key=lambda w: not w.get("mapped"))
    if extend and residential:
        candidates.extend(_road_edges(carriageways, box))
    for walk in candidates:
        back, normal = _back_edge(walk, carriageways)
        if back is None:
            continue
        limit = BUILT_FRONT_M if extend else MAX_GAP_M
        if walk.get("road_edge"):
            limit = min(limit, 15.)
        dist = walls.rays(back, normal, limit)
        free = ~_inside(back, targets + green)
        valid = free & np.isfinite(dist) & (dist >= .18)
        if walk.get("mapped"):
            # A mapped sidewalk's position must agree with the street edge.
            hard_dist = hard_walls.rays(back, normal, limit)
            road_edge = _street_edge_distance(back, carriageways)
            valid &= _inside(back, retail) | np.isclose(hard_dist, dist, rtol=0., atol=.08) | (road_edge <= 3.2)
        depth = np.where(valid, dist, np.nan)
        if photo is not None and extend and retail:
            photo_depth = photo.depths(back, normal)
            observed = free & ~valid & (photo_depth >= 1.5) & _inside(back, retail)
            depth[observed] = photo_depth[observed]
        if extend:
            depth = _connected_depths(back, depth)
        limits = green_walls.rays(back, normal, limit)
        across = street_walls.rays(back + .06 * normal, normal, limit)
        limits = np.fmin(limits, across + .06)
        depth = np.where(free, np.minimum(depth, np.where(np.isnan(limits), limit, limits)), np.nan)
        i = np.flatnonzero(np.isfinite(depth[:-1]) & np.isfinite(depth[1:]))
        if not len(i):
            continue
        end = back + normal * np.maximum(depth - .07, 0.)[:, None]
        if photo is not None:
            lawn = np.minimum(photo.lawn_edges(back, normal), depth)
            boundary = back + normal * np.maximum(lawn - .07, 0.)[:, None]
            if grass is not None:
                j = i[(depth[i] - lawn[i] >= .35) & (depth[i + 1] - lawn[i + 1] >= .35)]
                lawns = np.stack([boundary[j], boundary[j + 1], end[j + 1], end[j]], axis=1)
                if len(lawns):
                    grass.extend(lawns[photo.strip_share(lawns, photo.green) >= .58])
            end = boundary
        style = walk.get("surface") if walk.get("mapped") and walk.get("surface") in PAVER_SURFACES else "road_asphalt"
        start = back - .035 * normal
        for run in np.split(i, np.flatnonzero(np.diff(i) != 1) + 1):
            at = slice(run[0], run[-1] + 2)
            for strip in _band(start[at], end[at]):
                result.extend(_paving(part, style) for part in coverage.claim(strip))
    return result


# Outside Europe's street details (country_at_tile None): a city's sidewalks run to the buildings' faces. Downtown
# Baltimore (39.290 -76.615, the aerial 2026-11-10) is concrete and brick from the curb to the walls; the drawn sidewalk
# (2.5 m, or roadwidth's share of a walled street) left terrain grass between it and the building (the user,
# 2026-11-10: "sidewalks should extend up to most city buildings"). A first try painted the gap at the ground's level
# ("you just paint a sidewalk texture over the gap instead of actually filling it in"): the sidewalk itself is widened
# instead, its slab (raised, its collision) carried out to the building line, the curb where it was (as widen_walks
# does in Europe). A run is widened where city buildings (city_targets) stand behind CITY_HIT of it within
# CITY_FRONT_M, to CITY_PCT of their distances: the slab's back meets most of the block's walls and runs on inside
# the nearer ones (the user: "its fine if the sidewalks clip into the building a bit. no one will notice from the
# outside"); never across a street, mapped green or a barrier; CITY_WALK_MAX m wide at most. Houses keep their lawns.
CITY_ON = os.environ.get("BNE_CITY_FRONTAGE", "on") != "off"
CITY_FRONT_M = 6.0
CITY_FLOORS = 3
CITY_DENSE_COVER = 0.25
CITY_HIT = 0.6
CITY_PCT = 70
CITY_MIN_EXTRA_M = 0.5
CITY_WALK_MAX = 8.0
CITY_CLASSES = COMMERCIAL | {"commercial", "retail", "office", "hotel", "civic", "public", "government", "bank",
                             "apartments", "college", "university", "hospital", "train_station", "transportation",
                             "church", "cathedral", "mixed_use", "parking", "warehouse"}


def city_targets(items, origin, dense):
    """The drawn rings (tile frame + origin) of the city buildings of a road tile's items (city_frontage)."""
    import buildings
    from francehouses import HOUSE_CLASSES, UNTYPED

    out = []
    for spec, *_ in items:
        if getattr(spec, "deck", None) or (spec.min_h or 0.) > .5 or spec.cls in buildings.SHED_CLASSES \
                or spec.cls in ("tank", "greenhouse", "pyramid", "bridge_part"):
            continue
        cls = spec.cls or ""
        floors = spec.floors or 0
        house = cls in HOUSE_CLASSES or (cls in UNTYPED and floors <= 2 and not spec.shop)
        if spec.shop or cls in CITY_CLASSES or floors >= CITY_FLOORS or (dense and not house):
            out.append(buildings.drawn_ring(spec) + origin)
    return out


def widen_city_walks(walks, targets, rings, excluded, barriers, carriageways):
    """Widen the sidewalk runs out to the city buildings behind them (in place; see CITY_ON). The number widened."""
    import roads
    import sidewalks

    if not targets:
        return 0
    frontage = _walls(targets)
    block = _walls(list(excluded), barriers)
    road_blocks = roads._blocker_segments([(line[:, :2], spec.width / 2) for _, spec, line, _ in carriageways])
    n = 0
    for walk in walks:
        if ("pts" not in walk or walk.get("detached") or walk.get("median") or walk.get("mapped")
                or walk.get("bay_shifted") or walk["w"] >= CITY_WALK_MAX):
            continue
        line = roads.densify(np.asarray(walk["pts"], np.float64), 1.5)
        if dnp.norm(np.diff(line[:, :2], axis=0), axis=1).sum() < 8.:
            continue
        width = walk["w"]
        curb, out = sidewalks.run_geometry(line, walk["side"], width)
        back = curb[:, :2] + out * width
        norm = out / np.maximum(1e-9, dnp.norm(out, axis=1))[:, None]
        reach = frontage.rays(back, norm, CITY_FRONT_M)
        hit = np.isfinite(reach) & (reach >= .18)
        if hit.mean() < CITY_HIT:
            continue
        extra = float(np.floor(np.percentile(reach[hit], CITY_PCT) * 4.) / 4.)
        stop = block.rays(back, norm, CITY_FRONT_M)          # (green, barriers: never past them)
        if np.isfinite(stop).any():
            extra = min(extra, float(np.nanmin(stop)) - .1)
        extra = min(extra, CITY_WALK_MAX - width)
        if extra < CITY_MIN_EXTRA_M:
            continue
        end = back + extra * out
        quads = np.stack((back[:-1], back[1:], end[1:], end[:-1]), axis=1)
        if _road_overlap(quads, road_blocks).any():
            continue
        line[:, :2] += out * (extra / 2)
        walk["pts"], walk["w"] = line, width + extra
        walk["city_widened"] = round(extra, 2)
        n += 1
    return n


def city_frontage(frame, tx, ty, md, walks, rings, items, carriageways, dense, z_at=None, redrape=False):
    """Widen a city tile's sidewalks to its buildings outside Europe's details (widen_city_walks); the number."""
    import logging
    import roads
    import sidewalks

    if not CITY_ON or not items or not walks:
        return 0
    box = roads.tile_bbox(tx, ty)
    targets = city_targets(items, np.array(box[:2]), dense)
    if not targets:
        return 0
    try:
        features = [layer for key in roads.cells_of_tile(frame, tx, ty, CITY_FRONT_M + 2.)
                    for layer in md.cell(key, layers=("land_use", "infrastructure", "water"), kind="demand").layers.values()]
    except (OSError, RuntimeError, ValueError) as exc:
        logging.getLogger("earthd.roads").debug("city frontage data unavailable: %s", exc)
        return 0
    margin = CITY_FRONT_M + 2.
    near = (box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin)
    areas = _areas(frame, features, near)
    barriers = barrier_lines(frame, features, near)
    n = widen_city_walks(walks, targets, rings, areas[1], barriers, carriageways)
    if n:
        walks[:] = sidewalks.clear_overlaps(walks)
        if redrape:
            roads.drape_walks(frame, None, walks, z_at)
    return n


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
        cls = spec.cls or ""
        if cls in HOUSE_CLASSES | UNTYPED and (spec.floors or 0) <= 3:
            homes.append(ring)
            # An unclassified footprint is common even for an ordinary house.
            # Small single-storey flat-roofed outbuildings stay out of the candidate set.
            minimum = 42. if cls in UNTYPED and spec.roof == "flat" and spec.floors == 1 else 28.
            if (country == "FR" and props is not None and not spec.shop
                    and minimum <= abs(buildings._area2(ring)) / 2 <= 410.):
                yards.append((spec, ring))
    lat, lon, _ = frame.enu_to_geodetic((x0 + x1) / 2, (y0 + y1) / 2, 0.)
    open_verge = europewalks.grassy_verges(country, float(lon), float(lat))
    areas = _areas(frame, features, near)
    barriers = barrier_lines(frame, features, near)
    bays = list(streetparking.areas(frame, box, features))
    shifted = streetparking.adjust_walks(walks, bays, list(rings) + areas[1], barriers)
    widened = widen_walks(walks, photo if not open_verge else None, rings,
                         areas[1] + [parts[0] for _, _, parts, _ in bays], barriers, carriageways)
    if shifted or widened:
        import sidewalks
        walks[:] = sidewalks.clear_overlaps(walks)
        if redrape:
            import roads
            roads.drape_walks(frame, None, walks, z_at)
    paved = chunks(frame, walks, rings, features, carriageways, drives, near, shops=shops, barriers=barriers,
                   extend=not open_verge, residential=not open_verge, areas=areas, photo=photo, grass=grass, bays=bays)
    spots, marks = streetparking.tile_layout(frame, box, features, carriageways, driveways, walks, rings, homes,
                                            barriers, areas[1], z_at, left_hand, bays)
    if yards:
        import fences
        occupied = areas[0] + [parts[0] for _, _, parts, _ in bays]
        props.extend(fences.inferred_rows(frame, yards, rings, walks, carriageways, barriers, occupied,
                                         areas[1], drives, z_at, photo, residential=areas[4],
                                         public=areas[3] + areas[5], driveways=driveways))
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
