"""gradeshapes: each interchange solved as one shape, placed on the ground with its embankments."""
import numpy as np

import gradeshapes as ix
from gradeshapes import Member


def _line(x0, y0, x1, y1, n=40):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def _run(members, ground=lambda xy: np.zeros(len(xy))):
    net, systems = ix.find_systems(members)
    shapes = []
    for sysm in systems:
        pcs = ix.pieces(net, sysm)
        smp = ix.sample(net, pcs)
        zs = ix.solve_shape(net, sysm, pcs, smp, [ground(x["xy"]) for x in smp], lambda i, s: _fixed(net, ground, i, s))
        shapes.append((pcs, smp, zs))
    emb = ix.embankments(net, systems, shapes,
                         lambda i, sv: ground(np.stack([np.interp(sv, net.s[i], net.lines[i][:, 0]),
                                                        np.interp(sv, net.s[i], net.lines[i][:, 1])], axis=1)))
    return net, systems, shapes, emb


def _fixed(net, ground, i, s):
    """The ground of member i at arc length s (a float), or at an array of them (an array)."""
    sv = np.atleast_1d(np.asarray(s, np.float64))
    z = ground(np.stack([np.interp(sv, net.s[i], net.lines[i][:, 0]), np.interp(sv, net.s[i], net.lines[i][:, 1])],
                        axis=1))
    return np.asarray(z, np.float64) if np.ndim(s) else float(z[0])


def _shape_z(net, shapes, key, q):
    best = None
    for pcs, smp, zs in shapes:
        for x, z in zip(smp, zs):
            for k in range(len(z)):
                if net.m[int(x["mem"][k])].key != key:
                    continue
                d = float(np.hypot(*(x["xy"][k] - q)))
                if best is None or d < best[0]:
                    best = (d, z[k])
    return best[1]


def _key_index(net, key):
    return [n for n, m in enumerate(net.m) if m.key == key][0]


def test_an_overpass_rises_over_a_motorway_on_embankments_at_the_approaches_max_grade():
    m = [Member("mway", _line(-800, 0, 800, 0, 200), "ground", "motorway", 120, 0, 12.0, None),
         Member("deck", _line(0, -30, 0, 30, 13), "deck", "secondary", 60, 1, 5.0, None),
         Member("south", _line(0, -600, 0, -30), "ground", "secondary", 60, 0, 5.0, None),
         Member("north", _line(0, 30, 0, 600), "ground", "secondary", 60, 0, 5.0, None)]
    net, systems, shapes, emb = _run(m)
    assert len(systems) == 1
    assert all(c["moves"] == "up" for c in systems[0]["crossings"])
    z = _shape_z(net, shapes, "deck", np.array([0.0, 0.0]))
    assert z >= ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + ix.deck_depth(60.0) - 0.05     # the motorway's headroom
    assert _key_index(net, "mway") not in emb                          # the motorway keeps its ground
    # the deck slopes down toward its ends (less embankment) as far as its crest curve allows at its speed
    z_end = _shape_z(net, shapes, "deck", np.array([0.0, 30.0]))
    R = ix.curve_radius(m[1])
    assert z_end < z - 0.8 * 30.0 ** 2 / (2 * R)
    # the approach: an embankment turning onto its max grade and onto the ground through its curves
    g = ix.max_grade(m[3])
    for sv, zz, mode in emb[_key_index(net, "north")]:
        assert mode == "fill"
        assert abs(zz[0] - z_end) < 0.05
        gr = np.diff(zz) / np.diff(sv)
        assert (np.abs(gr) <= g + 1e-6).all()
        assert (np.abs(np.diff(gr)) <= np.diff(sv)[1:] / ix.curve_radius(m[3]) + 1e-6).all()
        assert abs(zz[-1]) < 0.05 and abs(gr[-1]) < 0.01                # onto the ground: the touch point a result


def test_a_short_bridge_over_a_street_is_the_street_dug_under_it():
    m = [Member("street", _line(-400, 0, 400, 0, 100), "ground", "residential", 40, 0, 4.0, None),
         Member("deck", _line(0, -6, 0, 6, 4), "deck", "tertiary", 50, 1, 4.0, None),
         Member("s", _line(0, -300, 0, -6), "ground", "tertiary", 50, 0, 4.0, None),
         Member("n", _line(0, 6, 0, 300), "ground", "tertiary", 50, 0, 4.0, None)]
    net, systems, shapes, emb = _run(m)
    assert [c["moves"] for c in systems[0]["crossings"]] == ["down"]
    zd = _shape_z(net, shapes, "deck", np.array([0.0, 0.0]))
    zs = _shape_z(net, shapes, "street", np.array([0.0, 0.0]))
    assert zd - zs >= ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + ix.deck_depth(12.0) - 0.05
    assert abs(zd) < 0.5                                               # the deck stays about at its ground
    st = emb[_key_index(net, "street")]
    assert any(mode == "cut" for _s, _z, mode in st)
    g = ix.max_grade(m[0])
    for sv, zz, mode in st:
        assert (np.abs(np.diff(zz) / np.diff(sv)) <= g + 1e-3).all()     # (its grade law, to the penalty's mm)
        assert zz.max() <= 0.05                                         # a cut: never over its ground


def test_a_road_in_a_short_tunnel_under_a_track_goes_down_and_the_track_stays():
    m = [Member("rail", _line(-600, 0, 600, 0, 150), "rail", "rail", None, 0, 2.0, None),
         Member("west", _line(0, -400, 0, -15), "ground", "secondary", 50, 0, 4.0, None),
         Member("under", _line(0, -15, 0, 15, 8), "tunnel", "secondary", 50, -1, 4.0, None),
         Member("east", _line(0, 15, 0, 400), "ground", "secondary", 50, 0, 4.0, None)]
    net, systems, shapes, emb = _run(m)
    assert [c["moves"] for c in systems[0]["crossings"]] == ["down"]
    zu = _shape_z(net, shapes, "under", np.array([0.0, 0.0]))
    assert zu <= -(ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + ix.COVER_M) + 0.05
    for k in ("west", "east"):
        pcs = emb[_key_index(net, k)]
        assert pcs and all(mode == "cut" for _s, _z, mode in pcs)       # cut down to the tunnel's mouth
    assert _key_index(net, "rail") not in emb


def test_two_overpasses_close_on_one_road_have_their_embankments_lerped():
    m = [Member("m1", _line(-800, 0, 800, 0, 200), "ground", "motorway", 120, 0, 12.0, None),
         Member("m2", _line(-800, 120, 800, 120, 200), "ground", "motorway", 120, 0, 12.0, None),
         Member("d1", _line(0, -30, 0, 30, 13), "deck", "secondary", 60, 1, 5.0, None),
         Member("d2", _line(0, 90, 0, 150, 13), "deck", "secondary", 60, 1, 5.0, None),
         Member("mid", _line(0, 30, 0, 90, 16), "ground", "secondary", 60, 0, 5.0, None),
         Member("s", _line(0, -600, 0, -30), "ground", "secondary", 60, 0, 5.0, None),
         Member("n", _line(0, 150, 0, 700), "ground", "secondary", 60, 0, 5.0, None)]
    net, systems, shapes, emb = _run(m)
    mid = emb[_key_index(net, "mid")]
    assert [mode for _s, _z, mode in mid] == ["set"]
    sv, zz, _m = mid[0]
    assert (zz > 1.0).all()                                            # it never comes down to the ground between them
    assert np.allclose(np.diff(zz) / np.diff(sv), (zz[-1] - zz[0]) / (sv[-1] - sv[0]))


def test_an_overpass_tilts_on_sloping_ground_so_each_end_needs_the_least_embankment():
    # the ground rising 5 % to the north across a motorway: neither end stands higher over its ground than the
    # clearance over the motorway's whole carriageway and the deck's own grade ask (tilted 5 % at the crossing, it stood
    # 0.33 m into the headroom over the motorway's south lanes: the clearance holds wherever the deck is over them)
    slope = lambda xy: 0.05 * xy[:, 1]
    m = [Member("mway", _line(-800, 0, 800, 0, 200), "ground", "motorway", 120, 0, 12.0, None),
         Member("deck", _line(0, -40, 0, 40, 17), "deck", "secondary", 60, 1, 5.0, None),
         Member("south", _line(0, -600, 0, -40), "ground", "secondary", 60, 0, 5.0, None),
         Member("north", _line(0, 40, 0, 600), "ground", "secondary", 60, 0, 5.0, None)]
    net, systems, shapes, emb = _run(m, slope)
    zc = _shape_z(net, shapes, "deck", np.array([0.0, 0.0]))
    assert zc >= ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + ix.deck_depth(80.0) - 0.05
    zs = _shape_z(net, shapes, "deck", np.array([0.0, -40.0]))
    zn = _shape_z(net, shapes, "deck", np.array([0.0, 40.0]))
    need = ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + ix.deck_depth(80.0)
    for y in (-12.0, 12.0):                                            # over the motorway's edges
        assert _shape_z(net, shapes, "deck", np.array([0.0, y])) >= need - 0.05
    assert zn >= zs - 1e-6                                             # never tilted against the ground
    # a level deck at the crossing's height would stand zc - ground at its ends; the tilted one needs less embankment
    # in all (the crest curve its speed asks limits how far both ends can fall away: the two are balanced)
    assert (zs + 2.0) ** 2 + (zn - 2.0) ** 2 < 0.8 * ((zc + 2.0) ** 2 + (zc - 2.0) ** 2)
    lens = {k: sum(abs(sv[-1] - sv[0]) for sv, _z, mo in emb[_key_index(net, k)] if mo == "fill")
            for k in ("south", "north")}
    assert lens["north"] < lens["south"]                               # uphill the ground comes up to meet it


def test_a_deck_over_a_railway_rises_by_the_catenary_and_its_embankments_hold_their_max_grade():
    m = [Member("rail", _line(-800, 0, 800, 0, 200), "rail", "rail", None, 0, 2.0, None),
         Member("deck", _line(0, -20, 0, 20, 11), "deck", "tertiary", 50, 1, 4.0, None),
         Member("south", _line(0, -600, 0, -20), "ground", "tertiary", 50, 0, 4.0, None),
         Member("north", _line(0, 20, 0, 600), "ground", "tertiary", 50, 0, 4.0, None)]
    net, systems, shapes, emb = _run(m)
    assert [c["moves"] for c in systems[0]["crossings"]] == ["up"]     # a track is never dug
    assert _shape_z(net, shapes, "deck", np.array([0.0, 0.0])) >= ix.HEADROOM_M + ix.deck_depth(40.0) - 0.05
    g = ix.max_grade(m[3])
    for sv, zz, mode in emb[_key_index(net, "north")]:
        gr = np.abs(np.diff(zz) / np.diff(sv))
        assert np.isclose(gr.max(), g, atol=1e-3)                      # a long embankment at the road's max grade
        assert abs(zz[-1]) < 0.05


def test_a_dip_follows_the_road_through_its_joints_without_a_step():
    # a ring road split in short parts (a roundabout's), a short rail bridge over the 20 m one: the cut spreads into
    # the parts either side through the joints, each joint at one height, within the road's grade
    m = [Member("rail_w", _line(-7, -300, -7, -7, 80), "rail", "rail", None, 0, 2.0, None),
         Member("rail_br", _line(-7, -7, -7, 7, 5), "rail_deck", "rail", None, 1, 2.0, None),
         Member("rail_e", _line(-7, 7, -7, 300, 80), "rail", "rail", None, 0, 2.0, None),
         Member(("ring", "a"), _line(-400, 0, -10, 0, 60), "ground", "secondary", 50, 0, 4.0, None),
         Member(("ring", "b"), _line(-10, 0, 10, 0, 6), "ground", "secondary", 50, 0, 4.0, None),
         Member(("ring", "c"), _line(10, 0, 400, 0, 60), "ground", "secondary", 50, 0, 4.0, None)]
    net, systems, shapes, emb = _run(m)
    assert [c["moves"] for c in systems[0]["crossings"]] == ["down"]
    zb = _shape_z(net, shapes, ("ring", "b"), np.array([-7.0, 0.0]))
    assert zb <= -(ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + ix.deck_depth(14.0)) + 0.05
    g = ix.max_grade(m[4])
    z_at = {}
    for k in (("ring", "a"), ("ring", "b"), ("ring", "c")):
        pcs = emb[_key_index(net, k)]
        assert pcs and all(mode == "cut" for _s, _z, mode in pcs)
        for sv, zz, mode in pcs:
            assert (np.abs(np.diff(zz) / np.diff(sv)) <= g + 1e-3).all()
        z_at[k] = pcs
    a_end = [zz[-1] for sv, zz, _m in z_at[("ring", "a")] if sv[-1] > 389.0]
    b_start = [zz[0] for sv, zz, _m in z_at[("ring", "b")] if sv[0] < 1.0]
    assert a_end and b_start and abs(a_end[0] - b_start[0]) < 0.05       # one height at the joint


def _joined(net, sysm, a, b):
    return any({net.m[i].key, net.m[j].key} == {a, b} for i, _si, j, _sj in sysm["joins"])


def _ray(p, deg, s0, s1, n=40):
    u = np.array([np.cos(np.radians(deg)), np.sin(np.radians(deg))])
    return np.asarray(p, np.float64) + np.linspace(s0, s1, n)[:, None] * u


def test_a_flyovers_joint_over_a_link_below_is_a_crossing_not_a_join():
    # the High Five: two parts of a level-4 flyover meet 2.2 m off a level-1 link's deck, which they cross 2.2 m on.
    # The joint touching the link in plan was taken for a joint with it (held at the link's height there and 8.35 m
    # over it 2 m on: the whole interchange infeasible)
    m = [Member("fly_s", _line(0, -300, 0, 0, 76), "deck", "motorway_link", 60, 4, 5.0, None),
         Member("fly_n", _line(0, 0, 0, 300, 76), "deck", "motorway_link", 60, 4, 5.0, None),
         Member("link", _line(-80, 2.2, 80, 2.2, 41), "deck", "secondary_link", 50, 1, 4.0, None),
         Member("fs", _line(0, -700, 0, -300), "ground", "motorway_link", 60, 0, 5.0, None),
         Member("fn", _line(0, 300, 0, 700), "ground", "motorway_link", 60, 0, 5.0, None),
         Member("lw", _line(-500, 2.2, -80, 2.2), "ground", "secondary_link", 50, 0, 4.0, None),
         Member("le", _line(80, 2.2, 500, 2.2), "ground", "secondary_link", 50, 0, 4.0, None)]
    net, systems, shapes, emb = _run(m)
    assert len(systems) == 1
    assert _joined(net, systems[0], "fly_s", "fly_n")
    assert not _joined(net, systems[0], "fly_s", "link") and not _joined(net, systems[0], "fly_n", "link")
    assert shapes[0][2] is not None
    need = ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + ix.deck_depth(300.0)
    for q in ((0.0, 0.0), (0.0, 2.2)):         # (the joint over the link's edge too: a crossing at its end)
        zf = _shape_z(net, shapes, "fly_s" if q[1] == 0.0 else "fly_n", np.array(q))
        assert zf - _shape_z(net, shapes, "link", np.array([q[0], 2.2])) >= need - 0.3


def test_a_deck_ending_along_one_it_passes_under_is_not_joined_to_it():
    # Gravelly Hill: a level-1 M6 deck passes under the level-3 one at 29 deg and ends 5 m on, 2.4 m off its line: a
    # merge's angle, but a road cannot meet the one it passes under 5 m before
    m = [Member("upper", _line(-300, 0, 300, 0, 76), "deck", "motorway", 110, 3, 12.0, None),
         Member("lower", _ray((0, 0), 29, -150, 5, 40), "deck", "motorway", 110, 1, 12.0, None),
         Member("uw", _line(-800, 0, -300, 0), "ground", "motorway", 110, 0, 12.0, None),
         Member("ue", _line(300, 0, 800, 0), "ground", "motorway", 110, 0, 12.0, None),
         Member("lw", _ray((0, 0), 29, -650, -150, 40), "ground", "motorway", 110, 0, 12.0, None)]
    net, systems, shapes, emb = _run(m)
    assert len(systems) == 1 and not _joined(net, systems[0], "upper", "lower")
    assert shapes[0][2] is not None
    need = ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + ix.deck_depth(600.0)
    end = _ray((0, 0), 29, 5, 5, 1)[0]
    for q in (np.zeros(2), end):               # (at the crossing and over the lower deck's end)
        zl = _shape_z(net, shapes, "lower", q)
        assert _shape_z(net, shapes, "upper", np.array([q[0], 0.0])) - zl >= need - 0.3


def test_a_deck_merging_along_one_at_another_level_still_joins_it():
    # a ramp's deck (layer 1) ending on a motorway's (layer 2) along it, 20 deg, as Vienna's A23: their joint holds
    m = [Member("main", _line(-300, 0, 300, 0, 76), "deck", "motorway", 100, 2, 12.0, None),
         Member("ramp", _ray((0, -1.0), 200, 0, 120, 31)[::-1], "deck", "motorway_link", 60, 1, 5.0, None),
         Member("under", _line(150, -400, 150, 400, 100), "ground", "secondary", 50, 0, 5.0, None),
         Member("mw", _line(-800, 0, -300, 0), "ground", "motorway", 100, 0, 12.0, None),
         Member("me", _line(300, 0, 800, 0), "ground", "motorway", 100, 0, 12.0, None)]
    net, systems, shapes, emb = _run(m)
    assert len(systems) == 1 and _joined(net, systems[0], "main", "ramp")
    assert shapes[0][2] is not None
    assert abs(_shape_z(net, shapes, "ramp", np.array([0.0, -1.0]))
               - _shape_z(net, shapes, "main", np.array([0.0, 0.0]))) < 0.3


def test_a_deck_along_a_tunnel_over_it_is_not_joined_to_its_middle():
    # Vienna: a 16 m street deck over a car park's ramp tunnel, along it, both ends 0.5 m off the tunnel's line: the
    # deck and the tunnel are either side of the ground, never joined there (their clearance held instead)
    m = [Member("tun", _line(0, -90, 0, 90, 46), "tunnel", "service", 20, -1, 3.0, None),
         Member("ts", _line(0, -300, 0, -90), "ground", "service", 20, 0, 3.0, None),
         Member("tn", _line(0, 90, 0, 300), "ground", "service", 20, 0, 3.0, None),
         Member("br", _line(-0.5, -8, 0.5, 8, 5), "deck", "residential", 30, 1, 4.0, None)]
    net, systems, shapes, emb = _run(m)
    assert len(systems) == 1 and not _joined(net, systems[0], "br", "tun")
    assert shapes[0][2] is not None
    need = ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + ix.deck_depth(16.0)
    for y in (-8.0, 0.0, 8.0):
        assert (_shape_z(net, shapes, "br", np.array([y / 16.0, y]))
                - _shape_z(net, shapes, "tun", np.array([0.0, y]))) >= need - 0.3


def test_a_street_ending_under_a_flyovers_middle_is_no_end_of_it_unless_the_map_joins_them():
    # the High Five: a secondary leaves its level-1 link's deck end 0.1 m off a level-4 flyover's line, 20 m under it.
    # Taken for the flyover's end on it, the street ran a 290 m embankment down from the flyover's height, into the
    # flyover over it further on. With no map node of both it is under the flyover, its clearance kept; a street
    # the map joins to the deck there (a node of both) still meets it
    def members(street_nodes):
        return [Member("fly", _line(-300, 0, 300, 0, 151), "deck", "motorway_link", 60, 4, 5.0, None,
                       frozenset({"fw", "fe"})),
                Member("under", _line(-100, -400, -100, 400, 200), "ground", "secondary", 50, 0, 5.0, None,
                       frozenset({"us", "un"})),
                Member("fw", _line(-800, 0, -300, 0), "ground", "motorway_link", 60, 0, 5.0, None,
                       frozenset({"fw"})),
                Member("fe", _line(300, 0, 800, 0), "ground", "motorway_link", 60, 0, 5.0, None,
                       frozenset({"fe"})),
                Member("street", _line(150, 0.1, 150, -400, 101), "ground", "secondary", 50, 0, 5.0, None,
                       street_nodes)]
    net, systems, shapes, emb = _run(members(frozenset({"s0", "s1"})))
    fly, street = _key_index(net, "fly"), _key_index(net, "street")
    assert len(systems) == 1 and shapes[0][2] is not None
    assert not any(i == fly and j == street for i, _si, j, _sj in systems[0]["ends"])
    assert any(c["u"] == fly and c["d"] == street for c in systems[0]["crossings"])
    assert street not in emb                                             # the street keeps its ground
    net, systems, shapes, emb = _run(members(frozenset({"s0", "fe"})))
    fly, street = _key_index(net, "fly"), _key_index(net, "street")
    assert any(i == fly and j == street for i, _si, j, _sj in systems[0]["ends"])


def test_decks_at_different_levels_meet_end_to_end_only_on_a_map_node():
    # Barcelona: a level-2 service deck's end lies on a level-1 deck's end in plan, the map joining them nowhere. Taken
    # for a joint (any two ends this close), the level-2 deck was held at the level-1 one's height there while it
    # crossed another road: 13 such joints in one 14-deck system. With no map node of both its end is over the other's,
    # its clearance kept; a deck the map joins there (a node of both: a road going on at another level) still meets it
    def members(lower_nodes):
        return [Member("upper", _line(-300, 0, 0, 0, 76), "deck", "service", 20, 2, 3.0, None,
                       frozenset({"uw", "u0"})),
                Member("uw", _line(-800, 0, -300, 0), "ground", "service", 20, 0, 3.0, None, frozenset({"uw"})),
                Member("lower", _line(0.5, 1.0, 300, 1.0, 76), "deck", "service", 20, 1, 3.0, None, lower_nodes),
                Member("le", _line(300, 1.0, 800, 1.0), "ground", "service", 20, 0, 3.0, None, frozenset({"le"})),
                Member("under", _line(-150, -400, -150, 400, 200), "ground", "secondary", 50, 0, 5.0, None,
                       frozenset({"us", "un"}))]
    net, systems, shapes, emb = _run(members(frozenset({"l0", "le"})))
    assert len(systems) == 1 and not _joined(net, systems[0], "upper", "lower")
    up, lo = _key_index(net, "upper"), _key_index(net, "lower")
    assert any({c["u"], c["d"]} == {up, lo} for c in systems[0]["crossings"])
    assert shapes[0][2] is not None
    need = ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + ix.deck_depth(300.0)
    assert (_shape_z(net, shapes, "upper", np.array([0.0, 0.0]))
            - _shape_z(net, shapes, "lower", np.array([0.5, 1.0]))) >= need - 0.3
    net, systems, shapes, emb = _run(members(frozenset({"u0", "le"})))
    assert len(systems) == 1 and _joined(net, systems[0], "upper", "lower")


def _arc(c, r, a0, a1, n=48):
    a = np.radians(np.linspace(a0, a1, n))
    return np.asarray(c, np.float64) + r * np.stack([np.cos(a), np.sin(a)], axis=1)


def test_a_road_looping_over_itself_is_not_joined_to_its_own_deck_under_it():
    # Kawazu's Nanadaru loop: one road in three decks at levels 1, 2 and 3; the level-2 deck ends right over the
    # level-1 one on its heading, where the level-3 one goes on and crosses it further on. Its end was taken for a
    # merge with the deck under it (held at its height there, and the level-3 deck's start 8.35 m over it): the loop
    # infeasible. A road meets its own parts only end to end; over its own deck it is a crossing, its clearance kept
    road = "loop"
    b = np.concatenate([_arc((200, 60), 60, -90, 90), _line(200, 120, -100, 120, 76)[1:],
                        _arc((-100, 60), 60, 90, 270)[1:]])
    m = [Member((road, "deck", 1), _line(-400, 0, 200, 0, 151), "deck", "trunk", 40, 1, 5.0, None),
         Member((road, "deck", 2), b, "deck", "trunk", 40, 2, 5.0, None),
         Member((road, "deck", 3), np.array([[-100.0, 0.0], [-50.0, 15.0], [50.0, -15.0], [400.0, -15.0]]), "deck",
                "trunk", 40, 3, 5.0, None),
         Member((road, "ground", "w"), _line(-800, 0, -400, 0), "ground", "trunk", 40, 0, 5.0, None),
         Member((road, "ground", "e"), _line(400, -15, 800, -15), "ground", "trunk", 40, 0, 5.0, None)]
    a, two, three = (road, "deck", 1), (road, "deck", 2), (road, "deck", 3)
    net, systems = ix.find_systems(m)
    assert len(systems) == 1
    assert _joined(net, systems[0], a, two) and _joined(net, systems[0], two, three)
    assert all(min(si, net.L[i] - si) <= ix.JOIN_M and min(sj, net.L[j] - sj) <= ix.JOIN_M     # (end to end only)
               for i, si, j, sj in systems[0]["joins"])
    net, systems, shapes, emb = _run(m)
    assert shapes[0][2] is not None
    need = ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + ix.DECK_FULL_DEPTH_M
    za = _shape_z(net, shapes, a, np.array([-100.0, 0.0]))
    assert _shape_z(net, shapes, two, np.array([-100.0, 0.0])) - za >= need - 0.3
    zc = _shape_z(net, shapes, three, np.array([0.0, 0.0]))         # (where it crosses it further on)
    assert zc - _shape_z(net, shapes, a, np.array([0.0, 0.0])) >= need - 0.3


def test_a_long_deck_crossing_a_street_keeps_its_girders_off_a_hill_away_from_the_crossing():
    # Kawazu's Nanadaru loop: decks crossing streets and each other had no floor, held up only at their crossings;
    # between them each went the straight way between its ends' ground, its lowest turn 18 m inside the hill it winds
    # round and the trucks on its decks in the hillside
    m = [Member(("road", "deck", 1), _line(-500, 0, 500, 0, 251), "deck", "secondary", 60, 1, 5.0, None),
         Member(("road", "ground", "w"), _line(-900, 0, -500, 0), "ground", "secondary", 60, 0, 5.0, None),
         Member(("road", "ground", "e"), _line(500, 0, 900, 0), "ground", "secondary", 60, 0, 5.0, None),
         Member("street", _line(-350, -300, -350, 300, 61), "ground", "residential", 40, 0, 4.0, None)]

    def hill(xy):
        return 20.0 * np.exp(-((np.asarray(xy)[:, 0] - 150.0) / 60.0) ** 2)
    net, systems, shapes, emb = _run(m, hill)
    assert len(systems) == 1 and [c["moves"] for c in systems[0]["crossings"]] == ["up"]
    pcs, smp, zs = shapes[0]
    assert zs is not None
    floor = ix.deck_depth(1000.0) + ix.DECK_AIR_M
    x, z = smp[0], zs[0]
    inner = (x["d"] >= ix.DECK_INNER_M) & (x["d"][-1] - x["d"] >= ix.DECK_INNER_M)
    assert (z - hill(x["xy"]))[inner].min() >= floor - 0.02
    assert _shape_z(net, shapes, ("road", "deck", 1), np.array([150.0, 0.0])) >= 20.0 + floor - 0.1     # (its top)


def _spiral(r0, pitch, a0, a1, n):
    a = np.linspace(a0, a1, n)
    r = r0 + pitch * a / (2 * np.pi)
    return np.stack([r * np.cos(a), r * np.sin(a)], axis=1)


def test_a_road_winding_over_itself_clears_its_own_turn_all_along_it_not_only_where_they_cross():
    # Kawazu's Nanadaru loop winds over its own turns 1.6-3.1 m off their lines for 20-135 m at a time, crossing them
    # only here and there at shallow angles: held apart only at those crossings, its lowest turn stood 5.6 m under the
    # one over it where the hill no longer hid it. A helix of one road in two decks (4 m between its turns, under the
    # 5 m of its half width), a street under it: its second turn clears its first all round
    m = [Member(("helix", "deck", 1), _spiral(60.0, 4.0, 0.0, 2 * np.pi, 97), "deck", "trunk", 40, 1, 5.0, None),
         Member(("helix", "deck", 2), _spiral(60.0, 4.0, 2 * np.pi, 4 * np.pi, 97), "deck", "trunk", 40, 2, 5.0, None),
         Member(("helix", "ground", "s"), _line(60, -500, 60, 0), "ground", "trunk", 40, 0, 5.0, None),
         Member(("helix", "ground", "n"), _line(68, 0, 68, 500), "ground", "trunk", 40, 0, 5.0, None),
         Member("street", _line(0, 0, 0, -400, 101), "ground", "residential", 40, 0, 4.0, None)]
    one, two = ("helix", "deck", 1), ("helix", "deck", 2)
    net, systems = ix.find_systems(m)
    assert len(systems) == 1
    assert not any({net.m[c["u"]].key, net.m[c["d"]].key} == {one, two} for c in systems[0]["crossings"])
    net, systems, shapes, emb = _run(m)
    assert shapes[0][2] is not None
    need = ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + ix.DECK_FULL_DEPTH_M
    for a in np.radians(np.arange(30.0, 331.0, 15.0)):     # (round the turn, away from where one goes on into the other)
        z1 = _shape_z(net, shapes, one, _spiral(60.0, 4.0, a, a, 1)[0])
        z2 = _shape_z(net, shapes, two, _spiral(60.0, 4.0, a + 2 * np.pi, a + 2 * np.pi, 1)[0])
        assert z2 - z1 >= need - 0.05, (np.degrees(a), z2 - z1)


def test_a_road_dug_under_a_rail_deck_is_that_low_under_the_overpass_beside_it():
    # Perth's Tonkin Highway: a trunk road under the railway in the motorway's median (a short rail deck: the road is
    # dug) and, 6 m on, under the motorway's deck (an overpass: it rises over the road). Solved apart, the road was dug
    # under the track and the deck lifted 7.3 m over the road's ground, its carriageway raised beside the track
    m = [Member("trunk", _line(-500, 0, 500, 0, 126), "ground", "trunk", 70, 0, 7.0, None),
         Member("rdeck", _line(0, -10, 0, 10, 6), "rail_deck", "rail", None, 1, 2.0, None),
         Member("rs", _line(0, -500, 0, -10), "rail", "rail", None, 0, 2.0, None),
         Member("rn", _line(0, 10, 0, 500), "rail", "rail", None, 0, 2.0, None),
         Member("mdeck", _line(8, -14, 8, 14, 8), "deck", "motorway", 100, 1, 6.0, None),
         Member("ms", _line(8, -600, 8, -14), "ground", "motorway", 100, 0, 6.0, None),
         Member("mn", _line(8, 14, 8, 600), "ground", "motorway", 100, 0, 6.0, None)]
    net, systems, shapes, emb = _run(m)
    assert len(systems) == 1                                            # one interchange: the trunk's dip is both's
    assert shapes[0][2] is not None
    zt = _shape_z(net, shapes, "trunk", np.array([8.0, 0.0]))
    zm = _shape_z(net, shapes, "mdeck", np.array([8.0, 0.0]))
    assert _shape_z(net, shapes, "trunk", np.array([0.0, 0.0])) <= -(ix.HEADROOM_M + ix.HEADROOM_MARGIN_M) + 0.05
    assert zm - zt >= ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + ix.deck_depth(28.0) - 0.05
    assert zm < 1.5                                                     # the motorway about at its ground


def test_overpasses_over_one_road_far_apart_stay_apart():
    m = [Member("street", _line(-800, 0, 800, 0, 200), "ground", "residential", 40, 0, 4.0, None),
         Member("d1", _line(0, -6, 0, 6, 4), "deck", "tertiary", 50, 1, 4.0, None),
         Member("d2", _line(600, -6, 600, 6, 4), "deck", "tertiary", 50, 1, 4.0, None)]
    net, systems, shapes, emb = _run(m)
    assert len(systems) == 2


def test_a_held_bridge_over_a_streets_mapped_tunnel_stays_at_its_ground_and_the_street_goes_under():
    # (grading's never-raised deck, the map's short tunnel under it: a 48 m bridge, an overpass by its length, raised
    # 8.3 m over the street's ground on its fills while the underpasses dug the street 6.9 m under it as well)
    def members(held):
        return [Member("street", _line(-400, 0, 400, 0, 100), "ground", "residential", 40, 0, 4.0, None),
                Member("deck", _line(0, -24, 0, 24, 13), "deck", "primary", 70, 1, 5.0, None, None, held),
                Member("s", _line(0, -400, 0, -24), "ground", "primary", 70, 0, 5.0, None),
                Member("n", _line(0, 24, 0, 400), "ground", "primary", 70, 0, 5.0, None)]
    net, systems, shapes, emb = _run(members(False))
    assert [c["moves"] for c in systems[0]["crossings"]] == ["up"]       # (not held: an overpass)
    net, systems, shapes, emb = _run(members(True))
    assert [c["moves"] for c in systems[0]["crossings"]] == ["down"]
    zd = _shape_z(net, shapes, "deck", np.array([0.0, 0.0]))
    zs = _shape_z(net, shapes, "street", np.array([0.0, 0.0]))
    need = ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + ix.deck_depth(48.0)
    assert abs(zd) < 0.5                                                  # the deck at its ground: no fills
    assert zd - zs >= need - 0.05 and zd - zs <= need + 0.5               # cleared once, not twice
    for k in ("s", "n"):                                                  # its approaches on their ground
        assert all(np.abs(zz).max() < 0.05 for _s, zz, _m in emb.get(_key_index(net, k), ()))
    assert any(mode == "cut" for _s, _z, mode in emb[_key_index(net, "street")])


def test_a_signed_height_limit_holds_at_its_own_crossing_only():
    # a street under two overpasses 600 m apart, a sign 1 m under the game's headroom at the west one alone: the west
    # deck clears the sign, the east one the game's headroom (the limit read where each crosses, as the dips read it)
    limit = ix.HEADROOM_M - 1.0
    sign = lambda q: limit if abs(q[0] + 300.0) < 4.0 else None
    m = [Member("street", _line(-800, 0, 800, 0, 400), "ground", "residential", 40, 0, 4.0, sign)]
    for x, name in ((-300.0, "w"), (300.0, "e")):
        m += [Member(name, _line(x, -30, x, 30, 13), "deck", "secondary", 60, 1, 5.0, None),
              Member(name + "s", _line(x, -600, x, -30), "ground", "secondary", 60, 0, 5.0, None),
              Member(name + "n", _line(x, 30, x, 600), "ground", "secondary", 60, 0, 5.0, None)]
    net, systems, shapes, emb = _run(m)
    depth = ix.deck_depth(60.0)
    zw = _shape_z(net, shapes, "w", np.array([-300.0, 0.0]))
    ze = _shape_z(net, shapes, "e", np.array([300.0, 0.0]))
    assert zw >= limit + ix.HEADROOM_MARGIN_M + depth - 0.05
    assert zw < ix.HEADROOM_M + depth - 0.5                         # not the game's headroom under the sign
    assert ze >= ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + depth - 0.05


def test_a_long_road_tunnel_under_a_decks_end_is_the_tunnels_own_not_a_dip():
    # Porte Maillot: the Avenue de Neuilly's deck over the Peripherique ends over the 349 m Tunnel Grand-Maillot. A
    # tunnel longer than TUNNEL_MAX_M is tunnels.py's (its bore buried under the ground over it), crossed by nothing
    # here: the shapes dug it as a dip held under its ground and raised the deck's end 6.2 m to clear that dip, which
    # was never drawn. A shorter one under the same end is an underpass, dug under it.
    base = [Member("mway", _line(-800, 0, 800, 0, 200), "ground", "motorway", 120, 0, 12.0, None),
            Member("deck", _line(0, -30, 0, 30, 13), "deck", "secondary", 60, 1, 5.0, None),
            Member("south", _line(0, -600, 0, -30), "ground", "secondary", 60, 0, 5.0, None),
            Member("north", _line(0, 30, 0, 600), "ground", "secondary", 60, 0, 5.0, None)]
    net0, systems0, shapes0, emb0 = _run(base)
    long_tun = Member("tun", _line(-175, 30, 175, 30, 90), "tunnel", "service", 30, -1, 3.0, None)
    net, systems, shapes, emb = _run(base + [long_tun])
    assert net.L[_key_index(net, "tun")] > ix.TUNNEL_MAX_M
    assert len(systems) == 1
    assert all(net.m[c["d"]].key != "tun" for c in systems[0]["crossings"])
    assert _key_index(net, "tun") not in emb                              # not cut: tunnels.py lays it
    for y in (-30.0, -15.0, 0.0, 15.0, 30.0):                             # the deck as if the tunnel were not there
        q = np.array([0.0, y])
        assert abs(_shape_z(net, shapes, "deck", q) - _shape_z(net0, shapes0, "deck", q)) < 1e-3
    short_tun = Member("tun", _line(-60, 30, 60, 30, 31), "tunnel", "service", 30, -1, 3.0, None)
    net, systems, shapes, emb = _run(base + [short_tun])
    assert [c["moves"] for c in systems[0]["crossings"] if net.m[c["d"]].key == "tun"] == ["down"]
    zd = _shape_z(net, shapes, "deck", np.array([0.0, 30.0]))
    zt = _shape_z(net, shapes, "tun", np.array([0.0, 30.0]))
    assert zd - zt >= ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + ix.deck_depth(60.0) - 0.05


def test_a_signed_road_under_a_wide_deck_has_its_headroom_under_the_whole_deck():
    # (the A5 by Heidelberg: a 21.6 m held motorway deck, 14 m wide, over a service road signed 3.8 m at 78 deg; the
    # clearance held where the centrelines cross alone, the dip rose 0.2 m under the deck's east half and the road's
    # own ground there left 3.78 m under the girders: a 3.8 m truck in them)
    sign = lambda q: 3.8
    t = np.radians(12.0)
    m = [Member("service", _line(-200 * np.cos(t), 200 * np.sin(t), 200 * np.cos(t), -200 * np.sin(t), 100),
                "ground", "service", None, 0, 2.0, sign),
         Member("a5", _line(0, -10.8, 0, 10.8, 7), "deck", "motorway", 120, 1, 7.0, None, None, True),
         Member("s", _line(0, -400, 0, -10.8), "ground", "motorway", 120, 0, 7.0, None),
         Member("n", _line(0, 10.8, 0, 400), "ground", "motorway", 120, 0, 7.0, None)]
    net, systems, shapes, emb = _run(m)
    assert [c["moves"] for c in systems[0]["crossings"]] == ["down"]
    need = 3.8 + ix.HEADROOM_MARGIN_M + ix.deck_depth(21.6)
    deck = _key_index(net, "a5")
    road = _key_index(net, "service")
    under = 0
    for pcs, smp, zs in shapes:
        for x, z in zip(smp, zs):
            for k in range(len(z)):
                if int(x["mem"][k]) != road:
                    continue
                s_u, off = ix.proj(net.lines[deck], net.s[deck], x["xy"][k])
                if off > 7.0 or not 0.0 < s_u < net.L[deck]:
                    continue                                            # (not under the deck)
                under += 1
                assert _shape_z(net, shapes, "a5", ix.at_s(net.lines[deck], net.s[deck], s_u)) - z[k] >= need - 0.01
    assert under >= 3                                                   # (the deck's whole width, not one point)


def test_a_railway_viaduct_over_a_road_bridge_is_over_it_by_the_maps_levels():
    # Zurich's Duttweilerbruecke (level 2) under the railway viaduct that crosses 7 m over it (level 3): every track
    # was level 1, the bridge over the viaduct and raised over the track's bed for it
    m = [Member("yard", _line(-300, -40, 300, -40, 80), "rail", "rail", None, 0, 2.0, None),
         Member("bridge", _line(-60, -100, 60, 100, 30), "deck", "secondary", 50, 2, 5.0, None),
         Member("south", _line(-200, -330, -60, -100), "ground", "secondary", 50, 0, 5.0, None),
         Member("north", _line(60, 100, 200, 330), "ground", "secondary", 50, 0, 5.0, None),
         Member("viaduct", _line(-400, 0, 400, 0, 100), "rail_deck", "rail", None, 3, 2.0, None)]
    net, systems = ix.find_systems(m)
    assert ix._over(m[1], m[4]) == "j" and ix._over(m[4], m[1]) == "i"
    assert ix._over(m[1], m[0]) == "i", "(and the bridge over the yard's tracks on the ground)"
    assert len(systems) == 1
    for c in systems[0]["crossings"]:
        assert net.m[c["d"]].key != "viaduct", "(nothing is over the viaduct)"
        assert not (net.m[c["u"]].key == "bridge" and net.m[c["d"]].kind == "rail_deck")


def test_the_maps_levels_order_two_structures_of_one_kind_and_a_track_as_a_road():
    def mem(kind, level, cls="rail"):
        return Member(f"{kind}{level}", _line(0, 0, 10, 0, 3), kind, cls, None, level, 2.0, None)
    assert ix._over(mem("rail_deck", 3), mem("rail_deck", 1)) == "i", "(a viaduct over another)"
    assert ix._over(mem("rail_deck", 1), mem("deck", 2, "primary")) == "j"
    assert ix._over(mem("tunnel", -1, "primary"), mem("tunnel", -2, "primary")) == "i", "(tunnels on two levels)"
    assert ix._over(mem("ground", 0, "primary"), mem("rail", -2)) == "i", "(a street over a track in a tunnel)"
    assert ix._over(mem("ground", 0, "primary"), mem("rail", 0)) is None, "(a level crossing)"
    assert ix._over(mem("deck", 1, "primary"), mem("deck", 1, "primary")) is None
    assert ix._over(mem("deck", -1, "primary"), mem("ground", 0, "primary")) == "i", "(a bridge is over the ground)"
