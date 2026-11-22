"""The interchange solver's order and heights steps (ix/s3_order.py, ix/relax.py, ix/diffgraph.py): a road's decisions
at its crossings are consistent with its joins, and where the heights cannot all hold what contradicts is relaxed by the least,
never the whole interchange given up."""
import os
import random
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import gradeshapes as gs  # noqa: E402
from gradeshapes import Member  # noqa: E402
from ix import diffgraph, relax, s3_order  # noqa: E402


def line(x0, y0, x1, y1, n=13):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def deck(key, pts, level, cls="secondary"):
    return Member(key, np.asarray(pts, np.float64), "deck", cls, 60, level, 5.0, None)


def zero(xy):
    return np.zeros(len(xy))


def fixed(net, ground):
    def f(i, s):
        sv = np.atleast_1d(np.asarray(s, np.float64))
        z = ground(np.stack([np.interp(sv, net.s[i], net.lines[i][:, 0]), np.interp(sv, net.s[i], net.lines[i][:, 1])],
                            axis=1))
        return np.asarray(z, np.float64) if np.ndim(s) else float(z[0])
    return f


def find(members, ground=zero):
    net, systems = gs.find_systems(members, lambda n_: (ground, fixed(n_, ground)))
    return net, systems


def by_key(net, key):
    return [n for n, m in enumerate(net.m) if m.key == key][0]


def hand(members, crossings, joins):
    """A net and a system of the crossings and joins given by hand: (key, s) pairs; the crossings are (upper, s, lower, s)."""
    net = gs.Net(members)
    ix = {m.key: n for n, m in enumerate(members)}
    xs = []
    for u, su, d, sd in crossings:
        q = gs.at_s(net.lines[ix[u]], net.s[ix[u]], su)
        xs.append(dict(u=ix[u], su=float(su), d=ix[d], sd=float(sd), q=q, moves="up", under=np.array([[su, sd]])))
    js = [(ix[a], float(sa), ix[b], float(sb)) for a, sa, b, sb in joins]
    return net, dict(seeds=sorted(ix.values()), joins=js, crossings=xs, ends=[], dips=[], digs=set(), free={})


def pairs(net, xs):
    return sorted((str(net.m[c["u"]].key), str(net.m[c["d"]].key), round(c["su"], 3)) for c in xs)


# ---- the difference graph ----

def test_a_negative_cycle_is_eased_and_the_labels_hold():
    g = diffgraph.Graph(3)
    g.add(0, 1, -1.0)
    g.add(1, 2, -1.0)
    g.add(2, 0, 1.5)
    seen = []

    def yield_(cyc):
        seen.append(sorted(cyc))
        for k in cyc:
            g.ew[k] += 0.3
        return cyc
    g.settle(yield_)
    assert seen == [[0, 1, 2]]
    assert g.check() < 1e-9


# ---- the order step ----

def flipping_road(near=2.5, arm=8.0):
    """R is one road: R1 (layer 1) then R2 (layer 3) joined at its middle; A (layer 2) passes under R2 and over R1 round
    a short U: over R1 where R is layer 1, under R2 where it is layer 3, the two crossings 2 x near apart along R, A's U
    arm m long: 12.7 m of clearance to climb between them, in 2 x near of R and 2 x (near + arm) of A: more than a vehicle
    drives at near = 2.5, arm = 8, a climb it can make at near = 20, arm = 30."""
    return [deck("R1", line(-100, 0, 0, 0), 1), deck("R2", line(0, 0, 100, 0), 3),
            deck("A", [(-near, -100), (-near, arm), (near, arm), (near, -100)], 2)]


def test_a_layer_flip_along_a_continuous_road_gives_up_one_layer_decision():
    net, systems = find(flipping_road())
    assert len(systems) == 1
    kept, dropped, deferred = s3_order.decide(net, systems[0])
    assert len(systems[0]["crossings"]) == 3 and len(kept) == 2 and len(dropped) == 1 and not deferred
    assert "layers" in dropped[0][1] and "joins" in dropped[0][1]
    # (what stands holds with the joins: the system has heights, nothing eased)
    s3_order.settle(net, systems[0])
    pcs = gs.pieces(net, systems[0])
    smp = gs.sample(net, pcs)
    diag = {}
    zs = gs.solve_shape(net, systems[0], pcs, smp, [zero(x["xy"]) for x in smp], fixed(net, zero), diag)
    assert zs is not None and not diag.get("relaxed")


def test_a_loop_a_vehicle_can_drive_is_no_contradiction():
    # (the class's own grade law (6 %) cannot make 12.7 m in 40 m, a vehicle's (45 %) can: nothing is given up in the order)
    net, systems = find(flipping_road(20.0, 30.0))
    kept, dropped, deferred = s3_order.decide(net, systems[0])
    assert len(kept) == 2 and not dropped and not deferred


def test_a_three_road_cycle_gives_up_exactly_one_decision():
    ms = []
    for k in "ABC":
        ms += [deck(k + "1", line(0, 0, 60, 0), 3), deck(k + "2", line(60, 0, 120, 0), 1)]
    cross = [("A1", 55, "B2", 5), ("B1", 55, "C2", 5), ("C1", 55, "A2", 5)]
    joins = [(k + "1", 60, k + "2", 0) for k in "ABC"]
    net, sysm = hand(ms, cross, joins)
    kept, dropped, deferred = s3_order.decide(net, sysm)
    assert len(kept) == 2 and len(dropped) == 1 and not deferred
    assert str(net.m[dropped[0][0]["u"]].key) == "A1"       # (all alike: the first by key)


def test_structure_decisions_are_never_given_up_in_the_order_step():
    # (bridge over ground, ground over tunnel: kinds decide; the loop of them is the heights step's)
    ms = [deck("D1", line(0, 0, 60, 0), 1), Member("T1", line(60, 0, 120, 0), "tunnel", "secondary", 60, -1, 5.0, None),
          Member("G1", line(0, 0, 60, 0), "ground", "secondary", 60, 0, 5.0, None),
          Member("G2", line(60, 0, 120, 0), "ground", "secondary", 60, 0, 5.0, None)]
    cross = [("D1", 55, "G2", 5), ("G1", 55, "T1", 5)]
    net, sysm = hand(ms, cross, [("D1", 60, "T1", 0), ("G1", 60, "G2", 0)])
    kept, dropped, deferred = s3_order.decide(net, sysm)
    assert len(kept) == 2 and not dropped and len(deferred) == 1


def test_the_order_does_not_depend_on_the_input_order():
    base = flipping_road()
    ref = None
    for seed in range(6):
        ms = list(base)
        random.Random(seed).shuffle(ms)
        net, systems = find(ms)
        kept, dropped, _ = s3_order.decide(net, systems[0])
        got = (pairs(net, kept), pairs(net, [c for c, _w in dropped]))
        assert ref is None or got == ref
        ref = got


def test_the_dropped_decisions_are_in_the_artifact():
    net, systems = find(flipping_road())
    loc = {g: n for n, g in enumerate(range(len(net.m)))}
    art = s3_order.run(None, net, systems[0], loc, "sys", "system:sys")
    notes = [f for f in art.findings if f.code == "order_dropped"]
    assert len(notes) == 1 and len(art.crossings) == 2 and not [f for f in art.findings if f.level == "fail"]


# ---- the heights step ----

def test_a_contradiction_a_vehicle_can_drive_is_eased_in_grade_and_no_decision_is_given_up():
    net, systems = find(flipping_road(20.0, 30.0))
    sysm = systems[0]            # (the order step not run: the contradicting decisions both stand)
    pcs = gs.pieces(net, sysm)
    smp = gs.sample(net, pcs)
    diag = {}
    zs = gs.solve_shape(net, sysm, pcs, smp, [zero(x["xy"]) for x in smp], fixed(net, zero), diag)
    assert zs is not None, diag
    done = diag["relaxed"]
    # (12.7 m of climb in 130 m is a grade of 10 %: steeper than the class's law, a grade every vehicle drives: the road
    # climbs it, and no road is put through another)
    assert {r["kind"] for r in done} == {"grade"}, done
    assert all(0.06 < float(r["text"].split(" to ")[1].split("%")[0]) / 100.0 <= relax.DRIVABLE for r in done)
    assert diag["feasible"] and diag["converged"]


def two_decks(need, level_a=1, level_b=2, grade=0.06):
    """two_chains' problem of two decks of one kind (a clearance between them is a decision of the layers alone)."""
    net, pcs, smp, chains, cps = two_chains(need=need, grade=grade)
    net = gs.Net([deck("A", line(0, 0, 40, 0, 11), level_a), deck("B", line(40, 0, 80, 0, 11), level_b)])
    return net, pcs, smp, chains, cps


def test_a_decision_of_the_layers_is_given_up_after_the_grade_has_given_all_a_vehicle_drives():
    # (a climb the grade law can make: the grade eased, the decision kept)
    net, pcs, smp, chains, cps = two_decks(need=20.0)
    done = relax.Problem(net, pcs, smp, chains, cps, dict(bound={})).relax()
    assert {r.kind for r in done} == {"grade"}
    # (more than a vehicle drives: the grade at what it drives, then the decision, whole)
    net, pcs, smp, chains, cps = two_decks(need=40.0)
    p = relax.Problem(net, pcs, smp, chains, cps, dict(bound={}))
    done = p.relax()
    assert sorted(r.kind for r in done) == ["grade", "grade", "layer"]
    assert all(abs(r.amount + 0.06 - relax.DRIVABLE) < 1e-9 for r in done if r.kind == "grade")
    ch, cp = p.eased_problem()
    assert not [c for c in cp if c[4] == "ge"]


def test_a_loop_of_decisions_alone_gives_up_the_weakest_and_no_grade_is_touched():
    net = gs.Net([deck("A", line(0, 0, 40, 0, 11), 1), deck("B", line(0, 5, 40, 5, 11), 3)])
    _, pcs, smp, chains, _cps = two_chains()
    # (A over B, and B over A, the same place: no grade can close it; the lesser difference of layers is the weaker)
    cps = [(0, 5, 1, 5, "ge", 3.0), (1, 5, 0, 5, "ge", 3.0)]
    p = relax.Problem(net, pcs, smp, chains, cps, dict(bound={}))
    done = p.relax()
    assert [r.kind for r in done] == ["layer"] and not p.eased


def test_an_infeasible_climb_is_relaxed_by_the_least_grade_excess():
    import carriageways
    net, pcs, smp, chains, cps = two_chains(need=8.0)
    p = relax.Problem(net, pcs, smp, chains, cps, dict(bound={}))
    done = p.relax()
    factors = {round(r.amount / 0.06 + 1.0, 6) for r in done}
    assert {r.kind for r in done} == {"grade"} and len(factors) == 1
    f = max(r.amount for r in done) / 0.06 + 1.0
    assert abs(f - (8.0 + relax.SLACK_M) / (0.06 * 80.0)) < 1e-6
    # (the kernel agrees: the eased laws have heights, the laws eased by a hair less still contradict)
    for under, feasible in ((0.97, False), (1.0, True)):
        ch, cp = p.eased_problem()
        for c in ch:
            c["grade"] = 0.06 * f * under
        info = {}
        carriageways.solve(ch, cp, diagnostics=info)
        assert bool(info["feasible"]) == feasible, (under, info["status"])


def valley_and_hill(xy):
    """Ground: flat to x = 0, a valley 10 m deep before it, a hill rising 30 m over the 200 m after it."""
    x = xy[:, 0]
    return np.where(x > 0, np.minimum(x, 200.0) * 0.15, -10.0 * np.exp(-((x + 150.0) / 60.0) ** 2))


def bridge_into_a_tunnel():
    """One road: a bridge over a road in a valley, going into a tunnel under a road on the hill."""
    return [Member("bridge", line(-300, 0, 0, 0, 40), "deck", "secondary", 60, 1, 5.0, None),
            Member("tunnel", line(0, 0, 200, 0, 30), "tunnel", "secondary", 60, -1, 5.0, None),
            Member("valley", line(-150, -200, -150, 200, 40), "ground", "secondary", 60, 0, 5.0, None),
            Member("hill", line(150, -200, 150, 200, 40), "ground", "secondary", 60, 0, 5.0, None)]


def test_a_bridge_that_becomes_a_tunnel_has_heights_and_nothing_to_relax():
    net, systems = find(bridge_into_a_tunnel(), valley_and_hill)
    assert len(systems) == 1
    sysm = systems[0]
    s3_order.settle(net, sysm)
    pcs = gs.pieces(net, sysm)
    smp = gs.sample(net, pcs)
    ground = [valley_and_hill(x["xy"]) for x in smp]
    diag = {}
    zs = gs.solve_shape(net, sysm, pcs, smp, ground, fixed(net, valley_and_hill), diag)
    assert zs is not None, diag
    assert not diag.get("relaxed"), diag["relaxed"]
    # (the bridge lands at the mouth, the tunnel stays under its ground)
    for pc, x, z, g in zip(pcs, smp, zs, ground):
        if pc["kind"] == "dip":
            assert (np.asarray(z) <= np.asarray(g) + 0.02).all()


def two_chains(lo=None, hi=None, need=None, grade=0.06):
    """A problem of two pieces on two members, 11 samples 4 m apart, their ends joined: A's last sample is B's first. lo: A's
    floor at its last sample; hi: B's ceiling at its first; need: a clearance of A's first sample over B's last."""
    net = gs.Net([deck("A", line(0, 0, 40, 0, 11), 1), Member("B", line(40, 0, 80, 0, 11), "tunnel", "secondary", 60, -1,
                                                                    5.0, None)])
    pcs = [dict(kind="deck", main=0, segs=[]), dict(kind="dip", main=1, segs=[])]
    smp = [dict(mem=np.zeros(11, int), ms=np.linspace(0, 40, 11)), dict(mem=np.ones(11, int), ms=np.linspace(0, 40, 11))]
    chains = []
    for _ in range(2):
        chains.append(dict(t=np.zeros(11), w=np.zeros(11), lo=np.full(11, -np.inf), hi=np.full(11, np.inf), grade=grade, K=1.0,
                           step=4.0, lam=0.0, length=0.0))
    cps = [(0, 10, 1, 0, "eq", 0.0)]
    if lo is not None:
        chains[0]["lo"][10] = lo
    if hi is not None:
        chains[1]["hi"][0] = hi
    if need is not None:
        cps.append((0, 0, 1, 10, "ge", need))
    return net, pcs, smp, chains, cps


def test_the_weakest_law_in_a_contradiction_gives_way_first():
    # (a floor over a ceiling at one joint: the deck's mapped bound and the road's, 10 m apart: both eased, 5 m each)
    net, pcs, smp, chains, cps = two_chains(lo=10.0, hi=0.0)
    p = relax.Problem(net, pcs, smp, chains, cps, dict(bound={}))
    done = p.relax()
    assert sorted(r.kind for r in done) == ["ceiling", "floor"]
    assert all(abs(r.amount - 5.0005) < 1e-3 for r in done)
    # (a clearance over the length of two pieces, past what the grade allows: the grade law, before the clearance)
    net, pcs, smp, chains, cps = two_chains(need=8.0)
    p = relax.Problem(net, pcs, smp, chains, cps, dict(bound={}))
    done = p.relax()
    assert [r.kind for r in done] == ["grade", "grade"]
    f = done[0].amount / 0.06 + 1.0
    assert abs(f - (8.0 + relax.SLACK_M) / (0.06 * 80.0)) < 1e-6          # (the least: 8 m over 80 m of road)
    # (more than the grade law gives but less than a vehicle drives: still the grade, no road is put through the other)
    net, pcs, smp, chains, cps = two_chains(need=20.0)
    done = relax.Problem(net, pcs, smp, chains, cps, dict(bound={})).relax()
    assert [r.kind for r in done] == ["grade", "grade"]
    # (more than a vehicle drives: the grade at what it drives, and the clearance for the rest)
    net, pcs, smp, chains, cps = two_chains(need=40.0)
    done = relax.Problem(net, pcs, smp, chains, cps, dict(bound={})).relax()
    assert sorted(r.kind for r in done) == ["clearance", "grade", "grade"]
    assert all(abs(r.amount + 0.06 - relax.DRIVABLE) < 1e-9 for r in done if r.kind == "grade")
    gave = next(r.amount for r in done if r.kind == "clearance")
    assert abs(gave - (40.0 + relax.SLACK_M - relax.DRIVABLE * 80.0)) < 1e-6
    # (a floor with a clearance: the floor first, the clearance kept whole)
    net, pcs, smp, chains, cps = two_chains(lo=10.0, hi=0.0, need=3.0)
    done = relax.Problem(net, pcs, smp, chains, cps, dict(bound={})).relax()
    assert {r.kind for r in done} == {"floor", "ceiling"}


def test_the_curve_law_gives_way_last_by_the_least_factor():
    # (a deck that must pass 3 m over its two ends: the grade (0.5) allows it, a vertical curve of the radius asked does not)
    import carriageways
    net, pcs, smp, chains, cps = two_chains()
    pcs, smp, chains, cps = pcs[:1], smp[:1], chains[:1], []
    chains[0].update(grade=0.5, K=200.0)
    chains[0]["lo"][5] = 3.0
    chains[0]["hi"][0] = chains[0]["hi"][10] = 0.0

    def feasible(ch, cp):
        info = {}
        carriageways.solve(ch, cp, diagnostics=info)
        return bool(info["feasible"])
    assert not feasible(chains, cps)
    assert relax.Problem(net, pcs, smp, chains, cps, dict(bound={})).relax() == []      # (no difference of heights contradicts)
    out, done = relax.ease_curves(net, pcs, chains, cps, set(), feasible)
    assert [r.kind for r in done] == ["curve"] and feasible(out, cps)
    mu = done[0].amount
    again = [dict(chains[0], K=chains[0]["K"] / (mu / 1.05))]
    assert not feasible(again, cps)                                     # (to within 5 %: the least)


def test_the_curve_law_is_not_eased_past_the_tightest_curve_the_laws_give_a_road():
    # (a deck that must pass 8 m over its two ends: no vertical curve of 60 m radius makes it, the curves are not the cause)
    import carriageways
    net, pcs, smp, chains, cps = two_chains()
    pcs, smp, chains, cps = pcs[:1], smp[:1], chains[:1], []
    chains[0].update(grade=0.5, K=200.0)
    chains[0]["lo"][5] = 8.0
    chains[0]["hi"][0] = chains[0]["hi"][10] = 0.0

    def feasible(ch, cp):
        info = {}
        carriageways.solve(ch, cp, diagnostics=info)
        return bool(info["feasible"])
    try:
        relax.ease_curves(net, pcs, chains, cps, set(), feasible)
    except ValueError as e:
        assert "tightest the laws give a road" in str(e)
    else:
        raise AssertionError("eased past the floor")
    floor = relax.curve_floor(net, pcs[0])
    assert floor == gs.R_MIN_ROAD_M / 100.0
    chains[0]["K"] = 200.0
    chains[0]["lo"][5] = 3.0
    out, done = relax.ease_curves(net, pcs, chains, cps, set(), feasible)
    assert out[0]["K"] >= floor and done[0].amount > 1.0


def test_the_relaxation_is_deterministic():
    runs = []
    for _ in range(2):
        net, systems = find(flipping_road(20.0, 30.0))
        sysm = systems[0]
        pcs = gs.pieces(net, sysm)
        smp = gs.sample(net, pcs)
        diag = {}
        zs = gs.solve_shape(net, sysm, pcs, smp, [zero(x["xy"]) for x in smp], fixed(net, zero), diag)
        runs.append((diag["relaxed"], [np.asarray(z).tobytes() for z in zs]))
    assert runs[0] == runs[1]


def test_no_pieces_no_relaxation():
    p = relax.Problem(gs.Net([]), [], [], [], [], dict(bound={}))
    assert p.relax() == []


def test_the_relaxations_are_in_the_artifact_and_the_step_line():
    import logging
    from ix import pipeline, s6_heights
    net, systems = find(flipping_road(20.0, 30.0))
    sysm = systems[0]
    pcs = gs.pieces(net, sysm)
    smp = gs.sample(net, pcs)
    diag = {}
    zs = gs.solve_shape(net, sysm, pcs, smp, [zero(x["xy"]) for x in smp], fixed(net, zero), diag)
    loc = {g: n for n, g in enumerate(range(len(net.m)))}
    art = s6_heights.run(None, net, sysm, loc, pcs, smp, zs, diag, "sys", "system:sys")
    assert art.solved and {f.code for f in art.findings} == {"relaxed_grade"}
    assert art.diag["relaxed"][0]["kind"] == "grade" and art.diag["first"]["status"] == "PrimalInfeasible"
    text = s6_heights.relax_text(diag)
    assert text.startswith("%d eased: grade x%d" % (len(diag["relaxed"]), len(diag["relaxed"])))
    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append((record.levelno, record.getMessage()))
    h = Grab()
    pipeline.log.addHandler(h)
    try:
        pipeline._line({}, "heights", "ab" * 20, 3, 2, 0.5, None, "r system s", text)
    finally:
        pipeline.log.removeHandler(h)
    assert records and records[0][0] >= logging.WARNING and "ok (3 eased: grade x3" in records[0][1]


def test_a_clearance_is_eased_no_further_than_the_structure_over_the_road():
    # (a road under a deck is eased to the deck's own depth over it and no further: past that the road stands in the girders)
    net, pcs, smp, chains, cps = two_chains(need=40.0)
    p = relax.Problem(net, pcs, smp, chains, cps, dict(bound={}))
    edge = [k for k in range(len(p.klass)) if p.klass[k] == relax.CLEARANCE][0]
    assert abs(p._room(edge) - (40.0 - gs.structure_depth(net, 0))) < 1e-9
    assert gs.clearance_need(net, 0, 1) == gs.headroom(net.m[1]) + gs.structure_depth(net, 0)


def test_a_first_attempt_that_is_relaxed_is_not_logged_as_a_miss(caplog):
    import logging
    import carriageways
    net, pcs, smp, chains, cps = two_chains(need=8.0)
    with caplog.at_level(logging.WARNING, logger="earthd.grading"):
        info = {}
        carriageways.solve(chains, cps, diagnostics=info, quiet=True)
        assert not info["feasible"] and not caplog.records
        carriageways.report(len(chains), len(cps), info)
        assert len(caplog.records) == 1 and "feasible=False" in caplog.records[0].getMessage()
        caplog.clear()
        # (the whole flow: the system is relaxed, solved, and the log has no line of a miss)
        net, systems = find(flipping_road(20.0, 30.0))
        sysm = systems[0]
        pcs = gs.pieces(net, sysm)
        smp = gs.sample(net, pcs)
        diag = {}
        assert gs.solve_shape(net, sysm, pcs, smp, [zero(x["xy"]) for x in smp], fixed(net, zero), diag) is not None
        assert diag["first"]["status"] == "PrimalInfeasible" and not caplog.records


def test_what_a_vehicle_drives_is_the_mods_limit():
    import re
    import gradelaw
    lua = os.path.join(os.path.dirname(os.path.dirname(HERE)), "mod", "beamng_earth", "lua", "ge", "extensions", "beamngEarth",
                       "navgraph.lua")
    with open(lua, encoding="utf-8") as f:
        got = re.search(r"^M\.MAX_GRADE\s*=\s*([0-9.]+)", f.read(), re.M)
    assert got and float(got.group(1)) == gradelaw.DRIVABLE_GRADE == relax.DRIVABLE
