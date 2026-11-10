"""French exit announcements: correct shoulder, sloping feet and raised D."""
from pathlib import Path
import copy
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import frdestinations as dest
import frhighwaysigns as fr
import highwaysigns
import signmesh as sm
import signlayout
from test_sign_layout import FRAME, BOX, fixture, flat


class LegendTest(unittest.TestCase):
    def test_superscript_d_uses_a_small_raised_existing_D_glyph_without_question_mark(self):
        source = 'CLERMONT-Fᵈ'
        self.assertEqual(dest.legend_chars(source)[-1], ('D', .60))
        mesh = sm.Mesh()
        fr.text(mesh, source, 0., 1., .4, 10., 'green')
        p = np.asarray(mesh.p)
        glyph = sm.glyph_for('D', 'fr_motorway')
        u0, v0, u1, v1 = glyph['uv']
        np.testing.assert_allclose(np.asarray(mesh.uv)[-4:], [(u0,v0),(u1,v0),(u1,v1),(u0,v1)])
        last, previous = p[-4:], p[-8:-4]
        self.assertAlmostEqual(np.ptp(last[:, 2])/np.ptp(previous[:, 2]), .6, places=2)
        self.assertGreater(last[:, 2].min(), previous[:, 2].min())
        self.assertLess(abs(last[:, 2].max()-previous[:, 2].max()), .015)

    def test_other_superscripts_and_spaces_keep_their_place(self):
        chars = dest.legend_chars('Sᵀ VICTOR')
        self.assertEqual(chars[:3], [('S',1.), ('T',.6), (' ',1.)])
        self.assertEqual(dest.key('Clermont-Fᴰ'), dest.key('Clermont-Fᵈ'))
        self.assertTrue(dest.is_green('CLERMONT-FD'))
        self.assertTrue(dest.is_green('Clermont-Ferrand'))

    def test_green_poles_are_grouped_above_the_local_white_destination(self):
        label = dict(ref='36', names=['Clermont-Fᵈ','Bourges','Désertines'], routes=[])
        plan = fr.compose(label, True)
        registers = [p for p in plan['panels'] if not p.get('header')]
        self.assertEqual([(p['colour'], p['names']) for p in registers],
            [('green',['CLERMONT-Fᵈ','BOURGES']), ('white',['DÉSERTINES'])])
        self.assertGreater(registers[0]['bottom'], registers[1]['top'])
        self.assertEqual(registers[0]['cap'], registers[1]['cap'])

    def test_opposite_exit_names_also_split_the_green_and_white_registers(self):
        blocks = fr.registers(dict(names=['Bourges','Montluçon','Désertines'], routes=[]), exit=True)
        self.assertEqual([(b['colour'],b['names']) for b in blocks],
                         [('green',['BOURGES','MONTLUÇON']), ('white',['DÉSERTINES'])])

    def test_explicit_route_colours_and_motorway_blue_override_fallback_classification(self):
        names = ['Bourges','Désertines']
        self.assertEqual(fr.registers(dict(names=names, colour='white'), True)[0]['colour'], 'white')
        supplied = dict(names=names, fr_blocks=[dict(colour='white',names=names)])
        self.assertEqual([b['colour'] for b in fr.registers(supplied, True)], ['white'])
        self.assertEqual([b['colour'] for b in fr.registers(dict(names=['Bourges'],routes=['A71']), True)], ['blue'])
        self.assertEqual([b['colour'] for b in fr.registers(dict(names=['Bourges']), False)], ['blue'])

    def test_classification_is_exact_accent_insensitive_and_handles_bipoles(self):
        for name in ('Haguenau', 'Montlucon', 'CHALON-SUR-SAÔNE', 'METZ-NANCY'):
            self.assertTrue(dest.is_green(name), name)
        for name in ('Désertines', 'Saint-Victor', 'Bourges-Centre', 'Clermont'):
            self.assertFalse(dest.is_green(name), name)


class ShoulderTest(unittest.TestCase):
    def setUp(self):
        objects, self.rows, self.context = fixture()
        self.objects = objects[:1]       # genuinely left-hand branch
        self.objects[0]['exit']['label'] = dict(ref='36', names=['Bourges','Montluçon','Désertines'],routes=[])
        self.objects[0]['exit']['main_tags'] = dict(highway='motorway', oneway='yes', ref='A714')

    def test_french_advance_is_on_the_right_even_for_a_left_branch(self):
        sides, raised = highwaysigns.guide_layout(FRAME, self.objects, self.context, BOX, flat,
                                                 mph=False, french=True)
        signs = [s for s in sides if s['kind']=='advance']
        self.assertEqual(len(signs), 2)
        for s in signs:
            right = np.array([s['travel'][1], -s['travel'][0]])
            self.assertGreater((np.asarray(s['centre'][:2])-s['road_point'])@right, 0.)
            self.assertEqual(s['mount_side'], 1)
            self.assertEqual(s['side'], -1, 'exit direction must be retained')
        self.assertEqual(len(raised), 1)
        self.assertTrue(all(p['side']==-1 for p in raised[0]['panels'] if p['exit']))

    def test_foreign_placement_keeps_its_original_branch_side(self):
        sides, _ = highwaysigns.guide_layout(FRAME, self.objects, self.context, BOX, flat, mph=False)
        for s in sides:
            if s['kind'] != 'advance': continue
            right = np.array([s['travel'][1], -s['travel'][0]])
            self.assertLess((np.asarray(s['centre'][:2])-s['road_point'])@right, 0.)
            self.assertNotIn('mount_side', s)

    def test_uphill_shoulder_keeps_right_side_with_independent_grounded_embedded_posts(self):
        def slope(lat, lon):
            _, y, _ = FRAME.geodetic_to_enu(lat, lon, 0.)
            return np.maximum(0., -np.asarray(y)-6.)*.65
        sides, _ = highwaysigns.guide_layout(FRAME, self.objects, self.context, BOX, slope,
                                             mph=False, french=True)
        sign = next(s for s in sides if s['kind']=='advance')
        self.assertLess(sign['centre'][1], 0.)
        self.assertGreater(np.ptp(sign['bases']), .5)
        self.assertAlmostEqual(sign['bottom']-max(sign['bases']), .45, places=5)
        p, t, _, _, mi = fr.build([sign], [], (0,0,0))
        metal = p[t[mi == sm.METAL]]
        self.assertAlmostEqual(metal[:, :, 2].min(), min(sign['bases'])-.22, places=5)

    def test_overlap_shift_preserves_right_shoulder_and_road_height_on_a_bank(self):
        def slope(lat, lon):
            _, y, _ = FRAME.geodetic_to_enu(lat, lon, 0.)
            return np.maximum(0., -np.asarray(y)-6.)*.65
        sides, _ = highwaysigns.guide_layout(FRAME, self.objects, self.context, BOX, slope,
                                             mph=False, french=True)
        sign = next(s for s in sides if s['kind']=='advance')
        duplicate = copy.deepcopy(sign)
        duplicate['id'] = -300
        duplicate['label']['ref'] = '37'
        objects = {o['id']: o for o in self.objects}
        objects[-300] = dict(self.objects[0], id=-300, exit=dict(
            self.objects[0]['exit'], label=duplicate['label']))
        actual, _ = signlayout.arrange(FRAME, [sign, duplicate], [],
            {oid:self.rows for oid in objects}, objects, slope, ())
        self.assertEqual(len(actual), 2)
        moved = next(s for s in actual if s['id']==sign['id'])
        self.assertLess(moved['centre'][0], sign['centre'][0])
        self.assertAlmostEqual(moved['centre'][1], sign['centre'][1])
        self.assertAlmostEqual(moved['road_point'][1], 0.)
        self.assertEqual(moved['mount_side'], 1)
        self.assertAlmostEqual(moved['bottom']-max(moved['bases']), .45, places=5)


if __name__ == '__main__':
    unittest.main()
