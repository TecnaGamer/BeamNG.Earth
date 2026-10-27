"""Shoulder rumble strips (the user, 2026-10-25: "rumble strips on shoulders, which you feel through the tyres"): low
raised bars across the shoulder just outside both edges of rural motorway and trunk carriageways, solid, so a tyre
drifting onto the shoulder drums over them. They go in the road tile's roadside mesh with the guide signs
(guidesigns.mesh, the 'guide' shape: solid near the car like the sidewalks).

Real US shoulder strips are milled grooves 12 in (0.3 m) apart, 16 in (0.4 m) wide, about half an inch (13 mm) deep;
the ground here is the terrain, so they are bars of that size on it instead. Each bar stands on the terrain block's
drawn ground (build_tile's ground, as the sidewalks: drape_walks), clear of the segment's ends (junctions, ramps).
Positions are a pure function of the road's geometry (fixed stations from its first vertex): every tile places the same
bars and keeps those in its box.

Where (the user, 2026-10-26: "some intersect roads weird and some are along the road but on the farthest outside edge
rather than beside the white line"): beside the edge line on the shoulder (roads.py's edge line: half the width less
its inset), or on the line itself where the paving outside it is too narrow for a bar (a trunk's edge strip: real
"rumble stripes"); never on another road's pavement (crossings, ramps' gores, junctions) and only where the road lies
on the ground (not on bridges or in cuttings, where the terrain is not the road's surface).
"""
import numpy as np

CLASSES = ("motorway", "trunk")
PERIOD_M = 0.30             # m between bars along the road
BAR_ALONG_M = 0.16          # a bar's length along the road ...
BAR_ACROSS_M = 0.40         # ... its width across ...
BAR_H_M = 0.013             # ... and height
EDGE_GAP_M = 0.10           # m from the edge line's outer side to the strip
OTHER_CLEAR_M = 0.5         # m kept clear of another road's pavement
ON_GROUND_M = 0.6           # the road's own height within this of the ground under the bar
END_CLEAR_M = 25.0          # m kept clear of a segment's ends (merges, exits, junctions)
MAX_BARS = 6000             # per tile (the shape's vertex limit, with the signs)


def offset(spec):
    """m from the centreline to the bars' centres: just outside the edge line when the paving beyond it holds a bar,
    else on the line. Pure."""
    import roads as roadsmod
    line = roadsmod.EDGE_LINE
    inset = roadsmod.motorway_shoulder(spec) if roadsmod.rural_motorway(spec) else line["inset"]
    edge = spec.width / 2 - inset
    room = inset - line["w"] / 2
    if room >= EDGE_GAP_M + BAR_ACROSS_M:
        return edge + line["w"] / 2 + EDGE_GAP_M + BAR_ACROSS_M / 2
    return edge


def _seg_dist(c, a, b):
    """Distances of points c (n, 2) to segment a-b."""
    d = b - a
    l2 = float(d @ d)
    t = np.clip(((c - a) @ d) / l2, 0.0, 1.0) if l2 > 0 else np.zeros(len(c))
    return np.hypot(*(c - (a + t[:, None] * d)).T)


def _clear_of(c, me, others):
    """A mask of the points c (n, 2) not on another road's pavement (others: [(fid, half width, xy (k, 2))])."""
    keep = np.ones(len(c), bool)
    lo, hi = c.min(axis=0), c.max(axis=0)
    for fid, hw, xy in others:
        if fid == me:
            continue
        r = hw + OTHER_CLEAR_M + BAR_ACROSS_M / 2
        if (xy[:, 0].max() < lo[0] - r or xy[:, 0].min() > hi[0] + r or xy[:, 1].max() < lo[1] - r
                or xy[:, 1].min() > hi[1] + r):
            continue
        for i in range(len(xy) - 1):
            a, b = xy[i], xy[i + 1]
            near = ((c[:, 0] > min(a[0], b[0]) - r) & (c[:, 0] < max(a[0], b[0]) + r)
                    & (c[:, 1] > min(a[1], b[1]) - r) & (c[:, 1] < max(a[1], b[1]) + r) & keep)
            if near.any():
                idx = np.nonzero(near)[0]
                keep[idx[_seg_dist(c[idx], a, b) < r]] = False
    return keep


def strips(roads, box, zfn):
    """The bars whose centres are in box (x0, y0, x1, y1): {"kind": "rumble", "c": (n, 3) centres on the ground,
    "d": (n, 2) unit directions along the road}, or None. roads: build_tile's (fid, spec, centreline (k, 3), lon);
    zfn(x (n,), y (n,)) -> ground z (n,)."""
    x0, y0, x1, y1 = box
    cs, ds, zs = [], [], []
    others = [(fid, spec.width / 2, np.asarray(line, np.float64)[:, :2]) for fid, spec, line, _ in roads
              if len(line) >= 2]
    for fid, spec, line, _ in sorted(roads, key=lambda r: str(r[0])):
        if spec.cls not in CLASSES or spec.urban or len(line) < 2:
            continue
        p3 = np.asarray(line, np.float64)
        p = p3[:, :2]
        seg = np.hypot(*np.diff(p, axis=0).T)
        arc = np.concatenate([[0.0], np.cumsum(seg)])
        L = float(arc[-1])
        if L < 2 * END_CLEAR_M + PERIOD_M:
            continue
        s = np.arange(END_CLEAR_M, L - END_CLEAR_M, PERIOD_M)
        k = np.clip(np.searchsorted(arc, s, side="right") - 1, 0, len(p) - 2)
        t = (s - arc[k]) / np.maximum(seg[k], 1e-9)
        q = p[k] + (p[k + 1] - p[k]) * t[:, None]
        d = (p[k + 1] - p[k]) / np.maximum(seg[k], 1e-9)[:, None]
        n = np.stack([-d[:, 1], d[:, 0]], axis=1)
        roadz = p3[k, 2] + (p3[k + 1, 2] - p3[k, 2]) * t if p3.shape[1] > 2 else None
        off = offset(spec)
        for side in (1.0, -1.0):
            c = q + n * (side * off)
            inside = (c[:, 0] >= x0) & (c[:, 0] < x1) & (c[:, 1] >= y0) & (c[:, 1] < y1)
            if not inside.any():
                continue
            c, dd = c[inside], d[inside]
            keep = _clear_of(c, fid, others)
            z = np.asarray(zfn(c[:, 0], c[:, 1]), np.float64)
            if roadz is not None:
                keep &= np.abs(roadz[inside] - z) <= ON_GROUND_M
            if keep.any():
                cs.append(c[keep])
                ds.append(dd[keep])
                zs.append(z[keep])
    if not cs:
        return None
    c = np.concatenate(cs)[:MAX_BARS]
    d = np.concatenate(ds)[:MAX_BARS]
    z = np.concatenate(zs)[:MAX_BARS]
    return {"kind": "rumble", "c": np.column_stack([c, z]), "d": d}
