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
        zs = ix.solve_shape(net, sysm, pcs, smp, [ground(xy) for xy, _s in smp],
                            lambda i, s: float(ground(ix.at_s(net.lines[i], net.s[i], s)[None])[0]))
        shapes.append((pcs, smp, zs))
    emb = ix.embankments(net, systems, shapes,
                         lambda i, sv: ground(np.stack([np.interp(sv, net.s[i], net.lines[i][:, 0]),
                                                        np.interp(sv, net.s[i], net.lines[i][:, 1])], axis=1)))
    return net, systems, shapes, emb


def _shape_z(net, shapes, key, q):
    best = None
    for pcs, smp, zs in shapes:
        for (i, a, b, kind), (xy, sv), z in zip(pcs, smp, zs):
            if net.m[i].key != key:
                continue
            d = np.hypot(*(xy - q).T)
            k = int(np.argmin(d))
            if best is None or d[k] < best[0]:
                best = (d[k], z[k])
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
    assert z >= ix.CLEAR_ROAD_M + ix.DECK_THICK_M - 0.05            # the motorway's headroom under the deck
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
    assert zd - zs >= ix.CLEAR_MINOR_M + ix.DECK_THICK_M - 0.05
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
    assert zu <= -(ix.CLEAR_ROAD_M + ix.COVER_M) + 0.05
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
    assert zc >= ix.CLEAR_ROAD_M + ix.DECK_THICK_M - 0.05
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
    assert _shape_z(net, shapes, "deck", np.array([0.0, 0.0])) >= ix.CLEAR_RAIL_M + ix.DECK_THICK_M - 0.05
    g = ix.max_grade(m[3])
    for sv, zz, mode in emb[_key_index(net, "north")]:
        gr = np.abs(np.diff(zz) / np.diff(sv))
        assert np.isclose(gr.max(), g, atol=1e-3)                      # a long embankment at the road's max grade
        assert abs(zz[-1]) < 0.05
