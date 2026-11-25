"""gradeshapes: each interchange solved as one shape, placed on the ground with its embankments."""
import numpy as np

import gradeshapes as ix
from gradeshapes import Member


def _line(x0, y0, x1, y1, n=40):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def _run(members, ground=lambda xy: np.zeros(len(xy))):
    net, systems = ix.find_systems(members, lambda net_: (ground, lambda i, s: _fixed(net_, ground, i, s)))
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
    # the deck keeps its natural line (level: its ends' ground is) and rises as a whole by what it lacks over the
    # motorway: no more (it stood on a bump over the lanes with its ends falling away at their grade: a deck 1.1 m under
    # the embankment it lands on)
    z_end = _shape_z(net, shapes, "deck", np.array([0.0, 30.0]))
    assert 0.0 <= z - z_end < 0.3, (z, z_end)             # (the deck's own stiffness lets its crest curve a few dm)
    assert z <= ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + ix.deck_depth(60.0) + 0.1
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
    # in all (the deck keeps one straight line, so the two ends are balanced, not each as low as its grade lets it fall)
    assert (zs + 2.0) ** 2 + (zn - 2.0) ** 2 < (zc + 2.0) ** 2 + (zc - 2.0) ** 2
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
    # a ring road split in short parts (a roundabout's), a road bridge too short to rise over the 20 m one: the cut
    # spreads into the parts either side through the joints, each joint at one height, within the road's grade
    m = [Member("over_w", _line(-7, -300, -7, -7, 80), "ground", "tertiary", 50, 0, 4.0, None),
         Member("over_br", _line(-7, -7, -7, 7, 5), "deck", "tertiary", 50, 1, 4.0, None),
         Member("over_e", _line(-7, 7, -7, 300, 80), "ground", "tertiary", 50, 0, 4.0, None),
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


def test_a_roads_lane_sections_meeting_end_to_end_at_different_levels_are_joined_not_separated():
    # Shanghai: one elevated road cut where its lane count changes ("<road>~lanes<a>-<b>"), the sections tagged levels 1
    # and 2 by the map and sharing no node by the cut: taken for two decks one over the other, a clearance between
    # two samples the joint holds equal: the system infeasible, left as it was. They are one road going on
    def members(second):
        return [Member("r~lanes0-1", _line(-300, 0, 0, 0, 76), "deck", "motorway", 20, 1, 3.0, None,
                       frozenset({"a", "m1"})),
                Member(second, _line(0, 0, 300, 0, 76), "deck", "motorway", 20, 2, 3.0, None, frozenset({"m2", "b"})),
                Member("w", _line(-800, 0, -300, 0), "ground", "motorway", 20, 0, 3.0, None, frozenset({"w", "a"})),
                Member("e", _line(300, 0, 800, 0), "ground", "motorway", 20, 0, 3.0, None, frozenset({"e", "b"})),
                Member("s1", _line(-150, -300, -150, 300, 100), "ground", "secondary", 50, 0, 5.0, None,
                       frozenset({"s1a", "s1b"})),
                Member("s2", _line(150, -300, 150, 300, 100), "ground", "secondary", 50, 0, 5.0, None,
                       frozenset({"s2a", "s2b"}))]
    net, systems, shapes, emb = _run(members("r~lanes1-2"))
    assert len(systems) == 1 and _joined(net, systems[0], "r~lanes0-1", "r~lanes1-2")
    assert shapes[0][2] is not None
    net, systems, shapes, emb = _run(members("other"))                # another road's deck there: a separation
    assert len(systems) == 1 and not _joined(net, systems[0], "r~lanes0-1", "other")


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


def test_a_deck_that_already_clears_keeps_its_natural_height_and_one_that_lacks_rises_by_the_lack():
    # findground, rules 1 to 3: a structure's natural height is the line between the ground at its ends; where that
    # already leaves the clearance nothing moves, else it rises by what is lacking, as a whole (a deck long enough to
    # climb the lack in its own run: the cost law). The ground rising 5 % across a street, 300 m of deck
    need = ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + ix.deck_depth(300.0)

    def solved(dip):
        gr = lambda xy: 0.05 * xy[:, 1] - dip * np.exp(-(xy[:, 1] / 6.0) ** 2)      # (the street in a cutting dip m deep)
        m = [Member("street", _line(-800, 0, 800, 0, 200), "ground", "residential", 40, 0, 4.0, None),
             Member("deck", _line(0, -150, 0, 150, 76), "deck", "secondary", 60, 1, 5.0, None),
             Member("south", _line(0, -600, 0, -150), "ground", "secondary", 60, 0, 5.0, None),
             Member("north", _line(0, 150, 0, 600), "ground", "secondary", 60, 0, 5.0, None)]
        net, systems, shapes, emb = _run(m, gr)
        assert [c["moves"] for c in systems[0]["crossings"]] == ["up"]
        return [_shape_z(net, shapes, "deck", np.array([0.0, y])) for y in (-150.0, -75.0, 0.0, 75.0, 150.0)]
    # (the street 14 m down: the line is 14 m over it, more than it asks: the deck is its line)
    z = solved(14.0)
    assert np.abs(np.array(z) - np.array([-7.5, -3.75, 0.0, 3.75, 7.5])).max() < 0.15, z
    # (the street at the line's height: it lacks the clearance and rises by it, where it lacks it)
    z = solved(0.0)
    assert abs(z[2] - need) < 0.3, z
    assert z[4] >= z[0] - 1e-6, z


def test_a_deck_too_short_to_climb_its_lack_leaves_a_longer_fill_than_the_streets_cut_and_the_street_goes_under():
    # the same street and ground, 80 m of deck: 40 m of run to each end where 6.6 m at the deck's 6 % asks 110, so 140 m
    # of fill against the residential street's 110 m of cut (a dip at its 12 %): the street goes down, the deck stays on its line
    gr = lambda xy: 0.05 * xy[:, 1]
    m = [Member("street", _line(-800, 0, 800, 0, 200), "ground", "residential", 40, 0, 4.0, None),
         Member("deck", _line(0, -40, 0, 40, 21), "deck", "secondary", 60, 1, 5.0, None),
         Member("south", _line(0, -600, 0, -40), "ground", "secondary", 60, 0, 5.0, None),
         Member("north", _line(0, 40, 0, 600), "ground", "secondary", 60, 0, 5.0, None)]
    net, systems, shapes, emb = _run(m, gr)
    assert [c["moves"] for c in systems[0]["crossings"]] == ["down"]
    z = [_shape_z(net, shapes, "deck", np.array([0.0, y])) for y in (-40.0, 0.0, 40.0)]
    assert np.abs(np.array(z) - np.array([-2.0, 0.0, 2.0])).max() < 0.3, z       # (its natural line)
    assert _shape_z(net, shapes, "deck", np.array([0.0, 0.0])) - _shape_z(net, shapes, "street", np.array([0.0, 0.0])) >=         ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + ix.deck_depth(80.0) - 0.05


def test_a_deck_lying_on_the_ground_at_its_ends_has_no_girders_to_keep_off_it_there():
    # Kempttal's viaduct and Duttweilerbruecke's ramps lie on the ground for 20 m from their ends (the real decks within
    # 0.2 m of it where a floor of 2.7 m was asked), then span the valley: the floor from 10 m on lifted the whole of
    # the deck, stiff as it is, 3 to 5 m over the real one. Here the ground is level for 20 m from each end, then falls
    # 12 % to a valley 7 m down: the girders keep off it where it has fallen away, the deck is on it before
    def ground(xy):
        u = 80.0 - np.abs(np.asarray(xy)[:, 1])
        return -0.12 * np.minimum(np.maximum(u - 20.0, 0.0), 60.0)
    m = [Member("river", _line(-800, 0, 800, 0, 200), "ground", "residential", 40, 0, 4.0, None),
         Member("deck", _line(0, -80, 0, 80, 41), "deck", "secondary", 60, 1, 5.0, None),
         Member("south", _line(0, -600, 0, -80), "ground", "secondary", 60, 0, 5.0, None),
         Member("north", _line(0, 80, 0, 600), "ground", "secondary", 60, 0, 5.0, None)]
    net, systems, shapes, emb = _run(m, ground)
    x, z = shapes[0][1][0], shapes[0][2][0]
    assert np.abs(z).max() < 0.1, z                  # (the line between the ends: level, and it clears everything)
    floor = ix.deck_depth(160.0) + ix.DECK_AIR_M
    # (the floor holds over the span: no sample of the valley is closer than it to the girders' height)
    assert (z - ground(x["xy"]))[ground(x["xy"]) < -(floor + 0.01)].min() >= floor - 1e-6


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
    over = hill(x["xy"]) > ix.DECK_AIR_M            # (where the ground rises into the deck's natural line: not its abutments)
    assert over.sum() > 20
    assert (z - hill(x["xy"]))[over].min() >= floor - 0.02
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


def test_a_road_under_a_rail_deck_and_a_short_motorway_deck_beside_it_is_dug_under_the_deck_and_the_deck_stays():
    # Perth's Tonkin Highway: a trunk road under the railway in the motorway's median and, 6 m on, under the motorway's
    # deck. Both bridges are short over the road (findground's cost law: 14 m of run each side where the rise asks 106,
    # the fill longer than the road's cut at its 9 %): the road goes under both (the railways' own pass for the track,
    # here the motorway's deck at its ground and the road dug 6 m under it), not the road dug under the track and the
    # deck lifted 7.3 m over the road's ground, its carriageway raised beside a track left on the ground
    m = [Member("trunk", _line(-500, 0, 500, 0, 126), "ground", "trunk", 70, 0, 7.0, None),
         Member("rdeck", _line(0, -10, 0, 10, 6), "rail_deck", "rail", None, 1, 2.0, None),
         Member("rs", _line(0, -500, 0, -10), "rail", "rail", None, 0, 2.0, None),
         Member("rn", _line(0, 10, 0, 500), "rail", "rail", None, 0, 2.0, None),
         Member("mdeck", _line(8, -14, 8, 14, 8), "deck", "motorway", 100, 1, 6.0, None),
         Member("ms", _line(8, -600, 8, -14), "ground", "motorway", 100, 0, 6.0, None),
         Member("mn", _line(8, 14, 8, 600), "ground", "motorway", 100, 0, 6.0, None)]
    net, systems, shapes, emb = _run(m)
    assert shapes[0][2] is not None
    assert [c["moves"] for c in systems[0]["crossings"]] == ["down"]
    assert abs(_shape_z(net, shapes, "mdeck", np.array([8.0, 0.0]))) < 0.5               # (the deck at its ground)
    zt = _shape_z(net, shapes, "trunk", np.array([8.0, 0.0]))
    assert -zt >= ix.HEADROOM_M + ix.HEADROOM_MARGIN_M + ix.deck_depth(28.0) - 0.6        # (and the road under it)
    pcs = emb[_key_index(net, "trunk")]
    assert any(mode == "cut" for _s, _z, mode in pcs)


def test_overpasses_over_one_road_far_apart_stay_apart():
    m = [Member("street", _line(-800, 0, 800, 0, 200), "ground", "residential", 40, 0, 4.0, None),
         Member("d1", _line(0, -6, 0, 6, 4), "deck", "tertiary", 50, 1, 4.0, None),
         Member("d2", _line(600, -6, 600, 6, 4), "deck", "tertiary", 50, 1, 4.0, None)]
    net, systems, shapes, emb = _run(m)
    assert len(systems) == 2


def test_a_held_bridge_over_a_streets_mapped_tunnel_stays_at_its_ground_and_the_street_goes_under():
    # (grading's never-raised deck, the map's short tunnel under it: a 48 m bridge, an overpass by its length, raised
    # 8.3 m over the street's ground on its fills while the underpasses dug the street 6.9 m under it as well)
    def members(held, half=24.0):
        return [Member("street", _line(-400, 0, 400, 0, 100), "ground", "residential", 40, 0, 4.0, None),
                Member("deck", _line(0, -half, 0, half, 13), "deck", "primary", 70, 1, 5.0, None, None, held),
                Member("s", _line(0, -400 - half, 0, -half), "ground", "primary", 70, 0, 5.0, None),
                Member("n", _line(0, half, 0, 400 + half), "ground", "primary", 70, 0, 5.0, None)]
    net, systems, shapes, emb = _run(members(False, 200.0))
    assert [c["moves"] for c in systems[0]["crossings"]] == ["up"]       # (not held, long: an overpass)
    net, systems, shapes, emb = _run(members(False))
    assert [c["moves"] for c in systems[0]["crossings"]] == ["down"]     # (not held, 48 m: 128 m of fill against 106 m of cut)
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
        m += [Member(name, _line(x, -250, x, 250, 101), "deck", "secondary", 60, 1, 5.0, None),
              Member(name + "s", _line(x, -850, x, -250), "ground", "secondary", 60, 0, 5.0, None),
              Member(name + "n", _line(x, 250, x, 850), "ground", "secondary", 60, 0, 5.0, None)]
    net, systems, shapes, emb = _run(m)
    depth = ix.deck_depth(500.0)
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


def test_a_road_cut_in_lane_sections_in_one_cell_and_whole_in_its_neighbour_is_one_road():
    """Vienna's A23 viaduct: its sections ("<road>~lanes<a>-<b>") from the cell with lane tags and the unsplit copy from the
    neighbour without them were both members, the deck and the tunnel it goes into twice, and the deck rose 2.3 m."""
    sec = Member("r1~lanes000000000-811393678", _line(0, 0, 53, 0, 12), "deck", "motorway", 100, 1, 8.0, None)
    whole = Member("r1", _line(3, 1, 49, 1, 10), "deck", "motorway", 100, 1, 8.0, None)
    other = Member("r2", _line(3, 1, 49, 1, 10), "deck", "motorway", 100, 1, 8.0, None)
    far = Member("r1", _line(0, 200, 40, 200, 10), "deck", "motorway", 100, 1, 8.0, None)
    tun = Member("r1", _line(3, 1, 49, 1, 10), "tunnel", "motorway", 100, -1, 8.0, None)
    got = {id(m) for m in ix.one_road([sec, whole, other, far, tun])}
    assert id(whole) not in got and id(sec) in got, "(the copy along a section of its own road)"
    assert id(other) in got, "(another road lying there is another road)"
    assert id(far) in got, "(a copy of the road away from its sections is not theirs)"
    assert id(tun) in got, "(a tunnel is not a deck's copy)"
    both = [whole, other]
    assert ix.one_road(both) is both


def test_a_deck_read_from_two_cells_with_its_line_cut_differently_is_one_deck():
    """A part's key is its road's and the middle of its line: the copies a neighbouring cell reads of a deck (a vertex more,
    its line a few metres longer, its width measured differently) were two decks, each the other's continuation at both ends,
    so neither was an end of the structure on the road it lands on."""
    a = Member(("r1", "deck", 100, 0), _line(0, 0, 200, 0, 40), "deck", "motorway", 100, 1, 7.0, None)
    b = Member(("r1", "deck", 99, 0), _line(0, 0.5, 198, 0.5, 38), "deck", "motorway", 100, 1, 9.0, None)
    same = Member(("r1", "deck", 100, 0), _line(0, 0, 200, 0, 40), "deck", "motorway", 100, 1, 7.0, None)
    next_part = Member(("r1", "deck", 300, 0), _line(200, 0, 400, 0, 40), "deck", "motorway", 100, 1, 7.0, None)
    tun = Member(("r1", "tunnel", 100, 0), _line(0, 0, 200, 0, 40), "tunnel", "motorway", 100, -1, 7.0, None)
    other = Member(("r2", "deck", 100, 0), _line(0, 0, 200, 0, 40), "deck", "motorway", 100, 1, 7.0, None)
    got = {id(m) for m in ix.one_road([b, a, next_part, tun, other])}
    assert id(a) in got and id(b) not in got, "(the longer copy stays)"
    assert id(next_part) in got, "(the next part of the road is another part)"
    assert id(tun) in got and id(other) in got, "(a tunnel, another road's deck, are not copies)"
    assert len(ix.one_road([same, a])) == 1, "(copies as long as each other keep one)"
    c = Member(("r1", "deck", 101, 0), _line(0, 0, 200, 0, 40), "deck", "motorway", 100, 1, 7.0, None)
    assert ix.one_road([c, a])[0].key == ix.one_road([a, c])[0].key == a.key, "(the first by key, whatever their order)"


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


def test_a_deck_ending_at_a_tunnels_mouth_is_where_its_structure_leaves_the_ground():
    # La Defense: a deck's start joined a road tunnel's end at its mouth, no road on the ground there: no end of the
    # structure was found, so the deck's chain was its own and its natural line began at the mouth, the ground of the
    # hill over the tube (a system of 67 pieces infeasible: 4.7 m of rise asked in 20 m of deck from the mouth)
    m = [Member("tun", _line(0, -200, 0, 0, 41), "tunnel", "motorway", 90, -1, 12.0, None),
         Member("br", _line(0, 0, 0, 120, 31), "deck", "motorway", 90, 1, 12.0, None),
         Member("gr", _line(0, 120, 0, 400, 31), "ground", "motorway", 90, 0, 12.0, None),
         Member("st", _line(-100, 60, 100, 60, 31), "ground", "residential", 30, 0, 5.0, None)]
    net = ix.Net(m)
    rel = {i: ix._relations(net, i) for i in range(len(m))}
    links, leave = ix._Verdicts(net, rel, lambda i: i, None).links()
    assert (1, 0) in leave and (1, 1) in leave


def test_a_ramps_samples_stop_at_the_reach_budget_and_the_embankments_own_do_not():
    # (the reach's budget, _Ramper's left: a cut below a ground that climbs faster than the roads' grade law would
    # run the whole of every road it is followed along; the budget caps its samples where the reach's chain has
    # spent it. The embankments' own laying passes no budget (left None) and is never capped)
    road = Member("north", _line(0, 0, 0, 3000, 200), "ground", "motorway", 120, 0, 12.0, None)
    net = ix.Net([road])
    ground = lambda xy: 0.12 * np.asarray(xy, np.float64)[:, 1]

    def run(left):
        lay = ix._Ramper(net, lambda x: ground(x["xy"]), {})
        lay.ramp_from(0, 0.0, True, None, -ix.deck_grade(road), over=0.0, sign=-1.0, left=left)
        rp = lay.ramps[0]
        return rp, float(rp[4]["d"][min(rp[9], len(rp[4]["d"]) - 1)]), rp[9]

    _r, full, ks_none = run(None)
    assert full == ix.RAMP_MAX_M and ks_none == len(_r[5])          # (the cut never gets back: every sample)
    assert run(ix.RAMP_MAX_M)[1:] == (full, ks_none)                # (the whole budget: as with none)
    _r, capped, ks_cap = run(100.0)
    assert capped <= 100.0 + ix.STEP_M and ks_cap < ks_none


def test_a_projection_asked_for_the_near_pairs_alone_keeps_them_to_the_last_bit():
    # (_proj_many's near: the ramp's junctions ask it of every road in the ramp's box and keep a pair by its distance;
    # nothing was within JOIN_M in 39 % of the asks, 70 % of its seconds. A point against the segments whose box grown
    # by near holds it: a bound, so every pair within near is kept, with the full projection's arc and distance, and a
    # point with no such segment is as far as one none is near)
    import dnp
    rng = np.random.default_rng(7)

    def full(line, s, P):
        a, ab = line[:-1], line[1:] - line[:-1]
        L2 = np.maximum((ab * ab).sum(1), 1e-12)
        t = np.clip(((P[:, None, :] - a[None]) * ab[None]).sum(2) / L2[None], 0.0, 1.0)
        v = a[None] + t[..., None] * ab[None] - P[:, None, :]
        dist = dnp.hypot(v[..., 0], v[..., 1])
        j = np.argmin(dist, axis=1)
        r = np.arange(len(P))
        return s[j] + t[r, j] * (s[j + 1] - s[j]), dist[r, j]

    for trial in range(120):
        nseg, k = int(rng.integers(3, 300)), int(rng.integers(1, 1600))
        if trial % 3 == 0:       # a wandering line, the points scattered wide
            line = np.cumsum(rng.normal(size=(nseg + 1, 2)) * 30.0, axis=0)
            P = line[0] + rng.uniform(-2000, 2000, size=(k, 2))
        elif trial % 3 == 1:     # the points along the line (a ramp beside the road)
            line = np.cumsum(rng.normal(size=(nseg + 1, 2)) * 5.0, axis=0)
            idx = rng.integers(0, nseg, k)
            P = line[idx] + rng.uniform(size=(k, 1)) * (line[idx + 1] - line[idx]) + rng.normal(scale=ix.JOIN_M * 1.5, size=(k, 2))
        else:                    # a winding line, the points in a thin box across it
            t = np.linspace(0.0, 6.0 * np.pi, nseg + 1)
            line = np.stack([t * 50.0, 400.0 * np.sin(t)], axis=1)
            P = np.stack([rng.uniform(line[:, 0].min(), line[:, 0].max(), k), rng.uniform(-400.0, 400.0, k)], axis=1)
        s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(line, axis=0).T))]
        sf, df = full(line, s, P)
        sn, dn = ix._proj_many(line, s, P, near=ix.JOIN_M)
        kept = df <= ix.JOIN_M
        assert np.array_equal(kept, dn <= ix.JOIN_M), trial
        assert np.array_equal(sf[kept], sn[kept]), trial
        assert np.array_equal(ix._proj_many(line, s, P)[0], sf) and np.array_equal(ix._proj_many(line, s, P)[1], df)


def test_a_members_laws_are_made_once_and_follow_the_laws_set_anew():
    # (max_grade, curve_radius, deck_grade and deck_depth by the member's law key and the laws' values: asked 160,000
    # times a region for a thousand members. The same floats as unmemoized; a law set anew (grading sets them from
    # roads.py; a test eases one) is made anew, never a stale answer)
    m = Member("x", _line(0, 0, 100, 0), "deck", "secondary", 60, 1.0, 5.0, None)
    for fn in (ix.max_grade, ix.curve_radius, ix.deck_grade):
        assert fn(m) == fn._unmemoized(m)
    assert ix.deck_depth(120.0) == ix._deck_depth_unmemoized(120.0) and ix.deck_depth(120) == ix.deck_depth(120.0)
    was = ix.deck_grade(m)
    ix.DECK_MAX_GRADE = was - 0.01
    try:
        assert ix.deck_grade(m) == was - 0.01 and ix.deck_grade(m) == ix.deck_grade._unmemoized(m)
    finally:
        ix.DECK_MAX_GRADE = was
    assert ix.deck_grade(m) == was
    t = Member("r", _line(0, 0, 100, 0), "rail", "rail", None, 0.0, 2.0, None)
    assert ix.max_grade(t) == ix.railways.GRADE and ix.curve_radius(t) == max(ix.R_MIN_RAIL_M, (100.0 / 3.6) ** 2 / ix.A_VERT_RAIL)


def _scan_rounds(members, rounds, ground):
    """find_interchanges over the members of each round in turn, one Scan kept (a growth's rounds: owner.grow): the
    last round's (net, found)."""
    scan = ix.Scan()
    net = found = None
    for part in rounds:
        net, found = ix.find_interchanges([members[k] for k in part], lambda n_: (ground, lambda i, s: _fixed(n_, ground, i, s)),
                                          scan=scan, reach=True)
    return net, found


def test_a_growth_lays_the_same_ramps_with_and_without_its_memo():
    # (ix/owner.grow's rounds lay the same ramps again from the same ends: _Ramper.memo keeps their samples, ground
    # and curve by the members they run along. The rounds' answer must be the one laid whole)
    slope = lambda xy: 0.05 * xy[:, 1] + 0.01 * np.sin(xy[:, 0] / 30.0)
    keys = ["m1", "m2", "d1", "d2", "mid", "s", "n", "side", "e"]
    m = [Member("m1", _line(-800, 0, 800, 0, 200), "ground", "motorway", 120, 0, 12.0, None),
         Member("m2", _line(-800, 120, 800, 120, 200), "ground", "motorway", 120, 0, 12.0, None),
         Member("d1", _line(0, -30, 0, 30, 13), "deck", "secondary", 60, 1, 5.0, None),
         Member("d2", _line(0, 90, 0, 150, 13), "deck", "secondary", 60, 1, 5.0, None),
         Member("mid", _line(0, 30, 0, 90, 16), "ground", "secondary", 60, 0, 5.0, None),
         Member("s", _line(0, -600, 0, -30), "ground", "secondary", 60, 0, 5.0, None),
         Member("n", _line(0, 150, 0, 700), "ground", "secondary", 60, 0, 5.0, None),
         Member("side", _line(-500, -300, 500, -300, 100), "ground", "residential", 30, 0, 4.0, None),
         Member("e", _line(300, -700, 300, 700, 200), "ground", "primary", 70, 0, 5.0, None)]
    # (the rounds number the members otherwise, as a growth's does: its box slides and members_of keeps those in it)
    rounds = [[0, 1, 2, 3, 4, 5, 6], [7, 6, 5, 4, 3, 2, 1, 0], [8, 7, 6, 5, 4, 3, 2, 1, 0], [3, 1, 8, 0, 6, 2, 7, 5, 4],
              [0, 1, 2, 3, 4, 5, 6, 7, 8]]
    with_memo = _scan_rounds(m, rounds, slope)
    real_init = ix._Ramper.__init__

    def no_memo(self, net, ground_of, pieced, memo=None):
        real_init(self, net, ground_of, pieced, None)
    ix._Ramper.__init__ = no_memo
    try:
        whole = _scan_rounds(m, rounds, slope)
    finally:
        ix._Ramper.__init__ = real_init
    assert len(with_memo[1]) == 1 and len(whole[1]) == 1, "one interchange: the two decks' ramps meet on mid"
    a, b = with_memo[1][0], whole[1][0]
    assert sorted(a.sysm["seeds"]) == sorted(b.sysm["seeds"])
    assert [(c["u"], c["d"]) for c in a.sysm["crossings"]] == [(c["u"], c["d"]) for c in b.sysm["crossings"]]
    assert sorted(a.sysm["ends"]) == sorted(b.sysm["ends"])
    assert len(a.parts) == len(b.parts)
    scan = ix.Scan()
    hits = [0]
    real_by_index = ix._x_by_index

    def counting(net, x, segs):
        hits[0] += 1
        return real_by_index(net, x, segs)
    ix._x_by_index = counting
    try:
        for part in rounds:
            ix.find_interchanges([m[k] for k in part], lambda n_: (slope, lambda i, s: _fixed(n_, slope, i, s)), scan=scan,
                                 reach=True)
    finally:
        ix._x_by_index = real_by_index
    assert hits[0] > 4, "the ramps of the rounds before were laid again from the memo, in the round's own indices"


def _indexed(members, reach=False):
    """find_interchanges of the members with the process's pair index (a growth's round, as owner.grow makes it): (net,
    found, the round's scan)."""
    scan = ix.Scan()
    net, found = ix.find_interchanges(members, lambda n_: (lambda xy: np.zeros(len(xy)), lambda i, s: _fixed(n_, lambda xy: np.zeros(len(xy)), i, s)),
                                      scan=scan, reach=reach)
    return net, found, scan


def _seeds_relations(net, scan):
    """{seed's key: (the index's relations, a full scan's)}."""
    out = {}
    for i in range(len(net.m)):
        if ix._is_seed(net, i):
            out[net.m[i].key] = (scan.relations(net, i), ix._relations(net, i))
    return out


def _assert_index_exact(net, scan):
    for key, (got, whole) in _seeds_relations(net, scan).items():
        assert got == whole, key


def test_the_pair_index_gives_each_growth_the_relations_of_a_full_scan():
    """The process's pair index across growths (PAIRS): a seed's entry names every member near it in the net, whichever
    growth indexed each, so relations() is a full scan's. A seed the index has not seen is scanned along its line; one
    it has is asked for the members near its box that its entry lacks: this net's new members (a road joining the deck's
    end), a member indexed by another growth and met here for the first time, and the same part cut differently and kept
    in this net in place of the copy the last net kept (one_part: a seed's neighbours are not the same from net to net).
    A member that is no seed is never asked and has no entry: no relations of its own, and every seed near it is."""
    d1 = Member("d1", _line(0, -30, 0, 30, 13), "deck", "secondary", 60, 1, 5.0, None)
    mid = Member("mid", _line(0, 30, 0, 90, 16), "ground", "secondary", 60, 0, 5.0, None)
    d2 = Member("d2", _line(0, 90, 0, 150, 13), "deck", "secondary", 60, 1, 5.0, None)
    w = Member("w", _line(-500, 100, 500, 100, 100), "ground", "primary", 70, 0, 5.0, None)
    s = Member("s", _line(0, -600, 0, -30, 100), "ground", "secondary", 60, 0, 5.0, None)
    n = Member("n", _line(0, 151, 0, 400, 50), "deck", "secondary", 60, 1, 5.0, None)
    n2 = Member("n", _line(0, 151.5, 0, 400, 50), "deck", "secondary", 60, 1, 5.0, None)     # (the same part, cut otherwise)
    # (a growth over n alone first)
    _0, _f, scan0 = _indexed([n])
    assert scan0.scanned[-1] == 1 and len(ix.PAIRS.pairs) == 1, "the seed alone is in the index"
    # (a growth over the decks' district: its two seeds scanned; mid and w, no seeds, never asked)
    net1, _f, scan1 = _indexed([d1, mid, d2, w])
    assert scan1.scanned[-1] == 2 and scan1.fresh == 2 and len(ix.PAIRS.pairs) == 3
    _assert_index_exact(net1, scan1)
    # (a growth with s added and n met for the first time: d1 and d2, had already, asked for what they lack: s and n)
    net2, _f, scan2 = _indexed([d1, mid, d2, w, s, n])
    assert scan2.scanned[-1] == 2 and scan2.fresh == 0, "d1 (lacks s) and d2 (lacks n) are asked, both ways: n lacks nothing then"
    _assert_index_exact(net2, scan2)
    rel = _seeds_relations(net2, scan2)
    keys = lambda got: {net2.m[r[0]].key for r in got}
    assert "n" in keys(rel["d2"][0]) and "d2" in keys(rel["n"][0]), "the strangers are paired"
    # (the same part cut otherwise takes n's place, as one_part keeps another copy from one net to the next)
    net3, _f, scan3 = _indexed([d1, mid, d2, w, s, n2])
    assert scan3.scanned[-1] == 1 and scan3.fresh == 1, "n2 scanned (new; its scan gives d2 its side): nobody else"
    _assert_index_exact(net3, scan3)
    stale = ix._line_id(net2, _key_index(net2, "n"))
    assert stale not in {ix._line_id(net3, r[0]) for r in scan3.relations(net3, _key_index(net3, "d2"))},         "n's stale entry names a member this net has not"
    # (nothing new, nothing lacked: nobody asked)
    net4, _f, scan4 = _indexed([d1, mid, d2, w, s, n2])
    assert scan4.scanned[-1] == 0 and scan4.fresh == 0
    _assert_index_exact(net4, scan4)


def test_the_pair_index_at_its_caps_holds_the_nets_seeds_and_scans_the_rest_again():
    """At its caps the index keeps the net's own seeds (a growth's rounds go on reading one another) and nothing of the
    other growths: met again, they are scanned as new. A growth reading the old index goes on with it."""
    d1 = Member("d1", _line(0, -30, 0, 30, 13), "deck", "secondary", 60, 1, 5.0, None)
    mid = Member("mid", _line(0, 30, 0, 90, 16), "ground", "secondary", 60, 0, 5.0, None)
    d3 = Member("d3", _line(-50, 60, 50, 60, 11), "deck", "secondary", 60, 1, 5.0, None)
    far = Member("far", _line(-3000, 0, -3000, 500, 100), "deck", "secondary", 60, 1, 5.0, None)
    _0, _f, scan1 = _indexed([d1, mid])
    assert scan1.scanned[-1] == 1 and len(ix.PAIRS.pairs) == 1
    was = ix.PAIRS
    ix.PAIRS_MAX_SEEDS, ix.PAIRS_MAX_PAIRS = 2, 2
    try:
        _1, _f, scan2 = _indexed([d1, mid, d3])          # (not full yet: d3 new)
        assert ix.PAIRS is was and scan2.scanned[-1] == 1 and scan2.fresh == 1 and len(ix.PAIRS.pairs) == 2
        _assert_index_exact(_1, scan2)
        _2, _f, scan3 = _indexed([d1, mid, far])         # (full: d1, this net's seed, kept; d3 out of it, dropped; far new)
        assert ix.PAIRS is not was and len(was.pairs) == 2, "the old index is the old growth's still"
        assert set(ix.PAIRS.pairs) == {ix._line_id(_2, _key_index(_2, "d1")), ix._line_id(_2, _key_index(_2, "far"))}
        assert scan3.scanned[-1] == 1 and scan3.fresh == 1
        _assert_index_exact(_2, scan3)
        _3, _f, scan4 = _indexed([d1, mid, far, d3])     # (d3 met again: new)
        assert scan4.scanned[-1] == 1 and scan4.fresh == 1
        _assert_index_exact(_3, scan4)
    finally:
        ix.PAIRS_MAX_SEEDS, ix.PAIRS_MAX_PAIRS = 100000, 200000


def test_the_find_asks_the_step_limit_in_its_dips_and_reaches_not_only_between_its_seeds_relations():
    # (a growth round's find of Shanghai's Yan'an mesh: 28 s of dips and 34 s of reaches at 128k members, and a close
    # of 3 s, none of it asking the cancel mechanism: the extent's 60 s limit was seen when the round ended, minutes
    # late, 48 times a build. The reaches ask per deck and per 32 ramps, the dips per road dug, the close per
    # interchange)
    import time
    import xproc
    slope = lambda xy: 0.05 * xy[:, 1] + 0.01 * np.sin(xy[:, 0] / 30.0)
    copies = 20
    span = 300
    m = [Member("m1", _line(-100, 0, copies * span + 100, 0, 200), "ground", "motorway", 120, 0, 12.0, None),
         Member("m2", _line(-100, 120, copies * span + 100, 120, 200), "ground", "motorway", 120, 0, 12.0, None)]
    for k in range(copies):
        x = k * span
        m += [Member("d1_%d" % k, _line(x, -30, x, 30, 13), "deck", "secondary", 60, 1, 5.0, None),
              Member("d2_%d" % k, _line(x, 90, x, 150, 13), "deck", "secondary", 60, 1, 5.0, None),
              Member("mid_%d" % k, _line(x, 30, x, 90, 16), "ground", "secondary", 60, 0, 5.0, None),
              Member("s_%d" % k, _line(x, -250, x, -30), "ground", "secondary", 60, 0, 5.0, None),
              Member("n_%d" % k, _line(x, 150, x, 250), "ground", "secondary", 60, 0, 5.0, None)]
    calls = []

    def slow_ground(xy):
        calls.append(1)
        time.sleep(0.01)
        return slope(xy)

    reader = lambda net: (slow_ground, None)
    t0 = time.perf_counter()
    net, found = ix.find_interchanges(m, reader, reach=True)
    full_s, whole = time.perf_counter() - t0, len(calls)
    assert len(found) == copies, "each copy's two decks' ramps meet: one interchange a copy (%d)" % len(found)
    assert full_s > 1.5 and whole > 150, "the whole find is the slow thing here (%.1f s, %d ground reads)" % (full_s, whole)
    calls.clear()
    old = xproc.set_deadline("extent", 0.2)
    try:
        t0 = time.perf_counter()
        try:
            ix.find_interchanges(m, reader, reach=True)
            raise AssertionError("the find ran past the step's limit without asking")
        except xproc.TimeLimit as e:
            assert e.what == "extent"
        took = time.perf_counter() - t0
    finally:
        xproc.restore_deadline(old)
    assert 0.2 <= took < full_s / 3, "stopped where it asked: %.2f s of the find's %.1f s, %d of its %d ground reads" % (
        took, full_s, len(calls), whole)
    xproc.check_cancel()         # (the deadline gone: the next find in the thread is not stopped)


def _chain_members():
    # (a road of 12 parts end to end with forks and strays at the joints, ground roads of one feature and others)
    rng = np.random.default_rng(7)
    m = []
    for k in range(12):
        m.append(Member(("r", k // 3, k), _line(k * 60.0, 0, k * 60.0 + 60.0, 0, 20), "ground", "secondary", 60, 0, 5.0, None))
    for k in range(6):
        y = float(rng.uniform(-3, 3))
        m.append(Member(("f", k), _line(k * 120.0, y, k * 120.0 + 60.0, 25 * (k % 2 - 0.5), 20), "ground", "secondary", 60, 0, 5.0,
                        None))
    return m


def test_next_member_asked_twice_of_a_net_is_what_it_is_computed_afresh():
    # (a dip's road is walked on from both its ends for every crossing under it: next_member keeps its answer per end of
    # the net, and the answer is the one _next_member computes)
    net = ix.Net(_chain_members())
    for i in range(len(net.m)):
        for at_end in (False, True):
            first = ix.next_member(net, i, at_end)
            assert first == ix._next_member(net, i, at_end), (i, at_end)
            assert ix.next_member(net, i, at_end) == first
    whole = ix.walk(net, 0, True, 400.0)
    assert [j for j, _a, _b in whole][:3] == [1, 2, 3], whole
    assert ix.walk(net, 0, True, 400.0) == whole


def test_piece_system_measures_the_seeds_by_one_call_what_it_measured_a_point_pair_at_a_time():
    from ix import owner
    rng = np.random.default_rng(3)

    class M:
        def __init__(self, key):
            self.key, self.kind = key, "deck"

    class Net:
        pass

    class Sys:
        def __init__(self, seeds):
            self.sysm = {"seeds": seeds}

    def old(cur_net, cur, nxt_net, found, cid):
        mid = [cur_net.lines[i][len(cur_net.lines[i]) // 2] for i in cur.sysm["seeds"]]
        best = None
        for x in found:
            seeds = x.sysm["seeds"]
            if cid not in {ix.canonical_id(nxt_net.m[i]) for i in seeds}:
                continue
            if not mid:
                return x
            d = min(float(np.min(np.hypot(nxt_net.lines[j][:, 0] - m[0], nxt_net.lines[j][:, 1] - m[1])))
                    for j in seeds for m in mid)
            if best is None or d < best[0]:
                best = (d, x)
        return None if best is None else best[1]
    for trial in range(20):
        nxt = Net()
        nxt.lines = [rng.normal(size=(int(rng.integers(2, 30)), 2)) * 80 for _ in range(60)]
        nxt.m = [M(("deck", "w%d" % (k % 7))) for k in range(60)]
        cur = Net()
        cur.lines = nxt.lines[:10]
        piece = Sys(list(range(int(rng.integers(0, 5)) + 1 if trial % 5 else 0)))
        found = [Sys(sorted(rng.choice(60, int(rng.integers(1, 9)), replace=False).tolist())) for _ in range(8)]
        found = [Sys(x.sysm["seeds"]) for x in found]
        for cid in ("deck:w0", "deck:w3", "deck:nowhere"):
            assert owner.piece_system(cur, piece, nxt, found, cid) is old(cur, piece, nxt, found, cid), (trial, cid)
