"""Tunnel and bridge grading: each interchange solved as one shape, then placed on the ground with its embankments
(BNE_SHAPES=1, off by default).

1. The system. Every road deck, short road tunnel (an underpass) and short rail deck, with what it joins and what it
   crosses, is one interchange with every other one it joins end to end, crosses, or meets through a short road
   between them (a ramp from one deck to the next). A single bridge or tunnel is a system of its own.

2. Who moves, at each crossing of an upper part over a lower one:
   - the lower road is mapped in a tunnel (an underpass): it goes down, the upper stays at its level (a track or a road
     at grade over it stays on its ground); a longer tunnel (TUNNEL_MAX_M) is tunnels.py's, crossed by nothing here;
   - the upper is a rail deck: a track cannot ramp, so a short rail deck (RAIL_LONG_M) stays where the track is and the
     road under goes down; a long one is a viaduct the railways raise themselves, nothing to solve here;
   - the upper is a road deck: a short deck chain (ROAD_SHORT_M) over a road is the road dug under it, and so is a
     held one (a bridge the map has over a road's short tunnel, grading's never-raised deck: the elevation there is
     its own); a longer one is an overpass that rises over the road under, which keeps its ground;
   - a track is never dug: the road deck over it rises.

3. The shape. The heights of the system's decks, and of the roads that go down under its crossings, in one problem:
   every crossing clears the vehicle height the road under needs (its signed limit, else by class; rail's catenary) plus
   the deck, and so does a deck all along another it runs over (a road winding over itself), not only where their
   lines cross; every deck keeps its girders off the ground under it (but over a road dug under it); every deck holds
   the grade its class and speed allow and the joints between decks meet. Within those, the
   decks' own slopes are free, chosen for the least earthwork: the smallest embankment (or cut) where the shape meets
   the roads on the ground at its ends, and the shallowest dip for a road going under. What must be at ground level (a
   road under an overpass, a track over an underpass) is at its ground: the shape is placed on it.

4. The embankments. Where the shape's end does not meet the road on the ground it joins, that road leading up to it is
   an earth embankment (or a cut, for a road going down) as steep as the road's max grade, as long as it takes to
   intercept the ground: the touch point is where the ramp meets the ground, a result, never an anchor. A road going
   under is cut down to its dip and back up the same way. Two ends whose ramps meet before the ground does (systems
   close to each other on one road, single bridges included) are joined by the straight line between them (lerped).

Pure: members in, shapes and ramps out. grading.py reads the members from its cells and applies the result to its
plans (the decks through roads.DECK_Z_HOOK, the tunnels' floors through roads.TUNNEL_Z_HOOK); the terrain follows the
plans (cut and fill).
"""
import hashlib
import math
from collections import namedtuple

import numpy as np

import dnp

STEP_M = 4.0                # m between a deck's or a dip's samples
JOIN_M = 2.5                # m: two parts' ends (or an end and another's line) this close meet there
CROSS_END_M = 2.0           # m: a crossing this near either part's end is a joint, not a separation
MERGE_DEG = 40.0            # deg: a deck's end on another deck's line at another level meets it only along it (a ramp's
                            # merge or diverge, its gore's taper: Vienna's A23 at 29 deg, Gravelly Hill's links at 6) and
                            # only if they do not cross; across it, it is over or under it (the High Five's level-1 links
                            # ending under its level-4 flyovers at 55-89 deg)
STRUCTURES = ("deck", "rail_deck", "tunnel")
# the game's decks (roads.py: MIN_BRIDGE_HEADROOM_M, BRIDGE_HEADROOM_MARGIN_M, deck_depth; grading sets them from there):
# the vehicle height under the girders, its margin, and the deck's depth by its span (slab + girders)
HEADROOM_M = 6.0
HEADROOM_MARGIN_M = 0.15
DECK_MIN_DEPTH_M = 0.9
DECK_FULL_DEPTH_M = 2.2
DECK_SPAN_RATIO = 22.0
# a track's: its height is its rails' top over the railways' bed (grading's fixed heights: railways.track_beds, the bed
# the track is drawn on), and a rail deck's structure under that top is the railways' deck (railways.RAIL_TOP_M +
# DECK_D - BALLAST_H; grading sets it from there), not a road deck's depth for its span
RAIL_DECK_DEPTH_M = 1.68
COVER_M = 1.0               # m of structure over an underpass's headroom (the roof under a road or track at grade)
TUNNEL_MAX_M = 300.0        # m: a longer road tunnel is a tunnel network of its own (tunnels.py), not an underpass
ROAD_SHORT_M = 20.0         # m: a road deck chain this short over a road is the road dug under it
RAIL_LONG_M = 60.0          # m: a rail deck this long is a viaduct (the railways raise it), shorter the road goes under
CONNECT_M = 400.0           # m: a road joining two decks' (or tunnels') ends no longer than this ties them in one system
RAMP_MAX_M = 600.0          # m: the longest embankment or cut followed along a road
DIP_MARGIN_M = 3.0          # m past the upper part's edge a road going under holds its dip before its cut ramps up
FOOT_STEP_M = 1.0           # m between the points of a crossing's footprint (under_pairs)
DECK_INNER_M = 10.0         # m from a deck's ends past which a span over the ground keeps its girders off it ...
DECK_AIR_M = 0.5            # ... by this much
SOFT_M = 0.6                # m: the rounding where an embankment meets the ground (a soft maximum)

CLEAR_PATH_M = 3.0          # m: a path's headroom (no vehicle on it)

W_END = 1.0                 # a system end's embankment (per m, squared)
W_HOLD = 100.0              # a short deck chain (a road dug under it) at its ground
W_DIP = 0.5                 # a road's dip under its crossing
W_SMOOTH = 0.2
W_JOIN = 300.0

Member = namedtuple("Member", "key line kind cls kmh level half limit nodes held", defaults=(None, False))
# key: hashable, unique, the same from every cell; line (k, 2) frame ENU; kind: "ground", "deck", "tunnel", "rail",
# "rail_deck"; cls: road class ("rail" for track); kmh: speed limit or None; level: the map's level (OSM's layer) of a
# deck or a rail deck, 0 on the ground (_over: the order of two crossing members of one kind); half: half
# width (m); limit: the road's signed height limit (m), a callable q (x, y) -> m or None (its signs, read where it
# crosses: grading's roads.road_sign_at, the dips' own) or None; nodes: its road's map nodes (a frozenset of connector
# ids) or None (not known: a track); held: a deck never raised (grading's UNDER_TUNNEL_FIXED: the map has the road
# under it in a short tunnel), its chain at its ground and the roads under it dug, whatever its length

PATHS = {"footway", "path", "cycleway", "pedestrian", "steps", "bridleway", "track"}
MINOR = {"residential", "service", "unclassified", "living_street", "road"}
DEFAULT_KMH = {"motorway": 110, "trunk": 90, "primary": 70, "secondary": 60, "tertiary": 50, "residential": 40,
               "unclassified": 50, "service": 20, "living_street": 15, "road": 40, "track": 20}


LINK_KMH = 60               # km/h of a link (a ramp, a slip road) with no limit mapped: not its road's (110 on a motorway's)
# The laws are a game's, a little more generous than a design manual's (AASHTO's grades are for a loaded truck, its
# 0.3 m/s2 a passenger's comfort): steeper by GAME_GRADE_PLUS, crests and sags at A_VERT. A curve still smooths every
# change of grade (no hump where roads meet), over a third of the length.
GAME_GRADE_PLUS = 0.02


def _base(cls):
    c = cls or "road"
    return c[:-5] if c.endswith("_link") else c


def speed(m):
    """km/h member m is driven at: its limit, else its class's (a link's LINK_KMH)."""
    if m.kmh:
        return m.kmh
    if (m.cls or "").endswith("_link"):
        return LINK_KMH
    return DEFAULT_KMH.get(_base(m.cls), 40)


def max_grade(m):
    """The steepest grade member m may hold, by class and speed (AASHTO's design grades for a speed: 4 % at 120 km/h
    to 15 % at 20; a link at least 6 %) plus GAME_GRADE_PLUS; a path 12 %; a track 2.5 %."""
    if m.kind in ("rail", "rail_deck"):
        return 0.025
    if m.cls in PATHS:
        return 0.12
    g = float(np.interp(speed(m), [20, 30, 40, 50, 60, 80, 100, 120], [0.15, 0.12, 0.10, 0.09, 0.08, 0.06, 0.05, 0.04]))
    if (m.cls or "").endswith("_link"):
        g = max(g, 0.06)
    return g + GAME_GRADE_PLUS


A_VERT = 1.0               # m/s2: the vertical acceleration a road's crest or sag may give at its speed (a game's: a
#                            design manual's comfort is 0.3; a car leaves the road over a crest at 9.8)
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
    v = speed(m) / 3.6
    return max(R_MIN_ROAD_M, v * v / A_VERT)


def headroom(m, q=None):
    """The vehicle height member m needs under a deck at q (x, y; None: anywhere), with its margin: its signed limit
    where it has one (a maxheight sign), else the game's tallest vehicles' (a track's catenary the same), a path's
    less."""
    lim = m.limit
    if callable(lim):
        lim = lim(q) if q is not None else None
    if lim is not None and lim > 0:
        return float(lim) + HEADROOM_MARGIN_M
    if m.cls in PATHS and m.kind not in ("rail", "rail_deck"):
        return CLEAR_PATH_M
    return HEADROOM_M + HEADROOM_MARGIN_M


def deck_depth(span):
    """m of structure under a deck's road for a span of span m (the game's: span / 22, 0.9 to 2.2 m)."""
    return float(np.clip(float(span) / DECK_SPAN_RATIO, DECK_MIN_DEPTH_M, DECK_FULL_DEPTH_M))


def clearance_need(net, u, d, q=None):
    """How far the road on upper part u stands over lower part d where they cross (at q): the headroom d needs there
    and u's structure (a deck's depth for its span, an underpass's roof)."""
    mu = net.m[u]
    struct = deck_depth(net.L[u]) if mu.kind == "deck" else RAIL_DECK_DEPTH_M if mu.kind == "rail_deck" else COVER_M
    return headroom(net.m[d], q) + struct


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


def _tunnel_network(net, j):
    """Whether member j is a road tunnel longer than TUNNEL_MAX_M: tunnels.py's own (its bore laid under the ground
    over it), no part of any system, neither a seed nor a road going under one. A deck crossing it took it for an
    underpass and dug it as a dip held under its ground while tunnels.py buried the bore it draws: Porte Maillot's two
    Avenue de Neuilly decks ended over the 349 m Tunnel Grand-Maillot and rose 6.2 m over the square to clear a dip at
    37.3 m that was never drawn (the bore at 34 m), the street between their ends and its ramps on a 6 m fill."""
    return net.m[j].kind == "tunnel" and net.L[j] > TUNNEL_MAX_M


_PAIR_REL = {}
_PAIR_REL_MAX = 200000


def _line_id(net, i):
    ids = getattr(net, "_line_ids", None)
    if ids is None:
        ids = net._line_ids = {}
    got = ids.get(i)
    if got is None:
        a = np.ascontiguousarray(net.lines[i], np.float64)
        got = ids[i] = (repr(net.m[i].key), len(a), hash(a.tobytes()), net.m[i].nodes)
    return got


def _relations(net, i):
    """Member i's joints and crossings with the members near it: [(j, "join"|"cross", s on i, s on j, q)]. Each
    pair's from a cache by the two members' keys and lines (a region grown cell by cell meets the same pairs again)."""
    out = []
    for j in net.near(i, JOIN_M + 0.5):
        if len(net.lines[j]) < 2:
            continue
        key = (_line_id(net, i), _line_id(net, j))
        got = _PAIR_REL.get(key)
        if got is None:
            got = _pair_relations(net, i, j)
            if len(_PAIR_REL) >= _PAIR_REL_MAX:
                _PAIR_REL.clear()
            _PAIR_REL[key] = got
        out.extend((j,) + r_ for r_ in got)
    return out


def _tangent(line, s, sq):
    a, b = at_s(line, s, max(sq - 2.0, 0.0)), at_s(line, s, min(sq + 2.0, float(s[-1])))
    v = b - a
    return v / max(float(np.hypot(*v)), 1e-9)


def _proj_many(line, s, P):
    """proj for many points P (k, 2): arc lengths along line (arc lengths s) of the points nearest them, distances."""
    a, ab = line[:-1], line[1:] - line[:-1]
    L2 = np.maximum((ab * ab).sum(1), 1e-12)
    t = np.clip(((P[:, None, :] - a[None]) * ab[None]).sum(2) / L2[None], 0.0, 1.0)
    v = a[None] + t[..., None] * ab[None] - P[:, None, :]
    dist = dnp.hypot(v[..., 0], v[..., 1])
    j = np.argmin(dist, axis=1)
    r = np.arange(len(P))
    return s[j] + t[r, j] * (s[j + 1] - s[j]), dist[r, j]


def under_pairs(net, u, su, d, sd):
    """Where road d runs under part u round their crossing (u at su, d at sd): [(s on u, s on d)], the two arc lengths
    whose heights stand over each other at each point of a FOOT_STEP_M lattice on one's carriageway (along its line,
    its half width across it) that lies on the other's (within its half width of its line, a lattice step more: the
    samples either side of the edge held too) and under u between its ends. The clearance holds there, not where the
    centrelines cross alone: the A5 by Heidelberg, a held deck 14 m wide over a service road signed 3.8 m at 78 deg,
    cleared the sign at that point and its dip rose 0.2 m toward the deck's east edge, where the road's own ground
    stood 3.78 m under the girders and a 3.8 m truck in them. The lattice lies on d under a deck (the shape's unknown:
    any point of it), else on u (a part whose heights are read, fixed_z: once a lattice step along it). Made once a
    crossing."""
    memo = net.__dict__.setdefault("_under", {})
    key = (u, round(float(su), 6), d, round(float(sd), 6))
    got = memo.get(key)
    if got is not None:
        return got
    a_, sa, b_, sb = (d, sd, u, su) if net.m[u].kind == "deck" else (u, su, d, sd)
    la, lsa, lb, lsb = net.lines[a_], net.s[a_], net.lines[b_], net.s[b_]
    ha, hb = float(net.m[a_].half), float(net.m[b_].half)
    ta, tb = _tangent(la, lsa, sa), _tangent(lb, lsb, sb)
    sin = abs(float(ta[0] * tb[1] - ta[1] * tb[0]))
    # (each line as far as straight lines at the crossing's angle overlap round it; a curve's points past it fail the
    # test below. The other's line read there alone: a road passing near its own start again (a loop's deck over the
    # deck it goes on from) has its nearest point on that stretch, not on the one over this crossing)
    c = abs(float(ta @ tb))
    reach_a = (hb + ha * c + FOOT_STEP_M) / sin if sin > 0.0 else math.inf
    reach_b = (ha + hb * c + FOOT_STEP_M) / sin if sin > 0.0 else math.inf
    lo, hi = max(0.0, sa - reach_a), min(net.L[a_], sa + reach_a)
    sv = np.linspace(lo, hi, max(2, int(math.ceil((hi - lo) / FOOT_STEP_M)) + 1))
    cv = np.linspace(-ha, ha, max(2, int(math.ceil(2.0 * ha / FOOT_STEP_M)) + 1))
    wlo, whi = max(0.0, sb - reach_b), min(net.L[b_], sb + reach_b)
    inner = (lsb > wlo) & (lsb < whi)
    sw = np.r_[wlo, lsb[inner], whi]
    lw = np.vstack([at_s(lb, lsb, wlo), lb[inner], at_s(lb, lsb, whi)])
    out = [(float(su), float(sd))]
    if whi <= wlo:
        memo[key] = out
        return out
    nc = len(cv)

    def rows(rs):
        """The lattice's rows rs: their points' arc lengths on a and on b, and which lie under."""
        sr = sv[rs]
        k = np.clip(np.searchsorted(lsa, sr, side="right") - 1, 0, len(lsa) - 2)
        t = la[k + 1] - la[k]
        t = t / np.maximum(dnp.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None]
        base = np.stack([np.interp(sr, lsa, la[:, 0]), np.interp(sr, lsa, la[:, 1])], axis=1)
        P = (base[:, None, :] + cv[None, :, None] * np.stack([-t[:, 1], t[:, 0]], axis=1)[:, None, :]).reshape(-1, 2)
        sP = np.repeat(sr, nc)
        # (b's segments near these points alone)
        lo_, hi_ = P.min(0) - hb - FOOT_STEP_M, P.max(0) + hb + FOOT_STEP_M
        seg = np.flatnonzero((np.minimum(lw[:-1], lw[1:]) <= hi_).all(1) & (np.maximum(lw[:-1], lw[1:]) >= lo_).all(1))
        if not len(seg):
            return sP, sP, np.zeros(len(sP), bool)
        s0, s1 = int(seg[0]), int(seg[-1]) + 2
        sQ, off = _proj_many(lw[s0:s1], sw[s0:s1], P)
        ok = off <= hb + FOOT_STEP_M
        if b_ == u:                         # (under the deck: between its ends, not round them)
            ok &= (sQ > 0.0) & (sQ < net.L[u])
        return sP, sQ, ok
    # (the stretch round this crossing alone, the lattice's rows on from it while they hold a point under, a block of
    # them at a time: a road along a deck meets it again elsewhere, at a joint or another crossing, theirs)
    r0 = int(np.argmin(np.abs(sv - sa)))
    got = [rows(np.array([r0]))]
    for way in (-1, 1):
        r = r0 + way
        while 0 <= r < len(sv):
            rs = np.arange(r, min(max(r + way * 16, -1), len(sv)), way)
            sP, sQ, ok = rows(rs)
            held = ok.reshape(len(rs), nc).any(1)
            n = len(rs) if held.all() else int(np.argmin(held))
            got.append((sP[:n * nc], sQ[:n * nc], ok[:n * nc]))
            if n < len(rs):
                break
            r += way * 16
    sP, sQ, ok = (np.concatenate(v) for v in zip(*got))
    sU, sD = (sQ, sP) if b_ == u else (sP, sQ)
    out += sorted(set(zip(np.round(sU[ok], 6).tolist(), np.round(sD[ok], 6).tolist())))
    memo[key] = out
    return out


def _road_of(m):
    """The map road member m is part of: its key's feature id (a deck's or a ground part's), else its key."""
    return m.key[0] if isinstance(m.key, tuple) else m.key


def _end_meets(net, e, se, o, so, crossed):
    """Whether member e's end (at arc length se) on member o's line (at so) is where they meet: between parts at one
    level (a T, a ramp onto its road, a road going on), or a road on the ground ending on a structure where the map
    joins them (a node of both; on the structure's end always), or two structures at different levels where the map
    joins them, end to end or a deck merging along another deck (MERGE_DEG) that it does not also cross; not where
    two structures at different levels touch in plan with no node of both (a deck's or a tunnel's end over or under
    another's end or middle: a separation, not a joint, its clearance kept: in Barcelona a level-2 service deck's ends
    lay on a level-1 one's ends in plan, taken for joints at both: 13 such in one 14-deck system, most of its relaxed
    solve's shortfall), nor a road on the ground
    ending under a deck's middle (or over a tunnel's) with no node of it: the High Five's secondary leaving its
    level-1 link's end 0.1 m off the line of a level-4 flyover 20 m over it, taken for that flyover's end on it, ran
    a 290 m embankment down from the flyover's height along the street, and the street under the next flyover met its
    deck. Two decks of one map road meet only end to end, never one's end on the other's middle: a road passing
    over itself (Kawazu's Nanadaru loop, a spiral of one road at levels 1 to 3, its level-2 deck ending right over
    its level-1 deck on its heading where the level-3 one goes on) was taken for a merge, held at the deck under it
    there and 8.35 m over it on the level-3 deck's start: the loop infeasible. crossed: e and o cross each other (a
    road cannot meet the one it passes over or under a few metres on: Gravelly Hill's level-1 M6 deck under its
    level-3 one at 29 deg, its end 5 m past their crossing)."""
    me, mo = net.m[e], net.m[o]
    if mo.kind not in STRUCTURES or _over(me, mo) is None:
        return True
    mapped = me.nodes is None or mo.nodes is None or bool(me.nodes & mo.nodes)    # (no nodes known: as they lie)
    if min(so, net.L[o] - so) <= JOIN_M:
        return me.kind not in STRUCTURES or mapped
    if me.kind not in STRUCTURES:
        return mapped
    if me.kind == mo.kind == "deck" and not crossed and _road_of(me) != _road_of(mo) and mapped:
        c = abs(float(_tangent(net.lines[e], net.s[e], se) @ _tangent(net.lines[o], net.s[o], so)))
        return c >= math.cos(math.radians(MERGE_DEG))
    return False


def _pair_relations(net, i, j):
    """Member i's joints and crossings with member j: [("join"|"cross", s on i, s on j, q)]. An end on the other's
    line is a joint, or (_end_meets) between structures at different levels a crossing at that end."""
    hits = []
    for a_i, b_j, ta, tb, q in _seg_hits(net.lines[i], net.lines[j]):
        si = net.s[i][a_i] + ta * (net.s[i][a_i + 1] - net.s[i][a_i])
        sj = net.s[j][b_j] + tb * (net.s[j][b_j + 1] - net.s[j][b_j])
        if min(si, net.L[i] - si, sj, net.L[j] - sj) >= CROSS_END_M:
            hits.append((si, sj, q))
    out = []
    for e, e_line, e_s, o, o_line, o_s, swap in ((i, net.lines[i], net.s[i], j, net.lines[j], net.s[j], False),
                                                  (j, net.lines[j], net.s[j], i, net.lines[i], net.s[i], True)):
        for se in (0.0, float(e_s[-1])):
            q = e_line[0] if se == 0.0 else e_line[-1]
            so, d = proj(o_line, o_s, q)
            if d <= JOIN_M:
                kind = "join" if _end_meets(net, e, se, o, so, bool(hits)) else "cross"
                out.append((j, kind, so if swap else se, se if swap else so, q))
    # (one joint found from both its parts' ends once)
    uniq = []
    for r_ in out:
        if not any(u[0] == r_[0] and u[1] == r_[1] and abs(u[2] - r_[2]) < 1.0 and abs(u[3] - r_[3]) < 1.0
                   for u in uniq):
            uniq.append(r_)
    out[:] = uniq
    for si, sj, q in hits:
        if any(r_[0] == j and r_[1] == "cross" and abs(r_[2] - si) < 1.0 for r_ in out):
            continue                        # (on a vertex: found on both its segments)
        out.append((j, "cross", si, sj, q))
    return [r_[1:] for r_ in out]


LEVEL_BAND = {"deck": 1, "rail_deck": 1, "ground": 0, "rail": 0, "tunnel": -1}


def _over(mi, mj):
    """Of two crossing members, which is over: "i", "j", or None (at grade): the map's kinds first (a bridge over
    what is on the ground, that over a tunnel), then its levels within a kind (Member.level, OSM's layer), a road's
    and a track's alike: stacked decks, a railway's viaduct over a road's bridge, a track in a tunnel under a street.
    The levels alone order every real crossing where they were measured (tests/test_truth.py: 280 of swisstopo's at
    three junctions in Zurich). Only two road decks' were compared, and every track was level 1: Zurich's
    Duttweilerbruecke (level 2) was over the railway viaduct that crosses 7 m over it (level 3) and rose over the
    track's bed for it."""
    a, b = (LEVEL_BAND[mi.kind], mi.level), (LEVEL_BAND[mj.kind], mj.level)
    if a == b:
        return None
    return "i" if a > b else "j"


def find_systems(members):
    """The interchanges among members: [System], each closed, in a deterministic order."""
    net = Net(members)
    n = len(members)
    cand = [i for i in range(n) if _is_seed(net, i) and len(net.lines[i]) >= 2]
    rel = {i: [r_ for r_ in _relations(net, i) if not _tunnel_network(net, r_[0])] for i in cand}
    # a deck chain is part of an interchange only if one of its decks crosses a road, a track or a tunnel (over or
    # under): a bridge over a river or a valley alone is the bridges' own (roads.py), not a grade separation's
    crosses = {i for i in cand if any(kind == "cross" and _over(members[i], members[j]) is not None
                                      for j, kind, _a, _b, _q in rel[i])}
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
        return net, []
    for i in seeds:
        rel[i] = [r_ for r_ in rel[i] if r_[0] in set(seeds) or not _is_seed(net, r_[0])]
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
    for a, b in _shared_dips(net, seeds, rel, droot):
        unite(a, b)
    groups = {}
    for i in seeds:
        groups.setdefault(root(i), []).append(i)
    systems = []
    for r in sorted(groups, key=lambda x: str(members[x].key)):
        systems.append(_system(net, sorted(groups[r], key=lambda x: str(members[x].key)), rel))
    return net, systems


def _dip_half(net, u, su, d, sd, q=None):
    """m along road d each side of its crossing under u (u at su, d at sd, at q) that it dips over (the clearance at
    its grade, the curves, the stretch of it under u (under_pairs) and the margin): _system's window."""
    memo = net.__dict__.setdefault("_dip_half", {})
    key = (u, round(float(su), 6), d, round(float(sd), 6))
    if key not in memo:
        md = net.m[d]
        g = max_grade(md)
        under = max(abs(s_d - sd) for _s_u, s_d in under_pairs(net, u, su, d, sd))
        memo[key] = clearance_need(net, u, d, q) / g + curve_radius(md) * g + under + DIP_MARGIN_M
    return memo[key]


def _dug_under(chain_len, held):
    """Whether the road on the ground under a deck chain chain_len m long goes down under it (the deck at its ground):
    a short chain (ROAD_SHORT_M), or a held one (Member.held). A held deck the shapes raised over the road under it
    while grading's underpasses dug that road under it at its ground did both: the SR17 at Popoli stood on its 8 m
    fills over Via Aldo Moro (the map's short tunnel under it), the street 6.9 m down in its pit, 15 m between them
    where 8.3 m clears it."""
    return chain_len < ROAD_SHORT_M or held


def _shared_dips(net, seeds, rel, droot):
    """Pairs of seeds over one road on the ground within its dip under one of them: [(a, b)]. A road dug under one
    structure (a short rail deck, a short deck) is that low under the other too: two systems solved apart had it at
    its ground under one and dug under the other. Perth's Tonkin Highway: a trunk road dug 7 m under the railway in the
    median and, 6 m on, a motorway deck lifted 7.3 m over its ground, its carriageway and the track's fill raised into
    the trains. The road is followed through its joints, as the dip's piece is."""
    members = net.m
    chain, held = {}, set()
    for i in seeds:
        if members[i].kind == "deck":
            chain[droot(i)] = chain.get(droot(i), 0.0) + net.L[i]
            if members[i].held:
                held.add(droot(i))
    under = {}              # ground member -> [(seed over it, s on the seed, s on it, the crossing)]
    for i in seeds:
        for j, kind, si, sj, q in rel[i]:
            if kind == "cross" and members[j].kind == "ground" and _over(members[i], members[j]) == "i":
                under.setdefault(j, []).append((i, si, sj, q))
    if not under:
        return []
    out = []
    far = max(_dip_half(net, i, si, j, sj, qq) for j, ls in under.items() for i, si, sj, qq in ls)
    for d, lst in under.items():
        for u, su, sd, qd in lst:
            mu = members[u]
            if not (mu.kind == "rail_deck" or (mu.kind == "deck" and _dug_under(chain.get(droot(u), 0.0),
                                                                                droot(u) in held))):
                continue        # (this crossing does not dig the road)
            w = _dip_half(net, u, su, d, sd, qd)
            reach = w + far
            for j, s0, s1 in path_of(net, d, sd - reach, sd + reach):
                lo, hi = min(s0, s1), max(s0, s1)
                for i, si, sj, qi in under.get(j, ()):
                    if i == u or not (lo - 1e-6 <= sj <= hi + 1e-6):
                        continue
                    along = abs(sj - sd) if j == d else _path_dist(net, d, sd, j, sj, reach)
                    if along is None:
                        continue
                    mi = members[i]
                    digs = mi.kind == "rail_deck" or (mi.kind == "deck" and _dug_under(chain.get(droot(i), 0.0),
                                                                                       droot(i) in held))
                    if along <= w + (_dip_half(net, i, si, j, sj, qi) if digs else 0.0):
                        out.append((u, i))
    return out


def _path_dist(net, d, sd, j, sj, reach):
    """Distance along the road from member d's arc length sd to member j's sj, followed through its joints (reach m
    each way at most), or None."""
    for toward_end in (True, False):
        left = net.L[d] - sd if toward_end else sd
        for m_, s0, s1 in walk(net, d, toward_end, reach):
            if m_ == j and min(s0, s1) - 1e-6 <= sj <= max(s0, s1) + 1e-6:
                return left + abs(sj - s0)
            left += abs(s1 - s0)
    return None


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
    chain_len, held = {}, set()
    for i in seeds:
        chain_len[root(i)] = chain_len.get(root(i), 0.0) + net.L[i]
        if members[i].kind == "deck" and members[i].held:
            held.add(root(i))
    joins, crossings, ends = [], [], []
    for i in seeds:
        for j, kind, si, sj, q in rel[i]:
            mi, mj = members[i], members[j]
            if kind == "join":
                if j in S:
                    if str(mi.key) < str(mj.key):
                        joins.append((i, si, j, sj))
                elif mj.kind == "ground":         # a road on the ground leading to the system: where a ramp starts
                    if min(sj, net.L[j] - sj) > JOIN_M and any(
                            k2 == "join" and j2 != i and members[j2].kind in ("deck", "rail_deck") and abs(s2 - si) < 1.0
                            for j2, k2, s2, _t, _q in rel[i]):
                        continue    # (the bridge goes on past this end, over the road's middle: no end onto it, so no
                        # fill run along the road under it from the deck's height: Vienna's Guertel under its square)
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
                    moves = "down" if _dug_under(chain_len[root(u)], root(u) in held) else "up"
                else:
                    moves = "up"
            else:
                moves = None
            if moves is None:
                continue
            crossings.append(dict(u=u, su=su, d=d, sd=sd, q=np.asarray(q, np.float64), moves=moves,
                                  under=under_pairs(net, u, su, d, sd)))
    # the roads going under: per road, the window that dips (its stretch under each upper part (under_pairs), the
    # margin and the cut each side back up to the ground at the road's grade, through its curves), past its own ends if
    # need be (the road followed on through its joints); a tunnel whole, with its approaches the same way
    win = {}
    for c in crossings:
        if c["moves"] != "down":
            continue
        d = c["d"]
        md = members[d]
        g = max_grade(md)
        w = clearance_need(net, c["u"], d, c["q"]) / g + curve_radius(md) * g
        if md.kind == "tunnel":
            win.setdefault(d, []).append((-w, net.L[d] + w))
        else:
            w += DIP_MARGIN_M
            sds = [s_d for _s_u, s_d in c["under"]]
            win.setdefault(d, []).append((min(sds) - w, max(sds) + w))
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
    held_of = {i for i in seeds if members[i].kind == "deck" and root(i) in held}
    return dict(seeds=seeds, joins=joins, crossings=crossings, ends=ends, dips=dips, chain_of=chain_of,
                held=held_of)


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


def _loc_sorted(x, i):
    """Piece x's samples on member i (with its joints'), ordered by arc length: (k, ms), or None. Made once a piece."""
    idx = x.get("_loc")
    if idx is None:
        idx = x["_loc"] = {}
    if i not in idx:
        idx[i] = _loc_sorted_calc(x, i)
    return idx[i]


def _loc_sorted_calc(x, i):
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
    if ms[-1] < ms[0]:
        k, ms = k[::-1], ms[::-1]
    o = np.argsort(ms, kind="stable")
    return k[o], ms[o], lo_, hi_


def _locate(smp, p, i, s_):
    """The two samples of piece p on member i bracketing arc length s_ there, with the weight of the second, or
    None."""
    got = _loc_sorted(smp[p], i)
    if got is None:
        return None
    k, ms, lo_, hi_ = got
    if not (lo_ - 1e-6 <= s_ <= hi_ + 1e-6):
        return None
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


def _vars(pcs, smp, i, sv):
    """_var for many arc lengths sv on member i: (piece, sample a, sample b, weight of b) arrays, piece -1 where none
    holds it."""
    sv = np.asarray(sv, np.float64)
    n = len(sv)
    P, A, B, T = np.full(n, -1, np.int64), np.zeros(n, np.int64), np.zeros(n, np.int64), np.zeros(n)
    for p in range(len(pcs)):
        got = _loc_sorted(smp[p], i)
        if got is None:
            continue
        k, ms, lo_, hi_ = got
        m = (P < 0) & (sv >= lo_ - 1e-6) & (sv <= hi_ + 1e-6)
        if not m.any():
            continue
        r = np.clip(np.searchsorted(ms, sv[m]) - 1, 0, len(ms) - 2)
        P[m], A[m], B[m] = p, k[r], k[r + 1]
        T[m] = np.clip((sv[m] - ms[r]) / np.maximum(ms[r + 1] - ms[r], 1e-9), 0.0, 1.0)
    return P, A, B, T


def _weighted(A, B, T):
    """The samples a point's height is made of ((sample, mask) of _vars' a and b; a sample on the point itself alone):
    each held to a law that holds at the point holds it there."""
    return ((A, T < 1.0 - 1e-9), (B, T > 1e-9))


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
    shape's ends on the ground, the depth a road goes under to, a short deck held at its ground. None where the
    kernel finds no answer (laws that cannot all hold): no stand-in, the system is left as it is and the caller says
    so (the conflict is the thing to fix). fixed_z(i, s): member i's height where the shape does not move it, at arc
    length s (None: not known), or at an array of them (an array, NaN: not known)."""
    try:
        return _solve_shape_qp(net, sysm, pcs, smp, ground, fixed_z, diagnostics)
    except Exception as e:      # (no kernel, or it failed)
        if diagnostics is not None:
            diagnostics["qp_error"] = f"{type(e).__name__}: {e}"
        return None


def _deck_targets(net, sysm, pc, t):
    """A deck's target and weights: toward the line between its ends' ground, held there if a road is dug under its
    chain (_dug_under: a short one, a held one), else free but for the embankment where it meets the road on the
    ground."""
    i = pc["main"]
    n = len(t)
    joined = [s_ for a, sa, b, sb in sysm["joins"] for s_, m_ in ((sa, a), (sb, b)) if m_ == i]
    short = _dug_under(sysm["chain_of"].get(i, 0.0), i in sysm.get("held", ()))
    target = t[0] + (t[-1] - t[0]) * np.arange(n) / max(n - 1, 1)
    w = np.full(n, W_HOLD if short else 1e-6)
    for k, s_end in ((0, 0.0), (n - 1, net.L[i])):
        if not any(abs(s_ - s_end) <= JOIN_M for s_ in joined):
            w[k] = W_HOLD if short else W_END
    return target, w


def _near_line(pts, line, reach):
    """Bool per point of pts (k, 2): within reach of polyline line (m, 2)."""
    out = np.zeros(len(pts), bool)
    if len(line) < 2 or not len(pts):
        return out
    near = np.flatnonzero(((pts >= line.min(0) - reach) & (pts <= line.max(0) + reach)).all(1))
    if len(near):
        a, ab = line[:-1], np.diff(line, axis=0)
        t = np.clip(((pts[near, None, :] - a[None]) * ab[None]).sum(2) / np.maximum((ab * ab).sum(1), 1e-12)[None],
                    0.0, 1.0)
        d = dnp.hypot(*(a[None] + t[..., None] * ab[None] - pts[near, None, :]).transpose(2, 0, 1))
        out[near] = d.min(axis=1) <= reach
    return out


def _over_dips(net, pcs, smp, x):
    """Bool per sample x of a deck: over a road the system digs (within that road's half width and DIP_MARGIN_M of
    its dip): the ground under the deck there is the road's cut, not the elevation's."""
    out = np.zeros(len(x["xy"]), bool)
    for pc, y in zip(pcs, smp):
        if pc["kind"] == "dip":
            out |= _near_line(np.asarray(x["xy"], np.float64), np.asarray(y["xy"], np.float64),
                              float(net.m[pc["main"]].half) + DIP_MARGIN_M)
    return out


def _deck_paths(net, sysm):
    """along(i, si, j, sj): m along the system's decks from member i's arc length si to member j's sj, through their
    joints (inf: not joined)."""
    at = {}                 # deck member -> [(s on it, joint)]
    n = 0
    for i, si, j, sj in sysm["joins"]:
        if net.m[i].kind == net.m[j].kind == "deck":
            at.setdefault(i, []).append((si, n))
            at.setdefault(j, []).append((sj, n))
            n += 1
    D = np.full((n, n), np.inf)
    np.fill_diagonal(D, 0.0)
    for lst in at.values():
        for sa, a in lst:
            for sb, b in lst:
                D[a, b] = min(D[a, b], abs(sa - sb))
    for k in range(n):
        D = np.minimum(D, D[:, k:k + 1] + D[k:k + 1, :])

    def along(i, si, j, sj):
        best = abs(si - sj) if i == j else np.inf
        for sa, a in at.get(i, ()):
            for sb, b in at.get(j, ()):
                best = min(best, abs(si - sa) + float(D[a, b]) + abs(sb - sj))
        return best
    return along


def _stacked(net, sysm, pcs, smp):
    """Where a deck of the system runs over another one along a stretch, not only across it (a road winding over
    itself, one deck stacked on another): [(upper piece, its sample, lower piece, its sample, the clearance)], for each
    sample of the lower deck's line under the upper deck's carriageway (within its half width of its line), the
    upper's two samples round the point over it. The crossings' clearance, held at the samples between them: Kawazu's
    Nanadaru loop winds over its own turns 1.6-3.1 m off their lines for 20-135 m at a time, crossing them only at
    shallow angles here and there. Two samples nearer each other along the decks than the clearance takes at their max
    grade are one stretch of road, not one over the other (a joint, a deck going on from another at another level)."""
    decks = [p for p, pc in enumerate(pcs) if pc["kind"] == "deck"]
    out = []
    along = None
    for q in decks:                         # (the upper)
        mu = pcs[q]["main"]
        xu = np.asarray(smp[q]["xy"], np.float64)
        if len(xu) < 2:
            continue
        a, ab = xu[:-1], np.diff(xu, axis=0)
        L2 = np.maximum((ab * ab).sum(1), 1e-12)
        half = float(net.m[mu].half)
        lo_u, hi_u = xu.min(0) - half, xu.max(0) + half
        for p in decks:                     # (the lower)
            md = pcs[p]["main"]
            if p == q or _over(net.m[mu], net.m[md]) != "i":
                continue
            xd = np.asarray(smp[p]["xy"], np.float64)
            near = np.flatnonzero(((xd >= lo_u) & (xd <= hi_u)).all(1))
            if not len(near):
                continue
            t = np.clip(((xd[near, None, :] - a[None]) * ab[None]).sum(2) / L2[None], 0.0, 1.0)
            dist = dnp.hypot(*(a[None] + t[..., None] * ab[None] - xd[near, None, :]).transpose(2, 0, 1))
            k = np.argmin(dist, axis=1)
            under = np.flatnonzero(dist[np.arange(len(near)), k] <= half)
            g = max(max_grade(net.m[mu]), max_grade(net.m[md]))
            for r_ in under.tolist():
                n_, kk = int(near[r_]), int(k[r_])
                tt = float(t[r_, kk])
                su = float(smp[q]["ms"][kk] + tt * (smp[q]["ms"][kk + 1] - smp[q]["ms"][kk]))
                need = clearance_need(net, mu, md, xd[n_])
                if along is None:
                    along = _deck_paths(net, sysm)
                if along(md, float(smp[p]["ms"][n_]), mu, su) < need / g:
                    continue
                out += [(q, kk, p, n_, need), (q, kk + 1, p, n_, need)]
    return out


def _solve_shape_qp(net, sysm, pcs, smp, ground, fixed_z, diagnostics):
    import carriageways as _cw
    chains = []
    for pc, x, t in zip(pcs, smp, ground):
        t = np.asarray(t, np.float64)
        n = len(t)
        lo, hi = np.full(n, -np.inf), np.full(n, np.inf)
        if pc["kind"] == "deck":
            target, w = _deck_targets(net, sysm, pc, t)
            # (a span over the ground: its girders off it, a deck that crosses a road, a track or another deck too: a
            # crossing holds a deck up only where it crosses. Kawazu's Nanadaru loop, one road's three decks crossing
            # each other and two streets, had no floor at all: between its crossings each deck went the straight way
            # between its ends' ground, its lowest turn 18 m inside the hill it winds round. A deck a road is dug
            # under stays at its ground (_dug_under), and over a road the system digs the ground is that road's cut,
            # the crossing's clearance its floor: _over_dips)
            if not _dug_under(sysm["chain_of"].get(pc["main"], 0.0), pc["main"] in sysm.get("held", ())):
                dv = x["d"]
                inner = (dv - dv[0] >= DECK_INNER_M) & (dv[-1] - dv >= DECK_INNER_M) & ~_over_dips(net, pcs, smp, x)
                lo = np.where(inner, t + deck_depth(net.L[pc["main"]]) + DECK_AIR_M, -np.inf)
        else:
            target, w = t.copy(), np.full(n, W_DIP)
            hi = t.copy()                                   # (a road going under never rises over its ground)
        g, rad = _law(net, pc)
        # (the grade law per step on the closest two samples: a piece through joints has them closer than its mean
        # step, and held at the mean its grade ran 1.8 % over the law there)
        dd = np.diff(np.asarray(x["d"], np.float64))
        step = float(dd[dd > 1e-6].min()) if (dd > 1e-6).any() else float(STEP_M)
        chains.append(dict(t=target, w=w, lo=lo, hi=hi, grade=g, K=rad / 100.0, lam=1e-3, step=step, length=0.0))
    cps = []
    for i, si, j, sj in sysm["joins"]:
        x, y = _var(pcs, smp, i, si), _var(pcs, smp, j, sj)
        if x and y:
            cps.append((x[0], x[1] if x[3] < 0.5 else x[2], y[0], y[1] if y[3] < 0.5 else y[2], "eq", 0.0))
    for p, a, q, b in _overlaps(pcs, smp):
        cps.append((p, a, q, b, "eq", 0.0))
    # (the clearance wherever the road runs under the upper part, under_pairs: its whole carriageway under the deck's)
    clear, fz = {}, {}

    def fixed(i, sv):
        new = [s_ for s_ in sv.tolist() if (i, s_) not in fz]
        if new:
            for s_, z_ in zip(new, np.asarray(fixed_z(i, np.array(new)), np.float64).tolist()):
                fz[(i, s_)] = z_
        return np.array([fz[(i, s_)] for s_ in sv.tolist()])
    for c in sysm["crossings"]:
        need = clearance_need(net, c["u"], c["d"], c["q"])
        uv = np.array([p_[0] for p_ in c["under"]])
        dv = np.array([p_[1] for p_ in c["under"]])
        xp, xa, xb, xt = _vars(pcs, smp, c["u"], uv)
        yp, ya, yb, yt = _vars(pcs, smp, c["d"], dv)
        both = (xp >= 0) & (yp >= 0)
        for kx, mx in _weighted(xa, xb, xt):
            for ky, my in _weighted(ya, yb, yt):
                m = both & mx & my
                for key in set(zip(xp[m].tolist(), kx[m].tolist(), yp[m].tolist(), ky[m].tolist())):
                    clear[key] = max(clear.get(key, -math.inf), need)
        m = (xp >= 0) & (yp < 0)            # (the road under read, fixed_z: the deck over it held over it)
        if m.any():
            u_s, inv = np.unique(dv[m], return_inverse=True)
            zd = fixed(c["d"], u_s)[inv]
            for kx, mx in _weighted(xa[m], xb[m], xt[m]):
                ok = mx & np.isfinite(zd)
                for p_ in np.unique(xp[m][ok]).tolist():
                    q_ = ok & (xp[m] == p_)
                    np.maximum.at(chains[p_]["lo"], kx[q_], zd[q_] + need)
        m = (xp < 0) & (yp >= 0)            # (the part over read: the road under it held under it)
        if m.any():
            u_s, inv = np.unique(uv[m], return_inverse=True)
            zu = fixed(c["u"], u_s)[inv]
            for ky, my in _weighted(ya[m], yb[m], yt[m]):
                ok = my & np.isfinite(zu)
                for p_ in np.unique(yp[m][ok]).tolist():
                    q_ = ok & (yp[m] == p_)
                    np.minimum.at(chains[p_]["hi"], ky[q_], zu[q_] - need)
    cps += [(p, kx, q, ky, "ge", need) for (p, kx, q, ky), need in sorted(clear.items())]
    for q, kq, p, kp, need in _stacked(net, sysm, pcs, smp):
        cps.append((q, kq, p, kp, "ge", need))
    info = {}
    zs = _cw.solve(chains, cps, diagnostics=info)
    if diagnostics is not None:
        diagnostics.update(info)
    if not info.get("converged") or not info.get("feasible"):
        return None
    return [np.asarray(z, np.float64) for z in zs]


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
    # (the shape's leaving grade at most the road's own: a deck's end left at 10x its grade made a ramp that climbed
    # to 10 km, the High Five's secondaries held at 8,200-10,900 m)
    g0 = float(np.clip(g0, -g, g))
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
            mem = np.asarray(x["mem"])
            gr = np.empty(len(mem))
            for m_ in np.unique(mem):               # (each member's ground in one read, not a read a sample)
                sel = mem == m_
                gr[sel] = ground_along(int(m_), np.asarray(x["ms"])[sel])
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
