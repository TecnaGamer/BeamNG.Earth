"""Bridge cross sections inherit their actual, aligned approach roads."""
import numpy as np


def match(feats, decks, ground):
    import roads
    out = list(decks)
    lines, ends, buckets = {}, [], {}
    origin = feats.lonlat[0] if len(feats.lonlat) else np.zeros(2)
    scale = np.array([111320. * np.cos(np.radians(origin[1])), 110574.])
    for i, (deck, road) in enumerate(zip(decks, ground)):
        sp = road or deck
        if sp is None:
            continue
        parts = feats.parts(i)
        if len(parts) != 1 or len(parts[0]) != 1:
            continue
        p = (parts[0][0] - origin) * scale
        p = p[np.r_[True, np.linalg.norm(np.diff(p, axis=0), axis=1) > .01]]
        if len(p) < 2:
            continue
        lines[i] = p
        if deck is not None and road is not None:
            # A bridge interval within one feature is the same cross section as
            # its ground intervals, including measured widths and urban margins.
            out[i] = road._replace(bridge_ends=None)
        for end, other in ((0, 1), (-1, -2)):
            tangent = p[other] - p[end]
            tangent /= np.linalg.norm(tangent)
            connectors = feats.rules.get('connectors')
            ids = frozenset(str(connectors.value[r]) for r in connectors.rows(i)
                            if abs(float(connectors.start[r]) - (1. if end else 0.)) < .001) if connectors else frozenset()
            key = tuple(np.floor(p[end]).astype(int))
            buckets.setdefault(key, []).append(len(ends))
            ends.append((i, end, p[end], tangent, sp, ids))
    connected, approaches = {}, {}
    for i, end, p, tangent, sp, ids in ends:
        if decks[i] is None or ground[i] is not None:
            continue
        near = []
        key = np.floor(p).astype(int)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for e in buckets.get((key[0] + dx, key[1] + dy), ()):
                    j, je, q, heading, other, jids = ends[e]
                    if (i == j or sp.cls != other.cls or sp.kind != other.kind or sp.link != other.link
                            or sp.oneway != other.oneway or (sp.lanes and other.lanes and sp.lanes != other.lanes)
                            or (ids and jids and ids.isdisjoint(jids))
                            or np.linalg.norm(p - q) > .75 or tangent @ heading > -.9):
                        continue
                    if sp.oneway and (sp.dir or 1) * (1 if end else -1) != -(other.dir or 1) * (1 if je else -1):
                        continue
                    near.append((j, other))
        if len(near) == 1:
            j, other = near[0]
            if ground[j] is not None:
                approaches[(i, end)] = other.width
            else:
                connected.setdefault(i, set()).add(j)
                connected.setdefault(j, set()).add(i)
    seen = set()
    for i, sp in enumerate(out):
        if sp is None or ground[i] is not None or i in seen or i not in lines:
            continue
        component, pending = set(), [i]
        while pending:
            j = pending.pop()
            if j in component:
                continue
            component.add(j)
            pending.extend(connected.get(j, ()))
        seen.update(component)
        widths = [w for (j, _), w in approaches.items() if j in component]
        if not widths:
            continue
        common = max(widths)
        for j in component:
            a, b = (approaches.get((j, end), common) for end in (0, -1))
            out[j] = out[j]._replace(width=common, bridge_ends=(a, common, b))
    return out


def at(spec, distance, length):
    """Pavement width along a whole bridge part, with smooth endpoint tapers."""
    s = np.asarray(distance, float)
    if not spec.bridge_ends:
        return np.full(s.shape, spec.width)
    a, middle, b = spec.bridge_ends
    reach = max(.01, min(60., length / 2))
    def ease(t):
        t = np.clip(t, 0., 1.)
        return t * t * (3. - 2. * t)
    return middle + (a - middle) * (1 - ease(s / reach)) + (b - middle) * (1 - ease((length - s) / reach))


def nodes(spec, source, points):
    """Width at clipped/resampled points, projected onto the complete source."""
    p, q = np.asarray(source)[:, :2], np.asarray(points)[:, :2]
    d = np.diff(p, axis=0)
    length = np.linalg.norm(d, axis=1)
    t = np.clip(((q[:, None] - p[:-1]) * d).sum(2) / np.maximum(length**2, 1e-12), 0, 1)
    dist = np.linalg.norm(q[:, None] - (p[:-1] + t[..., None] * d), axis=2)
    k = dist.argmin(1)
    s = np.r_[0., np.cumsum(length)]
    return at(spec, s[k] + t[np.arange(len(q)), k] * length[k], s[-1])


def extras(spec, pts, lon, left_hand=False):
    """Move paint with the tapered pavement; keep paint thickness and dash phase."""
    import roads
    base = roads.extras(spec, pts, lon, left_hand)
    if not spec.bridge_ends:
        return base
    wider = roads.extras(spec._replace(width=spec.width + 1.), pts, lon, left_hand)
    if len(base) != len(wider):
        return base
    delta = (nodes(spec, pts, pts) - spec.width)[:, None]
    result = []
    for (look, width, line), (_, _, expanded) in zip(base, wider):
        moved = line + (expanded - line) * delta
        if '_source' in look:
            look = dict(look, _source=moved)
        result.append((look, width, moved))
    return result
