"""One vertical solve for a cell's graded ground roads (docs/VERTICAL_SOLVER_RFC.md, step 2: the shadow solve).

Today a road's heights are the result of a chain of passes over Plan.z (grading._Planner._make_cell_roads), each
changing it in place in a fixed order. Here the continuous rules are solved together, in one constrained
least-squares problem on the carriageway solver's kernel (carriageways.solve: earthfast's Clarabel QP):

- objective: stay on the profile the elevation gives (plan_roads' heights, before any pass: its smoothing, the
  lidar's and the decks' corrections in it), a higher-ranked road more firmly (the road that keeps its profile at a
  junction, as snap_junctions), the changes smooth (second differences of the corrections from the profile: a road
  no row moves keeps its profile, a change spreads over SPREAD_M of it);
- hard rows: a road's end at another road (its end or its middle, JOIN_M) at one height with it; a road's end on a
  bridge at the deck's height; the cut ends of a cell's margin at their profile (the next cell sees the same);
  under a deck, no higher than the underpass rule's dip (grading._lower_underpasses, run over the whole solve: whether
  to dip and how deep stays a rule, RFC "what stays heuristic");
- soft rows: under a deck, a road no higher than the underpass rule left it (what clears there, a dip or the deck's
  lift over it, was made against those heights: the solve may lower it, not raise it into the deck; raised by a
  junction's tie it put Tokyo's roads through 8 decks where the passes had 6); onto a deck, a road no lower than the
  approach rule raised it (grading._lift_approaches: the deck's need, not the road's); a grade ceiling per road (its class's, or its ground's own where that is steeper: a steep street), met
  wherever the hard rows leave room and broken by the least where they don't (earthfast 489's soft couplings: an
  exact L1 penalty). Hard, one ceiling that could not hold made the whole cell's solve infeasible, and the cell
  was solved again with looser ceilings, then none: three stages, in place of a row that says where it breaks;
  roads side by side at one height, the difference between two what their gap holds as a bank (grading's
  share_corridors' rule, on its pairs: corridor rows, where a solve's heights broke it, round by round).

BNE_VSOLVE=shadow runs it beside the passes: their plans are used and kept as always; the solve's are only kept
in memory for tools/grade_metrics.py to measure (Planner._vsolve). BNE_VSOLVE=on (the RFC's step 3) runs it the
same and uses its heights for the cell's ground roads, the passes' where it falls back; its plans under their own cache
key (plancache.plans_format). On by default (the RFC's step 4) where the native kernel has the solve (KERNEL_MIN);
off, as before, on an older build. BNE_VSOLVE=off: the passes alone, and off it costs nothing.
Pure: the same plans in give the same heights out.
"""
import logging
import os
import time

import numpy as np

import dnp

log = logging.getLogger("earthd.vsolve")

KERNEL_MIN = 490        # earthfast's VERSION with the solve's soft couplings (489) and corridor pairs (490)


def _default_mode():
    """BNE_VSOLVE unset: "on" where the native kernel has the solve (the RFC's step 4), "off" on an older build. Off
    there rather than on and failing in every cell: the plans are the passes' either way, but on they were cached under
    the solve's key (plancache.plans_format) and kept after the kernel was rebuilt."""
    try:
        import fast
        version = int(getattr(getattr(fast, "_ef", None), "VERSION", 0) or 0)
    except Exception:
        version = 0
    return "on" if version >= KERNEL_MIN else "off"


MODE = (os.environ.get("BNE_VSOLVE") or "").strip().lower() or _default_mode()
SOLO = MODE == "solo"               # (an experiment: the solve's heights, from the rules alone: the passes it replaces
                                    # skipped, docs/research/2026-11-17-vsolve-solo.md)
ON = MODE == "on" or SOLO           # (the solve's heights used)
SHADOW = MODE == "shadow" or ON     # (the solve run, beside the passes or in their place)

JOIN_M = 1.0            # m: a road's end this close to another road's centre line meets it there (grademetrics.JUNCTION_R_M)
JOIN_END_M = 2.5        # m: an end no road comes within JOIN_M of meets another road's end this close: the same junction,
                        # drawn a little off (a Fells Point slip road's last vertex 2 m short of its junction in one cell's
                        # plans, where the next cell's ended on it: held at its profile there, free in the other, a 0.36 m
                        # seam between them)
JOIN_REACH_M = 3.0      # m past a road's carriageway edge an end no road comes within JOIN_M of, nor its end within
                        # JOIN_END_M, still meets it, at its nearest point (grading.JUNCTION_REACH_M, the passes' snap:
                        # Silistra's track ended 1.6 m off the next one's centre line, so the solve held it at its
                        # profile, 3.4 m over the other where the dip under the bridge took that one down). -inf: none
REACH_ON = os.environ.get("BNE_VSOLVE_REACH", "off") == "on"   # the reach in the solve's joins: off by default. On, it
                        # took Silistra's 3.4 m step away (drivability 5.8 -> 1.4), but Tokyo's cells joined ends
                        # differently: 4 seams over 30 cm (up to 1.87 m, 0 without), with the limits below as well
JOIN_REACH_DZ_M = 0.5   # m: ... only an end whose profile is this near that road's there (more: likely two levels; at
                        # any difference up to JOIN_DZ_M, Tokyo's cells joined ends differently, 4 seams up to 1.87 m);
                        # and never an end cut at the cell's box, which is no end of its road
JOINT_FLAT_M = 12.0     # m: a junction no higher than the deepest dip any of its roads has this near it (soft, a cap:
                        # grading.UNDER_JOINT_FLAT_M, the underpass rule's; a Vienna Südtiroler Platz junction of four
                        # primary roads stayed at its profile, held by all four, 0.27 m over the dip on one of them 4 m
                        # on, where the passes took the junction down with it)
JOIN_DZ_M = 6.0         # m: ends whose profiles differ more meet at no node (two levels, or bad data: grading.JUNCTION_MAX_M)
SPREAD_M = {"motorway": 120.0, "trunk": 120.0, "primary": 80.0, "secondary": 60.0, "tertiary": 50.0}
SPREAD_OTHER_M = 40.0   # m over which a forced change spreads (grading.JUNCTION_TAPER_M)
RANK_W = 2.0            # each class up holds its profile this many times more firmly
RANK_STEPS = 6.0        # classes at most (the weights' spread: the solve's conditioning)
DIP_M = 0.3             # m: a sample the underpass rule lowered this much is a dip (its depth an upper bound)
GRADE_SLACK = 0.01      # over the ground's own steepest: the ceiling of a road steeper than its class
GRADE_ROW_M = 0.04      # a ceiling's rows where the profile comes this near it (a smooth correction stays under elsewhere)
GRADE_ROWS_ON = os.environ.get("BNE_VSOLVE_GRADE", "on").strip().lower() not in ("0", "off", "false", "no")
GRADE_PENALTY = 100.0   # a grade row's cost per m it is broken by, times its road's stiffness (hold x samples over its
                        # spread: a ceiling's multiplier where it can hold is under that, so it holds as a hard one)
CLEAR_PENALTY = 10 * GRADE_PENALTY    # under a deck (caps): a road through a deck is worse than a steep one
GRADE_ROUNDS = 4        # solves at most, each adding the rows its heights broke (an active set)
GRADE_ROW_REACH = 10    # segments either side of a broken one that get rows too (the next round's excess beside it)
GRADE_D = 10.0          # the kernel's own grade bound, on the corrections: none
K_LOOSE = 0.001         # m per % of grade change: the kernel's curvature bound, on the corrections: none (at 0.05,
                        # 0.8 m per 2 m step: a dip held beside a held sample made cells infeasible)
BUDGET_S = float(os.environ.get("BNE_VSOLVE_BUDGET_S", "60"))
# Roads side by side at one height (grading.share_corridors' rule, its pairs from grading.corridor_pairs): two heights
# beside each other differ by at most what the gap between the carriageways holds at CORRIDOR_BANK (none where the
# pavements overlap), as soft rows where a solve's heights broke it (an active set, as the grade's). Not between two
# heights over CORRIDOR_MAX_DIFF_M apart (two levels: a road dipped under a deck beside the ramp up to it). Without
# them the solve stood a Rio street 0.8 m through a deck the passes' corridors had lowered it under. One-sided, as
# the pass: the road that gives way (share_corridors' keys: the slip road, the lower class; between equals the lower
# one, up) is held within what the gap holds of its neighbour's rule height (its profile after the dips and the
# floors), a fixed target, not of its neighbour's solved height. Tied two-sided to the solved heights, a road followed
# its neighbour down and the next road it, Athens' ground p95 1.58 -> 2.20 m in solo mode; one-sided, 1.69 m, and
# Rio's street under its deck (docs/research/2026-11-18-vsolve-corridors.md). BNE_VSOLVE_CORRIDOR=off: none
CORRIDOR_ROWS_ON = os.environ.get("BNE_VSOLVE_CORRIDOR", "on").strip().lower() in ("1", "on", "true", "yes")
CORRIDOR_BANK = 0.4          # m per m of gap (grading.CORRIDOR_BANK)
CORRIDOR_MAX_DIFF_M = 3.0    # m (grading.CORRIDOR_MAX_DIFF_M)
CORRIDOR_SLACK_M = 0.01      # m a pair's heights may break it by before it gets rows
CORRIDOR_PENALTY = GRADE_PENALTY     # per m broken, times the stiffer road's stiffness (a grade row's)


def _arc(xy):
    return np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]


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


def _cls(spec):
    return (getattr(spec, "cls", None) or "").removesuffix("_link")


def joins(xys, z0, r=JOIN_M, dz=JOIN_DZ_M, r_end=None, halves=None, cut=()):
    """[(a, ia, b, ib, sj, t)]: plan a's end sample ia (0 or -1) meets plan b at its sample ib (the nearest), at t
    along its segment sj, each pair of ends once, crossings no nodes; an end no line comes within r of, another plan's
    end within r_end (JOIN_END_M by default; at that end: t 0); an end that meets neither, the nearest line within its
    road's half width (halves: per plan, m; None: none) and JOIN_REACH_M of it, its profile within JOIN_REACH_DZ_M of
    that line's; cut: the (plan, sample) ends cut at the box (no end of the road: no reach). xys: per plan (n, 2); z0:
    per plan its profile (n,), whose difference over dz says two levels. Pure."""
    import grademetrics as gm
    r_end = JOIN_END_M if r_end is None else r_end
    seg = gm._Segments(xys)
    far = max(halves) + JOIN_REACH_M if halves is not None and len(halves) else 0.0
    out, seen = [], set()
    for a, xy in enumerate(xys):
        if len(xy) < 2:
            continue
        for ia in (0, len(xy) - 1):
            p = xy[ia]
            hits = {b: h for b, h in seg.near(p, r).items() if b != a and h[0] <= r}
            if not hits and r_end > r:
                for b, (dist, sj, t) in seg.near(p, r_end).items():
                    q = xys[b]
                    if b == a or len(q) < 2:
                        continue
                    for ib in (0, len(q) - 1):
                        if float(dnp.hypot(*(q[ib] - p))) <= r_end:
                            hits[b] = (0.0, ib if ib == 0 else ib - 1, 0.0 if ib == 0 else 1.0)
            if not hits and far > r and (a, ia) not in cut:
                near = [(h[0] - halves[b], b, h) for b, h in seg.near(p, far).items()
                        if b != a and len(xys[b]) > 1 and h[0] <= halves[b] + JOIN_REACH_M
                        and abs(float(z0[a][ia]) - _at(z0[b], h[1], h[2])) <= JOIN_REACH_DZ_M]
                if near:
                    _e, b, (_d, sj, t) = min(near, key=lambda v: v[0])
                    hits[b] = (0.0, sj, t)
            for b, (dist, sj, t) in hits.items():
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


def _halves(plans):
    """Per plan its road's half width for joins' reach, or None: the reach off (REACH_ON)."""
    return [float(getattr(p, "road_half", 0.0) or 0.0) for p in plans] if REACH_ON else None


def _at(z, sj, t):
    """z (n,) at t along its segment sj."""
    z = np.asarray(z, np.float64)
    return float(z[sj] + t * (z[min(sj + 1, len(z) - 1)] - z[sj]))


def nodes(xys, z0, r=JOIN_M, dz=JOIN_DZ_M, halves=None, cut=()):
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
    for a, ia, b, ib, sj, t in joins(xys, z0, r, dz, halves=halves, cut=cut):
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


def problem(plans, specs, z0, ceilings, ranks, pins=(), dips=None, grade_rows=True, extra=None, drop=(), caps=None,
            floors=None, corridors=None, rows=()):
    """(chains, couplings) for carriageways.solve, whose unknowns are the corrections d = z - z0 (its smoothing on
    them, not on the heights: a road no row moves keeps its profile exactly, and a change is spread along it). plans:
    [Plan] (xy, z: the passes' heights, abut); z0: per plan the profile before the passes; ceilings: per plan its class's
    grade ceiling (None: none); ranks: per plan its road_rank; pins: (plan, sample, height) held; dips: per plan (n,)
    upper bounds (NaN: none) or None; grade_rows: the ceilings as soft rows; extra: {plan: segments} that get the
    ceiling's rows too (a solve's own excess: solve's rounds), and gets every segment given one here; drop: kinds of
    rows left out ("pins", "abut", "dips", "nodes", "caps": _culprit's tests); caps: per plan (n,) the most its heights
    may be (NaN: no limit), soft rows, or None; floors: the same, the least they may be; corridors: solve's bands
    (j, i, target, lim arrays: plan j's height at sample i within lim of target), rows: the indices of those that get
    rows."""
    if "caps" in drop:
        caps = floors = None
    if "pins" in drop:
        pins = ()
    if "dips" in drop:
        dips = None
    xys = [np.asarray(p.xy, np.float64)[:, :2] for p in plans]
    z0 = [np.asarray(z, np.float64) for z in z0]
    rk = np.asarray(ranks, np.float64)
    r0 = float(rk.min()) if len(rk) else 0.0
    chains, couplings = [], []
    stiff = np.zeros(len(plans))
    node_list = nodes(xys, z0, halves=_halves(plans),
                      cut={(a, i) for a, i, _h in pins}) if "nodes" not in drop else []
    in_node = {k for m in node_list for k in m}
    held = {}
    for (a, i, h) in pins:
        k = (a, i if i >= 0 else len(xys[a]) + i)
        if k not in in_node:    # (a cut end on a junction: the node ties it; held at its profile as well, beside a road
            held[k] = float(h)  # the underpass rule dipped there, it made two of Tokyo's cells infeasible)
    for n, p in enumerate(plans):
        for e in (0, len(xys[n]) - 1):
            if p.abut[0 if e == 0 else 1] and "abut" not in drop:
                held[(n, e)] = float(np.asarray(p.z, np.float64)[e])   # (the deck's height at its abutment)
    for n, (sp, z) in enumerate(zip(specs, z0)):
        xy = xys[n]
        m = len(z)
        ds = dnp.hypot(*np.diff(xy, axis=0).T) if m > 1 else np.zeros(0)
        step = max(float(np.median(ds)) if m > 1 else 2.0, 0.25)
        spread = SPREAD_M.get(_cls(sp), SPREAD_OTHER_M)
        hold = dnp.power(RANK_W, min(RANK_STEPS, (rk[n] - r0) / 2.0))     # (road_rank: 2 per class, a link 1 under)
        lam = hold * dnp.power(spread / (2.0 * step), 4)      # (the rank scales both: the same spread, a firmer hold at nodes)
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
        stiff[n] = hold * max(spread / step, 1.0)
        pen = CLEAR_PENALTY * stiff[n]
        for bound, kind in ((caps, "le"), (floors, "ge")):
            if bound is not None and bound[n] is not None:
                for i in np.flatnonzero(np.isfinite(bound[n])).tolist():
                    if lo[i] != hi[i]:      # (a held sample: its height is given)
                        couplings.append((n, i, None, 0, kind, float(bound[n][i] - z[i]), pen))
        ceil = ceilings[n]
        if not grade_rows or ceil is None or m < 2:
            continue
        # (the ceiling on z = z0 + d, as rows on d, where z0 comes near it: elsewhere a smooth correction stays under)
        dz0 = np.diff(z)
        g = _ceiling(ceil, xy, z)
        lim = g * np.maximum(ds, 1e-6)
        rows_at = set(np.flatnonzero(np.abs(dz0) >= lim - GRADE_ROW_M * np.maximum(ds, 1e-6)).tolist())
        if extra is not None:
            rows_at |= {i for i in extra.get(n, ()) if 0 <= i < m - 1}
            extra[n] = set(rows_at)
        pen = GRADE_PENALTY * hold * max(spread / step, 1.0)
        for i in sorted(rows_at):
            # z[i+1] - z[i] <= lim:  d[i] - d[i+1] >= dz0 - lim;  >= -lim:  d[i+1] - d[i] >= -lim - dz0
            couplings.append((n, i, n, i + 1, "ge", float(dz0[i] - lim[i]), pen))
            couplings.append((n, i + 1, n, i, "ge", float(-lim[i] - dz0[i]), pen))
    n_joins = 0
    for members in node_list:
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
    if corridors is not None:
        j_, i_, tg_, lim_ = corridors
        for r in sorted(rows):
            j, i = int(j_[r]), int(i_[r])
            if chains[j]["lo"][i] == chains[j]["hi"][i]:
                continue                    # (a held sample: its height is given)
            c = float(tg_[r]) - float(z0[j][i])
            lim = float(lim_[r])
            pen = CORRIDOR_PENALTY * stiff[j]
            # (|z_j - target| <= lim, on j alone: d_j >= c - lim, d_j <= c + lim)
            couplings.append((j, i, None, 0, "ge", c - lim, pen))
            couplings.append((j, i, None, 0, "le", c + lim, pen))
        # (a road the rows move keeps its ends, as share_corridors' spline does, softly: lifted 1 m beside a road
        # climbing to its bridge, a Fells Point slip road's smoothed correction ran on down to its free end, and took
        # the junction there 1.3 m under its profile in the cell that owns it, where the next cell held that end at its
        # profile: a 0.36 m seam between them)
        for j in sorted({int(j_[r]) for r in rows}):
            for e in (0, len(z0[j]) - 1):
                if chains[j]["lo"][e] != chains[j]["hi"][e]:
                    pen = CORRIDOR_PENALTY * stiff[j]
                    couplings.append((j, e, None, 0, "ge", 0.0, pen))
                    couplings.append((j, e, None, 0, "le", 0.0, pen))
    return chains, couplings, n_joins


def solve(plans, specs, z0, ceilings, ranks, pins=(), dips=None, budget=BUDGET_S, caps=None, floors=None,
          corridors=None, joints=True):
    """(heights per plan, info), or (None, info) when the hard rows are infeasible (the passes' plans stand: the RFC's
    fallback). info: the kernel's diagnostics plus n (unknowns), joins, grade_broken (grade rows broken, over 1 mm),
    grade_excess_m (the most one is), grade_worst (where the heights are most over a ceiling: plan, x, y, grade,
    ceiling; None: nowhere), clear_broken / clear_excess_m / clear_worst (the caps under decks and the floors onto
    them, as the grade's), corridor_rows / corridor_broken / corridor_excess_m / corridor_worst (corridors: grading's
    corridor_pairs; the pairs given rows, those the heights break by over 1 mm, the most, where),
    rounds_grade, fallback (and culprit), elapsed_s. joints: the junctions capped by the dips near them (_joint_caps;
    off where the underpass rule keeps a dip between a street's junctions, on the coarse model, UNDER_RUN_ON)."""
    import carriageways
    t0 = time.perf_counter()
    info = {"n": int(sum(len(z) for z in z0)), "fallback": False, "grade_broken": 0, "grade_excess_m": 0.0,
            "clear_broken": 0, "clear_excess_m": 0.0, "clear_worst": None, "corridor_rows": 0, "corridor_broken": 0,
            "corridor_excess_m": 0.0, "corridor_worst": None}
    if not CORRIDOR_ROWS_ON or corridors is None or not len(corridors[0]):
        corridors = None
    if corridors is not None:
        corridors = _corridor_bands(corridors, z0, dips, floors)
    if joints:
        caps = _joint_caps(plans, z0, dips, caps, pins)
    if not plans:
        info.update(joins=0, elapsed_s=0.0)
        return [], info
    out = None
    extra = {}
    rows = set() if corridors is None else set(_corridor_over(corridors, z0))
    for _round in range(GRADE_ROUNDS if GRADE_ROWS_ON or corridors is not None else 1):
        chains, couplings, n_joins = problem(plans, specs, z0, ceilings, ranks, pins, dips, grade_rows=GRADE_ROWS_ON,
                                             extra=extra, caps=caps, floors=floors, corridors=corridors, rows=rows)
        diag = {}
        ds = carriageways.solve(chains, couplings, deadline=time.monotonic() + budget, diagnostics=diag)
        info.update({k: (v if isinstance(v, (bool, int, float, str)) else str(v)) for k, v in diag.items()})
        info["joins"] = n_joins
        info["rounds_grade"] = _round + 1
        if not (diag.get("converged") and diag.get("feasible")):
            out = None          # (the hard rows: no earlier round's heights, which had fewer rows, stand for these)
            break
        out = [np.asarray(z, np.float64) + np.asarray(d, np.float64) for z, d in zip(z0, ds)]
        clear = _cap_excess(plans, out, caps, floors)
        info.update(clear_broken=clear[0], clear_excess_m=clear[1], clear_worst=clear[2])
        corr = _corridor_excess(plans, corridors, rows, out)
        info.update(corridor_rows=len(rows), corridor_broken=corr[0], corridor_excess_m=corr[1],
                    corridor_worst=corr[2])
        # (the kernel's soft rows are all kinds: the caps' and the corridors' counted here, the same 1 mm)
        info["grade_broken"] = max(int(diag.get("soft_broken", 0)) - clear[0] - corr[0], 0)
        info["grade_excess_m"] = float(diag.get("soft_excess_m", 0.0)) if info["grade_broken"] else 0.0
        more = bool(GRADE_ROWS_ON and _over_ceiling(plans, out, z0, ceilings, extra))
        if corridors is not None:
            new = set(_corridor_over(corridors, out)) - rows
            rows |= new
            more = more or bool(new)
        if not more:
            break
    info["fallback"] = out is None
    info["grade_worst"] = _worst_grade(plans, out, z0, ceilings) if out is not None and GRADE_ROWS_ON else None
    if out is None:             # (which kind of row it is: the one whose absence makes the no-grade solve feasible)
        info["culprit"] = _culprit(plans, specs, z0, ceilings, ranks, pins, dips, budget, caps, floors)
    info["elapsed_s"] = time.perf_counter() - t0
    return out, info


def _joint_caps(plans, z0, dips, caps, pins=()):
    """caps with every junction (nodes) capped at the deepest dip any of its roads has within JOINT_FLAT_M of it:
    the underpass rule's flat junction. caps itself when no junction is near a dip. Pure."""
    if dips is None or all(d is None for d in dips):
        return caps
    xys = [np.asarray(p.xy, np.float64)[:, :2] for p in plans]
    zz = [np.asarray(z, np.float64) for z in z0]
    arcs = {}

    def arc(j):
        got = arcs.get(j)
        if got is None:
            got = arcs[j] = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xys[j], axis=0).T))]
        return got
    out = None
    for m in nodes(xys, zz, halves=_halves(plans),
                   cut={(a, i) for a, i, _h in pins}):
        h = np.inf
        for j, i in m:
            d = dips[j]
            if d is None:
                continue
            s = arc(j)
            v = np.asarray(d, np.float64)[np.abs(s - s[i]) <= JOINT_FLAT_M]
            v = v[np.isfinite(v)]
            if len(v):
                h = min(h, float(v.min()))
        if not np.isfinite(h):
            continue
        if out is None:
            out = [None if c is None else np.array(c, np.float64) for c in (caps or [None] * len(plans))]
        for j, i in m:
            if out[j] is None:
                out[j] = np.full(len(zz[j]), np.nan)
            out[j][i] = np.fmin(out[j][i], h)
    return caps if out is None else out


def _culprit(plans, specs, z0, ceilings, ranks, pins, dips, budget, caps=None, floors=None):
    """The kinds of hard rows ("pins", "abut", "dips", "nodes") without which the solve (no grade rows) is feasible,
    each tried alone: what made a cell fall back (logged, counted by tools/grade_metrics.py)."""
    import carriageways
    out = []
    for kind in ("dips", "pins", "abut", "nodes"):
        chains, couplings, _n = problem(plans, specs, z0, ceilings, ranks, pins, dips, grade_rows=False, drop=(kind,),
                                        caps=caps, floors=floors)
        diag = {}
        carriageways.solve(chains, couplings, deadline=time.monotonic() + budget, diagnostics=diag)
        if diag.get("converged") and diag.get("feasible"):
            out.append(kind)
    return out


def _cap_excess(plans, zs, caps, floors=None, slack=1e-3):
    """(how many capped samples heights zs are over their cap, or under their floor, by more than slack m, the most
    one is, where: {plan, x, y, excess_m} or None)."""
    n_over, worst, most = 0, None, 0.0
    for n, (p, z) in enumerate(zip(plans, zs)):
        z = np.asarray(z, np.float64)
        over = np.full(len(z), -np.inf)
        if caps is not None and caps[n] is not None:
            over = np.maximum(over, np.where(np.isfinite(caps[n]), z - caps[n], -np.inf))
        if floors is not None and floors[n] is not None:
            over = np.maximum(over, np.where(np.isfinite(floors[n]), floors[n] - z, -np.inf))
        if not np.isfinite(over).any():
            continue
        n_over += int((over > slack).sum())
        i = int(np.argmax(over))
        if over[i] > max(most, slack):
            most = float(over[i])
            xy = np.asarray(p.xy, np.float64)
            worst = {"plan": n, "x": float(xy[i, 0]), "y": float(xy[i, 1]), "excess_m": round(most, 3)}
    return n_over, most, worst


def _corridor_diff(corridors, zs):
    """Per pair of corridors (grading's corridor_pairs) the heights zs' difference, k's (at its nearest point) less j's,
    and what its gap holds (m)."""
    j, i, k, si, t, gap = corridors
    if not len(j):
        return np.zeros(0), np.zeros(0)
    cat = np.concatenate([np.asarray(z, np.float64) for z in zs])
    off = np.r_[0, np.cumsum([len(z) for z in zs])].astype(np.int64)
    a = cat[off[k] + si]
    b = cat[off[k] + si + 1]
    diff = a + (b - a) * t - cat[off[j] + i]
    return diff, np.maximum(gap, 0.0) * CORRIDOR_BANK


def _corridor_bands(corridors, z0, dips=None, floors=None):
    """grading's corridor_pairs (j, i, k, si, t, gap[, key j, key k]) as the solve's bands (j, i, target, lim), or
    None: per pair whose plan j gives way to k (share_corridors' keys: the lower; between equals the lower one, up;
    without keys, the lower one), k's rule height beside it (its profile z0, under its dips and over its floors) and what
    the gap holds. Pairs over CORRIDOR_MAX_DIFF_M apart by those heights are two levels and get none (a road beside the
    avenues Athens' underpass rule dips 8 m under the A1 followed them down)."""
    rule = [np.asarray(z, np.float64).copy() for z in z0]
    for n, r in enumerate(rule):
        for bound, pick in ((dips, np.fmin), (floors, np.fmax)):
            if bound is not None and bound[n] is not None:
                rule[n] = r = pick(r, np.asarray(bound[n], np.float64))
    diff, lim = _corridor_diff(corridors[:6], rule)
    if len(corridors) >= 8:
        kj, kk = np.asarray(corridors[6]), np.asarray(corridors[7])
        gives = (kj < kk) | ((kj == kk) & (diff > 0.0))
    else:
        gives = diff > 0.0
    keep = gives & (np.abs(diff) <= CORRIDOR_MAX_DIFF_M)
    if not keep.any():
        return None
    j, i = np.asarray(corridors[0])[keep], np.asarray(corridors[1])[keep]
    return j, i, np.asarray([rule[a][b] for a, b in zip(j.tolist(), i.tolist())]) + diff[keep], lim[keep]


def _band_excess(bands, zs):
    """Per band (_corridor_bands') how far heights zs are outside it (m, 0 inside)."""
    j, i, tg, lim = bands
    if not len(j):
        return np.zeros(0)
    z = np.asarray([zs[a][b] for a, b in zip(np.asarray(j).tolist(), np.asarray(i).tolist())], np.float64)
    return np.maximum(np.abs(z - np.asarray(tg, np.float64)) - np.asarray(lim, np.float64), 0.0)


def _corridor_over(corridors, zs, slack=CORRIDOR_SLACK_M):
    """The bands (_corridor_bands') heights zs are outside by over slack m."""
    return np.flatnonzero(_band_excess(corridors, zs) > slack).tolist()


def _corridor_excess(plans, corridors, rows, zs, slack=1e-3):
    """(how many of the bands given rows heights zs break by over slack m, the most one is, where: {plan, x, y,
    excess_m} or None)."""
    if corridors is None or not rows:
        return 0, 0.0, None
    r = np.asarray(sorted(rows), np.int64)
    excess = _band_excess(tuple(np.asarray(c)[r] for c in corridors), zs)
    n_over = int((excess > slack).sum())
    if not n_over:
        return 0, 0.0, None
    w = int(np.argmax(excess))
    j, i = int(corridors[0][r[w]]), int(corridors[1][r[w]])
    xy = np.asarray(plans[j].xy, np.float64)
    return n_over, float(excess[w]), {"plan": j, "x": float(xy[i, 0]), "y": float(xy[i, 1]),
                                      "excess_m": round(float(excess[w]), 3)}


def _worst_grade(plans, zs, z0, ceilings, slack=1e-3):
    """Where heights zs are most over their plan's ceiling (problem's, per segment): {plan, x, y (the segment's middle),
    grade, ceiling}, or None where every one holds (within slack m)."""
    worst, over = None, slack
    for n, (p, z, b) in enumerate(zip(plans, zs, z0)):
        if ceilings[n] is None or len(z) < 2:
            continue
        xy = np.asarray(p.xy, np.float64)[:, :2]
        ds = np.maximum(dnp.hypot(*np.diff(xy, axis=0).T), 1e-6)
        g = _ceiling(ceilings[n], xy, np.asarray(b, np.float64))
        excess = np.abs(np.diff(z)) - g * ds
        i = int(np.argmax(excess))
        if excess[i] > over:
            over = float(excess[i])
            mid = (xy[i] + xy[i + 1]) / 2
            worst = {"plan": n, "x": float(mid[0]), "y": float(mid[1]), "excess_m": round(over, 3),
                     "grade": round(float(abs(z[i + 1] - z[i]) / ds[i]), 3), "ceiling": round(float(g), 3)}
    return worst


def _over_ceiling(plans, zs, z0, ceilings, extra, slack=1e-3, reach=GRADE_ROW_REACH):
    """The segments where heights zs break their plan's ceiling (as problem's rows put it) and have no row yet, with
    `reach` segments either side, added to extra {plan: set}; how many were new (a broken row's segment has one: soft,
    it is broken where the hard rows leave no room, and another round would not change it)."""
    new = 0
    for n, (p, z, b) in enumerate(zip(plans, zs, z0)):
        ceil = ceilings[n]
        if ceil is None or len(z) < 2:
            continue
        xy = np.asarray(p.xy, np.float64)[:, :2]
        ds = np.maximum(dnp.hypot(*np.diff(xy, axis=0).T), 1e-6)
        g = _ceiling(ceil, xy, np.asarray(b, np.float64))
        bad = np.flatnonzero(np.abs(np.diff(z)) > g * ds + slack)      # (z: this solve's heights)
        got = extra.setdefault(n, set())
        bad = [i for i in bad.tolist() if i not in got]
        if not bad:
            continue
        for i in bad:
            for j in range(i - reach, i + reach + 1):
                if j not in got:
                    got.add(j)
                    new += 1
    return new
