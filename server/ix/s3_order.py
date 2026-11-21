"""Step 3 (SPEC.md): per system each crossing's upper and lower, the clearance it asks (clearance_need) and the carriageway
pairs it holds over (under_pairs), the members' signs and their level order, as the Order artifact.

The order of a crossing is decided from the evidence, strongest first: the structure (a bridge piece is over what it spans,
a tunnel under what is over it: the members' kinds, gradeshapes._over's first rule), then the relative layer of the two
roads where they cross (its second: the one layer against the other, never a section's layer on its own). Each decision
alone is consistent (the kinds and layers make one total order of members), but a road is continuous: its consecutive
sections meet at one height (the joins), and a layer that changes along a carriageway (the map tags its ways one by one)
puts it under a road at one crossing and over it a section later. Those decisions cannot all hold: the crossings, the
joins and the grade law between them close a loop that needs a negative rise. They are found here, as negative cycles of the
difference constraints of the system's crossings (z_over - z_under >= clearance), joins (equal heights) and the grade law
along each member between those points, the heights left free (the ground's too: the elevation is the heights step's), and each
is resolved by giving up the weakest evidence in it: a decision from layers alone, the smallest layer difference first,
before one from the structure. A structure's decision is never given up here: a loop of structure decisions only is left to
the heights step, which eases the clearance by the least it takes (relax.py). Every decision given up is recorded in the
artifact with its reason. Pure of the input's order: the crossings and joins are taken in order of their roads' keys."""
import math

import numpy as np

import gradeshapes as _gsh

from . import artifacts, cache, diffgraph

FORMAT = 3
LAWS = (("gradeshapes", ["HEADROOM_M", "RAIL_HEADROOM_M", "HEADROOM_MARGIN_M", "CLEAR_PATH_M", "DECK_MIN_DEPTH_M",
                         "DECK_FULL_DEPTH_M", "DECK_SPAN_RATIO", "RAIL_DECK_DEPTH_M", "COVER_M", "DECK_MAX_GRADE",
                         "JOIN_M"]),)
LAWS_DIGEST = cache.laws_digest(*LAWS)
STEP = 3

SIGN = {"tunnel": -1, "ground": 0, "rail": 0, "deck": 1, "rail_deck": 1}


def levels(net, members, edges):
    """({member: int level}, findings): a longest-path layering of 'd below u', level[u] > level[d] at every crossing
    (each member starts at its sign's band: 0 on the ground). A tunnel over something pushes that down, anything else
    rises over what it crosses, so the ground stays at 0 as far as the order lets it. A cycle (a road over itself) is a
    finding, never an exception: the last pass' levels stand."""
    lv = {i: SIGN[net.m[i].kind] for i in members}
    for _ in range(len(members) + 1):
        changed = False
        for u, d in edges:
            if lv[u] > lv[d]:
                continue
            if SIGN[net.m[u].kind] < 0:
                lv[d] = lv[u] - 1
            else:
                lv[u] = lv[d] + 1
            changed = True
        if not changed:
            return lv, []
    names = ", ".join(sorted({str(net.m[i].key[0]) for i in members})[:8])
    return lv, [artifacts.Finding(STEP, "fail", "order_cycle", "the crossings of %s loop (a road over itself)" % names)]


def _law(m):
    """The steepest grade member m's heights hold (gradeshapes._law: a deck's own, else its class's)."""
    return _gsh.deck_grade(m) if m.kind == "deck" else _gsh.max_grade(m)


def evidence(net, c):
    """(strength, text) of crossing c's order: (1, "structure") when the members' kinds decide it, else (0, ...) with the
    layer difference (a larger one is stronger). Compared as tuples: the weakest is the least."""
    mu, md = net.m[c["u"]], net.m[c["d"]]
    if _gsh.LEVEL_BAND[mu.kind] != _gsh.LEVEL_BAND[md.kind]:
        return (1, 0.0), "structure: %s over %s" % (mu.kind, md.kind)
    return (0, abs(float(mu.level) - float(md.level))), "layers: %g over %g" % (float(mu.level), float(md.level))


def _name(net, i):
    return str(net.m[i].key[0])[:8] + ":" + net.m[i].kind


def _tie(net, c):
    """A crossing's place in the order that does not depend on how the members were numbered."""
    return (str(net.m[c["u"]].key), round(float(c["su"]), 3), str(net.m[c["d"]].key), round(float(c["sd"]), 3))


def decide(net, sysm):
    """(kept, dropped, deferred): the system's crossings made consistent with its joins. kept: the crossings that stand,
    in their order; dropped: [(crossing, why)], the decisions given up (layer evidence in a loop); deferred: [text] of the
    loops of structure decisions only, which the heights step eases. Nothing is changed in sysm."""
    xs = sorted(sysm["crossings"], key=lambda c: _tie(net, c))
    joins = sorted(((i, si, j, sj) for i, si, j, sj in sysm["joins"]),
                   key=lambda r: (str(net.m[r[0]].key), round(float(r[1]), 3), str(net.m[r[2]].key), round(float(r[3]), 3)))
    pts = {}                    # member -> [(s, node)]
    n = [0]

    def node(i, s):
        k = n[0]
        n[0] += 1
        pts.setdefault(int(i), []).append((float(s), k))
        return k
    cross, welds = [], []
    for c in xs:
        cross.append((node(c["u"], c["su"]), node(c["d"], c["sd"])))
    for i, si, j, sj in joins:
        welds.append((node(i, si), node(j, sj)))
    g = diffgraph.Graph(n[0])
    for i in sorted(pts, key=lambda i: str(net.m[i].key)):
        law = _law(net.m[i])
        lst = sorted(pts[i])
        for (s0, a), (s1, b) in zip(lst, lst[1:]):
            g.add(a, b, law * (s1 - s0))
            g.add(b, a, law * (s1 - s0))
    for a, b in welds:
        g.add(a, b, 0.0)
        g.add(b, a, 0.0)
    edge_of = {}
    for ci, (a, b) in enumerate(cross):
        c = xs[ci]
        edge_of[g.add(a, b, -_gsh.clearance_need(net, c["u"], c["d"], c["q"]))] = ci
    dropped, deferred = {}, []

    def yield_(cyc):
        inx = [k for k in cyc if k in edge_of]
        short = -math.fsum(g.ew[k] for k in cyc)
        layer = [k for k in inx if evidence(net, xs[edge_of[k]])[0][0] == 0]
        if layer:
            k = min(layer, key=lambda k: (evidence(net, xs[edge_of[k]])[0], _tie(net, xs[edge_of[k]])))
            ci = edge_of[k]
            c = xs[ci]
            others = sorted(_name(net, xs[edge_of[q]]["u"]) + " over " + _name(net, xs[edge_of[q]]["d"])
                            for q in inx if q != k)
            dropped[ci] = ("%s: the decision %s over %s closes a loop with the joins, %.2f m short of the clearance and the "
                           "grade law along %d crossings%s: given up (the weakest evidence in it)"
                           % (evidence(net, c)[1], _name(net, c["u"]), _name(net, c["d"]), short, len(inx),
                              (" (with " + ", ".join(others[:4]) + (", ..." if len(others) > 4 else "") + ")")
                              if others else ""))
            g.ew[k] = math.inf
            return [k]
        # (structure decisions only: the loop is real, the clearance is the heights step's to ease; here it is eased in
        # the analysis alone, equally over its crossings, so that the search goes on to the loops after it)
        by = sorted(inx, key=lambda k: _tie(net, xs[edge_of[k]]))
        deferred.append("%d structure decisions close a loop with the joins, %.2f m short: %s" % (
            len(inx), short, ", ".join(_name(net, xs[edge_of[k]]["u"]) + " over " + _name(net, xs[edge_of[k]]["d"])
                                       for k in by[:4]) + (", ..." if len(by) > 4 else "")))
        each = (short + diffgraph.CONTRADICTION_M) / len(inx)
        for k in inx:
            g.ew[k] += each
        return inx
    g.settle(yield_)
    kept = [c for ci, c in enumerate(xs) if ci not in dropped]
    return kept, [(xs[ci], why) for ci, why in sorted(dropped.items())], deferred


def settle(net, sysm):
    """The system's crossings made consistent with its joins (decide), in place: sysm["crossings"] the ones that stand, and
    sysm["order"] = {"dropped": [(crossing, why)], "deferred": [text], "gone": {frozenset((i, j))}} (gone: the pairs of
    members no crossing of which stands: what gradeshapes._stacked must not hold over each other either). Returns it."""
    kept, dropped, deferred = decide(net, sysm)
    stands = {frozenset((int(c["u"]), int(c["d"]))) for c in kept}
    sysm["crossings"] = kept
    sysm["order"] = dict(dropped=dropped, deferred=deferred,
                         gone={p for p in (frozenset((int(c["u"]), int(c["d"]))) for c, _why in dropped) if p not in stands})
    return sysm["order"]


def run(planner, net, sysm, loc, system, scope):
    """The Order artifact of one system: its crossings (the system's, settled: settle) with their under_pairs and
    clearance_need, the members' signs and levels, the members numbered as the system's Paths' (loc); the decisions
    given up as notes."""
    order = sysm.get("order") or settle(net, sysm)
    crossings = [artifacts.Crossing(loc[int(c["u"])], float(c["su"]), loc[int(c["d"])], float(c["sd"]),
                                     np.asarray(c["q"], np.float64),
                                     float(_gsh.clearance_need(net, c["u"], c["d"], c["q"])),
                                     np.asarray(c["under"], np.float64).reshape(-1, 2))
                 for c in sysm["crossings"]]
    members = sorted(set(int(i) for i in sysm["seeds"]) | {int(c["u"]) for c in sysm["crossings"]} |
                     {int(c["d"]) for c in sysm["crossings"]})
    lv, findings = levels(net, members, [(int(c["u"]), int(c["d"])) for c in sysm["crossings"]])
    for c, why in order["dropped"]:
        findings.append(artifacts.Finding(STEP, "note", "order_dropped", why,
                                          ((loc[int(c["u"])], float(c["su"])), (loc[int(c["d"])], float(c["sd"])))))
    for text in order["deferred"]:
        findings.append(artifacts.Finding(STEP, "note", "order_deferred", text))
    return artifacts.Order(artifacts.Meta(STEP, FORMAT, "", (), LAWS_DIGEST, scope), system, crossings,
                           {str(loc[i]): SIGN[net.m[i].kind] for i in members},
                           {str(loc[i]): int(lv[i]) for i in members}, findings)
