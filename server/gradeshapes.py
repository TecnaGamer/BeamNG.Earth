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
import hashlib
import heapq
import math
import os
import threading
import time
from collections import OrderedDict, namedtuple

import numpy as np

import dmath

import dnp
import findground
import gradelaw
import railways

STEP_M = 4.0                # m between a deck's or a dip's samples
CUT_MIN_M = 0.05            # m: a join's arc this near a sample is that sample
SEGMENT_STEP_ON = os.environ.get("BNE_SEGMENT_STEP", "1").strip().lower() not in ("0", "off", "no", "false")
JOIN_M = 2.5                # m: two parts' ends (or an end and another's line) this close meet there
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
CONNECT_M = 400.0           # m: a road on the ground this short between two structures' ends is named by the audits (it joins
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
SOFT_M = 0.6                # m: the rounding where an embankment meets the ground (a soft maximum)
OFF_GROUND_M = 0.1          # m: a road this far over (under) its ground is off it: an embankment (a cut); less, it is on it

CLEAR_PATH_M = 3.0          # m: a path's headroom (no vehicle on it)

W_END = 1.0                 # a system end's embankment (per m, squared)
W_HOLD = 100.0              # a short deck chain (a road dug under it) at its ground
W_DIP = 0.5                 # a road's dip under its crossing
RAILS = ("rail", "rail_deck")
# A track held at the bed the railways lay (its distance from it, per sample, squared). What moving a way costs is the earth,
# and findground's law has it as the run of its ramp, lack / grade (findground.dip_run): a way that may climb only at a
# track's grade runs longer for the same move than any road, by the ratio of their grades. The cheapest way to move is the
# steepest road the laws allow (gradelaw: a link in a village, 17 %), a track's grade is railways.GRADE: so a track costs
# that ratio times a road's dip to move, the least it can cost, and it moves only where nothing cheaper closes the conflict.
W_TRACK = W_DIP * gradelaw.road_grade("service", 20) / railways.GRADE
W_SMOOTH = 0.2
W_STIFF = 3.0e3             # a deck's line (per sample, squared: its bends), see _solve_shape_qp
W_JOIN = 300.0

Member = namedtuple("Member", "key line kind cls kmh level half limit nodes held", defaults=(None, False))
# key: hashable, unique, the same from every cell; line (k, 2) frame ENU; kind: "ground", "deck", "tunnel", "rail",
# "rail_deck"; cls: road class ("rail" for track); kmh: speed limit or None; level: the map's level (OSM's layer) of a
# deck or a rail deck, 0 on the ground (_over: the order of two crossing members of one kind); half: half
# width (m); limit: the road's signed height limit (m), a callable q (x, y) -> m or None (its signs, read where it
# crosses: grading's roads.road_sign_at, the dips' own) or None; nodes: its road's map nodes (a frozenset of connector
# ids) or None (not known: a track); held: a deck never raised (grading's UNDER_TUNNEL_FIXED: the map has the road
# under it in a short tunnel), its chain at its ground and the roads under it dug, whatever its length

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


def deck_depth(span):
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


GRID_MIN_M, GRID_MAX_M = 32.0, 256.0    # m: the bounds of the index's cell (twice the members' median box side between)
GRID_LONG_CELLS = 64                    # a member whose box spans more cells than this is kept in a list every query reads
NEAR_CHUNK_M = 64.0                     # m of a line near_along asks the grid about at a time


class Net:
    """The members' geometry, made once: lines, arc lengths, boxes, and (built on the first near()) a grid over the
    boxes."""

    def __init__(self, members):
        self.m = members
        self.lines = [np.asarray(m.line, np.float64)[:, :2] for m in members]
        self.s = [arclen(l_) for l_ in self.lines]
        self.L = [float(s[-1]) for s in self.s]
        self.box = np.array([np.r_[l_.min(0), l_.max(0)] for l_ in self.lines]) if members else np.zeros((0, 4))
        self._grid = None

    def _index(self):
        """(cell side, {(ix, iy): [member ...]}, [long members]): every member in the grid cells its box covers (a box
        covering more than GRID_LONG_CELLS cells, a long road, in the list instead: it is checked on every query)."""
        bx = self.box
        side = float(np.clip(2.0 * np.median(np.maximum(bx[:, 2] - bx[:, 0], bx[:, 3] - bx[:, 1])), GRID_MIN_M,
                             GRID_MAX_M)) if len(bx) else GRID_MIN_M
        cells, long_ = {}, []
        c0 = np.floor(bx[:, :2] / side).astype(np.int64)
        c1 = np.floor(bx[:, 2:] / side).astype(np.int64)
        for i in range(len(bx)):
            (x0, y0), (x1, y1) = c0[i].tolist(), c1[i].tolist()
            if (x1 - x0 + 1) * (y1 - y0 + 1) > GRID_LONG_CELLS:
                long_.append(i)
                continue
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    cells.setdefault((x, y), []).append(i)
        self._grid = (side, cells, long_)
        return self._grid

    def _near_box(self, lo, hi, pad):
        """The members (ascending, an array) whose boxes come within pad of the box lo..hi. The grid only narrows what
        the boxes are compared on (a member sharing no cell with the box grown by pad is not within it); the comparison
        is the whole answer."""
        bx = self.box
        side, cells, long_ = self._grid or self._index()
        x0, y0 = (int(v) for v in np.floor((lo - pad) / side))
        x1, y1 = (int(v) for v in np.floor((hi + pad) / side))
        if (x1 - x0 + 1) * (y1 - y0 + 1) > len(bx):         # (a query over more cells than members: all of them)
            cand = np.arange(len(bx))
        else:
            got = set(long_)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    got.update(cells.get((x, y), ()))
            cand = np.array(sorted(got), np.int64)
        c = bx[cand]
        hit = (c[:, 0] <= hi[0] + pad) & (c[:, 2] >= lo[0] - pad) & (c[:, 1] <= hi[1] + pad) & (c[:, 3] >= lo[1] - pad)
        return cand[hit]

    def near(self, i, pad):
        """The members other than i whose boxes come within pad of i's, in index order."""
        b = self.box[i]
        return [int(j) for j in self._near_box(b[:2], b[2:], pad) if j != i]

    def near_along(self, i, pad):
        """The members other than i with a part of their box within pad of a part of i's line, in index order: the line cut
        every NEAR_CHUNK_M m, the members whose boxes come within pad of a piece's own box. A subset of near(i, pad) with
        every member that has a segment within pad of a segment of i: a long road's box holds a city, its line only what
        it passes."""
        line, s = self.lines[i], self.s[i]
        k = np.floor(s / NEAR_CHUNK_M).astype(np.int64)
        starts = np.r_[0, np.flatnonzero(np.diff(k)) + 1]
        ends = np.r_[starts[1:], len(line) - 1]
        got = np.unique(np.concatenate([self._near_box(line[a:e + 1].min(0), line[a:e + 1].max(0), pad)
                                        for a, e in zip(starts.tolist(), ends.tolist())]))
        return [int(j) for j in got if j != i]


def _is_seed(net, i):
    m = net.m[i]
    if m.kind == "deck":
        return True
    if m.kind == "tunnel":
        return net.L[i] <= TUNNEL_MAX_M
    return False            # (a rail deck: the railways', held at its bed; in a system only as what a seed crosses)


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


def _pair(net, i, j):
    """Member i's relations with member j, from a cache by the two members' keys and lines."""
    key = (_line_id(net, i), _line_id(net, j))
    got = _PAIR_REL.get(key)
    if got is None:
        got = _pair_relations(net, i, j)
        if len(_PAIR_REL) >= _PAIR_REL_MAX:
            _PAIR_REL.clear()
        _PAIR_REL[key] = got
    return got


def _relations(net, i):
    """Member i's joints and crossings with the members near it: [(j, "join"|"cross", s on i, s on j, q)]. Each
    pair's from a cache by the two members' keys and lines (a region grown cell by cell meets the same pairs again)."""
    out = []
    for j in net.near_along(i, JOIN_M + 0.5):
        if len(net.lines[j]) < 2:
            continue
        out.extend((j,) + r_ for r_ in _pair(net, i, j))
    return out


class Scan:
    """The relations of the structures with the members near them, kept across the rounds of one growth (find_interchanges
    scan=): a round scans only the members it adds, each against its neighbours through the net's grid, and the relations
    of the members of the rounds before are the ones kept. {line id: {line id: relations}} for the structures' side (a
    member's relations with a neighbour: _pair), and `scanned`: the members each round scanned (the count of them)."""

    def __init__(self):
        self.pairs = {}
        self.scanned = []

    def add(self, net):
        """This round's net: every member not seen before scanned against its neighbours."""
        ids = [_line_id(net, i) for i in range(len(net.m))]
        new = [i for i, k in enumerate(ids) if k not in self.pairs and len(net.lines[i]) >= 2]
        for i in new:
            self.pairs[ids[i]] = {}
        for i in new:
            seed_i = _is_seed(net, i)
            for j in net.near_along(i, JOIN_M + 0.5):
                if len(net.lines[j]) < 2:
                    continue
                if seed_i:
                    self.pairs[ids[i]][ids[j]] = _pair(net, i, j)
                if _is_seed(net, j):
                    self.pairs.setdefault(ids[j], {})[ids[i]] = _pair(net, j, i)
        self.scanned.append(len(new))
        self.ids = ids
        self.at = {k: j for j, k in enumerate(ids)}

    def relations(self, net, i):
        """_relations(net, i) from what the rounds kept: in index order of the neighbours, the same list."""
        got = sorted((self.at[k], rels) for k, rels in self.pairs.get(self.ids[i], {}).items() if k in self.at)
        return [(j,) + r_ for j, rels in got for r_ in rels]


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
    there and 8.35 m over it on the level-3 deck's start: the loop infeasible. A road's own lane sections meeting end
    to end are a road going on, whatever levels the map gave them and whether or not their node sets share a node
    (one carriageway's sections were tagged level 1 and 2 in Shanghai's Yan'an elevated road): taken for a
    separation, their clearance contradicted their joint (12.7 m round a loop of four), an infeasible system left
    as it was. crossed: e and o cross each other (a
    road cannot meet the one it passes over or under a few metres on: Gravelly Hill's level-1 M6 deck under its
    level-3 one at 29 deg, its end 5 m past their crossing)."""
    me, mo = net.m[e], net.m[o]
    if mo.kind not in STRUCTURES or _over(me, mo) is None:
        return True
    mapped = me.nodes is None or mo.nodes is None or bool(me.nodes & mo.nodes)    # (no nodes known: as they lie)
    if min(so, net.L[o] - so) <= JOIN_M:
        return me.kind not in STRUCTURES or mapped or _road_of(me) == _road_of(mo)
    if me.kind not in STRUCTURES:
        return mapped
    if me.kind == mo.kind == "deck" and not crossed and _road_of(me) != _road_of(mo) and mapped:
        c = abs(float(dnp.dot(_tangent(net.lines[e], net.s[e], se), _tangent(net.lines[o], net.s[o], so))))
        return c >= dmath.cos(math.radians(MERGE_DEG))
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
    up = _over(net.m[i], net.m[j])
    for si, sj, q in hits:
        if any(r_[0] == j and r_[1] == "cross" and abs(r_[2] - si) < 1.0 for r_ in out):
            continue                        # (on a vertex: found on both its segments)
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


def find_systems(members, reader=None, trace=None):
    """The interchanges among members: [System], each closed, in a deterministic order. See find_interchanges."""
    net, found = find_interchanges(members, reader, trace)
    return net, [p.sysm for x in found for p in (x.parts or (x,))]


def find_interchanges(members, reader=None, trace=None, want=None, skip=None, scan=None, reach=True):
    """(net, [Interchange]): the interchanges among members, each closed, in a deterministic order, each with the seconds
    its relations and its closing took (a merge's or a dip's seconds go to the interchange they ended up in). An interchange
    is the systems whose ramps meet (_ramp_reaches; Interchange.parts), each system the structures that join, cross or dig
    one road among themselves. want(net,
    seeds) -> bool: whether an interchange is asked for; one that is not is found (it may merge others) but not closed.
    skip(net, i) -> bool: member i is a piece of an interchange already solved: it is not a seed here, nor scanned for
    relations (it stays a member: what others cross). scan (a Scan): the relations are those kept by the rounds before (this round's net scanned into it: only the members
    it adds are scanned). reach: True, the ramps' reaches are computed (_ramp_reaches: the interchange's one growth, whose
    result is stored with its extent); False, none: the systems alone, as a region's find needs them; a list of sets of
    member keys (str), the reaches as that growth stored them: the seeds of a set are one interchange. reader(net) ->
    (ground, fixed): the
    elevation the cost of a move is read from (_Verdicts); None, flat ground. trace (a dict, the audit's: interchange_audit):
    filled with what merged the seeds, [(a, b, why, detail)] in the order they were united (why "relation": a join or a
    crossing of two seeds, detail (kind, s on a, s on b); "dip": two seeds over one road they dig, detail None), with the
    seeds; nothing else about the answer. "ramp": two seeds whose ramps meet on a road on the ground (detail the road): they
    stay systems of their own, in one interchange."""
    net = Net(members)
    n = len(members)
    if trace is not None:
        trace["seeds"], trace["merges"] = [], []
    cand = [i for i in range(n) if _is_seed(net, i) and len(net.lines[i]) >= 2 and not (skip is not None and skip(net, i))]
    t_rel = {}
    rel = {}
    if scan is not None:
        t0 = time.perf_counter()
        scan.add(net)
        t_scan = time.perf_counter() - t0
    import xproc
    for k_, i in enumerate(cand):
        if not k_ % 32:
            xproc.check_cancel()        # (the growth's step time limit, ix/limits.py, and a place a teleport left)
        t0 = time.perf_counter()
        rel[i] = [r_ for r_ in (_relations(net, i) if scan is None else scan.relations(net, i))
                  if not _tunnel_network(net, r_[0])]
        t_rel[i] = time.perf_counter() - t0 + (t_scan / len(cand) if scan is not None else 0.0)
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
    seed_set = set(seeds)
    for i in seeds:
        rel[i] = [r_ for r_ in rel[i] if r_[0] in seed_set or not _is_seed(net, r_[0])]
    if trace is not None:
        trace["seeds"] = list(seeds)
    par = {i: i for i in seeds}

    def root(i):
        while par[i] != i:
            par[i] = par[par[i]]
            i = par[i]
        return i

    def unite(a, b, why=None, detail=None):
        if trace is not None:
            trace["merges"].append((a, b, why, detail))
        ra, rb = root(a), root(b)
        if ra != rb:
            par[max(ra, rb, key=lambda x: str(members[x].key))] = min(ra, rb, key=lambda x: str(members[x].key))
    # (structures meet one another: a joint or a crossing. A road leaving a structure belongs to the interchange as far as
    # its ramp reaches (_ramp_reaches, below): an interchange ends where that road is back on its ground, which is not
    # where the structure's end is. A road on the ground beyond the reach is a boundary held at its own ground, not
    # followed on to the next structure)
    for i in seeds:
        for j, kind, si, sj, q in rel[i]:
            if j in par:
                unite(i, j, "relation", (kind, si, sj))
    dig = _Verdicts(net, rel, droot, reader)
    t_dip = {}
    for a, b in _shared_dips(net, seeds, rel, dig, t_dip):
        unite(a, b, "dip")
    groups = {}
    for i in seeds:
        groups.setdefault(root(i), []).append(i)
    # (the systems are those the structures make among themselves; an interchange is the systems whose ramps meet. Each
    # system is solved as it is, by the one that owns the interchange, and the interchange's embankments are laid at once:
    # where two ramps meet on a road one lays what the other does, and the pairing of ramps toward each other is theirs)
    cpar = {r: r for r in groups}

    def croot(r):
        while cpar[r] != r:
            cpar[r] = cpar[cpar[r]]
            r = cpar[r]
        return r
    if reach is True:
        reaches = _ramp_reaches(net, seeds, rel, dig, t_dip)
    elif not reach:
        reaches = []
    else:
        reaches = []
        for grp in reach:
            ss = sorted((i for i in seeds if str(members[i].key) in grp), key=lambda x: str(members[x].key))
            reaches += [(ss[0], i, -1) for i in ss[1:]]
    for a, b, j in reaches:
        if trace is not None:
            trace["merges"].append((a, b, "ramp", j))
        ra, rb = croot(root(a)), croot(root(b))
        if ra != rb:
            cpar[max(ra, rb, key=lambda x: str(members[x].key))] = min(ra, rb, key=lambda x: str(members[x].key))
    cgroups = {}
    for r in groups:
        cgroups.setdefault(croot(r), []).append(r)
    found = []
    for c in sorted(cgroups, key=lambda x: str(members[x].key)):
        seeds_c = sorted((i for r in cgroups[c] for i in groups[r]), key=lambda x: str(members[x].key))
        if want is not None and not want(net, seeds_c):
            continue
        parts = []
        for r in sorted(cgroups[c], key=lambda x: str(members[x].key)):
            t0 = time.perf_counter()
            seeds_r = sorted(groups[r], key=lambda x: str(members[x].key))
            parts.append(Interchange(_system(net, seeds_r, rel, dig), sum(t_rel[i] for i in seeds_r),
                                     time.perf_counter() - t0 + sum(t_dip.get(i, 0.0) for i in seeds_r)))
        if len(parts) == 1:
            found.append(parts[0])
            continue
        whole = dict(seeds=seeds_c, dips=[d for p in parts for d in p.sysm["dips"]],
                     crossings=[x for p in parts for x in p.sysm["crossings"]],
                     ends=[e for p in parts for e in p.sysm["ends"]])
        found.append(Interchange(whole, sum(p.paths_s for p in parts), sum(p.extent_s for p in parts), tuple(parts)))
    return net, found


def _ramp_reaches(net, seeds, rel, dig, clock=None):
    """[(seed, seed, road)] of the structures whose ramps meet on a road on the ground. An interchange ends where its road
    is back on its ground, which is the end of the ramp, not the end of the structure: from each end of a deck that rises
    for a road under it, onto a road on the ground, the ramps it may leave by, laid as the embankment step lays them
    (_Ramper: the road's grade and curve laws, through the junctions they meet) over the stretches of the roads they run
    along: an embankment from the height the structure needs (clearance_need: the headroom and its depth) falling away at
    the deck's grade, and a cut from the ground at that grade, the end standing anywhere between. What the solve picks is
    not known before it is made, so both are followed, as far as either goes (and no further than REACH_MAX_M from
    the end they left, the junctions' chains included). A structure that digs the road under it, or
    that rises for nothing, has no ramp. Two structures whose ramps run along one stretch of a road are one interchange
    (the road is not on its ground between them, and the ramps meeting there are laid together). A deck every end of
    which is further than that from every other rising deck's is not laid at all: its spans can overlap no other deck's
    (each deck's laying is its own — the claims stop no ramp of another deck's), so it pairs nothing.
    clock: {seed: seconds},
    to each seed the seconds spent for it added."""
    members = net.m
    ground = dig.read()[0]

    def ground_of(x):
        return np.zeros(len(x["mem"])) if ground is None else np.asarray(ground(x["xy"]), np.float64)

    rise = {}               # (deck, 0 its start | 1 its end) -> how far over its ground the structure's end stands
    for i in seeds:
        mi = members[i]
        if mi.kind != "deck" or dig.digs(i):
            continue
        for j, kind, si, sj, q in rel[i]:
            if kind != "cross" or _over(mi, members[j]) != "i":
                continue
            if dig.lift(i, si, j, sj, q) <= 0.0:
                continue        # (a deck that does not rise for the road under it stands on its ground)
            ends = dig.run(i, si)
            for dist, end in zip(ends[0], ends[1]):
                if end is not None and math.isfinite(dist):
                    rise[end] = max(rise.get(end, 0.0), clearance_need(net, i, j, q))
    # (the ends a reach is laid from, and the decks that can meet: a deck's every ramp stays within REACH_MAX_M of
    # the end it left, so two decks whose ends are all further apart than both reaches never overlap a span, and —
    # the laying is each deck's own, its claims stop no other deck's ramp — skipping one changes no pair. A deck
    # alone in its reach is the common case: a bridge over a valley with a town's grid under it)
    ends_at = {}
    for i, e in rise:
        ends_at.setdefault(i, []).append(net.lines[i][-1 if e else 0])
    with_another = {i for i, ps in ends_at.items()
                    if any(k != i
                           and min(float(np.linalg.norm(p - q)) for q in qs) <= 2.0 * REACH_MAX_M + 2.0 * JOIN_M + STEP_M
                           for k, qs in ends_at.items() for p in ps)}
    pieced = {int(i): [(0.0, float(net.L[i]))] for i in seeds}
    spans = {}
    for i in seeds:
        mi = members[i]
        if mi.kind != "deck" or i not in with_another:
            continue
        t0 = time.perf_counter()
        lay = _Ramper(net, ground_of, pieced)
        for j, kind, si, sj, q in rel[i]:
            mj = members[j]
            if kind != "join" or mj.kind != "ground" or min(si, net.L[i] - si) > JOIN_M:
                continue
            over = rise.get((i, int(si > net.L[i] / 2)))
            if over is None:
                continue
            if min(sj, net.L[j] - sj) > JOIN_M and any(
                    k2 == "join" and j2 != i and members[j2].kind in ("deck", "rail_deck") and abs(s2 - si) < 1.0
                    for j2, k2, s2, _t, _q in rel[i]):
                continue        # (the bridge goes on past this end over the road's middle: no end onto it)
            for sign_, over_ in ((1.0, over), (-1.0, 0.0)):
                lay.ramp_from(j, sj, sj < net.L[j] / 2, None, -deck_grade(mi), over=over_, sign=sign_,
                              left=REACH_MAX_M)
        n0, n = len(lay.ramps), 0
        while n < len(lay.ramps):
            rp = lay.ramps[n]
            if n >= n0:
                lay.cover(rp[4], rp[9])
            for jj, s_, ways, z0, surface, at_d in _junction_roads(net, rp, lay.done, pieced):
                lay.done.setdefault(jj, []).append((s_ - JOIN_M, s_ + JOIN_M))
                left = None if rp[11] is None else rp[11] - at_d - STEP_M
                if left is not None and left <= 0.0:
                    continue        # (the reach's budget spent: no ramp of this deck runs further from its end)
                for toward_end in ways:
                    tb = _tangent(net.lines[jj], net.s[jj], s_) * (1.0 if toward_end else -1.0)
                    lay.ramp_from(jj, s_, toward_end, z0, float(surface[0] * tb[0] + surface[1] * tb[1]), branch=True,
                                  left=left)
            n += 1
        for rp in lay.ramps:
            for m_, (lo, hi) in _stretches(rp[4], rp[9]).items():
                spans.setdefault(m_, []).append((lo, hi, i))
        if clock is not None:
            clock[i] = clock.get(i, 0.0) + time.perf_counter() - t0
    out = []
    for m_, lst in spans.items():
        lst.sort()
        hi, who = lst[0][1], lst[0][2]
        for lo_, b_, i in lst[1:]:
            if lo_ <= hi + JOIN_M:
                if i != who:
                    out.append((min(i, who), max(i, who), m_))
                if b_ > hi:
                    hi, who = b_, i
            else:
                hi, who = b_, i
    return out


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


class _Verdicts:
    """Who moves at each crossing of a deck over a road on the ground: findground's cost law (findground.leaves), asked
    with what only the elevation tells, the lack: how far the line between the ends of the deck's whole chain falls
    short of the clearance over the road's ground there (the elevation's own evidence under the span: a deck over a
    valley or a cut that already clears has none, and nothing moves). reader(net) -> (ground(xy (k, 2)) -> z (k,), fixed(i,
    s) -> z): the elevation as the solve reads it (None: flat ground, every clearance lacking in full).

    A deck that already goes up over a road at the clearance's whole lack needs no reading: more lack only moves the
    verdict toward the road going down (more to fill against the same rule), so the whole clearance as the lack is asked
    first (the bridge going up then, it goes up), and the elevation read only for a deck that might leave the ground
    under it. A chain whose roads under it all go down is held at its natural height (a deck is one line); one with a road it
    rises for rises, and a road beside that goes down dips what the rise does not carry; a map's short tunnel under a
    deck (Member.held: the findground tunnel rule) holds the chain at its ground whatever."""

    def __init__(self, net, rel, droot, reader):
        self.net, self.rel, self.droot, self.reader = net, rel, droot, reader
        self.memo, self.chain_memo, self.lacks = {}, {}, {}
        self._read = None
        self._links = None

    def read(self):
        if self._read is None:
            self._read = self.reader(self.net) if self.reader else (None, None)
        return self._read

    def links(self):
        """({(deck, 0 its start | 1 its end): [(deck, end)]} the decks' ends joined end to end, {(deck, end)} the ends a
        road on the ground leaves from: where the structure leaves the ground. A deck's end on another's middle (a merge,
        a ramp onto a viaduct) joins nothing here: the structure's ends are not looked for through it. A deck's end
        on a tunnel's end is where the structure goes under the ground, as on a road: the joint is the same relation
        (_end_meets: JOIN_M, and between structures a node of the map they share), the tunnel's end the same end-to-end
        test as a deck's. Left out, La Defense's deck at a tunnel mouth (a 67-piece system) had no end to find, the
        whole chain's lack was asked of it, 4.7 m of rise in 20 m at the mouth (z 50.5), and the system was
        infeasible."""
        if self._links is None:
            net, out, leave = self.net, {}, set()
            for i, rs in self.rel.items():
                if net.m[i].kind != "deck":
                    continue
                for j, kind, si, sj, _q in rs:
                    if kind != "join" or j == i or min(si, net.L[i] - si) > JOIN_M:
                        continue
                    e = int(si > net.L[i] / 2)
                    if net.m[j].kind == "ground" or (net.m[j].kind == "tunnel" and min(sj, net.L[j] - sj) <= JOIN_M):
                        leave.add((i, e))
                    elif net.m[j].kind == "deck" and min(sj, net.L[j] - sj) <= JOIN_M:
                        out.setdefault((i, e), []).append((j, int(sj > net.L[j] / 2)))
            self._links = (out, leave)
        return self._links

    def run(self, u, su):
        """(m from the crossing along the structure to the nearest end each way (its start side, its end side; inf: none
        that way, a dead end or a loop), the end each way as (deck, end) or None, whether the way there passed a joint
        of three decks or more): an end is where a road on the ground leaves the structure, no deck going on there."""
        net = self.net
        links, leave = self.links()
        out = []
        for e in (0, 1):
            heap, seen, got = [(float(su) if e == 0 else float(net.L[u] - su), u, e, False)], set(), (math.inf, None, False)
            while heap:
                d, m, en, br = heapq.heappop(heap)
                if (m, en) in seen:
                    continue
                seen.add((m, en))
                nxt = links.get((m, en))
                if not nxt:
                    if (m, en) in leave:
                        got = (d, (m, en), br)
                        break
                    continue                # (a dead end: nothing leaves the ground there)
                for j, f in nxt:
                    if (j, 1 - f) not in seen:
                        heapq.heappush(heap, (d + net.L[j], j, 1 - f, br or len(nxt) > 1))
            out.append(got)
        return (out[0][0], out[1][0]), (out[0][1], out[1][1]), out[0][2] or out[1][2]

    def free(self, u):
        """((m from deck u's start to the nearest end of its structure that way, that end (deck, 0 | 1)), (the same from
        its end)): findground's rule 1, the structure's ends. Where the decks joined end to end meet three at a joint (a
        viaduct and its ramps) the nearest end each way: the ramp's end on the ground is where the structure leaves it. A
        deck with no end that way (a loop), or whose way to it passes a joint of three decks (a fork, whose branches
        are no one line: measured at Gotthard's motorway links, a chord from the branch's far end lifted the deck 0.5 m
        off the real one), is its own: its own ends."""
        got = self.chain_memo.get(("free", u))
        if got is None:
            (d0, _), (f0, _), b0 = self.run(u, 0.0)
            (_, d1), (_, f1), b1 = self.run(u, float(self.net.L[u]))
            got = ((d0, f0), (d1, f1))
            if not (math.isfinite(d0) and math.isfinite(d1)) or f0 == f1 or b0 or b1:
                got = ((0.0, (u, 0)), (0.0, (u, 1)))
            self.chain_memo[("free", u)] = got
        return got

    def natural(self, u, su, ground):
        """The deck's natural height at su on u (findground, rule 1: the line between the ground at the two ends of its
        structure, over its length)."""
        net = self.net
        (da, fa), (db, fb) = self.free(u)
        pa, pb = (net.lines[m][0 if e == 0 else -1] for m, e in (fa, fb))
        za, zb = ground(np.array([pa, pb], np.float64))
        return float(za + (zb - za) * (su + da) / max(net.L[u] + da + db, 1e-9))

    def crossing(self, u, su, d, sd, q):
        """"down", "up" or "none" (nothing lacks: the elevation's own evidence already clears it): the road d on the
        ground at sd under deck u at su."""
        key = (u, round(float(su), 3), d, round(float(sd), 3))
        got = self.memo.get(key)
        if got is None:
            net = self.net
            mu, md = net.m[u], net.m[d]
            need = clearance_need(net, u, d, q)
            lower = findground.TUNNEL if mu.held and md.kind == "ground" else WAY[md.kind]
            ends = self.run(u, su)[0]
            args = (ends, deck_grade(mu), max_grade(md))
            who, lack = findground.leaves(lower, findground.BRIDGE, need, *args), need
            if who == "lower" and lower != findground.TUNNEL and self.reader:
                ground, fixed = self.read()
                zd = fixed(d, sd)
                if zd is not None and np.isfinite(zd):
                    lack = need - (self.natural(u, su, ground) - float(zd))
                    who = findground.leaves(lower, findground.BRIDGE, lack, *args)
            got = self.memo[key] = "down" if who == "lower" else "up" if lack > 0.0 else "none"
            self.lacks[key] = lack if got == "up" else 0.0
        return got

    def lift(self, u, su, d, sd, q):
        """How far deck u at su must rise over its natural height to clear road d at sd (crossing): the lack the verdict
        was read on; 0 where it does not rise."""
        if self.crossing(u, su, d, sd, q) != "up":
            return 0.0
        return self.lacks[(u, round(float(su), 3), d, round(float(sd), 3))]

    def digs(self, i):
        """Whether deck i's chain is held at its natural height with the roads under it dug: where the map has a road under
        it in a short tunnel (Member.held), or where its roads under it all say the road goes down (none says the deck
        rises: a deck is one line, and one that rises for a road clears the roads beside it as far as it goes)."""
        key = self.droot(i)
        got = self.chain_memo.get(key)
        if got is None:
            states, held = [], False
            for m, rs in self.rel.items():
                if self.net.m[m].kind != "deck" or self.droot(m) != key:
                    continue
                held = held or bool(self.net.m[m].held)
                for j, kind, si, sj, q in rs:
                    if kind == "cross" and self.net.m[j].kind == "ground" and _over(self.net.m[m], self.net.m[j]) == "i":
                        states.append(self.crossing(m, si, j, sj, q))
            got = self.chain_memo[key] = held or ("down" in states and "up" not in states)
        return got

    def moves(self, u, su, d, sd, q):
        """"down" or "up": who moves at this crossing: the road under a chain held at its ground, or the road findground
        says goes down (under a deck that rises for the others: it dips what that rise does not carry)."""
        return "down" if self.digs(u) or self.crossing(u, su, d, sd, q) == "down" else "up"


def _shared_dips(net, seeds, rel, dig, clock=None):
    """Pairs of seeds over one road on the ground within its dip under one of them: [(a, b)]. A road dug under one
    structure (a short rail deck, a short deck) is that low under the other too: two systems solved apart had it at
    its ground under one and dug under the other. Perth's Tonkin Highway: a trunk road dug 7 m under the railway in the
    median and, 6 m on, a motorway deck lifted 7.3 m over its ground, its carriageway and the track's fill raised into
    the trains. The road is followed through its joints, as the dip's piece is. clock: {seed: seconds}, to each seed
    the seconds of the pairs looked at for it added."""
    members = net.m
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
            t0 = time.perf_counter()
            mu = members[u]
            if not (mu.kind == "deck" and dig.moves(u, su, d, sd, qd) == "down"):
                if clock is not None:
                    clock[u] = clock.get(u, 0.0) + time.perf_counter() - t0
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
                    digs = mi.kind == "deck" and dig.moves(i, si, j, sj, qi) == "down"
                    if along <= w + (_dip_half(net, i, si, j, sj, qi) if digs else 0.0):
                        out.append((u, i))
            if clock is not None:
                clock[u] = clock.get(u, 0.0) + time.perf_counter() - t0
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


def system_key(net, sysm):
    """A system's identity: its parts' keys (the same from whichever cell found it)."""
    h = hashlib.sha1()
    for i in sorted(set(sysm["seeds"]) | {x[0] for x in sysm["dips"]}, key=lambda i: str(net.m[i].key)):
        h.update(repr(net.m[i].key).encode())
    return h.hexdigest()[:20]


KEY_DEG = 1.0e-4            # deg (about 11 m): the grid an interchange's centre is placed on in its key


def canonical_id(m):
    """A member's id the same from every region and every planning frame: its map feature's id and its kind. The keys
    members carry hold the middle of the line in the planner's own frame (a deck's, a tunnel's) or its ends' (a track's),
    different by metres from one frame to the next; a road's several parts of one kind share this id, which is all an
    interchange's identity needs (which roads and tracks it is made of)."""
    k = m.key
    if isinstance(k, tuple) and k and k[0] == "rail" and len(k) > 2:
        return "%s:%s" % (m.kind, k[2])
    return "%s:%s" % (m.kind, k[0] if isinstance(k, tuple) else k)


def system_ids(net, sysm):
    """The canonical ids (canonical_id) of a system's seeds and of the roads it digs under its crossings, as a set: what
    makes it the interchange it is, found from whichever region and cut however its box cut it."""
    return {canonical_id(net.m[i]) for i in sysm["seeds"]} | {canonical_id(net.m[d]) for d, _a, _b in sysm["dips"]}


def interchange_key(ids, centre, version=""):
    """An interchange's key: the hash of its sorted member ids (canonical_id), of where its centre is (lat, lon, to
    KEY_DEG: a road's parts over two places are two interchanges of the same roads, and its centre, found from the whole
    system whatever region found it, is the same to far less than that) and of the version of what a solve of it is made
    from (the plans' format: a solve made by other rules is another interchange's)."""
    h = hashlib.sha1(str(version).encode("utf-8"))
    h.update(b"\0%d,%d" % (round(centre[0] / KEY_DEG), round(centre[1] / KEY_DEG)))
    for i in sorted(ids):
        h.update(b"\0" + i.encode("utf-8"))
    return h.hexdigest()


def system_centre(net, sysm):
    """The middle of a system's seeds' bounding box (frame ENU): the point whose map cell owns the interchange."""
    pts = np.concatenate([net.lines[i] for i in sysm["seeds"]])
    return (pts.min(0) + pts.max(0)) / 2.0


def system_size(net, sysm):
    """(pieces, crossings) of a system: its decks and the roads dug under it, and its crossings."""
    return (sum(1 for i in sysm["seeds"] if net.m[i].kind == "deck") + len(sysm["dips"]) + len(sysm.get("tracks", ())),
            len(sysm["crossings"]))


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


def _var(pcs, smp, i, s_):
    """(piece, sample a, sample b, weight of b) of member i's unknown height at arc length s_, or None."""
    for p in _pieces_of(smp, i):
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
    for p in _pieces_of(smp, i):
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


def _law(net, pc):
    m = net.m[pc["main"]]
    return (deck_grade(m) if pc["kind"] == "deck" else max_grade(m)), curve_radius(m)


def solve_shape(net, sysm, pcs, smp, ground, fixed_z, diagnostics=None):
    """The shape: per piece its heights. The laws (grade, vertical curve by speed, clearance, joints) are hard
    constraints of one quadratic programme (the Rust kernel's, carriageways.solve); the costs: the embankment at the
    shape's ends on the ground, a deck's line kept (its stiffness), the depth a road goes under to, a short deck held at
    its ground. None where the
    kernel finds no answer (laws that cannot all hold): no stand-in, the system is left as it is and the caller says
    so (the conflict is the thing to fix). fixed_z(i, s): member i's height where the shape does not move it, at arc
    length s (None: not known), or at an array of them (an array, NaN: not known).

    BNE_STAGED=1 (measurement only, nothing is built from it unless asked): the heights come from the staged solver's stage 5
    (staged_heights: the same problem, our own deterministic ADMM, no kernel), the answer in the same form."""
    if os.environ.get("BNE_STAGED", "").strip().lower() in ("1", "on", "yes", "true"):
        import staged_heights
        return staged_heights.solve_shape(net, sysm, pcs, smp, ground, fixed_z, diagnostics)
    import pools
    import xproc
    try:
        return _solve_shape_qp(net, sysm, pcs, smp, ground, fixed_z, diagnostics)
    except (xproc.TimeLimit, pools.Abandoned):      # (the step's time limit, or a place a teleport left: not "no answer")
        raise
    except Exception as e:      # (no kernel, or it failed)
        if diagnostics is not None:
            diagnostics["qp_error"] = f"{type(e).__name__}: {e}"
        return None


def _natural_lines(net, sysm, pcs, smp, ground):
    """{deck piece: its natural height at its samples} (findground, rule 1): the line between the ground at the two
    ends of its structure (the decks joined end to end are one structure: the line over their total length), the ends
    being where the structure leaves the ground, the nearest each way where a viaduct has ramps (_Verdicts.free)."""
    piece_of = {pc["main"]: p for p, pc in enumerate(pcs) if pc["kind"] == "deck"}
    out = {}
    for i, p in piece_of.items():
        (da, fa), (db, fb) = sysm["free"][i]
        za, zb = (float(np.asarray(ground[piece_of[m]], np.float64)[-e]) if m in piece_of else None
                  for m, e in ((fa[0], fa[1]), (fb[0], fb[1])))
        d = np.asarray(smp[p]["d"], np.float64)
        if za is None or zb is None:
            t = np.asarray(ground[p], np.float64)
            za, zb, da, db = float(t[0]), float(t[-1]), 0.0, 0.0
        out[p] = za + (zb - za) * (d + da) / max(float(d[-1]) + da + db, 1e-9)
    return out


def _deck_targets(net, sysm, pc, t, natural):
    """A deck's target and weights: toward its natural height (_natural_lines), held there if a road is dug under its
    chain (a chain that digs: _Verdicts.digs). Only its ends on the road on the ground weigh (W_END: the embankment
    there; where it goes on in another deck of its chain it has no end), its middle weighs nothing: what keeps its line
    is its stiffness (W_STIFF), so the deck rises or falls as a whole by what its
    clearances ask, not by a bump over each of them with its ends dropping from it at their grade."""
    i = pc["main"]
    n = len(t)
    joined = [s_ for a, sa, b, sb in sysm["joins"] for s_, m_ in ((sa, a), (sb, b)) if m_ == i]
    short = i in sysm["digs"]
    w = np.full(n, W_HOLD if short else 0.0)
    for k, s_end in ((0, 0.0), (n - 1, net.L[i])):
        if not any(abs(s_ - s_end) <= JOIN_M for s_ in joined):
            w[k] = W_HOLD if short else W_END
    return natural, w


def _tunnel_mouths(net, sysm, i):
    """(start, end): whether deck i's start / end is where it goes into a tunnel (a join with a tunnel member at that end). A
    bridge that becomes a tunnel along one road lands on the ground at the mouth as it does at any end on the ground: no
    void under it there to keep its girders out of (its floor over the ground it spans is for the span, not for the road's
    way into the hill). It weighs nothing there (the tunnel's own height is the joint's), but its girders' floor must not hold
    it up over the tunnel's ceiling, the ground."""
    out = [False, False]
    for a, sa, b, sb in sysm["joins"]:
        for m_, s_, o in ((a, sa, b), (b, sb, a)):
            if m_ == i and net.m[o].kind == "tunnel":
                if s_ <= JOIN_M:
                    out[0] = True
                if net.L[i] - s_ <= JOIN_M:
                    out[1] = True
    return out


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
    gone = sysm.get("order", {}).get("gone", ())    # (pairs whose order the order step gave up: ix/s3_order.py)
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
            if p == q or _over(net.m[mu], net.m[md]) != "i" or frozenset((mu, md)) in gone:
                continue
            xd = np.asarray(smp[p]["xy"], np.float64)
            near = np.flatnonzero(((xd >= lo_u) & (xd <= hi_u)).all(1))
            if not len(near):
                continue
            t = np.clip(((xd[near, None, :] - a[None]) * ab[None]).sum(2) / L2[None], 0.0, 1.0)
            dist = dnp.hypot(*(a[None] + t[..., None] * ab[None] - xd[near, None, :]).transpose(2, 0, 1))
            k = np.argmin(dist, axis=1)
            under = np.flatnonzero(dist[np.arange(len(near)), k] <= half)
            g = max(deck_grade(net.m[mu]), deck_grade(net.m[md]))
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


def _crossing_vars(pcs, smp, c):
    """What the problem asks of crossing c's footprint (under_pairs): the arcs on its upper and lower member, and the
    unknowns of the pieces there (_vars of each; piece -1: the member's height is not the shape's)."""
    uv = np.array([p_[0] for p_ in c["under"]])
    dv = np.array([p_[1] for p_ in c["under"]])
    return uv, dv, _vars(pcs, smp, c["u"], uv), _vars(pcs, smp, c["d"], dv)


def fixed_reads(sysm, pcs, smp):
    """{member: ascending arcs}: where the problem reads the height of a member the shape does not move (a road on the
    ground under a deck, a deck or a track over a road that goes under): shape_problem's `fixed`, asked at exactly these.
    The terrain step reads them once (ix/s4_terrain.py); the problem is then made from what it read."""
    out = {}
    tracks = {int(pc["main"]) for pc in pcs if pc["kind"] == "track"}
    for c in sysm["crossings"]:
        uv, dv, (xp, _xa, _xb, _xt), (yp, _ya, _yb, _yt) = _crossing_vars(pcs, smp, c)
        for member, sv, m in ((c["d"], dv, (xp >= 0) & (yp < 0)), (c["u"], uv, (xp < 0) & (yp >= 0))):
            if m.any():
                out.setdefault(int(member), set()).update(np.unique(sv[m]).tolist())
        for member, sv in ((c["d"], dv), (c["u"], uv)):     # (a track is read where a crossing asks it, when the tracks are held)
            if int(member) in tracks and len(sv):
                out.setdefault(int(member), set()).update(np.unique(sv).tolist())
    for pc, x in zip(pcs, smp):         # (a track piece is drawn to the bed the railways lay, at each of its samples)
        if pc["kind"] == "track":
            mem, ms = np.asarray(x["mem"]), np.asarray(x["ms"], np.float64)
            for i in np.unique(mem).tolist():
                out.setdefault(int(i), set()).update(ms[mem == i].tolist())
    return {i: np.array(sorted(v), np.float64) for i, v in sorted(out.items())}


def _track_tops(x, fixed):
    """The rails' top the railways lay at the samples x of a track piece (NaN where they lay none)."""
    top = np.full(len(x["ms"]), np.nan)
    mem, ms = np.asarray(x["mem"]), np.asarray(x["ms"], np.float64)
    for i in np.unique(mem).tolist():
        sel = mem == i
        top[sel] = fixed(int(i), ms[sel])
    return top


def shape_problem(net, sysm, pcs, smp, ground, fixed_z, trace=None, pin_tracks=False):
    """The system's constraints as the kernel is given them: (chains, couplings) of carriageways.solve (a chain per
    piece: its target, weights, floor and ceiling per sample, grade, curve, step; the couplings: joints, overlaps,
    clearances). trace (a dict, the audit's: interchange_audit): filled with where each constraint came from, "cp"
    (one per coupling: ("join", index in sysm["joins"]), ("overlap",), ("clear", index in sysm["crossings"]),
    ("stacked",)) and "bound" ({(piece, sample, "lo" | "hi"): (the bound, the crossing's index)} of the bounds a crossing
    with a road the shape does not move set). pin_tracks: the crossings read every track as the heights the railways lay (fixed_z,
    as a road on the ground under a deck is read), never as unknowns: the problem as it was when a track was no member of the
    shape (the track's own chain is then drawn to its bed, free of anything else: its window ends are not held either, a short
    window's bed is the railways' own and may be steeper than the track's grade at its two ends, and the roads' problem would
    have no heights for it; _solve_shape_qp asks it first)."""
    chains = []
    is_track = np.array([pc["kind"] == "track" for pc in pcs], bool)
    natural = _natural_lines(net, sysm, pcs, smp, ground)
    clear, fz, clear_by = {}, {}, {}

    def fixed(i, sv):
        new = [s_ for s_ in sv.tolist() if (i, s_) not in fz]
        if new:
            for s_, z_ in zip(new, np.asarray(fixed_z(i, np.array(new)), np.float64).tolist()):
                fz[(i, s_)] = z_
        return np.array([fz[(i, s_)] for s_ in sv.tolist()])
    for p, (pc, x, t) in enumerate(zip(pcs, smp, ground)):
        t = np.asarray(t, np.float64)
        n = len(t)
        lo, hi = np.full(n, -np.inf), np.full(n, np.inf)
        if pc["kind"] == "deck":
            target, w = _deck_targets(net, sysm, pc, t, natural[p])
            # (a span over the ground: its girders off it past its abutments (_abutments), a deck that crosses a road, a
            # track or another deck too: a crossing holds a deck up only where it crosses. Kawazu's Nanadaru loop, one
            # road's three decks crossing each other and two streets, had no floor at all: between its crossings each
            # deck went the straight way between its ends' ground, its lowest turn 18 m inside the hill it winds round.
            # A deck a road is dug under stays at its ground (a chain that digs: _Verdicts.digs), and over a road the system digs the ground is
            # that road's cut, the crossing's clearance its floor: _over_dips)
            if pc["main"] not in sysm["digs"]:
                depth = deck_depth(net.L[pc["main"]])
                mouth = _tunnel_mouths(net, sysm, pc["main"])
                inner = (~_abutments(t, target, depth, w[0] > 0.0 or mouth[0], w[-1] > 0.0 or mouth[1])
                         & ~_over_dips(net, pcs, smp, x))
                lo = np.where(inner, t + depth + DECK_AIR_M, -np.inf)
        elif pc["kind"] == "track":
            # (a track held at the bed the railways lay, its rails' top over it: the weight is what moving it costs against a
            # road's (W_TRACK). Held there at both ends of its window: the track outside it is the railways' own, and the
            # two must meet. Where the railways lay no bed (a stub they leave out) there is no track to hold)
            top = _track_tops(x, fixed)
            ok = np.isfinite(top)
            target, w = np.where(ok, top, t), np.where(ok, W_TRACK, 0.0)
            for k in (() if pin_tracks else (0, n - 1)):      # (held, the crossings read the tracks: no law of a track's own may
                if ok[k]:                                     # contradict the roads', the problem is as it was without them)
                    lo[k] = hi[k] = top[k]
        else:
            target, w = t.copy(), np.full(n, W_DIP)
            hi = t.copy()                                   # (a road going under never rises over its ground)
        g, rad = _law(net, pc)
        # (the grade law per step on the closest two samples: a piece through joints has them closer than its mean
        # step, and held at the mean its grade ran 1.8 % over the law there)
        dd = np.diff(np.asarray(x["d"], np.float64))
        step = float(dd[dd > 1e-6].min()) if (dd > 1e-6).any() else float(STEP_M)
        # (SEGMENT_STEP_ON: each segment held to the law by its own spacing, the kernel's `ds`: one closest pair of samples,
        # 0.3 m apart where a joint falls beside a lattice sample, held a 232 m dip piece to 0.3 m of grade all along)
        own = dict(ds=dd) if SEGMENT_STEP_ON else {}
        # (a deck keeps its line, a road going under only its depth: W_STIFF is the least that makes a deck rise as a whole
        # by what it lacks (with the deck's own grade law, deck_grade): on a bridge over a motorway with the embankments'
        # top 6 m up its ends stand 14.92 m with the line free (1e-3), 15.42 with the deck law alone, 15.76 at 1000, on
        # the embankment's 15.95 at 3000. Stiffer, a deck cannot bend toward its ends' ground at all: at 2e4 (the road
        # shapes' own, carriageways.LAMBDA) a 515 m deck's end stood 1.8 m over the real road's, which Earth filled up
        # to, and the ground roads filled or dug against the survey went from 39 / 0 / 0 to 44 / 3 / 2 per mille
        # (Brunau, Europabruecke, Duettweilerbruecke), at 3000 to 40 / 0 / 0)
        chains.append(dict(t=target, w=w, lo=lo, hi=hi, grade=g, K=rad / 100.0, lam=W_STIFF if pc["kind"] == "deck" else 1e-3,
                           step=step, length=0.0, **own))
    cps, why = [], []
    for n_, (i, si, j, sj) in enumerate(sysm["joins"]):
        x, y = _var(pcs, smp, i, si), _var(pcs, smp, j, sj)
        if x and y:
            cps.append((x[0], x[1] if x[3] < 0.5 else x[2], y[0], y[1] if y[3] < 0.5 else y[2], "eq", 0.0))
            why.append(("join", n_))
    for p, a, q, b in _overlaps(pcs, smp):
        cps.append((p, a, q, b, "eq", 0.0))
        why.append(("overlap",))
    # (the clearance wherever the road runs under the upper part, under_pairs: its whole carriageway under the deck's)
    bound = {} if trace is not None else None
    crossing = []           # (the audit's trace: each crossing's upper part and the clearance it asks)
    for ci, c in enumerate(sysm["crossings"]):
        need = clearance_need(net, c["u"], c["d"], c["q"])
        crossing.append((c["u"], need))
        uv, dv, (xp, xa, xb, xt), (yp, ya, yb, yt) = _crossing_vars(pcs, smp, c)
        if pin_tracks:          # (a track is read where a crossing asks it, not solved for)
            xp = np.where((xp >= 0) & is_track[np.maximum(xp, 0)], -1, xp)
            yp = np.where((yp >= 0) & is_track[np.maximum(yp, 0)], -1, yp)
        both = (xp >= 0) & (yp >= 0)
        for kx, mx in _weighted(xa, xb, xt):
            for ky, my in _weighted(ya, yb, yt):
                m = both & mx & my
                for key in set(zip(xp[m].tolist(), kx[m].tolist(), yp[m].tolist(), ky[m].tolist())):
                    if need > clear.get(key, -math.inf):
                        clear[key] = need
                        clear_by[key] = ci
        m = (xp >= 0) & (yp < 0)            # (the road under read, fixed_z: the deck over it held over it)
        if m.any():
            u_s, inv = np.unique(dv[m], return_inverse=True)
            zd = fixed(c["d"], u_s)[inv]
            for kx, mx in _weighted(xa[m], xb[m], xt[m]):
                ok = mx & np.isfinite(zd)
                for p_ in np.unique(xp[m][ok]).tolist():
                    q_ = ok & (xp[m] == p_)
                    np.maximum.at(chains[p_]["lo"], kx[q_], zd[q_] + need)
                    if bound is not None:
                        for k_, v_ in zip(kx[q_].tolist(), (zd[q_] + need).tolist()):
                            if v_ >= bound.get((p_, k_, "lo"), (-math.inf,))[0]:
                                bound[(p_, k_, "lo")] = (v_, ci)
        m = (xp < 0) & (yp >= 0)            # (the part over read: the road under it held under it)
        if m.any():
            u_s, inv = np.unique(uv[m], return_inverse=True)
            zu = fixed(c["u"], u_s)[inv]
            for ky, my in _weighted(ya[m], yb[m], yt[m]):
                ok = my & np.isfinite(zu)
                for p_ in np.unique(yp[m][ok]).tolist():
                    q_ = ok & (yp[m] == p_)
                    np.minimum.at(chains[p_]["hi"], ky[q_], zu[q_] - need)
                    if bound is not None:
                        for k_, v_ in zip(ky[q_].tolist(), (zu[q_] - need).tolist()):
                            if v_ <= bound.get((p_, k_, "hi"), (math.inf,))[0]:
                                bound[(p_, k_, "hi")] = (v_, ci)
    for (p, kx, q, ky), need in sorted(clear.items()):
        cps.append((p, kx, q, ky, "ge", need))
        why.append(("clear", clear_by[(p, kx, q, ky)]))
    for q, kq, p, kp, need in _stacked(net, sysm, pcs, smp):
        cps.append((q, kq, p, kp, "ge", need))
        why.append(("stacked",))
    if trace is not None:
        trace["cp"], trace["bound"], trace["crossing"] = why, bound, crossing
    return chains, cps


def _solve_shape_qp(net, sysm, pcs, smp, ground, fixed_z, diagnostics):
    import carriageways as _cw
    import xproc
    # (a track keeps its bed wherever the interchange has heights with it there; only if it has none the tracks are free to
    # move, each at what it costs: W_TRACK, the least of the laws' costs of a move that closes the conflict)
    pin = any(pc["kind"] == "track" for pc in pcs)
    while True:
        chains, cps = shape_problem(net, sysm, pcs, smp, ground, fixed_z, pin_tracks=pin)
        info = {}
        xproc.check_cancel()
        _cw.PROBE.on = pin          # (the tracks held is a try: its miss is not the answer's, the tracks are freed next)
        try:
            zs = _cw.solve(chains, cps, diagnostics=info)
        finally:
            _cw.PROBE.on = False
        xproc.check_cancel()
        if pin and not info.get("feasible", True) and not info.get("timed_out"):
            pin = False
            continue
        break
    if diagnostics is not None and any(pc["kind"] == "track" for pc in pcs):
        diagnostics["tracks_moved"] = not pin
    if not info.get("feasible", True) and not info.get("timed_out"):
        # (laws that cannot all hold: not the interchange left as it is. What contradicts is eased by the least it takes, in
        # the order the laws are given up (ix/relax.py), and said so: the answer's diagnostics name each. The first attempt's
        # miss is the answer's `first`, not a log line of its own: the line of the answer is the last solve's)
        import ix.relax as _relax
        trace = {}
        chains, cps = shape_problem(net, sysm, pcs, smp, ground, fixed_z, trace, pin_tracks=pin)
        first = {k: info[k] for k in ("status", "grade_excess", "curve_excess", "lower_shortfall_m", "upper_excess_m",
                                      "junction_gap_m", "clearance_shortfall_m") if k in info}
        chains, cps, done, touched = _relax.relax(net, pcs, smp, chains, cps, trace)

        def feasible(ch, cp):
            probe = {}
            xproc.check_cancel()        # (each trial solve: the step's time limit, ix/limits.py)
            _cw.PROBE.on = True
            try:
                _cw.solve(ch, cp, diagnostics=probe)
            finally:
                _cw.PROBE.on = False
            return bool(probe.get("feasible"))
        # (the vertical curves are no difference of two heights, so the graph does not see them: where the laws were eased the
        # curve law of those roads is looked at next, on the few roads round them, before the whole is solved; then, if the
        # whole still has no heights, on more of them. Each solve of eased laws is the harder problem: its budget is its own)
        for hops in (2, 4, 8, len(chains)):
            eased, more = _relax.ease_curves(net, pcs, chains, cps, touched, feasible, hops)
            info = {}
            xproc.check_cancel()
            zs = _cw.solve(eased, cps, rounds=_cw.RELAXED_ROUNDS, deadline=time.monotonic() + _cw.BUDGET_RELAXED_S,
                           diagnostics=info)
            if info.get("feasible", True) or info.get("timed_out"):
                break
        done = list(done) + more
        info["relaxed"] = [dict(kind=r.kind, amount=round(float(r.amount), 6), at=[[int(m), round(float(s), 3)] for m, s in r.at],
                                text=r.text) for r in done]
        info["first"] = first
    if diagnostics is not None:
        diagnostics.update(info)
    if not info.get("converged") or not info.get("feasible"):
        return None
    if pin:
        # (a track that stays is at the bed the railways lay, as the roads were solved against: its chain, drawn to the bed and
        # held to its laws, is not the answer. Where the railways lay none the chain stands)
        zs = list(zs)
        for p, (pc, x) in enumerate(zip(pcs, smp)):
            if pc["kind"] == "track":
                top = _track_tops(x, lambda i, sv: np.asarray(fixed_z(int(i), sv), np.float64))
                zs[p] = np.where(np.isfinite(top), top, np.asarray(zs[p], np.float64))
    return [np.asarray(z, np.float64) for z in zs]


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


def _junction_roads(net, rp, done, pieced):
    """[(member, arc length, [toward its end L?], height, slope)]: the roads on the ground that ramp rp (embankments'
    tuple) meets before it is back on its ground (their lines within JOIN_M: a junction, an end on it or its on them,
    or a crossing at grade), other than where done ({member: [(s from, s to)]}: set by a shape or a ramp already) has
    them; the arc on that road where they meet, the ways along it away from there (both, mid-road; one, at its end),
    the ramp's height there and the slope of its surface there (the grade along the ramp as a vector: rise per m east
    and north). pieced: {member: [(s from, s to)]} of the shapes' pieces (decks, dips). The height is the ramp's
    between its samples, at the point nearest the road's line: the nearest sample is up to half a step (2 m) from the
    junction, 0.14 m of a 7 % ramp. A ramp that never gets OFF_GROUND_M off its ground has no junctions: a cut 0.04 m
    deep at the end of a deck laid the trunk meeting it 0.04 m down at the junction, the deck's approach beside it a
    bump (Storrow Drive, Boston)."""
    _j, _s0, _d, _z0, x, z, _k, sign, gr, ks, _g0, _left = rp
    k = min(int(ks), len(z))
    if k < 1:
        return []
    z = np.asarray(z, np.float64)[:k]
    off = sign * (z - np.asarray(gr, np.float64)[:k])
    if off.max() <= OFF_GROUND_M:
        return []
    xy = np.asarray(x["xy"], np.float64)[:k]
    mem = np.asarray(x["mem"])[:k]
    ms = np.asarray(x["ms"])[:k]
    # (the ramp's height only where it is the road's: not inside a shape's piece on it, a dip Perth's ramp ran
    # through at 3.5 m over it, its junction's side road raised to the ramp)
    own = np.array([not any(a + 0.5 < v < b - 0.5 for a, b in pieced.get(int(i), ())) for i, v in zip(mem, ms)])
    # (... and only while it is off its ground: past its touch the ramp is the ground, and a junction there put the
    # roads meeting it down on it — one on an embankment of its own 2 m over it was cut to it)
    own &= off > 0.0
    if not own.any():
        return []
    # (the ramp's surface at each sample: its grade along its line, as the direction of fall)
    lo_, hi_ = np.maximum(np.arange(k) - 1, 0), np.minimum(np.arange(k) + 1, k - 1)
    v = xy[hi_] - xy[lo_]
    run = np.maximum(dnp.hypot(v[:, 0], v[:, 1]), 1e-9)
    surface = ((z[hi_] - z[lo_]) / run)[:, None] * (v / run[:, None])
    # (the ramp's line every 1/16 step between its samples, over the stretches that are its own)
    u = np.linspace(0, k - 1, (k - 1) * 16 + 1) if k > 1 else np.zeros(1)
    i0, i1 = np.floor(u).astype(int), np.minimum(np.ceil(u).astype(int), k - 1)
    keep = own[i0] & own[i1]
    pt = np.stack([np.interp(u, np.arange(k), xy[:, 0]), np.interp(u, np.arange(k), xy[:, 1])], axis=1)[keep]
    zt = np.interp(u, np.arange(k), z)[keep]
    at = np.rint(u).astype(int)[keep]
    lo, hi = pt.min(0) - JOIN_M, pt.max(0) + JOIN_M
    bx = net.box
    cand = np.flatnonzero((bx[:, 0] <= hi[0]) & (bx[:, 2] >= lo[0]) & (bx[:, 1] <= hi[1]) & (bx[:, 3] >= lo[1]))
    out = []
    for j in cand.tolist():
        mj = net.m[j]
        if mj.kind != "ground" or len(net.lines[j]) < 2:
            continue
        sj, dist = _proj_many(net.lines[j], net.s[j], pt, near=JOIN_M)
        q = int(np.argmin(dist))
        if dist[q] > JOIN_M:
            continue
        s_ = float(sj[q])
        if any(a - JOIN_M <= s_ <= b + JOIN_M for a, b in done.get(j, ())):
            continue
        ways = [w for w in (True, False) if (net.L[j] - s_ if w else s_) > JOIN_M]
        if ways:
            # (the parent's arc at the junction too: the reach's budget spends it, ix's _ramp_reaches)
            out.append((j, s_, ways, float(zt[q]), surface[at[q]], float(x["d"][at[q]])))
    return sorted(out, key=lambda o: str(net.m[o[0]].key))


class _Ramper:
    """The ramps off the ends of shapes, laid one at a time: embankments' (from the solved shapes' ends and what they reach)
    and the reach's (_ramp_reaches: from the height the laws ask of an end, before anything is solved), one laying for
    both. ramps: (first member, s0, direction +1/-1, z0, samples, z, touch index, sign, ground, split index, leaving
    grade, reach budget left) of each laid; started: (member, direction, s0, sign, z0) of the ends a ramp has left; pieced: {member: [(s
    from, s to)]} the shapes' pieces (decks, dips); done: {member: [(s from, s to)]} the stretches set (the shapes' pieces
    first, then every ramp's as it is laid: a road's own ramp through a junction is its height there, not another's).
    ground_of(samples) -> the ground's heights at them."""

    def __init__(self, net, ground_of, pieced):
        self.net, self.ground_of, self.pieced = net, ground_of, pieced
        self.ramps, self.started = [], []
        self.done = {j: list(v) for j, v in pieced.items()}

    def cover(self, x, ks):
        """done: the stretches of the members a ramp's first ks samples run along."""
        for j_, ab in _stretches(x, ks).items():
            self.done.setdefault(j_, []).append(ab)

    def ramp_from(self, j, s0, toward_end, z0, g_along, branch=False, over=0.0, sign=None, left=None):
        """The ramp from height z0 (None: over its own ground by over) on member j at arc length s0 away from there (toward
        j's end if toward_end), leaving at grade g_along; branch: it is a junction's, off a ramp's. sign: +1 an embankment,
        -1 a cut (None: by z0 against the ground there). left: the arc length left of the reach's budget
        (REACH_MAX_M less what its chain has run from the deck's end; None: none, the embankments' own laying)."""
        net, started, pieced, done = self.net, self.started, self.pieced, self.done
        segs = _onward(net, j, s0, toward_end)
        x = sample(net, [dict(segs=segs)])[0]
        mem, ms = np.asarray(x["mem"]), np.asarray(x["ms"])
        gr = self.ground_of(x)
        if z0 is None:
            z0 = float(gr[0]) + over
        if sign is None:
            sign = 1.0 if z0 >= gr[0] else -1.0
        d_ = 1 if toward_end else -1
        g_max, rad = max_grade(net.m[j]), curve_radius(net.m[j])
        z, k = ramp(z0, g_along, sign, g_max, rad, x["d"] - x["d"][0], gr)
        if branch:
            # (a junction's ramp never rises past the crest it leaves it with (a fill) nor falls below the trough (a
            # cut): the junction is the top of the embankment it starts, the bottom of the cut, or where its grade
            # carries it on from there (the height its curve to the max grade reaches: 0.5 rad g^2). Left to follow
            # the ground past where it touches it — a road climbing a hill away from the junction, its grade read
            # across the climb — the ramp carried a fill 5 m up it, over the road's own way up to its tunnel)
            crest = z0 + sign * 0.5 * rad * max(sign * float(np.clip(g_along, -g_max, g_max)), 0.0) ** 2
            z = np.minimum(z, crest) if sign > 0 else np.maximum(z, crest)
        twin = any(jj == j and dd == d_ and sg == sign and abs(ss - float(s0)) <= JOIN_M and zz == float(z0)
                   for jj, dd, ss, sg, zz in started) and k >= len(z)
        started.append((j, d_, float(s0), sign, float(z0)))
        ks = k
        # (a ramp of a junction stops where a stretch a shape or another ramp already sets begins: over that stretch
        # its height is the other's, and the two, laid one to the other's end, meet in a step. And a ramp whose end
        # another has already left — the same road, way and height, within JOIN_M of it: parallel carriageways'
        # decks end together, each with its ramp. Where the one met its ground and the other's landing runs past all it
        # had to land on — the one end's embankment laid twice, the second running on over the plan's own grading past
        # the ground the first met — the second stops at once: the one end has the one ramp, the first. Its reach for
        # the junctions it meets is still its touch, the ramp the road's height that far. A deck's or a dip's own ramp
        # stops where it enters a shape's piece it did not start in: the road there is the shape's, and a fill's floor
        # laid over a dip's ceiling has no height to be (Gravelly Hill: a deck's end at the start of a road's dip laid
        # 12 samples of fill, 3.7 m over the dip's 92 m, over its first 44 m)
        if twin:
            ks = min(ks, 1)
        else:
            for i in range(1, len(z)):
                m_, v_ = int(mem[i]), float(ms[i])
                if branch:
                    stop = any(a + 0.5 < v_ < b - 0.5 for a, b in done.get(m_, ()))
                else:
                    stop = any(a + 0.5 < v_ < b - 0.5 and not (m_ == j and a + 0.5 < s0 < b - 0.5)
                               for a, b in pieced.get(m_, ()))
                if stop:
                    ks = min(ks, i)
                    break
        if left is not None and ks > 1:      # (the reach's budget: no sample of this ramp runs further along than it)
            ks = max(1, min(ks, int(np.searchsorted(x["d"], left)) + 1))
        self.ramps.append((j, s0, d_, z0, x, z, k, sign, gr, ks, g_along, left))
        if not branch:
            self.cover(x, ks)


def embankments(net, systems, shapes, ground_along, beside=()):
    """The roads' heights round the shapes: per member, [(s (m,), z (m,), mode)] with mode "fill" (the plan rises to
    it: an embankment), "cut" (the plan goes down to it) or "set" (the plan takes it: a road between two shapes whose
    ramps meet before the ground does, lerped). shapes: per system (pieces, samples, heights);
    ground_along(member, s) -> ground heights. beside: the pieces (pieces()) of other systems of net that are not among
    systems (solved elsewhere, as interchanges are, each by its owner): what the shapes hold of the roads there is theirs
    too, so no ramp of these runs on through it."""
    out = {}
    pieced = {}                 # (member: the stretches [(s from, s to)] of the shapes' pieces)
    for _pcs in [sh[0] for sh in shapes] + list(beside):
        for pc in _pcs:
            for j, a, b in pc["segs"]:
                pieced.setdefault(int(j), []).append((min(a, b), max(a, b)))

    def ground_of(x):
        """The ground under a ramp's samples (each member's in one read, not a read a sample)."""
        mem, ms = np.asarray(x["mem"]), np.asarray(x["ms"])
        gr = np.empty(len(mem))
        for m_ in np.unique(mem):
            sel = mem == m_
            gr[sel] = ground_along(int(m_), ms[sel])
        return gr

    lay = _Ramper(net, ground_of, pieced)
    ramps, done, cover, ramp_from = lay.ramps, lay.done, lay.cover, lay.ramp_from

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
    # a junction a ramp reaches before the ground is at the ramp's height there, for every road that meets it: each
    # other road leaves it from that height on its own ramp to its ground (and so on through the junctions those
    # reach). Followed along one road only, a deck's embankment put the six-road square at its end 1.28 m over
    # its ground on the two roads it went through, the four others left on the ground there and the square laid
    # at theirs: the deck's last 5.4 m fell 1.6 m to it (Porte Maillot, 48.8781 N 2.2826 E)
    n0 = len(ramps)
    n = 0
    while n < len(ramps):
        if n >= n0:
            cover(ramps[n][4], ramps[n][9])
        for j, s_, ways, z0, surface, _at_d in _junction_roads(net, ramps[n], done, pieced):
            done.setdefault(j, []).append((s_ - JOIN_M, s_ + JOIN_M))    # (the junction taken: another ramp
            #                                                         of the one shape meets its roads a few metres
            #                                                         along, at a height of its own descent — a second
            #                                                         group there, lerped to the first, stepped
            #                                                         between the two)
            for toward_end in ways:
                # (a road leaves the junction at the grade of the embankment's surface along its own direction: level
                # across the ramp's fall line, at the ramp's grade along it — a fork's other branch, running up the same
                # embankment beside the ramp, rises with it; left level it fell 1 m under the climb of the road beside
                # it, the two carriageways' pavements overlapping)
                tb = _tangent(net.lines[j], net.s[j], s_) * (1.0 if toward_end else -1.0)
                ramp_from(j, s_, toward_end, z0, float(surface @ tb), branch=True)
        n += 1
    # two ramps on one road toward each other that meet before the ground: the line between their ends. Between two
    # junctions' ramps, the curve through their heights that leaves each at its grade (a cubic: the straight line
    # between them ignored the climb the one leaves with, the road laid 0.6 m under the crest the laws made of it)
    used = set()
    for a in range(len(ramps)):
        for b in range(len(ramps)):
            if a == b or a in used or b in used:
                continue
            ja, sa, da, za, xa, _z_a, _ka, _sa, _ga, ka, g0a, _la = ramps[a]
            jb, sb, db, zb, xb, _z_b, _kb, _sb, _gb, kb, g0b, _lb = ramps[b]
            if ja != jb or da != 1 or db != -1 or sb <= sa:
                continue
            reach_a = sa + xa["d"][min(ka, len(xa["d"]) - 1)]
            reach_b = sb - xb["d"][min(kb, len(xb["d"]) - 1)]
            if reach_a >= reach_b:
                k = max(2, int(math.ceil((sb - sa) / STEP_M)) + 1)
                sv = np.linspace(sa, sb, k)
                t = (sv - sa) / max(sb - sa, 1e-6)
                if a >= n0 and b >= n0:
                    run = max(sb - sa, 1e-6)
                    zv = ((2 * t ** 3 - 3 * t ** 2 + 1) * za + (t ** 3 - 2 * t ** 2 + t) * run * g0a
                          + (-2 * t ** 3 + 3 * t ** 2) * zb + (t ** 3 - t ** 2) * run * -g0b)
                else:
                    zv = za + (zb - za) * t
                out.setdefault(ja, []).append((sv, zv, "set"))
                used |= {a, b}
    for n, (j, s0, d, z0, x, z, k, sign, gr, ks, _g0, _left) in enumerate(ramps):
        if n in used:
            continue
        k_end = min(ks, len(z))
        if sign < 0:
            # (a cut is the road under its ground: past where its ramp meets the ground, a ceiling over it is no cut, and
            # it holds the roads that land on it down: Porte Maillot's side road ended at a landing curve 0.16 m over
            # its ground, 39.56 under the 39.64 floor of the road it joins. The ceiling ends on the ground, at the first
            # sample the ramp is not under it)
            on = np.flatnonzero(np.asarray(z)[1:k_end] >= np.asarray(gr)[1:k_end])
            if len(on):
                k_end = int(on[0]) + 2
                z = np.array(z, np.float64)
                z[k_end - 1] = min(z[k_end - 1], gr[k_end - 1])
        _split(net, x, z, k_end, "fill" if sign > 0 else "cut", out)
    return out
