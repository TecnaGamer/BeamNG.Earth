"""Stage 4 of the staged grade solver: does a system have heights at all, exactly (see staged_paths for the whole).

Input: one system of stage 3 (gradeshapes._system's record: its decks and underpasses, crossings with who moves, joins,
ends on the roads on the ground, dips) and the ground. Output: heights exist, or the map's contradiction.

The constraints, each a difference z_v - z_u <= w between two heights of the system (heights of the road, one per sample
of a piece: a deck whole, a road going under through its window; interchange_audit.Graph):
  grade       between neighbouring samples, by their own run: |dz| <= grade x run. The shapes hand the kernel one step for a
              piece (the closest two samples' run) and a piece through joints has samples 0 to 1.8 m apart where it typically
              has 3.9, so it held whole pieces to the grade over its shortest gap (23 pieces at one Shanghai place); here
              each step is its own length (interchange_audit.Constraints spacing "true");
  clearance   a part over another at a crossing, over the whole carriageway under it (gradeshapes.under_pairs), by
              gradeshapes.clearance_need (the signed limit or class headroom, rail's headroom, the deck's depth); a road the
              shapes do not move is read at its ground (fixed) and bounds the other;
  joint       the heights of two parts' ends that meet are one;
  floor       a deck's girders over the ground under its span, past its abutments; a road going under never rises over its
              ground;
  ground held a point found on the ground (stage 3) is at the ground's height: the road on the ground leading to a deck's end
              is its embankment, as steep as that road's grade and RAMP_MAX_M long, so the deck's end is within grade x that of
              the ground there; a road going under returns to its ground at the ends of its window.
The curves (second differences) are not differences: a graph with no negative cycle is necessary for the shape, not sufficient
(stage 5 decides the curves). A deck running along another without crossing it (gradeshapes._stacked) is not here either.

The check is interchange_audit's: SPFA from every node at 0, the heights it ends on are a certificate checked edge by edge;
a negative cycle is the contradiction, reduced until no constraint of it can be left out (the deletion filter), the metres it is
short and the factor of the grade law that would give heights, named as a loop of roads (crossing, joint, ground point,
road). It is the map's, never solved round: the numbers do not change by solving harder. Stage 5 starts from the potentials this
returns. Pure, deterministic (plain floats, the same edges in the same order)."""
import math
from collections import namedtuple

import numpy as np

import gradeshapes as gs
import interchange_audit as ia

Finding = ia.Finding
MAX_CONTRADICTIONS = 12      # most contradictions of one system named (each is taken out before the search goes on)
Hold = namedtuple("Hold", "node lo hi member s text")


class Constraints(ia.Constraints):
    """interchange_audit.Constraints plus the points held on the ground (kind "hold")."""

    def __init__(self, net, sysm, pcs, smp, ground, fixed_z, spacing, problem, holds=()):
        super().__init__(net, sysm, pcs, smp, ground, fixed_z, spacing, problem)
        self.holds = list(holds)
        g, Z = self.g, self.g.zero
        for n, h in enumerate(self.holds):
            if np.isfinite(h.lo):
                g.add(h.node, Z, -float(h.lo), "hold", (n, "lo"))
            if np.isfinite(h.hi):
                g.add(Z, h.node, float(h.hi), "hold", (n, "hi"))
        g.finish()

    def _leg(self, kind, ref, w, u, v, namer):
        if kind != "hold":
            return super()._leg(kind, ref, w, u, v, namer)
        h = self.holds[ref[0]]
        what = "at least" if ref[1] == "lo" else "at most"
        return ("ground point held: %s %s %.2f m (%s)" % (namer.label(h.member, h.s), what, abs(w), h.text),
                [namer.info(h.member)])


def ground_lists(ground, net, smp):
    """The ground under every sample of every piece (as the shapes hand it to shape_problem): [(m,) array] per piece."""
    out = []
    for x in smp:
        z = np.zeros(len(x["xy"]))
        mem, ms = np.asarray(x["mem"]), np.asarray(x["ms"], np.float64)
        for m in np.unique(mem).tolist():
            sel = mem == m
            z[sel] = ground.at(int(m), ms[sel])
        out.append(z)
    return out


def _available(segs):
    return sum(abs(s1 - s0) for _j, s0, s1 in segs)


def holds_of(net, sysm, pcs, smp, ground, cons_node):
    """The points held on the ground: [Hold]. cons_node(piece, sample) -> node of the constraint graph."""
    out = []
    piece = {pc["main"]: p for p, pc in enumerate(pcs) if pc["kind"] == "deck"}
    for i, si, j, sj in sysm["ends"]:
        if i not in piece or net.m[i].kind != "deck":
            continue
        segs = gs._onward(net, j, sj, sj < net.L[j] / 2)
        run = _available(segs)
        if run < gs.RAMP_MAX_M - 1e-6:
            continue                # (the road ends first, at a dead end or another structure: no ground to reach)
        g = gs.max_grade(net.m[j])
        # (the embankment reaches the ground somewhere along the road, at the first point where the road's ground is within its grade
        # of the end's height: the end can stand where some point d m on has the ground within g d of it, the hull of those
        # bands. A far end alone held it to the ground 600 m up the valley: a Zurich ramp's deck end 47 m up, the kernel's answer
        # 40 m outside the band)
        zg_lo, zg_hi, d0, at_d = math.inf, -math.inf, 0.0, None
        for mj, s0, s1 in segs:
            sv = np.linspace(s0, s1, max(2, int(math.ceil(abs(s1 - s0) / gs.STEP_M)) + 1))
            zz = np.asarray(ground.at(mj, sv), np.float64)
            dd = d0 + np.abs(sv - s0)
            ok = np.isfinite(zz)
            if ok.any():
                zg_lo = min(zg_lo, float((zz[ok] - g * dd[ok]).min()))
                zg_hi = max(zg_hi, float((zz[ok] + g * dd[ok]).max()))
            d0 += abs(s1 - s0)
        if not (math.isfinite(zg_lo) and math.isfinite(zg_hi)):
            continue
        k = 0 if si < net.L[i] / 2 else -1
        out.append(Hold(cons_node(piece[i], k), zg_lo - gs.SOFT_M, zg_hi + gs.SOFT_M, i, si,
                        "its end's embankment on %s reaches the ground within %.0f m, at %.1f%% (ground %.2f to %.2f m by then)"
                        % (str(net.m[j].cls), run, 100.0 * g, zg_lo, zg_hi)))
    dips = iter(sysm["dips"])
    for p, pc in enumerate(pcs):
        if pc["kind"] != "dip":
            continue
        d, a, b = next(dips)
        # (a window the road's end cut short, at a dead end or another structure, never reached its ground: it holds nothing)
        if _available(pc["segs"]) < (b - a) - 1e-6:
            continue
        x = smp[p]
        for e in (0, len(x["ms"]) - 1):
            m, s = int(x["mem"][e]), float(x["ms"][e])
            if any(q != p and gs._locate(smp, q, m, s) is not None for q in gs._pieces_of(smp, m)):
                continue            # (the road goes on under another window there: not the end of its dip)
            zg = float(ground.at(m, s))
            out.append(Hold(cons_node(p, e), zg - gs.SOFT_M, zg, m, s,
                            "the window of a road going under returns to its ground, %.2f m (within the %.1f m the plan's own grading takes it)" % (zg, gs.SOFT_M)))
    return out


def build(net, sysm, ground, spacing="true", holds=True):
    """(constraints, pieces, samples) of one system: the shapes' own problem (gradeshapes.shape_problem) as a difference graph
    with each step's own run, and the points held on the ground."""
    pcs = gs.pieces(net, sysm)
    smp = gs.sample(net, pcs)
    gl = ground_lists(ground, net, smp)
    trace = {}
    problem = gs.shape_problem(net, sysm, pcs, smp, gl, ground.fixed, trace) + (trace,)
    if holds:
        base = ia.Constraints(net, sysm, pcs, smp, gl, ground.fixed, spacing, problem)
        hs = holds_of(net, sysm, pcs, smp, ground, base.node)
    else:
        hs = []
    return Constraints(net, sysm, pcs, smp, gl, ground.fixed, spacing, problem, hs), pcs, smp


def owner_of(cont, namer):
    """Whose a contradiction is, from its loop: ("levels", text) when it runs through a joint of one road whose parts carry
    different levels (the map's local level ranges over the OSM ways it joins: roads._bridge_level_spans splits a road's bridge at
    them, no inference; Earth's reading takes each part's level as the map has it), else ("map", text): the roads, their kinds
    and levels as the map has them, their lengths and the laws."""
    for leg in cont["legs"]:
        if leg["kind"] in ("join", "overlap") and len(leg["refs"]) == 2:
            a, b = leg["refs"]
            if a["earth"] == b["earth"] and a["level"] != b["level"]:
                md = namer.meta.get(a["earth"]) or {}
                parts = str((md.get("tags") or {}).get("lane_parts") or "")
                ways = [w.strip("' ") for w in parts.replace("(", " ").replace(")", " ").split(",") if w.strip("' ").isdigit() and len(w.strip("' ")) > 5]
                return ("map_levels", "a joint of one road (%s) whose parts carry levels %g and %g (the map's layer for the road: %s%s): the "
                        "levels are the map's own local level ranges (roads._bridge_level_spans splits a road's bridge at them, nothing inferred), so "
                        "this is the map's, not the level reading's"
                        % (a.get("osm") or a["earth"][:8], a["level"], b["level"], md.get("layer"),
                           "; OSM ways it joins: " + ", ".join("w" + w for w in ways) if ways else ""))
    return "map", "the map's roads, kinds and levels, their lengths and the grade law"


def check_system(net, sysm, ground, namer, index=0):
    """Stage 4 on one system: (Finding list, stats). Fails: no_heights (the map's contradiction, with its loop of roads)."""
    pcs = gs.pieces(net, sysm)
    if not pcs:
        return [], dict(nodes=0, edges=0, feasible=True, pieces=0)
    cons, pcs, smp = build(net, sysm, ground)
    g = cons.g
    kinds = {}
    for k in g.kind:
        kinds[k] = kinds.get(k, 0) + 1
    dist, cyc = ia._spfa(g)
    got = None
    if cyc is not None:
        w = math.fsum(g.ew[k] for k in cyc)
        got = (cyc, w) if w < -ia.CONTRADICTION_M else None
    stats = dict(nodes=g.n - 1, edges=len(g.eu), kinds=dict(sorted(kinds.items())), feasible=got is None, pieces=len(pcs),
                 holds=len(cons.holds))
    finds = []
    if dist is not None:
        stats["certificate_violation_m"] = ia.certificate(g, dist)
        stats["potentials"] = dist
    # the contradictions, one at a time: the one the search finds first, its constraints taken out, the search again (a system
    # with two that are independent has both named, not whichever the search met first)
    skip = set()
    stats["contradictions"] = []
    while got is not None and len(stats["contradictions"]) < MAX_CONTRADICTIONS:
        cont = ia.contradiction(cons, got[0], namer)
        stats["contradictions"].append({k: v for k, v in cont.items() if k not in ("T", "cycle", "groups")})
        names = []
        for leg in cont["legs"]:
            for r in leg["refs"]:
                if r not in names:
                    names.append(r)
        cr = [cons.g.ref[cons.g.groups[gid][0]][1] for gid in cont["groups"] if cons.g.kind[cons.g.groups[gid][0]] == "clear"
              and len(cons.g.ref[cons.g.groups[gid][0]]) > 1]
        where = namer.point(sysm["crossings"][cr[0]]["q"]) if cr else namer.at(cons.pcs[0]["main"], 0.0)
        who, why = owner_of(cont, namer)
        stats["contradictions"][-1]["owner"] = who
        finds.append(Finding(4, "no_heights", "fail",
                             "system %d has no heights: %d constraints close %.2f m short (%.1f m asked over %.0f m of road that "
                             "allows %.1f m, the grade law %s as a factor of it); the map's, never solved round: %s"
                             % (index, cont["constraints"], cont["short_m"], cont["demand_m"], cont["run_m"], cont["allowed_m"],
                                "alone cannot" if cont["grade_factor"] is None else "x%.2f" % cont["grade_factor"], why), where, names))
        for gid in cont["groups"]:
            skip |= set(g.groups[gid])
        _d, cyc = ia._spfa(g, frozenset(skip))
        got = None
        if cyc is not None:
            w = math.fsum(g.ew[k] for k in cyc)
            got = (cyc, w) if w < -ia.CONTRADICTION_M else None
    if stats["contradictions"]:
        stats["owner"] = stats["contradictions"][0]["owner"]
        stats["short_m"] = max(c["short_m"] for c in stats["contradictions"])
    stats["independent_contradictions"] = len(stats["contradictions"])
    return finds, stats
