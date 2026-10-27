"""Resolve lane counts once, before ground, bridge, tunnel and AI specs diverge.

Priority: scoped project corrections, explicit OSM way tag, unambiguous matching
current OSM geometry, then agreement along a connected through road. Never borrow lanes from a ramp,
opposing carriageway or crossing bridge. lane_source records the evidence.
"""
import numpy as np
import roadoverrides


def cached_counts(feats, key, read):
    """Recover missing highway tags from other cached copies of the SAME feature.

    Source geometry extends beyond its cell, but OSM enrichment is per cell.
    An old/moved OSM way can therefore be tagged in one copy and absent in the
    next. Read at most 16 cached cells around feature centres/ends; never fetch.
    Only direct evidence for the exact GERS id and source revision is accepted.
    Return overrides plus whether absent context warrants a later retry.
    """
    import mapcells
    counts = feats.attrs.get('lane_count', feats.attrs.get('osm_lanes', [None] * len(feats)))
    sources = feats.attrs.get('lane_source', [''] * len(feats))
    records = feats.attrs.get('src_record', [''] * len(feats))
    classes = feats.attrs.get('class', [''] * len(feats))
    candidates = [i for i in range(len(feats)) if classes[i] in ('motorway', 'trunk')
                  and records[i] and (count(counts[i]) is None or
                                      (count(counts[i]) == 2 and sources[i] == 'connected'))
                  and _rule(feats, 'oneway', i) is not None
                  and _rule(feats, 'subclass', i) != 'link'
                  and str(feats.attrs.get('subclass', [''] * len(feats))[i]) != 'link']
    if not candidates:
        return {}, False
    wanted = {}
    for i in candidates:
        parts = feats.parts(i)
        if len(parts) != 1 or len(parts[0]) != 1 or len(parts[0][0]) < 2:
            continue
        line = parts[0][0]
        # The bbox centre is the source owner cell; endpoints supply the two
        # adjoining contexts without scanning the entire region on each tile.
        for lon, lat in (0.5 * (line.min(0) + line.max(0)), line[0], line[-1]):
            other = mapcells.cell_of(float(lat), float(lon), key.z)
            if other != key:
                wanted.setdefault(other, set()).add(i)
    evidence = {i: set() for i in candidates}
    pending = set()
    for n, (other, indices) in enumerate(wanted.items()):
        if n >= 16:
            pending.update(candidates)
            break
        rows = read(other)
        if rows is None:
            pending.update(indices)
            continue
        for i in indices:
            row = rows.get(str(feats.ids[i]))
            if row is not None and row[0] == str(records[i]) and row[1] is not None:
                evidence[i].add(row[1])
    overrides = {i: next(iter(values)) for i, values in evidence.items() if len(values) == 1}
    return overrides, bool(pending.difference(overrides))


def count(value):
    try:
        n = float(value)
        return int(n) if np.isfinite(n) and n.is_integer() and 1 <= n <= 32 else None
    except (TypeError, ValueError):
        return None


def _rule(feats, name, i):
    table = feats.rules.get(name)
    rows = table.rows(i) if table is not None else ()
    return table.value[rows[0]] if len(rows) else None


def _project(line, points):
    delta = np.diff(line, axis=0)
    length = np.maximum(np.linalg.norm(delta, axis=1), 1e-9)
    t = np.clip(((points[:, None]-line[None, :-1])*delta).sum(2)/length**2, 0., 1.)
    q = line[None, :-1] + t[:, :, None]*delta
    dist = np.linalg.norm(points[:, None]-q, axis=2)
    k = dist.argmin(1)
    return dist[np.arange(len(points)), k], delta[k]/length[k, None]


def continuations(feats, eligible):
    """Unique, reciprocal through-road matches, indexed by (feature, end).

    Keep both ends distinct: two side roads at one end cannot establish a
    through route. Full source geometry makes this independent of render tiles.
    """
    classes = feats.attrs.get('class', np.full(len(feats), ''))
    origin = feats.lonlat[0] if len(feats.lonlat) else np.zeros(2)
    scale = np.array([111320. * np.cos(np.radians(origin[1])), 110574.])
    ends, buckets = {}, {}
    connectors = feats.rules.get('connectors')
    for i in eligible:
        parts = feats.parts(i)
        if len(parts) != 1 or len(parts[0]) != 1:
            continue
        line = (parts[0][0] - origin) * scale
        line = line[np.r_[True, np.linalg.norm(np.diff(line, axis=0), axis=1) > .01]]
        if len(line) < 2:
            continue
        for end, other in ((0, 1), (1, -2)):
            at = 0 if end == 0 else -1
            tangent = line[other] - line[at]
            tangent /= np.linalg.norm(tangent)
            ids = frozenset(str(connectors.value[r]) for r in connectors.rows(i)
                            if abs(float(connectors.start[r]) - end) < .001) if connectors else frozenset()
            direction = _rule(feats, 'oneway', i)
            flow = (0 if direction is None else -1 if float(direction) < 0 else 1) * (1 if end else -1)
            ends[i, end] = (line[at], tangent, ids, flow)
            buckets.setdefault(tuple(np.floor(line[at]).astype(int)), []).append((i, end))
    choices = {}
    for a, (p, tangent, ids, flow) in ends.items():
        key = np.floor(p).astype(int)
        near = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for b in buckets.get((key[0] + dx, key[1] + dy), ()):
                    q, heading, other_ids, other_flow = ends[b]
                    if (a[0] != b[0] and classes[a[0]] == classes[b[0]]
                            and flow == -other_flow
                            and not (ids and other_ids and ids.isdisjoint(other_ids))
                            and np.linalg.norm(p - q) <= .75 and tangent @ heading < -.9):
                        near.append(b)
        if len(near) == 1:
            choices[a] = near[0]
    return {a: b for a, b in choices.items() if choices.get(b) == a}


def bounded_components(candidates, joins):
    """Candidate chains and their two outside ends; no loops, dead ends or forks."""
    unseen = set(candidates)
    while unseen:
        component, pending, outside, closed = set(), [min(unseen)], [], True
        while pending:
            i = pending.pop()
            if i in component:
                continue
            component.add(i)
            for end in (0, 1):
                b = joins.get((i, end))
                if b is None:
                    closed = False
                elif b[0] in candidates:
                    pending.append(b[0])
                else:
                    outside.append(b[0])
        unseen.difference_update(component)
        if closed and len(outside) == 2 and outside[0] != outside[1]:
            yield component, outside


def generated_counts(feats):
    """Apply project corrections and recheck guesses without rewriting downloads.

    A cached 'connected' count is inference, not an explicit OSM tag. A lone
    lanes=1 segment must not shrink all the untagged road leading up to it.
    Otherwise direct OSM/geometry evidence and one-way carriageways are retained.
    """
    saved = feats.__dict__.get('_generated_lanes')
    if saved is not None:
        return saved
    values = feats.attrs.get('lane_count', feats.attrs.get('osm_lanes', [None] * len(feats)))
    counts = [count(v) for v in values]
    for i, value in feats.__dict__.get('_cached_lane_counts', {}).items():
        counts[i] = value
    for i, value in roadoverrides.lane_counts(feats).items():
        counts[i] = value
    sources = feats.attrs.get('lane_source', [''] * len(feats))
    classes = feats.attrs.get('class', [''] * len(feats))
    eligible = {i for i in range(len(feats)) if classes[i] in
                {'trunk', 'primary', 'secondary', 'tertiary', 'residential', 'unclassified'}
                and _rule(feats, 'oneway', i) is None and _rule(feats, 'subclass', i) != 'link'
                and str(feats.attrs.get('subclass', [''] * len(feats))[i]) != 'link'}
    guesses = {i for i in eligible if counts[i] == 1 and sources[i] == 'connected'}
    if guesses:
        for i in guesses:
            counts[i] = None
        for component, outside in bounded_components(guesses, continuations(feats, eligible)):
            if all(counts[i] == 1 for i in outside):
                for i in component:
                    counts[i] = 1
    # Recheck missing/cached guesses in the full source context. Explicit
    # two-lane sections and real 3 -> 2 transitions remain authoritative.
    highway = {i for i in range(len(feats)) if classes[i] in ('motorway', 'trunk')
               and _rule(feats, 'oneway', i) is not None
               and _rule(feats, 'subclass', i) != 'link'
               and str(feats.attrs.get('subclass', [''] * len(feats))[i]) != 'link'
               and str(feats.attrs.get('osm_junction', [''] * len(feats))[i]) not in ('roundabout', 'circular')}
    gaps = {i for i in highway if counts[i] is None or (counts[i] == 2 and sources[i] == 'connected')}
    if gaps:
        for component, outside in bounded_components(gaps, continuations(feats, highway)):
            if all(counts[i] == 3 for i in outside):
                for i in component:
                    counts[i] = 3
    feats._generated_lanes = counts
    return counts


def keep_two_lanes(feats, specs, eligible, minimum):
    """A narrow rural section bounded by two-lane continuations stays two-lane."""
    candidates = {i for i in eligible if specs[i].lanes in (None, 1)}
    if not candidates:
        return specs
    out = list(specs)
    for component, outside in bounded_components(candidates, continuations(feats, eligible)):
        if all(specs[i].lanes == 2 for i in outside):
            for i in component:
                out[i] = out[i]._replace(width=max(out[i].width, minimum), lanes=2)
    return out


def resolve(feats, tags, way_ids):
    n = len(feats)
    raw = feats.attrs.get('osm_lanes', np.full(n, np.nan))
    counts = np.array([count(v) or np.nan for v in raw])
    sources = ['osm-way' if np.isfinite(v) else '' for v in counts]
    matched = np.array([w or 0 for w in way_ids], dtype=np.int64)
    classes = feats.attrs.get('class', np.full(n, ''))
    origin = feats.lonlat.mean(0) if len(feats.lonlat) else np.zeros(2)
    scale = np.array([111320.*np.cos(np.radians(origin[1])), 110574.])
    local = lambda p: (np.asarray(p)-origin)*scale
    lines, dirs, links = [], [], []
    for i in range(n):
        parts = feats.parts(i)
        line = local(parts[0][0]) if parts and parts[0] else np.empty((0, 2))
        if len(line):
            line = line[np.r_[True, np.linalg.norm(np.diff(line, axis=0), axis=1) > .01]]
        lines.append(line)
        d = _rule(feats, 'oneway', i)
        dirs.append(0 if d is None else -1 if float(d) < 0 else 1)
        links.append(_rule(feats, 'subclass', i) == 'link' or
                     str(feats.attrs.get('subclass', ['']*n)[i]) == 'link')

    candidates = []
    for wid, tag in tags.items():
        p = tag.get('_coords')
        lanes = count(tag.get('lanes'))
        if lanes is None or p is None or len(p) < 2:
            continue
        p = local(p)
        highway = tag.get('_highway', '')
        candidates.append((wid, lanes, highway.removesuffix('_link'), highway.endswith('_link'),
                           tag.get('osm_oneway', ''), p, p.min(0)-6., p.max(0)+6.))
    for i, line in enumerate(lines):
        if np.isfinite(counts[i]) or len(line) < 2:
            continue
        s = np.r_[0., np.cumsum(np.linalg.norm(np.diff(line, axis=0), axis=1))]
        samples = np.column_stack([np.interp(np.linspace(0., s[-1], 9), s, line[:, d]) for d in (0, 1)])
        _, tangent = _project(line, samples)
        matches = []
        for wid, lanes, cls, link, oneway, p, lo, hi in candidates:
            if cls != classes[i] or link != links[i] or np.any(samples < lo) or np.any(samples > hi):
                continue
            distance, heading = _project(p, samples)
            alignment = (tangent*heading).sum(1)
            od = -1 if oneway == '-1' else 1 if oneway in ('yes', '1', 'true') else 0
            if dirs[i] and od:
                alignment *= dirs[i]*od
            else:
                alignment = np.abs(alignment)
            if distance.max() <= 6. and np.mean(distance) <= 3. and alignment.min() >= .9:
                matches.append((float(np.mean(distance)), wid, lanes))
        matches.sort()
        if matches and not any(m[2] != matches[0][2] and m[0] < matches[0][0]+1. for m in matches[1:]):
            _, matched[i], counts[i] = matches[0]
            sources[i] = 'osm-geometry'

    # Connect only unbranched, aligned continuations. Different explicit counts
    # at opposite ends are a real/ambiguous lane transition, not a majority vote.
    endpoints, buckets = [], {}
    for i, line in enumerate(lines):
        if len(line) < 2 or links[i] or classes[i] not in {'motorway', 'trunk', 'primary', 'secondary', 'tertiary'}:
            continue
        for end, other in ((0, 1), (-1, -2)):
            tangent = line[other]-line[end]
            tangent /= max(np.linalg.norm(tangent), 1e-9)
            k = tuple(np.floor(line[end]).astype(int))
            e = len(endpoints)
            connectors = feats.rules.get('connectors')
            ids = frozenset(str(connectors.value[r]) for r in connectors.rows(i)
                            if abs(float(connectors.start[r])-(1. if end == -1 else 0.)) < .001) if connectors else frozenset()
            endpoints.append((i, line[end], tangent, dirs[i]*(1 if end == -1 else -1), ids))
            buckets.setdefault(k, []).append(e)
    choices = []
    for e, (i, p, tangent, flow, ids) in enumerate(endpoints):
        k = np.floor(p).astype(int)
        near = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in buckets.get((k[0]+dx, k[1]+dy), ()):
                    f, q, heading, flow2, ids2 = endpoints[j]
                    if ids and ids2 and ids.isdisjoint(ids2):
                        continue
                    if f != i and classes[f] == classes[i] and bool(dirs[f]) == bool(dirs[i]) and (
                            np.linalg.norm(p-q) <= .75 and tangent @ heading < -.9 and flow == -flow2):
                        near.append(j)
        choices.append(near)
    adj = [set() for _ in range(n)]
    for e, near in enumerate(choices):
        if len(near) == 1 and choices[near[0]] == [e]:
            a, b = endpoints[e][0], endpoints[near[0]][0]
            adj[a].add(b)
            adj[b].add(a)
    seen = set()
    for i in range(n):
        if i in seen or np.isfinite(counts[i]):
            continue
        component, stack, known = set(), [i], set()
        while stack:
            j = stack.pop()
            if j in component:
                continue
            component.add(j)
            for other in adj[j]:
                if np.isfinite(counts[other]):
                    known.add(int(counts[other]))
                elif other not in component:
                    stack.append(other)
        seen.update(component)
        if len(known) == 1:
            # A single known shared lane at one end is not evidence that every
            # untagged two-way road approaching it is also a single lane.
            if known == {1} and not dirs[i]:
                exits = sum(other not in component for j in component for other in adj[j])
                if exits != 2 or any(len(adj[j]) != 2 for j in component):
                    continue
            for j in component:
                counts[j] = next(iter(known))
                sources[j] = 'connected'
    feats.attrs['lane_count'] = counts
    feats.attrs['lane_source'] = np.array(sources, dtype='<U16')
    feats.attrs['lane_osm_way'] = matched
