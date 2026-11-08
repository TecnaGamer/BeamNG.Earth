-- BeamNG.Earth 3D map mode: GE extension 'beamngEarth_map3d' (the user, 2026-11-07: "Add a 3d map mode, this
-- essentially turns the game into a modern map app like Apple Maps or Google earth ... Keep the screen on the game
-- and use custom camera movement ... as you enter and exit the 3d map mode make the camera zoom in or out from the
-- car and fly up like in the vanilla maps. Also keep the 2d map and let players choose between the 2").
--
-- Entering: a camera path from the current view up over the car (the vanilla big map's markers: framed on the car,
-- then high above it), ending at the orbit camera's first pose; then the orbit camera (core/cameraModes/earthGlobe)
-- takes over. Leaving: the reverse path back down to the car and the vehicle camera again. The car keeps its place;
-- the server streams for the camera (the mod's pose messages carry the camera: eye height, horizon).

local M = {}

M.CAMERA = 'earthGlobe'
M.START_DIST = 1800            -- m from the car when the orbit starts
M.START_PITCH = math.rad(52)
M.FLY_TIME = 2.4               -- s, as the vanilla big map's posTransitionTime

local active = false
local transitioning = false
local previousCamMode = nil

local function log_(level, msg) log(level, 'beamngEarth.map3d', msg) end

function M.isActive() return active end
function M.isTransitioning() return transitioning end

local function vehicleFrame()
  local veh = getPlayerVehicle(0)
  if not veh then return nil end
  local oobb = veh:getSpawnWorldOOBB()
  return veh, oobb:getCenter(), oobb:getHalfExtents():length()
end

-- the orbit's first pose: over the car, looking the way the camera looked
local function startOrbit(center)
  local fwd = core_camera.getQuat() * vec3(0, 1, 0)
  local heading = math.atan2(fwd.x, fwd.y)
  local g = rawget(_G, 'core_camera') and core_camera.getGlobalCameras and core_camera.getGlobalCameras()[M.CAMERA]
  local data = {target = vec3(center), dist = M.START_DIST, heading = heading, pitch = M.START_PITCH, fov = 55}
  local pos, rot
  if g then
    g:setCustomData(data)
    pos, rot = g:pose()
  else
    local cp = math.cos(data.pitch)
    local dir = vec3(math.sin(heading) * cp, math.cos(heading) * cp, -math.sin(data.pitch))
    pos, rot = center - dir * data.dist, quatFromDir(dir, vec3(0, 0, 1))
  end
  return data, pos, rot
end

local function marker(pos, rot, fov, t, nearClip, smooth)
  return {fov = fov, movingEnd = true, movingStart = true, positionSmooth = smooth or 0.5, pos = vec3(pos),
          rot = quat(rot), time = t, trackPosition = false, nearClip = nearClip or 1}
end

local function play(markers, done)
  transitioning = true
  local ok, err = pcall(core_paths.playPath, {looped = false, manualFov = false, markers = markers}, 0, {
    useDtReal = true,
    finishedPath = function()
      transitioning = false
      done()
    end})
  if not ok then
    log_('W', 'camera path failed: ' .. tostring(err))
    transitioning = false
    done()
  end
end

function M.enter()
  if active or transitioning then return end
  local veh, center, size = vehicleFrame()
  if not veh then return end
  previousCamMode = core_camera.getActiveCamName and core_camera.getActiveCamName(0) or nil
  local data, endPos, endRot = startOrbit(center)
  local camQuat = core_camera.getQuat()
  local down = quatFromDir(vec3(0, 0, -1), camQuat * vec3(0, 1, 0))
  local fov = core_camera.getFovDeg()
  local up1 = center + vec3(0, 0, 8 + size * 4)
  local up2 = center + vec3(0, 0, 60 + size * 20)
  local markers = {
    marker(core_camera.getPosition(), camQuat, fov, 0, 0.1),
    marker(up1, down, fov, M.FLY_TIME / 3, 1),
    marker(up2, down, fov, M.FLY_TIME / 2, 1),
    marker(endPos, endRot, data.fov, M.FLY_TIME, math.max(1, data.dist * 0.002), 0.1),
  }
  markers[1].movingStart = false
  markers[3].movingEnd = false
  log_('I', 'entering the 3D map')
  play(markers, function()
    core_camera.setGlobalCameraByName(M.CAMERA, false, data)
    active = true
    extensions.hook('onBeamngEarthMap3d', true)
  end)
end

function M.exit()
  if not active or transitioning then return end
  local veh, center, size = vehicleFrame()
  local g = core_camera.getGlobalCameras()[M.CAMERA]
  if not veh or not g then
    core_camera.setGlobalCameraByName(nil)
    active = false
    return
  end
  local startPos, startRot = g:pose()
  local camQuat = quat(startRot)
  local down = quatFromDir(vec3(0, 0, -1), camQuat * vec3(0, 1, 0))
  -- back to the vehicle camera, then blend its first frame in from straight above the car
  local up2 = center + vec3(0, 0, 60 + size * 20)
  local up1 = center + vec3(0, 0, 8 + size * 4)
  local markers = {
    marker(startPos, startRot, g.fov, 0, math.max(1, g.dist * 0.002)),
    marker(up2, down, 55, M.FLY_TIME / 2, 1),
    marker(up1, down, 55, M.FLY_TIME * 2 / 3, 1),
  }
  markers[1].movingStart = false
  markers[3].movingEnd = false
  active = false
  log_('I', 'leaving the 3D map')
  core_camera.setGlobalCameraByName(nil)
  play(markers, function()
    core_camera.setGlobalCameraByName(nil)
    if core_camera.startCameraTransition then pcall(core_camera.startCameraTransition, 0.6) end
    extensions.hook('onBeamngEarthMap3d', false)
  end)
end

function M.toggle()
  if active then M.exit() else M.enter() end
end

-- the orbit's state for the UI / server (target lat / lon, distance, heading)
function M.state()
  if not active then return {active = false} end
  local g = core_camera.getGlobalCameras()[M.CAMERA]
  if not g then return {active = false} end
  local out = {active = true, dist = g.dist, heading = g.heading, pitch = g.pitch}
  local core = rawget(_G, 'beamngEarth_core')
  local a = core and core._state and core._state().anchor
  if a and a.enuToGeodetic then
    out.lat, out.lon = a:enuToGeodetic(g.target.x, g.target.y, 0)
  end
  return out
end

-- a level change or a vehicle switch ends the mode at once (no path)
function M.onClientEndMission()
  active, transitioning = false, false
end

return M
