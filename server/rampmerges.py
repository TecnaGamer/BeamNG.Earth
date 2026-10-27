"""Tapered entrance/exit lanes within the union of the mapped road pavements.

Plan on complete source lines, before tile clipping. A connected, same-direction
link is required; proximity alone must never turn a frontage road into a merge.
No extra pavement is invented outside the existing road/terrain footprint.
"""
import numpy as np

import roadmarkings
import roadlayoutcache

STEP = 1.0
REACH = 400.0
EXTEND = 30.0       # extra metres along the highway, bounded by available shoulder/other ramps
PAINT_CLEARANCE = .10
JOIN = 1.0
HEIGHT = .75
_cache = roadlayoutcache.LayoutCache(max_entries=12, max_bytes=24 * 1024 * 1024)


def sample(line, at):
    s = roadmarkings.arc(line)
    return np.column_stack([np.interp(np.atleast_1d(at), s, line[:, d]) for d in range(3)])


def project(line, points):
    """Station, nearest point, tangent, signed left offset, and distance in xy."""
    p = np.asarray(points)
    d = np.diff(line[:, :2], axis=0)
    length = np.linalg.norm(d, axis=1)
    length = np.maximum(length, 1e-9)
    t = ((p[:, None, :2] - line[None, :-1, :2]) * d).sum(2) / length ** 2
    tc = np.clip(t, 0., 1.)
    q = line[None, :-1, :2] + tc[:, :, None] * d
    dist = ((p[:, None, :2] - q) ** 2).sum(2)
    k = dist.argmin(1)
    j = np.arange(len(p))
    near = line[k] + tc[j, k, None] * np.diff(line, axis=0)[k]
    tangent = d[k] / length[k, None]
    # Extrapolate stations beyond the ends for trimming a longer source line.
    fraction = np.where(((k == 0) & (t[j, k] < 0)) | ((k == len(d)-1) & (t[j, k] > 1)), t[j, k], tc[j, k])
    station = roadmarkings.arc(line)[k] + fraction * length[k]
    normal = np.c_[-tangent[:, 1], tangent[:, 0]]
    offset = ((p[:, :2] - near[:, :2]) * normal).sum(1)
    return station, near, tangent, offset, np.sqrt(dist[j, k])


def section(line, a, b):
    s = roadmarkings.arc(line)
    return sample(line, np.r_[a, s[(s > a) & (s < b)], b])


def _chain(first, station, mains, entry, reach=REACH):
    """Follow the carriageway away from the join, across source segment ends."""
    fid, spec, line, _ = first
    oriented = lambda row: row[2][::(-1 if (row[1].dir or 1) * (-1 if entry else 1) < 0 else 1)]
    line = oriented(first)
    path = section(line, station, roadmarkings.arc(line)[-1])
    ids = {fid}
    while roadmarkings.arc(path)[-1] < reach:
        tangent = path[-1, :2] - path[-2, :2]
        if np.linalg.norm(tangent) < 1e-9:
            tangent = line[-1, :2] - line[-2, :2]
        tangent /= max(np.linalg.norm(tangent), 1e-9)
        choices = []
        for row in mains:
            if row[0] in ids or abs(row[1].width - spec.width) > .25:
                continue
            p = oriented(row)
            if np.linalg.norm(p[0, :2] - path[-1, :2]) > JOIN or abs(p[0, 2]-path[-1, 2]) > HEIGHT:
                continue
            direction = p[1, :2] - p[0, :2]
            score = direction @ tangent / max(np.linalg.norm(direction), 1e-9)
            if score > .9:
                choices.append((-score, row[0], p))
        if not choices:
            break
        _, next_id, p = min(choices, key=lambda v: v[:2])
        ids.add(next_id)
        path = np.vstack([path, p[1:]])
    return section(path, 0., min(reach, roadmarkings.arc(path)[-1])), ids


def _extend(plan, main, station, mains, neighbours):
    """Lengthen a taper onto existing shoulder, without taking a through lane.

    Preserve the exact ramp-edge joins. Only widen within the union of the two
    pavements, and stop before an adjacent ramp's existing merge footprint.
    """
    import roads
    back, ids = _chain(main, roadmarkings.arc(main[2])[-1] - station, mains,
                       not plan['entry'], reach=EXTEND)
    path = np.vstack([back[::-1][:-1], plan['path']])
    path = path[np.r_[True, np.linalg.norm(np.diff(path[:, :2], axis=0), axis=1) > 1e-6]]
    old_start, end = project(path, plan['guide'][[0, -1]])[0]
    start = max(0., old_start - EXTEND)
    ids = ids | plan['mains']
    for other in neighbours:
        if other is plan or not ids.intersection(other['mains']):
            continue
        points = np.vstack([other['guide'], other['outer']])
        s, near, _, lateral, _ = project(path, points)
        same_side = (lateral * plan['side'] > 0.) & (np.abs(points[:, 2] - near[:, 2]) <= HEIGHT)
        if not same_side.any():
            continue
        lo, hi = s[same_side].min(), s[same_side].max()
        if hi + 1. >= start and lo <= end:
            if hi + 1. >= old_start:
                return  # already tight: do not extend either lane into this merge
            start = max(start, hi + 1.)
    if old_start - start < 1.:
        return
    at = np.linspace(start, end, max(3, int(np.ceil(end-start)) + 1))
    centre = sample(path, at)
    _, _, tangent, _, _ = project(path, centre)
    normal = np.c_[-tangent[:, 1], tangent[:, 0]] * plan['side']
    edge = roads.offset_polyline(path, plan['side'] * plan['edge'])
    edge_at = project(path, edge)[0]
    guide = np.column_stack([np.interp(at, edge_at, edge[:, d]) for d in range(3)])
    # Keep the original curved guide in the mapped part of the merge.
    original_at = project(path, plan['guide'])[0]
    original = at >= old_start
    for d in range(3):
        guide[original, d] = np.interp(at[original], original_at, plan['guide'][:, d])
    guide[-1] = plan['guide'][-1]
    t = (at-start) / (end-start)
    span = plan['span'][-1] * t*t*(2-t)
    outer = guide.copy()
    outer[:, :2] += normal * span[:, None]
    outer += t[:, None]**3 * (plan['outer'][-1] - outer[-1])
    # A little extra inset keeps the straight chords between samples inside the
    # pavement at the concave corner where the two road buffers join.
    paint = roadmarkings.profile(plan['spec'].state).solid / 2 + .05

    def supported(points):
        _, q, _, _, dm = project(path, points)
        _, r, _, _, dr = project(plan['source'], points)
        return ((dm <= main[1].width/2 - paint) & (np.abs(points[:, 2]-q[:, 2]) <= HEIGHT)
                | (dr <= plan['spec'].width/2 - paint) & (np.abs(points[:, 2]-r[:, 2]) <= HEIGHT))

    if not supported(guide).all():
        return
    # Left shoulders in particular cannot hold a full ramp lane. Limit the
    # widening to their asphalt until the mapped ramp supplies the extra width.
    fits = supported(outer)
    delta = outer-guide
    scale = np.ones(len(at))
    limited = np.flatnonzero(~fits)
    if len(limited):
        low, high = np.zeros(len(limited)), np.ones(len(limited))
        for _ in range(16):
            mid = (low+high)/2
            ok = supported(guide[limited] + mid[:, None]*delta[limited])
            low = np.where(ok, mid, low)
            high = np.where(ok, high, mid)
        scale[limited] = low
    outer = guide + scale[:, None]*delta
    span *= scale
    if scale[-1] < 1. or np.any(np.diff(span) < -.05):
        return  # cannot lengthen cleanly: retain the original source-based taper
    plan.update(path=path, mains=ids, start=float(start), end=float(end),
                guide=guide, outer=outer, span=span, normal=normal)


def _pavement_split(main, station, mains, entry, away, side, width):
    """Ramp station where its inner asphalt edge leaves the mainline asphalt.

    This is later than the lane taper's end: both paved shoulders must clear.
    Use the full source neighbourhood so a tile edge cannot change the colour.
    """
    import roads
    length = roadmarkings.arc(away)[-1]
    path, _ = _chain(main, station, mains, entry, reach=length + width)
    inner = roads.offset_polyline(away, -side * width / 2)
    source_station = project(away, inner)[0]
    # Check in bounded batches, stopping at the first split; long ramps do not
    # need a dense projection of their entire length against the whole highway.
    previous = None
    for start in np.arange(0., length, REACH):
        at = np.linspace(start, min(start + REACH, length), int(min(REACH, length-start)/STEP)+2)
        points = np.column_stack([np.interp(at, source_station, inner[:, d]) for d in range(3)])
        s, near, _, lateral, _ = project(path, points)
        gap = lateral * side - main[1].width / 2
        valid = (s >= 0.) & (s <= roadmarkings.arc(path)[-1]) & (np.abs(points[:, 2]-near[:, 2]) <= HEIGHT)
        for a, g, ok in zip(at, gap, valid):
            if not ok:
                continue
            if g >= 0.:
                if previous is None:
                    return float(a)
                b, h = previous
                return float(b + (a-b) * -h / max(g-h, 1e-9))
            previous = (a, g)
    # No physical split in the available source geometry: keep the shared edge white.
    return float(length)


def plans(rows, terminals=None):
    """Plans for [(fid, Spec, xyz, longitude)], all at one road level.

    terminals optionally restricts links to their true source endpoints, avoiding
    artificial joins at the boundary between a ground part and a bridge part.
    """
    import roads
    rows = [(fid, sp, p[np.r_[True, np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1) > 1e-6]], lon)
            for fid, sp, p, lon in rows if len(p) >= 2]
    rows = [r for r in rows if len(r[2]) >= 2]
    mains = [r for r in rows if r[1].kind == 'paved' and r[1].oneway and not r[1].link and not r[1].roundabout
             and r[1].cls in ('motorway', 'trunk')]
    out, extensions = [], []
    for fid, sp, source, lon in sorted(rows, key=lambda r: r[0]):
        if not sp.link or not sp.oneway or sp.kind != 'paved':
            continue
        travel = source if sp.dir >= 0 else source[::-1]
        for entry, away in ((False, travel), (True, travel[::-1])):
            if terminals is not None and not any(np.linalg.norm(away[0, :2]-q[:2]) < .1 for q in terminals.get(fid, ())):
                continue
            length = roadmarkings.arc(away)[-1]
            if length < 12.:
                continue
            direction = sample(away, min(12., length))[0, :2] - away[0, :2]
            direction /= max(np.linalg.norm(direction), 1e-9)
            choices = []
            for row in mains:
                p = row[2][::(-1 if (row[1].dir or 1) * (-1 if entry else 1) < 0 else 1)]
                s, q, tangent, _, distance = project(p, away[:1])
                if distance[0] > JOIN or abs(q[0, 2]-away[0, 2]) > HEIGHT or tangent[0] @ direction < .75:
                    continue
                if roadmarkings.arc(p)[-1] - s[0] < 2.:
                    continue
                choices.append((distance[0], row[0], row, float(max(0., s[0]))))
            if not choices:
                continue
            _, _, main, station = min(choices, key=lambda v: v[:2])
            path, ids = _chain(main, station, mains, entry)
            at = np.linspace(0., min(length, REACH), int(min(length, REACH) / STEP) + 1)
            rp = sample(away, at)
            ms, near, tangent, signed, _ = project(path, rp)
            moving = np.flatnonzero(np.abs(signed) > .5)
            if not len(moving):
                continue
            side = 1 if signed[moving[0]] > 0 else -1
            main_orientation = (main[1].dir or 1) * (-1 if entry else 1)
            ramp_orientation = (sp.dir or 1) * (-1 if entry else 1)
            edge = main[1].width / 2 - roads.edge_margin(main[1], side * main_orientation)
            drive = sp.width - 2 * roads.edge_margin(sp)
            if drive <= 0:
                continue
            # Offset the complete ramp first: its surviving edge paint and the
            # taper must meet exactly, including on bends/asymmetric shoulders.
            outer_edge = roads.offset_polyline(away, side * (sp.width / 2 - roads.edge_margin(sp, side * ramp_orientation)))
            inner_edge = roads.offset_polyline(away, -side * (sp.width / 2 - roads.edge_margin(sp, -side * ramp_orientation)))
            def boundary(line, stations):
                station = project(away, line)[0]
                return np.column_stack([np.interp(stations, station, line[:, d]) for d in range(3)])
            inner_distance = project(path, boundary(inner_edge, at))[3] * side
            outer_distance = project(path, boundary(outer_edge, at))[3] * side
            separated = np.flatnonzero(inner_distance >= edge)
            if not len(separated):
                continue
            end = int(separated[0])
            distance = signed * side
            if end < 2 or np.any(np.diff(ms[:end+1]) <= 0.) or np.any(np.diff(distance[:end+1]) < -.15):
                continue
            if ms[end] > roadmarkings.arc(path)[-1] or np.max(np.abs(rp[:end+1, 2]-near[:end+1, 2])) > HEIGHT:
                continue
            # Exact full-width boundary and zero-width taper tip, independent of sampling/tile edges.
            far = float(np.interp(edge, inner_distance[:end+1], at[:end+1]))
            tip = float(np.interp(edge, outer_distance[:end+1], at[:end+1]))
            if far - tip < 4.:
                continue
            ra = np.linspace(tip, far, max(3, int(np.ceil(far-tip)) + 1))
            rp = sample(away, ra)
            ms, near, tangent, signed, _ = project(path, rp)
            normal = np.c_[-tangent[:, 1], tangent[:, 0]] * side
            raw_outer = boundary(outer_edge, ra)
            available = np.maximum(0., project(path, raw_outer)[3] * side - edge)
            t = np.clip((available - available[0]) / max(available[-1] - available[0], 1e-9), 0., 1.)
            # Zero slope at the merge tip, original ramp slope at the far end.
            span = drive * t * t * (2 - t)
            guide = near.copy()
            guide[:, :2] += normal * edge
            guide += t[:, None] ** 3 * (boundary(inner_edge, [far])[0] - guide[-1])
            outer = guide.copy()
            outer[:, :2] += normal * span[:, None]
            outer[:, 2] += t * (rp[:, 2] - near[:, 2])
            outer += t[:, None] ** 3 * (raw_outer[-1] - outer[-1])
            # Every stroke remains supported by existing asphalt, including on curves.
            paint = roadmarkings.profile(sp.state).solid / 2
            _, _, _, _, dm = project(path, outer)
            _, _, _, _, dr = project(away, outer)
            if np.any((dm > main[1].width / 2 - paint) & (dr > sp.width / 2 - paint)):
                continue
            guide_station = project(path, guide)[0]
            color_cut = max(far, _pavement_split(main, station, mains, entry, away, side, sp.width))
            out.append(dict(ramp=fid, mains=ids, entry=entry, source=away, cut=far, path=path,
                            color_cut=color_cut,
                            start=float(guide_station[0]), end=float(guide_station[-1]), edge=edge, side=side,
                            guide=guide, outer=outer, span=span, normal=normal, spec=sp,
                            # Left/right relative to actual travel, not the OSM vertex order.
                            color=roads.edge_colour(main[1], side * main_orientation)))
            extensions.append((main, station))
    for plan, (main, station) in zip(out, extensions):
        _extend(plan, main, station, mains, out)
    return out


def ground(frame, cells, urban=None, widths=None, left_hand=False):
    """Cached full source neighbourhood; tile bounds never enter the layout."""
    import roads
    import mapgeom
    def build():
        rows, seen, terminals = [], set(), {}
        for feats in cells:
            if feats is None:
                continue
            for i, sp in enumerate(roads.road_specs(feats, urban, widths, left_hand)):
                fid = str(feats.ids[i])
                if sp is None or fid in seen or sp.cls not in ('motorway', 'trunk', 'primary'):
                    continue
                seen.add(fid)
                for part in feats.parts(i):
                    for ring in part:
                        if len(ring):
                            e, n = mapgeom.enu_xy(feats, frame, ring[[0, -1]])
                            terminals.setdefault(fid, []).extend(np.c_[e, n, np.zeros(2)])
                for ring in roads.ground_parts(feats, i):
                    e, n = mapgeom.enu_xy(feats, frame, ring)
                    rows.append((fid, sp, np.c_[e, n, np.zeros(len(e))], 0.))
        return plans(rows, terminals)
    owner = getattr(urban, '__self__', None)
    return _cache.get(frame, cells, (urban, widths, bool(left_hand), getattr(owner, 'epoch', 0)), build)


def _outside(line, metric):
    """Keep metric > 0; interpolate the exact crossing between one-metre samples."""
    import roads
    p = roads.densify(line, STEP)
    v = metric(p)
    out, run = [], []
    for i in range(len(p)-1):
        if v[i] > 0:
            run.append(p[i])
        if (v[i] > 0) != (v[i+1] > 0):
            q = p[i] + v[i] / (v[i]-v[i+1]) * (p[i+1]-p[i])
            run.append(q)
            if v[i] > 0:
                if len(run) > 1:
                    out.append(np.asarray(run))
                run = []
    if v[-1] > 0:
        run.append(p[-1])
    if len(run) > 1:
        out.append(np.asarray(run))
    return out


def trim(fid, extras, layouts):
    """Remove obsolete ramp paint and only the mainline edge being replaced."""
    out = []
    for look, width, line in extras:
        pieces = [line]
        for plan in layouts:
            if fid == plan['ramp'] and look['m'].startswith('line_'):
                metric = lambda p: project(plan['source'], p)[0] - plan['cut']
            elif fid in plan['mains'] and 'edge_side' in look:
                def metric(p):
                    s, _, _, lateral, _ = project(plan['path'], p)
                    return np.maximum.reduce([plan['start']-s, s-plan['end'],
                                              np.abs(lateral-plan['side']*plan['edge'])-.5])
            else:
                continue
            pieces = [part for p in pieces for part in _outside(p, metric)]
        coloured = [(look, p) for p in pieces]
        if look['m'] == 'line_yellow':
            for plan in layouts:
                if fid != plan['ramp'] or plan['color'] == 'line_yellow':
                    continue
                split = []
                for style, p in coloured:
                    metric = lambda q: project(plan['source'], q)[0] - plan['color_cut']
                    split.extend((dict(style, m='line_white'), q) for q in _outside(p, lambda q: -metric(q)))
                    split.extend((style, q) for q in _outside(p, metric))
                coloured = split
        out.extend((style, width, p) for style, p in coloured)
    return out


def related(fid, other, layouts):
    # These overlaps have already been trimmed to their taper. Generic asphalt
    # blockers would erase the ramp's continuation across the wide shoulder.
    return any((fid in p['mains'] and other == p['ramp']) or
               (other in p['mains'] and fid == p['ramp']) for p in layouts)


def chunks(layouts, box, fixed=False):
    import roads
    out = []
    for p in layouts:
        profile = roadmarkings.profile(p['spec'].state)
        kind = 'entrance' if p['entry'] else 'exit'
        # Preserve the mainline's outside boundary colour through the taper.
        # A right-side ramp's inner edge stays white until the pavements split;
        # a left-side ramp's outside edge remains yellow throughout.
        lines = [(dict(m=p['color'], p=2, tl=6.4, ramp_merge=kind), profile.solid, p['outer'])]
        if profile.ramp:
            length, gap, width = profile.ramp
            look = dict(m='line_white', p=6, tl=length+gap, dash=(length, gap), ramp_merge=kind)
            # A separator cannot fit at a zero-width tip: start where the paint
            # clears the solid edge, rather than drawing white strokes on top of it.
            clearance = (profile.solid + width)/2 + PAINT_CLEARANCE
            def separator(line, internal=False):
                def metric(points):
                    distance = project(p['outer'], points)[4] - clearance
                    if internal:
                        distance = np.minimum(distance, project(p['guide'], points)[4] - width - PAINT_CLEARANCE)
                    return distance
                for part in _outside(line, metric):
                    lines.append((dict(look, _source=part), width, part))
            separator(p['guide'])
            # A multi-lane ramp sheds its additional lanes in sequence as the available width narrows.
            count = p['spec'].lanes or 1
            for k in range(1, count):
                offset = p['span'] - k * (p['spec'].width-2*roads.edge_margin(p['spec'])) / count
                fraction = np.maximum(0., offset) / np.maximum(p['span'], 1e-9)
                line = p['guide'] + (p['outer']-p['guide']) * fraction[:, None]
                keep = np.flatnonzero(offset > .05)
                if len(keep) >= 2:
                    line = line[keep[0]:]
                    separator(line, internal=True)
        for look, width, line in lines:
            for piece in roads.clip_polyline(line, *box):
                for part in roads.split(piece):
                    ch = roads._chunk(look, width, part)
                    if fixed:
                        ch['fixed'] = True
                    out.append(ch)
    return out
