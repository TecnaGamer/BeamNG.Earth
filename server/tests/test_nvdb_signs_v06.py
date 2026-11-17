"""Offline nationwide Norwegian sign v0.6 tests: no API calls / game needed."""
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SERVER))
import norway_nvdb_signs_v06 as m


def fixture(number='202 - Vikeplikt', face='Parallelt fra veg',
            point=(62.89390647,7.6787657), road=(62.89387043,7.67871026), pid=80833718):
    return {'id':pid,'metadata':{'type':{'id':96}},
            'geometri':{'wkt':'POINT Z (%s %s 2.504)'%point,'srid':4326,'egengeometri':True},
            'lokasjon':{'geometri':{'wkt':'POINT Z (%s %s 2.504)'%road,'srid':4326}},
            'egenskaper':[{'id':5530,'verdi':number}, {'id':1894,'verdi':face,'enum_id':2838}]}


class FakeFrame:
    def geodetic_to_enu(self,lat,lon,z):
        return ((np.asarray(lon)-7.67)*50800.,(np.asarray(lat)-62.89)*111320.,np.asarray(z))
    def enu_to_geodetic(self,x,y,z):
        return ((np.asarray(y)/111320.)+62.89,(np.asarray(x)/50800.)+7.67,np.asarray(z))


class TestNormalize(unittest.TestCase):
    def test_every_supported_sign_role(self):
        roles=set(m.SIGN_ROLES.values())
        self.assertEqual(len(roles),6)
        for code,role in m.SIGN_ROLES.items():
            row=m.plate_of(fixture(code+' - Example'))
            self.assertEqual(row[1],role)

    def test_real_norway_geometry_and_ref_offset(self):
        x=m.plate_of(fixture())
        self.assertEqual(x[0],80833718)
        self.assertAlmostEqual(x[2],7.6787657)
        self.assertAlmostEqual(x[3],62.89390647)

    def test_rejects_unknown_orientation(self):
        self.assertIsNone(m.plate_of(fixture(face='Trafikk i metreringsretning')))

    def test_rejects_missing_physical_geometry(self):
        obj=fixture();obj['geometri']['egengeometri']=False
        self.assertIsNone(m.plate_of(obj))

    def test_rejects_speed_signs(self):
        self.assertIsNone(m.plate_of(fixture(number='362.60 - Fartsgrense 60 km/t')))

    def test_rejects_unsafe_distance(self):
        self.assertIsNone(m.plate_of(fixture(point=(62.89387,7.67871026))))
        self.assertIsNone(m.plate_of(fixture(point=(62.8943,7.6803))))

    def test_no_srid_mismatch(self):
        obj=fixture();obj['lokasjon']['geometri']['srid']=5973
        self.assertIsNone(m.plate_of(obj))

    def test_no_wrong_type(self):
        obj=fixture();obj['metadata']['type']['id']=95
        self.assertIsNone(m.plate_of(obj))

    def test_valid_both_wkt_axis_orders(self):
        self.assertEqual(m._wkt_point({'wkt':'POINT(7.6787657 62.89390647)','srid':4326}),
                         (7.6787657,62.89390647))

    def test_cells_bounded(self):
        keys=m.cell_keys((7.67,62.89,7.68,62.895))
        self.assertTrue(keys)
        self.assertFalse(m.cell_keys((-70,40,-69,41)))
        self.assertFalse(m.cell_keys((7,62,9,64)))

    def test_url_uses_official_endpoint(self):
        u=m.build_url((5241,319))
        self.assertTrue(u.startswith(m.BASE+'?'))
        self.assertIn('kartutsnitt=',u)
        with self.assertRaises(ValueError):m.build_url((5241,319),'contains a space')

    def test_pagination_terminates_on_empty_page(self):
        pages=[]
        def fake(url):
            pages.append(url)
            if len(pages)==1:
                return {'objekter':[fixture()], 'metadata':{'neste':{'start':'opaque123'}}}
            return {'objekter':[], 'metadata':{'neste':{'start':'opaque123'}}}
        result=m.download_cell((5241,319),fake)
        self.assertEqual(len(result),1)
        self.assertEqual(len(pages),2)
        self.assertIn('start=opaque123',pages[1])

    def test_pagination_failure_never_truncates(self):
        def fake(url):
            return {'objekter':[fixture()], 'metadata':{'neste':{'start':'same'}}}
        with self.assertRaises(ValueError):m.download_cell((5241,319),fake)

    def test_cache_disk_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(m,'cache_dir',return_value=Path(tmp)):
            sample=[[80833718,'prio.give_way',7.6787657,62.89390647,7.67871026,62.89387043]]
            key=(5241,319)
            m._save_disk(key,sample)
            got=m._load_disk(key)
            self.assertIsNotNone(got)
            self.assertEqual(got[1],sample)

    def test_half_open_ownership(self):
        self.assertTrue(m.in_box(2,3,(2,3,10,11)))
        self.assertFalse(m.in_box(10,3,(2,3,10,11)))

    def test_default_on_and_explicit_off(self):
        with patch.dict(os.environ,{m.ENV:'on'}):
            self.assertTrue(m.enabled())
        with patch.dict(os.environ,{m.ENV:'off'}):
            self.assertFalse(m.enabled())
        with patch.dict(os.environ,{},clear=True):
            self.assertTrue(m.enabled())

    def test_draws_real_sign_mesh_with_face_outward(self):
        with patch.dict(os.environ,{m.ENV:'on'}),self.fake_dependencies():
            rows=[[80833718,'prio.give_way',7.6787657,62.89390647,7.67871026,62.89387043]]
            with patch.object(m,'_tile_records',return_value=rows):
                mesh=m.build_tile(FakeFrame(),(300,250,700,620),lambda lat,lon: np.zeros_like(lat),[],
                                  {'atlas':'vienna_w','mount':[2.2,1.5]})
            self.assertIsNotNone(mesh)
            self.assertEqual(mesh[3],['bne_signs_vienna_w','bne_exit_metal'])
            self.assertEqual(self.captured.shape,(1,10))
            self.assertEqual(self.captured[0,7],2)
            self.assertAlmostEqual(float(np.linalg.norm(self.captured[0,4:6])),1,places=5)
            ref=np.array([(7.6787657-7.67871026)*50800,(62.89390647-62.89387043)*111320])
            self.assertLess(float(np.dot(self.captured[0,4:6],ref)),0)

    def test_no_native_yield_removed_if_no_confirmed_render(self):
        r=np.zeros((1,11));r[0,4]=20
        with patch.dict(os.environ,{m.ENV:'on'}),self.fake_dependencies():
            m._tls.rendered=[]
            self.assertEqual(len(m.suppress_inferred(r,FakeFrame())),1)

    def test_furniture_suppression_opt_in(self):
        rows=[['giveway',1.,2.,0.,0.,1.,3.,0.]]
        with patch.dict(os.environ,{m.ENV:'on','BNE_ROADSIGNS':'on'},clear=False):
            with patch.dict(os.environ,{'BNE_NVDB_SIGNS_HIDE_FURNITURE':'off'}):
                self.assertEqual(rows,m.suppress_furniture_yields(rows,FakeFrame()))

    def fake_dependencies(self):
        from contextlib import contextmanager
        @contextmanager
        def ctx():
            roles={'cols':8,'rows':16}
            systems={'rules':['prio.give_way','prio.stop','reg.no_entry','mand.keep_right','mand.keep_left','mand.roundabout'],
                 'sizes':{'default':{'tri_down':[.75,.9,1.25], 'octagon':[.6,.75,.9],
                                     'circle':[.6,.75,.9]}}}
            byid={'prio.give_way':(2,{'shape':'tri_down'})}
            def render(table,*args):
                self.captured=table.copy()
                n=len(table)
                return np.zeros((n*3,3)),np.zeros((n,3),np.int64),np.zeros((n*3,2)),np.zeros(n,np.int64)
            deps={'roadsigns':SimpleNamespace(tables=lambda:(roles,systems,byid),SHAPES={'tri_down':2},METAL='bne_exit_metal'),
                  'fast':SimpleNamespace(_ef=SimpleNamespace(road_sign_mesh=render)),
                  'exitsigns':SimpleNamespace(_on_deck=lambda *args:False)}
            with patch.dict(sys.modules,deps):yield
        return ctx()


if __name__=='__main__':unittest.main()
