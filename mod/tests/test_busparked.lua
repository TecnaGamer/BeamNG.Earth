-- luajit mod/tests/test_busparked.lua  (buses at the bus stations' bays: only with traffic on, three at most, cheap passes)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')

function vec3(x, y, z) return { x = x, y = y, z = z } end
function quatFromDir(d) return { d = d } end
function log() end
local objects, nextId, spawned, teleports, deleted, brakes = {}, 0, 0, 0, 0, 0
be = {
  getObjectByID = function(_, id) return objects[id] end,
  getSurfaceHeightBelow = function(_, p) return p.z - 3 end,
}
core_vehicles = {
  spawnNewVehicle = function(model, opt)
    nextId = nextId + 1
    spawned = spawned + 1
    local v = { id = nextId, model = model, pos = opt.pos }
    function v:getId() return self.id end
    function v:delete() deleted = deleted + 1; objects[self.id] = nil end
    function v:queueLuaCommand(c) if c:find('parkingbrake') then brakes = brakes + 1 end end
    objects[nextId] = v
    return v
  end,
}
spawn = { safeTeleport = function(v, pos, rot) teleports = teleports + 1; v.pos = pos end }
local traffic = 'on'
gameplay_traffic = { getState = function() return traffic end }

local B = require('beamngEarth/busparked')
local NG = require('beamngEarth/navgraph')
NG.takeChoice({ trafficMode = 'enabled', trafficParked = 'disabled' })

-- pure: bay key, traffic gate
T.check(B.bayKey({ 1.04, -2.0, 0, 0, 2 }) == '1.0_-2.0', 'bay key: position to 0.1 m')
T.check(B.trafficOn({ getState = function() return 'on' end }, { traffic = false }), 'stock traffic on: on whatever the choice')
T.check(not B.trafficOn({ getState = function() return 'off' end }, { traffic = true }), 'stock traffic off: off')
T.check(B.trafficOn(nil, { traffic = true }) and not B.trafficOn(nil, { traffic = false }), 'no stock system: the level-start choice')

-- pure: pick = the nearest within the range, apart from each other, none too near the player unless it held one
local rows = {
  { 100, 0, 1, 0, 2 }, { 105, 0, 1, 0, 2 },            -- 5 m apart: only one of the two
  { 0, 150, 1, 0, 2 }, { -190, 0, 1, 0, 2 }, { 500, 0, 1, 0, 2 }, { 6, 0, 1, 0, 2 } }
local pick = B.pick(rows, 0, 0, 3, 220, 14, 16, nil)
T.check(#pick == 3, 'three at most')
T.check(pick[1].row[1] == 100 and pick[2].row[2] == 150 and pick[3].row[1] == -190, 'nearest first, 5 m neighbour and the 500 m one left out, 6 m one too near')
T.check(#B.pick(rows, 0, 0, 3, 220, 14, 16, { ['6.0_0.0'] = true }) == 3 and B.pick(rows, 0, 0, 3, 220, 14, 16, { ['6.0_0.0'] = true })[1].row[1] == 6,
  'a bay that already holds a bus stays eligible when near')
T.check(#B.pick(rows, 0, 0, 1, 220, 14, 16, nil) == 1, 'max respected')

-- passes: nothing without rows; traffic off: no bus; on: one bus
local player = { x = 0, y = 0, z = 10 }
local now = 0
B.update(1, player, now)
T.check(spawned == 0, 'no rows: nothing')
B.onRows(1, '1:0_0', { { 100, 0, 10, 90, 2 }, { 0, 150, 10, 0, 2 }, { -190, 0, 10, 180, 2 }, { 0, -200, 10, 0, 2 } })
traffic = 'off'
now = now + 3
B.update(1, player, now)
T.check(spawned == 0, 'traffic off: no bus')
traffic = 'on'
now = now + 3
B.update(1, player, now)
T.check(spawned == 1 and teleports == 1 and brakes == 1, 'traffic on: one spawn per pass, parked (brake on)')
B.update(1, player, now + 0.5)
T.check(spawned == 1, 'a pass within UPDATE_S does nothing')
now = now + 3
B.update(1, player, now)
now = now + 3
B.update(1, player, now)
now = now + 3
B.update(1, player, now)
T.check(spawned == 1, 'one bus at most (three in a row closed a lane)')
T.check(B.status().buses == 1, 'status: one')

-- traffic switched off: the buses go
traffic = 'off'
now = now + 3
B.update(1, player, now)
T.check(B.status().buses == 0 and deleted == 1, 'traffic off: the bus is deleted')

-- the player drives far away: a bus is moved to a new bay, not spawned again
traffic = 'on'
B.onRows(1, '1:5_5', { { 3000, 0, 10, 0, 2 }, { 3100, 80, 10, 0, 2 } })
now = now + 3; B.update(1, player, now)
now = now + 3; B.update(1, player, now)
now = now + 3; B.update(1, player, now)
local s0, t0 = spawned, teleports
local far = { x = 3000, y = 0, z = 10 }
now = now + 3; B.update(1, far, now)
T.check(spawned == s0 and teleports == t0 + 1, 'a bus left behind is moved to the new bay (no new spawn)')

-- a teleport / a new anchor: all deleted; a missing model: no more tries
B.onTeleport()
T.check(B.status().buses == 0, 'teleport: all buses deleted')
B.reset()
core_vehicles.spawnNewVehicle = function() error('no such vehicle') end
B.onRows(1, '1:0_0', { { 100, 0, 10, 90, 2 } })
now = now + 3; B.update(1, player, now)
T.check(B.status().failed, 'the model could not be spawned: given up')
T.done('test_busparked')
