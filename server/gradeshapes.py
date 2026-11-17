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
DECK_THICK_M = 1.2          # m from a deck's road surface to its underside
COVER_M = 1.0               # m of structure over an underpass's headroom (the roof under a road or track at grade)
TUNNEL_MAX_M = 300.0        # m: a longer road tunnel is a tunnel network of its own (tunnels.py), not an underpass
ROAD_SHORT_M = 20.0         # m: a road deck chain this short over a road is the road dug under it
RAIL_LONG_M = 60.0          # m: a rail deck this long is a viaduct (the railways raise it), shorter the road goes under
CONNECT_M = 400.0           # m: a road joining two decks' (or tunnels') ends no longer than this ties them in one system
RAMP_MAX_M = 600.0          # m: the longest embankment or cut followed along a road
DIP_MARGIN_M = 3.0          # m past the upper part's edge a road going under holds its dip before its cut ramps up
SOFT_M = 0.6                # m: the rounding where an embankment meets the ground (a soft maximum)

# vehicle height (m) under a deck, by what runs under
CLEAR_ROAD_M = 4.8
CLEAR_MINOR_M = 4.0
CLEAR_PATH_M = 2.7
CLEAR_RAIL_M = 6.0

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
    """The vehicle height member m needs under a deck (its signed limit where it has one)."""
    if m.limit is not None and m.limit > 0:
        return float(m.limit)
    if m.kind in ("rail", "rail_deck"):
        return CLEAR_RAIL_M
    if m.cls in PATHS:
        return CLEAR_PATH_M
    if _base(m.cls) in MINOR:
        return CLEAR_MINOR_M
    return CLEAR_ROAD_M


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
    # the roads going under: the window of each that dips (its crossing +- the upper part's half width and a margin)
    dips = {}
    for c in crossings:
        if c["moves"] != "down":
            continue
        d = c["d"]
        if members[d].kind == "tunnel":
            dips.setdefault(d, []).append((0.0, net.L[d]))
        else:
            # (the window under the upper part, and the cut each side back up to the ground at the road's grade,
            # through its curves: the whole dip is the shape's, its least depth the cost)
            md = members[d]
            depth = headroom(md) + DECK_THICK_M
            g = max_grade(md)
            w = members[c["u"]].half + DIP_MARGIN_M + depth / g + curve_radius(md) * g
            dips.setdefault(d, []).append((max(0.0, c["sd"] - w), min(net.L[d], c["sd"] + w)))
    for d in list(dips):
        rs = sorted(dips[d])
        cur = [list(rs[0])]
        for a, b in rs[1:]:
            if a <= cur[-1][1] + STEP_M:
                cur[-1][1] = max(cur[-1][1], b)
            else:
                cur.append([a, b])
        dips[d] = [(float(a), float(b)) for a, b in cur]
    chain_of = {i: chain_len[root(i)] for i in seeds if members[i].kind == "deck"}
    return dict(seeds=seeds, joins=joins, crossings=crossings, ends=ends, dips=dips, chain_of=chain_of)


def system_key(net, sysm):
    """A system's identity: its parts' keys (the same from whichever cell found it)."""
    h = hashlib.sha1()
    for i in sorted(set(sysm["seeds"]) | set(sysm["dips"]), key=lambda i: str(net.m[i].key)):
        h.update(repr(net.m[i].key).encode())
    return h.hexdigest()[:20]


def pieces(net, sysm):
    """The shape's unknowns: [(member, s0, s1, kind)] for its road decks (whole) and the windows of the roads going
    under; kind "deck" or "dip"."""
    out = []
    for i in sysm["seeds"]:
        if net.m[i].kind == "deck":
            out.append((i, 0.0, net.L[i], "deck"))
    for d in sorted(sysm["dips"], key=lambda i: str(net.m[i].key)):
        for a, b in sysm["dips"][d]:
            out.append((d, a, b, "dip"))
    return out


def sample(net, pcs):
    """Per piece: (xy (m, 2), s (m,))."""
    out = []
    for i, a, b, kind in pcs:
        k = max(2, int(math.ceil((b - a) / STEP_M)) + 1)
        sv = np.linspace(a, b, k)
        out.append((np.stack([np.interp(sv, net.s[i], net.lines[i][:, 0]),
                              np.interp(sv, net.s[i], net.lines[i][:, 1])], axis=1), sv))
    return out


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


def _nearest(sv, s_):
    return int(np.clip(np.searchsorted(sv, s_), 1, len(sv) - 1) - (abs(sv[max(0, np.searchsorted(sv, s_) - 1)] - s_)
                                                                  <= abs(sv[min(len(sv) - 1, np.searchsorted(sv, s_))] - s_)))


def _solve_shape_qp(net, sysm, pcs, smp, ground, fixed_z, diagnostics):
    import carriageways as _cw
    where = {}
    for p, (i, a, b, kind) in enumerate(pcs):
        where.setdefault(i, []).append(p)

    def at(i, s_):
        """(piece, the samples bracketing arc length s_) of member i's unknown height, or None."""
        for p in where.get(i, ()):
            sv = smp[p][1]
            if sv[0] - 1e-6 <= s_ <= sv[-1] + 1e-6:
                k = int(np.clip(np.searchsorted(sv, s_) - 1, 0, len(sv) - 2))
                return p, (k, k + 1)
        return None
    joined = {}
    for i, si, j, sj in sysm["joins"]:
        joined.setdefault(i, []).append(si)
        joined.setdefault(j, []).append(sj)
    chains = []
    for p, (i, a, b, kind) in enumerate(pcs):
        sv = smp[p][1]
        t = np.asarray(ground[p], np.float64)
        n = len(sv)
        m = net.m[i]
        lo, hi = np.full(n, -np.inf), np.full(n, np.inf)
        if kind == "deck":
            short = sysm["chain_of"].get(i, 0.0) < ROAD_SHORT_M
            target = t[0] + (t[-1] - t[0]) * np.arange(n) / max(n - 1, 1)
            w = np.full(n, W_HOLD if short else 1e-6)
            for k, s_end in ((0, 0.0), (n - 1, net.L[i])):
                if not any(abs(s_ - s_end) <= JOIN_M for s_ in joined.get(i, ())):
                    w[k] = W_HOLD if short else W_END       # (the embankment where it meets the road on the ground)
        else:
            target = t.copy()
            w = np.full(n, W_DIP)
            hi = t.copy()                                   # (a road going under never rises over its ground)
        chains.append(dict(t=target, w=w, lo=lo, hi=hi, grade=max_grade(m), K=curve_radius(m) / 100.0,
                           lam=1e-3, step=float((sv[-1] - sv[0]) / max(n - 1, 1)), length=0.0))
    cps = []
    for i, si, j, sj in sysm["joins"]:
        x, y = at(i, si), at(j, sj)
        if x and y:
            kx = x[1][0] if abs(smp[x[0]][1][x[1][0]] - si) <= abs(smp[x[0]][1][x[1][1]] - si) else x[1][1]
            ky = y[1][0] if abs(smp[y[0]][1][y[1][0]] - sj) <= abs(smp[y[0]][1][y[1][1]] - sj) else y[1][1]
            cps.append((x[0], kx, y[0], ky, "eq", 0.0))
    for c in sysm["crossings"]:
        mu, md = net.m[c["u"]], net.m[c["d"]]
        need = headroom(md) + (DECK_THICK_M if mu.kind in ("deck", "rail_deck") else COVER_M)
        x, y = at(c["u"], c["su"]), at(c["d"], c["sd"])
        if x and y:
            for kx in x[1]:
                for ky in y[1]:
                    cps.append((x[0], kx, y[0], ky, "ge", need))
        elif x:
            zd = fixed_z(c["d"], c["sd"])
            if zd is not None:
                for kx in x[1]:
                    chains[x[0]]["lo"][kx] = max(chains[x[0]]["lo"][kx], zd + need)
        elif y:
            zu = fixed_z(c["u"], c["su"])
            if zu is not None:
                for ky in y[1]:
                    chains[y[0]]["hi"][ky] = min(chains[y[0]]["hi"][ky], zu - need)
    info = {}
    zs = _cw.solve(chains, cps, diagnostics=info)
    if diagnostics is not None:
        diagnostics.update(info)
    if not info.get("converged") or not info.get("feasible"):
        return None
    return [np.asarray(z, np.float64) for z in zs]


def solve_shape_soft(net, sysm, pcs, smp, ground, fixed_z):
    """The shape: per piece its heights. ground: per piece, the ground height at its samples; fixed_z(member, s): the
    height of a part that is not an unknown where the shape crosses it (its ground, a track's bed, a rail deck's line).
    Least squares; the grade laws, clearances and joints as penalties (active sets, re-evaluated each iteration), so
    there is always an answer."""
    import scipy.sparse as sp
    import scipy.sparse.linalg as spla
    off = np.cumsum([0] + [len(x[1]) for x in smp])
    N = int(off[-1])
    if N == 0:
        return []
    where = {}
    for p, (i, a, b, kind) in enumerate(pcs):
        where.setdefault(i, []).append(p)

    def var(i, s):
        """[(sample index, weight)] interpolating member i's unknown height at arc length s, or None."""
        for p in where.get(i, ()):
            sv = smp[p][1]
            if sv[0] - 1e-6 <= s <= sv[-1] + 1e-6:
                k = int(np.clip(np.searchsorted(sv, s) - 1, 0, len(sv) - 2))
                t = float(np.clip((s - sv[k]) / max(sv[k + 1] - sv[k], 1e-9), 0.0, 1.0))
                return [(int(off[p] + k), 1.0 - t), (int(off[p] + k + 1), t)]
        return None
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
    laws = []           # (sample a, sample a+1, ds, max grade)
    bends = []          # (sample k-1, k, k+1, the largest change of grade over them: ds / radius)
    for p, (i, a, b, kind) in enumerate(pcs):
        o = int(off[p])
        sv = smp[p][1]
        g = max_grade(net.m[i])
        rad = curve_radius(net.m[i])
        for k in range(len(sv) - 1):
            laws.append((o + k, o + k + 1, float(sv[k + 1] - sv[k]), g))
        for k in range(1, len(sv) - 1):
            ds = float(sv[k + 1] - sv[k - 1]) / 2
            bends.append((o + k - 1, o + k, o + k + 1, max(ds, 1e-6), ds / rad))
            row([(o + k - 1, 1.0), (o + k, -2.0), (o + k + 1, 1.0)], 0.0, W_SMOOTH)
    # a deck's end that is the system's end (no other deck there): the least embankment where it meets the ground
    joined = {}
    for i, si, j, sj in sysm["joins"]:
        joined.setdefault(i, []).append(si)
        joined.setdefault(j, []).append(sj)
    for p, (i, a, b, kind) in enumerate(pcs):
        if kind != "deck":
            continue
        o, n = int(off[p]), len(smp[p][1])
        short = sysm["chain_of"].get(i, 0.0) < ROAD_SHORT_M     # (the road under goes down: the deck stays)
        for k, s_end in ((0, 0.0), (n - 1, net.L[i])):
            if not any(abs(s_ - s_end) <= JOIN_M for s_ in joined.get(i, ())):
                row([(o + k, 1.0)], g_all[o + k], W_HOLD if short else W_END)
        # (toward the straight line between its ends' ground: held there if short, else only defined where nothing
        # else asks)
        for k in range(n):
            row([(o + k, 1.0)], g_all[o] + (g_all[o + n - 1] - g_all[o]) * k / max(n - 1, 1), W_HOLD if short else 1e-3)
    for i, si, j, sj in sysm["joins"]:
        a_, b_ = var(i, si), var(j, sj)
        if a_ and b_:
            row(a_ + [(c, -v) for c, v in b_], 0.0, W_JOIN)
    n_fixed = nrow[0]
    fixed = (np.array(R, np.int64), np.array(C, np.int64), np.array(V, np.float64), np.array(B, np.float64))
    # the clearances: upper - lower >= need, either side an unknown or a fixed height
    clears = []
    for c in sysm["crossings"]:
        mu, md = net.m[c["u"]], net.m[c["d"]]
        need = headroom(md) + (DECK_THICK_M if mu.kind in ("deck", "rail_deck") else COVER_M)
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
    # a road going under: as shallow as it may be (the least cut, L1: on its ground wherever it can be), never over it
    dip_ix = np.concatenate([np.arange(off[p], off[p + 1]) for p, x in enumerate(pcs) if x[3] == "dip"] or
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
        if len(bends):          # (the change of grade between neighbouring samples within the curve law)
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


def _span(net, j, s_from, toward_end):
    """Arc lengths along member j from s_from away from the shape (toward its end at L if toward_end, else toward 0),
    every STEP_M, at most RAMP_MAX_M."""
    L = net.L[j]
    stop = min(L, s_from + RAMP_MAX_M) if toward_end else max(0.0, s_from - RAMP_MAX_M)
    k = max(2, int(math.ceil(abs(stop - s_from) / STEP_M)) + 1)
    return np.linspace(s_from, stop, k)


def embankments(net, systems, shapes, ground_along):
    """The roads' heights round the shapes: per member, [(s (m,), z (m,), mode)] with mode "fill" (the plan rises to
    it: an embankment), "cut" (the plan goes down to it) or "set" (the plan takes it: a road between two shapes whose
    ramps meet before the ground does, lerped). shapes: per system, per piece its heights (solve_shape);
    ground_along(member, s) -> ground heights."""
    ramps = {}              # member -> [(s0, direction +1/-1, z0, s (m,), z (m,), touch index, sign)]
    for sysm, (pcs, smp, zs) in zip(systems, shapes):
        def shape_z(i, s_):
            for (pi, a, b, kind), (xy, sv), z in zip(pcs, smp, zs):
                if pi == i and sv[0] - 1e-6 <= s_ <= sv[-1] + 1e-6:
                    return float(np.interp(s_, sv, z))
            return None

        def leaving(i, s_):
            """The grade the shape leaves member i with at its end s_ (along the way out of it)."""
            for (pi, a, b, kind), (xy, sv), z in zip(pcs, smp, zs):
                if pi == i and sv[0] - 1e-6 <= s_ <= sv[-1] + 1e-6 and len(sv) >= 2:
                    if s_ - sv[0] < sv[-1] - s_:
                        return float((z[0] - z[1]) / max(sv[1] - sv[0], 1e-6))
                    return float((z[-1] - z[-2]) / max(sv[-1] - sv[-2], 1e-6))
            return 0.0
        # a deck's or a tunnel's end on a road on the ground
        for i, si, j, sj in sysm["ends"]:
            z0 = shape_z(i, si)
            if z0 is None:
                continue
            toward_end = sj < net.L[j] / 2
            sv = _span(net, j, sj, toward_end)
            gr = ground_along(j, sv)
            sign = 1.0 if z0 >= gr[0] else -1.0
            z, k = ramp(z0, leaving(i, si), sign, max_grade(net.m[j]), curve_radius(net.m[j]), np.abs(sv - sv[0]), gr)
            ramps.setdefault(j, []).append((sj, 1 if toward_end else -1, z0, sv, z, k, sign))
        # a road going under: its window's heights, cut ramps out of it both ways
        for (pi, a, b, kind), (xy, sv), z in zip(pcs, smp, zs):
            if kind != "dip" or net.m[pi].kind == "tunnel":
                continue
            ramps.setdefault(pi, []).append((a, 0, None, sv, np.asarray(z, np.float64), len(sv), 0.0))
            for s0, z0, toward_end in ((a, float(z[0]), False), (b, float(z[-1]), True)):
                if (toward_end and s0 >= net.L[pi] - 1e-6) or (not toward_end and s0 <= 1e-6):
                    continue
                sv2 = _span(net, pi, s0, toward_end)
                gr = ground_along(pi, sv2)
                sign = 1.0 if z0 >= gr[0] else -1.0
                g0 = float((z[1] - z[0]) / max(sv[1] - sv[0], 1e-6)) if not toward_end else float(
                    (z[-1] - z[-2]) / max(sv[-1] - sv[-2], 1e-6))
                z2, k = ramp(z0, -g0 if not toward_end else g0, sign, max_grade(net.m[pi]), curve_radius(net.m[pi]),
                             np.abs(sv2 - sv2[0]), gr)
                ramps.setdefault(pi, []).append((s0, 1 if toward_end else -1, z0, sv2, z2, k, sign))
    out = {}
    for j, lst in ramps.items():
        pieces_ = []
        used = set()
        # two ramps toward each other that meet before the ground: the straight line between their ends (lerped)
        for x in range(len(lst)):
            for y in range(len(lst)):
                if x == y or x in used or y in used:
                    continue
                s0x, dx, zx, svx, z_x, kx, _sx = lst[x]
                s0y, dy, zy, svy, z_y, ky, _sy = lst[y]
                if dx != 1 or dy != -1 or zx is None or zy is None or s0y <= s0x:
                    continue
                reach_x = svx[min(kx, len(svx) - 1)]
                reach_y = svy[min(ky, len(svy) - 1)]
                if reach_x >= reach_y:          # (the ramps overlap: no ground between them)
                    k = max(2, int(math.ceil((s0y - s0x) / STEP_M)) + 1)
                    sv = np.linspace(s0x, s0y, k)
                    pieces_.append((sv, zx + (zy - zx) * (sv - s0x) / max(s0y - s0x, 1e-6), "set"))
                    used |= {x, y}
        for x, (s0, d, z0, sv, z, k, sign) in enumerate(lst):
            if x in used:
                continue
            if d == 0:                          # (a dip's window)
                pieces_.append((sv, z, "cut"))
                continue
            k = min(k, len(sv))
            if k < 2:
                continue
            order = np.argsort(sv[:k])
            pieces_.append((sv[:k][order], z[:k][order], "fill" if sign > 0 else "cut"))
        out[j] = pieces_
    return out
