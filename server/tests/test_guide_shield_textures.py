"""Shared shield artwork on existing green guide boards."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import exitsigns
import throughsigns
import highwayroutes
import highwaysigns
from types import SimpleNamespace
import routefaces
from test_advance_sign_layout import sign


class GuideTexturesTest(unittest.TestCase):
    def test_mixed_green_exit_uses_wy_art_and_preserves_federal_meshes(self):
        board = sign(routes=('WY 210', 'US 30', 'I 80'))
        board['state'] = 'WY'
        before = exitsigns.advance_layout(board)
        with tempfile.TemporaryDirectory() as tmp:
            faces = {}
            mesh = throughsigns.build([board], [], (0,0,0), tmp, '/cache', faces)
            name, texture = routefaces.face(highwayroutes.panel(highwayroutes.Route('US:WY','210')), tmp, '/cache')
            self.assertEqual(faces, {name: texture})
            self.assertIn(name, mesh[3])
            self.assertIn(exitsigns.GUIDE_MATERIALS.index('bne_guide_red'), mesh[4])
            self.assertEqual(exitsigns.advance_layout(board), before)
            selected = mesh[1][mesh[4] == mesh[3].index(name)]
            vertices = mesh[0][selected]
            normals = np.cross(vertices[:,1]-vertices[:,0],vertices[:,2]-vertices[:,0])
            self.assertTrue(np.all(normals[:,1] < 0), 'faces point toward approaching traffic')
            uv = mesh[2][selected]
            self.assertTrue(np.all((uv >= -1e-6) & (uv <= 1+1e-6)))
            with mock.patch('imagery.write_dds') as write:
                throughsigns.build([board], [], (0,0,0), tmp, '/cache', {})
                write.assert_not_called()

    def test_different_numbers_reuse_a_blank_without_contaminating_its_artwork(self):
        routefaces.raster.cache_clear(); routefaces._blank.cache_clear()
        try:
            with mock.patch.object(routefaces.Image, 'open', wraps=routefaces.Image.open) as opened:
                one=routefaces.raster('US:WY','210')
                two=routefaces.raster('US:WY','211')
                # The lettering atlas may load once as well as the blank artwork.
                paths=[Path(call.args[0]) for call in opened.call_args_list]
                self.assertEqual(paths.count(routefaces.DATA/'wy.png'),1)
                self.assertNotEqual(one,two)
                self.assertEqual(routefaces._blank.cache_info().hits,1)
            routefaces.raster.cache_clear()
            self.assertEqual(routefaces.raster('US:WY','210'),one)
        finally:
            routefaces.raster.cache_clear(); routefaces._blank.cache_clear()

    def test_overhead_through_and_exit_panels_register_shared_faces(self):
        for is_exit in (False, True):
            board = dict(centre=[0.,0.,0.], travel=[0.,1.], bottom=5.5,
                         poles=[-5.,5.], bases=[0.,0.], side=1, state='WY', distance='1 MILE',
                         panels=[dict(offset=0., width=7., height=5., exit=is_exit,
                                      label=dict(ref='10' if is_exit else '', names=['Cheyenne'],
                                                 routes=['WY 210','US 30','I 80'], direction='EAST'))])
            with tempfile.TemporaryDirectory() as tmp:
                faces = {}
                mesh = throughsigns.build([], [board], (0,0,0), tmp, '/cache', faces)
                self.assertEqual(len(faces), 1)
                self.assertIn(next(iter(faces)), mesh[3])

    def test_special_networks_and_border_identity(self):
        routes = exitsigns.route_symbols(['FM 1234','CO 14','I 80'], 'WY')
        self.assertEqual([highwayroutes.identity(r)[0] for r in routes], ['US:TX:FM','US:CO','US:I'])
        self.assertEqual(exitsigns.route_symbols(['SR 210'],'WY')[0]['state'], 'WY')
        label = throughsigns.label(dict(highway='motorway', oneway='yes', ref='80', destination='Laramie'),
                                   [dict(type='route', route='road', network='US:I', ref='80')])
        self.assertEqual(exitsigns.route_symbols(label['routes'],'WY')[0]['kind'], 'interstate')

    def test_french_mode_retains_the_current_renderer_and_does_not_generate_us_faces(self):
        source=SimpleNamespace(context=lambda *args:None, warned=-float('inf'))
        md=SimpleNamespace(highway_signs=source)
        board=dict(centre=[0.,0.,0.]); mesh=object()
        with mock.patch.object(highwaysigns,'tile_objects',return_value=[dict(id=1)]), \
             mock.patch.object(highwaysigns,'guide_layout',return_value=([board],[])), \
             mock.patch.object(highwaysigns.milemarkers,'unique_objects',return_value=[]), \
             mock.patch('euguardrails.country_at',return_value='FR'), \
             mock.patch('frroadmesh.build_highways',return_value=mesh) as french, \
             mock.patch.object(throughsigns,'build') as american:
            from test_roads import FRAME,flat
            result=highwaysigns.build_tile(md,FRAME,0,0,flat,french=True,
                                           out_dir='unused',vfs_dir='/unused',faces={})
        self.assertIs(result[0],mesh);self.assertIsNone(result[1])
        french.assert_called_once();american.assert_not_called()

    def test_composite_route_labels_keep_the_compact_fallback_and_special_shields_keep_direction(self):
        route,=exitsigns.route_symbols(['I 40 BUS West'],'TX')
        self.assertEqual((route['kind'],route['number']),('text','I 40 BUS West'))
        route,=exitsigns.route_symbols(['FM 982 NORTH'],'TX')
        self.assertEqual((route['network'],route['number'],route['qualifier']),('US:TX:FM','982','NORTH'))

    def test_artwork_preserves_aspect_inside_reserved_inset(self):
        with tempfile.TemporaryDirectory() as tmp:
            mesh = exitsigns.Mesh((0,0,0), tmp, '/cache', {})
            mesh.centre = np.zeros(3); mesh.face = np.array([0.,-1.,0.]); mesh.right = np.array([1.,0.,0.])
            route = exitsigns.route_symbols(['CO 123'],'WY')[0]
            mesh.route_shield(dict(route,y=0.,bottom=0.,width=1.2,height=.95))
            vertices = np.asarray(mesh.p)
            width, height = np.ptp(vertices[:,0]), np.ptp(vertices[:,2])
            template = highwayroutes.registry()['templates'][highwayroutes.template_for('US:CO','123')]
            self.assertLessEqual(width, 1.2+1e-6)
            self.assertLessEqual(height, .95+1e-6)
            self.assertAlmostEqual(width/height, template.get('aspect',1.))


if __name__ == '__main__':
    unittest.main()
