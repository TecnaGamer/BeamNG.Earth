"""linenet: the line network (earthfast.LineNet) gives gradeshapes' relations to the bit, and finds every pair that has
one."""
import struct

import numpy as np

import gradeshapes as gsh
import linenet
from gradeshapes import Member

KINDS = ("ground", "deck", "tunnel", "rail", "rail_deck")


def _same(a, b):
    """a and b equal to the bit: floats by their bytes, arrays by dtype, shape and bytes, sequences element by element."""
    if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
        a, b = np.asarray(a), np.asarray(b)
        return a.dtype == b.dtype and a.shape == b.shape and a.tobytes() == b.tobytes()
    if isinstance(a, float) or isinstance(b, float):
        return isinstance(a, float) and isinstance(b, float) and struct.pack("<d", a) == struct.pack("<d", b)
    if isinstance(a, (tuple, list)):
        return isinstance(b, (tuple, list)) and len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _reference(net, i):
    """Member i's relations made in Python with no index: every other member of two points or more, ascending, through
    _pair_relations (gradeshapes' own geometry)."""
    out = []
    for j in range(len(net.m)):
        if j != i and len(net.lines[j]) >= 2:
            out.extend((j,) + r_ for r_ in gsh._pair_relations(net, i, j))
    return out


def _check(members):
    """Every member's relations from the network (gradeshapes._relations) against the Python made with no index: the
    same, to the bit; arc lengths and boxes as gradeshapes made them."""
    net = gsh.Net(members)
    for i in range(len(members)):
        assert _same(net.s[i], gsh.arclen(net.lines[i])), i
        assert _same(net.box[i], np.r_[net.lines[i].min(0), net.lines[i].max(0)]), i
        if len(net.lines[i]) < 2:
            assert gsh._relations(net, i) == []
            continue
        want = _reference(net, i)
        got = gsh._relations(net, i)
        assert _same(got, want), (i, got, want)
    return net


def _line(x0, y0, x1, y1, n=10):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def test_a_deck_over_a_road_and_the_roads_it_joins():
    m = [Member("mway", _line(-300, 0, 300, 0, 61), "ground", "motorway", 120, 0, 12.0, None),
         Member("deck", _line(0, -30, 0, 30, 13), "deck", "secondary", 60, 1, 5.0, None),
         Member("south", _line(0, -300, 0, -30), "ground", "secondary", 60, 0, 5.0, None),
         Member("north", _line(0, 30, 0, 300), "ground", "secondary", 60, 0, 5.0, None)]
    net = _check(m)
    kinds = sorted((j, r) for j, r, *_ in gsh._relations(net, 1))
    assert kinds == [(0, "cross"), (2, "join"), (3, "join")]


def test_ends_on_a_vertex_on_a_middle_and_lines_along_each_other():
    m = [Member("a", np.array([[0.0, 0.0], [10.0, 0.0], [20.0, 0.0]]), "deck", "primary", 50, 1, 4.0, None),
         Member("b", np.array([[10.0, 0.0], [10.0, 15.0]]), "deck", "primary", 50, 2, 4.0, None),     # on a's vertex
         Member("c", np.array([[5.0, -1.0], [5.0, -20.0]]), "ground", "residential", 30, 0, 3.0, None),   # 1 m off
         Member("d", np.array([[2.0, 0.0], [18.0, 0.0]]), "tunnel", "primary", 50, -1, 4.0, None),    # along a
         Member("e", np.array([[20.0, 0.0], [20.0, 0.0], [30.0, 0.0]]), "deck", "primary", 50, 1, 4.0, None),  # a 0 m segment
         Member("f", np.array([[0.0, 2.5], [20.0, 2.5000001]]), "rail", "rail", None, 0, 2.0, None)]       # near-parallel
    _check(m)


def test_random_networks():
    rng = np.random.default_rng(641)
    for trial in range(40):
        m = []
        for k in range(rng.integers(10, 40)):
            n = int(rng.integers(2, 9))
            p = np.cumsum(rng.normal(0.0, 15.0, (n, 2)), axis=0) + rng.uniform(-60, 60, 2)
            if trial % 2:
                p = np.round(p / 2.5) * 2.5         # (on a lattice: shared vertices, collinear runs, ends on lines)
            kind = KINDS[int(rng.integers(0, len(KINDS)))]
            nodes = None if rng.random() < 0.3 else frozenset(int(v) for v in rng.integers(0, 6, 2))
            m.append(Member("m%d" % k, p, kind, "rail" if kind.startswith("rail") else "primary", 50,
                            float(rng.integers(0, 3)), 4.0, None, nodes))
        _check(m)


def test_two_members_of_one_line_and_a_point():
    line = _line(0, 0, 50, 0, 6)
    m = [Member("a", line, "deck", "primary", 50, 1, 4.0, None),
         Member("b", line.copy(), "deck", "primary", 50, 2, 4.0, None),          # (another key on the same line)
         Member("p", np.array([[20.0, 1.0]]), "ground", "residential", 30, 0, 3.0, None),
         Member("c", _line(25, -20, 25, 20, 5), "ground", "residential", 30, 0, 3.0, None)]
    net = _check(m)
    assert 1 in {r_[0] for r_ in gsh._relations(net, 0)}           # (the deck on its twin)


def test_nets_share_the_process_network_and_add_a_line_once():
    linenet.reset()
    m = [Member("a", _line(0, 0, 50, 0), "deck", "primary", 50, 1, 4.0, None),
         Member("b", _line(25, -20, 25, 20), "ground", "residential", 30, 0, 3.0, None)]
    one = gsh.Net(m)
    two = gsh.Net(m[::-1] + [Member("c", _line(0, 5, 50, 5), "ground", "residential", 30, 0, 3.0, None)])
    assert one.network is two.network and len(one.network.ln) == 3
    assert two.ln_ids[:2] == one.ln_ids[::-1]
    assert _same(gsh._relations(two, 1), _reference(two, 1))


def test_lines_added_in_batches_pair_as_added_at_once():
    import fast
    rng = np.random.default_rng(7)
    lines = [np.cumsum(rng.normal(0.0, 20.0, (6, 2)), axis=0) for _ in range(80)]
    one = fast._ef.LineNet(gsh.JOIN_M + 0.5)
    one.add(lines)
    two = fast._ef.LineNet(gsh.JOIN_M + 0.5)
    assert two.add(lines[:30]) == 0
    assert two.add(lines[30:]) == 30
    assert len(two) == 80 and one.pairs == two.pairs
    for i in range(80):
        assert one.near(i) == two.near(i)
        assert _same(one.arclen(i), gsh.arclen(lines[i]))
