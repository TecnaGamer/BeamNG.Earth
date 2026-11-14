-- luajit mod/tests/test_parking.lua  (parked cars: the road tiles' spots handed to the stock parking system)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')

-- engine stubs: vectors, a minimal sites object as gameplay/sites/sites.lua builds it, gameplay_parking
function vec3(x, y, z) return { x = x, y = y, z = z } end
function quatFromDir(d) return { d = d } end
function log() end
table.clear = table.clear or function(t) for k in pairs(t) do t[k] = nil end end
local uid = 0
package.preload['/lua/ge/extensions/gameplay/sites/sites'] = function()
  return function()
    local s = { finalized = 0 }
    s.parkingSpots = { objects = {}, sorted = {} }
    function s.parkingSpots.objectConstructor()
      uid = uid + 1
      local ps = { id = uid, customFields = { tags = {} } }
      function ps:set(pos, rot, scl) self.pos, self.rot, self.scl = pos, rot, scl end
      function ps.customFields:addTag(t) self.tags[t] = true end
      return ps
    end
    function s.parkingSpots:sort()
      self.sorted = {}
      for _, o in pairs(self.objects) do self.sorted[#self.sorted + 1] = o end
    end
    function s:finalizeSites() self.finalized = self.finalized + 1 end
    return s
  end
end
local setupCalls, scatterCalls, active, given, lastAmount = 0, 0, false, nil, nil
gameplay_parking = {
  setSites = function(s) given = s end,
  getState = function() return active end,
  setupVehicles = function(amount) setupCalls = setupCalls + 1; active = true; lastAmount = amount end,
  scatterParkedCars = function() scatterCalls = scatterCalls + 1 end,
  getParkedCarsList = function() return {} end,
}
settings = { getValue = function(k) return k == 'trafficParkedAmount' and 16 or nil end }
local clock = 0
os.clock = function() return clock end

local P = require('beamngEarth/parking')
local NG = require('beamngEarth/navgraph')
NG.takeChoice({ trafficMode = 'disabled', trafficParked = 'enabled' })   -- the player chose parked cars
local player = { x = 0, y = 0, z = 0 }

local function rows(n, x0, y)
  local r = {}
  for i = 1, n do r[i] = { x0 + i * 6.5, y, 1.0, 90 } end
  return r
end

T.check(P.spotKey({ 1.04, -2.0, 0, 0 }) == '1.0_-2.0', 'spot key: position to 0.1 m')

-- spots within KEEP_M go to the sites; the cars start once START_MIN are near
P.onRows(1, '1:0_0', rows(30, 0, 10))
P.onRows(1, '1:9_9', rows(5, 5000, 0))            -- far away: not in the sites
P.update(1, player)
T.check(given and #given.parkingSpots.sorted == 30, 'the 30 near spots are in the sites')
T.check(given.finalized == 1, 'quadtree built')
local s1 = given.parkingSpots.sorted[1]
T.check(s1.scl.x == 2.5 and s1.scl.y == 6.0 and s1.customFields.tags.street and s1.customFields.tags.forwards,
  'stock spot size, street + forwards tags')
T.check(setupCalls == 1, 'parked cars started (the player chose them)')
P.onRows(1, '1:0_1', { { 50, 50, 1.0, 0, 1, 2.2, 5.0 } })
clock = clock + 3
P.update(1, player)
local lot
for _, ps in pairs(given.parkingSpots.objects) do if ps.pos.x == 50 then lot = ps end end
T.check(lot and not lot.customFields.tags.street and not lot.customFields.tags.forwards, 'a lot stall: no street / forwards tags')
T.check(lot and lot.scl.x == 2.2 and lot.scl.y == 5.0, 'mapped spot keeps its own dimensions')
P.dropKey('1:0_1')
clock = clock + 3
P.update(1, player)

-- rebuilds keep the same spot objects (a car's spot stays its spot)
local ids = {}
for id in pairs(given.parkingSpots.objects) do ids[id] = true end
P.onRows(1, '1:1_0', rows(3, 300, 10))
clock = clock + 1
P.update(1, player)
T.check(#given.parkingSpots.sorted == 30, 'no rebuild within REBUILD_S')
clock = clock + 2
P.update(1, player)
T.check(#given.parkingSpots.sorted == 33, 'rebuilt with the new tile')
local same = 0
for id in pairs(given.parkingSpots.objects) do if ids[id] then same = same + 1 end end
T.check(same == 30, 'the earlier spots kept their objects')

-- a dropped tile's spots go, except one a car stands on
local occupied
for _, ps in pairs(given.parkingSpots.objects) do
  if ps.pos.x > 300 then occupied = ps; ps.vehicle = 7; break end
end
P.dropKey('1:1_0')
clock = clock + 3
P.update(1, player)
T.check(#given.parkingSpots.sorted == 31 and given.parkingSpots.objects[occupied.id] == occupied,
  'dropped tile: its spots go but the occupied one stays')
T.check(setupCalls == 1, 'started once')

-- teleport: scattered onto the new place's spots once there are enough
P.onTeleport()
P.onRows(2, '2:0_0', rows(40, 0, -10))
clock = clock + 3
P.update(2, player)
T.check(#given.parkingSpots.sorted == 40, 'a new anchor: only its spots')
T.check(scatterCalls == 1, 'parked cars scattered onto them')

-- the block: spots EDGE_M inside its ground box only
T.check(P.inside({ 0, 0, 100, 100 }, 50, 50, 10) and not P.inside({ 0, 0, 100, 100 }, 95, 50, 10) and P.inside(nil, 1e9, 0, 10),
  'inside the box shrunk by m (no box: anywhere)')
P.update(2, player, { -100, -1000, 200 + P.EDGE_M, 1000 })
clock = clock + 3
P.update(2, player, { -100, -1000, 200 + P.EDGE_M, 1000 })
local maxX = -1e9
for _, ps in pairs(given.parkingSpots.objects) do maxX = math.max(maxX, ps.pos.x) end
T.check(maxX <= 200.01 and #given.parkingSpots.sorted == 30, 'spots near the block edge left out')

P.reset()
T.check(P.status().spots == 0 and not P.status().started, 'reset')

-- at most MAX_SPOTS, the nearest
P.MAX_SPOTS = 10
active = true
P.onRows(3, '3:0_0', rows(30, 0, 10))
clock = clock + 3
P.update(3, player)
local far = 0
for _, ps in pairs(given.parkingSpots.objects) do far = math.max(far, ps.pos.x) end
T.check(#given.parkingSpots.sorted == 10 and far <= 10 * 6.5 + 0.01, 'the MAX_SPOTS nearest spots only')
P.MAX_SPOTS = 800
P.reset()

-- parked cars off: the sites are not kept up; turning them on (the stock setupVehicles: the radial menu) builds them
-- first
NG.takeChoice({ trafficMode = 'disabled', trafficParked = 'disabled' })
active, given = false, nil
local calls0 = setupCalls
P.onRows(4, '4:0_0', rows(30, 0, 10))
clock = clock + 3
P.update(4, player)
T.check(given and #given.parkingSpots.sorted == 30, 'parked cars off: the sites built once')
P.onRows(4, '4:1_0', rows(3, 300, 10))
clock = clock + 3
P.update(4, player)
T.check(#given.parkingSpots.sorted == 30, 'parked cars off: not rebuilt every REBUILD_S')
gameplay_parking.setupVehicles(8)
T.check(#given.parkingSpots.sorted == 33 and setupCalls == calls0 + 1, 'turned on: the sites rebuilt first, then the cars')
P.reset()
NG.takeChoice({ trafficMode = 'disabled', trafficParked = 'enabled' })
active = false
P.onRows(5, '5:0_0', { { 50, 50, 1.0, 0, 1, 2.2, 5.0 } })
clock = clock + 3
P.update(5, player)
T.check(active and lastAmount == 1, 'a mapped village driveway can start a small parked fleet')
P.reset()
local P0 = { x = 0, y = 0 }
T.check(not P.offSpot({ x = 10, y = 0 }, nil, P0), 'near the player: never moved (one it pushed stays in view)')
T.check(P.offSpot({ x = 100, y = 0 }, nil, P0) and P.offSpot({ x = 100, y = 0 }, { missing = true }, P0),
  'far, no spot (or another frame\'s): moved')
T.check(not P.offSpot({ x = 100, y = 0 }, { pos = { x = 101, y = 1 } }, P0), 'on its spot: stays')
T.check(P.offSpot({ x = 100, y = 0 }, { pos = { x = 110, y = 0 } }, P0), 'off its spot: moved')

-- left behind: far in any direction, or HIDE_BEHIND_M behind a moving player only
local mv = { x = 0, y = 0, vx = 20, vy = 0 }
T.check(P.leftBehind({ x = -200, y = 0 }, mv) and not P.leftBehind({ x = 200, y = 0 }, mv),
  'behind a moving player: left behind; the same distance ahead: not')
T.check(not P.leftBehind({ x = -100, y = 0 }, mv), 'just behind (under HIDE_BEHIND_M): not')
T.check(not P.leftBehind({ x = -200, y = 0 }, P0) and P.leftBehind({ x = 700, y = 0 }, P0),
  'standing: only HIDE_M away counts')
T.check(not P.leftBehind({ x = -60, y = 200 }, mv), 'far but almost beside the player (under 120 degrees off): not')
T.check(P.moving(mv) and not P.moving(P0) and not P.moving({ x = 0, y = 0, vx = 1, vy = 1 }), 'moving: HIDE_MIN_SPEED')

-- a moving player: the sites rebuilt every REBUILD_MOVE_M, not every REBUILD_S
P.reset()
active, given = true, nil
P.onRows(6, '6:0_0', rows(30, 0, 10))
clock = clock + 3
P.update(6, { x = 0, y = 0, z = 0, vx = 20, vy = 0 })
T.check(given and #given.parkingSpots.sorted == 30, 'moving: first build at once')
P.onRows(6, '6:1_0', rows(3, 300, 10))
clock = clock + 3
P.update(6, { x = 50, y = 0, z = 0, vx = 20, vy = 0 })
T.check(#given.parkingSpots.sorted == 30, 'moving 50 m: not rebuilt yet')
clock = clock + 3
P.update(6, { x = 120, y = 0, z = 0, vx = 20, vy = 0 })
T.check(#given.parkingSpots.sorted == 33, 'moving 120 m: rebuilt')
P.onRows(6, '6:2_0', rows(2, 400, 10))
P.update(6, { x = 130, y = 0, z = 0, vx = 20, vy = 0 }, { -2000, -2000, 2000, 2000 })
clock = clock + 3
P.update(6, { x = 140, y = 0, z = 0, vx = 20, vy = 0 }, { -2000, -2000, 2000, 2000 })
T.check(#given.parkingSpots.sorted == 35, 'a new block: rebuilt at once')

-- the camera view: its field of view (at VIEW_ASPECT) plus VIEW_MARGIN_DEG
local camView = { pos = { x = -5, y = 0, z = 2 }, fwd = { x = 1, y = 0, z = 0 }, fov = 60 }
core_camera = {
  getPosition = function() return camView.pos end,
  getForward = function() return camView.fwd end,
  getFovDeg = function() return camView.fov end,
}
local v0 = P.cameraView()
T.check(v0 and P.inView({ x = 100, y = 10, z = 0 }, v0) and not P.inView({ x = -250, y = 10, z = 0 }, v0),
  'forward camera: ahead in view, behind not')
T.check(P.inView({ x = 50, y = 60, z = 0 }, v0), 'inside the field of view plus the margin: in view')
T.check(P.inView({ x = 0, y = 0, z = 0 }, nil), 'no camera known: in view (nothing hides)')

-- the guard: a car left behind goes inactive (out of the camera's view only); the stock pool taking it back clears it;
-- with every car hidden one comes back on a free spot UNHIDE_MIN_M .. START_M away
local cars = {}
local function car(id, x, y)
  local c = { id = id, pos = { x = x, y = y, z = 0 }, active = 1 }
  function c:getPosition() return self.pos end
  function c:getVelocity() return { x = 0, y = 0, z = 0 } end
  function c:getActive() return self.active == 1 end
  function c:setActive(a) self.active = a end
  cars[id] = c
  return c
end
function getObjectByID(id) return cars[id] end
local teleported, amountSet, vars = {}, nil, { activeAmount = math.huge }
gameplay_parking.getParkedCarsList = function() return { 11, 12 } end
gameplay_parking.getParkedCarsData = function() return {} end
gameplay_parking.forceTeleport = function(id, pos, minD, maxD) teleported[#teleported + 1] = { id, minD, maxD } end
gameplay_parking.getParkingVars = function() return vars end
gameplay_parking.setActiveAmount = function(n) amountSet, vars.activeAmount = n, n end
package.loaded['beamngEarth/settings'] = { get = function(k) return k == 'quality' and 'high' or nil end }
local function step(dt)
  clock = clock + (dt or 1)
  P.update(6, { x = 0, y = 0, z = 0, vx = 20, vy = 0 }, { -2000, -2000, 2000, 2000 })
end
car(11, 100, 10)    -- ahead
car(12, -250, 10)   -- left behind
camView.fwd = { x = -1, y = 0, z = 0 }                       -- a rear-facing camera: it sees the car
step()
T.check(cars[12].active == 1, 'rear camera: the car left behind is in view, stays')
camView.fwd = { x = -0.7, y = 0.7, z = 0 }                   -- an orbit camera looking back over the shoulder
step()
T.check(cars[12].active == 1, 'orbit camera looking back: stays')
camView.fwd = { x = 0, y = 1, z = 0 }                        -- an orbit camera looking to the side
step()
T.check(cars[12].active == 0, 'orbit camera looking away from it: hidden')
cars[12].active = 1                                          -- (back: as if the stock pool took it)
camView.fwd = { x = 1, y = 0, z = 0 }
step()
T.check(cars[12].active == 1, 'back, held HIDE_HOLD_S')
step(P.HIDE_HOLD_S)
T.check(cars[11].active == 1 and cars[12].active == 0, 'forward camera: the car left behind goes inactive, the one ahead stays')
T.check(amountSet == nil, 'quality high: no cap')
step()
T.check(cars[12].active == 0 and #teleported == 0, 'another parked car is out: the hidden one waits for the stock pool')
cars[12].active, cars[12].pos = 1, { x = 300, y = 10, z = 0 }    -- the stock pool moved it to a spot ahead
step()
T.check(cars[12].active == 1, 'taken back by the stock pool: active')
cars[11].pos, cars[12].pos = { x = -300, y = 10, z = 0 }, { x = -320, y = 10, z = 0 }
step()
T.check(cars[11].active == 0 and cars[12].active == 1, 'a car that just came back stays HIDE_HOLD_S')
step(P.HIDE_HOLD_S)
T.check(cars[12].active == 0, 'every car hidden')
step()
T.check(#teleported == 1 and teleported[1][2] == P.UNHIDE_MIN_M and teleported[1][3] == P.START_M,
  'none out: one back on a free spot UNHIDE_MIN_M .. START_M away')
local back = (cars[11].active == 1 and 1 or 0) + (cars[12].active == 1 and 1 or 0)
T.check(back == 1, 'one at a time')

-- the quality cap: only on the game's default parked-car amount, and only while the active amount is still ours
local function quality(q) package.loaded['beamngEarth/settings'] = { get = function(k) return k == 'quality' and q or nil end } end
T.check(P.amountIsDefault(function() return 5 end, 5, { parkedAmount = -1 }), 'the option at its default, auto at start: default')
T.check(not P.amountIsDefault(function() return 8 end, 5, { parkedAmount = -1 }), 'the option set by the player: not')
T.check(not P.amountIsDefault(function() return 0 end, 5, nil), 'the option on auto (0, a choice): not')
T.check(not P.amountIsDefault(function() return 5 end, 5, { parkedAmount = 3 }), 'an amount picked at level start: not')
T.check(P.amountIsDefault(function() return nil end, nil, nil), 'unreadable: default')
quality('medium')
step()
T.check(amountSet == P.ACTIVE_CAP.medium, 'quality medium: the active amount capped')
amountSet = nil
quality('ultra')
step()
T.check(amountSet == math.huge, 'a higher preset while the cap is ours: the amount back')
quality('medium')
step()
T.check(vars.activeAmount == P.ACTIVE_CAP.medium, 'medium again: capped')
vars.activeAmount, amountSet = 1, nil                             -- the player picks 1 (the radial menu, a mission)
step()
T.check(amountSet == nil, 'a lower amount set by someone else stays')
quality('high')
step()
T.check(amountSet == nil and vars.activeAmount == 1, 'a higher preset later: their newer amount kept, not the old one')
vars.activeAmount = math.huge
quality('medium')
step()
T.check(vars.activeAmount == math.huge, 'medium again after an outside change: the cap stays suspended')
gameplay_parking.setupVehicles(5)                                 -- a new fleet: the cap may apply again
vars.activeAmount = math.huge
step()
T.check(vars.activeAmount == P.ACTIVE_CAP.medium, 'a new fleet: capped again')
vars.activeAmount, amountSet = 4, nil                             -- a mission / another mod: above the cap
step()
step()
T.check(amountSet == nil and vars.activeAmount == 4, 'an outside amount above the cap: kept, not capped again')
quality('high')
step()
quality('medium')
step()
T.check(amountSet == nil and vars.activeAmount == 4, '... also across preset changes, until a new fleet')
gameplay_parking.setupVehicles(5)
vars.activeAmount = math.huge
step()
T.check(vars.activeAmount == P.ACTIVE_CAP.medium, 'capped again')
gameplay_parking.setupVehicles(5)                                 -- a new fleet (hooked: the stock setup)
T.check(vars.activeAmount == math.huge, 'a new fleet: the capped amount goes back before the stock setup')
step()
T.check(vars.activeAmount == P.ACTIVE_CAP.medium, '... and the cap is worked out again for it')
quality('ultra')
step()
T.check(vars.activeAmount == math.huge, '... and given back from the new fleet\'s amount')
local gv = settings.getValue
settings.getValue = function(k) return k == 'trafficParkingAmount' and 8 or nil end
settings.impl = { defaultValues = { trafficParkingAmount = 5 } }
quality('medium')
amountSet = nil
step()
T.check(amountSet == nil and vars.activeAmount == math.huge, 'the player\'s own parked-car amount: no cap on medium')
settings.getValue, settings.impl = gv, nil
P.reset()
core_camera = nil
T.done('test_parking')
