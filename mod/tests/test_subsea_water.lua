-- Numerical water-volume bounds and real sea/roads lifecycle consumers; engine calls are stubbed.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local G = require('beamngEarth/waterguard')
local realCreate = createObject
createObject = function(class)
  local o = realCreate(class)
  function o:getId() return self.id end
  function o:setScale(s) self.scale = vec3(s.x, s.y, s.z) end
  return o
end
local function count(t) local n = 0; for _ in pairs(t) do n = n + 1 end; return n end
local records = { bore = { r = 7, h = 6.8, nodes = { { -500, 0, -40 }, { 500, 0, -40 } } } }
local segs = G.segments(records)
local root = { x = 0, y = 0, z = 0, size = 1024, nx = 0, ny = 0 }
local tiles = G.blocks({ bay = root }, segs, 100)
T.check(count(tiles) <= G.MAX_BLOCKS and count(tiles) > 1, 'adaptive subdivisions have a bounded object count')
local protected, deep, surfaceArea = 0, 0, 0
for _, t in pairs(tiles) do
  surfaceArea = surfaceArea + t.size ^ 2
  if t.depth < 100 then protected = protected + 1 else deep = deep + 1 end
end
T.check(protected > 0 and deep > 0, 'depth is changed locally; distant columns remain deep')
T.near(surfaceArea, root.size ^ 2, .01, 'the sea surface is preserved')
local dry = true
for x = -490, 490, 7 do
  for y = -6, 6, 2 do
    for _, t in pairs(tiles) do
      if math.abs(x - t.x) <= t.size / 2 + .5 and math.abs(y - t.y) <= t.size / 2 + .5 then
        dry = dry and t.z - t.depth >= -40 + 6.8 + G.CLEARANCE - 1e-8
      end
    end
  end
end
T.check(dry, 'every overlapping water column stops above the full bore width and roof, including tile overlaps')
local budget = G.MAX_BLOCKS
G.MAX_BLOCKS = 1
local coarse = G.blocks({ bay = root }, segs, 100)
T.check(count(coarse) == 1 and coarse.bay.depth <= 32.2, 'budget exhaustion keeps a safe capped parent')
G.MAX_BLOCKS = budget
T.check(G.cap(root, G.segments({ b = { r = 5, h = 6.8,
  nodes = { { -5, 0, -150 }, { 5, 0, -150 } } } }), 100) == 100, 'a bore below the water volume changes nothing')
T.check(G.cap(root, G.segments({ b = { r = 5, h = 6.8,
  nodes = { { -5, 0, 10 }, { 5, 0, 10 } } } }), 100) == 0, 'solid land above the water plane needs no water volume')
local tilted = { x = 100, y = 30, z = 4, size = 256, nx = .02, ny = -.01 }
local depth = G.cap(tilted, segs, 100)
T.check(depth > 0 and depth < 100, 'tilted volumes use their own surface normal')
local river = { kind = 'river', flow = 2, nodes = { { 0, -200, 0, 20, 80 }, { 0, 200, 0, 20, 80 } } }
local rr = G.rivers({ r = river }, segs)
local crossingDry, intactEnds = true, false
for _, r in pairs(rr) do
  T.check(#r.nodes == 2 and r.nodes[1][5] == r.nodes[2][5], 'affected river spans cannot overshoot depth between spline controls')
  for _, n in ipairs(r.nodes) do
    if math.abs(n[2]) <= 20 then crossingDry = crossingDry and n[5] <= 32.2 + 1e-8 end
    if math.abs(n[2]) > 150 then intactEnds = intactEnds or n[5] == 80 end
  end
  T.check(r.flow == 2, 'river flow survives finite depth clipping')
end
T.check(crossingDry and intactEnds, 'a river keeps its deep ends and has no interpolated fluid through the bore')

local sea = require('beamngEarth/sea')
local anchor = { geodeticToWorld = function(_, a, b, h) return a, b, h end }
local bore = { { id = 'bay-bore', r = 7, h = 6.8, nodes = records.bore.nodes } }
local player = { x = 0, y = 0, z = -39, vx = 0, vy = 0, vz = 0 }
local car = { nudges = 0 }
function car:getPosition() return vec3(0, 0, -39) end
function car:getRotation() return { x = 0, y = 0, z = 0, w = 1 } end
function car:setPositionRotation() self.nudges = self.nudges + 1 end
getPlayerVehicle = function() return car end
quat = function(q) return q end
local function water()
  local out = {}; for _, o in pairs(S.objects) do if o.class == 'WaterBlock' then out[#out + 1] = o end end
  return out
end
local function checkDry(msg)
  local ok = true
  for _, o in ipairs(water()) do
    if math.abs(o.pos.x - player.x) <= o.scale.x / 2 and math.abs(o.pos.y - player.y) <= o.scale.y / 2 then
      ok = ok and o.pos.z - o.scale.z / 2 >= -32.2 - 1e-7
    end
  end
  T.check(ok, msg)
end
local function message(aid) return { anchorId = aid, tiles = { { 'bay', 0, 0, 0, 1024, 0, 0 } } } end
sea.onMessage(message(1)); sea.update(1, player)
T.check(#water() == 2, 'baseline includes the visible and player-local deep physics volumes')
local oldWater = water()
sea.protect('first-instance', bore, anchor)
local removed = true; for _, o in ipairs(oldWater) do removed = removed and not S.objects[o.id] end
T.check(removed, 'old deep fluid volumes are deleted synchronously before any tunnel collision creation')
for _ = 1, 70 do sea.update(1, player) end
checkDry('visible and invisible player-local water blocks both leave the tunnel dry')
T.check(car.nudges > 0, 'stationary vehicle fluid membership is refreshed after the volumes change')
player.z = -.2
sea.update(1, player)
local floats = false
for _, o in ipairs(water()) do
  if math.abs(o.pos.x) <= o.scale.x / 2 and math.abs(o.pos.y) <= o.scale.y / 2 then
    floats = floats or (o.pos.z >= player.z and o.pos.z - o.scale.z / 2 < player.z)
  end
end
T.check(floats, 'surface water physics still supports a boat over the tunnel')
sea.protect('second-instance', bore, anchor)
T.check(sea.status().guards == 1 and sea.status().guardOwners == 2, 'neighbouring road tiles deduplicate a whole bore')
sea.retain('first-instance') -- a live mesh
sea.release('first-instance'); sea.release('second-instance')
T.check(sea.status().guards == 1, 'dropping tile owners leaves protection while a mesh is alive')
sea.clear(); sea.onMessage(message(1))
for _ = 1, 70 do sea.update(1, player) end
checkDry('a reconnect cannot replace retained road geometry with deep water')
sea.transform({ apply = function(_, x, y, z) return x - 100, y + 100, z end }, 2)
player.x, player.y = -100, 100
sea.update(2, player)
T.check(sea.status().guards == 1, 'a rebase retains the bore while moving its exclusion with the water')
checkDry('rebased water stays above the rebased bore')
sea.release('first-instance')
T.check(sea.status().guards == 0 and sea.status().guardOwners == 0, 'last actual mesh release restores the original columns')
sea.clear(); sea.reset()

-- Road metadata can precede water, and a rebase can precede any water-object creation.
player.x, player.y = 0, 0
sea.protect('early', bore, anchor)
sea.onMessage(message(10))
sea.transform({ apply = function(_, x, y, z) return x - 100, y + 100, z end }, 11)
player.x, player.y = -100, 100
for _ = 1, 70 do sea.update(11, player) end
T.check(#water() > 0, 'a rebase maps water wanted before its first creation')
checkDry('road metadata received first protects late water in the new anchor')
sea.release('early'); sea.clear(); sea.reset()
player.x, player.y = 0, 0
sea.protect('land', { { id = 'land', r = 7, h = 6.8, nodes = { { -500, 0, 10 }, { 500, 0, 10 } } } }, anchor)
sea.onMessage(message(12))
for _ = 1, 70 do sea.update(12, player) end
local noLandWater = true
for _, o in ipairs(water()) do
  if math.abs(o.pos.x) <= o.scale.x / 2 and math.abs(o.pos.y) <= o.scale.y / 2 then noLandWater = false end
end
T.check(noLandWater, 'an exposed landward roof has neither visible fluid nor an invisible physics block')
sea.clear(); sea.reset()

-- Actual roads consumer: protect on receipt, retain mesh references, wait for deletion AND collision rebuild.
local roads = require('beamngEarth/roads')
readFile = function() return 'bore' end
jsonDecode = function() return { chunks = {}, waterguard = bore,
  deck = { shape = '/deck.cdae', pos = { 0, 0, 0 } }, tunnel = { shape = '/tunnel.cdae', pos = { 0, 0, 0 } } } end
local gov = { try = function(self, kind) return kind ~= 'roadDelete' or self.delete end,
  charge = function() end, chargeMotion = function() end }
sea.onMessage(message(1)); sea.update(1, player)
roads.onTile({ anchorId = 1, key = '0_0', path = '/bore.json' }, 1, anchor)
T.check(sea.status().guards == 1, 'roads register the exclusion before the queued collider is created')
roads.update(gov, 1, player, nil, anchor)
roads.onDrop({ anchorId = 1, key = '0_0' })
T.check(sea.status().guards == 1, 'time-sliced mesh deletion retains water protection')
gov.delete = true
roads.update(gov, 1, player, nil, anchor)
T.check(sea.status().guards == 1, 'object deletion alone does not release cached collision protection')
roads.collisionRebuilt()
roads.update(gov, 1, player, nil, anchor); roads.update(gov, 1, player, nil, anchor)
T.check(sea.status().guards == 0, 'protection ends after the rebuild has passed a subsequent update')

-- Production geotiles carry global metadata even though their shapes use a tile frame and a pivot.
roads.clear(); sea.clear(); sea.reset(); roads.setGeo(true)
local geodesy, geotile = require('beamngEarth/geodesy'), require('beamngEarth/geotile')
local a = geodesy.newAnchor(59.038578, 5.932688)
local gx, gy = geotile.tileOf(a.lat, a.lon)
local key = geotile.key(gx, gy)
local data = { frame = { a.lat, a.lon }, chunks = {},
  waterguard = { { id = 'geo-bore', r = 7, h = 6.8,
    nodes = { { a.lat, a.lon - .001, -40 }, { a.lat, a.lon + .001, -40 } } } },
  deck = { shape = '/geo-deck.cdae', pos = { 0, 0, 0 }, pivot = -40 },
  tunnel = { shape = '/geo-tunnel.cdae', pos = { 0, 0, 0 }, pivot = -40 } }
local msg = { key = key, h = 'same-bore', data = data }
T.check(roads.onGeoTile(msg, 1, a) == true and sea.status().guards == 1,
  'geotile receipt protects the bore before placing its queued shapes')
roads.update(gov, 1, player, nil, a)
T.check(roads.onGeoTile(msg, 1, a) == true and sea.status().guardOwners == 1,
  'same-content geotile replay keeps the original owner without leaking another reference')
roads.onDrop({ anchorId = 1, key = key })
roads.update(gov, 1, player, nil, a)
T.check(sea.status().guards == 1, 'geotile shape deletion also waits for the collision rebuild')
roads.collisionRebuilt()
roads.update(gov, 1, player, nil, a); roads.update(gov, 1, player, nil, a)
T.check(sea.status().guards == 0, 'the last geotile collision reference is released')
T.done('test_subsea_water')
