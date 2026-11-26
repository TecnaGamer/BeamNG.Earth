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


def _check(members):
    """Every member's relations from the network against gradeshapes': the same, to the bit; and every pair with a
    contact among the network's near pairs."""
    net = gsh.Net(members)
    lines = linenet.Lines(net)
    n = len(members)
    for i in range(n):
        if len(net.lines[i]) < 2:
            continue
        want = gsh._relations(net, i)
        got = lines.relations(i)
        assert _same(got, want), (i, got, want)
        near = set(lines.near(i))
        for j in range(n):
            if j == i or len(net.lines[j]) < 2:
                continue
            hits, ends = gsh._pair_geometry(net, i, j)
            if hits or ends:
                assert j in near, (i, j)
    return net, lines


def _line(x0, y0, x1, y1, n=10):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def test_a_deck_over_a_road_and_the_roads_it_joins():
    m = [Member("mway", _line(-300, 0, 300, 0, 61), "ground", "motorway", 120, 0, 12.0, None),
         Member("deck", _line(0, -30, 0, 30, 13), "deck", "secondary", 60, 1, 5.0, None),
         Member("south", _line(0, -300, 0, -30), "ground", "secondary", 60, 0, 5.0, None),
         Member("north", _line(0, 30, 0, 300), "ground", "secondary", 60, 0, 5.0, None)]
    net, lines = _check(m)
    kinds = sorted((j, r) for j, r, *_ in lines.relations(1))
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


def test_lines_added_in_batches_pair_as_added_at_once():
    import fast
    rng = np.random.default_rng(7)
    lines = [np.cumsum(rng.normal(0.0, 20.0, (6, 2)), axis=0) for _ in range(80)]
    one = fast._ef.LineNet(linenet.PAD_M)
    one.add(lines)
    two = fast._ef.LineNet(linenet.PAD_M)
    assert two.add(lines[:30]) == 0
    assert two.add(lines[30:]) == 30
    assert len(two) == 80 and one.pairs == two.pairs
    for i in range(80):
        assert one.near(i) == two.near(i)
        assert _same(one.arclen(i), gsh.arclen(lines[i]))
