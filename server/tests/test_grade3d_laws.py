"""grade3d.laws: each path's grade and vertical curve by class and speed (a track's by its city), and what moving each
sample off its terrain costs: its length, its width and its speed squared; nothing for a deck, a tunnel, or where two
lines are stacked."""
import numpy as np

import linenet
from gradeshapes import Member
from grade3d import laws, layers, network, samples


def _line(x0, y0, x1, y1, n=11):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def _m(key, line, kind="ground", cls="secondary", kmh=None, level=0.0, half=4.0):
    return Member(key, np.asarray(line, np.float64), kind, "rail" if kind.startswith("rail") else cls, kmh, level,
                  half, None, None)


def setup_function(_f):
    linenet.reset()


def _lay(ms, elev=None):
    net = network.build(ms)
    sm = samples.sample(net, layers.order(net))
    g = np.full(len(sm), 10.0) if elev is None else elev(sm.xy)
    return net, sm, laws.lay(net, sm, g)


def _p(net, key):
    return net.at[[str(m.key) for m in net.members].index(key)][0]


def test_a_faster_road_has_the_gentler_laws_and_costs_more_to_move():
    net, sm, lw = _lay([_m("mw", _line(0, 0, 500, 0), cls="motorway", kmh=110),
                        _m("st", _line(0, 300, 500, 300), cls="residential", kmh=30)])
    mw, st = _p(net, "mw"), _p(net, "st")
    assert lw.grade[sm.of(mw)].max() < lw.grade[sm.of(st)].min() and lw.R[sm.of(mw)].min() > lw.R[sm.of(st)].max()
    # (a metre of road a metre off its terrain: its width times (speed / REF_KMH)^2)
    per_m = {p: lw.w[sm.of(p)][1:-1] / sm.h[p] for p in (mw, st)}
    assert np.allclose(per_m[mw], 8.0 * (110 / laws.REF_KMH) ** 2)
    assert np.allclose(per_m[st], 8.0 * (30 / laws.REF_KMH) ** 2)
    # (a major road's curve no tighter than a car drives it at its class's driving speed)
    v = laws.DRIVE_KMH["motorway"] / 3.6
    assert lw.R[sm.of(mw)].min() >= v * v / (laws.DRIVE_LIFT_G * 9.81) - 1e-9


def test_a_link_going_on_into_its_motorway_keeps_its_own_laws():
    # (Pregerson's HOV connector chained into the Century Freeway: one path, the link's stretch held to the freeway's
    # 6.5 % and 844 m curves, its block with no answer)
    net, sm, lw = _lay([_m("link", _line(0, 0, 300, 0), cls="motorway_link"),
                        _m("mw", _line(300, 0, 900, 0), cls="motorway", kmh=110)])
    (p,) = range(len(net.paths))
    x = sm.xy[sm.of(p), 0]
    link, mw = x < 299.0, x > 301.0
    keys = [str(m.key) for m in net.members]
    want_link = laws.law_of(net.members[keys.index("link")])
    want_mw = laws.law_of(net.members[keys.index("mw")])
    assert want_link[0] > want_mw[0]
    assert np.allclose(lw.grade[sm.of(p)][link], want_link[0]) and np.allclose(lw.grade[sm.of(p)][mw], want_mw[0])
    assert np.allclose(lw.R[sm.of(p)][link], want_link[1]) and np.allclose(lw.R[sm.of(p)][mw], want_mw[1])
    gl = lw.glim[sm.of(p)][:-1]
    assert np.allclose(gl[x[:-1] < 298.0], want_link[0]) and np.allclose(gl[x[:-1] > 301.0], want_mw[0])


def test_a_track_in_a_city_bends_more_and_is_slower():
    streets = [_m("s%d" % k, _line(-250, y, 250, y), cls="residential") for k, y in enumerate(range(-200, 201, 50))]
    net, sm, lw = _lay([_m("city", _line(-200, 25, 200, 25), "rail")] + streets
                       + [_m("open", _line(5000, 0, 5400, 0), "rail")])
    c, o = _p(net, "city"), _p(net, "open")
    assert lw.urban[c] and not lw.urban[o]
    assert lw.R[sm.of(c)].max() < lw.R[sm.of(o)].min() and lw.grade[sm.of(c)].min() > lw.grade[sm.of(o)].max()
    assert lw.kmh[sm.of(c)].max() < lw.kmh[sm.of(o)].min()


def test_a_deck_and_a_tunnel_cost_nothing_to_move():
    ms = [_m("a", _line(0, 0, 100, 0)), _m("d", _line(100, 0, 300, 0), "deck", level=1.0),
          _m("b", _line(300, 0, 400, 0)), _m("t", _line(400, 0, 700, 0), "tunnel", level=-1.0),
          _m("c", _line(700, 0, 800, 0))]
    net, sm, lw = _lay(ms)
    (p,) = range(len(net.paths))
    x = sm.xy[sm.of(p), 0]
    assert np.all(lw.w[sm.of(p)][((x > 100.5) & (x < 299.5)) | ((x > 400.5) & (x < 699.5))] == 0.0)
    assert np.all(lw.w[sm.of(p)][(x > 0.5) & (x < 99)] > 0.0)


def test_where_two_lines_are_stacked_the_terrain_is_neithers():
    # (a deck over a street: the street's samples under the deck, and STACK_M past it, cost nothing to move)
    ms = [_m("w", _line(-300, 0, -60, 0)), _m("deck", _line(-60, 0, 60, 0, 13), "deck", level=1.0),
          _m("e", _line(60, 0, 300, 0)), _m("st", _line(0, -300, 0, 300, 61))]
    net, sm, lw = _lay(ms)
    st = _p(net, "st")
    y = sm.xy[sm.of(st), 1]
    w = lw.w[sm.of(st)]
    assert np.all(w[np.abs(y) <= 4.0 + laws.STACK_M] == 0.0)
    assert np.all(w[np.abs(y) > 4.0 + 1.5 + laws.STACK_M + 10.0] > 0.0)


def test_a_road_may_follow_ground_steeper_than_its_grade():
    net, sm, lw = _lay([_m("a", _line(0, 0, 400, 0, 41), cls="motorway", kmh=110)],
                       lambda xy: 10.0 + 0.2 * xy[:, 0])                   # (a 20 % slope under a motorway)
    p = _p(net, "a")
    gl = lw.glim[sm.of(p)][:-1]
    assert lw.grade[sm.of(p)].max() < 0.1 and np.allclose(gl, 0.2)


def test_the_chain_is_what_the_solve_reads():
    net, sm, lw = _lay([_m("a", _line(0, 0, 200, 0))])
    ch = lw.chain(sm, 0)
    assert set(ch) == {"t", "w", "xy", "ds", "glim", "R", "step"}
    assert len(ch["t"]) == len(sm) and len(ch["ds"]) == len(ch["glim"]) == len(sm) - 1
    assert abs(ch["step"] - sm.h[0]) < 1e-12
