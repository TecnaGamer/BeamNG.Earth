-- luajit mod/tests/test_aquaplane.lua
-- Aquaplaning (the user, 2026-11-26: "yes, plus deep-water aquaplaning"): the vehicle side (the hydroplaning speed,
-- the grip factor, the water in a puddle spot, the tread nodes written at changes only, a deflated tyre left alone) and
-- the game side (the rain's film, the spots near the car in a stable order, the command, sent at changes only, cleared
-- when the water goes; another player's car never).
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local check, near = T.check, T.near
local A = dofile(dir .. '/../beamng_earth/lua/vehicle/extensions/beamngEarth/aquaplane.lua')
local MPH = 0.44704

-- the hydroplaning speed: Horne's 10.35 sqrt(psi) mph in deep water (35 psi = 241 kPa: 61 mph), higher in shallow
near(A.onsetSpeed(20, 241.3) / MPH, 10.35 * math.sqrt(35), 0.3, 'Horne at 35 psi')
near(A.onsetSpeed(50, 241.3), A.onsetSpeed(20, 241.3), 1e-9, 'deeper than 20 mm: the same')
check(A.onsetSpeed(2, 241) / MPH > 69 and A.onsetSpeed(2, 241) / MPH < 72, '2 mm: ~70 mph (Gallaway 74)')
check(A.onsetSpeed(20, 180) < A.onsetSpeed(20, 241), 'a soft tyre aquaplanes sooner')
-- the grip factor
check(A.gripFactor(30, 0.3, 241) == 1 and A.gripFactor(0, 15, 241) == 1, 'a thin film, or standing still: full grip')
near(A.gripFactor(A.onsetSpeed(15, 241), 15, 241), A.F_MIN, 1e-9, 'deep water at the onset speed: F_MIN')
near(A.gripFactor(80, 15, 241), A.F_MIN, 1e-9, 'beyond it: F_MIN')
local f30, f50 = A.gripFactor(30 * MPH, 15, 241), A.gripFactor(50 * MPH, 15, 241)
check(f30 > 0.7 and f30 < 0.85 and f50 > 0.3 and f50 < 0.5, string.format('15 mm: 30 mph %.2f, 50 mph %.2f', f30, f50))
check(A.gripFactor(50 * MPH, 1.0, 241) > 0.85, '1 mm (heavy rain) at 50 mph: a little')
check(A.quantise(0.83) == 0.85 and A.quantise(0.12) == 0.1, 'quantised')

-- a spot: a 6 x 2 m sag pool (row 0, column 0) along +x at (10, 0, 1)
local sp = { 10, 0, 1, 1, 0, 3, 1, 0 }
near(A.depthAt(sp, 1, 15, 10, 0, 1.1), 15, 1e-9, 'full, its middle: DMAX')
check(A.depthAt(sp, 1, 255, 10, 0, 1) == 0, 'empty')
check(A.depthAt(sp, 1, 15, 10, 0, 3) == 0, 'a road 2 m above it (a bridge): no water')
check(A.depthAt(sp, 1, 15, 12.95, 0, 1) == 0 and A.depthAt(sp, 1, 15, 10, 0.99, 1) == 0, 'past its rim: dry')
local half = A.depthAt(sp, 1, 15, 10 + 3 * 0.92 * 0.86 * 0.75, 0, 1)
check(half > 0 and half < 15, 'its slope: shallower (' .. half .. ')')
local fill = A.depthAt(sp, 1, 150, 10, 0, 1)
check(fill > 0 and fill < 15, 'half full: shallower in the middle')
check(A.depthAt({ 10, 0, 1, 1, 0, 3, 1, 8 }, 1, 15, 10, 0, 1) == 6, 'a rut (row 2): 6 mm')
check(A.waterAt(sp, 8, 15, 0.8, 0, 0, 1) == 0.8 and A.waterAt(sp, 8, 15, 0.8, 10, 0, 1) == 15, 'the film, or the pool')

-- runtime: a car with four tyres, two tread nodes each; the stock deflation's call
local writes = {}
local carPos, carVel = vec3(0, 0, 1.3), vec3(0, 0, 0)
local nodePos = {}
_G.obj = {
  getPosition = function() return carPos end,
  getVelocity = function() return carVel end,
  getNodePosition = function(_, id) return nodePos[id] end,
  getGroupPressure = function() return 241000 + 101325 end,
  setNodeFrictionSlidingCoefs = function(_, cid, a, b) writes[#writes + 1] = { cid, a, b } end,
}
_G.v = { data = { nodes = {} } }
_G.wheels = { wheels = {}, wheelCount = 4 }
for i = 0, 3 do
  local x, y = (i < 2) and 1.4 or -1.4, (i % 2 == 0) and 0.8 or -0.8
  local n1, n2, t1, t2 = 100 + i * 10, 101 + i * 10, 102 + i * 10, 103 + i * 10
  nodePos[n1], nodePos[n2] = vec3(x, y + 0.1, 0), vec3(x, y - 0.1, 0)
  v.data.nodes[t1] = { frictionCoef = 1.1, slidingFrictionCoef = 0.9 }
  v.data.nodes[t2] = { frictionCoef = 1.1 }
  wheels.wheels[i] = { node1 = n1, node2 = n2, radius = 0.3, treadNodes = { t1, t2 }, pressureGroupId = 1 }
end
A.updateGFX(0.016)
check(#writes == 0, 'nothing sent: nothing done')
-- a deep 12 x 2 m sag pool under the left tyres (x 1.4 and -1.4, y 0.8), the car at 50 mph
A.set(15, 0, { 0, 0.8, 1.0, 1, 0, 6, 1, 0 })
carVel = vec3(50 * MPH, 0, 0)
A.updateGFX(0.016)
local f = A.quantise(A.gripFactor(50 * MPH, 15, 241))
local byNode = {}
for _, w in ipairs(writes) do byNode[w[1]] = w end
check(byNode[102] and math.abs(byNode[102][2] - 1.1 * f) < 1e-9 and math.abs(byNode[102][3] - 0.9 * f) < 1e-9,
  'the left front tyre: its nodes scaled')
check(byNode[103] and math.abs(byNode[103][3] - 1.1 * f) < 1e-9, 'sliding = static when not given')
check(byNode[112] == nil and byNode[132] == nil, 'the right tyres: dry, not written')
local n = #writes
for _ = 1, 10 do A.updateGFX(0.016) end
check(#writes == n, 'steady: no more writes')
-- out of the pool: back to the jbeam's own values
carPos = vec3(20, 0, 1.3)
A.updateGFX(0.016)
byNode = {}
for k = n + 1, #writes do byNode[writes[k][1]] = writes[k] end
check(byNode[102] and byNode[102][2] == 1.1 and byNode[102][3] == 0.9, 'out of the water: stock again')
-- a deflated tyre keeps the deflation's values
carPos = vec3(0, 0, 1.3)
A.updateGFX(0.016)
wheels.wheels[0].isTireDeflated = true
n = #writes
A.set(255, 0, {})
for k = n + 1, #writes do check(writes[k][1] ~= 102 and writes[k][1] ~= 103, 'the deflated tyre not restored') end
check(A.status():find('active=false', 1, true) ~= nil, 'cleared: idle')
n = #writes
A.updateGFX(0.016)
check(#writes == n, 'idle: nothing per frame')
wheels.wheels[0].isTireDeflated = nil
-- heavy rain: the film everywhere (2 mm at 70 mph: every tyre)
A.set(255, 2.0, {})
carVel = vec3(70 * MPH, 0, 0)
A.updateGFX(0.016)
byNode = {}
for k = n + 1, #writes do byNode[writes[k][1]] = writes[k] end
check(byNode[102] and byNode[112] and byNode[122] and byNode[132] and byNode[102][2] < 1.1 * 0.6,
  'a 2 mm film at 70 mph: every tyre loses grip')
A.set(255, 0, {})

-- the game side
local G = require('beamngEarth/aquaplane')
check(G.filmMm(0) == 0 and G.filmMm(2.5) < 0.3, 'light rain: no film to speak of')
near(G.filmMm(10), 1.04, 0.05, '10 mm/h: ~1 mm (Gallaway)')
near(G.filmMm(25), 2.14, 0.05, '25 mm/h: ~2 mm')
local parts = {
  { 100, 0, 5, { 1, 0, 0.5, 1, 0, 2, 1, 4, 30, 0, 0.5, 1, 0, 2, 1, 4 } },
  { 0, 0, 0, { 10, 0, 0.2, 0, 1, 3, 1, 0 } },
}
local near1 = G.nearSpots(parts, 0, 0, 120, 96)
check(#near1 == 16 and near1[1] == 10 and near1[9] == 101 and near1[11] == 5.5, 'within reach, nearest first, world')
local near2 = G.nearSpots({ parts[2], parts[1] }, 0, 0, 120, 96)
local same = #near1 == #near2
for k = 1, #near1 do same = same and near1[k] == near2[k] end
check(same, 'the same list whatever the parts\' order')
check(#G.nearSpots(parts, 0, 0, 120, 1) == 8 and #G.nearSpots(parts, 0, 0, 50, 96) == 8, 'at most maxN; within reach')
local cmd = G.command(80, 1.25, near1)
check(cmd:find("extensions.load('beamngEarth_aquaplane')", 1, true) and cmd:find('set(80, 1.25, {10.00,0.00,0.20,0.0000,1.0000,3.00,1.00,0,', 1, true),
  'command')

-- runtime: the player's car, the roads' puddle parts
local sent = {}
local veh = { getID = function() return 7 end, getPosition = function() return vec3(0, 0, 0) end,
  getVelocity = function() return vec3(10, 0, 0) end, queueLuaCommand = function(_, c) sent[#sent + 1] = c end }
function getPlayerVehicle() return veh end
function getObjectByID(id) return id == 7 and veh or nil end
local po = S.newObject('TSStatic')
po:registerObject('puddles1')
po.getPosition = function() return vec3(0, 0, 0) end
po.isHidden = function() return false end
package.loaded['beamngEarth/roads'] = { puddleParts = function() return { { id = po:getID(), spots = { 10, 0, 0.2, 0, 1, 3, 1, 0 } } } end }
check(G.update(0.6, 255, 0, true) == false and #sent == 0, 'dry: nothing sent')
G.update(0.6, 80, 0, true)
check(#sent == 1 and sent[1]:find('set(80, 0.00, {10.00', 1, true), 'puddles shown: sent')
G.update(0.6, 80, 0, true)
G.update(0.6, 80, 0, true)
check(#sent == 1, 'unchanged: not again')
for _ = 1, 6 do G.update(0.6, 80, 0, true) end
check(#sent == 2, 'the same list again after RESEND_S (a reset car)')
G.update(0.6, 255, 0, true)
check(sent[#sent]:find('set(255, 0, {})', 1, true) and G.update(0.6, 255, 0, true) == false, 'dried: cleared, idle')
local c0 = #sent
G.update(0.6, 80, 0, false)
check(#sent == c0, 'the setting off: nothing')
MPVehicleGE = { isOwn = function() return false end }
G.update(0.6, 80, 0, true)
check(#sent == c0, 'another player\'s car (BeamMP): never')
MPVehicleGE = nil
-- the rain's film grows over FILM_TAU_S and is sent alone (no puddles)
for _ = 1, 400 do G.update(0.6, 255, 25, true) end
near(G.film(), G.filmMm(25), 0.05, 'the film follows the rain')
check(sent[#sent]:find('set(255, 2.1', 1, true), 'sent: the film alone')
G.reset()
check(sent[#sent]:find('set(255, 0, {})', 1, true) and G.film() == 0, 'reset: cleared')
package.loaded['beamngEarth/roads'] = nil

T.done('test_aquaplane')
