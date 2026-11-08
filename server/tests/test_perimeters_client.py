"""Execute the client lifecycle under a small engine mock; no rendering claims."""
from pathlib import Path
import sys
import unittest

try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None

ROOT=Path(__file__).resolve().parents[2]
CLIENT=ROOT/'mod/beamng_earth/lua/ge/extensions/beamngEarth'


@unittest.skipIf(LuaRuntime is None,'optional Lua test runtime (lupa) unavailable')
class ClientTest(unittest.TestCase):
    def test_stock_collision_uses_batched_rebuild_and_waits_for_vehicle(self):
        changes=[]
        self.module.onCollide=lambda:changes.append(True)
        self.module.clearForCollision=lambda row:False
        self.module.onRows('tile',self.rows)
        self.module.update()
        self.assertEqual(self.module.status()['instantiated'],1)
        self.assertEqual(self.module.trace()[1]['collisionReason'],'awaiting_vehicle_clearance')
        self.assertEqual(self.lua.globals().objects[1].fields['collisionType'],'None')
        self.module.clearForCollision=lambda row:True
        self.module.update()
        self.assertEqual(self.lua.globals().objects[1].fields['collisionType'],'Collision Mesh')
        self.assertEqual(self.module.status()['made'],1)
        self.assertTrue(changes)
        before=len(changes);self.module.dropKey('tile')
        self.assertGreater(len(changes),before)
    def test_empty_tile_has_explicit_two_pass_status_and_drops_it(self):
        report=self.lua.table_from(dict(network_digest='approved-route',placement_status='no_sections_in_tile',version=7))
        self.module.onRows('empty',self.lua.table_from([]),report)
        self.assertEqual(self.module.tileTrace('empty')['network_digest'],'approved-route')
        self.module.dropKey('empty')
        self.assertIsNone(self.module.tileTrace('empty'))
    def setUp(self):
        self.lua=LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute('''
          objects = {}; nextId=0; cameraX=0; missing=false; failCreate=false
          function vec3(x,y,z) return {x=x,y=y,z=z} end
          function MatrixF()
            return {setColumn=function(self,k,v) self[k]=v end,
              setPosition=function(self,p) self.pos=p end}
          end
          FS={fileExists=function(self,path) return not missing end}
          function loadJsonMaterialsFile(path) end
          function log(...) end
          SFXSystem={getPositionXYZ=function() return cameraX,0,0 end}
          scenetree={findObjectById=function(id) return objects[id] end,
            MissionGroup={addObject=function(self,o) end}}
          function createObject(kind)
            if failCreate then return nil end
            nextId=nextId+1; local id=nextId
            local o={fields={},id=id}
            function o:setField(k,i,v) self.fields[k]=v end
            function o:setTransform(m) self.matrix=m end
            function o:setScale(s) self.scale=s end
            function o:registerObject(n) objects[id]=self end
            function o:getID() return id end
            function o:delete() objects[id]=nil end
            return o
          end
        ''')
        self.module=self.lua.execute((CLIENT/'perimeters.lua').read_text())
        self.rows=self.lua.table_from([self.lua.table_from([0,0,0,4,0,0,'real-fixture-panel'])])

    def test_exact_model_visible_creation_scale_and_drop(self):
        self.module.onRows('tile',self.rows)
        self.module.update()
        self.assertEqual(self.module.status()['instantiated'],1)
        obj=self.lua.globals().objects[1]
        self.assertEqual(obj.fields['shapeName'],self.module.MODEL)
        self.assertEqual(obj.fields['isRenderEnabled'],'true')
        self.assertEqual(obj.scale.z,1)
        self.assertEqual(obj.scale.x,1)
        self.module.dropKey('tile')
        self.assertEqual(self.module.status()['instantiated'],0)
        self.assertIsNone(self.lua.globals().objects[1])

    def test_read_only_trace_reports_source_facility_ownership_and_instantiation(self):
        row=self.lua.table_from([0,0,0,4,0,0,'trace-id','physical:way/900','run',0,0,'facility:one','military','external_physical'])
        rows=self.lua.table_from([row])
        self.module.onRows('tile-b',rows);self.module.onRows('tile-a',rows)
        trace=self.module.trace('physical:way/900')
        self.assertEqual(len(trace),1)
        self.assertEqual(trace[1]['facilityId'],'facility:one')
        self.assertEqual(trace[1]['classification'],'external_physical')
        self.assertEqual([trace[1]['owners'][i] for i in (1,2)],['tile-a','tile-b'])
        self.assertFalse(trace[1]['instantiated']);self.assertEqual(self.module.made,0)
        self.assertEqual(trace[1]['reason'],'awaiting_creation')
        self.module.update()
        self.assertTrue(self.module.trace()[1]['instantiated'])
        self.assertEqual(self.module.made,1)
        self.assertEqual(len(self.module.trace('absent')),0)
        self.lua.globals().cameraX=1000;self.module.update()
        self.assertEqual(self.module.trace()[1]['reason'],'outside_creation_distance')

    def test_stream_unload_reload_and_stable_replacement(self):
        self.module.onRows('tile',self.rows);self.module.update()
        self.module.onRows('tile',self.rows);self.module.update()
        self.assertEqual(self.module.made,1)
        self.lua.globals().cameraX=1000;self.module.update()
        self.assertEqual(self.module.status()['instantiated'],0)
        self.lua.globals().cameraX=0;self.module.update()
        self.assertEqual(self.module.status()['instantiated'],1)
        self.module.reset();self.assertEqual(self.module.status()['planned'],0)

    def test_missing_model_and_creation_failure(self):
        self.lua.globals().missing=True
        self.module.onRows('tile',self.rows);self.module.update()
        self.assertTrue(self.module.status()['missingModel'])
        self.assertEqual(self.module.status()['instantiated'],0)
        self.module.reset();self.lua.globals().missing=False
        self.lua.globals().failCreate=True
        self.module.onRows('tile',self.rows);self.module.update()
        self.assertEqual(self.module.failed,1)
        self.assertEqual(self.module.status()['instantiated'],0)

    def test_budget_slope_short_residual_and_rebase(self):
        rows=self.lua.table_from([self.lua.table_from([i*4,0,0,i*4+1,0,.2,str(i)]) for i in range(30)])
        self.module.onRows('tile',rows);self.module.update()
        self.assertEqual(self.module.status()['instantiated'],self.module.BUDGET)
        obj=self.lua.globals().objects[1]
        self.assertAlmostEqual(obj.scale.x,(1+.2**2)**.5/4)
        self.assertEqual(obj.scale.z,1)
        xf=self.lua.eval('{apply=function(self,x,y,z) return x+100,y-50,z+3 end}')
        before=obj.matrix.pos.x
        self.module.transform(xf)
        self.assertAlmostEqual(obj.matrix.pos.x,before+100)
        self.assertAlmostEqual(obj.matrix.pos.z,3.1)

    def test_modified_lua_files_parse(self):
        for name in ('perimeters.lua','roads.lua','core.lua','trees.lua'):
            self.lua.execute('assert(load(...))',(CLIENT/name).read_text())

    def test_cross_tile_reference_ownership_and_single_rebase(self):
        self.module.onRows('old-anchor-tile',self.rows)
        self.module.onRows('new-anchor-tile',self.rows)
        self.module.update()
        self.assertEqual(self.module.status()['instantiated'],1)
        self.assertEqual(self.module.made,1)
        xf=self.lua.eval('{apply=function(self,x,y,z) return x+100,y,z end}')
        self.module.transform(xf)
        self.assertEqual(self.lua.globals().objects[1].matrix.pos.x,102)
        self.assertEqual(self.rows[1][1],0) # cached wire JSON was not mutated
        self.module.dropKey('old-anchor-tile')
        self.assertEqual(self.module.status()['instantiated'],1)
        self.module.dropKey('new-anchor-tile')
        self.assertEqual(self.module.status()['instantiated'],0)

    def test_draw_distance_has_unload_hysteresis(self):
        self.module.onRows('tile',self.rows);self.module.update()
        self.lua.globals().cameraX=650;self.module.update()
        self.assertEqual(self.module.status()['instantiated'],1)
        self.lua.globals().cameraX=750;self.module.update()
        self.assertEqual(self.module.status()['instantiated'],0)


if __name__=='__main__':unittest.main()
