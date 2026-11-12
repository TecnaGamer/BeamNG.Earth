"""Street intersections: curb returns (rounded corners), crosswalks and curb ramps (docs/ARCHITECTURE.md
"Raised sidewalks"; the user, 2026-10-11: "the sidewalks have sharp corners at the intersections and
don't join together seamlessly, also there are no crosswalks or ramps leading to the crosswalks").

Overture splits roads at their junctions, so a junction is a node shared by several road centrelines.
At a node the branches (each road leaving it) are sorted by angle; between two neighbouring branches
i (its left side) and j (its right side) whose roads both have sidewalks and which meet at an angle
within CORNER_ANGLES, the corner is:
    - a curb return: an arc of radius RADIUS tangent to both curb lines (curb line = the carriageway
      edge); the sidewalks are cut exactly at the tangent points (a cross-section square to their
      road), and a corner piece fills between: the curb along the arc, the slab out to the corner of
      the two sidewalks' outer edges;
    - the asphalt between the arc and the curb lines' corner (the old square corner) filled.
On each branch with sidewalks on both sides, a crosswalk (WCUSA crossing_white) crosses the road just
outside its corners, and both sidewalks get a curb ramp there (the curb down to the road, the slab
rising back over RAMP_RUN, a tactile pad on the slope).
Pure functions of the road centrelines in the tile frame; deterministic (sorted inputs).
"""
import numpy as np

import fast     # Rust kernels (earthfast)

RADIUS = 5.0                    # m, curb return radius when the class is not listed in RADII
# by the larger road's class (street-view study of Fells Point, 2026-10-11: city corners are tight)
RADII = {"residential": 3.0, "living_street": 3.0, "unclassified": 3.0, "service": 3.0,
         "tertiary": 4.5, "secondary": 6.0, "primary": 6.0, "trunk": 8.0}
STOP_LINE = {"m": "line_white", "tl": 6.4, "p": 2, "w": 0.45}   # 1 m before the crosswalk
STOP_GAP = 1.0
BULB_M = 6.0                    # m a curb extension runs along the street past the corner
TAPER_M = 3.0                   # m over which it returns to the parking curb
CORNER_ANGLES = (15.0, 150.0)   # degrees between two branches for a curb return (an acute one under ACUTE_DEG:
                                # its sidewalks met in a sharp wedge, the user's report #46, Fells Point 2026-10-17)
RADIUS_TRIES = (1.0, 0.5, 0.25)  # a corner's radius tried at these shares of it until it fits
ACUTE_DEG = 50.0                # under this the return's radius shrinks so it starts within ACUTE_T_M of the
ACUTE_T_M = 9.0                 # carriageways' corner (radius / tan(theta / 2) from it), at least ACUTE_R_MIN,
ACUTE_R_MIN = 0.8               # and the corner gets no curb extension
BRANCH_M = 10.0                 # m of a branch used for its direction
CROSSWALK = {"m": "crossing_white", "tl": 4.8, "p": 3, "w": 3.0}
NOSE_DEG = 60.0                 # a corner sharper than this: its slab runs on past where the sidewalks' outer edges
NOSE_GAP_M = 4.0                # meet until they are this far apart ...
NOSE_MAX_M = 12.0               # ... at most this far past that point
CW_CELL_M = 25.0                # m: the grid of crosswalk middles find() checks a new crosswalk against
CW_GAP = 0.5                    # m between a corner's tangent point and its crosswalk
NODE_TOL = 0.5                  # m: centreline vertices this close are one node
MERGE_M = 3.0                   # m: junction nodes this close are one junction
CONNECTOR_M = 0.0              # m: ... and nodes joined by a road shorter than this
CONNECTOR_WIDTHS = 1.0          # ... or by a connector no longer than this many times the widest road at its two
                                # nodes: an offset junction lying within the carriageway (Fells Point, 10 m between
                                # two nodes of 10.6 m streets, 2026-10-17)
# Corners without curbs (find(pavement=True), roadcorners.py; from PR #363 by cody196): a rural junction's asphalt
# rounded off at PAVE_RADIUS, PAVE_RADIUS_MAJOR where a major road meets; a driveway's or a lot's service road keeps
# the service class's small radius (RADII: a 12 m flare at every driveway along a highway is not what the aerials show)
PAVE_RADIUS = 6.0
PAVE_RADIUS_MAJOR = 12.0
PAVE_MAJOR = {"motorway", "trunk", "primary", "secondary"}


def _left(u):
    return np.array([-u[1], u[0]])


def _consts(curb_w):
    """The constants junctions_find takes, in its order."""
    return [RADIUS, CORNER_ANGLES[0], CORNER_ANGLES[1], ACUTE_DEG, ACUTE_T_M, ACUTE_R_MIN, BRANCH_M, BULB_M, TAPER_M,
            NOSE_DEG, NOSE_GAP_M, NOSE_MAX_M, CW_CELL_M, CW_GAP, STOP_GAP, NODE_TOL, MERGE_M, CONNECTOR_M,
            CONNECTOR_WIDTHS, PAVE_RADIUS, PAVE_RADIUS_MAJOR, RADII["service"], curb_w]


def find(roads, has_walk, walk_w, parking_m=0.0, has_parking=lambda spec: False, pavement=False,
         pavement_bounds=None, left_hand=False):
    """Corners, cuts and crosswalks of the junctions among `roads` [(fid, spec, pts (k, 3), lon)].
    has_walk(spec) -> bool; walk_w: sidewalk width. Returns
        corners: [dict(arc (n, 3) curb points, inward (n, 2) unit vectors toward the sidewalk,
                       outer [(x, y, z) ...] the slab's outer corner points, fill (m, 3) asphalt polygon)]
        cuts: {(fid, side): [(node xy, branch u, t)]}: that sidewalk starts t m from the node
        crosswalks: [dict(a (3,), b (3,), w)]: across a road, curb to curb
        ramps: {(fid, side): [(node xy, branch u, t0, t1)]}: curb ramp along that sidewalk
        line_cuts: {fid: [(node xy, branch u, t)]}: that road's markings start t m from the node
                   (beyond its crosswalk)
        internal: the fids of the roads inside one junction (both ends in it)
    pavement: the corners' pavement alone (roadcorners.py: junctions without curbs; has_walk says which roads get
    corners): returns of PAVE_RADIUS (PAVE_RADIUS_MAJOR where a PAVE_MAJOR class meets; a service road's RADII; the
    curb's where both have sidewalks), no crosswalks or stop lines, and every corner also has road_sides ((fid, side),
    (fid, side)): the two road edges it joins (side +1 left / -1 right of the geometry), and edge_cuts: their cuts.
    pavement_bounds: (x0, y0, x1, y1) for pavement callers: the junctions provably outside it are skipped (every road
    still groups and tests the others).

    The work is the Rust kernel's (earthfast junctions_find, native 214): this hands it the roads' points and each
    road's facts (its spec read once). How a junction is found and cornered:
    - junction nodes: centreline vertices of two roads or more within NODE_TOL; nodes within MERGE_M, or joined by a
      road no longer than CONNECTOR_WIDTHS x the wider road (not a ring), are one junction (a five-way cobbled one at
      Fells Point was several nodes a few metres apart, 2026-10-11); a road with both ends in one junction within
      3 MERGE_M is internal (no branch);
    - branches: each road leaving a junction's node (not leading back into it within 2 MERGE_M or its connector),
      its direction over BRANCH_M, sorted round the junction by where it is BRANCH_M out seen from its centre (by
      direction alone a merged junction's branches interleave wrongly);
    - between neighbouring branches whose roads both get corners (has_walk; a street record's side without a curbside
      walk none) meeting within CORNER_ANGLES: the curb return of radius RADII (by class; the pavement radii in
      pavement mode) tangent to both curb lines, tried at RADIUS_TRIES shares of it until it fits (its street bending
      on put it on its own carriageway: report #46); under ACUTE_DEG the radius shrinks so it starts within ACUTE_T_M of the
      carriageways' corner; the branches must go on 1 m past the cuts (a short connector cannot carry one); no corner
      whose slab would cross itself (a mapped footway's), whose asphalt fill would be a needle (> 20 m + MERGE_M out),
      or whose curb would lie on a carriageway (other roads at their width, its own at their driving width) or whose
      slab's outer corner would lie on another road; two nodes of a merged junction joined by a connector get one
      corner along the connector's edge (_across in the Python: an offset junction, Fells Point 2026-10-17);
    - a corner sharper than NOSE_DEG without a footway: its slab runs on past where the sidewalks' outer edges meet
      until they are NOSE_GAP_M apart (at most NOSE_MAX_M), unless that lies on a carriageway (the user, 2026-11-11:
      "Also check sharper corners");
    - curb extensions (has_parking): the corner's curb at the driving lanes' edge, BULB_M along the street, back to
      the parking curb over TAPER_M (off since GitHub #208: roads.build_tile passes no has_parking);
    - a branch with corners on both sides (not a ring's): a crosswalk CW_GAP past the farther corner (none ending on
      another road's carriageway or crossing another road, none within 2 m of an earlier one: a CW_CELL_M grid of
      them), a stop line STOP_GAP before it on the incoming half (left_hand: traffic drives on the left, the incoming
      half the branch's right as one looks out of the junction; zolaski333's PR #1134), curb ramps on both sidewalks
      (on a bulb: the corner piece's), the road's markings cut beyond it."""
    fast.require(261, "junctions find (left_hand)")
    from sidewalks import CURB_W
    n = len(roads)
    if n == 0:
        return [], {}, [], {}, {}, set()
    pts = [np.asarray(p, np.float64) for _f, _s, p, _l in roads]
    off = np.zeros(n + 1, np.int64)
    off[1:] = np.cumsum([len(p) for p in pts])
    allp = np.ascontiguousarray(np.concatenate([p[:, :3] if len(p) else np.zeros((0, 3)) for p in pts]), np.float64)
    fids = [r[0] for r in roads]
    rank = {f: i for i, f in enumerate(sorted(set(fids)))}
    width, rad, walk = np.empty(n), np.empty(n), np.empty(n)
    flags = np.zeros(n, np.uint8)
    fw_state = np.zeros((n, 2), np.uint8)
    fw_width = np.zeros((n, 2))
    fw_key = [None] * (2 * n)
    walks = [None] * (2 * n)
    per = {}                                    # id(spec) -> its facts (specs are shared between rows)
    if pavement:
        from roads import has_sidewalks
    for i, (_f, sp, _p, _l) in enumerate(roads):
        got = per.get(id(sp))
        if got is None:
            fl = (1 if has_walk(sp) else 0) | (2 if has_parking(sp) else 0) | \
                (4 if getattr(sp, "roundabout", False) else 0)
            if pavement:
                fl |= (8 if has_sidewalks(sp) else 0) | (16 if sp.cls == "service" else 0) | \
                    (32 if sp.cls in PAVE_MAJOR else 0)
            w = getattr(sp, "walk", None)
            sides = []
            street = None if pavement else getattr(sp, "street", None)
            if street is not None:
                for side in (1, -1):
                    f = street.side(side)
                    sides.append((1, 0.0, None, None) if f is None or f.setback else
                                 (2, float(f.width), (f.width, f.surface), f))
            got = per[id(sp)] = (float(sp.width), float(RADII.get(sp.cls, RADIUS)),
                                 float("nan") if w is None else float(w), fl, sides)
        width[i], rad[i], walk[i], flags[i], sides = got
        for s, (state, fw, key, f) in enumerate(sides):
            fw_state[i, s], fw_width[i, s], fw_key[2 * i + s], walks[2 * i + s] = state, fw, key, f
    keys = sorted({k for k in fw_key if k is not None})
    krank = {k: j for j, k in enumerate(keys)}
    fw_rank = np.array([-1 if k is None else krank[k] for k in fw_key], np.int64).reshape(n, 2)
    out = fast._ef.junctions_find(
        allp, off, fids, np.array([rank[f] for f in fids], np.int64), width, rad, walk, flags, fw_state, fw_width,
        fw_rank, walks, None if walk_w is None else float(walk_w), float(parking_m), bool(pavement),
        None if pavement_bounds is None else tuple(float(v) for v in pavement_bounds), _consts(float(CURB_W)),
        [float(v) for v in RADIUS_TRIES], float(CROSSWALK["w"]), float(STOP_LINE["w"]), bool(left_hand))
    return tuple(out)


def apply_cuts(line, cuts, near=40.0):
    """A sidewalk centreline (k, 3) with the parts before its junction cuts removed: points within
    `near` m of a node X along branch u with (p - X) . u < t go, and the line is closed at exactly t
    (a cross-section square to the road). Returns the remaining pieces [(pts, (start_cut, end_cut))]
    (whether each end is a junction cut). Pure."""
    pts = np.asarray(line, np.float64)
    if not cuts or len(pts) < 2:
        return [(pts, (False, False))]
    # densify to 0.5 m so a cut lands within the resolution, then exact ends
    seg = np.hypot(*np.diff(pts[:, :2], axis=0).T)
    s = np.concatenate(([0.0], np.cumsum(seg)))
    if s[-1] <= 0:
        return []
    t = np.linspace(0.0, s[-1], max(2, int(s[-1] / 0.5) + 1))
    d = np.stack([np.interp(t, s, pts[:, c]) for c in range(pts.shape[1])], axis=1)
    keep = np.ones(len(d), bool)
    alongs = []
    for X, u, tc in cuts:
        rel = d[:, :2] - X
        along = rel @ u
        lateral = np.abs(rel @ _left(u))
        inside = (along < tc) & (along > -1.0) & (lateral < near)
        keep &= ~inside
        alongs.append((along - tc, inside))

    def exact(k_out, k_in):
        """The point between a removed sample and a kept one where its cut's cross-section lies."""
        for f, inside in alongs:
            if inside[k_out] and not inside[k_in] and f[k_in] != f[k_out]:
                # (between the two samples: where the line leaves the cut by its side or behind the node rather than
                # across t, both f are of one sign, and the line ran on 3 km past its tile, 2026-10-28)
                w = min(1.0, max(0.0, f[k_in] / (f[k_in] - f[k_out])))
                return d[k_in] + w * (d[k_out] - d[k_in])
        return d[k_in]
    out, start = [], None
    for k in range(len(d) + 1):
        if k < len(d) and keep[k]:
            if start is None:
                start = k
        elif start is not None:
            run = d[start:k].copy()
            cut0 = start > 0
            cut1 = k < len(d)
            if cut0:
                run[0] = exact(start - 1, start)
            if cut1:
                run[-1] = exact(k, k - 1)
            if len(run) >= 2:
                out.append((run, (cut0, cut1)))
            start = None
    return out


def ramp_ranges(run, ramps):
    """Arc-length ranges [(s0, s1)] along a sidewalk run (k, >=2) of its curb ramps. Pure."""
    if not ramps:
        return []
    seg = np.hypot(*np.diff(run[:, :2], axis=0).T)
    s = np.concatenate(([0.0], np.cumsum(seg)))
    out = []
    for X, u, t0, t1 in ramps:
        along = (run[:, :2] - X) @ u
        lateral = np.abs((run[:, :2] - X) @ _left(u))
        m = (lateral < 40.0)
        if not m.any():
            continue
        a = np.interp(t0, along[m], s[m]) if np.all(np.diff(along[m]) > 0) else None
        b = np.interp(t1, along[m], s[m]) if np.all(np.diff(along[m]) > 0) else None
        if a is None and np.all(np.diff(along[m]) < 0):
            a = np.interp(t0, along[m][::-1], s[m][::-1])
            b = np.interp(t1, along[m][::-1], s[m][::-1])
        if a is None or b is None:
            continue
        lo, hi = min(a, b), max(a, b)
        if hi > 0 and lo < s[-1] and hi - lo > 0.5:
            out.append((max(lo, 0.0), min(hi, s[-1])))
    return out
