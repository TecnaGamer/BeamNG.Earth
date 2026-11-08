"""Shields appear through ordinary streamed-road generation without inventory setup."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import numpy as np
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import streamedroutes
import highwaysigns
import mapcells
from test_roads import features, rules, FRAME, M_LAT, M_LON, LAT, LON


def junction():
    f=features([[(0.,0.),(500.,0.)],[(-100.,0.),(0.,0.),(0.,200.)]],['secondary','tertiary'])
    f.attrs['src_record']=np.array(['w10@1','w20@1'])
    return f


def membership(ids):
    return {10:[dict(type='route',route='road',network='US:KS',ref='10')]}


class StreamedShieldsTest(unittest.TestCase):
    def test_junction_generates_state_shield_without_road_tag_endpoint(self):
        f=junction();relations=SimpleNamespace(query=membership)
        objects=streamedroutes.stations([(f,0),(f,1)],relations)
        self.assertEqual(len(objects),1)
        route=objects[0]['roadside']['routes'][0]
        self.assertEqual((route['network'],route['number']),('US:KS','10'))
        self.assertAlmostEqual(objects[0]['lon'],LON+100./M_LON,places=6)
        self.assertEqual(objects[0]['roadside']['main_id'],10)

    def test_no_route_guess_and_no_arbitrary_segment_start(self):
        f=junction()
        self.assertEqual(streamedroutes.stations([(f,0),(f,1)],SimpleNamespace(query=lambda ids:{})),[])
        self.assertEqual(streamedroutes.stations([(f,0)],SimpleNamespace(query=membership)),[])

    def test_one_way_bridge_and_link_rules_remain_in_force(self):
        f=junction();f.rules['oneway']=rules([(0,-1.)],'float')
        self.assertEqual(streamedroutes.stations([(f,0),(f,1)],SimpleNamespace(query=membership)),[])
        for key,value in [('flags','is_bridge'),('flags','is_tunnel'),('flags','is_under_construction'),('subclass','link')]:
            f=junction();f.rules[key]=rules([(0,value)])
            self.assertEqual(streamedroutes.stations([(f,0),(f,1)],SimpleNamespace(query=membership)),[])

    def test_cached_cells_are_deterministic_and_need_no_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            f=junction();cell=mock.Mock(return_value=SimpleNamespace(layers={'roads':f}))
            source=streamedroutes.Source(SimpleNamespace(raw_dir=tmp,cell=cell))
            source.relations.query=membership
            box=(LON-.001,LAT-.001,LON+.005,LAT+.003)
            first=source.query(*box);calls=cell.call_count
            self.assertEqual(len(first),1)
            self.assertEqual(source.query(*box),first)
            self.assertEqual(cell.call_count,calls)
            self.assertEqual(source.query(*box,existing=first),[])
            self.assertEqual(list(Path(tmp).glob('*.sqlite')),[])

    def test_unavailable_membership_keeps_provider_fallback_available(self):
        f=junction()
        self.assertIsNone(streamedroutes.stations([(f,0),(f,1)],SimpleNamespace(query=lambda ids:{},ready=False)))

    def test_incomplete_cells_are_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            cell=mock.Mock(return_value=SimpleNamespace(layers={}))
            source=streamedroutes.Source(SimpleNamespace(raw_dir=tmp,cell=cell))
            key=mapcells.cell_of(LAT,LON)
            self.assertIsNone(source._cell(key))
            self.assertNotIn(key,source.cache)

    def test_normal_sign_pipeline_uses_streamed_roads_before_layercake(self):
        with tempfile.TemporaryDirectory() as tmp:
            f=junction()
            md=SimpleNamespace(raw_dir=tmp,cache_dir=tmp,fetcher=None,cell=lambda *a,**kw:SimpleNamespace(layers={'roads':f}))
            tags=SimpleNamespace(query=lambda *a:[],version=lambda:None)
            source=highwaysigns.Source(md,tags=tags)
            source.streamed.relations.query=membership
            with mock.patch.object(source.repeaters,'query',side_effect=AssertionError('Layercake was queried')):
                objects=source.objects(FRAME,(-200.,-200.,600.,300.))
            self.assertTrue(any(o.get('roadside',{}).get('source')=='streamed_roads' for o in objects))


if __name__=='__main__': unittest.main()
