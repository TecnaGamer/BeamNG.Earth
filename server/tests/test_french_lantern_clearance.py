"""French village lanterns must clear every carriageway before emitting lights."""
from pathlib import Path
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'server'), str(ROOT/'tools')]
import furniture
import poleprofiles
import roads
import build_french_furniture as assets
from test_furniture import road


class LanternClearanceTest(unittest.TestCase):
    def fixture(self, sidewalk):
        fid = next('lantern-road-'+str(i) for i in range(100)
                   if furniture._h01('lantern-road-'+str(i), 'lantern') < .7)
        base = road(fid, 'residential', True)
        side = 1 if furniture._h01(fid, 'side') < .5 else -1
        walks = [dict(fid=fid, pts=roads.offset_polyline(base[2], side*5.25),
                      side=side, w=2.5, caps=(False,False))] if sidewalk else []
        settings = dict(town=True, buried=1., pole_profile=poleprofiles.FR_KIT)
        return base, walks, settings

    def test_lantern_post_is_rejected_inside_a_long_road_segment(self):
        clear = furniture.RoadClearance([(4., np.array([[-500.,0.,0.],[500.,0.,0.]]))])
        row = ['fr_lantern',0.,0.,0.,0.,1.,5.4,0.]
        self.assertFalse(clear.clear(row))
        row[2] = 4.18  # pivot is outside, but post plus the normal safety gap is not
        self.assertFalse(clear.clear(row))
        row[2] = 4.4
        self.assertTrue(clear.clear(row), 'lanterns on a clear pavement are retained')

    def test_collision_envelope_covers_the_generated_asset_at_catalogue_height(self):
        self.assertIn('fr_lantern', furniture.COLLISION_BOXES)
        height, boxes = furniture.COLLISION_BOXES['fr_lantern']
        self.assertEqual(height, assets.HEIGHTS['lantern'])
        points = np.asarray(assets.collision('lantern').p)
        self.assertTrue(any(np.all(np.asarray(b[:3]) <= points.min(0)+1e-9)
                            and np.all(np.asarray(b[3:]) >= points.max(0)-1e-9) for b in boxes))

    def test_crossing_road_removes_lantern_and_its_lens_and_glow_on_both_placement_paths(self):
        for sidewalk in (False, True):
            with self.subTest(sidewalk=sidewalk):
                base, walks, settings = self.fixture(sidewalk)
                box = (0.,0.,512.,256.)
                before = furniture.place([base], walks, box, **settings)
                target = next(r for r in before if r[0]=='fr_lantern')
                crossing = ('crossing', roads.Spec(8.,'paved',False,'service',False),
                    np.array([[target[1],-500.,0.],[target[1],500.,0.]]), 0.)
                placed = furniture.place([base,crossing], walks, box, **settings)
                lamps = [r for r in placed if r[0]=='fr_lantern']
                self.assertTrue(lamps, 'the rest of the street keeps its lanterns')
                self.assertNotIn(target, lamps)
                for kind in ('fr_lantern','fr_lantern_light',furniture.G_GLOW):
                    self.assertFalse(any(r[0]==kind and r[1:3]==target[1:3] for r in placed), kind)
                self.assertEqual(placed, furniture.place([crossing,base], walks, box, **settings))
                self.assertEqual(len(lamps), sum(r[0]=='fr_lantern_light' for r in placed))

    def test_crossing_road_filter_is_identical_across_tile_boundaries(self):
        base, walks, settings = self.fixture(True)
        whole_box = (0.,0.,512.,256.)
        target = next(r for r in furniture.place([base], walks, whole_box, **settings)
                      if r[0]=='fr_lantern')
        crossing = ('crossing', roads.Spec(8.,'paved',False,'service',False),
                    np.array([[target[1],-500.,0.],[target[1],500.,0.]]), 0.)
        whole = furniture.place([base,crossing], walks, whole_box, **settings)
        split = (furniture.place([base,crossing], walks, (0.,0.,256.,256.), **settings)
                 + furniture.place([base,crossing], walks, (256.,0.,512.,256.), **settings))
        self.assertNotIn(target, whole)
        self.assertEqual(sorted(whole), sorted(split))

    def test_height_and_deck_separation_are_retained(self):
        row = ['fr_lantern',0.,0.,0.,90.,1.,5.4,0.]
        ground = [(4.,np.array([[-100.,0.,0.],[100.,0.,0.]]))]
        raised = [(4.,np.array([[-100.,0.,20.],[100.,0.,20.]]))]
        self.assertFalse(furniture.RoadClearance(ground).clear(row))
        self.assertTrue(furniture.RoadClearance(raised).clear(row))
        self.assertTrue(furniture.RoadClearance([], [(raised[0][1],4.)]).clear(row))
        large = list(row)
        large[2], large[6] = 4.25, 10.8
        self.assertFalse(furniture.RoadClearance(ground).clear(large), 'scaled collider must be tested')


if __name__ == '__main__':
    unittest.main()
