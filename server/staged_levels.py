"""Stage 2 of the staged grade solver: at every crossing of two paths, which is above (see staged_paths for the whole).

Input: stage 1's Paths. Output: Levels: the crossings (two segments crossing in plan away from either's ends, a path with
itself included: a spiral crossing itself is two places on one path), each with the upper and the lower part, who leaves
the ground (a provisional answer: findground without the elevation), and the order graph over the paths' spans.

Nothing is decided here that is not decided there: which is above is gradeshapes._over (the map's kinds, a bridge over
what is on the ground, that over a tunnel, then the map's levels, OSM's layer, within a kind) and who leaves the ground is
findground.leaves (a tunnel under anything goes down, a bridge over a plain way goes up unless the cost law, with the lack
the elevation leaves, says the way goes down: that needs the elevation, which stage 4 reads, so here a bridge over a plain
way is "up" and `provisional` says the cost law has the last word). The crossings are gradeshapes._relations'
("cross", between two parts at different levels: a join is stage 1's), so the staged solver and the shapes see the same
places.

The order graph: a span is a path's stretch of one kind and level (a place: staged_paths.Run); an edge lower -> upper per
crossing. Checks:
  level_tie     two structures cross with the same kind and level: nothing says which is over (gradeshapes._over: None;
                the system does not relate them and they would be built at one height); named, with both ways.
  order_cycle   a cycle in "above" between spans (A over B over C over A), reported with its crossings. The levels are a
                scalar per span, so a cycle cannot come from the map's kinds and layers alone: it means an order that came from
                elsewhere contradicts them (a caller's, or a future rule), which is why the graph is checked and not assumed.
  Notes: a path over itself (the spiral's own crossings), a pair of paths each over the other at different places (a
  weave: a loop ramp passing over and under its road), the crossings of plain ways at grade (no order).
What it does not decide: whether the levels fit the roads' lengths and grades (stage 4: a road tagged level 1 and level 2
in two parts and crossing itself 20 m from the joint is a contradiction of heights, not of order). Pure, deterministic."""
from collections import namedtuple

import findground
import gradeshapes as gs
import interchange_audit as ia
import staged_paths as sp

Finding = ia.Finding
Crossing = namedtuple("Crossing", "id upper su lower sd q by moves provisional span_u span_d")
# upper, lower: members; su, sd: arc lengths on them; q: the crossing's point; by: "kind" (a structure over what is on the
# ground, over a tunnel) or "level" (the map's layer within a kind); moves: "down" (the lower goes under), "up" (the upper
# rises), None (nothing moves: at grade, or a track's own); provisional: the cost law (with the elevation) decides it;
# span_u, span_d: (path id, run index)


class OrderGraph:
    """Nodes (spans), edges lower -> upper with what made them. Cycles by Tarjan's strongly connected components, iterative."""

    def __init__(self):
        self.adj = {}
        self.nodes = set()

    def add(self, lower, upper, ref):
        self.nodes |= {lower, upper}
        self.adj.setdefault(lower, []).append((upper, ref))

    def components(self):
        """The strongly connected components with more than one node, or one with an edge to itself: [[node]], sorted."""
        index, low, on, stack, out = {}, {}, set(), [], []
        counter = [0]
        for root in sorted(self.nodes):
            if root in index:
                continue
            work = [(root, iter(sorted(self.adj.get(root, ()), key=lambda e: (e[0], e[1]))))]
            index[root] = low[root] = counter[0]
            counter[0] += 1
            stack.append(root)
            on.add(root)
            while work:
                v, it = work[-1]
                advanced = False
                for w, _ref in it:
                    if w not in index:
                        index[w] = low[w] = counter[0]
                        counter[0] += 1
                        stack.append(w)
                        on.add(w)
                        work.append((w, iter(sorted(self.adj.get(w, ()), key=lambda e: (e[0], e[1])))))
                        advanced = True
                        break
                    if w in on:
                        low[v] = min(low[v], index[w])
                if advanced:
                    continue
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[v])
                if low[v] == index[v]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on.discard(w)
                        comp.append(w)
                        if w == v:
                            break
                    if len(comp) > 1 or any(w2 == v for w2, _r in self.adj.get(v, ())):
                        out.append(sorted(comp))
        return sorted(out)

    def cycle(self, comp):
        """One cycle of component comp as [(from node, to node, ref)] (the shortest through its first node, by BFS)."""
        inside = set(comp)
        start = comp[0]
        prev = {start: None}
        queue = [start]
        for v in queue:
            for w, ref in sorted(self.adj.get(v, ()), key=lambda e: (e[0], str(e[1]))):
                if w not in inside:
                    continue
                if w == start:
                    path = [(v, w, ref)]
                    while prev[v] is not None:
                        u, r = prev[v]
                        path.append((u, v, r))
                        v = u
                    return path[::-1]
                if w not in prev:
                    prev[w] = (v, ref)
                    queue.append(w)
        return []


class Levels:
    """Stage 2's answer."""

    def __init__(self, P):
        self.P = P
        self.crossings = []
        self.graph = OrderGraph()
        self.at_grade = []          # (member, member, q): two ways on the ground crossing with nothing to order them (not computed
        #                             for plain ways: a crossing of two on the ground is a junction the map has no node for)
        self.ties = []
        self.network = 0            # crossings with a tunnel longer than TUNNEL_MAX_M (tunnels.py's own): left out, as gradeshapes


def _span(P, i, s):
    pid, pos = P.at(i, s)
    p = P.paths[pid]
    runs = [k for k, r in enumerate(p.runs) if r.s0 - 1e-6 <= pos <= r.s1 + 1e-6]
    # (a point on the boundary of two runs: the run of the segment itself)
    for k in runs:
        r = p.runs[k]
        if r.first <= [j for j, (m, _f) in enumerate(p.segs) if m == i][0] <= r.last:
            return (pid, k)
    return (pid, runs[0])


def build(P):
    """The crossings of P's structures with what they cross, who is over, who moves, the order graph: Levels."""
    net = P.net
    L = Levels(P)
    for i in sorted(P.rel, key=lambda i: sp._key(net, i)):
        for j, kind, si, sj, q in sorted(P.rel[i], key=lambda r: (sp._key(net, r[0]), r[2], r[3])):
            if kind != "cross" or not sp.usable(net, j):
                continue
            if gs._tunnel_network(net, j) or gs._tunnel_network(net, i):
                L.network += 1
                continue
            if j in P.rel and j < i:
                continue            # (a crossing of two structures is found from each: once)
            mi, mj = net.m[i], net.m[j]
            o = gs._over(mi, mj)
            if o is None:
                L.ties.append((i, si, j, sj, q))
                continue
            u, su, d, sd = (i, si, j, sj) if o == "i" else (j, sj, i, si)
            mu, md = net.m[u], net.m[d]
            by = "kind" if gs.LEVEL_BAND[mu.kind] != gs.LEVEL_BAND[md.kind] else "level"
            who = findground.leaves(gs.WAY[md.kind], gs.WAY[mu.kind])
            provisional = False
            if who == "lower":
                moves = "down"
            elif who == "upper" and mu.kind == "deck":
                moves = "up"
                provisional = md.kind == "ground"
            else:
                moves = None
            su_, sd_ = _span(P, u, su), _span(P, d, sd)
            c = Crossing(len(L.crossings), u, float(su), d, float(sd), q, by, moves, provisional, su_, sd_)
            L.crossings.append(c)
            L.graph.add(sd_, su_, c.id)
    return L


def _tie_text(namer, i, si, j, sj, q):
    same = gs._road_of(namer.net.m[i]) == gs._road_of(namer.net.m[j])
    m = namer.net.m[i]
    return ("%s and %s cross at %s at the same %s%s: nothing says which is over"
            % (namer.label(i), namer.label(j), namer.point(q), "kind and level %g" % m.level if m.kind in sp.STRUCT else "level",
               " (one road's own parts)" if same else ""))


def check(L, namer):
    """Stage 2's checks: ([Finding], stats). Fails: level_tie, order_cycle. Notes: path_over_itself, weave."""
    P = L.P
    net = P.net
    finds = []
    for i, si, j, sj, q in L.ties:
        where = namer.point(q)
        finds.append(Finding(2, "level_tie", "fail", _tie_text(namer, i, si, j, sj, q), where, [namer.info(i), namer.info(j)]))
    for comp in L.graph.components():
        cyc = L.graph.cycle(comp)
        legs = []
        refs = []
        for a, b, cid in cyc:
            c = L.crossings[cid]
            legs.append("%s over %s at %s" % (namer.label(c.upper, c.su), namer.label(c.lower, c.sd), namer.point(c.q)))
            refs += [namer.info(c.upper), namer.info(c.lower)]
        where = namer.point(L.crossings[cyc[0][2]].q) if cyc else namer.point(P.joints[0].point)
        finds.append(Finding(2, "order_cycle", "fail", "a cycle in 'above' through %d spans of %d paths: %s"
                             % (len(comp), len({n[0] for n in comp}), "; ".join(legs)), where, refs))
    by_pair = {}
    self_over = []
    for c in L.crossings:
        a, b = c.span_u[0], c.span_d[0]
        if a == b:
            self_over.append(c)
        else:
            by_pair.setdefault((min(a, b), max(a, b)), set()).add((a, b))
    for c in self_over:
        finds.append(Finding(2, "path_over_itself", "note",
                             "path %d is over itself: %s over %s at %s (%s)" % (c.span_u[0], namer.label(c.upper, c.su),
                                                                               namer.label(c.lower, c.sd), namer.point(c.q),
                                                                               "the same span" if c.span_u == c.span_d else
                                                                               "two spans of one path"),
                             namer.point(c.q), [namer.info(c.upper), namer.info(c.lower)]))
    weaves = {k for k, v in by_pair.items() if len(v) == 2}
    for a, b in sorted(weaves):
        cs = [c for c in L.crossings if {c.span_u[0], c.span_d[0]} == {a, b}]
        c0 = cs[0]
        finds.append(Finding(2, "weave", "note", "paths %d and %d are each over the other at different places (%s, then %s)"
                             % (a, b, namer.point(cs[0].q), namer.point(next(c for c in cs if c.span_u[0] != c0.span_u[0]).q)),
                             namer.point(c0.q), [namer.info(c0.upper), namer.info(c0.lower)]))
    # a road whose parts carry different levels where they meet: one road going on from one level to another (named with the map's
    # layer for the whole road: where the two disagree the levels are the map's local ranges, stage 4 says whether they fit)
    splits = 0
    for p in P.paths:
        for (i, _fi), (j, _fj) in zip(p.segs, p.segs[1:]):
            mi, mj = net.m[i], net.m[j]
            if gs._road_of(mi) == gs._road_of(mj) and mi.kind == mj.kind and mi.kind in sp.STRUCT and mi.level != mj.level:
                splits += 1
                md = namer.md(i)
                q = net.lines[i][-1 if p.segs[[s_ for s_, _f in p.segs].index(i)][1] else 0]
                finds.append(Finding(2, "level_split", "note",
                                     "one road goes on from level %g to level %g where its parts meet: %s then %s (the map's layer for "
                                     "the road: %s)" % (mi.level, mj.level, namer.label(i), namer.label(j), md.get("layer")),
                                     namer.point(q), [namer.info(i), namer.info(j)]))
    moves, bykind = {}, {}
    for c in L.crossings:
        moves[c.moves] = moves.get(c.moves, 0) + 1
        k = "%s over %s" % (net.m[c.upper].kind, net.m[c.lower].kind)
        bykind[k] = bykind.get(k, 0) + 1
    stats = dict(crossings=len(L.crossings), ties=len(L.ties), spans=len(L.graph.nodes), edges=sum(len(v) for v in L.graph.adj.values()),
                 cycles=len(L.graph.components()), self_over=len(self_over), weaves=len(weaves), level_splits=splits, tunnel_network=L.network,
                 moves={str(k): v for k, v in sorted(moves.items(), key=lambda kv: str(kv[0]))},
                 by_kind=dict(sorted(bykind.items())), fails=sum(1 for f in finds if f.level == "fail"))
    return finds, stats
