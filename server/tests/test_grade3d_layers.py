"""grade3d.layers: at each crossing which member is over (the kinds, then OSM's levels) and the clearance it needs; no
clearance at one level, between two tunnels at one level, at a merge read as a crossing, or where the map also joins
the two there."""
import numpy as np

import gradeshapes as gsh
import linenet
from gradeshapes import Member
from grade3d import layers, network


def _line(x0, y0, x1, y1, n=11):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def _m(key, line, kind="ground", level=0.0, cls="secondary"):
    return Member(key, np.asarray(line, np.float64), kind, "rail" if kind.startswith("rail") else cls, 50, level, 4.0,
                  None, None)


def setup_function(_f):
    linenet.reset()


def _one(ms):
    net = network.build(ms)
    (x,) = layers.order(net)
    return net, x


def test_a_deck_over_a_road_needs_headroom_and_its_depth():
    net, x = _one([_m("deck", _line(-60, 0, 60, 0), "deck", 1.0), _m("road", _line(0, -100, 0, 100))])
    u, su, d, sd = x.over()
    assert str(net.members[u].key) == "deck" and x.why == "kinds"
    want = gsh.headroom(net.members[d]) + gsh.deck_depth(120.0)
    assert abs(x.need - want) < 1e-9 and abs(su - 60.0) < 1e-6 and abs(sd - 100.0) < 1e-6


def test_stacked_decks_by_their_levels_and_a_tunnel_under_a_road():
    _net, x = _one([_m("low", _line(-100, 0, 100, 0), "deck", 1.0), _m("high", _line(0, -100, 0, 100), "deck", 2.0)])
    assert x.why == "levels" and str(_net.members[x.over()[0]].key) == "high"
    _net, x = _one([_m("street", _line(-100, 0, 100, 0)), _m("tube", _line(0, -100, 0, 100), "tunnel", -1.0)])
    assert x.why == "kinds" and str(_net.members[x.over()[0]].key) == "street"
    assert abs(x.need - (gsh.headroom(_net.members[x.over()[2]]) + gsh.COVER_M)) < 1e-9


def test_two_at_one_level_have_no_clearance():
    _net, x = _one([_m("a", _line(-100, 0, 100, 0)), _m("t", _line(0, -100, 0, 100), "rail")])
    assert x.upper is None and x.over() is None and x.why == "grade" and x.need == 0.0


def test_an_order_by_the_levels_alone_near_an_end_is_a_merge():
    # (a ramp's deck crossing the viaduct 5 m from its own end, one level apart: a merge read as a crossing)
    ms = [_m("viaduct", _line(-200, 0, 200, 0), "deck", 2.0),
          _m("ramp", np.array([[-3.0, 5.0], [0.0, -5.0], [0.0, -100.0]]), "deck", 1.0)]
    _net, x = _one(ms)
    assert x.upper is None and x.why == "merge"
    assert layers.summary([x]) == {"merge": 1}


def test_a_member_end_inside_a_path_is_no_end():
    # (the viaduct split into two members at the crossing: its path goes on, the order by the levels holds)
    ms = [_m("v1", _line(-200, 0, 10, 0), "deck", 2.0), _m("v2", _line(10, 0, 200, 0), "deck", 2.0),
          _m("over", _line(0, -100, 0, 100), "deck", 3.0)]
    net = network.build(ms)
    (x,) = layers.order(net)
    assert x.why == "levels" and x.upper is not None


def test_an_order_by_the_kinds_where_the_map_also_joins_them_there_is_no_clearance():
    # (a street crossing under a deck 16 m from its end, where it meets the deck's own end and ramp: both meet and pass)
    ms = [_m("deck", _line(-200, 0, 0, 0), "deck", 1.0), _m("ramp", _line(0, 0, 100, 0)),
          _m("street", np.array([[-10.0, -100.0], [-10.0, 5.0], [0.0, 0.0]]))]
    net = network.build(ms)
    lay = layers.order(net)
    assert any(y.why == "joined" and y.upper is None for y in lay)
    assert not any(y.upper is not None for y in lay)
    # (the same street going on 200 m past the deck, not joined to it: the deck over it)
    linenet.reset()
    ms[2] = _m("street", _line(-50, -100, -50, 200))
    net = network.build(ms)
    (x,) = layers.order(net)
    assert x.why == "kinds" and x.upper is not None


def test_two_tunnels_at_one_level_are_left_apart():
    ms = [_m("road", _line(-200, 0, 200, 0), "tunnel", level=-1.0), _m("track", _line(0, -200, 0, 200), "tunnel", level=-1.0)]
    net = network.build(ms)
    (x,) = layers.order(net)
    assert x.upper is None and x.why == "apart"
