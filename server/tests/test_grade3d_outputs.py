"""grade3d.outputs: the solve's heights as the build reads them (_Planner._shape_region's form), read back through the
grader's own _shape_at: every member's whole line has its height, decks as decks, tunnels as cuts, tracks as tracks."""
import numpy as np

import geodesy
import grading
import linenet
import railways
from gradeshapes import Member
from grade3d import ground, laws, layers, network, outputs, problem, samples, solve


def _line(x0, y0, x1, y1, n=11):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def _m(key, line, kind="ground", level=0.0, cls="secondary"):
    return Member(key, np.asarray(line, np.float64), kind, "rail" if kind.startswith("rail") else cls, None, level,
                  4.0, None, None)


def _rail(way, line, kind="rail"):
    line = np.asarray(line, np.float64)
    return Member(("rail", kind, way) + tuple(np.round(line[[0, -1]].ravel())), line, kind, "rail", None,
                  1.0 if kind == "rail_deck" else 0.0, 2.0, None, None)


class _Planner:
    """What _shape_at reads of a planner: its frame and the region's profiles."""

    def __init__(self, prof):
        self.frame = geodesy.EnuFrame(45.0, 7.0)
        self._prof = prof

    def _shape_region(self, _home):
        return self._prof


def setup_function(_f):
    linenet.reset()


def _solved(ms, elev=lambda xy: 10.0 + xy[:, 0] * 0.01):
    net = network.build(ms)
    lay = layers.order(net)
    sm = samples.sample(net, lay)
    g = ground.smooth(sm, ground.read(elev, sm))
    lw = laws.lay(net, sm, g)
    sol = solve.solve(problem.pose(net, sm, lw))
    return net, sm, sol.z, outputs.profiles(net, sm, sol.z)


def _at(prof, line, fid, modes):
    return grading._Planner._shape_at(_Planner(prof), line, line, fid, modes)


def test_every_members_whole_line_reads_its_height_in_its_mode():
    ms = [_m(("r1", "ground", "0"), _line(-400, 0, -100, 0)), _m(("r1", "deck", 1), _line(-100, 0, 100, 0), "deck", 1.0),
          _m(("r1", "ground", "1"), _line(100, 0, 400, 0)), _m(("r2", "ground", "0"), _line(0, -300, 0, 300)),
          _m(("r3", "tunnel", 0), _line(-200, 150, 200, 150, 21), "tunnel", -1.0),
          _rail("w9", _line(-300, -200, 300, -200, 31))]
    net, sm, z, prof = _solved(ms)
    assert set(prof) == {"r1", "r2", "r3", "rail:w9"}
    assert sorted(mode for _xy, _z, mode in prof["r1"]) == ["deck", "set", "set"]
    assert [mode for _xy, _z, mode in prof["r3"]] == ["cut"] and [m for _x, _z, m in prof["rail:w9"]] == ["track"]
    for m in ms:
        fid = outputs.fid_of(m)
        mode = outputs.MODE[m.kind]
        zz, modes = _at(prof, m.line, fid, (mode,))
        assert np.isfinite(zz).all(), (m.key, zz)
        assert set(modes) == {mode}
    # (the deck's height read back is the solve's, and it clears the road under it)
    deck_z, _ = _at(prof, np.array([[0.0, 0.0]]), "r1", ("deck",))
    road_z, _ = _at(prof, np.array([[0.0, 0.0]]), "r2", ("set",))
    assert deck_z[0] - road_z[0] > 4.0


def test_a_piece_runs_its_members_way_and_meets_the_next_at_one_height():
    ms = [_m(("a", "ground", "0"), _line(0, 0, 101, 0)), _m(("a", "deck", 1), _line(161, 0, 101, 0), "deck", 1.0),
          _m(("a", "ground", "1"), _line(161, 0, 301, 0))]
    net, _sm, _z, prof = _solved(ms)
    by = {mode: (xy, zz) for xy, zz, mode in prof["a"] if mode == "deck"}
    xy, _zz = by["deck"]
    assert np.allclose(xy[0], [161, 0]) and np.allclose(xy[-1], [101, 0])    # (the deck's own direction)
    ends = {}
    for xy, zz, _mode in prof["a"]:
        for p, h in ((xy[0], zz[0]), (xy[-1], zz[-1])):
            ends.setdefault(tuple(np.round(p, 6)), []).append(h)
    for hs in ends.values():
        assert np.ptp(hs) < 1e-9


def test_the_hooks_mode_for_each_kind():
    assert outputs.MODE == {"deck": "deck", "tunnel": "cut", "ground": "set", "rail": "track", "rail_deck": "track"}
    assert outputs.fid_of(_rail("w1", _line(0, 0, 10, 0))) == "rail:w1"
    assert outputs.fid_of(_m(("x~lanes1-2", "ground", "0"), _line(0, 0, 10, 0))) == "x~lanes1-2"
    assert railways.TRACK_Z_HOOK is None or callable(railways.TRACK_Z_HOOK)
