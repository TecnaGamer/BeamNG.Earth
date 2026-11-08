-- BeamNG.Earth: the whole earth for the 3D map's far-out views (server globe.py; the user, 2026-11-07: "zoom out far
-- enough to view the entire earth like on google earth"). The far tiles reach 2600 km and the game's sky goes wrong
-- above ~600 km (objects black over its shell), so far out the 3D map draws one sphere of the world: asked once per
-- anchor while the map's camera is high, shown above SHOW_M (hidden again under HIDE_M), lit by its own texture
-- (emissive: no atmosphere lighting up there); the view distance reaches past the far side of the earth
-- (horizon.extraVisible). Past earthGlobe's MINI_ALT a small copy of it stands in front of the camera instead (the
-- sky's shell draws everything black above 600 km) and the sky is hidden: space is black.

local M = {}

-- (on again, 2026-11-07, as the backdrop only: the real camera and the real far tiles all the way up (the sky shader
-- patched), the sphere 3 km under sea level showing where the tiles do not reach and past Web Mercator's poles; the
-- small copy in front of a capped camera, which the user did not like, stays off: earthGlobe MINI_ALT)
M.ENABLED = true

M.SHOW_M = 250000           -- the camera's height above sea level
M.HIDE_M = 200000
M.LOW_TOO = true          -- shown under SHOW_M too while the map is open
M.NITS = 1500               -- the texture's brightness (emissive)
M.CHECK_S = 0.25
M.RETRY_S = 30
M.R = 6371000

local globe = nil           -- { obj, mini, anchorId, material }: the globe and its small copy (far out)
local pending = nil         -- { at, anchorId }
local shown, skyOff, high = false, false, false
local timer, clock = 0, 0

local function horizon() return require('beamngEarth/horizon') end

local function makeMaterial(name, texture)
  local m = scenetree.findObject(name)
  if m then
    m:setField('emissiveMap', 0, texture)
    m:setField('baseColorMap', 0, texture)
    if m.reload then pcall(function() m:reload() end) end
    return m
  end
  m = createObject('Material')
  m:setField('mapTo', 0, name)
  m:setField('version', 0, '1.5')
  m:setField('baseColorMap', 0, texture)
  m:setField('baseColorFactor', 0, '0 0 0 1')
  m:setField('emissiveMap', 0, texture)
  m:setField('emissiveFactor', 0, '1 1 1')
  m:setField('emissiveIntensityNits', 0, tostring(M.NITS))
  m:setField('roughnessFactor', 0, '1')
  m:setField('castShadows', 0, '0')
  m.canSave = false
  m:registerObject(name)
  return m
end

local function deleteGlobe()
  if not globe then return end
  pcall(function() globe.obj:delete() end)
  if globe.mini then pcall(function() globe.mini:delete() end) end
  globe = nil
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

-- The sky's brightness faded out as the camera nears the copy's height (the user, 2026-11-07: "popping between
-- modes": the sky went from blue to black at once): full below SKY_FADE_FROM_M, none at SKY_FADE_TO_M (the copy's).
M.SKY_FADE_FROM_M = 330000
M.SKY_FADE_TO_M = 480000
local skyBright = nil       -- { id, value }: the sky's own brightness before the fade

local function fadeSky(h)
  local s = scenetree.findObject('sunsky')
  if not s then return end
  if not skyBright or skyBright.id ~= s:getID() then
    skyBright = { id = s:getID(), value = tonumber(s:getField('skyBrightness', 0)) or 40 }
  end
  local t = 0
  if h then t = math.max(0, math.min(1, (h - M.SKY_FADE_FROM_M) / (M.SKY_FADE_TO_M - M.SKY_FADE_FROM_M))) end
  local want = skyBright.value * (1 - t)
  local cur = tonumber(s:getField('skyBrightness', 0))
  if not cur or math.abs(cur - want) > 0.05 then s:setField('skyBrightness', 0, tostring(want)) end
end

local function setSky(off)
  if skyOff == off then return end
  skyOff = off
  for _, cls in ipairs({ 'ScatterSky', 'CloudLayer' }) do
    for _, n in ipairs(scenetree.findClassObjects(cls) or {}) do
      local o = scenetree.findObject(n)
      if o then pcall(function() o:setHidden(off) end) end
    end
  end
end

-- Pure: shown or not with the camera h m above sea level, given whether it is now (hysteresis).
function M.shownAt(h, now)
  h = tonumber(h)
  if not h then return false end
  if now then return h > M.HIDE_M end
  return h > M.SHOW_M
end

-- Pure: the view distance (m) that reaches the far side of the earth from h m up.
function M.visibleFor(h)
  return (tonumber(h) or 0) + 2 * M.R + 100000
end

function M.update(dt, anchor, anchorId, send)
  if not M.ENABLED then return end
  clock = clock + dt
  timer = timer + dt
  if timer < M.CHECK_S then return end
  timer = 0
  local m3 = rawget(_G, 'beamngEarth_map3d')
  local active = m3 and m3.isActive and m3.isActive()
  local p = core_camera and core_camera.getPosition and core_camera.getPosition()
  local h = nil
  if p and anchor then
    local _, _, hh = anchor:enuToGeodetic(p.x, p.y, p.z)
    h = hh
  end
  -- (any camera that high, not only the 3D map's: a car flown 20,000 km up saw only the sky's blue shell, no earth;
  -- the user, 2026-11-08: "the earth just looks like a blue ball this high up")
  high = M.shownAt(h, high)
  -- (low down too while the map is open: the earth's photo under a place whose tiles have not come yet, not the
  -- empty sky; the far tiles cover it, 3 km over it, where they are. 2026-11-07: a flight to a fresh city landed on
  -- blue for 5-10 s)
  shown = high or (active and M.LOW_TOO and h ~= nil) or false
  -- far out the camera mode hands over a copy's place (earthGlobe C.MINI_*): the copy shown, the globe hidden
  local g = active and core_camera.getGlobalCameras()[m3.CAMERA] or nil
  local mini = shown and g and g.mini or nil
  if globe then
    local hide = not shown or (globe.anchorId ~= anchorId and globe.followed ~= anchorId) or mini ~= nil
    if globe.hidden ~= hide then
      globe.obj:setHidden(hide)
      globe.hidden = hide
    end
    if globe.mini then
      if mini and globe.anchorId == anchorId then
        globe.mini:setPosRot(mini.Om.x, mini.Om.y, mini.Om.z, 0, 0, 0, 1)    -- (the small mesh is about its centre)
        if globe.miniHidden then globe.mini:setHidden(false); globe.miniHidden = false end
      elseif not globe.miniHidden then
        globe.mini:setHidden(true)
        globe.miniHidden = true
      end
    end
  end
  setSky(mini ~= nil)
  -- (the sky shader patched: its own atmosphere from space, the limb and black space past it; else the old shell)
  if horizon().skyPatched then
    fadeSky(nil)
    horizon().atmoOverrideKm = nil
  else
    fadeSky(active and h or nil)
    horizon().atmoOverrideKm = (active and h and h > 60000) and horizon().ATMO_MAX_KM or nil
  end
  local hz = horizon()
  local want = high and M.visibleFor(h) or nil
  if want ~= hz.extraVisible then
    hz.extraVisible = want
    if want then hz.setVisibleDistance(math.max(want, (scenetree.theLevelInfo and scenetree.theLevelInfo.visibleDistance) or 0)) end
  end
  if not shown or not anchorId then return end
  if globe and globe.anchorId == anchorId then return end
  if pending and pending.anchorId == anchorId and clock - pending.at < M.RETRY_S then return end
  pending = { at = clock, anchorId = anchorId }
  send({ type = 'globe_req', anchorId = anchorId })
end

-- The server's 'globe': the sphere for this anchor (the old one goes).
function M.onMessage(msg, anchorId)
  pending = nil
  if msg.anchorId ~= anchorId or not FS:fileExists(msg.shape) then return end
  makeMaterial(msg.material, msg.texture)
  local o = spawn(msg.shape, 'bngEarthGlobe_a' .. tostring(msg.anchorId))
  local mo = msg.miniShape and FS:fileExists(msg.miniShape) and spawn(msg.miniShape, 'bngEarthGlobeMini_a' .. tostring(msg.anchorId)) or nil
  local cam = core_camera.getGlobalCameras()['earthGlobe']
  if cam and tonumber(msg.miniScale) then getmetatable(cam).miniScale = tonumber(msg.miniScale) end
  if mo then mo:setHidden(true) end
  deleteGlobe()
  globe = { obj = o, mini = mo, miniHidden = true, anchorId = msg.anchorId, material = msg.material, hidden = false }
  if not shown then
    o:setHidden(true)
    globe.hidden = true
  end
  log('I', 'beamngEarth', 'globe: anchor ' .. tostring(msg.anchorId))
end

-- Origin rebase (core, beside farfield.transform): the sphere moved into the new frame (T_new = R*T_old + t) and kept
-- for the new anchor, so it never goes while a flight's new place loads (the user, 2026-11-07: "the earth rendering
-- can't keep up": hidden at the flight's start until the new anchor's sphere came back).
function M.transform(xf, newAnchorId)
  if not globe or not xf then return end
  local geodesy = require('beamngEarth/geodesy')
  local p = globe.pos or {0, 0, 0}
  local q = globe.rot or {0, 0, 0, 1}
  local x, y, z = xf:apply(p[1], p[2], p[3])
  local bx, by, bz, bw = xf:beamngQuat()
  local qx, qy, qz, qw = geodesy.quatMul(q[1], q[2], q[3], q[4], bx, by, bz, bw)
  globe.pos, globe.rot = {x, y, z}, {qx, qy, qz, qw}
  pcall(function() globe.obj:setPosRot(x, y, z, qx, qy, qz, qw) end)
  -- shown for the new anchor meanwhile, its own (exact) sphere asked for at once: moved again and again it drifted
  -- off the tiles (the user, 2026-11-07: "the backddrop earth got offset")
  globe.followed = newAnchorId
  pending = nil
end

function M.clear()
  deleteGlobe()
  setSky(false)
  fadeSky(nil)
  horizon().atmoOverrideKm = nil
  horizon().extraVisible = nil
  pending, shown, timer, high = nil, false, 0, false
end

M._state = function() return { globe = globe, pending = pending, shown = shown, skyOff = skyOff } end -- tests

return M
