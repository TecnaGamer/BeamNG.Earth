"""Norwegian Roads v0.5.14: align the physical terrain grading core to NVDB width.

Works with the existing (v0.5.3+) measured-width profile. This only adjusts
Plan.half, Plan.road_half and the plan's spatial bbox. It does NOT change
centreline, vertical heights, road meshes, markings, or textures.

There are no network requests in this module. Unknown/ambiguous sections fall
back to BeamNG.Earth's existing grading. Works only with confident NVDB data.
"""
from __future__ import annotations
import logging
import math
import numpy as np

LOG = logging.getLogger("earthd.nvdb_terrain_v0514")
UNMATCHED_SENTINEL_M = 18.75  # outside the accepted NVDB width band
MIN_VALID_SHARE = 0.65
MAX_MEDIAN_RESIDUAL_M = 0.8
MAX_SAMPLE_RESIDUAL_M = 1.5
MIN_SAMPLES = 3
MAX_SAMPLES = 24
MAX_FLAT_SHOULDER_M = 0.50  # physically flattened strip beyond asphalt, per side
MIN_EDIT_M = 0.12


def _norway(lonlat):
    """Approximate bounding box only; confirmed road-width matches are required."""
    pts = np.asarray(lonlat, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[1] < 2 or len(pts) < 2:
        return False
    lon, lat = np.median(pts[:, 0]), np.median(pts[:, 1])
    return bool(np.isfinite([lon, lat]).all() and 57.5 <= lat <= 72.0 and 3.9 <= lon <= 32.5)


def measured_width(lonlat):
    """Return robust *constant* NVDB width only when measurements are reliable.

    For a strongly variable-width road, do not invent one width. The user's
    measured road profile / bridges remain untouched; this is grading only.
    """
    if not _norway(lonlat):
        return None
    pts = np.asarray(lonlat, dtype=np.float64)
    if len(pts) > MAX_SAMPLES:
        ix = np.unique(np.round(np.linspace(0, len(pts) - 1, MAX_SAMPLES)).astype(np.int64))
        pts = pts[ix]
    try:
        from nvdb_width_profile_v03 import at_points
        found = at_points(pts[:, 0], pts[:, 1], UNMATCHED_SENTINEL_M)
        if found is None:
            return None
        values = np.asarray(found, dtype=np.float64)
        if values.shape != (len(pts),):
            return None
        good = np.isfinite(values) & (values >= 2.5) & (values <= 15.0) & (np.abs(values - UNMATCHED_SENTINEL_M) > 0.25)
        if int(good.sum()) < max(MIN_SAMPLES, int(math.ceil(MIN_VALID_SHARE * len(pts)))):
            return None
        widths = values[good]
        center = float(np.median(widths))
        residual = np.abs(widths - center)
        if np.percentile(residual, 85) > MAX_MEDIAN_RESIDUAL_M:
            return None
        # Do not shrink terrain for a segment that crosses two different roads,
        # even if the majority of samples happens to match a nearby road.
        if np.count_nonzero(residual <= MAX_SAMPLE_RESIDUAL_M) < max(MIN_SAMPLES, int(math.ceil(0.9 * len(widths)))):
            return None
        return center
    except (ImportError, TypeError, ValueError, FloatingPointError, OverflowError) as exc:
        LOG.debug("NVDB terrain profile unavailable: %s", exc)
        return None


def grade_plan(plan, lonlat, spec, shoulder_m, transition_max):
    """Adjust physical grading, never asphalt width or terrain elevations.

    Returns True when a real grade-plan width was changed. The additional
    half-metre per side is a conservative flat verge, not the old wide core.
    """
    if plan is None or spec is None:
        return False
    if getattr(spec, "kind", None) not in ("paved", "cobble"):
        return False
    if getattr(spec, "urban", False) or getattr(spec, "median", None) or getattr(spec, "mountain", False):
        return False
    w = measured_width(lonlat)
    if w is None:
        return False
    if not (math.isfinite(float(shoulder_m)) and math.isfinite(float(transition_max))):
        return False
    xy = np.asarray(plan.xy, dtype=np.float64)
    if xy.ndim != 2 or xy.shape[1] < 2 or len(xy) < 2 or not np.isfinite(xy[:, :2]).all():
        return False
    flat_shoulder = min(MAX_FLAT_SHOULDER_M, max(0.0, float(shoulder_m)))
    half = w * 0.5 + flat_shoulder
    if not (0 < half <= 10.0):
        return False
    old_half = float(plan.half)
    old_road = float(plan.road_half)
    if not (math.isfinite(old_half) and math.isfinite(old_road)):
        return False
    # This patch repairs only *over-wide* grading. Never invent extra terrain.
    if half >= old_half - MIN_EDIT_M:
        return False
    if abs(old_half - half) <= MIN_EDIT_M and abs(old_road - w / 2) <= MIN_EDIT_M:
        return False
    plan.half = half
    plan.road_half = w * 0.5
    # Retain the native transition distance; do not force steep embankments.
    reach = half + max(0.0, float(transition_max))
    plan.bbox = (float(xy[:, 0].min() - reach), float(xy[:, 1].min() - reach),
                 float(xy[:, 0].max() + reach), float(xy[:, 1].max() + reach))
    LOG.debug("NVDB terrain v0.5.14 core %.2fm -> %.2fm (carriageway %.2fm)",
              old_half * 2, half * 2, w)
    return True
