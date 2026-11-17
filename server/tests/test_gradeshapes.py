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
        zs = ix.solve_shape(net, sysm, pcs, smp, [ground(x["xy"]) for x in smp],
                            lambda i, s: float(ground(ix.at_s(net.lines[i], net.s[i], s)[None])[0]))
        shapes.append((pcs, smp, zs))
    emb = ix.embankments(net, systems, shapes,
                         lambda i, sv: ground(np.stack([np.interp(sv, net.s[i], net.lines[i][:, 0]),
                                                        np.interp(sv, net.s[i], net.lines[i][:, 1])], axis=1)))
    return net, systems, shapes, emb


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
    # the ground rising 5 % to the north across a motorway: the deck rises with it at its grade, so neither end stands
    # higher over its ground than the clearance and the deck's own grade ask
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
    assert zn > zs                                                     # tilted with the ground
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
