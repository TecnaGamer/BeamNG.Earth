"""Check 3, Order: each crossing's upper and lower recomputed, the levels consistent with them, the sign with the
member's kind."""
import numpy as np

from ix import artifacts
from ixcheck import geom

SIGN = {"deck": 1, "rail_deck": 1, "tunnel": -1, "ground": 0, "rail": 0}


def check(art, parents):
    if not isinstance(art, artifacts.Order):
        return []
    out = []
    paths = parents.get(1)
    ms = paths.members if paths is not None else []
    lines = [np.asarray(m.line, np.float64) for m in ms]

    for c in art.crossings:
        if not (0 <= c.u < len(lines) and 0 <= c.d < len(lines) and len(lines[c.u]) and len(lines[c.d])):
            out.append(_f(art, "fail", "bad_crossing", "a crossing names members %d and %d of %d"
                          % (c.u, c.d, len(lines))))
            continue
        pa, pb = geom.at_s(lines[c.u], c.su), geom.at_s(lines[c.d], c.sd)
        d, dq = geom.dist(pa, pb), geom.dist(pa, np.asarray(c.q, np.float64))
        if max(d, dq) > geom.CROSS_M:
            out.append(_f(art, "fail", "bad_crossing",
                          "crossing of %s at s=%.1f and %s at s=%.1f: the points %.1f m apart, q %.1f m off, the "
                          "agreement is %s m" % (_key(ms, c.u), c.su, _key(ms, c.d), c.sd, d, dq, geom.CROSS_M),
                          ((c.u, c.su), (c.d, c.sd))))
        lu, ld = art.level.get(str(c.u)), art.level.get(str(c.d))
        if lu is None or ld is None or not lu > ld:
            out.append(_f(art, "fail", "bad_level", "crossing of %s over %s: the level of %s (%s) is not above %s "
                          "(%s)" % (_key(ms, c.u), _key(ms, c.d), _key(ms, c.u), lu, _key(ms, c.d), ld),
                          ((c.u, c.su), (c.d, c.sd))))
        if not (np.isfinite(c.need) and c.need > 0.0):
            out.append(_f(art, "fail", "bad_need", "crossing of %s over %s asks %r m of clearance"
                          % (_key(ms, c.u), _key(ms, c.d), c.need), ((c.u, c.su), (c.d, c.sd))))

    loop = _cycle([(c.u, c.d) for c in art.crossings if 0 <= c.u < len(ms) and 0 <= c.d < len(ms)])
    if loop:
        keys = [_key(ms, i) for i in loop]
        out.append(_f(art, "fail", "order_cycle", "the below-relation cycles: " + " -> ".join(keys + keys[:1]),
                      tuple((i, 0.0) for i in loop)))

    for k, v in art.sign.items():
        try:
            i = int(k)
        except ValueError:
            i = -1
        if not (0 <= i < len(ms)):
            out.append(_f(art, "fail", "bad_sign", "the sign names member %s of %d" % (k, len(ms))))
        elif ms[i].kind in SIGN and v != SIGN[ms[i].kind]:
            out.append(_f(art, "fail", "bad_sign", "the sign of %s is %r, a %s is %+d"
                          % (ms[i].key, v, ms[i].kind, SIGN[ms[i].kind]), ((i, 0.0),)))
    return out


def _f(art, level, code, text, where=()):
    return artifacts.Finding(art.meta.step, level, code, text, where)


def _key(ms, i):
    return str(ms[i].key) if 0 <= i < len(ms) else str(i)


def _cycle(edges):
    """A loop of the below-relation [u, ..., the member over u] or None, by depth-first search from the lowest member
    up: the same loop on every run."""
    adj = {}
    for u, d in edges:
        adj.setdefault(u, set()).add(d)
        adj.setdefault(d, set())
    color = {n: 0 for n in adj}
    for root in sorted(adj):
        if color[root]:
            continue
        parent, stack = {root: None}, [(root, iter(sorted(adj[root])))]
        color[root] = 1
        while stack:
            v, it = stack[-1]
            w = next(it, None)
            if w is None:
                color[v] = 2
                stack.pop()
                continue
            if w not in color:
                continue
            if color[w] == 1:                       # w is on the way up: the loop runs w .. v and back to w
                loop = [v]
                while v != w:
                    v = parent[v]
                    loop.append(v)
                return loop[::-1]
            parent[w] = v
            color[w] = 1
            stack.append((w, iter(sorted(adj[w]))))
    return None
