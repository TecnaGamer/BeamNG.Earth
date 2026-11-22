"""Tracks are members of an interchange's shape: held at the bed the railways lay (the cost of moving one, from the laws'
grades), moved only where the interchange has no heights with them held, read once by terrain, read back by the railways."""
import os
import sys
import threading

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import carriageways  # noqa: E402
import gradelaw  # noqa: E402
import gradeshapes as gs  # noqa: E402
import railways  # noqa: E402
from gradeshapes import Member  # noqa: E402
from ix import s2_extent, s4_terrain  # noqa: E402
from ixcheck import c5_datum  # noqa: E402


def line(x0, y0, x1, y1, n=13):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def flat(xy):
    return np.zeros(len(xy))


def _fixed(net):
    """The rails' top 0.5 m over the ground (flat), the ground of every other member."""
    def f(i, s_):
        sv = np.atleast_1d(np.asarray(s_, np.float64))
        z = np.full(len(sv), 0.5 if net.m[i].kind in ("rail", "rail_deck") else 0.0)
        return z if np.ndim(s_) else float(z[0])
    return f


def system():
    """A road in a short tunnel under a track on the ground."""
    members = [Member(("rail", "rail", "T", 0, 0), line(-600, 0, 600, 0, 150), "rail", "rail", None, 0, 2.0, None),
               Member("west", line(0, -400, 0, -15), "ground", "secondary", 50, 0, 4.0, None),
               Member("under", line(0, -15, 0, 15, 8), "tunnel", "secondary", 50, -1, 4.0, None),
               Member("east", line(0, 15, 0, 400), "ground", "secondary", 50, 0, 4.0, None)]
    net, systems = gs.find_systems(members, lambda n_: (flat, _fixed(n_)))
    assert len(systems) == 1
    pcs = gs.pieces(net, systems[0])
    return net, systems[0], pcs, gs.sample(net, pcs)


def test_a_track_costs_more_to_move_than_a_road_by_the_ratio_of_their_grades():
    assert gs.W_TRACK == gs.W_DIP * gradelaw.road_grade("service", 20) / railways.GRADE
    assert gs.W_TRACK > gs.W_DIP


def test_the_track_under_which_a_road_goes_is_a_piece_of_the_shape_with_a_window_round_the_crossing():
    net, sysm, pcs, smp = system()
    tracks = [pc for pc in pcs if pc["kind"] == "track"]
    assert len(tracks) == 1 and net.m[tracks[0]["main"]].kind == "rail"
    (t, a, b), = sysm["tracks"]
    assert t == tracks[0]["main"] and a < 600 - 1 < 600 + 1 < b      # (the crossing at arc 600: the window holds it)
    assert gs.system_size(net, sysm)[0] >= 2


def test_pinned_the_crossings_read_the_track_and_free_they_solve_for_it():
    net, sysm, pcs, smp = system()
    ground = [flat(x["xy"]) for x in smp]
    k = [n for n, pc in enumerate(pcs) if pc["kind"] == "track"][0]
    n = len(smp[k]["xy"])
    pinned, pcp = gs.shape_problem(net, sysm, pcs, smp, ground, _fixed(net), pin_tracks=True)
    free, fcp = gs.shape_problem(net, sysm, pcs, smp, ground, _fixed(net))
    assert not [c for c in pcp if k in (c[0], c[2])]                 # (no coupling asks the track's unknowns)
    assert [c for c in fcp if k in (c[0], c[2]) and c[4] == "ge"]
    for ch in (pinned[k], free[k]):                                  # (held at the ends of its window, drawn to its bed between)
        assert set(np.flatnonzero(ch["lo"] == ch["hi"]).tolist()) == {0, n - 1}
        assert (ch["t"] == 0.5).all() and (ch["w"] == gs.W_TRACK).all()
    # (the road under it is held under its top, read where the crossing is)
    tunnel = [q for q, pc in enumerate(pcs) if pc["kind"] == "dip" or net.m[pc["main"]].kind == "tunnel"][0]
    assert np.isfinite(pinned[tunnel]["hi"]).any()


def test_the_track_stays_at_its_bed_where_the_interchange_has_heights_with_it_there():
    net, sysm, pcs, smp = system()
    ground = [flat(x["xy"]) for x in smp]
    diag = {}
    zs = gs.solve_shape(net, sysm, pcs, smp, ground, _fixed(net), diag)
    k = [n for n, pc in enumerate(pcs) if pc["kind"] == "track"][0]
    assert zs is not None and diag["tracks_moved"] is False
    assert np.allclose(zs[k], 0.5)


def test_the_tracks_move_only_when_the_pinned_problem_has_no_heights(monkeypatch):
    net, sysm, pcs, smp = system()
    ground = [flat(x["xy"]) for x in smp]
    real = carriageways.solve
    asked = []

    def solve(chains, cps, diagnostics=None, **kw):
        track = [c for c, pc in zip(chains, pcs) if pc["kind"] == "track"][0]
        k = [n for n, pc in enumerate(pcs) if pc["kind"] == "track"][0]
        asked.append(not [c for c in cps if k in (c[0], c[2])])         # (True: the crossings read the track)
        if len(asked) == 1:
            diagnostics.update(feasible=False, converged=True)         # (no heights with the track held)
            return [np.zeros(len(c["t"])) for c in chains]
        return real(chains, cps, diagnostics=diagnostics, **kw)
    monkeypatch.setattr(carriageways, "solve", solve)
    diag = {}
    zs = gs.solve_shape(net, sysm, pcs, smp, ground, _fixed(net), diag)
    assert asked[:2] == [True, False] and diag["tracks_moved"] is True and zs is not None


class _Planner:
    def __init__(self):
        self.asked = []

    def _rail_tops_many(self, net, asks, tops):
        self.asked.append({i: list(sv) for i, sv in asks.items()})
        for i, sv in asks.items():
            for v in sv:
                tops[(i, float(v))] = 7.0


def test_the_extent_reads_the_rails_once_for_all_their_arcs():
    net, sysm, pcs, smp = system()
    p = _Planner()
    fixed = s2_extent.make_fixed(p, net)
    fixed.prefetch({0: [1.0, 2.0], 1: [5.0]})
    assert len(p.asked) == 1 and list(p.asked[0]) == [0]               # (the rail; the ground road is not asked)


def test_terrain_asks_for_the_rails_all_at_once_before_reading_any():
    net, sysm, pcs, smp = system()
    order = []
    base = _fixed(net)

    def f(i, s_):
        order.append(("read", i))
        return base(i, s_)

    def prefetch(asks):
        order.append(("prefetch", sorted(asks)))
    f.prefetch = prefetch
    table = s4_terrain.read_fixed(net, sysm, pcs, smp, f)
    assert order[0][0] == "prefetch" and all(o[0] == "read" for o in order[1:])
    track = [i for i, m in enumerate(net.m) if m.kind == "rail"][0]
    assert track in order[0][1] and any(i == track for i, _s in table)


def test_the_railways_read_back_what_an_interchange_solved_except_for_its_own_steps():
    rails = [("T", "standard_gauge", "ground", np.array([[0.0, 0.0], [2.0, 0.0], [4.0, 0.0]]))]
    own = [(np.array([1.0, 1.0, 1.0]), np.array([0.0, 0.0, 0.0]))]
    saved = railways.TRACK_Z_HOOK
    try:
        railways.TRACK_Z_HOOK = lambda frame, parts: [np.array([np.nan, 3.0, 3.5])]
        got = railways._read_back(None, rails, own)
        assert got[0][0].tolist() == [1.0, 3.0, 3.5]                   # (the solved bed where there is one, else the railways')
        with railways.own_beds():
            assert railways._read_back(None, rails, own)[0][0].tolist() == [1.0, 1.0, 1.0]
        assert railways._read_back(None, rails, own)[0][0].tolist() == [1.0, 3.0, 3.5]
        seen = []

        def other_thread():
            seen.append(railways._read_back(None, rails, own)[0][0].tolist())
        with railways.own_beds():
            t = threading.Thread(target=other_thread)
            t.start()
            t.join()
        assert seen == [[1.0, 3.0, 3.5]]                               # (one thread's steps do not blind another's)
        railways.TRACK_Z_HOOK = lambda frame, parts: None
        assert railways._read_back(None, rails, own)[0][0].tolist() == [1.0, 1.0, 1.0]
    finally:
        railways.TRACK_Z_HOOK = saved


def test_the_datum_check_wants_a_track_window_on_a_track_of_a_crossing():
    from ix import artifacts
    meta = artifacts.Meta(5, 1, "", (), "", "system:x")
    members = [artifacts.Member("a", np.zeros((2, 2)), "rail", "rail", None, 0.0, 2.0, False, None),
               artifacts.Member("b", np.zeros((2, 2)), "ground", "primary", 50.0, 0.0, 4.0, False, None)]
    paths = artifacts.Paths(meta, members, [], [])
    order = artifacts.Order(meta, "x", [artifacts.Crossing(1, 0.0, 0, 0.0, np.zeros(2), 4.0, np.zeros((1, 2)))], {}, {})
    ok = artifacts.Datum(meta, "x", ("up",), (), (), [artifacts.Option(("up",), None, None, "")], tracks=((0, 0.0, 9.0),))
    bad = artifacts.Datum(meta, "x", ("up",), (), (), [artifacts.Option(("up",), None, None, "")], tracks=((1, 0.0, 9.0),))
    assert not [f for f in c5_datum.check(ok, {1: paths, 3: order}) if f.level == "fail"]
    assert [f.code for f in c5_datum.check(bad, {1: paths, 3: order})] == ["bad_track"]


def test_a_join_in_the_middle_of_a_deck_is_a_sample_of_it():
    # (a T-join: the second deck ends on the first's line 61.8 m along it, between two of its lattice samples (every 3.93 m): the
    # join is held at one height, so it is a sample, not held at the nearer of two samples half a step off it)
    members = [Member("M", line(-61.8, 0, 60, 0, 25), "deck", "secondary", 60, 1, 5.0, None),
               Member("E", line(0, -80, 0, 0, 25), "deck", "secondary", 60, 1, 5.0, None),
               Member("G", line(60, 0, 300, 0, 25), "ground", "secondary", 60, 0, 5.0, None),
               Member("H", line(-61.8, 0, -300, 0, 25), "ground", "secondary", 60, 0, 5.0, None),
               Member("I", line(0, -80, 0, -300, 25), "ground", "secondary", 60, 0, 5.0, None),
               Member("X", line(30, -100, 30, 100, 25), "ground", "secondary", 60, 0, 5.0, None)]
    net, systems = gs.find_systems(members, lambda n_: (flat, _fixed(n_)))
    assert len(systems) == 1
    sysm = systems[0]
    pcs = gs.pieces(net, sysm)
    smp = gs.sample(net, pcs)
    mid = [(i, si, j, sj) for i, si, j, sj in sysm["joins"] if 5.0 < si < net.L[i] - 5.0 or 5.0 < sj < net.L[j] - 5.0]
    assert mid
    for i, si, j, sj in mid:
        m, s_ = (i, si) if 5.0 < si < net.L[i] - 5.0 else (j, sj)
        have = [pc for pc, x in zip(pcs, smp) if pc["main"] == m]
        assert have and min(abs(float(a) - s_) for x in smp for a, mm in zip(x["ms"], x["mem"]) if int(mm) == m) < 1e-9
    assert all((np.diff(x["d"]) > 0).all() for x in smp)


def test_the_hook_gives_the_railways_the_bed_an_interchange_solved_under_a_part_and_nothing_elsewhere(monkeypatch):
    import grading
    from test_roads import FRAME
    xy = np.stack([np.linspace(0.0, 20.0, 11), np.zeros(11)], axis=1)

    class Planner:
        frame = FRAME

        def _shape_at(self, line, pts, fid, modes):
            assert modes == ("track",)
            z = np.where(np.asarray(pts)[:, 0] < 10.0, 12.5, np.nan) if fid == "rail:T" else np.full(len(pts), np.nan)
            return z, [None] * len(z)

    class Store:
        def planners_of(self, lat, lon):
            return [(Planner(), None)]
    monkeypatch.setattr(grading, "_ACTIVE_STORE", Store())
    monkeypatch.setattr(grading, "SHAPES_ON", True)
    got = grading._track_z_hook(FRAME, [("T", "standard_gauge", "ground", xy), ("U", "standard_gauge", "ground", xy)])
    assert got[1] is None                                        # (no solved profile of U: the railways' own bed)
    assert np.allclose(got[0][xy[:, 0] < 10.0], 12.5, atol=1.0) and np.isnan(got[0][xy[:, 0] >= 10.0]).all()
    monkeypatch.setattr(grading, "SHAPES_ON", False)
    assert grading._track_z_hook(FRAME, [("T", "standard_gauge", "ground", xy)]) is None
