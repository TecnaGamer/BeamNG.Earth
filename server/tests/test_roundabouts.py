"""Roundabout widths shared by paint/AI/grading, and clearance of generated objects."""
import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import grading
import islands
import navgraph
import osmtags
import parking
import roads
import roundabouts
import vegetation
from test_islands import arc
from test_roads import FRAME, LAT, LON, M_LAT, M_LON, features, rules, flat


def ring_features(count=2, pieces=6, mapped=True, radius=30.):
    lines = [arc(radius, k * 2 * math.pi / pieces, (k + 1) * 2 * math.pi / pieces,
                 14, cx=120., cy=120.)[:, :2] for k in range(pieces)]
    lines.append(np.array([[120. + radius, 120.], [245., 120.]]))
    f = features(lines, ['primary'] * pieces + ['residential'])
    f.attrs['src_record'] = np.array([f'w{i+1}@1' for i in range(pieces + 1)])
    tags = {i + 1: {'lanes': count, 'sidewalk': 'both', 'osm_junction': 'roundabout'} for i in range(pieces)}
    osmtags.join(f, tags if mapped else {})
    return f


class RoundaboutTest(unittest.TestCase):
    def test_mapped_width_and_lanes_across_ground_decks_and_tunnels(self):
        for level in ('ground', 'bridge', 'tunnel'):
            f = ring_features()
            if level != 'ground':
                f.rules['flags'] = rules([(i, 'is_' + level) for i in range(6)])
            specs = {'ground': roads.road_specs, 'bridge': roads.bridge_specs,
                     'tunnel': roads.tunnel_specs}[level](f)
            for sp in specs[:6]:
                self.assertTrue(sp.roundabout and sp.oneway)
                self.assertEqual((sp.lanes, sp.width), (2, 19.))
                self.assertEqual(navgraph.driving_width(sp), 18.)
                line = arc(30., 0., math.pi / 3, 14)
                ex = roads.extras(sp, line, LON)
                self.assertEqual(sum('dash' in look for look, _, _ in ex), 1)
                self.assertEqual(grading._plan(line[:, :2], line[:, 2], sp).road_half, 9.5)

    def test_missing_tags_infer_one_lane_and_keep_city_blocks(self):
        f = ring_features(mapped=False)
        specs = roads.road_specs(f)
        self.assertTrue(all(sp.roundabout and sp.width == 10. and sp.lanes == 1 for sp in specs[:6]))
        self.assertFalse(specs[-1].roundabout)
        block = features([[(0, 0), (60, 0)], [(60, 0), (60, 60)],
                          [(60, 60), (0, 60)], [(0, 60), (0, 0)]], ['residential'] * 4)
        block.rules['oneway'] = rules([(i, 1) for i in range(4)], 'float')
        self.assertFalse(any(sp.roundabout for sp in roads.road_specs(block)))

    def test_partial_mapped_ring_keeps_reverse_direction_and_explicit_width(self):
        f = features([[(0, 0), (15, 8), (20, 20)]], ['primary'], widths=[(0, 22.)])
        f.attrs['src_record'] = np.array(['w1@1'])
        osmtags.join(f, {1: {'osm_junction': 'roundabout', 'osm_oneway': '-1', 'lanes': 2}})
        sp = roads.road_specs(f, widths=lambda *_: (5., 1.))[0]
        self.assertEqual((sp.width, sp.lanes, sp.dir, sp.roundabout), (22., 2, -1, True))

    def test_large_split_island_curb_and_sidewalks_clear_circulating_lanes(self):
        f = ring_features()
        f.attrs['osm_sidewalk'][-1] = 'both'   # mapped approaches retain their sidewalks too
        specs = roads.road_specs(f)
        rows = []
        for i, sp in enumerate(specs):
            ll = f.parts(i)[0][0]
            e, n, z = FRAME.geodetic_to_enu(ll[:, 1], ll[:, 0], 10.)
            rows.append((str(f.ids[i]), sp, np.c_[e, n, z], LON))
        found = islands.find(rows)
        self.assertEqual(len(found), 1)
        self.assertEqual(len(found[0]['inner']), 6)
        rings = roundabouts.corridors(rows)
        walks, props = [], []
        roads.build_tile(FRAME, 0, 0, [f], flat, urban_fn=lambda la, lo: np.ones(np.shape(la), bool),
                         walks=walks, props=props)
        self.assertTrue(any(w.get('fid') == 'island' for w in walks))
        self.assertTrue(any(w.get('fid') == str(f.ids[-1]) for w in walks))
        self.assertTrue(any(w.get('fid') in set(f.ids[:6]) for w in walks), 'mapped outer sidewalks remain')
        for w in walks:
            if 'pts' not in w:
                self.assertFalse(roundabouts.overlaps(roads.densify(w['corner']['arc'], .25), rings, -.15))
                continue
            # The full sidewalk ribbon, with a small tolerance for rounded mesh coordinates.
            for side in (-1, 1):
                edge = roads.offset_polyline(w['pts'], side * w['w'] / 2)
                self.assertFalse(roundabouts.overlaps(roads.densify(edge, .25), rings, -.2), w['fid'])
        self.assertEqual(roundabouts.clear_props(props, rings), props)

    def test_props_and_parked_cars_cannot_occupy_the_ring(self):
        sp = roads.Spec(11., 'paved', True, 'residential', True, lanes=2, roundabout=True)
        line = arc(30., 0., 2 * math.pi, 100)
        rings = [(line, 5.5)]
        rows = [[1, 30., 0., 0., 0., 1.], [1, 0., 0., 0., 0., 1.], [1, 30., 0., 15., 0., 1.]]
        self.assertEqual(roundabouts.clear_props(rows, rings), rows[1:])
        walks = [{'fid': 'r', 'side': -1, 'w': 2.5, 'pts': roads.offset_polyline(line, -6.75)}]
        with patch('furniture._h01', return_value=0.):
            self.assertEqual(parking.curb_spots([('r', sp, line, LON)], walks, (-100., -100., 100., 100.)), [])

    def test_vegetation_mask_covers_wide_ring(self):
        f = ring_features(count=3)
        x0, y0 = vegetation._to3857(LON, LAT)
        grid = SimpleNamespace(x0=float(x0), y0=float(y0) + 400., dx=1., dy=1.)
        mask = np.zeros((400, 400), bool)
        vegetation._mask_roads(f, mask, grid)
        ll = f.parts(0)[0][0][6]
        x, y = vegetation._to3857(*ll)
        self.assertTrue(mask[int(grid.y0 - y), int(x - grid.x0)])
        # Outside the old primary corridor (5.5 + 1.5 m), but within the
        # widened ring's 8 + 1.5 m clearance, between its source vertices.
        theta = math.pi / 6
        x, y = vegetation._to3857(LON + (120. + 38.5 * math.cos(theta)) / M_LON,
                                  LAT + (120. + 38.5 * math.sin(theta)) / M_LAT)
        pixel = (int(grid.y0 - y), int(x - grid.x0))
        self.assertTrue(mask[pixel])
        legacy = SimpleNamespace(**{k: getattr(f, k) for k in
                                   ('attrs', 'kind', 'lonlat', 'coord_off', 'ring_off', 'part_off')})
        old_mask = np.zeros_like(mask)
        vegetation._mask_roads(legacy, old_mask, grid)
        self.assertFalse(old_mask[pixel])


if __name__ == '__main__':
    unittest.main()
