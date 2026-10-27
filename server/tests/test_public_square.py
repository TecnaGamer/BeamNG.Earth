"""Regression for the user's Public Square screenshot (Angola, Indiana)."""
import json
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy
import grading
import islands
import mapgeom
import navgraph
import roads
import roundabouts

FRAME = geodesy.EnuFrame(41.63481, -84.99966, 0.)


def urban(lat, lon):
    return np.ones(np.shape(lat), bool)


def flat(lat, lon):
    return np.zeros(np.shape(lat))


def fixture():
    data = json.loads((Path(__file__).parent / 'fixtures/public_square_angola.json').read_text(encoding='utf-8'))
    rows = data['roads']
    f = mapgeom.build('roads', np.array([r['id'] for r in rows]),
                      [(mapgeom.LINE, [[np.array(p) for p in part] for part in r['parts']]) for r in rows])
    for k in rows[0]['attrs']:
        numeric = k in ('lane_count', 'osm_lanes', 'osm_width')
        f.attrs[k] = np.array([np.nan if numeric and r['attrs'][k] is None else r['attrs'][k] for r in rows])
    for k in sorted({k for r in rows for k in r['rules']}):
        entries = [(i, entry[0], entry[1:]) for i, r in enumerate(rows) for entry in r['rules'].get(k, ())]
        f.rules[k] = mapgeom.rule_table(entries, 'float' if isinstance(entries[0][1], (float, int)) else 'text')
    return f


def road_rows(f, specs):
    return [(str(f.ids[i]), sp, np.c_[part[0], np.zeros(len(part[0]))], FRAME.lon0)
            for i, sp in enumerate(specs) if sp is not None for part in f.parts(i, f.xy_in(FRAME))]


class PublicSquareTest(unittest.TestCase):
    def test_all_eight_untagged_arcs_and_approaches_share_widths_with_ai_and_grading(self):
        f = fixture()
        self.assertFalse(any(f.attrs['osm_junction']))
        specs = roads.road_specs(f, urban, widths=lambda *_: (3.5, 2.5))
        ring = [sp for sp in specs if sp and sp.roundabout]
        entry = [sp for sp in specs if sp and sp.roundabout_entry]
        self.assertEqual(len(ring), 8)
        self.assertEqual(len(entry), 8)
        for sp in ring:
            self.assertEqual((sp.width, sp.lanes, sp.dir, sp.median), (10., 1, 1, None))
            self.assertEqual(navgraph.driving_width(sp), 9.)
            self.assertFalse(roads.has_sidewalks(sp))
        for sp in entry:
            self.assertEqual(sp.lanes, 1)
            self.assertGreaterEqual(sp.width, 6.6)   # opposing carriageways retain their separation
            self.assertLessEqual(sp.width, 7.)
            self.assertAlmostEqual(navgraph.driving_width(sp), sp.width - 1.)
            self.assertFalse(roads.has_sidewalks(sp))
        for fid, sp, p, _ in road_rows(f, specs):
            if sp.roundabout or sp.roundabout_entry:
                self.assertEqual(grading._plan(p[:, :2], p[:, 2], sp).road_half, sp.width / 2)
            if sp.cls in ('footway', 'service'):
                self.assertFalse(sp.roundabout or sp.roundabout_entry)

    def test_central_island_can_contain_a_monument_and_all_curbs_clear_the_ring(self):
        f = fixture()
        rows = road_rows(f, roads.road_specs(f, urban))
        found = islands.find(rows, structures=[(32., 5.)])
        central = [i for i in found if len(i['inner']) == 8]
        self.assertEqual(len(central), 1)
        self.assertIsNotNone(central[0]['ring'])
        rings = roundabouts.corridors(rows)
        walks, props, park, chunks = [], [], [], []
        for tx in (-1, 0):
            for ty in (-1, 0):
                chunks += roads.build_tile(FRAME, tx, ty, [f], flat, urban_fn=urban,
                                           walks=walks, props=props, park=park, structures=[(32., 5.)])
        for w in walks:
            if 'pts' in w:
                for side in (-1, 1):
                    edge = roads.offset_polyline(w['pts'], side * w['w'] / 2)
                    self.assertFalse(roundabouts.overlaps(roads.densify(edge, .25), rings, -.2), w['fid'])
            else:
                self.assertFalse(roundabouts.overlaps(roads.densify(w['corner']['arc'], .25), rings, -.2))
        for ch in chunks:
            if ch['m'] == 'crossing_white':
                self.assertFalse(roundabouts.overlaps(roads.densify(np.array(ch['n']), .25), rings, ch['w'] / 2))
        self.assertEqual(roundabouts.clear_props(props, rings), props)
        no_walks = {fid for fid, sp, _, _ in rows if sp.roundabout or sp.roundabout_entry}
        self.assertFalse([w for w in walks if w.get('fid') in no_walks])
        central_curbs = [w for w in walks if w.get('fid') == 'island'
                         and np.linalg.norm(w['pts'][:, :2] - [32., 5.], axis=1).max() < 25.]
        self.assertTrue(central_curbs)
        self.assertTrue(all(w['w'] == .4 for w in central_curbs), 'a narrow curb, not an island walkway')

    def test_ai_connects_all_eight_entries_to_one_directed_ring(self):
        f = fixture()
        specs = roads.road_specs(f, urban)
        rings = roundabouts.corridors(road_rows(f, specs))
        graph = navgraph.build(FRAME, [f], flat, (32., 5.), urban_fn=urban)
        on_ring = {k for k, n in graph['nodes'].items() if roundabouts.overlaps([n[:3]], rings, -4.8)}
        edges = [e for e in graph['edges'] if e[0] in on_ring and e[1] in on_ring]
        self.assertGreaterEqual(len(edges), 8)
        for e in edges:
            self.assertEqual((e[2], e[3], e[4], e[7]), (1, 1, 0, 4.5))
        start = next(iter(on_ring))
        seen, pending = {start}, [start]
        while pending:
            at = pending.pop()
            for e in edges:
                if e[0] == at and e[1] not in seen:
                    seen.add(e[1])
                    pending.append(e[1])
        self.assertEqual(seen, on_ring)
        for i, sp in enumerate(specs):
            if sp is None or not sp.roundabout_entry:
                continue
            names = {'c' + str(c) for c, _, _ in f.rules['connectors'].for_feature(i)}
            connected = names & on_ring
            self.assertEqual(len(connected), 1)
            self.assertTrue(any((e[0] in connected) != (e[1] in connected)
                                for e in graph['edges'] if e[0] not in on_ring or e[1] not in on_ring))


if __name__ == '__main__':
    unittest.main()
