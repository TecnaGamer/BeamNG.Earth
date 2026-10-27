-- BeamNG.Earth: real weather at the player. GE extension 'beamngEarth_weather' (loaded by
-- scripts/beamng_earth_weather/modScript.lua). docs/research/2026-10-10-real-weather.md §4.2, measured
-- facts in docs/research/2026-10-10-weather-probe-results.md.
--
-- The server (server/weather.py) sends a 'weather' message: Open-Meteo's current conditions at the
-- player, every 10 min or 25 km; beamngEarth_core hands it to M.onWeather. Every value moves towards
-- its new target smoothly (exponential approach, 95 % in LERP_S) and is written through
-- core_environment, so the pause menu's Time & Weather panel, missions and other mods see the real
-- values: cloud cover (COVERAGE_CURVE, a calibration table), cirrus from high cloud, the clouds'
-- drift from the wind aloft, fog from visibility (density compensated for the height of the ground:
-- the engine's fog thins with absolute z, shaders/common/sceneFog.h.hlsl), ground wind with gusts, lulls and direction swings
-- (core_environment.setGroundWind reaches every vehicle), rain and snow (the level's Precipitation
-- objects; core_environment.setPrecipitation writes 'numOfDrops', a field the engine does not
-- have), and the air temperature (LevelInfo temperatureCurveC, which core_environment turns into
-- the engine's sea-level temperature every frame; the engine lowers it with height itself). The
-- real UTC offset goes to beamngEarth_sky (its clock), and the cloud base above the ground too (sky
-- owns the layer's ground-following altitude and the rebase weather-offset shift).
-- The sun dims under a thick cloud deck (ScatterSky.brightness, the direct light only: the game's
-- clouds cast no shadows, so under overcast the car kept a sharp sun shadow; in-game 2026-10-10 the
-- auto exposure keeps the scene's brightness while the shadows fade). The level's own value is put
-- back when the weather stops driving it (manual mode, a mission).
-- Gravity follows the player (GRAVITY_ENABLED, independent of the mode): the server's 'gravity'
-- message (server/gravity.py: WGS84 normal + EGM2008 anomaly at the player, every 2 km / 200 m of
-- height) with the free-air gradient for the height since; normal gravity by latitude and height
-- until the first message (or with a server without it). Changes are written in steps of at most
-- GRAVITY_STEP per check, so a new value is approached smoothly. A gravity change by anyone else
-- (the menu's presets) is kept until setMode('real') (S.gravityOverride); it does not touch the mode.
-- Everything runs in the BeamNG.Earth level only.
--
-- Modes: 'real' (the default) = drive the weather; 'manual' = hands off (the game's own controls).
-- A change made by anyone else (the pause menu, another mod such as BWM_Weather) after the start-up
-- grace period that really changes a value (the engine's value is compared: a setState repeating
-- the current values is no change) switches to 'manual' with one toast and saves that as the
-- setting (beamngEarth_settings, so the settings page shows it); choosing Real again goes back.
-- 'auto' (the old default: real unless BWM_Weather was changing the weather) means 'real'.
-- Missions and the big map pause it (they set and restore their own weather).
-- Console: beamngEarth_weather.setMode('real'|'manual')   .status()   .setGravityEnabled(bool)

local M = {}

M.LEVEL_ID = 'beamng_earth'
M.LERP_S = 60              -- a new target is 95 % reached after this long
M.APPLY_S = 0.5            -- writes at 2 Hz, only of values that changed
M.WIND_APPLY_S = 0.25      -- setGroundWind queues Lua into every vehicle: 4 Hz (a gust rises in 1-3 s),
                           -- and only when the wind moved by WIND_EPS_MS
M.GROUND_CHECK_S = 5       -- ground height under the player (fog compensation)
M.GRACE_S = 20             -- after a level start: BWM_Weather's start transition, env's setState(init_env)
M.USER_QUIET_S = 3         -- after someone else's change: core_environment lerps it over 1.15 s and
                           -- each of its setters cancels a running lerp, so write nothing meanwhile
M.MISSION_SETUP_S = 2      -- missions set their weather just before 'started': a change switches to
                           -- manual only when no mission started within this long
M.MISSION_RESUME_S = 5
M.BIGMAP_TAIL_S = 2        -- the big map restores fog, clouds and time as it closes
M.TEMP_WRITE_S = 10        -- temperature curve writes at most this often
M.TEMP_EPS_C = 0.2

-- Calibration: sky fraction of the volumetric deck (0..1, Open-Meteo max(low, mid) / 100) ->
-- CloudLayer.coverage, linear between rows. From the probe (camera straight up, base ~2 km above
-- the ground): nothing below ~0.7, 7 % at 0.79, 19 % at 0.90; looking 20 deg up, a band of small
-- cumulus at 0.7-0.95. Rows above 0.19 are guesses (overcast was never seen up to 0.95; the stock
-- UI goes to 3, BWM_Weather to 2.9): retune here.
M.COVERAGE_CURVE = {
  { 0.00, 0.00 },
  { 0.02, 0.70 },
  { 0.07, 0.79 },
  { 0.19, 0.90 },
  { 0.50, 1.10 },   -- uncalibrated
  { 0.80, 1.50 },   -- uncalibrated
  { 1.00, 2.00 },   -- uncalibrated
}
-- Cirrus: invisible while cirrusDensity is 0 (the CloudLayer default); 0.5 density + 0.5 coverage
-- gave faint streaks in the probe.
M.CIRRUS_DENSITY_MIN, M.CIRRUS_DENSITY_MAX = 0.3, 0.6
-- Fog: extinction sigma = 3.912 / visibility (Koschmieder, 2 % contrast); the engine's density at
-- z = 0 is sigma * exp(groundZ / height), so the player's ground gets sigma.
M.FOG_K = 3.912
M.FOG_VIS_MIN, M.FOG_VIS_MAX = 50, 100000
M.FOG_VIS_DEFAULT = 60000  -- no visibility in the data
M.FOG_HEIGHT = 1000        -- fogAtmosphereHeight, m (the menu's range is 1-1000)
M.FOG_HEIGHT_FOG = 500     -- with fog codes; lower makes valleys below the player opaque
M.FOG_FOLLOW_GROUND = true
M.FOG_DENSITY_MAX = 0.05   -- the menu's maximum (50 / 1000)
-- Clouds drift with the wind aloft; CloudLayer.windSpeed is a texture-scroll scalar (levels 0.1-0.2).
M.CLOUD_WIND_SCALE, M.CLOUD_WIND_MIN, M.CLOUD_WIND_MAX = 0.02, 0.02, 0.5
M.CLOUD_ABOVE_MIN_KM, M.CLOUD_ABOVE_MAX_KM = 0.3, 6
-- Rain/snow fraction of the drop pool: sqrt(rate / full), at least PRECIP_MIN while it falls.
M.RAIN_FULL_MMH, M.SNOW_FULL_CMH, M.PRECIP_MIN = 8, 2, 0.08
M.RAIN_NAME = 'bngEarthRain'      -- level objects (tools/make_level_assets.py)
M.SNOW_NAME = 'bngEarthSnow'
M.RAIN_POOL, M.SNOW_POOL = 4000, 3000                       -- drops (probe: 9000 cost +0.2 ms)
M.GUSTS = true
M.GUST_TAU_S, M.GUST_STD = 4, 0.4
-- The gust signal u (-1..1): above 0 the speed rises towards the gust speed, below 0 it falls to a
-- lull, at u = -1 to (1 - GUST_LULL) of the mean.
M.GUST_LULL = 0.5
-- The direction swings with its own slower signal; its spread grows with the gustiness
-- (gust / mean): DIR_STD_MIN at steady wind, + DIR_GUSTINESS_DEG per unit of gust factor above 1,
-- at most DIR_STD_MAX (real gusty wind veers 10-30 degrees). None below DIR_CALM_MS.
M.DIR_TAU_S = 8
M.DIR_STD_MIN_DEG, M.DIR_STD_MAX_DEG, M.DIR_GUSTINESS_DEG = 4, 25, 15
M.DIR_CALM_MS = 0.5
M.WIND_EPS_MS = 0.15
-- Sun under the deck: brightness factor 1 - SUN_DIM_MAX * deck^SUN_DIM_EXP (a few clouds barely
-- matter, overcast leaves 0.3; probe at noon: 0.35 made the car's shadow faint).
M.SUN_DIM_MAX, M.SUN_DIM_EXP = 0.7, 1.5
M.SUN_EPS = 0.02           -- write only when the factor moved this much (each write is a postApply)
M.GRAVITY_ENABLED = true
M.GRAVITY_EPS = 1e-4       -- m/s^2; ~30 m of climb (setGravity queues Lua into every vehicle)
M.GRAVITY_CHECK_S = 1
M.GRAVITY_DEFAULT = 9.81   -- the level's value, restored when switched off
M.GRAVITY_STEP = 1e-3      -- m/s^2 per check at most (100 mGal/s) once ours is set: a smooth approach
M.FREE_AIR = 3.086e-6      -- m/s^2 per m of height (normal free-air gradient)

-- core_environment's onEnvironmentChanged keys that are real weather -> the core_environment getter
-- of the engine's value (the hook fires before a value is applied) and the tolerance within which
-- an incoming value is no change. Vectors by component; fogDensity relative, in either scale
-- (setState carries the menu's x1000, setFogDensity the engine's); numOfDrops is no engine field, so
-- getPrecipitation may have nothing to compare with (a change). The menu's sliders (0.39
-- EnvWeather.vue) send their value rounded to their step when the number field loses focus, unedited:
-- a tolerance is at least half a step (cover 0.01, cloud speed 0.1, direction 5 deg, fog 0.005 x1000),
-- and below one step.
M.FOG_STEP_HALF = 2.6e-6   -- the fog tolerance never goes below half the menu's step (engine scale)
M.WEATHER_KEYS = {
  cloudCover = { 'getCloudCover', 0.006 },
  cloudCirrusCoverage = { 'getCloudCirrusCoverage', 0.01 },
  cloudCirrusDensity = { 'getCloudCirrusDensity', 0.01 },
  cloudCirrusAltitudeKm = { 'getCloudCirrusAltitudeKm', 0.05 },
  cloudWindDirection = { 'getCloudWindDirection', 0.045 }, -- unit vector: ~2.6 deg
  windSpeed = { 'getWindSpeed', 0.051 },
  fogDensity = { 'getFogDensity', 0.02 },
  fogAtmosphereHeight = { 'getFogAtmosphereHeight', 1 },
  groundWind = { 'getGroundWind', 0.2 },
  cloudAltitudeKm = { 'getCloudAltitudeKm', 0.05 },
  numOfDrops = { 'getPrecipitation', 0.5 },
}

------------------------------------------------------------------------------------------
-- pure

local function clamp(v, lo, hi) return math.max(lo, math.min(hi, v)) end

-- Sky fraction (0..1) -> CloudLayer.coverage over COVERAGE_CURVE.
function M.coverageFor(f, curve)
  curve = curve or M.COVERAGE_CURVE
  f = tonumber(f) or 0
  if f <= curve[1][1] then return curve[1][2] end
  for i = 2, #curve do
    local a, b = curve[i - 1], curve[i]
    if f <= b[1] then return a[2] + (b[2] - a[2]) * (f - a[1]) / (b[1] - a[1]) end
  end
  return curve[#curve][2]
end

-- Sky fraction of the deck (0..1) -> the sun's brightness factor.
function M.sunDimFor(deck)
  return 1 - M.SUN_DIM_MAX * clamp(tonumber(deck) or 0, 0, 1) ^ M.SUN_DIM_EXP
end

-- High cloud cover (%) -> cirrusCoverage, cirrusDensity.
function M.cirrusFor(highPct)
  local c = clamp((tonumber(highPct) or 0) / 100, 0, 1)
  if c <= 0 then return 0, 0 end
  return c, M.CIRRUS_DENSITY_MIN + (M.CIRRUS_DENSITY_MAX - M.CIRRUS_DENSITY_MIN) * c
end

-- Visibility (m, nil = unknown) -> extinction per metre.
function M.fogSigma(visM)
  local v = tonumber(visM) or M.FOG_VIS_DEFAULT
  return M.FOG_K / clamp(v, M.FOG_VIS_MIN, M.FOG_VIS_MAX)
end

-- The engine's fogDensity that gives extinction sigma at height groundZ (nil = no compensation).
function M.fogDensityFor(sigma, groundZ, height)
  local d = sigma
  if M.FOG_FOLLOW_GROUND and groundZ then d = sigma * math.exp(groundZ / height) end
  return math.min(d, M.FOG_DENSITY_MAX)
end

-- Ground wind: meteorological "from" direction (deg clockwise from north) and speed -> the world
-- vector the air moves along (ENU: +x east, +y north; the UI's x = s sin(dir), y = s cos(dir) with
-- dir = towards). A west wind (from 270) blows towards +x.
function M.windVec(ms, fromDeg)
  local t = math.rad(tonumber(fromDeg) or 0)
  ms = tonumber(ms) or 0
  return -ms * math.sin(t), -ms * math.cos(t)
end

-- Unit vector the clouds drift towards.
function M.cloudDir(fromDeg)
  return M.windVec(1, fromDeg)
end

-- Rate -> fraction of the drop pool.
function M.precipPct(rate, full)
  rate = tonumber(rate) or 0
  if rate <= 0.05 then return 0 end
  return clamp(math.sqrt(rate / full), M.PRECIP_MIN, 1)
end

-- Normal gravity (WGS84 Somigliana) at geodetic latitude latDeg, minus the free-air gradient for
-- height h (m above the ellipsoid ~ sea level ~ our z). m/s^2, positive.
function M.gravityAt(latDeg, h)
  local s = math.sin(math.rad(tonumber(latDeg) or 0))
  local s2 = s * s
  return 9.7803253359 * (1 + 0.00193185265241 * s2) / math.sqrt(1 - 0.00669437999013 * s2) - M.FREE_AIR * (tonumber(h) or 0)
end

-- The server's gravity msg (g at height msg.h) carried to height h by the free-air gradient.
function M.gravityFrom(msg, h)
  return msg.g - M.FREE_AIR * ((tonumber(h) or 0) - (tonumber(msg.h) or 0))
end

-- The next value to write from have towards want: at most step away from have (no step limit
-- without a previous value of ours).
function M.gravityToward(have, want, step)
  if not have then return want end
  return have + math.max(-step, math.min(step, want - have))
end

-- One step of the gust process (Ornstein-Uhlenbeck around 0, std GUST_STD, time constant tau);
-- n is a unit-variance random number. The speed is ms + (gust - ms) * max(0, u).
function M.gustStep(u, dt, tau, n)
  local s = M.GUST_STD * math.sqrt(2 / tau)
  return clamp(u - u * dt / tau + s * math.sqrt(dt) * n, -1, 1)
end

-- Wind speed for the gust signal u.
function M.gustSpeed(ms, gustMs, u)
  if u >= 0 then return ms + (math.max(ms, gustMs) - ms) * u end
  return ms * (1 + M.GUST_LULL * math.max(u, -1))
end

-- Spread (1 sigma, degrees) of the direction swings.
function M.dirStdDeg(ms, gustMs)
  if ms < M.DIR_CALM_MS then return 0 end
  return clamp(M.DIR_STD_MIN_DEG + M.DIR_GUSTINESS_DEG * (math.max(ms, gustMs) / ms - 1), M.DIR_STD_MIN_DEG, M.DIR_STD_MAX_DEG)
end

-- The weather message -> the targets every value moves towards.
function M.targets(msg)
  local cover, wind = msg.cover or {}, msg.wind or {}
  local aloft = msg.aloft or wind
  local deck = math.max(tonumber(cover.low) or 0, tonumber(cover.mid) or 0) / 100
  local cc, cd = M.cirrusFor(cover.high)
  local wx, wy = M.cloudDir(wind.fromDeg)
  local cx, cy = M.cloudDir(aloft.fromDeg)
  local ms = tonumber(wind.ms) or 0
  return {
    coverage = M.coverageFor(deck),
    sunDim = M.sunDimFor(deck),
    cirrusCoverage = cc, cirrusDensity = cd,
    cloudAboveKm = clamp((tonumber(msg.baseAglM) or 1800) / 1000, M.CLOUD_ABOVE_MIN_KM, M.CLOUD_ABOVE_MAX_KM),
    cloudDirX = cx, cloudDirY = cy,
    cloudWindSpeed = clamp((tonumber(aloft.ms) or 0) * M.CLOUD_WIND_SCALE, M.CLOUD_WIND_MIN, M.CLOUD_WIND_MAX),
    fogSigma = M.fogSigma(msg.visM),
    fogHeight = msg.fog and M.FOG_HEIGHT_FOG or M.FOG_HEIGHT,
    windMs = ms, gustMs = math.max(ms, tonumber(wind.gustMs) or ms),
    windDirX = wx, windDirY = wy,
    rainPct = M.precipPct(msg.rainMmH, M.RAIN_FULL_MMH),
    snowPct = M.precipPct(msg.snowCmH, M.SNOW_FULL_CMH),
    tempC = tonumber(msg.slTempC) or 15,
  }
end

-- Exponential approach of every numeric field of cur towards tgt: 95 % after T seconds.
function M.step(cur, tgt, dt, T)
  local a = 1 - math.exp(-3 * dt / T)
  for k, v in pairs(tgt) do
    local c = cur[k]
    cur[k] = (type(c) == 'number') and (c + (v - c) * a) or v
  end
  return cur
end

-- x, y of a vector (vec3 cdata or the menu's { x, y, z } table), or nil.
local function xyOf(v)
  local t = type(v)
  if t ~= 'table' and t ~= 'cdata' and t ~= 'userdata' then return nil end
  local ok, x, y = pcall(function() return tonumber(v.x), tonumber(v.y) end)
  if ok and x and y then return x, y end
end

-- Does the incoming value v of key differ from the engine's value have by more than tol?
function M.valueDiffers(key, v, have, tol)
  local vx, vy = xyOf(v)
  if vx then
    local hx, hy = xyOf(have)
    return not hx or math.abs(vx - hx) > tol or math.abs(vy - hy) > tol
  end
  v, have = tonumber(v), tonumber(have)
  if v == nil then return false end -- nothing the setter would apply
  if have == nil then return true end
  if key == 'fogDensity' then
    local t = math.max(tol * have, M.FOG_STEP_HALF)
    return math.abs(v - have) > t and math.abs(v / 1000 - have) > t
  end
  return math.abs(v - have) > tol
end

-- The real-weather keys of an onEnvironmentChanged table whose values really change, sorted. e:
-- core_environment (its getters); ref: values to compare with instead of the engine's (key -> value).
function M.changedKeys(changes, e, ref)
  local out = {}
  for k, v in pairs(changes) do
    local spec = M.WEATHER_KEYS[k]
    if spec then
      local have = ref and ref[k]
      if have == nil and e and type(e[spec[1]]) == 'function' then
        local ok, h = pcall(e[spec[1]])
        if ok then have = h end
      end
      if M.valueDiffers(k, v, have, spec[2]) then out[#out + 1] = k end
    end
  end
  table.sort(out)
  return out
end

-- BWM_Weather (the user's weather mod) changes the weather itself: automatic weather, or a
-- preset other than clear (its getWeatherState()). Named in the toast of a switch to manual.
function M.bwmFighting(state)
  if type(state) ~= 'table' then return false end
  if state.automaticWeather == true then return true end
  for _, k in ipairs({ 'targetPreset', 'currentPreset' }) do
    local p = state[k]
    if p ~= nil and p ~= '' and p ~= 'clear' then return true end
  end
  return false
end

local CODES = { [0] = 'clear', [1] = 'mainly clear', [2] = 'partly cloudy', [3] = 'overcast', [45] = 'fog', [48] = 'fog' }
function M.describe(code)
  code = tonumber(code) or -1
  if CODES[code] then return CODES[code] end
  if code >= 95 then return 'thunderstorm' elseif code >= 85 then return 'snow showers'
  elseif code >= 80 then return 'showers' elseif code >= 71 and code <= 77 then return 'snow'
  elseif code >= 61 and code <= 67 then return 'rain' elseif code >= 51 and code <= 57 then return 'drizzle' end
  return '?'
end

-- A BeamMP server's weather (its plugin's G.WEATHER, through mpsession): every player gets the same, as a weather
-- message like the world server's real one (WMO code, cloud cover %, wind m/s, visibility m, rain mm/h, snow cm/h).
M.PRESETS = {
  clear = { code = 0, cover = { low = 0, mid = 0, high = 5 }, wind = { ms = 2, fromDeg = 270 }, visM = 40000,
    slTempC = 20 },
  cloudy = { code = 3, cover = { low = 85, mid = 70, high = 40 }, wind = { ms = 4, fromDeg = 250 }, visM = 20000,
    slTempC = 14 },
  rain = { code = 63, cover = { low = 100, mid = 90, high = 60 }, wind = { ms = 6, gustMs = 10, fromDeg = 230 },
    visM = 8000, rainMmH = 4, slTempC = 11 },
  storm = { code = 95, cover = { low = 100, mid = 100, high = 80 }, wind = { ms = 13, gustMs = 22, fromDeg = 220 },
    visM = 3000, rainMmH = 15, slTempC = 17 },
  snow = { code = 73, cover = { low = 100, mid = 90, high = 60 }, wind = { ms = 5, gustMs = 8, fromDeg = 340 },
    visM = 4000, snowCmH = 1.5, slTempC = -4 },
  fog = { code = 45, cover = { low = 100, mid = 20, high = 0 }, wind = { ms = 1, fromDeg = 180 }, visM = 300,
    fog = true, slTempC = 8 },
}

-- Pure: the weather message of preset name (nil: not one).
function M.presetMsg(name)
  local p = M.PRESETS[tostring(name or ''):lower()]
  if not p then return nil end
  local m = { preset = tostring(name):lower() }
  for k, v in pairs(p) do m[k] = v end
  return m
end

function M.compass(deg)
  local names = { 'N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW' }
  return names[math.floor(((tonumber(deg) or 0) % 360) / 45 + 0.5) % 8 + 1]
end

------------------------------------------------------------------------------------------
-- runtime

local mode = 'real'
local lastMsg = nil                     -- the latest message, kept across level loads
local preset = nil                      -- a BeamMP server's weather (setPreset): its message, in place of the real one
local lastGravity = nil                 -- the latest 'gravity' message (server), kept the same way
local applying = false                  -- our own core_environment calls (their hook is not a change)
local S

local function resetState()
  S = {
    clock = 0, graceUntil = M.GRACE_S, quietUntil = 0, mission = false, bigMapUntil = 0,
    gravityOverride = nil, cur = nil, tgt = lastMsg and M.targets(lastMsg) or nil,
    applyTimer = 0, windTimer = 0, groundTimer = M.GROUND_CHECK_S, gravTimer = 0,
    groundZ = nil, gust = 0, veer = 0, gravity = nil, windSent = nil, -- windSent: our last ground wind
    temp = { at = -math.huge, value = nil, method = nil }, dirtyFrames = 0,
    precip = { [M.RAIN_NAME] = {}, [M.SNOW_NAME] = {} },
    pending = nil,  -- { at, keys }: someone else changed the weather; manual after MISSION_SETUP_S
    switched = nil, -- the last switch to manual: { at, keys, bwm }
    inOurLevel = false,
    sun = { base = nil, applied = nil }, -- the level's ScatterSky.brightness, the value we wrote
  }
end
resetState()

local function env() return rawget(_G, 'core_environment') end
local function sky() return rawget(_G, 'beamngEarth_sky') end

local function envCall(fn, ...)
  if type(fn) ~= 'function' then return false end
  applying = true
  local ok, err = pcall(fn, ...)
  applying = false
  if not ok then log('W', 'beamngEarth', 'weather: ' .. tostring(err)) end
  return ok
end

local function notify(text)
  log('W', 'beamngEarth', 'weather: ' .. text)
  local gh = rawget(_G, 'guihooks')
  if gh and gh.trigger then pcall(gh.trigger, 'toastrMsg', { type = 'warning', title = 'BeamNG.Earth weather', msg = text }) end
end

local function levelInfo()
  local li = scenetree.theLevelInfo
  if li then return li end
  local names = scenetree.findClassObjects('LevelInfo')
  return names and names[1] and scenetree.findObject(names[1]) or nil
end

local function playerPos()
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  return veh and veh:getPosition() or nil
end

local function groundUnder(p)
  local d = castRayStatic(vec3(p.x, p.y, p.z + 50), vec3(0, 0, -1), 5000)
  if d and d < 5000 then return p.z + 50 - d end
end

local function num(v, default) return tonumber(v) or default end

-- The values the engine has now, as the lerp's starting point (targets where unreadable).
local function readEngine(e, tgt)
  local c = {}
  for k, v in pairs(tgt) do c[k] = v end
  c.coverage = num(e.getCloudCover and e.getCloudCover(), tgt.coverage)
  c.cirrusCoverage = num(e.getCloudCirrusCoverage and e.getCloudCirrusCoverage(), tgt.cirrusCoverage)
  c.cirrusDensity = num(e.getCloudCirrusDensity and e.getCloudCirrusDensity(), tgt.cirrusDensity)
  c.cloudWindSpeed = num(e.getWindSpeed and e.getWindSpeed(), tgt.cloudWindSpeed)
  local d = e.getFogDensity and num((e.getFogDensity()), nil)
  local h = e.getFogAtmosphereHeight and num(e.getFogAtmosphereHeight(), nil)
  if d and h and h > 0 then
    c.fogHeight = h
    c.fogSigma = (M.FOG_FOLLOW_GROUND and S.groundZ) and d * math.exp(-S.groundZ / h) or d
  end
  local gw = e.getGroundWind and e.getGroundWind()
  if gw and gw.x then
    local l = math.sqrt(gw.x * gw.x + gw.y * gw.y)
    c.windMs = l
    if l > 0.01 then c.windDirX, c.windDirY = gw.x / l, gw.y / l end
  end
  local cw = e.getCloudWindDirection and e.getCloudWindDirection()
  if cw and cw.x and (cw.x ~= 0 or cw.y ~= 0) then c.cloudDirX, c.cloudDirY = cw.x, cw.y end
  c.rainPct, c.snowPct = 0, 0
  local sun = S.sun
  c.sunDim = (sun.applied and sun.base and sun.base > 0) and sun.applied / sun.base or 1
  return c
end

local function setIf(get, set, want, eps)
  local have = get and tonumber(get())
  if have and math.abs(have - want) <= eps then return end
  envCall(set, want)
end

-- Temperature: a flat temperatureCurveC at the sea-level temperature. core_environment re-reads
-- the curve when LevelInfo is editor-dirty (environment.lua onUpdate), so the flag is set for a few
-- frames. Which setter form the engine takes is found once per level (read back).
local function curveIs(li, tC)
  local ok, c = pcall(function() return li:getTemperatureCurveC() end)
  if not ok then return true end -- no getter: trust a setter that did not fail
  return type(c) == 'table' and type(c[1]) == 'table' and math.abs((tonumber(c[1][2]) or -999) - tC) < 0.01
end
local TEMP_METHODS = {
  table = function(li, tC) li:setTemperatureCurveC({ { 0, tC }, { 1, tC } }) end,
  field = function(li, tC) li:setField('temperatureCurveC', 0, string.format('0 %.2f 1 %.2f', tC, tC)) end,
}
local function writeTemperature(tC)
  local li = levelInfo()
  if not li then return false end
  local t = S.temp
  local order = t.method and { t.method } or { 'table', 'field' }
  for _, name in ipairs(order) do
    if pcall(TEMP_METHODS[name], li, tC) and curveIs(li, tC) then
      t.method = name
      pcall(function() li:setEditorDirty(true) end)
      S.dirtyFrames = 3
      return true
    end
  end
  t.method = 'n/a'
  return false
end

local function drivePrecip(name, pct, pool)
  local o = scenetree.findObject(name)
  if not o then return end
  local st = S.precip[name]
  if pct > 0 and st.pool ~= pool then
    o.numDrops = pool
    o:setField('numDrops', 0, tostring(pool))
    o:postApply()
    st.pool = pool
  end
  if st.pct == nil or math.abs(pct - st.pct) > 0.01 then
    if type(o.setPercentage) == 'function' then o:setPercentage(pct) end
    st.pct = pct
  end
  local hide = pct <= 0.001
  if st.hidden ~= hide then o:setHidden(hide); st.hidden = hide end
end

local function cameraZ()
  local cam = rawget(_G, 'core_camera')
  if not cam or not cam.getPositionXYZ then return nil end
  local ok, _, _, z = pcall(cam.getPositionXYZ)
  return ok and tonumber(z) or nil
end

local function applyPrecip(e, c)
  local rain, snow = c.rainPct, c.snowPct
  -- no rain above the cloud base
  local alt = e.getCloudAltitudeKm and tonumber(e.getCloudAltitudeKm())
  local cz = cameraZ()
  if alt and cz and cz > alt * 1000 then rain, snow = 0, 0 end
  drivePrecip(M.RAIN_NAME, rain, M.RAIN_POOL)
  drivePrecip(M.SNOW_NAME, snow, M.SNOW_POOL)
end

-- The sun: ScatterSky.brightness = the level's own value * factor. Restored only while it still
-- holds our value (another mod may have set its own since).
local function sunObject()
  local names = scenetree.findClassObjects and scenetree.findClassObjects('ScatterSky')
  return names and names[1] and scenetree.findObject(names[1]) or nil
end
local function writeSun(factor)
  local o = sunObject()
  if not o then return end
  local sun = S.sun
  local have = tonumber(o:getField('brightness', 0))
  if not have then return end
  if not sun.base or (sun.applied and math.abs(have - sun.applied) > 1e-3) then
    sun.base = have -- first write, or someone else changed it: their value is the new base
  end
  local want = sun.base * factor
  if sun.applied and math.abs(want - sun.applied) < M.SUN_EPS * sun.base then return end
  o:setField('brightness', 0, string.format('%.4f', want))
  o:postApply()
  sun.applied = tonumber(o:getField('brightness', 0)) or want
end
local function restoreSun()
  local sun = S.sun
  if not sun.applied then return end
  local o = sunObject()
  local have = o and tonumber(o:getField('brightness', 0))
  if have and math.abs(have - sun.applied) <= 1e-3 then
    o:setField('brightness', 0, string.format('%.4f', sun.base))
    o:postApply()
  end
  sun.applied = nil
end
M._restoreSun = function() restoreSun() end -- tests

local function apply(e)
  local c = S.cur
  setIf(e.getCloudCover, e.setCloudCover, c.coverage, 0.005)
  writeSun(c.sunDim)
  setIf(e.getCloudCirrusCoverage, e.setCloudCirrusCoverage, c.cirrusCoverage, 0.005)
  setIf(e.getCloudCirrusDensity, e.setCloudCirrusDensity, c.cirrusDensity, 0.005)
  local l = math.sqrt(c.cloudDirX ^ 2 + c.cloudDirY ^ 2)
  if l > 1e-3 then
    local x, y = c.cloudDirX / l, c.cloudDirY / l
    local have = e.getCloudWindDirection and e.getCloudWindDirection()
    if not (have and have.x and (have.x * x + have.y * y) > 0.9986) then envCall(e.setCloudWindDirection, x, y, 0) end -- 3 deg
  end
  setIf(e.getWindSpeed, e.setWindSpeed, c.cloudWindSpeed, 0.005)
  if S.groundZ then
    local want = M.fogDensityFor(c.fogSigma, S.groundZ, c.fogHeight)
    local have = e.getFogDensity and tonumber((e.getFogDensity()))
    if not have or math.abs(have - want) > 0.02 * want then envCall(e.setFogDensity, want) end
    setIf(e.getFogAtmosphereHeight, e.setFogAtmosphereHeight, c.fogHeight, 1)
  end
  local sk = sky()
  if sk and sk.setCloudAboveKm then sk.setCloudAboveKm(c.cloudAboveKm) end
  if S.clock - S.temp.at >= M.TEMP_WRITE_S and (not S.temp.value or math.abs(S.temp.value - c.tempC) > M.TEMP_EPS_C) then
    S.temp.at = S.clock
    if writeTemperature(c.tempC) then S.temp.value = c.tempC end
  end
  applyPrecip(e, c)
end

local function applyWind(e, dt)
  local c = S.cur
  local speed, dx, dy = c.windMs, c.windDirX, c.windDirY
  if M.GUSTS then
    S.gust = M.gustStep(S.gust, dt, M.GUST_TAU_S, (math.random() * 2 - 1) * math.sqrt(3))
    S.veer = M.gustStep(S.veer, dt, M.DIR_TAU_S, (math.random() * 2 - 1) * math.sqrt(3))
    speed = M.gustSpeed(c.windMs, c.gustMs, S.gust)
    local a = math.rad(S.veer / M.GUST_STD * M.dirStdDeg(c.windMs, c.gustMs))
    local ca, sa = math.cos(a), math.sin(a)
    dx, dy = c.windDirX * ca - c.windDirY * sa, c.windDirX * sa + c.windDirY * ca
  end
  local l = math.sqrt(dx ^ 2 + dy ^ 2)
  local x, y = 0, 0
  if l > 1e-3 then x, y = speed * dx / l, speed * dy / l end
  local have = e.getGroundWind and e.getGroundWind()
  if have and have.x and math.abs(have.x - x) < M.WIND_EPS_MS and math.abs(have.y - y) < M.WIND_EPS_MS then return end
  if envCall(e.setGroundWind, x, y, 0) then S.windSent = { x = x, y = y } end
end

local function applyGravity(e)
  if S.gravityOverride or not e.setGravity then return end
  local want = S.gravity
  if M.GRAVITY_ENABLED then
    local p = playerPos()
    if not p then return end
    if lastGravity then
      want = M.gravityFrom(lastGravity, p.z)
    else
      local names = scenetree.findClassObjects('TimeOfDay')
      local tod = names and names[1] and scenetree.findObject(names[1])
      if not tod then return end
      want = M.gravityAt(tonumber(tod.latitude) or 0, p.z)
    end
  elseif S.gravity == nil then
    return -- never set: nothing to restore
  else
    want = M.GRAVITY_DEFAULT
  end
  local have = e.getGravity and tonumber(e.getGravity())
  if have and math.abs(-have - want) <= M.GRAVITY_EPS then
    if not M.GRAVITY_ENABLED then S.gravity = nil end
    return
  end
  if M.GRAVITY_ENABLED then want = M.gravityToward(S.gravity, want, M.GRAVITY_STEP) end
  if envCall(e.setGravity, -want) then S.gravity = M.GRAVITY_ENABLED and want or nil end
end

local function hidePrecip()
  for name in pairs(S.precip) do pcall(drivePrecip, name, 0, 0) end
  pcall(restoreSun)
end

-- Is BWM_Weather changing the weather itself right now?
local function bwmNow()
  local b = rawget(_G, 'BWM_Weather_main')
  if not (b and b.getWeatherState) then return false end
  local ok, st = pcall(b.getWeatherState)
  return ok and M.bwmFighting(st) or false
end

-- The big map sets fog, clouds and time for its view and puts them back as it closes: not a change
-- by the player (and nothing is written meanwhile).
local function bigMapBusy()
  if S.clock < S.bigMapUntil then return true end
  local bm = rawget(_G, 'freeroam_bigMapMode')
  if not bm then return false end
  local ok, on = pcall(function()
    return (bm.bigMapActive and bm.bigMapActive()) or (bm.isTransitionActive and bm.isTransitionActive())
  end)
  return ok and on and true or false
end

-- Someone else changed the weather: real weather steps back to manual, saved as the setting (the
-- settings page shows it; settings.set applies the mode again, which is a no-op now), one toast.
local function switchToManual()
  local p = S.pending
  p.bwm = bwmNow()
  M.setMode('manual')
  S.switched = p
  local st = rawget(_G, 'beamngEarth_settings')
  if st and st.set then
    local ok, err = pcall(st.set, 'weatherMode', 'manual')
    if not ok then log('W', 'beamngEarth', 'weather: saving the mode failed: ' .. tostring(err)) end
  end
  notify('Weather set to Manual because it was changed'
    .. (p.bwm and ' (BWM_Weather is changing the weather: set it to Clear with automatic weather off to keep real weather)' or '')
    .. '. Choose Real in the BeamNG.Earth settings to go back.')
end

local function update(dt)
  if not S.inOurLevel or not scenetree.MissionGroup then return end
  local e = env()
  if not e then return end
  if S.pending and S.clock >= S.pending.at + M.MISSION_SETUP_S then switchToManual() end
  S.groundTimer = S.groundTimer + dt
  if S.groundTimer >= M.GROUND_CHECK_S then
    S.groundTimer = 0
    local p = playerPos()
    if p then S.groundZ = groundUnder(p) or S.groundZ end
  end
  if S.clock < S.graceUntil or S.mission then return end
  S.gravTimer = S.gravTimer + dt
  if S.gravTimer >= M.GRAVITY_CHECK_S then S.gravTimer = 0; applyGravity(e) end
  if mode ~= 'real' or not S.tgt or S.clock < S.quietUntil or bigMapBusy() then return end
  if not S.cur then S.cur = readEngine(e, S.tgt) end
  M.step(S.cur, S.tgt, dt, M.LERP_S)
  S.applyTimer = S.applyTimer + dt
  if S.applyTimer >= M.APPLY_S then S.applyTimer = 0; apply(e) end
  S.windTimer = S.windTimer + dt
  if S.windTimer >= M.WIND_APPLY_S then applyWind(e, S.windTimer); S.windTimer = 0 end
end

------------------------------------------------------------------------------------------
-- hooks and console API

function M.onWeather(msg)
  if type(msg) ~= 'table' then return end
  if not preset then
    lastMsg = msg
    S.tgt = M.targets(msg)
  end
  local sk = sky()
  local off = tonumber(msg.utcOffsetS)
  if sk and sk.setRealUtcOffset and off then sk.setRealUtcOffset(off / 3600, tonumber(msg.lat), tonumber(msg.lon)) end
end

-- The server's real gravity at the player (server/gravity.py; beamngEarth_core hands it over).
function M.onGravity(msg)
  if type(msg) ~= 'table' or type(msg.g) ~= 'number' or msg.g < 9 or msg.g > 10.5 then return end
  lastGravity = { g = msg.g, h = tonumber(msg.h) or 0, source = msg.source, lat = msg.lat, lon = msg.lon }
  S.gravTimer = M.GRAVITY_CHECK_S -- applied on the next update
end

-- A new server session (core, on hello): back to normal gravity until its message arrives.
function M.clearGravity() lastGravity = nil end

-- One line for the settings page: the gravity in use and where it comes from.
function M.gravityLine()
  if not M.GRAVITY_ENABLED then return 'off (level default ' .. M.GRAVITY_DEFAULT .. ' m/s2)' end
  local src = lastGravity and (lastGravity.source == 'egm2008' and 'EGM2008 (server)' or 'normal (server)')
    or 'normal by latitude (no server value)'
  return (S.gravity and string.format('%.5f m/s2', S.gravity) or 'not set yet') .. ', ' .. src
end

function M.onUpdate(dtReal)
  dtReal = dtReal or 0
  S.clock = S.clock + dtReal
  if S.dirtyFrames > 0 then
    S.dirtyFrames = S.dirtyFrames - 1
    if S.dirtyFrames == 0 then
      local li = levelInfo()
      if li then pcall(function() li:setEditorDirty(false) end) end
    end
  end
  local ok, err = pcall(update, dtReal)
  if not ok then log('W', 'beamngEarth', 'weather: ' .. tostring(err)) end
end

-- core_environment's hook, fired before a change is applied (setState: the whole table its caller
-- passed, possibly with unchanged values). Our own writes are not changes (applying).
function M.onEnvironmentChanged(changes)
  if applying or type(changes) ~= 'table' then return end
  if S.clock < S.graceUntil or S.mission or not S.inOurLevel then return end
  if changes.gravity ~= nil then -- gravity is its own setting: the game's value is kept
    S.gravityOverride = S.clock
    S.quietUntil = S.clock + M.USER_QUIET_S
  end
  if bigMapBusy() then return end
  -- a cloud altitude someone else sets is kept, in manual mode too: the layer stops following the ground
  if changes.cloudAltitudeKm ~= nil and #M.changedKeys({ cloudAltitudeKm = changes.cloudAltitudeKm }, env()) > 0
    and sky() and sky().setCloudFollow then sky().setCloudFollow(false) end
  if mode ~= 'real' then return end
  -- the ground wind: setGroundWind applies before its hook, so compare with what we wrote last
  local keys = M.changedKeys(changes, env(), { groundWind = S.windSent })
  if #keys == 0 then return end
  S.quietUntil = S.clock + M.USER_QUIET_S
  if not S.pending then S.pending = { at = S.clock, keys = keys } end
end

function M.onAnyMissionChanged(state)
  if state == 'started' then
    S.mission = true
    pcall(restoreSun)
    if S.pending then -- the mission's own setup, just before 'started': no switch
      S.pending = nil
      local sk = sky()
      if sk and sk.setCloudFollow then sk.setCloudFollow(true) end
    end
    if S.gravityOverride and S.gravityOverride >= S.clock - M.MISSION_SETUP_S then S.gravityOverride = nil end
  elseif state == 'stopped' then
    S.mission = false
    S.graceUntil = math.max(S.graceUntil, S.clock + M.MISSION_RESUME_S) -- its restore ran just before
    S.cur = nil
  end
end

function M.onBeforeBigMapActivated() S.bigMapUntil = S.clock + M.BIGMAP_TAIL_S end
M.onBigmapStartTransition = M.onBeforeBigMapActivated
function M.onDeactivateBigMapCallback()
  S.bigMapUntil = S.clock + M.BIGMAP_TAIL_S
  S.cur = nil -- from the values it put back
end

local function onLevel()
  resetState()
  S.inOurLevel = getCurrentLevelIdentifier and getCurrentLevelIdentifier() == M.LEVEL_ID
  local sk = sky()
  if sk and sk.setCloudFollow then sk.setCloudFollow(true) end
end
M.onClientStartMission = onLevel
function M.onClientEndMission() resetState() end
-- A Lua reload mid-level: settings.lua loads (and applies) before this extension, and no level
-- start follows to apply again, so the saved mode is read here.
function M.onExtensionLoaded()
  local st = rawget(_G, 'beamngEarth_settings')
  local saved = st and st.get and st.get('weatherMode')
  if saved then M.setMode(saved) end
  if scenetree.MissionGroup and getCurrentLevelIdentifier and getCurrentLevelIdentifier() then onLevel() end
end

-- 'real' (the default) or 'manual'; 'auto' (the old default) means 'real'. Safe to repeat (the
-- settings apply the saved mode again after a switch to manual).
function M.setMode(m)
  if m == 'auto' then m = 'real' end
  if m ~= 'real' and m ~= 'manual' then return 'mode must be "real" or "manual"' end
  if m == 'manual' and mode ~= 'manual' then M.manualBy = debug.traceback('', 2) end   -- (who: the MP status)
  mode = m
  S.pending = nil
  if mode == 'real' then
    S.gravityOverride, S.cur, S.quietUntil, S.windSent = nil, nil, 0, nil
    local sk = sky()
    if sk and sk.setCloudFollow then sk.setCloudFollow(true) end
  else
    hidePrecip()
  end
  return 'weather mode: ' .. mode
end
function M.getMode() return mode end

-- A BeamMP server's weather preset (M.PRESETS; nil or 'real': the real weather again, from the next real message).
function M.setPreset(name)
  local m = M.presetMsg(name)
  preset = m
  if m then
    lastMsg = m
    if S then S.tgt = M.targets(m) end
  end
  return m ~= nil
end

function M.preset() return preset and preset.preset or nil end

function M.setGravityEnabled(on)
  M.GRAVITY_ENABLED = on and true or false
  S.gravTimer = M.GRAVITY_CHECK_S
  return 'real gravity ' .. (M.GRAVITY_ENABLED and 'on' or 'off')
end

-- One line for the BeamNG.Earth window.
function M.statusLine()
  local m = lastMsg
  local head = 'Weather (' .. ((preset and mode == 'real') and ('server: ' .. preset.preset) or mode)
    .. ((mode == 'manual' and S.switched) and ', changed in the game' or '') .. ')'
  if not m then return head .. ': no data yet' end
  local w = m.wind or {}
  return string.format('%s: %.0f C, %s, wind %.0f m/s from %s, visibility %s', head, tonumber(m.tempC) or 0,
    M.describe(m.code), tonumber(w.ms) or 0, M.compass(w.fromDeg),
    m.visM and string.format('%.0f km', m.visM / 1000) or '?')
end

-- The latest weather message (code, cover, wind, tempC, ...) as a plain table, unlike status() (a dumps() string
-- for the console): the world map's weather badge (settings.uiMapState) reads it every second, too often to parse.
function M.current() return lastMsg end

function M.status()
  return dumps({ mode = mode, pending = S.pending, switched = S.switched, bwmFighting = bwmNow(),
    gravityOverride = S.gravityOverride ~= nil, bigMap = bigMapBusy(), inOurLevel = S.inOurLevel, grace = math.max(0, S.graceUntil - S.clock), mission = S.mission,
    groundZ = S.groundZ, gravity = S.gravity, gravityMsg = lastGravity, temperatureCurve = S.temp.method, tempC = S.temp.value,
    msg = lastMsg, target = S.tgt, current = S.cur, line = M.statusLine() })
end

return M
