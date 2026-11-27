"""Step 4 of the 3D solve: the ground under every sample, read once and smoothed along its path.

    read(elev, samples) -> raw (N,)       the elevation at every sample, in one call (NaN filled along the path)
    smooth(samples, raw) -> ground (N,)   each path's running median over MEDIAN_M, then a Gaussian of SIGMA_M

The median takes out what a road is not laid on (a wall, a tree, a car on the DEM: narrower than MEDIAN_M / 2), the
Gaussian the DEM's steps; the solve then lays each path to it at its laws, the cost of each metre off it the laws step's.
The smoothing is the road plans' own (grading._ground_line: median 12 m, sigma 6 m), per path, its ends kept (so paths
meeting at a fork read one height there).

elev(xy (k, 2)) -> z (k,): planner_elev(planner) is the build's (the planning frame's DEM, the bridges it holds under
mapped decks taken out)."""
import numpy as np

import grading as _grading

MEDIAN_M = 12.0
SIGMA_M = 6.0


def planner_elev(planner):
    """elev(xy) of a road planner: its elevation (with the decks taken out) at frame points."""
    def elev(xy):
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        if not len(xy):
            return np.zeros(0)
        la, lo, _ = planner.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return np.asarray(planner.elev(np.asarray(la, np.float64), np.asarray(lo, np.float64)), np.float64).ravel()
    return elev


def read(elev, samples):
    """The elevation at every sample (one call of elev); a sample with none takes its path's next ones' (linear), a
    path with none 0."""
    raw = np.asarray(elev(samples.xy), np.float64).ravel().copy()
    if raw.shape != (len(samples),):
        raise ValueError("elev gave %s heights for %d samples" % (raw.shape, len(samples)))
    bad = ~np.isfinite(raw)
    if bad.any():
        for p in range(len(samples.h)):
            sl = samples.of(p)
            z, ok = raw[sl], ~bad[sl]
            if ok.all():
                continue
            raw[sl] = np.interp(np.arange(len(z)), np.flatnonzero(ok), z[ok]) if ok.any() else 0.0
    return raw


def smooth(samples, raw, median_m=MEDIAN_M, sigma_m=SIGMA_M):
    """Each path's raw ground, its running median over median_m, then a Gaussian of sigma_m (paths of fewer than 5
    samples as they are)."""
    out = np.asarray(raw, np.float64).copy()
    for p, h in enumerate(samples.h):
        sl = samples.of(p)
        if sl.stop - sl.start >= 5 and h > 0:
            out[sl] = _grading.smooth_profile(_grading.median_profile(out[sl], h, median_m), h, sigma_m)
    return out
