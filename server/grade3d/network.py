"""Step 1 of the 3D solve: an area's roads and tracks as whole paths, with where they join and where they cross.

A member (gradeshapes.Member) is one OSM way's stretch of one kind: a road on the ground, a deck, a tunnel, a track, a
track's deck. A path is what a vehicle drives through: members chained end to end where exactly two ends of one family
(roads, or tracks) meet and nothing else touches there, so a road keeps one path across its bridge and into its tunnel,
and a path ends at a fork, a merge, a T, a dead end. Its members keep their kinds: a path's samples are a deck's where
its deck is.

    area(planner, lo, hi) -> [Member]       the members of the map cells over a box (frame metres), as the build reads them
    build(members) -> Network               paths, joins and crossings

Joins and crossings are those of the line network (linenet, gradeshapes._relations): a join is an end meeting another
member (end to end, or on its middle: a T); a crossing is two members crossing in plan away from their ends. The joins
inside a path (its chain's own) are not listed: the path is continuous there by construction."""
from dataclasses import dataclass, field

import numpy as np

import gradeshapes as _gsh

ROAD_KINDS = ("ground", "deck", "tunnel")
RAIL_KINDS = ("rail", "rail_deck")


def family(kind):
    """"road" or "rail": what a member may be chained with."""
    return "rail" if kind in RAIL_KINDS else "road"


@dataclass
class Path:
    """A chain of members end to end. parts: [(member, forward)] in order (forward: the member's own direction along
    the path); start: the path's arc length where each part begins; length: the whole's (m)."""
    parts: list
    start: np.ndarray
    length: float

    def members(self):
        return [i for i, _f in self.parts]


@dataclass
class Network:
    """members: [gradeshapes.Member]; net: their gradeshapes.Net (lines, arc lengths, the line network); paths: [Path]
    (in order of their first member's key); at: {member: (path, part index)}; joins: [(member a, s on a, member b, s on
    b)] each once, a < b, those inside a path left out; crossings: [(member a, s on a, member b, s on b, q (2,))] each
    once, a < b."""
    members: list
    net: object
    paths: list
    at: dict
    joins: list = field(default_factory=list)
    crossings: list = field(default_factory=list)

    def path_s(self, i, s):
        """(path, arc length along it) of member i's arc length s."""
        p, k = self.at[i]
        path = self.paths[p]
        _i, forward = path.parts[k]
        L = float(self.net.L[i])
        return p, float(path.start[k] + (s if forward else L - s))


def area(planner, lo, hi):
    """The members of the map cells over the box lo..hi (frame metres, (2,) each), as the interchange pipeline reads
    them (ix/s1_paths: each key's longest line, a road's lane sections in place of its unsplit copy)."""
    from ix import s1_paths
    byk, loaded = {}, set()
    lo, hi = np.asarray(lo, np.float64), np.asarray(hi, np.float64)
    s1_paths.load(planner, lo, hi, byk, loaded)
    return s1_paths.members_of(byk, lo, hi)


def relations(net):
    """[(i, j, kind, s on i, s on j, q)] of every member with every other it joins or crosses, each pair's contact once
    (i < j), in member order."""
    out = []
    for i in range(len(net.m)):
        for j, kind, si, sj, q in _gsh._relations(net, i):
            if j > i:
                out.append((i, j, kind, float(si), float(sj), q))
    return out


def _end(net, i, s):
    """0 (its start), 1 (its end) or None: which end of member i arc length s is at (within JOIN_M)."""
    L = float(net.L[i])
    if s <= _gsh.JOIN_M:
        return 0
    if s >= L - _gsh.JOIN_M:
        return 1
    return None


def chain(net, rel):
    """[Path] of the members of net chained where exactly two ends of one family meet and nothing else touches: rel is
    relations(net). A ring of such meetings is cut at its member of the least key."""
    n = len(net.m)
    par = list(range(2 * n))            # (an end: 2 i + 0 its start, 2 i + 1 its end)

    def root(a):
        while par[a] != a:
            par[a] = par[par[a]]
            a = par[a]
        return a
    busy = set()                        # (ends something touches on its middle, or another family meets)
    for i, j, kind, si, sj, _q in rel:
        if kind != "join":
            continue
        ei, ej = _end(net, i, si), _end(net, j, sj)
        if ei is not None and ej is not None:
            a, b = root(2 * i + ei), root(2 * j + ej)
            if a != b:
                par[max(a, b)] = min(a, b)
            if family(net.m[i].kind) != family(net.m[j].kind):
                busy |= {2 * i + ei, 2 * j + ej}
        else:                           # (a T: the end on the other's middle meets a road that goes on)
            for m_, e_ in ((i, ei), (j, ej)):
                if e_ is not None:
                    busy.add(2 * m_ + e_)
    groups = {}
    for e in range(2 * n):
        groups.setdefault(root(e), []).append(e)
    nxt = {}                            # (end -> the other member's end it continues into)
    for g in groups.values():
        if len(g) == 2 and g[0] // 2 != g[1] // 2 and not (set(g) & busy):
            nxt[g[0]], nxt[g[1]] = g[1], g[0]
    key = [str(m.key) for m in net.m]
    seen = [False] * n
    paths = []
    order = sorted(range(n), key=lambda i: (key[i], i))

    def walk(i, forward):
        parts = []
        while True:
            seen[i] = True
            parts.append((i, forward))
            out_end = 2 * i + (1 if forward else 0)
            o = nxt.get(out_end)
            if o is None or seen[o // 2]:
                return parts
            i, forward = o // 2, o % 2 == 0     # (entered at its start: forward)
    for i in order:                     # (a path from each free end: a member whose start has no continuation)
        if seen[i] or len(net.lines[i]) < 2:
            continue
        if (2 * i) in nxt and (2 * i + 1) in nxt:
            continue                    # (inside a chain: reached from its free end, or a ring, below)
        forward = (2 * i) not in nxt
        paths.append(walk(i, forward))
    for i in order:                     # (rings: cut at the least key)
        if not seen[i] and len(net.lines[i]) >= 2:
            paths.append(walk(i, True))
    for i in order:                     # (a member of one point: a path of its own, of no length)
        if not seen[i]:
            seen[i] = True
            paths.append([(i, True)])
    out = []
    for parts in sorted(paths, key=lambda ps: min((key[i], i) for i, _f in ps)):
        lens = np.array([float(net.L[i]) if len(net.lines[i]) >= 2 else 0.0 for i, _f in parts])
        start = np.r_[0.0, np.cumsum(lens)[:-1]]
        out.append(Path(parts, start, float(lens.sum())))
    return out


def build(members, network=None):
    """The Network of members: their Net (on the line network: the process's, or network), every member's joins and
    crossings, and the members chained into paths."""
    net = _gsh.Net(members, network)
    rel = relations(net)
    paths = chain(net, rel)
    at = {}
    for p, path in enumerate(paths):
        for k, (i, _f) in enumerate(path.parts):
            at[i] = (p, k)
    inside = set()
    for path in paths:
        for (a, fa), (b, _fb) in zip(path.parts, path.parts[1:]):
            inside.add((min(a, b), max(a, b)))
    joins, crossings = [], []
    for i, j, kind, si, sj, q in rel:
        if kind == "join":
            if (i, j) in inside and _end(net, i, si) is not None and _end(net, j, sj) is not None:
                continue
            joins.append((i, si, j, sj))
        else:
            crossings.append((i, si, j, sj, np.asarray(q, np.float64)))
    return Network(members, net, paths, at, joins, crossings)
