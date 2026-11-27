"""grade3d.problem and grade3d.solve: the network's chains and couplings, solved in one go, the answer measured from the
heights alone."""
import numpy as np

import gradeshapes as gsh
import linenet
from gradeshapes import Member
from grade3d import ground, laws, layers, network, problem, samples, solve


def _line(x0, y0, x1, y1, n=11):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def _m(key, line, kind="ground", cls="secondary", level=0.0, kmh=None):
    return Member(key, np.asarray(line, np.float64), kind, "rail" if kind.startswith("rail") else cls, kmh, level,
                  4.0, None, None)


def setup_function(_f):
    linenet.reset()


def _run(ms, elev=lambda xy: np.full(len(xy), 10.0)):
    net = network.build(ms)
    lay = layers.order(net)
    sm = samples.sample(net, lay)
    g = ground.smooth(sm, ground.read(elev, sm))
    lw = laws.lay(net, sm, g)
    pr = problem.pose(net, sm, lw)
    sol = solve.solve(pr)
    return net, sm, lw, pr, sol, solve.measure(pr, sol.z)


def _z(net, sm, sol, key, x=None, y=None):
    i = [str(m.key) for m in net.members].index(key)
    p = net.at[i][0]
    xy, z = sm.xy[sm.of(p)], sol.z[sm.of(p)]
    k = np.argmin(np.abs(xy[:, 0] - x)) if x is not None else np.argmin(np.abs(xy[:, 1] - y))
    return float(z[k])


def test_a_bridge_over_a_road_clears_it_and_its_approaches_meet_the_ground():
    ms = [_m("w", _line(-400, 0, -100, 0)), _m("deck", _line(-100, 0, 100, 0), "deck", level=1.0),
          _m("e", _line(100, 0, 400, 0)), _m("under", _line(0, -300, 0, 300))]
    net, sm, _lw, pr, sol, got = _run(ms)
    assert sol.diagnostics["status"] == "Solved", sol.diagnostics
    need = gsh.headroom(ms[3]) + gsh.deck_depth(200.0)
    assert _z(net, sm, sol, "deck", x=0.0) - _z(net, sm, sol, "under", y=0.0) >= need - 0.05
    assert got["clear_short_max"] < 0.05 and got["join_gap_max"] < 1e-4 and got["grade_excess_max"] < 1e-4
    assert abs(_z(net, sm, sol, "w", x=-400.0) - 10.0) < 0.5      # (far off, the road on its ground)


def test_a_track_is_moved_less_than_a_road():
    # (a road deck over a track and a road side by side: the clearance is made by the road, the track barely moves)
    ms = [_m("deck", _line(-60, 0, 60, 0), "deck", level=1.0),
          _m("a", _line(-500, 0, -60, 0)), _m("b", _line(60, 0, 500, 0)),
          _m("rail", _line(0, -800, 0, 800), "rail")]
    net, sm, _lw, _pr, sol, got = _run(ms)
    rail_move = abs(_z(net, sm, sol, "rail", y=0.0) - 10.0)
    deck_rise = _z(net, sm, sol, "deck", x=0.0) - 10.0
    assert got["clear_short_max"] < 0.05
    assert rail_move < 0.2 * deck_rise


def test_a_contradiction_is_answered_with_the_least_shortfall():
    # (two decks mapped as crossing one level apart, and a road 11 m long joining them 8 m from the crossing: within the
    # ceilings no answer holds the clearance)
    ms = [_m("hi", _line(-300, 0, 300, 0, 61), "deck", level=2.0), _m("lo", _line(0, -300, 0, 300, 61), "deck", level=1.0),
          _m("link", _line(0, 8, 8, 0, 4))]
    _net, _sm, _lw, pr, sol, got = _run(ms)
    assert sol.diagnostics["status"] == "Solved", sol.diagnostics
    assert got["join_gap_max"] < 1e-4 and got["grade_excess_max"] < 1e-4      # (the joins and the ceilings hold ...)
    assert got["clear_short_max"] > 0.5                                         # (... the clearance gives)


def _hilly(xy):
    return 10.0 + 6.0 * np.sin(xy[:, 0] / 90.0) + 4.0 * np.cos(xy[:, 1] / 70.0)


def _grid_with_bridges():
    ms = []
    for k, y in enumerate(range(0, 1201, 200)):          # (east-west streets, each with a bridge over x = 600)
        ms += [_m("w%d" % k, _line(0, y, 520, y, 27)), _m("d%d" % k, _line(520, y, 680, y, 9), "deck", level=1.0),
               _m("e%d" % k, _line(680, y, 1200, y, 27))]
    ms.append(_m("mw", _line(600, -100, 600, 1300, 71), cls="motorway", kmh=100))
    return ms


def test_blocks_blend_to_what_one_solve_gives_and_hold_its_clearances_across_a_seam():
    ms = _grid_with_bridges()
    net, sm, _lw, pr, whole, got = _run(ms, _hilly)
    for block_m in (400.0, 650.0):                      # (a seam beside the motorway, and one along it)
        sol = solve.solve_blocks(pr, sm, block_m=block_m, ramp_m=60.0, margin_m=250.0, workers=2)
        assert sol.diagnostics["blocks"] >= 4 and set(sol.diagnostics["status"]) == {"Solved"}, sol.diagnostics
        m = solve.measure(pr, sol.z)
        assert m["clear_short_max"] < 0.1 and m["join_gap_max"] < 1e-3, m
        assert np.abs(sol.z - whole.z).max() < 0.5, np.abs(sol.z - whole.z).max()


def test_a_block_takes_a_paths_runs_inside_it_and_the_couplings_of_two_samples_there():
    ms = _grid_with_bridges()
    net, sm, _lw, pr, _sol, _got = _run(ms)
    bl = solve.blocks(pr, sm, block_m=400.0, ramp_m=50.0, margin_m=50.0)
    seen = np.zeros(len(sm), int)
    for b in bl:
        seen[b.idx] += 1
        assert len(b.idx) == sum(e - a + 1 for _p, a, e in b.runs)
        for c in b.couplings:
            pa, ia, pb, ib = c[:4]
            assert 0 <= ia <= b.runs[pa][2] - b.runs[pa][1] and 0 <= ib <= b.runs[pb][2] - b.runs[pb][1]
    assert seen.min() >= 1                              # (every sample in a block, those near a seam in two or four)
    w = sum(solve.blend_weight(sm.xy, b.core, 50.0) * np.isin(np.arange(len(sm)), b.idx) for b in bl)
    assert np.allclose(w, 1.0)


def test_problem_couples_joins_hard_and_layers_soft():
    ms = [_m("a", _line(0, 0, 200, 0)), _m("x", _line(100, -100, 100, 100), "deck", level=1.0),
          _m("t", _line(200, 0, 200, 80)), _m("u", _line(200, 0, 300, -50))]
    net = network.build(ms)
    sm = samples.sample(net, layers.order(net))
    lw = laws.lay(net, sm, np.zeros(len(sm)))
    pr = problem.pose(net, sm, lw)
    for c, w, (what, _k) in zip(pr.couplings, pr.soft, pr.why):
        assert (c[4] == "eq") == (what == "join") and (w == 0.0) == (what == "join")
    clear = [c for c, (what, _k) in zip(pr.couplings, pr.why) if what == "clear"]
    assert len(clear) >= 3 and len({c[3] for c in clear}) >= 3     # (the lower held along the deck's width)


def test_carriageways_side_by_side_meet_and_a_bridge_ramp_beside_its_street_does_not():
    # (two roads 6 m apart, 4 m half widths: their carriageways overlap; the ground under one 1.5 m higher)
    ms = [_m("a", _line(0, 0, 400, 0, 41)), _m("b", _line(0, 6, 400, 6, 41))]
    net, sm, _lw, pr, sol, got = _run(ms, lambda xy: np.where(xy[:, 1] > 3, 11.5, 10.0))
    za, zb = _z(net, sm, sol, "a", x=200.0), _z(net, sm, sol, "b", x=200.0)
    assert abs(za - zb) < 0.1, (za, zb)
    assert any(w == "beside" for w, _k in pr.why)
    # (a bridge over a street, its west ramp beside the street for 100 m: no batter between them near the crossing)
    linenet.reset()
    ms = [_m("w", np.array([[-300.0, 20.0], [-60.0, 20.0]])), _m("deck", _line(-60, 20, 60, 20, 13), "deck", level=1.0),
          _m("e", _line(60, 20, 300, 20)), _m("st", _line(0, -300, 0, 300, 61)),
          _m("side", _line(-150, 14, -20, 14, 14))]
    net = network.build(ms)
    sm = samples.sample(net, layers.order(net))
    pairs = problem.beside(net, sm)
    names = {(str(net.members[sm.member[sm.off[p] + i]].key), str(net.members[sm.member[sm.off[r] + j]].key))
             for p, i, r, j, _g in pairs}
    assert ("w", "side") in names or ("side", "w") in names           # (two streets side by side: a batter)
    assert not any("st" in n for n in names)                          # (the street the bridge crosses: its clearance)


def _bend_case():
    # (a deck over a road forks at its top: the through deck goes on, a ramp deck leaves at 40 deg and comes down to the
    # ground; the ramp's own laws end at the fork, so only the bend law ties its grade to the deck's there)
    a = np.radians(40.0)
    ramp = np.stack([np.linspace(0.0, 160.0 * np.cos(a), 21), np.linspace(0.0, 160.0 * np.sin(a), 21)], axis=1)
    end = ramp[-1]
    return [_m("w", _line(-400, 0, -60, 0)), _m("d1", _line(-60, 0, 0, 0), "deck", level=1.0),
            _m("d2", _line(0, 0, 60, 0), "deck", level=1.0), _m("e", _line(60, 0, 400, 0)),
            _m("ramp", ramp, "deck", "secondary_link", level=1.0),
            _m("rg", _line(end[0], end[1], end[0] + 300 * np.cos(a), end[1] + 300 * np.sin(a))),
            _m("under", _line(-20, -300, -20, 300))]


def test_a_ramp_leaving_a_deck_at_a_fork_bends_across_it_no_more_than_a_drivable_curve():
    ms = _bend_case()
    net, sm, _lw, pr, sol, got = _run(ms)
    assert sol.diagnostics["status"] in ("Solved", "AlmostSolved"), sol.diagnostics
    assert pr.summary().get("bend", 0) > 0
    assert got["bend_excess_max"] < 0.02, got
    # (the lift a car driving off the deck onto the ramp feels across the fork, drivability's: v^2 |z(-5) - 2 z(0) + z(5)|
    # / 25 / g at the class's speed, at most the gate)
    off = np.r_[0, np.cumsum([len(c["t"]) for c in pr.chains])]
    for c, (what, _k) in zip(pr.couplings, pr.why):
        if what != "bend" or c[4] != "le":
            continue
        dev = float(solve._value(sol.z, off, c))
        assert abs(dev) <= c[5] + 0.02, (dev, c)


def test_without_the_bend_law_the_fork_kinks():
    ms = _bend_case()
    w = problem.W_BEND
    try:
        problem.W_BEND = 1e-9        # (0 would make it hard: the kernel holds an unweighted inequality)
        net, sm, _lw, pr, sol, got = _run(ms)
    finally:
        problem.W_BEND = w
    # (the same couplings, paid next to nothing: what they measure is how far the free answer bends)
    assert got["bend_excess_max"] > 0.02, got
