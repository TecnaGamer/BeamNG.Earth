"""grade3d.network: members chained into whole paths where exactly two ends of one family meet and nothing else touches;
the joins and crossings between paths, each once."""
import numpy as np

import linenet
from gradeshapes import Member
from grade3d import network


def _line(x0, y0, x1, y1, n=6):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def _m(key, line, kind="ground", cls="secondary", level=0.0):
    return Member(key, np.asarray(line, np.float64), kind, "rail" if kind.startswith("rail") else cls, 50, level, 4.0,
                  None, None)


def _paths(net):
    return [[str(net.members[i].key) for i in p.members()] for p in net.paths]


def setup_function(_f):
    linenet.reset()


def test_a_road_keeps_one_path_across_its_bridge():
    ms = [_m("a", _line(0, 0, 100, 0)), _m("b", _line(100, 0, 160, 0), "deck", level=1),
          _m("c", _line(160, 0, 300, 0)), _m("x", _line(130, -50, 130, 50))]
    net = network.build(ms)
    assert sorted(_paths(net)) == [["a", "b", "c"], ["x"]]
    p = net.paths[net.at[0][0]]
    assert abs(p.length - 300.0) < 1e-9
    assert net.joins == []                           # (the chain's own joins are not listed)
    (i, si, j, sj, q), = net.crossings
    assert {str(ms[i].key), str(ms[j].key)} == {"b", "x"} and abs(q[0] - 130.0) < 1e-6
    assert abs(net.path_s(1, 30.0)[1] - 130.0) < 1e-9


def test_a_member_drawn_backwards_is_walked_backwards():
    ms = [_m("a", _line(0, 0, 100, 0)), _m("b", _line(160, 0, 100, 0), "deck", level=1),
          _m("c", _line(160, 0, 300, 0))]
    net = network.build(ms)
    (p,), = [net.paths]
    assert [f for _i, f in p.parts] in ([True, False, True], [False, True, False])
    s_b0 = net.path_s(1, 0.0)[1]                     # (b's start is at x = 160)
    s_a0 = net.path_s(0, 0.0)[1]
    assert abs(abs(s_b0 - s_a0) - 160.0) < 1e-9


def test_a_fork_a_t_and_a_dead_end_end_paths():
    ms = [_m("a", _line(0, 0, 100, 0)), _m("b", _line(100, 0, 200, 0)), _m("c", _line(100, 0, 200, 80)),   # (fork)
          _m("d", _line(50, 0, 50, -80))]                                                                   # (T on a)
    net = network.build(ms)
    assert sorted(_paths(net)) == [["a"], ["b"], ["c"], ["d"]]
    pairs = sorted(tuple(sorted((str(ms[i].key), str(ms[j].key)))) for i, _si, j, _sj in net.joins)
    assert pairs == [("a", "b"), ("a", "c"), ("a", "d"), ("b", "c")]
    assert net.crossings == []


def test_a_track_is_not_chained_to_a_road_it_meets():
    ms = [_m("r", _line(0, 0, 100, 0), "rail"), _m("s", _line(100, 0, 200, 0)), _m("t", _line(200, 0, 300, 0), "rail")]
    net = network.build(ms)
    assert sorted(_paths(net)) == [["r"], ["s"], ["t"]]


def test_a_ring_is_one_path():
    ms = [_m("n", _line(0, 0, 100, 0)), _m("e", _line(100, 0, 100, 100)), _m("s", _line(100, 100, 0, 100)),
          _m("w", _line(0, 100, 0, 0))]
    net = network.build(ms)
    (p,), = [net.paths]
    assert sorted(p.members()) == [0, 1, 2, 3] and abs(p.length - 400.0) < 1e-9


def test_every_member_is_in_one_path_and_paths_are_continuous():
    rng = np.random.default_rng(3)
    ms = []
    for k in range(60):                             # (a lattice of streets, cut into pieces at random)
        x0, y0 = rng.integers(0, 6, 2) * 50.0
        horiz = bool(rng.integers(0, 2))
        x1, y1 = (x0 + 50.0, y0) if horiz else (x0, y0 + 50.0)
        kind = "deck" if rng.random() < 0.2 else "ground"
        ms.append(_m("m%02d" % k, _line(x0, y0, x1, y1, 4), kind, level=1.0 if kind == "deck" else 0.0))
    net = network.build(ms)
    seen = sorted(i for p in net.paths for i in p.members())
    assert seen == sorted(set(seen)) and len(set(seen)) == len(ms)
    for p in net.paths:
        ends = []
        for (i, f), s0 in zip(p.parts, p.start):
            line = net.net.lines[i]
            a, b = (line[0], line[-1]) if f else (line[-1], line[0])
            ends.append((a, b))
        for (_a, b), (a2, _b2) in zip(ends, ends[1:]):
            assert np.hypot(*(a2 - b)) <= 2.5 + 1e-9
