-- BeamNG.Earth 3D map camera (the user, 2026-11-07: "a 3d map mode ... like Apple Maps or Google Earth ... use custom
-- camera movement"): an orbit around a point on the ground. The target is a world position on the ground; the camera
-- sits `dist` metres from it, looking down at `pitch` (radians below the horizon) from `heading` (radians, 0 = north,
-- clockwise). Inputs (the free camera's own): mouse / right stick rotate (yaw: heading, pitch: tilt), WASD / left stick
-- move the target over the ground (faster the higher the camera), zoom in / out and up / down change the distance
-- exponentially, so the wheel feels the same at street level and from orbit. beamngEarth/map3d.lua enters and leaves
-- it (the fly-up / fly-down paths) and owns the target's rebase when it travels far from the car.

local C = {}
C.__index = C

local DIST_MIN, DIST_MAX = 15, 600000          -- m (the sky's atmosphere breaks past ~600 km: sky memory)
local PITCH_MIN, PITCH_MAX = math.rad(12), math.rad(89.5)
local ZOOM_RATE = 2.2                          -- e-folds of distance per second at full zoom input
local PAN_RATE = 0.9                           -- target speed in distances per second at full stick
local ROT_RATE = 1.6                           -- rad / s at full key / stick rotation

local function clamp(x, a, b) return math.max(a, math.min(b, x)) end

function C:init()
  self.isGlobal = true
  self.hidden = true                           -- (not in the camera cycle: map3d enters it)
  self.target = vec3(0, 0, 0)
  self.dist = 1500
  self.heading = 0
  self.pitch = math.rad(55)
  self.fov = 55
  self.groundSmooth = nil
end

function C:reset() end
function C:setLevelProperties() end

-- data: {target = vec3, dist, heading, pitch, fov} (any may be missing: kept)
function C:setCustomData(data)
  data = data or {}
  if data.target then self.target = vec3(data.target) end
  if data.dist then self.dist = clamp(data.dist, DIST_MIN, DIST_MAX) end
  if data.heading then self.heading = data.heading end
  if data.pitch then self.pitch = clamp(data.pitch, PITCH_MIN, PITCH_MAX) end
  if data.fov then self.fov = data.fov end
  self.groundSmooth = nil
end

function C:onCameraChanged(focused) end

-- the direction the camera looks (unit), from heading and pitch
function C:lookDir()
  local cp = math.cos(self.pitch)
  return vec3(math.sin(self.heading) * cp, math.cos(self.heading) * cp, -math.sin(self.pitch))
end

-- the camera pose for the current orbit: position, rotation
function C:pose()
  local dir = self:lookDir()
  return self.target - dir * self.dist, quatFromDir(dir, vec3(0, 0, 1))
end

-- the ground under (x, y) (the drawn surface: terrain blocks, far tiles, roads), or nil
local function groundAt(x, y, above)
  local z = be:getSurfaceHeightBelow(vec3(x, y, above))
  if z and z > -1e8 then return z end
  return nil
end

function C:update(data)
  local dt = math.min(data.dtReal or data.dt or 0.016, 0.1)
  -- rotation (mouse deltas come per frame; keys and sticks per second)
  local dtFactor = math.max(dt * 200, 1e-3)
  self.heading = self.heading + (MoveManager.yawRelative / dtFactor) * 0.9
      + (MoveManager.yawRight - MoveManager.yawLeft) * ROT_RATE * dt
  self.pitch = clamp(self.pitch - (MoveManager.pitchRelative / dtFactor) * 0.9
      - (MoveManager.pitchUp - MoveManager.pitchDown) * ROT_RATE * dt, PITCH_MIN, PITCH_MAX)
  -- zoom (wheel, zoom keys, up / down)
  local z = (MoveManager.zoomOut - MoveManager.zoomIn) + (MoveManager.up - MoveManager.down) + MoveManager.absZAxis
  if z ~= 0 then self.dist = clamp(self.dist * math.exp(z * ZOOM_RATE * dt), DIST_MIN, DIST_MAX) end
  -- pan the target over the ground, in the heading's frame
  local mx = MoveManager.right - MoveManager.left + MoveManager.absXAxis
  local my = MoveManager.forward - MoveManager.backward + MoveManager.absYAxis
  if mx ~= 0 or my ~= 0 then
    local f = vec3(math.sin(self.heading), math.cos(self.heading), 0)
    local r = vec3(f.y, -f.x, 0)
    local speed = self.dist * PAN_RATE
    self.target = self.target + (f * my + r * mx) * speed * dt
  end
  -- keep the target on the ground (eased: a building's roof or a bridge does not jolt the view)
  local g = groundAt(self.target.x, self.target.y, self.target.z + self.dist + 2000)
  if g then
    self.groundSmooth = self.groundSmooth and (self.groundSmooth + (g - self.groundSmooth) * math.min(1, dt * 4)) or g
    self.target.z = self.groundSmooth
  end
  local pos, rot = self:pose()
  -- never under the ground (a low, flat view over a hill)
  local gc = groundAt(pos.x, pos.y, pos.z + 5000)
  if gc and pos.z < gc + 3 then pos.z = gc + 3 end
  data.res.pos:set(pos)
  data.res.rot:set(rot)
  data.res.fov = self.fov
  data.res.nearClip = clamp(self.dist * 0.002, 0.1, 200)
  return true
end

function C:onSerialize()
  return {target = {self.target.x, self.target.y, self.target.z}, dist = self.dist, heading = self.heading,
          pitch = self.pitch}
end

function C:onDeserialized(d)
  if type(d) ~= 'table' then return end
  if d.target then self.target = vec3(d.target[1], d.target[2], d.target[3]) end
  self.dist, self.heading, self.pitch = d.dist or self.dist, d.heading or self.heading, d.pitch or self.pitch
end

return function(...)
  local o = ... or {}
  setmetatable(o, C)
  o:init()
  return o
end
