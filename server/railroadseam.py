"""A track laid beside a road, without a step between them (plan 028, 2026-11-08: prospekt Heroiv in Nemiia, a track whose
bed stood 2.1 m under the road 3 m past its edge, 1.2 m under the ground beside it: a vertical cut between the two).

The bed line (railways.bed_heights) is the smoothed ground under the track, laid to the DEM alone, so beside a road it
knows nothing of the road's height. The grader lays the ground to every plan's flat band and slopes it between two bands at
SLOPE_RUN : 1; where the room between a road's band and the bed's is less than that asks for, the ground stood as a wall.

couple() takes the bed to where the room is enough: per vertex, every road within REACH_M gives a band of heights,
its own +- (room / SEAM_SLOPE + SEAM_TOL_M), the room being the distance between the road's band and the bed's; the bed is
moved into the strictest. The moves are then spread along the track at the track's own grade, so no step is made
further on, and more where the road runs on beside it. A track crossing a road at grade has no room at all there: its bed
is the road's, which is the flush crossing (railways._flush) already, kept. Pure given its inputs."""
import math

import numpy as np

SEAM_SLOPE = 3.0        # m of room a metre of height difference needs between two flat bands (the grader's SLOPE_RUN is 2)
SEAM_TOL_M = 0.10       # m of difference allowed with no room at all
REACH_M = 40.0          # m from the bed line a road is looked at
MAX_MOVE_M = 6.0        # m the bed is moved at most (bad data: rather a step than a mountain)
SPREAD_GRADE = 0.02     # the track's own grade, at which a move is run out along it (railways.GRADE)


def _foot(p, a, ab, ab2):
    t = np.clip(((p - a) * ab).sum(1) / ab2, 0.0, 1.0)
    f = a + t[:, None] * ab
    d = np.hypot(*(p - f).T)
    k = int(np.argmin(d))
    return float(d[k]), k, float(t[k])


def bounds(xy, z, half, roads):
    """(lo, hi) per vertex: the heights the bed may have beside the roads (inf where none is near).
    xy (n, 2), z (n,), half: the bed's flat band's half width; roads: [(xy (k, 2), z (k,), half width)]: the roads' bands."""
    n = len(xy)
    lo = np.full(n, -np.inf)
    hi = np.full(n, np.inf)
    for rxy, rz, rh in roads:
        rxy = np.asarray(rxy, np.float64)[:, :2]
        rz = np.asarray(rz, np.float64)
        if len(rxy) < 2:
            continue
        if (rxy.max(0) < xy.min(0) - REACH_M).any() or (rxy.min(0) > xy.max(0) + REACH_M).any():
            continue
        a, b = rxy[:-1], rxy[1:]
        ab = b - a
        ab2 = np.maximum((ab * ab).sum(1), 1e-9)
        near = np.flatnonzero(((xy > rxy.min(0) - REACH_M) & (xy < rxy.max(0) + REACH_M)).all(1))
        for i in near:
            d, k, t = _foot(xy[i], a, ab, ab2)
            if d > REACH_M:
                continue
            zr = rz[k] + (rz[k + 1] - rz[k]) * t
            room = max(d - rh - half, 0.0)
            allow = room / SEAM_SLOPE + SEAM_TOL_M
            lo[i] = max(lo[i], zr - allow)
            hi[i] = min(hi[i], zr + allow)
    return lo, hi


def _tent(s, v, grade):
    """max over j of v_j - grade * |s - s_j|, v >= 0: each move run out at the track's grade."""
    out = np.array(v, np.float64)
    for k in range(1, len(s)):
        out[k] = max(out[k], out[k - 1] - grade * (s[k] - s[k - 1]))
    for k in range(len(s) - 2, -1, -1):
        out[k] = max(out[k], out[k + 1] - grade * (s[k + 1] - s[k]))
    return out


def couple(xy, z, half, roads):
    """The bed heights z (n,) of a track on xy (n, 2) taken to where the roads beside it leave room for the ground between
    (module doc); z itself where no road is near. half: the bed's flat band's half width; roads: as bounds()."""
    xy = np.asarray(xy, np.float64)[:, :2]
    z = np.asarray(z, np.float64)
    if len(z) < 2 or not roads:
        return z
    lo, hi = bounds(xy, z, half, roads)
    both = np.isfinite(lo) & np.isfinite(hi)
    if not both.any():
        return z
    mid = np.where(both, 0.5 * (np.where(both, lo, 0.0) + np.where(both, hi, 0.0)), z)
    target = np.where(both & (lo > hi), mid, np.clip(z, lo, hi))        # (a road on each side at other heights: between them)
    d = np.where(both, target - z, 0.0)
    d = np.clip(d, -MAX_MOVE_M, MAX_MOVE_M)
    if not np.abs(d).max() > 1e-6:
        return z
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    up = _tent(s, np.maximum(d, 0.0), SPREAD_GRADE)
    down = _tent(s, np.maximum(-d, 0.0), SPREAD_GRADE)
    return z + np.where(up >= down, up, -down)
