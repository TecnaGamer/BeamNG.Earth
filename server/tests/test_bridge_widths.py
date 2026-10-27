"""Approach width overrides must reach the actual deck, parapets, paint and AI."""
import unittest
import numpy as np
from test_roads import FRAME, features, flat, rules
import roads, roadmesh, bridgewidths, navgraph


def chain(widths=(13., 17.), reverse=False):
    lines = [[(-200., 0.), (-60., 0.)], [(-60., 0.), (60., 0.)], [(60., 0.), (200., 0.)]]
    if reverse:
        lines[1].reverse()
    f = features(lines, ['secondary'] * 3, widths=[(0, widths[0]), (2, widths[1])], flags=[(1, 'is_bridge')])
    f.attrs['lane_count'] = np.array([2., 2., 2.])
    return f


class BridgeWidthTest(unittest.TestCase):
    def test_promoted_approach_keeps_lane_width_and_collision_surface(self):
        # The clearance fix extends the deck onto an unflagged road. That new
        # deck must retain the infrastructure branch's three-lane cross section.
        f = features([[(-40, 0), (5, 0)], [(5, 0), (100, 0)], [(0, -100), (0, 100)]],
                     ['secondary', 'secondary', 'motorway'], flags=[(0, 'is_bridge')])
        f.attrs['lane_count'] = np.array([3., 3., 2.])
        self.assertEqual(roads.raw_level_ranges(f, 1), [])
        self.assertTrue(roads.bridge_parts(f, 1))
        sp = roads.bridge_specs(f)[1]
        ground = roads.road_specs(f)[1]
        self.assertEqual(sp.lanes, 3.)
        self.assertEqual(sp.width, ground.width)
        self.assertGreater(sp.width, 11.)
        bp = roads.BridgeProfiles(FRAME, lambda _: f, lambda la, lo: np.full(np.shape(la), 10.))
        parts = bp.parts([f], (-200., -200., 200., 200.))
        self.assertTrue(any(fid == str(f.ids[1]) for fid, *_ in parts))
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat, bridges=bp)
        decks = [c for c in chunks if c.get('deck') and c.get('fid') == str(f.ids[1]) and not c.get('ctx')]
        self.assertTrue(decks)
        self.assertTrue(all(c['w'] == sp.width for c in decks))
        self.assertIsNotNone(roadmesh.deck(decks, [0., 0., 0.]))

    def test_sidewalk_suppressed_approach_keeps_its_width_and_edge_margins_on_deck(self):
        f = features([[(0, 0), (5, 0), (10, 0), (15, 0), (20, 0), (400, 0)]],
                     ['secondary'], flags=[(0, 'is_bridge', .4, .6)])
        f.attrs['lane_count'] = np.array([2.])
        def farm(lat, lon):
            e, n, _ = FRAME.geodetic_to_enu(np.asarray(lat), np.asarray(lon), 0.)
            return (e < 35.) & (np.abs(n) < 60.)
        ground = roads.road_specs(f, farm)[0]
        deck = roads.bridge_specs(f, farm)[0]
        self.assertTrue(ground.urban)
        self.assertFalse(roads.has_sidewalks(ground))
        self.assertAlmostEqual(ground.width, 11.6)
        self.assertEqual(deck, ground)
        line = np.array([[0., 0., 10.], [100., 0., 10.]])
        for spec in (ground, deck):
            edges = [p for look, _, p in roads.extras(spec, line, -85.) if 'edge_side' in look]
            self.assertEqual(len(edges), 2)
            for p in edges:
                np.testing.assert_allclose(np.abs(p[:, 1]), 5.5)

    def test_partial_bridge_uses_the_same_measured_cross_section_as_its_ground(self):
        f = features([[(-200., 0.), (200., 0.)]], ['secondary'], flags=[(0, 'is_bridge', .3, .7)])
        f.attrs['lane_count'] = np.array([2.])
        widths = lambda *_: (15., 2.5)
        road = roads.road_specs(f, widths=widths)[0]
        deck = roads.bridge_specs(f, widths=widths)[0]
        self.assertEqual(deck, road)
        self.assertEqual(deck.width, 15.)
        bp = roads.BridgeProfiles(FRAME, lambda _: f, flat, widths=widths)
        parts = bp.parts([f], (-250., -100., 250., 100.))
        self.assertTrue(parts)
        self.assertTrue(all(sp.width == 15. for _, sp, *_ in parts))
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat, widths=widths, bridges=bp)
        decks = [c for c in chunks if c.get('deck')]
        self.assertTrue(decks)
        self.assertTrue(all(c['w'] == 15. for c in decks))

    def test_aligned_approaches_set_both_ends_in_either_geometry_order(self):
        for reverse in (False, True):
            f = chain(reverse=reverse)
            sp = roads.bridge_specs(f)[1]
            self.assertEqual(sp.width, 17.)
            self.assertEqual(sp.bridge_ends, (17., 17., 13.) if reverse else (13., 17., 17.))
            w = bridgewidths.at(sp, np.linspace(0., 120., 121), 120.)
            self.assertLess(np.abs(np.diff(w)).max(), .11)
            self.assertAlmostEqual(w[0], sp.bridge_ends[0])
            self.assertAlmostEqual(w[-1], sp.bridge_ends[-1])

    def test_bridge_chain_carries_agreed_width_through_middle_parts(self):
        f = features([[(-200, 0), (-100, 0)], [(-100, 0), (0, 0)], [(0, 0), (100, 0)], [(100, 0), (200, 0)]],
                     ['secondary'] * 4, widths=[(0, 15.), (3, 15.)], flags=[(1, 'is_bridge'), (2, 'is_bridge')])
        f.attrs['lane_count'] = np.full(4, 2.)
        self.assertTrue(all(sp.width == 15. and sp.bridge_ends == (15., 15., 15.)
                            for sp in roads.bridge_specs(f)[1:3]))

    def test_crossing_or_different_lane_count_does_not_size_the_bridge(self):
        for mode in ('crossing', 'count', 'ramp', 'connector'):
            f = chain((19., 19.))
            if mode == 'crossing':
                f.lonlat[:2] = features([[(-60., -200.), (-60., 0.)]], ['secondary']).lonlat
                f.lonlat[-2:] = features([[(60., 0.), (60., 200.)]], ['secondary']).lonlat
            elif mode == 'count':
                f.attrs['lane_count'][[0, 2]] = 4.
            elif mode == 'ramp':
                f.rules['subclass'] = rules([(0, 'link'), (2, 'link')])
            else:
                f.rules['connectors'] = rules([(0, 'a', 1., 1.), (1, 'b', 0., 0.),
                                               (1, 'c', 1., 1.), (2, 'd', 0., 0.)])
            sp = roads.bridge_specs(f)[1]
            self.assertEqual((sp.width, sp.bridge_ends), (11., None), mode)

    def test_tapered_deck_collision_parapets_and_paint_match_at_seams(self):
        sp = roads.bridge_specs(chain())[1]
        p = np.c_[np.linspace(-60., 60., 61), np.zeros(61), np.full(61, 20.)]
        allchunks = roads._deck_chunks([('bridge', sp, p, -85.)], (-70., -30., 70., 30.))
        decks = [c for c in allchunks if c.get('deck') and not c.get('ctx')]
        self.assertTrue(decks)
        left, right, _, _, _, z = roadmesh.ribbons(decks)[0]
        self.assertAlmostEqual(left[0, 1] - right[0, 1], 13.)
        self.assertAlmostEqual(left[-1, 1] - right[-1, 1], 17.)
        solid = roadmesh.deck(decks, [0., 0., 0.])
        self.assertIsNotNone(solid)
        positions = solid[0]
        for x, width in ((-60., 13.), (60., 17.)):
            at = positions[np.abs(positions[:, 0] - x) < .01]
            self.assertAlmostEqual(np.max(np.abs(at[:, 1])), width / 2 + roadmesh.PARAPET_W, places=4)
        edges = [line for look, _, line in bridgewidths.extras(sp, p, -85.) if 'edge_side' in look]
        self.assertEqual(len(edges), 2)
        for line in edges:
            self.assertAlmostEqual(abs(line[0, 1]), 5.25)
            self.assertAlmostEqual(abs(line[-1, 1]), 7.25)
        for box in ((-70., -30., 0., 30.), (0., -30., 70., 30.)):
            chunks = roads._deck_chunks([('bridge', sp, p, -85.)], box)
            at = [c for c in chunks if c.get('deck') and not c.get('ctx')][0]
            k = np.argmin(np.abs(np.array(at['n'])[:, 0]))
            self.assertAlmostEqual(at['w_nodes'][k], 17.)

    def test_ai_uses_the_narrower_end_of_each_tapered_edge(self):
        f = chain()
        g = navgraph.build(FRAME, [f], flat, (0., 0.))
        for e in g['edges']:
            if 'id001' in e[0] or 'id001' in e[1]:
                self.assertEqual((e[3], e[4]), (1, 1))
                self.assertLessEqual(e[7], 7.25)


if __name__ == '__main__':
    unittest.main()
