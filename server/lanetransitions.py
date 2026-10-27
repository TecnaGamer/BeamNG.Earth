"""Left-side mainline lane additions/drops, planned before tile clipping.

The through lanes retain their order and width from the right edge. Only the
leftmost lane opens/closes; paint stays inside the mapped pavement/deck. Source
segments and bridge boundaries do not restart the taper.
"""
from collections import defaultdict
import numpy as np

import roadlayoutcache
import roadmarkings
import rampmerges

TAPER_M = 240.0                 # per added lane, shortened only by the next junction/change
STEP = 2.0
_cache = roadlayoutcache.LayoutCache(max_entries=12, max_bytes=24*1024*1024)


def eligible(sp):
    return sp.oneway and not sp.link and not sp.roundabout and sp.cls in ('motorway', 'trunk') and sp.kind == 'paved' and bool(sp.lanes)


def plans(rows):
    """Unambiguous, same-direction endpoint joins; ramp junctions are excluded."""
    rows = sorted((r for r in rows if len(r[2]) >= 2), key=lambda r: r[0])
    endpoints, buckets = [], defaultdict(list)
    for i, (_, sp, line, _) in enumerate(rows):
        for end, other in ((0, 1), (-1, -2)):
            d = line[other, :2]-line[end, :2]
            d = d/max(np.linalg.norm(d), 1e-9)
            key = tuple(np.floor(line[end, :2]).astype(int))
            buckets[key].append(len(endpoints))
            endpoints.append((i, end, line[end], d, (sp.dir or 1)*(1 if end == -1 else -1)))
    choices, blocked = [], set()
    for e, (i, end, p, tangent, flow) in enumerate(endpoints):
        sp = rows[i][1]
        key = np.floor(p[:2]).astype(int)
        near = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in buckets.get((key[0]+dx, key[1]+dy), ()):
                    k, _, q, heading, flow2 = endpoints[j]
                    if k == i or np.linalg.norm(p[:2]-q[:2]) > .75 or abs(p[2]-q[2]) > .75:
                        continue
                    other = rows[k][1]
                    if other.link:
                        blocked.add(e)
                    if eligible(other) and other.cls == sp.cls and tangent@heading < -.9 and flow == -flow2:
                        near.append(j)
        choices.append(near)
    joins = {i: c[0] for i, c in enumerate(choices) if len(c) == 1 and choices[c[0]] == [i]}
    out = defaultdict(list)
    for e, j in joins.items():
        i, end, _, _, _ = endpoints[e]
        k = endpoints[j][0]
        fid, wide, _, _ = rows[i]
        narrow = rows[k][1]
        if (not eligible(wide) or wide.lanes <= narrow.lanes or wide.width <= narrow.width
                or e in blocked or j in blocked):
            continue
        path, ids, visited, current, bounded = [], set(), set(), e, False
        while current not in visited:
            visited.add(current)
            ri, re, _, _, _ = endpoints[current]
            row = rows[ri]
            p = row[2] if re == 0 else row[2][::-1]
            path.extend(p if not path else p[1:])
            ids.add(row[0])
            far = current+1 if re == 0 else current-1
            following = joins.get(far)
            if far in blocked or following is None:
                bounded = far in blocked
                break
            nextsp = rows[endpoints[following][0]][1]
            if nextsp.lanes != wide.lanes or abs(nextsp.width-wide.width) > .01:
                bounded = True
                break
            if roadmarkings.arc(np.asarray(path))[-1] >= TAPER_M*(wide.lanes-narrow.lanes)*2:
                break
            if rows[endpoints[following][0]][0] in ids:
                break
            current = following
        path = np.asarray(path)
        length = roadmarkings.arc(path)[-1]
        reach = min(TAPER_M*(wide.lanes-narrow.lanes), length*(.5 if bounded else 1.))
        if reach < 10.:
            continue
        # An OSM way can be many kilometres long. Projection only needs the
        # taper, not its entire continuation (which would make NxM arrays huge).
        path = rampmerges.section(path, 0., reach)
        plan = dict(path=path, reach=reach, narrow=narrow, wide=wide, ids=ids,
                    pair={fid, rows[k][0]})
        for rid in ids:
            out[rid].append(plan)
    return dict(out)


def source(frame, cells, urban=None, widths=None, left_hand=False):
    """Full feature geometry includes decks/tunnels so the taper crosses abutments."""
    import roads
    import mapgeom
    def build():
        rows, seen = [], set()
        for feats in cells:
            if feats is None:
                continue
            normal = roads.road_specs(feats, urban, widths, left_hand)
            bridge, tunnel = roads.bridge_specs(feats, urban, widths, left_hand), roads.tunnel_specs(feats)
            for i, sp in enumerate(normal):
                sp = sp or bridge[i] or tunnel[i]
                fid = str(feats.ids[i])
                if sp is None or fid in seen or sp.cls not in ('motorway', 'trunk'):
                    continue
                seen.add(fid)
                for part in feats.parts(i):
                    for ring in part:
                        if len(ring) >= 2:
                            e, n = mapgeom.enu_xy(feats, frame, ring)
                            rows.append((fid, sp, np.c_[e, n, np.zeros(len(e))], 0.))
        return plans(rows)
    owner = getattr(urban, '__self__', None)
    return _cache.get(frame, cells, (urban, widths, bool(left_hand), getattr(owner, 'epoch', 0)), build)


def related(fid, other, layouts):
    return any(other in p['ids'] or other in p['pair'] for p in layouts.get(fid, ())) or any(
        fid in p['pair'] for p in layouts.get(other, ()))


def extras(fid, sp, pts, lon, left_hand, layouts):
    """Replace paint only; the mapped asphalt, shoulders and collision remain usable."""
    import roads
    active = layouts.get(fid, ())
    if not active or not eligible(sp):
        return roads.extras(sp, pts, lon, left_hand)
    p = roads.densify(pts, STEP)
    direction = sp.dir or 1
    base_right = -sp.width/2 + roads.edge_margin(sp, -direction)
    base_drive = sp.width-2*roads.edge_margin(sp)
    right = np.full(len(p), base_right)
    drive = np.full(len(p), base_drive)
    lane = drive/sp.lanes
    weight = np.zeros(len(p))
    for plan in active:
        station, _, _, _, distance = rampmerges.project(plan['path'], p)
        u = np.clip(station/plan['reach'], 0., 1.)
        blend = 1.-u*u*(3.-2.*u)
        blend[(station < -.1) | (distance > .75)] = 0.
        use = blend > weight
        n = plan['narrow']
        nr = -n.width/2+roads.edge_margin(n, -(n.dir or 1))
        nd = n.width-2*roads.edge_margin(n)
        right[use] = base_right + blend[use]*(nr-base_right)
        drive[use] = base_drive + blend[use]*(nd-base_drive)
        lane[use] = base_drive/sp.lanes + blend[use]*(nd/n.lanes-base_drive/sp.lanes)
        weight[use] = blend[use]
    normal = roads.offset_polyline(p, 1.)[:, :2]-p[:, :2]
    def line(offset):
        q = p.copy()
        q[:, :2] += normal*(direction*offset)[:, None]
        return q
    profile = roadmarkings.profile(sp.state)
    out = [(look, w, q) for look, w, q in roads.extras(sp, pts, lon, left_hand)
           if not look['m'].startswith('line_')]
    for side, offset in ((-direction, right), (direction, right+drive)):
        out.append((dict(roads.EDGE_LINE, m=roads.edge_colour(sp, side), edge_side=side), profile.solid, line(offset)))
    length, gap, width = profile.travel
    clearance = (profile.solid+width)/2+.10
    for k in range(1, sp.lanes):
        q = line(right+k*lane)
        # Sequential left lane additions: never draw the new divider over the edge.
        room = drive-k*lane-clearance
        # Interpolate exact clearance crossings on source stations, before clipping/dashing.
        pieces, run = [], []
        for a in range(len(q)-1):
            if room[a] > 0: run.append(q[a])
            if (room[a] > 0) != (room[a+1] > 0):
                run.append(q[a]+room[a]/(room[a]-room[a+1])*(q[a+1]-q[a]))
                if room[a] > 0:
                    if len(run) > 1: pieces.append(np.asarray(run))
                    run = []
        if room[-1] > 0: run.append(q[-1])
        if len(run) > 1: pieces.append(np.asarray(run))
        for piece in pieces:
            out.append((dict(roads.LANE_LINE, m='line_white', dash=(length, gap), tl=length+gap,
                             _source=q), width, piece))
    return out
