-- Optional signs reuse 1.6's pooled shapes and teleport handling without changing road colliders.
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
files['/t/signs.json'] = '{ chunks = {}, exits = { shape = "/t/exit.cdae", pos = { 20, 30, 0 } }, ' ..
  'mileposts = { shape = "/t/mile.cdae", pos = { 20, 30, 0 } } }'
local exitPath, milePath = '/temp/beamngEarth/signs/slot_00001.cdae', '/temp/beamngEarth/signs/slot_00002.cdae'
local msg = { anchorId=1, key='0_0', path='/t/signs.json', n=0,
  slots={exits={exitPath,11},mileposts={milePath,12}} }
roads.setSession('signs')
roads.onTile(msg,1)
-- Turning off a feature before its queued shape is made must cancel that creation too.
settings.set('osmExitSigns',false)
roads.update(gov,1)
T.check(shapes()[milePath] and not shapes()[exitPath], 'independent toggle cancels queued exit shapes')
T.check(files[settings.FILE].osmExitSigns == false, 'exit-sign preference saved')
settings.set('osmExitSigns',true)
roads.update(gov,1)
local got = shapes()
T.check(got[exitPath] and got[milePath], 'enabling restores held shapes without re-requesting roads')
T.check(got[exitPath].fields.collisionType == 'None' and got[milePath].fields.collisionType == 'None',
  'both sign types are render only')
T.check(#roads.slotsHeld('signs') == 2, 'both sign slots stay held')
local collisions = S.calls.reloadCollision or 0
settings.set('mileMarkers',false)
roads.update(gov,1)
T.check(shapes()[exitPath] and not shapes()[milePath], 'mile-marker toggle keeps exits visible')
T.check(#roads.takeSlotsGone('signs') == 0, 'hiding signs retains their slots for re-enabling')
settings.set('mileMarkers',true)
roads.update(gov,1)
T.check((S.calls.reloadCollision or 0) == collisions, 'sign toggles do not rebuild physics')
roads.onTile(msg,1)
roads.update(gov,1)
local count = 0
for _ in pairs(shapes()) do count = count+1 end
T.check(count == 2, 're-sent tiles replace both meshes without duplicates')
roads.takeSlotsGone('signs')
local xf = {beamngQuat=function() return 0,0,0,1 end,
  apply=function(_,x,y,z) return x+100,y-50,z or 0 end}
roads.transform(xf,{quatMul=function(a,b,c,d) return a,b,c,d end})
got = shapes()
T.check(got[exitPath].pos.x == 120 and got[milePath].pos.y == -20, 'both meshes follow anchor transforms')
roads.onDrop({anchorId=1,key='0_0'})
roads.update(gov,1)
T.check(shapes()[exitPath] and shapes()[milePath], 'old-anchor drops preserve the transformed signs until covered')
local lifetime = roads.GHOST_MAX_S
roads.GHOST_MAX_S = -1
roads.update(gov,2)
roads.GHOST_MAX_S = lifetime
T.check(next(shapes()) == nil, 'ghost retirement removes both transformed signs')
T.check(#roads.takeSlotsGone('signs') == 2, 'both sign slots released after deletion')
-- A sign tile has its own key and must never satisfy road/physics readiness.
roads.reset()
roads.setSession('signs')
roads.onTile({anchorId=10,key='s0_0',data={chunks={},exits={shape=exitPath,pos={0,0,0}}}},10)
roads.update(gov,10)
T.check(not roads.settledFor(10,20,20), 'sign-only square is not a ready road')
roads.onTile({anchorId=10,key='0_0',data={chunks={}}},10)
roads.update(gov,10)
T.check(roads.settledFor(10,20,20), 'road readiness uses the actual road square')
settings.set('highwaySigns',false)
roads.update(gov,10)
T.check(not shapes()[exitPath], 'master highway switch hides signs')
settings.set('highwaySigns',true)
roads.update(gov,10)
T.check(shapes()[exitPath] ~= nil, 'master highway switch restores held signs')
roads.transform({beamngQuat=function() return 0,0,0,1 end,
  apply=function(_,x,y,z) return x,y,z or 0 end}, {quatMul=function(a,b,c,d) return a,b,c,d end})
roads.onTile({anchorId=11,key='0_0',data={chunks={}}},11)
roads.update(gov,11)
T.check(shapes()[exitPath] ~= nil, 'new roads do not retire old signs before sign replacement')
roads.onTile({anchorId=11,key='s0_0',data={chunks={}}},11)
roads.update(gov,11)
roads.update(gov,11)
T.check(not shapes()[exitPath], 'new sign square retires old sign ghosts')

-- 1.6.3's railway collider and fuel records share the road loader with sign tiles.
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
T.check(railObject and railObject.fields.collisionType == 'Visible Mesh Final', 'railway still collides alongside signs')
T.check(fuelRows['20:0_0'] and not fuelRows['20:s0_0'], 'station records belong only to the road tile')
roads.collisionRebuilt()
local beforeToggle = S.calls.reloadCollision or 0
settings.set('highwaySigns',false)
roads.update(gov,20)
settings.set('highwaySigns',true)
roads.update(gov,20)
T.check(shapes()[railPath] == railObject, 'sign toggle retains the same railway object')
T.check((S.calls.reloadCollision or 0) == beforeToggle, 'sign toggle leaves railway collision untouched')
roads.onDrop({anchorId=20,key='s0_0'})
roads.update(gov,20)
T.check(shapes()[railPath] == railObject and not fuelDrops['20:0_0'], 'sign removal keeps rail and refuelling records')
roads.onDrop({anchorId=20,key='0_0'})
roads.update(gov,20)
T.check(not shapes()[railPath] and fuelDrops['20:0_0'], 'road removal still retires rail and fuel records')
gas.onRows, gas.dropKey = onRows, dropKey

files['/legacy-settings.json'] = {osmExitSigns=false,mergeSigns=false,mileMarkers=false}
settings.load('/legacy-settings.json')
settings.applyAll()
T.check(not settings.get('highwaySigns') and not roads.exitSignsEnabled and not roads.mergeSignsEnabled,
  'old all-disabled preferences load into the highway switch')
files['/legacy-settings.json'] = {osmExitSigns=true,mergeSigns=false,mileMarkers=false}
settings.load('/legacy-settings.json')
settings.applyAll()
T.check(settings.get('highwaySigns') and roads.exitSignsEnabled and roads.mileMarkersEnabled,
  'legacy enabled preferences migrate to the combined switch')
T.done('test_highway_signs')
