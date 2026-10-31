-- luajit mod/tests/test_sea.lua  (sea.lua: the sea's WaterBlock tiles from the server's 'sea' messages)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

local realCreate = createObject
createObject = function(class)
  local o = realCreate(class)
  function o:getId() return self.id end
  function o:setScale(s) self.scale = vec3(s.x, s.y, s.z) end
  return o
end
local car = { pos = vec3(100, 100, -1), nudged = 0 }
function car:getPosition() return vec3(self.pos.x, self.pos.y, self.pos.z) end
function car:getRotation() return { x = 0, y = 0, z = 0, w = 1 } end
function car:setPositionRotation() self.nudged = self.nudged + 1 end
getPlayerVehicle = function() return car end
quat = quat or function(q) return q end
local sea = require('beamngEarth/sea')

local function blocks()     -- the drawn tiles (not the physics block)
  local r = {}
  for _, o in pairs(S.objects) do if o.class == 'WaterBlock' and o.name ~= sea.PHYS_NAME then r[o.name] = o end end
  return r
end
local function physBlock()
  for _, o in pairs(S.objects) do if o.name == sea.PHYS_NAME then return o end end
end
local function count(t) local n = 0; for _ in pairs(t) do n = n + 1 end; return n end
local function tile(key, x, y, z, size, nx, ny) return { key, x, y, z, size, nx or 0, ny or 0 } end
local player = { x = 100, y = 100, z = -1, vx = 0, vy = 0, vz = 0 }

sea.update(1, player)
T.check(count(blocks()) == 0, 'nothing before the server says so')

local six = {}
for i = 0, 5 do six[#six + 1] = tile('512_' .. i .. '_0', 256 + 512 * i, 256, -0.01 * i, 512, (256 + 512 * i) / 6371000, 0) end
sea.onMessage({ type = 'sea', anchorId = 1, tiles = six })
sea.update(1, player)
T.check(count(blocks()) == sea.CREATE_PER_FRAME, 'a few tiles per frame')
sea.update(1, player)
local b = blocks()
T.check(count(b) == 6 and sea.status().tiles == 6, 'all made over the next frames')
local t0 = b[sea.PREFIX .. '512_0_0']
T.check(t0 and t0.pos.x == 256 and t0.pos.z == 0 and t0.scale.x == 512 + sea.TILE_OVERLAP_M and t0.scale.z == sea.DEPTH_M,
  'surface at the position, the tile\'s size, DEPTH_M deep')
T.check(t0.canSave == false and t0.fields.baseColor ~= nil and t0.fields.rotation ~= nil, 'not saved, styled, tilted')
sea.onMessage({ type = 'sea', anchorId = 1, tiles = { six[1], tile('L512_0_0_895', 256, 256, 89.5, 512) } })
sea.update(1, player)
local lake = blocks()[sea.PREFIX .. 'L512_0_0_895']
T.check(lake and lake.fields.baseColor ~= t0.fields.baseColor, 'lakes and rivers have their own colour')
sea.onMessage({ type = 'sea', anchorId = 1, tiles = six })
sea.update(1, player); sea.update(1, player)
T.check(car.nudged >= 1, 'a parked car inside a new tile is re-placed (its physics sees the water)')
local pb = physBlock()
T.check(pb and pb.renderEnabled == false and pb.pos.x == 100 and math.abs(pb.pos.z) < 0.01 and pb.scale.x == sea.PHYS_SIZE,
  'a physics block around the player at the level of its tile, not drawn')
T.check(pb.fields.rippleTex and pb.fields.foamTex and pb.fields.depthGradientTex,
  'the physics block has its textures (without, every move logged "Texture missing")')
local made0 = sea.physMade
sea.update(1, player)
T.check(sea.physMade == made0 + 1, 'made again once the batch of new tiles is done')
for _, o in pairs(blocks()) do
  T.check(physBlock().id > o.id, 'newer than every tile (a vehicle takes the newest water object)')
end
sea.update(1, player)
T.check(sea.physMade == made0 + 1, 'not made again while nothing changes')
made0 = sea.physMade
player.x = 100 + sea.PHYS_SIZE / 2 - sea.PHYS_MARGIN + 1
sea.update(1, player)
T.check(sea.physMade == made0 + 1 and physBlock().pos.x == player.x, 'made again around the player near its edge')
player.x = 100

local c0 = sea.created
sea.onMessage({ type = 'sea', anchorId = 1, tiles = { six[1], six[2], tile('1024_1_0', 1536, 512, -0.2, 1024) } })
sea.update(1, player)
b = blocks()
T.check(count(b) == 3 and b[sea.PREFIX .. '1024_1_0'] and not b[sea.PREFIX .. '512_5_0'], 'a new set: changed tiles only')
T.check(sea.created == c0 + 1, 'the kept ones not made again')

player.vx = 20
local nudged = car.nudged
sea.onMessage({ type = 'sea', anchorId = 1, tiles = { tile('512_0_0b', 100, 100, 0, 512) } })
sea.update(1, player)
T.check(car.nudged == nudged, 'a moving car is not re-placed (it sees new water by moving)')
player.vx = 0
sea.onMessage({ type = 'sea', anchorId = 1, tiles = { tile('512_9_9', 5000, 5000, 0, 512) } })
sea.update(1, player)
T.check(physBlock() == nil, 'no tile under the player: no physics block')

sea.update(2, player)
T.check(count(blocks()) == 0, 'another anchor: removed until the server says')
sea.onMessage({ type = 'sea', anchorId = 2, tiles = six })
sea.update(2, player); sea.update(2, player)
T.check(count(blocks()) == 6, 'the new anchor\'s tiles')
sea.onMessage({ type = 'sea', anchorId = 2, tiles = {} })
sea.update(2, player)
T.check(count(blocks()) == 0, 'no sea near: removed')

sea.onMessage({ type = 'sea', anchorId = 2, tiles = six })
sea.update(2, player)
sea.clear()
T.check(count(blocks()) == 0 and sea.status().want == 0, 'clear: removed and forgotten')

-- rivers: River objects along the server's centrelines, made from a definition, changed keys only
local defs = {}
jsonEncode = function(t) return t end            -- the test's Sim reads the table itself
Sim = { deserializeObjectFromText = function(def)
  local o = S.newObject('River')
  function o:getId() return self.id end
  function o:reloadTextures() self.reloaded = true end
  function o:regenerate() self.regenerated = true end
  o.def = def
  o:registerObject(def.name)
  defs[#defs + 1] = def
  return o.id
end }
local function rivers()
  local r = {}
  for _, o in pairs(S.objects) do if o.class == 'River' then r[o.name] = o end end
  return r
end
local stream = { 'Rabc_0_0_0', 'stream', 0.6, { { 0, 0, 10, 4, 1 }, { 10, 0, 9.8, 4, 1 }, { 20, 0, 9.5, 4, 1 } } }
sea.onMessage({ type = 'sea', anchorId = 3, tiles = {}, rivers = { stream } })
sea.update(3, player)
local rv = rivers()[sea.RIVER_PREFIX .. 'Rabc_0_0_0']
T.check(rv ~= nil and #rv.def.nodes == 3 and rv.def.nodes[2][3] == 9.8 and rv.def.nodes[2][4] == 4,
  'a River along the nodes: their heights and widths')
T.check(rv.def.nodes[1][8] == 1 and rv.fields.flowMagnitudePhysics == '0.6' and rv.canSave == false,
  'upright, flowing at the server\'s speed, not saved')
T.check(rv.reloaded and rv.regenerated, 'textures and mesh built again after styling')
T.check(rv.fields.SubdivideLength == '6.0', 'a narrow river: the near mesh at the finest allowed')
T.check(sea.status().rivers == 1, 'counted')
sea.update(3, player)
T.check(sea.riversMade == 1, 'made once')
sea.onMessage({ type = 'sea', anchorId = 3, tiles = {}, rivers = {} })
sea.update(3, player)
T.check(next(rivers()) == nil and sea.status().rivers == 0, 'gone from the set: removed')
sea.onMessage({ type = 'sea', anchorId = 3, tiles = {}, rivers = { stream } })
sea.update(3, player)
sea.clear()
T.check(next(rivers()) == nil, 'clear: removed')

-- origin rebase: the water moves into the new frame at once and stays under the new anchor (no gap until the server's
-- next 'sea' message)
local shift = { apply = function(_, x, y, z) return x - 1000, y + 500, z end }      -- a pure translation
local p4 = { x = 1100, y = -400, z = -1, vx = 0, vy = 0, vz = 0 }
sea.onMessage({ type = 'sea', anchorId = 4, tiles = { tile('a', 1024, -512, 0, 512) } })
sea.update(4, p4); sea.update(4, p4)
local made4 = blocks()[sea.PREFIX .. 'a']
T.check(made4 and made4.pos.x == 1024 and physBlock() ~= nil, 'a tile and the physics block under the player')
local created4 = sea.created
sea.transform(shift, 5)
T.check(made4.pos.x == 24 and made4.pos.y == -12, 'the made tile moved by the rebase')
T.check(physBlock().pos.x == 100 and physBlock().pos.y == 100, 'the physics block too')
p4 = { x = 100, y = 100, z = -1, vx = 0, vy = 0, vz = 0 }
sea.update(5, p4)
T.check(blocks()[sea.PREFIX .. 'a'] == made4 and sea.created == created4, 'kept under the new anchor: nothing removed or made')
T.check(physBlock() ~= nil, 'still water under the player')
sea.onMessage({ type = 'sea', anchorId = 5, tiles = { tile('b', 0, 0, 0, 512) } })
sea.update(5, p4); sea.update(5, p4)
T.check(blocks()[sea.PREFIX .. 'a'] == nil and blocks()[sea.PREFIX .. 'b'] ~= nil, 'the server\'s new set replaces it')

T.done('test_sea')
