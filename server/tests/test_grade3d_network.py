"""grade3d.network: members chained into whole paths where exactly two ends of one family meet and nothing else touches;
the joins and crossings between paths, each once."""
import numpy as np

import gradeshapes as _gsh
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


def test_a_fork_a_t_and_a_dead_end_end_paths_the_road_going_straight_on_goes_through():
    ms = [_m("a", _line(0, 0, 100, 0, 11)), _m("b", _line(100, 0, 200, 0)), _m("c", _line(100, 0, 200, 80)),   # (fork)
          _m("d", _line(50, 0, 50, -80))]                                    # (T on a, at a node of a's: its point at 50)
    net = network.build(ms)
    assert sorted(_paths(net)) == [["a", "b"], ["c"], ["d"]]       # (b goes straight on from a; c leaves at 39 degrees)
    pairs = sorted(tuple(sorted((str(ms[i].key), str(ms[j].key)))) for i, _si, j, _sj in net.joins)
    assert pairs == [("a", "c"), ("a", "d"), ("b", "c")]
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


def _rail(way, line, kind="rail"):
    line = np.asarray(line, np.float64)
    return Member(("rail", kind, way) + tuple(np.round(line[[0, -1]].ravel())), line, kind, "rail", None,
                  1.0 if kind == "rail_deck" else 0.0, 2.0, None, None)


def test_a_ways_overlapping_clips_are_one_line():
    # (a track clipped by two cells with a margin: the copies overlap 100 m; a third copy wholly inside the first)
    ms = [_rail("w1", _line(0, 0, 600, 0)), _rail("w1", _line(500, 0, 1200, 0)), _rail("w1", _line(100, 0, 300, 0))]
    out = network.one_way(ms)
    (m,) = out
    assert abs(m.line[0, 0]) < 1e-9 and abs(m.line[-1, 0] - 1200) < 1e-9
    assert np.all(np.diff(m.line[:, 0]) > 0)
    net = network.build(ms)
    assert len(net.paths) == 1 and net.crossings == [] and abs(net.paths[0].length - 1200) < 1e-6


def test_a_copy_drawn_the_other_way_extends_the_start():
    ms = [_rail("w1", _line(0, 0, 600, 0)), _rail("w1", _line(100, 0, -400, 0))]
    (m,) = network.one_way(ms)
    xs = m.line[:, 0]
    assert abs(xs.min() + 400) < 1e-9 and abs(xs.max() - 600) < 1e-9 and np.all(np.diff(xs) > 0)


def test_a_copy_that_leaves_the_line_and_comes_back_is_a_piece_of_its_own():
    # (a way's second copy along the first for 30 m, 10 m off it for 30 m, back on it: its detour touches the kept line
    # at both ends, mid-line. It went back on the work list against that same line for ever: Popoli, 2026-11-27)
    x = np.linspace(10.0, 80.0, 36)     # (shorter than the first, 90 m with its detour: the first is the one kept)
    detour = np.c_[x, np.where((x > 30) & (x < 60), 10.0, 0.0)]
    out = network.one_way([_rail("w1", _line(0, 0, 100, 0)), _rail("w1", detour)])
    assert len(out) == 2
    off = [m for m in out if np.abs(m.line[:, 1]).max() > 5.0]
    (d,) = off
    tol = network.OVERLAP_M     # (its ends on the kept line: within its overlap of it)
    assert abs(d.line[0, 1]) <= tol and abs(d.line[-1, 1]) <= tol and 25 < d.line[0, 0] < d.line[-1, 0] < 65


def test_a_track_under_its_own_deck_is_cut_there_and_chains_through_it():
    ms = [_rail("w1", _line(0, 0, 1000, 0)), _rail("w1", _line(400, 0, 600, 0), "rail_deck"),
          _m("road", _line(500, -100, 500, 100))]
    net = network.build(ms)
    kinds = sorted(m.kind for m in net.members)
    assert kinds == ["ground", "rail", "rail", "rail_deck"]
    (x,) = net.crossings                              # (the road under the deck, not the track under its own deck)
    assert {net.members[x[0]].kind, net.members[x[2]].kind} == {"rail_deck", "ground"}
    rail_paths = [p for p in net.paths if any(net.members[i].kind.startswith("rail") for i in p.members())]
    assert len(rail_paths) == 1 and abs(rail_paths[0].length - 1000) < 2 * network.OVERLAP_M


def test_other_ways_are_untouched():
    ms = [_rail("w1", _line(0, 0, 600, 0)), _rail("w2", _line(0, 4, 600, 4)), _m("a", _line(0, 0, 100, 0))]
    out = network.one_way(ms)
    assert len(out) == 3 and all(o is m for o, m in zip(sorted(out, key=str), sorted(ms, key=str)))


def test_an_approach_meeting_its_decks_end_keeps_its_whole_line():
    ms = [_m(("r", "ground", "0"), _line(-300, 0, -100, 0)), _m(("r", "deck", 1), _line(-100, 0, 100, 0), "deck", level=1),
          _m(("r", "ground", "1"), _line(100, 0, 300, 0))]
    out = network.one_way(ms)
    ground = sorted((m for m in out if m.kind == "ground"), key=lambda m: m.line[0, 0])
    assert len(ground) == 2
    assert abs(ground[0].line[-1, 0] + 100) < 1e-9 and abs(ground[1].line[0, 0] - 100) < 1e-9
    net = network.build(ms)
    assert len(net.paths) == 1 and abs(net.paths[0].length - 600) < 1e-6


def test_a_road_goes_on_through_a_fork_its_ramp_leaves_it():
    ms = [_m("in", _line(-300, 0, 0, 0)), _m("on", _line(0, 0, 300, 0)), _m("ramp", _line(0, 0, 200, -120))]
    net = network.build(ms)
    keys = [[str(net.members[i].key) for i, _f in path.parts] for path in net.paths]
    assert sorted(len(k) for k in keys) == [1, 2] and any(set(k) == {"in", "on"} for k in keys)
    assert len(net.joins) >= 1                      # (the ramp joined to the road going on)


def test_a_decks_end_over_a_motorway_on_the_ground_joins_it_only_at_a_node_of_both():
    def mm(key, line, kind, level, at, cls="secondary"):
        return Member(key, np.asarray(line, np.float64), kind, cls, None, level, 4.0, None,
                      frozenset(c for c, _x, _y in at), node_xy=tuple(at))
    road = mm("road", _line(-200, 0, 200, 0), "ground", 0.0, [("a", -200.0, 0.0), ("b", 200.0, 0.0), ("x", 0.0, 0.0)],
              "motorway")
    deck = mm("deck", _line(0, 0, 0, 150), "deck", 1.0, [("y", 0.0, 0.0), ("z", 0.0, 150.0)])    # (no node of the road)
    net = network.build([road, deck])
    assert net.joins == [] and len(net.paths) == 2
    linenet.reset()
    deck = mm("deck", _line(0, 0, 0, 150), "deck", 1.0, [("x", 0.0, 0.0), ("z", 0.0, 150.0)])    # (the map joins them at x)
    net = network.build([road, deck])
    assert len(net.joins) == 1
    linenet.reset()
    # (a node of both, but at the road's end, 200 m off: no join where the deck ends)
    deck = mm("deck", _line(0, 0, 0, 150), "deck", 1.0, [("b", 200.0, 0.0), ("z", 0.0, 150.0)])
    net = network.build([road, deck])
    assert net.joins == []


def test_a_rail_decks_end_by_a_street_joins_it_only_at_a_node_of_both():
    # (Yokohama: a rail deck ending 2 m from the street it bridges, a point of each 4.6 cm apart there. Read as one map
    # node, the deck was joined to the street and held 5.83 m over it at once: its block had no answer)
    street = Member("street", np.array([[-50.0, 2.0], [0.03, 2.0], [50.0, 2.0]]), "ground", "tertiary", None, 0.0, 4.0,
                    None, frozenset({"a", "b"}), node_xy=(("a", -50.0, 2.0), ("b", 50.0, 2.0)))
    line = np.c_[np.zeros(51), np.arange(51) * 2.0]
    deck = Member(("rail", "rail_deck", "t1", 0, 0, 0, 100), line, "rail_deck", "rail", None, 1.0, 2.0, None,
                  frozenset({"s", "e"}), node_xy=(("s", 0.0, 0.0), ("e", 0.0, 100.0)))
    net = network.build([street, deck])
    assert net.joins == []


def test_a_road_shorter_than_the_join_reach_joins_each_neighbour_at_its_own_end():
    # (Suedtiroler Platz: a 2 m parking aisle from a building passage's end to the car park's ring, both its ends within
    # reach of both: each end was joined to both, the passage's end held at the ring's height)
    def mm(key, line, kind, level, at):
        return Member(key, np.asarray(line, np.float64), kind, "service", None, level, 2.5, None,
                      frozenset(c for c, _x, _y in at), node_xy=tuple(at))
    passage = mm("passage", _line(0, 0, 0, -15), "tunnel", -1.0, [("a", 0.0, 0.0), ("z", 0.0, -15.0)])
    aisle = mm("aisle", [[0.0, 0.0], [0.0, 2.0]], "ground", 0.0, [("a", 0.0, 0.0), ("b", 0.0, 2.0)])
    ring = mm("ring", _line(-20, 2, 20, 2), "ground", 0.0, [("r0", -20.0, 2.0), ("b", 0.0, 2.0), ("r1", 20.0, 2.0)])
    net = network.build([passage, aisle, ring])
    got = {frozenset([(str(net.members[i].key), round(si, 1)), (str(net.members[j].key), round(sj, 1))])
           for i, si, j, sj in net.joins}
    assert got == {frozenset([("aisle", 0.0), ("passage", 0.0)]), frozenset([("aisle", 2.0), ("ring", 20.0)])}
    # (and the passage's portal, opening onto the aisle 2 m from the ring, is not under the ring)
    assert [_gsh._over(net.members[i], net.members[j]) for i, _si, j, _sj, _q in net.crossings
            if _gsh._over(net.members[i], net.members[j]) is not None] == []


def test_a_deck_landing_on_a_junction_is_not_over_the_road_leaving_the_next_one():
    # (Kawazu: a deck landing on a junction, a 2.1 m stub from it to the next, where another road starts 2 m from the
    # deck's end: that road was held 5.05 m under the deck's end, joined to it through the stub)
    def mm(key, line, kind, level, at):
        return Member(key, np.asarray(line, np.float64), kind, "residential", None, level, 3.0, None,
                      frozenset(c for c, _x, _y in at), node_xy=tuple(at))
    deck = mm("deck", _line(-14, -14, 0, 0), "deck", 1.0, [("d0", -14.0, -14.0), ("j1", 0.0, 0.0)])
    street = mm("street", _line(-200, 50, 0, 0), "ground", 0.0, [("s0", -200.0, 50.0), ("j1", 0.0, 0.0)])
    stub = mm("stub", [[0.0, 0.0], [1.5, 1.4]], "ground", 0.0, [("j1", 0.0, 0.0), ("j2", 1.5, 1.4)])
    road = mm("road", _line(1.5, 1.4, 20, 100), "ground", 0.0, [("j2", 1.5, 1.4), ("r1", 20.0, 100.0)])
    net = network.build([deck, street, stub, road])
    assert [c for c in net.crossings if _gsh._over(net.members[c[0]], net.members[c[2]]) is not None] == []


def test_a_deck_landing_on_a_roads_middle_is_not_over_a_road_leaving_it_beside():
    # (Dallas: a service deck landing on a service road's middle, another road leaving that road 1 m on: held 6.35 m
    # under the deck's end, both joined to the road it lands on)
    def mm(key, line, kind, level, at):
        return Member(key, np.asarray(line, np.float64), kind, "service", None, level, 3.0, None,
                      frozenset(c for c, _x, _y in at), node_xy=tuple(at))
    deck = mm("deck", _line(-60, 0, 0, 0), "deck", 1.0, [("d0", -60.0, 0.0), ("j1", 0.0, 0.0)])
    road = mm("road", _line(0, -100, 0, 100, 201), "ground", 0.0,
              [("r0", 0.0, -100.0), ("j1", 0.0, 0.0), ("j2", 0.0, 1.0), ("r1", 0.0, 100.0)])
    other = mm("other", _line(0, 1, 38, 3.5), "ground", 0.0, [("j2", 0.0, 1.0), ("o1", 38.0, 3.5)])
    net = network.build([deck, road, other])
    assert [c for c in net.crossings if _gsh._over(net.members[c[0]], net.members[c[2]]) is not None] == []


def _part(way, kind, line, level, nodes):
    return Member((way, kind, 0, 0), np.asarray(line, np.float64), kind, "primary", None, level, 4.0, None,
                  frozenset(c for c, _x, _y in nodes), node_xy=tuple(nodes))


def test_two_portals_side_by_side_are_no_separation():
    # (Porte Maillot: the primary leaves its tunnel straight onto a level-1 part, its portal 2.2 m from a service road's
    # own portal beside it, the two lines never crossing. Each end by the other road was a separation: the primary's
    # portal held 6.35 m over the service road and 5.15 m under it at once, its block with no answer)
    ms = [_part("p", "tunnel", _line(0, -100, 0, 0), -1.0, [("p0", 0, -100), ("p1", 0, 0)]),
          _part("p", "deck", _line(0, 0, 0, 100), 1.0, [("p1", 0, 0), ("p2", 0, 100)]),
          _part("s", "tunnel", _line(40, -100, 2.2, 0), -1.0, [("s0", 40, -100), ("s1", 2.2, 0)]),
          _part("s", "ground", _line(2.2, 0, 40, 100), 0.0, [("s1", 2.2, 0), ("s2", 40, 100)])]
    net = network.build(ms)
    # (the two tunnels' portals by each other, both level -1: a contact with no order, no clearance)
    assert [_gsh._over(net.members[i], net.members[j]) for i, _si, j, _sj, _q in net.crossings] == [None]
    assert net.joins == []
    assert sorted(sorted(p) for p in _paths(net)) == [sorted([str(ms[0].key), str(ms[1].key)]),
                                                     sorted([str(ms[2].key), str(ms[3].key)])]


def test_a_portal_by_a_road_it_crosses_keeps_the_separation():
    # (the service road crossing the primary's line a metre past the primary's portal: the map's own contradiction, kept)
    ms = [_part("p", "tunnel", _line(0, -100, 0, 0), -1.0, [("p0", 0, -100), ("p1", 0, 0)]),
          _part("p", "deck", _line(0, 0, 0, 100, 101), 1.0, [("p1", 0, 0), ("p2", 0, 100)]),
          _part("s", "ground", _line(1.5, 1, 100, 1), 0.0, [("s1", 1.5, 1), ("s2", 100, 1)]),
          _part("s", "ground", _line(-100, 1, 1.5, 1), 0.0, [("s0", -100, 1), ("s1", 1.5, 1)])]
    net = network.build(ms)
    assert net.crossings
