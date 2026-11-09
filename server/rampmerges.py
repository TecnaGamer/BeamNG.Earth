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
    """Station, nearest point, tangent, signed left offset, and distance in xy (earthfast.polyline_project: the
    stations extrapolated beyond the ends, for trimming a longer source line)."""
    import fast
    fast.require(164, "polyline_project")
    return fast._ef.polyline_project(np.ascontiguousarray(line, np.float64),
                                     np.ascontiguousarray(np.atleast_2d(points), np.float64))


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


def _main_edge(path, ids, mains, entry, side, geometry=None):
    """The existing 1.8 paint edge, including its native width taper and shift."""
    import roads
    edges = []
    for fid, sp, line, _ in mains:
        if fid not in ids:
            continue
        pts, widths = geometry(fid, sp, line) if geometry else (line, sp.width)
        orientation = (sp.dir or 1) * (-1 if entry else 1)
        edge = roads.offset_polyline(pts, side * orientation *
                                     (np.asarray(widths) / 2 - roads.edge_inset(sp, side * orientation)))
        edges.append(edge)
    points = np.vstack(edges)
    at = project(path, points)[0]
    order = np.argsort(at, kind='stable')
    at, points = at[order], points[order]
    keep = np.r_[True, np.diff(at) > 1e-6]
    return at[keep], points[keep]


def _at_edge(edge, stations):
    at, points = edge
    return np.column_stack([np.interp(stations, at, points[:, d]) for d in range(3)])


def _supported(points, rows, pavement=None, clearance=.2):
    """Paint must fit the actual asphalt, including 1.8's shifted/tapered decks."""
    fits = np.zeros(len(points), bool)
    for fid, sp, line, _ in rows:
        axis, widths = pavement(fid, sp, line) if pavement else (line, sp.width)
        at, near, _, _, distance = project(axis, points)
        widths = np.asarray(widths)
        half = widths/2 if widths.ndim == 0 else np.interp(at, roadmarkings.arc(axis), widths)/2
        fits |= (distance <= half-clearance) & (np.abs(points[:, 2]-near[:, 2]) <= HEIGHT)
    return fits


def _lane_continuation(plan, main, mains, geometry, pavement=None):
    """Connect a lane added/dropped at the join to 1.8's shifted wider mainline.

    A ramp whose lane continues onto the highway needs a full-width connection,
    rather than a zero-width merge tip. Keep the native through-lane positions.
    """
    import roads
    origin = plan['source'][0]
    forward = sample(plan['path'], min(10., roadmarkings.arc(plan['path'])[-1]))[0] - origin
    forward /= max(np.linalg.norm(forward[:2]), 1e-9)
    choices = []
    for fid, sp, line, lon in mains:
        if fid in plan['mains'] or not sp.lanes or not main[1].lanes or sp.lanes <= main[1].lanes:
            continue
        orientation = (sp.dir or 1) * (-1 if plan['entry'] else 1)
        at_join = -1 if orientation > 0 else 0
        other = -2 if at_join == -1 else 1
        tangent = line[other] - line[at_join]
        tangent /= max(np.linalg.norm(tangent[:2]), 1e-9)
        if (np.linalg.norm(line[at_join, :2] - origin[:2]) > JOIN
                or abs(line[at_join, 2] - origin[2]) > HEIGHT or tangent[:2] @ forward[:2] > -.9):
            continue
        pts, widths = geometry(fid, sp, line) if geometry else (line, sp.width)
        side = plan['side'] * orientation
        edge = roads.offset_polyline(pts, side * (np.asarray(widths)/2 - roads.edge_inset(sp, side)))[at_join]
        guide = _at_edge(plan['main_edge'], [0.])[0]
        normal = np.array([-forward[1], forward[0]]) * plan['side']
        span = float((edge[:2] - guide[:2]) @ normal)
        drive = roads.lane_layout(sp)[0] / sp.lanes
        if abs(span - drive * (sp.lanes-main[1].lanes)) < .5:
            choices.append((fid, edge, span))
    if len(choices) != 1:
        return False
    fid, edge, span0 = choices[0]
    end = plan['end']
    at = np.linspace(0., end, max(3, int(np.ceil(end)) + 1))
    guide = _at_edge(plan['main_edge'], at)
    t = at / max(end, 1e-9)
    guide += t[:, None]**3 * (plan['guide'][-1] - guide[-1])
    tangent = project(plan['path'], guide)[2]
    normal = np.c_[-tangent[:, 1], tangent[:, 0]] * plan['side']
    span = span0 + (plan['span'][-1] - span0) * t*t*(3.-2.*t)
    outer = guide.copy()
    outer[:, :2] += normal * span[:, None]
    outer += (1.-t[:, None])**3 * (edge - outer[0])
    outer += t[:, None]**3 * (plan['outer'][-1] - outer[-1])
    # Never extend the new lane beyond the mapped pavement's union.
    def supported(points):
        rows = [r for r in mains if r[0] in plan['mains'] | {fid}] + [plan['row']]
        return _supported(points, rows, pavement)
    if not supported(guide).all() or not supported(outer[[0, -1]]).all():
        return False
    limited = np.flatnonzero(~supported(outer))
    if len(limited):
        low, high = np.zeros(len(limited)), np.ones(len(limited))
        delta = outer[limited] - guide[limited]
        for _ in range(16):
            mid = (low+high)/2
            fits = supported(guide[limited] + mid[:, None]*delta)
            low, high = np.where(fits, mid, low), np.where(fits, high, mid)
        outer[limited] = guide[limited] + low[:, None]*delta
        span[limited] *= low
    plan.update(start=0., guide=guide, outer=outer, span=span, normal=normal,
                mains=plan['mains'] | {fid}, lane_continuation=True)
    return True


def _extend(plan, main, station, mains, neighbours, geometry=None, pavement=None):
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
    edge = _main_edge(path, ids, mains, plan['entry'], plan['side'], geometry)
    guide = _at_edge(edge, at)
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
        return _supported(points, [r for r in mains if r[0] in ids] + [plan['row']], pavement, paint)

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
    if not _supported(roads.densify(outer, .25), [r for r in mains if r[0] in ids] + [plan['row']],
                      pavement, paint - .05).all():
        return  # chords across a concave deck join must also remain on asphalt
    plan.update(path=path, mains=ids, start=float(start), end=float(end),
                guide=guide, outer=outer, span=span, normal=normal, main_edge=edge)


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


def plans(rows, terminals=None, geometry=None, pavement=None):
    """Plans for [(fid, Spec, xyz, longitude)], all at one road level.

    terminals optionally restricts links to their true source endpoints, avoiding
    artificial joins at the boundary between a ground part and a bridge part.
    """
    import roads
    rows = [(fid, sp, p[np.r_[True, np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1) > 1e-6]], lon)
            for fid, sp, p, lon in rows if len(p) >= 2]
    rows = [r for r in rows if len(r[2]) >= 2]
    mains = [r for r in rows if r[1].kind == 'paved' and r[1].oneway and not r[1].link and not getattr(r[1], 'roundabout', False)
             and r[1].cls in ('motorway', 'trunk') and roadmarkings.for_road(r[1], lon=r[3]) is not None]
    # each main's xy box grown by JOIN: an end outside it is farther than JOIN from that main (the same choices,
    # without projecting every ramp end on every main: 6700 project() calls per 16 Midtown tiles)
    main_boxes = np.array([[r[2][:, 0].min() - JOIN, r[2][:, 1].min() - JOIN, r[2][:, 0].max() + JOIN,
                            r[2][:, 1].max() + JOIN] for r in mains]).reshape(-1, 4)
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
            ex, ey = float(away[0, 0]), float(away[0, 1])
            near_mains = np.flatnonzero((main_boxes[:, 0] <= ex) & (ex <= main_boxes[:, 2]) &
                                        (main_boxes[:, 1] <= ey) & (ey <= main_boxes[:, 3]))
            for row in (mains[i] for i in near_mains):
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
            edge = main[1].width / 2 - roads.edge_inset(main[1], side * main_orientation)
            main_edge = _main_edge(path, ids, mains, entry, side, geometry)
            edge_distance = project(path, _at_edge(main_edge, ms))[3] * side
            drive = sp.width - 2 * roads.edge_inset(sp)
            if drive <= 0:
                continue
            # Offset the complete ramp first: its surviving edge paint and the
            # taper must meet exactly, including on bends/asymmetric shoulders.
            paint_pts, widths = geometry(fid, sp, source) if geometry else (source, sp.width)
            outer_edge = roads.offset_polyline(paint_pts, side * ramp_orientation *
                                               (np.asarray(widths) / 2 - roads.edge_inset(sp, side * ramp_orientation)))
            inner_edge = roads.offset_polyline(paint_pts, -side * ramp_orientation *
                                               (np.asarray(widths) / 2 - roads.edge_inset(sp, -side * ramp_orientation)))
            if ramp_orientation < 0:
                outer_edge, inner_edge = outer_edge[::-1], inner_edge[::-1]
            def boundary(line, stations):
                station = project(away, line)[0]
                return np.column_stack([np.interp(stations, station, line[:, d]) for d in range(3)])
            inner_distance = project(path, boundary(inner_edge, at))[3] * side
            outer_distance = project(path, boundary(outer_edge, at))[3] * side
            separated = np.flatnonzero(inner_distance >= edge_distance)
            if not len(separated):
                continue
            end = int(separated[0])
            distance = signed * side
            if end < 2 or np.any(np.diff(ms[:end+1]) <= 0.) or np.any(np.diff(distance[:end+1]) < -.15):
                continue
            if ms[end] > roadmarkings.arc(path)[-1] or np.max(np.abs(rp[:end+1, 2]-near[:end+1, 2])) > HEIGHT:
                continue
            # Exact full-width boundary and zero-width taper tip, independent of sampling/tile edges.
            far = float(np.interp(0., inner_distance[:end+1] - edge_distance[:end+1], at[:end+1]))
            tip = float(np.interp(0., outer_distance[:end+1] - edge_distance[:end+1], at[:end+1]))
            if far - tip < 4.:
                continue
            ra = np.linspace(tip, far, max(3, int(np.ceil(far-tip)) + 1))
            rp = sample(away, ra)
            ms, near, tangent, signed, _ = project(path, rp)
            normal = np.c_[-tangent[:, 1], tangent[:, 0]] * side
            raw_outer = boundary(outer_edge, ra)
            guide = _at_edge(main_edge, ms)
            available = np.maximum(0., project(path, raw_outer)[3] * side - project(path, guide)[3] * side)
            t = np.clip((available - available[0]) / max(available[-1] - available[0], 1e-9), 0., 1.)
            # Zero slope at the merge tip, original ramp slope at the far end.
            span = drive * t * t * (2 - t)
            guide += t[:, None] ** 3 * (boundary(inner_edge, [far])[0] - guide[-1])
            outer = guide.copy()
            outer[:, :2] += normal * span[:, None]
            outer[:, 2] += t * (rp[:, 2] - near[:, 2])
            outer += t[:, None] ** 3 * (raw_outer[-1] - outer[-1])
            # Every stroke remains supported by existing asphalt, including on curves.
            paint = roadmarkings.profile(sp.state).solid / 2
            ramp_row = (fid, sp, source, lon)
            support = [r for r in mains if r[0] in ids] + [ramp_row]
            if not _supported(outer, support, pavement, paint).all():
                continue
            guide_station = project(path, guide)[0]
            color_cut = max(far, _pavement_split(main, station, mains, entry, away, side, sp.width))
            out.append(dict(ramp=fid, mains=ids, entry=entry, source=away, cut=far, path=path,
                            color_cut=color_cut,
                            start=float(guide_station[0]), end=float(guide_station[-1]), edge=edge, side=side,
                            guide=guide, outer=outer, span=span, normal=normal, spec=sp, row=ramp_row, main_edge=main_edge,
                            # Left/right relative to actual travel, not the OSM vertex order.
                            color=roads.edge_colour(main[1], side * main_orientation)))
            extensions.append((main, station))
    for plan, (main, station) in zip(out, extensions):
        if not _lane_continuation(plan, main, mains, geometry, pavement):
            _extend(plan, main, station, mains, out, geometry, pavement)
    return out


def ground(frame, cells, urban=None, widths=None, left_hand=False):
    """Cached full source neighbourhood; tile bounds never enter the layout."""
    import roads
    import mapgeom
    def build():
        rows, seen, terminals, copies = [], set(), {}, {}
        for feats in cells:
            if feats is None:
                continue
            for i, sp in enumerate(roads.road_specs(feats, urban, widths, left_hand)):
                fid = str(feats.ids[i])
                copies.setdefault(fid, []).append((feats, i))
                if sp is None or fid in seen or (sp.cls not in ('motorway', 'trunk') and not sp.link):
                    continue
                seen.add(fid)
                for part in feats.parts(i):
                    for ring in part:
                        if len(ring):
                            e, n = mapgeom.enu_xy(feats, frame, ring[[0, -1]])
                            terminals.setdefault(fid, []).extend(np.c_[e, n, np.zeros(2)])
                for ring in roads.ground_parts(feats, i):
                    e, n = mapgeom.enu_xy(feats, frame, ring)
                    rows.append((fid, sp, np.c_[e, n, np.zeros(len(e))], float(np.mean(ring[:, 0]))))
        memo = {}       # (kind, fid, id(pts)) -> its result: plans asks again and again for the same rows
        placement = roads.placement_context(cells, urban, widths, left_hand)    # (the mains where build_tile draws them)

        def placed(fid, spec, pts, lon, lat):
            if not placement.on:
                return pts
            f, i = copies[fid][0]
            sh = placement.shift(f, i, lon, lat, spec, fid)
            return pts if sh is None else roads.offset_polyline(pts, sh)

        def remembered(kind, fn):
            def get(fid, spec, pts):
                k = (kind, fid, id(pts))
                hit = memo.get(k)
                if hit is None or hit[0] is not pts:
                    hit = memo[k] = (pts, fn(fid, spec, pts))
                return hit[1]
            return get

        def dense(fid, spec, pts):          # (a moved main sampled where its shift changes)
            if not placement.on:
                return pts
            f, i = copies[fid][0]
            return placement.dense(f, i, pts, spec, fid)

        def geometry(fid, spec, pts):
            # Evaluate the same descriptors as build_tile; do not substitute the
            # older lane-transition or pavement generators for 1.8's versions.
            pts = dense(fid, spec, pts)
            lat, lon, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.)
            tp = roads.joint_desc(copies[fid], lambda f: roads.road_tapers(f, urban, widths, left_hand))
            ls = roads.joint_desc(copies[fid], lambda f: roads.lane_shifts(f, urban, widths, left_hand))
            w = roads.taper_widths(spec.width, tp, lon, lat) if tp else spec.width
            # A taper elsewhere on this source road may not reach this part.
            # None means constant width, not missing geometry.
            if w is None:
                w = spec.width
            sh = roads.shift_at(ls, lon, lat) if ls else None
            pts = placed(fid, spec, pts, lon, lat)
            return (roads.offset_polyline(pts, sh) if sh is not None else pts), w
        def pavement(fid, spec, pts):
            # On the ground only the markings shift; the native width taper
            # also changes asphalt. A bridge shifts its whole deck instead.
            pts = dense(fid, spec, pts)
            lat, lon, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.)
            tp = roads.joint_desc(copies[fid], lambda f: roads.road_tapers(f, urban, widths, left_hand))
            w = roads.taper_widths(spec.width, tp, lon, lat) if tp else spec.width
            return placed(fid, spec, pts, lon, lat), spec.width if w is None else w
        return plans(rows, terminals, remembered("g", geometry), remembered("p", pavement))
    owner = getattr(urban, '__self__', None)
    return _cache.get(frame, cells, (urban, widths, bool(left_hand), getattr(owner, 'epoch', 0)), build)


def _outside(line, metric):
    """Keep metric > 0; interpolate the exact crossing between one-metre samples."""
    import roads
    p = roads.densify(line, STEP)
    v = metric(p)
    keep = v > 0
    # Long source ways usually have only one short trimmed interval. Slice
    # their retained runs instead of copying every metre through Python.
    changes = np.flatnonzero(keep[1:] != keep[:-1])
    bounds = np.r_[0, changes + 1, len(p)]
    out = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        if not keep[a]:
            continue
        pieces = []
        if a:
            i = a - 1
            pieces.append((p[i] + v[i] / (v[i]-v[i+1]) * (p[i+1]-p[i]))[None])
        pieces.append(p[a:b])
        if b < len(p):
            i = b - 1
            pieces.append((p[i] + v[i] / (v[i]-v[i+1]) * (p[i+1]-p[i]))[None])
        run = np.concatenate(pieces) if len(pieces) > 1 else pieces[0]
        if len(run) > 1:
            out.append(run)
    return out


def trim(fid, extras, layouts):
    """Remove obsolete ramp paint and only the mainline edge being replaced."""
    out = []
    for look, width, line in extras:
        pieces = [line]
        for plan in layouts:
            if fid == plan['ramp'] and look['m'].startswith('line_'):
                metric = lambda p: project(plan['source'], p)[0] - plan['cut']
            elif fid in plan['mains'] and look.get('kind') == 'edge':
                def metric(p):
                    s, _, _, lateral, _ = project(plan['path'], p)
                    edge = project(plan['path'], _at_edge(plan['main_edge'], s))[3]
                    return np.maximum.reduce([plan['start']-s, s-plan['end'],
                                              np.abs(lateral-edge)-.5])
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


def continuation(fid, spec, other, other_spec, going_on):
    """An aligned highway source join must not erase a circular patch of paint.

    Do not exempt junctions, frontage roads or opposing carriageways. _going_on
    supplies reciprocal, aligned endpoint matches for these one-way highways.
    """
    return (spec.oneway and other_spec.oneway and not spec.link and not other_spec.link
            and spec.cls in ('motorway', 'trunk') and other_spec.cls == spec.cls
            and (fid, other) in going_on)


def through_joins(rows, going_on):
    """Aligned source pieces must also connect in their actual travel direction."""
    by_id = {}
    for fid, spec, pts, _ in rows:
        if spec.oneway and len(pts) >= 2:
            p = pts if spec.dir >= 0 else pts[::-1]
            by_id.setdefault(fid, []).append((p[0], p[-1]))
    out = set()
    for fid, other in going_on:
        if any(np.linalg.norm(end[:2]-start[:2]) < JOIN and abs(end[2]-start[2]) < HEIGHT
               for _, end in by_id.get(fid, ()) for start, _ in by_id.get(other, ())):
            out.update(((fid, other), (other, fid)))
    return out


def connections(rows):
    """Connected ramp side, even when a short/complex ramp cannot fit a taper.

    Such a ramp may still cut the outside edge opening, but must never erase
    through-lane dividers or the opposite median edge. No proximity-only match.
    """
    mains = [r for r in rows if r[1].oneway and not r[1].link
             and r[1].cls in ('motorway', 'trunk') and len(r[2]) >= 2]
    out = {}
    for fid, spec, line, _ in rows:
        if not spec.oneway or not spec.link or len(line) < 2:
            continue
        for end, other in ((0, 1), (-1, -2)):
            travel = (line[other, :2]-line[end, :2]) * (spec.dir or 1) * (1 if end == 0 else -1)
            travel /= max(np.linalg.norm(travel), 1e-9)
            for mid, ms, main, _ in mains:
                _, near, tangent, _, dist = project(main, line[[end]])
                if (dist[0] > JOIN or abs(near[0, 2]-line[end, 2]) > HEIGHT
                        or tangent[0] @ travel * (ms.dir or 1) < .75):
                    continue
                side = project(main, line[[other]])[3][0]
                if abs(side) > .05:
                    out[mid, fid] = 1 if side > 0 else -1
    return out


def _paint_lines(p):
    """A layout's paint lines [(look, width, line)], tile independent: built once per layout (the layouts are cached
    for the source neighbourhood, and every road tile under them asked for all of them again: ~0.4 s per 16 Phoenix
    I-10 tiles in the separators' projections), with their xy bounding box."""
    import roads
    got = p.get('_paint')
    if got is not None:
        return got
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
            offset = p['span'] - k * (p['spec'].width-2*roads.edge_inset(p['spec'])) / count
            fraction = np.maximum(0., offset) / np.maximum(p['span'], 1e-9)
            line = p['guide'] + (p['outer']-p['guide']) * fraction[:, None]
            keep = np.flatnonzero(offset > .05)
            if len(keep) >= 2:
                line = line[keep[0]:]
                separator(line, internal=True)
    pts = np.vstack([np.asarray(line)[:, :2] for _, _, line in lines])
    got = p['_paint'] = (lines, (float(pts[:, 0].min()), float(pts[:, 1].min()),
                                 float(pts[:, 0].max()), float(pts[:, 1].max())))
    return got


def chunks(layouts, box, fixed=False):
    import roads
    out = []
    x0, y0, x1, y1 = box
    for p in layouts:
        lines, (lx0, ly0, lx1, ly1) = _paint_lines(p)
        if lx1 < x0 or lx0 > x1 or ly1 < y0 or ly0 > y1:
            continue                # (no paint of this layout in the tile)
        for look, width, line in lines:
            for piece in roads.clip_polyline(line, *box):
                for part in roads.split(piece):
                    ch = roads._chunk(look, width, part)
                    if fixed:
                        ch['fixed'] = True
                    out.append(ch)
    return out
