"""Check 3, Order: each crossing's upper and lower recomputed, the levels consistent with them, the sign with the
member's kind, and the decisions consistent with the joins (a road is continuous: its crossings, its joins and the grade
law along it must have heights)."""
import math
from collections import deque

import numpy as np

from ix import artifacts
from ixcheck import geom

SIGN = {"deck": 1, "rail_deck": 1, "tunnel": -1, "ground": 0, "rail": 0}
GRADE_ROAD = 0.08   # a road's samples never climb steeper (fixed here, as check 6 has it)
GRADE_RAIL = 0.05   # a track's
MAX_LOOPS = 20      # loops named at most


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

    out += _weld_loops(art, parents, ms)

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


def _weld_loops(art, parents, ms):
    """A finding per loop the crossings make with the joins: the crossings' clearances (the upper at least `need` over the
    lower), the joins (one height) and the grade law along each member between those points (the heights free, the ground's
    too) cannot all hold. A loop with a decision from the layers alone in it (two members of one sign) is a fail: the order
    step gives those up; a loop of decisions of the structure only (members of different signs) is a note: the heights step
    eases the clearance. Each loop found is eased in the search alone, so that those after it are found too."""
    ext = parents.get(2)
    system = next((s for s in ext.systems if s.key == art.system), None) if ext is not None else None
    joins = system.joins if system is not None else ()
    pts, n = {}, [0]

    def node(i, s):
        pts.setdefault(i, []).append((float(s), n[0]))
        n[0] += 1
        return n[0] - 1
    cross = [(node(c.u, c.su), node(c.d, c.sd)) for c in art.crossings if 0 <= c.u < len(ms) and 0 <= c.d < len(ms)]
    welds = [(node(j.a, j.sa), node(j.b, j.sb)) for j in joins if 0 <= j.a < len(ms) and 0 <= j.b < len(ms)]
    eu, ev, ew = [], [], []

    def add(u, v, w):
        eu.append(u)
        ev.append(v)
        ew.append(float(w))
        return len(eu) - 1
    for i, lst in pts.items():
        g = GRADE_RAIL if ms[i].kind in ("rail", "rail_deck") else GRADE_ROAD
        lst.sort()
        for (s0, a), (s1, b) in zip(lst, lst[1:]):
            add(a, b, g * (s1 - s0))
            add(b, a, g * (s1 - s0))
    for a, b in welds:
        add(a, b, 0.0)
        add(b, a, 0.0)
    xs = [c for c in art.crossings if 0 <= c.u < len(ms) and 0 <= c.d < len(ms)]
    edge_of = {add(a, b, -float(c.need)): c for (a, b), c in zip(cross, xs)}
    out = []
    for cyc in _loops(n[0], eu, ev, ew, MAX_LOOPS):
        inx = [edge_of[k] for k in cyc if k in edge_of]
        short = -math.fsum(ew[k] for k in cyc)
        layer = [c for c in inx if art.sign.get(str(c.u)) == art.sign.get(str(c.d))]
        names = ", ".join("%s over %s" % (_key(ms, c.u), _key(ms, c.d)) for c in inx[:4]) + (", ..." if len(inx) > 4 else "")
        out.append(_f(art, "fail" if layer else "note", "weld_cycle",
                      "%d crossing decisions (%s) cannot hold with the joins and the grade law: %.2f m short%s"
                      % (len(inx), names, short, ", with a decision of the layers alone in it" if layer else
                         " (the structure's own: the heights step eases the clearance)"),
                      tuple((c.u, c.su) for c in inx)))
    return out


def _loops(n, eu, ev, ew, most):
    """The negative cycles of the graph (edge u -> v of weight w: z_v - z_u <= w), each as its edge indices in order, found
    one at a time by label correcting (the queue in node order: the same loops on every run); each is eased equally over its
    edges that carry a negative weight before the search goes on. The weights are changed in place."""
    adj = [[] for _ in range(n)]
    for k, u in enumerate(eu):
        adj[u].append(k)
    dist, pred, inq = [0.0] * n, [-1] * n, [True] * n
    queue = deque(range(n))
    since, every, found = 0, max(n, 2000), []
    while queue and len(found) < most:
        u = queue.popleft()
        inq[u] = False
        for k in adj[u]:
            v = ev[k]
            nd = dist[u] + ew[k]
            if nd < dist[v] - 1e-7:
                dist[v], pred[v] = nd, k
                since += 1
                if not inq[v]:
                    inq[v] = True
                    queue.append(v)
        if since >= every:
            since = 0
            cyc = _pred_cycle(n, pred, eu)
            if cyc is None:
                continue
            short = -math.fsum(ew[k] for k in cyc)
            if short > 1e-6:
                found.append(cyc)
                neg = [k for k in cyc if ew[k] < 0.0] or cyc
                for k in neg:
                    ew[k] += (short + 1e-6) / len(neg)
                for k in cyc:
                    if not inq[eu[k]]:
                        inq[eu[k]] = True
                        queue.append(eu[k])
            for k in cyc:
                pred[ev[k]] = -1
    return found


def _pred_cycle(n, pred, eu):
    stamp = [0] * n
    for s in range(n):
        if stamp[s]:
            continue
        v = s
        while v != -1 and stamp[v] == 0:
            stamp[v] = s + 1
            k = pred[v]
            v = eu[k] if k >= 0 else -1
        if v != -1 and stamp[v] == s + 1:
            cyc, u = [], v
            while True:
                k = pred[u]
                cyc.append(k)
                u = eu[k]
                if u == v:
                    break
            cyc.reverse()
            return cyc
    return None
