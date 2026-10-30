-- luajit mod/tests/test_fargeo.lua
-- roads.lua's far geotiles (server blockworker.far_geo, .claude/work/geotiles_plan.md phase 2): the far buildings
-- (g17, the road geotiles' grid), the skyline (g15) and the far bridges (g15, a piece per road geotile) are placed from
-- their own frames at their pivots, kept across a rebase (re-placed, nothing made or deleted), shown by one ownership
-- rule by geotile key (a far buildings tile hides once the road geotile of its key shows its buildings, a far bridge
-- piece once its road geotile shows its deck and road mesh, a skyline tile once all 16 far tiles of its square are
-- here), their lamps trees.lua geotiles; a server restart with the same content hash keeps them; drops.
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

local function farMsg(key, frame, data, kind)
  return { anchorId = 1, key = key, path = '/s/x.json', n = 1, frame = frame, pivot = data.pivot or 0, data = data }
end

-- a far buildings tile of K with its lamps, a skyline tile of K's square, a far bridge tile with pieces in K and K2
local fdata = { v = 1, key = K, frame = { clat, clon }, h = 'f1', shape = '/s/bf_' .. K .. '.cdae',
  pos = { -118, -118, 0 }, pivot = 25, relief = 40,
  lamps = { [K] = { pivot = 30, rows = { { 1, 2, 30 }, { 5, 6, 31 } } } } }
T.check(roads.onFarTile(farMsg(K, { clat, clon }, fdata), 1), 'far buildings geotile accepted')
local sdata = { v = 1, key = SK, frame = { slat, slon }, h = 's1', shape = '/s/bs_' .. SK .. '.cdae',
  pos = { -470, -470, 0 }, pivot = 15, lamps = { [K2] = { pivot = 20, rows = { { 0, 0, 20 } } } } }
T.check(roads.onFarTile(farMsg(SK, { slat, slon }, sdata), 1, true), 'skyline geotile accepted')
local bdata = { v = 1, key = SK, frame = { slat, slon }, h = 'b1',
  parts = { [K] = { shape = '/s/bb_' .. SK .. '_' .. K .. '.cdae', pos = { -470, -470, 0 }, pivot = 12 },
            [K2] = { shape = '/s/bb_' .. SK .. '_' .. K2 .. '.cdae', pos = { -470, -470, 0 }, pivot = 14 } } }
T.check(roads.onFarTile(farMsg(SK, { slat, slon }, bdata), 1, 'B'), 'far bridge geotile accepted')
run(1, A)
local f1 = objsWith('bf_' .. K)
T.check(#f1 == 1 and f1[1].hidden == false, 'far buildings made and shown (no road geotile here)')
T.check(#objsWith('bs_' .. SK) == 1 and objsWith('bs_' .. SK)[1].hidden == false, 'skyline shown: its square incomplete')
T.check(#objsWith('bb_' .. SK) == 2, 'a far bridge piece per road geotile')
local frame = geotile.frameOf({ frame = { clat, clon } })
do
  local pl = geotile.placement(frame, A, 0, 0, 25)
  local xf = geodesy.transformOf(pl.r, pl.t)
  local x, y, z = xf:apply(-118, -118, 0)
  T.near(f1[1].pos.x, x, 1e-9, 'far buildings placed at their pivot: x')
  T.near(f1[1].pos.y, y, 1e-9, 'far buildings placed at their pivot: y')
  T.near(f1[1].pos.z, z, 1e-9, 'far buildings placed at their pivot: z')
end
T.check(#called('trees', 'onGeoRows', 'FLF' .. K) == 1, 'its lamps a trees geotile of its key')
T.check(#called('trees', 'onGeoRows', 'FLS' .. K2) == 1, 'the skyline lamps per road geotile key')

-- the other 15 far tiles of the square (no buildings): the skyline hides, its lamps go
for j = 0, 3 do
  for i = 0, 3 do
    local k = geotile.key(sx * 4 + i, sy * 4 + j)
    if k ~= K then
      roads.onFarTile(farMsg(k, nil, { v = 1, key = k, frame = { geotile.centre(17, sx * 4 + i, sy * 4 + j) }, h = 'e' }), 1)
    end
  end
end
run(1, A)
T.check(objsWith('bs_' .. SK)[1].hidden == true, 'skyline hidden: its square\'s far tiles all here')
T.check(#called('trees', 'dropKey', 'g:FLS' .. K2) == 1, 'its lamps dropped')

-- the road geotile K arrives (buildings and a deck): the far buildings of K hide once its buildings are made; the
-- bridge piece of K once its deck and its skin's road mesh are; K2's stays
files['/s/rg.json'] = string.format([[{ v = 1, key = "%s", frame = { %.10f, %.10f }, h = "abc",
  bld = { shape = "/s/rg_bld.cdae", pos = { -118, -118, 0 }, pivot = 20, relief = 30 },
  deck = { shape = "/s/rg_deck.cdae", pos = { -118, -118, 0 }, pivot = 12, relief = 2 },
  colliders = { deck = { { -10, -10, 5, 10, 10, 15 } } } }]], K, clat, clon)
T.check(roads.nearShowsBuildings(K) == false, 'no road geotile: not covered')
roads.onTile({ anchorId = 1, key = K, path = '/s/rg.json', n = 0, frame = { clat, clon }, h = 'abc' }, 1, A)
T.check(roads.nearShowsBuildings(K) == false, 'its buildings not made yet: the far ones stay')
run(1, A)
T.check(objsWith('bf_' .. K)[1].hidden == true, 'far buildings of K hidden: the road geotile shows its own')
T.check(#called('trees', 'dropKey', 'g:FLF' .. K) == 1, 'their lamps dropped')
local function piece(k)
  for _, o in ipairs(objsWith('bb_' .. SK .. '_' .. k)) do return o end
end
T.check(piece(K).hidden == false, 'bridge piece of K shown: no road mesh yet')
local x0, y0 = A:geodeticToWorld(clat, clon, 0)
files['/s/rs_1.json'] = string.format([[{ key = "%s", anchorId = 1, q = 0.03125, pivot = { %f, %f, 10 },
  road = { shape = "/s/rs_1_road.cdae", pos = { %f, %f, 0 } } }]], K, x0, y0, x0 - 118, y0 - 118)
roads.onSkin({ type = 'roads_skin', anchorId = 1, key = K, path = '/s/rs_1.json' }, 1)
run(1, A)
T.check(piece(K).hidden == true and piece(K2).hidden == false, 'bridge piece of K hidden, K2\'s shown')
T.check(objsWith('bf_' .. K)[1].hidden == true, 'still one of the two shows K\'s buildings')

-- the rebase to B: nothing made or deleted; every far part re-placed at its pivot
local made0 = S.calls['createObject_TSStatic'] or 0
local del0 = S.calls['delete_TSStatic'] or 0
car.x, car.y = B:geodeticToWorld(clat, clon, 0)
local xf = A:worldTransformTo(B, car.x, car.y, 0)
roads.transform(xf, geodesy)
roads.replace(B, 2)
run(2, B)
T.check((S.calls['createObject_TSStatic'] or 0) == made0 and (S.calls['delete_TSStatic'] or 0) == del0,
  'no object made or deleted at the rebase')
do
  local pl = geotile.placement(frame, B, 0, 0, 25)
  local xfb = geodesy.transformOf(pl.r, pl.t)
  local x, y, z = xfb:apply(-118, -118, 0)
  local o = objsWith('bf_' .. K)[1]
  T.near(o.pos.x, x, 1e-9, 'far buildings re-placed: x')
  T.near(o.pos.y, y, 1e-9, 'far buildings re-placed: y')
  T.near(o.pos.z, z, 1e-9, 'far buildings re-placed: z')
  local qx, qy, qz, qw = xfb:beamngQuat()
  T.near(o.rot.w, qw, 1e-12, 'far buildings re-placed: rotation')
  -- a point of the tile at its pivot height lands within 3 mm of where the exact world map puts it
  local ex, ey, ez = geotile.worldMap(frame, B, 80, -60, 25)
  local rx, ry, rz = xfb:apply(80, -60, 25)
  T.check(math.abs(ex - rx) < 0.003 and math.abs(ey - ry) < 0.003 and math.abs(ez - rz) < 0.003,
    'rigid at the pivot: within 3 mm of the exact map')
end
do
  local sframe = geotile.frameOf({ frame = { slat, slon } })
  local pl = geotile.placement(sframe, B, 0, 0, 14)
  local x, y = geodesy.transformOf(pl.r, pl.t):apply(-470, -470, 0)
  T.near(piece(K2).pos.x, x, 1e-9, 'bridge piece re-placed at its own pivot')
  T.near(piece(K2).pos.y, y, 1e-9, 'bridge piece re-placed: y')
end
T.check(roads.status().farGeoTiles == 18, 'kept: 16 far buildings tiles, the skyline, the bridges')
T.check(objsWith('bf_' .. K)[1].hidden == true, 'the ownership rule holds across the rebase')

-- the same content again (a server restart): kept; new content replaces
roads.newSession()
local made1 = S.calls['createObject_TSStatic'] or 0
roads.onFarTile(farMsg(K, { clat, clon }, fdata), 1)
run(1, B)
T.check((S.calls['createObject_TSStatic'] or 0) == made1 and #objsWith('bf_' .. K) == 1,
  'same hash after a restart: kept, nothing made again')
local fdata2 = {}
for k, v in pairs(fdata) do fdata2[k] = v end
fdata2.h, fdata2.shape = 'f2', '/s/bf2_' .. K .. '.cdae'
roads.onFarTile(farMsg(K, { clat, clon }, fdata2), 1)
run(1, B)
T.check(#objsWith('bf_' .. K) == 0 and #objsWith('bf2_' .. K) == 1, 'new content: the old object gone, the new made')

-- drops
roads.onFarDrop({ anchorId = 1, key = SK }, 'B')
roads.onFarDrop({ anchorId = 1, key = SK }, true)
roads.onFarDrop({ anchorId = 1, key = K })
run(1, B)
T.check(#objsWith('bb_') == 0 and #objsWith('bs_') == 0 and #objsWith('bf2_') == 0, 'dropped: their objects gone')

T.done('test_fargeo')
