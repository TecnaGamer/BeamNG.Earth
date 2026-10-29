"""Lane evidence shared by pavement, structures, markings and navigation.

Adapted from the 1.7.0 custom build. Highway counts come from the source way,
an unambiguous current geometry match, or agreeing connected approaches. A
nearby road's name alone is not evidence for a highway carriageway's count.
"""
import numpy as np

HIGHWAYS = {"motorway", "trunk"}
MAX_LANES = 12


def count(value, minimum=1):
    """A whole lane count, not a prefix of a list, fraction or conditional tag."""
    try:
        n = float(value)
        return int(n) if np.isfinite(n) and n.is_integer() and minimum <= n <= MAX_LANES else None
    except (TypeError, ValueError):
        return None


def rule(feats, name, i):
    table = feats.rules.get(name)
    j = table.major(i) if table is not None else -1
    return table.value[j] if j >= 0 else None


def directions(total, osm=None):
    """(forward, backward, shared turn lanes) consistent with a two-way total.

    Incomplete directional tags can supply the missing side, but conflicting
    tags cannot invent lanes. An odd unqualified total keeps the extra lane
    along the geometry; a mapped shared turn lane is excluded from through AI.
    """
    osm = osm or {}
    both = count(osm.get("lanes:both_ways"), 0) or 0
    if total - both < 2:
        both = 0
    through = total - both
    fwd, bwd = count(osm.get("lanes:forward")), count(osm.get("lanes:backward"))
    if fwd is not None and bwd is None and fwd < through:
        bwd = through - fwd
    elif bwd is not None and fwd is None and bwd < through:
        fwd = through - bwd
    if fwd is None or bwd is None or fwd + bwd != through:
        fwd, bwd = max(1, (through + 1) // 2), max(1, through // 2)
    return fwd, bwd, both


def is_link(feats, i):
    subclasses = feats.attrs.get("subclass")
    return (rule(feats, "subclass", i) == "link"
            or (subclasses is not None and str(subclasses[i]) == "link"))


def invalidate(feats):
    """Enrichment can arrive after specs were requested; discard derived facts."""
    for key in ("_road_specs", "_bridge_specs", "_tunnel_specs", "_generated_lanes",
                "_raw_level_ranges", "_level_ranges", "_ranges_state", "_bnge_cell"):
        feats.__dict__.pop(key, None)


def _project(line, points):
    delta = np.diff(line, axis=0)
    length = np.maximum(np.linalg.norm(delta, axis=1), 1e-9)
    t = np.clip(((points[:, None] - line[None, :-1]) * delta).sum(2) / length**2, 0., 1.)
    q = line[None, :-1] + t[:, :, None] * delta
    dist = np.linalg.norm(points[:, None] - q, axis=2)
    k = dist.argmin(1)
    return dist[np.arange(len(points)), k], delta[k] / length[k, None]


def join(feats, tags, ids):
    """Keep highway evidence separate from osmtags' legacy named-street fallback."""
    n = len(feats)
    counts = np.array([count(v) or np.nan for v in feats.attrs.get("osm_lanes", [None] * n)])
    sources = ["osm-way" if np.isfinite(v) else "" for v in counts]
    matched = np.array([w or 0 for w in ids], dtype=np.int64)
    classes = feats.attrs.get("class", [""] * n)
    origin = feats.lonlat[0] if len(feats.lonlat) else np.zeros(2)
    scale = np.array([111320. * np.cos(np.radians(origin[1])), 110574.])
    candidates = []
    for wid, tag in tags.items():
        p, lanes, cls = tag.get("_coords"), count(tag.get("lanes")), tag.get("_highway", "")
        if lanes is None or p is None or len(p) < 2 or cls.removesuffix("_link") not in HIGHWAYS:
            continue
        p = (np.asarray(p) - origin) * scale
        p = p[np.r_[True, np.linalg.norm(np.diff(p, axis=0), axis=1) > .01]]
        if len(p) < 2:
            continue
        ow = str(tag.get("osm_oneway", ""))
        direction = -1 if ow == "-1" else 1 if ow in ("yes", "1", "true") else 0
        candidates.append((wid, lanes, cls.removesuffix("_link"), cls.endswith("_link"), direction,
                           p, p.min(0) - 6., p.max(0) + 6.))
    for i in range(n):
        if classes[i] not in HIGHWAYS:
            continue
        counts[i] = count(tags.get(ids[i], {}).get("lanes")) or np.nan
        sources[i] = "osm-way" if np.isfinite(counts[i]) else ""
        if sources[i] or not candidates:
            continue
        parts = feats.parts(i)
        if len(parts) != 1 or len(parts[0]) != 1:
            continue
        line = (parts[0][0] - origin) * scale
        if len(line) < 2:
            continue
        line = line[np.r_[True, np.linalg.norm(np.diff(line, axis=0), axis=1) > .01]]
        if len(line) < 2:
            continue
        s = np.r_[0., np.cumsum(np.linalg.norm(np.diff(line, axis=0), axis=1))]
        samples = np.column_stack([np.interp(np.linspace(0., s[-1], 9), s, line[:, d]) for d in (0, 1)])
        _, tangent = _project(line, samples)
        direction = rule(feats, "oneway", i)
        direction = 0 if direction is None else int(np.sign(direction))
        matches = []
        for wid, lanes, cls, link, od, p, lo, hi in candidates:
            if (cls != classes[i] or link != is_link(feats, i) or bool(od) != bool(direction)
                    or np.any(samples < lo) or np.any(samples > hi)):
                continue
            distance, heading = _project(p, samples)
            alignment = (tangent * heading).sum(1)
            alignment = alignment * direction * od if direction else np.abs(alignment)
            if distance.max() <= 6. and distance.mean() <= 3. and alignment.min() >= .9:
                matches.append((float(distance.mean()), wid, lanes))
        matches.sort()
        if matches and not any(m[2] != matches[0][2] and m[0] < matches[0][0] + 1. for m in matches[1:]):
            _, matched[i], counts[i] = matches[0]
            sources[i] = "osm-geometry"
    feats.attrs["lane_count"] = counts
    feats.attrs["lane_source"] = np.array(sources, dtype="<U16")
    feats.attrs["lane_osm_way"] = matched
    invalidate(feats)


def continuations(feats, eligible):
    """Unique reciprocal matches at aligned ends, respecting flow and connectors."""
    classes = feats.attrs.get("class", [""] * len(feats))
    origin = feats.lonlat[0] if len(feats.lonlat) else np.zeros(2)
    scale = np.array([111320. * np.cos(np.radians(origin[1])), 110574.])
    ends, buckets = {}, {}
    connectors = feats.rules.get("connectors")
    for i in sorted(eligible):
        parts = feats.parts(i)
        if len(parts) != 1 or len(parts[0]) != 1:
            continue
        line = (parts[0][0] - origin) * scale
        if len(line) < 2:
            continue
        line = line[np.r_[True, np.linalg.norm(np.diff(line, axis=0), axis=1) > .01]]
        if len(line) < 2:
            continue
        for end, other in ((0, 1), (1, -2)):
            at = 0 if end == 0 else -1
            tangent = line[other] - line[at]
            tangent /= np.linalg.norm(tangent)
            ids = frozenset(str(connectors.value[r]) for r in connectors.rows(i)
                            if abs(float(connectors.start[r]) - end) < .001) if connectors else frozenset()
            direction = rule(feats, "oneway", i)
            flow = (0 if direction is None else int(np.sign(direction))) * (1 if end else -1)
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
                    if (a[0] != b[0] and classes[a[0]] == classes[b[0]] and flow == -other_flow
                            and not (ids and other_ids and ids.isdisjoint(other_ids))
                            and np.linalg.norm(p - q) <= .75 and tangent @ heading < -.9):
                        near.append(b)
        if len(near) == 1:
            choices[a] = near[0]
    return {a: b for a, b in choices.items() if choices.get(b) == a}


def bounded_components(candidates, joins):
    """Missing chains with exactly two known ends; no loops, dead ends or forks."""
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
    """Validated mapped counts and bounded highway gaps; defaults stay in roads."""
    saved = feats.__dict__.get("_generated_lanes")
    if saved is not None:
        return saved
    n = len(feats)
    counts = [count(v) for v in feats.attrs.get("lane_count", feats.attrs.get("osm_lanes", [None] * n))]
    columns = [feats.attrs.get(k, [None] * n) for k in
               ("osm_lane_total", "osm_lanes_forward", "osm_lanes_backward", "osm_lanes_both_ways")]
    for i in range(n):
        total = count(columns[0][i])
        if total is not None:
            counts[i] = total
        elif counts[i] is None:
            fwd = count(columns[1][i], 0)
            bwd = count(columns[2][i], 0)
            both = count(columns[3][i], 0) or 0
            direction = rule(feats, "oneway", i)
            if direction is None and fwd is not None and bwd is not None:
                counts[i] = count(fwd + bwd + both)
            elif direction is not None:
                counts[i] = count(fwd if direction > 0 else bwd)
    classes = feats.attrs.get("class", [""] * n)
    rings = feats.attrs.get("osm_junction", [""] * n)
    eligible = {i for i in range(n) if classes[i] in HIGHWAYS and not is_link(feats, i)
                and rings[i] not in ("roundabout", "circular")}
    missing = {i for i in eligible if counts[i] is None}
    if missing:
        for component, outside in bounded_components(missing, continuations(feats, eligible)):
            agreed = counts[outside[0]]
            if agreed is not None and agreed == counts[outside[1]]:
                for i in component:
                    counts[i] = agreed
    import rampoverrides
    for i, lanes in rampoverrides.lane_counts(feats).items():
        counts[i] = lanes
    feats._generated_lanes = counts
    return counts
