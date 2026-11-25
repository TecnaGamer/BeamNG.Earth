"""v0.7.2 regression tests for Norwegian destination sign layouts and route markers."""
from pathlib import Path
from unittest.mock import patch
import unittest
import sys
import numpy as np
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent if HERE.name=='tests' else HERE))
import norway_nvdb_destinations_v07 as d


def row(code, text, color='gul'):
    return dict(id=123,kind=code.split('.')[0],code=code,text=text,color=color,
                width_mm=None,height_mm=None,position_code=5)

class NorwegianSignRedesignTests(unittest.TestCase):
    def test_723_county_116_not_symbol(self):
        self.assertEqual(d._render_lines(row('723.15','116','hvit')),
                         [[['text','116']]])
        im=d._draw_face(row('723.15','116','hvit'))
        self.assertEqual(im.size,(512,256))
    def test_e39_green_european_shield(self):
        im=d._draw_face(row('723.11','E39','grønn'))
        self.assertEqual(im.getpixel((10,10))[1],115)
    def test_713_no_duplicate_painted_arrow(self):
        lines=d._runs_plain(d.parse_text('Blakstad@31'),shape='713')
        self.assertEqual(lines,[[('text','Blakstad')]])
    def test_713_right_black_border_and_text(self):
        im=d._draw_face(row('713.H','Molde@723.11¤E39¤'))
        self.assertLess(im.getpixel((10,10))[0],100)
        self.assertGreater(im.getpixel((1000,128))[0],150)
    def test_713_left_arrow_text_and_geometry(self):
        im=d._draw_face(row('713.V','Kristiansund@723.11¤E39¤'))
        self.assertGreater(im.getpixel((25,128))[0],190)
    def test_711_board_keeps_arrow_code(self):
        im=d._draw_face(row('711.H90','32@@723.11¤E39¤Molde'))
        self.assertEqual(im.size,(1024,384))
        self.assertEqual(d.parse_text('32')[0][0][0],'arrow')
    def test_727_white_town_name(self):
        im=d._draw_face(row('727.1','Batnfjord','hvit'))
        self.assertGreater(im.getpixel((40,40))[0],235)
    def test_no_speed_signs(self):
        self.assertIsNone(d._kind('362.60'))
    def test_coincident_shield_filter_is_removed(self):
        import inspect
        src=inspect.getsource(d.build_tile)
        self.assertNotIn("if row['kind']=='723' and any",src)
        self.assertIn('shares_priority',src)
    def test_angle_is_explicitly_provisional(self):
        self.assertIn('provisional',d.face_of.__doc__.lower())

if __name__=='__main__':unittest.main()
