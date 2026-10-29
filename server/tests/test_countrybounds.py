"""Country lookup, actual boundary data, and per-position road/worker integration.

Offline tests, including real geographic controls. No BeamNG rendering claims.
"""
import gzip
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
import countrybounds as cb
import furniture
import geodesy
import poleprofiles as pp
import roads
from test_regional_poles import road, wire_rows


def feature(code, rings, multi=False):
    return {"type": "Feature", "properties": {"country": code}, "geometry": {
        "type": "MultiPolygon" if multi else "Polygon", "coordinates": rings}}


def square(x0, y0, x1, y1):
    return [[x0,y0], [x1,y0], [x1,y1], [x0,y1], [x0,y0]]


def index(*features):
    return cb.CountryIndex({"features": list(features)})


class PolygonTest(unittest.TestCase):
    def test_holes_islands_and_enclave(self):
        a = feature("FR", [[square(0,0,10,10), square(2,2,4,4)], [square(20,0,21,1)]], multi=True)
        b = feature("DE", [square(2,2,4,4)])
        idx = index(a, b)
        for lat, lon, expected in ((1,1,"FR"), (3,3,"DE"), (.5,20.5,"FR"), (11,11,None)):
            self.assertEqual(idx.at(lat,lon), expected)
        self.assertIsNone(index(a).at(3,3))

    def test_edges_vertices_overlap_and_input_order_are_unknown(self):
        a,b = feature("FR", [square(0,0,2,2)]), feature("DE", [square(1,0,3,2)])
        for idx in (index(a,b),index(b,a)):
            for lat,lon in ((0,0),(1,0),(1,1.5),(2,3)):
                self.assertIsNone(idx.at(lat,lon))
            self.assertEqual(idx.at(1,.5), "FR")
            self.assertEqual(idx.at(1,2.5), "DE")

    def test_shared_edge_and_nearby_points(self):
        idx=index(feature("FR",[square(0,0,1,1)]),feature("DE",[square(1,0,2,1)]))
        self.assertEqual(idx.at(.5, 1-1e-7), "FR")
        self.assertEqual(idx.at(.5, 1+1e-7), "DE")
        self.assertIsNone(idx.at(.5, 1))

    def test_date_line_and_reversed_rings(self):
        ring=[[179,-1],[-179,-1],[-179,1],[179,1],[179,-1]]
        for r in (ring,list(reversed(ring))):
            idx=index(feature("FR",[r]))
            for lon in (179.5,-179.5,180,-180): self.assertEqual(idx.at(0,lon),"FR")
            self.assertIsNone(idx.at(0,0))

    def test_invalid_positions_and_missing_coverage(self):
        idx=index(feature("FR",[square(0,0,1,1)]))
        for lat,lon in ((float("nan"),0),(0,float("inf")),(91,0),(0,181),(-50,50)):
            self.assertIsNone(idx.at(lat,lon))

    def test_corrupt_bundle_raises_without_geographic_guess(self):
        cb.bundled.cache_clear()
        with tempfile.TemporaryDirectory() as d:
            p=Path(d,"bad.gz"); p.write_bytes(b"bad")
            with mock.patch.object(cb,"DATA_PATH",p), self.assertRaisesRegex(ValueError,"checksum"):
                cb.bundled()
        cb.bundled.cache_clear()


class GeographicTest(unittest.TestCase):
    def test_pinned_real_control_points(self):
        idx=cb.bundled()
        points=[("Haguenau",48.815,7.790,"FR"), ("Wissembourg",49.037,7.944,"FR"),
                ("Schweigen",49.05,7.956,"DE"), ("Strasbourg",48.58,7.794,"FR"),
                ("Kehl",48.576,7.808,"DE"), ("Lauterbourg",48.975,8.178,"FR"),
                ("Berg",48.985,8.195,"DE"), ("Corsica",42.,9.,"FR"),
                ("Monaco",43.738,7.425,"MC"), ("Busingen",47.697,8.69,"DE"),
                ("Schaffhausen",47.695,8.635,"CH"), ("Vatican",41.903,12.452,"VA"),
                ("Rome",41.9,12.49,"IT"), ("Andorra",42.5075,1.5218,"AD"),
                ("Belgium",50.85,4.35,"BE"), ("Luxembourg",49.61,6.13,"LU"),
                ("Spain",40.4168,-3.7038,"ES"), ("San Marino",43.936,12.447,"SM"),
                ("USA",39.,-76.,"US"), ("French Guiana",4.94,-52.33,"GF"),
                ("Tokyo",35.68,139.69,"JP"), ("Sydney",-33.87,151.21,"AU"),
                ("Fiji",-18.14,178.44,"FJ"), ("South pole outside source",-89.,10.,None),
                ("Antarctica",-80.,10.,"AQ"), ("India",28.61,77.21,"IN"),
                ("Lesotho",-29.32,27.48,"LS"), ("Johannesburg",-26.20,28.04,"ZA"),
                ("Canada",45.42,-75.70,"CA"), ("Mexico",19.43,-99.13,"MX"),
                ("Brazil",-15.79,-47.88,"BR"), ("Argentina",-34.60,-58.38,"AR"),
                ("Auckland",-36.85,174.76,"NZ"), ("London",51.507,-.128,"GB"),
                ("Tokyo",35.68,139.69,"JP"), ("Taipei",25.03,121.56,"TW"),
                ("Beijing",39.90,116.40,"CN"), ("Cairo",30.04,31.24,"EG"),
                ("Nairobi",-1.29,36.82,"KE"), ("Nothweiler user",49.06593,7.79547,"DE")]
        for name,la,lo,expected in points:
            with self.subTest(name=name): self.assertEqual(idx.at(la,lo),expected)

    def test_real_rhine_crossing_in_one_tile_both_placement_paths(self):
        frame=geodesy.EnuFrame(48.58,7.8)
        profile=pp.geographic_profile("fr-all")
        for urban,walks_on in ((False,False),(True,False),(True,True)):
            r=road("crossing",urban)
            walks=[{"fid":r[0],"pts":roads.offset_polyline(r[2],s*5.25),"side":s,"w":2.5}
                   for s in (-1,1)] if walks_on else []
            def placed(box, enabled=True):
                wires=[]
                rows=furniture.place([r],walks,box,town=urban,wires=wires,
                                     pole_profile=profile if enabled else None, pole_frame=frame)
                return rows,wires
            full,wires=placed((-256,0,768,256))
            poles=[r for r in full if r[0] in (pp.STOCK.group,pp.RECESSED.group)]
            self.assertEqual({r[0] for r in poles},{pp.STOCK.group,pp.RECESSED.group})
            for row in poles:
                la,lo,_=frame.enu_to_geodetic(*row[1:4])
                expected=pp.RECESSED if cb.bundled().at(la,lo)=="FR" else pp.STOCK
                self.assertEqual(row[0],expected.group)
            a,aw=placed((-256,0,256,256)); b,bw=placed((256,0,768,256))
            self.assertEqual(sorted(full),sorted(a+b))
            self.assertEqual(wire_rows(wires),wire_rows(aw+bw))
            old,_=placed((-256,0,768,256),False)
            self.assertEqual([r[1:6]+r[7:] for r in old if r[0]==pp.STOCK.group],
                             [r[1:6]+r[7:] for r in poles])
            self.assertEqual([r for r in old if r[0]!=pp.STOCK.group],
                             [r for r in full if r[0] not in (pp.STOCK.group,pp.RECESSED.group)])

    def test_same_location_same_model_in_different_enu_frames(self):
        for la,lo,expected in ((48.58,7.794,pp.RECESSED),(48.576,7.808,pp.STOCK)):
            for origin in ((48.58,7.79),(48.58,7.82),(48.57,7.8)):
                frame=geodesy.EnuFrame(*origin)
                at=pp.selector(pp.geographic_profile("fr-all"),frame)
                self.assertEqual(at("same-road",*frame.geodetic_to_enu(la,lo,150.)),expected)

    def test_modes_and_data_versions_are_distinct_cache_tokens(self):
        old=pp.geographic_profile("fr-all")
        self.assertNotEqual(old,pp.FR_ALL)
        self.assertNotEqual(old,pp.geographic_profile("fr-demo"))
        with mock.patch.object(cb,"DATA_SHA256","different"):
            self.assertNotEqual(old,pp.geographic_profile("fr-all"))
        self.assertIsNone(pp.geographic_profile("off"))
        with self.assertRaisesRegex(ValueError,"ENU frame"):
            pp.selector(old,None)

    def test_cable_run_ends_when_attachment_layout_changes(self):
        poles=[(0.,0.,0.,0.,pp.RECESSED),(40.,0.,0.,0.,pp.RECESSED),
               (80.,0.,0.,0.,pp.STOCK),(120.,0.,0.,0.,pp.STOCK)]
        wires=furniture._own_spans(poles,(-1,-1,130,1))
        self.assertEqual(len(wires),4)  # one bundle + three conductors, no unsupported 3-to-1 join
        self.assertEqual(wire_rows(wires),wire_rows(furniture._own_spans(poles,(-1,-1,60,1))+
                                                   furniture._own_spans(poles,(60,-1,130,1))))

    def test_worker_uses_geographic_profile_and_cache(self):
        import blockworker
        import threading
        from test_roads import features
        from test_blockworker import FakeMapData, FakeDem
        profile=pp.geographic_profile("fr-all")
        for lat,lon,expected in ((48.815,7.79,pp.RECESSED),(49.05,7.956,pp.STOCK)):
            frame=geodesy.EnuFrame(lat,lon)
            f=features([[(-300.,100.),(600.,100.)]],["residential"])
            la,lo,_=frame.enu_to_geodetic(np.array([-300.,600.]),np.array([100.,100.]))
            f.lonlat[:]=np.column_stack((lo,la))
            keys=roads.cells_of_tile(frame,0,0)
            with tempfile.TemporaryDirectory() as d:
                state=SimpleNamespace(md=FakeMapData(f,keys[0]),dem=FakeDem(),widths=None,surface=None,
                                      airports=None,sea=None,lock=threading.Lock(),cache_dir=d,
                                      bridges={},anchor=lambda *args:(None,None))
                with mock.patch.object(blockworker,"_s",state):
                    for n,token in enumerate((None,profile,profile,None)):
                        out=Path(d,str(n)); out.mkdir()
                        blockworker.roads_tile(1,lat,lon,15,0,0,str(out),"/temp/test",True,.05,
                                               buildings=True,pole_profile=token)
                        data=json.loads((out/"roads_1_0_0.json").read_text())
                        self.assertEqual(data["props"]["groups"],[(expected if token else pp.STOCK).group])


if __name__ == "__main__":
    unittest.main()
