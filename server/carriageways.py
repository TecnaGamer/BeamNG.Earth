"""Motorway and trunk carriageways as one height problem (BNE_INTERCHANGE with BNE_CARRIAGEWAYS=on; the interchange
proposal's stage 2/3; the user, 2026-11-01: "first establish base dem, and minimum clearances, and then we produce smooth
roads that connect those dots, in a way that doesn't break our rules ... stay close to the DEM, but if the dem requires
us to make an unrealistic road, something needs to give").

Grading planned each road from the elevation up to the mapped end of its bridge, and roads.BridgeProfiles laid each deck
from the ground at its ends and lifted it over what it crosses: two answers that often did not meet. Mapped ends sit on
embankments' slopes, at the edge of a cutting or down a gully, so the road dived into a bridge and the deck dropped to
meet it (Perth's Mitchell Freeway through Northbridge: 1-2.5 m at its bridges).

Here the roads of the motorway and trunk families (their links too) are chains of parts, ground and bridge, joined end to
end, between branch points (three or more parts meeting) and loose ends. A cluster of chains (joined at branch points,
or crossing one another) is solved at once, for a height every STEP_M along each chain:

    minimise   sum w_i (z_i - t_i)^2  +  LENGTH_BIAS * sum (z_{i+1} - z_i)^2  +  LAMBDA * sum (z_{i-1} - 2 z_i + z_{i+1})^2
    such that  |z_{i+1} - z_i| <= grade * STEP_M               (the class's steepest grade)
               |z_{i-1} - 2 z_i + z_{i+1}| <= STEP_M^2 / (K * 100)   (its vertical curves: K m per 1 % of change)
               z_upper - z_lower >= clearance                   (where one passes over another or over a track)
               the chains' ends at a branch point equal

t: the elevation along it, filtered as a major road's plan; w: ANCHOR_W at a chain's ends, elsewhere GROUND_W on the
ground, 0 on a bridge, rising from 0 at a bridge's end to GROUND_W TRUST_M away (an abutment's slope, a cutting's edge
are not the road's). The length term makes the road the shortest between the points that matter. Which of two crossing parts is
over the other: the bridge over the ground; of two bridges, OSM's layer, else the one the elevation-only solve has
higher. Streets under a deck are not bounds: grading's underpass dips lower them to clear it (measured from this
profile), as the old rule did. Where the limits and the ground disagree, the ground gives way (its weight is finite);
incompatible limits are reported explicitly rather than silently accepted as a solved profile.

The Rust kernel in native/earthfast/src/carriageway_solver.rs solves the existing banded quadratic objective
with hard linear constraints, using Clarabel's native sparse QP solver when the unconstrained banded solution is
not feasible. This module only assembles its inputs and reports measured convergence/constraint diagnostics.
"""
import logging
import math
import os
import threading
import time

import numpy as np
import dnp

log = logging.getLogger("earthd.grading")
PROBE = threading.local()   # .on: this thread's solves are probes (a search for the least easing of a law: ix/relax.py): their
#                             misses are not logged, the answer's are

STEP_M = 4.0            # m between profile samples
MIN_SEGMENT_M = 0.5     # m: the least spacing a chain's own `ds` gives a segment (two samples closer than this, one point
                        # the lattice and a joint both asked for, are held to the grade of 0.5 m, not of 0 m)
TRUST_M = 40.0          # m from a bridge's end over which the ground's weight rises from 0 to 1
LAMBDA = 2.0e4          # smoothness (a curve of ~40 m per metre of bend where nothing else decides)
# The road's shape between the points whose heights matter (the user, 2026-11-02: "skip the earthworks calculation as
# i think course dem means we are basing that of incorrect data. instead add a strong bias to the shortest path
# possible between 2 points"): the elevation along a chain only weighs GROUND_W (a coarse DEM's bumps are not the
# road's), its ends weigh ANCHOR_W (where it meets the rest of the network: a junction, a tunnel's portal, the edge of
# what is solved), and every metre of rise and fall costs LENGTH_BIAS (sum of (dz)^2 per STEP_M: the shortest, straightest
# road between those points that keeps to its grade and curve limits and its clearances). Tune with the environment:
# BNE_CW_LENGTH, BNE_CW_GROUND, BNE_CW_ANCHOR (BNE_CW_LENGTH=0 BNE_CW_GROUND=1 is the earthworks fit as it was).
LENGTH_BIAS = float(os.environ.get("BNE_CW_LENGTH", "200"))
GROUND_W = float(os.environ.get("BNE_CW_GROUND", "0.2"))
ANCHOR_W = float(os.environ.get("BNE_CW_ANCHOR", "5"))
END_W = ANCHOR_W        # (the name before)
ROUNDS = 150            # sparse QP iterations at most
# s one region's solves may take at most: exhaustion is reported and the region is not persisted. A worker past
# earthd's road-tile timeout (300 s) is restarted, its work lost, and the tile built again from the start.
BUDGET_S = float(os.environ.get("BNE_CW_BUDGET_S", "30"))
# a system whose laws contradict is solved again with what contradicts eased (ix/relax.py): the harder problem (the interior point
# gets slow where the feasible set is thin), so its budget is its own
BUDGET_RELAXED_S = float(os.environ.get("BNE_CW_BUDGET_RELAXED_S", "150"))
RELAXED_ROUNDS = 4 * ROUNDS     # (its iterations too: 5444 roads' 173 where the same laws, not contradicting, take under 60)
TOUCH_M = 0.75          # m: parts whose ends are this close are joined
COUPLE_M = 2.5          # m: a chain's end this close to another chain meets it and is held to its height (a slip road's
                        # end lies up to ~2 m short of the node its overpass starts at: it stayed at the ground beside a
                        # deck 6 m up, and its surface pulled the ground under the deck's approach 2 m down)
FAMILY = ("motorway", "trunk")
# per class: steepest grade, K (m of vertical curve per 1 % of grade change: crest and sag alike)
LIMITS = {"motorway": (0.05, 30.0), "trunk": (0.06, 25.0), "motorway_link": (0.07, 12.0), "trunk_link": (0.08, 10.0)}


def family(cls):
    """'motorway' for motorway and motorway_link, 'trunk' likewise; None for anything else."""
    c = str(cls or "")
    base = c[:-5] if c.endswith("_link") else c
    return base if base in FAMILY else None


def limits(cls):
    return LIMITS.get(str(cls), LIMITS["motorway_link"])


def sample(parts, chain, step=STEP_M):
    """The chain's samples: (xy (m, 2), s (m,), part index (m,), bridge (m,) bool, from_end (m,): m to the nearest
    bridge end along it). parts [(key, line, is_bridge, cls)], chain [(index, reversed)]. Pure."""
    xs, pi, br = [], [], []
    for i, rev in chain:
        ln = np.asarray(parts[i][1], np.float64)[:, :2]
        if rev:
            ln = ln[::-1]
        s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(ln, axis=0).T))]
        m = max(2, int(math.ceil(s[-1] / step)) + 1)
        a = np.linspace(0.0, s[-1], m)
        pts = np.stack([dnp.interp(a, s, ln[:, 0]), dnp.interp(a, s, ln[:, 1])], axis=1)
        if xs:
            pts = pts[1:]                               # (the joint once)
        xs.append(pts)
        pi += [i] * len(pts)
        br += [bool(parts[i][2])] * len(pts)
    xy = np.concatenate(xs)
    s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]
    pi, br = np.array(pi), np.array(br)
    ch = np.flatnonzero(br[1:] != br[:-1])
    cut = np.r_[s[ch], s[ch + 1]] if len(ch) else np.zeros(0)
    from_end = np.min(np.abs(s[:, None] - cut[None, :]), axis=1) if len(cut) else np.full(len(s), np.inf)
    return xy, s, pi, br, from_end


def chains(parts, tol=TOUCH_M):
    """parts: [(key, line (k, 2), ...)] (the family's parts in a region, each once). Returns the chains: [[(part index,
    reversed)]], each a maximal run joined end to end through points where exactly two parts meet, ordered and
    oriented canonically (from the end of its lowest-keyed end part). Pure."""
    n = len(parts)
    if not n:
        return []
    ends = np.array([[p[1][0, :2], p[1][-1, :2]] for p in parts], np.float64).reshape(-1, 2)    # 2i, 2i+1
    order_e = np.lexsort((ends[:, 1], ends[:, 0]))
    nb = [[] for _ in range(2 * n)]
    # (neighbours by a sweep on x: ends within TOUCH_M)
    xs = ends[order_e, 0]
    for a_i, e in enumerate(order_e):
        b_i = a_i + 1
        while b_i < len(order_e) and xs[b_i] - xs[a_i] <= tol:
            f = order_e[b_i]
            if dnp.hypot(*(ends[e] - ends[f])) <= tol and e // 2 != f // 2:
                nb[e].append(int(f))
                nb[f].append(int(e))
            b_i += 1
    keys = [str(p[0]) for p in parts]
    used = np.zeros(n, bool)
    out = []
    for i in sorted(range(n), key=lambda i: keys[i]):
        if used[i]:
            continue
        # back to the start of its run
        j, rev, seen = i, False, {i}
        while True:
            e = 2 * j + (1 if rev else 0)               # the end walked in from
            if len(nb[e]) != 1:
                break
            k = nb[e][0]
            jj = k // 2
            if jj in seen:
                break
            seen.add(jj)
            j, rev = jj, (k % 2 == 0)
        seq = []
        while True:
            used[j] = True
            seq.append((j, rev))
            e = 2 * j + (0 if rev else 1)               # the end walked out of
            if len(nb[e]) != 1:
                break
            k = nb[e][0]
            jj = k // 2
            if used[jj]:
                break
            j, rev = jj, (k % 2 == 1)
        if keys[seq[0][0]] > keys[seq[-1][0]]:
            seq = [(a, not r) for a, r in seq[::-1]]
        out.append(seq)
    return out


def branch_ties(xys, brs, tol=COUPLE_M):
    """[(chain a, its end sample, chain b, b's sample)]: each chain end (xys: per chain its samples (m, 2); brs: per chain
    bool (m,), bridge) that lies within tol of another chain's inner sample on the ground (a slip road ending at the
    joint of an overpass road's parts, or alongside the road it joins, at the road's node or a little short of it: no
    other chain end meets it there, so the road is no branch point to `chains` and the slip road would be left at its
    own ground). Held to that sample's height; its chain is solved with the one it meets. Pure."""
    boxes = [(q.min(0) - tol, q.max(0) + tol) for q in xys]
    out = []
    for na, xy in enumerate(xys):
        for ia in (0, len(xy) - 1):
            p = xy[ia]
            best = None
            for nb, q in enumerate(xys):
                if nb == na or len(q) < 3 or (p < boxes[nb][0]).any() or (p > boxes[nb][1]).any():
                    continue
                if float(np.min(dnp.hypot(*(q[[0, -1]] - p).T))) <= tol:
                    continue                    # (an end meeting an end: a branch point, coupled as such)
                d = dnp.hypot(*(q[1:-1] - p).T)
                j = int(np.argmin(d)) + 1
                if d[j - 1] > tol or (brs[nb][j - 1] and brs[nb][j] and brs[nb][j + 1]):
                    continue                    # (too far, or on a deck: a road under a bridge is no branch of it)
                if best is None or d[j - 1] < best[0]:
                    best = (float(d[j - 1]), nb, j)
            if best is not None:
                out.append((na, ia, best[1], best[2]))
    return out


def weights(br, from_end, trust=TRUST_M):
    """0 on a bridge, rising (smoothstep) to 1 trust m from a bridge's end on the ground. Pure."""
    u = np.clip(from_end / trust, 0.0, 1.0)
    w = u * u * (3.0 - 2.0 * u)
    return np.where(br, 0.0, w)


def solve(chains, couplings=(), rounds=ROUNDS, deadline=None, diagnostics=None):
    """Profiles of coupled chains; the Rust kernel owns the numerical solve and releases the GIL.

    chains: dicts with t/w (n,), grade, K, optional lo/hi (n,), lam, step, length, and optionally ds (n - 1,): the distance
    from each sample to the next (a segment's grade bound and a window's curve bound are then its own spacings'; without
    it, the chain's one `step`, the closest two samples').
    couplings: (chain a, sample i, chain b, sample j, "eq", "ge" or "le", value): z_a - z_b = 0, >= value, <= value;
    or (a, i, b, j, kind, value, chain c, sample k, alpha): the same of z_a - (1 - alpha) z_b - alpha z_c (b's height
    moved alpha of the way to c's: a road's height where another leaves it; roadlaws' landings).
    Negative sample indices address a chain's end as in NumPy. Optional diagnostics receives
    convergence, budget and measured constraint residuals. The returned profiles retain input order.
    """
    import fast
    cp = list(couplings)
    three = any(len(c) == 9 for c in cp)
    fast.require(305 if three or any(c[4] == "le" for c in cp) else 156, "carriageway_profiles")
    target = [np.asarray(c["t"], np.float64) for c in chains]
    offsets = np.r_[0, np.cumsum([len(t) for t in target])].astype(np.int64)
    pack = lambda arrays: np.ascontiguousarray(np.concatenate(arrays) if arrays else np.zeros(0), np.float64)
    weights_ = [np.asarray(c["w"], np.float64) for c in chains]
    lower = [np.full(len(t), -np.inf) if c.get("lo") is None else np.asarray(c["lo"], np.float64)
             for c, t in zip(chains, target)]
    upper = [np.full(len(t), np.inf) if c.get("hi") is None else np.asarray(c["hi"], np.float64)
             for c, t in zip(chains, target)]
    for t, w, lo, hi in zip(target, weights_, lower, upper):
        if t.ndim != 1 or any(a.shape != t.shape for a in (w, lo, hi)):
            raise ValueError("each carriageway's t, w, lo and hi must have the same 1D shape")
    parameters = np.asarray([(float(c["grade"]), float(c["K"]), float(c.get("lam", LAMBDA)),
                              float(c.get("step", STEP_M)), float(c.get("length", LENGTH_BIAS)))
                             for c in chains], np.float64).reshape(-1, 5)
    if any(len(c) not in (6, 9) or c[4] not in ("eq", "ge", "le") for c in cp):
        raise ValueError("carriageway coupling kind must be eq, ge or le, with no third sample or one and its alpha")
    kinds = {"eq": 0, "ge": 1, "le": 2}
    if three:
        rows = np.asarray([tuple(c[:4]) + (kinds[c[4]],) + (tuple(c[6:8]) if len(c) == 9 else (-1, 0)) for c in cp],
                          np.int64).reshape(-1, 7)
        alpha = np.asarray([float(c[8]) if len(c) == 9 else 0.0 for c in cp], np.float64)
    else:
        rows = np.asarray([tuple(c[:4]) + (kinds[c[4]],) for c in cp], np.int64).reshape(-1, 5)
        alpha = None
    values = np.asarray([c[5] for c in cp], np.float64)
    own = [c.get("ds") for c in chains]
    spacing = None
    if any(d is not None for d in own):
        fast.require(309, "carriageway_profiles spacing")
        pieces = []
        for c, t, d in zip(chains, target, own):
            if d is None:
                pieces.append(np.zeros(len(t)))             # (0: the chain's step)
                continue
            d = np.asarray(d, np.float64)
            if d.shape != (max(len(t) - 1, 0),):
                raise ValueError("each carriageway's ds must have one distance per segment")
            pieces.append(np.r_[np.maximum(d, MIN_SEGMENT_M), 0.0][:len(t)])
        spacing = pack(pieces)
    budget = float(deadline) - time.monotonic() if deadline is not None else BUDGET_S
    if np.isnan(budget):
        raise ValueError("carriageway deadline must not be NaN")
    budget = max(0.0, budget)
    import xproc
    left = xproc.deadline_left()        # (a pipeline step's time limit is the kernel's too: its own budget never goes past it)
    if left is not None:
        budget = max(0.0, min(budget, left))
    z, info = fast._ef.carriageway_profiles(pack(target), pack(weights_), pack(lower), pack(upper), offsets,
                                           parameters, rows, values, rounds, budget,
                                           *(() if alpha is None and spacing is None else (alpha,)),
                                           **({} if spacing is None else {"spacing": spacing}))
    if diagnostics is not None:
        diagnostics.update(info)
    if (not info["converged"] or not info["feasible"]) and not getattr(PROBE, "on", False):
        log.warning("carriageways: %d chains, %d couplings: %s, %d iterations, converged=%s feasible=%s budget=%s; "
                    "grade excess %.6f, curve excess %.6f, bounds %.3f/%.3f m, junction %.3f m, clearance %.3f m",
                    len(chains), len(cp), info["status"], info["rounds"], info["converged"], info["feasible"], info["timed_out"],
                    info["grade_excess"], info["curve_excess"], info["lower_shortfall_m"], info["upper_excess_m"],
                    info["junction_gap_m"], info["clearance_shortfall_m"])
    return [z[a:b] for a, b in zip(offsets[:-1], offsets[1:])]
