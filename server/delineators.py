"""Deterministic rural rows with per-country coverage and curve spacing.

Full features supply station origins; tile bounds only select output. A phase
chooses *positions*, never whether an entire road receives delineators. No
250 m feature-length threshold: fragmented country roads participate too.
At feature joins the phase restarts, so the nominal spacing is approximate
across joins. This avoids a tile's changing neighbours shifting whole rows.
"""
import math
import numpy as np
import dmath
import fast
from geotiles import box_owns
import delineatorprofiles as profiles
import europeinfra


def curvature(line, stations):
    import furniture as f
    a, _ = f._at(line, stations - 15.)
    p, _ = f._at(line, stations)
    b, _ = f._at(line, stations + 15.)
    fast.require(582, "delineator_curvature")
    return fast._ef.delineator_curvature(a, p, b)


def place(roads, box, country_at, clearance_roads, lighting=None):
    import furniture
    fast.require(582, "delineator_rows")
    _x0, _y0, _x1, _y1 = box  # Validate the four bounds while retaining the geotile owner.
    others = fast.Others(clearance_roads)
    rows = []
    for fid, spec, line, _ in roads:
        rows.extend(fast._ef.delineator_rows(
            np.ascontiguousarray(line, dtype=float), spec, (lighting or {}).get(fid),
            furniture._h01(fid, 'eu-delineator-position-v4'), country_at, profiles,
            europeinfra.KITS, europeinfra.LEFT_HAND, box, box_owns, others.clear))
    return rows


def motorway_verges(roads, box, country_at, guardrails, decks=()):
    """Italian motorway delineators on open outer verges only."""
    import euguardrails
    import fast
    import furniture as f

    if not any(spec.cls == 'motorway' and spec.oneway and spec.dir and not spec.link
               for _, spec, _, _ in roads):
        return []
    clearance = fast.Others([(fid, spec.width / 2, line) for fid, spec, line, _ in roads
                             if len(line) >= 2])
    deck_clearance = fast.Others([(i, half, line) for i, (line, half) in enumerate(decks)])
    barriers = euguardrails._rail_segments(guardrails)
    rows = []
    for fid, spec, line, _ in roads:
        if (spec.cls != 'motorway' or spec.kind != 'paved' or spec.urban or spec.link
                or not spec.oneway or not spec.dir or len(line) < 2):
            continue
        # Canonical station origin, with travel direction adjusted to match it.
        direction = spec.dir
        if tuple(line[-1, :2]) < tuple(line[0, :2]):
            line, direction = line[::-1], -direction
        length = f._arclen(line)[-1]
        stations = np.arange(f._h01(fid, 'it-motorway-verge') * 50., length, 50.)
        points, tangents = f._at(line, stations)
        for p, tangent in zip(points, tangents):
            travel = tangent[:2] * direction
            q = p.copy()
            q[:2] += np.array([travel[1], -travel[0]]) * (spec.width / 2 + 1.1)
            if (not box_owns(box, *q[:2]) or country_at(*q) != 'IT'
                    or not clearance.clear(q, None, .65) or not deck_clearance.clear(q, None, 2.)
                    or euguardrails._near_barrier(q[:2], barriers, reach=4.5)):
                continue
            yaw = math.degrees(dmath.atan2(-travel[0], travel[1])) % 360
            rows.append(['eu_marker_it_motorway', *map(float, q), yaw, 1., 1.15, 0.])
    return rows
