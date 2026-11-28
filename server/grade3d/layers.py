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
- by the kinds within NEAR_END_M of an end of a path where the map joins the two paths within JOINED_NEAR_M ("joined"):
  a street ending where it meets the ramp off the deck it is mapped under, the map having it both meet the deck and
  pass under it."""
from dataclasses import dataclass

import numpy as np

import gradeshapes as _gsh

NEAR_END_M = 20.0       # m from a path's end within which an order by the levels alone is a merge ...
JOINED_NEAR_M = 40.0    # ... and one by the kinds too, where the map joins the two paths within this of the crossing


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


def path_joins(network):
    """{(path, path): [(s on the first, s on the second)]} of the network's joins, the lower path first; a path's joins
    with itself under (path, path), each both ways round (a loop coming back onto the path: Perth's Tonkin Highway, one
    path out over its deck carriageway and back 4.6 km later on its ground one, the two ending on one node: left out,
    their crossing 7 m before that node was a clearance the join made impossible)."""
    out = {}
    for i, si, j, sj in network.joins:
        (p, s), (r, t) = network.path_s(i, si), network.path_s(j, sj)
        if p > r:
            p, s, r, t = r, t, p, s
        out.setdefault((p, r), []).append((s, t))
        if p == r:
            out[(p, r)].append((t, s))
    return out


def joined_near(network, joins, a, sa, b, sb, near_m=JOINED_NEAR_M):
    """Whether the paths of members a and b (one path or two) are joined within near_m (along both) of where a at sa
    crosses b at sb."""
    (p, s), (r, t) = network.path_s(a, sa), network.path_s(b, sb)
    if p > r:
        p, s, r, t = r, t, p, s
    return any(abs(js - s) <= near_m and abs(jt - t) <= near_m for js, jt in joins.get((p, r), ()))


def layer(network, a, sa, b, sb, q, joins=None):
    """The Layer of members a and b of the network crossing at q (joins: path_joins(network), made once)."""
    net = network.net
    ma, mb = net.m[a], net.m[b]
    up = _gsh._over(ma, mb)
    if up is None:
        return Layer(a, sa, b, sb, q, None, 0.0, "apart" if ma.kind == mb.kind == "tunnel" else "grade")
    by_kind = _gsh.LEVEL_BAND[ma.kind] != _gsh.LEVEL_BAND[mb.kind]
    if _near_end(network, a, sa) or _near_end(network, b, sb):
        if not by_kind:
            return Layer(a, sa, b, sb, q, None, 0.0, "merge")
        if joined_near(network, path_joins(network) if joins is None else joins, a, sa, b, sb):
            return Layer(a, sa, b, sb, q, None, 0.0, "joined")
    u, d = (a, b) if up == "i" else (b, a)
    return Layer(a, sa, b, sb, q, "a" if up == "i" else "b", float(_gsh.clearance_need(net, u, d, q)),
                 "kinds" if by_kind else "levels")


def order(network):
    """[Layer] of every crossing of the network (step 1's), in its order."""
    joins = path_joins(network)
    return [layer(network, a, sa, b, sb, q, joins) for a, sa, b, sb, q in network.crossings]


def summary(layers):
    """{why: count}: what the layers rest on."""
    out = {}
    for x in layers:
        out[x.why] = out.get(x.why, 0) + 1
    return out
