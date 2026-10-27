"""Shared circulating-lane widths and clearance for roundabouts."""
import numpy as np

import islands

LANE_M = 9.0
EDGE_M = 0.5
APPROACH_LANE_M = 6.0
ISLAND_CURB_M = 0.4
PROP_CLEAR_M = 1.0
ROAD_CLASSES = {'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'residential', 'unclassified', 'living_street'}


def specs(feats, source, left_hand=False):
    """Keep mapped lane counts, widen circulating lanes, and omit parking/medians.

    OSM junction tags also cover arcs whose ring crosses a cell boundary. Where
    tags are missing, apply the 1.6.3 AI's ring test to curved source arcs and
    short one-way pieces connecting them.
    Enumerating ten-edge cycles of an entire city grid is too costly per cell.
    """
    import roads
    out = list(source)
    rows = []
    if not len(feats.lonlat):
        return out
    ref = feats.lonlat[0]
    scale = np.array([111320.0 * np.cos(np.radians(ref[1])), 110540.0])
    for i, sp in enumerate(source):
        if sp is None or sp.kind not in roads.BUILT or sp.cls not in ROAD_CLASSES or sp.link:
            continue
        for part in feats.parts(i):
            if part and len(part[0]) >= 2:
                xy = (part[0] - ref) * scale
                rows.append((str(i), sp, np.c_[xy, np.zeros(len(xy))], None))
    edges, adj = islands._graph(rows)
    curved = set()
    for k, (_, _, p, _, _, length) in enumerate(edges):
        if length <= islands.RING_MAX_LOOP_M and np.linalg.norm(p[-1, :2] - p[0, :2]) < .99 * length:
            curved.add(k)
    # Public Square has short, nearly straight pieces between its curved arcs.
    # Keep those links without enumerating cycles across an entire street grid.
    candidates = curved | {k for k, (_, sp, _, a, b, length) in enumerate(edges)
                           if sp.oneway and length <= 35.
                           and any(j in curved for j in adj[a])
                           and any(j in curved for j in adj[b])}
    selected = [(fid, sp, p, None) for k, (fid, sp, p, _, _, _) in enumerate(edges) if k in candidates]
    directions = islands.ring_directions(selected, left_hand, obstacles=rows) if selected else {}
    tags = feats.attrs.get('osm_junction', ())
    for i, sp in enumerate(source):
        if sp is None or sp.kind not in roads.BUILT:
            continue
        mapped = i < len(tags) and str(tags[i]) == 'roundabout'
        if not mapped and str(i) not in directions:
            continue
        lanes = sp.lanes or 1
        # Only explicit widths can enlarge this beyond the per-lane minimum;
        # a class's guessed parking/shoulder width is not circulating asphalt.
        width = roads._rule_value(feats.rules.get('width'), i)
        width = float(width) if width is not None and 1 <= float(width) <= 40 else None
        if width is None:
            width = roads._osm(feats, 'width', i, *roads.OSM_WIDTH_M)
        direction = sp.dir or directions.get(str(i), 1)
        osm_dir = feats.attrs.get('osm_oneway', ())
        if not sp.dir and i < len(osm_dir) and str(osm_dir[i]) == '-1':
            direction = -1
        out[i] = sp._replace(width=max(width or 0., lanes * LANE_M + 2 * EDGE_M), lanes=lanes,
                             oneway=True, dir=direction, roundabout=True, median=None, link=False)
    return out


def corridors(rows):
    return _Corridors((pts, sp.width / 2) for _, sp, pts, _ in rows if sp.roundabout)


class _Corridors(tuple):
    """Immutable geometry snapshot with segment data reused by all clearance queries."""
    def __new__(cls, rings):
        items, prepared = [], []
        for points, half in rings:
            p = np.array(points, dtype=np.float64, copy=True)
            p.setflags(write=False)
            items.append((p, half))
            if len(p) < 2:
                continue
            a, d = p[:-1], np.diff(p, axis=0)
            length2 = np.maximum(np.sum(d[:, :2] ** 2, axis=1), 1e-12)
            prepared.append((a, d, length2, half, p[:, :2].min(0), p[:, :2].max(0)))
        obj = super().__new__(cls, items)
        obj.prepared = tuple(prepared)
        return obj


def approaches(feats, source):
    """Give short entry/exit links room for trucks without widening distant streets."""
    import roads
    ring_ids = [i for i, sp in enumerate(source) if sp is not None and sp.roundabout]
    if not ring_ids:
        return source
    ref = feats.lonlat[0]
    scale = np.array([111320. * np.cos(np.radians(ref[1])), 110540.])
    nodes = np.concatenate([part[0][[0, -1]] for i in ring_ids for part in feats.parts(i)])
    nodes = (nodes - ref) * scale
    out = list(source)
    for i, sp in enumerate(source):
        if sp is None or sp.roundabout or sp.kind not in roads.BUILT or sp.cls not in ROAD_CLASSES:
            continue
        parts = [(part[0] - ref) * scale for part in feats.parts(i)]
        if sum(islands._length(p) for p in parts) > 150.:
            continue
        if not any(np.linalg.norm(p[[0, -1], None] - nodes, axis=2).min() < islands.JOIN_M for p in parts):
            continue
        lanes = sp.lanes or (1 if sp.oneway else 2)
        width = max(sp.width, lanes * APPROACH_LANE_M + 2 * EDGE_M)
        if sp.median:
            width = roads.dual_cap(sp.median, width)
        out[i] = sp._replace(width=width, lanes=lanes, roundabout_entry=True)
    return out


def overlaps(points, rings, margin=0., vertical=None):
    """Whether sampled geometry intrudes into circulating pavement (optionally at its height)."""
    q = np.asarray(points, np.float64)
    if not len(q):
        return False
    rings = rings if isinstance(rings, _Corridors) else _Corridors(rings)
    qlo, qhi = q[:, :2].min(0), q[:, :2].max(0)
    for a, d, length2, half, lo, hi in rings.prepared:
        reach = half + margin
        # A conservative broad phase: remote corners must never pay for every
        # point/segment projection of a roundabout elsewhere in the map cell.
        if reach <= 0 or np.any(qhi < lo - reach) or np.any(qlo > hi + reach):
            continue
        near = q[np.all(q[:, :2] >= lo - reach, axis=1) & np.all(q[:, :2] <= hi + reach, axis=1)]
        # Bound temporary arrays even for unusually long mapped polylines.
        batch = max(1, 65536 // len(a))
        for start in range(0, len(near), batch):
            points = near[start:start + batch]
            t = np.clip(np.sum((points[:, None, :2] - a[None, :, :2]) * d[None, :, :2], axis=2)
                        / length2, 0., 1.)
            nearest = a[None] + t[:, :, None] * d[None]
            inside = np.linalg.norm(nearest[:, :, :2] - points[:, None, :2], axis=2) < reach
            if vertical is not None:
                inside &= np.abs(nearest[:, :, 2] - points[:, None, 2]) < vertical
            if inside.any():
                return True
    return False


def clear_props(rows, rings):
    # Keep objects on grade-separated roads and in the central island.
    rings = rings if isinstance(rings, _Corridors) else _Corridors(rings)
    return [r for r in rows if not overlaps([r[1:4]], rings, PROP_CLEAR_M, vertical=3.)]


def connect_walks(runs, rows, walk_cuts=()):
    """Tangent sidewalk joins at curved ring entries, clearing the full pavement.

    Ordinary junction returns use straight branch directions. At a small ring
    those tangents miss the actual curved sidewalk ends, leaving separate slabs.
    Join the real ends with a checked cubic, before the ordinary straight joins.
    Driveways remain blockers; splitter medians are never joined to outer walks.
    """
    import roads
    rings = corridors(rows)
    if not rings:
        return runs
    ends = []
    for k, run in enumerate(runs):
        if run.get('median'):
            continue
        for end in (0, 1):
            p = run['pts'][-1 if end else 0]
            if run['open'][end] and not run['j'][end] and overlaps([p], rings, 14.):
                ends.append((k, end, p, roads._end_dir(run['pts'], bool(end))))
    pairs = []
    for a, (i, ei, p, di) in enumerate(ends):
        for b in range(a + 1, len(ends)):
            j, ej, q, dj = ends[b]
            length = float(np.linalg.norm(q[:2] - p[:2]))
            si = runs[i]['side'] * (1 if ei else -1)
            sj = runs[j]['side'] * (-1 if ej else 1)
            if i == j or si != sj or abs(runs[i]['w'] - runs[j]['w']) > .1 or not .3 < length < 14.:
                continue
            u = (q[:2] - p[:2]) / length
            if u @ di > .25 and -u @ dj > .25 and abs(p[2] - q[2]) < 1.5:
                pairs.append((length, a, b, si))
    out = [dict(r) for r in runs]
    blockers = [(p[:, :2], sp.width / 2) for _, sp, p, _ in rows] + list(walk_cuts)
    used = set()
    for length, a, b, side in sorted(pairs):
        if a in used or b in used:
            continue
        i, ei, p, di = ends[a]
        j, ej, q, dj = ends[b]
        width = out[i]['w']
        t = np.linspace(0., 1., max(4, int(length / .25) + 1))[:, None]
        nearby = [(cl, h) for cl, h in blockers
                  if np.all(np.max(cl, axis=0) + h + width >= np.minimum(p[:2], q[:2]) - length)
                  and np.all(np.min(cl, axis=0) - h - width <= np.maximum(p[:2], q[:2]) + length)]
        mid = (p[:2] + q[:2]) / 2
        nearest = []
        for line, _ in rings:
            start, delta = line[:-1, :2], np.diff(line[:, :2], axis=0)
            along = np.clip(np.sum((mid - start) * delta, axis=1) /
                            np.maximum(np.sum(delta * delta, axis=1), 1e-12), 0., 1.)
            nearest.extend(start + along[:, None] * delta)
        away = mid - min(nearest, key=lambda v: np.linalg.norm(v - mid))
        away /= max(np.linalg.norm(away), 1e-9)
        for bow in (0., .75, 1.5, 3.):
            handle = .35
            c1, c2 = p + np.r_[di * length * handle, 0.], q + np.r_[dj * length * handle, 0.]
            curve = (1-t)**3*p + 3*(1-t)**2*t*c1 + 3*(1-t)*t**2*c2 + t**3*q
            # A chord across a convex ring cuts its asphalt. Bow out while
            # retaining the exact end positions and tangent directions.
            curve[:, :2] += 16 * t**2 * (1-t)**2 * away * min(bow, length * .3)
            # Check both swept edges, including the mitres, not just the centre.
            probes = np.vstack([roads.offset_polyline(curve, s * width / 2) for s in (-1, 0, 1)])
            if any(roads._seg_dist_min(cl, v[:2]) < h - .1 for cl, h in nearby for v in probes):
                continue
            for k, end in ((i, ei), (j, ej)):
                out[k]['j'] = tuple(True if n == end else x for n, x in enumerate(out[k]['j']))
            out.append({'pts': curve, 'side': side, 'w': width, 'fid': 'roundabout-join',
                        'j': (True, True), 'open': (False, False)})
            used.update((a, b))
            break
    return out
