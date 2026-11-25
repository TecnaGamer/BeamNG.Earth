"""Reconcile Norwegian tunnel approach heights with matched road surveys."""

import logging

import numpy as np

import norway_tunnels


LOG = logging.getLogger("earthd.norway_tunnel_grades")
APPROACH_REACH_M = 180.0
APPROACH_MATCH_M = 2.5
REPAIR_MIN_M = 3.0
REPAIR_MAX_M = 36.0
RECOVERY_M = 25.0
RECOVERY_ERROR_M = 1.5
MAX_SURVEY_GRADE = 0.18


def _survey(frame, xy, spec, level, name):
    """A continuous profile from the local NVDB road survey."""
    s = norway_tunnels.arc(xy)
    z = norway_tunnels._observed(frame, xy, spec, level, name)
    if z is None or z.shape != s.shape or np.min(np.diff(s)) < 0.05:
        return None
    known = np.isfinite(z)
    if not known[0] or known.sum() < 3 or known.mean() < 0.8:
        return None
    if np.max(np.diff(s[known])) > 16.0:
        return None
    last = int(np.flatnonzero(known)[-1]) + 1
    z = np.interp(s[:last], s[known], z[known])
    if np.max(abs(np.diff(z)) / np.diff(s[:last])) > MAX_SURVEY_GRADE:
        return None
    return z


def _reconcile(s, original, surveyed):
    """Follow the surveyed cutting to where both profiles agree for 25 m."""
    z = original[:len(surveyed)]
    s = s[:len(surveyed)]
    delta = surveyed - z
    if abs(delta[0]) < REPAIR_MIN_M or np.max(abs(delta)) > REPAIR_MAX_M:
        return None
    disagreement = np.flatnonzero(abs(delta) > RECOVERY_ERROR_M)
    if not len(disagreement):
        return None
    begin = disagreement[-1] + 1
    if begin == len(s) or s[-1] - s[begin] < RECOVERY_M:
        return None
    t = np.clip((s - s[begin]) / RECOVERY_M, 0.0, 1.0)
    revised = z + delta * (1.0 - t * t * (3.0 - 2.0 * t))
    changed = np.searchsorted(s, s[begin] + RECOVERY_M, side="left")
    if np.max(abs(np.diff(revised[:changed + 1])) / np.diff(s[:changed + 1])) > MAX_SURVEY_GRADE:
        return None
    return revised


def approach(frame, plan, spec, ends, name):
    """Regrade a road end only when both its ground road and bore match NVDB."""
    if not norway_tunnels.SMOOTH_APPROACH or not ends or len(plan.xy) < 5:
        return
    for side in (0, 1):
        if plan.abut[side]:
            continue
        sl = slice(None, None, -1 if side else 1)
        xy, z = plan.xy[sl], plan.z[sl]
        heading = norway_tunnels.unit(xy[0] - xy[1])
        matches = [(p, inward) for p, inward, _ in ends
                   if np.linalg.norm(xy[0] - p) < APPROACH_MATCH_M and np.dot(heading, inward) > 0.82]
        if len(matches) != 1:
            continue
        portal, inward = matches[0]
        s = norway_tunnels.arc(xy)
        count = int(np.searchsorted(s, APPROACH_REACH_M, side="right"))
        if count < 5 or s[count - 1] < RECOVERY_M + 10.0:
            continue
        surveyed = _survey(frame, xy[:count], spec, 0, name)
        if surveyed is None or not REPAIR_MIN_M <= abs(surveyed[0] - z[0]) <= REPAIR_MAX_M:
            continue
        probes = portal + np.array([0.0, 4.0, 8.0, 12.0])[:, None] * inward
        bore = _survey(frame, probes, spec, -1, name)
        if bore is None or abs(bore[0] - surveyed[0]) > 1.0:
            continue
        revised = _reconcile(s, z, surveyed)
        if revised is not None:
            lat, lon = norway_tunnels.location(frame, portal)
            LOG.info("NVDB tunnel approach matched at %.6f, %.6f (endpoint change %.2fm)",
                     lat, lon, float(revised[0] - z[0]))
            z[:len(revised)] = revised
