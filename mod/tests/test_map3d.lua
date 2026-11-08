-- luajit mod/tests/test_map3d.lua  (the 3D map's orbit camera: pose, zoom / pan / rotate input, limits, ground)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/stubs.lua')

local ground = 100
be = { getSurfaceHeightBelow = function(_) return ground end }
MoveManager = { yawRelative = 0, pitchRelative = 0, yawLeft = 0, yawRight = 0, pitchUp = 0, pitchDown = 0,
  zoomIn = 0, zoomOut = 0, up = 0, down = 0, absZAxis = 0, left = 0, right = 0, forward = 0, backward = 0,
  absXAxis = 0, absYAxis = 0 }
local function idle() for k in pairs(MoveManager) do MoveManager[k] = 0 end end

local make = require('core/cameraModes/earthGlobe')
local cam = make()
T.check(cam.isGlobal and cam.hidden, 'a global camera, not in the camera cycle')

local function frame(dt)
  local res = { pos = vec3(), rot = quat(), fov = 0 }
  function res.rot:set(q) for k, v in pairs(q) do self[k] = v end end
  cam:update({ dt = dt or 0.02, dtReal = dt or 0.02, res = res })
  return res
end

-- pose: the camera dist from the target, along the look direction (heading 0 = north: the camera south of it)
cam:setCustomData({ target = vec3(10, 20, 0), dist = 1000, heading = 0, pitch = math.rad(45) })
local r = frame()
T.check(math.abs((r.pos - cam.target):length() - 1000) < 1e-6, 'the camera sits dist from the target: '
  .. (r.pos - cam.target):length())
T.check(r.pos.y < cam.target.y and math.abs(r.pos.x - 10) < 1e-6, 'heading 0 looks north: the camera south of it')
T.check(math.abs(cam.target.z - ground) < 1e-6, 'the target is put on the ground')
T.check(r.pos.z > ground, 'the camera above the ground')

-- zoom in: exponential, the same feel at any height; never below the minimum
MoveManager.zoomIn = 1
local d0 = cam.dist
frame(0.5)
T.check(cam.dist < d0 and cam.dist > d0 * 0.2, 'zoom in shortens the distance by a factor: ' .. cam.dist)
for _ = 1, 200 do frame(0.1) end
T.check(cam.dist >= 15, 'never closer than the minimum: ' .. cam.dist)
idle()
MoveManager.zoomOut = 1
for _ = 1, 400 do frame(0.1) end
T.check(cam.dist <= 600000, 'never farther than the maximum: ' .. cam.dist)
idle()

-- pan: forward moves the target along the heading, faster the higher the camera
cam:setCustomData({ target = vec3(0, 0, 0), dist = 1000, heading = math.pi / 2, pitch = math.rad(50) })
MoveManager.forward = 1
frame(0.1)
local moved1 = cam.target.x
T.check(moved1 > 0 and math.abs(cam.target.y) < 1e-6, 'forward at heading 90 degrees moves the target east')
cam:setCustomData({ target = vec3(0, 0, 0), dist = 10000 })
frame(0.1)
T.check(cam.target.x > moved1 * 5, 'ten times higher pans about ten times faster')
idle()

-- pitch stays between the limits (no flipping over the top, not under the horizon)
MoveManager.pitchUp = 1
for _ = 1, 200 do frame(0.1) end
T.check(cam.pitch >= math.rad(12) - 1e-9, 'pitch floor')
idle()
MoveManager.pitchDown = 1
for _ = 1, 200 do frame(0.1) end
T.check(cam.pitch <= math.rad(89.5) + 1e-9, 'pitch ceiling')
idle()

-- a hill between the camera and nothing: the camera is kept above the ground under it
ground = 5000
cam:setCustomData({ target = vec3(0, 0, 0), dist = 50, pitch = math.rad(13) })
local r2 = frame()
T.check(r2.pos.z >= 5000 + 3 - 1e-6, 'never under the ground: ' .. r2.pos.z)

T.done('test_map3d')
