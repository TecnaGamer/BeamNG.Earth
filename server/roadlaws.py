"""The road laws on every road: a cell's finished ground plans under the grade and the vertical curve their speed allows.

Every earlier pass shapes one road, one junction or one interchange; where their results meet the ground roads (a side
road snapped to the road it joins, held flat beside it and let go up its own 20 % bank; a lidar sag) a road could still
turn from one grade to another within a few metres: a 12 m truck bottomed out there. Here the roads of the cell are
one quadratic programme (the Rust kernel of carriageways.solve, as the interchange shapes'):

- each road's pieces, end to end through their joints (the straightest pair at a node, its own road's first), are one
  profile (a run), sampled every STEP_M;
- the profile stays as near the plans as it can (a higher class stiffer: a side road gives way to the road it joins),
  under its laws (gradeshapes.max_grade, gradeshapes.curve_radius: AASHTO's grades for the speed, a vertical curve of
  v^2 / a): a step, a kink, a sag a truck bottoms out in, becomes a curve as long as the speed needs;
- a run's end that meets another road (a T junction, a node of three or more) stands at that road's height there;
- the grade is never held under what the ground itself has (a real mountain road stays as steep as it is, plus
  NATURAL_PLUS): it is the sudden changes of grade the laws take out, not the hills;
- what the passes solved is held (an interchange's shape and its ramps, a bridge's abutment end, a tunnel), and a road
  under a deck may only go down (its clearance stays).

Pure but for the kernel; plans are changed in place (a plan's z replaced)."""
import collections
import logging
import math

import numpy as np

import gradeshapes as _gsh

log = logging.getLogger("earthd.grading")

STEP_M = 4.0                # m between a run's samples
NODE_M = 1.0                # m: plan ends this close are one node
REACH_M = 3.0               # m past the road's carriageway an end still joins it (grading.JUNCTION_REACH_M)
CONT_COS = math.cos(math.radians(35.0))     # two ends going on within this of straight: one road (grading.CONT_TURN_DEG)
NATURAL_WIN_M = 20.0        # m over which the ground's own grade is read
NATURAL_PLUS = 0.02         # a run may be as steep as its ground plus this
W_RANK = 0.5                # a sample's weight: 1 + this x its class rank (gradeshapes' DEFAULT_KMH order)
W_HOLD = 1.0e4              # a held sample's weight (a shape, an abutment end)
RANKS = {"motorway": 9, "trunk": 8, "primary": 7, "secondary": 6, "tertiary": 5, "unclassified": 4, "residential": 3,
         "living_street": 2, "service": 2, "road": 2}
MIN_MOVE_M = 0.005          # m: a plan changed less than this is left as it was

Member = collections.namedtuple("Member", "kind cls kmh")


def laws(spec):
    """(max grade, vertical curve radius m) of a road by its spec (cls, kmh)."""
    m = Member("ground", str(getattr(spec, "cls", None) or "road"), getattr(spec, "kmh", None))
    return _gsh.max_grade(m), _gsh.curve_radius(m)


def _rank(spec):
    c = str(getattr(spec, "cls", None) or "road")
    return RANKS.get(c[:-5] if c.endswith("_link") else c, 1)


def _arc(xy):
    return np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]


def _dir_out(xy, end):
    """Unit direction away from plan end (0: first point, 1: last) along the plan."""
    a, b = (xy[0], xy[min(2, len(xy) - 1)]) if end == 0 else (xy[-1], xy[max(-3, -len(xy))])
    d = b - a
    n = float(np.hypot(*d))
    return d / n if n > 1e-9 else np.zeros(2)


def runs(xys, specs, rids):
    """The plans end to end through their joints: [[(plan, reversed)]], and the nodes [[(plan, end)]] of their ends.
    At a node the ends are paired straightest first (the same road's first) while within CONT_COS of straight."""
    n = len(xys)
    ends = [(i, e) for i in range(n) if len(xys[i]) >= 2 for e in (0, 1)]
    pts = np.array([xys[i][0 if e == 0 else -1] for i, e in ends]).reshape(-1, 2)
    parent = list(range(len(ends)))

    def root(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    cells = collections.defaultdict(list)
    for k, p in enumerate(pts):
        cells[(int(p[0] // NODE_M), int(p[1] // NODE_M))].append(k)
    for (cx, cy), ks in cells.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for k2 in cells.get((cx + dx, cy + dy), ()):
                    for k in ks:
                        if k < k2 and float(np.hypot(*(pts[k] - pts[k2]))) <= NODE_M:
                            parent[root(k)] = root(k2)
    groups = collections.defaultdict(list)
    for k in range(len(ends)):
        groups[root(k)].append(k)
    nodes = [[ends[k] for k in ks] for ks in groups.values() if len(ks) >= 2]
    nxt = {}                                # (plan, end) -> (plan, end) it goes on into
    for node in nodes:
        cand = []
        for a in range(len(node)):
            for b in range(a + 1, len(node)):
                (i, e), (j, f) = node[a], node[b]
                if i == j:
                    continue
                c = -float(_dir_out(xys[i], e) @ _dir_out(xys[j], f))
                if c >= CONT_COS:
                    cand.append((rids[i] != rids[j] or rids[i] < 0, -c, node[a], node[b]))
        for _same, _c, x, y in sorted(cand, key=lambda q: (q[0], q[1])):
            if x not in nxt and y not in nxt:
                nxt[x], nxt[y] = y, x
    out, seen = [], set()
    starts = [i for i in range(n) if len(xys[i]) >= 2 and ((i, 0) not in nxt or (i, 1) not in nxt)]
    starts += [i for i in range(n) if len(xys[i]) >= 2 and i not in starts]     # (loops last: broken anywhere)
    for i in starts:
        if i in seen:
            continue
        rev = (i, 0) in nxt and (i, 1) not in nxt      # (start from its free end)
        run, cur = [], (i, rev)
        while cur[0] not in seen:
            seen.add(cur[0])
            run.append(cur)
            leave = (cur[0], 0 if cur[1] else 1)       # (the end it is left by)
            if leave not in nxt:
                break
            j, f = nxt[leave]
            cur = (j, f == 1)                           # (entered at its end f: reversed if that is its last point)
        out.append(run)
    return out, nodes


def _nearest(xy, P):
    """(segment index, t, distance) of point P's nearest point on polyline xy."""
    a, b = xy[:-1], xy[1:]
    ab = b - a
    t = np.clip(((P - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
    d = np.hypot(*(a + ab * t[:, None] - P).T)
    k = int(np.argmin(d))
    return k, float(t[k]), float(d[k])


def apply(plans, specs, held=None, under=None, skip=(), diagnostics=None):
    """plans: [Plan] (xy (n, 2+), z (n,), road_half, abut, rid), specs alike (cls, kmh; None: left out); held(i) ->
    bool (n,) of plan i's held samples (or None), under(i) -> bool (n,) of those under a deck (only down there); skip:
    plan indices left as they are, held whole (tunnels). Changes the plans' z in place; returns how many changed."""
    idx = [i for i, sp in enumerate(specs) if sp is not None and len(plans[i].xy) >= 2]
    if not idx:
        return 0
    xys = [np.asarray(plans[i].xy, np.float64)[:, :2] for i in idx]
    zs = [np.asarray(plans[i].z, np.float64) for i in idx]
    sps = [specs[i] for i in idx]
    rids = [int(getattr(plans[i], "rid", -1)) for i in idx]
    skip = set(skip)
    hold = []
    for k, i in enumerate(idx):
        h = np.zeros(len(zs[k]), bool)
        if i in skip:
            h[:] = True
        elif held is not None:
            got = held(i)
            if got is not None:
                h |= np.asarray(got, bool)
        ab = getattr(plans[i], "abut", (False, False))
        h[0] |= bool(ab[0])
        h[-1] |= bool(ab[1])
        hold.append(h)
    down = [np.asarray(under(i), bool) if under is not None and under(i) is not None else np.zeros(len(zs[k]), bool)
            for k, i in enumerate(idx)]
    rs, nodes = runs(xys, sps, rids)
    chains, where = [], []          # where[c]: [(plan k, reversed, s0 in the run)] of chain c
    samp = []                       # per chain: run arc length of its samples
    for run in rs:
        xy_r, z_r, h_r, d_r, s_r, parts = [], [], [], [], [], []
        s0 = 0.0
        for k, rev in run:
            xy, z, h, d = xys[k], zs[k], hold[k], down[k]
            if rev:
                xy, z, h, d = xy[::-1], z[::-1], h[::-1], d[::-1]
            s = _arc(xy) + s0
            if xy_r:                                    # (the joint: one point, the first part's)
                xy, z, h, d, s = xy[1:], z[1:], h[1:], d[1:], s[1:]
            parts.append((k, rev, s0))
            xy_r.append(xy)
            z_r.append(z)
            h_r.append(h)
            d_r.append(d)
            s_r.append(s)
            s0 = float(s[-1]) if len(s) else s0
        S = np.concatenate(s_r)
        Z = np.concatenate(z_r)
        H = np.concatenate(h_r)
        D = np.concatenate(d_r)
        L = float(S[-1] - S[0])
        m = max(2, int(math.ceil(L / STEP_M)) + 1)
        sv = np.linspace(S[0], S[-1], m)
        t = np.interp(sv, S, Z)
        # (a sample is held / only down where a plan point within half a step of it is)
        near = np.abs(sv[:, None] - S[None, :]) <= (L / max(m - 1, 1)) / 2 + 1e-6 if len(S) * m < 4e6 else None
        hs = (near & H[None, :]).any(1) if near is not None else np.interp(sv, S, H.astype(float)) > 0.5
        ds = (near & D[None, :]).any(1) if near is not None else np.interp(sv, S, D.astype(float)) > 0.5
        g_law, rad = 0.0, 0.0
        rank = 0
        for k, _rev in run:
            g, r = laws(sps[k])
            g_law, rad, rank = max(g_law, g), max(rad, r), max(rank, _rank(sps[k]))
        step = L / max(m - 1, 1)
        if m >= 3 and L > 1e-6:
            w_n = max(1, int(round(NATURAL_WIN_M / max(step, 1e-6))))
            nat = np.abs(t[w_n:] - t[:-w_n]) / (w_n * step) if m > w_n else np.abs(np.diff(t)) / step
            g = max(g_law, float(nat.max()) + NATURAL_PLUS if len(nat) else g_law)
        else:
            g = max(g_law, float(abs(t[-1] - t[0]) / max(L, 1e-6)) + NATURAL_PLUS)
        w = np.where(hs, W_HOLD, 1.0 + W_RANK * rank)
        hi = np.where(ds, t, np.inf)
        chains.append(dict(t=t, w=w, lo=np.full(m, -np.inf), hi=hi, grade=g, K=rad / 100.0, lam=1e-3, step=step,
                           length=0.0))
        where.append(parts)
        samp.append(sv)
    loc = {}                        # plan k -> (chain, reversed, s0)
    for c, parts in enumerate(where):
        for k, rev, s0 in parts:
            loc[k] = (c, rev, s0)

    def sample_of(k, s_plan):
        """(chain, sample index) of plan k's point at its own arc length s_plan."""
        c, rev, s0 = loc[k]
        Lk = float(_arc(xys[k])[-1])
        s = s0 + (Lk - s_plan if rev else s_plan)
        return c, int(np.argmin(np.abs(samp[c] - s)))
    cps = []
    paired = set()
    for c, parts in enumerate(where):
        for (k, rev, _s0), (k2, _r2, _s2) in zip(parts, parts[1:]):
            paired.add((k, 0 if rev else 1))
            paired.add((k2, 1 if _r2 else 0))
    # (a node's ends not going on into each other: one height, the first's)
    for node in nodes:
        free = [(k, e) for k, e in node if (k, e) not in paired]
        anchor = node[0]
        for k, e in free:
            if (k, e) == anchor:
                continue
            a = sample_of(anchor[0], 0.0 if anchor[1] == 0 else float(_arc(xys[anchor[0]])[-1]))
            b = sample_of(k, 0.0 if e == 0 else float(_arc(xys[k])[-1]))
            if a != b:
                cps.append((b[0], b[1], a[0], a[1], "eq", 0.0))
    # (a run's end on another road's line, not at its end: a T junction, at that road's height there)
    in_node = {x for node in nodes for x in node}
    boxes = [(xy.min(0), xy.max(0)) for xy in xys]
    for k in range(len(xys)):
        for e in (0, 1):
            if (k, e) in paired or (k, e) in in_node:
                continue
            P = xys[k][0 if e == 0 else -1]
            best = None
            for j in range(len(xys)):
                if j == k or loc[j][0] == loc[k][0]:
                    continue
                reach = float(getattr(plans[idx[j]], "road_half", 0.0) or 0.0) + REACH_M
                lo, hi_ = boxes[j]
                if (P < lo - reach).any() or (P > hi_ + reach).any():
                    continue
                seg, tt, d = _nearest(xys[j], P)
                if d <= reach and (best is None or d < best[0]):
                    a = _arc(xys[j])
                    best = (d, j, float(a[seg] + tt * (a[seg + 1] - a[seg])))
            if best is None:
                continue
            a = sample_of(best[1], best[2])
            b = sample_of(k, 0.0 if e == 0 else float(_arc(xys[k])[-1]))
            cps.append((b[0], b[1], a[0], a[1], "eq", 0.0))
    import carriageways as _cw
    info = {}
    try:
        out = _cw.solve(chains, cps, diagnostics=info)
    except Exception as e:          # (the plans as the passes left them)
        log.warning("grading: road laws: %s: %s", type(e).__name__, e)
        return 0
    if diagnostics is not None:
        diagnostics.update(info)
        diagnostics.update(chains=len(chains), couplings=len(cps), samples=int(sum(len(c["t"]) for c in chains)))
    if not info.get("feasible", True):
        return 0
    changed = 0
    for k, i in enumerate(idx):
        if k not in loc or i in skip:
            continue
        c, rev, s0 = loc[k]
        sk = _arc(xys[k])
        s = s0 + (sk[-1] - sk if rev else sk)
        new_t = np.interp(s, samp[c], np.asarray(out[c], np.float64))
        old_t = np.interp(s, samp[c], chains[c]["t"])
        z = zs[k] + (new_t - old_t)                     # (the change, not the resampled profile: the plan's own detail)
        z = np.where(hold[k], zs[k], z)
        z = np.where(down[k], np.minimum(z, zs[k]), z)
        if float(np.abs(z - zs[k]).max()) >= MIN_MOVE_M:
            plans[i].z = z
            changed += 1
    return changed
