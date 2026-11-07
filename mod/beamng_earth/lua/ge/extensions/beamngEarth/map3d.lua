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
local function anchor()
  local core = rawget(_G, 'beamngEarth_core')
  return core and core._state and core._state().anchor or nil
end

local function startOrbit(center)
  local fwd = core_camera.getQuat() * vec3(0, 1, 0)
  local heading = math.atan2(fwd.x, fwd.y)
  local a = anchor()
  local lat, lon, h = 0, 0, 0
  if a then
    if a.worldToGeodetic then lat, lon, h = a:worldToGeodetic(center.x, center.y, center.z)
    else lat, lon, h = a:enuToGeodetic(center.x, center.y, center.z) end
  end
  local data = {lat = lat, lon = lon, ground = h or 0, dist = M.START_DIST, heading = heading, pitch = M.START_PITCH,
                fov = 55}
  local g = core_camera.getGlobalCameras and core_camera.getGlobalCameras()[M.CAMERA]
  local pos, rot
  if g then
    g:setCustomData(data)
    pos, rot = g:pose()
  end
  if not pos then
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
    -- (the car paused while the map is open, as the vanilla big map: far from the origin its ground tilts against
    -- the game's gravity, the map's rebases below)
    pcall(simTimeAuthority.pushPauseRequest, 'beamngEarthMap3d')
    local stg = rawget(_G, 'beamngEarth_settings')
    if stg and stg.navigate then stg.navigate(M.ROUTE) end
    extensions.hook('onBeamngEarthMap3d', true)
  end)
end

local function unpause()
  pcall(simTimeAuthority.popPauseRequest, 'beamngEarthMap3d')
end

-- The world back in the car's own frame before the map closes (the map moved the origin to the view: core 'view'
-- frames, the car hidden in its own): 'exit' / '2d' once core says so (onUpdate)
local returning = nil
local function viewFramed()
  local core = rawget(_G, 'beamngEarth_core')
  return core and core.inViewFrame and core.inViewFrame() or false
end

function M.exit()
  if not active or transitioning then return end
  if viewFramed() then
    returning = 'exit'
    return
  end
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
    unpause()
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
    g.pitch = math.max(g.pitchMin and g.pitchMin(g.dist) or math.rad(12), math.min(math.rad(89.5), g.pitch + dy * 0.004))
    return
  end
  local mpp = 2 * g.dist * math.tan(math.rad(g.fov) / 2) / math.max(200, tonumber(screenH) or 1080)
  g:move(dy / math.max(0.2, math.sin(g.pitch)) * mpp, -dx * mpp)
end

-- the wheel: notches (positive: out), each ~15 % of the distance
function M.zoom(notches)
  local g = globe()
  if not g then return end
  g.dist = math.max(g.DIST_MIN or 15, math.min(g.DIST_MAX or 600000, g.dist * math.exp((tonumber(notches) or 0) * 0.15)))
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
    unpause()
    if stg.navigate then stg.navigate('play') end
    extensions.hook('onBeamngEarthMap3d', false)
  end
  return r
end

-- the 2D map instead (the screen's 2D button): straight back to the car's camera, then the flat map
function M.to2d()
  if active and viewFramed() then
    returning = '2d'
    return
  end
  active, transitioning = false, false
  core_camera.setGlobalCameraByName(nil)
  unpause()
  extensions.hook('onBeamngEarthMap3d', false)
  local stg = rawget(_G, 'beamngEarth_settings')
  if stg and stg.navigate then stg.navigate(stg.MAP_ROUTE) end
end

-- the orbit's state for the UI / server (target lat / lon, distance, heading)
function M.state()
  if not active then return {active = false} end
  local g = core_camera.getGlobalCameras()[M.CAMERA]
  if not g then return {active = false} end
  return {active = true, dist = g.dist, heading = g.heading, pitch = g.pitch, lat = g.lat, lon = g.lon}
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

-- ---- labels over the 3D view, as map apps draw them (the user, 2026-11-07: "handle labels similar how modern map apps
-- like Apple Maps dose it. As you zoom in more and more info appears and try to make it as clean as this"). The
-- server sends every label round the view with the web-map zoom it starts and stops at and a rank (map_labels:
-- maplabels.py: Natural Earth far out, Overture's towns, the streets' names and places close in); here they are
-- placed in the world (the anchor's frame: the earth's curve in it) and handed to the map screen (EarthMap3d.vue)
-- with the camera every frame. The screen projects them, shows those its zoom allows, the most important first,
-- none overlapping, fading in and out.
M.SCREEN_H = 1080                       -- the UI's height in px (the screen says: setScreen)
local labels = {rows = {}, reqId = 0, last = nil, anchor = nil, version = 0, sent = -1, heights = 0}

function M.setScreen(h)
  h = tonumber(h)
  if h and h > 100 then M.SCREEN_H = h end
end

-- Pure: the web-map zoom of a view eye metres above the ground at lat with a vertical field of view fovDeg on a
-- screen screenH px tall (256 px tiles: zoom z shows 156543 cos(lat) / 2^z metres per px)
function M.zoomFor(eye, lat, fovDeg, screenH)
  local mpp = 2 * math.max(1, eye) * math.tan(math.rad(fovDeg or 55) / 2) / math.max(100, screenH or 1080)
  local z = math.log(156543.03 * math.max(0.05, math.cos(math.rad(lat or 0))) / mpp) / math.log(2)
  return math.max(0, math.min(21, z))
end

-- Pure: how far from the target the view's ground reaches (m): to its top edge when that meets the ground, else
-- to the horizon
function M.viewRadius(eye, pitch, fovDeg)
  local horizon = math.sqrt(2 * 6371000 * math.max(1, eye) + eye * eye)
  local top = pitch - math.rad(fovDeg or 55) * 0.5 * 1.15
  local r = top > 0.02 and math.min(horizon, eye / math.tan(top)) or horizon
  return math.max(1500, math.min(4000000, r * 1.25 + eye))
end

-- (against a 1000 px view, the size the web maps' zooms are tuned for: a 4K screen at its own height showed a
-- state's area at zoom 8.7, past Natural Earth's state names, 2026-11-07)
M.ZOOM_REF_PX = 1000
local function viewZoom(g, s)
  local eye = g.dist * math.sin(g.pitch)
  return M.zoomFor(eye, s.lat, g.fov or 55, M.ZOOM_REF_PX), eye
end

local function requestLabels(g, s, now)
  local zoom, eye = viewZoom(g, s)
  local radius = M.viewRadius(eye, g.pitch, g.fov)
  local L = labels.last
  if L and now - L.t < 0.5 then return end
  if L and math.abs(zoom - L.z) < 0.5 then
    local dx = (s.lat - L.lat) * 111320
    local dy = (s.lon - L.lon) * 111320 * math.cos(math.rad(s.lat))
    if dx * dx + dy * dy < (L.r / 4) ^ 2 then return end
  end
  labels.reqId = labels.reqId + 1
  labels.last = {t = now, lat = s.lat, lon = s.lon, r = radius, z = zoom}
  require('beamngEarth/net').send({ type = 'map_labels', id = labels.reqId, lat = s.lat, lon = s.lon, zoom = zoom,
    radius = radius })
end

-- the server's map_labels_result: rows [kind, text, lat, lon, min zoom, max zoom, rank, extra, bearing]
function M.onMapLabels(msg)
  if not msg or msg.id ~= labels.reqId or type(msg.rows) ~= 'table' then return end
  local rows = {}
  for i, r in ipairs(msg.rows) do
    if type(r) == 'table' and r[2] and tonumber(r[3]) and tonumber(r[4]) then
      rows[#rows + 1] = {id = tostring(r[1]) .. '|' .. tostring(r[2]) .. '|' .. string.format('%.4f,%.4f', r[3], r[4]),
        k = tostring(r[1]), t = tostring(r[2]), lat = tonumber(r[3]), lon = tonumber(r[4]), mz = tonumber(r[5]) or 0,
        xz = tonumber(r[6]) or 99, r = tonumber(r[7]) or 50, e = r[8] ~= nil and tostring(r[8]) or '',
        b = tonumber(r[9])}
    end
  end
  labels.rows, labels.anchor = rows, nil
  labels.version = labels.version + 1
end

-- the rows' places in the anchor's world: x, y, z (the ground, read from the drawn world close in; sea level far
-- out) and a street's direction along its road
local NEAR_KINDS = {street = true, poi = true, district = true, town = true}
local function placeLabels(a)
  if labels.anchor ~= a then
    for _, p in ipairs(labels.rows) do
      p.x, p.y, p.z = a:geodeticToEnu(p.lat, p.lon, 0)
      p.ground = not NEAR_KINDS[p.k]      -- (far kinds stay at sea level)
      if p.b then
        local br = math.rad(p.b)
        local x2, y2, z2 = a:geodeticToEnu(p.lat + math.cos(br) * 30 / 111320,
          p.lon + math.sin(br) * 30 / (111320 * math.max(0.05, math.cos(math.rad(p.lat)))), 0)
        p.dx, p.dy, p.dz = x2 - p.x, y2 - p.y, z2 - p.z
      end
    end
    labels.anchor = a
    labels.version = labels.version + 1
  end
  -- the ground under the near ones, a few each frame (the drawn far tiles and blocks)
  local n = 0
  for _, p in ipairs(labels.rows) do
    if not p.ground and be then
      local z = be:getSurfaceHeightBelow(vec3(p.x, p.y, p.z + 9000))
      if z and z == z and z > p.z - 600 and z < p.z + 9000 then p.z = z + 2 end
      p.ground = true
      n = n + 1
      if n >= 60 then break end
    end
  end
  if n > 0 then labels.version = labels.version + 1 end
end

local function hook(name, data)
  if guihooks and guihooks.trigger then guihooks.trigger(name, data) end
end

local function sendLabels()
  if labels.sent == labels.version then return end
  labels.sent = labels.version
  local items = {}
  for i, p in ipairs(labels.rows) do
    if p.x then
      items[i] = {id = p.id, k = p.k, t = p.t, x = p.x, y = p.y, z = p.z, mz = p.mz, xz = p.xz, r = p.r, e = p.e,
        dx = p.dx, dy = p.dy, dz = p.dz}
    end
  end
  hook('BngEarthMap3dLabels', {v = labels.version, items = items})
end

-- (the screen just opened: everything again)
function M.resendLabels() labels.sent = -2 end

-- every frame while the map is open: the camera, for the screen's projection (and the earth's centre: labels past
-- the horizon hidden)
local function sendView(g, s)
  local p = core_camera.getPosition()
  local q = core_camera.getQuat()
  if not p or not q then return end
  local f, u, r = q * vec3(0, 1, 0), q * vec3(0, 0, 1), q * vec3(1, 0, 0)
  local core = rawget(_G, 'beamngEarth_core')
  local a = core and core._state and core._state().anchor
  local ox, oy, oz = 0, 0, -6371000
  if a and a.ecefToEnu then ox, oy, oz = a:ecefToEnu(0, 0, 0) end
  local zoom = viewZoom(g, s)
  hook('BngEarthMap3dView', {px = p.x, py = p.y, pz = p.z, fx = f.x, fy = f.y, fz = f.z, ux = u.x, uy = u.y, uz = u.z,
    rx = r.x, ry = r.y, rz = r.z, fov = core_camera.getFovDeg(), zoom = zoom, ox = ox, oy = oy, oz = oz})
end

function M.onPreRender()
  if not active then return end
  local g = core_camera.getGlobalCameras()[M.CAMERA]
  local s = g and M.state()
  if not g or not s or not s.lat then return end
  local core = rawget(_G, 'beamngEarth_core')
  local a = core and core._state and core._state().anchor
  if not a then return end
  placeLabels(a)
  sendLabels()
  sendView(g, s)
end

-- The world's origin follows the view (the user, 2026-11-07: zooming into a spot a continent away "the screen stays
-- black": the ground there lay ~160 km under the origin's z = 0 on the earth's curve, and the game's sky took the
-- camera for being underground): once the view's target is REBASE_M from the origin it moves there (core.rebaseTo
-- 'view', on the teleport grid: the frame only, ~0.1 s; the car and its block stay in their own frame, hidden, and
-- the world goes back there before the map closes: returning).
M.REBASE_M = 100000
M.REBASE_EYE_MAX_M = 1500000    -- (higher up the whole earth is in view: the origin stays, no far field rebuilt)
M.REBASE_EVERY_S = 3
local lastRebase = -1e9

local clock = 0
-- No sun shadows from high up (the user, 2026-11-07: "this shadow map box that follows underneath the camera when
-- zoomed out in map view": the shadow map covers a box round the camera, its edge a dark line across the land from
-- miles up): off above SHADOW_EYE_M while the map is open, the sky's own setting back otherwise.
M.SHADOW_EYE_M = 1600
local shadowSaved = nil         -- the sky's castShadows while the map holds it off
local function holdShadows(off)
  local sky = scenetree.findObject('sunsky')
  if not sky then shadowSaved = nil return end
  if off and shadowSaved == nil then
    shadowSaved = sky:getField('castShadows', 0)
    sky:setField('castShadows', 0, '0')
    if sky.postApply then sky:postApply() end
  elseif not off and shadowSaved ~= nil then
    sky:setField('castShadows', 0, shadowSaved ~= '' and shadowSaved or '1')
    if sky.postApply then sky:postApply() end
    shadowSaved = nil
  end
end

function M.onUpdate(dtReal)
  if not active then
    holdShadows(false)
    pcall(function() require('beamngEarth/farfield').setPreview(false) end)
    if labels.sent ~= -1 then       -- (the map closed: the screen's labels go)
      labels.sent = -1
      hook('BngEarthMap3dLabels', {v = -1, items = {}})
    end
    return
  end
  do
    local gc = core_camera.getGlobalCameras()[M.CAMERA]
    if gc then
      local eye = gc.dist * math.sin(gc.pitch)
      holdShadows(eye > M.SHADOW_EYE_M)
      -- (each new far tile shown as it loads, not when its whole swap group has: farfield.setPreview)
      pcall(function() require('beamngEarth/farfield').setPreview(true, eye) end)
    end
  end
  clock = clock + (dtReal or 0)
  local g = core_camera.getGlobalCameras()[M.CAMERA]
  if not g then return end
  local s = M.state()
  if returning then
    if not viewFramed() then
      local r = returning
      returning = nil
      if r == '2d' then M.to2d() else M.exit() end
      return
    end
    local core = rawget(_G, 'beamngEarth_core')
    if core and core.returnToCar then core.returnToCar() end
  elseif not transitioning and g.target and clock - lastRebase > M.REBASE_EVERY_S
      and g.dist * math.sin(g.pitch) < M.REBASE_EYE_MAX_M
      and g.target.x * g.target.x + g.target.y * g.target.y > M.REBASE_M * M.REBASE_M then
    local core = rawget(_G, 'beamngEarth_core')
    if core and core.rebaseTo and s.lat then
      local sLat, sLon = require('beamngEarth/geodesy').snapAnchor(s.lat, s.lon)
      local ok = core.rebaseTo(sLat, sLon, 'view')
      lastRebase = clock
      if ok then log_('I', string.format('origin moved to the view: %.4f, %.4f', sLat, sLon)) end
    end
  end
  if s.lat then requestLabels(g, s, clock) end
end

-- a level change or a vehicle switch ends the mode at once (no path)
function M.onClientEndMission()
  if active then unpause() end
  active, transitioning, returning = false, false, nil
end

return M
