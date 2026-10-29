"""Content-sized exit panels and per-panel supports restored from the 1.7.0 build."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import exitsigns
from test_portrait_exit_signs import panel
from test_exit_signs import fixture, FRAME


class SignFitTest(unittest.TestCase):
    def test_landergin_example_does_not_scale_height_with_name_width(self):
        label=dict(ref='28',names=['Landergin'],routes=[])
        s=panel('TX',label=label)
        self.assertLess(s['height'],2.6)
        self.assertLess(s['height'],s['width']*.6)
        row,=exitsigns.advance_layout(s)['lines']
        self.assertGreaterEqual(row['max_width'],exitsigns.guide_width('Landergin',row['height']))
        # Lettering and the distance keep their cap heights; the empty panel shrinks.
        self.assertEqual(row['height'],.64)

    def test_short_names_and_text_routes_get_compact_readable_panels(self):
        for state in ('TX', 'MI', 'KY', ''):
            label = dict(ref='37', names=['Vega'], routes=[])
            simple = panel(state, label=label)
            self.assertLess(simple['height'], 2.8)
            self.assertLess(simple['width'], 3.6)
            label = dict(ref='37', names=['Vega'], routes=['I 40 BUS West'])
            s = panel(state, label=label)
            route, = exitsigns.advance_layout(s)['routes']
            self.assertGreaterEqual(route['width'], exitsigns.guide_width(route['number'], .39))
            self.assertLess(s['height'], 3.3)
            mesh = exitsigns.Mesh((0,0,0))
            with patch.object(mesh, 'guide_text', wraps=mesh.guide_text) as text:
                mesh.panel(s)
            rendered = [c.args for c in text.call_args_list if c.args[0]=='I 40 BUS West']
            self.assertEqual(len(rendered),1)
            self.assertGreaterEqual(rendered[0][3],.36)

    def test_destination_descenders_clear_footer_on_compact_signs(self):
        for kind in ('advance','decision'):
            for names in (['Vega'], ['Oak Rd'], ['A very long destination name','Gas City','Spring Valley']):
                s = panel('TX',kind,label=dict(ref='37',names=names,routes=[]))
                layout=exitsigns.advance_layout(s)
                footer_top = 1.25 if kind=='decision' else .76
                for row in layout['lines']:
                    fit=min(1.,row['max_width']/exitsigns.guide_width(row['value'],row['height']))
                    descent=max(exitsigns.guide_glyph(c)[1]['bbox'][3]/exitsigns.guide_glyph(c)[0]['capHeight']
                                for c in row['value'])*row['height']*fit
                    self.assertGreater(row['baseline']-descent,footer_top)

    def test_compact_and_multi_destination_sign_supports_follow_the_panel(self):
        rows,obj=fixture()
        labels=[(dict(ref='28',names=['Landergin'],routes=[]),2),
                (dict(ref='37',names=['Vega'],routes=['I 40 BUS West']),2),
                (dict(ref='259',names=['Upland','Gas City'],routes=['US 35 NORTH','IN 22']),3),
                (dict(ref='259',names=[],routes=['US 35 NORTH','IN 22']),3),
                (dict(ref='',names=['Rest Area','Welcome Center'],routes=[]),2)]
        for state in ('TX','KY','MI','IN'):
            for label, count in labels:
                obj['exit']['label']=label
                # Sloping terrain also checks that the additional centre post
                # gets its own base height, rather than reusing an outer post.
                def ground(la,lo):
                    x,y,_=FRAME.geodetic_to_enu(la,lo,0.)
                    return .01*x+.1*y
                with patch('signstates.states',return_value=np.array(state)):
                    signs=exitsigns.place(FRAME,[obj],rows,(-2000,-300,300,300),ground,link_ids={'ramp'})
                self.assertTrue(signs)
                for s in signs:
                    expected=count if s['kind']!='ramp' else 2
                    self.assertEqual(len(s['bases']),expected)
                    self.assertEqual(s['post_offsets'],[-.32,0.,.32] if expected==3 else [-.32,.32])
                    right=np.array([s['travel'][1],-s['travel'][0]])
                    posts=np.array(s['centre'][:2])+np.array(s['post_offsets'])[:,None]*s['width']*right
                    expected_bases=FRAME.surface_enu(posts[:,0],posts[:,1],ground)[-1]
                    np.testing.assert_allclose(s['bases'],expected_bases,atol=1e-5)
                    # Explicit placement and the mesh's default path agree.
                    for placed in (s,{k:v for k,v in s.items() if k not in ('bases','post_offsets')}):
                        mesh=exitsigns.Mesh((0,0,0))
                        with patch.object(mesh,'box',wraps=mesh.box) as box:
                            mesh.panel(placed)
                        self.assertEqual(len([c for c in box.call_args_list if c.args[2]==2]),expected)
                        _,_,_,materials,mi=mesh.result()
                        self.assertTrue(np.any(mi==materials.index('bne_guide_letters')))

if __name__ == '__main__':
    unittest.main()
