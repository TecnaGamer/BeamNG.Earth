"""Step 6 of the 3D solve: the problem, its chains (one per path: its law and its earthwork) and its couplings (the joins
and the clearances), every coupling a constraint.

    pose(network, samples, laws) -> Problem

- A join holds the two paths at one height: a road meeting another in the map is continuous.
- A clearance (step 2) holds the upper path over the lower by its need, wherever the lower runs under the upper's
  carriageway (the lower's samples within the upper's half width and CLEAR_PAD_M of the crossing, across the angle they
  cross at) and the upper over the lower's.
- A path shorter than MIN_STEP_M a step (a stub of a few cm between two members) has no vertical curve: its samples
  are too close for one."""
from dataclasses import dataclass, field

import numpy as np

CLEAR_PAD_M = 1.5           # m past the upper's half width the lower is held under it (a shoulder, a parapet)
MIN_STEP_M = 0.5            # m: a path whose step is shorter has no vertical curve
MIN_SIN = 0.25              # the sine of the angle two paths cross at, at least (a crossing at 15 deg reaches 4x as far)


@dataclass
class Problem:
    """chains: [dict] one per path (laws.chain); couplings: [(chain a, sample i, chain b, sample j, kind, value)], kind
    "eq" (z_a = z_b) or "ge" (z_a - z_b >= value); why: [(what, index)] one per coupling: ("join", k) of samples.joins,
    ("clear", k) of samples.crossings; names: one per path, what it is made of (a contradiction is named by them)."""
    chains: list
    couplings: list = field(default_factory=list)
    why: list = field(default_factory=list)
    names: list = field(default_factory=list)

    def summary(self):
        kinds = {}
        for w, _k in self.why:
            kinds[w] = kinds.get(w, 0) + 1
        return dict(chains=len(self.chains), samples=sum(len(c["t"]) for c in self.chains), **kinds)


def tangent(samples, p, i):
    """The unit direction of path p at its sample i ((0, 0) for a path of one point)."""
    xy = samples.xy[samples.of(p)]
    if len(xy) < 2:
        return np.zeros(2)
    a, b = xy[max(0, i - 1)], xy[min(len(xy) - 1, i + 1)]
    d = b - a
    n = float(np.hypot(*d))
    return d / n if n > 0 else np.zeros(2)


def _reach(samples, p, i, other, k_other, half):
    """The samples of path p within half (m, across the angle) of the other path's line at its sample k_other."""
    t1, t2 = tangent(samples, p, i), tangent(samples, other, k_other)
    sin = max(MIN_SIN, abs(float(t1[0] * t2[1] - t1[1] * t2[0])))
    h = float(samples.h[p])
    n = samples.of(p).stop - samples.of(p).start
    if h <= 0:
        return [i]
    k = int(np.ceil(half / sin / h))
    return list(range(max(0, i - k), min(n - 1, i + k) + 1))


def name(network, path):
    """What a path is made of: its members' keys, kinds, classes and levels."""
    out = []
    for i in path.members():
        m = network.members[i]
        k = m.key if not isinstance(m.key, tuple) else m.key[2] if m.key[0] == "rail" else m.key[0]
        s = "%s %s %s level %g" % (k, m.kind, m.cls, m.level)
        if s not in out:
            out.append(s)
    return "; ".join(out)


def pose(network, samples, laws):
    """The Problem of the network (step 1) on its samples (step 3) at its laws (step 5)."""
    chains = []
    for p in range(len(network.paths)):
        ch = laws.chain(samples, p)
        if samples.h[p] < MIN_STEP_M:
            ch["R"] = 0.0
        chains.append(ch)
    pr = Problem(chains, names=[name(network, path) for path in network.paths])

    def add(c, why):
        pr.couplings.append(c)
        pr.why.append(why)
    for k, (pa, ia, pb, ib) in enumerate(samples.joins):
        add((pa, ia, pb, ib, "eq", 0.0), ("join", k))
    for k, (pa, ia, pb, ib, lay) in enumerate(samples.crossings):
        if lay.upper is None:
            continue
        (pu, iu, mu), (pl, il, ml) = ((pa, ia, lay.a), (pb, ib, lay.b)) if lay.upper == "a" else \
            ((pb, ib, lay.b), (pa, ia, lay.a))
        half_u = float(network.members[mu].half) + CLEAR_PAD_M
        half_l = float(network.members[ml].half) + CLEAR_PAD_M
        pairs = {(iu, j) for j in _reach(samples, pl, il, pu, iu, half_u)}
        pairs |= {(j, il) for j in _reach(samples, pu, iu, pl, il, half_l)}
        for ju, jl in sorted(pairs):
            add((pu, ju, pl, jl, "ge", float(lay.need)), ("clear", k))
    return pr
