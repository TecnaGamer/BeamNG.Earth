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
    local stg = rawget(_G, 'beamngEarth_settings')
    if stg and stg.navigate then stg.navigate(M.ROUTE) end
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
  local stg = rawget(_G, 'beamngEarth_settings')
  if stg and stg.navigate then stg.navigate('play') end
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

-- ---- the 3D map screen's mouse (ui EarthMap3d.vue: the UI takes the mouse from the camera, as the vanilla big map's)
M.ROUTE = 'beamngEarthMap3d'          -- index.js registers it

local function globe()
  return active and core_camera.getGlobalCameras()[M.CAMERA] or nil
end

-- grab and drag: the ground under the cursor follows it (metres per pixel at the target, the tilt stretching the
-- vertical); mode 'orbit' turns (dx) and tilts (dy) instead
function M.drag(dx, dy, mode, screenH)
  local g = globe()
  if not g then return end
  dx, dy = tonumber(dx) or 0, tonumber(dy) or 0
  if mode == 'orbit' then
    g.heading = g.heading + dx * 0.006
    g.pitch = math.max(math.rad(12), math.min(math.rad(89.5), g.pitch + dy * 0.004))
    return
  end
  local mpp = 2 * g.dist * math.tan(math.rad(g.fov) / 2) / math.max(200, tonumber(screenH) or 1080)
  local f = vec3(math.sin(g.heading), math.cos(g.heading), 0)
  local r = vec3(f.y, -f.x, 0)
  g.target = g.target + (r * (-dx) + f * (dy / math.max(0.2, math.sin(g.pitch)))) * mpp
end

-- the wheel: notches (positive: out), each ~15 % of the distance
function M.zoom(notches)
  local g = globe()
  if not g then return end
  g.dist = math.max(15, math.min(600000, g.dist * math.exp((tonumber(notches) or 0) * 0.15)))
end

function M.setHeading(h)
  local g = globe()
  if g then g.heading = tonumber(h) or 0 end
end

-- teleport the car to the place in the middle of the view, then fly down to it
function M.driveHere()
  local s = M.state()
  if not s.active or not s.lat then return {ok = false, error = 'no place in view'} end
  local stg = rawget(_G, 'beamngEarth_settings')
  if not stg or not stg.uiMapTeleport then return {ok = false, error = 'BeamNG.Earth not loaded'} end
  local r = stg.uiMapTeleport(s.lat, s.lon, '')
  if r and r.ok then
    active, transitioning = false, false
    core_camera.setGlobalCameraByName(nil)
    if stg.navigate then stg.navigate('play') end
    extensions.hook('onBeamngEarthMap3d', false)
  end
  return r
end

-- the 2D map instead (the screen's 2D button): straight back to the car's camera, then the flat map
function M.to2d()
  active, transitioning = false, false
  core_camera.setGlobalCameraByName(nil)
  extensions.hook('onBeamngEarthMap3d', false)
  local stg = rawget(_G, 'beamngEarth_settings')
  if stg and stg.navigate then stg.navigate(stg.MAP_ROUTE) end
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

-- the 3D map screen's poll (EarthMap3d.vue): state() and the place under the view (the status bar asks the server
-- about the view's target while the map is open: core.lua)
function M.uiState()
  local s = M.state()
  local core = rawget(_G, 'beamngEarth_core')
  local loc = core and core.statusbar and core.statusbar.location() or nil
  if loc then
    local parts = {}
    for _, k in ipairs({'street', 'place', 'region', 'country'}) do
      if loc[k] and loc[k] ~= '' then parts[#parts + 1] = loc[k] end
    end
    s.place = table.concat(parts, ', ')
  end
  return s
end

-- ---- place names over the 3D view (the user: "Add overlays to the terrain to show street names and some of our other
-- overlays from the 2d map"): the towns round the view's target from the server (labels: placenames.labels, the largest
-- first), asked again when the view moved a third of its distance or zoomed by half; drawn each frame as text at the
-- ground, each kind of place only within its reach (a city from far away, a hamlet only close by), the nearest
-- LABELS_DRAWN at most.
M.LABEL_REACH = {megacity = 900000, city = 250000, town = 45000, village = 14000, hamlet = 5000}
M.LABEL_REACH_DEFAULT = 2500           -- a city's district (no class)
M.LABELS_DRAWN = 90
local labels = {list = {}, anchor = nil, reqId = 0, last = nil}

function M.onLabels(msg)
  if not msg or msg.id ~= labels.reqId or type(msg.places) ~= 'table' then return end
  labels.list = {}
  for _, p in ipairs(msg.places) do
    if type(p) == 'table' and p[1] and p[2] and p[3] then
      labels.list[#labels.list + 1] = {name = tostring(p[1]), lat = p[2], lon = p[3], cls = p[4] or '', pop = p[5] or 0}
    end
  end
  labels.anchor = nil                  -- (placed in the current anchor's frame at the next draw)
end

local function requestLabels(g, s, now)
  local radius = math.max(8000, math.min(150000, g.dist * 3))
  local L = labels.last
  if L and now - L.t < 1.0 then return end
  if L and math.abs(math.log(radius / L.r)) < 0.5 then
    local dx = (s.lat - L.lat) * 111320
    local dy = (s.lon - L.lon) * 111320 * math.cos(math.rad(s.lat))
    if dx * dx + dy * dy < (radius / 3) ^ 2 then return end
  end
  labels.reqId = labels.reqId + 1
  labels.last = {t = now, lat = s.lat, lon = s.lon, r = radius}
  require('beamngEarth/net').send({ type = 'labels', id = labels.reqId, lat = s.lat, lon = s.lon, radius = radius })
end

local white, back = nil, nil
local function drawLabels(g)
  local core = rawget(_G, 'beamngEarth_core')
  local a = core and core._state and core._state().anchor
  if not a or not debugDrawer then return end
  if labels.anchor ~= a then           -- (a rebase or new results: positions in this anchor's frame, heights again)
    for _, p in ipairs(labels.list) do
      p.x, p.y = a:geodeticToEnu(p.lat, p.lon, 0)
      p.z = nil
    end
    labels.anchor = a
  end
  white = white or ColorF(1, 1, 1, 1)
  back = back or ColorI(0, 0, 0, 150)
  local cam = core_camera.getPosition()
  local drawn = 0
  for _, p in ipairs(labels.list) do
    if drawn >= M.LABELS_DRAWN then break end
    if p.x then
      local dx, dy = p.x - cam.x, p.y - cam.y
      local reach = M.LABEL_REACH[p.cls] or M.LABEL_REACH_DEFAULT
      if dx * dx + dy * dy < reach * reach then
        if not p.z then
          local z = be:getSurfaceHeightBelow(vec3(p.x, p.y, cam.z + 20000))
          if z and z > -1e8 then p.z = z end
        end
        if p.z then
          debugDrawer:drawTextAdvanced(vec3(p.x, p.y, p.z + 30), p.name, white, true, false, back)
          drawn = drawn + 1
        end
      end
    end
  end
end

local clock = 0
function M.onUpdate(dtReal)
  if not active then return end
  clock = clock + (dtReal or 0)
  local g = core_camera.getGlobalCameras()[M.CAMERA]
  if not g then return end
  local s = M.state()
  if s.lat then requestLabels(g, s, clock) end
  drawLabels(g)
end

-- a level change or a vehicle switch ends the mode at once (no path)
function M.onClientEndMission()
  active, transitioning = false, false
end

return M
