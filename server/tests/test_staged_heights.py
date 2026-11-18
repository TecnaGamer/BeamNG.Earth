"""Stage 5 of the staged solver (server/staged_heights.py): the banded solve, the heights of small systems against the kernel's, the
contradiction that has none, the switch that routes the shapes' heights through it."""
import os
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import gradeshapes as gs  # noqa: E402
import staged  # noqa: E402
import staged_feasible as s4  # noqa: E402
import staged_heights as h5  # noqa: E402
import staged_places as places  # noqa: E402

Member = gs.Member


def line(x0, y0, x1, y1, n=20):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def road(key, ln, kind="ground", cls="secondary", level=0.0, half=4.0, kmh=60):
    return Member(key, ln, kind, cls, kmh, level, half, None)


def overpass():
    return [road(("mway", "ground", "m"), line(-400, 0, 400, 0, 200), cls="motorway", kmh=120, half=12.0),
            road(("deck", "deck", 0, 0), line(0, -60, 0, 60, 31), "deck", "secondary", 1.0, half=5.0),
            road(("south", "ground", "s"), line(0, -600, 0, -60, 100), half=5.0),
            road(("north", "ground", "n"), line(0, 60, 0, 600, 100), half=5.0)]


def placed(members, ground=None):
    pl = places.synthetic(members, ground=ground)
    return staged.run(pl.members, pl.meta, None, None, pl.ground, feasibility=False)


def test_the_banded_solve_is_the_dense_one():
    rng = np.random.default_rng(5)
    n = 40
    d = rng.uniform(5.0, 9.0, n)
    e = rng.uniform(-1.0, 1.0, n)
    f = rng.uniform(-0.5, 0.5, n)
    e[17] = f[17] = f[16] = 0.0               # (two chains: nothing across 17 | 18)
    M = np.diag(d) + np.diag(e[:-1], 1) + np.diag(e[:-1], -1) + np.diag(f[:-2], 2) + np.diag(f[:-2], -2)
    b = rng.normal(size=n)
    got = h5.Banded(d, e, f).solve(b)
    assert np.abs(M @ got - b).max() < 1e-12
    # the batched sweeps over the chains give the same bits
    off = [0, 18, 40]
    batch = h5.BandedBatch(d, e, f, off).solve(b)
    assert batch.tobytes() == got.tobytes() or np.abs(batch - got).max() < 1e-13


def test_the_batched_sweeps_and_the_one_at_a_time_give_the_same_heights(monkeypatch):
    S = placed(overpass())
    sysm = S.G.systems[0]
    a = h5.solve_system(S.net, sysm, S.ground)
    monkeypatch.setattr(h5, "BATCH_MIN", 1)
    b = h5.solve_system(S.net, sysm, S.ground)
    assert max(float(np.abs(x - y).max()) for x, y in zip(a.zs, b.zs)) < 1e-6 and a.stats["iterations"] == b.stats["iterations"]


def test_an_overpass_gets_heights_that_hold_every_law_and_match_the_kernels():
    S = placed(overpass())
    sysm = S.G.systems[0]
    H = h5.solve_system(S.net, sysm, S.ground)
    st = H.stats
    assert st["feasible"] and st["converged"] and st["iterations"] < 1500
    assert st["violation_m"] <= h5.VIOLATION_M and st["grade_excess"] <= 1e-3 and st["clearance_shortfall_m"] <= 1e-3
    pcs = gs.pieces(S.net, sysm)
    smp = gs.sample(S.net, pcs)
    gl = s4.ground_lists(S.ground, S.net, smp)
    zk = gs.solve_shape(S.net, sysm, pcs, smp, gl, S.ground.fixed, {})
    assert zk is not None
    assert max(float(np.abs(a - b).max()) for a, b in zip(H.zs, zk)) < 0.05      # (the same problem, the same answer)
    # the deck stands over the motorway by the clearance the law asks
    need = gs.clearance_need(S.net, sysm["crossings"][0]["u"], sysm["crossings"][0]["d"], sysm["crossings"][0]["q"])
    assert H.zs[0][len(H.zs[0]) // 2] >= need - 1e-3


def test_the_answer_is_the_same_bits_twice():
    a = h5.solve_system(placed(overpass()).net, placed(overpass()).G.systems[0], placed(overpass()).ground)
    b = h5.solve_system(placed(overpass()).net, placed(overpass()).G.systems[0], placed(overpass()).ground)
    assert all(x.tobytes() == y.tobytes() for x, y in zip(a.zs, b.zs)) and a.stats["iterations"] == b.stats["iterations"]


def test_a_system_with_a_contradiction_has_no_heights_and_says_so():
    def twisted(p1_level, q_level):
        return [Member(("P", "deck", 1), line(-100, 0, 0, 0, 30), "deck", "trunk_link", None, p1_level, 4.0, None),
                Member(("P", "deck", 2), line(0, 0, 100, 0, 30), "deck", "trunk_link", None, 3.0, 4.0, None),
                Member(("Q1", "deck", 1), np.array([(-19, -60), (-19, 0), (-19, 60)], np.float64), "deck", "trunk_link", None, q_level, 4.0, None),
                Member(("Q2", "deck", 1), np.array([(-19, 60), (15, 60), (15, 0), (15, -60)], np.float64), "deck", "trunk_link", None, q_level, 4.0, None)]
    S = placed(twisted(1.0, 2.0))
    H = h5.solve_system(S.net, S.G.systems[0], S.ground)
    assert H.zs is None and not H.stats["feasible"] and H.stats["short_m"] > 1.0
    S2 = placed(twisted(3.0, 2.0))
    H2 = h5.solve_system(S2.net, S2.G.systems[0], S2.ground)
    assert H2.zs is not None and H2.stats["feasible"]


def test_the_switch_routes_the_shapes_heights_through_stage_5(monkeypatch):
    S = placed(overpass())
    sysm = S.G.systems[0]
    pcs = gs.pieces(S.net, sysm)
    smp = gs.sample(S.net, pcs)
    gl = s4.ground_lists(S.ground, S.net, smp)
    monkeypatch.setenv("BNE_STAGED", "1")
    diag = {}
    zs = gs.solve_shape(S.net, sysm, pcs, smp, gl, S.ground.fixed, diag)
    assert zs is not None and diag.get("staged") and diag["converged"] and diag["iterations"] > 0
    monkeypatch.delenv("BNE_STAGED")
    diag2 = {}
    zk = gs.solve_shape(S.net, sysm, pcs, smp, gl, S.ground.fixed, diag2)
    assert not diag2.get("staged") and max(float(np.abs(a - b).max()) for a, b in zip(zs, zk)) < 0.05


def test_a_hilltop_ground_at_the_far_end_of_the_ramp_does_not_hold_the_deck_up():
    # the ramp road climbs a hill 85 m over its 600 m: the deck's end is held within what its embankment reaches of the ground
    # *somewhere* along the road, not of the ground 600 m up (which held it 47 m over its own ground)
    def hill(xy):
        xy = np.atleast_2d(xy)
        return np.clip(xy[:, 1] - 60.0, 0.0, None) * 0.14
    S = placed(overpass(), ground=hill)
    H = h5.solve_system(S.net, S.G.systems[0], S.ground)
    assert H.stats["feasible"] and H.stats["converged"]
    z = H.zs[0]
    assert abs(float(z[-1]) - float(hill(S.net.lines[S.G.systems[0]["seeds"][0]][-1:])[0])) < 12.0


def test_a_deck_with_no_weight_of_its_own_is_set_by_the_kernels_floor_on_every_weight():
    """Brunau's viaducts: a deck's middle weighs nothing and its line is kept by the stiffness alone, so what sets its height is the
    kernel's floor on every weight (carriageway_solver.rs W_MIN, 1e-3). With a tenth of it (1e-4 on top of the weights) the decks
    stood 8-10 m over the kernel's, whose answer is 1-2 m from the survey's and ours 4-8 m; with it the two agree to the tolerance."""
    try:
        pl = places.truth("zurich_brunau")
        S = staged.run(pl.members, pl.meta, pl.ll, pl.edge, pl.ground, name="brunau")
        sysm = S.G.systems[0]
        pcs = gs.pieces(S.net, sysm)
        smp = gs.sample(S.net, pcs)
        gl = s4.ground_lists(S.ground, S.net, smp)
        zk = gs.solve_shape(S.net, sysm, pcs, smp, gl, S.ground.fixed, {})
    except (OSError, ImportError):
        pytest.skip("no Brunau survey fixture here")
    if zk is None:
        pytest.skip("no kernel here")
    H = h5.solve_system(S.net, sysm, S.ground)
    assert H.stats["converged"]
    assert max(float(np.abs(a - b).max()) for a, b in zip(H.zs, zk)) < 0.1
    assert h5.W_MIN == 1e-3
