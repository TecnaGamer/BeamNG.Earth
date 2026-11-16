"""One vertical solve for a cell's graded ground roads (docs/VERTICAL_SOLVER_RFC.md, step 2: the shadow solve).

Today a road's heights are the result of a chain of passes over Plan.z (grading._Planner._make_cell_roads), each
changing it in place in a fixed order. Here the continuous rules are solved together, in one constrained
least-squares problem on the carriageway solver's kernel (carriageways.solve: earthfast's Clarabel QP):

- objective: stay on the profile the elevation gives (plan_roads' heights, before any pass: its smoothing, the
  lidar's and the decks' corrections in it), a higher-ranked road more firmly (the road that keeps its profile at a
  junction, as snap_junctions), smooth (second differences: a change spreads over SPREAD_M of the road);
- hard rows: a road's end at another road (its end or its middle, JOIN_M) at one height with it; a road's end on a
  bridge at the deck's height; the cut ends of a cell's margin at their profile (the next cell sees the same);
  under a deck, no higher than the passes' dip (whether to dip and how deep stays the passes' rule, RFC "what stays
  heuristic"); a grade ceiling per road (its class's, or its ground's own where that is steeper: a steep street).

BNE_VSOLVE=shadow runs it beside the passes: their plans are used and kept as always; the solve's are only kept
in memory for tools/grade_metrics.py to measure (Planner._vsolve). Off by default, and off it costs nothing.
Pure: the same plans in give the same heights out.
"""
import logging
import os
import time

import numpy as np

log = logging.getLogger("earthd.vsolve")

MODE = os.environ.get("BNE_VSOLVE", "off").strip().lower()
SHADOW = MODE == "shadow"

JOIN_M = 1.0            # m: a road's end this close to another road's centre line meets it there (grademetrics.JUNCTION_R_M)
JOIN_DZ_M = 6.0         # m: ends whose profiles differ more meet at no node (two levels, or bad data: grading.JUNCTION_MAX_M)
SPREAD_M = {"motorway": 120.0, "trunk": 120.0, "primary": 80.0, "secondary": 60.0, "tertiary": 50.0}
SPREAD_OTHER_M = 40.0   # m over which a forced change spreads (grading.JUNCTION_TAPER_M)
RANK_W = 2.0            # each class up holds its profile this many times more firmly
RANK_STEPS = 6.0        # classes at most (the weights' spread: the solve's conditioning)
DIP_M = 0.3             # m: a sample the passes lowered this much under a deck is a dip (its depth an upper bound)
DIP_REACH_M = 2.0       # m beyond the deck's half width a sample is under it
GRADE_SLACK = 0.01      # over the ground's own steepest: the ceiling of a road steeper than its class
GRADE_LOOSE = 0.60      # the ceiling when the class's made the solve infeasible (reported)
K_LOOSE = 0.3           # m per % of grade change: a loose curvature ceiling (the smoothing does the shaping)
BUDGET_S = float(os.environ.get("BNE_VSOLVE_BUDGET_S", "60"))


def _arc(xy):
    return np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]


def _grade_10m(xy, z, window=10.0):
    """The steepest grade over `window` m of a profile (0 when shorter)."""
    s = _arc(xy)
    if len(s) < 2 or s[-1] < window:
        return 0.0
    j = np.searchsorted(s, s + window)
    ok = j < len(s)
    if not ok.any():
        return 0.0
    i = np.flatnonzero(ok)
    return float(np.max(np.abs(z[j[ok]] - z[i]) / (s[j[ok]] - s[i])))


def _cls(spec):
    return (getattr(spec, "cls", None) or "").removesuffix("_link")


def joins(xys, z0, r=JOIN_M, dz=JOIN_DZ_M):
    """[(a, ia, b, ib)]: plan a's end sample ia (0 or -1) meets plan b at its sample ib (the nearest), each pair of
    ends once, crossings no nodes. xys: per plan (n, 2); z0: per plan its profile (n,), whose difference over dz says
    two levels. Pure."""
    import grademetrics as gm
    seg = gm._Segments(xys)
    out, seen = [], set()
    for a, xy in enumerate(xys):
        if len(xy) < 2:
            continue
        for ia in (0, len(xy) - 1):
            p = xy[ia]
            for b, (dist, sj, t) in seg.near(p, r).items():
                if b == a or dist > r:
                    continue
                q = xys[b]
                ib = sj if t < 0.5 else sj + 1
                if ib in (0, len(q) - 1):
                    k = tuple(sorted(((a, ia), (b, ib))))
                    if k in seen:
                        continue
                    seen.add(k)
                if abs(float(z0[a][ia]) - float(z0[b][ib])) > dz:
                    continue
                out.append((a, ia, b, ib))
    return out


def problem(plans, specs, z0, ceilings, ranks, pins=(), dips=None):
    """(chains, couplings) for carriageways.solve. plans: [Plan] (xy, z: the passes' heights, abut); z0: per plan the
    profile before the passes; ceilings: per plan its class's grade ceiling (None: no ceiling); ranks: per plan its
    road_rank; pins: (plan, sample, height) held; dips: per plan (n,) upper bounds (NaN: none) or None."""
    xys = [np.asarray(p.xy, np.float64)[:, :2] for p in plans]
    rk = np.asarray(ranks, np.float64)
    r0 = float(rk.min()) if len(rk) else 0.0
    chains = []
    held = {}
    for (a, i, h) in pins:
        held[(a, i if i >= 0 else len(xys[a]) + i)] = float(h)
    for n, (p, sp, z) in enumerate(zip(plans, specs, z0)):
        xy = xys[n]
        z = np.asarray(z, np.float64)
        m = len(z)
        step = float(np.median(np.hypot(*np.diff(xy, axis=0).T))) if m > 1 else 2.0
        step = max(step, 0.25)
        spread = SPREAD_M.get(_cls(sp), SPREAD_OTHER_M)
        hold = RANK_W ** min(RANK_STEPS, (rk[n] - r0) / 2.0)     # (road_rank: 2 per class, a link 1 under)
        lam = hold * (spread / (2.0 * step)) ** 4      # (the rank scales both: the same spread, a firmer hold at nodes)
        w = np.full(m, hold)
        lo = np.full(m, -np.inf)
        hi = np.full(m, np.inf)
        if dips is not None and dips[n] is not None:
            hi = np.where(np.isfinite(dips[n]), dips[n], hi)
        for e in (0, m - 1):
            if p.abut[0 if e == 0 else 1]:
                held[(n, e)] = float(np.asarray(p.z, np.float64)[e])   # (the deck's height at its abutment)
        ceil = ceilings[n]
        g = 10.0 if ceil is None else max(float(ceil), _grade_10m(xy, z) + GRADE_SLACK)
        for (a, i), h in held.items():
            if a == n:
                lo[i] = hi[i] = h
                w[i] = 0.0
        chains.append({"t": z, "w": w, "lo": lo, "hi": hi, "grade": g, "K": K_LOOSE, "lam": lam, "step": step,
                       "length": 0.0})
    couplings = []
    for a, ia, b, ib in joins(xys, z0):
        ka, kb = (a, ia if ia >= 0 else len(xys[a]) + ia), (b, ib)
        if ka in held and kb in held:
            continue                    # (both held: their heights are given, a difference there is the data's)
        couplings.append((a, ka[1], b, ib, "eq", 0.0))
    return chains, couplings


def solve(plans, specs, z0, ceilings, ranks, pins=(), dips=None, budget=BUDGET_S):
    """(heights per plan, info): the one solve of these plans (problem's arguments). info: the kernel's diagnostics
    plus n (unknowns), joins, loosened (the class ceilings were dropped to make it feasible), elapsed_s."""
    import carriageways
    t0 = time.perf_counter()
    chains, couplings = problem(plans, specs, z0, ceilings, ranks, pins, dips)
    info = {"n": int(sum(len(c["t"]) for c in chains)), "joins": len(couplings), "loosened": False}
    if not chains:
        info["elapsed_s"] = 0.0
        return [], info
    diag = {}
    zs = carriageways.solve(chains, couplings, deadline=time.monotonic() + budget, diagnostics=diag)
    if not (diag.get("converged") and diag.get("feasible")):
        for c in chains:
            c["grade"] = max(c["grade"], GRADE_LOOSE)
        diag2 = {}
        zs2 = carriageways.solve(chains, couplings, deadline=time.monotonic() + budget, diagnostics=diag2)
        if diag2.get("converged") and diag2.get("feasible"):
            zs, diag = zs2, diag2
            info["loosened"] = True
    info.update({k: (v if isinstance(v, (bool, int, float, str)) else str(v)) for k, v in diag.items()})
    info["elapsed_s"] = time.perf_counter() - t0
    return [np.asarray(z, np.float64) for z in zs], info


def dips_under(plans, z0, decks, dip=DIP_M, reach=DIP_REACH_M):
    """Per plan (n,) upper bounds (NaN: none): its samples under a deck (decks: [(line (k, 2), half width)]) that the
    passes lowered by dip or more, at the passes' height (the dip's depth is the passes' rule: here its bound)."""
    import grademetrics as gm
    out = [None] * len(plans)
    if not decks:
        return out
    seg = gm._Segments([np.asarray(d[0], np.float64)[:, :2] for d in decks])
    halves = [float(d[1]) for d in decks]
    reach_all = max(halves) + reach + max((float(p.half) for p in plans), default=0.0)
    for n, p in enumerate(plans):
        z = np.asarray(p.z, np.float64)
        low = np.flatnonzero(np.asarray(z0[n], np.float64) - z >= dip)
        if not len(low):
            continue
        xy = np.asarray(p.xy, np.float64)[:, :2]
        b = np.full(len(z), np.nan)
        for i in low.tolist():
            for j, (dist, _s, _t) in seg.near(xy[i], reach_all).items():
                if dist <= halves[j] + reach + float(p.half):
                    b[i] = z[i]
                    break
        if np.isfinite(b).any():
            out[n] = b
    return out
