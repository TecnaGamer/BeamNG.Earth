"""Australia's T-junction hazard board (tools/build_au_tboard.py): the wide black-and-white chevron board across the far
side of a T on a rural road, chevrons pointing outward both ways, facing the driver on the road that ends at the T.

A T is found from the roads alone: the end of road A lies (within JOIN_M) on the interior of road B, or on the end of B where
another road continues B straight on (Overture splits a road at every junction, so a through road is often two features),
and A meets B at 60-120 degrees. Nothing else is guessed: not where A carries on past B (a crossroads: any other road
leaving the far side along A's line), not on a one-way A leaving the T, not where the board's spot is on another road.
Both roads must be sealed, rural (not urban, no footpaths) and of a numbered class or unclassified (warnsigns.CLASSES). The
board stands centred on A's axis, B's half width + FAR_M past A's end, facing back down A, so it is at the same spot
whichever tile asks and each tile keeps the boards whose point it owns (the same Forest rows as warnsigns.py, called from
blockworker._kantstolpe_rows for Australia). Buildings are not in that hook's data, so one is not checked for.
"""
import numpy as np

import motesfri
import warnsigns

COUNTRIES = warnsigns.COUNTRIES
CLASSES = warnsigns.CLASSES | {"unclassified"}
JOIN_M = 3.0                    # m: how near A's end must be to B's line
END_M = 5.0                     # m: nearer than this to B's end it is B's end, not its interior
DIR_M = 30.0                    # m: A's direction at its end is read over this much road (10 m read the flare of a turn lane: the board stood off to one side)
MIN_ROAD_M = 30.0               # m: a road shorter than this is not read for a T
MIN_DEG = 60.0                  # A meets B at 60-120 degrees
FAR_M = 3.0                     # m beyond B's edge the board's centre stands
HALF_M = 2.4                    # m: half the board's length
ROAD_CLEAR_M = 1.0              # m the board stands clear of another road's edge
BEYOND_M = (3.0, 40.0)          # m past the T along A's axis, and ...
BESIDE_M = 8.0                  # ... within this of it: a road there is A carrying on (a crossroads)
HEIGHT_M = 1.7                  # the model's height (tools/build_au_tboard.py)
COS_MIN = float(np.cos(np.radians(MIN_DEG)))      # (A meets B at 60-120 degrees: its |cos| against B's line is at most this)


def wanted(spec):
    """Whether a road of this spec takes part in a T: sealed, rural, a numbered class or unclassified."""
    return spec.kind == "paved" and not spec.urban and not spec.sides and spec.cls in CLASSES


def _cum(xy):
    return np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]


def _tangent_at_end(xy, first):
    """Unit direction of the road at its last point (first: its first, running outward) read over DIR_M, pointing out of it."""
    p = xy[::-1] if first else xy
    cum = _cum(p)
    back = np.array([np.interp(max(cum[-1] - DIR_M, 0.0), cum, p[:, 0]), np.interp(max(cum[-1] - DIR_M, 0.0), cum, p[:, 1])])
    d = p[-1] - back
    n = float(np.hypot(*d))
    return d / n if n > 1e-9 else None


def _nearest(q, xy):
    """(distance m, unit tangent there, m along xy, its length) of the point of polyline xy (k, 2) nearest q (2,)."""
    a, b = xy[:-1], xy[1:]
    ab = b - a
    seg = np.maximum((ab * ab).sum(1), 1e-12)
    t = np.clip(((q - a) * ab).sum(1) / seg, 0.0, 1.0)
    d = np.hypot(*(q - (a + t[:, None] * ab)).T)
    k = int(np.argmin(d))
    cum = _cum(xy)
    along = cum[k] + t[k] * float(np.sqrt(seg[k]))
    return float(d[k]), ab[k] / float(np.sqrt(seg[k])), along, float(cum[-1])


def _continuing(end, d_out, others):
    """[(id, spec)] of the roads of others [(id, spec, xy)] that leave the point end (2,) straight on from a road leaving it
    the way d_out (2,) does (their direction there within 20 degrees of opposite)."""
    out = []
    for fid, spec, xy in others:
        for first in (True, False):
            if float(np.hypot(*(xy[0 if first else -1] - end))) > JOIN_M:
                continue
            t = _tangent_at_end(xy, first)
            if t is not None and float(t @ d_out) < -0.94:
                out.append((fid, spec))
    return out


def _through(e, roads, skip):
    """(distance, unit tangent, width, ids) of the through road a road ending at e (2,) meets, or None: the nearest road of
    roads [(id, spec, xy)] not in skip within JOIN_M whose point there is interior (or the end of one that another road
    carries straight on): its tangent, the greater width and the ids of the roads it is made of."""
    best = None
    for fid, spec, xy in roads:
        if fid in skip:
            continue
        dist, t, along, length = _nearest(e, xy)
        if dist > JOIN_M or length < MIN_ROAD_M or (best is not None and dist >= best[0]):
            continue
        width, ids = spec.width, {fid}
        if along < END_M or along > length - END_M:             # (B's end: only where another road goes straight on)
            first = along < END_M
            out = _tangent_at_end(xy, first)
            more = [] if out is None else _continuing(xy[0 if first else -1], out,
                                                      [r for r in roads if r[0] not in skip and r[0] != fid])
            if not more:
                continue
            width = max([width] + [s.width for _, s in more])
            ids |= {f for f, _ in more}
        best = (dist, t, width, ids)
    return best


def boards(road, roads):
    """[(point (2,), unit travel direction (2,))] of the T boards for road (id, spec, xy (k, 2)) among roads [(id, spec, xy)]
    (every road near it, itself too). Pure."""
    fid, spec, xy = road
    if not wanted(spec) or len(xy) < 2 or float(_cum(xy)[-1]) < MIN_ROAD_M:
        return []
    ends = (True, False) if not spec.oneway else ((True,) if int(spec.dir) < 0 else (False,))
    out = []
    eligible = [r for r in roads if wanted(r[1]) and len(r[2]) >= 2]
    for first in ends:
        e = xy[0 if first else -1]
        d = _tangent_at_end(xy, first)         # (out of the road's end: the way the driver is heading)
        if d is None:
            continue
        hit = _through(e, eligible, {fid})
        if hit is None:
            continue
        _, t, width, ids = hit
        if abs(float(d @ t)) > COS_MIN + 1e-6:
            continue
        centre = e + d * (width / 2 + FAR_M)
        side = np.array([-d[1], d[0]])
        # a crossroads: another road leaving the far side along A's line; and the board's own spot on another road
        rel = [(r[2], r[1].width) for r in roads if r[0] != fid and r[0] not in ids]
        skip = False
        for p, _w in rel:
            for pt in np.vstack([p[:, :2], (p[:-1] + p[1:]) / 2]):
                v = pt - e
                along, beside = float(v @ d), abs(float(v[0] * d[1] - v[1] * d[0]))
                if BEYOND_M[0] < along < BEYOND_M[1] and beside < BESIDE_M:
                    skip = True
                    break
            if skip:
                break
        if skip:
            continue
        spots = np.array([centre + side * HALF_M * k for k in (-1.0, -0.5, 0.0, 0.5, 1.0)])
        if any(float(motesfri._distance(spots, p[:, :2]).min()) <= w / 2 + ROAD_CLEAR_M for p, w in rel
               if p.shape[0] >= 2):
            continue
        out.append((centre, d))
    return out


def tile_rows(frame, near_roads, box, elev, country):
    """Forest rows (guardrails.forest_rows' layout and GROUPS) of the T boards of road tile box: near_roads build_tile's
    roads_out, elev(lat, lon) the ground. [] outside Australia."""
    if (country or "").upper() not in COUNTRIES:
        return []
    x0, y0, x1, y1 = box
    roads = [(fid, spec, np.asarray(pts, np.float64)[:, :2]) for fid, spec, pts, *_ in near_roads if len(pts) >= 2]
    out = []
    for road in roads:
        for q, travel in boards(road, roads):
            if not (x0 <= q[0] < x1 and y0 <= q[1] < y1):
                continue
            la, lo, _ = frame.enu_to_geodetic(q[None, 0], q[None, 1], np.zeros(1))
            z = float(np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2]).ravel()[0])
            row = warnsigns._row("au_tboard", q, travel, z, HEIGHT_M)
            out.append(row)
    return out
