"""Norway-only NVDB bridge deck/abutment fixes, ported from local v0.5.6-0.5.8.

Uses existing nonblocking NVDB width records. Does not change junction geometry,
road markings, NPCs, or the way world data is downloaded. Unknown widths leave
BeamNG.Earth's original bridge geometry in place.
"""
from __future__ import annotations

import logging
import math
import numpy as np

LOG = logging.getLogger("earthd.nvdb_bridge_fixes_v059")
SENTINEL_M = 18.75  # must fall outside supported NVDB measurements (2.5..15m)
MIN_COVERAGE = 0.75
MAX_BRIDGE_DEVIATION_M = 1.10
DECK_MARGIN_PER_SIDE_M = 0.45


class MatchedWidths(np.ndarray):
    """Marker for confident NVDB carriageway measurement (not deck shoulder)."""


def _tag(values):
    out = np.ascontiguousarray(values, dtype=np.float64).view(MatchedWidths)
    out._nvdb_matched = True
    return out


def _norway(lon, lat):
    return (lon.ndim == lat.ndim == 1 and lon.shape == lat.shape and len(lon) >= 2
            and np.isfinite(lon).all() and np.isfinite(lat).all()
            and 57.5 <= float(np.mean(lat)) <= 72.0
            and 3.9 <= float(np.mean(lon)) <= 32.5)


def _native_taper(width, tapers, lon, lat):
    """Original bridge-to-bridge transition, WITHOUT Norwegian road-width overlay.

    This must not call roads.taper_widths(), which now performs an independent
    NVDB lookup; using that result as a fallback reintroduced patchy flares.
    """
    if not tapers:
        return None
    out = np.full(lon.shape, float(width), dtype=np.float64)
    touched = False
    for tlon, tlat, target, span in tapers:
        if span <= 0:
            continue
        d = np.hypot((lon - tlon) * 111320.0 * math.cos(math.radians(tlat)),
                     (lat - tlat) * 110540.0)
        near = d < span
        if near.any():
            touched = True
            t = np.clip(d / span, 0.0, 1.0)
            out += np.where(near, (target - width) * (1 - t * t * (3 - 2 * t)), 0.0)
    return out if touched else None


def bridge_widths(lon, lat, original_width, native_tapers=()):
    """Confident NVDB carriageway widths on a simple Norwegian bridge.

    >=75% coverage, max 1.1m deviation from the median; never combine a
    partial NVDB measurement with the original independent bridge taper.
    Return the original bridge taper (or None) if uncertain or outside Norway.
    """
    lon, lat = np.asarray(lon, np.float64).reshape(-1), np.asarray(lat, np.float64).reshape(-1)
    if lon.shape != lat.shape or not np.isfinite(original_width) or not 2.5 <= original_width <= 28:
        return None
    old = _native_taper(original_width, native_tapers, lon, lat)
    if not _norway(lon, lat):
        return old
    try:
        from nvdb_width_profile_v03 import at_points
        measurements = at_points(lon, lat, SENTINEL_M)
        if measurements is None:
            return old
        a = np.asarray(measurements, np.float64)
        if a.shape != lon.shape:
            return old
        good = (np.isfinite(a) & (2.5 <= a) & (a <= 15.0)
                & (np.abs(a - SENTINEL_M) > 0.25))
        if good.mean() < MIN_COVERAGE:
            return old
        median = float(np.median(a[good]))
        if np.max(np.abs(a[good] - median)) > MAX_BRIDGE_DEVIATION_M:
            return old
        # Small consistent gradients can be real; missing samples are filled
        # from the common median rather than generating a sudden funnel.
        chosen = a if good.all() and len(a) >= 3 and np.ptp(a[good]) <= 0.5 else np.full_like(a, median)
        return _tag(np.round(chosen, 3))
    except (ImportError, ValueError, TypeError, AttributeError, FloatingPointError, OverflowError) as exc:
        LOG.debug('NVDB bridge profile fallback: %s', exc)
        return old


def structural_deck_widths(widths, chunk_points, bridge_points):
    """Add a constant 45cm *each side* to slab, collision and parapet width.

    Marking offsets continue to use the original measured carriageway widths.
    No centre hump/taper is added to the structure.
    """
    w = np.asarray(widths, np.float64)
    part = np.asarray(chunk_points, np.float64)
    whole = np.asarray(bridge_points, np.float64)
    if (w.ndim != 1 or len(w) < 2 or part.ndim != 2 or whole.ndim != 2
            or part.shape[0] != len(w) or len(whole) < 2
            or part.shape[1] < 2 or whole.shape[1] < 2
            or not np.isfinite(w).all() or not np.isfinite(part[:, :2]).all()
            or not np.isfinite(whole[:, :2]).all()):
        return widths
    return np.round(w + 2 * DECK_MARGIN_PER_SIDE_M, 3).tolist()


def wall_width_core(frame, points, spec, default_width, default_core, shoulder_m):
    """Keep Norwegian rural retaining-wall probes aligned with NVDB grading.

    Uses the same reliable-width estimator as nvdb_terrain_v0514.
    No changes on urban, mountain, median, or ambiguous roads.
    """
    if (spec is None or spec.kind not in ('paved', 'cobble') or spec.urban
            or spec.median or spec.mountain):
        return default_width, default_core
    if not (57.5 <= float(getattr(frame, 'lat0', -90)) <= 72
            and 3.9 <= float(getattr(frame, 'lon0', -180)) <= 32.5):
        return default_width, default_core
    try:
        pts = np.asarray(points, np.float64)
        if pts.ndim != 2 or pts.shape[1] < 2 or len(pts) < 2:
            return default_width, default_core
        if len(pts) > 24:
            pts = pts[np.unique(np.round(np.linspace(0, len(pts) - 1, 24)).astype(int))]
        lat, lon, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], np.zeros(len(pts)))
        import nvdb_terrain_v0514
        width = nvdb_terrain_v0514.measured_width(np.column_stack((lon, lat)))
        if width is None:
            return default_width, default_core
        # Terrain v0.5.14 repairs only excessively broad grading cores.
        candidate = width / 2 + min(max(0.0, float(shoulder_m)),
                                    nvdb_terrain_v0514.MAX_FLAT_SHOULDER_M)
        if candidate >= float(default_core) - nvdb_terrain_v0514.MIN_EDIT_M:
            return default_width, default_core
        return width, candidate
    except (ImportError, ValueError, TypeError, AttributeError, FloatingPointError, OverflowError) as exc:
        LOG.debug('NVDB retaining wall width fallback: %s', exc)
        return default_width, default_core


def clean_walls(frame, walls, decks):
    """Remove only short disconnected concrete nubs beside bridge abutments.

    Never remove true parapets, stone walls or long retaining walls.
    """
    if not (57.5 <= float(getattr(frame, 'lat0', -90)) <= 72
            and 3.9 <= float(getattr(frame, 'lon0', -180)) <= 32.5):
        return walls
    tips = []
    for deck in decks or ():
        xy = np.asarray(deck.get('n', []), np.float64)
        if deck.get('deck') and xy.ndim == 2 and xy.shape[1] >= 2 and len(xy) >= 2:
            tips.extend((xy[0, :2], xy[-1, :2]))
    if not tips:
        return walls
    ends = np.asarray(tips, np.float64)
    out = []
    for wall in walls:
        xy = np.asarray(wall.get('base', []), np.float64)
        if wall.get('parapet') or not wall.get('concrete') or xy.ndim != 2 or xy.shape[1] < 2 or len(xy) < 2:
            out.append(wall)
            continue
        length = float(np.linalg.norm(np.diff(xy[:, :2], axis=0), axis=1).sum())
        nearest = float(np.min(np.linalg.norm(xy[:, None, :2] - ends[None, :, :], axis=2)))
        if length >= 22.0 or nearest >= 10.0:
            out.append(wall)
    return out
