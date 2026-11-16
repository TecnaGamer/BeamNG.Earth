"""Experimental Norwegian reflector posts for rural roads only.

Replaces Norway's default country-marker rows in the existing regional marker
pipeline. Uses the upstream country policy, road geometry, already available
NVDB width profiles, and the tile's ground sampler; no network I/O or geometry
changes. Does not modify markers outside Norway.
"""
import math
import logging
import os

import numpy as np

LOG = logging.getLogger('earthd.norway_posts')
GROUP = 'eu_marker_no'
ORIGINAL_GROUP = 'eu_marker_de'
# BNE_NO_POST_MODE=legacy is a visual diagnostic that preserves original posts.
MODE = os.environ.get('BNE_NO_POST_MODE', 'custom').strip().lower()
MODEL_HEIGHT = 1.01
VERGE_M = 1.10
BURY_M = 0.10
END_CLEAR_M = 12.0
JUNCTION_CLEAR_M = 14.0
DECK_EXTRA_M = 2.0
MAX_SLOPE_DROP_M = 1.65
# Leave eligibility to delineatorprofiles.NO (rural paved primary/secondary,
# sufficiently wide, normally 80 km/h and not explicitly lit).


def _road_stations(line):
    xy = np.asarray(line, np.float64)
    if len(xy) < 2:
        return None
    distances = np.linalg.norm(np.diff(xy[:, :2], axis=0), axis=1)
    return np.r_[0., np.cumsum(distances)]


def _width_profile(line, spec, frame):
    """(arclength, width): one nonblocking batched NVDB lookup per eligible road.

    If no reliable record is cached, fall back to the unchanged OSM width.
    No downloads or network work are performed in this placement function.
    """
    original = float(spec.width)
    stations = _road_stations(line)
    if stations is None or stations[-1] < 1.:
        return np.array([0., 1.]), np.full(2, original)
    s = np.linspace(0., stations[-1], max(2, min(120, int(stations[-1] / 16.) + 2)))
    nominal = np.full(len(s), original)
    if frame is None or not hasattr(frame, 'enu_to_geodetic'):
        return s, nominal
    try:
        xyz = np.asarray(line, np.float64)
        x = np.interp(s, stations, xyz[:, 0])
        y = np.interp(s, stations, xyz[:, 1])
        lat, lon, _ = frame.enu_to_geodetic(x, y, np.zeros(len(x)))
        import nvdb_width_profile_v03
        widths = nvdb_width_profile_v03.at_points(np.asarray(lon, np.float64),
                                                  np.asarray(lat, np.float64), original)
        if widths is None:
            return s, nominal
        widths = np.asarray(widths, np.float64)
        if widths.shape != nominal.shape:
            return s, nominal
        valid = np.isfinite(widths) & (widths >= 2.5) & (widths <= 15.)
        return s, np.where(valid, widths, nominal)
    except (AttributeError, ImportError, TypeError, ValueError, FloatingPointError) as exc:
        LOG.debug('NVDB post width fallback: %s', exc)
        return s, nominal


def _ground_z(ground, x, y, base):
    if ground is None:
        return float(base)
    try:
        h = float(np.asarray(ground(np.asarray([x]), np.asarray([y]))).reshape(-1)[0])
    except (TypeError, ValueError, IndexError, AttributeError) as exc:
        LOG.debug('post ground sample fallback: %s', exc)
        return None
    if not math.isfinite(h) or abs(h - float(base)) > MAX_SLOPE_DROP_M:
        return None  # a steep cutting/bank: don't float, bury or place a post dangerously
    return h


def _nearest_polyline_distance(q, line):
    """Small geometry-only deck/junction exclusion; no native or optional modules."""
    p = np.asarray(line, np.float64)
    if p.ndim != 2 or len(p) < 2:
        return math.inf
    a = p[:-1, :2]
    d = p[1:, :2] - a
    l2 = np.einsum('ij,ij->i', d, d)
    t = np.clip(np.einsum('ij,ij->i', np.asarray(q[:2]) - a, d) / np.maximum(l2, 1e-9), 0., 1.)
    close = a + d * t[:, None]
    return float(np.linalg.norm(close - np.asarray(q[:2]), axis=1).min())


def _near_decks(q, decks):
    for deck in decks or ():
        try:
            line, half_width = deck
            if _nearest_polyline_distance(q, line) < float(np.max(half_width)) + DECK_EXTRA_M:
                return True
        except (ValueError, TypeError):
            continue
    return False


def _junctions(roads):
    """Junction points including minor roads: exclude posts at mouths and corners."""
    import furniture as f
    points = []
    for arms in f._arms(roads).values():
        if len(arms) >= 3 and len({str(a[0]) for a in arms}) >= 2:
            points.append(np.asarray(arms[0][3][:2], np.float64))
    return np.asarray(points, np.float64).reshape(-1, 2)


def _near_junction(q, junctions):
    if not len(junctions):
        return False
    d = junctions - np.asarray(q[:2], np.float64)
    return bool(np.any(np.einsum('ij,ij->i', d, d) < JUNCTION_CLEAR_M ** 2))


def replace(rows, roads, box, country_at, *, frame=None, ground=None, decks=(), lighting=None):
    """Replace *only* Norwegian generic markers with aligned lightweight posts.

    ``rows`` is regionalmarkers.place output; country_at gives country at a local
    ENU position. All other countries' existing markers and chevrons survive.
    """
    import furniture as f
    import delineators
    import delineatorprofiles as profiles
    import europeinfra
    import fast
    from geotiles import box_owns

    def is_no(row):
        return (row[0] == ORIGINAL_GROUP and
                country_at(float(row[1]), float(row[2]), float(row[3])) == 'NO')

    # Fast reject off Norway, without changing neighboring countries' posts.
    if frame is not None and hasattr(frame, 'lat0') and hasattr(frame, 'lon0'):
        if not (57.0 <= float(frame.lat0) <= 72.5 and
                3.0 <= float(frame.lon0) <= 33.0):
            return rows
    # Keep original upstream rows until corresponding valid replacements exist.
    # Never delete every post just because a height/width/clearance check failed.
    if MODE == 'legacy':
        return rows
    has_no = any((not getattr(spec, 'urban', False) and
                  getattr(spec, 'cls', None) in profiles.policy('NO').classes)
                 for _, spec, line, _ in roads if len(line) >= 2)
    if not has_no:
        return rows
    all_clearance = [(fid, float(spec.width) / 2, np.asarray(line, np.float64))
                     for fid, spec, line, _ in roads if len(line) >= 2]
    if not all_clearance:
        return rows
    others = fast.Others(all_clearance)
    junctions = _junctions(roads)
    lighting = lighting or {}
    created = []
    for fid, spec, line, _ in roads:
        if (len(line) < 2 or getattr(spec, 'urban', True) or getattr(spec, 'link', False) or
                getattr(spec, 'oneway', False) or getattr(spec, 'median', None) or
                getattr(spec, 'kind', None) != 'paved' or
                getattr(spec, 'cls', None) not in profiles.policy('NO').classes):
            continue
        # The upstream candidate generator chooses canonical direction before
        # stationing. Our width, ground and tangent lookup MUST use that same
        # direction, or reversed map features place posts on the wrong bend.
        line = np.asarray(line, np.float64)
        if tuple(line[-1, :2]) < tuple(line[0, :2]):
            line = line[::-1]
        candidates = [c for c in delineators.candidates(fid, spec, line, country_at, lighting.get(fid))
                      if c[2] == 'NO']
        if not candidates:
            continue
        arc = _road_stations(line)
        if arc is None or arc[-1] < 2 * END_CLEAR_M:
            continue
        ws, widths = _width_profile(line, spec, frame)
        for station, side, code, _old_q, _old_tangent in candidates:
            if station <= END_CLEAR_M or station >= arc[-1] - END_CLEAR_M:
                continue
            p, tangents = f._at(line, np.asarray([station], np.float64))
            p, tan = p[0], tangents[0]
            width = float(np.interp(station, ws, widths))
            if not math.isfinite(width) or width < profiles.policy('NO').min_width:
                continue
            n = np.array([-tan[1], tan[0]]) * side
            q = p[:2] + n * (width / 2. + VERGE_M)
            if (not box_owns(box, float(q[0]), float(q[1])) or
                    country_at(float(q[0]), float(q[1]), float(p[2])) != 'NO' or
                    _near_junction(q, junctions) or _near_decks(q, decks) or
                    # Use owner-aware clearance: the original OSM road half-width
                    # can be wider than the measured NVDB asphalt by >0.45 m.
                    # Checking against the old own-width deleted every post.
                    not others.clear(np.r_[q, p[2]], fid, .65) or
                    # Still exclude hairpins where a second segment of *this*
                    # road passes close to the candidate (own-fid is excluded above).
                    _nearest_polyline_distance(q, line) < width / 2. + .65):
                continue
            z = _ground_z(ground, float(q[0]), float(q[1]), float(p[2]))
            if z is None:
                continue
            # Mirror upstream delineators yaw, preserving traffic-facing direction.
            leading = side * (1 if code in europeinfra.LEFT_HAND else -1)
            yaw = math.degrees(math.atan2(-tan[0], tan[1])) + (180. if leading < 0 else 0.)
            created.append([GROUP, round(float(q[0]), 2), round(float(q[1]), 2),
                            round(z - BURY_M, 2), round(yaw % 360, 1),
                            1.0, MODEL_HEIGHT, 0.0])
    if not created:
        if os.environ.get('BNE_NO_POST_DIAG') == '1':
            LOG.warning('NO rural posts: no valid replacements; preserved %d legacy rows',
                        sum(is_no(r) for r in rows))
        return rows
    # Only remove original rows for which a successfully placed custom row
    # exists nearby. A failed custom candidate retains its visible original.
    cell_size = 5.0
    cells = {}
    for r in created:
        cell = (math.floor(r[1] / cell_size), math.floor(r[2] / cell_size))
        cells.setdefault(cell, []).append(r)
    kept = []
    for r in rows:
        if not is_no(r):
            kept.append(r)
            continue
        i, j = math.floor(r[1] / cell_size), math.floor(r[2] / cell_size)
        substituted = any((r[1] - q[1]) ** 2 + (r[2] - q[2]) ** 2 < 16.0
                          for ii in (-1, 0, 1) for jj in (-1, 0, 1)
                          for q in cells.get((i + ii, j + jj), ()))
        if not substituted:
            kept.append(r)
    if os.environ.get('BNE_NO_POST_DIAG') == '1':
        LOG.warning('NO rural posts: original=%d new=%d preserved=%d',
                    sum(is_no(r) for r in rows), len(created), sum(is_no(r) for r in kept))
    return kept + created
