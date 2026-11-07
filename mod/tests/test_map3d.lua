-- luajit mod/tests/test_map3d.lua  (the 3D map's orbit camera: pose in the target's local frame, zoom / move / rotate
-- input, limits, the ground height check)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/stubs.lua')

local groundZ = 100
be = { getSurfaceHeightBelow = function(_) return groundZ end }
MoveManager = { yawRelative = 0, pitchRelative = 0, yawLeft = 0, yawRight = 0, pitchUp = 0, pitchDown = 0,
  zoomIn = 0, zoomOut = 0, up = 0, down = 0, absZAxis = 0, left = 0, right = 0, forward = 0, backward = 0,
  absXAxis = 0, absYAxis = 0 }
local function idle() for k in pairs(MoveManager) do MoveManager[k] = 0 end end

-- a flat frame round (0, 0): x east, y north, z up (the anchor's own frame near it)
local K = 111320
local function flat(lat, lon, h) return vec3(lon * K, lat * K, h) end
-- a curved frame: the ground drops d^2 / 2R away from the origin (the earth's curve in an anchor's frame)
local R = 6371000
local function curved(lat, lon, h)
  local x, y = lon * K, lat * K
  local d2 = x * x + y * y
  local up = vec3(x / R, y / R, 1):normalized()
  return vec3(x, y, -d2 / (2 * R)) + up * h
end

local make = require('core/cameraModes/earthGlobe')
local cam = make()
cam.frame = flat
T.check(cam.isGlobal and cam.hidden, 'a global camera, not in the camera cycle')

local function frame(dt)
  local res = { pos = vec3(), rot = quat(), fov = 0 }
  function res.rot:set(q) for k, v in pairs(q) do self[k] = v end end
  cam:update({ dt = dt or 0.02, dtReal = dt or 0.02, res = res })
  return res
end

-- pose: the camera dist from the target, heading 0 looks north (the camera south of it)
cam:setCustomData({ lat = 0, lon = 0, ground = 0, dist = 1000, heading = 0, pitch = math.rad(45) })
local r = frame()
T.check(math.abs((r.pos - cam.target):length() - 1000) < 1e-6, 'the camera sits dist from the target')
T.check(r.pos.y < cam.target.y and math.abs(r.pos.x) < 1e-6, 'heading 0 looks north: the camera south of it')
for _ = 1, 100 do frame(0.05) end
T.check(math.abs(cam.ground - 100) < 1, 'the target eases to the ground height: ' .. cam.ground)

-- a bad ground hit (2026-11-07: 4e17 m from orbit) is ignored
groundZ = 4e17
local g0 = cam.ground
frame()
T.check(cam.ground == g0, 'a ground height out of range is not taken')
groundZ = 100

-- zoom: exponential, between the limits
MoveManager.zoomIn = 1
local d0 = cam.dist
frame(0.5)
T.check(cam.dist < d0 and cam.dist > d0 * 0.2, 'zoom in shortens the distance by a factor: ' .. cam.dist)
for _ = 1, 200 do frame(0.1) end
T.check(cam.dist >= cam.DIST_MIN, 'never closer than the minimum')
idle()
MoveManager.zoomOut = 1
for _ = 1, 600 do frame(0.1) end
T.check(cam.dist <= cam.DIST_MAX, 'never farther than the maximum')
idle()

-- move: forward at heading 90 degrees moves the target east (its longitude), faster the higher the camera
cam:setCustomData({ lat = 0, lon = 0, dist = 1000, heading = math.pi / 2, pitch = math.rad(50) })
MoveManager.forward = 1
frame(0.1)
local moved1 = cam.lon
T.check(moved1 > 0 and math.abs(cam.lat) < 1e-9, 'forward at heading 90 degrees moves the target east')
cam:setCustomData({ lat = 0, lon = 0, dist = 10000 })
frame(0.1)
T.check(cam.lon > moved1 * 5, 'ten times higher moves about ten times faster')
idle()
cam:move(0, 0)
cam:setCustomData({ lat = 89.95 })
cam:move(100000, 0)
T.check(cam.lat <= 89.9 + 1e-9, 'never past the pole')

-- pitch limits
MoveManager.pitchUp = 1
for _ = 1, 200 do frame(0.1) end
T.check(cam.pitch >= math.rad(12) - 1e-9, 'pitch floor')
idle()
MoveManager.pitchDown = 1
for _ = 1, 200 do frame(0.1) end
T.check(cam.pitch <= math.rad(89.5) + 1e-9, 'pitch ceiling')
idle()

-- the curved earth: 500 km from the origin the camera looks down along the local up there, not world z
cam.frame = curved
groundZ = -1e9                     -- (no geometry: the ground height keeps its value)
cam:setCustomData({ lat = 0, lon = 500000 / K, ground = 0, dist = 2000, heading = 0, pitch = math.rad(89.5) })
local r2 = frame()
local p = curved(0, 500000 / K, 0)
local up = (curved(0, 500000 / K, 1000) - p):normalized()
local off = (r2.pos - p):normalized()
T.check(off:dot(up) > 0.999, 'from straight above, the camera is along the local up of a far target: ' .. off:dot(up))
T.check(p.z < -19000, 'the far target lies on the curve, ~20 km below the anchor plane: ' .. p.z)

T.done('test_map3d')
