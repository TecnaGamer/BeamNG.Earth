"""Stage 5 of the staged grade solver: the heights of a system that has them (see staged_paths for the whole).

Input: a system stage 4 found feasible (its difference constraints: the grade law per step by the step's own run, clearances by
the order stage 2 gave, joints equal, the ground points stage 3 found held to the elevation, the floors and ceilings), and the
shapes' own objective (gradeshapes.shape_problem's chains: a target and weight per sample, the stiffness of a deck's line).
Output: a height per sample of every piece, the answer's residuals, and the iterations it took. Our own solver, deterministic
(plain floats, the same operations in the same order), no kernel, no time budget: an iteration cap, then it says what it reached.

The problem is the shapes' (carriageways.solve) without the vertical-curve bound:

    minimise   sum_i max(w_i, W_MIN) (z_i - t_i)^2  +  sum_chains lam * sum (z_{i-1} - 2 z_i + z_{i+1})^2
    such that  lo_e <= z_b - z_a <= hi_e   for every edge e of the constraint graph   (grade, clearance, joint, overlap)
               lo_k <= z_k <= hi_k          for every node with a floor, a ceiling or a ground point held

The curve bound (|second difference| <= step^2 / (100 K)) is not a difference constraint, which is what makes stage 4 exact; it is
not a hard constraint here, the stiffness lam is what keeps a deck's line, and the curvature of the answer is measured against the
kernel's bound (Heights.stats["curve_excess_m"]) instead of enforced. W_MIN (the kernel's own floor on every weight, native/earthfast carriageway_solver.rs) makes the objective strictly convex and is
part of the shapes' answer, not a regulariser of ours: a deck's middle weighs nothing and its line is kept by lam alone, so what
sets its height is this floor's pull of every sample to its natural line (a deck joined at both ends, no target). At Brunau a
weight of 1e-4 here instead (what this solver first took) put decks 8-10 m over the kernel's and the survey's.

The method: ADMM in OSQP's form (Stellato et al. 2020), l <= A x <= u the rows above, the objective 1/2 x'Hx - h'x:

    solve (H + sigma I + A' diag(rho) A) x~ = sigma x + h + A'(rho z - y)       z~ = A x~
    x <- a x~ + (1 - a) x      z <- clip(a z~ + (1 - a) z + y / rho, l, u)       y <- y + rho (a z~ + (1 - a) z - z)

Convergence is a theorem, not a hope: the objective is strictly convex (W_MIN) and the rows' set closed convex and, as the system is
feasible (stage 4: no negative cycle, and the potentials it found are the start), the two-block ADMM converges for every rho > 0
and relaxation a in (0, 2): the primal residual |Ax - z| and the dual one |Hx - h + A'y| go to zero and x to the one minimiser
(Boyd et al. 2011, section 3.2). What the code reports is the residuals it stopped on, never more than that. The linear system is
solved by preconditioned conjugate gradients with the banded part of the matrix as the preconditioner: H is banded by piece
(pentadiagonal, nothing across two pieces), and so are the grade rows between neighbouring samples; they are factored (LDL', plain
floats, the pieces of similar length swept side by side) once for a rho and used for every solve. What is left of the matrix are
the rows between pieces (the joints, the clearances, the overlaps): that many directions at most (rank of the rest) for conjugate
gradients to find, each a banded solve. So one path is solved between its ground points, the piece's own line at once, and a
joint's equality is a row between two pieces' samples whose multiplier the iteration finds: the residual at the joints
(stats["joint_gap_m"]) is what is left of it. rho is rescaled by the residuals' balance (OSQP's rule) at most MAX_RHO times, each a new
factorisation; the work is MAX_ITER sweeps of at most PCG_MAX solves, bounded by those and never by a clock.

The exact bounds: from stage 4's graph the tightest height each node can have (shortest paths from the zero node, forward for the
ceiling, backward for the floor: Bellman-Ford with a queue, the graph has no negative cycle) are the box rows' bounds (the set of
feasible heights is unchanged by tightening a box with what the constraints already imply), and the potentials stage 4 found,
heights that satisfy every constraint, are where the iteration starts.

Stopping: the primal residual under TOL_M and the dual one under DUAL_ABS + DUAL_REL of the objective's own forces, or MAX_ITER. The
constraint violation of the answer itself is measured, edge by edge (stats["violation_m"]): where it is over VIOLATION_M the answer
is not given as converged."""
import math
from collections import deque

import numpy as np

import dnp
import gradeshapes as gs
import interchange_audit as ia
import staged_feasible as s4
import staged_ground as s3

TOL_M = 5e-4            # m: the iteration stops when no row is further than this from its bounds (and the dual residual is small)
VIOLATION_M = 2e-3      # m: an answer whose worst constraint is violated by more than this is not given as converged
MAX_ITER = 6000         # sweeps at most (the work is bounded by this, never by a clock)
MAX_RHO = 12            # rescalings of rho at most
BATCH_MIN = 24         # chains of one length class laid side by side from this many (below, one at a time is cheaper)
RHO0 = 10.0
RHO_EQ = 1.0
SIGMA = 1e-6
PCG_MAX = 60
PCG_TOL = 1e-9
PCG_REL = 0.05
DUAL_ABS = 1e-4
DUAL_REL = 1e-4
ALPHA = 1.6             # over-relaxation (Boyd 3.4.3: any value in (0, 2) keeps the convergence)
W_MIN = 1e-3            # least weight of a sample on its target: the kernel's (carriageway_solver.rs W_MIN), the strictly convex part
RHO_EVERY = 25          # sweeps between looks at the residuals' balance


def implied_bounds(g):
    """(floor, ceiling) per node of g (a Graph with no negative cycle): the tightest heights the difference constraints leave a node
    with, relative to the zero node: ceiling_v = shortest path zero -> v, floor_v = - shortest path v -> zero. Lists of g.n."""
    n, Z = g.n, g.zero
    rev = [[] for _ in range(n)]
    for k, v in enumerate(g.ev):
        rev[v].append(k)

    def spfa(source, edges_at, other, forward):
        dist = [math.inf] * n
        dist[source] = 0.0
        queue, inq = deque([source]), [False] * n
        inq[source] = True
        while queue:
            u = queue.popleft()
            inq[u] = False
            du = dist[u]
            for k in edges_at[u]:
                v = other[k]
                nd = du + g.ew[k]
                if nd < dist[v] - ia.EPS_M:
                    dist[v] = nd
                    if not inq[v]:
                        inq[v] = True
                        queue.append(v)
        return dist
    up = spfa(Z, g.adj, g.ev, True)
    down = spfa(Z, rev, g.eu, False)
    return [-d for d in down], up


class Problem:
    """A system's objective and constraints as arrays: chains (offsets), target, weight, stiffness; slabs per pair of nodes; a box per
    node. Built from interchange_audit.Constraints (the constraints stage 4 checked, the shapes' chains)."""

    def __init__(self, cons):
        g = cons.g
        self.cons = cons
        self.N = N = g.n - 1
        Z = g.zero
        self.off = list(cons.off)
        t = np.zeros(N)
        w = np.zeros(N)
        d = np.zeros(N)
        e = np.zeros(N)
        f = np.zeros(N)
        for c, ch in enumerate(cons.chains):
            a, b = self.off[c], self.off[c + 1]
            m = b - a
            t[a:b] = np.asarray(ch["t"], np.float64)
            w[a:b] = np.asarray(ch["w"], np.float64)
            lam = float(ch.get("lam", 0.0))
            if lam > 0.0 and m >= 3:
                # (the second differences' normal matrix: rows (1, -2, 1) at j, j+1, j+2)
                d[a:b - 2] += lam
                d[a + 1:b - 1] += 4.0 * lam
                d[a + 2:b] += lam
                e[a:b - 2] += -2.0 * lam
                e[a + 1:b - 1] += -2.0 * lam
                f[a:b - 2] += lam
        self.t = t
        self.wt = np.maximum(w, W_MIN)
        self.hd = 2.0 * (self.wt + d)       # (the Hessian's three bands: H = 2 (W + lam D2'D2))
        self.he = 2.0 * e
        self.hf = 2.0 * f
        self.h = 2.0 * self.wt * t
        lo = np.full(N, -np.inf)
        hi = np.full(N, np.inf)
        slab = {}
        for k in range(len(g.eu)):
            u, v, wk = g.eu[k], g.ev[k], g.ew[k]
            if u == Z:
                hi[v] = min(hi[v], wk)
            elif v == Z:
                lo[u] = max(lo[u], -wk)
            elif u < v:                     # z_v - z_u <= wk
                s = slab.setdefault((u, v), [-math.inf, math.inf])
                s[1] = min(s[1], wk)
            else:                           # z_v - z_u <= wk with v < u: z_u - z_v >= -wk
                s = slab.setdefault((v, u), [-math.inf, math.inf])
                s[0] = max(s[0], -wk)
        keys = sorted(slab)
        self.A = np.array([a for a, _b in keys], np.int64)
        self.B = np.array([b for _a, b in keys], np.int64)
        self.DLO = np.array([slab[k][0] for k in keys], np.float64)
        self.DHI = np.array([slab[k][1] for k in keys], np.float64)
        self.lo, self.hi = lo, hi

    def objective(self, z):
        """The shapes' objective at z (the kernel's floor on the weights included): sum w (z - t)^2 + lam sum (second differences)^2."""
        val = float(np.sum(self.wt * dnp.sq(z - self.t)))
        for c, ch in enumerate(self.cons.chains):
            a, b = self.off[c], self.off[c + 1]
            lam = float(ch.get("lam", 0.0))
            if lam > 0.0 and b - a >= 3:
                zz = z[a:b]
                val += lam * float(np.sum(dnp.sq(zz[:-2] - 2.0 * zz[1:-1] + zz[2:])))
        return val

    def violation(self, z):
        """The most any edge or box is violated by z (m), and per kind of constraint: (worst, {kind: worst})."""
        g = self.cons.g
        Z = g.zero
        zz = np.append(z, 0.0)
        eu = np.asarray(g.eu, np.int64)
        ev = np.asarray(g.ev, np.int64)
        ew = np.asarray(g.ew, np.float64)
        over = zz[ev] - zz[eu] - ew
        kinds = {}
        for kind in sorted(set(g.kind)):
            sel = np.fromiter((k_ == kind for k_ in g.kind), bool, len(g.kind))
            kinds[kind] = float(max(over[sel].max(), 0.0)) if sel.any() else 0.0
        return float(max(over.max(), 0.0)) if len(over) else 0.0, kinds


class Banded:
    """LDL' of a symmetric pentadiagonal matrix (diagonal d, first band e: i with i+1, second band f: i with i+2; zero across the
    chains' ends): made once, solved by two sweeps. Plain floats in plain order, the same on every platform."""

    def __init__(self, d, e, f):
        n = len(d)
        d, e, f = d.tolist(), e.tolist(), f.tolist()
        D = [0.0] * n
        l1 = [0.0] * n
        l2 = [0.0] * n
        for i in range(n):
            a = b = 0.0
            if i >= 2:
                b = f[i - 2] / D[i - 2]
            if i >= 1:
                a = (e[i - 1] - (b * D[i - 2] * l1[i - 1] if i >= 2 else 0.0)) / D[i - 1]
            l1[i], l2[i] = a, b
            D[i] = d[i] - a * a * (D[i - 1] if i >= 1 else 0.0) - b * b * (D[i - 2] if i >= 2 else 0.0)
            if not D[i] > 0.0:
                raise ValueError("the banded system is not positive definite at %d (pivot %g)" % (i, D[i]))
        self.n, self.D, self.l1, self.l2 = n, D, l1, l2

    def solve(self, rhs):
        n, D, l1, l2 = self.n, self.D, self.l1, self.l2
        y = rhs.tolist()
        for i in range(1, n):                   # (L y = rhs)
            v = y[i] - l1[i] * y[i - 1]
            if i >= 2:
                v -= l2[i] * y[i - 2]
            y[i] = v
        for i in range(n):                      # (D)
            y[i] /= D[i]
        for i in range(n - 2, -1, -1):          # (L' x = y)
            v = y[i] - l1[i + 1] * y[i + 1]
            if i + 2 < n:
                v -= l2[i + 2] * y[i + 2]
            y[i] = v
        return np.array(y, np.float64)


class BandedBatch:
    """The same LDL' as Banded, for a matrix of independent chains (the pieces: nothing couples one to the next), done a sweep for every
    chain at once: the chains of similar length are laid side by side (padded with an identity) and the recurrence runs along them
    in step, so a system of a thousand short pieces costs the length of its longest piece's sweeps, not the sum of all. Each
    chain's arithmetic is the same operations in the same order as Banded's, so the answers agree to the last bit."""

    def __init__(self, d, e, f, off):
        self.n = len(d)
        spans = [(off[c], off[c + 1]) for c in range(len(off) - 1) if off[c + 1] > off[c]]
        buckets = {}
        for a, b in spans:
            buckets.setdefault(max(1, (b - a - 1).bit_length()) if b - a > 1 else 0, []).append((a, b))
        self.buckets = []
        small = []
        for key in sorted(buckets):
            sp = buckets[key]
            Lb = max(b - a for a, b in sp)
            nb = len(sp)
            if nb < BATCH_MIN:
                small += [np.arange(a, b) for a, b in sp]     # (a sweep along each of a few chains costs less one at a time)
                continue
            idx = np.full((Lb, nb), self.n, np.int64)            # (the padding reads index n: a zero)
            for j, (a, b) in enumerate(sp):
                idx[:b - a, j] = np.arange(a, b)
            dp = np.append(d, 1.0)[idx]
            ep = np.append(e, 0.0)[idx]
            fp = np.append(f, 0.0)[idx]
            # (the last sample of a chain has no band after it: the bands of the entry at a chain's end are already zero)
            D = np.zeros((Lb, nb))
            l1 = np.zeros((Lb, nb))
            l2 = np.zeros((Lb, nb))
            for k in range(Lb):
                a_ = np.zeros(nb)
                b_ = np.zeros(nb)
                if k >= 2:
                    b_ = fp[k - 2] / D[k - 2]
                if k >= 1:
                    a_ = (ep[k - 1] - (b_ * D[k - 2] * l1[k - 1] if k >= 2 else 0.0)) / D[k - 1]
                l1[k], l2[k] = a_, b_
                D[k] = dp[k] - a_ * a_ * (D[k - 1] if k >= 1 else 0.0) - b_ * b_ * (D[k - 2] if k >= 2 else 0.0)
            if not (D[idx != self.n] > 0.0).all():
                raise ValueError("the banded system is not positive definite")
            self.buckets.append((idx, D, l1, l2))
        self.small = np.concatenate(small) if small else None
        self.small_fac = Banded(d[self.small], e[self.small], f[self.small]) if small else None

    def solve(self, rhs):
        out = np.zeros(self.n)
        if self.small is not None:
            out[self.small] = self.small_fac.solve(rhs[self.small])
        rp = np.append(rhs, 0.0)
        for idx, D, l1, l2 in self.buckets:
            Lb = idx.shape[0]
            y = rp[idx]
            for k in range(1, Lb):
                v = y[k] - l1[k] * y[k - 1]
                if k >= 2:
                    v = v - l2[k] * y[k - 2]
                y[k] = v
            y = y / D
            for k in range(Lb - 2, -1, -1):
                v = y[k] - l1[k + 1] * y[k + 1]
                if k + 2 < Lb:
                    v = v - l2[k + 2] * y[k + 2]
                y[k] = v
            sel = idx != self.n
            out[idx[sel]] = y[sel]
        return out


class Heights:
    """Stage 5's answer for one system: zs (a height array per piece, as gradeshapes.solve_shape gives them), pieces, samples, stats."""

    def __init__(self):
        self.zs = None
        self.pcs = self.smp = None
        self.stats = {}


def _bandmul(d, e, f, v):
    """The symmetric pentadiagonal matrix (d, e, f) times v."""
    out = d * v
    out[:-1] += e[:-1] * v[1:]
    out[1:] += e[:-1] * v[:-1]
    out[:-2] += f[:-2] * v[2:]
    out[2:] += f[:-2] * v[:-2]
    return out


def _dot(a, b):
    return float(np.add.reduce(a * b))


def admm(P, z0, max_iter=MAX_ITER):
    """ADMM on the problem as OSQP has it (Stellato et al. 2020): minimise 1/2 x'Hx - h'x such that l <= Ax <= u, the rows of A the
    edges' differences and the boxes. Each sweep: solve (H + sigma I + A' diag(rho) A) x~ = sigma x + h + A'(rho z - y) by preconditioned
    conjugate gradients (the banded part of the matrix, the pieces' own bands and the grade edges between neighbours, factored
    once for a rho and used as the preconditioner: the edges between pieces are what it iterates over), z~ = A x~, the relaxed
    (alpha) averages, z <- the relaxed z + y/rho clipped to [l, u], y <- y + rho (relaxed z - z). Convergence: for every rho > 0 the
    residuals go to zero and x to the minimiser (Boyd et al. 2011, section 3.2; the objective is strictly convex by W_MIN, the sets
    closed convex). (z, stats)."""
    N = P.N
    A, B = P.A, P.B
    floor, ceil = implied_bounds(P.cons.g)
    lo = np.maximum(P.lo, np.array(floor[:N], np.float64))
    hi = np.minimum(P.hi, np.array(ceil[:N], np.float64))
    bn = np.flatnonzero(np.isfinite(lo) | np.isfinite(hi))
    m1, m2 = len(A), len(bn)
    Lw = np.r_[P.DLO, lo[bn]]
    Uw = np.r_[P.DHI, hi[bn]]
    eq = (Uw - Lw) <= 1e-9
    chain = np.searchsorted(np.array(P.off[1:], np.int64), np.arange(N), side="right")
    local = (B == A + 1) & (chain[A] == chain[B])
    Al, Bl = A[local], B[local]
    Ac, Bc = A[~local], B[~local]
    cmask = ~local
    x = np.clip(np.asarray(z0, np.float64), lo, hi)
    Ax = np.r_[x[B] - x[A], x[bn]]
    z = np.minimum(np.maximum(Ax, Lw), Uw)
    y = np.zeros(m1 + m2)
    rho = RHO0
    stats_pcg = 0

    def factor(rho):
        rv = np.where(eq, RHO_EQ * rho, rho)
        rp, rb = rv[:m1], rv[m1:]
        d = P.hd + SIGMA
        e = P.he.copy()
        d = d + np.bincount(Al, weights=rp[local], minlength=N) + np.bincount(Bl, weights=rp[local], minlength=N)
        d[bn] += rb
        e[Al] += -rp[local]
        return rv, d, e, BandedBatch(d, e, P.hf, P.off)

    def Aty(w):
        return np.bincount(B, weights=w[:m1], minlength=N) - np.bincount(A, weights=w[:m1], minlength=N) + np.bincount(bn, weights=w[m1:], minlength=N)

    rv, bd, be, fac = factor(rho)
    xt = x.copy()
    rescalings = 0
    it = 0
    r_pri = r_dual = math.inf
    converged = False
    while it < max_iter:
        it += 1
        rhs = SIGMA * x + P.h + Aty(rv * z - y)

        def Kmul(v):
            out = _bandmul(bd, be, P.hf, v)
            if len(Ac):
                w = rv[:m1][cmask] * (v[Bc] - v[Ac])
                out = out + np.bincount(Bc, weights=w, minlength=N) - np.bincount(Ac, weights=w, minlength=N)
            return out
        # (preconditioned conjugate gradients, warm from the last x~)
        r = rhs - Kmul(xt)
        scale = max(float(np.abs(rhs).max(initial=0.0)), 1.0)
        # (inexact solves, as OSQP's indirect solver: as tight as a twentieth of the dual residual, never looser than the floor)
        tol = max(PCG_TOL * scale, PCG_REL * (r_dual if math.isfinite(r_dual) else scale))
        for _k in range(PCG_MAX):
            if float(np.abs(r).max(initial=0.0)) <= tol:
                break
            zz = fac.solve(r)
            if _k == 0:
                p = zz
                rz = _dot(r, zz)
            else:
                rz_new = _dot(r, zz)
                p = zz + (rz_new / rz) * p
                rz = rz_new
            Kp = Kmul(p)
            a = rz / _dot(p, Kp)
            xt = xt + a * p
            r = r - a * Kp
            stats_pcg += 1
        zt = np.r_[xt[B] - xt[A], xt[bn]]
        xr = ALPHA * xt + (1.0 - ALPHA) * x
        zr = ALPHA * zt + (1.0 - ALPHA) * z
        z_new = np.minimum(np.maximum(zr + y / rv, Lw), Uw)
        y = y + rv * (zr - z_new)
        x, z = xr, z_new
        Ax = np.r_[x[B] - x[A], x[bn]]
        Px = _bandmul(P.hd, P.he, P.hf, x)
        ATy = Aty(y)
        r_pri = float(np.abs(Ax - z).max(initial=0.0))
        r_dual = float(np.abs(Px - P.h + ATy).max(initial=0.0))
        eps_dual = DUAL_ABS
        if r_pri <= TOL_M and r_dual <= eps_dual:
            converged = True
            break
        if it % RHO_EVERY == 0 and rescalings < MAX_RHO:
            # (OSQP's rule: rho by the square root of the ratio of the normalised residuals, when it is off by 5x)
            pn = r_pri / max(float(np.abs(Ax).max(initial=0.0)), float(np.abs(z).max(initial=0.0)), 1e-12)
            dn = r_dual / max(float(np.abs(Px).max(initial=0.0)), float(np.abs(ATy).max(initial=0.0)),
                              float(np.abs(P.h).max(initial=0.0)), 1e-12)
            ratio = math.sqrt(pn / max(dn, 1e-30))
            if ratio > 5.0 or ratio < 0.2:
                rho = min(max(rho * ratio, 1e-6), 1e8)
                rv, bd, be, fac = factor(rho)
                rescalings += 1
    stats = dict(iterations=it, pcg_iterations=stats_pcg, rho=rho, rho_rescalings=rescalings, primal_residual_m=r_pri,
                 dual_residual=r_dual, converged=converged)
    return x, stats


def constraints_of(net, sysm, pcs, smp, gl, fixed_z, at=None, spacing="true"):
    """The system's constraint graph and the shapes' chains: interchange_audit.Constraints with the points held on the ground
    (staged_feasible.holds_of). at(member, s) -> the elevation (None: the held points are left out)."""
    fixed_z = s3.anchored(net, fixed_z)         # (a street over a tunnel's footprint is not at the terrain there: staged_ground)
    if at is not None:
        at = s3.anchored(net, at)
    trace = {}
    problem = gs.shape_problem(net, sysm, pcs, smp, gl, fixed_z, trace) + (trace,)
    hs = []
    if at is not None:
        base = ia.Constraints(net, sysm, pcs, smp, gl, fixed_z, spacing, problem)
        ground = type("G", (), {"at": staticmethod(lambda m, s: at(m, s)), "fixed": staticmethod(fixed_z)})
        hs = s4.holds_of(net, sysm, pcs, smp, ground, base.node)
    return s4.Constraints(net, sysm, pcs, smp, gl, fixed_z, spacing, problem, hs)


def solve_constraints(cons):
    """Heights for constraints cons (staged_feasible.Constraints): Heights; stats["feasible"] False where the graph has a negative
    cycle (stage 4 names it): no heights then, zs None."""
    H = Heights()
    H.pcs, H.smp = cons.pcs, cons.smp
    g = cons.g
    dist, cyc = ia._spfa(g)
    if cyc is not None:
        w = math.fsum(g.ew[k] for k in cyc)
        if w < -ia.CONTRADICTION_M:
            H.stats = dict(feasible=False, converged=False, short_m=-w)
            return H
        dist = [0.0] * g.n
    z0 = np.array([dist[v] - dist[g.zero] for v in range(g.n - 1)], np.float64)
    P = Problem(cons)
    z, st = admm(P, z0)
    viol, kinds = P.violation(z)
    H.zs = [z[P.off[c]:P.off[c + 1]].copy() for c in range(len(cons.chains))]
    st.update(feasible=True, nodes=P.N, edges=len(P.A), boxes=int(np.count_nonzero(np.isfinite(P.lo) | np.isfinite(P.hi))),
              objective=P.objective(z), objective_start=P.objective(np.clip(z0, P.lo, P.hi)), violation_m=viol, violation_by_kind=kinds)
    st["joint_gap_m"] = max([kinds.get("join", 0.0), kinds.get("overlap", 0.0)])
    st["converged"] = bool(st["converged"] and viol <= VIOLATION_M)
    st.update(measure(cons, H.zs))
    H.stats = st
    return H


def measure(cons, zs):
    """What the heights are, measured the kernel's way: the worst grade excess (rise over a step's run less the law, as a share of
    the law's rise, per cent), the worst vertical-curve excess (second difference over the kernel's bound, m), the worst clearance
    shortfall (m)."""
    grade = curve = 0.0
    for c, ch in enumerate(cons.chains):
        z = np.asarray(zs[c], np.float64)
        d = np.asarray(cons.smp[c]["d"], np.float64)
        if len(z) >= 2:
            dd = np.maximum(np.diff(d), gs_min_step())
            grade = max(grade, float((np.abs(np.diff(z)) / dd - ch["grade"]).max()))
        if len(z) >= 3:
            step = float(ch["step"])
            bound = step * step / (float(ch["K"]) * 100.0)
            curve = max(curve, float((np.abs(z[:-2] - 2.0 * z[1:-1] + z[2:]) - bound).max()))
    short = 0.0
    for cp in cons.cps:
        a, i, b, j, kind, val = cp[:6]
        if kind == "ge":
            short = max(short, float(val) - (float(zs[a][i]) - float(zs[b][j])))
    return dict(grade_excess=max(grade, 0.0), curve_excess_m=max(curve, 0.0), clearance_shortfall_m=max(short, 0.0))


def gs_min_step():
    return 0.5


def solve_system(net, sysm, ground, spacing="true"):
    """Stage 5 on one system of stage 3 (ground: staged_fixtures.Ground or alike: at, fixed, xy): Heights."""
    pcs = gs.pieces(net, sysm)
    if not pcs:
        H = Heights()
        H.zs, H.pcs, H.smp, H.stats = [], pcs, [], dict(feasible=True, converged=True, iterations=0)
        return H
    smp = gs.sample(net, pcs)
    gl = s4.ground_lists(ground, net, smp)
    cons = constraints_of(net, sysm, pcs, smp, gl, ground.fixed, ground.at, spacing)
    return solve_constraints(cons)


def solve_shape(net, sysm, pcs, smp, ground, fixed_z, diagnostics=None):
    """gradeshapes.solve_shape's signature and answer (a height array per piece, None where there are none), through stage 5: the
    switch BNE_STAGED routes the shapes' heights here for measurement. ground: the elevation under each piece's samples (lists);
    fixed_z(i, s): the height of what the shape does not move, which for a road on the ground is the elevation (the held points
    read it)."""
    def at(m, s):
        z = fixed_z(m, s)
        return np.full(np.shape(s), np.nan) if z is None else z
    cons = constraints_of(net, sysm, pcs, smp, ground, fixed_z, at)
    H = solve_constraints(cons)
    if diagnostics is not None:
        diagnostics.update({k: v for k, v in H.stats.items() if not isinstance(v, dict)})
        diagnostics["staged"] = True
        diagnostics["converged"] = bool(H.stats.get("converged"))
        diagnostics["feasible"] = bool(H.stats.get("feasible"))
    if H.zs is None or not H.stats.get("converged"):
        return None
    return H.zs
