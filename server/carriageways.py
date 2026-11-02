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
where the limits disagree with each other, the clearance gives way last.

The inequalities are met by an active set: violated ones become stiff quadratic penalties and the problem is solved
again (each chain's matrix is banded, the couplings between chains few: Cholesky per chain, Woodbury for the couplings).
"""
import math
import os

import numpy as np

STEP_M = 4.0            # m between profile samples
TRUST_M = 40.0          # m from a bridge's end over which the ground's weight rises from 0 to 1
LAMBDA = 2.0e4          # smoothness (a curve of ~40 m per metre of bend where nothing else decides)
MU = 1.0e6              # the stiffness of an active limit
DENSE_N = 250           # chains up to this many samples solved densely (LAPACK), longer ones banded
W_MIN = 1.0e-3          # the least weight of a sample's ground
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
ROUNDS = 40             # active-set rounds at most
TOUCH_M = 0.75          # m: parts whose ends are this close are joined
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
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(ln, axis=0).T))]
        m = max(2, int(math.ceil(s[-1] / step)) + 1)
        a = np.linspace(0.0, s[-1], m)
        pts = np.stack([np.interp(a, s, ln[:, 0]), np.interp(a, s, ln[:, 1])], axis=1)
        if xs:
            pts = pts[1:]                               # (the joint once)
        xs.append(pts)
        pi += [i] * len(pts)
        br += [bool(parts[i][2])] * len(pts)
    xy = np.concatenate(xs)
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    pi, br = np.array(pi), np.array(br)
    ch = np.flatnonzero(br[1:] != br[:-1])
    cut = np.r_[s[ch], s[ch + 1]] if len(ch) else np.zeros(0)
    from_end = np.min(np.abs(s[:, None] - cut[None, :]), axis=1) if len(cut) else np.full(len(s), np.inf)
    return xy, s, pi, br, from_end


def chains(parts):
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
        while b_i < len(order_e) and xs[b_i] - xs[a_i] <= TOUCH_M:
            f = order_e[b_i]
            if np.hypot(*(ends[e] - ends[f])) <= TOUCH_M and e // 2 != f // 2:
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


def weights(br, from_end, trust=TRUST_M):
    """0 on a bridge, rising (smoothstep) to 1 trust m from a bridge's end on the ground. Pure."""
    u = np.clip(from_end / trust, 0.0, 1.0)
    w = u * u * (3.0 - 2.0 * u)
    return np.where(br, 0.0, w)


class _Chain:
    """One chain's banded system: bands A0 (n), A1 (n-1), A2 (n-2) and right-hand side b."""

    def __init__(self, t, w, lam, h):
        n = len(t)
        self.n, self.h = n, h
        self.t, self.w = np.asarray(t, np.float64), np.asarray(w, np.float64)
        self.lam = lam
        self.length = 0.0
        self.rows1 = {}     # k -> (target of z[k+1] - z[k])
        self.rows2 = {}     # k -> (target of z[k] - 2 z[k+1] + z[k+2])
        self.pins = {}      # i -> height

    def assemble(self):
        n = self.n
        A0 = self.w.copy()
        A1 = np.zeros(max(n - 1, 0))
        A2 = np.zeros(max(n - 2, 0))
        b = self.w * self.t
        if n >= 2 and self.length > 0:
            m = self.length
            k = np.arange(n - 1)
            np.add.at(A0, k, m)
            np.add.at(A0, k + 1, m)
            np.add.at(A1, k, -m)
        if n >= 3:
            lam = self.lam
            k = np.arange(n - 2)
            np.add.at(A0, k, lam)
            np.add.at(A0, k + 1, 4 * lam)
            np.add.at(A0, k + 2, lam)
            np.add.at(A1, k, -2 * lam)
            np.add.at(A1, k + 1, -2 * lam)
            np.add.at(A2, k, lam)
        for k, y in self.rows1.items():
            A0[k] += MU
            A0[k + 1] += MU
            A1[k] -= MU
            b[k] -= MU * y
            b[k + 1] += MU * y
        for k, y in self.rows2.items():
            A0[k] += MU
            A0[k + 1] += 4 * MU
            A0[k + 2] += MU
            A1[k] -= 2 * MU
            A1[k + 1] -= 2 * MU
            A2[k] += MU
            b[k] += MU * y
            b[k + 1] -= 2 * MU * y
            b[k + 2] += MU * y
        for i, y in self.pins.items():
            A0[i] += MU
            b[i] += MU * y
        self.b = b
        self._factor(A0, A1, A2)

    def _factor(self, A0, A1, A2):
        n = self.n
        if n <= DENSE_N:
            A = np.diag(A0)
            if n > 1:
                A += np.diag(A1, 1) + np.diag(A1, -1)
            if n > 2:
                A += np.diag(A2, 2) + np.diag(A2, -2)
            self.A = A
            return
        self.A = None
        l0, l1, l2 = np.zeros(n), np.zeros(n), np.zeros(n)
        a0, a1, a2 = A0.tolist(), A1.tolist(), A2.tolist()
        L0, L1, L2 = [0.0] * n, [0.0] * n, [0.0] * n
        for i in range(n):
            if i >= 2:
                L2[i] = a2[i - 2] / L0[i - 2]
            if i >= 1:
                L1[i] = (a1[i - 1] - (L2[i] * L1[i - 1] if i >= 2 else 0.0)) / L0[i - 1]
            L0[i] = math.sqrt(max(a0[i] - L1[i] * L1[i] - L2[i] * L2[i], 1e-12))
        self.l0, self.l1, self.l2 = np.array(L0), np.array(L1), np.array(L2)

    def solve(self, r):
        """B^-1 r for r (n,) or (n, m): one LAPACK call for a short chain, the banded Cholesky's two sweeps (every
        right-hand side at once) for a long one."""
        r = np.asarray(r, np.float64)
        if self.A is not None:
            return np.linalg.solve(self.A, r)
        one = r.ndim == 1
        r = r.reshape(self.n, -1)
        l0, l1, l2 = self.l0, self.l1, self.l2
        n = self.n
        y = np.empty_like(r)
        for i in range(n):
            acc = r[i]
            if i >= 1:
                acc = acc - l1[i] * y[i - 1]
            if i >= 2:
                acc = acc - l2[i] * y[i - 2]
            y[i] = acc / l0[i]
        z = np.empty_like(r)
        for i in range(n - 1, -1, -1):
            acc = y[i]
            if i + 1 < n:
                acc = acc - l1[i + 1] * z[i + 1]
            if i + 2 < n:
                acc = acc - l2[i + 2] * z[i + 2]
            z[i] = acc / l0[i]
        return z[:, 0] if one else z


def solve(chains, couplings=(), rounds=ROUNDS):
    """The cluster's profiles (see the module). chains: [dict(t (n,), w (n,), grade, K, lo (n,) or None, hi (n,) or
    None, lam=LAMBDA, step=STEP_M, length=LENGTH_BIAS)]; couplings: [(a, i, b, j, kind, value)]: kind "eq" (z_a[i] = z_b[j]) or "ge"
    (z_a[i] - z_b[j] >= value). Returns [z (n,)]. Pure."""
    cs = []
    for c in chains:
        h = float(c.get("step", STEP_M))
        # (a little weight everywhere: a chain all on bridges or under them stays defined, near the ground's line)
        ch = _Chain(c["t"], np.maximum(np.asarray(c["w"], np.float64), W_MIN), float(c.get("lam", LAMBDA)), h)
        ch.length = float(c.get("length", LENGTH_BIAS))
        ch.g = float(c["grade"]) * h
        ch.c = h * h / (float(c["K"]) * 100.0)
        ch.lo = None if c.get("lo") is None else np.asarray(c["lo"], np.float64)
        ch.hi = None if c.get("hi") is None else np.asarray(c["hi"], np.float64)
        cs.append(ch)
    off = np.r_[0, np.cumsum([c.n for c in cs])].astype(int)
    eqs = [(a, i, b, j, 0.0) for a, i, b, j, kind, v in couplings if kind == "eq"]
    ges = [(a, i, b, j, float(v)) for a, i, b, j, kind, v in couplings if kind == "ge"]
    active_ge = set()
    zs = None
    for _round in range(rounds):
        for c in cs:
            c.assemble()
        cols = [(a, i, b, j, v) for a, i, b, j, v in eqs] + [ges[k] for k in sorted(active_ge)]
        # z = B^-1 r - B^-1 U (M^-1 + U' B^-1 U)^-1 U' B^-1 r, U's columns e_(a,i) - e_(b,j), M = MU I
        r = [c.b.copy() for c in cs]
        for a, i, b, j, v in cols:
            r[a][i] += MU * v
            r[b][j] -= MU * v
        z0 = [c.solve(rr) for c, rr in zip(cs, r)]
        if cols:
            k = len(cols)
            E = [np.zeros((c.n, k)) for c in cs]
            for m, (a, i, b, j, v) in enumerate(cols):
                E[a][i, m] += 1.0
                E[b][j, m] -= 1.0
            BU = [cs[n].solve(E[n]) if E[n].any() else E[n] for n in range(len(cs))]
            UtBU = np.zeros((k, k))
            Utz = np.zeros(k)
            for m, (a, i, b, j, v) in enumerate(cols):
                UtBU[m] = BU[a][i] - BU[b][j]
                Utz[m] = z0[a][i] - z0[b][j]
            y = np.linalg.solve(np.eye(k) / MU + UtBU, Utz)
            zs = [z0[n] - BU[n] @ y for n in range(len(cs))]
        else:
            zs = z0
        changed = False
        # release the limits the solution keeps inside of by itself (held at a limit it no longer pushes against: a
        # bend forced there in an early round stayed and bowed the road up 10 m)
        for c, z in zip(cs, zs):
            d1 = np.diff(z)
            for k in [k for k, y in c.rows1.items() if (d1[k] - y) * np.sign(y) < -1e-7]:
                del c.rows1[k]
                changed = True
            if c.n >= 3:
                d2 = z[:-2] - 2 * z[1:-1] + z[2:]
                for k in [k for k, y in c.rows2.items() if (d2[k] - y) * np.sign(y) < -1e-9]:
                    del c.rows2[k]
                    changed = True
            for i in list(c.pins):
                y = c.pins[i]
                if (c.lo is not None and np.isfinite(c.lo[i]) and y == c.lo[i] and z[i] > y + 1e-6) or \
                        (c.hi is not None and np.isfinite(c.hi[i]) and y == c.hi[i] and z[i] < y - 1e-6):
                    del c.pins[i]
                    changed = True
        for m in [m for m in active_ge if zs[ges[m][0]][ges[m][1]] - zs[ges[m][2]][ges[m][3]] > ges[m][4] + 1e-6]:
            active_ge.discard(m)
            changed = True
        for c, z in zip(cs, zs):
            d1 = np.diff(z)
            for k in np.flatnonzero(np.abs(d1) > c.g * 1.02 + 1e-3):
                if k not in c.rows1:
                    c.rows1[int(k)] = float(np.sign(d1[k]) * c.g)
                    changed = True
            if c.n >= 3:
                d2 = z[:-2] - 2 * z[1:-1] + z[2:]
                for k in np.flatnonzero(np.abs(d2) > c.c * 1.05 + 1e-4):
                    if k not in c.rows2:
                        c.rows2[int(k)] = float(np.sign(d2[k]) * c.c)
                        changed = True
            if c.lo is not None:
                for i in np.flatnonzero(z < c.lo - 0.02):
                    if int(i) not in c.pins:
                        c.pins[int(i)] = float(c.lo[i])
                        changed = True
            if c.hi is not None:
                for i in np.flatnonzero(z > c.hi + 0.02):
                    if int(i) not in c.pins:
                        c.pins[int(i)] = float(c.hi[i])
                        changed = True
        for m, (a, i, b, j, v) in enumerate(ges):
            if m not in active_ge and zs[a][i] - zs[b][j] < v - 0.02:
                active_ge.add(m)
                changed = True
        if not changed:
            break
    return zs
