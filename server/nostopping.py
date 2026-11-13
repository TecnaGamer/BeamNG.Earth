"""Australia's continuous yellow no-stopping edge lines (a solid yellow line along the kerb: AS 1742.2 / MUTCD
Queensland Part 13, "No Stopping" by line marking) on the approach to a signalised intersection: the last LENGTH_M of
every kerbed arm that traffic comes in along, on the driver's left (left-hand traffic). Which junctions are signalised is
furniture._is_signalled's: a mapped traffic signal at it (no guess without map data).

OpenStreetMap's parking:lane / parking:condition tags (no_stopping) are not here: the lane-tag shards (railtags.
LANE_COLUMNS, tools/build_lane_tags.py) do not carry them, so the road facts would need a new shard build to have them.

roads.build_tile adds the lines to a road's marking rows (roads.extras' kind "edge", so they are cut at the crossing roads
and rounded corners like an edge line). Pure.
"""
import numpy as np

LENGTH_M = 30.0                 # m of arm before the junction's middle
MIN_ARM_M = 15.0                # a shorter road is a link inside the junction


def _piece(pts, at_start, length):
    """The part of pts (k, 3) within length m of its start (at_start) or its end, in its own order."""
    pts = np.asarray(pts, np.float64)
    seg = np.hypot(*np.diff(pts[:, :2], axis=0).T)
    cum = np.r_[0.0, np.cumsum(seg)]
    total = float(cum[-1])
    if total < 1e-6:
        return pts[:0]
    length = min(length, total * 0.5)
    if at_start:
        keep = cum < length
        end = np.array([np.interp(length, cum, pts[:, i]) for i in range(pts.shape[1])])
        return np.vstack([pts[keep], end])
    keep = cum > total - length
    begin = np.array([np.interp(total - length, cum, pts[:, i]) for i in range(pts.shape[1])])
    return np.vstack([begin, pts[keep]])


def approaches(roads, signals, kerbed, ring=frozenset()):
    """{fid: [(side, pts (k, 3))]} of the roads' arms that come into a signalised junction: side +1 / -1 the left of the
    road's geometry that is the left of the traffic coming in, pts the arm's last LENGTH_M in the geometry's order.
    roads: [(fid, spec, pts, lon)]; signals: (n, 2) mapped signal points (None / empty: none); kerbed(spec): whether
    its edge is a kerb (a road with an edge line of its own has the white one there); ring: ids of roundabout roads."""
    if signals is None or not len(signals):
        return {}
    import furniture
    lines = {fid: pts for fid, _, pts, _ in roads}
    out = {}
    for arms in furniture._arms(roads).values():
        if len(arms) < 3 or not furniture._is_signalled(arms, signals):
            continue
        for a in arms:
            fid, spec, _u, _p, start = a[:5]
            if (spec.kind != "paved" or spec.cls == "motorway" or spec.link or fid in ring or not kerbed(spec)
                    or furniture._outbound(a) or (len(a) > 5 and a[5] < MIN_ARM_M)):
                continue
            piece = _piece(lines[fid], bool(start), LENGTH_M)
            if len(piece) >= 2:
                # (traffic comes in towards the junction: against the geometry from its start, along it at its end)
                out.setdefault(fid, []).append((-1 if start else 1, piece))
    return out
