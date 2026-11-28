"""grade3d.problem and grade3d.solve: the network's chains and couplings, the least earthwork that holds every join,
clearance and law, the answer measured from the heights alone; with no answer, the contradiction named."""
import numpy as np
import pytest

import gradeshapes as gsh
import linenet
from gradeshapes import Member
from grade3d import ground, laws, layers, network, problem, samples, solve


def _line(x0, y0, x1, y1, n=11):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def _m(key, line, kind="ground", cls="secondary", level=0.0, kmh=None, nodes=None, node_xy=None):
    return Member(key, np.asarray(line, np.float64), kind, "rail" if kind.startswith("rail") else cls, kmh, level,
                  4.0, None, nodes, node_xy=node_xy)


def setup_function(_f):
    linenet.reset()


def _pose(ms, elev=lambda xy: np.full(len(xy), 10.0)):
    net = network.build(ms)
    lay = layers.order(net)
    sm = samples.sample(net, lay)
    g = ground.smooth(sm, ground.read(elev, sm))
    lw = laws.lay(net, sm, g)
    return net, sm, lw, problem.pose(net, sm, lw)


def _run(ms, elev=lambda xy: np.full(len(xy), 10.0)):
    net, sm, lw, pr = _pose(ms, elev)
    sol = solve.solve(pr)
    return net, sm, lw, pr, sol, solve.measure(pr, sol.z)


def _z(net, sm, sol, key, x=None, y=None):
    i = [str(m.key) for m in net.members].index(key)
    p = net.at[i][0]
    xy, z = sm.xy[sm.of(p)], sol.z[sm.of(p)]
    k = np.argmin(np.abs(xy[:, 0] - x)) if x is not None else np.argmin(np.abs(xy[:, 1] - y))
    return float(z[k])


def _held(got):
    assert got["clear_short_max"] < 1e-5 and got["join_gap_max"] < 1e-6, got
    assert got["grade_excess_max"] < 1e-6 and got["curve_excess_max"] < 1e-6, got


def test_a_bridge_over_a_road_clears_it_and_its_approaches_meet_the_ground():
    ms = [_m("w", _line(-400, 0, -100, 0)), _m("deck", _line(-100, 0, 100, 0), "deck", level=1.0),
          _m("e", _line(100, 0, 400, 0)), _m("under", _line(0, -300, 0, 300))]
    net, sm, _lw, pr, sol, got = _run(ms)
    assert sol.diagnostics["status"] == "Solved", sol.diagnostics
    need = gsh.headroom(ms[3]) + gsh.deck_depth(200.0)
    assert _z(net, sm, sol, "deck", x=0.0) - _z(net, sm, sol, "under", y=0.0) >= need - 1e-5
    _held(got)
    assert abs(_z(net, sm, sol, "w", x=-400.0) - 10.0) < 1e-5      # (far off, the road on its terrain)
    assert abs(_z(net, sm, sol, "under", y=-300.0) - 10.0) < 1e-5


def test_the_faster_line_moves_least():
    # (a road deck over a track: the clearance is made by the road. Under the deck the terrain is neither's, so the
    # track may dip a little there where that saves the road's ramps; off it, it stays on its terrain)
    ms = [_m("deck", _line(-60, 0, 60, 0), "deck", level=1.0),
          _m("a", _line(-500, 0, -60, 0)), _m("b", _line(60, 0, 500, 0)),
          _m("rail", _line(0, -800, 0, 800), "rail")]
    net, sm, _lw, _pr, sol, got = _run(ms)
    _held(got)
    rail_move = abs(_z(net, sm, sol, "rail", y=0.0) - 10.0)
    deck_rise = _z(net, sm, sol, "deck", x=0.0) - 10.0
    assert deck_rise >= 5.0 and rail_move < 0.02 * deck_rise, (rail_move, deck_rise)
    assert abs(_z(net, sm, sol, "rail", y=-400.0) - 10.0) < 1e-5


def test_a_street_over_a_tunnel_stays_on_its_terrain_and_the_tunnel_goes_under():
    # (a street on the ground over a tunnel, its portals' approaches on either side: the slower street does not move)
    ms = [_m("w", _line(-600, 0, -150, 0, 46), cls="primary"), _m("tube", _line(-150, 0, 150, 0, 31), "tunnel",
                                                                    cls="primary", level=-1.0),
          _m("e", _line(150, 0, 600, 0, 46), cls="primary"), _m("street", _line(0, -300, 0, 300, 61), cls="residential")]
    net, sm, _lw, _pr, sol, got = _run(ms)
    _held(got)
    assert abs(_z(net, sm, sol, "street", y=0.0) - 10.0) < 1e-5
    assert _z(net, sm, sol, "tube", x=0.0) <= 10.0 - gsh.headroom(ms[1]) - gsh.COVER_M + 1e-5


def test_a_road_ending_near_a_tunnels_middle_is_not_joined_to_it():
    # (Suedtiroler Platz: a street ends 1.8 m off the line of the tunnel under it; the two share a map node, but at the
    # tunnel's portal, not there: no join, the street over the tunnel by its clearance)
    portal = ("b", 150.0, 0.0)
    ms = [_m("w", _line(-600, 0, -150, 0, 46), cls="primary", nodes=frozenset({"a"}), node_xy=(("a", -150.0, 0.0),)),
          _m("tube", _line(-150, 0, 150, 0, 31), "tunnel", cls="primary", level=-1.0, nodes=frozenset({"a", "b"}),
             node_xy=(("a", -150.0, 0.0), portal)),
          _m("e", _line(150, 0, 600, 0, 46), cls="primary", nodes=frozenset({"b"}), node_xy=(portal,)),
          _m("street", _line(0, 1.8, 0, 300, 31), cls="residential", nodes=frozenset({"b", "s"}),
             node_xy=(portal, ("s", 0.0, 300.0)))]
    net = network.build(ms)
    keys = [str(m.key) for m in net.members]
    st, tu = keys.index("street"), keys.index("tube")
    assert not any({i, j} == {st, tu} for i, _si, j, _sj in net.joins)
    _net, sm, _lw, pr, sol, got = _run(ms)
    _held(got)
    assert any(w == "clear" for w, _k in pr.why)


def _under_then_over():
    # (a level-2 deck, and a road mapped under it at level 1 that turns and comes back over it at level 3 20 m on: the
    # two never joined, the road would climb 12.7 m in 40 m. Two decks joined to each other too near their one crossing
    # are a joint, layers' "joined"; this is no single crossing's joint: two clearances that cannot both hold)
    return [_m("mid", _line(-300, 0, 300, 0, 61), "deck", level=2.0),
            _m("lo", np.array([[-10.0, -300.0], [-10.0, 10.0], [10.0, 10.0]]), "deck", level=1.0),
            _m("hi", _line(10, 10, 10, -300, 63), "deck", level=3.0)]


def test_a_contradiction_is_named_not_eased():
    _net, _sm, _lw, pr = _pose(_under_then_over())
    with pytest.raises(solve.NotSolved) as e:
        solve.solve(pr)
    msg = str(e.value)
    assert "clearance" in msg and "mid deck" in msg and ("lo deck" in msg or "hi deck" in msg), msg


def test_a_contradiction_is_named_by_what_lies_round_it():
    # (the same contradiction with a city's streets and bridges 5 km off, which hold: the search reads round the
    # conflict first, and names only what contradicts)
    far = [m._replace(line=m.line + np.array([5000.0, 5000.0])) for m in _grid_with_bridges()]
    _net, _sm, _lw, pr = _pose(_under_then_over() + far)
    with pytest.raises(solve.NotSolved) as e:
        solve.solve(pr)
    named = str(e.value).split("\n")[1:]
    assert named and all("mid deck" in ln or "lo deck" in ln or "hi deck" in ln for ln in named), named


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
        assert sol.diagnostics["blocks"] >= 4 and sol.diagnostics["status"] == "Solved", sol.diagnostics
        m = solve.measure(pr, sol.z)
        assert m["clear_short_max"] < 0.1 and m["join_gap_max"] < 1e-3, m
        assert np.abs(sol.z - whole.z).max() < 0.5, np.abs(sol.z - whole.z).max()


def test_a_block_takes_a_paths_runs_inside_it_and_the_couplings_of_two_samples_there():
    ms = _grid_with_bridges()
    _net, sm, _lw, pr = _pose(ms)
    bl = solve.blocks(pr, sm, block_m=400.0, ramp_m=50.0, margin_m=50.0)
    seen = np.zeros(len(sm), int)
    for b in bl:
        seen[b.idx] += 1
        assert len(b.idx) == sum(e - a + 1 for _p, a, e in b.runs) and len(b.couplings) == len(b.why)
        for c in b.couplings:
            pa, ia, pb, ib = c[:4]
            assert 0 <= ia <= b.runs[pa][2] - b.runs[pa][1] and 0 <= ib <= b.runs[pb][2] - b.runs[pb][1]
    assert seen.min() >= 1                              # (every sample in a block, those near a seam in two or four)
    w = sum(solve.blend_weight(sm.xy, b.core, 50.0) * np.isin(np.arange(len(sm)), b.idx) for b in bl)
    assert np.allclose(w, 1.0)


def test_problem_holds_joins_equal_and_clearances_over():
    ms = [_m("a", _line(0, 0, 200, 0)), _m("x", _line(100, -100, 100, 100), "deck", level=1.0),
          _m("t", _line(200, 0, 200, 80)), _m("u", _line(200, 0, 300, -50))]
    _net, _sm, _lw, pr = _pose(ms)
    assert {w for w, _k in pr.why} == {"join", "clear"}
    for c, (what, _k) in zip(pr.couplings, pr.why):
        assert c[4] == ("eq" if what == "join" else "ge")
    clear = [c for c, (what, _k) in zip(pr.couplings, pr.why) if what == "clear"]
    assert len(clear) >= 3 and len({c[3] for c in clear}) >= 3     # (the lower held along the deck's width)


_THREADS_SCRIPT = r"""
import os, sys, threading
sys.path.insert(0, sys.argv[1])
import numpy as np
from grade3d import solve
def work():
    n = 20
    A = np.random.rand(30, n); b = np.random.rand(30)
    assert solve.highs(np.ones(n), A_ub=-A, b_ub=-b, bounds=[(0, 10)] * n).status == 0
for k in range(100):
    ts = [threading.Thread(target=work) for _ in range(4)]
    for t in ts: t.start()
    for t in ts:
        t.join(20)
        if t.is_alive():
            os._exit(2)
print("ok")
"""


def test_threads_that_solve_end_and_the_process_exits():
    # (a thread that ran HiGHS itself never ended on Windows, its join waiting for ever: every solve runs on the
    # process's own HiGHS threads, solve.highs. Run apart, under a limit: a hang is a failure, not a stuck suite)
    import os
    import subprocess
    import sys
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    got = subprocess.run([sys.executable, "-c", _THREADS_SCRIPT, here], capture_output=True, text=True, timeout=120)
    assert got.returncode == 0 and got.stdout.strip() == "ok", (got.returncode, got.stdout, got.stderr[-2000:])
