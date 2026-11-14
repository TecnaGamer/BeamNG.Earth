-- luajit mod/tests/test_roads.lua  (roads.lua: road tiles from the server become DecalRoads, one chunk per
-- governor unit; drops, anchor changes, clear and the off switch remove them)
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')

-- DecalRoad methods roads.lua calls (not in the generic stub object)
local realCreate = createObject
createObject = function(class)
  local o = realCreate(class)
  o.nodes = {}
  function o:insertNodeNoRegen(p, w) self.nodes[#self.nodes + 1] = { p.x, p.y, p.z, w } end
  function o:regenerate() self.regenerated = (self.regenerated or 0) + 1 end
  function o:getId() return self.id end
  return o
end
local files = {}
readFile = function(p) return files[p] end
jsonDecode = function(s) return assert(load('return ' .. s))() end   -- the test writes Lua table text

-- Each update represents a frame. Do not depend on host clock resolution: on Windows several
-- updates can occur in the same os.clock tick, leaving the zero-delay rebuild tests pending.
local elapsedMs = 0
local stopwatch = require('beamngEarth/stopwatch')
stopwatch.clock = function()
  local start = elapsedMs
  return function() return elapsedMs - start end
end
stopwatch.new = stopwatch.clock
local turbineRows, turbineDrops = {}, {}
package.loaded['beamngEarth/windturbines'] = {
  onRows = function(key, rows) turbineRows[#turbineRows + 1] = { key, rows } end,
  dropKey = function(key) turbineDrops[#turbineDrops + 1] = key end,
}
local roads = require('beamngEarth/roads')
T.check(roads.SURFACE_FIRST == true, 'drawn road surfaces ranked with the colliders by default (2026-11-13)')
roads.SURFACE_FIRST = false     -- (the tests below: the base order; SURFACE_FIRST's own tests turn it on)
local update = roads.update
roads.update = function(...)
  elapsedMs = elapsedMs + 16
  return update(...)
end

local gov = { tries = 0 }
function gov:try() self.tries = self.tries + 1; return self.allow == nil or self.tries <= self.allow end
function gov:charge() end
function gov:chargeMotion() end

local function chunk(x)
  return string.format('{ m = "road_asphalt_2lane", w = 6, tl = 11, p = 20, n = { {%d, 0, 10}, {%d, 10, 10}, {%d, 20, 10} } }', x, x, x)
end
files['/t/a.json'] = '{ chunks = { ' .. chunk(0) .. ', ' .. chunk(5) .. [[ },
  turbines = { { id = "osm:node:1175681635", x = 1, y = 2, z = 1800, yaw = 30, scale = 1 } } }]]
files['/t/b.json'] = '{ chunks = { ' .. chunk(50) .. ' } }'

local function decalRoads()
  local n = 0
  for _, o in pairs(S.objects) do if o.class == 'DecalRoad' then n = n + 1 end end
  return n
end

-- fields of a chunk
local f = {}
for _, kv in ipairs(roads.fieldsFor({ m = 'road_asphalt_2lane', tl = 11, p = 20 })) do f[kv[1]] = kv[2] end
T.check(f.material == 'road_asphalt_2lane' and f.textureLength == '11' and f.renderPriority == '20', 'material, texture length, priority')
T.check(f.drivability == '-1' and f.overObjects == 'false', 'not in the navgraph; drawn on the terrain')

-- a tile: queued, then made one chunk per governor unit
T.check(roads.onTile({ anchorId = 1, key = '0_0', path = '/t/a.json', n = 2 }, 1) == true, 'tile accepted')
T.check(roads.status().queued == 2 and decalRoads() == 0, 'queued, nothing made yet')
T.check(roads.queued() == roads.status().queued + roads.status().walksQueued, 'roads.queued(): the status queues without its walk')
T.check(turbineRows[1][1] == '1:0_0' and turbineRows[1][2][1].id == 'osm:node:1175681635'
  and turbineRows[1][2][1].z == 1800, 'legacy road tile passes turbine identity and world base')
gov.allow, gov.tries = 1, 0
roads.update(gov, 1)
T.check(decalRoads() == 1 and roads.status().queued == 1, 'one chunk per allowed unit')
T.check(roads.queued() == roads.status().queued + roads.status().walksQueued, 'roads.queued() follows the queue')
gov.allow, gov.tries = nil, 0
roads.update(gov, 1)
T.check(decalRoads() == 2 and roads.status().roads == 2 and not roads.hasWork(), 'all made')
local some
for _, o in pairs(S.objects) do if o.class == 'DecalRoad' then some = o end end
T.check(#some.nodes == 3 and some.nodes[1][4] == 6 and some.regenerated == 1 and some.canSave == false, 'nodes with width, one regenerate, not saved')

-- a stale tile (another anchor) is ignored
T.check(roads.onTile({ anchorId = 9, key = '0_0', path = '/t/b.json', n = 1 }, 1) == false, 'stale anchor ignored')

-- drop
roads.onTile({ anchorId = 1, key = '1_0', path = '/t/b.json', n = 1 }, 1)
T.check(#turbineRows[#turbineRows][2] == 0, 'legacy road tile without turbines reconciles an empty list')
roads.update(gov, 1)
T.check(decalRoads() == 3, 'second tile made')
roads.onDrop({ anchorId = 1, key = '0_0' })
roads.update(gov, 1)
T.check(decalRoads() == 1 and roads.status().tiles == 1, 'dropped tile deleted')
T.check(turbineDrops[1] == '1:0_0', 'legacy tile drop releases its turbine owner')

-- a re-sent tile replaces its old roads
roads.onTile({ anchorId = 1, key = '1_0', path = '/t/b.json', n = 1 }, 1)
roads.update(gov, 1)
T.check(decalRoads() == 1 and roads.status().roads == 1, 're-sent tile replaces, no duplicates')

-- a new anchor: the old anchor's roads go
roads.update(gov, 2)
T.check(decalRoads() == 0 and roads.status().tiles == 0, 'new anchor: old roads deleted')

-- unreadable file
T.check(roads.onTile({ anchorId = 2, key = '0_0', path = '/t/missing.json', n = 1 }, 2) == false and roads.status().error ~= nil,
  'unreadable tile reported')

-- off: everything removed at once, new tiles ignored
roads.onTile({ anchorId = 2, key = '0_0', path = '/t/a.json', n = 2 }, 2)
roads.update(gov, 2)
T.check(decalRoads() == 2, 'made again')
roads.setEnabled(false)
T.check(decalRoads() == 0 and roads.status().roads == 0, 'off: removed at once')
T.check(roads.onTile({ anchorId = 2, key = '1_1', path = '/t/a.json', n = 2 }, 2) == false, 'off: tiles ignored')
T.check(roads.setEnabled(true) == true, 'on again: reported (core then asks the server for the tiles)')
T.check(roads.setEnabled(true) == false, 'already on: nothing to ask')

-- raised sidewalks: the tile's sidewalk mesh is one TSStatic that collides; it goes with its tile
files['/t/w.json'] = '{ chunks = { ' .. chunk(0) .. ' }, walk = { shape = "/t/walks_2_5_5.cdae", pos = { 256, 512, 0 } } }'
local function statics()
  local r = {}
  for _, o in pairs(S.objects) do if o.class == 'TSStatic' then r[#r + 1] = o end end
  return r
end
S.calls.reloadCollision = 0
roads.RELOAD_EVERY_S = 0
roads.onTile({ anchorId = 2, key = '5_5', path = '/t/w.json', n = 1 }, 2)
roads.update(gov, 2)
local w = statics()[1]
T.check(#statics() == 1 and roads.status().walks == 1, 'sidewalk mesh made')
T.check(w.fields.shapeName == '/t/walks_2_5_5.cdae' and w.fields.collisionType == 'Visible Mesh Final' and w.canSave == false,
  'shape, collides with its visible mesh, not saved')
T.check(w.pos.x == 256 and w.pos.y == 512, 'at the tile corner')
T.check(S.calls.reloadCollision == 1, 'collision reloaded for it')
roads.update(gov, 2)
T.check(S.calls.reloadCollision == 1, 'nothing more while nothing changes')
-- a terrain .ter swap: no rebuild for SWAP_QUIET_FRAMES frames (its collision follows by itself; one in between took
-- the block before its heights were in, 2026-10-19)
do
  local quietMs = roads.SWAP_QUIET_MS
  roads.SWAP_QUIET_MS = 0
  roads.requestCollision()
  roads.terrainSwapped()
  for i = 1, roads.SWAP_QUIET_FRAMES - 1 do roads.update(gov, 2) end
  T.check(S.calls.reloadCollision == 1 and roads.swapQuiet(), 'no rebuild in the frames after a terrain swap')
  roads.update(gov, 2)
  T.check(S.calls.reloadCollision == 2 and not roads.swapQuiet(), 'then the pending rebuild runs')
  roads.SWAP_QUIET_MS = quietMs
  S.calls.reloadCollision = 1
end
roads.onDrop({ anchorId = 2, key = '5_5' })
roads.update(gov, 2)
T.check(#statics() == 0 and decalRoads() == 0 and S.calls.reloadCollision == 2, 'dropped tile: mesh deleted, collision reloaded')

-- with a lean collision mesh (walkcol) the drawn sidewalks only render; both go with the tile
files['/t/wc.json'] = '{ chunks = { }, walk = { shape = "/t/walks_2_6_6.cdae", pos = { 0, 0, 0 } }, ' ..
  'walkcol = { shape = "/t/walkcol_2_6_6.cdae", pos = { 0, 0, 0 } } }'
roads.onTile({ anchorId = 2, key = '6_6', path = '/t/wc.json', n = 0 }, 2)
roads.update(gov, 2)
local kinds = {}
for _, o in ipairs(statics()) do kinds[o.fields.shapeName] = o.fields.collisionType end
T.check(kinds['/t/walks_2_6_6.cdae'] == 'None' and kinds['/t/walkcol_2_6_6.cdae'] == 'Collision Mesh',
  'walkcol collides, the drawn sidewalks only render')
roads.onDrop({ anchorId = 2, key = '6_6' })
roads.update(gov, 2)
T.check(#statics() == 0, 'walkcol dropped with its tile')
-- the same for a railway track's lean collider (railcol, PR #41); without one the drawn track collides as before
files['/t/rc.json'] = '{ chunks = { }, rail = { shape = "/t/rail_2_6_6.cdae", pos = { 0, 0, 0 } }, ' ..
  'railcol = { shape = "/t/railcol_2_6_6.cdae", pos = { 0, 0, 0 } } }'
roads.onTile({ anchorId = 2, key = '6_6', path = '/t/rc.json', n = 0 }, 2)
roads.update(gov, 2)
kinds = {}
for _, o in ipairs(statics()) do kinds[o.fields.shapeName] = o.fields.collisionType end
T.check(kinds['/t/rail_2_6_6.cdae'] == 'None' and kinds['/t/railcol_2_6_6.cdae'] == 'Collision Mesh',
  'railcol collides, the drawn track only renders')
roads.onDrop({ anchorId = 2, key = '6_6' })
roads.update(gov, 2)
T.check(#statics() == 0, 'railcol dropped with its tile')
files['/t/r.json'] = '{ chunks = { }, rail = { shape = "/t/rail_2_6_6.cdae", pos = { 0, 0, 0 } } }'
roads.onTile({ anchorId = 2, key = '6_6', path = '/t/r.json', n = 0 }, 2)
roads.update(gov, 2)
T.check(#statics() == 1 and statics()[1].fields.collisionType == 'Visible Mesh Final', 'no railcol: the track collides')
roads.onDrop({ anchorId = 2, key = '6_6' })
roads.update(gov, 2)

-- pooled shape files (server/matslots.py): the slot paths replace the tile's own files; a slot is reported gone once
-- its tile is dropped and its object deleted (one the tile cannot use: at once)
gov.allow, gov.tries = nil, 0
roads.setSession('sA')
roads.takeSlotsGone('sA')
roads.onTile({ anchorId = 2, key = '6_7', path = '/t/wc.json', n = 0, slots = {
  walk = { '/temp/beamngEarth/sA/slot_00012.cdae', 40 }, walkcol = { '/temp/beamngEarth/sA/slot_00013.cdae', 41 },
  deck = { '/temp/beamngEarth/sA/slot_00014.cdae', 42 } } }, 2)
roads.update(gov, 2)
local shapes = {}
for _, o in ipairs(statics()) do shapes[o.fields.shapeName] = true end
T.check(shapes['/temp/beamngEarth/sA/slot_00012.cdae'] and shapes['/temp/beamngEarth/sA/slot_00013.cdae']
  and not shapes['/t/walks_2_6_6.cdae'], 'the slot paths are loaded instead of the tile files')
local gone = roads.takeSlotsGone('sA')
T.check(#gone == 1 and gone[1][1] == 14 and gone[1][2] == 42, 'a slot the tile has no mesh for: reported at once')
local held = roads.slotsHeld('sA')
T.check(#held == 2, 'held: the two loaded slots')
roads.onDrop({ anchorId = 2, key = '6_7' })
T.check(#roads.takeSlotsGone('sA') == 0, 'dropped but its objects not deleted yet: still held')
roads.update(gov, 2)
gone = roads.takeSlotsGone('sA')
local got = {}
for _, g in ipairs(gone) do got[g[1]] = g[2] end
T.check(#gone == 2 and got[12] == 40 and got[13] == 41, 'objects deleted: both slots reported with their serials')
T.check(#roads.takeSlotsGone('other') == 0, 'reports are per server session')

-- (from PR #64, KyzoFox 1.5.4) a tile sent again under the same key before its meshes were made: the old tile's queued meshes
-- (their slot paths already reported gone, so the server may have given them to another tile) are not made into the
-- new tile (houses floating near Cleburne, AL: another tile's buildings at this tile's place, 2026-10-19)
for _, o in ipairs(statics()) do o:delete() end
roads.takeSlotsGone('sA')
gov.allow, gov.tries = 0, 0                 -- no mesh made this frame
roads.onTile({ anchorId = 2, key = '6_8', path = '/t/wc.json', n = 0, slots = {
  walk = { '/temp/beamngEarth/sA/slot_00020.cdae', 50 }, walkcol = { '/temp/beamngEarth/sA/slot_00021.cdae', 51 } } }, 2)
roads.update(gov, 2)
T.check(#statics() == 0, 'resend: nothing made yet')
roads.onTile({ anchorId = 2, key = '6_8', path = '/t/wc.json', n = 0, slots = {
  walk = { '/temp/beamngEarth/sA/slot_00030.cdae', 60 }, walkcol = { '/temp/beamngEarth/sA/slot_00031.cdae', 61 } } }, 2)
gone = roads.takeSlotsGone('sA')
T.check(#gone == 2, 'resend: the first copy\'s slots reported gone (never loaded)')
gov.allow, gov.tries = nil, 0
roads.update(gov, 2); roads.update(gov, 2)
shapes = {}
for _, o in ipairs(statics()) do shapes[o.fields.shapeName] = (shapes[o.fields.shapeName] or 0) + 1 end
T.check(not shapes['/temp/beamngEarth/sA/slot_00020.cdae'] and not shapes['/temp/beamngEarth/sA/slot_00021.cdae'],
  'resend: the reported slots are never loaded')
T.check(shapes['/temp/beamngEarth/sA/slot_00030.cdae'] == 1 and shapes['/temp/beamngEarth/sA/slot_00031.cdae'] == 1
  and #statics() == 2, 'resend: the new copy\'s meshes made once each')
roads.onDrop({ anchorId = 2, key = '6_8' })
roads.update(gov, 2)
T.check(#statics() == 0, 'resend: every mesh goes with the tile (none orphaned)')
roads.takeSlotsGone('sA')

-- a sidewalk mesh never turns solid around the player's car: held while the car is in its boxes
files['/t/h.json'] = '{ chunks = { }, walk = { shape = "/t/walks_2_7_7.cdae", pos = { 0, 0, 0 } }, ' ..
  'colliders = { walk = { { 100, 100, 0, 120, 110, 1 } } } }'
local car = { pos = vec3(110, 105, 0.5) }
function car:getPosition() return self.pos end
getPlayerVehicle = function() return car end
roads.onTile({ anchorId = 2, key = '0_0', path = '/t/h.json', n = 0 }, 2)
roads.update(gov, 2)
T.check(#statics() == 0, 'not made with the car on it')
car.pos = vec3(120.5, 105, 0.5)        -- just past its box (the box's own margin covers the curb)
roads.update(gov, 2)
T.check(#statics() == 1, 'made with the car beside it (only the car over a sidewalk holds it)')
-- made, then the car drives into it before collision is rebuilt: removed, made again once clear
roads.onDrop({ anchorId = 2, key = '0_0' })
roads.update(gov, 2)
roads.RELOAD_EVERY_S = 1e9
roads.onTile({ anchorId = 2, key = '0_0', path = '/t/h.json', n = 0 }, 2)
roads.update(gov, 2)
T.check(#statics() == 1, 'made (car clear)')
car.pos = vec3(110, 105, 0.5)
roads.collisionRebuilt()
T.check(#statics() == 0 and roads.solidAround == 1, 'removed before it turns solid around the car')
car.pos = vec3(200, 105, 0.5)
roads.update(gov, 2)
T.check(#statics() == 1, 'made again once clear')
roads.RELOAD_EVERY_S = 0
roads.onDrop({ anchorId = 2, key = '0_0' })
roads.update(gov, 2)
-- a deck is held only with the car under its surface: a car on the approach at the deck's height (in its box) gets
-- it at once (held, a late deck stayed out until the car was past and the car fell through it, 2026-10-20)
files['/t/d.json'] = '{ chunks = { }, deck = { shape = "/t/deck_2_9_9.cdae", pos = { 0, 0, 0 } }, ' ..
  'colliders = { deck = { { 100, 100, 7.8, 120, 110, 12 } } } }'      -- (surface 10 m: 10 - 2.2 .. 10 + 2)
car.pos = vec3(110, 105, 10.5)
roads.onTile({ anchorId = 2, key = '0_0', path = '/t/d.json', n = 0 }, 2)
roads.update(gov, 2)
T.check(#statics() == 1, 'a deck made with the car driving onto it')
roads.onDrop({ anchorId = 2, key = '0_0' })
roads.update(gov, 2)
car.pos = vec3(110, 105, 9.0)          -- in its slab
roads.onTile({ anchorId = 2, key = '0_0', path = '/t/d.json', n = 0 }, 2)
roads.update(gov, 2)
T.check(#statics() == 0, 'held with the car in its slab')
car.pos = vec3(200, 105, 0.5)
roads.update(gov, 2)
T.check(#statics() == 1, 'made once the car is clear')
roads.onDrop({ anchorId = 2, key = '0_0' })
roads.update(gov, 2)
-- with a lean collision mesh the drawn sidewalk is render-only: made with the car in its box (it cannot catch the car;
-- held, a roundabout's island band was never drawn, 2026-10-17); the collision mesh still waits
files['/t/hl.json'] = '{ chunks = { }, walk = { shape = "/t/walks_2_8_8.cdae", pos = { 0, 0, 0 } }, ' ..
  'walkcol = { shape = "/t/walkcol_2_8_8.cdae", pos = { 0, 0, 0 } }, ' ..
  'colliders = { walk = { { 100, 100, 0, 120, 110, 1 } }, walkcol = { { 100, 100, 0, 120, 110, 1 } } } }'
car.pos = vec3(110, 105, 0.5)
roads.onTile({ anchorId = 2, key = '0_0', path = '/t/hl.json', n = 0 }, 2)
roads.update(gov, 2)
local shapes = {}
for _, o in ipairs(statics()) do shapes[#shapes + 1] = o.fields.shapeName end
T.check(#statics() == 1 and shapes[1] == '/t/walks_2_8_8.cdae', 'the drawn sidewalk made with the car on it, its collision held')
car.pos = vec3(200, 105, 0.5)
roads.update(gov, 2)
T.check(#statics() == 2, 'the collision mesh once the car is clear')
roads.onDrop({ anchorId = 2, key = '0_0' })
roads.update(gov, 2)
-- A diagonal sidewalk may be inside the tile box but well away from the car.
files['/t/diagonal.json'] = '{ chunks = { }, walkcol = { shape = "/t/diag.cdae", pos = { 0, 0, 0 } }, ' ..
  'colliders = { walkcol = { { 99, 99, 0, 113, 113, 0.15, { 100, 100, 112, 112, 111, 113, 99, 101 } } } } }'
car.pos = vec3(109, 102, 0.5)
roads.onTile({ anchorId = 2, key = '0_0', path = '/t/diagonal.json', n = 0 }, 2)
roads.update(gov, 2)
T.check(#statics() == 1, 'collision made beside a diagonal sidewalk while inside its bounding box')
roads.onDrop({ anchorId = 2, key = '0_0' })
roads.update(gov, 2)
car.pos = vec3(108, 108.5, 0.5)
roads.onTile({ anchorId = 2, key = '0_0', path = '/t/diagonal.json', n = 0 }, 2)
roads.update(gov, 2)
T.check(#statics() == 0, 'a collider still waits if the player is on the actual sidewalk')
roads.onDrop({ anchorId = 2, key = '0_0' })
roads.update(gov, 2)
-- colliders only near the car: a far tile's sidewalk waits, made as the car comes near, dropped far behind
roads.COLLIDE_CHECK_S = 0
roads.RELOAD_EVERY_S = 0
car.pos = vec3(0, 0, 0.5)
files['/t/far.json'] = '{ chunks = { }, walk = { shape = "/t/walks_2_9_0.cdae", pos = { 2304, 0, 0 } } }'
roads.onTile({ anchorId = 2, key = '9_0', path = '/t/far.json', n = 0 }, 2)
roads.update(gov, 2)
T.check(#statics() == 0, 'a tile 2.4 km away: no collider yet')
car.pos = vec3(2200, 100, 0.5)
roads.update(gov, 2)
roads.update(gov, 2)
T.check(#statics() == 1, 'made once the car is near')
car.pos = vec3(0, 0, 0.5)
roads.update(gov, 2)
roads.update(gov, 2)
T.check(#statics() == 0, 'dropped again far behind')
T.check(roads.collideRadius(0) == roads.COLLIDE_M[1] and roads.collideRadius(45) == 45 * roads.COLLIDE_LEAD_S
  and roads.collideRadius(500) == roads.COLLIDE_M[2], 'the radius grows with speed, within its limits')
roads.onDrop({ anchorId = 2, key = '9_0' })
roads.update(gov, 2)
-- a fast car: the road pieces along its path first (a tile 512 m ahead before one 512 m beside it), the ones behind
-- it last; standing, by distance (the user, 2026-10-19: at 100+ mph the road texture went first)
do
  files['/t/e.json'] = '{ chunks = { } }'
  for _, k in ipairs({ '2_0', '0_1', '0_2', '-2_0', '0_0' }) do roads.onTile({ anchorId = 2, key = k, path = '/t/e.json', n = 0 }, 2) end
  car.pos = vec3(128, 128, 0.5)
  function car:getVelocity() return vec3(45, 0, 0) end
  roads.update(gov, 2)              -- (the motion read: updateCollideRange)
  local q = { { key = '2:0_2' }, { key = '2:-2_0' }, { key = '2:2_0' } }
  T.check(q[roads.nextWalk(q)].key == '2:2_0', 'fast: the tile ahead on the path first')
  table.remove(q, 3)
  T.check(q[roads.nextWalk(q)].key == '2:0_2', 'then beside, behind last')
  q = { { key = '2:2_0', what = 'walkcol' }, { key = '2:2_0', what = 'bldcol' }, { key = '2:2_0', what = 'deck' } }
  T.check(q[roads.nextWalk(q)].what == 'deck', 'fast: what the car drives on first')
  q = { { key = '2:2_0', what = 'walkcol' }, { key = '2:0_0', what = 'walkcol' } }
  car.pos = vec3(230, 128, 0.5)       -- (past tile 0_0's centre by 102 m, still in it: not yet behind)
  roads.update(gov, 2)
  T.check(q[roads.nextWalk(q)].key == '2:0_0', 'the tile the car is in first')
  car.pos = vec3(300, 128, 0.5)       -- (its centre 172 m behind: left behind)
  roads.update(gov, 2)
  T.check(q[roads.nextWalk(q)].key == '2:2_0', 'a tile left behind goes after the one ahead')
  car.pos = vec3(300, 128, 0.5)       -- (tile 2_0's centre 340 m ahead: within 6 s at 45 m/s)
  roads.update(gov, 2)
  q = { { key = '2:2_0', what = 'walkcol' }, { key = '2:0_2', what = 'deck' }, { key = '2:2_0', what = 'deck' },
        { key = '2:-2_0', what = 'deck' } }
  T.check(roads.urgentWalk(q) == 3, 'urgent: the deck ahead within reach (not beside first, not behind)')
  T.check(roads.urgentWalk({ { key = '2:2_0', what = 'walkcol' } }) == nil, 'urgent: decks and tunnels only')
  -- the drawn surfaces after the colliders of their distance (RENDER_AFTER_M; nil: after every collider, as before),
  -- or by the same distance with SURFACE_FIRST
  q = { { key = '2:2_0', what = 'bldcol' }, { key = '2:2_0', what = 'walk', renderOnly = true } }
  T.check(q[roads.nextWalk(q)].what == 'bldcol', 'render-only sidewalks after the colliders')
  q = { { key = '2:0_2', what = 'bldcol' }, { key = '2:2_0', what = 'walk', renderOnly = true } }
  roads.SURFACE_FIRST = true
  T.check(q[roads.nextWalk(q)].what == 'walk', 'SURFACE_FIRST: the sidewalk ahead before a collider beside')
  q[2].what = 'bld'
  T.check(q[roads.nextWalk(q)].what == 'bld', 'a drawn building ahead before a collider left behind (RENDER_AFTER_M)')
  local after = roads.RENDER_AFTER_M
  roads.RENDER_AFTER_M = nil
  T.check(q[roads.nextWalk(q)].what == 'bldcol', 'RENDER_AFTER_M nil: drawn buildings after every collider')
  roads.RENDER_AFTER_M = after
  q = { { key = '2:2_0', what = 'bld', renderOnly = true }, { key = '2:2_0', what = 'bldcol' } }
  T.check(q[roads.nextWalk(q)].what == 'bldcol', 'SURFACE_FIRST: drawn buildings still after the colliders')
  local _, _, nearest = roads.nextWalk({ { key = '2:0_2', what = 'bldcol' }, { key = '2:2_0', what = 'bld', renderOnly = true } })
  T.check(nearest and math.abs(nearest - 340) < 1e-6, 'nearest: the way ahead before the render penalty, the tile behind left out')
  roads.SURFACE_FIRST = false
  function car:getVelocity() return vec3(0, 0, 0) end
  roads.update(gov, 2)
  q = { { key = '2:2_0' }, { key = '2:0_1' } }
  T.check(q[roads.nextWalk(q)].key == '2:0_1', 'standing: the nearer tile first')
  for _, k in ipairs({ '2_0', '0_1', '0_2', '-2_0', '0_0' }) do roads.onDrop({ anchorId = 2, key = k }) end
  roads.update(gov, 2)
  car.getVelocity = nil
end
getPlayerVehicle = nil

-- a road mesh: the tile's DecalRoads are not made; one TSStatic without collision instead
files['/t/r.json'] = '{ chunks = { ' .. chunk(0) .. ', ' .. chunk(5) .. ' }, road = { shape = "/t/road_2_6_6.cdae", pos = { 0, 0, 0 } } }'
S.calls.reloadCollision = 0
roads.onTile({ anchorId = 2, key = '6_6', path = '/t/r.json', n = 2 }, 2)
roads.update(gov, 2)
local rm = statics()[1]
T.check(#statics() == 1 and decalRoads() == 0 and roads.status().roadMeshes == 1, 'road mesh instead of DecalRoads')
T.check(rm.fields.shapeName == '/t/road_2_6_6.cdae' and rm.fields.collisionType == 'None', 'render only')
T.check(S.calls.reloadCollision == 0, 'no collision reload for it')
roads.onDrop({ anchorId = 2, key = '6_6' })
roads.update(gov, 2)
T.check(#statics() == 0 and S.calls.reloadCollision == 0, 'dropped without a collision reload')

-- origin rebase: the shown tiles move into the new frame and stay ("ghosts") until the new anchor's made tiles cover
-- them; the server's drop of a ghost does not remove it early
local shift = { apply = function(_, x, y, z) return x, y - 20, z end, beamngQuat = function() return 0, 0, 0, 1 end }
local geo = { quatMul = function(ax, ay, az, aw) return ax, ay, az, aw end }
roads.onTile({ anchorId = 2, key = '0_0', path = '/t/r.json', n = 2 }, 2)
roads.update(gov, 2)
local g0 = statics()[1]
T.check(#statics() == 1 and g0.pos.x == 0, 'a road mesh at its place')
roads.transform(shift, geo)
T.check(g0.pos.x == 0 and g0.pos.y == -20 and g0.rot ~= nil, 'moved (and turned) by the rebase')
roads.update(gov, 3)
T.check(#statics() == 1 and roads.status().tiles == 1, 'kept under the new anchor')
roads.onDrop({ anchorId = 2, key = '0_0' })
roads.update(gov, 3)
T.check(#statics() == 1, 'the server\'s drop of a ghost waits for its cover')
-- its square (centre 128, 128-20) overlaps the new grid's 0_0 and 0_-1: covered once both are made
roads.onTile({ anchorId = 3, key = '0_0', path = '/t/r.json', n = 2 }, 3)
roads.update(gov, 3); roads.update(gov, 3)
T.check(#statics() == 2, 'half covered: the ghost stays')
roads.onTile({ anchorId = 3, key = '0_-1', path = '/t/r.json', n = 2 }, 3)
roads.update(gov, 3); roads.update(gov, 3); roads.update(gov, 3)
T.check(#statics() == 2 and roads.status().tiles == 2, 'covered: the ghost goes, the two new tiles stay')
roads.onDrop({ anchorId = 3, key = '0_0' }); roads.onDrop({ anchorId = 3, key = '0_-1' })
roads.update(gov, 3)

-- a ghost dropped far from the car counts as deletesDirty (its stale collision harms nobody out there: the lazy
-- RELOAD_DELETES_S rebuild picks it up), and a rebase itself still rebuilds at once (the island moved)
do
  gov.allow, gov.tries = nil, 0
  roads.RELOAD_EVERY_S, roads.COLLIDE_CHECK_S = 0, 0
  local waitS = roads.GHOST_FAR_WAIT_S
  roads.GHOST_FAR_WAIT_S = 0
  local car2 = { pos = vec3(100, 100, 0.5) }
  function car2:getPosition() return self.pos end
  getPlayerVehicle = function() return car2 end
  files['/t/gw.json'] = '{ chunks = { }, walk = { shape = "/t/walks_ghost.cdae", pos = { 0, 0, 0 } } }'
  roads.onTile({ anchorId = 3, key = '0_0', path = '/t/gw.json', n = 0 }, 3)
  roads.update(gov, 3)
  T.check(#statics() == 1, 'sidewalk made near the car')
  local r0 = S.calls.reloadCollision
  roads.transform(shift, geo)
  roads.update(gov, 4, { x = 100, y = 100 })
  T.check(S.calls.reloadCollision == r0 + 1, 'a rebase rebuilds at once (the island moved)')
  r0 = S.calls.reloadCollision
  -- the car leaves; the ghost goes far behind it (GHOST_FAR_M): no prompt rebuild for a far drop
  car2.pos = vec3(5000, 5000, 0.5)
  roads.update(gov, 4, { x = 5000, y = 5000 })
  roads.update(gov, 4, { x = 5000, y = 5000 })
  roads.update(gov, 4, { x = 5000, y = 5000 })
  T.check(roads.status().tiles == 0 and #statics() == 0, 'the far ghost dropped')
  T.check(S.calls.reloadCollision == r0, 'a ghost dropped far from the car: no prompt rebuild')
  local deletesS = roads.RELOAD_DELETES_S
  roads.RELOAD_DELETES_S = 0
  roads.update(gov, 4, { x = 5000, y = 5000 })
  T.check(S.calls.reloadCollision == r0 + 1, 'its stale collision goes with the lazy deletes rebuild')
  roads.RELOAD_DELETES_S = deletesS
  roads.GHOST_FAR_WAIT_S = waitS
  getPlayerVehicle = nil
end

-- another module's colliders (markCollisionDirty, wired to the gas stations', pump jacks' and wind turbines'
-- onCollide): with the spot's position the prompt flag only near an island; a far spot waits and promotes when the
-- player arrives; without a position: prompt, as before
do
  gov.allow, gov.tries = nil, 0
  roads.RELOAD_EVERY_S, roads.COLLIDE_CHECK_S = 0, 0
  local car3 = { pos = vec3(0, 0, 0.5) }
  function car3:getPosition() return self.pos end
  getPlayerVehicle = function() return car3 end
  roads.update(gov, 4)
  local r0 = S.calls.reloadCollision
  roads.markCollisionDirty(8962, 0)             -- a pump jack spawned ~9 km out, by its square's west edge
  roads.update(gov, 4)
  T.check(S.calls.reloadCollision == r0, 'a far spot: no prompt rebuild')
  roads.markCollisionDirty(9215, 200)           -- the same square (8960..9216) churns again, at its far corner
  roads.update(gov, 4)
  T.check(S.calls.reloadCollision == r0, 'the far spot churning: still none')
  -- the square promotes by its own geometry (centre 9088, 128 + the tile margins: ~758 m reach), never by whichever
  -- spot was marked last: from 8400, 0 the centre is ~700 m away, the far-corner spot ~839 m
  car3.pos = vec3(8400, 0, 0.5)
  roads.update(gov, 4)
  T.check(S.calls.reloadCollision == r0 + 1, 'the player arrives: the deferred square rebuilds once, by its geometry')
  roads.update(gov, 4)
  T.check(S.calls.reloadCollision == r0 + 1, 'and is done')
  roads.markCollisionDirty(8405, 5)
  roads.update(gov, 4)
  T.check(S.calls.reloadCollision == r0 + 2, 'a near spot marks at once')
  roads.markCollisionDirty()
  roads.update(gov, 4)
  T.check(S.calls.reloadCollision == r0 + 3, 'no position: prompt, as before')
  getPlayerVehicle = nil
end

-- a Lua reload: the new module state deletes the objects the old one left
roads.onTile({ anchorId = 2, key = '0_0', path = '/t/a.json', n = 2 }, 2)
roads.update(gov, 2)
T.check(decalRoads() == 2, 'made')
package.loaded['beamngEarth/roads'] = nil
local fresh = require('beamngEarth/roads')
T.check(decalRoads() == 0 and fresh.status().roads == 0, 'a reloaded module sweeps the old roads')
roads = fresh

-- level end
roads.onTile({ anchorId = 2, key = '0_0', path = '/t/a.json', n = 2 }, 2)
roads.reset()
T.check(roads.status().tiles == 0 and roads.status().queued == 0, 'level end: forgotten')

-- high speed (2026-10-19): the collision reach is ahead of a fast car only, all round when slow or no direction
do
  local p = vec3(0, 0, 0)
  local east = { x = 1, y = 0 }
  T.check(roads.radiusToward(p, 1500, 100, 1400, east) == 1400, 'ahead of a fast car: the full reach')
  T.check(roads.radiusToward(p, -1500, 0, 1400, east) == roads.COLLIDE_M[1], 'behind it: the minimum')
  T.check(roads.radiusToward(p, 0, 1500, 1400, east) == roads.COLLIDE_M[1], 'beside it: the minimum')
  T.check(roads.radiusToward(p, -100, 50, 1400, east) == 1400, 'its own tile: the full reach')
  T.check(roads.radiusToward(p, -1500, 0, 1400, nil) == 1400, 'slow (no direction): all round')
  T.check(roads.collideRadius(100) == 1400 and roads.collideRadius(500) == roads.COLLIDE_M[2], 'reach: 14 s of driving, capped')
end

-- the skyline: a 1 km square is covered once all 16 of its far tiles are here (no hole while they arrive)
do
  local seen = {}
  T.check(not roads.skyCovered(seen, 1, 0, 0), 'skyCovered: nothing here')
  for i = 0, 3 do for j = 0, 3 do if not (i == 3 and j == 3) then seen['1:' .. i .. '_' .. j] = true end end end
  T.check(not roads.skyCovered(seen, 1, 0, 0), 'skyCovered: 15 of 16: still shown')
  seen['1:3_3'] = true
  T.check(roads.skyCovered(seen, 1, 0, 0), 'skyCovered: all 16: hidden')
  T.check(not roads.skyCovered(seen, 2, 0, 0), 'skyCovered: another anchor\'s')
  local s2 = {}
  for i = -4, -1 do for j = 4, 7 do s2['1:' .. i .. '_' .. j] = true end end
  T.check(roads.skyCovered(s2, 1, -1, 1), 'skyCovered: negative squares')
end
-- the opaque road surfaces: drawn opaque and casting no sun shadow, one reload each (PR #41)
do
  roads.reset()
  local made = {}
  for _, name in ipairs({ 'road_asphalt_2lane', 'm_gm_concrete_plain', 'curbs' }) do
    local m = createObject('Material'); m:registerObject(name); m:setField('translucent', 0, '1'); made[name] = m
  end
  S.calls.reload_Material = 0
  files['/t/mat.json'] = '{ chunks = { ' .. chunk(0) .. ' } }'
  roads.onTile({ anchorId = 7, key = '0_0', path = '/t/mat.json', n = 1 }, 7)
  roads.update(gov, 7)
  local a, c, k = made.road_asphalt_2lane, made.m_gm_concrete_plain, made.curbs
  T.check(a:getField('translucent') == '0' and a:getField('castShadows') == '0', 'asphalt: opaque, no shadow')
  T.check(c:getField('castShadows') == '0' and c:getField('translucent') == '1', 'concrete streets: no shadow')
  T.check(k:getField('castShadows') == nil, 'the curbs keep their shadow')
  T.check(S.calls.reload_Material == 2, 'one reload a changed material')
  for _, m in pairs(made) do m:delete() end
  roads.reset()
end

-- Dynamic sign text is one render-only mesh; resend/drop clean up the same way as wires.
roads.reset(); gov.allow = nil
files['/t/signs.json'] = '{ chunks = {}, signs = { shape = "/t/signs_3_0_0.cdae", pos = {0,0,0} } }'
roads.onTile({anchorId=3,key='0_0',path='/t/signs.json',n=0},3)
roads.update(gov,3)
T.check(#statics() == 1 and statics()[1].fields.collisionType == 'None', 'sign faces render only (posts collide separately)')
roads.onTile({anchorId=3,key='0_0',path='/t/signs.json',n=0},3); roads.update(gov,3)
T.check(#statics() == 1, 'revisited signs do not duplicate')
roads.onDrop({anchorId=3,key='0_0'}); roads.update(gov,3)
T.check(#statics() == 0, 'sign mesh deleted with tile')
-- the materials' prewarm shape, and its sibling in the dirt and gravel strips' vertex format when the server wrote one
do
  roads.reset()
  local function shapes()
    local r = {}
    for _, o in ipairs(S.liveOf('TSStatic')) do r[#r + 1] = o:getField('shapeName') end
    table.sort(r)
    return table.concat(r, ' ')
  end
  for _, o in ipairs(S.liveOf('TSStatic')) do o:delete() end
  local exists = FS.fileExists
  FS.fileExists = function(_, p) return p == '/t/a/prewarm_x.cdae' end
  T.check(roads.prewarm('/t/a/prewarm.cdae'), 'prewarm taken')
  T.check(shapes() == '/t/a/prewarm.cdae /t/a/prewarm_x.cdae', 'both prewarm shapes: ' .. shapes())
  FS.fileExists = function() return false end
  roads.prewarm('/t/b/prewarm.cdae')
  T.check(shapes() == '/t/b/prewarm.cdae', 'an older server: its one shape, the old ones gone: ' .. shapes())
  FS.fileExists = exists
  for _, o in ipairs(S.liveOf('TSStatic')) do o:delete() end
  roads.reset()
end
-- Custom median walls have their own collision and lifecycle, separate from decks and signs.
do
  roads.reset()
  -- (the barrier's statics only: the materials' prewarm shapes stand beside them)
  local function statics()
    local r = {}
    for _, o in pairs(S.objects) do
      if o.class == 'TSStatic' and not tostring(o.fields and o.fields.shapeName or ''):find('prewarm', 1, true) then
        r[#r + 1] = o
      end
    end
    return r
  end
  gov.allow, gov.tries = nil, 0
  car.pos = vec3(103, 105, .5)
  getPlayerVehicle = function() return car end
  files['/t/barrier.json'] = '{ chunks = {}, barrier = { shape = "/t/barrier_7_0_0.cdae", pos = { 0, 0, 0 } }, ' ..
    'colliders = { barrier = { { 99.5, 100, -.5, 100.5, 110, 1.9 } } } }'
  roads.onTile({ anchorId = 7, key = '0_0', path = '/t/barrier.json', n = 0 }, 7)
  roads.update(gov, 7)
  T.check(#statics() == 1, 'barrier beside the car loads without a wide collision hold')
  T.check(statics()[1].fields.collisionType == 'Visible Mesh Final', 'custom barrier collides')
  roads.onDrop({ anchorId = 7, key = '0_0' })
  roads.update(gov, 7)
  T.check(#statics() == 0, 'barrier deleted with its tile')
  car.pos = vec3(100, 105, .5)
  roads.onTile({ anchorId = 7, key = '0_0', path = '/t/barrier.json', n = 0 }, 7)
  roads.update(gov, 7)
  T.check(#statics() == 0, 'barrier collision held while intersecting the car')
  car.pos = vec3(103, 105, .5)
  roads.update(gov, 7)
  T.check(#statics() == 1, 'barrier released once the car is clear')
  roads.clear()
  T.check(#statics() == 0, 'clear leaves no orphaned barrier')
  getPlayerVehicle = nil
end
-- non-urgent collision rebuilds wait while the car rolls slowly (wheels trapped in the terrain by a rebuild)
do
  local d = roads.deferWhileRolling
  T.check(d(0, 0) == false, 'parked: a rebuild runs')
  T.check(d(nil, 0) == false, 'speed unknown: a rebuild runs')
  T.check(d(4, 6000) == true, 'rolling at 4 m/s: the rebuild waits')
  T.check(d(4, roads.ROLL_DEFER_S * 1000 + 1) == false, '... ROLL_DEFER_S at most')
  T.check(d(roads.FAST_MS, 6000) == false, 'at FAST_MS and above: no wait')
end
-- rebuild by deadline (NEED): a collider made within the collision radius waits until the car is within needReach of
-- what it covers (its boxes), and one rebuild then makes everything pending solid; the old rule (tile centre within
-- URGENT_S + the tile's half-diagonal) rebuilt at once
do
  roads.clear()
  gov.allow, gov.tries = nil, 0
  local every, check = roads.RELOAD_EVERY_S, roads.COLLIDE_CHECK_S
  roads.RELOAD_EVERY_S, roads.COLLIDE_CHECK_S = 1e9, 0
  local carN = { pos = vec3(110, -150, 10.5) }   -- tile 0_0's centre 278 m away, the deck's box 250 m
  function carN:getPosition() return self.pos end
  getPlayerVehicle = function() return carN end
  files['/t/nd.json'] = '{ chunks = { }, deck = { shape = "/t/deck_8_0_0.cdae", pos = { 0, 0, 0 } }, ' ..
    'colliders = { deck = { { 100, 100, 7.8, 120, 110, 12 } } } }'
  local function upd() elapsedMs = elapsedMs + 16; roads.update(gov, 8) end   -- (the reloaded module: unwrapped)
  local function settle() for _ = 1, 80 do upd() end end    -- (past URGENT_GAP_S)
  local function decks()
    local n = 0
    for _, o in ipairs(statics()) do if o.fields.shapeName == '/t/deck_8_0_0.cdae' then n = n + 1 end end
    return n
  end
  roads.NEED = false
  settle()
  roads.onTile({ anchorId = 8, key = '0_0', path = '/t/nd.json', n = 0 }, 8)
  local r0 = S.calls.reloadCollision
  upd()
  T.check(decks() == 1 and S.calls.reloadCollision == r0 + 1, 'old rule: the deck rebuilds at once (its tile within reach)')
  roads.clear()
  settle()
  roads.NEED = true
  T.check(roads.needReach(0) == roads.NEED_MIN_M + roads.NEED_PAD_M and roads.needReach(20) == 20 * roads.NEED_S +
    roads.NEED_PAD_M and roads.needReach(40) == 40 * roads.URGENT_S + roads.NEED_PAD_M, 'needReach: floor, NEED_S, URGENT_S when fast')
  roads.onTile({ anchorId = 8, key = '0_0', path = '/t/nd.json', n = 0 }, 8)
  r0 = S.calls.reloadCollision
  settle()
  T.check(decks() == 1 and S.calls.reloadCollision == r0, 'NEED: made, but no rebuild 250 m from its box')
  T.check(math.abs(roads.needDistance() - 250) < 1e-6, 'its distance: to the deck box, not the tile')
  carN.pos = vec3(110, 100 - roads.needReach(0) + 1, 10.5)
  upd()
  T.check(S.calls.reloadCollision == r0 + 1, 'the car within needReach of the box: rebuilt')
  local log = roads.reloadLog()
  T.check(log[#log].why == 'urgent' and log[#log].n == 1 and log[#log].kinds.deck == 1, 'the reload log says why and what')
  T.check(roads.needDistance() == math.huge, 'nothing pending after it')
  settle()
  T.check(S.calls.reloadCollision == r0 + 1, 'and nothing more')
  T.check(roads.onlyColliderDirt({ tile = 2, tileNear = 1 }) and not roads.onlyColliderDirt({ tile = 1, request = 1 }),
    'onlyColliderDirt: made colliders only')
  -- right after a rebuild a collider inside needReach but beyond hardReach waits for NEED_GAP_S (the same tile's next
  -- collider a frame later rides along); one within hardReach goes after URGENT_GAP_S
  files['/t/nd2.json'] = '{ chunks = { }, deck = { shape = "/t/deck_8_1_0.cdae", pos = { 0, 0, 0 } }, ' ..
    'colliders = { deck = { { 356, 100, 7.8, 376, 110, 12 } } } }'
  carN.pos = vec3(356 - roads.needReach(0) + 5, 105, 10.5)      -- inside needReach, beyond hardReach
  T.check(356 - carN.pos.x > roads.hardReach(0), '(the test spot is beyond hardReach)')
  roads.onTile({ anchorId = 8, key = '1_0', path = '/t/nd2.json', n = 0 }, 8)
  upd()
  T.check(S.calls.reloadCollision == r0 + 1, 'inside needReach right after a rebuild: waits')
  for _ = 1, math.ceil(roads.NEED_GAP_S * 1000 / 16) do upd() end
  T.check(S.calls.reloadCollision == r0 + 2, '... until NEED_GAP_S after it')
  roads.onDrop({ anchorId = 8, key = '1_0' }); upd()
  for _ = 1, 3 do upd() end
  r0 = S.calls.reloadCollision
  carN.pos = vec3(356 - roads.hardReach(0) + 5, 105, 10.5)      -- within hardReach
  roads.onTile({ anchorId = 8, key = '1_0', path = '/t/nd2.json', n = 0 }, 8)
  for _ = 1, math.ceil(roads.URGENT_GAP_S * 1000 / 16) + 1 do upd() end
  T.check(S.calls.reloadCollision >= r0 + 1 and S.calls.reloadCollision <= r0 + 2, 'within hardReach: after URGENT_GAP_S')
  roads.clear()
  roads.RELOAD_EVERY_S, roads.COLLIDE_CHECK_S = every, check
  getPlayerVehicle = nil
end
-- the road import queue on a Steam Deck (2026-11-14): each mesh class its own governor kind, due work said to the
-- governor (setDue) and reserved against the far tiles (reserve), the drawn surfaces under the car made at once
do
  T.check(roads.walkKind('road') == 'roadSurf' and roads.walkKind('walk') == 'roadSurf'
    and roads.walkKind('bldcol') == 'roadCol' and roads.walkKind('railcol2') == 'roadCol'
    and roads.walkKind('deck') == 'roadDrive' and roads.walkKind('bld') == 'roadWalk', 'each mesh class its own kind')
  roads.KIND_SPLIT = false
  T.check(roads.walkKind('road') == 'roadWalk', 'KIND_SPLIT off: one kind, as before')
  roads.KIND_SPLIT = true
  T.check(roads.dueReach(0) == roads.DUE_MIN_M and roads.dueReach(30) == 30 * roads.DUE_S, 'the due reach')
  local car = { pos = vec3(128, 128, 0.5) }
  function car:getPosition() return self.pos end
  function car:getVelocity() return vec3(20, 0, 0) end     -- (45 mph: under FAST_MS, over ORDER_MS)
  getPlayerVehicle = function() return car end
  local check = roads.COLLIDE_CHECK_S
  roads.COLLIDE_CHECK_S = 0
  -- a recording governor: denies every unit (tokens), takes due and reservations
  local g = { kinds = {}, due = 0, reserved = {} }
  function g:try(kind) self.kinds[#self.kinds + 1] = kind; return false end
  function g:charge() end
  function g:chargeMotion() end
  function g:setDue(on) if on then self.due = self.due + 1 end end
  function g:reserve(kind) self.reserved[#self.reserved + 1] = kind end
  files['/t/dq.json'] = '{ chunks = { }, road = { shape = "/t/road_9_0_0.cdae", pos = { 0, 0, 0 } }, ' ..
    'bld = { shape = "/t/bld_9_0_0.cdae", pos = { 0, 0, 0 } }, bldcol = { shape = "/t/bldcol_9_0_0.cdae", pos = { 0, 0, 0 } } }'
  files['/t/dq2.json'] = '{ chunks = { }, bldcol = { shape = "/t/bldcol_9_5_0.cdae", pos = { 0, 0, 0 } } }'
  roads.onTile({ anchorId = 9, key = '0_0', path = '/t/dq.json', n = 0 }, 9)
  roads.onTile({ anchorId = 9, key = '5_0', path = '/t/dq2.json', n = 0 }, 9)
  g.blocked = true          -- (quiet frames: no urgent surface)
  roads.update(g, 9)
  T.check(g.due == 1 and g.kinds[1] == 'roadSurf' and g.reserved[1] == 'roadSurf' and #g.kinds == 1,
    'the tile under the car is due: said once, its road tried under its own kind and reserved')
  -- 45 mph, ORDER_MS: a tile behind the car comes after one ahead
  car.pos = vec3(1300, 128, 0.5)            -- (tile 0_0's centre 1172 m behind, 5_0's 108 m ahead)
  g.kinds, g.reserved, g.due = {}, {}, 0
  roads.update(g, 9)
  T.check(g.kinds[1] == 'roadCol' and g.due == 1, 'the tile ahead first, due')
  local q = { { key = '9:0_0', what = 'bldcol' }, { key = '9:5_0', what = 'bldcol' } }
  local i, _, nearest = roads.nextWalk(q)
  T.check(i == 2 and math.abs(nearest - 108) < 1e-6, 'moving (ORDER_MS): the way ahead first, behind left out of nearest')
  -- nothing due far from the car
  car.pos = vec3(-3000, 128, 0.5)
  function car:getVelocity() return vec3(0, 0, 0) end
  g.kinds, g.reserved, g.due = {}, {}, 0
  roads.update(g, 9)
  T.check(g.due == 0 and #g.reserved == 0 and #g.kinds > 0, 'nothing within the due reach: not due, nothing reserved')
  -- the drawn road under the car: made at once (urgentWalk), but not in the quiet frames round a heavy op
  car.pos = vec3(128, 128, 0.5)
  function car:getVelocity() return vec3(20, 0, 0) end
  roads.update(g, 9)
  q = { { key = '9:5_0', what = 'bldcol' }, { key = '9:0_0', what = 'walk' }, { key = '9:0_0', what = 'road' } }
  T.check(roads.urgentWalk(q) == 3, 'urgent surfaces: the road of the tile under the car')
  T.check(roads.urgentWalk(q, true) == nil, 'not in quiet frames')
  table.remove(q, 3)
  T.check(roads.urgentWalk(q) == 2, 'then its sidewalks')
  T.check(roads.urgentWalk({ { key = '9:5_0', what = 'road' } }) == nil, 'not a tile 1 km ahead')
  car.pos = vec3(1200, 128, 0.5)            -- (tile 5_0 starts 80 m ahead: within URGENT_SURFACE_S at 20 m/s)
  roads.update(g, 9)
  T.check(roads.urgentWalk({ { key = '9:5_0', what = 'road' } }) == 1, 'the tile the car reaches within URGENT_SURFACE_S')
  roads.URGENT_SURFACES = false
  T.check(roads.urgentWalk({ { key = '9:5_0', what = 'road' } }) == nil, 'URGENT_SURFACES off: decks and tunnels only')
  roads.URGENT_SURFACES = true
  -- a forced surface is made whatever the governor says
  local made0 = 0
  for _, o in pairs(S.objects) do if o.class == 'TSStatic' then made0 = made0 + 1 end end
  car.pos = vec3(128, 128, 0.5)
  g.blocked = false
  for _ = 1, roads.URGENT_EVERY_FRAMES + 1 do roads.update(g, 9) end
  local made = 0
  for _, o in pairs(S.objects) do if o.class == 'TSStatic' then made = made + 1 end end
  T.check(made == made0 + 1, 'the road under the car made at once, the budget denied (' .. made .. ')')
  -- with the real governor: an urgent surface skips token starvation, not a used-up frame or a deep debt
  -- (governor.lua tryUrgent)
  local function count()
    local n = 0
    for _, o in pairs(S.objects) do if o.class == 'TSStatic' then n = n + 1 end end
    return n
  end
  roads.onDrop({ anchorId = 9, key = '0_0' })
  for _ = 1, 3 do roads.update(gov, 9) end
  local G = require('beamngEarth/governor')
  local gv = G.new({ clock = function() return elapsedMs end })
  local function frameWith(setup)
    gv:beginFrame(true, 30)
    setup(gv)
    roads.update(gv, 9)
  end
  roads.onTile({ anchorId = 9, key = '0_0', path = '/t/dq.json', n = 0 }, 9)
  local c0 = count()
  for _ = 1, roads.URGENT_EVERY_FRAMES + 1 do
    frameWith(function(x) x.tokens = -G.URGENT_DEBT_MS - 5 end)
  end
  T.check(count() == c0, 'urgent surface: not with the bucket deep in debt')
  frameWith(function(x) x.tokens = -2; x:noteOther(G.URGENT_ROOM_MS + 2) end)
  T.check(count() == c0, 'urgent surface: not into a frame already busy (its budget used up, tokens negative)')
  frameWith(function(x) x.tokens = -2 end)
  T.check(count() == c0 + 1, 'urgent surface: starved of tokens but the frame free: made at once')
  roads.onDrop({ anchorId = 9, key = '0_0' }); roads.onDrop({ anchorId = 9, key = '5_0' })
  roads.update(gov, 9)
  roads.COLLIDE_CHECK_S = check
  getPlayerVehicle = nil
end
-- BEHIND (2026-11-14): no colliders behind a moving car; its heading kept through a stop, dropped when it moves back
do
  local d = { x = 1, y = 0 }
  T.check(roads.behindBy({ x = 0, y = 0 }, d, -300, 0, 128) == 172 and roads.behindBy({ x = 0, y = 0 }, d, 200, 0, 128) < 0,
    'behindBy: the near edge along the heading')
  T.check(math.abs(roads.behindBy({ x = 0, y = 0 }, { x = 0.6, y = 0.8 }, -300, -400, 100) - (500 - 140)) < 1e-9,
    'behindBy: a diagonal heading takes the square\'s extent along it')
  local st = {}
  roads.behindStep(st, 0, 0, 20, 0, 250, false)
  T.check(st.dir and st.dir.x == 1, 'heading set at speed')
  roads.behindStep(st, 5, 0, 0, 0, 100000, false)
  T.check(st.dir ~= nil, 'kept through a stop')
  roads.behindStep(st, 5, 0, 0.5, 0.5, roads.BEHIND_SLOW_S * 1000, false)
  T.check(st.dir == nil, 'all round again after BEHIND_SLOW_S slow')
  roads.behindStep(st, 5, 0, 20, 0, 250, false)
  roads.behindStep(st, 5, 0, -2, 0, 250, false)
  T.check(st.dir == nil, 'dropped when the car moves back along it')
  roads.behindStep(st, 5, 0, 20, 0, 250, false)
  roads.behindStep(st, 5, 0, 1.5, 1, 250, false)
  T.check(st.dir ~= nil, 'kept turning sideways at low speed')
  roads.behindStep(st, 500, 0, 20, 0, 250, false)
  T.check(st.dir == nil, 'dropped at a jump (a teleport)')
  roads.behindStep(st, 505, 0, 20, 0, 250, false)
  roads.behindStep(st, 510, 0, 20, 0, 250, true)
  T.check(st.dir == nil, 'dropped under a drop hold')

  roads.clear()
  local function upd7() elapsedMs = elapsedMs + 16; roads.update(gov, 7) end   -- (the reloaded module: unwrapped)
  local car = { pos = vec3(128, 128, 0.5), v = vec3(20, 0, 0) }
  function car:getPosition() return self.pos end
  function car:getVelocity() return self.v end
  getPlayerVehicle = function() return car end
  local check, jump = roads.COLLIDE_CHECK_S, roads.BEHIND_JUMP_M
  roads.COLLIDE_CHECK_S, roads.BEHIND_JUMP_M = 0, 1e9      -- (the test moves the car in steps of hundreds of m)
  local function count()
    local n = 0
    for _, o in pairs(S.objects) do if o.class == 'TSStatic' then n = n + 1 end end
    return n
  end
  local c0 = count()
  files['/t/bh0.json'] = '{ chunks = { }, walk = { shape = "/t/walks_bh_0.cdae", pos = { 0, 0, 0 } } }'
  files['/t/bh1.json'] = '{ chunks = { }, walk = { shape = "/t/walks_bh_1.cdae", pos = { -256, 0, 0 } } }'
  upd7(); upd7()
  roads.onTile({ anchorId = 7, key = '0_0', path = '/t/bh0.json', n = 0 }, 7)
  roads.onTile({ anchorId = 7, key = '-1_0', path = '/t/bh1.json', n = 0 }, 7)
  upd7(); upd7()
  T.check(count() == c0 + 1, 'moving east: the tile under the car collides, the one behind it (128 m) waits')
  roads.collisionRebuilt()
  car.pos = vec3(300, 128, 0.5)
  upd7(); upd7()
  T.check(count() == c0 + 1, '44 m past the tile: kept')
  car.pos = vec3(420, 128, 0.5)
  for _ = 1, 3 do upd7() end
  T.check(count() == c0, '164 m past it: dropped')
  roads.collisionRebuilt()
  local L = roads.reloadLog()
  T.check(not (L[#L].src or {}).delete, 'its deletion no prompt rebuild (the stale collision is behind the car)')
  car.v = vec3(0, 0, 0)
  for _ = 1, 3 do upd7() end
  T.check(count() == c0, 'stopped: the heading kept, nothing made behind')
  car.v = vec3(-3, 0, 0)
  for _ = 1, 3 do upd7() end
  T.check(count() == c0 + 1, 'moving back: all round again, the tile behind made again')
  roads.BEHIND = false
  car.pos, car.v = vec3(128, 128, 0.5), vec3(20, 0, 0)
  for _ = 1, 3 do upd7() end
  T.check(count() == c0 + 2 and roads.behindDir() == nil, 'BEHIND off: colliders all round, as before')
  roads.BEHIND = true
  roads.onDrop({ anchorId = 7, key = '0_0' }); roads.onDrop({ anchorId = 7, key = '-1_0' })
  upd7()
  -- a collider queued while its tile was near, the tile behind the car by the time the governor lets it through:
  -- parked again, not made only to be dropped
  local c1 = count()
  local function walksMade() local u = roads.status().units.bldcol return u and u.n or 0 end
  files['/t/bh2.json'] = '{ chunks = { }, bldcol = { shape = "/t/bldcol_bh_2.cdae", pos = { 0, 0, 0 } } }'
  local w1 = walksMade()
  gov.allow, gov.tries = 0, 0
  car.pos, car.v = vec3(128, 128, 0.5), vec3(20, 0, 0)
  upd7()
  roads.onTile({ anchorId = 7, key = '0_0', path = '/t/bh2.json', n = 0 }, 7)
  upd7()
  car.pos = vec3(420, 128, 0.5)
  upd7()
  gov.allow, gov.tries = nil, 0
  upd7(); upd7()
  T.check(count() == c1 and walksMade() == w1 and roads.status().queued == 0, 'queued, then behind the car: parked, not made')
  car.v = vec3(-3, 0, 0)
  upd7(); upd7()
  T.check(count() == c1 + 1, '... made once the car turns back')
  roads.onDrop({ anchorId = 7, key = '0_0' })
  upd7()
  -- AHEAD_LEAD_S: with the heading, the colliders ahead made farther out (not beside the car)
  files['/t/ah1.json'] = '{ chunks = { }, bldcol = { shape = "/t/bldcol_ah_1.cdae", pos = { 512, 0, 0 } } }'
  files['/t/ah2.json'] = '{ chunks = { }, bldcol = { shape = "/t/bldcol_ah_2.cdae", pos = { 0, 512, 0 } } }'
  local c2 = count()
  car.pos, car.v = vec3(0, 128, 0.5), vec3(20, 0, 0)   -- (tile 2_0's centre 640 m ahead, 0_2's 528 m off at 76 deg)
  roads.onTile({ anchorId = 7, key = '2_0', path = '/t/ah1.json', n = 0 }, 7)
  roads.onTile({ anchorId = 7, key = '0_2', path = '/t/ah2.json', n = 0 }, 7)
  for _ = 1, 3 do upd7() end
  T.check(count() == c2, 'AHEAD_LEAD_S off: neither')
  roads.AHEAD_LEAD_S = 30
  for _ = 1, 3 do upd7() end
  T.check(count() == c2 + 1, 'AHEAD_LEAD_S: the tile 640 m ahead collides, the one beside waits')
  roads.AHEAD_LEAD_S = 0
  for _, k in ipairs({ '2_0', '0_2' }) do roads.onDrop({ anchorId = 7, key = k }) end
  upd7()
  -- another module's colliders: urgent with a distance within needReach, else the throttled slot
  roads.collisionRebuilt()
  local r0 = S.calls.reloadCollision
  local every = roads.RELOAD_EVERY_S
  roads.RELOAD_EVERY_S = 1e9
  for _ = 1, 80 do upd7() end       -- (past URGENT_GAP_S)
  car.pos, car.v = vec3(128, 128, 0.5), vec3(20, 0, 0)
  upd7()
  roads.requestCollision({ 628, 100, 0, 640, 140, 0 })        -- (500 m ahead)
  upd7()
  T.check(S.calls.reloadCollision == r0, 'trees made 500 m off: no prompt rebuild')
  roads.requestCollision({ 148, 100, 0, 160, 140, 0 })        -- (20 m ahead)
  upd7()
  T.check(S.calls.reloadCollision == r0 + 1, 'trees made 20 m off (the car turned back): rebuilt at once')
  -- made 150 m ahead (beyond needReach): no rebuild; due as the car drives up to them, nothing asked again
  for _ = 1, 80 do upd7() end
  r0 = S.calls.reloadCollision
  roads.requestCollision({ 278, 100, 0, 290, 140, 0 })
  upd7()
  T.check(S.calls.reloadCollision == r0, 'trees made 150 m ahead: not yet')
  car.pos = vec3(240, 128, 0.5)
  upd7()
  T.check(S.calls.reloadCollision == r0 + 1, '... rebuilt once the car is within hardReach of them')
  -- another player's car (a focus with trees) about to reach trees made again behind the active car: due for it too
  for _ = 1, 80 do upd7() end
  r0 = S.calls.reloadCollision
  car.pos = vec3(3000, 128, 0.5)
  upd7()
  roads.requestCollision({ 148, 100, 0, 160, 140, 0 })
  upd7()
  T.check(S.calls.reloadCollision == r0, 'trees 2.8 km behind the active car: not due for it')
  roads.setFoci({ { x = 128, y = 128, r = 320, trees = true, kind = 'player' } })
  upd7()
  T.check(S.calls.reloadCollision == r0 + 1, '... but due for the other player\'s car 20 m from them')
  roads.setFoci({ { x = 128, y = 128, r = 80, trees = false, kind = 'traffic' } })
  roads.requestCollision({ 148, 100, 0, 160, 140, 0 })
  for _ = 1, 80 do upd7() end
  T.check(S.calls.reloadCollision == r0 + 1, 'traffic: not (no solid trees round it, its rebuilds as before)')
  -- a road collider made again for an AI traffic car (its island) far behind the active car: due within hardReach of it
  roads.collisionRebuilt()
  r0 = S.calls.reloadCollision
  roads.setFoci({ { x = 128 + 182 + 100, y = 128, r = 400, trees = false, kind = 'traffic' } })
  roads.onTile({ anchorId = 7, key = '0_0', path = '/t/bh2.json', n = 0 }, 7)
  for _ = 1, 80 do upd7() end
  T.check(S.calls.reloadCollision == r0, 'made for traffic 100 m off it: not yet (' .. S.calls.reloadCollision - r0 .. ')')
  roads.setFoci({ { x = 128 + 182 + 20, y = 128, r = 400, trees = false, kind = 'traffic' } })
  upd7()
  T.check(S.calls.reloadCollision == r0 + 1, '... rebuilt with the traffic car 20 m from it')
  roads.onDrop({ anchorId = 7, key = '0_0' })
  upd7()
  roads.setFoci({})
  T.check(not roads.extDue(nil, 20, 1e6) and not roads.extDue(roads.needReach(20) + 1, 20, 1e6)
    and roads.extDue(roads.hardReach(20), 20, 0) and not roads.extDue(roads.hardReach(20) + 1, 20, 0)
    and roads.extDue(roads.hardReach(20) + 1, 20, roads.NEED_GAP_S * 1000), 'extDue: the NEED rule of the roads\' colliders')
  roads.RELOAD_EVERY_S = every
  roads.COLLIDE_CHECK_S, roads.BEHIND_JUMP_M = check, jump
  getPlayerVehicle = nil
end
T.done('test_roads')
