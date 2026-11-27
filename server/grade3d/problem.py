"""Step 6 of the 3D solve: the kernel's problem, its chains (one per path: the laws) and its couplings (the joins and the
layers).

    pose(network, samples, laws) -> Problem

- A join holds the two paths at one height (hard: a road meeting another is continuous).
- A layer (step 2) over: the upper path stands over the lower by its clearance, wherever the lower runs under the upper's
  carriageway (the lower's samples within the upper's half width and CLEAR_PAD_M of the crossing, across the angle they
  cross at) and the upper over the lower's (soft: its Layer's weight per m^2 short).
- A layer at one level: the two meet (two soft inequalities, a step either way paid at its weight).
- A path shorter than a sample step (a stub of a few cm between two members) has no soft laws: its two samples are held
  within its comfortable grade outright (the kernel's slack over a few cm is ill-conditioned)."""
from dataclasses import dataclass, field

import numpy as np

CLEAR_PAD_M = 1.5           # m past the upper's half width the lower is held under it (a shoulder, a parapet)
MIN_STEP_M = 0.5            # m: a path whose step is shorter has no soft laws
MIN_SIN = 0.25              # the sine of the angle two paths cross at, at least (a crossing at 15 deg reaches 4x as far)


@dataclass
class Problem:
    """chains: [dict] the kernel's, one per path; couplings: [(chain a, sample i, chain b, sample j, kind, value)];
    soft: [weight] one per coupling (0: hard); why: [(what, index)] one per coupling: ("join", k) of samples.joins,
    ("clear", k) and ("meet", k) of samples.crossings."""
    chains: list
    couplings: list = field(default_factory=list)
    soft: list = field(default_factory=list)
    why: list = field(default_factory=list)

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


BESIDE_M = 8.0              # m between two carriageways' edges within which the ground between them is held to ...
BATTER = 0.5                # ... rising or falling at most this (1:2) from one to the other; overlapping, one height
W_BESIDE = 1.0e3            # per m^2 past it
BESIDE_EVERY = 2            # one pair every this many samples of a path
LAYERED_NEAR_M = 150.0      # m along either path from where the two cross at two levels: no batter between them


def beside(network, samples):
    """[(path a, sample i, path b, sample j, gap m)]: the samples of roads on the ground and tracks (not decks, not
    tunnels) whose carriageways come within BESIDE_M of another path's (gap: between their edges, negative where they
    overlap), each sample's nearest of that path, one pair per BESIDE_EVERY samples, each pair once. A path's own samples
    far along it (a hairpin) count as another's."""
    kinds = np.array([m.kind for m in network.members], dtype=object)
    half = np.array([float(m.half) for m in network.members])
    on = np.flatnonzero(np.isin(kinds[samples.member], ("ground", "rail")))
    if not len(on):
        return []
    path_of = np.repeat(np.arange(len(samples.h)), np.diff(samples.off))
    xy, hs = samples.xy[on], half[samples.member[on]]
    cell = float(2.0 * hs.max() + BESIDE_M)
    key = np.floor(xy / cell).astype(np.int64)
    W = int(key[:, 1].max() - key[:, 1].min()) + 3
    lin = (key[:, 0] - key[:, 0].min() + 1) * W + (key[:, 1] - key[:, 1].min() + 1)
    order = np.argsort(lin, kind="stable")
    sk = lin[order]
    best = {}
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            q = lin + dx * W + dy
            a, b = np.searchsorted(sk, q, "left"), np.searchsorted(sk, q, "right")
            cnt = b - a
            src = np.repeat(np.arange(len(on)), cnt)
            if not len(src):
                continue
            first = np.repeat(a - np.r_[0, np.cumsum(cnt)[:-1]], cnt)
            dst = order[first + np.arange(len(src))]
            pa, pb = path_of[on[src]], path_of[on[dst]]
            same = pa == pb
            far_along = np.abs(samples.s[on[src]] - samples.s[on[dst]]) > 3.0 * cell
            keep = (src != dst) & (~same | far_along) & (on[src] % BESIDE_EVERY == 0)
            src, dst = src[keep], dst[keep]
            d = np.hypot(*(xy[src] - xy[dst]).T)
            gap = d - hs[src] - hs[dst]
            ok = gap <= BESIDE_M
            for i, j, g, dd in zip(on[src[ok]].tolist(), on[dst[ok]].tolist(), gap[ok].tolist(), d[ok].tolist()):
                k = (i, int(path_of[j]))
                if k not in best or dd < best[k][2]:
                    best[k] = (j, g, dd)
    # (two paths that cross at two levels are held apart by their clearance near the crossing: a ramp beside the street
    # its bridge goes over, an abutment's wall between them, not a batter)
    layered = {}
    for pa, ia, pb, ib, lay in samples.crossings:
        if lay.upper is not None:
            layered.setdefault((pa, pb), []).append((ia, ib))
            layered.setdefault((pb, pa), []).append((ib, ia))
    out, seen = [], set()
    for (i, _q), (j, g, _d) in best.items():
        u = (min(i, j), max(i, j))
        if u in seen:
            continue
        seen.add(u)
        p, r = int(path_of[i]), int(path_of[j])
        a, b = int(i - samples.off[p]), int(j - samples.off[r])
        if any(abs(samples.s[i] - samples.s[samples.off[p] + x]) < LAYERED_NEAR_M
               or abs(samples.s[j] - samples.s[samples.off[r] + y]) < LAYERED_NEAR_M
               for x, y in layered.get((p, r), ())):
            continue
        out.append((p, a, r, b, float(g)))
    return out


def pose(network, samples, laws):
    """The Problem of the network (step 1) on its samples (step 3) at its laws (step 5)."""
    chains = []
    for p in range(len(network.paths)):
        ch = laws.chain(samples, p)
        if samples.h[p] < MIN_STEP_M:
            ch.update(grade=ch["soft_grade"], K=ch["soft_K"], w_grade=0.0, w_curve=0.0)
        chains.append(ch)
    pr = Problem(chains)

    def add(c, w, why):
        pr.couplings.append(c)
        pr.soft.append(float(w))
        pr.why.append(why)
    for k, (pa, ia, pb, ib) in enumerate(samples.joins):
        add((pa, ia, pb, ib, "eq", 0.0), 0.0, ("join", k))
    for k, (pa, ia, pb, ib, lay) in enumerate(samples.crossings):
        if lay.upper is None and lay.weight <= 0.0:
            continue
        if lay.upper is None:
            add((pa, ia, pb, ib, "ge", 0.0), lay.weight, ("meet", k))
            add((pa, ia, pb, ib, "le", 0.0), lay.weight, ("meet", k))
            continue
        (pu, iu, mu), (pl, il, ml) = ((pa, ia, lay.a), (pb, ib, lay.b)) if lay.upper == "a" else \
            ((pb, ib, lay.b), (pa, ia, lay.a))
        half_u = float(network.members[mu].half) + CLEAR_PAD_M
        half_l = float(network.members[ml].half) + CLEAR_PAD_M
        pairs = {(iu, j) for j in _reach(samples, pl, il, pu, iu, half_u)}
        pairs |= {(j, il) for j in _reach(samples, pu, iu, pl, il, half_l)}
        for ju, jl in sorted(pairs):
            add((pu, ju, pl, jl, "ge", float(lay.need)), lay.weight, ("clear", k))
    for k, (pa, ia, pb, ib, gap) in enumerate(beside(network, samples)):
        v = BATTER * max(0.0, gap)
        add((pa, ia, pb, ib, "le", v), W_BESIDE, ("beside", k))
        add((pa, ia, pb, ib, "ge", -v), W_BESIDE, ("beside", k))
    return pr
