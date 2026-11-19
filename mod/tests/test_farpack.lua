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
-- the horizon ring (kind 'H', server bldhz.py): a zoom-12 tile's pieces, one a skyline square; a piece hides while its
-- square has its skyline tile made, its pack made, or a far buildings tile under it seen
roads.onFarDrop({ anchorId = 1, key = PK }, 'P')
roads.onFarDrop({ anchorId = 1, key = SK }, true)
run(1, A)
local hx, hy = math.floor(gx / 32), math.floor(gy / 32)
local HK = geotile.key(hx, hy, 12)
local hlat, hlon = geotile.centre(12, hx, hy)
local OK = geotile.key(sx + 1, sy, 15)          -- (another square under the same tile, nothing of its own there)
local hdata = { v = 1, key = HK, frame = { hlat, hlon }, h = 'h1', parts = {
  [SK] = { shape = '/s/bh_' .. HK .. '_' .. SK .. '.cdae', pos = { -3700, -3700, 0 }, pivot = 40 },
  [OK] = { shape = '/s/bh_' .. HK .. '_' .. OK .. '.cdae', pos = { -3700, -3700, 0 }, pivot = 40 } } }
T.check(roads.onFarTile(farMsg(HK, { hlat, hlon }, hdata), 1, 'H'), 'horizon tile accepted')
for _ = 1, 3 do run(1, A) end
local hf = roads.debugFar()[roads.farGeoKey('H', HK)]
local function debugSky() return roads.debugFar()[roads.farGeoKey('S', SK)] end
T.check(hf ~= nil and hf.parts[SK] ~= nil and hf.parts[OK] ~= nil, 'a piece per skyline square')
T.check(roads.farGeoHidden(hf, SK) == false and roads.farGeoHidden(hf, OK) == false, 'nothing covers either square yet')
T.check(#objsWith('bh_' .. HK .. '_' .. OK) == 1 and objsWith('bh_' .. HK .. '_' .. OK)[1].hidden == false,
  'the uncovered piece made and shown')
roads.onFarTile(farMsg(SK, { slat, slon }, sdata), 1, true)
for _ = 1, 3 do run(1, A) end
T.check(roads.farGeoHidden(hf, SK) == true, 'a piece hides under its square\'s made skyline tile')
T.check(roads.farGeoHidden(hf, OK) == false, 'the other square\'s piece stays')
roads.onFarDrop({ anchorId = 1, key = SK }, true)
run(1, A)
T.check(roads.farGeoHidden(hf, SK) == false, 'the skyline tile gone: the piece shows again')
roads.onFarTile(farMsg(keys[1], centreOf(keys[1]), { v = 1, key = keys[1], frame = centreOf(keys[1]), h = 'e3' }), 1)
run(1, A)
T.check(roads.farGeoHidden(hf, SK) == false, 'one far tile of sixteen under its square: the piece stays (partial arrival)')
for i = 2, #keys - 1 do
  roads.onFarTile(farMsg(keys[i], centreOf(keys[i]), { v = 1, key = keys[i], frame = centreOf(keys[i]), h = 'e3' }), 1)
end
run(1, A)
T.check(roads.farGeoHidden(hf, SK) == false, 'fifteen of sixteen: the piece still stays')
roads.onFarTile(farMsg(keys[#keys], centreOf(keys[#keys]), { v = 1, key = keys[#keys], frame = centreOf(keys[#keys]), h = 'e3' }), 1)
run(1, A)
T.check(roads.farGeoHidden(hf, SK) == true, 'all far buildings tiles under its square in and made: hidden')
for _, k in ipairs(keys) do roads.onFarDrop({ anchorId = 1, key = k }) end
run(1, A)
T.check(roads.farGeoHidden(hf, SK) == false, 'the far tiles dropped: the piece shows again')
-- handover: a made skyline tile stays while its square's far tiles arrive; one far mesh still to make
roads.onFarTile(farMsg(SK, { slat, slon }, sdata), 1, true)
for _ = 1, 3 do run(1, A) end
T.check(roads.farGeoHidden(hf, SK) == true, 'the skyline tile back: the piece hides')
for _, k in ipairs(keys) do
  local d = { v = 1, key = k, frame = centreOf(k), h = 'e4' }
  if k == K then d.shape, d.pos, d.pivot, d.h = '/s/bf_' .. K .. '.cdae', { -118, -118, 0 }, 25, 'f4' end
  roads.onFarTile(farMsg(k, d.frame, d), 1)
end
T.check(roads.farGeoHidden(debugSky(), nil) == true, 'the skyline hands over: hidden once all 16 far tiles seen')
T.check(roads.farGeoHidden(hf, SK) == false, 'a far mesh still to make: the piece stays though the skyline hides')
for _ = 1, 3 do run(1, A) end
T.check(roads.farGeoHidden(hf, SK) == true, 'all far meshes made: the piece hides')
for _, k in ipairs(keys) do roads.onFarDrop({ anchorId = 1, key = k }) end
roads.onFarDrop({ anchorId = 1, key = SK }, true)
run(1, A)
roads.onFarDrop({ anchorId = 1, key = HK }, 'H')
run(1, A)
T.check(roads.debugFar()[roads.farGeoKey('H', HK)] == nil and #objsWith('bh_' .. HK) == 0, 'the horizon tile dropped')
-- a pack whose square misses one far tile hides once that geotile's road tile shows its own buildings (after a fast
-- run the far field lagged 30-60 s and the pack's painted walls stood over the real buildings beside a stopped car)
do
  local P2 = geotile.key(sx + 1, sy, 15)
  local p2lat, p2lon = geotile.centre(15, sx + 1, sy)
  roads.onFarTile(farMsg(P2, { p2lat, p2lon }, { v = 1, key = P2, frame = { p2lat, p2lon }, h = 'q1',
    shape = '/s/bp_' .. P2 .. '.cdae', pos = { -470, -470, 0 }, pivot = 15 }), 1, 'P')
  run(1, A)
  -- (a retained skyline tile of the square and a horizon piece over it: hidden under the pack, and still after it hands
  -- over to the mix of near and far tiles below; Codex review)
  local s2 = { v = 1, key = P2, frame = { p2lat, p2lon }, h = 's2', shape = '/s/bs_' .. P2 .. '.cdae',
    pos = { -470, -470, 0 }, pivot = 15 }
  roads.onFarTile(farMsg(P2, { p2lat, p2lon }, s2), 1, true)
  local hx2, hy2 = math.floor((sx + 1) / 8), math.floor(sy / 8)
  local H2 = geotile.key(hx2, hy2, 12)
  local h2lat, h2lon = geotile.centre(12, hx2, hy2)
  roads.onFarTile(farMsg(H2, { h2lat, h2lon }, { v = 1, key = H2, frame = { h2lat, h2lon }, h = 'h2', parts = {
    [P2] = { shape = '/s/bh_' .. H2 .. '_' .. P2 .. '.cdae', pos = { -3700, -3700, 0 }, pivot = 40 } } }), 1, 'H')
  for _ = 1, 3 do run(1, A) end
  local s2f = roads.debugFar()[roads.farGeoKey('S', P2)]
  local h2f = roads.debugFar()[roads.farGeoKey('H', H2)]
  T.check(roads.farGeoHidden(s2f) == true, 'the skyline hidden under the shown pack')
  T.check(roads.farGeoHidden(h2f, P2) == true, 'the horizon piece hidden: the square\'s pack made')
  local ks = keysOf(P2)
  local missing = geotile.key((sx + 1) * 4, gy)        -- (the square's geotile nearest the car: its road tile is made)
  for _, k in ipairs(ks) do
    if k ~= missing then roads.onFarTile(farMsg(k, centreOf(k), { v = 1, key = k, frame = centreOf(k), h = 'e9' }), 1) end
  end
  run(1, A)
  local pk = roads.debugFar()[roads.farGeoKey('P', P2)]
  T.check(roads.packCovered(pk) == false, 'one far tile of sixteen missing: the pack stays')
  local mc = centreOf(missing)
  car.x, car.y = A:geodeticToWorld(mc[1], mc[2], 0)   -- (the car beside it: buildings are made near the car)
  files['/s/rg_m.json'] = string.format([[{ v = 1, key = "%s", frame = { %.10f, %.10f }, h = "m1",
    bld = { shape = "/s/rgm_bld.cdae", pos = { -118, -118, 0 }, pivot = 20, relief = 30 } }]], missing, mc[1], mc[2])
  local okT = roads.onTile({ anchorId = 1, key = missing, path = '/s/rg_m.json', n = 0, frame = mc, h = 'm1' }, 1, A)
  T.check(okT, 'the road tile accepted')
  T.check(roads.packCovered(pk) == false, 'its road tile here, buildings not made yet: still')
  for _ = 1, 10 do run(1, A) end
  T.check(roads.nearShowsBuildings(missing) == true, 'the road tile shows its buildings')
  T.check(roads.packCovered(pk) == true, 'covered: the missing far tile\'s geotile shows its own buildings')
  T.check(objsWith('bp_' .. P2)[1].hidden == true, 'the pack hidden over the real buildings')
  T.check(roads.farGeoHidden(s2f) == true, 'the skyline stays hidden: near and far tiles cover its square')
  T.check(roads.farGeoHidden(h2f, P2) == true, 'the horizon piece stays hidden too')
  roads.onFarDrop({ anchorId = 1, key = P2 }, 'P')        -- (the pack gone: the horizon piece's own near/far rule decides)
  run(1, A)
  T.check(roads.debugFar()[roads.farGeoKey('P', P2)] == nil, 'the pack dropped')
  T.check(roads.farGeoHidden(h2f, P2) == true, 'the horizon piece still hidden: near and far tiles cover the square')
end
T.done('test_farpack')
