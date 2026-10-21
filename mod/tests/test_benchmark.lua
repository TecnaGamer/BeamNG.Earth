-- luajit mod/tests/test_benchmark.lua  (tools/benchmark.lua, docs/DEVTOOLS.md "Visual benchmark")
-- The benchmark's GE chunk against stubs.lua plus a fake camera, core and far field, with the mod's
-- real sky.lua, weather.lua and geodesy.lua: info() saves exactly what restore() needs, setTime /
-- setWeather / view change only what they say, and restore() puts back every value and mode, the
-- game camera or the user's free camera (relative to the car, which a teleport moves), also when one
-- part fails.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local S = dofile(dir .. '/stubs.lua')
local CHUNK = dir .. '/../../tools/benchmark.lua'

function jsonEncode(v) return v end -- answers stay tables here
function jsonDecode(s) error('the test passes tables') end

local geodesy = require('beamngEarth/geodesy')
local sky = require('beamngEarth/sky')
local weather = require('beamngEarth/weather')
beamngEarth_sky, beamngEarth_weather = sky, weather

-- level objects
local function obj(class, name, fields)
  local o = S.newObject(class)
  for k, v in pairs(fields) do o[k] = v end
  o.applied = 0
  function o:postApply() self.applied = self.applied + 1 end
  o:registerObject(name)
  return o
end
local tod = obj('TimeOfDay', 'tod', { time = 0.1, play = true, dayLength = 86400, year = 2026, month = 10, day = 11,
  utcOffset = -7 })
local cloud = obj('CloudLayer', 'bngEarthClouds', { coverage = 0.8, cirrusCoverage = 0.3 })
local level = obj('LevelInfo', 'theLevelInfo', { fogDensity = 0.002, fogAtmosphereHeight = 400 })

-- the player
local veh = { pos = vec3(3, 4, 1500), vel = vec3(0.2, 0, 0), cmds = {} }
function veh:getPosition() return vec3(self.pos.x, self.pos.y, self.pos.z) end
function veh:getVelocity() return vec3(self.vel.x, self.vel.y, self.vel.z) end
function veh:queueLuaCommand(c) self.cmds[#self.cmds + 1] = c end
function getPlayerVehicle() return veh end
function veh:getID() return 42 end
veh.alpha = 1
function veh:setMeshAlpha(a) self.alpha = a end
function getObjectByID(id) return id == 42 and veh or nil end

-- camera (core_camera / commands, as camera.lua and server/commands.lua)
local cam = { name = 'chase', pos = vec3(3, -2, 1503), rot = quat(0.1, 0.2, 0.3, 0.9), fov = 75, sets = 0 }
core_camera = {
  getPositionXYZ = function() return cam.pos.x, cam.pos.y, cam.pos.z end,
  getQuat = function() return quat(cam.rot.x, cam.rot.y, cam.rot.z, cam.rot.w) end,
  getFovDeg = function() return cam.fov end,
  getActiveCamName = function() return cam.name end,
  setPosition = function(_, p) if cam.name == 'free' then cam.pos = vec3(p.x, p.y, p.z); cam.sets = cam.sets + 1 end end,
  setRotation = function(_, q) if cam.name == 'free' then cam.rot = quat(q.x, q.y, q.z, q.w) end end,
  setFOV = function(_, f) if cam.name == 'free' then cam.fov = f; core_camera._free:setFOV(f) end end,
}
-- the free camera keeps its own FOV while another camera is active (cameraModes/free.lua: manualzoom,
-- set by C:setFOV -> manualzoom:init(fov), which also makes it the default)
core_camera._free = { manualzoom = { fov = 60, fovDefault = 60 } }
function core_camera._free:setFOV(f) self.manualzoom.fov, self.manualzoom.fovDefault = f, f end
core_camera.getGlobalCameras = function() return { free = core_camera._free } end
commands = {
  setFreeCamera = function() cam.name = 'free' end,
  setGameCamera = function() cam.name = 'chase'; cam.fov = 75 end,
  isFreeCamera = function() return cam.name == 'free' end,
}
function quatFromDir(d) return quat(d.x, d.y, d.z, 0) end -- the direction, recognisable in the test

-- static collision: ground 1490 + 0.01 x inside |x|, |y| <= 1024 (the TerrainBlock), nothing beyond
function castRayStatic(pos, _, len)
  if math.abs(pos.x) > 1024 or math.abs(pos.y) > 1024 then return len end
  return pos.z - (1490 + 0.01 * pos.x)
end

-- the mod: core state, far field, net
local teleports = {}
local st = { active = true, anchorId = 4, sessionId = 's1', status = 'Far field ready (900 tiles)',
  anchor = geodesy.newAnchor(36.5785, -118.2923),
  player = { lat = 36.5785, lon = -118.2923, x = 3, y = 4, z = 1500, speed = 0.2 } }
beamngEarth_core = {
  _state = function() return st end,
  teleport = function(lat, lon) teleports[#teleports + 1] = { lat, lon }; return true end,
}
local farStats = { live = 900, queued = 0, done = true, nearVisual = 'mesh', underLevel = -2, exactLevel = -2,
  exactMiss = 3, exactFrames = 100, imports = 1200 }
package.loaded['beamngEarth/farfield'] = { stats = function() return farStats end }
package.loaded['beamngEarth/net'] = { state = function() return 'connected' end }

-- perfmon through extensions.load / unload
local loaded = {}
extensions = {
  load = function(n) loaded[n] = true; _G[n] = { snapshot = function() return { fps = 60, p95 = 18, frames = 300, worst = { { 40, 1 } } } end } end,
  unload = function(n) loaded[n] = nil; _G[n] = nil end,
}

------------------------------------------------------------------------------------------
T.check(dofile(CHUNK) == 'ok', 'the chunk loads and answers ok')
local B = BNGE_BENCH
T.check(type(B) == 'table' and B.info and B.restore, 'BNGE_BENCH defined')

-- info: preconditions and the saved state, nothing changed
sky.setMode('real'); weather.setMode('real')
local info = B.info()
T.check(info.mod and info.active and info.net == 'connected' and info.nearVisual == 'mesh', 'info: mod, net, mode')
T.near(info.speed, 0.2, 1e-9, 'info: speed')
T.check(info.player.lat == 36.5785 and info.player.lon == -118.2923, 'info: the player lat/lon (home)')
local sv = info.saved
T.check(sv.cam.free == false and sv.cam.fov == 75 and sv.cam.rot.w == 0.9, 'saved: game camera, fov, rot')
T.near(sv.cam.rel.y, -6, 1e-9, 'saved: camera relative to the car')
T.check(sv.sky.mode == 'real' and sv.sky.tod.time == 0.1 and sv.sky.tod.play == true and sv.sky.tod.utcOffset == -7,
  'saved: sky mode and TimeOfDay')
T.check(sv.weather.mode == 'real' and sv.weather.cloud.coverage == 0.8 and sv.weather.fog.fogDensity == 0.002
  and sv.weather.fog.fogAtmosphereHeight == 400, 'saved: weather mode, clouds, fog')
T.check(sv.perfmonLoaded == false, 'saved: perfmon was not loaded')
T.check(tod.applied == 0 and cloud.applied == 0 and level.applied == 0 and cam.name == 'chase', 'info changes nothing')

-- inputs
B.zeroInputs()
T.check(veh.cmds[1] and veh.cmds[1]:find("'throttle', 0") and veh.cmds[1]:find("'parkingbrake', 1"),
  'zeroInputs: throttle 0, parking brake on')

-- teleport / progress
local tp = B.teleport({ lat = 46.5935, lon = 7.9089 })
T.check(tp.ok and tp.anchorBefore == 4 and teleports[1][1] == 46.5935 and teleports[1][2] == 7.9089, 'teleport')
local pr = B.progress()
T.check(pr.mod and pr.anchorId == 4 and pr.far.live == 900 and pr.far.exactMiss == 3 and pr.player.z == 1500,
  'progress: anchor, far stats, player')

-- time: manual, the fields, the clock stopped
local r = B.setTime({ time = 0.916667, year = 2026, month = 9, day = 22, utcOffset = -7.8862 })
T.check(r.ok and sky.getMode() == 'manual', 'setTime: sky manual')
T.check(tod.time == 0.916667 and tod.month == 9 and tod.day == 22 and tod.utcOffset == -7.8862 and tod.play == false
  and tod.dayLength == 86400, 'setTime: TimeOfDay fields, play off, dayLength kept')

-- weather: manual, clear, fog for 60 km at the player's ground
r = B.setWeather({ coverage = 0, cirrus = 0, visM = 60000 })
T.check(weather.getMode() == 'manual' and cloud.coverage == 0 and cloud.cirrusCoverage == 0, 'setWeather: clear')
T.near(level.fogDensity, weather.fogDensityFor(weather.fogSigma(60000), 1500, 1000), 1e-15, 'setWeather: fog')
T.check(level.fogAtmosphereHeight == 1000 and level.applied == 1 and cloud.applied == 1, 'setWeather: applied once')

-- views
r = B.view({ e = 10, n = -12, agl = 2, dir = { x = 0, y = 1, z = 0 }, fov = 65 })
T.check(r.ok and cam.name == 'free' and r.groundFrom == 'ray', 'view: free camera, ground from the ray')
T.near(cam.pos.z, 1490 + 0.1 + 2, 1e-9, 'view agl: 2 m above the ray ground')
T.check(cam.pos.x == 10 and cam.pos.y == -12 and cam.fov == 65 and cam.rot.y == 1, 'view: position, fov, direction')
T.check(veh.alpha == 0, 'view: the player\'s vehicle is hidden while shooting')
r = B.view({ e = 5000, n = 0, agl = 30, dir = { x = 1, y = 0, z = 0 } })
T.check(r.groundFrom == 'player' and cam.pos.z == 1530, 'view: beyond the block, above the player ground')
r = B.view({ e = 0, n = -12, alt = 4416.5, dir = { x = 0, y = 1, z = -0.3 }, fov = 65 })
T.check(cam.pos.z == 4416.5 and r.groundFrom == nil, 'view alt: the given height')
local lat, lon, h = st.anchor:enuToGeodetic(200, -300, 1600)
r = B.view({ lat = lat, lon = lon, alt = h, dir = { x = 0, y = 1, z = 0 } })
T.near(cam.pos.x, 200, 1e-6, 'view lat/lon: east')
T.near(cam.pos.y, -300, 1e-6, 'view lat/lon: north')
T.near(cam.pos.z, 1600, 1e-6, 'view lat/lon/alt: the ENU height')

-- perfmon: loaded for the run, unloaded by restore (it was not loaded before)
T.check(B.perfStart().ok and loaded.beamngEarth_perfmon, 'perfStart loads perfmon')
local pf = B.perfRead()
T.check(pf.ok and pf.fps == 60 and pf.worst == nil and pf.focused == true, 'perfRead: the snapshot without worst')
Engine = { getCurrentFpsLimitReason = function() return { windowFocused = false, fpsCap = 60 } end }
pf = B.perfRead()
T.check(pf.focused == false and pf.fpsLimit.fpsCap == 60, 'perfRead: the actual background cap and window focus')
Engine = nil

-- restore: weather, sky, perfmon first; the camera separately
r = B.restore(sv, { weather = true, sky = true, perfmon = true })
T.check(r.ok and #r.errors == 0, 'restore: no errors')
T.check(sky.getMode() == 'real' and weather.getMode() == 'real', 'restore: both modes back')
T.check(tod.time == 0.1 and tod.play == true and tod.month == 10 and tod.day == 11 and tod.utcOffset == -7,
  'restore: TimeOfDay back')
T.check(cloud.coverage == 0.8 and cloud.cirrusCoverage == 0.3 and level.fogDensity == 0.002
  and level.fogAtmosphereHeight == 400, 'restore: clouds and fog back')
T.check(not loaded.beamngEarth_perfmon and beamngEarth_perfmon == nil, 'restore: perfmon unloaded')
T.check(cam.name == 'free', 'restore without cam leaves the camera')
T.check(core_camera._free.manualzoom.fov == 65, 'the views left their FOV on the free camera')
r = B.restore(sv, { cam = true })
T.check(veh.alpha == 1, 'restore cam: the player\'s vehicle is shown again')
T.check(r.ok and r.cam == 'game' and cam.name == 'chase', 'restore cam: the game camera')
T.check(sv.cam.freeFov == 60 and core_camera._free.manualzoom.fov == 60 and core_camera._free.manualzoom.fovDefault == 60,
  'restore cam: the free camera has its own FOV back (the user had the game camera)')

-- the user had the free camera: back at the same place relative to the car, after the car moved
commands.setFreeCamera(); core_camera.setPosition(0, vec3(13, 4, 1520)); core_camera.setRotation(0, quat(0, 0, 0.6, 0.8))
core_camera.setFOV(0, 50)
local sv2 = B.info().saved
T.check(sv2.cam.free == true and sv2.cam.fov == 50, 'saved: free camera')
B.view({ e = 0, n = 0, agl = 5, dir = { x = 0, y = 1, z = 0 }, fov = 70 })
veh.pos = vec3(-100, 50, 900) -- the teleport home put the car elsewhere in the new frame
r = B.restore(sv2, { cam = true })
T.check(r.cam == 'free' and cam.name == 'free', 'restore cam: free again')
T.check(cam.pos.x == -90 and cam.pos.y == 50 and cam.pos.z == 920 and cam.rot.z == 0.6 and cam.fov == 50,
  'restore cam: same offset from the car, rotation, fov')
commands.setGameCamera()

-- a failing part: listed, the others (and the camera, last) still restored
B.setTime({ time = 0.5 }); B.setWeather({ coverage = 0, visM = 60000 }); B.view({ agl = 2, dir = { x = 0, y = 1, z = 0 } })
local realSet = sky.setMode
sky.setMode = function(m) if m == 'real' then error('boom') end return realSet(m) end
r = B.restore(sv)
sky.setMode = realSet
T.check(not r.ok and #r.errors == 1 and r.errors[1]:find('sky') and r.errors[1]:find('boom'), 'restore: the sky error listed')
T.check(weather.getMode() == 'real' and cloud.coverage == 0.8 and cam.name == 'chase', 'restore: weather and camera anyway')
T.check(tod.time == 0.1, 'restore: the TimeOfDay fields went back before the failing mode switch')

-- no anchor: view refuses; no core: teleport refuses
local saveAnchor = st.anchor
st.anchor = nil
T.check(B.view({ agl = 2 }).ok == false, 'view without an anchor refuses')
st.anchor = saveAnchor
local core = beamngEarth_core
beamngEarth_core = nil
T.check(B.teleport({ lat = 1, lon = 2 }).ok == false and B.progress().mod == false and B.info().mod == false,
  'without the mod: teleport refuses, progress / info say so')
beamngEarth_core = core

-- loading the chunk again replaces the table (a fresh BNGE_BENCH)
T.check(dofile(CHUNK) == 'ok' and BNGE_BENCH ~= B, 'reload gives a fresh table')

T.done('test_benchmark')
