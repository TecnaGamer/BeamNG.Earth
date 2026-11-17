"""Tunnel and bridge grading: each interchange solved as one shape, then placed on the ground with its embankments
(BNE_SHAPES=1, off by default).

1. The system. Every road deck, short road tunnel (an underpass) and short rail deck, with what it joins and what it
   crosses, is one interchange with every other one it joins end to end, crosses, or meets through a short road
   between them (a ramp from one deck to the next). A single bridge or tunnel is a system of its own.

2. Who moves, at each crossing of an upper part over a lower one:
   - the lower road is mapped in a tunnel (an underpass): it goes down, the upper stays at its level (a track or a road
     at grade over it stays on its ground);
   - the upper is a rail deck: a track cannot ramp, so a short rail deck (RAIL_LONG_M) stays where the track is and the
     road under goes down; a long one is a viaduct the railways raise themselves, nothing to solve here;
   - the upper is a road deck: a short deck chain (ROAD_SHORT_M) over a road is the road dug under it; a longer one is
     an overpass that rises over the road under, which keeps its ground;
   - a track is never dug: the road deck over it rises.

3. The shape. The heights of the system's decks, and of the roads that go down under its crossings, in one problem:
   every crossing clears the vehicle height the road under needs (its signed limit, else by class; rail's catenary) plus
   the deck; every deck holds the grade its class and speed allow and the joints between decks meet. Within those, the
   decks' own slopes are free, chosen for the least earthwork: the smallest embankment (or cut) where the shape meets
   the roads on the ground at its ends, and the shallowest dip for a road going under. What must be at ground level (a
   road under an overpass, a track over an underpass) is at its ground: the shape is placed on it.

4. The embankments. Where the shape's end does not meet the road on the ground it joins, that road leading up to it is
   an earth embankment (or a cut, for a road going down) as steep as the road's max grade, as long as it takes to
   intercept the ground: the touch point is where the ramp meets the ground, a result, never an anchor. A road going
   under is cut down to its dip and back up the same way. Two ends whose ramps meet before the ground does (systems
   close to each other on one road, single bridges included) are joined by the straight line between them (lerped).

Pure: members in, shapes and ramps out. grading.py reads the members from its cells and applies the result to its
plans (the decks through roads.DECK_Z_HOOK); the terrain follows the plans (cut and fill).
"""
import hashlib
import math
from collections import namedtuple

import numpy as np

import dnp

STEP_M = 4.0                # m between a deck's or a dip's samples
JOIN_M = 2.5                # m: two parts' ends (or an end and another's line) this close meet there
CROSS_END_M = 2.0           # m: a crossing this near either part's end is a joint, not a separation
# the game's decks (roads.py: MIN_BRIDGE_HEADROOM_M, BRIDGE_HEADROOM_MARGIN_M, deck_depth; grading sets them from there):
# the vehicle height under the girders, its margin, and the deck's depth by its span (slab + girders)
HEADROOM_M = 6.0
HEADROOM_MARGIN_M = 0.15
DECK_MIN_DEPTH_M = 0.9
DECK_FULL_DEPTH_M = 2.2
DECK_SPAN_RATIO = 22.0
COVER_M = 1.0               # m of structure over an underpass's headroom (the roof under a road or track at grade)
TUNNEL_MAX_M = 300.0        # m: a longer road tunnel is a tunnel network of its own (tunnels.py), not an underpass
ROAD_SHORT_M = 20.0         # m: a road deck chain this short over a road is the road dug under it
RAIL_LONG_M = 60.0          # m: a rail deck this long is a viaduct (the railways raise it), shorter the road goes under
CONNECT_M = 400.0           # m: a road joining two decks' (or tunnels') ends no longer than this ties them in one system
RAMP_MAX_M = 600.0          # m: the longest embankment or cut followed along a road
DIP_MARGIN_M = 3.0          # m past the upper part's edge a road going under holds its dip before its cut ramps up
SOFT_M = 0.6                # m: the rounding where an embankment meets the ground (a soft maximum)

CLEAR_PATH_M = 3.0          # m: a path's headroom (no vehicle on it)

W_END = 1.0                 # a system end's embankment (per m, squared)
W_HOLD = 100.0              # a short deck chain (a road dug under it) at its ground
W_DIP = 0.5                 # a road's dip under its crossing
W_SMOOTH = 0.2
W_LAW = 3000.0              # a grade, curve or clearance law broken (penalty: always an answer; it outranks every cost)
W_JOIN = 300.0
ITERS = 80

Member = namedtuple("Member", "key line kind cls kmh level half limit")
# key: hashable, unique, the same from every cell; line (k, 2) frame ENU; kind: "ground", "deck", "tunnel", "rail",
# "rail_deck"; cls: road class ("rail" for track); kmh: speed limit or None; level: OSM layer of a deck; half: half
# width (m); limit: the road's signed height limit (m) or None

PATHS = {"footway", "path", "cycleway", "pedestrian", "steps", "bridleway", "track"}
MINOR = {"residential", "service", "unclassified", "living_street", "road"}
DEFAULT_KMH = {"motorway": 110, "trunk": 90, "primary": 70, "secondary": 60, "tertiary": 50, "residential": 40,
               "unclassified": 50, "service": 20, "living_street": 15, "road": 40, "track": 20}


def _base(cls):
    c = cls or "road"
    return c[:-5] if c.endswith("_link") else c


def max_grade(m):
    """The steepest grade member m may hold, by class and speed (AASHTO's design grades for a speed: 4 % at 120 km/h
    to 15 % at 20); a link at least 6 %; a path 12 %; a track 2.5 %."""
    if m.kind in ("rail", "rail_deck"):
        return 0.025
    if m.cls in PATHS:
        return 0.12
    kmh = m.kmh or DEFAULT_KMH.get(_base(m.cls), 40)
    g = float(np.interp(kmh, [20, 30, 40, 50, 60, 80, 100, 120], [0.15, 0.12, 0.10, 0.09, 0.08, 0.06, 0.05, 0.04]))
    if (m.cls or "").endswith("_link"):
        g = max(g, 0.06)
    return g


A_VERT = 0.3               # m/s2: the vertical acceleration a road's crest or sag may give at its speed (comfort)
A_VERT_RAIL = 0.15         # m/s2 for a train
R_MIN_ROAD_M = 60.0        # m: the tightest vertical curve on any road (a path's, a service road's)
R_MIN_RAIL_M = 2000.0      # m: the tightest on a track


def curve_radius(m):
    """The tightest vertical curve (radius, m) member m may hold at its speed: v^2 / a (a sudden change of grade is
    smoothed over radius x change of grade, the longer the faster the road), a track's never under R_MIN_RAIL_M."""
    if m.kind in ("rail", "rail_deck"):
        v = (m.kmh or 100.0) / 3.6
        return max(R_MIN_RAIL_M, v * v / A_VERT_RAIL)
    if m.cls in PATHS:
        return R_MIN_ROAD_M
    v = (m.kmh or DEFAULT_KMH.get(_base(m.cls), 40)) / 3.6
    return max(R_MIN_ROAD_M, v * v / A_VERT)


def headroom(m):
    """The vehicle height member m needs under a deck, with its margin: its signed limit where it has one (a
    maxheight sign), else the game's tallest vehicles' (a track's catenary the same), a path's less."""
    if m.limit is not None and m.limit > 0:
        return float(m.limit) + HEADROOM_MARGIN_M
    if m.cls in PATHS and m.kind not in ("rail", "rail_deck"):
        return CLEAR_PATH_M
    return HEADROOM_M + HEADROOM_MARGIN_M


def deck_depth(span):
    """m of structure under a deck's road for a span of span m (the game's: span / 22, 0.9 to 2.2 m)."""
    return float(np.clip(float(span) / DECK_SPAN_RATIO, DECK_MIN_DEPTH_M, DECK_FULL_DEPTH_M))


def clearance_need(net, u, d):
    """How far the road on upper part u stands over lower part d where they cross: the headroom d needs and u's
    structure (a deck's depth for its span, an underpass's roof)."""
    mu = net.m[u]
    struct = deck_depth(net.L[u]) if mu.kind in ("deck", "rail_deck") else COVER_M
    return headroom(net.m[d]) + struct


def arclen(line):
    return np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(line, axis=0).T))]


def _seg_hits(a, b):
    """Plan crossings of polylines a and b: [(i, j, ta, tb, q)]."""
    p, r = a[:-1], a[1:] - a[:-1]
    q, sv = b[:-1], b[1:] - b[:-1]
    lo_a, hi_a = np.minimum(a[:-1], a[1:]), np.maximum(a[:-1], a[1:])
    lo_b, hi_b = np.minimum(b[:-1], b[1:]), np.maximum(b[:-1], b[1:])
    ii, jj = np.nonzero(((lo_a[:, None, :] <= hi_b[None, :, :]) & (hi_a[:, None, :] >= lo_b[None, :, :])).all(2))
    if not len(ii):
        return []
    R, S, D = r[ii], sv[jj], q[jj] - p[ii]
    den = R[:, 0] * S[:, 1] - R[:, 1] * S[:, 0]
    ok = np.abs(den) > 1e-12
    den = np.where(ok, den, 1.0)
    ta = (D[:, 0] * S[:, 1] - D[:, 1] * S[:, 0]) / den
    tb = (D[:, 0] * R[:, 1] - D[:, 1] * R[:, 0]) / den
    hit = ok & (ta >= 0.0) & (ta <= 1.0) & (tb >= 0.0) & (tb <= 1.0)
    return [(int(ii[k]), int(jj[k]), float(ta[k]), float(tb[k]), p[ii[k]] + ta[k] * r[ii[k]]) for k in np.flatnonzero(hit)]


def proj(line, s, q):
    """Arc length along line (arc lengths s) of the point nearest q, and the distance."""
    a, b = line[:-1], line[1:]
    ab = b - a
    L2 = np.maximum((ab * ab).sum(1), 1e-12)
    t = np.clip(((q - a) * ab).sum(1) / L2, 0.0, 1.0)
    d = dnp.hypot(*(a + t[:, None] * ab - q).T)
    j = int(np.argmin(d))
    return float(s[j] + t[j] * (s[j + 1] - s[j])), float(d[j])


def at_s(line, s, sq):
    """The point at arc length sq along line."""
    return np.array([np.interp(sq, s, line[:, 0]), np.interp(sq, s, line[:, 1])])


class Net:
    """The members' geometry, made once: lines, arc lengths, boxes."""

    def __init__(self, members):
        self.m = members
        self.lines = [np.asarray(m.line, np.float64)[:, :2] for m in members]
        self.s = [arclen(l_) for l_ in self.lines]
        self.L = [float(s[-1]) for s in self.s]
        self.box = np.array([np.r_[l_.min(0), l_.max(0)] for l_ in self.lines]) if members else np.zeros((0, 4))

    def near(self, i, pad):
        b = self.box[i]
        bx = self.box
        return [int(j) for j in np.flatnonzero((bx[:, 0] <= b[2] + pad) & (bx[:, 2] >= b[0] - pad) &
                                              (bx[:, 1] <= b[3] + pad) & (bx[:, 3] >= b[1] - pad)) if j != i]


def _is_seed(net, i):
    m = net.m[i]
    if m.kind == "deck":
        return True
    if m.kind == "tunnel":
        return net.L[i] <= TUNNEL_MAX_M
    if m.kind == "rail_deck":
        return net.L[i] < RAIL_LONG_M
    return False


def _relations(net, i):
    """Member i's joints and crossings with the members near it: [(j, "join"|"cross", s on i, s on j, q)]."""
    out = []
    for j in net.near(i, JOIN_M + 0.5):
        if len(net.lines[j]) < 2:
            continue
        for e_line, e_s, o_line, o_s, swap in ((net.lines[i], net.s[i], net.lines[j], net.s[j], False),
                                                (net.lines[j], net.s[j], net.lines[i], net.s[i], True)):
            for se in (0.0, float(e_s[-1])):
                q = e_line[0] if se == 0.0 else e_line[-1]
                so, d = proj(o_line, o_s, q)
                if d <= JOIN_M:
                    out.append((j, "join", so if swap else se, se if swap else so, q))
        # (one joint found from both its parts' ends once)
        uniq = []
        for r_ in out:
            if not any(u[0] == r_[0] and u[1] == r_[1] and abs(u[2] - r_[2]) < 1.0 and abs(u[3] - r_[3]) < 1.0
                       for u in uniq):
                uniq.append(r_)
        out[:] = uniq
        for a_i, b_j, ta, tb, q in _seg_hits(net.lines[i], net.lines[j]):
            si = net.s[i][a_i] + ta * (net.s[i][a_i + 1] - net.s[i][a_i])
            sj = net.s[j][b_j] + tb * (net.s[j][b_j + 1] - net.s[j][b_j])
            if min(si, net.L[i] - si, sj, net.L[j] - sj) < CROSS_END_M:
                continue
            if any(r_[0] == j and r_[1] == "cross" and abs(r_[2] - si) < 1.0 for r_ in out):
                continue                        # (on a vertex: found on both its segments)
            out.append((j, "cross", si, sj, q))
    return out


def _over(mi, mj):
    """Of two crossing members, which is over: "i", "j", or None (at grade)."""
    up = {"deck": 2, "rail_deck": 2, "ground": 1, "rail": 1, "tunnel": 0}
    a, b = up[mi.kind], up[mj.kind]
    if a == b:
        if a == 2 and mi.level != mj.level:
            return "i" if mi.level > mj.level else "j"
        return None
    return "i" if a > b else "j"


def find_systems(members):
    """The interchanges among members: [System], each closed, in a deterministic order."""
    net = Net(members)
    n = len(members)
    seeds = [i for i in range(n) if _is_seed(net, i) and len(net.lines[i]) >= 2]
    if not seeds:
        return net, []
    rel = {i: _relations(net, i) for i in seeds}
    par = {i: i for i in seeds}

    def root(i):
        while par[i] != i:
            par[i] = par[par[i]]
            i = par[i]
        return i

    def unite(a, b):
        ra, rb = root(a), root(b)
        if ra != rb:
            par[max(ra, rb, key=lambda x: str(members[x].key))] = min(ra, rb, key=lambda x: str(members[x].key))
    ends_of = {}            # ground member -> the seeds whose ends join it (a ramp from one deck to the next)
    for i in seeds:
        for j, kind, si, sj, q in rel[i]:
            if j in par:
                unite(i, j)
            elif kind == "join" and members[j].kind == "ground":
                ends_of.setdefault(j, set()).add(i)
    for j, ss in ends_of.items():
        if len(ss) >= 2 and net.L[j] <= CONNECT_M:
            ss = sorted(ss)
            for i in ss[1:]:
                unite(ss[0], i)
    groups = {}
    for i in seeds:
        groups.setdefault(root(i), []).append(i)
    systems = []
    for r in sorted(groups, key=lambda x: str(members[x].key)):
        systems.append(_system(net, sorted(groups[r], key=lambda x: str(members[x].key)), rel))
    return net, systems


def _system(net, seeds, rel):
    """One interchange: its decks and tunnels, its crossings with who moves, its ends on the roads on the ground."""
    members = net.m
    S = set(seeds)
    # deck chains: decks joined end to end (their total length decides an overpass from a road dug under it)
    par = {i: i for i in seeds}

    def root(i):
        while par[i] != i:
            par[i] = par[par[i]]
            i = par[i]
        return i
    for i in seeds:
        for j, kind, si, sj, q in rel[i]:
            if kind == "join" and j in S and members[i].kind == members[j].kind == "deck":
                par[root(i)] = root(j)
    chain_len = {}
    for i in seeds:
        chain_len[root(i)] = chain_len.get(root(i), 0.0) + net.L[i]
    joins, crossings, ends = [], [], []
    for i in seeds:
        for j, kind, si, sj, q in rel[i]:
            mi, mj = members[i], members[j]
            if kind == "join":
                if j in S:
                    if str(mi.key) < str(mj.key):
                        joins.append((i, si, j, sj))
                elif mj.kind == "ground":         # a road on the ground leading to the system: where a ramp starts
                    ends.append((i, si, j, sj))
                continue
            o = _over(mi, mj)
            if o is None:
                continue
            u, su, d, sd = (i, si, j, sj) if o == "i" else (j, sj, i, si)
            if j in S and str(mi.key) > str(mj.key):
                continue                        # (a crossing of two of its own parts, once)
            mu, md = members[u], members[d]
            if md.kind == "tunnel":
                moves = "down"                  # (mapped as an underpass: it goes under)
            elif mu.kind == "rail_deck":
                moves = "down" if md.kind == "ground" else None
            elif mu.kind == "deck":
                if md.kind in ("rail", "rail_deck"):
                    moves = "up"                # (a track is never dug)
                elif md.kind == "ground":
                    moves = "down" if chain_len[root(u)] < ROAD_SHORT_M else "up"
                else:
                    moves = "up"
            else:
                moves = None
            if moves is None:
                continue
            crossings.append(dict(u=u, su=su, d=d, sd=sd, q=np.asarray(q, np.float64), moves=moves))
    # the roads going under: per road, the window that dips (its crossings +- the upper part's half width and the cut
    # each side back up to the ground at the road's grade, through its curves), past its own ends if need be (the road
    # followed on through its joints); a tunnel whole, with its approaches the same way
    win = {}
    for c in crossings:
        if c["moves"] != "down":
            continue
        d = c["d"]
        md = members[d]
        g = max_grade(md)
        w = clearance_need(net, c["u"], d) / g + curve_radius(md) * g
        if md.kind == "tunnel":
            win.setdefault(d, []).append((-w, net.L[d] + w))
        else:
            w += members[c["u"]].half + DIP_MARGIN_M
            win.setdefault(d, []).append((c["sd"] - w, c["sd"] + w))
    dips = []
    for d in sorted(win, key=lambda i: str(members[i].key)):
        rs = sorted(win[d])
        cur = [list(rs[0])]
        for a, b in rs[1:]:
            if a <= cur[-1][1] + STEP_M:
                cur[-1][1] = max(cur[-1][1], b)
            else:
                cur.append([a, b])
        dips += [(d, float(a), float(b)) for a, b in cur]
    chain_of = {i: chain_len[root(i)] for i in seeds if members[i].kind == "deck"}
    return dict(seeds=seeds, joins=joins, crossings=crossings, ends=ends, dips=dips, chain_of=chain_of)


def system_key(net, sysm):
    """A system's identity: its parts' keys (the same from whichever cell found it)."""
    h = hashlib.sha1()
    for i in sorted(set(sysm["seeds"]) | {x[0] for x in sysm["dips"]}, key=lambda i: str(net.m[i].key)):
        h.update(repr(net.m[i].key).encode())
    return h.hexdigest()[:20]


# ---- paths: a road followed through its joints (a roundabout's ring, a street split at its junctions) ----

ROADS = ("ground", "tunnel")


def _ends_index(net):
    """{grid cell: [(member, at its end L?)]} of the roads' ends, made once."""
    idx = getattr(net, "_ends_idx", None)
    if idx is None:
        idx = {}
        for i, m in enumerate(net.m):
            if m.kind not in ROADS or len(net.lines[i]) < 2:
                continue
            for at_end in (False, True):
                p = net.lines[i][-1] if at_end else net.lines[i][0]
                idx.setdefault((int(p[0] // 5.0), int(p[1] // 5.0)), []).append((i, at_end))
        net._ends_idx = idx
    return idx


def next_member(net, i, at_end):
    """The road going on from member i's end (its end at L if at_end, else its start): (member, entering at its start?)
    or None: of the roads whose end is there, the same road (feature) first, else the straightest."""
    idx = _ends_index(net)
    line = net.lines[i]
    p = line[-1] if at_end else line[0]
    d_out = (line[-1] - line[-2]) if at_end else (line[0] - line[1])
    d_out = d_out / max(float(np.hypot(*d_out)), 1e-9)
    gx, gy = int(p[0] // 5.0), int(p[1] // 5.0)
    best = None
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for j, j_end in idx.get((gx + dx, gy + dy), ()):
                if j == i:
                    continue
                lj = net.lines[j]
                q = lj[-1] if j_end else lj[0]
                if float(np.hypot(*(q - p))) > JOIN_M:
                    continue
                d_in = (lj[-2] - lj[-1]) if j_end else (lj[1] - lj[0])
                d_in = d_in / max(float(np.hypot(*d_in)), 1e-9)
                same = net.m[j].key[0] == net.m[i].key[0]
                score = (0 if same else 1, -float(d_out @ d_in), str(net.m[j].key))
                if best is None or score < best[0]:
                    best = (score, j, not j_end)
    if best is None or best[0][1] > -0.5:       # (nothing going on within 60 degrees of straight on)
        return None
    return best[1], best[2]


def walk(net, i, at_end, length):
    """Segments [(member, s from, s to)] following the road on from member i's end (at L if at_end, else at 0) for
    length m, in the order walked."""
    out, left, cur, cur_end, seen = [], float(length), i, at_end, {i}
    while left > 1e-6:
        nx = next_member(net, cur, cur_end)
        if nx is None or nx[0] in seen:
            break
        j, from_start = nx
        seen.add(j)
        L = net.L[j]
        take = min(L, left)
        out.append((j, 0.0, take) if from_start else (j, L, L - take))
        left -= take
        cur, cur_end = j, from_start          # (leaving j by its other end)
    return out


def path_of(net, i, a, b):
    """The segments of the road through member i from arc length a to b (either may lie past its ends: the road
    followed on through its joints), in order."""
    L = net.L[i]
    segs = []
    if a < 0:
        back = walk(net, i, False, -a)
        segs += [(j, s1, s0) for j, s0, s1 in back[::-1]]
    segs.append((i, max(0.0, a), min(L, b)))
    if b > L:
        segs += walk(net, i, True, b - L)
    return segs


def pieces(net, sysm):
    """The shape's unknowns: [dict(kind "deck" or "dip", main member, segs)]: its road decks (whole), and per road
    going under its window followed along the road through its joints."""
    out = []
    for i in sysm["seeds"]:
        if net.m[i].kind == "deck":
            out.append(dict(kind="deck", main=i, segs=[(i, 0.0, net.L[i])]))
    for d, a, b in sysm["dips"]:
        out.append(dict(kind="dip", main=d, segs=path_of(net, d, a, b)))
    return out


def sample(net, pcs):
    """Per piece: dict(xy (m, 2), d (m,) along it, mem (m,) member, ms (m,) arc length on that member)."""
    out = []
    for pc in pcs:
        xs, ds, mem, ms, joints = [], [], [], [], []
        d0 = 0.0
        count = 0
        for n, (j, s0, s1) in enumerate(pc["segs"]):
            k = max(2, int(math.ceil(abs(s1 - s0) / STEP_M)) + 1)
            sv = np.linspace(s0, s1, k)
            if n:
                sv = sv[1:]                  # (the joint once: its sample the last part's, and this one's at s0)
                joints.append((count - 1, j, float(s0)))
            if not len(sv):
                continue
            xy = np.stack([np.interp(sv, net.s[j], net.lines[j][:, 0]), np.interp(sv, net.s[j], net.lines[j][:, 1])],
                          axis=1)
            if xs:
                d_here = d0 + np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(np.vstack([xs[-1][-1:], xy]), axis=0).T))][1:]
            else:
                d_here = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]
            xs.append(xy)
            ds.append(d_here)
            mem.append(np.full(len(sv), j))
            ms.append(sv)
            d0 = float(d_here[-1])
            count += len(sv)
        out.append(dict(xy=np.concatenate(xs), d=np.concatenate(ds), mem=np.concatenate(mem),
                        ms=np.concatenate(ms), joints=joints))
    return out


def _locate(smp, p, i, s_):
    """The two samples of piece p on member i bracketing arc length s_ there, with the weight of the second, or
    None."""
    x = smp[p]
    k = np.flatnonzero(x["mem"] == i)
    ms = x["ms"][k]
    extra = [(kk, s0) for kk, j, s0 in x.get("joints", ()) if j == i]
    if extra:                               # (a joint's sample is this member's too, at its end)
        k = np.r_[k, [e[0] for e in extra]].astype(np.int64)
        ms = np.r_[ms, [e[1] for e in extra]]
        o = np.argsort(ms, kind="stable")
        k, ms = k[o], ms[o]
    if len(k) < 2:
        return None
    lo_, hi_ = float(min(ms[0], ms[-1])), float(max(ms[0], ms[-1]))
    if not (lo_ - 1e-6 <= s_ <= hi_ + 1e-6):
        return None
    if ms[-1] < ms[0]:
        k, ms = k[::-1], ms[::-1]
    o = np.argsort(ms, kind="stable")
    k, ms = k[o], ms[o]
    r = int(np.clip(np.searchsorted(ms, s_) - 1, 0, len(ms) - 2))
    t = float(np.clip((s_ - ms[r]) / max(ms[r + 1] - ms[r], 1e-9), 0.0, 1.0))
    return int(k[r]), int(k[r + 1]), t


def _var(pcs, smp, i, s_):
    """(piece, sample a, sample b, weight of b) of member i's unknown height at arc length s_, or None."""
    for p in range(len(pcs)):
        got = _locate(smp, p, i, s_)
        if got is not None:
            return (p,) + got
    return None


def _overlaps(pcs, smp):
    """Samples of two pieces on the same stretch of road (two dips along one road): [(p, k, q, k2)] to hold equal."""
    out = []
    for q in range(len(pcs)):
        for k in range(len(smp[q]["mem"])):
            i, s_ = int(smp[q]["mem"][k]), float(smp[q]["ms"][k])
            for p in range(q):
                got = _locate(smp, p, i, s_)
                if got is not None:
                    a, b, t = got
                    out.append((p, a if t < 0.5 else b, q, k))
                    break
    return out


def _law(net, pc):
    m = net.m[pc["main"]]
    return max_grade(m), curve_radius(m)


def solve_shape(net, sysm, pcs, smp, ground, fixed_z, diagnostics=None):
    """The shape: per piece its heights. The laws (grade, vertical curve by speed, clearance, joints) are hard
    constraints of one quadratic programme (the Rust kernel's, carriageways.solve); the costs: the embankment at the
    shape's ends on the ground, the depth a road goes under to, a short deck held at its ground. Where the kernel finds
    no answer (laws that cannot all hold), the same problem with the laws as penalties (solve_shape_soft): always an
    answer."""
    try:
        got = _solve_shape_qp(net, sysm, pcs, smp, ground, fixed_z, diagnostics)
    except Exception as e:      # (no kernel, or it failed: the penalty solve)
        if diagnostics is not None:
            diagnostics["qp_error"] = f"{type(e).__name__}: {e}"
        got = None
    if got is not None:
        return got
    return solve_shape_soft(net, sysm, pcs, smp, ground, fixed_z)


def _deck_targets(net, sysm, pc, t):
    """A deck's target and weights: toward the line between its ends' ground, held there if its chain is short (a
    road is dug under it), else free but for the embankment where it meets the road on the ground."""
    i = pc["main"]
    n = len(t)
    joined = [s_ for a, sa, b, sb in sysm["joins"] for s_, m_ in ((sa, a), (sb, b)) if m_ == i]
    short = sysm["chain_of"].get(i, 0.0) < ROAD_SHORT_M
    target = t[0] + (t[-1] - t[0]) * np.arange(n) / max(n - 1, 1)
    w = np.full(n, W_HOLD if short else 1e-6)
    for k, s_end in ((0, 0.0), (n - 1, net.L[i])):
        if not any(abs(s_ - s_end) <= JOIN_M for s_ in joined):
            w[k] = W_HOLD if short else W_END
    return target, w


def _solve_shape_qp(net, sysm, pcs, smp, ground, fixed_z, diagnostics):
    import carriageways as _cw
    chains = []
    for pc, x, t in zip(pcs, smp, ground):
        t = np.asarray(t, np.float64)
        n = len(t)
        lo, hi = np.full(n, -np.inf), np.full(n, np.inf)
        if pc["kind"] == "deck":
            target, w = _deck_targets(net, sysm, pc, t)
        else:
            target, w = t.copy(), np.full(n, W_DIP)
            hi = t.copy()                                   # (a road going under never rises over its ground)
        g, rad = _law(net, pc)
        chains.append(dict(t=target, w=w, lo=lo, hi=hi, grade=g, K=rad / 100.0, lam=1e-3,
                           step=float((x["d"][-1] - x["d"][0]) / max(n - 1, 1)), length=0.0))
    cps = []
    for i, si, j, sj in sysm["joins"]:
        x, y = _var(pcs, smp, i, si), _var(pcs, smp, j, sj)
        if x and y:
            cps.append((x[0], x[1] if x[3] < 0.5 else x[2], y[0], y[1] if y[3] < 0.5 else y[2], "eq", 0.0))
    for p, a, q, b in _overlaps(pcs, smp):
        cps.append((p, a, q, b, "eq", 0.0))
    for c in sysm["crossings"]:
        need = clearance_need(net, c["u"], c["d"])
        x, y = _var(pcs, smp, c["u"], c["su"]), _var(pcs, smp, c["d"], c["sd"])
        if x and y:
            for kx in (x[1], x[2]):
                for ky in (y[1], y[2]):
                    cps.append((x[0], kx, y[0], ky, "ge", need))
        elif x:
            zd = fixed_z(c["d"], c["sd"])
            if zd is not None:
                for kx in (x[1], x[2]):
                    chains[x[0]]["lo"][kx] = max(chains[x[0]]["lo"][kx], zd + need)
        elif y:
            zu = fixed_z(c["u"], c["su"])
            if zu is not None:
                for ky in (y[1], y[2]):
                    chains[y[0]]["hi"][ky] = min(chains[y[0]]["hi"][ky], zu - need)
    info = {}
    zs = _cw.solve(chains, cps, diagnostics=info)
    if diagnostics is not None:
        diagnostics.update(info)
    if not info.get("converged") or not info.get("feasible"):
        return None
    return [np.asarray(z, np.float64) for z in zs]


def solve_shape_soft(net, sysm, pcs, smp, ground, fixed_z):
    """The shape with its laws as penalties (active sets, re-evaluated each iteration): always an answer."""
    import scipy.sparse as sp
    import scipy.sparse.linalg as spla
    off = np.cumsum([0] + [len(x["d"]) for x in smp])
    N = int(off[-1])
    if N == 0:
        return []

    def var(i, s_):
        got = _var(pcs, smp, i, s_)
        if got is None:
            return None
        p, a, b, t = got
        return [(int(off[p] + a), 1.0 - t), (int(off[p] + b), t)]
    g_all = np.concatenate([np.asarray(g, np.float64) for g in ground])
    R, C, V, B = [], [], [], []
    nrow = [0]

    def row(ent, b, w):
        for c, v in ent:
            R.append(nrow[0])
            C.append(c)
            V.append(v * w)
        B.append(b * w)
        nrow[0] += 1
    laws, bends = [], []
    for p, (pc, x) in enumerate(zip(pcs, smp)):
        o = int(off[p])
        dv = x["d"]
        g, rad = _law(net, pc)
        for k in range(len(dv) - 1):
            laws.append((o + k, o + k + 1, float(dv[k + 1] - dv[k]), g))
        for k in range(1, len(dv) - 1):
            ds = float(dv[k + 1] - dv[k - 1]) / 2
            bends.append((o + k - 1, o + k, o + k + 1, max(ds, 1e-6), ds / rad))
            row([(o + k - 1, 1.0), (o + k, -2.0), (o + k + 1, 1.0)], 0.0, W_SMOOTH)
        if pc["kind"] == "deck":
            target, w = _deck_targets(net, sysm, pc, g_all[o:off[p + 1]])
            for k in range(len(dv)):
                if w[k] > 1e-5:
                    row([(o + k, 1.0)], target[k], math.sqrt(w[k]))
    for i, si, j, sj in sysm["joins"]:
        a_, b_ = var(i, si), var(j, sj)
        if a_ and b_:
            row(a_ + [(c, -v) for c, v in b_], 0.0, W_JOIN)
    for p, a, q, b in _overlaps(pcs, smp):
        row([(int(off[p] + a), 1.0), (int(off[q] + b), -1.0)], 0.0, W_JOIN)
    n_fixed = nrow[0]
    fixed = (np.array(R, np.int64), np.array(C, np.int64), np.array(V, np.float64), np.array(B, np.float64))
    clears = []
    for c in sysm["crossings"]:
        need = clearance_need(net, c["u"], c["d"])
        vu, vd = var(c["u"], c["su"]), var(c["d"], c["sd"])
        zu = None if vu else fixed_z(c["u"], c["su"])
        zd = None if vd else fixed_z(c["d"], c["sd"])
        if (vu is None and zu is None) or (vd is None and zd is None) or (vu is None and vd is None):
            continue
        clears.append((vu, zu, vd, zd, need))
    la = np.array([x[0] for x in laws], np.int64)
    lb = np.array([x[1] for x in laws], np.int64)
    lds = np.maximum(np.array([x[2] for x in laws], np.float64), 1e-6)
    lg = np.array([x[3] for x in laws], np.float64)
    ba = np.array([x[0] for x in bends], np.int64)
    bb = np.array([x[1] for x in bends], np.int64)
    bc = np.array([x[2] for x in bends], np.int64)
    bds = np.array([x[3] for x in bends], np.float64)
    blim = np.array([x[4] for x in bends], np.float64)
    dip_ix = np.concatenate([np.arange(off[p], off[p + 1]) for p, pc in enumerate(pcs) if pc["kind"] == "dip"] or
                            [np.zeros(0, np.int64)]).astype(np.int64)
    z = g_all.copy()
    for it in range(ITERS):
        Rr, Cc, Vv, Bb = [fixed[0]], [fixed[1]], [fixed[2]], [fixed[3]]
        r0 = n_fixed
        if len(dip_ix):
            r_ = z[dip_ix] - g_all[dip_ix]
            w = np.where(r_ > 0.0, 0.3 * W_LAW, np.sqrt(W_DIP / np.maximum(np.abs(r_), 0.05)))
            k = len(dip_ix)
            Rr.append(r0 + np.arange(k))
            Cc.append(dip_ix)
            Vv.append(w)
            Bb.append(w * g_all[dip_ix])
            r0 += k
        gr = (z[lb] - z[la]) / lds
        over = np.flatnonzero(np.abs(gr) > lg)
        if len(over):
            k = len(over)
            rr = r0 + np.arange(k)
            Rr += [rr, rr]
            Cc += [lb[over], la[over]]
            Vv += [np.full(k, W_LAW), np.full(k, -W_LAW)]
            Bb += [W_LAW * np.sign(gr[over]) * lg[over] * lds[over]]
            r0 += k
        if len(bends):
            dg = ((z[bc] - z[bb]) - (z[bb] - z[ba])) / bds
            bent = np.flatnonzero(np.abs(dg) > blim)
            if len(bent):
                k = len(bent)
                rr = r0 + np.arange(k)
                w = W_LAW / bds[bent]
                Rr += [rr, rr, rr]
                Cc += [bc[bent], bb[bent], ba[bent]]
                Vv += [w, -2.0 * w, w]
                Bb += [W_LAW * np.sign(dg[bent]) * blim[bent]]
                r0 += k
        for vu, zu, vd, zd, need in clears:
            hu = sum(z[c] * v for c, v in vu) if vu else zu
            hd = sum(z[c] * v for c, v in vd) if vd else zd
            if hu - hd >= need:
                continue
            ent, b = [], need
            if vu:
                ent += vu
            else:
                b -= zu
            if vd:
                ent += [(c, -v) for c, v in vd]
            else:
                b += zd
            for c, v in ent:
                Rr.append(np.array([r0]))
                Cc.append(np.array([c], np.int64))
                Vv.append(np.array([v * W_LAW]))
            Bb.append(np.array([b * W_LAW]))
            r0 += 1
        A = sp.csr_matrix((np.concatenate(Vv), (np.concatenate(Rr), np.concatenate(Cc))), shape=(r0, N))
        AtA = (A.T @ A).tocsc()
        z_new = spla.spsolve(AtA + sp.identity(N, format="csc") * 1e-9, A.T @ np.concatenate(Bb))
        moved = float(np.abs(z_new - z).max())
        z = z_new
        if moved < 0.001 and it > 2:
            break
    return [z[off[p]:off[p + 1]] for p in range(len(pcs))]


def ramp(z0, g0, sign, g, rad, d, ground):
    """An embankment (sign +1: its start over the ground, the road falling away from it) or a cut (-1) from height z0,
    leaving at grade g0 (along d), at distances d (from 0, every STEP_M) with ground heights ground: it turns from g0
    to the road's max grade g (falling for an embankment, rising for a cut) over a vertical curve of radius rad, holds
    it until it meets the ground, and turns onto the ground's grade over a curve of the same radius centred on that
    touch point. Returns its heights and the index past which it is the ground (len(d) if it never meets it)."""
    n = len(d)
    if n < 2:
        return np.full(n, z0), n
    ds = np.diff(d)
    target = -sign * g
    grade = np.empty(n - 1)
    gcur = g0
    for k in range(n - 1):                      # (the change of grade at most ds / rad a step)
        step = ds[k] / rad
        gcur = gcur + float(np.clip(target - gcur, -step, step))
        grade[k] = gcur
    z = z0 + np.r_[0.0, np.cumsum(grade * ds)]
    off_ = sign * (z - ground)
    hit = np.flatnonzero(off_[1:] <= 0.0)
    if not len(hit):
        return z, n
    t = int(hit[0]) + 1                         # (the first sample past the touch point)
    f = float(off_[t - 1] / max(off_[t - 1] - off_[t], 1e-12))
    s_t = float(d[t - 1] + f * (d[t] - d[t - 1]))   # (the touch point: where the ramp meets the ground)
    z_t = float(np.interp(s_t, d, ground))
    g1 = float(grade[t - 1])
    look = min(n - 1, t + max(2, int(round(abs(g1) * rad / max(ds.mean(), 1e-6))) + 2))
    g2 = float((ground[look] - ground[t - 1]) / max(d[look] - d[t - 1], 1e-6)) if look > t - 1 else g1
    half = 0.5 * rad * abs(g2 - g1)             # (the curve onto the ground, centred on the touch point)
    s1 = s_t - half
    s2 = s_t + half
    k1 = int(np.searchsorted(d, s1))
    k2 = int(min(n, np.searchsorted(d, s2) + 1))
    z1 = z_t - g1 * half
    out = z.copy()
    for k in range(max(k1, 1), k2):
        x = d[k] - s1
        out[k] = z1 + g1 * x + (g2 - g1) / (2.0 * max(2 * half, 1e-6)) * x * x
    # (past the curve the plan's own ground grading takes over)
    return out, max(k2, t + 1)


def _onward(net, j, s_from, toward_end):
    """The road from member j's arc length s_from on, away from the shape (toward j's end at L if toward_end), followed
    through its joints, RAMP_MAX_M at most: segments."""
    L = net.L[j]
    if toward_end:
        segs = [(j, s_from, min(L, s_from + RAMP_MAX_M))]
        left = RAMP_MAX_M - (segs[0][2] - s_from)
        return segs + (walk(net, j, True, left) if left > 1e-6 else [])
    segs = [(j, s_from, max(0.0, s_from - RAMP_MAX_M))]
    left = RAMP_MAX_M - (s_from - segs[0][2])
    return segs + (walk(net, j, False, left) if left > 1e-6 else [])


def _split(net, x, z, k_end, mode, out):
    """A ramp's or dip's samples (x from sample()) up to k_end, per member: out[member] += (s ascending, z, mode)."""
    mem, ms = x["mem"][:k_end], x["ms"][:k_end]
    zz = z[:k_end]
    for j in dict.fromkeys(mem.tolist()):
        k = np.flatnonzero(mem == j)
        sv, zv = ms[k], zz[k]
        extra = [(kk, s0) for kk, jj, s0 in x.get("joints", ()) if jj == j and kk < k_end]
        if extra:                           # (the joint's sample: this member's end too)
            sv = np.r_[sv, [e[1] for e in extra]]
            zv = np.r_[zv, [zz[e[0]] for e in extra]]
        if len(sv) < 2:
            continue
        o = np.argsort(sv, kind="stable")
        out.setdefault(int(j), []).append((sv[o], zv[o], mode))


def embankments(net, systems, shapes, ground_along):
    """The roads' heights round the shapes: per member, [(s (m,), z (m,), mode)] with mode "fill" (the plan rises to
    it: an embankment), "cut" (the plan goes down to it) or "set" (the plan takes it: a road between two shapes whose
    ramps meet before the ground does, lerped). shapes: per system (pieces, samples, heights);
    ground_along(member, s) -> ground heights."""
    ramps = []              # (first member, s0, direction +1/-1, z0, samples, z, touch index, sign)
    out = {}
    for sysm, (pcs, smp, zs) in zip(systems, shapes):
        def shape_z(i, s_):
            got = _var(pcs, smp, i, s_)
            if got is None:
                return None, 0.0
            p, a, b, t = got
            z = zs[p]
            dd = smp[p]["d"]
            zz = z[a] + (z[b] - z[a]) * t
            g = (z[b] - z[a]) / max(dd[b] - dd[a], 1e-6)
            return float(zz), float(g)

        def ramp_from(j, s0, toward_end, z0, g_along):
            segs = _onward(net, j, s0, toward_end)
            x = sample(net, [dict(segs=segs)])[0]
            gr = np.concatenate([ground_along(int(m_), np.array([s_])) for m_, s_ in zip(x["mem"], x["ms"])]) \
                if len(set(x["mem"].tolist())) > 1 else ground_along(j, x["ms"])
            sign = 1.0 if z0 >= gr[0] else -1.0
            z, k = ramp(z0, g_along, sign, max_grade(net.m[j]), curve_radius(net.m[j]), x["d"] - x["d"][0], gr)
            ramps.append((j, s0, 1 if toward_end else -1, z0, x, z, k, sign))

        # a deck's end on a road on the ground: the embankment (or cut) down (up) to the ground along it
        for i, si, j, sj in sysm["ends"]:
            if net.m[i].kind != "deck":
                continue
            z0, g = shape_z(i, si)
            if z0 is None:
                continue
            leaving = -g if si < net.L[i] / 2 else g        # (the deck's grade on the way out of it)
            ramp_from(j, sj, sj < net.L[j] / 2, z0, leaving)
        # a road going under: its dip (cut), and on from its ends if they are not back on the ground
        for pc, x, z in zip(pcs, smp, zs):
            if pc["kind"] != "dip":
                continue
            _split(net, x, np.asarray(z, np.float64), len(z), "cut", out)
            for e in (0, len(z) - 1):
                j, s_ = int(x["mem"][e]), float(x["ms"][e])
                gr = ground_along(j, np.array([s_]))[0]
                if abs(z[e] - gr) < 0.05:
                    continue
                k2 = 1 if e == 0 else len(z) - 2
                g = (z[e] - z[k2]) / max(abs(x["d"][e] - x["d"][k2]), 1e-6)
                toward_end = (x["ms"][e] - x["ms"][k2] > 0) if int(x["mem"][k2]) == j else s_ > net.L[j] / 2
                ramp_from(j, s_, bool(toward_end), float(z[e]), g)
    # two ramps on one road toward each other that meet before the ground: the straight line between their ends
    used = set()
    for a in range(len(ramps)):
        for b in range(len(ramps)):
            if a == b or a in used or b in used:
                continue
            ja, sa, da, za, xa, z_a, ka, _ = ramps[a]
            jb, sb, db, zb, xb, z_b, kb, _ = ramps[b]
            if ja != jb or da != 1 or db != -1 or sb <= sa:
                continue
            reach_a = sa + xa["d"][min(ka, len(xa["d"]) - 1)]
            reach_b = sb - xb["d"][min(kb, len(xb["d"]) - 1)]
            if reach_a >= reach_b:
                k = max(2, int(math.ceil((sb - sa) / STEP_M)) + 1)
                sv = np.linspace(sa, sb, k)
                out.setdefault(ja, []).append((sv, za + (zb - za) * (sv - sa) / max(sb - sa, 1e-6), "set"))
                used |= {a, b}
    for n, (j, s0, d, z0, x, z, k, sign) in enumerate(ramps):
        if n in used:
            continue
        _split(net, x, z, min(k, len(z)), "fill" if sign > 0 else "cut", out)
    return out
