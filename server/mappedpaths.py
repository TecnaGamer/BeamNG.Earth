"""Mapped ground paths, independent of vehicle-road grading, traffic and decoration.

The terrain carries their photo and ground material, as it does the roads. Width defaults are
estimates, not measured widths. Access tags do not imply pavement, stairs or vehicle access.
"""
import math
import threading
from collections import OrderedDict

import numpy as np

import mapgeom

STYLE = {"footway": (1.8, False), "path": (1.2, False), "cycleway": (2.5, True),
         "bridleway": (2.0, False), "pedestrian": (4.0, True)}
# Keep explicitly mapped steps out of the vehicle fallback, without inventing stair geometry.
CLASSES = tuple(STYLE) + ("steps",)

# Built paths need a bench across a hillside, not vehicle-road grading along it. One physics
# sample beyond their edge guards the triangles under the painted width; the cut/fill fades
# back to the hillside in a narrow outer band. Natural trails receive neither operation.
BENCH_EDGE_M = 1.0           # one physics cell guards the triangles beneath the actual path
BENCH_FADE_M = 0.5           # narrow outer blend, not the vehicle grader's broad shoulder
MAX_EARTHWORK_M = 2.5         # conservative inference limit, not a surveyed construction depth
MAX_APPROACH_CHANGE_M = 6.0  # only at a mapped bridge connection, with a graded approach
APPROACH_GRADE = 0.08
APPROACH_REACH_M = 128.0      # maximum nominal ramp plus the extrapolation-safe landing
MAX_CROSSFALL = 0.025         # retain gentle drainage slopes, cap DEM hillside crossfall
JOIN_REACH_M = 12.0
PROFILE_STEP_M = 2.0
PROFILE_SIGMA_M = 6.0        # remove DEM chatter on explicit built surfaces, retaining the overall climb
BENCH_CELLS = 32


def engineered(spec):
    """Explicit paving or compacted aggregate justifies a bench; loose gravel alone does not."""
    import roads
    if spec is None:
        return False
    return spec.surf == "compacted" or (
        spec.kind in roads.BUILT and spec.surf in roads.PAVED | roads.COBBLE | roads.COBBLE_ALIASES)


def node_keys(ring):
    """Shared mapped vertices, rather than nearby geometry, supply connection evidence."""
    return frozenset(map(tuple, np.round(np.asarray(ring)[:, :2], 7)))


def _vehicle_half(road):
    """The protected carriageway/sidewalk footprint, excluding a generic grading shoulder."""
    import grading
    half = road.half if road.half - road.road_half > grading.SHOULDER_M + 1e-6 else road.road_half
    return half + (float(np.max(road.widen)) if road.widen is not None else 0.0)


def _profile_parts(xy, box):
    """Clip before allocating samples, with spacing anchored to each original segment.

    Adjacent cells retain identical samples in their overlap, including curved/short segments.
    Resampling a clipped line instead resets its phase and makes seams on uneven ground.
    """
    import grading
    run = []
    for a, b in zip(xy[:-1], xy[1:]):
        delta = b - a
        length = float(np.linalg.norm(delta))
        clipped = grading.clip_polyline(np.array([a, b]), box) if length > 1e-6 else []
        if not clipped:
            if run:
                yield np.concatenate(run)
                run = []
            continue
        n = max(1, math.ceil(length / PROFILE_STEP_M))
        ends = (clipped[0] - a) @ delta / (length * length)
        lo, hi = max(0, math.floor(ends[0] * n)), min(n, math.ceil(ends[-1] * n))
        points = a + np.arange(lo, hi + 1)[:, None] / n * delta
        if run and np.linalg.norm(run[-1][-1] - points[0]) < 1e-6:
            points = points[1:]
        elif run:
            yield np.concatenate(run)
            run = []
        if len(points):
            run.append(points)
    if run:
        yield np.concatenate(run)


class Grader:
    """A regional planner's separate built-path pass; never enters vehicle plans or their cache."""

    def __init__(self, planner):
        self.planner = planner
        self.cells = OrderedDict()
        self.sets = OrderedDict()
        self.nodes = OrderedDict()
        self.approaches = OrderedDict()
        store = getattr(planner, "store", None)
        self.bridges = store.path_bridge_view(planner.frame) if store is not None else None
        self.lock = threading.RLock()

    def _cell(self, key, lift):
        import fast
        import grading
        pl = self.planner
        if key not in pl.cells or pl.path_cells.get(key) is False:
            return []
        cache_key = (key, lift)
        with self.lock:
            hit = self.cells.get(cache_key)
            if hit is not None:
                self.cells.move_to_end(cache_key)
                return hit[1]
        try:
            feats = pl.md.cell(key, layers=("roads",)).layers.get("roads")
        except OSError:
            return []
        if feats is None:              # failed source reads must be retried, not remembered empty
            return []
        with self.lock:
            # Older plan caches have no presence bit: discover it once without invalidating
            # otherwise valid vehicle plans. Empty cells then need no more map-data reads.
            pl.path_cells[key] = any(engineered(sp) for sp in specs(feats))
            if not pl.path_cells[key]:
                return []
            hit = self.cells.get(cache_key)
            if hit is not None and hit[0] is feats:
                self.cells.move_to_end(cache_key)
                return hit[1]
            west, south, east, north = key.bounds()
            la, lo = np.array([south, south, north, north]), np.array([west, east, west, east])
            e, n, _ = pl.frame.geodetic_to_enu(la, lo, 0.0)
            context = JOIN_REACH_M + grading.EXTRAP_M + PROFILE_STEP_M
            cell_box = (float(e.min()) - context, float(n.min()) - context,
                        float(e.max()) + context, float(n.max()) + context)
            pad = max(20.0 + BENCH_EDGE_M + BENCH_FADE_M + PROFILE_STEP_M
                      + 3 * PROFILE_SIGMA_M + grading.EXTRAP_M,
                      APPROACH_REACH_M if self.bridges is not None else 0)
            box = (float(e.min()) - pad, float(n.min()) - pad, float(e.max()) + pad, float(n.max()) + pad)
            decks = []
            if self.bridges is not None:
                import mapcells
                la, lo, _ = pl.frame.enu_to_geodetic(np.array([box[0], box[2]]), np.array([box[1], box[3]]), 0.)
                cells = [pl.md.cell(k, layers=("roads",)).layers.get("roads") for k in
                         mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()))]
                decks = self.bridges.parts(cells, box)
            plans = []
            complete = True
            for fid, sp, ring in parts([feats]):
                if not engineered(sp):
                    continue
                e, n, _ = pl.frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                xy = np.column_stack([e, n])
                rid = grading.road_id(fid)
                self.nodes[rid] = self.nodes.get(rid, frozenset()) | node_keys(ring)
                self.nodes.move_to_end(rid)
                while len(self.nodes) > 4096:
                    self.nodes.popitem(last=False)
                for piece in _profile_parts(xy, box):
                    if len(piece) < 2:
                        continue
                    la, lo, _ = pl.frame.enu_to_geodetic(piece[:, 0], piece[:, 1], 0.0)
                    # A built path follows the climb, not the DEM's metre-scale chatter.
                    # Use the existing endpoint-preserving filter without vehicle wear.
                    z = pl.apply(la, lo, pl.elev(la, lo), lift, roads_only=True)
                    if not np.isfinite(z).all():
                        complete = False
                        continue
                    z = grading.smooth_profile(z, PROFILE_STEP_M, PROFILE_SIGMA_M)
                    plan = grading.Plan(piece, z, sp.width / 2 + BENCH_EDGE_M, road_half=sp.width / 2)
                    plan.rid = rid
                    arc = np.r_[0., np.cumsum(np.linalg.norm(np.diff(piece, axis=0), axis=1))]
                    ramp = np.zeros(len(piece), bool)
                    landing_blend = np.zeros(len(piece))
                    for _fid, _sp, deck_xy, deck_z, _lon in decks:
                        if not np.isfinite(deck_z).all():
                            continue
                        for end in (0, -1):
                            for deck_end in (0, -1):
                                if np.linalg.norm(piece[end] - deck_xy[deck_end]) > .15:
                                    continue
                                _la, _lo, target = pl.frame.enu_to_geodetic(
                                    deck_xy[deck_end, 0], deck_xy[deck_end, 1], deck_z[deck_end])
                                delta = float(target - plan.z[end])
                                if abs(delta) > MAX_APPROACH_CHANGE_M:
                                    continue
                                distance = np.abs(arc - arc[end])
                                available = float(arc[-1])
                                for road in pl.plans(la, lo, roads_only=True):
                                    d = fast._ef.polyline_dist(np.ascontiguousarray(road.xy, np.float64),
                                                              np.ascontiguousarray(piece))
                                    core = _vehicle_half(road) + BENCH_EDGE_M
                                    far = -1 if end == 0 else 0
                                    if d[far] <= core and d[end] > core:
                                        available = min(available, float(distance[d <= core].min()))
                                if available <= PROFILE_STEP_M:
                                    continue
                                # The native grader extrapolates neighbouring segments. Hold
                                # the deck's end tangent across that reach, then return to the
                                # ground before the protected road footprint, even on short ways.
                                landing_m = min(grading.EXTRAP_M + PROFILE_STEP_M, available / 4)
                                length = min(available, landing_m + max(8.,
                                             math.pi * abs(delta) / (2 * APPROACH_GRADE)))
                                weight = .5 * (1 + np.cos(np.pi * np.clip(
                                    (distance - landing_m) / max(length - landing_m, 1e-6), 0, 1)))
                                near = 1 if deck_end == 0 else -2
                                _a, _o, neighbour_z = pl.frame.enu_to_geodetic(
                                    deck_xy[near, 0], deck_xy[near, 1], deck_z[near])
                                slope = float(target - neighbour_z) / max(
                                    float(np.linalg.norm(deck_xy[deck_end] - deck_xy[near])), 1e-6)
                                plan.z = plan.z + weight * (float(target) + slope * distance - plan.z)
                                landing_blend = np.maximum(landing_blend, weight)
                                ramp |= weight > 0
                    tangent = np.gradient(piece, axis=0)
                    normal = np.column_stack([-tangent[:, 1], tangent[:, 0]])
                    normal /= np.maximum(np.linalg.norm(normal, axis=1)[:, None], 1e-9)
                    probes = np.concatenate([piece + normal * sp.width / 2, piece - normal * sp.width / 2])
                    pa, po, _ = pl.frame.enu_to_geodetic(probes[:, 0], probes[:, 1], 0.0)
                    side_z = pl.apply(pa, po, pl.elev(pa, po), lift, roads_only=True)
                    left, right = np.split(side_z, 2)
                    # Filter the same DEM chatter across the path before limiting the bank.
                    # Clipping noisy probes first can create alternating saturated banks even
                    # when the centreline is smooth. Retain the low-frequency drainage slope.
                    bank = grading.smooth_profile((left - right) / sp.width, PROFILE_STEP_M, PROFILE_SIGMA_M)
                    plan.bank = np.clip(bank, -MAX_CROSSFALL, MAX_CROSSFALL)
                    plan.bank *= 1 - landing_blend
                    if not np.isfinite(plan.bank).all():
                        complete = False
                        continue
                    reach = plan.half + BENCH_FADE_M
                    plan.bbox = (piece[:, 0].min() - reach, piece[:, 1].min() - reach,
                                 piece[:, 0].max() + reach, piece[:, 1].max() + reach)
                    # Sampling needs the larger context above; a cell must not donate its
                    # artificial clipped ends to points outside the planner's lookup reach.
                    for bounded in grading.trim_plan(plan, cell_box):
                        plans.append(bounded)
                        if ramp.any():
                            self.approaches[(rid, *bounded.xy[0], *bounded.xy[-1])] = piece[ramp].copy()
                    while len(self.approaches) > 4096:
                        self.approaches.popitem(last=False)
            if complete:
                self.cells[cache_key] = (feats, plans)
            while len(self.cells) > BENCH_CELLS:
                self.cells.popitem(last=False)
            return plans

    def apply(self, lat, lon, base, lift):
        import fast
        import grading
        pl = self.planner
        keys = pl._keys(lat, lon)
        set_key = (tuple(sorted(keys)), lift)
        with self.lock:
            plans = self.sets.get(set_key)
        if plans is None:
            plans = self._plans(keys, lift)
            if all(k in pl.cells and (pl.path_cells.get(k) is False or (k, lift) in self.cells) for k in keys):
                with self.lock:
                    self.sets[set_key] = plans
                    while len(self.sets) > BENCH_CELLS:
                        self.sets.popitem(last=False)
        if not plans:
            return base
        e, n, _ = pl.frame.geodetic_to_enu(np.ravel(lat), np.ravel(lon), 0.0)
        original = np.asarray(base).ravel()
        out = grading.grade_points(e, n, original, plans, band=BENCH_FADE_M)
        limit = np.full(len(original), MAX_EARTHWORK_M)
        if self.approaches:
            points = np.ascontiguousarray(np.column_stack([e, n]))
            for p in plans:
                span = self.approaches.get((p.rid, *p.xy[0], *p.xy[-1]))
                if span is not None and len(span) > 1:
                    distance = fast._ef.polyline_dist(np.ascontiguousarray(span), points)
                    limit[distance <= p.half + BENCH_FADE_M] = MAX_APPROACH_CHANGE_M
        out = original + np.clip(out - original, -limit, limit)
        changed = np.flatnonzero(out != original)
        if len(changed):
            # Vehicle carriageways, sidewalks and their junctions have priority. Fade the path
            # correction outside their cores rather than making a hard height step at an edge.
            pts = np.ascontiguousarray(np.column_stack([e[changed], n[changed]]))
            weight = np.ones(len(changed))
            for road in pl.plans(lat, lon, roads_only=True):
                d = fast._ef.polyline_dist(np.ascontiguousarray(road.xy, np.float64), pts)
                # The generic one-metre shoulder is not a carriageway or sidewalk. Protecting
                # its whole grading core tilted a closely parallel cycleway back into the bank.
                half = _vehicle_half(road)
                w = np.clip((d - half) / BENCH_EDGE_M, 0.0, 1.0)
                weight = np.minimum(weight, w * w * (3 - 2 * w))
            # Raceway surfaces are kept/smoothed separately and have no vehicle grading Plan.
            kept = getattr(pl, "_on_kept", None)
            if kept is not None:
                mask = kept(e[changed], n[changed])
                if mask is not None:
                    weight[mask] = 0.0
            out[changed] = original[changed] + weight * (out[changed] - original[changed])
        return out.reshape(np.shape(base))

    def _plans(self, keys, lift):
        import grading
        plans = {}
        for key in keys:
            for p in self._cell(key, lift):
                plans.setdefault((p.rid, *p.xy[0], *p.xy[-1]), p)
        joined = []
        # A side path that meets a wider paved corridor shares its platform there. Geometry
        # stays separate; distant parallel paths keep their own profiles. This also avoids
        # blending two hillside-derived heights into a tilted strip at a path junction.
        for key in sorted(plans, key=lambda k: (-plans[k].road_half, k)):
            p = grading.mapped_plan(plans[key], lambda xy: xy)
            parents = []
            for other in joined:
                shared = self.nodes.get(p.rid, frozenset()) & self.nodes.get(other.rid, frozenset())
                if (shared and other.rid != p.rid and other.bbox[2] >= p.bbox[0] and other.bbox[0] <= p.bbox[2]
                        and other.bbox[3] >= p.bbox[1] and other.bbox[1] <= p.bbox[3]):
                    parent = grading.Plan(other.xy, other.z, other.road_half + p.road_half + BENCH_EDGE_M)
                    parent.rid = other.rid
                    parent.bank = other.bank
                    parents.append(parent)
            if parents:
                target = grading.grade_points(p.xy[:, 0], p.xy[:, 1], p.z, parents, band=BENCH_FADE_M)
                # A shared node is a local join, not evidence that two entire parallel ways
                # share an elevation. Large disagreements remain unresolved instead of filled.
                shared = set().union(*(self.nodes.get(p.rid, frozenset()) & self.nodes.get(q.rid, frozenset())
                                       for q in parents))
                coords = np.array(sorted(shared))
                ex, ny, _ = self.planner.frame.geodetic_to_enu(coords[:, 1], coords[:, 0], 0.0)
                arc = np.r_[0., np.cumsum(np.linalg.norm(np.diff(p.xy, axis=0), axis=1))]
                distance = np.full(len(p.xy), np.inf)
                for point in np.column_stack([ex, ny]):
                    nearest = int(np.argmin(np.linalg.norm(p.xy - point, axis=1)))
                    if np.linalg.norm(p.xy[nearest] - point) <= .15:
                        distance = np.minimum(distance, np.abs(arc - arc[nearest]))
                weight = 0.5 * (1 + np.cos(np.pi * np.clip(distance / JOIN_REACH_M, 0, 1)))
                delta = target - p.z
                p.z = p.z + np.where(np.abs(delta) <= MAX_EARTHWORK_M, weight * delta, 0.0)
            joined.append(p)
        return joined


def _with_paths(feats_list):
    return [f for f in feats_list if f is not None and any(sp is not None for sp in specs(f))]


def specs(feats, bridge=False):
    """Existing roads.Spec objects for ground paths or path decks; no vehicle width inference."""
    import roads
    if not any(str(c) in STYLE for c in feats.attrs.get("class", ())):
        return [None] * len(feats.kind)

    def make():
        out = []
        for i, cls in enumerate(feats.attrs.get("class", [""] * len(feats))):
            cls = str(cls)
            if cls not in STYLE or feats.kind[i] != mapgeom.LINE or roads.unbuilt(feats, i):
                out.append(None)
                continue
            if str(feats.attrs.get("subtype", ["road"] * len(feats))[i]) != "road":
                out.append(None)
                continue
            # OSM flags can be newer than Overture's; never paint a deck/tunnel as a ground path.
            flags = [str(feats.attrs.get(k, [""] * len(feats))[i]).lower()
                     for k in ("osm_bridge", "osm_tunnel")]
            lv = feats.attrs.get("osm_layer")
            elevated = flags[0] not in ("", "no", "false", "0")
            tunnel = flags[1] not in ("", "no", "false", "0")
            level = float(lv[i]) if lv is not None and np.isfinite(lv[i]) else 0.0
            ranges = roads.raw_level_ranges(feats, i)
            if (bridge and (tunnel or level < 0 or not (elevated or any(k == "bridge" for _, _, k in ranges)))) or (
                    not bridge and (elevated or tunnel or level != 0)):
                out.append(None)
                continue
            default, paved = STYLE[cls]
            width = roads._osm(feats, "width", i, np.nextafter(0.0, 1.0), 40.0)
            if width is None:
                v = roads._rule_value(feats.rules.get("width"), i)
                width = float(v) if v is not None and math.isfinite(float(v)) and 0 < float(v) <= 40 else default
            surf = roads._surface_value(feats, i)
            # Boardwalks need their own wooden/metal surface, not the roads' asphalt fallback.
            if roads.surface_token(surf) in ("wood", "metal"):
                out.append(None)
                continue
            # A footway label alone does not establish pavement (notably rural UK footways).
            # A mapped bridge still needs its structural deck fallback; explicit surfaces always win.
            kind = roads.surface_kind("residential" if paved or bridge else "track", surf)
            if bridge and kind not in roads.BUILT:
                out.append(None)
                continue
            out.append(roads.Spec(width, kind, False, cls, walk=0.0, parking=False,
                                  surf=roads.surface_token(surf)))
        return out

    return roads._cell_memo(feats, "_mapped_path_bridge_specs" if bridge else "_mapped_path_specs", make)


def bridge_parts(feats, i):
    """Explicit whole OSM bridge ways, otherwise Overture's scoped bridge pieces."""
    import roads
    value = str(feats.attrs.get("osm_bridge", [""] * len(feats))[i]).lower()
    if value not in ("", "no", "false", "0"):
        return [ring for part in feats.parts(i) for ring in part if len(ring) >= 2]
    return roads.bridge_parts(feats, i)


def parts(feats_list, bounds=None):
    """(id, spec, lon/lat line) of ground paths, source-id deduplicated and spatially clipped.

    bounds is lon0, lat0, lon1, lat1. Clip before any consumer allocates elevation/profile work.
    Ground splitting remains the roads' existing bridge/tunnel/level handling.
    """
    import roads
    seen = set()
    for feats in feats_list:
        if feats is None or not len(feats.kind):
            continue
        for i, sp in enumerate(specs(feats)):
            fid = str(feats.ids[i])
            if sp is None or fid in seen:
                continue
            seen.add(fid)
            for ring in roads.ground_parts(feats, i):
                if len(ring) < 2:
                    continue
                if bounds is None:
                    yield fid, sp, ring
                    continue
                dx = sp.width / (2 * 111320 * max(0.1, math.cos(math.radians((bounds[1] + bounds[3]) / 2))))
                dy = sp.width / (2 * 110540)
                box = (bounds[0] - dx, bounds[1] - dy, bounds[2] + dx, bounds[3] + dy)
                if (ring[:, 0].max() < box[0] or ring[:, 0].min() > box[2]
                        or ring[:, 1].max() < box[1] or ring[:, 1].min() > box[3]):
                    continue
                for piece in roads.clip_polyline(ring, *box):
                    yield fid, sp, piece


def paint_materials(idx, lat, lon, square, feats_list):
    """Paint exact path widths into the existing terrain layers; vehicle roads paint over them."""
    import fast
    import roads
    import surface
    feats_list = _with_paths(feats_list)
    if not feats_list:
        return idx.copy()
    grid = surface.grid_affine(lat, lon)
    mid_lat, mid_lon = float(np.mean(lat)), float(np.mean(lon))
    c0, r0 = grid(mid_lon, mid_lat)
    c1, r1 = grid(mid_lon + square / (2 * 111320 * max(0.1, math.cos(math.radians(mid_lat)))),
                  mid_lat + square / (2 * 111320))
    # Root clearance shares the roads' narrow cell-edge guard. Grass billboards extend past their roots;
    # keep that guard bare without widening the path's painted surface or removing distant vegetation.
    dy = surface.ROAD_EDGE_M / 110540
    dx = surface.ROAD_EDGE_M / (111320 * max(.1, math.cos(math.radians(mid_lat))))
    bounds = (float(np.min(lon)) - dx, float(np.min(lat)) - dy,
              float(np.max(lon)) + dx, float(np.max(lat)) + dy)
    groups, edges, edge_widths = {}, [], []
    for _, sp, ring in parts(feats_list, bounds):
        c, r = grid(ring[:, 0], ring[:, 1])
        # GroundCover selects the nearest terrain sample; its footprint is centred on
        # that vertex, unlike the physical material of the square north-east of it.
        roots = np.column_stack([c[:-1], r[:-1], c[1:], r[1:]])
        c, r = c - (c1 - c0), r - (r1 - r0)
        seg = np.column_stack([c[:-1], r[:-1], c[1:], r[1:]])
        dst = groups.setdefault(roads.surface_material_kind(sp), ([], []))
        dst[0].append(seg)
        dst[1].append(np.full(len(seg), sp.width / (2 * square)))
        if roads.surface_material_kind(sp) != "grass":
            edges.append(roots)
            edge_widths.append(np.full(len(seg), (sp.width / 2 + surface.ROAD_EDGE_M) / square))
    if not groups:
        return idx.copy()
    out = idx.copy()
    if edges:
        bare = np.zeros(idx.shape, bool)
        fast._ef.mask_segments(bare, np.ascontiguousarray(np.concatenate(edges)), np.concatenate(edge_widths))
        out[bare & np.isin(out, (surface.GRASS, surface.DUSTY, surface.LAWN))] = surface.DIRT
    for kind in surface.ROAD_ORDER:
        if kind not in groups:
            continue
        segs, hw = groups[kind]
        mask = np.zeros(idx.shape, bool)
        fast._ef.mask_segments(mask, np.ascontiguousarray(np.concatenate(segs)), np.concatenate(hw))
        out[mask] = surface.ROAD_MATERIAL[kind]
    return out


def paint_photo(rgb, frame, x0, y0, size, feats_list):
    """Reuse the ground-photo tints, without car ruts or a smooth ribbon over rock/grass."""
    import fast
    import roads
    import surface
    feats_list = _with_paths(feats_list)
    if not feats_list:
        return rgb
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0 + size, x0, x0 + size]),
                                    np.array([y0, y0, y0 + size, y0 + size]), 0.0)
    bounds = (float(lo.min()), float(la.min()), float(lo.max()), float(la.max()))
    segs, widths, kinds = [], [], []
    palette = list(surface.PHOTO_ROAD_RGBA)
    h, w = rgb.shape[:2]
    for _, sp, ring in parts(feats_list, bounds):
        if roads.terrain_surface(sp) is not None:
            continue
        e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
        c, r = (e - x0) * w / size, (y0 + size - n) * h / size
        seg = np.column_stack([c[:-1], r[:-1], c[1:], r[1:]])
        segs.append(seg)
        widths.append(np.full(len(seg), sp.width * w / (2 * size)))
        kinds.append(np.full(len(seg), palette.index(sp.kind), np.uint8))
    if not segs:
        return rgb
    return fast._ef.paint_lines(np.ascontiguousarray(rgb, np.uint8), np.concatenate(segs),
                               np.concatenate(widths), np.concatenate(kinds),
                               np.array(list(surface.PHOTO_ROAD_RGBA.values()), np.float32))


def clear_obstructions(items, feats_list):
    """Remove bushes and tree origins on a path, not crowns overhanging it or stones on a rocky trail.

    Separately mapped sidewalks keep their intentional street trees. No broad vehicle-road clearance.
    """
    import fast
    import vegetation
    feats_list = _with_paths(feats_list)
    if not len(items.lon) or not feats_list:
        return items
    lat0, lon0 = float(np.mean(items.lat)), float(np.mean(items.lon))
    scale = np.array([111320 * math.cos(math.radians(lat0)), 110540])
    origin = np.array([lon0, lat0])
    pts = np.column_stack([items.lon - lon0, items.lat - lat0]) * scale
    bounds = (float(items.lon.min()), float(items.lat.min()), float(items.lon.max()), float(items.lat.max()))
    keep = np.ones(len(items.lon), bool)
    for _, sp, ring in parts(feats_list, bounds):
        xy = (ring - origin) * scale
        segs = np.ascontiguousarray(np.column_stack([xy[:-1, 0], xy[:-1, 1], xy[1:, 0], xy[1:, 1]]))
        inside = fast._ef.inside_blockers(np.ascontiguousarray(pts), segs,
                                          np.array([0, len(segs)], np.int64), np.array([sp.width / 2]))
        removable = items.kind == vegetation.KIND_BUSH
        if sp.cls not in ("footway", "pedestrian"):
            removable |= items.kind == vegetation.KIND_TREE
        import roads
        if roads.terrain_surface(sp) not in ("rock", "grass"):
            removable |= items.kind == vegetation.KIND_ROCK
        keep &= ~(inside & removable)
    return items.take(keep)
