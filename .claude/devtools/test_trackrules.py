"""Synthetic cases for trackrules.py: python -m pytest .claude/devtools/test_trackrules.py"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trackrules as tr

# a straight track along +x, 12 m wide
LINE = [np.array([[-500.0, 0.0], [500.0, 0.0]])]
HALF = [6.0]
SL, SD = 88.4, 28.2


def test_stand_facing_track_passes_and_180_fails():
    # a stand south of the track, open front (+y at yaw 0) toward it
    good, _ = tr.stand_check([(0.0, -40.0, 0.0, 1.0)], LINE, HALF, [], SL, SD, 4.0, 0.6)
    assert good[0]["faces"] > 0.9 and not good[0]["faults"]
    bad, _ = tr.stand_check([(0.0, -40.0, 180.0, 1.0)], LINE, HALF, [], SL, SD, 4.0, 0.6)
    assert "away" in bad[0]["faults"]


def test_stand_on_band_fails():
    s, _ = tr.stand_check([(0.0, -20.0, 0.0, 1.0)], LINE, HALF, [], SL, SD, 4.0, 0.6)
    assert "band" in s[0]["faults"]


def test_stand_cover_and_off_footprint():
    fp = np.array([[-44.2, -54.1], [44.2, -54.1], [44.2, -25.9], [-44.2, -25.9]])
    s, cov = tr.stand_check([(0.0, -40.0, 0.0, 1.0)], LINE, HALF, [fp], SL, SD, 4.0, 0.6)
    assert not s[0]["faults"] and cov[0] > 0.9
    s, cov = tr.stand_check([(300.0, -40.0, 0.0, 1.0)], LINE, HALF, [fp], SL, SD, 4.0, 0.6)
    assert "off" in s[0]["faults"] and cov[0] < 0.05


def test_props_on_band():
    rows = [["pole", 0.0, 3.0], ["pole", 0.0, 9.0], ["pole", 0.0, 11.0], ["guard", 0.0, 0.0]]
    hit = tr.props_on_band(rows, LINE, HALF, 4.0, skip_groups=("guard",))
    assert [h[2] for h in hit] == [3.0, 9.0]


def test_building_over_oval_and_beside():
    th = np.linspace(0, 2 * np.pi, 200)
    oval = [np.c_[200 * np.cos(th), 100 * np.sin(th)]]
    lid = np.array([[-300.0, -200], [300, -200], [300, 200], [-300, 200]])
    on, under = tr.building_on_band(lid, oval, [8.0], 4.0)
    assert under > 900 and on == 0
    infield = np.array([[-50.0, -20], [50, -20], [50, 20], [-50, 20]])
    assert tr.building_on_band(infield, oval, [8.0], 4.0) == (0, 0)
    # an infield building with its hole (inner ring) round the oval would be pieces: one piece touching the band
    edge = np.array([[-20.0, 85], [20, 85], [20, 95], [-20, 95]])
    assert tr.building_on_band(edge, oval, [8.0], 4.0)[0] > 0


def test_join_step_and_twist():
    flat = lambda P: np.zeros(len(P))
    assert tr.join_profile(flat, [0, 0], [1, 0], 6.0) == (0.0, 0.0)
    step = lambda P: np.where(P[:, 0] > 0, 0.294, 0.0)
    assert tr.join_profile(step, [0, 0], [1, 0], 6.0)[0] > 0.25
    # bank 0 before, 10 % after: 0.6 m edge twist over a 6 m half width
    tw = lambda P: np.where(P[:, 0] > 0, 0.1 * P[:, 1], 0.0)
    assert abs(tr.join_profile(tw, [0, 0], [1, 0], 6.0)[1] - 0.6) < 0.01
    # a steady grade is no step
    grade = lambda P: 0.05 * P[:, 0]
    assert tr.join_profile(grade, [0, 0], [1, 0], 6.0)[0] < 1e-6


def test_joins_found():
    th = np.linspace(0, 2 * np.pi, 100)
    oval = np.c_[200 * np.cos(th), 100 * np.sin(th)]
    pit = np.array([[200.0, 0.0], [150.0, -50.0]])
    j = tr.joins({"o": oval, "p": pit})
    assert ("o", "seam") in [(f, k) for f, _, _, k in j] and ("p", "join") in [(f, k) for f, _, _, k in j]


def test_barrier_share_and_severity():
    rows = [["w", x, 8.0] for x in np.arange(-500, 0, 2.0)]
    assert abs(tr.barrier_share(LINE[0], rows, 6.0, 4.0) - 0.5) < 0.02
    assert tr.severity({"stand_away": 1}) == 50


def test_main_pieces_circuit_and_pit():
    th = np.linspace(0, 2 * np.pi, 100)
    oval = np.c_[200 * np.cos(th), 100 * np.sin(th)]
    # a pit lane from one point of the oval to another, inside it (shorter than the arc it bypasses)
    a, b = oval[10], oval[90]
    pit = np.array([a, (a + b) / 2 * 0.9, b])
    m = tr.main_pieces({"o": oval, "p": pit})
    assert m == {"o": True, "p": False}
    strip = np.array([[0.0, 0.0], [400.0, 0.0]])
    road = np.array([[200.0, 0.0], [200.0, -50.0]])
    assert tr.main_pieces({"s": strip, "r": road}) == {"s": True, "r": False}


def test_main_pieces_pit_apart_from_circuit():
    th = np.linspace(0, 2 * np.pi, 100)
    oval = np.c_[200 * np.cos(th), 100 * np.sin(th)]
    pit = np.array([[-100.0, -80.0], [100.0, -80.0]])          # 20 m inside, never joined
    far = np.array([[0.0, 600.0], [400.0, 600.0]])             # a drag strip of its own
    m = tr.main_pieces({"o": oval, "p": pit, "d": far}, beside_m=40.0)
    assert m == {"o": True, "p": False, "d": True}


def test_main_pieces_pit_beside_straight():
    straight = np.array([[-300.0, 0.0], [300.0, 0.0]])
    loop = np.array([[300.0, 0.0], [300.0, 200.0], [-300.0, 200.0], [-300.0, 0.0]])
    pit = np.array([[-200.0, -15.0], [200.0, -15.0]])
    m = tr.main_pieces({"s": straight, "l": loop, "p": pit}, beside_m=40.0)
    assert m["s"] and m["l"] and not m["p"]
    m = tr.main_pieces({"s": straight, "l": loop, "p": pit}, beside_m=40.0, pits=("s",))
    assert not m["s"]


def test_join_twist_nets_out_a_steady_bank_change():
    # (a bank easing in at 0.0025 / m through the join: no twist; the same with a 0.03 jump at the join: 0.18 m)
    ramp = lambda P: 0.0025 * P[:, 0] * P[:, 1]             # noqa: E731  (bank = 0.0025 x, across +y)
    _st, tw = tr.join_profile(ramp, (0.0, 0.0), (1.0, 0.0), 6.0)
    assert tw < 0.01
    jump = lambda P: (0.0025 * P[:, 0] + 0.03 * (P[:, 0] > 0)) * P[:, 1]     # noqa: E731
    _st, tw = tr.join_profile(jump, (0.0, 0.0), (1.0, 0.0), 6.0)
    assert abs(tw - 0.18) < 0.01
