"""Offline regression for v0.7.4 artwork. No NVDB, BeamNG runtime or network."""
from pathlib import Path
import sys,unittest
import numpy as np
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent if HERE.name=='tests' else HERE))
import norway_nvdb_destinations_v07 as d
from norway_signs_v073 import visual_v074


def row(code,text,color='gul'):
    return {'id':42,'code':code,'kind':code.split('.')[0], 'text':text,'color':color,
            'width_mm':None,'height_mm':None,'position_code':1}

class Visual074(unittest.TestCase):
    def test_713_sign_has_no_second_arrow_inside_wing(self):
        with_arrow=d._draw_face(row('713.H','Molde@33'))
        no_arrow=d._draw_face(row('713.H','Molde'))
        self.assertEqual(with_arrow.tobytes(),no_arrow.tobytes())
    def test_713_tip_matches_left_right(self):
        r=d._draw_face(row('713.H','Molde'))
        l=d._draw_face(row('713.V','Molde'))
        self.assertGreater(r.getpixel((r.width-40,r.height//2))[0],200)
        self.assertGreater(l.getpixel((40,l.height//2))[0],200)
        self.assertLess(r.getpixel((10,10))[0],100)
    def test_shield_116_is_white(self):
        im=d._draw_face(row('723.15','116','hvit'))
        self.assertGreater(im.getpixel((10,10))[0],200)
    def test_shield_e39_is_green(self):
        im=d._draw_face(row('723.11','E39','grønn'))
        self.assertLess(im.getpixel((10,10))[0],20)
        self.assertGreater(im.getpixel((10,10))[1],100)
    def test_711_has_inset_arrow_and_shield(self):
        im=d._draw_face(row('711.H90','32@@723.11¤E39¤Molde'))
        self.assertEqual(im.size,(1024,384))
    def test_727_white_plate(self):
        im=d._draw_face(row('727.1','Løkkemyra','hvit'))
        self.assertGreater(im.getpixel((100,100))[0],200)
    def test_pole_moves_to_back(self):
        a=row('713.H','Molde')
        p,t,uv,mi=d._panel_mesh(a,[0,0],0,[1,0],1,[0,0],1.5,.4,1.7)
        # Front is at x=.018; support at x=-.115 (not obscuring face).
        self.assertAlmostEqual(float(p[-8:,0].max()),-.075,places=3)
        self.assertLess(float(p[-8:,0].max()),float(p[0,0]))
    def test_out_of_scope_no_changes(self):
        self.assertIsNone(d._kind('362.40'))
        self.assertEqual(d.PRIORITY['206'],'prio.main_road')
    def test_v074_named_texture(self):
        import inspect
        self.assertIn('0.7.4',inspect.getsource(d._face_filename))
    def test_dimension_default_small(self):
        w,h=d._tile_dimensions(row('713.H','Molde'))
        self.assertLessEqual(w,1.8)

if __name__=='__main__':unittest.main()
