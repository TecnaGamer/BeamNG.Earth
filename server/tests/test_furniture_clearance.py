"""Consumer regressions for full, level-aware street-furniture collision clearance (no map downloads)."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import furniture  # noqa: E402
import roads  # noqa: E402
from test_furniture import road  # noqa: E402
from test_roads import FRAME, features, flat, rules  # noqa: E402


def row(group, x, y, z=0.0, yaw=0.0, height=None):
    return [group, x, y, z, yaw, 1.0,
            furniture.COLLISION_BOXES[group][0] if height is None else height, 0.0]


class ColliderClearanceTest(unittest.TestCase):
    def test_sign_face_not_just_pivot_and_yaw(self):
        clear = furniture.RoadClearance([(4.0, np.array([[-100., 0., 0.], [100., 0., 0.]]))])
        # Pivot 0.4 m off the pavement: the turned 0.46 m-wide face intrudes, the unturned face does not.
        self.assertTrue(clear.clear(row('stop', 0., 4.4)))
        self.assertFalse(clear.clear(row('stop', 0., 4.4, yaw=90.)))

    def test_rotated_tower_corner_not_just_pivot(self):
        clear = furniture.RoadClearance([(4.5, np.array([[-100., 0., 0.], [100., 0., 0.]]))])
        self.assertTrue(clear.clear(row('pylon', 0., 10.)))
        self.assertFalse(clear.clear(row('pylon', 0., 10., yaw=45.)))
        self.assertGreater(10., 4.5, 'the pivot is outside both carriageways')

    def test_native_scale_not_the_unused_row_scale(self):
        clear = furniture.RoadClearance([(4., np.array([[-100., 0., 0.], [100., 0., 0.]]))])
        small = row('stop', 0., 4.8, yaw=90.)
        large = row('stop', 0., 4.8, yaw=90., height=6.)
        small[5] = 1000.  # unused by trees.lua:M.pick
        self.assertTrue(clear.clear(small))
        self.assertFalse(clear.clear(large))

    def test_real_overhead_arm_clearance_and_higher_crossing(self):
        # Stock lamp collider's arm starts at z=7.124; its support is outside x=0..8 pavement.
        lamp = row('streetlight', -0.5, 0.)
        ground = [(4., np.array([[4., -100., 0.], [4., 100., 0.]]))]
        self.assertTrue(furniture.RoadClearance(ground).clear(lamp))
        raised = [(4., np.array([[4., -100., 2.], [4., 100., 2.]]))]
        self.assertFalse(furniture.RoadClearance(raised).clear(lamp))

    def test_grade_separated_objects_above_and_below(self):
        ground = [(4., np.array([[-100., 0., 0.], [100., 0., 0.]]))]
        self.assertTrue(furniture.RoadClearance(ground).clear(row('stop', 0., 0., z=10.)))
        self.assertFalse(furniture.RoadClearance(ground).clear(row('stop', 0., 0., z=6.)))
        deck = (np.array([[-100., 0., 50.], [100., 0., 50.]]), 4.)
        self.assertTrue(furniture.RoadClearance([], [deck]).clear(row('pylon', 0., 0.)))
        low = (np.array([[-100., 0., 20.], [100., 0., 20.]]), 4.)
        self.assertFalse(furniture.RoadClearance([], [low]).clear(row('pylon', 0., 0.)))

    def test_all_segments_not_just_the_nearest_branch(self):
        # Closest branch is 30 m overhead, second branch is at grade and clips the sign's face.
        line = np.array([[-100., 0., 30.], [100., 0., 30.], [100., 1., 0.], [-100., 1., 0.]])
        self.assertFalse(furniture.RoadClearance([(1., line)]).clear(row('stop', 0., 0.)))

    def test_rising_segment_clipped_at_collision_height(self):
        # Nearest XY point is too low to hit the hanging lamp; farther along the SAME segment is not.
        line = np.array([[-3., 0., 0.], [3., 0., 14.]])
        self.assertFalse(furniture.RoadClearance([(2., line)]).clear(row('polelamp', 0., 0., z=8.)))

    def test_segment_crossing_rectangle_without_any_endpoint_inside(self):
        a, b = np.array([[-10., 0.]]), np.array([[10., 0.]])
        d = furniture._box_distance2(a, b, np.array([-1., -1.]), np.array([1., 1.]))
        self.assertEqual(d.tolist(), [0.])

    def test_index_omits_distant_roads_but_finds_long_segments(self):
        lines = [(1., np.array([[1000.+i*32., -100., 0.], [1000.+i*32., 100., 0.]])) for i in range(1000)]
        lines.append((4., np.array([[-5000., 0., 0.], [5000., 0., 0.]])))
        clear = furniture.RoadClearance(lines)
        pick = clear.candidates(np.array([-2., -2.]), np.array([2., 2.]))
        self.assertEqual(pick.tolist(), [1000])
        self.assertFalse(clear.clear(row('stop', 0., 0.)))

    def test_catalog_covers_measured_stock_collision_bounds(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures' / 'furniture_collision_bounds.json').read_text())
        for group, asset in fixture['assets'].items():
            if group not in furniture.COLLISION_BOXES:
                continue
            boxes = furniture.COLLISION_BOXES[group][1]
            for source in asset['envelopes']:
                self.assertTrue(any(np.all(np.asarray(b[:3]) <= np.asarray(source[:3]) + 1e-9)
                                    and np.all(np.asarray(b[3:]) >= np.asarray(source[3:]) - 1e-9)
                                    for b in boxes), (group, source))


class PlacementClearanceTest(unittest.TestCase):
    def fixture(self):
        base = road('clearance-fixture', 'residential', True)
        side = 1 if furniture._h01(base[0], 'side') < .5 else -1
        walks = [{'fid': base[0], 'pts': roads.offset_polyline(base[2], side * 5.25),
                  'side': side, 'w': 2.5, 'caps': (False, False)}]
        box = (0., 0., 512., 256.)
        initial = furniture.place([base], walks, box)
        target = next(r for r in initial if r[0] == furniture.G_LAMP)
        crossing = ('crossing', roads.Spec(8., 'paved', False, 'service', False),
                    np.array([[target[1], -100., 0.], [target[1], 300., 0.]]), 0.)
        return base, walks, box, crossing, target

    def test_sidewalk_lamps_clear_crossing_service_road(self):
        base, walks, box, crossing, target = self.fixture()
        placed = furniture.place([base, crossing], walks, box)
        lamps = [r for r in placed if r[0] == furniture.G_LAMP]
        self.assertTrue(lamps, 'unobstructed lamps remain')
        self.assertNotIn(target, lamps, 'old sidewalk placement did not check any carriageway')
        self.assertTrue(all(abs(r[1] - target[1]) >= 4.15 for r in lamps))
        self.assertEqual(placed, furniture.place([crossing, base], walks, box), 'source order does not affect placement')

    def test_tile_ownership_does_not_change_clearance(self):
        base, walks, box, crossing, _ = self.fixture()
        whole = furniture.place([base, crossing], walks, box)
        split = (furniture.place([base, crossing], walks, (0., 0., 256., 256.))
                 + furniture.place([base, crossing], walks, (256., 0., 512., 256.)))
        self.assertEqual(sorted(whole), sorted(split))
        self.assertEqual(len({tuple(r) for r in split}), len(split), 'no duplicate owner at the seam')

    def test_own_hairpin_is_not_exempt(self):
        fid = 'hairpin'
        side = 1 if furniture._h01(fid, 'pside') < .5 else -1
        line = np.array([[-300., 100., 0.], [600., 100., 0.],
                         [600., 100.+side*10., 0.], [-300., 100.+side*10., 0.]])
        bent = (fid, roads.Spec(8., 'paved', False, 'tertiary', False), line, 0.)
        rows = furniture.place([bent], [], (0., 0., 256., 256.), wires=[])
        # These two verges face the 2 m gap between carriageways. A pole 2.4 m off either edge blocks the other.
        self.assertFalse(any(r[0] == furniture.G_POLE for r in rows), rows)
        self.assertTrue(any(r[0] == furniture.G_POLE for r in furniture.place(
            [road(fid, 'tertiary', False, y=100.)], [], (0., 0., 256., 256.))))

    def test_rejected_town_poles_do_not_leave_spans_attached_to_them(self):
        base, walks, box, _, _ = self.fixture()
        baseline_wires = []
        baseline = furniture.place([base], walks, box, town=True, wires=baseline_wires)
        target = next(r for r in baseline if r[0] == furniture.G_POLE)
        crossing = ('crossing', roads.Spec(8., 'paved', False, 'service', False),
                    np.array([[target[1], -100., 0.], [target[1], 300., 0.]]), 0.)
        wires = []
        rows = furniture.place([base, crossing], walks, box, town=True, wires=wires)
        self.assertNotIn(target, rows)
        self.assertTrue(wires)
        # The three wire arms can be offset in x/y; compare the removed pole's original attachment ends instead.
        bad = [w[0] for w in baseline_wires if abs(w[0][0] - target[1]) < 1.]
        for wire in wires:
            self.assertFalse(any(np.allclose(wire[0], p) or np.allclose(wire[-1], p) for p in bad))

    def test_mapped_width_not_road_class_default(self):
        f = features([[(-100., 0.), (100., 0.)]], ['residential'])
        f.attrs['osm_width'] = np.array([24.])
        spec = roads.road_specs(f)[0]
        self.assertEqual(spec.width, 24.)
        clear = furniture.RoadClearance([(spec.width / 2, np.array([[-100., 0., 0.], [100., 0., 0.]]))])
        self.assertFalse(clear.clear(row('stop', 0., 10.)))
        self.assertTrue(clear.clear(row('stop', 0., 13.)))

    def test_long_crossing_segment_is_not_culled_by_its_endpoints(self):
        own = (4., np.array([[0., -100.], [0., 100.]]))
        cross = (4., np.array([[-5000., 6.5], [5000., 6.5]]))
        q = furniture.signal_spot(np.zeros(2), np.array([0., 1.]), np.array([1., 0.]), 4., 4., [own, cross])
        self.assertIsNotNone(q, 'a reachable safe spot exists; blanket omission is not a fix')
        self.assertGreaterEqual(abs(q[1] - 6.5), 4.6)

    def test_relocated_junction_sign_uses_support_height_at_its_spot(self):
        spec = roads.Spec(8., 'paved', True, 'primary', False)
        rs = [('s', spec, np.array([[0., 0., 0.], [0., -100., -20.]]), 0.),
              ('e', spec, np.array([[0., 0., 0.], [100., 0., 0.]]), 0.),
              ('w', spec, np.array([[0., 0., 0.], [-100., 0., 0.]]), 0.)]
        rows = furniture.junction_props(rs, (-50., -50., 50., 50.), signals=np.zeros((1, 2)),
                                        ground=lambda x, y: .2*np.asarray(y))
        self.assertTrue(rows)
        for r in rows:
            self.assertAlmostEqual(r[3], .2*r[2]+.15, places=2)

    def test_true_overhead_wire_stays(self):
        deck = (np.array([[-100., 0., 8.], [100., 0., 8.]]), 5.5)
        high = np.array([[-20., 0., 20.], [20., 0., 20.]])
        low = np.array([[-20., 0., 7.9], [20., 0., 7.9]])
        wires = [high, low]
        furniture.under_decks([], [deck], wires)
        self.assertEqual(len(wires), 1)
        np.testing.assert_array_equal(wires[0], high)


class DevelopmentContextTest(unittest.TestCase):
    def test_segment_widths_survive_tile_clipping_without_a_feature_maximum(self):
        pts = np.array([[-1000., 0., 0.], [100., 0., 0.], [1000., 0., 0.]])
        clear = furniture.RoadClearance([(np.array([4., 8.]), pts)], bounds=(0., -20., 256., 20.))
        self.assertTrue(clear.clear(row('stop', 20., 5.)))
        self.assertFalse(clear.clear(row('stop', 180., 5.)))
        self.assertEqual(set(clear.half), {4., 8.})

    def test_every_stacked_deck_is_checked_but_real_overhead_clearance_stays(self):
        decks = [(np.array([[-100., 0., z], [100., 0., z]]), 5.5) for z in (8., 20.)]
        between = row('stop', 0., 0., z=10.)
        through_upper = row('stop', 0., 0., z=18.)
        upper_wire = np.array([[-20., 0., 20.], [20., 0., 20.]])
        high_wire = upper_wire + [0., 0., 10.]
        between_wire = upper_wire - [0., 0., 10.]
        wires = [upper_wire, high_wire, between_wire]
        self.assertEqual(furniture.under_decks([between, through_upper], decks, wires), [between])
        self.assertEqual(len(wires), 2)
        np.testing.assert_array_equal(wires[0], high_wire)
        np.testing.assert_array_equal(wires[1], between_wire)

    def test_owned_span_checks_its_next_support_beyond_the_tile_edge(self):
        fid = 'seam-support'
        phase = furniture._h01(fid, 'pphase') * furniture.POLE_SPACING
        start = 250. - phase - 5 * furniture.POLE_SPACING
        base = (fid, roads.Spec(8., 'paved', False, 'tertiary', False),
                np.array([[start, 100., 0.], [600., 100., 0.]]), 0.)
        crossing = ('next-support', roads.Spec(8., 'paved', False, 'service', False),
                    np.array([[295., -100., 0.], [295., 300., 0.]]), 0.)
        rs = [base, crossing]
        whole_wires, left_wires, right_wires = [], [], []
        whole = furniture.place(rs, [], (0., 0., 512., 256.), wires=whole_wires)
        split = (furniture.place(rs, [], (0., 0., 256., 256.), wires=left_wires)
                 + furniture.place(rs, [], (256., 0., 512., 256.), wires=right_wires))
        self.assertEqual(sorted(whole), sorted(split))
        self.assertTrue(whole_wires)
        self.assertEqual(sorted(tuple(w.ravel()) for w in whole_wires),
                         sorted(tuple(w.ravel()) for w in left_wires + right_wires))

    def test_build_tile_uses_narrowed_taper_instead_of_nominal_width(self):
        f = features([[(0., 50.), (64., 50.), (96., 50.), (112., 50.), (128., 50.)],
                      [(128., 50.), (256., 50.)]], ['primary', 'primary'])
        f.attrs['osm_width'] = np.array([12., 4.])
        context, rs = [], []
        roads.build_tile(FRAME, 0, 0, [f], lambda la, lo: np.zeros(np.shape(la)),
                         clearance_out=context, roads_out=rs)
        self.assertEqual(rs[0][1].width, 12., 'placement Specs and generated road geometry are not rewritten')
        self.assertTrue(context[0].clear(row('stop', 120., 55.)))
        self.assertFalse(furniture.RoadClearance([(6., rs[0][2])]).clear(row('stop', 120., 55.)))

    def test_narrowed_sidewalk_lamps_do_not_readd_the_nominal_carriageway(self):
        base, walks, box, _, _ = PlacementClearanceTest().fixture()
        fid, spec, pts, lon = base
        nominal = (fid, spec._replace(width=12.), pts, lon)
        actual = furniture.RoadClearance([(4., pts)], bounds=box)
        rows = furniture.place([nominal], walks, box, clearance=actual)
        self.assertTrue(any(r[0] == furniture.G_LAMP for r in rows))
        self.assertTrue(all(actual.clear(r) for r in rows if r[0] == furniture.G_LAMP))
        self.assertFalse(any(r[0] == furniture.G_LAMP for r in furniture.place([nominal], walks, box)))

    def test_painted_mean_taper_preserves_lamps_in_tile_and_graph_contexts(self):
        import navgraph
        f = features([[(0., 100.), (100., 100.)], [(100., 100.), (200., 100.)]],
                     ['primary', 'primary'], widths=[(0, 12.), (1, 4.)])
        urban = lambda la, lo: np.ones(np.shape(la), bool)
        elev = lambda la, lo: np.zeros(np.shape(la))
        tile, near = [], []
        roads.build_tile(FRAME, 0, 0, [f], elev, urban_fn=urban, clearance_out=tile, roads_out=near)
        fid, spec, pts, lon = near[0]
        self.assertEqual(spec.width, 12.)
        self.assertAlmostEqual(tile[0].half[0], 5., delta=.02, msg='endpoint widths 12 and 8 paint a 10 m road')
        walks = [{'fid': fid, 'pts': pts + [0., 6.35, 0.], 'side': 1, 'w': 2.5}]
        box = (0., 0., 256., 256.)
        lamps = [r for r in furniture.place([near[0]], walks, box, curb_h=0., clearance=tile[0])
                 if r[0] == furniture.G_LAMP]
        self.assertTrue(lamps, 'the 5.6 m pivot clears a painted 5 m road edge')
        self.assertFalse(any(r[0] == furniture.G_LAMP for r in furniture.place([near[0]], walks, box, curb_h=0.)),
                         'the obsolete nominal 12 m half-width would delete this lamp')
        lamp = lamps[0]
        self.assertAlmostEqual(lamp[2] - pts[0, 1], 5.6, delta=.02)
        self.assertAlmostEqual((lamp[2] - pts[0, 1]) - .144, 5.456, delta=.02)
        intruding = list(lamp)
        intruding[2] -= .5
        self.assertFalse(tile[0].clear(intruding), 'a post inside the actual painted margin must still be rejected')

        captured, make = [], furniture.RoadClearance
        def record(*args, **kwargs):
            index = make(*args, **kwargs)
            captured.append(index)
            return index
        with patch.object(furniture, 'RoadClearance', side_effect=record):
            navgraph.build(FRAME, [f], elev, (50., 100.), urban_fn=urban)
        self.assertEqual(len(captured), 1)
        self.assertAlmostEqual(captured[0].half[0], 5., delta=.02)
        self.assertTrue(captured[0].clear(lamp), 'dynamic signals use the same actual ground clearance')
        self.assertFalse(captured[0].clear(intruding))

    def test_short_deck_actual_depth_keeps_lamp_in_tile_and_graph_contexts(self):
        import navgraph
        base, walks, box, _, _ = PlacementClearanceTest().fixture()
        target = next(r for r in furniture.place([base], walks, box, curb_h=0.)
                      if r[0] == furniture.G_LAMP and 20. < r[1] < 236.)
        self.assertAlmostEqual(target[3] + furniture.COLLISION_BOXES[furniture.G_LAMP][1][1][5], 8.271)
        f = features([[(0., target[2]), (256., target[2])]], ['primary'],
                     flags=[(0, 'is_bridge')], ids=['short-deck'])
        ll = np.asarray(f.parts(0)[0][0], np.float64)
        x, y, _ = FRAME.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
        xy, z = np.column_stack([x, y]), np.full(len(x), 10.)
        pts = np.column_stack([xy, z])
        deck_spec = roads.bridge_specs(f)[0]
        chunks = roads._deck_chunks([('short-deck', deck_spec, pts, 0.)], (0., 0., 256., 256.),
                                    depths={'short-deck': .9})
        ground = features([[tuple(p[:2]) for p in base[2]]], ['residential'], ids=[base[0]],
                          widths=[(0, 8.)])
        elev = lambda la, lo: np.zeros(np.shape(la))
        urban = lambda la, lo: np.ones(np.shape(la), bool)
        tile = []
        with patch.object(roads, '_bridge_chunks', return_value=chunks):
            roads.build_tile(FRAME, 0, 0, [ground], elev, urban_fn=urban, clearance_out=tile)
        deck_context = [roads._deck_clearance(ch) for ch in chunks if ch.get('deck')]
        self.assertTrue(deck_context)
        self.assertTrue(all(depth == .9 for _, _, depth in deck_context))
        self.assertTrue(tile[0].clear(target), '8.271 m lamp collider is below the 9.1 m real underside')
        self.assertFalse(furniture.RoadClearance([], [(pts, deck_spec.width / 2)]).clear(target),
                         'the unknown-depth 2.2 m default would put the underside at 7.8 m')
        raised = list(target)
        raised[3] = 1.
        self.assertFalse(tile[0].clear(raised), 'a lamp that actually reaches the underside is rejected')
        decks_for_wires = [(p, h) for p, h, _ in deck_context]
        placed = furniture.place([base], walks, box, curb_h=0., decks=decks_for_wires, clearance=tile[0])
        self.assertIn(target, placed)

        captured, make = [], furniture.RoadClearance
        def record(*args, **kwargs):
            index = make(*args, **kwargs)
            captured.append(index)
            return index
        with patch.object(furniture, 'RoadClearance', side_effect=record):
            graph = navgraph.build(FRAME, [f], elev, (128., target[2]),
                                   decks={'short-deck': [(xy, z, .9)]})
        self.assertEqual(len(captured), 1)
        self.assertTrue(graph['nodes'], 'three-item bridge parts still supply the graph node elevations')
        self.assertTrue(all(abs(n[2] - 10.) < .05 for n in graph['nodes'].values()))
        self.assertTrue(np.allclose(captured[0].bottom, .9))
        self.assertTrue(captured[0].clear(target))
        self.assertFalse(captured[0].clear(raised))

    def test_actual_shifted_sidewalk_deck_and_its_taper_feed_the_shared_index(self):
        spec = roads.Spec(8., 'paved', True, 'primary', False)
        pts = np.array([[0., 100., 20.], [128., 100., 20.], [256., 100., 20.]])
        chunks = roads._deck_chunks(
            [('deck', spec, pts, 0.)], (0., 0., 256., 256.), walks={'deck': (2., (1,))},
            shift_of=lambda fid, p: np.full(len(p), 10.),
            width_of=lambda fid, p: np.where(p[:, 0] < 128., 8., 16.))
        context = []
        with patch.object(roads, '_bridge_chunks', return_value=chunks):
            roads.build_tile(FRAME, 0, 0, [], flat, clearance_out=context)
        self.assertTrue(context[0].clear(row('stop', 20., 119., z=20.)))
        self.assertFalse(context[0].clear(row('stop', 190., 119., z=20.)))
        self.assertTrue(furniture.RoadClearance([], [(pts, 4.)]).clear(row('stop', 190., 119., z=20.)))

    def test_tunnel_vault_is_not_a_second_traffic_deck(self):
        import roadmesh
        tunnel = {'tunnel': True, 'n': [[0., 100., 0.], [256., 100., 0.]], 'w': 8.}
        context = []
        with patch.object(roads, '_tunnel_chunks', return_value=[tunnel]), \
                patch.object(furniture, 'place', wraps=furniture.place) as place:
            roads.build_tile(FRAME, 0, 0, [], flat, tunnels=object(), walks=[], props=[],
                             clearance_out=context)
        self.assertIs(place.call_args.kwargs['clearance'], context[0])
        self.assertEqual(place.call_args.kwargs['decks'], [])
        self.assertTrue(context[0].clear(row('stop', 100., 100., z=roadmesh.TUNNEL_H + 1.)))

    def test_high_bridge_preserves_static_and_switchable_signal_level_parity(self):
        import navgraph
        f = features([[(0., 0.), (0., -100.)], [(0., 0.), (100., 0.)], [(0., 0.), (-100., 0.)],
                      [(-100., 0.), (100., 0.)]], ['primary'] * 4, flags=[(3, 'is_bridge')])
        f.attrs['osm_width'] = np.array([8., 8., 8., 80.])
        f.rules['connectors'] = rules([(i, 'J', 0., 0.) for i in range(3)])
        urban = lambda la, lo: np.ones(np.shape(la), bool)
        specs = roads.road_specs(f, urban)
        rs = []
        for i in range(3):
            ll = np.asarray(f.parts(i)[0][0], np.float64)
            x, y, _ = FRAME.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
            z = FRAME.geodetic_to_enu(ll[:, 1], ll[:, 0], flat(ll[:, 1], ll[:, 0]))[2]
            rs.append((str(f.ids[i]), specs[i], np.column_stack([x, y, z]), float(ll[:, 0].mean())))
        ll = np.asarray(f.parts(3)[0][0], np.float64)
        x, y, _ = FRAME.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
        xy, z = np.column_stack([x, y]), np.full(len(x), 50.)
        decks = {str(f.ids[3]): [(xy, z)]}
        signals = np.zeros((1, 2))
        static = furniture.junction_props(rs, (-50., -50., 50., 50.), signals=signals,
                                         decks=[(np.column_stack([xy, z]), 40.)],
                                         ground=lambda x, y: np.full(np.shape(x), 10.))
        graph = navgraph.build(FRAME, [f], flat, (0., 0.), urban_fn=urban, decks=decks, signals=signals)
        heads = [a[7:11] for junction in graph['signals'] for a in junction['app']]
        self.assertTrue(static)
        self.assertEqual(len(heads), len(static))
        self.assertTrue(all(h[0] is not None for h in heads), 'no static-only uncontrolled lights below a high deck')
        self.assertEqual(sorted(tuple(h) for h in heads), sorted(tuple(r[1:5]) for r in static))


if __name__ == '__main__':
    unittest.main()
