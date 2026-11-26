"""Step 4 reads the heights of the members the shape does not move (ix/s4_terrain.py): at exactly the arcs the problem asks,
once, and the heights step is made from what it read."""
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import gradeshapes as gs  # noqa: E402
from gradeshapes import Member  # noqa: E402
from ix import artifacts, s4_terrain  # noqa: E402


def line(x0, y0, x1, y1, n=13):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def slope(xy):
    return 0.05 * np.asarray(xy)[:, 0] + 0.02 * np.asarray(xy)[:, 1]


def fixed_of(net, calls):
    """fixed_z(i, s) reading slope along member i, counting what it was asked."""
    def f(i, s):
        sv = np.atleast_1d(np.asarray(s, np.float64))
        calls.append((i, sv.tolist()))
        xy = np.stack([np.interp(sv, net.s[i], net.lines[i][:, 0]), np.interp(sv, net.s[i], net.lines[i][:, 1])], axis=1)
        z = slope(xy)
        return z if np.ndim(s) else float(z[0])
    return f


def system():
    """A deck over a road on the ground (a deck over a road is raised: the road is read, not moved)."""
    members = [Member("D", line(-60, 0, 60, 0, 25), "deck", "secondary", 60, 1, 5.0, None),
               Member("R", line(0, -60, 0, 60, 25), "ground", "secondary", 60, 0, 5.0, None)]
    net, systems = gs.find_systems(members, lambda n_: (lambda xy: slope(xy), fixed_of(n_, [])))
    assert len(systems) == 1
    sysm = systems[0]
    pcs = gs.pieces(net, sysm)
    return net, sysm, pcs, gs.sample(net, pcs)


def test_the_reads_are_the_arcs_the_problem_asks_of_the_road_it_does_not_move():
    net, sysm, pcs, smp = system()
    reads = gs.fixed_reads(sysm, pcs, smp)
    road = [i for i, m in enumerate(net.m) if m.key[0] == "R"][0]
    assert list(reads) == [road] and len(reads[road]) > 3
    assert (np.diff(reads[road]) > 0).all()                    # (ascending, once each)
    asked = []
    gs.shape_problem(net, sysm, pcs, smp, [slope(x["xy"]) for x in smp], fixed_of(net, asked))
    assert sorted({s_ for i, sv in asked for s_ in sv}) == reads[road].tolist()


def test_the_problem_made_from_what_terrain_read_is_the_problem_made_from_the_planner():
    net, sysm, pcs, smp = system()
    table = s4_terrain.read_fixed(net, sysm, pcs, smp, fixed_of(net, []))
    ground = [slope(x["xy"]) for x in smp]
    a_chains, a_cps = gs.shape_problem(net, sysm, pcs, smp, ground, fixed_of(net, []))
    b_chains, b_cps = gs.shape_problem(net, sysm, pcs, smp, ground, s4_terrain.lookup(table))
    assert a_cps == b_cps
    for a, b in zip(a_chains, b_chains):
        assert all(np.array_equal(a[k], b[k]) for k in ("t", "w", "lo", "hi"))


def test_lookup_reads_nothing_again():
    table = {(3, 1.5): 7.0}
    f = s4_terrain.lookup(table)
    assert f(3, 1.5) == 7.0 and f(3, np.array([1.5])).tolist() == [7.0]
    try:
        f(3, 2.5)
    except KeyError:
        return
    raise AssertionError("an arc the terrain did not read was answered")


def test_the_artifact_holds_the_reads_as_the_paths_number_them():
    net, sysm, pcs, smp = system()
    loc = {g: n for n, g in enumerate(reversed(range(len(net.m))))}       # (the Paths' numbers: not net's)
    art, ground, table = s4_terrain.run(_Planner(), net, sysm, pcs, smp, loc, fixed_of(net, []), "sys", "system:sys")
    road = [i for i, m in enumerate(net.m) if m.key[0] == "R"][0]
    assert [f.member for f in art.fixed] == [loc[road]]
    f = art.fixed[0]
    assert len(f.s) == len(f.z) and np.isfinite(f.z).all()
    assert artifacts.load_bytes(artifacts.to_bytes(art)).fixed[0].z.tolist() == f.z.tolist() \
        if hasattr(artifacts, "load_bytes") else True


class _Planner:
    def _ground_line(self, xy):
        return slope(xy)
