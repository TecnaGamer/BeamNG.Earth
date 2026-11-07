-- luajit mod/tests/test_camshift.lua  (camera state carried across a rebase without a swing)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/stubs.lua')
local geodesy = require('beamngEarth/geodesy')
local camshift = require('beamngEarth/camshift')

local a1 = geodesy.newAnchor(36.5785, -118.2923)
local lat, lon = a1:enuToGeodetic(10000, 0, 0)
local xf = a1:transformTo(geodesy.newAnchor(lat, lon))
local P = { 10020, 5, 1200 } -- player (old frame), driving east at 30 m/s

local function smoother(v) return setmetatable({ state = v, vel = 0 }, { __index = {} }) end
local function expSmoother(v) return setmetatable({ st = v }, { __index = {} }) end
local Cam = {} -- camera objects carry a metatable, like the game's camera modes
Cam.__index = Cam
local function cam(t) return setmetatable(t, Cam) end

-- orbit as in core/cameraModes/orbit.lua: world-space history + local/angle state
local shared = vec3(P[1] - 3, P[2], P[3] + 1)
local orbit = cam({
  camLastTargetPos = vec3(P[1], P[2], P[3] + 1), camLastTargetPos2 = vec3(P[1] - 1.5, P[2], P[3] + 1),
  camLastPos = vec3(P[1] - 6, P[2], P[3] + 2.5), camLastPos2 = shared, camLastPosPerp = vec3(P[1], P[2] + 2.4, P[3] + 1),
  preResetPos = vec3(1e300, 0, 0), camRot = vec3(0, -12, 0), camLastRot = vec3(0.1, 0.2, 0), camVel = vec3(30, 0, 0),
  orbitOffset = vec3(), targetCenter = vec3(0, 0, 0), camBase = vec3(0, 0.3, 1.2),
  dynamicPitchQuat = quat(0, 0, 0, 1), alias = shared, -- the same vec twice must move once
  collision = cam({ lastNearClipCenter = vec3(P[1] - 5.9, P[2], P[3] + 2.4), smoother = smoother(5) }),
})
-- chase: lastDataPos (velocity estimate) + direction smoothers (unit values: not positions)
local chase = cam({ lastDataPos = vec3(P[1], P[2], P[3]), dirSmoothX = smoother(1), dirSmoothY = smoother(0), dirSmoothZ = smoother(0) })
-- topDown: a world position held in three scalar smoothers
local topDown = cam({ posSmoothX = smoother(P[1] + 10), posSmoothY = smoother(P[2]), posSmoothZ = smoother(P[3]) })
-- external with a nested smooth filter (springs) and exponential triples
local external = cam({ camPos = vec3(P[1] + 40, P[2] + 20, P[3] + 3), smooth = cam({ posX = smoother(P[1] + 40), posY = smoother(P[2] + 20), posZ = smoother(P[3] + 3) }),
  autoX = expSmoother(P[1]), autoY = expSmoother(P[2]), autoZ = expSmoother(P[3]) })
-- free camera far from the car (must move anyway) + transition filter
local free = cam({ pos = vec3(P[1] - 4000, P[2] + 3000, P[3] + 500), rot = vec3(0.3, 0.1, 0), velocity = vec3(1, 2, 3) })
local transition = cam({ camLastPosAbs = vec3(P[1] - 6, P[2], P[3] + 2.5), camLastPosRel = vec3(-6, 0, 1.5) })
-- the 3D map's ground target, far from the car (the near-player walk would leave it behind)
local globe = cam({ target = vec3(P[1] + 20000, P[2] - 8000, 40), dist = 5000, heading = 0.4, pitch = 0.8,
  groundSmooth = 40, fov = 55 })

local camData = { pos = vec3(P[1], P[2], P[3]), prevPos = vec3(P[1] - 0.5, P[2], P[3]), vehPos = vec3(P[1], P[2], P[3]),
  prevVehPos = vec3(P[1] - 0.5, P[2], P[3]), vel = vec3(30, 0, 0) }
camData.resPos = vec3(P[1] - 6, P[2], P[3] + 2.5)
camData.res = { pos = camData.resPos }
local contexts = { main = {
  camData = camData, finalCameraData = { pos = vec3(P[1] - 6, P[2], P[3] + 2.5) }, lastValidData = { pos = vec3(P[1] - 6, P[2], P[3] + 2.5) },
  vehicleCamerasCache = { [1] = { focusedCamName = 'orbit', cameras = { orbit = orbit, chase = chase, topDown = topDown, external = external } } },
  globalCamerasCache = { free = free, transition = transition, earthGlobe = globe },
} }
package.loaded['core/cameraUpdate'] = { onPreRender = function() return contexts end }

local function copy(v) return { v.x, v.y, v.z } end
local function expect(v, old, tag)
  local x, y, z = xf:apply(old[1], old[2], old[3])
  T.near(v.x, x, 1e-6, tag .. ' x'); T.near(v.y, y, 1e-6, tag .. ' y'); T.near(v.z, z, 1e-6, tag .. ' z')
end
local function same(v, old, tag)
  T.check(v.x == old[1] and v.y == old[2] and v.z == old[3], tag .. ' unchanged')
end

local before = {}
for k, v in pairs(orbit) do if type(v) == 'table' and v.z then before['o.' .. k] = copy(v) end end
local oldLast2 = copy(orbit.camLastPos2)
local oldTarget = copy(orbit.camLastTargetPos)
local oldFree, oldCam, oldRes = copy(free.pos), copy(camData.pos), copy(camData.resPos)
local oldTop = { topDown.posSmoothX.state, topDown.posSmoothY.state, topDown.posSmoothZ.state }
local oldExp = { external.autoX.st, external.autoY.st, external.autoZ.st }

local stats = camshift.shift(xf, P[1], P[2], P[3])
T.check(stats.viaDebug and stats.contexts == 1, 'camera contexts found via cameraUpdate.onPreRender upvalue')

for _, k in ipairs({ 'camLastTargetPos', 'camLastTargetPos2', 'camLastPos', 'camLastPosPerp' }) do
  expect(orbit[k], before['o.' .. k], 'orbit.' .. k)
end
expect(orbit.camLastPos2, oldLast2, 'orbit.camLastPos2 (shared vec moved once)')
T.check(orbit.alias == orbit.camLastPos2, 'alias still the same vec')
for _, k in ipairs({ 'camRot', 'camLastRot', 'camVel', 'orbitOffset', 'targetCenter', 'camBase' }) do
  same(orbit[k], before['o.' .. k], 'orbit.' .. k)
end
T.check(orbit.preResetPos.x == 1e300, 'orbit.preResetPos sentinel untouched')
expect(orbit.collision.lastNearClipCenter, { P[1] - 5.9, P[2], P[3] + 2.4 }, 'collision filter (nested)')
T.check(orbit.collision.smoother.state == 5, 'collision distance smoother untouched')

-- the orbit aiming rule after the vehicle move: dir = targetPos_new - camLastPos2 must still point
-- along the direction of travel (+x turned by R), not back towards the old position
local tx, ty, tz = xf:apply(oldTarget[1], oldTarget[2], oldTarget[3])
local dx, dy = tx - orbit.camLastPos2.x, ty - orbit.camLastPos2.y
local fx, fy = xf:rotate(1, 0, 0)
T.check((dx * fx + dy * fy) / math.sqrt(dx * dx + dy * dy) > 0.9999, 'orbit still aims along the travel direction: camera stays behind')

expect(chase.lastDataPos, { P[1], P[2], P[3] }, 'chase.lastDataPos')
T.check(chase.dirSmoothX.state == 1 and chase.dirSmoothY.state == 0, 'chase direction smoothers untouched')
local x, y, z = xf:apply(oldTop[1], oldTop[2], oldTop[3])
T.near(topDown.posSmoothX.state, x, 1e-6, 'topDown smoother x'); T.near(topDown.posSmoothY.state, y, 1e-6, 'topDown smoother y')
T.near(topDown.posSmoothZ.state, z, 1e-6, 'topDown smoother z')
x, y, z = xf:apply(oldExp[1], oldExp[2], oldExp[3])
T.near(external.autoX.st, x, 1e-6, 'exponential smoother triple x')
expect(external.camPos, { P[1] + 40, P[2] + 20, P[3] + 3 }, 'external.camPos')
x = xf:apply(P[1] + 40, P[2] + 20, P[3] + 3)
T.near(external.smooth.posX.state, x, 1e-6, 'external.smooth spring x (nested filter)')
expect(free.pos, oldFree, 'free camera (4 km away) moved anyway')
same(free.rot, { 0.3, 0.1, 0 }, 'free.rot'); same(free.velocity, { 1, 2, 3 }, 'free.velocity')
expect(globe.target, { P[1] + 20000, P[2] - 8000, 40 }, '3D map target (20 km from the car) moved once')
T.check(globe.groundSmooth == nil and globe.dist == 5000 and globe.heading == 0.4 and globe.pitch == 0.8,
  '3D map: old ground smooth cleared, the orbit itself unchanged')
expect(transition.camLastPosAbs, { P[1] - 6, P[2], P[3] + 2.5 }, 'transition.camLastPosAbs')
same(transition.camLastPosRel, { -6, 0, 1.5 }, 'transition.camLastPosRel')
expect(camData.pos, oldCam, 'camData.pos'); expect(camData.vehPos, oldCam, 'camData.vehPos')
expect(camData.prevPos, { P[1] - 0.5, P[2], P[3] }, 'camData.prevPos')
same(camData.resPos, oldRes, 'camData.resPos (rebuilt each frame)')
same(camData.vel, { 30, 0, 0 }, 'camData.vel')
expect(contexts.main.finalCameraData.pos, { P[1] - 6, P[2], P[3] + 2.5 }, 'finalCameraData.pos')

-- objectTeleported() (common/utils.lua:1452) with the next frame's data now sees normal motion:
-- the vehicle moves 0.5 m in the new frame and camData.pos holds the mapped previous position
local nx, ny, nz = xf:apply(P[1] + 0.5, P[2], P[3])
local jump = math.sqrt((nx - camData.pos.x) ^ 2 + (ny - camData.pos.y) ^ 2 + (nz - camData.pos.z) ^ 2)
T.check(jump < 1.5, 'no teleport detected next frame (moved ' .. string.format('%.3f', jump) .. ' m < 1.5 m threshold)')

-- without the debug route the public accessors are used (main context only)
package.loaded['core/cameraUpdate'] = nil
local orbit2 = cam({ camLastPos2 = vec3(P[1] - 3, P[2], P[3] + 1) })
local free2 = cam({ pos = vec3(1, 2, 3) })
local globe2 = cam({ target = vec3(P[1] + 20000, P[2], 10), groundSmooth = 10, dist = 4000 })
core_camera = {
  getCameraDataById = function(id) return id == 1 and { orbit = orbit2 } or nil end,
  getGlobalCameras = function() return { free = free2, earthGlobe = globe2 } end,
}
getAllVehicles = function() return { { getID = function() return 1 end } } end
stats = camshift.shift(xf, P[1], P[2], P[3])
T.check(not stats.viaDebug, 'fallback path')
expect(orbit2.camLastPos2, { P[1] - 3, P[2], P[3] + 1 }, 'fallback: vehicle camera mapped')
expect(free2.pos, { 1, 2, 3 }, 'fallback: free camera mapped')
expect(globe2.target, { P[1] + 20000, P[2], 10 }, 'fallback: 3D map target mapped')
T.check(globe2.groundSmooth == nil and globe2.dist == 4000, 'fallback: 3D map ground smooth cleared')

T.done('test_camshift')
