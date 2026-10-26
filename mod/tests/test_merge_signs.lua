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
files['/t/signs.json'] = '{ chunks = {}, merges = { shape = "/t/exit.cdae", pos = { 20, 30, 0 } }, ' ..
  'mileposts = { shape = "/t/mile.cdae", pos = { 20, 30, 0 } } }'
local exitPath, milePath = '/temp/beamngEarth/signs/slot_00001.cdae', '/temp/beamngEarth/signs/slot_00002.cdae'
local msg = { anchorId=1, key='0_0', path='/t/signs.json', n=0,
  slots={merges={exitPath,11},mileposts={milePath,12}} }
roads.setSession('signs')
roads.onTile(msg,1)
-- Turning off a feature before its queued shape is made must cancel that creation too.
settings.set('mergeSigns',false)
roads.update(gov,1)
T.check(shapes()[milePath] and not shapes()[exitPath], 'independent toggle cancels queued exit shapes')
T.check(files[settings.FILE].mergeSigns == false, 'exit-sign preference saved')
settings.set('mergeSigns',true)
roads.update(gov,1)
local got = shapes()
T.check(got[exitPath] and got[milePath], 'enabling restores held shapes without re-requesting roads')
T.check(got[exitPath].fields.collisionType == 'None' and got[milePath].fields.collisionType == 'None',
  'both sign types are render only')
T.check(#roads.slotsHeld('signs') == 2, 'both sign slots stay held')
local collisions = S.calls.reloadCollision or 0
settings.set('mileMarkers',false)
roads.update(gov,1)
T.check(shapes()[exitPath] and not shapes()[milePath], 'mile-marker toggle keeps merges visible')
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
T.done('test_merge_signs')
