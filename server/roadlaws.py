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
- a run's end that meets another road (a T junction, a node of three or more) stands at that road's height there and
  leaves it at that road's grade its way (a landing: the junction's surface goes on into it, no kink at the mouth),
  its last APPROACH_M driven at JUNCTION_KMH (a vertical curve that speed's: a side road climbing its bank from the
  junction turns up over ~10 m, not a 12 m truck's length of 0 to 12 % in 4 m, nor a 50 km/h road's 80 m of fill);
- the grade is never held under what the ground itself has (a real mountain road stays as steep as it is, plus
  NATURAL_PLUS; read from the plans' natural profiles, not from what the passes made of them: a 24 m cliff an
  interchange's ramp left is no hill to keep): it is the sudden changes of grade the laws take out, not the hills;
- each connected network of roads is solved on its own (a conflict at one junction of a city, an infeasible
  programme, left 900 roads of the cell as the passes made them), and one that is infeasible again without its
  landings, then without its grade law (the curves alone), before it is left as it was;
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
APPROACH_M = 40.0           # m of a road before a junction it meets driven at JUNCTION_KMH (its curve law that speed's)
JUNCTION_KMH = 20.0         # km/h: turning in or out of a junction
LANDING_TOL = 0.03          # a road leaves a junction at the grade of the road it joins its way, within this (camber)

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


def _solve_networks(chains, ccps):
    """The chains' profiles ([array or None: left as it was]), each connected network (chains joined by couplings)
    solved on its own: as it is, else without its landings (the "ge" couplings), else without its grade law too.
    Returns (profiles, stats)."""
    import carriageways as _cw
    n = len(chains)
    parent = list(range(n))

    def root(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for a, _i, b, _j, _k, _v in ccps:
        parent[root(a)] = root(b)
    groups = collections.defaultdict(list)
    for c in range(n):
        groups[root(c)].append(c)
    out = [None] * n
    stats = dict(networks=len(groups), relaxed=0, infeasible=0, feasible=True, ok_chain=[False] * n)
    for members in groups.values():
        pos = {c: q for q, c in enumerate(members)}
        cps = [(pos[a], i, pos[b], j, k, v) for a, i, b, j, k, v in ccps if a in pos]
        for level in (0, 1, 2):
            sub = [dict(chains[c]) for c in members]
            sc = cps
            if level >= 1:
                sc = [c for c in cps if c[4] != "ge"]
            if level >= 2:
                for ch in sub:
                    ch["grade"] = 1.0
            info = {}
            try:
                got = _cw.solve(sub, sc, diagnostics=info)
            except Exception as e:      # (this network as the passes left it)
                log.warning("grading: road laws: %s: %s", type(e).__name__, e)
                got, info = None, {"feasible": False}
            if got is not None and info.get("feasible", True) and info.get("converged", True):
                for c, z in zip(members, got):
                    out[c] = z
                    stats["ok_chain"][c] = True
                if level:
                    stats["relaxed"] += 1
                break
        else:
            stats["infeasible"] += 1
    return out, stats


def apply(plans, specs, held=None, under=None, skip=(), diagnostics=None, natural=None):
    """plans: [Plan] (xy (n, 2+), z (n,), road_half, abut, rid), specs alike (cls, kmh; None: left out); held(i) ->
    bool (n,) of plan i's held samples (or None), under(i) -> bool (n,) of those under a deck (only down there); skip:
    plan indices left as they are, held whole (tunnels); natural(i) -> (n,) plan i's natural profile (the heights
    before the passes; None: its heights now). Changes the plans' z in place; returns how many changed."""
    idx = [i for i, sp in enumerate(specs) if sp is not None and len(plans[i].xy) >= 2]
    if not idx:
        return 0
    xys = [np.asarray(plans[i].xy, np.float64)[:, :2] for i in idx]
    zs = [np.asarray(plans[i].z, np.float64) for i in idx]
    nats = []
    for k, i in enumerate(idx):
        got = natural(i) if natural is not None else None
        nats.append(np.asarray(got, np.float64) if got is not None and len(got) == len(zs[k]) else zs[k])
    sps = [specs[i] for i in idx]
    rids = [int(getattr(plans[i], "rid", -1)) for i in idx]
    arcs = [_arc(xy) for xy in xys]
    skip = set(skip)
    hold, down = [], []
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
        got = under(i) if under is not None else None
        down.append(np.asarray(got, bool) if got is not None else np.zeros(len(zs[k]), bool))
    rs, nodes = runs(xys, sps, rids)
    R = []                          # per run: its samples, targets, weights, bounds and laws
    loc = {}                        # plan k -> (run, reversed, s0)
    for r, run in enumerate(rs):
        z_r, h_r, d_r, s_r, n_r = [], [], [], [], []
        s0 = 0.0
        for k, rev in run:
            z, h, d, s, nz = zs[k], hold[k], down[k], arcs[k], nats[k]
            if rev:
                z, h, d, s, nz = z[::-1], h[::-1], d[::-1], s[-1] - s[::-1], nz[::-1]
            s = s + s0
            if z_r:                                     # (the joint: one point, the first part's)
                z, h, d, s, nz = z[1:], h[1:], d[1:], s[1:], nz[1:]
            loc[k] = (r, rev, s0)
            z_r.append(z)
            h_r.append(h)
            d_r.append(d)
            s_r.append(s)
            n_r.append(nz)
            s0 = float(s[-1]) if len(s) else s0
        S, Z, H, D, NZ = (np.concatenate(a) for a in (s_r, z_r, h_r, d_r, n_r))
        L = float(S[-1] - S[0])
        m = max(2, int(math.ceil(L / STEP_M)) + 1)
        sv = np.linspace(S[0], S[-1], m)
        step = L / max(m - 1, 1)
        t = np.interp(sv, S, Z)
        j = np.clip(np.searchsorted(S, sv), 0, len(S) - 1)      # (a sample is held / only down where a plan point
        hs = np.zeros(m, bool)                                   # within half a step of it is)
        ds = np.zeros(m, bool)
        for jj in (j, np.clip(j - 1, 0, len(S) - 1)):
            ok = np.abs(S[jj] - sv) <= step / 2 + 1e-6
            hs |= ok & H[jj]
            ds |= ok & D[jj]
        g_law = rad = 0.0
        rank = 0
        for k, _rev in run:
            g, rr = laws(sps[k])
            g_law, rad, rank = max(g_law, g), max(rad, rr), max(rank, _rank(sps[k]))
        w_n = max(1, int(round(NATURAL_WIN_M / max(step, 1e-6))))
        tn = np.interp(sv, S, NZ)                       # (the ground's own grade: the natural profile's)
        nat = np.abs(tn[w_n:] - tn[:-w_n]) / (w_n * step) if m > w_n else np.abs(np.diff(tn)) / max(step, 1e-6)
        g = max(g_law, float(nat.max()) + NATURAL_PLUS if len(nat) else g_law)
        R.append(dict(sv=sv, t=t, w=np.where(hs, W_HOLD, 1.0 + W_RANK * rank), hi=np.where(ds, t, np.inf), grade=g,
                      rad=rad, step=step, junction=[False, False], parts=[(k, rev) for k, rev in run]))

    def sample_of(k, s_plan):
        """(run, sample) of plan k's point at its own arc length s_plan."""
        r, rev, s0 = loc[k]
        s = s0 + (arcs[k][-1] - s_plan if rev else s_plan)
        return r, int(np.argmin(np.abs(R[r]["sv"] - s)))

    def run_dir(r, i):
        """Unit direction of run r at its sample i (the way its samples go) and its grade there (as planned)."""
        s = R[r]["sv"][i]
        for k, _rev in R[r]["parts"]:
            _r, rev, s0 = loc[k]
            if s0 - 1e-6 <= s <= s0 + arcs[k][-1] + 1e-6:
                sp = arcs[k][-1] - (s - s0) if rev else s - s0
                seg = int(np.clip(np.searchsorted(arcs[k], sp) - 1, 0, len(arcs[k]) - 2))
                d = xys[k][seg + 1] - xys[k][seg]
                d = d / max(float(np.hypot(*d)), 1e-9)
                sv, t = R[r]["sv"], R[r]["t"]
                a, b = max(0, i - 1), min(len(sv) - 1, i + 1)
                return (-d if rev else d), float((t[b] - t[a]) / max(sv[b] - sv[a], 1e-6))
        return None, 0.0

    cps = []                        # in the runs' terms: (run a, sample, run b, sample, kind, value): z_a - z_b (= / >=) v

    def end_sample(k, e):
        return sample_of(k, 0.0 if e == 0 else float(arcs[k][-1]))

    landed = []                     # (plan, the plan it lands on): its ground need not yield to that one's (grading)

    def land(k, e, main, onto):
        """Plan k's end e meets main (run, sample): one height there, and it leaves at main's grade its way."""
        r, i = end_sample(k, e)
        ra, ia = main
        if (r, i) == (ra, ia):
            return
        cps.append((r, i, ra, ia, "eq", 0.0))
        m = len(R[r]["sv"])
        if i not in (0, m - 1) or m < 3 or r == ra:
            return
        R[r]["junction"][0 if i == 0 else 1] = True
        landed.append((idx[k], idx[onto], r))
        md, mg = run_dir(ra, ia)
        if md is None:
            return
        g_way = mg * float(_dir_out(xys[k], e) @ md)   # (the joined road's surface rising along the way out of it)
        j = 1 if i == 0 else m - 2
        st = abs(float(R[r]["sv"][j] - R[r]["sv"][i]))
        cps.append((r, j, r, i, "ge", (g_way - LANDING_TOL) * st))
        cps.append((r, i, r, j, "ge", -(g_way + LANDING_TOL) * st))
    paired = set()
    for run in rs:
        for (k, rev), (k2, rev2) in zip(run, run[1:]):
            paired.add((k, 0 if rev else 1))
            paired.add((k2, 1 if rev2 else 0))
    for node in nodes:              # (its ends not going on into another: onto the road through it, or the top one's)
        through = [x for x in node if x in paired]
        free = [x for x in node if x not in paired]
        if through:
            main, onto = end_sample(*through[0]), through[0][0]
        else:
            free.sort(key=lambda x: -_rank(sps[x[0]]))
            main, onto = end_sample(*free[0]), free[0][0]
            free = free[1:]
        for k, e in free:
            land(k, e, main, onto)
    in_node = {x for node in nodes for x in node}
    boxes = [(xy.min(0), xy.max(0)) for xy in xys]
    for k in range(len(xys)):       # (an end on another road's line, not at its end: a T junction)
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
                    best = (d, j, float(arcs[j][seg] + tt * (arcs[j][seg + 1] - arcs[j][seg])))
            if best is not None:
                land(k, e, sample_of(best[1], best[2]), best[1])
    # (each run as chains: its ends at junctions driven at JUNCTION_KMH, the rest at its speed; the pieces overlap by
    # two samples held equal, so the grade goes on through the change of law)
    rad_j = laws(Member("ground", "road", JUNCTION_KMH))[1]
    chains, where = [], []          # where[run]: [(chain, its first run sample)]
    ccps = []
    for r, x in enumerate(R):
        m = len(x["sv"])
        na = int(round(APPROACH_M / max(x["step"], 1e-6)))
        a0 = na if x["junction"][0] else 0
        a1 = m - 1 - na if x["junction"][1] else m - 1
        if x["rad"] <= rad_j or not (x["junction"][0] or x["junction"][1]):
            cuts = [(0, m - 1, x["rad"])]
        elif a1 - a0 < 3:
            cuts = [(0, m - 1, rad_j)]
        else:
            cuts = ([(0, a0 + 1, rad_j)] if a0 > 0 else []) + [(a0, a1, x["rad"])] + \
                   ([(a1 - 1, m - 1, rad_j)] if a1 < m - 1 else [])
        where.append([])
        for a, b, rr in cuts:
            sl = slice(a, b + 1)
            chains.append(dict(t=x["t"][sl], w=x["w"][sl], lo=np.full(b - a + 1, -np.inf), hi=x["hi"][sl],
                               grade=x["grade"], K=rr / 100.0, lam=1e-3, step=x["step"], length=0.0))
            where[r].append((len(chains) - 1, a, b))
        for (c1, a1_, b1_), (c2, a2_, _b2) in zip(where[r], where[r][1:]):
            for q in range(a2_, b1_ + 1):               # (the overlap: both pieces' samples equal)
                ccps.append((c1, q - a1_, c2, q - a2_, "eq", 0.0))

    def chain_of(r, i):
        for c, a, b in where[r]:
            if a <= i <= b:
                return c, i - a
        c, a, _b = where[r][-1]
        return c, i - a
    for ra, ia, rb, ib, kind, v in cps:
        ca, ja = chain_of(ra, ia)
        cb, jb = chain_of(rb, ib)
        if (ca, ja) != (cb, jb):
            ccps.append((ca, ja, cb, jb, kind, v))
    out, stats = _solve_networks(chains, ccps)
    if diagnostics is not None:
        diagnostics.update(stats)
        diagnostics.update(chains=len(chains), couplings=len(ccps), samples=int(sum(len(c["t"]) for c in chains)),
                           landed=[(i, j) for i, j, r in landed if all(stats["ok_chain"][c] for c, _a, _b in where[r])])
    sol = []
    for r, x in enumerate(R):       # (each run's samples from its chains; a network left as it was: its targets)
        zr = np.array(x["t"], np.float64)
        for c, a, _b in where[r]:
            if out[c] is None:
                continue
            o = np.asarray(out[c], np.float64)
            zr[a:a + len(o)] = o
        sol.append(zr)
    changed = 0
    for k, i in enumerate(idx):
        if k not in loc or i in skip:
            continue
        r, rev, s0 = loc[k]
        s = s0 + (arcs[k][-1] - arcs[k] if rev else arcs[k])
        if all(out[c] is None for c, _a, _b in where[r]):
            continue
        z = zs[k] + (np.interp(s, R[r]["sv"], sol[r]) - np.interp(s, R[r]["sv"], R[r]["t"]))
        z = np.where(hold[k], zs[k], z)                 # (the change, not the resampled profile: the plan's own detail)
        z = np.where(down[k], np.minimum(z, zs[k]), z)
        if float(np.abs(z - zs[k]).max()) >= MIN_MOVE_M:
            plans[i].z = z
            changed += 1
    return changed
