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
GRADE_ROW_M = 0.04      # a ceiling's rows where the profile comes this near it (a smooth correction stays under elsewhere)
GRADE_ROUNDS = 4        # solves at most, each adding the rows its heights broke (an active set)
GRADE_ROW_REACH = 10    # segments either side of a broken one that get rows too (the next round's excess beside it)
GRADE_D = 10.0          # the kernel's own grade bound, on the corrections: none
K_LOOSE = 0.05          # m per % of grade change: the kernel's curvature bound, on the corrections: next to none
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


def _ceiling(ceil, xy, z0, floor=None):
    """A road's grade ceiling: its class's, its ground's own steepest where that is steeper (a steep street), floor."""
    g = max(float(ceil), _grade_10m(xy, z0) + GRADE_SLACK)
    return g if floor is None else max(g, float(floor))


def _steepest_step(p):
    """The steepest grade between neighbouring samples of plan p's heights."""
    xy = np.asarray(p.xy, np.float64)[:, :2]
    if len(xy) < 2:
        return 0.0
    return float(np.max(np.abs(np.diff(np.asarray(p.z, np.float64))) / np.maximum(np.hypot(*np.diff(xy, axis=0).T),
                                                                                  1e-6)))


def _cls(spec):
    return (getattr(spec, "cls", None) or "").removesuffix("_link")


def joins(xys, z0, r=JOIN_M, dz=JOIN_DZ_M):
    """[(a, ia, b, ib, sj, t)]: plan a's end sample ia (0 or -1) meets plan b at its sample ib (the nearest), at t
    along its segment sj, each pair of ends once, crossings no nodes. xys: per plan (n, 2); z0: per plan its profile (n,), whose difference over dz says
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
                out.append((a, ia, b, ib, sj, t))
    return out


def nodes(xys, z0, r=JOIN_M, dz=JOIN_DZ_M):
    """The junctions (joins' pairs grouped, union-find): per node {(plan, sample): offset}, the height of the node
    being the sample's profile plus its offset (0 for a road's end; where an end lands between a road's samples, that
    road's profile there less its nearest sample's: on a 15 % road the nearest sample alone missed by up to 15 cm,
    Lauterbrunnen). A sample several ends land by: their offsets' mean. Pure."""
    parent = {}

    def find(k):
        while parent.setdefault(k, k) != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k
    offs = {}
    for a, ia, b, ib, sj, t in joins(xys, z0, r, dz):
        ka = (a, ia if ia >= 0 else len(xys[a]) + ia)
        kb = (b, ib)
        if ib in (0, len(xys[b]) - 1):
            off = 0.0                   # (an end on an end: the same point)
        else:
            zb = float(z0[b][sj] + t * (z0[b][sj + 1] - z0[b][sj]))
            off = zb - float(z0[b][ib])
        offs.setdefault(ka, []).append(0.0)
        offs.setdefault(kb, []).append(off)
        ra, rb = find(ka), find(kb)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    out = {}
    for k, o in offs.items():
        out.setdefault(find(k), {})[k] = float(np.mean(o))
    return [m for _r, m in sorted(out.items()) if len(m) > 1]


def problem(plans, specs, z0, ceilings, ranks, pins=(), dips=None, grade_rows=True, extra=None, floors=None):
    """(chains, couplings) for carriageways.solve, whose unknowns are the corrections d = z - z0 (its smoothing on
    them, not on the heights: a road no row moves keeps its profile exactly, and a change is spread along it). plans:
    [Plan] (xy, z: the passes' heights, abut); z0: per plan the profile before the passes; ceilings: per plan its class's
    grade ceiling (None: none); ranks: per plan its road_rank; pins: (plan, sample, height) held; dips: per plan (n,)
    upper bounds (NaN: none) or None; grade_rows: the ceilings as rows (off: the retry of an infeasible solve); extra:
    {plan: segments} that get the ceiling's rows too (a solve's own excess: solve's rounds); floors: per plan a grade
    its ceiling is at least (the passes' own steepest: solve's middle retry) or None."""
    xys = [np.asarray(p.xy, np.float64)[:, :2] for p in plans]
    z0 = [np.asarray(z, np.float64) for z in z0]
    rk = np.asarray(ranks, np.float64)
    r0 = float(rk.min()) if len(rk) else 0.0
    chains, couplings = [], []
    held = {}
    for (a, i, h) in pins:
        held[(a, i if i >= 0 else len(xys[a]) + i)] = float(h)
    for n, p in enumerate(plans):
        for e in (0, len(xys[n]) - 1):
            if p.abut[0 if e == 0 else 1]:
                held[(n, e)] = float(np.asarray(p.z, np.float64)[e])   # (the deck's height at its abutment)
    for n, (sp, z) in enumerate(zip(specs, z0)):
        xy = xys[n]
        m = len(z)
        ds = np.hypot(*np.diff(xy, axis=0).T) if m > 1 else np.zeros(0)
        step = max(float(np.median(ds)) if m > 1 else 2.0, 0.25)
        spread = SPREAD_M.get(_cls(sp), SPREAD_OTHER_M)
        hold = RANK_W ** min(RANK_STEPS, (rk[n] - r0) / 2.0)     # (road_rank: 2 per class, a link 1 under)
        lam = hold * (spread / (2.0 * step)) ** 4      # (the rank scales both: the same spread, a firmer hold at nodes)
        w = np.full(m, hold)
        lo = np.full(m, -np.inf)
        hi = np.full(m, np.inf)
        if dips is not None and dips[n] is not None:
            hi = np.where(np.isfinite(dips[n]), dips[n] - z, hi)
        for (a, i), h in held.items():
            if a == n:
                lo[i] = hi[i] = h - z[i]
                w[i] = 0.0
        chains.append({"t": np.zeros(m), "w": w, "lo": lo, "hi": hi, "grade": GRADE_D, "K": K_LOOSE, "lam": lam,
                       "step": step, "length": 0.0})
        ceil = ceilings[n]
        if not grade_rows or ceil is None or m < 2:
            continue
        # (the ceiling on z = z0 + d, as rows on d, where z0 comes near it: elsewhere a smooth correction stays under)
        dz0 = np.diff(z)
        g = _ceiling(ceil, xy, z, None if floors is None else floors[n])
        lim = g * np.maximum(ds, 1e-6)
        rows_at = set(np.flatnonzero(np.abs(dz0) >= lim - GRADE_ROW_M * np.maximum(ds, 1e-6)).tolist())
        if extra is not None:
            rows_at |= {i for i in extra.get(n, ()) if 0 <= i < m - 1}
        for i in sorted(rows_at):
            # z[i+1] - z[i] <= lim:  d[i] - d[i+1] >= dz0 - lim;  >= -lim:  d[i+1] - d[i] >= -lim - dz0
            couplings.append((n, i, n, i + 1, "ge", float(dz0[i] - lim[i])))
            couplings.append((n, i + 1, n, i, "ge", float(-lim[i] - dz0[i])))
    n_joins = 0
    for members in nodes(xys, z0):
        # (each node one height: every member tied to one root, a star with no loops. Tied pair by pair, three ends
        # meeting at a side road's mouth formed a loop whose offsets did not add up: 13 of Tokyo's 15 cells infeasible)
        keys = sorted(members)
        root = next((k for k in keys if k in held), keys[0])
        hr = float(z0[root[0]][root[1]]) + members[root]
        for k in keys:
            if k == root or (k in held and root in held):
                continue                # (both held: their heights are given, a difference there is the data's)
            # z_k + off_k = z_root + off_root:  d_k - d_root = v, as two rows (the kernel's equality holds d_a = d_b)
            v = hr - (float(z0[k[0]][k[1]]) + members[k])
            couplings.append((k[0], k[1], root[0], root[1], "ge", v))
            couplings.append((root[0], root[1], k[0], k[1], "ge", -v))
            n_joins += 1
    return chains, couplings, n_joins


def solve(plans, specs, z0, ceilings, ranks, pins=(), dips=None, budget=BUDGET_S):
    """(heights per plan, info), or (None, info) when no solve was feasible (the passes' plans stand: the RFC's
    fallback). info: the kernel's diagnostics plus n (unknowns), joins, loosened (solved only without the grade rows),
    fallback, elapsed_s."""
    import carriageways
    t0 = time.perf_counter()
    info = {"n": int(sum(len(z) for z in z0)), "loosened": False, "fallback": False, "grade_left": False}
    if not plans:
        info.update(joins=0, elapsed_s=0.0)
        return [], info
    out = None
    passes_floor = [_steepest_step(p) + GRADE_SLACK for p in plans]
    for stage, (rows, floors) in enumerate(((True, None), (True, passes_floor), (False, None))):
        extra = {}
        for _round in range(GRADE_ROUNDS if rows else 1):
            chains, couplings, n_joins = problem(plans, specs, z0, ceilings, ranks, pins, dips, grade_rows=rows,
                                                 extra=extra, floors=floors)
            diag = {}
            ds = carriageways.solve(chains, couplings, deadline=time.monotonic() + budget, diagnostics=diag)
            info.update({k: (v if isinstance(v, (bool, int, float, str)) else str(v)) for k, v in diag.items()})
            info["joins"] = n_joins
            info["rounds_grade"] = _round + 1
            if not (diag.get("converged") and diag.get("feasible")):
                info["grade_left"] = out is not None    # (an earlier round's heights kept, some over their ceiling)
                break
            out = [np.asarray(z, np.float64) + np.asarray(d, np.float64) for z, d in zip(z0, ds)]
            if not rows or not _over_ceiling(plans, out, z0, ceilings, extra, floors):
                break
        if out is not None:
            info["stage"] = stage           # (0: the class ceilings; 1: no lower than the passes' own; 2: no grade rows)
            info["loosened"] = stage > 0
            break
    info["fallback"] = out is None
    info["elapsed_s"] = time.perf_counter() - t0
    return out, info


def _over_ceiling(plans, zs, z0, ceilings, extra, floors=None, slack=1e-3, reach=GRADE_ROW_REACH):
    """The segments where heights zs break their plan's ceiling (as problem's rows put it) and have no row yet, with
    `reach` segments either side, added to extra {plan: set}; how many were new."""
    new = 0
    for n, (p, z, b) in enumerate(zip(plans, zs, z0)):
        ceil = ceilings[n]
        if ceil is None or len(z) < 2:
            continue
        xy = np.asarray(p.xy, np.float64)[:, :2]
        ds = np.maximum(np.hypot(*np.diff(xy, axis=0).T), 1e-6)
        g = _ceiling(ceil, xy, np.asarray(b, np.float64), None if floors is None else floors[n])
        bad = np.flatnonzero(np.abs(np.diff(z)) > g * ds + slack)      # (z: this solve's heights)
        if not len(bad):
            continue
        got = extra.setdefault(n, set())
        for i in bad.tolist():
            for j in range(i - reach, i + reach + 1):
                if j not in got:
                    got.add(j)
                    new += 1
    return new


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
