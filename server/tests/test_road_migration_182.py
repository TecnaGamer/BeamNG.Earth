"""Custom lanes/shoulders must coexist with 1.8's cell facts, tapers and shifts."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import unittest
import numpy as np
import osmtags
import roadlanes
import roads
from test_roadlanes import chain, highway


class IntegrationTest(unittest.TestCase):
    def test_enrichment_retains_cell_context_and_drops_derived_facts(self):
        f = chain()
        cid = ('migration-test', 'roads', '{}')
        f._bnge_cell = cid
        hook = f._bnge_neighbours = lambda: []
        roads.bridge_specs(f)
        roads.road_tapers(f)
        roads.deck_tapers(f)
        roads.lane_shifts(f)
        self.assertTrue(any(k[0] == cid for k in roads._FACTS_MEM))
        osmtags.join(f, {1: {'lanes': 4}, 3: {'lanes': 4}})
        self.assertEqual(f._bnge_cell, cid)
        self.assertIs(f._bnge_neighbours, hook)
        self.assertFalse(any(k[0] == cid for k in roads._FACTS_MEM))
        self.assertNotIn('_deck_tapers', f.__dict__)
        self.assertNotIn('_lane_shifts', f.__dict__)
        self.assertEqual(roads.bridge_specs(f)[1].lanes, 4)
        with roads._FACTS_LOCK:
            for k in list(roads._FACTS_MEM):
                if k[0] == cid:
                    del roads._FACTS_MEM[k]

    def test_taper_and_merge_shift_keep_asymmetric_shoulder_alignment(self):
        for direction in (-1, 1):
            f = highway(direction=direction)
            osmtags.join(f, {1: {'lanes': 3}})
            sp = roads.road_specs(f)[0]
            pts = np.c_[np.arange(101.), np.zeros((101, 2))]
            widths = np.linspace(sp.width, sp.width + 4., len(pts))
            shifts = np.linspace(0., -2., len(pts))
            paint = roads.extras(sp, pts, -85., country='US', ws=widths, shift=shifts)
            edges = {look['side']: p for look, w, p in paint if look.get('kind') == 'edge'}
            normal = np.array([.02, 1.]) / np.hypot(.02, 1.)
            base = np.c_[pts[:, 0], shifts]
            for side in (-1, 1):
                expected = base + normal * (side * (widths / 2 - roads.edge_margin(sp, side)))[:, None]
                np.testing.assert_allclose(edges[side][:, :2], expected)
            drive, _ = roads.lane_layout(sp)
            for look, w, p in paint:
                if look.get('kind') != 'lane':
                    continue
                # All dashed lane dividers stay centred in their tapered travel band.
                station = (p[:, 0] - .02 * p[:, 1]) / 1.0004
                half_lane = (drive + np.interp(station, pts[:, 0], widths) - sp.width) / 6
                np.testing.assert_allclose(np.abs(p[:, :2] @ normal - roads.driving_offset(sp)), half_lane, atol=1e-6)

    def test_current_lane_flow_can_supply_an_unmapped_bridge_after_a_merge(self):
        from test_roads import rules
        f = highway([[(-300, 0), (0, 0)], [(-200, -150), (0, 0)], [(0, 0), (300, 0)]],
                    flags=[(2, 'is_bridge')])
        f.rules['subclass'] = rules([(1, 'link')])
        osmtags.join(f, {1: {'lanes': 4}})
        self.assertEqual(roadlanes.generated_counts(f), [4, None, None])
        sp = roads.bridge_specs(f)[2]
        self.assertEqual(sp.lanes, 5)
        self.assertEqual(sp.width, 26.)
        self.assertEqual(roads._structure_width(f, 2), sp.width)


if __name__ == '__main__':
    unittest.main()
