"""Cliff switches must skip work, preserve independent props and isolate caches."""
import asyncio
import itertools
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blockworker
import cliffrocks
import earthd
import geodesy
import roads
from test_farcliffs import built
import test_graphics


class SwitchMessageTest(unittest.TestCase):
    setUp = test_graphics.GraphicsMessageTest.setUp
    conn = test_graphics.GraphicsMessageTest.conn

    def test_switches_validate_independently(self):
        pairs = [('cliffFaces','cliff_faces'), ('cliffRocks','cliff_rocks'), ('cliffDecals','cliff_decals')]
        for field, attr in pairs:
            self.assertTrue(getattr(self.conn([]),attr))
            for invalid in (None, 0, 1, 'false', [], {}):
                conn=self.conn([{'type':'graphics',field:False}, {'type':'graphics',field:invalid}])
                self.assertIs(getattr(conn,attr),False)
            conn=self.conn([{'type':'graphics',field:False}, {'type':'graphics',field:True}])
            self.assertIs(getattr(conn,attr),True)

    def test_shared_server_builds_only_features_some_client_requests(self):
        a=self.conn([dict(type='graphics',cliffFaces=False,cliffRocks=False,cliffDecals=False,distantCliffFaces=False)])
        b=self.conn([dict(type='graphics',cliffFaces=True,cliffRocks=False,cliffDecals=False,distantCliffFaces=True)])
        self.s.connections.add(a)
        self.assertEqual(self.s.cliff_generation(),(False,False,False,False))
        self.s.connections.add(b)
        self.assertEqual(self.s.cliff_generation(),(True,False,False,True))
        self.s.connections.remove(b)
        self.assertEqual(self.s.cliff_generation(),(False,False,False,False))
        self.s.connections.clear()
        self.assertEqual(self.s.cliff_generation(),(True,True,True,True),'offline area builds retain scenery')


class NearGenerationTest(unittest.TestCase):
    def test_all_off_does_not_sample_terrain_or_surface(self):
        fail=Mock(side_effect=AssertionError('disabled cliff work'))
        surface=SimpleNamespace(classes=fail)
        with patch.object(cliffrocks,'qualifying_cells',fail):
            result=cliffrocks.build(None,None,fail,surface,features=(False,False,False))
        self.assertIsNone(result)
        fail.assert_not_called()

    def test_props_without_faces_skip_mesh_refinement_for_all_regions(self):
        class Surface:
            def classes(self, lat, lon):
                return np.full(np.shape(lat),60),True
            def urban(self, lat, lon):
                return np.zeros(np.shape(lat),bool)
        for lat0,lon0 in ((46,7),(36.1,-112.1)):
            frame=geodesy.EnuFrame(lat0,lon0)
            def elev(lat,lon):
                return 100+(np.asarray(lon)-lon0)*111320*np.cos(np.radians(lat0))*2
            for rocks,decals in ((True,False),(False,True)):
                fail=Mock(side_effect=AssertionError('disabled face construction'))
                with patch.object(cliffrocks,'_refine',fail), patch.object(cliffrocks,'mesh',fail), \
                     patch('clifftalus.rows',return_value=[['talus_test',1]]) as rubble, \
                     patch('cliffstrips.tile_parts',side_effect=AssertionError('no strips without faces')) as patches:
                    result=cliffrocks.build(frame,(0,0,160,160),elev,Surface(),features=(False,rocks,decals))
                if not rocks:
                    self.assertIsNone(result)
                    patches.assert_not_called()
                    continue
                self.assertIsNotNone(result)
                fail.assert_not_called()
                self.assertEqual(rubble.call_count,int(rocks))
                self.assertEqual(patches.call_count,0)
                with patch('cliffrocks.write',fail):
                    with tempfile.TemporaryDirectory() as tmp:
                        roads.write_tile(tmp,'/x',0,0,0,[],cliff_mesh=result,decals=False)
                        data=json.loads(Path(tmp,'roads_0_0_0.json').read_text())
                        self.assertFalse(list(Path(tmp).glob('*.cdae')))
                self.assertEqual('talus' in data,rocks)
                self.assertNotIn('cliffDecals',data)

    def test_enabled_faces_do_not_generate_disabled_props(self):
        class Surface:
            def classes(self, lat, lon):
                return np.full(np.shape(lat),60),True
            def urban(self, lat, lon):
                return np.zeros(np.shape(lat),bool)
        frame=geodesy.EnuFrame(46,7)
        def elev(lat,lon):
            return 100+(np.asarray(lon)-7)*111320*np.cos(np.radians(46))*2
        fail=Mock(side_effect=AssertionError('disabled prop generation'))
        with patch('clifftalus.rows',fail), patch('cliffstrips.tile_parts',fail):
            result=cliffrocks.build(frame,(0,0,80,80),elev,Surface(),features=(True,False,False))
        self.assertIsNotNone(result)
        self.assertGreater(len(result.tris),0)
        self.assertEqual((result.talus,result.strips),([],[]))
        fail.assert_not_called()

    def test_worker_revisit_cache_keeps_disabled_and_enabled_builds_separate(self):
        from test_blockworker import FakeMapData, FakeDem
        from test_roads import FRAME, LAT, LON, features
        keys = roads.cells_of_tile(FRAME, 0, 0)
        md = FakeMapData(features([[(-300,100),(300,100)]], ['secondary']), keys[0])
        with tempfile.TemporaryDirectory() as tmp:
            state = SimpleNamespace(md=md, dem=FakeDem(), widths=None, surface=None, airports=None,
                                    sea=None, lock=threading.Lock(), cache_dir=os.path.join(tmp,'cache'),
                                    bridges={}, anchor=lambda *args: (None,None))
            out = os.path.join(tmp,'out')
            os.makedirs(out)
            off, on = (False,False,False), (True,False,False)
            with patch.object(blockworker,'_s',state), patch.object(cliffrocks,'tile_mesh',return_value=None) as build:
                for flags in (off, on, off, on):
                    blockworker.roads_tile(1,LAT,LON,15,0,0,out,'/temp/x',True,0.05,cliffs=flags)
                self.assertEqual([c.kwargs['features'] for c in build.call_args_list], [off,on])

    def test_near_revisit_keys_include_every_generation_switch(self):
        keys={blockworker._geo_content_key('g17_1_1',15,False,False,True,False,0,None,None,
                                           md={},cliffs=flags)
              for flags in itertools.product((False,True),repeat=3)}
        self.assertEqual(len(keys),8)


class FarGenerationTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s=earthd.Server(self.tmp.name,os.path.join(self.tmp.name,'cache'),imagery=None,cells=64,block=1024)
        self.addCleanup(self.s.hm_pool.shutdown)
        self.addCleanup(self.s.tile_pool.shutdown)
        self.conn=SimpleNamespace(distant_cliff_faces=False, active=1, sent={}, desired={}, inflight={},
                                  slot_holds={}, held=set(), shown=set(), retired={})
        # SimpleNamespace is unhashable: the server only needs an iterable here.
        self.s.connections=[self.conn]
        self.anchor=earthd.Anchor(1,36.1,-112.1,geodesy.EnuFrame(36.1,-112.1),zooms={0:15,1:14})
        import farfield
        self.tile=farfield.Tile(1,0,0)

    def test_off_skips_cliff_kernel_and_on_rebuilds_cached_photo_safely(self):
        import farfield
        import matslots
        from test_server import FakeImagery
        self.s.imagery=FakeImagery(os.path.join(self.tmp.name,'imagery'))
        b=built()
        with patch.object(farfield,'build_tile',return_value=b), \
             patch('farcliffs.geometry',side_effect=AssertionError('off must skip cliff geometry')) as kernel:
            off=self.s.make_tile(self.anchor,self.tile)
        kernel.assert_not_called()
        self.assertFalse(off.cliff_faces)
        self.assertIsNotNone(off.material)
        self.conn.slot_holds[matslots.MatSlots.number(off.material)]=1
        old_slot=Path(self.s.out_dir,off.shape).read_bytes()
        ck=self.s.far_ck(1,self.tile.key)
        self.s.tiles[ck]=off
        original=Path(self.s.out_dir,self.s.tile_name(*ck)).read_bytes()
        self.conn.distant_cliff_faces=True
        async def run():
            errors=[]
            asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: errors.append(ctx))
            with patch.object(farfield,'build_tile',return_value=b), patch('farcliffs.geometry',return_value=None) as kernel:
                upgraded=await self.s.tile(self.anchor,self.tile)
                self.assertEqual(kernel.call_count,1)
                await asyncio.sleep(0)
                self.assertEqual(errors,[], 'cache callback must complete cleanly')
                return upgraded
        upgraded=asyncio.run(run())
        self.assertTrue(upgraded.cliff_faces)
        self.assertNotEqual(off.material,upgraded.material,'a held slot must not be reused')
        self.assertEqual(old_slot,Path(self.s.out_dir,off.shape).read_bytes())
        self.assertNotEqual(upgraded.source,self.s.tile_name(*ck))
        self.assertEqual(original,Path(self.s.out_dir,self.s.tile_name(*ck)).read_bytes(), 'old live file unchanged')
        self.assertIsNone(self.s.plain_far_shape(SimpleNamespace(distant_cliff_faces=False),ck,off))
        self.assertIn(str(Path(self.s.out_dir,upgraded.source)),self.s.tile_files(self.s.tile_name(*ck)))


if __name__ == '__main__':
    unittest.main()
