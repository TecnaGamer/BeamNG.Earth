"""Step 1 of the 3D solve: an area's roads and tracks as whole paths, with where they join and where they cross.

A member (gradeshapes.Member) is one OSM way's stretch of one kind: a road on the ground, a deck, a tunnel, a track, a
track's deck. A path is what a vehicle drives through: members chained end to end where exactly two ends of one family
(roads, or tracks) meet and nothing else touches there, so a road keeps one path across its bridge and into its tunnel,
and a path ends at a fork, a merge, a T, a dead end. Its members keep their kinds: a path's samples are a deck's where
its deck is.

    area(planner, lo, hi) -> [Member]       the members of the map cells over a box (frame metres), as the build reads them
    one_way(members) -> [Member]            each way once: its cells' overlapping copies fused, its deck's stretch cut out
                                            of the track or road it lies along
    build(members) -> Network               paths, joins and crossings (of one_way's members)

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


OVERLAP_M = _gsh.JOIN_M      # m: a point this close to another copy of its way's line is on it
DENSE_M = 2.0               # m: a copy's line is read every this many metres (a long straight segment has no points inside)


def way_of(m):
    """(family, OSM way) of member m: a track's key ("rail", kind, way, ...), a road's (way[~lanes...], ...)."""
    k = m.key
    if isinstance(k, tuple) and len(k) > 2 and k[0] == "rail":
        return "rail", str(k[2])
    return "road", str(k[0] if isinstance(k, tuple) else k).split("~lanes")[0]


def _dense(line, step=DENSE_M):
    line = np.asarray(line, np.float64)[:, :2]
    if len(line) < 2:
        return line
    d = np.hypot(*np.diff(line, axis=0).T)
    k = np.maximum(1, np.ceil(d / step).astype(np.int64))
    if (k == 1).all():
        return line
    seg = np.repeat(np.arange(len(k)), k)
    u = (np.arange(len(seg)) - np.repeat(np.r_[0, np.cumsum(k)[:-1]], k) + 1) / np.repeat(k, k)
    return np.concatenate([line[:1], line[seg] + (line[seg + 1] - line[seg]) * u[:, None]])


def near_line(points, line, tol=OVERLAP_M):
    """bool (n,): the points within tol of the vertices of line, a line read every DENSE_M at most (_dense: then within
    tol of a vertex is within tol of the line, and within tol of the line within sqrt(tol^2 + (DENSE_M / 2)^2) of a
    vertex). A grid of tol over line's vertices, each point looking in the nine cells round its own."""
    points, line = np.asarray(points, np.float64)[:, :2], np.asarray(line, np.float64)[:, :2]
    out = np.zeros(len(points), bool)
    if not len(points) or not len(line):
        return out
    lo, hi = line.min(0) - tol, line.max(0) + tol
    inb = np.flatnonzero(((points >= lo) & (points <= hi)).all(1))
    if not len(inb):
        return out
    cell = float(tol)
    org = lo
    lk = np.floor((line - org) / cell).astype(np.int64)
    W = int(lk[:, 0].max()) + 3
    lkey = (lk[:, 0] + 1) * W + (lk[:, 1] + 1)
    order = np.argsort(lkey, kind="stable")
    skey, sv = lkey[order], line[order]
    pts = points[inb]
    pk = np.floor((pts - org) / cell).astype(np.int64)
    hit = np.zeros(len(pts), bool)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            key = (pk[:, 0] + 1 + dx) * W + (pk[:, 1] + 1 + dy)
            a, b = np.searchsorted(skey, key, "left"), np.searchsorted(skey, key, "right")
            cnt = b - a
            todo = np.flatnonzero((cnt > 0) & ~hit)
            if not len(todo):
                continue
            rep = np.repeat(todo, cnt[todo])
            first = np.repeat(a[todo] - np.r_[0, np.cumsum(cnt[todo])[:-1]], cnt[todo])
            idx = first + np.arange(len(rep))
            d = np.hypot(*(sv[idx] - pts[rep]).T)
            close = rep[d <= tol]
            hit[close] = True
    out[inb] = hit
    return out


def _runs(cov):
    """[(a, b)]: the index ranges (inclusive) of the uncovered points, each widened by the covered point next to it (so a
    piece cut off still touches what covers it)."""
    out, n, i = [], len(cov), 0
    while i < n:
        if cov[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and not cov[j + 1]:
            j += 1
        out.append((max(0, i - 1), min(n - 1, j + 1)))
        i = j + 1
    return out


def _fuse(ms):
    """One kind's copies of one way: the longest kept, each other copy's stretch along a kept one dropped, what goes on
    past a kept one's end added to it, what is left a piece of its own."""
    kept = []                   # ([line], template member)
    work = [(_dense(m.line), m) for m in sorted(ms, key=lambda m: -float(np.hypot(*np.diff(
        np.asarray(m.line, np.float64)[:, :2], axis=0).T).sum()) if len(m.line) > 1 else 0.0)]
    while work:
        line, m = work.pop(0)
        placed = False
        for k in kept:
            cov = near_line(line, k[0])
            if not cov.any():
                continue
            placed = True
            if cov.all():
                break
            for a, b in _runs(cov):
                run = line[a:b + 1]
                if len(run) < 2:
                    continue
                head_cov, tail_cov = bool(cov[a]), bool(cov[b])
                end, start = k[0][-1], k[0][0]
                if head_cov and np.hypot(*(run[0] - end)) <= OVERLAP_M:
                    k[0] = np.concatenate([k[0], run[1:]])
                elif tail_cov and np.hypot(*(run[-1] - end)) <= OVERLAP_M:
                    k[0] = np.concatenate([k[0], run[::-1][1:]])
                elif tail_cov and np.hypot(*(run[-1] - start)) <= OVERLAP_M:
                    k[0] = np.concatenate([run[:-1], k[0]])
                elif head_cov and np.hypot(*(run[0] - start)) <= OVERLAP_M:
                    k[0] = np.concatenate([run[::-1][:-1], k[0]])
                else:
                    work.append((run, m))
            break
        if not placed:
            kept.append([line, m])
    out = []
    for n, (line, m) in enumerate(kept):
        key = m.key if n == 0 else (m.key if isinstance(m.key, tuple) else (m.key,)) + ("copy", n)
        out.append(m._replace(key=key, line=line) if len(kept) > 1 or len(line) != len(m.line) else m)
    return out


def _cut(base, structures):
    """base (a road on the ground, a track) without its stretches along its own way's structures: [pieces]. Along a
    structure is along its middle: a road that only meets the structure's end (its approach) keeps all of its line,
    its end where the structure's is (the structure's vertices within OVERLAP_M of its own ends are not read)."""
    line = _dense(base.line)
    cov = np.zeros(len(line), bool)
    for s in structures:
        sl = _dense(s.line)
        if len(sl) >= 2:
            far = (np.hypot(*(sl - sl[0]).T) > OVERLAP_M) & (np.hypot(*(sl - sl[-1]).T) > OVERLAP_M)
            sl = sl[far]
        if len(sl):
            cov |= near_line(line, sl)
    if not cov.any():
        return [base]
    if cov.all():
        return []
    key = base.key if isinstance(base.key, tuple) else (base.key,)
    return [base._replace(key=key + ("cut", n), line=line[a:b + 1])
            for n, (a, b) in enumerate(_runs(cov)) if b > a]


def one_way(members):
    """members with each way once: the copies of a way of one kind (its clips from several cells, a road twice from its
    lane sections) fused into one line; a way's road or track without the stretch its own deck or tunnel lies along (the
    structure is the way there). Members of no other way's are untouched."""
    groups = {}
    for i, m in enumerate(members):
        groups.setdefault(way_of(m), []).append(i)
    out = []
    for _w, idx in groups.items():
        ms = [members[i] for i in idx]
        if len(ms) == 1:
            out.append(ms[0])
            continue
        by_kind = {}
        for m in ms:
            by_kind.setdefault(m.kind, []).append(m)
        fused = {k: (_fuse(v) if len(v) > 1 else v) for k, v in by_kind.items()}
        structures = [m for k, v in fused.items() if k in _gsh.STRUCTURES for m in v]
        for k, v in fused.items():
            for m in v:
                out.extend(_cut(m, structures) if structures and k not in _gsh.STRUCTURES else [m])
    return out


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


THROUGH_COS = np.cos(np.radians(30.0))     # two ends at a fork this near straight on are one road going on through
END_LOOK_M = 10.0           # m into a member its direction at an end is read over


def _out(net, e):
    """Unit direction from end e (2 i: member i's start, 2 i + 1 its end) into its member, over END_LOOK_M."""
    ln = np.asarray(net.lines[e // 2], np.float64)[:, :2]
    if e % 2:
        ln = ln[::-1]
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(ln, axis=0).T))]
    k = int(min(len(ln) - 1, max(1, np.searchsorted(s, END_LOOK_M))))
    d = ln[k] - ln[0]
    n = float(np.hypot(*d))
    return d / n if n > 0 else None


def through(net, ends):
    """(a, b): of the ends meeting at a fork or merge (three or more, of one family), the two of different members that
    go on most nearly straight (their directions into their members within 30 degrees of opposite), the fastest class's
    first; None when no two do. The motorway through a diverge is one path, its ramp leaving it: two paths ending there
    joined it at a height only, and its decks kinked there (S1 at Breitenleer and Rautenweg: crests of 20-40x the
    drivable lift on the solve's own line)."""
    rank = {c: k for k, c in enumerate(("motorway", "trunk", "primary", "secondary", "tertiary"))}
    best = None
    dirs = {e: _out(net, e) for e in ends}
    for x in range(len(ends)):
        for y in range(x + 1, len(ends)):
            a, b = ends[x], ends[y]
            if a // 2 == b // 2 or dirs[a] is None or dirs[b] is None:
                continue
            ma, mb = net.m[a // 2], net.m[b // 2]
            if family(ma.kind) != family(mb.kind):
                continue
            c = float(dirs[a] @ dirs[b])
            if c > -THROUGH_COS:
                continue
            cls = min(rank.get(str(ma.cls or "").replace("_link", ""), 9), rank.get(str(mb.cls or "").replace("_link", ""), 9))
            k = (cls, c)
            if best is None or k < best[0]:
                best = (k, (a, b))
    return None if best is None else best[1]


def chain(net, rel):
    """[Path] of the members of net chained where exactly two ends of one family meet and nothing else touches, and
    through a fork or merge where two of its ends go on straight (through): rel is relations(net). A ring of such
    meetings is cut at its member of the least key."""
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
        elif len(g) >= 3:               # (a fork or a merge: the road going straight on goes on through it)
            pair = through(net, [e for e in g if e not in busy])
            if pair is not None:
                nxt[pair[0]], nxt[pair[1]] = pair[1], pair[0]
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
    """The Network of members (one_way's of them): their Net (on the line network: the process's, or network), every
    member's joins and crossings, and the members chained into paths."""
    members = one_way(members)
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
