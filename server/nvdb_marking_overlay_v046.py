"""Norwegian road-marking overlay v0.5.9 (NVDB 99, source-first conflicts and stable matching).

Replaces v0.5.1's *overlay module only*; keeps BeamNG.Earth road geometry,
NVDB width profiles, and terrain handling. No network IO in game workers.

Known limitation: NVDB-derived LINESTRING geometry often follows the source
road network, NOT the exact paint position. Supports NVDB yellow single, double and mixed longitudinal lines.
Ambiguous spatial matches remain unpainted until confirmed.
"""
from functools import lru_cache
import logging
import math
import os
import time

import numpy as np

LOG = logging.getLogger('earthd.nvdb_markings_v059')
MAX_DIST_M = float(os.environ.get('BNE_NVDB_MARKING_MATCH_M', '2.4'))
COS_MIN = 0.94
AMBIGUOUS_M = 0.60
GRID_M = 24.0
STEP_M = 3.0
MAX_SAMPLES = 1600
INSET_M = 0.30
# Strict mode: with a cached NVDB 99 tile, only paint lines with a confident
# source match. Roads in areas without cached source retain the old fallback.
MODE = os.environ.get('BNE_NVDB_MARKINGS_MODE', 'strict').strip().lower()
SLOW_SEC = 0.5
# Source-first: honor explicit NVDB edges. Only fill a missing edge next to a
# matched yellow centre unless BNE_NVDB_EDGE_POLICY=source (strict).
# 'centre_solid' is an opt-in legacy behavior, not the default.
EDGE_POLICY = os.environ.get('BNE_NVDB_EDGE_POLICY', 'source_if_available').strip().lower()
MIN_RUN_CENTRE_M = 9.0
MIN_RUN_EDGE_M = 6.0
# Fill one dropped sample at most, not the intentional dashed gaps of a line.
MAX_BRIDGE_M = float(os.environ.get('BNE_NVDB_BRIDGE_GAP_M', '6.5'))
MAX_BRIDGE_M = max(0.0, min(7.0, MAX_BRIDGE_M))
# Only 1 anomalous sample may switch to a different NVDB record, and only
# when the flanking source record is spatially/chainage-continuous.
ISOLATED_SWITCH_M = 7.0
# Stronger record must beat a contradictory centre/edge claim by this amount.
CONFLICT_SCORE_DELTA_M = 0.45



def _classify(props):
    """Return role, standard marking style, paint width and NVDB ID.

    F short dash, V long warning dash, T town dash, S solid. XY means
    two parallel stripes (first is left relative to record direction).
    H/V edge sides are NVDB metering-direction relative, not guaranteed
    equivalent to an OpenStreetMap way orientation.
    """
    name = str(props.get('type') or '').casefold().strip()
    color = str(props.get('farge') or '').casefold().strip()
    side = str(props.get('sideposisjon') or '').upper().strip()
    try:
        width = float(props.get('bredde_enkeltlinje') or 0.10)
    except (TypeError, ValueError):
        width = 0.10
    width = float(np.clip(width, 0.08, 0.30))
    rid = str(props.get('nvdb_id', 'unknown'))

    if side in ('H', 'V') and color == 'hvit':
        if name.startswith('kantlinje, stiplet'):
            return side, 'dashed', width, rid
        if name.startswith('kantlinje, heltrukket'):
            return side, 'solid', width, rid
    if side == 'M' and color == 'gul':
        # Use explicit NVDB line codes, not vague text heuristics.
        # This covers 1000, 1002, 1004, and 1006.1-1006.5.
        import re
        code = re.search(r'\((?:1000|1002|1004|1006\.[1-5])[-–]([A-Za-z]{1,2})\)', name, re.I)
        if code:
            pair = code.group(1).upper()
            if pair in ('F', 'V', 'T', 'S'):
                return 'M', pair, width, rid
            if pair in ('FV','VF','FS','SF','VS','SV','SS','VV'):
                return 'M', 'pair_'+pair, width, rid
        # Some historical versions carry the type without a numeric suffix.
        if name.startswith('dobbel sperrelinje'): return 'M', 'pair_SS', width, rid
        if name.startswith('dobbel varsellinje'): return 'M', 'pair_VV', width, rid
        if name.startswith('sperrelinje'): return 'M', 'S', width, rid
        if name.startswith('varsellinje'): return 'M', 'V', width, rid
        if name.startswith('kjørefeltlinje'): return 'M', 'F', width, rid
        if name.startswith('tettstedlinje'): return 'M', 'T', width, rid
    return None


def _xy(lon, lat, ref):
    lo, la = ref
    scale_x = 111320.0 * math.cos(math.radians(la))
    a = np.asarray(lon, dtype=np.float64)
    b = np.asarray(lat, dtype=np.float64)
    return np.column_stack(((a - lo) * scale_x, (b - la) * 111320.0))


def _cells(a, b, pad):
    low = np.floor((np.minimum(a, b) - pad) / GRID_M).astype(int)
    high = np.floor((np.maximum(a, b) + pad) / GRID_M).astype(int)
    for gx in range(int(low[0]), int(high[0]) + 1):
        for gy in range(int(low[1]), int(high[1]) + 1):
            yield gx, gy


@lru_cache(maxsize=9)
def _indexed(sig):
    import nvdb_nationwide_v05 as region
    feats = region._combined('99', sig)
    valid = []
    for f in feats:
        p = f.get('properties') or {}
        desc = _classify(p)
        g = f.get('geometry') or {}
        if desc is None or g.get('type') != 'LineString':
            continue
        coords = np.asarray(g.get('coordinates', []), dtype=np.float64)
        if coords.ndim != 2 or coords.shape[0] < 2 or coords.shape[1] < 2:
            continue
        if not np.isfinite(coords[:, :2]).all():
            continue
        valid.append((coords[:, :2], desc))
    # Use an area-specific local projection, not Batnfjordsora's latitude for
    # every road nationwide. This matters for metre-scale matching in the north.
    if not valid:
        return None
    middle = valid[len(valid) // 2][0][len(valid[len(valid) // 2][0]) // 2]
    ref = float(middle[0]), float(middle[1])
    a_out, v_out, length2, dirs, styles, roles, widths, recs, chainages = [], [], [], [], [], [], [], [], []
    buckets = {}
    for coord, (role, style, width, rid) in valid:
        line = _xy(coord[:, 0], coord[:, 1], ref)
        chainage_m = 0.0
        for start, end in zip(line[:-1], line[1:]):
            v = end - start
            l2 = float(v @ v)
            if l2 < 0.01:
                continue
            i = len(a_out)
            a_out.append(start); v_out.append(v); length2.append(l2)
            length_m = math.sqrt(l2)
            dirs.append(v / length_m); styles.append(style)
            roles.append(role); widths.append(width); recs.append(rid); chainages.append(chainage_m)
            chainage_m += length_m
            for cell in _cells(start, end, MAX_DIST_M):
                buckets.setdefault(cell, []).append(i)
    if not a_out:
        return None
    LOG.info('NVDB v0.5.9: indexed %d supported features (%d segments)', len(valid), len(a_out))
    return (ref, np.asarray(a_out), np.asarray(v_out), np.asarray(length2),
            np.asarray(dirs), tuple(roles), tuple(styles), tuple(widths), tuple(recs),
            {k: np.asarray(v, dtype=np.int32) for k, v in buckets.items()},
            np.asarray(chainages, dtype=np.float64))


def _profile(points, lon, lat, width):
    xyz = np.asarray(points, dtype=np.float64)
    lon = np.asarray(lon, dtype=np.float64)
    lat = np.asarray(lat, dtype=np.float64)
    if xyz.ndim != 2 or len(xyz) < 2 or xyz.shape[1] < 2 or len(lon) != len(xyz) or len(lat) != len(xyz):
        return None
    lengths = np.linalg.norm(np.diff(xyz[:, :2], axis=0), axis=1)
    dist = np.r_[0.0, np.cumsum(lengths)]
    if dist[-1] < 1.0:
        return None
    sample = np.linspace(0.0, dist[-1], min(MAX_SAMPLES, max(2, math.ceil(dist[-1] / STEP_M) + 1)))
    line = np.column_stack([np.interp(sample, dist, xyz[:, col]) for col in range(xyz.shape[1])])
    lons = np.interp(sample, dist, lon)
    lats = np.interp(sample, dist, lat)
    if np.ndim(width) == 0:
        widths = np.full(len(xyz), float(width))
    else:
        widths = np.asarray(width, dtype=float)
    if len(widths) != len(xyz) or not np.isfinite(widths).all():
        return None
    wi = np.interp(sample, dist, widths)
    return line, sample, lons, lats, wi


def _reverse_style(style):
    # A two-stripe marking mirrored along a reversed road geometry swaps
    # the left and right patterns; a single stripe stays unchanged.
    return ('pair_' + style[5:][::-1]) if style.startswith('pair_') else style


def _lookup(lon, lat, indexed):
    """Choose nearest aligned per-role records; reject same-role contradictions.

    This is a geometry proxy, *not* an exact NVDB road-link association: the
    underlying BeamNG.Earth road features do not carry NVDB road-link IDs.
    Continuity filtering below rejects one-off matches near junctions.
    """
    ref, origins, vectors, lens2, dirs, roles, styles, widths, recs, grid, chainages = indexed
    pts = _xy(lon, lat, ref)
    direction = np.gradient(pts, axis=0)
    norm = np.linalg.norm(direction, axis=1)
    direction = np.divide(direction, norm[:, None], out=np.zeros_like(direction), where=norm[:, None] > 1e-8)
    result = {'M': [None] * len(pts), 'H': [None] * len(pts), 'V': [None] * len(pts)}
    for pi, p in enumerate(pts):
        cell = tuple(np.floor(p / GRID_M).astype(int))
        cand = grid.get(cell)
        if cand is None or not len(cand):
            continue
        dp = p - origins[cand]
        t = np.clip(np.einsum('ij,ij->i', dp, vectors[cand]) / lens2[cand], 0, 1)
        projected = origins[cand] + vectors[cand] * t[:, None]
        d2 = np.einsum('ij,ij->i', p - projected, p - projected)
        dot = dirs[cand] @ direction[pi]
        usable = (d2 <= MAX_DIST_M ** 2) & (np.abs(dot) >= COS_MIN)
        if not np.any(usable):
            continue
        # Align by distance + heading penalty; a nearly parallel side-road
        # segment is otherwise indistinguishable by centreline proximity.
        chosen = cand[usable]
        fractions = t[usable]
        distances = np.sqrt(d2[usable])
        alignments = np.abs(dot[usable])
        penalty = (1.0 - alignments) * 6.0
        order = np.argsort(distances + penalty)
        best = {}
        for j in order:
            i = int(chosen[j])
            role = roles[i]
            if dot[usable][j] < 0:
                role = {'H': 'V', 'V': 'H'}.get(role, role)
            score = float(distances[j] + penalty[j])
            style = _reverse_style(styles[i]) if dot[usable][j] < 0 and role == 'M' else styles[i]
            # NVDB chainage anchors dashed-line phase to the source geometry,
            # rather than restarting at every BeamNG.Earth road/tile boundary.
            val = (style, widths[i], recs[i], float(chainages[i] + fractions[j] * math.sqrt(lens2[i])),
                   score)
            byid = best.setdefault(role, {})
            old = byid.get(recs[i])
            if old is None or score < old[0]:
                byid[recs[i]] = (score, float(distances[j]), val)
        for role, choices in best.items():
            ranked = sorted(choices.values(), key=lambda row: row[0])
            winner = ranked[0]
            # Never choose a style when another record of the same role and
            # different style is almost equally plausible at this location.
            if len(ranked) > 1 and ranked[1][2][0] != winner[2][0]:
                if ranked[1][0] - winner[0] <= AMBIGUOUS_M:
                    continue
            result[role][pi] = winner[2]
    return result


def _coherent(values, sample, minimum_m):
    """Repair only a short one-point dropout *within the SAME NVDB record*.

    Do not fill intentional gaps between recorded features, change a marking
    type, or bridge multiple missing samples. The actual deliberate dashes are
    constructed later and are NEVER filled here.
    """
    out = list(values)
    for i in range(1, len(values) - 1):
        left, right = values[i - 1], values[i + 1]
        if values[i] is not None or left is None or right is None:
            continue
        if left[:3] != right[:3]:
            continue
        distance = float(sample[i + 1] - sample[i - 1])
        if distance > MAX_BRIDGE_M or distance <= 0:
            continue
        # A full NVDB geometry gap can connect two fragments with the same ID.
        # Only interpolate if recorded chainage evolves with this road segment.
        delta = abs(right[3] - left[3])
        if delta > max(2.0, distance * 1.55) or delta < distance * 0.4:
            continue
        fraction = (sample[i] - sample[i - 1]) / distance
        chainage = left[3] + fraction * (right[3] - left[3])
        out[i] = (left[0], left[1], left[2], chainage,
                  max(left[4], right[4]) if len(left) > 4 and len(right) > 4 else 0.0)
    start = 0
    while start < len(out):
        if out[start] is None:
            start += 1
            continue
        stop = start + 1
        while stop < len(out) and out[stop] is not None and out[stop][:3] == out[start][:3]:
            stop += 1
        if float(sample[stop - 1] - sample[start]) + 1e-5 < minimum_m:
            out[start:stop] = [None] * (stop - start)
        start = stop
    return out


def _drop_isolated_record_switches(values, sample):
    """Reject one off-route NVDB object taking over a single 3m sample.

    Unlike a majority filter, this never smears a legitimate longer marking
    transition; the surrounding identical source object must be chainage-
    consistent. A dropped sample can be restored by _coherent if safe.
    """
    out = list(values)
    for i in range(1, len(values) - 1):
        left, cur, right = values[i - 1:i + 2]
        if left is None or cur is None or right is None:
            continue
        span = float(sample[i + 1] - sample[i - 1])
        if span > ISOLATED_SWITCH_M or span <= 0:
            continue
        if left[:3] != right[:3] or left[:3] == cur[:3]:
            continue
        if not _connected(left, right, span):
            continue
        out[i] = None
    return out


def _score(value):
    """Metres + heading penalty from source matching; absent for legacy tests."""
    return float(value[4]) if value is not None and len(value) > 4 else float('inf')


def _edge_source_segments(found, sample):
    """Recorded edge segments; no guesses or centre-line overrides."""
    return {side: [a if _connected(a, b, float(sample[i + 1] - sample[i])) else None
                   for i, (a, b) in enumerate(zip(found[side][:-1], found[side][1:]))]
            for side in ('H', 'V')}


def _resolve_marking_conflicts(found, sample, widths):
    """Resolve overlapping NVDB records conservatively, *per segment*.

    A confidently matched dashed edge and yellow centre contradict the user's
    chosen Norwegian layout rule. Do not paint both. If the centre match is
    significantly closer/aligned, suppress only the dubious edge match;
    otherwise suppress the yellow. Never create paint to settle a conflict.
    Return (yellow segment mask, source edges, conflict count).
    """
    styles = [v[0] if v else '' for v in found['M']]
    yellow = np.asarray([(widths[i] >= 4.5 and widths[i + 1] >= 4.5 and
                          bool(styles[i]) and styles[i] == styles[i + 1] and
                          _connected(found['M'][i], found['M'][i + 1], float(sample[i+1]-sample[i])))
                         for i in range(len(sample)-1)], dtype=bool)
    edges = _edge_source_segments(found, sample)
    conflicts = 0
    for i in np.flatnonzero(yellow):
        dashed = [(side, val) for side in ('H', 'V')
                  if (val := edges[side][i]) is not None and val[0] == 'dashed']
        if not dashed:
            continue
        conflicts += 1
        centre_score = max(_score(found['M'][i]), _score(found['M'][i+1]))
        edge_score = min(_score(v) for _, v in dashed)
        if centre_score + CONFLICT_SCORE_DELTA_M < edge_score:
            # Better centreline evidence; edge is likely from a nearby road.
            for side, _ in dashed:
                edges[side][i] = None
        else:
            # Edge is better or ambiguous: no yellow unless we can justify it.
            yellow[i] = False
    return yellow, edges, conflicts


def _source_edge_policy(source_edge, yellow_segment):
    """Use source if available; only synthesize an absent solid edge by policy."""
    if EDGE_POLICY == 'centre_solid' and yellow_segment:
        return ('solid', 0.10, 'centre_solid_opt_in', 0.0)
    if source_edge is not None:
        return source_edge
    if yellow_segment and EDGE_POLICY == 'source_if_available':
        return ('solid', 0.10, 'missing_edge_fallback', 0.0)
    return None

def _runs(path, segments):
    # A boolean per *segment*, not per vertex; no accidental line generation
    # past the first or last sample of an NVDB record.
    out = []
    start = None
    for i, yes in enumerate(segments):
        if yes and start is None:
            start = i
        if start is not None and (not yes or i == len(segments) - 1):
            end = i + 1 if yes else i
            if end > start:
                out.append(np.asarray(path[start:end + 1]).copy())
            start = None
    return out


def _pattern_mask(s, style):
    """Legacy simple mask (retained for external test compatibility)."""
    pattern = {'V': (9.0, 3.0), 'F': (3.0, 9.0),
               'T': (3.0, 3.0), 'dashed': (3.0, 3.0)}.get(style)
    if pattern is None:
        return np.ones(len(s) - 1, bool)
    dash, gap = pattern
    return np.mod((s[:-1] + s[1:]) * 0.5, dash + gap) < dash


def _paint_runs(path, enabled, style, values, *, town_pattern=(3.0, 3.0)):
    """Render accurate solid/dashed line spans in NVDB's global record phase.

    Unlike a point-midpoint dash mask, cut at exact dash boundaries so a 3 m
    stripe cannot disappear merely because a road was sampled every 3-4 m.
    Keep the same source chainage across road-generation tile boundaries.
    """
    if style in ('solid', 'S'):
        return _runs(path, enabled)
    pattern = {'V': (9.0, 3.0), 'F': (3.0, 9.0),
               'T': town_pattern, 'dashed': (3.0, 3.0)}.get(style)
    if pattern is None:
        return _runs(path, enabled)
    dash, gap = pattern
    period = dash + gap
    out = []
    current = []

    def flush():
        nonlocal current
        if len(current) >= 2:
            out.append(np.asarray(current, dtype=np.float64))
        current = []

    for i, yes in enumerate(enabled):
        if not yes or values[i] is None or values[i + 1] is None:
            flush()
            continue
        p0, p1 = np.asarray(path[i]), np.asarray(path[i + 1])
        ph0, ph1 = values[i][3], values[i + 1][3]
        lo, hi = sorted((ph0, ph1))
        if hi - lo < 1e-7:
            flush()
            continue
        # Split this sampled road segment at all REAL marking dash boundaries.
        cuts = [0.0, 1.0]
        for n in range(math.floor(lo / period) - 1, math.ceil(hi / period) + 2):
            for boundary in (n * period, n * period + dash):
                if lo + 1e-7 < boundary < hi - 1e-7:
                    cuts.append((boundary - ph0) / (ph1 - ph0))
        cuts.sort()
        for a, b in zip(cuts[:-1], cuts[1:]):
            mid_phase = ph0 + (ph1 - ph0) * (a + b) * 0.5
            if mid_phase % period >= dash:
                flush()
                continue
            a_pt, b_pt = p0 + (p1 - p0) * a, p0 + (p1 - p0) * b
            if not current:
                current.extend((a_pt, b_pt))
            elif np.linalg.norm(current[-1][:2] - a_pt[:2]) < 0.015:
                current.append(b_pt)
            else:
                flush()
                current.extend((a_pt, b_pt))
    flush()
    return out

def _connected(a, b, spacing):
    if a is None or b is None or a[0] != b[0] or a[2] != b[2]:
        return False
    if len(a) >= 4 and len(b) >= 4:
        delta = abs(a[3] - b[3])
        # Same NVDB object can contain disconnected geometries. Never bridge
        # those as though they were one continuous length of physical paint.
        if delta > max(2.0, spacing * 1.65 + 0.8):
            return False
    return True


def _same_record_mask(values, sty, sample=None):
    return np.array([a is not None and a[0] == sty and
                     _connected(a, b, float(sample[i + 1] - sample[i]) if sample is not None else 3.0)
                     for i, (a, b) in enumerate(zip(values[:-1], values[1:]))], dtype=bool)


def _centre_styles(styles):
    return tuple(sorted(set(x for x in styles if x and
                            (x in ('F', 'V', 'T', 'S') or x.startswith('pair_')))))


def _tunnel_fallback(items, pts, sample, recorded):
    """Keep the generated marking over intervals with no source match."""
    stations = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(pts[:, :2], axis=0), axis=1))]
    output = []
    for look, width, line in items:
        kind = look.get('kind')
        role = 'M' if kind == 'centre' else {1: 'V', -1: 'H'}.get(look.get('side')) if kind == 'edge' else None
        if role not in recorded or not recorded[role].any():
            output.append((look, width, line))
            continue
        for run in _runs(sample[:, None], ~recorded[role]):
            start, end = run[0, 0], run[-1, 0]
            at = np.r_[start, stations[(stations > start) & (stations < end)], end]
            section = np.column_stack([np.interp(at, stations, line[:, dim]) for dim in range(line.shape[1])])
            output.append((look, width, section))
    return output


def adjust(items, pts, lon, lat, width_profile, spec, *, in_tunnel=False):
    """Return a source-only overlay in cached NVDB regions, without guesses.

    Preserve non-marking paint. In an uncached area and in uncovered portions
    of a tunnel, keep the generated markings. Performance is bounded by sampling.
    """
    t0 = time.perf_counter()
    if (getattr(spec, 'kind', None) != 'paved' or getattr(spec, 'oneway', False)
            or getattr(spec, 'cls', '') == 'motorway' or getattr(spec, 'median', None)):
        return items
    import nvdb_nationwide_v05 as region
    la, lo = float(np.mean(lat)), float(np.mean(lon))
    if not region.norway(la, lo) or len(pts) > 30000:
        return items
    # No live networking here. Missing tile keeps old rendering; prefetch first.
    cell = region.key(la, lo)
    if region.read_tile(cell, '99') is None:
        region.features(la, lo, '99')  # async queue, returns immediately
        return items
    prof = _profile(pts, lon, lat, width_profile)
    if prof is None:
        return items
    centre, s, llon, llat, wi = prof
    sig = region.signature(region.near_keys(la, lo), '99')
    indexed = _indexed(sig)
    raw = _lookup(llon, llat, indexed) if indexed is not None else {'M':[None]*len(s),'H':[None]*len(s),'V':[None]*len(s)}
    found = {role: _coherent(_drop_isolated_record_switches(vals, s), s,
                             MIN_RUN_CENTRE_M if role == 'M' else MIN_RUN_EDGE_M)
             for role, vals in raw.items()}
    if in_tunnel:
        # The mapped road may carry the approach's ordinary dashed centre
        # through the bore. Keep the tunnel warning line when it does.
        found['M'] = [v if v and 'F' not in v[0] else None for v in found['M']]
    source_present = any(any(v is not None for v in vals) for vals in found.values())
    if (in_tunnel or MODE == 'hybrid') and not source_present:
        return items
    # Remove generic guessed paint in cached areas, including phantom white
    # internal lane dashes on roads not explicitly mapped with 4+ lanes.
    mapped_lanes = getattr(spec, 'lanes', None)
    try:
        has_extra_lanes = mapped_lanes is not None and int(mapped_lanes) >= 4
    except (ValueError, TypeError):
        has_extra_lanes = False
    suppressed = {'centre', 'edge'} if has_extra_lanes else {'centre', 'edge', 'lane'}
    output = [(look, width, geom) for look, width, geom in items
              if look.get('kind') not in suppressed]
    if not source_present:
        return output

    # Physical width gate prevents central yellow paint on a one-lane road.
    # IMPORTANT: never infer the missing 1006 types from road width alone.
    centre_style = np.array([v[0] if v else '' for v in found['M']], dtype=object)
    yellow_segments, edge_sources, conflicting = _resolve_marking_conflicts(found, s, wi)
    town_pattern = (3.0, 1.0) if in_tunnel else (3.0, 3.0)
    if in_tunnel:
        # In Norway's one-lane layout the two dashed edge lines replace the
        # centre line. Preserve that layout where both edges are recorded.
        dashed_edges = np.array([a is not None and a[0] == 'dashed' and b is not None and b[0] == 'dashed'
                                 for a, b in zip(edge_sources['H'], edge_sources['V'])], dtype=bool)
        recorded = {side: np.asarray([v is not None for v in values], dtype=bool)
                    for side, values in edge_sources.items()}
        recorded['M'] = yellow_segments | dashed_edges
        output = _tunnel_fallback(items, np.asarray(pts, np.float64), s, recorded)
    if conflicting and os.environ.get('BNE_NVDB_MARKING_DEBUG') == '1':
        LOG.info('NVDB v0.5.9: %d conflicting yellow/dashed edge segments', conflicting)

    import roads as roadsmod
    # Road-edge lines are always placed using the *width profile*, not the
    # position of NVDB's (usually road-centreline-derived) geometries.
    for side, sign in (('V', 1), ('H', -1)):
        inset = roadsmod.edge_inset(spec, sign) if in_tunnel else INSET_M
        edge_path = roadsmod.offset_polyline(centre, sign * np.maximum(0.1, wi / 2.0 - inset))
        per_style = edge_sources[side] if in_tunnel else [
            _source_edge_policy(source, bool(yellow)) for source, yellow in zip(edge_sources[side], yellow_segments)]
        for sty in ('solid','dashed'):
            # Synthetic solid-only fallback segments need no source chainage.
            mask = np.array([v is not None and v[0] == sty for v in per_style], bool)
            if sty == 'solid':
                lines = _runs(edge_path, mask)
            else:
                lines = _paint_runs(edge_path, mask, sty, found[side])
            # Use measured NVDB paint width where the source provides it.
            active_widths = [float(v[1]) for v in per_style if v is not None and v[0] == sty]
            paint_width = round(float(np.clip(np.median(active_widths), .08, .25)), 3) if active_widths else .10
            for line in lines:
                output.append(({'m':'line_white','kind':'edge','side':sign,'tl':6.4,
                                'p':6,'nvdb_v055':sty,'nvdb_v059':True}, paint_width, line))

    # Paint all registered single and paired yellow styles, including:
    # 1000-F, 1002-V/T, 1004-S, 1006.1 FV/VF, 1006.2 FS/SF,
    # 1006.3 VS/SV, 1006.4 SS and 1006.5 VV.
    for style in _centre_styles(centre_style):
        mask = _same_record_mask(found['M'], style, s) & yellow_segments
        if not np.any(mask): continue
        paint_width = round(float(np.clip(np.median([v[1] for v in found['M'] if v and v[0] == style]), .08, .25)), 3)
        if style.startswith('pair_'):
            letters = style[5:]
            # First stripe is to the left of the NVDB geometry direction;
            # _lookup reverses the two letters if road direction is opposite.
            for letter, offset, part in ((letters[0], 0.14, 'L'),
                                         (letters[1], -0.14, 'R')):
                stripe_path = roadsmod.offset_polyline(centre, offset)
                for line in _paint_runs(stripe_path, mask, letter, found['M'], town_pattern=town_pattern):
                    output.append(({'m':'line_yellow','kind':'centre','tl':6.4,
                                    'p':6,'nvdb_v055':style,'nvdb_v059':True,'part':part},
                                   paint_width, line))
        else:
            for line in _paint_runs(centre, mask, style, found['M'], town_pattern=town_pattern):
                output.append(({'m':'line_yellow','kind':'centre','tl':6.4,
                                'p':6,'nvdb_v055':style,'nvdb_v059':True},
                               paint_width, line))

    dt = time.perf_counter() - t0
    if dt > SLOW_SEC:
        LOG.warning('NVDB v0.5.9 slow overlay: %.2f s; %d sample points, %d output items',
                    dt, len(s), len(output))
    return output

# NVDB_V052_SOURCE_FIRST

# NVDB_V054_COHERENT_MATCHING_AND_DOUBLE_LINES
# NVDB_V055_RECORD_ANCHORED_DASHES_AND_SHORT_GAP_REPAIR

# NVDB_V059_SOURCE_PRIORITY_CONFLICTS_AND_ISOLATED_RECORD_FIX
