import os
os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "regional"))  # (PR #505's data: the tests' slice)
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "tools")]
import frgore  # noqa: E402
import frlocaldirections as directions
import frroadmesh
import geodesy
import mapgeom
import poleprofiles
import roads
import signmesh
import signplacement
from test_roads import rules

FRAME = geodesy.EnuFrame(48.80, 7.78)
BOX = (0.0, 0.0, 256.0, 256.0)


def fr(*_):
    return 'FR'


def coordinate(x, y):
    lat, lon, _ = FRAME.enu_to_geodetic(x, y, 0.0)
    return [round(lon * 1.0e7), round(lat * 1.0e7)]


def road(key, points, cls='secondary', direction=0, width=7.0, link=False, z=0.0):
    sp = roads.Spec(width, 'paved', False, cls, bool(direction), dir=direction, kmh=80, link=link)
    return key, sp, np.c_[points, np.full(len(points), z)], 7.78




class LocalDirectionsTest(unittest.TestCase):
    def setUp(self):
        self.incoming = road('in', [(5.0, 120.0), (150.0, 120.0)])
        self.north = road('n', [(150.0, 120.0), (150.0, 238.0)], direction=1)
        self.east = road('e', [(150.0, 120.0), (255.0, 120.0)], direction=1)
        self.plan = {
            'id': 'junction/1',
            'at': coordinate(150.0, 120.0),
            'in': coordinate(130.0, 120.0),
            'way': 1,
            'panels': [
                {
                    'names': ['Montgon', 'Le Chesne'],
                    'out': coordinate(150.0, 140.0),
                    'way': 2,
                    'ref': 'D 25',
                    'source': 'relation',
                    'distance': [2500, 7000],
                },
                {
                    'names': ['Semuy'],
                    'out': coordinate(172.0, 120.0),
                    'way': 3,
                    'ref': 'D 25',
                    'source': 'route',
                    'route_m': 2949,
                },
            ],
        }
        self.ways = {'in': 1, 'n': 2, 'e': 3}

    def place(self, rows=None, box=BOX, plan=None, **options):
        mesh = signmesh.Mesh((box[0], box[1], 0.0))
        records = directions.add(
            FRAME,
            rows or [self.incoming, self.north, self.east],
            box,
            fr,
            mesh,
            self.ways,
            data=[self.plan if plan is None else plan],
            **options
        )
        return mesh, records

    def test_distance_column_arrow_and_full_pavement_clearance(self):
        south = road('s', [(150., 120.), (150., 12.)], direction=1)
        self.ways['s'] = 4
        rows = [self.incoming, self.north, self.east, south]
        straight = self.plan['panels'][1]
        self.plan['panels'].append(dict(straight, out=coordinate(150., 92.), way=4))
        straight['names'] = ['Saudoy']
        mesh, (sign,) = self.place(rows, ground=lambda x, y: np.full_like(x, 2.4))
        self.assertEqual(sign['route'], 'D 25')
        self.assertEqual(sign['panels'][0]['turn'], 'left')
        self.assertEqual(sign['panels'][0]['distance_km'], ['2,5', '7'])
        self.assertEqual(sign['panels'][1]['turn'], 'right')
        self.assertEqual(len(sign['panels']), 2)
        self.assertEqual(sign['panels'][1]['distance_km'], ['3'])
        self.assertEqual(sign['pos'][2], 2.4)
        self.assertLess(np.asarray(mesh.p)[:, 1].max(), 120.0 - 3.5)
        _, (urban,) = self.place(rows, urban_fn=lambda lat, lon: np.ones_like(lat, bool))
        self.assertNotIn('distance_km', urban['panels'][1])
        self.assertEqual(urban['panels'][0]['distance_km'], ['2,5', '7'])

    def test_keeps_safe_surveyed_position_and_moves_away_from_rail(self):
        self.plan['site'] = coordinate(132.0, 114.0)
        _, (surveyed,) = self.place()
        self.assertTrue(surveyed['surveyed_position'])
        x, y, _ = surveyed['pos']
        barrier = [0, x, y, 0.0, 0.0, 0.0, 0.7, 0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        _, (shifted,) = self.place(guardrails=[barrier])
        self.assertFalse(shifted['surveyed_position'])
        self.assertGreater(np.linalg.norm(np.asarray(shifted['pos'][:2]) - [x, y]), 2.0)

    def test_rejects_occlusion_wrong_way_and_wrong_grade(self):
        wall = [[5, 114.75], [156, 114.75], [156, 116.25], [5, 116.25]]
        self.assertEqual(self.place(obstacles=signplacement.Context([wall]))[1], [])
        reverse = road('in', [(5.0, 120.0), (150.0, 120.0)], direction=-1)
        self.assertEqual(self.place([reverse, self.north, self.east])[1], [])
        over = road('n', [(150.0, 120.0), (150.0, 238.0)], direction=1, z=5.0)
        self.assertEqual(self.place([self.incoming, over, self.east])[1], [])

    def test_no_duplicate_on_tile_seam(self):
        self.assertFalse(self.place(box=(150.0, 0.0, 406.0, 256.0))[1])
        self.assertEqual(len(self.place()[1]), 1)

    def test_current_village_and_unmapped_town_hall_are_filtered(self):
        north, east = self.plan['panels']
        north = dict(north, names=['Lembach', 'Mairie'], local=True)
        names = dict(east, names=['Lém-bach', 'Mairie', 'Mattstall'], out=coordinate(150., 92.), way=4)
        self.plan['panels'] = [north, names]
        south = road('s', [(150., 120.), (150., 12.)], direction=1)
        self.ways['s'] = 4
        with patch.object(directions, '_settlements', return_value=[('lembach', 1300., np.array([155., 120.]))]):
            _, (sign,) = self.place([self.incoming, self.north, south],
                                    urban_fn=lambda lat, lon: np.ones_like(lat, bool))
        self.assertEqual([p['names'] for p in sign['panels']], [['Lembach', 'Mairie'], ['MATTSTALL']])
        self.assertEqual(sign['panels'][0]['distance_km'], ['2,5', '7'])

    def test_adds_a_readable_face_for_another_branch_at_a_t_junction(self):
        east = road('e', [(150., 120.), (255., 120.)])
        _, signs = self.place([self.incoming, self.north, east])
        self.assertEqual(len(signs), 2)
        other = next(s for s in signs if s['id'] != self.plan['id'])
        self.assertGreater(other['pos'][0], 150.)
        self.assertEqual(other['panels'], [{
            'names': ['MONTGON', 'LE CHESNE'], 'turn': 'right', 'source': 'route',
        }])

    def test_yield_support_is_shared_and_the_board_faces_the_driver(self):
        row = ['fr_giveway_m9c', 141., 114.9, 0., 270., 1., 2.45, 0.]
        with patch.object(frroadmesh, 'local_direction', wraps=frroadmesh.local_direction) as draw:
            mesh, (sign,) = self.place(props=[row])
        self.assertEqual(sign['support'], 'yield')
        self.assertLess(np.linalg.norm(np.asarray(sign['pos'][:2]) - row[1:3]), .03)
        self.assertGreater(abs(draw.call_args.args[2][1]), .05)
        self.assertGreater(min(p[2] for p in mesh.p), 2.2)
        _, (separate,) = self.place(props=[row[:4] + [90.] + row[5:]])
        self.assertNotIn('support', separate)

    def test_short_t_junction_uses_clear_ground_across_the_main_road(self):
        stem = road('in', [(142., 120.), (150., 120.)])
        south = road('s', [(150., 120.), (150., 12.)], direction=1)
        self.ways['s'] = 4
        self.plan['in'] = coordinate(142., 120.)
        self.plan['panels'][1].update(out=coordinate(150., 92.), way=4)
        _, (sign,) = self.place([stem, self.north, south])
        self.assertGreater(sign['pos'][0], 153.5)
        self.assertEqual({p['turn'] for p in sign['panels']}, {'left', 'right'})

    def test_production_tile_defers_and_publishes_the_mesh_with_the_diagnostics(self):
        lonlat = [
            coordinate(*p) for row in (self.incoming, self.north, self.east) for p in row[2][:, :2]
        ]
        f = mapgeom.Features(
            layer='roads',
            ids=np.array(['in', 'n', 'e']),
            kind=np.full(3, mapgeom.LINE, np.uint8),
            part_off=np.arange(4),
            ring_off=np.arange(4),
            coord_off=np.arange(0, 7, 2),
            lonlat=np.asarray(lonlat) * 1.0e-7,
            attrs={
                'class': np.array(['secondary'] * 3),
                'subtype': np.array(['road'] * 3),
                'src_record': np.array(['w1', 'w2', 'w3']),
            },
            rules={'oneway': rules([(1, 1.), (2, 1.)], 'float')},
        )
        props, signs, jobs = [], [], []
        with patch.dict(os.environ, {'BNE_EU_ROAD_DETAILS': 'on'}), patch.object(
            directions, 'nearby', return_value=[self.plan]
        ):
            roads.build_tile(
                FRAME,
                0,
                0,
                [f],
                lambda lat, lon: np.zeros_like(lat),
                walks=[],
                props=props,
                signs=signs,
                pole_profile=poleprofiles.FR_KIT,
                roadside_jobs=jobs,
            )
            self.assertFalse(
                any(record['kind'] == 'direction' for _, records in signs for record in records)
            )
            (finish,) = jobs
            finish([])
        ((mesh, records),) = signs
        self.assertTrue(mesh.t)
        (sign,) = [record for record in records if record.get('kind') == 'direction']
        self.assertEqual(sign['id'], self.plan['id'])

    def roundabout_case(self, exit_direction):
        ring = road(
            'ring',
            [(175.0, 128.0), (190.0, 113.0), (205.0, 128.0), (190.0, 143.0), (175.0, 128.0)],
            direction=1,
        )
        incoming = road('in', [(2.0, 128.0), (175.0, 128.0)])
        right = road('s', [(190.0, 113.0), (190.0, 60.0)], direction=exit_direction)
        ahead = road('e', [(205.0, 128.0), (252.0, 128.0)], direction=exit_direction)
        left = road('n', [(190.0, 143.0), (190.0, 230.0)], direction=exit_direction)
        plan = {
            'id': 'ring/1',
            'at': coordinate(175.0, 128.0),
            'in': coordinate(160.0, 128.0),
            'way': 1,
            'panels': [
                {
                    'names': [name],
                    'source': 'route',
                    'out': coordinate(*out),
                    'exit': coordinate(*fork),
                    'ring': coordinate(185.0, 118.0),
                }
                for name, fork, out in [
                    ('Épinal', (190.0, 143.0), (190.0, 165.0)),
                    ('Éloyes', (205.0, 128.0), (230.0, 128.0)),
                    ('Gare', (190.0, 113.0), (190.0, 92.0)),
                ]
            ],
        }
        return plan, [incoming, ring, right, ahead, left]

    def test_roundabout_board_uses_connected_exits_and_has_100_m_advance(self):
        plan, rows = self.roundabout_case(exit_direction=1)
        incoming, ring, right, ahead, left = rows
        mesh, records = self.place(rows, plan=plan, roundabouts={'ring'})
        local, = [r for r in records if r['kind'] == 'direction']
        advance, = [r for r in records if r['kind'] == 'roundabout-advance']
        exits = [r for r in records if r['kind'] == 'direction-exit']
        self.assertGreaterEqual(len(exits), 2)
        self.assertTrue(all(p['turn'] in {'left', 'right'} for r in exits for p in r['panels']))
        self.assertEqual(local['kind'], 'direction')
        self.assertEqual(advance['kind'], 'roundabout-advance')
        self.assertEqual(advance['distance_to_entry_m'], 100)
        self.assertEqual({p['turn'] for p in advance['panels']}, {'left', 'right', 'straight'})
        self.assertAlmostEqual(advance['pos'][0], 75.0, delta=0.1)
        self.assertTrue(mesh.t)
        _, (upstream,) = self.place(
            [incoming],
            box=(0.0, 0.0, 120.0, 256.0),
            plan=plan,
            roundabouts={'ring'},
            context_roads=rows[1:],
        )
        _, junction_signs = self.place(
            rows, box=(120.0, 0.0, 376.0, 256.0), plan=plan, roundabouts={'ring'}
        )
        junction, = [r for r in junction_signs if r['kind'] == 'direction']
        self.assertEqual(upstream['id'], advance['id'])
        self.assertEqual(junction['id'], local['id'])
        island = road('ring', ring[2][:, :2], direction=-1)
        self.assertEqual(
            self.place([incoming, island, right, ahead, left], plan=plan, roundabouts={'ring'})[1],
            [],
        )
        self.assertIsNone(frroadmesh.roundabout_layout([dict(plan['panels'][0], turn='left')]))

    def test_roundabout_completes_other_incoming_branches_but_never_an_exit_only(self):
        plan, rows = self.roundabout_case(exit_direction=0)
        rows[2] = road('s', [(190.0, 113.0), (190.0, 60.0)], direction=1)
        _, inferred = self.place(rows, plan=plan, roundabouts={'ring'})
        self.assertEqual([r['id'] for r in inferred if r['kind'] == 'direction'], ['ring/1'])
        for panel in plan['panels']:
            panel['source'] = 'road'
        _, records = self.place(rows, plan=plan, roundabouts={'ring'})
        local = [r for r in records if r['kind'] == 'direction']
        self.assertEqual(len(local), 3)
        north = next(r for r in local if r['id'].startswith('junction/n/'))
        self.assertNotIn('ÉPINAL', north['name'])
        self.assertIn('ÉLOYES', north['name'])
        self.assertFalse(any(r['id'].startswith('junction/s/') for r in records))

    def test_display_keeps_estimated_distances_aligned_with_regional_names(self):
        panel = {'source': 'route', 'names': ['Haguenau', 'Saudoy'], 'route_distances': [18_000, 0]}
        self.assertEqual(directions._display(panel, False, '')['display_distance'], ['18', ''])
        self.assertEqual(directions._display(panel, True, '')['display_distance'], ['', ''])

    def test_roundabout_without_a_plan_can_follow_a_labelled_continuation(self):
        _, rows = self.roundabout_case(exit_direction=0)
        rows.append(road('out', [(252., 128.), (319., 128.)]))
        plan = {
            'id': 'downstream',
            'at': coordinate(252., 128.),
            'in': coordinate(232., 128.),
            'way': 3,
            'panels': [{'source': 'road', 'out': coordinate(275., 128.), 'names': ['Éloyes']}],
        }
        _, records = self.place(rows, plan=plan, roundabouts={'ring'})
        exit_sign = next(r for r in records if r['id'].startswith('exit/e/'))
        self.assertEqual(exit_sign['panels'][0]['names'], ['ÉLOYES'])
        self.assertIn(exit_sign['panels'][0]['turn'], ('left', 'right'))
        rows[-1] = road('out', [(252., 128.), (319., 128.)], direction=-1)
        _, records = self.place(rows, plan=plan, roundabouts={'ring'})
        self.assertFalse(any(r['id'].startswith('exit/e/') for r in records))


class SplitExitTest(unittest.TestCase):
    def test_follows_split_parts_and_continues_after_obstructed_gore(self):
        rows = [
            road('in', [(0.0, 130.0), (30.0, 130.0)], 'motorway', 1),
            road('a', [(30.0, 130.0), (58.0, 130.0)], 'motorway', 1),
            road('b', [(58.0, 130.0), (243.0, 130.0)], 'motorway', 1),
            road('first', [(30.0, 130.0), (48.0, 125.5)], 'motorway', 1, width=4.0, link=True),
            road(
                'second',
                [(48.0, 125.5), (145.0, 99.0), (220.0, 86.0)],
                'motorway',
                1,
                width=4.0,
                link=True,
            ),
        ]
        (early,) = frgore.place(FRAME, rows, BOX, fr)
        x, y = early[1:3]
        obstruction = signplacement.Context(
            [[(x - 2, y - 2), (x + 6, y - 2), (x + 6, y + 2), (x - 2, y + 2)]]
        )
        (late,) = frgore.place(FRAME, rows, BOX, fr, obstacles=obstruction)
        self.assertGreater(late[1], x + 6.0)


if __name__ == '__main__':
    unittest.main()
