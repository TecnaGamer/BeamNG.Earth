"""The staged grade solver, stages 1 to 4 in order, and the comparison with today's pipeline (the design is in staged_paths'
docstring: stage inputs and outputs, what each check guarantees, how stage 5 (heights) will consume it).

    run(members, meta, ll, edge, ground, reader=True) -> Staged     the four stages on one map
    Staged.report()                                                  markdown: per stage pass / fail with the top failures
    compare(staged, today, source_net) -> dict                       today's find_systems answer against the staged systems

A Staged keeps every stage's answer (paths, levels, grounds), its findings and its stats, the staged systems with what they
say about levels, and per system whether it has heights. No time budget anywhere; the same members in, the same answer out.
Nothing here is called by Earth: it runs beside gradeshapes.find_systems and solve_shape and changes nothing they build."""
import math

import interchange_audit as ia
import gradeshapes as gs
import staged_feasible as s4
import staged_ground as s3
import staged_levels as s2
import staged_paths as s1

TOP = 8


class Staged:
    def __init__(self):
        self.finds = {1: [], 2: [], 3: [], 4: []}
        self.stats = {}
        self.facts = []
        self.feasible = {}              # system index -> stats of stage 4
        self.P = self.L = self.G = self.namer = self.net = self.ground = None
        self.name = ""

    def fails(self, step):
        return [f for f in self.finds[step] if f.level == "fail"]

    def counts(self, step):
        out = {}
        for f in self.finds[step]:
            out.setdefault((f.code, f.level), []).append(f)
        return out

    def passed(self, step):
        return not self.fails(step)

    def report(self, top=TOP):
        lines = ["# Staged solver: %s" % self.name, ""]
        lines += ["| stage | pass | failures | notes |", "|---|---|---|---|"]
        names = {1: "paths", 2: "level order", 3: "ground points, systems", 4: "feasibility"}
        for st in (1, 2, 3, 4):
            nf = len(self.fails(st))
            nn = len([f for f in self.finds[st] if f.level == "note"])
            lines.append("| %d %s | %s | %d | %d |" % (st, names[st], "pass" if nf == 0 else "FAIL", nf, nn))
        lines.append("")
        for st in (1, 2, 3, 4):
            lines += ["## Stage %d: %s" % (st, names[st]), "", "stats: `%s`" % {k: v for k, v in self.stats[st].items()
                                                                                if k not in ("potentials",)}, ""]
            for (code, level), fs in sorted(self.counts(st).items(), key=lambda kv: (kv[0][1] != "fail", -len(kv[1]), kv[0][0])):
                lines.append("### %s %s: %d" % (level, code, len(fs)))
                lines += ["- %s" % f.text for f in fs[:top]]
                if len(fs) > top:
                    lines.append("- ... and %d more" % (len(fs) - top))
                lines.append("")
        return "\n".join(lines) + "\n"


def run(members, meta=None, ll=None, edge=None, ground=None, reader=True, feasibility=True, name=""):
    """The four stages on one map. members: gradeshapes.Member list (as grading._shape_members gives them); meta: what the map has of
    each road by Earth feature id; ll(x, y) -> (lat, lon) for naming; edge(xy) -> at the edge of the data read; ground: an object with
    at(i, s), xy(points), fixed(i, s) and remap(net, source) (tests/staged_fixtures.Ground); reader: ask the elevation for who moves
    (needs ground); feasibility: run stage 4."""
    S = Staged()
    S.name = name
    S.P = P = s1.build(members, meta, edge)
    S.net = net = P.net
    S.namer = namer = ia.Namer(net, ll, meta, edge)
    S.finds[1], S.stats[1] = s1.check(P, namer)
    S.ground = g = ground.remap(net, P.cleaned.source, P.cleaned.offset) if ground is not None else None
    S.L = L = s2.build(P, g.xy if g is not None else None)
    S.finds[2], S.stats[2] = s2.check(L, namer)
    S.G = G = s3.build(P, L, (lambda n_: (g.xy, g.fixed)) if (g is not None and reader) else None)
    S.finds[3], S.stats[3], S.facts = s3.check(G, namer)
    st4 = dict(systems=len(G.systems), checked=0, feasible=0, infeasible=0, nodes=0, edges=0, owner={})
    if feasibility and g is not None:
        for n, sysm in enumerate(G.systems):
            finds, stats = s4.check_system(net, sysm, g, namer, n)
            S.finds[4] += finds
            stats.pop("potentials", None)
            S.feasible[n] = stats
            st4["checked"] += 1
            st4["feasible" if stats["feasible"] else "infeasible"] += 1
            st4["nodes"] += stats.get("nodes", 0)
            st4["edges"] += stats.get("edges", 0)
            if not stats["feasible"]:
                st4["owner"][stats.get("owner")] = st4["owner"].get(stats.get("owner"), 0) + 1
    S.stats[4] = st4
    return S


def today_sizes(systems):
    """(seeds, pieces = decks + dips) of each of today's systems (gradeshapes.find_systems records), largest first."""
    return sorted(((len(s["seeds"]), len(s["seeds"]) + len(s["dips"])) for s in systems), reverse=True)


def compare(S, today, today_net):
    """Today's find_systems answer (records over the members as given: today_net) against the staged systems (over the cleaned net:
    S.P.cleaned.source[k] is the input member of cleaned member k). Returns {sizes, split, merged, moves, ...}."""
    cl = S.P.cleaned
    net = S.net
    to_in = lambda k: cl.source[k]
    sys_of_today = {}
    for n, s in enumerate(today):
        for i in s["seeds"]:
            sys_of_today[i] = n
    staged_of = {}
    for n, sysm in enumerate(S.G.systems):
        for i in sysm["seeds"]:
            staged_of[to_in(i)] = n
    # which of today's systems each staged one lies in (by its seeds), and the other way
    split = {}
    for i, n in staged_of.items():
        if i in sys_of_today:
            split.setdefault(sys_of_today[i], set()).add(n)
    merged = {}
    for i, n in staged_of.items():
        if i in sys_of_today:
            merged.setdefault(n, set()).add(sys_of_today[i])
    only_today = [i for i in sys_of_today if i not in staged_of]
    only_staged = [i for i in staged_of if i not in sys_of_today]
    # who moves at the crossings both see (u, d, the crossing's place)
    def key(u, d, q):
        return (u, d, round(float(q[0]) / 4.0), round(float(q[1]) / 4.0))
    tm = {}
    for s in today:
        for c in s["crossings"]:
            tm[key(c["u"], c["d"], c["q"])] = c["moves"]
    agree = differ = missing = 0
    for sysm in S.G.systems:
        for c in sysm["crossings"]:
            k = key(to_in(c["u"]), to_in(c["d"]), c["q"])
            if k in tm:
                if tm[k] == c["moves"]:
                    agree += 1
                else:
                    differ += 1
            else:
                missing += 1
    return dict(today_systems=len(today), staged_systems=len(S.G.systems), today_sizes=today_sizes(today)[:8],
                staged_sizes=sorted(((len(s["seeds"]), len(s["seeds"]) + len(s["dips"])) for s in S.G.systems), reverse=True)[:8],
                today_split_into=sorted(len(v) for v in split.values() if len(v) > 1)[::-1][:6],
                staged_joins_today=sorted(len(v) for v in merged.values() if len(v) > 1)[::-1][:6],
                seeds_only_today=len(only_today), seeds_only_staged=len(only_staged),
                moves_agree=agree, moves_differ=differ, moves_unmatched=missing)
