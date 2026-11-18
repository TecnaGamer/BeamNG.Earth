"""Offline smoke/regression tests for the clean Norwegian NVDB integration.

These test pure marking/width/grade functions only, not BeamNG.drive itself.
"""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import nvdb_marking_overlay_v046 as marking
import nvdb_terrain_v0514 as terrain
import nvdb_width_profile_v03 as width


class NorwayDataTests(unittest.TestCase):
    def test_known_marking_types(self):
        self.assertEqual(marking._classify({'type':'Sperrelinje', 'farge':'Gul',
                                            'sideposisjon':'M'})[0:2], ('M','S'))
        self.assertEqual(marking._classify({'type':'Kantlinje, heltrukket',
                                            'farge':'Hvit', 'sideposisjon':'H'})[0:2],
                         ('H','solid'))
        self.assertIsNone(marking._classify({'type':'Unrecognized', 'farge':'Rød',
                                              'sideposisjon':'M'}))

    def test_conflicting_source_priority(self):
        s=np.array([0.,3.,6.,9.,12.,15.]); w=np.full(len(s),7.2)
        def vals(kind,rid,score):
            return [(kind,.12,rid,float(x),score) for x in s]
        found={'M':vals('S','m-001',.07),
               'H':vals('dashed','e-001',.95),
               'V':vals('solid','e-002',.17)}
        yellow, edges, conflicting=marking._resolve_marking_conflicts(found,s,w)
        self.assertTrue(yellow.all())
        self.assertGreater(conflicting,0)
        self.assertTrue(all(x is None for x in edges['H']))

    def test_widths_outside_norway_fallback(self):
        self.assertIsNone(width.at_points([10.,10.0001],[50.,50.],7.0))

    def test_isolated_width_spike_removed(self):
        base=np.array([5.,5.,5.]); measured=np.array([5.,11.,5.])
        found=np.array([True,True,True])
        lon=np.array([7.,7.0001,7.0002]); lat=np.full(3,62.9)
        result=width.stabilise_road_widths(measured,base,found,lon,lat)
        self.assertTrue(np.allclose(result,base))

    def test_grading_reduces_overwide_road_only(self):
        xy=np.c_[np.linspace(0,100,12),np.zeros(12)]
        plan=SimpleNamespace(xy=xy, half=6.0, road_half=4.0,
                             bbox=(-20,-20,120,20),z=np.linspace(12,24,12))
        spec=SimpleNamespace(kind='paved',urban=False,median=0,mountain=False)
        before=plan.z.copy()
        with patch.object(terrain,'measured_width',return_value=5.0):
            self.assertTrue(terrain.grade_plan(plan,np.c_[np.full(12,7.0),np.full(12,62.9)],
                                               spec,1.0,12.0))
        self.assertAlmostEqual(plan.half,3.0)
        self.assertAlmostEqual(plan.road_half,2.5)
        self.assertTrue(np.array_equal(before,plan.z))
        spec.oneway=True

    def test_grading_skips_divided_roads(self):
        plan=SimpleNamespace(xy=np.array([[0.,0.],[1.,0.]]), half=6., road_half=4.,bbox=(-1,-1,2,1))
        spec=SimpleNamespace(kind='paved',urban=False,median=3.0,mountain=False)
        with patch.object(terrain,'measured_width',return_value=5.0):
            self.assertFalse(terrain.grade_plan(plan,np.array([[7.,62.9],[7.1,62.9]]),spec,1.,12.))
        self.assertEqual(plan.half,6.)

if __name__=='__main__':
    unittest.main(verbosity=2)
