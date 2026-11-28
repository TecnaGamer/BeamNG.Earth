"""Step 2 of the 3D solve: at each crossing, which path is over and the clearance it needs.

    order(network) -> [Layer]

The order is OSM's: the kinds first (a deck over what is on the ground, that over a tunnel), then the levels within a
kind (OSM's layer: stacked decks, a viaduct over a bridge, a track in a tunnel under a street), gradeshapes._over. The
clearance is the headroom the lower needs (its sign's, a truck's 4 m, a train's 6 m) and the upper's structure (a deck's
depth by its span, a rail deck's, a tunnel's roof), gradeshapes.clearance_need.

A clearance is a constraint of the solve: the upper is over the lower by it at least. A crossing is no clearance where
the map does not put one path over the other there:
- at one level ("grade"): the map has no node of both there, so it says nothing of their heights;
- two tunnels at one level ("apart"): the one under the other, and the map does not say which;
- by the levels alone within NEAR_END_M of an end of a path ("merge"): a ramp merging into a deck, mapped crossing it
  one level apart, its end the merge;
- where the map joins the two places so near each other that no route between them through its joins can part them by
  the clearance ("joined", Reach): along a path a stretch climbs or falls at most its steepest member's grade, across
  a join not at all (one height). The map has them both meet and pass one over the other, and its joins (the nodes
  the roads share, which every router reads) outrank its layer (an order for drawing). A street ending where it
  meets the ramp off the deck it is mapped under; a raceway's deck crossing its own ground approach at 1.8 deg 14 m
  from where they meet (Vienna's Suedtiroler Platz); La Defense's service tunnel starting at a node of the road
  whose 1.2 m junction piece it passes 5.25 m under 9 m on, the two joined through that road alone. Was: within
  20 m of a path's end and joined to each other directly within 40 m along both: each of these left its block with
  no answer (#135)."""
import heapq
from dataclasses import dataclass

import numpy as np

import gradeshapes as _gsh

NEAR_END_M = 20.0       # m from a path's end within which an order by the levels alone is a merge


@dataclass(frozen=True)
class Layer:
    """A crossing of members a and b (at sa, sb; at q): upper "a", "b" or None (no clearance); need (m) the upper's road
    over the lower's at least (0 without a clearance); why "kinds", "levels" (a clearance), "grade", "apart", "merge"
    or "joined" (none)."""
    a: int
    sa: float
    b: int
    sb: float
    q: np.ndarray
    upper: object
    need: float
    why: str

    def over(self):
        """(upper member, its s, lower member, its s), or None without a clearance."""
        if self.upper is None:
            return None
        return (self.a, self.sa, self.b, self.sb) if self.upper == "a" else (self.b, self.sb, self.a, self.sa)


def _near_end(network, i, s):
    """Whether member i's arc length s is within NEAR_END_M of an end of its path."""
    p, sp = network.path_s(i, s)
    return min(sp, network.paths[p].length - sp) <= NEAR_END_M


def _grade(m):
    """The steepest grade member m may take (a track's in a city or out of one, whichever is steeper)."""
    from . import laws
    return max(laws.member_law(m, False)[0], laws.member_law(m, True)[0])


class Reach:
    """The network's paths and joins as a graph of how far apart in height two of its places can be: along a path a
    stretch's length times its steepest member's grade (no vertical curve: at most that), across a join nothing (one
    height). A path's joins with itself count (a loop coming back onto the path: Perth's Tonkin Highway, one path out
    over its deck carriageway and back 4.6 km later on its ground one, the two ending on one node, crossing 7 m before
    it)."""

    def __init__(self, network):
        self.net = network
        self.grades = [[_grade(network.members[i]) for i, _f in path.parts] for path in network.paths]
        self.at = {}            # path -> sorted [(s, node)] of its joins
        self.adj = {}           # node -> [(node, height it can climb on the way)]
        ids = {}

        def node(p, s):
            k = (p, round(float(s), 3))
            if k not in ids:
                ids[k] = len(ids)
                self.at.setdefault(p, []).append((k[1], ids[k]))
            return ids[k]

        for i, si, j, sj in network.joins:
            a, b = node(*network.path_s(i, si)), node(*network.path_s(j, sj))
            self.adj.setdefault(a, []).append((b, 0.0))
            self.adj.setdefault(b, []).append((a, 0.0))
        for p, lst in self.at.items():
            lst.sort()
            for (s0, a), (s1, b) in zip(lst, lst[1:]):
                c = self.climb(p, s0, s1)
                self.adj.setdefault(a, []).append((b, c))
                self.adj.setdefault(b, []).append((a, c))

    def climb(self, p, s0, s1):
        """The most path p can climb or fall between arc lengths s0 and s1: their distance times its steepest grade
        there."""
        path = self.net.paths[p]
        lo, hi = min(s0, s1), max(s0, s1)
        ends = list(path.start[1:]) + [path.length]
        g = max(g for g, a, b in zip(self.grades[p], path.start, ends) if a <= hi + 1e-9 and b >= lo - 1e-9)
        return (hi - lo) * g

    def apart(self, p, s, r, t, need):
        """Whether place s of path p and place t of path r can be need (m) apart in height: no route between them
        through the network's joins climbs less."""
        if p == r and self.climb(p, s, t) < need:
            return False
        dist, heap = {}, []
        for sj, n in self.at.get(p, ()):
            c = self.climb(p, s, sj)
            if c < need and c < dist.get(n, np.inf):
                dist[n] = c
                heapq.heappush(heap, (c, n))
        on_r = {n: sj for sj, n in self.at.get(r, ())}
        while heap:
            d, n = heapq.heappop(heap)
            if d > dist.get(n, np.inf):
                continue
            if n in on_r and d + self.climb(r, on_r[n], t) < need:
                return False
            for m, c in self.adj.get(n, ()):
                e = d + c
                if e < need and e < dist.get(m, np.inf):
                    dist[m] = e
                    heapq.heappush(heap, (e, m))
        return True


def layer(network, a, sa, b, sb, q, reach=None):
    """The Layer of members a and b of the network crossing at q (reach: Reach(network), made once)."""
    net = network.net
    ma, mb = net.m[a], net.m[b]
    up = _gsh._over(ma, mb)
    if up is None:
        return Layer(a, sa, b, sb, q, None, 0.0, "apart" if ma.kind == mb.kind == "tunnel" else "grade")
    by_kind = _gsh.LEVEL_BAND[ma.kind] != _gsh.LEVEL_BAND[mb.kind]
    if not by_kind and (_near_end(network, a, sa) or _near_end(network, b, sb)):
        return Layer(a, sa, b, sb, q, None, 0.0, "merge")
    u, d = (a, b) if up == "i" else (b, a)
    need = float(_gsh.clearance_need(net, u, d, q))
    if not (Reach(network) if reach is None else reach).apart(*network.path_s(a, sa), *network.path_s(b, sb), need):
        return Layer(a, sa, b, sb, q, None, 0.0, "joined")
    return Layer(a, sa, b, sb, q, "a" if up == "i" else "b", need, "kinds" if by_kind else "levels")


def order(network):
    """[Layer] of every crossing of the network (step 1's), in its order."""
    reach = Reach(network)
    return [layer(network, a, sa, b, sb, q, reach) for a, sa, b, sb, q in network.crossings]


def summary(layers):
    """{why: count}: what the layers rest on."""
    out = {}
    for x in layers:
        out[x.why] = out.get(x.why, 0) + 1
    return out
