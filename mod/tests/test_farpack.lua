-- luajit mod/tests/test_farpack.lua
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

local calls = {}
local function rec(mod)
  return setmetatable({}, { __index = function(_, fn)
    if fn == 'placeRows' or fn == 'placeSpaces' then return nil end
    return function(...) calls[#calls + 1] = { mod, fn, ... } end
  end })
end
for _, m in ipairs({ 'trees', 'nightlights', 'parking', 'gasstations', 'pumpjacks', 'ambience' }) do
  package.loaded['beamngEarth/' .. m] = rec(m)
end
rawset(package.loaded['beamngEarth/gasstations'], 'MATERIAL_FILES', {})
local function called(mod, fn, arg1)
  local out = {}
  for _, c in ipairs(calls) do
    if c[1] == mod and c[2] == fn and (arg1 == nil or c[3] == arg1) then out[#out + 1] = c end
  end
  return out
end

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
roads.setFarGeo(true)
roads.setSession('s1')

local gov = {}
function gov:try() return true end
function gov:charge() end
function gov:chargeMotion() end
local function run(aid, a) for _ = 1, 6 do roads.update(gov, aid, car, nil, a) end end
local function objsWith(pat)
  local out = {}
  for _, o in pairs(S.objects) do
    if o.class == 'TSStatic' and tostring(o.fields.shapeName):find(pat, 1, true) then out[#out + 1] = o end
  end
  return out
end

local LAT, LON = 39.2834, -76.5934
local gx, gy = geotile.tileOf(LAT, LON)
local K = geotile.key(gx, gy)
local K2 = geotile.key(gx + 1, gy)
local sx, sy = math.floor(gx / 4), math.floor(gy / 4)
local SK = geotile.key(sx, sy, 15)
local clat, clon = geotile.centre(17, gx, gy)
local slat, slon = geotile.centre(15, sx, sy)
local A = geodesy.newAnchor(LAT, LON + 15000 / (111320 * math.cos(math.rad(LAT))))   -- 15 km east
local B = geodesy.newAnchor(LAT, LON)                                                  -- the rebase: here
car.x, car.y = A:geodeticToWorld(clat, clon, 0)
-- (anchorObj: the roads module's anchor, set by update)
run(1, A)
local function farMsg(key, frame, data)
  return { anchorId = 1, key = key, path = '/s/x.json', n = 1, frame = frame, pivot = data.pivot or 0, data = data }
end
-- roads.lua far building packs (server buildings.PACK_*): a pack of a skyline square shows until the square's 16 far
-- buildings tiles are here and made, the skyline of the square hides under it, and a far tile's drop waits for its
-- square's pack to be made.
local PK = SK
local function keysOf(sq)
  local _, x, y = geotile.parse(sq)
  local out = {}
  for j = 0, 3 do for i = 0, 3 do out[#out + 1] = geotile.key(x * 4 + i, y * 4 + j) end end
  return out
end
local function centreOf(k)
  local z, x, y = geotile.parse(k)
  return { geotile.centre(z, x, y) }
end
local sdata = { v = 1, key = SK, frame = { slat, slon }, h = 's1', shape = '/s/bs_' .. SK .. '.cdae',
  pos = { -470, -470, 0 }, pivot = 15 }
roads.onFarTile(farMsg(SK, { slat, slon }, sdata), 1, true)
run(1, A)
T.check(#objsWith('bs_' .. SK) == 1 and objsWith('bs_' .. SK)[1].hidden == false, 'skyline shown: no far tile, no pack')
local pdata = { v = 1, key = PK, frame = { slat, slon }, h = 'p1', shape = '/s/bp_' .. PK .. '.cdae',
  pos = { -470, -470, 0 }, pivot = 15, lamps = { [K] = { pivot = 20, rows = { { 0, 0, 20 } } } } }
T.check(roads.onFarTile(farMsg(PK, { slat, slon }, pdata), 1, 'P'), 'pack accepted')
run(1, A)
T.check(#objsWith('bp_' .. PK) == 1 and objsWith('bp_' .. PK)[1].hidden == false, 'the pack made and shown')
T.check(objsWith('bs_' .. SK)[1].hidden == true, 'the skyline of its square hidden under the shown pack')
T.check(#called('trees', 'onGeoRows', 'FLP' .. K) == 1, 'the pack\'s lamps on')
-- the square's far tiles come (one with buildings): the pack shows until that one is made
local keys = keysOf(PK)
for _, k in ipairs(keys) do
  local d = { v = 1, key = k, frame = centreOf(k), h = 'e' }
  if k == K then d.shape, d.pos, d.pivot, d.h = '/s/bf_' .. K .. '.cdae', { -118, -118, 0 }, 25, 'f1' end
  roads.onFarTile(farMsg(k, d.frame, d), 1)
end
T.check(roads.packCovered(roads.debugFar()[roads.farGeoKey('P', PK)]) == false, 'not covered before the far tile is made')
run(1, A)
T.check(#objsWith('bf_' .. K) == 1 and objsWith('bf_' .. K)[1].hidden == false, 'the far tile made and shown')
T.check(objsWith('bp_' .. PK)[1].hidden == true, 'the pack hidden once its square\'s far tiles all show')
T.check(objsWith('bs_' .. SK)[1].hidden == true, 'the skyline stays hidden (all 16 far tiles here)')
-- driving away: the far tiles dropped (the pack made: at once), the pack shows again
for _, k in ipairs(keys) do roads.onFarDrop({ anchorId = 1, key = k }) end
run(1, A)
T.check(#objsWith('bf_' .. K) == 0, 'far tiles dropped at once: their pack is made')
T.check(objsWith('bp_' .. PK)[1].hidden == false, 'the pack shows again')
-- a far tile's drop waits for a pack not made yet
for _, k in ipairs(keys) do
  local d = { v = 1, key = k, frame = centreOf(k), h = 'e2' }
  if k == K then d.shape, d.pos, d.pivot = '/s/bf2_' .. K .. '.cdae', { -118, -118, 0 }, 25 end
  roads.onFarTile(farMsg(k, d.frame, d), 1)
end
run(1, A)
roads.onFarDrop({ anchorId = 1, key = PK }, 'P')
run(1, A)
T.check(#objsWith('bp_') == 0, 'the pack dropped')
local pdata2 = {}
for k, v in pairs(pdata) do pdata2[k] = v end
pdata2.h, pdata2.shape = 'p2', '/s/bp2_' .. PK .. '.cdae'
roads.onFarTile(farMsg(PK, { slat, slon }, pdata2), 1, 'P')
for _, k in ipairs(keys) do roads.onFarDrop({ anchorId = 1, key = k }) end
local fk = roads.debugFar()[roads.farGeoKey('F', K)]
T.check(fk ~= nil and fk.dropWait ~= nil, 'a far tile\'s drop waits while its square\'s pack is not made')
for _ = 1, 3 do run(1, A) end
T.check(#objsWith('bp2_' .. PK) == 1 and objsWith('bp2_' .. PK)[1].hidden == false, 'the new pack made and shown')
T.check(#objsWith('bf2_' .. K) == 0 and roads.debugFar()[roads.farGeoKey('F', K)] == nil, 'then the far tile went')
T.done('test_farpack')
