-- luajit mod/tests/test_swapgen.lua
-- A generation swap's commit barrier (roads.lua swapBegin / onSkin {swap} / swapStaged / swapCommit / swapAbort;
-- server earthd Connection.stale_swap): a skin sent with `swap = gen` is STAGED: its parts are made hidden and without
-- collision while the old skin stays exactly as it is, shown and colliding; swapCommit shows the new ones, gives them
-- their collision and takes the old ones out of sight and out of the collision in the same call (the frame core.lua
-- commits the swap's terrain); swapAbort drops what was staged and the old skin is the tile's again. Never an old
-- road surface or sidewalk collision beside the new ground's.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

local function rec()
  return setmetatable({}, { __index = function(_, fn)
    if fn == 'placeRows' or fn == 'placeSpaces' then return nil end
    return function() end
  end })
end
for _, m in ipairs({ 'trees', 'nightlights', 'parking', 'gasstations', 'pumpjacks', 'ambience' }) do
  package.loaded['beamngEarth/' .. m] = rec()
end
rawset(package.loaded['beamngEarth/gasstations'], 'MATERIAL_FILES', {})

local files = {}
readFile = function(p) return files[p] end
jsonDecode = function(s) return assert(load('return ' .. s))() end
local car = { x = 0, y = 0, z = 0 }
getPlayerVehicle = function() return { getPosition = function() return vec3(car.x, car.y, car.z) end,
  getVelocity = function() return vec3(0, 0, 0) end } end

local geodesy = require('beamngEarth/geodesy')
local geotile = require('beamngEarth/geotile')
local roads = require('beamngEarth/roads')
roads.setGeo(true)
roads.setSession('s1')

local gov = {}
function gov:try() return true end
function gov:charge() end
function gov:chargeMotion() end
local LAT, LON = 39.2834, -76.5934
local A = geodesy.newAnchor(LAT, LON)
local gx, gy = geotile.tileOf(LAT, LON)
local key = geotile.key(gx, gy)
local clat, clon = geotile.centre(17, gx, gy)
-- the car far from the tile's sidewalks (their colliders are held while the car stands in their boxes)
car.x, car.y = A:geodeticToWorld(clat, clon, 0)
car.x = car.x + 60
local function run() for _ = 1, 8 do roads.update(gov, 1, car, nil, A) end end

files['/s/rg.json'] = string.format([[{ v = 1, key = "%s", frame = { %.10f, %.10f }, h = "abc",
  bld = { shape = "/s/rg_bld.cdae", pos = { -118, -118, 0 }, pivot = 20, relief = 30 } }]], key, clat, clon)
local function skinOf(tag, parts)
  local x, y = A:geodeticToWorld(clat, clon, 0)
  local s = string.format('{ key = "%s", anchorId = 1, q = 0.03125, pivot = { %f, %f, 10 }', key, x, y)
  for _, w in ipairs(parts or { 'road', 'walk', 'walkcol' }) do
    s = s .. string.format(', %s = { shape = "/s/rs_%s_%s.cdae", pos = { %f, %f, 0 } }', w, tag, w, x - 118, y - 118)
  end
  s = s .. ', colliders = { walk = { { 0, 0, 0, 1, 1, 1 } }, walkcol = { { 0, 0, 0, 1, 1, 1 } } } }'
  files['/s/rs_' .. tag .. '.json'] = s
  return '/s/rs_' .. tag .. '.json'
end
local function objs(tag)
  local out = {}
  for _, o in pairs(S.objects) do
    if o.class == 'TSStatic' then
      local what = tostring(o.fields.shapeName):match('/s/rs_' .. tag .. '_(%a+)%.cdae')
      if what then out[what] = o end
    end
  end
  return out
end
local function count(t) local n = 0; for _ in pairs(t) do n = n + 1 end; return n end

T.check(roads.onTile({ anchorId = 1, key = key, path = '/s/rg.json', n = 0, frame = { clat, clon }, h = 'abc' }, 1, A),
  'the geotile accepted')
T.check(roads.onSkin({ type = 'roads_skin', anchorId = 1, key = key, path = skinOf('old') }, 1), 'the first skin')
run()
local old = objs('old')
T.check(count(old) == 3, 'the old skin: road, walk and its lean collision mesh')
T.check(old.walkcol.fields.collisionType == 'Collision Mesh' and old.road.fields.collisionType == 'None',
  'the old skin collides by its collision mesh')

-- a skin of a swap that is not running is not wanted
T.check(roads.onSkin({ type = 'roads_skin', anchorId = 1, key = key, path = skinOf('x'), swap = 3 }, 1) == false,
  'a staged skin without its swap is refused')
run()
T.check(count(objs('x')) == 0 and count(objs('old')) == 3, '... and nothing changed')

-- ---- staged: the new skin beside the old one, unseen and without collision --------------------------------
roads.swapBegin(7)
T.check(roads.swapStaged(7), 'an empty swap is staged')
T.check(roads.onSkin({ type = 'roads_skin', anchorId = 1, key = key, path = skinOf('new'), swap = 7 }, 1),
  'the staged skin accepted')
T.check(not roads.swapStaged(7), 'not staged while its parts are queued')
run()
T.check(roads.swapStaged(7), 'staged once every part is made')
local new = objs('new')
old = objs('old')
T.check(count(new) == 3, 'the new parts are made')
T.check(count(old) == 3, 'the old parts are all still there')
for w, o in pairs(old) do
  T.check(o.hidden == false, 'the old ' .. w .. ' is still shown')
end
T.check(old.walkcol.fields.collisionType == 'Collision Mesh', 'the old collision mesh still collides')
for w, o in pairs(new) do
  T.check(o.hidden == true, 'the new ' .. w .. ' is hidden')
  T.check(o.fields.collisionType == 'None', 'the new ' .. w .. ' does not collide yet')
end
-- frames go by (the swap's terrain is still being staged): nothing changes
run(); run()
T.check(count(objs('old')) == 3 and objs('old').road.hidden == false and objs('new').road.hidden == true,
  'the stage holds over the frames')
T.check(not roads.swapCommit(8), 'another swap cannot commit it')
T.check(roads.swapCount(7) == 3, 'three parts staged')

-- ---- the commit: one call --------------------------------------------------------------------------------
T.check(roads.swapCommit(7), 'committed')
new = objs('new')
for w, o in pairs(new) do T.check(o.hidden == false, 'the new ' .. w .. ' is shown') end
T.check(new.walkcol.fields.collisionType == 'Collision Mesh', 'the new collision mesh collides')
T.check(new.road.fields.collisionType == 'None' and new.walk.fields.collisionType == 'None',
  'the drawn meshes stay render-only')
for w, o in pairs(objs('old')) do
  T.check(o.hidden == true and o.fields.collisionType == 'None', 'the old ' .. w .. ' is out of sight and collision at once')
end
-- the old objects are KEPT until the commit is known to stand (its terrain imported, the collision in effect)
T.check(count(objs('old')) == 3, 'the old parts are kept, hidden, until the swap is finished')
T.check(not roads.swapSettled(7), 'not settled before the collision was rebuilt')
local r0 = S.calls.reloadCollision or 0
roads.update(gov, 1, car, nil, A)
T.check((S.calls.reloadCollision or 0) == r0 + 1, 'the commit forces a collision rebuild in the next update')
T.check(not roads.swapSettled(7), '... in effect a frame later')
roads.update(gov, 1, car, nil, A)
T.check(roads.swapSettled(7), 'settled: the rebuild ran and a frame has passed')
T.check(count(objs('old')) == 3, 'still kept')
T.check(roads.swapFinish(7), 'finished')
run()
T.check(count(objs('old')) == 0, 'the old parts are deleted after')
T.check(count(objs('new')) == 3, 'the new skin stays')
T.check(roads._swapState() == nil, 'no swap left')

-- ---- the collision waits out a terrain swap's quiet frames -------------------------------------------------
roads.swapBegin(20)
roads.onSkin({ type = 'roads_skin', anchorId = 1, key = key, path = skinOf('q'), swap = 20 }, 1)
run()
T.check(roads.swapStaged(20), 'staged')
roads.swapCommit(20)
roads.terrainSwapped()          -- (core's commitTerrain with a swapped .ter: its collision follows by itself)
r0 = S.calls.reloadCollision or 0
local n = 0
local tEnd = os.clock() + 3
while not roads.swapSettled(20) and os.clock() < tEnd do
  roads.update(gov, 1, car, nil, A)
  n = n + 1
  if roads.swapQuiet() then
    T.check((S.calls.reloadCollision or 0) == r0, 'no collision rebuild in the terrain swap\'s quiet frames')
    T.check(not roads.swapSettled(20), 'and not settled meanwhile')
  end
end
T.check(roads.swapSettled(20) and n >= roads.SWAP_QUIET_FRAMES + 1, 'settled only after the quiet frames and the rebuild')
T.check((S.calls.reloadCollision or 0) == r0 + 1, 'one rebuild')
roads.swapFinish(20)
run()
T.check(count(objs('new')) == 0 and count(objs('q')) == 3, 'the swap stands')

-- ---- a commit taken back (the terrain failed, or the collision never came): the old ground whole ------------
roads.swapBegin(21)
roads.onSkin({ type = 'roads_skin', anchorId = 1, key = key, path = skinOf('u'), swap = 21 }, 1)
run()
roads.swapCommit(21)
T.check(objs('q').road.hidden == true and objs('u').road.hidden == false, 'committed: new shown, old hidden')
T.check(roads.swapUndo(21), 'taken back')
T.check(not roads.swapRolledBack(), 'the rollback\'s collision is still to come')
for w, o in pairs(objs('q')) do T.check(o.hidden == false, 'the old ' .. w .. ' is shown again') end
T.check(objs('q').walkcol.fields.collisionType == 'Collision Mesh' and objs('q').walk.fields.collisionType == 'None'
  and objs('q').road.fields.collisionType == 'None', 'with the collision it had')
for w, o in pairs(objs('u')) do
  T.check(o.hidden == true and o.fields.collisionType == 'None', 'the new ' .. w .. ' is out of sight and collision')
end
do
  local r1 = S.calls.reloadCollision or 0
  local tEnd = os.clock() + 3
  while not roads.swapRolledBack() and os.clock() < tEnd do roads.update(gov, 1, car, nil, A) end
  T.check(roads.swapRolledBack(), 'rolled back: its collision rebuilt and a frame passed')
  T.check((S.calls.reloadCollision or 0) > r1, 'by a rebuild the rollback forced')
end
run()
T.check(count(objs('u')) == 0 and count(objs('q')) == 3, 'the staged parts are gone, the old skin is the tile\'s')
T.check(roads._swapState() == nil, 'no swap left')
T.check(roads.onSkin({ type = 'roads_skin', anchorId = 1, key = key, path = skinOf('new') }, 1), 'an ordinary skin after')
run()
T.check(count(objs('new')) == 3 and count(objs('q')) == 0, 'and the tile takes an ordinary skin after it')

-- ---- staging failures: the swap is not ready, and says why ---------------------------------------------------
roads.swapBegin(30)
local ok30, why30 = roads.swapStaged(30, 1)
T.check(ok30 == false and why30 == nil, 'the server staged a skin that has not come yet: not ready, not failed')
files['/s/none.json'] = nil
T.check(roads.onSkin({ type = 'roads_skin', anchorId = 1, key = key, path = '/s/none.json', swap = 30 }, 1) == false,
  'an unreadable staged skin is refused')
ok30, why30 = roads.swapStaged(30)
T.check(ok30 == false and type(why30) == 'string', 'an unreadable staged skin fails the swap: ' .. tostring(why30))
roads.swapAbort(30)
roads.swapBegin(31)
T.check(roads.onSkin({ type = 'roads_skin', anchorId = 1, key = 'g17_1_1', path = skinOf('x'), swap = 31 }, 1) == false,
  'a staged skin of a tile the mod has not')
T.check(select(2, roads.swapStaged(31)) ~= nil, '... fails the swap')
roads.swapAbort(31)
roads.swapBegin(32)
roads.onSkin({ type = 'roads_skin', anchorId = 1, key = key, path = skinOf('bad2'), swap = 32 }, 1)
local realCreate = createObject
local made32 = 0
createObject = function(class)          -- (the second shape cannot be made)
  made32 = made32 + 1
  if made32 == 2 then error('no such shape') end
  return realCreate(class)
end
run()
createObject = realCreate
local ok32, why32 = roads.swapStaged(32)
T.check(ok32 == false and type(why32) == 'string', 'a part that could not be made fails the swap: ' .. tostring(why32))
T.check(count(objs('new')) == 3 and objs('new').road.hidden == false, 'the old skin untouched')
roads.swapAbort(32)
run()
T.check(count(objs('bad2')) == 0 and count(objs('new')) == 3, 'and whole after the abort')

-- ---- a swap given up: the old ground stays whole ---------------------------------------------------------
roads.swapBegin(9)
T.check(roads.onSkin({ type = 'roads_skin', anchorId = 1, key = key, path = skinOf('bad'), swap = 9 }, 1), 'staged again')
run()
T.check(count(objs('bad')) == 3 and objs('bad').road.hidden == true, 'its parts staged')
T.check(roads.swapAbort(9), 'given up')
run()
T.check(count(objs('bad')) == 0, 'what was staged is gone')
local kept = objs('new')
T.check(count(kept) == 3, 'the skin of before is whole')
for w, o in pairs(kept) do T.check(o.hidden == false, 'its ' .. w .. ' is shown') end
T.check(kept.walkcol.fields.collisionType == 'Collision Mesh', 'and collides')
-- a part still queued when the swap is given up is never made
roads.swapBegin(10)
roads.onSkin({ type = 'roads_skin', anchorId = 1, key = key, path = skinOf('late'), swap = 10 }, 1)
roads.swapAbort(10)
run()
T.check(count(objs('late')) == 0 and count(objs('new')) == 3, 'a staged part queued at the abort is not made')
-- and the tile takes an ordinary skin after it (the old one swapped part by part, as ever)
T.check(roads.onSkin({ type = 'roads_skin', anchorId = 1, key = key, path = skinOf('next') }, 1), 'an ordinary skin after')
run()
T.check(count(objs('next')) == 3 and count(objs('new')) == 0, 'the ordinary swap still works')
for w, o in pairs(objs('next')) do T.check(o.hidden == false, 'its ' .. w .. ' is shown') end

-- ---- a staged skin without sidewalks: the old ones go at the commit ---------------------------------------
roads.swapBegin(11)
roads.onSkin({ type = 'roads_skin', anchorId = 1, key = key, path = skinOf('bare', { 'road' }), swap = 11 }, 1)
run()
T.check(roads.swapStaged(11) and count(objs('next')) == 3, 'staged; the old sidewalks still there')
roads.swapCommit(11)
T.check(objs('next').walk.hidden == true and objs('next').walkcol.fields.collisionType == 'None',
  'the old sidewalks out of sight and collision at the commit')
local tEnd11 = os.clock() + 3
while not roads.swapSettled(11) and os.clock() < tEnd11 do roads.update(gov, 1, car, nil, A) end
T.check(roads.swapSettled(11) and roads.swapFinish(11), 'settled and finished')
run()
T.check(count(objs('next')) == 0 and count(objs('bare')) == 1, 'and deleted after')

-- ---- the level ends with a swap under way: nothing of it is left ------------------------------------------------
roads.swapBegin(40)
roads.onSkin({ type = 'roads_skin', anchorId = 1, key = key, path = skinOf('end'), swap = 40 }, 1)
run()
roads.swapCommit(40)
roads.reset()
T.check(roads._swapState() == nil and roads.swapRolledBack(), 'reset forgets the swap')
T.check(roads.swapStaged(40) == false and not roads.swapCommit(40) and not roads.swapFinish(40), 'and it cannot go on')
T.done('test_swapgen')
