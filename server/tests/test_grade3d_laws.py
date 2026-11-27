"""grade3d.laws: each path's comfortable and ceiling grade and curve by class and speed, a track's by its city, a track
dear to move, a deck over its ground and a tunnel under it away from their ends."""
import numpy as np

import gradeshapes as gsh
import linenet
from gradeshapes import Member
from grade3d import laws, layers, network, samples


def _line(x0, y0, x1, y1, n=11):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def _m(key, line, kind="ground", cls="secondary", kmh=None, level=0.0):
    return Member(key, np.asarray(line, np.float64), kind, "rail" if kind.startswith("rail") else cls, kmh, level,
                  4.0, None, None)


def setup_function(_f):
    linenet.reset()


def _lay(ms):
    net = network.build(ms)
    sm = samples.sample(net)
    g = np.full(len(sm), 10.0)
    return net, sm, laws.lay(net, sm, g)


def _p(net, key):
    return net.at[[str(m.key) for m in net.members].index(key)][0]


def test_a_faster_road_has_the_gentler_laws_and_pays_more_past_them():
    net, _sm, lw = _lay([_m("mw", _line(0, 0, 500, 0), cls="motorway", kmh=110),
                         _m("st", _line(0, 300, 500, 300), cls="residential", kmh=30)])
    mw, st = _p(net, "mw"), _p(net, "st")
    assert lw.soft_grade[mw] < lw.soft_grade[st] and lw.soft_K[mw] > lw.soft_K[st]
    assert lw.w_grade[mw] > lw.w_grade[st] and lw.w_curve[mw] > lw.w_curve[st]
    # (a street's ceilings its comfortable laws' factors; a major road's no looser than a car drives at its class's speed)
    assert abs(lw.grade[st] - laws.CEIL_GRADE_X * lw.soft_grade[st]) < 1e-12
    assert abs(lw.K[st] * laws.CEIL_CURVE_X - lw.soft_K[st]) < 1e-12
    v = laws.DRIVE_KMH["motorway"] / 3.6
    assert lw.grade[mw] <= max(laws.DRIVE_GRADE, lw.soft_grade[mw]) + 1e-12
    assert lw.K[mw] * 100.0 >= v * v / (laws.DRIVE_LIFT_G * 9.81) - 1e-9 and lw.soft_K[mw] >= lw.K[mw]


def test_a_track_in_a_city_bends_more_and_any_track_is_dear_to_move():
    streets = [_m("s%d" % k, _line(-250, y, 250, y), cls="residential") for k, y in enumerate(range(-200, 201, 50))]
    net, sm, lw = _lay([_m("city", _line(-200, 25, 200, 25), "rail")] + streets
                       + [_m("open", _line(5000, 0, 5400, 0), "rail")])
    c, o = _p(net, "city"), _p(net, "open")
    assert lw.urban[c] and not lw.urban[o]
    assert lw.soft_K[c] < lw.soft_K[o] and lw.soft_grade[c] > lw.soft_grade[o] and lw.kmh[c] < lw.kmh[o]
    s = _p(net, "s0")
    assert np.all(lw.w[sm.of(c)] == laws.RAIL_GROUND_X * lw.w[sm.of(s)][0])


def test_a_deck_stands_over_its_ground_and_a_tunnel_under_it_away_from_their_ends():
    ms = [_m("a", _line(0, 0, 100, 0)), _m("d", _line(100, 0, 300, 0), "deck", level=1.0),
          _m("b", _line(300, 0, 400, 0)), _m("t", _line(400, 0, 700, 0), "tunnel", level=-1.0),
          _m("c", _line(700, 0, 800, 0))]
    net, sm, lw = _lay(ms)
    (p,) = range(len(net.paths))
    x = sm.xy[sm.of(p), 0]
    on_deck = (x > 100 + laws.ABUT_M + 4) & (x < 300 - laws.ABUT_M - 4)
    assert np.allclose(lw.lo[on_deck], 10 + gsh.deck_depth(200.0) + laws.DECK_AIR_M)
    assert np.all(lw.w[(x > 100.5) & (x < 299.5)] == 0.0)
    near_abut = (x > 101) & (x < 100 + laws.ABUT_M - 1)
    assert np.all(np.isneginf(lw.lo[near_abut]))
    in_tube = (x > 400 + laws.ABUT_M + 4) & (x < 700 - laws.ABUT_M - 4)
    assert np.allclose(lw.hi[in_tube], 10 - gsh.COVER_M - gsh.HEADROOM_M)
    on_ground = x < 99
    assert np.all(lw.w[on_ground] == laws.W_GROUND) and np.all(np.isinf(lw.lo[on_ground]) & np.isinf(lw.hi[on_ground]))
    assert lw.lam[p] == laws.LAM_DECK


def test_the_chain_dict_is_the_kernels():
    net, sm, lw = _lay([_m("a", _line(0, 0, 200, 0))])
    ch = lw.chain(sm, 0)
    assert set(ch) >= {"t", "w", "lo", "hi", "grade", "K", "lam", "step", "soft_grade", "w_grade", "soft_K", "w_curve"}
    assert len(ch["t"]) == len(sm) and abs(ch["step"] - sm.h[0]) < 1e-12


def test_no_ceiling_is_the_kernels_none(monkeypatch):
    import carriageways as cw
    monkeypatch.setattr(laws, "CEIL_GRADE_X", 0.0)
    monkeypatch.setattr(laws, "CEIL_CURVE_X", 0.0)
    _net, sm, lw = _lay([_m("a", _line(0, 0, 200, 0), cls="residential")])
    ch = lw.chain(sm, 0)
    assert ch["grade"] >= cw.NO_GRADE_CEILING and ch["K"] <= cw.NO_CURVE_CEILING
    assert ch["soft_grade"] < ch["grade"] and ch["soft_K"] > ch["K"] and ch["w_bound"] == laws.W_BOUND


def test_a_tunnel_is_drawn_to_the_line_between_its_portals_not_to_the_mountain():
    ms = [_m("w", _line(-600, 0, -300, 0)), _m("tun", _line(-300, 0, 300, 0, 61), "tunnel", level=-1.0),
          _m("e", _line(300, 0, 600, 0))]
    net = network.build(ms)
    sm = samples.sample(net, layers.order(net))
    xy = sm.xy
    g = np.where(np.abs(xy[:, 0]) < 280, 10.0 + 200.0 * np.cos(xy[:, 0] / 600.0), 10.0)    # (a mountain 200 m high)
    lw = laws.lay(net, sm, g)
    tun = lw.w == 0.0
    assert tun.any() and np.allclose(lw.t[tun], 10.0)
    assert (lw.hi[tun] > 100.0).any()                                   # (the cover still the mountain's)
