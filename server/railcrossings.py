"""Walking crossings of the tracks at the rails' level (task 037, stage F): railstations.Link kind "level".

A boarded crossing along the mapped footway: a slab at the rail heads' level between and beside the rails (as the
concrete or rubber plates of a real crossing), its ends sloping down to the ground, a warning stripe along both edges.
The rails stay proud of it by CROSS_PROUD_M. Only the part of the way near the tracks is made (the first crossing - REACH_M
to the last + REACH_M); the same on every tile (the caller keeps the tile's own triangles). The lean collider has the slab.

    add(mesh, frame, elev, st, link, collide)
"""
import numpy as np

import busstationmesh as bm
import railbridges as rb

RAIL_TOP_M = 0.58           # the rail head over the bed (railstationmesh.RAIL_TOP_M)
CROSS_PROUD_M = 0.025       # the rails' heads over the slab
REACH_M = 2.2               # the slab runs this far past the outer rails
RAMP_M = 1.6                # and slopes down to the ground over this
WIDTH_M = 2.0
WIDTH_RANGE_M = (1.0, 5.0)
SPAN_MAX_M = 60.0           # a way crossing more than this of track in a run is not one crossing: left out
STRIPE_W_M = 0.15
STRIPE_IN_M = 0.05
STRIPE_LIFT_M = 0.006
THICK_M = 0.30
CONCRETE = bm.PLATFORM_TOP
STRIPE = ("line_white", 1.0)


def width_of(link):
    w = link.width
    return w if w and WIDTH_RANGE_M[0] <= w <= WIDTH_RANGE_M[1] else WIDTH_M


def _runs(arcs):
    """[(first arc, last arc)] the crossings of a way grouped where they lie within 2 * REACH_M + a track gap of each other."""
    runs, cur = [], [arcs[0], arcs[0]]
    for a in arcs[1:]:
        if a - cur[1] <= 2 * REACH_M + 6.0:
            cur[1] = a
        else:
            runs.append(tuple(cur))
            cur = [a, a]
    runs.append(tuple(cur))
    return runs


def add(mesh, frame, elev, st, link, collide=False):
    """The level crossings of the link (kind "level") into mesh."""
    path = np.asarray(link.xy, float)
    s = rb._arc(path)
    arcs = sorted(a for _k, a in link.crosses)
    if not arcs or len(path) < 2:
        return
    w = width_of(link)
    for a0, a1 in _runs(arcs):
        lo, hi = max(0.0, a0 - REACH_M - RAMP_M), min(float(s[-1]), a1 + REACH_M + RAMP_M)
        if hi - lo < 4.0 or (a1 - a0) > SPAN_MAX_M:
            continue
        ts = np.unique(np.concatenate((np.arange(lo, hi, 1.0), [hi])))
        line = np.c_[np.interp(ts, s, path[:, 0]), np.interp(ts, s, path[:, 1])]
        g = bm.ground(frame, elev, line)
        # the slab's level: the rail head - proud, from the first rail to the last; sloping to the ground past them
        z_c = float(np.median(bm.ground(frame, elev, np.c_[
            np.interp([a0, a1], s, path[:, 0]), np.interp([a0, a1], s, path[:, 1])]))) + RAIL_TOP_M - CROSS_PROUD_M
        d_in = np.minimum(np.abs(ts - a0), np.abs(ts - a1))
        inside = (ts >= a0 - REACH_M) & (ts <= a1 + REACH_M)
        up = np.clip(1.0 - np.maximum(0.0, np.where(ts < a0, a0 - REACH_M - ts, ts - a1 - REACH_M)) / RAMP_M, 0.0, 1.0)
        z = np.where(inside, z_c, g + (z_c - g) * up) if True else g
        z = np.maximum(z, g + 0.02)
        rb._strip(mesh, line, z, w, THICK_M, CONCRETE, bm.KERB)
        if not collide:
            t, n = rb._normals(line)
            m = len(line)
            for side in (1.0, -1.0):
                a = line + n * side * (w / 2.0 - STRIPE_IN_M - STRIPE_W_M)
                b = line + n * side * (w / 2.0 - STRIPE_IN_M)
                P = np.vstack((np.column_stack((a, z + STRIPE_LIFT_M)), np.column_stack((b, z + STRIPE_LIFT_M))))
                T = []
                for i in range(m - 1):
                    T += [[i, i + 1, m + i + 1], [i, m + i + 1, m + i]] if side > 0 else \
                        [[i, m + i + 1, i + 1], [i, m + i, m + i + 1]]
                sa = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(line, axis=0).T))))
                UV = np.vstack((np.c_[sa, np.zeros(m)], np.c_[sa, np.ones(m)]))
                mesh.add(STRIPE[0], P, UV, T)
