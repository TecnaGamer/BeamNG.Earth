"""server/evals/earth_view.py's reference vehicles: trucks along the roads, wagons along the railways, a truck sized to a height limit."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import earth_view as ev  # noqa: E402


def strip(x0, x1, y0, y1, z=0.0, step=1.0):
    """A flat strip of 1 m quads (two triangles each) from x0 to x1, y0 to y1."""
    out = []
    for x in np.arange(x0, x1, step):
        for y in np.arange(y0, y1, step):
            a, b, c, d = (x, y, z), (x + step, y, z), (x + step, y + step, z), (x, y + step, z)
            out += [(a, b, c), (a, c, d)]
    return np.array(out, np.float64)


def boxes(tris):
    return tris.reshape(-1, 12, 3, 3)


def test_a_box_stands_on_its_base_along_its_axis():
    t = ev.box_triangles((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), 12.0, 2.5, 4.0)
    assert t.shape == (12, 3, 3)
    assert np.isclose(t[..., 2].min(), 1.0) and np.isclose(t[..., 2].max(), 5.0)
    assert np.isclose(np.ptp(t[..., 1]), 12.0) and np.isclose(np.ptp(t[..., 0]), 2.5)


def test_trucks_along_a_road_every_truck_spacing_in_the_middle_heading_along_it():
    road = strip(0, 100, -3.5, 3.5)
    t, c = ev.reference_vehicles(road, np.full(len(road), ev.ROAD, np.int8))
    b = boxes(t)
    assert (c == ev.TRUCK).all() and 4 <= len(b) <= 6
    mid = b.mean(axis=(1, 2))
    assert np.all(np.abs(mid[:, 1]) < 0.5)                                   # in the middle of the road
    assert np.all(np.isclose(np.ptp(b[..., 0], axis=(1, 2)), ev.TRUCK_LWH[0], atol=0.3))     # along x
    xs = np.sort(mid[:, 0])
    assert np.all(np.diff(xs) >= ev.TRUCK_EVERY_M * 0.75)
    assert np.allclose(b[..., 2].max(axis=(1, 2)), ev.TRUCK_LWH[2])


def test_a_road_on_two_levels_gets_trucks_on_both():
    road = np.concatenate([strip(0, 40, -3.5, 3.5, 0.0), strip(14, 21, -20, 20, 7.0)[:, ::-1][:, :, [1, 0, 2]]])     # (x, y swapped: wound back up)
    cls = np.full(len(road), ev.ROAD, np.int8)
    t, c = ev.reference_vehicles(road, cls)
    bottoms = boxes(t)[..., 2].min(axis=(1, 2))
    assert (np.isclose(bottoms, 0.0)).any() and (np.isclose(bottoms, 7.0)).any()


def test_wagons_on_the_rails_heads_and_none_on_a_rail_deck():
    rail = strip(0, 60, -1.5, 1.5, 0.5)[:, ::-1]                      # (no road to read the winding from: Earth's)
    deck = strip(0, 60, -3.0, 3.0, 0.0)[:, ::-1]
    tris = np.concatenate([rail, deck])
    cls = np.array([ev.RAIL] * len(rail) + [ev.DECK] * len(deck), np.int8)
    t, c = ev.reference_vehicles(tris, cls)
    assert (c == ev.WAGON).all() and len(boxes(t)) >= 2                      # (no truck on the track's deck)
    assert np.allclose(boxes(t)[..., 2].min(axis=(1, 2)), 0.5)


def test_walls_do_not_turn_a_wagon_across_its_track():
    rail = strip(0, 60, -1.5, 1.5, 0.5)[:, ::-1]                      # (Earth's winding)
    wall = np.array([[(30, -6, 0), (30, 6, 0), (30, 6, 5)], [(30, -6, 0), (30, 6, 5), (30, -6, 5)]] * 20, np.float64)
    tris = np.concatenate([rail, wall])
    cls = np.full(len(tris), ev.RAIL, np.int8)
    b = boxes(ev.reference_vehicles(tris, cls)[0])
    assert len(b) >= 2 and np.all(np.ptp(b[..., 0], axis=(1, 2)) > np.ptp(b[..., 1], axis=(1, 2)))


def test_a_signed_road_gets_a_vehicle_as_high_as_the_sign_and_a_pickup_under_2_5_m():
    road = strip(0, 30, -3.5, 3.5)
    cls = np.full(len(road), ev.ROAD, np.int8)
    line = np.array([[-5.0, 0.0], [40.0, 0.0]])
    for h, length in ((3.3, ev.TRUCK_LWH[0]), (1.9, ev.PICKUP_LWH[0])):
        b = boxes(ev.reference_vehicles(road, cls, [(line, h)])[0])
        assert np.allclose(b[..., 2].max(axis=(1, 2)), h)
        assert np.allclose(np.ptp(b[..., 0], axis=(1, 2)), length, atol=0.3)
    far = np.array([[-5.0, 50.0], [40.0, 50.0]])
    b = boxes(ev.reference_vehicles(road, cls, [(far, 1.9)])[0])
    assert np.allclose(b[..., 2].max(axis=(1, 2)), ev.TRUCK_LWH[2])         # (a sign elsewhere)


def slab(x0, x1, y0, y1, z0, z1):
    """A closed box (a deck), its faces wound outward the way strip() winds its top (up = +z)."""
    top = strip(x0, x1, y0, y1, z1)
    bottom = strip(x0, x1, y0, y1, z0)[:, ::-1]                       # (reversed: facing down)
    return np.concatenate([top, bottom])


def test_a_closed_deck_gets_one_row_of_trucks_on_its_top_not_one_hanging_from_its_underside():
    road = strip(-30, 0, -3.5, 3.5, 6.0)
    deck = slab(0, 60, -4.0, 4.0, 3.9, 6.0)                           # (2.1 m deep, as Earth's)
    tris = np.concatenate([road, deck])
    cls = np.array([ev.ROAD] * len(road) + [ev.DECK] * len(deck), np.int8)
    b = boxes(ev.reference_vehicles(tris, cls)[0])
    assert np.allclose(b[..., 2].min(axis=(1, 2)), 6.0)              # every truck on the top


def test_the_winding_up_is_read_from_the_roads_either_way():
    road = strip(0, 30, -3.5, 3.5)
    cls = np.full(len(road), ev.ROAD, np.int8)
    assert ev.up_sign(road, cls) == 1.0 and ev.up_sign(road[:, ::-1], cls) == -1.0
    assert ev.up_sign(road, np.full(len(road), ev.RAIL, np.int8)) == ev.EARTH_UP_SIGN
    flipped = road[:, ::-1]                                           # (Earth's winding: trucks still on it)
    assert len(boxes(ev.reference_vehicles(flipped, cls)[0])) >= 1


def test_limit_at_takes_the_lowest_near():
    lines = [(np.array([[0.0, 0.0], [10.0, 0.0]]), 3.3), (np.array([[0.0, 2.0], [10.0, 2.0]]), 2.5),
             (np.array([[0.0, 50.0], [10.0, 50.0]]), 1.0)]
    assert ev.limit_at(lines, (5.0, 1.0)) == 2.5 and ev.limit_at(lines, (5.0, 20.0)) is None


# ---- along Earth's map lines -------------------------------------------------------------------------------------------------
def test_surfaces_give_every_height_under_a_point():
    tris = np.concatenate([strip(0, 10, 0, 10, 1.0), strip(0, 10, 0, 10, 7.5)])
    s = ev.Surfaces(tris)
    assert np.allclose(s.heights(5.2, 5.7), [1.0, 7.5]) and not len(s.heights(20.0, 5.0))
    assert ev.levels(np.array([1.0, 1.4, 7.5])) == [1.2, 7.5] and ev.levels(np.array([1.0, 1.4]), top=True) == [1.4]


def test_along_spaces_points_from_half_a_step():
    pts = ev.along(np.array([[0.0, 0.0], [100.0, 0.0]]), 20.0)
    assert [round(float(p[0]), 6) for p, _t, _f in pts] == [10.0, 30.0, 50.0, 70.0, 90.0]
    assert np.allclose(pts[0][1], [1.0, 0.0]) and abs(pts[2][2] - 0.5) < 1e-9


def scene_road_under_deck():
    ground_road = strip(-60, 60, -3.5, 3.5, 0.0)                                       # (along x)
    deck = strip(-4, 4, -40, 40, 7.0)                                                  # (along y, over it)
    deck_road = strip(-4, 4, -60, -40, 0.0)                                            # (its approaches, simplified flat)
    tris = np.concatenate([ground_road, deck, deck_road])
    cls = np.array([ev.ROAD] * len(ground_road) + [ev.DECK] * len(deck) + [ev.ROAD] * len(deck_road), np.int8)
    return tris, cls


def test_trucks_follow_their_own_line_on_both_levels():
    tris, cls = scene_road_under_deck()
    lines = {"road": [(np.array([[-60.0, 0.0], [60.0, 0.0]]), []),                      # the road under: ground
                      (np.array([[0.0, -40.0], [0.0, 40.0]]), [(0.0, 1.0, "bridge")])],   # the deck over it
             "rail": []}
    t, c = ev.reference_vehicles(tris, cls, lines=lines, ground=lambda x, y: 0.0)
    b = boxes(t[c == ev.TRUCK])
    mid, bottom = b.mean(axis=(1, 2)), b[..., 2].min(axis=(1, 2))
    under = np.abs(mid[:, 1]) < 1.0
    over = np.abs(mid[:, 0]) < 1.0
    assert under.sum() >= 4 and np.allclose(bottom[under & ~over], 0.0)                 # the ground road's on the ground ...
    assert over.sum() >= 3 and np.allclose(bottom[over & ~under], 7.0)                  # ... the deck's on the deck
    assert np.all(np.ptp(b[under & ~over][..., 0], axis=(1, 2)) > 11)                  # heading along x
    assert np.all(np.ptp(b[over & ~under][..., 1], axis=(1, 2)) > 11)                  # heading along y


def test_a_ground_road_stands_on_the_terrain_and_an_under_road_never_on_the_overpass():
    """Earth paints ground roads into the terrain: the road under a deck has only its edges as meshes. Its trucks stand on
    the terrain even where the terrain under the deck reads high (an approach's fill), never on the deck over it."""
    tris, cls = scene_road_under_deck()
    lines = {"road": [(np.array([[-60.0, 0.0], [60.0, 0.0]]), [])], "rail": []}
    fill = lambda x, y: 6.5 if abs(x) < 6 else 0.0                                      # noqa: E731 (the deck's fill)
    deck = cls == ev.DECK
    ground = strip(-60, 60, -20, 20, 0.0)                                                # (the view: the ground's extent)
    tris = np.concatenate([tris[deck], ground])
    cls = np.array([ev.DECK] * int(deck.sum()) + [ev.GROUND] * len(ground), np.int8)
    b = boxes(ev.reference_vehicles(tris, cls, lines=lines, ground=fill)[0])
    assert len(b) >= 5 and np.all(b[..., 2].min(axis=(1, 2)) < 6.6)                   # (never on the deck at 7)


def test_a_bridge_with_no_deck_drawn_gets_red_posts_and_a_ground_road_none():
    road = strip(0, 100, -3.5, 3.5, 0.2)
    deck = np.concatenate([strip(0, 40, -3.5, 3.5, 6.0), strip(70, 100, -3.5, 3.5, 6.0)])     # (a 30 m hole)
    tris = np.concatenate([deck, road])
    cls = np.array([ev.DECK] * len(deck) + [ev.ROAD] * len(road), np.int8)
    bridge = {"road": [(np.array([[0.0, 0.0], [100.0, 0.0]]), [(0.0, 1.0, "bridge")])], "rail": []}
    t, c = ev.reference_vehicles(tris, cls, lines=bridge, ground=lambda x, y: 0.0)
    posts = boxes(t[c == ev.MISSING]).mean(axis=(1, 2))
    assert len(posts) == 6 and np.all((posts[:, 0] > 40) & (posts[:, 0] < 70))
    ground_road = {"road": [(np.array([[0.0, 0.0], [100.0, 0.0]]), [])], "rail": []}
    t, c = ev.reference_vehicles(tris, cls, lines=ground_road, ground=lambda x, y: 0.0)
    assert not (c == ev.MISSING).any()                                                   # (painted into the terrain)


def test_a_truck_stands_at_every_crossing_under_a_bridge_or_a_railway():
    ground_road = strip(-60, 60, -3.5, 3.5, 0.0)
    deck = strip(-4, 4, -40, 40, 7.0)
    ground = strip(-60, 60, -40, 40, 0.0)
    tris = np.concatenate([ground_road, deck, ground])
    cls = np.array([ev.ROAD] * len(ground_road) + [ev.DECK] * len(deck) + [ev.GROUND] * len(ground), np.int8)
    under = np.array([[-60.0, 0.0], [60.0, 0.0]])
    lines = {"road": [(under, []), (np.array([[0.0, -40.0], [0.0, 40.0]]), [(0.0, 1.0, "bridge")])], "rail": []}
    b = boxes(ev.reference_vehicles(tris, cls, lines=lines, ground=lambda x, y: 0.0)[0])
    mid, bottom = b.mean(axis=(1, 2)), b[..., 2].min(axis=(1, 2))
    at = (np.hypot(mid[:, 0], mid[:, 1]) < 0.5) & np.isclose(bottom, 0.0)
    assert at.sum() == 1                                                                 # the probe, under the deck
    assert ev.crossings_under(under, [(np.array([[0.0, -40.0], [0.0, 40.0]]), [], False)]) == []    # (a ground road: no)
    assert ev.crossings_under(under, [(np.array([[0.0, -40.0], [0.0, 40.0]]), [], True)]) == [0.5]  # (a railway: yes)


def test_a_short_tunnel_is_open_ground_a_long_one_is_not():
    assert ev._kind([(0.4, 0.5, "tunnel")], 0.45, 100.0) is None
    assert ev._kind([(0.0, 0.8, "tunnel")], 0.45, 100.0) == "tunnel"

# ---- a wagon on its rails' heads, lying along its chord ------------------------------------------------------------------------
def rect(x0, x1, y0, y1, z):
    """One flat rectangle (two triangles), facing up."""
    a, b, c, d = (x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)
    return np.array([(a, b, c), (a, c, d)], np.float64)


def test_a_wagon_stands_on_its_rails_heads_not_on_the_sleepers_or_ballast_between_them():
    """Earth's track: ballast, rails 0.16 over the sleepers, their heads 0.07 wide at +-0.7525 m. The point on the line itself
    reads the ballast (or a sleeper's top), 0.28 m under the heads a wagon's wheels ride on: a deck's wall 0.42 m over the head
    (Birmingham's, Perth's freeway deck) then stood in a body that starts 0.5 m over the wheels (earth_checks.BODY_BASE_M)."""
    import earth_checks
    track = np.concatenate([rect(0, 80, -1.8, 1.8, 0.0), rect(0, 80, 0.7175, 0.7875, 0.28), rect(0, 80, -0.7875, -0.7175, 0.28)])
    wall = rect(0, 80, 1.4, 2.4, 0.28 + 0.42)                         # (the neighbour's deck wall's top: 0.42 over the heads)
    line = np.array([[0.0, 0.0], [80.0, 0.0]])
    (base, axis), = ev.vehicles_on_lines([(line, [])], ev.Surfaces(track), 80.0, top=True, painted=False, half=ev.WAGON_LWH[0] / 2)
    assert abs(base[2] - 0.28) < 0.01, base
    wagon = ev.box_triangles(base, axis, *ev.WAGON_LWH)
    assert not earth_checks.vehicle_hits(wagon, wall)[0]


def test_a_wagon_lies_along_its_chord_not_the_slope_of_the_3_m_round_it():
    """A 0.25 m step in the track over 6 m (a deck's end, a dip) is +-4 % within 3 m of it, 1.25 % over the wagon's 20 m."""
    quads = []
    for x in np.arange(0.0, 80.0, 0.5):
        z0, z1 = [0.28 + 0.25 * np.clip((v - 37.0) / 6.0, 0.0, 1.0) for v in (x, x + 0.5)]
        a, b, c, d = (x, -1.8, z0), (x + 0.5, -1.8, z1), (x + 0.5, 1.8, z1), (x, 1.8, z0)
        quads += [(a, b, c), (a, c, d)]
    line = np.array([[0.0, 0.0], [80.0, 0.0]])
    (base, axis), = ev.vehicles_on_lines([(line, [])], ev.Surfaces(np.array(quads, np.float64)), 80.0, top=True, painted=False,
                                         half=ev.WAGON_LWH[0] / 2)
    assert abs(axis[2]) < 0.02, axis


def test_a_road_under_a_stack_keeps_its_level_not_the_decks_over_it():
    # A road at 0 m with a deck 8 m over it at the 6 middle points, the preferred level the highest: the chain stays at the
    # road's level instead of jumping 8 m up for two points (a truck stood in the deck over its road).
    cands = [[0.0]] + [[0.0, 8.0]] * 6 + [[0.0]]
    prefs = [None] + [8.0] * 6 + [None]
    assert ev._choose(cands, prefs) == [0.0] * 8


def test_a_truck_at_the_scenes_edge_lies_on_its_ramp_not_level():
    """The view's box cuts a 10 % ramp 1.5 m past a truck: its slope from the side the scene has, not flat (held level, a
    12 m truck stood 0.09 m into its own deck at the box's edge)."""
    quads = []
    for x in np.arange(0.0, 31.5, 0.5):
        z0, z1 = 0.1 * x, 0.1 * (x + 0.5)
        a, b, c, d = (x, -3.0, z0), (x + 0.5, -3.0, z1), (x + 0.5, 3.0, z1), (x, 3.0, z0)
        quads += [(a, b, c), (a, c, d)]
    got = ev.vehicles_on_lines([(np.array([[0.0, 0.0], [60.0, 0.0]]), [])], ev.Surfaces(np.array(quads)), 20.0,
                               painted=False)
    edge = [axis for base, axis in got if abs(base[0] - 30.0) < 0.5]
    assert edge and abs(edge[0][2] - 0.1) < 0.005, got
