"""grade3d.ground: the elevation at every sample in one call, holes filled along the path, each path smoothed with its
ends kept."""
import numpy as np

import linenet
from gradeshapes import Member
from grade3d import ground, network, samples


def _m(key, line):
    return Member(key, np.asarray(line, np.float64), "ground", "secondary", 50, 0.0, 4.0, None, None)


def setup_function(_f):
    linenet.reset()


def _sm():
    net = network.build([_m("a", [[0, 0], [400, 0]]), _m("b", [[0, 100], [400, 100]])])
    return samples.sample(net)


def test_one_call_and_holes_filled():
    sm = _sm()
    calls = []

    def elev(xy):
        calls.append(len(xy))
        z = xy[:, 0] * 0.05
        z[(xy[:, 0] > 100) & (xy[:, 0] < 120)] = np.nan
        return z
    raw = ground.read(elev, sm)
    assert calls == [len(sm)] and np.isfinite(raw).all()
    assert np.allclose(raw, sm.xy[:, 0] * 0.05, atol=1e-9)


def test_a_spike_is_taken_out_and_the_ends_kept():
    sm = _sm()
    raw = ground.read(lambda xy: np.where(np.abs(xy[:, 0] - 200) < 3, 30.0, xy[:, 0] * 0.02), sm)
    g = ground.smooth(sm, raw)
    want = sm.xy[:, 0] * 0.02
    assert np.abs(g - want).max() < 0.2           # (the 30 m spike, a car on the DEM, gone)
    for p in range(2):
        sl = sm.of(p)
        assert abs(g[sl][0] - raw[sl][0]) < 1e-9 and abs(g[sl][-1] - raw[sl][-1]) < 1e-9
