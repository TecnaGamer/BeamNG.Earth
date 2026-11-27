"""Step 7 of the 3D solve: the heights of every sample, the least earthwork that holds every join, clearance and law.

    solve(problem) -> Solution        the whole problem, one linear program
    blocks(problem, samples) -> [Block]     the area cut into square cores, each with what lies within RAMP_M and
                                      MARGIN_M round it (its paths' runs there, the couplings of two samples there)
    solve_blocks(problem, samples) -> Solution   each block solved, the answers blended across RAMP_M either side of each
                                      core's edge (weights summing to 1: a join's or a clearance's two samples, at one
                                      place, take one blend of two answers that each hold it)
    measure(problem, z) -> dict       what the answer did: the earthwork, moves off the terrain, grades and curves past
                                      the laws, the clearances short, the joins' gaps

The earthwork is linear in depth: a sample's |z - t| times its w (laws: length x width x speed^2), so a conflict goes
whole to where it costs least, not shared out as a squared pull shares it. Known and never bent: the joins (one
height), the clearances (the upper over the lower by its need at least), each path's grade (per segment, laws.glim)
and vertical curve (its change of grade between two segments at most their mean length over R). Among answers of one
earthwork, the one whose grades change least (TIE of the median sample's cost per m of path and unit of grade change).

With no answer the problem contradicts itself and nothing is made of it (NotSolved): an irreducible set of its laws and
clearances that cannot hold together with the joins is named, each by its place and its paths, for the map's data
there to be read."""
from concurrent.futures import Future
from dataclasses import dataclass, field
import os
import queue
import threading
import time

import numpy as np
import scipy.sparse as sp
from scipy.optimize import linprog

TIE = 1.0e-3                # of the median sample's cost: per m of path and unit of grade change
HIGHS_THREADS = max(2, min(8, (os.cpu_count() or 2) // 2))     # threads the process's HiGHS solves run on

# HiGHS (scipy's linprog "highs") keeps a task executor per thread that calls it, with worker threads of its own, torn
# down when that thread ends. On Windows the thread then never ends: its join waits for ever (scipy 1.17.1, 2026-11-27: a
# thread that ran one linprog and returned hung its join in PyThread_join_thread; Popoli's plans waited on a pool's
# shutdown for 30 min, no road in the game). So every solve of the process runs on HIGHS_THREADS threads of its own that
# live as long as it does (daemon threads, never joined), whatever thread asks.
_HIGHS_Q = queue.Queue()
_HIGHS_LOCK = threading.Lock()
_HIGHS_RUNNING = []


def _highs_worker():
    while True:
        fut, args, kw = _HIGHS_Q.get()
        if not fut.set_running_or_notify_cancel():
            continue
        try:
            fut.set_result(linprog(*args, method="highs", **kw))
        except BaseException as e:      # (the caller's to raise)
            fut.set_exception(e)


def highs(*args, **kw):
    """scipy.optimize.linprog(*args, method="highs", **kw) on one of the process's HiGHS threads, waited for."""
    with _HIGHS_LOCK:
        while len(_HIGHS_RUNNING) < HIGHS_THREADS:
            t = threading.Thread(target=_highs_worker, daemon=True, name="highs_%d" % len(_HIGHS_RUNNING))
            t.start()
            _HIGHS_RUNNING.append(t)
    fut = Future()
    _HIGHS_Q.put((fut, args, kw))
    return fut.result()


@dataclass
class Solution:
    """z (N,): every sample's height, path after path (samples.off); seconds; diagnostics."""
    z: np.ndarray
    seconds: float
    diagnostics: dict = field(default_factory=dict)


class NotSolved(RuntimeError):
    """The problem has no answer (its laws and clearances contradict each other with its joins) or the solver ended
    without one: no heights are made of it (there is no fallback)."""


def _flat(chains, couplings):
    """(off (P + 1,), flat index of each coupling's two samples (C, 2))."""
    off = np.r_[0, np.cumsum([len(c["t"]) for c in chains])].astype(np.int64)
    ab = np.array([(off[c[0]] + c[1] % (off[c[0] + 1] - off[c[0]]), off[c[2]] + c[3] % (off[c[2] + 1] - off[c[2]]))
                   for c in couplings], np.int64).reshape(-1, 2)
    return off, ab


class _Rows:
    """The rows of A x <= b, by group (a law of one path, one crossing's clearances, the earthwork's)."""

    def __init__(self):
        self.r, self.c, self.v, self.b, self.g = [], [], [], [], []
        self.n = 0

    def add(self, cols, vals, b, group):
        """Rows k of len(b): sum over j of vals[j][k] x[cols[j][k]] <= b[k]."""
        b = np.asarray(b, np.float64)
        k = self.n + np.arange(len(b))
        for cj, vj in zip(cols, vals):
            self.r.append(k)
            self.c.append(np.asarray(cj, np.int64))
            self.v.append(np.broadcast_to(np.asarray(vj, np.float64), k.shape))
        self.b.append(b)
        self.g += group if isinstance(group, list) else [group] * len(b)
        self.n += len(b)

    def matrix(self, ncol):
        if not self.n:
            return sp.csr_matrix((0, ncol)), np.zeros(0)
        return (sp.csr_matrix((np.concatenate(self.v), (np.concatenate(self.r), np.concatenate(self.c))),
                              shape=(self.n, ncol)), np.concatenate(self.b))


def program(chains, couplings, why):
    """The linear program of the chains and couplings: (c, A_ub, b_ub, A_eq, b_eq, bounds, groups, n), x = [z (n), u (m:
    |z - t| of the samples that cost), v (k: |grade change| at each curve's sample)]; groups one per A_ub row."""
    off, ab = _flat(chains, couplings)
    n = int(off[-1])
    t = np.concatenate([np.asarray(c["t"], np.float64) for c in chains]) if chains else np.zeros(0)
    w = np.concatenate([np.asarray(c["w"], np.float64) for c in chains]) if chains else np.zeros(0)
    cost = np.flatnonzero(w > 0)
    m = len(cost)
    # (the curves' samples: every inner sample of a path with a vertical curve)
    mid, a_ds, b_ds, rad, p_of = [], [], [], [], []
    for p, c in enumerate(chains):
        k = len(c["t"])
        if k >= 3 and c["R"] > 0:
            ds = np.asarray(c["ds"], np.float64)
            mid.append(off[p] + np.arange(1, k - 1))
            a_ds.append(ds[:-1])
            b_ds.append(ds[1:])
            rad.append(np.full(k - 2, float(c["R"])))
            p_of.append(np.full(k - 2, p))
    mid = np.concatenate(mid) if mid else np.zeros(0, np.int64)
    a_ds = np.concatenate(a_ds) if a_ds else np.zeros(0)
    b_ds = np.concatenate(b_ds) if b_ds else np.zeros(0)
    rad = np.concatenate(rad) if rad else np.zeros(0)
    p_of = np.concatenate(p_of) if p_of else np.zeros(0, np.int64)
    kv = len(mid)
    ncol = n + m + kv
    rows = _Rows()
    U = n + np.arange(m)
    rows.add([cost, U], [1.0, -1.0], t[cost], ("earth",))               # z - u <= t
    rows.add([cost, U], [-1.0, -1.0], -t[cost], ("earth",))             # -z - u <= -t
    for p, c in enumerate(chains):
        k = len(c["t"])
        if k < 2:
            continue
        i = off[p] + np.arange(k - 1)
        lim = np.asarray(c["glim"], np.float64) * np.asarray(c["ds"], np.float64)
        rows.add([i + 1, i], [1.0, -1.0], lim, ("grade", p))
        rows.add([i + 1, i], [-1.0, 1.0], lim, ("grade", p))
    if kv:
        # (grade after - grade before: (z+ - z) / b - (z - z-) / a, at most (a + b) / 2 / R either way)
        ca, cb = 1.0 / a_ds, 1.0 / b_ds
        cols = [mid - 1, mid, mid + 1]
        vals = [ca, -(ca + cb), cb]
        bend = (a_ds + b_ds) / 2.0 / rad
        grp = [("curve", int(p)) for p in p_of]
        rows.add(cols, vals, bend, grp)
        rows.add(cols, [-x for x in vals], bend, grp)
        V = n + m + np.arange(kv)
        rows.add(cols + [V], vals + [-1.0], np.zeros(kv), ("earth",))
        rows.add(cols + [V], [-x for x in vals] + [-1.0], np.zeros(kv), ("earth",))
    eq = [k for k, c in enumerate(couplings) if c[4] == "eq"]
    ge = [k for k, c in enumerate(couplings) if c[4] == "ge"]
    if ge:
        g = np.asarray(ge)
        rows.add([ab[g, 1], ab[g, 0]], [1.0, -1.0], [-float(couplings[k][5]) for k in ge], [why[k] for k in ge])
    A, b = rows.matrix(ncol)
    if eq:
        e = np.asarray(eq)
        A_eq = sp.csr_matrix((np.r_[np.ones(len(eq)), -np.ones(len(eq))],
                              (np.r_[np.arange(len(eq)), np.arange(len(eq))], np.r_[ab[e, 0], ab[e, 1]])),
                             shape=(len(eq), ncol))
        b_eq = np.array([float(couplings[k][5]) for k in eq])
    else:
        A_eq, b_eq = None, None
    unit = float(np.median(w[cost])) if m else 1.0
    span = (a_ds + b_ds) / 2.0
    c = np.r_[np.zeros(n), w[cost], TIE * unit * span]
    bounds = [(None, None)] * n + [(0.0, None)] * (m + kv)
    return c, A, b, A_eq, b_eq, bounds, rows.g, n


def _irreducible(A, b, A_eq, b_eq, bounds, groups):
    """An irreducible set of the groups (a path's grade, a path's curve, one crossing's clearances) that has no answer
    with the joins and the rest of the earthwork's rows: each taken out in turn, a half of those left first, and left out
    for good while what remains still has none."""
    gid = {}
    for r, g in enumerate(groups):
        gid.setdefault(g, []).append(r)
    zero = np.zeros(A.shape[1])

    def feasible(mask):
        return highs(zero, A_ub=A[mask], b_ub=b[mask], A_eq=A_eq, b_eq=b_eq, bounds=bounds).status == 0
    alive = np.ones(len(b), bool)
    todo = [g for g in gid if g[0] != "earth"]
    core = []
    chunk = len(todo)
    while todo:
        chunk = max(1, min(chunk, len(todo)) // 2) if len(todo) > 1 else 1
        part, rest = todo[:chunk], todo[chunk:]
        trial = alive.copy()
        for g in part:
            trial[gid[g]] = False
        if not feasible(trial):
            alive, todo, chunk = trial, rest, len(rest)
        elif chunk == 1:
            core.append(part[0])
            todo, chunk = rest, len(rest)
    return core


def _named(chains, couplings, why, names, core):
    """The contradiction's groups as text: each clearance by its need, its place and its two paths; each law by its
    value and its path."""
    out = []
    for g in core:
        if g[0] == "clear":
            k = next(j for j, w in enumerate(why) if w == g)
            pa, ia, pb, ib, _kind, need = couplings[k][:6]
            q = np.asarray(chains[pa]["xy"])[ia % len(chains[pa]["t"])]
            out.append("clearance %.2f m at (%.1f, %.1f): [%s] over [%s]" % (need, q[0], q[1], names[pa], names[pb]))
        else:
            c = chains[g[1]]
            xy = np.asarray(c["xy"])
            law = ("grade %.3f" % float(np.min(c["glim"]))) if g[0] == "grade" else ("vertical curve %.0f m" % c["R"])
            out.append("%s of [%s], (%.1f, %.1f) to (%.1f, %.1f)" % (law, names[g[1]], xy[0, 0], xy[0, 1], xy[-1, 0],
                                                                       xy[-1, 1]))
    return out


def _solve(chains, couplings, why, names, what):
    """(z, diagnostics) of the chains and couplings; NotSolved with the contradiction named when there is no answer."""
    c, A, b, A_eq, b_eq, bounds, groups, n = program(chains, couplings, why)
    if n == 0:
        return np.zeros(0), dict(status="Solved", rows=0)
    res = highs(c, A_ub=A, b_ub=b, A_eq=A_eq, b_eq=b_eq, bounds=bounds)
    if res.status == 2:
        core = _irreducible(A, b, A_eq, b_eq, bounds, groups)
        raise NotSolved("grade3d: %s has no answer: these cannot hold together with the map's joins:\n  %s" % (
            what, "\n  ".join(_named(chains, couplings, why, names, core))))
    if res.status != 0:
        raise NotSolved("grade3d: %s not solved: %s" % (what, res.message))
    return res.x[:n], dict(status="Solved", rows=int(A.shape[0]) + (0 if A_eq is None else int(A_eq.shape[0])),
                           objective=float(res.fun))


def solve(problem):
    """The heights of the problem's samples: one linear program of the whole (NotSolved when it has no answer)."""
    t0 = time.perf_counter()
    z, d = _solve(problem.chains, problem.couplings, problem.why, problem.names, "the area")
    return Solution(z, time.perf_counter() - t0, d)


def _value(z, off, c):
    """The coupled quantity z_a - z_b."""
    def at(p, i):
        return z[off[p] + (i % (off[p + 1] - off[p]))]
    return at(c[0], c[1]) - at(c[2], c[3])


def measure(problem, z):
    """{earthwork (the cost), moved_mean, moved_p99, moved_max (m off the terrain where that costs), grade_excess_max,
    curve_excess_max (past the laws), join_gap_max (m), clear_short_max (m), clear_short_count (more than 0.05 m)}."""
    off = np.r_[0, np.cumsum([len(c["t"]) for c in problem.chains])].astype(np.int64)
    moved, gx, cx, work = [], [0.0], [0.0], 0.0
    for p, c in enumerate(problem.chains):
        zp = z[off[p]:off[p + 1]]
        w, t = np.asarray(c["w"]), np.asarray(c["t"])
        if (w > 0).any():
            moved.append(np.abs(zp - t)[w > 0])
            work += float((w * np.abs(zp - t)).sum())
        if len(zp) >= 2:
            ds = np.asarray(c["ds"])
            g = np.diff(zp) / ds
            gx.append(float(max(0.0, (np.abs(g) - np.asarray(c["glim"])).max())))
            if len(zp) >= 3 and c["R"] > 0:
                cx.append(float(max(0.0, (np.abs(np.diff(g)) - (ds[:-1] + ds[1:]) / 2.0 / c["R"]).max())))
    moved = np.concatenate(moved) if moved else np.zeros(1)
    gap, short, n_short = 0.0, 0.0, 0
    for c, (what, _k) in zip(problem.couplings, problem.why):
        v = float(_value(z, off, c))
        if c[4] == "eq":
            gap = max(gap, abs(v - c[5]))
        elif what == "clear":
            s = max(0.0, c[5] - v)
            short = max(short, s)
            n_short += s > 0.05
    return dict(earthwork=work, moved_mean=float(moved.mean()), moved_p99=float(np.percentile(moved, 99)),
                moved_max=float(moved.max()), grade_excess_max=max(gx), curve_excess_max=max(cx), join_gap_max=gap,
                clear_short_max=short, clear_short_count=n_short)


def worst(problem, z, n=10, what="clear"):
    """The n couplings of kind what ("clear", "join") most short of what they ask: [(short m, coupling index)]."""
    off = np.r_[0, np.cumsum([len(c["t"]) for c in problem.chains])].astype(np.int64)
    out = []
    for k, (c, (w, _i)) in enumerate(zip(problem.couplings, problem.why)):
        if w != what:
            continue
        v = float(_value(z, off, c))
        out.append((abs(v - c[5]) if c[4] == "eq" else max(0.0, c[5] - v), k))
    return sorted(out, reverse=True)[:n]


BLOCK_M = 1500.0        # m: a block's core
RAMP_M = 100.0          # m either side of a core's edge over which two blocks' answers are blended
MARGIN_M = 300.0        # m past the blend each block reaches (its free cut ends that far from where it is used)
WORKERS = 6             # blocks solved at once at most


@dataclass
class Block:
    """core (lo (2,), hi (2,)); runs [(path, first sample, last sample)]: its chains; couplings and why, remapped to the
    runs; idx (n,): the flat sample index of each of its samples, in the runs' order."""
    core: tuple
    runs: list
    couplings: list
    why: list
    idx: np.ndarray


def block_of(problem, samples, c0, c1, reach, path_of=None):
    """The Block of core c0..c1 ((2,) each; an infinite side has no seam): a path's samples within reach of the core are
    its runs (a path leaving and coming back two), a coupling is the block's where both its samples are. None when no
    sample is in reach."""
    xy = samples.xy
    if path_of is None:
        path_of = np.repeat(np.arange(len(samples.h)), np.diff(samples.off))
    k = np.flatnonzero(((xy >= np.asarray(c0) - reach) & (xy <= np.asarray(c1) + reach)).all(1))
    if not len(k):
        return None
    runs, idx = [], []
    run_of = np.full(len(xy), -1, np.int64)
    pos_in = np.zeros(len(xy), np.int64)
    brk = np.flatnonzero((np.diff(k) != 1) | (np.diff(path_of[k]) != 0)) + 1
    for seg in np.split(k, brk):
        p = int(path_of[seg[0]])
        run_of[seg] = len(runs)
        pos_in[seg] = np.arange(len(seg))
        runs.append((p, int(seg[0] - samples.off[p]), int(seg[-1] - samples.off[p])))
        idx.append(seg)
    cps, why = [], []
    for c, w in zip(problem.couplings, problem.why):
        n_a = samples.off[c[0] + 1] - samples.off[c[0]]
        n_b = samples.off[c[2] + 1] - samples.off[c[2]]
        fa, fb = samples.off[c[0]] + c[1] % n_a, samples.off[c[2]] + c[3] % n_b
        if run_of[fa] < 0 or run_of[fb] < 0:
            continue
        cps.append((int(run_of[fa]), int(pos_in[fa]), int(run_of[fb]), int(pos_in[fb])) + tuple(c[4:6]))
        why.append(w)
    return Block((np.asarray(c0, np.float64), np.asarray(c1, np.float64)), runs, cps, why, np.concatenate(idx))


def blocks(problem, samples, block_m=BLOCK_M, ramp_m=RAMP_M, margin_m=MARGIN_M):
    """[Block] of the problem over its samples' extent, cores of block_m from the samples' least corner (block_of)."""
    xy = samples.xy
    if not len(xy):
        return []
    lo, hi = xy.min(0), xy.max(0)
    nx, ny = (max(1, int(np.ceil((hi[k] - lo[k]) / block_m))) for k in (0, 1))
    path_of = np.repeat(np.arange(len(samples.h)), np.diff(samples.off))
    out = []
    for bx in range(nx):
        for by in range(ny):
            c0 = lo + np.array([bx, by]) * block_m
            c1 = c0 + block_m
            # (the area's own edges are no seam: an outer core reaches past them, its weight 1 to the last sample)
            c0 = np.where([bx == 0, by == 0], -np.inf, c0)
            c1 = np.where([bx == nx - 1, by == ny - 1], np.inf, c1)
            b_ = block_of(problem, samples, c0, c1, ramp_m + margin_m, path_of)
            if b_ is not None:
                out.append(b_)
    return out


def solve_block(problem, b):
    """(z (len(b.idx),), diagnostics, seconds) of block b of the problem."""
    chains = []
    for p, a, e in b.runs:
        ch = dict(problem.chains[p])
        for key in ("t", "w", "xy"):
            ch[key] = np.asarray(ch[key])[a:e + 1]
        for key in ("ds", "glim"):
            ch[key] = np.asarray(ch[key])[a:e]
        chains.append(ch)
    s0 = time.perf_counter()
    z, d = _solve(chains, b.couplings, b.why, [problem.names[p] for p, _a, _e in b.runs],
                  "block %s" % (np.round(np.asarray(b.core, np.float64).ravel(), 0).tolist(),))
    return z, d, time.perf_counter() - s0


def blend_weight(xy, core, ramp_m=RAMP_M):
    """A block's weight at the points xy: 1 inside its core past ramp_m from the edge, falling linearly to 0 ramp_m
    outside it (two neighbours' weights sum to 1 across their shared edge; four at a corner)."""
    c0, c1 = core
    w = np.ones(len(xy))
    r = 2.0 * ramp_m
    for k in (0, 1):
        w *= np.clip((xy[:, k] - (c0[k] - ramp_m)) / r, 0.0, 1.0) * np.clip(((c1[k] + ramp_m) - xy[:, k]) / r, 0.0, 1.0)
    return w


def solve_blocks(problem, samples, block_m=BLOCK_M, ramp_m=RAMP_M, margin_m=MARGIN_M, workers=None):
    """The heights of the problem's samples, block by block (blocks), blended (blend_weight). diagnostics: the blocks,
    the slowest block's seconds and all of theirs."""
    import os
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.perf_counter()
    bl = blocks(problem, samples, block_m, ramp_m, margin_m)
    n = int(workers or max(1, min(WORKERS, (os.cpu_count() or 2) // 2)))
    with ThreadPoolExecutor(n) as ex:
        got = list(ex.map(lambda b_: solve_block(problem, b_), bl))
    num, den = np.zeros(len(samples)), np.zeros(len(samples))
    worst_s, total = 0.0, 0.0
    per_block = []
    for b, (zb, d, secs) in zip(bl, got):
        per_block.append(dict(core=[float(v) for v in np.r_[b.core[0], b.core[1]]], chains=len(b.runs), samples=len(b.idx),
                              couplings=len(b.couplings), seconds=round(secs, 2), rows=d.get("rows")))
        w = blend_weight(samples.xy[b.idx], b.core, ramp_m)
        np.add.at(num, b.idx, w * zb)
        np.add.at(den, b.idx, w)
        worst_s, total = max(worst_s, secs), total + secs
    z = num / np.where(den > 0, den, 1.0)
    sol = Solution(z, time.perf_counter() - t0, dict(blocks=len(bl), status="Solved", worst_block_s=round(worst_s, 2),
                                                     solve_s=round(total, 2), workers=n))
    sol.per_block = per_block
    return sol
