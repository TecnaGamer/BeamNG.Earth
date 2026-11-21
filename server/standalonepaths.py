"""Opt-in walking surfaces and scenery clearance."""
import os

import numpy as np

import pathlayout


def enabled():
    return os.environ.get("BNE_STANDALONE_PATHS", "off").lower() in {"1", "on", "true"}


def clear_props(rows, blockers):
    """Reject scenery overlapping a walking strip, including mapped and late-added props."""
    import furniture
    import pathfootprints
    if not rows or not blockers:
        return rows
    # These are freestanding ground objects. Signs, bridge fixtures, overhead
    # wires and building attachments can share their XY with a path on another
    # level and must not be removed by a 2D test.
    boxed = {furniture.G_HYDRANT, furniture.G_BENCH, furniture.G_BIN}
    candidates = {}
    for i, row in enumerate(rows):
        if row[0] in pathfootprints.CATALOG or row[0] in boxed or row[0] == furniture.G_STREET_TREE:
            candidates.setdefault(row[0], []).append(i)
    keep = np.ones(len(rows), bool)
    clearance = None
    for group, indices in candidates.items():
        if group in boxed:
            if clearance is None:
                clearance = furniture.RoadClearance([(half, line) for line, half in blockers])
            keep[indices] = [clearance.clear(rows[i], margin=0.0) for i in indices]
        else:
            radii = (0.0 if group == furniture.G_STREET_TREE else
                     pathfootprints.radii(group, [rows[i][6] for i in indices], 0.))
            keep[indices] = pathlayout.keep_outside([rows[i][1:3] for i in indices], blockers, radii)
    return [row for row, ok in zip(rows, keep) if ok]


def finish_props(rows, frame, box, cells, urban=None, widths=None, *, path_parts=None):
    """The final generation gate, after every source has contributed its scenery."""
    if not rows or not enabled():
        return rows
    if path_parts is None:
        path_parts = parts(frame, box, cells, urban, widths)
    blockers = [(xy, width / 2) for _, xy, width, _ in path_parts]
    return clear_props(rows, blockers)


def parts(frame, box, cells, urban=None, widths=None, quick=False):
    if not enabled():
        return []
    return pathlayout.parts(frame, box, cells, urban, widths, quick)
