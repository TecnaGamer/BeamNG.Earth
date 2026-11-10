"""French motorway faces: actual triangle winding, mounts and roadside roles."""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import exitsigns
import frhighway_refs as references
import frhighwaysigns as fr
import frmotorway_layout
import highwaysigns
import signmesh as sm
import throughsigns
from test_sign_layout import FRAME, BOX, fixture, flat, ll


class CompositionTest(unittest.TestCase):
    def test_reference_has_separate_registers_header_and_one_arrow_per_lane(self):
        r = references.catalogue()[0]
        main = fr.compose(r['main'], lanes=3)
        exit_plan = fr.compose(dict(ref='26', **r['exit']), True, lanes=3)
        self.assertEqual([b['colour'] for b in main['panels']], ['blue', 'blue'])
        self.assertEqual([b['arrow_lanes'] for b in main['panels']], [0, 3])
        self.assertEqual([b['colour'] for b in exit_plan['panels']],
                         ['white', 'green', 'white', 'white', 'yellow'])
        self.assertEqual(main['routes'], ['E15', 'E21', 'A6'])
        self.assertTrue(exit_plan['panels'][0]['header'])
        self.assertGreater(main['width']/main['height'], 1.8)
        for plan in (main, exit_plan):
            for a, b in zip(plan['panels'], plan['panels'][1:]):
                self.assertGreaterEqual(a['bottom'], b['top']+fr.GAP-1e-9)

    def test_front_facing_arrow_heads_and_correct_downward_exit_direction(self):
        for side in (0, 1, -1):
            mesh = sm.Mesh()
            fr.arrow(mesh, 0., 0., .8, side, 'white')
            p, t = np.asarray(mesh.p), np.asarray(mesh.t)
            normal = np.cross(p[t[:, 1]]-p[t[:, 0]], p[t[:, 2]]-p[t[:, 0]])
            self.assertTrue(np.all(normal[:, 1] < 0.), 'every arrow triangle must face the driver')
            tip = p[5]                     # triangle's point, after four shaft vertices
            shaft = p[:4].mean(0)
            self.assertLess(tip[2], shaft[2])
            if side:
                self.assertGreater(side*(tip[0]-shaft[0]), .1)
            else:
                self.assertAlmostEqual(tip[0], shaft[0])

    def test_short_blue_stack_is_connected_to_the_beam_of_a_taller_exit_stack(self):
        r = references.catalogue()[0]
        panels = fr.planned_panels(dict(names=['PARIS'], routes=['A6']), dict(ref='26', **r['exit']), 2, '', 1)
        sign = fr.finalize(dict(panels=panels, centre=[0, 0, 0], bottom=6.,
                                poles=[-9., 12.], bases=[0., 0.], distance='', side=1))
        beam, mounts = fr.mounts(sign)
        short = sign['panels'][0]
        self.assertGreater(beam, 6.+short['fr_plan']['body_height'])
        for p in sign['panels']:
            connected = [m for m in mounts if abs(m['x']-p['offset']) < p['width']/2]
            self.assertEqual(len(connected), 2)
            for m in connected:
                self.assertLess(m['bottom'], 6.+p['fr_plan']['body_height'])
                self.assertGreater(m['top'], beam)

    def test_exit_badge_outline_is_in_front_of_the_header_not_coplanar(self):
        mesh = sm.Mesh()
        fr.exit_badge(mesh, '26', 0., 1.)
        p = np.asarray(mesh.p)
        triangles = np.asarray(mesh.t)[np.asarray(mesh.tm) == sm.BLACK]
        self.assertTrue(len(triangles))
        self.assertLess(p[triangles, 1].max(), -.028)

    def test_unknown_destinations_never_become_a_square_a6_filler_board(self):
        self.assertIsNone(fr.compose(dict(names=['A6'], routes=['A6'], route_only=True)))
        exit_plan = fr.compose(dict(ref='26', names=[]), True)
        self.assertIsNotNone(exit_plan)
        self.assertEqual(len(exit_plan['panels']), 1)
        self.assertTrue(exit_plan['panels'][0]['header'])

    def test_side_advance_and_distance_confirmation_have_no_lane_arrows(self):
        r = references.catalogue()[0]
        advance = fr.compose(dict(ref='26', **r['exit']), True, '900 m', roadside=True)
        header = advance['panels'][0]
        self.assertEqual(header['distance'], '900 m')
        self.assertFalse(header['exit_arrow'])
        confirmation = fr.compose(dict(names=['PARIS', 'BESANÇON', 'DIJON', 'BEAUNE'],
            routes=['E15', 'E21', 'A6'], distances=['340', '135', '70', '35']),
            roadside=True, confirmation=True)
        self.assertEqual(len(confirmation['panels'][0]['names']), 4)
        self.assertEqual(confirmation['panels'][0]['distances'], ['340', '135', '70', '35'])
        self.assertFalse(any(p.get('arrow_lanes') for p in confirmation['panels']))

    def test_cantilever_has_one_ground_column_and_still_spans_every_panel(self):
        r = references.catalogue()[1]
        s = fr.finalize(dict(centre=[0, 0, 0], bottom=6., poles=[-8., 10.], bases=[0., 0.],
            fr_support='cantilever', panels=fr.planned_panels(r['main'], dict(ref='18', **r['exit']), 2, '', 1)))
        structure = fr.support_plan(s)
        self.assertEqual(structure['posts'], [(10., 0.)])
        for panel in s['panels']:
            self.assertLess(structure['left'], panel['offset']-panel['width']/2)
            self.assertGreater(structure['right'], panel['offset']+panel['width']/2)


class PlacementTest(unittest.TestCase):
    def objects(self):
        objs, rows, context = fixture()
        objs = objs[1:]
        objs[0]['exit']['main_tags'] = dict(highway='motorway', ref='A4', lanes='3', destination='PARIS')
        return objs, rows, context

    def test_renderer_envelopes_fit_between_the_supports_used_by_placement(self):
        objs, _, context = self.objects()
        _, overheads = highwaysigns.guide_layout(FRAME, objs, context, BOX, flat, mph=False, french=True)
        self.assertEqual(len(overheads), 1)
        for sign in overheads:
            for p in sign['panels']:
                self.assertEqual(p['width'], p['fr_plan']['width'])
                self.assertEqual(p['height'], p['fr_plan']['height'])
                self.assertLess(sign['poles'][0], p['offset']-p['width']/2)
                self.assertGreater(sign['poles'][1], p['offset']+p['width']/2)

    def test_blocked_decision_station_tries_upstream_without_losing_the_gantry(self):
        objs, _, context = self.objects()
        deck = dict(deck=True, n=[[-40., -30., 8.], [-40., 30., 8.]], w=15.)
        _, overheads = highwaysigns.guide_layout(FRAME, objs, context, BOX, flat,
                                                chunks=[deck], mph=False, french=True)
        self.assertEqual(len(overheads), 1)
        self.assertLess(overheads[0]['centre'][0], -60.)
        self.assertFalse(exitsigns._on_deck(np.array(overheads[0]['centre'][:2]), [deck], 3.))

    def test_known_distances_generate_a_blue_confirmation_on_the_clear_shoulder(self):
        _, rows, context = self.objects()
        obj = dict(id=-999, through=dict(main_ids=[100], point=ll(-400., 0.),
            approach=[ll(-500., 0.), ll(-300., 0.)], label=dict(ref='', names=['PARIS','DIJON'],
                routes=['A6'], distances=['340', '70'])))
        sides, raised = highwaysigns.guide_layout(FRAME, [obj], context, BOX, flat, mph=False, french=True)
        self.assertFalse(raised)
        self.assertEqual(len(sides), 1)
        self.assertEqual(sides[0]['kind'], 'confirmation')
        self.assertTrue(exitsigns._clear(np.array(sides[0]['centre'][:2]), sides[0]['width']/2+.5,
                                         exitsigns._outlines(rows)))
        mesh = fr.build(sides, [], (0, 0, 0))
        self.assertTrue(np.any(mesh[4] == fr.BLUE))

    def test_source_distances_are_retained_but_absent_or_mixed_units_are_not_invented(self):
        base = dict(highway='motorway', oneway='yes', ref='A6', int_ref='E15;E21',
                    destination='Paris;Besançon;Dijon;Beaune')
        content = throughsigns.label(dict(base, **{'destination:distance': '340;135;70;35'}))
        full = fr.full_label(content)
        self.assertEqual(full['names'], ['Paris', 'Besançon', 'Dijon', 'Beaune'])
        self.assertEqual(full['distances'], ['340', '135', '70', '35'])
        self.assertNotIn('distances', throughsigns.label(base))
        self.assertNotIn('distances', throughsigns.label(dict(base, **{'destination:distance': '340 mi;135;70;35'})))


class ReferenceTest(unittest.TestCase):
    def test_three_photo_examples_match_only_the_local_exit_and_travel_direction(self):
        for r in references.catalogue():
            p = np.asarray(r['junction'])
            scale = [111320*np.cos(np.radians(p[1])), 110540.]
            start = p-np.asarray(r['travel'])*1000/scale
            obj = dict(id=-1, exit=dict(approach=[start.tolist(), p.tolist()],
                label=dict(ref=r['exit_ref'], names=['source exit'], routes=[])))
            before = copy.deepcopy(obj)
            got = references.enrich([obj])[0]
            self.assertEqual(got['exit']['fr_reference'], r['id'])
            self.assertEqual(obj, before)
            reverse = copy.deepcopy(obj)
            reverse['exit']['approach'] = [(p+np.asarray(r['travel'])*1000/scale).tolist(), p.tolist()]
            self.assertEqual(references.enrich([reverse]), [reverse])
            far = copy.deepcopy(obj)
            far['exit']['approach'] = (np.asarray(far['exit']['approach'])+[.1, .1]).tolist()
            self.assertEqual(references.enrich([far]), [far])

    def test_new_materials_have_packaged_textures_and_expected_blue(self):
        import json
        from PIL import Image
        root = Path(__file__).resolve().parents[2]
        art = root/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/regional_signs'
        mats = json.loads((art/'main.materials.json').read_text())
        for name in fr.MATERIALS:
            self.assertIn(name, mats)
        blue = np.asarray(mats['bne_fr_highway_blue']['Stages'][0]['baseColorFactor'][:3])
        srgb = np.where(blue <= .0031308, 12.92*blue, 1.055*blue**(1/2.4)-.055)
        np.testing.assert_allclose(srgb*255, [12,67,180], atol=.001)
        data = sm.font()
        self.assertIn('fr_motorway', data['fonts'])
        with Image.open(art/'sign_font_opacity.data.png') as image:
            self.assertEqual(image.size, tuple(data['atlas_size']))
        self.assertLessEqual(max(data['atlas_size']), 4096)


if __name__ == '__main__':
    unittest.main()
