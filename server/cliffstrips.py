"""Bounded server generation of projected cliff-contact strips.

UV0 is a fixed planar projection. UV1 carries height-erode coverage, including
both end caps. This module has no engine calls or per-frame generation work.
"""
from dataclasses import dataclass
import numpy as np

HALF_WIDTH_M = 2.0
END_FADE_M = 2.0
SAMPLE_M = 0.80
SURFACE_OFFSET_M = 0.08
GROUND_CLEARANCE_M = 0.035
MAX_BRIDGE_M = 1.5
TEXTURE_REPEAT_M = 4.0
LOOKUP_WIDTH = 1024
MAX_TRIANGLES = 12000
DEPTH_LIMIT_M = 12.0
CLIP_EPS = 1e-8


def unit(v):
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-12)


def threshold(across, along, length, half_width=HALF_WIDTH_M, end_fade=END_FADE_M):
    """Union of side/end erosion: zero at the interior, one on every edge."""
    side = np.clip(np.abs(across)/half_width, 0, 1)**1.35
    distance = np.minimum(along, length-along)
    cap = np.clip(distance/min(end_fade, length/2), 0, 1)
    cap = cap*cap*(3-2*cap)
    return 1-(1-side)*cap


def basis(normal):
    normal = unit(np.asarray(normal, float))
    reference = np.array([0., 0., 1.]) if abs(normal[2]) < .95 else np.array([0., 1., 0.])
    right = unit(np.cross(reference, normal))
    return np.stack((right, np.cross(normal, right), normal))


class Projector:
    """Orthographic ray projection onto the frontmost receiving triangle.

    Spatial bins keep each sample local. Reusing one projector per strip also
    prevents changing UV axes as the centerline curves. Triangle IDs allow the
    caller to inspect which receiver was hit (terrain versus rock).
    """
    def __init__(self, triangles, normal, origin, bin_m=2.0, vertex_normals=None, index=True):
        self.axes = basis(normal)
        self.origin = np.asarray(origin, float)
        self.triangles = np.asarray(triangles, float)
        self.local = (self.triangles-self.origin) @ self.axes.T
        self.vertex_normals = vertex_normals
        self.xy_min = self.local[:, :, :2].min(1)
        self.xy_max = self.local[:, :, :2].max(1)
        self.bin_m = bin_m
        self.bins = {}
        a = self.local[:, 1, :2]-self.local[:, 0, :2]
        b = self.local[:, 2, :2]-self.local[:, 0, :2]
        det = a[:, 0]*b[:, 1]-a[:, 1]*b[:, 0]
        self.valid = np.abs(det)>1e-7
        self.planes = np.zeros((len(self.local), 3))
        # Depth is affine in projected XY for every non-grazing receiver.
        m = np.concatenate((self.local[self.valid, :, :2],
                            np.ones((self.valid.sum(), 3, 1))), axis=2)
        self.planes[self.valid] = np.linalg.solve(m, self.local[self.valid, :, 2, None])[..., 0]
        for i in np.flatnonzero(self.valid) if index else ():
            lo = np.floor(self.local[i, :, :2].min(0)/bin_m).astype(int)
            hi = np.floor(self.local[i, :, :2].max(0)/bin_m).astype(int)
            for x in range(lo[0], hi[0]+1):
                for y in range(lo[1], hi[1]+1):
                    self.bins.setdefault((x,y), []).append(i)

    def sample(self, xy, near_depth=None, depth_limit=12.0):
        xy = np.asarray(xy, float)
        output = np.full((len(xy),3), np.nan)
        normals = np.zeros_like(output)
        owner = np.full(len(xy), -1, dtype=int)
        keys = np.floor(xy/self.bin_m).astype(int)
        for key in set(map(tuple, keys)):
            points = np.flatnonzero(np.all(keys==key, axis=1))
            ids = np.asarray(self.bins.get(key, []), int)
            if not len(ids): continue
            tri = self.local[ids]
            a, b = tri[:,1]-tri[:,0], tri[:,2]-tri[:,0]
            det = a[:,0]*b[:,1]-a[:,1]*b[:,0]
            d = xy[points,None,:]-tri[None,:,0,:2]
            u = (d[:,:,0]*b[:,1]-d[:,:,1]*b[:,0])/det
            v = (a[:,0]*d[:,:,1]-a[:,1]*d[:,:,0])/det
            z = tri[:,0,2]+u*a[:,2]+v*b[:,2]
            valid = (u>=-1e-6)&(v>=-1e-6)&(u+v<=1.000001)
            if near_depth is not None:
                valid &= np.abs(z-np.asarray(near_depth)[points,None]) < depth_limit
            z = np.where(valid,z,-np.inf)
            best = z.argmax(1); good = np.isfinite(z[np.arange(len(points)),best])
            take = points[good]; chosen = ids[best[good]]
            local = np.column_stack((xy[take],z[good,best[good]]))
            output[take] = local @ self.axes+self.origin
            n = unit(np.cross(self.triangles[chosen,1]-self.triangles[chosen,0],self.triangles[chosen,2]-self.triangles[chosen,0]))
            n *= np.where((n@self.axes[2])[:,None]<0,-1,1)
            normals[take] = n; owner[take] = chosen
        return output,normals,owner


@dataclass
class Strip:
    positions: np.ndarray
    tris: np.ndarray
    uvs: np.ndarray
    uvs2: np.ndarray
    normals: np.ndarray
    receiver: np.ndarray
    threshold: np.ndarray


def _area(poly):
    if len(poly) < 3:
        return 0.0
    return .5 * abs(np.sum(poly[:, 0] * np.roll(poly[:, 1], -1)
                         - poly[:, 1] * np.roll(poly[:, 0], -1)))


def _clip(poly, plane):
    """Clip a convex polygon against a half-space, retaining all attributes."""
    if not len(poly):
        return poly
    distance = poly @ plane[:-1] + plane[-1]
    inside = distance >= -CLIP_EPS
    if inside.all():
        return poly
    if not inside.any():
        return np.empty((0, poly.shape[1]))
    result = []
    for i in range(len(poly)):
        j = (i - 1) % len(poly)
        if inside[i] != inside[j]:
            t = distance[j] / (distance[j] - distance[i])
            result.append(poly[j] + t * (poly[i] - poly[j]))
        if inside[i]:
            result.append(poly[i])
    return np.array(result)


def _edge_planes(boundary, columns):
    """Inward half-spaces for either winding of a convex XY boundary."""
    signed = np.sum(boundary[:, 0] * np.roll(boundary[:, 1], -1)
                    - boundary[:, 1] * np.roll(boundary[:, 0], -1))
    sign = 1 if signed >= 0 else -1
    for a, b in zip(boundary, np.roll(boundary, -1, axis=0)):
        dx, dy = b[:2] - a[:2]
        if abs(dx)+abs(dy) <= CLIP_EPS:
            continue  # A repeated clipping vertex is not a polygon boundary.
        plane = np.zeros(columns + 1)
        plane[0], plane[1] = -dy * sign, dx * sign
        plane[-1] = (dy * a[0] - dx * a[1]) * sign
        yield plane


def _intersect(poly, boundary):
    for plane in _edge_planes(boundary, poly.shape[1]):
        poly = _clip(poly, plane)
        if len(poly) < 3:
            break
    return poly


def _subtract(poly, boundary):
    """Subtract a convex occluder, returning disjoint convex fragments."""
    remainder = poly
    fragments = []
    for plane in _edge_planes(boundary, poly.shape[1]):
        outside = _clip(remainder, -plane)
        if _area(outside) > CLIP_EPS:
            fragments.append(outside)
        remainder = _clip(remainder, plane)
        if _area(remainder) <= CLIP_EPS:
            break
    return fragments


def _candidates(projector, poly):
    lo = np.floor(poly[:, :2].min(0) / projector.bin_m).astype(int)
    hi = np.floor(poly[:, :2].max(0) / projector.bin_m).astype(int)
    ids = set()
    for x in range(lo[0], hi[0] + 1):
        for y in range(lo[1], hi[1] + 1):
            ids.update(projector.bins.get((x, y), ()))
    ids = np.array(sorted(ids), dtype=int)
    keep = np.all(projector.xy_max[ids] >= poly[:, :2].min(0), axis=1) & np.all(projector.xy_min[ids] <= poly[:, :2].max(0), axis=1)
    return ids[keep]


def _visible_fragments(domain, projector):
    """Intersect a mask cell with receivers and subtract foreground occluders.

    Every returned polygon lies on exactly one receiving triangle. Depth-plane
    clipping separates crossing terrain/rock triangles at their intersection,
    instead of stretching one strip triangle across that crease.
    """
    ids = _candidates(projector, domain)
    regions = {}
    for owner in ids:
        polygon = _intersect(domain, projector.local[owner, :, :2])
        if _area(polygon) <= CLIP_EPS:
            continue
        plane = projector.planes[owner]
        # Domain columns are XY, threshold and expected centerline depth.
        delta = np.array([plane[0], plane[1], 0, -1, plane[2]])
        polygon = _clip(polygon, delta + [0, 0, 0, 0, DEPTH_LIMIT_M])
        polygon = _clip(polygon, -delta + [0, 0, 0, 0, DEPTH_LIMIT_M])
        if _area(polygon) > CLIP_EPS:
            regions[owner] = polygon
    bounds = {i: (p[:, :2].min(0), p[:, :2].max(0)) for i, p in regions.items()}
    for owner, polygon in regions.items():
        fragments = [polygon]
        for other, boundary in regions.items():
            if other == owner:
                continue
            if np.any(bounds[other][1] <= bounds[owner][0] + CLIP_EPS) or np.any(bounds[owner][1] <= bounds[other][0] + CLIP_EPS):
                continue
            delta = projector.planes[other] - projector.planes[owner]
            if np.max(np.abs(delta)) < CLIP_EPS:
                if other > owner:
                    continue  # One deterministic owner for coplanar surfaces.
                occluder = boundary
            else:
                occluder = _clip(boundary, np.array([delta[0], delta[1], 0, 0, delta[2]]))
            if _area(occluder) <= CLIP_EPS:
                continue
            visible = []
            for fragment in fragments:
                overlap = _intersect(fragment, occluder)
                if _area(overlap) <= CLIP_EPS:
                    visible.append(fragment)
                else:
                    visible.extend(_subtract(fragment, occluder))
            fragments = visible
            if not fragments:
                break
        for fragment in fragments:
            yield owner, fragment


def _mask_cells(centerline, projector, half_width, spacing, end_fade):
    local = (np.asarray(centerline, float) - projector.origin) @ projector.axes.T
    segment = np.linalg.norm(np.diff(local[:, :2], axis=0), axis=1)
    local = local[np.r_[True, segment > 1e-5]]
    if len(local) < 2:
        return
    arc = np.r_[0, np.cumsum(np.linalg.norm(np.diff(local[:, :2], axis=0), axis=1))]
    length = arc[-1]
    if length < 1:
        return
    along = np.linspace(0, length, max(2, int(np.ceil(length / spacing))) + 1)
    center = np.column_stack([np.interp(along, arc, local[:, k]) for k in range(3)])
    tangent = unit(np.gradient(center[:, :2], axis=0))
    perpendicular = np.column_stack((-tangent[:, 1], tangent[:, 0]))
    # Include the centerline even with a coarse mask grid: otherwise short
    # runs or an odd cross-section can contain only eroded vertices.
    across_steps = max(2, 2 * int(np.ceil(half_width / spacing)))
    cross = np.linspace(-half_width, half_width, across_steps + 1)
    xy = center[:, None, :2] + perpendicular[:, None, :] * cross[None, :, None]
    t = threshold(cross[None, :], along[:, None], length, half_width, end_fade)
    points = np.column_stack((xy.reshape(-1, 2), t.ravel(), np.repeat(center[:, 2], len(cross))))
    for j in range(len(along) - 1):
        for i in range(len(cross) - 1):
            a = j * len(cross) + i
            for ids in ((a, a+1, a+len(cross)), (a+1, a+len(cross)+1, a+len(cross))):
                domain = points[list(ids)]
                if _area(domain) > CLIP_EPS:
                    yield domain


def build_reference(centerline, projector, half_width=HALF_WIDTH_M, spacing=SAMPLE_M,
          end_fade=END_FADE_M, repeat=TEXTURE_REPEAT_M):
    """Conform exactly to receiver triangles with straight UVs and four faded edges.

    The mask grid only approximates the curved footprint and coverage gradient.
    Receiver clipping supplies the surface tessellation, including sharp folds.
    A single projection-axis offset avoids cracks caused by independently moving
    adjoining faces along different normals. It leaves projected UVs unchanged.
    """
    if min(half_width, spacing, end_fade, repeat) <= 0:
        raise ValueError('Strip dimensions and texture repeat must be positive')
    positions, normals, thresholds, owners, triangles = [], [], [], [], []
    for domain in _mask_cells(centerline, projector, half_width, spacing, end_fade):
        for owner, polygon in _visible_fragments(domain, projector):
            if _area(polygon) <= CLIP_EPS:
                continue
            depth = polygon[:, :2] @ projector.planes[owner, :2] + projector.planes[owner, 2]
            world = np.column_stack((polygon[:, :2], depth)) @ projector.axes + projector.origin
            receiver = projector.triangles[owner]
            normal = unit(np.cross(receiver[1] - receiver[0], receiver[2] - receiver[0]))
            if normal @ projector.axes[2] < 0:
                normal = -normal
            if projector.vertex_normals is None:
                shading = np.tile(normal, (len(world), 1))
            else:
                local = projector.local[owner, :, :2]
                m = np.column_stack((local[1]-local[0], local[2]-local[0]))
                weights = np.linalg.solve(m, (polygon[:, :2]-local[0]).T).T
                bary = np.column_stack((1-weights.sum(1), weights))
                shading = unit(bary @ projector.vertex_normals[owner])
            offset = len(positions)
            positions.extend(world + projector.axes[2] * SURFACE_OFFSET_M)
            normals.extend(shading)
            thresholds.extend(np.clip(polygon[:, 2], 0, 1))
            owners.extend([owner] * len(world))
            for k in range(1, len(world)-1):
                face = [offset, offset+k, offset+k+1]
                if np.cross(world[k]-world[0], world[k+1]-world[0]) @ projector.axes[2] < 0:
                    face[1], face[2] = face[2], face[1]
                if np.linalg.norm(np.cross(world[k]-world[0], world[k+1]-world[0])) > CLIP_EPS:
                    triangles.append(face)
            if len(triangles) > MAX_TRIANGLES:
                raise ValueError('Projected strip exceeds the bounded triangle budget')
    if not triangles:
        return None
    return _finish_strip(projector, positions, normals, thresholds, owners, triangles, repeat)



def _barycentric(faces, points):
    """Weights in projected receiver XY; projection-axis lift leaves these unchanged."""
    matrix = np.stack((faces[:, 1]-faces[:, 0], faces[:, 2]-faces[:, 0]), axis=2)
    weights = np.linalg.solve(matrix, (points-faces[:, 0])[..., None])[..., 0]
    return np.column_stack((1-weights.sum(1), weights))


def build(centerline, projector, half_width=HALF_WIDTH_M, spacing=SAMPLE_M,
          end_fade=END_FADE_M, repeat=TEXTURE_REPEAT_M):
    """Native receiver clipping, followed by vectorized shading/UV interpolation."""
    import fast
    fast.require(150, "cliff_strip_project")
    if min(half_width, spacing, end_fade, repeat) <= 0:
        raise ValueError('Strip dimensions and texture repeat must be positive')
    domains = list(_mask_cells(centerline, projector, half_width, spacing, end_fade))
    if not domains:
        return None
    rows = np.asarray(fast._ef.cliff_strip_project(
        np.ascontiguousarray(projector.local.reshape(-1, 9)),
        np.ascontiguousarray(np.asarray(domains).reshape(-1, 12)), MAX_TRIANGLES))
    if not len(rows):
        return None
    owners = rows[:, 4].astype(np.int64)
    world = rows[:, :3] @ projector.axes + projector.origin
    if projector.vertex_normals is None:
        face = projector.triangles[owners]
        normals = unit(np.cross(face[:, 1]-face[:, 0], face[:, 2]-face[:, 0]))
        normals *= np.where((normals @ projector.axes[2])[:, None] < 0, -1, 1)
    else:
        face = projector.local[owners, :, :2]
        bary = _barycentric(face, rows[:, :2])
        normals = unit(np.einsum('ij,ijk->ik', bary, projector.vertex_normals[owners]))
    world += projector.axes[2]*SURFACE_OFFSET_M
    return _finish_strip(projector, world, normals, rows[:, 3], owners,
                         np.arange(len(rows)).reshape(-1, 3), repeat)


def _finish_strip(projector, positions, normals, thresholds, owners, triangles, repeat):
    """Weld shared fragments without merging receiver or shading discontinuities."""
    p, normals, t, owners = map(np.asarray, (positions, normals, thresholds, owners))
    key = np.column_stack((np.round(p, 7), np.round(normals, 6), np.round(t, 7), owners))
    _, first, inverse = np.unique(key, axis=0, return_index=True, return_inverse=True)
    p, t, normals, owners = p[first], t[first], normals[first], owners[first]
    uv = p @ projector.axes[:2].T / repeat
    uv[:, 1] *= -1  # CDAE expects unflipped V.
    uv2 = np.column_stack(((.5+t*(LOOKUP_WIDTH-1))/LOOKUP_WIDTH, np.full(len(t), .5)))
    return Strip(p, inverse[np.asarray(triangles)], uv, uv2, normals, owners, t)


# Limits apply to every cliff family, including road cuts. Sparse transition
# coverage is preferable to an unbounded geometry/import spike on a complex tile.
PARTS_MAX = 24
ATTEMPTS_MAX = 128
# More bounded batches cover complex contact networks without making any
# individual synchronous game import larger than the previous 8k triangles.
PART_TRIANGLES = 8000
TILE_TRIANGLES = PARTS_MAX * PART_TRIANGLES
RUN_LENGTH_M = 48.0
CONTACT_HALF_WIDTH_M = 4.0
CONTACT_SAMPLE_M = 4.0
RECEIVER_MARGIN_M = CONTACT_HALF_WIDTH_M + 4.0
MIN_RUN_M = 4.0
GROUND_GRID_M = 1.0
PATCH_FACES_MAX = 128000
MATERIAL = 'bne_cliff_contact_strip'


def contact_runs(positions, triangles, clearance):
    """Trace the zero-clearance contour through the actual displaced rock mesh."""
    signed = clearance[triangles]
    crossing = np.isfinite(signed).all(1) & (signed.min(1) < 0) & (signed.max(1) >= 0)
    face = positions[triangles[crossing]]
    signed = signed[crossing]
    points = np.empty((len(face), 2, 3)); counts = np.zeros(len(face), int)
    for i, j in ((0, 1), (1, 2), (2, 0)):
        ids = np.flatnonzero((signed[:, i] < 0) != (signed[:, j] < 0))
        t = signed[ids, i]/(signed[ids, i]-signed[ids, j])
        points[ids, counts[ids]] = face[ids, i]+t[:, None]*(face[ids, j]-face[ids, i])
        counts[ids] += 1
    points = points[counts == 2]
    if not len(points):
        return []
    _, first, inverse = np.unique(np.rint(points.reshape(-1, 3)/.03).astype(np.int64),
                                  axis=0, return_index=True, return_inverse=True)
    nodes = points.reshape(-1, 3)[first]; edges = inverse.reshape(-1, 2)
    adjacency = {}
    for i, (a, b) in enumerate(edges):
        if a == b:
            continue
        adjacency.setdefault(a, []).append(i); adjacency.setdefault(b, []).append(i)
    used = set(); runs = []
    for start in sorted(adjacency, key=lambda v: len(adjacency[v]) == 2):
        for edge in adjacency[start]:
            if edge in used:
                continue
            v = start; run = [nodes[v]]
            while edge not in used:
                used.add(edge); a, b = edges[edge]; v = b if a == v else a
                run.append(nodes[v])
                choices = [e for e in adjacency[v] if e not in used]
                if v == start or len(adjacency[v]) != 2 or not choices:
                    break
                edge = choices[0]
            run = np.asarray(run)
            arc = np.r_[0, np.cumsum(np.linalg.norm(np.diff(run, axis=0), axis=1))]
            if arc[-1] < MIN_RUN_M:
                continue
            # Equal physical lengths, not vertex counts (tall cliffs are anisotropic).
            for lo in np.arange(0, arc[-1], RUN_LENGTH_M):
                hi = min(lo+RUN_LENGTH_M, arc[-1])
                if hi-lo < MIN_RUN_M:
                    continue
                at = np.r_[lo, arc[(arc > lo) & (arc < hi)], hi]
                runs.append(np.column_stack([np.interp(at, arc, run[:, k]) for k in range(3)]))
    return runs


def spread_runs(runs):
    """Deterministic farthest-first contacts prevent one contour starving others."""
    if not runs:
        return []
    centers = np.array([r.mean(0) for r in runs])
    lengths = np.array([np.linalg.norm(np.diff(r, axis=0), axis=1).sum() for r in runs])
    distance = np.full(len(runs), np.inf)
    remaining = np.ones(len(runs), bool)
    selected = int(lengths.argmax()); ordered = []
    for _ in range(min(len(runs), ATTEMPTS_MAX)):
        ordered.append(runs[selected]); remaining[selected] = False
        distance = np.minimum(distance, np.sum((centers-centers[selected])**2, axis=1))
        if not remaining.any():
            break
        selected = int(np.where(remaining, distance, -1).argmax())
    return ordered


def pack_parts(parts):
    """Batch contact runs into bounded imports, without dropping contours.

    Split only on triangle boundaries. UVs, erosion and shading were already
    computed per run, so batching does not stretch textures or join gaps.
    """
    output = []; batch = []; count = 0
    def flush():
        nonlocal batch, count
        if not batch:
            return
        offsets = np.cumsum([0]+[len(s.positions) for s,c in batch[:-1]])
        arrays = lambda name: np.concatenate([getattr(s,name) for s,c in batch])
        strip = Strip(arrays('positions'), np.concatenate([s.tris+o for (s,c),o in zip(batch,offsets)]),
                      arrays('uvs'), arrays('uvs2'), arrays('normals'), arrays('receiver'), arrays('threshold'))
        output.append((strip,np.concatenate([c for s,c in batch])))
        batch = []; count = 0
    for strip, colors in parts:
        start = 0
        while start < len(strip.tris):
            take = min(PART_TRIANGLES-count, len(strip.tris)-start)
            tri = strip.tris[start:start+take]
            used, remap = np.unique(tri, return_inverse=True)
            cut = Strip(strip.positions[used],remap.reshape(-1,3),strip.uvs[used],strip.uvs2[used],
                        strip.normals[used],strip.receiver[used],strip.threshold[used])
            batch.append((cut,colors[used])); count += take; start += take
            if count == PART_TRIANGLES:
                flush()
    flush()
    return output


def prepare(rock, ground_at):
    """Build bounded, server-side contact strips; no game-thread raycasts or decals.

    Retain only bounded rock receiver patches and contact centerlines. Geotiles
    cache these inputs, then project against each anchor's rendered terrain.
    """
    from clifftransition import MEANS
    p = np.asarray(rock.positions, float); tri = np.asarray(rock.tris, np.int64)
    if not len(tri):
        return []
    ground = ground_at(p[:, :2])
    runs = contact_runs(p, tri, p[:, 2]-ground)
    if not runs:
        return []
    faces = p[tri]; low, high = faces.min(1), faces.max(1)
    face_normals = unit(np.cross(faces[:, 1]-faces[:, 0], faces[:, 2]-faces[:, 0]))
    face_normals *= np.where(face_normals[:, 2:3] < 0, -1, 1)
    means = np.asarray(MEANS)
    families = np.zeros(len(tri), int) if rock.tri_materials is None else rock.tri_materials
    from cliffcolors import linear
    # Use the original cliff tint across the whole strip. The rendered cliff's
    # outer rim already contains satellite colors (including photo shadows).
    source_colors = rock.strip_colors if rock.strip_colors is not None else rock.colors
    color = np.asarray(source_colors, float)[:, :3]
    if np.issubdtype(np.asarray(source_colors).dtype, np.integer) or color.max(initial=0) > 1:
        color /= 255
    # BeamNG decodes vertex RGB as sRGB; compute material multiplication in light.
    albedo = linear(color)[tri]*means[families, None, :]
    patches = []; count = 0
    # Spread a bounded selection over the tile, rather than consuming the
    # entire budget on consecutive pieces of the first contact contour.
    runs = spread_runs(runs)
    for run in runs[:ATTEMPTS_MAX]:
        lo, hi = run.min(0)-RECEIVER_MARGIN_M, run.max(0)+RECEIVER_MARGIN_M
        ids = np.flatnonzero(np.all(high >= lo, axis=1) & np.all(low <= hi, axis=1))
        if not len(ids):
            continue
        if count+len(ids) > PATCH_FACES_MAX:
            continue
        rn = np.repeat(face_normals[ids, None, :], 3, axis=1) if rock.normals is None else rock.normals[tri[ids]]
        patches.append(dict(run=run, faces=faces[ids], normals=rn.astype(np.float32),
                            albedo=albedo[ids].astype(np.float32)))
        count += len(ids)
    return patches


class TerrainSurface:
    """The quantized one-meter TerrainBlock lattice in the active anchor frame.

    Sample only requested corners; both queries and receiver triangles use the
    engine's alternating diagonals. No game-thread sampling is needed.
    """
    def __init__(self, frame, drawn, quantum):
        self.frame, self.drawn, self.quantum = frame, drawn, quantum

    def corners(self, xy):
        la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        z = np.asarray(self.frame.geodetic_to_enu(la, lo, self.drawn(la, lo))[2])
        if self.quantum:
            z = np.rint(z/self.quantum)*self.quantum
        return z

    def __call__(self, xy):
        cell = np.floor(xy/GROUND_GRID_M).astype(np.int64)
        frac = xy/GROUND_GRID_M-cell
        nodes = cell[:, None, :]+np.array([[0,0],[1,0],[0,1],[1,1]])
        unique, inverse = np.unique(nodes.reshape(-1, 2), axis=0, return_inverse=True)
        a,b,c,d = self.corners(unique*GROUND_GRID_M)[inverse].reshape(-1,4).T
        x,y = frac.T
        diagonal = ((cell[:,0]^cell[:,1]) & 1) == 0
        forward = np.where(x > y, a+x*(b-a)+y*(d-b), a+y*(c-a)+x*(d-c))
        backward = np.where(x+y < 1, a+x*(b-a)+y*(c-a), d+(1-y)*(b-d)+(1-x)*(c-d))
        return np.where(diagonal, forward, backward)

    def faces(self, lo, hi):
        return ground_faces(self.corners, lo, hi)


def ground_faces(ground_at, lo, hi):
    xs = np.arange(np.floor(lo[0]/GROUND_GRID_M)*GROUND_GRID_M, hi[0]+GROUND_GRID_M, GROUND_GRID_M)
    ys = np.arange(np.floor(lo[1]/GROUND_GRID_M)*GROUND_GRID_M, hi[1]+GROUND_GRID_M, GROUND_GRID_M)
    xx, yy = np.meshgrid(xs, ys)
    xy = np.column_stack((xx.ravel(), yy.ravel()))
    gp = np.column_stack((xy, ground_at(xy)))
    j,i = np.mgrid[:len(ys)-1, :len(xs)-1]
    a=(j*len(xs)+i).ravel();b=a+1;c=a+len(xs);d=c+1
    even = (((i+round(xs[0]/GROUND_GRID_M)) ^ (j+round(ys[0]/GROUND_GRID_M))) & 1).ravel() == 0
    gt = np.concatenate((np.column_stack((a[even],b[even],d[even])),
                         np.column_stack((a[even],d[even],c[even])),
                         np.column_stack((a[~even],b[~even],c[~even])),
                         np.column_stack((b[~even],d[~even],c[~even]))))
    return gp[gt]


def map_patches(patches, source, target, origin):
    """Map cached receiver patches into the anchor that owns the drawn terrain."""
    import geotiles
    r, _, _ = geotiles.placement(source, target, (0,0,0))
    out = []
    for patch in patches:
        out.append(dict(run=geotiles.world_map(source, target, patch['run']+origin),
                        faces=geotiles.world_map(source, target, patch['faces'].reshape(-1,3)+origin).reshape(-1,3,3),
                        normals=unit(patch['normals'] @ r.T), albedo=patch['albedo']))
    return out


def tile_parts(rock, ground_at):
    return project_parts(prepare(rock, ground_at), ground_at)


def _strip_tint(strip, projector, faces, albedo, ground_count):
    """Blend receiver colors in linear light, with bounded ground-neighbor batches."""
    # Tint the ground half from nearby rock; interpolate exact rock tint on
    # the rock half. No satellite texture fetch or additional material variants.
    cp = faces.reshape(-1, 3); cv = albedo.reshape(-1, 3)
    tint = np.empty((len(strip.positions), 3))
    mask = strip.receiver >= ground_count
    # Ground inherits broad nearby cliff color, not every duplicated triangle
    # corner. A 2 m color grid bounds this lookup; exact receiver barycentrics
    # below retain full color detail on the rock side.
    _, groups = np.unique(np.floor(cp/2).astype(np.int64), axis=0, return_inverse=True)
    count = np.bincount(groups)
    centers = np.column_stack([np.bincount(groups, weights=cp[:,k])/count for k in range(3)])
    palette = np.column_stack([np.bincount(groups, weights=cv[:,k])/count for k in range(3)])
    ground_ids = np.flatnonzero(~mask)
    for start in range(0, len(ground_ids), 64):
        ids = ground_ids[start:start+64]
        distance = np.sum((strip.positions[ids,None]-centers[None])**2, axis=2)
        tint[ids] = palette[distance.argmin(1)]
    if mask.any():
        local_ids = strip.receiver[mask]
        f = projector.local[local_ids, :, :2]
        point = ((strip.positions[mask]-projector.origin) @ projector.axes.T)[:, :2]
        bary = _barycentric(f, point)
        tint[mask] = np.einsum('ij,ijk->ik', bary, albedo[local_ids-ground_count])
    return tint


def project_parts(patches, ground_at):
    """Project bounded cached patches onto this anchor's actual terrain lattice."""
    from cliffcolors import srgb
    parts = []; total = 0
    for patch in patches:
        if total >= TILE_TRIANGLES - 512:
            break
        run, faces, rn, albedo = (patch[k] for k in ('run', 'faces', 'normals', 'albedo'))
        lo, hi = run.min(0)-RECEIVER_MARGIN_M, run.max(0)+RECEIVER_MARGIN_M
        ground = ground_at.faces(lo,hi) if hasattr(ground_at,'faces') else ground_faces(ground_at,lo,hi)
        gn = unit(np.cross(ground[:, 1]-ground[:, 0], ground[:, 2]-ground[:, 0]))
        receiver = np.concatenate((ground, faces))
        normals = np.concatenate((np.repeat(gn[:, None, :], 3, axis=1), rn))
        normal = unit(rn.mean(axis=(0,1))+gn.mean(0))
        projector = Projector(receiver, normal, run.mean(0), vertex_normals=normals, index=False)
        try:
            strip = build(run, projector, half_width=CONTACT_HALF_WIDTH_M, spacing=CONTACT_SAMPLE_M, end_fade=4.0)
        except ValueError as exc:
            if 'budget' not in str(exc):
                raise
            continue
        if strip is None or total+len(strip.tris) > TILE_TRIANGLES:
            continue
        tint = _strip_tint(strip, projector, faces, albedo, len(ground))
        # The projection-axis lift can cross into the next terrain triangle on
        # a steep fold. Correct the final XY location, not the pre-lift sample.
        strip.positions[:, 2] = np.maximum(strip.positions[:, 2],
                                           ground_at(strip.positions[:, :2])+GROUND_CLEARANCE_M)
        colors = np.column_stack((np.rint(srgb(np.clip(tint, 0, 1))*255), np.full(len(tint), 255))).astype(np.uint8)
        parts.append((strip, colors)); total += len(strip.tris)
    return pack_parts(parts)


def write(path, part):
    import cdae
    strip, colors = part
    return cdae.write(str(path), strip.positions, strip.tris, uvs=strip.uvs, uvs2=strip.uvs2,
                      normals=strip.normals, colors=colors, material=MATERIAL,
                      object_name='cliffContact', tangent_uvs=strip.uvs)
