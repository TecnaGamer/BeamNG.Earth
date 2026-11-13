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
run()
T.check(count(objs('old')) == 0, 'the old parts are deleted after')
T.check(count(objs('new')) == 3, 'the new skin stays')
T.check(roads._swapState() == nil, 'no swap left')

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
run()
T.check(count(objs('next')) == 0 and count(objs('bare')) == 1, 'and deleted after')

roads.reset()
T.done('test_swapgen')
