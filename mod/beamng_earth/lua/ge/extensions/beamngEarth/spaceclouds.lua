-- BeamNG.Earth: the clouds seen from space (server spaceclouds.py, 2026-11-06). Above SHOW_M (the camera's height
-- over sea level) the game's volumetric clouds are hidden (weather.lua CLOUD_HIDE_M: up there they composite a stale
-- picture of themselves into the sky) and the satellite photos are cloud-free, so the Earth looked bare from orbit.
-- This asks the server for the latest satellite day's clouds round the point under the camera: a curved shell a few km
-- up out to the horizon, its colour and opacity textures; drawn translucent, shown only while the camera is that high
-- (back under HIDE_M it is hidden), asked again when the camera has moved a quarter of its reach, after a rebase, or
-- when it is REFRESH_S old.

local M = {}

M.SHOW_M = 12000
M.HIDE_M = 10000
M.CHECK_S = 1.0
M.REFRESH_S = 15 * 60       -- (the night side's edge moves ~4 degrees in that time: its lights are baked in)
M.LIGHTS_NITS = 150
M.RETRY_S = 60
M.MOVE_SHARE = 0.25
M.GROW_SHARE = 1.2          -- ... or when the camera's reach (it rose) is this much the shell's
M.SHELL_H_M = 6000          -- (server spaceclouds.SHELL_H_M / MAX_RADIUS_M)
M.MAX_RADIUS_M = 2600000

local shell = nil       -- { obj, material, anchorId, lat, lon, radius, at }
local pending = nil     -- { at }: a request out
local shown = false
local timer = 0
local clock = 0

local function deleteShell()
  if not shell then return end
  for _, o in ipairs({ shell.obj, shell.lights }) do pcall(function() o:delete() end) end
  for _, name in ipairs({ shell.material, shell.lightsMaterial }) do
    local m = name and scenetree.findObject(name)
    if m then pcall(function() m:delete() end) end
  end
  shell = nil
end

-- The night side's city lights (server spaceclouds._lights): emissive, added to what is under them (black adds nothing,
-- and the server darkened the day side already).
local function makeLightsMaterial(name, lights)
  local m = scenetree.findObject(name)
  if m then return m end
  m = createObject('Material')
  m:setField('mapTo', 0, name)
  m:setField('version', 0, '1.5')
  m:setField('baseColorFactor', 0, '0 0 0 1')
  m:setField('emissiveMap', 0, lights)
  m:setField('emissiveFactor', 0, '1 1 1')
  m:setField('emissiveIntensityNits', 0, tostring(M.LIGHTS_NITS))
  m:setField('translucent', 0, '1')
  m:setField('translucentBlendOp', 0, 'Add')
  m:setField('translucentZWrite', 0, '0')
  m:setField('castShadows', 0, '0')
  m.canSave = false
  m:registerObject(name)
  return m
end

local function spawn(shape, name)
  local o = createObject('TSStatic')
  o:setField('shapeName', 0, shape)
  o:setField('collisionType', 0, 'None')
  o:setField('decalType', 0, 'None')
  o:setField('castShadows', 0, '0')
  o:setPosRot(0, 0, 0, 0, 0, 0, 1)
  o.canSave = false
  o:registerObject(name)
  if scenetree.MissionGroup then scenetree.MissionGroup:addObject(o) end
  return o
end

local function makeMaterial(name, color, opacity)
  local m = scenetree.findObject(name)
  if m then return m end
  m = createObject('Material')
  m:setField('mapTo', 0, name)
  m:setField('version', 0, '1.5')
  m:setField('baseColorMap', 0, color)
  m:setField('opacityMap', 0, opacity)
  m:setField('opacityMapUseUV', 0, '0')
  m:setField('translucent', 0, '1')
  m:setField('translucentZWrite', 0, '0')
  m:setField('castShadows', 0, '0')
  m:setField('roughnessFactor', 0, '1')
  m.canSave = false
  m:registerObject(name)
  return m
end

-- Pure: the great-circle distance (m) between two lat / lon points.
function M.distance(lat1, lon1, lat2, lon2)
  local r = math.pi / 180
  local a = math.sin((lat2 - lat1) * r / 2) ^ 2 + math.cos(lat1 * r) * math.cos(lat2 * r) * math.sin((lon2 - lon1) * r / 2) ^ 2
  return 2 * 6371000 * math.asin(math.min(1, math.sqrt(a)))
end

-- Pure: whether the shell is shown with the camera h m above sea level, given whether it is now (hysteresis).
function M.shownAt(h, now)
  h = tonumber(h)
  if not h then return false end
  if now then return h > M.HIDE_M end
  return h > M.SHOW_M
end

-- Pure: the shell's reach (m) for a camera h m above sea level (server spaceclouds.radius_for).
function M.reach(h)
  local R = 6371000
  return math.min(M.MAX_RADIUS_M, math.sqrt(2 * R * math.max(0, h or 0)) + math.sqrt(2 * R * M.SHELL_H_M))
end

-- Pure: whether to ask again: no shell, another anchor, moved MOVE_SHARE of its reach, REFRESH_S old, or the camera
-- (at h m) now sees GROW_SHARE past its edge.
function M.stale(s, anchorId, lat, lon, now, h)
  if not s then return true end
  if s.anchorId ~= anchorId then return true end
  if now - s.at >= M.REFRESH_S then return true end
  if h and M.reach(h) > M.GROW_SHARE * s.radius then return true end
  return M.distance(s.lat, s.lon, lat, lon) > M.MOVE_SHARE * s.radius
end

function M.update(dt, anchor, anchorId, send)
  clock = clock + dt
  timer = timer + dt
  if timer < M.CHECK_S then return end
  timer = 0
  local cam = rawget(_G, 'core_camera')
  local p = cam and cam.getPosition and cam.getPosition()
  if not p or not anchor then return end
  shown = M.shownAt(p.z, shown)
  local m3 = rawget(_G, 'beamngEarth_map3d')
  if m3 and m3.isActive and m3.isActive() then shown = false end      -- (no clouds over the 3D map)
  if shell and shell.obj then
    local hide = not shown or shell.anchorId ~= anchorId
    if shell.hidden ~= hide then
      shell.obj:setHidden(hide)
      if shell.lights then shell.lights:setHidden(hide) end
      shell.hidden = hide
    end
  end
  if not shown or not anchorId then return end
  local lat, lon = anchor:enuToGeodetic(p.x, p.y, 0)
  if not lat then return end
  if pending and clock - pending.at < M.RETRY_S then return end
  if M.stale(shell, anchorId, lat, lon, clock, p.z) then
    pending = { at = clock }
    -- (the game's own sun, whatever its clock: the night side's lights follow the sky drawn, a manual time of day too)
    local sky = scenetree.findObject('sunsky')
    local az = sky and tonumber(sky:getField('azimuth', 0))
    local el = sky and tonumber(sky:getField('elevation', 0))
    send({ type = 'space_clouds_req', anchorId = anchorId, lat = lat, lon = lon, alt = p.z, sunAz = az, sunEl = el })
  end
end

-- The server's 'space_clouds': the new shell (the old one goes once it is in).
function M.onMessage(msg, anchorId)
  pending = nil
  if msg.anchorId ~= anchorId or not FS:fileExists(msg.shape) then return end
  makeMaterial(msg.material, msg.color, msg.opacity)
  local o = spawn(msg.shape, 'bngEarthSpaceClouds_' .. tostring(msg.material))
  local lo = nil
  if msg.lightsShape and msg.lights and msg.lightsMaterial and FS:fileExists(msg.lightsShape) then
    makeLightsMaterial(msg.lightsMaterial, msg.lights)
    lo = spawn(msg.lightsShape, 'bngEarthSpaceLights_' .. tostring(msg.lightsMaterial))
  end
  deleteShell()
  shell = { obj = o, lights = lo, material = msg.material, lightsMaterial = lo and msg.lightsMaterial or nil,
            anchorId = msg.anchorId, lat = msg.lat, lon = msg.lon, radius = msg.radius or 1e6, at = clock, hidden = false }
  if not shown then
    o:setHidden(true)
    if lo then lo:setHidden(true) end
    shell.hidden = true
  end
  log('I', 'beamngEarth', string.format('space clouds: %.0f km round %.2f, %.2f', (msg.radius or 0) / 1000, msg.lat or 0,
    msg.lon or 0))
end

function M.clear()
  deleteShell()
  pending, shown, timer = nil, false, 0
end

M._state = function() return { shell = shell, pending = pending, shown = shown } end -- tests

return M
