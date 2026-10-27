"""Kentucky treatment: threshold, openings, shared collision/sign mounts and both faces."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy
import medianbarriers as barriers
import milemarkers
import roadmesh
import roads
import signroads

FRAME = geodesy.EnuFrame(37.1, -86.2)
BOX = (-50., -250., 50., 250.)


def flat(la, lo):
    return np.zeros(np.shape(la))


def pair(gap=4., reverse=False, start=-350., end=350.):
    rows = []
    for fid, x, direction in [('north', (12.+gap)/2., 1), ('south', -(12.+gap)/2., -1)]:
        line = np.array([[x, start, 0.], [x, end, 0.]])
        if reverse:
            line = line[::-1]
            direction *= -1
        rows.append((fid, signroads.Spec(12., 'motorway', True, direction, False), line, 12.))
    return rows


def marker(mile=46., direction='N', y=50.):
    la, lo, _ = FRAME.enu_to_geodetic(8., y, 0.)
    return milemarkers._object('KY', 65, mile, direction, [lo, la],
                               [0., 1. if direction == 'N' else -1.], 'dot:ky')


class LayoutTest(unittest.TestCase):
    def setUp(self):
        # Route identification is verified separately against the real bundled inventory.
        self.enterContext(mock.patch.object(barriers, 'corridor', return_value=[(65, np.array([[0., -500.], [0., 500.]]))]))
        self.enterContext(mock.patch.object(barriers, 'identify', side_effect=lambda f,p,d,c: (65, (p[-1,1]-p[0,1])*d > 0)))

    def test_edge_gap_is_strictly_under_fifteen_and_never_in_travel_lanes(self):
        for gap, expected in ((1.1, True), (4., True), (14.99, True), (15., False), (20., False), (.5, True), (.25, False)):
            with self.subTest(gap=gap):
                walls = barriers.runs(FRAME, pair(gap), BOX, flat)
                self.assertEqual(bool(walls), expected)
                if walls:
                    p = np.concatenate([w['n'] for w in walls])
                    np.testing.assert_allclose(p[:, 0], 0., atol=1e-8)
                    mesh = barriers.build(walls,(0.,0.,0.))
                    self.assertLess(np.max(np.abs(mesh[0][:,0])), gap/2.)

    def test_geometry_direction_and_adjacent_tile_seams_do_not_duplicate_or_move_wall(self):
        whole = barriers.runs(FRAME, pair(), BOX, flat)
        reversed_ = barriers.runs(FRAME, pair(reverse=True), BOX, flat)
        np.testing.assert_allclose(whole[0]['n'], np.array(reversed_[0]['n'])[::-1], atol=1e-8)
        a = barriers.runs(FRAME, pair(), (-50., -250., 50., 0.), flat)
        b = barriers.runs(FRAME, pair(), (-50., 0., 50., 250.), flat)
        np.testing.assert_allclose(a[0]['n'][-1], b[0]['n'][0])
        self.assertAlmostEqual(a[0]['top'][-1], b[0]['top'][0])

    def test_crossover_stays_open_and_unpaired_mainline_gets_no_wall(self):
        sp = signroads.Spec(6., 'service', False, 0, False)
        crossover = ('cross', sp, np.array([[-30., 0., 0.], [30., 0., 0.]]), 6.)
        walls = barriers.runs(FRAME, pair()+[crossover], BOX, flat)
        self.assertEqual(len(walls), 2)
        self.assertLess(walls[0]['n'][-1][1], -7.)
        self.assertGreater(walls[1]['n'][0][1], 7.)
        self.assertEqual(barriers.runs(FRAME, pair()[:1], BOX, flat), [])

    def test_one_blue_duplex_post_on_actual_wall_top_with_fraction_preserved(self):
        objects = [marker(46.2), marker(46.2, 'S'), marker(46.2)]
        signs = milemarkers.place(FRAME, objects, pair(), BOX, flat)
        self.assertEqual(len(signs), 1)
        sign = signs[0]
        self.assertTrue(sign['double_sided'])
        self.assertEqual(sign['mile'], 46.2)
        self.assertEqual(sign['style'], 'ky_blue')
        self.assertAlmostEqual(sign['centre'][0], 0.)
        self.assertAlmostEqual(sign['bottom']-sign['centre'][2], .55)
        self.assertGreater(sign['travel'][1], .99)
        walls = barriers.runs(FRAME, pair(), BOX, flat)
        self.assertAlmostEqual(sign['centre'][2], np.interp(sign['centre'][1],
                               np.array(walls[0]['n'])[:,1], walls[0]['top']), places=6)

    def test_marker_has_one_tile_owner_and_none_in_openings_or_wide_medians(self):
        obj = marker(46.4, y=0.)
        signs = [s for box in ((-50.,-250.,50.,0.), (-50.,0.,50.,250.))
                 for s in milemarkers.place(FRAME, [obj], pair(), box, flat)]
        self.assertEqual(len(signs), 1)
        self.assertEqual(milemarkers.place(FRAME, [obj], pair(15.), BOX, flat), [])
        crossover = ('cross', signroads.Spec(6., 'service', False, 0, False),
                     np.array([[-30.,0.,0.], [30.,0.,0.]]), 6.)
        self.assertEqual(milemarkers.place(FRAME, [obj], pair()+[crossover], BOX, flat), [])

    def test_wall_mesh_is_closed_outward_facing_and_announced_as_collision(self):
        walls = barriers.runs(FRAME, pair(), BOX, flat)
        mesh = roadmesh.deck(walls, (0.,0.,0.))
        p,t,uv,mats,mi = mesh
        self.assertTrue(np.isfinite(p).all())
        self.assertEqual(mats, [barriers.MATERIAL])
        normals = np.cross(p[t[:,1]]-p[t[:,0]], p[t[:,2]]-p[t[:,0]])
        centres = p[t].mean(1)
        self.assertTrue((normals[centres[:,2] > 1.3,2] >= 0.).all())
        self.assertTrue((normals[centres[:,2] < -.14,2] <= 0.).all())
        self.assertTrue((normals[centres[:,0] > .44,0] > 0.).all())
        self.assertTrue((normals[centres[:,0] < -.44,0] < 0.).all())
        with tempfile.TemporaryDirectory() as tmp:
            path,_ = roads.write_tile(tmp, '/session', 1, 0, 0, walls, deck_mesh=mesh)
            result = json.loads((Path(tmp)/Path(path).name).read_text())
            self.assertIn('deck', result)
            self.assertIn('deck', result['colliders'])
            self.assertEqual(result['chunks'], [])


class InventoryAndPanelTest(unittest.TestCase):
    def test_posted_direction_follows_chainage_even_on_a_southward_bend(self):
        frame = geodesy.EnuFrame(37.,-86.)
        coords = {}
        for i in range(5):
            la,lo,_ = frame.enu_to_geodetic(0., -i*milemarkers.MILE_M, 0.)
            coords[i] = [lo,la]
        ways = [(1,list(coords),dict(highway='motorway',oneway='yes',ref='I 65'))]
        anchors = {10:(*coords[0],dict(distance='10 mi')),11:(*coords[4],dict(distance='14 mi'))}
        out = milemarkers.osm_records(ways,coords,anchors,[(1,65,'S','KY')],mile_step=.2,
                                      route_directions={65:'N'})
        self.assertEqual(len(out),21)
        self.assertTrue(all(o['mile_marker']['direction'] == 'N' for o in out))
        self.assertTrue(all(o['mile_marker']['axis'][1] < -.99 for o in out))

    def test_all_fifths_between_dot_endpoints_exist_and_follow_route_geometry(self):
        paths, _, stations = barriers.inventory()
        for route, end in ((65,137), (75,191.6)):
            rows = stations[stations[:,0] == route]
            self.assertEqual([round(m*5) for m in rows[:,1]], list(range(round(end*5)+1)))
            xy = milemarkers._metric(rows[:,2:4], rows[0,2:4])
            distances = np.linalg.norm(np.diff(xy,axis=0),axis=1)
            self.assertTrue((distances > 270.).all(), distances.min())
            self.assertTrue((distances < 365.).all(), distances.max())
            # This uses the actual corpus and route lookup rather than mocks.
            sample = rows[len(rows)//3]
            frame = geodesy.EnuFrame(sample[3],sample[2])
            corridors = barriers.corridor(frame, (-100.,-100.,100.,100.))
            r, line = min(corridors, key=lambda r: barriers.project(np.zeros((1,2)),r[1])[2][0])
            ident = barriers.identify(frame, np.c_[line,np.zeros(len(line))], 1, corridors)
            self.assertEqual(ident[0], route)

    def test_both_faces_have_correct_direction_shield_whole_mile_and_decimal(self):
        sign = dict(centre=[0.,0.,1.37], travel=[0.,1.], bottom=1.92, width=.61, height=2.44,
                    route=65, mile=46., direction='NORTH', style='ky_blue', double_sided=True)
        mesh = milemarkers.Mesh((0.,0.,0.))
        with mock.patch.object(mesh, 'text', wraps=mesh.text) as text:
            mesh.panel(sign)
        self.assertEqual([c.args[0] for c in text.call_args_list],
                         ['NORTH','INTERSTATE','65','MILE','46','.0',
                          'SOUTH','INTERSTATE','65','MILE','46','.0'])
        p,t,uv,mats,mi = mesh.result()
        normals = np.cross(p[t[:,1]]-p[t[:,0]],p[t[:,2]]-p[t[:,0]])
        blue = normals[mi == mats.index('bne_mile_blue')]
        self.assertTrue((blue[:,1] > 0).any())
        self.assertTrue((blue[:,1] < 0).any())
        self.assertFalse((mi == mats.index('bne_mile_green')).any())
        self.assertNotEqual(milemarkers.marker_id('KY',65,46.,'N'), milemarkers.marker_id('KY',65,46.2,'N'))


if __name__ == '__main__':
    unittest.main()
