"""Embedded cliff outcrops, companion rocks, and settled toe rubble.

Detection coordinates x/y are the world geographic lattice in degree-metres;
scale converts horizontal distances to metres at the tile latitude. Surface
samples and emitted rows use the tile's ENU frame. Ownership is decided at the
support point before burial so neighboring tiles choose the same rock once.
"""

from cliffsettings import CLIFF_KERNEL
import math
import dmath

import numpy as np
from runtimeprofiler import step as profile_step

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
    """Road corridors for rubble, including bridges and mapped tunnels.

    The cliff detector deliberately uses ground roads only. Rubble must also
    avoid bridge/deck approaches (Hoover Dam Access Road is partly elevated).
    Tunnel tags can span open approaches and galleries, so their complete XY
    corridor must also stay clear. This conservatively omits rubble above bores.
    This uses already-fetched cells and adds no elevation or network queries.
    """
    import mapgeom
    import roads

    segments = {str(k): (pts[:, :2], spec.width / 2) for k, (_, spec, pts, _) in enumerate(ground_roads)}
    for feats in road_cells:
        if feats is None or not len(feats):
            continue
        for specs, parts in ((roads.bridge_specs(feats), roads.bridge_parts),
                             (roads.tunnel_specs(feats), roads.tunnel_parts)):
            for i, spec in enumerate(specs):
                if spec is None:
                    continue
                for ring in parts(feats, i):
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


def mineral_tints(biomes, rgb=None, family=0):
    """Match the cliff's final albedo, including its texture and material gain.

    The bounded RGB palette shares private Forest materials across nearby rocks.
    Sample the source cliff, not the vegetation or shadow under the fallen rock.
    """
    import cliffrocks as cr
    import cliffcolors as cc
    import cliffmaterial as cm
    reference = cc.linear(cm.CANYON_MEDIAN_SRGB) * cm.CANYON_COLOR_FACTOR
    palette = (reference.tolist(), cc.CANYON_LIGHT_BLEND, cc.CANYON_HUE_BLEND,
               cm.SATELLITE_HEADROOM, TINT_STEP)
    return cr.rock_colors(biomes, rgb, family, palette=palette)


def base_seeds(xs, ys, keep, heights):
    """Native downhill boundary detection; uphill rims never seed rubble."""
    import fast
    fast.require(CLIFF_KERNEL, "cliff rubble seeds")
    return fast._ef.cliff_base_seeds(np.ascontiguousarray(xs, float), np.ascontiguousarray(ys, float),
        np.ascontiguousarray(heights, float), np.ascontiguousarray(keep, bool))


def base_cells(xs, ys, keep, seeds, scale):
    """Native bounded downhill fans with deterministic nearest-seed ownership."""
    import fast
    fast.require(CLIFF_KERNEL, "cliff rubble cells")
    return fast._ef.cliff_base_cells(np.ascontiguousarray(seeds, float),
        float(xs[0]), float(ys[0]), float(xs[1]-xs[0]), float(ys[1]-ys[0]),
        keep.shape[1], keep.shape[0], scale, REACH_M, SCATTER_M)


def surface_sampler(xs, ys, heights, surface):
    """Interpolate the same two triangles used by the visible cliff mesh.

    Sample its displacement, not an unfaded heightmap value: the latter leaves
    rocks floating metres away from the edge where cliff relief fades to zero.
    Outside the skin the terrain wins over the buried boundary vertices.
    """
    import fast
    fast.require(CLIFF_KERNEL, "cliff surface placement")
    heights = np.ascontiguousarray(heights, float)
    surface = np.ascontiguousarray(surface, float)

    def sample(x, y, z):
        points = np.ascontiguousarray(np.column_stack((x, y, z)), float)
        return fast._ef.cliff_sample_surface(float(xs[0]), float(ys[0]),
            float(xs[1] - xs[0]), float(ys[1] - ys[0]), heights, surface, points)

    return sample


def rough_face(sample, x, y, positions, normal, height, scale):
    """Require footprint-scale relief and changing normals, not just a steep plane.

    Eight samples around the intended boulder measure the rendered rock surface.
    Removing the local tangent plane avoids confusing slope with roughness.
    """
    import fast
    fast.require(CLIFF_KERNEL, "cliff outcrop roughness")
    queries = fast._ef.cliff_footprint_queries(
        np.ascontiguousarray(np.column_stack((x, y, height)), float), scale)
    # Python performs the terrain/imagery I/O; native code owns the geometry.
    rings = np.stack([sample(queries[:, k, 0], queries[:, k, 1]) for k in range(queries.shape[1])], axis=1)
    return fast._ef.cliff_rough_faces(np.ascontiguousarray(rings, float),
        np.ascontiguousarray(positions, float), np.ascontiguousarray(normal, float),
        np.ascontiguousarray(height, float), OUTCROP_MIN_RELIEF_M,
        OUTCROP_RELIEF_RATIO, OUTCROP_NORMAL_SPREAD_DEG)



def _allowed_cells(classes, blocked, scale):
    """Require all four corners to be dry, open ground outside the city buffer."""
    import cliffrocks as cr
    import fast
    return fast._ef.cliff_allowed_cells(
        np.ascontiguousarray(classes, np.uint8), np.ascontiguousarray(blocked, bool),
        math.ceil(cr.CITY_BUFFER_M / (cr.GRID_M * scale)))


def _owned(positions, box):
    """Half-open support-point ownership avoids duplicate rocks across tile seams."""
    import fast
    return fast._ef.cliff_owned(np.ascontiguousarray(positions, float), box)


def _surface_normal(sample, x, y, scale):
    """Upward normals of the displaced face, sampled over one physical metre."""
    import fast
    samples = np.stack((sample(x + 0.5 / scale, y), sample(x - 0.5 / scale, y),
                        sample(x, y + 0.5), sample(x, y - 0.5)), axis=1)
    return fast._ef.cliff_surface_normals(np.ascontiguousarray(samples, float))


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

    if skin_at is None or not np.any(keep):
        return []
    scale = dmath.cos(math.radians(frame.lat0))
    import fast
    fast.require(CLIFF_KERNEL, "cliff outcrop candidates")
    candidates = fast._ef.cliff_outcrop_candidates(np.ascontiguousarray(xs, float),
        np.ascontiguousarray(ys, float), np.ascontiguousarray(keep, bool),
        np.ascontiguousarray(blocked, bool), OUTCROP_SPACING_M, OUTCROP_DENSITY)
    if not len(candidates):
        return []
    gi, gj = candidates[:, :2].astype(np.int64).T
    x, y = candidates[:, 2:4].T
    i, j = candidates[:, 4:].astype(np.int64).T
    allowed = _allowed_cells(classes, blocked, scale) & keep
    la, lo = y / cr.WORLD_M_PER_DEG, (x / cr.WORLD_M_PER_DEG + 180) % 360 - 180
    family = np.broadcast_to(cm.natural_family(la, lo), la.shape) if families is None else families[j, i]
    valid = allowed[j, i] & (family == cm.ARID_CUT)
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
    owned = _owned(positions, box)
    properties, poses = fast._ef.cliff_outcrop_poses(
        np.ascontiguousarray(positions), np.ascontiguousarray(normal),
        np.ascontiguousarray(np.column_stack((gi, gj)), np.int64),
        OUTCROP_HEIGHT_M, OUTCROP_BURIAL, OUTCROP_MAX_TILT_DEG, OUTCROP_CENTER_HEIGHT)
    height, yaw, rank, slope = properties.T
    rough = rough_face(sample, x, y, positions, normal, height, scale)
    positions, tangent, bitangent, up = poses.transpose(1, 0, 2)
    valid = rough & (slope >= OUTCROP_MIN_SLOPE_DEG) & np.all(np.isfinite(positions), axis=1)
    valid &= road_clear(positions, np.round(height, 3), road_segments)
    rgb = None if color_at is None else color_at(la, lo)
    tint = mineral_tints(biomes[j, i], rgb, family)
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
        companions = fast._ef.cliff_companion_candidates(
            np.ascontiguousarray(np.column_stack((x[parents], y[parents], normal[parents], height[parents]))),
            np.ascontiguousarray(np.column_stack((gi[parents], gj[parents])), np.int64),
            slot, COMPANION_COUNT, scale, *COMPANION_HEIGHT_M)
        sx, sy, sh, companion_rank = companions.T
        si, sj = np.searchsorted(xs, sx, side='right') - 1, np.searchsorted(ys, sy, side='right') - 1
        inside = (si >= 0) & (si < keep.shape[1]) & (sj >= 0) & (sj < keep.shape[0])
        k = np.flatnonzero(inside)
        if not len(k):
            continue
        parent = parents[k]
        sx, sy, si, sj, sh, companion_rank = [a[k] for a in (sx, sy, si, sj, sh, companion_rank)]
        support = sample(sx, sy)
        sn = _surface_normal(sample, sx, sy, scale)
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
        seated = fast._ef.cliff_seat_companions(
            np.ascontiguousarray(support), np.ascontiguousarray(sn), np.ascontiguousarray(up[parent]),
            np.ascontiguousarray(sh), 0.40, OUTCROP_CENTER_HEIGHT)
        selected &= np.all(np.isfinite(seated), axis=1) & road_clear(seated, np.round(sh, 3), road_segments)
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


@profile_step('Cliff rock placement')
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

    if not np.any(keep):
        return []
    scale = dmath.cos(math.radians(frame.lat0))
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
    import fast
    points, ids, parents = fast._ef.cliff_rubble_candidates(
        np.ascontiguousarray(np.column_stack((xs[i], ys[j])), float), SPACING_M)
    x, y = points.T
    gi, gj = ids.T
    seed = seed[parents]
    la, lo = y / cr.WORLD_M_PER_DEG, (x / cr.WORLD_M_PER_DEG + 180) % 360 - 180
    z = np.asarray(elev(la, lo), float)
    e, n, _ = frame.geodetic_to_enu(la, lo, np.zeros_like(z))
    owned = _owned(np.column_stack((e, n)), box)
    d = 1.0 / cr.WORLD_M_PER_DEG
    gx = (elev(la, lo + d / scale) - elev(la, lo - d / scale)) / 2
    gy = (elev(la + d, lo) - elev(la - d, lo)) / 2
    base = seeds[seed]
    base_z = np.asarray(
        elev(base[:, 1] / cr.WORLD_M_PER_DEG, (base[:, 0] / cr.WORLD_M_PER_DEG + 180) % 360 - 180)
    )
    selected, metrics = fast._ef.cliff_rubble_selection(
        np.ascontiguousarray(np.column_stack((x, y, z, gx, gy, base_z)), float),
        np.ascontiguousarray(base, float), np.ascontiguousarray(np.column_stack((gi, gj)), np.int64),
        scale, REACH_M, SCATTER_M, FULL_SLOPE_DEG, MAX_SLOPE_DEG, MAX_DENSITY)
    steep, slope, distance = metrics.T
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
    # Sample the exact faded skin at the inner rim, original terrain outside.
    if skin_at is not None:
        skin = skin_at(x[k], y[k], z[k])
        se, sn, sz = frame.geodetic_to_enu(
            skin[:, 1] / cr.WORLD_M_PER_DEG, (skin[:, 0] / cr.WORLD_M_PER_DEG + 180) % 360 - 180, skin[:, 2]
        )
        positions = np.column_stack((se, sn, sz))
    else:
        positions = np.column_stack(frame.geodetic_to_enu(la[k], lo[k], z[k]))
    properties, poses = fast._ef.cliff_rubble_poses(
        np.ascontiguousarray(positions), np.ascontiguousarray(np.column_stack((gx[k], gy[k]))),
        np.ascontiguousarray(np.column_stack((steep[k], slope[k], distance[k]))),
        np.ascontiguousarray(np.column_stack((gi[k], gj[k])), np.int64),
        REACH_M, MAX_TILT_DEG, RUBBLE_BURIAL)
    height, yaw, rank = properties.T
    positions, tangent, bitangent, up = poses.transpose(1, 0, 2)
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


def debug_footprints(frame, rows):
    """Recorded placements only: preserve serialized kind IDs and map precision."""
    if not rows:
        return []
    positions = np.asarray([row[1:4] for row in rows], float)
    latitudes, longitudes, _ = frame.enu_to_geodetic(*positions.T)
    return [[1 if row[0].startswith('talus_large_') else 2 if row[0].startswith('talus_face_') else 0,
             round(float(lon), 7), round(float(lat), 7), round(float(row[6]) * .5, 2)]
            for row, lat, lon in zip(rows, latitudes, longitudes)]
