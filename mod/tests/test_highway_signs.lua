-- Highway sign tiles (server signstream.py, from cody196's PR #363): render-only shapes with pooled slots, their own
-- "s<tx>_<ty>" keys (never a road's readiness), one settings switch (highwaySigns) that hides and restores them
-- without touching the roads' collision.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local roads = require('beamngEarth/roads')
local settings = require('beamngEarth/settings')
local files = {}
readFile = function(path) return files[path] end
jsonDecode = function(text) return assert(load('return '..text))() end
jsonReadFile = function(path) return files[path] end
jsonWriteFile = function(path, value) files[path] = value; return true end
local gov = { try = function() return true end, charge = function() end, chargeMotion = function() end }
local function shapes()
  local out = {}
  for _, o in pairs(S.objects) do
    if o.class == 'TSStatic' then out[o.fields.shapeName] = o end
  end
  return out
end

T.check(settings.defaults().highwaySigns == true, 'highway signs on by default')
files['/t/signs.json'] = '{ chunks = {}, exits = { shape = "/t/exit.cdae", pos = { 20, 30, 0 } }, ' ..
  'mileposts = { shape = "/t/mile.cdae", pos = { 20, 30, 0 } }, merges = { shape = "/t/merge.cdae", pos = { 20, 30, 0 } } }'
local exitPath, milePath = '/temp/beamngEarth/signs/slot_00001.cdae', '/temp/beamngEarth/signs/slot_00002.cdae'
local mergePath = '/temp/beamngEarth/signs/slot_00003.cdae'
local msg = { anchorId=1, key='s0_0', path='/t/signs.json', n=3,
  slots={exits={exitPath,11},mileposts={milePath,12},merges={mergePath,13}} }
roads.setSession('signs')
roads.onTile(msg,1)
-- switched off before its queued shapes are made: none is made
settings.set('highwaySigns',false)
roads.update(gov,1)
T.check(not shapes()[exitPath] and not shapes()[milePath] and not shapes()[mergePath], 'switch cancels queued shapes')
T.check(files[settings.FILE].highwaySigns == false, 'highway signs preference saved')
local collisions = S.calls.reloadCollision or 0
settings.set('highwaySigns',true)
roads.update(gov,1)
local got = shapes()
T.check(got[exitPath] and got[milePath] and got[mergePath], 'switching on restores the held shapes')
T.check(got[exitPath].fields.collisionType == 'None' and got[milePath].fields.collisionType == 'None'
  and got[mergePath].fields.collisionType == 'None', 'sign shapes are render only')
T.check(#roads.slotsHeld('signs') == 3, 'the sign slots stay held')
settings.set('highwaySigns',false)
roads.update(gov,1)
T.check(next(shapes()) == nil, 'switching off removes the shown signs')
T.check(#roads.takeSlotsGone('signs') == 0, 'hidden signs keep their slots for switching on again')
settings.set('highwaySigns',true)
roads.update(gov,1)
T.check((S.calls.reloadCollision or 0) == collisions, 'the switch never rebuilds collision')
roads.onTile(msg,1)
roads.update(gov,1)
local count = 0
for _ in pairs(shapes()) do count = count+1 end
T.check(count == 3, 'a re-sent sign tile replaces its meshes without duplicates')
roads.takeSlotsGone('signs')
local xf = {beamngQuat=function() return 0,0,0,1 end,
  apply=function(_,x,y,z) return x+100,y-50,z or 0 end}
roads.transform(xf,{quatMul=function(a,b,c,d) return a,b,c,d end})
got = shapes()
T.check(got[exitPath].pos.x == 120 and got[milePath].pos.y == -20, 'signs follow anchor transforms')
roads.onDrop({anchorId=1,key='s0_0'})
roads.update(gov,1)
T.check(shapes()[exitPath] and shapes()[milePath], 'an old-anchor drop keeps the moved signs until covered')
local lifetime = roads.GHOST_MAX_S
roads.GHOST_MAX_S = -1
roads.update(gov,2)
roads.GHOST_MAX_S = lifetime
T.check(next(shapes()) == nil, 'ghost retirement removes the moved signs')
T.check(#roads.takeSlotsGone('signs') == 3, 'their slots are let go after the deletes')

-- A sign tile never makes a road square ready, and road tiles never retire sign ghosts.
roads.reset()
roads.setSession('signs')
roads.onTile({anchorId=10,key='s0_0',data={chunks={},exits={shape=exitPath,pos={0,0,0}}}},10)
roads.update(gov,10)
T.check(not roads.settledFor(10,20,20), 'a sign-only square is not a ready road')
roads.onTile({anchorId=10,key='0_0',data={chunks={}}},10)
roads.update(gov,10)
T.check(roads.settledFor(10,20,20), 'road readiness uses the road square')
roads.transform({beamngQuat=function() return 0,0,0,1 end,
  apply=function(_,x,y,z) return x,y,z or 0 end}, {quatMul=function(a,b,c,d) return a,b,c,d end})
roads.onTile({anchorId=11,key='0_0',data={chunks={}}},11)
roads.update(gov,11)
T.check(shapes()[exitPath] ~= nil, 'new roads do not retire old signs before their sign tile comes')
roads.onTile({anchorId=11,key='s0_0',data={chunks={}}},11)
roads.update(gov,11)
roads.update(gov,11)
T.check(not shapes()[exitPath], 'the new sign square retires the old sign ghost')

-- Railway colliders and fuel records stay the road tile's.
roads.clear()
roads.reset()
local gas = require('beamngEarth/gasstations')
local fuelRows, fuelDrops, onRows, dropKey = {}, {}, gas.onRows, gas.dropKey
gas.onRows = function(aid,key,recs) fuelRows[key] = recs end
gas.dropKey = function(key) fuelDrops[key] = true end
local railPath = '/temp/beamngEarth/signs/rail.cdae'
roads.onTile({anchorId=20,key='0_0',data={chunks={},rail={shape=railPath,pos={0,0,0}},
  fuel={{id='station'}}}},20)
roads.onTile({anchorId=20,key='s0_0',data={chunks={},exits={shape=exitPath,pos={20,0,0}}}},20)
roads.update(gov,20)
local railObject = shapes()[railPath]
T.check(railObject and railObject.fields.collisionType == 'Visible Mesh Final', 'railway still collides beside signs')
T.check(fuelRows['20:0_0'] and not fuelRows['20:s0_0'], 'station records belong to the road tile only')
roads.collisionRebuilt()
local beforeToggle = S.calls.reloadCollision or 0
settings.set('highwaySigns',false)
roads.update(gov,20)
settings.set('highwaySigns',true)
roads.update(gov,20)
T.check(shapes()[railPath] == railObject, 'the switch keeps the railway object')
T.check((S.calls.reloadCollision or 0) == beforeToggle, 'the switch leaves railway collision alone')
roads.onDrop({anchorId=20,key='s0_0'})
roads.update(gov,20)
T.check(shapes()[railPath] == railObject and not fuelDrops['20:0_0'], 'dropping signs keeps rail and fuel')
roads.onDrop({anchorId=20,key='0_0'})
roads.update(gov,20)
T.check(not shapes()[railPath] and fuelDrops['20:0_0'], 'dropping the road tile still retires rail and fuel')
gas.onRows, gas.dropKey = onRows, dropKey

-- Speed-limit signs (server speedsigns.py) come in the same sign tiles: render-only, under the same switch.
roads.clear()
roads.reset()
roads.setSession('signs')
local speedPath = '/temp/beamngEarth/signs/slot_00009.cdae'
roads.onTile({anchorId=30,key='s1_2',data={chunks={},speeds={shape=speedPath,pos={256,512,0}}}},30)
roads.update(gov,30)
T.check(shapes()[speedPath] and shapes()[speedPath].fields.collisionType == 'None', 'speed signs are render only')
settings.set('highwaySigns',false)
roads.update(gov,30)
T.check(not shapes()[speedPath], 'the switch hides speed signs')
settings.set('highwaySigns',true)
roads.update(gov,30)
T.check(shapes()[speedPath] ~= nil, 'and shows them again')
T.check(not roads.settledFor(30,300,600), 'a speed-sign square is not a ready road')

-- German Autobahn signs (server deautobahn.py) are a sign-tile field of their own: render-only, under the same switch.
roads.clear()
roads.reset()
roads.setSession('signs')
local autobahnPath = '/temp/beamngEarth/signs/slot_00011.cdae'
roads.onTile({anchorId=40,key='s1_2',data={chunks={},autobahn={shape=autobahnPath,pos={256,512,0}}}},40)
roads.update(gov,40)
T.check(shapes()[autobahnPath] and shapes()[autobahnPath].fields.collisionType == 'None', 'Autobahn signs are render only')
settings.set('highwaySigns',false)
roads.update(gov,40)
T.check(not shapes()[autobahnPath], 'the switch hides Autobahn signs')
settings.set('highwaySigns',true)
roads.update(gov,40)
T.check(shapes()[autobahnPath] ~= nil, 'and shows them again')
T.done('test_highway_signs')
