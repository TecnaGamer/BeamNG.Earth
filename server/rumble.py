"""Shoulder rumble strips (the user, 2026-10-25: "rumble strips on shoulders, which you feel through the tyres"): low
raised bars across the shoulder just outside both edges of rural motorway and trunk carriageways, solid, so a tyre
drifting onto the shoulder drums over them. They go in the road tile's roadside mesh with the guide signs
(guidesigns.mesh, the 'guide' shape: solid near the car like the sidewalks).

Real US shoulder strips are milled grooves 12 in (0.3 m) apart, 16 in (0.4 m) wide, about half an inch (13 mm) deep;
the ground here is the terrain, so they are bars of that size on it instead. Each bar stands on the terrain block's
drawn ground (build_tile's ground, as the sidewalks: drape_walks), clear of the segment's ends (junctions, ramps).
Positions are a pure function of the road's geometry (fixed stations from its first vertex): every tile places the same
bars and keeps those in its box.
"""
import numpy as np

CLASSES = ("motorway", "trunk")
PERIOD_M = 0.30             # m between bars along the road
BAR_ALONG_M = 0.16          # a bar's length along the road ...
BAR_ACROSS_M = 0.40         # ... its width across ...
BAR_H_M = 0.013             # ... and height
EDGE_GAP_M = 0.15           # m from the carriageway's edge (the edge line) to the strip
END_CLEAR_M = 25.0          # m kept clear of a segment's ends (merges, exits, junctions)
MAX_BARS = 6000             # per tile (the shape's vertex limit, with the signs)


def strips(roads, box, zfn):
    """The bars whose centres are in box (x0, y0, x1, y1): {"kind": "rumble", "c": (n, 3) centres on the ground,
    "d": (n, 2) unit directions along the road}, or None. roads: build_tile's (fid, spec, centreline (k, 3), lon);
    zfn(x (n,), y (n,)) -> ground z (n,)."""
    x0, y0, x1, y1 = box
    cs, ds = [], []
    for fid, spec, line, _ in sorted(roads, key=lambda r: str(r[0])):
        if spec.cls not in CLASSES or spec.urban or len(line) < 2:
            continue
        p = np.asarray(line, np.float64)[:, :2]
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
        off = spec.width / 2 + EDGE_GAP_M + BAR_ACROSS_M / 2
        for side in (1.0, -1.0):
            c = q + n * (side * off)
            inside = (c[:, 0] >= x0) & (c[:, 0] < x1) & (c[:, 1] >= y0) & (c[:, 1] < y1)
            if inside.any():
                cs.append(c[inside])
                ds.append(d[inside])
    if not cs:
        return None
    c = np.concatenate(cs)[:MAX_BARS]
    d = np.concatenate(ds)[:MAX_BARS]
    z = np.asarray(zfn(c[:, 0], c[:, 1]), np.float64)
    return {"kind": "rumble", "c": np.column_stack([c, z]), "d": d}
