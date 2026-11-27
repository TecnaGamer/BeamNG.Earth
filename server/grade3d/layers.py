"""Step 2 of the 3D solve: at each crossing, which path is over and the clearance it needs; at one level, they meet.

    order(network) -> [Layer]

The order is OSM's: the kinds first (a deck over what is on the ground, that over a tunnel), then the levels within a
kind (OSM's layer: stacked decks, a viaduct over a bridge, a track in a tunnel under a street), gradeshapes._over. The
clearance is the headroom the lower needs (its sign's, a truck's 4 m, a train's 6 m) and the upper's structure (a deck's
depth by its span, a rail deck's, a tunnel's roof), gradeshapes.clearance_need.

Every layer is a cost, not a law (the solve's soft couplings): OSM's layers can contradict the joins (a ramp merging into
a deck mapped as crossing it, one level apart), and then the problem has no answer with every clearance held. A
clearance short costs W_CLEAR per m^2; one the levels alone ask near an end of a path (a merge read as a crossing: the
end of the ramp's path, not of a member a path goes on from), W_DOUBT, and so does one by the kinds near an end of a path
where the map also joins the two paths within JOINED_NEAR_M (a street ending where it meets the ramp off the deck it is
mapped under: the map has it both meet the deck and pass under it).
At one level the two meet: their heights equal at the crossing, W_GRADE per m^2 of a step there; two tunnels at one
level do not (why "apart", weight 0: a road tunnel and a track's crossing in a mountain pass far apart, the one under
the other, and the map does not say which; Casagranda's road climbed 60 m in its tunnel to meet a track's)."""
from dataclasses import dataclass

import numpy as np

import gradeshapes as _gsh

W_CLEAR = 1.0e5         # per m^2 of a clearance short (a metre short costs what 1e5 samples a metre off their ground do)
W_DOUBT = 1.0e3         # per m^2 of a clearance the levels alone ask within NEAR_END_M of an end
W_GRADE = 1.0e4         # per m^2 of a step where two crossing at one level meet
NEAR_END_M = 20.0       # m from a path's end within which an order by the levels alone is doubted ...
JOINED_NEAR_M = 40.0    # ... and one by the kinds too, where the map joins the two paths within this of the crossing


@dataclass(frozen=True)
class Layer:
    """A crossing of members a and b (at sa, sb; at q): upper "a", "b" or None (at grade); need (m) the upper's road
    over the lower's at least (0 at grade); why "kinds", "levels", "grade" or "apart" (no coupling); weight: what a metre short (or of step)
    squared costs."""
    a: int
    sa: float
    b: int
    sb: float
    q: np.ndarray
    upper: object
    need: float
    why: str
    weight: float

    def over(self):
        """(upper member, its s, lower member, its s), or None at grade."""
        if self.upper is None:
            return None
        return (self.a, self.sa, self.b, self.sb) if self.upper == "a" else (self.b, self.sb, self.a, self.sa)


def _near_end(network, i, s):
    """Whether member i's arc length s is within NEAR_END_M of an end of its path."""
    p, sp = network.path_s(i, s)
    return min(sp, network.paths[p].length - sp) <= NEAR_END_M


def path_joins(network):
    """{(path, path): [(s on the first, s on the second)]} of the network's joins, the lower path first."""
    out = {}
    for i, si, j, sj in network.joins:
        (p, s), (r, t) = network.path_s(i, si), network.path_s(j, sj)
        if p == r:
            continue
        if p > r:
            p, s, r, t = r, t, p, s
        out.setdefault((p, r), []).append((s, t))
    return out


def joined_near(network, joins, a, sa, b, sb, near_m=JOINED_NEAR_M):
    """Whether the paths of members a and b are joined within near_m (along both) of where a at sa crosses b at sb."""
    (p, s), (r, t) = network.path_s(a, sa), network.path_s(b, sb)
    if p > r:
        p, s, r, t = r, t, p, s
    return any(abs(js - s) <= near_m and abs(jt - t) <= near_m for js, jt in joins.get((p, r), ()))


def layer(network, a, sa, b, sb, q, joins=None):
    """The Layer of members a and b of the network crossing at q (joins: path_joins(network), made once)."""
    net = network.net
    ma, mb = net.m[a], net.m[b]
    up = _gsh._over(ma, mb)
    if up is None and ma.kind == "tunnel" and mb.kind == "tunnel":
        return Layer(a, sa, b, sb, q, None, 0.0, "apart", 0.0)     # (two tunnels: one under the other, which unknown)
    if up is None:
        return Layer(a, sa, b, sb, q, None, 0.0, "grade", W_GRADE)
    by_kind = _gsh.LEVEL_BAND[ma.kind] != _gsh.LEVEL_BAND[mb.kind]
    u, d = (a, b) if up == "i" else (b, a)
    need = float(_gsh.clearance_need(net, u, d, q))
    near = _near_end(network, a, sa) or _near_end(network, b, sb)
    if joins is None:
        joins = path_joins(network)
    doubt = near and (not by_kind or joined_near(network, joins, a, sa, b, sb))
    return Layer(a, sa, b, sb, q, "a" if up == "i" else "b", need, "kinds" if by_kind else "levels",
                 W_DOUBT if doubt else W_CLEAR)


def order(network):
    """[Layer] of every crossing of the network (step 1's), in its order."""
    joins = path_joins(network)
    return [layer(network, a, sa, b, sb, q, joins) for a, sa, b, sb, q in network.crossings]


def summary(layers):
    """{why: count, "doubted": count}: what the layers rest on."""
    out = {}
    for x in layers:
        out[x.why] = out.get(x.why, 0) + 1
        if x.weight == W_DOUBT:
            out["doubted"] = out.get("doubted", 0) + 1
    return out
