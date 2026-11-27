"""Step 7 of the 3D solve: the heights of every sample.

    solve(problem) -> Solution        the problem's one kernel solve (carriageways.solve: the soft laws, earthfast 643)
    blocks(problem, samples) -> [Block]     the area cut into square cores, each with what lies within RAMP_M and
                                      MARGIN_M round it (its paths' runs there, the couplings of two samples there)
    solve_blocks(problem, samples) -> Solution   each block solved (in threads: the kernel releases the GIL), the answers
                                      blended across RAMP_M either side of each core's edge (weights summing to 1: a
                                      join's or a clearance's two samples, at one place, take one blend of two answers
                                      that each hold it)
    measure(problem, z) -> dict       what the answer did: moves off the ground, grades and curves past the laws, the
                                      clearances short, the joins' gaps

The answer is checked against the problem, not trusted: measure recomputes what each law and coupling asked from the
heights alone."""
from dataclasses import dataclass, field
import time

import numpy as np

import carriageways as _cw


@dataclass
class Solution:
    """z (N,): every sample's height, path after path (samples.off); seconds; diagnostics (the kernel's)."""
    z: np.ndarray
    seconds: float
    diagnostics: dict = field(default_factory=dict)


class NotSolved(RuntimeError):
    """The kernel ended without a converged answer: no heights are made of it (there is no fallback)."""


def _checked(d, what):
    if d.get("status") not in ("Solved", "Unconstrained") or not d.get("converged", True):   # (Unconstrained: nothing in it)
        raise NotSolved("grade3d: %s not solved: %s" % (what, {k: d.get(k) for k in ("status", "converged", "rounds")}))


def solve(problem, rounds=_cw.ROUNDS):
    """The heights of the problem's samples: one solve of the whole, no time limit (NotSolved when it does not
    converge)."""
    t0 = time.perf_counter()
    d = {}
    zs = _cw.solve(problem.chains, problem.couplings, rounds=rounds, diagnostics=d,
                   soft=problem.soft if problem.couplings else None, unlimited=True)
    _checked(d, "the area")
    z = np.concatenate([np.asarray(x, np.float64) for x in zs]) if zs else np.zeros(0)
    return Solution(z, time.perf_counter() - t0, d)


def _value(z, off, c):
    return z[off[c[0]] + (c[1] % (off[c[0] + 1] - off[c[0]]))] - z[off[c[2]] + (c[3] % (off[c[2] + 1] - off[c[2]]))]


def measure(problem, z, doubt_w=None):
    """{moved_mean, moved_p99, moved_max (m off the target where the target pays), grade_excess_max (past the hard
    ceilings), soft_grade_excess_p99 (past the comfortable), join_gap_max (m), bound_short_max and bound_short_count (a
    deck under its floor, a tunnel over its roof), clear_short_max and clear_short_count
    (of the clearances at doubt_w or more: the firm ones), doubted_short_max and doubted_short_count (the others),
    meet_step_max}."""
    off = np.r_[0, np.cumsum([len(c["t"]) for c in problem.chains])].astype(np.int64)
    moved, gx, sgx = [], [0.0], [0.0]
    bound, n_bound = 0.0, 0
    for p, c in enumerate(problem.chains):
        zp = z[off[p]:off[p + 1]]
        w = np.asarray(c["w"])
        if (w > 0).any():
            moved.append(np.abs(zp - np.asarray(c["t"]))[w > 0])
        out = np.maximum(np.asarray(c["lo"]) - zp, zp - np.asarray(c["hi"]))
        out = out[np.isfinite(out)]
        if len(out):
            bound = max(bound, float(out.max()))
            n_bound += int((out > 0.05).sum())
        if len(zp) >= 2 and c["step"] > 0:
            g = np.abs(np.diff(zp)) / (np.asarray(c["ds"]) if c.get("ds") is not None else c["step"])
            gx.append(float(max(0.0, (g - c["grade"]).max())))
            sgx.append(float(np.percentile(np.maximum(0.0, g - c["soft_grade"]), 99)))
    moved = np.concatenate(moved) if moved else np.zeros(1)
    if doubt_w is None:
        from . import layers
        doubt_w = layers.W_CLEAR
    gap, short, n_short, step = 0.0, 0.0, 0, 0.0
    dshort, n_dshort = 0.0, 0
    for c, (what, _k), sw in zip(problem.couplings, problem.why, problem.soft):
        v = float(_value(z, off, c))
        if c[4] == "eq":
            gap = max(gap, abs(v - c[5]))
        elif what == "clear":
            s = max(0.0, c[5] - v)
            if sw >= doubt_w:
                short = max(short, s)
                n_short += s > 0.05
            else:
                dshort = max(dshort, s)
                n_dshort += s > 0.05
        elif what == "meet":
            step = max(step, abs(v))
    return dict(moved_mean=float(moved.mean()), moved_p99=float(np.percentile(moved, 99)), moved_max=float(moved.max()),
                grade_excess_max=max(gx), soft_grade_excess_p99=max(sgx), join_gap_max=gap,
                bound_short_max=max(0.0, bound), bound_short_count=n_bound, clear_short_max=short,
                clear_short_count=n_short, doubted_short_max=dshort, doubted_short_count=n_dshort, meet_step_max=step)


def worst(problem, z, n=10, what="clear", min_w=0.0):
    """The n couplings of kind what ("clear", "meet", "join"), of weight min_w or more, most short of what they ask:
    [(short m, coupling index)]."""
    off = np.r_[0, np.cumsum([len(c["t"]) for c in problem.chains])].astype(np.int64)
    out = []
    for k, (c, (w, _i)) in enumerate(zip(problem.couplings, problem.why)):
        if w != what or (problem.soft[k] < min_w and c[4] != "eq"):
            continue
        v = float(_value(z, off, c))
        short = abs(v - c[5]) if c[4] == "eq" else max(0.0, c[5] - v) if c[4] == "ge" else max(0.0, v - c[5])
        out.append((short, k))
    return sorted(out, reverse=True)[:n]


BLOCK_M = 1500.0        # m: a block's core
RAMP_M = 100.0          # m either side of a core's edge over which two blocks' answers are blended
MARGIN_M = 300.0        # m past the blend each block reaches (its free cut ends that far from where it is used)
WORKERS = 6             # threads solving blocks at most: past a few the kernels contend for memory (Yokohama's 10 km
#                         square: one block 9.9 s with 4 at once, 31 s with 16, the whole as long)


@dataclass
class Block:
    """core (lo (2,), hi (2,)); runs [(path, first sample, last sample)]: its chains; couplings and soft, remapped to the
    runs; idx (n,): the flat sample index of each of its samples, in the runs' order."""
    core: tuple
    runs: list
    couplings: list
    soft: list
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
    cps, soft = [], []
    for c, w in zip(problem.couplings, problem.soft):
        n_a = samples.off[c[0] + 1] - samples.off[c[0]]
        n_b = samples.off[c[2] + 1] - samples.off[c[2]]
        fa, fb = samples.off[c[0]] + c[1] % n_a, samples.off[c[2]] + c[3] % n_b
        if run_of[fa] < 0 or run_of[fb] < 0:
            continue
        cps.append((int(run_of[fa]), int(pos_in[fa]), int(run_of[fb]), int(pos_in[fb])) + tuple(c[4:]))
        soft.append(w)
    return Block((np.asarray(c0, np.float64), np.asarray(c1, np.float64)), runs, cps, soft, np.concatenate(idx))


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


def solve_block(problem, b, rounds=_cw.ROUNDS):
    """(z (len(b.idx),), the kernel's diagnostics, seconds) of block b of the problem."""
    chains = []
    for p, a, e in b.runs:
        ch = dict(problem.chains[p])
        for key in ("t", "w", "lo", "hi"):
            ch[key] = np.asarray(ch[key])[a:e + 1]
        if ch.get("ds") is not None:
            ch["ds"] = np.asarray(ch["ds"])[a:e]
        chains.append(ch)
    d = {}
    s0 = time.perf_counter()
    zs = _cw.solve(chains, b.couplings, rounds=rounds, diagnostics=d, soft=b.soft if b.couplings else None,
                   unlimited=True)
    _checked(d, "block %s" % (np.round(np.asarray(b.core, np.float64).ravel(), 0).tolist(),))
    return np.concatenate(zs), d, time.perf_counter() - s0


def blend_weight(xy, core, ramp_m=RAMP_M):
    """A block's weight at the points xy: 1 inside its core past ramp_m from the edge, falling linearly to 0 ramp_m
    outside it (two neighbours' weights sum to 1 across their shared edge; four at a corner)."""
    c0, c1 = core
    w = np.ones(len(xy))
    r = 2.0 * ramp_m
    for k in (0, 1):
        w *= np.clip((xy[:, k] - (c0[k] - ramp_m)) / r, 0.0, 1.0) * np.clip(((c1[k] + ramp_m) - xy[:, k]) / r, 0.0, 1.0)
    return w


def solve_blocks(problem, samples, block_m=BLOCK_M, ramp_m=RAMP_M, margin_m=MARGIN_M, workers=None, rounds=_cw.ROUNDS):
    """The heights of the problem's samples, block by block (blocks), blended (blend_weight). diagnostics: the blocks,
    their kernels' statuses, the slowest block's seconds and all of theirs."""
    import os
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.perf_counter()
    bl = blocks(problem, samples, block_m, ramp_m, margin_m)

    def one(b):
        return solve_block(problem, b, rounds)
    n = int(workers or max(1, min(WORKERS, (os.cpu_count() or 2) // 2)))
    with ThreadPoolExecutor(n) as ex:
        got = list(ex.map(one, bl))
    num, den = np.zeros(len(samples)), np.zeros(len(samples))
    status, worst, total = {}, 0.0, 0.0
    per_block = []
    for b, (zb, d, secs) in zip(bl, got):
        per_block.append(dict(core=[float(v) for v in np.r_[b.core[0], b.core[1]]], chains=len(b.runs), samples=len(b.idx),
                              couplings=len(b.couplings), seconds=round(secs, 2), status=str(d.get("status")),
                              rounds=d.get("rounds"), rows=d.get("constraint_rows")))
        w = blend_weight(samples.xy[b.idx], b.core, ramp_m)
        np.add.at(num, b.idx, w * zb)
        np.add.at(den, b.idx, w)
        s = str(d.get("status"))
        status[s] = status.get(s, 0) + 1
        worst, total = max(worst, secs), total + secs
    z = num / np.where(den > 0, den, 1.0)
    sol = Solution(z, time.perf_counter() - t0, dict(blocks=len(bl), status=status, worst_block_s=round(worst, 2),
                                                     kernel_s=round(total, 2), workers=n))
    sol.per_block = per_block
    return sol
