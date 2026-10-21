-- BeamNG.Earth visual benchmark: the GE side of tools/benchmark.py (docs/DEVTOOLS.md "Visual
-- benchmark"). The tool sends this chunk through the game's MCP run_lua; there is no mod file.
-- Loading it defines the global BNGE_BENCH and returns 'ok'. Every function takes one JSON string
-- (or nothing) and returns JSON:
--   info()            the preconditions and everything restore() needs, read only
--   zeroInputs()      throttle / brake / steering / clutch 0, parking brake on (the car is parked)
--   teleport(cfg)     beamngEarth_core.teleport(cfg.lat, cfg.lon); {ok, anchorBefore}
--   progress()        the loading state: anchor, far-field stats, server backlog, the tile under the
--                     car (exact or not), the player
--   setTime(cfg)      the sky to manual and the TimeOfDay to cfg {time, year, month, day, utcOffset},
--                     the clock stopped (play = false)
--   setWeather(cfg)   the weather to manual and a fixed sky: cloud coverage, cirrus, fog for cfg.visM
--   view(cfg)         the free camera at cfg {e, n | lat, lon; alt | agl; dir {x, y, z}; fov}
--   perfStart()       a fresh beamngEarth_perfmon window (loaded if needed); perfRead() reads it
--   restore(saved)    camera (and the free camera's own zoom: the views set its FOV, which outlives a
--                     switch back to the game camera), sky, weather and perfmon as info() found them (saved = info().saved,
--                     passed back by the tool, so a Lua reload in between loses nothing); every part
--                     in its own pcall, the errors listed
-- Nothing here changes the world: the sky and weather objects' fields, the camera, the player's
-- inputs and (through the mod's own teleport) where the player is. The weather and sky go to their
-- 'manual' mode through their own setMode (not saved in the settings file) and the fields are
-- written directly (no core_environment call, so no onEnvironmentChanged hook fires).

local B = {}
_G.BNGE_BENCH = B

B.ZERO_INPUTS = "input.event('throttle', 0, 2); input.event('brake', 0, 2); input.event('steering', 0, 2); "
  .. "input.event('clutch', 0, 2); input.event('parkingbrake', 1, 2)"
B.RAY_UP = 6000          -- m above the player where the ground ray for an `agl` camera starts
B.RAY_LEN = 15000
B.CLOUD_NAME = 'bngEarthClouds'
B.FOG_HEIGHT = 1000      -- fogAtmosphereHeight of the clear-sky preset (weather.lua FOG_HEIGHT)
B.TOD_KEYS = { 'time', 'play', 'dayLength', 'year', 'month', 'day', 'utcOffset' }

local function toJson(v)
  local je = rawget(_G, 'jsonEncode')
  if je then return je(v) end
  return dumps(v)
end

local function decode(cfg)
  if type(cfg) == 'string' then return jsonDecode(cfg) end
  return cfg or {}
end

local function ext(name) return rawget(_G, name) end

local function firstOfClass(class)
  local names = scenetree.findClassObjects(class)
  return names and names[1] and scenetree.findObject(names[1]) or nil
end

local function cloudLayer() return scenetree.findObject(B.CLOUD_NAME) or firstOfClass('CloudLayer') end
local function levelInfo() return firstOfClass('LevelInfo') or scenetree.theLevelInfo end
local function postApply(o) if o and o.postApply then o:postApply() end end

local function farfield()
  local ok, ff = pcall(require, 'beamngEarth/farfield')
  return ok and type(ff) == 'table' and ff or nil
end

local function coreState()
  local core = ext('beamngEarth_core')
  if not core or not core._state then return nil end
  local ok, st = pcall(core._state)
  return ok and st or nil
end

local function isFree()
  local c = ext('commands')
  if c and c.isFreeCamera then
    local ok, v = pcall(c.isFreeCamera)
    if ok then return v and true or false end
  end
  local cam = ext('core_camera')
  if cam and cam.getActiveCamName then
    local ok, n = pcall(cam.getActiveCamName)
    if ok then return n == 'free' end
  end
  return false
end

local function camera()
  local cam = ext('core_camera')
  if not cam or not cam.getPositionXYZ then return nil end
  local ok, x, y, z = pcall(cam.getPositionXYZ)
  if not ok or type(x) ~= 'number' then return nil end
  local out = { x = x, y = y, z = z, free = isFree() }
  if cam.getQuat then
    local okQ, q = pcall(cam.getQuat)
    if okQ and q then out.rot = { x = q.x, y = q.y, z = q.z, w = q.w } end
  end
  if cam.getFovDeg then
    local okF, f = pcall(cam.getFovDeg)
    if okF and type(f) == 'number' then out.fov = f end
  end
  return out
end

local function vehicle() return getPlayerVehicle(0) end

-- The free camera's own zoom (core/cameraModes/free.lua: manualzoom {fov, fovDefault}, which
-- core_camera.setFOV changes while it is active and which outlives a switch back to the game camera).
local function freeZoom()
  local cam = ext('core_camera')
  if not cam or not cam.getGlobalCameras then return nil end
  local ok, cams = pcall(cam.getGlobalCameras)
  local z = ok and type(cams) == 'table' and cams.free and cams.free.manualzoom
  return type(z) == 'table' and z or nil
end

------------------------------------------------------------------------------------------
-- info: preconditions and the state to restore (no change to the game)

function B.info()
  local st = coreState()
  local veh = vehicle()
  local out = { mod = st ~= nil, level = getCurrentLevelIdentifier and getCurrentLevelIdentifier() or nil }
  local net = package.loaded['beamngEarth/net']
  if st then
    out.active, out.anchorId, out.session, out.pending = st.active, st.anchorId, st.sessionId,
      st.pending and st.pending.purpose or nil
    out.status = st.status
    if st.player then out.player = { lat = st.player.lat, lon = st.player.lon, z = st.player.z, speed = st.player.speed } end
  end
  if net and net.state then
    local ok, s = pcall(net.state)
    if ok then out.net = s end
  end
  if veh then
    local okV, v = pcall(function() return veh:getVelocity():length() end)
    out.speed = okV and v or nil
  end
  local ff = farfield()
  if ff and ff.stats then
    local ok, s = pcall(ff.stats)
    if ok then out.nearVisual = s.nearVisual end
  end
  out.perfmon = ext('beamngEarth_perfmon') ~= nil
  -- what restore() puts back
  local saved = { perfmonLoaded = out.perfmon }
  local cam = camera()
  if cam then
    saved.cam = { free = cam.free, rot = cam.rot, fov = cam.fov }
    local z = freeZoom()
    if z then saved.cam.freeFov, saved.cam.freeFovDefault = tonumber(z.fov), tonumber(z.fovDefault) end
    if veh then
      local p = veh:getPosition()
      saved.cam.rel = { x = cam.x - p.x, y = cam.y - p.y, z = cam.z - p.z } -- the frame changes on teleports
    end
  end
  local sky = ext('beamngEarth_sky')
  local tod = firstOfClass('TimeOfDay')
  saved.sky = { mode = sky and sky.getMode and sky.getMode() or nil }
  if tod then
    saved.sky.tod = {}
    for _, k in ipairs(B.TOD_KEYS) do
      local v = tod[k]
      if v ~= nil then saved.sky.tod[k] = (k == 'play') and (v == true or v == 1 or v == '1' or v == 'true') or tonumber(v) end
    end
  end
  local w = ext('beamngEarth_weather')
  saved.weather = { mode = w and w.getMode and w.getMode() or nil }
  local cl, li = cloudLayer(), levelInfo()
  if cl then saved.weather.cloud = { coverage = tonumber(cl.coverage), cirrusCoverage = tonumber(cl.cirrusCoverage) } end
  if li then saved.weather.fog = { fogDensity = tonumber(li.fogDensity), fogAtmosphereHeight = tonumber(li.fogAtmosphereHeight) } end
  out.saved = saved
  return toJson(out)
end

function B.zeroInputs()
  local veh = vehicle()
  if not veh then return toJson({ ok = false }) end
  local ok = pcall(veh.queueLuaCommand, veh, B.ZERO_INPUTS)
  return toJson({ ok = ok })
end

------------------------------------------------------------------------------------------
-- teleport and loading progress

function B.teleport(cfg)
  cfg = decode(cfg)
  local core = ext('beamngEarth_core')
  if not core or not core.teleport then return toJson({ ok = false, error = 'beamngEarth_core is not loaded' }) end
  local st = coreState()
  local before = st and st.anchorId or nil
  local ok, r = pcall(core.teleport, cfg.lat, cfg.lon, nil, { record = false }) -- not in the recent places
  return toJson({ ok = ok and r == true, anchorBefore = before, error = (not ok) and tostring(r) or nil,
    status = st and st.status or nil })
end

function B.progress()
  local st = coreState()
  if not st then return toJson({ mod = false }) end
  local out = { mod = true, anchorId = st.anchorId, pending = st.pending and st.pending.purpose or nil,
    status = st.status, serverBacklog = st.serverBacklog, active = st.active }
  if st.messages then out.messages = { queued = st.messages.queued, total = st.messages.total } end
  if st.player then
    out.player = { lat = st.player.lat, lon = st.player.lon, x = st.player.x, y = st.player.y, z = st.player.z,
      speed = st.player.speed }
  end
  local ff = farfield()
  if ff and ff.stats then
    local ok, s = pcall(ff.stats)
    if ok then out.far = s end
  end
  local roads = package.loaded['beamngEarth/roads']   -- DecalRoads (roads.lua): made a few per frame
  if roads and roads.status then
    local ok, s = pcall(roads.status)
    if ok then out.roads = { enabled = s.enabled, roads = s.roads, queued = s.queued, deleting = s.deleting, quietS = s.quietS,
      tiles = s.tiles, walksQueued = s.walksQueued } end
  end
  return toJson(out)
end

------------------------------------------------------------------------------------------
-- sky and weather

local function writeTod(tod, fields)
  for _, k in ipairs(B.TOD_KEYS) do
    local v = fields[k]
    if v ~= nil then tod[k] = v end
  end
end

function B.setTime(cfg)
  cfg = decode(cfg)
  local tod = firstOfClass('TimeOfDay')
  if not tod then return toJson({ ok = false, error = 'no TimeOfDay' }) end
  local sky = ext('beamngEarth_sky')
  if sky and sky.setMode then sky.setMode('manual') end
  writeTod(tod, { time = cfg.time, year = cfg.year, month = cfg.month, day = cfg.day, utcOffset = cfg.utcOffset,
    play = false })
  postApply(tod)
  return toJson({ ok = true, sky = sky and sky.status and sky.status() or nil })
end

-- cfg: { coverage, cirrus, visM } (the 'clear' preset of tools/benchmark.py)
function B.setWeather(cfg)
  cfg = decode(cfg)
  local w = ext('beamngEarth_weather')
  if w and w.setMode then w.setMode('manual') end -- hides rain / snow and gives the sun its own brightness back
  local out = { ok = true }
  local cl = cloudLayer()
  if cl then
    cl.coverage = cfg.coverage or 0
    cl.cirrusCoverage = cfg.cirrus or 0
    postApply(cl)
    out.coverage = tonumber(cl.coverage)
  end
  local li = levelInfo()
  if li then
    local st = coreState()
    local groundZ = st and st.player and st.player.z or 0
    local sigma = (w and w.fogSigma) and w.fogSigma(cfg.visM) or 3.912 / (cfg.visM or 60000)
    local d = (w and w.fogDensityFor) and w.fogDensityFor(sigma, groundZ, B.FOG_HEIGHT)
      or math.min(sigma * math.exp(groundZ / B.FOG_HEIGHT), 0.05)
    li.fogDensity = d
    li.fogAtmosphereHeight = B.FOG_HEIGHT
    postApply(li)
    out.fogDensity = d
  end
  return toJson(out)
end

------------------------------------------------------------------------------------------
-- camera

-- Ground height under (x, y): the static collision (the TerrainBlock; the far tiles do not
-- collide), else nil.
-- The ground under (x, y): the drawn tiles first (castRay with render geometry: the mesh tiles have
-- no collision, and the physics block only covers ~1 km around the player - outside it the static
-- ray found nothing and an aerial camera 600 m back went inside a valley wall, in-game 2026-10-11),
-- then the static collision.
-- A hit counts when the surface is flat around it (FLAT_M each way within FLAT_DZ): a bridge's truss cross beams,
-- arch ribs and hangers over the road are skipped and the ray goes on down to the deck (the street view
-- comparison's cameras stood on the Queensboro Bridge's top chords, 2026-10-18).
B.FLAT_M, B.FLAT_DZ = 1.5, 0.6
local function flatAt(x, y, z)
  for _, o in ipairs({ { B.FLAT_M, 0 }, { -B.FLAT_M, 0 }, { 0, B.FLAT_M }, { 0, -B.FLAT_M } }) do
    local ok, h = pcall(castRay, vec3(x + o[1], y + o[2], z + 1.0), vec3(x + o[1], y + o[2], z - 1.5), true, true)
    if not (ok and type(h) == 'table' and h.pt and h.pt.z and math.abs(h.pt.z - z) < B.FLAT_DZ) then return false end
  end
  return true
end

-- under: the flat surface nearest the player's car instead (a teleport into a tunnel puts it on the floor: the
-- first surface was the tube's roof, the camera in the rock; Tower Bridge's approach over a passage: its deck)
local function groundAt(x, y, zRef, under)
  local top = zRef + B.RAY_UP
  local best
  for _ = 1, 24 do
    local okR, hit = pcall(castRay, vec3(x, y, top), vec3(x, y, zRef - B.RAY_LEN), true, true)
    if not (okR and type(hit) == 'table' and hit.pt and hit.pt.z) then break end
    if flatAt(x, y, hit.pt.z) then
      if not under then return hit.pt.z end
      if not best or math.abs(hit.pt.z - zRef) < math.abs(best - zRef) then best = hit.pt.z end
    end
    top = hit.pt.z - 0.3
  end
  if best then return best end
  local ok, d = pcall(castRayStatic, vec3(x, y, top), vec3(0, 0, -1), B.RAY_LEN)
  if ok and d and d < B.RAY_LEN then return top - d end
end

-- The player's vehicle is hidden while a view is shot (the 'ground' view stands where it is) and
-- shown again by the restore.
local hiddenVeh = nil
local function hidePlayer()
  local v = vehicle()
  if v and not hiddenVeh then
    local ok, id = pcall(function() return v:getID() end)
    if ok and id and pcall(function() v:setMeshAlpha(0, '', false) end) then hiddenVeh = id end
  end
end
local function showPlayer()
  if not hiddenVeh then return end
  local v = getObjectByID and getObjectByID(hiddenVeh)
  if v then pcall(function() v:setMeshAlpha(1, '', false) end) end
  hiddenVeh = nil
end

-- cfg: { e, n } (m east / north of the anchor = the place) or { lat, lon }; { alt } (m: world z with
-- e / n, the height above the ellipsoid with lat / lon) or { agl } (m above the ground there, else above the player's ground); dir { x, y, z }; fov.
function B.view(cfg)
  cfg = decode(cfg)
  local st = coreState()
  if not st or not st.anchor then return toJson({ ok = false, error = 'no anchor' }) end
  local x, y, zGeo = cfg.e or 0, cfg.n or 0, nil
  if cfg.lat and cfg.lon then
    x, y, zGeo = st.anchor:geodeticToEnu(cfg.lat, cfg.lon, cfg.alt or 0)
  end
  local pz = st.player and st.player.z or 0
  local z, ground, groundFrom
  if cfg.alt then
    z = zGeo or cfg.alt -- lat/lon/alt: the ENU height (the Earth curves away from the anchor)
  else
    ground = groundAt(x, y, pz, cfg.under)
    groundFrom = ground and 'ray' or 'player'
    z = (ground or pz) + (cfg.agl or 2)
  end
  local d = cfg.dir or { x = 0, y = 1, z = 0 }
  hidePlayer()
  commands.setFreeCamera()
  core_camera.setPosition(0, vec3(x, y, z))
  core_camera.setRotation(0, quatFromDir(vec3(d.x, d.y, d.z):normalized(), vec3(0, 0, 1)))
  if cfg.fov and core_camera.setFOV then core_camera.setFOV(0, cfg.fov) end
  return toJson({ ok = true, x = x, y = y, z = z, ground = ground, groundFrom = groundFrom, cam = camera() })
end

------------------------------------------------------------------------------------------
-- frame times (beamngEarth_perfmon, the mod's dev sampler)

function B.perfStart()
  if not ext('beamngEarth_perfmon') then extensions.load('beamngEarth_perfmon') end
  local p = ext('beamngEarth_perfmon')
  if not p then return toJson({ ok = false }) end
  p.snapshot()
  return toJson({ ok = true })
end

function B.perfRead()
  local p = ext('beamngEarth_perfmon')
  if not p then return toJson({ ok = false }) end
  local s = p.snapshot()
  s.worst = nil
  s.ok = true
  -- Report the actual engine policy: background caps differ between installations.
  local okL, lim = pcall(function() return Engine.getCurrentFpsLimitReason() end)
  s.focused = not (okL and type(lim) == 'table' and lim.windowFocused == false)
  if okL and type(lim) == 'table' then s.fpsLimit = lim end
  return toJson(s)
end

------------------------------------------------------------------------------------------
-- restore

local function restoreFreeZoom(c)
  local z = freeZoom()
  if not z or not c then return end
  if c.freeFov then z.fov = c.freeFov end
  if c.freeFovDefault then z.fovDefault = c.freeFovDefault end
end

local function restoreCamera(c)
  showPlayer()
  if not c or not c.free then
    commands.setGameCamera()
    restoreFreeZoom(c) -- the views zoomed the free camera; the user meets it again on its next toggle
    return 'game'
  end
  -- the user had the free camera: put it back at the same place relative to the player
  local veh = vehicle()
  commands.setFreeCamera()
  if veh and c.rel then
    local p = veh:getPosition()
    core_camera.setPosition(0, vec3(p.x + c.rel.x, p.y + c.rel.y, p.z + c.rel.z))
  end
  if c.rot then core_camera.setRotation(0, quat(c.rot.x, c.rot.y, c.rot.z, c.rot.w)) end
  if c.fov and core_camera.setFOV then core_camera.setFOV(0, c.fov) end
  restoreFreeZoom(c) -- setFOV also made the FOV the zoom's default
  return 'free'
end

local function restoreSky(s)
  local tod = firstOfClass('TimeOfDay')
  if tod and s.tod then
    writeTod(tod, s.tod)
    postApply(tod)
  end
  local sky = ext('beamngEarth_sky')
  if sky and sky.setMode and s.mode then sky.setMode(s.mode) end -- 'real' re-syncs on its next update
  return s.mode
end

local function restoreWeather(w)
  local cl, li = cloudLayer(), levelInfo()
  if cl and w.cloud then
    if w.cloud.coverage then cl.coverage = w.cloud.coverage end
    if w.cloud.cirrusCoverage then cl.cirrusCoverage = w.cloud.cirrusCoverage end
    postApply(cl)
  end
  if li and w.fog then
    if w.fog.fogDensity then li.fogDensity = w.fog.fogDensity end
    if w.fog.fogAtmosphereHeight then li.fogAtmosphereHeight = w.fog.fogAtmosphereHeight end
    postApply(li)
  end
  local we = ext('beamngEarth_weather')
  if we and we.setMode and w.mode then we.setMode(w.mode) end
  return w.mode
end

-- parts: which to restore ({ cam = true, sky = true, weather = true, perfmon = true }; nil = all)
function B.restore(saved, parts)
  saved = decode(saved)
  parts = parts and decode(parts) or { cam = true, sky = true, weather = true, perfmon = true }
  local out, errors = {}, {}
  local function step(name, fn, arg)
    if not parts[name] or arg == nil then return end
    local ok, r = pcall(fn, arg)
    if ok then out[name] = r else errors[#errors + 1] = name .. ': ' .. tostring(r) end
  end
  step('weather', restoreWeather, saved.weather)
  step('sky', restoreSky, saved.sky)
  step('perfmon', function(loaded)
    if not loaded and ext('beamngEarth_perfmon') then extensions.unload('beamngEarth_perfmon') end
    return loaded
  end, saved.perfmonLoaded)
  -- the camera last, and always (also without a saved camera: the game camera)
  if parts.cam then step('cam', restoreCamera, saved.cam or { free = false }) end
  out.errors = errors
  out.ok = #errors == 0
  return toJson(out)
end

return 'ok'
