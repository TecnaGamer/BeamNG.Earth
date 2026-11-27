"""Join a clipped local mesh to the rendered edge of its global neighbour."""

from dataclasses import replace

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
    if len(parameter) < 2:
        return np.empty(0), np.empty((0, 3))
    rotation, shift, _ = geotiles.placement(geotiles.frame(x, y, z), anchor, built.pos)
    p = built.positions[ordered[first]] + built.pos
    world = p[:, :1] * rotation[:, 0] + p[:, 1:2] * rotation[:, 1] + p[:, 2:] * rotation[:, 2] + shift
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



def join_edges(built, edges):
    """Fit the local rim to the loaded global profile before moving the mesh into the scene."""
    segments, points, offsets = [], [], [0]
    for start, stop, (fraction, rim), spacing in edges:
        if len(fraction) >= 2:
            segments.append((*start, *stop, spacing))
            points.append(np.column_stack((fraction, rim)))
            offsets.append(offsets[-1] + len(fraction))
    if not segments:
        return built
    fast.require(642, "far_handoff_stitch")
    attributes = np.column_stack((built.positions, built.normals, built.uvs, built.planar))
    joined = fast._ef.far_handoff_stitch(
        np.ascontiguousarray(attributes, np.float64), np.ascontiguousarray(built.tris, np.int64),
        tuple(built.pos), np.asarray(segments, np.float64), np.concatenate(points),
        np.asarray(offsets, np.int64))
    if joined is None:
        return built
    attrs, faces = joined
    return replace(built, positions=attrs[:, :3], normals=attrs[:, 3:6], uvs=attrs[:, 6:8],
                   planar=attrs[:, 8:10], tris=faces)
