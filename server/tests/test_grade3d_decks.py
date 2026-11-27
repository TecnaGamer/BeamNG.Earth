"""grade3d.decks: a road on the ground over a tube is a tunnel by default, a deck over the tube where the terrain has no
room for that: the tube's opening within MOUTH_M of its edge, or the cover over the vault under SOIL_M."""
import numpy as np

import linenet
from gradeshapes import Member
from grade3d import decks, ground, laws, layers, network, problem, samples, solve


def _line(x0, y0, x1, y1, n=11):
    return np.stack([np.linspace(x0, x1, n), np.linspace(y0, y1, n)], axis=1)


def _m(key, line, kind="ground", cls="secondary", level=0.0):
    return Member(key, np.asarray(line, np.float64), kind, cls, None, level, 4.0, None, None)


def setup_function(_f):
    linenet.reset()


def _decks(ms, elev):
    net = network.build(ms)
    sm = samples.sample(net, layers.order(net))
    g = ground.smooth(sm, ground.read(elev, sm))
    pr = problem.pose(net, sm, laws.lay(net, sm, g))
    z = solve.solve(pr).z
    return decks.find(net, sm, pr, z)


def _tube(x0, x1, portal_w, portal_e):
    """A road along x from x0 to x1, in its tunnel between portal_w and portal_e (its approaches on the ground)."""
    return [_m("app_w", _line(x0, 0, portal_w, 0)), _m("tube", _line(portal_w, 0, portal_e, 0), "tunnel", level=-1.0),
            _m("app_e", _line(portal_e, 0, x1, 0))]


def test_a_street_by_the_portal_is_a_deck():
    # (the street 3 m from the west portal: no room for the opening between them)
    ms = _tube(-600, 600, -10, 300) + [_m("street", _line(-3, -200, -3, 200))]
    got = _decks(ms, lambda xy: np.full(len(xy), 10.0))
    (fid, a, b), = got
    assert fid == "street"
    ys = sorted([a[1], b[1]])
    assert ys[0] < -4.0 and ys[1] > 4.0 and abs(a[0] + 3) < 1e-6 and abs(b[0] + 3) < 1e-6


def test_a_street_over_the_middle_of_a_deep_tube_is_a_tunnel():
    # (a hill 15 m over the tube's line: the street over it has the cover, the portals far)
    def hill(xy):
        return 10.0 + 15.0 * np.clip(1.0 - np.abs(xy[:, 0]) / 250.0, 0.0, 1.0)
    ms = _tube(-600, 600, -250, 250) + [_m("street", _line(0, -200, 0, 200))]
    assert _decks(ms, hill) == []


def test_a_thin_cover_is_a_deck():
    # (flat ground: the street held over the tube by the clearance alone, no soil over its vault)
    ms = _tube(-600, 600, -250, 250) + [_m("street", _line(0, -200, 0, 200))]
    (fid, _a, _b), = _decks(ms, lambda xy: np.full(len(xy), 10.0))
    assert fid == "street"
