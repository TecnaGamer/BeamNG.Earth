"""Join a clipped local mesh to the rendered edge of its global neighbour."""

from dataclasses import replace
from itertools import combinations

import numpy as np

import fast
import geotiles


def global_edge(built, anchor, tile, side, z, x, y):
    """The neighbour's visible border, parameterized along the piece's Mercator edge."""
    opposite = (side + 2) % 4
    axis = int(opposite in (0, 2))
    end = float(opposite in (1, 2))
    indices = np.flatnonzero(built.planar[:, axis] == end)
    if len(indices) < 2:
        return np.empty(0), np.empty((0, 3))
    along = built.planar[indices, 1 - axis]
    # The lower duplicates are the tile's skirt. Keep the upper vertex.
    order = np.lexsort((-built.positions[indices, 2], along))
    ordered = indices[order]
    parameter, first = np.unique(along[order], return_index=True)
    rotation, shift, _ = geotiles.placement(geotiles.frame(x, y, z), anchor, built.pos)
    world = (built.positions[ordered[first]] + built.pos) @ rotation.T + shift
    cells = 1 << (tile.cz - z)
    offset = tile.cx - x * cells if axis else (y + 1) * cells - 1 - tile.cy
    lo, hi = offset / cells, (offset + 1) / cells
    knots = np.r_[lo, parameter[(parameter > lo) & (parameter < hi)], hi]
    world = _interpolate(parameter, world, knots)
    fraction = (knots - lo) * cells
    # The local quad is counter-clockwise. Its north and west sides run
    # against the neighbour's increasing easting/northing coordinates.
    if side in (2, 3):
        return 1.0 - fraction[::-1], world[::-1]
    return fraction, world


def _interpolate(profile, values, distance):
    """Evaluate the piecewise-linear profile, including the sub-metre corner projection."""
    i = np.clip(np.searchsorted(profile, distance) - 1, 0, len(profile) - 2)
    f = (distance - profile[i]) / (profile[i + 1] - profile[i])
    return values[i] + f[:, None] * (values[i + 1] - values[i])


def _split_edge(built, on_edge, along, knots):
    """Put the global profile's breakpoints into the local border triangles."""
    attributes = np.column_stack((built.positions, built.normals, built.uvs, built.planar))
    at_edge = np.count_nonzero(on_edge[built.tris], axis=1)
    # The old cliff-like skirt hangs below a now-joined edge. Its intermediate
    # vertices would fold in plan as the border follows the curved world edge.
    tris = built.tris[at_edge < 3]
    split_faces = np.flatnonzero(at_edge[at_edge < 3] == 2)
    kept = np.ones(len(tris), dtype=bool)
    extras, faces, edges = [], [], {}

    def extra(vertex):
        extras.append(vertex)
        return len(attributes) + len(extras) - 1

    for fi in split_faces:
        a, b, c = map(int, tris[fi])
        ring = []
        for start, end in ((a, b), (b, c), (c, a)):
            ring.append(start)
            if not (on_edge[start] and on_edge[end]) or abs(along[start] - along[end]) <= 1e-6:
                continue
            u, v = sorted((start, end))
            if (u, v) not in edges:
                lo, hi = sorted((along[u], along[v]))
                inside = knots[np.searchsorted(knots, lo + 1e-6):np.searchsorted(knots, hi - 1e-6)]
                f = (inside - along[u]) / (along[v] - along[u])
                if along[u] > along[v]:
                    f = f[::-1]
                edges[u, v] = [extra(attributes[u] + t * (attributes[v] - attributes[u])) for t in f]
            inserted = edges[u, v]
            ring.extend(inserted if start == u else reversed(inserted))
        if len(ring) > 3:
            kept[fi] = False
            centre = extra(attributes[[a, b, c]].mean(axis=0))
            faces.extend((centre, u, v) for u, v in zip(ring, ring[1:] + ring[:1]))

    if extras:
        attributes = np.vstack((attributes, extras))
        tris = np.vstack((tris[kept], faces))
    used, indices = np.unique(tris, return_inverse=True)
    return attributes[used], indices.reshape(-1, 3)


def _edge_profile(built, edge_start, edge_end, target, spacing):
    start, stop = np.asarray(edge_start), np.asarray(edge_end)
    length = np.linalg.norm(stop - start)
    direction = (stop - start) / length
    inward = np.array([-direction[1], direction[0]])
    world = built.positions + built.pos
    along = (world[:, :2] - start) @ direction
    signed = (world[:, :2] - start) @ inward
    on = np.abs(signed) < 1e-6
    local, inverse = np.unique(np.round(along[on], 7), return_inverse=True)
    fraction, target = target
    if len(local) < 2 or len(fraction) < 2:
        return None
    lz = np.full(len(local), -np.inf)
    np.maximum.at(lz, inverse, world[on, 2])
    far = fraction * length

    attributes, tris = _split_edge(built, on, along, far)
    clipped = replace(built, positions=attributes[:, :3], normals=attributes[:, 3:6], uvs=attributes[:, 6:8],
                      planar=attributes[:, 8:10], tris=tris)
    return clipped, (start, direction, inward, local, lz, far, target, spacing)


def _correction(world, profile):
    start, direction, inward, local, lz, far, target, spacing = profile
    along = (world[:, :2] - start) @ direction
    signed = (world[:, :2] - start) @ inward
    on_rim = np.abs(signed) < 1e-6
    at = _interpolate(far, target, along)
    rim = np.column_stack((start + along[:, None] * direction, np.interp(along, local, lz)))
    delta = at - rim
    # Take at least one far cell to blend. Larger height differences get a
    # broader shoulder to limit the grade introduced by the cubic transition.
    width = np.hypot(spacing, 6.0 * np.max(np.abs(delta[on_rim, 2])))
    t = np.where(on_rim, 0., np.clip(np.abs(signed) / width, 0.0, 1.0))
    fade = (1.0 - t) ** 2 * (1.0 + 2.0 * t)
    gradient = -6.0 * t * (1.0 - t) * np.sign(signed) * delta[:, 2] / width
    return fade, delta, gradient[:, None] * inward, on_rim


def _trim_crossing_rims(outgoing, incoming):
    """Trim the tips already covered by the two global tiles' overlap."""
    out_fraction, a = outgoing
    in_fraction, b = incoming
    # Work away from the shared corner on both rims. Test the full profiles,
    # since a knot or a local mesh vertex may precede the crossing.
    b = b[::-1]
    in_fraction = 1. - in_fraction[::-1]
    u = np.diff(a, axis=0)
    v = np.diff(b, axis=0)
    det = u[:, 1:2] * v[None, :, 0] - u[:, :1] * v[None, :, 1]
    dx = b[None, :-1, 0] - a[:-1, None, 0]
    dy = b[None, :-1, 1] - a[:-1, None, 1]
    distinct = np.abs(det) > 1e-12
    s = np.divide(dy * v[None, :, 0] - dx * v[None, :, 1], det, out=np.full_like(det, np.nan), where=distinct)
    t = np.divide(dy * u[:, :1] - dx * u[:, 1:2], det, out=np.full_like(det, np.nan), where=distinct)
    i, j = np.nonzero((0. <= s) & (s <= 1.) & (0. <= t) & (t <= 1.))
    if not len(i):
        return outgoing, incoming
    so = out_fraction[i] + s[i, j] * (out_fraction[i+1] - out_fraction[i])
    ti = in_fraction[j] + t[i, j] * (in_fraction[j+1] - in_fraction[j])
    first = np.argmin(so + ti)
    i, j = i[first], j[first]
    at = (a[i] + s[i, j] * u[i] + b[j] + t[i, j] * v[j]) * .5
    keep_out = out_fraction > so[first] + 1e-9
    keep_in = in_fraction > ti[first] + 1e-9
    if not (keep_out.any() and keep_in.any()):
        return outgoing, incoming
    return ((np.r_[0., out_fraction[keep_out]], np.vstack((at, a[keep_out]))),
            (np.r_[1. - in_fraction[keep_in][::-1], 1.], np.vstack((b[keep_in][::-1], at))))


def _corner_triangles(endpoints, attributes, chain):
    rim = np.vstack((endpoints[0], attributes[chain], endpoints[1]))
    joined = np.asarray(fast._ef.triangulate(np.ascontiguousarray(rim[:, :2])))
    if len(joined) != len(rim) - 2:
        return None
    a, b, c = rim[joined, :2].transpose(1, 0, 2)
    ab, ac = b - a, c - a
    if np.any(ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0] <= 0):
        return None
    return joined


def _close_corners(built, original, on_rim, displacement):
    """Replace a corner fan by a ring that ends exactly on each rendered rim."""
    corners = np.flatnonzero(np.count_nonzero(on_rim, axis=0) == 2)
    if not len(corners):
        return built
    attributes = np.column_stack((built.positions, built.normals, built.uvs, built.planar))
    faces = built.tris
    for corner in corners:
        sides = np.flatnonzero(on_rim[:, corner])
        ends = original[corner] + displacement[sides, corner]
        if np.linalg.norm(ends[0] - ends[1]) <= 1e-6:
            continue

        incident = np.any(faces == corner, axis=1)
        # The edges opposite the corner run counter-clockwise from its
        # outgoing boundary to its incoming boundary in a single open fan.
        links = {}
        for face in faces[incident]:
            at = list(face).index(corner)
            links[face[(at + 1) % 3]] = face[(at + 2) % 3]
        start = links.keys() - set(links.values())
        if len(start) != 1 or len(links) != np.count_nonzero(incident):
            continue
        chain = [start.pop()]
        while chain[-1] in links and len(chain) <= len(links):
            chain.append(links[chain[-1]])
        if len(chain) != len(links) + 1 or chain[-1] in links:
            continue
        out, incoming = on_rim[np.ix_(sides, [chain[0], chain[-1]])].T
        if np.count_nonzero(out) != 1 or not np.array_equal(incoming, ~out):
            continue

        # Keep both endpoints. Their small connecting edge closes the corner
        # even when the two rigid global placements disagree horizontally.
        endpoints = np.tile(attributes[corner], (2, 1))
        endpoints[:, :3] = ends[np.r_[np.flatnonzero(out), np.flatnonzero(incoming)]]
        joined = _corner_triangles(endpoints, attributes, chain)
        if joined is None:
            # A border knot can sit closer to the corner than the offset
            # between the rims. Grow the cavity around that boundary knot
            # only as far as needed to avoid a folded face.
            following = np.roll(faces, -1, axis=1)
            best = None
            for outgoing in (True, False):
                expanded, added = chain.copy(), []
                while best is None or len(added) + 1 < len(best[0]):
                    i = 0 if outgoing else len(expanded) - 2
                    a, b = expanded[i:i+2]
                    adjacent = np.flatnonzero(np.any((faces == b) & (following == a), axis=1) & ~incident)
                    if len(adjacent) != 1 or adjacent[0] in added:
                        break
                    face = faces[adjacent[0]]
                    extra = face[(face != a) & (face != b)]
                    if len(extra) != 1 or extra[0] in expanded:
                        break
                    expanded.insert(i+1, extra[0])
                    added.append(adjacent[0])
                    trial = _corner_triangles(endpoints, attributes, expanded)
                    if trial is not None:
                        best = added, expanded, trial
                        break
            if best is not None:
                added, chain, joined = best
                incident[added] = True
        if joined is None:
            continue
        indices = np.r_[len(attributes), chain, len(attributes) + 1]
        attributes = np.vstack((attributes, endpoints))
        on_rim = np.column_stack((on_rim, on_rim[:, chain[0]], on_rim[:, chain[-1]]))
        faces = np.vstack((faces[~incident], indices[joined]))

    used, indices = np.unique(faces, return_inverse=True)
    attributes = attributes[used]
    return replace(built, positions=attributes[:, :3], normals=attributes[:, 3:6],
                   uvs=attributes[:, 6:8], planar=attributes[:, 8:10], tris=indices.reshape(-1, 3))


def join_edges(built, edges):
    """Add the global knots before moving vertices, so both sides of a corner stay constrained."""
    edges = [list(edge) for edge in edges if len(edge[2][0]) >= 2]
    for i, j in combinations(range(len(edges)), 2):
        a, b = edges[i], edges[j]
        if np.array_equal(a[0], b[1]):
            a[2], b[2] = _trim_crossing_rims(a[2], b[2])
        elif np.array_equal(b[0], a[1]):
            b[2], a[2] = _trim_crossing_rims(b[2], a[2])

    profiles = []
    for start, stop, target, spacing in edges:
        split = _edge_profile(built, start, stop, target, spacing)
        if split is not None:
            built, profile = split
            profiles.append(profile)
    if not profiles:
        return built

    world = built.positions + built.pos
    strength, delta, slope, on_rim = map(np.asarray, zip(*(_correction(world, p) for p in profiles)))
    rest = 1.0 - strength
    if len(profiles) == 1:
        offset = strength[0, :, None] * delta[0]
        gradient = slope[0]
    else:
        # Vertices on just one rim follow it exactly. Keep a temporary mean
        # for split corners until the two independent endpoints are stitched.
        masks = 1.0 - np.eye(len(profiles))
        outside = np.prod(np.where(masks[:, :, None], rest[None, :, :], 1.0), axis=1)
        weights = strength * outside
        total = np.prod(rest, axis=0) + weights.sum(axis=0)
        corner = total <= 1.e-12
        weights[:, corner] = np.isclose(strength[:, corner], 1., rtol=0., atol=1e-10)
        total[corner] = weights[:, corner].sum(axis=0)
        offset = np.sum(weights[:, :, None] * delta, axis=0) / total[:, None]
        gradient = np.sum(outside[:, :, None] * slope, axis=0) / total[:, None]

    normals = built.normals.copy()
    normals[:, :2] -= normals[:, 2:3] * gradient
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
    joined = replace(built, positions=built.positions + offset, normals=normals)
    return _close_corners(joined, built.positions, on_rim, delta) if len(profiles) > 1 else joined
