"""Embedded cliff outcrops, companion rocks, and settled toe rubble.

Detection coordinates x/y are the world geographic lattice in degree-metres;
scale converts horizontal distances to metres at the tile latitude. Surface
samples and emitted rows use the tile's ENU frame. Ownership is decided at the
support point before burial so neighboring tiles choose the same rock once.
"""

import math

import numpy as np

# Rubble rests on benches/ground, never on the generating wall or its crest.
FULL_SLOPE_DEG = 28.0
MAX_SLOPE_DEG = 44.0
REACH_M = 22.0
SCATTER_M = 5.0
SPACING_M = 2.0
MAX_DENSITY = 0.65
MAX_TILT_DEG = 28.0
TINT_STEP = 8.0
# Conservative pivot-centred sphere per metre of requested instance height.
# Bounds measured in BeamNG for the stock rock catalog: the widest is
# collection_02 (~2.20). This also encloses tilted clusters and off-centre pivots.
ROAD_RADIUS_PER_HEIGHT = 2.25
ROAD_SHOULDER_M = 0.5
ROAD_RADIUS_BIN_M = 0.5


# Sparse, world-anchored outcrops across confirmed arid faces, not loose toe stones.
OUTCROP_SPACING_M = 9.0
OUTCROP_DENSITY = 0.85
OUTCROP_MIN_SLOPE_DEG = 38.0
OUTCROP_HEIGHT_M = (4.0, 10.0)
OUTCROP_BURIAL = (0.35, 0.45)
RUBBLE_BURIAL = 0.22
OUTCROP_RELIEF_RATIO = 0.06
OUTCROP_MIN_RELIEF_M = 0.18
OUTCROP_NORMAL_SPREAD_DEG = 16.0
OUTCROP_MAX_TILT_DEG = 12.0
OUTCROP_CENTER_HEIGHT = 0.30
COMPANION_COUNT = 5
COMPANION_HEIGHT_M = (0.7, 2.4)


def road_exclusion(frame, road_cells, urban, widths, ground_roads):
    """Road corridors for rubble, including elevated surface roads.

    The cliff detector deliberately uses ground roads only. Rubble must also
    avoid bridge/deck approaches (Hoover Dam Access Road is partly elevated).
    Conservatively exclude their XY corridor; tunnels remain available above.
    This uses already-fetched cells and adds no elevation or network queries.
    """
    import mapgeom
    import roads

    segments = {str(k): (pts[:, :2], spec.width / 2) for k, (_, spec, pts, _) in enumerate(ground_roads)}
    for feats in road_cells:
        if feats is None or not len(feats):
            continue
        specs = roads.road_specs(feats, urban, widths)
        for i, spec in enumerate(specs):
            if spec is None:
                continue
            for ring in roads.bridge_parts(feats, i):
                e, n = mapgeom.enu_xy(feats, frame, ring)
                if len(e) >= 2:
                    segments[str(len(segments))] = (np.column_stack((e, n)), spec.width / 2)
    return roads._Segs(segments)


def road_clear(positions, heights, road_segments):
    """Check final seated footprints, batching native segment tests by size.

    Rounded-up radius bins bound the number of native calls and avoid a Python
    rock-by-road loop. The half-metre shoulder also covers output quantization.
    """
    clear = np.ones(len(positions), bool)
    if road_segments is None or not len(road_segments.h):
        return clear
    radius = np.asarray(heights) * ROAD_RADIUS_PER_HEIGHT + ROAD_SHOULDER_M
    bins = np.ceil(radius / ROAD_RADIUS_BIN_M).astype(int)
    for size in np.unique(bins):
        k = np.flatnonzero(bins == size)
        clear[k] = ~road_segments.near(positions[k, :2], size * ROAD_RADIUS_BIN_M)
    return clear


def probability(slope, distance):
    """Dense at the toe, thinning onto stable ground farther downhill."""
    t = np.clip((np.asarray(slope) - FULL_SLOPE_DEG) / (MAX_SLOPE_DEG - FULL_SLOPE_DEG), 0, 1)
    near = np.clip(1 - np.asarray(distance) / REACH_M, 0, 1)
    return MAX_DENSITY * (1 - t * t * (3 - 2 * t)) * near**1.4


def mineral_tints(biomes, rgb=None, family=0):
    """Match the cliff's final albedo, including its texture and material gain.

    The bounded RGB palette shares private Forest materials across nearby rocks.
    Sample the source cliff, not the vegetation or shadow under the fallen rock.
    """
    import cliffrocks as cr
    import cliffcolors as cc
    import cliffmaterial as cm
    from clifftransition import MEANS

    means = np.asarray(MEANS)
    tint = cr.rock_colors(biomes, rgb, family)[..., :3]
    canyon = np.asarray(family) == cm.CANYON
    if np.any(canyon):
        tint = np.where(canyon[..., None], cc.canyon_tint(biomes, rgb)[..., :3], tint)
    target = cc.srgb(cc.linear(tint.astype(float) / 255) * means[np.asarray(family)]) * 255
    return np.clip(np.rint(target / TINT_STEP) * TINT_STEP, 0, 255).astype(int)


def base_seeds(xs, ys, keep, heights):
    """Downhill exits of a sustained cliff mask; uphill rims never seed rubble."""
    h = (heights[:-1, :-1] + heights[1:, :-1] + heights[:-1, 1:] + heights[1:, 1:]) * 0.25
    seeds = []
    source_j, source_i = np.nonzero(keep)
    for dj, di in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        j, i = source_j, source_i
        oj, oi = j + dj, i + di
        valid = (oj >= 0) & (oj < keep.shape[0]) & (oi >= 0) & (oi < keep.shape[1])
        j, i, oj, oi = j[valid], i[valid], oj[valid], oi[valid]
        valid = ~keep[oj, oi] & (h[j, i] > h[oj, oi] + 0.25)
        j, i = j[valid], i[valid]
        if not len(i):
            continue
        x = (xs[i] + xs[i + 1]) * 0.5 + di * (xs[1] - xs[0]) * 0.5
        y = (ys[j] + ys[j + 1]) * 0.5 + dj * (ys[1] - ys[0]) * 0.5
        seeds.append(np.column_stack((x, y, np.full(len(i), di), np.full(len(i), dj), j, i)))
    return np.concatenate(seeds) if seeds else np.empty((0, 6))


def base_cells(xs, ys, keep, seeds, scale):
    """Bounded downhill fans, with at most five metres of lateral scatter.

    Work only around detected toes, not all candidate/seed pairs. Stable sorting
    makes nearest-base ownership deterministic across independently built tiles.
    """
    if not len(seeds):
        return np.empty((0, 3), int)
    dx, dy = xs[1] - xs[0], ys[1] - ys[0]
    si = np.floor((seeds[:, 0] - xs[0]) / dx).astype(int)
    sj = np.floor((seeds[:, 1] - ys[0]) / dy).astype(int)
    hits = []
    for dj in range(-math.ceil(REACH_M / dy), math.ceil(REACH_M / dy) + 1):
        for di in range(-math.ceil(REACH_M / (dx * scale)), math.ceil(REACH_M / (dx * scale)) + 1):
            i, j = si + di, sj + dj
            ox = (xs[0] + (i + 0.5) * dx - seeds[:, 0]) * scale
            oy = ys[0] + (j + 0.5) * dy - seeds[:, 1]
            along = ox * seeds[:, 2] + oy * seeds[:, 3]
            across = abs(ox * seeds[:, 3] - oy * seeds[:, 2])
            dist = np.hypot(ox, oy)
            valid = (i >= 0) & (i < keep.shape[1]) & (j >= 0) & (j < keep.shape[0])
            # Add one cell of query padding; exact candidate checks follow.
            valid &= (along >= -max(dx * scale, dy)) & (dist < REACH_M + dy) & (across < SCATTER_M + dy)
            k = np.flatnonzero(valid)
            if len(k):
                hits.append(np.column_stack((j[k] * keep.shape[1] + i[k], dist[k], k)))
    if not hits:
        return np.empty((0, 3), int)
    hits = np.concatenate(hits)
    hits = hits[np.lexsort((hits[:, 2], hits[:, 1], hits[:, 0]))]
    hits = hits[np.r_[True, np.diff(hits[:, 0]) != 0]].astype(int)
    return np.column_stack((hits[:, 0] // keep.shape[1], hits[:, 0] % keep.shape[1], hits[:, 2]))


def surface_sampler(xs, ys, heights, surface):
    """Interpolate the same two triangles used by the visible cliff mesh.

    Sample its displacement, not an unfaded heightmap value: the latter leaves
    rocks floating metres away from the edge where cliff relief fades to zero.
    Outside the skin the terrain wins over the buried boundary vertices.
    """
    ground = np.stack((*np.meshgrid(xs, ys), heights), axis=-1)
    delta = surface - ground
    # A buried skirt must not pull an otherwise exposed rock through the ground.
    delta[..., 2] = np.maximum(delta[..., 2], 0)

    def sample(x, y, z):
        u = np.clip((x - xs[0]) / (xs[1] - xs[0]), 0, len(xs) - 1 - 1e-6)
        v = np.clip((y - ys[0]) / (ys[1] - ys[0]), 0, len(ys) - 1 - 1e-6)
        i, j = u.astype(int), v.astype(int)
        u, v = (u - i)[:, None], (v - j)[:, None]
        a, b, c, d = delta[j, i], delta[j, i + 1], delta[j + 1, i + 1], delta[j + 1, i]
        offset = np.where(u >= v, a * (1 - u) + b * (u - v) + c * v, a * (1 - v) + c * u + d * (v - u))
        return np.column_stack((x, y, z)) + offset

    return sample


def rough_face(sample, x, y, positions, normal, height, scale):
    """Require footprint-scale relief and changing normals, not just a steep plane.

    Eight samples around the intended boulder measure the rendered rock surface.
    Removing the local tangent plane avoids confusing slope with roughness.
    """
    radius = height * 0.5
    ring = (
        np.stack(
            [
                sample(x + radius * np.cos(a) / scale, y + radius * np.sin(a))
                for a in np.arange(8) * np.pi / 4
            ],
            axis=1,
        )
        - positions[:, None, :]
    )
    distances = np.sum(ring * normal[:, None, :], axis=2)
    relief = np.std(distances, axis=1)
    facets = np.cross(ring, np.roll(ring, -1, axis=1))
    facets /= np.maximum(np.linalg.norm(facets, axis=2, keepdims=True), 1e-9)
    angles = np.degrees(np.arccos(np.clip(np.abs(np.sum(facets * normal[:, None, :], axis=2)), 0, 1)))
    spread = np.percentile(angles, 75, axis=1)
    return (relief >= np.maximum(OUTCROP_MIN_RELIEF_M, height * OUTCROP_RELIEF_RATIO)) & (
        spread >= OUTCROP_NORMAL_SPREAD_DEG
    )


def _allowed_cells(classes, blocked, scale):
    """Require all four corners to be dry, open ground outside the city buffer."""
    import cliffrocks as cr

    safe = np.isin(classes, (10, 20, 30, 60, 100))
    safe &= ~cr._grow(classes == 50, math.ceil(cr.CITY_BUFFER_M / (cr.GRID_M * scale)))
    return safe[:-1, :-1] & safe[1:, :-1] & safe[:-1, 1:] & safe[1:, 1:] & ~blocked


def _owned(positions, box):
    """Half-open support-point ownership avoids duplicate rocks across tile seams."""
    return (
        (positions[:, 0] >= box[0])
        & (positions[:, 0] < box[2])
        & (positions[:, 1] >= box[1])
        & (positions[:, 1] < box[3])
    )


def _surface_normal(sample, x, y, scale):
    """Upward normals of the displaced face, sampled over one physical metre."""
    dx = sample(x + 0.5 / scale, y) - sample(x - 0.5 / scale, y)
    dy = sample(x, y + 0.5) - sample(x, y - 0.5)
    normal = np.cross(dx, dy)
    normal /= np.maximum(np.linalg.norm(normal, axis=1, keepdims=True), 1e-9)
    return np.where(normal[:, 2:3] < 0, -normal, normal)


def _orientation(up, yaw):
    """Build a right-handed basis without tilting upright rocks into the wall."""
    tangent = np.column_stack((np.cos(yaw), np.sin(yaw), np.zeros(len(yaw))))
    tangent -= up * np.sum(tangent * up, axis=1, keepdims=True)
    tangent /= np.maximum(np.linalg.norm(tangent, axis=1, keepdims=True), 1e-9)
    return tangent, np.cross(up, tangent)


def _forest_row(prefix, tint, position, yaw, height, rank, tangent, bitangent, up):
    """Encode the shared 17-field Forest protocol, quantizing only at delivery."""
    group = prefix + ''.join(f'{value:02x}' for value in tint)
    basis = np.concatenate((tangent, bitangent, up))
    return [
        group,
        *np.round(position, 3).tolist(),
        round(float(np.degrees(yaw)), 2),
        1.0,
        round(float(height), 3),
        round(float(rank), 5),
        *np.round(basis, 6).tolist(),
    ]


def outcrop_rows(
    frame,
    box,
    xs,
    ys,
    keep,
    classes,
    blocked,
    biomes,
    elev,
    color_at=None,
    skin_at=None,
    road_segments=None,
    families=None,
):
    """Seat sparse rock projections inside the actual displaced arid cliff face."""
    import cliffrocks as cr
    import cliffmaterial as cm
    from vegetation import _hash01

    if skin_at is None or not np.any(keep):
        return []
    scale = math.cos(math.radians(frame.lat0))
    j, i = np.nonzero(keep & ~blocked)
    buckets = np.unique(
        np.column_stack((np.floor(xs[i] / OUTCROP_SPACING_M), np.floor(ys[j] / OUTCROP_SPACING_M))).astype(
            np.int64
        ),
        axis=0,
    )
    if not len(buckets):
        return []
    gi, gj = buckets.T
    x = (gi + 0.2 + 0.6 * _hash01(gi, gj, 51)) * OUTCROP_SPACING_M
    y = (gj + 0.2 + 0.6 * _hash01(gi, gj, 52)) * OUTCROP_SPACING_M
    i, j = np.searchsorted(xs, x, side='right') - 1, np.searchsorted(ys, y, side='right') - 1
    valid = (i >= 0) & (i < keep.shape[1]) & (j >= 0) & (j < keep.shape[0])
    k = np.flatnonzero(valid)
    gi, gj, x, y, i, j = [a[k] for a in (gi, gj, x, y, i, j)]
    allowed = _allowed_cells(classes, blocked, scale) & keep
    la, lo = y / cr.WORLD_M_PER_DEG, (x / cr.WORLD_M_PER_DEG + 180) % 360 - 180
    family = np.broadcast_to(cm.natural_family(la, lo), la.shape) if families is None else families[j, i]
    valid = allowed[j, i] & (family == cm.ARID_CUT) & (_hash01(gi, gj, 53) < OUTCROP_DENSITY)
    k = np.flatnonzero(valid)
    gi, gj, x, y, i, j, la, lo, family = [a[k] for a in (gi, gj, x, y, i, j, la, lo, family)]
    if not len(k):
        return []

    def sample(a, b):
        z = np.asarray(elev(b / cr.WORLD_M_PER_DEG, (a / cr.WORLD_M_PER_DEG + 180) % 360 - 180), float)
        skin = skin_at(a, b, z)
        return np.column_stack(
            frame.geodetic_to_enu(
                skin[:, 1] / cr.WORLD_M_PER_DEG,
                (skin[:, 0] / cr.WORLD_M_PER_DEG + 180) % 360 - 180,
                skin[:, 2],
            )
        )

    positions = sample(x, y)
    # Normals of the displaced surface, including relief rather than only DEM slope.
    normal = _surface_normal(sample, x, y, scale)
    slope = np.degrees(np.arccos(np.clip(normal[:, 2], -1, 1)))
    owned = _owned(positions, box)
    height = OUTCROP_HEIGHT_M[0] + np.diff(OUTCROP_HEIGHT_M)[0] * _hash01(gi, gj, 54) ** 1.35
    rough = rough_face(sample, x, y, positions, normal, height, scale)
    yaw = _hash01(gi, gj, 55) * 2 * np.pi
    # Keep the mass gravity-oriented. The face normal controls burial, never
    # the boulder's tall axis: perpendicular wall alignment looks glued on.
    tilt = np.radians(np.minimum(slope * 0.12, OUTCROP_MAX_TILT_DEG))
    horizontal = normal[:, :2] / np.maximum(np.linalg.norm(normal[:, :2], axis=1, keepdims=True), 1e-9)
    up = np.column_stack((horizontal * np.sin(tilt)[:, None], np.cos(tilt)))
    tangent, bitangent = _orientation(up, yaw)
    burial = OUTCROP_BURIAL[0] + np.diff(OUTCROP_BURIAL)[0] * _hash01(gi, gj, 56)
    positions -= normal * (height * burial)[:, None]
    # Utah pivots sit below the volume centre (about 17-43% of model height).
    # Lower the shared placement so the visible mass, not its foot, meets the face.
    positions -= up * (height * OUTCROP_CENTER_HEIGHT)[:, None]
    valid = rough & (slope >= OUTCROP_MIN_SLOPE_DEG) & np.all(np.isfinite(positions), axis=1)
    valid &= road_clear(positions, np.round(height, 3), road_segments)
    rgb = None if color_at is None else color_at(la, lo)
    tint = mineral_tints(biomes[j, i], rgb, family)
    rank = _hash01(gi, gj, 57)
    out = []
    for at in np.flatnonzero(valid & owned):
        out.append(
            _forest_row(
                'talus_large_',
                tint[at],
                positions[at],
                yaw[at],
                height[at],
                rank[at],
                tangent[at],
                bitangent[at],
                up[at],
            )
        )
    # Companions are independently owned at their support point, so a cluster
    # crossing a tile boundary is neither duplicated nor clipped to its parent.
    parents = np.flatnonzero(valid)
    for slot in range(COMPANION_COUNT):
        if not len(parents):
            break
        pi, pj = gi[parents], gj[parents]
        angle = (slot + _hash01(pi, pj, 61 + slot)) * 2 * np.pi / COMPANION_COUNT
        radius = height[parents] * (0.35 + 0.30 * _hash01(pi, pj, 71 + slot))
        # Measure spacing along the face, not its horizontal projection.
        grade = (normal[parents, 0] * np.cos(angle) + normal[parents, 1] * np.sin(angle)) / np.maximum(
            normal[parents, 2], 0.1
        )
        radius /= np.sqrt(1 + grade * grade)
        sx = x[parents] + np.cos(angle) * radius / scale
        sy = y[parents] + np.sin(angle) * radius
        si, sj = np.searchsorted(xs, sx, side='right') - 1, np.searchsorted(ys, sy, side='right') - 1
        inside = (si >= 0) & (si < keep.shape[1]) & (sj >= 0) & (sj < keep.shape[0])
        k = np.flatnonzero(inside)
        if not len(k):
            continue
        parent = parents[k]
        sx, sy, si, sj, pi, pj = [a[k] for a in (sx, sy, si, sj, pi, pj)]
        support = sample(sx, sy)
        sn = _surface_normal(sample, sx, sy, scale)
        sh = COMPANION_HEIGHT_M[0] + np.diff(COMPANION_HEIGHT_M)[0] * _hash01(pi, pj, 81 + slot)
        local_family = (
            np.broadcast_to(
                cm.natural_family(sy / cr.WORLD_M_PER_DEG, (sx / cr.WORLD_M_PER_DEG + 180) % 360 - 180),
                sx.shape,
            )
            if families is None
            else families[sj, si]
        )
        selected = allowed[sj, si] & (local_family == cm.ARID_CUT)
        selected &= _owned(support, box)
        selected &= rough_face(sample, sx, sy, support, sn, np.maximum(sh, height[parent] * 0.75), scale)
        seated = support - sn * (sh * 0.40)[:, None] - up[parent] * (sh * OUTCROP_CENTER_HEIGHT)[:, None]
        selected &= np.all(np.isfinite(seated), axis=1) & road_clear(seated, np.round(sh, 3), road_segments)
        companion_rank = _hash01(pi, pj, 91 + slot)
        for at in np.flatnonzero(selected):
            owner = parent[at]
            out.append(
                _forest_row(
                    'talus_face_',
                    tint[owner],
                    seated[at],
                    yaw[owner],
                    sh[at],
                    companion_rank[at],
                    tangent[owner],
                    bitangent[owner],
                    up[owner],
                )
            )
    return out


def rows(
    frame,
    box,
    xs,
    ys,
    keep,
    classes,
    blocked,
    biomes,
    elev,
    color_at=None,
    skin_at=None,
    road_segments=None,
    families=None,
):
    args = (frame, box, xs, ys, keep, classes, blocked, biomes, elev, color_at, skin_at, road_segments)
    return rubble_rows(*args, families=families) + outcrop_rows(*args, families=families)


def rubble_rows(
    frame,
    box,
    xs,
    ys,
    keep,
    classes,
    blocked,
    biomes,
    elev,
    color_at=None,
    skin_at=None,
    road_segments=None,
    families=None,
):
    """17-field Forest rows with a tilted orthonormal basis and regional tint.

    The same geographic lattice owns every candidate across tile boundaries.
    The qualifying cliff mask is mandatory: a steep isolated anomaly never seeds
    rubble. City/road/water exclusions apply to its whole coarse footprint.
    """
    import cliffrocks as cr
    from vegetation import _hash01

    if not np.any(keep):
        return []
    scale = math.cos(math.radians(frame.lat0))
    xx, yy = np.meshgrid(xs, ys)
    heights = np.asarray(elev(yy / cr.WORLD_M_PER_DEG, (xx / cr.WORLD_M_PER_DEG + 180) % 360 - 180))
    seeds = base_seeds(xs, ys, keep, heights)
    cells = base_cells(xs, ys, keep, seeds, scale)
    if not len(cells):
        return []
    allowed = _allowed_cells(classes, blocked, scale)
    j, i, seed = cells.T
    source_j, source_i = seeds[seed, 4:].astype(int).T
    valid = allowed[j, i] & ~blocked[source_j, source_i]
    j, i, seed = j[valid], i[valid], seed[valid]
    if not len(i):
        return []
    # Four independent 2 m candidates per detection cell, jittered away from edges.
    i, j = np.repeat(i, 4), np.repeat(j, 4)
    seed = np.repeat(seed, 4)
    sub = np.tile(np.arange(4), len(i) // 4)
    gi = np.rint(xs[i] / SPACING_M).astype(np.int64) + sub % 2
    gj = np.rint(ys[j] / SPACING_M).astype(np.int64) + sub // 2
    x = (gi + 0.2 + 0.6 * _hash01(gi, gj, 31)) * SPACING_M
    y = (gj + 0.2 + 0.6 * _hash01(gi, gj, 32)) * SPACING_M
    la, lo = y / cr.WORLD_M_PER_DEG, (x / cr.WORLD_M_PER_DEG + 180) % 360 - 180
    z = np.asarray(elev(la, lo), float)
    e, n, _ = frame.geodetic_to_enu(la, lo, np.zeros_like(z))
    owned = _owned(np.column_stack((e, n)), box)
    d = 1.0 / cr.WORLD_M_PER_DEG
    gx = (elev(la, lo + d / scale) - elev(la, lo - d / scale)) / 2
    gy = (elev(la + d, lo) - elev(la - d, lo)) / 2
    steep = np.hypot(gx, gy)
    slope = np.degrees(np.arctan(steep))
    base = seeds[seed]
    ox, oy = (x - base[:, 0]) * scale, y - base[:, 1]
    along = ox * base[:, 2] + oy * base[:, 3]
    across = abs(ox * base[:, 3] - oy * base[:, 2])
    distance = np.hypot(ox, oy)
    # Scatter beside the intersection, then continue downhill as a thinning fan.
    footprint = (along >= -1) & (across <= SCATTER_M) & (distance < REACH_M)
    base_z = np.asarray(
        elev(base[:, 1] / cr.WORLD_M_PER_DEG, (base[:, 0] / cr.WORLD_M_PER_DEG + 180) % 360 - 180)
    )
    footprint &= z <= base_z + 0.5
    # Low-frequency deterministic clusters break up an evenly peppered apron.
    cluster = 0.45 + 0.55 * _hash01(gi // 4, gj // 4, 39)
    selected = (
        footprint & np.isfinite(z + steep) & (_hash01(gi, gj, 33) < probability(slope, distance) * cluster)
    )
    k = np.flatnonzero(selected)
    if not len(k):
        return []
    source_j, source_i = base[k, 4:].astype(int).T
    source_la = (ys[source_j] + ys[source_j + 1]) * 0.5 / cr.WORLD_M_PER_DEG
    source_lo = ((xs[source_i] + xs[source_i + 1]) * 0.5 / cr.WORLD_M_PER_DEG + 180) % 360 - 180
    rgb = None if color_at is None else color_at(source_la, source_lo)
    import cliffmaterial as cm

    source_family = (
        cm.natural_family(source_la, source_lo) if families is None else families[source_j, source_i]
    )
    tint = mineral_tints(biomes[source_j, source_i], rgb, source_family)
    height = (0.65 + 1.2 * _hash01(gi[k], gj[k], 34)) * (1 - 0.45 * distance[k] / REACH_M)
    yaw = _hash01(gi[k], gj[k], 35) * 2 * np.pi
    rank = _hash01(gi[k], gj[k], 36)
    # Stones rest at a shallower attitude than the face, with random heading.
    tilt = np.minimum(np.radians(slope[k]) * 0.65, math.radians(MAX_TILT_DEG))
    up = np.column_stack(
        (
            -gx[k] / np.maximum(steep[k], 1e-12) * np.sin(tilt),
            -gy[k] / np.maximum(steep[k], 1e-12) * np.sin(tilt),
            np.cos(tilt),
        )
    )
    tangent, bitangent = _orientation(up, yaw)
    # Use the exact faded skin for the inner rim, and original terrain outside.
    # Collection pivots are near their centre. Bury their centre a little along
    # the support normal so multiple constituent stones intersect the ground.
    normal = np.column_stack((-gx[k], -gy[k], np.ones(len(k)))) / np.sqrt(1 + steep[k] ** 2)[:, None]
    if skin_at is not None:
        skin = skin_at(x[k], y[k], z[k])
        se, sn, sz = frame.geodetic_to_enu(
            skin[:, 1] / cr.WORLD_M_PER_DEG, (skin[:, 0] / cr.WORLD_M_PER_DEG + 180) % 360 - 180, skin[:, 2]
        )
        positions = np.column_stack((se, sn, sz))
    else:
        positions = np.column_stack(frame.geodetic_to_enu(la[k], lo[k], z[k]))
    positions -= normal * (height * RUBBLE_BURIAL)[:, None]
    clear = owned[k] & road_clear(positions, np.round(height, 3), road_segments)
    out = []
    for at in np.flatnonzero(clear):
        out.append(
            _forest_row(
                'talus_',
                tint[at],
                positions[at],
                yaw[at],
                height[at],
                rank[at],
                tangent[at],
                bitangent[at],
                up[at],
            )
        )
    return out
