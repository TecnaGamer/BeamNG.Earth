"""Stage 3 of the staged grade solver: a point on the ground for every path, and the systems cut where the level resets
(see staged_paths for the whole).

Input: stage 1's Paths, stage 2's Levels, and optionally the elevation (reader(net) -> (ground(xy), fixed(i, s)), the
pipeline's own: gradeshapes._Verdicts asks it who moves where a deck's rise and a road's dip are close). Output: Grounds.

Ground points. A path has a point found on the ground where
  segment    it has a ground segment (a road or a track on the ground: every point of it is the elevation's),
  at grade   an end of its structure meets a road on the ground (a ramp's foot, a bridge's abutment road, a tunnel's mouth),
  held       a deck the map has a road under in a short tunnel (gradeshapes Member.held: findground's tunnel rule: it stays at
             its ground, the road under it is dug).
A ground road point that the map puts over a tunnel's footprint (a street on a slab over a trench) is no point on the ground
  there: the terrain under a cut-and-cover tunnel's slab is the trench floor, 7 m under the street at Porte Maillot's LiDAR, and a
  street read at it would sink the tunnel (its ceiling is the street's height less the clearance). Such a point takes its height from
  the anchors round it along its own road (footprints, anchored): the nearest points of the road that are not over a tunnel,
  joined by the straight line. The road's own tunnel (the portal approach) is not a footprint it is over.
Those are the direct ones. A path with none is anchored through a joint: Dijkstra over the places paths meet (joints), the
distance along the paths to the nearest ground point; the joint chain is kept. A path no ground point reaches (an elevated
loop; a viaduct the map data was cut across) is reported with every path it is joined to: its heights have no datum.
(A bridge over a plain way is "up" and the way stays at its ground, so the way's ground segment is already a point: findground
asks nothing more here.)

Systems. The structures (road decks, short road tunnels) joined to one another, crossing one another, or digging the same road
(gradeshapes._shared_dips) are one system, as find_systems has them, except one thing: a road on the ground between two
structures' ends does not join them (find_systems' ramp rule, CONNECT_M: a road of up to 400 m between two decks' ends). A road on
the ground is at its ground, and where every path at a node is on the ground the level resets and one interchange ends; the
deck's end weighs toward that road's ground by itself. The link that holds a big system together is then a joint, a crossing
or a shared dip, and is named (the bridge of the system's graph, interchange_audit's search: the one link between two parts).
Each system is gradeshapes._system's own record (its crossings with who moves, joins, ends on the road on the ground, dips) so
the next stage and the comparison with today's read the same facts. Stage 3's checks:
  no_ground_point   a path of a system with no point on the ground and none through its joints (fail), named with the path;
  oversized_system  a system of more pieces than a place of this kind has (interchange_audit.OVERSIZE_PIECES), with the links
                    that hold it together named (fail: something really links them, and here is what).
Notes: paths anchored only through a joint; the ground roads the old rule would have merged through. Pure, deterministic."""
import heapq
import math
from collections import namedtuple

import numpy as np

import gradeshapes as gs
import interchange_audit as ia
import staged_paths as sp

Finding = ia.Finding
OVERSIZE_PIECES = ia.OVERSIZE_PIECES
GroundPoint = namedtuple("GroundPoint", "path pos member s why")      # why: "segment" | "at_grade" | "held"


class Grounds:
    """Stage 3's answer: ground points per path, their reach, the systems."""

    def __init__(self, P, L):
        self.P, self.L = P, L
        self.points = {}            # path id -> [GroundPoint]
        self.reach = {}             # path id -> m along the paths to the nearest ground point (inf: none)
        self.via = {}               # path id -> [joint index] the way to it (empty for a direct one)
        self.systems = []           # gradeshapes._system records, each with "meta": the staged facts
        self.merges = []            # (a, b, why, detail) in the order they were united (interchange_audit's trace)
        self.ramps = []             # (road, [seeds]): ground roads of up to CONNECT_M between two structures' ends (today's merge)
        self.net = P.net
        self.dig = None
        self.rel = {}


FOOTPRINT_STEP_M = 4.0      # m: a ground road is read along every this far for what lies under it
PORTAL_M = 30.0             # m: from a tunnel's end, a road is on its approach, not over it
MARGIN_M = 8.0              # m: the stretch each side of a footprint that is still the trench wall, no anchor (the trench is wider than the tunnel)


def footprints(net):
    """{ground member: [(s0, s1)]}: the stretches of a road on the ground that lie over a tunnel of the map (within its half width of
    the tunnel's line, further than PORTAL_M from its ends, the tunnel not of the road's own: gradeshapes._road_of). Cached on the net."""
    got = getattr(net, "_footprints", None)
    if got is not None:
        return got
    out = {}
    tunnels = [j for j in range(len(net.m)) if net.m[j].kind == "tunnel" and net.L[j] <= gs.TUNNEL_MAX_M]
    for i in range(len(net.m)):
        if net.m[i].kind != "ground" or not len(net.lines[i]) or not tunnels:
            continue
        k = max(2, int(math.ceil(net.L[i] / FOOTPRINT_STEP_M)) + 1)
        sv = np.linspace(0.0, net.L[i], k)
        xy = np.stack([np.interp(sv, net.s[i], net.lines[i][:, 0]), np.interp(sv, net.s[i], net.lines[i][:, 1])], axis=1)
        over = np.zeros(k, bool)
        for j in tunnels:
            if gs._road_of(net.m[i]) == gs._road_of(net.m[j]) or j == i:
                continue
            reach = float(net.m[j].half)
            near = gs._near_line(xy, np.asarray(net.lines[j], np.float64)[:, :2], reach)
            if not near.any():
                continue
            for q in np.flatnonzero(near).tolist():
                sj, _d = gs.proj(net.lines[j], net.s[j], xy[q])
                if PORTAL_M <= sj <= net.L[j] - PORTAL_M:
                    over[q] = True
        if over.any():
            spans, a = [], None
            for q in range(k):
                if over[q] and a is None:
                    a = q
                if a is not None and (not over[q] or q == k - 1):
                    b = q if over[q] else q - 1
                    spans.append((float(sv[a]) - MARGIN_M, float(sv[b]) + MARGIN_M))
                    a = None
            out[i] = spans
    net._footprints = out
    return out


def anchored(net, fixed_z):
    """fixed_z(i, s) with the ground roads' points over a tunnel's footprint taken from their anchors: where member i has stretches over
    a footprint, its height at s there is the straight line between the nearest points of the road outside any (the elevation's, as
    fixed_z gives it); a road with none outside keeps the elevation. Other members, and points outside, are fixed_z's own."""
    fp = footprints(net)
    if not fp:
        return fixed_z

    def fixed(i, s):
        spans = fp.get(int(i)) if np.ndim(i) == 0 else None
        if not spans:
            return fixed_z(i, s)
        sv = np.atleast_1d(np.asarray(s, np.float64))
        z = np.asarray(fixed_z(i, sv), np.float64).copy()
        inside = np.zeros(len(sv), bool)
        for a, b in spans:
            inside |= (sv > a) & (sv < b)
        if inside.any():
            k = max(2, int(math.ceil(net.L[i] / FOOTPRINT_STEP_M)) + 1)
            grid = np.linspace(0.0, net.L[i], k)
            free = np.ones(k, bool)
            for a, b in spans:
                free &= ~((grid > a) & (grid < b))
            if free.any():
                zg = np.asarray(fixed_z(i, grid[free]), np.float64)
                z[inside] = np.interp(sv[inside], grid[free], zg)
        return z if np.ndim(s) else float(z[0])
    return fixed


def _direct_points(P):
    """{path id: [GroundPoint]}: the points on the ground each path has of its own."""
    net = P.net
    out = {p.id: [] for p in P.paths}
    for p in P.paths:
        for r in p.runs:
            if r.kind in ("ground", "rail"):
                i, _f = p.segs[r.first]
                spans = footprints(net).get(i, ())
                if r.kind == "ground" and any(a <= 0.0 and b >= net.L[i] for a, b in spans):
                    continue                    # (a street wholly over a tunnel: the trench floor is not its ground)
                out[p.id].append(GroundPoint(p.id, r.s0, i, 0.0, "segment"))
            else:
                for k in range(r.first, r.last + 1):
                    i, f = p.segs[k]
                    if net.m[i].held:
                        out[p.id].append(GroundPoint(p.id, p.offset[i], i, 0.0, "held"))
    # (an end of a structure that meets a road on the ground: at grade there)
    for jt in P.joints:
        plain = [a for a in jt.ends if net.m[a.seg].kind not in sp.STRUCT] + [(j, s) for j, s in jt.through
                                                                              if net.m[j].kind not in sp.STRUCT]
        if not plain:
            continue
        for a in jt.ends:
            if net.m[a.seg].kind in sp.STRUCT:
                s = 0.0 if a.end == 0 else net.L[a.seg]
                pid, pos = P.at(a.seg, s)
                out[pid].append(GroundPoint(pid, pos, a.seg, s, "at_grade"))
    return out


def _reach(P, points):
    """Dijkstra from the ground points over the places paths meet: ({path id: m to the nearest ground point}, {path id: [joint
    indices of the way]}). A path's nodes: its joints (where it has an end or is passed through), in order along it."""
    net = P.net
    nodes = {}                  # (path, round pos) -> node id; joints are shared by every path at them
    on_path = {p.id: [] for p in P.paths}
    ids = []

    def node(key):
        if key not in nodes:
            nodes[key] = len(nodes)
            ids.append(key)
        return nodes[key]
    adj = {}
    for jx, jt in enumerate(P.joints):
        here = []
        for a in jt.ends:
            pid, pos = P.at(a.seg, 0.0 if a.end == 0 else net.L[a.seg])
            here.append((pid, pos))
        for j, s in jt.through:
            if sp.usable(net, j):
                here.append(P.at(j, s))
        jn = node(("j", jx))
        for pid, pos in here:
            on_path[pid].append((pos, jn))
    src = []
    for pid, gps in points.items():
        for g in gps:
            gn = node(("g", pid, round(g.pos, 3)))
            on_path[pid].append((g.pos, gn))
            src.append(gn)
    inside = []                 # joints inside a ground run: the road is on the ground there
    for p in P.paths:
        grs = [(r.s0, r.s1) for r in p.runs if r.kind in ("ground", "rail")]
        for pos, nd in on_path[p.id]:
            if ids[nd][0] == "j" and any(a - 1e-6 <= pos <= b + 1e-6 for a, b in grs):
                inside.append(nd)
    for pid, lst in on_path.items():
        lst.sort()
        for (a, na), (b, nb) in zip(lst, lst[1:]):
            w = b - a
            adj.setdefault(na, []).append((nb, w, pid))
            adj.setdefault(nb, []).append((na, w, pid))
    dist = {n: math.inf for n in range(len(ids))}
    prev = {}
    heap = []
    for n in sorted(set(src) | set(inside)):
        dist[n] = 0.0
        heap.append((0.0, n))
    heapq.heapify(heap)
    while heap:
        d, v = heapq.heappop(heap)
        if d > dist[v]:
            continue
        for w, wt, pid in adj.get(v, ()):
            if d + wt < dist[w] - 1e-9:
                dist[w] = d + wt
                prev[w] = v
                heapq.heappush(heap, (dist[w], w))
    reach, via = {}, {}
    for p in P.paths:
        best, bn = math.inf, None
        for pos, nd in on_path[p.id]:
            if dist[nd] < best:
                best, bn = dist[nd], nd
        if points.get(p.id):
            best, bn = 0.0, None
        reach[p.id] = best
        chain = []
        while bn is not None and bn in prev:
            bn = prev[bn]
            if ids[bn][0] == "j":
                chain.append(ids[bn][1])
        via[p.id] = chain
    return reach, via


def _is_seed(net, i):
    return gs._is_seed(net, i) and sp.usable(net, i)


def build(P, L, reader=None):
    """Grounds of P and L: the ground points, their reach, the systems. reader: the elevation as gradeshapes.find_systems is given
    it (None: flat ground, every clearance lacking in full)."""
    net = P.net
    G = Grounds(P, L)
    G.points = _direct_points(P)
    G.reach, G.via = _reach(P, G.points)
    members = net.m
    cand = [i for i in sorted(P.rel, key=lambda i: sp._key(net, i)) if _is_seed(net, i)]
    rel = {i: [r for r in P.rel[i] if not gs._tunnel_network(net, r[0])] for i in cand}
    crosses = {i for i in cand if any(kind == "cross" and gs._over(members[i], members[j]) is not None
                                      for j, kind, _a, _b, _q in rel[i])}
    # (two decks of one level the map cannot order that stage 2 ordered by their natural heights: crossings like the others)
    ordered = [c for c in L.crossings if c.by == "elevation" and c.moves is not None]
    crosses |= {i for c in ordered for i in (c.upper, c.lower) if i in set(cand)}
    dpar = {i: i for i in cand if members[i].kind == "deck"}

    def droot(i):
        while dpar[i] != i:
            dpar[i] = dpar[dpar[i]]
            i = dpar[i]
        return i
    for i in dpar:
        for j, kind, _a, _b, _q in rel[i]:
            if kind == "join" and j in dpar:
                dpar[droot(i)] = droot(j)
    live = {droot(i) for i in dpar if i in crosses}
    seeds = [i for i in cand if (i in dpar and droot(i) in live) or (i not in dpar and i in crosses)]
    if not seeds:
        return G
    sset = set(seeds)
    for i in seeds:
        rel[i] = [r_ for r_ in rel[i] if r_[0] in sset or not _is_seed(net, r_[0])]
    par = {i: i for i in seeds}

    def root(i):
        while par[i] != i:
            par[i] = par[par[i]]
            i = par[i]
        return i

    def unite(a, b, why, detail):
        G.merges.append((a, b, why, detail))
        ra, rb = root(a), root(b)
        if ra != rb:
            par[max(ra, rb, key=lambda x: str(members[x].key))] = min(ra, rb, key=lambda x: str(members[x].key))
    ends_of = {}
    for i in seeds:
        for j, kind, si, sj, _q in rel[i]:
            if j in par:
                unite(i, j, "relation", (kind, si, sj))
            elif kind == "join" and members[j].kind == "ground":
                ends_of.setdefault(j, set()).add(i)
    # (what the pipeline's ramp rule would join: not joined here, kept to be named)
    for j, ss in sorted(ends_of.items()):
        if len(ss) >= 2 and net.L[j] <= gs.CONNECT_M:
            G.ramps.append((j, sorted(ss)))
    dig = G.dig = gs._Verdicts(net, rel, droot, reader)
    G.rel = rel
    for a, b in gs._shared_dips(net, seeds, rel, dig):
        unite(a, b, "dip", None)
    groups = {}
    for i in seeds:
        groups.setdefault(root(i), []).append(i)
    for r in sorted(groups, key=lambda x: str(members[x].key)):
        sysm = gs._system(net, sorted(groups[r], key=lambda x: str(members[x].key)), rel, dig)
        have = set(sysm["seeds"])
        for c in ordered:
            if c.upper in have and c.lower in have:
                sysm["crossings"].append(dict(u=c.upper, su=c.su, d=c.lower, sd=c.sd, q=np.asarray(c.q, np.float64), moves=c.moves,
                                              under=gs.under_pairs(net, c.upper, c.su, c.lower, c.sd), by="elevation"))
        G.systems.append(sysm)
    return G


def _stack(L, nodes):
    """The most spans stacked over one another in the order graph restricted to nodes (the longest chain of 'above'), or None
    where the graph has a cycle (stage 2 reports it)."""
    memo, state = {}, {}
    sys_nodes = set(nodes)
    for root in sorted(sys_nodes):
        if root in memo:
            continue
        stack = [(root, iter(sorted(w for w, _r in L.graph.adj.get(root, ()) if w in sys_nodes)))]
        state[root] = 1
        while stack:
            v, it = stack[-1]
            moved = False
            for w in it:
                if state.get(w) == 1:
                    return None
                if w not in memo and w not in state:
                    state[w] = 1
                    stack.append((w, iter(sorted(x for x, _r in L.graph.adj.get(w, ()) if x in sys_nodes))))
                    moved = True
                    break
            if moved:
                continue
            stack.pop()
            memo[v] = 1 + max([memo.get(w, 0) for w, _r in L.graph.adj.get(v, ()) if w in sys_nodes] + [0])
            state[v] = 2
    return max(memo.values()) if memo else 0


def stretches(P, pid):
    """The stretches of path pid with no ground under them: [(m, first segment, last segment, s0, s1)], longest first: where a
    path stays up (a viaduct whose ends are the only ground it has)."""
    p = P.paths[pid]
    out, cur = [], None
    for r in p.runs:
        if r.kind in ("ground", "rail"):
            if cur:
                out.append(cur)
                cur = None
        elif cur is None:
            cur = [r.s0, r.s1, r.first, r.last]
        else:
            cur[1], cur[3] = r.s1, r.last
    if cur:
        out.append(cur)
    return sorted(((s1 - s0, p.segs[f][0], p.segs[l][0], s0, s1) for s0, s1, f, l in out), reverse=True)


def link_kinds(G, seeds):
    """What holds a system's seeds together, by the kind of link: {"join": (components, largest), "join+cross": ..., "all": ...}
    over G.merges (a joint of two structures, a crossing, a shared dip): if the joints alone leave one big component the system is
    one structure network, whatever else links it."""
    sset = set(seeds)
    out = {}
    for name, kinds in (("join", {"join"}), ("join+cross", {"join", "cross"}), ("all", {"join", "cross", "dip"})):
        par = {i: i for i in seeds}

        def f(i):
            while par[i] != i:
                par[i] = par[par[i]]
                i = par[i]
            return i
        for a, b, why, detail in G.merges:
            k = why if why != "relation" else detail[0]
            if k in kinds and a in sset and b in sset:
                par[f(a)] = f(b)
        sizes = {}
        for i in seeds:
            sizes[f(i)] = sizes.get(f(i), 0) + 1
        out[name] = (len(sizes), max(sizes.values()))
    return out


def system_facts(G, n):
    """What system n says about its places and levels: size, extent, kinds and levels, paths, how high it stacks."""
    net, P, L = G.net, G.P, G.L
    sysm = G.systems[n]
    seeds = sysm["seeds"]
    xy = [net.lines[i] for i in seeds]
    lo = min(float(l_[:, 0].min()) for l_ in xy), min(float(l_[:, 1].min()) for l_ in xy)
    hi = max(float(l_[:, 0].max()) for l_ in xy), max(float(l_[:, 1].max()) for l_ in xy)
    paths = sorted({P.path_of[i] for i in seeds})
    levels = sorted({(net.m[i].kind, float(net.m[i].level)) for i in seeds})
    spans = {c.span_u for c in L.crossings if c.upper in set(seeds)} | {c.span_d for c in L.crossings if c.lower in set(seeds)}
    up = sorted((st + (pid,) for pid in paths for st in stretches(P, pid)), reverse=True)
    return dict(index=n, unbroken=[(round(m), a, b, pid) for m, a, b, _s0, _s1, pid in up[:3]], links=link_kinds(G, seeds),
                seeds=len(seeds), decks=sum(1 for i in seeds if net.m[i].kind == "deck"), dips=len(sysm["dips"]),
                pieces=sum(1 for i in seeds if net.m[i].kind == "deck") + len(sysm["dips"]),
                extent_m=round(math.hypot(hi[0] - lo[0], hi[1] - lo[1]), 0), crossings=len(sysm["crossings"]),
                joins=len(sysm["joins"]), ends=len(sysm["ends"]), paths=paths, levels=levels, stack=_stack(L, spans))


def check(G, namer):
    """Stage 3's checks: ([Finding], stats, [system facts]). Fails: no_ground_point. Notes: oversized_system (what links it, named)."""
    P, net = G.P, G.net
    finds = []
    facts = []
    # paths of a system without a point on the ground, directly or through joints
    floating = {}
    anchored = 0
    for n, sysm in enumerate(G.systems):
        f = system_facts(G, n)
        facts.append(f)
        for pid in f["paths"]:
            if G.reach[pid] == math.inf:
                floating.setdefault(n, []).append(pid)
            elif G.reach[pid] > 0.0:
                anchored += 1
    for n, pids in sorted(floating.items()):
        names = "; ".join(sp.describe(P, namer, P.paths[pid]) for pid in pids[:4])
        i0 = P.paths[pids[0]].segs[0][0]
        where = namer.at(i0, 0.0)
        finds.append(Finding(3, "no_ground_point", "fail",
                             "system %d: %d of its paths have no point on the ground and none through their joints (no datum for "
                             "their heights): %s" % (n, len(pids), names), where, [namer.info(P.paths[pid].segs[0][0]) for pid in pids[:4]]))
    # oversized systems: what links them
    by_seed = {}
    for n, sysm in enumerate(G.systems):
        for i in sysm["seeds"]:
            by_seed[i] = n
    rels = {}

    def rel_of(i):
        if i not in rels:
            rels[i] = [r_ for r_ in gs._relations(net, i) if not gs._tunnel_network(net, r_[0])]
        return rels[i]
    links = {}
    for a, b, why, detail in G.merges:
        if a not in by_seed or a == b:
            continue
        kind = why if why != "relation" else detail[0]
        if why == "dip":
            detail = ia._shared_road(net, a, b, rel_of)
        links.setdefault(by_seed[a], {}).setdefault((min(a, b), max(a, b)), [a, b, kind, detail, None])
    for f in facts:
        n = f["index"]
        if f["pieces"] <= OVERSIZE_PIECES:
            continue
        edges = list(links.get(n, {}).values())
        seeds = G.systems[n]["seeds"]
        adj = {i: [] for i in seeds}
        for k, (a, b, kind, detail, _s) in enumerate(edges):
            adj[a].append((b, k))
            adj[b].append((a, k))
        br = ia._bridges(seeds, edges, adj)
        f["chain"] = [ia._edge_text(net, namer, edges[k]) for k in ia._chain(net, seeds, edges, adj)]
        f["bridges"] = []
        refs = []
        for side, k in br[:3]:
            a, b, kind, detail, _s = edges[k]
            where = ia._edge_where(net, namer, edges[k])
            f["bridges"].append("the %s of %s and %s at %s is the only link between two parts of %d and %d seeds"
                                % (ia._kind_words(kind, detail), namer.label(a), namer.label(b), where, side, len(seeds) - side))
            refs += [namer.info(a), namer.info(b)]
        lk = f["links"]
        why = ("the structures' own joints: joints alone leave %d component(s), the largest %d of %d seeds; with the crossings %d (largest "
               "%d); with the shared dips %d (largest %d)" % (lk["join"][0], lk["join"][1], len(seeds), lk["join+cross"][0],
                                                               lk["join+cross"][1], lk["all"][0], lk["all"][1]))
        up = ""
        if f["unbroken"]:
            up = "; stretches of structure with no ground under them: " + ", ".join(
                "%d m from %s to %s (path %d)" % (m, namer.at(a, 0.0), namer.at(b, net.L[b]), pid) for m, a, b, pid in f["unbroken"])
        where = namer.at(seeds[0], 0.0)
        finds.append(Finding(3, "oversized_system", "note",
                             "system %d (%d pieces, %d seeds, %.0f m across), linked by %s%s%s" % (
                                 n, f["pieces"], len(seeds), f["extent_m"], why, up,
                                 ("; " + "; ".join(f["bridges"])) if f["bridges"] else "; no single link holds it, a mesh of %d joints, "
                                 "crossings and shared dips" % len(edges)), where, refs or [namer.info(seeds[0])]))
    direct = sum(1 for p in P.paths if G.points.get(p.id))
    stats = dict(paths=len(P.paths), with_ground_point=direct, anchored_by_joint=sum(1 for p in P.paths if not G.points.get(p.id)
                                                                                  and G.reach[p.id] < math.inf),
                 floating_paths=sum(1 for p in P.paths if G.reach[p.id] == math.inf), systems=len(G.systems),
                 sizes=sorted((f["pieces"] for f in facts), reverse=True)[:12], ramp_roads_not_merged=len(G.ramps),
                 fails=sum(1 for f in finds if f.level == "fail"))
    return finds, stats, facts
