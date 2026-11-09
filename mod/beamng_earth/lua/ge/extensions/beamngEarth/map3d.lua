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
  if M.cancelFly then M.cancelFly() end     -- (the player's hand: a flight in progress stops)
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
  if M.cancelFly then M.cancelFly() end
  local g = globe()
  if not g then return end
  g.dist = math.max(g.DIST_MIN or 15, math.min(g.DIST_MAX or 600000, g.dist * math.exp((tonumber(notches) or 0) * 0.15)))
end

-- smooth: turned there over TURN_S, the short way round, easing out (the compass's "face north": the user, 2026-11-08:
-- "quick and smoothly rotate to north instead of snapping"); the player turning the view meanwhile takes over
M.TURN_S = 0.4
local turn = nil            -- {from, d, t, last}: the turn under way (turnStep)

function M.setHeading(h, smooth)
  local g = globe()
  if not g then return end
  h = tonumber(h) or 0
  local d = (h - g.heading + math.pi) % (2 * math.pi) - math.pi      -- (the short way: -pi .. pi)
  if not smooth or math.abs(d) < 1e-4 then
    turn = nil
    g.heading = h
    return
  end
  turn = {from = g.heading, d = d, t = 0, last = g.heading}
end

-- a frame of the turn (onUpdate; g: the globe camera, or any table with .heading)
function M.turnStep(g, dt)
  if not turn then return end
  if math.abs(g.heading - turn.last) > 1e-6 then turn = nil return end    -- (the player turned it meanwhile)
  turn.t = turn.t + (dt or 0)
  local k = math.min(1, turn.t / M.TURN_S)
  g.heading = turn.from + turn.d * (1 - (1 - k) ^ 3)
  turn.last = g.heading
  if k >= 1 then turn = nil end
end
function M.turning() return turn ~= nil end

-- "Drive here": the car teleported to the place in the middle of the view first, the map staying open over it while
-- its ground loads, then the camera flown down to the car and handed over (the user, 2026-11-07: "first teleport the
-- car then it zooms the camera down to the car to make it look seamless"; it closed the map at once before, the car's
-- camera over a place still loading). driveStep (onUpdate) runs it.
M.DRIVE_WAIT_MAX_S = 90         -- the map goes down to the car after this even if its ground never said ready
M.DRIVE_NEAR_M = 250            -- the flight's end over the car, then exit()'s path into its camera
M.DRIVE_SETTLE_S = 0.6          -- after the ground is ready (the car set down) before the flight
local drive = nil

function M.driveHere(lat, lon, name)
  local s = M.state()
  if not s.active then return {ok = false, error = 'the 3D map is not open'} end
  lat, lon = tonumber(lat) or s.lat, tonumber(lon) or s.lon
  if not lat or not lon then return {ok = false, error = 'no place in view'} end
  local stg = rawget(_G, 'beamngEarth_settings')
  if not stg or not stg.uiMapTeleport then return {ok = false, error = 'BeamNG.Earth not loaded'} end
  local r = stg.uiMapTeleport(lat, lon, name or '')
  if r and r.ok then
    drive = {at = os.clock(), stage = 'loading'}
    r.pending = true
  end
  return r
end

function M.driving() return drive ~= nil end

local function driveStep()
  if not drive then return end
  if not active then drive = nil return end
  local core = rawget(_G, 'beamngEarth_core')
  local st = core and core._state and core._state()
  local now = os.clock()
  if drive.stage == 'loading' then
    local ready = st and not st.pending and core.groundReady and core.groundReady()
    if ready then
      drive.readyAt = drive.readyAt or now
    else
      drive.readyAt = nil
    end
    if (drive.readyAt and now - drive.readyAt >= M.DRIVE_SETTLE_S) or now - drive.at > M.DRIVE_WAIT_MAX_S then
      local p = st and st.player
      if p and p.lat and p.lon and not M.flying() then
        M.flyTo(p.lat, p.lon, M.DRIVE_NEAR_M)
        drive.stage = 'flying'
      else
        drive = nil
        M.exit()
      end
    end
  elseif drive.stage == 'flying' and not M.flying() then
    drive = nil
    M.exit()
  end
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
  -- (the map key opens the style switched to last: the user, 2026-11-07, "have the game remember which map mode the
  -- player last switched to")
  if stg and stg.set then pcall(stg.set, 'mapStyle', '2d') end
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
-- ---- the 2D map's clicks on the 3D view (the user, 2026-11-07: "i want to be able to set waypoints by clicking on the
-- map and have a right click menu like the 2d map"; "overall try to make the ui almost the same on both maps")
-- The ground's lat / lon under screen point (sx, sy) of a w x h screen: the camera's ray through it against the earth's
-- sphere at the view target's height (the anchor's frame: its centre at ecefToEnu(0, 0, 0)); nil off the earth.
function M.pick(sx, sy, w, h)
  local core = rawget(_G, 'beamngEarth_core')
  local a = core and core._state and core._state().anchor
  local g = core_camera.getGlobalCameras()[M.CAMERA]
  if not active or not a or not a.ecefToEnu or not g then return nil end
  sx, sy, w, h = tonumber(sx), tonumber(sy), tonumber(w), tonumber(h)
  if not sx or not w or w <= 0 or h <= 0 then return nil end
  local p, q = core_camera.getPosition(), quat(core_camera.getQuat())
  local f, r, u = q * vec3(0, 1, 0), q * vec3(1, 0, 0), q * vec3(0, 0, 1)
  local tv = math.tan(math.rad(core_camera.getFovDeg() or 60) / 2)
  local d = (f + r * ((2 * sx / w - 1) * tv * w / h) + u * ((1 - 2 * sy / h) * tv)):normalized()
  local ox, oy, oz = a:ecefToEnu(0, 0, 0)
  local c = vec3(ox, oy, oz)
  local R = (p - c):length() - math.max(0, g.dist * math.sin(g.pitch))     -- (the target's radius: its height kept)
  local oc = p - c
  local b = oc:dot(d)
  local disc = b * b - (oc:dot(oc) - R * R)
  if disc < 0 then return nil end
  local t = -b - math.sqrt(disc)
  if t <= 0 then return nil end
  local hit = p + d * t
  local lat, lon = a:worldToGeodetic(hit.x, hit.y, hit.z)
  return {lat = lat, lon = lon}
end

-- the clicked spot's marker (EarthMap3d.vue's target) and the GPS destination's, drawn in the world: a pin standing on
-- the ground, its size kept on the screen
local marker = nil
function M.setMarker(lat, lon)
  lat, lon = tonumber(lat), tonumber(lon)
  marker = (lat and lon) and {lat = lat, lon = lon} or nil
end
local pinTarget, pinGps = nil, nil
local function drawPin(a, lat, lon, colour)
  if not debugDrawer then return end
  local g = core_camera.getGlobalCameras()[M.CAMERA]
  local x, y, z = a:geodeticToEnu(lat, lon, (g and g.ground) or 0)
  local gz = be and be:getSurfaceHeightBelow(vec3(x, y, z + 9000))
  if gz and gz == gz and gz > z - 600 and gz < z + 9000 then z = gz end
  local base = vec3(x, y, z)
  local d = math.max(1, (core_camera.getPosition() - base):length())
  local s = d * 0.012
  debugDrawer:drawCylinder(base, base + vec3(0, 0, s * 4), s * 0.12, colour)
  debugDrawer:drawSphere(base + vec3(0, 0, s * 4), s * 0.7, colour)
end
local function drawPins(a)
  if marker then
    pinTarget = pinTarget or ColorF(1, 0.42, 0.11, 0.95)
    drawPin(a, marker.lat, marker.lon, pinTarget)
  end
  local okG, gs = pcall(function() return require('beamngEarth/gps').status() end)
  if okG and gs and gs.lat and gs.lon then
    pinGps = pinGps or ColorF(0.16, 0.55, 1, 0.95)
    drawPin(a, gs.lat, gs.lon, pinGps)
  end
end
M._drawPins = drawPins

-- the screen back when something else put the driving HUD up over the open map (the user, 2026-11-07: "the map ui
-- will sometimes dissapear"): the map's own route again a moment later
local reshowAt = nil
local uiRoute, uiFrom, uiAt = nil, nil, -1e9    -- the screen last switched to, the one before it, when (onUiChangedState)
function M.onUiChangedState(toState)
  if active and not transitioning and toState ~= M.ROUTE and type(toState) == 'string'
      and toState:sub(1, 4) ~= 'menu' and toState ~= 'loading' then
    reshowAt = os.clock() + 0.25
  end
  if type(toState) == 'string' and toState ~= uiRoute then uiRoute, uiFrom, uiAt = toState, uiRoute, os.clock() end
end

-- Esc: the game's back (ui/menuManager navigationBack) takes our screen to its backTarget 'play' and says so with
-- onMenuToggled(false); the re-show above then put the map's screen straight back, so Esc did nothing (the user,
-- 2026-11-08: "pressing esc dosnt exit map mode"; the log: play / beamngEarthMap3d again 0.3 s later, six times). Back
-- from our own screen closes the map; back from the pause menu over the map goes back to the map, as before.
function M.onMenuToggled(show)
  if show or not active or transitioning then return end
  -- (on our screen still, or just left it for 'play': the back's own switch may be reported before this hook)
  local fromOurs = uiRoute == M.ROUTE or (uiRoute == 'play' and uiFrom == M.ROUTE and os.clock() - uiAt < 1.0)
  if not fromOurs then return end
  reshowAt = nil
  M.exit()
end
local function reshow()
  if reshowAt and os.clock() >= reshowAt then
    reshowAt = nil
    if active and not transitioning then
      local stg = rawget(_G, 'beamngEarth_settings')
      if stg and stg.navigate then stg.navigate(M.ROUTE) end
    end
  end
end
M._reshow = reshow

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
  -- the GPS route's state for the screen's buttons (gps.lua)
  local okG, g = pcall(function() return require('beamngEarth/gps').status() end)
  if okG and g then
    local txt = g.planning and 'Planning the route...' or g.routeError and ('No route: ' .. tostring(g.routeError)) or nil
    if not txt and g.remaining then
      txt = string.format('%s%.1f mi to go', g.name and g.name ~= '' and (g.name .. ': ') or '', g.remaining / 1609.344)
    end
    s.gps = {text = txt or (g.name ~= '' and g.name) or 'Route set'}
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
M.LABELS_IMGUI = true                   -- labels drawn by the game (maplabels3d.lua), not the screen (map3dLabels.js)
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
  if M.LABELS_IMGUI then
    -- (drawn from the camera's own update, drawLabels; the screen's labels off)
    if labels.sent ~= -3 then labels.sent = -3; hook('BngEarthMap3dLabels', {v = -3, items = {}}) end
    return
  end
  sendLabels()
  sendView(g, s)
end

-- earthGlobe's update, right after it set this frame's pose: the labels drawn with it (maplabels3d.lua)
function M.drawLabels(pos, rot, fov)
  if not active or not M.LABELS_IMGUI then return end
  local g = core_camera.getGlobalCameras()[M.CAMERA]
  local s = g and M.state()
  local core = rawget(_G, 'beamngEarth_core')
  local a = core and core._state and core._state().anchor
  if not g or not s or not s.lat or not a then return end
  local ox, oy, oz = 0, 0, -6371000
  if a.ecefToEnu then ox, oy, oz = a:ecefToEnu(0, 0, 0) end
  local t = os.clock()
  local dt = labels.drawAt and math.min(0.1, t - labels.drawAt) or 0.016
  labels.drawAt = t
  local q = quat(rot)
  local ok, err = pcall(require('beamngEarth/maplabels3d').draw, {p = vec3(pos), f = q * vec3(0, 1, 0),
    u = q * vec3(0, 0, 1), r = q * vec3(1, 0, 0), fov = fov, zoom = viewZoom(g, s), O = vec3(ox, oy, oz)}, labels.rows, dt)
  if not ok and not labels.drawErr then labels.drawErr = true; log_('E', 'labels: ' .. tostring(err)) end
end

-- ---- flying to a place (the user, 2026-11-07: "if you serch for a place dont just teliport the camera but smoothly
-- zoom it from 1 place to another"): out and over and down, as map apps: along the great circle, the distance rising
-- with how far the trip is and coming down at the end (log scale), eased.
M.FLY_MIN_S, M.FLY_MAX_S = 1.2, 6.5
local fly = nil

local function unitOf(lat, lon)
  local la, lo = math.rad(lat), math.rad(lon)
  return vec3(math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la))
end

-- Pure: the flight's path (van Wijk and Nuij, "Smooth and efficient zooming and panning", 2003, the curve map apps
-- and d3.interpolateZoom fly): the view's width w (the camera distance) and its ground travel u chosen so the picture
-- moves at one steady speed on screen, out as far as the trip needs and back down. The first version (2026-11-07)
-- moved along the ground on one curve and the height on another; the user: "the camera animation going from 1 place
-- to another needs some work". Sets f.S (the path's length, for the duration) and f.u1 (m along the great circle).
M.FLY_RHO = 1.42
function M.flyPath(f)
  local a, b = unitOf(f.lat0, f.lon0), unitOf(f.lat1, f.lon1)
  f.om = math.acos(math.max(-1, math.min(1, a:dot(b))))
  local rho = M.FLY_RHO
  local r2, u1, w0, w1 = rho * rho, f.om * 6371000, f.d0, f.d1
  f.u1 = u1
  if u1 < 1 then
    f.S = math.abs(math.log(w1 / w0)) / rho
    return f
  end
  local b0 = (w1 * w1 - w0 * w0 + r2 * r2 * u1 * u1) / (2 * w0 * r2 * u1)
  local b1 = (w1 * w1 - w0 * w0 - r2 * r2 * u1 * u1) / (2 * w1 * r2 * u1)
  f.r0 = math.log(math.sqrt(b0 * b0 + 1) - b0)
  f.S = (math.log(math.sqrt(b1 * b1 + 1) - b1) - f.r0) / rho
  return f
end

local function cosh(x) return (math.exp(x) + math.exp(-x)) / 2 end
local function sinh(x) return (math.exp(x) - math.exp(-x)) / 2 end
local function tanh(x) local e = math.exp(2 * x) return (e - 1) / (e + 1) end

-- Pure: the camera's (lat, lon, dist) s of the way (0..1, eased already) along a flight f (flyPath set).
function M.flyAt(f, s)
  if not f.S then M.flyPath(f) end
  local rho, sig = M.FLY_RHO, s * f.S
  local frac, w
  if f.u1 < 1 then
    frac = s
    w = f.d0 * math.exp((f.d1 >= f.d0 and 1 or -1) * rho * sig)
  else
    local c0 = cosh(f.r0)
    local u = f.d0 / (rho * rho) * (c0 * tanh(rho * sig + f.r0) - sinh(f.r0))
    frac = math.max(0, math.min(1, u / f.u1))
    w = f.d0 * c0 / cosh(rho * sig + f.r0)
  end
  if s >= 1 then frac, w = 1, f.d1 end
  local a, b, om = unitOf(f.lat0, f.lon0), unitOf(f.lat1, f.lon1), f.om
  local p
  if om < 1e-6 then p = a else
    p = a * (math.sin((1 - frac) * om) / math.sin(om)) + b * (math.sin(frac * om) / math.sin(om))
  end
  local lat = math.deg(math.asin(math.max(-1, math.min(1, p.z / p:length()))))
  local lon = math.deg(math.atan2(p.y, p.x))
  return lat, lon, math.max(15, math.min(20000000, w))
end

-- fly the view to (lat, lon), ending dist m from it (default: the current distance, at most 4 km)
function M.flyTo(lat, lon, dist)
  lat, lon = tonumber(lat), tonumber(lon)
  local g = core_camera.getGlobalCameras()[M.CAMERA]
  if not active or not g or not lat or not lon then return false end
  local d1 = tonumber(dist) or math.min(g.dist, 4000)
  local a, b = unitOf(g.lat, g.lon), unitOf(lat, lon)
  local km = math.acos(math.max(-1, math.min(1, a:dot(b)))) * 6371
  fly = M.flyPath({lat0 = g.lat, lon0 = g.lon, d0 = math.max(15, g.dist), lat1 = lat, lon1 = lon, d1 = d1, t = 0,
    pitch0 = g.pitch})
  -- (the duration from the path's length: ~1.5 s across a town, ~3.5 s to the next city, ~6 s round the earth)
  fly.dur = math.max(M.FLY_MIN_S, math.min(M.FLY_MAX_S, 0.6 + 0.45 * fly.S))
  -- the destination loads while the camera flies (the user, 2026-11-07: "big leaps in world gen speed"): the origin
  -- there at once, then the server told where the view will end (flyHint, map_focus)
  local core = rawget(_G, 'beamngEarth_core')
  local a = core and core._state and core._state().anchor
  if core and core.rebaseTo and a then
    local p1, p2 = math.rad(a.lat), math.rad(lat)
    local h = math.sin((p2 - p1) / 2) ^ 2 + math.cos(p1) * math.cos(p2) * math.sin(math.rad(lon - a.lon) / 2) ^ 2
    if 2 * 6371000 * math.asin(math.min(1, math.sqrt(h))) > M.REBASE_M then
      local sLat, sLon = require('beamngEarth/geodesy').snapAnchor(lat, lon)
      pcall(core.rebaseTo, sLat, sLon, 'view')
    end
  end
  return true
end

-- while flying: the server builds round where the view will end, at the detail it will have there (map_focus, for
-- the flight's anchor: sent again after the origin moved)
local function flyHint()
  if not fly then return end
  local core = rawget(_G, 'beamngEarth_core')
  local st = core and core._state and core._state()
  if not st or not st.anchor or fly.hintFor == st.anchorId then return end
  -- (not to the old origin while the rebase to the destination is on its way: Tucson 2026-11-07, the first 7 s of
  -- every far worker went to the destination's tiles in the frame of a place 2,000 miles away, thrown away after)
  local a = st.anchor
  local p1, p2 = math.rad(a.lat), math.rad(fly.lat1)
  local hv = math.sin((p2 - p1) / 2) ^ 2 + math.cos(p1) * math.cos(p2) * math.sin(math.rad(fly.lon1 - a.lon) / 2) ^ 2
  if 2 * 6371000 * math.asin(math.min(1, math.sqrt(hv))) > M.REBASE_M then return end
  fly.hintFor = st.anchorId
  local x, y = st.anchor:geodeticToEnu(fly.lat1, fly.lon1, 0)
  require('beamngEarth/net').send({type = 'map_focus', anchorId = st.anchorId, x = x, y = y,
    eye = fly.d1 * math.sin(math.rad(50)), seconds = fly.dur * (1 - math.min(1, fly.t)) + 2})
end

function M.cancelFly() fly = nil end
function M.flying() return fly ~= nil end

local function flyStep(g, dt)
  if not fly then return end
  flyHint()
  fly.t = fly.t + dt / fly.dur
  local t = math.min(1, fly.t)
  local s = t * t * (3 - 2 * t)
  g.lat, g.lon, g.dist = M.flyAt(fly, s)
  -- the tilt as it was when the flight began, back at the end (the camera's own rule looks straight down high up:
  -- earthGlobe.pitchMin, which left a flight's end looking down)
  if fly.pitch0 and g.pitchMin then
    g.pitch = math.max(fly.pitch0, g.pitchMin(g.dist))
  end
  if t >= 1 then fly = nil end
end

-- ---- the GPS route on the 3D map (the 2D map's line, drawn in the world: settings.uiGpsRoute)
-- The line's heights (the user, 2026-11-08: "the gps line isn't fully drawn in the 3d map"): every point at sea level
-- unless the game's own ground was loaded under it when the line was made, so over land above the sea the far terrain
-- covered it everywhere but round the car. Now: the route's elevation profile (gps elev, the server's DEM along it) for
-- every point, the loaded ground within ROUTE_NEAR_M of the anchor (looked up again every ROUTE_GROUND_S: blocks load
-- after the line is made), and lifted by ROUTE_LIFT of the camera's distance (the far terrain is a coarse mesh of the
-- same ground: from high up, a few metres over it was under its triangles between the points).
M.ROUTE_NEAR_M = 4000
M.ROUTE_GROUND_S = 4
M.ROUTE_LIFT = 0.004          -- of the camera's distance, at least ROUTE_LIFT_MIN m
M.ROUTE_LIFT_MIN = 3

-- Pure: each route point's height (m) from the profile {{dist m, elev m}...} by its distance along the points
-- {{lat, lon}...} (great circle); nil where there is no profile.
function M.routeHeights(pts, elev)
  local out = {}
  if type(elev) ~= 'table' or #elev == 0 then return out end
  local ed, ez = {}, {}
  for i, e in ipairs(elev) do ed[i], ez[i] = tonumber(e[1] or e['1']) or 0, tonumber(e[2] or e['2']) or 0 end
  local s, k, pla, plo = 0, 1, nil, nil
  for i, p in ipairs(pts) do
    local la, lo = tonumber(p[1] or p['1']), tonumber(p[2] or p['2'])
    if la and lo then
      if pla then
        local r = math.pi / 180
        local dla, dlo = (la - pla) * r, (lo - plo) * r
        local h = math.sin(dla / 2) ^ 2 + math.cos(pla * r) * math.cos(la * r) * math.sin(dlo / 2) ^ 2
        s = s + 2 * 6371008.8 * math.asin(math.min(1, math.sqrt(h)))
      end
      pla, plo = la, lo
      while k < #ed and ed[k + 1] < s do k = k + 1 end
      if #ed == 1 or s <= ed[1] then out[i] = ez[1]
      elseif k >= #ed then out[i] = ez[#ez]
      else
        local span = ed[k + 1] - ed[k]
        local f = span > 0 and math.max(0, math.min(1, (s - ed[k]) / span)) or 0
        out[i] = ez[k] + f * (ez[k + 1] - ez[k])
      end
    end
  end
  return out
end

local route = {version = nil, pts = nil, elev = nil, base = nil, world = nil, anchor = nil, check = 0,
  ground = 0, lift = nil}
local routeColor = nil
local function routeWorld(a, lift)
  -- (base: each point's frame position at its profile height, made once per route and anchor)
  if route.base == nil or route.anchor ~= a then
    route.base, route.anchor = {}, a
    local hs = M.routeHeights(route.pts, route.elev)
    for i, p in ipairs(route.pts) do
      local la, lo = tonumber(p[1] or p['1']), tonumber(p[2] or p['2'])
      if la and lo then
        local x, y, z = a:geodeticToEnu(la, lo, hs[i] or 0)
        route.base[#route.base + 1] = {x, y, z, x * x + y * y < M.ROUTE_NEAR_M ^ 2}
      end
    end
  end
  route.world = {}
  for i, b in ipairs(route.base) do
    local z = b[3]
    if b[4] and be then                 -- (near the anchor: the game's ground, where it is loaded)
      local gz = be:getSurfaceHeightBelow(vec3(b[1], b[2], z + 2000))
      if gz and gz == gz and gz > z - 600 and gz < z + 2000 then z = gz end
    end
    route.world[i] = vec3(b[1], b[2], z + lift)
  end
end

local function drawRoute(a, dt)
  route.check = route.check - dt
  route.ground = route.ground - dt
  if route.check <= 0 then
    route.check = 1
    local stg = rawget(_G, 'beamngEarth_settings')
    local r = stg and stg.uiGpsRoute and stg.uiGpsRoute() or nil
    if not r or not r.points then route.version, route.pts, route.base, route.world = nil, nil, nil, nil
    elseif r.version ~= route.version or (r.elev ~= nil and route.elev == nil) then
      route.version, route.pts, route.elev, route.base, route.world = r.version, r.points, r.elev, nil, nil
    end
  end
  if not route.pts then return end
  local g = core_camera.getGlobalCameras()[M.CAMERA]
  local dist = g and g.dist or 1000
  -- (the lift in steps of a third: the line is made again only when the camera's height changes that much)
  local lift = math.max(M.ROUTE_LIFT_MIN, M.ROUTE_LIFT * dist)
  lift = M.ROUTE_LIFT_MIN * 1.33 ^ math.ceil(math.log(lift / M.ROUTE_LIFT_MIN) / math.log(1.33))
  if route.world == nil or route.anchor ~= a or route.lift ~= lift or route.ground <= 0 then
    route.lift, route.ground = lift, M.ROUTE_GROUND_S
    routeWorld(a, lift)
  end
  routeColor = routeColor or ColorF(0.16, 0.55, 1, 0.95)
  local w = math.max(3, math.min(40, dist * 0.0015))      -- (px-ish: wider from higher up)
  for i = 2, #route.world do
    debugDrawer:drawLineInstance(route.world[i - 1], route.world[i], w, routeColor)
  end
end

-- the screen's GPS buttons: a route to the view's target / none
function M.gpsHere(name)
  local s = M.state()
  local stg = rawget(_G, 'beamngEarth_settings')
  if not s.lat or not stg or not stg.uiMapGps then return {ok = false} end
  return stg.uiMapGps(s.lat, s.lon, name or '')
end

-- The world's origin follows the view (the user, 2026-11-07: zooming into a spot a continent away "the screen stays
-- black": the ground there lay ~160 km under the origin's z = 0 on the earth's curve, and the game's sky took the
-- camera for being underground): once the view's target is REBASE_M from the origin it moves there (core.rebaseTo
-- 'view', on the teleport grid: the frame only, ~0.1 s; the car and its block stay in their own frame, hidden, and
-- the world goes back there before the map closes: returning).
-- (100 km at first; with the sky shader's camera on the planet (skypatch.py SKY) the view far from the origin renders
-- right, and every move reloaded the view: the user, 2026-11-07: "getting alot of issues as we rebase ... fix the grey
-- rendering without haveing to rebase". Kept past 1,000 km: the anchor's own tile grid lies on its tangent plane,
-- stretched ~1 % there, 11 % at 3,000 km)
M.REBASE_M = 1000000
-- Never while the eye is over REBASE_MAX_EYE_M (the whole earth in view): turning the globe moved the origin every few
-- thousand km, each a reload and the backdrop earth swapped (the user, 2026-11-07: "when im zoomed all the way out
-- and im rotating the earth around, it will lag alot and the earth will pop out and in from its center"); the sky
-- shader's camera on the planet (skypatch.py SKY) draws the far side right. Coming down, one move to the view.
M.REBASE_MAX_EYE_M = 300000
M.REBASE_EVERY_S = 3
local lastRebase = -1e9

-- the view's target's great-circle distance from the world's origin (m): its x / y on the origin's plane shrink again
-- past a quarter of the earth (the far side's point lies under the origin)
local function anchorDistance(g)
  local core = rawget(_G, 'beamngEarth_core')
  local a = core and core._state and core._state().anchor
  if not a or not g.lat then return 0 end
  local p1, p2 = math.rad(a.lat), math.rad(g.lat)
  local dl = math.rad(g.lon - a.lon)
  local h = math.sin((p2 - p1) / 2) ^ 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ^ 2
  return 2 * 6371000 * math.asin(math.min(1, math.sqrt(h)))
end

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
  flyStep(g, dtReal or 0.016)
  M.turnStep(g, dtReal or 0.016)
  driveStep()
  do
    local core = rawget(_G, 'beamngEarth_core')
    local a = core and core._state and core._state().anchor
    if a then pcall(drawRoute, a, dtReal or 0.016); pcall(drawPins, a) end
    reshow()
  end
  if returning then
    if not viewFramed() then
      local r = returning
      returning = nil
      if r == '2d' then M.to2d() else M.exit() end
      return
    end
    local core = rawget(_G, 'beamngEarth_core')
    if core and core.returnToCar then core.returnToCar() end
  elseif not transitioning and g.target and clock - lastRebase > M.REBASE_EVERY_S and not M.flying()
      and g.dist * math.sin(g.pitch) < M.REBASE_MAX_EYE_M and anchorDistance(g) > M.REBASE_M then
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
