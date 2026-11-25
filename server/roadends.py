"""Mapped road ends and roadside pads (BNE_ROAD_ENDS=on, drivefeat.py): OpenStreetMap's highway=turning_circle /
turning_loop nodes at a road's dead end as a paved bulb, highway=passing_place as a lay-by beside a single-track road,
highway=mini_roundabout as its painted disc (roads.build_tile: mini_discs). The pads ride on the rounded corners'
machinery (roadcorners.py): their outlines are "fills" of pseudo-corners in its entry, so the ground paint, the photo's
asphalt, the near tiles' road mask and the trees' clearance take them as they take the corners'; grading.Grader holds
the ground under each at its road's planned height (grade). The outlines come from earthfast drivefeat.rs drive_pads:
a disc round a dead end's node (none on a through road), a lay-by with tapers on the side traffic keeps to.

Sizes: a mapped diameter=* (6-40 m); else a turning circle's paved radius 13 m in the Americas and Australia (US
cul-de-sacs: 40-50 ft local-street radii), 9 m elsewhere (European turning heads ~16-20 m across), at least the
road's half width + 3 m; a loop 3 m more. A passing place 2.5 m wide, 15 m long with 6 m tapers (Highland Council
single-track road guidance). A mini roundabout's disc: 1/4 of the narrower crossing road's width each side, 1-2 m
radius (TSRGD diagram 1003.4 domes 1-4 m across).
"""
import logging

import numpy as np
import dnp

import drivefeat
import drivetags

log = logging.getLogger("earthd.roadends")

NATIVE = 612
REACH_M = 4.0               # m from a road's end a turning circle node may stand (on its way: usually the end itself)
SEGS = 32                   # a disc's sides
BIG = ("US", "CA", "MX", "AU", "NZ", "BR", "AR", "CL")
R_BIG, R_SMALL, LOOP_EXTRA = 13.0, 9.0, 3.0
PASS_W = 2.5
KIND = {"turning_circle": 1, "turning_loop": 1, "passing_place": 2}


def _diameter(tags):
    try:
        d = float(str(tags.get("diameter", "")).split()[0].replace(",", "."))
    except (ValueError, IndexError):
        return None
    return d if 6.0 <= d <= 40.0 else None


def node_rows(nodes, half_of=None):
    """(m, 5) rows [x, y, kind, radius, side] of the drive nodes [(x, y, tags, id)] that make pads (KIND), in their
    order. half_of(x, y): the half width of the road there (None: not known, 3 m)."""
    import countrybounds
    import europeinfra
    import navgraph
    cb = countrybounds.bundled()
    out = []
    for x, y, tags, _id, la, lo in nodes:
        k = KIND.get(tags.get("highway"))
        if k is None:
            continue
        c = str(cb.at(la, lo) or "").upper()
        if k == 1:
            d = _diameter(tags)
            r = d / 2 if d else (R_BIG if c in BIG else R_SMALL)
            if tags.get("highway") == "turning_loop" and not d:
                r += LOOP_EXTRA
            out.append([x, y, 1.0, r, 0.0])
        else:
            left = c in navgraph.LEFT_HAND or c in europeinfra.LEFT_HAND
            out.append([x, y, 2.0, PASS_W, 1.0 if left else -1.0])
    return np.array(out, np.float64).reshape(-1, 5)


def _nodes_ll(lon0, lat0, lon1, lat1):
    t = drivetags.DEFAULT[0] if drivetags.DEFAULT else None
    if t is None:
        return None
    return t.nodes(lat0, lon0, lat1, lon1)


def corner_pads(rows, frame):
    """roadcorners' pseudo-corners [{"fills": [outline (k, 3)], "drive_pad": kind, "fid", "centre"}] of the road-end
    and passing-place nodes on rows [(fid, spec, pts (k, 3), lon)] (roadcorners._rows, in frame); [] when off, none or
    the shards cannot be had."""
    if not drivefeat.ENDS or not rows or frame is None:
        return []
    import fast
    fast.require(NATIVE, "drive_pads")
    xy = np.vstack([np.asarray(r[2], np.float64)[:, :2] for r in rows])
    x0, y0 = xy.min(axis=0) - 20.0
    x1, y1 = xy.max(axis=0) + 20.0
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), np.zeros(4))
    got = _nodes_ll(float(np.min(lo)), float(np.min(la)), float(np.max(lo)), float(np.max(la)))
    if not got:
        return []
    got = [g for g in got if g[3].get("highway") in KIND]
    if not got:
        return []
    ll = np.array([[g[1], g[2]] for g in got], np.float64)
    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
    nodes = [(float(a), float(b), g[3], g[0], float(g[2]), float(g[1])) for a, b, g in zip(np.asarray(e), np.asarray(n), got)]
    import roads
    paved = [r for r in rows if r[1].kind in roads.BUILT]
    if not paved:
        return []
    nr = node_rows(nodes)
    lines = [np.ascontiguousarray(np.asarray(r[2], np.float64)[:, :2]) for r in paved]
    half = [float(r[1].width) / 2 for r in paved]
    out = []
    for poly, li, c in fast._ef.drive_pads(lines, half, np.ascontiguousarray(nr), REACH_M, SEGS):
        p = np.asarray(poly, np.float64)
        kind = 1 if len(p) == SEGS else 2
        if kind == 1:                   # (a bulb no narrower than its own road + 3 m each side)
            r = float(np.max(dnp.hypot(*(p - np.asarray(c)).T)))
            want = half[li] + 3.0
            if r < want:
                p = np.asarray(c) + (p - np.asarray(c)) * (want / r)
        out.append({"fills": [np.c_[p, np.zeros(len(p))]], "drive_pad": kind, "fid": paved[li][0],
                    "centre": np.asarray(c, np.float64)})
    return out


def grade(pads, plans, frame=None):
    """[(fid, Plan)] holding the ground under each pad at its road's planned height (plans [(fid, Plan)] of the cell):
    a disc as one closed ring Plan of half its radius round its centre (the ring's core reaches the centre and the
    rim); a lay-by as a Plan along its middle. The height: the nearest planned point of its road."""
    import grading
    out = []
    by = {}
    for fid, p in plans:
        by.setdefault(fid, []).append(p)
    for k, pd in enumerate(pads):
        ps = by.get(pd["fid"])
        if not ps:
            continue
        c = pd["centre"]
        best = None
        for p in ps:
            xy = np.asarray(p.xy, np.float64)[:, :2]
            d = dnp.hypot(*(xy - c).T)
            i = int(np.argmin(d))
            if best is None or d[i] < best[0]:
                best = (float(d[i]), float(np.asarray(p.z)[i]))
        z = best[1]
        poly = np.asarray(pd["fills"][0], np.float64)[:, :2]
        if pd["drive_pad"] == 1:
            r = float(np.max(dnp.hypot(*(poly - c).T)))
            t = np.linspace(0.0, 2 * np.pi, 25)
            ring = np.c_[c[0] + 0.5 * r * dnp.cos(t), c[1] + 0.5 * r * dnp.sin(t)]
            q = grading.Plan(ring, np.full(len(ring), z), 0.5 * r + 0.3, road_half=0.0)
        else:
            u = poly[3] - poly[2] if len(poly) == 4 else poly[1] - poly[0]
            u = u / max(float(dnp.hypot(*u)), 1e-9)
            h = 0.5 * 15.0 + 3.0
            line = np.array([c - u * h, c + u * h], np.float64)
            q = grading.Plan(line, np.full(2, z), 0.5 * PASS_W + 0.3, road_half=0.0)
        fid = "drivepad:" + repr((pd["fid"], tuple(np.round(c, 1).tolist())))
        q.rid = grading.road_id(fid)
        out.append((fid, q))
    return out


def rows_of(parts, frame):
    """junctions-style rows [(fid, spec, pts (k, 3), lon)] of a grading cell's parts [(fid, spec, lon/lat ring)]."""
    got = [(fid, sp, np.asarray(r, np.float64)) for fid, sp, r in parts if len(r) >= 2]
    if not got:
        return []
    ll = np.concatenate([r for _f, _s, r in got])
    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
    out, k = [], 0
    for fid, sp, r in got:
        out.append((fid, sp, np.c_[np.asarray(e)[k:k + len(r)], np.asarray(n)[k:k + len(r)], np.zeros(len(r))],
                    float(np.mean(r[:, 0]))))
        k += len(r)
    out.sort(key=lambda row: (row[0], tuple(np.round(row[2][0, :2], 3))))
    return out
