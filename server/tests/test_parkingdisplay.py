"""Exercise visual parking selection, including the normal map-data entry point."""
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy
import mapgeom
import parkingdisplay as pd


BOX = (0., 0., 256., 256.)
LOT = np.array([[10.,10.],[90.,10.],[90.,50.],[10.,50.],[10.,10.]])
PARK = [[x,25.,1.,90.,1] for x in range(16, 88, 3)]


class DisplayTest(unittest.TestCase):
    def test_rows_reserved_only_inside_lot_and_stable(self):
        lot = ("1",LOT,"")
        ordinary = [[4,25,1,90,1], [105,25,1,90,1], [20,80,1,0], [40,25,1,0,2]]
        rows = PARK + ordinary
        first = pd.allocate(rows, [lot], BOX)
        cars = [r for r in first if len(r)>4 and r[4]==3]
        self.assertTrue(cars)
        self.assertLessEqual(len(cars),int(.75 * len(PARK)))
        self.assertEqual(first, pd.allocate(rows, [lot], BOX))
        for row in ordinary:
            self.assertIn(row, first)
        for car in cars:
            self.assertEqual(car[7], "general")
            self.assertTrue(0 <= car[8] <= 1)
            others = [r for r in first if r is not car]
            self.assertFalse(pd._overlaps(car,np.array([r[:2] for r in others]),
                                          np.radians([r[3] for r in others])).any())

    def test_excludes_drives_reserved_bays_and_building_edges(self):
        lines = [(np.array([[30.,0.],[30.,80.]]), 3.)]
        reserved = [LOT*.08 + [49.,21.]]
        blockers = [np.array([[65.,27.],[75.,27.],[75.,35.],[65.,35.]])]
        rows = pd.allocate(PARK, [("1",LOT,"")], BOX, lines=lines, reserved=reserved, blockers=blockers)
        cars = [r for r in rows if r[4]==3]
        self.assertTrue(cars)
        for r in cars:
            self.assertGreaterEqual(abs(r[0]-30.), 5.8)
            self.assertFalse(48 < r[0] < 58 or 65 <= r[0] <= 75)

    def test_wire_rows_from_map_cells_and_switch(self):
        frame = geodesy.EnuFrame(40.,-83.)
        def feature(name, kind, xy, attrs):
            la,lo,_ = frame.enu_to_geodetic(xy[:,0],xy[:,1],np.zeros(len(xy)))
            layer = mapgeom.build(name, np.array(["1"]), [(kind, [[np.column_stack((lo,la))]])])
            layer.attrs.update({k: np.array([v]) for k,v in attrs.items()})
            return layer
        infra = feature("infrastructure",mapgeom.POLYGON,LOT, {"class":"parking","tags":json.dumps({})})
        place = feature("places",mapgeom.POINT,np.array([[15.,15.]]),
                        {"basic_category":"car_dealer","name":"Example Auto","confidence":.96})
        md = SimpleNamespace(cell=lambda *a,**kw: SimpleNamespace(layers={"infrastructure":infra, "places":place}))
        with patch.dict(os.environ, BNE_PARKING_DISPLAYS="1"):
            rows = pd.apply(frame,0,0,md,PARK,[],[])
        display = [r for r in rows if r[4]==3]
        self.assertTrue(display)
        self.assertEqual({r[7] for r in display},{"dealer"})
        with patch.dict(os.environ, BNE_PARKING_DISPLAYS="0"):
            self.assertIs(pd.apply(frame,0,0,None,PARK,[],[]),PARK)
        mixed = [(np.array([20.,25.]),"",p) for p in ("dealer","retail")]
        self.assertEqual(pd._profile(LOT,"",mixed),"general")


if __name__ == "__main__":
    unittest.main()
