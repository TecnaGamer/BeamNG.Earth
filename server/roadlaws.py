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
  leaves it at that road's grade its way (a landing: the junction's surface goes on into it, no kink at the mouth): the
  grade that road is solved to, not the one it was planned with (a planned grade is what the laws change: a ramp's
  10 m step at a joint read as a 300 % grade, a side road held to it had no answer, and neither had its network),
  its last APPROACH_M driven at JUNCTION_KMH (a vertical curve that speed's: a side road climbing its bank from the
  junction turns up over ~10 m, not a 12 m truck's length of 0 to 12 % in 4 m, nor a 50 km/h road's 80 m of fill);
- the grade is never held under what the ground itself has (a real mountain road stays as steep as it is, plus
  NATURAL_PLUS; read from the plans' natural profiles, not from what the passes made of them: a 24 m cliff an
  interchange's ramp left is no hill to keep): it is the sudden changes of grade the laws take out, not the hills;
- each connected network of roads is solved on its own (a conflict at one junction of a city, an infeasible
  programme, left 900 roads of the cell as the passes made them); one with no answer is left as it is, and said (no
  stand-in: the conflict is the thing to fix);
- what the passes solved is held (an interchange's shape and its ramps, a bridge's abutment end, a tunnel), and a road
  under a deck may only go down (its clearance stays); where a shape set nothing but bounds a road (a cut's ceiling
  over it, a fill's floor under it) the road stays within it, and a road over a low arch stays over its cover (the
  soil roads.arch_raise keeps over the vault, which would otherwise stand as a step in the road).

Pure but for the kernel; plans are changed in place (a plan's z replaced)."""
import collections
import logging
import math

import numpy as np

import dmath
import dnp
import gradeshapes as _gsh

log = logging.getLogger("earthd.grading")
_DUMP_N = 0                   # (BNE_LAWS_DUMP's files, this process)

STEP_M = 4.0                # m between a run's samples
NODE_M = 1.0                # m: plan ends this close are one node
REACH_M = 3.0               # m past the road's carriageway an end still joins it (grading.JUNCTION_REACH_M)
CONT_COS = dmath.cos(math.radians(35.0))     # two ends going on within this of straight: one road (grading.CONT_TURN_DEG)
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
EASE_M = (0.05, 0.2, 0.6, 2.0)   # m: the rungs a network the kernel leaves without an answer is eased by, the least that
                                  # gives it one: its landings' bands, its bounds and its grades (ix/relax's easing, lumped;
                                  # a 4-chain network dead for 6 cm of landing, a 260-chain one for a floor over a ceiling,
                                  # A5 underpass and Boston's Bowker, 2026-11-23)

Member = collections.namedtuple("Member", "kind cls kmh")


def laws(spec):
    """(max grade, vertical curve radius m) of a road by its spec (cls, kmh)."""
    m = Member("ground", str(getattr(spec, "cls", None) or "road"), getattr(spec, "kmh", None))
    return _gsh.max_grade(m), _gsh.curve_radius(m)


def _rank(spec):
    c = str(getattr(spec, "cls", None) or "road")
    return RANKS.get(c[:-5] if c.endswith("_link") else c, 1)


def _arc(xy):
    return np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]


def _dir_out(xy, end):
    """Unit direction away from plan end (0: first point, 1: last) along the plan."""
    a, b = (xy[0], xy[min(2, len(xy) - 1)]) if end == 0 else (xy[-1], xy[max(-3, -len(xy))])
    d = b - a
    n = float(dnp.hypot(*d))
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
                        if k < k2 and float(dnp.hypot(*(pts[k] - pts[k2]))) <= NODE_M:
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
                c = -float(dnp.dot(_dir_out(xys[i], e), _dir_out(xys[j], f)))
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
    d = dnp.hypot(*(a + ab * t[:, None] - P).T)
    k = int(np.argmin(d))
    return k, float(t[k]), float(d[k])


def _past_carriageway(xy, P, seg, t, half):
    """m from point P to the carriageway of the road (line xy, half width half) by its nearest point (seg, t) on the line,
    the carriageway ending square at the line's ends: a point past an end is the distance to that end's edge, not the
    radius of a disc round the end's centre (half + REACH_M wide). Pure."""
    a, b = xy[seg], xy[seg + 1]
    v = P - (a + (b - a) * t)
    d = float(dnp.hypot(v[0], v[1]))
    end = (seg == 0 and t <= 0.0) or (seg == len(xy) - 2 and t >= 1.0)
    if not end:
        return max(d - half, 0.0)
    out = (a - b) if seg == 0 and t <= 0.0 else (b - a)           # (the way out of the end)
    out = out / max(float(dnp.hypot(out[0], out[1])), 1e-12)
    along = float(v[0] * out[0] + v[1] * out[1])
    if along <= 0.0:
        return max(d - half, 0.0)
    side = abs(float(v[0] * out[1] - v[1] * out[0]))
    return float(dnp.hypot(along, max(side - half, 0.0)))


def _dump(members, chains, cps, info):
    """BNE_LAWS_DUMP=<dir>: a failed network's problem as an npz (its chains' samples, bounds and laws, its couplings,
    the kernel's info), to be read offline: the conflict named without running the game. Pure but for the file."""
    import json
    import os
    d = os.environ.get("BNE_LAWS_DUMP")
    if not d:
        return
    try:
        os.makedirs(d, exist_ok=True)
        global _DUMP_N
        _DUMP_N += 1
        off = np.r_[0, np.cumsum([len(c["t"]) for c in chains])]
        rows = []
        for cp in cps:
            rows.append([*cp[:4], {"eq": 0, "ge": 1, "le": 2}[cp[4]], cp[5]] +
                        (list(cp[6:8]) + [cp[8]] if len(cp) == 9 else [-1, 0, 0.0]))
        np.savez(os.path.join(d, "net%03d.npz" % _DUMP_N),
                 members=np.asarray(members, np.int64),
                 t=np.concatenate([c["t"] for c in chains]),
                 w=np.concatenate([c["w"] for c in chains]),
                 lo=np.concatenate([c["lo"] for c in chains]),
                 hi=np.concatenate([c["hi"] for c in chains]),
                 par=np.asarray([(c["grade"], c["K"], c.get("lam", 1e-3), c.get("step", 4.0), c.get("length", 0.0))
                                 for c in chains], np.float64),
                 off=off,
                 cps=np.asarray(rows if rows else np.zeros((0, 9)), np.float64),
                 info=json.dumps({k: v for k, v in info.items() if k != "failed"}))
    except Exception as e:          # (never the solve's to fail on)
        log.debug("laws dump: %s: %s", type(e).__name__, e)


def _eased(chains, cps, ease, e):
    """Copies of the chains and their couplings with rung e of ease in them: every landing's band widened by e (ease:
    the couplings' flags, False where a run's own pieces join — a road is one road), every bound lo/hi by e, every grade
    by e a step and every curve's K eased by as much (a steeper stretch, a tighter curve: drivable where the passes'
    estimates contradict). Pure."""
    out = []
    for q, cp in enumerate(cps):
        if e <= 0.0 or not (ease[q] if q < len(ease) else True):
            out.append(cp)
            continue
        a, i, b, j, kind, v = cp[:6]
        tail = tuple(cp[6:]) if len(cp) == 9 else ()
        if kind == "eq":
            out += [(a, i, b, j, "ge", v - e) + tail, (a, i, b, j, "le", v + e) + tail]
            continue
        cp = list(cp)
        cp[5] = v - e if kind == "ge" else (v + e if kind == "le" else v)
        out.append(tuple(cp))
    if e <= 0.0:
        return chains, out
    import copy
    ch = copy.deepcopy(chains)
    for c in ch:
        c["lo"], c["hi"] = c["lo"] - e, c["hi"] + e
        c["grade"], c["K"] = c["grade"] + e / c["step"], c["K"] / (1.0 + e / c["step"])
    return ch, out


def _collapse(chains):
    """The chains with every lo > hi met in the middle, in place: two estimates of one height (a shape's floor over a
    deck's ceiling, a landing pin between bounds), the laws the arbiter — the kernel refuses a lo over a hi outright and
    the whole network with it. (n, max m) of the places. Pure but for the chains' lo/hi."""
    n, mx = 0, 0.0
    for c in chains:
        bad = np.flatnonzero(c["lo"] > c["hi"])
        if not len(bad):
            continue
        mx = max(mx, float((c["lo"] - c["hi"])[bad].max()))
        n += len(bad)
        mid = 0.5 * (c["lo"][bad] + c["hi"][bad])
        c["lo"], c["hi"] = np.array(c["lo"]), np.array(c["hi"])
        c["lo"][bad] = c["hi"][bad] = mid
    return n, mx


def _solve_networks(chains, ccps, ease=None):
    """The chains' profiles ([array or None: left as it was]), each connected network (chains joined by couplings)
    solved on its own. A network the kernel leaves without an answer (its laws contradicting, or two of its bounds) is
    solved again eased by the least rung of EASE_M that gives it one (said); one with no answer even eased is left as it
    is, and said. Returns (profiles, stats: stats["failed"] [(chains, kernel info)] of those with none)."""
    import carriageways as _cw
    n = len(chains)
    parent = list(range(n))

    def root(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for cp in ccps:
        parent[root(cp[0])] = root(cp[2])
        if len(cp) == 9:
            parent[root(cp[0])] = root(cp[6])
    groups = collections.defaultdict(list)
    for c in range(n):
        groups[root(c)].append(c)
    out = [None] * n
    stats = dict(networks=len(groups), infeasible=0, eased=0, failed=[], ok_chain=[False] * n)
    for members in groups.values():
        pos = {c: q for q, c in enumerate(members)}
        ces = [ease[q] if ease is not None else True for q, cp in enumerate(ccps) if cp[0] in pos]
        cps = [(pos[cp[0]], cp[1], pos[cp[2]]) + tuple(cp[3:6]) + ((pos[cp[6]],) + tuple(cp[7:]) if len(cp) == 9 else ())
               for cp in ccps if cp[0] in pos]
        chs = [chains[c] for c in members]
        nbad, bad_m = _collapse(chs)
        got, info, e_used = None, {}, 0.0
        for e in (0.0,) + EASE_M:
            ch2, cp2 = _eased(chs, cps, ces, e)
            info = {}
            try:
                got = _cw.solve(ch2, cp2, diagnostics=info)
            except Exception as ex:          # (this network as the passes left it)
                got, info = None, {"error": "%s: %s" % (type(ex).__name__, ex)}
            if got is not None and info.get("feasible", True) and info.get("converged", True):
                e_used = e
                break
            if e == 0.0 and info.get("status") not in ("PrimalInfeasible", "AlmostPrimalInfeasible", "InconsistentBounds"):
                break                   # (a budget spent or a kernel error: easing is not its answer)
        if got is not None and info.get("feasible", True) and info.get("converged", True):
            for c, z in zip(members, got):
                out[c] = z
                stats["ok_chain"][c] = True
            if e_used > 0.0 or nbad:
                stats["eased"] += 1
                log.info("road laws: a network of %d chains answered with %.2f m of ease (its landings' bands, its "
                         "bounds, its grades; the least rung)%s", len(members), e_used,
                         (", %d bounds met in the middle by up to %.2f m" % (nbad, bad_m)) if nbad else "")
        else:
            stats["infeasible"] += 1
            stats["failed"].append((members, info))
            _dump(members, chs, cps, info)
    return out, stats


def apply(plans, specs, held=None, under=None, skip=(), diagnostics=None, natural=None, meets=None, bounds=None):
    """plans: [Plan] (xy (n, 2+), z (n,), road_half, abut, rid), specs alike (cls, kmh; None: left out); held(i) ->
    bool (n,) of plan i's held samples (or None), under(i) -> bool (n,) of those under a deck (only down there); skip:
    plan indices left as they are, held whole (tunnels); natural(i) -> (n,) plan i's natural profile (the heights
    before the passes; None: its heights now). meets(i, j) -> True / False / None (unknown): plans i and j share a node
    of the map (a T junction is one only where they do: an end a few metres from a road it does not join, a service
    road's cut ending 6 m beside a street, is no junction). bounds(i) -> (lo (n,), hi (n,)) or None: plan i's heights
    stay within them (-inf / inf: none). Changes the plans' z in place; returns how many changed."""
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
    hold, down, lob, hib = [], [], [], []
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
        got = bounds(i) if bounds is not None else None
        lob.append(np.asarray(got[0], np.float64) if got is not None else np.full(len(zs[k]), -np.inf))
        hib.append(np.asarray(got[1], np.float64) if got is not None else np.full(len(zs[k]), np.inf))
    rs, nodes = runs(xys, sps, rids)
    R = []                          # per run: its samples, targets, weights, bounds and laws
    loc = {}                        # plan k -> (run, reversed, s0)
    for r, run in enumerate(rs):
        z_r, h_r, d_r, s_r, n_r, lo_r, hi_r = [], [], [], [], [], [], []
        s0 = 0.0
        for k, rev in run:
            z, h, d, s, nz, lb, hb = zs[k], hold[k], down[k], arcs[k], nats[k], lob[k], hib[k]
            if rev:
                z, h, d, s, nz, lb, hb = z[::-1], h[::-1], d[::-1], s[-1] - s[::-1], nz[::-1], lb[::-1], hb[::-1]
            s = s + s0
            if z_r:                                     # (the joint: one point, the first part's)
                z, h, d, s, nz, lb, hb = z[1:], h[1:], d[1:], s[1:], nz[1:], lb[1:], hb[1:]
            loc[k] = (r, rev, s0)
            z_r.append(z)
            h_r.append(h)
            d_r.append(d)
            s_r.append(s)
            n_r.append(nz)
            lo_r.append(lb)
            hi_r.append(hb)
            s0 = float(s[-1]) if len(s) else s0
        S, Z, H, D, NZ, LB, HB = (np.concatenate(a) for a in (s_r, z_r, h_r, d_r, n_r, lo_r, hi_r))
        L = float(S[-1] - S[0])
        m = max(2, int(math.ceil(L / STEP_M)) + 1)
        sv = np.linspace(S[0], S[-1], m)
        step = L / max(m - 1, 1)
        t = dnp.interp(sv, S, Z)
        j = np.clip(np.searchsorted(S, sv), 0, len(S) - 1)      # (a sample is held / only down where a plan point
        hs = np.zeros(m, bool)                                   # within half a step of it is; bounded by theirs)
        ds = np.zeros(m, bool)
        lo_s, hi_s = np.full(m, -np.inf), np.full(m, np.inf)
        for jj in (j, np.clip(j - 1, 0, len(S) - 1)):
            ok = np.abs(S[jj] - sv) <= step / 2 + 1e-6
            hs |= ok & H[jj]
            ds |= ok & D[jj]
            # (a bound is the plan point's: at the sample it stands the road's own rise between them from its height there,
            # the same offset the sample's own height (t: where it may only go down) is from the point's. Taken as it was,
            # a shape's floor at a point 31.688 over a cap 31.683 at the sample beside it (a road under a deck, falling
            # between them), and 33.028 over 33.027 on another: no network could hold both)
            lo_s = np.where(ok, np.maximum(lo_s, LB[jj] + (t - Z[jj])), lo_s)
            hi_s = np.where(ok, np.minimum(hi_s, HB[jj] + (t - Z[jj])), hi_s)
        g_law = rad = 0.0
        rank = 0
        for k, _rev in run:
            g, rr = laws(sps[k])
            g_law, rad, rank = max(g_law, g), max(rad, rr), max(rank, _rank(sps[k]))
        w_n = max(1, int(round(NATURAL_WIN_M / max(step, 1e-6))))
        tn = dnp.interp(sv, S, NZ)                       # (the ground's own grade: the natural profile's)
        nat = np.abs(tn[w_n:] - tn[:-w_n]) / (w_n * step) if m > w_n else np.abs(np.diff(tn)) / max(step, 1e-6)
        g = max(g_law, float(nat.max()) + NATURAL_PLUS if len(nat) else g_law)
        R.append(dict(sv=sv, t=t, w=np.where(hs, W_HOLD, 1.0 + W_RANK * rank), lo=lo_s,
                      hi=np.minimum(np.where(ds, t, np.inf), hi_s), held=hs, S=S, Z=Z, grade=g, rad=rad, step=step,
                      junction=[False, False], parts=[(k, rev) for k, rev in run]))

    def arc_of(k, s_plan):
        """(run, arc along the run) of plan k's point at its own arc length s_plan."""
        r, rev, s0 = loc[k]
        return r, s0 + (arcs[k][-1] - s_plan if rev else s_plan)

    def sample_of(k, s_plan):
        """(run, sample) of plan k's point at its own arc length s_plan."""
        r, s = arc_of(k, s_plan)
        return r, int(np.argmin(np.abs(R[r]["sv"] - s)))

    def between(r, s):
        """(sample a, sample b, alpha): run r's height at its arc s is (1 - alpha) z_a + alpha z_b (two neighbouring
        samples; alpha past 0..1 at the run's ends: carried on along its end segment)."""
        sv = R[r]["sv"]
        a = int(np.clip(np.searchsorted(sv, s) - 1, 0, len(sv) - 2))
        return a, a + 1, float((s - sv[a]) / max(float(sv[a + 1] - sv[a]), 1e-9))

    def run_dir(r, i):
        """Unit direction of run r at its sample i (the way its samples go)."""
        s = R[r]["sv"][i]
        for k, _rev in R[r]["parts"]:
            _r, rev, s0 = loc[k]
            if s0 - 1e-6 <= s <= s0 + arcs[k][-1] + 1e-6:
                sp = arcs[k][-1] - (s - s0) if rev else s - s0
                seg = int(np.clip(np.searchsorted(arcs[k], sp) - 1, 0, len(arcs[k]) - 2))
                d = xys[k][seg + 1] - xys[k][seg]
                d = d / max(float(dnp.hypot(*d)), 1e-9)
                return -d if rev else d
        return None

    cps = []                        # in the runs' terms: (run a, sample, run b, sample, kind, value[, run c, sample,
                                    # alpha]): z_a - z_b (= / >= / <=) v, or z_a - (1 - alpha) z_b - alpha z_c

    def end_sample(k, e):
        return sample_of(k, 0.0 if e == 0 else float(arcs[k][-1]))

    def end_arc(k, e):
        return arc_of(k, 0.0 if e == 0 else float(arcs[k][-1]))

    def part_at(r, s, way):
        """(plan, its own arc) of run r's point at its arc s: the part holding it, at a joint the one going on from it
        way (+1 up the run's arcs, -1 down them); past the run's ends its end part's end."""
        got = []
        for k2, _rev in R[r]["parts"]:
            _r, rev2, s0 = loc[k2]
            if s0 - 1e-6 <= s <= s0 + float(arcs[k2][-1]) + 1e-6:
                got.append(k2)
        if not got:
            got = [R[r]["parts"][0 if s < R[r]["sv"][0] else -1][0]]
        k2 = got[-1] if way > 0 else got[0]
        _r, rev2, s0 = loc[k2]
        sp = float(np.clip(arcs[k2][-1] - (s - s0) if rev2 else s - s0, 0.0, arcs[k2][-1]))
        return k2, sp

    def z_on(k2, sp):
        """Plan k2's own height at its arc sp, past its ends carried on along its end segment."""
        ar, z = arcs[k2], zs[k2]
        if len(ar) < 2 or 0.0 <= sp <= ar[-1]:
            return float(dnp.interp(sp, ar, z))
        q = 0 if sp < 0.0 else len(ar) - 2
        return float(z[q] + (z[q + 1] - z[q]) * (sp - ar[q]) / max(float(ar[q + 1] - ar[q]), 1e-9))

    def final_on(k2, sp):
        """Plan k2's height at its arc sp as the laws leave it."""
        return float(dnp.interp(sp, arcs[k2], np.asarray(plans[idx[k2]].z, np.float64)))

    landed = []                     # (plan, the plan it lands on, its run, whether it stands as landed): grading

    def land(k, e, main, onto):
        """Plan k's end e meets main (run, arc along it: the mouth): one height there, and it leaves at main's grade its
        way: its next sample st on stands at main's height st x cos(the angle between them) along main from the mouth,
        within LANDING_TOL x st. Main's height there as its plan ends up: its two samples round the point and its own
        detail between them (a plan keeps its change, not the resampled profile), not its nearest sample's (4 m apart, a
        16 % ramp's nearest sample stood 0.3 m over the node a side road landed at); a held main's own heights (it
        keeps them whatever its samples solve to: a side road landed on its solve stood apart from it), on the one part
        of main the mouth is on (at a joint, the part the way out goes along) and that part's own grade: the passes may
        leave two parts apart at their joint (each keeps its own heights), and the run's profile there is the first
        part's point and the next part's beyond it, a step between two plans no plan has: the High Five's secondary
        230c31f9, at the node where 8ff47da6 at the ground (176.6) goes on into fe6285a3 held on an abutment's fill
        6.5 m up, was pinned to the fill's height at its mouth and to the ground's 4 m on (160 %, its law 10 %), and
        its network was left as it was (Popoli's roundabout the same at 0.93 m)."""
        r, i = end_sample(k, e)
        ra, sa = main
        a0, a1, _f = between(ra, sa)
        if r == ra and i in (a0, a1):
            return
        md = run_dir(ra, a0)
        c = float(dnp.dot(_dir_out(xys[k], e), md)) if md is not None else 0.0
        k0, sp0 = part_at(ra, sa, 1 if c >= 0.0 else -1)        # (the part the way out goes along)
        ar = arcs[k0]
        q0 = int(np.clip(np.searchsorted(ar, sp0, "right") - 1, 0, len(ar) - 1))
        q1 = int(np.clip(np.searchsorted(ar, sp0, "left"), 0, len(ar) - 1))
        held = bool(hold[k0][q0] and hold[k0][q1] and not R[r]["held"][i])     # (both held: each its own)

        def pin(j, lo, hi):
            R[r]["lo"][j] = max(float(R[r]["lo"][j]), lo)
            R[r]["hi"][j] = min(float(R[r]["hi"][j]), hi)

        def couple(j, s, k2, band):
            """Sample j at main's height at its arc s within band, as main's part k2 ends up there: its two samples
            round s and k2's own detail between them (its own heights less its own at the samples, plus the solve)."""
            b0, b1, g = between(ra, s)
            v = 0.0
            if R[ra]["S"][0] <= s <= R[ra]["S"][-1]:
                _r, rev2, s0 = loc[k2]
                sk = s0 + (arcs[k2][-1] - arcs[k2] if rev2 else arcs[k2])
                o = np.argsort(sk, kind="stable")
                ow = dnp.interp(R[ra]["sv"][[b0, b1]], sk[o], zs[k2][o])
                sp = float(np.clip(arcs[k2][-1] - (s - s0) if rev2 else s - s0, 0.0, arcs[k2][-1]))
                v = z_on(k2, sp) - ((1.0 - g) * float(ow[0]) + g * float(ow[1]))
            if band == 0.0 and abs(v) < 1e-9:   # (an "eq" coupling's value is 0: a band of none for the rest)
                cps.append((r, j, ra, b0, "eq", 0.0, ra, b1, g))
            else:
                cps.extend([(r, j, ra, b0, "ge", v - band, ra, b1, g), (r, j, ra, b0, "le", v + band, ra, b1, g)])
        if held:
            zm = z_on(k0, sp0)
            pin(i, zm, zm)
        else:
            couple(i, sa, k0, 0.0)
        m = len(R[r]["sv"])
        if i not in (0, m - 1) or m < 3 or r == ra:
            return
        R[r]["junction"][0 if i == 0 else 1] = True
        j = 1 if i == 0 else m - 2
        st = abs(float(R[r]["sv"][j] - R[r]["sv"][i]))
        along = st * c                  # (m along main's samples the way out of it reaches)
        way = -1 if c >= 0.0 else 1

        def stands():
            """The landing as the plans ended up, not as the laws were asked for it: k's height at the mouth is main's
            (the part k0 it lands on) and at its next sample st on stands within LANDING_TOL x st of main's there."""
            zk = [final_on(*part_at(r, float(R[r]["sv"][q]), 1)) for q in (i, j)]
            if abs(zk[0] - final_on(k0, sp0)) > MIN_MOVE_M:
                return False
            return md is None or abs(zk[1] - final_on(*part_at(ra, sa + along, way))) <= LANDING_TOL * st + MIN_MOVE_M
        landed.append((idx[k], idx[onto], r, stands))
        if md is None:
            return
        if held:
            zm = z_on(k0, sp0 - along if loc[k0][1] else sp0 + along)
            pin(j, zm - LANDING_TOL * st, zm + LANDING_TOL * st)
        else:
            couple(j, sa + along, part_at(ra, sa + along, way)[0], LANDING_TOL * st)
    paired = set()
    for run in rs:
        for (k, rev), (k2, rev2) in zip(run, run[1:]):
            paired.add((k, 0 if rev else 1))
            paired.add((k2, 1 if rev2 else 0))
    for node in nodes:              # (its ends not going on into another: onto the road through it, or the top one's)
        through = [x for x in node if x in paired]
        free = [x for x in node if x not in paired]
        if through:
            main, onto = end_arc(*through[0]), through[0][0]
        else:
            free.sort(key=lambda x: -_rank(sps[x[0]]))
            main, onto = end_arc(*free[0]), free[0][0]
            free = free[1:]
        for k, e in free:
            land(k, e, main, onto)
    in_node = {x for node in nodes for x in node}
    # (the plans' boxes grown by their reach, as arrays: each end tested against all of them at once; one box test a
    # pair in Python was 40 % of a city cell's time)
    reach_all = np.array([float(getattr(plans[idx[j]], "road_half", 0.0) or 0.0) + REACH_M for j in range(len(xys))])
    BX = np.array([np.r_[xy.min(0), xy.max(0)] for xy in xys]).reshape(-1, 4)
    BLO = BX[:, :2] - reach_all[:, None]
    BHI = BX[:, 2:] + reach_all[:, None]
    run_of = np.array([loc[j][0] if j in loc else -1 for j in range(len(xys))])
    for k in range(len(xys)):       # (an end on another road's line, not at its end: a T junction)
        for e in (0, 1):
            if (k, e) in paired or (k, e) in in_node:
                continue
            P = xys[k][0 if e == 0 else -1]
            best = None
            cand = np.flatnonzero((BLO[:, 0] <= P[0]) & (BLO[:, 1] <= P[1]) & (BHI[:, 0] >= P[0]) &
                                  (BHI[:, 1] >= P[1]) & (run_of != run_of[k]))
            for j in cand.tolist():
                if j == k:
                    continue
                reach = reach_all[j]
                seg, tt, d = _nearest(xys[j], P)
                if d <= reach and (best is None or d < best[0]) and \
                        _past_carriageway(xys[j], P, seg, tt, reach - REACH_M) <= REACH_M and \
                        (meets is None or meets(idx[k], idx[j]) is not False):
                    best = (d, j, float(arcs[j][seg] + tt * (arcs[j][seg + 1] - arcs[j][seg])))
            if best is not None:
                land(k, e, arc_of(best[1], best[2]), best[1])
    # (each run as chains: its ends at junctions driven at JUNCTION_KMH, the rest at its speed; the pieces overlap by
    # two samples held equal, so the grade goes on through the change of law)
    rad_j = laws(Member("ground", "road", JUNCTION_KMH))[1]
    chains, where = [], []          # where[run]: [(chain, its first run sample)]
    ccps = []
    cease = []                   # parallel: True where a coupling is a landing (roadlaws' own), False a run's pieces
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
            chains.append(dict(t=x["t"][sl], w=x["w"][sl], lo=x["lo"][sl], hi=x["hi"][sl],
                               grade=x["grade"], K=rr / 100.0, lam=1e-3, step=x["step"], length=0.0))
            where[r].append((len(chains) - 1, a, b))
        for (c1, a1_, b1_), (c2, a2_, _b2) in zip(where[r], where[r][1:]):
            for q in range(a2_, b1_ + 1):               # (the overlap: both pieces' samples equal)
                ccps.append((c1, q - a1_, c2, q - a2_, "eq", 0.0))
                cease.append(False)

    def chain_of(r, i):
        for c, a, b in where[r]:
            if a <= i <= b:
                return c, i - a
        c, a, _b = where[r][-1]
        return c, i - a
    for cp in cps:
        ca, ja = chain_of(cp[0], cp[1])
        cb, jb = chain_of(cp[2], cp[3])
        if len(cp) == 9:
            ccps.append((ca, ja, cb, jb, cp[4], cp[5]) + chain_of(cp[6], cp[7]) + (cp[8],))
            cease.append(True)
        elif (ca, ja) != (cb, jb):
            ccps.append((ca, ja, cb, jb, cp[4], cp[5]))
            cease.append(True)
    out, stats = _solve_networks(chains, ccps, cease)
    if diagnostics is not None:
        diagnostics.update(stats)
        diagnostics.update(chains=len(chains), couplings=len(ccps), samples=int(sum(len(c["t"]) for c in chains)))
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
        o = np.argsort(s, kind="stable")
        own = dnp.interp(R[r]["sv"], s[o], zs[k][o])     # (the run's samples on this plan's own heights: at a joint whose
                                                        # parts the passes left apart, each part's own, so both end on
                                                        # the solved height; the run's targets there are the first's)
        z = zs[k] + (dnp.interp(s, R[r]["sv"], sol[r]) - dnp.interp(s, R[r]["sv"], own))
        z = np.minimum(np.maximum(z, lob[k]), hib[k])
        z = np.where(hold[k], zs[k], z)                 # (the change, not the resampled profile: the plan's own detail)
        z = np.where(down[k], np.minimum(z, zs[k]), z)
        if float(np.abs(z - zs[k]).max()) >= MIN_MOVE_M:
            plans[i].z = z
            changed += 1
    if diagnostics is not None:
        diagnostics["landed"] = [(i, j) for i, j, r, stands in landed
                                 if all(stats["ok_chain"][c] for c, _a, _b in where[r]) and stands()]
    return changed
