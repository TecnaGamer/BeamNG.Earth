-- BeamNG.Earth 3D map camera (the user, 2026-11-07: "a 3d map mode ... like Apple Maps or Google Earth ... use custom
-- camera movement"): an orbit around a place on the earth. The target is a latitude / longitude (on the ground there);
-- the camera sits `dist` metres from it, looking down at `pitch` (radians below the local horizon) from `heading`
-- (radians, 0 = north, clockwise), in the target's own local frame: its up is the earth's normal there, so the view
-- follows the curve of the earth as the target travels far from the car (the user: "as i move the camera farther away
-- from the car it dosnt follow the earth"; a flat x / y target drifted off into space over the curved ground).
-- Positions come from the active anchor's frame (beamngEarth_core: geodeticToEnu, the earth's curve in it), so a
-- rebase changes nothing here. Inputs (the free camera's own, when the 3D map screen is not taking them; map3d.lua's
-- drag / zoom otherwise): rotate (yaw: heading, pitch: tilt), move (the target over the ground, faster the higher the
-- camera), zoom (distance, exponential: the same feel at street level and from orbit).

local C = {}
C.__index = C

-- (the whole earth, as map apps: the user, 2026-11-07, "so i can zoom out as far as the apple map screenshots": the
-- real camera all the way up, the game's sky shader patched for a camera above the air, launcher skypatch.py)
C.DIST_MIN, C.DIST_MAX = 15, 20000000
local PITCH_MIN, PITCH_MAX = math.rad(12), math.rad(89.5)
local ZOOM_RATE = 2.2                          -- e-folds of distance per second at full zoom input
local PAN_RATE = 0.9                           -- target speed in distances per second at full stick
local ROT_RATE = 1.6                           -- rad / s at full key / stick rotation
local M_PER_DEG = 111320
local GROUND_MIN, GROUND_MAX = -500, 9000      -- m: a ground height outside this is a bad hit (2026-11-07: 4e17 m)

local function clamp(x, a, b) return math.max(a, math.min(b, x)) end

-- The tilt allowed at a distance (the user, 2026-11-07: from high up "tilt/turn goes weird": a low tilt from far out
-- orbited the target from the side and swung the view off the earth): any tilt within TILT_FREE_M, straight down
-- from TILT_TOP_M, smoothly (in log distance) between, as map apps do.
C.TILT_FREE_M = 30000
C.TILT_TOP_M = 3000000
function C.pitchMin(dist)
  local a, b = math.log(C.TILT_FREE_M), math.log(C.TILT_TOP_M)
  local t = clamp((math.log(math.max(1, dist)) - a) / (b - a), 0, 1)
  t = t * t * (3 - 2 * t)
  return PITCH_MIN + (math.rad(88) - PITCH_MIN) * t
end

-- Far out (the whole earth): the game's sky draws every object black with the camera above its shell (600 km), so
-- past MINI_ALT the real camera stays MINI_CAM_ALT up, on the line from the earth's centre to where the view really
-- is, and globe.lua draws the server's small copy of the globe (C.miniScale of the earth, ~30 km across) in front of
-- it, s x the real centre distance away: the same angle as the earth from the real place.
-- self.mini = {s, O (the earth's centre), Om (the copy's centre)} then; nil otherwise.
C.MINI_ALT = math.huge          -- (off: globe.lua's stand-in; was 480 km)
C.MINI_CAM_ALT = 300000
C.R = 6371000
C.miniScale = 30000 / 6368000      -- (globe.lua sets the server's own: globe.py's miniScale)

local function earthCentre()
  local core = rawget(_G, 'beamngEarth_core')
  local a = core and core._state and core._state().anchor
  if not a or not a.ecefToEnu then return nil end
  local x, y, z = a:ecefToEnu(0, 0, 0)
  return vec3(x, y, z)
end

-- the frame: (lat, lon, h) -> world vec3, from the active anchor; nil without one (a test sets C.frame)
local function defaultFrame()
  local core = rawget(_G, 'beamngEarth_core')
  local a = core and core._state and core._state().anchor
  if not a then return nil end
  return function(lat, lon, h)
    local x, y, z = a:geodeticToEnu(lat, lon, h)
    return vec3(x, y, z)
  end
end

function C:init()
  self.isGlobal = true
  self.hidden = true                           -- (not in the camera cycle: map3d enters it)
  self.lat, self.lon = 0, 0
  self.ground = 0                              -- m above the ellipsoid at the target (eased)
  self.target = vec3(0, 0, 0)                  -- the target's world position (kept for map3d / core: tx, ty)
  self.dist = 1500
  self.heading = 0
  self.pitch = math.rad(55)
  self.fov = 55
  self.frame = nil                             -- (tests: a fixed frame function)
end

function C:reset() end
function C:setLevelProperties() end

-- data: {lat, lon, dist, heading, pitch, fov, ground} (any may be missing: kept)
function C:setCustomData(data)
  data = data or {}
  if data.lat and data.lon then self.lat, self.lon = data.lat, data.lon end
  if data.ground then self.ground = data.ground end
  if data.dist then self.dist = clamp(data.dist, C.DIST_MIN, C.DIST_MAX) end
  if data.heading then self.heading = data.heading end
  if data.pitch then self.pitch = clamp(data.pitch, PITCH_MIN, PITCH_MAX) end
  if data.fov then self.fov = data.fov end
end

function C:onCameraChanged(focused) end

-- the target's local frame in the world: position, up, north, east (unit vectors), or nil without a frame
function C:localFrame()
  local f = self.frame or defaultFrame()
  if not f then return nil end
  local lat, lon = clamp(self.lat, -89.9, 89.9), self.lon
  local p = f(lat, lon, self.ground)
  local up = (f(lat, lon, self.ground + 1000) - p):normalized()
  local n = f(lat + 0.001, lon, self.ground) - p
  n = (n - up * n:dot(up)):normalized()
  local e = n:cross(up)
  return p, up, n, e
end

-- the camera pose for the current orbit: position, rotation (nil without a frame)
function C:pose()
  local p, up, n, e = self:localFrame()
  if not p then return nil end
  self.target = p
  local cp, sp = math.cos(self.pitch), math.sin(self.pitch)
  local fwd = e * math.sin(self.heading) + n * math.cos(self.heading)
  local dir = fwd * cp - up * sp
  -- (the screen's up: the ground's up tilted towards the heading. With the ground's up itself the reference was all
  -- but parallel to a look nearly straight down, and from high up the view turned upside down: south at the top,
  -- 2026-11-07)
  local camUp = up * cp + fwd * sp
  return p - dir * self.dist, quatFromDir(dir, camUp), up
end

-- move the target over the ground: forward (along the heading) and right, in metres
function C:move(fwd, right)
  local sh, ch = math.sin(self.heading), math.cos(self.heading)
  local dn, de = fwd * ch - right * sh, fwd * sh + right * ch
  self.lat = clamp(self.lat + dn / M_PER_DEG, -89.9, 89.9)
  self.lon = self.lon + de / (M_PER_DEG * math.max(0.01, math.cos(math.rad(self.lat))))
  if self.lon > 180 then self.lon = self.lon - 360 elseif self.lon < -180 then self.lon = self.lon + 360 end
end

-- the drawn ground's height above the ellipsoid at the target, or nil (no geometry under it yet, a bad hit)
function C:groundHeight()
  local f = self.frame or defaultFrame()
  if not f or not be then return nil end
  local p0 = f(self.lat, self.lon, 0)
  local up = (f(self.lat, self.lon, 1000) - p0):normalized()
  -- (straight down along world z from above: the drawn surface; good enough while up is near z, i.e. within a few
  -- hundred km of the anchor; farther out the target just keeps its eased height)
  if up.z < 0.995 then return nil end
  local top = p0 + up * (GROUND_MAX + 500)
  local z = be:getSurfaceHeightBelow(vec3(top.x, top.y, top.z))
  if not z or z ~= z then return nil end
  local h = (z - p0.z) / up.z
  if h < GROUND_MIN or h > GROUND_MAX then return nil end
  return h
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
  if z ~= 0 then self.dist = clamp(self.dist * math.exp(z * ZOOM_RATE * dt), C.DIST_MIN, C.DIST_MAX) end
  -- move the target, in the heading's frame
  local mx = MoveManager.right - MoveManager.left + MoveManager.absXAxis
  local my = MoveManager.forward - MoveManager.backward + MoveManager.absYAxis
  if mx ~= 0 or my ~= 0 then self:move(my * self.dist * PAN_RATE * dt, mx * self.dist * PAN_RATE * dt) end
  -- (the tilt the distance allows: eased up to it, so zooming out raises the view smoothly)
  local pmin = C.pitchMin(self.dist)
  if self.pitch < pmin then self.pitch = self.pitch + (pmin - self.pitch) * math.min(1, dt * 6) end
  -- the target on the ground (eased: a roof or a bridge does not jolt the view)
  local g = self:groundHeight()
  if g then self.ground = self.ground + (g - self.ground) * math.min(1, dt * 4) end
  local pos, rot, up = self:pose()
  if not pos then return false end
  -- far out: the real camera stays inside the sky's shell, the globe's copy in front of it (C.MINI_*)
  self.mini = nil
  local O = self.earthCentre and self.earthCentre() or earthCentre()
  if O then
    local vo = pos - O
    local dc = vo:length()
    if dc - C.R > C.MINI_ALT then
      local cam = O + vo * ((C.R + C.MINI_CAM_ALT) / dc)
      local s = C.miniScale
      self.mini = { s = s, O = O, Om = cam - vo * s }
      data.res.pos:set(cam)
      data.res.rot:set(rot)
      data.res.fov = self.fov
      data.res.nearClip = clamp(s * (dc - C.R) * 0.05, 0.05, 200)
      return true
    end
  end
  -- never under the ground near the target (a low view over a hill)
  if self.dist < 20000 and be then
    local gz = be:getSurfaceHeightBelow(vec3(pos.x, pos.y, pos.z + 10000))
    if gz and gz == gz and pos.z < gz + 3 and gz - pos.z < 10000 then pos.z = gz + 3 end
  end
  data.res.pos:set(pos)
  data.res.rot:set(rot)
  data.res.fov = self.fov
  data.res.nearClip = clamp(self.dist * 0.002, 0.1, 2000)
  -- the map's labels with this very pose, the one this frame renders with (map3d.drawLabels: from the extensions'
  -- onPreRender they could come before this update, a frame behind the ground)
  local m3 = rawget(_G, 'beamngEarth_map3d')
  if m3 and m3.drawLabels then pcall(m3.drawLabels, pos, rot, self.fov) end
  return true
end

function C:onSerialize()
  return {lat = self.lat, lon = self.lon, ground = self.ground, dist = self.dist, heading = self.heading,
          pitch = self.pitch}
end

function C:onDeserialized(d)
  if type(d) ~= 'table' then return end
  for _, k in ipairs({'lat', 'lon', 'ground', 'dist', 'heading', 'pitch'}) do
    if d[k] then self[k] = d[k] end
  end
end

return function(...)
  local o = ... or {}
  setmetatable(o, C)
  o:init()
  return o
end
