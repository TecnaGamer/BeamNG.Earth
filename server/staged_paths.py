"""Stage 1 of the staged grade solver: the map's segments joined into paths.

The staged solver (stages 1-4 here, stage 5 heights later) does what gradeshapes' one big quadratic programme per
system does, in pieces that can each be tested and each name their failure (EarthRally issue 67; the user: "we should
have a solver that is in pieces (joining road segments into paths, ordering levels of all involved intersection until
all the path have a ground point found could be done first)"). The modules (deterministic, dnp / dmath only, no time
budget anywhere) run beside today's pipeline and change nothing Earth builds:

  staged_paths      1. segments -> paths        in: gradeshapes.Member list (+ what the map has of each road: meta)
                                                out: Paths (paths, joints, runs of kinds along each path)
  staged_levels     2. level order              in: Paths  out: crossings, who is above, the order graph, its checks
  staged_ground     3. ground points, systems   in: Paths, Levels  out: a ground point per path, the systems cut where
                                                every path is back on the ground, the link that holds a big one together
  staged_feasible   4. feasibility              in: a system, the ground  out: heights exist, or the map's contradiction
                                                as a loop of named roads
  staged            the four run in order, and the comparison with today's find_systems and solver

What each check guarantees (a pass is a claim about the next stage's input, never about the heights):
  1. every segment the map has (two points or more) is in exactly one path, once; a path's consecutive segments meet
     (within gradeshapes.JOIN_M); no segment is in the map twice; the one-way flags along a path agree; no end of a
     bridge or tunnel hangs in the air; a road's own parts are not left a gap apart.
  2. at every crossing of two segments in plan which of them is above is decided once, by gradeshapes._over (the map's
     bridge / tunnel kinds and its layers) and findground.leaves (who leaves the ground); two structures with nothing to
     tell them apart are named; the above relation between the stretches of a path (spans) has no cycle; a path over
     itself (a spiral) is two spans of one path.
  3. every path has a point on the ground, found by propagation (a ground segment, an end at grade with a road on the
     ground, a held deck, a tunnel's mouth, or through a joint to a path that has one), and the systems are what is left
     when the roads on the ground are taken out: where every path is back on the ground the level resets and one
     interchange ends. A system that is big is explained by the one link between its parts, named.
  4. the system's grade law (each segment by its own length), clearances at its crossings, equalities at its joints and
     its ground points are difference constraints z_v - z_u <= w; they have heights if and only if the graph has no
     negative cycle (interchange_audit's exact checker: Bellman-Ford with a certificate). A failure is a loop of roads
     with the metres it is short, reduced until no constraint of it can be left out: the map's contradiction, never
     solved round.

Stage 5 (heights, not built) takes a system that passed 4: its constraint graph already holds the potentials that are a
feasible set of heights (the checker's certificate), and the target (the natural line of each structure, findground rule
1) and the stiffness go in as the objective of one deterministic solve per system with no time budget; curves (a second
difference, not a difference) are the part of the shape that stage 4 does not decide.

Stage 1 here. A segment is one gradeshapes Member (a road's part between two nodes, a lane section
"<road>~lanes<a>-<b>", a bridge or tunnel part, a track). Two segments' ends within JOIN_M, or an end on another's line,
are a joint when gradeshapes' relations say so (_end_meets: the map's nodes and levels decide a bridge's end on a road
under it); two plain roads' ends are always one. At a joint the segments are paired off into the road going on: the one
that is the same road, the straightest, the one whose one-way flow takes the traffic on; the rest end there. A joint of
two ends with a flow that cannot go on (both enter, both leave) is a failure, not a pair. The paths are the chains of
those pairs. Before any of that clean() makes the members one segment per road part: points closer than MIN_SEG_M are merged
and a part shorter than that is absorbed into its joint (the kernel holds such a step to MIN_SEG_M, so none is left), the same
road twice (another cell's copy under another key, a copy clipped at the cell's edge, a road and its own lane sections) is
kept once. Parts of one road that do not meet (a tunnel 12 m short of its ground part) are joined by the road's identity and
the map's nodes, within BRIDGE_MAX_M, never by distance alone; the gap is a note.

Stage 3 cuts a system where its paths are all back on the ground; it does not cut at a region box and it does not merge
through a ground road between two decks (today's 400 m "ramp" rule): a ground road is a ground point on both decks. What
still holds a big system together is named: on the Shanghai regions it is the elevated network's own joints and long
viaducts with no road on the ground under them, not ground roads. Stage 4 holds the ground points as constraints (a deck's
end reaches the ground within the grade law over the ground road between, +- gradeshapes.SOFT_M for the ground's own slope)
and says whose a contradiction is: the map's own tags, or the levels read from them (several level ranges of one road).
Pure; deterministic: members and ends in sorted order, ties broken by key."""
import math
from collections import namedtuple

import numpy as np

import dnp
import gradeshapes as gs
import interchange_audit as ia

Finding = ia.Finding
ROAD, RAIL = "road", "rail"
NETWORK = {"ground": ROAD, "deck": ROAD, "tunnel": ROAD, "rail": RAIL, "rail_deck": RAIL}
STRUCT = gs.STRUCTURES                  # the members that are no ground: deck, rail_deck, tunnel
CELL_M = 12.0                           # m: the grid cells ends and line samples are indexed in
SAMPLE_M = 6.0                          # m: a line's samples in the index (a cell is twice that)
FOLD_COS = 0.9                          # two ends leaving the joint within 25 deg of each other fold back on one another (a
#                                         line over itself, a lane section laid over its road): never the road going on
STRAIGHT_COS = -0.5                     # a road goes on within 60 deg of straight where several meet (gradeshapes.next_member)
DUP_NEAR_M = ia.DUP_NEAR_M
GAP_M = ia.GAP_M
TOP = ia.TOP

End = namedtuple("End", "seg end")      # a segment's start (0) or end (1)
Run = namedtuple("Run", "kind level first last s0 s1 cls")      # a stretch of one kind and level: path segments [first, last]
PathEnd = namedtuple("PathEnd", "seg end point meets state")    # state: "joint" | "free" | "data_edge"
Path = namedtuple("Path", "id segs closed start stop runs length flow offset")
# segs: [(member, forward)] in order along the path (forward: travelled start to end); start / stop: PathEnd or None
# (closed); runs: [Run]; flow: +1 / -1 one-way along the path, 0 two-way throughout, None conflicting; offset: {member:
# m along the path to the member's first point as travelled}


def _road(m):
    """The map road a member is a part of: its feature id (a lane section is its road's: gradeshapes._road_of), a track's its
    line's id (key[2]: every track's key starts "rail")."""
    if m.kind in ("rail", "rail_deck") and isinstance(m.key, tuple) and len(m.key) > 2:
        return "rail:" + str(m.key[2])
    return gs._road_of(m).partition("~rest")[0]


def _key(net, i):
    return (str(net.m[i].key), i)


def usable(net, i):
    """Whether member i has a line a path can follow (two points or more, finite, some length)."""
    return len(net.lines[i]) >= 2 and bool(np.isfinite(net.lines[i]).all()) and net.L[i] > 0.0


class Joint:
    """Where segments meet: the ends there (End), the segments whose line it lies on (through: [(member, s)]), the pairs
    of ends that go on into each other (paired), the pairs of ends with a flow that cannot go on (conflicts)."""

    def __init__(self, point):
        self.point = point
        self.ends, self.through, self.paired, self.conflicts = [], [], [], []


class Paths:
    """Stage 1's answer: paths, the joints, which path a segment is in, with the map's facts about each road."""

    def __init__(self, net, meta=None, edge=None, cleaned=None):
        self.net, self.meta, self.edge = net, meta or {}, edge
        self.cleaned = cleaned          # Cleaned: what was dropped, merged, absorbed before the paths (None: the net as given)
        self.bridged = []               # (End, End, gap m): parts of one road joined by its identity across a gap
        self.paths = []
        self.path_of = {}               # member -> path id
        self.joints = []
        self.joint_of = {}              # End -> joint index
        self.mate = {}                  # End -> the End it goes on into
        self.partners = {}              # End -> [End] joined to it (before the pairing)
        self.touch = {}                 # End -> [(member, s)] whose line it lies on
        self.skipped = []               # members with no usable line
        self.rel = {}                   # structure member -> gradeshapes._relations (kept: stage 2 reads them)

    def md(self, i):
        """What the map has of member i's road: {osm, name, layer, oneway, dir} (an empty dict: nothing known)."""
        e = str(self.net.m[i].key[0] if isinstance(self.net.m[i].key, tuple) else self.net.m[i].key)
        return self.meta.get(e) or self.meta.get(_road(self.net.m[i])) or {}

    def flow(self, i, end):
        """(vehicles enter segment i from its end `end`, leave it there): the one-way flag and its direction (ia._flow)."""
        return ia._flow(self.md(i), end)

    def at(self, i, s):
        """(path id, m along that path) of member i's arc length s."""
        p = self.paths[self.path_of[i]]
        fwd = dict(p.segs)[i]
        return p.id, p.offset[i] + (s if fwd else self.net.L[i] - s)

    def span(self, path, lo, hi=None):
        """The runs of a path overlapping [lo, hi] m along it (hi None: the run at lo)."""
        hi = lo if hi is None else hi
        return [r for r in path.runs if r.s0 - 1e-6 <= hi and r.s1 + 1e-6 >= lo]


def _network(net, i):
    return NETWORK[net.m[i].kind]


def _grid(net, members):
    """({cell: {(member, end)}} of the ends, {cell: {member}} of the line samples) of members."""
    ends, lines = {}, {}
    for i in members:
        line = net.lines[i]
        for e, p in ((0, line[0]), (1, line[-1])):
            ends.setdefault((int(p[0] // CELL_M), int(p[1] // CELL_M)), set()).add((i, e))
        sv = np.linspace(0.0, net.L[i], max(2, int(net.L[i] // SAMPLE_M) + 1))
        x, y = np.interp(sv, net.s[i], line[:, 0]), np.interp(sv, net.s[i], line[:, 1])
        for cell in set(zip((x // CELL_M).astype(np.int64).tolist(), (y // CELL_M).astype(np.int64).tolist())):
            lines.setdefault(cell, set()).add(i)
    return ends, lines


def _near_cells(p, table):
    cx, cy = int(p[0] // CELL_M), int(p[1] // CELL_M)
    out = set()
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            out |= table.get((cx + dx, cy + dy), set())
    return out


def _len(v):
    """Length of a 2-vector (the same bits on every platform: dnp)."""
    return float(dnp.hypot(float(v[0]), float(v[1])))


def _dot(a, b):
    return float(dnp.dot(np.asarray(a, np.float64), np.asarray(b, np.float64)))


def _out(net, i, e):
    """Unit vector at member i's end e pointing away from the joint along the member."""
    line = net.lines[i]
    p, q = (line[0], line[1]) if e == 0 else (line[-1], line[-2])
    k = 1
    while k < len(line) - 1 and _len(q - p) < 1.0:       # (a repeated point or a very short first step)
        k += 1
        q = line[k] if e == 0 else line[-1 - k]
    v = q - p
    return v / max(_len(v), 1e-9)


def build(members, meta=None, edge=None):
    """The paths of the map's members (gradeshapes.Member list): Paths, over the cleaned net P.net (clean: one segment per road
    part, no point closer than MIN_SEG_M to the last). meta: {Earth feature id: {osm, name, layer, oneway, dir}} (what the map
    has of a road; {} where nothing is known: every road two-way), edge(xy) -> whether the point is at the edge of the map data
    read (a road met there may go on in a cell not read)."""
    cl = clean(members, meta)
    net = gs.Net(cl.members)
    P = Paths(net, meta, edge, cl)
    n = len(net.m)
    ok = [i for i in range(n) if usable(net, i)]
    P.skipped = [i for i in range(n) if not usable(net, i)]
    ends_grid, lines_grid = _grid(net, ok)
    structs = [i for i in ok if net.m[i].kind in STRUCT]
    for i in structs:
        P.rel[i] = gs._relations(net, i)
    partners = {(i, e): set() for i in ok for e in (0, 1)}
    touch = {(i, e): set() for i in ok for e in (0, 1)}

    def link(a, b):
        partners[a].add(b)
        partners[b].add(a)

    def end_s(i, e):
        return 0.0 if e == 0 else net.L[i]
    # (a) structures: the joints gradeshapes' relations find (_end_meets decides a bridge's end on what is under it)
    for i in structs:
        for j, kind, si, sj, _q in P.rel[i]:
            if kind != "join" or j == i or not usable(net, j) or _network(net, i) != _network(net, j):
                continue
            ei = int(si > net.L[i] / 2) if min(si, net.L[i] - si) <= gs.JOIN_M else None
            ej = int(sj > net.L[j] / 2) if min(sj, net.L[j] - sj) <= gs.JOIN_M else None
            if ei is not None and ej is not None:
                link((i, ei), (j, ej))
            elif ei is not None:
                touch[(i, ei)].add((j, round(float(sj), 3)))
            elif ej is not None:
                touch[(j, ej)].add((i, round(float(si), 3)))
    # (b) two members that are no structure meet where they are: ends within JOIN_M, an end on a line's middle
    plain = [i for i in ok if net.m[i].kind not in STRUCT]
    pend = {c: {x for x in v if net.m[x[0]].kind not in STRUCT} for c, v in ends_grid.items()}
    for i in plain:
        for e in (0, 1):
            p = net.lines[i][0 if e == 0 else -1]
            for j, f in sorted(_near_cells(p, pend)):
                if j <= i or _network(net, i) != _network(net, j):
                    continue
                if _len(net.lines[j][0 if f == 0 else -1] - p) <= gs.JOIN_M:
                    link((i, e), (j, f))
    for i in plain:
        for e in (0, 1):
            p = net.lines[i][0 if e == 0 else -1]
            for j in sorted(_near_cells(p, lines_grid)):
                if j == i or net.m[j].kind in STRUCT or _network(net, i) != _network(net, j):
                    continue
                sj, d = gs.proj(net.lines[j], net.s[j], p)
                if d <= gs.JOIN_M and min(sj, net.L[j] - sj) > gs.JOIN_M:
                    touch[(i, e)].add((j, round(float(sj), 3)))
    P.partners = {End(*a): sorted(End(*b) for b in v) for a, v in partners.items() if v}
    P.touch = {End(*a): sorted(v) for a, v in touch.items() if v}
    # (c) the joints: the ends joined to each other, with what they lie on
    comp = {}
    for a in sorted(partners):
        if a in comp:
            continue
        stack, comp[a] = [a], len(comp)
        cid = comp[a]
        while stack:
            x = stack.pop()
            for y in partners[x]:
                if y not in comp:
                    comp[y] = cid
                    stack.append(y)
    by = {}
    for a, c in comp.items():
        by.setdefault(c, []).append(a)
    for c in sorted(by, key=lambda c: min(_key(net, a[0]) + (a[1],) for a in by[c])):
        es = sorted(by[c], key=lambda a: _key(net, a[0]) + (a[1],))
        a0 = es[0]
        jt = Joint(net.lines[a0[0]][0 if a0[1] == 0 else -1])
        jt.ends = [End(*a) for a in es]
        jt.through = sorted({t for a in es for t in touch[a]})
        for a in es:
            P.joint_of[End(*a)] = len(P.joints)
        P.joints.append(jt)
    for a, ts in touch.items():
        if ts and End(*a) not in P.joint_of:
            jt = Joint(net.lines[a[0]][0 if a[1] == 0 else -1])
            jt.ends = [End(*a)]
            jt.through = sorted(ts)
            P.joint_of[End(*a)] = len(P.joints)
            P.joints.append(jt)
    # (d) at each joint, the pairs that go on into each other
    cand = []
    for jx, jt in enumerate(P.joints):
        for a in jt.ends:
            for b in P.partners.get(a, ()):
                if a < b:
                    crowded = len(P.partners.get(a, ())) > 1 or len(P.partners.get(b, ())) > 1 or a in P.touch or b in P.touch
                    same = _road(net.m[a.seg]) == _road(net.m[b.seg])
                    cos = _dot(_out(net, *a), _out(net, *b))
                    ea, la = P.flow(a.seg, a.end)
                    eb, lb = P.flow(b.seg, b.end)
                    flows = (la and eb) or (lb and ea)
                    score = (0 if same else 1, 0 if net.m[a.seg].cls == net.m[b.seg].cls else 1, cos)
                    cand.append((jx, a, b, crowded, same, cos, flows, score))
    cand.sort(key=lambda c: (c[7], _key(net, c[1].seg) + (c[1].end,), _key(net, c[2].seg) + (c[2].end,)))
    for jx, a, b, crowded, same, cos, flows, _score in cand:
        if a in P.mate or b in P.mate:
            continue
        if not flows:
            P.joints[jx].conflicts.append((a, b))
            continue
        if cos > FOLD_COS or (crowded and not (same or cos <= STRAIGHT_COS)):
            continue
        P.mate[a], P.mate[b] = b, a
        P.joints[jx].paired.append((a, b))
    _bridge_road_parts(P, ok)
    _chains(P, ok)
    return P


MIN_SEG_M = 0.5                         # m: no segment of a path is shorter (the kernel holds one under this to this: a point
#                                         closer than this to the last is merged into it, a part shorter than this is its joint)
SECTION_NEAR_M = 3.0                    # m: a lane section lies this near its road's line (a section's line is the road's, smoothed
#                                         differently: 2.1 m at most at Shanghai's elevated motorway)
NEAR_GAP_M = 15.0                       # m: a gap this short is a road's bend, whichever way the two ends face (A61's tunnel 4.0 m off its
#                                         ground part, round a bend); a longer one is bridged only where each part lies ahead of the other
BRIDGE_MAX_M = 100.0                    # m: the most a road's own parts are bridged by its identity (a tunnel part 12.0 m short
#                                         of its ground part at A61, 6.3 m at Prague; a part further off is a gap)


class Cleaned:
    """What stage 1 did to the members before the paths: the members kept (cleaned lines), where each came from, what was dropped
    and why. members[k] came from input source[k]; alias {input index: the input index whose path it is in (None: absorbed)};
    absorbed: inputs shorter than MIN_SEG_M (their two ends are one point); collapsed: [(dropped, kept, why)], the same road twice."""

    def __init__(self):
        self.members, self.source, self.alias, self.absorbed, self.collapsed = [], [], {}, [], []
        self.offset = []                # m along the input member where member k begins (a road cut where its lane sections lie)
        self.merged_points = 0
        self.input = None               # gradeshapes.Net of the members as given (names of what was dropped)


def _merge_points(line):
    """The line with no point closer than MIN_SEG_M to the one before it (the last point is kept: a line's end is where it
    meets the next), and how many were merged."""
    pts = np.asarray(line, np.float64)[:, :2]
    if len(pts) < 2 or not np.isfinite(pts).all():
        return np.asarray(line, np.float64), 0
    keep = [pts[0]]
    for p in pts[1:-1]:
        if _len(p - keep[-1]) >= MIN_SEG_M:
            keep.append(p)
    last = pts[-1]
    while len(keep) > 1 and _len(last - keep[-1]) < MIN_SEG_M:
        keep.pop()
    keep.append(last)
    return np.array(keep), len(pts) - len(keep)


def overlap_pairs(net, members=None):
    """The members of net whose lines lie over one another: [(a, b, whole, over m, same road)], a < b, one kind and level: the
    same line twice (whole: nearly all of each within DUP_NEAR_M of the other, of nearly one length), or one road's parts over one
    another (over: m of the longer overlap). Found from the ends (a line twice has its ends in common) and, for structures, from
    the neighbours' boxes."""
    ok = list(range(len(net.m))) if members is None else list(members)
    ok = [i for i in ok if len(net.lines[i]) >= 2 and net.L[i] >= 1.0 and np.isfinite(net.lines[i]).all()]
    okset = set(ok)
    ends = {}
    for i in ok:
        for p in (net.lines[i][0], net.lines[i][-1]):
            ends.setdefault((int(p[0] // CELL_M), int(p[1] // CELL_M)), set()).add((i, 0))
    cand = set()
    for i in ok:
        for p in (net.lines[i][0], net.lines[i][-1]):
            for j, _f in sorted(_near_cells(p, ends)):
                if j > i and j in okset:
                    cand.add((i, j))
    for i in ok:
        if net.m[i].kind in STRUCT or _is_section(net.m[i]):
            for j in net.near(i, 1.0):
                if j in okset:
                    cand.add((min(i, j), max(i, j)))
    pts = {}

    def points(i):
        if i not in pts:
            sv = np.linspace(0.0, net.L[i], max(2, int(net.L[i] // gs.STEP_M) + 1))
            pts[i] = np.stack([np.interp(sv, net.s[i], net.lines[i][:, 0]), np.interp(sv, net.s[i], net.lines[i][:, 1])], axis=1)
        return pts[i]
    out = []
    for a, b in sorted(cand):
        ma, mb = net.m[a], net.m[b]
        if ma.kind != mb.kind or ma.level != mb.level or _network(net, a) != _network(net, b):
            continue
        ina = float((gs._proj_many(net.lines[b], net.s[b], points(a))[1] <= DUP_NEAR_M).mean())
        inb = float((gs._proj_many(net.lines[a], net.s[a], points(b))[1] <= DUP_NEAR_M).mean())
        over = max(ina * net.L[a], inb * net.L[b])
        whole = min(ina, inb) >= 0.9 and abs(net.L[a] - net.L[b]) <= 0.1 * max(net.L[a], net.L[b])
        same = _road(ma) == _road(mb)
        if whole or (same and over >= 2 * gs.STEP_M):
            out.append((a, b, whole, over, same, ina, inb))
    return out


def _part_key(key, n):
    """The key of the n-th stretch kept of a road whose lane sections lie over the rest: the road's id first, as every part of it."""
    return key + ("rest", n) if isinstance(key, tuple) else "%s~rest%d" % (key, n)


def _slice(line, s, a, b):
    """The polyline between arc lengths a and b."""
    inner = (s > a + 1e-6) & (s < b - 1e-6)
    pa = np.array([np.interp(a, s, line[:, 0]), np.interp(a, s, line[:, 1])])
    pb = np.array([np.interp(b, s, line[:, 0]), np.interp(b, s, line[:, 1])])
    return np.vstack([pa, line[inner][:, :2], pb])


def _is_section(m):
    return "~lanes" in str(m.key[0] if isinstance(m.key, tuple) else m.key)


def clean(members, meta=None):
    """The members as one segment per road part: points closer than MIN_SEG_M merged, a part shorter than that its joint (absorbed),
    the same road twice kept once (two cells' copies of one road part under different keys; a road and its own lane sections: the
    sections stay, they are the road cut where its lanes change). Cleaned."""
    out = Cleaned()
    first = []
    for i, m in enumerate(members):
        line, k = _merge_points(m.line)
        out.merged_points += k
        if len(line) >= 2 and np.isfinite(line).all() and float(dnp.hypot(*np.diff(line[:, :2], axis=0).T).sum()) < MIN_SEG_M:
            out.absorbed.append(i)
            continue
        first.append((i, m._replace(line=line)))
    net1 = gs.Net([m for _i, m in first])
    drop = {}
    split = {}
    sections_of = {}
    for a, b, whole, over, same, ina, inb in overlap_pairs(net1):
        if not same:
            continue
        ma, mb = net1.m[a], net1.m[b]
        if not whole and _is_section(ma) == _is_section(mb):
            # (one road's part lying inside another's: a copy clipped at another cell's edge)
            short, frac = (a, ina) if net1.L[a] <= net1.L[b] else (b, inb)
            if frac >= 0.9:
                drop.setdefault(short, (b if short == a else a, "a part of it from another cell, clipped"))
                continue
        if whole:
            # (one of them goes: the lane section, else the one whose key sorts first)
            if _is_section(ma) != _is_section(mb):
                keep, gone = (a, b) if _is_section(ma) else (b, a)
            else:
                keep, gone = (a, b) if str(ma.key) <= str(mb.key) else (b, a)
            drop.setdefault(gone, (keep, "the same line twice"))
        elif _is_section(ma) != _is_section(mb):
            parent, sec = (a, b) if _is_section(mb) else (b, a)
            sections_of.setdefault(parent, []).append((sec, over))
    for parent, secs in sections_of.items():
        if parent in drop:
            continue
        spans = []
        for sec, _over in secs:
            sv = np.linspace(0.0, net1.L[sec], max(2, int(net1.L[sec] // gs.STEP_M) + 1))
            pts = np.stack([np.interp(sv, net1.s[sec], net1.lines[sec][:, 0]), np.interp(sv, net1.s[sec], net1.lines[sec][:, 1])], axis=1)
            sp_, d = gs._proj_many(net1.lines[parent], net1.s[parent], pts)
            near = d <= SECTION_NEAR_M
            if near.mean() >= 0.9:
                spans.append((float(sp_[near].min()), float(sp_[near].max())))
        merged = []
        for lo_, hi_ in sorted(spans):
            if merged and lo_ <= merged[-1][1] + gs.JOIN_M:
                merged[-1][1] = max(merged[-1][1], hi_)
            else:
                merged.append([lo_, hi_])
        covered = sum(b_ - a_ for a_, b_ in merged)
        if covered >= 0.9 * net1.L[parent]:
            drop[parent] = (min(s_ for s_, _o in secs), "its own lane sections cover it")
        elif merged:
            # (the sections cover a stretch of it: the road keeps the stretches they do not, the sections have the rest)
            rest, at = [], 0.0
            for a_, b_ in merged:
                if a_ - at >= gs.JOIN_M:
                    rest.append((at, a_))
                at = max(at, b_)
            if net1.L[parent] - at >= gs.JOIN_M:
                rest.append((at, net1.L[parent]))
            split[parent] = rest
            out.collapsed.append((first[parent][0], first[secs[0][0]][0], "its lane sections lie over %.0f m of it: it keeps the other %.0f m"
                                  % (covered, sum(b_ - a_ for a_, b_ in rest))))

    def final(k):
        seen = set()
        while k in drop and k not in seen:
            seen.add(k)
            k = drop[k][0]
        return k
    for k, (m_i, m) in enumerate(first):
        if k in drop:
            keep = final(k)
            out.alias[m_i] = first[keep][0]
            out.collapsed.append((m_i, first[keep][0], drop[k][1]))
        elif k in split:
            for n_, (a_, b_) in enumerate(split[k]):
                out.members.append(m._replace(key=_part_key(m.key, n_), line=_slice(net1.lines[k], net1.s[k], a_, b_)))
                out.source.append(m_i)
                out.offset.append(a_)
        else:
            out.members.append(m)
            out.source.append(m_i)
            out.offset.append(0.0)
    for i in out.absorbed:
        out.alias[i] = None
    out.input = gs.Net(members)
    return out


def _bridge_road_parts(P, ok):
    """A road's own parts that do not meet: the ends of its parts nothing else joins, paired nearest first within BRIDGE_MAX_M
    when each lies ahead of the other along its own heading (the tunnel part 12.0 m short of its ground part: the road goes on).
    The map's identity decides, not the distance: the road is one, a gap in its parts is a gap in the data."""
    net = P.net
    by = {}
    for i in ok:
        by.setdefault((_road(net.m[i]), _network(net, i)), []).append(i)
    for road in sorted(by):
        parts = by[road]
        if len(parts) < 2:
            continue
        free = [End(i, e) for i in sorted(parts) for e in (0, 1) if End(i, e) not in P.mate]
        cand = []
        for x in range(len(free)):
            for y in range(x + 1, len(free)):
                a, b = free[x], free[y]
                if a.seg == b.seg:
                    continue
                pa, pb = net.lines[a.seg][0 if a.end == 0 else -1], net.lines[b.seg][0 if b.end == 0 else -1]
                d = float(math.hypot(float(pa[0] - pb[0]), float(pa[1] - pb[1])))
                if not (gs.JOIN_M < d <= BRIDGE_MAX_M):
                    continue
                v = (pb - pa) / d
                if _dot(_out(net, *a), _out(net, *b)) > FOLD_COS:
                    continue            # (the two lines leave the gap the same way: one over the other, not a road going on)
                if d > NEAR_GAP_M and (_dot(v, -_out(net, *a)) < 0.0 or _dot(-v, -_out(net, *b)) < 0.0):
                    continue            # (not ahead of each other: two ends side by side, not a road going on)
                ea, la = P.flow(a.seg, a.end)
                eb, lb = P.flow(b.seg, b.end)
                if not ((la and eb) or (lb and ea)):
                    continue
                cand.append((d, a, b))
        cand.sort(key=lambda c: (c[0], _key(net, c[1].seg), _key(net, c[2].seg)))
        for d, a, b in cand:
            if a in P.mate or b in P.mate:
                continue
            P.mate[a], P.mate[b] = b, a
            P.bridged.append((a, b, d))
            jt = Joint(net.lines[a.seg][0 if a.end == 0 else -1])
            jt.ends = [a, b]
            jt.paired = [(a, b)]
            for e in (a, b):
                P.joint_of[e] = len(P.joints)
            P.joints.append(jt)


def _chains(P, ok):
    """The paths: the chains of paired ends, each member once, in an order that does not depend on the input's."""
    net = P.net
    seen, raw = set(), []
    order = sorted(ok, key=lambda i: _key(net, i))

    def walk(first, e_in):
        segs, cur, ent = [], first, e_in
        closed = False
        while True:
            seen.add(cur)
            segs.append((cur, ent == 0))
            nxt = P.mate.get(End(cur, 1 - ent))
            if nxt is None:
                break
            if nxt.seg in seen:
                closed = nxt.seg == first and nxt.end == e_in
                break
            cur, ent = nxt.seg, nxt.end
        return segs, closed
    for i in order:
        if i in seen:
            continue
        # (the chain's free end first: a member in the middle of a chain is reached from its beginning)
        cur, e_out = i, 0
        guard = set()
        while True:
            guard.add(cur)
            nxt = P.mate.get(End(cur, e_out))
            if nxt is None or nxt.seg in guard:
                break
            cur, e_out = nxt.seg, 1 - nxt.end
        if P.mate.get(End(cur, e_out)) is None:
            segs, closed = walk(cur, e_out)
        else:
            segs, closed = walk(i, 0)
        raw.append((segs, closed))
    out = []
    for segs, closed in raw:
        out.append(_orient(P, segs, closed))
    out.sort(key=lambda t: min(_key(net, s[0]) for s in t[0]))
    for pid, (segs, closed) in enumerate(out):
        P.paths.append(_make_path(P, pid, segs, closed))
        for s, _f in segs:
            P.path_of[s] = pid


def _flows_along(P, segs):
    """{+1, -1} of the one-way flows along a chain (+1: the way it is travelled), whether it has a two-way part."""
    out = set()
    for i, fwd in segs:
        md = P.md(i)
        if md.get("oneway"):
            d = md.get("dir") or 1
            out.add(d if fwd else -d)
    return out


def _orient(P, segs, closed):
    """The chain travelled with the traffic where it is one-way, else from the end whose member sorts first."""
    fl = _flows_along(P, segs)
    flip = False
    if fl == {-1}:
        flip = True
    elif not fl:
        a, b = _key(P.net, segs[0][0]), _key(P.net, segs[-1][0])
        flip = len(segs) > 1 and b < a
    if flip:
        segs = [(i, not f) for i, f in segs[::-1]]
    return segs, closed


def _make_path(P, pid, segs, closed):
    net = P.net
    off, run, runs = {}, 0.0, []
    for k, (i, fwd) in enumerate(segs):
        off[i] = run
        m = net.m[i]
        if runs and runs[-1][0] == m.kind and runs[-1][1] == m.level:
            r = runs[-1]
            runs[-1] = (r[0], r[1], r[2], k, r[4], run + net.L[i], r[6] | {m.cls})
        else:
            runs.append((m.kind, m.level, k, k, run, run + net.L[i], {m.cls}))
        run += net.L[i]
    runs = [Run(k_, lv, f, l, s0, s1, tuple(sorted(c for c in cl if c))) for k_, lv, f, l, s0, s1, cl in runs]
    fl = _flows_along(P, segs)
    flow = None if len(fl) > 1 else (next(iter(fl)) if fl else 0)

    def pend(k, e):
        i, fwd = segs[k]
        end = (0 if fwd else 1) if e == "start" else (1 if fwd else 0)
        a = End(i, end)
        p = net.lines[i][0 if end == 0 else -1]
        meets = sorted({b for b in P.partners.get(a, ()) if b != P.mate.get(a)})
        through = list(P.touch.get(a, ()))
        if meets or through:
            state = "joint"
        elif P.edge is not None and P.edge(p):
            state = "data_edge"
        else:
            state = "free"
        return PathEnd(i, end, p, meets, state)
    start = None if closed else pend(0, "start")
    stop = None if closed else pend(len(segs) - 1, "stop")
    return Path(pid, segs, closed, start, stop, runs, run, flow, off)


# ---- the checks ----

def _label(namer, i, s=None):
    return namer.label(i, s)


def check(P, namer):
    """Stage 1's checks. ([Finding], stats): every failure named by its Earth id, OSM way and lat / lon (namer: interchange_audit.
    Namer over the same net and meta). Fails: broken_part, missing_segment, duplicated_segment, path_gap, gap, duplicate,
    oneway_conflict, path_direction, dangling_end; notes: free_end (a road's dead end), end_at_data_edge, zero_length."""
    net = P.net
    finds = []
    # 0. what the cleaning did: one segment per road part (the pipeline's copies, collapsed; parts that are only a point)
    cl = P.cleaned
    nin = ia.Namer(cl.input, namer.ll, namer.meta, namer.edge) if cl is not None else None
    if cl is not None:
        for gone, kept, why in cl.collapsed:
            finds.append(Finding(1, "collapsed_copy", "note",
                                 "%s and %s are one road part: %s" % (nin.label(gone), nin.label(kept), why),
                                 nin.at(gone, 0.0), [nin.info(gone), nin.info(kept)]))
        for i in cl.absorbed:
            finds.append(Finding(1, "absorbed_part", "note", "%s is shorter than %.1f m: its two ends are one point, no segment of a "
                                 "path" % (nin.label(i), MIN_SEG_M), nin.at(i, 0.0), [nin.info(i)]))
    # 1. every segment in exactly one path, once (every input part: in one, or the copy of one, or a point)
    count = {}
    for p in P.paths:
        for i, _f in p.segs:
            count[i] = count.get(i, 0) + 1
    for i in sorted(range(len(net.m)), key=lambda i: _key(net, i)):
        if not usable(net, i):
            for code, text in ia._member_defects(net, i) or [("broken_part", "no usable line")]:
                level = "note" if code == "zero_length" else "fail"
                finds.append(Finding(1, code, level, "%s: %s" % (namer.label(i), text), namer.at(i, 0.0), [namer.info(i)]))
            continue
        for code, text in ia._member_defects(net, i):
            if code == "zero_length":
                finds.append(Finding(1, code, "note", "%s: %s" % (namer.label(i), text), namer.at(i, 0.0), [namer.info(i)]))
        c = count.get(i, 0)
        if c != 1:
            finds.append(Finding(1, "missing_segment" if c == 0 else "duplicated_segment", "fail",
                                 "%s is in %d paths (every segment is in exactly one)" % (namer.label(i), c),
                                 namer.at(i, 0.0), [namer.info(i)]))
    if cl is not None:
        covered = set(cl.source) | set(cl.alias)
        for i in range(len(cl.input.m)):
            if i not in covered:
                finds.append(Finding(1, "missing_segment", "fail", "%s is in no path, nor the copy of one" % nin.label(i),
                                     nin.at(i, 0.0), [nin.info(i)]))
        for i, k in cl.alias.items():
            if k is not None and k not in set(cl.source):
                finds.append(Finding(1, "missing_segment", "fail", "%s is the copy of %s, which is in no path" % (nin.label(i), nin.label(k)),
                                     nin.at(i, 0.0), [nin.info(i)]))
    # 2. no gap along a path: the next segment starts where the last ended (a road's own parts bridged by its identity are
    # named, with the metres: the map has them apart)
    bridged_set = set()
    for a_, b_, gap in P.bridged:
        bridged_set |= {(a_, b_), (b_, a_)}
        finds.append(Finding(1, "identity_join", "note",
                             "%s and %s are parts of one road %.1f m apart (its end and its next part's start): joined by the road's "
                             "identity" % (namer.label(a_.seg), namer.label(b_.seg), gap), namer.point(P.joints[P.joint_of[a_]].point),
                             [namer.info(a_.seg), namer.info(b_.seg)]))
    for p in P.paths:
        pairs = list(zip(p.segs, p.segs[1:])) + ([(p.segs[-1], p.segs[0])] if p.closed and len(p.segs) > 1 else [])
        for (i, fi), (j, fj) in pairs:
            qi = net.lines[i][-1 if fi else 0]
            qj = net.lines[j][0 if fj else -1]
            d = float(math.hypot(float(qi[0] - qj[0]), float(qi[1] - qj[1])))
            if d > gs.JOIN_M + 1e-6 and (End(i, 1 if fi else 0), End(j, 0 if fj else 1)) not in bridged_set:
                finds.append(Finding(1, "path_gap", "fail",
                                     "path %d: %s and %s are joined %.1f m apart at %s" % (p.id, namer.label(i), namer.label(j),
                                                                                          d, namer.point(qi)),
                                     namer.point(qi), [namer.info(i), namer.info(j)]))
    # 3. a road's own parts left a gap apart (nothing joins either end)
    free_ends = [a for a in (End(i, e) for i in range(len(net.m)) if usable(net, i) for e in (0, 1))
                 if a not in P.mate and a not in P.partners and a not in P.touch]
    cell = {}
    for a in free_ends:
        p = net.lines[a.seg][0 if a.end == 0 else -1]
        cell.setdefault((int(p[0] // GAP_M), int(p[1] // GAP_M)), []).append(a)
    seen = set()
    for a in free_ends:
        pa = net.lines[a.seg][0 if a.end == 0 else -1]
        cx, cy = int(pa[0] // GAP_M), int(pa[1] // GAP_M)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for b in cell.get((cx + dx, cy + dy), ()):
                    if b.seg <= a.seg or (a, b) in seen:
                        continue
                    if _road(net.m[a.seg]) != _road(net.m[b.seg]) or _network(net, a.seg) != _network(net, b.seg):
                        continue
                    pb = net.lines[b.seg][0 if b.end == 0 else -1]
                    d = float(math.hypot(float(pa[0] - pb[0]), float(pa[1] - pb[1])))
                    if gs.JOIN_M < d <= GAP_M:
                        seen.add((a, b))
                        finds.append(Finding(1, "gap", "fail",
                                             "one road's parts %.1f m apart: %s (its %s) and %s (its %s), nothing joins either end"
                                             % (d, namer.label(a.seg), "start" if a.end == 0 else "end", namer.label(b.seg),
                                                "start" if b.end == 0 else "end"), namer.point(pa),
                                             [namer.info(a.seg), namer.info(b.seg)]))
    # 4. no duplicate: the same line twice (structures: one road's parts over one another too)
    finds += _duplicates(P, namer)
    # 5. directions agree: no joint whose flows cannot go on, no path with traffic both ways along it
    for jx, jt in enumerate(P.joints):
        for a, b in jt.conflicts:
            ea, la = P.flow(a.seg, a.end)
            eb, lb = P.flow(b.seg, b.end)
            if la and not lb:
                what = "flows into the joint, and so does"
            elif ea and not la:
                what = "flows out of the joint, and so does"
            else:
                what = "and"
            where = namer.point(jt.point)
            finds.append(Finding(1, "oneway_conflict", "fail",
                                 "%s %s %s at %s: the one-way flags do not let the road go on (nothing there takes the traffic "
                                 "on)" % (namer.label(a.seg), what, namer.label(b.seg), where), where,
                                 [namer.info(a.seg), namer.info(b.seg)]))
    for p in P.paths:
        if p.flow is None:
            fl = [(i, f, P.md(i)) for i, f in p.segs if P.md(i).get("oneway")]
            first = next(((i, f, md) for i, f, md in fl if ((md.get("dir") or 1) if f else -(md.get("dir") or 1)) > 0), fl[0])
            other = next(((i, f, md) for i, f, md in fl if ((md.get("dir") or 1) if f else -(md.get("dir") or 1)) < 0), fl[-1])
            finds.append(Finding(1, "path_direction", "fail",
                                 "path %d carries traffic both ways along it: %s one way, %s the other"
                                 % (p.id, namer.label(first[0]), namer.label(other[0])), namer.at(other[0], 0.0),
                                 [namer.info(first[0]), namer.info(other[0])]))
    # 6. the path's ends: a bridge or tunnel meeting nothing hangs in the air
    free = dangling = edge_ends = dead = 0
    for p in P.paths:
        for pe, name in ((p.start, "start"), (p.stop, "end")):
            if pe is None:
                continue
            m = net.m[pe.seg]
            if pe.state == "joint":
                continue
            if pe.state == "data_edge":
                edge_ends += 1
                finds.append(Finding(1, "end_at_data_edge", "note",
                                     "path %d: %s, its %s at %s, meets nothing, at the edge of the map data read"
                                     % (p.id, namer.label(pe.seg), name, namer.point(pe.point)), namer.point(pe.point),
                                     [namer.info(pe.seg)]))
            elif m.kind in STRUCT:
                dangling += 1
                role = "a ramp: " if (m.cls or "").endswith("_link") else ""
                finds.append(Finding(1, "dangling_end", "fail",
                                     "%s%s (path %d): its %s at %s meets nothing (hangs in the air)"
                                     % (role, namer.label(pe.seg), p.id, name, namer.point(pe.point)), namer.point(pe.point),
                                     [namer.info(pe.seg)]))
            else:
                dead += 1
    kinds = {}
    for p in P.paths:
        key = "+".join(sorted({r.kind for r in p.runs}))
        kinds[key] = kinds.get(key, 0) + 1
    segs = sum(len(p.segs) for p in P.paths)
    stats = dict(segments=len(net.m), usable=len(net.m) - len(P.skipped), in_paths=segs, paths=len(P.paths),
                 closed=sum(1 for p in P.paths if p.closed), joints=len(P.joints),
                 pairs=sum(len(j.paired) for j in P.joints), longest_m=round(max([p.length for p in P.paths] + [0.0]), 0),
                 by_kinds=dict(sorted(kinds.items())), dangling_ends=dangling, dead_ends=dead, edge_ends=edge_ends,
                 oneway_known=sum(1 for p in P.paths if p.flow), fails=sum(1 for f in finds if f.level == "fail"))
    return finds, stats


def _duplicates(P, namer):
    """What is left of the same line twice once the road's own copies are collapsed (clean): two roads with one line (the map's),
    or one road's parts over one another that no lane sections explain."""
    net = P.net
    out = []
    for a, b, whole, over, same, _ina, _inb in overlap_pairs(net):
        out.append(Finding(1, "duplicate" if whole else "overlap", "fail",
                           ("the same line twice" if whole else "one road's parts over one another for %.0f m" % over)
                           + (" (two roads of the map)" if not same else "") + ": %s and %s" % (namer.label(a), namer.label(b)),
                           namer.at(a, net.L[a] / 2), [namer.info(a), namer.info(b)]))
    return out


def describe(P, namer, p):
    """One line for a path: its segments' OSM ways, kinds, length and ends."""
    net = P.net
    ways = []
    for i, _f in p.segs:
        w = namer.md(i).get("osm") or namer.earth(i)[:8]
        if not ways or ways[-1] != w:
            ways.append(w)
    kinds = "+".join("%s%s" % (r.kind, "" if r.kind == "ground" else " L%g" % r.level) for r in p.runs)
    ends = "closed" if p.closed else "%s to %s" % (namer.point(p.start.point), namer.point(p.stop.point))
    return "path %d: %s, %.0f m, %d segments (%s), %s" % (p.id, kinds, p.length, len(p.segs), ", ".join(ways[:6]) +
                                                          (" ..." if len(ways) > 6 else ""), ends)
