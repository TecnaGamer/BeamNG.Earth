"""The original Python lane evidence (roadlanes.generated_counts with its continuations and bounded_components) and
lane flow glue (roads._lane_flow_rows / _lane_flow_local, with the per-feature _home_cell of _lane_flow_home), kept
only as an independent test oracle of their Rust ports (earthfast generated_lanes / lane_flow_cells, 2026-11-08).
Nothing here is cached on the features."""
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


def is_link(feats, i):
    subclasses = feats.attrs.get("subclass")
    return (rule(feats, "subclass", i) == "link"
            or (subclasses is not None and str(subclasses[i]) == "link"))


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
    """Validated mapped counts and bounded highway gaps; defaults stay in roads (not remembered)."""
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
    return counts


def lane_flow_rows(f, counts):
    """roads._lane_flow_rows of layer f with its generated counts: (ids, first coordinate, end coordinate, attrs)."""
    import roads
    n = len(f)
    ids = f.ids.tolist()
    po = np.asarray(f.part_off, np.int64)
    starts = np.asarray(f.coord_off, np.int64)[np.asarray(f.ring_off, np.int64)[po[:n]]]
    ends = np.asarray(f.coord_off, np.int64)[np.asarray(f.ring_off, np.int64)[po[1:n + 1]]]
    attrs = np.zeros((n, 4))
    attrs[:, 1] = np.nan
    attrs[:, 3] = np.nan
    cls_a, sub_a = f.attrs.get("class"), f.attrs.get("subtype")
    for i in range(n):
        if cls_a is None or str(cls_a[i]) not in roads.LANE_FLOW_CLASSES or (sub_a is not None
                                                                            and str(sub_a[i]) != "road"):
            continue
        v = roads._rule_value(f.rules.get("oneway"), i)
        if v is None:
            continue
        attrs[i, 0] = -1.0 if float(v) < 0 else 1.0
        lanes = counts[i]
        if lanes is not None:
            attrs[i, 1] = float(int(lanes))
        attrs[i, 2] = 1.0 if is_link(f, i) else 0.0
        c = str(cls_a[i])
        if c not in roads._LANE_FLOW_DFLT:
            roads._LANE_FLOW_DFLT[c] = roads.lane_layout(roads.Spec(roads.carriageway(c, "paved", False, None, True,
                                                                                      None), "paved", False, c, True))[1]
        attrs[i, 3] = float(roads._LANE_FLOW_DFLT[c])
    return ids, starts, ends, attrs


def lane_flow_local(n_feats, layers):
    """roads._lane_flow_local's solve: layers [(rows of lane_flow_rows, ids as a str array, lonlat)], the cell's own
    first (n_feats roads); per road of it its count or None."""
    import fast
    import roads
    id_arrays = [ida for _rows, ida, _ll in layers]
    keep = np.zeros(sum(len(a) for a in id_arrays), bool)
    if len(keep):
        _u, first = np.unique(np.concatenate(id_arrays), return_index=True)
        keep[first] = True
    coords, lens, attrs, mine = [], [], [], None
    base = 0
    for k, ((ids, starts, ends, at), ida, lonlat) in enumerate(layers):
        sel = np.flatnonzero(keep[base:base + len(ida)]).astype(np.int64)
        base += len(ida)
        if k == 0 and mine is None:
            mine = sel
        if not len(sel):
            continue
        a, n_ = starts[sel], ends[sel] - starts[sel]
        tot = int(n_.sum())
        if tot:
            first = np.concatenate([[0], np.cumsum(n_)[:-1]])
            coords.append(lonlat[np.repeat(a - first, n_) + np.arange(tot)])
        lens.append(n_)
        attrs.append(at[sel])
    out = [None] * n_feats
    if lens:
        n_all = np.concatenate(lens)
        off = np.concatenate([[0], np.cumsum(n_all)]).astype(np.int64)
        at_all = np.ascontiguousarray(np.concatenate(attrs), np.float64)
        if off[-1]:
            got = np.asarray(fast._ef.lane_flow(np.ascontiguousarray(np.concatenate(coords), np.float64), off, at_all,
                                                float(roads.LANE_FLOW_MAX)))
            for r, i in enumerate((mine if mine is not None else np.zeros(0, np.int64)).tolist()):
                x = got[r]
                out[i] = None if not np.isfinite(x) or at_all[r, 2] > 0.5 else int(x)
    return out


def home_cell(feats, i):
    """roads._home_cell without its memo."""
    import mapcells
    a = feats.coord_off[feats.ring_off[feats.part_off[i]]]
    return str(mapcells.cell_of(float(feats.lonlat[a, 1]), float(feats.lonlat[a, 0])))
