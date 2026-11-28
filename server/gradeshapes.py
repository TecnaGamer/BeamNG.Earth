"""Tunnel and bridge grading: each interchange solved as one shape, then placed on the ground with its embankments
(BNE_SHAPES=1, off by default).

1. The system. Every road deck, short road tunnel (an underpass) and short rail deck, with what it joins and what it
   crosses, is one interchange with every other one it joins end to end, crosses, or meets through a short road
   between them (a ramp from one deck to the next). A single bridge or tunnel is a system of its own.

2. Who moves, at each crossing of an upper part over a lower one:
   - the lower road is mapped in a tunnel (an underpass): it goes down, the upper stays at its level (a track or a road
     at grade over it stays on its ground); a longer tunnel (TUNNEL_MAX_M) is tunnels.py's, crossed by nothing here;
   - the upper is a rail deck over a road on the ground: the railways' (railways.profile asks findground: the track
     rises over the road where the cost law says it is the one that moves, else grading's underpasses dig the road under
     it), nothing to solve here;
   - the upper is a road deck over a road on the ground: findground's cost law (the deck's own run carries part of the
     rise, the rest is fill: whichever of that fill and the road's cut is shorter), with the lack the elevation leaves
     (_Verdicts); a held deck (a bridge the map has over a road's short tunnel, grading's never-raised deck: the elevation
     there is its own) is at its ground and the roads under it dug. A short deck over a minor road digs it (the old
     ROAD_SHORT_M: 20 m, now what the grade laws give); a track under a deck is never dug (its cut at 5 % is the longest);

3. The shape. The heights of the system's decks, and of the roads that go down under its crossings, in one problem:
   every crossing clears the vehicle height the road under needs (its signed limit, else by class; rail's catenary) plus
   the deck, and so does a deck all along another it runs over (a road winding over itself), not only where their
   lines cross; every deck keeps its girders off the ground under its span (_abutments: not where it lies on the
   ground at its ends, nor over a road dug under it); every deck holds the grade its class and speed allow and the
   joints between decks meet. Within those, a deck keeps its natural height (findground, rule 1: the line between the
   ground at the ends of its chain) and rises as a whole by what its clearances lack (rule 3), never by a bump over
   each of them with its ends falling away from it: its ends weigh where it meets the road on the ground (the smallest
   embankment there), its line is held by its stiffness (W_STIFF). A road going under holds
   the shallowest dip. What must be at ground level (a road under an overpass, a track over an underpass) is at its
   ground: the shape is placed on it.

4. The embankments. Where the shape's end does not meet the road on the ground it joins, that road leading up to it is
   an earth embankment (or a cut, for a road going down) as steep as the road's max grade, as long as it takes to
   intercept the ground: the touch point is where the ramp meets the ground, a result, never an anchor. A road going
   under is cut down to its dip and back up the same way. Two ends whose ramps meet before the ground does (systems
   close to each other on one road, single bridges included) are joined by the straight line between them (lerped).

Pure: members in, shapes and ramps out. grading.py reads the members from its cells and applies the result to its
plans (the decks through roads.DECK_Z_HOOK, the tunnels' floors through roads.TUNNEL_Z_HOOK); the terrain follows the
plans (cut and fill).
"""
import heapq
import math
import threading
from collections import OrderedDict, namedtuple

import numpy as np

import dmath

import dnp
import findground
import gradelaw
import railways

import logging

log = logging.getLogger("earthd.grading")

STEP_M = 4.0                # m between a deck's or a dip's samples
CUT_MIN_M = 0.05            # m: a join's arc this near a sample is that sample
JOIN_M = 2.5                # m: two parts' ends (or an end and another's line) this close meet there
NODE_M = 6.0                # m: a map node of both this near where they meet is that meeting (_mapped_at)
NODE_EPS_M = 0.05           # m: two lines' points this close are one map node (a road whose nodes are not known: both
#                             drawn from OpenStreetMap's coordinates of it, which agree to a centimetre)
CROSS_END_M = 2.0           # m: a crossing this near either part's end is a joint, not a separation
MERGE_DEG = 40.0            # deg: a deck's end on another deck's line at another level meets it only along it (a ramp's
                            # merge or diverge, its gore's taper: Vienna's A23 at 29 deg, Gravelly Hill's links at 6) and
                            # only if they do not cross; across it, it is over or under it (the High Five's level-1 links
                            # ending under its level-4 flyovers at 55-89 deg)
STRUCTURES = ("deck", "rail_deck", "tunnel")
# the game's decks (roads.py: MIN_BRIDGE_HEADROOM_M, BRIDGE_HEADROOM_MARGIN_M, deck_depth; grading sets them from there):
# the vehicle height under the girders, its margin, and the deck's depth by its span (slab + girders)
HEADROOM_M = 4.0
RAIL_HEADROOM_M = 6.0       # a track's (roads.RAIL_HEADROOM_M: a wagon and the catenary over it)
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
#                             nothing: an interchange takes in a road as far as its ramps reach, _ramp_reaches)
RAMP_MAX_M = 600.0          # m: the longest embankment or cut followed along a road
REACH_MAX_M = 1800.0        # m: how far from its end a deck's reach is followed, the junctions' chains included (a
                            # ramp is back on its ground, or meets another deck's, within this of the end it left;
                            # farther on is a grid wandering, not a ramp: a rising ground the cuts cannot climb back
                            # to sent them along every street of a town's grid, minutes of the extent step. A deck
                            # whose ends are each further than this from every other rising deck's is not laid at all)
DIP_MARGIN_M = 3.0          # m past the upper part's edge a road going under holds its dip before its cut ramps up
FOOT_STEP_M = 1.0           # m between the points of a crossing's footprint (under_pairs)
DECK_MAX_GRADE = 0.06       # a deck climbs no steeper than this (the game's: roads.DECK_MAX_GRADE, grading sets it from there)
DECK_AIR_M = 0.5            # m: a span over the ground keeps its girders this far off it (past its abutments: _abutments)

CLEAR_PATH_M = 3.0          # m: a path's headroom (no vehicle on it)

W_HOLD = 100.0              # a short deck chain (a road dug under it) at its ground
RAILS = ("rail", "rail_deck")
# A track held at the bed the railways lay (its distance from it, per sample, squared). What moving a way costs is the earth,
# and findground's law has it as the run of its ramp, lack / grade (findground.dip_run): a way that may climb only at a
# track's grade runs longer for the same move than any road, by the ratio of their grades. The cheapest way to move is the
# steepest road the laws allow (gradelaw: a link in a village, 17 %), a track's grade is railways.GRADE: so a track costs
# that ratio times a road's dip to move, the least it can cost, and it moves only where nothing cheaper closes the conflict.

Member = namedtuple("Member", "key line kind cls kmh level half limit nodes held node_xy", defaults=(None, False, None))
# key: hashable, unique, the same from every cell; line (k, 2) frame ENU; kind: "ground", "deck", "tunnel", "rail",
# "rail_deck"; cls: road class ("rail" for track); kmh: speed limit or None; level: the map's level (OSM's layer) of a
# deck or a rail deck, 0 on the ground (_over: the order of two crossing members of one kind); half: half
# width (m); limit: the road's signed height limit (m), a callable q (x, y) -> m or None (its signs, read where it
# crosses: grading's roads.road_sign_at, the dips' own) or None; nodes: its road's map nodes (a frozenset of connector
# ids) or None (not known: a track); held: a deck never raised (grading's UNDER_TUNNEL_FIXED: the map has the road
# under it in a short tunnel), its chain at its ground and the roads under it dug, whatever its length; node_xy: where
# each of its nodes lies ((connector id, x, y) frame ENU; None: not known), the place a join of its road is (_mapped_at)

PATHS = gradelaw.PATHS
MINOR = {"residential", "service", "unclassified", "living_street", "road"}
DEFAULT_KMH = gradelaw.DEFAULT_KMH
LINK_KMH = gradelaw.LINK_KMH
GAME_GRADE_PLUS = gradelaw.GAME_GRADE_PLUS
# (the grade laws are gradelaw's: findground reads them too, for what moving a way costs)


def _base(cls):
    return gradelaw.base(cls)


def speed(m):
    """km/h member m is driven at: its limit, else its class's (a link's LINK_KMH)."""
    return gradelaw.speed(m.cls, m.kmh)


# (the laws of a member are its class, speed and kind alone: asked 160,000 times a region (max_grade 49,000, deck_depth
# 39,000, deck_grade 16,000, curve_radius 22,000) for a thousand distinct members, by the growth's rounds, the finds, the
# reaches and the steps alike. By the member's own law key, each law is made once a run; the laws' own values are in
# the key too (grading sets them from roads.py at its import), so one set anew is made anew)
_LAWS_MAX = 65536
_laws_memo = {}
_laws_lock = threading.Lock()


def _law_value(v):
    """A law's value as a key's part: a set (the path classes) as its sorted tuple, the rest as it is."""
    return tuple(sorted(v)) if isinstance(v, (set, frozenset)) else v


def _by_law(fn, name, laws=(), gradelaws=()):
    """fn(m) of a member's law key alone (kind, class, speed, level) and the laws' values it reads (names in this
    module, and in gradelaw: read at each ask, so a law set anew is made anew), made once for each: the same float
    every time."""
    def run(m):
        key = (name,) + tuple(_law_value(globals()[n]) for n in laws) + tuple(_law_value(getattr(gradelaw, n)) for n in gradelaws) \
            + (m.kind, m.cls, m.kmh, getattr(m, "level", None))
        got = _laws_memo.get(key)
        if got is None:
            got = _laws_memo[key] = fn(m)
            if len(_laws_memo) > _LAWS_MAX:
                with _laws_lock:
                    _laws_memo.clear()
                    _laws_memo[key] = got
        return got
    run.__name__ = fn.__name__
    run.__doc__ = fn.__doc__
    run._unmemoized = fn
    return run


def max_grade(m):
    """The steepest grade member m may hold (gradelaw.road_grade: by class and speed, AASHTO's design grades plus
    GAME_GRADE_PLUS; a path 12 %); a track railways.GRADE (the one law of a track: this was its own 2.5 %, beside
    railways' 2 % for the decks' ramps and the lifts, and the seam's literal 2 %)."""
    if m.kind in ("rail", "rail_deck"):
        return railways.GRADE
    return gradelaw.road_grade(m.cls, m.kmh)


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
    where it has one (a maxheight sign), else a full-height truck's 4 m (a track's: a wagon and its catenary), a
    path's less."""
    lim = m.limit
    if callable(lim):
        lim = lim(q) if q is not None else None
    if lim is not None and lim > 0:
        return float(lim) + HEADROOM_MARGIN_M
    if m.kind in ("rail", "rail_deck"):
        return RAIL_HEADROOM_M + HEADROOM_MARGIN_M
    if m.cls in PATHS:
        return CLEAR_PATH_M
    return HEADROOM_M + HEADROOM_MARGIN_M


def _deck_depth_unmemoized(span):
    """m of structure under a deck's road for a span of span m (the game's: span / 22, 0.9 to 2.2 m)."""
    return float(np.clip(float(span) / DECK_SPAN_RATIO, DECK_MIN_DEPTH_M, DECK_FULL_DEPTH_M))


def structure_depth(net, u):
    """m of structure under the road of upper part u: a deck's depth for its span, a rail deck's, an underpass's roof."""
    mu = net.m[u]
    return deck_depth(net.L[u]) if mu.kind == "deck" else RAIL_DECK_DEPTH_M if mu.kind == "rail_deck" else COVER_M


def clearance_need(net, u, d, q=None):
    """How far the road on upper part u stands over lower part d where they cross (at q): the headroom d needs there
    and u's structure (structure_depth)."""
    return headroom(net.m[d], q) + structure_depth(net, u)


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
    return np.array([dnp.interp(sq, s, line[:, 0]), dnp.interp(sq, s, line[:, 1])])


class Net:
    """The members' geometry: their lines, and their arc lengths and boxes from the process's line network (linenet:
    every line the solve reads in one index, each added once), which holds their joints and crossings too
    (_relations)."""

    def __init__(self, members, network=None):
        """network: a linenet.Network (a growth's, for all its rounds: Scan.network), else the process's."""
        import linenet
        self.m = members
        self.lines = [np.asarray(m.line, np.float64)[:, :2] for m in members]
        self.network = network if network is not None else linenet.current()
        self.ln_ids, self.ln_at = self.network.bind(self)
        self.s = self.network.ln.arclens(self.ln_ids)
        self.box = self.network.ln.boxes(self.ln_ids)
        self.L = [float(s[-1]) for s in self.s]

    def near(self, i, pad):
        """The members other than i whose boxes come within pad of i's, ascending: the audits' and the staged
        pipeline's question (the solve's is _relations, the line network's)."""
        b, bx = self.box[i], self.box
        hit = (bx[:, 0] <= b[2] + pad) & (bx[:, 2] >= b[0] - pad) & (bx[:, 1] <= b[3] + pad) & (bx[:, 3] >= b[1] - pad)
        return [int(j) for j in np.flatnonzero(hit) if j != i]


def _is_seed(net, i):
    m = net.m[i]
    if m.kind == "deck":
        return True
    if m.kind == "tunnel":
        return net.L[i] <= TUNNEL_MAX_M
    return False            # (a rail deck: the railways', held at its bed; in a system only as what a seed crosses)


_LINE_ID = {}              # (repr(key), len(line)) -> a member's line id: a growth's rounds each make a new net, and
_LINE_ID_MAX = 100000      # (62,000 members hashed a round was a second of each: Yokohama; ~35 MB at the cap)
                           # ramp, 44 MB at most; a giant interchange lays 1,200 a round)
#                            the growths of the interchanges in one district lay the same ramps off the same decks, each from
#                            the start (Yokohama: 44 growths in one region, 40 of its 57 s in the ramps' reaches)


def _line_id(net, i):
    ids = getattr(net, "_line_ids", None)
    if ids is None:
        ids = net._line_ids = {}
    got = ids.get(i)
    if got is None:
        a = np.ascontiguousarray(net.lines[i], np.float64)
        # (the cache's key: the member's key, its nodes, its length and its two ends, which two frames' lines of one way
        # never share; the hash of the whole line only a miss pays. A member's line is immutable in a frame)
        k = (repr(net.m[i].key), len(a), net.m[i].nodes, float(a[0, 0]), float(a[0, 1]), float(a[-1, 0]), float(a[-1, 1]))
        got = _LINE_ID.get(k)
        if got is None:
            got = (k[0], len(a), hash(a.tobytes()), net.m[i].nodes)
            if len(_LINE_ID) >= _LINE_ID_MAX:
                _LINE_ID.clear()
            _LINE_ID[k] = got
        ids[i] = got
    return got


def _relations(net, i):
    """Member i's joints and crossings with the members near it: [(j, "join"|"cross", s on i, s on j, q)], j ascending.
    The pairs' geometry is the process's line network's (linenet: the lines near each other paired once, as they are
    added), what each contact is _pair_from's on this net's members. A member of fewer than two points has none (no
    segment: no line to meet; the pairs' own geometry cannot project onto it)."""
    if len(net.lines[i]) < 2:
        return []
    import linenet
    out = []
    for j, hits, ends in linenet.geometry(net, i):
        out.extend((j,) + r_ for r_ in _pair_from(net, i, j, hits, ends))
    return out


def _tangent(line, s, sq):
    a, b = at_s(line, s, max(sq - 2.0, 0.0)), at_s(line, s, min(sq + 2.0, float(s[-1])))
    v = b - a
    return v / max(float(dnp.hypot(*v)), 1e-9)


def _proj_many(line, s, P, near=None):
    """proj for many points P (k, 2): arc lengths along line (arc lengths s) of the points nearest them, distances.
    near: only the pairs a caller keeping pairs by distance could keep are computed: a point against the segments
    whose box, grown by near, holds it (a segment further than near from a point's x or y range is further than
    near from the point: a bound, so no pair within near is lost); a point with no such segment has distance inf
    and the arc of its first candidate, had it one (the callers test the distance alone). The ramp's junctions
    asked it of every road in its box, 54,000 times a region, and nothing was within JOIN_M in 39 % of them, 70 %
    of the seconds (Boston's Bowker: the long ramps' 1,600 points against every line of the box)."""
    a, ab = line[:-1], line[1:] - line[:-1]
    L2 = np.maximum((ab * ab).sum(1), 1e-12)
    P = np.asarray(P, np.float64)
    if near is not None and len(P) * len(a) > 4096:
        sx0, sx1 = np.minimum(a[:, 0], line[1:, 0]) - near, np.maximum(a[:, 0], line[1:, 0]) + near
        sy0, sy1 = np.minimum(a[:, 1], line[1:, 1]) - near, np.maximum(a[:, 1], line[1:, 1]) + near
        may = ((P[:, 0, None] >= sx0[None]) & (P[:, 0, None] <= sx1[None])
               & (P[:, 1, None] >= sy0[None]) & (P[:, 1, None] <= sy1[None]))
        rows = np.flatnonzero(may.any(1))
        if not len(rows):
            return np.full(len(P), float(s[0])), np.full(len(P), np.inf)
        if len(rows) < len(P) or not may.all():
            # (only the points with a candidate, each against its own: the same arithmetic on the kept pairs)
            sj = np.full(len(P), float(s[0]))
            dj = np.full(len(P), np.inf)
            Q = P[rows]
            t = np.clip(((Q[:, None, :] - a[None]) * ab[None]).sum(2) / L2[None], 0.0, 1.0)
            v = a[None] + t[..., None] * ab[None] - Q[:, None, :]
            dist = np.where(may[rows], dnp.hypot(v[..., 0], v[..., 1]), np.inf)
            j = np.argmin(dist, axis=1)
            r = np.arange(len(rows))
            sj[rows] = s[j] + t[r, j] * (s[j + 1] - s[j])
            dj[rows] = dist[r, j]
            return sj, dj
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
    crossing. Also kept for every net that meets the same two lines at the same place (_UNDER: a region grown cell by
    cell and the regions round it each made a new net and made the same footprints again, 9,994 calls for ~2,000
    crossings, 45 of the 87 profiled s of a Shanghai region's find)."""
    memo = net.__dict__.setdefault("_under", {})
    key = (u, round(float(su), 6), d, round(float(sd), 6))
    got = memo.get(key)
    if got is not None:
        return got
    mu, md = net.m[u], net.m[d]
    gkey = (_line_id(net, u), _line_id(net, d), mu.kind == "deck", float(mu.half), float(md.half), float(su), float(sd))
    with _UNDER_LOCK:
        kept = _UNDER.get(gkey)
        if kept is not None:
            _UNDER.move_to_end(gkey)
    if kept is not None:
        out = [(float(su), float(sd))] + list(zip(kept[0].tolist(), kept[1].tolist()))
        memo[key] = out
        return out
    out = _under_pairs_calc(net, u, su, d, sd)
    rest = out[1:]
    with _UNDER_LOCK:
        if gkey not in _UNDER:
            _UNDER[gkey] = (np.array([p_[0] for p_ in rest], np.float64), np.array([p_[1] for p_ in rest], np.float64))
            _UNDER_N[0] += len(rest) + 1
            while _UNDER_N[0] > _UNDER_PAIRS_MAX and len(_UNDER) > 1:
                _UNDER_N[0] -= len(_UNDER.popitem(last=False)[1][0]) + 1
    memo[key] = out
    return out


_UNDER = OrderedDict()          # (the two lines, which is the deck, their half widths, the crossing) -> the footprint's
_UNDER_LOCK = threading.Lock()  # pairs after the first (arrays), least recently used let go
_UNDER_N = [0]
_UNDER_PAIRS_MAX = 4_000_000    # pairs kept (16 bytes each)


def _under_pairs_calc(net, u, su, d, sd):
    """under_pairs, made (not looked up): [(su, sd)] and the footprint's pairs."""
    a_, sa, b_, sb = (d, sd, u, su) if net.m[u].kind == "deck" else (u, su, d, sd)
    la, lsa, lb, lsb = net.lines[a_], net.s[a_], net.lines[b_], net.s[b_]
    ha, hb = float(net.m[a_].half), float(net.m[b_].half)
    ta, tb = _tangent(la, lsa, sa), _tangent(lb, lsb, sb)
    sin = abs(float(ta[0] * tb[1] - ta[1] * tb[0]))
    # (each line as far as straight lines at the crossing's angle overlap round it; a curve's points past it fail the
    # test below. The other's line read there alone: a road passing near its own start again (a loop's deck over the
    # deck it goes on from) has its nearest point on that stretch, not on the one over this crossing)
    c = abs(float(dnp.dot(ta, tb)))
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
        return out
    nc = len(cv)

    def rows(rs):
        """The lattice's rows rs: their points' arc lengths on a and on b, and which lie under."""
        sr = sv[rs]
        k = np.clip(np.searchsorted(lsa, sr, side="right") - 1, 0, len(lsa) - 2)
        t = la[k + 1] - la[k]
        t = t / np.maximum(dnp.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None]
        base = np.stack([dnp.interp(sr, lsa, la[:, 0]), dnp.interp(sr, lsa, la[:, 1])], axis=1)
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
    return out


def _road_of(m):
    """The map road member m is part of: its key's feature id (a deck's or a ground part's), else its key. A lane
    section ("<road>~lanes<a>-<b>", lanesections.py: one road cut where its lane count changes) is its road's."""
    return str(m.key[0] if isinstance(m.key, tuple) else m.key).partition("~lanes")[0]


def way_of(m):
    """(family, OSM way) of member m: a track's key ("rail", kind, way, ...), a road's (way[~lanes...], ...)."""
    k = m.key
    if isinstance(k, tuple) and len(k) > 2 and k[0] == "rail":
        return "rail", str(k[2])
    return "road", str(k[0] if isinstance(k, tuple) else k).split("~lanes")[0]


def one_road(members):
    """members without the unsplit copies of roads that are also there as lane sections. A road crossing cells is cut
    into lane sections in the cells with lane tags ("<road>~lanes<a>-<b>", lanesections.py) and stays whole in a neighbour
    without them (replaced_roads: "keep the original if only part of its replacement is available"): the region's members
    from several cells then held the road twice, a 46 m deck and its 53 m sections a lane's width apart, and the tunnels it
    goes into twice, 21 ends where the road has 6: Vienna's A23 deck rose 2.3 m over its own ground in the world's solve
    (z 162.4) where the 300 m replay, with one copy, kept it at 160.2, its carriageway beside it at 160.2 (1.7 m step, the
    trucks into it). A copy lying along a section of its own road, within the widths of the two, is that road's section's.
    Then one_part: a deck or tunnel part twice."""
    lanes = {}
    for m in members:
        if "~lanes" in str(m.key[0] if isinstance(m.key, tuple) else m.key):
            lanes.setdefault((_road_of(m), m.kind), []).append(m)
    if not lanes:
        return one_part(members)
    out = []
    for m in members:
        key = str(m.key[0] if isinstance(m.key, tuple) else m.key)
        if "~lanes" not in key and (key, m.kind) in lanes and len(m.line) >= 2:
            line = np.asarray(m.line, np.float64)
            if any(_near_line(line, np.asarray(o.line, np.float64), float(m.half + o.half)).all()
                   for o in lanes[(key, m.kind)]):
                continue
        out.append(m)
    return one_part(out)


def one_part(members):
    """members without the second copy of a deck or tunnel part. A part's key is its road's and the middle of its line, so
    the same part read from two neighbouring cells, its line cut a vertex or a few metres differently (or its width
    measured differently), is two members, and the system took each for a deck going on in the other: its end on the other's
    line was a joint, no end of the structure, so it was neither held to the ground it lands on nor given the abutment
    it has no girders over, and its floor (the ground and the girders' depth) stood under it to the very last sample,
    metres over the hill it lands on; the joint of the carriageways it goes on from was raised with it, and the carriageways
    beside each other came out apart in height. A part lying along another of its own road and kind, within the widths of
    the two, from end to end, is that part's copy: the longer one stays (the first by key where they are as long)."""
    by = {}
    for n, m in enumerate(members):
        if m.kind in ("deck", "tunnel") and len(m.line) >= 2:
            by.setdefault((_road_of(m), m.kind), []).append(n)
    drop = set()
    for ids in by.values():
        if len(ids) < 2:
            continue
        lines = {n: np.asarray(members[n].line, np.float64)[:, :2] for n in ids}
        length = {n: float(arclen(lines[n])[-1]) for n in ids}
        kept = []
        for n in sorted(ids, key=lambda n: (-round(length[n], 3), str(members[n].key))):
            if any(_near_line(lines[n], lines[o], float(members[n].half + members[o].half)).all() for o in kept):
                drop.add(n)
            else:
                kept.append(n)
    return [m for n, m in enumerate(members) if n not in drop] if drop else members


def _end_meets(net, e, se, o, so, crossed):
    """Whether member e's end (at arc length se) on member o's line (at so) is where they meet. Two parts of one map
    road meet where they lie (a road going on). Two roads meet only where the map joins them, a node of both at that
    place (_mapped_at), at any levels: a T, a ramp onto its road, a deck's end on the road it lands on; and between
    two structures at different levels only end to end, or a deck merging along another deck (MERGE_DEG) that it does
    not also cross. Not where they only lie near each other: two structures at different levels touching in plan (a
    deck's or a tunnel's end over or under another's end or middle: a separation, not a joint, its clearance kept: in
    Barcelona a level-2 service deck's ends lay on a level-1 one's ends in plan, taken for joints at both: 13 such in
    one 14-deck system, most of its relaxed solve's shortfall); a deck's end over a road on the ground (four link
    decks over the S1 at Breitenleer, no node of it, pulled down onto the motorway and it up to them; Wien
    Hauptbahnhof's rail decks ending 0.5 m off the line of the busway they bridge, the busway held at the tracks'
    height and 5.83 m under them at once); a road on the ground ending under a deck's middle (or over a tunnel's):
    the High Five's secondary leaving its level-1 link's end 0.1 m off the line of a level-4 flyover 20 m over it,
    taken for that flyover's end on it, ran a 290 m embankment down from the flyover's height along the street, and
    the street under the next flyover met its deck. Two decks of one map road meet only end to end, never one's end
    on the other's middle: a road passing
    over itself (Kawazu's Nanadaru loop, a spiral of one road at levels 1 to 3, its level-2 deck ending right over
    its level-1 deck on its heading where the level-3 one goes on) was taken for a merge, held at the deck under it
    there and 8.35 m over it on the level-3 deck's start: the loop infeasible. A road's own lane sections meeting end
    to end are a road going on, whatever levels the map gave them and whether or not their node sets share a node
    (one carriageway's sections were tagged level 1 and 2 in Shanghai's Yan'an elevated road): taken for a
    separation, their clearance contradicted their joint (12.7 m round a loop of four), an infeasible system left
    as it was. crossed: e and o cross each other (a
    road cannot meet the one it passes over or under a few metres on: Gravelly Hill's level-1 M6 deck under its
    level-3 one at 29 deg, its end 5 m past their crossing)."""
    me, mo = net.m[e], net.m[o]
    one = _road_of(me) == _road_of(mo)
    if _over(me, mo) is None:
        return one or _mapped_at(net, e, se, o)
    mapped = one or _mapped_at(net, e, se, o)
    if mo.kind not in STRUCTURES or me.kind not in STRUCTURES or min(so, net.L[o] - so) <= JOIN_M:
        return mapped
    if me.kind == mo.kind == "deck" and not crossed and not one and mapped:
        c = abs(float(dnp.dot(_tangent(net.lines[e], net.s[e], se), _tangent(net.lines[o], net.s[o], so))))
        return c >= dmath.cos(math.radians(MERGE_DEG))
    return False


def _own_node(m, p):
    """Member m's map node nearest point p, within NODE_M; None."""
    if not m.node_xy:
        return None
    d, c = min((math.hypot(x - p[0], y - p[1]), c) for c, x, y in m.node_xy)
    return c if d <= NODE_M else None


def _goes_on(net, a, p):
    """The other members member a's road goes on into at point p: a part of its own way with an end within JOIN_M of
    p, or a road the map joins it to there (a's own node there one of theirs, lying there: at its end or on its middle,
    a T)."""
    w = way_of(net.m[a])
    node = _own_node(net.m[a], p)
    out = []
    for c in net.near(a, JOIN_M):
        lc = net.lines[c]
        if len(lc) < 2:
            continue
        if way_of(net.m[c]) == w:
            if min(math.hypot(*(lc[0] - p)), math.hypot(*(lc[-1] - p))) <= JOIN_M:
                out.append(c)
        elif node is not None and any(n == node and math.hypot(x - p[0], y - p[1]) <= NODE_M
                                      for n, x, y in (net.m[c].node_xy or ())):
            out.append(c)
    return out


def _node_of_both_at(mi, mj, q):
    """Whether a map node of both members lies at point q (within JOIN_M on each: their nodes are placed along their
    lines, _cell_nodes)."""
    if not mi.node_xy or not mj.node_xy:
        return False
    at_i = {c for c, x, y in mi.node_xy if math.hypot(x - q[0], y - q[1]) <= JOIN_M}
    return any(c in at_i and math.hypot(x - q[0], y - q[1]) <= JOIN_M for c, x, y in mj.node_xy)


def _cross_through(a, b, tol=0.01):
    """Plan crossings of polylines a and b through both, not where either only touches the other with an end of its
    line: [q]."""
    out = []
    for _i, _j, _ta, _tb, h in _seg_hits(a, b):
        h = np.asarray(h, np.float64)
        if min(math.hypot(*(h - a[0])), math.hypot(*(h - a[-1])), math.hypot(*(h - b[0])),
               math.hypot(*(h - b[-1]))) > tol:
            out.append(h)
    return out


def _turns_beside(net, e, se, o, so):
    """Whether member e's end (at arc length se) lying by member o's line (at so), not joined to it, is where one of
    the two roads changes level beside the other: it goes on there (_goes_on: into a part of its own way, or a road the
    map joins it to there) on the other side of the other's level or at it (a tunnel's portal where its road rises onto
    a deck: under the other, then over it), and e's and o's lines do not cross through each other there (_cross_through:
    a line only ending on the other is no crossing). No height is clear of the other at such
    a point: it is no separation. Porte Maillot's primary left its tunnel straight onto a level-1 part, its portal 2.2 m
    from a service road's own portal beside it; each of its two ends by the service road was a separation of its own,
    the one point held 6.35 m over the service road and 5.15 m under it: its block had no answer. Suedtiroler Platz's
    building passage opened onto a 2 m aisle into a car park's ring 2 m from its portal, Kawazu's deck landed on a
    junction 2 m from the next one along a stub: the passage held 5.15 m under the ring, the deck 5.05 m over the road
    leaving the next junction, joined to both through 2 m of road. Where e's and o's own lines cross, the contact is
    kept (the map's contradiction, reported)."""
    up = _over(net.m[e], net.m[o])
    if up is None:
        return False
    flips = []
    for a, sa, b, first in ((e, se, o, True), (o, so, e, False)):
        if JOIN_M < sa < net.L[a] - JOIN_M:
            continue
        p = net.lines[a][0] if sa <= net.L[a] - sa else net.lines[a][-1]
        for c in _goes_on(net, a, p):
            u2 = _over(net.m[c], net.m[b]) if first else _over(net.m[b], net.m[c])
            if u2 != up:
                flips.append(p)
                break
    if not flips:
        return False
    q = flips[0]
    # (a crossing there of what either goes on into is that pair's contact, found with it: Montfort Drive's bridge
    # starting 1.4 m before LBJ's ramp deck crosses it, the ramp through the bridge, the street's ground part ending
    # short of the ramp: the ground part held 6.35 m under the ramp and the bridge 6.35 m over it, at their one joint)
    return not any(math.hypot(*(h - q)) <= NODE_M for h in _cross_through(net.lines[e], net.lines[o]))


def _mapped_at(net, e, se, o):
    """Whether the map joins members e and o where e's end (at arc length se) lies: a map node of both there (within
    NODE_M), not one of both somewhere else. Suedtiroler Platz's primary ended on the middle of the Guertel's tunnel
    under it, 1.8 m off its line; the two share a node, the tunnel's portal 100 m away, and were taken for joined there:
    the square held at the tube's height and 5.15 m over it at once. Where both members' nodes are placed (node_xy), e's
    node there (the nearest to it, within NODE_M) a node of o's lying there; where one's are not known (a busway the
    OpenStreetMap import adds has no node ids), a point of e's line there within NODE_EPS_M of a point of o's (one map
    node is one place)."""
    me, mo = net.m[e], net.m[o]
    q = np.array([np.interp(se, net.s[e], net.lines[e][:, 0]), np.interp(se, net.s[e], net.lines[e][:, 1])])
    if me.node_xy and mo.node_xy:
        # (e's own node there: the one of its nodes nearest the place, not any within NODE_M. Suedtiroler Platz's
        # 2 m parking aisle between a building passage and the car park's ring had both its ends within NODE_M of both
        # its nodes: each end was joined to both, the passage's end held at the ring's height and 5.15 m under it)
        d, c = min((math.hypot(x - q[0], y - q[1]), c) for c, x, y in me.node_xy)
        if d > NODE_M:
            return False
        return any(co == c and math.hypot(x - q[0], y - q[1]) <= NODE_M for co, x, y in mo.node_xy)
    le, lo = np.asarray(net.lines[e], np.float64)[:, :2], np.asarray(net.lines[o], np.float64)[:, :2]
    pe = le[np.hypot(*(le - q).T) <= NODE_M]
    po = lo[np.hypot(*(lo - q).T) <= NODE_M + NODE_EPS_M]
    if not len(pe) or not len(po):
        return False
    return bool((np.hypot(pe[:, None, 0] - po[None, :, 0], pe[:, None, 1] - po[None, :, 1]) <= NODE_EPS_M).any())


def _pair_relations(net, i, j):
    """Member i's joints and crossings with member j: [("join"|"cross", s on i, s on j, q)]. An end on the other's
    line is a joint, or (_end_meets) between structures at different levels a crossing at that end."""
    return _pair_from(net, i, j, *_pair_geometry(net, i, j))


def _pair_geometry(net, i, j):
    """(hits, ends) of members i and j, the plan's numbers alone (earthfast.LineNet makes the same to the bit):
    hits [(s on i, s on j, q)] where they cross away from both ends (CROSS_END_M), ends [(swap, s of the end, s on
    the other, q)] where an end of one (i's, then j's: swap) lies within JOIN_M of the other's line."""
    hits = []
    for a_i, b_j, ta, tb, q in _seg_hits(net.lines[i], net.lines[j]):
        si = net.s[i][a_i] + ta * (net.s[i][a_i + 1] - net.s[i][a_i])
        sj = net.s[j][b_j] + tb * (net.s[j][b_j + 1] - net.s[j][b_j])
        if min(si, net.L[i] - si, sj, net.L[j] - sj) >= CROSS_END_M:
            hits.append((si, sj, q))
    ends = []
    for e_line, e_s, o_line, o_s, swap in ((net.lines[i], net.s[i], net.lines[j], net.s[j], False),
                                           (net.lines[j], net.s[j], net.lines[i], net.s[i], True)):
        for se in (0.0, float(e_s[-1])):
            q = e_line[0] if se == 0.0 else e_line[-1]
            so, d = proj(o_line, o_s, q)
            if d <= JOIN_M:
                ends.append((swap, se, so, q))
    return hits, ends


def _pair_from(net, i, j, hits, ends):
    """_pair_relations from the pair's geometry (_pair_geometry's): what each contact is."""
    out = []
    for swap, se, so, q in ends:
        e, o = (j, i) if swap else (i, j)
        if _end_meets(net, e, se, o, so, bool(hits)):
            kind = "join"
        elif _turns_beside(net, e, se, o, so):
            continue
        else:
            kind = "cross"
        out.append((j, kind, so if swap else se, se if swap else so, q))
    # (one joint found from both its parts' ends once)
    uniq = []
    for r_ in out:
        if not any(u[0] == r_[0] and u[1] == r_[1] and abs(u[2] - r_[2]) < 1.0 and abs(u[3] - r_[3]) < 1.0
                   for u in uniq):
            uniq.append(r_)
    out[:] = uniq
    up = _over(net.m[i], net.m[j])
    for si, sj, q in hits:
        if any(r_[0] == j and r_[1] in ("cross", "join") and abs(r_[2] - si) < 1.0 for r_ in out):
            continue                        # (on a vertex: found on both its segments)
        if _node_of_both_at(net.m[i], net.m[j], q):
            # (the map joins them where they cross: one height, whatever their layers. A car park's floors mapped as
            # layers 1 and 2, no bridge, each crossing the aisles of the floor under it at a node of both (Barcelona's
            # bus depot by Carrer de Santander): held 6.35 m over the aisle they meet, the block had no answer)
            out.append((j, "join", si, sj, q))
            continue
        if up is not None and net.m[i].kind in STRUCTURES and net.m[j].kind in STRUCTURES:
            # (a crossing nearer a joint of the same two structures than the clearance takes at their grade, along both
            # to it, is that joint: one stretch of road meeting itself at a shallow angle, not one over the other, which
            # the joint holds at one height and the clearance at several metres apart: _stacked's rule. Only between
            # two structures: both heights are the shape's, the joint an equation. A deck's end on a road on the
            # ground is no equation (the road's own height is read, the deck's end weighs where it meets it), and a
            # deck that crosses that road a metre or two before its end is over it)
            u, d = (i, j) if up == "i" else (j, i)
            need = clearance_need(net, u, d, q)
            g = max(deck_grade(net.m[i]), deck_grade(net.m[j]))
            if any(r_[0] == j and r_[1] == "join" and abs(r_[2] - si) + abs(r_[3] - sj) < need / g for r_ in out):
                continue
        out.append((j, "cross", si, sj, q))
    return [r_[1:] for r_ in out]


LEVEL_BAND = {"deck": 1, "rail_deck": 1, "ground": 0, "rail": 0, "tunnel": -1}
WAY = {"deck": findground.BRIDGE, "rail_deck": findground.BRIDGE, "ground": findground.GROUND,
       "rail": findground.GROUND, "tunnel": findground.TUNNEL}      # (a member's kind as findground's)


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


Interchange = namedtuple("Interchange", "sysm paths_s extent_s parts", defaults=((),))
# sysm: the system (find_systems' record); paths_s: the seconds spent finding the relations of its members (the joints
# and crossings with what is near them), extent_s: those closing it (what merged its seeds, its dips, its record).
# parts: where the structures' ramps meet the interchange is several systems (each a record of its own, as find_systems
# gives them), sysm then only what names it (its seeds, dips, crossings and ends together) and each part its own; empty:
# the interchange is the one system


def _stretches(x, ks):
    """{member: (s from, s to)} the stretches of the members a ramp's first ks samples run along (its joints included: the
    stretch runs to the end of a member the ramp enters by)."""
    k = max(1, min(int(ks), len(x["xy"])))
    mem, ms = np.asarray(x["mem"])[:k], np.asarray(x["ms"])[:k]
    out = {}
    for j_ in np.unique(mem).tolist():
        sv = list(ms[mem == j_])
        sv += [s0_ for kk, jj, s0_ in x.get("joints", ()) if jj == j_ and kk < k]
        out[int(j_)] = (float(min(sv)), float(max(sv)))
    return out


def _system(net, seeds, rel, dig):
    """One interchange: its decks and tunnels, its crossings with who moves, its ends on the roads on the ground."""
    members = net.m
    S = set(seeds)
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
            who = findground.leaves(WAY[md.kind], WAY[mu.kind])
            if who == "lower":
                moves = "down"                  # (mapped as an underpass: it goes under, what is over it stays)
            elif who == "upper" and mu.kind == "deck":
                # (the deck rises, or the road goes under it: findground's cost law, asked with the lack where the
                # elevation is read (_Verdicts); a chain over a road's short tunnel is at its ground. A track under
                # it is never dug: its cut is the longest)
                moves = dig.moves(u, su, d, sd, q) if md.kind == "ground" else "up"
            else:
                moves = None                    # (at grade; or a rail deck: the railways raise their own)
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
    # the tracks the interchange crosses: a track is a member of it as a road going under is, solved with the roads, held at the
    # bed the railways lay (W_TRACK) over a window: the stretch under each crossing and what a track needs to leave its bed and
    # come back to it at its grade and curve
    twin = {}
    for c in crossings:
        for m_, k_ in ((c["u"], 0), (c["d"], 1)):
            if members[m_].kind not in RAILS:
                continue
            g = max_grade(members[m_])
            w = clearance_need(net, c["u"], c["d"], c["q"]) / g + curve_radius(members[m_]) * g + DIP_MARGIN_M
            sv = [p_[k_] for p_ in c["under"]]
            twin.setdefault(m_, []).append((min(sv) - w, max(sv) + w))
    tracks = []
    for m_ in sorted(twin, key=lambda i: str(members[i].key)):
        rs = sorted(twin[m_])
        cur = [list(rs[0])]
        for a, b in rs[1:]:
            if a <= cur[-1][1] + STEP_M:
                cur[-1][1] = max(cur[-1][1], b)
            else:
                cur.append([a, b])
        tracks += [(m_, float(a), float(b)) for a, b in cur]
    digs = {i for i in seeds if members[i].kind == "deck" and dig.digs(i)}     # (the decks held at their ground)
    free = {i: dig.free(i) for i in seeds if members[i].kind == "deck"}        # (their structures' ends: rule 1)
    return dict(seeds=seeds, joins=joins, crossings=crossings, ends=ends, dips=dips, tracks=tracks, digs=digs, free=free)


KEY_DEG = 1.0e-4            # deg (about 11 m): the grid an interchange's centre is placed on in its key


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
    or None: of the roads whose end is there, the same road (feature) first, else the straightest. Made once per end of
    a net (the answer is the net's: its lines and keys do not change): a dip's road is walked on from both its ends for
    every crossing under it and again for every other crossing it is measured to, 1.5 million asks in nine rounds of a
    mesh's growth, 60 of the round's 140 seconds."""
    memo = net.__dict__.setdefault("_next_member", {})
    key = (i, bool(at_end))
    if key in memo:
        return memo[key]
    got = memo[key] = _next_member(net, i, at_end)
    return got


def _next_member(net, i, at_end):
    idx = _ends_index(net)
    line = net.lines[i]
    p = line[-1] if at_end else line[0]
    d_out = (line[-1] - line[-2]) if at_end else (line[0] - line[1])
    d_out = d_out / max(float(dnp.hypot(*d_out)), 1e-9)
    gx, gy = int(p[0] // 5.0), int(p[1] // 5.0)
    best = None
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for j, j_end in idx.get((gx + dx, gy + dy), ()):
                if j == i:
                    continue
                lj = net.lines[j]
                q = lj[-1] if j_end else lj[0]
                if float(dnp.hypot(*(q - p))) > JOIN_M:
                    continue
                d_in = (lj[-2] - lj[-1]) if j_end else (lj[1] - lj[0])
                d_in = d_in / max(float(dnp.hypot(*d_in)), 1e-9)
                same = net.m[j].key[0] == net.m[i].key[0]
                score = (0 if same else 1, -float(dnp.dot(d_out, d_in)), str(net.m[j].key))
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
    for t, a, b in sysm.get("tracks", ()):
        out.append(dict(kind="track", main=t, segs=path_of(net, t, a, b)))
    # (the arcs where another member joins each member: a join is held at one height, so it is a sample of the piece it falls in,
    # not held at the nearer of two samples that are up to half a step off it, STEP_M / 2 of grade each way: 0.05-0.11 m of
    # step between two decks at a T-join on Barcelona's trench)
    cuts = {}
    for i, si, j, sj in sysm["joins"]:
        cuts.setdefault(int(i), set()).add(float(si))
        cuts.setdefault(int(j), set()).add(float(sj))
    for pc in out:
        pc["cuts"] = {int(m): sorted(cuts[int(m)]) for m, _a, _b in pc["segs"] if int(m) in cuts}
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
            lo_, hi_ = min(s0, s1), max(s0, s1)
            extra = [a_ for a_ in pc.get("cuts", {}).get(int(j), ()) if lo_ + CUT_MIN_M < a_ < hi_ - CUT_MIN_M]
            if extra:
                up = np.sort(np.r_[sv, extra])
                up = up[np.r_[True, np.diff(up) > CUT_MIN_M]]       # (none twice: one within CUT_MIN_M of another is it)
                sv = up if s1 >= s0 else up[::-1]
            if n:
                sv = sv[1:]                  # (the joint once: its sample the last part's, and this one's at s0)
                joints.append((count - 1, j, float(s0)))
            if not len(sv):
                continue
            xy = np.stack([dnp.interp(sv, net.s[j], net.lines[j][:, 0]), dnp.interp(sv, net.s[j], net.lines[j][:, 1])],
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


def _pieces_of(smp, i):
    """The pieces (ascending) whose samples are on member i, or hold a joint's with it: the only ones _locate can find
    anything in. Made once for the pieces' samples (on the first's): looked up for every sample of every piece, _locate
    asked of each piece of a 1,300-piece Shanghai system 50 million times, 155 of the solve's 168 profiled s."""
    by = smp[0].get("_by") if smp else None
    if by is None:
        by = {}
        for p, x in enumerate(smp):
            for j in set(np.unique(x["mem"]).tolist()) | {int(j) for _k, j, _s in x.get("joints", ())}:
                by.setdefault(j, []).append(p)
        smp[0]["_by"] = by
    return by.get(int(i), ())


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


def _overlaps(pcs, smp):
    """Samples of two pieces on the same stretch of road (two dips along one road): [(p, k, q, k2)] to hold equal."""
    out = []
    for q in range(len(pcs)):
        for k in range(len(smp[q]["mem"])):
            i, s_ = int(smp[q]["mem"][k]), float(smp[q]["ms"][k])
            for p in _pieces_of(smp, i):
                if p >= q:
                    break
                got = _locate(smp, p, i, s_)
                if got is not None:
                    a, b, t = got
                    out.append((p, a if t < 0.5 else b, q, k))
                    break
    return out


def deck_grade(m):
    """The steepest grade a deck of road m may hold: its class's, and never more than a deck's own law (the approaches
    and embankments keep the class's). With the class's alone a secondary's deck took 0.11, and its line, free over
    the lanes, humped (ends 14.92, middle 17.03 over a motorway whose clearance asked 16.35) and dropped its ends
    away from the embankment it lands on."""
    return min(max_grade(m), DECK_MAX_GRADE)


# (each by its member's law key: _by_law. Wrapped here, after the originals, so each calls the originals itself. The
# law values each reads are in its key: this module's constants (grading sets them from roads.py at its import) and
# gradelaw's GAME_GRADE_PLUS, which road_grade adds; a test setting one anew is made anew)
_GRADELAWS = ("GAME_GRADE_PLUS",)
max_grade = _by_law(max_grade, "max_grade", laws=tuple(_GRADELAWS), gradelaws=_GRADELAWS)
curve_radius = _by_law(curve_radius, "curve_radius",
                       laws=("A_VERT", "A_VERT_RAIL", "R_MIN_ROAD_M", "R_MIN_RAIL_M", "PATHS"), gradelaws=_GRADELAWS)
deck_grade = _by_law(deck_grade, "deck_grade", laws=("DECK_MAX_GRADE",), gradelaws=_GRADELAWS)
_law_span_memo = {}


def deck_depth(span):
    """deck_depth(span) by the span and the deck laws (DECK_SPAN_RATIO, the depths' bounds), made once for each: asked
    39,000 times a region for 200 spans."""
    span = float(span)
    key = (span, DECK_SPAN_RATIO, DECK_MIN_DEPTH_M, DECK_FULL_DEPTH_M)
    got = _law_span_memo.get(key)
    if got is None:
        if len(_law_span_memo) > _LAWS_MAX:
            _law_span_memo.clear()
        got = _law_span_memo[key] = _deck_depth_unmemoized(span)
    return got


def _abutments(t, natural, depth, at_start, at_end):
    """Bool per sample of a deck: in an abutment, the stretch from either end where its natural height stays within
    the girders' depth of the ground (over it by up to depth + DECK_AIR_M, or under by the air): the deck lies on the
    ground there (its fill, its abutment wall), no void under it to keep the girders out of. The floor is for the span
    past it, over what the ground does under the line: where it falls away, and where it rises into it (the hill the
    deck must pass over). A deck 12 m from its end on a slope still level with it (Kempttal's viaduct: the ground 0.0
    and 1.5 m under its line at 12 and 20 m, 3.5 at 24) stood 2.7 m up over a ground it lies on, and a flat-footed
    ramp (Duttweilerbruecke's service ramp, level with its ground all along) 2.5 m up at 24 m: with the deck's line
    stiff that lifted a whole deck 5 m to 8 m over the real one."""
    off = np.asarray(t, np.float64) - np.asarray(natural, np.float64)
    ok = (off > -(depth + DECK_AIR_M)) & (off < DECK_AIR_M)
    out = np.zeros(len(ok), bool)
    if at_start:
        out |= np.logical_and.accumulate(ok)
    if at_end:
        out |= np.logical_and.accumulate(ok[::-1])[::-1]
    return out


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


def _deck_paths(net, sysm):
    """along(i, si, j, sj, limit): m along the system's decks from member i's arc length si to member j's sj, through their
    joints (inf: not joined). Of the way there only what is nearer than limit is looked for (a shortest path over the joints
    from i's side, stopped where it passes limit: a distance further than that is inf, one nearer is the shortest): the
    asker wants to know whether two places are within a clearance's run of each other, not how far apart they are across a
    system of hundreds of decks, which every joint to every joint (all pairs) of it cost."""
    at = {}                 # deck member -> [(s on it, joint)]
    n = 0
    for i, si, j, sj in sysm["joins"]:
        if net.m[i].kind == net.m[j].kind == "deck":
            at.setdefault(i, []).append((si, n))
            at.setdefault(j, []).append((sj, n))
            n += 1
    nbr = [dict() for _ in range(n)]            # (joint -> {the joints on the same decks: the run between them})
    for lst in at.values():
        for sa, a in lst:
            for sb, b in lst:
                if a != b:
                    w = abs(sa - sb)
                    if w < nbr[a].get(b, np.inf):
                        nbr[a][b] = w

    def along(i, si, j, sj, limit=np.inf):
        best = abs(si - sj) if i == j else np.inf
        goal = at.get(j, ())
        if not goal:
            return best
        dist, heap = {}, []
        for sa, a in at.get(i, ()):
            d = abs(si - sa)
            if d < limit and d < dist.get(a, np.inf):
                dist[a] = d
                heapq.heappush(heap, (d, a))
        while heap:
            d, a = heapq.heappop(heap)
            if d > dist[a]:
                continue
            for b, w in nbr[a].items():
                nd = d + w
                if nd < limit and nd < dist.get(b, np.inf):
                    dist[b] = nd
                    heapq.heappush(heap, (nd, b))
        for sb, b in goal:
            if b in dist:
                best = min(best, dist[b] + abs(sb - sj))
        return best
    return along


def ramp(z0, g0, sign, g, rad, d, ground):
    """An embankment (sign +1: its start over the ground, the road falling away from it) or a cut (-1) from height z0,
    leaving at grade g0 (along d), at distances d (from 0, every STEP_M) with ground heights ground: it turns from g0
    to the road's max grade g (falling for an embankment, rising for a cut) over a vertical curve of radius rad, holds
    it until it meets the ground, and turns onto the ground's grade over a curve of the same radius, landing tangent
    to the ground's line. Returns its heights and the index past which it is the ground (len(d) if it never meets
    it)."""
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
    z_t = float(dnp.interp(s_t, d, ground))
    g1 = float(grade[t - 1])
    look = min(n - 1, t + max(2, int(round(abs(g1) * rad / max(ds.mean(), 1e-6))) + 2))
    g2 = float((ground[look] - ground[t - 1]) / max(d[look] - d[t - 1], 1e-6)) if look > t - 1 else g1
    # (the road lands on a line of at most its own max grade: a ground rising off the start faster than that (a deck's
    # end in a trench, the ground a metre over it a few metres on) is read over a few samples as a grade of 0.3 or 0.6,
    # and the curve onto that line, 0.5 rad |g2 - g1| long, carried the ramp 40 m up over level ground: A61's two
    # motorways' ramps floored the road at 235-247 m on ground of 199, each interchange's differently)
    g2 = float(np.clip(g2, -g, g))
    half = 0.5 * rad * abs(g2 - g1)             # (a curve onto the ground: about half its length either side)
    line = z_t + g2 * (d - s_t)                 # (the ground's line the ramp lands tangent to)
    # (that curve runs from where the ramp itself is, at its grade, to the ground's line, at its grade, over the
    # shortest length the radius allows — the latest start along the ramp that still lands on the ground. Centred on
    # the touch point instead it assumed the ramp held the touch's grade from far back: a ramp still turning when
    # it comes at its ground — a short one, from a junction, on a road little under it — met the curve 0.2 m under
    # the ramp it joined, a kink in the embankment the plan then took on. A landing needs the ramp's grade closing
    # on the line's at the start it leaves; where it never does — a ground climbing to the shape's end as fast as
    # the ramp leaves it, its touch on the line at once — there is no landing to make: laid anyway the curve ran on
    # at the leaving grade, past the ground it was to meet, an embankment climbing off its hill. The curve stays
    # centred on the touch point there, as it was)
    ks = np.arange(1, min(t, n - 1) + 1)
    off_ = sign * (z[ks] - line[ks])
    clos = sign * (g2 - grade[ks - 1])
    ok = (off_ > 0.0) & (clos > 0.0) & (2.0 * off_ >= rad * clos * clos)
    ka = int(ks[ok][-1]) if ok.any() else max(int(np.searchsorted(d, s_t - half)), 1)
    if float(clos[ks == ka][0]) <= 0.0:
        s1 = s_t - half
        k1 = int(np.searchsorted(d, s1))
        k2 = int(min(n, np.searchsorted(d, s_t + half) + 1))
        z1 = z_t - g1 * half
        out = z.copy()
        for k in range(max(k1, 1), k2):
            x = d[k] - s1
            out[k] = z1 + g1 * x + (g2 - g1) / (2.0 * max(2 * half, 1e-6)) * x * x
        # (past the curve the plan's own ground grading takes over)
        return out, max(k2, t + 1)
    ga = float(grade[ka - 1])
    L = max(2.0 * float(off_[ks == ka][0]) / float(clos[ks == ka][0]), rad * abs(g2 - ga))
    k2 = int(min(n, np.searchsorted(d, d[ka] + L) + 1))
    out = z.copy()
    for k in range(ka, k2):
        x = d[k] - d[ka]
        out[k] = z[ka] + ga * x + (g2 - ga) / (2.0 * L) * x * x
    # (past the curve the plan's own ground grading takes over)
    return out, max(k2, t + 1)


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


