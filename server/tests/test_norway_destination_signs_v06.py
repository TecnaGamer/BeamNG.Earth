"""Offline contracts for Norwegian direction boards; no native engine required."""
import json
import os
from pathlib import Path
import unittest

SERVER = Path(__file__).resolve().parents[1]


class NorwegianDirectionBoardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        style = json.loads((SERVER / 'data' / 'dirsigns_styles.json').read_text(encoding='utf-8'))
        cls.no = style['countries']['NO']

    def test_yellow_on_ordinary_roads(self):
        for kind in ('road', 'urban'):
            c = self.no['board'][kind]
            self.assertGreater(c['bg'][0], 200)
            self.assertGreater(c['bg'][1], 120)
            self.assertLess(c['bg'][2], 50)
            self.assertEqual(c['ink'], [0, 0, 0])

    def test_blue_motorway_signs(self):
        row = self.no['board']['freeway']
        self.assertGreater(row['bg'][2], row['bg'][0])
        self.assertEqual(row['ink'], [255, 255, 255])

    def test_e_road_green_plate(self):
        e = self.no['plates']['e']
        self.assertGreater(e[1][1], e[1][0])
        self.assertEqual(e[2], [255, 255, 255])

    def test_route_numbers_optional_not_guessed(self):
        self.assertTrue(any(n['network'] == 'e-road' for n in self.no['networks']))
        self.assertTrue(all('network' in n and 'plate' in n for n in self.no['networks']))

    def test_uses_existing_renderer(self):
        text = (SERVER / 'signstream.py').read_text(encoding='utf-8')
        self.assertIn('BNE_NORWAY_SIGNS_V06', text)
        self.assertIn('(dirs or no_dirs)', text)
        self.assertIn('(rsigns or no_signs)', text)

    def test_does_not_modify_speed_sign_engine(self):
        text = (SERVER / 'signstream.py').read_text(encoding='utf-8')
        self.assertIn('speeds = _speeds(', text)
        self.assertIn('roadsigns.build_tile', (SERVER/'signstream.py').read_text(encoding='utf-8'))

    def test_no_english_subtitle_for_norwegian_names(self):
        s = (SERVER / 'dirsigns.py').read_text(encoding='utf-8')
        self.assertIn("show_en and cc != 'NO' and not town", s)


if __name__ == '__main__':
    unittest.main()
