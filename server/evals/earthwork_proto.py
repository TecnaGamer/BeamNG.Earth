"""Prototype of the earthwork solve (#119), against today's grade3d, on a drivability site.

    python server/evals/earthwork_proto.py SITE X Y [R]   (from the repo root; X, Y m from the site's centre; R: the
                                                          report's radius)

Known, hard: joins (one height); a crossing at two layers (upper over lower by the clearance at least); each path's
grading law (its class's grade and vertical curve; a road may follow ground that is steeper than its law).
Unknown: each sample's height off the terrain, the earthwork. Chosen: the least of it, linear in depth, each sample at
its length x width x (speed / 50 km/h)^2: the faster line moves least. Decks and tunnels cost nothing.
A join is a map node the two roads share, at that node's place: two lines that only lie near each other are not joined
(left out, counted, SHOW=n lists n). Where two lines are stacked the heightmap has one height for both, so the samples
of a crossing at two layers are drawn to no terrain (NOSTACK=1: drawn all the same).
A linear program (scipy's HiGHS). With no answer, IIS=1 names an irreducible set of laws and clearances that cannot hold
together. PATHS=<id prefix> NEAR=m STRIDE=n: the profile of the paths with that member; OFF=m: the samples further off
the terrain than m; STEPS=m: the joins across a step in the terrain; ALLJOINS=1: the plan's joins as today."""
import os
import sys
import time


os.environ.setdefault("BNE_SHAPES", "1")
sys.path.insert(0, os.path.join(os.getcwd(), "server"))
sys.path.insert(0, os.path.join(os.getcwd(), "server", "tests"))
from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

import numpy as np  # noqa: E402

import drivability as dv  # noqa: E402
import grading  # noqa: E402
import interchanges  # noqa: E402
import revisit  # noqa: E402
import roadsurf  # noqa: E402

name = sys.argv[1]
with mock.patch.object(revisit, "ON", False), mock.patch.object(roadsurf, "ON", False):
    site = interchanges.load(name, dv.SITES[name][0], keep_m=dv.KEEP_M_SITE.get(name, dv.KEEP_M))
    md = SimpleNamespace(cell=lambda key, **kw: SimpleNamespace(layers={"roads": site.cells.get(str(key))}),
                         dem=SimpleNamespace(sample=site.sample))
    dv._hook_cells(site, md, name)
    store = grading.PlanStore(md, site.sample)
    store.frame_of = lambda region: site.frame
    grading.Grader(site.frame, md, site.sample, store=store)
    la0, lo0, _ = site.frame.enu_to_geodetic(np.array([0.0]), np.array([0.0]), 0.0)
    pl = store.planners_of(la0, lo0)[0][0]
import linenet  # noqa: E402
import scipy.sparse as sp  # noqa: E402
from scipy.optimize import linprog  # noqa: E402
from grade3d import ground, laws, layers, network, problem, region, samples, solve  # noqa: E402

REF_KMH = 50.0
MEET_TOL = 0.15         # m: two lines crossing at one layer with no node meet within this
GROUND_GRADE_WIN = 5    # samples either side the terrain's own grade is read over (a road may follow steep ground)
NODE_M = 6.0            # m: a join's sample lies this near the shared node (a sample step and the plan's 2.5 m)
JOIN_IN_M = 5.0         # m from a member's ends: further in, a sample is on its middle
TIE = 1e-3              # of the median sample's cost: the straightest line among equals (per m of second difference)

x, y = float(sys.argv[2]), float(sys.argv[3])
R = float(sys.argv[4]) if len(sys.argv) > 4 else 60.0
e0, n0, _ = pl.frame.geodetic_to_enu(la0, lo0, 0.0)
c0 = np.array([float(e0[0]), float(n0[0])])
c = np.array([x, y]) + c0
net = network.build(region.members_in(pl, c - 500, c + 500), linenet.Network())
sm = samples.sample(net, layers.order(net))
g = ground.smooth(sm, ground.read(ground.planner_elev(pl), sm))
lw = laws.lay(net, sm, g)
pr = problem.pose(net, sm, lw)
t0 = time.time()
z_old = solve.solve(pr).z
t_old = time.time() - t0
N, P = len(sm), len(net.paths)
off = sm.off
kind = np.array([m.kind for m in net.members], dtype=object)[sm.member]
half = np.array([float(m.half) for m in net.members])[sm.member]
path_of = np.repeat(np.arange(P), np.diff(off))
costed = (kind == "ground") | (kind == "rail")
span = np.zeros(N)
for p in range(P):
    sl = sm.of(p)
    ds = np.diff(sm.s[sl])
    span[sl] = (np.r_[ds, 0.0] / 2 + np.r_[0.0, ds] / 2) if len(ds) else 1.0
# (earthwork is there to meet a layer off the ground: the ground going on from a deck's or a tunnel's end is its
# approach, as far along the line as its law needs to climb one clearance (CLEAR_REF_M at its grade, and its vertical
# curve's two halves); past that, and every road that leads to no structure, is plain ground, PLAIN_X times as dear)
import heapq  # noqa: E402

CLEAR_REF_M = 6.0
GO_ON_COS = 0.7             # two path ends joined go on as one line when they point within ~45 degrees of each other
PLAIN_X = float(os.environ.get("PLAIN_X", "1"))
struct = np.isin(kind, ("tunnel", "deck", "rail_deck"))
ramp_len = CLEAR_REF_M / np.maximum(lw.soft_grade, 1e-3) + lw.soft_K * 100.0 * lw.soft_grade       # (P,)


def out_dir(p, first):
    sl = sm.of(p)
    if sl.stop - sl.start < 2:
        return None
    d_ = (sm.xy[sl.start] - sm.xy[sl.start + 1]) if first else (sm.xy[sl.stop - 1] - sm.xy[sl.stop - 2])
    n_ = float(np.hypot(*d_))
    return d_ / n_ if n_ > 1e-6 else None


adj = {}
for pa, ia, pb, ib in sm.joins:
    na_, nb_ = int(off[pa + 1] - off[pa]), int(off[pb + 1] - off[pb])
    ia_, ib_ = ia % na_, ib % nb_
    if ia_ in (0, na_ - 1) and ib_ in (0, nb_ - 1):
        da, db = out_dir(pa, ia_ == 0), out_dir(pb, ib_ == 0)
        if da is not None and db is not None and float(np.dot(da, db)) <= -GO_ON_COS:
            adj.setdefault(int(off[pa]) + ia_, []).append(int(off[pb]) + ib_)
            adj.setdefault(int(off[pb]) + ib_, []).append(int(off[pa]) + ia_)
dist = np.where(struct, 0.0, np.inf)
heap = [(0.0, int(i)) for i in np.flatnonzero(struct)]
heapq.heapify(heap)
while heap:
    d_, i = heapq.heappop(heap)
    if d_ > dist[i]:
        continue
    p_ = path_of[i]
    for j in ([i - 1] if i > off[p_] else []) + ([i + 1] if i + 1 < off[p_ + 1] else []) + adj.get(i, []):
        nd = d_ + (abs(float(sm.s[j] - sm.s[i])) if path_of[j] == p_ else 0.0)
        if nd < dist[j]:
            dist[j] = nd
            heapq.heappush(heap, (nd, j))
approach = costed & (dist <= ramp_len[path_of])
owns = approach            # (the report's "with a structure to meet")
cost = np.where(costed, span * 2 * half * (lw.kmh[path_of] / REF_KMH) ** 2 * np.where(approach, 1.0, PLAIN_X), 0.0)
# (a heightmap has one height at a place: where two lines are stacked it says at most one of them, and not which. The
# samples of a crossing at two layers, and STACK_M along their paths either side, are drawn to no terrain)
STACK_M = float(os.environ.get("STACK_M", "8"))
stacked = np.zeros(N, bool)
for cp_, (what_, _k), w_ in zip(pr.couplings, pr.why, pr.soft):
    if what_ == "clear" and w_ >= layers.W_CLEAR:
        for p_, i_ in ((cp_[0], cp_[1]), (cp_[2], cp_[3])):
            n_ = int(off[p_ + 1] - off[p_])
            q_ = int(off[p_]) + i_ % n_
            sl_ = sm.of(p_)
            stacked[sl_.start + np.flatnonzero(np.abs(sm.s[sl_] - sm.s[q_]) <= STACK_M)] = True
if not os.environ.get("NOSTACK"):
    cost[stacked] = 0.0
    costed_terrain = costed & ~stacked
else:
    costed_terrain = costed
vol = np.where(costed_terrain, span * 2 * half, 0.0)        # m^2 a sample stands for: its earthwork in m^3 per m off the ground

# variables: z (N), u (N: |z - ground| of the costed), v (N: |second difference|, the tie-break)
rows, cols, vals, ub, grp = [], [], [], [], []
GROUP = [("earth",)]


def le(terms, b):
    k = len(ub)
    grp.append(GROUP[0])
    for j, a in terms:
        rows.append(k)
        cols.append(j)
        vals.append(a)
    ub.append(b)


for i in np.flatnonzero(costed_terrain):
    le([(i, 1.0), (N + i, -1.0)], g[i])
    le([(i, -1.0), (N + i, -1.0)], -g[i])
if os.environ.get("PIN_PLAIN"):
    # (the question "what stops the structures' own roads from taking it all": every road with no structure of its
    # own held within PIN_PLAIN m of its ground, hard)
    for i in np.flatnonzero(costed & ~approach & (np.hypot(*(sm.xy - c).T) <= float(os.environ.get('PIN_R', '1e9')))):
        le([(i, 1.0)], g[i] + float(os.environ["PIN_PLAIN"]))
        le([(i, -1.0)], -g[i] + float(os.environ["PIN_PLAIN"]))
n_law = 0
for p in range(P):
    a, b = int(off[p]), int(off[p + 1])
    n = b - a
    if n < 2:
        continue
    ds = np.maximum(np.diff(sm.s[a:b]), 1e-3)
    gg = g[a:b]
    # (the terrain's own grade round each segment: a road may lie on ground steeper than its law)
    own = np.zeros(n - 1)
    for k in range(n - 1):
        i0, i1 = max(0, k - GROUND_GRADE_WIN), min(n - 1, k + 1 + GROUND_GRADE_WIN)
        own[k] = abs(gg[i1] - gg[i0]) / max(sm.s[a + i1] - sm.s[a + i0], 1e-3)
    free = ~costed_terrain[a:b]
    lim = np.maximum(lw.soft_grade[p], np.where(free[:-1] | free[1:], 0.0, own))
    GROUP[0] = ("grade", p)
    for k in range(n - 1):
        le([(a + k + 1, 1.0), (a + k, -1.0)], lim[k] * ds[k])
        le([(a + k + 1, -1.0), (a + k, 1.0)], lim[k] * ds[k])
        n_law += 2
    rad = max(float(lw.soft_K[p]) * 100.0, 1.0)
    h = float(sm.h[p])
    if h >= problem.MIN_STEP_M:
        for k in range(1, n - 1):
            d2 = [(a + k - 1, 1.0), (a + k, -2.0), (a + k + 1, 1.0)]
            bend = h * h / rad
            GROUP[0] = ("curve", p)
            le(d2, bend)
            le([(j, -w) for j, w in d2], bend)
            GROUP[0] = ("earth",)
            le(d2 + [(2 * N + a + k, -1.0)], 0.0)
            le([(j, -w) for j, w in d2] + [(2 * N + a + k, -1.0)], 0.0)
            n_law += 2
clears, eq, meets, clear_k, suspect, unknown_joins = [], [], [], [], [], []
node_at = {}        # road feature id -> {connector id: its place in the frame}
for f_ in site.cells.values():
    tab_ = f_.rules.get("connectors") if f_ is not None and len(f_) else None
    if tab_ is None:
        continue
    xy_f = f_.xy_in(site.frame)
    for i_ in range(len(f_)):
        fid_ = str(f_.ids[i_])
        if fid_ in node_at:
            continue
        rws = list(tab_.rows(i_))
        if not rws:
            continue
        line_ = np.asarray(f_.parts(i_, xy_f)[0][0], np.float64)[:, :2]
        s_l = np.r_[0.0, np.cumsum(np.hypot(*np.diff(line_, axis=0).T))]
        node_at[fid_] = {str(tab_.value[r_]): np.array([np.interp(float(tab_.start[r_]) * s_l[-1], s_l, line_[:, 0]),
                                                        np.interp(float(tab_.start[r_]) * s_l[-1], s_l, line_[:, 1])])
                         for r_ in rws}


def fid_of(i):
    k_ = net.members[sm.member[i]].key
    return str(k_[2] if str(k_[0]) == "rail" else k_[0])


def rail_fam(i):
    return net.members[sm.member[i]].kind in ("rail", "rail_deck")


interior = np.zeros(N, bool)        # more than JOIN_IN_M along its member from both of the member's ends
for p in range(P):
    sl = sm.of(p)
    s_, mem_ = sm.s[sl], sm.member[sl]
    for i_ in np.unique(mem_):
        on = mem_ == i_
        interior[sl.start + np.flatnonzero(on)] = (s_[on] - s_[on].min() > JOIN_IN_M) & (s_[on].max() - s_[on] > JOIN_IN_M)
GROUP[0] = ("meet",)
doubted = 0
for cp, (what, k), w in zip(pr.couplings, pr.why, pr.soft):
    ia, ib = int(off[cp[0]]) + cp[1] % int(off[cp[0] + 1] - off[cp[0]]), int(off[cp[2]]) + cp[3] % int(off[cp[2] + 1] - off[cp[2]])
    if what == "join":
        # (a join is a shared map node, at that node's place: the two members' roads have a connector in common that
        # lies within NODE_M of both samples. A road with no connectors known (a busway from the OSM import) joins as it
        # lies, but never onto the middle of a tunnel or a deck. ALLJOINS=1: every join the plan gives, as today)
        fa, fb = fid_of(ia), fid_of(ib)
        na, nb = node_at.get(fa), node_at.get(fb)
        if os.environ.get("ALLJOINS") or fa == fb:
            ok_ = True
        elif na and nb:
            ok_ = any(np.hypot(*(na[c_] - sm.xy[ia])) <= NODE_M and np.hypot(*(nb[c_] - sm.xy[ib])) <= NODE_M
                      for c_ in set(na) & set(nb))
        else:
            # (and a road meets a track only at a mapped level crossing: never by lying near it)
            ok_ = rail_fam(ia) == rail_fam(ib) and (rail_fam(ia) or not any(
                net.members[sm.member[i_]].kind in ("tunnel", "deck", "rail_deck") and interior[i_] for i_ in (ia, ib)))
            unknown_joins.append((ia, ib))
        if not ok_:
            suspect.append((ia, ib))
            continue
        eq.append((ia, ib))
    elif what == "clear":
        if w < layers.W_CLEAR:      # (a merge read as a crossing: the map joins the two there)
            doubted += 1
            continue
        clears.append((ia, ib, float(cp[5])))       # (upper, lower, need)
        clear_k.append(k)
    elif what == "meet" and cp[4] == "ge":
        # (two lines crossing at one layer with no shared node: the map states no relation. MEET=1: held together)
        meets.append((ia, ib))
        if os.environ.get("MEET"):
            le([(ia, 1.0), (ib, -1.0)], MEET_TOL)
            le([(ia, -1.0), (ib, 1.0)], MEET_TOL)
n_fixed = len(ub)
for j, (iu, il, need) in enumerate(clears):
    GROUP[0] = ("clear", clear_k[j])
    le([(il, 1.0), (iu, -1.0)], -need)
A_eq = sp.csr_matrix((np.r_[np.ones(len(eq)), -np.ones(len(eq))],
                      (np.r_[np.arange(len(eq)), np.arange(len(eq))], np.r_[[a for a, _b in eq], [b for _a, b in eq]])),
                     shape=(len(eq), 3 * N))
tie = TIE * float(np.median(cost[cost > 0]))
obj = np.r_[np.zeros(N), cost, np.full(N, tie)]
bounds = [(None, None)] * N + [(0, None)] * (2 * N)


def name_of(i):
    m = net.members[sm.member[i]]
    k = m.key[2] if str(m.key[0]) == "rail" else m.key[0]
    return "%s:%s:%s:L%+.0f" % (str(k)[:8], m.kind, m.cls, m.level)


t0 = time.time()
A = sp.csr_matrix((vals, (rows, cols)), shape=(len(ub), 3 * N))
res = linprog(obj, A_ub=A, b_ub=np.array(ub), A_eq=A_eq, b_eq=np.zeros(len(eq)), bounds=bounds, method="highs")
t_new = time.time() - t0
print("samples %d, paths %d, joins %d, clearances %d (doubted, left out: %d), same-layer crossings with no node %d, law rows %d" % (N, P, len(eq), len(clears), doubted, len(meets), n_law))
print("earthwork LP: %s in %.1f s (today's solve %.1f s)" % (res.message.split("\n")[0][:60], t_new, t_old))
if suspect:
    print("joins with no shared map node there, left out: %d of %d (roads with no nodes known, joined as they lie: %d)" % (
        len(suspect), len(suspect) + len(eq), len(unknown_joins)))
    for ia, ib in suspect[:int(os.environ.get("SHOW", "12"))]:
        print("   %s at (%.1f, %.1f) = %s at (%.1f, %.1f)" % (name_of(ia), *(sm.xy[ia] - c0), name_of(ib), *(sm.xy[ib] - c0)))
dropped = []
if os.environ.get("STEPS"):
    # the joined sets (one height) whose samples' ground differs by more than STEPS m: a join across a step in the terrain
    par = list(range(N))

    def find(i):
        while par[i] != i:
            par[i] = par[par[i]]
            i = par[i]
        return i
    for a_, b_ in eq:
        par[find(a_)] = find(b_)
    comp = {}
    for a_, b_ in eq:
        comp.setdefault(find(a_), set()).update((a_, b_))
    rows_ = []
    for members_ in comp.values():
        ms_ = sorted(members_)
        gs = g[ms_]
        if gs.max() - gs.min() > float(os.environ["STEPS"]) and np.hypot(*(sm.xy[ms_[0]] - c)) <= R:
            rows_.append((gs.max() - gs.min(), ms_))
    for d_, ms_ in sorted(rows_, key=lambda r_: -r_[0])[:15]:
        print("joined set with %.2f m between its samples' ground:" % d_)
        for i in ms_:
            print("     ground %.2f  %-44s at (%.1f, %.1f)  %s" % (g[i], name_of(i), *(sm.xy[i] - c0),
                                                                    "approach" if approach[i] else ("plain" if costed[i] else "structure")))
    sys.exit(0)
if os.environ.get("TIED"):
    # the clearances between two samples the joins hold at one height (directly or through other joins)
    par = list(range(N))

    def find(i):
        while par[i] != i:
            par[i] = par[par[i]]
            i = par[i]
        return i
    for a_, b_ in eq:
        par[find(a_)] = find(b_)
    comp = {}
    for a_, b_ in eq:
        comp.setdefault(find(a_), set()).update((a_, b_))
    seen_ = set()
    for iu, il, need in clears:
        if find(iu) == find(il) and (name_of(iu), name_of(il)) not in seen_:
            seen_.add((name_of(iu), name_of(il)))
            print("TIED: %s at (%.1f, %.1f) over %s at (%.1f, %.1f), need %.2f; the joined set:" % (
                name_of(iu), *(sm.xy[iu] - c0), name_of(il), *(sm.xy[il] - c0), need))
            for i in sorted(comp[find(iu)]):
                print("      %s path %d sample %d of %d at (%.1f, %.1f)" % (name_of(i), path_of[i], i - off[path_of[i]],
                                                                        off[path_of[i] + 1] - off[path_of[i]], *(sm.xy[i] - c0)))
    print("tied clearances: %d kinds" % len(seen_))
    sys.exit(0)
if res.status != 0 and os.environ.get("IIS"):
    # an irreducible set of groups (a path's grade law, its curve law, a crossing's clearance; the joins always) that
    # cannot hold together: each group taken out in turn, and left out for good while the rest still has no answer
    ub_a, gid = np.array(ub), {}
    for r_, g_ in enumerate(grp):
        gid.setdefault(g_, []).append(r_)
    alive = np.ones(len(ub), bool)
    zero = np.zeros(3 * N)

    def feasible(mask):
        r_ = linprog(zero, A_ub=A[mask], b_ub=ub_a[mask], A_eq=A_eq, b_eq=np.zeros(len(eq)), bounds=bounds, method="highs")
        return r_.status == 0
    order_ = [g_ for g_ in gid if g_[0] in ("clear", "grade", "curve", "meet")]
    # (halves first: most groups go at once)
    chunk = len(order_)
    todo = list(order_)
    core = []
    while todo:
        chunk = max(1, min(chunk, len(todo)) // 2) if len(todo) > 1 else 1
        part, rest = todo[:chunk], todo[chunk:]
        trial = alive.copy()
        for g_ in part:
            trial[gid[g_]] = False
        if not feasible(trial):
            alive = trial           # (still no answer without them: not needed for the contradiction)
            todo = rest
            chunk = len(todo)
        elif chunk == 1:
            core.append(part[0])    # (needed: without it there is an answer)
            todo = rest
            chunk = len(todo)
    print("IRREDUCIBLE CONTRADICTION: %d groups, with the joins:" % len(core))
    for g_ in core:
        if g_[0] == "clear":
            j = clear_k.index(g_[1])
            iu, il, need = clears[j]
            print("  clearance %.2f at (%.1f, %.1f): %s over %s" % (need, *(sm.xy[iu] - c0), name_of(iu), name_of(il)))
        elif g_[0] in ("grade", "curve"):
            sl = sm.of(g_[1])
            mem = sorted({name_of(i) for i in range(sl.start, sl.stop)})
            print("  %s law of path %d (%.0f m, %.0f km/h, grade %.3f, curve %.0f m), (%.1f, %.1f)->(%.1f, %.1f): %s" % (
                g_[0], g_[1], sm.s[sl][-1], lw.kmh[g_[1]], lw.soft_grade[g_[1]], lw.soft_K[g_[1]] * 100,
                *(sm.xy[sl.start] - c0), *(sm.xy[sl.stop - 1] - c0), " + ".join(mem)[:300]))
        else:
            print(" ", g_)
    sys.exit(2)
if res.status != 0:
    # the least of the clearances that cannot hold with the joins and the laws: each given a slack, the slacks' sum the
    # least (nothing else weighed); those are named, left out, and the rest solved
    S = len(clears)
    A2 = sp.csr_matrix((vals + [-1.0] * S, (rows + list(range(n_fixed, n_fixed + S)), cols + list(range(3 * N, 3 * N + S)))),
                       shape=(len(ub), 3 * N + S))
    A_eq2 = sp.hstack([A_eq, sp.csr_matrix((len(eq), S))]).tocsr()
    res2 = linprog(np.r_[np.zeros(3 * N), np.ones(S)], A_ub=A2, b_ub=np.array(ub), A_eq=A_eq2, b_eq=np.zeros(len(eq)),
                   bounds=bounds + [(0, None)] * S, method="highs")
    if res2.status != 0:
        print("no answer even with every clearance eased:", res2.message.split(chr(10))[0][:60])
        sys.exit(1)
    short = res2.x[3 * N:]
    dropped = [j for j in range(S) if short[j] > 0.01]
    seen = {}
    for j in dropped:
        iu, il, need = clears[j]
        key = (name_of(iu), name_of(il))
        if key not in seen or short[j] > seen[key][0]:
            seen[key] = (short[j], need, sm.xy[iu] - c0)
    print("CONTRADICTIONS: %d clearances cannot hold with the joins and the laws (%d crossings):" % (len(dropped), len(seen)))
    for (up, lo_), (sh, need, at) in sorted(seen.items(), key=lambda kv: -kv[1][0])[:25]:
        print("  %.2f m short of %.2f at (%.1f, %.1f): %s over %s" % (sh, need, *at, up, lo_))
    keep = np.ones(len(ub), bool)
    keep[[n_fixed + j for j in dropped]] = False
    res = linprog(obj, A_ub=A[keep], b_ub=np.array(ub)[keep], A_eq=A_eq, b_eq=np.zeros(len(eq)), bounds=bounds, method="highs")
    print("without them:", res.message.split(chr(10))[0][:60])
    if res.status != 0:
        sys.exit(1)
z = res.x[:N]


name = name_of


def report(tag, zz):
    d = zz - g
    short = [need - (zz[iu] - zz[il]) for j, (iu, il, need) in enumerate(clears) if j not in set(dropped)]
    worst = max(short) if short else 0.0
    near = (np.hypot(*(sm.xy - c).T) <= R) & costed_terrain
    if not near.any():
        near = costed_terrain
    print("%-8s clearances short: %d (worst %.2f m); earthwork %.0f m3 (off the approaches: %.0f m3); "
          "within %.0f m: mean |off ground| %.2f m, max %.2f m, samples over 0.5 m off: %d of %d" % (
              tag, sum(1 for s_ in short if s_ > 0.05), worst, float((vol * np.abs(d)).sum()),
              float((vol * np.abs(d))[costed & ~approach].sum()), R,
              float(np.abs(d[near]).mean()), float(np.abs(d[near]).max()), int((np.abs(d[near]) > 0.5).sum()), int(near.sum())))


report("today", z_old)
report("proto", z)
print("by class within %.0f m (mean / max |off ground| m): today -> proto" % R)
cls = np.array([str(m.cls) for m in net.members], dtype=object)[sm.member]
near = (np.hypot(*(sm.xy - c).T) <= R) & costed_terrain
for cl in sorted(set(cls[near])):
    q = near & (cls == cl)
    print("  %-16s %4d samples  %.2f / %.2f  ->  %.2f / %.2f" % (cl, q.sum(), np.abs(z_old - g)[q].mean(), np.abs(z_old - g)[q].max(),
                                                               np.abs(z - g)[q].mean(), np.abs(z - g)[q].max()))
want = os.environ.get("PATHS")
if want:
    for p in range(P):
        sl = sm.of(p)
        if not any(str(net.members[m].key[0]).startswith(want) or str(net.members[m].key[-1]).startswith(want) for m in net.paths[p].members()):
            continue
        idx = np.arange(sl.start, sl.stop)
        idx = idx[np.hypot(*(sm.xy[idx] - c).T) <= float(os.environ.get("NEAR", "150"))]
        if not len(idx):
            continue
        print("path %d (%.0f km/h, law grade %.3f, curve %.0f m):" % (p, lw.kmh[p], lw.soft_grade[p], lw.soft_K[p] * 100))
        print("      x       y    ground   today   proto   member")
        for i in idx[::int(os.environ.get("STRIDE", "2"))]:
            print("  %7.1f %7.1f  %7.2f %7.2f %7.2f   %s%s" % (*(sm.xy[i] - c0), g[i], z_old[i], z[i], name(i),
                                                          "  (stacked)" if stacked[i] and costed[i] else ""))
if os.environ.get("OFF"):
    print("samples drawn to the terrain and more than %s m off it, within %.0f m:" % (os.environ["OFF"], R))
    q_ = np.flatnonzero(costed_terrain & (np.hypot(*(sm.xy - c).T) <= R) & (np.abs(z - g) > float(os.environ["OFF"])))
    for i in q_:
        print("  (%6.1f, %6.1f) ground %.2f proto %.2f (%+.2f) today %.2f  %s path %d" % (*(sm.xy[i] - c0), g[i], z[i], z[i] - g[i], z_old[i], name(i), path_of[i]))
