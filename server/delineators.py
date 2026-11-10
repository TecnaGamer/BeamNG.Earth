"""Deterministic rural rows with per-country coverage and curve spacing.

Full features supply station origins; tile bounds only select output. A phase
chooses *positions*, never whether an entire road receives delineators. No
250 m feature-length threshold: fragmented country roads participate too.
At feature joins the phase restarts, so the nominal spacing is approximate
across joins. This avoids a tile's changing neighbours shifting whole rows.
"""
import math
import numpy as np
from geotiles import box_owns
import delineatorprofiles as profiles
import europeinfra


def curvature(line, stations):
    import furniture as f
    a, _ = f._at(line, stations - 15.)
    p, _ = f._at(line, stations)
    b, _ = f._at(line, stations + 15.)
    u, v = p[:, :2] - a[:, :2], b[:, :2] - p[:, :2]
    cross = u[:, 0]*v[:, 1] - u[:, 1]*v[:, 0]
    lengths = np.linalg.norm(u, axis=1)*np.linalg.norm(v, axis=1)*np.linalg.norm(u+v, axis=1)
    radii = np.full(len(stations), np.inf)
    ok = (np.abs(cross) > 1e-8) & (lengths > 1e-8)
    radii[ok] = lengths[ok] / (2*np.abs(cross[ok]))
    return radii, np.sign(cross)


def candidates(fid, spec, line, country_at, lit=None):
    """(station, side, country, position, tangent), without tile clipping."""
    import furniture as f
    line = np.asarray(line, dtype=float)
    # Canonical direction also keeps Iceland's single verge and phase stable
    # when the geometry of a feature is reversed.
    if tuple(line[-1, :2]) < tuple(line[0, :2]):
        line = line[::-1]
    length = f._arclen(line)[-1]
    if length < 1e-6:
        return []
    samples = np.r_[np.arange(0., length, 5.), length]
    points, tangents = f._at(line, samples)
    radii, turns = curvature(line, samples)
    normals = np.c_[-tangents[:, 1], tangents[:, 0]]
    phase = f._h01(fid, 'eu-delineator-position-v4')
    out = []
    for side in (-1, 1):
        shoulders = points[:, :2] + side*normals*(spec.width/2 + 1.1)
        codes = [country_at(float(q[0]), float(q[1]), float(p[2])) for q, p in zip(shoulders, points)]
        if not any(c in europeinfra.KITS and profiles.eligible(c, spec, lit) for c in codes):
            continue
        # Integral of 1/spacing: tighten the same row smoothly through a bend,
        # instead of laying a second, overlapping pair of J1 substitutes on it.
        gaps = np.array([profiles.spacing(c, spec, r) if c in europeinfra.KITS
                         and profiles.eligible(c, spec, lit) else 50.
                         for c, r in zip(codes, radii)])
        metric = np.r_[0., np.cumsum(np.diff(samples) * (1/gaps[:-1] + 1/gaps[1:]) / 2)]
        levels = np.arange(phase, metric[-1], 1.)
        stations = np.interp(levels, metric, samples)
        pp, dd = f._at(line, stations)
        rr, tt = curvature(line, stations)
        for s, p, d, radius, turn in zip(stations, pp, dd, rr, tt):
            q = p[:2] + side*np.array([-d[1], d[0]])*(spec.width/2+1.1)
            code = country_at(float(q[0]), float(q[1]), float(p[2]))
            if (code not in europeinfra.KITS or not profiles.eligible(code, spec, lit)
                    or not profiles.verge(code, spec, radius, turn, side)):
                continue
            out.append((float(s), side, code, np.r_[q, p[2]], d))
    return sorted(out, key=lambda r: (r[0], r[1]))


def place(roads, box, country_at, clearance_roads, lighting=None):
    import fast
    x0, y0, x1, y1 = box
    rows = []
    # Include the owning road: a post on a tight bend must not lie in another
    # segment of its own carriageway. Reuse the existing native clearance kernel.
    others = fast.Others(clearance_roads)
    for fid, spec, line, _ in roads:
        for _, side, code, q, tangent in candidates(fid, spec, line, country_at, (lighting or {}).get(fid)):
            if not box_owns(box, q[0], q[1]):
                continue
            if not others.clear(q, None, .65):
                continue
            leading = side*(1 if code in europeinfra.LEFT_HAND else -1)
            yaw = math.degrees(math.atan2(-tangent[0], tangent[1])) + (180 if leading < 0 else 0)
            rows.append(['eu_marker_'+europeinfra.KITS[code].marker,
                         round(float(q[0]), 2), round(float(q[1]), 2), round(float(q[2]), 2),
                         round(yaw % 360, 1), 1., 1.15, 0.])
    return rows
