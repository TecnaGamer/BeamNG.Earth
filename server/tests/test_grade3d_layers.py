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


def test_a_path_coming_back_to_its_own_node_is_joined_there():
    # (Perth's Tonkin Highway: one path out over its deck carriageway, round a loop and back on its ground carriageway to
    # the node it left from, the two crossing 7 m before it: joined there, no clearance)
    deck = _m("deck", np.array([[-400.0, -40.0], [-10.0, -1.0], [-4.0, 1.0], [0.0, 0.0]]), "deck", 1.0, cls="motorway")
    out = _m("out", np.array([[0.0, 0.0], [300.0, 30.0]]), cls="motorway")       # (straight on from the deck)
    loop = _m("loop", np.array([[300.0, 30.0], [300.0, -400.0], [-500.0, -400.0], [-500.0, 0.0]]), cls="motorway")
    ground = _m("ground", _line(-500, 0, 0, 0, 51), cls="motorway")
    net = network.build([deck, out, loop, ground])
    assert len(net.paths) == 1
    (x,) = [y for y in layers.order(net) if {net.members[y.a].key, net.members[y.b].key} == {"deck", "ground"}]
    assert x.upper is None and x.why == "joined"


def _noded(key, line, kind, level, nodes, cls="service"):
    return Member(key, np.asarray(line, np.float64), kind, cls, None, level, 3.0, None,
                  frozenset(c for c, _x, _y in nodes), node_xy=tuple(nodes))


def test_two_roads_joined_through_a_third_too_near_their_crossing_are_joined_there():
    # (La Defense: a service tunnel starts at a node of a street, and 9 m on passes 5.25 m under the street's junction
    # piece, the two joined only through that street: held there and 5.25 m apart at once, its block had no answer)
    street = _noded("street", _line(-200, 0, 200, 0, 41), "ground", 0.0,
                    [("w", -200.0, 0.0), ("a", 0.0, 0.0), ("b", 10.0, 0.0), ("e", 200.0, 0.0)])
    piece = _noded("piece", _line(10, 0, 10, 60, 7), "ground", 0.0, [("b", 10.0, 0.0), ("pn", 10.0, 60.0)])
    tube = _noded("tube", np.array([[0.0, 0.0], [8.0, 4.0], [12.0, 6.0], [100.0, 6.0]]), "tunnel", -1.0,
                  [("a", 0.0, 0.0), ("tn", 100.0, 6.0)])
    net = network.build([street, piece, tube])
    xs = [y for y in layers.order(net) if {str(net.members[y.a].key), str(net.members[y.b].key)} == {"piece", "tube"}]
    assert xs and all(y.upper is None and y.why == "joined" for y in xs)
    # (the tube starting 400 m along the street instead: its grade parts them, the clearance kept)
    linenet.reset()
    street = _noded("street", _line(-500, 0, 200, 0, 71), "ground", 0.0,
                    [("w", -500.0, 0.0), ("a", -400.0, 0.0), ("b", 10.0, 0.0), ("e", 200.0, 0.0)])
    tube = _noded("tube", np.array([[-400.0, 0.0], [-400.0, -40.0], [8.0, -40.0], [8.0, 4.0], [12.0, 6.0],
                                    [100.0, 6.0]]), "tunnel", -1.0, [("a", -400.0, 0.0), ("tn", 100.0, 6.0)])
    net = network.build([street, piece, tube])
    xs = [y for y in layers.order(net) if {str(net.members[y.a].key), str(net.members[y.b].key)} == {"piece", "tube"}]
    assert xs and all(y.upper is not None for y in xs)


def test_a_crossing_near_an_end_the_map_joins_to_nothing_driven_is_open():
    # (La Defense's car park spiral: its ramp rises from a node only a footway touches, its car park not mapped, and 2 m
    # on the map has it over a delivery tunnel (here 12 m on): the map says nothing of its height there)
    ramp = _noded("ramp", np.array([[-2.0, 0.0], [0.0, 0.0], [200.0, 0.0]]), "ground", 0.0,
                  [("r0", -2.0, 0.0), ("r1", 200.0, 0.0)])
    walk = _noded("walk", _line(-2, 0, -2, -80, 9), "ground", 0.0, [("r0", -2.0, 0.0), ("w1", -2.0, -80.0)],
                  cls="footway")
    tube = _noded("tube", _line(10, -100, 10, 100, 21), "tunnel", -1.0, [("t0", 10.0, -100.0), ("t1", 10.0, 100.0)])
    street = _noded("street", _line(200, -100, 200, 100, 21), "ground", 0.0, [("r1", 200.0, 0.0), ("s0", 200.0, -100.0),
                                                                                ("s1", 200.0, 100.0)], cls="secondary")
    net = network.build([ramp, walk, tube, street])
    (x,) = [y for y in layers.order(net) if {str(net.members[y.a].key), str(net.members[y.b].key)} == {"ramp", "tube"}]
    assert x.upper is None and x.why == "open"
    # (its end joined to a street: the map's road goes on there, the tunnel under it by its clearance)
    linenet.reset()
    lane = _noded("lane", _line(-2, -100, -2, 100, 21), "ground", 0.0, [("r0", -2.0, 0.0), ("l0", -2.0, -100.0),
                                                                        ("l1", -2.0, 100.0)], cls="residential")
    net = network.build([ramp, lane, tube, street])
    (x,) = [y for y in layers.order(net) if {str(net.members[y.a].key), str(net.members[y.b].key)} == {"ramp", "tube"}]
    assert x.why == "kinds" and str(net.members[x.over()[0]].key) == "ramp"
    # (its end outside the area read: it may go on to what was not read, the clearance kept)
    linenet.reset()
    net = network.build([ramp, walk, tube, street], box=(np.array([0.0, -150.0]), np.array([300.0, 150.0])))
    (x,) = [y for y in layers.order(net) if {str(net.members[y.a].key), str(net.members[y.b].key)} == {"ramp", "tube"}]
    assert x.why == "kinds"


def test_two_tunnels_at_one_level_are_left_apart():
    ms = [_m("road", _line(-200, 0, 200, 0), "tunnel", level=-1.0), _m("track", _line(0, -200, 0, 200), "tunnel", level=-1.0)]
    net = network.build(ms)
    (x,) = layers.order(net)
    assert x.upper is None and x.why == "apart"
