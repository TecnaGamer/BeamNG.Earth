"""ixcheck's tests: the import rule, each check on synthetic artifacts (a clean one, then one per fault it must name),
and scan over a saved run."""
import ast
import dataclasses
import os
import sys
import tempfile

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
IXCHECK = os.path.join(SERVER, "ixcheck")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import ix.artifacts as artifacts                                                            # noqa: E402
from ixcheck import c1_paths, c2_extent, c3_order, c4_terrain, c5_datum, c6_heights, load, run  # noqa: E402

FORBIDDEN = {"gradeshapes", "grading", "roads", "railways", "interchange_audit"}


def member(key, pts, kind="ground", level=0.0):
    return artifacts.Member(key, np.asarray(pts, np.float64), kind, "motorway", 100.0, level, 5.0)


def meta(step, key, parents=()):
    return artifacts.Meta(step=step, format=1, key=key, parents=tuple(parents), laws="test", scope="test")


def codes(mod, art, parents=None):
    return [f.code for f in mod.check(art, parents or {})]


def fails(mod, art, parents=None):
    return [f for f in mod.check(art, parents or {}) if f.level == "fail"]


def notes(mod, art, parents=None):
    return [f for f in mod.check(art, parents or {}) if f.level == "note"]


# ---- the import rule ---------------------------------------------------------------------------------------------


def test_import_rule():
    bad = []
    for f in sorted(os.listdir(IXCHECK)):
        if not f.endswith(".py"):
            continue
        with open(os.path.join(IXCHECK, f), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=f)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.module == "ix" and [a.name for a in node.names] == ["artifacts"]:
                    continue                    # the records: the one thing of ix the checks may read
                names = [node.module or ""]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in FORBIDDEN or top.startswith("staged") or (top == "ix" and name != "ix.artifacts"):
                    bad.append("%s imports %s" % (f, name))
    assert not bad, bad


# ---- a small net: a road, a deck over it, the ground stubs its ends meet ------------------------------------------


def net_paths():
    ms = [member("R", [(-40.0, 0.0), (40.0, 0.0)]),
          member("D", [(0.0, -40.0), (0.0, 40.0)], kind="deck", level=1.0),
          member("S", [(0.0, -70.0), (0.0, -40.0)]),
          member("N", [(0.0, 40.0), (0.0, 70.0)])]
    return artifacts.Paths(meta(1, "k1"), ms, [artifacts.Join(1, 0.0, 2, 30.0), artifacts.Join(1, 80.0, 3, 0.0)],
                           [artifacts.PlanCrossing(1, 40.0, 0, 40.0, np.zeros(2))])


def net_extent(paths=None):
    sysm = artifacts.System("SY", (1,), (artifacts.Join(1, 0.0, 2, 30.0),),
                            (artifacts.Boundary(1, 0.0, 2, 30.0),), (0.0, -40.0, 0.0, 40.0))
    return artifacts.Extent(meta(2, "k2", ("k1",)), "cell:0:0", [sysm])


def net_order():
    c = artifacts.Crossing(1, 40.0, 0, 40.0, np.zeros(2), 5.0, np.zeros((0, 2)))
    return artifacts.Order(meta(3, "k3"), "SY", [c], {"1": 1, "0": 0}, {"1": 1, "0": 0})


def net_parents():
    paths = net_paths()
    return paths, {1: paths, 2: net_extent(paths)}


# ---- check 1 ----


def test_c1_clean():
    paths, parents = net_parents()
    assert codes(c1_paths, paths, parents) == []


def test_c1_duplicate_member():
    paths = net_paths()
    paths.members.append(member("R", [(0.0, 100.0), (0.0, 120.0)]))
    assert "duplicate_member" in codes(c1_paths, paths)


def test_c1_broken_member():
    paths = net_paths()
    paths.members[2] = member("S", [(0.0, -70.0), (0.0, -70.0), (0.0, -40.0)])
    assert "broken_member" in codes(c1_paths, paths)


def test_c1_hanging_end_is_a_note():
    # (the map's own end: a deck's end 6 m from the road it should meet; nothing in the pipeline can join it)
    paths = net_paths()
    paths.members[3] = member("N", [(6.0, 40.0), (6.0, 70.0)])
    paths = dataclasses.replace(paths, joins=paths.joins[:1])
    assert not fails(c1_paths, paths)
    got = notes(c1_paths, paths)
    assert [f.code for f in got] == ["hanging_end"]
    assert "D" in got[0].text and "6.0 m" in got[0].text


def test_c1_unrelated_end():
    # (the deck's end lies on N and the paths name no join there: what the lines show and the artifact does not)
    paths = dataclasses.replace(net_paths(), joins=net_paths().joins[:1])
    got = fails(c1_paths, paths)
    assert [f.code for f in got] == ["unrelated_end"]
    assert "D" in got[0].text and "s=80.0" in got[0].text


def test_c1_a_crossing_at_an_end_agrees_within_a_join():
    # (a ground road's end 1.4 m from a deck's end is an end's relation: its two points are within a join of each other,
    # as the pipeline finds them; a crossing in the middle of both lines is held to half a metre)
    paths = net_paths()
    ms = list(paths.members)
    ms[3] = member("N", [(1.4, 40.0), (1.4, 70.0)])
    near = dataclasses.replace(paths, members=ms, crossings=[artifacts.PlanCrossing(1, 80.0, 3, 0.0, np.array([1.4, 40.0]))])
    assert not [f for f in fails(c1_paths, near) if f.code == "bad_crossing"]
    mid = dataclasses.replace(paths, crossings=[artifacts.PlanCrossing(1, 40.0, 0, 40.0, np.array([0.0, 0.0])),
                                                artifacts.PlanCrossing(1, 30.0, 0, 38.6, np.array([0.0, -10.0]))])
    assert [f.code for f in fails(c1_paths, mid)] == ["bad_crossing"]


def test_c1_bad_join():
    paths = net_paths()
    paths = dataclasses.replace(paths, joins=[artifacts.Join(1, 10.0, 2, 30.0)])
    assert "bad_join" in [f.code for f in fails(c1_paths, paths)]


def test_c1_bad_crossing():
    paths = dataclasses.replace(net_paths(),
                                crossings=[artifacts.PlanCrossing(1, 40.0, 0, 40.0, np.array([1.0, 0.0]))])
    assert [f.code for f in fails(c1_paths, paths)] == ["bad_crossing"]


# ---- check 2 ----


def test_c2_clean():
    paths, parents = net_parents()
    assert codes(c2_extent, parents[2], parents) == []


def test_c2_bad_seed():
    ext = net_extent()
    ext.systems[0] = artifacts.System("SY", (0,), (), (artifacts.Boundary(0, 40.0, 2, 30.0),),
                                      (-40.0, 0.0, 40.0, 0.0))
    assert "bad_seed" in codes(c2_extent, ext, {1: net_paths()})


def test_c2_no_boundary():
    ext = net_extent()
    ext.systems[0] = artifacts.System("SY", (1,), (), (), (0.0, -40.0, 0.0, 40.0))
    got = notes(c2_extent, ext, {1: net_paths()})
    assert [f.code for f in got] == ["no_boundary"]


def test_c2_oversized_system():
    ms = [member("E%d" % i, [(float(i), 0.0), (float(i), 10.0)], kind="deck", level=1.0) for i in range(61)]
    paths = artifacts.Paths(meta(1, "big"), ms, [], [])
    ext = artifacts.Extent(meta(2, "big2", ("big",)), "cell:0:0",
                           [artifacts.System("BIG", tuple(range(61)), (), (), (0.0, 0.0, 60.0, 10.0))])
    got = fails(c2_extent, ext, {1: paths})
    assert [f.code for f in got] == ["oversized_system"]
    assert "E0" in got[0].text and "E60" in got[0].text


def test_c2_bad_boundary():
    ext = net_extent()
    ext.systems[0] = artifacts.System("SY", (1,), (), (artifacts.Boundary(1, 0.0, 1, 0.0),),
                                      (0.0, -40.0, 0.0, 40.0))
    assert "bad_boundary" in codes(c2_extent, ext, {1: net_paths()})
    ext = net_extent()
    ext.systems[0] = artifacts.System("SY", (1,), (), (artifacts.Boundary(1, 20.0, 2, 30.0),),
                                      (0.0, -40.0, 0.0, 40.0))
    assert "bad_boundary" in codes(c2_extent, ext, {1: net_paths()})


def test_c2_bad_box():
    ext = net_extent()
    ext.systems[0] = artifacts.System("SY", (1,), (), (artifacts.Boundary(1, 0.0, 2, 30.0),),
                                      (0.0, -10.0, 0.0, 10.0))
    assert [f.code for f in fails(c2_extent, ext, {1: net_paths()})] == ["bad_box"]


# ---- check 3 ----


def test_c3_clean():
    _paths, parents = net_parents()
    assert codes(c3_order, net_order(), parents) == []


def test_c3_bad_crossing():
    o = dataclasses.replace(net_order(),
                            crossings=[artifacts.Crossing(1, 40.0, 0, 40.0, np.array([1.2, 0.0]), 5.0,
                                                          np.zeros((0, 2)))])
    assert [f.code for f in fails(c3_order, o, {1: net_paths()})] == ["bad_crossing"]


def test_c3_order_cycle():
    q = np.zeros(2)
    o = dataclasses.replace(net_order(), crossings=[
        artifacts.Crossing(1, 40.0, 0, 40.0, q, 5.0, np.zeros((0, 2))),
        artifacts.Crossing(0, 40.0, 1, 40.0, q, 5.0, np.zeros((0, 2)))], level={"1": 2, "0": 1})
    got = fails(c3_order, o, {1: net_paths()})
    assert "order_cycle" in [f.code for f in got]
    text = next(f.text for f in got if f.code == "order_cycle")
    assert "R" in text and "D" in text


def test_c3_bad_level():
    o = dataclasses.replace(net_order(), level={"1": 0, "0": 0})
    assert "bad_level" in codes(c3_order, o, {1: net_paths()})


def test_c3_bad_sign():
    o = dataclasses.replace(net_order(), sign={"1": 0, "0": 0})
    assert "bad_sign" in codes(c3_order, o, {1: net_paths()})


def test_c3_bad_need():
    o = dataclasses.replace(net_order(),
                            crossings=[artifacts.Crossing(1, 40.0, 0, 40.0, np.zeros(2), 0.0, np.zeros((0, 2)))])
    assert "bad_need" in codes(c3_order, o, {1: net_paths()})


# ---- check 3 under the joins: crossings, joins and the grade law must have heights --------------------------------


def road_of_two(levels):
    """Three roads of two decks each, the second joined to the first at 60 m: A1 A2, B1 B2, C1 C2 (members 0..5)."""
    ms = []
    for k, y in zip("ABC", (0.0, 300.0, 600.0)):
        ms += [member(k + "1", [(0.0, y), (60.0, y)], kind="deck", level=levels[0]),
                member(k + "2", [(60.0, y), (120.0, y)], kind="deck", level=levels[1])]
    return artifacts.Paths(meta(1, "w1"), ms, [], [])


def weld_parents():
    joins = tuple(artifacts.Join(2 * k, 60.0, 2 * k + 1, 0.0) for k in range(3))
    sysm = artifacts.System("SY", tuple(range(6)), joins, (), (0.0, 0.0, 120.0, 600.0))
    return {1: road_of_two((3.0, 1.0)), 2: artifacts.Extent(meta(2, "w2", ("w1",)), "cell:0:0", [sysm])}


def weld_order(pairs, sign):
    xs = [artifacts.Crossing(u, 55.0, d, 5.0, np.zeros(2), 5.0, np.zeros((0, 2))) for u, d in pairs]
    return artifacts.Order(meta(3, "w3"), "SY", xs, {str(i): v for i, v in enumerate(sign)},
                           {str(i): len(sign) - i for i in range(len(sign))})


def test_c3_weld_cycle_with_layer_decisions_fails():
    # A1 over B2, B1 over C2, C1 over A2, the three roads each one road joined at 60 m: a loop of layer decisions
    o = weld_order([(0, 3), (2, 5), (4, 1)], [1] * 6)
    got = [f for f in c3_order.check(o, weld_parents()) if f.code == "weld_cycle"]
    assert [f.level for f in got] == ["fail"]
    assert "A1" in got[0].text and "layers alone" in got[0].text


def test_c3_weld_cycle_of_the_structure_alone_is_a_note():
    o = weld_order([(0, 3), (2, 5), (4, 1)], [1, -1, 1, -1, 1, -1])
    got = [f for f in c3_order.check(o, weld_parents()) if f.code == "weld_cycle"]
    assert [f.level for f in got] == ["note"]


def test_c3_two_decisions_that_hold_with_the_joins_are_clean():
    o = weld_order([(0, 3), (2, 5)], [1] * 6)
    assert [f for f in c3_order.check(o, weld_parents()) if f.code == "weld_cycle"] == []


# ---- check 6: relaxations are counted apart from failures -----------------------------------------------------------


def test_c6_a_clearance_short_by_what_was_eased_is_a_relaxation_not_a_failure():
    h = solved_heights(pieces=[deck_piece(1, -40.0, z=3.0), deck_piece(4, 0.0, z=3.0), dip_piece()])
    assert "clearance" in [f.code for f in fails(c6_heights, h, solved_parents(heights=h))]
    eased = dataclasses.replace(h, diag={"relaxed": [dict(kind="clearance", amount=2.5, at=[[4, 20.0], [0, 40.0]],
                                                         text="clearance of D2 over R")]})
    got = c6_heights.check(eased, solved_parents(heights=eased))
    assert [f.code for f in got if f.level == "fail"] == []
    assert [f.code for f in got if f.code.startswith("relaxed_")] == ["relaxed_clearance"]
    short = dataclasses.replace(h, diag={"relaxed": [dict(kind="clearance", amount=1.0, at=[[4, 20.0], [0, 40.0]],
                                                         text="clearance of D2 over R")]})
    assert "clearance" in [f.code for f in fails(c6_heights, short, solved_parents(heights=short))]


# ---- a solved system: two decks joined over a road that dips under them --------------------------------------------


def solved_paths():
    ms = [member("R", [(-40.0, 20.0), (40.0, 20.0)]),
          member("D1", [(0.0, -40.0), (0.0, 0.0)], kind="deck", level=1.0),
          member("S", [(0.0, -70.0), (0.0, -40.0)]),
          member("N", [(0.0, 40.0), (0.0, 70.0)]),
          member("D2", [(0.0, 0.0), (0.0, 40.0)], kind="deck", level=1.0)]
    return artifacts.Paths(meta(1, "s1"), ms, [artifacts.Join(1, 0.0, 2, 30.0), artifacts.Join(1, 40.0, 4, 0.0),
                                               artifacts.Join(4, 40.0, 3, 0.0)],
                           [artifacts.PlanCrossing(4, 20.0, 0, 40.0, np.array([0.0, 20.0]))])


def solved_extent():
    sysm = artifacts.System("SY", (1, 4), (artifacts.Join(1, 40.0, 4, 0.0),),
                            (artifacts.Boundary(1, 0.0, 2, 30.0), artifacts.Boundary(4, 40.0, 3, 0.0)),
                            (0.0, -40.0, 0.0, 40.0))
    return artifacts.Extent(meta(2, "s2", ("s1",)), "cell:0:0", [sysm])


def solved_order():
    c = artifacts.Crossing(4, 20.0, 0, 40.0, np.array([0.0, 20.0]), 5.0, np.zeros((0, 2)))
    return artifacts.Order(meta(3, "s3", ("s2", "s1")), "SY", [c], {"1": 1, "4": 1, "0": 0},
                           {"1": 1, "4": 1, "0": 0})


def deck_piece(main, y0, z=10.0):
    ms = np.arange(0.0, 41.0, 4.0)
    xy = np.stack([np.zeros(len(ms)), y0 + ms], axis=1)
    return artifacts.Piece("deck", main, xy, ms.copy(), np.full(len(ms), main, np.int64), ms,
                           np.full(len(ms), float(z), np.float64))


def dip_piece(z=0.0):
    ms = np.arange(30.0, 51.0, 4.0)
    xy = np.stack([ms - 40.0, np.full(len(ms), 20.0)], axis=1)
    return artifacts.Piece("dip", 0, xy, ms - 30.0, np.zeros(len(ms), np.int64), ms,
                           np.full(len(ms), float(z), np.float64))


def solved_heights(pieces=None, solved=True):
    pieces = [deck_piece(1, -40.0), deck_piece(4, 0.0), dip_piece()] if pieces is None else pieces
    return artifacts.Heights(meta(6, "s6", ("s3", "s5", "s4", "s2", "s1")), "SY", pieces, solved, {})


def solved_terrain(ground=None, fixed=()):
    ground = ground if ground is not None else [np.full(11, 10.0), np.full(11, 10.0), np.full(6, 0.0)]
    return artifacts.Terrain(meta(4, "s4", ("s6", "s3")), "SY", ground, [np.ones(len(g)) for g in ground],
                             fixed=list(fixed))


def solved_datum():
    return artifacts.Datum(meta(5, "s5", ("s3",)), "SY", ("down",), ((0, 30.0, 50.0),), (),
                           [artifacts.Option(("down",), None, None, "today's verdicts")], 0)


def solved_parents(heights=None, terrain=None):
    return {1: solved_paths(), 2: solved_extent(), 3: solved_order(), 4: terrain or solved_terrain(),
            5: solved_datum(), 6: heights or solved_heights()}


# ---- check 4 ----


def test_c4_clean():
    assert codes(c4_terrain, solved_terrain(), {6: solved_heights()}) == []


def test_c4_bad_shape():
    t = solved_terrain(ground=[np.full(11, 10.0), np.full(11, 10.0), np.full(5, 0.0)])
    assert [f.code for f in fails(c4_terrain, t, {6: solved_heights()})] == ["bad_shape"]


def test_c4_ground_nan():
    g = [np.full(11, 10.0), np.full(11, 10.0), np.full(6, 0.0)]
    g[1][3] = np.nan
    t = solved_terrain(ground=g)
    assert [f.code for f in fails(c4_terrain, t, {6: solved_heights()})] == ["ground_nan"]


def test_c4_bad_trust():
    t = solved_terrain()
    t.trust[0][2] = 1.5
    assert [f.code for f in fails(c4_terrain, t, {6: solved_heights()})] == ["bad_trust"]


# ---- check 5 ----


def test_c5_clean():
    assert codes(c5_datum, solved_datum(), {1: solved_paths(), 3: solved_order()}) == []


def test_c5_bad_moves():
    d = dataclasses.replace(solved_datum(), moves=())
    assert "bad_moves" in codes(c5_datum, d, {1: solved_paths(), 3: solved_order()})
    d = dataclasses.replace(solved_datum(), moves=("sideways",))
    assert "bad_moves" in codes(c5_datum, d, {1: solved_paths(), 3: solved_order()})


def test_c5_bad_dip():
    d = dataclasses.replace(solved_datum(), dips=((1, 0.0, 10.0),))
    got = fails(c5_datum, d, {1: solved_paths(), 3: solved_order()})
    assert [f.code for f in got] == ["bad_dip"]
    assert "D1" in got[0].text


def test_c5_bad_chosen():
    d = dataclasses.replace(solved_datum(), chosen=3)
    assert [f.code for f in fails(c5_datum, d, {1: solved_paths(), 3: solved_order()})] == ["bad_chosen"]


# ---- check 6 ----


def test_c6_clean():
    got = c6_heights.check(solved_heights(), solved_parents())
    assert [f.level for f in got] == ["note", "note"]
    assert [f.code for f in got] == ["boundary_off", "boundary_off"]


def test_c6_no_heights():
    got = c6_heights.check(solved_heights(solved=False), solved_parents(heights=solved_heights(solved=False)))
    assert [f.code for f in got] == ["no_heights"]
    assert "D1" in got[0].text and "D2" in got[0].text


def test_c6_heights_nan():
    h = solved_heights()
    h.pieces[1].z[5] = np.nan
    assert "heights_nan" in codes(c6_heights, h, solved_parents(heights=h))


def test_c6_clearance():
    h = solved_heights(pieces=[deck_piece(1, -40.0), deck_piece(4, 0.0, z=3.0), dip_piece()])
    got = fails(c6_heights, h, solved_parents(heights=h))
    assert "clearance" in [f.code for f in got]
    text = next(f.text for f in got if f.code == "clearance")
    assert "D2" in text and "R" in text and "1.95 m short" in text


def test_c6_clearance_from_the_heights_the_terrain_read():
    # no dip piece: the lower surface is the height the terrain step read of the road the shape does not move (not the
    # ground on the deck's own samples: the road's ground is read along the road, smoothed, and is what the solve held the
    # deck over)
    h = solved_heights(pieces=[deck_piece(1, -40.0), deck_piece(4, 0.0, z=3.0)])
    fixed = [artifacts.Fixed(0, np.array([38.0, 40.0, 42.0]), np.array([10.0, 10.0, 10.0]))]
    t = solved_terrain(ground=[np.full(11, 2.0), np.full(11, 2.0)], fixed=fixed)
    got = fails(c6_heights, h, solved_parents(heights=h, terrain=t))
    text = next(f.text for f in got if f.code == "clearance")
    assert "11.95 m short" in text
    # (a crossing the terrain read nothing of was no constraint of the solve: nothing to hold it to)
    t = solved_terrain(ground=[np.full(11, 10.0), np.full(11, 10.0)])
    assert not [f for f in fails(c6_heights, h, solved_parents(heights=h, terrain=t)) if f.code == "clearance"]


def test_c6_joint_step():
    h = solved_heights(pieces=[deck_piece(1, -40.0), deck_piece(4, 0.0, z=10.2), dip_piece()])
    got = fails(c6_heights, h, solved_parents(heights=h))
    assert [f.code for f in got if f.level == "fail"] == ["joint_step"]
    assert "0.20 m" in got[0].text


def test_c6_boundary_off():
    t = solved_terrain(ground=[np.full(11, 8.0), np.full(11, 10.0), np.full(6, 0.0)])
    got = notes(c6_heights, solved_heights(), solved_parents(terrain=t))
    assert [f.code for f in got] == ["boundary_off", "boundary_off"]
    assert "2.00 m over" in got[0].text


def test_c6_grade():
    pc = deck_piece(4, 0.0)
    pc.z[:] = 10.0 + pc.ms * 0.25
    h = solved_heights(pieces=[deck_piece(1, -40.0), pc, dip_piece()])
    got = notes(c6_heights, h, solved_parents(heights=h))
    assert [f.code for f in got] == ["boundary_off", "boundary_off", "grade"]
    assert "0.250" in got[-1].text


def test_c6_a_decision_given_up_excuses_its_crossings_clearance():
    h = solved_heights(pieces=[deck_piece(1, -40.0, z=3.0), deck_piece(4, 0.0, z=3.0), dip_piece()])
    gone = dataclasses.replace(h, diag={"relaxed": [dict(kind="layer", amount=5.0, at=[[4, 20.0], [0, 40.0]],
                                                        text="decision D2 over R given up")]})
    got = c6_heights.check(gone, solved_parents(heights=gone))
    assert [f.code for f in got if f.level == "fail"] == [] and "relaxed_layer" in [f.code for f in got]


# ---- end to end: scan over a saved run -----------------------------------------------------------------------------


def test_scan_over_a_saved_run():
    arts = [solved_paths(), solved_extent(), solved_order(), solved_terrain(), solved_datum(), solved_heights()]
    with tempfile.TemporaryDirectory() as tmp:
        try:
            for a in arts:
                d = os.path.join(tmp, "s%d" % a.meta.step, a.meta.key[:2])
                os.makedirs(d, exist_ok=True)
                artifacts.save(a, os.path.join(d, a.meta.key + ".npz"))
        except NotImplementedError:
            pytest.skip("ix.artifacts.save is not written yet: this test runs once it is")
        scanned = load.scan(tmp)
        assert scanned["errors"] == []
        assert set(scanned["by_key"]) == {"s1", "s2", "s3", "s4", "s5", "s6"}
        assert sorted(scanned["steps"]) == [1, 2, 3, 4, 5, 6]
        assert len(scanned["steps"][6]) == 1
        parents = load.parents_of(scanned["by_key"]["s6"], scanned)
        assert all(s in parents for s in (1, 2, 3, 4, 5))   # the chain reaches every step it was made from
        rows = run.check_all(scanned)
        assert not [f for _a, fs in rows for f in fs if f.level == "fail"]
        lines, fail_lines, counts = run.report(rows)
        assert fail_lines == [] and counts["6"]["boundary_off"] == {"fail": 0, "note": 2}
        assert any(ln.startswith("   6") for ln in lines)  # one row per step in the table
