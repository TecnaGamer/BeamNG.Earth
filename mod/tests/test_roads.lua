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

local roads = require('beamngEarth/roads')

local gov = { tries = 0 }
function gov:try() self.tries = self.tries + 1; return self.allow == nil or self.tries <= self.allow end
function gov:charge() end
function gov:chargeMotion() end

local function chunk(x)
  return string.format('{ m = "road_asphalt_2lane", w = 6, tl = 11, p = 20, n = { {%d, 0, 10}, {%d, 10, 10}, {%d, 20, 10} } }', x, x, x)
end
files['/t/a.json'] = '{ chunks = { ' .. chunk(0) .. ', ' .. chunk(5) .. ' } }'
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
gov.allow, gov.tries = 1, 0
roads.update(gov, 1)
T.check(decalRoads() == 1 and roads.status().queued == 1, 'one chunk per allowed unit')
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
roads.update(gov, 1)
T.check(decalRoads() == 3, 'second tile made')
roads.onDrop({ anchorId = 1, key = '0_0' })
roads.update(gov, 1)
T.check(decalRoads() == 1 and roads.status().tiles == 1, 'dropped tile deleted')

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
T.done('test_roads')
