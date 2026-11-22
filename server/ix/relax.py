"""Heights: what contradicts is relaxed, by the least it takes, never the whole interchange given up.

The kernel (carriageways.solve) holds every law as a hard constraint: the grade between neighbouring samples, the vertical
curves, the clearances, the joins, and a floor and ceiling per sample. When they cannot all hold it says so and has no
heights. The laws that are differences of two heights (all but the curves) are a graph (diffgraph), whose negative cycles are
the contradictions; each is named and eased here, one at a time, in the order the laws are given up:

  1. the grade law: a road is steeper than its class for a stretch (a game's, not a design code's) and every vehicle takes
     it, up to the steepest grade a vehicle can drive at all (gradelaw.DRIVABLE_GRADE, the mod's own limit: no car drives a
     link steeper). A steeper road or a tighter curve is drivable; a road through a deck never is, so nothing that keeps a
     vehicle off a deck gives way until the grade and the curves (below) have given all they can;
  2. a decision from the layers alone (two members of one kind, one over the other by the map's layer tags: the order step's
     weakest evidence, ix/s3_order.py, whose loops it can see only with the ground free): given up whole, the least layer
     difference first, as the order step does;
  3. a deck's mapped bound: its floor over the ground it spans (a girder's depth and the air under it) and a road going
     under's ceiling (it never rises over its ground). They come from the map's kinds and the elevation: an estimate, and
     where the road really goes under the ground (a bridge that becomes a tunnel along one road) the one that is wrong;
  4. a clearance: a vehicle must pass under;
  5. a join: two ends of one road are one height (never given up: they are the roads' topology).

The vertical curves are no difference of two heights: the graph does not see them, and a loop it finds is no curves' (they only
add to what it holds). They are eased by the kernel's own answer (ease_curves), whatever the graph's loops asked of 2 to 4,
down to the tightest curve the laws give any road (gradeshapes.R_MIN_ROAD_M: a path's and a service road's) and no tighter.

The cycle is eased by the least that closes it (its shortfall and a millimetre of room, so that the feasible set is more than
a point), within the weakest class it holds: the bounds and the clearances equally over the cycle's constraints, the grade
law of every road of the cycle by one factor (a road's law is one number: the kernel's `grade`). The labels the search has
reached stay for the cycles after it. What was eased, and by how much, is returned for the artifact and the log: nothing is
eased silently.

The vertical curve law (ease_curves) is eased where the curves are what is left to contradict: the radius of the roads whose
other laws were eased (or, where none was, of all) divided by the least factor that gives the region round them heights, found
on that region alone (a few roads coupled to them, solved in a moment) and checked on the whole, no road's radius under the
floor. If they are at it and the whole still has none, the loop is not the curves': it says so and nothing is built from it."""
import math
from collections import namedtuple

import numpy as np

import gradelaw
import gradeshapes as _gsh

from . import diffgraph

LAYER, BOUND, GRADE, CLEARANCE, JOIN = 0, 1, 2, 3, 4    # (the kinds of constraint; JOIN is never given up)
GIVEN_UP = (GRADE, LAYER, BOUND, CLEARANCE)             # (in this order: a vehicle can drive what the first gives)
DRIVABLE = gradelaw.DRIVABLE_GRADE                      # the steepest grade a vehicle drives at all
SLACK_M = 0.001         # m of room past what closes a cycle (the feasible set is not one point: a solver's interior)
MIN_SEGMENT_M = 0.5     # carriageways.MIN_SEGMENT_M: the least run a segment's own spacing gives

# one relaxation: kind ("floor", "ceiling", "clearance", "grade", "curve"), amount (m; for "grade" and "curve" the factor
# the law was eased by), members ((member, s), ...) the constraint's places (global member numbers of the net), text
Relaxed = namedtuple("Relaxed", "kind amount at text")


class Problem:
    """The kernel's problem (chains, couplings) as a graph, each edge knowing which constraint it is."""

    def __init__(self, net, pcs, smp, chains, cps, trace):
        self.net, self.pcs, self.smp, self.chains, self.cps = net, pcs, smp, chains, cps
        self.off = [0]
        for ch in chains:
            self.off.append(self.off[-1] + len(ch["t"]))
        Z = self.zero = self.off[-1]
        g = self.g = diffgraph.Graph(Z + 1)
        self.klass, self.ref = [], []
        self.weak = {}                              # edge of a layer decision -> |layer difference|
        self.decision, self.of_decision = {}, {}    # a layer decision -> its edges; an edge -> its decision
        self.grade_edges = {}                       # chain -> [(edge, its base weight)]
        self.grade0 = [float(ch["grade"]) for ch in chains]
        self.lam = [1.0] * len(chains)
        bound = trace["bound"] or {}
        self.bound, self.cross = bound, trace.get("crossing") or []
        for c, ch in enumerate(chains):
            m = len(ch["t"])
            ds = None if ch.get("ds") is None else np.asarray(ch["ds"], np.float64)
            for k in range(m - 1):
                run = max(float(ds[k]), MIN_SEGMENT_M) if ds is not None else float(ch["step"])
                w = self.grade0[c] * run
                for a, b in ((k, k + 1), (k + 1, k)):
                    e = g.add(self.off[c] + a, self.off[c] + b, w)
                    self._tag(GRADE, ("grade", c, k))
                    self.grade_edges.setdefault(c, []).append((e, w))
            lo, hi = np.asarray(ch["lo"], np.float64), np.asarray(ch["hi"], np.float64)
            for k in np.flatnonzero(np.isfinite(lo)).tolist():
                src = bound.get((c, k, "lo"))
                g.add(self.off[c] + k, Z, -float(lo[k]))
                self._tag(CLEARANCE if src and src[0] >= lo[k] - 1e-9 else BOUND, ("floor", c, k))
            for k in np.flatnonzero(np.isfinite(hi)).tolist():
                src = bound.get((c, k, "hi"))
                g.add(Z, self.off[c] + k, float(hi[k]))
                self._tag(CLEARANCE if src and src[0] <= hi[k] + 1e-9 else BOUND, ("ceiling", c, k))
        for n_, cp in enumerate(cps):
            if len(cp) != 6:
                raise ValueError("a coupling of the shapes is (chain, sample, chain, sample, kind, value)")
            a, i, b, j, kind, val = cp
            ia_, ib = self.node(a, i), self.node(b, j)
            if kind == "eq":
                for u, v in ((ib, ia_), (ia_, ib)):
                    g.add(u, v, 0.0)
                    self._tag(JOIN, ("cp", n_))
            elif kind == "ge":
                g.add(ia_, ib, -float(val))
                weak = self._layers(a, i, b, j)
                self._tag(LAYER if weak is not None else CLEARANCE, ("cp", n_))
                if weak is not None:
                    e = len(g.eu) - 1
                    self.weak[e] = weak
                    # (a decision is the order of two members: every coupling of it, at its crossings and along them)
                    d = tuple(int(self.smp[c]["mem"][k]) for c, k in ((a, i), (b, j)))
                    self.decision.setdefault(d, []).append(e)
                    self.of_decision[e] = d
            else:
                g.add(ib, ia_, float(val))
                self._tag(CLEARANCE, ("cp", n_))
        self.eased = {}                             # edge -> m it was eased by
        self.dropped = {}                           # edge -> the decision given up (a layer decision: ge)

    def _layers(self, a, i, b, j):
        """|the layers' difference| where the clearance of chain a's sample i over chain b's sample j was decided by the layers
        alone (the two members of one kind), else None."""
        ma, mb = (self.net.m[int(self.smp[c]["mem"][k])] for c, k in ((a, i), (b, j)))
        if _gsh.LEVEL_BAND[ma.kind] != _gsh.LEVEL_BAND[mb.kind]:
            return None
        return abs(float(ma.level) - float(mb.level))

    def _tag(self, klass, ref):
        self.klass.append(klass)
        self.ref.append(ref)

    def node(self, c, k):
        n = len(self.chains[c]["t"])
        return self.off[c] + (k if k >= 0 else n + k)

    def of(self, v):
        """(chain, sample) of node v (None: the zero node)."""
        if v == self.zero:
            return None
        lo, hi = 0, len(self.off) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.off[mid] <= v:
                lo = mid
            else:
                hi = mid
        return lo, v - self.off[lo]

    def place(self, v):
        """(member, s) of node v on its piece."""
        got = self.of(v)
        if got is None:
            return None
        c, k = got
        x = self.smp[c]
        return int(x["mem"][k]), float(x["ms"][k])

    def _name(self, member):
        return str(self.net.m[member].key[0])[:8] + ":" + self.net.m[member].kind

    # ---- the search ----

    def relax(self):
        """Ease every contradiction of the graph; the Relaxed list (in the order they were found). Deterministic: the edges
        are in the order the problem gives them."""
        self.g.settle(self._yield)
        return self.report()

    def _yield(self, cyc):
        g, klass = self.g, self.klass
        short = -math.fsum(g.ew[k] for k in cyc) + SLACK_M
        present = {klass[k] for k in cyc if klass[k] != JOIN}
        if not present:
            raise ValueError("a loop of joins alone is short %.3f m: nothing in it can give way" % short)
        changed = []
        for kind in GIVEN_UP:
            if kind not in present:
                continue
            if kind == GRADE:
                short -= self._ease_grade(cyc, short, changed)
                if short <= 1e-9:
                    return changed
            elif kind == LAYER:
                edges = [k for k in cyc if klass[k] == LAYER]
                k = min(edges, key=lambda k: (self.weak[k], k))
                all_ = self.decision[self.of_decision[k]]
                self.dropped[k] = -g.ew[k]
                for e in all_:
                    g.ew[e] = math.inf
                    if e != k:
                        self.dropped.setdefault(e, None)
                return changed + list(all_)
            else:
                return changed + self._ease(sorted({k for k in cyc if klass[k] == kind}), short)
        raise ValueError("a loop short %.3f m has nothing left to give way, the grade law at what a vehicle drives" % short)

    def _room(self, k):
        """m the constraint k can be eased by: a clearance, no more than the headroom it asks over the structure's own depth
        (further, the road under stands in the girders and the slab of the road over it: a road through a deck is never
        drivable); a bound that is no clearance's, any."""
        kind, *rest = self.ref[k]
        if kind == "cp":
            a, i = self.cps[rest[0]][:2]
            return max(-self.g.ew[k] - _gsh.structure_depth(self.net, int(self.smp[a]["mem"][i])), 0.0)
        if self.klass[k] != CLEARANCE:
            return math.inf
        c, s = rest
        src = self.bound[(c, s, "lo" if kind == "floor" else "hi")]
        upper, need = self.cross[src[1]]
        if kind == "floor":
            upper = int(self.smp[c]["mem"][s])      # (the piece's own road is the upper one over a road the shape does not move)
        return max(need - _gsh.structure_depth(self.net, upper) - self.eased.get(k, 0.0), 0.0)

    def _ease(self, edges, short):
        """Edges eased by short in all, equally, an edge that holds less than its share (a clearance is eased to the depth of
        the structure over it, no further: _room) eased by what it holds and the rest shared by the others. The edges
        eased."""
        g = self.g
        room = {k: self._room(k) for k in edges}
        left = sorted(edges, key=lambda k: (room[k], k))
        got = {}
        while left and short > 1e-12:
            share = short / len(left)
            k = left[0]
            take = min(share, max(room[k], 0.0))
            if room[k] <= share:
                got[k] = got.get(k, 0.0) + take
                short -= take
                left.pop(0)
                continue
            for k in left:
                got[k] = got.get(k, 0.0) + share
            short = 0.0
        if short > SLACK_M + 1e-9:      # (the room's millimetre is not needed: only what closes the loop is)
            raise ValueError("a loop is short %.3f m with nothing left to ease" % short)
        for k, v in got.items():
            g.ew[k] += v
            self.eased[k] = self.eased.get(k, 0.0) + v
        return sorted(got)

    def _ease_grade(self, cyc, short, changed):
        """The grade law of the cycle's roads eased by one factor, the least that makes up the shortfall, and no road past the
        steepest grade a vehicle drives (DRIVABLE). The shortfall it made up (less than short where the cap held); changed
        gets the edges."""
        g = self.g
        own = [k for k in cyc if self.klass[k] == GRADE]
        allowed = math.fsum(g.ew[k] for k in own)
        chains = sorted({self.ref[k][1] for k in own})
        room = min(max(DRIVABLE / self.grade0[c], 1.0) / self.lam[c] for c in chains)
        f = min(1.0 + short / allowed, room)
        if f <= 1.0:
            return 0.0
        for c in chains:
            self.lam[c] *= f
            for e, w in self.grade_edges[c]:
                g.ew[e] = w * self.lam[c]
                changed.append(e)
        return allowed * (f - 1.0)

    # ---- what was eased ----

    def report(self):
        """[Relaxed] of everything eased: per constraint the total it was eased by (a grade law once a road, by its factor)."""
        out = []
        for k in sorted(k for k, v in self.dropped.items() if v is not None):
            a, b = self.place(self.g.eu[k]), self.place(self.g.ev[k])
            out.append(Relaxed("layer", self.dropped[k], (a, b), "decision %s over %s by the layers alone (%.3g apart, at s=%.0f "
                               "/ %.0f) given up, %.2f m of clearance: it contradicts the joins, the grade law and what is "
                               "weightier" % (self._name(a[0]), self._name(b[0]), self.weak[k], a[1], b[1], self.dropped[k])))
        for k in sorted(self.eased):
            kind, *rest = self.ref[k]
            amount = self.eased[k]
            if kind == "cp":
                a, b = self.place(self.g.eu[k]), self.place(self.g.ev[k])
                need = self.cps[rest[0]][5]
                out.append(Relaxed("clearance", amount, (a, b), "clearance of %s over %s at s=%.0f / %.0f: %.2f m asked, "
                                   "%.2f m kept" % (self._name(a[0]), self._name(b[0]), a[1], b[1], need, need - amount)))
            elif kind in ("floor", "ceiling"):
                c, s = rest
                at = self.place(self.off[c] + s)
                lim = float((self.chains[c]["lo"] if kind == "floor" else self.chains[c]["hi"])[s])
                if self.klass[k] == CLEARANCE:
                    out.append(Relaxed("clearance", amount, (at,), "clearance %s a road the shape does not move at s=%.0f of "
                                       "%s: %.2f m of the bound given up (%s %.2f m, now %.2f m)" % (
                                           "over" if kind == "floor" else "under", at[1], self._name(at[0]), amount, kind, lim,
                                           lim - amount if kind == "floor" else lim + amount)))
                else:
                    out.append(Relaxed(kind, amount, (at,), "%s of %s at s=%.0f given up by %.2f m (%.2f m, now %.2f m): %s" % (
                        kind, self._name(at[0]), at[1], amount, lim, lim - amount if kind == "floor" else lim + amount,
                        "the deck's girders over the ground it spans" if kind == "floor" else "a road going under never rises "
                        "over its ground")))
        for c in range(len(self.chains)):
            if self.lam[c] != 1.0:
                pc = self.pcs[c]
                at = (int(pc["main"]), 0.0)
                out.append(Relaxed("grade", self.grade0[c] * (self.lam[c] - 1.0), (at,),
                                   "grade law of the %s piece of %s eased from %.2f%% to %.2f%% (x%.3f)" % (
                                       pc["kind"], self._name(pc["main"]), 100.0 * self.grade0[c],
                                       100.0 * self.grade0[c] * self.lam[c], self.lam[c])))
        return out

    def eased_problem(self):
        """(chains, couplings) as the kernel is given them with what was eased: the same objects' copies, the laws moved."""
        chains = [dict(ch) for ch in self.chains]
        for c, ch in enumerate(chains):
            ch["lo"], ch["hi"] = np.array(ch["lo"], np.float64), np.array(ch["hi"], np.float64)
            ch["grade"] = self.grade0[c] * self.lam[c]
        cps = list(self.cps)
        gone = {self.ref[k][1] for k in self.dropped}
        for k, amount in self.eased.items():
            kind, *rest = self.ref[k]
            if kind == "cp":
                a, i, b, j, how, val = cps[rest[0]]
                cps[rest[0]] = (a, i, b, j, how, val - amount)
            elif kind == "floor":
                chains[rest[0]]["lo"][rest[1]] -= amount
            else:
                chains[rest[0]]["hi"][rest[1]] += amount
        return chains, [cp for n_, cp in enumerate(cps) if n_ not in gone]


    def touched(self):
        """The chains whose laws were eased (a bound, a clearance, a grade law, a layer decision): where the roads are at the
        limit of what they can do."""
        out = {c for c in range(len(self.chains)) if self.lam[c] != 1.0}
        for k in list(self.eased) + list(self.dropped):
            kind, *rest = self.ref[k]
            out |= {self.cps[rest[0]][0], self.cps[rest[0]][2]} if kind == "cp" else {rest[0]}
        return out


def relax(net, pcs, smp, chains, cps, trace):
    """(chains, couplings, [Relaxed], touched): the problem (shape_problem's, with its trace) with the laws that contradict
    eased by the least, in the order of the module's docstring; touched: the chains whose laws were eased."""
    p = Problem(net, pcs, smp, chains, cps, trace)
    done = p.relax()
    ch, cp = p.eased_problem()
    return ch, cp, done, p.touched()


CURVE_STEPS = 7         # halvings of the factor's bracket (a ratio of 2 to 1 + 1 %): the curve law is eased to 1 % of the least


def curve_floor(net, pc):
    """K (the kernel's: the radius over 100, m) of the tightest vertical curve the laws give the piece's road: any road's
    (R_MIN_ROAD_M), a track's (R_MIN_RAIL_M)."""
    rail = net.m[pc["main"]].kind in ("rail", "rail_deck")
    return (_gsh.R_MIN_RAIL_M if rail else _gsh.R_MIN_ROAD_M) / 100.0


def ease_curves(net, pcs, chains, cps, touched, feasible, hops=2):
    """(chains, [Relaxed]): the vertical curve law eased where the curves are what is left to contradict. feasible(chains,
    couplings) -> whether the kernel gives them heights. The radius of the chains in touched (where the other laws were eased:
    these roads are at the limit of what they can do; all of them where none was) is divided by the least factor that gives
    the chains within hops couplings of them heights (found on those alone: a few roads, solved in a moment), none past
    curve_floor. The caller checks the whole and asks again with more hops where it still has none. Nothing is changed where
    the curves hold; ValueError where they are at their floor and the chains still have none (the curves are not the cause)."""
    n = len(chains)
    near = [set() for _ in range(n)]
    for cp in cps:
        near[cp[0]].add(cp[2])
        near[cp[2]].add(cp[0])
    ease = sorted(touched) if touched else list(range(n))
    easeset = set(ease)
    region = set(ease)
    for _ in range(hops):
        grown = region | {b for a in region for b in near[a]}
        if grown == region:
            break
        region = grown
    order = sorted(region)
    idx = {c: i for i, c in enumerate(order)}
    sub_cps = [(idx[cp[0]], cp[1], idx[cp[2]], cp[3]) + tuple(cp[4:]) for cp in cps if cp[0] in region and cp[2] in region]
    floor = {c: min(chains[c]["K"], curve_floor(net, pcs[c])) for c in ease}
    most = max(chains[c]["K"] / floor[c] for c in ease)          # (the factor past which every radius is at its floor)

    def radius(c, mu):
        return max(chains[c]["K"] / mu, floor[c]) if c in easeset else chains[c]["K"]

    def scaled(mu):
        return [dict(chains[c], K=radius(c, mu)) for c in order]
    mu = 1.0
    if not feasible(scaled(1.0), sub_cps):
        if most <= 1.0 or not feasible(scaled(most), sub_cps):
            raise ValueError("the curves are not what contradicts %d roads and the %d round them: their radius at the "
                             "tightest the laws give a road (%g m) still has no heights" % (
                                 len(ease), len(region) - len(ease), _gsh.R_MIN_ROAD_M))
        lo, hi = 1.0, most
        for _ in range(CURVE_STEPS):
            mid = math.sqrt(lo * hi)
            if feasible(scaled(mid), sub_cps):
                hi = mid
            else:
                lo = mid
        mu = hi
    out = [dict(ch) for ch in chains]
    done = []
    if mu > 1.0:
        for c in ease:
            out[c]["K"] = radius(c, mu)
            if out[c]["K"] < chains[c]["K"]:
                done.append(Relaxed("curve", chains[c]["K"] / out[c]["K"], ((int(pcs[c]["main"]), 0.0),),
                                    "vertical curve law of the %s piece of %s eased: radius divided by %.2f" % (
                                        pcs[c]["kind"], str(net.m[pcs[c]["main"]].key[0])[:8] + ":" + net.m[pcs[c]["main"]].kind,
                                        chains[c]["K"] / out[c]["K"])))
    return out, done
