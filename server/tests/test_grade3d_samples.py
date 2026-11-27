"""grade3d.samples: each path at an even step, on the member it is on there; joins and crossings at their nearest
samples."""
import numpy as np

import linenet
from gradeshapes import Member
from grade3d import layers, network, samples


def _line(x0, y0, x1, y1, n=6):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def _m(key, line, kind="ground", level=0.0):
    return Member(key, np.asarray(line, np.float64), kind, "rail" if kind.startswith("rail") else "secondary", 50,
                  level, 4.0, None, None)


def setup_function(_f):
    linenet.reset()


def test_an_even_step_from_end_to_end_on_each_member():
    ms = [_m("a", _line(0, 0, 101, 0)), _m("b", _line(161, 0, 101, 0), "deck", 1.0), _m("c", _line(161, 0, 301, 0))]
    net = network.build(ms)
    sm = samples.sample(net)
    (h,) = sm.h
    assert abs(h - 301.0 / 75) < 1e-12 and len(sm) == 76
    assert np.allclose(np.diff(sm.s), h)
    xs = sm.xy[:, 0] if sm.xy[0, 0] < 1 else sm.xy[::-1, 0]
    assert np.allclose(xs, np.linspace(0, 301, 76), atol=1e-9) and np.allclose(sm.xy[:, 1], 0)
    on_deck = sm.member == 1
    x = sm.xy[on_deck, 0]
    assert x.min() >= 101 - 1e-9 and x.max() <= 161 + 1e-9 and on_deck.sum() >= 14


def test_a_crossing_and_a_join_at_their_nearest_samples():
    ms = [_m("a", _line(0, 0, 200, 0)), _m("x", _line(50, -100, 50, 100), "deck", 1.0), _m("t", _line(150, 0, 150, 80))]
    net = network.build(ms)
    sm = samples.sample(net, layers.order(net))
    (pa, ia, pb, ib, lay), = sm.crossings
    for p, i in ((pa, ia), (pb, ib)):
        assert np.hypot(*(sm.xy[sm.of(p)][i] - [50, 0])) <= sm.h[p] / 2 + 1e-9
    assert lay.upper is not None
    (pa, ia, pb, ib), = sm.joins
    for p, i in ((pa, ia), (pb, ib)):
        assert np.hypot(*(sm.xy[sm.of(p)][i] - [150, 0])) <= sm.h[p] / 2 + 1e-9


def test_the_flat_arrays_are_the_paths_in_order():
    ms = [_m("a", _line(0, 0, 40, 0)), _m("b", _line(0, 50, 0, 90)), _m("p", np.array([[500.0, 500.0]]))]
    net = network.build(ms)
    sm = samples.sample(net)
    assert sm.off[-1] == len(sm) == len(sm.xy) == len(sm.member)
    assert [sm.of(p).stop - sm.of(p).start for p in range(3)] == [11, 11, 1]
    assert sm.h[2] == 0.0


def test_a_join_and_a_crossing_are_exactly_at_a_sample():
    # (a T 61.3 m along a road, a deck crossing it at 137.9 m: neither on the even 4 m grid)
    ms = [_m("a", _line(0, 0, 301, 0, 31)), _m("t", _line(61.3, 0, 61.3, 120, 13)),
          _m("x", _line(137.9, -60, 137.9, 60, 13), "deck", 1.0)]
    net = network.build(ms)
    sm = samples.sample(net, layers.order(net))
    (pa, ia, pb, ib), = sm.joins
    for p, i in ((pa, ia), (pb, ib)):
        assert np.hypot(*(sm.xy[sm.of(p)][i] - [61.3, 0])) < 1e-6
    (pa, ia, pb, ib, _lay), = sm.crossings
    for p, i in ((pa, ia), (pb, ib)):
        assert np.hypot(*(sm.xy[sm.of(p)][i] - [137.9, 0])) < 1e-6
    for p in range(len(sm.h)):
        d = np.diff(sm.s[sm.of(p)])
        assert (d > 0).all() and d.max() <= 1.5 * sm.h[p] + 1e-9          # (a sample moved half a step at most)


def test_grid_moves_the_nearest_sample_or_puts_one_in():
    s = samples.grid(40.0, [9.0, 9.02, 21.9, 40.0])
    assert np.allclose(s, [0, 4, 9.0, 12, 16, 20, 21.9, 28, 32, 36, 40][:len(s)]) or (9.0 in s and 21.9 in s)
    assert 9.0 in s and 21.9 in s and np.all(np.diff(s) > 0) and s[0] == 0 and s[-1] == 40.0
